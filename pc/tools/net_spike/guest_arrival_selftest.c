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
    /* a structural refusal (reasons 1-6) consumes the latch; a transient one (7 LOCAL_DEMO, 8 COMING_FLAG, 9 BUSY) does not */
    for (i = 1; i <= 9; i++) {
        latch = 0;
        pcarr_remote_standing_should_evaluate(&latch, 1, 1);
        pcarr_remote_standing_settle(&latch, i);
        check("T4 latch: reason 1-6 keeps the latch, 7-9 release it", (i <= 6) == (latch == 1) && pcarr_remote_reason_is_transient(i) == (i >= 7));
    }
    latch = 0;
    pcarr_remote_standing_should_evaluate(&latch, 1, 1);
    pcarr_remote_standing_settle(&latch, PCARR_REMOTE_TRAIN_OK);
    check("T4 latch: OK keeps the latch", latch == 1);

    /* transient: the resident is in the first-job intro for 50 frames (LOCAL_DEMO), then the intro ends -> the train is called exactly once */
    latch = 0;
    n = 0;
    {
        int calls = 0;
        for (i = 0; i < 200; i++) {
            int local_demo = pcarr_local_intro_arriving(1, i < 50); /* INTRO_DEMO actor lives on; FirstIntro ends at frame 50 */
            if (pcarr_remote_standing_should_evaluate(&latch, 1, 1)) {
                int why = decide(0, 1, 1, 1, 0, 0, local_demo, 0u, 0u);
                calls++;
                pcarr_remote_standing_settle(&latch, why);
                n += why == PCARR_REMOTE_TRAIN_OK;
            }
        }
        check("T4 transient: LOCAL_DEMO re-polled every frame (50 refused + 1 OK), then latched", n == 1 && calls == 51 && latch == 1);
    }
    check("T4 intro actor alone (first intro finished) is NOT a local arrival", pcarr_local_intro_arriving(1, 0) == 0 && pcarr_local_intro_arriving(0, 1) == 0 &&
                                                                                    pcarr_local_intro_arriving(1, 1) == 1);

    /* transient BUSY (own hourly train, action 5) then idle: called once when it clears */
    latch = 0;
    n = 0;
    for (i = 0; i < 100; i++) {
        if (pcarr_remote_standing_should_evaluate(&latch, 1, 1)) {
            int why = decide(0, 1, 1, 1, 0, 0, 0, 0u, i < 40 ? 5u : 0u);
            pcarr_remote_standing_settle(&latch, why);
            n += why == PCARR_REMOTE_TRAIN_OK;
        }
    }
    check("T4 transient: BUSY then free -> the train is called exactly once", n == 1 && latch == 1);

    /* latching: a structural refusal (not in the town scene) is never retried every frame */
    latch = 0;
    n = 0;
    for (i = 0; i < 100; i++) {
        if (pcarr_remote_standing_should_evaluate(&latch, 1, 1)) {
            n++;
            pcarr_remote_standing_settle(&latch, decide(0, 1, 0, 1, 0, 0, 0, 0u, 0u));
        }
    }
    check("T4 latching: LOCAL_SCENE refusal evaluated once, not retried", n == 1 && latch == 1);

    /* a transient refusal that never clears does not outlive the standing period: leaving the state clears the latch */
    latch = 0;
    pcarr_remote_standing_should_evaluate(&latch, 1, 1);
    pcarr_remote_standing_settle(&latch, PCARR_REMOTE_TRAIN_BUSY);
    pcarr_remote_standing_should_evaluate(&latch, 0, 1);
    check("T4 transient: leaving the state after only refusals leaves the latch clear", latch == 0);

    /* ---- T3 DEMO_WALK mapping ---- */
    check("T3 DEMO_WALK (index 17) with the sender's OTHER class plays walk", pcarr_demo_walk_plays_walk(1, 17, 17, 1) == 1);
    check("T3 a different state is untouched", pcarr_demo_walk_plays_walk(1, 18, 17, 1) == 0);
    check("T3 no action info (v6 sender) is untouched", pcarr_demo_walk_plays_walk(0, 17, 17, 1) == 0);
    check("T3 a sender that already classified it (not OTHER) is untouched", pcarr_demo_walk_plays_walk(1, 17, 17, 0) == 0);

    /* ---- SHARED train arrival: what the other players are doing decides how a joining guest starts (main indexes: STANDING_TRAIN 79, GETOFF_TRAIN 78, DEMO_WALK 75, TALK 65, DEMO_WAIT 74) ---- */
