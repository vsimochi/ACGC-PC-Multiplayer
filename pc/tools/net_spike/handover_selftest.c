/* handover_selftest.c - native selftest of the pure hand-over logic (pc/include/pc_handover.h): message shape, the clip script sequencing, the rate
 * limit, the Work Mode plan (which hand-overs an op plays, and that a refusal plays none) and the local waiter. No engine.
 * Build: gcc -std=gnu11 -Wall -Wextra -Werror -Iinclude tools/net_spike/handover_selftest.c -o handover_selftest.exe   (cwd = pc) */
#include <stdio.h>
#include <string.h>
#include "pc_handover.h"

static int s_n, s_fail;
#define CK(c, d) do { s_n++; if (!(c)) { s_fail++; printf("FAIL - %s\n", d); } else { printf("PASS - %s\n", d); } } while (0)

static PCHandoverMsg mk(int kind, int cls, int slot, int npc, int item) {
    PCHandoverMsg m;
    memset(&m, 0, sizeof(m));
    m.msg_type = PC_HO_MSG_ID;
    m.kind = (uint8_t)kind;
    m.npc_class = (uint8_t)cls;
    m.slot = (uint8_t)slot;
    m.npc_id = (uint16_t)npc;
    m.item = (uint16_t)item;
    return m;
}

/* run a script with a clip that stops `stop_after` frames into every step (-1 = never); returns the visited steps */
static int run_script(int kind, int stop_after, int* steps, int cap, float* total) {
    PCHoScript s;
    int n = 0, last = -1, i, changed;
    float in_step = 0.0f;
    pc_ho_script_start(&s, kind, 0x1234, 0);
    steps[n++] = s.step;
    last = s.step;
    *total = 0.0f;
    for (i = 0; i < 2000 && s.active; i++) {
        int st;
        in_step += 1.0f;
        st = pc_ho_script_tick(&s, stop_after >= 0 && in_step >= (float)stop_after, 1.0f, &changed);
        *total += 1.0f;
        if (changed) {
            in_step = 0.0f;
        }
        if (st != last && n < cap) {
            steps[n++] = st;
            last = st;
        }
    }
    return n;
}

