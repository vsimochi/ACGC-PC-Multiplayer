/* pc_day_roll_host.c - day rollover on a long-running multiplayer HOST (dedicated observer or listen host). See pc_day_roll.h and
 * docs/multiplayer-guest-roadmap.md "Day rollover on a long-running host".
 *
 * Vanilla runs the new-day work at boot (mSDI_StartInitAfter: weather, move-in / move-out, the day's mail, shop line-up, events, ...) and the overnight
 * growth at a SCENE_FG field build after 6 AM; a host that never reloads the town never got either (the weather and the daily event flags DO renew while
 * running: mTM_time -> mTM_renewal_renew_time every frame, consumed by the weather actor and mEv_run). Here the host detects the new day against the date of
 * its last new-day init (the boot, or the last roll; process memory: every process start IS a vanilla boot init) and the vanilla 6 AM rule on
 * Save all_grow_renew_time, waits for a safe moment, and does ONE same-position SCENE_FG
 * reload (pc_town_same_position_reload, the shop-renewal door); a DAY roll runs the boot init inside that reload's play_init (pc_day_roll_play_init), the
 * field build that follows grows the field. Clients get the result through the existing host -> client channels. Single player and clients: never. */
#include "pc_day_roll.h"

#include "m_common_data.h"
#include "m_start_data_init.h"
#include "m_play.h"
#include "m_event.h"
#include "m_time.h"
#include "m_npc.h"
#include "m_demo.h"
#include "m_submenu.h"
#include "m_field_info.h"
#include "m_player_lib.h"
#include "lb_rtc.h"
#include "game.h"
#include "pc_net_game.h"
#include "pc_dedicated.h"
#include "pc_host_observer.h"
#include "pc_server.h"
#include "pc_test_hooks.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern int pc_town_same_position_reload(const char* why); /* pc_m_card.c */

_Static_assert(PC_NETSCENE_KIND_FIELD == 1 && PC_NETSCENE_KIND_SHOP == 2 && PC_NETSCENE_KIND_PLAYER_HOUSE == 6 && PC_NETSCENE_KIND_VILLAGER_HOUSE == 7,
               "pc_dayroll_scene_block() maps PCNetSceneKind by value");

#define PC_DAYROLL_KIND_DAY  1
#define PC_DAYROLL_KIND_GROW 2
#define PC_DAYROLL_ARM_TIMEOUT_MS 30000u /* the reload's play_init must come within this; else the arming is dropped */

typedef struct PCDayRollState {
    int      env_read, enabled;
    int      armed;            /* PC_DAYROLL_KIND_* of the reload just issued (waits for its play_init) */
    uint32_t armed_ms;
    int      init_ran;         /* the DAY init ran inside the reload */
    int      init_skipped;     /* the DAY init was refused inside the reload (a peer moved into a blocker during the fade) */
    int      verify;           /* the reload's scene is loading: verify once settled */
    int      save_due;
    int      ignore_soft;      /* operator `day roll now` (for the current due roll) */
    PCDayRollAttempts att_day, att_grow;
    int      due_kinds;        /* last poll's due kinds */
    uint32_t due_since_ms;
    int      last_gate;
    uint32_t last_log_ms;
    int      init_date_valid;  /* the date of the last new-day init in this process (the boot, then every roll) */
    PCDayRollDate init_date;
    /* before / after of the last roll */
    lbRTC_time_c roll_clock;
    int      weather_before, villagers_before;
    char     last_result[200];
    int      rolls;
    /* test hook */
    int      test_state;       /* 0 env not read, 1 off, 2 armed (counting), 3 done */
    int      test_sec, test_hh, test_mm;
    uint32_t test_start_ms;
} PCDayRollState;

static PCDayRollState s_dr; /* zero = env not read, nothing armed */

static void pc_dr_log(const char* fmt, ...)
#if defined(__GNUC__)
    __attribute__((format(printf, 1, 2)))
#endif
    ;
static void pc_dr_log(const char* fmt, ...) {
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    printf("[SERVER] day roll: %s\n", buf);
    fflush(stdout);
    if (pc_server_active()) {
        pc_server_log("day roll: %s", buf);
    }
}

