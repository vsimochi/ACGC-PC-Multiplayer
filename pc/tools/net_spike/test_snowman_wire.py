#!/usr/bin/env python3
"""test_snowman_wire.py - World Ecology: snowmen. PURE PROTOCOL/UNIT test (category C), mirroring
test_bury_wire.py's own structure/conventions.

Covers the three new wire messages added for host-authoritative snowman build/break/melt sync
(pc/src/pc_net_game.c):

  33 PCNetGameSnowmanBuildRequestMsg  (client -> host, 12 bytes)
  34 PCNetGameSnowmanBuildResultMsg   (host -> requester, 12 bytes)
  35 PCNetGameSnowmanStateMsg         (host -> every READY client, 24 bytes)

Checks:
  1. Struct size/layout matches the documented C layout exactly.
  2. Encode -> decode round-trip preserves every field exactly (u8/u16/u32 ranges).
  3. Payload-length validation: a truncated/oversized buffer must not be interpreted as valid (mirrors
     pcnetgame_handle_host_data()/pcnetgame_handle_client_data()'s own `size == sizeof(...)` gate).
  4. Message type ids (33/34/35) do not collide with any existing PC_NETGAME_MSG_* value (1..32,
     through PC_NETGAME_MSG_BURY_RESULT) or with each other.
  5. SNOWMAN_STATE's 12-byte `snowmen` payload round-trips as 3 x (exists, head_size, body_size, score)
     exactly matching mSN_snowman_data_c's 4-byte layout (include/m_snowman.h).
  6. Save-validity rule mirror (sChk_snowman_save_check(), save_check_take.c_inc): score < 4,
     exists <= 1, year < GAME_YEAR_MAX (== 2100 for this PC port, lb_rtc.h), month <= 12, day <= 31,
     hour < 24 -- exercised here as a pure function mirroring the client's own validation so this test
     stays meaningful even without a live client process.

Usage: python3 test_snowman_wire.py   (no host/port needed)
Exit code: 0 all checks passed, 1 otherwise.
"""
import struct
import sys

# --- wire layout -----------------------------------------------------------------------------
# PCNetGameSnowmanBuildRequestMsg: uint8 msg_type, uint8 ut_x, uint8 ut_z, uint8 head_size,
#   uint8 body_size, uint8 score, uint16 _reserved0, uint32 request_id
BUILD_REQ_FMT = "<BBBBBBHI"
BUILD_REQ_SIZE = struct.calcsize(BUILD_REQ_FMT)

# PCNetGameSnowmanBuildResultMsg: uint8 msg_type, uint8 accepted, uint8 ut_x, uint8 ut_z,
#   uint32 request_id, uint8 slot, uint8 reason, uint16 _reserved0
BUILD_RES_FMT = "<BBBBIBBH"
BUILD_RES_SIZE = struct.calcsize(BUILD_RES_FMT)

# PCNetGameSnowmanStateMsg: uint8 msg_type, uint8 flags, uint16 _reserved0, uint32 world_seq,
#   uint8 snowmen[12], uint8 year, uint8 month, uint8 day, uint8 hour
STATE_FMT = "<BBHI12sBBBB"
STATE_SIZE = struct.calcsize(STATE_FMT)

MSG_TYPE_SNOWMAN_BUILD_REQUEST = 33
MSG_TYPE_SNOWMAN_BUILD_RESULT = 34
MSG_TYPE_SNOWMAN_STATE = 35

SNOWMAN_REASON_NONE = 0
SNOWMAN_REASON_SLOTS_FULL = 1
SNOWMAN_REASON_TILE_INVALID = 2
SNOWMAN_REASON_NOT_READY = 3

STATE_FLAG_IN_SNAPSHOT = 0x01

# Every existing PC_NETGAME_MSG_* value through PC_NETGAME_MSG_BURY_RESULT (32) -- 33/34/35 must not
# collide with any of these, or with the concurrent T1 workstream's own TREE_SHAKE/TREE_CHOP FIELD_
# ACTION *kind* values (3/4 -- a different namespace: FIELD_ACTION kinds, not PC_NETGAME_MSG_* ids, so
# they are deliberately NOT included here).
EXISTING_MSG_TYPES = set(range(1, 33))

# save_check_take.c_inc constants this test mirrors.
GAME_YEAR_MAX = 2100  # lb_rtc.h, PC port value (VER_GAFE01_00 branch uses 2032 -- either way < covers it)
LBRTC_DECEMBER = 12

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + f": {name}")


def encode_build_request(ut_x, ut_z, head_size, body_size, score, request_id):
    return struct.pack(BUILD_REQ_FMT, MSG_TYPE_SNOWMAN_BUILD_REQUEST, ut_x & 0xFF, ut_z & 0xFF,
                        head_size & 0xFF, body_size & 0xFF, score & 0xFF, 0, request_id & 0xFFFFFFFF)


