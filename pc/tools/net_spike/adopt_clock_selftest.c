/* adopt_clock_selftest.c - native unit test of pc/include/pc_adopt_clock.h (the client record-adoption watchdog clock). No game, no network. */
#include <stdio.h>
#include <stdint.h>
#include "pc_adopt_clock.h"

static int s_pass = 0, s_fail = 0;
static void check(const char* what, int cond) {
    printf("%s: %s\n", cond ? "PASS" : "FAIL", what);
    if (cond) {
        s_pass++;
    } else {
        s_fail++;
    }
}

#define LIMIT 10000u
#define CAP 300000u

/* poll every 16 ms from `from` until `to` (exclusive), the state at each poll given by f(t); returns the first t at which the clock expired, or 0 */
typedef int (*life_fn)(uint32_t t);
static uint32_t run(PCAdoptClock* c, uint32_t base, uint32_t from, uint32_t to, life_fn f) {
    uint32_t t;
    for (t = from; t < to; t += 16) {
        pc_adopt_clock_tick(c, base + t, f(t));
        if (pc_adopt_clock_expired(c, base + t, LIMIT, CAP)) {
            return t ? t : 1;
        }
    }
    return 0;
}

static int always_life(uint32_t t) {
    (void)t;
    return 1;
}
static int never_life(uint32_t t) {
    (void)t;
    return 0;
}
/* the reported real timeline: the push arrives during the title -> player-select fade (scene lifecycle), the human chooses a character in the
 * player-select train (a pre-game scene, ~24 s), the scene change to the town runs, then the player is in the town and adoption is possible */
static int start_flow(uint32_t t) {
    return t < 26000u;
}
static int six_then_stuck(uint32_t t) {
    return t < 6000u;
}

int main(void) {
    PCAdoptClock c;
    uint32_t r;

    /* A: the reported failure: a lifecycle blocker for 26 s must NOT expire the watchdog (a flat 10 s window would have) */
    pc_adopt_clock_arm(&c, 1000u);
    r = run(&c, 1000u, 16, 26000u, start_flow);
    check("A the Start Game flow (26 s of scene lifecycle) does not expire the watchdog", r == 0);
    check("A the OLD flat 10 s window would have expired (total elapsed >= 10 s)", (uint32_t)(1000u + 25984u - c.armed_ms) >= LIMIT);
    check("A active time stays ~0 while the game is in its scene lifecycle", pc_adopt_clock_active_ms(&c, 1000u + 25984u) < 100u);

    /* B: a genuinely stuck non-lifecycle blocker still fails after 10 s (not earlier) */
    pc_adopt_clock_arm(&c, 5000u);
    r = run(&c, 5000u, 16, 30000u, never_life);
    check("B a stuck (non-lifecycle) blocker expires after the 10 s window", r >= 9984u && r <= 10032u);

    /* C: mixed: 6 s lifecycle then stuck: expires 10 s after the lifecycle ended (t ~ 16 s), not at 10 s */
    pc_adopt_clock_arm(&c, 0u);
    r = run(&c, 0u, 16, 40000u, six_then_stuck);
    check("C 6 s of lifecycle then a stuck blocker: expires ~10 s after the lifecycle ended", r >= 15984u && r <= 16064u);

    /* D: a scene lifecycle that never ends is still bounded by its own cap */
    pc_adopt_clock_arm(&c, 0u);
    r = run(&c, 0u, 16, 400000u, always_life);
    check("D an endless scene lifecycle expires at the lifecycle cap", r >= CAP - 16u && r <= CAP + 16u);

    /* E: 32-bit time wrap-around */
    pc_adopt_clock_arm(&c, 0xFFFFF000u);
    r = run(&c, 0xFFFFF000u, 16, 30000u, never_life);
    check("E uint32 wrap-around: a stuck blocker still expires after ~10 s", r >= 9984u && r <= 10032u);
    pc_adopt_clock_arm(&c, 0xFFFFF000u);
    r = run(&c, 0xFFFFF000u, 16, 26000u, start_flow);
    check("E uint32 wrap-around: the lifecycle is still not counted", r == 0);

    /* F: a long gap between two ticks (a frame hitch) is attributed to the state observed after it, never to both */
    pc_adopt_clock_arm(&c, 0u);
    pc_adopt_clock_tick(&c, 20000u, 1);
    check("F a 20 s gap observed in the lifecycle counts as paused (not expired)", !pc_adopt_clock_expired(&c, 20000u, LIMIT, CAP) && c.paused_ms == 20000u);
    pc_adopt_clock_arm(&c, 0u);
    pc_adopt_clock_tick(&c, 20000u, 0);
    check("F a 20 s gap observed in a stuck state counts as active (expired)", pc_adopt_clock_expired(&c, 20000u, LIMIT, CAP));

    /* G: re-arming (a new push) starts a clean history */
    pc_adopt_clock_arm(&c, 90000u);
    check("G re-arm: zero active / paused time", pc_adopt_clock_active_ms(&c, 90000u) == 0 && c.paused_ms == 0);

    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail ? 1 : 0;
}