static int pc_dr_enabled(void) {
    if (!s_dr.env_read) {
        const char* e = getenv("AC_MP_DAY_ROLL");
        s_dr.env_read = 1;
        s_dr.enabled = !(e != NULL && e[0] == '0' && e[1] == '\0');
    }
    return s_dr.enabled;
}

static void pc_dr_date_from_rtc(const lbRTC_time_c* t, PCDayRollDate* d) {
    d->year = t->year;
    d->month = t->month;
    d->day = t->day;
    d->hour = t->hour;
    d->min = t->min;
    d->sec = t->sec;
}

/* renew = the date of the last new-day init (the boot / the last roll); before it is known: today (nothing due). */
static void pc_dr_dates(PCDayRollDate* now, PCDayRollDate* renew, PCDayRollDate* grow, int* grow_clear) {
    pc_dr_date_from_rtc(Common_GetPointer(time.rtc_time), now);
    memset(renew, 0, sizeof(*renew));
    renew->year = s_dr.init_date_valid ? s_dr.init_date.year : now->year;
    renew->month = s_dr.init_date_valid ? s_dr.init_date.month : now->month;
    renew->day = s_dr.init_date_valid ? s_dr.init_date.day : now->day;
    pc_dr_date_from_rtc(Save_GetPointer(all_grow_renew_time), grow);
    *grow_clear = mTM_AreTimesEqual(&mTM_rtcTime_clear_code, Save_GetPointer(all_grow_renew_time)) == TRUE;
}

static void pc_dr_note_init_date(void) {
    pc_dr_date_from_rtc(Common_GetPointer(time.rtc_time), &s_dr.init_date);
    s_dr.init_date.hour = s_dr.init_date.min = s_dr.init_date.sec = 0;
    s_dr.init_date_valid = s_dr.init_date.year != 0;
}

static int pc_dr_due_kinds(long long* day_key, long long* grow_key) {
    PCDayRollDate now, renew, grow;
    int clear, k = 0;
    pc_dr_dates(&now, &renew, &grow, &clear);
    *day_key = (long long)pc_dayroll_days_from_civil(now.year, now.month, now.day) * 1000003LL + (long long)pc_dayroll_days_from_civil(renew.year, renew.month, renew.day);
    *grow_key = clear ? -1LL : pc_dayroll_seconds(&grow);
    if (pc_dayroll_day_due(&renew, &now)) {
        k |= PC_DAYROLL_KIND_DAY;
    }
    if (pc_dayroll_grow_due(&grow, clear, &now)) {
        k |= PC_DAYROLL_KIND_GROW;
    }
    return k;
}

static int pc_dr_villagers(void) {
    int i, n = 0;
    for (i = 0; i < ANIMAL_NUM_MAX; i++) {
        if (mNpc_CheckFreeAnimalPersonalID(&Save_Get(animals[i]).id) == FALSE) {
            n++;
        }
    }
    return n;
}

static const char* pc_dr_kind_name(int k) {
    return k == (PC_DAYROLL_KIND_DAY | PC_DAYROLL_KIND_GROW) ? "new day + growth" : k == PC_DAYROLL_KIND_DAY ? "new day" : k == PC_DAYROLL_KIND_GROW ? "growth (6 AM)" : "none";
}

/* The peer part of the gate (also re-checked inside the reload's play_init). who = the first blocker, for the log. */
static void pc_dr_peer_gate(PCDayRollGateIn* in, char* who, size_t cap) {
    int slot, span = pc_net_game_dedicated_peer_slots();
    pc_net_game_host_day_roll_busy(&in->peers_joining, &in->pending_interactions);
    in->hard_blockers = 0;
    in->soft_blockers = 0;
    if (who != NULL && cap > 0) {
        who[0] = '\0';
    }
    for (slot = 0; slot < span; slot++) {
        PCNetGameDedicatedPeerInfo pi;
        PCNetPlayerScene sc;
        int have, b;
        if (slot == PC_NETGAME_HOST_PLAYER_ID || !pc_net_game_dedicated_peer_info(slot, &pi) || pi.link != PC_NETGAME_LINK_READY) {
            continue;
        }
        have = pc_net_game_get_peer_scene((PCNetPlayerId)slot, &sc) && sc.valid;
        b = pc_dayroll_scene_block(have, have ? (int)sc.kind : 0);
        if (b == PC_DAYROLL_BLOCK_HARD) {
            in->hard_blockers++;
        } else if (b == PC_DAYROLL_BLOCK_SOFT) {
            in->soft_blockers++;
        }
        if (b != PC_DAYROLL_BLOCK_NONE && who != NULL && who[0] == '\0') {
            snprintf(who, cap, "%s (peer %d) in scene %d", pi.name, slot, have ? (int)sc.scene_id : -1);
        }
    }
}

