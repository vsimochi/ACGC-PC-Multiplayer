/* pc_room_ui.c - ROOMMATES / MOVE-IN: the move-in dialogue INSIDE the vanilla gyroid conversation (see pc_room_ui.h, docs/multiplayer-guest-roadmap.md
 * "Roommates / move-in"). No overlay: the vanilla message window, choice windows, font, talk camera and talk state carry everything.
 *   1. A at the gyroid of a house (ac_haniwa_move.c_inc aHNW_dance -> pc_room_gyroid_intercept), by a GUEST or the house's OWNER: the host is asked first
 *      (message 104 QUERY; a moment, no talk yet). Its INFO ARMS the conversation (pc_room_dlg.c then adds one choice to the gyroid's own menu) and the vanilla
 *      talk starts by itself (m_demo.c g_pc_demo_force_talk_actor). No answer / nothing to offer: the vanilla talk, unarmed.
 *   2. The menu choice (ac_haniwa_move.c_inc talk_with_master / talk_with_guest -> pc_room_gyroid_menu_pick) enters the actor's PC_ROOM action, which runs
 *      pc_room_gyroid_dialog every frame: the generated rows (pc_room_dlg.c), the vanilla Yes / No windows, the host's answer behind "Processing request...".
 *   3. The talk ends (any way): pc_room_gyroid_frame disarms.
 * The other side's news (a request arrived, accepted, moved out...) arrive as host NOTICEs: one line at the top of the screen, QUEUED (each ~5 s, none overwrites
 * another). Nothing here writes game state. */
#include "pc_room_ui.h"
#include "pc_room_dlg.h"
#include "pc_platform.h"
#include "pc_pause_menu.h"
#include "pc_field_authority.h"
#include "m_common_data.h"
#include "m_play.h"
#include "m_controller.h"
#include "m_demo.h"
#include "m_private.h"
#include "m_msg.h"
#include "m_choice.h"
#include "m_field_info.h"
#include "m_house.h"
#include "m_player_lib.h"
#include "ac_haniwa.h" /* aHNW_ACTION_* */
#include <SDL.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PC_ROOM_QUERY_TIMEOUT_MS 1500u /* the A at the gyroid waits this long for the host's INFO, then the vanilla talk starts unarmed */
#define PC_ROOM_OP_TIMEOUT_MS    5000u /* "Processing request..." waits this long for the host's RESULT */
#define PC_ROOM_NOTICE_MS        5000u
#define PC_ROOM_NOTICE_QUEUE     6
#define PC_ROOM_MAX_STEPS        PC_ROOM_MSG_ENTRIES

enum { RC_IDLE = 0, RC_QUERY, RC_ARMED };                 /* the conversation */
enum { DS_NONE = 0, DS_ASK, DS_WAIT, DS_SHOW, DS_DONE };  /* the move-in rows inside it */

void* g_pc_demo_force_talk_actor; /* m_demo.c choice_demo_sub: this actor's TALK request is chosen without a new A press (one shot) */

static int       s_cphase;
static int       s_house = -1;
static void*     s_actor;      /* the gyroid of the conversation (only compared, never dereferenced here) */
static uint32_t  s_seq, s_t0, s_arm_ms, s_requery_ms;
static int       s_requeried, s_talk_seen;
static PCRoomMsg s_info;       /* the host's INFO of this conversation */
static int       s_ds, s_row;  /* the dialogue stage, the PC_ROOM_DLG_* row on screen */
static int       s_op;         /* the op in flight (DS_WAIT) */
static PCRoomMsg s_reply;
static int       s_have_reply;
static int       s_steps[PC_ROOM_MAX_STEPS]; /* the holder's list: indexes into s_info.e (requests first, then roommates) */
static int       s_nsteps, s_step;
static int       s_gone[PC_ROOM_MSG_ENTRIES]; /* denied / removed in this conversation */

static uint32_t rui_now(void) {
    return (uint32_t)SDL_GetTicks();
}

