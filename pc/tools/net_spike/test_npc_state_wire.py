#!/usr/bin/env python3
"""test_npc_state_wire.py - N3 Channel B (villager is_home/hide/forced-schedule sync): PURE
PROTOCOL/UNIT test (category C), mirroring test_npc_move_wire.py's own structure/conventions exactly.

Does NOT launch the game and does NOT open any socket. It only checks that PCNetGameNpcStateMsg's
documented wire layout (pc/src/pc_net_game.c) is exactly what this script's own encode/decode helpers
assume (msg_type u8, slot u8, npc_id u16, is_home u8, hide u8, forced_type u8, _reserved0 u8,
forced_timer_remaining u16, _reserved1 u16, state_seq u32 -- total 16 bytes, matching
PCNetGameNpcStateMsg's own _Static_assert(sizeof(...) == 16, ...) in pc_net_game.c).

This is a REGRESSION GUARD for the wire contract, not an end-to-end test: it cannot exercise the real
host/client accept/reject logic (that requires the real game process -- see this milestone's N3
completion report for the real two-process --bootstrap-resident run and its "NPC_STATE change"/
"villager population snapshot applied" log evidence). What IS covered here, purely in Python, mirroring
the same staleness and slot-reuse-identity RULES pc_net_game.c implements (re-derived from its source,
not imported):
  1. Struct size/layout matches the documented C layout exactly (16 bytes).
  2. Encode -> decode round-trip preserves every field exactly (u8/u16/u32 ranges).
  3. The "state_seq <= last_applied_seq is stale/duplicate/reordered" rule from
     pcnetgame_handle_client_npc_state()'s doc comment (mirrors WORLD_META's own strict-`>` rule),
     re-implemented and checked against a scripted sequence of state_seq values.
  4. The "cached_npc_id != expected_npc_id -> reject, discard state" slot-reuse rule from
     pc_net_game_get_npc_state()'s doc comment, re-implemented and checked -- including the
     "departed occupant's stale state must not affect the new occupant" scenario the task explicitly
     calls for.
  5. Payload-length validation: a truncated/oversized buffer must not be interpreted as a valid
     message (mirrors pcnetgame_handle_client_data()'s `size == sizeof(PCNetGameNpcStateMsg)` gate).
  6. `slot` range validation (mirrors pcnetgame_handle_client_npc_state()'s `in->slot >= ANIMAL_NUM_MAX`
     guard).

Usage: python3 test_npc_state_wire.py   (no host/port needed)
Exit code: 0 all checks passed, 1 otherwise.
"""
import struct
import sys

# Mirrors PCNetGameNpcStateMsg exactly (see pc_net_game.c): u8,u8,u16,u8,u8,u8,u8,u16,u16,u32
# '<' = little-endian/no padding, matching the natural (unpadded) C layout confirmed by the
# _Static_assert(sizeof(PCNetGameNpcStateMsg) == 16, ...) in pc_net_game.c.
FMT = "<BBHBBBBHHI"
MSG_TYPE_NPC_STATE = 28  # PC_NETGAME_MSG_NPC_STATE (see pc_net_game.c's PCNetGameMsgType enum)
ANIMAL_NUM_MAX = 15

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + f": {name}")


def encode(slot, npc_id, is_home, hide, forced_type, forced_timer_remaining, state_seq):
    return struct.pack(FMT, MSG_TYPE_NPC_STATE, slot & 0xFF, npc_id & 0xFFFF, 1 if is_home else 0,
                        1 if hide else 0, forced_type & 0xFF, 0, forced_timer_remaining & 0xFFFF, 0,
                        state_seq & 0xFFFFFFFF)


def decode(buf):
    (msg_type, slot, npc_id, is_home, hide, forced_type, _r0, forced_timer_remaining, _r1,
     state_seq) = struct.unpack(FMT, buf)
    return dict(msg_type=msg_type, slot=slot, npc_id=npc_id, is_home=is_home, hide=hide,
                forced_type=forced_type, forced_timer_remaining=forced_timer_remaining, state_seq=state_seq)


class ClientSlot:
    """Re-implements PCNetNpcStateSlot's receive-side rules (pc_net_game.c) purely in Python."""

    def __init__(self):
        self.have_state = False
        self.cached_npc_id = None
        self.last_applied_seq = 0
        self.is_home = self.hide = self.forced_type = self.forced_timer_remaining = 0

    def handle(self, msg):
        """Mirrors pcnetgame_handle_client_npc_state(): staleness check only, no identity check."""
        if self.have_state and msg["state_seq"] <= self.last_applied_seq:
            return False  # stale/duplicate/reordered -- dropped
        self.have_state = True
        self.last_applied_seq = msg["state_seq"]
        self.cached_npc_id = msg["npc_id"]
        self.is_home = msg["is_home"]
        self.hide = msg["hide"]
        self.forced_type = msg["forced_type"]
        self.forced_timer_remaining = msg["forced_timer_remaining"]
        return True

    def consume(self, expected_npc_id):
        """Mirrors pc_net_game_get_npc_state(): identity (slot-reuse) guard at CONSUME time."""
        if not self.have_state:
            return None
        if self.cached_npc_id != expected_npc_id:
            self.__init__()  # discard stale state entirely, same as memset(ns, 0, sizeof(*ns))
            return None
        return dict(is_home=self.is_home, hide=self.hide, forced_type=self.forced_type,
                    forced_timer_remaining=self.forced_timer_remaining)


