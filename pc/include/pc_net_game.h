/* pc_net_game.h - Stage 1: in-game networking integration (role selection + identity handshake).
 *
 * This sits ON TOP of the Stage 0 transport (pc_net.h) and is the only layer that knows
 * anything about "the game" -- pc_net.c itself remains a generic, game-agnostic UDP transport
 * (it knows nothing about this module's messages). Stage 1 left it unmodified; the multiplayer
 * foundation phase later made PC_NET_RELIABLE truly reliable + ordered per peer and added
 * pc_net_reliable_backlog() (see pc_net.h); its standalone test, pc/tools/net_spike/, exercises
 * it directly.
 *
 * Scope (deliberately limited -- see the Stage 1 task notes):
 *   - Role selection (host / client / none) from command-line flags, no UI yet.
 *   - A minimal identity handshake once pc_net reports a peer is transport-connected, so each
 *     side learns *who* it's talking to (see PCNetGameIdentity below) before anything else is
 *     built on top of the connection.
 *   - An explicit per-link state machine (see PCNetGameLinkState) so "a UDP HELLO arrived" is
 *     never mistaken for "the handshake is complete."
 *
 * Explicitly NOT in scope here (later stages): remote player actors, movement sync, world
 * state, inventory, NPCs, time/weather, saves. (Later stages did add several of these; see below.) This module does not create or touch any
 * ACTOR, does not read pad/controller state, and does not call into rendering or audio.
 *
 * Safety: every public function here is safe to call whether or not networking was ever
 * started, and pc_net_game_poll() never blocks. A role that fails to start (bad port, bad
 * address) simply leaves the game in PC_NETGAME_ROLE_NONE / everyone-DISCONNECTED -- normal
 * single-player operation never depends on any of this succeeding.
 */
#ifndef PC_NET_GAME_H
#define PC_NET_GAME_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Protocol version 2 (World Protocol): town identity validation in the handshake, PLAYER_CONTEXT,
 * host->client world snapshots, FIELD_UPDATE v2 (grid/acre/tile + world_seq), WORLD_META, and
 * RESYNC_REQUEST. See pc_net_game.c's "WORLD PROTOCOL (v2)" block for the full wire spec. The
 * IDENTITY message keeps protocol_version at byte offset 4 and a 32-byte size in every version so
 * that a mismatched peer is always REJECTed (never silently ignored). */
#define PC_NETGAME_PROTOCOL_VERSION 2u

/* SHARED-WORLD INTERACTION POLICY (intentional): the TOWN field is the one shared, host-authoritative
 * world -- pickups and drops of ground items go through the two-phase protocol below. A PRIVATE HOUSE /
 * room inventory is local to its owner and is NOT networked (no room path talks to this module).
 *
 * Two-phase pickup/drop (reserve -> confirm -> commit; see pc_net_game.c's "Shared-world interactions"
 * block for the full wire spec incl. the client -> host INTERACT_CONFIRM message, type 17):
 *   1. client sends PICKUP_REQUEST / DROP_REQUEST;
 *   2. the host validates everything it can and, on success, RESERVES the tile and answers with
 *      accepted = 1, which is only PROVISIONAL -- the field has NOT changed (accepted = 0 is a final
 *      rejection);
 *   3. the client validates its OWN inventory (same player/save, drop: the slot still holds the claimed
 *      item, pickup: a free pocket exists), applies its inventory step and sends INTERACT_CONFIRM
 *      (COMMIT), or sends INTERACT_CONFIRM (ABORT, reason) and changes nothing;
 *   4. only on COMMIT does the host re-validate the tile and write the field (FIELD_UPDATE to every
 *      client incl. the requester). ABORT / a 20 s timeout / disconnect just release the reservation.
 * A tile reserved by any peer's pending interaction is unavailable to every other peer's request and to
 * the host player's own local pickup/drop (see pc_net_game_field_tile_reserved()). A peer may hold one
 * pending pickup and one pending drop at the same time (independent; a new request replaces only the
 * pending interaction of the same kind); the same peer cannot reserve one tile with both. */

