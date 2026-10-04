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
#include "sys_matrix.h" /* M9-C Phase 1: Matrix_get/put/push/pull, _Matrix_to_Mtx_new -- held-item draw */
#include "m_collision_obj.h" /* M9-B: ClObjPipe_c, CollisionCheck_setOC() -- see pc_remote_player_collide_update() */
#include "m_lib.h"           /* M9-B: _Game_play_isPause() */
#include "libultra/libultra.h"
#include "pc_platform.h"     /* M9-B: g_pc_collide_test_* (diagnostic verbosity only) */
#include "pc_host_observer.h" /* --host-observer: pc_host_observer_active() (the observer registers no collider and is never pushed by puppets) */
#include "pc_lowaddr.h" /* PC_LOWADDR_LIMIT -- see the guard in pc_remote_player_poll() */
#include "ac_effectbg.h" /* M9-C Phase 4: EffectBG_EFFECT_SHAKE_LARGE / EffectBG_VARIANT_* (remote tree-shake presentation) */
#include "ef_effect_control.h" /* M9-C Phase 2a: eEC_EFFECT_TURI_MIZU (alias proof for the relax_rod row); Phase 3: eEC_CLIP + effect ids */
#include "m_collision_bg.h" /* M9-C Phase 3: mCoBG_Wpos2Attribute() (read-only ground attribute), mCoBG_ATTRIBUTE_* */
#include "m_kankyo.h"        /* M9-C Phase 3: mEnv_NowWeather(), mEnv_WEATHER_* (caller-side effect guard) */
#define PC_REMOTE_PLAYER_HAVE_EEC_ENUM 1
#include "audio.h" /* Stage 4B.1: sAdo_OngenTrgStart() -- see the TURN_DASH skid sound in
                     * pc_remote_player_mv(). Already #include'd elsewhere in pc/ (e.g.
                     * pc_nes_fixnes.c) and compiles cleanly in the PC build. */

#include <math.h> /* sqrtf() -- see the Stage 4B animation-speed formula in pc_remote_player_mv() */
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

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

/* M9-C Phase 1: held-item classes (see pc_remote_player_held_class()). PC_HELD_DEFERRED = a kind the puppet
 * deliberately does NOT present yet (custom/ORG umbrellas, balloons, pinwheels, out-of-range values): it is treated
 * exactly like "no item" (idle arms, no model) so a puppet never has an item pose without its model. */
enum {
    PC_HELD_NONE = 0,
    PC_HELD_AXE,
    PC_HELD_NET,
    PC_HELD_UMBRELLA,
    PC_HELD_ROD,
    PC_HELD_SHOVEL,
    PC_HELD_FAN,
    PC_HELD_DEFERRED,
};

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

    /* M9-C Phase 1 (remote held-item presentation). All of this is per-actor (zero-initialised at creation,
     * so a recreated puppet never inherits a previous item) and contains no pointer to anything the local
     * player owns. */
    int                   current_anim1_idx;   /* mPlayer_ANIM_* currently bound to keyframe1 (== current_anim_idx
                                                * unless a held item overrides the upper-body layer) */
    int                   current_part_table;  /* mPlayer_PART_TABLE_* currently copied into part_table */
    int                   held_kind;           /* EFFECTIVE held item kind this puppet renders (-1 none/deferred) */
    int                   held_class;          /* PC_HELD_* of held_kind */
    int                   diag_valid;          /* PC_PUPPET_DIAG: last logged raw item kind is diag_raw_kind */
    int                   diag_raw_kind;
    int                   item_ready;          /* item_keyframe holds a valid net/rod skeleton instance */
    int                   item_anim_idx;       /* mPlayer_ITEM_DATA_* currently bound on item_keyframe */
    cKF_SkeletonInfo_R_c  item_keyframe;       /* net / rod skeleton instance (puppet-owned) */
    s_xyz                 item_joint_data[8];  /* same sizes as PLAYER_ACTOR::item_joint_data / item_morph_data */
    s_xyz                 item_morph_data[8];
    s_xyz                 net_angle;           /* net joint-3 rotation (PLAYER_ACTOR::net_angle equivalent) */

    /* M9-C Phase 2a (protocol v7 state rows). Per actor, zero-initialised at creation: a recreated puppet (scene
     * generation, disconnect/reconnect) starts with no row, no latch and an unknown pair. */
    int                   pair_known;          /* a (main_index, counter) pair has been adopted */
    int                   pair_valid;          /* last adopted pair carried action info */
    uint8_t               pair_idx;            /* last adopted wire main index */
    uint8_t               pair_ctr;            /* last adopted 4-bit entry counter */
    int                   row_active;          /* a state row currently drives the body clips */
    int                   row_idx;             /* its mPlayer_INDEX_* */
    int                   row_finished;        /* the one-shot clip reached its end */
    int                   row_chained;         /* switched to the row's chain (REPEAT) clip after the one-shot ended */
    int                   row_pending;         /* a fallback state arrived while the one-shot was still playing */
    double                row_pending_since;   /* graph_dt_frame_time() of that edge */
    double                row_start_time;
    int                   item_restart;        /* rebind the item clip at the next item tick (row (re)start) */
    int                   item_carry_ok;       /* a row was active before this one (frame carry-over is meaningful) */
    int                   item_hidden;         /* row flag: do not draw the carried item */
    int                   row_held_kind;       /* effective held kind when the row started (chain clip item pose) */
    int                   row_adopted;         /* M9-C Phase 6a: the active row was adopted on a fresh actor (no entry edge seen) */
    int                   rebind_from_row;     /* review R1-L3: current_anim_idx was invalidated by a row release (no skid replay) */
} PCRemotePlayerVisual;

/* M9-C Phase 3 (locomotion cosmetics): per-actor puppet state. Zero-initialised at actor creation (a recreated puppet,
 * scene generation or reconnect starts with no edge state, no foot data and a full effect bucket). Contains no pointer to
 * anything the local player owns. */
typedef struct PCRemotePlayerCosmetic {
    xyz_t   foot_pos[2];     /* [0] left / [1] right foot world position, captured by THIS puppet's after-draw callback */
    s16     foot_angle_y[2];
    u32     foot_frame[2];   /* GAME::frame_counter + 1 at capture; 0 = never captured */
    int     known;           /* the first state has been adopted (first sight never fires an edge) */
    int     state;           /* PC_COS_* */
    int     last_valid;
    int     last_clip;       /* anim index (or 1000 + row index) the stored frame belongs to */
    float   last_frame;      /* keyframe0 current_frame seen on the previous puppet move */
    double  last_row_start;  /* PCRemotePlayerVisual::row_start_time of the run last_frame belongs to (0 = no row) */
    double  foot_block;      /* graph_dt_frame_time() before which no foot trigger fires (flicker guard) */
    double  turn_in_block;
    double  turn_out_block;
    double  tumble_block;
    int     bucket_init;
    float   tokens;          /* per-puppet effect budget (token bucket) */
    double  bucket_time;
    float   ripple_accum;
    int     ripple_foot;
    unsigned pending;        /* foot-step pieces still to emit (bit = foot * 3 + PC_PIECE_*), retried while the budget is full */
    int     pending_dash;
    double  pending_until;
    /* M9-C Phase 4 (tree shake at SHAKE1 frame 10) */
    double  tree_row_start;  /* PCRemotePlayerVisual::row_start_time of the SHAKE_TREE run this state belongs to */
    int     tree_armed;      /* this run has not presented its frame-10 shake yet */
    int     tree_committed;  /* the shake was accepted (cooldown stamped, sound handled): retries only retry the effect */
    double  tree_deadline;   /* a presentation denied only by a cap is retried until this time (0 = not yet attempted) */
    int     tree_have_last;  /* tree_last is valid */
    double  tree_last;       /* time of the last accepted shake of this puppet (per-puppet 84 frame cooldown) */
    /* M9-C Phase 5 (pickup): left-hand capture + the flying-item presentation of the current PICKUP run */
    xyz_t   hand_pos;        /* left hand (joint LARM2, 1100 units along the joint x axis), this puppet's own draw callback */
    u32     hand_frame;      /* GAME::frame_counter + 1 at capture; 0 = never */
    double  pk_row_start;    /* PCRemotePlayerVisual::row_start_time of the PICKUP run the fields below belong to (0 = none) */
    int     pk_bound;        /* this run has consumed its (one) event */
    int     pk_active;       /* the flying item is being drawn for this run */
    u16     pk_item;
    int     pk_ofs_valid;
    xyz_t   pk_ground;       /* the item's ground position (tile centre + vanilla weed/shell offset) */
    xyz_t   pk_ofs;          /* vanilla main_pickup_c::item_offset: ground - hand, refreshed while the timer is < 20 */
    /* M9-C Phase 6a (held-tool actions): net position capture + the one-shot event bookkeeping of the current tool run */
    xyz_t   net_pos;         /* PLAYER_ACTOR::net_pos equivalent (net item matrix, RotY(3000), 4000 units along z), own draw */
    u32     net_frame;       /* GAME::frame_counter + 1 at capture; 0 = never */
    double  tool_row_start;  /* PCRemotePlayerVisual::row_start_time of the tool run the fields below belong to (0 = none) */
    unsigned tool_done;      /* bit i = event i of the row table was presented/skipped for this run */
    unsigned tool_due;       /* bit i = event i reached its trigger and still waits for the budget */
    double  tool_due_time;   /* time the oldest still-due event became due (retry window start) */
    double  tool_repeat_last;/* last time a repeating event (fan) fired: wrap-alias guard */
    /* M9-C Phase 6b: FEEL joint (Player_actor_draw_After_feel) capture, this puppet's own draw callback */
    xyz_t   feel_pos;
    u32     feel_frame;      /* GAME::frame_counter + 1 at capture; 0 = never */
    int     sweat_active;    /* review H1: this puppet spawned a (continuous) ASE2 pitfall sweat that it must kill itself */
    struct {                 /* PC_PUPPET_DIAG rate limit: lines printed per (kind, name, budget) key */
        char key[40];
        int  n;
    } diag[48];
} PCRemotePlayerCosmetic;

/* Lifetime counters of one player's cosmetics; they live in the slot so they survive a scene-generation recreate and
 * are dumped (PC_PUPPET_DIAG) when the slot is destroyed. */
typedef struct PCRemotePlayerCosStats {
    int spawned;          /* top-level effect spawns through the clip */
    int capped;           /* skipped by the per-puppet bucket / global per-frame cap */
    int suppressed;       /* skipped by scene/pause/hidden/null/range/pool/weather */
    int sounds;
    int sounds_suppressed;
    int tree_effects;     /* M9-C Phase 4: EffectBG tree shakes presented */
    int tree_sounds;      /* ... and their NA_SE_TREE_YURASU one-shots */
    int tree_skipped;     /* shakes whose effect was denied (notree/cooldown/dup/pool/capped/scene/range/...) */
    int pk_events;        /* M9-C Phase 5: PLAYER_ACTION pickup events queued */
    int pk_started;       /* ... presented from the start of the clip */
    int pk_late;          /* ... presented from a later clip frame */
    int pk_dropped;       /* ... dropped (clip already past the flight / expired / evicted) */
    int pk_bell;          /* ... bell items (wallet path: no flying item) */
    int pk_ignored;       /* ... gated (scene/hidden/pause/range/null) */
    int pk_sounds;        /* pickup sounds played (ITEM_GET / GASAGOSO) */
    int tool_effects;     /* M9-C Phase 6a: held-tool effects spawned */
    int tool_sounds;      /* ... held-tool sounds played */
    int tool_noop;        /* ... effects skipped because vanilla's init would spawn nothing visible */
    int tool_dropped;     /* ... events lost to a cap for the whole retry window / run ended while due */
} PCRemotePlayerCosStats;

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
    uint8_t               cosmetic_action_index;   /* M9-C v7: last-interpolated wire main index (newer snapshot) */
    uint8_t               cosmetic_action_counter; /* its 4-bit entry counter */
    uint8_t               cosmetic_action_valid;   /* 0 = no/invalid action info (fallback mapping only) */
    PCRemotePlayerVisual  visual;
    ClObjPipe_c           col_pipe;     /* M9-B: this puppet's own player<->player collision pipe, registered
                                          * with the vanilla OC pass from pc_remote_player_mv() (see
                                          * pc_remote_player_collide_update()). Initialised at creation in
                                          * pc_remote_player_poll() (the puppet's ct_proc is none_proc2). */
    int                   collide_hold; /* M9-B: frames left during which the pipe is NOT registered (set after
                                          * an interpolator teleport-snap to avoid a deep-overlap pop) */
    MtxF                  hand_mtx;       /* M9-C Phase 1: world matrix of the right-hand joint, captured by this
                                           * puppet's OWN after-draw callback during its own body draw (never the
                                           * vanilla PLAYER_ACTOR callbacks / right_hand_mtx) */
    int                   hand_mtx_valid; /* set by the callback, cleared before every body draw */
    PCRemotePlayerCosmetic cos;           /* M9-C Phase 3: foot capture + cosmetic edge/budget state */
} PCRemotePlayerActor;

/* M9-B: the puppet pipe. Mirrors Player_actor_OcInfoData_forStand (m_player_common.c_inc) except for the group
 * flags: flags0 0x09 = CHECK | collides-with GROUP_PLAYER (0x08); flags1 0x10 = this puppet is GROUP_2 and NOT
 * ClObj_FLAG2_IS_PLAYER (0x08). The local player's pipe is {0x39, 0x08}: 0x39 & 0x10 and 0x08 & 0x09 both pass
 * CollisionCheck_Check2ClObjNoOC(), so player<->puppet pairs; puppet<->puppet fails (0x09 & 0x10 == 0), and no
 * other actor sets the 0x08 is-player bit, so nothing else pairs with a puppet. No ClObj_FLAG_COLLISION_PRIORITY. */
static ClObjPipeData_c s_remote_player_pipe_data = {
    { 0x09, 0x10, ClObj_TYPE_PIPE }, /* ClObjData_c */
    { 0x01 },                        /* ClObjElemData_c */
    { { 20, 60, 0, { 0, 0, 0 } } },  /* ClObjPipeAttrData_c: radius 20, height 60 (same as the player's) */
};

#define PC_REMOTE_PLAYER_COLLIDE_RANGE 150.0f       /* XZ units; only nearby puppets enter the 50-entry OC table */
#define PC_REMOTE_PLAYER_COLLIDE_SNAP_DIST 80.0f    /* a single-frame puppet XZ jump larger than this = teleport snap */
#define PC_REMOTE_PLAYER_COLLIDE_SNAP_HOLD_FRAMES 2 /* frames to stay unregistered after such a snap */

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
    uint8_t action_index;   /* M9-C v7: wire main index (valid only if action_valid) */
    uint8_t action_counter; /* 4-bit entry counter */
    uint8_t action_valid;
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

/* M9-C Phase 5: queued PLAYER_ACTION pickup events of one puppet (the puppet runs ~9 frames behind the network and a
 * reliable event can arrive before the MOVE that shows the pickup state, so events wait here for the puppet's own state). */
#define PC_PUPPET_PK_RING 2
typedef struct PCRemotePickupEvent {
    int      valid;
    uint16_t seq;
    uint8_t  ux, uz;
    uint16_t item;
    double   arrival; /* graph_dt_frame_time() when queued (0 when no game existed) */
} PCRemotePickupEvent;

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

    PCNetPlayerScene   scene;              /* M9-A: last accepted scene identity of this player (valid == 0 when
                                             * unknown); cleared on disconnect/ready/timeout, NOT by a local
                                             * scene-generation change. Presence data only. */

    int                collide_armed;      /* M9-B: 1 while this puppet's collider was registered on the last frame it
                                             * was evaluated (log state only -- registration itself is stateless) */
    int                collide_target_ok;  /* M9-B: test-hook accessor input: puppet is live, same-scene, has
                                             * snapshots and a visual (range/hold ignored) */
    int                latch_clear_req;    /* M9-C v7: the player's scene presence changed -> the puppet drops any one-shot
                                             * latch at its next move (consumed by pc_remote_player_row_pre) */
    PCRemotePlayerCosStats cos_stats;      /* M9-C Phase 3: cosmetic counters (diag) */
    PCRemotePickupEvent pk_ev[PC_PUPPET_PK_RING]; /* M9-C Phase 5: queued pickup events (cleared with the slot / actor) */
} PCRemotePlayerSlot;

/* M9-B diagnostics (read-only; never influence behaviour). */
static int s_collide_peak_colliders = 0; /* highest play->collision_check.collider_num seen at poll time */
static int s_collide_failed_setoc = 0;   /* CollisionCheck_setOC() returned -1 for a puppet */

static PCRemotePlayerSlot s_slots[PC_REMOTE_PLAYER_SLOT_COUNT];

static void pc_remote_player_dt(ACTOR* actor, GAME* game); /* review H1: kills the puppet's own pitfall sweat */

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
    uint8_t action_index;
    uint8_t action_counter;
    uint8_t action_valid;
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
        out->action_index = s1->action_index; /* discrete: from the NEWER snapshot like move_state (never interpolated) */
        out->action_counter = s1->action_counter;
        out->action_valid = s1->action_valid;
    } else if (s0 != NULL) {
        out->pos.x = s0->pos_x;
        out->pos.y = s0->pos_y;
        out->pos.z = s0->pos_z;
        out->angle = s0->facing_angle;
        out->speed = s0->speed;
        out->move_state = s0->move_state;
        out->item_kind = s0->item_kind;
        out->action_index = s0->action_index;
        out->action_counter = s0->action_counter;
        out->action_valid = s0->action_valid;
    } else { /* s1 != NULL */
        out->pos.x = s1->pos_x;
        out->pos.y = s1->pos_y;
        out->pos.z = s1->pos_z;
        out->angle = s1->facing_angle;
        out->speed = s1->speed;
        out->move_state = s1->move_state;
        out->item_kind = s1->item_kind;
        out->action_index = s1->action_index;
        out->action_counter = s1->action_counter;
        out->action_valid = s1->action_valid;
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
    s_remote_player_profile.dt_proc = pc_remote_player_dt;
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

    visual->current_anim1_idx = mPlayer_ANIM_WAIT1;
    visual->current_part_table = mPlayer_PART_TABLE_NORMAL; /* matches the mPlib_DMA_player_Part_Table() above */
    visual->held_kind = mPlayer_ITEM_KIND_NONE;
    visual->held_class = PC_HELD_NONE;
    visual->item_ready = 0;

    visual->initialized = 1;
    visual->current_anim_idx = mPlayer_ANIM_WAIT1; /* explicit, even though this matches the
                                                     * zero-init default -- see the struct's doc
                                                     * comment */
    printf("[NET][REMOTE][DIAG] visual initialized: model=%s num_shown_joints=%d\n",
           (appearance->skeleton == &cKF_bs_r_boy_1) ? "boy" : "girl", (int)appearance->skeleton->num_shown_joints);
}

/* ------------------------------------------------------------------------------------------------------------
 * M9-C Phase 1: remote held-item presentation (no protocol change: MOVE.item_kind already arrives).
 *
 * Everything below is PUPPET-OWNED: it only reads pure compiled-in data (mPlib_* tables, item display lists and
 * skeletons) and writes only into the PCRemotePlayerActor it is given. It never calls a Player_actor_* function,
 * never reads Now_Private / the local PLAYER_ACTOR, never touches a vanilla draw callback and never binds a
 * segment (the tool/umbrella display lists address their textures/palettes directly, verified in
 * src/data/model/player_tool.c and tol_umb_NN.c). Phase 1 shows the CARRIED pose only; swing/cast poses come
 * with the main-index sync of a later phase (ITEM_USE currently maps to WAIT1, so the carried pose is shown there).
 * ------------------------------------------------------------------------------------------------------------ */

/* 1 once, from the environment (PC_PUPPET_DIAG=1): decision lines on item change only. */
static int pc_remote_player_puppet_diag(void) {
    static int s_diag = -1;
    if (s_diag < 0) {
        const char* e = getenv("PC_PUPPET_DIAG");
        s_diag = (e != NULL && e[0] == '1') ? 1 : 0;
    }
    return s_diag;
}

static int pc_remote_player_held_class(int kind) {
    if (kind < 0) {
        return PC_HELD_NONE;
    }
    if (mPlayer_ITEM_IS_AXE(kind)) {
        return PC_HELD_AXE;
    }
    if (mPlayer_ITEM_IS_NET(kind)) {
        return PC_HELD_NET;
    }
    if (kind >= mPlayer_ITEM_KIND_UMBRELLA00 && kind <= mPlayer_ITEM_KIND_UMBRELLA31) {
        return PC_HELD_UMBRELLA; /* catalog umbrellas only; ORG00..07 (custom designs are not transmitted) fall through */
    }
    if (mPlayer_ITEM_IS_ROD(kind)) {
        return PC_HELD_ROD;
    }
    if (mPlayer_ITEM_IS_SHOVEL(kind)) {
        return PC_HELD_SHOVEL;
    }
    if (mPlayer_ITEM_IS_FAN(kind)) {
        return PC_HELD_FAN;
    }
    return PC_HELD_DEFERRED; /* ORG umbrellas, balloons, pinwheels, anything out of range */
}

static const char* pc_remote_player_held_name(int kind, int cls) {
    switch (cls) {
        case PC_HELD_NONE:     return "none";
        case PC_HELD_AXE:      return (kind == mPlayer_ITEM_KIND_GOLD_AXE) ? "gold_axe" : "axe";
        case PC_HELD_NET:      return (kind == mPlayer_ITEM_KIND_GOLD_NET) ? "gold_net" : "net";
        case PC_HELD_UMBRELLA: return "umbrella";
        case PC_HELD_ROD:      return (kind == mPlayer_ITEM_KIND_GOLD_ROD) ? "gold_rod" : "rod";
        case PC_HELD_SHOVEL:   return (kind == mPlayer_ITEM_KIND_GOLD_SHOVEL) ? "gold_shovel" : "shovel";
        case PC_HELD_FAN:      return "fan";
        default:               return "deferred";
    }
}


/* ------------------------------------------------------------------------------------------------------------
 * M9-C Phase 2a: STATE ROW TABLE (protocol v7 action_state -> puppet animation).
 *
 * One row per vanilla mPlayer_INDEX_* (121). A row with name == NULL is the DEFAULT ("fallback"): no override, the
 * puppet keeps using the coarse move_state -> animation mapping plus the Phase 1 carried-item pose. A row is filled
 * ONLY when the animation / part table / mode / speed were confirmed by reading the vanilla
 * Player_actor_setup_main_<X>() (src/game/m_player_main_<x>.c_inc, cited per row) and the InitAnimation_Base* /
 * SetupItem_Base* it calls (src/game/m_player_common.c_inc: Base1 1856 = REPEAT, Base2 1885 = explicit mode,
 * SetupItem_Base0 3911 / Base1 3953 / Base2 3968 = held-item layer).
 *
 * Alias trap (decomp naming, verified numerically in include/m_player.h and asserted below): the item clips passed
 * to SetupItem_Base2()/LoadOrDestruct_Item() use mPlayer_ANIM_* names for mPlayer_ITEM_DATA_* values, e.g.
 * ANIM_PICKUP1 (7) == ITEM_DATA_NET_SWING, ANIM_HOLD_WAIT1 (6) == ITEM_DATA_NET_GET_M, ANIM_LTURN1 (8) ==
 * ITEM_DATA_KAMAE_MAIN_M, ANIM_RTURN1 (9) == ITEM_DATA_KOKERU_GETUP_N, ANIM_GET1 (10) == ITEM_DATA_KOKERU_N, rod:
 * ANIM_TRANS_WAIT1 (15) == ROD_GET_T, UMB_OPEN1 (17) == ROD_SINARI, UMBRELLA1 (18) == ROD_SWING, RUN_SLIP1 (20) ==
 * ROD_NOT_SWING, and relax_rod passes eEC_EFFECT_TURI_MIZU (70) as a body clip == ANIM_TURI_WAIT1 (70). The rows
 * below therefore name the ITEM_DATA_* clips explicitly.
 *
 * Deliberate approximations (all cosmetic): morph is always 0 (vanilla blends with morph -5/-3/0; the puppet has
 * always snapped between clips); start frame 1.0 as in vanilla; the net-hit frame hold in swing_net and the
 * item_scale animation of take-out/put-away are not reproduced.
 * ------------------------------------------------------------------------------------------------------------ */

#define PC_ROW_A1_SAME (-1)             /* anim1 == anim0 */
#define PC_ROW_A1_ITEM (-2)             /* anim1 == held-item pose clip when an item is carried, else anim0 */
#define PC_ROW_ANIM_TUMBLE (-10)        /* anim0 chosen by item kind (m_player_main_tumble.c_inc:42) */
#define PC_ROW_ANIM_TUMBLE_GETUP (-11)  /* anim0 chosen by item kind (m_player_main_tumble_getup.c_inc:58) */

enum {
    PC_ROWF_BODY_CONTINUE = 1 << 0,   /* vanilla does NOT re-init the body clips: keep them if already playing anim0 */
    PC_ROWF_PT_NET_IF_ITEM = 1 << 2,  /* part table NET when an item is carried (pickup_exchange.c_inc:31) */
    PC_ROWF_BODY_CARRY = 1 << 3,      /* start the body clips at the previous row's current frame (vanilla passes it) */
    PC_ROWF_ITEM_CARRY = 1 << 4,      /* same for the item clip */
    PC_ROWF_PT_FROM_ANIM1 = 1 << 5,   /* part table = mPlib_Get_BasicPartTableIndex_fromAnimeIndex(anim1), i.e. the
                                       * SetupItem_Base1() out-parameter the vanilla state passes on (Phase 2b) */
    PC_ROWF_CHAIN_STOP = 1 << 6,      /* the chain clip is a STOP clip (stays on its last frame), not REPEAT (stung_bee) */
    PC_ROWF_HIDE_BODY = 1 << 7,       /* vanilla draw type NONE (m_player.c Player_actor_draw): draw neither body nor item */
};

enum { PC_NETANG_DEFAULT = 0, PC_NETANG_RESET, PC_NETANG_READY, PC_NETANG_READY_WALK, PC_NETANG_SLIP,
       PC_NETANG_SWING, PC_NETANG_ZERO, PC_NETANG_WALK };
enum { PC_TEMPO_FIXED = 0, PC_TEMPO_READY_WALK_NET };

typedef struct PCStateRow {
    const char* name;     /* NULL = fallback row (no override) */
    s16         anim0;    /* mPlayer_ANIM_* (or PC_ROW_ANIM_*) on keyframe0 */
    s16         anim1;    /* mPlayer_ANIM_* or PC_ROW_A1_* on keyframe1 */
    s8          part_table;
    u8          mode;     /* cKF_FRAMECONTROL_STOP (one-shot: latched) / REPEAT */
    f32         speed;
    f32         start_frame;
    u16         flags;    /* PC_ROWF_* */
    s16         chain_anim; /* body clip (REPEAT, 0.5x, frame 1) the vanilla state switches to when the STOP clip ends; -1 none */
    s16         item_clip;  /* mPlayer_ITEM_DATA_* net/rod clip on the puppet item skeleton, -1 = carried default */
    u8          item_mode;
    f32         item_start;
    u8          net_angle;  /* PC_NETANG_* (Player_actor_Item_net_CulcJointAngle_dummy_net table, m_player_item_net.c_inc:125) */
    u8          tempo;      /* PC_TEMPO_* */
} PCStateRow;

#define PC_STOP cKF_FRAMECONTROL_STOP
#define PC_REPEAT cKF_FRAMECONTROL_REPEAT
#define PC_NORMAL mPlayer_PART_TABLE_NORMAL
#define PCROW(nm, a0, a1, pt, md, spd, st, fl, chain, ic, im, is, na, tp) \
    { nm, a0, a1, pt, md, spd, st, fl, chain, ic, im, is, na, tp }
/* common shapes */
#define PCROW_BODY(nm, a0, md, na) PCROW(nm, a0, PC_ROW_A1_SAME, PC_NORMAL, md, 0.5f, 1.0f, 0, -1, -1, 0, 1.0f, na, PC_TEMPO_FIXED)
/* Reaction shape: anim1 = SetupItem_Base1() item pose (or the clip itself), explicit part table, speed 0.5, start frame 1. */
#define PCROW_ITEM(nm, a0, pt, md, fl, chain, na) PCROW(nm, a0, PC_ROW_A1_ITEM, pt, md, 0.5f, 1.0f, fl, chain, -1, 0, 1.0f, na, PC_TEMPO_FIXED)
/* EXPLICIT FALLBACK row: no override, the coarse move_state mapping + Phase 1 carried pose apply. The string states why
 * and is dropped by the preprocessor (it is documentation that the table test greps). */
#define PCFB(why) { NULL }

/* Alias-trap proofs (compile-time): the item-clip names the vanilla code passes as mPlayer_ANIM_* are these ITEM_DATA ids. */
_Static_assert((int)mPlayer_ANIM_PICKUP1 == (int)mPlayer_ITEM_DATA_NET_SWING, "alias: ANIM_PICKUP1 is ITEM_DATA_NET_SWING");
_Static_assert((int)mPlayer_ANIM_HOLD_WAIT1 == (int)mPlayer_ITEM_DATA_NET_GET_M, "alias: ANIM_HOLD_WAIT1 is ITEM_DATA_NET_GET_M");
_Static_assert((int)mPlayer_ANIM_LTURN1 == (int)mPlayer_ITEM_DATA_KAMAE_MAIN_M, "alias: ANIM_LTURN1 is ITEM_DATA_KAMAE_MAIN_M");
_Static_assert((int)mPlayer_ANIM_RTURN1 == (int)mPlayer_ITEM_DATA_KOKERU_GETUP_N, "alias: ANIM_RTURN1 is ITEM_DATA_KOKERU_GETUP_N");
_Static_assert((int)mPlayer_ANIM_GET1 == (int)mPlayer_ITEM_DATA_KOKERU_N, "alias: ANIM_GET1 is ITEM_DATA_KOKERU_N");
_Static_assert((int)mPlayer_ANIM_GET_CHANGE1 == (int)mPlayer_ITEM_DATA_SWING_WAIT, "alias: ANIM_GET_CHANGE1 is ITEM_DATA_SWING_WAIT");
_Static_assert((int)mPlayer_ANIM_TRANS_WAIT1 == (int)mPlayer_ITEM_DATA_ROD_GET_T, "alias: ANIM_TRANS_WAIT1 is ITEM_DATA_ROD_GET_T");
_Static_assert((int)mPlayer_ANIM_UMB_OPEN1 == (int)mPlayer_ITEM_DATA_ROD_SINARI, "alias: ANIM_UMB_OPEN1 is ITEM_DATA_ROD_SINARI");
_Static_assert((int)mPlayer_ANIM_UMBRELLA1 == (int)mPlayer_ITEM_DATA_ROD_SWING, "alias: ANIM_UMBRELLA1 is ITEM_DATA_ROD_SWING");
_Static_assert((int)mPlayer_ANIM_RUN_SLIP1 == (int)mPlayer_ITEM_DATA_ROD_NOT_SWING, "alias: ANIM_RUN_SLIP1 is ITEM_DATA_ROD_NOT_SWING");
#ifdef PC_REMOTE_PLAYER_HAVE_EEC_ENUM
_Static_assert((int)eEC_EFFECT_TURI_MIZU == (int)mPlayer_ANIM_TURI_WAIT1, "alias: relax_rod's eEC_EFFECT_TURI_MIZU is ANIM_TURI_WAIT1");
#endif

