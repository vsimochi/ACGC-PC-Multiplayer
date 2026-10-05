/* membership_equiv_selftest.c - native EQUIVALENCE test (M-H): pc_mp_membership_resolve (pc/src/pc_mp_membership.c) vs a C COPY of the old host classifier
 * (pcnetgame_host_classify_identity) running on stubs that copy the real semantics of mPr_CheckCmpPersonalID / mPr_NullCheckPersonalID / mPr_CheckPrivate /
 * mLd_CheckCmpLandName / mPr_CheckCmpPlayerName (src/game/m_private.c, src/game/m_land.c, include/m_land.h: mLd_CHECK_LAND_ID(id) = ((id & 0xFF00) == 0x3000),
 * blank = every byte CHAR_SPACE 0x20, the comparisons are plain 8-byte memcmp after the blank check). Prints PASS:/FAIL: lines and "RESULT passed=N failed=M".
 * Expected: identical RESIDENT(idx) / UNKNOWN / AMBIGUOUS results wherever the old code defines them; the documented additions (guest aliasing, STALE) are refusals. */
#include "pc_mp_membership.h"

#include <stdio.h>
#include <string.h>

#define TRUE 1
#define FALSE 0
#define CHAR_SPACE 0x20
#define mLd_BITMASK 0x3000
#define mLd_CHECK_LAND_ID(id) (((id) & 0xFF00) == mLd_BITMASK)

typedef struct { unsigned char player_name[8]; unsigned char land_name[8]; unsigned short player_id; unsigned short land_id; } PersonalID_c;
typedef struct { PersonalID_c player_ID; int exists; } Private_c;

static int mem_cmp(const unsigned char* a, const unsigned char* b, int n) { return memcmp(a, b, (size_t)n) == 0 ? TRUE : FALSE; }
static int mLd_NullCheckLandName(const unsigned char* n) { int i; for (i = 0; i < 8; i++) { if (n[i] != CHAR_SPACE) break; } return i == 8 ? TRUE : FALSE; }
static int mLd_CheckCmpLandName(const unsigned char* a, const unsigned char* b) { return (mLd_NullCheckLandName(a) == FALSE && mLd_NullCheckLandName(b) == FALSE) ? mem_cmp(a, b, 8) : FALSE; }
static int mPr_NullCheckPlayerName(const unsigned char* n) { int i; for (i = 0; i < 8; i++) { if (n[i] != CHAR_SPACE) break; } return i == 8 ? TRUE : FALSE; }
static int mPr_CheckCmpPlayerName(const unsigned char* a, const unsigned char* b) { return (mPr_NullCheckPlayerName(a) == FALSE && mPr_NullCheckPlayerName(b) == FALSE) ? mem_cmp(a, b, 8) : FALSE; }
static int mPr_NullCheckPersonalID(const PersonalID_c* p) { return (p->land_id == 0xFFFF && mPr_NullCheckPlayerName(p->player_name) == TRUE) ? TRUE : FALSE; }
static int mPr_CheckCmpPersonalID(const PersonalID_c* a, const PersonalID_c* b) {
    return (a->land_id == b->land_id && a->player_id == b->player_id && mLd_CheckCmpLandName(a->land_name, b->land_name) == TRUE &&
            mPr_CheckCmpPlayerName(a->player_name, b->player_name) == TRUE) ? TRUE : FALSE;
}
static int mPr_CheckPrivate(const Private_c* p) { return mLd_CHECK_LAND_ID(p->player_ID.land_id) ? TRUE : FALSE; }

enum { OLD_RESIDENT = 0, OLD_UNKNOWN, OLD_AMBIGUOUS };

/* C copy of pcnetgame_host_classify_identity (pc_net_game.c) */
static int old_classify(const PersonalID_c* claim, const Private_c priv[4], int* out_idx) {
    int i, matches = 0, idx = -1;
    *out_idx = -1;
    for (i = 0; i < 4; i++) {
        const Private_c* p = &priv[i];
        if (mPr_CheckPrivate(p) == TRUE && p->exists == TRUE && mPr_NullCheckPersonalID(&p->player_ID) == FALSE && mPr_CheckCmpPersonalID(claim, &p->player_ID) == TRUE) {
            matches++;
            if (idx < 0) idx = i;
        }
    }
    if (matches == 0) return OLD_UNKNOWN;
    if (matches > 1) return OLD_AMBIGUOUS;
    *out_idx = idx;
    return OLD_RESIDENT;
}