/* Mirrors PLAYER_NAME_LEN / LAND_NAME_SIZE from include/m_personal_id.h by value (not by
 * including that decomp header here, to keep this header decomp-independent). pc_net_game.c
 * _Static_assert's that these actually match. */
#define PC_NETGAME_NAME_LEN 8
#define PC_NETGAME_LAND_LEN 8

typedef enum PCNetGameRole {
    PC_NETGAME_ROLE_NONE = 0, /* networking not started; game runs exactly as single-player always has */
    PC_NETGAME_ROLE_HOST,
    PC_NETGAME_ROLE_CLIENT,
} PCNetGameRole;

/* Per-link (client's-eye-view of its one connection to the host, or the host's-eye-view of
 * one particular connecting peer) handshake state. */
typedef enum PCNetGameLinkState {
    PC_NETGAME_LINK_DISCONNECTED = 0, /* no transport connection (yet, or not any more) */
    PC_NETGAME_LINK_CONNECTING,       /* client only: pc_net_client_connect() called, no transport ack yet */
    PC_NETGAME_LINK_HANDSHAKE,        /* transport-connected; our identity sent, waiting on the other side's */
    PC_NETGAME_LINK_READY,            /* identity exchanged and acknowledged both ways */
} PCNetGameLinkState;

/* A network-safe, fixed-layout snapshot of "who this is" -- deliberately NOT the raw decomp
 * Private_c/PersonalID_c struct (which is fine to live in RAM but was never designed to be
 * serialized to a different process; see pc_net_game.c for exactly which fields are read and
 * why). player_name/land_name are copied verbatim from PersonalID_c: they are the game's
 * internal fixed-width font/character-code bytes, NOT ASCII text -- treat them as an opaque
 * fixed-size blob for now (nothing in Stage 1 decodes or displays them as text). player_id/
 * land_id are 0 if no save was loaded yet when the handshake ran. Protocol v2: IDENTITY is only
 * ever sent (client) / answered (host) once the sender's own save is loaded, so a v2 handshake
 * always carries has_save == 1. */
typedef struct PCNetGameIdentity {
    uint8_t  player_name[PC_NETGAME_NAME_LEN];
    uint8_t  land_name[PC_NETGAME_LAND_LEN];
    uint16_t player_id;
    uint16_t land_id;
    int      has_save; /* 0 if player_id/land_id/names are placeholder (no save loaded yet) */
} PCNetGameIdentity;

/* Protocol v2: which TOWN a save holds -- the thing host and client must agree on before any shared
 * world state is exchanged. Built only from immutable save data (see pc_net_game.c's
 * pcnetgame_capture_town_identity()):
 *   land_name    Save land_info.name (8 opaque font-code bytes, not ASCII)
 *   land_id      Save land_info.id (mLd_MakeLandId(): 0x3000 | random 8 bits) -- vanilla's own
 *                town identity is exactly (name, id), see mLd_CheckCmpLand()
 *   terrain_hash 32-bit FNV-1a over the 30 town acres' Save combi_table entries (acre type +
 *                height, generated once at town creation by mRF_MakeRandomField and never written
 *                again for those rows). This is what determines the town's bg/collision layout, and
 *                it tells apart two towns that happen to share name+id (only 256 ids per name).
 * Saved/mutable values (copy_protect, which m_card re-randomizes on EVERY save, fg contents,
 * island combi row 8) are deliberately NOT part of it. */
typedef struct PCNetGameTownIdentity {
    uint8_t  land_name[PC_NETGAME_LAND_LEN];
    uint16_t land_id;
    uint32_t terrain_hash;
} PCNetGameTownIdentity;

