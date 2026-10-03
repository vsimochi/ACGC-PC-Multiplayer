#!/usr/bin/env python3
"""test_snowman_sync.py - World Ecology: snowmen. REAL two-process synthetic network test against a
REAL, currently-running `AnimalCrossing.exe --host <port>` process.

Mirrors test_field_action_sync.py's own methodology exactly: hand-crafts wire bytes for
PC_NETGAME_MSG_SNOWMAN_BUILD_REQUEST(33)/SNOWMAN_BUILD_RESULT(34)/SNOWMAN_STATE(35) and for the
SNOWMAN_BREAK FIELD_ACTION kind (9), and drives a real host through them via net_spike_lib.FakeClient.
None of these are registered in net_spike_lib's message-spec tables, so this script builds/decodes
them itself and sends/waits for them via FakeClient's lower-level send_reliable()/inbox primitives.

No dedicated --snowman-test-seed fixture exists (unlike --field-action-test-seed), so this script
finds its own known-empty tiles at runtime via net_spike_lib.CandidateQueue + a real PICKUP_REQUEST
probe (exactly like test_drop_sync.py's own find_and_empty_tile()) -- a snowman may be built on any
ordinary town tile, so there is no need for a purpose-built fixture the way DIG_BURIED/MONEY_ROCK_HIT
need one.

What this covers (all against the REAL host process -- genuine Save_t.snowmen/Save_t.fg reads and
writes, genuine host-side validation):
  Test B1: SNOWMAN_BUILD_REQUEST on a known-empty tile is accepted, gets a real slot (0..2), and the
           tile's own FIELD_UPDATE shows a SNOWMAN0..8 value consistent with that slot.
  Test B2: the accompanying SNOWMAN_STATE broadcast shows exists=1 for that slot with the exact
           head_size/body_size/score this request sent, and a fresh (non-zero) world_seq.
  Test B3: a SECOND SNOWMAN_BUILD_REQUEST on the SAME (now-occupied) tile is rejected, reason
           TILE_INVALID -- no orphaned second slot for the same tile.
  Test B4 (slots full): after occupying all mSN_SAVE_COUNT (3) slots, a further build on a fresh empty
           tile is rejected, reason SLOTS_FULL, and nothing is committed (verified via a repeat
           request receiving the exact SAME dedup-replayed answer).
  Test K1 (SNOWMAN_BREAK, FIELD_ACTION kind 9): breaking one of the built snowmen is accepted; its
           tile's FIELD_UPDATE goes to EMPTY_NO, and the accompanying SNOWMAN_STATE shows that slot's
           exists go 1 -> 0.
  Test K2: breaking the SAME (now-empty) tile again is rejected (no longer a SNOWMAN0..8 value).
  Test K3: after K1 frees a slot, a fresh SNOWMAN_BUILD_REQUEST on a NEW empty tile is accepted again
           (SLOTS_FULL was genuinely lifted by the break, not just always-full from that point on).

Real two-process evidence note: like test_field_action_sync.py, this is a scripted stand-in for a
CLIENT sending raw protocol bytes -- it validates the HOST's real C code paths byte-for-byte
(pcnetgame_handle_host_snowman_build_request(), the SNOWMAN_BREAK adapters in s_field_action_handlers,
pcnetgame_host_check_snowman_state_diff()/pcnetgame_broadcast_snowman_state()). It does NOT exercise
the CLIENT-side C code that applies an accepted SNOWMAN_STATE into a second real, gameplay-driven
process (pcnetgame_handle_client_snowman_state()) or the ac_snowman.c/ac_psnowman.c actor seams
themselves (those only run inside real gameplay, which this headless harness cannot drive). The
Finding A live-actor-overlay fix (pcnetgame_host_resolve_snowman_tile_overlay()) is likewise not
exercised here -- it only matters while the HOST's own local player has a live PSNOWMAN actor in its
own scene, which requires real gameplay input this harness cannot script; that fix remains
SOURCE-ONLY-verified (see this milestone's own report).

Usage: python3 test_snowman_sync.py <host_ip> <port>
"""
import struct
import sys

