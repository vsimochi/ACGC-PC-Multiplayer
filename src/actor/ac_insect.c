#include "ac_insect.h"

#include "m_name_table.h"
#include "m_common_data.h"
#include "ac_set_ovl_insect.h"
#include "m_player_lib.h"
#include "m_actor_shadow.h"
#include "libultra/libultra.h"
#include "m_skin_matrix.h"
#include "sys_matrix.h"
#include "m_rcp.h"

#ifdef TARGET_PC
#include "pc_wildlife_authority.h" /* World Ecology Wildlife Sync T4: pcwld_bug_controller_torn_down() --
                                       see aINS_actor_dt()'s own doc below. ADDITIVE only. */
#include "pc_net_game.h"           /* multiplayer latent bugs: pc_net_game_wildlife_tool_action() (aINS_set_pl_act_tim) */
#endif

static aINS_CTRL_ACTOR* aINS_ctrlActor = NULL;
static aINS_overlay_c aINS_overlay;
static aINS_Clip_c aINS_clip;

/* order .bss */
#ifdef MUST_MATCH
BSS_ORDER_GROUP_START
BSS_ORDER_ITEM(aINS_ctrlActor);
BSS_ORDER_ITEM(aINS_overlay);
BSS_ORDER_ITEM(aINS_clip);
BSS_ORDER_GROUP_END
#endif

static void aINS_actor_ct(ACTOR* actorx, GAME* game);
static void aINS_actor_dt(ACTOR* actorx, GAME* game);
static void aINS_actor_move(ACTOR* actorx, GAME* game);
static void aINS_actor_draw(ACTOR* actorx, GAME* game);

// clang-format off
ACTOR_PROFILE Insect_Profile = {
    mAc_PROFILE_INSECT,
    ACTOR_PART_CONTROL,
    ACTOR_STATE_NO_DRAW_WHILE_CULLED | ACTOR_STATE_NO_MOVE_WHILE_CULLED,
    EMPTY_NO,
    ACTOR_OBJ_BANK_KEEP,
    sizeof(aINS_CTRL_ACTOR),
    &aINS_actor_ct,
    &aINS_actor_dt,
    &aINS_actor_move,
    &aINS_actor_draw,
    NULL
};
// clang-format on

static void aINS_position_move(ACTOR* actorx);

#include "../src/actor/ac_insect_data.c_inc"
#include "../src/actor/ac_insect_clip.c_inc"
#include "../src/actor/ac_insect_move.c_inc"
#include "../src/actor/ac_insect_draw.c_inc"

static void aINS_actor_ct(ACTOR* actorx, GAME* game) {
    aINS_CTRL_ACTOR* ctrl_actor = (aINS_CTRL_ACTOR*)actorx;
    aINS_INSECT_ACTOR* insect_actor;
    aINS_overlay_entry_c* overlay_p;
    int i;

    aINS_ctrlActor = ctrl_actor;
    aINS_init_dma_and_clip_area(ctrl_actor);
    insect_actor = ctrl_actor->insect_actor;
    overlay_p = aINS_overlay.entries;

    for (i = 0; i < aINS_ACTOR_NUM; i++) {
        insect_actor->exist_flag = FALSE;
        insect_actor->_254 = 0;
        insect_actor->_258 = 0;
        insect_actor++;
    }

    for (i = 0; i < 3; i++) {
        overlay_p->_1C00 = aINS_PROGRAM_NUM;
        overlay_p->_1C04 = 0;
        overlay_p++;
    }
}

static void aINS_actor_dt(ACTOR* actorx, GAME* game) {
    aINS_free_clip_area();

#ifdef TARGET_PC
    /* World Ecology Wildlife Sync T4 (ordinary bug catching): aINS_ctrlActor (this file's own static
     * global, above) is never reset to NULL here in vanilla, and the single shared aINS_CTRL_ACTOR this
     * pointer names -- together with every one of its aINS_ACTOR_NUM insect_actor[] slots -- is torn
     * down whenever THIS process's own town scene is unloaded (entering a house/shop/museum etc), not
     * only on a full town change. pcwld_bug_local_actor_for_entity()/pcwld_bug_handle_wildlife_despawn()
     * (pc_wildlife_authority.h/.c) hold raw aINS_INSECT_ACTOR* pointers into that same memory in their
     * own bookkeeping (no unused field of aINS_INSECT_ACTOR exists to stamp an entity_id into the way
     * fish are stamped -- see that header's own doc) -- invalidate them here, mirroring ac_gyoei.c's own
     * `aGYO_ctrlActor = NULL` defensive fix for the exact same class of stale-pointer risk. Gated
     * entirely behind TARGET_PC so the non-PC (matching) build is byte-for-byte unaffected; ADDITIVE
     * only, never called from vanilla code. */
    pcwld_bug_controller_torn_down();
    aINS_ctrlActor = NULL;
#endif
}

#ifdef TARGET_PC
/* Multiplayer spirits (G2, pc_wildlife_authority.c): the HOST released the record of an uncaught spirit of acre (bx, bz) that has no live actor any
 * more -- the move to another free acre vanilla's aINS_destruct() would have made (aINS_regist_set_block_table), done now. No-op outside the ghost
 * event (no common area). */
void aINS_pc_spirit_reregister(int bx, int bz) {
    static ACTOR dummy; /* aINS_regist_set_block_table() reads only block_x / block_z (the acre the new one must differ from) */

    if (mEv_get_common_area(mEv_EVENT_GHOST, 55) == NULL) {
        return;
    }
    dummy.block_x = (s8)bx;
    dummy.block_z = (s8)bz;
    aINS_regist_set_block_table(&dummy);
}
#endif
