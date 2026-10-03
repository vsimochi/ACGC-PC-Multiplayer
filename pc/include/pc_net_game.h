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

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Protocol version 2 (World Protocol): town identity validation in the handshake, PLAYER_CONTEXT,
 * host->client world snapshots, FIELD_UPDATE v2 (grid/acre/tile + world_seq), WORLD_META, and
 * RESYNC_REQUEST. See pc_net_game.c's "WORLD PROTOCOL (v2)" block for the full wire spec. The
 * IDENTITY message keeps protocol_version at byte offset 4 and a 32-byte size in every version so
 * that a mismatched peer is always REJECTed (never silently ignored).
 *
 * Protocol version 3 (World Ecology: dig/pitfall/shine family): PCNetGameFieldActionRequestMsg widened
 * from 8 to 12 bytes to add `hole_variant` (+ 3 bytes padding) -- see that struct's own doc. This is a
 * wire-incompatible change (an old 8-byte sender/reader would misread the new 12-byte layout), so the
 * version bumps to make a version-2 peer cleanly REJECT at IDENTITY instead of silently desyncing.
 *
 * Protocol version 4 (World Ecology T3: real bury-item logic): the BYTE LAYOUT of
 * PCNetGameBuryRequestMsg/PCNetGameBuryResultMsg is UNCHANGED from the T0 scaffolding (still 12 bytes
 * each) -- this bump exists purely so a v3 build (whose host dispatch stub always answered
 * BURY_RESULT(accepted=0) and would otherwise interoperate "successfully", just silently rejecting
 * every bury) can never be mistaken for a real v4 host/client that actually implements bury. A v3 peer
 * is now cleanly REJECTed at IDENTITY instead of connecting and masking the missing feature. See
 * PCNetGameBuryRequestMsg/PCNetGameBuryResultMsg's own doc for the field-semantics changes (hole_variant
 * becomes authoritative for one sub-case; the result's second uint16 _reserved0 becomes flags+reason).
 *
 * Protocol version 5 (M9-A, scene identity / player presence): adds ONE new reliable message,
 * PC_NETGAME_MSG_PLAYER_SCENE (id 44, 12 bytes). Existing layouts are unchanged; the bump exists so a
 * v4 peer (which would silently ignore the unknown message and leave scene presence permanently empty)
 * is cleanly REJECTed at IDENTITY instead of connecting without ever being able to announce a scene.
 *
 * Protocol version 6 (M9-C, host-authoritative client villager-talk hold): adds ONE new reliable
 * client -> host message, PC_NETGAME_MSG_NPC_TALK (id 45, 8 bytes): the begin/end edge of a client's local
 * talk lease on a villager. Existing layouts are unchanged; the bump exists so a v5 host (which would silently
 * ignore the unknown message and never hold the villager) and a v6 client can never interoperate unnoticed.
 *
 * Protocol version 7 (M9-C, remote player action presentation): the BYTE LAYOUT and size (28 bytes) of
 * PC_NETGAME_MSG_MOVE are UNCHANGED, but the formerly-always-zero int16 at offset 22 (`_reserved0`) now carries the
 * sender's vanilla `now_main_index` (0..120) in its LOW byte and a 4-bit state-ENTRY COUNTER in bits 8..11 (bits
 * 12..15 reserved, must be 0) -- see PCNetMoveMsg::action_state. A v6 peer would silently ignore the new field
 * (and a v7 receiver would see a v6 sender's 0 as "no action info"), so the actions would never be presented and
 * nobody would notice; the bump makes any v6 peer be cleanly REJECTed at IDENTITY instead (same convention as the
 * v4 bury bump above). v7 ALSO includes ONE new reliable host -> client message, PC_NETGAME_MSG_PLAYER_ACTION (id 46,
 * 10 bytes): a presentation-only "player X picked up item I at tile (ux,uz)" hint (kind 1 = PICKUP; the host emits it at
 * its own commit, relays it to every READY client except the originator, and a client never sends one) -- the same
 * unreleased v7, so no further bump.
 *
 * Protocol version 8 (D3, host-mirrored resident record): ONE new message family, ids 47..50 (RECORD_HELLO 24 B,
 * RECORD_BEGIN 28 B, RECORD_CHUNK 1012 B, RECORD_ACK 20 B, all reliable) carrying a resident's Private_c as canonical
 * big-endian GCI bytes (0x2440 B in 10 chunks). Existing layouts are unchanged; the bump makes a v7 peer be refused at
 * IDENTITY (PROTOCOL_MISMATCH) because it would never answer the host's record protocol. See pc_net_game.c's D3 block.
 *
 * VERSION POLICY (X1, host-transactional pickup/drop/bury commit): v8 is still UNRELEASED, so its message set is EXTENDED IN PLACE
 * with ids 51 TXN_COMMIT (client -> host, 72 bytes) and 52 TXN_RESULT (host -> client, 76 bytes), both reliable -- NO version
 * bump. The version check stays STRICT equality (a host and a client of different versions are refused at IDENTITY, never
 * downgraded to a mixed mode), so the v8 build that ships must contain the whole set. Ids 53/54 (TXN_QUERY / TXN_STATUS) are
 * reserved for X2: if v8 ships before X2 they arrive as an additive v9. See pc_net_game.c's X1 WIRE block. */
#define PC_NETGAME_PROTOCOL_VERSION 8u

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
    /* Protocol v7 (M9-C): decoded + range-checked PCNetMoveMsg::action_state. action_valid == 0 means "no action
     * information" (v6-style zero field, main index 0 = DMA/boot, or an out-of-range value that was ignored): the
     * receiver must then use the coarse move_state mapping only. Otherwise action_index is the sender's
     * mPlayer_INDEX_* (1..120) and action_counter its 4-bit entry counter; receivers compare the (index, counter)
     * PAIR by equality only (never ordering: the counter wraps every 16 entries). */
    uint8_t  action_index;
    uint8_t  action_counter;
    uint8_t  action_valid;
} PCNetMoveSample;

/* M9-A: which scene a network player is currently in -- IDENTITY of the scene only, never gameplay state.
 * The wire carries the decomp's RAW scene id (play->scene_id, an enum scene_table value < SCENE_NUM <= 255;
 * both peers run the same build -- guaranteed by PC_NETGAME_PROTOCOL_VERSION and the matching-town check --
 * so the enum is identical on both sides) plus an `owner` that is meaningful only for the shared house
 * scenes (SCENE_NPC_HOUSE: Common.house_owner_name = villager npc id, i.e. which villager's house;
 * the player-room scenes: Common.house_owner_name = which player house) and 0 for every other scene, plus
 * a per-sender `seq` so stale/reordered updates are rejected. `kind` is a LOCAL classification derived from
 * the raw id by pc_net_game_scene_kind() (never sent -- no second numbering system on the wire).
 * Only scene ids for which pc_net_game_scene_is_announceable() is true are ever sent/stored; title,
 * demo, player-select, test and tool scenes are never announced. */
#define PC_NETGAME_SCENE_FLAG_IN_TOWN 0x01u /* sender's latched SCENE_FG field grid is the home town (pcfa_scene_is_town():
                                             * every town-acre block pointer matches Save). The island is two blocks of the
                                             * SAME SCENE_FG grid, so this does NOT distinguish island from town (it is 1
                                             * for any normal SCENE_FG, island included); it only separates SCENE_FG from
                                             * a not-yet-built/other field grid (0). It is 0 for every non-FIELD kind. */
#define PC_NETGAME_SCENE_FLAG_CLEARED 0x80u /* host -> client only: "this player is gone/unknown": drop its scene */

typedef enum PCNetSceneKind {
    PC_NETSCENE_KIND_UNKNOWN = 0, /* not announceable (title/demo/player-select/test/tool/out of range) */
    PC_NETSCENE_KIND_FIELD,       /* SCENE_FG (town AND island: one shared grid; FLAG_IN_TOWN does not tell them apart) */
    PC_NETSCENE_KIND_SHOP,        /* Nook's Cranny/Nook 'n' Go/Nookway/Nookington's 1F+2F, Crazy Redd, Able Sisters */
    PC_NETSCENE_KIND_POST_OFFICE,
    PC_NETSCENE_KIND_POLICE,
    PC_NETSCENE_KIND_MUSEUM,      /* entrance + the four museum rooms */
    PC_NETSCENE_KIND_PLAYER_HOUSE, /* every player-room size/basement + the island cottage of the player */
    PC_NETSCENE_KIND_VILLAGER_HOUSE, /* SCENE_NPC_HOUSE (one scene for ALL villagers; see owner) + island NPC cottage */
    PC_NETSCENE_KIND_OTHER_INTERIOR, /* igloo, buggy, lighthouse, tent */
} PCNetSceneKind;

typedef struct PCNetPlayerScene {
    uint8_t  valid;    /* 1 once a scene was accepted/announced; 0 = nothing known (never announced / cleared) */
    uint8_t  scene_id; /* raw enum scene_table value */
    uint8_t  flags;    /* PC_NETGAME_SCENE_FLAG_* (CLEARED is never stored) */
    uint8_t  kind;     /* PCNetSceneKind, derived locally from scene_id */
    uint16_t owner;    /* house_owner_name for SCENE_NPC_HOUSE / player rooms, else 0 */
    uint32_t seq;      /* sender's location sequence number (newer-than check) */
} PCNetPlayerScene;

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

/* Protocol v7 (M9-C): read-only observation hook called by the vanilla player code (src/game/m_player.c,
 * Player_actor_change_main_index, TARGET_PC only) right after the LOCAL player actually entered a main index
 * (including re-entry of the SAME index, which never changes now_main_index by itself, e.g. repeated swings, digs
 * and pickups). It only bumps a 4-bit counter that pcnetgame_sample_local_move() later puts into the MOVE
 * `action_state` field; it never writes anything the game reads and costs one increment when no session is active. */
void pc_net_game_note_player_main_entry(int new_index);

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