/* Protocol v2: the small slice of a player's state host-authoritative interactions need from the
 * requester (sent client->host as PLAYER_CONTEXT after READY and whenever it changes). Today the
 * host's pickup/drop validation reads the IN_TOWN flag; future host-side resolvers should read the
 * rest through pc_net_game_get_player_context(). Deliberately NOT Private_c. */
#define PC_NETGAME_CTX_FLAG_IN_TOWN 0x01u /* sender's loaded scene is the outdoor town (pcfa_scene_is_town()) --
                                           * the host requires this for PICKUP/DROP requests, because
                                           * their ut_x/ut_z are only town coordinates when it is set */
typedef struct PCNetPlayerContext {
    uint8_t player_no;    /* Common player_no: resident slot 0..3 (mPr_FOREIGNER etc. never reach READY) */
    uint8_t destiny_type; /* Private_c destiny.type (mPr_DESTINY_*) -- fortune/luck */
    uint8_t flags;        /* PC_NETGAME_CTX_FLAG_* */
    int16_t money_power;  /* mPr_GetMoneyPower(): Common money_power + destiny adjustment, clamped */
    int16_t goods_power;  /* mPr_GetGoodsPower(): Common goods_power + destiny adjustment, clamped */
} PCNetPlayerContext;

/* Stage 3: movement synchronization.
 *
 * "Network player id" identifies a specific remote player for movement purposes -- deliberately
 * a DIFFERENT number space from PCNetPeerId (pc_net.h's transport-level connection-slot index,
 * meaningful only to whoever is directly connected to that peer). The host has a real PCNetPeerId
 * for each of its up to PC_NET_MAX_PEERS clients, and those ids are reused directly here. The
 * host itself has no PCNetPeerId at all (nobody is "connected to" the host from its own point of
 * view) but is still a movement participant, so it gets the one reserved id past the valid client
 * range: PC_NETGAME_HOST_PLAYER_ID. This lets a client (which only ever has ONE real PCNetPeerId --
 * its single link to the host) still tell apart "the host's own movement" from "another client's
 * movement, relayed by the host" once both arrive tagged with a PCNetPlayerId. */
typedef int32_t PCNetPlayerId;
#define PC_NETGAME_HOST_PLAYER_ID ((PCNetPlayerId)PC_NET_MAX_PEERS)

/* A small, PC-only, deliberately coarse classification of what the local player's real
 * (~121-value) now_main_index is currently doing -- see pc_net_game.c's classifier. Never an
 * attempt to reproduce the real per-state animation/action system remotely; used only as a
 * cosmetic cue on the Stage 2 placeholder marker. */
typedef enum PCMoveState {
    PC_MOVE_STATE_IDLE = 0,
    PC_MOVE_STATE_WALK,
    PC_MOVE_STATE_RUN,
    PC_MOVE_STATE_DASH,
    PC_MOVE_STATE_AIRBORNE,
    PC_MOVE_STATE_TUMBLE,
    PC_MOVE_STATE_ITEM_USE,
    PC_MOVE_STATE_OTHER,
    /* Stage 4B.1: mPlayer_INDEX_TURN_DASH (the real player's sprint sharp-turn/skid state) was
     * previously folded into PC_MOVE_STATE_DASH -- see pc_net_game.c's classifier. Split out into
     * its own value so a remote client can distinguish "still dashing" from "skidding to a stop"
     * and play the correct mPlayer_ANIM_RUN_SLIP1 animation + skid sound instead of continuing
     * DASH1. Appended at the end so existing values keep their numeric encoding; still fits the
     * wire's existing uint8_t move_state field -- no packet layout/size change. */
    PC_MOVE_STATE_TURN_DASH,
} PCMoveState;

/* A network-safe, already-decoded movement sample -- built from / unpacked into the wire-format
 * PCNetMoveMsg (see pc_net_game.c). Deliberately NOT the raw PLAYER_ACTOR: no pointers, no
 * skeleton/animation state, no function pointers, no main_data unions.
 *
 * IMPORTANT: sender_frame is the SENDER's own monotonically increasing per-send counter (see
 * PCNetMoveMsg's doc comment). It is only ever meaningful compared against another sample from
 * that SAME sender (ordering/duplicate/staleness), and must never be compared against this
 * process's own frame/time domain -- the two processes' counters do not share an origin. The
 * receiver builds its own local presentation timeline from *when it received* each sample (see
 * pc_remote_player.c), never from this field directly. */
