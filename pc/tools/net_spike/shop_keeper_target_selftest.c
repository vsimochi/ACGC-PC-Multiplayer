/* shop_keeper_target_selftest.c - native selftest of the pure shop keeper target picker (pc/include/pc_shop_keeper_target.h): nearest pick, the
 * one-tile margin + dwell hysteresis, determinism across "processes" (same input = same target; ties by the lower wire id), a talking player first,
 * a target that leaves, and a jittered approach that never flickers. No engine.
 * Build: gcc -std=gnu11 -Wall -Wextra -Werror -Iinclude tools/net_spike/shop_keeper_target_selftest.c -o skt_selftest.exe   (cwd = pc) */
#include <stdio.h>
#include <string.h>
#include "pc_shop_keeper_target.h"

static int s_n, s_fail;
#define CK(c, d) do { s_n++; if (!(c)) { s_fail++; printf("FAIL - %s\n", d); } else { printf("PASS - %s\n", d); } } while (0)

static PCSktCand C(int id, float x, float z, int talk) {
    PCSktCand c;
    c.id = id;
    c.x = x;
    c.z = z;
    c.talking = talk;
    return c;
}

/* run `frames` frames with the same candidates; returns the last pick */
static int run(PCSktState* s, const PCSktCand* c, int n, int frames) {
    int i, r = PC_SKT_NONE;
    for (i = 0; i < frames; i++) {
        r = pc_skt_pick(s, 0.0f, 0.0f, c, n, 1.0f);
    }
    return r;
}

