#!/usr/bin/env python3
"""test_field_action_sync.py - World Ecology Stage 1 (DIG_BURIED / MONEY_ROCK_HIT): REAL two-process
synthetic network test against a REAL, currently-running `AnimalCrossing.exe --host <port>
--field-action-test-seed` process.

Mirrors test_pickup_sync.py's own methodology exactly (see that file's own doc comment): hand-crafts
wire bytes for PC_NETGAME_MSG_FIELD_ACTION_REQUEST/RESULT (pc_net_game.c) and drives a real host
through them via net_spike_lib.FakeClient. FIELD_ACTION_REQUEST/RESULT are not yet registered in
net_spike_lib's message-spec tables, so this script builds/decodes them itself (see build_field_
action_request()/decode_field_action_result() below) and sends/waits for them via FakeClient's
lower-level send_reliable()/inbox primitives -- the same primitives net_spike_lib's own higher-level
helpers are built on.

Requires the host to have been launched with --field-action-test-seed (see pc_net_game.c's
pcnetgame_run_field_action_test_seed()), which places exactly two known fixtures:
  - a buried ITM_FOOD_APPLE at tile (40, 104)
  - a MONEY_ROCK_A at tile (24, 104)
These are TEST-ONLY, off by default, and never placed in normal single-player or hosted play.

What this covers (all against the REAL host process -- genuine pcfa_get_tile/pcfa_set_tile reads and
writes, genuine host-side validation, genuine deterministic reward bookkeeping):
  Test D1: DIG_BURIED on the seeded buried tile is accepted, grants the correct item, and consumes it.
  Test D2: a SECOND DIG_BURIED request on the SAME (now-dug) tile is rejected -- no duplication, even
           back-to-back before the first RESULT's implications are otherwise visible.
  Test D3: DIG_BURIED on an ordinary empty tile (no deposit bit) is rejected.
  Test M1: MONEY_ROCK_HIT on the seeded money-rock tile is accepted, opens a window.
  Test M2: the same request_id retried (host dedup) replays the SAME accepted outcome rather than
           double-counting the hit -- verified indirectly via the reward escalation in M3 staying
           correct (a double-count would skip a reward tier).
  Test M3: repeated hits (fresh request_id each time) escalate the reward exactly as
           bIT_actor_ten_coin_entryR() would (100,100,100,1000,1000,1000,10000,...) -- read back via
           an ordinary PICKUP_REQUEST at the deterministic drop tile after each hit.
  Test M4: the dropped money bag IS pickupable over the network (the pcnetgame_is_money_bag_item()
           exclusion is correctly lifted for exactly this tile) -- covered by M3 itself succeeding.
  Test M5: a pickup at some OTHER, unrelated coordinate never dispenses a bag (no blanket lift).

Real two-process evidence note: this script is a scripted stand-in for a CLIENT sending raw protocol
bytes (exactly like test_pickup_sync.py already establishes as valid methodology in this codebase's
own docs) -- it validates the HOST's real C code paths byte-for-byte. It does NOT exercise the
CLIENT-side C code that applies an accepted DIG_BURIED result into a real pocket
(pcnetgame_handle_client_field_action_result()) or credits a picked-up money bag to a real wallet
(pcnetgame_handle_client_pickup_result()'s PC_ENHANCEMENTS branch) -- those run only inside a second
real AnimalCrossing.exe client process driven by real player input, which this harness cannot yet
simulate headlessly (see this milestone's final report for the exact limitation).

Usage: python3 test_field_action_sync.py <host_ip> <port>
"""
import struct
import sys
import time

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
KIND_DIG_BURIED = 1             # PC_NETGAME_FIELD_ACTION_KIND_DIG_BURIED
KIND_MONEY_ROCK_HIT = 2         # PC_NETGAME_FIELD_ACTION_KIND_MONEY_ROCK_HIT

REQ_FMT = "<BBBBIBBH"  # protocol v3: + hole_variant, _reserved0, _reserved1 (was "<BBBBI", 8 bytes)
RES_FMT = "<BBBBIHBB"
REQ_SIZE = struct.calcsize(REQ_FMT)
RES_SIZE = struct.calcsize(RES_FMT)

