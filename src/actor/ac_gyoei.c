#include "ac_gyoei.h"

#include "m_common_data.h"
#include "m_actor_shadow.h"
#include "libultra/libultra.h"
#include "m_malloc.h"
#include "sys_matrix.h"
#include "m_skin_matrix.h"
#include "m_rcp.h"
#include "m_player_lib.h"

static void aGYO_actor_ct(ACTOR* actorx, GAME* game);
static void aGYO_actor_dt(ACTOR* actorx, GAME* game);
static void aGYO_actor_move(ACTOR* actorx, GAME* game);
static void aGYO_actor_draw(ACTOR* actorx, GAME* game);

// clang-format off
ACTOR_PROFILE Gyoei_Profile = {
    mAc_PROFILE_GYOEI,
    ACTOR_PART_CONTROL,
    ACTOR_STATE_NO_DRAW_WHILE_CULLED | ACTOR_STATE_NO_MOVE_WHILE_CULLED,
    EMPTY_NO,
    ACTOR_OBJ_BANK_KEEP,
    sizeof(GYOEI_ACTOR),
    &aGYO_actor_ct,
    &aGYO_actor_dt,
    &aGYO_actor_move,
    &aGYO_actor_draw,
    NULL,
};
// clang-format on

static int aGYO_init_dma_and_clip_area(void);
static void aGYO_free_clip_area(void);

static ACTOR* aGYO_ctrlActor = NULL;

typedef struct gyoei_overlay_s {
    u8 buf[0x3C00];
} aGYO_overlay_c ATTRIBUTE_ALIGN(8);

static aGYO_overlay_c aGYO_overlay[aGYO_MAX_GYOEI];

static void aGYO_actor_ct(ACTOR* actorx, GAME* game) {
    GYOEI_ACTOR* gyoei = (GYOEI_ACTOR*)actorx;
    int i;
    aGYO_CTRL_ACTOR* ctrl = gyoei->ctrl;

    for (i = 0; i < aGYO_MAX_GYOEI; i++) {
        ctrl->exist = FALSE;
        ctrl->overlay_p = aGYO_overlay[i].buf;
        ctrl++;
    }

    for (i = 0; i < aGYO_EXIST_MAX; i++) {
        gyoei->exist[i] = FALSE;
        gyoei->segment_type[i] = -1;
    }

    gyoei->logic_accum = 0.0f;

    aGYO_ctrlActor = (ACTOR*)gyoei;
    aGYO_init_dma_and_clip_area();
}

static void aGYO_actor_dt(ACTOR* actorx, GAME* game) {
    GYOEI_ACTOR* gyoei = (GYOEI_ACTOR*)actorx;
    int i;
    aGYO_CTRL_ACTOR* ctrl = gyoei->ctrl;

    for (i = 0; i < aGYO_MAX_GYOEI; i++) {
        if (ctrl->overlay_p != NULL) {
            ctrl->overlay_p = NULL;
        }

        ctrl++;
    }

    aGYO_free_clip_area();

#ifdef TARGET_PC
    /* World Ecology Wildlife Sync T-catch (residual review fix): defensive only -- closes a theoretical
     * use-after-free transition window aGYO_pc_handle_wildlife_despawn()/aGYO_pc_clear_all_entity_stamps()
     * (both PC-only, further down this file) could otherwise hit via the stale aGYO_ctrlActor pointer,
     * e.g. a race between town-field construction and this controller actor's own construction, or an
     * edge-case scene without this controller at all -- pcfa_scene_is_town()'s own gate on those callers
     * does not by itself guarantee this pointer is still valid. Both callers already NULL-check
     * `gyoei`/`aGYO_ctrlActor` before dereferencing it, so this one-line reset is enough. Gated entirely
     * behind TARGET_PC so the non-PC (matching) build is byte-for-byte unaffected. */
    aGYO_ctrlActor = NULL;
#endif
}

#include "../src/actor/ac_gyoei_model.c_inc"

static mActor_proc aGYO_program_dlftbl[] = {
    &aGTT_actor_init,
    &aGKK_actor_init,
};

#include "../src/actor/ac_gyoei_clip.c_inc"
#include "../src/actor/ac_gyoei_move.c_inc"
#include "../src/actor/ac_gyoei_data.c_inc"
#include "../src/actor/ac_gyoei_draw.c_inc"

#ifdef TARGET_PC
/* World Ecology Wildlife Sync T-catch -- see ac_gyoei.h's own doc for both functions' full contract.
 * ADDITIVE only: neither function is ever called from vanilla code, and neither changes any existing
 * vanilla behavior in this file -- gated entirely behind TARGET_PC so the non-PC (matching) build is
 * byte-for-byte unaffected. */
#include "ac_uki.h"

