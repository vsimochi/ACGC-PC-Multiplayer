/* pc_able_net.h - Able Sisters (Mable & Sable) multiplayer seams (worker B). Implemented in pc_net_game.c (ABLE TRADE JOURNAL / VILLAGER OUTFIT blocks).
 *
 * The shop's designs (Save_t.needlework.original_design[0..7]: 0..3 mannequins, 4..7 umbrellas) are host-authoritative town pages (PAGE msg 71, kind DESIGN).
 * A trade at Mable is journaled on a CLIENT so a refused / superseded page write can never cost the player the design it handed over:
 *   pc_able_trade_gate()        1 = a trade may start now (solo / host: always). A client refuses while not READY, while the previous trade is open, before every
 *                               design page was adopted or while a design write is in flight (the caller shows Mable's vanilla "Oh, I see." and changes nothing).
 *   pc_able_trade_begin/_end    around the vanilla aNI_Exchange* / aNI_Copy*Data call of a TRADE (never the live-refresh / ct copies): before / after images.
 *   pc_able_buffer_synced(d)    the shop's double buffer of design d equals the Save: the player SEES the adopted revision (the trade write is built on it).
 * Single player and the host's own player: every call is a no-op (vanilla). */
#ifndef PC_ABLE_NET_H
#define PC_ABLE_NET_H

#ifdef __cplusplus
extern "C" {
#endif

#define PC_ABLE_TRADE_EXCHANGE 1 /* the player's my_org[k] <-> the shop design (aNI_ExchangeCloth / aNI_ExchangeUmb) */
#define PC_ABLE_TRADE_DISPLAY  2 /* the player's my_org[k] is copied onto the shop slot (aNI_CopyClothData / aNI_CopyUmbData from a trade) */

int  pc_able_trade_gate(void);
void pc_able_trade_begin(int kind, int design, int org);
void pc_able_trade_end(void);
void pc_able_buffer_synced(int design);
/* TEST-ONLY (PC_NET_TEST_HOOKS builds + AC_TEST_HOOKS=1, AC_TEST_ABLE_TRADE): 1 = the shop actor should request this trade now (always 0 otherwise). */
int  pc_able_test_trade_poll(int* kind, int* design, int* org);

#ifdef __cplusplus
}
#endif
#endif
