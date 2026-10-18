/* pc_room_dlg.c - ROOMMATES / MOVE-IN: the rows of the move-in dialogue inside the vanilla gyroid conversation and the patch of the gyroid's own menus.
 * See pc_room_dlg.h. Pure: no game state (the message system calls in through pc_nook_house.c; pc_room_ui.c arms it per conversation). */
#include <stdio.h>
#include <string.h>

#include "pc_room_dlg.h"
#include "pc_nook_house.h" /* PC_NOOK_SEL_BASE */
#include "pc_roommates.h"  /* PC_ROOM_R_* */

/* control code bytes (include/m_font.h mFont_CONT_CODE_*; the same as pc_nook_house.c) */
#define RD_CC 0x7F
#define RD_END 0x00
#define RD_CONTINUE 0x01
#define RD_CLEAR 0x02
#define RD_PAUSE 0x03
#define RD_BTN 0x04
#define RD_DEMO_NPC0 0x09
#define RD_OPENCHOICE 0x0D
#define RD_NEXT0 0x0F /* SET_NEXT_MESSAGE_0..3 = 0x0F..0x12 */
#define RD_NEXT4 0x77 /* SET_NEXT_MESSAGE_4 / 5 = 0x77 / 0x78 */
#define RD_SEL2 0x16
#define RD_SEL3 0x17
#define RD_SEL4 0x18
#define RD_FORCENEXT 0x19
#define RD_PLAYERNAME 0x1A
#define RD_FREE10 0x36
#define RD_SNDTRGSYS 0x59
#define RD_SELNOB 0x5E
#define RD_SEL5 0x79
#define RD_SEL6 0x7A
#define RD_NEWLINE 205

#define RD_SEL_YES 3 /* "Yes." (the vanilla table) */
#define RD_SEL_NO 4  /* "No." */

typedef struct {
    unsigned char* d;
    int n, cap, err;
} RdBuf;

static void rd_byte(RdBuf* b, int c) {
    if (b->n < b->cap) {
        b->d[b->n++] = (unsigned char)c;
    } else {
        b->err = 1;
    }
}

static void rd_text(RdBuf* b, const char* s) {
    for (; *s != '\0'; s++) {
        rd_byte(b, *s == '\n' ? RD_NEWLINE : (unsigned char)*s);
    }
}

static void rd_code(RdBuf* b, int code) {
    rd_byte(b, RD_CC);
    rd_byte(b, code);
}

static void rd_pause(RdBuf* b, int t) {
    rd_code(b, RD_PAUSE);
    rd_byte(b, t);
}

static void rd_name(RdBuf* b) {
    rd_code(b, RD_FREE10); /* the other party (mMsg_FREE_STR10, set by pc_room_ui.c) */
}

static void rd_page(RdBuf* b) {
    rd_code(b, RD_BTN);
    rd_code(b, RD_CLEAR);
}

static void rd_ids(RdBuf* b, const int* ids, int n) {
    int i;
    for (i = 0; i < n; i++) {
        rd_byte(b, (ids[i] >> 8) & 0xFF);
        rd_byte(b, ids[i] & 0xFF);
    }
}

/* the vanilla-shaped choice tail (as pc_nook_house.c nk_choice): SETSELSTR<n>, SELNOB, BTN, OPENCHOICE, FORCENEXT, "choice made" (demo order NPC0 9 = 1), MSGCONTINUE:
 * pc_room_ui.c reads the choice and sets the next message itself */
static void rd_choice(RdBuf* b, const int* ids, int n) {
    rd_code(b, n == 2 ? RD_SEL2 : n == 3 ? RD_SEL3 : n == 4 ? RD_SEL4 : n == 5 ? RD_SEL5 : RD_SEL6);
    rd_ids(b, ids, n);
    rd_code(b, RD_SELNOB);
    rd_code(b, RD_BTN);
    rd_code(b, RD_OPENCHOICE);
    rd_code(b, RD_FORCENEXT);
    rd_code(b, RD_DEMO_NPC0);
    rd_byte(b, 9);
    rd_byte(b, 0);
    rd_byte(b, 1);
    rd_code(b, RD_CONTINUE);
}

static void rd_yes_no(RdBuf* b) {
    static const int yn[2] = { RD_SEL_YES, RD_SEL_NO };
    rd_choice(b, yn, 2);
}

