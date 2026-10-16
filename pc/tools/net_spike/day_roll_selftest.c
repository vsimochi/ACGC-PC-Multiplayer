/* day_roll_selftest.c - native model test of pc_day_roll.c (day rollover on a long-running host: the PURE date rules, gate and attempt limiter).
 * Build + run (git-bash, PATH appended with /c/msys64/ucrt64/bin), from pc/:
 *   gcc -std=gnu11 -Wall -Iinclude tools/net_spike/day_roll_selftest.c -o build64_w3/day_roll_selftest.exe && build64_w3/day_roll_selftest.exe */
#include "../../src/pc_day_roll.c"

#include <stdio.h>

static int s_fail = 0, s_n = 0;
#define CHECK(c) do { s_n++; if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); s_fail++; } } while (0)

static PCDayRollDate D(int y, int mo, int d, int h, int mi, int s) {
    PCDayRollDate t;
    t.year = y; t.month = mo; t.day = d; t.hour = h; t.min = mi; t.sec = s;
    return t;
}

static PCDayRollGateIn gate_ok(void) {
    PCDayRollGateIn in;
    memset(&in, 0, sizeof(in));
    in.enabled = 1; in.is_host = 1; in.world_ready = 1; in.host_in_town = 1; in.host_idle = 1;
    in.soft_max_ms = PC_DAYROLL_SOFT_MAX_MS;
    return in;
}

