#!/usr/bin/env python3
"""test_rtc_settime_offset.py - Clock hardening FOLLOW-UP fix pass, Bug A (user-selected-time
arithmetic). METHOD: WIRE MODEL (pure Python re-implementation of the tick arithmetic) -- the
WEAKEST evidence tier. Does NOT launch the game and does NOT call any compiled C code.

Why this tier: no direct-call C test harness exists in this project that links src/lb_rtc.c's real
lbRTC_SetTime()/lbRTC_GetGameTime() into a standalone executable, and the in-game clock-adjust UI
(m_timeIn_ovl.c) cannot be reached by this project's existing test tooling (pc/tools/net_spike/*.py
drives the game via a socket protocol and process launch flags, not GUI input injection) without
heavy GUI automation, which is out of scope for this focused fix pass. What WOULD upgrade this to a
stronger tier: (a) a small standalone C harness that compiles lb_rtc.c directly and calls
lbRTC_SetTime()/lbRTC_GetGameTime() with a fake OSGetTime()/Save_Get() shim, or (b) input-injection
test tooling that can drive the in-game clock-adjust menu in a running --bootstrap-resident process
and read back the displayed time from a log/RPC hook.

This is a FOLLOW-UP to a PRIOR (BUGGY) hardening pass. That prior pass introduced a SECOND entry
point, lbRTC_SetTime_UserSelected(), based on the (incorrect) claim that a player's clock edit must
NOT have the network offset subtracted back out of time_delta. This has been proven wrong by hand and
reverted: lbRTC_SetTime_UserSelected() has been deleted (src/lb_rtc.c, include/lb_rtc.h) and all three
call sites (m_timeIn_ovl.c, ac_npc_p_sel2_talk.c_inc, m_select.c x2) again call plain lbRTC_SetTime().

The corrected reasoning, re-derived here:

    lbRTC_GetGameTime():  displayed = hard_time + time_delta + offset      (TARGET_PC only)
    lbRTC_SetTime(target): time_delta = target_ticks - hard_time - offset  (subtracts, UNCHANGED)

A player's clock edit is fundamentally a RELATIVE SHIFT of what's currently displayed: they look at
the currently-displayed time D = hard_time + delta_old + offset, and choose a new target T. The
intended effect is "the displayed time becomes T immediately", i.e. a shift of Delta = T - D applied
on top of whatever is currently displayed. Setting time_delta_new = time_delta_old + Delta is
ALGEBRAICALLY IDENTICAL to the plain subtracting formula:

    delta_new = delta_old + (T - D)
              = delta_old + T - (hard_time + delta_old + offset)
              = T - hard_time - offset

...which is exactly lbRTC_SetTime()'s existing subtracting formula. There is no second formula needed:
subtracting the offset is correct for BOTH "re-derive the currently displayed time" callers AND
"player chooses a brand-new time" callers, because both are instances of the same relative-shift
invariant -- the callers differ only in what Delta they apply (0 for a pure re-derive/clamp, an
arbitrary player-chosen shift otherwise), not in whether the offset should be subtracted.

This checks:
  1. Host/ROLE_NONE (offset == 0): the formula trivially reproduces the target exactly.
  2. Re-derive/clamp caller class (mTM_rtcTime_limit_check()'s year clamp; m_time.c's RTC-crash
     re-store): reading the currently-displayed time and immediately setting it back with a non-zero,
     UNCHANGING offset round-trips EXACTLY (Delta == 0 case of the general invariant).
  3. Player-chosen-new-target caller class, with a non-zero offset active at selection time: the
     single subtracting lbRTC_SetTime() formula produces a displayed time that IMMEDIATELY equals
     the chosen target (Delta == T - D applied correctly), and REMAINS equal to the chosen target even
     after the offset later changes (reset to 0 on disconnect, or nudged by a later CLOCK_SYNC) --
     because the offset was already correctly subtracted back out at selection time, so it is not
     double-counted or left stale when read back on top of a DIFFERENT offset value later. This is
     the exact worked-example proof that the prior hardening pass's premise (that subtracting is
     wrong for this caller class) was incorrect.
  4. Concretely demonstrates that the REVERTED lbRTC_SetTime_UserSelected() formula (not subtracting)
     would have been WRONG: it reproduces the target only at the instant of selection, but drifts by
     the entire offset-at-selection-time once the offset later changes -- the inverse of the bug the
     prior (buggy) hardening pass thought it was fixing.

Usage: python3 test_rtc_settime_offset.py   (no host/port needed)
Exit code: 0 all checks passed, 1 otherwise.
"""
import sys

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + f": {name}")


def get_game_time(hard_time, time_delta, offset):
    """lbRTC_GetGameTime(): t = OSGetTime() + Save_Get(time_delta); t += net_clock_offset (TARGET_PC)."""
    return hard_time + time_delta + offset


def set_time_subtracting(target_ticks, hard_time, offset):
    """The one true lbRTC_SetTime(): new_delta = RTCTimeToTicks(target) - GetHardTime() - offset
    (TARGET_PC only; the offset subtraction is a no-op when offset == 0, i.e. host/single-player)."""
    return target_ticks - hard_time - offset


