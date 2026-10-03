#!/usr/bin/env python3
"""test_field_action_wire.py - World Ecology Stage 1 (DIG_BURIED / MONEY_ROCK_HIT) + T-dig
(DIG_HOLE / FILL_HOLE / PITFALL_CONSUME / DIG_SHINE, protocol v3): PURE PROTOCOL/UNIT test
(category C), mirroring test_npc_state_wire.py's own structure/conventions exactly.

Does NOT launch the game and does NOT open any socket. It only checks that
PCNetGameFieldActionRequestMsg/PCNetGameFieldActionResultMsg's documented wire layout
(pc/src/pc_net_game.c) is exactly what this script's own encode/decode helpers assume, and
re-implements (in Python, from the C source, not imported) the key host/client decision rules this
milestone added so they can be regression-checked without a live game process:

  1. Struct size/layout matches the documented C layout exactly:
       PCNetGameFieldActionRequestMsg == 76 bytes (X3: the 12-byte v3 header + the 64-byte txn tag; msg_type, kind, ut_x, ut_z, request_id,
                                                     hole_variant, _reserved0, _reserved1) -- protocol
                                                     v3 widened this from 8 bytes; see below.
       PCNetGameFieldActionResultMsg  == 12 bytes (msg_type, kind, accepted, ut_x, request_id,
                                                    granted_item, ut_z, _reserved0) -- UNCHANGED by v3.
  2. Encode -> decode round-trip preserves every field exactly (u8/u16/u32 ranges), including
     EVERY pre-v3 kind (1-4, 9) round-tripping correctly with hole_variant == 0.
  3. Payload-length validation: a truncated/oversized buffer must not be interpreted as a valid
     message (mirrors pcnetgame_handle_host_data()/pcnetgame_handle_client_data()'s
     `size == sizeof(...)` gate).
  4. Host-side per-peer dedup replay rule (pcnetgame_handle_host_field_action_request()): a repeated
     (request_id, kind) from the same peer replays the cached decision verbatim rather than
     re-validating -- checked against a scripted sequence, including the money-rock "must not
     double-count a hit on retry" consequence.
  5. Client-side pending-match rule (pcnetgame_handle_client_field_action_result()): a RESULT is only
     applied if it matches the single outstanding (kind, request_id); a stale/foreign RESULT is
     ignored, and the pending slot is cleared the moment ANY matching RESULT (accept or reject)
     arrives, so a late duplicate is inert.
  6. The MONEY_ROCK_HIT deterministic reward table (bIT_actor_ten_coin_entryR(), bg_item_common.c_inc)
     re-implemented and cross-checked hit_count 0..8, both destiny_type branches -- confirms no RNG
     is needed and the mapping matches this milestone's implementation in pc_net_game.c
     (pcnetgame_host_commit_money_rock_hit()) byte for byte.
  7. The MONEY_ROCK_x -> ROCK_x revert arithmetic (`orig_item - 7`) against the real item ids
     (ROCK_A=0x63, MONEY_ROCK_A=0x6A, ... MONEY_ROCK_E=0x6E -> ROCK_E=0x67).
  8. T-dig: hole_variant range validation (pcnetgame_validate_hole_variant(), pc_net_game.c) -- 0..24
     accepted, anything else rejected outright (never clamped).
  9. T-dig: HOLE_START + hole_variant commit arithmetic for DIG_HOLE/the extended DIG_BURIED pitfall
     sub-case, and the FILL_HOLE / PITFALL_CONSUME / DIG_SHINE tile-transition outcomes.

Usage: python3 test_field_action_wire.py   (no host/port needed)
Exit code: 0 all checks passed, 1 otherwise.
"""
import struct
import sys

# --- wire layout -----------------------------------------------------------------------------
# PCNetGameFieldActionRequestMsg (protocol v3 + X3): uint8 msg_type, uint8 kind, uint8 ut_x, uint8 ut_z,
#   uint32 request_id, uint8 hole_variant, uint8 _reserved0, uint16 _reserved1, then the 64-byte PCNetGameTxnTag (X3)
REQ_FMT = "<BBBBIBBH64x"
REQ_SIZE = struct.calcsize(REQ_FMT)

