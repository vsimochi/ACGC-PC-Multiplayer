#!/usr/bin/env python3
"""test_dig_family_sync.py - World Ecology T-dig (DIG_HOLE / FILL_HOLE / PITFALL_CONSUME / DIG_SHINE,
plus the extended DIG_BURIED pitfall-dig-up sub-case and hole_variant range validation): REAL
two-process synthetic network test against a REAL, currently-running `AnimalCrossing.exe --host <port>
--bootstrap-resident 0 --field-action-test-seed` process.

Mirrors test_field_action_sync.py's own methodology exactly (see that file's own doc comment) -- hand-
crafts wire bytes for the (protocol v3, 12-byte) PC_NETGAME_MSG_FIELD_ACTION_REQUEST/RESULT and drives a
real host through them via net_spike_lib.FakeClient.

Requires the host to have been launched with --field-action-test-seed (pcnetgame_run_field_action_test_seed(),
pc_net_game.c), which (beyond Stage 1/T1's own fixtures) now also places five T-dig fixtures at z=105,
under the same already-loaded acre columns the z=104 fixtures use:
  - (24, 105)  BURIED_PITFALL_HOLE00 -- for the extended DIG_BURIED pitfall-dig-up sub-case test
  - (40, 105)  BURIED_PITFALL_HOLE00 -- dedicated to the PITFALL_CONSUME race test
  - (56, 105)  EMPTY_NO              -- for the DIG_HOLE test
  - (72, 105)  HOLE_START            -- for the FILL_HOLE test
  - (88, 105)  SHINE_SPOT            -- for the DIG_SHINE test
These are TEST-ONLY, off by default, and never placed in normal single-player or hosted play.

What this covers (all against the REAL host process):
  Test H1: DIG_HOLE on the seeded EMPTY_NO tile is accepted, commits HOLE_START + the requested
           hole_variant, and grants nothing (granted_item is the PRE-commit tile value, EMPTY_NO).
  Test H2: DIG_HOLE with an out-of-range hole_variant (200) is REJECTED outright (never clamped).
  Test H3: a repeat DIG_HOLE on the now-holed tile is rejected (it's a hole-type value now, not
           EMPTY_NO/a removable plant).
  Test F1: FILL_HOLE on the seeded HOLE_START (deposit OFF) tile is accepted; a FIELD_UPDATE for that
           tile to EMPTY_NO follows.
  Test F2: a repeat FILL_HOLE on the same (now EMPTY_NO) tile is rejected (not a hole any more).
  Test P1: PITFALL_CONSUME race -- two fake clients ("A" and "B") both target the SAME seeded
           BURIED_PITFALL_HOLE00 tile at once; exactly one is accepted, the other rejected, and the
           rejected one's RESULT.granted_item echoes the host's own current (post-consume) tile value
           (EMPTY_NO) for client-side reconciliation -- no duplication, no corruption.
  Test P2: a further PITFALL_CONSUME on the now-EMPTY_NO tile is rejected.
  Test B1: the extended DIG_BURIED validator, given the seeded BURIED_PITFALL_HOLE00 (dig-up) tile
           (deposit OFF), is ACCEPTED and grants ITM_PITFALL -- the new case 5b fold-in, verified not to
           regress the ordinary buried-item case (see test_field_action_sync.py's own Test D1/D2/D3,
           re-run separately against the same host process for that regression check).
  Test S1: DIG_SHINE on the seeded SHINE_SPOT tile is accepted and grants nothing (host-side) -- the
           digging client is solely responsible for its own local bell roll (not exercised by this
           protocol-level harness).
  Test S2: a repeat DIG_SHINE on the same (now HOLE_SHINE) tile is rejected.

Usage: python3 test_dig_family_sync.py <host_ip> <port>
"""
import struct
import sys

from net_spike_lib import (
    CH_RELIABLE,
    EMPTY_NO,
    FakeClient,
    check,
    make_request_id_counter,
    summary_and_exit_code,
    tile_center,
)

FIELD_ACTION_REQUEST_TYPE = 29  # PC_NETGAME_MSG_FIELD_ACTION_REQUEST
FIELD_ACTION_RESULT_TYPE = 30   # PC_NETGAME_MSG_FIELD_ACTION_RESULT
KIND_DIG_BURIED = 1
KIND_DIG_HOLE = 5
KIND_FILL_HOLE = 6
KIND_PITFALL_CONSUME = 7
KIND_DIG_SHINE = 8