typedef struct PCNetMoveSample {
    uint32_t sender_frame;
    float    pos_x, pos_y, pos_z;
    int16_t  facing_angle; /* native engine angle units (a signed 16-bit angle, matching
                              ACTOR.world.angle.y -- wraps at +/-32768, which is also exactly the
                              representation shortest-path angle interpolation wants) */
    float    speed;
    uint8_t  move_state;   /* PCMoveState */
    int8_t   item_kind;    /* mirrors PLAYER_ACTOR::item_kind; -1 = none */
} PCNetMoveSample;

/* Stage 4C-1: a network-safe, already-decoded snapshot of a player's visible appearance -- see
 * pc_net_game.c for the wire message this is built from/unpacked into. Deliberately NOT the raw
 * decomp mNW_original_design_c/Private_c (kept decomp-independent at this header level, matching
 * PCNetGameIdentity/PCNetMoveSample's own convention above) -- pc_remote_player.c is the only
 * consumer, and it already includes the real decomp headers directly where it needs them.
 *
 * Sent once when a peer becomes READY (see pc_net_game.c's pc_net_game_poll()/
 * pcnetgame_handle_host_data()), again immediately whenever pc_net_game_poll()'s per-frame
 * comparison detects the sender's own appearance has changed (Stage 4C-2), and periodically as a
 * loss-recovery safety net (PC_NETGAME_APPEARANCE_RESEND_PERIOD_60FPS_FRAMES, pc_net_game.c) --
 * every send is a complete, self-contained snapshot, never a partial/delta update, so a receiver
 * always treats the latest one as the current authoritative appearance for that owner regardless
 * of which of these three triggers produced it.
 *
 * design_record is an opaque, byte-exact copy of the decomp's mNW_original_design_c (verified POD,
 * no pointers -- see the Stage 4C investigation) and is only meaningful when is_custom_design is
 * set; pc_remote_player.c reinterprets it as a real mNW_original_design_c to reuse the existing
 * mNW_CopyOriginalTexture()/mNW_CopyOriginalPalette() decomp functions directly. */
#define PC_NETGAME_DESIGN_RECORD_SIZE 544 /* mirrors sizeof(mNW_original_design_c); pc_net_game.c
                                            * _Static_assert's this matches exactly */
typedef struct PCNetPlayerAppearance {
    uint8_t gender;             /* mirrors Private_c::gender (mPr_SEX_MALE/FEMALE) */
    uint8_t face;               /* mirrors Private_c::face (mPr_FACE_TYPE0..7) */
    uint8_t sunburn_rank;       /* mirrors Private_c::sunburn.rank (0-8) */
    uint8_t is_custom_design;   /* 1 if cloth_item is the "wearing one of my own designs" sentinel
                                  * (RSV_CLOTH) and design_record below is meaningful */
    uint16_t cloth_item;        /* mirrors Private_c::cloth.item (mActor_name_t) */
    uint8_t design_record[PC_NETGAME_DESIGN_RECORD_SIZE]; /* only meaningful when is_custom_design */
} PCNetPlayerAppearance;

/* --- lifecycle --- */

/* Starts hosting on `port`. Returns 1 on success, 0 on failure (logged; caller should just
 * continue running single-player). */
int pc_net_game_start_host(uint16_t port);

/* Starts connecting to host_ip:port. Returns 1 if the attempt was launched (not that it
 * succeeded yet -- poll pc_net_game_client_link_state()), 0 on immediate failure. */
int pc_net_game_start_client(const char* host_ip, uint16_t port);

