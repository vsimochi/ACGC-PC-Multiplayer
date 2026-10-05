/* pc_mp_membership.c - see pc_mp_membership.h. */
#include "pc_mp_membership.h"

#include <string.h>

static int guest_in_town(const PCMpGuestEntry* e, const PCMpTownKey* t) {
    return e->present && memcmp(e->town_land_name, t->land_name, 8) == 0 && e->town_land_id == t->land_id && e->town_terrain_hash == t->terrain_hash;
}

int pc_mp_membership_lookup(const uint8_t pid_be[20], const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4],
                            const PCMpGuestFile* guests, PCMpMembership* out) {
    PCMpMembership m;
    int i;
    memset(&m, 0, sizeof(m));
    m.res_index = -1;
    m.guest_slot = -1;
    memcpy(m.pid, pid_be, 20);
    for (i = 0; res_pid != NULL && res_exists != NULL && i < 4; i++) {
        if (res_exists[i] && memcmp(res_pid[i], pid_be, 20) == 0) {
            m.res_index = i;
            break;
        }
    }
    for (i = 0; guests != NULL && town != NULL && i < PC_MP_GUEST_SLOTS; i++) {
        if (guest_in_town(&guests->e[i], town) && memcmp(guests->e[i].pid, pid_be, 20) == 0) {
            m.guest_slot = i;
            m.confirmed = guests->e[i].confirmed ? 1 : 0;
            break;
        }
    }
    m.kind = (m.res_index >= 0 && m.guest_slot >= 0) ? PC_MP_MEMBER_AMBIGUOUS
           : m.res_index >= 0 ? PC_MP_MEMBER_RESIDENT
           : m.guest_slot >= 0 ? PC_MP_MEMBER_GUEST
           : PC_MP_MEMBER_NONE;
    if (out != NULL) {
        *out = m;
    }
    return m.kind;
}

int pc_mp_membership_list(const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4], const PCMpGuestFile* guests,
                          PCMpMembership* rows, int cap) {
    int n = 0, i;
    for (i = 0; res_pid != NULL && res_exists != NULL && i < 4 && n < cap; i++) {
        if (res_exists[i]) {
            pc_mp_membership_lookup(res_pid[i], town, res_pid, res_exists, guests, &rows[n]);
            rows[n].res_index = i; /* a duplicate resident PID keeps its own slot */
            n++;
        }
    }
    for (i = 0; guests != NULL && town != NULL && i < PC_MP_GUEST_SLOTS && n < cap; i++) {
        if (guest_in_town(&guests->e[i], town)) {
            pc_mp_membership_lookup(guests->e[i].pid, town, res_pid, res_exists, guests, &rows[n]);
            rows[n].guest_slot = i;
            rows[n].confirmed = guests->e[i].confirmed ? 1 : 0;
            n++;
        }
    }
    return n;
}