static void pc_dr_gate_in(PCDayRollGateIn* in, char* who, size_t cap) {
    GAME_PLAY* play = NULL;
    memset(in, 0, sizeof(*in));
    in->enabled = pc_dr_enabled();
    in->is_host = pc_net_game_role() == PC_NETGAME_ROLE_HOST;
    in->world_ready = pc_net_game_dedicated_world_ready() && (!g_pc_host_observer || pc_host_observer_ready());
    in->soft_max_ms = PC_DAYROLL_SOFT_MAX_MS;
    in->ignore_soft = s_dr.ignore_soft;
    if (gamePT != NULL && gamePT->exec == play_main) {
        play = (GAME_PLAY*)gamePT;
        in->host_in_town = play->scene_id == SCENE_FG && Save_Get(scene_no) == SCENE_FG && mFI_GET_TYPE(mFI_GetFieldId()) == mFI_FIELD_FG;
        if (in->host_in_town && play->fb_wipe_mode == WIPE_MODE_NONE && play->fb_fade_type == FADE_TYPE_NONE && Common_Get(player_actor_exists)) {
            PLAYER_ACTOR* pl = GET_PLAYER_ACTOR_NOW();
            in->host_idle = pl != NULL && play->submenu.process_status == mSM_PROCESS_WAIT && mDemo_CheckDemo() == FALSE &&
                            (pc_host_observer_active() || mPlib_able_submenu_type1((GAME*)play));
        }
    }
    pc_dr_peer_gate(in, who, cap);
}

#ifdef PC_NET_TEST_HOOKS
/* TEST-ONLY: move the HOST game clock to tomorrow hh:mm:05 (lbRTC_SetTime: a real clock change, broadcast to clients as a CLOCK_SYNC discontinuity). */
static void pc_dr_advance_clock(int hh, int mm, const char* why) {
    lbRTC_time_c t = Common_Get(time.rtc_time);
    PCDayRollDate d;
    pc_dayroll_civil_from_days(pc_dayroll_days_from_civil(t.year, t.month, t.day) + 1, &d);
    t.year = (lbRTC_year_t)d.year;
    t.month = (lbRTC_month_t)d.month;
    t.day = (lbRTC_day_t)d.day;
    t.hour = (lbRTC_hour_t)hh;
    t.min = (lbRTC_min_t)mm;
    t.sec = 5;
    t.weekday = lbRTC_Week(t.year, t.month, t.day);
    lbRTC_SetTime(&t);
    lbRTC_GetTime(Common_GetPointer(time.rtc_time));
    pc_dr_log("[TEST-ONLY] %s: the host game clock was moved to %04d-%02d-%02d %02d:%02d:%02d (NOT active in normal play)", why, (int)t.year, (int)t.month, (int)t.day,
              (int)t.hour, (int)t.min, (int)t.sec);
}

