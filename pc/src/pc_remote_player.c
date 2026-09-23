/* pc_remote_player.c - Stage 2/3/4A/4B: visible, movement-synchronized, real-model, animated
 * representation of a connected remote player.
 *
 * See pc_remote_player.h for scope. Design notes carried forward from Stage 2/3 (see those
 * investigations for the full reasoning -- summarized here so this file is self-contained):
 *
 *   - Actor part: ACTOR_PART_UNUSED, not ACTOR_PART_PLAYER. get_player_actor_withoutCheck()
 *     reads actor_info->list[ACTOR_PART_PLAYER].actor[0], and Actor_info_part_new() prepends new
 *     actors to a part's list -- putting a remote player in ACTOR_PART_PLAYER risks it being
 *     picked up as *the* local player by that lookup. ACTOR_PART_UNUSED is an existing,
 *     correctly-sized Actor_info list slot whose only other reader in the whole codebase is
 *     ac_insect_move.c_inc's insect "stress" calculation, which just treats anything in it like a
 *     nearby moving NPC/player for scaring insects away -- a harmless, even mildly thematic, side
 *     effect for an actor that stands in for another player.
 *   - Allocation: Actor_malloc_actor_class() dispatches purely on ITEM_NAME_GET_TYPE(name_id), not
 *     on `part`. NAME_TYPE_PAD15 (m_name_table.h) is an unused decomp name-type value that falls
 *     through to the `default:` case there -- a plain zelda_malloc(), bypassing both the 9-slot
 *     NPC pool and the 32-slot structure pool entirely. No pool sizes or dispatch logic change.
 *   - Culling/persistence: ACTOR_STATE_NO_MOVE_WHILE_CULLED | ACTOR_STATE_NO_DRAW_WHILE_CULLED in
 *     the profile's initial_flags_state is the exact, already-proven precedent ac_structure.c uses
 *     (Structure_Profile) so an actor is never skipped by Actor_delete_check()'s block-distance
 *     deletion, without touching global culling behavior.
 *   - Creation pipeline: pc_actor_make_from_profile() (src/game/m_actor.c, TARGET_PC-only) runs
 *     the exact same allocate/init/link/construct sequence as Actor_info_make_actor(), just taking
 *     an ACTOR_PROFILE/ACTOR_DLFTBL pair directly instead of indexing the decomp's fixed
 *     actor_dlftbls[] table (which has no slot for a PC-only actor type).
 *   - Destruction: Actor_delete() only nulls mv_proc/dw_proc; the actor system reaps the memory
 *     later during its normal per-frame sweep. This file never frees an ACTOR directly.
 *   - Position: NOT network-synchronized *from* this file -- it comes from Stage 3's delayed
 *     interpolation over the per-peer snapshot ring buffer, unchanged in Stage 4A.
 *
 * Stage 4A replaces the flat-colored pyramid marker with the actual player skeleton/model,
 * reusing the same exported mPlib_ and cKF_ pipeline the inventory-overlay player preview uses
 * (src/game/m_inventory_ovl.c's mIV_pl_shape_init/mIV_pl_shape_draw) -- i.e. composition, not a
 * fake PLAYER_ACTOR: none of Player_actor_ct, Player_actor_move, the per-state main functions, or
 * the CulcAnimation helpers is ever called, no controller is read, and every mPlib_/cKF_ symbol
 * used here is already declared extern in m_player_lib.h / c_keyframe.h. See
 * pc_remote_player_visual_init()/pc_remote_player_dw() below for exactly which calls are made and
 * why each is safe to make independently of the local player's own state machine.
 *
 * Stage 4B drives that skeleton through IDLE/WALK/RUN/DASH using the already-synchronized Stage 3
 * move_state/speed fields -- no protocol change. It reuses the real player's own verified
 * animation-selection and playback-speed formulas (see pc_remote_player_mv()'s Stage 4B block for
 * exact citations); AIRBORNE/TUMBLE/ITEM_USE/OTHER, held items, and appearance sync are explicitly
 * out of scope and fall back to WAIT1.
 */
#include "pc_remote_player.h"

#include "m_actor.h"
#include "m_play.h"
#include "m_player_lib.h"
#include "m_name_table.h"
#include "m_needlework.h" /* Stage 4C-1: mNW_original_design_c, mNW_CopyOriginalTexture(),
                            * mNW_CopyOriginalPalette() -- see pc_remote_player_apply_appearance() */
#include "m_common_data.h" /* Common_Get(player_actor_exists), Now_Private -- see
                             * pc_remote_player_visual_init()'s readiness gate */
#include "m_rcp.h"
#include "libultra/libultra.h"
#include "pc_lowaddr.h" /* PC_LOWADDR_LIMIT -- see the guard in pc_remote_player_poll() */
#include "audio.h" /* Stage 4B.1: sAdo_OngenTrgStart() -- see the TURN_DASH skid sound in
                     * pc_remote_player_mv(). Already #include'd elsewhere in pc/ (e.g.
                     * pc_nes_fixnes.c) and compiles cleanly in the PC build. */

#include <math.h> /* sqrtf() -- see the Stage 4B animation-speed formula in pc_remote_player_mv() */
#include <string.h>
#include <stdio.h>

/* Same two decomp-owned skeleton globals m_player_lib.c's own mPlib_get_player_mdl_p() selects
 * between (src/data/model/boy_model.c / girl_model.c) -- declared extern here purely for the
 * diagnostic model-name print below; the actual selection always goes through
 * mPlib_get_player_mdl_p() itself, never this pointer directly. */
extern cKF_Skeleton_R_c cKF_bs_r_boy_1;
extern cKF_Skeleton_R_c cKF_bs_r_grl_1;

/* Stage 3: one extra slot past the real 0..PC_NET_MAX_PEERS-1 client-peer-id range, reserved for
 * the host itself (PC_NETGAME_HOST_PLAYER_ID == PC_NET_MAX_PEERS) -- see pc_net_game.h's doc on
 * PCNetPlayerId for why the host needs a slot here despite having no PCNetPeerId of its own. */
#define PC_REMOTE_PLAYER_SLOT_COUNT (PC_NET_MAX_PEERS + 1)

/* Stage 3 tuning constants (named so they're easy to find and retune later). */
#define PC_REMOTE_PLAYER_SNAPSHOT_COUNT 4        /* ring size per peer; 2 is the true minimum for
                                                   * interpolation, 4 gives slack against jitter */
#define PC_REMOTE_PLAYER_INTERP_DELAY_FRAMES 9.0 /* ~150ms at 60fps-equivalent units */
#define PC_REMOTE_PLAYER_TIMEOUT_FRAMES 300.0     /* ~5s; matches pc_net.c's own PCNET_TIMEOUT_MS,
                                                    * used only for relay-discovered peers (see
                                                    * pc_remote_player_on_move()) that have no
                                                    * direct PC_NET_EVENT_PEER_DISCONNECTED */
#define PC_REMOTE_PLAYER_TELEPORT_DIST_SQ (400.0f * 400.0f) /* squared world units between two
                                                              * consecutive snapshots; farther than
                                                              * this is treated as a teleport/
                                                              * loading-zone jump, not a slide */

/* Stage 4C-1: the local player's own face-texture bank is exactly 8 eye slots + 6 mouth slots at
 * 256 bytes each (mPlayer_EYE_TEX_NUM/mPlayer_MOUTH_TEX_NUM, include/m_player.h) -- verified via
 * src/game/m_player_lib.c's mPlib_change_player_face() (a bare 0xE00 literal there; no named
 * decomp macro exists for it, so this file names its own copy). Every remote player's own face-tex
 * buffer uses this exact same layout, filled by mPlib_Load_FaceTexAndPallet(). */
#define PC_REMOTE_PLAYER_FACE_TEX_SIZE 0xE00

/* Stage 4A: a remote player's own PC-owned skeleton/animation state, mirroring exactly what
 * src/game/m_inventory_ovl.c's mIV_pl_shape_init()/mIV_pl_shape_draw() already do for the
 * inventory's player preview -- two combine-played keyframe layers sharing one joint/morph work
 * buffer pair, plus a part table. Every field here is per-instance (never shared between remote
 * players); the skeleton/animation data each keyframe *points to* (cKF_bs_r_boy_1/grl_1, the
 * mPlib_Get_Pointer_Animation() table) is shared, read-only, compiled-in decomp data. */