static const PCStateRow s_state_rows[mPlayer_INDEX_NUM] = {
    /* TUMBLE 11 / TUMBLE_GETUP 12: clip by item kind (tumble.c_inc:68 setup_main_Tumble: InitAnimation_Base2(anim, anim,
     * 1, 1, speed 0.5, morph -5, mode 0 STOP, table 0); getup.c_inc:96 identical). Nets also play the net item clip
     * KOKERU_N / KOKERU_GETUP_N in STOP mode (setup_Item_Tumble tumble.c_inc:11 -> ANIM_GET1, getup.c_inc:52 -> ANIM_RTURN1);
     * every other kind keeps its default carried item clip. Item net angle: dummy_net table = zero for both states. */
    [mPlayer_INDEX_TUMBLE] = PCROW("tumble", PC_ROW_ANIM_TUMBLE, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 1.0f, 0, -1,
                                   mPlayer_ITEM_DATA_KOKERU_N, PC_STOP, 1.0f, PC_NETANG_ZERO, PC_TEMPO_FIXED),
    [mPlayer_INDEX_TUMBLE_GETUP] = PCROW("tumble_getup", PC_ROW_ANIM_TUMBLE_GETUP, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f,
                                         1.0f, 0, -1, mPlayer_ITEM_DATA_KOKERU_GETUP_N, PC_STOP, 1.0f, PC_NETANG_ZERO,
                                         PC_TEMPO_FIXED),

    /* PICKUP 30 (m_player_main_pickup.c_inc:20,74,182): SetupItem_Base1(PICKUP1) gives layer 1 the carried-item pose
     * (PICKUP1 itself when nothing is carried); InitAnimation_Base2(PICKUP1, anim1, 1, 1, 0.5, morph -6, STOP,
     * PART_TABLE_PICK_UP). Net angle: dummy_net table = reset.
     * PICKUP_JUMP 31 / PICKUP_FURNITURE 32: room-only, left to the fallback. */
    [mPlayer_INDEX_PICKUP] = PCROW("pickup", mPlayer_ANIM_PICKUP1, PC_ROW_A1_ITEM, mPlayer_PART_TABLE_PICK_UP, PC_STOP, 0.5f,
                                   1.0f, 0, -1, -1, 0, 1.0f, PC_NETANG_RESET, PC_TEMPO_FIXED),
    /* PICKUP_EXCHANGE 33 (m_player_main_pickup_exchange.c_inc:17,29-36): Base1 REPEAT, anim0 PICKUP_WAIT1, anim1 =
     * item pose (PICKUP_WAIT1 when nothing carried), part table NET when anim1 != PICKUP_WAIT1 (an item is carried). */
    [mPlayer_INDEX_PICKUP_EXCHANGE] = PCROW("pickup_exchange", mPlayer_ANIM_PICKUP_WAIT1, PC_ROW_A1_ITEM, PC_NORMAL,
                                            PC_REPEAT, 0.5f, 1.0f, PC_ROWF_PT_NET_IF_ITEM, -1, -1, 0, 1.0f, PC_NETANG_RESET,
                                            PC_TEMPO_FIXED),

    /* AXE 37..40: Base2(clip, clip, 1, 1, 0.5, -5, STOP, table 0): swing_axe.c_inc:38,45 AXE_SWING1; air_axe.c_inc:11,16
     * AXE_SUKA1; reflect_axe.c_inc:32,39 AXE_HANE1; broken_axe.c_inc:32,54 AXE_BREAK1, then (broken_axe.c_inc:121) the state
     * itself switches to AXE_BREAKWAIT1 via Base1 = REPEAT when AXE_BREAK1 has stopped -> chain_anim. The axe is a plain
     * display list (Phase 1) so there is no item clip. */
    [mPlayer_INDEX_SWING_AXE] = PCROW_BODY("swing_axe", mPlayer_ANIM_AXE_SWING1, PC_STOP, PC_NETANG_RESET),
    [mPlayer_INDEX_AIR_AXE] = PCROW_BODY("air_axe", mPlayer_ANIM_AXE_SUKA1, PC_STOP, PC_NETANG_RESET),
    [mPlayer_INDEX_REFLECT_AXE] = PCROW_BODY("reflect_axe", mPlayer_ANIM_AXE_HANE1, PC_STOP, PC_NETANG_RESET),
    [mPlayer_INDEX_BROKEN_AXE] = PCROW("broken_axe", mPlayer_ANIM_AXE_BREAK1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 1.0f, 0,
                                       mPlayer_ANIM_AXE_BREAKWAIT1, -1, 0, 1.0f, PC_NETANG_RESET, PC_TEMPO_FIXED),

    /* NET 41..48 (net item clips via SetupItem_Base2: only applied when the carried item is a net, Check_ItemAnimationToItemKind
     * item_common.c_inc:412). 41 SLIP_NET slip_net.c_inc:21,30-31: Base1 (REPEAT) KAMAE_SLIP_M1 both layers, table 0, default
     * net item clip. 42 READY_NET ready_net.c_inc:11,16-17: KAMAE_WAIT_M1 REPEAT. 43 READY_WALK_NET ready_walk_net.c_inc:11,
     * 16-17: KAMAE_MOVE_M1 REPEAT with a movement-driven tempo (main: CulcAnimation_Ready_walk_net, speed = 0.252 *
     * sqrt(actor->speed * over_norm / 1.8), min 0.22; over_norm = 1 here). 44 SWING_NET swing_net.c_inc:21,28-30: body
     * NET_SWING1 Base2 STOP 0.5; item ITEM_DATA_NET_SWING in REPEAT mode (SetupItem_Base2 mode arg 1). 45 PULL_NET
     * pull_net.c_inc:21,28-30: body GET_M1 STOP; item NET_GET_M (alias ANIM_HOLD_WAIT1) STOP. 48 PUTAWAY_NET
     * putaway_net.c_inc:15,23-25: body PUTAWAY_M1 STOP; item KAMAE_MAIN_M (alias ANIM_LTURN1) STOP.
     * STOP_NET 46 (only freezes the previous clips, speed 0, stop_net.c_inc:11) and NOTICE_NET 47 (no clip in setup, YATTA2 is
     * started later inside the message state machine, notice_net.c_inc:270) are left to the fallback. */
    [mPlayer_INDEX_SLIP_NET] = PCROW_BODY("slip_net", mPlayer_ANIM_KAMAE_SLIP_M1, PC_REPEAT, PC_NETANG_SLIP),
    [mPlayer_INDEX_READY_NET] = PCROW_BODY("ready_net", mPlayer_ANIM_KAMAE_WAIT_M1, PC_REPEAT, PC_NETANG_READY),
    [mPlayer_INDEX_READY_WALK_NET] = PCROW("ready_walk_net", mPlayer_ANIM_KAMAE_MOVE_M1, PC_ROW_A1_SAME, PC_NORMAL, PC_REPEAT,
                                           0.5f, 1.0f, 0, -1, -1, 0, 1.0f, PC_NETANG_READY_WALK, PC_TEMPO_READY_WALK_NET),
    [mPlayer_INDEX_SWING_NET] = PCROW("swing_net", mPlayer_ANIM_NET_SWING1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 1.0f, 0, -1,
                                      mPlayer_ITEM_DATA_NET_SWING, PC_REPEAT, 1.0f, PC_NETANG_SWING, PC_TEMPO_FIXED),
    [mPlayer_INDEX_PULL_NET] = PCROW("pull_net", mPlayer_ANIM_GET_M1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 1.0f, 0, -1,
                                     mPlayer_ITEM_DATA_NET_GET_M, PC_STOP, 1.0f, PC_NETANG_ZERO, PC_TEMPO_FIXED),
    [mPlayer_INDEX_PUTAWAY_NET] = PCROW("putaway_net", mPlayer_ANIM_PUTAWAY_M1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 1.0f,
                                        0, -1, mPlayer_ITEM_DATA_KAMAE_MAIN_M, PC_STOP, 1.0f, PC_NETANG_RESET,
                                        PC_TEMPO_FIXED),

    /* ROD 49..57 (rod item clips via SetupItem_Base2, applied only for a carried rod). 49 READY_ROD ready_rod.c_inc:11,15-17:
     * body SAO_SWING1 Base2 STOP; item ROD_SWING (alias ANIM_UMBRELLA1). 50 CAST_ROD cast_rod.c_inc:14,23: NO body re-init (the
     * READY clip keeps playing, BODY_CONTINUE; ready_rod hands over at frame >= 10, ready_rod.c_inc request_proc), item
     * ROD_SWING restarted at its current frame (ITEM_CARRY). 51 AIR_ROD air_rod.c_inc:11,20-22: body NOT_SAO_SWING1 from
     * kf0's current frame (BODY_CARRY), item ROD_NOT_SWING (alias ANIM_RUN_SLIP1) from the item's current frame.
     * 52 RELAX_ROD relax_rod.c_inc:11,19-21: body TURI_WAIT1 (eEC_EFFECT_TURI_MIZU == 70, see the assert above) Base2 mode 1
     * REPEAT; item ROD_SINARI (alias ANIM_UMB_OPEN1) REPEAT from frame 180. 53 COLLECT_ROD collect_rod.c_inc:11,15-17: body
     * NOT_GET_T1 STOP; item ROD_GET_T. 54 VIB_ROD vib_rod.c_inc:11,16-18: body TURI_HIKI1 REPEAT; item ROD_SINARI REPEAT from
     * its current frame. 55 FLY_ROD fly_rod.c_inc:11,15-17: body GET_T1 STOP; item ROD_GET_T. 56 NOTICE_ROD
     * notice_rod.c_inc:15,167: body GET_T2 STOP (item clip left as it was: ROD_GET_T; the later YATTA2 of the 'already
     * collected' branch, notice_rod.c_inc:352, is not reproduced). 57 PUTAWAY_ROD putaway_rod.c_inc:17,27-29: body PUTAWAY_T1
     * STOP; item ROD_GET_T. No bobber/line (single UKI actor, out of scope). */
    [mPlayer_INDEX_READY_ROD] = PCROW("ready_rod", mPlayer_ANIM_SAO_SWING1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 1.0f, 0, -1,
                                      mPlayer_ITEM_DATA_ROD_SWING, PC_STOP, 1.0f, PC_NETANG_DEFAULT, PC_TEMPO_FIXED),
    [mPlayer_INDEX_CAST_ROD] = PCROW("cast_rod", mPlayer_ANIM_SAO_SWING1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 10.0f,
                                     PC_ROWF_BODY_CONTINUE | PC_ROWF_ITEM_CARRY, -1, mPlayer_ITEM_DATA_ROD_SWING, PC_STOP, 10.0f,
                                     PC_NETANG_DEFAULT, PC_TEMPO_FIXED),
    [mPlayer_INDEX_AIR_ROD] = PCROW("air_rod", mPlayer_ANIM_NOT_SAO_SWING1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 1.0f,
                                    PC_ROWF_BODY_CARRY | PC_ROWF_ITEM_CARRY, -1, mPlayer_ITEM_DATA_ROD_NOT_SWING, PC_STOP, 1.0f,
                                    PC_NETANG_DEFAULT, PC_TEMPO_FIXED),
    [mPlayer_INDEX_RELAX_ROD] = PCROW("relax_rod", mPlayer_ANIM_TURI_WAIT1, PC_ROW_A1_SAME, PC_NORMAL, PC_REPEAT, 0.5f, 1.0f, 0,
                                      -1, mPlayer_ITEM_DATA_ROD_SINARI, PC_REPEAT, 180.0f, PC_NETANG_DEFAULT, PC_TEMPO_FIXED),
    [mPlayer_INDEX_COLLECT_ROD] = PCROW("collect_rod", mPlayer_ANIM_NOT_GET_T1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 1.0f, 0,
                                        -1, mPlayer_ITEM_DATA_ROD_GET_T, PC_STOP, 1.0f, PC_NETANG_DEFAULT, PC_TEMPO_FIXED),
    [mPlayer_INDEX_VIB_ROD] = PCROW("vib_rod", mPlayer_ANIM_TURI_HIKI1, PC_ROW_A1_SAME, PC_NORMAL, PC_REPEAT, 0.5f, 1.0f,
                                    PC_ROWF_ITEM_CARRY, -1, mPlayer_ITEM_DATA_ROD_SINARI, PC_REPEAT, 1.0f, PC_NETANG_DEFAULT,
                                    PC_TEMPO_FIXED),
    [mPlayer_INDEX_FLY_ROD] = PCROW("fly_rod", mPlayer_ANIM_GET_T1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 1.0f, 0, -1,
                                    mPlayer_ITEM_DATA_ROD_GET_T, PC_STOP, 1.0f, PC_NETANG_DEFAULT, PC_TEMPO_FIXED),
    [mPlayer_INDEX_NOTICE_ROD] = PCROW("notice_rod", mPlayer_ANIM_GET_T2, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 1.0f, 0, -1,
                                       mPlayer_ITEM_DATA_ROD_GET_T, PC_STOP, 1.0f, PC_NETANG_DEFAULT, PC_TEMPO_FIXED),
    [mPlayer_INDEX_PUTAWAY_ROD] = PCROW("putaway_rod", mPlayer_ANIM_PUTAWAY_T1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0.5f, 1.0f, 0,
                                        -1, mPlayer_ITEM_DATA_ROD_GET_T, PC_STOP, 1.0f, PC_NETANG_DEFAULT, PC_TEMPO_FIXED),

    /* SCOOP 58..64: all Base2(clip, clip, 1, 1, 0.5, morph, STOP, table 0), no item clip (the shovel is a display list):
     * dig_scoop.c_inc:16,37 DIG1 (a tree-stump target uses DIG_KABU1 at :31, which the puppet cannot tell: DIG1 is shown);
     * fill_scoop.c_inc:15,22 FILL_UP1; reflect_scoop.c_inc:18,27 NOT_DIG1; air_scoop.c_inc:11,14 DIG_SUKA1; get_scoop.c_inc:16,53
     * GET_D1; putaway_scoop.c_inc:19,29 PUTAWAY_D1; putin_scoop.c_inc:26,39 FILL_UP_I1. */
    [mPlayer_INDEX_DIG_SCOOP] = PCROW_BODY("dig_scoop", mPlayer_ANIM_DIG1, PC_STOP, PC_NETANG_RESET),
    [mPlayer_INDEX_FILL_SCOOP] = PCROW_BODY("fill_scoop", mPlayer_ANIM_FILL_UP1, PC_STOP, PC_NETANG_RESET),
    [mPlayer_INDEX_REFLECT_SCOOP] = PCROW_BODY("reflect_scoop", mPlayer_ANIM_NOT_DIG1, PC_STOP, PC_NETANG_RESET),
    [mPlayer_INDEX_AIR_SCOOP] = PCROW_BODY("air_scoop", mPlayer_ANIM_DIG_SUKA1, PC_STOP, PC_NETANG_RESET),
    [mPlayer_INDEX_GET_SCOOP] = PCROW_BODY("get_scoop", mPlayer_ANIM_GET_D1, PC_STOP, PC_NETANG_RESET),
    [mPlayer_INDEX_PUTAWAY_SCOOP] = PCROW_BODY("putaway_scoop", mPlayer_ANIM_PUTAWAY_D1, PC_STOP, PC_NETANG_RESET),
    [mPlayer_INDEX_PUTIN_SCOOP] = PCROW_BODY("putin_scoop", mPlayer_ANIM_FILL_UP_I1, PC_STOP, PC_NETANG_RESET),

    /* ROTATE_UMBRELLA 103 (m_player_main_rotate_umbrella.c_inc:10-12): Base2(UMB_ROT1, UMB_ROT1, 1, 1, speed 1.0 (not 0.5),
     * morph -5, STOP, PART_TABLE_NORMAL). */
    [mPlayer_INDEX_ROTATE_UMBRELLA] = PCROW("rotate_umbrella", mPlayer_ANIM_UMB_ROT1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 1.0f,
                                            1.0f, 0, -1, -1, 0, 1.0f, PC_NETANG_RESET, PC_TEMPO_FIXED),

    /* SWING_FAN 109 (m_player_main_swing_fan.c_inc:14,29-30): Base2(anim0 UTIWA_D1, anim1 WAIT1, frame0 1, frame1 1 (a
     * continued swing keeps kf1's frame; the puppet restarts it), speed 0.5, morph -5/0, mode REPEAT, PART_TABLE_FAN).
     * TAKEOUT_ITEM 72 / PUTIN_ITEM 73 are NOT filled: the clip depends on the item kind (umbrella UMB_OPEN1/UMB_CLOSE1 vs a
     * reversed/forward PUTAWAY1) and on the item_scale 0<->1 animation plus a mid-state clip switch
     * (takeout_item.c_inc:14,36-42,75-100, putin_item.c_inc:14,32-54,75-88): they stayed PCFB fallbacks in Phase 2b (documented
     * deferral: the carried item pops in/out at full scale at the state boundary). */
    [mPlayer_INDEX_SWING_FAN] = PCROW("swing_fan", mPlayer_ANIM_UTIWA_D1, mPlayer_ANIM_WAIT1, mPlayer_PART_TABLE_FAN, PC_REPEAT,
                                      0.5f, 1.0f, 0, -1, -1, 0, 1.0f, PC_NETANG_RESET, PC_TEMPO_FIXED),

    /* ===== M9-C Phase 2b: the remaining rows. Every index 0..120 is either a filled row (name) or an EXPLICIT fallback (PCFB, with the
     * reason). Filled here (cites in the row comments below): WAIT_BED 27, SITDOWN_WAIT 35, HIDE 81, TIRED 85, SHAKE_TREE 89,
     * STRUGGLE_PITFALL 94, STUNG_BEE 96, REMOVE_GRASS 98, PUSH_SNOWBALL 102, COMPLETE_PAYMENT 105, STUNG_MOSQUITO 107,
     * NOTICE_MOSQUITO 108. States that call cKF_SkeletonInfo_R_AnimationMove_ct_base() are fallbacks: the puppet never runs
     * AnimationMove (animation_enabled == 0), so cKF_Si3_draw_SV_R_child would draw the clip's own root translation/rotation
     * on top of the MOVE-synced position (c_keyframe.c:788-816). ===== */
    [mPlayer_INDEX_DMA] = PCFB("index 0 = no info (also the boot DMA wait, vanilla draw type NONE): nothing to override; cannot be told from 'unknown'"),
    [mPlayer_INDEX_INTRO] = PCFB("m_player_main_intro.c_inc:14-16: Base1 WAIT1 REPEAT speed 0.5 + SetupItem_Base1 pose = the idle fallback (a 4-frame boot state)"),
    [mPlayer_INDEX_REFUSE] = PCFB("m_player_main_refuse.c_inc:14-16: WAIT1 REPEAT 0.5 + item pose = idle fallback (input-blocked stand)"),
    [mPlayer_INDEX_REFUSE_PICKUP] = PCFB("m_player_main_refuse_pickup.c_inc: message/demo-driven refusal stand (setup binds WAIT1-class idle); no distinct clip"),
    [mPlayer_INDEX_RETURN_DEMO] = PCFB("m_player_main_return_demo.c_inc:21-26: WAIT1 REPEAT 0.5 + item pose = idle fallback (scene-return arrival, timer driven)"),
    [mPlayer_INDEX_RETURN_OUTDOOR] = PCFB("m_player_main_return_outdoor.c_inc:23-27: WAIT1 REPEAT 0.5 + item pose = idle fallback (scene-return arrival)"),
    [mPlayer_INDEX_RETURN_OUTDOOR2] = PCFB("m_player_main_return_outdoor2.c_inc:24-29: WAIT1 REPEAT 0.5 + item pose = idle fallback (scene-return arrival)"),
    [mPlayer_INDEX_WAIT] = PCFB("locomotion basic: the move_state mapping (IDLE -> WAIT1 + carried pose, Phase 1/Stage 4B) IS the vanilla clip (wait.c_inc)"),
    [mPlayer_INDEX_WALK] = PCFB("locomotion basic: move_state WALK -> WALK1 + tempo formula + carried pose (Stage 4B, walk.c_inc)"),
    [mPlayer_INDEX_RUN] = PCFB("locomotion basic: move_state RUN -> RUN1 + tempo formula + carried pose (Stage 4B, run.c_inc)"),
    [mPlayer_INDEX_DASH] = PCFB("locomotion basic: move_state DASH -> DASH1 + tempo formula + carried pose (Stage 4B, dash.c_inc)"),
    [mPlayer_INDEX_TURN_DASH] = PCFB("locomotion basic: move_state TURN_DASH -> RUN_SLIP1 at fixed 0.5 + skid sound (Stage 4B.1, turn_dash.c_inc)"),
    [mPlayer_INDEX_FALL] = PCFB("m_player_main_fall.c_inc:26-28: WAIT1 REPEAT 0.5 + item pose = idle fallback (airborne stand; position synced by MOVE)"),
    [mPlayer_INDEX_WADE] = PCFB("m_player_main_wade.c_inc:32-34: WAIT1 REPEAT 0.5 + item pose = idle fallback (the slide across the wade tiles is position-synced)"),
    [mPlayer_INDEX_DOOR] = PCFB("scene transition: clip chosen by door type + AnimationMove root motion (door.c_inc:34, ct_base): would double-apply root translation"),
    [mPlayer_INDEX_OUTDOOR] = PCFB("scene transition: clip/frame from the door animation + AnimationMove root motion (outdoor.c_inc:37)"),
    [mPlayer_INDEX_INVADE] = PCFB("m_player_main_invade.c_inc:19-20: WAIT1 REPEAT 0.5 + item pose (entering a building) = idle fallback"),
    [mPlayer_INDEX_HOLD] = PCFB("interior furniture grab: clip depends on furniture class (hold.c_inc:58,139-151 HOLD_WAIT1/_O1/_H1), not transmitted"),
    [mPlayer_INDEX_PUSH] = PCFB("interior furniture push: AnimationMove root motion (push.c_inc:47, ct_base)"),
    [mPlayer_INDEX_PULL] = PCFB("interior furniture pull: AnimationMove root motion (pull.c_inc:57, ct_base)"),
    [mPlayer_INDEX_ROTATE_FURNITURE] = PCFB("interior furniture: clip by rotation kind (rotate_furniture.c_inc:58), furniture-only"),
    [mPlayer_INDEX_OPEN_FURNITURE] = PCFB("interior furniture: KAGU_OPEN_D/H/K by furniture class + AnimationMove root motion"),
    [mPlayer_INDEX_WAIT_OPEN_FURNITURE] = PCFB("interior furniture: KAGU_WAIT_D/H/K by furniture class (wait_open_furniture.c_inc:37), not transmitted"),
    [mPlayer_INDEX_CLOSE_FURNITURE] = PCFB("interior furniture: KAGU_CLOSE_D/H/K by furniture class + AnimationMove root motion"),
    [mPlayer_INDEX_LIE_BED] = PCFB("interior bed entry: AnimationMove root motion (lie_bed.c_inc, ct_base)"),
    /* WAIT_BED 27 (m_player_main_wait_bed.c_inc:34-35): Base1 BED_WAIT1 on both layers REPEAT 0.5 morph 0, table 0. In place: the state never calls AnimationMove (the lie_bed entry that does is a fallback), settle_main resets animation_enabled via AnimationMove_dt. */
    [mPlayer_INDEX_WAIT_BED] = PCROW_BODY("wait_bed", mPlayer_ANIM_BED_WAIT1, PC_REPEAT, PC_NETANG_RESET),
    [mPlayer_INDEX_ROLL_BED] = PCFB("interior bed turn: AnimationMove root motion (roll_bed.c_inc, ct_base)"),
    [mPlayer_INDEX_STANDUP_BED] = PCFB("interior bed exit: AnimationMove root motion (standup_bed.c_inc, ct_base)"),
    [mPlayer_INDEX_PICKUP_JUMP] = PCFB("room-only (pickup_jump.c_inc:50-68: furniture/room jump pickup, morph 0, item-dependent a1)"),
    [mPlayer_INDEX_PICKUP_FURNITURE] = PCFB("room-only furniture pickup (pickup_furniture.c_inc:37-46)"),
    [mPlayer_INDEX_SITDOWN] = PCFB("interior chair entry: AnimationMove root motion + ftrID (sitdown.c_inc, ct_base)"),
    /* SITDOWN_WAIT 35 (m_player_main_sitdown_wait.c_inc:22-23): Base1 SITDOWN_WAIT1 both layers REPEAT 0.5 table 0, no AnimationMove (same reasoning as wait_bed). The chair entry/exit are root-motion fallbacks. */
    [mPlayer_INDEX_SITDOWN_WAIT] = PCROW_BODY("sitdown_wait", mPlayer_ANIM_SITDOWN_WAIT1, PC_REPEAT, PC_NETANG_RESET),
    [mPlayer_INDEX_STANDUP] = PCFB("interior chair exit: AnimationMove root motion (standup.c_inc, ct_base)"),
    [mPlayer_INDEX_STOP_NET] = PCFB("stop_net.c_inc:15-19: binds no body clip, only freezes the previous clips (speed 0): nothing to bind"),
    [mPlayer_INDEX_NOTICE_NET] = PCFB("notice_net.c_inc:15 setup binds no clip; YATTA2 starts inside the message state machine (:270-278) after the player closes a window"),
    [mPlayer_INDEX_TALK] = PCFB("talk.c_inc:34-36: WAIT1 REPEAT 0.5 + item pose = idle fallback; GAAAN1/2/BIKU1 are switched by demo orders from the talk script (:93-145), not synced"),
    [mPlayer_INDEX_RECIEVE_WAIT] = PCFB("recieve_wait.c_inc:26-33: WAIT1 REPEAT 0.5 + item pose = idle fallback (trade handover, scripted)"),
    [mPlayer_INDEX_RECIEVE_STRETCH] = PCFB("recieve_stretch.c_inc:35-45: clip depends on the traded item (fish GET_F1 / mail RETURN_MAIL1 / GET1) and a surface flag, not synced"),
    [mPlayer_INDEX_RECIEVE] = PCFB("recieve.c_inc:43-45: clip depends on the traded item kind (fish/mail/other), not synced"),
    [mPlayer_INDEX_RECIEVE_PUTAWAY] = PCFB("recieve_putaway.c_inc:42-44: clip depends on the traded item kind, not synced"),
    [mPlayer_INDEX_GIVE] = PCFB("give.c_inc:64-73: clip depends on the traded item (TRANSFER_F1 / SEND_MAIL1 / TRANSFER1), not synced; the item actor is not reproduced"),
    [mPlayer_INDEX_GIVE_WAIT] = PCFB("give_wait.c_inc:35-44: clip depends on the traded item (TRANS_WAIT_F1 / WAIT1 / TRANS_WAIT1), not synced"),
    [mPlayer_INDEX_TAKEOUT_ITEM] = PCFB("takeout_item.c_inc:31-43,75-100: kind-dependent (umbrella UMB_OPEN1 vs reversed PUTAWAY1 = Base3), item_scale 0->1 and a mid-state clip switch; the puppet has no reverse-play or item_scale infrastructure"),
    [mPlayer_INDEX_PUTIN_ITEM] = PCFB("putin_item.c_inc:22-54,75-88: kind from the pocket submenu (not the carried kind), UMB_CLOSE1 vs PUTAWAY1 and an item_scale 1->0 curve; no item_scale infrastructure"),
    [mPlayer_INDEX_DEMO_WAIT] = PCFB("m_player_main_demo_wait.c_inc:38-40: WAIT1 REPEAT 0.5 + item pose = idle fallback (scripted demo)"),
    [mPlayer_INDEX_DEMO_WALK] = PCFB("demo_walk.c_inc:21-41: WALK1 (or WAIT1 near the goal) REPEAT with a demo-driven tempo (CulcAnimation_Demo_walk); scripted, kept on the move_state mapping"),
    [mPlayer_INDEX_DEMO_GETON_TRAIN] = PCFB("scripted train demo with AnimationMove root motion (demo_geton_train.c_inc, ct_base)"),
    [mPlayer_INDEX_DEMO_GETON_TRAIN_WAIT] = PCFB("scripted train demo: INTRAIN_WAIT1 REPEAT (demo_geton_train_wait.c_inc:11), demo-driven; fallback by policy"),
    [mPlayer_INDEX_DEMO_GETOFF_TRAIN] = PCFB("scripted train demo with AnimationMove root motion (demo_getoff_train.c_inc, ct_base)"),
    [mPlayer_INDEX_DEMO_STANDING_TRAIN] = PCFB("demo_standing_train.c_inc:14-15: WAIT1 REPEAT 0.5 + item pose = idle fallback (scripted)"),
    [mPlayer_INDEX_DEMO_WADE] = PCFB("demo_wade.c_inc:27-28: WAIT1 REPEAT 0.5 + item pose = idle fallback (scripted)"),
    /* HIDE 81 (m_player_main_hide.c_inc:10-12 binds no clip; m_player.c Player_actor_draw: draw type table index 81 == mPlayer_DRAW_TYPE_NONE): the row exists only for the HIDE_BODY flag (body + item not drawn). Cleared by the usual scene/snap/gap/edge releases. */
    [mPlayer_INDEX_HIDE] = PCROW("hide", mPlayer_ANIM_WAIT1, PC_ROW_A1_SAME, PC_NORMAL, PC_REPEAT, 0.5f, 1.0f, PC_ROWF_HIDE_BODY, -1, -1, 0, 1.0f, PC_NETANG_RESET, PC_TEMPO_FIXED),
    [mPlayer_INDEX_GROUNDHOG] = PCFB("groundhog.c_inc:14-16: WAIT1 REPEAT 0.5 + item pose = idle fallback (New Year's event stand)"),
    [mPlayer_INDEX_RELEASE_CREATURE] = PCFB("release_creature.c_inc:90-92: WAIT1 REPEAT 0.5 + item pose = idle fallback; the balloon/creature release is a scripted actor interaction"),
    [mPlayer_INDEX_WASH_CAR] = PCFB("wash_car.c_inc:18-37,59-79: scripted car-wash event (position set from the car actor, WASH1..5 clips cycled by an internal counter/effect flag, speed starts 0)"),
    /* TIRED 85 (m_player_main_tired.c_inc:11-12): Base1 TIRED1 both layers REPEAT 0.5 morph 0 table NORMAL (the frame-10 araiiki sound + dust effect are presented by the Phase 6b tool table; eye/mouth patterns are not reproduced). */
    [mPlayer_INDEX_TIRED] = PCROW_BODY("tired", mPlayer_ANIM_TIRED1, PC_REPEAT, PC_NETANG_RESET),
    [mPlayer_INDEX_ROTATE_OCTAGON] = PCFB("AnimationMove root motion (rotate_octagon.c_inc:37, ct_base), event-prop specific"),
    [mPlayer_INDEX_THROW_MONEY] = PCFB("AnimationMove root motion (throw_money.c_inc:9-15, ct_base): the SAISEN1 clip carries XZ travel that the synced position already contains"),
    [mPlayer_INDEX_PRAY] = PCFB("AnimationMove root motion (pray.c_inc:9-15, ct_base): the OMAIRI_US1 clip carries XZ travel that the synced position already contains"),
    /* SHAKE_TREE 89 (m_player_main_shake_tree.c_inc:33-35): SetupItem_Base1(SHAKE1) + Base2(SHAKE1, anim1 = item pose, 1, 1, 0.5, morph -5, STOP, part table from SetupItem). The tree shake itself fires at SHAKE1 frame 10 (:55, Check_AnimationFrame 10.0): Phase 4 keys the canopy effect on this row at kf0 frame 10. */
    [mPlayer_INDEX_SHAKE_TREE] = PCROW_ITEM("shake_tree", mPlayer_ANIM_SHAKE1, PC_NORMAL, PC_STOP, PC_ROWF_PT_FROM_ANIM1, -1, PC_NETANG_RESET),
    [mPlayer_INDEX_MAIL_JUMP] = PCFB("AnimationMove root motion (mail_jump.c_inc:33, ct_base)"),
    [mPlayer_INDEX_MAIL_LAND] = PCFB("mail_land.c_inc:14-20: Base3 = reversed CONFIRM1 playback; the puppet has no reverse-play support"),
    [mPlayer_INDEX_READY_PITFALL] = PCFB("AnimationMove root motion (ready_pitfall.c_inc:31-36, ct_base XZ+Y) "),
    [mPlayer_INDEX_FALL_PITFALL] = PCFB("AnimationMove root motion (fall_pitfall.c_inc, ct_base): OTIRU1/2 would double-apply the fall translation"),
    /* STRUGGLE_PITFALL 94 (m_player_main_struggle_pitfall.c_inc:19-21): Base1 MOGAKU1 on BOTH layers (anim1 is NOT the item pose here), REPEAT 0.5, table NORMAL. No AnimationMove (the entry/exit pitfall states have it). The clip speed is input driven 0.5..1.0 (:34-87), not synced: 0.5 is shown. */
    [mPlayer_INDEX_STRUGGLE_PITFALL] = PCROW_BODY("struggle_pitfall", mPlayer_ANIM_MOGAKU1, PC_REPEAT, PC_NETANG_RESET),
    [mPlayer_INDEX_CLIMBUP_PITFALL] = PCFB("AnimationMove root motion (climbup_pitfall.c_inc, ct_base): DERU1/2 climb-out translation"),
    /* STUNG_BEE 96 (m_player_main_stung_bee.c_inc:27-29, 56-67): SetupItem_Base1(HATI1) + Base2(HATI1, item pose, 0.5, STOP, part table from SetupItem); when HATI1 has stopped the state binds HATI2 the same way (STOP, so it holds its last frame) -> chain_anim HATI2 with CHAIN_STOP. mNpc/swell-face side effects at HATI2 frame 21 (:77-82) are not reproduced. */
    [mPlayer_INDEX_STUNG_BEE] = PCROW_ITEM("stung_bee", mPlayer_ANIM_HATI1, PC_NORMAL, PC_STOP, PC_ROWF_PT_FROM_ANIM1 | PC_ROWF_CHAIN_STOP, mPlayer_ANIM_HATI2, PC_NETANG_RESET),
    [mPlayer_INDEX_NOTICE_BEE] = PCFB("notice_bee.c_inc:20-25: AnimationMove ROT_Y base rotation (ct_base) replaces the root rotation: not reproducible without per-frame AnimationMove_base"),
    /* REMOVE_GRASS 98 (m_player_main_remove_grass.c_inc:25-27): SetupItem_Base1(ZASSOU1) + Base2(ZASSOU1, item pose, 1, 1, 0.5, morph -6, STOP, PART_TABLE_PICK_UP (explicit, not the item's)). The frame-17 weed-pull presentation (grass_mud effect + zassou_nuku sound, only while the target tile still holds grass) is the Phase 6b tool table; the weed removal itself is a world change and stays host-authoritative. */
    [mPlayer_INDEX_REMOVE_GRASS] = PCROW_ITEM("remove_grass", mPlayer_ANIM_ZASSOU1, mPlayer_PART_TABLE_PICK_UP, PC_STOP, 0, -1, PC_NETANG_RESET),
    [mPlayer_INDEX_SHOCK] = PCFB("shock.c_inc:34-36,84-114: WAIT1 for a REQUEST-dependent start_time (20/24/60 ticks, not synced) then GAAAN1 -> GAAAN2: time-driven phases the wire cannot reproduce"),
    [mPlayer_INDEX_KNOCK_DOOR] = PCFB("AnimationMove root motion (knock_door.c_inc:27-32, ct_base)"),
    [mPlayer_INDEX_CHANGE_CLOTH] = PCFB("change_cloth.c_inc:49-62: clip/speed by try_on_flag (MENU_CHANGE1 @1.0 vs ITAZURA1 @0.5), not synced"),
    /* PUSH_SNOWBALL 102 (m_player_main_push_snowball.c_inc:43-50): SetupItem_Base1(PUSH_YUKI1), part table from the item pose but forced to NET when anim1 != PUSH_YUKI1; Base1 REPEAT, start frames 0.0/0.0 (not 1.0), speed 0.5. Net carried angle: dummy_net table entry 102 = walk (m_player_item_net.c_inc table, 2 entries/line from index 0 at line 8 -> line 59). */
    [mPlayer_INDEX_PUSH_SNOWBALL] = PCROW("push_snowball", mPlayer_ANIM_PUSH_YUKI1, PC_ROW_A1_ITEM, PC_NORMAL, PC_REPEAT, 0.5f, 0.0f, PC_ROWF_PT_FROM_ANIM1 | PC_ROWF_PT_NET_IF_ITEM, -1, -1, 0, 1.0f, PC_NETANG_WALK, PC_TEMPO_FIXED),
    [mPlayer_INDEX_WADE_SNOWBALL] = PCFB("wade_snowball.c_inc:19-69: setup binds NO clip (keeps the push_snowball clip until a crash switches to WAIT1): previous-clip dependent"),
    /* COMPLETE_PAYMENT 105 (m_player_main_complete_payment.c_inc:22-23, the #else branch: the PC build is VERSION=0 < VER_GAFU01_00, whose branch :19 would use 0.6): Base2 YATTA1 both layers STOP speed 0.5 morph 0 table NORMAL. Net carried angle table entry 105 = zero (item_net.c_inc table line 60). */
    [mPlayer_INDEX_COMPLETE_PAYMENT] = PCROW_BODY("complete_payment", mPlayer_ANIM_YATTA1, PC_STOP, PC_NETANG_ZERO),
    [mPlayer_INDEX_FAIL_EMU] = PCFB("fail_emu.c_inc:17-18: WAIT1 REPEAT 0.5 = idle fallback (message-driven stand)"),
    /* STUNG_MOSQUITO 107 (m_player_main_stung_mosquito.c_inc:47-49): SetupItem_Base1(MOSQUITO1) + Base2(MOSQUITO1, item pose, 1, 1, 0.5, -5, STOP, PART_TABLE_PICK_UP (explicit)). */
    [mPlayer_INDEX_STUNG_MOSQUITO] = PCROW_ITEM("stung_mosquito", mPlayer_ANIM_MOSQUITO1, mPlayer_PART_TABLE_PICK_UP, PC_STOP, 0, -1, PC_NETANG_RESET),
    /* NOTICE_MOSQUITO 108 (m_player_main_notice_mosquito.c_inc:25-27): SetupItem_Base1(MOSQUITO2) + Base1 (REPEAT) MOSQUITO2, item pose on layer 1, 0.5, explicit PART_TABLE_PICK_UP. (Idle message state: the steady pose repeats until the state ends.) */
    [mPlayer_INDEX_NOTICE_MOSQUITO] = PCROW_ITEM("notice_mosquito", mPlayer_ANIM_MOSQUITO2, mPlayer_PART_TABLE_PICK_UP, PC_REPEAT, 0, -1, PC_NETANG_RESET),
    [mPlayer_INDEX_SWITCH_ON_LIGHTHOUSE] = PCFB("AnimationMove root motion (switch_on_lighthouse.c_inc:38, ct_base), lighthouse-island scripted"),
    [mPlayer_INDEX_RADIO_EXERCISE] = PCFB("radio_exercise.c_inc:15-65,83-130: clip chosen by the radio command and speed by the radio tempo/sound frame counter, both local audio state"),
    [mPlayer_INDEX_DEMO_GETON_BOAT] = PCFB("scripted boat demo with AnimationMove root motion (demo_geton_boat.c_inc, ct_base)"),
    [mPlayer_INDEX_DEMO_GETON_BOAT_SITDOWN] = PCFB("scripted boat demo: RIDE2 (demo_geton_boat_sitdown.c_inc:16-18), boat-actor coupled"),
    [mPlayer_INDEX_DEMO_GETON_BOAT_WAIT] = PCFB("scripted boat demo: RIDEWAIT REPEAT (demo_geton_boat_wait.c_inc:15), boat-actor coupled"),
    [mPlayer_INDEX_DEMO_GETON_BOAT_WADE] = PCFB("scripted boat demo: RIDEWAIT REPEAT (demo_geton_boat_wade.c_inc:28), boat-actor coupled"),
    [mPlayer_INDEX_DEMO_GETOFF_BOAT_STANDUP] = PCFB("scripted boat demo with AnimationMove root motion (demo_getoff_boat_standup.c_inc, ct_base)"),
    [mPlayer_INDEX_DEMO_GETOFF_BOAT] = PCFB("scripted boat demo with AnimationMove root motion (demo_getoff_boat.c_inc, ct_base)"),
    [mPlayer_INDEX_DEMO_GET_GOLDEN_ITEM] = PCFB("scripted golden-tool demo: clip chosen by the tool (demo_get_golden_item.c_inc:45-53)"),
    [mPlayer_INDEX_DEMO_GET_GOLDEN_ITEM2] = PCFB("scripted golden-tool demo (demo_get_golden_item2.c_inc)"),
    [mPlayer_INDEX_DEMO_GET_GOLDEN_AXE_WAIT] = PCFB("demo_get_golden_axe_wait.c_inc:19-21: WAIT1 REPEAT 0.5 + item pose = idle fallback (scripted)"),
};

