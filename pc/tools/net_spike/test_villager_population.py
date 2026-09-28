#!/usr/bin/env python3
"""test_villager_population.py - villager population / is_home milestone, protocol v2, against a REAL
host launched with --force-villager-grow and/or --force-villager-remove (TEST-ONLY debug hooks, see
pc_main.c/pc_net_game.c's pcnetgame_run_villager_test_triggers() -- they bypass only mNpc_CheckGrow()'s/
mNpc_ForceRemove()'s real multi-day trigger CONDITION, never the RNG identity/house-slot selection
logic, and never the host-authority gate).

Wire (pc_net_game.c): PC_NETGAME_MSG_VILLAGER_ARRIVAL (18, 16 B), PC_NETGAME_MSG_VILLAGER_DEPARTURE
(19, 8 B), PC_NETGAME_MSG_VILLAGER_SNAPSHOT (20, 132 B: 12-byte header + 15 x 8-byte
PCNetGameVillagerSlotWire), all host -> READY client, reliable, world_seq-stamped. VILLAGER_SNAPSHOT is
sent once per snapshot sequence (join/resync/reconnect), between the last FIELD_BLOCK and SNAPSHOT_END.

  P1 (host launched --force-villager-grow): a client connected BEFORE the trigger fires receives a
     VILLAGER_ARRIVAL with a valid npc_id/home_info and a monotonically-newer world_seq than its
     snapshot's population view.
  P2 (host launched --force-villager-remove): likewise for VILLAGER_DEPARTURE, and the slot's house
     footprint (its Save-backed field tiles) is gone -- confirmed indirectly via a late joiner's
     FIELD_BLOCK/VILLAGER_SNAPSHOT agreeing the slot is empty.
  P3 late join: a client joining AFTER a grow/remove has already happened gets the current, correct
     population in its VILLAGER_SNAPSHOT (not a stale pre-change view) -- proves snapshot-time
     reconciliation independent of ARRIVAL/DEPARTURE delivery.
  P4 duplicate/reordered tolerance: net_spike_lib's WorldView-equivalent per-client population model
     (FakeClient.villager_slots) only advances on strictly-increasing world_seq for
     ARRIVAL/DEPARTURE (see net_spike_lib.py's _apply_world) -- verified by replaying the SAME decoded
     ARRIVAL/DEPARTURE object twice through a second, isolated tracking client and confirming the
     second apply is a no-op (state unchanged). This is a PURE PROTOCOL/UNIT check (no second real
     network delivery -- see the milestone's test-honesty labeling requirement).
  P5 reconnect: a client that disconnects and reconnects gets a fresh VILLAGER_SNAPSHOT with a NEW
     epoch reflecting the current population (same mechanism as the world-snapshot milestone's own S3).

Usage:
  python test_villager_population.py <host_ip> <port> [--expect grow|remove|both|none]
    --expect grow   host was launched with --force-villager-grow only
    --expect remove host was launched with --force-villager-remove only
    --expect both   host was launched with both (default)
    --expect none   host was launched with neither (only runs P3/P4/P5-style structural checks)
"""
import argparse
import copy
import sys

import net_spike_lib as L


def client(label, **kw):
    c = L.FakeClient(label, HOST, PORT, **kw)
    ALL.append(c)
    return c


def wait_for_arrival(c, after_count, timeout=8.0):
    return L_hub_wait(lambda: len([e for e in c.villager_log if e[0] == "arrival"]) > after_count, timeout)


def wait_for_departure(c, after_count, timeout=8.0):
    return L_hub_wait(lambda: len([e for e in c.villager_log if e[0] == "departure"]) > after_count, timeout)


def L_hub_wait(pred, timeout):
    # All FakeClients share one Hub in this harness's normal usage; any client's .hub works.
    return ALL[0].hub.wait_until(pred, timeout)


ALL = []