static void pc_dr_test_hook(void) {
    if (s_dr.test_state == 0) {
        const char* e = pc_test_hook_getenv("AC_TEST_DAY_ROLL_AT");
        s_dr.test_state = 1;
        if (e != NULL && e[0] != '\0') {
            if (pc_dayroll_parse_test_at(e, &s_dr.test_sec, &s_dr.test_hh, &s_dr.test_mm)) {
                s_dr.test_state = 2;
            } else {
                pc_dr_log("[TEST-ONLY] AC_TEST_DAY_ROLL_AT='%s' is not <sec>[,<hhmm>]: ignored", e);
            }
        }
    }
    if (s_dr.test_state != 2 || !pc_net_game_dedicated_world_ready() || (g_pc_host_observer && !pc_host_observer_ready())) {
        return;
    }
    if (pc_net_game_host_ready_peer_count() == 0 && pc_test_hook_getenv("AC_TEST_DAY_ROLL_SOLO") == NULL) {
        return; /* armed once a READY peer is connected (it then sees the roll), unless SOLO */
    }
    if (s_dr.test_start_ms == 0) {
        s_dr.test_start_ms = pc_net_game_now_ms() | 1u;
        pc_dr_log("[TEST-ONLY] AC_TEST_DAY_ROLL_AT: the clock moves to tomorrow %02d:%02d in %d s", s_dr.test_hh, s_dr.test_mm, s_dr.test_sec);
    }
    if ((uint32_t)(pc_net_game_now_ms() - s_dr.test_start_ms) >= (uint32_t)s_dr.test_sec * 1000u) {
        s_dr.test_state = 3;
        pc_dr_advance_clock(s_dr.test_hh, s_dr.test_mm, "AC_TEST_DAY_ROLL_AT");
    }
}
#endif

static void pc_dr_verify(void) {
    long long dk, gk;
    const int left = pc_dr_due_kinds(&dk, &gk);
    const PCDayRollDate r = s_dr.init_date;
    const lbRTC_time_c g = Save_Get(all_grow_renew_time);
    snprintf(s_dr.last_result, sizeof(s_dr.last_result), "%04d-%02d-%02d %02d:%02d %s: %s; weather %d -> %d, villagers %d -> %d%s",
             (int)s_dr.roll_clock.year, (int)s_dr.roll_clock.month, (int)s_dr.roll_clock.day, (int)s_dr.roll_clock.hour, (int)s_dr.roll_clock.min,
             pc_dr_kind_name(s_dr.armed), left == 0 ? "done" : "NOT complete", s_dr.weather_before, (int)Common_Get(weather), s_dr.villagers_before, pc_dr_villagers(),
             s_dr.init_skipped ? " (the new-day init was skipped inside the reload: a player moved into a blocking building)" : "");
    pc_dr_log("done (%s): new-day init date %04d-%02d-%02d, all_grow_renew_time %04d-%02d-%02d %02d:%02d, still due: %s; weather %d -> %d, villagers %d -> %d%s",
              pc_dr_kind_name(s_dr.armed), (int)r.year, (int)r.month, (int)r.day, (int)g.year, (int)g.month, (int)g.day, (int)g.hour, (int)g.min, pc_dr_kind_name(left),
              s_dr.weather_before, (int)Common_Get(weather), s_dr.villagers_before, pc_dr_villagers(), s_dr.init_skipped ? " (new-day init skipped: retried)" : "");
    if (s_dr.init_skipped && (s_dr.armed & PC_DAYROLL_KIND_DAY)) {
        /* a player stepped into a shop / villager house during the fade (or the init could not run): that reload does not count as a try, and the next
         * one may come 1 min after this one (not the full retry gap) once the gate is clear again */
        if (s_dr.att_day.tries > 0) {
            s_dr.att_day.tries--;
        }
        s_dr.att_day.last_ms = pc_net_game_now_ms() - PC_DAYROLL_RETRY_GAP_MS + 60000u;
    }
    s_dr.rolls += s_dr.init_skipped ? 0 : 1;
    s_dr.save_due = 1;
    s_dr.armed = 0;
    s_dr.init_ran = 0;
    s_dr.init_skipped = 0;
    s_dr.ignore_soft = 0;
}

