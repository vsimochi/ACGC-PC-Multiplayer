/* pc_remote_player.h - Stage 2/3/4A/4B: visible, movement-synchronized, real-model, animated
 * representation of a remote player.
 *
 * Stage 2 scope: once pc_net_game's per-peer handshake reaches READY, each side creates a
 * lightweight placeholder ACTOR for the *other* side so the connection is visibly proven
 * end-to-end (connect -> identity -> actor creation -> rendering).
 *
 * Stage 3 scope: the placeholder actor's position/facing follow the real remote player via
 * delayed two-snapshot interpolation over a small per-peer ring buffer (see pc_remote_player.c).
 * No player-vs-player collision and no input/inventory/world-state sync of any kind -- only
 * position, facing, speed, a coarse move-state, and item_kind are ever received.
 *
 * Stage 4A scope: the placeholder is now the actual player skeleton/model (the shared
 * cKF_bs_r_boy_1/grl_1 resources, selected by the LOCAL player's gender), using the LOCAL player's
 * own appearance textures. This is composition, not a PLAYER_ACTOR: none of Player_actor_ct,
 * Player_actor_move, the per-state main functions, or the CulcAnimation helpers ever run for a
 * remote player, no controller is read.
 *
 * Stage 4B scope: the skeleton now animates through IDLE/WALK/RUN/DASH driven by the already-
 * synchronized move_state/speed fields -- no protocol change, no new network state. States
 * outside those four (AIRBORNE/TUMBLE/ITEM_USE/OTHER) and held-item rendering are still deferred.
 *
 * This module never sends or receives network traffic itself; pc_net_game.c is the only caller,
 * at the points where a peer's link state transitions to READY or away from it, and once per
 * accepted movement sample. It also never touches pad/controller state and never makes or writes
 * to the local player-controlled actor.
 */
#ifndef PC_REMOTE_PLAYER_H
#define PC_REMOTE_PLAYER_H

#include "pc_net.h"
#include "pc_net_game.h"
#include "pc_puppet_pool.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Puppet slots are allocated on demand for any usable wire id 0..254 (pc_puppet_pool.h); the id of a slot is its wire id, the host's own id 8 included (on a client it names the host).
 * PC_REMOTE_PLAYER_ID_LIMIT is the exclusive upper bound of a loop over player ids (0xFF is "nobody"). Readers never allocate. */
#define PC_REMOTE_PLAYER_ID_LIMIT 255
_Static_assert(PC_REMOTE_PLAYER_ID_LIMIT == PC_NETGAME_WIRE_ID_SPACE - 1, "puppet ids are every wire id but 0xFF");

/* Called by pc_net_game.c the instant a peer's handshake reaches READY. `player_id` is a
 * PCNetPlayerId (see pc_net_game.h) -- host: that client's real PCNetPeerId; client:
 * PC_NETGAME_HOST_PLAYER_ID, meaning "the host". `identity` is the peer's captured
 * PCNetGameIdentity (may be NULL); stored but not displayed anywhere yet. Actor creation may be
 * deferred to a later frame -- see pc_remote_player_poll(). Calling this again for a player_id
 * that is already tracked replaces its previous entry (destroying any actor it already had),
 * rather than leaking a second one. */
void pc_remote_player_on_ready(PCNetPlayerId player_id, const PCNetGameIdentity* identity);

/* Called by pc_net_game.c when a peer disconnects. Destroys that player's remote actor (if any)
 * through the normal deferred-destruction path (Actor_delete()), never a direct free. Safe to
 * call for a player_id with no tracked actor (e.g. it disconnected before ever reaching READY, or
 * was never tracked in the first place). */
void pc_remote_player_on_disconnect(PCNetPlayerId player_id);

/* Capacity phase 6: forget a player completely (puppet, slot, appearance, scene): used when the host says a relayed peer left. Same as a disconnect. */
void pc_remote_player_forget(PCNetPlayerId player_id);