int main(void) {
    PCSktState s, s2;
    PCSktCand c[4];
    int r, i, flips, last;

    /* empty / single */
    pc_skt_reset(&s);
    CK(pc_skt_pick(&s, 0, 0, c, 0, 1.0f) == PC_SKT_NONE, "nobody in the shop -> no target");
    c[0] = C(3, 100, 0, 0);
    CK(pc_skt_pick(&s, 0, 0, c, 1, 1.0f) == 3, "one player -> that player");

    /* first pick = nearest */
    pc_skt_reset(&s);
    c[0] = C(1, 200, 0, 0);
    c[1] = C(2, 80, 0, 0);
    CK(pc_skt_pick(&s, 0, 0, c, 2, 1.0f) == 2, "first pick: the nearest player (B at 80 vs A at 200)");

    /* hysteresis: B the target; A comes to within less than one tile nearer -> stays B */
    c[0] = C(1, 60, 0, 0); /* A 60, B 80: only 20 nearer */
    CK(run(&s, c, 2, 200) == 2, "a challenger less than one tile nearer never takes over (200 frames)");
    /* A one tile+ nearer, but for fewer than DWELL frames */
    c[0] = C(1, 30, 0, 0); /* 50 nearer */
    CK(run(&s, c, 2, (int)PC_SKT_DWELL - 2) == 2, "one tile nearer but shorter than the dwell -> still the old target");
    c[0] = C(1, 70, 0, 0); /* back out of the margin: dwell restarts */
    CK(run(&s, c, 2, 1) == 2, "challenger fell back inside the margin -> old target");
    c[0] = C(1, 30, 0, 0);
    CK(run(&s, c, 2, (int)PC_SKT_DWELL - 2) == 2, "the dwell restarted after the challenger fell back");
    CK(run(&s, c, 2, 3) == 1, "one tile nearer for the whole dwell -> switch to A");
    /* and back: B must now be the one a tile nearer */
    c[1] = C(2, 0, 10, 0); /* B 10, A 30: 20 nearer */
    CK(run(&s, c, 2, 300) == 1, "switched target is kept against a 20-unit-nearer B");
    c[1] = C(2, 0, -100, 0);
    c[0] = C(1, 0, 200, 0);
    CK(run(&s, c, 2, (int)PC_SKT_DWELL + 1) == 2, "B a tile nearer for the dwell -> back to B");

    /* determinism / ties: two processes with the candidates in different orders agree; an exact tie picks the lower id */
    pc_skt_reset(&s);
    pc_skt_reset(&s2);
    c[0] = C(5, 50, 0, 0);
    c[1] = C(2, -50, 0, 0);
    c[2] = C(7, 0, 50, 0);
    {
        PCSktCand rev[3];
        rev[0] = c[2];
        rev[1] = c[1];
        rev[2] = c[0];
        r = pc_skt_pick(&s, 0, 0, c, 3, 1.0f);
        CK(r == 2 && pc_skt_pick(&s2, 0, 0, rev, 3, 1.0f) == r, "an exact tie: the lower wire id (2), whatever the candidate order");
    }

    /* talking player first, at once, inside the talk range only */
    pc_skt_reset(&s);
    c[0] = C(1, 20, 0, 0);
    c[1] = C(2, 90, 0, 1); /* talking, 90 < talk range */
    CK(pc_skt_pick(&s, 0, 0, c, 2, 1.0f) == 2 && s.cur_talk, "a talking player within the talk range wins at once over a nearer idle one");
    c[1] = C(2, 90, 0, 0); /* the talk ended */
    CK(run(&s, c, 2, (int)PC_SKT_DWELL - 2) == 2, "after the talk the talker stays the target until the dwell");
    CK(run(&s, c, 2, 3) == 1, "then the nearer player (one tile+ nearer) takes over");
    c[1] = C(2, 300, 0, 1); /* talking far away (not with this keeper) */
    CK(run(&s, c, 2, 50) == 1, "a player talking outside the talk range is not a talk partner");
    c[0] = C(1, 60, 0, 1);
    c[1] = C(2, 40, 0, 1);
    CK(pc_skt_pick(&s, 0, 0, c, 2, 1.0f) == 2, "two talkers: the nearer one");

    /* the target leaves the shop -> replaced at once */
    pc_skt_reset(&s);
    c[0] = C(1, 20, 0, 0);
    c[1] = C(2, 300, 0, 0);
    CK(run(&s, c, 2, 5) == 1, "A the target");
    c[0] = c[1];
    CK(pc_skt_pick(&s, 0, 0, c, 1, 1.0f) == 2, "A left the shop -> B at once (no dwell)");

    /* jitter: two players about equally far, positions jittered by +-15 units each frame (puppet delay) -> no flicker */
    pc_skt_reset(&s);
    flips = 0;
    last = PC_SKT_NONE;
    for (i = 0; i < 2000; i++) {
        float ja = (float)((i * 37) % 31 - 15), jb = (float)((i * 53) % 31 - 15);
        c[0] = C(1, 100 + ja, 0, 0);
        c[1] = C(2, 0, 100 + jb, 0);
        r = pc_skt_pick(&s, 0, 0, c, 2, 1.0f);
        if (last != PC_SKT_NONE && r != last) {
            flips++;
        }
        last = r;
    }
    CK(flips == 0, "two players at ~equal distance with +-15 unit jitter: no target flicker in 2000 frames");

    /* a slow walk past: B walks from far to near; exactly one switch */
    pc_skt_reset(&s);
    flips = 0;
    last = PC_SKT_NONE;
    for (i = 0; i <= 300; i++) {
        c[0] = C(1, 100, 0, 0);
        c[1] = C(2, 0, 300.0f - (float)i, 0);
        r = pc_skt_pick(&s, 0, 0, c, 2, 1.0f);
        if (last != PC_SKT_NONE && r != last) {
            flips++;
        }
        last = r;
    }
    CK(flips == 1 && last == 2, "B walks in from 300 to 0 past A at 100: exactly one switch, to B");

    /* dt scaling (a 30 fps frame counts double) */
    pc_skt_reset(&s);
    c[0] = C(1, 200, 0, 0);
    c[1] = C(2, 100, 0, 0);
    (void)pc_skt_pick(&s, 0, 0, c, 2, 1.0f);
    c[0] = C(1, 10, 0, 0);
    for (i = 0; i < (int)(PC_SKT_DWELL / 2.0f); i++) {
        r = pc_skt_pick(&s, 0, 0, c, 2, 2.0f);
    }
    CK(r == 1, "the dwell counts 60 fps frames (dt 2 halves the number of calls)");

    /* the margin is one tile in distance, not in squares: 100 vs 61 (39) stays, 100 vs 59 (41) switches */
    pc_skt_reset(&s);
    c[0] = C(1, 100, 0, 0);
    c[1] = C(2, 0, 200, 0);
    (void)pc_skt_pick(&s, 0, 0, c, 2, 1.0f);
    c[1] = C(2, 0, 61, 0);
    CK(run(&s, c, 2, 100) == 1, "39 units nearer: no switch");
    c[1] = C(2, 0, 59, 0);
    CK(run(&s, c, 2, (int)PC_SKT_DWELL + 1) == 2, "41 units nearer for the dwell: switch");

    printf("%d/%d checks passed\n", s_n - s_fail, s_n);
    return s_fail ? 1 : 0;
}
