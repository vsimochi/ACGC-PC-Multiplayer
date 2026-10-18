/* pc_room_ui.c - ROOMMATES / MOVE-IN: the gyroid menu, a small PC overlay (see pc_room_ui.h, docs/multiplayer-guest-roadmap.md "Roommates / move-in").
 * Opened by A at a placed gyroid (pc_ofurn_draw.c). It asks the host (message 104 QUERY), shows the rows of the requester's role, asks Yes / No before every
 * decision and sends the decision; the host's RESULT becomes a notice line. While it is open (also while it waits for the host) the pad is consumed, so the
 * player stands still. Closed by B, by a scene change, a pause, the link going away, or after 3 s without an answer. Nothing here writes game state. */
#include "pc_room_ui.h"
#include "pc_platform.h"
#include "pc_menu_util.h"
#include "pc_pause_menu.h"
#include "pc_text_draw.h"
#include "pc_field_authority.h"
#include "m_common_data.h"
#include "m_play.h"
#include "m_font.h"
#include "m_rcp.h"
#include "graph.h"
#include "main.h"
#include <SDL.h>
#include <dolphin/pad.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PC_ROOM_UI_WAIT_NOTICE_MS 500u
#define PC_ROOM_UI_TIMEOUT_MS     3000u
#define PC_ROOM_UI_MAX_ROWS       10

enum { RUI_CLOSED = 0, RUI_WAIT, RUI_LIST, RUI_DECIDE, RUI_CONFIRM, RUI_BUSY };
enum { ROW_CLOSE = 1, ROW_ASK, ROW_WITHDRAW, ROW_MOVE_OUT, ROW_REQUEST, ROW_REMOVE, ROW_INFO };

typedef struct {
    int     kind;
    int     entry; /* index into s_info.e[] */
    char    label[64];
} RuiRow;

static int       s_phase;
static int       s_ux, s_uz, s_item;
static uint32_t  s_seq, s_t0;
static int       s_busy_op;
static uint8_t   s_busy_key[PC_ROOM_KEY_LEN];
static char      s_busy_name[12];
static PCRoomMsg s_info;
static RuiRow    s_rows[PC_ROOM_UI_MAX_ROWS];
static int       s_nrows, s_sel, s_dsel, s_csel;
static int       s_confirm_op;  /* the op a Yes sends */
static int       s_decide_entry;
static char      s_title[64], s_sub[64], s_confirm_text[64];
static unsigned short s_prev_buttons;
static int       s_requeried;
static uint32_t  s_requery_ms;
static int       s_stick_latch;
static char      s_notice[96];
static uint32_t  s_notice_ms;

static uint32_t rui_now(void) {
    return (uint32_t)SDL_GetTicks();
}

void pc_room_ui_notice_set(const char* text) {
    snprintf(s_notice, sizeof(s_notice), "%s", text != NULL ? text : "");
    s_notice_ms = rui_now() | 1u;
    printf("[ROOM][UI] notice: %s\n", s_notice);
}

const char* pc_room_ui_notice_get(void) {
    if (s_notice_ms == 0 || s_notice[0] == '\0' || (uint32_t)(rui_now() - s_notice_ms) >= 6000u) {
        s_notice_ms = 0;
        return NULL;
    }
    return s_notice;
}

int pc_room_ui_is_open(void) {
    return s_phase != RUI_CLOSED;
}

/* a player name (8 game-font bytes, blank padded) as a drawable string; the font's letters / digits / blank are their ASCII codes */
static void rui_name(const uint8_t* n8, char* out, size_t cap) {
    size_t i, k = 0;
    for (i = 0; i < PC_ROOM_NAME_LEN && k + 1 < cap; i++) {
        out[k++] = n8[i] != 0 ? (char)n8[i] : ' ';
    }
    while (k > 0 && out[k - 1] == ' ') {
        k--;
    }
    out[k] = '\0';
    if (k == 0) {
        snprintf(out, cap, "someone");
    }
}

static void rui_close(const char* why) {
    if (s_phase != RUI_CLOSED) {
        printf("[ROOM][UI] menu closed (%s)\n", why);
    }
    s_phase = RUI_CLOSED;
    s_nrows = 0;
}

