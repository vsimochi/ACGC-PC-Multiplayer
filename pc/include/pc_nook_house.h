#ifndef PC_NOOK_HOUSE_H
#define PC_NOOK_HOUSE_H

/* Guest Nook dialogue (guest-first town milestone M4): the TEXT of the "buy a house" offer.
 *
 * The port has no message override system, so this module is the small one: message ids >= MSG_MAX (the real table ends at 0x3F90) are GENERATED here, byte for byte in the
 * game's own message format (control codes, 2-byte choice string ids), and mMsg_LoadMsgData() (m_msg_main.c_inc) calls pc_nook_msg_build() for them instead of reading ARAM.
 * The same for choice strings >= mChoice_SELECT_STR_NUM (mChoice_Load_ChoseStringFromRom, m_choice_main.c_inc) -> pc_nook_sel_build().
 * The shop actor (ac_npc_shop_common.c) drives the dialogue (state machine aNSC_pc_house_proc); this file knows nothing about it except the few values it must print.
 * Charset: letters, digits, space and ! ' , - . ? : only (the font has no arbitrary punctuation; '/' draws a music note). pc_nook_house_selftest() checks every byte. */

#define PC_NOOK_MSG_COUNT 55
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
    PC_NOOK_MSG_WORK_JOB,        /* the job row of the character's CURRENT job (type + step + reward) and its choices (pc_nook_work_row) */
    PC_NOOK_MSG_WORK_DONE,       /* APPLIED delivery: the reward was paid */
    PC_NOOK_MSG_WORK_NOITEM,     /* the player does not carry the objective item (nothing sent) */
    PC_NOOK_MSG_WORK_LATER,      /* "I'll fetch it.": come back with the item */
    PC_NOOK_MSG_WORK_LEFT,       /* left Work Mode */
    PC_NOOK_MSG_WORK_FAILED,     /* the host refused (stale / no job): nothing changed */
    PC_NOOK_MSG_WORK_LINKLOST,   /* the link was lost in flight: the outcome is unknown */
    PC_NOOK_MSG_WORK_FULL,       /* the payment would overflow the wallet: the job stays */
    PC_NOOK_MSG_WORK_PARCEL,     /* TAKE_PARCEL done: "here is the parcel for <villager>" */
    PC_NOOK_MSG_WORK_V_GIVE,     /* the villager hands over the fetched item (spoken by the villager) */
    PC_NOOK_MSG_WORK_V_RECEIVE,  /* the villager receives the parcel (+ the optional tip) */
    PC_NOOK_MSG_WORK_V_FAIL,     /* the villager does not know what this is about */
    PC_NOOK_MSG_EVNPC_GIFT_GONE, /* Gulliver: his gift of this visit was already given away (the host decided) */
    PC_NOOK_MSG_PORTER_STATION,  /* Porter (station master): replaces the vanilla "going somewhere? / return to your village" offer, which has no meaning in this multiplayer version */
    PC_NOOK_MSG_DRESSER_REFUSED, /* dresser storage (worker 3): a REJECTED / unanswered dresser request (system text: nothing changed) */
    /* Multiplayer shop upgrade (pc_shop_upgrade.h; appended: every earlier id keeps its value): */
    PC_NOOK_MSG_UPGRADE_ANNOUNCE, /* Nook has been thinking about upgrading the shop, it would help everyone in town, the shop closes for N minutes; 2 acknowledgement choices */
    PC_NOOK_MSG_UPGRADE_CLOSING,  /* the closure started (this player's request or a simultaneous one): "I'll close up right away, see you in N minutes" */
    PC_NOOK_MSG_UPGRADE_LATER,    /* the start failed (timeout / link lost / refused): "let's talk about it another time" (the shop stays open) */
    PC_NOOK_MSG_UPGRADE_DOOR,     /* the shop door while the shop is closed for the upgrade (the minutes left) */
    /* Guest mailbox (the post office girl; appended: every earlier id keeps its value): */
    PC_NOOK_MSG_POST_NOMAIL,      /* "Check my mail." with an empty guest mailbox: nothing is waiting (closing row) */
    PC_NOOK_MSG_POST_DONE,        /* after the guest's mailbox overlay closed: the letters left there stay at the post office (closing row) */
    /* House upgrades via Nook for resident clients (worker C, pc_house_upgrade.h; appended: every earlier id keeps its value): */
    PC_NOOK_MSG_UPG_WAIT,         /* talk start while the host takes the statue order / the "built" acknowledgement / the automatic basement order: a greeting that waits (CONTINUE) */
    PC_NOOK_MSG_UPG_FAILED,       /* the host refused (nothing changed): "come back later" (closing row) */
    PC_NOOK_MSG_UPG_LINKLOST,     /* the link was lost in flight: the outcome is unknown (closing row) */
    PC_NOOK_MSG_UPG_BUSY,         /* the house is busy (HOUSE_BUSY) / the paperwork changed (HOUSE_CHANGED): ask again in a moment (closing row) */
    /* Shared event NPCs (worker E, pc_evnpc_shared.h; appended: every earlier id keeps its value). Spoken by the visitor; closing rows. */
    PC_NOOK_MSG_EVC_SOLD,         /* SOLD_OUT 50 / NOT_AVAILABLE: the piece / the visit's quota is gone */
    PC_NOOK_MSG_EVC_CLAIMED,      /* CLAIMED 51: this player already got something this visit */
    PC_NOOK_MSG_EVC_FUNDS,        /* NO_FUNDS 19 */
    PC_NOOK_MSG_EVC_FAILED,       /* any other refusal / a lost link (nothing is claimed as charged) */
    PC_NOOK_MSG_EVC_OLDHOST,      /* a client of a host without the claim (0.2.x): refused locally, nothing sent */
    PC_NOOK_MSG_EVC_WISP_DONE,    /* the Wisp: somebody already returned the spirits (CLAIMED 51 for op 5) */
    PC_NOOK_MSG_EVC_NO_SPIRITS,   /* the Wisp: NO_SPIRITS 54 */
    /* Nook bulk sell basket (pc_sell_ui.h; appended: every earlier id keeps its value): */
    PC_NOOK_MSG_SELL_BASKET,      /* the quote of a closed basket: "That's N items ... X Bells for the lot" Yes./No. (read by aNSC_buy_check like the vanilla offer) */
    /* Nook Work Mode v2 (pc_work_logic.h; appended: every earlier id keeps its value). Two generated rows whose TEXT is a variant (PC_WK2_V_*) chosen by the actor
     * right before it selects them (pc_nook_house_set_wk2): WK2 = the row on screen now, WK2_NEXT = the row after it (a hand-over plays while WK2 is read). */
    PC_NOOK_MSG_WK2,
    PC_NOOK_MSG_WK2_NEXT
};