void aGYO_pc_stamp_entity_id(ACTOR* actorx, u32 entity_id) {
    aGYO_CTRL_ACTOR* ctrl = (aGYO_CTRL_ACTOR*)actorx;
    if (actorx == NULL || entity_id == 0) {
        return;
    }
    ctrl->_1F8 = (int)entity_id;
}

u32 aGYO_pc_get_entity_id_stamp(ACTOR* actorx) {
    aGYO_CTRL_ACTOR* ctrl;
    /* T8 audit defensive fix (Finding #6): the audit found every current caller (e.g.
     * Player_actor_setup_main_Notice_rod(), m_player_main_notice_rod.c_inc, passing uki->child_actor)
     * only ever reaches here with a genuine GYOEI actor, so this cast was not actually reachable with the
     * wrong type -- but nothing enforced that, making it fragile against a future caller passing some
     * other ACTOR*. actorx->id is the same ACTOR_PROFILE id field other TARGET_PC call sites already key
     * off of (e.g. `((ACTOR*)label)->id == mAc_PROFILE_BEE`, m_player_main_swing_net.c_inc) -- cheap,
     * always-valid bounds/type check before the reinterpret-cast. */
    if (actorx == NULL || actorx->id != mAc_PROFILE_GYOEI) {
        return 0;
    }
    ctrl = (aGYO_CTRL_ACTOR*)actorx;
    return (u32)ctrl->_1F8;
}

int aGYO_pc_handle_wildlife_despawn(u32 entity_id) {
    GYOEI_ACTOR* gyoei = (GYOEI_ACTOR*)aGYO_ctrlActor;
    aGYO_CTRL_ACTOR* ctrl;
    int i, found = -1;
    UKI_ACTOR* uki = NULL;
    int engaged_by_this_process;

    if (gyoei == NULL || entity_id == 0) {
        return 0;
    }
    ctrl = gyoei->ctrl;
    for (i = 0; i < aGYO_MAX_GYOEI; i++) {
        if (ctrl[i].exist && (u32)ctrl[i]._1F8 == entity_id) {
            found = i;
            break;
        }
    }
    if (found < 0) {
        return 0; /* not materialized locally (already gone, or never seen here) -- nothing to do */
    }

    if (gamePT != NULL) {
        PLAYER_ACTOR* player = GET_PLAYER_ACTOR_GAME((GAME*)gamePT);
        if (player != NULL && player->fishing_rod_actor_p != NULL) {
            uki = (UKI_ACTOR*)player->fishing_rod_actor_p;
        }
    }

    engaged_by_this_process = (uki != NULL && uki->child_actor == (ACTOR*)&ctrl[found]);

    if (!engaged_by_this_process && (ctrl[found].gyo_flags & 2) == 0) {
        /* Case 1: never hooked by THIS process's own bobber -- the same deferred-destroy flag vanilla's
           own aGYO_actor_move() already consumes every tick (ac_gyoei_move.c_inc). */
        ctrl[found].gyo_flags |= 0x20;
    } else if (engaged_by_this_process && uki->gyo_status < 5) {
        /* Case 2: engaged by this process, before aUKI_bite()'s point of no return -- exact same pair
           of writes as vanilla's own bite-timeout escape path (ac_gyo_test.c). */
        uki->gyo_command = 0;
        ctrl[found].gyo_flags |= 0x20;
    } else if (!engaged_by_this_process && (ctrl[found].gyo_flags & 2) != 0) {
        /* Case 3 (Bug D partial fix, post-T3 review): "approaching the bobber" (aGTT_near_init() has set
           gyo_flags & 2), but child_actor is not yet assigned to THIS fish actor -- no bobber references
           it yet, so it is exactly as safe to hand it the same deferred-destroy treatment as Case 1.
           aGYO_actor_move() consumes gyo_flags & 0x20 unconditionally, regardless of gyo_flags & 2, so
           this is not a new destruction path -- same one Case 1/2 already use. Without this, such a fish
           matched neither Case 1 nor Case 2 and was left as a harmless but visually-persisting "ghost"
           until a later, unrelated cull. */
        ctrl[found].gyo_flags |= 0x20;
    }
    /* else: gyo_status >= 5 (engaged AND at/past the point of no return), or the defensive
       engaged-but-flag-mismatch edge case -- leave completely untouched, see this function's own doc. */

    return 1;
}

void aGYO_pc_clear_all_entity_stamps(void) {
    GYOEI_ACTOR* gyoei = (GYOEI_ACTOR*)aGYO_ctrlActor;
    int i;

    if (gyoei == NULL) {
        return;
    }
    for (i = 0; i < aGYO_MAX_GYOEI; i++) {
        gyoei->ctrl[i]._1F8 = 0;
    }
}
#endif /* TARGET_PC */
