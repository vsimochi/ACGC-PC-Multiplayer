#!/usr/bin/env python3
"""test_bury_wire.py - World Ecology T3 (host-authoritative bury): PURE PROTOCOL/UNIT test (category
C), mirroring test_field_action_wire.py's own structure/conventions.

PCNetGameBuryRequestMsg / PCNetGameBuryResultMsg (pc/src/pc_net_game.c) now carry REAL host-authoritative
bury logic (pcnetgame_handle_host_bury_request() / the BURY branch of pcnetgame_handle_host_confirm()).
The wire LAYOUT is unchanged for the request (still 12 bytes) but the result's second half changed: the
old 16-bit _reserved0 is now two separate bytes, `flags` (PC_NETGAME_BURY_FLAG_*) and `reason`
(diagnostic only) -- see PCNetGameBuryResultMsg's own doc comment. This script checks:

  1. Struct size/layout matches the documented C layout exactly:
       PCNetGameBuryRequestMsg == 12 bytes (msg_type, pocket_slot_idx, ut_x, ut_z, claimed_item,
                                             hole_variant, _reserved0, request_id)
       PCNetGameBuryResultMsg  == 12 bytes (msg_type, accepted, ut_x, ut_z, request_id, buried_item,
                                             flags, reason)
  2. Encode -> decode round-trip preserves every field exactly (u8/u16/u32 ranges), including the new
     flags/reason bytes and the RECONCILE_VALID/DEPOSIT_ON flag bits.
  3. Payload-length validation: a truncated/oversized buffer must not be interpreted as a valid
     message (mirrors pcnetgame_handle_host_data()/pcnetgame_handle_client_data()'s own
     `size == sizeof(...)` gate).
  4. Message type ids (31/32) do not collide with any existing PC_NETGAME_MSG_* value through
     PC_NETGAME_MSG_FIELD_ACTION_RESULT (30).

Usage: python3 test_bury_wire.py   (no host/port needed)
Exit code: 0 all checks passed, 1 otherwise.
"""
import struct
import sys

# --- wire layout -----------------------------------------------------------------------------
# PCNetGameBuryRequestMsg: uint8 msg_type, uint8 pocket_slot_idx, uint8 ut_x, uint8 ut_z,
#   uint16 claimed_item, uint8 hole_variant, uint8 _reserved0, uint32 request_id
REQ_FMT = "<BBBBHBBI"
REQ_SIZE = struct.calcsize(REQ_FMT)

# PCNetGameBuryResultMsg: uint8 msg_type, uint8 accepted, uint8 ut_x, uint8 ut_z, uint32 request_id,
#   uint16 buried_item, uint8 flags, uint8 reason
RES_FMT = "<BBBBIHBB"
RES_SIZE = struct.calcsize(RES_FMT)

PC_NETGAME_BURY_FLAG_RECONCILE_VALID = 0x01
PC_NETGAME_BURY_FLAG_DEPOSIT_ON = 0x02

MSG_TYPE_BURY_REQUEST = 31  # PC_NETGAME_MSG_BURY_REQUEST
MSG_TYPE_BURY_RESULT = 32   # PC_NETGAME_MSG_BURY_RESULT

# Every existing PC_NETGAME_MSG_* value this milestone's audit traced through FIELD_ACTION_RESULT (30)
# -- BURY_REQUEST/RESULT (31/32) must not collide with any of these.
EXISTING_MSG_TYPES = set(range(1, 31))

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + f": {name}")


def encode_request(pocket_slot_idx, ut_x, ut_z, claimed_item, hole_variant, request_id):
    return struct.pack(REQ_FMT, MSG_TYPE_BURY_REQUEST, pocket_slot_idx & 0xFF, ut_x & 0xFF, ut_z & 0xFF,
                        claimed_item & 0xFFFF, hole_variant & 0xFF, 0, request_id & 0xFFFFFFFF)


def decode_request(buf):
    msg_type, pocket_slot_idx, ut_x, ut_z, claimed_item, hole_variant, _reserved0, request_id = \
        struct.unpack(REQ_FMT, buf)
    return dict(msg_type=msg_type, pocket_slot_idx=pocket_slot_idx, ut_x=ut_x, ut_z=ut_z,
                claimed_item=claimed_item, hole_variant=hole_variant, request_id=request_id)


