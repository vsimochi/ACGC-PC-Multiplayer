#ifndef PC_EVNPC_SHARED_H
#define PC_EVNPC_SHARED_H

/* SHARED EVENT NPCs (worker E; docs/multiplayer-guest-roadmap.md "Shared event NPCs").
 *
 * The visitors whose talk changes town-shared state (Crazy Redd's one-of-a-kind lineup, Saharah's town-wide price counter, Wendell's / Gracie's once-per-player
 * gifts of a visit, the Wisp's once-per-town spirit reward) ask the HOST with ONE one-phase transaction, TXN_COMMIT kind 21 EVNPC_CLAIM (protocol v8 extended in
 * place, no new message id). tag.aux_cond = the op (PC_EVC_OP_*); the other tag fields per op are documented at PC_NETGAME_TXN_KIND_EVNPC_CLAIM (pc_net_game.c).
 * The host announces the feature in town service 9 EVNPC_CAPS; a client of a host without it (0.1.1 / 0.2.x) never sends kind 21.
 *
 * This header has two halves: the PURE logic (pc_evnpc_shared.c: no game state, native selftest pc_evc_selftest()) and the client seam API implemented in
 * pc_net_game.c ("EVNPC SHARED" block). */

#include <stdint.h>

#define PC_EVC_OP_REDD_BUY       1 /* Crazy Redd: buy lineup piece `flags` (0..2) */
#define PC_EVC_OP_SAHARAH_TRADE  2 /* Saharah: a carpet + Bells for a random event carpet */
#define PC_EVC_OP_ARTIST_TRADE   3 /* Wendell: a fish for a random event wallpaper (once per player, 3 per visit) */
#define PC_EVC_OP_DESIGNER_GIFT  4 /* Gracie: the car-wash result 0 / 1 -> a cloth (once per player, 3 per visit) */
#define PC_EVC_OP_WISP_RETURN    5 /* the Wisp: all spirits (>= 5 points) for a reward (once per town) */
#define PC_EVC_OP_MAX            5
#define PC_EVC_OPS_ALL           0x3Eu /* bit op set for ops 1..5 */

/* reasons (PC_NETGAME_TXN_REASON_* 50..54, allocated to this block) */
#define PC_EVC_REASON_SOLD_OUT   50 /* the piece / the visit's quota is gone */
#define PC_EVC_REASON_CLAIMED    51 /* this player already got it / the Wisp already has its spirits */
#define PC_EVC_REASON_CHANGED    52 /* the client's view (piece, price) is stale */
#define PC_EVC_REASON_OFF        53 /* the host has the feature off (AC_MP_EVENT_NPC=0) */
#define PC_EVC_REASON_NO_SPIRITS 54 /* the pre-image does not hold 5 spirit points */

#define PC_EVC_WISP_WEEDS        0u
#define PC_EVC_WISP_ROOF         0x100u /* | palette */
#define PC_EVC_WISP_ITEM         2u
#define PC_EVC_ROOF_PAL_MAX      12u    /* mSP_ROOF_COLOR_NUM upper bound accepted by the shape check (the host checks the exact count) */

#define PC_EVC_SAHARAH_PRICE_START 3000u /* == eECPD_PRICE_START */
#define PC_EVC_SAHARAH_MULT_CAP    4     /* == eECPD_PRICE_MULT_CAP */
#define PC_EVC_REDD_MULT           4u    /* == aEBR2_PRICE_MULT */
#define PC_EVC_REDD_ITEMS          3
#define PC_EVC_ARTIST_MAX          3     /* gifts per visit (used 0, 1, 2 record; used 3 = done) */
#define PC_EVC_DESIGNER_MAX        3     /* == mEv_DESGINER_NUM */
#define PC_EVC_SPIRIT_POINTS       5     /* == mEv_GHOST_HITODAMA_NUM */

#define PC_EVC_CAPS_LEN            8u
#define PC_EVC_CAPS_VERSION        1u

#ifdef __cplusplus
extern "C" {
#endif

/* ---- pure logic (pc_evnpc_shared.c) ---- */
/* the tag shape of an op (1 = legal). dest: 0 NONE / 1 POCKET; empty = the game's EMPTY_NO. */
int pc_evc_shape_ok(int op, unsigned dest, unsigned slot, unsigned item, unsigned flags, unsigned aux_item, unsigned pocket_count);
/* the spirit points of a pocket image (vanilla aEGH_hitodama_num: SPIRIT(k) in NORMAL condition counts k + 1). cond_of(i) = 2-bit condition of slot i. */
int pc_evc_spirit_points(const uint16_t* pockets, uint32_t conds, int pocket_count, uint16_t spirit0);
/* removes every spirit (SPIRIT0..4, NORMAL) from the image (vanilla aEGH_delete_hitodama); returns the number of slots cleared. */
int pc_evc_strip_spirits(uint16_t* pockets, uint32_t* conds, int pocket_count, uint16_t spirit0, uint16_t empty);
/* Saharah's price for the town's counter `used` (3000 x 2^min(used, 4)). */
uint32_t pc_evc_saharah_price(int used);
/* 1 = the month/day `md` ((month << 8) | day) lies in [start, end] (a range across new year wraps). */
int pc_evc_md_in_range(unsigned md, unsigned start, unsigned end);
/* service 9 blob */
void pc_evc_caps_build(uint8_t* blob, int on, int wisp_returned, uint32_t ops);
int pc_evc_caps_valid(const uint8_t* blob);
/* native selftest: 0 = pass, else the number of failures */
int pc_evc_selftest(void);

/* ---- client seams (pc_net_game.c, "EVNPC SHARED" block) ---- */
#define PC_EVC_MODE_LOCAL   0 /* host / solo / feature off: the vanilla local path */
#define PC_EVC_MODE_CLAIM   1 /* a client of a host that announced kind 21 for this op: ask the host */
#define PC_EVC_MODE_REFUSE  2 /* a client of a host without the claim (0.2.x): refuse politely (a local sale could duplicate) */
int pc_evc_mode(int op);
/* begins one claim: 1 = sent (poll pc_net_game_ts_poll()), 0 = refused locally (pc_evc_last_reason()), -1 = busy (retry next frame) */
int pc_evc_begin(int op, int slot, int item, int flags, int aux_item);
/* after APPLIED: the item the host put into the claim's slot (EMPTY_NO = none) */
int pc_evc_item(void);
/* after REJECTED: the host reason (PC_EVC_REASON_* / a PC_NETGAME_TXN_REASON_*), 0 = local refusal / lost link */
int pc_evc_last_reason(void);
/* the dialogue's refusal row for a reason (a generated message id, pc_nook_house.c) */
int pc_evc_refusal_msg(int op, int reason);
/* CLIENT: 1 = the host's Wisp already got its spirits back (service 9) */
int pc_evc_host_wisp_returned(void);

/* ---- host hooks in the actor files (TARGET_PC) ---- */
uint16_t aEGH_pc_host_roll_item(void);       /* ac_ev_ghost_talk.c_inc: the vanilla uncollected-item roll (uses Now_Private's collect bits) */
int aBD_PC_SyncDisplayWithLineup(void);      /* ac_broker_design.c: takes pieces no longer in the lineup off Redd's display; returns how many */

#ifdef __cplusplus
}
#endif

#endif
