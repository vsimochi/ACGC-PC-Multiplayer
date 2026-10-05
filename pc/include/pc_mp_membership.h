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

#ifdef __cplusplus
extern "C" {
#endif

enum { PC_MP_MEMBER_NONE = 0, PC_MP_MEMBER_RESIDENT = 1, PC_MP_MEMBER_GUEST = 2, PC_MP_MEMBER_AMBIGUOUS = 3 };

typedef struct PCMpTownKey {
    uint8_t  land_name[8];
    uint16_t land_id;
    uint32_t terrain_hash;
} PCMpTownKey;

typedef struct PCMpMembership {
    int     kind;        /* PC_MP_MEMBER_* */
    int     res_index;   /* resident slot 0..3, -1 if none */
    int     guest_slot;  /* guest table slot 0..7, -1 if none */
    int     confirmed;   /* guest entry confirmed flag */
    uint8_t pid[20];
} PCMpMembership;

/* Returns the kind; `out` (optional) is filled. res_exists[i] != 0 marks a used resident slot. guests may be NULL. */
int pc_mp_membership_lookup(const uint8_t pid_be[20], const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4],
                            const PCMpGuestFile* guests, PCMpMembership* out);

/* Lists every member of `town`: residents first (slot order), then the active guests (table order). Returns the number of rows written (<= cap). */
int pc_mp_membership_list(const PCMpTownKey* town, const uint8_t res_pid[4][20], const uint8_t res_exists[4], const PCMpGuestFile* guests,
                          PCMpMembership* rows, int cap);

#ifdef __cplusplus
}
#endif
#endif