from net_spike_lib import (
    CH_RELIABLE,
    CandidateQueue,
    EMPTY_NO,
    FakeClient,
    check,
    make_request_id_counter,
    summary_and_exit_code,
    tile_center,
)

FIELD_ACTION_REQUEST_TYPE = 29   # PC_NETGAME_MSG_FIELD_ACTION_REQUEST
FIELD_ACTION_RESULT_TYPE = 30    # PC_NETGAME_MSG_FIELD_ACTION_RESULT
KIND_SNOWMAN_BREAK = 9           # PC_NETGAME_FIELD_ACTION_KIND_SNOWMAN_BREAK

SNOWMAN_BUILD_REQUEST_TYPE = 33  # PC_NETGAME_MSG_SNOWMAN_BUILD_REQUEST
SNOWMAN_BUILD_RESULT_TYPE = 34   # PC_NETGAME_MSG_SNOWMAN_BUILD_RESULT
SNOWMAN_STATE_TYPE = 35          # PC_NETGAME_MSG_SNOWMAN_STATE

# PCNetGameFieldActionRequestMsg (current, post-T1-widening): msg_type, kind, ut_x, ut_z, uint32
# request_id, hole_variant, _reserved0, uint16 _reserved1 -- 12 bytes.
FA_REQ_FMT = "<BBBBIBBH64x"  # X3: 76 B = the 12-byte v3 header + the 64-byte txn tag (all zero = no grant)
FA_RES_FMT = "<BBBBIHBB"
FA_RES_SIZE = struct.calcsize(FA_RES_FMT)

# PCNetGameSnowmanBuildRequestMsg: msg_type, ut_x, ut_z, head_size, body_size, score, uint16
# _reserved0, uint32 request_id -- 12 bytes.
SB_REQ_FMT = "<BBBBBBHI"
# PCNetGameSnowmanBuildResultMsg: msg_type, accepted, ut_x, ut_z, uint32 request_id, slot, reason,
# uint16 _reserved0 -- 12 bytes.
SB_RES_FMT = "<BBBBIBBH"
SB_RES_SIZE = struct.calcsize(SB_RES_FMT)
# PCNetGameSnowmanStateMsg: msg_type, flags, uint16 _reserved0, uint32 world_seq, 12s snowmen,
# year, month, day, hour -- 24 bytes.
SS_FMT = "<BBHI12sBBBB"
SS_SIZE = struct.calcsize(SS_FMT)

SNOWMAN_REASON_NONE = 0
SNOWMAN_REASON_SLOTS_FULL = 1
SNOWMAN_REASON_TILE_INVALID = 2
SNOWMAN_REASON_NOT_READY = 3

SNOWMAN0 = 0xA008  # m_name_table.h
SNOWMAN8 = 0xA010
SNOWMAN_SAVE_COUNT = 3  # mSN_SAVE_COUNT

fresh_request_id = make_request_id_counter(30000)  # disjoint range from every other script's own counter


def build_field_action_request(kind, ut_x, ut_z, request_id):
    return struct.pack(FA_REQ_FMT, FIELD_ACTION_REQUEST_TYPE, kind & 0xFF, ut_x & 0xFF, ut_z & 0xFF,
                        request_id & 0xFFFFFFFF, 0, 0, 0)


def decode_field_action_result(payload):
    msg_type, kind, accepted, ut_x, request_id, granted_item, ut_z, _reserved0 = struct.unpack(FA_RES_FMT, payload)
    return dict(msg_type=msg_type, kind=kind, accepted=bool(accepted), ut_x=ut_x, ut_z=ut_z,
                request_id=request_id, granted_item=granted_item)


def wait_field_action_result(client, request_id, timeout=1.0):
    conn = client.connect_count

    def pred(m):
        if m.conn != conn or m.channel != CH_RELIABLE or not m.payload:
            return False
        if m.payload[0] != FIELD_ACTION_RESULT_TYPE or len(m.payload) != FA_RES_SIZE:
            return False
        return decode_field_action_result(m.payload)["request_id"] == request_id

    m = client.inbox.wait_for(pred, timeout)
    return None if m is None else decode_field_action_result(m.payload)