/* Capacity phase 6: observable resource use (diagnostics, the dedicated status line). */
typedef struct PCRemotePlayerStats {
    int      slots_used, slots_peak, slot_alloc_failures; /* the slot pool (state memory) */
    int      tracked, actors_live, pending_create;        /* slots in use / with a live actor / still waiting for one */
    int      actors_blocked_now;                          /* puppets that could not get an actor this poll (actor headroom) */
    unsigned actors_blocked_total, actor_create_failed;   /* polls blocked by the headroom rule / pc_actor_make_from_profile() failures */
    int      actor_total_peak, actor_max;                 /* highest scene actor count seen / the scene actor pool size (mAc_MAX_ACTORS) */
    int      collide_armed;                               /* puppets whose collider is registered right now */
    int      collider_peak, collider_table;               /* highest shared OC table use seen / its size (Cl_COLLIDER_NUM) */
    int      failed_setoc;                                /* puppet colliders refused by a full table */
} PCRemotePlayerStats;
void pc_remote_player_get_stats(PCRemotePlayerStats* out);

/* Stage 3: called by pc_net_game.c for every accepted movement sample -- both a directly-tracked
 * player (host: a specific client; client: the host) and, on a client, any OTHER network player
 * learned about purely through host relay (a client is never directly connected to another
 * client, so this is the only way it finds out about them). Creates that player's remote actor
 * lazily on first sight if it isn't already tracked -- there is no separate "identity handshake"
 * for a relay-discovered peer. Stale or duplicate samples (by sender_frame) are rejected
 * internally; see pc_remote_player.c. Safe to call for an out-of-range player_id (no-op). */
void pc_remote_player_on_move(PCNetPlayerId player_id, const PCNetMoveSample* sample);

/* Stage 4C-1: called by pc_net_game.c once a peer's appearance data arrives (see
 * PC_NETGAME_MSG_APPEARANCE, pc_net_game.c) -- currently sent exactly once, alongside
 * READY/identity. Safe to call before or after on_ready()/on_move(): appearance, identity, and
 * movement are each tracked independently per slot, and this may arrive in any order relative to
 * the others (e.g. a relayed client's appearance can arrive before this process ever directly
 * handshakes with them). Resolves the remote player's own render-ready texture/palette buffers
 * immediately (skeleton selection, face, and clothing -- see pc_remote_player.c) -- never touches
 * Now_Private, gamePT, or any Object_Exchange_c bank. Calling this again for an already-known
 * player_id is safe (Stage 4C-2 will use this same entry point for live appearance changes; Stage
 * 4C-1 only ever calls it once, at READY). Safe to call for an out-of-range player_id (no-op). */
void pc_remote_player_on_appearance(PCNetPlayerId player_id, const PCNetPlayerAppearance* appearance);

/* Stage 4C-1 (backfill/resend fix): reads back this player's last-known appearance, exactly as
 * captured by the most recent pc_remote_player_on_appearance() call for it -- the same canonical
 * per-slot storage pc_remote_player.c already keeps, not a second cache. Used by pc_net_game.c to
 * resend a peer's already-known appearance to a newly-READY peer (backfill) and, periodically, to
 * every READY peer (loss mitigation) -- see pc_net_game.c's pcnetgame_handle_host_data() and
 * pc_net_game_poll(). Returns 1 and fills *out if the slot exists and holds a valid appearance
 * (i.e. pc_remote_player_on_appearance() has been called for it since its last disconnect), 0
 * otherwise (out is left untouched on failure). Never returns a pointer into internal storage and
 * never modifies the stored appearance -- purely a read. Safe to call for an out-of-range or
 * never-seen player_id (returns 0). */
int pc_remote_player_get_appearance(PCNetPlayerId player_id, PCNetPlayerAppearance* out);

/* Stage 5A: reads this player's most recently accepted movement sample position (the same data
 * pc_remote_player_on_move() already stores for interpolation/rendering) -- used by the host's
 * authoritative pickup handler (pc_net_game.c) to validate a remote client's claimed proximity to
 * a field tile without needing any new tracking of its own. Plain floats, not xyz_t, so this
 * header never needs a decomp include (matching pc_net_game.h's own PCNetMoveSample convention).
 * Returns 1 and fills *out_x/*out_y/*out_z if at least one movement sample has ever been accepted
 * for this player_id, 0 otherwise (outputs left untouched on failure -- e.g. a peer that reached
 * READY but hasn't sent its first movement sample yet). Safe to call for an out-of-range or
 * never-seen player_id (returns 0). */
int pc_remote_player_get_last_position(PCNetPlayerId player_id, float* out_x, float* out_y, float* out_z);

