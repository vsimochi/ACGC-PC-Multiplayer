#ifndef PC_NOOK_HOUSE_H
#define PC_NOOK_HOUSE_H

/* Guest Nook dialogue (guest-first town milestone M4): the TEXT of the "buy a house" offer.
 *
 * The port has no message override system, so this module is the small one: message ids >= MSG_MAX (the real table ends at 0x3F90) are GENERATED here, byte for byte in the
 * game's own message format (control codes, 2-byte choice string ids), and mMsg_LoadMsgData() (m_msg_main.c_inc) calls pc_nook_msg_build() for them instead of reading ARAM.
 * The same for choice strings >= mChoice_SELECT_STR_NUM (mChoice_Load_ChoseStringFromRom, m_choice_main.c_inc) -> pc_nook_sel_build().
 * The shop actor (ac_npc_shop_common.c) drives the dialogue (state machine aNSC_pc_house_proc); this file knows nothing about it except the few values it must print.
 * Charset: letters, digits, space and ! ' , - . ? : only (the font has no arbitrary punctuation; '/' draws a music note). pc_nook_house_selftest() checks every byte. */

#define PC_NOOK_MSG_COUNT 28
enum {
    PC_NOOK_MSG_INTRO = 0, /* first talk: greeting + "would you like a house?" Yes./No. (the answer is read by the actor) */
    PC_NOOK_MSG_OTHER,     /* the guest variant of the "Other things" submenu (vanilla message + a 4th choice "Buy a house.") */
    PC_NOOK_MSG_PICK,      /* price + "which house?" (the free houses, Any house., Never mind...) */
    PC_NOOK_MSG_CONFIRM,   /* "House N, 18,400 Bells, shall we make it official?" Yes./No. */
    PC_NOOK_MSG_DECLINE,   /* Nook accepts a No. */
    PC_NOOK_MSG_NOFUNDS,   /* not enough Bells */
    PC_NOOK_MSG_NOLOT,     /* no free lot / house */
    PC_NOOK_MSG_FAILED,    /* the host refused (a HOST reason): nothing was charged */
    PC_NOOK_MSG_THANKS,    /* APPLIED: congratulations */
    PC_NOOK_MSG_LINKLOST,  /* the link was lost while the request was in flight (reason 0): the outcome is UNKNOWN, so the text never says "nothing was charged" */
    PC_NOOK_MSG_NAMETAKEN, /* reason 30 NAME_TAKEN: a resident of the town already has the guest's name */
    /* Nook's shop manual restock (every player, 500 Bells): */
    PC_NOOK_MSG_OTHER_R,         /* the resident / host variant of the "Other things" submenu (vanilla message + "Refresh shop.") */
    PC_NOOK_MSG_RESTOCK_ASK,     /* 500 Bells, the shop closes for a minute, everyone inside is shown out: Yes./No. */
    PC_NOOK_MSG_RESTOCK_DECLINE, /* Nook accepts a No. */
    PC_NOOK_MSG_RESTOCK_THANKS,  /* APPLIED: paid, the shop is restocking, please step outside */
    PC_NOOK_MSG_RESTOCK_NOFUNDS, /* not enough Bells (nothing charged) */
    PC_NOOK_MSG_RESTOCK_BUSY,    /* a restock is already running (nothing charged) */
    PC_NOOK_MSG_RESTOCK_FAILED,  /* the host refused for another reason (nothing charged) */
    PC_NOOK_MSG_RESTOCK_LINKLOST,/* the link was lost in flight: the outcome is unknown */
    PC_NOOK_MSG_RESTOCK_DOOR,    /* the shop door while restocking */
    /* Nook Work Mode ("I'd like to work." under Other things; every player): */
    PC_NOOK_MSG_WORK_JOB,        /* the job (item + reward) and "Do you have it?": Here it is! / I'll fetch it. / I'm done working. */
    PC_NOOK_MSG_WORK_DONE,       /* APPLIED delivery: the reward was paid */
    PC_NOOK_MSG_WORK_NOITEM,     /* the player does not carry the objective item (nothing sent) */
    PC_NOOK_MSG_WORK_LATER,      /* "I'll fetch it.": come back with the item */
    PC_NOOK_MSG_WORK_LEFT,       /* left Work Mode */
    PC_NOOK_MSG_WORK_FAILED,     /* the host refused (stale / no job): nothing changed */
    PC_NOOK_MSG_WORK_LINKLOST,   /* the link was lost in flight: the outcome is unknown */
    PC_NOOK_MSG_WORK_FULL        /* the reward would overflow the wallet: the job stays */
};

#define PC_NOOK_SEL_BASE 607 /* == mChoice_SELECT_STR_NUM: the first reserved choice string id */
enum { PC_NOOK_SEL_BUY_HOUSE = PC_NOOK_SEL_BASE, PC_NOOK_SEL_HOUSE1, PC_NOOK_SEL_HOUSE2, PC_NOOK_SEL_HOUSE3, PC_NOOK_SEL_HOUSE4, PC_NOOK_SEL_ANY_HOUSE, PC_NOOK_SEL_REFRESH, PC_NOOK_SEL_WORK, PC_NOOK_SEL_WORK_HERE, PC_NOOK_SEL_WORK_LATER, PC_NOOK_SEL_WORK_LEAVE, PC_NOOK_SEL_COUNT_END };
/* one choice string per house: a different PC_RESIDENCE_HOUSES needs more PC_NOOK_SEL_HOUSEn entries (and Nook's choice paging beyond 6 choices; see the roadmap) */
_Static_assert(PC_NOOK_SEL_ANY_HOUSE - PC_NOOK_SEL_HOUSE1 == 4, "one PC_NOOK_SEL_HOUSEn per house (PC_RESIDENCE_HOUSES)");
#define PC_NOOK_SEL_COUNT (PC_NOOK_SEL_COUNT_END - PC_NOOK_SEL_BASE)

