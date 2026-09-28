#ifndef LQRAND_DOMAINS_H
#define LQRAND_DOMAINS_H

#include "types.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * qrand_domains - independent, per-domain instances of the qrand() LCG (see libc64/qrand.h).
 *
 * WHY THIS EXISTS
 * ----------------
 * Every unrelated system in the original game (villagers, fish, bugs, weather, the Stalk Market,
 * shop restock, daily tree growth, event NPC selection, money rocks, snowmen, ...) draws from the
 * SAME global qrand() stream (qrand()/sqrand()/fqrand()/fqrand2() in libc64/qrand.c, seeded once
 * by init_rnd() in sys_math.c -> second_game.c). Consumption is frame/call-order dependent, so any
 * two runs that call the global stream in a different order diverge immediately. That is the root
 * cause of most multiplayer state divergence found by the RNG audit that motivated this file.
 *
 * This header gives a handful of KNOWN FUTURE world-authoritative domains their own, independent
 * LCG state, so that consuming one domain's stream can never perturb another's.
 *
 * THIS IS PURELY ADDITIVE
 * ------------------------
 * qrand()/sqrand()/fqrand()/fqrand2() and every existing RANDOM()/RANDOM_F()/RANDOM2()/RANDOM2_F()
 * call site are completely untouched and keep drawing from the single legacy global stream exactly
 * as before. No existing call site has been migrated to a domain stream by this change -- wiring a
 * specific domain (weather, Stalk Market, ...) to its real decision point is future work.
 *
 * WHAT THIS DOES NOT DO
 * ----------------------
 * This is domain SEPARATION, not multiplayer SYNCHRONIZATION. Seeding two independently-running
 * processes with the same domain seed does not make their subsequent draws mean the same thing to
 * each process -- it only stops, say, the weather stream and the fish-spawn stream from perturbing
 * each other's sequence *within one process*. The eventual authoritative server owns actual
 * world-simulation outcomes (weather, Stalk Market prices, villager decisions, fish/bug spawns,
 * ...) and will broadcast them; this infrastructure does not let two clients independently arrive
 * at the same authoritative decision, and is not intended to. See qrand_domain_make_seed() below.
 *
 * ALGORITHM
 * ---------
 * Identical LCG to qrand.c: x[n+1] = x[n] * 0x19660D + 0x3C6EF35F (mod 2^32) -- the same
 * multiplier/increment as the game's own rand implementation, just kept in a separate state word
 * per domain instead of the one shared `__qrand_idum`. Full period 2^32, same as qrand().
 *
 * An un-seeded domain defaults to idum = 1, exactly like qrand.c's own default -- so, exactly like
 * the legacy global stream before init_rnd() runs, an un-seeded domain is deterministic and *every*
 * un-seeded domain produces the identical sequence as every other un-seeded domain (and as the
 * legacy stream before its own sqrand()). Call sqrand_d() before relying on a domain's output.
 */

typedef enum QRandDomain {
  QRAND_DOMAIN_WEATHER = 0, /* future: daily weather pattern/forecast selection */
  QRAND_DOMAIN_STALK_MARKET, /* future: Stalk Market (turnip) buy/sell price generation */
  QRAND_DOMAIN_DAILY_SIM, /* future: daily-simulation rolls shared by tree growth, money rocks,
                              snowmen, shop restock and similar once-a-day world updates */
  QRAND_DOMAIN_VILLAGER, /* future: villager selection / arrival / departure decisions */
  QRAND_DOMAIN_FISH_SPAWN, /* future: fish spawn selection (src/actor/ac_set_ovl_gyoei.c) */
  QRAND_DOMAIN_BUG_SPAWN, /* future: bug/insect spawn selection (src/actor/ac_set_ovl_insect.c) */

  QRAND_DOMAIN_NUM, /* number of domains actually assigned above */

  /* Headroom for a handful of future domains (e.g. event NPC selection) without resizing the
   * backing state array. This is NOT a dynamic registration system -- QRAND_DOMAIN_CAPACITY is a
   * fixed, small upper bound; add a new named enumerator above (before QRAND_DOMAIN_NUM) when a
   * new domain is actually wired up. */
  QRAND_DOMAIN_CAPACITY = 16
} QRandDomain;

/* Seed one domain's independent stream. Domains not yet seeded behave as described above. */
void sqrand_d(QRandDomain domain, u32 seed);