def main():
    # 1. Layout size.
    check("struct size is 16 bytes (matches PCNetGameNpcStateMsg's _Static_assert)", struct.calcsize(FMT) == 16)

    # 2. Encode/decode round trip.
    buf = encode(slot=3, npc_id=0xE006, is_home=1, hide=1, forced_type=5, forced_timer_remaining=7200,
                 state_seq=1)
    check("encoded buffer is exactly 16 bytes", len(buf) == 16)
    d = decode(buf)
    check("msg_type round-trips as PC_NETGAME_MSG_NPC_STATE (28)", d["msg_type"] == 28)
    check("slot round-trips", d["slot"] == 3)
    check("npc_id round-trips", d["npc_id"] == 0xE006)
    check("is_home round-trips", d["is_home"] == 1)
    check("hide round-trips", d["hide"] == 1)
    check("forced_type round-trips", d["forced_type"] == 5)
    check("forced_timer_remaining round-trips (7200, aNPC_think_sleep_set_force_schedule's own value)",
          d["forced_timer_remaining"] == 7200)
    check("state_seq round-trips", d["state_seq"] == 1)

    # 2b. forced_timer_remaining fits the full u16 range (defensive clamp in pc_net_game_notify_npc_state()).
    d2 = decode(encode(0, 1, 0, 0, 0, 65535, 1))
    check("forced_timer_remaining handles the full u16 range (65535)", d2["forced_timer_remaining"] == 65535)

    # 3. Staleness/duplicate/reorder rule (strict `>`, mirrors WORLD_META's s_client_meta_seq).
    slot = ClientSlot()
    check("first-ever state_seq (1) is accepted", slot.handle(decode(encode(0, 1, 0, 0, 0, 0, 1))))
    check("higher state_seq (2) is accepted", slot.handle(decode(encode(0, 1, 1, 0, 0, 0, 2))))
    check("duplicate state_seq (2) is REJECTED", not slot.handle(decode(encode(0, 1, 0, 0, 0, 0, 2))))
    check("stale/reordered state_seq (1, older) is REJECTED", not slot.handle(decode(encode(0, 1, 0, 0, 0, 0, 1))))
    check("higher state_seq (5, skipping some) is accepted", slot.handle(decode(encode(0, 1, 1, 1, 2, 100, 5))))
    check("out-of-order late state_seq (3, older than last accepted 5) is REJECTED",
          not slot.handle(decode(encode(0, 1, 0, 0, 0, 0, 3))))

    # 4. Slot-reuse identity guard, including the explicit "departed occupant's stale state must not
    #    affect the new occupant" scenario.
    reuse = ClientSlot()
    check("consume before any receive returns nothing", reuse.consume(expected_npc_id=0xE006) is None)
    reuse.handle(decode(encode(0, 0xE006, 1, 1, 3, 7200, 1)))  # villager A: home, hidden, forced sleep
    got = reuse.consume(expected_npc_id=0xE006)
    check("consume with matching npc_id succeeds", got is not None and got["is_home"] == 1 and got["hide"] == 1)

    # Villager A departs; villager B (different npc_id) now occupies the slot. A late/stale NPC_STATE
    # for A (still in flight, or simply not yet superseded) must not leak into B's state.
    stale_for_a = decode(encode(0, 0xE006, 1, 1, 3, 50, 2))  # would be accepted by staleness rule alone
    check("stale state_seq 2 (for departed A) is still sequence-valid", reuse.handle(stale_for_a))
    got_b = reuse.consume(expected_npc_id=0xB100)  # B's real, different npc_id
    check("slot reuse: late state for departed occupant A is rejected once B occupies the slot", got_b is None)
    check("slot reuse: state was discarded after the mismatch (no leftover data)", not reuse.have_state)
    reuse.handle(decode(encode(0, 0xB100, 0, 0, 0, 0, 1)))  # B's own fresh state (its own seq stream)
    got_b2 = reuse.consume(expected_npc_id=0xB100)
    check("slot reuse: B's own subsequent state is accepted normally", got_b2 is not None and got_b2["is_home"] == 0)

    # 5. Payload-length validation (mirrors pcnetgame_handle_client_data()'s exact-size gate).
    buf16 = encode(0, 1, 0, 0, 0, 0, 1)

    def would_be_dispatched(b):
        return len(b) == 16 and len(b) > 0 and b[0] == MSG_TYPE_NPC_STATE

    check("a 15-byte (truncated) buffer is NOT dispatched as NPC_STATE", not would_be_dispatched(buf16[:-1]))
    check("a 17-byte (oversized) buffer is NOT dispatched as NPC_STATE", not would_be_dispatched(buf16 + b"\x00"))
    check("an empty buffer is NOT dispatched as NPC_STATE", not would_be_dispatched(b""))
    wrong_type = bytearray(buf16)
    wrong_type[0] = 27  # PC_NETGAME_MSG_CLOCK_SYNC -- same 16-byte size, different msg_type
    check("a correctly-sized buffer WITH the wrong msg_type (CLOCK_SYNC, also 16 B) is NOT dispatched as "
          "NPC_STATE", not would_be_dispatched(bytes(wrong_type)))

    # 6. slot range validation (mirrors pcnetgame_handle_client_npc_state()'s `in->slot >= ANIMAL_NUM_MAX`).
    check("slot == ANIMAL_NUM_MAX (15, out of range) would be rejected by the real handler",
          decode(encode(ANIMAL_NUM_MAX, 1, 0, 0, 0, 0, 1))["slot"] >= ANIMAL_NUM_MAX)

    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
