/* pc_remote_player.c - Stage 2: minimal visible representation of a connected remote player.
 *
 * See pc_remote_player.h for scope. Design notes (see the Stage 2 investigation for the full
 * reasoning -- summarized here so this file is self-contained):
 *
 *   - Actor part: ACTOR_PART_UNUSED, not ACTOR_PART_PLAYER. get_player_actor_withoutCheck()
 *     reads actor_info->list[ACTOR_PART_PLAYER].actor[0], and Actor_info_part_new() prepends new
 *     actors to a part's list -- putting a remote player in ACTOR_PART_PLAYER risks it being
 *     picked up as *the* local player by that lookup. ACTOR_PART_UNUSED is an existing,
 *     correctly-sized Actor_info list slot (part of the fixed-size actor_info->list[ACTOR_PART_NUM]
 *     array every GAME_PLAY already has) whose only other reader in the whole codebase is
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
 *   - Rendering: a small hand-emitted flat-colored pyramid marker using the *runtime* GBI macros
 *     (gSPVertex/gSP1Triangle/etc., which resolve real PC pointers via pc_gbi_pack_runtime_ptr()
 *     at call time) rather than a precompiled static display list, so nothing here needs C++
 *     compilation the way a static Gfx array's _GBI_STATIC_PTR initializer would under
 *     PC_LOW_ADDRESS_64. It reuses the real player's model/profile in no way -- that pipeline is
 *     wired to the local PLAYER_ACTOR's own animation/skeleton state -- and does not reuse
 *     ac_sample.c's Sample_Profile either (skeleton-animation-dependent, and has a documented
 *     pre-existing rendering bug); a self-contained marker was judged substantially safer for a
 *     prototype whose only job is to prove the pipeline end-to-end.
 *   - Position: NOT network-synchronized. Each remote actor's mv_proc recomputes its own position
 *     every frame as "the local player's current position plus a small fixed per-peer offset" --
 *     purely a local rendering anchor to prove connection -> identity -> actor creation ->
 *     rendering, exactly as scoped for Stage 2.
 */
#include "pc_remote_player.h"

#include "m_actor.h"
#include "m_play.h"
#include "m_player_lib.h"
#include "m_name_table.h"
#include "sys_matrix.h"
#include "m_rcp.h"
#include "libultra/libultra.h"
#include "pc_lowaddr.h" /* PC_LOWADDR_LIMIT -- see the guard in pc_remote_player_poll() */

#include <string.h>
#include <stdio.h>

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

/* A remote-player actor is just the generic ACTOR base plus which network player it stands in
 * for, and a little bit of purely cosmetic Stage 3 state. ACTOR must be the first member: the
 * whole actor pipeline (Actor_init_actor_class, Actor_info_part_new, generic mv_proc/dw_proc
 * dispatch, ...) only ever knows about ACTOR*. */