/* kind -> tumble clip class, copied from the vanilla tables (m_player_main_tumble.c_inc:42 / tumble_getup.c_inc:58, which
 * are identical): 'A' = KOKERU_A1 / KOKERU_GETUP_A1 (axes 0..8, shovels 53,54, fans 71..78), 'N' = KOKERU_N1 /
 * KOKERU_GETUP_N1 (everything else 0..78: nets, umbrellas, rods, balloons, pinwheels). Anything else (kind -1 / out of range):
 * KOKERU1 / KOKERU_GETUP1. */
static const char s_tumble_class[] =
    "AAAAAAAAANNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNAANNNNNNNNNNNNNNNNAAAAAAAA";
_Static_assert(sizeof(s_tumble_class) - 1 == mPlayer_ITEM_KIND_NUM, "tumble class table must cover every item kind");

static int pc_remote_player_row_anim0(const PCStateRow* row, int raw_kind) {
    if (row->anim0 == PC_ROW_ANIM_TUMBLE || row->anim0 == PC_ROW_ANIM_TUMBLE_GETUP) {
        int getup = (row->anim0 == PC_ROW_ANIM_TUMBLE_GETUP);
        if (raw_kind >= 0 && raw_kind < mPlayer_ITEM_KIND_NUM) {
            if (s_tumble_class[raw_kind] == 'A') {
                return getup ? mPlayer_ANIM_KOKERU_GETUP_A1 : mPlayer_ANIM_KOKERU_A1;
            }
            return getup ? mPlayer_ANIM_KOKERU_GETUP_N1 : mPlayer_ANIM_KOKERU_N1;
        }
        return getup ? mPlayer_ANIM_KOKERU_GETUP1 : mPlayer_ANIM_KOKERU1;
    }
    return row->anim0;
}

/* The row of a wire main index, or NULL (fallback). Index 0 (DMA) and out-of-range never have a row. */
static const PCStateRow* pc_remote_player_row_for(int idx) {
    if (idx <= 0 || idx >= mPlayer_INDEX_NUM || s_state_rows[idx].name == NULL) {
        return NULL;
    }
    return &s_state_rows[idx];
}

/* Catalog umbrella display lists: the same {handle, canopy} pairs ac_t_umbrella.c's draw_dt[] and the inventory
 * preview use, indexed by (kind - mPlayer_ITEM_KIND_UMBRELLA00) == tool_name. */
extern Gfx e_umb01_model[];
extern Gfx kasa_umb01_model[];
extern Gfx e_umb02_model[];
extern Gfx kasa_umb02_model[];
extern Gfx e_umb03_model[];
extern Gfx kasa_umb03_model[];
extern Gfx e_umb04_model[];
extern Gfx kasa_umb04_model[];
extern Gfx e_umb05_model[];
extern Gfx kasa_umb05_model[];
extern Gfx e_umb06_model[];
extern Gfx kasa_umb06_model[];
extern Gfx e_umb07_model[];
extern Gfx kasa_umb07_model[];
extern Gfx e_umb08_model[];
extern Gfx kasa_umb08_model[];
extern Gfx e_umb09_model[];
extern Gfx kasa_umb09_model[];
extern Gfx e_umb10_model[];
extern Gfx kasa_umb10_model[];
extern Gfx e_umb11_model[];
extern Gfx kasa_umb11_model[];
extern Gfx e_umb12_model[];
extern Gfx kasa_umb12_model[];
extern Gfx e_umb13_model[];
extern Gfx kasa_umb13_model[];
extern Gfx e_umb14_model[];
extern Gfx kasa_umb14_model[];
extern Gfx e_umb15_model[];
extern Gfx kasa_umb15_model[];
extern Gfx e_umb16_model[];
extern Gfx kasa_umb16_model[];
extern Gfx e_umb17_model[];
extern Gfx kasa_umb17_model[];
extern Gfx e_umb18_model[];
extern Gfx kasa_umb18_model[];
extern Gfx e_umb19_model[];
extern Gfx kasa_umb19_model[];
extern Gfx e_umb20_model[];
extern Gfx kasa_umb20_model[];
extern Gfx e_umb21_model[];
extern Gfx kasa_umb21_model[];
extern Gfx e_umb22_model[];
extern Gfx kasa_umb22_model[];
extern Gfx e_umb23_model[];
extern Gfx kasa_umb23_model[];
extern Gfx e_umb24_model[];
extern Gfx kasa_umb24_model[];
extern Gfx e_umb25_model[];
extern Gfx kasa_umb25_model[];
extern Gfx e_umb26_model[];
extern Gfx kasa_umb26_model[];
extern Gfx e_umb27_model[];
extern Gfx kasa_umb27_model[];
extern Gfx e_umb28_model[];
extern Gfx kasa_umb28_model[];
extern Gfx e_umb29_model[];
extern Gfx kasa_umb29_model[];
extern Gfx e_umb30_model[];
extern Gfx kasa_umb30_model[];
extern Gfx e_umb31_model[];
extern Gfx kasa_umb31_model[];
extern Gfx e_umb32_model[];
extern Gfx kasa_umb32_model[];
typedef struct PCRemoteUmbrellaModel {
    Gfx* model_e;
    Gfx* model_kasa;
} PCRemoteUmbrellaModel;

static const PCRemoteUmbrellaModel s_remote_umbrella_models[32] = {
    { e_umb01_model, kasa_umb01_model },
    { e_umb02_model, kasa_umb02_model },
    { e_umb03_model, kasa_umb03_model },
    { e_umb04_model, kasa_umb04_model },
    { e_umb05_model, kasa_umb05_model },
    { e_umb06_model, kasa_umb06_model },
    { e_umb07_model, kasa_umb07_model },
    { e_umb08_model, kasa_umb08_model },
    { e_umb09_model, kasa_umb09_model },
    { e_umb10_model, kasa_umb10_model },
    { e_umb11_model, kasa_umb11_model },
    { e_umb12_model, kasa_umb12_model },
    { e_umb13_model, kasa_umb13_model },
    { e_umb14_model, kasa_umb14_model },
    { e_umb15_model, kasa_umb15_model },
    { e_umb16_model, kasa_umb16_model },
    { e_umb17_model, kasa_umb17_model },
    { e_umb18_model, kasa_umb18_model },
    { e_umb19_model, kasa_umb19_model },
    { e_umb20_model, kasa_umb20_model },
    { e_umb21_model, kasa_umb21_model },
    { e_umb22_model, kasa_umb22_model },
    { e_umb23_model, kasa_umb23_model },
    { e_umb24_model, kasa_umb24_model },
    { e_umb25_model, kasa_umb25_model },
    { e_umb26_model, kasa_umb26_model },
    { e_umb27_model, kasa_umb27_model },
    { e_umb28_model, kasa_umb28_model },
    { e_umb29_model, kasa_umb29_model },
    { e_umb30_model, kasa_umb30_model },
    { e_umb31_model, kasa_umb31_model },
    { e_umb32_model, kasa_umb32_model },
};

#define PC_NET_ANGLE_FRAC 0.2f

static void pc_remote_player_net_angle_step(s_xyz* angle, const s_xyz* base) {
    /* identical to Player_actor_Item_net_CulcJointAngle_dummy_net_base(angle, base, 2730, 100, 0.2f) */
    f32 frac = 1.0f - sqrtf(1.0f - PC_NET_ANGLE_FRAC);
    add_calc_short_angle2(&angle->x, base->x, frac, 2730 >> 1, 100 >> 1);
    add_calc_short_angle2(&angle->y, base->y, frac, 2730 >> 1, 100 >> 1);
    add_calc_short_angle2(&angle->z, base->z, frac, 2730 >> 1, 100 >> 1);
}

/* Per-frame net carried angle from the puppet's (interpolated) move state; mirrors m_player_item_net.c_inc:
 * dummy_net_reset (idle), dummy_net_walk (walk/run/dash, blended by keyframe0's playback speed exactly like
 * dummy_net_common minus the turn-rate term) and dummy_net_turn (skid). */
static void pc_remote_player_net_angle_update(PCRemotePlayerActor* self) {
    static const s_xyz reset_angle = { 0, 182, -7281 };
    s_xyz base;
    const PCStateRow* row = self->visual.row_active ? pc_remote_player_row_for(self->visual.row_idx) : NULL;
    int row_mode = (row != NULL) ? (int)row->net_angle : PC_NETANG_DEFAULT;

    /* M9-C Phase 2a: a state row selects the vanilla per-main-index branch of the dummy_net table (m_player_item_net.c_inc:
     * 125): reset / ready (0,364,-11832) / ready_walk (tempo blend) / slip (z -65deg) / swing (ready until frame 5, then
     * zero) / zero. DEFAULT keeps the Phase 1 move-state mapping below. */
    if (row_mode != PC_NETANG_DEFAULT) {
        switch (row_mode) {
            case PC_NETANG_READY:
                base.x = 0; base.y = 364; base.z = -11832;
                break;
            case PC_NETANG_SLIP:
                base.x = 0; base.y = 0; base.z = (s16)(int)DEG2SHORT_ANGLE3(-65.0f);
                break;
            case PC_NETANG_SWING:
                if (self->visual.keyframe0.frame_control.current_frame >= 5.0f) {
                    base.x = 0; base.y = 0; base.z = 0;
                } else {
                    base.x = 0; base.y = 364; base.z = -11832;
                }
                break;
            case PC_NETANG_READY_WALK: {
                f32 sp = (self->visual.keyframe0.frame_control.speed - 0.22f) / 0.37999996f;
                f32 start_y = DEG2SHORT_ANGLE3(2.0f);
                f32 start_z = DEG2SHORT_ANGLE3(-65.0f);
                if (sp > 1.0f) {
                    sp = 1.0f;
                } else if (sp < 0.0f) {
                    sp = 0.0f;
                }
                base.x = 0;
                base.y = (s16)(int)(start_y + (sp * (0.0f - start_y)));
                base.z = (s16)(int)(start_z + (sp * (0.0f - start_z)));
                break;
            }
            case PC_NETANG_ZERO:
                base.x = 0; base.y = 0; base.z = 0;
                break;
            case PC_NETANG_WALK: { /* dummy_net_walk (push_snowball): the Phase 1 walk blend by keyframe0's tempo */
                f32 sp = (self->visual.keyframe0.frame_control.speed - 0.22f) / 0.37999996f;
                f32 start_y = DEG2SHORT_ANGLE3(1.0f);
                f32 start_z = DEG2SHORT_ANGLE3(-40.0f);
                if (sp > 1.0f) {
                    sp = 1.0f;
                } else if (sp < 0.0f) {
                    sp = 0.0f;
                }
                base.x = 0;
                base.y = (s16)(int)(start_y + (sp * (0.0f - start_y)));
                base.z = (s16)(int)(start_z + (sp * (0.0f - start_z)));
                break;
            }
            default: /* PC_NETANG_RESET */
                base = reset_angle;
                break;
        }
        pc_remote_player_net_angle_step(&self->visual.net_angle, &base);
        return;
    }

    switch (self->cosmetic_move_state) {
        case PC_MOVE_STATE_WALK:
        case PC_MOVE_STATE_RUN:
        case PC_MOVE_STATE_DASH: {
            f32 sp = (self->visual.keyframe0.frame_control.speed - 0.22f) / 0.37999996f;
            f32 start_y = DEG2SHORT_ANGLE3(1.0f);
            f32 start_z = DEG2SHORT_ANGLE3(-40.0f);
            if (sp > 1.0f) {
                sp = 1.0f;
            } else if (sp < 0.0f) {
                sp = 0.0f;
            }
            base.x = 0;
            base.y = (s16)(int)(start_y + (sp * (0.0f - start_y)));
            base.z = (s16)(int)(start_z + (sp * (0.0f - start_z)));
            break;
        }
        case PC_MOVE_STATE_TURN_DASH:
            base.x = 0;
            base.y = (s16)(int)DEG2SHORT_ANGLE3(-25.0f);
            base.z = (s16)(int)DEG2SHORT_ANGLE3(-25.0f);
            break;
        default:
            base = reset_angle;
            break;
    }
    pc_remote_player_net_angle_step(&self->visual.net_angle, &base);
}

/* (Re)builds the puppet-owned net/rod skeleton instance for `kind`. Mirrors Player_actor_Item_DMA_Data():
 * ct against the item skeleton with the puppet's own joint/morph work buffers, then a repeating item clip at 0.5x
 * (kamae_main_m for nets; ROD_WAIT for the idle SetupItem_Base1 and ROD_MOVE for the walk/run/dash
 * SetupItem_Base3 for rods). On any missing pointer item_ready stays 0 and nothing is drawn. */
static void pc_remote_player_item_skeleton_setup(PCRemotePlayerVisual* v, int kind, int cls, int moving) {
    int shape = mPlib_Get_BasicItemShapeIndex_fromItemKind(kind);
    cKF_Skeleton_R_c* skl = (cKF_Skeleton_R_c*)mPlib_Get_Item_DataPointer(shape);
    int anim_idx;
    cKF_Animation_R_c* anim;

    v->item_ready = 0;
    if (cls == PC_HELD_NET) {
        anim_idx = mPlayer_ITEM_DATA_KAMAE_MAIN_M;
    } else {
        anim_idx = moving ? mPlayer_ITEM_DATA_ROD_MOVE : mPlayer_ITEM_DATA_ROD_WAIT;
    }
    anim = (cKF_Animation_R_c*)mPlib_Get_Item_DataPointer(anim_idx);
    if (skl == NULL || anim == NULL) {
        return;
    }
    memset(v->item_joint_data, 0, sizeof(v->item_joint_data));
    memset(v->item_morph_data, 0, sizeof(v->item_morph_data));
    cKF_SkeletonInfo_R_ct(&v->item_keyframe, skl, NULL, v->item_joint_data, v->item_morph_data);
    cKF_SkeletonInfo_R_init_standard_setframeandspeedandmorphandmode(&v->item_keyframe, anim, NULL, 1.0f, 0.5f, 0.0f,
                                                                     cKF_FRAMECONTROL_REPEAT);
    v->item_anim_idx = anim_idx;
    v->net_angle.x = 0;
    v->net_angle.y = 182;
    v->net_angle.z = -7281;
    v->item_ready = 1;
}

/* Puppet-owned cKF_Si3_draw_R_SV after-callback (signature = cKF_draw_callback). `arg` is ALWAYS the
 * PCRemotePlayerActor passed by pc_remote_player_dw(); it is never cast to PLAYER_ACTOR. Phase 3 also captures the
 * left/right foot (joints LFOOT3/RFOOT3) position + angle. Captures the world matrix
 * of the right-hand joint (mPlayer_JOINT_HAND == 20, the joint the vanilla Player_actor_draw_After_hand,
 * m_player_draw.c_inc, and mIV_pl_shape_after_draw, m_inventory_ovl.c, both use) into the puppet's own MtxF. */
static int pc_remote_player_draw_after(GAME* game, cKF_SkeletonInfo_R_c* kf, int joint_no, Gfx** gfx_pp, u8* work_flag,
                                       void* arg, s_xyz* rot, xyz_t* pos) {
    PCRemotePlayerActor* self = (PCRemotePlayerActor*)arg;

    if (joint_no == mPlayer_JOINT_HAND && self != NULL) {
        Matrix_get(&self->hand_mtx);
        self->hand_mtx_valid = 1;
    } else if (joint_no == mPlayer_JOINT_LARM2 && self != NULL) {
        /* M9-C Phase 5: Player_actor_draw_After_Larm2 (m_player_draw.c_inc:95) left_hand_pos, into this puppet's storage. */
        Matrix_Position_VecX(1100.0f, &self->cos.hand_pos);
        self->cos.hand_frame = game->frame_counter + 1u;
    } else if (joint_no == mPlayer_JOINT_FEEL && self != NULL) {
        /* M9-C Phase 6b: Player_actor_draw_After_feel (m_player_draw.c_inc:106) feel_pos = Matrix_Position_Zero, into this
         * puppet's storage only. The vanilla body (draw_effect_idx effect, wash-car effect) is NOT run: the puppet spawns
         * those effects itself from its move (pc_puppet_tool_tick), budgeted. */
        Matrix_Position_Zero(&self->cos.feel_pos);
        self->cos.feel_frame = game->frame_counter + 1u;
    } else if ((joint_no == mPlayer_JOINT_LFOOT3 || joint_no == mPlayer_JOINT_RFOOT3) && self != NULL) {
        /* M9-C Phase 3: same conversion as Player_actor_draw_After_{L,R}foot3 -> Player_actor_draw_After_Culc_FootMarkPos
         * (m_player_draw.c_inc:60-78,99-104), but into this puppet's own storage. */
        int fi = (joint_no == mPlayer_JOINT_RFOOT3) ? 1 : 0;
        s_xyz frot;

        Matrix_Position_Zero(&self->cos.foot_pos[fi]);
        Matrix_to_rotate_new(get_Matrix_now(), &frot, MTX_LOAD);
        self->cos.foot_angle_y[fi] = frot.y;
        self->cos.foot_frame[fi] = game->frame_counter + 1u;
    }
    return TRUE;
}

/* Net skeleton after-callback: joint 3 gets the carried angle (Player_actor_Item_draw_net_After_dummy_net). */
static int pc_remote_player_item_net_after(GAME* game, cKF_SkeletonInfo_R_c* kf, int joint_no, Gfx** gfx_pp,
                                           u8* work_flag, void* arg, s_xyz* rot, xyz_t* pos) {
    PCRemotePlayerActor* self = (PCRemotePlayerActor*)arg;

    if (joint_no == 3 && self != NULL) {
        Matrix_rotateXYZ(self->visual.net_angle.x, self->visual.net_angle.y, self->visual.net_angle.z, MTX_MULT);
    }
    return TRUE;
}

/* Draws the carried item at the captured hand matrix. Structure follows Player_actor_Item_draw()
 * (m_player_item.c_inc): Matrix_push / Matrix_put(hand) / _Matrix_to_Mtx_new / _texture_z_light_fog_prim /
 * gSPMatrix, then the per-kind draw (item_scale is 1.0 outside take-out/put-away, so no scale is applied). */