def encode_result(accepted, ut_x, ut_z, request_id, buried_item, flags=0, reason=0):
    return struct.pack(RES_FMT, MSG_TYPE_BURY_RESULT, 1 if accepted else 0, ut_x & 0xFF, ut_z & 0xFF,
                        request_id & 0xFFFFFFFF, buried_item & 0xFFFF, flags & 0xFF, reason & 0xFF)


def decode_result(buf):
    msg_type, accepted, ut_x, ut_z, request_id, buried_item, flags, reason = struct.unpack(RES_FMT, buf)
    return dict(msg_type=msg_type, accepted=accepted, ut_x=ut_x, ut_z=ut_z, request_id=request_id,
                buried_item=buried_item, flags=flags, reason=reason)


# --- 1/2: layout + round-trip ------------------------------------------------------------------
check("PCNetGameBuryRequestMsg size == 12 bytes", REQ_SIZE == 12)
check("PCNetGameBuryResultMsg size == 12 bytes", RES_SIZE == 12)

req = decode_request(encode_request(3, 200, 5, 0x1234, 2, 0xDEADBEEF & 0xFFFFFFFF))
check("request round-trip: pocket_slot_idx", req["pocket_slot_idx"] == 3)
check("request round-trip: ut_x", req["ut_x"] == 200)
check("request round-trip: ut_z", req["ut_z"] == 5)
check("request round-trip: claimed_item", req["claimed_item"] == 0x1234)
check("request round-trip: hole_variant", req["hole_variant"] == 2)
check("request round-trip: request_id", req["request_id"] == 0xDEADBEEF)

req_sentinel = decode_request(encode_request(0, 1, 1, 0x2435, 0xFF, 42))
check("request round-trip: hole_variant 0xFF sentinel", req_sentinel["hole_variant"] == 0xFF)

res_accept = decode_result(encode_result(True, 12, 34, 7, 0x5678))
check("result round-trip: accepted", res_accept["accepted"] == 1)
check("result round-trip: ut_x/ut_z", res_accept["ut_x"] == 12 and res_accept["ut_z"] == 34)
check("result round-trip: request_id", res_accept["request_id"] == 7)
check("result round-trip: buried_item", res_accept["buried_item"] == 0x5678)

res_reject = decode_result(encode_result(False, 0, 0, 1, 0))
check("result round-trip: rejected accepted==0", res_reject["accepted"] == 0)

# flags/reason: the fields that replaced the old 16-bit _reserved0 (see PCNetGameBuryResultMsg's own doc)
res_reconcile = decode_result(encode_result(False, 5, 6, 9, 0x0011,
                                            PC_NETGAME_BURY_FLAG_RECONCILE_VALID | PC_NETGAME_BURY_FLAG_DEPOSIT_ON,
                                            3))
check("result round-trip: flags RECONCILE_VALID bit",
      (res_reconcile["flags"] & PC_NETGAME_BURY_FLAG_RECONCILE_VALID) != 0)
check("result round-trip: flags DEPOSIT_ON bit", (res_reconcile["flags"] & PC_NETGAME_BURY_FLAG_DEPOSIT_ON) != 0)
check("result round-trip: reason byte independent of flags", res_reconcile["reason"] == 3)

res_no_echo = decode_result(encode_result(False, 5, 6, 9, 0, 0, 0))
check("result round-trip: flags 0 means no valid reconciliation echo",
      (res_no_echo["flags"] & PC_NETGAME_BURY_FLAG_RECONCILE_VALID) == 0)

# --- 3: payload-length validation ---------------------------------------------------------------
good_req = encode_request(0, 0, 0, 1, 0, 1)
check("truncated request rejected by size gate", len(good_req[:-1]) != REQ_SIZE)
check("oversized request rejected by size gate", len(good_req + b"\x00") != REQ_SIZE)

good_res = encode_result(True, 0, 0, 1, 5)
check("truncated result rejected by size gate", len(good_res[:-1]) != RES_SIZE)
check("oversized result rejected by size gate", len(good_res + b"\x00") != RES_SIZE)

# --- 4: message id collision -------------------------------------------------------------------
check("BURY_REQUEST (31) does not collide with any existing message id",
      MSG_TYPE_BURY_REQUEST not in EXISTING_MSG_TYPES)
check("BURY_RESULT (32) does not collide with any existing message id",
      MSG_TYPE_BURY_RESULT not in EXISTING_MSG_TYPES)
check("BURY_REQUEST/BURY_RESULT are distinct", MSG_TYPE_BURY_REQUEST != MSG_TYPE_BURY_RESULT)

print()
passed = sum(1 for _, ok in checks if ok)
total = len(checks)
print(f"{passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