typedef struct PCRemotePlayerVisual {
    int                   initialized; /* 0 until pc_remote_player_visual_init() has run */
    cKF_SkeletonInfo_R_c  keyframe0;   /* lower-body/main animation layer */
    cKF_SkeletonInfo_R_c  keyframe1;   /* upper-body/item animation layer (kept in lockstep with
                                        * keyframe0 -- see pc_remote_player_mv()'s Stage 4B block) */
    s_xyz                 joint_data[mPlayer_JOINT_NUM + 1];  /* shared by both layers, matching
                                                                * PLAYER_ACTOR's own layout */
    s_xyz                 morph_data[mPlayer_JOINT_NUM + 1];
    s8                    part_table[mPlayer_JOINT_NUM + 1];
    int                   current_anim_idx; /* Stage 4B: mPlayer_ANIM_* currently playing on both
                                              * layers -- per-instance, never static/shared (see
                                              * pc_remote_player_mv()). Zero-initialized by
                                              * Actor_init_actor_class()'s mem_clear at creation,
                                              * which is exactly mPlayer_ANIM_WAIT1's value (0), so
                                              * a freshly (re)created actor already starts correctly
                                              * "on WAIT1" without needing an explicit reset here. */
} PCRemotePlayerVisual;

/* A remote-player actor is just the generic ACTOR base plus which network player it stands in
 * for, the last-interpolated cosmetic values Stage 3 already tracked, and (new in Stage 4A) its
 * own player visual state. ACTOR must be the first member: the whole actor pipeline
 * (Actor_init_actor_class, Actor_info_part_new, generic mv_proc/dw_proc dispatch, ...) only ever
 * knows about ACTOR*. */
typedef struct PCRemotePlayerActor {
    ACTOR                 actor_class;
    PCNetPlayerId         peer;
    float                 cosmetic_speed;      /* last-interpolated speed; Stage 4B uses this to
                                                 * drive WALK/RUN/DASH animation playback speed */
    uint8_t               cosmetic_move_state; /* last-interpolated PCMoveState; Stage 4B uses this
                                                 * to select the IDLE/WALK/RUN/DASH animation */
    int8_t                cosmetic_item_kind;  /* last-interpolated item_kind; stored correctly,
                                                 * but Stage 4A does not render a held item */
    PCRemotePlayerVisual  visual;
} PCRemotePlayerActor;

typedef struct PCRemotePlayerOffset {
    f32 x, z;
} PCRemotePlayerOffset;

/* Small fixed per-peer spawn offsets (world units, relative to the local player), used only as
 * the actor's initial spawn position before any real movement data has arrived (see
 * pc_remote_player_mv()) -- not a layout with any other meaning. Sized/indexed like s_slots. */
static const PCRemotePlayerOffset s_offset_table[PC_REMOTE_PLAYER_SLOT_COUNT] = {
    { 60.0f, 0.0f },   { -60.0f, 0.0f },  { 0.0f, 60.0f },   { 0.0f, -60.0f },
    { 60.0f, 60.0f },  { -60.0f, 60.0f }, { 60.0f, -60.0f }, { -60.0f, -60.0f },
    { 0.0f, 100.0f },
};

/* One accepted movement sample, timestamped in THIS process's own local clock -- never the
 * sender's. See pc_net_game.h's PCNetMoveSample doc for why sender_frame itself is not stored
 * here past the per-slot dedup check (pc_remote_player_on_move() consumes it immediately). */
typedef struct PCRemoteMoveSnapshot {
    double  recv_local_frame; /* graph_dt_frame_time() at the moment this sample was accepted */
    float   pos_x, pos_y, pos_z;
    int16_t facing_angle;
    float   speed;
    uint8_t move_state;
    int8_t  item_kind;
} PCRemoteMoveSnapshot;

/* Stage 4C-1: raw appearance data as received over the network for this remote player (see
 * PCNetPlayerAppearance, pc_net_game.h). Lives in the slot -- not the ACTOR-embedded `visual` --
 * for exactly the same reason `identity` and the snapshot ring do: it is a function of the network
 * message, not of any ACTOR, so it must survive scene transitions (Actor_info_dt() sweeping the
 * ACTOR away) untouched. See pc_remote_player_on_appearance(). */
typedef struct PCRemotePlayerAppearance {
    int      valid;              /* 0 until pc_remote_player_on_appearance() has been called */
    int      pending_resolve;    /* 1 whenever these raw fields hold data that resolved_appearance
                                   * does not yet (or no longer) reflect. Set unconditionally by
                                   * pc_remote_player_on_appearance() on every arrival, cleared only
                                   * by a SUCCESSFUL pc_remote_player_apply_appearance() (one that
                                   * ran with gamePT != NULL). Deliberately independent of
                                   * resolved_appearance.ready: `ready` can still be 1 from an
                                   * earlier, different appearance, so pc_remote_player_poll()'s
                                   * retry must key off THIS flag, never "!ready" -- see
                                   * pc_remote_player_apply_appearance()'s doc comment. */
    uint8_t  gender;              /* mirrors Private_c::gender (mPr_SEX_MALE/FEMALE) */
    uint8_t  face;                 /* mirrors Private_c::face (mPr_FACE_TYPE0..7) */
    uint8_t  sunburn_rank;         /* mirrors Private_c::sunburn.rank (0-8) */
    uint8_t  is_custom_design;     /* 1 if cloth_item is the RSV_CLOTH "one of my own designs" sentinel */
    uint16_t cloth_item;           /* mirrors Private_c::cloth.item (mActor_name_t) */
    mNW_original_design_c design;  /* only meaningful when is_custom_design; the real decomp type
                                     * (verified POD, no pointers) -- reused directly by
                                     * mNW_CopyOriginalTexture()/mNW_CopyOriginalPalette() below,
                                     * rather than hand-copied field by field. */
} PCRemotePlayerAppearance;

/* Stage 4C-1: this player's fully-resolved, render-ready appearance resources -- built once by
 * pc_remote_player_apply_appearance() when `appearance` above first arrives (Stage 4C-2 will
 * re-invoke it on live changes; Stage 4C-1 only ever calls it once). Never touched per-frame or on
 * scene transitions -- like `appearance`, this lives in the slot specifically so a recreated ACTOR
 * (see pc_remote_player_poll()'s scene-generation handling) never needs to rebuild it, only to
 * re-point its own keyframe skeleton at `skeleton` below (see pc_remote_player_visual_init()).
 *
 * face_tex/face_pallet/cloth_tex/cloth_pallet are each a DMA destination for
 * _JW_GetResourceAram() (see pc_remote_player_apply_appearance(), mPlib_Load_FaceTexAndPallet(),
 * mPlib_Load_PlayerTexAndPallet()), which -- like every other ARAM/DMA destination in the original
 * decomp (mNW_original_tex_c, include/m_needlework.h; OthersSave_c's keep_mail/keep_original/
 * keep_diary members, include/m_card.h) -- requires 32-byte alignment. `ready`/`skeleton` above are
 * ordinary, unaligned fields, so each buffer below gets its OWN ATTRIBUTE_ALIGN(32) rather than
 * relying on the struct's own (whole-struct) alignment: aligning only the struct type would still
 * leave each individual member at whatever offset the preceding fields happen to add up to (here,
 * 16 bytes past a 32-byte boundary from `ready`+padding+`skeleton`) -- exactly what let this slip
 * through unnoticed until a live runtime appearance test actually exercised the ARAM path (see the
 * _Static_assert block right after the struct, which now catches this at compile time instead).
 * This mirrors OthersSave_c's own established pattern (include/m_card.h) of mixing ordinary fields
 * with several independently ATTRIBUTE_ALIGN(32)'d members in one struct -- the compiler inserts
 * whatever padding each one individually needs. */
typedef struct PCRemotePlayerResolvedAppearance {
    int                ready;                         /* 0 until fully resolved */
    cKF_Skeleton_R_c*  skeleton;                       /* &cKF_bs_r_boy_1 or &cKF_bs_r_grl_1 --
                                                         * shared, read-only, compiled-in */
    u8                 face_tex[PC_REMOTE_PLAYER_FACE_TEX_SIZE] ATTRIBUTE_ALIGN(32);  /* own copy:
                                                                    * 8 eye slots then 6 mouth
                                                                    * slots, 256B each -- the exact
                                                                    * layout mPlib_Get_eye_tex_p()/
                                                                    * mPlib_Get_mouth_tex_p() slice
                                                                    * out of the (singleton) local
                                                                    * bank */
    u16                face_pallet[mNW_PALETTE_COUNT] ATTRIBUTE_ALIGN(32);
    u8                 cloth_tex[mNW_DESIGN_TEX_SIZE] ATTRIBUTE_ALIGN(32);
    u16                cloth_pallet[mNW_PALETTE_COUNT] ATTRIBUTE_ALIGN(32);
} PCRemotePlayerResolvedAppearance;

