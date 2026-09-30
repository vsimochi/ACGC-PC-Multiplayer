#!/usr/bin/env python3
"""test_clock_sync_wire.py - N-clock milestone: PURE PROTOCOL/UNIT test (category C).

Does NOT launch the game and does NOT open any socket. It only checks that
PCNetGameClockSyncMsg's documented wire layout (pc/src/pc_net_game.c) is exactly what this script's
own encode/decode helpers assume -- msg_type u8, flags u8, _reserved0 u16, clock_seq u32,
host_game_ticks i64 -- total 16 bytes, matching PCNetGameClockSyncMsg's own
_Static_assert(sizeof(...) == 16, ...) in pc_net_game.c -- and re-derives (from source, not
imported) the client-side accept/apply rules pcnetgame_handle_client_clock_sync() implements:

  1. Struct size/layout matches the documented C layout exactly (16 bytes).
  2. Encode -> decode round-trip preserves every field exactly (u8/u32/i64 ranges, including a
     negative host_game_ticks-relative offset and large tick values).
  3. The "clock_seq <= last_applied -> reject (stale/duplicate/reordered)" rule.
  4. The "first sync (last_applied == 0) is always applied unconditionally" rule.
  5. The "|new_offset - current_offset| > tolerance (2s in ticks) -> re-apply; else keep current
     offset (but still advance last_applied seq)" drift-tolerance rule -- including the DISCONTINUITY
     flag forcing an unconditional re-apply regardless of tolerance.
  6. Payload-length validation: a truncated/oversized buffer, or one with the wrong msg_type, must
     not be interpreted as a valid CLOCK_SYNC (mirrors pcnetgame_handle_client_data()'s
     `size == sizeof(PCNetGameClockSyncMsg)` exact-size gate) -- note PCNetGameVillagerArrivalMsg is
     ALSO 16 bytes, so the msg_type byte is the only thing that disambiguates them on the wire; this
     is exercised explicitly.
  7. Reconnect semantics (Rule 2 of the design, re-derived from pcnetgame_reset_client_session_state()
     in pc_net_game.c): resetting the sequence tracker to 0 does NOT reset the offset itself, and the
     first post-reconnect sync is unconditionally accepted (covered by check 4's same machinery).
  8. No direction-based (forward/backward) guard exists in pcnetgame_handle_client_clock_sync(): once
     past the accept gate (rule 5 above), the candidate offset is ALWAYS applied, regardless of whether
     it moves the effective clock forward or backward. This was proven safe during the clock hardening
     follow-up fix pass: CLOCK_SYNC is sent PC_NET_RELIABLE, and pc_net.h documents that reliable
     transport delivers every payload to a peer's game layer exactly once and in send order per peer/
     direction -- so combined with the strict `clock_seq >` staleness check (rule 3), any packet that
     reaches the accept gate is by construction the host's genuinely newer state, never a stale/
     reordered/duplicate one. A backward-blocking guard was briefly added and then reverted as
     unnecessary and actively harmful (it would have permanently locked out legitimate backward
     corrections, e.g. oscillator-drift correction or a host clock rolled backward, once
     seq_applied != 0). These checks assert the CORRECTED (guard-free) behavior.

Usage: python3 test_clock_sync_wire.py   (no host/port needed)
Exit code: 0 all checks passed, 1 otherwise.
"""
import struct
import sys

# Mirrors PCNetGameClockSyncMsg exactly (see pc_net_game.c): u8,u8,u16,u32,i64
# '<' = little-endian/no padding, matching the natural (unpadded) C layout confirmed by the
# _Static_assert(sizeof(PCNetGameClockSyncMsg) == 16, ...) in pc_net_game.c.
FMT = "<BBHIq"
MSG_TYPE_CLOCK_SYNC = 27  # PC_NETGAME_MSG_CLOCK_SYNC (see pc_net_game.c's PCNetGameMsgType enum)
FLAG_DISCONTINUITY = 0x01

