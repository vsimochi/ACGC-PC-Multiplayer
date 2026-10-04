/* guest_arrival_selftest.c - native unit test of pc/include/pc_remote_arrival_logic.h (T4 remote-arrival train guard + per-puppet latch,
 * T3 DEMO_WALK mapping). No game, no network. Compiled against the real header the game uses. */
#include <stdio.h>
#include "pc_remote_arrival_logic.h"

static int s_pass = 0, s_fail = 0;
static void check(const char* what, int cond) {
    printf("%s: %s\n", cond ? "PASS" : "FAIL", what);
    if (cond) {
        s_pass++;
    } else {
        s_fail++;
    }
}

/* the all-clear baseline: puppet in the local town, local town scene, player present, no title demo / pre-game / local demo,
 * no request, no train */
static int decide(int latched, int pscene, int lscene, int player, int title, int pregame, int ldemo, unsigned coming, unsigned action) {
    return pcarr_remote_arrival_train_decide(latched, pscene, lscene, player, title, pregame, ldemo, coming, action);
}

int main(void) {
    int latch;
    int n;
    int i;
    unsigned a;

    /* ---- T4 guard: every branch ---- */
    check("T4 all clear -> OK (call the arrival train)", decide(0, 1, 1, 1, 0, 0, 0, 0u, 0u) == PCARR_REMOTE_TRAIN_OK);
    check("T4 already latched -> LATCHED", decide(1, 1, 1, 1, 0, 0, 0, 0u, 0u) == PCARR_REMOTE_TRAIN_LATCHED);
    check("T4 puppet not in the local town -> PUPPET_SCENE", decide(0, 0, 1, 1, 0, 0, 0, 0u, 0u) == PCARR_REMOTE_TRAIN_PUPPET_SCENE);
    check("T4 this process not in the town scene -> LOCAL_SCENE", decide(0, 1, 0, 1, 0, 0, 0, 0u, 0u) == PCARR_REMOTE_TRAIN_LOCAL_SCENE);
    check("T4 no local player actor -> NO_PLAYER", decide(0, 1, 1, 0, 0, 0, 0, 0u, 0u) == PCARR_REMOTE_TRAIN_NO_PLAYER);
    check("T4 title demo running -> TITLE", decide(0, 1, 1, 1, 1, 0, 0, 0u, 0u) == PCARR_REMOTE_TRAIN_TITLE);
    check("T4 player-select / train interior draw type -> PRE_GAME", decide(0, 1, 1, 1, 0, 1, 0, 0u, 0u) == PCARR_REMOTE_TRAIN_PRE_GAME);
    check("T4 local player arriving / boarding (train main index or ride-off demo actor) -> LOCAL_DEMO", decide(0, 1, 1, 1, 0, 0, 1, 0u, 0u) == PCARR_REMOTE_TRAIN_LOCAL_DEMO);
    check("T4 coming_flag 2 (station master leave request pending) -> COMING_FLAG", decide(0, 1, 1, 1, 0, 0, 0, 2u, 0u) == PCARR_REMOTE_TRAIN_COMING_FLAG);
    check("T4 coming_flag 3 (an arrival already requested) -> COMING_FLAG", decide(0, 1, 1, 1, 0, 0, 0, 3u, 0u) == PCARR_REMOTE_TRAIN_COMING_FLAG);
    check("T4 coming_flag 4 -> COMING_FLAG", decide(0, 1, 1, 1, 0, 0, 0, 4u, 0u) == PCARR_REMOTE_TRAIN_COMING_FLAG);

    n = 0;
    for (a = 1u; a <= 8u; a++) {
        n += decide(0, 1, 1, 1, 0, 0, 0, 0u, a) == PCARR_REMOTE_TRAIN_BUSY;
    }
    check("T4 every non-NONE train_action 1..8 (spawn, slowdown, stop, signal, wait, starting, pull out, speed up) -> BUSY", n == 8);
    check("T4 a hidden-title parked train (action 5) is also BUSY (never overwritten)", decide(0, 1, 1, 1, 0, 0, 0, 0u, 5u) == PCARR_REMOTE_TRAIN_BUSY);

    /* evaluation order = enum order: the first failing guard is reported */
    check("T4 order: latched wins over everything", decide(1, 0, 0, 0, 1, 1, 1, 3u, 5u) == PCARR_REMOTE_TRAIN_LATCHED);
    check("T4 order: puppet scene before local scene", decide(0, 0, 0, 0, 1, 1, 1, 3u, 5u) == PCARR_REMOTE_TRAIN_PUPPET_SCENE);
    check("T4 order: busy last", decide(0, 1, 1, 1, 0, 0, 0, 0u, 5u) == PCARR_REMOTE_TRAIN_BUSY && decide(0, 1, 1, 1, 0, 0, 0, 2u, 5u) == PCARR_REMOTE_TRAIN_COMING_FLAG);
    check("T4 only the all-clear input yields OK (every single-guard failure refuses)",
          decide(0, 1, 1, 1, 0, 0, 0, 0u, 0u) == 0 && decide(1, 1, 1, 1, 0, 0, 0, 0u, 0u) != 0 && decide(0, 0, 1, 1, 0, 0, 0, 0u, 0u) != 0 &&
              decide(0, 1, 0, 1, 0, 0, 0, 0u, 0u) != 0 && decide(0, 1, 1, 0, 0, 0, 0, 0u, 0u) != 0 && decide(0, 1, 1, 1, 1, 0, 0, 0u, 0u) != 0 &&
              decide(0, 1, 1, 1, 0, 1, 0, 0u, 0u) != 0 && decide(0, 1, 1, 1, 0, 0, 1, 0u, 0u) != 0 && decide(0, 1, 1, 1, 0, 0, 0, 1u, 0u) != 0 &&
              decide(0, 1, 1, 1, 0, 0, 0, 0u, 1u) != 0);

    /* ---- T4 latch: once per contiguous standing period ---- */
    latch = 0;
    check("T4 latch: not standing -> never evaluates", pcarr_remote_standing_should_evaluate(&latch, 0, 1) == 0 && latch == 0);
    check("T4 latch: first standing frame in a known town scene evaluates", pcarr_remote_standing_should_evaluate(&latch, 1, 1) == 1 && latch == 1);
    n = 0;
    for (i = 0; i < 1000; i++) {
        n += pcarr_remote_standing_should_evaluate(&latch, 1, 1);
    }
    check("T4 latch: 1000 further standing frames evaluate 0 more times (at most once per arrival)", n == 0);
    check("T4 latch: leaving the state (GETOFF / walk) clears the latch", pcarr_remote_standing_should_evaluate(&latch, 0, 1) == 0 && latch == 0);
    check("T4 latch: a NEW standing period of the same puppet (second arrival) evaluates again", pcarr_remote_standing_should_evaluate(&latch, 1, 1) == 1);

    latch = 0;
    check("T4 latch: standing but the puppet scene is not known yet -> not consumed", pcarr_remote_standing_should_evaluate(&latch, 1, 0) == 0 && latch == 0);
    check("T4 latch: the scene becomes the local town while still standing -> evaluates once", pcarr_remote_standing_should_evaluate(&latch, 1, 1) == 1 && pcarr_remote_standing_should_evaluate(&latch, 1, 1) == 0);
    latch = 0;
    n = 0;
    for (i = 0; i < 5; i++) {
        n += pcarr_remote_standing_should_evaluate(&latch, 1, 1);
        n += pcarr_remote_standing_should_evaluate(&latch, 1, 1);
        pcarr_remote_standing_should_evaluate(&latch, 0, 1);
    }
    check("T4 latch: five arrivals with duplicated frames evaluate exactly five times", n == 5);

    /* a full sequence through the guard: first standing frame calls the train, the rest and a refused retry do not */
    latch = 0;
    n = 0;
    for (i = 0; i < 100; i++) {
        if (pcarr_remote_standing_should_evaluate(&latch, 1, 1)) {
            n += decide(0, 1, 1, 1, 0, 0, 0, 0u, 0u) == PCARR_REMOTE_TRAIN_OK;
        }
    }
    check("T4 sequence: 100 standing frames call the train exactly once", n == 1);
    latch = 0;
    n = 0;
    for (i = 0; i < 100; i++) {
        if (pcarr_remote_standing_should_evaluate(&latch, 1, 1)) {
            n += decide(0, 1, 1, 1, 0, 0, 0, 0u, 5u) == PCARR_REMOTE_TRAIN_OK; /* own hourly train is running: refused, and not retried later */
        }
    }
    check("T4 sequence: with an own train running no call is made, and the refusal is not retried every frame", n == 0 && latch == 1);

    /* ---- T3 DEMO_WALK mapping ---- */
    check("T3 DEMO_WALK (index 17) with the sender's OTHER class plays walk", pcarr_demo_walk_plays_walk(1, 17, 17, 1) == 1);
    check("T3 a different state is untouched", pcarr_demo_walk_plays_walk(1, 18, 17, 1) == 0);
    check("T3 no action info (v6 sender) is untouched", pcarr_demo_walk_plays_walk(0, 17, 17, 1) == 0);
    check("T3 a sender that already classified it (not OTHER) is untouched", pcarr_demo_walk_plays_walk(1, 17, 17, 0) == 0);

    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail ? 1 : 0;
}