/* Compile-time proof, not just an assumption from the ATTRIBUTE_ALIGN annotations above -- verifies
 * the actual resulting offset of each DMA-destination member is a multiple of 32, exactly what
 * checkOkAddress()/JKR_ISALIGNED32() (src/static/JSystem/JKernel/JKRAram.cpp,
 * src/static/JSystem/JKernel/JKRAramPiece.cpp) require of any address passed through
 * _JW_GetResourceAram(). This is a per-member check because a struct instance's own base address
 * (s_slots[], a static array -- see below) is not otherwise known to be 32-byte aligned by any
 * other guarantee in this file. */
_Static_assert(offsetof(PCRemotePlayerResolvedAppearance, face_tex) % 32 == 0,
              "PCRemotePlayerResolvedAppearance.face_tex is not 32-byte aligned -- ARAM DMA requires it");
_Static_assert(offsetof(PCRemotePlayerResolvedAppearance, face_pallet) % 32 == 0,
              "PCRemotePlayerResolvedAppearance.face_pallet is not 32-byte aligned -- ARAM DMA requires it");
_Static_assert(offsetof(PCRemotePlayerResolvedAppearance, cloth_tex) % 32 == 0,
              "PCRemotePlayerResolvedAppearance.cloth_tex is not 32-byte aligned -- ARAM DMA requires it");
_Static_assert(offsetof(PCRemotePlayerResolvedAppearance, cloth_pallet) % 32 == 0,
              "PCRemotePlayerResolvedAppearance.cloth_pallet is not 32-byte aligned -- ARAM DMA requires it");

typedef struct PCRemotePlayerSlot {
    int                in_use;             /* tracked at all (pending creation, or actor already live) */
    int                pending_create;     /* READY/discovered but actor creation hasn't succeeded yet */
    int                lazily_discovered;  /* 1 if learned about purely via a relayed movement sample
                                             * (no direct on_ready()/on_disconnect() for this one --
                                             * see pc_remote_player_on_move()); only such slots are
                                             * ever cleaned up by the liveness timeout in poll() */
    ACTOR*             actor;              /* NULL until pc_actor_make_from_profile() succeeds */
    uint32_t           scene_generation;   /* s_scene_generation at the moment `actor` was created --
                                             * see pc_remote_player_poll()'s staleness check. Meaningless
                                             * while actor == NULL. */
    PCNetGameIdentity  identity;           /* captured at READY, if any; not yet displayed anywhere */

    int                has_sender_frame;
    uint32_t           newest_sender_frame; /* highest PCNetMoveMsg.frame accepted from this sender */
    double             last_move_recv_local_frame; /* for the lazily_discovered timeout */

    PCRemoteMoveSnapshot snapshots[PC_REMOTE_PLAYER_SNAPSHOT_COUNT];
    int                  snapshot_count; /* 0..PC_REMOTE_PLAYER_SNAPSHOT_COUNT, valid entries */
    int                  snapshot_head;  /* index the NEXT snapshot will be written to */

    PCRemotePlayerAppearance         appearance;          /* Stage 4C-1: see pc_remote_player_on_appearance() */
    PCRemotePlayerResolvedAppearance resolved_appearance; /* Stage 4C-1: see pc_remote_player_apply_appearance() */
} PCRemotePlayerSlot;

static PCRemotePlayerSlot s_slots[PC_REMOTE_PLAYER_SLOT_COUNT];

static ACTOR_PROFILE s_remote_player_profile;
static ACTOR_DLFTBL  s_remote_player_dlftbl;
static int           s_profile_ready = 0;

/* Stage 4A lifecycle fix: title<->town<->house transitions each fully destroy and reconstruct the
 * whole GAME_PLAY object (Actor_info_dt() unconditionally sweeps and frees every actor in every
 * part list, including ACTOR_PART_UNUSED -- see src/game/m_actor.c -- immediately followed by a
 * fresh Actor_info_ct() for the newly-allocated GAME_PLAY; see src/game/m_play.c's
 * play_cleanup()/play_init()). Any ACTOR* created in the old scene is gone -- not just unlinked,
 * genuinely freed -- once that happens; Actor_delete() must never be called on it again.
 *
 * Detecting this by watching for an observable `gamePT == NULL` window (game_dt() clears it at
 * the very end of tearing down the old scene, before the next game_ct() sets it to the new one)
 * was tried and found unreliable by live testing: the free-old/malloc-new/construct-new sequence
 * (src/graph.c's graph_proc()) can complete between two consecutive pc_remote_player_poll() calls
 * without this code ever observing gamePT as NULL in between -- confirmed live, where a real
 * play_cleanup()/play_init() cycle (visible via mCD_toNextLand()'s own log line) produced a stale
 * dw_proc/mv_proc read one second later with zeroed-then-poisoned field values, despite gamePT
 * never appearing NULL to this poll loop.
 *
 * Instead, every poll() call while gamePT is non-NULL directly compares the CURRENT GAME_PLAY
 * object's own identity against what was last observed: its pointer value, and its
 * `frame_counter` (reset to 0 by game_ct() for every newly constructed GAME, src/game.c). Either
 * one changing unexpectedly -- a different pointer, or a frame_counter that went backwards --
 * proves the object was replaced, even in the case a straight pointer comparison alone would miss
 * (the allocator handing back the exact same address for the new GAME_PLAY). */
static uint32_t s_scene_generation = 0;
static GAME*    s_last_seen_game = NULL;
static uint32_t s_last_seen_frame_counter = 0;

static void pc_remote_player_mv(ACTOR* actor, GAME* game);
static void pc_remote_player_dw(ACTOR* actor, GAME* game);

static PCRemotePlayerSlot* pc_remote_player_get_slot(PCNetPlayerId player_id) {
    if (player_id < 0 || player_id >= PC_REMOTE_PLAYER_SLOT_COUNT) {
        return NULL;
    }
    return &s_slots[player_id];
}

/* The result of interpolating (or falling back on) a peer's snapshot buffer, ready to apply
 * directly to an ACTOR. See pc_remote_player_interpolate(). */
typedef struct PCRemotePlayerRenderState {
    xyz_t   pos;
    s16     angle;
    float   speed;
    uint8_t move_state;
    int8_t  item_kind;
} PCRemotePlayerRenderState;

/* Delayed two-snapshot interpolation. `target_time` and every snapshot's recv_local_frame are all
 * in THIS process's own graph_dt_frame_time() domain -- see pc_net_game.h's PCNetMoveSample doc
 * for why that matters (the sender's own frame counter never appears here at all; it was already
 * consumed by pc_remote_player_on_move()'s dedup check).
 *
 * Returns 0 if the slot has no snapshots yet (caller should leave the actor's current
 * position/facing untouched -- e.g. its spawn position from creation time). Otherwise fills *out
 * and returns 1: interpolated between straddling snapshots, held at the last known one if
 * target_time is past all of them (packet loss/stall -- never extrapolated), held at the earliest
 * one if target_time is before all of them (peer just appeared), or snapped straight to the
 * newer position if the gap between two consecutive snapshots is implausibly large (teleport/
 * loading-zone jump, not something to slide across). */