/* M9-A: 1 iff the raw decomp scene id may be announced to peers (see PCNetPlayerScene). */
int pc_net_game_scene_is_announceable(int scene_id);
/* M9-A: local category for a raw scene id; PC_NETSCENE_KIND_UNKNOWN when not announceable. */
PCNetSceneKind pc_net_game_scene_kind(int scene_id);
/* M9-A: this process's own last announced scene. 0 if none announced yet (never in an announceable live
 * scene since start). */
int pc_net_game_get_local_scene(PCNetPlayerScene* out);
/* M9-A: the last accepted scene of another network player (host: a READY client id; client: the host id
 * or a relayed client id). Returns 1 and fills *out only if a scene is currently known; 0 otherwise
 * (never announced, disconnected, or cleared). Presence only: nothing consumes this yet (puppet
 * filtering is a later M9 stage). */
int pc_net_game_get_peer_scene(PCNetPlayerId player_id, PCNetPlayerScene* out);

/* M9-C: client -> host villager talk hold. The ready client's NPC code (ac_npc_move.c_inc) calls
 * pc_net_game_notify_local_npc_talk() exactly once per edge of its local talk lease on a regular villager
 * (begin = 1 rising, 0 falling/destroyed). Client only; 0 (nothing sent) for single-player/host/not READY or
 * when the reliable send fails. Never sends positions. */
int pc_net_game_notify_local_npc_talk(int slot, uint16_t npc_id, int begin);
/* M9-C keepalive: while a BEGIN is outstanding pc_net_game_poll() re-sends it (fresh seq, same message) every 10 s
 * (PC_NPC_TALKHOLD_REFRESH_MS, test only; 0 disables) iff this probe -- registered by the NPC code -- still reports
 * the local talk lease active for (slot, npc_id). A NULL probe or a 0 result stops the refreshes for that slot. */
typedef int (*PCNetGameNpcTalkLeaseProbe)(int slot, uint16_t npc_id);
void pc_net_game_set_npc_talk_lease_probe(PCNetGameNpcTalkLeaseProbe fn);
/* Monotonic wall-clock milliseconds (same source the net layer uses; never 0). Test hook timing only. */
uint32_t pc_net_game_now_ms(void);
/* M9-C: HOST only (0 on single-player/client): 1 iff at least one validated, unexpired client talk hold exists
 * for animal `slot` with this `npc_id`. Called every frame per villager by the host gate in ac_npc_move.c_inc. */
int pc_net_game_host_npc_talk_held(int slot, int npc_id);
/* M9-C: client: incremented on every client session reset (connect/disconnect/shutdown); the NPC edge code
 * drops its per-slot "begin sent" bits when it changes. */
int pc_net_game_npc_talk_session_epoch(void);

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
 * (works while the host is indoors), and commits via pcfa_set_tile().
 *
 * `item` is this CLIENT's own local knowledge of the target tile's item (the same mActor_name_t value,
 * kept as a plain int here -- not mActor_name_t -- to keep this header decomp-independent, matching
 * pc_net_game_is_droppable_item()'s own exact convention above; the caller's vanilla targeting code
 * already resolved this value -- see Player_actor_setup_main_Pickup(), m_player_main_pickup.c_inc),
 * used ONLY to decide whether to still send the request when pockets are full: review finding fix -- a
 * full-pockets client used to refuse to even SEND a pickup request for ANY item, including a money bag
 * the WALLET has room for (vanilla/PC_ENHANCEMENTS allows a money bag to bypass the pocket check
 * entirely via direct wallet credit). `item` is never trusted as the real outcome -- the host is still
 * the sole authority on what the tile actually holds, and the eventual PICKUP_RESULT's granted_item,
 * not this local guess, is what pcnetgame_handle_client_pickup_result()'s own Bug 3 wallet/pocket
 * pre-check re-verifies before ever sending INTERACT_CONFIRM. Worst case this local guess is wrong
 * (e.g. the field changed underneath the client): the request is simply sent and rejected/aborted with
 * no inventory change, exactly like any other stale/invalid pickup attempt already is. */
int pc_net_game_request_pickup(int ut_x, int ut_z, int item);

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

/* World Ecology P2 audit fix: called from m_field_make.c's mFM_PcFieldInitGrowth() whenever the
 * host's own daily-growth pass actually changed at least one town acre. Growth can change a tile's
 * tree species/stage (e.g. a sapling growing into a full tree needing a different hit count to fell),
 * which could otherwise let a stale, pre-growth s_host_tree_cut_count entry for that tile (see
 * pc_net_game.c) wrongly carry over into the new tree's state. Just clears that table -- the same
 * reset pcnetgame_reset_host_tree_cut_state() already performs on a town change or session reset.
 * A no-op for single-player and for a client (only the host ever populates or consults that table). */
void pc_net_game_notify_field_growth(void);

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

/* M9-D G4-2: catch-exchange menu (mSM_IV_OPEN_EXCHANGE) support for a network CLIENT; both functions are
 * no-ops for the host and single-player (they return 0 / record nothing).
 * pc_net_game_exchange_note_swap(): called by mHD_drop_item2() (m_hand_ovl.c) after every hand->pocket drop;
 * `swapped` != 0 means the hand and pockets[slot] exchanged items, new_item is what pockets[slot] now holds.
 * pc_net_game_exchange_request_drop(): called from mTG_exchange_proc() when the item left in the HAND
 * (hand_item/hand_cond: the true pocket item id and its condition, before the present->field conversion) is
 * about to be put on the ground. It puts hand_item back into the swap slot and sends it as an ordinary
 * pc_net_game_request_drop(); on the host's ACCEPT the drop-result handler clears the slot and then writes the
 * item that was in the slot (the caught item) into it. Returns 1 if a request is now in flight. Returns 0 if
 * nothing was sent (not town, one-in-flight, unknown swap slot, present/quest item, not droppable ...): then
 * the hand item stays in the slot (or is lost when there is no valid slot) and the replacement is lost.
 * A return of 0 NEVER means 'write the field locally'. */
void pc_net_game_exchange_note_swap(int slot, int swapped, int new_item);
int pc_net_game_exchange_request_drop(int hand_item, int hand_cond, int ut_x, int ut_z);

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

/* World Ecology T3: called from the decomp bury seams (mTG_bury_proc()/mTG_plant_proc()'s shovel
 * branch, m_tag_ovl.c) INSTEAD OF triggering the real local field mutation
 * (mPlib_request_main_putin_scoop_from_submenu() with the real item, which would eventually reach
 * player_drop_entry_proc() -- see Player_actor_setup_main_Putin_scoop(),
 * m_player_main_putin_scoop.c_inc) -- this process is a network client, so the host (not the local
 * save) is the authority on what burying `claimed_item` at (ut_x, ut_z) actually produces.
 * (mTG_exchange_proc()'s hole branch has a defensive TARGET_PC gate but is not reachable by a network
 * client in this build and does not call this function -- see its own comment.)
 *
 * POCKET-CLEAR TIMING CONTRACT (bugfix -- this deliberately does NOT match vanilla's own timing):
 * vanilla's own mTG_bury_proc()/mTG_plant_proc() clear the source pocket slot
 * (mPr_SetPossessionItem(..., EMPTY_NO, ...)) at MENU-ACTION time, before the bury animation even
 * starts, with no cancel path afterward -- for vanilla this is safe because nothing can fail once that
 * menu action closes. The NETWORKED path can genuinely fail for reasons that have nothing to do with
 * vanilla's own risk shape -- a lost network race, the pending request's own retry timeout expiring, an
 * owner-stamp mismatch, or an outright host reject -- so calling immediate-clear-at-menu-time "vanilla-
 * matching" here would be misleading: it would be a NEW, network-only item-loss mode, not a
 * continuation of any risk vanilla already accepted. To avoid that, the CALLER MUST NOT clear the
 * pocket slot based on this function's return value. Instead:
 *   - the caller (the m_tag_ovl.c bury seam) leaves the pocket slot untouched after calling this
 *     function, on every return value;
 *   - pcnetgame_handle_client_bury_result() clears the slot itself, later, only once the host's
 *     provisional accept has been re-verified (tile/item echo, owner stamp, and the slot STILL holding
 *     exactly the claimed item) and INTERACT_CONFIRM(COMMIT) has actually been queued -- the same
 *     two-phase reserve/confirm pattern pc_net_game_request_drop()/
 *     pcnetgame_handle_client_drop_result() already use, mirrored exactly, including the slot-still-
 *     holds-item re-check before the clear;
 *   - on a REJECT (or a request this call never even sent -- see the return-value doc below), the
 *     pocket is simply never cleared: the item stays exactly where it was, and only the shared WORLD
 *     tile is reconciled (see pcnetgame_handle_client_bury_result()'s own doc).
 *
 * pocket_slot_idx/claimed_item are a TRUST BOUNDARY exactly like PCNetGameDropRequestMsg's own (the
 * host holds no shadow of any remote player's inventory). hole_variant is the CALLER's own locally
 * computed mCoBG_GetHoleNumber(shovel_pos) result (0..24), or 0xFF for that function's -1 ("no valid
 * hole shape") sentinel -- see PCNetGameBuryRequestMsg's own doc for the one sub-case (a pitfall buried
 * into a HOLE_SHINE tile) where the host actually trusts this value; every other outcome discards it.
 *
 * Returns 1 if this process is a connected, READY client -- the caller must NOT perform the normal
 * local mutation, AND must NOT clear the pocket slot itself, regardless of whether a request was
 * actually queued this call (e.g. one was already pending, in which case nothing is sent and the
 * pocket is simply left alone with no further feedback, same posture as pc_net_game_request_drop()'s
 * own already-pending guard). Returns 0 for single-player or host, in which case the caller proceeds
 * exactly as before, unmodified (for the HOST role, see pc_net_game_host_local_bury() below). Never
 * blocks. Safe with out-of-range pocket_slot_idx/ut_x/ut_z (rejected internally, nothing sent, returns
 * 0 so the caller falls back to vanilla's local "can't do that" handling -- matching
 * pc_net_game_request_drop()'s own convention; the caller must NOT clear the pocket on this path
 * either). */
