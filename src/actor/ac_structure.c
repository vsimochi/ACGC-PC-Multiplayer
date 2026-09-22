#include "ac_structure.h"

#include "m_play.h"
#include "m_name_table.h"
#include "m_malloc.h"
#include "m_common_data.h"

static void aSTR_actor_ct(ACTOR* actor, GAME* game);
static void aSTR_actor_dt(ACTOR* actor, GAME* game);
static void aSTR_actor_move(ACTOR* actor, GAME* game);

ACTOR_PROFILE Structure_Profile = { mAc_PROFILE_STRUCTURE,
                                    ACTOR_PART_CONTROL,
                                    ACTOR_STATE_NO_MOVE_WHILE_CULLED | ACTOR_STATE_NO_DRAW_WHILE_CULLED,
                                    EMPTY_NO,
                                    ACTOR_OBJ_BANK_KEEP,
                                    sizeof(STRUCTURE_CONTROL_ACTOR),

                                    &aSTR_actor_ct,
                                    &aSTR_actor_dt,
                                    &aSTR_actor_move,
                                    NONE_ACTOR_PROC,
                                    NULL };

static u8 aSTR_overlay[aSTR_ACTOR_TBL_COUNT][aSTR_OVERLAY_SIZE];
#ifdef TARGET_PC
#include "ac_shrine.h" /* the one structure-actor subclass measured larger than STRUCTURE_ACTOR on x64, see below */

/* Structure profiles such as TOUDAI_ACTOR are larger than STRUCTURE_ACTOR because of Delta time.
 * If you are modding, be careful to not make the same mistake.
 *
 * x64: every structure-actor subclass in the game (all ~45 profiles reachable via aSTR_setupActor_proc's
 * setupInfo_table, checked exhaustively) was measured against sizeof(STRUCTURE_ACTOR). The union below
 * already self-sizes to fit a plain STRUCTURE_ACTOR (its `actor` member), which on x64 is 0x340 (832)
 * bytes -- bigger than the original GameCube-era 0x300 (768) `bytes[]` padding, but that member no longer
 * drives the union's real size, only its lower bound. The ONE subclass that is still bigger than that is
 * SHRINE_ACTOR (STRUCTURE_ACTOR + a trailing f32, padded to 8-byte alignment on x64): 0x348 (840) bytes.
 * Without accounting for it, aSTR_get_actor_area_proc()'s slots are 8 bytes too small for a shrine actor,
 * which silently overwrites the first bytes of the next slot in aSTR_actor_cl[] (corrupting that
 * neighbour's ACTOR::part/npc_id) -- this was the confirmed root cause of the __osFree "invalid free"
 * (0172xxxx) crash. Deriving the slot size from the actual largest known subclass, instead of a fixed
 * literal, keeps this correct if a subclass changes size again. */
#define aSTR_PC_ACTOR_SLOT_SIZE ALIGN_NEXT(sizeof(SHRINE_ACTOR), 16)

typedef union {
    u64 align;
    STRUCTURE_ACTOR actor;
    u8 bytes[aSTR_PC_ACTOR_SLOT_SIZE];
} aSTR_pc_actor_storage_c;

_Static_assert(sizeof(aSTR_pc_actor_storage_c) >= sizeof(SHRINE_ACTOR),
              "aSTR_pc_actor_storage_c must be able to hold the largest structure-actor subclass");
_Static_assert(sizeof(aSTR_pc_actor_storage_c) >= sizeof(STRUCTURE_ACTOR),
              "aSTR_pc_actor_storage_c must be able to hold at least a plain STRUCTURE_ACTOR");

static aSTR_pc_actor_storage_c aSTR_actor_cl[aSTR_ACTOR_TBL_COUNT];

static STRUCTURE_ACTOR* aSTR_pc_actor_slot(int idx) {
    return &aSTR_actor_cl[idx].actor;
}
#else
static STRUCTURE_ACTOR aSTR_actor_cl[aSTR_ACTOR_TBL_COUNT];
#define aSTR_pc_actor_slot(idx) (&aSTR_actor_cl[idx])
#endif

#include "../src/actor/ac_structure_clip.c_inc"

static void aSTR_actor_ct(ACTOR* actor, GAME* game) {
    STRUCTURE_CONTROL_ACTOR* structure = (STRUCTURE_CONTROL_ACTOR*)actor;

    aSTR_init_clip_area();
    structure->str_door_name = Common_Get(door_data).door_actor_name;
    structure->reset = Common_Get(door_data).exit_type;
}

static void aSTR_actor_dt(ACTOR* actor, GAME* game) {
    aSTR_free_clip_area();
}

static void aSTR_check_door_data(STRUCTURE_CONTROL_ACTOR* actor, GAME* game) {
    static int request[2] = { 4, 5 };

    GAME_PLAY* play = (GAME_PLAY*)game;

    if (ITEM_NAME_GET_TYPE(actor->str_door_name) == NAME_TYPE_STRUCT) {
        STRUCTURE_ACTOR* str_actor =
            (STRUCTURE_ACTOR*)Actor_info_fgName_search(&play->actor_info, actor->str_door_name, ACTOR_PART_ITEM);

        if (str_actor != NULL && str_actor->request_type == 0) {
            str_actor->request_type = request[actor->reset == TRUE];
            actor->str_door_name = EMPTY_NO;
        }
    } else {
        actor->str_door_name = EMPTY_NO;
    }
}

static void aSTR_actor_move(ACTOR* actor, GAME* game) {
    STRUCTURE_CONTROL_ACTOR* structure = (STRUCTURE_CONTROL_ACTOR*)actor;
    switch (mFI_GetFieldId()) {
        case mFI_FIELD_FG:
            aSTR_check_door_data(structure, game);
            break;
    }
}