static int pc_remote_player_interpolate(const PCRemotePlayerSlot* slot, double target_time,
                                         PCRemotePlayerRenderState* out) {
    int oldest, i, n;
    const PCRemoteMoveSnapshot* s0 = NULL;
    const PCRemoteMoveSnapshot* s1 = NULL;

    n = slot->snapshot_count;
    if (n == 0) {
        return 0;
    }

    oldest = (slot->snapshot_head - n + PC_REMOTE_PLAYER_SNAPSHOT_COUNT) % PC_REMOTE_PLAYER_SNAPSHOT_COUNT;
    for (i = 0; i < n; i++) {
        int idx = (oldest + i) % PC_REMOTE_PLAYER_SNAPSHOT_COUNT;
        const PCRemoteMoveSnapshot* snap = &slot->snapshots[idx];
        if (snap->recv_local_frame <= target_time) {
            s0 = snap; /* keep advancing -- we want the LATEST one that's still <= target_time */
        } else if (s1 == NULL) {
            s1 = snap; /* the FIRST one that's > target_time */
        }
    }

    if (s0 != NULL && s1 != NULL) {
        double span = s1->recv_local_frame - s0->recv_local_frame;
        float t = (span > 0.0) ? (float)((target_time - s0->recv_local_frame) / span) : 1.0f;
        float dx = s1->pos_x - s0->pos_x;
        float dy = s1->pos_y - s0->pos_y;
        float dz = s1->pos_z - s0->pos_z;
        s16 angle_delta;

        if (t < 0.0f) t = 0.0f;
        if (t > 1.0f) t = 1.0f;

        if ((dx * dx + dy * dy + dz * dz) > PC_REMOTE_PLAYER_TELEPORT_DIST_SQ) {
            /* This straddling pair (s0, s1) stays the same across every frame for as long as
             * target_time remains between them (up to ~PC_REMOTE_PLAYER_INTERP_DELAY_FRAMES) --
             * log it once per distinct pair, not once per frame. */
            static const PCRemoteMoveSnapshot* s_last_logged_s1 = NULL;
            if (s1 != s_last_logged_s1) {
                printf("[NET][REMOTE][DIAG] teleport snap: dist_sq=%.1f (%.1f,%.1f,%.1f) -> (%.1f,%.1f,%.1f)\n",
                       (double)(dx * dx + dy * dy + dz * dz), s0->pos_x, s0->pos_y, s0->pos_z, s1->pos_x, s1->pos_y,
                       s1->pos_z);
                s_last_logged_s1 = s1;
            }
            out->pos.x = s1->pos_x;
            out->pos.y = s1->pos_y;
            out->pos.z = s1->pos_z;
        } else {
            out->pos.x = s0->pos_x + dx * t;
            out->pos.y = s0->pos_y + dy * t;
            out->pos.z = s0->pos_z + dz * t;
        }

        /* Shortest-path angle interpolation: (s16)(a - b) wraps correctly at the +/-32768
         * boundary (the exact idiom already used elsewhere in this codebase, e.g.
         * Actor_player_look_direction_check() in src/game/m_actor.c) -- a naive linear lerp of
         * the raw values would rotate the long way around whenever the two angles straddle 0. */
        angle_delta = (s16)(s1->facing_angle - s0->facing_angle);
        out->angle = (s16)(s0->facing_angle + (s16)((f32)angle_delta * t));
        out->speed = s0->speed + (s1->speed - s0->speed) * t;
        out->move_state = s1->move_state;
        out->item_kind = s1->item_kind;
    } else if (s0 != NULL) {
        out->pos.x = s0->pos_x;
        out->pos.y = s0->pos_y;
        out->pos.z = s0->pos_z;
        out->angle = s0->facing_angle;
        out->speed = s0->speed;
        out->move_state = s0->move_state;
        out->item_kind = s0->item_kind;
    } else { /* s1 != NULL */
        out->pos.x = s1->pos_x;
        out->pos.y = s1->pos_y;
        out->pos.z = s1->pos_z;
        out->angle = s1->facing_angle;
        out->speed = s1->speed;
        out->move_state = s1->move_state;
        out->item_kind = s1->item_kind;
    }

    return 1;
}

static void pc_remote_player_init_profile(void) {
    if (s_profile_ready) {
        return;
    }

    memset(&s_remote_player_profile, 0, sizeof(s_remote_player_profile));
    s_remote_player_profile.id = -1;
    s_remote_player_profile.part = ACTOR_PART_UNUSED;
    s_remote_player_profile.initial_flags_state = ACTOR_STATE_NO_MOVE_WHILE_CULLED | ACTOR_STATE_NO_DRAW_WHILE_CULLED;
    s_remote_player_profile.npc_id = 0;
    s_remote_player_profile.obj_bank_id = -1;
    s_remote_player_profile.class_size = sizeof(PCRemotePlayerActor);
    s_remote_player_profile.ct_proc = none_proc2;
    s_remote_player_profile.dt_proc = none_proc2;
    s_remote_player_profile.mv_proc = pc_remote_player_mv;
    s_remote_player_profile.dw_proc = pc_remote_player_dw;
    s_remote_player_profile.sv_proc = NULL;

    memset(&s_remote_player_dlftbl, 0, sizeof(s_remote_player_dlftbl));
    s_remote_player_dlftbl.profile = &s_remote_player_profile;

    s_profile_ready = 1;
}

/* Stage 4C-1: resolve this slot's raw `appearance` into `resolved_appearance`'s render-ready
 * buffers. Invoked every time new appearance data arrives (see pc_remote_player_on_appearance())
 * and, if that attempt was deferred, retried from pc_remote_player_poll() once resources become
 * available -- see the gamePT gate and `pending_resolve` below. Never per-frame otherwise, and
 * never again on a scene transition (the resolved buffers live in the slot, not the ACTOR, so a
 * recreated ACTOR needs no rebuilding here, only re-pointing its own keyframe skeleton -- see
 * pc_remote_player_visual_init()).
 *
 * Never touches Now_Private or any Object_Exchange_c bank -- every resource is written directly
 * into this slot's own buffers via explicit-parameter decomp helpers (see the Stage 4C
 * investigation): mPlib_Load_FaceTexAndPallet() for the face (a new, small, additive decomp
 * function -- src/game/m_player_lib.c -- that mirrors mPlib_change_player_face()/
 * mPlib_Get_UseFacePalletRom_p()'s existing logic with explicit parameters instead of Now_Private
 * reads), mPlib_Load_PlayerTexAndPallet() (already exported) for catalog clothing, and
 * mNW_CopyOriginalTexture()/mNW_CopyOriginalPalette() (already exported) for a custom design.
 *
 * gamePT gate (real-client crash fix): mPlib_Load_FaceTexAndPallet()/mPlib_Load_PlayerTexAndPallet()
 * both eventually call JW_GetAramAddress(), which dereferences forest_arc_aram_p -- mounted only by
 * JW_Init2() (src/static/boot.c, src/static/jsyswrap.cpp). An appearance message can be received
 * and dispatched here (via pc_net_game_poll(), called unconditionally every VIWaitForRetrace())
 * during the game's one-time early boot sequence, strictly before JW_Init2() has run -- confirmed
 * live via GDB as a NULL-`this` crash inside JKRArchive::findTypeResource(). gamePT is the
 * established, already-relied-upon signal that is only ever non-NULL after JW_Init2() has already
 * completed (see pc_remote_player_poll()'s own gate), so it is read here purely as a readiness
 * check -- never written, and Now_Private is still never touched, so this is a deferral, not a
 * behavior change. mNW_CopyOriginalTexture()/mNW_CopyOriginalPalette() never touch ARAM (a plain
 * bcopy() from the wire-provided design bytes), so they are not actually at risk, but are kept
 * behind the same gate for simplicity: this function either fully resolves an appearance or leaves
 * it exactly as it was, never half of one.
 *
 * If gamePT is NULL, the raw `appearance` fields (already updated by the caller) are left
 * untouched and `a->pending_resolve` (set by every caller before invoking this -- see
 * pc_remote_player_on_appearance()) is left set, so pc_remote_player_poll() retries this same slot
 * once gamePT becomes non-NULL -- see its own comment. `pending_resolve`, not
 * resolved_appearance.ready, is the retry condition specifically because `ready` can already be 1
 * from a PREVIOUS, different, successful resolution: a slot whose appearance changes again while
 * gamePT is momentarily NULL must not be mistaken for "up to date" just because it was ready
 * before. */
static void pc_remote_player_apply_appearance(PCRemotePlayerSlot* slot) {
    PCRemotePlayerAppearance* a = &slot->appearance;
    PCRemotePlayerResolvedAppearance* r = &slot->resolved_appearance;
    int face;
    int sunburn_rank;

    if (gamePT == NULL) {
        return; /* deferred -- see doc comment above; pc_remote_player_poll() retries */
    }

    face = a->face;
    sunburn_rank = a->sunburn_rank;

    /* Defensive clamps: a malformed/out-of-range network value must never reach ROM-address
     * arithmetic. Gender needs no clamp -- mPlib_get_player_mdl_p()'s own real logic already
     * treats "anything not mPr_SEX_MALE" as female, so any value is inherently safe here too. */
    if (face < 0 || face >= mPr_FACE_TYPE_NUM) {
        face = 0;
    }
    if (sunburn_rank < 0 || sunburn_rank > mPr_SUNBURN_RANK8) {
        sunburn_rank = 0;
    }

    r->skeleton = (a->gender == mPr_SEX_MALE) ? &cKF_bs_r_boy_1 : &cKF_bs_r_grl_1;

    mPlib_Load_FaceTexAndPallet(r->face_tex, r->face_pallet, a->gender, face, sunburn_rank, FALSE, FALSE);

    memset(r->cloth_tex, 0, sizeof(r->cloth_tex));
    memset(r->cloth_pallet, 0, sizeof(r->cloth_pallet));

    if (a->is_custom_design) {
        /* Custom design: the pixel data travels over the network verbatim (it is arbitrary,
         * player-drawn content that cannot be derived from an id alone -- see the Stage 4C
         * investigation). mPlib_Load_PlayerTexAndPallet() must NOT be called here: its
         * custom-design branch reads Now_Private->my_org[], which is the LOCAL player's own
         * designs, not this remote player's. mNW_CopyOriginalTexture()/mNW_CopyOriginalPalette()
         * (src/game/m_needlework.c) take the design record directly and need no Now_Private
         * access at all. */
        mNW_CopyOriginalTexture(r->cloth_tex, &a->design);
        mNW_CopyOriginalPalette(r->cloth_pallet, &a->design);
    } else {
        /* Catalog item: every instance already has the same compiled-in catalog ROM data, so only
         * the 2-byte item id needs to have been sent -- resolve it locally via the exact same
         * pure, Now_Private-independent helper the original game uses. */
        mPr_cloth_c scratch_cloth;
        mPlib_change_player_cloth_info(&scratch_cloth, (mActor_name_t)a->cloth_item);
        mPlib_Load_PlayerTexAndPallet(r->cloth_tex, r->cloth_pallet, scratch_cloth.idx);
    }

    r->ready = 1;
    a->pending_resolve = 0;
}