static void to_be(const PersonalID_c* p, uint8_t out[20]) {
    memcpy(out, p->player_name, 8);
    memcpy(out + 8, p->land_name, 8);
    out[16] = (uint8_t)(p->player_id >> 8); out[17] = (uint8_t)p->player_id;
    out[18] = (uint8_t)(p->land_id >> 8); out[19] = (uint8_t)p->land_id;
}

static PersonalID_c mk(const char* name, const char* land, unsigned pid, unsigned lid) {
    PersonalID_c p;
    memset(p.player_name, CHAR_SPACE, 8); memset(p.land_name, CHAR_SPACE, 8);
    memcpy(p.player_name, name, strlen(name)); memcpy(p.land_name, land, strlen(land));
    p.player_id = (unsigned short)pid; p.land_id = (unsigned short)lid;
    return p;
}

static int g_pass = 0, g_fail = 0;
static void check(const char* d, int c) { if (c) { g_pass++; printf("PASS: %s\n", d); } else { g_fail++; printf("FAIL: %s\n", d); } }

static PCMpTownKey g_town;
static const uint32_t TH = 0x11223344u;

typedef struct { Private_c priv[4]; PCMpGuestFile guests; PCMpMemberFile members; int use_guests, use_members; } World;

static void world_init(World* w) {
    int i;
    memset(w, 0, sizeof(*w));
    for (i = 0; i < 4; i++) { w->priv[i].player_ID = mk("", "", 0xFFFF, 0xFFFF); w->priv[i].exists = FALSE; }
}

static void guest_add(World* w, int slot, const PersonalID_c* pid) {
    PCMpGuestEntry* e = &w->guests.e[slot];
    e->present = 1;
    to_be(pid, e->pid);
    memcpy(e->town_land_name, g_town.land_name, 8);
    e->town_land_id = g_town.land_id;
    e->town_terrain_hash = g_town.terrain_hash;
    w->use_guests = 1;
}

static void member_add(World* w, int slot, const PersonalID_c* pid, int kind) {
    PCMpMemberEntry* e = &w->members.e[slot];
    e->present = 1; e->kind = (uint8_t)kind; e->res_slot = 0;
    memcpy(e->land_name, g_town.land_name, 8); e->land_id = g_town.land_id; e->terrain_hash = g_town.terrain_hash;
    to_be(pid, e->pid);
    w->use_members = 1;
}

/* runs both; returns the resolver kind, *old = the classify class, *oidx / *ridx the indexes */
static int run(const World* w, const PersonalID_c* claim, int ext_kind, const PersonalID_c* home, int own_idx, PCMpAdmitView* v, int* old, int* oidx) {
    PCMpAdmitIn in;
    int i;
    memset(&in, 0, sizeof(in));
    to_be(claim, in.claim_pid);
    in.ext_kind = ext_kind;
    if (home) to_be(home, in.ext_home_pid);
    in.town = g_town;
    for (i = 0; i < 4; i++) {
        to_be(&w->priv[i].player_ID, in.res[i].pid);
        in.res[i].valid = (mLd_CHECK_LAND_ID(w->priv[i].player_ID.land_id) && mPr_NullCheckPersonalID(&w->priv[i].player_ID) == FALSE) ? 1 : 0;
        in.res[i].exists = w->priv[i].exists == TRUE ? 1 : 0;
    }
    in.guests = w->use_guests ? &w->guests : NULL;
    in.members = w->use_members ? &w->members : NULL;
    in.own_idx = own_idx;
    *old = old_classify(claim, w->priv, oidx);
    return pc_mp_membership_resolve(&in, v);
}

/* the old class maps to the resolver kinds that are "equivalent" (the decide function's equiv_ok) */
static int equivalent(int kind, int old, int ridx, int oidx) {
    return (kind == PC_MP_ADMIT_RESIDENT && old == OLD_RESIDENT && ridx == oidx) || (kind == PC_MP_ADMIT_AMBIGUOUS && old == OLD_AMBIGUOUS) ||
           ((kind == PC_MP_ADMIT_NONE || kind == PC_MP_ADMIT_GUEST || kind == PC_MP_ADMIT_STALE) && old == OLD_UNKNOWN);
}

