/* Guest Nook dialogue (M4): generated messages + choice strings of the house offer. See include/pc_nook_house.h. */
#include <stdio.h>
#include <string.h>

#include "pc_nook_house.h"
#include "pc_guest_mail.h" /* guest mailbox: the post office girl's "Check my mail." choice patch (pc_post_mail_msg_arm / _more_choice) */
#include "pc_net_game.h"
#include "pc_residence.h" /* PC_RESIDENCE_HOUSES */
#include "pc_platform.h"  /* SDL_GetTicks: the rejoin hold heartbeat runs on the wall clock */
#include "m_msg_data.h"
#include "m_name_table.h" /* ITM_FOOD_*: the Work Mode objective names */
#include "pc_shop_upgrade.h" /* multiplayer shop upgrade: the closure length / minutes left in Nook's rows and the door row */
#include "pc_sell_logic.h"   /* Nook bulk sell basket: pc_sell_format_bells (the quote row) */

/* control code bytes (tools/msg_tool.py COMMANDS / CONT_SIZES; include/m_font.h mFont_CONT_CODE_*) */
#define NK_CC 0x7F
#define NK_END 0x00
#define NK_CONTINUE 0x01
#define NK_CLEAR 0x02
#define NK_PAUSE 0x03
#define NK_BTN 0x04
#define NK_DEMO_NPC0 0x09
#define NK_OPENCHOICE 0x0D
#define NK_SEL2 0x16
#define NK_SEL3 0x17
#define NK_SEL4 0x18
#define NK_FORCENEXT 0x19
#define NK_PLAYERNAME 0x1A
#define NK_SELNOB 0x5E
#define NK_SEL5 0x79
#define NK_SEL6 0x7A
#define NK_NEWLINE 205

/* existing choice string ids (the vanilla table) */
#define NK_SEL_YES 3          /* "Yes." */
#define NK_SEL_NO 4           /* "No." */
#define NK_SEL_NEVERMIND 11   /* "Never mind..." */
#define NK_SEL_TURNIPS 0x24E  /* "Turnip Prices?" */
#define NK_SEL_HEARCODE 0x24C /* "Hear code" */
#define NK_SEL_SAYCODE 0x24D  /* "Say code" */
#define NK_SEL_HANGON 0x3D    /* "Umm, hang on!" */

