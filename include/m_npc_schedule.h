#ifndef M_NPC_SCHEDULE_H
#define M_NPC_SCHEDULE_H

#include "types.h"
#include "m_npc_schedule_h.h"

#ifdef __cplusplus
extern "C" {
#endif

extern mNPS_schedule_c* mNPS_get_schedule_area(AnmPersonalID_c* anm_id);
extern void mNPS_set_island_schedule_area(AnmPersonalID_c* anm_id);
extern void mNPS_reset_schedule_area(AnmPersonalID_c* anm_id);
extern void mNPS_schedule_manager();
extern void mNPS_set_all_schedule_area();

/* N3 FIX S3 (PC multiplayer): re-registers ONE slot's schedule area -- same registration logic
 * mNPS_set_all_schedule_area()'s loop already uses (the previously-`static` mNPS_set_schedule_area()),
 * just callable for a single AnmPersonalID_c outside full start-data init. See m_npc.c's
 * mNpc_PcApplyVillagerArrival()/mNpc_PcApplyVillagerSnapshotSlot() for why this is needed: both call
 * mNpc_ClearAnimalInfo() (-> mNPS_reset_schedule_area(), clearing npc_schedule[slot].id to NULL) and
 * never re-registered it before N3, so a network-driven arrival/snapshot silently fell out of
 * mNPS_schedule_manager()'s iteration forever. */
extern void mNPS_pc_register_schedule_area(AnmPersonalID_c* anm_id);

#ifdef __cplusplus
}
#endif

#endif