int main(void) {
    World w;
    PCMpAdmitView v;
    int old, oidx, kind;
    PersonalID_c alice = mk("ALICE", "TOWNAA", 0x1234, 0x3001);
    PersonalID_c c;

    memset(&g_town, 0, sizeof(g_town));
    memcpy(g_town.land_name, "TOWNAA  ", 8); g_town.land_id = 0x3001; g_town.terrain_hash = TH;

    /* 1 baseline: a resident matches */
    world_init(&w); w.priv[2].player_ID = alice; w.priv[2].exists = TRUE;
    kind = run(&w, &alice, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("baseline: RESIDENT(2) in both, equivalent", kind == PC_MP_ADMIT_RESIDENT && v.res_index == 2 && old == OLD_RESIDENT && oidx == 2 && equivalent(kind, old, v.res_index, oidx));
    kind = run(&w, &alice, PC_MP_EXT_RESIDENT, NULL, 2, &v, &old, &oidx);
    check("own resident: RESIDENT(2) with is_own set (the decide function refuses it with its own-resident text)", kind == PC_MP_ADMIT_RESIDENT && v.is_own == 1);
    kind = run(&w, &alice, PC_MP_EXT_MALFORMED, NULL, -1, &v, &old, &oidx);
    check("a malformed EXT is treated like no EXT (RESIDENT, same result)", kind == PC_MP_ADMIT_RESIDENT && v.res_index == 2);

    /* 2 same name, different land id */
    c = alice; c.land_id = 0x3002;
    kind = run(&w, &c, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("same name, different land_id: NONE / UNKNOWN", kind == PC_MP_ADMIT_NONE && old == OLD_UNKNOWN && equivalent(kind, old, v.res_index, oidx));

    /* 3 same name and land, different player id */
    c = alice; c.player_id = 0x1235;
    kind = run(&w, &c, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("same name and land, different player_id: NONE / UNKNOWN", kind == PC_MP_ADMIT_NONE && old == OLD_UNKNOWN && equivalent(kind, old, v.res_index, oidx));

    /* 4 blank name with a valid id: the raw memcmp of the old lookup would match, the vanilla comparator does not */
    world_init(&w); w.priv[1].player_ID = mk("", "TOWNAA", 0x1234, 0x3001); w.priv[1].exists = TRUE;
    c = mk("", "TOWNAA", 0x1234, 0x3001);
    kind = run(&w, &c, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("blank player name with a valid id: NONE / UNKNOWN (closes the raw-memcmp gap)", kind == PC_MP_ADMIT_NONE && old == OLD_UNKNOWN && equivalent(kind, old, v.res_index, oidx));
    {
        uint8_t a[20], b[20];
        PersonalID_c x = mk("", "TOWNAA", 1, 0x3001), y = mk("BOB", "TOWNAA", 1, 0x3001), z = mk("BOB", "", 1, 0x3001);
        to_be(&x, a); to_be(&y, b);
        check("pid_equal_vanilla: blank player name is never equal (even to itself)", !pc_mp_pid_equal_vanilla(a, a));
        check("pid_equal_vanilla: a named PID equals itself", pc_mp_pid_equal_vanilla(b, b));
        to_be(&z, a);
        check("pid_equal_vanilla: a blank land name is never equal", !pc_mp_pid_equal_vanilla(a, a));
    }

    /* 5 land id 0xFFFF in a resident record (the null / cleared PersonalID) */
    world_init(&w); w.priv[0].player_ID = mk("ALICE", "TOWNAA", 0x1234, 0xFFFF); w.priv[0].exists = TRUE;
    c = mk("ALICE", "TOWNAA", 0x1234, 0xFFFF);
    kind = run(&w, &c, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("land_id 0xFFFF: NONE / UNKNOWN (not a valid land id)", kind == PC_MP_ADMIT_NONE && old == OLD_UNKNOWN && equivalent(kind, old, v.res_index, oidx));

    /* 6 exists == FALSE */
    world_init(&w); w.priv[3].player_ID = alice; w.priv[3].exists = FALSE;
    kind = run(&w, &alice, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("exists == FALSE: NONE / UNKNOWN", kind == PC_MP_ADMIT_NONE && old == OLD_UNKNOWN && equivalent(kind, old, v.res_index, oidx));

    /* 7 invalid land id with exists == TRUE */
    world_init(&w); w.priv[3].player_ID = mk("ALICE", "TOWNAA", 0x1234, 0x4A01); w.priv[3].exists = TRUE;
    c = mk("ALICE", "TOWNAA", 0x1234, 0x4A01);
    kind = run(&w, &c, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("invalid land id (0x4A01) with exists == TRUE: NONE / UNKNOWN", kind == PC_MP_ADMIT_NONE && old == OLD_UNKNOWN && equivalent(kind, old, v.res_index, oidx));

    /* 8 two identical residents */
    world_init(&w); w.priv[0].player_ID = alice; w.priv[0].exists = TRUE; w.priv[3].player_ID = alice; w.priv[3].exists = TRUE;
    kind = run(&w, &alice, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("two identical residents: AMBIGUOUS / AMBIGUOUS", kind == PC_MP_ADMIT_AMBIGUOUS && old == OLD_AMBIGUOUS && equivalent(kind, old, v.res_index, oidx));
    w.priv[3].exists = FALSE;
    kind = run(&w, &alice, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("a duplicate with exists == FALSE is not counted: RESIDENT(0)", kind == PC_MP_ADMIT_RESIDENT && v.res_index == 0 && old == OLD_RESIDENT && oidx == 0);

    /* 9 guest aliasing: a guest entry of this town whose PID equals a resident PID */
    world_init(&w); w.priv[1].player_ID = alice; w.priv[1].exists = TRUE; guest_add(&w, 4, &alice);
    kind = run(&w, &alice, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("guest PID equal to a resident PID: the resolver says AMBIGUOUS (a REFUSAL) where the old code said RESIDENT (documented addition)",
          kind == PC_MP_ADMIT_AMBIGUOUS && old == OLD_RESIDENT && !equivalent(kind, old, v.res_index, oidx));

    /* 10 guest of ANOTHER town: inactive, never matches */
    world_init(&w); w.priv[1].player_ID = alice; w.priv[1].exists = TRUE; guest_add(&w, 4, &alice);
    w.guests.e[4].town_land_id = 0x3007;
    kind = run(&w, &alice, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("a guest entry of another town is inactive: RESIDENT(1) / RESIDENT(1)", kind == PC_MP_ADMIT_RESIDENT && v.res_index == 1 && old == OLD_RESIDENT && equivalent(kind, old, v.res_index, oidx));

    /* 11 guest claims */
    {
        PersonalID_c gp = mk("GUESTY", "OTHERTWN", 0x0777, 0x3009), claim = alice;
        world_init(&w); claim = mk("GUESTY", "TOWNAA", 0x0777, 0x3001);
        kind = run(&w, &claim, PC_MP_EXT_GUEST, &gp, -1, &v, &old, &oidx);
        check("GUEST EXT, no resident, unknown key: GUEST with slot -1 (new key) / UNKNOWN", kind == PC_MP_ADMIT_GUEST && v.guest_slot == -1 && old == OLD_UNKNOWN && equivalent(kind, old, v.res_index, oidx));
        guest_add(&w, 5, &gp);
        kind = run(&w, &claim, PC_MP_EXT_GUEST, &gp, -1, &v, &old, &oidx);
        check("GUEST EXT with a known guest of this town: GUEST with slot 5", kind == PC_MP_ADMIT_GUEST && v.guest_slot == 5 && equivalent(kind, old, v.res_index, oidx));
        w.guests.e[5].town_land_id = 0x3007;
        kind = run(&w, &claim, PC_MP_EXT_GUEST, &gp, -1, &v, &old, &oidx);
        check("GUEST EXT whose key is a guest of ANOTHER town: GUEST with slot -1 (new in this town)", kind == PC_MP_ADMIT_GUEST && v.guest_slot == -1);
        kind = run(&w, &claim, PC_MP_EXT_NONE, &gp, -1, &v, &old, &oidx);
        check("the same claim without a GUEST EXT: NONE (refused as no resident record)", kind == PC_MP_ADMIT_NONE);
        world_init(&w); w.priv[0].player_ID = alice; w.priv[0].exists = TRUE;
        kind = run(&w, &alice, PC_MP_EXT_GUEST, &gp, -1, &v, &old, &oidx);
        check("GUEST EXT but the IDENTITY matches a resident: RESIDENT (the decide function refuses it: guest claim on a resident)", kind == PC_MP_ADMIT_RESIDENT && v.res_index == 0 && equivalent(kind, old, v.res_index, oidx));
    }

    /* 12 credential for an absent PID */
    world_init(&w); member_add(&w, 0, &alice, PC_MP_MEMBER_KIND_RESIDENT_TOKEN);
    kind = run(&w, &alice, PC_MP_EXT_RESIDENT, NULL, -1, &v, &old, &oidx);
    check("a credential for a PID with no resident: STALE (a refusal; old: UNKNOWN, also a refusal)", kind == PC_MP_ADMIT_STALE && old == OLD_UNKNOWN && equivalent(kind, old, v.res_index, oidx));
    w.members.e[0].kind = PC_MP_MEMBER_KIND_PROMOTION_HANDOFF;
    kind = run(&w, &alice, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("a handoff entry for the PID with no resident: STALE", kind == PC_MP_ADMIT_STALE);
    w.members.e[0].terrain_hash ^= 1u;
    kind = run(&w, &alice, PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
    check("a credential of ANOTHER town is not a STALE signal: NONE", kind == PC_MP_ADMIT_NONE);
    w.members.e[0].terrain_hash ^= 1u;
    w.priv[2].player_ID = alice; w.priv[2].exists = FALSE;
    kind = run(&w, &alice, PC_MP_EXT_RESIDENT, NULL, -1, &v, &old, &oidx);
    check("a credential and the resident record has exists == FALSE: STALE", kind == PC_MP_ADMIT_STALE);
    w.priv[2].exists = TRUE;
    kind = run(&w, &alice, PC_MP_EXT_RESIDENT, NULL, -1, &v, &old, &oidx);
    check("the same credential with a live resident: RESIDENT(2) (never STALE)", kind == PC_MP_ADMIT_RESIDENT && v.res_index == 2 && equivalent(kind, old, v.res_index, oidx));
    w.use_members = 0;
    w.priv[2].exists = FALSE;
    kind = run(&w, &alice, PC_MP_EXT_RESIDENT, NULL, -1, &v, &old, &oidx);
    check("members file not given (policy off / untrusted): the same claim is NONE, not STALE", kind == PC_MP_ADMIT_NONE);

    /* 13 exhaustive sweep: every combination of two residents x claim variants: resolver (without guests / members) must equal the old classifier */
    {
        int bad = 0, n = 0, a, b, ea, eb, cl;
        PersonalID_c names[3];
        names[0] = mk("ALICE", "TOWNAA", 0x1234, 0x3001);
        names[1] = mk("", "TOWNAA", 0x1234, 0x3001);
        names[2] = mk("ALICE", "TOWNAA", 0x1234, 0xFFFF);
        for (a = 0; a < 3; a++) for (b = 0; b < 3; b++) for (ea = 0; ea < 2; ea++) for (eb = 0; eb < 2; eb++) for (cl = 0; cl < 3; cl++) {
            world_init(&w);
            w.priv[0].player_ID = names[a]; w.priv[0].exists = ea;
            w.priv[2].player_ID = names[b]; w.priv[2].exists = eb;
            kind = run(&w, &names[cl], PC_MP_EXT_NONE, NULL, -1, &v, &old, &oidx);
            n++;
            if (!equivalent(kind, old, v.res_index, oidx)) bad++;
        }
        printf("sweep: %d combinations, %d disagreements\n", n, bad);
        check("exhaustive sweep (3 x 3 x 2 x 2 x 3 residents/claims incl. blank names, land id 0xFFFF, exists FALSE): resolver == old classify in every case", bad == 0 && n == 108);
    }

    printf("RESULT passed=%d failed=%d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