/* ---- the notice line (host NOTICEs): a FIFO, each line ~5 s ---- */
static char     s_nq[PC_ROOM_NOTICE_QUEUE][96];
static int      s_nq_head, s_nq_n;
static uint32_t s_nq_ms; /* the head's first draw (| 1), 0 = not shown yet */

void pc_room_ui_notice_set(const char* text) {
    int tail;
    if (text == NULL || text[0] == '\0') {
        return;
    }
    if (s_nq_n > 0 && strcmp(s_nq[(s_nq_head + s_nq_n - 1) % PC_ROOM_NOTICE_QUEUE], text) == 0) {
        return; /* the same line twice in a row: once */
    }
    if (s_nq_n == PC_ROOM_NOTICE_QUEUE) { /* full: the oldest goes */
        s_nq_head = (s_nq_head + 1) % PC_ROOM_NOTICE_QUEUE;
        s_nq_n--;
        s_nq_ms = 0;
    }
    tail = (s_nq_head + s_nq_n) % PC_ROOM_NOTICE_QUEUE;
    snprintf(s_nq[tail], sizeof(s_nq[tail]), "%s", text);
    s_nq_n++;
    printf("[ROOM][UI] notice: %s (%d queued)\n", text, s_nq_n);
}

const char* pc_room_ui_notice_get(void) {
    while (s_nq_n > 0) {
        if (s_nq_ms == 0) {
            s_nq_ms = rui_now() | 1u;
        }
        if ((int32_t)(rui_now() - s_nq_ms) < (int32_t)PC_ROOM_NOTICE_MS) { /* (signed age: the set time | 1 can lie one ms ahead) */
            return s_nq[s_nq_head];
        }
        s_nq_head = (s_nq_head + 1) % PC_ROOM_NOTICE_QUEUE;
        s_nq_n--;
        s_nq_ms = 0;
    }
    return NULL;
}

int pc_room_ui_is_open(void) {
    return s_cphase != RC_IDLE;
}

/* ---- the conversation ---- */
static void rui_free_name(const uint8_t* n8) {
    u8 nm[PC_ROOM_NAME_LEN];
    memcpy(nm, n8, PC_ROOM_NAME_LEN);
    mMsg_Set_free_str(mMsg_Get_base_window_p(), mMsg_FREE_STR10, nm, PC_ROOM_NAME_LEN);
}

static void rui_reset(const char* why) {
    if (s_cphase != RC_IDLE) {
        printf("[ROOM][UI] gyroid conversation of house %d over (%s)\n", s_house, why);
    }
    pc_room_dlg_arm(0, 0, -1);
    if (g_pc_demo_force_talk_actor != NULL && g_pc_demo_force_talk_actor == s_actor) {
        g_pc_demo_force_talk_actor = NULL;
    }
    s_cphase = RC_IDLE;
    s_ds = DS_NONE;
    s_have_reply = 0;
}

/* the holder's list from s_info: open requests first, then roommates (the ones denied / removed in this conversation are left out) */
static void rui_build_steps(void) {
    int i, pass;
    s_nsteps = 0;
    for (pass = 0; pass < 2; pass++) {
        for (i = 0; i < (int)s_info.count && i < PC_ROOM_MSG_ENTRIES; i++) {
            const int want = pass == 0 ? PC_ROOM_ST_PENDING : PC_ROOM_ST_ROOMMATE;
            if (!s_gone[i] && s_info.e[i].state == want && s_nsteps < PC_ROOM_MAX_STEPS) {
                s_steps[s_nsteps++] = i;
            }
        }
    }
    s_step = 0;
}

static int rui_step_row(int k) {
    return s_info.e[s_steps[k]].state == PC_ROOM_ST_PENDING ? PC_ROOM_DLG_OWNER_REQ_Q : PC_ROOM_DLG_OWNER_RM_Q;
}