/* Next raw 32-bit value from one domain's stream. Does not touch any other domain's state, and is
 * not touched by any other domain's or the legacy global stream's calls. */
u32 qrand_d(QRandDomain domain);

/* Next float in a domain's stream, range [0, 1). Mirrors fqrand(). */
f32 fqrand_d(QRandDomain domain);

/* Next float in a domain's stream, range [-0.5, 0.5). Mirrors fqrand2(). */
f32 fqrand2_d(QRandDomain domain);

/* Macro to generate a random float in a domain's stream, range [0, n). Mirrors RANDOM_F(n). */
#define RANDOM_D_F(domain, n) (fqrand_d(domain) * (f32)(n))

/* Macro to generate a random integer in a domain's stream, range [0, n). Mirrors RANDOM(n). */
#define RANDOM_D(domain, n) ((int)RANDOM_D_F(domain, n))

/* Macro to generate a random float in a domain's stream, range [-n/2, n/2). Mirrors
 * RANDOM_CENTER_F(n). */
#define RANDOM_D_CENTER_F(domain, n) (fqrand2_d(domain) * (f32)(n))

/*
 * SEED FORMULA (Phase 4 of the RNG-separation task)
 * ---------------------------------------------------
 * These three functions are pure -- none of them reads Save_t or any other game state. They exist
 * so a *future* caller can turn "this authoritative town's identity" + "this authoritative day" +
 * "which domain" into one deterministic 32-bit seed, without this file depending on m_common_data.h
 * or any other decomp header:
 *
 *   u32 town = qrand_domain_hash_town(Save_Get(land_info).id, Save_Get(land_info).name, LAND_NAME_SIZE);
 *     -- land_info is the town's own persisted identity: mLd_land_info_c { name[8], exists, id },
 *        include/m_land_h.h:15-19, held at Save_t.land_info, include/m_common_data.h:91
 *        ("town name & id"). land_info.id is already used elsewhere in the codebase as this save's
 *        town identity (e.g. src/game/m_card.c:3474 `mail->landid = Save_Get(land_info).id`).
 *
 *   u32 day = qrand_domain_day_epoch(Save_Get(all_grow_renew_time).year,
 *                                    Save_Get(all_grow_renew_time).month,
 *                                    Save_Get(all_grow_renew_time).day);
 *     -- all_grow_renew_time is an existing lbRTC_time_c (include/lb_rtc.h) persisted at
 *        Save_t.all_grow_renew_time, include/m_common_data.h:106, and is already updated once per
 *        real day by the daily fg-item renewal pass (compared via its `.day` field in
 *        src/game/m_all_grow_ovl.c:1701,1956). It is reused here only as a convenient "which day is
 *        it, per the save" anchor that already exists -- no new Save_t field is added.
 *
 *   u32 seed = qrand_domain_make_seed(town, day, QRAND_DOMAIN_WEATHER);
 *   sqrand_d(QRAND_DOMAIN_WEATHER, seed);
 *
 * No such call exists anywhere in the codebase yet -- deciding exactly when/where each domain gets
 * (re)seeded is future work for that domain's own migration, not part of this change. As noted
 * above, this formula's purpose today is only to keep unrelated local RNG consumers from
 * perturbing each other; it is NOT a synchronization mechanism between processes.
 */

/* Mixes a town's persisted identity (land_info.id/name) into one 32-bit value. Plain FNV-1a --
 * not cryptographic, just needs to mix a short byte string well. land_name may be NULL (name_len
 * is then ignored) if only the numeric id is available. */
u32 qrand_domain_hash_town(u16 land_id, const u8* land_name, int name_len);

/* Turns a (year, month, day) reading into a value that changes once per calendar day and is
 * monotonic across days. Deliberately NOT real calendar math (every month is treated as a fixed
 * 31-day block) -- a true Julian/ordinal day count is unnecessary for this purpose. */
u32 qrand_domain_day_epoch(u16 year, u8 month, u8 day);

/* Final mix of (town identity, day epoch, domain) into one deterministic 32-bit seed. Uses one
 * step of the same LCG as a cheap avalanche, not as a stream -- the returned value is meant to be
 * passed straight to sqrand_d(), not consumed further here. */
u32 qrand_domain_make_seed(u32 town_hash, u32 day_epoch, QRandDomain domain);

#ifdef __cplusplus
}
#endif

#endif