#define JC(valid, idx, x, z) pcarr_join_class((valid), (idx), 79, 78, 75, 65, 74, (x), (z))
    check("J class: standing in the arrival train is RIDING", JC(1, 79, 2000.0f, 760.0f) == PCARR_JOIN_RIDING);
    check("J class: getting off the train / the walk from the station are STOPPED (the train is at the station)", JC(1, 78, 2180.0f, 820.0f) == PCARR_JOIN_STOPPED && JC(1, 75, 2210.0f, 830.0f) == PCARR_JOIN_STOPPED);
    check("J class: Porter's welcome (TALK) and the DEMO_WAIT gaps ON THE STATION PLATFORM are STOPPED (joined mid-welcome)", JC(1, 65, 2200.0f, 820.0f) == PCARR_JOIN_STOPPED && JC(1, 74, 2190.0f, 815.0f) == PCARR_JOIN_STOPPED);
    check("J class: a TALK / DEMO_WAIT elsewhere (an ordinary conversation, another guest parked at the ride-off start) is NOT an arrival", JC(1, 65, 1400.0f, 1400.0f) == PCARR_JOIN_NONE && JC(1, 74, 1970.0f, 760.0f) == PCARR_JOIN_NONE);
    check("J class: ordinary field movement, boarding the train (a departure) and an old sender (no action info) are NOT arrivals",
          JC(1, 7, 2200.0f, 820.0f) == PCARR_JOIN_NONE && JC(1, 3, 2200.0f, 820.0f) == PCARR_JOIN_NONE && JC(1, 76, 2200.0f, 820.0f) == PCARR_JOIN_NONE && JC(0, 79, 2000.0f, 760.0f) == PCARR_JOIN_NONE);