def snowman_break(client, ut_x, ut_z, timeout=1.0):
    x, y, z = tile_center(ut_x, ut_z)
    client.claim_position(x, y, z)
    rid = fresh_request_id()
    client.send_reliable(build_field_action_request(KIND_SNOWMAN_BREAK, ut_x, ut_z, rid))
    return wait_field_action_result(client, rid, timeout=timeout), rid


def build_snowman_build_request(ut_x, ut_z, head_size, body_size, score, request_id):
    return struct.pack(SB_REQ_FMT, SNOWMAN_BUILD_REQUEST_TYPE, ut_x & 0xFF, ut_z & 0xFF, head_size & 0xFF,
                        body_size & 0xFF, score & 0xFF, 0, request_id & 0xFFFFFFFF)


def decode_snowman_build_result(payload):
    msg_type, accepted, ut_x, ut_z, request_id, slot, reason, _reserved0 = struct.unpack(SB_RES_FMT, payload)
    return dict(msg_type=msg_type, accepted=bool(accepted), ut_x=ut_x, ut_z=ut_z, request_id=request_id,
                slot=slot, reason=reason)


def wait_snowman_build_result(client, request_id, timeout=1.0):
    conn = client.connect_count

    def pred(m):
        if m.conn != conn or m.channel != CH_RELIABLE or not m.payload:
            return False
        if m.payload[0] != SNOWMAN_BUILD_RESULT_TYPE or len(m.payload) != SB_RES_SIZE:
            return False
        return decode_snowman_build_result(m.payload)["request_id"] == request_id

    m = client.inbox.wait_for(pred, timeout)
    return None if m is None else decode_snowman_build_result(m.payload)


def snowman_build(client, ut_x, ut_z, head_size=180, body_size=220, score=3, timeout=1.0):
    x, y, z = tile_center(ut_x, ut_z)
    client.claim_position(x, y, z)
    rid = fresh_request_id()
    client.send_reliable(build_snowman_build_request(ut_x, ut_z, head_size, body_size, score, rid))
    return wait_snowman_build_result(client, rid, timeout=timeout), rid


def decode_snowman_state(payload):
    msg_type, flags, _reserved0, world_seq, snowmen, year, month, day, hour = struct.unpack(SS_FMT, payload)
    slots = [tuple(snowmen[i * 4:i * 4 + 4]) for i in range(SNOWMAN_SAVE_COUNT)]
    return dict(msg_type=msg_type, flags=flags, world_seq=world_seq, slots=slots, year=year, month=month,
                day=day, hour=hour)


def wait_snowman_state(client, min_world_seq=1, timeout=1.0, since=None):
    """The first SNOWMAN_STATE (with inbox index >= `since`, if given) with world_seq >= min_world_seq
    seen from now on, or None. `since` (client.inbox.mark(), captured BEFORE the action under test)
    guards against matching a STALE broadcast still sitting in the inbox from an earlier, unrelated
    mutation this script never consumed -- min_world_seq alone is not always enough once several
    builds/breaks have already happened in the same run."""
    conn = client.connect_count

    def pred(m):
        if m.conn != conn or m.channel != CH_RELIABLE or not m.payload:
            return False
        if since is not None and m.index < since:
            return False
        if m.payload[0] != SNOWMAN_STATE_TYPE or len(m.payload) != SS_SIZE:
            return False
        return decode_snowman_state(m.payload)["world_seq"] >= min_world_seq

    m = client.inbox.wait_for(pred, timeout)
    return None if m is None else decode_snowman_state(m.payload)


def find_and_empty_tile(client, queue, max_probes=60):
    """Consumes tiles from `queue` via real pickups until one succeeds (leaving that tile EMPTY_NO),
    or `max_probes` are exhausted. Returns (ut_x, ut_z) or None. Mirrors test_drop_sync.py's own
    helper of the same name/shape exactly."""
    tried = 0
    while tried < max_probes:
        tile = queue.next_tile()
        if tile is None:
            return None
        tried += 1
        x, y, z = tile_center(*tile)
        client.claim_position(x, y, z)
        rid = fresh_request_id()
        client.send_pickup_request(*tile, rid)
        result = client.recv_pickup_result(timeout=0.6, expect_request_id=rid)
        if result is not None and bool(result[0]):
            print(f"    prepared known-empty tile {tile} after {tried} probe(s)")
            return tile
    return None


