/* pc_net_game.h - Stage 1: in-game networking integration (role selection + identity handshake).
 *
 * This sits ON TOP of the Stage 0 transport (pc_net.h) and is the only layer that knows
 * anything about "the game" -- pc_net.c itself remains a generic, game-agnostic UDP transport
 * and is unmodified by Stage 1 (its own standalone test, pc/tools/net_spike/, still exercises
 * it directly and unchanged).
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
 * state, inventory, NPCs, time/weather, saves. This module does not create or touch any
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

#define PC_NETGAME_PROTOCOL_VERSION 1u

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
 * land_id are 0 if no save was loaded yet when the handshake ran. */
typedef struct PCNetGameIdentity {
    uint8_t  player_name[PC_NETGAME_NAME_LEN];
    uint8_t  land_name[PC_NETGAME_LAND_LEN];
    uint16_t player_id;
    uint16_t land_id;
    int      has_save; /* 0 if player_id/land_id/names are placeholder (no save loaded yet) */
} PCNetGameIdentity;

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

#ifdef __cplusplus
}
#endif
#endif /* PC_NET_GAME_H */
