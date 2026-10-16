/* pc_day_roll.c - the PURE part of the host day rollover (see pc_day_roll.h): no game headers, natively tested. */
#include "pc_day_roll.h"

#include <stdlib.h>
#include <string.h>

long pc_dayroll_days_from_civil(int y, int m, int d) {
    /* H. Hinnant's days_from_civil */
    long yy = (long)y - (m <= 2 ? 1 : 0);
    const long era = (yy >= 0 ? yy : yy - 399) / 400;
    const long yoe = yy - era * 400;
    const long mp = (long)(m + (m > 2 ? -3 : 9));
    const long doy = (153 * mp + 2) / 5 + (long)d - 1;
    const long doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}

void pc_dayroll_civil_from_days(long z, PCDayRollDate* out) {
    long era, doe, yoe, y, doy, mp, d, m;
    z += 719468;
    era = (z >= 0 ? z : z - 146096) / 146097;
    doe = z - era * 146097;
    yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    y = yoe + era * 400;
    doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    mp = (5 * doy + 2) / 153;
    d = doy - (153 * mp + 2) / 5 + 1;
    m = mp < 10 ? mp + 3 : mp - 9;
    memset(out, 0, sizeof(*out));
    out->year = (int)(y + (m <= 2 ? 1 : 0));
    out->month = (int)m;
    out->day = (int)d;
}

long long pc_dayroll_seconds(const PCDayRollDate* t) {
    return (long long)pc_dayroll_days_from_civil(t->year, t->month, t->day) * 86400LL + (long long)t->hour * 3600LL + (long long)t->min * 60LL + (long long)t->sec;
}

int pc_dayroll_day_due(const PCDayRollDate* renew, const PCDayRollDate* now) {
    if (renew == NULL || now == NULL || now->year == 0) {
        return 0; /* m_time.c mTM_renewal_renew_time (PC): an unread clock never renews */
    }
    return renew->year != now->year || renew->month != now->month || renew->day != now->day;
}

void pc_dayroll_next_grow(const PCDayRollDate* last, PCDayRollDate* out) {
    long days = pc_dayroll_days_from_civil(last->year, last->month, last->day);
    if (last->hour >= 6) {
        days++;
    }
    pc_dayroll_civil_from_days(days, out);
    out->hour = 6;
}

int pc_dayroll_grow_due(const PCDayRollDate* last, int last_is_clear, const PCDayRollDate* now) {
    PCDayRollDate next;
    if (now == NULL || now->year == 0) {
        return 0;
    }
    if (last_is_clear) {
        return 1; /* mAGrw_GROW_FIRST */
    }
    if (pc_dayroll_seconds(now) < pc_dayroll_seconds(last)) {
        return 1; /* lbRTC_IsOverTime(last, now) == lbRTC_LESS: mAGrw_GROW_FIRST_FORCE */
    }
    pc_dayroll_next_grow(last, &next);
    return pc_dayroll_seconds(now) >= pc_dayroll_seconds(&next); /* lbRTC_IsOverTime(grow_time, now) == lbRTC_OVER */
}

int pc_dayroll_scene_block(int valid, int kind) {
    if (!valid) {
        return PC_DAYROLL_BLOCK_SOFT;
    }
    switch (kind) {
        case 1: /* PC_NETSCENE_KIND_FIELD */
        case 6: /* PC_NETSCENE_KIND_PLAYER_HOUSE */
            return PC_DAYROLL_BLOCK_NONE;
        case 2: /* PC_NETSCENE_KIND_SHOP */
        case 7: /* PC_NETSCENE_KIND_VILLAGER_HOUSE */
            return PC_DAYROLL_BLOCK_HARD;
        default: /* UNKNOWN (0), POST_OFFICE (3), POLICE (4), MUSEUM (5), OTHER_INTERIOR (8) */
            return PC_DAYROLL_BLOCK_SOFT;
    }
}

int pc_dayroll_gate(const PCDayRollGateIn* in) {
    if (!in->enabled) {
        return PC_DAYROLL_WAIT_OFF;
    }
    if (!in->is_host) {
        return PC_DAYROLL_WAIT_NOT_HOST;
    }
    if (!in->world_ready) {
        return PC_DAYROLL_WAIT_NOT_READY;
    }
    if (!in->host_in_town) {
        return PC_DAYROLL_WAIT_HOST_AWAY;
    }
    if (!in->host_idle) {
        return PC_DAYROLL_WAIT_HOST_BUSY;
    }
    if (in->peers_joining > 0) {
        return PC_DAYROLL_WAIT_PEER_JOINING;
    }
    if (in->pending_interactions > 0) {
        return PC_DAYROLL_WAIT_PEER_PENDING;
    }
    if (in->hard_blockers > 0) {
        return PC_DAYROLL_WAIT_PEER_HARD;
    }
    if (in->soft_blockers > 0 && !in->ignore_soft && in->deferred_ms < in->soft_max_ms) {
        return PC_DAYROLL_WAIT_PEER_SOFT;
    }
    return PC_DAYROLL_GO;
}

const char* pc_dayroll_gate_name(int gate) {
    static const char* const names[PC_DAYROLL_GATE_NUM] = {
        "go", "switched off (AC_MP_DAY_ROLL=0)", "not hosting", "world not ready", "the host is not in the town", "the host is busy (transition / menu / talk)",
        "a player is joining", "a pickup / drop / bury is pending", "a player is in a shop or a villager house", "a player is in a building",
    };
    return (gate >= 0 && gate < PC_DAYROLL_GATE_NUM) ? names[gate] : "?";
}

int pc_dayroll_gate_is_peer_wait(int gate) {
    return gate == PC_DAYROLL_WAIT_PEER_JOINING || gate == PC_DAYROLL_WAIT_PEER_PENDING || gate == PC_DAYROLL_WAIT_PEER_HARD || gate == PC_DAYROLL_WAIT_PEER_SOFT;
}

int pc_dayroll_attempt_allowed(const PCDayRollAttempts* a, long long key, uint32_t now_ms, uint32_t gap_ms, int max_tries) {
    if (!a->have || a->key != key) {
        return 1;
    }
    if (a->tries >= max_tries) {
        return 0;
    }
    return (uint32_t)(now_ms - a->last_ms) >= gap_ms;
}

void pc_dayroll_attempt_note(PCDayRollAttempts* a, long long key, uint32_t now_ms) {
    if (!a->have || a->key != key) {
        a->have = 1;
        a->key = key;
        a->tries = 0;
    }
    a->tries++;
    a->last_ms = now_ms;
}

int pc_dayroll_parse_test_at(const char* s, int* sec, int* hh, int* mm) {
    char* end = NULL;
    long v, t;
    if (s == NULL || *s == '\0') {
        return 0;
    }
    v = strtol(s, &end, 10);
    if (end == s || v < 0 || v > 86400L * 7) {
        return 0;
    }
    *sec = (int)v;
    *hh = 0;
    *mm = 0;
    if (*end == '\0') {
        return 1;
    }
    if (*end != ',') {
        return 0;
    }
    s = end + 1;
    t = strtol(s, &end, 10);
    if (end == s || *end != '\0' || (end - s) != 4 || t < 0 || t / 100 > 23 || t % 100 > 59) {
        return 0;
    }
    *hh = (int)(t / 100);
    *mm = (int)(t % 100);
    return 1;
}