def find_tile_value(updates, ut_x, ut_z):
    """Last (ut_x, ut_z, value) match in `updates` (as returned by FakeClient.drain_field_updates()),
    or None if this tile never appeared."""
    value = None
    for ut_x2, ut_z2, v in updates:
        if ut_x2 == ut_x and ut_z2 == ut_z:
            value = v
    return value


def main():
    if len(sys.argv) != 3:
        print("usage: test_snowman_sync.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    results = []

    a = FakeClient("A", host_ip, port)
    a.connect_and_ready()
    a.drain_field_updates(timeout=0.3)

    queue = CandidateQueue(fallback=True)

    # --- Test B1/B2: SNOWMAN_BUILD_REQUEST on a known-empty tile is accepted -------------------------
    tile1 = find_and_empty_tile(a, queue)
    check("setup: found a known-empty tile for Test B1", tile1 is not None, results)
    if tile1 is None:
        return summary_and_exit_code(results)

    since_b1 = a.inbox.mark()
    r1, rid1 = snowman_build(a, *tile1, head_size=180, body_size=220, score=3)
    check("Test B1: SNOWMAN_BUILD_RESULT received", r1 is not None, results)
    slot1 = None
    if r1 is not None:
        check("Test B1: accepted", r1["accepted"], results)
        check("Test B1: request_id echoed", r1["request_id"] == rid1, results)
        check("Test B1: reason == NONE on acceptance", r1["reason"] == SNOWMAN_REASON_NONE, results)
        slot1 = r1["slot"]
        check("Test B1: slot in 0..2", r1["accepted"] and 0 <= slot1 <= 2, results)

    tile1_value = find_tile_value(a.drain_field_updates(timeout=1.0), *tile1)
    check(f"Test B1: tile {tile1}'s FIELD_UPDATE shows a SNOWMAN0..8 value (got {tile1_value!r})",
          tile1_value is not None and SNOWMAN0 <= tile1_value <= SNOWMAN8, results)
    if tile1_value is not None and slot1 is not None:
        expected_base = SNOWMAN0 + slot1 * SNOWMAN_SAVE_COUNT
        check(f"Test B1: tile value's slot matches the RESULT's own slot {slot1} "
              f"(expected base {expected_base:#x}..{expected_base + 2:#x}, got {tile1_value:#x})",
              expected_base <= tile1_value <= expected_base + 2, results)

    state1 = wait_snowman_state(a, min_world_seq=1, timeout=1.0, since=since_b1)
    check("Test B2: SNOWMAN_STATE broadcast received after the build", state1 is not None, results)
    if state1 is not None and slot1 is not None:
        exists, head, body, score = state1["slots"][slot1]
        check(f"Test B2: slot {slot1} exists == 1", exists == 1, results)
        check(f"Test B2: slot {slot1} head_size == 180 (got {head})", head == 180, results)
        check(f"Test B2: slot {slot1} body_size == 220 (got {body})", body == 220, results)
        check(f"Test B2: slot {slot1} score == 3 (got {score})", score == 3, results)
        check("Test B2: world_seq > 0", state1["world_seq"] > 0, results)

    # --- Test B3: a second build on the SAME (now-occupied) tile is rejected, TILE_INVALID -----------
    r3, _rid3 = snowman_build(a, *tile1)
    check("Test B3: second build on the occupied tile received an answer", r3 is not None, results)
    if r3 is not None:
        check("Test B3: rejected", not r3["accepted"], results)
        check("Test B3: reason == TILE_INVALID", r3["reason"] == SNOWMAN_REASON_TILE_INVALID, results)

    # --- Test B4: fill every remaining slot, then a further build hits SLOTS_FULL --------------------
    filled_tiles = [tile1]
    filled_slots = {slot1}
    for i in range(SNOWMAN_SAVE_COUNT - 1):
        tile = find_and_empty_tile(a, queue)
        check(f"setup: found a known-empty tile for slot fill #{i + 2}", tile is not None, results)
        if tile is None:
            break
        r, _rid = snowman_build(a, *tile, head_size=100, body_size=100, score=1)
        check(f"Test B4 setup: build #{i + 2} accepted", r is not None and r["accepted"], results)
        if r is not None and r["accepted"]:
            filled_tiles.append(tile)
            filled_slots.add(r["slot"])

    check("Test B4 setup: all 3 slots now occupied (distinct)", len(filled_slots) == SNOWMAN_SAVE_COUNT, results)

    overflow_tile = find_and_empty_tile(a, queue)
    check("setup: found a known-empty tile for the overflow attempt", overflow_tile is not None, results)
    if overflow_tile is not None:
        r4, rid4 = snowman_build(a, *overflow_tile)
        check("Test B4: overflow build received an answer", r4 is not None, results)
        if r4 is not None:
            check("Test B4: rejected", not r4["accepted"], results)
            check("Test B4: reason == SLOTS_FULL", r4["reason"] == SNOWMAN_REASON_SLOTS_FULL, results)
            check("Test B4: slot == 0xFF on rejection", r4["slot"] == 0xFF, results)

        # dedup replay: resend the EXACT same request_id -- must replay the identical decision, not
        # re-evaluate (mirrors pcnetgame_handle_host_snowman_build_request()'s own dedup table).
        a.send_reliable(build_snowman_build_request(*overflow_tile, 180, 220, 3, rid4))
        r4_replay = wait_snowman_build_result(a, rid4, timeout=1.0)
        check("Test B4: retried request_id answered", r4_replay is not None, results)
        if r4_replay is not None:
            check("Test B4: retry replays the SAME rejection (dedup, not re-evaluated)",
                  not r4_replay["accepted"] and r4_replay["reason"] == SNOWMAN_REASON_SLOTS_FULL, results)

    # --- Test K1: SNOWMAN_BREAK (FIELD_ACTION kind 9) on one of the built snowmen is accepted --------
    break_tile = filled_tiles[0]
    # slot1 corresponds to filled_tiles[0] == tile1
    break_slot = slot1

    since_k1 = a.inbox.mark()  # so the SNOWMAN_STATE wait below can never match a stale B4-loop broadcast
    k1, _krid1 = snowman_break(a, *break_tile)
    check("Test K1: SNOWMAN_BREAK result received", k1 is not None, results)
    if k1 is not None:
        check("Test K1: accepted", k1["accepted"], results)
        check("Test K1: kind echoed == 9", k1["kind"] == KIND_SNOWMAN_BREAK, results)
        check("Test K1: granted_item always 0 for a break", k1["granted_item"] == EMPTY_NO, results)

    break_tile_value = find_tile_value(a.drain_field_updates(timeout=1.0), *break_tile)
    check(f"Test K1: broken tile {break_tile}'s latest known value is EMPTY_NO (got {break_tile_value!r})",
          break_tile_value == EMPTY_NO, results)

    state2 = wait_snowman_state(a, min_world_seq=1, timeout=1.0, since=since_k1)
    check("Test K1: a fresh SNOWMAN_STATE (newer world_seq) followed the break", state2 is not None, results)
    if state2 is not None and break_slot is not None:
        exists2, *_rest = state2["slots"][break_slot]
        check(f"Test K1: slot {break_slot} exists == 0 after the break", exists2 == 0, results)

    # --- Test K2: breaking the SAME (now-empty) tile again is rejected -------------------------------
    k2, _krid2 = snowman_break(a, *break_tile)
    check("Test K2: second break on the now-empty tile received an answer", k2 is not None, results)
    if k2 is not None:
        check("Test K2: rejected (no longer a SNOWMAN0..8 tile)", not k2["accepted"], results)

    # --- Test K3: the freed slot can be built into again (SLOTS_FULL genuinely lifted) ---------------
    tile_k3 = find_and_empty_tile(a, queue)
    check("setup: found a known-empty tile for Test K3", tile_k3 is not None, results)
    if tile_k3 is not None:
        rk3, _ridk3 = snowman_build(a, *tile_k3, head_size=50, body_size=50, score=0)
        check("Test K3: SNOWMAN_BUILD_RESULT received", rk3 is not None, results)
        if rk3 is not None:
            check("Test K3: accepted (the slot the break freed is usable again)", rk3["accepted"], results)
            check("Test K3: reused slot == the one the break freed", rk3["slot"] == break_slot, results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