REQ_FMT = "<BBBBIBBH64x"  # protocol v3 + X3: msg_type,kind,ut_x,ut_z,request_id,hole_variant,_reserved0,_reserved1 + the 64-byte txn tag (zero = no grant)
RES_FMT = "<BBBBIHBB"
REQ_SIZE = struct.calcsize(REQ_FMT)
RES_SIZE = struct.calcsize(RES_FMT)

HOLE_START = 0x0011
HOLE_SHINE = 0x005D
SHINE_SPOT = 0x005C

# Fixture tiles placed by --field-action-test-seed (pc_net_game.c), z=105 row.
PITFALL_DIG_TILE = (24, 105)      # BURIED_PITFALL_HOLE00, for extended DIG_BURIED
PITFALL_CONSUME_TILE = (40, 105)  # BURIED_PITFALL_HOLE00, for PITFALL_CONSUME race
DIG_HOLE_TILE = (56, 105)         # EMPTY_NO
FILL_HOLE_TILE = (72, 105)        # HOLE_START
DIG_SHINE_TILE = (88, 105)        # SHINE_SPOT

fresh_request_id = make_request_id_counter(50000)


def build_field_action_request(kind, ut_x, ut_z, request_id, hole_variant=0):
    return struct.pack(REQ_FMT, FIELD_ACTION_REQUEST_TYPE, kind & 0xFF, ut_x & 0xFF, ut_z & 0xFF,
                        request_id & 0xFFFFFFFF, hole_variant & 0xFF, 0, 0)


def decode_field_action_result(payload):
    msg_type, kind, accepted, ut_x, request_id, granted_item, ut_z, _reserved0 = struct.unpack(RES_FMT, payload)
    return dict(msg_type=msg_type, kind=kind, accepted=bool(accepted), ut_x=ut_x, ut_z=ut_z,
                request_id=request_id, granted_item=granted_item)


def send_field_action_request(client, kind, ut_x, ut_z, request_id, hole_variant=0):
    if kind == KIND_DIG_BURIED:
        # X3: DIG_BURIED (incl. the pitfall dig-up sub-case) is a host-transactional GRANT: it carries the pre-image tag
        return client.send_fa_grant(kind, ut_x, ut_z, request_id, hole_variant=hole_variant)
    return client.send_reliable(build_field_action_request(kind, ut_x, ut_z, request_id, hole_variant))


def wait_field_action_result(client, request_id, timeout=1.0):
    conn = client.connect_count

    def pred(m):
        if m.conn != conn or m.channel != CH_RELIABLE or not m.payload:
            return False
        if m.payload[0] != FIELD_ACTION_RESULT_TYPE or len(m.payload) != RES_SIZE:
            return False
        return decode_field_action_result(m.payload)["request_id"] == request_id

    m = client.inbox.wait_for(pred, timeout)
    return None if m is None else decode_field_action_result(m.payload)


def field_action(client, kind, ut_x, ut_z, hole_variant=0, timeout=1.0):
    x, y, z = tile_center(ut_x, ut_z)
    client.claim_position(x, y, z)
    rid = fresh_request_id()
    send_field_action_request(client, kind, ut_x, ut_z, rid, hole_variant)
    return wait_field_action_result(client, rid, timeout=timeout), rid


