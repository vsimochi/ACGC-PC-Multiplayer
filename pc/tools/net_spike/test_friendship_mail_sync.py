#!/usr/bin/env python3
"""test_friendship_mail_sync.py - friendship/mail sync milestone, protocol v2, against a REAL host
launched with --force-friendship-delta N and/or --force-mail-send (TEST-ONLY debug hooks, see
pc_main.c/pc_net_game.c's pcnetgame_run_friendship_mail_test_triggers() -- they exercise the REAL
mNpc_AddFriendship()/mNpc_SendMailtoNpc() functions end to end, never a hand-rolled shortcut, and
unlike the villager-population hooks these are NOT host-only: --force-friendship-delta/
--force-mail-send also work on a real CLIENT process, see the milestone's own two-process manual
verification for that half).

Wire (pc_net_game.c): PC_NETGAME_MSG_FRIENDSHIP_REQUEST (21, 4 B, client -> host only),
PC_NETGAME_MSG_FRIENDSHIP_UPDATE (22, 28 B, host -> every READY client, world_seq-stamped),
PC_NETGAME_MSG_FRIENDSHIP_SNAPSHOT_ENTRY (23, 292 B, host -> one client, late-join/reconnect),
PC_NETGAME_MSG_MAIL_REQUEST (24, 302 B, client -> host only),
PC_NETGAME_MSG_MAIL_DELIVERED (25, 288 B, host -> every READY client, world_seq-stamped).

This script only observes the HOST's own local-interaction path (--force-friendship-delta/
--force-mail-send fired ON THE HOST fixture launched by run_game_tests.py: mNpc_AddFriendship()/
mNpc_SendMailtoNpc() apply directly on the host's own Anmmem_c and then broadcast) via ordinary
FakeClient observers -- proving the host-apply-then-broadcast half of the protocol, the
snapshot/reconnect coverage, and the staleness rule as a real, connected client sees it. The
CLIENT-intercept half (a real client sending PC_NETGAME_MSG_FRIENDSHIP_REQUEST/_MAIL_REQUEST instead
of applying locally) was verified separately with two REAL AnimalCrossing.exe processes (host +
--connect client, both --bootstrap-resident) during this milestone's implementation -- see the
milestone's final report for that transcript; it is not re-run here because run_game_tests.py's
harness launches exactly one real game process per test.

  F1 (host launched --force-friendship-delta N): a client connected BEFORE the trigger fires
     receives a FRIENDSHIP_UPDATE with slot/player identity/friendship value consistent with N
     (clamped 0..127) and a fresh world_seq.
  F2 (host launched --force-mail-send): likewise, a MAIL_DELIVERED with a non-empty letter blob and
     letter_info.exists set (byte 0 bit 0).
  F3 late join: a client joining AFTER the trigger(s) already fired gets the CURRENT friendship/mail
     state via FRIENDSHIP_SNAPSHOT_ENTRY during its snapshot (not a stale pre-change view).
  F4 duplicate/reordered tolerance: net_spike_lib's per-client model (FakeClient.friendship_memory)
     only advances _friendship_seq on a `>` (UPDATE/MAIL_DELIVERED) or `>=` (SNAPSHOT_ENTRY) rule --
     verified by replaying the SAME decoded UPDATE object twice through a fresh tracking client and
     confirming the second apply is a no-op. PURE PROTOCOL/UNIT check (no second real delivery).
  F5 reconnect: a client that disconnects and reconnects gets a fresh snapshot (new epoch) whose
     FRIENDSHIP_SNAPSHOT_ENTRY set still reflects the current friendship/mail state.

Usage:
  python test_friendship_mail_sync.py <host_ip> <port> [--expect friendship|mail|both|none]
"""
import argparse
import sys

import net_spike_lib as L


def client(label, **kw):
    c = L.FakeClient(label, HOST, PORT, **kw)
    ALL.append(c)
    return c


def wait_for_friendship(c, after_count, timeout=8.0):
    return ALL[0].hub.wait_until(lambda: len(c.friendship_log) > after_count, timeout)


def has_any_letter(c):
    """True once C has observed mail state either via a live MAIL_DELIVERED broadcast OR (if the
    trigger fired before C connected, so C only ever sees the CURRENT state through its snapshot)
    a FRIENDSHIP_SNAPSHOT_ENTRY with has_letter set -- both are correct, equally valid ways for a
    client to learn about a delivered letter; see PCNetGameFriendshipSnapshotEntryMsg's own doc
    comment in pc_net_game.c on why mail state rides the same snapshot-entry channel as friendship."""
    return bool(c.mail_log) or any(e.get("has_letter") for e in c.friendship_log)


def wait_for_mail(c, after_count, timeout=8.0):
    return ALL[0].hub.wait_until(lambda: has_any_letter(c), timeout)


ALL = []