static void rui_add(int kind, int entry, const char* label) {
    if (s_nrows < PC_ROOM_UI_MAX_ROWS) {
        s_rows[s_nrows].kind = kind;
        s_rows[s_nrows].entry = entry;
        snprintf(s_rows[s_nrows].label, sizeof(s_rows[s_nrows].label), "%s", label);
        s_nrows++;
    }
}

static void rui_build_rows(void) {
    char owner[16], nm[16], buf[64];
    int i;
    rui_name(s_info.owner_name, owner, sizeof(owner));
    s_nrows = 0;
    s_sub[0] = '\0';
    switch (s_info.role) {
        case PC_ROOM_ROLE_OWNER:
            snprintf(s_title, sizeof(s_title), "Your gyroid");
            for (i = 0; i < (int)s_info.count; i++) {
                rui_name(s_info.e[i].name, nm, sizeof(nm));
                if (s_info.e[i].state == PC_ROOM_ST_PENDING) {
                    snprintf(buf, sizeof(buf), "%s wants to move in", nm);
                    rui_add(ROW_REQUEST, i, buf);
                }
            }
            for (i = 0; i < (int)s_info.count; i++) {
                rui_name(s_info.e[i].name, nm, sizeof(nm));
                if (s_info.e[i].state == PC_ROOM_ST_ROOMMATE) {
                    snprintf(buf, sizeof(buf), "Remove %s%s", nm, s_info.e[i].online ? " (online)" : "");
                    rui_add(ROW_REMOVE, i, buf);
                }
            }
            snprintf(s_sub, sizeof(s_sub), s_nrows == 0 ? "No move-in requests" : "Move-in requests and roommates");
            break;
        case PC_ROOM_ROLE_CAN_REQUEST:
            snprintf(s_title, sizeof(s_title), "%s's gyroid", owner);
            snprintf(s_sub, sizeof(s_sub), "%s lives here", owner);
            rui_add(ROW_ASK, -1, "Ask to move in");
            break;
        case PC_ROOM_ROLE_PENDING:
            snprintf(s_title, sizeof(s_title), "%s's gyroid", owner);
            snprintf(s_sub, sizeof(s_sub), "Your move-in request is waiting");
            rui_add(ROW_WITHDRAW, -1, "Withdraw my request");
            break;
        case PC_ROOM_ROLE_ROOMMATE:
            snprintf(s_title, sizeof(s_title), "%s's gyroid", owner);
            snprintf(s_sub, sizeof(s_sub), "You live here with %s", owner);
            rui_add(ROW_MOVE_OUT, -1, "Move out");
            break;
        case PC_ROOM_ROLE_BLOCKED:
            snprintf(s_title, sizeof(s_title), "%s's gyroid", owner);
            snprintf(s_sub, sizeof(s_sub), "%s", pc_room_result_text((int)s_info.result));
            break;
        case PC_ROOM_ROLE_RESIDENT:
            snprintf(s_title, sizeof(s_title), "%s's gyroid", owner);
            snprintf(s_sub, sizeof(s_sub), "%s lives here", owner);
            break;
        default:
            snprintf(s_title, sizeof(s_title), "A gyroid");
            snprintf(s_sub, sizeof(s_sub), "%s", s_info.result != 0 ? pc_room_result_text((int)s_info.result) : "Nobody lives with this gyroid");
            break;
    }
    rui_add(ROW_CLOSE, -1, "Close");
    if (s_sel >= s_nrows) {
        s_sel = s_nrows - 1;
    }
    if (s_sel < 0) {
        s_sel = 0;
    }
}