void pc_day_roll_poll(void) {
    PCDayRollGateIn in;
    char who[96];
    long long day_key, grow_key;
    int kinds, gate;
    uint32_t now;

    if (pc_net_game_role() != PC_NETGAME_ROLE_HOST) {
        return;
    }
#ifdef PC_NET_TEST_HOOKS
    pc_dr_test_hook();
#endif
    if (!pc_dr_enabled()) {
        return;
    }
    now = pc_net_game_now_ms();
    if (s_dr.armed && !s_dr.verify) {
        if ((uint32_t)(now - s_dr.armed_ms) >= PC_DAYROLL_ARM_TIMEOUT_MS) {
            pc_dr_log("the reload for the %s roll never reached its scene load: dropped (retried later)", pc_dr_kind_name(s_dr.armed));
            s_dr.armed = 0;
        }
        return;
    }
    pc_dr_gate_in(&in, who, sizeof(who));
    if (s_dr.verify) {
        if (in.world_ready && in.host_in_town && in.host_idle) {
            s_dr.verify = 0;
            pc_dr_verify();
        } else if ((uint32_t)(now - s_dr.armed_ms) >= 120000u) {
            s_dr.verify = 0;
            pc_dr_verify(); /* the scene never settled idle (a listen host walked into a menu): report what is there */
        }
        return;
    }
    if (!in.world_ready || gamePT == NULL || gamePT->exec != play_main) {
        return;
    }
    if (!s_dr.init_date_valid) {
        pc_dr_note_init_date(); /* the boot's own new-day init (the vanilla boot / observer init) was for today */
        if (s_dr.init_date_valid) {
            pc_dr_log("ON: the boot's new-day init is for %04d-%02d-%02d; the next roll comes with the next game date (and growth after 6 AM)", s_dr.init_date.year,
                      s_dr.init_date.month, s_dr.init_date.day);
        }
        return;
    }
    kinds = pc_dr_due_kinds(&day_key, &grow_key);
    if ((kinds & PC_DAYROLL_KIND_DAY) && !pc_dayroll_attempt_allowed(&s_dr.att_day, day_key, now, PC_DAYROLL_RETRY_GAP_MS, PC_DAYROLL_MAX_TRIES)) {
        kinds &= ~PC_DAYROLL_KIND_DAY;
    }
    if ((kinds & PC_DAYROLL_KIND_GROW) && !pc_dayroll_attempt_allowed(&s_dr.att_grow, grow_key, now, PC_DAYROLL_RETRY_GAP_MS, PC_DAYROLL_MAX_TRIES)) {
        kinds &= ~PC_DAYROLL_KIND_GROW;
    }
    if (kinds == 0) {
        if (s_dr.due_kinds != 0) {
            const lbRTC_time_c t = Common_Get(time.rtc_time);
            pc_dr_log("no longer due (%s) without a roll: game %04d-%02d-%02d %02d:%02d, last new-day init %04d-%02d-%02d (a clock change, or every try was used)",
                      pc_dr_kind_name(s_dr.due_kinds), (int)t.year, (int)t.month, (int)t.day, (int)t.hour, (int)t.min, s_dr.init_date.year, s_dr.init_date.month,
                      s_dr.init_date.day);
            s_dr.due_kinds = 0;
        }
        s_dr.ignore_soft = 0;
        return;
    }
    if (s_dr.due_kinds == 0) {
        const lbRTC_time_c t = Common_Get(time.rtc_time);
        s_dr.due_since_ms = now;
        s_dr.last_gate = -1;
        pc_dr_log("due: %s at %04d-%02d-%02d %02d:%02d:%02d (last new-day init %04d-%02d-%02d)", pc_dr_kind_name(kinds), (int)t.year, (int)t.month, (int)t.day,
                  (int)t.hour, (int)t.min, (int)t.sec, s_dr.init_date.year, s_dr.init_date.month, s_dr.init_date.day);
    }
    else if (kinds != s_dr.due_kinds) {
        pc_dr_log("due changed: %s -> %s", pc_dr_kind_name(s_dr.due_kinds), pc_dr_kind_name(kinds));
    }
    s_dr.due_kinds = kinds;
    in.deferred_ms = now - s_dr.due_since_ms;
    gate = pc_dayroll_gate(&in);
    if (gate != PC_DAYROLL_GO) {
        if (gate != s_dr.last_gate || (uint32_t)(now - s_dr.last_log_ms) >= PC_DAYROLL_LOG_EVERY_MS) {
            if (gate != PC_DAYROLL_WAIT_HOST_BUSY || s_dr.last_gate != PC_DAYROLL_WAIT_HOST_BUSY) {
                const lbRTC_time_c t = Common_Get(time.rtc_time);
                const lbRTC_time_c g = Save_Get(all_grow_renew_time);
                pc_dr_log("deferred (%s): %s%s%s, waiting %u s (game %04d-%02d-%02d %02d:%02d, last new-day init %04d-%02d-%02d, last growth %04d-%02d-%02d %02d:%02d)", pc_dr_kind_name(kinds),
                          pc_dayroll_gate_name(gate), who[0] ? ": " : "", who, (unsigned)(in.deferred_ms / 1000u), (int)t.year, (int)t.month, (int)t.day, (int)t.hour, (int)t.min,
                          s_dr.init_date.year, s_dr.init_date.month, s_dr.init_date.day, (int)g.year, (int)g.month, (int)g.day, (int)g.hour,
                          (int)g.min);
            }
            s_dr.last_gate = gate;
            s_dr.last_log_ms = now;
        }
        return;
    }
    {
        int res;
        s_dr.roll_clock = Common_Get(time.rtc_time);
        s_dr.weather_before = Common_Get(weather);
        s_dr.villagers_before = pc_dr_villagers();
        s_dr.init_ran = 0;
        s_dr.init_skipped = 0;
        res = pc_town_same_position_reload("day roll");
        if (res != 1) {
            return; /* not this frame */
        }
        if (kinds & PC_DAYROLL_KIND_DAY) {
            pc_dayroll_attempt_note(&s_dr.att_day, day_key, now);
        }
        if (kinds & PC_DAYROLL_KIND_GROW) {
            pc_dayroll_attempt_note(&s_dr.att_grow, grow_key, now);
        }
        s_dr.armed = kinds;
        s_dr.armed_ms = now;
        pc_dr_log("starting (%s): same-position town reload%s%s", pc_dr_kind_name(kinds), in.soft_blockers > 0 ? " (players in buildings waited for long enough: " : "",
                  in.soft_blockers > 0 ? "rolling anyway)" : "");
        s_dr.due_kinds = 0;
    }
}

