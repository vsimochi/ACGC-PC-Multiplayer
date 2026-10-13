#ifndef PC_SHOP_UPGRADE_H
#define PC_SHOP_UPGRADE_H

/* Shop upgrade: announced 5-minute closure (multiplayer only; see docs/multiplayer-guest-roadmap.md "Shop upgrade: announced 5-minute closure").
 *
 * In a multiplayer session whose HOST (listen or dedicated) has the feature on (env AC_MP_SHOP_UPGRADE, default ON; "0" / "off" = the vanilla 2-day schedule as before),
 * the vanilla renewal schedule (aSL_JudgeRenewShop: mEv_SAVED_RENEWSHOP + renewal_time two days ahead + the closure from renewal_time - 1 day) is REPLACED by:
 *   IDLE -> PENDING     the host sees mSP_GetRealShopLevel() > mSP_GetShopLevel() (a shop block exists, the shop is plainly OPEN, no restock running);
 *   PENDING -> CLOSED   the NEXT player (resident, guest or the host's own player) who talks to Tom Nook hears the announcement and the host starts the closure
 *                       (a client asks with PC_NETGAME_MSG_SHOP_UPGRADE_REQ; the first request wins, the closure is started exactly once);
 *   CLOSED -> RENEWING  after PC_SHOP_UPGRADE_SECONDS of host time (wall-clock deadline persisted in shop_upgrade.ini next to shop_restock.ini);
 *   RENEWING -> IDLE    the host runs the vanilla renewal body (structure tile SHOP0 + level, ground clean into the police box, mSP_RenewShopLevel, a new lineup) and
 *                       publishes the result (the shop acre, the shop / police mirrors), then the shop reopens.
 * The state travels to the clients as town service 6 (SHOP_UPGRADE, 16 bytes); an old client ignores the unknown service.
 * Single player / a client of an old host / the feature switched off: every function below is inert (0) and the vanilla code runs unchanged. */

#define PC_SHOP_UPGRADE_SECONDS 300 /* the closure in seconds of host time (AC_TEST_SHOP_UPGRADE_SECONDS overrides it in a PC_TEST_HOOKS build with AC_TEST_HOOKS=1) */

enum {
    PC_SHOP_UPGRADE_IDLE = 0,
    PC_SHOP_UPGRADE_PENDING = 1,  /* the town qualifies: Nook announces at the next talk */
    PC_SHOP_UPGRADE_CLOSED = 2,   /* announced: the shop is closed, the countdown runs */
    PC_SHOP_UPGRADE_RENEWING = 3  /* the countdown is over: the host performs the renewal (still closed) */
};

#ifdef __cplusplus
extern "C" {
#endif

/* 1 while the shop is closed for the upgrade (HOST: its own state; CLIENT: the host's mirrored service 6). Read by mSP_ShopOpen() / mSP_InRenewal(), the shop doors and the
 * shop interior (everyone inside is shown out). */
int pc_shop_upgrade_closed(void);
/* HOST only: 1 = this host replaces the vanilla 2-day schedule (role HOST + the feature on). aSL_JudgeRenewShop() then never schedules a vanilla renewal. */
int pc_shop_upgrade_host_replaces_vanilla(void);
/* HOST: 1 = a due vanilla renewal may be performed now (pc_host_renewshop_poll's town reload): the feature is off, or the announced upgrade is RENEWING (it armed the vanilla state
 * itself). 0 = a vanilla renewal state found while the feature is on is NOT performed (the upgrade takes it over: Nook announces first). */
int pc_shop_upgrade_vanilla_renewal_allowed(void);
/* 1 = Nook should give the upgrade announcement to the local player now (state PENDING, host: its own state, client: READY + the mirror). */
int pc_shop_upgrade_announce_due(void);
/* Starts the closure after the announcement. HOST role: starts at once, returns 3 (started or already closed). CLIENT: sends the request, returns 1 (then poll
 * pc_shop_upgrade_poll()). 0 = refused locally (not pending / not connected). */
int pc_shop_upgrade_begin(void);
/* After pc_shop_upgrade_begin() == 1: 0 = waiting, 1 = the shop is closing (this player's request or a simultaneous one: the closure of the announced upgrade runs),
 * -1 = failed (timeout / link lost / the host refused): the dialogue falls back. */
int pc_shop_upgrade_poll(void);
/* The closure length in whole minutes (rounded up, >= 1), and the minutes left of the running closure (rounded up; 0 when not closed). */
int pc_shop_upgrade_minutes_total(void);
int pc_shop_upgrade_minutes_left(void);
/* On-screen notice (closure start / reopening): 1 = draw `line` (filled), 0 = nothing to show. */
int pc_shop_upgrade_notice(char* line, int cap);
/* Dedicated console `shop-upgrade`: a one-line state description. */
void pc_shop_upgrade_describe(char* out, int cap);
/* Dedicated console `shop-upgrade start confirm`: the operator starts the closure of a PENDING upgrade (as if a player had heard the announcement). 1 = started; 0 = refused (`out` = why). */
int pc_shop_upgrade_console_start(char* out, int cap);

/* ac_shop_level.c (TARGET_PC): the vanilla renewal body without its actor, on the save's town field (the host is NOT in the town scene: no live structure actor can restore an
 * old tile). 1 = renewed (structure tile, ground clean, mSP_RenewShopLevel, event off, new exchange day + lineup). */
int aSL_PC_RenewShopDirect(void);
/* ac_shop_level.c (TARGET_PC): the shop acre (0-based acre index of the town field), the structure tile index (0..255) and the ground-clean footprint tiles of shop level
 * `level` around it. Returns the footprint count (0 = no shop block / no structure tile found). The structure tile is found as SHOP0..SHOP3 or a live DUMMY_SHOP0..3. */
int aSL_PC_ShopFootprint(int level, int* acre, int* struct_tile, int* tiles, int cap);

#ifdef __cplusplus
}
#endif

#endif
