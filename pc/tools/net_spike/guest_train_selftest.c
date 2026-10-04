/* guest_train_selftest.c - native unit test of pc/include/pc_arrival_logic.h (T2 title-demo train edge, T5 borderless edge decisions).
 * No game, no network. Compiled against the real header the game uses. */
#include <stdio.h>
#include "pc_arrival_logic.h"

static int s_pass = 0, s_fail = 0;
static void check(const char* what, int cond) {
    printf("%s: %s\n", cond ? "PASS" : "FAIL", what);
    if (cond) {
        s_pass++;
    } else {
        s_fail++;
    }
}

#define START1 1
#define NONE 0

int main(void) {
    int latch;
    int fired;
    int i;

    /* ---- T2 ---- */
    latch = 0;
    check("T2 never saw START1: no edge for NONE", pcarr_trc_title_edge(&latch, NONE, START1, 0) == 0);
    check("T2 never saw START1: latch stays clear", latch == 0);

    latch = 0;
    check("T2 START1 frames never fire", pcarr_trc_title_edge(&latch, START1, START1, 0) == 0 && pcarr_trc_title_edge(&latch, START1, START1, 0) == 0);
    check("T2 START1 arms the latch", latch == 1);
    check("T2 START1 -> NONE (town) fires", pcarr_trc_title_edge(&latch, NONE, START1, 0) == 1);
    check("T2 the latch is consumed", latch == 0);
    fired = 0;
    for (i = 0; i < 1000; i++) {
        fired += pcarr_trc_title_edge(&latch, NONE, START1, 0);
    }
    check("T2 fires exactly once per title exit (1000 further town frames: 0 more)", fired == 0);

    latch = 0;
    pcarr_trc_title_edge(&latch, START1, START1, 0);
    check("T2 START1 -> START2 (title cycling) fires once (harmless reset)", pcarr_trc_title_edge(&latch, 2, START1, 0) == 1);
    check("T2 START2 frames afterwards do not fire", pcarr_trc_title_edge(&latch, 2, START1, 0) == 0);

    latch = 0;
    pcarr_trc_title_edge(&latch, START1, START1, 0);
    check("T2 START1 -> LOGO (-1) fires", pcarr_trc_title_edge(&latch, -1, START1, 0) == 1);

    latch = 0;
    pcarr_trc_title_edge(&latch, START1, START1, 0);
    check("T2 vanilla Start flow: exit into the player-select / train draw type does NOT fire", pcarr_trc_title_edge(&latch, NONE, START1, 1) == 0);
    check("T2 vanilla flow: the latch is consumed anyway (no late re-init in the town: mSDI_StartDataInit already ran)", latch == 0);
    check("T2 vanilla flow: the later town frames do not fire", pcarr_trc_title_edge(&latch, NONE, START1, 0) == 0);

    latch = 0;
    for (i = 0; i < 3; i++) {
        pcarr_trc_title_edge(&latch, START1, START1, 0);
        fired += pcarr_trc_title_edge(&latch, NONE, START1, 0);
    }
    check("T2 three title entries / exits fire three times", fired == 3);

    check("T2 coming_flag 3 (ride-off arrival) is kept across the re-init", pcarr_trc_coming_after_reinit(3u, 3u) == 3u);
    check("T2 coming_flag 0 stays 0", pcarr_trc_coming_after_reinit(0u, 3u) == 0u);
    check("T2 coming_flag 2 (leave) is NOT kept (mTRC_init semantics)", pcarr_trc_coming_after_reinit(2u, 3u) == 0u);
    check("T2 coming_flag 4 is NOT kept", pcarr_trc_coming_after_reinit(4u, 3u) == 0u);

    /* ---- T5 ---- */
    check("T5 floor_div: inside block 0", pcarr_floor_div(10.0f, 640.0f) == 0);
    check("T5 floor_div: block 1 start", pcarr_floor_div(640.0f, 640.0f) == 1);
    check("T5 floor_div: just left of the map is block -1 (truncation would give 0)", pcarr_floor_div(-1.0f, 640.0f) == -1);
    check("T5 floor_div: -640 is exactly block -1", pcarr_floor_div(-640.0f, 640.0f) == -1);
    check("T5 floor_div: -640.5 is block -2", pcarr_floor_div(-640.5f, 640.0f) == -2);
    check("T5 floor_div: 0.0 is block 0", pcarr_floor_div(0.0f, 640.0f) == 0);
    check("T5 floor_div: 4479.9 is block 6 (the last of 7 columns)", pcarr_floor_div(4479.9f, 640.0f) == 6);

    check("T5 clamp needed: FG field, invalid new block", pcarr_borderless_clamp_needed(1, 0) == 1);
    check("T5 clamp NOT needed: FG field, valid new block (normal acre crossings unchanged)", pcarr_borderless_clamp_needed(1, 1) == 0);
    check("T5 clamp NOT needed: not the FG field (rooms), even when the block lookup is meaningless", pcarr_borderless_clamp_needed(0, 0) == 0);
    check("T5 wade allowed: valid new block", pcarr_borderless_wade_allowed(1, 1) == 1);
    check("T5 wade refused: FG field, invalid new block", pcarr_borderless_wade_allowed(1, 0) == 0);
    check("T5 wade untouched outside the FG field", pcarr_borderless_wade_allowed(0, 0) == 1);

    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail ? 1 : 0;
}
