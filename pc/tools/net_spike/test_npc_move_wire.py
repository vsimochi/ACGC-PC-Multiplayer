#!/usr/bin/env python3
"""test_npc_move_wire.py - N2 villager movement sync: PURE PROTOCOL/UNIT test (category C).

Does NOT launch the game and does NOT open any socket. It only checks that
PCNetGameNpcMoveMsg's documented wire layout (pc/src/pc_net_game.c) is exactly what this script's
own encode/decode helpers assume -- i.e. that the Python struct format string below stays in sync
with the C struct's field order/sizes/no-padding layout (msg_type u8, slot u8, action_type u8
[N3 Channel A: repurposed what was _reserved0 -- see PCNetGameNpcMoveMsg's doc comment],
_reserved1 u8, npc_id u16, _reserved2 u16, frame u32, pos_x/y/z f32 x3, facing_angle i16,
_reserved3 i16 -- total 28 bytes, matching PCNetGameNpcMoveMsg's own _Static_assert(sizeof(...) ==
28, ...) in pc_net_game.c).

This is a REGRESSION GUARD for the wire contract, not an end-to-end test: it cannot exercise the
real host/client accept/reject logic (that requires the real game process -- see this milestone's
completion report for the real two-process --bootstrap-resident run and its
"NPC_MOVE first sample accepted" / "identity mismatch" log evidence, since PC_NETGAME_MSG_NPC_MOVE
is host -> client only and this repo's net_spike harness has no FakeHost stub to impersonate the
host side of that direction). What IS covered here, purely in Python, mirroring the same
stale/duplicate/reorder and slot-reuse-identity RULES pc_net_game.c implements (re-derived from its
source, not imported):
  1. Struct size/layout matches the documented C layout exactly (28 bytes).
  2. Encode -> decode round-trip preserves every field exactly (u8/u16/u32/f32/i16 ranges).
  3. The "frame <= last_accepted_frame is stale/duplicate/reordered" rule from
     pcnetgame_handle_client_npc_move()'s doc comment, re-implemented and checked against a scripted
     sequence of frames.
  4. The "cached_npc_id != expected_npc_id -> reject, discard ring" slot-reuse rule from
     pc_net_game_get_npc_move_pose()'s doc comment, re-implemented and checked.
  5. Payload-length validation: a truncated/oversized buffer must not be interpreted as a valid
     message (mirrors pcnetgame_handle_client_data()'s `size == sizeof(PCNetGameNpcMoveMsg)` gate).

Usage: python3 test_npc_move_wire.py   (no host/port needed)
Exit code: 0 all checks passed, 1 otherwise.
"""
import struct
import sys

# Mirrors PCNetGameNpcMoveMsg exactly (see pc_net_game.c): u8,u8,u8,u8,u16,u16,u32,f32,f32,f32,i16,i16
# '<' = little-endian/no padding, matching the natural (unpadded) C layout confirmed by the
# _Static_assert(sizeof(PCNetGameNpcMoveMsg) == 28, ...) in pc_net_game.c.
FMT = "<BBBBHHIfffhh"
MSG_TYPE_NPC_MOVE = 26  # PC_NETGAME_MSG_NPC_MOVE (see pc_net_game.c's PCNetGameMsgType enum)

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + f": {name}")


def encode(slot, npc_id, frame, x, y, z, angle, action_type=0):
    return struct.pack(FMT, MSG_TYPE_NPC_MOVE, slot & 0xFF, action_type & 0xFF, 0, npc_id & 0xFFFF, 0,
                        frame & 0xFFFFFFFF, float(x), float(y), float(z),
                        angle & 0xFFFF if angle >= 0 else angle, 0)


def decode(buf):
    msg_type, slot, action_type, _r1, npc_id, _r2, frame, x, y, z, angle, _r3 = struct.unpack(FMT, buf)
    return dict(msg_type=msg_type, slot=slot, action_type=action_type, npc_id=npc_id, frame=frame, x=x, y=y, z=z,
                angle=angle)