GC_TIMER_CLOCK = 162000000 // 4  # pc_platform.h GC_BUS_CLOCK/4 -- ticks per real second
TOLERANCE_TICKS = 2 * GC_TIMER_CLOCK  # PC_NETGAME_CLOCK_SYNC_TOLERANCE_SEC == 2

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + f": {name}")


def encode(clock_seq, host_game_ticks, flags=0):
    return struct.pack(FMT, MSG_TYPE_CLOCK_SYNC, flags & 0xFF, 0, clock_seq & 0xFFFFFFFF,
                        host_game_ticks)


def decode(buf):
    msg_type, flags, _r0, clock_seq, host_game_ticks = struct.unpack(FMT, buf)
    return dict(msg_type=msg_type, flags=flags, clock_seq=clock_seq, host_game_ticks=host_game_ticks)


class ClientClockState:
    """Re-implements pcnetgame_handle_client_clock_sync()'s accept/apply rules (pc_net_game.c)."""

    def __init__(self):
        self.seq_applied = 0   # s_client_clock_seq_applied
        self.offset = 0        # pc_lb_rtc_get/set_net_clock_offset() (lb_rtc.c) -- survives reconnect()

    def reconnect(self):
        # Rule 2 / pcnetgame_reset_client_session_state(): seq resets, offset does NOT.
        self.seq_applied = 0

    def receive(self, clock_seq, host_game_ticks, local_now_ticks, flags=0):
        """Returns True if the offset was actually (re)applied, False if the message was
        accepted-but-within-tolerance (seq still advances, offset unchanged), or None if the message
        was stale/duplicate/reordered (never processed at all). No direction-based guard: a candidate
        that clears the accept gate is applied whether it moves the clock forward or backward -- see
        this file's module docstring, check 8."""
        if clock_seq <= self.seq_applied:
            return None  # stale/duplicate/reordered -- not even considered
        candidate_offset = host_game_ticks - local_now_ticks
        diff = abs(candidate_offset - self.offset)
        discontinuity = bool(flags & FLAG_DISCONTINUITY)
        first_sync = self.seq_applied == 0
        gated_in = first_sync or discontinuity or (diff > TOLERANCE_TICKS)
        applied = False
        if gated_in:
            self.offset = candidate_offset
            applied = True
        self.seq_applied = clock_seq
        return applied


