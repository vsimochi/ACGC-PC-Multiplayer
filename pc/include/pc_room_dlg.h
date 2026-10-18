/* pc_room_dlg.h - ROOMMATES / MOVE-IN: the TEXT of the move-in dialogue inside the vanilla gyroid conversation (docs/multiplayer-guest-roadmap.md "Roommates / move-in").
 * Pure (no game state; native selftest pc_room_dlg_selftest()). The rows are generated in the game's own message byte format, like the Nook rows of pc_nook_house.c:
 * message ids MSG_MAX + PC_ROOM_DLG_MSG_OFS + k and choice string ids PC_NOOK_SEL_BASE + PC_ROOM_DLG_SEL_OFS + k are routed here by pc_nook_msg_build / pc_nook_sel_build,
 * and pc_nook_msg_patch hands every loaded vanilla message to pc_room_dlg_msg_patch (the gyroid's own menus get one more choice while a conversation is ARMED).
 * Names travel through the vanilla free string 10 (the rows print it; pc_room_ui.c sets it with mMsg_Set_free_str). Charset as pc_nook_house.h. */
#ifndef PC_ROOM_DLG_H
#define PC_ROOM_DLG_H

#ifdef __cplusplus
extern "C" {
#endif

#define PC_ROOM_DLG_MSG_MAX 0x3F91 /* == MSG_MAX (include/m_msg_data.h; pc_nook_house.c asserts it): this file stays free of game headers */
#define PC_ROOM_DLG_MSG_OFS 64 /* message id = MSG_MAX + 64 + PC_ROOM_DLG_* (after the Nook rows: room for them to grow) */
#define PC_ROOM_DLG_SEL_OFS 64 /* choice string id = PC_NOOK_SEL_BASE + 64 + PC_ROOM_SEL_* */

enum {
    /* guest */
    PC_ROOM_DLG_ASK_Q = 0,   /* "This residence belongs to X. Shall I ask X if you may move in?" Yes. / No. */
    PC_ROOM_DLG_WITHDRAW_Q,  /* "Your request awaits X's answer. Shall I withdraw it?" Yes. / No. */
    PC_ROOM_DLG_MOVEOUT_Q,   /* "You reside here with X. Do you wish to move out?" Yes. / No. */
    PC_ROOM_DLG_WAIT,        /* "Processing request..." (stops at its continue point until the host answered: FORCENEXT) */
    PC_ROOM_DLG_ASK_SENT,    /* closing rows (MSGEND) */
    PC_ROOM_DLG_WITHDRAWN,
    PC_ROOM_DLG_MOVED_OUT,
    PC_ROOM_DLG_REFUSED_END, /* "My apologies." + the reason (closing row) */
    /* holder */
    PC_ROOM_DLG_OWNER_NONE,       /* no request, no roommate (continue row) */
    PC_ROOM_DLG_OWNER_REQ_Q,      /* "X wishes to move in with you." Let them in. / Turn them down. / Decide later. */
    PC_ROOM_DLG_OWNER_ACCEPTED,   /* continue rows: the actor goes on with the next request / roommate or back to the menu */
    PC_ROOM_DLG_OWNER_DENIED,
    PC_ROOM_DLG_OWNER_RM_Q,       /* "X resides here with you." Ask to move out / Leave it be. */
    PC_ROOM_DLG_OWNER_RM_CONFIRM, /* "Shall I really ask X to move out?" Yes. / No. */
    PC_ROOM_DLG_OWNER_REMOVED,
    PC_ROOM_DLG_OWNER_REFUSED,    /* "My apologies." + the reason (continue row) */
    PC_ROOM_DLG_OWNER_MENU,       /* "May I be of further assistance?" + the gyroid's own menu (the vanilla 0x0927 choices + the inserted one) */
    PC_ROOM_DLG_COUNT
};

enum {
    PC_ROOM_SEL_ROOMMATES = 0, /* the holder's menu: "Roommates" (no request waiting) / "Move-in requests" */
    PC_ROOM_SEL_REQUESTS,
    PC_ROOM_SEL_ASK,           /* the guest's menu: "Ask to move in" / "Withdraw request" / "Move out" */
    PC_ROOM_SEL_WITHDRAW,
    PC_ROOM_SEL_MOVE_OUT,
    PC_ROOM_SEL_LET_IN,
    PC_ROOM_SEL_TURN_DOWN,
    PC_ROOM_SEL_LATER,
    PC_ROOM_SEL_ASK_OUT,
    PC_ROOM_SEL_LEAVE_BE,
    PC_ROOM_SEL_COUNT
};

/* the reason of the REFUSED rows: a PC_ROOM_R_* (pc_roommates.h) or PC_ROOM_DLG_REASON_LINK (no answer / the link went away) */
#define PC_ROOM_DLG_REASON_LINK 0xFF

/* the vanilla gyroid rows the dialogue builds on (ac_haniwa_move.c_inc) */
#define PC_ROOM_VMSG_OWNER_FIRST  0x0925 /* "Welcome home, <player>! How may I be of assistance?" (the 4-choice menu) */
#define PC_ROOM_VMSG_OWNER_AGAIN  0x0927 /* "Request processed. May I be of further assistance?" (the same menu) */
#define PC_ROOM_VMSG_GUEST_MENU   0x0929 /* "By the way, how may I be of assistance?" (2 choices) */
#define PC_ROOM_VMSG_GUEST_BYE    0x092A /* "Very well. Please come again." (closing) */
#define PC_ROOM_VMSG_OWNER_BYE    0x0926 /* "Is that so? Very well. Take care." (closing) */

int pc_room_dlg_msg_id(int which);         /* MSG_MAX + PC_ROOM_DLG_MSG_OFS + which */
int pc_room_dlg_sel_id(int which);         /* PC_NOOK_SEL_BASE + PC_ROOM_DLG_SEL_OFS + which */
int pc_room_dlg_msg_id_valid(int id);
int pc_room_dlg_sel_id_valid(int id);

/* message `which` (PC_ROOM_DLG_*) into dst: the length (ending with MSGEND / MSGCONTINUE), 0 = unknown. `reason` is used by the REFUSED rows. */
int pc_room_dlg_msg_build(int which, int reason, unsigned char* dst, int cap);
/* the message system's entry points (pc_nook_msg_build / pc_nook_sel_build route the ids here): the REFUSED rows print the reason last set by pc_room_dlg_set_reason */
int pc_room_dlg_build_id(int id, unsigned char* dst, int cap);
int pc_room_dlg_sel_build_id(int id, unsigned char* dst16);
void pc_room_dlg_set_reason(int reason);

/* inserts choice string id `sel_id` right before the LAST choice of the first SETSELSTR2..5 of a message; when the message names the next message of that last choice
 * (SETNEXTMSG<k-1>), `next_msg_id` becomes the inserted choice's next message and the old one moves to the new last choice (SETNEXTMSG<k>). Returns the new length
 * (0 = not patched: no choice list, 6 choices already, no room); *choice_out = the inserted choice's index. */
int pc_room_dlg_patch_menu(unsigned char* data, int len, int cap, int sel_id, int next_msg_id, int* choice_out);

/* THE ARM (pc_room_ui.c, one gyroid conversation): mode 0 = off, PC_ROOM_DLG_ARM_OWNER = the holder's menus (0x0925 / 0x0927 / 0x0930 / 0x0933 / 0x0936 and
 * PC_ROOM_DLG_OWNER_MENU) get `sel` (a PC_ROOM_SEL_*) before "End", whose next message is `next_msg_id`; PC_ROOM_DLG_ARM_GUEST = the guest menu 0x0929 gets `sel` before
 * its last choice (the actor sets the next message). A message is patched only when its choice list is exactly the vanilla one. */
#define PC_ROOM_DLG_ARM_OWNER 1
#define PC_ROOM_DLG_ARM_GUEST 2
void pc_room_dlg_arm(int mode, int sel, int next_msg_id);
int  pc_room_dlg_armed(void);
/* pc_nook_msg_patch: every loaded vanilla message; returns the new length, 0 = untouched */
int  pc_room_dlg_msg_patch(int index, unsigned char* data, int len, int cap);
/* the inserted choice's index in the LAST loaded gyroid menu of the armed kind (owner menus / the guest menu), -1 = that load was not patched (vanilla choices) */
int  pc_room_dlg_menu_choice(void);

int pc_room_dlg_selftest(void); /* 0 = pass */

#ifdef __cplusplus
}
#endif
#endif
