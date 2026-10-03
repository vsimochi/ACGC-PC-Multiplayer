#!/usr/bin/env python3
"""test_money_rock_review_gaps.py - FOCUSED FAKE-CLIENT regression test closing two specific evidence
gaps a completed independent review of the World Ecology Stage 1 money-rock Bugs 1-4 fixes flagged as
not yet exercised by test_field_action_sync.py (which this script deliberately does not duplicate):

  Test R1 (Bug 2): pcnetgame_find_money_rock_drop_tile()'s reservation-respecting search. Client A hits
    the money rock (24,104) once, dropping a reward bag on the FIRST candidate neighbor tile,
    pcnetgame_find_money_rock_drop_tile()'s own dxs/dzs[0] -- (23,103). A then opens a PENDING,
    unconfirmed PICKUP reservation on that exact tile (never sending CONFIRM, so the field write never
    happens and the RESERVATION stays held -- pcnetgame_host_tile_reserved_by() does not care which
    peer/kind of pending interaction holds a tile, only that one does, so a single fake client can hold
    this reservation on itself just as well as a second peer's in-flight request could). A then hits
    the rock again: the second reward must land on the SECOND candidate, (23,105), never overwrite the
    still-reserved (23,103) -- this is the exact bug the review flagged (a drop onto a tile someone
    else's pending commit is about to write, which would either corrupt that peer's own commit
    re-validation or destroy the freshly dropped bag).
  Test R2 (expiry/revert-guard): hits the rock, waits past the window's expire_target_frames, then
    confirms a FURTHER hit is correctly REJECTED -- a used-up money-rock window reverts to an ORDINARY
    (non-money) rock on expiry (orig_item - 7, verbatim matching vanilla's own bIT_actor_ten_coin_move()
    revert), so IS_ITEM_STONE_TC is false afterwards and MONEY_ROCK_HIT must reject it. The buggy
    alternative this rules out: the tile left stuck on MONEY_FLOWER_SEED (which the host would still
    treat as hittable) or a stale bookkeeping slot silently reopening -- also incidentally the "rock
    lifecycle: reset" case from the Part 4 test matrix.

Requires a freshly-launched host (a rock hit by an EARLIER test script leaves hit_count non-zero,
which R1/R2 do not depend on but would make less legible): `AnimalCrossing.exe --host <port>
--bootstrap-resident 0 --field-action-test-seed --verbose`.

Usage: python3 test_money_rock_review_gaps.py <host_ip> <port>
"""
import struct
import sys
import time

from net_spike_lib import (
    CH_RELIABLE,
    CONFIRM_KIND_PICKUP,
    CONFIRM_OUTCOME_ABORT,
    EMPTY_NO,
    FakeClient,
    check,
    make_request_id_counter,
    pump_sleep,
    summary_and_exit_code,
    tile_center,
)

FIELD_ACTION_REQUEST_TYPE = 29
FIELD_ACTION_RESULT_TYPE = 30
KIND_MONEY_ROCK_HIT = 2

#   FIELD_ACTION_REQUEST v3 (12 bytes): msg_type, kind, ut_x, ut_z, uint32 request_id, uint8
#   hole_variant, uint8 _reserved0, uint16 _reserved1 -- see PCNetGameFieldActionRequestMsg's own doc
#   (pc_net_game.c) / test_field_action_wire.py. hole_variant is pinned to 0 for MONEY_ROCK_HIT.
REQ_FMT = "<BBBBIBBH64x"  # X3: 76 B = the 12-byte v3 header + the 64-byte txn tag (all zero = no grant)
RES_FMT = "<BBBBIHBB"
RES_SIZE = struct.calcsize(RES_FMT)

ROCK_TILE = (24, 104)
# pcnetgame_find_money_rock_drop_tile()'s own dxs/dzs order: (-1,-1) first, then (-1,1), ...
CANDIDATE1 = (ROCK_TILE[0] - 1, ROCK_TILE[1] - 1)  # (23,103) -- what R1 reserves
CANDIDATE2 = (ROCK_TILE[0] - 1, ROCK_TILE[1] + 1)  # (23,105) -- expected landing spot instead

ITM_MONEY_100 = 0x2103

fresh_request_id = make_request_id_counter(9000)


def build_field_action_request(kind, ut_x, ut_z, request_id):
    return struct.pack(REQ_FMT, FIELD_ACTION_REQUEST_TYPE, kind & 0xFF, ut_x & 0xFF, ut_z & 0xFF,
                        request_id & 0xFFFFFFFF, 0, 0, 0)


def decode_field_action_result(payload):
    msg_type, kind, accepted, ut_x, request_id, granted_item, ut_z, _reserved0 = struct.unpack(RES_FMT, payload)
    return dict(msg_type=msg_type, kind=kind, accepted=bool(accepted), ut_x=ut_x, ut_z=ut_z,
                request_id=request_id, granted_item=granted_item)


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