int pc_net_game_request_bury(int pocket_slot_idx, int claimed_item, int ut_x, int ut_z, int hole_variant);

/* X1b: 1 while THIS process is a network client with an unresolved pickup/drop/bury request or host-transactional pocket commit (the
 * TXN_COMMIT / TXN_RESULT round trip), else 0 (single-player and the host are never locked). The client-only lock that
 * mPlib_able_submenu_type1() (src/game/m_player_lib.c, TARGET_PC) ANDs in: while it is 1 the inventory / map / A-button boards, the
 * mailbox and the mscore stay shut, so the pocket slot a transaction refers to cannot be rearranged before the host's outcome is
 * known. It clears by itself on every matched TXN_RESULT, and on a session reset. */
int pc_net_game_client_pocket_locked(void);

/* Town services milestone 1 (protocol v8, unreleased): the client UI seams of the Blathers donation and the Booker lost-and-found claim. Both are
 * host-authoritative transactions (TXN_COMMIT kind 8 / 9): the vanilla dialogue state machines call _begin_* once, then poll pc_net_game_ts_poll()
 * every frame in a neutral state until it stops answering PENDING. The pocket is changed ONLY by the host's APPLIED post-image; REJECTED changes
 * nothing. begin: 1 = started, 0 = refused for good (not a READY client / bad arguments: take the give-back / refusal path), -1 = busy, ask again on
 * the next frame. Only ever called with pc_net_game_role() == PC_NETGAME_ROLE_CLIENT. */
#define PC_NETGAME_TS_OP_PENDING  0
#define PC_NETGAME_TS_OP_APPLIED  1
#define PC_NETGAME_TS_OP_REJECTED 2
int pc_net_game_ts_begin_museum_donate(int pocket_slot, int item);
int pc_net_game_ts_begin_police_claim(int pocket_slot, int police_idx, int item);
int pc_net_game_ts_poll(void);

/* Town services milestone 2 (NOOK'S SHOP): the client seams of a purchase / a sale. SHOP_BUY / SHOP_SELL are host transactions (TXN_COMMIT kind 10 / 11)
 * on the same machinery; the HOST computes the price / the sale value, validates the stock slot, pays, marks the stock sold and credits sales_sum. Only
 * ever called with pc_net_game_role() == PC_NETGAME_ROLE_CLIENT. begin return values as above.
 *   pc_net_game_shop_buy_stock_code(item): the stock code a purchase of `item` must carry against the MIRRORED shop (0..38 = Save_t.shop.items[] index,
 *                                          0xFE rare item, 0xFD counted candy / grab bag, 0xFF stationery), -1 = refuse locally (sold out in the mirror,
 *                                          bargain / raffle day, paint, not a shop item).
 *   pc_net_game_ts_begin_shop_buy(slot, item, code, price): a free pocket slot, the item, its stock code and the price the player was shown.
 *   pc_net_game_ts_begin_shop_sell(mask, slot, item): bit i of `mask` = pocket slot i is sold (bits 0..14); `slot` = one of them, `item` = its item.
 *   pc_net_game_ts_last_reject_reason(): the host's PC_NETGAME_TS_REJECT_* reason of the last REJECTED result (0 = a local refusal / none). */
#define PC_NETGAME_TS_REJECT_NOT_AVAILABLE   16
#define PC_NETGAME_TS_REJECT_NO_FUNDS        19
#define PC_NETGAME_TS_REJECT_NOT_SELLABLE    20
#define PC_NETGAME_TS_REJECT_PRICE_MISMATCH  21
#define PC_NETGAME_TS_REJECT_NO_ROOM         22
int pc_net_game_shop_buy_stock_code(int item);
int pc_net_game_ts_begin_shop_buy(int pocket_slot, int item, int stock_code, int price);
int pc_net_game_ts_begin_shop_sell(int slot_mask, int primary_slot, int item);
int pc_net_game_ts_last_reject_reason(void);

/* Mail milestone 1 (protocol v8, unreleased): the client seam of a LETTER TO A PLAYER (a resident's house). The vanilla post-girl dialogue would run
 * mPO_receipt_proc() on the client's LOCAL (dead) post office and lose the letter and its gift; a client instead asks the HOST (TXN_COMMIT kind 12
 * MAIL_SEND: the host reads the letter from its own mirror of the resident record, so the record must be CLEAN first). The seam calls _begin_send once,
 * then pc_net_game_mail_poll() every frame in a neutral state until it stops answering PC_NETGAME_TS_OP_PENDING (the same codes as the town services).
 * begin: 1 = started, 0 = refused for good (not a READY + SYNCED client / not a player-addressed send-font letter in that slot: take the refusal path),
 * -1 = busy, ask again next frame. The letter stays in its mail[] slot until the host's APPLIED removes it; every refusal leaves it there.
 * pc_net_game_mail_last_reason() = the host's PC_NETGAME_MAIL_REJECT_* of the last REJECTED result consumed by _poll (0 = a local refusal / any other
 * reason). Only ever called with pc_net_game_role() == PC_NETGAME_ROLE_CLIENT. */
#define PC_NETGAME_MAIL_REJECT_NO_SUCH_ADDRESS 23
#define PC_NETGAME_MAIL_REJECT_MAILBOX_FULL    24
#define PC_NETGAME_MAIL_REJECT_PO_FULL         25
int pc_net_game_mail_begin_send(int mail_slot);
int pc_net_game_mail_poll(void);
int pc_net_game_mail_last_reason(void);

/* Mail milestone 2 (protocol v8, unreleased): the owning client's house mailbox is the HOST's. The host sends every slot of that mailbox as MAILBOX_LETTER
 * (56) at READY and whenever a slot changes; the client keeps a verified SHADOW in its local homes[].mailbox. The mailbox actor / overlay open for a
 * network client ONLY while pc_net_game_client_mailbox_usable() == 1 (all 10 slots received this session); solo and the host never use it. Taking a letter
 * is a host transaction (TXN_COMMIT kind 13 MAIL_TAKE): the overlay's transfer code calls pc_net_game_mail_take_step(mailbox slot, &mail slot) every
 * transfer tick. PENDING = wait, change nothing; APPLIED = the letter is now in Now_Private->mail[*mail slot] and the local mailbox slot is clear (the
 * overlay does only its animation / sound); REJECTED = nothing moved, stop transferring (pc_net_game_mail_take_last_reason() = the host's
 * PC_NETGAME_TXN_REASON_*, 0 = a local refusal). Throwing letters away from the mailbox stays refused for a client (the overlay clears the marks). */
#define PC_NETGAME_MAIL_TAKE_REJECT_NO_SUCH_LETTER 26
#define PC_NETGAME_MAIL_TAKE_REJECT_MAIL_CHANGED   27
int pc_net_game_client_mailbox_usable(void);
int pc_net_game_mail_take_step(int mbox_idx, int* dst_out);
int pc_net_game_mail_take_last_reason(void);
/* H1: 1 when the HOST's own purchase of `item` may proceed (always 1 unless this is a networked HOST whose stock no longer holds the item: a client may have
 * bought it meanwhile). Special-day stock (bargain / raffle) and solo / client roles answer 1 (vanilla behaviour). */
int pc_net_game_host_shop_can_sell(int item);

/* World Ecology T3: HOST-LOCAL bury -- mirrors pc_net_game_host_local_money_rock_hit()'s/
 * pc_net_game_host_local_tree_shake()'s own precedent exactly: the host's own local bury action routes
 * through the SAME validate+commit logic a remote peer's BURY_REQUEST/INTERACT_CONFIRM pair uses,
 * skipping only the reach/IN_TOWN checks (the host's own real local targeting already guarantees
 * those), and runs synchronously (no reservation/CONFIRM round trip needed for a local, single-threaded
 * call).
 *
 * IMPORTANT (bugfix): the shared validation this routes through DOES still check whether the target
 * tile is currently reserved by a peer's own in-flight pickup/drop/bury (pcnetgame_host_tile_reserved_by()),
 * even for this host-local call, so a 0 return here can mean EITHER "not a legal bury" (not a hole,
 * already deposited, etc.) OR "reserved by a peer" -- the two are indistinguishable from this return
 * value alone. The CALLER MUST re-check pc_net_game_field_tile_reserved() (or, in m_tag_ovl.c, the
 * equivalent mTG_host_put_tile_free() helper) before falling back to vanilla's own local mutation on a
 * 0 return: falling back unconditionally would let the host's own local bury land on top of / overwrite
 * a peer's in-flight buried item if that peer's commit completes first, since vanilla's own local
 * mutation path has no idea the tile is spoken for. On success, clears the HOST's OWN pocket slot
 * `pocket_slot_idx` itself (mirroring what the
 * decomp seam would otherwise have done). No-op for single-player or a client, and for any request that
 * fails the host's own validation (in which case the caller must fall back to vanilla's local mutation,
 * exactly like every other host_local_* wrapper's own precedent -- see the call site in m_tag_ovl.c).
 * Never blocks. Safe with out-of-range pocket_slot_idx/ut_x/ut_z/hole_variant (rejected internally). */
int pc_net_game_host_local_bury(int pocket_slot_idx, int ut_x, int ut_z, int claimed_item, int hole_variant);

/* Bug 3 fix: exposes the exact "is this tile still a legal bury target" half of
 * pcnetgame_validate_and_resolve_bury()'s own check (raw tile value is HOLE00..24 or HOLE_SHINE) so a
 * caller of pc_net_game_host_local_bury() can distinguish, on a 0 return that is NOT explained by
 * mTG_host_put_tile_free()/pc_net_game_field_tile_reserved() (i.e. the tile is not reserved either), a
 * genuine "not a valid hole anymore" outcome (a racing peer's BURY already committed into it, or it
 * changed to something else between menu-open and commit) from every other host-local rejection reason
 * (bad pocket slot, deposit already on, stale claimed item, etc. -- all of which ALSO leave this TRUE,
 * since none of them change the tile itself). The caller must treat FALSE here as "do not fall back to
 * vanilla's local mutation" (show the ordinary warning instead) exactly like a reserved tile already
 * does, to avoid clearing the pocket with nothing actually placed. Returns TRUE (safe to proceed) for
 * single-player/a client, for an unresolvable ut_x/ut_z, and for any other read failure -- this function
 * only ever turns a proceed into a refusal, never the other way, so it cannot introduce a new way to
 * lose an item. */