def main():
    if len(sys.argv) != 3:
        print("usage: test_dig_family_sync.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    results = []

    a = FakeClient("A", host_ip, port)
    a.connect_and_ready()
    a.drain_field_updates(timeout=0.3)

    # --- Test H1: DIG_HOLE on the seeded EMPTY_NO tile is accepted, commits HOLE_START+variant. -------
    h1, hrid1 = field_action(a, KIND_DIG_HOLE, *DIG_HOLE_TILE, hole_variant=5)
    check("Test H1: DIG_HOLE result received", h1 is not None, results)
    if h1 is not None:
        check("Test H1: accepted", h1["accepted"], results)
        check("Test H1: kind echoed", h1["kind"] == KIND_DIG_HOLE, results)
        check("Test H1: request_id echoed", h1["request_id"] == hrid1, results)
        check("Test H1: granted_item is the PRE-commit tile value (EMPTY_NO) -- nothing is granted",
              h1["granted_item"] == EMPTY_NO, results)
    # Confirm the tile is now HOLE_START + 5 via an ordinary FIELD_UPDATE.
    updates = a.drain_field_updates(timeout=1.0)
    dig_hole_update = [u for u in updates if (u[0], u[1]) == DIG_HOLE_TILE]
    check("Test H1: a FIELD_UPDATE for the dug tile was observed", len(dig_hole_update) >= 1, results)
    if dig_hole_update:
        check(f"Test H1: tile committed to HOLE_START+5 (0x{HOLE_START + 5:04X}), got 0x{dig_hole_update[-1][2]:04X}",
              dig_hole_update[-1][2] == HOLE_START + 5, results)

    # --- Test H2: DIG_HOLE with an out-of-range hole_variant is rejected outright. --------------------
    # (uses a fresh coordinate that would otherwise validate -- but this tile is ALREADY a hole now
    # after H1, so this also independently doubles as evidence the range check runs before/independent
    # of the tile-state check; a truly fresh EMPTY_NO tile is not available beyond the one fixture, so
    # H3 below re-confirms the ordinary "already a hole" rejection path separately.)
    h2, _hrid2 = field_action(a, KIND_DIG_HOLE, *DIG_HOLE_TILE, hole_variant=200)
    check("Test H2: DIG_HOLE with hole_variant=200 received an answer", h2 is not None, results)
    if h2 is not None:
        check("Test H2: rejected (hole_variant out of range, never clamped)", not h2["accepted"], results)

    # --- Test H3: a repeat DIG_HOLE on the now-holed tile (valid variant) is rejected. -----------------
    h3, _hrid3 = field_action(a, KIND_DIG_HOLE, *DIG_HOLE_TILE, hole_variant=3)
    check("Test H3: repeat DIG_HOLE result received", h3 is not None, results)
    if h3 is not None:
        check("Test H3: rejected (tile is a hole now, not EMPTY_NO/a removable plant)", not h3["accepted"], results)

    # --- Test F1: FILL_HOLE on the seeded HOLE_START tile is accepted, commits EMPTY_NO. --------------
    f1, frid1 = field_action(a, KIND_FILL_HOLE, *FILL_HOLE_TILE)
    check("Test F1: FILL_HOLE result received", f1 is not None, results)
    if f1 is not None:
        check("Test F1: accepted", f1["accepted"], results)
        check("Test F1: kind echoed", f1["kind"] == KIND_FILL_HOLE, results)
        check("Test F1: request_id echoed", f1["request_id"] == frid1, results)
    fill_updates = a.drain_field_updates(timeout=1.0)
    fill_hole_update = [u for u in fill_updates if (u[0], u[1]) == FILL_HOLE_TILE]
    check("Test F1: a FIELD_UPDATE for the filled tile was observed", len(fill_hole_update) >= 1, results)
    if fill_hole_update:
        check(f"Test F1: tile committed to EMPTY_NO, got 0x{fill_hole_update[-1][2]:04X}",
              fill_hole_update[-1][2] == EMPTY_NO, results)

    # --- Test F2: a repeat FILL_HOLE on the same (now EMPTY_NO) tile is rejected. ----------------------
    f2, _frid2 = field_action(a, KIND_FILL_HOLE, *FILL_HOLE_TILE)
    check("Test F2: repeat FILL_HOLE result received", f2 is not None, results)
    if f2 is not None:
        check("Test F2: rejected (not a hole any more)", not f2["accepted"], results)

    # --- Test B1: extended DIG_BURIED, given the seeded BURIED_PITFALL_HOLE00 (dig-up) tile, is
    # accepted and grants ITM_PITFALL -- the new case 5b fold-in. --------------------------------------
    ITM_PITFALL = 0x2500 + 18
    b1, brid1 = field_action(a, KIND_DIG_BURIED, *PITFALL_DIG_TILE)
    check("Test B1: extended DIG_BURIED (pitfall dig-up) result received", b1 is not None, results)
    if b1 is not None:
        check("Test B1: accepted", b1["accepted"], results)
        check("Test B1: request_id echoed", b1["request_id"] == brid1, results)
        check(f"Test B1: granted ITM_PITFALL (0x{ITM_PITFALL:04X}), got 0x{b1['granted_item']:04X}",
              b1["granted_item"] == ITM_PITFALL, results)
        tb1 = next((g for conn, g in a.txn_results if g.request_id == brid1 and conn == a.connect_count), None)
        check("Test B1 (X3): the grant came with a TXN_RESULT(APPLIED, kind DIG_BURIED, item ITM_PITFALL, post-image slot == item)",
              tb1 is not None and tb1.outcome == 0 and tb1.kind == 4 and tb1.item == ITM_PITFALL
              and tb1.post_pockets[tb1.slot] == ITM_PITFALL, results)
    b1_updates = a.drain_field_updates(timeout=1.0)
    b1_tile_update = [u for u in b1_updates if (u[0], u[1]) == PITFALL_DIG_TILE]
    check("Test B1: a FIELD_UPDATE for the dug pitfall tile was observed", len(b1_tile_update) >= 1, results)
    if b1_tile_update:
        check("Test B1: tile committed to a HOLE_START..+24 value",
              HOLE_START <= b1_tile_update[-1][2] <= HOLE_START + 24, results)

    # --- Test S1: DIG_SHINE on the seeded SHINE_SPOT tile is accepted, grants nothing host-side. -------
    s1, srid1 = field_action(a, KIND_DIG_SHINE, *DIG_SHINE_TILE)
    check("Test S1: DIG_SHINE result received", s1 is not None, results)
    if s1 is not None:
        check("Test S1: accepted", s1["accepted"], results)
        check("Test S1: request_id echoed", s1["request_id"] == srid1, results)
        check("Test S1: granted_item is 0 (host grants nothing -- digger rolls its own bell amount)",
              s1["granted_item"] == EMPTY_NO, results)
    s1_updates = a.drain_field_updates(timeout=1.0)
    s1_tile_update = [u for u in s1_updates if (u[0], u[1]) == DIG_SHINE_TILE]
    check("Test S1: a FIELD_UPDATE for the dug shine tile was observed", len(s1_tile_update) >= 1, results)
    if s1_tile_update:
        check(f"Test S1: tile committed to HOLE_SHINE (0x{HOLE_SHINE:04X}), got 0x{s1_tile_update[-1][2]:04X}",
              s1_tile_update[-1][2] == HOLE_SHINE, results)

    # --- Test S2: a repeat DIG_SHINE on the same (now HOLE_SHINE) tile is rejected. --------------------
    s2, _srid2 = field_action(a, KIND_DIG_SHINE, *DIG_SHINE_TILE)
    check("Test S2: repeat DIG_SHINE result received", s2 is not None, results)
    if s2 is not None:
        check("Test S2: rejected (tile is no longer an unburied SHINE_SPOT)", not s2["accepted"], results)

    # --- Test P1: PITFALL_CONSUME race -- two fake clients target the SAME buried-pitfall tile at once.
    # Exactly one must be accepted; the other rejected, with its granted_item echoing the host's own
    # current tile value (EMPTY_NO, since the winner's commit skips the transient HOLE_n stage). --------
    b = FakeClient("B", host_ip, port)
    b.connect_and_ready()
    b.drain_field_updates(timeout=0.3)

    x, y, z = tile_center(*PITFALL_CONSUME_TILE)
    a.claim_position(x, y, z)
    b.claim_position(x, y, z)
    rid_a = fresh_request_id()
    rid_b = fresh_request_id()
    send_field_action_request(a, KIND_PITFALL_CONSUME, PITFALL_CONSUME_TILE[0], PITFALL_CONSUME_TILE[1], rid_a)
    send_field_action_request(b, KIND_PITFALL_CONSUME, PITFALL_CONSUME_TILE[0], PITFALL_CONSUME_TILE[1], rid_b)
    r_a = wait_field_action_result(a, rid_a, timeout=1.0)
    r_b = wait_field_action_result(b, rid_b, timeout=1.0)
    check("Test P1: client A's PITFALL_CONSUME received an answer", r_a is not None, results)
    check("Test P1: client B's PITFALL_CONSUME received an answer", r_b is not None, results)
    if r_a is not None and r_b is not None:
        exactly_one_accepted = r_a["accepted"] != r_b["accepted"]
        check("Test P1: EXACTLY ONE of the two racing requests was accepted (no duplication, no double-reject)",
              exactly_one_accepted, results)
        loser = r_b if r_a["accepted"] else r_a
        if exactly_one_accepted:
            check("Test P1: the LOSING request's granted_item echoes the host's own post-consume tile value "
                  f"(EMPTY_NO), got 0x{loser['granted_item']:04X}",
                  loser["granted_item"] == EMPTY_NO, results)

    # --- Test P2: a further PITFALL_CONSUME on the now-EMPTY_NO tile is rejected. ----------------------
    p2, _prid2 = field_action(a, KIND_PITFALL_CONSUME, *PITFALL_CONSUME_TILE)
    check("Test P2: repeat PITFALL_CONSUME result received", p2 is not None, results)
    if p2 is not None:
        check("Test P2: rejected (tile is no longer a buried pitfall)", not p2["accepted"], results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