int pc_room_dlg_msg_id(int which) {
    return PC_ROOM_DLG_MSG_MAX + PC_ROOM_DLG_MSG_OFS + which;
}

int pc_room_dlg_sel_id(int which) {
    return PC_NOOK_SEL_BASE + PC_ROOM_DLG_SEL_OFS + which;
}

int pc_room_dlg_msg_id_valid(int id) {
    return id >= PC_ROOM_DLG_MSG_MAX + PC_ROOM_DLG_MSG_OFS && id < PC_ROOM_DLG_MSG_MAX + PC_ROOM_DLG_MSG_OFS + PC_ROOM_DLG_COUNT;
}

int pc_room_dlg_sel_id_valid(int id) {
    return id >= PC_NOOK_SEL_BASE + PC_ROOM_DLG_SEL_OFS && id < PC_NOOK_SEL_BASE + PC_ROOM_DLG_SEL_OFS + PC_ROOM_SEL_COUNT;
}

/* why a request failed, in the gyroid's words (two short lines at most) */
static const char* rd_reason(int r) {
    switch (r) {
        case PC_ROOM_R_NOT_GYROID: return "Please stand right in\nfront of me.";
        case PC_ROOM_R_NOT_OWNER: return "Only the owner of this\nhouse may do that.";
        case PC_ROOM_R_NO_HOUSE: return "No one owns this house.";
        case PC_ROOM_R_PENDING_ELSEWHERE: return "You have already asked\nto move in elsewhere.";
        case PC_ROOM_R_ROOMMATE_ELSEWHERE: return "You already reside in\nanother house.";
        case PC_ROOM_R_ALREADY: return "That request was already\nmade.";
        case PC_ROOM_R_COOLDOWN: return "Please wait a while\nbefore asking again.";
        case PC_ROOM_R_HOUSE_FULL: return "This house has no room\nfor another resident.";
        case PC_ROOM_R_LIST_FULL: return "Too many requests are\nwaiting just now.";
        case PC_ROOM_R_NOT_FOUND: return "That request is no\nlonger open.";
        case PC_ROOM_R_BUSY:
        case PC_ROOM_R_FAULT: return "The records could not be\nchanged just now.";
        case PC_ROOM_R_OFF: return "Moving in is not possible\nin this town.";
        case PC_ROOM_R_NOT_GUEST: return "You have a house of your\nown already.";
        case PC_ROOM_R_RATE: return "Please try again in a\nmoment.";
        case PC_ROOM_DLG_REASON_LINK: return "I could not reach the\nrecords just now.";
        default: return "The request could not be\nprocessed.";
    }
}

/* the vanilla 0x0927 choice block (save / leave items / other / end, their next messages): the holder's "anything else?" row repeats it, then it is patched like the
 * vanilla rows */
static const unsigned char k_owner_menu_tail[] = {
    RD_CC, RD_DEMO_NPC0, 0x09, 0x00, 0x01, RD_CC, RD_SEL4, 0x00, 0x5E, 0x00, 0x14, 0x01, 0xB3, 0x00, 0x15, RD_CC, RD_SELNOB, RD_CC, RD_BTN, RD_CC, RD_OPENCHOICE,
    RD_CC, 0x0F, 0x09, 0x2F, RD_CC, 0x10, 0x09, 0x27, RD_CC, 0x11, 0x09, 0x31, RD_CC, 0x12, 0x09, 0x26, RD_CC, RD_FORCENEXT, RD_NEWLINE, RD_CC, RD_CONTINUE
};
static const int k_owner_ids[4] = { 0x005E, 0x0014, 0x01B3, 0x0015 }; /* Save / Leave items (sale) / Other / End */
static const int k_guest_ids[2] = { 0x0016, 0x0015 };                 /* Look at the items / Leave */

static int s_arm_mode, s_arm_sel = -1, s_arm_next = -1, s_reason;
static int s_menu_choice = -1;

static int rd_menu_patch(unsigned char* data, int len, int cap);

