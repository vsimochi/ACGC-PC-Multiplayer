#ifndef PC_DAY_ROLL_H
#define PC_DAY_ROLL_H

/* Day rollover on a long-running host (docs/multiplayer-guest-roadmap.md "Day rollover on a long-running host", TODO WS-01).
 *
 * PURE part (pc_day_roll.c: no game headers, natively tested by tools/net_spike/day_roll_selftest.c): the date rules (the vanilla
 * mTM_renewal_renew_time date check and the mAGrw_RenewalFgItem 6 AM growth rule), the safe-moment gate and the per-key attempt limiter.
 * HOST part (pc_day_roll_host.c: the game state, the same-position town reload, the play_init hook, the console status). */

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct PCDayRollDate {
    int year, month, day, hour, min, sec;
} PCDayRollDate;

/* Days since 1970-01-01 of a proleptic Gregorian date (month 1..12). */
long pc_dayroll_days_from_civil(int y, int m, int d);
/* Seconds since 1970-01-01 00:00:00 of a date-time (no time zone: the game's own local calendar). */
long long pc_dayroll_seconds(const PCDayRollDate* t);
/* The inverse of pc_dayroll_days_from_civil (time of day 0). */
void pc_dayroll_civil_from_days(long days, PCDayRollDate* out);

/* DAY roll due: the game date `now` (year 0 = the clock is not read yet -> 0) differs from `renew` (y/m/d) = the date of the host's last new-day init
 * (the boot, or the last roll). NOT Save renew_time: vanilla mTM_time() renews that every frame (weather / daily flags only). */
int pc_dayroll_day_due(const PCDayRollDate* renew, const PCDayRollDate* now);
/* The vanilla growth moment after `last` (Save all_grow_renew_time): the same day 06:00:00 when last.hour < 6, else the next day 06:00:00. */
void pc_dayroll_next_grow(const PCDayRollDate* last, PCDayRollDate* out);
/* GROW due (the reload's field build would run mAGrw growth): last is the clear code (first growth), the clock went back (now < last), or now >= the next 6 AM. */
int pc_dayroll_grow_due(const PCDayRollDate* last, int last_is_clear, const PCDayRollDate* now);

/* What a peer's location means for a due roll. */
enum {
    PC_DAYROLL_BLOCK_NONE = 0, /* the town / island field, a player house: never blocks */
    PC_DAYROLL_BLOCK_SOFT = 1, /* museum, post office, police, other interiors, an unknown scene: wait up to the soft limit */
    PC_DAYROLL_BLOCK_HARD = 2  /* a shop (stock / purchases) or a villager house (that villager may move out): wait */
};
/* kind = PCNetSceneKind (pc_net_game.h) of a READY peer's last scene; valid = 0 when no scene is known. */
int pc_dayroll_scene_block(int valid, int kind);

typedef struct PCDayRollGateIn {
    int      enabled;          /* AC_MP_DAY_ROLL != 0 */
    int      is_host;          /* role HOST */
    int      world_ready;      /* host world ready (+ observer ready on an observer host) */
    int      host_in_town;     /* the host's own scene is SCENE_FG in the town field */
    int      host_idle;        /* no wipe / fade, the host player idle (no menu / talk / demo) */
    int      peers_joining;    /* peers in the handshake or with a world snapshot under way */
    int      pending_interactions; /* two-phase pickup / drop / bury PENDING */
    int      hard_blockers;    /* READY peers in a HARD scene */
    int      soft_blockers;    /* READY peers in a SOFT scene */
    uint32_t deferred_ms;      /* how long the due roll has waited for peers so far */
    uint32_t soft_max_ms;      /* PC_DAYROLL_SOFT_MAX_MS (the soft wait limit) */
    int      ignore_soft;      /* operator `day roll now` */
} PCDayRollGateIn;

enum {
    PC_DAYROLL_GO = 0,
    PC_DAYROLL_WAIT_OFF,          /* switched off */
    PC_DAYROLL_WAIT_NOT_HOST,
    PC_DAYROLL_WAIT_NOT_READY,
    PC_DAYROLL_WAIT_HOST_AWAY,    /* the host is not in the town (a listen host in a building) */
    PC_DAYROLL_WAIT_HOST_BUSY,    /* transition / menu / talk */
    PC_DAYROLL_WAIT_PEER_JOINING,
    PC_DAYROLL_WAIT_PEER_PENDING,
    PC_DAYROLL_WAIT_PEER_HARD,
    PC_DAYROLL_WAIT_PEER_SOFT,
    PC_DAYROLL_GATE_NUM
};
int pc_dayroll_gate(const PCDayRollGateIn* in);
const char* pc_dayroll_gate_name(int gate);
/* 1 = the wait is caused by other players (deferred_ms runs), 0 = by the host itself / nothing. */
int pc_dayroll_gate_is_peer_wait(int gate);

/* One roll reload per key (DAY: today + stored date, GROW: the stored growth time); a reload that did not take is retried at most `max_tries` times, `gap_ms` apart. */
typedef struct PCDayRollAttempts {
    long long key;
    int       have;
    int       tries;
    uint32_t  last_ms;
} PCDayRollAttempts;
int  pc_dayroll_attempt_allowed(const PCDayRollAttempts* a, long long key, uint32_t now_ms, uint32_t gap_ms, int max_tries);
void pc_dayroll_attempt_note(PCDayRollAttempts* a, long long key, uint32_t now_ms);

/* Parses AC_TEST_DAY_ROLL_AT = "<sec>[,<hhmm>]" (test hook): 1 = ok (*sec >= 0, *hh 0..23, *mm 0..59; hhmm defaults to 0000). */
int pc_dayroll_parse_test_at(const char* s, int* sec, int* hh, int* mm);

#define PC_DAYROLL_SOFT_MAX_MS   (30u * 60u * 1000u) /* a SOFT blocker delays a due roll at most this long */
#define PC_DAYROLL_RETRY_GAP_MS  (10u * 60u * 1000u) /* a roll that did not take: next try of the same key */
#define PC_DAYROLL_MAX_TRIES     3
#define PC_DAYROLL_LOG_EVERY_MS  (5u * 60u * 1000u)  /* a deferred roll repeats its log line this often */

/* ---- HOST part (pc_day_roll_host.c; game build only) ---- */
/* Once per frame (pc_vi.c, next to pc_host_observer_poll): detection, gating, the reload, the post-roll verification. A no-op unless HOST. */
void pc_day_roll_poll(void);
/* m_play.c play_init, right before mFM_FieldInit: runs the boot init of a DAY roll when this scene load is the roll's reload. */
void pc_day_roll_play_init(void* play);
/* pc_vi.c save block: 1 = a finished roll asks for one authoritative save now; report = the result of that save. */
int  pc_day_roll_save_due(void);
void pc_day_roll_save_report(int ok);
/* Dedicated console `day [status|roll now|advance [hhmm]]`: writes the reply lines through `out`. */
void pc_day_roll_console(const char* args, void (*out)(const char* line));

#ifdef __cplusplus
}
#endif

#endif