void pc_room_ui_open(int ut_x, int ut_z, int item) {
    uint32_t seq;
    if (s_phase != RUI_CLOSED || !pc_net_game_room_on()) {
        return;
    }
    seq = pc_net_game_room_send(PC_ROOM_OP_QUERY, ut_x, ut_z, item, NULL);
    if (seq == 0) {
        pc_room_ui_notice_set("Try again in a moment");
        return;
    }
    s_ux = ut_x;
    s_uz = ut_z;
    s_item = item;
    s_seq = seq;
    s_t0 = rui_now();
    s_sel = 0;
    s_requeried = 0;
    s_requery_ms = 0;
    s_phase = RUI_WAIT;
    printf("[ROOM][UI] gyroid at (%d,%d) item 0x%04X: asking the host (seq %u)\n", ut_x, ut_z, (unsigned)item, (unsigned)seq);
}

static void rui_send(int op, const uint8_t* key, const char* name) {
    const uint32_t seq = pc_net_game_room_send(op, s_ux, s_uz, s_item, key);
    if (seq == 0) {
        pc_room_ui_notice_set("Try again in a moment");
        rui_close("send refused");
        return;
    }
    s_seq = seq;
    s_t0 = rui_now();
    s_busy_op = op;
    if (key != NULL) {
        memcpy(s_busy_key, key, PC_ROOM_KEY_LEN);
    }
    snprintf(s_busy_name, sizeof(s_busy_name), "%s", name != NULL ? name : "");
    s_phase = RUI_BUSY;
    printf("[ROOM][UI] op %d sent (seq %u)\n", op, (unsigned)seq);
}

static void rui_result_notice(int op, int result) {
    char owner[16], buf[96];
    rui_name(s_info.owner_name, owner, sizeof(owner));
    if (result != PC_ROOM_R_OK) {
        pc_room_ui_notice_set(pc_room_result_text(result));
        return;
    }
    switch (op) {
        case PC_ROOM_OP_REQUEST: snprintf(buf, sizeof(buf), "You asked %s if you can move in", owner); break;
        case PC_ROOM_OP_CANCEL: snprintf(buf, sizeof(buf), "You withdrew your request"); break;
        case PC_ROOM_OP_ACCEPT: snprintf(buf, sizeof(buf), "%s moved in with you", s_busy_name); break;
        case PC_ROOM_OP_DENY: snprintf(buf, sizeof(buf), "You turned %s down", s_busy_name); break;
        case PC_ROOM_OP_REMOVE: snprintf(buf, sizeof(buf), "%s no longer lives with you", s_busy_name); break;
        case PC_ROOM_OP_LEAVE: snprintf(buf, sizeof(buf), "You moved out of %s's house", owner); break;
        default: snprintf(buf, sizeof(buf), "Done"); break;
    }
    pc_room_ui_notice_set(buf);
}

static void rui_choose_row(void) {
    const RuiRow* r = &s_rows[s_sel];
    char owner[16], nm[16];
    rui_name(s_info.owner_name, owner, sizeof(owner));
    switch (r->kind) {
        case ROW_CLOSE: rui_close("Close"); return;
        case ROW_ASK:
            s_confirm_op = PC_ROOM_OP_REQUEST;
            snprintf(s_confirm_text, sizeof(s_confirm_text), "Ask %s if you can move in?", owner);
            break;
        case ROW_WITHDRAW:
            s_confirm_op = PC_ROOM_OP_CANCEL;
            snprintf(s_confirm_text, sizeof(s_confirm_text), "Withdraw your request?");
            break;
        case ROW_MOVE_OUT:
            s_confirm_op = PC_ROOM_OP_LEAVE;
            snprintf(s_confirm_text, sizeof(s_confirm_text), "Move out of %s's house?", owner);
            break;
        case ROW_REQUEST:
            s_decide_entry = r->entry;
            s_dsel = 0;
            s_phase = RUI_DECIDE;
            return;
        case ROW_REMOVE:
            s_decide_entry = r->entry;
            rui_name(s_info.e[r->entry].name, nm, sizeof(nm));
            s_confirm_op = PC_ROOM_OP_REMOVE;
            snprintf(s_confirm_text, sizeof(s_confirm_text), "Remove %s from your house?", nm);
            break;
        default: return;
    }
    s_csel = 0; /* "No" first: a decision is never one stray A press */
    s_phase = RUI_CONFIRM;
}