void pc_day_roll_play_init(void* p) {
    GAME_PLAY* play = (GAME_PLAY*)p;
    PCDayRollGateIn in;
    char who[96];
    if (!s_dr.armed || pc_net_game_role() != PC_NETGAME_ROLE_HOST) {
        return;
    }
    s_dr.verify = 1;
    if (!(s_dr.armed & PC_DAYROLL_KIND_DAY)) {
        return; /* a growth-only roll: the field build that follows grows the field */
    }
    if (Save_Get(scene_no) != SCENE_FG) {
        s_dr.init_skipped = 1;
        pc_dr_log("the reload did not load the town (scene %d): new-day init skipped", (int)Save_Get(scene_no));
        return;
    }
    memset(&in, 0, sizeof(in));
    pc_dr_peer_gate(&in, who, sizeof(who));
    if (in.peers_joining > 0 || in.pending_interactions > 0 || in.hard_blockers > 0) {
        s_dr.init_skipped = 1;
        pc_dr_log("new-day init skipped inside the reload (joining %d, pending %d, in shop / villager house %d%s%s): retried", in.peers_joining, in.pending_interactions,
                  in.hard_blockers, who[0] ? ": " : "", who);
        return;
    }
    if (mSDI_DayRollInit((GAME*)play) != TRUE) {
        s_dr.init_skipped = 1;
        pc_dr_log("new-day init FAILED (no valid town save / binding)");
        return;
    }
    mEv_init(&play->event); /* the boot order: the force-init ran before this scene load, play_init's normal init after it */
    s_dr.init_ran = 1;
    pc_dr_note_init_date();
    pc_dr_log("new-day init ran (binding player_no %d) for %04d-%02d-%02d: weather %d, villagers %d", (int)Common_Get(player_no), s_dr.init_date.year, s_dr.init_date.month,
              s_dr.init_date.day, (int)Common_Get(weather), pc_dr_villagers());
}

int pc_day_roll_save_due(void) {
    return s_dr.save_due;
}

void pc_day_roll_save_report(int ok) {
    s_dr.save_due = 0;
    pc_dr_log("%s", ok ? "saved (authoritative town save written)" : "save FAILED (the periodic save retries)");
}