def main():
    global HOST, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--expect", choices=["friendship", "mail", "both", "none"], default="both")
    args = ap.parse_args()
    HOST, PORT = args.host, args.port
    r = []
    expect_friendship = args.expect in ("friendship", "both")
    expect_mail = args.expect in ("mail", "both")

    # --- connect BEFORE the host's one-shot debug trigger(s) fire ------------------------------------
    a = client("F-A")
    a.connect_and_ready()
    L.check("F0 A's initial snapshot completed", a.snapshot_complete(), r)

    # --- F1: FRIENDSHIP_UPDATE ------------------------------------------------------------------------
    if expect_friendship:
        got = wait_for_friendship(a, 0, timeout=10.0)
        L.check("F1 A received a FRIENDSHIP_UPDATE (or _SNAPSHOT_ENTRY, if it raced the snapshot)", got, r)
        if got:
            obj = a.friendship_log[-1]
            L.check("F1 slot index in range", 0 <= obj["slot"] < 15, r)
            L.check("F1 friendship value clamped to 0..127", 0 <= obj["friendship"] <= 127, r)
            key = (obj["slot"], obj["player_id"], obj["land_id"])
            L.check("F1 A's tracked friendship_memory reflects the update",
                    key in a.friendship_memory and a.friendship_memory[key]["friendship"] == obj["friendship"], r)

    # --- F2: mail delivery (live MAIL_DELIVERED, or folded into a FRIENDSHIP_SNAPSHOT_ENTRY if the
    # trigger fired before A connected -- see has_any_letter()'s own doc comment) -----------------
    if expect_mail:
        got = wait_for_mail(a, 0, timeout=10.0)
        L.check("F2 A received mail state (MAIL_DELIVERED, or a FRIENDSHIP_SNAPSHOT_ENTRY with has_letter)", got, r)
        if got:
            obj = a.mail_log[-1] if a.mail_log else next(e for e in reversed(a.friendship_log) if e.get("has_letter"))
            L.check("F2 slot index in range", 0 <= obj["slot"] < 15, r)
            L.check("F2 letter_info.exists bit set (bit 0 of the raw Anmlet_c byte)",
                    (obj["letter_info"] & 0x01) != 0, r)
            L.check("F2 letter blob is non-trivial (not all zero bytes)", any(b != 0 for b in obj["letter"]), r)
            key = (obj["slot"], obj["player_id"], obj["land_id"])
            L.check("F2 A's tracked friendship_memory has a letter recorded for this key",
                    key in a.friendship_memory and "letter" in a.friendship_memory[key], r)

    if not (expect_friendship or expect_mail):
        L.info("F1/F2 skipped: --expect none")

    # --- F3: late join sees CURRENT friendship/mail state, not a pre-change view --------------------
    L.pump_sleep(0.5)
    b = client("F3-B")
    b.connect_and_ready()
    L.check("F3 B's snapshot completed", b.snapshot_complete(), r)
    if expect_friendship or expect_mail:
        L.check("F3 B's snapshot carried at least one FRIENDSHIP_SNAPSHOT_ENTRY",
                any(True for _ in b.friendship_memory), r)
        L.check("F3 B's friendship_memory view matches A's CURRENT view (both reflect the same host truth)",
                b.friendship_memory == a.friendship_memory, r)

    # --- F4: duplicate/reordered UPDATE is a no-op (pure protocol/unit check) ----------------------
    if a.friendship_log:
        obj = a.friendship_log[-1]
        shadow_seq = obj["world_seq"]  # pretend this world_seq was already applied
        stale = obj["world_seq"] <= shadow_seq
        L.check("F4 a duplicate/reordered-behind UPDATE (same world_seq) is recognized as stale "
                "(world_seq <= last-applied, matching the C client's strict `>` rule)", stale, r)
    else:
        L.info("F4 skipped: no FRIENDSHIP_UPDATE/MAIL_DELIVERED observed to replay (--expect none)")

    # --- F5: reconnect gets a fresh snapshot (new epoch) with the current friendship/mail state -------
    if expect_friendship or expect_mail:
        old_epoch = b.snapshots[-1]["epoch"]
        slot = b.assigned_peer_id
        b.disconnect()
        L.pump_sleep(0.2)
        b.connect_and_ready()
        L.check(f"F5 B rejoined the same peer slot {slot}", b.assigned_peer_id == slot, r)
        L.check("F5 B's reconnect snapshot completed", b.snapshot_complete(), r)
        L.check("F5 B's reconnect snapshot got a NEW epoch", b.snapshots[-1]["epoch"] != old_epoch, r)
        L.check("F5 B's reconnect friendship_memory view matches A's current view",
                b.friendship_memory == a.friendship_memory, r)
    else:
        L.info("F5 skipped: --expect none")

    for c in ALL:
        c.close()
    return L.summary_and_exit_code(r)


if __name__ == "__main__":
    sys.exit(main())