static void rui_confirmed(void) {
    char nm[16];
    const uint8_t* key = NULL;
    nm[0] = '\0';
    if (s_confirm_op == PC_ROOM_OP_ACCEPT || s_confirm_op == PC_ROOM_OP_DENY || s_confirm_op == PC_ROOM_OP_REMOVE) {
        key = s_info.e[s_decide_entry].key;
        rui_name(s_info.e[s_decide_entry].name, nm, sizeof(nm));
    }
    rui_send(s_confirm_op, key, nm);
}

/* ---- TEST-ONLY (PC_NET_TEST_HOOKS build + AC_TEST_HOOKS=1): AC_TEST_ROOM_UI=<pick>,<pick>,... drives the open menu like a player: every 1.2 s the next pick:
 * a row label prefix (case-insensitive) on the list / decision page, `yes` / `no` on a confirm page, `close`. Logged [ROOM][UI][TEST-ONLY]. ---- */
#ifdef PC_NET_TEST_HOOKS
#include "pc_test_hooks.h"
static int      s_test_step;
static uint32_t s_test_ms;
static int rui_prefix(const char* label, const char* p, size_t n) {
    size_t i;
    for (i = 0; i < n; i++) {
        char a = label[i], b = p[i];
        if (a == '\0') {
            return 0;
        }
        if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
        if (b >= 'A' && b <= 'Z') b = (char)(b - 'A' + 'a');
        if (a != b) {
            return 0;
        }
    }
    return 1;
}
/* 1 = A this frame (the selection was moved to the pick), 2 = B */
static int rui_test_pick(void) {
    const char* sc = getenv("AC_TEST_ROOM_UI");
    const char* p;
    char tok[32];
    int i, k = 0;
    if (!pc_test_hooks_enabled() || sc == NULL || sc[0] == '\0' || (s_phase != RUI_LIST && s_phase != RUI_DECIDE && s_phase != RUI_CONFIRM)) {
        return 0;
    }
    if (s_test_ms == 0) {
        s_test_ms = rui_now() | 1u;
        return 0;
    }
    if ((uint32_t)(rui_now() - s_test_ms) < 1200u) {
        return 0;
    }
    s_test_ms = rui_now() | 1u;
    p = sc;
    while (*p != '\0') {
        size_t n = 0;
        while (p[n] != '\0' && p[n] != ',') n++;
        if (k++ == s_test_step) {
            snprintf(tok, sizeof(tok), "%.*s", (int)(n < sizeof(tok) - 1 ? n : sizeof(tok) - 1), p);
            s_test_step++;
            printf("[ROOM][UI][TEST-ONLY] pick %d '%s' on page %d\n", s_test_step, tok, s_phase);
            if (strcmp(tok, "close") == 0 || strcmp(tok, "b") == 0) {
                return 2;
            }
            if (s_phase == RUI_CONFIRM) {
                s_csel = strcmp(tok, "yes") == 0 ? 1 : 0;
                return 1;
            }
            if (s_phase == RUI_DECIDE) {
                s_dsel = rui_prefix("accept", tok, strlen(tok)) ? 0 : rui_prefix("deny", tok, strlen(tok)) ? 1 : 2;
                return 1;
            }
            for (i = 0; i < s_nrows; i++) {
                if (rui_prefix(s_rows[i].label, tok, strlen(tok))) {
                    s_sel = i;
                    return 1;
                }
            }
            printf("[ROOM][UI][TEST-ONLY] no row starts with '%s': closing (the pick is retried at the next visit)\n", tok);
            s_test_step--;
            return 2;
        }
        p += n;
        if (*p == ',') p++;
    }
    printf("[ROOM][UI][TEST-ONLY] the script is done: closing the menu\n");
    return 2; /* every pick was used: a later visit only closes the menu again */
}
#endif