/* Stage 5B-2: reads this player's most recently accepted movement sample facing angle -- the same
 * PCNetMoveSample.facing_angle (native signed 16-bit engine angle units) pc_remote_player_on_move()
 * already stores for interpolation/rendering, from the SAME newest snapshot
 * pc_remote_player_get_last_position() reads. Used by the host's authoritative drop handler
 * (pc_net_game.c) to reproduce vanilla's facing-dependent neighbor-tile search for a remote client
 * without any new tracking of its own. Plain int16_t, not s16, so this header never needs a decomp
 * include (matching pc_remote_player_get_last_position()'s own convention). Returns 1 and fills
 * *out_angle if at least one movement sample has ever been accepted for this player_id, 0 otherwise
 * (output left untouched on failure). Safe to call for an out-of-range or never-seen player_id
 * (returns 0). */
int pc_remote_player_get_last_facing_angle(PCNetPlayerId player_id, int16_t* out_angle);

/* M9-A: per-player scene presence (see PCNetPlayerScene, pc_net_game.h), stored in the same per-slot state as
 * everything else about a remote player and therefore cleared by the same paths (on_ready, on_disconnect,
 * relay-liveness timeout, shutdown). on_scene() returns 1 and stores it iff `scene` is valid and its seq is
 * strictly newer than the stored one (or none is stored); 0 = stale/invalid, nothing changed. clear_scene()
 * forgets it (a host CLEARED notice). get_scene() returns 1 and fills *out only while a scene is known. All
 * are safe for an out-of-range player_id. Pure bookkeeping: no puppet is created, moved or filtered by it. */
int  pc_remote_player_on_scene(PCNetPlayerId player_id, const PCNetPlayerScene* scene);

/* M9-C Phase 5: a host-validated PLAYER_ACTION (cosmetic only). kind 1 = PICKUP: `item` was picked up from town tile
 * (ut_x, ut_z). The event is only QUEUED (small per-puppet ring) and presented from the puppet's own move/draw when its
 * pickup state shows up; it never touches the field, the inventory or any other state. Returns 1 if queued. */
int  pc_remote_player_on_action(PCNetPlayerId player_id, int kind, int ut_x, int ut_z, uint16_t item, uint16_t seq);
void pc_remote_player_clear_scene(PCNetPlayerId player_id);
int  pc_remote_player_get_scene(PCNetPlayerId player_id, PCNetPlayerScene* out);

/* --dedicated console (`players`): read-only state of this player's puppet. 0 = no puppet slot, 1 = tracked but the actor is not created yet
 * (pending), 2 = the actor is live in the current GAME_PLAY. Safe for an out-of-range player_id (0). */
int  pc_remote_player_puppet_state(PCNetPlayerId player_id);

/* Shared train arrival (pc_remote_arrival_logic.h): the local guest about to start the vanilla ride-off asks what the OTHER players are doing. 0 = not decidable yet (the caller keeps
 * its demo parked this frame and asks again); 1 = decided: *join_class = PCARR_JOIN_NONE (start the normal arrival) / RIDING / STOPPED, *train_x = the train x to start at for RIDING.
 * Client only (anything else is decided NONE at once). Never waits for another arrival to END. */
int  pc_remote_arrival_join_query(int* join_class, float* train_x);

/* M9-B TEST-ONLY (used by the off-by-default --collide-test-* hooks in pc_net_game.c): fills the current
 * (interpolated) world position of the first live puppet that has snapshots, a visual and a same-scene FIELD/IN_TOWN
 * presence (range and transient holds are ignored). Returns 0 when there is none. Read-only. */
int pc_remote_player_collide_test_target(float* out_x, float* out_y, float* out_z);

/* Call once per frame, unconditionally, regardless of game/menu/networking state (see
 * pc/src/pc_vi.c, right after pc_net_game_poll()). Retries any actor creation that was deferred
 * because the game/actor system wasn't in a valid state yet. Never blocks, never allocates
 * dynamically, and is a no-op whenever there is nothing pending. */
void pc_remote_player_poll(void);

/* Destroys every currently-tracked remote actor and clears all peer tracking state. Call this
 * from pc_net_game_shutdown(): a local, voluntary disconnect/quit never generates a
 * PC_NET_EVENT_PEER_DISCONNECTED for ourselves (that event is how the *other* side learns we
 * left), so without this explicit call our own remote-actor tracking would otherwise leak an
 * actor across a manual shutdown. Safe to call with nothing tracked. */
void pc_remote_player_shutdown(void);

#ifdef __cplusplus
}
#endif
#endif /* PC_REMOTE_PLAYER_H */
