/* work_v2_selftest.c - native selftest of the pure Nook Work Mode v2 logic (pc/include/pc_work_logic.h + the v2 cases of pc_ho_work_plan in pc_handover.h):
 * the villager tip formula (range, friendship scaling, determinism), the urgent roll + bonus, the item pick, the service 11 blob and the Check job choices.
 * No engine. Build: gcc -std=gnu11 -Wall -Wextra -Werror -Iinclude tools/net_spike/work_v2_selftest.c -o work_v2_selftest.exe   (cwd = pc) */
#include <stdio.h>
#include <string.h>
#include "pc_work_logic.h"
#include "pc_handover.h"

static int s_n, s_fail;
#define CK(c, d) do { s_n++; if (!(c)) { s_fail++; printf("FAIL - %s\n", d); } else { printf("PASS - %s\n", d); } } while (0)

int main(void) {
    uint32_t j;
    int ok, f, n, acts[4], kinds[2], items[2];
    uint8_t b[PC_WORK_CAPS_LEN];
    unsigned urgent = 0, lo = 100000, hi = 0, bonus_lo = 100000, bonus_hi = 0, seen[5] = {0, 0, 0, 0, 0};

    /* ---- the tip */
    ok = 1;
    for (j = 1; j < 200000; j++) {
        const uint32_t t0 = pc_work_v2_tip(j, 0);
        lo = t0 < lo ? t0 : lo;
        hi = t0 > hi ? t0 : hi;
        ok &= (t0 % 10u) == 0u && t0 >= 50u && t0 <= 300u;
    }
    CK(ok && lo == 50u && hi == 300u, "T1 friendship 0: the tip is 50..300 Bells in steps of 10, both ends reached (random base)");
    ok = 1;
    for (j = 1; j < 50000; j++) {
        for (f = -128; f <= 127; f += 7) {
            const uint32_t t = pc_work_v2_tip(j, f), t0 = pc_work_v2_tip(j, 0);
            const uint32_t fc = f <= 0 ? 0u : (uint32_t)f;
            ok &= t == ((t0 + fc * 2u) / 10u) * 10u && t >= t0 && t <= 550u && (t % 10u) == 0u;
        }
    }
    CK(ok, "T2 tip = base + 2 per friendship point (negative friendship = 0), rounded down to 10, never above 550");
    ok = 1;
    for (j = 1; j < 50000; j++) {
        ok &= pc_work_v2_tip(j, 100) >= pc_work_v2_tip(j, 20) && pc_work_v2_tip(j, 127) == pc_work_v2_tip(j, 127) && pc_work_v2_tip(j, 127) == pc_work_v2_tip(j, 300);
    }
    CK(ok, "T3 higher friendship never pays less; deterministic per job; friendship above 127 is clamped");
    CK(pc_work_v2_tip(1, 127) - pc_work_v2_tip(1, 0) >= 250u, "T4 best friends add 250 Bells on top of the base (254 before rounding)");

    /* ---- urgent */
    for (j = 1; j <= 100000; j++) {
        const uint32_t bb = pc_work_v2_urgent_bonus(j);
        urgent += (unsigned)pc_work_v2_urgent(j);
        bonus_lo = bb < bonus_lo ? bb : bonus_lo;
        bonus_hi = bb > bonus_hi ? bb : bonus_hi;
        if (bb >= 100u && bb <= 300u && bb % 50u == 0u) {
            seen[(bb - 100u) / 50u] = 1;
        }
    }
    CK(urgent > 38000u && urgent < 42000u, "U1 about 40 % of the jobs are urgent when asked");
    CK(bonus_lo == 100u && bonus_hi == 300u && seen[0] && seen[1] && seen[2] && seen[3] && seen[4], "U2 the urgent bonus is 100 / 150 / 200 / 250 / 300 Bells");
    ok = 1;
    for (j = 1; j < 5000; j++) {
        ok &= pc_work_v2_urgent(j) == pc_work_v2_urgent(j) && pc_work_v2_urgent_bonus(j) == pc_work_v2_urgent_bonus(j);
    }
    CK(ok, "U3 urgent + bonus are a function of the job id only (asking again cannot re-roll them)");

    /* ---- the item pick */
    {
        static const uint16_t pool[5] = { 11, 22, 33, 44, 55 };
        unsigned hit[5] = {0, 0, 0, 0, 0};
        int k;
        ok = 1;
        for (j = 1; j <= 50000; j++) {
            const uint16_t it = pc_work_v2_pick(pool, 5, j);
            ok &= it == pc_work_v2_pick(pool, 5, j);
            for (k = 0; k < 5; k++) {
                hit[k] += it == pool[k];
            }
        }
        CK(ok && hit[0] > 8000 && hit[1] > 8000 && hit[2] > 8000 && hit[3] > 8000 && hit[4] > 8000, "I1 the pick is deterministic per job and spreads over the whole pool");
        CK(pc_work_v2_pick(NULL, 5, 1) == 0 && pc_work_v2_pick(pool, 0, 1) == 0, "I2 an empty pool gives 0 (the caller keeps the old item)");
    }

    /* ---- service 11 */
    pc_work_caps_build(b, 1);
    CK(pc_work_caps_valid(b) && b[0] == 1 && b[1] == 1 && b[7] == 0, "C1 the caps blob round trip (on)");
    pc_work_caps_build(b, 0);
    CK(pc_work_caps_valid(b) && b[1] == 0, "C2 the caps blob round trip (off)");
    b[1] = 2;
    CK(!pc_work_caps_valid(b), "C3 an unknown flag bit is refused");
    pc_work_caps_build(b, 1);
    b[5] = 1;
    CK(!pc_work_caps_valid(b), "C4 a reserved byte must be 0");
    pc_work_caps_build(b, 1);
    b[0] = 2;
    CK(!pc_work_caps_valid(b), "C5 another version is refused");

    /* ---- the Check job choices (job type 1 fruit, 2 fetch, 3 deliver; step 0 start, 1 carrying, 2 delivered) */
    n = pc_work_v2_row(1, 0, 1, acts, 4);
    CK(n == 3 && acts[0] == PC_WORK_ACT_HAND && acts[1] == PC_WORK_ACT_NO && acts[2] == PC_WORK_ACT_QUIT, "R1 fruit, holding it: Here it is! / No. / Quit job");
    n = pc_work_v2_row(1, 0, 0, acts, 4);
    CK(n == 2 && acts[0] == PC_WORK_ACT_NO && acts[1] == PC_WORK_ACT_QUIT, "R2 fruit, not holding it: No. / Quit job");
    n = pc_work_v2_row(2, 0, 1, acts, 4);
    CK(n == 2 && acts[0] == PC_WORK_ACT_NO && acts[1] == PC_WORK_ACT_QUIT, "R3 fetch, not fetched yet: No. / Quit job (never hand in)");
    n = pc_work_v2_row(2, 1, 1, acts, 4);
    CK(n == 3 && acts[0] == PC_WORK_ACT_HAND && acts[1] == PC_WORK_ACT_NO && acts[2] == PC_WORK_ACT_QUIT, "R4 fetch, carrying the item: Here it is! / No. / Quit job");
    n = pc_work_v2_row(3, 1, 0, acts, 4);
    CK(n == 2 && acts[0] == PC_WORK_ACT_NO && acts[1] == PC_WORK_ACT_QUIT, "R5 delivery on the way: No. / Quit job");
    n = pc_work_v2_row(3, 0, 1, acts, 4);
    CK(n == 3 && acts[0] == PC_WORK_ACT_TAKE && acts[1] == PC_WORK_ACT_NO, "R6 a delivery not taken yet (pockets were full) + room now: I'll take it. / No. / Quit job");
    n = pc_work_v2_row(3, 0, 0, acts, 4);
    CK(n == 2 && acts[0] == PC_WORK_ACT_NO, "R7 the same without room: No. / Quit job");
    n = pc_work_v2_row(3, 2, 0, acts, 4);
    CK(n == 2 && acts[0] == PC_WORK_ACT_REPORT && acts[1] == PC_WORK_ACT_LATER, "R8 an older delivery already handed over: I delivered it. / later (Nook still pays it; no quit)");
    CK(pc_work_v2_row(1, 0, 1, acts, 2) == 0, "R9 a too small action array is refused");

    /* ---- the v2 hand-overs */
    n = pc_ho_work_plan(9, 1, 0x2805, 0, 0, 0, kinds, items);
    CK(n == 1 && kinds[0] == PC_HO_KIND_NPC_TAKES && items[0] == 0x2805, "H1 QUIT of a fetch: the player gives the fetched item back to Nook");
    n = pc_ho_work_plan(9, 1, 0x1234, 0x1234, 0, 0, kinds, items);
    CK(n == 1 && kinds[0] == PC_HO_KIND_NPC_TAKES && items[0] == 0x1234, "H2 QUIT of a delivery: the parcel goes back to Nook");
    n = pc_ho_work_plan(10, 1, 0x1234, 0x1234, 1, 300, kinds, items);
    CK(n == 1 && kinds[0] == PC_HO_KIND_NPC_TAKES && items[0] == 0x1234, "H3 VILLAGER_COMPLETE: the player hands the parcel over; the tip and the pay are Bells (no second hand-over)");
    CK(pc_ho_work_plan(8, 1, 1, 1, 0, 0, kinds, items) == 0 && pc_ho_work_plan(9, 0, 1, 1, 0, 0, kinds, items) == 0 && pc_ho_work_plan(10, 0, 1, 1, 0, 0, kinds, items) == 0,
       "H4 URGE moves nothing; a refused QUIT / COMPLETE plays nothing");

    printf("%d/%d checks passed\n", s_n - s_fail, s_n);
    return s_fail ? 1 : 0;
}