static void rui_tick(void) {
    PCRoomMsg m;
    const uint32_t waited = rui_now() - s_t0;
    if (s_phase == RUI_CLOSED) {
        return;
    }
    if (!pc_net_game_room_on() || !pcfa_scene_is_town() || g_pc_paused) {
        rui_close(g_pc_paused ? "paused" : "the town / link went away");
        return;
    }
    if (s_phase == RUI_WAIT || s_phase == RUI_BUSY) {
        if (pc_net_game_room_take_reply(s_seq, &m)) {
            if (m.op == PC_ROOM_OP_INFO) {
                s_info = m;
                rui_build_rows();
                s_phase = RUI_LIST;
                printf("[ROOM][UI] INFO role %u (%u entr%s): menu open with %d row(s)\n", (unsigned)m.role, (unsigned)m.count, m.count == 1 ? "y" : "ies", s_nrows);
            } else if (m.op == PC_ROOM_OP_RESULT && m.aux == PC_ROOM_OP_QUERY && m.result == PC_ROOM_R_NOT_GYROID && s_requeried < 2) {
                /* the host's copy of the player's position can lag behind (the player just walked up): ask twice more, a moment later */
                s_requeried++;
                s_requery_ms = rui_now() | 1u;
                printf("[ROOM][UI] the host does not see the player next to the gyroid yet: asking again in 700 ms\n");
            } else if (m.op == PC_ROOM_OP_RESULT) {
                printf("[ROOM][UI] RESULT op %u -> %u (%s)\n", (unsigned)m.aux, (unsigned)m.result, pc_room_result_text((int)m.result));
                rui_result_notice(s_busy_op, (int)m.result);
                if (m.result == PC_ROOM_R_OK && s_info.role == PC_ROOM_ROLE_OWNER) {
                    const uint32_t seq = pc_net_game_room_send(PC_ROOM_OP_QUERY, s_ux, s_uz, s_item, NULL); /* the holder's list refreshes */
                    if (seq != 0) {
                        s_seq = seq;
                        s_t0 = rui_now();
                        s_phase = RUI_WAIT;
                        return;
                    }
                }
                rui_close("decision answered");
            }
            return;
        }
        if (s_requery_ms != 0 && (uint32_t)(rui_now() - s_requery_ms) >= 700u) {
            const uint32_t seq = pc_net_game_room_send(PC_ROOM_OP_QUERY, s_ux, s_uz, s_item, NULL);
            s_requery_ms = 0;
            if (seq != 0) {
                s_seq = seq;
                s_t0 = rui_now();
            }
            return;
        }
        if (waited >= PC_ROOM_UI_TIMEOUT_MS) {
            pc_room_ui_notice_set(s_phase == RUI_BUSY ? "No answer from the server - check the gyroid again" : "Try again in a moment");
            rui_close("no answer");
        }
    }
}

void pc_room_ui_input(unsigned short* buttons, signed char* stick_x, signed char* stick_y, signed char* cstick_x, signed char* cstick_y) {
    unsigned short b, edge;
    int up = 0, down = 0, a, bk, n;
    (void)stick_x;
    rui_tick();
    if (s_phase == RUI_CLOSED) {
        s_prev_buttons = buttons != NULL ? *buttons : 0;
        return;
    }
    b = *buttons;
    edge = (unsigned short)(b & ~s_prev_buttons);
    s_prev_buttons = b;
    if (*stick_y > 50) {
        up = !s_stick_latch;
        s_stick_latch = 1;
    } else if (*stick_y < -50) {
        down = !s_stick_latch;
        s_stick_latch = 1;
    } else if (*stick_y > -30 && *stick_y < 30) {
        s_stick_latch = 0;
    }
    up |= (edge & PAD_BUTTON_UP) != 0;
    down |= (edge & PAD_BUTTON_DOWN) != 0;
    a = (edge & PAD_BUTTON_A) != 0;
    bk = (edge & PAD_BUTTON_B) != 0;
#ifdef PC_NET_TEST_HOOKS
    {
        const int t = rui_test_pick();
        a |= t == 1;
        bk |= t == 2;
    }
#endif
    /* the menu owns the pad: the player stands still, no A / B reaches the game */
    *buttons = 0;
    *stick_x = *stick_y = 0;
    *cstick_x = *cstick_y = 0;
    switch (s_phase) {
        case RUI_LIST:
            n = s_nrows;
            if (up) s_sel = (s_sel + n - 1) % n;
            if (down) s_sel = (s_sel + 1) % n;
            if (bk) {
                rui_close("B");
            } else if (a) {
                rui_choose_row();
            }
            break;
        case RUI_DECIDE:
            if (up) s_dsel = (s_dsel + 2) % 3;
            if (down) s_dsel = (s_dsel + 1) % 3;
            if (bk || (a && s_dsel == 2)) {
                s_phase = RUI_LIST;
            } else if (a) {
                char nm[16];
                rui_name(s_info.e[s_decide_entry].name, nm, sizeof(nm));
                s_confirm_op = s_dsel == 0 ? PC_ROOM_OP_ACCEPT : PC_ROOM_OP_DENY;
                snprintf(s_confirm_text, sizeof(s_confirm_text), s_dsel == 0 ? "Let %s move in?" : "Turn %s down?", nm);
                s_csel = 0;
                s_phase = RUI_CONFIRM;
            }
            break;
        case RUI_CONFIRM:
            if (edge & (PAD_BUTTON_LEFT | PAD_BUTTON_RIGHT)) s_csel ^= 1;
            if (up || down) s_csel ^= 1;
            if (bk || (a && s_csel == 0)) {
                s_phase = RUI_LIST;
            } else if (a) {
                rui_confirmed();
            }
            break;
        default:
            break;
    }
}