/* arms the holder's menus: the inserted choice leads to the first entry of the list (its name in free string 10) or to "no one" */
static void rui_arm_owner(void) {
    int i, pending = 0;
    rui_build_steps();
    for (i = 0; i < s_nsteps; i++) {
        pending += s_info.e[s_steps[i]].state == PC_ROOM_ST_PENDING;
    }
    if (s_nsteps > 0) {
        rui_free_name(s_info.e[s_steps[0]].name);
    }
    pc_room_dlg_arm(PC_ROOM_DLG_ARM_OWNER, pending > 0 ? PC_ROOM_SEL_REQUESTS : PC_ROOM_SEL_ROOMMATES,
                    pc_room_dlg_msg_id(s_nsteps > 0 ? rui_step_row(0) : PC_ROOM_DLG_OWNER_NONE));
}

/* the INFO arrived: 1 = armed (the conversation offers the move-in choice) */
static int rui_arm(const PCRoomMsg* m) {
    s_info = *m;
    memset(s_gone, 0, sizeof(s_gone));
    switch (m->role) {
        case PC_ROOM_ROLE_OWNER:
            rui_arm_owner();
            return 1;
        case PC_ROOM_ROLE_CAN_REQUEST:
        case PC_ROOM_ROLE_BLOCKED: /* "Ask to move in" -> the gyroid says why not */
            rui_free_name(m->owner_name);
            pc_room_dlg_arm(PC_ROOM_DLG_ARM_GUEST, PC_ROOM_SEL_ASK, -1);
            return 1;
        case PC_ROOM_ROLE_PENDING:
            rui_free_name(m->owner_name);
            pc_room_dlg_arm(PC_ROOM_DLG_ARM_GUEST, PC_ROOM_SEL_WITHDRAW, -1);
            return 1;
        case PC_ROOM_ROLE_ROOMMATE:
            rui_free_name(m->owner_name);
            pc_room_dlg_arm(PC_ROOM_DLG_ARM_GUEST, PC_ROOM_SEL_MOVE_OUT, -1);
            return 1;
        default:
            return 0; /* nobody holds it / another resident: the vanilla conversation */
    }
}

static void rui_continue(int row) {
    mMsg_Set_continue_msg_num(mMsg_Get_base_window_p(), pc_room_dlg_msg_id(row));
}

static int rui_on_row(int row) {
    mMsg_Window_c* win = mMsg_Get_base_window_p();
    return mMsg_Get_msg_num(win) == pc_room_dlg_msg_id(row) && mMsg_Check_MainNormalContinue(win) == TRUE;
}

static void rui_ask(int row) {
    rui_continue(row);
    s_row = row;
    s_ds = DS_ASK;
}

static void rui_show(int row) {
    rui_continue(row);
    s_row = row;
    s_ds = DS_SHOW;
}

/* a closing row (MSGEND): the talk ends after it */
static int rui_close_with(int msg_id) {
    mMsg_Set_continue_msg_num(mMsg_Get_base_window_p(), msg_id);
    s_ds = DS_DONE;
    return 1;
}

/* the decision goes to the host; "Processing request..." until it answers */
static void rui_send(int op, const uint8_t* key) {
    s_have_reply = 0;
    s_op = op;
    s_seq = pc_net_game_room_send(op, s_house, key);
    s_t0 = rui_now();
    printf("[ROOM][UI] op %d sent (seq %u)\n", op, (unsigned)s_seq);
    rui_continue(PC_ROOM_DLG_WAIT);
    s_row = PC_ROOM_DLG_WAIT;
    s_ds = DS_WAIT;
}

/* the holder: the next entry of the list, or back to the gyroid's menu (2) */
static int rui_next_step(void) {
    while (++s_step < s_nsteps && s_gone[s_steps[s_step]]) {
    }
    if (s_step < s_nsteps) {
        rui_free_name(s_info.e[s_steps[s_step]].name);
        rui_ask(rui_step_row(s_step));
        return 0;
    }
    rui_arm_owner(); /* the next menu pick starts from the list as it is now */
    mMsg_Set_continue_msg_num(mMsg_Get_base_window_p(), pc_room_dlg_msg_id(PC_ROOM_DLG_OWNER_MENU));
    s_ds = DS_NONE;
    return 2;
}

