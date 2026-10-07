/* pc_mp_membership.h - town memberships (M2): is a PersonalID a RESIDENT, a GUEST or NEW (NONE) in a given host town? Pure C over arrays, no game state.
 *
 * The key of a membership is (host town identity, PersonalID): the same PID can be a resident of town A, a guest of town B and unknown in town C.
 * Residents are given as the 4 vanilla private_data PersonalIDs of THAT town (BE 20-byte form: name 8, land name 8, player_id BE, land_id BE); guests come from the
 * host guest table (pc_mp_guests.h PCMpGuestFile, whose entries carry their own host town). Entries of other towns are INACTIVE and never match.
 * AMBIGUOUS = the PID is both a resident and a guest of the same town (the host refuses to create such a guest; reported, never resolved silently). */
#ifndef PC_MP_MEMBERSHIP_H
#define PC_MP_MEMBERSHIP_H

#include <stdint.h>
#include "pc_mp_guests.h"
#include "pc_mp_members.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The guest table as the membership layer sees it: one lightweight ROW per guest (no record), read through a callback so that a store of ANY size can be searched (the fixed
 * 8-entry PCMpGuestFile is just one source of rows). fn returns 1 while `idx` is inside the table (the row may be absent: present = 0) and 0 past its end. */
typedef struct PCMpGuestRow {
    uint8_t  present;
    uint8_t  confirmed;
    uint8_t  pid[20];
    uint8_t  town_land_name[8];
    uint16_t town_land_id;
    uint32_t town_terrain_hash;
} PCMpGuestRow;
typedef int (*PCMpGuestRowFn)(void* ctx, int idx, PCMpGuestRow* row);
typedef struct PCMpGuestSource {
    PCMpGuestRowFn fn;
    void*          ctx;
} PCMpGuestSource;

enum { PC_MP_MEMBER_NONE = 0, PC_MP_MEMBER_RESIDENT = 1, PC_MP_MEMBER_GUEST = 2, PC_MP_MEMBER_AMBIGUOUS = 3 };

typedef struct PCMpTownKey {
    uint8_t  land_name[8];
    uint16_t land_id;
    uint32_t terrain_hash;
} PCMpTownKey;

typedef struct PCMpMembership {
    int     kind;        /* PC_MP_MEMBER_* */
    int     res_index;   /* resident slot 0..3, -1 if none */
    int     guest_slot;  /* guest table slot (0 .. the table size - 1), -1 if none */
    int     confirmed;   /* guest entry confirmed flag */
    uint8_t pid[20];
} PCMpMembership;

/* Returns the kind; `out` (optional) is filled. res_exists[i] != 0 marks a used resident slot. guests may be NULL. */
int pc_mp_membership_lookup(const uint8_t pid_be[20], const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4],
                            const PCMpGuestFile* guests, PCMpMembership* out);

/* The same two functions over any guest source (the host's per-guest store of any size); `guests` NULL = no guests. Identical results to the PCMpGuestFile forms for the same rows. */
int pc_mp_membership_lookup_src(const uint8_t pid_be[20], const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4],
                                const PCMpGuestSource* guests, PCMpMembership* out);
int pc_mp_membership_list_src(const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4], const PCMpGuestSource* guests,
                              PCMpMembership* rows, int cap);

/* Lists every member of `town`: residents first (slot order), then the active guests (table order). Returns the number of rows written (<= cap). */
int pc_mp_membership_list(const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4], const PCMpGuestFile* guests,
                          PCMpMembership* rows, int cap);

/* ---- M-H: the ADMISSION classifier (pure): the membership layer is the authority that decides who an IDENTITY claim is ----
 * Inputs are plain arrays (no game state). `pid_equal_vanilla` mirrors mPr_CheckCmpPersonalID on the 20-byte BE form: equal player_id AND land_id, and both the player name and the
 * land name are NOT blank (all 0x20 = CHAR_SPACE) and byte-equal. A resident record counts only when `valid` (mLd_CHECK_LAND_ID(land_id) and the PersonalID is not the null one) and
 * `exists` (exists == FALSE is vanilla's "resident away" state). Results:
 *   RESIDENT   exactly one valid, existing resident matches (res_index) and the claim matches no guest entry of the town
 *   AMBIGUOUS  more than one resident matches, OR a resident matches and a guest entry of this town has the same PID (guest aliasing)
 *   GUEST      no resident matches and the EXT is a GUEST claim: guest_slot = the entry of this town with the claimed home PID, or -1 (a NEW key)
 *   STALE      no resident matches, the EXT is not a GUEST claim, but the members file holds a credential / handoff of this town for the claim PID
 *   NONE       anything else (a claim that is no member; a guest PID WITHOUT a guest claim is NONE as well)
 * A malformed EXT is treated exactly like no EXT. */
enum { PC_MP_EXT_NONE = 0, PC_MP_EXT_GUEST = 1, PC_MP_EXT_RESIDENT = 2, PC_MP_EXT_MALFORMED = 3 };
enum { PC_MP_ADMIT_NONE = 0, PC_MP_ADMIT_RESIDENT = 1, PC_MP_ADMIT_GUEST = 2, PC_MP_ADMIT_AMBIGUOUS = 3, PC_MP_ADMIT_STALE = 4 };

typedef struct PCMpAdmitRes {
    uint8_t pid[20];
    uint8_t valid;   /* mLd_CHECK_LAND_ID(land_id) and not the null PersonalID */
    uint8_t exists;  /* Private_c.exists == TRUE */
} PCMpAdmitRes;

typedef struct PCMpAdmitIn {
    uint8_t              claim_pid[20];     /* the IDENTITY claim (BE) */
    int                  ext_kind;          /* PC_MP_EXT_* (GUEST wins when both flags are set, like the host) */
    uint8_t              ext_home_pid[20];  /* the EXT home PersonalID (BE), meaningful for GUEST */
    PCMpTownKey          town;
    PCMpAdmitRes         res[4];
    const PCMpGuestFile* guests;            /* NULL = none / untrusted */
    const PCMpGuestSource* guest_src;       /* used when `guests` is NULL: the same table as rows of ANY size (NULL = none) */
    const PCMpMemberFile* members;          /* NULL = none / untrusted / not loaded */
    int                  own_idx;           /* the host's own active resident, -1 none */
} PCMpAdmitIn;

typedef struct PCMpAdmitView {
    int kind;         /* PC_MP_ADMIT_* */
    int res_index;    /* RESIDENT: the slot, else -1 */
    int guest_slot;   /* GUEST: the table slot or -1 (new key); else -1 */
    int n_res_match;  /* residents matching the claim */
    int is_own;       /* RESIDENT and res_index == own_idx */
} PCMpAdmitView;

int pc_mp_pid_equal_vanilla(const uint8_t a[20], const uint8_t b[20]);
int pc_mp_membership_resolve(const PCMpAdmitIn* in, PCMpAdmitView* out);

#ifdef __cplusplus
}
#endif
#endif