/* Tears down networking entirely (transport + all handshake state) and returns to
 * PC_NETGAME_ROLE_NONE. Safe to call even if networking was never started. */
void pc_net_game_shutdown(void);

/* Call exactly once per frame from PC-only code (see pc/src/pc_vi.c). Never blocks. Drains
 * the transport, advances the handshake state machine, and logs on state transitions only
 * (never every frame). */
void pc_net_game_poll(void);

/* --- queries --- */

PCNetGameRole pc_net_game_role(void);

/* Client only: DISCONNECTED if not a client / not connecting. */
PCNetGameLinkState pc_net_game_client_link_state(void);

/* Host only: number of peers currently at PC_NETGAME_LINK_READY (handshake complete). 0 if
 * not hosting. */
int pc_net_game_host_ready_peer_count(void);

/* Client only: the host's identity, once PC_NETGAME_LINK_READY. Returns 1 and fills *out, or
 * 0 if not yet available. */
int pc_net_game_get_host_identity(PCNetGameIdentity* out);

/* Contract section 3: 1 iff this process is a network CLIENT whose link is READY -- i.e. shared
 * persistent world state (daily growth results, field contents) comes from the host and must not
 * be generated locally. 0 for single-player, host, or a client that is not (yet) READY. Note: a
 * client's link only reaches READY after its own save is loaded AND the host validated its town,
 * so code that runs at save-load time (before READY) sees 0 even on a client; use
 * pc_net_game_role() == PC_NETGAME_ROLE_CLIENT to know that a host will take over. */
int pc_net_game_world_is_host_authoritative(void);

/* Protocol v2: the latest PLAYER_CONTEXT for a network player, for future host-side resolvers.
 * Host: PC_NETGAME_HOST_PLAYER_ID -> the host's own live local context; a READY peer id -> the last
 * context that peer sent (0 if it has not sent one yet). Any role: returns 0 for anything else, and
 * a client may query only its own context via pc_net_game_get_local_player_context(). */
int pc_net_game_get_player_context(PCNetPlayerId player_id, PCNetPlayerContext* out);

/* This process's own live PLAYER_CONTEXT. 0 if no gameplay save is loaded. */
int pc_net_game_get_local_player_context(PCNetPlayerContext* out);

/* This process's own town identity (see PCNetGameTownIdentity). 0 if no gameplay save is loaded
 * (the value would be meaningless -- e.g. the title-demo town). */
int pc_net_game_get_local_town_identity(PCNetGameTownIdentity* out);

/* Client only: 1 once at least one complete world snapshot from the host has been applied on this
 * connection (reset on disconnect). Diagnostics/tests only -- gameplay does not wait on it. */
int pc_net_game_client_world_synced(void);

/* Stage 5A: called from the decomp pickup state (see m_player_main_pickup.c_inc) instead of
 * mutating the field/inventory locally -- this process is a network client, so the host (not the
 * local save) is the authority on the field tile at (ut_x, ut_z) and whatever it currently holds.
 * Sends a PICKUP_REQUEST; the actual inventory grant and field clear only happen later, through the
 * two-phase exchange above: the host's provisional PICKUP_RESULT(accepted=1), this process's own
 * validation + INTERACT_CONFIRM, and the host's commit + FIELD_UPDATE (see pc_net_game.c's
 * pcnetgame_handle_client_pickup_result()). If this client has NO free pocket slot nothing is sent
 * (vanilla leaves the item where it is when the pockets are full) and the item stays in the world;
 * the return value is unchanged (1).
 *
 * Returns 1 if this process is a connected, READY client -- meaning the caller must NOT perform
 * the normal local mutation, whether or not a request was actually queued this call (e.g. one was
 * already pending). Returns 0 if this process is not a client (single-player or host), in which
 * case the caller should proceed exactly as before, unmodified. Never blocks. Safe to call with
 * out-of-range ut_x/ut_z (rejected internally, same as a malformed network message would be).
 * INTENTIONAL POLICY (do not "fix" by adding a local fallback): a network CLIENT that is not yet
 * READY (connecting, handshaking, or disconnected) also gets 0, and nothing is sent -- a return of
 * 0 therefore does NOT mean "mutate locally". Callers must decide on pc_net_game_role() ==
 * PC_NETGAME_ROLE_CLIENT, never on this return value: a client never performs a shared-world
 * pickup itself, before READY or indoors. (The seam in m_player_main_pickup.c_inc does exactly that.)
 * Protocol v2: nothing is sent while this client is not in the town scene (indoors ut_x/ut_z are
 * room coordinates; room floors are not shared state) -- still returns 1. The host additionally
 * requires the requester's PLAYER_CONTEXT to say IN_TOWN, validates against its PERSISTENT field
 * (works while the host is indoors), and commits via pcfa_set_tile(). */