void pc_room_ui_draw(struct game_s* game) {
    int i, r, g, b, al;
    if (s_phase == RUI_CLOSED || game == NULL || game->graph == NULL) {
        return;
    }
    if (s_phase == RUI_WAIT && (uint32_t)(rui_now() - s_t0) < PC_ROOM_UI_WAIT_NOTICE_MS && s_info.op == 0) {
        return; /* a quick answer: no flash of an empty panel */
    }
    mFont_SetMatrix(game->graph, mFont_MODE_FONT);
    pc_menu_dim_rect(game->graph, 120);
    if (s_phase == RUI_WAIT || s_phase == RUI_BUSY) {
        pc_menu_draw_centered(game, "Asking the server...", 110.0f, 255, 255, 255, 255, 1.0f);
    } else if (s_phase == RUI_LIST) {
        pc_menu_draw_centered(game, s_title, 30.0f, 255, 255, 255, 255, 1.0f);
        if (s_sub[0] != '\0') {
            pc_menu_draw_centered(game, s_sub, 44.0f, 200, 220, 255, 230, 1.0f);
        }
        for (i = 0; i < s_nrows; i++) {
            pc_menu_row_colors(i == s_sel, &r, &g, &b, &al);
            pc_menu_draw_centered(game, s_rows[i].label, 62.0f + (f32)i * 15.0f, r, g, b, al, i == s_sel ? PC_MENU_SCALE_SELECTED : 1.0f);
        }
        pc_menu_draw_centered(game, "A choose   B close", 218.0f, 150, 150, 150, 200, 1.0f);
    } else if (s_phase == RUI_DECIDE) {
        static const char* const opts[3] = { "Accept", "Deny", "Back" };
        char nm[16], t[64];
        rui_name(s_info.e[s_decide_entry].name, nm, sizeof(nm));
        snprintf(t, sizeof(t), "%s wants to move in", nm);
        pc_menu_draw_centered(game, t, 70.0f, 255, 255, 255, 255, 1.0f);
        for (i = 0; i < 3; i++) {
            pc_menu_row_colors(i == s_dsel, &r, &g, &b, &al);
            pc_menu_draw_centered(game, opts[i], 100.0f + (f32)i * 15.0f, r, g, b, al, i == s_dsel ? PC_MENU_SCALE_SELECTED : 1.0f);
        }
        pc_menu_draw_centered(game, "A choose   B back", 218.0f, 150, 150, 150, 200, 1.0f);
    } else if (s_phase == RUI_CONFIRM) {
        pc_menu_draw_centered(game, s_confirm_text, 100.0f, 255, 255, 255, 255, 1.0f);
        pc_menu_draw_two_choice(game, "No", "Yes", s_csel, 130.0f);
    }
    mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
}
