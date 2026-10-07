/* pc_mp_membership.c - see pc_mp_membership.h. */
#include "pc_mp_membership.h"

#include <string.h>

static int row_in_town(const PCMpGuestRow* r, const PCMpTownKey* t) {
    return r->present && memcmp(r->town_land_name, t->land_name, 8) == 0 && r->town_land_id == t->land_id && r->town_terrain_hash == t->terrain_hash;
}

/* rows of the fixed PCMpGuestFile (the original source) */
static int file_row(void* ctx, int idx, PCMpGuestRow* row) {
    const PCMpGuestFile* f = (const PCMpGuestFile*)ctx;
    const PCMpGuestEntry* e;
    if (f == NULL || idx < 0 || idx >= PC_MP_GUEST_SLOTS) {
        return 0;
    }
    e = &f->e[idx];
    row->present = e->present;
    row->confirmed = e->confirmed;
    memcpy(row->pid, e->pid, 20);
    memcpy(row->town_land_name, e->town_land_name, 8);
    row->town_land_id = e->town_land_id;
    row->town_terrain_hash = e->town_terrain_hash;
    return 1;
}

static int next_row(const PCMpGuestSource* src, int idx, PCMpGuestRow* row) {
    if (src == NULL || src->fn == NULL) {
        return 0;
    }
    memset(row, 0, sizeof(*row));
    return src->fn(src->ctx, idx, row);
}

int pc_mp_membership_lookup_src(const uint8_t pid_be[20], const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4],
                                const PCMpGuestSource* guests, PCMpMembership* out) {
    PCMpMembership m;
    PCMpGuestRow r;
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
    for (i = 0; town != NULL && next_row(guests, i, &r); i++) {
        if (row_in_town(&r, town) && memcmp(r.pid, pid_be, 20) == 0) {
            m.guest_slot = i;
            m.confirmed = r.confirmed ? 1 : 0;
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

int pc_mp_membership_lookup(const uint8_t pid_be[20], const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4],
                            const PCMpGuestFile* guests, PCMpMembership* out) {
    PCMpGuestSource src;
    src.fn = file_row;
    src.ctx = (void*)guests;
    return pc_mp_membership_lookup_src(pid_be, town, res_pid, res_exists, guests != NULL ? &src : NULL, out);
}

int pc_mp_membership_list_src(const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4], const PCMpGuestSource* guests,
                              PCMpMembership* rows, int cap) {
    PCMpGuestRow r;
    int n = 0, i;
    for (i = 0; res_pid != NULL && res_exists != NULL && i < 4 && n < cap; i++) {
        if (res_exists[i]) {
            pc_mp_membership_lookup_src(res_pid[i], town, res_pid, res_exists, guests, &rows[n]);
            rows[n].res_index = i; /* a duplicate resident PID keeps its own slot */
            n++;
        }
    }
    for (i = 0; town != NULL && n < cap && next_row(guests, i, &r); i++) {
        if (row_in_town(&r, town)) {
            pc_mp_membership_lookup_src(r.pid, town, res_pid, res_exists, guests, &rows[n]);
            rows[n].guest_slot = i;
            rows[n].confirmed = r.confirmed ? 1 : 0;
            n++;
        }
    }
    return n;
}

int pc_mp_membership_list(const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4], const PCMpGuestFile* guests,
                          PCMpMembership* rows, int cap) {
    PCMpGuestSource src;
    src.fn = file_row;
    src.ctx = (void*)guests;
    return pc_mp_membership_list_src(town, res_pid, res_exists, guests != NULL ? &src : NULL, rows, cap);
}

/* ---- M-H: the admission classifier ---- */
static int blank8(const uint8_t* n) {
    int i;
    for (i = 0; i < 8; i++) {
        if (n[i] != 0x20) {
            return 0;
        }
    }
    return 1;
}

/* mPr_CheckCmpPersonalID on the BE 20-byte form (name 8, land name 8, player_id 2, land_id 2). */
int pc_mp_pid_equal_vanilla(const uint8_t a[20], const uint8_t b[20]) {
    return memcmp(a + 16, b + 16, 4) == 0 && !blank8(a + 8) && !blank8(b + 8) && memcmp(a + 8, b + 8, 8) == 0 && !blank8(a) && !blank8(b) && memcmp(a, b, 8) == 0;
}

int pc_mp_membership_resolve(const PCMpAdmitIn* in, PCMpAdmitView* out) {
    PCMpAdmitView v;
    int i, guest_alias = 0;
    PCMpGuestSource fsrc;
    const PCMpGuestSource* gs;
    PCMpGuestRow gr;
    memset(&v, 0, sizeof(v));
    fsrc.fn = file_row;
    fsrc.ctx = (void*)in->guests;
    gs = in->guests != NULL ? &fsrc : in->guest_src;
    v.res_index = -1;
    v.guest_slot = -1;
    for (i = 0; i < 4; i++) {
        if (in->res[i].valid && in->res[i].exists && pc_mp_pid_equal_vanilla(in->claim_pid, in->res[i].pid)) {
            if (v.n_res_match == 0) {
                v.res_index = i;
            }
            v.n_res_match++;
        }
    }
    for (i = 0; next_row(gs, i, &gr); i++) {
        if (row_in_town(&gr, &in->town) && pc_mp_pid_equal_vanilla(gr.pid, in->claim_pid)) {
            guest_alias = 1;
        }
    }
    if (v.n_res_match > 1 || (v.n_res_match == 1 && guest_alias)) {
        v.kind = PC_MP_ADMIT_AMBIGUOUS;
        v.res_index = -1;
    } else if (v.n_res_match == 1) {
        v.kind = PC_MP_ADMIT_RESIDENT;
        v.is_own = (in->own_idx >= 0 && v.res_index == in->own_idx) ? 1 : 0;
    } else if (in->ext_kind == PC_MP_EXT_GUEST) {
        v.kind = PC_MP_ADMIT_GUEST;
        for (i = 0; next_row(gs, i, &gr); i++) {
            if (row_in_town(&gr, &in->town) && memcmp(gr.pid, in->ext_home_pid, 20) == 0) {
                v.guest_slot = i;
                break;
            }
        }
    } else {
        v.kind = PC_MP_ADMIT_NONE;
        for (i = 0; in->members != NULL && i < PC_MP_MEMBERS_SLOTS; i++) {
            const PCMpMemberEntry* e = &in->members->e[i];
            if (e->present && memcmp(e->land_name, in->town.land_name, 8) == 0 && e->land_id == in->town.land_id && e->terrain_hash == in->town.terrain_hash &&
                memcmp(e->pid, in->claim_pid, 20) == 0) {
                v.kind = PC_MP_ADMIT_STALE;
                break;
            }
        }
    }
    *out = v;
    return v.kind;
}
