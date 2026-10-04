#ifndef PC_ADOPT_CLOCK_H
#define PC_ADOPT_CLOCK_H

/* Client record-adoption watchdog clock (header-only, libc-free, natively unit-testable: pc/tools/net_spike/test_adopt_clock.py).
 *
 * Why: the client sends its IDENTITY as soon as a save is loaded, i.e. while the normal title -> Start Game flow is still running
 * (SCENE_TITLE_DEMO -> SCENE_PLAYERSELECT_2, the train where the human picks a character -> SCENE_FG). The host's record push therefore arrives
 * long before the player can be adopted, and the whole startup sequence (fades, scene changes, an interactive character choice) is time the
 * adoption is legitimately blocked. A flat 10 s window from the push's arrival expired during it (ADOPT_FAILED -> the host dropped the peer
 * although the game was progressing normally towards the town).
 *
 * The watchdog still means "adoption is blocked by a STUCK condition for 10 s": time the game spends in its own scene lifecycle (a fade / wipe /
 * scene change in progress, no running GAME_PLAY between two games, or a pre-game scene: title demo / player select / start demo) is PAUSED time
 * and does not count towards the 10 s. A separate, much larger cap bounds the paused time itself (a scene lifecycle that never ends is still a
 * failure), so a stuck client cannot hold the link forever. */

#include <stdint.h>

typedef struct PCAdoptClock {
    uint32_t armed_ms;   /* when the push was staged */
    uint32_t last_ms;    /* the previous tick */
    uint32_t paused_ms;  /* accumulated time spent in scene lifecycle */
} PCAdoptClock;

static inline void pc_adopt_clock_arm(PCAdoptClock* c, uint32_t now) {
    c->armed_ms = now;
    c->last_ms = now;
    c->paused_ms = 0;
}

/* Call once per blocked adoption attempt: the time since the previous tick is attributed to the state observed now. */
static inline void pc_adopt_clock_tick(PCAdoptClock* c, uint32_t now, int lifecycle_paused) {
    uint32_t dt = (uint32_t)(now - c->last_ms);
    if (lifecycle_paused) {
        c->paused_ms += dt;
    }
    c->last_ms = now;
}

/* Milliseconds that count towards the stuck-condition window. */
static inline uint32_t pc_adopt_clock_active_ms(const PCAdoptClock* c, uint32_t now) {
    uint32_t total = (uint32_t)(now - c->armed_ms);
    return total > c->paused_ms ? total - c->paused_ms : 0u;
}

/* 1 = the adoption must be given up: blocked by a non-lifecycle condition for active_limit_ms, or the scene lifecycle itself exceeded paused_cap_ms. */
static inline int pc_adopt_clock_expired(const PCAdoptClock* c, uint32_t now, uint32_t active_limit_ms, uint32_t paused_cap_ms) {
    return pc_adopt_clock_active_ms(c, now) >= active_limit_ms || c->paused_ms >= paused_cap_ms;
}

#endif