int pc_room_dlg_msg_build(int which, int reason, unsigned char* dst, int cap) {
    RdBuf b;
    if (dst == NULL || cap <= 0) {
        return 0;
    }
    b.d = dst;
    b.n = 0;
    b.cap = cap;
    b.err = 0;
    switch (which) {
        case PC_ROOM_DLG_ASK_Q:
            rd_text(&b, "This residence belongs to\n");
            rd_name(&b);
            rd_text(&b, ".");
            rd_pause(&b, 8);
            rd_page(&b);
            rd_text(&b, "Shall I ask ");
            rd_name(&b);
            rd_text(&b, "\nif you may move in?");
            rd_yes_no(&b);
            break;
        case PC_ROOM_DLG_WITHDRAW_Q:
            rd_text(&b, "Your request to move in\nawaits ");
            rd_name(&b);
            rd_text(&b, "'s answer.");
            rd_pause(&b, 8);
            rd_page(&b);
            rd_text(&b, "Shall I withdraw it?");
            rd_yes_no(&b);
            break;
        case PC_ROOM_DLG_MOVEOUT_Q:
            rd_text(&b, "You reside here with\n");
            rd_name(&b);
            rd_text(&b, ".");
            rd_pause(&b, 8);
            rd_page(&b);
            rd_text(&b, "Do you wish to move out?");
            rd_yes_no(&b);
            break;
        case PC_ROOM_DLG_WAIT:
            rd_text(&b, "Processing request");
            rd_pause(&b, 8);
            rd_text(&b, ".");
            rd_pause(&b, 8);
            rd_text(&b, ".");
            rd_pause(&b, 8);
            rd_text(&b, ".");
            rd_code(&b, RD_FORCENEXT); /* stops here (no continue message yet) until pc_room_ui.c names the answer row */
            rd_code(&b, RD_CONTINUE);
            break;
        case PC_ROOM_DLG_ASK_SENT:
            rd_text(&b, "Request processed.");
            rd_pause(&b, 8);
            rd_text(&b, "\n");
            rd_name(&b);
            rd_text(&b, " shall hear of\nyour wish to move in.");
            rd_pause(&b, 8);
            rd_text(&b, "\nPlease come again.");
            rd_code(&b, RD_END);
            break;
        case PC_ROOM_DLG_WITHDRAWN:
            rd_text(&b, "Request processed.");
            rd_pause(&b, 8);
            rd_text(&b, "\nYour request has been\nwithdrawn.");
            rd_pause(&b, 8);
            rd_text(&b, "\nPlease come again.");
            rd_code(&b, RD_END);
            break;
        case PC_ROOM_DLG_MOVED_OUT:
            rd_text(&b, "Request processed.");
            rd_pause(&b, 8);
            rd_text(&b, "\nYou no longer reside here.");
            rd_pause(&b, 8);
            rd_text(&b, "\nPlease take care.");
            rd_code(&b, RD_END);
            break;
        case PC_ROOM_DLG_REFUSED_END:
            rd_text(&b, "My apologies.");
            rd_pause(&b, 8);
            rd_text(&b, "\n");
            rd_text(&b, rd_reason(reason));
            rd_code(&b, RD_END);
            break;
        case PC_ROOM_DLG_OWNER_NONE:
            rd_text(&b, "No one has asked to move\nin with you,");
            rd_pause(&b, 6);
            rd_text(&b, " and no one\nresides here with you.");
            rd_code(&b, RD_CONTINUE);
            break;
        case PC_ROOM_DLG_OWNER_REQ_Q: {
            int c[3];
            c[0] = pc_room_dlg_sel_id(PC_ROOM_SEL_LET_IN);
            c[1] = pc_room_dlg_sel_id(PC_ROOM_SEL_TURN_DOWN);
            c[2] = pc_room_dlg_sel_id(PC_ROOM_SEL_LATER);
            rd_name(&b);
            rd_text(&b, " wishes to move\nin with you.");
            rd_pause(&b, 8);
            rd_text(&b, "\nShall I let them in?");
            rd_choice(&b, c, 3);
            break;
        }
        case PC_ROOM_DLG_OWNER_ACCEPTED:
            rd_text(&b, "Request processed.");
            rd_pause(&b, 8);
            rd_text(&b, "\n");
            rd_name(&b);
            rd_text(&b, " now resides here\nwith you.");
            rd_code(&b, RD_CONTINUE);
            break;
        case PC_ROOM_DLG_OWNER_DENIED:
            rd_text(&b, "Request processed.");
            rd_pause(&b, 8);
            rd_text(&b, "\n");
            rd_name(&b);
            rd_text(&b, " has been turned\ndown.");
            rd_code(&b, RD_CONTINUE);
            break;
        case PC_ROOM_DLG_OWNER_RM_Q: {
            int c[2];
            c[0] = pc_room_dlg_sel_id(PC_ROOM_SEL_ASK_OUT);
            c[1] = pc_room_dlg_sel_id(PC_ROOM_SEL_LEAVE_BE);
            rd_name(&b);
            rd_text(&b, " resides here\nwith you.");
            rd_pause(&b, 8);
            rd_text(&b, "\nWhat shall I do?");
            rd_choice(&b, c, 2);
            break;
        }
        case PC_ROOM_DLG_OWNER_RM_CONFIRM:
            rd_text(&b, "Shall I really ask\n");
            rd_name(&b);
            rd_text(&b, " to move out?");
            rd_yes_no(&b);
            break;
        case PC_ROOM_DLG_OWNER_REMOVED:
            rd_text(&b, "Request processed.");
            rd_pause(&b, 8);
            rd_text(&b, "\n");
            rd_name(&b);
            rd_text(&b, " no longer\nresides here.");
            rd_code(&b, RD_CONTINUE);
            break;
        case PC_ROOM_DLG_OWNER_REFUSED:
            rd_text(&b, "My apologies.");
            rd_pause(&b, 8);
            rd_text(&b, "\n");
            rd_text(&b, rd_reason(reason));
            rd_code(&b, RD_CONTINUE);
            break;
        case PC_ROOM_DLG_OWNER_MENU: {
            int n;
            rd_text(&b, "May I be of further\nassistance?");
            if (b.n + (int)sizeof(k_owner_menu_tail) > b.cap) {
                return 0;
            }
            memcpy(&b.d[b.n], k_owner_menu_tail, sizeof(k_owner_menu_tail));
            b.n += (int)sizeof(k_owner_menu_tail);
            n = rd_menu_patch(b.d, b.n, b.cap); /* the same choices as the vanilla menu (patched while armed) */
            if (n > 0) {
                b.n = n;
            }
            break;
        }
        default:
            return 0;
    }
    return b.err ? 0 : b.n;
}

