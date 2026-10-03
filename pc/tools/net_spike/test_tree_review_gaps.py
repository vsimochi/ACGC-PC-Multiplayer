#!/usr/bin/env python3
"""test_tree_review_gaps.py - World Ecology T1 (host-authoritative TREE_SHAKE/TREE_CHOP) review-gap
regression: the independent review that gave T1 a CONDITIONAL-GO required, besides the
s_host_tree_cut_count reset fix (pc_net_game.c, alongside s_host_money_rock's own reset), genuine
live two-process (real host + fake client) evidence for the tree race/ordering properties its design
brief calls out. None existed before this script.

Requires the host to have been launched with --field-action-test-seed AND --bootstrap-resident 0
(the non-interactive boot path -- see pc_main.c's g_pc_bootstrap_resident, and pcnetgame_run_
field_action_test_seed()'s own doc for the three tree fixtures this script's own follow-up edit added
to that seed):
  AnimalCrossing.exe --host <port> --bootstrap-resident 0 --field-action-test-seed --verbose
which places, among the pre-existing dig/rock fixtures:
  - TREE_APPLE_FRUIT (a 3-hit fruit tree) at tile (56, 104)  -- Test 1 (shake race) + Test 4 (reach)
  - TREE_APPLE_FRUIT (a SECOND, independent instance)  at tile (72, 104) -- Test 2 (alternating chop)
  - TREE_BEES (a 3-hit tree with an active beehive) at tile (88, 104) -- Test 3 (bee-birth ordering)

Methodology note on Test 1/2 (host+client race/alternation): pc_net_game_host_local_tree_shake()/
pc_net_game_host_local_tree_chop() exist (pc_net_game.c) but, unlike MONEY_ROCK_HIT, T1 shipped with
NO --force-tree-shake/--force-tree-chop test-only CLI hook to actually fire them from this harness,
and adding one is out of scope for this review-fix pass (narrowly limited to the cut-count reset).
Both a host-local hit and a peer's request funnel into the EXACT SAME validate/commit functions
(pcnetgame_validate_tree_shake_core/pcnetgame_host_commit_tree_shake, ditto for chop) and the exact
same shared s_host_tree_cut_count bookkeeping this review is about -- the race/ordering property under
test (single-writer correctness under concurrent access to that shared state) is identical regardless
of whether the second concurrent actor is host-local or another peer. Tests 1/2 therefore use TWO fake
peer clients (A, B) racing/alternating each other instead of literally "host + client" -- this is a
deliberate, documented substitution, not an oversight.

What this covers (all against a REAL host process):
  Test 1 (shake race): A and B fire TREE_SHAKE at the SAME fruit tree back-to-back (no wait in
    between -- both RDATA packets hit the wire microseconds apart, well within the same host poll
    tick). Exactly one must be accepted (with a real drop + conversion), the other rejected.
  Test 2 (alternating chop): A, B, A alternate TREE_CHOP hits on a fresh 3-hit fruit tree. The tree
    must fall (produce a stump) on exactly the 3rd COMBINED hit (not the 3rd hit by either actor
    individually -- neither A nor B ever lands more than 2 chops here), fruit drops once (hit 1 only,
    no duplication), and only the 3rd hit's result is a stump.
  Test 3 (bee-birth queued behind a felling chop): 3 chops fell the TREE_BEES fixture into a stump;
    a TREE_SHAKE (bee-birth) sent immediately after (no wait) must be REJECTED and must not disturb
    the already-committed stump.
  Test 4 (reach check sanity): a TREE_SHAKE from a position one tile-width (40 units) beyond the
    80-unit tree-reach threshold is rejected; Test 1's own successful hit is the in-range control.

Test 5 (cut-count reset across a town-change/session-reset) is NOT exercised live here -- see this
script's own final report: there is no protocol message that forces a same-process town change, and
the GUI-keystroke harness (game_input.py) needed to walk a real host through a gate is documented as
broken in this sandbox. That property is verified at SOURCE-ONLY tier instead (both memset call sites
read before/after the fix, confirmed to fire in the identical branch/condition as the already-proven
s_host_money_rock reset).

Usage: python3 test_tree_review_gaps.py <host_ip> <port>
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
KIND_TREE_SHAKE = 3             # PC_NETGAME_FIELD_ACTION_KIND_TREE_SHAKE
KIND_TREE_CHOP = 4              # PC_NETGAME_FIELD_ACTION_KIND_TREE_CHOP

# PCNetGameFieldActionRequestMsg is now 12 bytes (protocol v3 added hole_variant/_reserved0/_reserved1
# for the T-dig pitfall sub-case, concurrent with this T1 review work) -- msg_type, kind, ut_x, ut_z,
# request_id, hole_variant, _reserved0, _reserved1. hole_variant is always 0 for TREE_SHAKE/TREE_CHOP.
REQ_FMT = "<BBBBIBBH64x"  # X3: 76 B = the 12-byte v3 header + the 64-byte txn tag (all zero = no grant)
RES_FMT = "<BBBBIHBB"
REQ_SIZE = struct.calcsize(REQ_FMT)
RES_SIZE = struct.calcsize(RES_FMT)

# Fixture tiles placed by --field-action-test-seed's tree follow-up (pc_net_game.c,
# pcnetgame_run_field_action_test_seed()).
SHAKE_TILE = (56, 104)   # TREE_APPLE_FRUIT
CHOP_TILE = (72, 104)    # TREE_APPLE_FRUIT (independent instance)
BEE_TILE = (88, 104)     # TREE_BEES

# IS_ITEM_TREE_STUMP() (m_name_table.h): four disjoint id ranges, one per tree family.
STUMP_RANGES = ((1, 4), (112, 115), (116, 119), (123, 126))


def is_tree_stump(item):
    return any(lo <= item <= hi for lo, hi in STUMP_RANGES)


fresh_request_id = make_request_id_counter(11000)


def build_field_action_request(kind, ut_x, ut_z, request_id):
    return struct.pack(REQ_FMT, FIELD_ACTION_REQUEST_TYPE, kind & 0xFF, ut_x & 0xFF, ut_z & 0xFF,
                        request_id & 0xFFFFFFFF, 0, 0, 0)


def decode_field_action_result(payload):
    msg_type, kind, accepted, ut_x, request_id, granted_item, ut_z, _reserved0 = struct.unpack(RES_FMT, payload)
    return dict(msg_type=msg_type, kind=kind, accepted=bool(accepted), ut_x=ut_x, ut_z=ut_z,
                request_id=request_id, granted_item=granted_item)


def send_field_action_request(client, kind, ut_x, ut_z, request_id):
    return client.send_reliable(build_field_action_request(kind, ut_x, ut_z, request_id))


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


def field_action(client, kind, ut_x, ut_z, timeout=1.0):
    x, y, z = tile_center(ut_x, ut_z)
    client.claim_position(x, y, z)
    rid = fresh_request_id()
    send_field_action_request(client, kind, ut_x, ut_z, rid)
    return wait_field_action_result(client, rid, timeout=timeout), rid


def main():
    if len(sys.argv) != 3:
        print("usage: test_tree_review_gaps.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    results = []

    a = FakeClient("A", host_ip, port)
    b = FakeClient("B", host_ip, port)
    a.connect_and_ready()
    b.connect_and_ready()
    a.drain_field_updates(timeout=0.3)
    b.drain_field_updates(timeout=0.3)

    # ================================================================================================
    # Test 4a (reach check, out-of-range half): one tile-width (40 units) beyond the 80-unit tree
    # reach threshold. Done FIRST, on the (still-untouched) SHAKE_TILE, and must be a no-op rejection
    # (no mutation) so it cannot interfere with Test 1's own use of this same tile.
    # ================================================================================================
    cx, cy, cz = tile_center(*SHAKE_TILE)
    far_x = cx + 120.0  # 80 (threshold) + 40 (one tile width) beyond the tile center
    a.claim_position(far_x, cy, cz)
    rid_far = fresh_request_id()
    send_field_action_request(a, KIND_TREE_SHAKE, SHAKE_TILE[0], SHAKE_TILE[1], rid_far)
    r_far = wait_field_action_result(a, rid_far, timeout=1.0)
    check("Test 4a: TREE_SHAKE from a position 120 units away (one tile-width beyond the 80-unit "
          "reach threshold) received an answer", r_far is not None, results)
    if r_far is not None:
        check("Test 4a: rejected -- out of tree-reach range", not r_far["accepted"], results)

    # ================================================================================================
    # Test 1: shake race. A and B fire TREE_SHAKE at SHAKE_TILE back-to-back (no wait in between) --
    # both claim an IN-RANGE position first (Test 4's own in-range control, per this script's doc).
    # ================================================================================================
    sx, sy, sz = tile_center(*SHAKE_TILE)
    a.claim_position(sx, sy, sz)
    b.claim_position(sx, sy, sz)
    rid_a1 = fresh_request_id()
    rid_b1 = fresh_request_id()
    send_field_action_request(a, KIND_TREE_SHAKE, SHAKE_TILE[0], SHAKE_TILE[1], rid_a1)
    send_field_action_request(b, KIND_TREE_SHAKE, SHAKE_TILE[0], SHAKE_TILE[1], rid_b1)
    r_a1 = wait_field_action_result(a, rid_a1, timeout=1.5)
    r_b1 = wait_field_action_result(b, rid_b1, timeout=1.5)
    check("Test 1: A's TREE_SHAKE received an answer", r_a1 is not None, results)
    check("Test 1: B's TREE_SHAKE received an answer", r_b1 is not None, results)
    if r_a1 is not None and r_b1 is not None:
        accepts = [r for r in (r_a1, r_b1) if r["accepted"]]
        rejects = [r for r in (r_a1, r_b1) if not r["accepted"]]
        check(f"Test 1: exactly ONE of A/B's simultaneous TREE_SHAKE on the same tree was accepted "
              f"(accepted={len(accepts)}, rejected={len(rejects)}) -- no double-drop/double-conversion",
              len(accepts) == 1 and len(rejects) == 1, results)
        if accepts:
            check("Test 1: the accepted shake's granted_item is a real conversion (not EMPTY_NO, i.e. a "
                  "genuine commit happened)", accepts[0]["granted_item"] != EMPTY_NO, results)
        print(f"    Test 1: A accepted={r_a1['accepted']} granted=0x{r_a1['granted_item']:04X}; "
              f"B accepted={r_b1['accepted']} granted=0x{r_b1['granted_item']:04X}")
        # The loser must find NO matching row any more -- prove it by re-sending its own SAME shake
        # again (fresh request id) and confirming it is STILL rejected (the tile stayed converted; a
        # buggy double-write would have "re-armed" a fruit row for a second win here).
        loser = a if not r_a1["accepted"] else b
        r_retry, _ = field_action(loser, KIND_TREE_SHAKE, *SHAKE_TILE, timeout=1.0)
        check("Test 1: the loser's tree-shake retry on the now-converted tile is ALSO rejected (the "
              "winner's conversion stuck; no re-arming)", r_retry is not None and not r_retry["accepted"],
              results)

    # ================================================================================================
    # Test 2: alternating chop. A, B, A alternate TREE_CHOP hits on CHOP_TILE (independent fresh
    # TREE_APPLE_FRUIT, 3 hits to fell). Must fall on the 3rd COMBINED hit, not per-actor.
    # ================================================================================================
    r_c1, _ = field_action(a, KIND_TREE_CHOP, *CHOP_TILE)
    check("Test 2 hit1 (A): TREE_CHOP received an answer", r_c1 is not None, results)
    if r_c1 is not None:
        check("Test 2 hit1 (A): accepted", r_c1["accepted"], results)
        check("Test 2 hit1 (A): NOT yet a stump (1 of 3 hits)", not is_tree_stump(r_c1["granted_item"]),
              results)
        check("Test 2 hit1 (A): fruit-drop conversion happened (granted_item != EMPTY_NO, the row's "
              "dst_tree_item)", r_c1["granted_item"] != EMPTY_NO, results)

    r_c2, _ = field_action(b, KIND_TREE_CHOP, *CHOP_TILE)
    check("Test 2 hit2 (B): TREE_CHOP received an answer", r_c2 is not None, results)
    if r_c2 is not None:
        check("Test 2 hit2 (B): accepted (B's FIRST hit on this tile -- proves the combined, not "
              "per-actor, hit count is what's tracked)", r_c2["accepted"], results)
        check("Test 2 hit2 (B): NOT yet a stump (2 of 3 combined hits)",
              not is_tree_stump(r_c2["granted_item"]), results)
        if r_c1 is not None and r_c1["accepted"]:
            check("Test 2 hit2 (B): no duplicate fruit-drop conversion -- granted_item unchanged from "
                  "hit1's (already-converted, no-longer-fruit) tile value",
                  r_c2["granted_item"] == r_c1["granted_item"], results)

    r_c3, _ = field_action(a, KIND_TREE_CHOP, *CHOP_TILE)
    check("Test 2 hit3 (A): TREE_CHOP received an answer", r_c3 is not None, results)
    if r_c3 is not None:
        check("Test 2 hit3 (A): accepted (A's SECOND hit on this tile, but the 3rd COMBINED hit)",
              r_c3["accepted"], results)
        check("Test 2 hit3 (A): the tree fell -- granted_item IS a stump on exactly the 3rd combined "
              f"hit (0x{r_c3['granted_item']:04X})", is_tree_stump(r_c3["granted_item"]), results)

    # A 4th hit on the now-stumped tile must be rejected -- no further mutation possible, and proves
    # only ONE stump write ever happened (a duplicate stump-write bug could still "succeed" again).
    r_c4, _ = field_action(b, KIND_TREE_CHOP, *CHOP_TILE)
    check("Test 2 hit4 (B, post-fell): TREE_CHOP on the now-stumped tile received an answer",
          r_c4 is not None, results)
    if r_c4 is not None:
        check("Test 2 hit4 (B, post-fell): rejected -- a stump is not choppable (only one stump write "
              "ever happened)", not r_c4["accepted"], results)

    # ================================================================================================
    # Test 3: bee-birth request queued immediately behind a felling chop on BEE_TILE (TREE_BEES).
    # Hits 1-2 wait normally; hit 3 (felling) and the bee-birth TREE_SHAKE are fired back-to-back with
    # NO wait, so both are in flight to the host within the same/adjacent poll tick.
    # ================================================================================================
    r_b1c, _ = field_action(a, KIND_TREE_CHOP, *BEE_TILE)
    check("Test 3 hit1: TREE_CHOP on the bee tree accepted", r_b1c is not None and r_b1c["accepted"], results)
    r_b2c, _ = field_action(a, KIND_TREE_CHOP, *BEE_TILE)
    check("Test 3 hit2: TREE_CHOP on the bee tree accepted", r_b2c is not None and r_b2c["accepted"], results)

    bx, by, bz = tile_center(*BEE_TILE)
    a.claim_position(bx, by, bz)
    rid_fell = fresh_request_id()
    send_field_action_request(a, KIND_TREE_CHOP, BEE_TILE[0], BEE_TILE[1], rid_fell)
    rid_bee = fresh_request_id()
    send_field_action_request(a, KIND_TREE_SHAKE, BEE_TILE[0], BEE_TILE[1], rid_bee)  # no wait -- same-tick

    r_fell = wait_field_action_result(a, rid_fell, timeout=1.5)
    r_bee = wait_field_action_result(a, rid_bee, timeout=1.5)
    check("Test 3 hit3 (felling): TREE_CHOP received an answer", r_fell is not None, results)
    if r_fell is not None:
        check("Test 3 hit3 (felling): accepted and produced a stump (3rd combined hit)",
              r_fell["accepted"] and is_tree_stump(r_fell["granted_item"]), results)
    check("Test 3: the queued-behind bee-birth TREE_SHAKE received an answer", r_bee is not None, results)
    if r_bee is not None:
        check("Test 3: the bee-birth TREE_SHAKE sent immediately after the felling chop is REJECTED "
              "(the stump is already committed -- not overwritten)", not r_bee["accepted"], results)

    # Final integrity check: one more chop on BEE_TILE must ALSO be rejected -- the stump from hit3 is
    # still there, untouched by the rejected bee-birth shake (a bug would have overwritten it back to
    # TREE_BEES, making this chop succeed instead of being rejected as "not choppable").
    r_final, _ = field_action(a, KIND_TREE_CHOP, *BEE_TILE)
    check("Test 3 post-check: a further chop on BEE_TILE is still rejected (the stump was never "
          "overwritten by the rejected bee-birth shake)", r_final is not None and not r_final["accepted"],
          results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