#define PC_NOOK_SEL_BASE 607 /* == mChoice_SELECT_STR_NUM: the first reserved choice string id */
enum { PC_NOOK_SEL_BUY_HOUSE = PC_NOOK_SEL_BASE, PC_NOOK_SEL_HOUSE1, PC_NOOK_SEL_HOUSE2, PC_NOOK_SEL_HOUSE3, PC_NOOK_SEL_HOUSE4, PC_NOOK_SEL_ANY_HOUSE, PC_NOOK_SEL_REFRESH, PC_NOOK_SEL_WORK, PC_NOOK_SEL_WORK_HERE, PC_NOOK_SEL_WORK_LATER, PC_NOOK_SEL_WORK_LEAVE, PC_NOOK_SEL_JOB, PC_NOOK_SEL_WORK_TAKE, PC_NOOK_SEL_WORK_REPORT,
       PC_NOOK_SEL_DRESSER_MORE, /* dresser storage: "More storage" (appended: every earlier id keeps its value) */
       PC_NOOK_SEL_UP_GREAT,     /* shop upgrade announcement: "Sounds great!" */
       PC_NOOK_SEL_UP_OK,        /* shop upgrade announcement: "All right." */
       PC_NOOK_SEL_CHECK_MAIL,   /* guest mailbox: the post office girl's "Check my mail." (appended) */
       PC_NOOK_SEL_GIVE_JOB,     /* Work v2: "Give me a job" (the first page, no job) */
       PC_NOOK_SEL_CHECK_JOB,    /* Work v2: "Check job" (the first page, an active job) */
       PC_NOOK_SEL_QUIT_JOB,     /* Work v2: "Quit job" */
       PC_NOOK_SEL_TAKE_IT,      /* Work v2: "I'll take it" (a parcel that did not fit the pockets at the assignment) */
       PC_NOOK_SEL_COUNT_END };
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
/* Nook Work Mode (pc_net_game.c). The character's job as the host last told us (host role: its own table). */
enum { PC_WORK_OP_ENTER = 1, PC_WORK_OP_DELIVER = 2, PC_WORK_OP_LEAVE = 3, PC_WORK_OP_TAKE_PARCEL = 4, PC_WORK_OP_VILLAGER_GIVE = 5, PC_WORK_OP_VILLAGER_RECEIVE = 6, PC_WORK_OP_REPORT = 7,
       /* Work v2 (pc_work_logic.h; only with the host's service 11 bit): */
       PC_WORK_OP_URGE = 8, PC_WORK_OP_QUIT = 9, PC_WORK_OP_VILLAGER_COMPLETE = 10 };