static int s_pick[PC_RESIDENCE_HOUSES];
static int s_pick_n = 0;
static int s_confirm = -1;
static PCWorkView s_wk_view;
static unsigned char s_wk_name[8] = { ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ' };
static int s_first_page_work = 0;
static int s_first_page_id = 0x1092; /* the vanilla What can I do for you row of Tom Nook / Timmy / Tommy (aNSC_MSG_INTERACT_START) */

typedef struct {
    unsigned char* d;
    int n, cap, err;
} NkBuf;

static void nk_byte(NkBuf* b, int c) {
    if (b->n < b->cap) {
        b->d[b->n++] = (unsigned char)c;
    } else {
        b->err = 1;
    }
}

static void nk_text(NkBuf* b, const char* s) {
    for (; *s != '\0'; s++) {
        if (*s == '\n') {
            nk_byte(b, NK_NEWLINE);
        } else {
            nk_byte(b, (unsigned char)*s); /* the game's charset is ASCII for these characters (the selftest checks the set) */
        }
    }
}

static void nk_code(NkBuf* b, int code) {
    nk_byte(b, NK_CC);
    nk_byte(b, code);
}

static void nk_pause(NkBuf* b, int t) {
    nk_code(b, NK_PAUSE);
    nk_byte(b, t);
}

static void nk_ids(NkBuf* b, const int* ids, int n) {
    int i;
    for (i = 0; i < n; i++) {
        nk_byte(b, (ids[i] >> 8) & 0xFF);
        nk_byte(b, ids[i] & 0xFF);
    }
}

/* page break: wait for the button, then clear the window */
static void nk_page(NkBuf* b) {
    nk_code(b, NK_BTN);
    nk_code(b, NK_CLEAR);
}

/* SETSELSTR<n> + the tail the vanilla Nook rows use (SELNOB, BTN, OPENCHOICE, FORCENEXT, "choice made" demo order 9 = 1, MSGCONTINUE): the actor reads the choice and sets
 * the next message itself (same shape as the 0x1092 "What can I do for you" row). */
static void nk_choice(NkBuf* b, const int* ids, int n) {
    nk_code(b, n == 2 ? NK_SEL2 : n == 3 ? NK_SEL3 : n == 4 ? NK_SEL4 : n == 5 ? NK_SEL5 : NK_SEL6);
    nk_ids(b, ids, n);
    nk_code(b, NK_SELNOB);
    nk_code(b, NK_BTN);
    nk_code(b, NK_OPENCHOICE);
    nk_code(b, NK_FORCENEXT);
    nk_code(b, NK_DEMO_NPC0);
    nk_byte(b, 9);
    nk_byte(b, 0);
    nk_byte(b, 1);
    nk_code(b, NK_CONTINUE);
}

/* Nook bulk sell basket: the vanilla-shaped choice tail WITHOUT the "choice made" demo order (aNSC_buy_check reads the choice itself, like the vanilla sell offer;
 * the order value 9 is what aNSC_buy_after_service waits for after a sale, so the quote must not set it) */
static void nk_choice_plain(NkBuf* b, const int* ids, int n) {
    nk_code(b, n == 2 ? NK_SEL2 : n == 3 ? NK_SEL3 : n == 4 ? NK_SEL4 : n == 5 ? NK_SEL5 : NK_SEL6);
    nk_ids(b, ids, n);
    nk_code(b, NK_SELNOB);
    nk_code(b, NK_BTN);
    nk_code(b, NK_OPENCHOICE);
    nk_code(b, NK_FORCENEXT);
    nk_code(b, NK_CONTINUE);
}

static int s_sell_count = 0;
static unsigned s_sell_total = 0;

void pc_nook_house_set_sell(int count, unsigned total) {
    s_sell_count = count;
    s_sell_total = total;
}

static void nk_price(char* out, size_t cap) {
    int p = pc_net_game_house_purchase_price();
    if (p >= 1000) {
        snprintf(out, cap, "%d,%03d", p / 1000, p % 1000);
    } else {
        snprintf(out, cap, "%d", p);
    }
}

/* Rejoin hold with a HEARTBEAT (see pc_nook_house.h): 0 off, 1 purchase in flight, 2 APPLIED. The shop proc touches it every game frame; it expires NK_HOLD_IDLE_MS (30 frames at 60 fps)
 * after the last touch and, once APPLIED, NK_HOLD_APPLIED_CAP_MS after APPLIED whatever happens. WALL CLOCK, not polls: pc_main polls the hold from VIWaitForRetrace, which runs many
 * times per game frame (the first version counted polls and hit its 900 "frames" cap ~4 s after APPLIED, before the congratulation row was read). */
#define NK_HOLD_IDLE_MS 500u
#define NK_HOLD_APPLIED_CAP_MS 15000u
static int s_hold_mode = 0;
static uint32_t s_hold_touch_ms = 0;
static uint32_t s_hold_applied_ms = 0;

static uint32_t nk_now_ms(void) {
    return (uint32_t)SDL_GetTicks();
}

void pc_nook_house_rejoin_hold_set(int on) {
    const int mode = on <= 0 ? 0 : on >= 2 ? 2 : 1;
    if (mode != s_hold_mode) {
        printf("[NET][HOUSE] rejoin hold set to %d\n", mode);
    }
    if (mode == 2 && s_hold_mode != 2) {
        s_hold_applied_ms = nk_now_ms();
    }
    s_hold_mode = mode;
    s_hold_touch_ms = nk_now_ms();
}

void pc_nook_house_rejoin_hold_touch(void) {
    if (s_hold_mode != 0) {
        s_hold_touch_ms = nk_now_ms();
    }
}

int pc_nook_house_rejoin_hold(void) {
    uint32_t now;
    if (s_hold_mode == 0) {
        return 0;
    }
    now = nk_now_ms();
    if ((uint32_t)(now - s_hold_touch_ms) > NK_HOLD_IDLE_MS || (s_hold_mode == 2 && (uint32_t)(now - s_hold_applied_ms) > NK_HOLD_APPLIED_CAP_MS)) {
        printf("[NET][HOUSE] rejoin hold EXPIRED (heartbeat): mode %d, %u ms since the last touch, %u ms since APPLIED\n", s_hold_mode, (unsigned)(now - s_hold_touch_ms), (unsigned)(now - s_hold_applied_ms));
        s_hold_mode = 0;
        return 0;
    }
    return 1;
}

int pc_nook_msg_id(int which) {
    return MSG_MAX + which;
}

int pc_nook_restock_door_msg(void) {
    return MSG_MAX + PC_NOOK_MSG_RESTOCK_DOOR; /* the shop doors (ac_*_move.c_inc) while the shop restocks */
}

int pc_nook_upgrade_door_msg(void) {
    return MSG_MAX + PC_NOOK_MSG_UPGRADE_DOOR; /* the shop doors (ac_*_move.c_inc) while the shop is closed for the upgrade */
}

/* "5 minutes" / "1 minute" (the shop upgrade rows) */
static void nk_minutes(char* out, size_t cap, int m) {
    if (m < 1) {
        m = 1;
    }
    snprintf(out, cap, "%d minute%s", m, m == 1 ? "" : "s");
}

int pc_nook_msg_id_valid(int id) {
    return id >= MSG_MAX && id < MSG_MAX + PC_NOOK_MSG_COUNT;
}

int pc_nook_sel_id_valid(int id) {
    return id >= PC_NOOK_SEL_BASE && id < PC_NOOK_SEL_COUNT_END;
}

void pc_nook_house_set_pick(const int* houses, int n) {
    int i;
    s_pick_n = (houses == NULL || n < 1) ? 0 : (n > PC_RESIDENCE_HOUSES ? PC_RESIDENCE_HOUSES : n);
    for (i = 0; i < s_pick_n; i++) {
        s_pick[i] = houses[i];
    }
}

void pc_nook_house_set_confirm(int house_or_auto) {
    s_confirm = house_or_auto;
}

void pc_nook_house_set_work_view(const PCWorkView* v, const unsigned char* villager_name8) {
    if (v != NULL) {
        s_wk_view = *v;
    }
    if (villager_name8 != NULL) {
        memcpy(s_wk_name, villager_name8, 8);
    }
}

static const char* nk_work_item_name(int item) {
    switch (item) {
        case ITM_FOOD_APPLE: return "an apple";
        case ITM_FOOD_CHERRY: return "a cherry";
        case ITM_FOOD_PEAR: return "a pear";
        case ITM_FOOD_PEACH: return "a peach";
        case ITM_FOOD_ORANGE: return "an orange";
        default: break;
    }
    if (item >= ITM_PAPER_START && item < ITM_PAPER_START + 64) {
        return "some stationery";
    }
    return "the item";
}

/* the villager's name as the game's own name bytes (letters are ASCII; trailing spaces trimmed) */
static void nk_work_name(NkBuf* b) {
    int n = 8, i;
    while (n > 0 && (s_wk_name[n - 1] == ' ' || s_wk_name[n - 1] == 0)) {
        n--;
    }
    if (n == 0) {
        nk_text(b, "the villager");
        return;
    }
    for (i = 0; i < n; i++) {
        nk_byte(b, s_wk_name[i]);
    }
}

int pc_nook_work_row(const PCWorkView* v, int* acts, int cap) {
    int n = 0;
    if (cap < 3) {
        return 0;
    }
    if (v->job_type == PC_WORK_JOB_FRUIT || (v->job_type == PC_WORK_JOB_FETCH_VILLAGER && v->obj_state == PC_WORK_OBJ_CARRYING)) {
        acts[n++] = PC_NOOK_WACT_HAND;
        acts[n++] = PC_NOOK_WACT_LATER;
    } else if (v->job_type == PC_WORK_JOB_FETCH_VILLAGER) {
        acts[n++] = PC_NOOK_WACT_LATER;
    } else if (v->obj_state == PC_WORK_OBJ_START) {
        acts[n++] = PC_NOOK_WACT_TAKE;
        acts[n++] = PC_NOOK_WACT_LATER;
    } else if (v->obj_state == PC_WORK_OBJ_CARRYING) {
        acts[n++] = PC_NOOK_WACT_LATER;
    } else {
        acts[n++] = PC_NOOK_WACT_REPORT;
    }
    acts[n++] = PC_NOOK_WACT_QUIT;
    return n;
}

int pc_nook_first_page_work(void) {
    return s_first_page_work;
}

/* ---- MAP LINE (see pc_nook_house.h): one extra page appended to the armed vanilla thanks message ---- */
static int s_map_arm_idx = -1;
static int s_map_patched = 0;

void pc_nook_map_line_arm(int msg_index) {
    s_map_arm_idx = msg_index;
}

void pc_nook_map_line_clear(void) {
    s_map_arm_idx = -1;
}

int pc_nook_map_line_armed(void) {
    return s_map_arm_idx;
}

int pc_nook_map_line_patched_count(void) {
    return s_map_patched;
}

/* the vanilla message must end exactly with MSGEND (7F 00): that END becomes "button, clear, map page, END". Returns the new length, 0 = untouched. */
static int nk_map_line_patch(unsigned char* data, int len, int cap) {
    unsigned char tmp[160];
    NkBuf b;
    if (data == NULL || len < 2 || data[len - 2] != NK_CC || data[len - 1] != NK_END) {
        return 0;
    }
    b.d = tmp;
    b.n = 0;
    b.cap = (int)sizeof(tmp);
    b.err = 0;
    nk_page(&b);
    nk_text(&b, "Oh, and one more thing!");
    nk_pause(&b, 6);
    nk_text(&b, "\nHere is a map of the town,");
    nk_pause(&b, 6);
    nk_text(&b, "\non the house, hm?");
    nk_code(&b, NK_END);
    if (b.err || len - 2 + b.n > cap) {
        return 0;
    }
    memcpy(&data[len - 2], tmp, (size_t)b.n);
    return len - 2 + b.n;
}

/* Patches the loaded vanilla first page: SETSELSTR4 (7F 18 + 4 string ids) becomes SETSELSTR5 (7F 79 + 5 ids) with the work choice inserted before the last one. */
/* Dresser storage (worker 3): a ONE-SHOT arm for the next load of one dresser message (empty / 1-item / 2-item owner dresser): its choice list gets
 * "More storage" (PC_NOOK_SEL_DRESSER_MORE) inserted right before the last (cancel) choice. Armed by ac_my_room only when pc_net_game_dresser_available(). */
static int s_dr_arm_idx = -1, s_dr_patched_idx = -1, s_dr_more_choice = -1;

void pc_dresser_msg_arm(int index) {
    s_dr_arm_idx = index;
    s_dr_patched_idx = -1;
    s_dr_more_choice = -1;
}

int pc_dresser_msg_more_choice(int index) {
    return (index >= 0 && s_dr_patched_idx == index) ? s_dr_more_choice : -1;
}

static int nk_dresser_patch(unsigned char* data, int len, int cap) {
    static const unsigned char k_sel[7] = { 0, 0, NK_SEL2, NK_SEL3, NK_SEL4, NK_SEL5, NK_SEL6 };
    int i, k, last;
    if (data == NULL || len < 4 || len + 2 > cap) {
        return 0;
    }
    for (i = 0; i + 1 < len; i++) {
        if (data[i] != NK_CC) {
            continue;
        }
        for (k = 2; k <= 5; k++) {
            if (data[i + 1] == k_sel[k]) {
                break;
            }
        }
        if (k > 5) {
            continue;
        }
        last = i + 2 + 2 * (k - 1); /* the cancel id (the last of the k ids) */
        if (last + 2 > len) {
            return 0;
        }
        memmove(&data[last + 2], &data[last], (size_t)(len - last));
        data[last] = (unsigned char)((PC_NOOK_SEL_DRESSER_MORE >> 8) & 0xFF);
        data[last + 1] = (unsigned char)(PC_NOOK_SEL_DRESSER_MORE & 0xFF);
        data[i + 1] = k_sel[k + 1];
        s_dr_more_choice = k - 1;
        return len + 2;
    }
    return 0;
}

/* Guest mailbox (the post office girl): while armed for message `index` (the greeting / "anything else" row of the post office, set by the actor for a READY guest), EVERY
 * load of that message gets "Check my mail." (PC_NOOK_SEL_CHECK_MAIL) inserted right before its last (cancel) choice; the arm stays until the actor disarms it (-1) at the
 * end of the conversation, so a reload of the same row is patched the same way. The choice index is recorded per patched message. */
static int s_pm_arm_idx = -1, s_pm_patched_idx = -1, s_pm_choice = -1;

void pc_post_mail_msg_arm(int index) {
    s_pm_arm_idx = index;
    if (index < 0) {
        s_pm_patched_idx = -1;
        s_pm_choice = -1;
    }
}

int pc_post_mail_more_choice(int index) {
    return (index >= 0 && s_pm_patched_idx == index) ? s_pm_choice : -1;
}

static int nk_post_mail_patch(unsigned char* data, int len, int cap) {
    static const unsigned char k_sel[7] = { 0, 0, NK_SEL2, NK_SEL3, NK_SEL4, NK_SEL5, NK_SEL6 };
    int i, k, last;
    if (data == NULL || len < 4 || len + 2 > cap) {
        return 0;
    }
    for (i = 0; i + 1 < len; i++) {
        if (data[i] != NK_CC) {
            continue;
        }
        for (k = 2; k <= 5; k++) {
            if (data[i + 1] == k_sel[k]) {
                break;
            }
        }
        if (k > 5) {
            continue;
        }
        last = i + 2 + 2 * (k - 1); /* the cancel id (the last of the k ids) */
        if (last + 2 > len) {
            return 0;
        }
        memmove(&data[last + 2], &data[last], (size_t)(len - last));
        data[last] = (unsigned char)((PC_NOOK_SEL_CHECK_MAIL >> 8) & 0xFF);
        data[last + 1] = (unsigned char)(PC_NOOK_SEL_CHECK_MAIL & 0xFF);
        data[i + 1] = k_sel[k + 1];
        s_pm_choice = k - 1;
        return len + 2;
    }
    return 0;
}

int pc_nook_msg_patch(int index, unsigned char* data, int len, int cap) {
    PCWorkView v;
    int i, id;
    if (s_pm_arm_idx >= 0 && index == s_pm_arm_idx) {
        const int n = nk_post_mail_patch(data, len, cap);
        if (n > 0) {
            s_pm_patched_idx = index;
            printf("[NET][GUEST][MAIL] post office message 0x%04X: 'Check my mail.' added as choice %d (len %d -> %d)\n", index, s_pm_choice, len, n);
        } else {
            s_pm_patched_idx = -1;
            printf("[NET][GUEST][MAIL] post office message 0x%04X: no choice list found -- not patched\n", index);
        }
        return n;
    }
    if (s_dr_arm_idx >= 0 && index == s_dr_arm_idx) {
        int n;
        s_dr_arm_idx = -1; /* one-shot */
        n = nk_dresser_patch(data, len, cap);
        if (n > 0) {
            s_dr_patched_idx = index;
            printf("[NET][DRESSER] dresser message 0x%04X: 'More storage' added as choice %d (len %d -> %d)\n", index, s_dr_more_choice, len, n);
        } else {
            printf("[NET][DRESSER] dresser message 0x%04X: no choice list found -- not patched\n", index);
        }
        return n;
    }
    if (s_map_arm_idx >= 0 && index == s_map_arm_idx) {
        int n;
        s_map_arm_idx = -1; /* one-shot: consumed by the first load of the armed message */
        n = nk_map_line_patch(data, len, cap);
        if (n > 0) {
            s_map_patched++;
            printf("[NET][SHOP] nook dialogue: map line appended to message 0x%04X (len %d -> %d)\n", index, len, n);
        } else {
            printf("[NET][SHOP] nook dialogue: map line skipped for message 0x%04X (it does not end with MSGEND)\n", index);
        }
        return n; /* never the first page (0x1092 is not a thanks row) */
    }
    if (index != s_first_page_id) {
        return 0;
    }
    s_first_page_work = 0;
    if (data == NULL || len < 4 || len + 2 > cap || !pc_net_game_work_offer_available()) {
        return 0;
    }
    for (i = 0; i + 11 < len; i++) {
        if (data[i] == NK_CC && data[i + 1] == NK_SEL4) {
            id = (pc_net_game_work_view(&v) && v.state == PC_WORK_STATE_ACTIVE) ? PC_NOOK_SEL_JOB : PC_NOOK_SEL_WORK;
            memmove(&data[i + 10], &data[i + 8], (size_t)(len - (i + 8))); /* the last (cancel) id and everything after move 2 bytes up ... */
            data[i + 1] = NK_SEL5;
            data[i + 8] = (unsigned char)((id >> 8) & 0xFF); /* ... and the work / job choice takes the 4th place (index 3) */
            data[i + 9] = (unsigned char)(id & 0xFF);
            s_first_page_work = 1;
            return len + 2;
        }
    }
    return 0;
}

int pc_nook_msg_build(int id, unsigned char* dst, int cap) {
    NkBuf b;
    char price[16], tmp[96];
    int ids[6], n, i;
    if (!pc_nook_msg_id_valid(id) || dst == NULL) {
        return 0;
    }
    b.d = dst;
    b.n = 0;
    b.cap = cap;
    b.err = 0;
    nk_price(price, sizeof(price));
    switch (id - MSG_MAX) {
        case PC_NOOK_MSG_INTRO:
            nk_text(&b, "Welcome, welcome!");
            nk_pause(&b, 8);
            nk_text(&b, "\nI'm Tom Nook, the owner\nof this shop, hm?");
            nk_page(&b);
            nk_text(&b, "I see you're new in town,");
            nk_pause(&b, 6);
            nk_text(&b, "\nand you have no house\nof your own yet.");
            nk_page(&b);
            nk_text(&b, "Would you like to buy");
            nk_pause(&b, 6);
            nk_text(&b, "\na house here in town, hm?");
            ids[0] = NK_SEL_YES;
            ids[1] = NK_SEL_NO;
            nk_choice(&b, ids, 2);
            break;
        case PC_NOOK_MSG_OTHER: /* the vanilla 0x3E07 text and choices, plus "Buy a house." before the last one */
            nk_text(&b, "As far as other things go,\nthis is all I have to offer.");
            ids[0] = NK_SEL_TURNIPS;
            ids[1] = NK_SEL_HEARCODE;
            ids[2] = NK_SEL_SAYCODE;
            ids[3] = PC_NOOK_SEL_BUY_HOUSE;
            ids[4] = PC_NOOK_SEL_REFRESH;
            ids[5] = NK_SEL_HANGON;
            nk_choice(&b, ids, 6);
            break;
        case PC_NOOK_MSG_OTHER_R: /* the vanilla 0x3E07 text and choices, plus "Refresh shop." before the last one */
            nk_text(&b, "As far as other things go,\nthis is all I have to offer.");
            ids[0] = NK_SEL_TURNIPS;
            ids[1] = NK_SEL_HEARCODE;
            ids[2] = NK_SEL_SAYCODE;
            ids[3] = PC_NOOK_SEL_REFRESH;
            ids[4] = NK_SEL_HANGON;
            nk_choice(&b, ids, 5);
            break;
        case PC_NOOK_MSG_RESTOCK_ASK:
            nk_text(&b, "A fresh set of goods,");
            nk_pause(&b, 6);
            nk_text(&b, "\nhm? That is 500 Bells.");
            nk_page(&b);
            nk_text(&b, "The shop closes for one");
            nk_pause(&b, 6);
            nk_text(&b, "\nminute, and everyone inside\nis shown out.");
            nk_page(&b);
            nk_text(&b, "Shall we do it?");
            ids[0] = NK_SEL_YES;
            ids[1] = NK_SEL_NO;
            nk_choice(&b, ids, 2);
            break;
        case PC_NOOK_MSG_RESTOCK_DECLINE:
            nk_text(&b, "Very well, very well!");
            nk_pause(&b, 8);
            nk_text(&b, "\nJust ask me again under\nOther things, hm?");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_RESTOCK_THANKS:
            nk_text(&b, "Splendid! 500 Bells,");
            nk_pause(&b, 6);
            nk_text(&b, "\nthank you very much.");
            nk_page(&b);
            nk_text(&b, "I must close up and");
            nk_pause(&b, 6);
            nk_text(&b, "\nrestock the shelves.\nBack in a minute, hm?");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_RESTOCK_NOFUNDS:
            nk_text(&b, "Oh dear, you don't have");
            nk_pause(&b, 6);
            nk_text(&b, "\nenough Bells. A restock\ncosts 500 Bells.");
            nk_page(&b);
            nk_text(&b, "Nothing was charged.");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_RESTOCK_BUSY:
            nk_text(&b, "The shop is restocking");
            nk_pause(&b, 6);
            nk_text(&b, "\nright now, hm!");
            nk_page(&b);
            nk_text(&b, "Nothing was charged.");
            nk_pause(&b, 6);
            nk_text(&b, "\nPlease ask again in a bit!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_RESTOCK_FAILED:
            nk_text(&b, "Oh my, something went");
            nk_pause(&b, 6);
            nk_text(&b, "\nwrong with the order.");
            nk_page(&b);
            nk_text(&b, "Nothing was charged, hm.");
            nk_pause(&b, 6);
            nk_text(&b, "\nPlease try again later!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_RESTOCK_LINKLOST:
            nk_text(&b, "Oh my, the line went quiet");
            nk_pause(&b, 6);
            nk_text(&b, "\nin the middle of the order.");
            nk_page(&b);
            nk_text(&b, "I cannot say whether it");
            nk_pause(&b, 6);
            nk_text(&b, "\nwent through. Please check\nyour wallet, then ask me!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_WORK_JOB: {
            /* the job row of the character's CURRENT job (view set by the actor): what to do next and the Nook reward, then the choices of pc_nook_work_row() */
            int acts[4];
            int n = pc_nook_work_row(&s_wk_view, acts, 4);
            nk_text(&b, "Work, hm? Splendid!");
            nk_pause(&b, 6);
            switch (s_wk_view.job_type) {
                case PC_WORK_JOB_FRUIT:
                    nk_text(&b, "\nBring me ");
                    nk_text(&b, nk_work_item_name(s_wk_view.obj_item));
                    nk_text(&b, "\nfrom this town.");
                    break;
                case PC_WORK_JOB_FETCH_VILLAGER:
                    nk_text(&b, "\n");
                    nk_work_name(&b);
                    nk_text(&b, " has ");
                    nk_text(&b, nk_work_item_name(s_wk_view.obj_item));
                    nk_text(&b, "\nfor me. Ask for it, then\nbring it here.");
                    break;
                default:
                    if (s_wk_view.obj_state == PC_WORK_OBJ_START) {
                        nk_text(&b, "\nI have a parcel for ");
                        nk_work_name(&b);
                        nk_text(&b, ".\nCan you take it over?");
                    } else if (s_wk_view.obj_state == PC_WORK_OBJ_CARRYING) {
                        nk_text(&b, "\nHave you handed the parcel\nto ");
                        nk_work_name(&b);
                        nk_text(&b, " yet?");
                    } else {
                        nk_text(&b, "\nYou gave it to ");
                        nk_work_name(&b);
                        nk_text(&b, "?\nExcellent!");
                    }
                    break;
            }
            nk_page(&b);
            snprintf(tmp, sizeof(tmp), "I'll pay %u Bells for it.", s_wk_view.reward);
            nk_text(&b, tmp);
            nk_pause(&b, 6);
            nk_text(&b, "\nWhat would you like to do?");
            for (i = 0; i < n; i++) {
                ids[i] = acts[i] == PC_NOOK_WACT_HAND ? PC_NOOK_SEL_WORK_HERE : acts[i] == PC_NOOK_WACT_TAKE ? PC_NOOK_SEL_WORK_TAKE : acts[i] == PC_NOOK_WACT_REPORT ? PC_NOOK_SEL_WORK_REPORT
                       : acts[i] == PC_NOOK_WACT_QUIT ? PC_NOOK_SEL_WORK_LEAVE : PC_NOOK_SEL_WORK_LATER;
            }
            nk_choice(&b, ids, n);
            break;
        }
        case PC_NOOK_MSG_WORK_DONE:
            nk_text(&b, "Excellent! Just what I");
            nk_pause(&b, 6);
            nk_text(&b, "\nneeded.");
            nk_page(&b);
            snprintf(tmp, sizeof(tmp), "Here are your %u Bells.", s_wk_view.reward);
            nk_text(&b, tmp);
            nk_pause(&b, 6);
            nk_text(&b, "\nCome back any time for\nmore work!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_WORK_NOITEM:
            nk_text(&b, "Hm? You do not have ");
            nk_text(&b, nk_work_item_name(s_wk_view.obj_item));
            nk_text(&b, ",");
            nk_pause(&b, 6);
            nk_text(&b, "\nnot yet. Come back when\nyou do!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_WORK_LATER:
            nk_text(&b, "Very well. Come back to");
            nk_pause(&b, 6);
            nk_text(&b, "\nme whenever you like, hm?");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_WORK_LEFT:
            nk_text(&b, "All right, no more work");
            nk_pause(&b, 6);
            nk_text(&b, "\nfor now. Thank you for\nyour help!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_WORK_FAILED:
            nk_text(&b, "Hm, something is off with");
            nk_pause(&b, 6);
            nk_text(&b, "\nthe job. Nothing was lost.\nPlease ask me again.");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_WORK_LINKLOST:
            nk_text(&b, "The line went quiet.");
            nk_pause(&b, 6);
            nk_text(&b, "\nI cannot say if the work\nwas recorded. Ask me again.");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_WORK_FULL:
            nk_text(&b, "Your wallet is full!");
            nk_pause(&b, 6);
            nk_text(&b, "\nSpend some Bells and come\nback, hm?");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_WORK_PARCEL:
            nk_text(&b, "Here is the parcel for ");
            nk_work_name(&b);
            nk_text(&b, ".");
            nk_pause(&b, 6);
            nk_text(&b, "\nPlease hand it over and\nthen report back to me.");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_WORK_V_GIVE:
            nk_text(&b, "Oh, Nook sent you for ");
            nk_text(&b, nk_work_item_name(s_wk_view.obj_item));
            nk_text(&b, "?");
            nk_pause(&b, 6);
            nk_text(&b, "\nHere you go! Tell him I said\nhello.");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_WORK_V_RECEIVE:
            nk_text(&b, "Oh! A parcel from Nook");
            nk_pause(&b, 6);
            nk_text(&b, "\nfor me? Thank you!");
            if (s_wk_view.tip_kind == PC_WORK_TIP_BELLS) {
                nk_page(&b);
                snprintf(tmp, sizeof(tmp), "Here are %u Bells for", s_wk_view.tip_value);
                nk_text(&b, tmp);
                nk_text(&b, "\nyour trouble.");
            } else if (s_wk_view.tip_kind == PC_WORK_TIP_ITEM) {
                nk_page(&b);
                nk_text(&b, "Please take ");
                nk_text(&b, nk_work_item_name((int)s_wk_view.tip_value));
                nk_text(&b, "\nfor your trouble.");
            }
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_EVNPC_GIFT_GONE:
            nk_text(&b, "Hm? I gave my gift away");
            nk_pause(&b, 6);
            nk_text(&b, "\nto someone else, I'm afraid.\nMaybe next time!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_PORTER_STATION:
            nk_text(&b, "Welcome to the station!");
            nk_pause(&b, 6);
            nk_text(&b, "\nThis station only serves\nthis town, you see.");
            nk_page(&b);
            nk_text(&b, "Visits with other players");
            nk_pause(&b, 6);
            nk_text(&b, "\nare arranged online, by\nconnecting to a host.");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_DRESSER_REFUSED:
            nk_text(&b, "The storage could not be");
            nk_pause(&b, 6);
            nk_text(&b, "\nused just now. Nothing\nwas moved.");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_WORK_V_FAIL:
            nk_text(&b, "Hm? I do not know what");
            nk_pause(&b, 6);
            nk_text(&b, "\nthis is about.");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_UPGRADE_ANNOUNCE:
            nk_minutes(tmp, sizeof(tmp), pc_shop_upgrade_minutes_total());
            nk_text(&b, "Ah, ");
            nk_code(&b, NK_PLAYERNAME);
            nk_text(&b, "! Welcome!");
            nk_pause(&b, 8);
            nk_text(&b, "\nBusiness has been booming\nlately, hm!");
            nk_page(&b);
            nk_text(&b, "I've been thinking about");
            nk_pause(&b, 6);
            nk_text(&b, "\nupgrading the shop, hm.");
            nk_page(&b);
            nk_text(&b, "A bigger store would");
            nk_pause(&b, 6);
            nk_text(&b, "\nhelp everyone out here\nin town!");
            nk_page(&b);
            nk_text(&b, "So the shop will close");
            nk_pause(&b, 6);
            nk_text(&b, "\nfor ");
            nk_text(&b, tmp);
            nk_text(&b, " while\nthe work is done.");
            nk_page(&b);
            nk_text(&b, "Everyone inside will be");
            nk_pause(&b, 6);
            nk_text(&b, "\nshown out. Please come\nback soon, hm?");
            ids[0] = PC_NOOK_SEL_UP_GREAT;
            ids[1] = PC_NOOK_SEL_UP_OK;
            nk_choice(&b, ids, 2);
            break;
        case PC_NOOK_MSG_UPGRADE_CLOSING:
            nk_minutes(tmp, sizeof(tmp), pc_shop_upgrade_minutes_total());
            nk_text(&b, "Splendid! I'll close up");
            nk_pause(&b, 6);
            nk_text(&b, "\nright away.");
            nk_page(&b);
            nk_text(&b, "See you in ");
            nk_text(&b, tmp);
            nk_text(&b, ",");
            nk_pause(&b, 6);
            nk_text(&b, "\nhm! Do come back soon!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_UPGRADE_LATER:
            nk_text(&b, "Hm, on second thought...");
            nk_pause(&b, 6);
            nk_text(&b, "\nlet me look over the\nplans a little longer.");
            nk_page(&b);
            nk_text(&b, "Let's talk about it");
            nk_pause(&b, 6);
            nk_text(&b, "\nanother time, hm!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_UPGRADE_DOOR:
            nk_minutes(tmp, sizeof(tmp), pc_shop_upgrade_minutes_left());
            nk_text(&b, "Sorry, sorry!");
            nk_pause(&b, 8);
            nk_text(&b, "\nThe shop is closed for\nits upgrade.");
            nk_page(&b);
            nk_text(&b, "Please come back in about");
            nk_pause(&b, 6);
            nk_text(&b, "\n");
            nk_text(&b, tmp);
            nk_text(&b, ", hm?");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_POST_NOMAIL: /* guest mailbox: the post office girl, nothing waiting for the guest */
            nk_text(&b, "Let me check...");
            nk_pause(&b, 10);
            nk_text(&b, "\nI'm sorry, there's no mail\nfor you right now.");
            nk_page(&b);
            nk_text(&b, "We keep your letters here");
            nk_pause(&b, 6);
            nk_text(&b, "\nfor you, so do come back\nand check again!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_POST_DONE: /* guest mailbox: after the mailbox overlay closed */
            nk_text(&b, "There you go!");
            nk_pause(&b, 6);
            nk_text(&b, "\nAny letters you leave here\nwill stay safe with us.");
            nk_page(&b);
            nk_text(&b, "Please come again!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_UPG_WAIT: /* house upgrades (worker C): the talk start while the host answers; the actor sets the continue message (the vanilla row / a refusal row) */
            nk_text(&b, "Ah, ");
            nk_code(&b, NK_PLAYERNAME);
            nk_text(&b, "! Hello, hello!");
            nk_pause(&b, 8);
            nk_text(&b, "\nJust a moment, let me\ncheck my records, hm...");
            nk_code(&b, NK_BTN);
            nk_code(&b, NK_FORCENEXT);
            nk_code(&b, NK_CONTINUE);
            break;
        case PC_NOOK_MSG_UPG_FAILED:
            nk_text(&b, "Hm, I'm afraid I can't");
            nk_pause(&b, 6);
            nk_text(&b, "\ntake care of that just now.");
            nk_page(&b);
            nk_text(&b, "Nothing has changed, hm.");
            nk_pause(&b, 6);
            nk_text(&b, "\nPlease come back later!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_UPG_LINKLOST:
            nk_text(&b, "Oh my, the line went quiet");
            nk_pause(&b, 6);
            nk_text(&b, "\nin the middle of the\npaperwork.");
            nk_page(&b);
            nk_text(&b, "I cannot say whether it");
            nk_pause(&b, 6);
            nk_text(&b, "\nwent through. Please ask\nme again in a bit, hm?");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_UPG_BUSY:
            nk_text(&b, "Hm, my records for your");
            nk_pause(&b, 6);
            nk_text(&b, "\nhouse are being updated\nright now.");
            nk_page(&b);
            nk_text(&b, "Please ask me again in");
            nk_pause(&b, 6);
            nk_text(&b, "\na moment, hm?");
            nk_code(&b, NK_END);
            break;
        /* worker E: shared event NPC refusals (the visitor speaks them; closing rows) */
        case PC_NOOK_MSG_EVC_SOLD:
            nk_text(&b, "Oh! Somebody else just");
            nk_pause(&b, 6);
            nk_text(&b, "\ngot that one, I'm afraid.\nIt's all gone now!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_EVC_CLAIMED:
            nk_text(&b, "Hm? You already got");
            nk_pause(&b, 6);
            nk_text(&b, "\nsomething from me on\nthis visit, you know!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_EVC_FUNDS:
            nk_text(&b, "Hey now, you don't have");
            nk_pause(&b, 6);
            nk_text(&b, "\nenough Bells for that.\nNo money, no deal!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_EVC_FAILED:
            nk_text(&b, "Hmm, something got in");
            nk_pause(&b, 6);
            nk_text(&b, "\nthe way just now.\nLet's try that again later.");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_EVC_OLDHOST:
            nk_text(&b, "Sorry, I can't do that");
            nk_pause(&b, 6);
            nk_text(&b, "\nfor you in this town\nright now.");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_EVC_WISP_DONE:
            nk_text(&b, "Oh! Somebody already");
            nk_pause(&b, 6);
            nk_text(&b, "\nbrought my spirits back!\nKeep those, then.");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_EVC_NO_SPIRITS:
            nk_text(&b, "Hm? That's not all five");
            nk_pause(&b, 6);
            nk_text(&b, "\nof my spirits after all.\nPlease keep looking!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_SELL_BASKET: { /* Nook bulk sell basket: the quote of the closed basket (count + total), Yes. = sell everything, No. = back to "anything else?" */
            char bells[24];
            nk_text(&b, "Let me see here...");
            nk_pause(&b, 6);
            snprintf(tmp, sizeof(tmp), "\nThat's %d item%s in all.", s_sell_count, s_sell_count == 1 ? "" : "s");
            nk_text(&b, tmp);
            nk_page(&b);
            if (s_sell_total == 0u) {
                nk_text(&b, "I can't pay anything for");
                nk_pause(&b, 6);
                nk_text(&b, s_sell_count == 1 ? "\nthat, but I'll take it\noff your hands. All right?" : "\nthese, but I'll take them\noff your hands. All right?");
            } else {
                nk_text(&b, "I'll give you");
                nk_pause(&b, 6);
                snprintf(tmp, sizeof(tmp), "\n%s Bells for\n%s. Deal?", pc_sell_format_bells(bells, sizeof(bells), s_sell_total), s_sell_count == 1 ? "it" : "the lot");
                nk_text(&b, tmp);
            }
            ids[0] = NK_SEL_YES;
            ids[1] = NK_SEL_NO;
            nk_choice_plain(&b, ids, 2);
            break;
        }
        case PC_NOOK_MSG_RESTOCK_DOOR:
            nk_text(&b, "Sorry, sorry!");
            nk_pause(&b, 8);
            nk_text(&b, "\nWe are restocking the\nshelves right now.");
            nk_page(&b);
            nk_text(&b, "Please come back in about");
            nk_pause(&b, 6);
            nk_text(&b, "\na minute, hm?");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_PICK:
            nk_text(&b, "Wonderful!");
            nk_pause(&b, 6);
            snprintf(tmp, sizeof(tmp), "\nA house costs %s Bells,\npaid all at once, hm?", price);
            nk_text(&b, tmp);
            nk_page(&b);
            nk_text(&b, "Which house would you like?");
            n = 0;
            for (i = 0; i < s_pick_n && i < PC_RESIDENCE_HOUSES; i++) {
                ids[n++] = PC_NOOK_SEL_HOUSE1 + (s_pick[i] % PC_RESIDENCE_HOUSES);
            }
            if (s_pick_n >= 2) {
                ids[n++] = PC_NOOK_SEL_ANY_HOUSE;
            }
            ids[n++] = NK_SEL_NEVERMIND;
            if (n < 2) {
                ids[n++] = NK_SEL_NEVERMIND;
            }
            nk_choice(&b, ids, n);
            break;
        case PC_NOOK_MSG_CONFIRM:
            if (s_confirm >= 0) {
                snprintf(tmp, sizeof(tmp), "House %d, then!", (s_confirm % PC_RESIDENCE_HOUSES) + 1);
            } else {
                snprintf(tmp, sizeof(tmp), "Any free house, then!");
            }
            nk_text(&b, tmp);
            nk_pause(&b, 6);
            snprintf(tmp, sizeof(tmp), "\nThat comes to %s Bells,\npayable right now, hm?", price);
            nk_text(&b, tmp);
            nk_page(&b);
            nk_text(&b, "Shall we make it official?");
            ids[0] = NK_SEL_YES;
            ids[1] = NK_SEL_NO;
            nk_choice(&b, ids, 2);
            break;
        case PC_NOOK_MSG_DECLINE:
            nk_text(&b, "I see, I see.");
            nk_pause(&b, 8);
            nk_text(&b, "\nNo pressure at all!");
            nk_page(&b);
            nk_text(&b, "If you change your mind,");
            nk_pause(&b, 6);
            nk_text(&b, "\njust ask me under Other\nthings, hm?");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_NOFUNDS:
            nk_text(&b, "Oh dear, you don't have");
            nk_pause(&b, 6);
            snprintf(tmp, sizeof(tmp), "\nenough Bells for that.\nA house costs %s Bells.", price);
            nk_text(&b, tmp);
            nk_page(&b);
            nk_text(&b, "Do come back when you can");
            nk_pause(&b, 6);
            nk_text(&b, "\nafford it, hm?");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_NOLOT:
            nk_text(&b, "I'm terribly sorry,");
            nk_pause(&b, 6);
            nk_text(&b, "\nbut there are no lots\navailable in this town.");
            nk_page(&b);
            nk_text(&b, "Nothing was charged.");
            nk_pause(&b, 6);
            nk_text(&b, "\nPlease do ask me again!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_FAILED:
            nk_text(&b, "Oh my, something went");
            nk_pause(&b, 6);
            nk_text(&b, "\nwrong with the paperwork.");
            nk_page(&b);
            nk_text(&b, "Nothing was charged, hm.");
            nk_pause(&b, 6);
            nk_text(&b, "\nPlease try again later!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_LINKLOST:
            nk_text(&b, "Oh my, the line went quiet");
            nk_pause(&b, 6);
            nk_text(&b, "\nin the middle of the\npaperwork.");
            nk_page(&b);
            nk_text(&b, "I cannot say whether it");
            nk_pause(&b, 6);
            nk_text(&b, "\nwent through. Please check\nyour wallet, then ask me!");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_NAMETAKEN:
            nk_text(&b, "I'm terribly sorry,");
            nk_pause(&b, 6);
            nk_text(&b, "\nbut a resident of this town\nalready has your name.");
            nk_page(&b);
            nk_text(&b, "Nothing was charged.");
            nk_pause(&b, 6);
            nk_text(&b, "\nPlease come back with\nanother name, hm?");
            nk_code(&b, NK_END);
            break;
        case PC_NOOK_MSG_THANKS:
            nk_text(&b, "Splendid, splendid!");
            nk_pause(&b, 8);
            nk_text(&b, "\nThe house is yours,\n");
            nk_code(&b, NK_PLAYERNAME);
            nk_text(&b, "!");
            nk_page(&b);
            nk_text(&b, "I'm filing the paperwork");
            nk_pause(&b, 6);
            nk_text(&b, "\nright now.\nThank you very much!");
            nk_code(&b, NK_END);
            break;
        default:
            return 0;
    }
    return b.err ? 0 : b.n;
}

int pc_nook_sel_build(int id, unsigned char* dst16) {
    char tmp[32];
    int i, len;
    if (!pc_nook_sel_id_valid(id) || dst16 == NULL) {
        return 0;
    }
    switch (id) {
        case PC_NOOK_SEL_BUY_HOUSE:
            snprintf(tmp, sizeof(tmp), "Buy a house.");
            break;
        case PC_NOOK_SEL_ANY_HOUSE:
            snprintf(tmp, sizeof(tmp), "Any house.");
            break;
        case PC_NOOK_SEL_REFRESH:
            snprintf(tmp, sizeof(tmp), "Refresh shop.");
            break;
        case PC_NOOK_SEL_WORK:
            snprintf(tmp, sizeof(tmp), "I'd like to work"); /* the choice string window holds 16 characters: no room for the final period */
            break;
        case PC_NOOK_SEL_WORK_HERE:
            snprintf(tmp, sizeof(tmp), "Here it is!");
            break;
        case PC_NOOK_SEL_WORK_LATER:
            snprintf(tmp, sizeof(tmp), "I'll get to it.");
            break;
        case PC_NOOK_SEL_WORK_LEAVE:
            snprintf(tmp, sizeof(tmp), "I quit the job");
            break;
        case PC_NOOK_SEL_JOB:
            snprintf(tmp, sizeof(tmp), "Check my job");
            break;
        case PC_NOOK_SEL_WORK_TAKE:
            snprintf(tmp, sizeof(tmp), "Take the parcel");
            break;
        case PC_NOOK_SEL_WORK_REPORT:
            snprintf(tmp, sizeof(tmp), "I delivered it.");
            break;
        case PC_NOOK_SEL_DRESSER_MORE:
            snprintf(tmp, sizeof(tmp), "More storage"); /* dresser storage (worker 3): the host-side pool of the own dresser */
            break;
        case PC_NOOK_SEL_UP_GREAT:
            snprintf(tmp, sizeof(tmp), "Sounds great!"); /* shop upgrade announcement */
            break;
        case PC_NOOK_SEL_UP_OK:
            snprintf(tmp, sizeof(tmp), "All right.");
            break;
        case PC_NOOK_SEL_CHECK_MAIL:
            snprintf(tmp, sizeof(tmp), "Check my mail."); /* guest mailbox: the post office girl */
            break;
        default:
            snprintf(tmp, sizeof(tmp), "House %d.", id - PC_NOOK_SEL_HOUSE1 + 1);
            break;
    }
    len = (int)strlen(tmp);
    for (i = 0; i < 16; i++) {
        dst16[i] = (unsigned char)(i < len ? tmp[i] : ' ');
    }
    return 1;
}

static int nk_char_ok(int c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == ' ' || c == '!' || c == '\'' || c == ',' || c == '-' || c == '.' || c == '?' || c == ':';
}

int pc_nook_house_selftest(void) {
    static unsigned char buf[1536];
    int which, i, bad = 0, h4[PC_RESIDENCE_HOUSES] = { 0, 1, 2, 3 };
    pc_nook_house_set_pick(h4, PC_RESIDENCE_HOUSES);
    pc_nook_house_set_confirm(2);
    {   /* Nook bulk sell basket: the quote in its three shapes (one item, a free lot, the largest plausible total) stays in the charset and builds */
        static const int sc[3] = { 1, 4, 15 };
        static const unsigned st[3] = { 250u, 0u, 990005u };
        int k;
        for (k = 0; k < 3; k++) {
            int n, j;
            pc_nook_house_set_sell(sc[k], st[k]);
            n = pc_nook_msg_build(MSG_MAX + PC_NOOK_MSG_SELL_BASKET, buf, sizeof(buf));
            if (n < 2 || buf[n - 2] != NK_CC || buf[n - 1] != NK_CONTINUE) {
                printf("[NOOK] selftest: sell basket quote %d did not build\n", k);
                bad++;
            }
            for (j = 0; j < n; j++) {
                if (buf[j] == NK_CC) {
                    const int code = buf[j + 1];
                    j += (code == NK_PAUSE) ? 2 : (code == NK_SEL2) ? 5 : 1;
                } else if (!nk_char_ok(buf[j]) && buf[j] != NK_NEWLINE) {
                    printf("[NOOK] selftest: sell basket quote %d byte %d = 0x%02X is outside the charset\n", k, j, buf[j]);
                    bad++;
                }
            }
        }
        pc_nook_house_set_sell(3, 1200u);
    }
    {
        PCWorkView v;
        unsigned char nm[8] = {'B', 'o', 'b', ' ', ' ', ' ', ' ', ' '};
        int jt, os;
        memset(&v, 0, sizeof(v));
        v.state = 1;
        v.mode_on = 1;
        v.reward = 400;
        v.tip_value = 150;
        v.obj_item = ITM_FOOD_PEACH;
        for (jt = PC_WORK_JOB_FRUIT; jt <= PC_WORK_JOB_DELIVER_VILLAGER; jt++) {
            for (os = 0; os <= 2; os++) {
                v.job_type = jt;
                v.obj_state = os;
                v.tip_kind = os;
                v.tip_kind = v.tip_kind > 2 ? 2 : v.tip_kind;
                pc_nook_house_set_work_view(&v, nm);
                for (which = PC_NOOK_MSG_WORK_JOB; which < PC_NOOK_MSG_COUNT; which++) {
                    int n = pc_nook_msg_build(MSG_MAX + which, buf, sizeof(buf));
                    if (n < 2 || !(buf[n - 2] == NK_CC && (buf[n - 1] == NK_END || buf[n - 1] == NK_CONTINUE))) {
                        printf("[NOOK] selftest: work message %d (type %d step %d) did not build\n", which, jt, os);
                        bad++;
                    }
                }
            }
        }
        pc_nook_house_set_work_view(&v, nm);
    }
    for (which = 0; which < PC_NOOK_MSG_COUNT; which++) {
        int n = pc_nook_msg_build(MSG_MAX + which, buf, sizeof(buf)), last_ok;
        if (n < 2) {
            printf("[NOOK] selftest: message %d did not build\n", which);
            return 1;
        }
        last_ok = buf[n - 2] == NK_CC && (buf[n - 1] == NK_END || buf[n - 1] == NK_CONTINUE);
        if (!last_ok) {
            bad++;
        }
        for (i = 0; i < n; i++) {
            int c = buf[i];
            if (c == NK_CC) {
                int code = buf[i + 1];
                i += (code == NK_PAUSE) ? 2 : (code == NK_DEMO_NPC0) ? 4 : (code == NK_SEL2) ? 5 : (code == NK_SEL3) ? 7 : (code == NK_SEL4) ? 9 : (code == NK_SEL5) ? 11 : (code == NK_SEL6) ? 13 : 1;
            } else if (!nk_char_ok(c) && c != NK_NEWLINE) {
                printf("[NOOK] selftest: message %d byte %d = 0x%02X is outside the charset\n", which, i, c);
                bad++;
            }
        }
    }
    {   /* guest mailbox: an armed post office row (4 choices) gets "Check my mail." as choice 3 (before the cancel choice), on EVERY load while armed; disarmed: untouched */
        unsigned char p[64] = { 'H', 'i', NK_CC, NK_SEL4, 0, 1, 0, 2, 0, 3, 0, 4, NK_CC, NK_CONTINUE };
        unsigned char q[64];
        int n;
        memcpy(q, p, sizeof(q));
        pc_post_mail_msg_arm(0x31D3);
        if (pc_nook_msg_patch(0x31D2, p, 14, (int)sizeof(p)) != 0 || pc_post_mail_more_choice(0x31D2) != -1) {
            bad++;
        }
        n = pc_nook_msg_patch(0x31D3, p, 14, (int)sizeof(p));
        if (n != 16 || p[3] != NK_SEL5 || p[10] != ((PC_NOOK_SEL_CHECK_MAIL >> 8) & 0xFF) || p[11] != (PC_NOOK_SEL_CHECK_MAIL & 0xFF) || p[12] != 0 || p[13] != 4 ||
            pc_post_mail_more_choice(0x31D3) != 3) {
            printf("[NOOK] selftest: post office mail patch failed\n");
            bad++;
        }
        if (pc_nook_msg_patch(0x31D3, q, 14, (int)sizeof(q)) != 16) { /* a reload while still armed is patched the same way */
            bad++;
        }
        pc_post_mail_msg_arm(-1);
        if (pc_nook_msg_patch(0x31D3, q, 16, (int)sizeof(q)) != 0) { /* disarmed: never patched */
            bad++;
        }
        if (pc_post_mail_more_choice(0x31D3) != -1) {
            bad++;
        }
    }
    {   /* the map line: a message ending with MSGEND gets exactly one extra page, once; one ending with MSGCONTINUE is untouched; both consume the arm */
        unsigned char m[256] = { 'T', 'h', 'a', 'n', 'k', 's', '!', NK_CC, NK_END };
        unsigned char c[256] = { 'T', 'i', 'c', 'k', 'e', 't', NK_CC, NK_CONTINUE };
        int n, pre = s_map_patched, saved = s_map_arm_idx;
        pc_nook_map_line_arm(0x2345);
        if (pc_nook_msg_patch(0x2344, m, 9, (int)sizeof(m)) != 0 || pc_nook_map_line_armed() != 0x2345) { /* another index: neither patched nor consumed */
            bad++;
        }
        n = pc_nook_msg_patch(0x2345, m, 9, (int)sizeof(m));
        if (n <= 9 || m[n - 2] != NK_CC || m[n - 1] != NK_END || m[7] != NK_CC || m[8] != NK_BTN || pc_nook_map_line_armed() != -1 || s_map_patched != pre + 1) {
            bad++;
        }
        for (i = 9; i < n - 2; i++) {
            if (m[i] == NK_CC) {
                i += (m[i + 1] == NK_PAUSE) ? 2 : 1;
            } else if (!nk_char_ok(m[i]) && m[i] != NK_NEWLINE) {
                bad++;
            }
        }
        if (pc_nook_msg_patch(0x2345, m, n, (int)sizeof(m)) != 0) { /* the second load is not armed any more */
            bad++;
        }
        pc_nook_map_line_arm(0x2346);
        if (pc_nook_msg_patch(0x2346, c, 8, (int)sizeof(c)) != 0 || c[6] != NK_CC || c[7] != NK_CONTINUE || pc_nook_map_line_armed() != -1 || s_map_patched != pre + 1) {
            bad++;
        }
        pc_nook_map_line_arm(0x2347);
        memcpy(m, "Thanks!", 7);
        m[7] = NK_CC;
        m[8] = NK_END;
        if (pc_nook_msg_patch(0x2347, m, 9, 10) != 0 || pc_nook_map_line_armed() != -1) { /* no room: untouched, consumed */
            bad++;
        }
        s_map_arm_idx = saved;
    }
    return bad;
}