int pc_net_game_host_bury_tile_is_valid(int ut_x, int ut_z);

/* World Ecology milestone (Stage 1, Item 1): called from the decomp scoop dispatch (see
 * Player_actor_CheckAndRequest_main_scoop_all(), m_player_common.c_inc) INSTEAD of
 * Player_actor_request_main_get_scoop_all() when scoop_request_index == mPlayer_INDEX_GET_SCOOP --
 * i.e. BEFORE the local dig ever enters the mPlayer_INDEX_GET_SCOOP state at all. This is the
 * audited-safe interception point: entering that state locally first (even just its setup callback)
 * risks Player_actor_settle_main_Get_scoop()'s own "pocket was full" fallback
 * (bg_item_clip->drop_entry_v1_proc) writing a ghost item onto the field if the state is later
 * blocked/aborted -- skipping the state transition entirely for a host-authoritative client avoids
 * that class of bug outright, at the cost of no local dig ANIMATION playing for this specific
 * interaction (the tile still visibly changes once the host's ordinary FIELD_UPDATE arrives, and the
 * item is granted straight into a pocket slot the same way pc_net_game_request_pickup() already
 * does -- see pcnetgame_handle_client_field_action_result(), pc_net_game.c). This function itself
 * checks for a free pocket slot before sending anything, exactly like
 * pc_net_game_request_pickup()'s own plain EMPTY_NO check -- NOT the ticket/paper-stacking-aware
 * mPlib_Get_space_putin_item_forTICKET() vanilla's local setup callback would have used, because the
 * real granted item is not known client-side until the host resolves it. If no slot is free, nothing
 * is sent (matches vanilla: the dig simply does not happen).
 * Returns 1 if this process is a connected, READY client (the caller must NOT enter
 * mPlayer_INDEX_GET_SCOOP locally, whether or not a request was actually queued this call). Returns
 * 0 for single-player or host, in which case the caller proceeds exactly as before, unmodified.
 * Never blocks. Safe with out-of-range ut_x/ut_z or a wpos that isn't a town tile (rejected
 * internally). The host never rolls RNG for the granted item -- it is read directly from its own
 * authoritative pcfa_get_tile() at the target tile, exactly what vanilla's own mPlib_Check_scoop_after()
 * would have read locally. */
/* X3 NOTE (applies to every request below that carries a grant: DIG_BURIED, the _with_grant variants, catch_fish / catch_bug): the grant is a
 * HOST-TRANSACTIONAL transaction, not a local mutation on RESULT. The request carries the 64-byte PCNetGameTxnTag (pre-image of the pockets /
 * conds / wallet, the D3 base, nonce + seq) and is resent byte-identically until the host's TXN_RESULT arrives; the host validates, mutates the
 * world and writes its resident mirror in ONE call and answers TXN_RESULT (kinds DIG_BURIED / DIG_HOLE / DIG_SHINE / CATCH) immediately before the
 * legacy RESULT. This client changes its pockets, sets its collection bit and records the catch outcome ONLY on TXN_RESULT(APPLIED) (the legacy
 * RESULT is informational). One pocket transaction at a time: a request made while another is unresolved is refused (a catch returns 0 = the seam's
 * local denial). The wording about mPr_SetFreePossessionItem / "on accept" in the older paragraphs below describes the pre-X3 behaviour. */
int pc_net_game_request_dig_buried(int ut_x, int ut_z);
int pc_net_game_request_dig_buried(int ut_x, int ut_z);

/* World Ecology milestone (Stage 1, Item 2): called from the decomp money-rock hit seam (see
 * Player_actor_Search_STONE_TC(), m_player_common.c_inc) INSTEAD of calling
 * bg_item_clip->ten_coin_entry_ex_proc() -- gating the WHOLE entry point, not suppressing only part
 * of bIT_actor_ten_coin_entryR()'s own field writes (see this function's own doc comment,
 * bg_item_common.c_inc, for why a partial gate would strand an ambiguous RSV_NO tile needing its own
 * settle timer). The host maintains its own small per-tile "money rock hit window" table (hit_count +
 * expiry, deterministic from hit_count/destiny_type/money_power -- no RNG, see
 * pcnetgame_host_check_field_action_money_rock() in pc_net_game.c) and commits every field change
 * (MONEY_ROCK_x -> MONEY_FLOWER_SEED on the first hit of a window, the dropped money-bag item, and
 * the eventual MONEY_FLOWER_SEED/MONEY_ROCK_x -> ROCK_x revert on expiry) via pcfa_set_tile(), which
 * already broadcasts to every client via the existing FIELD_UPDATE path -- no new broadcast message.
 * Deliberate Stage 1 simplification: this client does not get its own local "wobble" visual for this
 * hit while host-authoritative (unlike pickup, this interaction has no safe way to run any part of
 * bIT_actor_ten_coin_entryR() locally without also running its field writes) -- the tile update
 * arrives moments later via the ordinary FIELD_UPDATE broadcast instead. Returns 1 for a connected,
 * READY client (caller must not call ten_coin_entry_ex_proc() locally); 0 for single-player or host
 * (caller proceeds unmodified -- for the HOST role, see pc_net_game_host_local_money_rock_hit() below,
 * which the host's own local hit now routes through instead of vanilla's ten_coin_entry_ex_proc()).
 * Never blocks. Safe with out-of-range ut_x/ut_z (rejected internally). The dropped money-bag item
 * this creates is an entirely ORDINARY field item (Bug 4 fix) -- pickupable by any client through the
 * completely ordinary pickup path, with no special per-tile allow-list needed. */
int pc_net_game_request_money_rock_hit(int ut_x, int ut_z);

/* Bug 1 fix: called from the SAME decomp money-rock hit seam (Player_actor_Search_STONE_TC(),
 * m_player_common.c_inc), but for the HOST's own LOCAL hit rather than a remote peer's request --
 * see that call site's own doc for why single ownership requires this. Routes the hit through the
 * exact same s_host_money_rock bookkeeping a remote peer's hit uses (pc_net_game.c), so vanilla's own
 * ten_coin_entry_ex_proc()/bg_item_ten_coin_c path becomes structurally unreachable for a HOST on a
 * money-rock tile. No-op for single-player or a client, and for any tile/position that fails the
 * host's own validation (not a money-rock tile, every window slot busy, host not genuinely in the town
 * scene). Never blocks. Safe with out-of-range ut_x/ut_z (rejected internally). */
void pc_net_game_host_local_money_rock_hit(int ut_x, int ut_z);

/* Bug 4 fix: pc_net_game_is_money_bag_pickup_allowed() has been removed. A money bag dropped by
 * pc_net_game_request_money_rock_hit()'s own host-side commit is now an entirely ORDINARY field item
 * (see PCNetGameMoneyRockState's doc, pc_net_game.c) -- pickupable by any client through the
 * completely ordinary reserve/validate/commit pickup path, with no special per-tile allow-list needed
 * (pcnetgame_is_money_bag_item() is still used to detect the wallet-crediting case client-side, and
 * to keep money bags out of the network DROP path -- see that function's own doc). */

/* World Ecology T1 (tree shake/chop/bee-birth): classifies `item` (a raw field tile value) as one of
 * drop_fruit()'s own 21 source-tree rows (bg_item_common.c_inc) -- i.e. an item whose SHAKE would
 * actually mutate the field (drop an item and/or convert the tree tile), as opposed to a plain
 * TREE/CEDAR_TREE/GOLD_TREE (or any small/sapling/stump stage) shake, which only triggers a local
 * insect visual with no shared-state concern at all. Used at the SHAKE seam
 * (Player_actor_SetEffect_Shake_tree(), m_player_main_shake_tree.c_inc) to decide whether a given shake
 * needs to go through the network at all -- mirrors pc_net_game_is_droppable_item()'s own "plain int,
 * not mActor_name_t" convention to keep this header decomp-independent. CHOP (see
 * pc_net_game_request_tree_chop() below) always goes through the network regardless of this
 * classification -- a chop's own outcome (fruit drop vs. nothing vs. felling) depends on the host's own
 * authoritative per-tile hit-count table, not a single static table lookup. */
int pc_net_game_is_tree_fruit_drop_source(int item);

/* World Ecology T1: called from the decomp tree-shake seam (Player_actor_SetEffect_Shake_tree(),
 * m_player_main_shake_tree.c_inc) INSTEAD of item_tree_fruit_drop_proc(item, ut_x, ut_z, &drop_pos) --
 * but ONLY for an item pc_net_game_is_tree_fruit_drop_source() classifies as an actual field mutation
 * (the caller keeps running the plain TREE/CEDAR_TREE/GOLD_TREE insect-visual shake locally,
 * unconditionally, for every role -- see that classifier's own doc). Also called from the bee-birth
 * seam (Check_BirthBee_common(), m_player_common.c_inc) for the bee tile once
 * mPlib_able_birth_bee() (a LOCAL, client-only chase-flag check that is never gated) allows it. The
 * host re-reads the target tile FRESH (never trusting anything this client claims) and resolves the
 * dropped item/tree conversion itself -- see pcnetgame_host_commit_tree_shake() in pc_net_game.c.
 * Returns 1 for a connected, READY client (caller must not call item_tree_fruit_drop_proc() locally for
 * this tile); 0 for single-player or host (caller proceeds unmodified -- for the HOST role, see
 * pc_net_game_host_local_tree_shake() below). Never blocks. Safe with out-of-range ut_x/ut_z (rejected
 * internally). Unlike DIG_BURIED/MONEY_ROCK_HIT, this has NO "one already in flight" guard: several
 * distinct tree tiles may legitimately have a shake/bee-birth request outstanding at once (the shared
 * 4-deep s_field_action_queue FIFO, T0-C, already supports this). */