# Fixture tiles placed by --field-action-test-seed (pc_net_game.c).
DIG_TILE = (40, 104)
ROCK_TILE = (24, 104)
# Deterministic first drop-neighbor candidate (pcnetgame_find_money_rock_drop_tile()'s own dxs/dzs
# order tries (-1,-1) first) -- expected empty on a fresh save.
ROCK_DROP_TILE = (ROCK_TILE[0] - 1, ROCK_TILE[1] - 1)

# m_name_table.h: ITM_MONEY_1000 = ITM_MONEY_START (0x2100), then +1 per step -- NOT numerically
# sequential by amount (ITM_MONEY_100 is actually LAST in this chain, 0x2103).
ITM_MONEY_1000 = 0x2100
ITM_MONEY_10000 = 0x2101
ITM_MONEY_30000 = 0x2102
ITM_MONEY_100 = 0x2103

fresh_request_id = make_request_id_counter(5000)


def build_field_action_request(kind, ut_x, ut_z, request_id, hole_variant=0):
    return struct.pack(REQ_FMT, FIELD_ACTION_REQUEST_TYPE, kind & 0xFF, ut_x & 0xFF, ut_z & 0xFF,
                        request_id & 0xFFFFFFFF, hole_variant & 0xFF, 0, 0)


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
        print("usage: test_field_action_sync.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    results = []

    a = FakeClient("A", host_ip, port)
    a.connect_and_ready()
    a.drain_field_updates(timeout=0.3)

    # --- Test D1: DIG_BURIED on the seeded buried tile is accepted and grants the right item. ------
    r1, rid1 = field_action(a, KIND_DIG_BURIED, *DIG_TILE)
    check("Test D1: DIG_BURIED result received", r1 is not None, results)
    if r1 is not None:
        check("Test D1: accepted", r1["accepted"], results)
        check("Test D1: kind echoed", r1["kind"] == KIND_DIG_BURIED, results)
        check("Test D1: request_id echoed", r1["request_id"] == rid1, results)
        check("Test D1: granted item is the seeded ITM_FOOD_APPLE (or a resolved equivalent, never EMPTY_NO)",
              r1["accepted"] and r1["granted_item"] != EMPTY_NO, results)

    # --- Test D2: a second DIG_BURIED on the SAME (now-dug) tile is rejected -- no duplication. -----
    r2, _rid2 = field_action(a, KIND_DIG_BURIED, *DIG_TILE)
    check("Test D2: second DIG_BURIED on the same tile received an answer", r2 is not None, results)
    if r2 is not None:
        check("Test D2: rejected (deposit already cleared -- no item duplication)", not r2["accepted"], results)

    # --- Test D2b: two RAPID DIG_BURIED requests for the same tile (simulated double-request), only
    # one may ever be granted. Uses a fresh, still-buried-looking coordinate is not available (only one
    # buried fixture exists), so this reuses the already-dug tile: both must now be rejected, which is
    # itself the no-duplication evidence for a tile with no remaining deposit. ------------------------
    rid_x = fresh_request_id()
    rid_y = fresh_request_id()
    x, y, z = tile_center(*DIG_TILE)
    a.claim_position(x, y, z)
    send_field_action_request(a, KIND_DIG_BURIED, DIG_TILE[0], DIG_TILE[1], rid_x)
    send_field_action_request(a, KIND_DIG_BURIED, DIG_TILE[0], DIG_TILE[1], rid_y)
    r_x = wait_field_action_result(a, rid_x, timeout=1.0)
    r_y = wait_field_action_result(a, rid_y, timeout=1.0)
    check("Test D2b: rapid double-request -- first answered", r_x is not None, results)
    check("Test D2b: rapid double-request -- second answered", r_y is not None, results)
    if r_x is not None and r_y is not None:
        check("Test D2b: NEITHER rapid request is granted an item on an exhausted tile (no duplication)",
              not r_x["accepted"] and not r_y["accepted"], results)

    # --- Test D3: DIG_BURIED on an ordinary empty (non-deposit) tile is rejected. --------------------
    empty_tile = (70, 90)  # far from both fixtures; matches test_pickup_sync.py's own "Test B" tile
    r3, _rid3 = field_action(a, KIND_DIG_BURIED, *empty_tile)
    check("Test D3: DIG_BURIED on an ordinary empty tile received an answer", r3 is not None, results)
    if r3 is not None:
        check("Test D3: rejected (no deposit bit set)", not r3["accepted"], results)

    # --- Test M1: MONEY_ROCK_HIT on the seeded money rock is accepted. -------------------------------
    m1, mrid1 = field_action(a, KIND_MONEY_ROCK_HIT, *ROCK_TILE)
    check("Test M1: MONEY_ROCK_HIT result received", m1 is not None, results)
    if m1 is not None:
        check("Test M1: accepted", m1["accepted"], results)
        check("Test M1: kind echoed", m1["kind"] == KIND_MONEY_ROCK_HIT, results)
        check("Test M1: granted_item is always 0 for MONEY_ROCK_HIT", m1["granted_item"] == EMPTY_NO, results)

    # --- Test M2 (retry dedup) + M3 (reward escalation) + M4 (bag IS pickupable): pick up the dropped
    # bag after hit 1, hit twice more (retrying the SECOND of those with the same request_id to prove
    # the retry does not double-count), then confirm hit_count 0..5 range map to the documented reward
    # tiers by reading the item back at the deterministic drop tile after each hit. -------------------
    def pickup_at(client, ut_x, ut_z):
        x, y, z = tile_center(ut_x, ut_z)
        client.claim_position(x, y, z)
        rid = fresh_request_id()
        client.send_pickup_request(ut_x, ut_z, rid)
        return client.recv_pickup_result(timeout=1.0, expect_request_id=rid)

    def read_and_clear_drop(expected_item, label):
        res = pickup_at(a, *ROCK_DROP_TILE)
        check(f"{label}: drop tile pickup answered", res is not None, results)
        if res is None:
            return
        accepted, _x, _z, _rid, item = res
        check(f"{label}: bag accepted (money-bag pickup exclusion correctly lifted for this tile)",
              accepted, results)
        if accepted:
            check(f"{label}: reward item == {expected_item:#x} (got {item:#x})", item == expected_item, results)

    # hit_count 0 (just happened via M1) -> ITM_MONEY_100
    read_and_clear_drop(ITM_MONEY_100, "Test M3 hit1")

    # hit 2 -> hit_count 1 -> still ITM_MONEY_100
    m2, mrid2 = field_action(a, KIND_MONEY_ROCK_HIT, *ROCK_TILE)
    check("Test M2: second hit accepted", m2 is not None and m2["accepted"], results)
    # retry the SAME request_id -- must be dedup-replayed, not re-applied (no second hit_count bump)
    send_field_action_request(a, KIND_MONEY_ROCK_HIT, ROCK_TILE[0], ROCK_TILE[1], mrid2)
    m2_replay = wait_field_action_result(a, mrid2, timeout=1.0)
    check("Test M2: retried request_id answered", m2_replay is not None, results)
    if m2_replay is not None:
        check("Test M2: retry replays the SAME outcome (accepted)", m2_replay["accepted"] == m2["accepted"], results)
    read_and_clear_drop(ITM_MONEY_100, "Test M3 hit2 (post-dedup-retry)")

    # hit 3 -> hit_count 2 -> still ITM_MONEY_100
    field_action(a, KIND_MONEY_ROCK_HIT, *ROCK_TILE)
    read_and_clear_drop(ITM_MONEY_100, "Test M3 hit3")

    # hit 4 -> hit_count 3 -> escalates to ITM_MONEY_1000
    field_action(a, KIND_MONEY_ROCK_HIT, *ROCK_TILE)
    read_and_clear_drop(ITM_MONEY_1000, "Test M3 hit4 (escalation to 1000)")

    # --- Test M5: pickup at an unrelated tile never dispenses a bag (no blanket exclusion lift). -----
    unrelated_tile = (72, 90)
    res5 = pickup_at(a, *unrelated_tile)
    check("Test M5: pickup at an unrelated empty tile is rejected (not a blanket money-bag lift)",
          res5 is None or not res5[0], results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
