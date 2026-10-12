/* board_repair_selftest.c - native test of the REAL pc_board_post_is_zeroed + pc_board_repair_zeroed_posts text (extracted from pc/src/pc_m_card.c by
 * test_board_repair_native.py into board_repair_extract.inc) against minimal type stubs matching include/lb_rtc.h / m_notice.h layouts. No game, no network. */
#include <stdio.h>
#include <string.h>
#include <stdint.h>

typedef uint8_t u8;
typedef uint16_t u16;
#define TRUE 1
#define FALSE 0
#define CHAR_SPACE 32
#define MAIL_BODY_LEN 192
#define mNtc_BOARD_POST_COUNT 15
#define lbRTC_CHECK_ALL 0x3F
typedef struct { u8 sec, min, hour, day, weekday, month; u16 year; } lbRTC_time_c;
typedef struct { u8 message[MAIL_BODY_LEN]; lbRTC_time_c post_time; } mNtc_board_post_c;
static const lbRTC_time_c mTM_rtcTime_clear_code = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFFFF };
static int lbRTC_IsEqualTime(const lbRTC_time_c* a, const lbRTC_time_c* b, int f) { (void)f; return memcmp(a, b, sizeof(*a)) == 0 ? TRUE : FALSE; }
#define OSReport printf

#include "board_repair_extract.inc"

static int s_pass, s_fail;
static void check(const char* w, int c) { printf("%s: %s\n", c ? "PASS" : "FAIL", w); c ? s_pass++ : s_fail++; }

static mNtc_board_post_c legit(int n) {
    mNtc_board_post_c p; int i;
    for (i = 0; i < MAIL_BODY_LEN; i++) p.message[i] = (u8)(n + i);
    p.post_time.sec = 1; p.post_time.min = 2; p.post_time.hour = 3; p.post_time.day = (u8)(1 + n); p.post_time.weekday = 2; p.post_time.month = 5; p.post_time.year = 2001;
    return p;
}
static mNtc_board_post_c empty(void) {
    mNtc_board_post_c p; memset(p.message, CHAR_SPACE, sizeof p.message); p.post_time = mTM_rtcTime_clear_code; return p;
}
static mNtc_board_post_c zero(void) { mNtc_board_post_c p; memset(&p, 0, sizeof p); return p; }
static int same(const mNtc_board_post_c* a, const mNtc_board_post_c* b) { return memcmp(a, b, sizeof *a) == 0; }

int main(void) {
    mNtc_board_post_c b[mNtc_BOARD_POST_COUNT], ref[mNtc_BOARD_POST_COUNT];
    int i, n, ok;
    check("struct size is 200 bytes (0xC0 body + 8 time)", sizeof(mNtc_board_post_c) == 200);

    for (i = 0; i < 15; i++) b[i] = zero();
    n = pc_board_repair_zeroed_posts(b, 15);
    ref[0] = empty();
    for (ok = 1, i = 0; i < 15; i++) ok &= same(&b[i], &ref[0]);
    check("all-zero legacy board -> 15 empty slots, repaired == 15", n == 15 && ok);

    for (i = 0; i < 15; i++) b[i] = zero();
    b[14] = legit(7); ref[0] = legit(7);
    n = pc_board_repair_zeroed_posts(b, 15);
    ref[1] = empty();
    for (ok = same(&b[0], &ref[0]), i = 1; i < 15; i++) ok &= same(&b[i], &ref[1]);
    check("14 zero posts + one real post (legacy after a write): real post compacted to slot 0, others empty, repaired == 14", n == 14 && ok);

    for (i = 0; i < 15; i++) ref[i] = b[i] = (i < 4) ? legit(i) : empty();
    n = pc_board_repair_zeroed_posts(b, 15);
    for (ok = 1, i = 0; i < 15; i++) ok &= same(&b[i], &ref[i]);
    check("vanilla board (4 handbills + 11 clear-code posts): repaired == 0, byte-identical", n == 0 && ok);

    for (i = 0; i < 15; i++) ref[i] = b[i] = legit(i);
    ref[3] = b[3] = zero(); b[3].message[191] = 1; ref[3] = b[3];          /* body: one nonzero byte */
    ref[5] = b[5] = zero(); b[5].post_time.year = 1; ref[5] = b[5];        /* time: one nonzero byte */
    ref[6] = b[6] = zero(); b[6].post_time.sec = 0; b[6].post_time.day = 1; ref[6] = b[6]; /* body zero, time day=1 */
    n = pc_board_repair_zeroed_posts(b, 15);
    for (ok = 1, i = 0; i < 15; i++) ok &= same(&b[i], &ref[i]);
    check("full board of real posts incl. partially-zero posts: repaired == 0, nothing modified", n == 0 && ok);

    for (i = 0; i < 15; i++) b[i] = (i == 2 || i == 9) ? zero() : legit(i);
    n = pc_board_repair_zeroed_posts(b, 15);
    {
        int w = 0, bad = 0;
        for (i = 0; i < 15; i++) { if (i == 2 || i == 9) continue; mNtc_board_post_c l = legit(i); if (!same(&b[w], &l)) bad = 1; w++; }
        for (i = w; i < 15; i++) { mNtc_board_post_c e = empty(); if (!same(&b[i], &e)) bad = 1; }
        check("2 zeroed posts among real ones: survivors keep relative order and count (13), empties at the tail, repaired == 2", n == 2 && !bad && w == 13);
    }

    {
        mNtc_board_post_c p = zero(); p.post_time = mTM_rtcTime_clear_code;     /* zero body but clear-code time: an interrupted empty slot-not a zeroed post */
        b[0] = p; for (i = 1; i < 15; i++) b[i] = legit(i);
        ref[0] = b[0];
        n = pc_board_repair_zeroed_posts(b, 15);
        check("zero body + clear-code time is NOT 'zeroed' (not touched)", n == 0 && same(&b[0], &ref[0]));
    }
    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail ? 1 : 0;
}
