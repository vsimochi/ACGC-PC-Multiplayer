/* pc_remote_player.h - Stage 2/3/4A: visible, movement-synchronized, real-model representation
 * of a remote player.
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
 * cKF_bs_r_boy_1/grl_1 resources, selected by the LOCAL player's gender), shown in a looping
 * WAIT1 idle pose using the LOCAL player's own appearance textures. This is composition, not a
 * PLAYER_ACTOR: none of Player_actor_ct, Player_actor_move, the per-state main functions, or the
 * CulcAnimation helpers ever run for a remote player, no
 * controller is read, move_state/item_kind are still tracked but not yet used to change the
 * animation or draw a held item (that is Stage 4B/4D).
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

#ifdef __cplusplus
extern "C" {
#endif

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

/* Stage 3: called by pc_net_game.c for every accepted movement sample -- both a directly-tracked
 * player (host: a specific client; client: the host) and, on a client, any OTHER network player
 * learned about purely through host relay (a client is never directly connected to another
 * client, so this is the only way it finds out about them). Creates that player's remote actor
 * lazily on first sight if it isn't already tracked -- there is no separate "identity handshake"
 * for a relay-discovered peer. Stale or duplicate samples (by sender_frame) are rejected
 * internally; see pc_remote_player.c. Safe to call for an out-of-range player_id (no-op). */
void pc_remote_player_on_move(PCNetPlayerId player_id, const PCNetMoveSample* sample);

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