void pc_room_dlg_set_reason(int reason) {
    s_reason = reason;
}

int pc_room_dlg_build_id(int id, unsigned char* dst, int cap) {
    if (!pc_room_dlg_msg_id_valid(id)) {
        return 0;
    }
    return pc_room_dlg_msg_build(id - (PC_ROOM_DLG_MSG_MAX + PC_ROOM_DLG_MSG_OFS), s_reason, dst, cap);
}

int pc_room_dlg_sel_build_id(int id, unsigned char* dst16) {
    const char* t;
    int i, len;
    if (!pc_room_dlg_sel_id_valid(id) || dst16 == NULL) {
        return 0;
    }
    switch (id - (PC_NOOK_SEL_BASE + PC_ROOM_DLG_SEL_OFS)) {
        case PC_ROOM_SEL_ROOMMATES: t = "Roommates"; break;
        case PC_ROOM_SEL_REQUESTS: t = "Move-in requests"; break;
        case PC_ROOM_SEL_ASK: t = "Ask to move in"; break;
        case PC_ROOM_SEL_WITHDRAW: t = "Withdraw request"; break;
        case PC_ROOM_SEL_MOVE_OUT: t = "Move out"; break;
        case PC_ROOM_SEL_LET_IN: t = "Let them in."; break;
        case PC_ROOM_SEL_TURN_DOWN: t = "Turn them down."; break;
        case PC_ROOM_SEL_LATER: t = "Decide later."; break;
        case PC_ROOM_SEL_ASK_OUT: t = "Ask to move out"; break;
        case PC_ROOM_SEL_LEAVE_BE: t = "Leave it be."; break;
        default: return 0;
    }
    len = (int)strlen(t);
    for (i = 0; i < 16; i++) {
        dst16[i] = (unsigned char)(i < len ? t[i] : ' ');
    }
    return 1;
}

/* the size of the control code at data[i] (data[i] == 0x7F): the codes of the gyroid rows (include/m_font.h mFont_cont_info_tbl), 2 for the plain ones */
static int rd_code_size(const unsigned char* data, int i) {
    const int c = data[i + 1];
    if (c == RD_PAUSE || c == RD_SNDTRGSYS) return 3;
    if (c >= 0x08 && c <= 0x0C) return 5; /* SET_DEMO_ORDER_* */
    if ((c >= RD_NEXT0 - 1 && c <= RD_NEXT0 + 3) || c == RD_NEXT4 || c == RD_NEXT4 + 1) return 4;
    if (c == RD_SEL2) return 6;
    if (c == RD_SEL3) return 8;
    if (c == RD_SEL4) return 10;
    if (c == RD_SEL5) return 12;
    if (c == RD_SEL6) return 14;
    if (c == 0x05) return 6; /* COLOR */
    return 2;
}