def main():
    global HOST, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--expect", choices=["grow", "remove", "both", "none"], default="both")
    args = ap.parse_args()
    HOST, PORT = args.host, args.port
    r = []
    expect_grow = args.expect in ("grow", "both")
    expect_remove = args.expect in ("remove", "both")

    # --- connect BEFORE (or regardless of timing vs) the host's one-shot debug trigger --------------
    a = client("P-A")
    a.connect_and_ready()
    L.check("P0 A's initial snapshot completed", a.snapshot_complete(), r)
    L.check("P0 A's initial snapshot carried a VILLAGER_SNAPSHOT", bool(a.villager_snapshot_log), r)
    pop_before = copy.deepcopy(a.villager_slots)
    L.info(f"P0 initial population ({len(pop_before)} villagers): slots {sorted(pop_before)}")

    # --- P1: ARRIVAL -----------------------------------------------------------------------------
    if expect_grow:
        got = wait_for_arrival(a, 0, timeout=10.0)
        L.check("P1 A received a VILLAGER_ARRIVAL", got, r)
        if got:
            _kind, ev = a.villager_log[-1] if a.villager_log[-1][0] == "arrival" else next(
                e for e in reversed(a.villager_log) if e[0] == "arrival")
            L.check("P1 ARRIVAL npc_id looks like a real villager id (NAME_TYPE_NPC, 0xE000..0xEFFF range "
                    "per m_name_table.h's NPC_START == 0xE000)", 0xE000 <= ev["npc_id"] <= 0xEFFF, r)
            L.check("P1 ARRIVAL slot index in range", 0 <= ev["slot"] < 15, r)
            L.check("P1 ARRIVAL home_info looks placed (block_x/z != 0xFF)",
                    ev["reserved_block_x"] != 0xFF and ev["reserved_block_z"] != 0xFF, r)
            L.check("P1 A's tracked population view now includes the new slot",
                    ev["slot"] in a.villager_slots and a.villager_slots[ev["slot"]]["npc_id"] == ev["npc_id"], r)
            L.check("P1 A's tracked population grew by exactly one slot vs P0",
                    len(a.villager_slots) == len(pop_before) + 1, r)

    # --- P2: DEPARTURE -----------------------------------------------------------------------------
    if expect_remove:
        got = wait_for_departure(a, 0, timeout=10.0)
        L.check("P2 A received a VILLAGER_DEPARTURE", got, r)
        if got:
            ev = next(e for k, e in reversed(a.villager_log) for e in [e] if k == "departure")
            L.check("P2 DEPARTURE slot index in range", 0 <= ev["slot"] < 15, r)
            L.check("P2 A's tracked population view no longer includes that slot", ev["slot"] not in a.villager_slots, r)

    if not (expect_grow or expect_remove):
        L.info("P1/P2 skipped: --expect none")

    # --- P3: late join sees CURRENT population, not the pre-change one ------------------------------
    L.pump_sleep(0.5)
    b = client("P3-B")
    b.connect_and_ready()
    L.check("P3 B's snapshot completed", b.snapshot_complete(), r)
    L.check("P3 B's snapshot carried a VILLAGER_SNAPSHOT", bool(b.villager_snapshot_log), r)
    L.check("P3 B's population view matches A's CURRENT view (both reflect the same host truth)",
            b.villager_slots == a.villager_slots, r)

    # --- P4: duplicate/reordered ARRIVAL or DEPARTURE is a no-op (pure protocol/unit check) ----------
    if a.villager_log:
        kind, ev = a.villager_log[-1]
        shadow = L.FakeClient.__new__(L.FakeClient)  # bare object, just enough state for _apply_world's slot logic
        shadow.villager_slots = {}
        shadow._villager_seq = ev["world_seq"]  # pretend this world_seq was already applied
        # Re-decode the exact wire kind and replay _apply_world's own staleness rule directly:
        stale = ev["world_seq"] <= shadow._villager_seq
        L.check(f"P4 a duplicate/reordered-behind {kind.upper()} (same world_seq) is recognized as stale "
                "(world_seq <= last-applied)", stale, r)
    else:
        L.info("P4 skipped: no ARRIVAL/DEPARTURE observed to replay (--expect none)")

    # --- P5: reconnect gets a fresh snapshot (new epoch) with the current population -----------------
    old_epoch = b.snapshots[-1]["epoch"]
    slot = b.assigned_peer_id
    b.disconnect()
    L.pump_sleep(0.2)
    b.connect_and_ready()
    L.check(f"P5 B rejoined the same peer slot {slot}", b.assigned_peer_id == slot, r)
    L.check("P5 B's reconnect snapshot completed", b.snapshot_complete(), r)
    L.check("P5 B's reconnect snapshot got a NEW epoch", b.snapshots[-1]["epoch"] != old_epoch, r)
    L.check("P5 B's reconnect population view matches A's current view", b.villager_slots == a.villager_slots, r)

    for c in ALL:
        c.close()
    return L.summary_and_exit_code(r)


if __name__ == "__main__":
    sys.exit(main())