int pc_net_game_request_pickup(int ut_x, int ut_z);

/* Stage 5A.1: called from the decomp pickup state (see m_player_main_pickup.c_inc) immediately
 * after the HOST's OWN local pickup has already mutated the field tile through the existing,
 * unmodified single-player code -- never before that mutation, and never as a substitute for it.
 * Broadcasts a FIELD_UPDATE for (ut_x, ut_z) to every connected client so they converge on the
 * same now-empty tile, exactly as they already do for a client-initiated pickup. A no-op for
 * single-player and (defensively) for a client -- see pc_net_game.c's own doc on this function.
 * Never blocks. Safe to call with out-of-range ut_x/ut_z (rejected internally).
 * Protocol v2: redundant-but-harmless -- it only triggers the single commit path early for this
 * tile's acre (the write hook + per-poll dirty flush would broadcast the same change a moment later;
 * the shared shadow guarantees exactly one send). Ignored while the host is not in the town scene. */
void pc_net_game_notify_local_field_pickup(int ut_x, int ut_z);

/* Stage 5B-1: called from the decomp drop seam (see m_tag_ovl.c's mTG_field_put_proc) instead of
 * mutating the field/inventory locally -- this process is a network client, so the host (not the
 * local save) is the authority on whether (ut_x, ut_z) may receive this item. Sends a
 * DROP_REQUEST; the pocket slot is only cleared later, if/when the host's provisional DROP_RESULT
 * accepts it AND the slot still holds exactly `claimed_item` when it arrives (see pc_net_game.c's
 * pcnetgame_handle_client_drop_result()) -- never at send time. A pending request is cancelled if the
 * local player/save changes before the RESULT arrives.
 *
 * pocket_slot_idx/claimed_item are an explicit TRUST BOUNDARY: the host cannot verify either (it
 * holds no shadow/mirror of any remote player's inventory -- see the Stage 5B audit's
 * "Inventory/Authority Architecture"). Every OTHER aspect of the request (target tile emptiness,
 * terrain legality, requester reach, item classification, duplicate/retry) is independently
 * re-validated host-side; see pcnetgame_validate_and_resolve_drop().
 *
 * Returns 1 if this process is a connected, READY client -- meaning the caller must NOT perform the
 * normal local mutation, whether or not a request was actually queued this call (e.g. one was
 * already pending). Returns 0 if this process is not a client (single-player or host), in which case
 * the caller should proceed exactly as before, unmodified. Never blocks. Safe to call with
 * out-of-range pocket_slot_idx/ut_x/ut_z (rejected internally).
 * INTENTIONAL POLICY (do not "fix" by adding a local fallback): a network CLIENT that is not yet
 * READY, or is READY but not in the town scene, gets 0 and nothing is sent -- a return of 0 does
 * NOT mean "mutate locally". The m_tag_ovl.c client seam (gated on pc_net_game_role() ==
 * PC_NETGAME_ROLE_CLIENT) treats it as "can't place that" and shows vanilla's warning; no shared-
 * world drop is ever performed by a client before READY or indoors.
 * Protocol v2: a READY client that is not in the town scene gets 0 and nothing is sent (the
 * m_tag_ovl.c client seam then shows vanilla's "can't place that" warning). The host rejects any
 * drop while it is itself not in the town scene (vanilla's neighbor search needs its loaded
 * collision) or while the requester's PLAYER_CONTEXT does not say IN_TOWN. */