def main():
    # 1. Layout size.
    check("struct size is 28 bytes (matches PCNetGameNpcMoveMsg's _Static_assert)", struct.calcsize(FMT) == 28)

    # 2. Encode/decode round trip.
    buf = encode(slot=3, npc_id=0xE006, frame=12345, x=2217.625, y=160.0, z=2270.625, angle=-1000)
    check("encoded buffer is exactly 28 bytes", len(buf) == 28)
    d = decode(buf)
    check("msg_type round-trips as PC_NETGAME_MSG_NPC_MOVE (26)", d["msg_type"] == 26)
    check("slot round-trips", d["slot"] == 3)
    check("npc_id round-trips", d["npc_id"] == 0xE006)
    check("frame round-trips", d["frame"] == 12345)
    check("pos_x round-trips exactly (representable float)", d["x"] == 2217.625)
    check("pos_y round-trips exactly (representable float)", d["y"] == 160.0)
    check("pos_z round-trips exactly (representable float)", d["z"] == 2270.625)
    check("facing_angle round-trips as signed 16-bit", d["angle"] == -1000)

    # 2c. N3 Channel A: action_type (aNPC_ACTION_TYPE_* on the wire's former _reserved0 byte).
    buf_act = encode(slot=1, npc_id=2, frame=1, x=0, y=0, z=0, angle=0, action_type=5)  # aNPC_ACTION_TYPE_WALK
    d_act = decode(buf_act)
    check("action_type round-trips (N3 Channel A repurposed byte)", d_act["action_type"] == 5)
    check("action_type fits the full u8 range (255)", decode(encode(0, 0, 0, 0, 0, 0, 0, 255))["action_type"] == 255)

    # 2b. Negative/wraparound facing angle (native engine angle units, s16 wraps at +/-32768).
    buf2 = encode(slot=0, npc_id=1, frame=1, x=0, y=0, z=0, angle=-32768)
    d2 = decode(buf2)
    check("facing_angle handles the -32768 wraparound boundary", d2["angle"] == -32768)

    # 3. Stale/duplicate/reordered rejection -- re-implements pcnetgame_handle_client_npc_move()'s
    #    rule: "ns->have_frame && in->frame <= ns->last_accepted_frame -> reject".
    class RingState:
        def __init__(self):
            self.have_frame = False
            self.last_accepted_frame = 0

        def accept(self, frame):
            if self.have_frame and frame <= self.last_accepted_frame:
                return False  # stale/duplicate/reordered
            self.have_frame = True
            self.last_accepted_frame = frame
            return True

    rs = RingState()
    check("first-ever frame (1) is accepted", rs.accept(1) is True)
    check("higher frame (2) is accepted", rs.accept(2) is True)
    check("duplicate frame (2) is REJECTED", rs.accept(2) is False)
    check("stale/reordered frame (1, older) is REJECTED", rs.accept(1) is False)
    check("higher frame (5, skipping some) is accepted", rs.accept(5) is True)
    check("out-of-order late frame (3, older than last accepted 5) is REJECTED", rs.accept(3) is False)

    # 4. Slot-reuse identity guard -- re-implements pc_net_game_get_npc_move_pose()'s rule:
    #    "cached_npc_id != expected_npc_id -> reject and discard the ring".
    class SlotState:
        def __init__(self):
            self.cached_npc_id = 0
            self.have_data = False

        def receive(self, npc_id):
            self.cached_npc_id = npc_id
            self.have_data = True

        def consume(self, expected_npc_id):
            if not self.have_data:
                return None  # nothing yet
            if self.cached_npc_id != expected_npc_id:
                self.have_data = False  # discard stale ring, matching memset(ns, 0, ...)
                self.cached_npc_id = 0
                return None  # rejected: identity mismatch
            return self.cached_npc_id

        def consume_raises_on_none_before_any_receive(self):
            return self.consume(0xE006) is None

    ss = SlotState()
    check("consume before any receive returns nothing (hold current pose)", ss.consume(0xE006) is None)
    ss.receive(0xE006)  # villager A occupies this slot
    check("consume with matching npc_id succeeds", ss.consume(0xE006) == 0xE006)
    # Villager A departs; villager B (different npc_id) arrives in the SAME slot. A late packet for
    # A must never move B.
    stale_result = ss.consume(0xB002)  # caller's live npc_id is now B's (0xB002), but ring still holds A's data
    check("slot reuse: late packet for departed occupant A is rejected once B occupies the slot",
          stale_result is None)
    check("slot reuse: ring was discarded after the mismatch (no leftover data)", ss.have_data is False)
    ss.receive(0xB002)  # B's own first real sample arrives
    check("slot reuse: B's own subsequent sample is accepted normally", ss.consume(0xB002) == 0xB002)

    # 5. Payload-length validation -- a truncated or oversized buffer must never be accepted as a
    #    valid NPC_MOVE (mirrors pcnetgame_handle_client_data()'s `size == sizeof(...)` exact-size gate).
    def would_be_dispatched(buf):
        return len(buf) == 28 and len(buf) > 0 and buf[0] == MSG_TYPE_NPC_MOVE

    check("a 27-byte (truncated) buffer is NOT dispatched as NPC_MOVE", not would_be_dispatched(buf[:-1]))
    check("a 29-byte (oversized) buffer is NOT dispatched as NPC_MOVE", not would_be_dispatched(buf + b"\x00"))
    check("an empty buffer is NOT dispatched as NPC_MOVE", not would_be_dispatched(b""))
    check("a correctly-sized buffer WITH the wrong msg_type is NOT dispatched as NPC_MOVE",
          not would_be_dispatched(b"\x04" + buf[1:]))

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