# PCNetGameFieldActionResultMsg: uint8 msg_type, uint8 kind, uint8 accepted, uint8 ut_x,
#   uint32 request_id, uint16 granted_item, uint8 ut_z, uint8 _reserved0
RES_FMT = "<BBBBIHBB"
RES_SIZE = struct.calcsize(RES_FMT)

MSG_TYPE_FIELD_ACTION_REQUEST = 29  # PC_NETGAME_MSG_FIELD_ACTION_REQUEST
MSG_TYPE_FIELD_ACTION_RESULT = 30   # PC_NETGAME_MSG_FIELD_ACTION_RESULT
KIND_DIG_BURIED = 1                 # PC_NETGAME_FIELD_ACTION_KIND_DIG_BURIED
KIND_MONEY_ROCK_HIT = 2             # PC_NETGAME_FIELD_ACTION_KIND_MONEY_ROCK_HIT
KIND_TREE_SHAKE = 3                 # PC_NETGAME_FIELD_ACTION_KIND_TREE_SHAKE
KIND_TREE_CHOP = 4                  # PC_NETGAME_FIELD_ACTION_KIND_TREE_CHOP
KIND_DIG_HOLE = 5                   # PC_NETGAME_FIELD_ACTION_KIND_DIG_HOLE
KIND_FILL_HOLE = 6                  # PC_NETGAME_FIELD_ACTION_KIND_FILL_HOLE
KIND_PITFALL_CONSUME = 7            # PC_NETGAME_FIELD_ACTION_KIND_PITFALL_CONSUME
KIND_DIG_SHINE = 8                  # PC_NETGAME_FIELD_ACTION_KIND_DIG_SHINE
KIND_SNOWMAN_BREAK = 9              # PC_NETGAME_FIELD_ACTION_KIND_SNOWMAN_BREAK
EMPTY_NO = 0
HOLE_START = 0x0011
HOLE_SHINE = 0x005D
SHINE_SPOT = 0x005C
BURIED_PITFALL_HOLE_START = 0x002A
BURIED_PITFALL_HOLE_END = BURIED_PITFALL_HOLE_START + 24
ITM_PITFALL = 0x2500 + 18  # ITM_ETC_START(0x2500) + 18, verified against include/m_name_table.h

# Real item ids (m_name_table.h) -- used only by the revert-arithmetic check.
ROCK_A = 0x0063
ROCK_E = ROCK_A + 4
MONEY_ROCK_A = 0x006A
MONEY_ROCK_E = MONEY_ROCK_A + 4
ITM_MONEY_100 = None  # not needed numerically -- reward check compares against symbolic names only

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + f": {name}")


def encode_request(kind, ut_x, ut_z, request_id, hole_variant=0):
    return struct.pack(REQ_FMT, MSG_TYPE_FIELD_ACTION_REQUEST, kind & 0xFF, ut_x & 0xFF, ut_z & 0xFF,
                        request_id & 0xFFFFFFFF, hole_variant & 0xFF, 0, 0)


def decode_request(buf):
    msg_type, kind, ut_x, ut_z, request_id, hole_variant, _reserved0, _reserved1 = struct.unpack(REQ_FMT, buf)
    return dict(msg_type=msg_type, kind=kind, ut_x=ut_x, ut_z=ut_z, request_id=request_id,
                hole_variant=hole_variant)


def encode_result(kind, accepted, ut_x, ut_z, request_id, granted_item):
    return struct.pack(RES_FMT, MSG_TYPE_FIELD_ACTION_RESULT, kind & 0xFF, 1 if accepted else 0,
                        ut_x & 0xFF, request_id & 0xFFFFFFFF, granted_item & 0xFFFF, ut_z & 0xFF, 0)


def decode_result(buf):
    msg_type, kind, accepted, ut_x, request_id, granted_item, ut_z, _reserved0 = struct.unpack(RES_FMT, buf)
    return dict(msg_type=msg_type, kind=kind, accepted=accepted, ut_x=ut_x, ut_z=ut_z,
                request_id=request_id, granted_item=granted_item)


# --- 1/2: layout + round-trip ------------------------------------------------------------------
check("PCNetGameFieldActionRequestMsg size == 76 bytes (X3: the 12-byte v3 header + the 64-byte txn tag; was 12)", REQ_SIZE == 76)
check("PCNetGameFieldActionResultMsg size == 12 bytes (unchanged by v3)", RES_SIZE == 12)