int main(void) {
    PCDayRollDate a, b, n;
    PCDayRollGateIn in;
    PCDayRollAttempts at;
    int sec, hh, mm;
    long d;

    /* calendar */
    CHECK(pc_dayroll_days_from_civil(1970, 1, 1) == 0);
    CHECK(pc_dayroll_days_from_civil(2000, 3, 1) - pc_dayroll_days_from_civil(2000, 2, 28) == 2); /* leap 2000 */
    CHECK(pc_dayroll_days_from_civil(2100, 3, 1) - pc_dayroll_days_from_civil(2100, 2, 28) == 1); /* no leap 2100 */
    CHECK(pc_dayroll_days_from_civil(2027, 1, 1) - pc_dayroll_days_from_civil(2026, 12, 31) == 1);
    for (d = -1000; d < 80000; d += 37) {
        PCDayRollDate c;
        pc_dayroll_civil_from_days(d, &c);
        if (pc_dayroll_days_from_civil(c.year, c.month, c.day) != d) { CHECK(0); break; }
    }
    CHECK(1);

    /* DAY due: the vanilla mTM_renewal_renew_time date check */
    a = D(2026, 10, 15, 0, 0, 0); b = D(2026, 10, 15, 23, 59, 59);
    CHECK(!pc_dayroll_day_due(&a, &b));                              /* same date, any time */
    b = D(2026, 10, 16, 0, 0, 5);  CHECK(pc_dayroll_day_due(&a, &b)); /* midnight passed */
    b = D(2026, 10, 14, 23, 0, 0); CHECK(pc_dayroll_day_due(&a, &b)); /* clock went back across midnight: vanilla re-inits too */
    b = D(2026, 11, 15, 0, 0, 0);  CHECK(pc_dayroll_day_due(&a, &b)); /* month differs */
    b = D(2027, 10, 15, 0, 0, 0);  CHECK(pc_dayroll_day_due(&a, &b)); /* year differs */
    b = D(0, 0, 0, 0, 0, 0);       CHECK(!pc_dayroll_day_due(&a, &b)); /* clock not read */
    CHECK(!pc_dayroll_day_due(NULL, &b));

    /* next 6 AM and GROW due: the vanilla mAGrw_RenewalFgItem rule */
    a = D(2026, 10, 15, 5, 30, 0); pc_dayroll_next_grow(&a, &n);
    CHECK(n.year == 2026 && n.month == 10 && n.day == 15 && n.hour == 6 && n.min == 0 && n.sec == 0);
    a = D(2026, 10, 15, 6, 0, 0);  pc_dayroll_next_grow(&a, &n);
    CHECK(n.day == 16 && n.hour == 6);
    a = D(2026, 12, 31, 22, 0, 0); pc_dayroll_next_grow(&a, &n);
    CHECK(n.year == 2027 && n.month == 1 && n.day == 1 && n.hour == 6);
    a = D(2028, 2, 28, 7, 0, 0);   pc_dayroll_next_grow(&a, &n);
    CHECK(n.month == 2 && n.day == 29); /* leap year */
    a = D(2026, 10, 15, 6, 10, 0);
    b = D(2026, 10, 16, 0, 0, 5);  CHECK(!pc_dayroll_grow_due(&a, 0, &b)); /* midnight: not yet */
    b = D(2026, 10, 16, 5, 59, 59); CHECK(!pc_dayroll_grow_due(&a, 0, &b));
    b = D(2026, 10, 16, 6, 0, 0);  CHECK(pc_dayroll_grow_due(&a, 0, &b));  /* 6 AM exactly */
    b = D(2026, 10, 19, 3, 0, 0);  CHECK(pc_dayroll_grow_due(&a, 0, &b));  /* several days down */
    b = D(2026, 10, 15, 6, 9, 0);  CHECK(pc_dayroll_grow_due(&a, 0, &b));  /* backwards clock: FIRST_FORCE */
    b = D(2026, 10, 15, 12, 0, 0); CHECK(!pc_dayroll_grow_due(&a, 0, &b)); /* same morning, after growth */
    CHECK(pc_dayroll_grow_due(&a, 1, &b));                                  /* clear code: first growth */
    a = D(2026, 10, 16, 2, 0, 0);  b = D(2026, 10, 16, 6, 0, 1); CHECK(pc_dayroll_grow_due(&a, 0, &b)); /* last before 6: same-day 6 AM */
    b = D(0, 0, 0, 0, 0, 0); CHECK(!pc_dayroll_grow_due(&a, 1, &b));

    /* scene blocks (PCNetSceneKind values) */
    CHECK(pc_dayroll_scene_block(1, 1) == PC_DAYROLL_BLOCK_NONE); /* FIELD */
    CHECK(pc_dayroll_scene_block(1, 6) == PC_DAYROLL_BLOCK_NONE); /* PLAYER_HOUSE */
    CHECK(pc_dayroll_scene_block(1, 2) == PC_DAYROLL_BLOCK_HARD); /* SHOP */
    CHECK(pc_dayroll_scene_block(1, 7) == PC_DAYROLL_BLOCK_HARD); /* VILLAGER_HOUSE */
    CHECK(pc_dayroll_scene_block(1, 3) == PC_DAYROLL_BLOCK_SOFT); /* POST_OFFICE */
    CHECK(pc_dayroll_scene_block(1, 4) == PC_DAYROLL_BLOCK_SOFT);
    CHECK(pc_dayroll_scene_block(1, 5) == PC_DAYROLL_BLOCK_SOFT);
    CHECK(pc_dayroll_scene_block(1, 8) == PC_DAYROLL_BLOCK_SOFT);
    CHECK(pc_dayroll_scene_block(1, 0) == PC_DAYROLL_BLOCK_SOFT);
    CHECK(pc_dayroll_scene_block(0, 1) == PC_DAYROLL_BLOCK_SOFT); /* unknown scene */

    /* gate */
    in = gate_ok(); CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_GO);
    in = gate_ok(); in.enabled = 0; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_WAIT_OFF);
    in = gate_ok(); in.is_host = 0; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_WAIT_NOT_HOST);
    in = gate_ok(); in.world_ready = 0; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_WAIT_NOT_READY);
    in = gate_ok(); in.host_in_town = 0; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_WAIT_HOST_AWAY); /* listen host in a building */
    in = gate_ok(); in.host_idle = 0; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_WAIT_HOST_BUSY);
    in = gate_ok(); in.peers_joining = 1; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_WAIT_PEER_JOINING);
    in = gate_ok(); in.pending_interactions = 1; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_WAIT_PEER_PENDING);
    in = gate_ok(); in.hard_blockers = 1; in.deferred_ms = 0xFFFFFFF0u; in.ignore_soft = 1;
    CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_WAIT_PEER_HARD); /* a shop never times out, not even with `roll now` */
    in = gate_ok(); in.soft_blockers = 2; in.deferred_ms = 1000; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_WAIT_PEER_SOFT);
    in.deferred_ms = PC_DAYROLL_SOFT_MAX_MS - 1; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_WAIT_PEER_SOFT);
    in.deferred_ms = PC_DAYROLL_SOFT_MAX_MS; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_GO); /* bounded wait */
    in.deferred_ms = 0; in.ignore_soft = 1; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_GO); /* operator */
    in = gate_ok(); in.host_in_town = 0; in.hard_blockers = 1; CHECK(pc_dayroll_gate(&in) == PC_DAYROLL_WAIT_HOST_AWAY);
    CHECK(pc_dayroll_gate_is_peer_wait(PC_DAYROLL_WAIT_PEER_SOFT) && pc_dayroll_gate_is_peer_wait(PC_DAYROLL_WAIT_PEER_HARD) && !pc_dayroll_gate_is_peer_wait(PC_DAYROLL_WAIT_HOST_BUSY));
    CHECK(strcmp(pc_dayroll_gate_name(PC_DAYROLL_GO), "go") == 0 && strcmp(pc_dayroll_gate_name(99), "?") == 0);

    /* attempts: one per key, retries spaced, bounded */
    memset(&at, 0, sizeof(at));
    CHECK(pc_dayroll_attempt_allowed(&at, 42, 1000, 600000, 3));
    pc_dayroll_attempt_note(&at, 42, 1000);
    CHECK(!pc_dayroll_attempt_allowed(&at, 42, 2000, 600000, 3));          /* same key: not before the gap */
    CHECK(pc_dayroll_attempt_allowed(&at, 43, 2000, 600000, 3));           /* a new key (the next day) at once */
    CHECK(pc_dayroll_attempt_allowed(&at, 42, 601000, 600000, 3));
    pc_dayroll_attempt_note(&at, 42, 601000);
    pc_dayroll_attempt_note(&at, 42, 1201000);
    CHECK(at.tries == 3);
    CHECK(!pc_dayroll_attempt_allowed(&at, 42, 5000000, 600000, 3));       /* given up on that key */
    pc_dayroll_attempt_note(&at, 43, 5000000);
    CHECK(at.key == 43 && at.tries == 1);
    CHECK(pc_dayroll_attempt_allowed(&at, 43, 5000000u + 600000u, 600000, 3));
    memset(&at, 0, sizeof(at));
    pc_dayroll_attempt_note(&at, 7, 0xFFFFFF00u);
    CHECK(pc_dayroll_attempt_allowed(&at, 7, 0xFFFFFF00u + 600000u, 600000, 3)); /* ms wrap */

    /* test hook parser */
    CHECK(pc_dayroll_parse_test_at("30", &sec, &hh, &mm) && sec == 30 && hh == 0 && mm == 0);
    CHECK(pc_dayroll_parse_test_at("5,0630", &sec, &hh, &mm) && sec == 5 && hh == 6 && mm == 30);
    CHECK(!pc_dayroll_parse_test_at("", &sec, &hh, &mm));
    CHECK(!pc_dayroll_parse_test_at("x", &sec, &hh, &mm));
    CHECK(!pc_dayroll_parse_test_at("-1", &sec, &hh, &mm));
    CHECK(!pc_dayroll_parse_test_at("5,2460", &sec, &hh, &mm));
    CHECK(!pc_dayroll_parse_test_at("5,630", &sec, &hh, &mm));
    CHECK(!pc_dayroll_parse_test_at("5;0630", &sec, &hh, &mm));
    CHECK(!pc_dayroll_parse_test_at(NULL, &sec, &hh, &mm));

    printf("day_roll_selftest: %d checks, %d failed\n", s_n, s_fail);
    return s_fail ? 1 : 0;
}