static int rd_sel_count(int code) {
    return code == RD_SEL2 ? 2 : code == RD_SEL3 ? 3 : code == RD_SEL4 ? 4 : code == RD_SEL5 ? 5 : code == RD_SEL6 ? 6 : 0;
}

static int rd_sel_code(int n) {
    return n == 2 ? RD_SEL2 : n == 3 ? RD_SEL3 : n == 4 ? RD_SEL4 : n == 5 ? RD_SEL5 : n == 6 ? RD_SEL6 : 0;
}

static int rd_next_code(int k) { /* SET_NEXT_MESSAGE_k */
    return k <= 3 ? RD_NEXT0 + k : RD_NEXT4 + (k - 4);
}

/* the first choice list of a message: its offset (data[off] == 0x7F) and its ids; -1 = none */
static int rd_find_sel(const unsigned char* data, int len, int* ids, int* n_out) {
    int i = 0, k, n;
    while (i + 1 < len) {
        if (data[i] != RD_CC) {
            i++;
            continue;
        }
        if (data[i + 1] == RD_END || data[i + 1] == RD_CONTINUE) {
            return -1;
        }
        n = rd_sel_count(data[i + 1]);
        if (n > 0) {
            if (i + 2 + 2 * n > len) {
                return -1;
            }
            for (k = 0; k < n; k++) {
                ids[k] = (data[i + 2 + 2 * k] << 8) | data[i + 3 + 2 * k];
            }
            *n_out = n;
            return i;
        }
        i += rd_code_size(data, i);
    }
    return -1;
}

int pc_room_dlg_patch_menu(unsigned char* data, int len, int cap, int sel_id, int next_msg_id, int* choice_out) {
    int ids[6], n = 0, at, last, i, nx = -1, old_hi = 0, old_lo = 0;
    if (data == NULL || len < 4 || (at = rd_find_sel(data, len, ids, &n)) < 0 || n < 2 || n >= 6 || len + 2 > cap) {
        return 0;
    }
    /* the next message of the old last choice (SETNEXTMSG<n-1>), when the message names it */
    for (i = 0; i + 1 < len;) {
        if (data[i] != RD_CC) {
            i++;
            continue;
        }
        if (data[i + 1] == RD_END || data[i + 1] == RD_CONTINUE) {
            break;
        }
        if (data[i + 1] == rd_next_code(n - 1) && i + 4 <= len) {
            nx = i;
            break;
        }
        i += rd_code_size(data, i);
    }
    if (nx >= 0 && (next_msg_id < 0 || len + 6 > cap)) {
        return 0;
    }
    last = at + 2 + 2 * (n - 1);
    memmove(&data[last + 2], &data[last], (size_t)(len - last));
    data[last] = (unsigned char)((sel_id >> 8) & 0xFF);
    data[last + 1] = (unsigned char)(sel_id & 0xFF);
    data[at + 1] = (unsigned char)rd_sel_code(n + 1);
    len += 2;
    if (nx >= 0) {
        nx += 2; /* (after the inserted id) */
        old_hi = data[nx + 2];
        old_lo = data[nx + 3];
        data[nx + 2] = (unsigned char)((next_msg_id >> 8) & 0xFF);
        data[nx + 3] = (unsigned char)(next_msg_id & 0xFF);
        memmove(&data[nx + 8], &data[nx + 4], (size_t)(len - (nx + 4)));
        data[nx + 4] = RD_CC;
        data[nx + 5] = (unsigned char)rd_next_code(n);
        data[nx + 6] = (unsigned char)old_hi;
        data[nx + 7] = (unsigned char)old_lo;
        len += 4;
    }
    if (choice_out != NULL) {
        *choice_out = n - 1;
    }
    return len;
}

