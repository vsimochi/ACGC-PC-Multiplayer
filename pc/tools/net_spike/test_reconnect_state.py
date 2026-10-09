#!/usr/bin/env python3
"""test_reconnect_state.py - the REAL "reused peer slot never inherits per-peer state" test
(foundation contract section 4, last bullet), replacing what test_pickup_sync.py Test I and
test_drop_sync.py Test J could not prove.

Why the old tests were insufficient (prior audit): they closed the socket without a DISCONNECT, so
the host kept the old slot until its 5 s timeout and the reconnect landed in a DIFFERENT slot; and
their ever-incrementing request_id never repeated, so a stale dedup cache could never be hit.

This test forces the dangerous case on purpose:
  1. connection #1 is the ONLY peer -> slot k (0 on an otherwise idle host);
  2. request_id=N gets a real host answer, which the host caches per slot (dedup/replay cache);
  3. a proper DISCONNECT (or a same-address nonce restart, or a timeout) frees slot k;
  4. connection #2 lands in the SAME slot k (asserted -- otherwise the test is inconclusive and
     FAILS its precondition rather than passing vacuously);
  5. connection #2 sends the SAME request_id=N with DIFFERENT content.
A correct host validates #2 fresh (its answer echoes #2's tile). A buggy host replays #1's cached
decision (echoes #1's tile, and in R1 even re-grants #1's item -> duplication).

Scenarios:
  R1  pickup cache, accepted entry (needs --pickup-test-seed; SKIP otherwise): #1 picks up a live
      fixture item with rid N; #2 asks rid N for OUT_OF_RANGE -> must be REJECTED echoing (255,255),
      never "accepted + apple" (item duplication).
  R2  pickup cache, rejected entry (always runs): #1 rid N at (72,92); #2 rid N at (255,255).
  R3  drop cache: #1 drop rid M at (72,92); #2 drop rid M at (255,255).
  R4  slot taken over by a DIFFERENT client (new socket, new label/nonce) after a DISCONNECT.
  R5  same-address restart: new nonce, NO DISCONNECT (transport resets the peer by nonce rule).
  R6  slot freed by 5 s timeout (client silent), then a different client takes it.
  R7  every new connection's host->client reliable stream restarted at seq 0.
  R8  PLAYER_CONTEXT is not inherited: the reused slot, without its own context, has pickup of a live
      tile REJECTED (v2 IN_TOWN gate); control: after sending context the same pickup is ACCEPTED.
  R9  snapshot progress / world versions are not inherited: connection #1 leaves mid-snapshot;
      connection #2 in the same slot gets a complete NEW-epoch snapshot of all 30 acres from acre 0.

Run it ALONE against the host. Usage: python test_reconnect_state.py <host_ip> <port>
"""
import sys

import net_spike_lib as L

PICKUP = L.PC_NETGAME_MSG_PICKUP_RESULT
DROP = L.PC_NETGAME_MSG_DROP_RESULT
OOR = L.OUT_OF_RANGE_TILE
PLAIN_TILE = (72, 92)  # in-range ordinary tile (test_pickup_sync Test I / test_drop_sync Test J)
ITM_FOOD_CHERRY = 0x2801
ALL = []


def new_client(label):
    c = L.FakeClient(label, HOST, PORT)
    ALL.append(c)
    return c


def fresh_verdict(res, expect_tile, stale_tile):
    """'fresh' if the answer echoes the NEW request's tile, 'stale' if it echoes the old one."""
    if res is None:
        return "no-answer"
    tile = (res.ut_x, res.ut_z)
    if tile == expect_tile:
        return "fresh"
    if tile == stale_tile:
        return "stale"
    return f"other{tile}"


def slot_reuse_check(r, name, slot1, slot2):
    L.check(f"{name} precondition: second connection reused slot {slot1} (got {slot2}) -- run this test alone",
            slot1 == slot2, r)