#define PC_WORK_OP_MAX_V1 7
#define PC_WORK_OP_MAX_V2 10
enum { PC_WORK_STATE_NONE = 0, PC_WORK_STATE_ACTIVE = 1 };
enum { PC_WORK_JOB_NONE = 0, PC_WORK_JOB_FRUIT = 1, PC_WORK_JOB_FETCH_VILLAGER = 2, PC_WORK_JOB_DELIVER_VILLAGER = 3, PC_WORK_JOB_MAX = 3 };
enum { PC_WORK_OBJ_START = 0, PC_WORK_OBJ_CARRYING = 1, PC_WORK_OBJ_DELIVERED = 2 };
enum { PC_WORK_TIP_NONE = 0, PC_WORK_TIP_BELLS = 1, PC_WORK_TIP_ITEM = 2 };
typedef struct PCWorkView {
    int      mode_on, state /* 0 none, 1 active */, job_type /* 1 fruit, 2 fetch from a villager, 3 deliver to a villager */, obj_state /* 0 start, 1 carrying, 2 delivered */, tip_kind;
    unsigned job_id, reward, jobs_done, tip_value;
    int      obj_item, carried_item, target_villager /* npc_id */;
    int      v2_job /* a Work v2 job */, urged /* the urgent bonus was added */, auto_done /* the last job was completed at the villager */;
} PCWorkView;
/* offer_available: the host's own player (world ready) or a READY client. view: 1 = filled, 0 = unknown yet. begin_op(op, npc): op 1 ENTER, 2 DELIVER, 3 LEAVE (quit), 4 TAKE_PARCEL, 5
 * VILLAGER_GIVE, 6 VILLAGER_RECEIVE (npc = the villager's npc_id), 7 REPORT; HOST role runs the host rules at once and returns 3 (done) or 0 (refused, pc_net_game_ts_last_reject_reason());
 * CLIENT: 1 = request sent (poll pc_net_game_ts_poll()), 0 = refused locally, -1 = busy (retry next frame). villager_pending: 1 = talking to villager `npc` advances the job (*op = 5 / 6).
 * villager_name: the villager's name as the game's name bytes (8, space padded), 0 = not in this town. */
int pc_net_game_work_offer_available(void);
int pc_net_game_work_view(PCWorkView* v);
int pc_net_game_work_begin_op(int op, int npc);
int pc_net_game_work_begin(int op);
int pc_net_game_work_villager_pending(int npc, int* op);
int pc_net_game_work_villager_name(int npc, unsigned char* out8);
/* Work v2 (pc_work_logic.h): v2() = 1 when the v2 dialogue / ops are used (host role: its own switch; client: READY + the host's service 11 bit + the local switch);
 * has_item(v) = the pockets hold what the job's next Nook step needs (FRUIT: the fruit; FETCH carrying: the QUEST item; DELIVER start: a free pocket);
 * item_name(item, out16, &article) = the game's own item name (16 bytes, space padded) + its mIN_ARTICLE_*; notice(text) = one local line in the chat log. */
int pc_net_game_work_v2(void);
int pc_net_game_work_has_item(const PCWorkView* v);
void pc_net_game_work_item_name(int item, unsigned char* out16, int* article);
void pc_net_game_work_notice(const char* ascii);

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
/* The shop door row while the shop is closed for the multiplayer shop upgrade (PC_NOOK_MSG_UPGRADE_DOOR). */
int pc_nook_upgrade_door_msg(void);
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
/* Nook bulk sell basket: the count and the (advisory) total of PC_NOOK_MSG_SELL_BASKET (set by the actor right before it selects the message). */
void pc_nook_house_set_sell(int count, unsigned total);
/* The dynamic values of the Work Mode rows (set by the actor before it selects PC_NOOK_MSG_WORK_*): the job view and the villager name bytes. */
void pc_nook_house_set_work_view(const PCWorkView* v, const unsigned char* villager_name8);
/* The choices of the job row (PC_NOOK_WACT_*), shared by the message builder and the actor so both agree on what choice k means; returns the count (2..3). */
enum { PC_NOOK_WACT_HAND = 0, PC_NOOK_WACT_LATER, PC_NOOK_WACT_QUIT, PC_NOOK_WACT_TAKE, PC_NOOK_WACT_REPORT };
int pc_nook_work_row(const PCWorkView* v, int* acts, int cap);
/* Work v2 rows (PC_NOOK_MSG_WK2 / _NEXT): the variant of each (PC_WK2_V_*), the item name bytes of the job (16, space padded, + mIN_ARTICLE_*), the urgent bonus
 * shown by PC_WK2_V_URGENT, the "has the item" bit of the Check job row (pc_work_v2_row). serial() counts the builds of PC_NOOK_MSG_WK2 / WK2_NEXT: an actor that
 * selects a row records it first and knows the row is ON SCREEN when it changed (the hand-over then plays while it is read). */