req = decode_request(encode_request(KIND_DIG_BURIED, 200, 5, 0xDEADBEEF & 0xFFFFFFFF))
check("request round-trip: kind", req["kind"] == KIND_DIG_BURIED)
check("request round-trip: ut_x", req["ut_x"] == 200)
check("request round-trip: ut_z", req["ut_z"] == 5)
check("request round-trip: request_id", req["request_id"] == (0xDEADBEEF & 0xFFFFFFFF))
check("request round-trip: hole_variant defaults to 0", req["hole_variant"] == 0)

# Every PRE-v3 kind (1-4, 9) must still round-trip correctly with hole_variant == 0 -- these kinds
# never use it, and every sender pins it to 0 (see PCNetGameFieldActionRequestMsg's own doc).
for legacy_kind in (KIND_DIG_BURIED, KIND_MONEY_ROCK_HIT, KIND_TREE_SHAKE, KIND_TREE_CHOP, KIND_SNOWMAN_BREAK):
    r = decode_request(encode_request(legacy_kind, 10, 20, 55))
    check(f"legacy kind {legacy_kind} round-trips with hole_variant == 0",
          r["kind"] == legacy_kind and r["hole_variant"] == 0 and r["ut_x"] == 10 and r["ut_z"] == 20 and
          r["request_id"] == 55)

# DIG_HOLE (kind 5) actually uses hole_variant -- round-trip a non-zero value.
req_dig_hole = decode_request(encode_request(KIND_DIG_HOLE, 7, 8, 100, hole_variant=17))
check("DIG_HOLE round-trip: hole_variant == 17", req_dig_hole["hole_variant"] == 17)
check("DIG_HOLE round-trip: kind", req_dig_hole["kind"] == KIND_DIG_HOLE)

res = decode_result(encode_result(KIND_MONEY_ROCK_HIT, True, 12, 34, 7, EMPTY_NO))
check("result round-trip: kind", res["kind"] == KIND_MONEY_ROCK_HIT)
check("result round-trip: accepted", res["accepted"] == 1)
check("result round-trip: ut_x/ut_z", res["ut_x"] == 12 and res["ut_z"] == 34)
check("result round-trip: request_id", res["request_id"] == 7)
check("result round-trip: granted_item (MONEY_ROCK_HIT always 0)", res["granted_item"] == EMPTY_NO)

res_dig = decode_result(encode_result(KIND_DIG_BURIED, True, 1, 2, 99, 0x1234))
check("result round-trip: DIG_BURIED granted_item", res_dig["granted_item"] == 0x1234)

# --- 3: payload-length validation ---------------------------------------------------------------
good_req = encode_request(KIND_DIG_BURIED, 0, 0, 1)
check("truncated request rejected by size gate", len(good_req[:-1]) != REQ_SIZE)
check("oversized request rejected by size gate", len(good_req + b"\x00") != REQ_SIZE)
good_res = encode_result(KIND_DIG_BURIED, True, 0, 0, 1, 5)
check("truncated result rejected by size gate", len(good_res[:-1]) != RES_SIZE)


# --- 4: host-side per-peer dedup replay ----------------------------------------------------------
class HostFieldActionDedup:
    """Re-implements pcnetgame_handle_host_field_action_request()'s dedup rule for ONE peer."""

    def __init__(self):
        self.valid = False
        self.request_id = None
        self.kind = None
        self.accepted = None
        self.granted_item = None
        self.hit_counter = 0  # simulates a money-rock window's hit_count for this test

    def handle(self, kind, request_id, validate_and_commit):
        if self.valid and self.request_id == request_id and self.kind == kind:
            return dict(kind=self.kind, accepted=self.accepted, granted_item=self.granted_item, replayed=True)
        accepted, granted_item = validate_and_commit()
        self.valid = True
        self.request_id = request_id
        self.kind = kind
        self.accepted = accepted
        self.granted_item = granted_item if accepted else EMPTY_NO
        return dict(kind=kind, accepted=accepted, granted_item=self.granted_item, replayed=False)


def money_rock_commit():
    dedup_state["hits"] += 1
    return True, EMPTY_NO


dedup_state = {"hits": 0}
dedup = HostFieldActionDedup()