/* Stage 4A: bring up a remote visual's skeleton/animation state. Mirrors
 * src/game/m_inventory_ovl.c's mIV_pl_shape_init() exactly: construct BOTH keyframe layers
 * against the SAME joint_data/morph_data buffers (this is what lets
 * cKF_SkeletonInfo_R_combine_play() later merge the two layers' contributions per part_table),
 * copy the "normal" part table row, then init both layers to a standing, looping WAIT1 pose.
 *
 * Every symbol called here is declared extern in m_player_lib.h / c_keyframe.h -- no PLAYER_ACTOR
 * is created, no Player_actor_ct, per-state main function, or CulcAnimation helper runs, no
 * controller is read.
 *
 * Stage 4C-1: the skeleton now comes from this remote player's OWN resolved appearance
 * (`appearance->skeleton`, selected from its own synchronized gender -- see
 * pc_remote_player_apply_appearance()) instead of mPlib_get_player_mdl_p() (the LOCAL player's
 * singleton). The caller (pc_remote_player_mv()) only invokes this once `appearance->ready` is
 * already true, so `appearance` is guaranteed valid here.
 *
 * Safety: called only once player readiness has already been confirmed by the caller (see the
 * player_actor_exists gate in pc_remote_player_mv()). Still additionally guarded here against
 * Now_Private being NULL -- belt-and-suspenders, since player_actor_exists should not be TRUE
 * without a loaded save, but this file never assumes that without checking. */
static void pc_remote_player_visual_init(PCRemotePlayerVisual* visual,
                                         const PCRemotePlayerResolvedAppearance* appearance) {
    cKF_Animation_R_c* wait_anim;

    if (Now_Private == NULL) {
        return; /* not actually ready despite the caller's check -- retry next frame */
    }

    wait_anim = mPlib_Get_Pointer_Animation(mPlayer_ANIM_WAIT1);

    cKF_SkeletonInfo_R_ct(&visual->keyframe0, appearance->skeleton, NULL, visual->joint_data, visual->morph_data);
    cKF_SkeletonInfo_R_ct(&visual->keyframe1, appearance->skeleton, NULL, visual->joint_data, visual->morph_data);
    mPlib_DMA_player_Part_Table(visual->part_table, mPlayer_PART_TABLE_NORMAL);

    cKF_SkeletonInfo_R_init_standard_repeat_setframeandspeedandmorph(&visual->keyframe0, wait_anim, NULL, 1.0f, 0.5f,
                                                                     0.0f);
    cKF_SkeletonInfo_R_init_standard_repeat_setframeandspeedandmorph(&visual->keyframe1, wait_anim, NULL, 1.0f, 0.5f,
                                                                     0.0f);

    visual->initialized = 1;
    visual->current_anim_idx = mPlayer_ANIM_WAIT1; /* explicit, even though this matches the
                                                     * zero-init default -- see the struct's doc
                                                     * comment */
    printf("[NET][REMOTE][DIAG] visual initialized: model=%s num_shown_joints=%d\n",
           (appearance->skeleton == &cKF_bs_r_boy_1) ? "boy" : "girl", (int)appearance->skeleton->num_shown_joints);
}