typedef struct PCRemotePlayerActor {
    ACTOR          actor_class;
    PCNetPlayerId  peer;
    float          cosmetic_speed;      /* last-interpolated speed; drives the visual spin below */
    uint8_t        cosmetic_move_state; /* last-interpolated PCMoveState; stored, not yet used to
                                          * change the marker's appearance beyond the spin */
    int8_t         cosmetic_item_kind;  /* last-interpolated item_kind; stored correctly, but
                                          * Stage 3 does not attempt to render an actual item */
    float          spin_angle;          /* free-running cosmetic Y-axis spin, radians-ish units
                                          * consumed only by Matrix_RotateY in the draw function */
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

typedef struct PCRemotePlayerColor {
    u8 r, g, b;
} PCRemotePlayerColor;

/* Purely cosmetic: lets Test G (two clients on one host) tell the two remote markers apart at a
 * glance. Also indexed like s_slots. */
static const PCRemotePlayerColor s_marker_colors[PC_REMOTE_PLAYER_SLOT_COUNT] = {
    { 0, 200, 255 }, { 255, 120, 0 },  { 0, 255, 120 }, { 255, 0, 160 },
    { 255, 220, 0 }, { 160, 0, 255 },  { 0, 255, 255 }, { 255, 255, 255 },
    { 200, 200, 200 },
};

/* Flat-colored pyramid: apex + 4-vertex square base, 4 side triangles, no bottom cap (never seen
 * from below in practice for a floating marker). Model-space units only -- world placement is
 * done every frame via Matrix_translate() in the draw function, matching the rest of the
 * codebase's convention (see e.g. ef_coin.c's eCoin_dw()). Plain data, no pointers, so this is a
 * perfectly ordinary static const initializer -- the C++-only static-GBI-pointer restriction
 * under PC_LOW_ADDRESS_64 applies to static *display list* (Gfx) arrays, not plain Vtx data. It is
 * referenced directly (not copied into a per-frame GRAPH_ALLOC scratch buffer): gSPVertex's
 * runtime macro packs whatever real PC pointer it is given, static or not. */
#define PC_REMOTE_PLAYER_VTX_COUNT 5
static const Vtx s_marker_verts[PC_REMOTE_PLAYER_VTX_COUNT] = {
    { .v = { { 0, 120, 0 }, 0, { 0, 0 }, { 255, 255, 255, 255 } } },    /* 0: apex */
    { .v = { { -40, 0, -40 }, 0, { 0, 0 }, { 255, 255, 255, 255 } } },  /* 1 */
    { .v = { { 40, 0, -40 }, 0, { 0, 0 }, { 255, 255, 255, 255 } } },   /* 2 */
    { .v = { { 40, 0, 40 }, 0, { 0, 0 }, { 255, 255, 255, 255 } } },    /* 3 */
    { .v = { { -40, 0, 40 }, 0, { 0, 0 }, { 255, 255, 255, 255 } } },   /* 4 */
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

typedef struct PCRemotePlayerSlot {
    int                in_use;             /* tracked at all (pending creation, or actor already live) */
    int                pending_create;     /* READY/discovered but actor creation hasn't succeeded yet */
    int                lazily_discovered;  /* 1 if learned about purely via a relayed movement sample
                                             * (no direct on_ready()/on_disconnect() for this one --
                                             * see pc_remote_player_on_move()); only such slots are
                                             * ever cleaned up by the liveness timeout in poll() */
    ACTOR*             actor;              /* NULL until pc_actor_make_from_profile() succeeds */
    PCNetGameIdentity  identity;           /* captured at READY, if any; not yet displayed anywhere */

    int                has_sender_frame;
    uint32_t           newest_sender_frame; /* highest PCNetMoveMsg.frame accepted from this sender */
    double             last_move_recv_local_frame; /* for the lazily_discovered timeout */

    PCRemoteMoveSnapshot snapshots[PC_REMOTE_PLAYER_SNAPSHOT_COUNT];
    int                  snapshot_count; /* 0..PC_REMOTE_PLAYER_SNAPSHOT_COUNT, valid entries */
    int                  snapshot_head;  /* index the NEXT snapshot will be written to */
} PCRemotePlayerSlot;

static PCRemotePlayerSlot s_slots[PC_REMOTE_PLAYER_SLOT_COUNT];

static ACTOR_PROFILE s_remote_player_profile;
static ACTOR_DLFTBL  s_remote_player_dlftbl;
static int           s_profile_ready = 0;

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

    if (!pc_remote_player_interpolate(slot, target_time, &render)) {
        return; /* no movement data yet -- hold at the creation-time spawn position */
    }

    actor->world.position = render.pos;
    actor->world.angle.y = render.angle;
    actor->shape_info.rotation.y = render.angle; /* mirrors how the local player's own main-state
                                                   * functions keep these two in sync, e.g.
                                                   * Player_actor_Movement_Walk() */

    self->cosmetic_speed = render.speed;
    self->cosmetic_move_state = render.move_state;
    self->cosmetic_item_kind = render.item_kind;

    /* Purely cosmetic: a faster-moving remote player spins its placeholder marker faster, so
     * `speed` visibly does something even though Stage 3 draws no real animation. Accumulated
     * here (the "movement" function) rather than in the draw function, and scaled by dt so it
     * doesn't depend on the render/frame rate either. */
    self->spin_angle += self->cosmetic_speed * 1500.0f * (f32)game->graph->dt_num_60fps_frames;
}

static void pc_remote_player_dw(ACTOR* actor, GAME* game) {
    GRAPH* graph = game->graph;
    PCRemotePlayerActor* self = (PCRemotePlayerActor*)actor;
    int color_idx = (self->peer >= 0 && self->peer < PC_REMOTE_PLAYER_SLOT_COUNT) ? self->peer : 0;
    const PCRemotePlayerColor* color = &s_marker_colors[color_idx];
    Gfx* gfx;

    /* Reset RDP/RSP mode (texture/z/light/fog/prim) to a known baseline before emitting our own
     * commands -- the same call every other actor's draw function makes first (see e.g.
     * ef_coin.c's eCoin_dw()), since whatever actor drew immediately before us may have left the
     * pipe in an arbitrary state (XLU cycle type, some tile bound, lighting on, ...). */
    _texture_z_light_fog_prim(graph);

    OPEN_DISP(graph);
    gfx = NOW_POLY_OPA_DISP;

    Matrix_translate(actor->world.position.x, actor->world.position.y, actor->world.position.z, MTX_LOAD);
    Matrix_RotateY((s16)self->spin_angle, MTX_MULT);
    gSPMatrix(gfx++, _Matrix_to_Mtx_new(graph), G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);

    gDPPipeSync(gfx++);
    gSPTexture(gfx++, 0, 0, 0, 0, G_OFF);
    /* Explicit, not inherited: no lighting (we supply a flat primitive color), no backface
     * culling (a small hand-built marker with unverified winding is safer visible from both
     * sides than possibly invisible), zbuffer on so it sorts correctly against everything else. */
    gSPLoadGeometryMode(gfx++, G_ZBUFFER);
    gDPSetCombineMode(gfx++, G_CC_PRIMITIVE, G_CC_PRIMITIVE);
    gDPSetPrimColor(gfx++, 0, 0, color->r, color->g, color->b, 255);

    gSPVertex(gfx++, s_marker_verts, PC_REMOTE_PLAYER_VTX_COUNT, 0);
    gSP1Triangle(gfx++, 0, 1, 2, 0);
    gSP1Triangle(gfx++, 0, 2, 3, 0);
    gSP1Triangle(gfx++, 0, 3, 4, 0);
    gSP1Triangle(gfx++, 0, 4, 1, 0);

    SET_POLY_OPA_DISP(gfx);
    CLOSE_DISP(graph);
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
    play = (GAME_PLAY*)gamePT;
    now = graph_dt_frame_time(gamePT);
    dump_now = graph_dt_period_elapsed(gamePT, &s_diag_dump_accum, PC_REMOTE_PLAYER_DIAG_DUMP_PERIOD_60FPS_FRAMES);

    pc_remote_player_init_profile();

    for (i = 0; i < PC_REMOTE_PLAYER_SLOT_COUNT; i++) {
        PCRemotePlayerSlot* slot = &s_slots[i];
        ACTOR* actor;
        const PCRemotePlayerOffset* ofs;
        f32 x, y, z;

        if (!slot->in_use) {
            continue;
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

        if (dump_now && slot->actor != NULL) {
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
        slot->actor = actor;
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