int pc_net_game_request_tree_shake(int ut_x, int ut_z);

/* World Ecology T1: called from the decomp axe-swing seam (Player_actor_CutTree_Swing_axe(),
 * m_player_main_swing_axe.c_inc) INSTEAD of Get_TreeNoToStumpNo()'s local cut-count decrement + fruit
 * drop/tree conversion + stump write -- unconditionally, for EVERY tree hit (unlike TREE_SHAKE, there
 * is no "would this actually mutate anything" pre-filter client-side: even a hit on an already-NOFRUIT
 * tree still needs its host-tracked cut-count decremented). The host maintains its own small per-tile
 * cut-count table (s_host_tree_cut_count, pc_net_game.c) mirroring vanilla's own
 * BIT_actor_tree_cutcount_check(), since that table only covers the calling process's own currently
 * loaded blocks and cannot serve a remote peer's chop. Returns 1 for a connected, READY client (caller
 * must not call Get_TreeNoToStumpNo()/write a stump locally -- it still plays the EffectBG_EFFECT_SHAKE
 * visual for feel, see that call site's own doc); 0 for single-player or host (caller proceeds
 * unmodified -- for the HOST role, see pc_net_game_host_local_tree_chop() below). Never blocks. Safe
 * with out-of-range ut_x/ut_z (rejected internally). No "one already in flight" guard, unlike DIG_BURIED/
 * MONEY_ROCK_HIT -- repeated axe swings need the 4-deep FIFO (T0-C) so a chop-spam sequence is not
 * silently dropped down to one hit. */
int pc_net_game_request_tree_chop(int ut_x, int ut_z);

/* World Ecology T1: called from the SAME decomp tree-shake/bee-birth seams as
 * pc_net_game_request_tree_shake() above, but for the HOST's own LOCAL shake/bee-birth rather than a
 * remote peer's request -- exactly mirroring pc_net_game_host_local_money_rock_hit()'s own precedent
 * for single ownership. No-op for single-player or a client, and for any tile that fails the host's own
 * validation. Never blocks. Safe with out-of-range ut_x/ut_z (rejected internally). */
void pc_net_game_host_local_tree_shake(int ut_x, int ut_z);

/* World Ecology T1: called from the SAME decomp axe-swing seam as pc_net_game_request_tree_chop()
 * above, but for the HOST's own LOCAL chop rather than a remote peer's request -- single ownership,
 * same precedent as pc_net_game_host_local_money_rock_hit()/pc_net_game_host_local_tree_shake(). No-op
 * for single-player or a client, and for any tile that fails the host's own validation. Never blocks.
 * Safe with out-of-range ut_x/ut_z (rejected internally). */
void pc_net_game_host_local_tree_chop(int ut_x, int ut_z);

/* World Ecology T-dig (dig/pitfall/shine family): DIG_HOLE (kind 5) -- digging a brand-new hole into
 * EMPTY_NO ground, or removing a flower/tree-stump/grass tuft/sapling (see pc_net_game.c's
 * pcnetgame_is_dig_removable_plant(), a byte-exact duplicate of mFI_CheckDigRemoveItem(),
 * m_field_info.c). Grants NOTHING on accept -- a removed plant flies off and fades, it is never placed
 * in a pocket (this corrects an earlier, incorrect assumption in T0's own doc comments). `hole_variant`
 * is this client's own proposed hole-shape pick (0..24) -- a TRUST BOUNDARY the host independently
 * range-validates and, if in range, commits EXACTLY (never clamps an out-of-range value; rejects it
 * outright instead -- see PCNetGameFieldActionRequestMsg's own doc, pc_net_game.c). Returns 1 for a
 * connected, READY client; 0 for single-player or host (see pc_net_game_host_local_dig_hole() below).
 * Never blocks. Safe with out-of-range ut_x/ut_z or hole_variant (both rejected/normalized internally). */
int pc_net_game_request_dig_hole(int ut_x, int ut_z, int hole_variant);

/* World Ecology T-dig (A-2): grant-carrying variant of pc_net_game_request_dig_hole() above, for the
 * golden-shovel 10% ITM_MONEY_100 bonus (mFI_GetDigStatus(), m_field_info.c) -- a case classified as
 * mFI_DIGSTATUS_GET_ITEM/mPlayer_INDEX_GET_SCOOP by vanilla, but which resolves to an ordinary DIG_HOLE
 * on the wire (the host only ever writes a plain hole; it never rolls or knows about the bonus).
 * `local_grant` (a plain item id, e.g. ITM_MONEY_100) is stored on the queued request and granted to a
 * free pocket slot via mPr_SetFreePossessionItem() if and only if this exact request is later accepted --
 * see pcnetgame_handle_client_field_action_result()'s DIG_HOLE branch, pc_net_game.c. The caller must
 * itself verify a free pocket slot exists before calling this (mirrors pc_net_game_request_dig_buried()'s
 * own precedent) and must not play any local animation for this path (matches DIG_BURIED: wait for the
 * RESULT before any visual). Accepted gap, same shape as DIG_BURIED's own: if the pocket fills up between
 * send and the RESULT arriving, the bonus is lost -- not solved with new mechanism. Same
 * out-of-range-input/role/scene safety as pc_net_game_request_dig_hole(). */
int pc_net_game_request_dig_hole_with_grant(int ut_x, int ut_z, int hole_variant, int local_grant);

/* World Ecology T-dig: FILL_HOLE (kind 6) -- filling an existing EMPTY hole (deposit OFF) back in;
 * commits to EMPTY_NO (a HOLE_SHINE tile filled in also just becomes EMPTY_NO -- destroying the shine
 * spot is correct vanilla behavior, not a bug). Returns 1 for a connected, READY client; 0 for
 * single-player or host (see pc_net_game_host_local_fill_hole() below). Never blocks. Safe with
 * out-of-range ut_x/ut_z (rejected internally). */
int pc_net_game_request_fill_hole(int ut_x, int ut_z);

/* World Ecology T-dig: PITFALL_CONSUME (kind 7) -- a player (or, on the host, a villager) falling INTO
 * an already-buried pitfall; a TRIGGER, distinct from DIGGING one up (which stays on the extended
 * DIG_BURIED path, kind 1 -- see pc_net_game_request_dig_buried()'s own updated doc). Deliberately
 * OPTIMISTIC: the caller must play its local fall animation immediately and unconditionally, WITHOUT
 * waiting for this request's RESULT -- the animation has no gameplay consequence, so there is nothing to
 * gate on network latency here. On a reject (a racing peer already consumed it, or it was already dug
 * up), the client reconciles by applying the host's own current tile value, echoed in the RESULT's
 * granted_item field (see pcnetgame_handle_client_field_action_result()'s own PITFALL_CONSUME branch,
 * pc_net_game.c). Returns 1 for a connected, READY client; 0 for single-player or host (see
 * pc_net_game_host_local_pitfall_consume() below). Never blocks. Safe with out-of-range ut_x/ut_z
 * (rejected internally). */
int pc_net_game_request_pitfall_consume(int ut_x, int ut_z);

/* World Ecology T-dig: DIG_SHINE (kind 8) -- digging up an unburied SHINE_SPOT. hole_variant is not a
 * parameter here: it is IGNORED host-side for this kind (a shine hole is not variant-shaped) and always
 * sent as 0. Commits tile -> HOLE_SHINE unconditionally. Grants NOTHING host-side -- the digging CLIENT
 * rolls its own bell amount (1000/10000/30000) locally, after its own free-pocket-slot pre-check, and
 * grants it privately on accept, matching vanilla's per-digger-luck design (no duplication risk: only
 * one digger can win the tile-consumption race). Returns 1 for a connected, READY client; 0 for
 * single-player or host (see pc_net_game_host_local_dig_shine() below). Never blocks. Safe with
 * out-of-range ut_x/ut_z (rejected internally). */
int pc_net_game_request_dig_shine(int ut_x, int ut_z);

/* World Ecology T-dig (D): grant-carrying variant of pc_net_game_request_dig_shine() above.
 * `local_grant` is THIS client's own privately-rolled bell amount (1000/10000/30000, per vanilla's
 * per-digger-luck design) -- stored on the queued request and granted to a free pocket slot via
 * mPr_SetFreePossessionItem() if and only if this exact request is later accepted (see
 * pcnetgame_handle_client_field_action_result()'s DIG_SHINE branch, pc_net_game.c). The caller must
 * itself verify a free pocket slot exists before calling this and must not play any local animation for
 * this path (matches DIG_BURIED precedent: wait for the RESULT before any visual). Accepted gap, same
 * shape as DIG_BURIED's own: if the pocket fills up between send and the RESULT arriving, the bells are
 * lost -- not solved with new mechanism. Same out-of-range-input/role/scene safety as
 * pc_net_game_request_dig_shine(). A single-flight guard (pcnetgame_field_action_grant_already_pending(),
 * pc_net_game.c) prevents a player from queueing more than one outstanding grant-carrying request
 * (this or pc_net_game_request_dig_hole_with_grant()) at a time. */
int pc_net_game_request_dig_shine_with_grant(int ut_x, int ut_z, int local_grant);

/* World Ecology T-dig: HOST-LOCAL wrappers for DIG_HOLE/FILL_HOLE/PITFALL_CONSUME/DIG_SHINE, mirroring
 * pc_net_game_host_local_money_rock_hit()'s/pc_net_game_host_local_tree_shake()'s own precedent exactly
 * -- single ownership: the host's own local action routes through the SAME validate/commit adapter a
 * remote peer's request uses. No-op for single-player or a client, and for any tile that fails the
 * host's own validation. Never blocks. Safe with out-of-range ut_x/ut_z/hole_variant (rejected/
 * normalized internally). NOT YET WIRED to a decomp caller in this build (see this task's own report for
 * the documented client-seam scope decision) -- these entry points exist for a future caller to use,
 * exactly like every other kind's own precedent. */