def main():
    # 1. Layout size.
    check("struct size is 16 bytes (matches PCNetGameClockSyncMsg's _Static_assert)", struct.calcsize(FMT) == 16)

    # 2. Encode/decode round trip.
    buf = encode(clock_seq=42, host_game_ticks=123456789012345, flags=0)
    check("encoded buffer is exactly 16 bytes", len(buf) == 16)
    d = decode(buf)
    check("msg_type round-trips as PC_NETGAME_MSG_CLOCK_SYNC (27)", d["msg_type"] == 27)
    check("clock_seq round-trips", d["clock_seq"] == 42)
    check("host_game_ticks round-trips exactly (large positive i64)", d["host_game_ticks"] == 123456789012345)
    check("flags round-trips as 0", d["flags"] == 0)

    buf_neg = encode(clock_seq=1, host_game_ticks=-987654321, flags=FLAG_DISCONTINUITY)
    d_neg = decode(buf_neg)
    check("host_game_ticks round-trips exactly (negative i64 -- pre-epoch/underflowed OSTime)",
          d_neg["host_game_ticks"] == -987654321)
    check("DISCONTINUITY flag round-trips", d_neg["flags"] & FLAG_DISCONTINUITY != 0)

    # 3 & 4. Stale/duplicate/reordered rejection, and "first sync always applied unconditionally".
    cs = ClientClockState()
    r1 = cs.receive(clock_seq=1, host_game_ticks=1_000_000, local_now_ticks=0)
    check("first-ever sync (seq 1) is applied unconditionally even with huge apparent drift", r1 is True)
    check("offset after first sync equals host_game_ticks - local_now_ticks", cs.offset == 1_000_000)

    r_dup = cs.receive(clock_seq=1, host_game_ticks=999_999_999, local_now_ticks=0)
    check("duplicate clock_seq (1, same as last applied) is REJECTED (not even considered)", r_dup is None)
    check("offset unchanged after a rejected duplicate", cs.offset == 1_000_000)

    # 5. Tolerance: a tiny drift (well under 2s in ticks) on a NEWER seq is accepted-but-not-reapplied.
    tiny_drift_ticks = TOLERANCE_TICKS // 4  # 0.5s equivalent, under the 2s tolerance
    r2 = cs.receive(clock_seq=2, host_game_ticks=1_000_000 + tiny_drift_ticks, local_now_ticks=0)
    check("newer seq (2) with drift UNDER tolerance is accepted (seq advances) but NOT re-applied", r2 is False)
    check("offset stays at the OLD value when under tolerance (no jitter)", cs.offset == 1_000_000)

    # 5b. A drift OVER tolerance on a newer seq forces re-application.
    big_drift_ticks = TOLERANCE_TICKS * 3  # 6s equivalent, over the 2s tolerance
    r3 = cs.receive(clock_seq=3, host_game_ticks=1_000_000 + big_drift_ticks, local_now_ticks=0)
    check("newer seq (3) with drift OVER tolerance IS re-applied", r3 is True)
    check("offset updates to the new value once tolerance is exceeded", cs.offset == 1_000_000 + big_drift_ticks)

    # 5c. DISCONTINUITY flag forces re-application even with drift under tolerance.
    r4 = cs.receive(clock_seq=4, host_game_ticks=cs.offset + tiny_drift_ticks, local_now_ticks=0,
                     flags=FLAG_DISCONTINUITY)
    check("DISCONTINUITY flag forces re-apply even though drift is under tolerance", r4 is True)

    # 5d. Reordered-but-not-duplicate (older seq arriving late) is still rejected.
    r5 = cs.receive(clock_seq=2, host_game_ticks=5_000_000, local_now_ticks=0)
    check("reordered older seq (2, after seq 4 already applied) is REJECTED", r5 is None)

    # 6. Payload-length / msg_type-collision validation. PCNetGameVillagerArrivalMsg is ALSO 16 bytes
    #    (see pc_net_game.c), so only the msg_type byte disambiguates -- exactly like the existing
    #    INTERACT_CONFIRM-vs-other-8-byte-messages precedent this dispatcher already relies on.
    def would_be_dispatched_as_clock_sync(b):
        return len(b) == 16 and len(b) > 0 and b[0] == MSG_TYPE_CLOCK_SYNC

    check("a 15-byte (truncated) buffer is NOT dispatched as CLOCK_SYNC", not would_be_dispatched_as_clock_sync(buf[:-1]))
    check("a 17-byte (oversized) buffer is NOT dispatched as CLOCK_SYNC", not would_be_dispatched_as_clock_sync(buf + b"\x00"))
    check("an empty buffer is NOT dispatched as CLOCK_SYNC", not would_be_dispatched_as_clock_sync(b""))
    villager_arrival_type = 18  # PC_NETGAME_MSG_VILLAGER_ARRIVAL -- also a 16-byte message
    check("a correctly-sized 16-byte buffer with VILLAGER_ARRIVAL's msg_type is NOT dispatched as CLOCK_SYNC",
          not would_be_dispatched_as_clock_sync(bytes([villager_arrival_type]) + buf[1:]))

    # 7. Reconnect: seq resets to 0 (so the very next sync is always applied), offset survives.
    cs2 = ClientClockState()
    cs2.receive(clock_seq=1, host_game_ticks=42_000_000, local_now_ticks=0)
    check("pre-reconnect offset was applied", cs2.offset == 42_000_000)
    cs2.reconnect()
    check("reconnect resets seq_applied to 0 (Rule 2)", cs2.seq_applied == 0)
    check("reconnect does NOT reset the offset (Rule 2: no visible backward clock jump while down)",
          cs2.offset == 42_000_000)
    r_post = cs2.receive(clock_seq=1, host_game_ticks=42_000_000, local_now_ticks=0)
    check("first post-reconnect sync (seq 1 again) is accepted despite seq having been seen before "
          "the reset", r_post is True)

    # 8. No direction-based guard: an over-tolerance BACKWARD correction on a newer seq is applied
    # just like a forward one -- this is the direct regression test for the reverted bug (a
    # backward-blocking guard was briefly added, then proven unnecessary/harmful and removed: see
    # this file's module docstring).
    cs3 = ClientClockState()
    cs3.receive(clock_seq=1, host_game_ticks=10_000_000, local_now_ticks=0)
    check("setup: first sync applied", cs3.offset == 10_000_000)

    # A correction (no DISCONTINUITY) that moves the clock BACKWARD by more than tolerance on a
    # strictly newer clock_seq now applies -- reliable transport + strict clock_seq monotonicity
    # already guarantee this can only be a genuine newer host state, never a stale/reordered replay.
    backward_ticks = TOLERANCE_TICKS * 5
    r6 = cs3.receive(clock_seq=2, host_game_ticks=10_000_000 - backward_ticks, local_now_ticks=0)
    check("backward correction without DISCONTINUITY on a newer seq IS applied (no guard blocks it)",
          r6 is True)
    check("offset moves backward to the new candidate value", cs3.offset == 10_000_000 - backward_ticks)
    check("seq_applied advances", cs3.seq_applied == 2)

    # A subsequent FORWARD correction (e.g. the next periodic broadcast) must still go through
    # normally -- forward and backward corrections are treated identically.
    r7 = cs3.receive(clock_seq=3, host_game_ticks=10_000_000 + TOLERANCE_TICKS * 5, local_now_ticks=0)
    check("a later forward correction is still applied normally after a backward one", r7 is True)
    check("offset advances forward correctly", cs3.offset == 10_000_000 + TOLERANCE_TICKS * 5)

    # A backward correction WITH DISCONTINUITY (a genuine host-side manual adjustment) is honored,
    # exactly as it always has been.
    cs4 = ClientClockState()
    cs4.receive(clock_seq=1, host_game_ticks=10_000_000, local_now_ticks=0)
    r8 = cs4.receive(clock_seq=2, host_game_ticks=10_000_000 - backward_ticks, local_now_ticks=0,
                      flags=FLAG_DISCONTINUITY)
    check("backward correction WITH DISCONTINUITY is honored", r8 is True)
    check("offset moves backward when DISCONTINUITY legitimately says so",
          cs4.offset == 10_000_000 - backward_ticks)

    # Midnight-rollover-shaped sequence: forward corrections straddling a day boundary, then a
    # delayed/stale-feeling correction that would appear to move backward relative to the *offset*
    # (not wall time) is ALSO applied now -- no special-casing of the boundary, and no direction-based
    # guard left to interact with it either way.
    cs5 = ClientClockState()
    midnight_base = 1_000_000_000
    cs5.receive(clock_seq=1, host_game_ticks=midnight_base, local_now_ticks=0)
    r9 = cs5.receive(clock_seq=2, host_game_ticks=midnight_base + TOLERANCE_TICKS * 10, local_now_ticks=0)
    check("forward correction straddling a simulated midnight boundary applies normally", r9 is True)
    r10 = cs5.receive(clock_seq=3, host_game_ticks=midnight_base + TOLERANCE_TICKS * 5, local_now_ticks=0)
    check("a later correction that is now BEHIND the already-applied offset near the boundary IS "
          "applied (no direction-based guard)", r10 is True)
    check("offset moves to the newer (less-forward) candidate value near the simulated boundary",
          cs5.offset == midnight_base + TOLERANCE_TICKS * 5)

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