#undef JC
    check("J train x: the engine runs 190 ahead of a passenger (caboose = train - 250, passenger = caboose + 60)", pcarr_join_train_x(1911.0f) > 2100.9f && pcarr_join_train_x(1911.0f) < 2101.1f);
    check("J train x: clamped to the approach (never before the vanilla start 2037, never past 2160 so the slowdown / stop still ends at the station)",
          pcarr_join_train_x(1500.0f) == PCARR_TRAIN_START_X && pcarr_join_train_x(2175.0f) == PCARR_TRAIN_RIDE_MAX_X);
    check("J decide: the handshake is not complete -> not decidable yet", pcarr_join_decide(1, 0, 0, 0, 0, 0) == -1);
    check("J decide: not a connected client (single-player / host / refused) -> the vanilla arrival at once", pcarr_join_decide(0, 0, 1, 3, 2, 2) == PCARR_JOIN_NONE);
    check("J decide: READY but the host's roster / first MOVEs are not in yet -> not decidable yet (bounded by the existing silence gap)", pcarr_join_decide(0, 1, 1, 0, 0, 0) == -1 && pcarr_join_decide(0, 1, 0, 1, 0, 0) == -1);
    check("J decide: nobody is arriving -> the normal arrival", pcarr_join_decide(0, 1, 0, 0, 0, 0) == PCARR_JOIN_NONE);
    check("J decide: another passenger riding -> JOIN riding; someone at the station -> JOIN stopped (wins over a riding one); never a wait for the arrival to end",
          pcarr_join_decide(0, 1, 0, 0, 0, 1) == PCARR_JOIN_RIDING && pcarr_join_decide(0, 1, 0, 0, 1, 0) == PCARR_JOIN_STOPPED && pcarr_join_decide(0, 1, 0, 0, 2, 1) == PCARR_JOIN_STOPPED);

    /* ---- hotfix 0.2.1: the arrival hold is bounded ---- */
    check("H thresholds: unknown window 3000 ms, 4500 ms after READY, 15000 ms in total, trace every 2000 ms",
          PCARR_JOIN_UNKNOWN_WINDOW_MS == 3000u && PCARR_JOIN_HOLD_READY_MS == 4500u && PCARR_JOIN_HOLD_TOTAL_MS == 15000u && PCARR_JOIN_HOLD_TRACE_MS == 2000u);
    check("H unknown slot counts before READY and during the first 3 s after it, never later",
          pcarr_join_unknown_counts(0, 0u) && pcarr_join_unknown_counts(0, 99999u) && pcarr_join_unknown_counts(1, 0u) && pcarr_join_unknown_counts(1, 2999u) &&
          !pcarr_join_unknown_counts(1, 3000u) && !pcarr_join_unknown_counts(1, 60000u));
    check("H no timeout inside both limits (handshake 14.9 s, READY for 4.4 s)",
          pcarr_join_hold_timeout(14999u, 0, 0u) == PCARR_HOLD_KEEP && pcarr_join_hold_timeout(10000u, 1, 4499u) == PCARR_HOLD_KEEP && pcarr_join_hold_timeout(0u, 1, 0u) == PCARR_HOLD_KEEP);
    check("H READY limit: 4.5 s after READY ends the hold (even early in the total)",
          pcarr_join_hold_timeout(4500u, 1, 4500u) == PCARR_HOLD_TIMEOUT_READY && pcarr_join_hold_timeout(20000u, 1, 4600u) == PCARR_HOLD_TIMEOUT_READY);
    check("H total limit: 15 s held in total ends it whatever the link (handshake, or READY only recently)",
          pcarr_join_hold_timeout(15000u, 0, 0u) == PCARR_HOLD_TIMEOUT_TOTAL && pcarr_join_hold_timeout(15000u, 1, 1000u) == PCARR_HOLD_TIMEOUT_TOTAL);
    check("H bounded decide: a decided answer is never changed by the limits",
          pcarr_join_decide_bounded(PCARR_JOIN_NONE, PCARR_HOLD_TIMEOUT_TOTAL, 3, 3) == PCARR_JOIN_NONE &&
          pcarr_join_decide_bounded(PCARR_JOIN_RIDING, PCARR_HOLD_KEEP, 0, 0) == PCARR_JOIN_RIDING && pcarr_join_decide_bounded(PCARR_JOIN_STOPPED, PCARR_HOLD_KEEP, 0, 0) == PCARR_JOIN_STOPPED);
    check("H bounded decide: undecided within the limits keeps holding (-1)", pcarr_join_decide_bounded(-1, PCARR_HOLD_KEEP, 1, 1) == -1);
    check("H bounded decide: a timed-out hold starts the arrival with what is known (nobody -> NONE, a stopped one -> STOPPED, a riding one -> RIDING)",
          pcarr_join_decide_bounded(-1, PCARR_HOLD_TIMEOUT_READY, 0, 0) == PCARR_JOIN_NONE && pcarr_join_decide_bounded(-1, PCARR_HOLD_TIMEOUT_TOTAL, 1, 1) == PCARR_JOIN_STOPPED &&
          pcarr_join_decide_bounded(-1, PCARR_HOLD_TIMEOUT_READY, 0, 2) == PCARR_JOIN_RIDING);
    {
        /* the field case: link READY, the world snapshot never completes (roster_pending stays 1), one other player whose MOVE never arrives. Simulated 16 ms polls:
         * the hold must end at 4.5 s after READY (READY reached 0.8 s into the hold), never earlier than the roster would have allowed, and exactly once. */
        unsigned t, ready_at = 800u, released_at = 0u;
        int released = 0, holds_after = 0;
        for (t = 0u; t <= 20000u; t += 16u) {
            const int ready = t >= ready_at;
            const unsigned since_ready = ready ? t - ready_at : 0u;
            const int unknown = pcarr_join_unknown_counts(ready, since_ready) ? 1 : 0;
            const int d = pcarr_join_decide(!ready, ready, ready, unknown, 0, 0);
            const int b = pcarr_join_decide_bounded(d, d < 0 ? pcarr_join_hold_timeout(t, ready, since_ready) : PCARR_HOLD_KEEP, 0, 0);
            if (b >= 0 && !released) {
                released = 1;
                released_at = t;
            } else if (b < 0 && released) {
                holds_after++;
            }
        }
        check("H sim: a stalled snapshot (roster never known) releases the parked guest 4.5 s after READY with the normal arrival", released && released_at >= ready_at + 4500u &&
              released_at < ready_at + 4500u + 16u && holds_after == 0);
    }
    {
        /* the intended behaviour is unchanged: the roster arrives 1.2 s after READY and the other player's first MOVE 0.4 s later (riding): decided then, as before */
        unsigned t, ready_at = 300u, released_at = 0u;
        int released = 0, cls = -1;
        for (t = 0u; t <= 20000u && !released; t += 16u) {
            const int ready = t >= ready_at;
            const unsigned since_ready = ready ? t - ready_at : 0u;
            const int roster = ready && since_ready < 1200u;
            const int moved = ready && since_ready >= 1600u;
            const int unknown = (!moved && pcarr_join_unknown_counts(ready, since_ready)) ? 1 : 0;
            const int d = pcarr_join_decide(!ready, ready, roster, unknown, 0, moved ? 1 : 0);
            const int b = pcarr_join_decide_bounded(d, d < 0 ? pcarr_join_hold_timeout(t, ready, since_ready) : PCARR_HOLD_KEEP, 0, moved ? 1 : 0);
            if (b >= 0) {
                released = 1;
                released_at = t;
                cls = b;
            }
        }
        check("H sim: a quick roster still decides normally (JOIN riding once the other's MOVE is in, before any limit)",
              released && cls == PCARR_JOIN_RIDING && released_at >= ready_at + 1600u && released_at < ready_at + 1600u + 32u);
    }
    {
        /* a quiet other player (scene known, MOVE never arrives), roster known at once: held only for the 3 s unknown window, then the normal arrival */
        unsigned t, ready_at = 0u, released_at = 0u;
        int released = 0, cls = -1;
        for (t = 0u; t <= 20000u && !released; t += 16u) {
            const unsigned since_ready = t - ready_at;
            const int unknown = pcarr_join_unknown_counts(1, since_ready) ? 1 : 0;
            const int d = pcarr_join_decide(0, 1, 0, unknown, 0, 0);
            const int b = pcarr_join_decide_bounded(d, d < 0 ? pcarr_join_hold_timeout(t, 1, since_ready) : PCARR_HOLD_KEEP, 0, 0);
            if (b >= 0) {
                released = 1;
                released_at = t;
                cls = b;
            }
        }
        check("H sim: a quiet other player holds the joiner for the 3 s unknown window only", released && cls == PCARR_JOIN_NONE && released_at >= 3000u && released_at < 3016u);
    }

    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail ? 1 : 0;
}
