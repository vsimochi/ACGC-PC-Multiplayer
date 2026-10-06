/* Guest Nook dialogue (M4): generated messages + choice strings of the house offer. See include/pc_nook_house.h. */
#include <stdio.h>
#include <string.h>

#include "pc_nook_house.h"
#include "pc_net_game.h"
#include "pc_residence.h" /* PC_RESIDENCE_HOUSES */
#include "pc_platform.h"  /* SDL_GetTicks: the rejoin hold heartbeat runs on the wall clock */
#include "m_msg_data.h"

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
    return bad;
}