#ifdef __cplusplus
extern "C" {
#endif

/* The dialogue-side helpers implemented in pc_net_game.c (declared HERE, not in pc_net_game.h, which is included by almost every translation unit). pc_net_game_house_offer_available()
 * = CLIENT role + READY link + the local player is a GUEST (the only case in which Nook offers a house); pc_net_game_house_free_list() = the FREE houses (0..3, ownerID null) of the
 * LOCAL town copy, returns the count; pc_net_game_nook_intro_get/_set = the membership.ini key `nook_intro` (absent / declined / bought): get returns 1 present, 0 absent, -1 no
 * store character (the dialogue then remembers the answer in RAM); set returns 1 when written. */
int pc_net_game_house_offer_available(void);
int pc_net_game_house_free_list(int* out, int cap);
int pc_net_game_nook_intro_get(char* out, unsigned long cap);
int pc_net_game_nook_intro_set(const char* value);
/* 1 once an APPLIED HOUSE_PURCHASE was applied locally (sticky until the next begin): the dialogue treats a link-loss REJECTED after it as APPLIED (see pc_net_game.c). */
int pc_net_game_house_purchase_applied(void);
/* Nook's shop manual restock (pc_net_game.c). offer_available: the host's own player or a READY client; precheck: 0 may be tried else a PC_NETGAME_TXN_REASON_* (31 RESTOCKING,
 * 19 NO_FUNDS, 9 PRECOND); begin: HOST role pays and starts at once (3), CLIENT sends the request (1, then poll pc_net_game_ts_poll()), 0 refused locally, -1 busy;
 * shop_restocking: 1 while the shop is closed for a restock (host: real state, client: the last HOST_CONFIG). */
int pc_net_game_restock_offer_available(void);
int pc_net_game_restock_precheck(void);
int pc_net_game_restock_begin(void);
int pc_net_game_restock_price(void);
int pc_net_game_shop_restocking(void);
/* Nook Work Mode (pc_net_game.c). offer_available: the host's own player (world ready) or a READY client. info: the character's job as the host last told us (host role: its own
 * table): returns 0 when unknown yet, else 1 and fills job_id / the objective item / the reward / state (0 none, 1 active) / mode_on / the jobs done. begin(op): op 1 ENTER (start Work
 * Mode and receive or resume the job), 2 DELIVER (hand in the objective item), 3 LEAVE; HOST role runs the host rules at once and returns 3 (done) or 0 (refused,
 * pc_net_game_ts_last_reject_reason()); CLIENT: 1 = request sent (poll pc_net_game_ts_poll()), 0 = refused locally, -1 = busy (retry next frame). */
int pc_net_game_work_offer_available(void);
int pc_net_game_work_info(unsigned* job_id, int* obj_item, unsigned* reward, int* state, int* mode_on, unsigned* jobs_done);
int pc_net_game_work_begin(int op);

/* The in-process rejoin of the promoted guest (pc_main.c pc_main_play_online_poll) WAITS while the Nook congratulation row is still being read: the shop sets the hold when it
 * shows the row and clears it when the conversation ended; pc_nook_house_rejoin_hold() (polled once per frame by the rejoin) returns 1 while the hold is on. */
/* HEARTBEAT (replaces the fixed ~40 s crutch): set(1) = a purchase is in flight, set(2) = APPLIED, set(0) = released. While the hold is on the shop proc calls
 * pc_nook_house_rejoin_hold_touch() EVERY frame (PENDING / END); the hold expires 500 ms (30 frames) of WALL CLOCK after the last touch (the shop proc stopped: scene change, pause, walked away) and, in
 * any case, ~15 s (900 frames) after APPLIED. pc_nook_house_rejoin_hold() is polled once per frame by the rejoin. */
void pc_nook_house_rejoin_hold_set(int on);
void pc_nook_house_rejoin_hold_touch(void);
int pc_nook_house_rejoin_hold(void);

/* The id of PC message `which` (PC_NOOK_MSG_*), and the range tests used by the bounds checks of the message system. */
int pc_nook_msg_id(int which);
int pc_nook_msg_id_valid(int id);
int pc_nook_sel_id_valid(int id);

/* Builds the bytes of message `id` into dst (cap bytes); returns the length (including the terminating MSGEND / MSGCONTINUE code) or 0 when the id is not ours. */
int pc_nook_msg_build(int id, unsigned char* dst, int cap);
/* Builds the 16-byte choice string `id` (space padded); returns 1 or 0. */
int pc_nook_sel_build(int id, unsigned char* dst16);

/* The dynamic values of PC_NOOK_MSG_PICK / PC_NOOK_MSG_CONFIRM (set by the actor right before it selects the message). pick: the FREE houses 0..3 listed as choices (n >= 1).
 * confirm: the house the player chose (0..3) or -1 = any. */
void pc_nook_house_set_pick(const int* houses, int n);
void pc_nook_house_set_confirm(int house_or_auto);
/* The dynamic values of the Work Mode rows (set by the actor before it selects PC_NOOK_MSG_WORK_*): the objective item (an ITM_FOOD_* fruit) and the reward in Bells. */
void pc_nook_house_set_work(int item, unsigned reward);

/* Source audit helper: builds every PC message, returns 0 when all bytes are in the charset and every message ends with MSGEND / MSGCONTINUE. */
int pc_nook_house_selftest(void);

#ifdef __cplusplus
}
#endif

#endif