static void pc_remote_player_mv(ACTOR* actor, GAME* game) {
    PCRemotePlayerActor* self = (PCRemotePlayerActor*)actor;
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(self->peer);
    PCRemotePlayerRenderState render;
    double target_time;

    if (slot == NULL) {
        return; /* extremely defensive: self->peer is only ever set from an already-validated
                  * slot index at creation time, but never trust that blindly in a callback. */
    }

    /* Stage 3: the sender's own frame counter never appears here -- this is entirely THIS
     * process's own local presentation timeline, built from when samples arrived (see
     * pc_remote_player_interpolate()'s doc). A fixed delay behind "now" guarantees there is
     * almost always a real newer snapshot to interpolate towards rather than extrapolating into
     * the unknown. */
    target_time = graph_dt_frame_time(game) - PC_REMOTE_PLAYER_INTERP_DELAY_FRAMES;

    if (pc_remote_player_interpolate(slot, target_time, &render)) {
        actor->world.position = render.pos;
        actor->world.angle.y = render.angle;
        actor->shape_info.rotation.y = render.angle; /* mirrors how the local player's own
                                                       * main-state functions keep these two in
                                                       * sync, e.g. Player_actor_Movement_Walk() */

        self->cosmetic_speed = render.speed;
        self->cosmetic_move_state = render.move_state;
        self->cosmetic_item_kind = render.item_kind;
    }
    /* else: no movement data yet -- hold at the creation-time spawn position. Visual bring-up
     * below still proceeds regardless, so the idle model is ready as soon as possible rather than
     * waiting for the first network sample. */

    /* Stage 4A: bring up the real skeleton/animation once the LOCAL player's own appearance
     * resources are known-good. common_data.player_actor_exists is set at the very start of
     * Player_actor_ct() (src/game/m_player.c), which is itself only invoked from within this
     * same per-frame Actor_info_call_actor() sweep -- and ACTOR_PART_UNUSED (this actor's part)
     * is processed before ACTOR_PART_PLAYER in that sweep's part loop, so on the one frame where
     * the local player's ct_proc actually runs, THIS check still sees the pre-frame (FALSE)
     * value; it only reads TRUE starting the frame after Player_actor_ct() (and the face-texture
     * bank fill inside it, mPlib_change_player_face()) has fully completed. That is exactly the
     * ordering pc_remote_player_dw() below depends on for mPlib_get_player_face_p()/
     * mPlib_get_player_tex_p() to return non-stale data (Stage 4A's own local-appearance reads,
     * now superseded for rendering purposes -- see below -- but this readiness signal is still a
     * reasonable proxy for "the game/actor system is far enough along to build a skeleton" and is
     * kept for Stage 4C-1 too). This does not touch gamePT/Actor_info directly and does not
     * replace the existing low-address guard in pc_remote_player_poll() -- it is an additional,
     * independent readiness signal for player *appearance* resources specifically, as recommended
     * by the Stage 4 investigation.
     *
     * Stage 4C-1: additionally gated on slot->resolved_appearance.ready -- this remote player's
     * OWN appearance (gender/face/clothing) must have already arrived and been resolved (see
     * pc_remote_player_on_appearance()/pc_remote_player_apply_appearance()) before a skeleton is
     * selected for it, otherwise pc_remote_player_visual_init() would have nothing valid to read
     * appearance->skeleton from. Until then this actor simply stays un-visualized (no draw, see
     * pc_remote_player_dw()'s own initialized check) -- never falls back to the local player's
     * skeleton/appearance. */
    if (!self->visual.initialized && Common_Get(player_actor_exists) && slot->resolved_appearance.ready) {
        pc_remote_player_visual_init(&self->visual, &slot->resolved_appearance);
    }

    if (self->visual.initialized) {
        /* Stage 4B: pick the animation for the interpolated (or, absent movement data yet,
         * last-known/default-zero i.e. IDLE) move_state. self->cosmetic_move_state was already
         * updated above from render.move_state when interpolation succeeded, so this always
         * reflects the same value the investigation traced -- no separate lookup of `render`
         * needed here. Mapping verified against pc_net_game.c's own PCMoveState classifier, which
         * already sources move_state from the real player's mPlayer_INDEX_WAIT/WALK/RUN/DASH
         * constants (see the Stage 4B investigation). Anything outside the four basic states
         * (AIRBORNE/TUMBLE/ITEM_USE/OTHER) deliberately falls back to WAIT1 -- no dedicated remote
         * animation exists for those, and none should be added here. */
        int desired_anim_idx;
        switch (self->cosmetic_move_state) {
            case PC_MOVE_STATE_WALK:      desired_anim_idx = mPlayer_ANIM_WALK1;     break;
            case PC_MOVE_STATE_RUN:       desired_anim_idx = mPlayer_ANIM_RUN1;      break;
            case PC_MOVE_STATE_DASH:      desired_anim_idx = mPlayer_ANIM_DASH1;     break;
            /* Stage 4B.1: the real player's sprint sharp-turn/skid state (mPlayer_INDEX_TURN_DASH)
             * -- see pc_net_game.c's classifier, which now reports this separately from ordinary
             * DASH. Confirmed by source: uses mPlayer_ANIM_RUN_SLIP1, NOT DASH1. */
            case PC_MOVE_STATE_TURN_DASH: desired_anim_idx = mPlayer_ANIM_RUN_SLIP1; break;
            default:                      desired_anim_idx = mPlayer_ANIM_WAIT1;    break;
        }

        if (desired_anim_idx != self->visual.current_anim_idx) {
            /* State changed -- (re)start both layers on the new clip at frame 0, the same call
             * the real player's own Player_actor_InitAnimation_Base1() makes on every state entry
             * (src/game/m_player_common.c_inc). The keyframe structs themselves were already
             * cKF_SkeletonInfo_R_ct()'d once, in pc_remote_player_visual_init(), and never need
             * that again -- only this call, which rebinds the animation pointer/frame/speed
             * without touching the joint/morph buffer wiring cKF_SkeletonInfo_R_ct() set up. */
            cKF_Animation_R_c* new_anim = mPlib_Get_Pointer_Animation(desired_anim_idx);
            printf("[NET][REMOTE][DIAG] player %d: animation %d -> %d (move_state=%d)\n", (int)self->peer,
                   self->visual.current_anim_idx, desired_anim_idx, (int)self->cosmetic_move_state);
            cKF_SkeletonInfo_R_init_standard_repeat_setframeandspeedandmorph(&self->visual.keyframe0, new_anim, NULL,
                                                                             0.0f, 1.0f, 0.0f);
            cKF_SkeletonInfo_R_init_standard_repeat_setframeandspeedandmorph(&self->visual.keyframe1, new_anim, NULL,
                                                                             0.0f, 1.0f, 0.0f);
            self->visual.current_anim_idx = desired_anim_idx;

            if (desired_anim_idx == mPlayer_ANIM_RUN_SLIP1) {
                /* Stage 4B.1: RUN_SLIP1 does not use the movement-speed formula below -- the real
                 * player's own Player_actor_setup_main_Turn_dash_common() (m_player_main_turn_dash.c_inc)
                 * passes a fixed frame_speed=0.5f into its InitAnimation_Base1() call, and its
                 * per-frame advance is Player_actor_CulcAnimation_Base() (plain advance), never
                 * Player_actor_CulcAnimation_Walk() (the formula below). Set once, here, at the
                 * same state-entry point the animation itself is (re)bound, and never touched
                 * again while RUN_SLIP1 remains current -- see the exclusion below. */
                self->visual.keyframe0.frame_control.speed = 0.5f;
                self->visual.keyframe1.frame_control.speed = 0.5f;

                /* Fire the skid sound exactly once, on this same state-change edge -- never
                 * per-frame, never re-fired while RUN_SLIP1 remains current (this whole block only
                 * runs when desired_anim_idx just changed), and never retriggered by a stale or
                 * reordered packet (those are already rejected in pc_remote_player_on_move()
                 * before ever reaching the snapshot ring, so they cannot produce a "new"
                 * cosmetic_move_state value here). sAdo_OngenTrgStart() is the same generic,
                 * actor-agnostic, positional one-shot trigger the real Player_actor_sound_slip()/
                 * set_sound_common2() ultimately call (src/game/m_player_sound.c_inc) -- it only
                 * reads world.position, a base ACTOR field, so it is safe to call directly with
                 * this remote actor's own position; no new event/dedup system needed. */
                sAdo_OngenTrgStart(0x4129, &actor->world.position);
            }
        }

        /* Stage 4B: retune playback tempo every frame regardless of whether the state just
         * changed -- exactly like the real player's Player_actor_CulcAnimation_Walk() does
         * (shared verbatim by Walk/Run/Dash, src/game/m_player_main_walk.c_inc): never
         * reinitializes the animation, just updates frame_control.speed so
         * cKF_SkeletonInfo_R_combine_play() below advances at the right tempo. WAIT1 and
         * RUN_SLIP1 are excluded: the real player never runs this formula for Wait, and
         * RUN_SLIP1 uses a fixed 0.5x speed set once at entry above (see the Stage 4B.1
         * investigation) rather than a continuously-recalculated movement-speed tempo. */
        if (self->visual.current_anim_idx != mPlayer_ANIM_WAIT1 &&
            self->visual.current_anim_idx != mPlayer_ANIM_RUN_SLIP1) {
            float raw_speed = self->cosmetic_speed;
            float sp;

            if (!(raw_speed > 0.0f)) {
                /* Guards zero, any unexpected negative value, and NaN (all fail `> 0.0f`) --
                 * sqrtf() of a negative input is undefined, and a legitimate WALK/RUN/DASH state
                 * always carries positive speed, so this is strictly a defensive floor. */
                raw_speed = 0.0f;
            }

            /* Verified real-player formula (src/game/m_player_main_walk.c_inc's
             * Player_actor_CulcAnimation_Walk()), normalize == 1.0f: the real player's
             * terrain/slope normalization factor is derived from local collision data that is
             * never network-synced (see the Stage 4B investigation) -- an intentional,
             * cosmetic-only approximation. */
            sp = (raw_speed * 1.0f) / 7.5f;
            sp = sqrtf(sp);
            sp = 0.59999996f * sp;
            if (sp < 0.22f) {
                sp = 0.22f;
            }

            self->visual.keyframe0.frame_control.speed = sp;
            self->visual.keyframe1.frame_control.speed = sp;
        }

        cKF_SkeletonInfo_R_combine_play(&self->visual.keyframe0, &self->visual.keyframe1, self->visual.part_table);
    }
}

static void pc_remote_player_dw(ACTOR* actor, GAME* game) {
    GRAPH* graph = game->graph;
    PCRemotePlayerActor* self = (PCRemotePlayerActor*)actor;
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(self->peer);
    const PCRemotePlayerResolvedAppearance* appearance;
    Mtx* mtx;
    u8* eye_tex_p;
    u8* mouth_tex_p;
    Gfx* gfx;

    if (!self->visual.initialized) {
        return; /* not ready yet (see pc_remote_player_mv()) -- draw nothing this frame, retried
                  * automatically next frame */
    }

    if (slot == NULL || !slot->resolved_appearance.ready) {
        return; /* extremely defensive: pc_remote_player_mv() never sets visual.initialized until
                  * resolved_appearance.ready is true, so this should be unreachable -- but this
                  * dw_proc must never fall back to the local player's own segments, so it draws
                  * nothing rather than guessing. */
    }
    appearance = &slot->resolved_appearance;

    /* Per-frame scratch matrices, exactly like the inventory preview (mIV_pl_shape_draw) --
     * never a persistent per-instance buffer like PLAYER_ACTOR's own work_mtx[2][13]. */
    mtx = (Mtx*)GRAPH_ALLOC_TYPE(graph, Mtx, self->visual.keyframe0.skeleton->num_shown_joints);
    if (mtx == NULL) {
        return;
    }

    /* Stage 4C-1 appearance: THIS remote player's own eye/mouth/cloth/face resources, resolved
     * once at pc_remote_player_on_appearance() time into slot->resolved_appearance (see
     * pc_remote_player_apply_appearance()) -- no longer the local singleton banks Stage 4A used,
     * so each connected player now renders with their own gender/face/clothing. Pattern 0 (the
     * first 0x100-byte eye slot / first mouth slot within appearance->face_tex) is simply "not
     * blinking, default mouth"; blink/mouth-pattern animation is not synchronized, same as Stage
     * 4A. */
    eye_tex_p = (u8*)appearance->face_tex + 0 * 0x100;
    mouth_tex_p = (u8*)appearance->face_tex + mPlayer_EYE_TEX_NUM * 0x100;

    /* Reset RDP/RSP mode (texture/z/light/fog/prim) to a known baseline before emitting our own
     * commands -- the same call the real player's own Player_actor_draw_Normal() makes first. */
    _texture_z_light_fog_prim(graph);

    OPEN_DISP(graph);
    gfx = NOW_POLY_OPA_DISP;

    /* Same five segments Player_actor_draw_Normal() binds, but now sourced from THIS remote
     * player's own resolved buffers instead of the local singleton getters -- see the struct doc
     * comment on PCRemotePlayerResolvedAppearance. Kept immediately adjacent to the draw call
     * below within this same function, per the Stage 4C investigation: the local player's next
     * dw_proc call later this same frame rebinds these same segment registers to its own
     * resources before drawing itself, so a remote player's bindings must never be separated from
     * its own draw by any other actor's dw_proc or by a frame boundary. */
    gSPSegment(gfx++, ANIME_1_TXT_SEG, eye_tex_p);
    gSPSegment(gfx++, ANIME_2_TXT_SEG, mouth_tex_p);
    gSPSegment(gfx++, ANIME_3_TXT_SEG, (u8*)appearance->cloth_tex);
    gSPSegment(gfx++, ANIME_4_TXT_SEG, (u16*)appearance->cloth_pallet);
    gSPSegment(gfx++, ANIME_5_TXT_SEG, (u16*)appearance->face_pallet);

    SET_POLY_OPA_DISP(gfx);
    CLOSE_DISP(graph);

    /* No Matrix_translate/gSPMatrix here on purpose: the generic Actor_draw() (src/game/m_actor.c)
     * already loaded the root matrix from actor->world.position/shape_info.rotation/actor->scale
     * before calling this dw_proc, exactly like it does for the real PLAYER_ACTOR. Overriding it
     * (as the Stage 2/3 pyramid marker used to) would discard that and is not needed here. */
    cKF_Si3_draw_R_SV(game, &self->visual.keyframe0, mtx, NULL, NULL, actor);
}