static void pc_remote_player_draw_item(PCRemotePlayerActor* self, GAME* game) {
    GRAPH* graph = game->graph;
    PCRemotePlayerVisual* v = &self->visual;
    int kind = v->held_kind;
    int cls = v->held_class;
    Mtx* mtx;

    if (cls == PC_HELD_NONE || cls == PC_HELD_DEFERRED || !self->hand_mtx_valid || v->item_hidden) {
        return;
    }

    Matrix_push();
    Matrix_put(&self->hand_mtx);

    if (cls == PC_HELD_UMBRELLA) {
        /* Vanilla: the T_UMBRELLA child actor (ac_t_umbrella.c, aTUMB_actor_draw) receives this same hand matrix and
         * draws handle then canopy; in the steady carried state (OPENING clip at its last frame 26) the handle scale
         * is (1,1,1) and the canopy scale (0.9,1,1). Reproduced statically; no actor, no animation. */
        int idx = kind - mPlayer_ITEM_KIND_UMBRELLA00;
        if (idx >= 0 && idx < 32) {
            Mtx* e_mtx;
            Mtx* k_mtx;

            _texture_z_light_fog_prim_npc(graph);
            Matrix_rotateXYZ(0, -0x4000, 0, MTX_MULT);
            e_mtx = _Matrix_to_Mtx_new(graph);
            Matrix_translate(4500.0f, 0.0f, 0.0f, MTX_MULT);
            Matrix_scale(0.9f, 1.0f, 1.0f, MTX_MULT);
            k_mtx = _Matrix_to_Mtx_new(graph);
            if (e_mtx != NULL && k_mtx != NULL) {
                OPEN_POLY_OPA_DISP(graph);
                gSPMatrix(POLY_OPA_DISP++, e_mtx, G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
                gSPDisplayList(POLY_OPA_DISP++, s_remote_umbrella_models[idx].model_e);
                gSPMatrix(POLY_OPA_DISP++, k_mtx, G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
                gSPDisplayList(POLY_OPA_DISP++, s_remote_umbrella_models[idx].model_kasa);
                CLOSE_POLY_OPA_DISP(graph);
            }
        }
        Matrix_pull();
        return;
    }

    mtx = _Matrix_to_Mtx_new(graph);
    if (mtx != NULL) {
        _texture_z_light_fog_prim(graph);

        OPEN_POLY_OPA_DISP(graph);
        gSPMatrix(POLY_OPA_DISP++, mtx, G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
        CLOSE_POLY_OPA_DISP(graph);

        switch (cls) {
            case PC_HELD_AXE:
            case PC_HELD_SHOVEL:
            case PC_HELD_FAN: {
                /* Pure display lists (Player_actor_Item_draw_axe/_scoop/_fan): the vanilla position-only side
                 * effects (axe_pos / scoop_pos) feed collision and are not needed. */
                Gfx* dl = (Gfx*)mPlib_Get_Item_DataPointer(mPlib_Get_BasicItemShapeIndex_fromItemKind(kind));
                if (dl != NULL) {
                    OPEN_POLY_OPA_DISP(graph);
                    gSPDisplayList(POLY_OPA_DISP++, dl);
                    CLOSE_POLY_OPA_DISP(graph);
                }
                break;
            }
            case PC_HELD_NET:
            case PC_HELD_ROD:
                if (v->item_ready && v->item_keyframe.skeleton != NULL) {
                    Mtx* item_mtx = (Mtx*)GRAPH_ALLOC_TYPE(graph, Mtx, v->item_keyframe.skeleton->num_shown_joints);
                    if (item_mtx != NULL) {
                        if (cls == PC_HELD_NET) {
                            /* M9-C Phase 6a: PLAYER_ACTOR::net_pos exactly as Player_actor_Item_draw_net (m_player_item_net.c_inc:
                             * 243-249) derives it from the hand matrix; stored in THIS puppet only (net sounds). */
                            Matrix_push();
                            Matrix_rotateXYZ(0, 3000, 0, MTX_MULT);
                            Matrix_Position_VecZ(4000.0f, &self->cos.net_pos);
                            Matrix_pull();
                            self->cos.net_frame = game->frame_counter + 1u;
                        }
                        /* Net: Player_actor_Item_draw_net; rod: Player_actor_Item_draw_rod with item_rod_angle_z == 0
                         * (its only writer is the ready-rod state), so no extra rotation/matrix is needed. The
                         * rod's top-position callback only feeds the bobber and is not needed. */
                        cKF_Si3_draw_R_SV(game, &v->item_keyframe, item_mtx, NULL,
                                          (cls == PC_HELD_NET) ? pc_remote_player_item_net_after : NULL, self);
                    }
                }
                break;
            default:
                break;
        }
    }
    Matrix_pull();
}


/* ------------------------------------------------------------------------------------------------------------
 * M9-C Phase 2a: puppet state machine over the (main_index, entry counter) pair of the interpolated snapshot.
 *
 *  - An ENTRY EDGE is any change of the pair (or of its validity). Compared by equality only (the 4-bit counter wraps).
 *    Same pair = the state continues (nothing restarts); a different counter on the same index = a re-entry (repeated
 *    swing/dig) and restarts the clip.
 *  - An edge into a state that has a row starts that row at once (interrupting any latched one-shot).
 *  - ONE-SHOT LATCH: a STOP-mode row keeps playing until its clip FINISHED even if a later edge already reports a state
 *    without a row (idle/walk/run/dash/other = fallback): the fallback only takes over after the clip finished, or after
 *    PC_REMOTE_PLAYER_LATCH_TIMEOUT_FRAMES past that edge (a lost/stuck state can never freeze the pose). A snapshot gap,
 *    a teleport snap, a scene-presence change and a recreated actor (zeroed visual) all clear it.
 *  - First sight of a pair on a fresh actor adopts it silently, except REPEAT rows (steady states) which start.
 * Everything is per-actor (PCRemotePlayerVisual), nothing is shared between puppets.
 * ------------------------------------------------------------------------------------------------------------ */

#define PC_REMOTE_PLAYER_LATCH_TIMEOUT_FRAMES 120.0 /* fallback may cut a one-shot this long after its edge */
#define PC_REMOTE_PLAYER_ACTION_GAP_FRAMES 90.0     /* no MOVE for this long clears a one-shot latch */

static void pc_puppet_sweat_kill(PCRemotePlayerActor* self, const char* reason); /* review H1 (defined with the Phase 3 block) */

static void pc_remote_player_row_diag_release(PCRemotePlayerActor* self, const char* why) {
    if (pc_remote_player_puppet_diag()) {
        printf("[NET][PUPPET][DIAG] player %d latch released (%s)\n", (int)self->peer, why);
    }
}

/* Ends the active row: the next frame's fallback block rebinds clips (current_* invalidated). A one-shot (STOP) row that
 * ends logs its latch release with `why`. */
static void pc_remote_player_row_release(PCRemotePlayerActor* self, const char* why) {
    PCRemotePlayerVisual* v = &self->visual;
    const PCStateRow* row;

    if (!v->row_active) {
        return;
    }
    row = pc_remote_player_row_for(v->row_idx);
    if (row != NULL && row->mode == cKF_FRAMECONTROL_STOP) {
        pc_remote_player_row_diag_release(self, why);
    }
    pc_puppet_sweat_kill(self, why); /* review H1: the pitfall run ends here (no-op unless this puppet's sweat is up) */
    v->rebind_from_row = 1; /* review R1-L3: the rebind of the next frame is a row release, not a state entry edge */
    v->row_active = 0;
    v->row_pending = 0;
    v->row_finished = 0;
    v->row_chained = 0;
    v->item_hidden = 0;
    v->item_restart = 0;
    v->current_anim_idx = -1; /* force the fallback block to rebind both layers + part table */
    v->current_anim1_idx = -1;
    v->current_part_table = -1;
}

static void pc_remote_player_bind_body(PCRemotePlayerVisual* v, int a0, int a1, f32 frame, f32 speed, int mode) {
    cKF_SkeletonInfo_R_init_standard_setframeandspeedandmorphandmode(&v->keyframe0, mPlib_Get_Pointer_Animation(a0), NULL,
                                                                     frame, speed, 0.0f, mode);
    cKF_SkeletonInfo_R_init_standard_setframeandspeedandmorphandmode(&v->keyframe1, mPlib_Get_Pointer_Animation(a1), NULL,
                                                                     frame, speed, 0.0f, mode);
    v->current_anim_idx = a0;
    v->current_anim1_idx = a1;
}

/* Starts `row` (restart if it is already active). Returns 0 and leaves the puppet on its fallback if a clip pointer is
 * missing. Writes only into the puppet's own PCRemotePlayerVisual. */
static int pc_remote_player_row_start(PCRemotePlayerActor* self, int idx, const PCStateRow* row, int raw_kind, int held_kind,
                                      double now, int* out_a0, int* out_a1, int* out_pt) {
    PCRemotePlayerVisual* v = &self->visual;
    int prev_active = v->row_active;
    int a0 = pc_remote_player_row_anim0(row, raw_kind);
    int a1;
    int pt = row->part_table;
    int cont;
    f32 f0;

    if (mPlib_Get_Pointer_Animation(a0) == NULL) {
        return 0;
    }
    if (row->anim1 == PC_ROW_A1_SAME) {
        a1 = a0;
    } else if (row->anim1 == PC_ROW_A1_ITEM) {
        a1 = a0;
        if (held_kind >= 0) {
            int pose = mPlib_Get_BasicPlayerAnimeIndex_fromItemKind(held_kind);
            if (pose >= 0 && mPlib_Get_Pointer_Animation(pose) != NULL) {
                a1 = pose;
            }
        }
    } else {
        a1 = row->anim1;
        if (mPlib_Get_Pointer_Animation(a1) == NULL) {
            a1 = a0;
        }
    }
    if (row->flags & PC_ROWF_PT_FROM_ANIM1) {
        pt = mPlib_Get_BasicPartTableIndex_fromAnimeIndex(a1);
    }
    if ((row->flags & PC_ROWF_PT_NET_IF_ITEM) && held_kind >= 0 && a1 != a0) {
        pt = mPlayer_PART_TABLE_NET;
    }
    if (pt < 0 || pt >= mPlayer_PART_TABLE_NUM) {
        pt = mPlayer_PART_TABLE_NORMAL;
    }

    /* vanilla keeps the running body clips for cast_rod (it never calls InitAnimation) */
    cont = (row->flags & PC_ROWF_BODY_CONTINUE) && prev_active && v->current_anim_idx == a0 &&
           v->current_anim1_idx == a1;
    if (!cont) {
        f0 = ((row->flags & PC_ROWF_BODY_CARRY) && prev_active) ? v->keyframe0.frame_control.current_frame : row->start_frame;
        pc_remote_player_bind_body(v, a0, a1, f0, row->speed, row->mode);
    }
    if (pt != v->current_part_table) {
        mPlib_DMA_player_Part_Table(v->part_table, pt);
        v->current_part_table = pt;
    }

    v->row_active = 1;
    v->row_idx = idx;
    v->row_finished = 0;
    v->row_chained = 0;
    v->row_pending = 0;
    v->row_start_time = now;
    v->row_adopted = 0;
    v->item_hidden = 0; /* no row hides the carried item (the HIDE row hides the whole body via PC_ROWF_HIDE_BODY) */
    v->item_restart = 1;
    v->item_carry_ok = prev_active;
    v->row_held_kind = held_kind;
    *out_a0 = a0;
    *out_a1 = a1;
    *out_pt = pt;
    return 1;
}

/* Entry-edge detection + row start/latch bookkeeping; runs once per puppet move BEFORE the fallback animation block.
 * fb_* are the fallback (move_state / carried pose) values, logged for rows without an override. */
static void pc_remote_player_row_pre(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, double now, int raw_kind,
                                     int held_kind, int fb_a0, int fb_a1, int fb_pt, int snapped) {
    PCRemotePlayerVisual* v = &self->visual;
    int valid = self->cosmetic_action_valid ? 1 : 0;
    int idx = valid ? (int)self->cosmetic_action_index : -1;
    int ctr = valid ? (int)self->cosmetic_action_counter : -1;
    const PCStateRow* row;
    const PCStateRow* active;
    int oneshot_held;
    int edge;
    int a0, a1, pt;
    int diag = pc_remote_player_puppet_diag();

    if (slot->snapshot_count == 0) {
        return; /* review R1-L1: no MOVE seen yet (fresh actor / reconnect): nothing to adopt, so the first real pair is adopted
                 * silently (steady rows start) instead of being treated as an entry edge of an action in progress */
    }
    active = v->row_active ? pc_remote_player_row_for(v->row_idx) : NULL;
    oneshot_held = (active != NULL && active->mode == cKF_FRAMECONTROL_STOP && !v->row_chained);
    if (active != NULL && (active->flags & PC_ROWF_HIDE_BODY)) {
        /* a hidden (HIDE) puppet must never stay invisible after a teleport snap or a silent peer: same clears as a latch */
        if (snapped || (now - slot->last_move_recv_local_frame) > PC_REMOTE_PLAYER_ACTION_GAP_FRAMES) {
            pc_remote_player_row_release(self, snapped ? "snap" : "gap");
            active = NULL;
        }
    }

    /* clears first: scene presence change / teleport snap / snapshot gap */
    if (slot->latch_clear_req) {
        slot->latch_clear_req = 0;
        pc_remote_player_row_release(self, "scene");
        if (v->pair_known) {
            v->pair_valid = valid; /* adopt the current pair silently: no restart without a NEW edge */
            v->pair_idx = (uint8_t)(idx < 0 ? 0 : idx);
            v->pair_ctr = (uint8_t)(ctr < 0 ? 0 : ctr);
            return;
        }
        /* review R1-L2: a fresh (recreated) actor has no pair yet: fall through to the first-sight adoption below so a
         * steady REPEAT row (hide, sitdown_wait, wait_bed, relax_rod, tired) starts instead of being skipped */
        active = NULL;
        oneshot_held = 0;
    }
    if (oneshot_held && snapped) {
        pc_remote_player_row_release(self, "snap");
        active = NULL;
        oneshot_held = 0;
    } else if (oneshot_held && (now - slot->last_move_recv_local_frame) > PC_REMOTE_PLAYER_ACTION_GAP_FRAMES) {
        pc_remote_player_row_release(self, "gap");
        active = NULL;
        oneshot_held = 0;
    }

    if (!v->pair_known) { /* fresh actor: adopt; only steady (REPEAT) rows start */
        v->pair_known = 1;
        v->pair_valid = valid;
        v->pair_idx = (uint8_t)(idx < 0 ? 0 : idx);
        v->pair_ctr = (uint8_t)(ctr < 0 ? 0 : ctr);
        row = valid ? pc_remote_player_row_for(idx) : NULL;
        if (row != NULL && row->mode == cKF_FRAMECONTROL_REPEAT &&
            pc_remote_player_row_start(self, idx, row, raw_kind, held_kind, now, &a0, &a1, &pt) && diag) {
            printf("[NET][PUPPET][DIAG] player %d state main_index=%d counter=%d row=%s anim0=%d anim1=%d part_table=%d "
                   "mode=repeat latch=off\n",
                   (int)self->peer, idx, ctr, row->name, a0, a1, pt);
        }
        if (row != NULL && v->row_active && v->row_idx == idx && v->row_start_time == now) {
            v->row_adopted = 1; /* M9-C Phase 6a: a steady row adopted without an entry edge: no entry cosmetics */
        }
        return;
    }

    edge = (valid != v->pair_valid) || (valid && ((uint8_t)idx != v->pair_idx || (uint8_t)ctr != v->pair_ctr));
    if (!edge) {
        return;
    }
    v->pair_valid = valid;
    v->pair_idx = (uint8_t)(idx < 0 ? 0 : idx);
    v->pair_ctr = (uint8_t)(ctr < 0 ? 0 : ctr);
    row = valid ? pc_remote_player_row_for(idx) : NULL;

    if (row != NULL) {
        /* a latched, unfinished one-shot is interrupted at once by an edge into another action state. Only logged here:
         * the row start below keeps the running clips (cast_rod continues ready_rod's clip) and overwrites the latch. */
        if (oneshot_held && !v->row_finished && diag) {
            pc_remote_player_row_diag_release(self, "interrupted");
        }
        if (pc_remote_player_row_start(self, idx, row, raw_kind, held_kind, now, &a0, &a1, &pt)) {
            if (diag) {
                printf("[NET][PUPPET][DIAG] player %d state main_index=%d counter=%d row=%s anim0=%d anim1=%d part_table=%d "
                       "mode=%s latch=%s\n",
                       (int)self->peer, idx, ctr, row->name, a0, a1, pt,
                       (row->mode == cKF_FRAMECONTROL_STOP) ? "stop" : "repeat",
                       (row->mode == cKF_FRAMECONTROL_STOP) ? "on" : "off");
            }
            return;
        }
        row = NULL; /* clip missing: behave as a fallback state */
    }

    /* fallback state: a latched, unfinished one-shot is allowed to finish first */
    {
        int latched = (v->row_active && oneshot_held && !v->row_finished);
        if (diag) {
            printf("[NET][PUPPET][DIAG] player %d state main_index=%d counter=%d row=fallback anim0=%d anim1=%d part_table=%d "
                   "mode=repeat latch=%s\n",
                   (int)self->peer, idx, ctr, fb_a0, fb_a1, fb_pt, latched ? "on" : "off");
        }
        if (latched) {
            if (!v->row_pending) {
                v->row_pending = 1;
                v->row_pending_since = now;
            }
        } else if (v->row_active) {
            pc_remote_player_row_release(self, "finished");
        }
    }
}

/* After the animation play of this frame: detect clip end, chain clips, and release a pending latch. */
static void pc_remote_player_row_post(PCRemotePlayerActor* self, int play_state, double now) {
    PCRemotePlayerVisual* v = &self->visual;
    const PCStateRow* row;

    if (!v->row_active) {
        return;
    }
    row = pc_remote_player_row_for(v->row_idx);
    if (row == NULL) {
        pc_remote_player_row_release(self, "interrupted");
        return;
    }
    if (row->mode == cKF_FRAMECONTROL_STOP && !v->row_chained && !v->row_finished && play_state == cKF_STATE_STOPPED) {
        if (!v->row_pending && row->chain_anim >= 0 && mPlib_Get_Pointer_Animation(row->chain_anim) != NULL) {
            int c1 = row->chain_anim;
            if (row->anim1 == PC_ROW_A1_ITEM && v->row_held_kind >= 0) { /* SetupItem_Base1 again: item pose on layer 1 */
                int pose = mPlib_Get_BasicPlayerAnimeIndex_fromItemKind(v->row_held_kind);
                if (pose >= 0 && mPlib_Get_Pointer_Animation(pose) != NULL) {
                    c1 = pose;
                }
            }
            pc_remote_player_bind_body(v, row->chain_anim, c1, 1.0f, 0.5f,
                                       (row->flags & PC_ROWF_CHAIN_STOP) ? cKF_FRAMECONTROL_STOP : cKF_FRAMECONTROL_REPEAT);
            v->row_chained = 1; /* steady pose now (vanilla: InitAnimation_Base1/2 in the state): no latch */
        } else {
            v->row_finished = 1;
        }
    }
    if (v->row_pending) {
        if (v->row_finished || v->row_chained) {
            pc_remote_player_row_release(self, "finished");
        } else if ((now - v->row_pending_since) > PC_REMOTE_PLAYER_LATCH_TIMEOUT_FRAMES) {
            pc_remote_player_row_release(self, "timeout");
        }
    }
}

/* Desired item-skeleton clip of a NET/ROD puppet this frame (see the row's item_* fields); rebinding keeps the puppet's own
 * item_keyframe (ct'd once by pc_remote_player_item_skeleton_setup). */
static void pc_remote_player_item_clip_bind(PCRemotePlayerVisual* v, int anim_idx, int mode, f32 frame) {
    cKF_Animation_R_c* anim = (cKF_Animation_R_c*)mPlib_Get_Item_DataPointer(anim_idx);
    if (anim == NULL) {
        return;
    }
    cKF_SkeletonInfo_R_init_standard_setframeandspeedandmorphandmode(&v->item_keyframe, anim, NULL, frame, 0.5f, 0.0f, mode);
    v->item_anim_idx = anim_idx;
}

static void pc_remote_player_item_tick(PCRemotePlayerActor* self, int held_moving) {
    PCRemotePlayerVisual* v = &self->visual;
    const PCStateRow* row = v->row_active ? pc_remote_player_row_for(v->row_idx) : NULL;
    int cls = v->held_class;
    int want, mode, force;
    f32 frame;

    if (!v->item_ready || (cls != PC_HELD_NET && cls != PC_HELD_ROD)) {
        return;
    }
    /* carried default: nets KAMAE_MAIN_M (SetupItem_Base1), rods ROD_WAIT (idle) / ROD_MOVE (walk/run/dash, Base3) */
    want = (cls == PC_HELD_NET) ? mPlayer_ITEM_DATA_KAMAE_MAIN_M
                                : ((row == NULL && held_moving) ? mPlayer_ITEM_DATA_ROD_MOVE : mPlayer_ITEM_DATA_ROD_WAIT);
    mode = cKF_FRAMECONTROL_REPEAT;
    frame = 1.0f;
    force = 0;
    if (row != NULL && row->item_clip >= 0) {
        /* vanilla only applies a clip whose data type fits the carried item (Check_ItemAnimationToItemKind) */
        u8 type = mPlib_Get_Item_DataPointerType(row->item_clip);
        if ((cls == PC_HELD_NET && type == mPlayer_ITEM_DATA_TYPE_NET_ANIMATION) ||
            (cls == PC_HELD_ROD && type == mPlayer_ITEM_DATA_TYPE_ROD_ANIMATION)) {
            want = row->item_clip;
            mode = row->item_mode;
            frame = row->item_start;
            if ((row->flags & PC_ROWF_ITEM_CARRY) && v->item_carry_ok) {
                frame = v->item_keyframe.frame_control.current_frame;
                if (v->item_anim_idx == want && v->item_keyframe.frame_control.mode == mode) {
                    v->item_restart = 0; /* same clip continues (cast_rod keeps ROD_SWING running) */
                }
            }
            force = v->item_restart;
        }
    }
    v->item_restart = 0;
    if (force || want != v->item_anim_idx) {
        pc_remote_player_item_clip_bind(v, want, mode, frame);
    }
}

/* M9-C Phase 3: the puppet's scene is the local scene and that scene is the town FIELD. Factored out of the M9-B arming
 * test (identical conditions) so presentation and collision cannot drift apart. */
static int pc_remote_player_scene_is_local_field(const PCRemotePlayerSlot* slot, const GAME_PLAY* play) {
    return slot->scene.valid && slot->scene.kind == (uint8_t)PC_NETSCENE_KIND_FIELD &&
           (slot->scene.flags & PC_NETGAME_SCENE_FLAG_IN_TOWN) && slot->scene.scene_id == (uint8_t)play->scene_id &&
           pc_net_game_scene_kind((int)play->scene_id) == PC_NETSCENE_KIND_FIELD;
}

/* M9-B: decides whether this puppet's collision pipe is registered this frame. Returns NULL = arm; "hold" = a
 * transient, deliberately silent skip (pause / net-or-axe swing / post-teleport-snap hold: no log state change);
 * otherwise a DISARMED reason string for the log. Pure read-only evaluation. *out_local is set only when a real local
 * player actor was found. Interiors are intentionally never armed (FIELD, same scene id, IN_TOWN only). */
static const char* pc_remote_player_collide_eval(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game,
                                                 PLAYER_ACTOR** out_local, float* out_dist) {
    GAME_PLAY* play = (GAME_PLAY*)game;
    PLAYER_ACTOR* local;
    float dx, dz, dist;
    int main_index;

    *out_local = NULL;
    *out_dist = 0.0f;

    if (pc_host_observer_active()) {
        return "observer"; /* --host-observer: the hidden server observer is never shoved by a puppet (it also registers no collider of its own) */
    }
    if (!pc_remote_player_scene_is_local_field(slot, play)) {
        return "scene";
    }
    if (slot->snapshot_count == 0) {
        return "no-snapshot";
    }
    if (!self->visual.initialized) {
        return "no-visual";
    }
    local = NULL;
    if (Common_Get(player_actor_exists)) {
        local = GET_PLAYER_ACTOR(play);
    }
    if (local == NULL || (uint64_t)(uintptr_t)local >= PC_LOWADDR_LIMIT) {
        return "no-local";
    }
    *out_local = local;
    dx = self->actor_class.world.position.x - local->actor_class.world.position.x;
    dz = self->actor_class.world.position.z - local->actor_class.world.position.z;
    dist = sqrtf(dx * dx + dz * dz);
    *out_dist = dist;
    slot->collide_target_ok = 1; /* eligible for the test hooks regardless of range/hold */
    if (!(dist < PC_REMOTE_PLAYER_COLLIDE_RANGE)) { /* also rejects NaN */
        return "range";
    }

    /* Transient holds. Pause: CollisionCheck_setOC() itself refuses while paused. Net/axe swing: the player's
     * item-triangle check (OCC) ignores group flags, so a registered pipe would stop/reflect the swing. */
    main_index = local->now_main_index;
    if (_Game_play_isPause(play) == 1 || main_index == mPlayer_INDEX_SWING_AXE || main_index == mPlayer_INDEX_SWING_NET) {
        return "hold";
    }
    if (self->collide_hold > 0) {
        self->collide_hold--;
        return "hold";
    }
    return NULL;
}

/* M9-B: opt-in for the periodic/per-contact "[NET][COLLIDE][DIAG] player N frame=..." lines. Off by default (normal
 * gameplay stays quiet); enabled by PC_COLLIDE_DIAG=1 in the environment (read once and cached, like
 * PC_NPC_TALKHOLD_DIAG) or by the --collide-test-* hook flags. The ARMED/DISARMED transition lines and the rare
 * peak / failed-setOC lines are not gated. */
static int pc_remote_player_collide_diag(void) {
    static int s_diag = -1;
    if (s_diag < 0) {
        const char* e = getenv("PC_COLLIDE_DIAG");
        s_diag = (e != NULL && e[0] == '1') ? 1 : 0;
    }
    return s_diag;
}

/* M9-B: called at the end of pc_remote_player_mv(). Registers the puppet's pipe with the vanilla OC pass (stateless:
 * the collider table is cleared every frame, so disarming is just "don't call setOC"). The vanilla solver
 * (CollisionCheck_OC at the top of the NEXT frame's play update, m_play.c) writes status_data.collision_vec only on
 * the local player (the puppet is MASSTYPE_HEAVY so a normal-weight player takes the whole push, an idle/HEAVY
 * player half), and Actor_position_move() inside the player's own movement applies it; the player's own BG check
 * corrects walls the same frame. The puppet's own collision_vec is cleared right after its mv (Actor_info_call_actor)
 * and is never applied: puppet position stays purely interpolated. */
static void pc_remote_player_collide_update(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game) {
    GAME_PLAY* play = (GAME_PLAY*)game;
    PLAYER_ACTOR* local = NULL;
    float dist = 0.0f;
    const char* reason;
    ACTOR* actor = &self->actor_class;

    slot->collide_target_ok = 0;
    reason = pc_remote_player_collide_eval(self, slot, game, &local, &dist);

    if (reason != NULL) {
        if (strcmp(reason, "observer") == 0) {
            static int s_observer_disarm_logged = 0;
            slot->collide_armed = 0;
            if (!s_observer_disarm_logged) {
                s_observer_disarm_logged = 1;
                printf("[NET][COLLIDE] player %d: collider DISARMED reason=observer\n", (int)self->peer);
            }
            return;
        }
        if (strcmp(reason, "hold") != 0 && slot->collide_armed) {
            slot->collide_armed = 0;
            printf("[NET][COLLIDE] player %d: collider DISARMED reason=%s\n", (int)self->peer, reason);
        }
        return;
    }

    {
        /* Read-only diagnostics BEFORE setOC (which clears the pipe's COLLIDED flag): the UNUSED part runs before
         * PLAYER in Actor_info_call_actor and only the player's own clear (after its mv) zeroes its collision_vec,
         * so collision_vec still holds the push this frame's OC pass produced. */
        const xyz_t* push = &local->actor_class.status_data.collision_vec;
        int hit = (self->col_pipe.collision_obj.collision_flags0 & ClObj_FLAG_COLLIDED) != 0;
        int test_active = (g_pc_collide_test_overlap || g_pc_collide_test_approach > 0);
        int pushed = (push->x != 0.0f || push->z != 0.0f);
        u32 fc = game->frame_counter;

        if (!slot->collide_armed) {
            slot->collide_armed = 1;
            printf("[NET][COLLIDE] player %d: collider ARMED\n", (int)self->peer);
        }
        if ((pc_remote_player_collide_diag() || test_active) &&
            ((test_active && (pushed || hit || dist < 60.0f)) || (pushed && (fc % 15) == 0) || (fc % 120) == 0)) {
            printf("[NET][COLLIDE][DIAG] player %d frame=%u xz_dist=%.2f push=(%.2f,%.2f) local=(%.2f,%.2f) "
                   "puppet=(%.2f,%.2f) hit=%d wt=%d main=%d peak_oc=%d failed_setoc=%d\n",
                   (int)self->peer, (unsigned)fc, (double)dist, (double)push->x, (double)push->z,
                   (double)local->actor_class.world.position.x, (double)local->actor_class.world.position.z,
                   (double)actor->world.position.x, (double)actor->world.position.z, hit,
                   (int)local->actor_class.status_data.weight, (int)local->now_main_index, s_collide_peak_colliders, s_collide_failed_setoc);
        }
    }

    CollisionCheck_Uty_ActorWorldPosSetPipeC(actor, &self->col_pipe); /* interpolated render position, s16 centre */
    if (CollisionCheck_setOC(game, &play->collision_check, &self->col_pipe.collision_obj) < 0) {
        s_collide_failed_setoc++;
        if (s_collide_failed_setoc == 1 || (s_collide_failed_setoc % 600) == 0) {
            printf("[NET][COLLIDE][DIAG] setOC failed (collider table full / paused / skipped) count=%d\n",
                   s_collide_failed_setoc);
        }
    }
}

/* ====================================================================================================================
 * M9-C Phase 3: puppet-safe locomotion cosmetics (footsteps, foot/skid/tumble effects, idle ripple). No wire change.
 *
 * Everything here runs ONLY from pc_remote_player_mv() (never from a network handler), reads only puppet-owned state
 * (its interpolated position/angle/speed/move_state/state row, keyframe0 frame control, the foot positions its OWN
 * after-draw callback captured) plus read-only world data (ground attribute, season/weather), and spawns through the
 * global effect clip / sAdo_* one-shots. It never calls a Player_actor_* function, a vanilla draw callback, the local
 * player's sound-status path (sAdo_PlyWalkSe / sAdo_PlayerStatusLevel) or anything that mutates world state: the
 * dash-flower trample (RANDOM + mFI_SetFG_common + island log) is deliberately NOT replayed.
 * ================================================================================================================== */

#define PC_PUPPET_FX_EFFECT_RANGE 500.0f /* box half-extent (|dx|,|dz|,|dy|) within which a puppet may spawn effects. Effect
                                          * profiles die at 800 (eEC_DEFAULT_DEATH_DIST, eEC_DistDeath: same 3-axis box test
                                          * against the LOCAL player), so 500 keeps every spawned effect alive for its life. */
#define PC_PUPPET_FX_SOFT_RANGE 250.0f   /* range used instead while the pool is above PC_PUPPET_FX_POOL_SOFT (nearest first) */
#define PC_PUPPET_FX_SOUND_RANGE 600.0f  /* footstep/tumble sounds are positional one-shots (distance attenuated by the mixer) */
#define PC_PUPPET_FX_POOL_SOFT 50        /* of eEC_EFFECT_ACTIVE_MAX (100): above this, shrink the range */
#define PC_PUPPET_FX_POOL_HARD 70        /* a spawn is refused (budget=pool) when pool_count + its fan-out would exceed this, so the
                                          * local player keeps ~30 slots (review R3-L2: top-level spawns are weighted by the number of
                                          * sub-effect slots their init/ct claims, see pc_puppet_fx_fanout) */
#define PC_PUPPET_FX_FRAME_CAP 3         /* top-level effect spawns per game frame across ALL puppets */
#define PC_PUPPET_SND_FRAME_CAP 4        /* puppet sounds per game frame across ALL puppets */
#define PC_PUPPET_FX_BUCKET_MAX 4.0f     /* per-puppet effect burst */
#define PC_PUPPET_FX_REFILL_PER_FRAME (10.0f / 60.0f) /* per-puppet sustained cap: 10 top-level effects per second */
#define PC_PUPPET_FOOT_COOLDOWN_FRAMES 8.0  /* min spacing between two foot triggers of one puppet (WALK/IDLE flicker) */
#define PC_PUPPET_EDGE_COOLDOWN_FRAMES 30.0 /* min spacing between two identical one-shot edges (skid in/out, tumble) */
#define PC_PUPPET_FX_ITEM_NAME_BASE 0xFFE0u /* + slot: per-puppet effect item_name (the local player uses RSV_NO 0xFFFF) */
#define PC_PUPPET_FOOT_STALE_FRAMES 3       /* captured foot data older than this (game frames) is replaced by the fallback */
#define PC_PUPPET_FOOT_FALLBACK_OFFSET 7.0f /* lateral offset (world units) of the fallback foot positions */
#define PC_PUPPET_FX_DIAG_MAX_OK 40         /* diag lines per puppet actor and (cosmetic name) when it was spawned */
#define PC_PUPPET_FX_DIAG_MAX_SUPPRESSED 4  /* ... per (cosmetic name, reason) when it was suppressed */
#define PC_PUPPET_PENDING_FRAMES 12.0       /* a foot step denied only by the frame cap is retried for this many frames */
#define PC_PUPPET_RIPPLE_PERIOD 8.0f        /* Player_actor_set_ripple period (60fps frames) */
#define PC_PUPPET_MAX_DT_FRAMES 4.0f        /* a frame step above this suppresses frame-window triggers (hitch) */

/* Foot clip frames (vanilla Player_actor_Check_{Left,Right}FootMark_AnimeFrame_fromAnimeIndex data tables,
 * m_player_common.c_inc: left_data_{walk1,run1,dash1} = {1}, right_data_{walk1,run1,dash1} = {9}; RUN_SLIP1 = none;
 * SetEffect_Walk/Dash(actor, game, 1.0f, 9.0f) use the same pair). */
#define PC_PUPPET_FOOT_LEFT_FRAME 1.0f
#define PC_PUPPET_FOOT_RIGHT_FRAME 9.0f

enum { PC_PIECE_FOOTPRINT = 0, PC_PIECE_SOUND = 1, PC_PIECE_DUST = 2 }; /* pieces of one foot step */

enum { /* PCRemotePlayerCosmetic::state */
    PC_COS_NONE = 0,
    PC_COS_IDLE,
    PC_COS_MOVE,      /* WALK1 / RUN1 / DASH1 clip on the body layer */
    PC_COS_TURN,      /* RUN_SLIP1 (the skid) */
    PC_COS_TUMBLE,    /* state row TUMBLE (frame triggers available) */
    PC_COS_TUMBLE_MV, /* move_state TUMBLE without a row (v6 sender): entry edge only */
};

static int s_puppet_fx_frame_valid = 0;
static u32 s_puppet_fx_frame = 0;
static int s_puppet_fx_frame_count = 0;
static int s_puppet_snd_frame_count = 0;
static int s_puppet_fx_peak_frame = 0; /* diag: highest per-frame spawn count observed */

/* eEC_pc_count_active / eEC_pc_source_pos_override: declared in ef_effect_control.h (TARGET_PC) */


/* M9-C Phase 3: puppet-owned foot position for foot index 0 = left / 1 = right. Uses the data this puppet's own
 * after-draw callback captured during its last body draw (one frame of lag, like vanilla's own callbacks which also run
 * during the draw after the move); falls back to the puppet's world position +- a lateral offset (facing based) when no
 * recent capture exists (first frames, culled/hidden puppet, a frame without a draw). */
static void pc_puppet_foot_get(const PCRemotePlayerActor* self, const GAME* game, int idx, xyz_t* out_pos, s16* out_angle) {
    const PCRemotePlayerCosmetic* c = &self->cos;
    u32 fc = game->frame_counter;

    if (c->foot_frame[idx] != 0 && (fc + 1u) - c->foot_frame[idx] <= (u32)PC_PUPPET_FOOT_STALE_FRAMES) {
        *out_pos = c->foot_pos[idx];
        *out_angle = c->foot_angle_y[idx];
    } else {
        float a = (float)self->actor_class.world.angle.y * (3.14159265f / 32768.0f);
        float side = (idx == 0) ? -PC_PUPPET_FOOT_FALLBACK_OFFSET : PC_PUPPET_FOOT_FALLBACK_OFFSET;

        *out_pos = self->actor_class.world.position;
        out_pos->x += side * cosf(a); /* local +x is (cos, -sin), see Player_actor_Check_FlatPlace's offset rotation */
        out_pos->z -= side * sinf(a);
        *out_angle = self->actor_class.world.angle.y;
    }
}

/* Decides whether a puppet cosmetic may be produced right now. NULL = allowed (and the budget slot is consumed);
 * otherwise the reason token used in the diagnostics: scene | pause | hidden | null | range | pool | capped. */
static const char* pc_puppet_fx_reason_cost(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, int is_sound,
                                            int fanout) {
    GAME_PLAY* play = (GAME_PLAY*)game;
    PCRemotePlayerCosmetic* c = &self->cos;
    PLAYER_ACTOR* local = NULL;
    float limit;
    float dx, dz, dy;
    u32 fc = game->frame_counter;
    int pool = 0;

    if (!pc_remote_player_scene_is_local_field(slot, play) || slot->snapshot_count == 0 || !self->visual.initialized) {
        return "scene"; /* includes an unknown (never announced) puppet scene */
    }
    if (self->visual.row_active) {
        const PCStateRow* hrow = pc_remote_player_row_for(self->visual.row_idx);
        if (hrow != NULL && (hrow->flags & PC_ROWF_HIDE_BODY)) {
            return "hidden";
        }
    }
    if (_Game_play_isPause(play) == 1) {
        return "pause";
    }
    if (!is_sound) {
        /* The clip is created by the effect-control actor's ct and cleared by its dt (ef_effect_control.c): it can be
         * NULL right after a scene change, before the controller exists, and in scenes without one. */
        if (eEC_CLIP == NULL || eEC_CLIP->effect_make_proc == NULL || eEC_CLIP->make_effect_proc == NULL) {
            return "null";
        }
    }
    if (!Common_Get(player_actor_exists)) {
        return "scene";
    }
    local = GET_PLAYER_ACTOR(play);
    if (local == NULL || (uint64_t)(uintptr_t)local >= PC_LOWADDR_LIMIT) {
        return "scene";
    }
    limit = is_sound ? PC_PUPPET_FX_SOUND_RANGE : PC_PUPPET_FX_EFFECT_RANGE;
    dx = self->actor_class.world.position.x - local->actor_class.world.position.x;
    dz = self->actor_class.world.position.z - local->actor_class.world.position.z;
    dy = self->actor_class.world.position.y - local->actor_class.world.position.y;
    if (!(fabsf(dx) <= limit && fabsf(dz) <= limit && fabsf(dy) <= limit)) { /* also rejects NaN */
        return "range";
    }
    if (!is_sound) {
        pool = eEC_pc_count_active();
        if (pool + fanout > PC_PUPPET_FX_POOL_HARD) {
            return "pool";
        }
        if (pool > PC_PUPPET_FX_POOL_SOFT &&
            !(fabsf(dx) <= PC_PUPPET_FX_SOFT_RANGE && fabsf(dz) <= PC_PUPPET_FX_SOFT_RANGE &&
              fabsf(dy) <= PC_PUPPET_FX_SOFT_RANGE)) {
            return "pool"; /* nearest puppets first while the pool is filling up */
        }
    }

    /* budgets */
    if (!s_puppet_fx_frame_valid || s_puppet_fx_frame != fc) {
        s_puppet_fx_frame_valid = 1;
        s_puppet_fx_frame = fc;
        s_puppet_fx_frame_count = 0;
        s_puppet_snd_frame_count = 0;
    }
    if (is_sound) {
        if (s_puppet_snd_frame_count >= PC_PUPPET_SND_FRAME_CAP) {
            return "capped";
        }
        s_puppet_snd_frame_count++;
    } else {
        double now = graph_dt_frame_time(game);

        if (!c->bucket_init) {
            c->bucket_init = 1;
            c->tokens = PC_PUPPET_FX_BUCKET_MAX;
            c->bucket_time = now;
        } else if (now > c->bucket_time) {
            c->tokens += (float)(now - c->bucket_time) * PC_PUPPET_FX_REFILL_PER_FRAME;
            if (c->tokens > PC_PUPPET_FX_BUCKET_MAX) {
                c->tokens = PC_PUPPET_FX_BUCKET_MAX;
            }
            c->bucket_time = now;
        }
        if (c->tokens < 1.0f || s_puppet_fx_frame_count >= PC_PUPPET_FX_FRAME_CAP) {
            return "capped";
        }
        c->tokens -= 1.0f;
        s_puppet_fx_frame_count++;
        if (s_puppet_fx_frame_count > s_puppet_fx_peak_frame) {
            s_puppet_fx_peak_frame = s_puppet_fx_frame_count;
            if (pc_remote_player_puppet_diag()) {
                printf("[NET][PUPPET][DIAG] cosmetic per-frame spawn peak=%d cap=%d\n", s_puppet_fx_peak_frame,
                       PC_PUPPET_FX_FRAME_CAP);
            }
        }
    }
    return NULL;
}

/* Review R3-L2: extra pool slots one top-level spawn claims beyond what the old per-spawn check saw (estimated from the
 * effect sources; a SLOT is one eEC_ctrl_work.effects entry):
 *   DIG_SCOOP (ef_dig_scoop.c init, makes only children): arg1 0 = 6 DIG_MUD; arg1 1 on bush = 5 BUSH_HAPPA (+5 BUSH_YUKI in
 *     winter, the reflect winter-bush path) = up to 10 (the worst case, used); the other variants make <= 6 (yukidama 4 /
 *     sandsplash 3 / tumble dust 3 / mizutama 4 + ami_mizu).
 *   DIG_HOLE (ef_dig_hole.c ct): itself + 1 DIG_MUD (fill) + 2 MIZUTAMA (water) = 4.
 *   DASH_ASIMOTO (ef_dash_asimoto.c): itself + 2 HANABIRA + 3 BUSH_HAPPA + 3 BUSH_YUKI = 9 worst case; WALK_ASIMOTO
 *     (ef_walk_asimoto.c) about 5. 10 is used for both (a safe upper bound: puppet foot dust simply stops a little earlier).
 * Everything else keeps the old check (0 extra). */
static int pc_puppet_fx_fanout(int fx_id) {
    switch (fx_id) {
        case eEC_EFFECT_DIG_SCOOP:
            return 10;
        case eEC_EFFECT_DIG_HOLE:
            return 4;
        case eEC_EFFECT_DASH_ASIMOTO:
        case eEC_EFFECT_WALK_ASIMOTO:
            return 10;
        default:
            return 0;
    }
}

static const char* pc_puppet_fx_reason(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, int is_sound) {
    return pc_puppet_fx_reason_cost(self, slot, game, is_sound, 0);
}

/* Review H1: kill this puppet's own continuous ASE2 pitfall sweat (vanilla: effect_kill_proc(ASE2, RSV_NO) in
 * struggle_pitfall.c_inc:27 / ready_pitfall.c_inc:56). eEC_Name2EffectKill matches id AND item_name, so only the effect
 * spawned with this puppet's item_name dies, never the local player's (RSV_NO). ASE2's profile is eEC_IMMEDIATE_DEATH: the
 * slot is freed at once, so a following pitfall spawns a fresh one. Safe with a NULL clip (scene change / no controller:
 * the pool is gone with it); only acts when this puppet spawned one (sweat_active). */
static void pc_puppet_sweat_kill(PCRemotePlayerActor* self, const char* reason) {
    if (!self->cos.sweat_active) {
        return;
    }
    self->cos.sweat_active = 0;
    if (eEC_CLIP != NULL && eEC_CLIP->effect_kill_proc != NULL) {
        eEC_CLIP->effect_kill_proc(eEC_EFFECT_ASE2, (u16)(PC_PUPPET_FX_ITEM_NAME_BASE + (u32)self->peer));
        if (pc_remote_player_puppet_diag()) {
            printf("[NET][PUPPET][DIAG] player %d pitfall sweat kill reason=%s\n", (int)self->peer, reason);
        }
    }
}

/* Actor dt (scene teardown / reaped after Actor_delete): last chance to free the puppet's sweat. */
static void pc_remote_player_dt(ACTOR* actor, GAME* game) {
    (void)game;
    pc_puppet_sweat_kill((PCRemotePlayerActor*)actor, "scene");
}

/* Rate limiter shared by every puppet cosmetic diag line: 1 = this (what, name, budget) key may still print. */
static int pc_puppet_diag_allow(PCRemotePlayerActor* self, const char* what, const char* name, const char* budget) {
    PCRemotePlayerCosmetic* c = &self->cos;
    int ok = (strcmp(budget, "ok") == 0);
    char key[40];
    int i;

    if (!pc_remote_player_puppet_diag()) {
        return 0;
    }
    snprintf(key, sizeof(key), "%s/%s/%s", what, name, ok ? "ok" : budget);
    for (i = 0; i < (int)(sizeof(c->diag) / sizeof(c->diag[0])); i++) {
        if (c->diag[i].key[0] == '\0') {
            strcpy(c->diag[i].key, key);
            break;
        }
        if (strcmp(c->diag[i].key, key) == 0) {
            break;
        }
    }
    if (i >= (int)(sizeof(c->diag) / sizeof(c->diag[0])) ||
        c->diag[i].n >= (ok ? PC_PUPPET_FX_DIAG_MAX_OK : PC_PUPPET_FX_DIAG_MAX_SUPPRESSED)) {
        return 0;
    }
    c->diag[i].n++;
    return 1;
}

static void pc_puppet_fx_diag(PCRemotePlayerActor* self, const char* what, const char* name, float frame, int attr,
                              const char* budget) {
    if (!pc_puppet_diag_allow(self, what, name, budget)) {
        return;
    }
    if (strcmp(what, "effect") == 0) {
        printf("[NET][PUPPET][DIAG] player %d cosmetic effect=%s frame=%.1f pos=(%.0f,%.0f) attr=%d budget=%s\n",
               (int)self->peer, name, (double)frame, (double)self->actor_class.world.position.x,
               (double)self->actor_class.world.position.z, attr, budget);
    } else {
        printf("[NET][PUPPET][DIAG] player %d %s sound label=%d frame=%.1f budget=%s\n", (int)self->peer, name, attr,
               (double)frame, budget);
    }
}

/* One effect through the global clip: gated, budgeted, null-checked. `weather_sensitive`: the effect's init/ct indexes a
 * per-weather table (ef_walk_asimoto.c:92-110, ef_dash_asimoto.c:165-181) that is NULL/short for "leaves" when BUGFIXES is
 * off (a NULL call / one-past-the-end read): the caller-side guard skips exactly that condition (and any out-of-range
 * weather value) instead of editing the effect files. BUGFIXES is defined for this target, which makes the table
 * complete; the guard stays as defence in depth. */
static void pc_puppet_fx_refund(PCRemotePlayerActor* self);
static int pc_puppet_tool_fx_noop(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, int fx_id, int arg1);

/* M9-C Phase 6b: optional per-spawn extras (NULL = the Phase 3-6a behaviour). */
typedef struct PCFxOpts {
    const xyz_t* attr_pos; /* ground attribute is read here instead of at the puppet (vanilla: mCoBG_Wpos2Attribute(*pos)) */
    int attr_force;        /* >= 0: this attribute value is passed as arg0 (vanilla passes a literal 0) */
    int angle_zero;        /* vanilla passes angle 0 */
    int dig_src;           /* ef_dig_hole / ef_dig_scoop: puppet position replaces the local player's (eEC_pc_source_pos_override) */
} PCFxOpts;

static int pc_puppet_fx_emit_core(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, int fx_id,
                                  const char* name, const xyz_t* pos, s16 angle, float frame, s16 arg1,
                                  int weather_sensitive, int retry, const PCFxOpts* opts) {
    const char* reason = NULL;
    int attr = -1;
    PCRemotePlayerCosStats* st = &slot->cos_stats;

    if (weather_sensitive) {
        int w = mEnv_NowWeather();

        if (w < 0 || w >= mEnv_WEATHER_NUM) {
            reason = "weather";
        }
#ifndef BUGFIXES
        else if (w == mEnv_WEATHER_LEAVES) {
            reason = "weather";
        }
#endif
    }
    if (reason == NULL) {
        reason = pc_puppet_fx_reason_cost(self, slot, game, 0, pc_puppet_fx_fanout(fx_id));
    }
    if (reason != NULL && retry && strcmp(reason, "capped") == 0) {
        return 0; /* deferred: the caller retries next frame (counted only if it finally expires) */
    }
    if (reason == NULL && pc_puppet_tool_fx_noop(self, slot, game, fx_id, (int)arg1)) {
        /* M9-C Phase 6a: vanilla's init would spawn nothing visible here (swing rod / swing axe start on non-bush ground):
         * all gates passed, the budget slot is returned and no pool slot is burned. Other effects never match. */
        pc_puppet_fx_refund(self);
        slot->cos_stats.tool_noop++;
        pc_puppet_fx_diag(self, "effect", name, frame, -1, "noop");
        return 1;
    }
    if (reason == NULL) {
        /* Scene == local scene was verified by pc_puppet_fx_reason(): mCoBG_Wpos2Attribute reads the CURRENT scene's
         * collision data, which is what the puppet's position refers to. Pure read (no cant_dig out-param). */
        if (opts != NULL && opts->attr_force >= 0) {
            attr = opts->attr_force;
        } else {
            xyz_t apos = (opts != NULL && opts->attr_pos != NULL) ? *opts->attr_pos : self->actor_class.world.position;

            attr = (int)mCoBG_Wpos2Attribute(apos, NULL);
        }
        if (opts != NULL && opts->angle_zero) {
            angle = 0;
        }
        if (opts != NULL && opts->dig_src) {
            eEC_pc_source_pos_override = &self->actor_class.world.position; /* cleared right after the one call */
        }
        eEC_CLIP->effect_make_proc(fx_id, *pos, 2, angle, game, (u16)(PC_PUPPET_FX_ITEM_NAME_BASE + (u32)self->peer),
                                   (s16)attr, arg1);
        eEC_pc_source_pos_override = NULL;
        st->spawned++;
        pc_puppet_fx_diag(self, "effect", name, frame, attr, "ok");
    } else {
        if (strcmp(reason, "capped") == 0) {
            st->capped++;
        } else {
            st->suppressed++;
        }
        pc_puppet_fx_diag(self, "effect", name, frame, -1, reason);
    }
    return 1;
}

static int pc_puppet_fx_emit_ex(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, int fx_id,
                                const char* name, const xyz_t* pos, s16 angle, float frame, s16 arg1, int weather_sensitive,
                                int retry) {
    return pc_puppet_fx_emit_core(self, slot, game, fx_id, name, pos, angle, frame, arg1, weather_sensitive, retry, NULL);
}

static void pc_puppet_fx_emit(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, int fx_id, const char* name,
                              const xyz_t* pos, s16 angle, float frame, s16 arg1, int weather_sensitive) {
    (void)pc_puppet_fx_emit_ex(self, slot, game, fx_id, name, pos, angle, frame, arg1, weather_sensitive, 0);
}

/* Footstep (NPC-style path: sAdo_NpcWalkSe + sAdo_Get_WalkLabel, exactly aNPC_WalkSe in ac_npc_sound.c_inc, never the
 * local-player sAdo_PlyWalkSe/sou_walk_flag) or tumble sound (sAdo_OngenTrgStart with sAdo_Get_KokeruLabel, exactly the
 * body of Player_actor_sound_Tumble / aNPC_TumbleSe). Outdoor (field) only: interior floor sounds are not played. */
static int pc_puppet_snd_emit_ex(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, int tumble, float frame,
                                 int retry) {
    const char* reason = pc_puppet_fx_reason(self, slot, game, 1);
    PCRemotePlayerCosStats* st = &slot->cos_stats;
    const char* what = tumble ? "tumble" : "footstep";

    if (reason != NULL && retry && strcmp(reason, "capped") == 0) {
        return 0;
    }
    if (reason == NULL) {
        xyz_t pos = self->actor_class.world.position;
        u32 attr = mCoBG_Wpos2Attribute(pos, NULL);
        u16 label;

        if (tumble) {
            label = sAdo_Get_KokeruLabel((int)attr);
            sAdo_OngenTrgStart(label, &pos);
        } else {
            label = sAdo_Get_WalkLabel((int)attr);
            if (sAdo_CheckOnPlussBridge(&pos, attr)) {
                label = NA_SE_FOOTSTEP_PLUSSBRIDGE;
            }
            sAdo_NpcWalkSe(label, &pos);
        }
        st->sounds++;
        pc_puppet_fx_diag(self, what, what, frame, (int)label, "ok");
    } else {
        st->sounds_suppressed++;
        pc_puppet_fx_diag(self, what, what, frame, -1, reason);
    }
    return 1;
}

static void pc_puppet_snd_emit(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, int tumble, float frame) {
    (void)pc_puppet_snd_emit_ex(self, slot, game, tumble, frame, 0);
}

/* Window test of Player_actor_Check_AnimationFrame_Common for a positive-speed clip, over the REAL previous/current
 * frames this puppet observed (so it is independent of dt scaling): last < target <= cur, with the repeat-clip wrap
 * (frames run 1..max_frames; vanilla's `1.0f <= target <= cur` branch after the wrap). */
static int pc_puppet_frame_crossed(float last, float cur, float max_frames, float target) {
    if (cur >= last) {
        return last < target && target <= cur;
    }
    return (last < target && target <= max_frames) || (1.0f <= target && target <= cur);
}

/* ====================================================================================================================
 * M9-C Phase 4: remote tree-shake presentation (cosmetic only).
 *
 * Vanilla chain (all local to the shaking process): CheckAndRequest_main_shake_tree_all finds the tree with
 * Get_shake_tree_position_and_itemNo (read-only 8-neighbour search over the LOCAL fg grid), then the SHAKE_TREE state (index
 * 89, SHAKE1 anim, STOP, speed 0.5) fires at keyframe0 frame 10 (SetEffect_Shake_tree, m_player_main_shake_tree.c_inc:52-99):
 * Set_shake_tree_table -> Set_EffectBgTree -> Make_EffectBG(EffectBG_EFFECT_SHAKE_LARGE, variant, tile centre) + the
 * NA_SE_TREE_YURASU one-shot at the tree. Everything after that (item_tree_fruit_drop_proc / request_tree_shake / bees /
 * insect reactions) is gameplay and is NOT reproduced: the host stays the only authority for the tile (FIELD_UPDATE).
 * The puppet re-derives the tree from its own interpolated position/facing at frame 10; it reads only mFI_/mCoBG_ pure
 * accessors of the current scene and spawns through the public clip (CLIP(make_effect_bg_proc)).
 * ================================================================================================================== */

#define PC_PUPPET_TREE_FRAME 10.0f          /* SHAKE1 keyframe0 frame of SetEffect_Shake_tree (Check_AnimationFrame 10.0f) */
#define PC_PUPPET_TREE_COOLDOWN_FRAMES 84.0 /* vanilla shake_tree_timer (Set_shake_tree_table 84.0f): per-puppet min spacing */
#define PC_PUPPET_TREE_GLOBAL_SPACING 30.0  /* at most one puppet-driven tree effect per this many frames across all puppets */
#define PC_PUPPET_TREE_RETRY_FRAMES 60.0    /* an effect denied only by a cap is retried this long */
#define PC_PUPPET_TREE_EFBG_MAX_ACTIVE 1    /* EffectBG pool = 3 slots: a puppet spawns only while <= 1 is busy (>= 1 stays free) */
#define PC_PUPPET_TREE_DUP_RADIUS 20.0f     /* an active EffectBG whose base is within this XZ distance is "the same tile" */
#define PC_PUPPET_TREE_RING 4

/* Effectbg_pc_count_active / Effectbg_pc_count_near: declared in ac_effectbg.h (TARGET_PC) */

static struct {
    int ux, uz;
    double t;
    int used;
} s_tree_ring[PC_PUPPET_TREE_RING]; /* recently presented tiles (any puppet): a second puppet on the same tree = "cooldown" */
static int s_tree_ring_next = 0;
static int s_tree_global_valid = 0;
static int s_tree_diag_efbg = -1; /* EffectBG slots busy when the last attempt started (-1 = no EffectBG actor); diag only */
static double s_tree_global_last = 0.0;

/* Puppet-owned mirror of Player_actor_Get_shake_tree_position_and_itemNo (m_player_common.c_inc:6243-6330) followed by the
 * mFI_Wpos2UtNum of Player_actor_CheckAndRequest_main_shake_tree_all: same neighbour order (row-major, centre skipped), same
 * tile filters (IS_ITEM_COLLIDEABLE_TREE, |dy| <= 10, dist^2 < unit base size^2, facing within 45 degrees, first strictly
 * smaller angle wins). Pure reads of the current scene's field/collision data. Returns the tree item (EMPTY_NO = none) and
 * fills the tile and its centre position. */
static mActor_name_t pc_puppet_find_shake_tree(const xyz_t* ppos, s16 facing, int* out_ux, int* out_uz, xyz_t* out_pos) {
    xyz_t center_pos;

    *out_ux = -1;
    *out_uz = -1;
    *out_pos = *ppos;
    if (mFI_Wpos2UtCenterWpos(&center_pos, *ppos)) {
        static const f32 add_X[] = { -mFI_UT_WORLDSIZE_X_F, 0.0f, mFI_UT_WORLDSIZE_X_F, -mFI_UT_WORLDSIZE_X_F,
                                     mFI_UT_WORLDSIZE_X_F, -mFI_UT_WORLDSIZE_X_F, 0.0f, mFI_UT_WORLDSIZE_X_F };
        static const f32 add_Z[] = { -mFI_UT_WORLDSIZE_Z_F, -mFI_UT_WORLDSIZE_Z_F, -mFI_UT_WORLDSIZE_Z_F, 0.0f,
                                     0.0f, mFI_UT_WORLDSIZE_Z_F, mFI_UT_WORLDSIZE_Z_F, mFI_UT_WORLDSIZE_Z_F };
        mActor_name_t unit_item[8];
        xyz_t unit_pos[8];
        int select_index = -1;
        int i;

        for (i = 0; i < 8; i++) {
            unit_pos[i].x = center_pos.x + add_X[i];
            unit_pos[i].z = center_pos.z + add_Z[i];
        }
        for (i = 0; i < 8; i++) {
            mActor_name_t* fg_p = mFI_GetUnitFG(unit_pos[i]);

            unit_item[i] = (fg_p != NULL && IS_ITEM_COLLIDEABLE_TREE(*fg_p)) ? *fg_p : EMPTY_NO;
        }
        for (i = 0; i < 8; i++) {
            if (unit_item[i] != EMPTY_NO) {
                unit_pos[i].y = mCoBG_GetBgY_OnlyCenter_FromWpos2(unit_pos[i], 0.0f);
                if (ABS(unit_pos[i].y - ppos->y) > 10.0f) {
                    unit_item[i] = EMPTY_NO;
                }
            }
        }
        for (i = 0; i < 8; i++) {
            if (unit_item[i] != EMPTY_NO) {
                f32 dist_sq = Math3DLengthSquare2D(unit_pos[i].x, unit_pos[i].z, ppos->x, ppos->z);

                if (dist_sq >= SQ(mFI_UNIT_BASE_SIZE_F)) {
                    unit_item[i] = EMPTY_NO;
                }
            }
        }
        {
            int min_angle = DEG2SHORT_ANGLE2(45.0f);

            for (i = 0; i < 8; i++) {
                if (unit_item[i] != EMPTY_NO) {
                    f32 dx = unit_pos[i].x - ppos->x;
                    f32 dz = unit_pos[i].z - ppos->z;
                    int angle = (s16)(atans_table(dz, dx) - (int)facing);
                    int abs_angle = ABS(angle);

                    if (abs_angle < min_angle) {
                        select_index = i;
                        min_angle = abs_angle;
                    }
                }
            }
        }
        if (select_index >= 0 && select_index < 8) {
            int ux, uz;

            if (mFI_Wpos2UtNum(&ux, &uz, unit_pos[select_index])) {
                *out_ux = ux;
                *out_uz = uz;
                *out_pos = unit_pos[select_index];
                return unit_item[select_index];
            }
        }
    }
    return EMPTY_NO;
}

/* Mirror of Player_actor_Set_EffectBgTree's item -> variant mapping (m_player_common.c_inc:6182-6241). The variant comes
 * from the CURRENT tile item: a fruit tree the host already converted to NOFRUIT is the plain full-size TREE id, i.e. the
 * same *_FULL variant the fruit-bearing id maps to (the else branch), so a FIELD_UPDATE landing first changes nothing. */
static s16 pc_puppet_tree_variant(mActor_name_t item, const char** species, int* small) {
    int type = FGTreeType_check(item);
    s16 v;

    *species = (type == mNT_TREE_TYPE_PALM)    ? "palm"
               : (type == mNT_TREE_TYPE_CEDAR) ? "cedar"
               : (type == mNT_TREE_TYPE_GOLD)  ? "gold"
                                               : "normal";
    *small = 0;
    if (IS_ITEM_XMAS_TREE(item)) {
        v = (type == mNT_TREE_TYPE_CEDAR) ? EffectBG_VARIANT_CEDAR_XMAS : EffectBG_VARIANT_TREE_XMAS;
    } else if (IS_ITEM_SMALL_TREE(item)) {
        *small = 1; /* Make_EffectBG routes small variants to eEC_EFFECT_YOUNG_TREE (effect pool), not an EffectBG slot */
        v = (type == mNT_TREE_TYPE_PALM)    ? EffectBG_VARIANT_PALM_SMALL
            : (type == mNT_TREE_TYPE_CEDAR) ? EffectBG_VARIANT_CEDAR_SMALL
            : (type == mNT_TREE_TYPE_GOLD)  ? EffectBG_VARIANT_GOLD_SMALL
                                            : EffectBG_VARIANT_TREE_SMALL;
    } else if (IS_ITEM_MED_TREE(item)) {
        v = (type == mNT_TREE_TYPE_PALM)    ? EffectBG_VARIANT_PALM_MED
            : (type == mNT_TREE_TYPE_CEDAR) ? EffectBG_VARIANT_CEDAR_MED
            : (type == mNT_TREE_TYPE_GOLD)  ? EffectBG_VARIANT_GOLD_MED
                                            : EffectBG_VARIANT_TREE_MED;
    } else if (IS_ITEM_LARGE_TREE(item)) {
        v = (type == mNT_TREE_TYPE_PALM)    ? EffectBG_VARIANT_PALM_LARGE
            : (type == mNT_TREE_TYPE_CEDAR) ? EffectBG_VARIANT_CEDAR_LARGE
            : (type == mNT_TREE_TYPE_GOLD)  ? EffectBG_VARIANT_GOLD_LARGE
                                            : EffectBG_VARIANT_TREE_LARGE;
    } else {
        v = (type == mNT_TREE_TYPE_PALM)    ? EffectBG_VARIANT_PALM_FULL
            : (type == mNT_TREE_TYPE_CEDAR) ? EffectBG_VARIANT_CEDAR_FULL
            : (type == mNT_TREE_TYPE_GOLD)  ? EffectBG_VARIANT_GOLD_FULL
                                            : EffectBG_VARIANT_TREE_FULL;
    }
    return v;
}

static void pc_puppet_tree_diag(PCRemotePlayerActor* self, const char* what, const char* detail, int ux, int uz,
                                mActor_name_t item, const char* species, const char* budget) {
    if (!pc_puppet_diag_allow(self, "tree", what, budget)) {
        return;
    }
    if (strcmp(what, "effect") == 0) {
        printf("[NET][PUPPET][DIAG] player %d tree shake effect=large variant=%s tile=(%d,%d) item=0x%04x species=%s "
               "budget=%s efbg_active=%d\n",
               (int)self->peer, detail, ux, uz, (unsigned)item, species, budget, s_tree_diag_efbg);
    } else {
        printf("[NET][PUPPET][DIAG] player %d tree shake sound label=%s tile=(%d,%d) item=0x%04x species=%s budget=%s\n",
               (int)self->peer, detail, ux, uz, (unsigned)item, species, budget);
    }
}

/* Undo the token / per-frame slot pc_puppet_fx_reason(.., 0) consumed when a later tree-specific gate denies. */
static void pc_puppet_fx_refund(PCRemotePlayerActor* self) {
    self->cos.tokens += 1.0f;
    if (self->cos.tokens > PC_PUPPET_FX_BUCKET_MAX) {
        self->cos.tokens = PC_PUPPET_FX_BUCKET_MAX;
    }
    if (s_puppet_fx_frame_count > 0) {
        s_puppet_fx_frame_count--;
    }
}

/* One frame-10 attempt. Returns 1 = final (presented or definitively skipped), 0 = denied only by a cap: retry later. */
static int pc_puppet_tree_shake_fire(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, double now,
                                     int expired) {
    PCRemotePlayerCosmetic* c = &self->cos;
    PCRemotePlayerCosStats* st = &slot->cos_stats;
    const char* reason;
    const char* species = "normal";
    xyz_t tpos;
    int ux = -1, uz = -1, small = 0, i;
    mActor_name_t item;
    s16 variant;

    /* scene -> hidden -> pause -> null (eEC clip) -> local player -> range -> pool (effect pool) -> capped */
    s_tree_diag_efbg = Effectbg_pc_count_active();
    reason = pc_puppet_fx_reason(self, slot, game, 0);
    if (reason != NULL) {
        if (strcmp(reason, "capped") == 0 && !expired) {
            return 0;
        }
        st->tree_skipped++;
        pc_puppet_tree_diag(self, "effect", "-", -1, -1, EMPTY_NO, species, reason);
        return 1;
    }
    /* From here the scene is the local field (checked above), so the pure field readers below see the right grid. */
    item = pc_puppet_find_shake_tree(&self->actor_class.world.position, self->actor_class.shape_info.rotation.y, &ux, &uz,
                                     &tpos);
    if (item == EMPTY_NO) {
        pc_puppet_fx_refund(self);
        st->tree_skipped++;
        pc_puppet_tree_diag(self, "effect", "-", -1, -1, EMPTY_NO, species, "notree");
        return 1;
    }
    variant = pc_puppet_tree_variant(item, &species, &small);

    if (!c->tree_committed) {
        int cool = c->tree_have_last && (now - c->tree_last) < PC_PUPPET_TREE_COOLDOWN_FRAMES;

        for (i = 0; i < PC_PUPPET_TREE_RING && !cool; i++) {
            if (s_tree_ring[i].used && s_tree_ring[i].ux == ux && s_tree_ring[i].uz == uz &&
                (now - s_tree_ring[i].t) < PC_PUPPET_TREE_COOLDOWN_FRAMES) {
                cool = 1;
            }
        }
        if (cool) {
            pc_puppet_fx_refund(self);
            st->tree_skipped++;
            pc_puppet_tree_diag(self, "effect", "-", ux, uz, item, species, "cooldown");
            return 1;
        }
        if (!small && Effectbg_pc_count_near(&tpos, PC_PUPPET_TREE_DUP_RADIUS) > 0) {
            /* the local player's own shake (or another effect) is already playing on this tile */
            pc_puppet_fx_refund(self);
            st->tree_skipped++;
            pc_puppet_tree_diag(self, "effect", "-", ux, uz, item, species, "dup");
            return 1;
        }
        /* The shake is accepted (vanilla: Set_shake_tree_table succeeded): stamp the cooldowns, then play the sound. */
        c->tree_committed = 1;
        c->tree_have_last = 1;
        c->tree_last = now;
        s_tree_ring[s_tree_ring_next].used = 1;
        s_tree_ring[s_tree_ring_next].ux = ux;
        s_tree_ring[s_tree_ring_next].uz = uz;
        s_tree_ring[s_tree_ring_next].t = now;
        s_tree_ring_next = (s_tree_ring_next + 1) % PC_PUPPET_TREE_RING;
        {
            const char* sreason = pc_puppet_fx_reason(self, slot, game, 1);

            if (sreason == NULL) {
                xyz_t spos = tpos;

                sAdo_OngenTrgStart(NA_SE_TREE_YURASU, &spos); /* Player_actor_sound_tree_yurasu(target_pos) */
                st->tree_sounds++;
                pc_puppet_tree_diag(self, "sound", "0x135", ux, uz, item, species, "ok");
            } else {
                st->sounds_suppressed++;
                pc_puppet_tree_diag(self, "sound", "0x135", ux, uz, item, species, sreason);
            }
        }
    }

    /* effect: never evict (Make_EffectBG overwrites the OLDEST slot when all 3 are busy) and keep >= 1 slot free */
    if (Common_Get(clip).make_effect_bg_proc == NULL) {
        pc_puppet_fx_refund(self);
        st->tree_skipped++;
        pc_puppet_tree_diag(self, "effect", "-", ux, uz, item, species, "null");
        return 1;
    }
    if (!small) {
        int active = Effectbg_pc_count_active();

        if (active < 0) {
            pc_puppet_fx_refund(self);
            st->tree_skipped++;
            pc_puppet_tree_diag(self, "effect", "-", ux, uz, item, species, "null");
            return 1;
        }
        if (active > PC_PUPPET_TREE_EFBG_MAX_ACTIVE) {
            pc_puppet_fx_refund(self);
            st->tree_skipped++;
            pc_puppet_tree_diag(self, "effect", "-", ux, uz, item, species, "pool");
            return 1;
        }
    }
    if (s_tree_global_valid && (now - s_tree_global_last) < PC_PUPPET_TREE_GLOBAL_SPACING) {
        pc_puppet_fx_refund(self);
        if (!expired) {
            return 0;
        }
        st->tree_skipped++;
        pc_puppet_tree_diag(self, "effect", "-", ux, uz, item, species, "capped");
        return 1;
    }
    {
        xyz_t epos;

        if (mFI_UtNum2CenterWpos(&epos, ux, uz)) { /* Player_actor_Set_EffectBgTree: tile centre, y = tile centre height */
            epos.y = mCoBG_UtNum2UtCenterY_Keep(ux, uz);
            Common_Get(clip).make_effect_bg_proc(game, EffectBG_EFFECT_SHAKE_LARGE, variant, &epos);
            s_tree_global_valid = 1;
            s_tree_global_last = now;
            st->tree_effects++;
            pc_puppet_tree_diag(self, "effect", small ? "young-tree" : "canopy", ux, uz, item, species, "ok");
        } else {
            pc_puppet_fx_refund(self);
            st->tree_skipped++;
            pc_puppet_tree_diag(self, "effect", "-", ux, uz, item, species, "notree");
        }
    }
    return 1;
}

/* Per puppet move (called from pc_remote_player_cosmetics): arms on each SHAKE_TREE run (entry edge = a new
 * row_start_time), fires once when kf0 reaches frame 10, retries a cap-denied effect for a while, and disarms when the run
 * ends/changes. A latched shake whose state was already replaced by walking/idle still plays its clip to the end, so it
 * still presents (the visual matches the clip); a different row state or a scene clear disarms it before frame 10. */
static void pc_puppet_tree_shake_tick(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, double now,
                                      float cur, float last, int have_last, float max_frames, double row_start_now) {
    PCRemotePlayerCosmetic* c = &self->cos;
    PCRemotePlayerVisual* v = &self->visual;

    if (!(v->row_active && v->row_idx == mPlayer_INDEX_SHAKE_TREE)) {
        if (c->tree_armed && c->tree_deadline != 0.0) {
            slot->cos_stats.tree_skipped++; /* still retrying a capped effect when the run ended: final "capped" */
            pc_puppet_tree_diag(self, "effect", "-", -1, -1, EMPTY_NO, "normal", "capped");
        }
        c->tree_armed = 0;
        c->tree_row_start = 0.0;
        return;
    }
    if (c->tree_row_start != row_start_now) { /* new entry edge (incl. a repeated shake with a new entry counter) */
        c->tree_row_start = row_start_now;
        c->tree_armed = 1;
        c->tree_committed = 0;
        c->tree_deadline = 0.0;
        return; /* never fires on the entry frame itself */
    }
    if (!c->tree_armed) {
        return;
    }
    if (c->tree_deadline == 0.0) {
        int hit = (have_last && pc_puppet_frame_crossed(last, cur, max_frames, PC_PUPPET_TREE_FRAME)) ||
                  cur >= PC_PUPPET_TREE_FRAME; /* a hitch step may skip the window: armed-once makes `>=` equivalent */

        if (!hit) {
            return;
        }
        c->tree_deadline = now + PC_PUPPET_TREE_RETRY_FRAMES;
    }
    if (pc_puppet_tree_shake_fire(self, slot, game, now, now >= c->tree_deadline)) {
        c->tree_armed = 0;
        c->tree_deadline = 0.0;
    }
}

/* ====================================================================================================================
 * M9-C Phase 5: remote PICKUP presentation (flying item + sounds), cosmetic only.
 *
 * Vanilla (m_player_main_pickup.c_inc): PICKUP state (index 30), anim PICKUP1 (29 frames), morph -6, speed 0.5. The state's
 * item timer runs in game frames from state entry: < 20 the item stays on the ground (scale 0.01) and item_offset =
 * ground - left hand is refreshed; 20..40 pos = hand + p * offset, scale = p * 0.01, p = 1 - (timer-20)/20; >= 40 gone.
 * Sounds at clip frame 10 (NA_SE_ITEM_GET) and 20 (NA_SE_GASAGOSO). The draw (m_player_draw.c_inc:311-353) is
 * bg_item_clip->single_draw_proc(item, pos, scale), FG scenes only.
 * The puppet has no morph phase (its clip starts at once, 12 game frames = 6/0.5 ahead of vanilla's clip), so the vanilla
 * timer is reconstructed from the puppet's own clip frame: timer = 12 + 2 * (frame - 1) (speed 0.5). The item therefore
 * stays in step with the hand pose exactly like the vanilla one. The item id and tile come from the host's PLAYER_ACTION
 * (pc_remote_player_on_action): the ground tile may already be cleared by the FIELD_UPDATE (the item is removed from the
 * ground slightly before the puppet reaches for it) so it cannot be reconstructed from the field. Bells (money bags)
 * get no flying item (wallet path). A rejected/aborted pickup has no event: the pose plays with no item. The sounds are
 * keyed on the pickup clip itself (the sender plays them for any pickup attempt).
 * Nothing here writes the field, the inventory or any gameplay state.
 * ================================================================================================================== */

#define PC_PUPPET_PK_HOLD_FRAMES 90.0f      /* a queued event waits this long (game frames) for the puppet's pickup state */
#define PC_PUPPET_PK_TIMER_BASE 12.0f       /* vanilla morph -6 at 0.5/frame: clip frame 1 is at timer 12 */
#define PC_PUPPET_PK_TIMER_PER_FRAME 2.0f   /* 1 / row speed 0.5 */
#define PC_PUPPET_PK_FLY_START 20.0f        /* timer at which the item starts flying to the hand */
#define PC_PUPPET_PK_TIMER_END 40.0f        /* ... and is gone */
#define PC_PUPPET_PK_END_CF (1.0f + (PC_PUPPET_PK_TIMER_END - PC_PUPPET_PK_TIMER_BASE) / PC_PUPPET_PK_TIMER_PER_FRAME)
#define PC_PUPPET_PK_LATE_CF 2.0f           /* bound later than this many clip frames after the start = "late" */
#define PC_PUPPET_PK_SOUND_GET_FRAME 10.0f
#define PC_PUPPET_PK_SOUND_RUSTLE_FRAME 20.0f

static void pc_pk_diag_event(int peer, uint16_t seq, int ux, int uz, uint16_t item, const char* state, const char* budget) {
    static int s_n = 0;

    if (!pc_remote_player_puppet_diag() || s_n >= 400) {
        return;
    }
    s_n++;
    printf("[NET][PUPPET][DIAG] player %d pickup event seq=%u tile=(%d,%d) item=0x%04X state=%s%s%s\n", peer,
           (unsigned)seq, ux, uz, (unsigned)item, state, budget != NULL ? " budget=" : "", budget != NULL ? budget : "");
}

/* Bell/money-bag items are credited to the wallet by the vanilla pickup state (PC_ENHANCEMENTS): no flying item. */
static int pc_pk_item_is_bell(uint16_t item) {
#ifdef PC_ENHANCEMENTS
    if (mNT_check_unknown((mActor_name_t)item)) {
        return 0; /* the lookup indexes its tables unchecked: never look up an unknown id */
    }
    return mPr_GetAmountForMoneyItem((mActor_name_t)item) > 0;
#else
    (void)item;
    return 0;
#endif
}

/* Same checks as pc_puppet_fx_reason() minus the budgets (the flying item is one draw, no effect/sound slot). NULL = ok. */
static const char* pc_pk_gate(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game) {
    GAME_PLAY* play = (GAME_PLAY*)game;
    PLAYER_ACTOR* local;
    float dx, dz, dy;

    if (!pc_remote_player_scene_is_local_field(slot, play) || slot->snapshot_count == 0 || !self->visual.initialized) {
        return "scene";
    }
    if (self->visual.row_active) {
        const PCStateRow* hrow = pc_remote_player_row_for(self->visual.row_idx);
        if (hrow != NULL && (hrow->flags & PC_ROWF_HIDE_BODY)) {
            return "hidden";
        }
    }
    if (_Game_play_isPause(play) == 1) {
        return "pause";
    }
    if (Common_Get(clip).bg_item_clip == NULL || Common_Get(clip).bg_item_clip->single_draw_proc == NULL) {
        return "null";
    }
    if (!Common_Get(player_actor_exists)) {
        return "scene";
    }
    local = GET_PLAYER_ACTOR(play);
    if (local == NULL || (uint64_t)(uintptr_t)local >= PC_LOWADDR_LIMIT) {
        return "scene";
    }
    dx = self->actor_class.world.position.x - local->actor_class.world.position.x;
    dz = self->actor_class.world.position.z - local->actor_class.world.position.z;
    dy = self->actor_class.world.position.y - local->actor_class.world.position.y;
    if (!(fabsf(dx) <= PC_PUPPET_FX_EFFECT_RANGE && fabsf(dz) <= PC_PUPPET_FX_EFFECT_RANGE &&
          fabsf(dy) <= PC_PUPPET_FX_EFFECT_RANGE)) {
        return "range";
    }
    return NULL;
}

/* Ground position of the picked item: Player_actor_CheckItemPosition_forPickup's target (tile centre, y = bg height with a -1
 * offset in FG) + bIT_actor_rand_pos_get_plus (weeds and shells are displaced by a fixed per-tile table). */
static int pc_pk_ground_pos(int ux, int uz, uint16_t item, xyz_t* out) {
    static const f32 rnd_x[4][4] = { { 7.5f, 0.0f, -5.0f, 5.0f }, { -5.0f, -7.5f, 2.5f, -2.5f },
                                     { 7.5f, 10.0f, -2.5f, 5.0f }, { -2.5f, 5.0f, 7.5f, -2.5f } };
    static const f32 rnd_z[4][4] = { { -5.0f, 0.0f, 5.0f, 0.0f }, { -5.0f, 2.5f, 0.0f, 5.0f },
                                     { -2.5f, 0.0f, 7.5f, 5.0f }, { 7.5f, -7.5f, -2.5f, -5.0f } };
    xyz_t c;

    if (!mFI_UtNum2CenterWpos(&c, ux, uz)) {
        return 0;
    }
    c.y = mCoBG_GetBgY_OnlyCenter_FromWpos2(c, -1.0f);
    if (IS_ITEM_GRASS(item) || (item >= ITM_SHELL_START && item <= ITM_SHELL7)) {
        c.x += rnd_x[ux & 3][uz & 3];
        c.z += rnd_z[ux & 3][uz & 3];
    }
    *out = c;
    return 1;
}

/* One positional one-shot (the vanilla Player_actor_sound_ITEM_GET / _GASAGOSO body: sAdo_OngenTrgStart at the actor). */
static void pc_pk_snd(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, u16 id, const char* name, float frame) {
    const char* reason = pc_puppet_fx_reason(self, slot, game, 1);
    PCRemotePlayerCosStats* st = &slot->cos_stats;

    if (reason == NULL) {
        xyz_t pos = self->actor_class.world.position;

        sAdo_OngenTrgStart(id, &pos);
        st->sounds++;
        st->pk_sounds++;
        pc_puppet_fx_diag(self, name, name, frame, (int)id, "ok");
    } else {
        st->sounds_suppressed++;
        pc_puppet_fx_diag(self, name, name, frame, -1, reason);
    }
}

/* Per puppet move (from pc_remote_player_cosmetics, after the clip advanced). */
static void pc_puppet_pickup_tick(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, double now, float cur,
                                  float last, int have_last, float max_frames, double row_start_now) {
    PCRemotePlayerCosmetic* c = &self->cos;
    PCRemotePlayerVisual* v = &self->visual;
    PCRemotePlayerCosStats* st = &slot->cos_stats;
    int peer = (int)self->peer;
    int in_row = v->row_active && v->row_idx == mPlayer_INDEX_PICKUP;
    int i;

    /* queued events that never met a pickup state expire */
    for (i = 0; i < PC_PUPPET_PK_RING; i++) {
        PCRemotePickupEvent* ev = &slot->pk_ev[i];

        if (ev->valid && (now - ev->arrival) > (double)PC_PUPPET_PK_HOLD_FRAMES) {
            ev->valid = 0;
            st->pk_dropped++;
            pc_pk_diag_event(peer, ev->seq, ev->ux, ev->uz, ev->item, "dropped", "expired");
        }
    }

    if (!in_row) {
        c->pk_row_start = 0.0;
        c->pk_active = 0;
        c->pk_bound = 0;
        return;
    }
    if (c->pk_row_start != row_start_now) { /* a new PICKUP run (incl. a repeated pickup with a new entry counter) */
        c->pk_row_start = row_start_now;
        c->pk_active = 0;
        c->pk_bound = 0;
        c->pk_ofs_valid = 0;
    }

    /* sounds: clip frames 10 / 20 (Player_actor_SetSound_Pickup); never on the entry frame */
    if (have_last) {
        if (pc_puppet_frame_crossed(last, cur, max_frames, PC_PUPPET_PK_SOUND_GET_FRAME)) {
            pc_pk_snd(self, slot, game, (u16)NA_SE_ITEM_GET, "pickup_item_get", cur);
        }
        if (pc_puppet_frame_crossed(last, cur, max_frames, PC_PUPPET_PK_SOUND_RUSTLE_FRAME)) {
            pc_pk_snd(self, slot, game, (u16)NA_SE_GASAGOSO, "pickup_gasagoso", cur);
        }
    }

    /* pairing: the oldest queued event binds to this run (one per run); an event that is only seen after the flight is over
     * (clip still playing) is dropped; once the clip FINISHED, events wait for the next run (they most likely belong to
     * the next pickup, whose MOVE has not shown up yet) */
    if (!c->pk_bound && !v->row_finished) {
        int idx = -1;
        PCRemotePickupEvent ev;

        for (i = 0; i < PC_PUPPET_PK_RING; i++) {
            if (slot->pk_ev[i].valid && (idx < 0 || slot->pk_ev[i].arrival < slot->pk_ev[idx].arrival)) {
                idx = i;
            }
        }
        if (idx < 0) {
            return;
        }
        ev = slot->pk_ev[idx];
        slot->pk_ev[idx].valid = 0;
        c->pk_bound = 1;
        if (pc_pk_item_is_bell(ev.item)) {
            st->pk_bell++;
            pc_pk_diag_event(peer, ev.seq, ev.ux, ev.uz, ev.item, "bell", NULL);
        } else if (cur >= PC_PUPPET_PK_END_CF) {
            st->pk_dropped++;
            pc_pk_diag_event(peer, ev.seq, ev.ux, ev.uz, ev.item, "dropped", "late");
        } else {
            const char* reason = pc_pk_gate(self, slot, game);

            if (reason == NULL && !pc_pk_ground_pos(ev.ux, ev.uz, ev.item, &c->pk_ground)) {
                reason = "tile";
            }
            if (reason != NULL) {
                st->pk_ignored++;
                pc_pk_diag_event(peer, ev.seq, ev.ux, ev.uz, ev.item, "ignored", reason);
            } else {
                int late = (cur - 1.0f) >= PC_PUPPET_PK_LATE_CF;

                c->pk_active = 1;
                c->pk_item = ev.item;
                c->pk_ofs_valid = 0;
                if (late) {
                    st->pk_late++;
                } else {
                    st->pk_started++;
                }
                pc_pk_diag_event(peer, ev.seq, ev.ux, ev.uz, ev.item, late ? "late" : "started", NULL);
                if (pc_remote_player_puppet_diag()) {
                    printf("[NET][PUPPET][DIAG] player %d pickup flying item start frame=%.1f\n", peer, (double)cur);
                }
            }
        }
    }
}

/* Draw (puppet dw, after the body and the carried item): mirrors Player_actor_Set_Item_Pickup's timeline and the draw at
 * m_player_draw.c_inc:348-353, with the puppet's own item position/scale and the hand captured by its own callback. */
static void pc_remote_player_draw_pickup_item(PCRemotePlayerActor* self, GAME* game) {
    PCRemotePlayerCosmetic* c = &self->cos;
    PCRemotePlayerVisual* v = &self->visual;
    PCRemotePlayerSlot* slot;
    float timer, p, scale;
    xyz_t pos;

    if (!c->pk_active || !v->row_active || v->row_idx != mPlayer_INDEX_PICKUP) {
        return;
    }
    if (c->hand_frame != game->frame_counter + 1u) {
        return; /* no fresh hand this frame (hidden body etc.) */
    }
    slot = pc_remote_player_get_slot(self->peer);
    if (slot == NULL || pc_pk_gate(self, slot, game) != NULL ||
        mFI_GET_TYPE(mFI_GetFieldId()) != mFI_FIELD_FG) { /* vanilla: single_draw_proc only in FG scenes */
        return;
    }
    timer = PC_PUPPET_PK_TIMER_BASE + (v->keyframe0.frame_control.current_frame - 1.0f) * PC_PUPPET_PK_TIMER_PER_FRAME;
    if (!(timer < PC_PUPPET_PK_TIMER_END)) {
        return; /* flight over (vanilla: scale 0, nothing drawn) */
    }
    if (timer < PC_PUPPET_PK_FLY_START || !c->pk_ofs_valid) {
        /* timer < 20: item_offset = item_pos - left_hand_pos every frame (vanilla); a late-bound run captures it once */
        c->pk_ofs.x = c->pk_ground.x - c->hand_pos.x;
        c->pk_ofs.y = c->pk_ground.y - c->hand_pos.y;
        c->pk_ofs.z = c->pk_ground.z - c->hand_pos.z;
        c->pk_ofs_valid = 1;
    }
    if (timer < PC_PUPPET_PK_FLY_START) {
        pos = c->pk_ground;
        scale = 0.01f;
    } else {
        p = 1.0f - (timer - PC_PUPPET_PK_FLY_START) / 20.0f;
        if (p < 0.0f) {
            p = 0.0f;
        } else if (p > 1.0f) {
            p = 1.0f;
        }
        scale = p * 0.01f;
        pos.x = c->hand_pos.x + p * c->pk_ofs.x;
        pos.y = c->hand_pos.y + p * c->pk_ofs.y;
        pos.z = c->hand_pos.z + p * c->pk_ofs.z;
    }
    if (scale > 0.0f && c->pk_item != (u16)EMPTY_NO) {
        Common_Get(clip).bg_item_clip->single_draw_proc(game, (mActor_name_t)c->pk_item, &pos, scale, NULL, NULL, NULL);
    }
}

/* Foot step on foot `idx` (0 left, 1 right): Set_FootMark_Base1 (footprint decal + footstep sound) followed by
 * SetEffect_Walk/Dash (m_player_main_walk.c_inc:106, m_player_main_dash.c_inc:73). The 1-in-4 flower trample of
 * SetEffectRemoveFlower_Dash is NOT replayed (world mutation): plain dash dust only. The three pieces are pending bits:
 * a piece denied ONLY by a frame cap (per-frame cap across all puppets, or the sound cap) stays pending and is retried on
 * the next moves for PC_PUPPET_PENDING_FRAMES, so identical-phase puppets take turns instead of the same ones always
 * winning; pieces denied by any other reason (scene/range/pool/...) are final. Expiry counts the leftovers as capped. */
/* ====================================================================================================================
 * M9-C Phase 6a: remote held-tool presentation (axe / net / fishing rod / fan / umbrella), cosmetic only.
 *
 * Table-driven, keyed on the puppet's state row (visual.row_idx) and keyframe0 frame exactly like the tree-shake/pickup
 * ticks: a run is armed on a new visual.row_start_time (entry edge); `frame == 0` events fire on that entry tick (vanilla
 * fires them inside setup_main_X), `frame > 0` events fire on a frame crossing of the puppet's own clip
 * (pc_puppet_frame_crossed), never on the entry tick; a run interrupted by another row before the frame never fires.
 * Every effect goes through pc_puppet_fx_emit_ex and every sound through pc_puppet_tool_snd (scene/hidden/pause/null/
 * local-player/range/pool/per-puppet bucket/global per-frame caps, diag limiter); a cap-denied event stays "due" and is
 * retried for PC_PUPPET_TOOL_RETRY_FRAMES. The sounds are plain sAdo_OngenTrgStart(id, pos) with the id the vanilla
 * Player_actor_sound_* wrapper uses (m_player_sound.c_inc:84-217): never Player_actor_sound_*. The effects read no local
 * player state: ef_swing_axe/ef_swing_rod/ef_kasamizu/ef_turn_asimoto/ef_break_axe/ef_kikuzu/ef_impact_star (audited:
 * only the effect clip, season/weather, the tile at the effect position and the global gameplay fqrand()/RANDOM_F stream
 * inside the effect internals (ACCEPTED RESIDUE, see impl_notes 'Review round 2 fixes'; mostly the global fqrand() stream, several effects also draw RANDOM2, impact_star only RANDOM; streams are not synchronized across processes, so no desync)). The ONE effect with
 * a local-player read in this family (ef_swing_net.c:68, only in the water-hit branch arg1 != 0) is never spawned; the
 * arg1 == 0 swoosh is spawned by vanilla only from settle_main_Swing_net when the state is LEFT inside (8.5, 9.0], and
 * produces only bush leaves / flower petals: it is deferred (the puppet cannot observe the vanilla exit instant).
 * ================================================================================================================== */

#define PC_PUPPET_TOOL_RETRY_FRAMES 30.0   /* a cap-denied tool event is retried this long */
#define PC_PUPPET_TOOL_REPEAT_GUARD 8.0    /* min spacing of a repeating tool sound (clip wrap alias guard) */
#define PC_PUPPET_TOOL_MAX_EVENTS 10

enum { PC_TOOL_FX = 0, PC_TOOL_SND = 1 };
enum { PC_TPOS_WORLD = 0, PC_TPOS_AXE_HIT, PC_TPOS_AXE_TARGET, PC_TPOS_NET,
       PC_TPOS_SCOOP_TGT,  /* Phase 6b: the shovel's target tile (read-only mirror of mPlib_Check_scoop_after's tile choice) */
       PC_TPOS_REFLECT,    /* Phase 6b: reflect_scoop effect point 37 ahead / 2 aside of the puppet */
       PC_TPOS_FEEL,       /* Phase 6b: FEEL joint captured by the puppet's own draw */
       PC_TPOS_GRASS };    /* Phase 6b: remove_grass item position (tile found at 20/10 ahead, vanilla weed offset) */
enum { PC_TCOND_ANY = 0, PC_TCOND_NOT_STUMP, PC_TCOND_STUMP };
enum { PC_TFL_DIGSRC = 1, PC_TFL_ATTR_TGT = 2, PC_TFL_ATTR0 = 4, PC_TFL_ANGLE0 = 8 };
/* dynamic ids resolved at fire time (never valid NA_SE / eEC ids) */
#define PC_TID_ARAIIKI 0xFFF1u    /* tired: gender-dependent breath (Player_actor_sound_araiiki) */
#define PC_TID_REFLECT_SND 0xFFF2u /* reflect_scoop: target-dependent hit sound (Player_actor_SetSound_Reflect_scoop) */
#define PC_TID_REFLECT_FX 0xFFF3u  /* reflect_scoop: DIG_SCOOP arg1 1, attribute chosen from the target tile */
#define PC_TOOL_NA_SLIP 0x4129 /* Player_actor_sound_slip (m_player_sound.c_inc:156) */

typedef struct PCToolEvent {
    uint8_t kind;       /* PC_TOOL_FX / PC_TOOL_SND */
    uint8_t pos;        /* PC_TPOS_* */
    uint8_t repeat;     /* fires on every crossing of a REPEAT clip (fan) instead of once per run */
    s16     arg1;       /* effect arg1 */
    u16     id;         /* eEC_EFFECT_* or NA_SE_* */
    float   frame;      /* 0 = entry tick, else keyframe0 frame (Check_AnimationFrame target) */
    const char* name;   /* diag name */
    uint8_t cond;       /* Phase 6b: PC_TCOND_* (evaluated on the reconstructed shovel target tile) */
    uint8_t fl;         /* Phase 6b: PC_TFL_* effect options */
} PCToolEvent;

typedef struct PCToolRow {
    int row;            /* mPlayer_INDEX_* */
    int n;
    PCToolEvent ev[PC_PUPPET_TOOL_MAX_EVENTS];
} PCToolRow;

/* Sources (m_player_main_*.c_inc / m_player_common.c_inc):
 * SWING_AXE 37: swing_axe:230-237 FURI frame 10 (SetSound_AXE_FURI_axe_common), :72-76 SetEffectStart_axe_common (common
 *   .c_inc:3601) SWING_AXE arg1 1 at frame 12, :53 SetEffectHit_axe_common SWING_AXE arg1 0 at frame 15 (offset -7,20,24),
 *   :240-246 AXE_CUT at frame 15 at the target tile (puppet: forward approximation). AIR_AXE 38: air_axe:32-37 FURI 10 + start 12.
 * REFLECT_AXE 39: reflect_axe:73-76 hit effect arg1 2 (impact stars) at 15 + start 12, :153-173 FURI 10, AXE_HIT at 15 (rock/
 *   ground; a snowman/ball target would be NA_SE_AXE_BALL_HIT / silent: unknown to the puppet -> AXE_HIT).
 * BROKEN_AXE 40: broken_axe:104-110 SWING_AXE arg1 3 (BREAK_AXE x2) at frame 15 + the swing/reflect search (FURI 10, start 12)
 *   + ChangeItemNo_axe_common (swing_axe:253-278) NA_SE_TOOL_BROKEN3 at 15 (axe_damage_no 0 -> EMPTY_NO). The type-specific hit
 *   effect/sound (swing vs reflect target) is not transmitted and is left out.
 * SLIP_NET 41: slip_net:30-36 slip sound + TURN_ASIMOTO arg1 1 on entry (the per-frame SLIP effect :85 and the exit
 *   SLIP_FOOTPRINT :44 are deferred). SWING_NET 44: swing_net:37 AMI_FURI at net_pos on entry. PULL_NET 45: the catch
 *   sound AMI_GET (swing_net:215, `CatchSomethingCheck`) plays in the SWING state when the insect enters the net; PULL_NET is
 *   only entered after a catch, so its entry is the closest observable edge (up to one swing late). AMI_HIT (BG/actor hit,
 *   swing_net:310-320) is a hit/miss branch the puppet cannot tell: not played. PUTAWAY_NET 48: putaway_net:28 GASAGOSO.
 * CAST_ROD 50: cast_rod:25-27 SWING_ROD arg1 0 on entry + :39-45 ROD_STROKE frame 20. AIR_ROD 51: air_rod:27-29 SWING_ROD
 *   entry + :38-41 ROD_STROKE_SMALL frame 20. COLLECT_ROD 53: collect_rod:20-24 ROD_BACK + SWING_ROD on entry. FLY_ROD 55:
 *   fly_rod:17 ROD_BACK on entry (no effect). PUTAWAY_ROD 57: putaway_rod:32 GASAGOSO on entry. READY/RELAX/VIB/NOTICE_ROD
 *   have no player-side sound/effect except notice_rod's YAJIRUSHI (killifish type of the LOCAL bobber: deferred).
 * ROTATE_UMBRELLA 103: rotate_umbrella:17-20 KASAMIZU (arg0 0, arg1 0) + UMBRELLA_ROTATE on entry. SWING_FAN 109:
 *   swing_fan:51-55 UCHIWA at frame 1.5 of every pass of the REPEAT clip. */
static const PCToolRow s_tool_rows[] = {
    { mPlayer_INDEX_SWING_AXE, 4, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_TOOL_FURI, 10.0f, "axe_furi", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_WORLD, 0, 1, eEC_EFFECT_SWING_AXE, 12.0f, "swing_axe_start", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_AXE_HIT, 0, 0, eEC_EFFECT_SWING_AXE, 15.0f, "swing_axe_hit", 0, 0 },
        { PC_TOOL_SND, PC_TPOS_AXE_TARGET, 0, 0, NA_SE_AXE_CUT, 15.0f, "axe_cut", 0, 0 } } },
    { mPlayer_INDEX_AIR_AXE, 2, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_TOOL_FURI, 10.0f, "axe_furi", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_WORLD, 0, 1, eEC_EFFECT_SWING_AXE, 12.0f, "swing_axe_start", 0, 0 } } },
    { mPlayer_INDEX_REFLECT_AXE, 4, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_TOOL_FURI, 10.0f, "axe_furi", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_WORLD, 0, 1, eEC_EFFECT_SWING_AXE, 12.0f, "swing_axe_start", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_AXE_HIT, 0, 2, eEC_EFFECT_SWING_AXE, 15.0f, "reflect_axe_hit", 0, 0 },
        { PC_TOOL_SND, PC_TPOS_AXE_TARGET, 0, 0, NA_SE_AXE_HIT, 15.0f, "axe_hit", 0, 0 } } },
    { mPlayer_INDEX_BROKEN_AXE, 4, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_TOOL_FURI, 10.0f, "axe_furi", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_WORLD, 0, 1, eEC_EFFECT_SWING_AXE, 12.0f, "swing_axe_start", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_WORLD, 0, 3, eEC_EFFECT_SWING_AXE, 15.0f, "break_axe", 0, 0 },
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_TOOL_BROKEN3, 15.0f, "axe_broken", 0, 0 } } },
    { mPlayer_INDEX_SLIP_NET, 2, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, PC_TOOL_NA_SLIP, 0.0f, "net_slip", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_WORLD, 0, 1, eEC_EFFECT_TURN_ASIMOTO, 0.0f, "net_slip_asimoto", 0, 0 } } },
    { mPlayer_INDEX_SWING_NET, 1, {
        { PC_TOOL_SND, PC_TPOS_NET, 0, 0, NA_SE_TOOL_FURI, 0.0f, "ami_furi", 0, 0 } } },
    { mPlayer_INDEX_PULL_NET, 1, {
        { PC_TOOL_SND, PC_TPOS_NET, 0, 0, NA_SE_TOOL_GET, 0.0f, "ami_get", 0, 0 } } },
    { mPlayer_INDEX_PUTAWAY_NET, 1, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_GASAGOSO, 0.0f, "net_putaway_gasagoso", 0, 0 } } },
    { mPlayer_INDEX_CAST_ROD, 2, {
        { PC_TOOL_FX, PC_TPOS_WORLD, 0, 0, eEC_EFFECT_SWING_ROD, 0.0f, "swing_rod", 0, 0 },
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_ROD_STROKE, 20.0f, "rod_stroke", 0, 0 } } },
    { mPlayer_INDEX_AIR_ROD, 2, {
        { PC_TOOL_FX, PC_TPOS_WORLD, 0, 0, eEC_EFFECT_SWING_ROD, 0.0f, "swing_rod", 0, 0 },
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_ROD_STROKE_SMALL, 20.0f, "rod_stroke_small", 0, 0 } } },
    { mPlayer_INDEX_COLLECT_ROD, 2, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_ROD_BACK, 0.0f, "rod_back", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_WORLD, 0, 0, eEC_EFFECT_SWING_ROD, 0.0f, "swing_rod", 0, 0 } } },
    { mPlayer_INDEX_FLY_ROD, 1, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_ROD_BACK, 0.0f, "rod_back", 0, 0 } } },
    { mPlayer_INDEX_PUTAWAY_ROD, 1, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_GASAGOSO, 0.0f, "rod_putaway_gasagoso", 0, 0 } } },
    { mPlayer_INDEX_ROTATE_UMBRELLA, 2, {
        { PC_TOOL_FX, PC_TPOS_WORLD, 0, 0, eEC_EFFECT_KASAMIZU, 0.0f, "kasamizu", 0, 0 },
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_UMBRELLA_ROTATE, 0.0f, "umbrella_rotate", 0, 0 } } },
    { mPlayer_INDEX_SWING_FAN, 1, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 1, 0, NA_SE_UCHIWA, 1.5f, "uchiwa", 0, 0 } } },
    /* ---- M9-C Phase 6b: shovel states (m_player_main_{dig,fill,reflect,air,get,putaway,putin}_scoop.c_inc) ---- */
    /* DIG_SCOOP 58: SetEffectHit_Dig_scoop (dig_scoop.c_inc:30-68) DIG_HOLE arg 0/1/2 at 14/15/16, DIG_SCOOP arg 0 at 22 at the
     *   target; stump target (DIG_KABU1 clip, not transmitted: the puppet always plays DIG1): DIG_SCOOP arg 0 at 42.
     *   SetSound_Dig_scoop (:171-205): SCOOP1 at 15 (stump: KIRIBASU_SCOOP at 15, KIRIBASU_OUT at 35). Sounds at the digger. */
    { mPlayer_INDEX_DIG_SCOOP, 8, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_SCOOP1, 15.0f, "scoop1", PC_TCOND_NOT_STUMP, 0 },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 0, eEC_EFFECT_DIG_HOLE, 14.0f, "dig_hole0", PC_TCOND_NOT_STUMP, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 1, eEC_EFFECT_DIG_HOLE, 15.0f, "dig_hole1", PC_TCOND_NOT_STUMP, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 2, eEC_EFFECT_DIG_HOLE, 16.0f, "dig_hole2", PC_TCOND_NOT_STUMP, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 0, eEC_EFFECT_DIG_SCOOP, 22.0f, "dig_scoop", PC_TCOND_NOT_STUMP, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_KIRIBASU_SCOOP, 15.0f, "kirikabu_scoop", PC_TCOND_STUMP, 0 },
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_KIRIBASU_OUT, 35.0f, "kirikabu_out", PC_TCOND_STUMP, 0 },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 0, eEC_EFFECT_DIG_SCOOP, 42.0f, "dig_scoop_stump", PC_TCOND_STUMP, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT } } },
    /* FILL_SCOOP 59 (FILL_UP1, mod 0): fill_scoop.c_inc:73-111 DIG_HOLE arg 3/4/5 at 13/19/25 + DIG_SCOOP arg 2 at 40 at the
     *   target; SetSound_Fill_scoop (:113-126) SCOOP_UMERU at 18-7 = 11. */
    { mPlayer_INDEX_FILL_SCOOP, 5, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_SCOOP_UMERU, 11.0f, "scoop_umeru", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 3, eEC_EFFECT_DIG_HOLE, 13.0f, "fill_hole3", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 4, eEC_EFFECT_DIG_HOLE, 19.0f, "fill_hole4", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 5, eEC_EFFECT_DIG_HOLE, 25.0f, "fill_hole5", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 2, eEC_EFFECT_DIG_SCOOP, 40.0f, "fill_scoop", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT } } },
    /* PUTIN_SCOOP 64 (FILL_UP_I1: the shared fill helpers add mod 7): same helpers (putin_scoop.c_inc:62-77): frames 20/26/32/47,
     *   UMERU at 18. */
    { mPlayer_INDEX_PUTIN_SCOOP, 5, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_SCOOP_UMERU, 18.0f, "scoop_umeru", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 3, eEC_EFFECT_DIG_HOLE, 20.0f, "fill_hole3", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 4, eEC_EFFECT_DIG_HOLE, 26.0f, "fill_hole4", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 5, eEC_EFFECT_DIG_HOLE, 32.0f, "fill_hole5", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 2, eEC_EFFECT_DIG_SCOOP, 47.0f, "fill_scoop", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT } } },
    /* REFLECT_SCOOP 60 (NOT_DIG1): reflect_scoop.c_inc:50-137 DIG_SCOOP arg1 1 at frame 13 at 37 ahead + 2 aside (attribute 7 for a
     *   tree/stone target, else the ground attribute there; none for furniture/item targets) and the target-dependent hit sound at 13
     *   (:139-216: bush SHIGEMI, tree/reserve TREE_HIT, furniture/item ITEM_HIT, else wood TREE_HIT / SCOOP_HIT). */
    { mPlayer_INDEX_REFLECT_SCOOP, 2, {
        { PC_TOOL_SND, PC_TPOS_SCOOP_TGT, 0, 0, PC_TID_REFLECT_SND, 13.0f, "reflect_scoop_snd", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_REFLECT, 0, 1, PC_TID_REFLECT_FX, 13.0f, "reflect_scoop", 0, PC_TFL_DIGSRC } } },
    /* AIR_SCOOP 61 (DIG_SUKA1): air_scoop.c_inc:25-32 KARABURI at 13. */
    { mPlayer_INDEX_AIR_SCOOP, 1, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_KARABURI, 13.0f, "karaburi", 0, 0 } } },
    /* GET_SCOOP 62 (GET_D1): get_scoop.c_inc:99-102 -> SetEffectHit_Dig_scoop with main index GET_SCOOP: DIG_HOLE arg 0/1/2 at
     *   14/15/16 + DIG_SCOOP arg 3 at 22; SetSound_Dig_scoop: SCOOP1 at 15 + ITEM_HORIDASHI at 21 (GET_D1). */
    { mPlayer_INDEX_GET_SCOOP, 6, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_SCOOP1, 15.0f, "scoop1", 0, 0 },
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_ITEM_HORIDASHI, 21.0f, "horidashi", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 0, eEC_EFFECT_DIG_HOLE, 14.0f, "dig_hole0", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 1, eEC_EFFECT_DIG_HOLE, 15.0f, "dig_hole1", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 2, eEC_EFFECT_DIG_HOLE, 16.0f, "dig_hole2", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT },
        { PC_TOOL_FX, PC_TPOS_SCOOP_TGT, 0, 3, eEC_EFFECT_DIG_SCOOP, 22.0f, "dig_scoop_get", 0, PC_TFL_DIGSRC | PC_TFL_ATTR_TGT } } },
    /* PUTAWAY_SCOOP 63: putaway_scoop.c_inc:32 GASAGOSO on entry. */
    { mPlayer_INDEX_PUTAWAY_SCOOP, 1, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_GASAGOSO, 0.0f, "scoop_putaway_gasagoso", 0, 0 } } },
    /* ---- M9-C Phase 6b: other filled common-action rows ---- */
    /* TIRED 85: tired.c_inc:26-35 at frame 10 araiiki (gender) + DUST at feel_pos (arg0 0, arg1 0). */
    { mPlayer_INDEX_TIRED, 2, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, PC_TID_ARAIIKI, 10.0f, "araiiki", 0, 0 },
        { PC_TOOL_FX, PC_TPOS_FEEL, 0, 0, eEC_EFFECT_DUST, 10.0f, "tired_dust", 0, PC_TFL_ATTR0 } } },
    /* STRUGGLE_PITFALL 94: setup sets draw_effect_idx = ASE2 + 1 (struggle_pitfall.c_inc:22); the draw callback spawns ASE2 once at
     *   feel_pos on the next draw (m_player_draw.c_inc:106-116, arg0 0, arg1 0). */
    { mPlayer_INDEX_STRUGGLE_PITFALL, 1, {
        { PC_TOOL_FX, PC_TPOS_FEEL, 0, 0, eEC_EFFECT_ASE2, 0.0f, "pitfall_sweat", 0, PC_TFL_ATTR0 } } },
    /* STUNG_BEE 96: stung_bee.c_inc:37 HACHI_SASARERU on entry. */
    { mPlayer_INDEX_STUNG_BEE, 1, {
        { PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_HACHI_SASARERU, 0.0f, "hachi_sasareru", 0, 0 } } },
    /* REMOVE_GRASS 98: remove_grass.c_inc:44-58 at frame 17, only if the target tile still holds grass: DIG_MUD arg1 8 at the
     *   weed (angle 0) + ZASSOU_NUKU at the weed. The weed fly-off / tile change is a world mutation and is NOT replayed. */
    { mPlayer_INDEX_REMOVE_GRASS, 2, {
        { PC_TOOL_FX, PC_TPOS_GRASS, 0, 8, eEC_EFFECT_DIG_MUD, 17.0f, "grass_mud", 0, PC_TFL_ATTR_TGT | PC_TFL_ANGLE0 },
        { PC_TOOL_SND, PC_TPOS_GRASS, 0, 0, NA_SE_ZASSOU_NUKU, 17.0f, "zassou_nuku", 0, 0 } } },
};

