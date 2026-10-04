#ifndef AC_MY_HOUSE_H
#define AC_MY_HOUSE_H

#include "types.h"
#include "m_actor.h"
#include "ac_structure.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct my_house_actor_s MY_HOUSE_ACTOR;

struct my_house_actor_s {
    STRUCTURE_ACTOR structure_class;
};

extern ACTOR_PROFILE MyHouse_Profile;

#ifdef TARGET_PC
/* pc_remote_player.c: cosmetic door animation for a remote puppet (returns 1 when started) */
int aMHS_pc_cosmetic_door(ACTOR* actorx, int exit);
#endif

#ifdef __cplusplus
}
#endif

#endif