int pc_room_gyroid_menu_pick(void* actor, int chosen) {
    const int c = pc_room_dlg_menu_choice();
    if (s_cphase != RC_ARMED || actor == NULL || actor != s_actor || c < 0) {
        return chosen;
    }
    if (chosen != c) {
        return chosen > c ? chosen - 1 : chosen;
    }
    printf("[ROOM][UI] menu choice %d: the move-in rows (role %u)\n", c, (unsigned)s_info.role);
    switch (s_info.role) {
        case PC_ROOM_ROLE_OWNER:
            /* the menu named the first row itself (SETNEXTMSG of the inserted choice) */
            rui_build_steps();
            if (s_nsteps > 0) {
                s_row = rui_step_row(0);
                s_ds = DS_ASK;
            } else {
                s_row = PC_ROOM_DLG_OWNER_NONE;
                s_ds = DS_SHOW;
            }
            break;
        case PC_ROOM_ROLE_CAN_REQUEST: rui_ask(PC_ROOM_DLG_ASK_Q); break;
        case PC_ROOM_ROLE_PENDING: rui_ask(PC_ROOM_DLG_WITHDRAW_Q); break;
        case PC_ROOM_ROLE_ROOMMATE: rui_ask(PC_ROOM_DLG_MOVEOUT_Q); break;
        default: /* BLOCKED: why not */
            pc_room_dlg_set_reason((int)s_info.result != 0 ? (int)s_info.result : PC_ROOM_DLG_REASON_LINK);
            rui_close_with(pc_room_dlg_msg_id(PC_ROOM_DLG_REFUSED_END));
            break;
    }
    return -1;
}

/* the host answered the decision (or did not): the row that says so */
static int rui_answered(int ok, int result) {
    const int owner = s_info.role == PC_ROOM_ROLE_OWNER;
    printf("[ROOM][UI] RESULT op %d -> %d%s\n", s_op, result, ok ? " (ok)" : "");
    if (!owner) {
        if (!ok) {
            pc_room_dlg_set_reason(result);
            return rui_close_with(pc_room_dlg_msg_id(PC_ROOM_DLG_REFUSED_END));
        }
        return rui_close_with(pc_room_dlg_msg_id(s_op == PC_ROOM_OP_REQUEST ? PC_ROOM_DLG_ASK_SENT : s_op == PC_ROOM_OP_CANCEL ? PC_ROOM_DLG_WITHDRAWN : PC_ROOM_DLG_MOVED_OUT));
    }
    if (!ok) {
        pc_room_dlg_set_reason(result);
        rui_show(PC_ROOM_DLG_OWNER_REFUSED);
        return 0;
    }
    {
        const int e = s_steps[s_step];
        if (s_op == PC_ROOM_OP_ACCEPT) {
            s_info.e[e].state = PC_ROOM_ST_ROOMMATE; /* (a roommate now: listed as one from the next menu pick on) */
            rui_show(PC_ROOM_DLG_OWNER_ACCEPTED);
        } else {
            s_info.e[e].state = PC_ROOM_ST_REMOVED;
            s_gone[e] = 1;
            rui_show(s_op == PC_ROOM_OP_DENY ? PC_ROOM_DLG_OWNER_DENIED : PC_ROOM_DLG_OWNER_REMOVED);
        }
    }
    return 0;
}

static int rui_read_choice(void) {
    const int c = (int)mChoice_GET_CHOSENUM();
    mDemo_Set_OrderValue(mDemo_ORDER_NPC0, 9, 0);
    return c;
}