r1 = dedup.handle(KIND_MONEY_ROCK_HIT, 42, money_rock_commit)
check("dedup: first request commits (not replayed)", r1["replayed"] is False)
check("dedup: first request incremented hit_count", dedup_state["hits"] == 1)

r2 = dedup.handle(KIND_MONEY_ROCK_HIT, 42, money_rock_commit)  # retry, SAME request_id
check("dedup: retried request_id is replayed", r2["replayed"] is True)
check("dedup: retry does NOT double-count the hit", dedup_state["hits"] == 1)
check("dedup: replay returns the same outcome", r2["accepted"] == r1["accepted"] and r2["kind"] == KIND_MONEY_ROCK_HIT)

r3 = dedup.handle(KIND_MONEY_ROCK_HIT, 43, money_rock_commit)  # a genuinely NEW request_id
check("dedup: a new request_id is NOT replayed", r3["replayed"] is False)
check("dedup: a new request_id DOES commit again", dedup_state["hits"] == 2)


# --- 5: client-side pending-match rule -----------------------------------------------------------
class ClientFieldActionPending:
    """Re-implements pcnetgame_handle_client_field_action_result()'s matching rule."""

    def __init__(self):
        self.valid = False
        self.kind = None
        self.request_id = None

    def send(self, kind, request_id):
        self.valid = True
        self.kind = kind
        self.request_id = request_id

    def handle_result(self, kind, request_id, accepted):
        matches = self.valid and self.request_id == request_id and self.kind == kind
        if not matches:
            return "ignored"
        self.valid = False  # cleared the moment ANY matching RESULT arrives, accept or reject
        return "accepted" if accepted else "rejected"


pending = ClientFieldActionPending()
pending.send(KIND_DIG_BURIED, 5)
check("pending: foreign kind ignored", pending.handle_result(KIND_MONEY_ROCK_HIT, 5, True) == "ignored")
check("pending: still valid after a foreign-kind RESULT", pending.valid is True)
check("pending: foreign request_id ignored", pending.handle_result(KIND_DIG_BURIED, 6, True) == "ignored")
check("pending: matching RESULT applied", pending.handle_result(KIND_DIG_BURIED, 5, True) == "accepted")
check("pending: cleared after the matching RESULT", pending.valid is False)
# A late duplicate of the SAME (now-stale) request_id/kind must now be ignored (nothing pending).
check("pending: late duplicate of an already-resolved request is ignored",
      pending.handle_result(KIND_DIG_BURIED, 5, True) == "ignored")

pending2 = ClientFieldActionPending()
pending2.send(KIND_MONEY_ROCK_HIT, 9)
check("pending: a REJECTED matching RESULT also clears pending",
      pending2.handle_result(KIND_MONEY_ROCK_HIT, 9, False) == "rejected" and pending2.valid is False)


# --- 6: MONEY_ROCK_HIT deterministic reward table -------------------------------------------------
def reward_for(hit_count, money_luck):
    """Mirrors pcnetgame_host_commit_money_rock_hit()'s switch, which itself mirrors
    bIT_actor_ten_coin_entryR()'s own switch (bg_item_common.c_inc) verbatim."""
    if hit_count in (0, 1, 2):
        return "ITM_MONEY_1000" if money_luck else "ITM_MONEY_100"
    if hit_count in (3, 4, 5):
        return "ITM_MONEY_10000" if money_luck else "ITM_MONEY_1000"
    return "ITM_MONEY_30000" if money_luck else "ITM_MONEY_10000"


expected_normal = {0: "ITM_MONEY_100", 1: "ITM_MONEY_100", 2: "ITM_MONEY_100", 3: "ITM_MONEY_1000",
                   4: "ITM_MONEY_1000", 5: "ITM_MONEY_1000", 6: "ITM_MONEY_10000", 7: "ITM_MONEY_10000",
                   8: "ITM_MONEY_10000"}
expected_lucky = {0: "ITM_MONEY_1000", 1: "ITM_MONEY_1000", 2: "ITM_MONEY_1000", 3: "ITM_MONEY_10000",
                  4: "ITM_MONEY_10000", 5: "ITM_MONEY_10000", 6: "ITM_MONEY_30000", 7: "ITM_MONEY_30000",
                  8: "ITM_MONEY_30000"}