void pc_net_game_host_local_dig_hole(int ut_x, int ut_z, int hole_variant);
void pc_net_game_host_local_fill_hole(int ut_x, int ut_z);
void pc_net_game_host_local_pitfall_consume(int ut_x, int ut_z);
void pc_net_game_host_local_dig_shine(int ut_x, int ut_z);

/* Villager population/is_home milestone: called from mNpc_Grow()/mNpc_InitNpcData() and
 * mNpc_ForceRemove() (m_npc.c) right after the HOST's OWN local mutation has fully completed --
 * never before, and never as a substitute for it. `slot` is the Save_t.animals[] index
 * (0..ANIMAL_NUM_MAX-1) that changed. A no-op for single-player and for a client (mirrors
 * pc_net_game_notify_local_field_pickup()'s own pattern) -- the vanilla call site is unconditional,
 * so the role guard lives here rather than requiring the decomp caller to check first. Never blocks.
 * Safe with an out-of-range slot (rejected internally, logged, no send). Broadcasts
 * PC_NETGAME_MSG_VILLAGER_ARRIVAL / PC_NETGAME_MSG_VILLAGER_DEPARTURE to every READY client, stamped
 * with the current world_seq for the same stale/duplicate/reorder protection FIELD_UPDATE/WORLD_META
 * already use. */
void pc_net_game_notify_villager_arrival(int slot);
void pc_net_game_notify_villager_departure(int slot);

/* Friendship/mail sync milestone. See mNpc_AddFriendship()/mNpc_SendMailtoNpc()'s own doc comments
 * (m_npc.c) for the full contract; both function bodies are the only callers of every function
 * below. player_name/land_name/player_id/land_id are the same 4 PersonalID_c fields
 * PCNetGameIdentity already carries -- kept as plain arrays/ints here (not PersonalID_c) to keep
 * this header decomp-independent, matching every other function in it. */

/* Client-intercept path: called by mNpc_AddFriendship() INSTEAD OF applying `delta` locally.
 * `slot` is the Save_t.animals[] index owning the Anmmem_c the caller resolved (via
 * mNpc_FindAnimalSlotForMemory()). The memory slot itself is resolved/created HOST-SIDE from this
 * connection's own cached READY identity -- nothing about "which player" is ever sent per-request,
 * since a client has exactly one identity for its whole connection. The host's resulting
 * PC_NETGAME_MSG_FRIENDSHIP_UPDATE broadcast (received by every READY client including this one) is
 * the ONLY thing that ever mutates this client's local Anmmem_c for `slot` -- see
 * mNpc_PcApplyFriendshipUpdate(). A no-op for single-player and for the host itself (callers must
 * already be gated on PC_NETGAME_ROLE_CLIENT). Safe with slot == -1 or an out-of-range delta. */
void pc_net_game_request_friendship_delta(int slot, int delta);

/* Host/single-player-apply path: called by mNpc_AddFriendship() AFTER it has already applied
 * `friendship` (the final, already-clamped 0..127 value) locally to the caller's own Anmmem_c --
 * never before. `slot` is that memory's owning Save_t.animals[] index. A no-op for single-player
 * and for a client -- broadcasts PC_NETGAME_MSG_FRIENDSHIP_UPDATE to every READY client. Safe with
 * an out-of-range slot. */
void pc_net_game_notify_local_friendship_change(int slot, const uint8_t* player_name, const uint8_t* land_name,
                                                uint16_t player_id, uint16_t land_id, int friendship);

/* Client-intercept path: called by mNpc_SendMailtoNpc() INSTEAD OF running the real function
 * locally (which would mutate this client's own copy of the shared villager's Anmmem_c out of
 * authority). `mail`/`mail_size` are a raw, opaque copy of the decomp Mail_c the caller composed
 * (mail_size must be exactly sizeof(Mail_c); a mismatch is dropped defensively). `recipient_anm_idx`
 * is unused here (kept for symmetry with the villager-population notify functions' own `slot`
 * parameter) -- the host always re-resolves the true recipient from `mail`'s own header, never
 * trusting anything the sender claims about it. A no-op for single-player and for the host itself
 * (callers must already be gated on PC_NETGAME_ROLE_CLIENT). */
void pc_net_game_request_mail_delivery(int recipient_anm_idx, const void* mail, size_t mail_size);

/* Host/single-player-apply path: called by mNpc_PcApplyMailToVillagerMemory() (m_npc.c) AFTER the
 * host has already applied the delivery locally to its own Anmmem_c -- never before. `slot` is that
 * memory's owning Save_t.animals[] index; `letter`/`letter_info` are the resulting Anmplmail_c (raw
 * 258 bytes) and Anmlet_c (raw 1 byte) for this memory entry. A no-op for single-player (vanilla's
 * unmodified mNpc_SendMailtoNpc() runs directly there and this is simply never reached) and for a
 * client -- broadcasts PC_NETGAME_MSG_MAIL_DELIVERED to every READY client. Safe with an
 * out-of-range slot. */
void pc_net_game_notify_local_mail_delivered(int slot, const uint8_t* player_name, const uint8_t* land_name,
                                             uint16_t player_id, uint16_t land_id, int friendship,
                                             uint8_t letter_info, const uint8_t* letter);

/* N2 villager movement sync: PC_NETGAME_MSG_NPC_MOVE, host -> every READY client, UNRELIABLE
 * (matches PC_NETGAME_MSG_MOVE's own reliability class -- a high-frequency position stream where a
 * dropped sample is superseded by the next one a few dozen milliseconds later; queuing/retrying a
 * stale position would only ever make things worse). Scope: the on-screen, full-detail villager
 * NPC_ACTOR (ac_npc_move.c_inc) ONLY -- i.e. exactly the case where both a host and a client could
 * actually be looking at the same villager moving in the same acre at the same time. The separate,
 * off-screen SET_NPC_MANAGER inter-acre "daily walk" position (src/actor/ac_set_npc_manager.c,
 * mNpc_NpcList_c.position) is NOT covered by this milestone -- see the design note above
 * pc_net_game_world_is_host_authoritative()'s N1 client-side call site in m_npc_walk.c, and N2's own
 * completion report, for why: that system tracks no facing angle at all, and its per-block arrival
 * decision (mNpcW_ChangeNpcWalk(), aSNMgr_go_back_home_sub()) is fused with the same RNG-bearing
 * roster/goal-block selection N1 already deferred. A client's stale, independently-simulated
 * off-screen position for a villager self-heals the instant that villager's NPC_ACTOR is actually
 * created near a player (host or client): the very first NPC_MOVE sample received is necessarily far
 * from whatever the client's own unsynced simulation guessed, so the teleport-distance check below
 * snaps it directly to the host's true position rather than sliding across the gap.
 *
 * Identity/slot-reuse guard: every sample carries `slot` (Save_t.animals[] index) and `npc_id`
 * (Animal_c.id.npc_id at the moment the host sampled it) -- the SAME pairing the villager
 * population-sync milestone already established (PCNetGameVillagerArrivalMsg/
 * PCNetGameVillagerSnapshotMsg), never a new identity scheme. No new arrival/departure channel is
 * added for this: pc_net_game_get_npc_move_pose()'s `expected_npc_id` parameter is checked against
 * the CALLER's own live Save_t.animals[slot].id.npc_id every time a pose is consumed, so a late
 * packet aimed at a departed occupant is rejected at the moment of use, using population state that
 * is already authoritative and already applied synchronously by VILLAGER_ARRIVAL/_DEPARTURE/
 * _SNAPSHOT -- exactly the guard Part 3 of the design calls for, with no extra wire state. */

/* Host only (no-op for single-player/client): report this frame's already-resolved position/facing
 * for the villager NPC_ACTOR in Save_t.animals[slot] -- called once per frame per currently
 * instantiated, on-screen villager actor (see ac_npc_move.c_inc's aNPC_actor_move_show_before(),
 * right after aNPC_position_move()/aNPC_angle_calc() finish for the frame -- i.e. the SAME point the
 * unmodified host's own physics/AI just wrote to). Internally throttled per-slot to ~20 Hz (matches
 * the player movement stream) via its own accumulator -- safe, and a no-op, to call every single
 * frame. `npc_id` is Animal_c.id.npc_id at the moment of sampling, purely so a slot that departed and
 * was immediately reoccupied within the same frame can never have the old occupant's sample posted
 * under the new occupant's identity. Safe with an out-of-range slot (ignored) or non-finite
 * pos/angle (dropped, logged, matching PC_NETGAME_MSG_MOVE's own validation). */
void pc_net_game_notify_npc_move(int slot, uint16_t npc_id, float pos_x, float pos_y, float pos_z,
                                 int16_t facing_angle, uint8_t action_type);

/* Client only: fills *out_pos_x/y/z and *out_facing_angle with the current delay-interpolated
 * authoritative pose for villager Save_t.animals[slot], and *out_moving with 1/0 (derived purely
 * from observed position deltas between the two straddling network samples -- no action/schedule
 * state ever rides this wire; see the design note above). Returns 1 if a pose was written; returns 0
 * -- meaning the caller must hold the actor's current position/facing/animation completely unchanged
 * -- if: this process is not a READY client (nothing to consume: single-player, host, or not yet
 * READY), `slot` is out of range, no NPC_MOVE has been received yet for this slot (actor just
 * created, or this villager has not been in view of the host's own player), or `expected_npc_id`
 * (the caller's own live Save_t.animals[slot].id.npc_id) does not match the identity carried by the
 * most recently received sample for this slot (the slot-reuse guard above) -- in the last case any
 * stale buffered samples for this slot are also discarded, so a later, correctly-identified arrival
 * never has to compete with leftover data. Never blocks, never allocates, safe every frame. */
int pc_net_game_get_npc_move_pose(int slot, uint16_t expected_npc_id, float* out_pos_x, float* out_pos_y,
                                  float* out_pos_z, int16_t* out_facing_angle, int* out_moving,
                                  uint8_t* out_action_type);