int pc_room_gyroid_dialog(void* actor, int house) {
    mMsg_Window_c* win = mMsg_Get_base_window_p();
    (void)house;
    if (s_cphase != RC_ARMED || actor != s_actor) {
        return 1;
    }
    if (mDemo_Check(mDemo_TYPE_TALK, (ACTOR*)actor) == FALSE) {
        rui_reset("the talk ended");
        return 1;
    }
    if (s_ds == DS_WAIT && !s_have_reply && s_seq != 0 && pc_net_game_room_take_reply(s_seq, &s_reply)) {
        s_have_reply = 1;
    }
    switch (s_ds) {
        case DS_ASK: {
            int c;
            if (!mDemo_Get_OrderValue(mDemo_ORDER_NPC0, 9) || !rui_on_row(s_row)) {
                return 0;
            }
            c = rui_read_choice();
            printf("[ROOM][UI] row %d: choice %d\n", s_row, c);
            switch (s_row) {
                case PC_ROOM_DLG_ASK_Q:
                    if (c == 0) {
                        rui_send(PC_ROOM_OP_REQUEST, NULL);
                        return 0;
                    }
                    return rui_close_with(PC_ROOM_VMSG_GUEST_BYE);
                case PC_ROOM_DLG_WITHDRAW_Q:
                    if (c == 0) {
                        rui_send(PC_ROOM_OP_CANCEL, NULL);
                        return 0;
                    }
                    return rui_close_with(PC_ROOM_VMSG_GUEST_BYE);
                case PC_ROOM_DLG_MOVEOUT_Q:
                    if (c == 0) {
                        rui_send(PC_ROOM_OP_LEAVE, NULL);
                        return 0;
                    }
                    return rui_close_with(PC_ROOM_VMSG_GUEST_BYE);
                case PC_ROOM_DLG_OWNER_REQ_Q:
                    if (c == 0 || c == 1) {
                        rui_send(c == 0 ? PC_ROOM_OP_ACCEPT : PC_ROOM_OP_DENY, s_info.e[s_steps[s_step]].key);
                        return 0;
                    }
                    return rui_next_step(); /* Decide later. */
                case PC_ROOM_DLG_OWNER_RM_Q:
                    if (c == 0) {
                        rui_ask(PC_ROOM_DLG_OWNER_RM_CONFIRM);
                        return 0;
                    }
                    return rui_next_step(); /* Leave it be. */
                case PC_ROOM_DLG_OWNER_RM_CONFIRM:
                    if (c == 0) {
                        rui_send(PC_ROOM_OP_REMOVE, s_info.e[s_steps[s_step]].key);
                        return 0;
                    }
                    return rui_next_step();
                default:
                    return rui_close_with(PC_ROOM_VMSG_GUEST_BYE);
            }
        }
        case DS_WAIT:
            if (!rui_on_row(PC_ROOM_DLG_WAIT)) {
                return 0; /* (the row is still being typed) */
            }
            if (s_seq == 0) {
                return rui_answered(0, PC_ROOM_DLG_REASON_LINK);
            }
            if (s_have_reply) {
                if (s_reply.op == PC_ROOM_OP_RESULT) {
                    return rui_answered(s_reply.result == PC_ROOM_R_OK, (int)s_reply.result);
                }
                s_have_reply = 0; /* (an INFO for this serial: keep waiting for the RESULT) */
            }
            if ((int32_t)(rui_now() - s_t0) >= (int32_t)PC_ROOM_OP_TIMEOUT_MS || !pc_net_game_room_on()) {
                printf("[ROOM][UI] op %d: no answer from the host\n", s_op);
                return rui_answered(0, PC_ROOM_DLG_REASON_LINK);
            }
            return 0;
        case DS_SHOW:
            if (!rui_on_row(s_row)) {
                return 0;
            }
            if (s_info.role == PC_ROOM_ROLE_OWNER) {
                if (s_row == PC_ROOM_DLG_OWNER_NONE) {
                    s_step = s_nsteps;
                }
                return rui_next_step();
            }
            return rui_close_with(PC_ROOM_VMSG_GUEST_BYE);
        case DS_DONE:
            return 1;
        default:
            (void)win;
            return 1;
    }
}

/* ---- TEST-ONLY (PC_NET_TEST_HOOKS build + AC_TEST_HOOKS=1): AC_TEST_ROOM_UI=<pick>,<pick>,... answers the gyroid's choice windows like a player: each choice
 * window of an armed conversation takes the next pick (a label prefix, case-insensitive: "ask", "yes", "let", "move-in", ...; no match / no pick left = the last
 * choice), the cursor is moved with the D-pad and A pressed; between choice windows A turns the pages. Logged [ROOM][UI][TEST-ONLY]. ---- */