static const PCToolRow* pc_puppet_tool_row_for(int row_idx) {
    size_t i;

    for (i = 0; i < sizeof(s_tool_rows) / sizeof(s_tool_rows[0]); i++) {
        if (s_tool_rows[i].row == row_idx) {
            return &s_tool_rows[i];
        }
    }
    return NULL;
}

/* One tool sound: sAdo_OngenTrgStart(label, pos) (positional one-shot, reads only the camera mic position), gated and
 * budgeted like every puppet sound. Returns 0 only when `retry` and the frame cap denied it (caller keeps it due). */
static int pc_puppet_tool_snd(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, const char* name, u16 label,
                              const xyz_t* pos, float frame, int retry) {
    const char* reason = pc_puppet_fx_reason(self, slot, game, 1);
    PCRemotePlayerCosStats* st = &slot->cos_stats;

    if (reason != NULL && retry && strcmp(reason, "capped") == 0) {
        return 0;
    }
    if (reason == NULL) {
        xyz_t p = *pos;

        sAdo_OngenTrgStart(label, &p);
        st->sounds++;
        st->tool_sounds++;
        pc_puppet_fx_diag(self, name, name, frame, (int)label, "ok");
    } else {
        st->sounds_suppressed++;
        pc_puppet_fx_diag(self, name, name, frame, -1, reason);
    }
    return 1;
}

