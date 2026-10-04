#ifndef AC_POST_OFFICE_H
#define AC_POST_OFFICE_H

#include "types.h"
#include "ac_structure.h"

#ifdef __cplusplus
extern "C" {
#endif

extern ACTOR_PROFILE Post_Office_Profile;

#ifdef TARGET_PC
/* pc_remote_player.c: cosmetic door animation for a remote puppet (returns 1 when started) */
int aPOFF_pc_cosmetic_door(ACTOR* actorx, int exit);
#endif

#ifdef __cplusplus
}
#endif

#endif