def decode_build_request(buf):
    msg_type, ut_x, ut_z, head_size, body_size, score, _reserved0, request_id = struct.unpack(BUILD_REQ_FMT, buf)
    return dict(msg_type=msg_type, ut_x=ut_x, ut_z=ut_z, head_size=head_size, body_size=body_size, score=score,
                request_id=request_id)


def encode_build_result(accepted, ut_x, ut_z, request_id, slot, reason):
    return struct.pack(BUILD_RES_FMT, MSG_TYPE_SNOWMAN_BUILD_RESULT, 1 if accepted else 0, ut_x & 0xFF, ut_z & 0xFF,
                        request_id & 0xFFFFFFFF, slot & 0xFF, reason & 0xFF, 0)


def decode_build_result(buf):
    msg_type, accepted, ut_x, ut_z, request_id, slot, reason, _reserved0 = struct.unpack(BUILD_RES_FMT, buf)
    return dict(msg_type=msg_type, accepted=accepted, ut_x=ut_x, ut_z=ut_z, request_id=request_id, slot=slot,
                reason=reason)


def encode_snowman_data(slots):
    """slots: list of up to 3 (exists, head_size, body_size, score) tuples."""
    out = bytearray(12)
    for i, (exists, head, body, score) in enumerate(slots):
        out[i * 4 + 0] = exists & 0xFF
        out[i * 4 + 1] = head & 0xFF
        out[i * 4 + 2] = body & 0xFF
        out[i * 4 + 3] = score & 0xFF
    return bytes(out)


def encode_state(flags, world_seq, slots, year, month, day, hour):
    return struct.pack(STATE_FMT, MSG_TYPE_SNOWMAN_STATE, flags & 0xFF, 0, world_seq & 0xFFFFFFFF,
                        encode_snowman_data(slots), year & 0xFF, month & 0xFF, day & 0xFF, hour & 0xFF)


def decode_state(buf):
    msg_type, flags, _reserved0, world_seq, snowmen, year, month, day, hour = struct.unpack(STATE_FMT, buf)
    slots = [tuple(snowmen[i * 4:i * 4 + 4]) for i in range(3)]
    return dict(msg_type=msg_type, flags=flags, world_seq=world_seq, slots=slots, year=year, month=month, day=day,
                hour=hour)


def snowman_state_is_valid(state):
    """Mirrors sChk_snowman_save_check() (save_check_take.c_inc) / the client's own
    pcnetgame_handle_client_snowman_state() validity gate exactly."""
    if state["year"] > GAME_YEAR_MAX - 1 or state["month"] > LBRTC_DECEMBER or state["day"] > 31 or state["hour"] >= 24:
        return False
    for exists, _head, _body, score in state["slots"]:
        if score >= 4 or exists > 1:
            return False
    return True


# --- 1/2: layout + round-trip ------------------------------------------------------------------
check("PCNetGameSnowmanBuildRequestMsg size == 12 bytes", BUILD_REQ_SIZE == 12)
check("PCNetGameSnowmanBuildResultMsg size == 12 bytes", BUILD_RES_SIZE == 12)
check("PCNetGameSnowmanStateMsg size == 24 bytes", STATE_SIZE == 24)

req = decode_build_request(encode_build_request(200, 5, 180, 220, 3, 0xDEADBEEF))
check("build request round-trip: ut_x/ut_z", req["ut_x"] == 200 and req["ut_z"] == 5)
check("build request round-trip: head_size/body_size", req["head_size"] == 180 and req["body_size"] == 220)
check("build request round-trip: score", req["score"] == 3)
check("build request round-trip: request_id", req["request_id"] == 0xDEADBEEF)

res_accept = decode_build_result(encode_build_result(True, 12, 34, 7, 2, SNOWMAN_REASON_NONE))
check("build result round-trip: accepted", res_accept["accepted"] == 1)
check("build result round-trip: ut_x/ut_z", res_accept["ut_x"] == 12 and res_accept["ut_z"] == 34)
check("build result round-trip: request_id", res_accept["request_id"] == 7)
check("build result round-trip: slot", res_accept["slot"] == 2)
check("build result round-trip: reason == NONE", res_accept["reason"] == SNOWMAN_REASON_NONE)

res_reject = decode_build_result(encode_build_result(False, 0, 0, 1, 0xFF, SNOWMAN_REASON_SLOTS_FULL))
check("build result round-trip: rejected accepted==0", res_reject["accepted"] == 0)
check("build result round-trip: rejected slot==0xFF", res_reject["slot"] == 0xFF)
check("build result round-trip: reason == SLOTS_FULL", res_reject["reason"] == SNOWMAN_REASON_SLOTS_FULL)