/* 1 = vanilla's effect init would spawn nothing visible here, so the pool slot is not burned: ef_swing_rod_init and the
 * arg1 == 1 branch of ef_swing_axe_init only act on BUSH ground (ef_swing_axe's ct also drops petals on a grown flower
 * tile). Evaluated only for the local scene (collision data of the CURRENT scene); otherwise the normal gate reports it. */
static int pc_puppet_tool_fx_noop(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, int fx_id, int arg1) {
    GAME_PLAY* play = (GAME_PLAY*)game;
    u32 attr;

    if (!(fx_id == eEC_EFFECT_SWING_ROD || (fx_id == eEC_EFFECT_SWING_AXE && arg1 == 1))) {
        return 0;
    }
    if (!pc_remote_player_scene_is_local_field(slot, play) || slot->snapshot_count == 0 || !self->visual.initialized) {
        return 0;
    }
    attr = mCoBG_Wpos2Attribute(self->actor_class.world.position, NULL);
    if (attr == mCoBG_ATTRIBUTE_BUSH) {
        return 0;
    }
    if (fx_id == eEC_EFFECT_SWING_AXE) {
        mActor_name_t* fg_p = mFI_GetUnitFG(self->actor_class.world.position);

        if (fg_p != NULL && IS_ITEM_GROWN_FLOWER(*fg_p)) {
            return 0;
        }
    }
    return 1;
}