void pc_room_dlg_arm(int mode, int sel, int next_msg_id) {
    s_arm_mode = (mode == PC_ROOM_DLG_ARM_OWNER || mode == PC_ROOM_DLG_ARM_GUEST) ? mode : 0;
    s_arm_sel = sel;
    s_arm_next = next_msg_id;
    if (s_arm_mode == 0) {
        s_menu_choice = -1;
    }
}

int pc_room_dlg_armed(void) {
    return s_arm_mode;
}

int pc_room_dlg_menu_choice(void) {
    return s_arm_mode != 0 ? s_menu_choice : -1;
}

static int rd_is_owner_menu(int index) {
    return index == PC_ROOM_VMSG_OWNER_FIRST || index == PC_ROOM_VMSG_OWNER_AGAIN || index == 0x0930 || index == 0x0933 || index == 0x0936;
}

/* an owner menu (vanilla bytes or PC_ROOM_DLG_OWNER_MENU) while the holder is armed: exactly the 4 vanilla choices get the inserted one before End */
static int rd_menu_patch(unsigned char* data, int len, int cap) {
    int ids[6], n = 0, k, c = -1, out;
    if (s_arm_mode != PC_ROOM_DLG_ARM_OWNER || rd_find_sel(data, len, ids, &n) < 0 || n != 4) {
        s_menu_choice = -1;
        return 0;
    }
    for (k = 0; k < 4; k++) {
        if (ids[k] != k_owner_ids[k]) {
            s_menu_choice = -1;
            return 0;
        }
    }
    out = pc_room_dlg_patch_menu(data, len, cap, pc_room_dlg_sel_id(s_arm_sel), s_arm_next, &c);
    s_menu_choice = out > 0 ? c : -1;
    return out;
}

int pc_room_dlg_msg_patch(int index, unsigned char* data, int len, int cap) {
    int ids[6], n = 0, c = -1, out;
    if (rd_is_owner_menu(index)) {
        if (s_arm_mode != PC_ROOM_DLG_ARM_OWNER) {
            return 0;
        }
        out = rd_menu_patch(data, len, cap);
        printf("[ROOM][DLG] gyroid menu 0x%04X: %s\n", index, out > 0 ? "the roommates choice added" : "*** not the vanilla choices: left as it is ***");
        return out;
    }
    if (index == PC_ROOM_VMSG_GUEST_MENU) {
        if (s_arm_mode != PC_ROOM_DLG_ARM_GUEST) {
            return 0;
        }
        if (rd_find_sel(data, len, ids, &n) < 0 || n != 2 || ids[0] != k_guest_ids[0] || ids[1] != k_guest_ids[1]) {
            s_menu_choice = -1;
            printf("[ROOM][DLG] gyroid guest menu 0x%04X: *** not the vanilla choices: left as it is ***\n", index);
            return 0;
        }
        out = pc_room_dlg_patch_menu(data, len, cap, pc_room_dlg_sel_id(s_arm_sel), -1, &c);
        s_menu_choice = out > 0 ? c : -1;
        printf("[ROOM][DLG] gyroid guest menu 0x%04X: %s\n", index, out > 0 ? "the move-in choice added" : "*** no room: left as it is ***");
        return out;
    }
    return 0;
}

/* ---- selftest ---- */
static int rd_char_ok(int c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == ' ' || c == '!' || c == '\'' || c == ',' || c == '-' || c == '.' || c == '?' ||
           c == ':' || c == RD_NEWLINE;
}

static int rd_check_bytes(const unsigned char* b, int n, const char* what) {
    int i, bad = 0;
    if (n < 2 || b[n - 2] != RD_CC || (b[n - 1] != RD_END && b[n - 1] != RD_CONTINUE)) {
        printf("[ROOM][DLG] selftest: %s does not end with MSGEND / MSGCONTINUE\n", what);
        return 1;
    }
    for (i = 0; i < n;) {
        if (b[i] == RD_CC) {
            i += rd_code_size(b, i);
        } else {
            if (!rd_char_ok(b[i])) {
                printf("[ROOM][DLG] selftest: %s byte %d = 0x%02X is outside the charset\n", what, i, b[i]);
                bad++;
            }
            i++;
        }
    }
    return bad;
}

