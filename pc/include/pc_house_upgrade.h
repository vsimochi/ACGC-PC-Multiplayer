#ifndef PC_HOUSE_UPGRADE_H
#define PC_HOUSE_UPGRADE_H

/* Multiplayer HOUSE UPGRADES through Tom Nook for resident CLIENTS (worker C; see docs/multiplayer-guest-roadmap.md "House upgrades via Nook (residents)").
 *
 * The house header (homes[h].size_info, ordered_outlook_pal, flags.has_basement), Save.num_statues and the resident's loan are written ONLY by the HOST:
 *   - a resident client's Nook dialogue asks for each write with ONE one-phase transaction, TXN_COMMIT kind 20 HOUSE_UPGRADE (protocol v8 extended in place, no new message id):
 *       tag.aux_cond = the op (PC_HU_OP_*), tag.aux_item = its argument (ORDER_SIZE: the roof palette 0..mHm_OUTLOOK_PAL_NUM-1, else 0), tag.flags = the STALE-HEADER GUARD
 *       (pc_house_upgrade_guard() of the client's copy of its house: the host refuses HOUSE_CHANGED when its own header differs), tag.dest NONE, tag.slot 0, tag.item 0;
 *   - the host re-checks the vanilla preconditions on ITS data (its header + its mirror of the loan), writes the order (ORDER_*: the header; ORDER_STATUE also the town-global
 *     num_statues, serialised because every order runs on the host thread), or the "built" acknowledgement (ACK_BUILT: renew / basement / statue flags + the mirror loan := the
 *     vanilla next debt, rev + 1), saves the town DURABLY before it answers APPLIED and refreshes the house canon (the header reaches every client through house sync);
 *   - the client never writes homes[] itself; on APPLIED of ACK_BUILT it sets its own loan to the same deterministic value (pc_house_upgrade_loan_after_ack) and moves its record
 *     base to the host's (epoch, rev, cdig).
 *   - CONSTRUCTION: a host that stays up builds an order whose date is not today (the per-house body of mHm_CheckRehouseOrder, mHm_PC_RehouseOne) as soon as the house is safe
 *     (nobody in a room of it, no owner commit in flight), saves durably and pushes the house; vanilla only did this at a game load.
 * Old hosts: kind 20 is unknown there (BAD_SHAPE + one record violation); a client sends it only when the host announced town service 6 (the closest capability proxy) and latches
 * "unsupported" for the session after one BAD_SHAPE answer, keeping the previous behaviour (plain greeting). */

#define PC_HU_OP_ORDER_SIZE     1 /* the next size + the roof colour (aNSC_check_roof_col_order / _order2) */
#define PC_HU_OP_ORDER_BASEMENT 2 /* the basement (choice 2 of the Medium offer, automatic for a Large house without one) */
#define PC_HU_OP_ORDER_STATUE   3 /* the statue (loan paid off, Upper house: REHOUSE talk start) */
#define PC_HU_OP_ACK_BUILT      4 /* Nook announces what was built (REHOUSE talk start): renew / basement / statue flags, the next loan */

#define PC_HU_REJECT_NOT_ELIGIBLE 45 /* the vanilla preconditions do not hold on the HOST (a loan left, an order already pending, ...) */
#define PC_HU_REJECT_BUSY         46 /* the host cannot take it now (an owner commit / record push of this player in flight) */
#define PC_HU_REJECT_CHANGED      47 /* the client's copy of the house header is stale (the guard differs from the host's header) */

#ifdef __cplusplus
extern "C" {
#endif

/* The stale-header guard of a house header (8 bits: size 0..2, has_basement 3, renew 4, basement_ordered 5, statue_ordered 6, next_size == size 7). */
unsigned pc_house_upgrade_guard_bits(int size, int next_size, int has_basement, int renew, int basement_ordered, int statue_ordered);
/* The loan the vanilla "built" acknowledgement sets (aNSC_set_talk_info_start_wait): basement 49,800, else by the NEW size (Medium 148,000, Large 398,000, Upper 798,000). */
unsigned pc_house_upgrade_loan_after_ack(int size, int basement_ordered);

/* CLIENT: 1 = this process may use kind 20 now (READY resident client with its own house, house sync on, the host announced service 6, not latched unsupported).
 * When 0 the dialogue keeps the old client behaviour (the plain greeting). */
int pc_house_upgrade_available(void);
/* CLIENT: begins one op. 1 = started (AWAIT_CLEAN, then the transaction), 0 = refused locally (pc_house_upgrade_last_reason() = 0), -1 = busy (retry next frame). */
int pc_house_upgrade_begin(int op, int arg);
/* PC_NETGAME_TS_OP_PENDING / _APPLIED / _REJECTED (consumed once). After REJECTED: pc_house_upgrade_last_reason() = the host reason (PC_HU_REJECT_* / a PC_NETGAME_TXN_REASON_*),
 * 0 = a local refusal or a lost link; pc_house_upgrade_last_sent() = 1 when the request had reached the wire (a reason 0 is then a LINK LOSS: the outcome is unknown). */
int pc_house_upgrade_poll(void);
int pc_house_upgrade_last_reason(void);
int pc_house_upgrade_last_sent(void);

#ifdef __cplusplus
}
#endif

#endif