#ifdef PC_NET_TEST_HOOKS
#include "pc_test_hooks.h"
static int      s_test_step, s_test_choice_open, s_test_target = -1, s_test_pressed;
static uint32_t s_test_ms;

static int rui_prefix(const u8* label, int label_len, const char* p) {
    int i;
    for (i = 0; p[i] != '\0'; i++) {
        int a, b = (unsigned char)p[i];
        if (i >= label_len) {
            return 0;
        }
        a = label[i];
        if (a >= 'A' && a <= 'Z') a = a - 'A' + 'a';
        if (b >= 'A' && b <= 'Z') b = b - 'A' + 'a';
        if (a != b) {
            return 0;
        }
    }
    return 1;
}

static int rui_test_token(char* tok, size_t cap) {
    const char* sc = getenv("AC_TEST_ROOM_UI");
    const char* p = sc;
    int k = 0;
    if (sc == NULL) {
        return 0;
    }
    while (p != NULL && *p != '\0') {
        size_t n = 0;
        while (p[n] != '\0' && p[n] != ',') n++;
        if (k++ == s_test_step) {
            snprintf(tok, cap, "%.*s", (int)(n < cap - 1 ? n : cap - 1), p);
            s_test_step++;
            return 1;
        }
        p += n;
        if (*p == ',') p++;
    }
    return 0;
}

unsigned pc_room_ui_test_pad(void) {
    const char* sc = getenv("AC_TEST_ROOM_UI");
    mChoice_c* ch;
    mMsg_Window_c* win;
    const uint32_t now = rui_now();
    if (!pc_test_hooks_enabled() || sc == NULL || sc[0] == '\0' || s_cphase != RC_ARMED || s_actor == NULL || !s_talk_seen ||
        mDemo_Check(mDemo_TYPE_TALK, (ACTOR*)s_actor) == FALSE) {
        s_test_choice_open = 0;
        s_test_pressed = 0;
        return 0;
    }
    if (s_test_pressed) { /* a press lasts ~3 frames, then ~8 frames released (the game reads edges) */
        if ((int32_t)(now - s_test_ms) < 50) {
            return (unsigned)s_test_pressed;
        }
        s_test_pressed = 0;
        s_test_ms = now;
        return 0;
    }
    if ((int32_t)(now - s_test_ms) < 130) { /* (signed: s_test_ms may lie ahead: "a moment on screen") */
        return 0;
    }
    ch = mChoice_Get_base_window_p();
    win = mMsg_Get_base_window_p();
    if (ch != NULL && mChoice_check_main_normal(ch) && ch->data.choice_num > 0) {
        if (!s_test_choice_open) {
            char tok[32];
            int i;
            s_test_choice_open = 1;
            s_test_target = ch->data.choice_num - 1;
            if (rui_test_token(tok, sizeof(tok))) {
                for (i = 0; i < ch->data.choice_num; i++) {
                    if (rui_prefix(ch->data.strings[i], ch->data.string_lens[i], tok)) {
                        s_test_target = i;
                        break;
                    }
                }
                printf("[ROOM][UI][TEST-ONLY] pick %d '%s' -> choice %d of %d ('%.*s')\n", s_test_step, tok, s_test_target, ch->data.choice_num,
                       ch->data.string_lens[s_test_target], (const char*)ch->data.strings[s_test_target]);
            } else {
                printf("[ROOM][UI][TEST-ONLY] the script is done: the last choice ('%.*s')\n", ch->data.string_lens[s_test_target],
                       (const char*)ch->data.strings[s_test_target]);
            }
            s_test_ms = now + 600u; /* a moment on screen before the cursor moves */
            return 0;
        }
        s_test_ms = now;
        s_test_pressed = ch->selected_choice_idx < s_test_target ? 2 : ch->selected_choice_idx > s_test_target ? 4 : 1;
        return (unsigned)s_test_pressed;
    }
    s_test_choice_open = 0;
    if (win != NULL && mMsg_Check_MainNormal(win) && (int32_t)(now - s_test_ms) >= 700) { /* a page / the closing row: A */
        s_test_ms = now;
        s_test_pressed = 1;
        return 1u;
    }
    return 0;
}
#else
unsigned pc_room_ui_test_pad(void) {
    return 0;
}
#endif

