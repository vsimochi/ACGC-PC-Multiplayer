#ifndef AC_HOUSE_H
#define AC_HOUSE_H

#include "types.h"
#include "m_actor.h"
#include "ac_structure.h"
#include "m_snowman.h"
#include "m_event_map_npc.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef STRUCTURE_ACTOR HOUSE_ACTOR;

extern ACTOR_PROFILE House_Profile;

#ifdef TARGET_PC
/* pc_remote_player.c: cosmetic door animation for a remote puppet (returns 1 when started) */
int aHUS_pc_cosmetic_door(ACTOR* actorx, int exit);
#endif

#ifdef __cplusplus
}
#endif

#endif