void pc_day_roll_console(const char* args, void (*out)(const char* line)) {
    char line[256];
    PCDayRollDate now, renew, grow, next;
    PCDayRollGateIn in;
    char who[96];
    long long dk, gk;
    int clear, kinds, gate;
    while (args != NULL && (*args == ' ' || *args == '\t')) {
        args++;
    }
    if (args != NULL && *args != '\0' && strcmp(args, "status") != 0 && strcmp(args, "roll now") != 0 && strncmp(args, "advance", 7) != 0) {
        out("usage: day [status] | day roll now | day advance [hhmm] (test builds)");
        return;
    }
    if (pc_net_game_role() != PC_NETGAME_ROLE_HOST || !pc_net_game_dedicated_world_ready()) {
        out("DAY: the world is not ready yet");
        return;
    }
    if (args != NULL && strncmp(args, "advance", 7) == 0) {
#ifdef PC_NET_TEST_HOOKS
        int hh = 0, mm = 0, sec;
        char spec[16];
        const char* a = args + 7;
        while (*a == ' ') {
            a++;
        }
        snprintf(spec, sizeof(spec), "0%s%s", *a ? "," : "", a);
        if (!pc_test_hooks_enabled()) {
            out("DAY ADVANCE REFUSED: test hooks are off (AC_TEST_HOOKS=1 in a test build)");
            return;
        }
        if (!pc_dayroll_parse_test_at(spec, &sec, &hh, &mm)) {
            out("usage: day advance [hhmm]");
            return;
        }
        pc_dr_advance_clock(hh, mm, "console day advance");
        out("DAY ADVANCE: the game clock moved to tomorrow (TEST-ONLY)");
#else
        out("DAY ADVANCE UNSUPPORTED: not a test build");
        return;
#endif
    }
    pc_dr_dates(&now, &renew, &grow, &clear);
    kinds = pc_dr_due_kinds(&dk, &gk);
    pc_dr_gate_in(&in, who, sizeof(who));
    in.deferred_ms = s_dr.due_kinds ? pc_net_game_now_ms() - s_dr.due_since_ms : 0;
    if (args != NULL && strcmp(args, "roll now") == 0) {
        if (kinds == 0) {
            out("DAY ROLL: nothing is due (the last new-day init is for today and the next growth time has not come)");
        } else {
            s_dr.ignore_soft = 1;
            in.ignore_soft = 1;
            out("DAY ROLL: the wait for players in buildings is skipped for this roll (shops / villager houses / joining players still hold it)");
        }
    }
    gate = pc_dayroll_gate(&in);
    snprintf(line, sizeof(line), "[DEDICATED] day: rollover %s; game date %04d-%02d-%02d %02d:%02d:%02d", pc_dr_enabled() ? "ON" : "OFF (AC_MP_DAY_ROLL=0)", now.year, now.month,
             now.day, now.hour, now.min, now.sec);
    out(line);
    if (clear) {
        snprintf(line, sizeof(line), "  last new-day init %04d-%02d-%02d; all_grow_renew_time cleared (first growth pending)", renew.year, renew.month, renew.day);
    } else {
        pc_dayroll_next_grow(&grow, &next);
        snprintf(line, sizeof(line), "  last new-day init %04d-%02d-%02d; last growth %04d-%02d-%02d %02d:%02d; next growth from %04d-%02d-%02d 06:00", renew.year, renew.month, renew.day,
                 grow.year, grow.month, grow.day, grow.hour, grow.min, next.year, next.month, next.day);
    }
    out(line);
    if (s_dr.armed) {
        snprintf(line, sizeof(line), "  state: ROLLING (%s)", pc_dr_kind_name(s_dr.armed));
    } else if (kinds == 0) {
        snprintf(line, sizeof(line), "  state: idle (next roll at midnight, growth at 6 AM)");
    } else if (gate == PC_DAYROLL_GO) {
        snprintf(line, sizeof(line), "  state: DUE (%s): starts on the next frame", pc_dr_kind_name(kinds));
    } else {
        snprintf(line, sizeof(line), "  state: DEFERRED (%s): %s%s%s; waiting %u s", pc_dr_kind_name(kinds), pc_dayroll_gate_name(gate), who[0] ? ": " : "", who,
                 (unsigned)(in.deferred_ms / 1000u));
    }
    out(line);
    snprintf(line, sizeof(line), "  rolls this run: %d; last: %s", s_dr.rolls, s_dr.last_result[0] ? s_dr.last_result : "none");
    out(line);
}