int pc_net_game_request_drop(int pocket_slot_idx, int claimed_item, int ut_x, int ut_z);

/* Host-local guard (used by the decomp host-local pickup and menu-drop seams): 1 iff the TOWN tile at
 * (ut_x, ut_z) is currently RESERVED by some client's pending network pickup/drop (a provisionally
 * accepted request still awaiting that client's INTERACT_CONFIRM, at most ~20 s). The host player's
 * own pickup/drop must then treat the tile as "cannot do that now" and not mutate it, so the
 * reservation is never invalidated underneath the client. Host only: returns 0 for single-player, for
 * a client, when the host itself is not in the town scene (its ut coordinates are then room
 * coordinates, not town ones), and for coordinates that are not a persistent town tile. Cheap (a scan
 * of at most 2 x PC_NET_MAX_PEERS records); never mutates anything. */
int pc_net_game_field_tile_reserved(int ut_x, int ut_z);

/* Stage 5B-3: called from the decomp drop-menu seam (see m_tag_ovl.c's mTG_field_put_proc) right
 * after the HOST's OWN local, vanilla Drop action successfully queues its delayed, animated field
 * mutation -- i.e. right after the existing mTG_common_throw_put_field() call returns success,
 * never before it, and never as a substitute for it. Arms a one-shot watch for (ut_x, ut_z): the
 * next drop that actually lands on that exact tile (see pc_net_game_notify_local_drop_landing(),
 * called from bg_item_common.c_inc's two landing sites) is announced to clients. Does not touch
 * the field itself and does not send anything by itself. A no-op for single-player and for a
 * client (a client's own drop landings are never host-authoritative -- see
 * pc_net_game_request_drop()). Safe to call with out-of-range ut_x/ut_z (rejected internally). */
void pc_net_game_arm_local_drop_landing(int ut_x, int ut_z);

/* Stage 5B-3: called from the decomp drop-landing seam (see bg_item_common.c_inc's
 * bIT_actor_drop_move_fly() and bIT_actor_drop_move_fly_destruct()) immediately AFTER the real
 * field write already happened -- never before, and never as a substitute for it. If a host-local
 * drop is currently armed (see pc_net_game_arm_local_drop_landing()) for exactly this tile,
 * consumes the watch and broadcasts the item that just landed; otherwise a no-op, since the
 * landing belongs to something else this stage doesn't track (money-rock, get-scoop, pickup-
 * exchange, putin-scoop, a client's own Stage 5B-1 drop, etc.). A no-op for single-player and for
 * a client.
 * Protocol v2: redundant-but-harmless early trigger of the single commit path (see
 * pc_net_game_notify_local_field_pickup()); `item` is no longer sent -- the persistent value that
 * actually landed is. The landings this watch never tracked are now broadcast by the dirty flush. */
void pc_net_game_notify_local_drop_landing(int ut_x, int ut_z, int item);

/* Stage 5B-1: does `item` classify as a plain, ordinary outdoor pocket item this stage supports
 * dropping over the network? Used by the decomp drop seam (m_tag_ovl.c) to give the SAME immediate,
 * synchronous "can't place that" feedback vanilla's own failure path already gives, rather than
 * silently sending a request the host is guaranteed to reject. See pc_net_game.c's
 * pcnetgame_is_droppable_item() for the exact, source-cited classification this wraps -- kept as a
 * plain int here (not mActor_name_t) to keep this header decomp-independent, matching every other
 * function in this header. */
int pc_net_game_is_droppable_item(int item);

#ifdef __cplusplus
}
#endif
#endif /* PC_NET_GAME_H */