def main():
    global HOST, PORT
    hp = L.parse_host_port(sys.argv, "usage: test_reconnect_state.py <host_ip> <port>")
    if hp is None:
        return 1
    HOST, PORT = hp
    r, pend = [], []
    rid_base = 4200

    # --- R1: accepted pickup entry must not be replayed to the reused slot ------------------------
    a = new_client("R1")
    a.connect_and_ready()
    slot1 = a.assigned_peer_id
    live = None
    rid = rid_base
    for tile in L.TOWN_FIXTURE_TILES:
        rid += 1
        res = a.pickup(tile[0], tile[1], rid)
        if res is not None and res.accepted:
            live = (tile, rid, res.granted_item)
            break
    if live is None:
        print("SKIP - R1: no live fixture item (launch the host with --pickup-test-seed for R1)")
        a.disconnect()
    else:
        tile, n, item = live
        print(f"    R1: connection #1 picked up item {item:#x} at {tile} with request_id={n}")
        a.reconnect_and_ready()
        slot_reuse_check(r, "R1", slot1, a.assigned_peer_id)
        res = a.pickup(OOR[0], OOR[1], n, claim=False)
        L.check("R1 reused slot, same request_id, different tile: host validated fresh (rejected, echoes (255,255)) "
                f"-- verdict: {fresh_verdict(res, OOR, tile)}",
                res is not None and not res.accepted and (res.ut_x, res.ut_z) == OOR, r)
        L.check("R1 no item duplication (the stale accepted grant was NOT replayed)",
                res is not None and not (res.accepted and res.granted_item == item), r)
        a.disconnect()
    L.pump_sleep(0.05)

    # --- R2: rejected pickup entry ------------------------------------------------------------------
    n = rid_base + 100
    a = new_client("R2")
    a.connect_and_ready()
    slot1 = a.assigned_peer_id
    first = a.pickup(PLAIN_TILE[0], PLAIN_TILE[1], n)
    L.check("R2 connection #1 got an answer for request_id N at (72,92)",
            first is not None and (first.ut_x, first.ut_z) == PLAIN_TILE, r)
    a.reconnect_and_ready()  # DISCONNECT + same socket + new nonce
    slot_reuse_check(r, "R2", slot1, a.assigned_peer_id)
    res = a.pickup(OOR[0], OOR[1], n, claim=False)
    L.check(f"R2 same request_id after reconnect is validated fresh -- verdict: {fresh_verdict(res, OOR, PLAIN_TILE)}",
            fresh_verdict(res, OOR, PLAIN_TILE) == "fresh", r)
    a.disconnect()
    L.pump_sleep(0.05)

    # --- R3: drop cache ------------------------------------------------------------------------------
    m = rid_base + 200
    a = new_client("R3")
    a.connect_and_ready()
    slot1 = a.assigned_peer_id
    first = a.drop(0, ITM_FOOD_CHERRY, PLAIN_TILE[0], PLAIN_TILE[1], m, claim_at=PLAIN_TILE)
    L.check("R3 connection #1 got a DROP_RESULT for request_id M at (72,92)",
            first is not None and (first.ut_x, first.ut_z) == PLAIN_TILE, r)
    a.reconnect_and_ready(new_socket=True)  # also a different source port this time
    slot_reuse_check(r, "R3", slot1, a.assigned_peer_id)
    res = a.drop(0, ITM_FOOD_CHERRY, OOR[0], OOR[1], m)
    L.check(f"R3 same drop request_id after reconnect is validated fresh -- verdict: {fresh_verdict(res, OOR, PLAIN_TILE)}",
            fresh_verdict(res, OOR, PLAIN_TILE) == "fresh" and not res.accepted, r)
    a.disconnect()
    L.pump_sleep(0.05)

    # --- R4: a different client takes over the freed slot -----------------------------------------------
    n = rid_base + 300
    p = new_client("R4-P")
    p.connect_and_ready()
    slot1 = p.assigned_peer_id
    p.pickup(PLAIN_TILE[0], PLAIN_TILE[1], n)
    p.disconnect()
    L.pump_sleep(0.05)
    q = new_client("R4-Q")
    q.connect_and_ready()
    slot_reuse_check(r, "R4", slot1, q.assigned_peer_id)
    res = q.pickup(OOR[0], OOR[1], n, claim=False)
    L.check(f"R4 a different client in the reused slot is never answered from the previous client's cache "
            f"-- verdict: {fresh_verdict(res, OOR, PLAIN_TILE)}", fresh_verdict(res, OOR, PLAIN_TILE) == "fresh", r)
    q.disconnect()
    L.pump_sleep(0.05)

    # --- R5: same-address restart (new nonce, no DISCONNECT) ----------------------------------------------
    n = rid_base + 400
    a = new_client("R5")
    a.connect_and_ready()
    slot1 = a.assigned_peer_id
    a.pickup(PLAIN_TILE[0], PLAIN_TILE[1], n)
    a.reconnect_and_ready(same_address_restart=True)
    slot_reuse_check(r, "R5", slot1, a.assigned_peer_id)
    res = a.pickup(OOR[0], OOR[1], n, claim=False)
    L.check(f"R5 nonce-restart peer is validated fresh -- verdict: {fresh_verdict(res, OOR, PLAIN_TILE)}",
            fresh_verdict(res, OOR, PLAIN_TILE) == "fresh", r)
    a.disconnect()
    L.pump_sleep(0.05)

    # --- R6: slot freed by timeout --------------------------------------------------------------------------
    n = rid_base + 500
    a = new_client("R6-silent")
    a.connect_and_ready()
    slot1 = a.assigned_peer_id
    a.pickup(PLAIN_TILE[0], PLAIN_TILE[1], n)
    a.go_silent()
    L.pump_sleep(L.PCNET_TIMEOUT_S + 1.5)
    a.state = a.STATE_CLOSED  # the host has forgotten us; never send from this address again
    b = new_client("R6-new")
    b.connect_and_ready()
    slot_reuse_check(r, "R6", slot1, b.assigned_peer_id)
    res = b.pickup(OOR[0], OOR[1], n, claim=False)
    L.check(f"R6 slot freed by timeout carries no stale cache -- verdict: {fresh_verdict(res, OOR, PLAIN_TILE)}",
            fresh_verdict(res, OOR, PLAIN_TILE) == "fresh", r)
    b.disconnect()

    # --- R8: PLAYER_CONTEXT is not inherited -------------------------------------------------------------------
    # Connection #1 sends PLAYER_CONTEXT(IN_TOWN) (the host gates pickup/drop on it). Connection #2 in the SAME
    # slot sends NO context: a pickup of a live tile must be REJECTED. Control: after #2 sends its own
    # context, the very same pickup is ACCEPTED (so the rejection really was the missing context).
    a = new_client("R8")
    a.connect_and_ready()  # sends PLAYER_CONTEXT(IN_TOWN) and waits for the snapshot
    slot1 = a.assigned_peer_id
    live = [t for t in L.TOWN_FIXTURE_TILES if a.world.tile_ut(*t) not in (None, L.EMPTY_NO)]
    ok_ctx1 = a.pickup(OOR[0], OOR[1], rid_base + 600, claim=False) is not None
    a.disconnect()
    L.pump_sleep(0.05)
    a.context_flags = None
    a.connect_and_ready()  # same socket, new nonce, no PLAYER_CONTEXT
    slot_reuse_check(r, "R8", slot1, a.assigned_peer_id)
    if not live:
        print("SKIP - R8: no live fixture tile in the snapshot (launch host with --pickup-test-seed)")
    else:
        t = live[0]
        res = a.pickup(t[0], t[1], rid_base + 601)
        L.check(f"R8 reused slot without its own PLAYER_CONTEXT: pickup of live tile {t} REJECTED "
                f"(previous connection's IN_TOWN context not inherited)", ok_ctx1 and res is not None and not res.accepted, r)
        a.send_player_context(L.PC_NETGAME_CTX_FLAG_IN_TOWN)
        res = a.pickup(t[0], t[1], rid_base + 602)
        L.check("R8 control: after sending its own context the same pickup is ACCEPTED", res is not None and res.accepted, r)
    a.disconnect()
    L.pump_sleep(0.05)

    # --- R9: snapshot progress is not inherited ------------------------------------------------------------------
    # Connection #1 leaves mid-snapshot (after a few blocks). Connection #2 in the SAME slot must receive a
    # complete snapshot from its BEGIN: a new epoch, all 30 acres starting at acre 0 -- never a resumption.
    a = new_client("R9")
    a.context_flags = None
    a.connect_and_ready(wait_snapshot=False)
    slot1 = a.assigned_peer_id
    L.DEFAULT_HUB.wait_until(lambda: a.snapshots and len(a.snapshots[-1]["blocks"]) >= 3, 5.0)
    first = a.snapshots[-1] if a.snapshots else None
    got_blocks = len(first["blocks"]) if first else 0
    a.disconnect()  # mid-snapshot
    L.pump_sleep(0.05)
    a.connect_and_ready(wait_snapshot=True)
    slot_reuse_check(r, "R9", slot1, a.assigned_peer_id)
    snap = a.completed_snapshots()[0] if a.completed_snapshots() else None
    L.info(f"R9 connection #1 left after {got_blocks} block(s) of epoch {first['epoch'] if first else '?'}")
    L.check("R9 reused slot got a complete fresh snapshot: new epoch, 30 acres in order from acre 0",
            first is not None and got_blocks < L.ACRE_NUM and snap is not None and snap["epoch"] != first["epoch"]
            and snap["blocks"] == list(range(L.ACRE_NUM)) and snap["end"]["acre_count"] == L.ACRE_NUM, r)
    L.check("R9 the new connection's world view starts fresh (no violations, every acre at its snapshot seq)",
            not a.world.violations and sorted(k[1] for k in a.world.acre_seq) == list(range(L.ACRE_NUM)), r)
    a.disconnect()

    # --- R7 (last, so it covers every connection above) ---------------------------------------------------------
    L.check("R7 every connection's host->client reliable stream started at seq 0 and stayed contiguous",
            all(c.delivered_in_order() for c in ALL), r)

    for c in ALL:
        c.close()
    return L.summary_and_exit_code(r, pend)


if __name__ == "__main__":
    sys.exit(main())