static void pc_remote_player_destroy_slot(PCRemotePlayerSlot* slot) {
    if (!slot->in_use) {
        return;
    }
    if (slot->actor != NULL) {
        /* Two-phase, matching every other actor kind: this only nulls mv_proc/dw_proc. The actor
         * system reaps the memory and unlinks it from Actor_info during its normal per-frame
         * sweep (Actor_info_call_actor / Actor_info_delete). Never free this pointer directly. */
        Actor_delete(slot->actor);
    }
    slot->in_use = 0;
    slot->pending_create = 0;
    slot->lazily_discovered = 0;
    slot->actor = NULL;
    slot->has_sender_frame = 0;
    slot->newest_sender_frame = 0;
    slot->snapshot_count = 0;
    slot->snapshot_head = 0;

    /* Stage 4C-1: a genuine disconnect (unlike a scene-generation staleness event, which never
     * calls this function) means whoever reconnects into this slot next -- possibly a completely
     * different player -- must not start out able to render with the PREVIOUS occupant's
     * appearance. resolved_appearance.ready gates pc_remote_player_visual_init() (see
     * pc_remote_player_mv()), so clearing it here guarantees a freshly (re)connected peer always
     * waits for its own real PC_NETGAME_MSG_APPEARANCE before ever becoming visible. */
    slot->appearance.valid = 0;
    slot->appearance.pending_resolve = 0;
    slot->resolved_appearance.ready = 0;
}

void pc_remote_player_on_ready(PCNetPlayerId player_id, const PCNetGameIdentity* identity) {
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(player_id);

    if (slot == NULL) {
        return;
    }

    if (slot->in_use) {
        /* Shouldn't normally happen (a player id isn't reused while still tracked), but don't
         * leak a stale actor if it somehow does. */
        pc_remote_player_destroy_slot(slot);
    }

    slot->in_use = 1;
    slot->pending_create = 1;
    slot->lazily_discovered = 0; /* a direct handshake, not a relay discovery */
    slot->actor = NULL;
    if (identity != NULL) {
        slot->identity = *identity;
    } else {
        memset(&slot->identity, 0, sizeof(slot->identity));
    }

    printf("[NET][REMOTE] player %d READY -- remote-player actor creation pending\n", (int)player_id);
}

void pc_remote_player_on_disconnect(PCNetPlayerId player_id) {
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(player_id);
    if (slot == NULL) {
        return;
    }
    if (slot->in_use) {
        printf("[NET][REMOTE] player %d disconnected -- destroying remote-player actor\n", (int)player_id);
    }
    pc_remote_player_destroy_slot(slot);
}

void pc_remote_player_on_move(PCNetPlayerId player_id, const PCNetMoveSample* sample) {
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(player_id);
    PCRemoteMoveSnapshot* snap;
    double now;

    if (slot == NULL || sample == NULL) {
        return;
    }

    if (!slot->in_use) {
        /* Learned about this player purely through movement data -- e.g. a client seeing another
         * client's movement relayed by the host, having never handshaken with them directly.
         * Create the tracking slot lazily, matching on_ready()'s bookkeeping but with no captured
         * identity (there was no IDENTITY exchange to capture one from). Only slots created this
         * way are ever cleaned up by poll()'s liveness timeout below, since this is the only path
         * that has no matching PC_NET_EVENT_PEER_DISCONNECTED to rely on instead. */
        slot->in_use = 1;
        slot->pending_create = 1;
        slot->lazily_discovered = 1;
        slot->actor = NULL;
        memset(&slot->identity, 0, sizeof(slot->identity));
        printf("[NET][REMOTE] player %d discovered via movement relay -- remote-player actor creation pending\n",
               (int)player_id);
    }

    if (slot->has_sender_frame && sample->sender_frame <= slot->newest_sender_frame) {
        printf("[NET][REMOTE][DIAG] rejected stale/duplicate/reordered sample for player %d: "
               "incoming.frame=%u <= newest=%u\n",
               (int)player_id, (unsigned)sample->sender_frame, (unsigned)slot->newest_sender_frame);
        return; /* stale, duplicate, or reordered -- reject (see PCNetMoveSample's doc) */
    }
    slot->newest_sender_frame = sample->sender_frame;
    slot->has_sender_frame = 1;

    now = (gamePT != NULL) ? graph_dt_frame_time(gamePT) : 0.0;

    snap = &slot->snapshots[slot->snapshot_head];
    snap->recv_local_frame = now;
    snap->pos_x = sample->pos_x;
    snap->pos_y = sample->pos_y;
    snap->pos_z = sample->pos_z;
    snap->facing_angle = sample->facing_angle;
    snap->speed = sample->speed;
    snap->move_state = sample->move_state;
    snap->item_kind = sample->item_kind;

    slot->snapshot_head = (slot->snapshot_head + 1) % PC_REMOTE_PLAYER_SNAPSHOT_COUNT;
    if (slot->snapshot_count < PC_REMOTE_PLAYER_SNAPSHOT_COUNT) {
        slot->snapshot_count++;
    }
    slot->last_move_recv_local_frame = now;
}

void pc_remote_player_on_appearance(PCNetPlayerId player_id, const PCNetPlayerAppearance* appearance) {
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(player_id);

    if (slot == NULL || appearance == NULL) {
        return;
    }

    /* Deliberately no `slot->in_use` gate here, matching pc_remote_player_on_move()'s own
     * lazy-discovery pattern above: appearance is tracked independently of the handshake/movement
     * bookkeeping (see this function's own doc comment in pc_remote_player.h), so it must be
     * captured whichever of on_ready()/on_move()/on_appearance() happens to arrive first for this
     * player_id. */
    slot->appearance.valid = 1;
    slot->appearance.gender = appearance->gender;
    slot->appearance.face = appearance->face;
    slot->appearance.sunburn_rank = appearance->sunburn_rank;
    slot->appearance.is_custom_design = appearance->is_custom_design;
    slot->appearance.cloth_item = appearance->cloth_item;
    if (appearance->is_custom_design) {
        /* Only copied when actually meaningful -- design_record is otherwise unpacked but unused
         * padding on the wire for a catalog-clothing player (see PCNetPlayerAppearance's doc). */
        memcpy(&slot->appearance.design, appearance->design_record, sizeof(slot->appearance.design));
    }

    /* Set unconditionally on every arrival, BEFORE attempting to resolve -- pc_remote_player_poll()
     * uses this (never resolved_appearance.ready) to know a retry is needed, since `ready` can
     * still be 1 from an earlier, different appearance. See pc_remote_player_apply_appearance()'s
     * doc comment. */
    slot->appearance.pending_resolve = 1;

    pc_remote_player_apply_appearance(slot);

    printf("[NET][REMOTE][DIAG] player %d: appearance received (gender=%d face=%d sunburn=%d cloth_item=%d %s)\n",
           (int)player_id, (int)appearance->gender, (int)appearance->face, (int)appearance->sunburn_rank,
           (int)appearance->cloth_item, appearance->is_custom_design ? "custom-design" : "catalog");
}

int pc_remote_player_get_appearance(PCNetPlayerId player_id, PCNetPlayerAppearance* out) {
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(player_id);

    if (slot == NULL || out == NULL || !slot->appearance.valid) {
        return 0;
    }

    /* Zero first: `design_record` must never carry stale bytes onto the wire. If this slot was
     * previously occupied by a custom-design-wearing player and is now occupied by a
     * catalog-clothing one, slot->appearance.design still physically holds the PREVIOUS
     * occupant's design bytes (pc_remote_player_on_appearance() only overwrites `design` when the
     * NEW appearance itself is custom -- see its own doc comment) -- zeroing here, then copying it
     * out only when is_custom_design is set, matches the exact "only meaningful when
     * is_custom_design" convention PCNetPlayerAppearance/PCNetGameAppearanceMsg already use, and
     * guarantees a resent/backfilled catalog-clothing appearance never leaks anyone else's
     * leftover design pixels. */
    memset(out, 0, sizeof(*out));
    out->gender = slot->appearance.gender;
    out->face = slot->appearance.face;
    out->sunburn_rank = slot->appearance.sunburn_rank;
    out->is_custom_design = slot->appearance.is_custom_design;
    out->cloth_item = slot->appearance.cloth_item;
    if (slot->appearance.is_custom_design) {
        memcpy(out->design_record, &slot->appearance.design, sizeof(out->design_record));
    }

    return 1;
}