def hit_rock(client, timeout=1.0):
    x, y, z = tile_center(*ROCK_TILE)
    client.claim_position(x, y, z)
    rid = fresh_request_id()
    client.send_reliable(build_field_action_request(KIND_MONEY_ROCK_HIT, ROCK_TILE[0], ROCK_TILE[1], rid))
    return wait_field_action_result(client, rid, timeout=timeout)


def pickup_at(client, ut_x, ut_z, auto_confirm=True):
    x, y, z = tile_center(ut_x, ut_z)
    client.claim_position(x, y, z)
    rid = fresh_request_id()
    client.send_pickup_request(ut_x, ut_z, rid, auto_confirm=auto_confirm)
    return client.recv_pickup_result(timeout=1.0, expect_request_id=rid)


def main():
    if len(sys.argv) != 3:
        print("usage: test_money_rock_review_gaps.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    results = []

    a = FakeClient("A", host_ip, port)
    a.connect_and_ready()
    a.drain_field_updates(timeout=0.3)

    # --- Test R1 step 1: A hits the (fresh) rock once. The reward must land on CANDIDATE1, the FIRST
    # neighbor pcnetgame_find_money_rock_drop_tile() tries, since nothing is reserved yet. -------------
    m1 = hit_rock(a)
    check("Test R1 step1: first MONEY_ROCK_HIT accepted", m1 is not None and m1["accepted"], results)
    res_c1_first = pickup_at(a, *CANDIDATE1, auto_confirm=False)
    check("Test R1 step1: the first reward landed on CANDIDATE1 (23,103) as expected on an unreserved rock",
          res_c1_first is not None and res_c1_first[0], results)

    if res_c1_first is not None and res_c1_first[0]:
        pickup_rid = res_c1_first[3]
        # A's own pickup of CANDIDATE1 is left PENDING (unconfirmed) on purpose -- this holds a real
        # RESERVATION on CANDIDATE1 exactly like a second peer's in-flight pickup/drop would, without
        # needing a second client to also pass every ordinary-item drop precondition (terrain legality
        # etc.) that a DROP_REQUEST would add unrelated risk of failing for. pcnetgame_host_tile_
        # reserved_by() does not care WHICH kind of pending interaction holds a tile, only that one does.

        # --- Test R1 step 2: A hits the rock AGAIN while CANDIDATE1 is still reserved (by A's own
        # pending pickup). The reward must skip CANDIDATE1 and land on CANDIDATE2 instead. -------------
        m2 = hit_rock(a)
        check("Test R1 step2: second MONEY_ROCK_HIT accepted while CANDIDATE1 is reserved",
              m2 is not None and m2["accepted"], results)

        res_c2 = pickup_at(a, *CANDIDATE2)
        check("Test R1 step2: the second reward landed on CANDIDATE2 (23,105), skipping the reserved tile",
              res_c2 is not None and res_c2[0], results)

        # Release A's own pending reservation on CANDIDATE1 (ABORT) and confirm the bag is STILL there,
        # untouched by the second hit's drop search -- the real proof nothing clobbered it.
        a.confirm(CONFIRM_KIND_PICKUP, pickup_rid, outcome=CONFIRM_OUTCOME_ABORT)  # release A's own reservation
        res_c1_after = pickup_at(a, *CANDIDATE1)
        check("Test R1 step2: CANDIDATE1's original (first-hit) bag is untouched after the reservation is released",
              res_c1_after is not None and res_c1_after[0] and res_c1_after[4] == ITM_MONEY_100, results)
    else:
        print("SKIP: Test R1 step2 -- step1's own reward pickup did not land as expected; skipping the "
              "reservation-respecting search check")

    # --- Test R2: expiry + revert-guard on a REAL running host. A used-up money-rock window, once
    # expired, reverts to an ORDINARY (non-money) rock -- verbatim matching vanilla's own
    # bIT_actor_ten_coin_move() revert (orig_item - 7), see pcnetgame_host_check_field_action_money_rock()'s
    # doc. So the REAL, observable proof the revert-guard worked is that a FURTHER MONEY_ROCK_HIT on the
    # same tile is correctly REJECTED afterwards (IS_ITEM_STONE_TC is false for an ordinary rock) --
    # the wrong/buggy outcome would be either an ACCEPT (the tile stuck on MONEY_FLOWER_SEED, which the
    # host would treat as still a valid target) or a stale bookkeeping slot silently reopening. ---------
    m3 = hit_rock(a)
    check("Test R2 setup: MONEY_ROCK_HIT accepted", m3 is not None and m3["accepted"], results)
    print("Test R2: waiting ~10s for the money-rock window to expire and revert to an ordinary rock...")
    pump_sleep(10.0)  # keeps A's transport alive (heartbeats/ACKs) during the wait -- a bare
                       # time.sleep() here would let the host idle-timeout and drop the connection
    m4 = hit_rock(a)
    check("Test R2: a further hit AFTER expiry is correctly REJECTED (the tile is now an ordinary rock, "
          "not stuck on MONEY_FLOWER_SEED and not silently reopening a stale window)",
          m4 is not None and not m4["accepted"], results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