int pc_room_dlg_selftest(void) {
    /* the vanilla 0x0925 bytes (forest_2nd.arc message_data.bin) */
    static const unsigned char v925[] = {
        'W', 'e', 'l', 'c', 'o', 'm', 'e', ' ', 'h', 'o', 'm', 'e', ',', ' ', 0x7F, 0x1A, '!', 0x7F, 0x03, 0x08, 0xCD, 'H', 'o', 'w', ' ', 'm', 'a', 'y', ' ', 'I', ' ',
        'b', 'e', ' ', 'o', 'f', ' ', 'a', 's', 's', 'i', 's', 't', 'a', 'n', 'c', 'e', '?', 0x7F, 0x09, 0x09, 0x00, 0x01, 0x7F, 0x18, 0x00, 0x5E, 0x00, 0x14, 0x01,
        0xB3, 0x00, 0x15, 0x7F, 0x5E, 0x7F, 0x04, 0x7F, 0x0D, 0x7F, 0x0F, 0x09, 0x2F, 0x7F, 0x10, 0x09, 0x27, 0x7F, 0x11, 0x09, 0x31, 0x7F, 0x12, 0x09, 0x26, 0x7F,
        0x19, 0xCD, 0x7F, 0x01
    };
    /* the vanilla 0x0929 bytes */
    static const unsigned char v929[] = {
        'B', 'y', ' ', 't', 'h', 'e', ' ', 'w', 'a', 'y', ',', 0x7F, 0x03, 0x04, ' ', 'h', 'o', 'w', ' ', 'm', 'a', 'y', ' ', 'I', 0xCD, 'b', 'e', ' ', 'o', 'f', ' ',
        'a', 's', 's', 'i', 's', 't', 'a', 'n', 'c', 'e', '?', 0x7F, 0x16, 0x00, 0x16, 0x00, 0x15, 0x7F, 0x5E, 0x7F, 0x04, 0x7F, 0x0D, 0x7F, 0x19, 0xCD, 0x7F, 0x09,
        0x00, 0x00, 0x01, 0xCD, 0x7F, 0x01
    };
    static unsigned char buf[1024];
    char what[48];
    int w, r, n, bad = 0, sel, i;
    const int saved_mode = s_arm_mode, saved_sel = s_arm_sel, saved_next = s_arm_next, saved_choice = s_menu_choice;
    for (w = 0; w < PC_ROOM_DLG_COUNT; w++) {
        for (r = 0; r <= PC_ROOM_R_MAX + 1; r++) {
            const int reason = r == PC_ROOM_R_MAX + 1 ? PC_ROOM_DLG_REASON_LINK : r;
            pc_room_dlg_arm(PC_ROOM_DLG_ARM_OWNER, PC_ROOM_SEL_REQUESTS, pc_room_dlg_msg_id(PC_ROOM_DLG_OWNER_REQ_Q));
            n = pc_room_dlg_msg_build(w, reason, buf, (int)sizeof(buf));
            snprintf(what, sizeof(what), "row %d (reason %d)", w, reason);
            if (n <= 0) {
                printf("[ROOM][DLG] selftest: %s did not build\n", what);
                bad++;
                continue;
            }
            bad += rd_check_bytes(buf, n, what);
            if (w != PC_ROOM_DLG_REFUSED_END && w != PC_ROOM_DLG_OWNER_REFUSED) {
                break; /* (the reason matters to the refusal rows only) */
            }
        }
    }
    if (pc_room_dlg_msg_build(PC_ROOM_DLG_COUNT, 0, buf, (int)sizeof(buf)) != 0 || pc_room_dlg_msg_build(PC_ROOM_DLG_ASK_Q, 0, buf, 8) != 0) {
        bad++; /* unknown row / no room */
    }
    for (sel = 0; sel < PC_ROOM_SEL_COUNT; sel++) {
        unsigned char s16[16];
        if (!pc_room_dlg_sel_build_id(pc_room_dlg_sel_id(sel), s16)) {
            bad++;
            continue;
        }
        for (i = 0; i < 16; i++) {
            if (!rd_char_ok(s16[i]) || s16[i] == RD_NEWLINE) {
                bad++;
            }
        }
    }
    /* the holder's first menu: SEL4 -> SEL5 with the choice before End, NEXT3 -> ours, NEXT4 = the old End row */
    pc_room_dlg_arm(PC_ROOM_DLG_ARM_OWNER, PC_ROOM_SEL_ROOMMATES, 0x3FAB);
    memcpy(buf, v925, sizeof(v925));
    n = pc_room_dlg_msg_patch(PC_ROOM_VMSG_OWNER_FIRST, buf, (int)sizeof(v925), (int)sizeof(buf));
    {
        static const unsigned char want[] = { 0x7F, 0x79, 0x00, 0x5E, 0x00, 0x14, 0x01, 0xB3, 0x02, 0x9F, 0x00, 0x15, 0x7F, 0x5E, 0x7F, 0x04, 0x7F, 0x0D, 0x7F, 0x0F,
                                              0x09, 0x2F, 0x7F, 0x10, 0x09, 0x27, 0x7F, 0x11, 0x09, 0x31, 0x7F, 0x12, 0x3F, 0xAB, 0x7F, 0x77, 0x09, 0x26, 0x7F, 0x19,
                                              0xCD, 0x7F, 0x01 };
        if (PC_NOOK_SEL_BASE + PC_ROOM_DLG_SEL_OFS + PC_ROOM_SEL_ROOMMATES != 0x029F || n != (int)sizeof(v925) + 6 || memcmp(&buf[53], want, sizeof(want)) != 0 ||
            pc_room_dlg_menu_choice() != 3 || memcmp(buf, v925, 53) != 0) {
            printf("[ROOM][DLG] selftest: the holder's menu patch is wrong (len %d, choice %d)\n", n, pc_room_dlg_menu_choice());
            bad++;
        }
    }
    /* our own "anything else" row carries the same patched menu */
    n = pc_room_dlg_msg_build(PC_ROOM_DLG_OWNER_MENU, 0, buf, (int)sizeof(buf));
    if (n <= 0 || pc_room_dlg_menu_choice() != 3 || buf[n - 2] != RD_CC || buf[n - 1] != RD_CONTINUE) {
        bad++;
    }
    /* not armed / armed for the guest: the holder's menu stays vanilla */
    pc_room_dlg_arm(0, 0, -1);
    memcpy(buf, v925, sizeof(v925));
    if (pc_room_dlg_msg_patch(PC_ROOM_VMSG_OWNER_FIRST, buf, (int)sizeof(v925), (int)sizeof(buf)) != 0 || memcmp(buf, v925, sizeof(v925)) != 0 || pc_room_dlg_menu_choice() != -1) {
        bad++;
    }
    pc_room_dlg_arm(PC_ROOM_DLG_ARM_GUEST, PC_ROOM_SEL_ASK, -1);
    if (pc_room_dlg_msg_patch(PC_ROOM_VMSG_OWNER_FIRST, buf, (int)sizeof(v925), (int)sizeof(buf)) != 0) {
        bad++;
    }
    /* the guest menu: SEL2 -> SEL3 with the choice before Leave (no next-message codes) */
    memcpy(buf, v929, sizeof(v929));
    n = pc_room_dlg_msg_patch(PC_ROOM_VMSG_GUEST_MENU, buf, (int)sizeof(v929), (int)sizeof(buf));
    if (n != (int)sizeof(v929) + 2 || buf[43] != RD_SEL3 || buf[44] != 0x00 || buf[45] != 0x16 || buf[46] != 0x02 || buf[47] != 0xA1 || buf[48] != 0x00 || buf[49] != 0x15 ||
        memcmp(&buf[50], &v929[48], sizeof(v929) - 48) != 0 || pc_room_dlg_menu_choice() != 1) {
        printf("[ROOM][DLG] selftest: the guest menu patch is wrong (len %d)\n", n);
        bad++;
    }
    /* a different choice list (another message, a changed table) is never patched */
    {
        unsigned char odd[96];
        memcpy(odd, v929, sizeof(v929));
        odd[45] = 0x17;
        if (pc_room_dlg_msg_patch(PC_ROOM_VMSG_GUEST_MENU, odd, (int)sizeof(v929), (int)sizeof(odd)) != 0 || pc_room_dlg_menu_choice() != -1) {
            bad++;
        }
    }
    /* no room in the buffer: untouched */
    memcpy(buf, v929, sizeof(v929));
    if (pc_room_dlg_patch_menu(buf, (int)sizeof(v929), (int)sizeof(v929) + 1, 0x02A1, -1, NULL) != 0 || memcmp(buf, v929, sizeof(v929)) != 0) {
        bad++;
    }
    s_arm_mode = saved_mode;
    s_arm_sel = saved_sel;
    s_arm_next = saved_next;
    s_menu_choice = saved_choice;
    return bad;
}