/* ---- the gyroid that comes with each house (ac_haniwa_move.c_inc, TARGET_PC) ---- */
extern int mDemo_PC_TalkPositionOk(ACTOR* actor);
static int s_test_force_a; /* TEST-ONLY (pc_room_ui_test_press): the next talk-position frame counts as an A press */

void pc_room_ui_test_press(void) {
    s_test_force_a = 1;
}

/* the host's INFO arrived (or did not): the vanilla talk starts (armed or not) */
static int rui_start_talk(void* actor, const PCRoomMsg* info) {
    const int armed = info != NULL && rui_arm(info);
    s_cphase = armed ? RC_ARMED : RC_IDLE;
    s_arm_ms = rui_now() | 1u;
    s_talk_seen = 0;
    s_ds = DS_NONE;
    g_pc_demo_force_talk_actor = actor; /* m_demo.c: picked without a new A press */
    printf("[ROOM][UI] the gyroid of house %d talks: %s\n", s_house, armed ? "armed (the move-in choice is in its menu)" : "the vanilla conversation");
    return 0;
}

int pc_room_gyroid_intercept(void* actor, int house) {
    int guest, owner, a;
    if (s_cphase == RC_QUERY) {
        PCRoomMsg m;
        if (actor != s_actor) {
            return 0;
        }
        if (pc_net_game_room_take_reply(s_seq, &m)) {
            if (m.op == PC_ROOM_OP_INFO) {
                printf("[ROOM][UI] INFO role %u (%u entr%s)\n", (unsigned)m.role, (unsigned)m.count, m.count == 1 ? "y" : "ies");
                return rui_start_talk(actor, &m);
            }
            if (m.op == PC_ROOM_OP_RESULT && m.result == PC_ROOM_R_NOT_GYROID && s_requeried < 2) {
                /* the host's copy of the player's position can lag behind (the player just walked up): ask again a moment later */
                s_requeried++;
                s_requery_ms = rui_now() | 1u;
                printf("[ROOM][UI] the host does not see the player next to the gyroid yet: asking again in 400 ms\n");
                return 1;
            }
            printf("[ROOM][UI] QUERY answered %u (%u): the vanilla conversation\n", (unsigned)m.op, (unsigned)m.result);
            return rui_start_talk(actor, NULL);
        }
        if (s_requery_ms != 0 && (int32_t)(rui_now() - s_requery_ms) >= (int32_t)400u) {
            const uint32_t seq = pc_net_game_room_send(PC_ROOM_OP_QUERY, s_house, NULL);
            s_requery_ms = 0;
            if (seq != 0) {
                s_seq = seq;
                s_t0 = rui_now();
            }
            return 1;
        }
        if ((int32_t)(rui_now() - s_t0) >= (int32_t)PC_ROOM_QUERY_TIMEOUT_MS) {
            printf("[ROOM][UI] no answer from the host: the vanilla conversation\n");
            return rui_start_talk(actor, NULL);
        }
        return 1; /* the talk starts once the host answered */
    }
    if (s_cphase == RC_ARMED) {
        return 0; /* (this conversation's talk request: vanilla) */
    }
    if (actor == NULL || house < 0 || house > 3 || !pc_net_game_room_on() || Now_Private == NULL) {
        return 0;
    }
    if (g_pc_demo_force_talk_actor == actor) {
        return 0; /* the talk that rui_start_talk asked for */
    }
    guest = (int)Common_Get(player_no) >= PLAYER_NUM;
    owner = !guest && mPr_NullCheckPersonalID(&Save_Get(homes[house]).ownerID) == FALSE &&
            mPr_CheckCmpPersonalID(&Save_Get(homes[house]).ownerID, &Now_Private->player_ID) == TRUE;
    if (!guest && !owner) {
        return 0;
    }
    a = chkTrigger(BUTTON_A) || s_test_force_a;
    if (s_test_force_a) {
        static uint32_t diag_ms;
        if (diag_ms == 0 || (int32_t)(rui_now() - diag_ms) >= (int32_t)1000u) {
            diag_ms = rui_now() | 1u;
            printf("[ROOM][UI][TEST-ONLY] gyroid of house %d: armed A; demo %d talk position %d (distance %.1f)\n", house, (int)mDemo_CheckDemo(),
                   mDemo_PC_TalkPositionOk((ACTOR*)actor), (double)((ACTOR*)actor)->player_distance_xz);
        }
    }
    if (!a || mMsg_Check_MainHide(mMsg_Get_base_window_p()) == FALSE || !mDemo_PC_TalkPositionOk((ACTOR*)actor)) { /* no message window open; the vanilla talk position */
        return 0;
    }
    s_test_force_a = 0;
    s_seq = pc_net_game_room_send(PC_ROOM_OP_QUERY, house, NULL);
    if (s_seq == 0) {
        return 0; /* (not sent: the vanilla talk) */
    }
    printf("[ROOM][UI] A at the gyroid of house %d (%s): asking the host first (seq %u)\n", house, guest ? "a guest" : "its owner", (unsigned)s_seq);
    s_house = house;
    s_actor = actor;
    s_t0 = rui_now();
    s_requeried = 0;
    s_requery_ms = 0;
    s_cphase = RC_QUERY;
    return 1;
}

