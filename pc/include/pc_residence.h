#ifndef PC_RESIDENCE_H
#define PC_RESIDENCE_H

/* Residence allocator (guest -> resident lifecycle hardening). ONE place for "how many resident slots / houses a town has", and the prototypes of the allocator that lives in
 * pc_net_game.c (kept out of pc_net_game.h on purpose: that wire header stays byte-identical to HEAD).
 *
 * The vanilla save packs house_arrangement as 2 bits per player (so 4 players -> 8 bits) and mHS_* assume house_no < PLAYER_NUM: more than 4 houses is NOT a one-constant
 * change (see docs/multiplayer-guest-roadmap.md "what >4 houses would still need"). The static asserts below make a change of either constant a build error until that work is done.
 *
 * HOST pair: pc_residence_find_free() picks the free resident slot / house of the host save (want_* = -1 auto, else explicit; 1 = found, 0 = *reason is NO_RESIDENCE 28 /
 * INVALID_HOUSE 29, *slot / *house = -1 for the part that failed); pc_residence_check() = the invariants a promotion relies on (err filled on failure); pc_residence_assign() =
 * checks + vanilla mHS_set_use + mEv_ClearPersonalEventFlag. CLIENT twin over the local copy of the town: pc_residence_local_free() returns the number of free houses and fills
 * the list / *reason. pc_net_game_pocket_legal_item() = the D3 pocket-legal predicate (pc_net_game.c). */
#include <stddef.h>
#include <stdint.h>
#include "m_personal_id.h" /* PLAYER_NUM */
#include "m_house.h"       /* mHS_HOUSE_NUM */

#define PC_RESIDENCE_SLOTS  PLAYER_NUM
#define PC_RESIDENCE_HOUSES mHS_HOUSE_NUM

_Static_assert(PC_RESIDENCE_SLOTS == 4, "house_arrangement packs 2 bits per player: exactly 4 resident slots");
_Static_assert(PC_RESIDENCE_HOUSES == 4, "house_arrangement packs 2 bits per player: exactly 4 houses");

#ifdef __cplusplus
extern "C" {
#endif
int pc_residence_find_free(int want_slot, int want_house, int* slot, int* house, uint8_t* reason);
int pc_residence_check(int slot, int house, char* err, size_t cap);
int pc_residence_assign(int slot, int house);
int pc_residence_local_free(int want_house, int* slot, int* house, int* list, int cap, uint8_t* reason);
int pc_net_game_pocket_legal_item(unsigned item);
#ifdef __cplusplus
}
#endif

#endif