/* Stage 3 diagnostic: periodically logs each tracked remote player's current (interpolated)
 * actor position, purely so movement replication can be verified from stdout/log output alone --
 * there is no other way to observe a remote actor's live state without a graphical session. Not
 * gameplay-affecting; safe to leave in. */
static float s_diag_dump_accum = 0.0f;
#define PC_REMOTE_PLAYER_DIAG_DUMP_PERIOD_60FPS_FRAMES 60.0f /* ~1s */

void pc_remote_player_poll(void) {
    PLAYER_ACTOR* local;
    GAME_PLAY* play;
    double now;
    int i;
    int dump_now;

    if (gamePT == NULL) {
        return; /* no active GAME_PLAY (e.g. still at a menu) -- nothing to do, and Actor_info
                  * isn't valid to touch yet anyway. */
    }
    local = GET_PLAYER_ACTOR_NOW();
    if (local == NULL || (uint64_t)(uintptr_t)local >= PC_LOWADDR_LIMIT) {
        /* GAME_PLAY exists but the local player actor doesn't (yet) -- defer and retry. The
         * low-address check catches a real, observed transitional case: right around a scene
         * transition, gamePT can be non-NULL before that GAME_PLAY's actor_info is actually
         * constructed (see pc_net_game.c's pcnetgame_is_real_player_actor() for the full
         * explanation), which otherwise reads a non-NULL but garbage ACTOR* here. */
        return;
    }
    /* Only reached once `local`'s low-address check has confirmed this GAME_PLAY's actor_info (and
     * therefore everything game_ct()/play_init() sets up before it, including frame_counter) is
     * genuinely constructed -- see s_scene_generation's doc comment above for why this check must
     * not run any earlier than this. */
    if (gamePT != s_last_seen_game || gamePT->frame_counter < s_last_seen_frame_counter) {
        s_scene_generation++;
    }
    s_last_seen_game = gamePT;
    s_last_seen_frame_counter = gamePT->frame_counter;

    play = (GAME_PLAY*)gamePT;
    now = graph_dt_frame_time(gamePT);
    dump_now = graph_dt_period_elapsed(gamePT, &s_diag_dump_accum, PC_REMOTE_PLAYER_DIAG_DUMP_PERIOD_60FPS_FRAMES);

    pc_remote_player_init_profile();

    for (i = 0; i < PC_REMOTE_PLAYER_SLOT_COUNT; i++) {
        PCRemotePlayerSlot* slot = &s_slots[i];
        ACTOR* actor;
        const PCRemotePlayerOffset* ofs;
        f32 x, y, z;

        /* Real-client crash fix: retry resolving any appearance that arrived while gamePT was NULL
         * (see pc_remote_player_apply_appearance()'s doc comment). Deliberately NOT gated on
         * slot->in_use -- pc_remote_player_on_appearance() can populate `appearance` before
         * on_ready()/on_move() ever mark this slot in_use (see its own comment above) -- and
         * deliberately keyed on `pending_resolve`, never `!resolved_appearance.ready`, since
         * `ready` may still be 1 from an earlier, unrelated successful resolution. gamePT is
         * already confirmed non-NULL by this function's own check above, so this call is
         * guaranteed to actually resolve (and clear pending_resolve) rather than defer again. */
        if (slot->appearance.valid && slot->appearance.pending_resolve) {
            pc_remote_player_apply_appearance(slot);
        }

        if (!slot->in_use) {
            continue;
        }

        /* Stage 4A lifecycle fix: this slot's actor (if any) was created in an earlier scene that
         * Actor_info_dt() has since fully torn down -- see s_scene_generation's doc comment above.
         * That teardown already deleted and freed the ACTOR; it must NOT be passed to
         * Actor_delete() again (it is not merely unlinked, it no longer exists). Just drop the
         * stale reference and re-arm creation -- the existing pending_create handling below
         * (unchanged) recreates it in the current scene at the current local player's position.
         * Peer/network state (in_use, identity, the snapshot ring) is untouched, so movement sync
         * resumes immediately once the new actor exists, and since a freshly-allocated
         * PCRemotePlayerActor's embedded `visual` comes back zeroed (Actor_init_actor_class()'s
         * mem_clear), pc_remote_player_mv()'s existing player_actor_exists gate transparently
         * reinitializes the Stage 4A visual too -- no separate visual-recreation code needed. */
        if (slot->actor != NULL && slot->scene_generation != s_scene_generation) {
            printf("[NET][REMOTE] player %d: scene changed -- remote-player actor was replaced, recreating\n", i);
            slot->actor = NULL;
            slot->pending_create = 1;
        }

        /* Liveness timeout: only for relay-discovered peers (see pc_remote_player_on_move()) --
         * a directly-tracked peer (host: a real client; client: the host) is already destroyed
         * promptly and unconditionally by the real PC_NET_EVENT_PEER_DISCONNECTED via
         * on_disconnect(), so this never needs to (and must not) second-guess that path. */
        if (slot->lazily_discovered && slot->has_sender_frame &&
            (now - slot->last_move_recv_local_frame) > PC_REMOTE_PLAYER_TIMEOUT_FRAMES) {
            printf("[NET][REMOTE] player %d timed out (no movement data) -- destroying remote-player actor\n", i);
            pc_remote_player_destroy_slot(slot);
            continue;
        }

        /* Stage 4A lifecycle fix: belt-and-suspenders low-address liveness check, matching the one
         * already used for `local` above -- defends this direct dereference against any stale
         * slot->actor the scene-generation check (above) hasn't already caught. */
        if (dump_now && slot->actor != NULL && (uint64_t)(uintptr_t)slot->actor < PC_LOWADDR_LIMIT) {
            printf("[NET][REMOTE][DIAG] player %d actor pos=(%.1f,%.1f,%.1f) angle=%d snapshots=%d "
                   "speed=%.2f move_state=%d item_kind=%d\n",
                   i, slot->actor->world.position.x, slot->actor->world.position.y, slot->actor->world.position.z,
                   (int)slot->actor->world.angle.y, slot->snapshot_count,
                   (double)((PCRemotePlayerActor*)slot->actor)->cosmetic_speed,
                   (int)((PCRemotePlayerActor*)slot->actor)->cosmetic_move_state,
                   (int)((PCRemotePlayerActor*)slot->actor)->cosmetic_item_kind);
        }

        if (!slot->pending_create) {
            continue;
        }

        ofs = &s_offset_table[i];
        x = local->actor_class.world.position.x + ofs->x;
        y = local->actor_class.world.position.y;
        z = local->actor_class.world.position.z + ofs->z;

        actor = pc_actor_make_from_profile(&play->actor_info, gamePT, &s_remote_player_profile,
                                           &s_remote_player_dlftbl, x, y, z, 0, 0, 0,
                                           (mActor_name_t)((NAME_TYPE_PAD15 << 12) | (i & 0xFF)), 0);
        if (actor == NULL) {
            continue; /* still pending; retried again next frame */
        }

        ((PCRemotePlayerActor*)actor)->peer = (PCNetPlayerId)i;
        ((PCRemotePlayerActor*)actor)->cosmetic_item_kind = -1;

        /* Stage 4A: same scale/ofs_y the real player uses (Player_actor_init_value()/
         * Player_actor_ct(), src/game/m_player.c) -- required for the generic Actor_draw()'s
         * root-matrix setup (Matrix_softcv3_load/Matrix_scale) to place the shared player
         * skeleton correctly, since it's the same skeleton at the same model-space scale. No
         * shadow is set up (shape_info.shadow_proc stays NULL from Actor_init_actor_class()'s
         * mem_clear) -- out of scope for Stage 4A. */
        actor->scale.x = actor->scale.y = actor->scale.z = 0.01f;
        actor->shape_info.ofs_y = 200.0f;

        slot->actor = actor;
        slot->scene_generation = s_scene_generation;
        slot->pending_create = 0;
        printf("[NET][REMOTE] created remote-player actor for player %d at (%.1f, %.1f, %.1f)\n", i, x, y, z);
    }
}

void pc_remote_player_shutdown(void) {
    int i;
    for (i = 0; i < PC_REMOTE_PLAYER_SLOT_COUNT; i++) {
        pc_remote_player_destroy_slot(&s_slots[i]);
    }
}