void pc_room_gyroid_frame(void* actor, int house, int act) {
    (void)house;
    if (actor == NULL || actor != s_actor) {
        return;
    }
    if (g_pc_demo_force_talk_actor == actor && s_arm_ms != 0 && (int32_t)(rui_now() - s_arm_ms) >= (int32_t)1000u) {
        g_pc_demo_force_talk_actor = NULL; /* the talk did not start (the player walked off): no stale pick */
    }
    if (s_cphase == RC_QUERY && (int32_t)(rui_now() - s_t0) >= (int32_t)3000u) {
        rui_reset("the player left before the host answered");
        return;
    }
    if (s_cphase != RC_ARMED) {
        return;
    }
    if (!s_talk_seen) {
        static uint32_t diag_ms;
        if (diag_ms == 0 || (int32_t)(rui_now() - diag_ms) >= (int32_t)250u) {
            diag_ms = rui_now() | 1u;
            printf("[ROOM][UI] waiting for the talk: act %d forced %d position %d talkable %d demo %d player %d\n", act, g_pc_demo_force_talk_actor == actor,
                   mDemo_PC_TalkPositionOk((ACTOR*)actor), mPlib_check_request_main_talk_type1_priority(gamePT), (int)mDemo_CheckDemo(),
                   mPlib_get_player_actor_main_index(gamePT));
        }
    }
    if (mDemo_Check(mDemo_TYPE_TALK, (ACTOR*)actor) == TRUE) {
        s_talk_seen = 1;
    } else if ((s_talk_seen && (act == aHNW_ACTION_WAIT || act == aHNW_ACTION_DANCE)) || (!s_talk_seen && (int32_t)(rui_now() - s_arm_ms) >= (int32_t)2000u)) {
        rui_reset(s_talk_seen ? "the talk ended" : "the talk did not start");
    }
}

int pc_room_music_box_house(void) {
    if (Common_Get(player_no) >= PLAYER_NUM && mFI_GET_TYPE(mFI_GetFieldId()) == mFI_FIELD_PLAYER0_ROOM) {
        return (int)mFI_GET_PLAYER_ROOM_NO(mFI_GetFieldId());
    }
    return mHS_get_arrange_idx(Common_Get(player_no));
}

void pc_room_gyroid_gone(void* actor) {
    if (actor != NULL && actor == s_actor) {
        rui_reset("the gyroid was unloaded");
        s_actor = NULL;
    }
    if (actor != NULL && actor == g_pc_demo_force_talk_actor) {
        g_pc_demo_force_talk_actor = NULL;
    }
}