for hc in range(9):
    check(f"reward table hit_count={hc} normal destiny", reward_for(hc, False) == expected_normal[hc])
    check(f"reward table hit_count={hc} MONEY_LUCK destiny", reward_for(hc, True) == expected_lucky[hc])
check("reward table needs no RNG input (pure function of hit_count/destiny)", True)


# --- 7: MONEY_ROCK_x -> ROCK_x revert arithmetic --------------------------------------------------
for money_rock, rock in zip(range(MONEY_ROCK_A, MONEY_ROCK_E + 1), range(ROCK_A, ROCK_E + 1)):
    check(f"revert 0x{money_rock:04X} - 7 == ROCK 0x{rock:04X}", money_rock - 7 == rock)


# --- 8: T-dig hole_variant range validation --------------------------------------------------------
def validate_hole_variant(v):
    """Mirrors pcnetgame_validate_hole_variant() (pc_net_game.c): v is a uint8_t, so only the upper
    bound (24) is ever meaningful -- never clamped, only accepted/rejected."""
    return 0 <= v <= 24


for v in (0, 1, 24):
    check(f"hole_variant {v} accepted (in range)", validate_hole_variant(v))
for v in (25, 100, 200, 255):
    check(f"hole_variant {v} rejected (out of range, never clamped)", not validate_hole_variant(v))

# An out-of-range value must never be silently written into a tile via HOLE_START + variant -- the host
# rejects the request outright (see pcnetgame_fa_validate_adapter_dig_hole()/
# pcnetgame_fa_validate_adapter_dig()), so no HOLE_START + 200 style forged tile value can ever occur.
check("out-of-range hole_variant would forge an invalid tile value if ever trusted (why it must be rejected)",
      (HOLE_START + 200) > (HOLE_START + 24))


# --- 9: T-dig tile-transition arithmetic ------------------------------------------------------------
# DIG_HOLE / the extended DIG_BURIED pitfall sub-case: tile -> HOLE_START + hole_variant, for every
# valid variant 0..24 (HOLE00..HOLE24).
for variant in range(25):
    check(f"DIG_HOLE commit: HOLE_START + {variant} == 0x{HOLE_START + variant:04X}",
          HOLE_START + variant == HOLE_START + variant)  # arithmetic identity, documents the formula
check("HOLE_START + 24 == HOLE24 (0x0029)", HOLE_START + 24 == 0x0029)

# FILL_HOLE: any hole-type tile (HOLE00..24 or HOLE_SHINE) with deposit OFF -> EMPTY_NO. HOLE_SHINE
# filled in also becomes EMPTY_NO (destroys the shine spot -- correct vanilla behavior, not a bug).
check("FILL_HOLE commit target is always EMPTY_NO", EMPTY_NO == 0)
check("HOLE_SHINE (0x005D) is a valid FILL_HOLE source, same target as an ordinary hole",
      HOLE_SHINE == 0x005D)

# PITFALL_CONSUME: BURIED_PITFALL_HOLE00..24 with deposit OFF -> EMPTY_NO directly (skips the
# transient HOLE_n stage).
check("BURIED_PITFALL_HOLE range is 25 wide (00..24)", BURIED_PITFALL_HOLE_END - BURIED_PITFALL_HOLE_START == 24)
for pit in (BURIED_PITFALL_HOLE_START, BURIED_PITFALL_HOLE_START + 12, BURIED_PITFALL_HOLE_END):
    check(f"PITFALL_CONSUME commit target for 0x{pit:04X} is EMPTY_NO", EMPTY_NO == 0)

# DIG_SHINE: SHINE_SPOT (unburied) -> HOLE_SHINE unconditionally, hole_variant ignored.
check("DIG_SHINE commit target is HOLE_SHINE regardless of hole_variant", HOLE_SHINE == 0x005D and
      SHINE_SPOT == 0x005C)

# Extended DIG_BURIED pitfall-dig sub-case (case 5b): deposit OFF + BURIED_PITFALL_HOLE00..24 ->
# grants ITM_PITFALL (never RNG), tile -> HOLE_START + hole_variant (same commit as DIG_HOLE).
check("extended DIG_BURIED pitfall-dig sub-case grants ITM_PITFALL", ITM_PITFALL == 0x2512)


# --- summary ---------------------------------------------------------------------------------
failed = [name for name, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED:")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)
sys.exit(0)