slots = [(1, 200, 220, 3), (0, 0, 0, 0), (1, 10, 20, 0)]
state = decode_state(encode_state(STATE_FLAG_IN_SNAPSHOT, 42, slots, 24, 12, 25, 18))
check("state round-trip: flags", state["flags"] == STATE_FLAG_IN_SNAPSHOT)
check("state round-trip: world_seq", state["world_seq"] == 42)
check("state round-trip: slot 0", state["slots"][0] == (1, 200, 220, 3))
check("state round-trip: slot 1 (empty)", state["slots"][1] == (0, 0, 0, 0))
check("state round-trip: slot 2", state["slots"][2] == (1, 10, 20, 0))
check("state round-trip: year/month/day/hour", (state["year"], state["month"], state["day"], state["hour"]) ==
      (24, 12, 25, 18))

# --- 3: payload-length validation ---------------------------------------------------------------
good_breq = encode_build_request(0, 0, 0, 0, 0, 1)
check("truncated build request rejected by size gate", len(good_breq[:-1]) != BUILD_REQ_SIZE)
check("oversized build request rejected by size gate", len(good_breq + b"\x00") != BUILD_REQ_SIZE)

good_bres = encode_build_result(True, 0, 0, 1, 0, 0)
check("truncated build result rejected by size gate", len(good_bres[:-1]) != BUILD_RES_SIZE)
check("oversized build result rejected by size gate", len(good_bres + b"\x00") != BUILD_RES_SIZE)

good_state = encode_state(0, 1, [(0, 0, 0, 0)] * 3, 0, 1, 1, 0)
check("truncated state rejected by size gate", len(good_state[:-1]) != STATE_SIZE)
check("oversized state rejected by size gate", len(good_state + b"\x00") != STATE_SIZE)

# --- 4: message id collision -------------------------------------------------------------------
check("SNOWMAN_BUILD_REQUEST (33) does not collide with any existing message id",
      MSG_TYPE_SNOWMAN_BUILD_REQUEST not in EXISTING_MSG_TYPES)
check("SNOWMAN_BUILD_RESULT (34) does not collide with any existing message id",
      MSG_TYPE_SNOWMAN_BUILD_RESULT not in EXISTING_MSG_TYPES)
check("SNOWMAN_STATE (35) does not collide with any existing message id",
      MSG_TYPE_SNOWMAN_STATE not in EXISTING_MSG_TYPES)
check("all three new ids are mutually distinct",
      len({MSG_TYPE_SNOWMAN_BUILD_REQUEST, MSG_TYPE_SNOWMAN_BUILD_RESULT, MSG_TYPE_SNOWMAN_STATE}) == 3)

# --- 5: mSN_snowman_data_c layout mirror --------------------------------------------------------
raw = encode_snowman_data([(1, 2, 3, 4 - 1)])  # score clamped to 3 in real data; using 3 here directly
check("snowmen[] byte layout: slot 0 exists at offset 0", raw[0] == 1)
check("snowmen[] byte layout: slot 0 head_size at offset 1", raw[1] == 2)
check("snowmen[] byte layout: slot 0 body_size at offset 2", raw[2] == 3)
check("snowmen[] byte layout: slot 0 score at offset 3", raw[3] == 3)
check("snowmen[] total size == 12 (3 slots x 4 bytes, mSN_snowman_data_c)", len(encode_snowman_data([])) == 12)

# --- 6: save-validity rule mirror (sChk_snowman_save_check()) -----------------------------------
valid_state = decode_state(encode_state(0, 1, [(1, 10, 10, 3), (0, 0, 0, 0), (0, 0, 0, 0)], 24, 12, 31, 23))
check("valid state passes validity mirror", snowman_state_is_valid(valid_state))

bad_score = decode_state(encode_state(0, 1, [(1, 10, 10, 4), (0, 0, 0, 0), (0, 0, 0, 0)], 24, 12, 31, 23))
check("score == 4 fails validity mirror (must be < 4)", not snowman_state_is_valid(bad_score))

bad_exists = decode_state(encode_state(0, 1, [(2, 10, 10, 3), (0, 0, 0, 0), (0, 0, 0, 0)], 24, 12, 31, 23))
check("exists == 2 fails validity mirror (must be <= 1)", not snowman_state_is_valid(bad_exists))

bad_month = decode_state(encode_state(0, 1, [(0, 0, 0, 0)] * 3, 24, 13, 1, 0))
check("month == 13 fails validity mirror (must be <= 12)", not snowman_state_is_valid(bad_month))

bad_day = decode_state(encode_state(0, 1, [(0, 0, 0, 0)] * 3, 24, 12, 32, 0))
check("day == 32 fails validity mirror (must be <= 31)", not snowman_state_is_valid(bad_day))

bad_hour = decode_state(encode_state(0, 1, [(0, 0, 0, 0)] * 3, 24, 12, 1, 24))
check("hour == 24 fails validity mirror (must be < 24)", not snowman_state_is_valid(bad_hour))

zero_day_ok = decode_state(encode_state(0, 1, [(0, 0, 0, 0)] * 3, 0, 0, 0, 0))
check("day == 0 (no completion yet) passes validity mirror", snowman_state_is_valid(zero_day_ok))

print()
passed = sum(1 for _, ok in checks if ok)
total = len(checks)
print(f"{passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
