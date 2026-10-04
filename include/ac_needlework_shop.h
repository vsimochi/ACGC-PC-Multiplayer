#ifndef AC_NEEDLEWORK_SHOP_H
#define AC_NEEDLEWORK_SHOP_H

#include "types.h"
#include "ac_structure.h"

#ifdef __cplusplus
extern "C" {
#endif

extern ACTOR_PROFILE Needlework_Shop_Profile;

#ifdef TARGET_PC
/* pc_remote_player.c: cosmetic door animation for a remote puppet (returns 1 when started) */
int aNW_pc_cosmetic_door(ACTOR* actorx, int exit);
#endif

#ifdef __cplusplus
}
#endif

#endif