enum {
    PC_WK2_V_NONE = 0,
    PC_WK2_V_ASSIGN,      /* Nook: the new FRUIT / FETCH job (closing row) */
    PC_WK2_V_PARCEL,      /* Nook: the new DELIVER job: "here is <item> for <villager>" (Nook hands it over while it is read; continues) */
    PC_WK2_V_PARCEL_PAY,  /* Nook: after PARCEL: the pay comes when it is delivered (closing row) */
    PC_WK2_V_NOROOM,      /* Nook: the new DELIVER job but no free pocket: make room, then Check job (closing row) */
    PC_WK2_V_CHECK,       /* Nook: "did you bring <item> from <villager>?" + the pc_work_v2_row() choices */
    PC_WK2_V_RELAX,       /* Nook, "No.": take your time, it is not urgent (closing row) */
    PC_WK2_V_URGENT,      /* Nook, "No.": it IS urgent, the bonus is added (closing row) */
    PC_WK2_V_HURRY,       /* Nook, "No." again on an urgent job (closing row) */
    PC_WK2_V_TAKEBACK,    /* Nook, "Quit job" with the item in the pockets: he takes it back while it is read (continues) */
    PC_WK2_V_QUIT,        /* Nook: the job is quit (closing row) */
    PC_WK2_V_THANKS,      /* Nook: the item is handed in, the player hands it over while it is read (continues) */
    PC_WK2_V_PAID,        /* Nook: the reward was paid (closing row) */
    PC_WK2_V_VGIVE,       /* villager (FETCH): "Nook sent you for <item>? Here you go!" (the villager hands it over while it is read; continues) */
    PC_WK2_V_VGIVE_BYE,   /* villager: "tell him I said hello" (closing row) */
    PC_WK2_V_VRECV,       /* villager (DELIVER): "<item> from Tom Nook? For me?" (the player hands it over while it is read; continues) */
    PC_WK2_V_VRECV_TIP,   /* villager, old step (op 6): thank you + the job's old-style tip (closing row; Nook pays at REPORT) */
    PC_WK2_V_VDONE,       /* villager, auto-complete (op 10): the tip + Nook's pay, job done (closing row) */
    PC_WK2_V_COUNT
};
void pc_nook_house_set_wk2(int variant, int next_variant);
void pc_nook_house_set_work_item(const unsigned char* name16, int article);
void pc_nook_house_set_wk2_extra(unsigned bonus, int has_item);
unsigned pc_nook_wk2_serial(void);
/* The FIRST PAGE of Nook's talk (vanilla message 0x1092): adds "I'd like to work" (no job) / "Check my job" (active job) as a choice before the last one, decided from the
 * authoritative job state at the moment the message is loaded. Returns the new length (0 = not patched). pc_nook_first_page_work() = 1 when the message on screen carries the
 * extra choice (the actor then reads choice 3 as Work Mode). */
int pc_nook_msg_patch(int index, unsigned char* data, int len, int cap);
/* Dresser storage (worker 3): arm the next load of dresser message `index` to get "More storage" before its last choice; more_choice = that choice index
 * when the last load of `index` was patched, else -1 (the vanilla choice list: the caller keeps the vanilla mapping). */
void pc_dresser_msg_arm(int index);
int pc_dresser_msg_more_choice(int index);
int pc_nook_first_page_work(void);

/* MAP LINE (first purchase): right after a purchase that granted the town map, the actor arms the vanilla thanks message it just selected (its message index). When that message is loaded,
 * pc_nook_msg_patch() appends one extra page ("here is a map of the town") in place of its final MSGEND, ONCE: the arm is consumed by the first load of that index whether or not it was
 * patched (a message that ends with MSGCONTINUE, e.g. the ticket rows, is left byte-identical). Nothing else changes: no new action, no new message id. clear() drops a stale arm. */
void pc_nook_map_line_arm(int msg_index);
void pc_nook_map_line_clear(void);
int pc_nook_map_line_armed(void); /* the armed message index or -1 */
int pc_nook_map_line_patched_count(void);

/* Source audit helper: builds every PC message, returns 0 when all bytes are in the charset and every message ends with MSGEND / MSGCONTINUE. */
int pc_nook_house_selftest(void);

#ifdef __cplusplus
}
#endif

#endif