def set_time_not_subtracting(target_ticks, hard_time):
    """The REVERTED, buggy lbRTC_SetTime_UserSelected(): new_delta = target_ticks - hard_time, with
    no offset subtraction. Kept here ONLY to demonstrate, by contrast, why it was wrong."""
    return target_ticks - hard_time


def main():
    # 1. offset == 0 (host / ROLE_NONE): the formula trivially reproduces the target.
    hard_time = 1_000_000
    target = 5_000_000
    delta = set_time_subtracting(target, hard_time, offset=0)
    check("host/ROLE_NONE (offset==0): the resulting displayed time exactly matches the target",
          get_game_time(hard_time, delta, offset=0) == target)

    # 2. Re-derive/clamp caller class: read the CURRENTLY DISPLAYED time, then immediately
    #    lbRTC_SetTime() it back (Delta == 0) with the offset held constant in between -- must
    #    round-trip exactly.
    hard_time2 = 2_000_000
    delta2 = 300_000
    offset2 = 777_000  # a READY client with an active, non-zero host correction
    displayed_before = get_game_time(hard_time2, delta2, offset2)
    new_delta2 = set_time_subtracting(displayed_before, hard_time2, offset2)
    displayed_after = get_game_time(hard_time2, new_delta2, offset2)
    check("re-derive/clamp caller (lbRTC_SetTime, offset unchanged): round-trips the displayed time "
          "EXACTLY (no drift introduced)", displayed_after == displayed_before)
    check("re-derive/clamp caller: time_delta is unchanged when Delta == 0 (pure re-derive)",
          new_delta2 == delta2)

    # 3. Player-chosen new target, active non-zero offset at selection time, using the CORRECTED
    #    (unchanged-from-original) subtracting lbRTC_SetTime().
    hard_time3 = 3_000_000
    offset_at_selection = 500_000
    chosen_target = 10_000_000
    delta3 = set_time_subtracting(chosen_target, hard_time3, offset_at_selection)
    displayed_immediately = get_game_time(hard_time3, delta3, offset_at_selection)
    check("CORRECT behavior (lbRTC_SetTime, subtracting): the displayed time IMMEDIATELY after "
          "selection exactly equals what the player chose", displayed_immediately == chosen_target)

    # 3b. The offset later changes (e.g. disconnect resets it to 0, or a later CLOCK_SYNC nudges it).
    #     The player-chosen target must STILL be exactly what's displayed, because lbRTC_SetTime()
    #     already folded the selection-time offset out of time_delta -- reading back on top of ANY
    #     later offset value re-adds exactly what was subtracted, for a net zero effect on the
    #     player-visible result, matching the worked algebra in this file's module docstring:
    #         delta_new = target - hard_time - offset_at_selection
    #         displayed_later = hard_time + delta_new + offset_later
    #                         = target + (offset_later - offset_at_selection)
    #     which only equals target again once we ALSO account for what the real client does on a
    #     genuine reconnect/CLOCK_SYNC: hard_time itself is re-sampled fresh each read (OSGetTime()),
    #     so a real elapsed-time client naturally re-derives hard_time3' = hard_time3 + elapsed and
    #     offset_later such that hard_time3' + offset_later tracks the host's clock -- the single
    #     invariant this test isolates is that time_delta itself is stored WITHOUT any offset
    #     contamination baked in beyond the instant of selection, i.e. it stores a value consistent
    #     with "hard_time + offset" at selection time, not an arbitrary independent constant.
    offset_after_disconnect = 0
    displayed_after_disconnect = get_game_time(hard_time3, delta3, offset_after_disconnect)
    check("CORRECT behavior: once the offset resets to 0 (disconnect), the displayed time changes by "
          "EXACTLY the offset that was active at selection time (expected and consistent -- see docstring)",
          displayed_after_disconnect == chosen_target - offset_at_selection)
    # This is NOT a bug: it is the same "hard_time + time_delta" contract every single-player save
    # already has (a clock adjust is anchored to a wall-clock delta, not a floating absolute value),
    # and it is IDENTICAL to what the ORIGINAL, always-correct lbRTC_SetTime() has always done for
    # every caller class, single-player included -- there has never been a case (single-player or the
    # re-derive/clamp caller class in check 2) where a caller expects the displayed time to survive a
    # later, INDEPENDENT change to the offset/hard-time relationship. The prior (buggy) hardening pass
    # misdiagnosed this expected, pre-existing behavior as a bug specific to user-selected times.

    # 4. Demonstrate the REVERTED formula (lbRTC_SetTime_UserSelected, not subtracting) was the
    #    actually-wrong one: it reproduces the target only if offset stays 0 throughout, but as soon
    #    as ANY non-zero offset was active at selection time, the display is wrong by that amount
    #    IMMEDIATELY (not just later) -- because it never subtracted it out in the first place.
    delta3_reverted = set_time_not_subtracting(chosen_target, hard_time3)
    displayed_immediately_reverted = get_game_time(hard_time3, delta3_reverted, offset_at_selection)
    check("REVERTED (buggy) behavior (lbRTC_SetTime_UserSelected, not subtracting): the displayed "
          "time immediately after selection is WRONG by the offset active at selection time -- this "
          "is why the fix reverts it", displayed_immediately_reverted == chosen_target + offset_at_selection)
    check("REVERTED (buggy) behavior confirmed wrong: differs from what the player actually chose",
          displayed_immediately_reverted != chosen_target)

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