static void pc_puppet_tool_pos(const PCRemotePlayerActor* self, int mode, u32 game_frame, xyz_t* out) {
    const ACTOR* a = &self->actor_class;
    s16 rot = a->shape_info.rotation.y; /* swing/reflect/air axe: world.angle.y == shape_info.rotation.y while the state runs */
    f32 s = sin_s(rot);
    f32 c = cos_s(rot);

    *out = a->world.position;
    switch (mode) {
        case PC_TPOS_AXE_HIT: /* Player_actor_SetEffectHit_axe_common offset {-7, 20, 24} */
            out->y += 20.0f;
            out->z += (24.0f * c) - (-7.0f * s);
            out->x += (24.0f * s) + (-7.0f * c);
            break;
        case PC_TPOS_AXE_TARGET: /* the hit tile is not transmitted: one tile-ish ahead of the puppet at chest height */
            out->y += 20.0f;
            out->x += 40.0f * s;
            out->z += 40.0f * c;
            break;
        case PC_TPOS_NET:
            if (self->cos.net_frame != 0u && self->cos.net_frame + 3u >= game_frame + 1u) {
                *out = self->cos.net_pos; /* captured by this puppet's own draw */
            } else { /* vanilla net_pos is ~ 40 along the net handle: 50 ahead at hand height */
                out->y += 20.0f;
                out->x += 50.0f * s;
                out->z += 50.0f * c;
            }
            break;
        default:
            break;
    }
}

/* M9-C Phase 6b: READ-ONLY reconstruction of the shovel's target tile, the tile-choice part of mPlib_Check_scoop_after
 * (m_player_lib.c:2971-3150) for the puppet's own position/facing: the 8 neighbours of the tile under the puppet, the one
 * whose centre is closest to the facing angle (diagonals re-checked for walls / too far like vanilla), result tile centre with
 * y = column height (or the plain ground height for stump/stone/tree/reserve targets). Only field reads (mFI_* / mCoBG_*
 * getters of the CURRENT scene's tables, the caller has verified scene == local scene): no Now_Private, no local player, no
 * world write. The state itself (dig/fill/get/reflect/air) comes from the MOVE main index; vanilla's later classification
 * (wall line checks, NPC/snowman hits, dig status) is not repeated. Returns 1 = tile exists (out/fg_out filled), 0 = none
 * (air). Facing = shape_info.rotation.y like the tree search. */
static int pc_puppet_scoop_target(const PCRemotePlayerActor* self, xyz_t* out, mActor_name_t* fg_out) {
    static const int add_num[8][2] = { { -1, -1 }, { 0, -1 }, { 1, -1 }, { -1, 0 }, { 1, 0 }, { -1, 1 }, { 0, 1 }, { 1, 1 } };
    const xyz_t* pp = &self->actor_class.world.position;
    xyz_t unit_pos[8];
    int unit_num[8][2];
    int unit_exist[8];
    xyz_t unit_dist[8];
    s16 unit_angle_y[8];
    int unit_abs_diff[8];
    xyz_t center;
    int pux, puz, idx = 0, min_angle_y, player_angle_y, i;
    mActor_name_t* fg_p;
    mActor_name_t fg;
    int special;

    if (Common_Get(field_type) != mFI_FIELDTYPE2_FG || !mFI_Wpos2UtNum(&pux, &puz, *pp)) {
        return 0;
    }
    mFI_UtNum2CenterWpos(&center, pux, puz);
    for (i = 0; i < 8; i++) {
        unit_num[i][0] = pux + add_num[i][0];
        unit_num[i][1] = puz + add_num[i][1];
        unit_exist[i] = mFI_UtNum2CenterWpos(&unit_pos[i], unit_num[i][0], unit_num[i][1]);
        unit_pos[i].y = pp->y;
        if (unit_exist[i] == FALSE) {
            unit_pos[i].x = center.x + unit_num[i][0] * mFI_UT_WORLDSIZE_X_F;
            unit_pos[i].z = center.z + unit_num[i][1] * mFI_UT_WORLDSIZE_Z_F;
        }
    }
    for (i = 0; i < 8; i++) {
        unit_dist[i].x = unit_pos[i].x - pp->x;
        unit_dist[i].z = unit_pos[i].z - pp->z;
        unit_angle_y[i] = atans_table(unit_dist[i].z, unit_dist[i].x);
    }
    player_angle_y = self->actor_class.shape_info.rotation.y;
    min_angle_y = DEG2SHORT_ANGLE2(360.0f);
    for (i = 0; i < 8; i++) {
        int d = unit_angle_y[i] - player_angle_y;

        unit_abs_diff[i] = ABS(d);
        if (unit_abs_diff[i] > DEG2SHORT_ANGLE2(180.0f)) {
            unit_abs_diff[i] = DEG2SHORT_ANGLE2(360.0f) - unit_abs_diff[i];
        }
        if (min_angle_y > unit_abs_diff[i]) {
            min_angle_y = unit_abs_diff[i];
            idx = i;
        }
    }
    if (idx == 0 || idx == 2 || idx == 5 || idx == 7) { /* diagonal: wall / distance re-check (vanilla :3071-3140) */
        int near_wall, too_far_away = FALSE, c0, c1, wall0 = FALSE, wall1 = FALSE;
        f32 base_ut_y = mCoBG_GetBgY_OnlyCenter_FromWpos2(unit_pos[idx], 0.0f);

        if (idx == 0) {
            c0 = 1;
            c1 = 3;
        } else if (idx == 2) {
            c0 = 1;
            c1 = 4;
        } else if (idx == 5) {
            c0 = 3;
            c1 = 6;
        } else {
            c0 = 4;
            c1 = 6;
        }
        if (unit_exist[c0]) {
            f32 g0 = mCoBG_GetBgY_OnlyCenter_FromWpos2(unit_pos[c0], 0.0f);
            f32 g1 = mCoBG_Wpos2BgUtCenterHeight_AddColumn(unit_pos[c0]);

            if (g0 != g1 && base_ut_y < g1) {
                wall0 = TRUE;
            }
        }
        if (unit_exist[c1]) {
            f32 g0 = mCoBG_GetBgY_OnlyCenter_FromWpos2(unit_pos[c1], 0.0f);
            f32 g1 = mCoBG_Wpos2BgUtCenterHeight_AddColumn(unit_pos[c1]);

            if (g0 != g1 && base_ut_y < g1) {
                wall1 = TRUE;
            }
        }
        near_wall = (wall0 && wall1) ? TRUE : FALSE;
        if (near_wall == FALSE) {
            f32 dist = Math3DVecLengthSquare2D(unit_dist[idx].z, unit_dist[idx].x);

            too_far_away = (SQ(63.245553f) > dist) ? FALSE : TRUE;
        }
        if (too_far_away || near_wall) {
            static const int card[4] = { 1, 3, 4, 6 };

            min_angle_y = DEG2SHORT_ANGLE2(360.0f);
            for (i = 0; i < 4; i++) {
                if (min_angle_y > unit_abs_diff[card[i]]) {
                    min_angle_y = unit_abs_diff[card[i]];
                    idx = card[i];
                }
            }
        }
    }
    if (unit_exist[idx] == FALSE) {
        return 0;
    }
    fg_p = mFI_UtNum2UtFG(unit_num[idx][0], unit_num[idx][1]);
    fg = (fg_p == NULL) ? (mActor_name_t)EMPTY_NO : *fg_p;
    special = IS_ITEM_TREE_STUMP(fg) || IS_ITEM_STONE_TC(fg) || IS_ITEM_HITTABLE_TREE(fg) || fg == DUMMY_RESERVE;
    unit_pos[idx].y = mCoBG_Wpos2BgUtCenterHeight_AddColumn(unit_pos[idx]);
    *out = unit_pos[idx];
    if (special) {
        out->y = mCoBG_GetBgY_OnlyCenter_FromWpos2(*out, 0.0f);
    }
    *fg_out = fg;
    return 1;
}

/* M9-C Phase 6b: remove_grass target (Player_actor_Search_putin_item, m_player_common.c_inc:7401, GRASS items only): the tile
 * 20 then 10 units in front of the puppet; vanilla item position = tile centre + weed offset (pc_pk_ground_pos mirrors
 * bIT_actor_rand_pos_get_plus), within 15 units / 15 height of the probe. Read-only. 1 = a grass tile found. */
static int pc_pk_ground_pos(int ux, int uz, uint16_t item, xyz_t* out);
static int pc_puppet_find_grass(const PCRemotePlayerActor* self, xyz_t* item_pos) {
    static const f32 dists[2] = { 20.0f, 10.0f };
    s16 ang = self->actor_class.shape_info.rotation.y;
    int k;

    for (k = 0; k < 2; k++) {
        xyz_t probe = self->actor_class.world.position;
        mActor_name_t* fg_p;
        int ux, uz;
        xyz_t ground, c;

        probe.x += dists[k] * sin_s(ang);
        probe.z += dists[k] * cos_s(ang);
        if (mFI_Wpos2DepositGet(probe) != FALSE) {
            continue;
        }
        fg_p = mFI_GetUnitFG(probe);
        if (fg_p == NULL || IS_ITEM_GRASS(*fg_p) == FALSE || !mFI_Wpos2UtNum(&ux, &uz, probe)) {
            continue;
        }
        if (!pc_pk_ground_pos(ux, uz, (uint16_t)*fg_p, &ground)) {
            continue;
        }
        mFI_Wpos2UtCenterWpos(&c, probe);
        c.y = mCoBG_GetBgY_OnlyCenter_FromWpos2(c, 0.0f);
        if (Math3DLengthSquare2D(ground.x, ground.z, probe.x, probe.z) <= SQ(15.0f) &&
            ABS(c.y - probe.y) <= 15.0f) {
            *item_pos = ground;
            return 1;
        }
    }
    return 0;
}

/* Target-dependent reflect_scoop hit sound (Player_actor_SetSound_Reflect_scoop, reflect_scoop.c_inc:139-216) for the
 * "no reflected actor" case (the puppet cannot see the sender's NPC/snowman/ball): bush ground SHIGEMI; tree / reserve
 * TREE_HIT; furniture/item1 ITEM_HIT; else wood ground TREE_HIT, otherwise SCOOP_HIT. */
static u16 pc_puppet_reflect_label(int have_tgt, const xyz_t* tgt, mActor_name_t fg) {
    int type;

    if (!have_tgt) {
        return NA_SE_SCOOP_HIT;
    }
    if (mCoBG_Wpos2Attribute(*tgt, NULL) == mCoBG_ATTRIBUTE_BUSH) {
        return NA_SE_SCOOP_SHIGEMI;
    }
    if (IS_ITEM_COLLIDEABLE_TREE(fg) || fg == DUMMY_RESERVE) {
        return NA_SE_SCOOP_TREE_HIT;
    }
    type = ITEM_NAME_GET_TYPE(fg);
    if (type == NAME_TYPE_FTR0 || type == NAME_TYPE_ITEM1 || type == NAME_TYPE_FTR1) {
        return NA_SE_SCOOP_ITEM_HIT;
    }
    return mCoBG_WoodSoundEffect(tgt) ? NA_SE_SCOOP_TREE_HIT : NA_SE_SCOOP_HIT;
}

static void pc_puppet_scoop_diag(PCRemotePlayerActor* self, const char* name, float frame, int found, const xyz_t* tgt,
                                 mActor_name_t fg) {
    int ux = -1, uz = -1;

    if (!pc_puppet_diag_allow(self, "scoop", name, "ok")) {
        return;
    }
    if (found) {
        (void)mFI_Wpos2UtNum(&ux, &uz, *tgt);
    }
    printf("[NET][PUPPET][DIAG] player %d scoop target event=%s frame=%.1f found=%d tile=(%d,%d) item=0x%04X stump=%d\n",
           (int)self->peer, name, (double)frame, found, ux, uz, (unsigned)fg, found && IS_ITEM_TREE_STUMP(fg) ? 1 : 0);
}

static int pc_puppet_tool_fire(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, const PCToolEvent* e, float cur,
                               int retry) {
    xyz_t pos;
    xyz_t tgt;
    mActor_name_t tfg = (mActor_name_t)EMPTY_NO;
    int have_tgt = 0;
    int need_tgt = (e->pos == PC_TPOS_SCOOP_TGT || e->pos == PC_TPOS_REFLECT || e->cond != PC_TCOND_ANY);
    int mode = e->pos;
    u16 id = e->id;
    PCFxOpts opts;

    memset(&tgt, 0, sizeof(tgt));
    opts.attr_pos = NULL;
    opts.attr_force = -1;
    opts.angle_zero = (e->fl & PC_TFL_ANGLE0) != 0;
    opts.dig_src = (e->fl & PC_TFL_DIGSRC) != 0;
    if (need_tgt || mode == PC_TPOS_GRASS) {
        const char* gate = pc_pk_gate(self, slot, game);

        if (gate != NULL) {
            /* scene / hidden / pause / null / local player / range: do not read any field data for a puppet that is not in the
             * local scene or not near the local player. Review R3-L1: an event that needs a target (or a stump / not-stump
             * condition, or the grass position) is NEVER fired with an unknown target: log the token and finish it, so the
             * sounds' wider range (600 vs 500), a NULL bg_item_clip or similar can neither play both conditional sounds nor
             * spawn a ground effect read at (0,0,0). */
            if (e->kind == PC_TOOL_SND) {
                slot->cos_stats.sounds_suppressed++;
                pc_puppet_fx_diag(self, e->name, e->name, cur, -1, gate);
            } else {
                slot->cos_stats.suppressed++;
                pc_puppet_fx_diag(self, "effect", e->name, cur, -1, gate);
            }
            return 1;
        }
    }
    if (need_tgt) {
        have_tgt = pc_puppet_scoop_target(self, &tgt, &tfg);
        pc_puppet_scoop_diag(self, e->name, cur, have_tgt, &tgt, tfg);
        if ((e->cond == PC_TCOND_NOT_STUMP && have_tgt && IS_ITEM_TREE_STUMP(tfg)) ||
            (e->cond == PC_TCOND_STUMP && !(have_tgt && IS_ITEM_TREE_STUMP(tfg)))) {
            return 1; /* the other variant of this frame (stump vs ground) handles it */
        }
    }
    pc_puppet_tool_pos(self, mode, game->frame_counter, &pos);
    switch (mode) {
        case PC_TPOS_SCOOP_TGT:
            if (e->kind == PC_TOOL_FX) {
                if (!have_tgt) {
                    pc_puppet_fx_diag(self, "effect", e->name, cur, -1, "notarget");
                    return 1;
                }
                pos = tgt;
                opts.attr_pos = &tgt;
            }
            break;
        case PC_TPOS_REFLECT: {
            s16 rot = self->actor_class.shape_info.rotation.y;
            f32 s = sin_s(rot);
            f32 c = cos_s(rot);
            int attr = -1;
            int type = ITEM_NAME_GET_TYPE(tfg);

            pos = self->actor_class.world.position;
            pos.x += (37.0f * s) + (2.0f * c);
            pos.z += (37.0f * c) - (2.0f * s);
            if (have_tgt && IS_ITEM_COLLIDEABLE_TREE(tfg)) {
                attr = mCoBG_ATTRIBUTE_STONE;
            }
            if (have_tgt && IS_ITEM_STONE_TC(tfg) && attr < 0) {
                attr = mCoBG_ATTRIBUTE_STONE;
            }
            if (attr < 0 && (type >= 4 || type < 1)) {
                attr = (int)mCoBG_Wpos2Attribute(pos, NULL);
            }
            if (attr < 0) { /* furniture / item targets: vanilla spawns nothing */
                pc_puppet_fx_diag(self, "effect", e->name, cur, -1, "noop");
                return 1;
            }
            opts.attr_force = attr;
            id = (u16)eEC_EFFECT_DIG_SCOOP;
            break;
        }
        case PC_TPOS_FEEL:
            if (self->cos.feel_frame != 0u && (game->frame_counter + 1u) - self->cos.feel_frame <= 3u) {
                pos = self->cos.feel_pos; /* captured by this puppet's own draw */
                if (pc_puppet_diag_allow(self, "feel", e->name, "ok")) {
                    printf("[NET][PUPPET][DIAG] player %d feel pos source=captured event=%s\n", (int)self->peer, e->name);
                }
            } else if (retry) {
                return 0; /* no recent draw yet: wait (bounded by the retry window), then use the fallback */
            } else {
                pos.y += 40.0f; /* approximate head height */
                if (pc_puppet_diag_allow(self, "feel", e->name, "ok")) {
                    printf("[NET][PUPPET][DIAG] player %d feel pos source=fallback event=%s\n", (int)self->peer, e->name);
                }
            }
            break;
        case PC_TPOS_GRASS:
            if (!pc_puppet_find_grass(self, &pos)) {
                if (e->kind == PC_TOOL_FX) {
                    pc_puppet_fx_diag(self, "effect", e->name, cur, -1, "notarget");
                }
                return 1; /* vanilla: no grass at the target any more -> nothing happens */
            }
            opts.attr_pos = &pos;
            break;
        default:
            break;
    }
    if (e->kind == PC_TOOL_SND) {
        if (id == PC_TID_ARAIIKI) {
            id = (slot->appearance.gender == mPr_SEX_MALE) ? (u16)NA_SE_ARAIIKI_BOY : (u16)NA_SE_ARAIIKI_GIRL;
        } else if (id == PC_TID_REFLECT_SND) {
            id = pc_puppet_reflect_label(have_tgt, &tgt, tfg);
            pos = self->actor_class.world.position; /* sound at the digger */
        }
        return pc_puppet_tool_snd(self, slot, game, e->name, id, &pos, cur, retry);
    }
    {
        int before = slot->cos_stats.spawned;
        int done;

        if (e->fl & PC_TFL_ATTR0) {
            opts.attr_force = 0;
        } else if (e->fl & PC_TFL_ATTR_TGT) {
            opts.attr_pos = (mode == PC_TPOS_GRASS) ? &pos : &tgt;
        }
        done = pc_puppet_fx_emit_core(self, slot, game, (int)id, e->name, &pos, self->actor_class.shape_info.rotation.y, cur,
                                      e->arg1, 0, retry, &opts);
        if (slot->cos_stats.spawned != before) {
            slot->cos_stats.tool_effects++;
            if (id == (u16)eEC_EFFECT_ASE2) {
                self->cos.sweat_active = 1; /* review H1: killed again when the run ends (pc_puppet_sweat_kill) */
            }
        }
        return done;
    }
}

static void pc_puppet_tool_tick(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, double now, float cur,
                                float last, int have_last, float max_frames, double row_start_now, int first_sight) {
    PCRemotePlayerCosmetic* c = &self->cos;
    PCRemotePlayerVisual* v = &self->visual;
    const PCToolRow* tr = v->row_active ? pc_puppet_tool_row_for(v->row_idx) : NULL;
    int i;

    if (tr == NULL) {
        pc_puppet_sweat_kill(self, "left-row"); /* review H1 */
        if (c->tool_due != 0) { /* the run ended/was replaced while events still waited for the budget */
            slot->cos_stats.tool_dropped++;
        }
        c->tool_row_start = 0.0;
        c->tool_done = 0;
        c->tool_due = 0;
        return;
    }
    if (c->tool_row_start != row_start_now) { /* entry edge of a new run (also a repeated same-state entry) */
        pc_puppet_sweat_kill(self, "left-row"); /* review H1: previous run's sweat (a new pitfall run spawns a fresh one below) */
        if (c->tool_due != 0) {
            slot->cos_stats.tool_dropped++;
        }
        c->tool_row_start = row_start_now;
        c->tool_done = 0;
        c->tool_due = 0;
        c->tool_due_time = 0.0;
        for (i = 0; i < tr->n; i++) {
            if (tr->ev[i].frame == 0.0f) {
                if (first_sight) {
                    c->tool_done |= 1u << i; /* a steady row adopted on a fresh actor: no entry edge happened here */
                } else {
                    c->tool_due |= 1u << i;
                    c->tool_due_time = now;
                }
            }
        }
    } else if (have_last) {
        for (i = 0; i < tr->n; i++) {
            const PCToolEvent* e = &tr->ev[i];
            unsigned bit = 1u << i;

            if (e->frame <= 0.0f || (c->tool_done & bit) || (c->tool_due & bit)) {
                continue;
            }
            if (pc_puppet_frame_crossed(last, cur, max_frames, e->frame)) {
                if (e->repeat) {
                    if (now - c->tool_repeat_last < PC_PUPPET_TOOL_REPEAT_GUARD) {
                        continue;
                    }
                    c->tool_repeat_last = now;
                }
                if (c->tool_due == 0) {
                    c->tool_due_time = now;
                }
                c->tool_due |= bit;
            }
        }
    }
    if (c->tool_due == 0) {
        return;
    }
    {
        int expired = (now - c->tool_due_time) > PC_PUPPET_TOOL_RETRY_FRAMES;

        for (i = 0; i < tr->n; i++) {
            unsigned bit = 1u << i;

            if (!(c->tool_due & bit)) {
                continue;
            }
            if (pc_puppet_tool_fire(self, slot, game, &tr->ev[i], cur, !expired)) {
                c->tool_due &= ~bit;
                if (!tr->ev[i].repeat) {
                    c->tool_done |= bit;
                }
            } else if (expired) {
                c->tool_due &= ~bit;
                slot->cos_stats.tool_dropped++;
            }
        }
    }
}

/* Review R3-L4: a frame step above PC_PUPPET_MAX_DT_FRAMES clears the frame window, so held-tool events whose frame lies in
 * the skipped window are lost; count them in tool_dropped (visible in the destroy summary) instead of losing them silently. */
static void pc_puppet_tool_count_hitch(const PCRemotePlayerCosmetic* c, const PCRemotePlayerVisual* v,
                                       PCRemotePlayerSlot* slot, float last, float cur, float max_frames, double row_start_now) {
    const PCToolRow* tr = v->row_active ? pc_puppet_tool_row_for(v->row_idx) : NULL;
    int i;

    if (tr == NULL || c->tool_row_start != row_start_now) {
        return;
    }
    for (i = 0; i < tr->n; i++) {
        const PCToolEvent* e = &tr->ev[i];
        unsigned bit = 1u << i;

        if (e->frame > 0.0f && !(c->tool_done & bit) && !(c->tool_due & bit) &&
            pc_puppet_frame_crossed(last, cur, max_frames, e->frame)) {
            slot->cos_stats.tool_dropped++;
        }
    }
}

static void pc_puppet_pending_run(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, double now) {
    PCRemotePlayerCosmetic* c = &self->cos;
    int idx;

    for (idx = 0; idx < 2 && c->pending != 0; idx++) {
        unsigned bit_fp = 1u << (idx * 3 + PC_PIECE_FOOTPRINT);
        unsigned bit_snd = 1u << (idx * 3 + PC_PIECE_SOUND);
        unsigned bit_fx = 1u << (idx * 3 + PC_PIECE_DUST);
        float frame = (idx == 0) ? PC_PUPPET_FOOT_LEFT_FRAME : PC_PUPPET_FOOT_RIGHT_FRAME;
        xyz_t fpos;
        s16 fang;

        if ((c->pending & (bit_fp | bit_snd | bit_fx)) == 0) {
            continue;
        }
        pc_puppet_foot_get(self, game, idx, &fpos, &fang);
        if ((c->pending & bit_fp) &&
            pc_puppet_fx_emit_ex(self, slot, game, eEC_EFFECT_FOOTPRINT, "footprint", &fpos, fang, frame, 0, 0, 1)) {
            c->pending &= ~bit_fp;
        }
        if ((c->pending & bit_snd) && pc_puppet_snd_emit_ex(self, slot, game, 0, frame, 1)) {
            c->pending &= ~bit_snd;
        }
        if ((c->pending & bit_fx) &&
            pc_puppet_fx_emit_ex(self, slot, game, c->pending_dash ? eEC_EFFECT_DASH_ASIMOTO : eEC_EFFECT_WALK_ASIMOTO,
                                 c->pending_dash ? "dash_asimoto" : "walk_asimoto", &fpos, fang, frame, 0, 1, 1)) {
            c->pending &= ~bit_fx;
        }
    }
    if (c->pending != 0 && now > c->pending_until) {
        unsigned m = c->pending;

        for (idx = 0; idx < 2; idx++) {
            if (m & (1u << (idx * 3 + PC_PIECE_FOOTPRINT))) {
                slot->cos_stats.capped++;
            }
            if (m & (1u << (idx * 3 + PC_PIECE_SOUND))) {
                slot->cos_stats.sounds_suppressed++;
            }
            if (m & (1u << (idx * 3 + PC_PIECE_DUST))) {
                slot->cos_stats.capped++;
            }
        }
        pc_puppet_fx_diag(self, "effect", "foot_step", 0.0f, -1, "capped");
        c->pending = 0;
    }
}