/* N3 Channel B: is_home/hide/forced-schedule sync. Host only (no-op for single-player/client): report
 * this villager's current is_home/hide/forced_type/forced_timer -- called once per frame per real
 * Save_t.animals[]-backed villager NPC_ACTOR, whether or not it is currently visible/hidden (see
 * ac_npc_move.c_inc's aNPC_pc_host_check_state(), called from both the shown and hidden per-frame
 * paths). Internally dirty-checked against the previous broadcast for this slot; only actually sends
 * (reliable) when is_home, hide, forced_type, or forced-active (forced_timer > 0) changed -- never on
 * forced_timer's own continuous per-frame countdown. Safe and cheap to call every frame. */
void pc_net_game_notify_npc_state(int slot, uint16_t npc_id, uint8_t is_home, uint8_t hide, uint8_t forced_type,
                                  int forced_timer);

/* Client only: fills *out_is_home/out_hide/out_forced_type/out_forced_timer_remaining with the latest
 * applied NPC_STATE for Save_t.animals[slot]. Returns 1 if a state was written; returns 0 -- meaning
 * the caller must not touch any of is_home/hide/forced_type/forced_timer -- if: not a READY client,
 * `slot` out of range, nothing received yet for this slot, or `expected_npc_id` (the caller's own live
 * Save_t.animals[slot].id.npc_id) does not match the identity carried by the most recently applied
 * state for this slot (slot-reuse guard, mirroring pc_net_game_get_npc_move_pose()'s own). Apply all
 * four fields together, atomically, in one place -- never is_home separately from hide separately from
 * forced_type at different times (see the design note above PC_NETGAME_MSG_NPC_STATE, pc_net_game.c). */
int pc_net_game_get_npc_state(int slot, uint16_t expected_npc_id, uint8_t* out_is_home, uint8_t* out_hide,
                              uint8_t* out_forced_type, uint16_t* out_forced_timer_remaining);

/* World Ecology: snowmen (host-authoritative build/break/melt sync). Called from
 * aSNOWMAN_Set_PSnowman_info() (ac_snowman.c) at the moment the two-half actor's destructor decides
 * to register the finished snowman -- see that call site's own doc for why the destructor, not the
 * earlier visual-completion callback, is the correct seam. Deliberately places NO reach/IN_TOWN
 * precondition on the CALLER (the player may already be walking away, or the scene may be mid-
 * transition) -- the host itself decides acceptance purely from field/slot state. Returns 1 for a
 * connected, READY client (caller must not call mSN_regist_snowman_society() locally); 0 for
 * single-player or host, in which case the caller proceeds exactly as before, unmodified. Never
 * blocks. Safe with out-of-range ut_x/ut_z (rejected internally). head_size/body_size/score are the
 * same 0..255 (0..3 for score) fields mSN_snowman_info_c.data already carries -- never re-derived or
 * re-validated locally; the host is the sole judge of acceptance. Known, accepted limitation (see this
 * task's own report): if this request is lost, rejected, or never sent (the snowman broken before its
 * destructor runs), the host never learns this build's completion-time dates -- a later SNOWMAN_STATE
 * can then restore older dates than this client wrote locally into Save_t.snowman_year/month/day/hour,
 * potentially allowing a same-day local snowball respawn. Not engineered around; documented only. */
int pc_net_game_request_snowman_build(int ut_x, int ut_z, int head_size, int body_size, int score);

/* World Ecology: snowmen -- called from the decomp snowman-break seam (see aPSM_actor_move(),
 * ac_psnowman.c) INSTEAD of calling mSN_ClearSnowman() directly. Uses FIELD_ACTION_REQUEST kind
 * PC_NETGAME_FIELD_ACTION_KIND_SNOWMAN_BREAK (9) through the same 4-deep client queue every other
 * FIELD_ACTION kind uses (pc_net_game.c) -- unlike DIG_BURIED/MONEY_ROCK_HIT this deliberately does
 * NOT limit itself to one in-flight request at a time. Returns 1 for a connected, READY client (the
 * caller must leave actor->npc_id untouched and still call Actor_delete() -- see that call site's own
 * doc for why: a host rejection then self-heals via the actor's own restore-on-destroy write); 0 for
 * single-player or host (caller proceeds unmodified). Never blocks. Safe with out-of-range ut_x/ut_z
 * (rejected internally). */
int pc_net_game_request_snowman_break(int ut_x, int ut_z);

/* World Ecology Wildlife Sync T0/T1 opt-in gate: 1 iff --authoritative-wildlife was passed
 * (g_pc_authoritative_wildlife, pc_platform.h/pc_main.c), 0 otherwise (the default). Off by default
 * so that the host-authoritative wildlife adapter (pc_wildlife_authority.c), which has no
 * client-side presentation yet, never silently replaces vanilla's local fish/bug spawning in
 * ordinary multiplayer play. Checked by aSetMgr_move_set() (ac_set_manager.c) AS WELL AS by
 * pc_net_game_request_wildlife_spawn_trigger()/pc_net_game_host_local_wildlife_spawn_trigger()
 * below, in each case IN ADDITION TO (never instead of) the existing
 * pc_net_game_world_is_host_authoritative()/pc_net_game_role() checks. */
int pc_net_game_authoritative_wildlife_enabled(void);

/* World Ecology Wildlife Sync T0 (authority seam foundation only -- see pc_wildlife_authority.h's
 * own scope doc: NOT fish/bug catching, NOT a snapshot, NOT bee/ant capture sync). Called from the
 * decomp wade-trigger seam (aSetMgr_move_set(), ac_set_manager.c) INSTEAD of running
 * aSOI_insect_set()/aSOG_gyoei_set() locally, at the exact point SET_MANAGER would otherwise invoke
 * proc_table[set_ovl_type] for the player's own next_bx/next_bz acre. `bx`/`bz` are that SAME raw
 * block-number acre pair (SET_MANAGER's own player_pos.next_bx/next_bz convention) -- deliberately
 * the ONLY thing sent: never a species, an RNG result, or a position, since the host alone is the
 * source of truth for the actual decision (see pc_wildlife_authority.c). Returns 1 for a connected,
 * READY client with --authoritative-wildlife active (caller must not run the local overlay proc for
 * this acre this call); 0 for single-player, host, OR a client without --authoritative-wildlife, in
 * which case the caller proceeds exactly as before, unmodified -- for the HOST role, see
 * pc_net_game_host_local_wildlife_spawn_trigger() below. Never blocks. Safe with an out-of-range
 * bx/bz (rejected internally, without sending). Like pc_net_game_request_tree_shake(), silently does
 * nothing (returns 1, swallowed) if the client is not currently in the town scene
 * (pcfa_scene_is_town()). No "one already in flight" guard, matching TREE_SHAKE's own precedent -- a
 * wade event for a different acre may legitimately arrive while an earlier one is still being
 * processed. */
int pc_net_game_request_wildlife_spawn_trigger(int bx, int bz);

/* World Ecology Wildlife Sync T0: called from the SAME decomp wade-trigger seam as
 * pc_net_game_request_wildlife_spawn_trigger() above, but for the HOST's own LOCAL wade event
 * rather than a remote peer's request -- mirrors pc_net_game_host_local_tree_shake()'s own
 * single-ownership precedent exactly. No-op for single-player -- a single-player host still HAS a
 * role of PC_NETGAME_ROLE_HOST once hosting is active, but this function only ever runs the
 * network-authoritative adapter path when actually hosting a live session WITH
 * --authoritative-wildlife active; when not hosting at all, or hosting without the flag, the
 * caller's own vanilla proc_table invocation is left completely untouched by this seam (see the
 * call site's own doc in ac_set_manager.c). No-op for a client (that role always goes through
 * pc_net_game_request_wildlife_spawn_trigger() instead). Never blocks. Safe with an out-of-range
 * bx/bz or the host's own scene not being the town (rejected internally by
 * pcwld_host_spawn_trigger(), pc_wildlife_authority.c). */
void pc_net_game_host_local_wildlife_spawn_trigger(int bx, int bz);

/* World Ecology Wildlife Sync T-catch (ordinary fish catching only -- see this milestone's own ABSOLUTE
 * SCOPE LIMIT: no bug catching, tournaments, or ant/bee special-case handling). Called from
 * Player_actor_setup_main_Notice_rod() (m_player_main_notice_rod.c_inc), the exact point vanilla would
 * otherwise call Player_actor_putin_item() unconditionally, for a CLIENT under a host-authoritative
 * session. `entity_id` is the fish actor's own stamp (aGYO_pc_get_entity_id_stamp(), ac_gyoei.h/.c);
 * `claimed_species` is uki->gyo_type (the bobber's own settled species); `local_grant_item` is
 * uki->get_fish_type_proc()'s result -- the caller computes this ONCE, up front, and this function
 * remembers it to apply later, on accept, exactly like pc_net_game_request_dig_shine_with_grant()'s own
 * local_grant pattern (never re-derived host-side -- see PCNetGameCatchResultMsg's own doc,
 * pc_net_game.c). Returns 1 for a connected, READY client with authoritative wildlife active (the
 * caller must SKIP the vanilla Player_actor_putin_item() call AND the mSM_COLLECT_FISH_SET() collection-
 * bit write for this catch -- both are deferred to the eventual CATCH_RESULT, see
 * pcnetgame_handle_client_catch_result()'s own doc, pc_net_game.c); 0 for single-player, host, a
 * non-authoritative client, or malformed input (entity_id == 0, no gameplay save loaded, not in the town
 * scene) -- in every 0 case the caller must proceed with the ordinary vanilla grant, unmodified, exactly
 * as if this milestone did not exist. Never blocks. */
int pc_net_game_request_catch_fish(uint32_t entity_id, int claimed_species, int local_grant_item);