int main(void) {
    PCHandoverMsg m;
    int steps[16], n, kinds[4], items[4], i, ok;
    float total;

    /* ---- shape ---- */
    CK(sizeof(PCHandoverMsg) == 16, "wire size 16");
    m = mk(PC_HO_KIND_NPC_GIVES, PC_HO_CLASS_VILLAGER, 3, 0xE012, 0x2A00);
    CK(pc_ho_msg_ok(&m, 15), "a villager message (slot 3 < 15) is well-formed");
    m.slot = 15;
    CK(!pc_ho_msg_ok(&m, 15), "slot >= ANIMAL_NUM_MAX is refused");
    m = mk(PC_HO_KIND_NPC_TAKES, PC_HO_CLASS_SHOP, PC_HO_SLOT_NONE, 0xD001, 0x2A00);
    CK(pc_ho_msg_ok(&m, 15), "a shop message (slot 0xFF) is well-formed");
    m.slot = 0;
    CK(!pc_ho_msg_ok(&m, 15), "a shop message with a slot is refused");
    m = mk(3, PC_HO_CLASS_VILLAGER, 1, 0xE012, 0x2A00);
    CK(!pc_ho_msg_ok(&m, 15), "kind 3 is refused");
    m = mk(PC_HO_KIND_NPC_GIVES, 3, 1, 0xE012, 0x2A00);
    CK(!pc_ho_msg_ok(&m, 15), "class 3 is refused");
    m = mk(PC_HO_KIND_NPC_GIVES, PC_HO_CLASS_VILLAGER, 1, 0xE012, 0x2A00);
    m.flags = 0x02;
    CK(!pc_ho_msg_ok(&m, 15), "a reserved flag bit is refused");
    m.flags = PC_HO_FL_FISH;
    CK(pc_ho_msg_ok(&m, 15), "the fish flag is accepted");
    m.rsv = 1;
    CK(!pc_ho_msg_ok(&m, 15), "rsv != 0 is refused");
    m = mk(PC_HO_KIND_NPC_GIVES, PC_HO_CLASS_VILLAGER, 1, 0, 0x2A00);
    CK(!pc_ho_msg_ok(&m, 15), "npc_id 0 is refused");
    m = mk(PC_HO_KIND_NPC_GIVES, PC_HO_CLASS_VILLAGER, 1, 0xE012, 0);
    CK(!pc_ho_msg_ok(&m, 15), "item 0 (EMPTY_NO) is refused");
    m = mk(PC_HO_KIND_NPC_GIVES, PC_HO_CLASS_VILLAGER, 1, 0xE012, 0x2A00);
    m.msg_type = 96;
    CK(!pc_ho_msg_ok(&m, 15), "another message id is refused");

    /* ---- script: give ---- */
    n = run_script(PC_HO_KIND_NPC_GIVES, 30, steps, 16, &total);
    CK(n == 3 && steps[0] == PC_HO_ST_TRANSFER && steps[1] == PC_HO_ST_TRANS_WAIT && steps[2] == PC_HO_ST_DONE, "give: TRANSFER -> TRANS_WAIT -> DONE");
    CK(total >= 30.0f + PC_HO_TRANS_WAIT_FRAMES - 1.0f && total <= 30.0f + PC_HO_TRANS_WAIT_FRAMES + 2.0f, "give: TRANSFER until its clip stopped, then the timed wait");
    /* ---- script: take ---- */
    n = run_script(PC_HO_KIND_NPC_TAKES, 20, steps, 16, &total);
    CK(n == 4 && steps[0] == PC_HO_ST_GET && steps[1] == PC_HO_ST_GET_PULL && steps[2] == PC_HO_ST_GET_PUTAWAY && steps[3] == PC_HO_ST_DONE, "take: GET -> GET_PULL -> GET_PUTAWAY -> DONE");
    CK(total >= 59.0f && total <= 62.0f, "take: every step ends when its clip stopped (3 x 20 frames)");
    /* the previous clip's STOPPED flag on the first frame never skips a step */
    n = run_script(PC_HO_KIND_NPC_TAKES, 0, steps, 16, &total);
    CK(n == 4 && total >= 3.0f * PC_HO_MIN_FRAMES - 1.0f && total <= 3.0f * PC_HO_MIN_FRAMES + 2.0f, "a clip reporting STOPPED at once still shows each step PC_HO_MIN_FRAMES");
    /* a clip that never stops: the caps end it */
    n = run_script(PC_HO_KIND_NPC_GIVES, -1, steps, 16, &total);
    CK(n == 3 && total >= PC_HO_TRANSFER_CAP + PC_HO_TRANS_WAIT_FRAMES - 1.0f && total <= PC_HO_TRANSFER_CAP + PC_HO_TRANS_WAIT_FRAMES + 2.0f, "give: a clip that never stops ends at the caps");
    n = run_script(PC_HO_KIND_NPC_TAKES, -1, steps, 16, &total);
    CK(n == 4 && total <= PC_HO_SCRIPT_TTL_FRAMES, "take: a clip that never stops ends at the caps, within the script TTL");
    {
        PCHoScript s;
        int ch;
        memset(&s, 0, sizeof(s));
        CK(pc_ho_script_tick(&s, 1, 1.0f, &ch) == PC_HO_ST_NONE && !ch, "an inactive script shows nothing");
        pc_ho_script_start(&s, PC_HO_KIND_NPC_GIVES, 7, 1);
        CK(s.fish == 1 && s.npc_id == 7 && s.step == PC_HO_ST_TRANSFER, "start: fish flag, npc id and first step kept");
        s.total_frames = PC_HO_SCRIPT_TTL_FRAMES;
        CK(pc_ho_script_tick(&s, 0, 1.0f, &ch) == PC_HO_ST_DONE && ch && !s.active, "the script TTL ends a script");
    }

    /* ---- rate ---- */
    {
        PCHoRate r;
        memset(&r, 0, sizeof(r));
        ok = 1;
        for (i = 0; i < (int)PC_HO_RATE_N; i++) {
            ok &= pc_ho_rate_ok(&r, 1000u + (uint32_t)i);
        }
        CK(ok, "the first PC_HO_RATE_N messages of a window pass");
        CK(!pc_ho_rate_ok(&r, 1100u), "one more within the window is refused");
        CK(pc_ho_rate_ok(&r, 1000u + PC_HO_RATE_MS), "a new window passes again");
        memset(&r, 0, sizeof(r));
        CK(pc_ho_rate_ok(&r, 0xFFFFFFF0u) && pc_ho_rate_ok(&r, 0x10u), "the window survives the ms counter wrap");
    }

    /* ---- the Work Mode plan ---- */
    CK(pc_ho_work_plan(2, 0, 0x2805, 0, 0, 0, kinds, items) == 0 && pc_ho_work_plan(4, 0, 0, 0x2A00, 0, 0, kinds, items) == 0 &&
           pc_ho_work_plan(5, 0, 0x2805, 0, 0, 0, kinds, items) == 0 && pc_ho_work_plan(6, 0, 0, 0x2A00, 2, 0x1000, kinds, items) == 0,
       "a REFUSED op never plays a hand-over (DELIVER / TAKE_PARCEL / VILLAGER_GIVE / VILLAGER_RECEIVE)");
    n = pc_ho_work_plan(2, 1, 0x2805, 0, 0, 0, kinds, items);
    CK(n == 1 && kinds[0] == PC_HO_KIND_NPC_TAKES && items[0] == 0x2805, "DELIVER: the player hands the objective to Nook");
    n = pc_ho_work_plan(4, 1, 0x2805, 0x2A00, 0, 0, kinds, items);
    CK(n == 1 && kinds[0] == PC_HO_KIND_NPC_GIVES && items[0] == 0x2A00, "TAKE_PARCEL: Nook hands the parcel to the player");
    n = pc_ho_work_plan(5, 1, 0x2805, 0, 0, 0, kinds, items);
    CK(n == 1 && kinds[0] == PC_HO_KIND_NPC_GIVES && items[0] == 0x2805, "VILLAGER_GIVE: the villager hands the fetched item to the player");
    n = pc_ho_work_plan(6, 1, 0, 0x2A00, 0, 0, kinds, items);
    CK(n == 1 && kinds[0] == PC_HO_KIND_NPC_TAKES && items[0] == 0x2A00, "VILLAGER_RECEIVE (no tip): the player hands the parcel over");
    n = pc_ho_work_plan(6, 1, 0, 0x2A00, 1, 500, kinds, items);
    CK(n == 1, "VILLAGER_RECEIVE with a Bells tip: no second hand-over (the wallet)");
    n = pc_ho_work_plan(6, 1, 0, 0x2A00, 2, 0x1234, kinds, items);
    CK(n == 2 && kinds[1] == PC_HO_KIND_NPC_GIVES && items[1] == 0x1234, "VILLAGER_RECEIVE with an item tip: the parcel goes, then the tip comes back");
    CK(pc_ho_work_plan(1, 1, 1, 1, 0, 0, kinds, items) == 0 && pc_ho_work_plan(3, 1, 1, 1, 0, 0, kinds, items) == 0 && pc_ho_work_plan(7, 1, 1, 1, 2, 5, kinds, items) == 0,
       "ENTER / LEAVE / REPORT move no item by hand");
    CK(pc_ho_work_plan(2, 1, 0, 0, 0, 0, kinds, items) == 0 && pc_ho_work_plan(4, 1, 0, 0, 0, 0, kinds, items) == 0, "an op without an item plays nothing");

    /* ---- the local waiter ---- */
    {
        PCHoWait w;
        int r = 0;
        pc_ho_wait_start(&w);
        for (i = 0; i < 10; i++) {
            r = pc_ho_wait_tick(&w, 0, 0, 1.0f);
        }
        CK(r == PC_HO_WAIT_RUNNING && !w.started, "not started yet: running");
        r = pc_ho_wait_tick(&w, 1, 0, 1.0f);
        CK(r == PC_HO_WAIT_RUNNING && w.started, "the item actor got a master: started");
        r = pc_ho_wait_tick(&w, 0, 1, 1.0f);
        CK(r == PC_HO_WAIT_RUNNING, "no master but the player still in a receive state: running");
        r = pc_ho_wait_tick(&w, 0, 0, 1.0f);
        CK(r == PC_HO_WAIT_DONE, "no master and the player free: done");
        pc_ho_wait_start(&w);
        for (i = 0; i < 1000 && (r = pc_ho_wait_tick(&w, 0, 0, 1.0f)) == PC_HO_WAIT_RUNNING; i++) {
        }
        CK(r == PC_HO_WAIT_TIMEOUT && w.frames >= PC_HO_WAIT_START_CAP - 1.0f && w.frames <= PC_HO_WAIT_START_CAP + 1.0f, "a hand-over that never starts times out at the start cap");
        pc_ho_wait_start(&w);
        for (i = 0; i < 2000 && (r = pc_ho_wait_tick(&w, 1, 1, 1.0f)) == PC_HO_WAIT_RUNNING; i++) {
        }
        CK(r == PC_HO_WAIT_TIMEOUT && w.frames >= PC_HO_WAIT_TOTAL_CAP - 1.0f, "a hand-over that never ends times out at the total cap");
    }

    printf("%d/%d checks passed\n", s_n - s_fail, s_n);
    return s_fail == 0 ? 0 : 1;
}