/* Called once per puppet move after the body clip advanced (and the state row was updated). */
static void pc_remote_player_cosmetics(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, double now) {
    PCRemotePlayerCosmetic* c = &self->cos;
    PCRemotePlayerVisual* v = &self->visual;
    cKF_FrameControl_c* fc = &v->keyframe0.frame_control;
    ACTOR* actor = &self->actor_class;
    int anim = v->current_anim_idx;
    int state = PC_COS_NONE;
    int clip_id;
    double row_start_now;
    int have_last;
    int restarted;
    int was_tumble;
    float cur = fc->current_frame;
    float last;
    float dt = (float)game->graph->dt_num_60fps_frames;

    if (v->row_active) {
        if (v->row_idx == mPlayer_INDEX_TUMBLE) {
            state = PC_COS_TUMBLE;
        }
        clip_id = 1000 + v->row_idx;
        row_start_now = v->row_start_time;
    } else {
        if (anim == mPlayer_ANIM_WALK1 || anim == mPlayer_ANIM_RUN1 || anim == mPlayer_ANIM_DASH1) {
            state = PC_COS_MOVE;
        } else if (anim == mPlayer_ANIM_RUN_SLIP1) {
            state = PC_COS_TURN;
        } else if (anim == mPlayer_ANIM_WAIT1) {
            state = (self->cosmetic_move_state == PC_MOVE_STATE_TUMBLE) ? PC_COS_TUMBLE_MV : PC_COS_IDLE;
        }
        clip_id = anim;
        row_start_now = 0.0;
    }

    have_last = c->last_valid && c->last_clip == clip_id;
    restarted = c->last_valid && c->last_row_start != row_start_now;
    last = have_last ? c->last_frame : cur;
    if (restarted) {
        last = cur; /* a (re)started row: no window over the previous run's frames */
        have_last = 0;
    }
    if (have_last && fc->mode == cKF_FRAMECONTROL_STOP && cur < last) {
        last = cur; /* a one-shot clip cannot wrap: it was restarted */
    }
    if (!(dt <= PC_PUPPET_MAX_DT_FRAMES)) {
        if (have_last) {
            pc_puppet_tool_count_hitch(c, v, slot, last, cur, fc->max_frames, row_start_now);
        }
        last = cur; /* hitch: never alias a long step into spurious triggers */
        have_last = 0;
    }
    c->last_frame = cur;
    c->last_clip = clip_id;
    c->last_row_start = row_start_now;
    c->last_valid = 1;

    /* ---- state edges (own interpolated state; first sight adopts silently) ---- */
    was_tumble = (c->state == PC_COS_TUMBLE || c->state == PC_COS_TUMBLE_MV);
    if (!c->known) {
        c->known = 1;
        c->state = state;
    } else {
        int tumble_in = (state == PC_COS_TUMBLE || state == PC_COS_TUMBLE_MV) &&
                        (!was_tumble || (state == PC_COS_TUMBLE && restarted));

        if (state == PC_COS_TURN && c->state != PC_COS_TURN && now >= c->turn_in_block) {
            /* setup_main_Turn_dash_common (m_player_main_turn_dash.c_inc:17-32): skid sound 0x4129 is already played by the
             * Stage 4B.1 block in pc_remote_player_mv; here only the dust/water effect at the puppet position. */
            c->turn_in_block = now + PC_PUPPET_EDGE_COOLDOWN_FRAMES;
            pc_puppet_fx_emit(self, slot, game, eEC_EFFECT_TURN_ASIMOTO, "turn_asimoto", &actor->world.position,
                              actor->world.angle.y, cur, 0, 0);
        }
        if (c->state == PC_COS_TURN && state != PC_COS_TURN && now >= c->turn_out_block) {
            /* settle_main_Turn_dash (:38): turn footprint at the right foot. */
            xyz_t rpos;
            s16 rang;

            c->turn_out_block = now + PC_PUPPET_EDGE_COOLDOWN_FRAMES;
            pc_puppet_foot_get(self, game, 1, &rpos, &rang);
            pc_puppet_fx_emit(self, slot, game, eEC_EFFECT_TURN_FOOTPRINT, "turn_footprint", &rpos, actor->world.angle.y,
                              cur, 0, 0);
        }
        if (tumble_in && now >= c->tumble_block) {
            /* setup_main_Tumble (m_player_main_tumble.c_inc:63-72): sound + TUMBLE effect (arg1 0). */
            c->tumble_block = now + PC_PUPPET_EDGE_COOLDOWN_FRAMES;
            pc_puppet_snd_emit(self, slot, game, 1, cur);
            pc_puppet_fx_emit(self, slot, game, eEC_EFFECT_TUMBLE, "tumble", &actor->world.position,
                              actor->world.angle.y, cur, 0, 0);
        }
        c->state = state;
    }

    /* ---- frame triggers ---- */
    if (state == PC_COS_MOVE && have_last && now >= c->foot_block) {
        int left = pc_puppet_frame_crossed(last, cur, fc->max_frames, PC_PUPPET_FOOT_LEFT_FRAME);
        int right = pc_puppet_frame_crossed(last, cur, fc->max_frames, PC_PUPPET_FOOT_RIGHT_FRAME);
        int dash = (anim == mPlayer_ANIM_DASH1);

        if (left || right) {
            c->foot_block = now + PC_PUPPET_FOOT_COOLDOWN_FRAMES;
            c->pending = (left ? 7u : 0u) | (right ? (7u << 3) : 0u);
            c->pending_dash = dash;
            c->pending_until = now + PC_PUPPET_PENDING_FRAMES;
        }
    }
    if (state == PC_COS_MOVE) {
        if (c->pending != 0) {
            pc_puppet_pending_run(self, slot, game, now);
        }
    } else {
        c->pending = 0; /* left the foot-step clips: drop leftovers */
    }
    if (state == PC_COS_TUMBLE && have_last) {
        /* Player_actor_SetEffect_Tumble (m_player_main_tumble.c_inc:89-103): frame 15 -> TUMBLE (arg1 1), frame 17 ->
         * TUMBLE_BODYPRINT (frame 10 is controller rumble only: not reproduced). */
        if (pc_puppet_frame_crossed(last, cur, fc->max_frames, 15.0f)) {
            pc_puppet_fx_emit(self, slot, game, eEC_EFFECT_TUMBLE, "tumble", &actor->world.position,
                              actor->world.angle.y, 15.0f, 1, 0);
        } else if (pc_puppet_frame_crossed(last, cur, fc->max_frames, 17.0f)) {
            pc_puppet_fx_emit(self, slot, game, eEC_EFFECT_TUMBLE_BODYPRINT, "tumble_bodyprint", &actor->world.position,
                              actor->world.angle.y, 17.0f, 0, 0);
        }
    }

    /* ---- M9-C Phase 4: remote tree shake at SHAKE1 frame 10 ---- */
    pc_puppet_tree_shake_tick(self, slot, game, now, cur, last, have_last, fc->max_frames, row_start_now);

    /* ---- M9-C Phase 5: remote pickup (flying item pairing + clip-frame sounds) ---- */
    pc_puppet_pickup_tick(self, slot, game, now, cur, last, have_last, fc->max_frames, row_start_now);

    /* ---- M9-C Phase 6a: held-tool sounds/effects (axe, net, rod, fan, umbrella) keyed on the state row + clip frame ---- */
    pc_puppet_tool_tick(self, slot, game, now, cur, last, have_last, fc->max_frames, row_start_now, v->row_adopted);

    /* ---- idle ripple (Player_actor_set_ripple, m_player_common.c_inc:6883): ef_wait_asimoto only does something when
     * the ground attribute is WAVE/SEA (it spawns the TURI_HAMON ripple); on any other ground vanilla's call just burns a
     * pool slot for one tick, so the puppet spawns it only for water. ---- */
    if (state == PC_COS_IDLE) {
        c->ripple_accum += dt;
        if (c->ripple_accum >= PC_PUPPET_RIPPLE_PERIOD) {
            c->ripple_accum = 0.0f;
            if (pc_remote_player_scene_is_local_field(slot, (GAME_PLAY*)game)) {
                u32 attr = mCoBG_Wpos2Attribute(actor->world.position, NULL);

                if (attr == mCoBG_ATTRIBUTE_WAVE || attr == mCoBG_ATTRIBUTE_SEA) {
                    xyz_t fpos;
                    s16 fang;

                    pc_puppet_foot_get(self, game, c->ripple_foot, &fpos, &fang);
                    c->ripple_foot ^= 1;
                    pc_puppet_fx_emit(self, slot, game, eEC_EFFECT_WAIT_ASIMOTO, "wait_asimoto", &fpos, fang, cur, 0, 0);
                }
            }
        }
    } else {
        c->ripple_accum = 0.0f;
    }
}

static void pc_puppet_fx_dump_stats(const PCRemotePlayerSlot* slot, int player) {
    if (pc_remote_player_puppet_diag()) {
        const PCRemotePlayerCosStats* st = &slot->cos_stats;

        printf("[NET][PUPPET][DIAG] player %d cosmetics summary: effects spawned=%d capped=%d suppressed=%d sounds=%d "
               "sounds_suppressed=%d peak_frame_spawns=%d tree_effects=%d tree_sounds=%d tree_skipped=%d pickup_events=%d "
               "pickup_started=%d pickup_late=%d pickup_dropped=%d pickup_bell=%d pickup_ignored=%d pickup_sounds=%d "
               "tool_effects=%d tool_sounds=%d tool_noop=%d tool_dropped=%d\n",
               player, st->spawned, st->capped, st->suppressed, st->sounds, st->sounds_suppressed,
               s_puppet_fx_peak_frame, st->tree_effects, st->tree_sounds, st->tree_skipped, st->pk_events, st->pk_started,
               st->pk_late, st->pk_dropped, st->pk_bell, st->pk_ignored, st->pk_sounds, st->tool_effects, st->tool_sounds,
               st->tool_noop, st->tool_dropped);
    }
}

static void pc_remote_player_mv(ACTOR* actor, GAME* game) {
    PCRemotePlayerActor* self = (PCRemotePlayerActor*)actor;
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(self->peer);
    PCRemotePlayerRenderState render;
    double target_time;
    int pos_snapped = 0; /* M9-C v7: an interpolator teleport snap / placement jump this frame (clears a one-shot latch) */

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
        {
            /* M9-B: an interpolator teleport-snap (or the first placement) moves the puppet far in a single frame;
             * keep its collider out of the OC pass for a couple of frames so it cannot deep-overlap-pop the player. */
            float jdx = render.pos.x - actor->world.position.x;
            float jdz = render.pos.z - actor->world.position.z;
            if ((jdx * jdx + jdz * jdz) > (PC_REMOTE_PLAYER_COLLIDE_SNAP_DIST * PC_REMOTE_PLAYER_COLLIDE_SNAP_DIST)) {
                self->collide_hold = PC_REMOTE_PLAYER_COLLIDE_SNAP_HOLD_FRAMES;
                pos_snapped = 1;
            }
        }
        actor->world.position = render.pos;
        actor->world.angle.y = render.angle;
        actor->shape_info.rotation.y = render.angle; /* mirrors how the local player's own
                                                       * main-state functions keep these two in
                                                       * sync, e.g. Player_actor_Movement_Walk() */

        self->cosmetic_speed = render.speed;
        self->cosmetic_move_state = render.move_state;
        self->cosmetic_item_kind = render.item_kind;
        self->cosmetic_action_index = render.action_index;
        self->cosmetic_action_counter = render.action_counter;
        self->cosmetic_action_valid = render.action_valid;
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
         * (AIRBORNE/TUMBLE/ITEM_USE/OTHER) falls back to WAIT1 here; since M9-C Phase 2 the state-row table
         * (pc_remote_player_row_pre) owns every action main index and overrides this fallback block while a row is
         * active, so this mapping only covers states WITHOUT a row (PCFB fallbacks and v6 senders). */
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

        /* M9-C Phase 1: held item. The EFFECTIVE kind comes from the same interpolation result as move_state
         * (self->cosmetic_item_kind, set above from render.item_kind). Deferred/unknown kinds behave exactly like
         * "no item": idle arms and no model, i.e. identical to the pre-M9-C puppet. */
        int raw_kind = (int)self->cosmetic_item_kind;
        int held_cls = pc_remote_player_held_class(raw_kind);
        int held_kind = (held_cls == PC_HELD_NONE || held_cls == PC_HELD_DEFERRED) ? mPlayer_ITEM_KIND_NONE : raw_kind;
        int held_moving = (desired_anim_idx == mPlayer_ANIM_WALK1 || desired_anim_idx == mPlayer_ANIM_RUN1 ||
                           desired_anim_idx == mPlayer_ANIM_DASH1);
        int desired_anim1_idx = desired_anim_idx; /* upper-body/item layer clip: same clip as layer 0 unless an item */
        int desired_part_table = mPlayer_PART_TABLE_NORMAL;

        if (held_kind >= 0) {
            /* Vanilla binds keyframe1 to the item pose clip and the part table to its row for every state that
             * calls Player_actor_SetupItem_Base1/3 (wait/walk/run/dash/turn-dash: SetupItem_Base0 ->
             * mPlib_Get_BasicPlayerAnimeIndex_fromItemKind / mPlib_Get_BasicPartTableIndex_fromAnimeIndex,
             * m_player_common.c_inc). This fallback block only runs while no state row is active, so every state this puppet maps to
             * WAIT1/WALK1/RUN1/DASH1/RUN_SLIP1 (incl. ITEM_USE/AIRBORNE/TUMBLE/OTHER -> WAIT1) shows the carried pose. */
            int a1 = mPlib_Get_BasicPlayerAnimeIndex_fromItemKind(held_kind);
            if (a1 >= 0 && mPlib_Get_Pointer_Animation(a1) != NULL) {
                int pt = mPlib_Get_BasicPartTableIndex_fromAnimeIndex(a1);
                desired_anim1_idx = a1;
                desired_part_table = (pt >= 0 && pt < mPlayer_PART_TABLE_NUM) ? pt : mPlayer_PART_TABLE_NORMAL;
            }
        }

        if (held_kind != self->visual.held_kind) {
            self->visual.held_kind = held_kind;
            self->visual.held_class = (held_kind >= 0) ? held_cls : PC_HELD_NONE;
            self->visual.item_ready = 0;
            if (held_cls == PC_HELD_NET || held_cls == PC_HELD_ROD) {
                pc_remote_player_item_skeleton_setup(&self->visual, held_kind, held_cls, held_moving);
            }
        }
        if (pc_remote_player_puppet_diag() &&
            (!self->visual.diag_valid || self->visual.diag_raw_kind != raw_kind)) {
            self->visual.diag_valid = 1;
            self->visual.diag_raw_kind = raw_kind;
            printf("[NET][PUPPET][DIAG] player %d held item kind=%d model=%s layer1_anim=%d part_table=%d\n",
                   (int)self->peer, raw_kind,
                   (held_cls == PC_HELD_DEFERRED) ? "deferred" : pc_remote_player_held_name(raw_kind, held_cls),
                   (held_kind >= 0) ? desired_anim1_idx : -1, desired_part_table);
        }

        /* M9-C Phase 2a: state rows (protocol v7). Entry-edge detection + latch bookkeeping; when a row is active it owns
         * the body clips / part table / tempo, and the fallback blocks below (move_state mapping + carried pose) are skipped. */
        double now_time = graph_dt_frame_time(game);
        int row_on;
        int play_state;
        int rebind_from_row;

        pc_remote_player_row_pre(self, slot, now_time, raw_kind, held_kind, desired_anim_idx, desired_anim1_idx,
                                 desired_part_table, pos_snapped);
        row_on = self->visual.row_active;
        rebind_from_row = self->visual.rebind_from_row;
        self->visual.rebind_from_row = 0;

        if (!row_on && desired_anim_idx != self->visual.current_anim_idx) {
            /* State changed -- (re)start both layers on the new clip at frame 0, the same call
             * the real player's own Player_actor_InitAnimation_Base1() makes on every state entry
             * (src/game/m_player_common.c_inc). The keyframe structs themselves were already
             * cKF_SkeletonInfo_R_ct()'d once, in pc_remote_player_visual_init(), and never need
             * that again -- only this call, which rebinds the animation pointer/frame/speed
             * without touching the joint/morph buffer wiring cKF_SkeletonInfo_R_ct() set up.
             * M9-C Phase 1: layer 1 gets the item pose clip when an item is carried (desired_anim1_idx ==
             * desired_anim_idx otherwise, i.e. exactly the pre-M9-C behaviour). */
            cKF_Animation_R_c* new_anim = mPlib_Get_Pointer_Animation(desired_anim_idx);
            cKF_Animation_R_c* new_anim1 = mPlib_Get_Pointer_Animation(desired_anim1_idx);
            if (pc_remote_player_puppet_diag()) { /* review R1-L3: was unconditional (one line per remote action end) */
                printf("[NET][REMOTE][DIAG] player %d: animation %d -> %d (move_state=%d)\n", (int)self->peer,
                       self->visual.current_anim_idx, desired_anim_idx, (int)self->cosmetic_move_state);
            }
            cKF_SkeletonInfo_R_init_standard_repeat_setframeandspeedandmorph(&self->visual.keyframe0, new_anim, NULL,
                                                                             0.0f, 1.0f, 0.0f);
            cKF_SkeletonInfo_R_init_standard_repeat_setframeandspeedandmorph(&self->visual.keyframe1, new_anim1, NULL,
                                                                             0.0f, 1.0f, 0.0f);
            self->visual.current_anim_idx = desired_anim_idx;
            self->visual.current_anim1_idx = desired_anim1_idx;

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
                if (!rebind_from_row) { /* review R1-L3: a row release rebind is not a skid entry (no late replay) */
                    sAdo_OngenTrgStart(0x4129, &actor->world.position);
                }
            }
        } else if (!row_on && desired_anim1_idx != self->visual.current_anim1_idx) {
            /* M9-C Phase 1: only the carried item changed (equip/unequip while the body clip keeps playing): rebind
             * just the upper-body layer, at frame 0 and the body layer's current tempo, leaving keyframe0 (the
             * walk/run cycle) untouched. */
            cKF_SkeletonInfo_R_init_standard_repeat_setframeandspeedandmorph(
                &self->visual.keyframe1, mPlib_Get_Pointer_Animation(desired_anim1_idx), NULL, 0.0f,
                self->visual.keyframe0.frame_control.speed, 0.0f);
            self->visual.current_anim1_idx = desired_anim1_idx;
        }

        if (!row_on && desired_part_table != self->visual.current_part_table) {
            /* Part table row for the item pose clip (mPlayer_PART_TABLE_NORMAL again once the item is gone); the
             * same mPlib_DMA_player_Part_Table() the visual bring-up uses, into this puppet's own buffer. */
            mPlib_DMA_player_Part_Table(self->visual.part_table, desired_part_table);
            self->visual.current_part_table = desired_part_table;
        }

        /* Stage 4B: retune playback tempo every frame regardless of whether the state just
         * changed -- exactly like the real player's Player_actor_CulcAnimation_Walk() does
         * (shared verbatim by Walk/Run/Dash, src/game/m_player_main_walk.c_inc): never
         * reinitializes the animation, just updates frame_control.speed so
         * cKF_SkeletonInfo_R_combine_play() below advances at the right tempo. WAIT1 and
         * RUN_SLIP1 are excluded: the real player never runs this formula for Wait, and
         * RUN_SLIP1 uses a fixed 0.5x speed set once at entry above (see the Stage 4B.1
         * investigation) rather than a continuously-recalculated movement-speed tempo. */
        if (!row_on && self->visual.current_anim_idx != mPlayer_ANIM_WAIT1 &&
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

        if (row_on) {
            const PCStateRow* arow = pc_remote_player_row_for(self->visual.row_idx);
            if (arow != NULL && arow->tempo == PC_TEMPO_READY_WALK_NET) {
                /* Player_actor_CulcAnimation_Ready_walk_net (m_player_main_ready_walk_net.c_inc): 0.252*sqrt(speed*over_norm/
                 * 1.8), floor 0.22; over_norm and the wall modifiers are not network-synced (1.0 / none). */
                float rs = (self->cosmetic_speed > 0.0f) ? self->cosmetic_speed : 0.0f;
                float sp = 0.252f * sqrtf(rs / 1.8f);
                if (sp < 0.22f) {
                    sp = 0.22f;
                }
                self->visual.keyframe0.frame_control.speed = sp;
                self->visual.keyframe1.frame_control.speed = sp;
            }
        }

        play_state = cKF_SkeletonInfo_R_combine_play(&self->visual.keyframe0, &self->visual.keyframe1,
                                                     self->visual.part_table);
        pc_remote_player_row_post(self, play_state, now_time);

        /* M9-C Phase 3: footsteps / effects keyed on this puppet's own clip timing and edges (after the clip advanced). */
        pc_remote_player_cosmetics(self, slot, game, now_time);

        /* M9-C Phase 1: puppet-owned net/rod skeleton instance, advanced once per puppet move like
         * Player_actor_Item_main_net_normal / _rod_normal (cKF_SkeletonInfo_R_play + the net carried angle). Phase 2a: the
         * clip is chosen by pc_remote_player_item_tick() (carried default, or the active state row's item clip). */
        if (self->visual.item_ready &&
            (self->visual.held_class == PC_HELD_NET || self->visual.held_class == PC_HELD_ROD)) {
            pc_remote_player_item_tick(self, held_moving);
            cKF_SkeletonInfo_R_play(&self->visual.item_keyframe);
            if (self->visual.held_class == PC_HELD_NET) {
                pc_remote_player_net_angle_update(self);
            }
        }
    }

    /* M9-B: player<->player collision (registers the puppet's pipe when every arming condition holds). */
    pc_remote_player_collide_update(self, slot, game);
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

    /* M9-C Phase 2b: HIDE (mPlayer_INDEX_HIDE 81) has vanilla draw type NONE (m_player.c, Player_actor_draw table), so the
     * peer's body and carried item are not drawn while that state is the active row. */
    if (self->visual.row_active) {
        const PCStateRow* hrow = pc_remote_player_row_for(self->visual.row_idx);
        if (hrow != NULL && (hrow->flags & PC_ROWF_HIDE_BODY)) {
            return;
        }
    }

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
    /* M9-C Phase 1: the puppet's OWN right-hand after-callback (arg == this PCRemotePlayerActor, never a
     * PLAYER_ACTOR) captures the world hand matrix during this very draw; the carried item is then drawn at it,
     * right after the body, in the same function (no one-frame lag, no state shared with the local player). */
    self->hand_mtx_valid = 0;
    cKF_Si3_draw_R_SV(game, &self->visual.keyframe0, mtx, NULL, pc_remote_player_draw_after, self);
    pc_remote_player_draw_item(self, game);
    pc_remote_player_draw_pickup_item(self, game); /* M9-C Phase 5: flying item of a remote PICKUP (event-paired) */
}

/* 1 only if slot->actor still belongs to the GAME_PLAY that is alive right now. game_dt() frees every
 * actor and sets gamePT to NULL, and graph_proc() does this both between scenes and on quit (before
 * main.c calls pc_net_game_shutdown()); pc_remote_player_poll()'s generation check has not run yet
 * for a replaced GAME_PLAY when an event handler gets here, so compare the GAME_PLAY identity too.
 * gamePT is also the title/player-select/logo GAME, which may reuse a freed GAME_PLAY's address, so
 * only a GAME running play_main (the only exec a constructed GAME_PLAY has) can own live actors. */
static int pc_remote_player_actor_is_live(const PCRemotePlayerSlot* slot) {
    if (slot->actor == NULL || gamePT == NULL || gamePT->exec != play_main) {
        return 0;
    }
    if (slot->scene_generation != s_scene_generation) {
        return 0;
    }
    return gamePT == s_last_seen_game && gamePT->frame_counter >= s_last_seen_frame_counter;
}

/* keep_scene == 0: genuine departure paths (on_disconnect, on_ready replacing a tracked slot, shutdown) --
 * the stored M9-A scene identity is wiped. keep_scene == 1: ONLY the client-side relay-liveness timeout
 * (a relayed peer merely stopped sending MOVE while its transport is still up, e.g. it is inside the NES
 * emulator or paused): the puppet presentation is torn down but the scene identity is PRESERVED, because
 * the peer's scene dedup will not re-announce an unchanged scene when it comes back. A real departure
 * still clears it later (host CLEARED notice, host-link loss, on_ready's own memset). */
static void pc_remote_player_destroy_slot(PCRemotePlayerSlot* slot, int keep_scene) {
    /* M9-A: a scene can be stored for a relay-discovered player that never became in_use, so clear it
     * BEFORE the in_use early-out: no stale interior presence may survive a disconnect. */
    if (!keep_scene) {
        memset(&slot->scene, 0, sizeof(slot->scene));
    }
    if (!slot->in_use) {
        return;
    }
    if (slot->collide_armed) { /* M9-B: the puppet (and so its collider) is going away */
        printf("[NET][COLLIDE] player %d: collider DISARMED reason=gone\n", (int)(slot - s_slots));
    }
    pc_puppet_fx_dump_stats(slot, (int)(slot - s_slots));
    memset(&slot->cos_stats, 0, sizeof(slot->cos_stats));
    memset(slot->pk_ev, 0, sizeof(slot->pk_ev)); /* M9-C Phase 5: queued pickup events die with the player */
    slot->collide_armed = 0;
    slot->collide_target_ok = 0;
    slot->latch_clear_req = 0; /* the actor (and its latch) is going away; a new actor starts with a clean visual */
    if (pc_remote_player_actor_is_live(slot)) {
        pc_puppet_sweat_kill((PCRemotePlayerActor*)slot->actor, "destroy"); /* review H1 */
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
        pc_remote_player_destroy_slot(slot, 0);
    }

    memset(&slot->scene, 0, sizeof(slot->scene)); /* M9-A: a new READY starts with no known scene */
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
    pc_remote_player_destroy_slot(slot, 0);
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
    snap->action_index = sample->action_index;
    snap->action_counter = sample->action_counter;
    snap->action_valid = sample->action_valid;

    slot->snapshot_head = (slot->snapshot_head + 1) % PC_REMOTE_PLAYER_SNAPSHOT_COUNT;
    if (slot->snapshot_count < PC_REMOTE_PLAYER_SNAPSHOT_COUNT) {
        slot->snapshot_count++;
    }
    slot->last_move_recv_local_frame = now;
}

int pc_remote_player_on_action(PCNetPlayerId player_id, int kind, int ut_x, int ut_z, uint16_t item, uint16_t seq) {
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(player_id);
    PCRemotePickupEvent* ev = NULL;
    double now;
    int i;

    if (slot == NULL || !slot->in_use || kind != 1 || ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255 || item == 0u ||
        item == 0xFFFFu) {
        pc_pk_diag_event((int)player_id, seq, ut_x, ut_z, item, "ignored", "invalid");
        return 0;
    }
    now = (gamePT != NULL) ? graph_dt_frame_time(gamePT) : 0.0;
    for (i = 0; i < PC_PUPPET_PK_RING; i++) {
        if (!slot->pk_ev[i].valid) {
            ev = &slot->pk_ev[i];
            break;
        }
    }
    if (ev == NULL) { /* ring full: the oldest one is evicted (it can no longer be paired in order anyway) */
        ev = &slot->pk_ev[0];
        for (i = 1; i < PC_PUPPET_PK_RING; i++) {
            if (slot->pk_ev[i].arrival < ev->arrival) {
                ev = &slot->pk_ev[i];
            }
        }
        slot->cos_stats.pk_dropped++;
        pc_pk_diag_event((int)player_id, ev->seq, ev->ux, ev->uz, ev->item, "dropped", "evicted");
    }
    ev->valid = 1;
    ev->seq = seq;
    ev->ux = (uint8_t)ut_x;
    ev->uz = (uint8_t)ut_z;
    ev->item = item;
    ev->arrival = now;
    slot->cos_stats.pk_events++;
    pc_pk_diag_event((int)player_id, seq, ut_x, ut_z, item, "pending", NULL);
    return 1;
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

/* M9-A: see pc_remote_player.h. Accepts only a scene whose seq is strictly newer than the stored one. */
int pc_remote_player_on_scene(PCNetPlayerId player_id, const PCNetPlayerScene* scene) {
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(player_id);

    if (slot == NULL || scene == NULL || !scene->valid) {
        return 0;
    }
    if (slot->scene.valid && scene->seq <= slot->scene.seq) {
        return 0;
    }
    slot->scene = *scene;
    slot->scene.valid = 1;
    slot->latch_clear_req = 1; /* M9-C v7: scene presence changed -> drop any one-shot latch at the next puppet move */
    return 1;
}

void pc_remote_player_clear_scene(PCNetPlayerId player_id) {
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(player_id);
    if (slot != NULL) {
        memset(&slot->scene, 0, sizeof(slot->scene));
        slot->latch_clear_req = 1; /* M9-C v7: see pc_remote_player_on_scene() */
    }
}

/* M9-B TEST-ONLY accessor: see pc_remote_player.h. */
int pc_remote_player_collide_test_target(float* out_x, float* out_y, float* out_z) {
    int i;

    for (i = 0; i < PC_REMOTE_PLAYER_SLOT_COUNT; i++) {
        PCRemotePlayerSlot* slot = &s_slots[i];
        if (slot->in_use && slot->collide_target_ok && pc_remote_player_actor_is_live(slot) &&
            (uint64_t)(uintptr_t)slot->actor < PC_LOWADDR_LIMIT) {
            *out_x = slot->actor->world.position.x;
            *out_y = slot->actor->world.position.y;
            *out_z = slot->actor->world.position.z;
            return 1;
        }
    }
    return 0;
}

int pc_remote_player_get_scene(PCNetPlayerId player_id, PCNetPlayerScene* out) {
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(player_id);
    if (slot == NULL || out == NULL || !slot->scene.valid) {
        return 0;
    }
    *out = slot->scene;
    return 1;
}

/* Stage 5A: see the doc comment in pc_remote_player.h. Reads the NEWEST entry of the same
 * snapshot ring pc_remote_player_interpolate() already consumes for rendering -- `snapshot_head`
 * is "the index the NEXT snapshot will be written to" (see its own field doc above), so the most
 * recent one is always one slot behind it. Deliberately does not interpolate/predict: this is an
 * authorization check, not a render position, so the exact last-confirmed sample is the right
 * thing to validate against, not a smoothed guess. */
int pc_remote_player_get_last_position(PCNetPlayerId player_id, float* out_x, float* out_y, float* out_z) {
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(player_id);
    int newest;

    if (slot == NULL || out_x == NULL || out_y == NULL || out_z == NULL || slot->snapshot_count == 0) {
        return 0;
    }

    newest = (slot->snapshot_head - 1 + PC_REMOTE_PLAYER_SNAPSHOT_COUNT) % PC_REMOTE_PLAYER_SNAPSHOT_COUNT;
    *out_x = slot->snapshots[newest].pos_x;
    *out_y = slot->snapshots[newest].pos_y;
    *out_z = slot->snapshots[newest].pos_z;
    return 1;
}

/* Stage 5B-2: see the doc comment in pc_remote_player.h. Exact sibling of
 * pc_remote_player_get_last_position() above -- same slot lookup, same "newest = one behind
 * snapshot_head" index, same no-interpolation reasoning (an authorization input, not a render
 * value) -- so a caller reading both back-to-back in one synchronous step always gets the position
 * and facing of the SAME accepted sample. */
int pc_remote_player_get_last_facing_angle(PCNetPlayerId player_id, int16_t* out_angle) {
    PCRemotePlayerSlot* slot = pc_remote_player_get_slot(player_id);
    int newest;

    if (slot == NULL || out_angle == NULL || slot->snapshot_count == 0) {
        return 0;
    }

    newest = (slot->snapshot_head - 1 + PC_REMOTE_PLAYER_SNAPSHOT_COUNT) % PC_REMOTE_PLAYER_SNAPSHOT_COUNT;
    *out_angle = slot->snapshots[newest].facing_angle;
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

    if (gamePT == NULL || gamePT->exec != play_main) {
        /* No running GAME_PLAY (still booting / at a menu): gamePT may be a much smaller GAME_SECOND etc., and
         * GET_PLAYER_ACTOR_NOW() / play->collision_check would read adjacent heap (boot-crash root cause). Nothing in this poll
         * is needed outside a GAME_PLAY: the scene generation is re-derived at the first play poll (gamePT / frame_counter change). */
        return;
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

    /* M9-B diagnostic (read-only): peak size of the shared OC collider table, sampled once per poll. Poll runs
     * between game frames, so this is the final count of the last completed frame. Logged only on a new peak
     * and only while a remote player is tracked, so single-player logs stay quiet. */
    for (i = 0; i < PC_REMOTE_PLAYER_SLOT_COUNT; i++) {
        if (s_slots[i].in_use) {
            int n = play->collision_check.collider_num;
            if (n > s_collide_peak_colliders) {
                s_collide_peak_colliders = n;
                printf("[NET][COLLIDE][DIAG] OC collider_num new peak=%d (table size %d) failed_setoc=%d\n", n,
                       Cl_COLLIDER_NUM, s_collide_failed_setoc);
            }
            break;
        }
    }
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
            if (slot->collide_armed) { /* M9-B: the old actor (and its collider) no longer exists */
                printf("[NET][COLLIDE] player %d: collider DISARMED reason=scene\n", i);
            }
            slot->collide_armed = 0;
            slot->collide_target_ok = 0;
            memset(slot->pk_ev, 0, sizeof(slot->pk_ev)); /* M9-C Phase 5: events of the old scene generation are meaningless */
            slot->actor = NULL;
            slot->pending_create = 1;
        }

        /* Liveness timeout: only for relay-discovered peers (see pc_remote_player_on_move()) --
         * a directly-tracked peer (host: a real client; client: the host) is already destroyed
         * promptly and unconditionally by the real PC_NET_EVENT_PEER_DISCONNECTED via
         * on_disconnect(), so this never needs to (and must not) second-guess that path. */
        if (slot->lazily_discovered && slot->has_sender_frame &&
            (now - slot->last_move_recv_local_frame) > PC_REMOTE_PLAYER_TIMEOUT_FRAMES) {
            printf("[NET][REMOTE] player %d timed out (no movement data) -- destroying remote-player actor "
                   "(scene identity %s)\n", i, slot->scene.valid ? "preserved" : "none");
            pc_remote_player_destroy_slot(slot, 1); /* keep the scene: see destroy_slot's doc */
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

        /* review round 2 (R5-LOW-1), premise corrected by the final review (R7): despite its name, setting
         * ACTOR_STATE_NO_MOVE_WHILE_CULLED makes Actor_info_call_actor (m_actor.c:502) keep running the actor's mv_proc while
         * it is culled, so the puppet's own move (row release, tool tick, sweat kill) already runs off screen and this block is
         * REDUNDANT (belt and braces). It is harmless: if this puppet's ASE2 sweat is up, the actor is culled (ACTOR_STATE_NO_CULL
         * clear: Actor_cull_check clears it when the actor is out of view) and the NEWEST snapshot is no longer STRUGGLE_PITFALL
         * (or carries no action info), the sweat is killed here a few interpolation frames earlier than the move would. Only
         * reads actor fields; pc_puppet_sweat_kill is flag-guarded and NULL-checks the effect clip. */
        if (slot->actor != NULL && (uint64_t)(uintptr_t)slot->actor < PC_LOWADDR_LIMIT &&
            ((PCRemotePlayerActor*)slot->actor)->cos.sweat_active &&
            (slot->actor->state_bitfield & ACTOR_STATE_NO_CULL) == 0) {
            const PCRemoteMoveSnapshot* newest = NULL;
            if (slot->snapshot_count > 0) {
                newest = &slot->snapshots[(slot->snapshot_head - 1 + PC_REMOTE_PLAYER_SNAPSHOT_COUNT) %
                                          PC_REMOTE_PLAYER_SNAPSHOT_COUNT];
            }
            if (newest == NULL || !newest->action_valid || (int)newest->action_index != (int)mPlayer_INDEX_STRUGGLE_PITFALL) {
                pc_puppet_sweat_kill((PCRemotePlayerActor*)slot->actor, "culled");
            }
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

        /* M9-B: the puppet's own collision pipe (ct_proc is none_proc2, so it is built here, where the actor is
         * created). ClObjPipe_dt only calls the trivial ClObj_dt/ClObjPipeAttr_dt (both just return 1), so no
         * destructor is needed. HEAVY: a normal-weight local player takes the entire push (collision_vec) and the
         * puppet none; an idle (also HEAVY) local player takes half. */
        ClObjPipe_ct(gamePT, &((PCRemotePlayerActor*)actor)->col_pipe);
        ClObjPipe_set5(gamePT, &((PCRemotePlayerActor*)actor)->col_pipe, actor, &s_remote_player_pipe_data);
        actor->status_data.weight = MASSTYPE_HEAVY;

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
        pc_remote_player_destroy_slot(&s_slots[i], 0);
    }
}