/* World Ecology Wildlife Sync T-catch: the HOST's own local catch, from the SAME seam as
 * pc_net_game_request_catch_fish() above -- mirrors pc_net_game_host_local_tree_shake()'s own
 * single-ownership precedent exactly. Resolves synchronously against the SAME authoritative-table
 * validation a remote peer's CATCH_REQUEST uses (race-safe against a peer's own simultaneous claim on
 * the SAME entity_id -- see pcnetgame_validate_and_commit_catch()'s own race-safety doc, pc_net_game.c).
 * Returns 1 iff accepted: the entity is already removed from the authoritative table and
 * WILDLIFE_DESPAWN already broadcast by the time this returns -- the caller must then run the vanilla
 * Player_actor_putin_item() grant AND the mSM_COLLECT_FISH_SET() collection-bit write UNMODIFIED, with
 * NO network round-trip to itself. Returns 0 if rejected (a genuine race loss, a stale/unknown
 * entity_id, or authoritative wildlife not enabled/hosting not active) -- the caller must skip BOTH the
 * grant and the collection-bit write entirely (nothing was actually caught). Never blocks. */
int pc_net_game_host_local_wildlife_catch(uint32_t entity_id, int claimed_species);

/* World Ecology Wildlife Sync T-catch (residual review fix): tri-state query used to gate the
 * full-pockets EXCHANGE screen's actual grant (Player_actor_request_proc_index_fromPutaway_rod(),
 * m_player_main_putaway_rod.c_inc) against what the host actually decided about this entity_id's catch,
 * rather than granting unconditionally to whatever slot the player picks.
 *
 * Why this exists: the exchange screen is a player-paced UI flow -- the player takes real time
 * choosing "exchange" vs "release" while Player_actor_setup_main_Notice_rod() already sent (or
 * resolved, for the host's own local catch) the CATCH_REQUEST for the SAME entity_id. Without this
 * check, a client whose request the host later REJECTS (e.g. lost a race for the same fish to another
 * peer, or the CATCH_REQUEST send itself silently failed -- window full) would still see the
 * full-pockets dialogue and could still choose "exchange," locally granting itself an item the host
 * never actually awarded it -- a genuine duplicate. The host's own local full-pockets catch has the
 * exact same shape when a remote peer's request wins the race first (synchronous rejection).
 *
 * Returns:
 *   PC_NETGAME_CATCH_STATUS_NONE     -- no host-authoritative decision is tracked for this entity_id
 *                                        (authoritative wildlife disabled, entity_id == 0, or this
 *                                        catch never went through the network path at all) -- the
 *                                        caller should proceed with the ordinary vanilla grant,
 *                                        unmodified, exactly as if this milestone did not exist.
 *   PC_NETGAME_CATCH_STATUS_PENDING  -- (client only) the CATCH_REQUEST for this entity_id was sent
 *                                        and is still awaiting CATCH_RESULT. Rare in practice (the
 *                                        exchange dialogue's own player-paced timing normally lets the
 *                                        RESULT arrive first) -- the caller's chosen policy for this
 *                                        case is documented at its own call site (see that site's own
 *                                        doc for why it defaults to the safer no-grant behavior rather
 *                                        than blocking or defaulting to grant).
 *   PC_NETGAME_CATCH_STATUS_ACCEPTED -- the host accepted this catch (client CATCH_RESULT, or the
 *                                        host's own synchronous local catch) -- safe to grant.
 *   PC_NETGAME_CATCH_STATUS_REJECTED -- the host rejected this catch, the CATCH_RESULT disagreed with
 *                                        the original request (entity/owner mismatch), or the
 *                                        CATCH_REQUEST could never be sent at all (window full) --
 *                                        the caller MUST NOT grant anything; redirect to the same
 *                                        no-grant completion the code already takes when no grant is
 *                                        needed (see the call site's own doc for exactly which path).
 *
 * A resolved (ACCEPTED/REJECTED) entry is consumed (cleared back to NONE) the first time it is
 * observed via this query, mirroring s_catch_pending's own "the RESULT arrived: resolved either way,
 * never re-applied" precedent -- the putaway-rod call site is the ONLY consumer, and only ever queries
 * once per catch. Never blocks. */
int pc_net_game_query_catch_outcome(uint32_t entity_id);

/* M9-D F2: record a locally-decided DENIED outcome for entity_id (same effect as a host reject: the next
 * pc_net_game_query_catch_outcome(entity_id) returns PC_NETGAME_CATCH_STATUS_REJECTED once). Called by the
 * catch seams (m_player_main_notice_net/rod.c_inc) when a DISCONNECTED client (role still CLIENT, link
 * not READY) suppresses the local grant for a tracked entity: without it the putaway exchange gate would
 * see NONE and let the full-pockets "exchange" grant an item the host still considers live (duplicate).
 * No wire traffic. entity_id == 0 is ignored. */
void pc_net_game_record_local_catch_denied(uint32_t entity_id);

/* ================================================================================================
 * World Ecology Wildlife Sync T4: ordinary bug catching. Reuses the EXACT SAME CATCH_REQUEST/
 * CATCH_RESULT/WILDLIFE_DESPAWN messages and the SAME pcnetgame_validate_and_commit_catch() core T-catch
 * already established for fish (their wire shape carries no fish-specific field -- kind is derived
 * host-side from the authoritative record itself, never sent by the requester) -- see pc_net_game.c's
 * own T4 top-of-section doc for the full design and pcwld_bug_species_matches_claim()/pcwld_bug_local_
 * actor_for_entity() (pc_wildlife_authority.h) for the bug-specific pieces this reuses them alongside.
 * ================================================================================================ */

/* Client side: identical contract to pc_net_game_request_catch_fish() above, for a PC_WILDLIFE_KIND_BUG
 * entity instead. Called from Player_actor_setup_main_Notice_net() (m_player_main_notice_net.c_inc),
 * the exact point vanilla would otherwise call Player_actor_putin_item()/mSM_COLLECT_INSECT_SET()
 * unconditionally, for an ORDINARY bug catch only (player->item_net_catch_type == 0 -- an ant/bee actor
 * catch is a distinct, T5-scoped path and never reaches this function). `entity_id` comes from
 * pc_net_game_bug_entity_id_for_label() below; `claimed_species` is the label's own insect_type;
 * `local_grant_item` is the item this client would otherwise have granted itself (computed once, up
 * front, exactly like the fish call site). Same return/caller contract as pc_net_game_request_catch_
 * fish(): 1 means the caller must SKIP the vanilla grant and mSM_COLLECT_INSECT_SET() write (deferred to
 * CATCH_RESULT); 0 means proceed with the ordinary vanilla grant, unmodified. */
int pc_net_game_request_catch_bug(uint32_t entity_id, int claimed_species, int local_grant_item);

/* Thin wrapper around pcwld_bug_entity_id_for_local_actor() (pc_wildlife_authority.h) so game files
 * (m_player_main_notice_net.c_inc, m_player_main_putaway_net.c_inc) never need to include pc_wildlife_
 * authority.h directly -- same separation of concerns as every other pcwld_-vs-pc_net_game_ split in this
 * header. label_actor is player->item_net_catch_label cast back to void* (an ordinary bug catch only,
 * player->item_net_catch_type == 0); insect_type is ((aINS_INSECT_ACTOR*)label_actor)->type. Returns 0
 * if this exact pointer+species is not currently tracked as a live local BUG presentation entry (e.g.
 * this bug predates T4 tracking, or an indoor round-trip invalidated the mapping -- see pcwld_bug_
 * controller_torn_down()'s own doc) -- the caller falls through to the ordinary vanilla grant in that
 * case, exactly like fish's own entity_id == 0 fallback. */
uint32_t pc_net_game_bug_entity_id_for_label(const void* label_actor, int insect_type);

#define PC_NETGAME_CATCH_STATUS_NONE     0
#define PC_NETGAME_CATCH_STATUS_PENDING  1
#define PC_NETGAME_CATCH_STATUS_ACCEPTED 2
#define PC_NETGAME_CATCH_STATUS_REJECTED 3

/* World Ecology Wildlife Sync T0: broadcasts one freshly-created authoritative wildlife record to
 * every READY client, reliable. Called ONLY from pcwld_table_insert() (pc_wildlife_authority.c)
 * right after a spawn decision is captured into the host's table -- never called directly from
 * gameplay code. HOST-only (a no-op otherwise). See PCNetGameWildlifeSpawnMsg's own doc,
 * pc_net_game.c, for the wire format. T1 UPDATE (stale as of T0): the client-side handler now
 * materializes a REAL local vanilla fish/bug actor via pcwld_presentation_create() rather than only
 * logging -- see pc_wildlife_authority.h/.c and pcnetgame_handle_client_wildlife_spawn() (pc_net_game.c). */
void pc_net_game_notify_wildlife_spawn(uint32_t entity_id, int kind, int species, int bx, int bz,
                                        float pos_x, float pos_y, float pos_z);

/* D3 Q5: HOST only. Nonzero when a peer with unsaved accepted record uploads went away (coalesced flag; the consumer is the
 * early authoritative host save in pc_vi.c, D3-4). pc_net_game_record_note_saved() clears the flag and every per-resident
 * unsaved marker (called from pc_net_game_record_after_gci_save() after a successful GCI save). */
int pc_net_game_record_early_save_requested(void);
void pc_net_game_record_note_saved(void);
/* D3-4: early_save_requested() AND the host world is ready (pc_vi.c adds HOST role, pcfa_save_ready and the 5 s coalescing). */
int pc_net_game_record_early_save_due(void);
/* D3-4: called by pc_m_card.c (main thread) right after every successful Card-A GCI save: clears the unsaved markers and
 * writes the host record sidecar save/mp/records.dat (pc_mp_records.c). No-op unless this process is the HOST. */
void pc_net_game_record_after_gci_save(const char* gci_path);

/* D3 client half: graceful-quit flush, called from the CLIENT branch of the shutdown block in src/main.c (after the game
 * loop ended, before pc_net_game_shutdown()). If this process is a client whose resident record is SYNCED and dirty, uploads
 * it and waits at most `max_ms` for the host's APPLIED (best effort, strictly bounded, never blocks longer; it pumps only the
 * transport and consumes RECORD_ACKs, no game state). A no-op for every other role/state. It writes no save. */
void pc_net_game_client_record_quit_flush(unsigned max_ms);

#ifdef __cplusplus
}
#endif
#endif /* PC_NET_GAME_H */
