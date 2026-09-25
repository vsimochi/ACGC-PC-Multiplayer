/* pc_net_game.c - Stage 1: in-game networking integration (role selection + identity handshake).
 *
 * See pc_net_game.h for the design summary. This file is the ONLY place that mixes the
 * Stage 0 transport (pc_net.h) with decomp game state -- for identity/appearance/movement it only
 * ever reads a handful of plain-integer/fixed-byte-array fields out of the currently active save
 * slot (Now_Private) to build an explicit, hand-written wire message. Stage 5A adds the first
 * exception: a client that receives an authoritative PICKUP_RESULT grant writes the resulting item
 * into Now_Private's own pockets via the real, unmodified mPr_SetFreePossessionItem() (see
 * pcnetgame_handle_client_pickup_result()) -- everywhere else in this file remains read-only. This
 * file never touches an ACTOR, never reads pad state, and never calls into rendering/audio.
 *
 * Why not just serialize Private_c/PersonalID_c directly: PersonalID_c (m_personal_id.h)
 * happens to already be flat/POD (no pointers), but "flat today" is not a wire-format
 * guarantee, and Private_c itself is a much larger struct that does hold pointer-shaped and
 * game-internal fields we have no business ever putting on a socket. So a small,
 * independently-defined PCNetGameIdentity(*Msg) exists here instead, populated by explicitly
 * copying only the four fields we actually need (player_name, land_name, player_id, land_id).
 *
 * player_name / land_name are copied byte-for-byte from PersonalID_c but are NOT ASCII text --
 * they are the game's internal fixed-width character/font-code encoding used for in-game name
 * entry (see PLAYER_NAME_LEN/LAND_NAME_SIZE, m_personal_id.h). Stage 1 treats them as an opaque
 * 8-byte blob: safe to transmit (fixed size, no pointers), not decoded into readable text --
 * that decode/encode step belongs to whichever later stage actually displays remote names.
 */
#include "pc_net_game.h"
#include "pc_net.h"
#include "pc_remote_player.h"

#include "m_common_data.h"
#include "m_private.h"
#include "m_personal_id.h"
#include "m_player_lib.h" /* Stage 3: GET_PLAYER_ACTOR_NOW(), PLAYER_ACTOR, mPlayer_INDEX_*, and
                            * (transitively, via m_actor.h -> game.h) gamePT/GAME/
                            * graph_dt_period_elapsed()/graph_dt_frame_time(). Read-only: this
                            * file only ever *samples* the local player, never writes to it. */
#include "m_name_table.h"  /* Stage 4C-1: RSV_CLOTH, CLOTH_NUM -- see pcnetgame_build_appearance_msg().
                            * Stage 5A: also EMPTY_NO/ITM_* -- see pcnetgame_resolve_pickup_item(). */
#include "m_needlework.h"  /* Stage 4C-1: mNW_original_design_c -- see pcnetgame_build_appearance_msg().
                            * Read-only here too: only ever copies out of Now_Private->my_org[],
                            * never writes to it (mPr_ORIGINAL_DESIGN_IDX_VALID comes from the
                            * already-included m_private.h). */
#include "m_field_info.h"  /* Stage 5A: mFI_UtNum2UtFG()/mFI_UtNumtoFGSet_common()/mFI_UtNumCheck()/
                            * mFI_UtNum2CenterWpos() -- see pcnetgame_handle_host_pickup_request()
                            * and pcnetgame_apply_field_update_or_defer(). */
#include "m_collision_bg.h" /* Stage 5B-1: mCoBG_CheckPlace() -- see
                             * pcnetgame_validate_and_resolve_drop(). Already extern-declared
                             * (include/m_collision_bg.h:440); no decomp changes needed to reach it. */
#include "pc_lowaddr.h"    /* PC_LOWADDR_LIMIT -- see pcnetgame_is_real_player_actor() */
#include "pc_platform.h"   /* Stage 5A.1: g_pc_pickup_test_seed -- see pcnetgame_run_pickup_test_seed() */

#include <math.h>   /* fabsf() -- see pcnetgame_validate_and_resolve_pickup() */
#include <stdio.h>
#include <string.h>

_Static_assert(PC_NETGAME_NAME_LEN == PLAYER_NAME_LEN, "PC_NETGAME_NAME_LEN must match PLAYER_NAME_LEN (m_personal_id.h)");
_Static_assert(PC_NETGAME_LAND_LEN == LAND_NAME_SIZE, "PC_NETGAME_LAND_LEN must match LAND_NAME_SIZE (m_land_h.h)");

/* ---- wire messages: carried as the raw payload of a pc_net PC_NET_EVENT_DATA event. pc_net
 * itself has no idea these exist; msg_type is this module's own sub-protocol discriminator. ---- */

typedef enum PCNetGameMsgType {
    PC_NETGAME_MSG_IDENTITY     = 1, /* client -> host, sent once transport-connected */
    PC_NETGAME_MSG_IDENTITY_ACK = 2, /* host -> client, sent after accepting an IDENTITY */
    PC_NETGAME_MSG_REJECT       = 3, /* host -> client, sent instead of ACK if rejected */
    PC_NETGAME_MSG_MOVE         = 4, /* Stage 3: client -> host (own movement), host -> client
                                       * (its own movement, or a relay of another client's) */
    PC_NETGAME_MSG_APPEARANCE   = 5, /* Stage 4C-1: client -> host (own appearance, sent once
                                       * alongside IDENTITY), host -> client (its own appearance,
                                       * sent once alongside IDENTITY_ACK, or a relay of another
                                       * client's) -- see pcnetgame_build_appearance_msg() */
    PC_NETGAME_MSG_PICKUP_REQUEST = 6, /* Stage 5A: client -> host only. See
                                         * pcnetgame_handle_host_pickup_request(). */
    PC_NETGAME_MSG_PICKUP_RESULT  = 7, /* Stage 5A: host -> the one requesting client only (never
                                         * broadcast -- see PC_NETGAME_MSG_FIELD_UPDATE for what
                                         * every OTHER client needs). */
    PC_NETGAME_MSG_FIELD_UPDATE   = 8, /* Stage 5A: host -> every READY client (including the
                                         * requester). Carries only the resulting tile value, never
                                         * a request id -- it is a plain fact about the shared
                                         * world, not tied to any one request. */
    PC_NETGAME_MSG_DROP_REQUEST   = 9,  /* Stage 5B-1: client -> host only. See
                                          * pcnetgame_handle_host_drop_request(). */
    PC_NETGAME_MSG_DROP_RESULT    = 10, /* Stage 5B-1: host -> the one requesting client only (never
                                          * broadcast -- PC_NETGAME_MSG_FIELD_UPDATE, reused
                                          * unchanged, is what every OTHER client needs). */
} PCNetGameMsgType;

typedef enum PCNetGameRejectReason {
    PC_NETGAME_REJECT_PROTOCOL_MISMATCH = 1,
    PC_NETGAME_REJECT_SERVER_FULL       = 2,
} PCNetGameRejectReason;

typedef struct PCNetGameIdentityMsg {
    uint8_t  msg_type;              /* PC_NETGAME_MSG_IDENTITY */
    uint8_t  _reserved0[3];
    uint32_t protocol_version;
    uint8_t  player_name[PC_NETGAME_NAME_LEN];
    uint8_t  land_name[PC_NETGAME_LAND_LEN];
    uint16_t player_id;
    uint16_t land_id;
    uint8_t  has_save;
    uint8_t  _reserved1[3];
} PCNetGameIdentityMsg;
_Static_assert(sizeof(PCNetGameIdentityMsg) == 32, "PCNetGameIdentityMsg wire size drifted");
/* Every wire message below carries TWO compile-time checks, not one: the exact-size assert pins
 * this module's own sub-protocol layout (catches accidental padding/field drift between builds),
 * while this second `<= PC_NET_MAX_PAYLOAD` assert pins the TRANSPORT's hard ceiling (pc_net.h:39).
 * pc_net_send() silently returns 0 for any payload above that ceiling (pc_net.c:371) and the
 * receive path discards one as malformed (pc_net.c:274) -- so a future message that outgrows it
 * would otherwise compile cleanly and then just never arrive, with no error at either end. The
 * exact-size assert alone does not prevent this: whoever grows a struct simply updates its pinned
 * number too. Failing the build here is the only place that mistake is guaranteed to surface. */
_Static_assert(sizeof(PCNetGameIdentityMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameIdentityMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

typedef struct PCNetGameIdentityAckMsg {
    uint8_t  msg_type;              /* PC_NETGAME_MSG_IDENTITY_ACK */
    uint8_t  accepted;              /* always 1 here; a rejection is a separate message (below) */
    uint16_t assigned_peer_id;      /* the PCNetPeerId pc_net.c assigned this connection, widened */
    uint32_t protocol_version;      /* host's own version, for the client's diagnostics */
    uint8_t  player_name[PC_NETGAME_NAME_LEN]; /* host's identity, so the client knows who it reached */
    uint8_t  land_name[PC_NETGAME_LAND_LEN];
    uint16_t player_id;
    uint16_t land_id;
    uint8_t  has_save;
    uint8_t  _reserved[3];
} PCNetGameIdentityAckMsg;
_Static_assert(sizeof(PCNetGameIdentityAckMsg) == 32, "PCNetGameIdentityAckMsg wire size drifted");
_Static_assert(sizeof(PCNetGameIdentityAckMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameIdentityAckMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

typedef struct PCNetGameRejectMsg {
    uint8_t  msg_type;              /* PC_NETGAME_MSG_REJECT */
    uint8_t  reason;                /* PCNetGameRejectReason */
    uint16_t _reserved;
    uint32_t expected_protocol_version;
} PCNetGameRejectMsg;
_Static_assert(sizeof(PCNetGameRejectMsg) == 8, "PCNetGameRejectMsg wire size drifted");
_Static_assert(sizeof(PCNetGameRejectMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameRejectMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* Stage 3 movement snapshot. Fixed-size, no pointers, matching the wire-message convention above.
 *
 * frame is the SENDER's own monotonically increasing per-send counter -- treat it primarily as a
 * sequence number, not a timestamp. Two different processes' counters do NOT share an origin (each
 * starts at 0 independently), so it must never be compared against this process's own frame/time
 * domain -- see PCNetMoveSample's doc in pc_net_game.h for how the receiver actually builds its
 * presentation timeline. Within one sender's own stream, though, a plain `>` comparison is exactly
 * the ordering/duplicate/staleness signal needed; a uint32_t counter incrementing once per SEND
 * (not per real frame) at 20Hz would take on the order of 200+ years to wrap, so wraparound-aware
 * comparison is intentionally not implemented here.
 *
 * net_player_id is a PCNetPlayerId (see pc_net_game.h): ignored when a client sends this to the
 * host (the host trusts its own transport-level sender id, ev.peer, never a client-claimed value);
 * meaningful on every host -> client send, where it is either PC_NETGAME_HOST_PLAYER_ID (the
 * host's own movement) or the true originating client's real peer id (a relay). */
typedef struct PCNetMoveMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_MOVE */
    uint8_t  net_player_id;
    uint8_t  move_state;    /* PCMoveState */
    int8_t   item_kind;     /* mirrors PLAYER_ACTOR::item_kind, -1 = none */
    uint32_t frame;
    float    pos_x;
    float    pos_y;
    float    pos_z;
    int16_t  facing_angle;  /* world.angle.y, native engine angle units */
    int16_t  _reserved0;
    float    speed;
} PCNetMoveMsg;
_Static_assert(sizeof(PCNetMoveMsg) == 28, "PCNetMoveMsg wire size drifted");
_Static_assert(sizeof(PCNetMoveMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetMoveMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* Stage 4C-1: a player's visible appearance -- sent once, reliably, alongside IDENTITY/
 * IDENTITY_ACK (never as part of the 20Hz movement stream: this is much larger than a movement
 * sample and changes far less often -- see the Stage 4C investigation's explicit guidance against
 * enlarging PCNetMoveMsg). net_player_id follows PCNetMoveMsg's exact convention: ignored on a
 * client's send to the host (the host uses its own transport-verified peer id, matching
 * pcnetgame_handle_host_move()'s precedent), meaningful on every host -> client send
 * (PC_NETGAME_HOST_PLAYER_ID for the host's own appearance, or the true originating client's peer
 * id for a relay).
 *
 * `design` is the decomp's own mNW_original_design_c, embedded directly (verified POD, no
 * pointers, already designed to be DMA'd/copied as raw bytes in the original engine -- see the
 * Stage 4C investigation) -- always present on the wire so this stays one fixed-size message with
 * no variable-length branch, but only meaningful when is_custom_design is set; zeroed otherwise.
 * The struct's own ATTRIBUTE_ALIGN(32) (on its nested texture field) is why _reserved0 pads the
 * header out to exactly 32 bytes before it -- see the _Static_assert below. */
typedef struct PCNetGameAppearanceMsg {
    uint8_t  msg_type;             /* PC_NETGAME_MSG_APPEARANCE */
    uint8_t  net_player_id;
    uint8_t  gender;
    uint8_t  face;
    uint16_t cloth_item;
    uint8_t  sunburn_rank;
    uint8_t  is_custom_design;
    uint8_t  _reserved0[24];
    mNW_original_design_c design;  /* only meaningful when is_custom_design; zeroed otherwise */
} PCNetGameAppearanceMsg;
_Static_assert(sizeof(PCNetGameAppearanceMsg) == 576, "PCNetGameAppearanceMsg wire size drifted");
/* The one message with real headroom risk: at 576 of 1024 bytes it is already over half the
 * transport ceiling, so e.g. adding a second embedded design record would silently exceed it. */
_Static_assert(sizeof(PCNetGameAppearanceMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameAppearanceMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");
_Static_assert(sizeof(mNW_original_design_c) == PC_NETGAME_DESIGN_RECORD_SIZE,
              "mNW_original_design_c size no longer matches PC_NETGAME_DESIGN_RECORD_SIZE (pc_net_game.h)");

/* Stage 5A: client -> host. ut_x/ut_z are global field-tile coordinates (see
 * mFI_Wpos2UtNum()/mFI_UtNum2UtFG(), m_field_info.c) -- the same addressing the field's own
 * mActor_name_t grid already uses, not a world position (never trust a client's claimed float
 * position for this; the host re-derives the tile's true center itself when validating, see
 * pcnetgame_handle_host_pickup_request()). request_id is this connection's own monotonically
 * advancing counter (see s_next_pickup_request_id) -- meaningful only paired with the sender's
 * PCNetPeerId, never compared across different peers. */
typedef struct PCNetGamePickupRequestMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_PICKUP_REQUEST */
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint8_t  _reserved0;
    uint32_t request_id;
} PCNetGamePickupRequestMsg;
_Static_assert(sizeof(PCNetGamePickupRequestMsg) == 8, "PCNetGamePickupRequestMsg wire size drifted");
_Static_assert(sizeof(PCNetGamePickupRequestMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGamePickupRequestMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* Stage 5A: host -> the one requesting client. Deliberately carries no slot number -- the client
 * chooses its own free pocket slot via the real mPr_SetFreePossessionItem() on receipt (see
 * pcnetgame_handle_client_pickup_result()); the host never tracks a remote client's real pocket
 * contents (see the Stage 5A inventory-architecture audit). granted_item is the host's fully
 * resolved, authoritative item -- never the raw field value the client may have glimpsed, and
 * never a present/dummy sentinel (see pcnetgame_resolve_pickup_item()). Meaningless when
 * accepted == 0. */
typedef struct PCNetGamePickupResultMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_PICKUP_RESULT */
    uint8_t  accepted;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint32_t request_id;
    uint16_t granted_item;
    uint16_t _reserved0;
} PCNetGamePickupResultMsg;
_Static_assert(sizeof(PCNetGamePickupResultMsg) == 12, "PCNetGamePickupResultMsg wire size drifted");
_Static_assert(sizeof(PCNetGamePickupResultMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGamePickupResultMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* Stage 5A: host -> every READY client. The authoritative new value of one field tile -- always a
 * complete, self-contained fact (never a delta), so a receiver just overwrites its own local copy
 * (see pcnetgame_handle_client_field_update()/pcnetgame_apply_field_update_or_defer()). No
 * request_id: this is a statement about shared world state, not an answer to any one client's
 * request (every READY client receives the same message, including the original requester, who
 * also gets the separate, unicast PICKUP_RESULT above). */
typedef struct PCNetGameFieldUpdateMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_FIELD_UPDATE */
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint8_t  _reserved0;
    uint16_t new_fg_value;
    uint16_t _reserved1;
} PCNetGameFieldUpdateMsg;
_Static_assert(sizeof(PCNetGameFieldUpdateMsg) == 8, "PCNetGameFieldUpdateMsg wire size drifted");
_Static_assert(sizeof(PCNetGameFieldUpdateMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameFieldUpdateMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* Stage 5B-1: client -> host. Mirrors PCNetGamePickupRequestMsg's shape/conventions, with two
 * additions the Stage 5B protocol audit found required (see the Stage 5B audit's "Proposed Network
 * Protocol"): an explicit pocket slot index (inventory removal in this codebase is ALWAYS by slot,
 * never by item-id scan -- confirmed against ~90 mPr_SetPossessionItem call sites in
 * src/game/m_private.c) and a claimed_item.
 *
 * claimed_item is an EXPLICIT TRUST BOUNDARY: the host holds no shadow/mirror of any remote
 * player's Private_c/inventory (unchanged since the Stage 5A inventory-architecture audit) and
 * therefore cannot verify this field independently -- see pcnetgame_validate_and_resolve_drop()'s
 * own doc comment. The host only ever uses it as the value to place, and only after every check it
 * CAN perform has already passed. This is the same trust posture already accepted for movement sync
 * (no anti-cheat, Stage 3) -- not a gap introduced here.
 *
 * ut_x/ut_z: for Stage 5B-2 this must be exactly the ONE tile vanilla's own mTG_search_put_pos2()
 * would deterministically pick for the requester's last-synced position and facing -- the
 * requester's own current tile if legal, otherwise the first legal candidate in vanilla's 8-neighbor
 * zigzag search (src/game/m_tag_ovl.c:2740-2867). The host independently re-runs that exact search
 * (pcnetgame_resolve_vanilla_drop_tile()) against its own authoritative field state and requires an
 * EXACT match against that single answer (not a radius like pickup's reach check, and not "any
 * legal-looking neighbor" -- see pcnetgame_validate_and_resolve_drop()'s own doc for why). */
typedef struct PCNetGameDropRequestMsg {
    uint8_t  msg_type;         /* PC_NETGAME_MSG_DROP_REQUEST */
    uint8_t  pocket_slot_idx;  /* 0..mPr_POCKETS_SLOT_COUNT-1 */
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint16_t claimed_item;     /* TRUST BOUNDARY -- see doc above */
    uint16_t _reserved0;
    uint32_t request_id;
} PCNetGameDropRequestMsg;
_Static_assert(sizeof(PCNetGameDropRequestMsg) == 12, "PCNetGameDropRequestMsg wire size drifted");
_Static_assert(sizeof(PCNetGameDropRequestMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameDropRequestMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* Stage 5B-1: host -> the one requesting client. Mirrors PCNetGamePickupResultMsg's exact shape.
 * placed_item echoes what the host actually wrote -- normally identical to the request's
 * claimed_item, since (unlike pickup's granted_item) the host has no independent item truth to
 * resolve it against; still sent explicitly rather than leaving the client to assume its own claim
 * was honored verbatim, so the host remains the one stated authority for what is actually now on
 * the field. Meaningless when accepted == 0. */
typedef struct PCNetGameDropResultMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_DROP_RESULT */
    uint8_t  accepted;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint32_t request_id;
    uint16_t placed_item;
    uint16_t _reserved0;
} PCNetGameDropResultMsg;
_Static_assert(sizeof(PCNetGameDropResultMsg) == 12, "PCNetGameDropResultMsg wire size drifted");
_Static_assert(sizeof(PCNetGameDropResultMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameDropResultMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* Stage 5A: how close a requesting peer's own last-synced position must be to a target tile's
 * center. Vanilla's own reach check (Player_actor_Search_putin_item/CheckItemPosition_forPickup,
 * m_player_common.c_inc) tries hand-offset positions up to 35 world units in front of the player,
 * each then accepting a target within a further 15-unit radius -- so the farthest a legitimately
 * reachable tile's center can ever be from the player's own ROOT position is 35+15=50 units in the
 * colinear case. This deliberately validates against that generous envelope from the player's
 * root position, not vanilla's exact 3-offset hand-reach geometry, for two reasons: (1) the
 * "requester's position" available here is their last-synced movement snapshot, not their true
 * current position, so some slack is required anyway; (2) exactly replicating the hand-offset
 * search would require simulating facing/rotation math this file has no other reason to touch.
 * This is intentionally a coarse anti-cheat/sanity bound (catches "claims to be on the other side
 * of the map"), not a pixel-accurate reach check -- see the Stage 5A implementation report. */
#define PC_NETGAME_PICKUP_MAX_REACH_SQ (50.0f * 50.0f)
#define PC_NETGAME_PICKUP_MAX_REACH_Y 40.0f /* one field tile's worth (mFI_UNIT_BASE_SIZE) of
                                               vertical slack for terrain/sync staleness */

/* Stage 5B-1: vertical slack for the exact-tile-match check below -- see
 * pcnetgame_validate_and_resolve_drop()'s own doc for why this stage uses an EXACT tile-match
 * check (not a radius envelope like pickup's PC_NETGAME_PICKUP_MAX_REACH_SQ): vanilla's own drop
 * search (current tile, or Stage 5B-2's 8-neighbor zigzag) picks exactly one discrete tile, not a
 * fuzzy hand-reach area, and a radius sized around one tile width would sit ambiguously close to
 * the orthogonal-neighbor distance (exactly 40 units) --
 * discovered while designing this stage's own test coverage. This constant only guards against a
 * requester whose last-synced Y is wildly stale (e.g. a scene transition mid-flight), not normal
 * terrain height variation within one tile. */
#define PC_NETGAME_DROP_MAX_REACH_Y 40.0f

/* ---- module state ---- */

static PCNetGameRole s_role = PC_NETGAME_ROLE_NONE;

/* client-only */
static PCNetGameLinkState s_client_link = PC_NETGAME_LINK_DISCONNECTED;
static PCNetGameIdentity  s_client_host_identity;
static int                s_client_host_identity_valid = 0;

/* host-only: parallel to pc_net's own peer table (indexed by the same PCNetPeerId) */
static PCNetGameLinkState s_host_peer_link[PC_NET_MAX_PEERS];

/* Stage 3: movement send throttle. Decoupled from the render/frame rate on purpose -- see
 * graph_dt_period_elapsed()'s own doc (src/graph.c): it accumulates real elapsed
 * dt_num_60fps_frames, so this keeps firing at ~20Hz whether the game is running under 60fps,
 * over it (--no-framelimit), or with a stalling/variable dt. No new thread, no new timer. */
#define PC_NETGAME_MOVE_SEND_RATE_HZ 20.0f
#define PC_NETGAME_MOVE_SEND_PERIOD_60FPS_FRAMES (60.0f / PC_NETGAME_MOVE_SEND_RATE_HZ)
static float    s_move_send_accum = 0.0f;
static uint32_t s_local_move_send_counter = 0; /* this process's own per-send counter; see
                                                 * PCNetMoveMsg.frame's doc for why it never needs
                                                 * to relate to any other process's counter */

/* Stage 3 validation-pass diagnostic: logs the ACTUAL measured movement-sample rate once per
 * real second, independent of render/frame rate, so --no-framelimit / sub-60fps runs can be
 * checked directly against the intended ~20Hz instead of only trusting the constant above. Counts
 * "sampled locally and handed to pc_net_send()" once per fired throttle tick (not once per UDP
 * datagram -- the host fans one sampled tick out to every ready peer, which is a peer-count
 * multiplier, not a rate change). */
static float s_move_rate_log_accum = 0.0f;
static int   s_move_send_count_this_window = 0;
#define PC_NETGAME_MOVE_RATE_LOG_PERIOD_60FPS_FRAMES 60.0f /* ~1s */

/* Stage 4C-1 (one-shot-UDP-loss fix): low-frequency periodic appearance resend. PC_NET_RELIABLE
 * does not actually retransmit (see pc_net.h) -- appearance is otherwise sent only a handful of
 * times total (once at READY, once per newcomer backfill), so a single lost datagram would
 * otherwise leave a peer's appearance unresolved for the rest of the session with no recovery.
 * This is deliberately NOT a generic reliability layer: no ack, no sequence number, no per-message
 * retry/timeout bookkeeping -- just an unconditional, idempotent full resend every few seconds,
 * exactly like a coarse heartbeat. Whether or not the appearance actually changed since the last
 * resend, resending it is always safe -- a duplicate is byte-identical to what was already applied
 * and is safe to reapply (see pc_remote_player_on_appearance()'s doc comment: allocation-free,
 * fixed-size buffer overwrite). This remains the loss-recovery safety net even after Stage 4C-2
 * added immediate sends on detected change (see s_last_local_appearance below) -- an immediate
 * send can itself be lost, same as any other datagram. 3 seconds is conservative relative to
 * the 20Hz movement stream and the ~500ms transport heartbeat (PCNET_HEARTBEAT_INTERVAL_MS,
 * pc_net.c) -- frequent enough that a lost packet is corrected within a couple of seconds of
 * joining, infrequent enough that it never meaningfully adds to network load (worst case, 8
 * clients, is a handful of 576-byte sends every 3 seconds). */
#define PC_NETGAME_APPEARANCE_RESEND_PERIOD_60FPS_FRAMES 180.0f /* ~3s */
static float s_appearance_resend_accum = 0.0f;

/* Stage 4C-2: this process's own last-known-transmitted appearance, for detecting a local change
 * as soon as it happens (see pc_net_game_poll()) instead of waiting for the periodic resend above
 * (which remains unchanged and still runs as the loss-recovery safety net). s_last_local_appearance
 * is only meaningful once s_last_local_appearance_valid is set -- mirrors this file's own existing
 * s_client_host_identity_valid convention (a plain int flag, not a separate optional/maybe type). */
static PCNetPlayerAppearance s_last_local_appearance;
static int                   s_last_local_appearance_valid = 0;

/* Stage 5A: host-only, one slot per pc_net peer (same indexing as s_host_peer_link). This is
 * deliberately NOT a queue -- a player can only ever have one pickup animation in flight (the
 * decomp state machine itself serializes this; see m_player_main_pickup.c_inc), so "the most
 * recently PROCESSED request from this peer, and exactly what we decided" is all that's ever
 * needed. This is what makes a retry safe: if the same request_id arrives again (its original
 * PICKUP_RESULT was lost, not the request), the host must replay the SAME decision rather than
 * re-validating against now-changed ground truth (by the time a retry arrives, a successful
 * original attempt has already cleared the tile, so a naive re-check would wrongly see it as
 * already-empty and reject a request that actually succeeded the first time). Reset whenever the
 * peer disconnects AND defensively the moment a peer freshly reaches READY (see
 * pcnetgame_reset_host_pickup_state()) so a reused pc_net peer slot never inherits a previous,
 * unrelated connection's cached result. */
typedef struct PCNetGameHostPickupState {
    int      valid;         /* 0 until this peer's first PICKUP_REQUEST has been processed */
    uint32_t request_id;    /* the request_id this cached result answers */
    uint8_t  accepted;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint16_t granted_item;
} PCNetGameHostPickupState;
static PCNetGameHostPickupState s_host_pickup_state[PC_NET_MAX_PEERS];

/* Stage 5A: client-only. Exactly one outstanding pickup request at a time, for the same reason as
 * s_host_pickup_state above -- the local pickup animation state can't request a second one before
 * the first resolves. request_id is this connection's own counter, reset to 1 on every fresh
 * pc_net_game_start_client() (see there) so it can never collide with a previous, unrelated
 * connection's in-flight id on the host side. */
typedef struct PCNetGamePickupPending {
    int      valid;
    uint32_t request_id;
    uint8_t  ut_x;
    uint8_t  ut_z;
    float    timeout_accum;  /* frames-as-60fps-units since this attempt (send or last retry) --
                               * see PC_NETGAME_PICKUP_TIMEOUT_60FPS_FRAMES */
    int      retry_count;    /* number of retries already sent for this request_id */
} PCNetGamePickupPending;
static PCNetGamePickupPending s_pickup_pending;
static uint32_t               s_next_pickup_request_id = 1;

/* ~500ms: generous relative to a LAN round-trip (movement/appearance already prove typical
 * latency is well under this), but short enough that a genuinely lost request/result is retried
 * quickly rather than leaving the pickup animation waiting. 3 retries -> worst case ~2s of total
 * waiting before giving up, safely under pc_net.c's own 5s transport disconnect timeout, so a
 * pickup that can never succeed does not itself look like a dead connection. */
#define PC_NETGAME_PICKUP_TIMEOUT_60FPS_FRAMES 30.0f /* ~500ms */
#define PC_NETGAME_PICKUP_MAX_RETRIES 3

/* Stage 5A: client-only. A FIELD_UPDATE that arrived before the local field data was ready to
 * accept it (mFI_UtNumtoFGSet_common() returns FALSE -- see pcnetgame_apply_field_update_or_defer())
 * is retried here every frame once gamePT is available, mirroring the exact
 * gamePT-gate-plus-per-frame-retry pattern already proven for deferred appearance resolution (see
 * pc_remote_player.c's `pending_resolve`). A small fixed array, not a single slot: this is a
 * genuine authoritative fact about the shared world and must never be silently dropped (unlike a
 * movement/appearance sample, there is no periodic resend to fall back on for a one-shot
 * FIELD_UPDATE), so several tiles updated in quick succession while not yet ready must each be
 * preserved. Deduplicated by tile: a second deferred update for the same (ut_x, ut_z) overwrites
 * the first rather than growing the array, since only the latest value for a given tile is ever
 * meaningful. */
#define PC_NETGAME_PENDING_FIELD_UPDATE_MAX 8
typedef struct PCNetGamePendingFieldUpdate {
    int      valid;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint16_t new_fg_value;
} PCNetGamePendingFieldUpdate;
static PCNetGamePendingFieldUpdate s_pending_field_updates[PC_NETGAME_PENDING_FIELD_UPDATE_MAX];

/* Stage 5B-1: host-only, one slot per pc_net peer -- same purpose, shape, and reasoning as
 * s_host_pickup_state above (see its own doc comment): the most recently PROCESSED drop request
 * from this peer, and exactly what was decided, so a retry (original DROP_RESULT lost, not the
 * request) replays the SAME decision rather than re-validating against now-changed ground truth (a
 * successful original attempt has already occupied the tile, so a naive re-check would wrongly see
 * "no longer empty" and reject a request that actually succeeded the first time -- the mirror image
 * of pickup's own stale-recheck hazard). Kept entirely SEPARATE from s_host_pickup_state rather than
 * overloading it: a peer could have one pickup and one drop resolve in close succession, and
 * conflating the two caches would let a retry of one replay the other's decision. Reset on
 * disconnect and defensively on fresh READY, exactly mirroring pcnetgame_reset_host_pickup_state(). */
typedef struct PCNetGameHostDropState {
    int      valid;
    uint32_t request_id;
    uint8_t  accepted;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint16_t placed_item;
} PCNetGameHostDropState;
static PCNetGameHostDropState s_host_drop_state[PC_NET_MAX_PEERS];

/* Stage 5B-1: client-only. Exactly one outstanding drop request at a time, for the same reason as
 * s_pickup_pending -- kept as a wholly separate instance (not reused/overloaded) for the same
 * "don't conflate pickup and drop state" reason as s_host_drop_state above. claimed_item is stored
 * so a retry resends byte-for-byte the same request the host's dedup cache expects, rather than
 * relying on the host's replay path happening to ignore it. */
typedef struct PCNetGameDropPending {
    int      valid;
    uint32_t request_id;
    uint8_t  pocket_slot_idx;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint16_t claimed_item;
    float    timeout_accum;
    int      retry_count;
} PCNetGameDropPending;
static PCNetGameDropPending s_drop_pending;
static uint32_t             s_next_drop_request_id = 1;

#define PC_NETGAME_DROP_TIMEOUT_60FPS_FRAMES 30.0f /* ~500ms, mirrors pickup's own budget */
#define PC_NETGAME_DROP_MAX_RETRIES 3

static void pcnetgame_capture_local_identity(uint8_t* player_name, uint8_t* land_name, uint16_t* player_id,
                                              uint16_t* land_id, uint8_t* has_save) {
    /* Now_Private is NULL until a save slot is actually loaded (e.g. still at the title/select
     * screen when a handshake happens) -- never dereference it without checking first. */
    if (Now_Private != NULL) {
        memcpy(player_name, Now_Private->player_ID.player_name, PC_NETGAME_NAME_LEN);
        memcpy(land_name, Now_Private->player_ID.land_name, PC_NETGAME_LAND_LEN);
        *player_id = Now_Private->player_ID.player_id;
        *land_id = Now_Private->player_ID.land_id;
        *has_save = 1;
    } else {
        memset(player_name, 0, PC_NETGAME_NAME_LEN);
        memset(land_name, 0, PC_NETGAME_LAND_LEN);
        *player_id = 0;
        *land_id = 0;
        *has_save = 0;
    }
}

static void pcnetgame_build_identity_msg(PCNetGameIdentityMsg* msg) {
    memset(msg, 0, sizeof(*msg));
    msg->msg_type = (uint8_t)PC_NETGAME_MSG_IDENTITY;
    msg->protocol_version = PC_NETGAME_PROTOCOL_VERSION;
    pcnetgame_capture_local_identity(msg->player_name, msg->land_name, &msg->player_id, &msg->land_id,
                                      &msg->has_save);
}

/* Stage 4C-1/4C-2: read-only sample of the local player's current visible appearance, into the
 * decomp-independent PCNetPlayerAppearance shape (not the wire message directly) -- this is the
 * ONE place that ever reads Now_Private for this purpose. pcnetgame_build_appearance_msg() (the
 * one-shot/periodic-resend wire-message builder) and pc_net_game_poll()'s per-frame local-change
 * comparison (Stage 4C-2) both funnel through this exact function, so there is never a second,
 * subtly different place that decides what "the current local appearance" is. Never writes to
 * Now_Private/Private_c::my_org[] -- only ever copies out of them. If no save is loaded yet
 * (Now_Private == NULL, mirroring pcnetgame_capture_local_identity()'s own has_save=0 case),
 * yields a harmless all-zero placeholder (gender=mPr_SEX_MALE, face=0, no sunburn, catalog item
 * 0). */
static void pcnetgame_capture_local_appearance(PCNetPlayerAppearance* out) {
    memset(out, 0, sizeof(*out));

    if (Now_Private != NULL) {
        out->gender = (uint8_t)Now_Private->gender;
        out->face = (uint8_t)Now_Private->face;
        out->sunburn_rank = (uint8_t)Now_Private->sunburn.rank;
        out->cloth_item = (uint16_t)Now_Private->cloth.item;

        /* RSV_CLOTH is the sentinel Private_c::cloth.item carries when the worn shirt is one of
         * the player's own custom designs rather than a catalog item (see m_hand_ovl.c's
         * item==RSV_CLOTH branch) -- the same check the original game itself uses, not an
         * invented classification. cloth.idx (never sent -- meaningless on another machine) then
         * encodes which of the 8 my_org[] slots via CLOTH_NUM+1 + (slot & 7); see
         * mPlib_Get_PlayerTexRom_p(), src/game/m_player_lib.c. */
        if (Now_Private->cloth.item == RSV_CLOTH) {
            int org_idx = Now_Private->cloth.idx - (CLOTH_NUM + 1);
            if (!mPr_ORIGINAL_DESIGN_IDX_VALID(org_idx)) {
                org_idx = 0;
            }
            out->is_custom_design = 1;
            memcpy(out->design_record, &Now_Private->my_org[org_idx & 7], sizeof(out->design_record));
        }
    }
}

/* Stage 4C-1: unpack a received PCNetGameAppearanceMsg into the decomp-independent
 * PCNetPlayerAppearance pc_remote_player.c consumes. design_record is a raw byte copy of the
 * decomp mNW_original_design_c -- see PCNetPlayerAppearance's doc (pc_net_game.h). */
static void pcnetgame_appearance_msg_to_state(const PCNetGameAppearanceMsg* in, PCNetPlayerAppearance* out) {
    out->gender = in->gender;
    out->face = in->face;
    out->sunburn_rank = in->sunburn_rank;
    out->is_custom_design = in->is_custom_design;
    out->cloth_item = in->cloth_item;
    memcpy(out->design_record, &in->design, sizeof(out->design_record));
}

/* Stage 4C-1 (backfill/resend fix): the reverse of pcnetgame_appearance_msg_to_state() -- packs an
 * already-known PCNetPlayerAppearance (read back via pc_remote_player_get_appearance() for a peer
 * whose appearance the host already received, or freshly captured by pcnetgame_build_appearance_msg()
 * for this process's own appearance) into wire format, addressed to whichever net_player_id
 * actually owns it. Used by both the newcomer backfill pass and the periodic resend below -- no
 * new wire message type, no change to PCNetGameAppearanceMsg's layout/size. Same zero-init
 * discipline as pcnetgame_build_appearance_msg(): no uninitialized bytes ever go on the wire. */
static void pcnetgame_pack_appearance_msg(PCNetGameAppearanceMsg* msg, uint8_t net_player_id,
                                          const PCNetPlayerAppearance* state) {
    memset(msg, 0, sizeof(*msg));
    msg->msg_type = (uint8_t)PC_NETGAME_MSG_APPEARANCE;
    msg->net_player_id = net_player_id;
    msg->gender = state->gender;
    msg->face = state->face;
    msg->sunburn_rank = state->sunburn_rank;
    msg->is_custom_design = state->is_custom_design;
    msg->cloth_item = state->cloth_item;
    if (state->is_custom_design) {
        memcpy(&msg->design, state->design_record, sizeof(msg->design));
    }
}

/* Stage 4C-1: builds this process's own current appearance directly into wire format -- used for
 * the one-shot send at READY and the existing periodic full-roster resend. Stage 4C-2 additionally
 * reuses pcnetgame_capture_local_appearance() directly (not this wrapper) for its own per-frame
 * change comparison in pc_net_game_poll(), so this function and that comparison can never
 * disagree about what "the current local appearance" is -- there is exactly one capture routine. */
static void pcnetgame_build_appearance_msg(PCNetGameAppearanceMsg* msg, uint8_t net_player_id) {
    PCNetPlayerAppearance state;
    pcnetgame_capture_local_appearance(&state);
    pcnetgame_pack_appearance_msg(msg, net_player_id, &state);
}

static void pcnetgame_send_reject(PCNetPeerId peer, PCNetGameRejectReason reason) {
    PCNetGameRejectMsg msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_REJECT;
    msg.reason = (uint8_t)reason;
    msg.expected_protocol_version = PC_NETGAME_PROTOCOL_VERSION;
    pc_net_send(peer, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
}

/* Stage 3: classify the local player's real (~121-value) now_main_index into the small, PC-only
 * PCMoveState used on the wire. This is a one-way, read-only, best-effort lookup -- it never
 * attempts to run or reconstruct the real per-state action system (see pc_net_game.h's doc on
 * PCMoveState). States not explicitly listed fall through to PC_MOVE_STATE_OTHER; that is
 * intentional, not an oversight -- exhaustively categorizing all ~121 values has no real payoff
 * for what is currently only a cosmetic cue on the Stage 2 placeholder marker. */
static PCMoveState pcnetgame_classify_move_state(int now_main_index) {
    switch (now_main_index) {
        case mPlayer_INDEX_WAIT:
        case mPlayer_INDEX_WAIT_OPEN_FURNITURE:
        case mPlayer_INDEX_WAIT_BED:
        case mPlayer_INDEX_SITDOWN_WAIT:
        case mPlayer_INDEX_GIVE_WAIT:
        case mPlayer_INDEX_DEMO_WAIT:
        case mPlayer_INDEX_STANDUP:
        case mPlayer_INDEX_STANDUP_BED:
        case mPlayer_INDEX_SITDOWN:
        case mPlayer_INDEX_TIRED:
            return PC_MOVE_STATE_IDLE;

        case mPlayer_INDEX_WALK:
            return PC_MOVE_STATE_WALK;

        case mPlayer_INDEX_RUN:
            return PC_MOVE_STATE_RUN;

        case mPlayer_INDEX_DASH:
            return PC_MOVE_STATE_DASH;

        /* Stage 4B.1: previously folded into PC_MOVE_STATE_DASH above -- split out so remote
         * clients can play mPlayer_ANIM_RUN_SLIP1 + the skid sound instead of continuing DASH1
         * (see pc_remote_player.c's animation-selection switch). */
        case mPlayer_INDEX_TURN_DASH:
            return PC_MOVE_STATE_TURN_DASH;

        case mPlayer_INDEX_FALL:
        case mPlayer_INDEX_READY_PITFALL:
        case mPlayer_INDEX_FALL_PITFALL:
        case mPlayer_INDEX_STRUGGLE_PITFALL:
        case mPlayer_INDEX_CLIMBUP_PITFALL:
            return PC_MOVE_STATE_AIRBORNE;

        case mPlayer_INDEX_TUMBLE:
        case mPlayer_INDEX_TUMBLE_GETUP:
            return PC_MOVE_STATE_TUMBLE;

        case mPlayer_INDEX_SWING_AXE:
        case mPlayer_INDEX_AIR_AXE:
        case mPlayer_INDEX_REFLECT_AXE:
        case mPlayer_INDEX_BROKEN_AXE:
        case mPlayer_INDEX_SLIP_NET:
        case mPlayer_INDEX_READY_NET:
        case mPlayer_INDEX_READY_WALK_NET:
        case mPlayer_INDEX_SWING_NET:
        case mPlayer_INDEX_PULL_NET:
        case mPlayer_INDEX_STOP_NET:
        case mPlayer_INDEX_NOTICE_NET:
        case mPlayer_INDEX_PUTAWAY_NET:
        case mPlayer_INDEX_READY_ROD:
        case mPlayer_INDEX_CAST_ROD:
        case mPlayer_INDEX_AIR_ROD:
        case mPlayer_INDEX_RELAX_ROD:
        case mPlayer_INDEX_COLLECT_ROD:
        case mPlayer_INDEX_VIB_ROD:
        case mPlayer_INDEX_FLY_ROD:
        case mPlayer_INDEX_NOTICE_ROD:
        case mPlayer_INDEX_PUTAWAY_ROD:
        case mPlayer_INDEX_DIG_SCOOP:
        case mPlayer_INDEX_FILL_SCOOP:
        case mPlayer_INDEX_REFLECT_SCOOP:
        case mPlayer_INDEX_AIR_SCOOP:
        case mPlayer_INDEX_GET_SCOOP:
        case mPlayer_INDEX_PUTAWAY_SCOOP:
        case mPlayer_INDEX_PUTIN_SCOOP:
        case mPlayer_INDEX_PICKUP:
        case mPlayer_INDEX_PICKUP_JUMP:
        case mPlayer_INDEX_PICKUP_FURNITURE:
        case mPlayer_INDEX_PICKUP_EXCHANGE:
        case mPlayer_INDEX_TAKEOUT_ITEM:
        case mPlayer_INDEX_PUTIN_ITEM:
        case mPlayer_INDEX_SHAKE_TREE:
        case mPlayer_INDEX_THROW_MONEY:
        case mPlayer_INDEX_ROTATE_UMBRELLA:
        case mPlayer_INDEX_SWING_FAN:
        case mPlayer_INDEX_PUSH_SNOWBALL:
        case mPlayer_INDEX_WADE_SNOWBALL:
            return PC_MOVE_STATE_ITEM_USE;

        default:
            return PC_MOVE_STATE_OTHER;
    }
}

/* Stage 3 safety check, found necessary by testing (see the Stage 3 report's Limitations
 * section): gamePT becomes non-NULL the instant game_ct() mallocs and assigns the new game-class
 * object (src/game.c), but that object's *contents* -- for a GAME_PLAY, that includes
 * actor_info -- are only actually constructed afterwards, by that scene's own init callback
 * (Actor_info_ct(), called from src/game/m_play.c). There is therefore a real, observed window,
 * right around a scene transition, where gamePT != NULL but casting it to GAME_PLAY* and reading
 * ->actor_info reads memory that GAME_PLAY hasn't initialized yet -- in testing this returned a
 * non-NULL but garbage ACTOR* (a classic freed-heap poison pattern) from
 * get_player_actor_withoutCheck(), which then segfaulted on the first dereference.
 *
 * This project's own "low-address 64-bit" invariant (pc_lowaddr.h) gives a cheap, reliable way to
 * reject exactly this: every genuinely live, game-visible pointer is guaranteed to be below
 * PC_LOWADDR_LIMIT (4GB) by construction, and the garbage observed here was nowhere close. This
 * cannot produce a false negative against a real PLAYER_ACTOR, and closes the gap without
 * touching Actor_info_ct()/m_play.c/the scene-transition system at all -- it simply refuses to
 * trust a pointer (or any pointer reached through it) that the rest of this codebase's own
 * invariant already says cannot be real. */
static int pcnetgame_is_real_player_actor(PLAYER_ACTOR* local) {
    ACTOR_DLFTBL* dlftbl;

    if (local == NULL || (uint64_t)(uintptr_t)local >= PC_LOWADDR_LIMIT) {
        return 0;
    }
    dlftbl = local->actor_class.dlftbl;
    if (dlftbl == NULL || (uint64_t)(uintptr_t)dlftbl >= PC_LOWADDR_LIMIT) {
        return 0;
    }
    if (dlftbl->profile == NULL || (uint64_t)(uintptr_t)dlftbl->profile >= PC_LOWADDR_LIMIT) {
        return 0;
    }
    return dlftbl->profile->class_size >= sizeof(PLAYER_ACTOR);
}

/* Stage 3: read-only sample of the local player, for sending. Never writes to `local`. Caller
 * must have already verified pcnetgame_is_real_player_actor(local). */
static void pcnetgame_sample_local_move(PLAYER_ACTOR* local, PCNetMoveMsg* msg) {
    ACTOR* actor = &local->actor_class;

    memset(msg, 0, sizeof(*msg));
    msg->msg_type = (uint8_t)PC_NETGAME_MSG_MOVE;
    msg->net_player_id = 0; /* meaningless on send; the receiver decides what this means (see the
                              * PCNetMoveMsg doc comment) */
    msg->move_state = (uint8_t)pcnetgame_classify_move_state(local->now_main_index);
    msg->item_kind = (int8_t)local->item_kind;
    msg->frame = ++s_local_move_send_counter;
    msg->pos_x = actor->world.position.x;
    msg->pos_y = actor->world.position.y;
    msg->pos_z = actor->world.position.z;
    /* Stage 4B.1 fix: mPlayer_INDEX_TURN_DASH (the sprint sharp-turn/skid state) eases the
     * player's visual facing on shape_info.rotation.y every frame (Player_actor_ChangeDirection_Turn_dash(),
     * src/game/m_player_main_turn_dash.c_inc) but leaves world.angle.y frozen until the state
     * exits, where Player_actor_settle_main_Turn_dash() snaps it to match in one step (called once,
     * from the state-dispatch in src/game/m_player.c, only when leaving the state). Sending
     * world.angle.y during TURN_DASH therefore transmits a frozen value for the whole skid
     * followed by a single discontinuous jump, which Stage 3's (correct) interpolation then blends
     * across just one send interval -- reading as a near-instant snap instead of the original's
     * gradual turn. Every other state keeps these two fields in lockstep every frame (e.g.
     * m_player_main_walk.c_inc:155's `world.angle.y = shape_info.rotation.y = target`), so this
     * only changes behavior for TURN_DASH specifically. */
    msg->facing_angle = (local->now_main_index == mPlayer_INDEX_TURN_DASH) ? actor->shape_info.rotation.y
                                                                            : actor->world.angle.y;
    msg->speed = actor->speed;
}

static void pcnetgame_move_msg_to_sample(const PCNetMoveMsg* in, PCNetMoveSample* sample) {
    sample->sender_frame = in->frame;
    sample->pos_x = in->pos_x;
    sample->pos_y = in->pos_y;
    sample->pos_z = in->pos_z;
    sample->facing_angle = in->facing_angle;
    sample->speed = in->speed;
    sample->move_state = in->move_state;
    sample->item_kind = in->item_kind;
}

/* Host side: a client's movement. Only accepted from a peer that has already completed the
 * identity handshake (PC_NETGAME_LINK_READY) -- matches the existing "READY is earned, not
 * assumed" precedent used for IDENTITY. This is the only validation performed: packet
 * size/type (by the caller) and peer state here, plus stale/duplicate rejection inside
 * pc_remote_player_on_move(). No movement/physics validation, no anti-cheat -- explicitly out of
 * scope for Stage 3. */
static void pcnetgame_handle_host_move(PCNetPeerId peer, const PCNetMoveMsg* in) {
    PCNetMoveSample sample;
    int i;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }

    pcnetgame_move_msg_to_sample(in, &sample);
    pc_remote_player_on_move((PCNetPlayerId)peer, &sample); /* the host's own view of this client */

    /* Relay to every OTHER ready client, tagging net_player_id with the TRUE originating peer id
     * -- never the client-supplied (and here, ignored) value -- and never back to the sender. */
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (i != peer && s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
            PCNetMoveMsg out = *in;
            out.net_player_id = (uint8_t)peer;
            pc_net_send((PCNetPeerId)i, PC_NET_UNRELIABLE, &out, (uint16_t)sizeof(out));
        }
    }
}

/* Client side: a movement message from the host -- either the host's own movement
 * (net_player_id == PC_NETGAME_HOST_PLAYER_ID) or another client's, relayed. Both cases end up in
 * the same per-player-id tracking table; see pc_remote_player_on_move(). */
static void pcnetgame_handle_client_move(const PCNetMoveMsg* in) {
    PCNetMoveSample sample;
    pcnetgame_move_msg_to_sample(in, &sample);
    pc_remote_player_on_move((PCNetPlayerId)in->net_player_id, &sample);
}

/* Host side: a client's appearance -- same READY-gating and relay pattern as
 * pcnetgame_handle_host_move(), just for the (much rarer, reliable) appearance message instead of
 * the 20Hz movement stream. */
static void pcnetgame_handle_host_appearance(PCNetPeerId peer, const PCNetGameAppearanceMsg* in) {
    PCNetPlayerAppearance state;
    int i;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }

    pcnetgame_appearance_msg_to_state(in, &state);
    pc_remote_player_on_appearance((PCNetPlayerId)peer, &state); /* the host's own view of this client */

    /* Relay to every OTHER ready client, tagging net_player_id with the TRUE originating peer id
     * -- never back to the sender -- exactly like pcnetgame_handle_host_move()'s own relay. */
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (i != peer && s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
            PCNetGameAppearanceMsg out = *in;
            out.net_player_id = (uint8_t)peer;
            pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &out, (uint16_t)sizeof(out));
        }
    }
}

/* Host side (Stage 4C-1 backfill/resend fix): sends `dest` the host's own appearance plus the
 * last-known appearance of every OTHER currently-READY peer -- i.e. everything `dest` needs to
 * render every currently-visible player. Used both for the one-shot newcomer backfill (right after
 * a peer reaches READY) and for the periodic full-roster resend below; kept as one function so the
 * two call sites can never drift apart. Reads peer appearances back from pc_remote_player.c's own
 * canonical per-slot storage via pc_remote_player_get_appearance() -- no second appearance cache.
 * Skips `dest` itself and any peer that is not READY (disconnected or still mid-handshake), so a
 * disconnected player's old appearance can never be sent out by this function. */
static void pcnetgame_host_send_full_roster(PCNetPeerId dest) {
    PCNetGameAppearanceMsg amsg;
    int i;

    pcnetgame_build_appearance_msg(&amsg, (uint8_t)PC_NETGAME_HOST_PLAYER_ID);
    pc_net_send(dest, PC_NET_RELIABLE, &amsg, (uint16_t)sizeof(amsg));

    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        PCNetPlayerAppearance state;
        if (i == dest || s_host_peer_link[i] != PC_NETGAME_LINK_READY) {
            continue;
        }
        if (pc_remote_player_get_appearance((PCNetPlayerId)i, &state)) {
            PCNetGameAppearanceMsg out;
            pcnetgame_pack_appearance_msg(&out, (uint8_t)i, &state);
            pc_net_send(dest, PC_NET_RELIABLE, &out, (uint16_t)sizeof(out));
        }
    }
}

/* Client side: an appearance message from the host -- either the host's own appearance
 * (net_player_id == PC_NETGAME_HOST_PLAYER_ID) or another client's, relayed. Mirrors
 * pcnetgame_handle_client_move()'s exact pattern. */
static void pcnetgame_handle_client_appearance(const PCNetGameAppearanceMsg* in) {
    PCNetPlayerAppearance state;
    pcnetgame_appearance_msg_to_state(in, &state);
    pc_remote_player_on_appearance((PCNetPlayerId)in->net_player_id, &state);
}

/* Stage 5A: clears one peer's cached pickup dedup/replay state -- see s_host_pickup_state's own
 * doc comment for why this exists and why it must run on disconnect (a reused pc_net peer slot
 * must never answer a new connection's request with a previous, unrelated connection's cached
 * result). Safe to call for an out-of-range peer (no-op). */
static void pcnetgame_reset_host_pickup_state(PCNetPeerId peer) {
    if (peer < 0 || peer >= PC_NET_MAX_PEERS) {
        return;
    }
    memset(&s_host_pickup_state[peer], 0, sizeof(s_host_pickup_state[peer]));
}

/* Stage 5B-1: see s_host_drop_state's own doc comment for why this exists and why it must run on
 * disconnect and defensively on fresh READY -- exact mirror of pcnetgame_reset_host_pickup_state(),
 * kept separate for the same "don't conflate pickup and drop state" reason. Safe to call for an
 * out-of-range peer (no-op). */
static void pcnetgame_reset_host_drop_state(PCNetPeerId peer) {
    if (peer < 0 || peer >= PC_NET_MAX_PEERS) {
        return;
    }
    memset(&s_host_drop_state[peer], 0, sizeof(s_host_drop_state[peer]));
}

/* The ONE place every host-side per-peer cache gets cleared. It exists so the list of caches lives
 * in one spot instead of being copied at each call site. It has exactly two callers, and both must
 * stay in sync: the PC_NET_EVENT_PEER_DISCONNECTED handler in pc_net_game_poll() (the real reset,
 * so a reused pc_net peer slot never answers a new connection with the previous one's cached
 * results) and the IDENTITY-acceptance path in pcnetgame_handle_host_data() (the defensive
 * re-reset for a slot that somehow went READY again without this file seeing the disconnect).
 * Before this function, each of those sites called pcnetgame_reset_host_pickup_state() and
 * pcnetgame_reset_host_drop_state() directly. That meant every new per-peer cache had to be added
 * by hand at BOTH sites, and missing one would only show up later as a stale-cache replay bug on
 * a reused slot. A prior audit flagged that "remember N call sites" pattern as the design's weak
 * point. So a future feature that adds its own s_host_*_state[PC_NET_MAX_PEERS] cache (and its
 * own pcnetgame_reset_host_*_state()) adds one line HERE and nowhere else. Each per-feature reset
 * stays its own function, for the same "don't conflate pickup and drop state" reason
 * s_host_drop_state's doc gives. This is only the dispatch point. Safe to call for an
 * out-of-range peer, because every callee already treats that as a no-op. */
static void pcnetgame_reset_all_host_peer_state(PCNetPeerId peer) {
    pcnetgame_reset_host_pickup_state(peer); /* Stage 5A */
    pcnetgame_reset_host_drop_state(peer);   /* Stage 5B-1 */
}

/* Stage 5A: is `item` one of the wallet-crediting money-bag sentinels Setup_main_Pickup's own
 * PC_ENHANCEMENTS branch special-cases (m_player_main_pickup.c_inc)? Explicitly excluded from
 * this stage's network path -- see pc_net_game_request_pickup()'s doc and the Stage 5A
 * architecture audit ("do not invent wallet synchronization yet"). mPr_GetAmountForMoneyItem()
 * itself is only declared under PC_ENHANCEMENTS (m_private.h) -- in a build without it, money
 * bags are not wallet-special at all (Setup_main_Pickup's #else branch treats them as an
 * ordinary pocket item), so there is nothing to exclude in that configuration either. */
static int pcnetgame_is_money_bag_item(mActor_name_t item) {
#ifdef PC_ENHANCEMENTS
    return mPr_GetAmountForMoneyItem(item) > 0;
#else
    (void)item;
    return 0;
#endif
}

/* Stage 5A: mirrors mPr_SetPossessionItem()'s own sentinel switch (src/game/m_private.c) MINUS
 * the priv-writing side effects -- the host never holds a Private_c for a remote player (see the
 * Stage 5A inventory-architecture audit), so this resolves just the ITEM VALUE, authoritatively,
 * exactly once, here. The requesting client applies the result with mPr_ITEM_COND_NORMAL (see
 * pcnetgame_handle_client_pickup_result()) rather than the mPr_ITEM_COND_PRESENT the real function
 * would also set for these same sentinels -- a deliberate Stage 5A simplification: the granted
 * item arrives already resolved and immediately usable, not as a still-wrapped gift needing its
 * own local unwrap step. mPr_DummyPresentToTruePresent() includes genuine runtime randomness (a
 * chance of a random non-native fruit) -- calling it here, once, on the host, and sending only the
 * result is exactly why two clients (or the same client retried) can never disagree about what a
 * present resolved to. */
static mActor_name_t pcnetgame_resolve_pickup_item(mActor_name_t raw_item) {
    switch (raw_item) {
        case ITM_PRESENT:              return mPr_DummyPresentToTruePresent();
        case ITM_GOLDEN_NET_PRESENT:   return ITM_GOLDEN_NET;
        case ITM_GOLDEN_AXE_PRESENT:   return ITM_GOLDEN_AXE;
        case ITM_GOLDEN_SHOVEL_PRESENT: return ITM_GOLDEN_SHOVEL;
        case ITM_GOLDEN_ROD_PRESENT:   return ITM_GOLDEN_ROD;
        default:                       return raw_item;
    }
}

/* Stage 5A: does `item` classify as an "ordinary ground item" for this stage's pickup path?
 * Mirrors Player_actor_CheckItem_fromPosition()'s own classification (m_player_common.c_inc)
 * with two deliberate differences: (1) grass/weeds are rejected here even though that function
 * accepts them -- a grass tile routes to the entirely different mPlayer_INDEX_REMOVE_GRASS state
 * client-side (see m_player_common.c_inc's master trigger, Player_actor_CheckAndRequest_main_pickup_all),
 * which never reaches pc_net_game_request_pickup() in the first place, so this is pure defense in
 * depth against a request that could never legitimately arrive this way, not a real gameplay
 * restriction; (2) the original function's outdoor-only check for NAME_TYPE_FTR0/FTR1
 * (`Common_Get(field_type) == mFI_FIELDTYPE2_FG`) is intentionally NOT replicated -- that check
 * exists to distinguish an outdoor-grid lookup from an indoor room's separate layer-2 storage, but
 * every tile this function is ever asked about already came from mFI_UtNum2UtFG() (the primary,
 * outdoor-only grid -- see pcnetgame_validate_and_resolve_pickup()), so that distinction is
 * already structurally guaranteed by which grid was read, without needing any process's local
 * field_type (which, on the HOST, would describe the HOST's own current scene, not the requesting
 * client's -- exactly the kind of single-player-singleton assumption this stage must not carry
 * over uncritically). */
static int pcnetgame_is_pickupable_field_item(mActor_name_t item) {
    if (item == (mActor_name_t)EMPTY_NO || IS_ITEM_GRASS(item)) {
        return 0;
    }
    switch (ITEM_NAME_GET_TYPE(item)) {
        case NAME_TYPE_FTR0:
        case NAME_TYPE_FTR1:
        case NAME_TYPE_ITEM1:
            return 1;
        default:
            return ITEM_IS_SIGNBOARD(item) != 0;
    }
}

/* Stage 5B-1: does `item` classify as a plain, ordinary outdoor pocket item this stage supports
 * dropping? A narrow, evidence-based exclusion list rather than a broad guessed allowlist -- see
 * the Stage 5B audit's "Recommended Stage 5B Scope". Mirrors pcnetgame_is_pickupable_field_item()'s
 * own NAME_TYPE classification above -- confirmed symmetric during the Stage 5B-1 preflight: the
 * same FTR0/FTR1/ITEM1 types Stage 5A already treats as ordinary enough to pick up land via the
 * identical bare mFI_SetFG_common() write when dropped too (bIT_actor_drop_move_fly's ordinary
 * landing branch, src/bg_item/bg_item_common.c_inc:1758-1764) -- with two additional exclusions
 * beyond what pickup needed, each confirmed by reading the exact landing code, not guessed:
 *   - ITM_SIGNBOARD: mTG_common_throw_put_field() special-cases this into a completely different,
 *     two-phase actor (aSIGN_set_white_sign/src/actor/ac_sign.c) carrying design-pattern data this
 *     stage's protocol has no field for -- deferred, see the Stage 5B audit.
 *   - HONEYCOMB: bIT_actor_drop_move_fly's landing code (bg_item_common.c_inc:1755-1757) diverts
 *     this into a bee-attack event instead of a plain field placement -- placing it via a bare
 *     mFI_SetFG_common() would silently skip that event, a real behavioral divergence from vanilla,
 *     not merely a missing enhancement.
 * NAME_TYPE_ITEM0 ("Scenery items") is excluded by the switch's default case, matching pickup's own
 * classification -- these never legitimately reach a pocket in the first place (Stage 5A's own
 * pickup path already never grants one).
 *
 * Money bags are excluded too, by the same pcnetgame_is_money_bag_item() test that pickup already
 * uses to reject them (pcnetgame_validate_and_resolve_pickup()), so pickup and drop take the same
 * position. Without this, a money item would pass the NAME_TYPE_ITEM1 case below just like any
 * ordinary pocket item: the money bags (0x2100-0x2103, ITEM1_CAT_MONEY) and ITM_MONEY1000BELL
 * (0x250D, ITEM1_CAT_ETC) are the only entries mNT_get_itemTableNo() maps to mNT_ITEM_TYPE_BAG
 * (src/game/m_name_table.c:100,127). Note that a money bag CAN reach a pocket slot through
 * vanilla's own UI. The wallet's "make a sack" action (mTG_make_money_sack(), src/game/m_tag_ovl.c)
 * puts one in the hand for pocketing, and PC_ENHANCEMENTS' full-wallet pickup fallback
 * (m_player_main_pickup.c_inc) pockets one too. ITEM1_CAT_MONEY also gets the ordinary
 * mTG_TYPE_FIELD_DEFAULT tag menu, which includes "put on ground". So before this check, a
 * client's money-bag drop was accepted and placed as a plain field item. Stage 5A never made that
 * a deliberate path. It also leaves the bag stranded: no CLIENT can ever pick it back up, because
 * the client pickup seam skips money bags (m_player_main_pickup.c_inc:62-70) and
 * pcnetgame_validate_and_resolve_pickup() rejects them. Only the host's own local player could
 * retrieve it. Rejecting the drop instead gives the client vanilla's own local "can't place that"
 * feedback (the m_tag_ovl.c drop seam calls pc_net_game_is_droppable_item() before sending), and
 * the bag stays in their pocket. This affects only network CLIENTS. Single-player and the host's
 * own local drops never go through this classifier. It also means the ordinary-item field-value
 * protocol can never become a way to move money. Money sync needs its own wallet-aware protocol
 * (see pcnetgame_is_money_bag_item()'s doc), not this one.
 *
 * The mNT_check_unknown() guard is REQUIRED, not decoration. mPr_GetAmountForMoneyItem() goes
 * through mNT_get_itemTableNo(), which indexes item1_tableNo[category][idx] with no bounds check
 * (src/game/m_name_table.c:191-195). Some of those tables have only 2-4 entries, while idx can be
 * anything up to 0xFF. Pickup only ever asks about the host's own field value, so it never hits
 * this. Here, though, `item` is the requester's unverified claimed_item (see
 * PCNetGameDropRequestMsg's TRUST BOUNDARY doc), and an unguarded call would let any client cause
 * an out-of-bounds read on the host. mNT_check_unknown() (m_name_table.c:403) checks the index
 * against the per-category *_NUM limits, and those equal each table's real length. That makes
 * the lookup safe for every ITEM1 id it lets through. An ITEM1 id it rejects as out of range
 * cannot be a money bag anyway, so it falls through to the unchanged classification below. For
 * non-ITEM1 types, mNT_get_itemTableNo() never returns mNT_ITEM_TYPE_BAG, so limiting the check to
 * ITEM1 leaves nothing out. */
static int pcnetgame_is_droppable_item(mActor_name_t item) {
    if (item == (mActor_name_t)EMPTY_NO || item == ITM_SIGNBOARD || item == (mActor_name_t)HONEYCOMB) {
        return 0;
    }
    if (ITEM_IS_ITEM1(item) && !mNT_check_unknown(item) && pcnetgame_is_money_bag_item(item)) {
        return 0; /* money bag -- deliberately never droppable over the network, see doc above */
    }
    switch (ITEM_NAME_GET_TYPE(item)) {
        case NAME_TYPE_FTR0:
        case NAME_TYPE_FTR1:
        case NAME_TYPE_ITEM1:
            return 1;
        default:
            return 0;
    }
}

/* Stage 5B-1: public wrapper around pcnetgame_is_droppable_item() -- see pc_net_game.h's own doc.
 * Lets the decomp drop seam (m_tag_ovl.c) give the SAME immediate, synchronous "can't place that"
 * feedback vanilla's own failure path already gives for an unsupported item, rather than silently
 * sending a request the host is guaranteed to reject. Plain int, not mActor_name_t, to keep
 * pc_net_game.h decomp-independent (matching every other function in that header). */
int pc_net_game_is_droppable_item(int item) {
    return pcnetgame_is_droppable_item((mActor_name_t)item);
}

/* Stage 5A: every read-only validation step for one pickup request, in sequence, stopping at the
 * first failure -- never mutates anything, so pcnetgame_handle_host_pickup_request() can call this
 * freely without side effects. Returns 1 and fills *out_granted_item with the host's fully
 * resolved, authoritative item if this request should be granted; returns 0 (leaving
 * *out_granted_item untouched) otherwise. `peer` identifies whose last-synced position to validate
 * proximity against (see pc_remote_player_get_last_position()) -- never any claim from `in`
 * itself. */
static int pcnetgame_validate_and_resolve_pickup(PCNetPeerId peer, uint8_t ut_x, uint8_t ut_z,
                                                 mActor_name_t* out_granted_item) {
    mActor_name_t* fg_p;
    mActor_name_t raw_item;
    xyz_t center;
    float px, py, pz;

    if (mFI_UtNumCheck((int)ut_x, (int)ut_z, mFI_GetBlockXMax(), mFI_GetBlockZMax()) == FALSE) {
        return 0; /* out-of-range coordinate -- never trust it, whatever the client intended */
    }
    if (mFI_UtNum2DepositGet((int)ut_x, (int)ut_z)) {
        return 0; /* buried/reserved tile -- never an ordinary pickupable ground item */
    }

    fg_p = mFI_UtNum2UtFG((int)ut_x, (int)ut_z);
    if (fg_p == NULL) {
        return 0; /* out of range, or that acre's data is not currently resident on the host */
    }
    raw_item = *fg_p; /* the host's own authoritative read, taken now -- never the client's claim,
                        * and never cached from any earlier moment */

    if (!pcnetgame_is_pickupable_field_item(raw_item) || pcnetgame_is_money_bag_item(raw_item)) {
        return 0; /* wrong classification, or explicitly excluded for Stage 5A */
    }

    if (mFI_UtNum2CenterWpos(&center, (int)ut_x, (int)ut_z) == FALSE ||
        !pc_remote_player_get_last_position((PCNetPlayerId)peer, &px, &py, &pz)) {
        return 0; /* can't resolve the tile's center, or no movement sample from this peer yet */
    }
    /* mFI_UtNum2CenterWpos() only ever writes center.x/center.z -- it never touches center.y, which
       would otherwise be read uninitialized just below via `dy`. Seed it from the requester's own
       last-synced height so the vertical-distance check compares against a defined value instead of
       indeterminate stack memory, without changing what the check is actually guarding against. */
    center.y = py;
    {
        float dx = center.x - px;
        float dz = center.z - pz;
        float dy = center.y - py;
        if ((dx * dx + dz * dz) > PC_NETGAME_PICKUP_MAX_REACH_SQ || fabsf(dy) > PC_NETGAME_PICKUP_MAX_REACH_Y) {
            return 0; /* too far from this peer's last-known position -- see the reach macros' doc */
        }
    }

    *out_granted_item = pcnetgame_resolve_pickup_item(raw_item);
    return 1;
}

/* Stage 5B-2: verbatim reproduction of m_tag_ovl.c's own file-local mTG_SEARCH_BAD_ITEM() macro
 * (src/game/m_tag_ovl.c:2650-2737) -- the test mTG_search_put_pos2() applies to a DIAGONAL
 * candidate's orthogonal flanking tile (see pcnetgame_resolve_vanilla_drop_tile()). No header
 * exposes that macro, so it is reproduced here with the same membership; if it ever changes there
 * it must change here too. Note what it does NOT require: the flanking tile need not be empty --
 * only "not a tree / hole / shining hole / stump" (an ordinary dropped item on the flank does not
 * block a diagonal drop in vanilla, so it must not block one here either). */
static int pcnetgame_is_drop_search_bad_item(mActor_name_t item) {
    return item == TREE_S0 || item == TREE_S1 || item == TREE_S2 || item == TREE ||
           item == TREE_1000BELLS_S0 || item == TREE_1000BELLS_S1 || item == TREE_1000BELLS_S2 ||
           item == TREE_1000BELLS || item == TREE_10000BELLS_S0 || item == TREE_10000BELLS_S1 ||
           item == TREE_10000BELLS_S2 || item == TREE_10000BELLS || item == TREE_30000BELLS_S0 ||
           item == TREE_30000BELLS_S1 || item == TREE_30000BELLS_S2 || item == TREE_30000BELLS ||
           item == TREE_100BELLS_S0 || item == TREE_100BELLS_S1 || item == TREE_100BELLS_S2 ||
           item == TREE_100BELLS || item == CEDAR_TREE_S0 || item == CEDAR_TREE_S1 || item == CEDAR_TREE_S2 ||
           item == CEDAR_TREE || item == GOLD_TREE_S0 || item == GOLD_TREE_S1 || item == GOLD_TREE_S2 ||
           item == GOLD_TREE || item == GOLD_TREE_SHOVEL || item == TREE_APPLE_S0 || item == TREE_APPLE_S1 ||
           item == TREE_APPLE_S2 || item == TREE_APPLE_NOFRUIT_0 || item == TREE_APPLE_NOFRUIT_1 ||
           item == TREE_APPLE_NOFRUIT_2 || item == TREE_APPLE_FRUIT || item == TREE_ORANGE_S0 ||
           item == TREE_ORANGE_S1 || item == TREE_ORANGE_S2 || item == TREE_ORANGE_NOFRUIT_0 ||
           item == TREE_ORANGE_NOFRUIT_1 || item == TREE_ORANGE_NOFRUIT_2 || item == TREE_ORANGE_FRUIT ||
           item == TREE_PEACH_S0 || item == TREE_PEACH_S1 || item == TREE_PEACH_S2 ||
           item == TREE_PEACH_NOFRUIT_0 || item == TREE_PEACH_NOFRUIT_1 || item == TREE_PEACH_NOFRUIT_2 ||
           item == TREE_PEACH_FRUIT || item == TREE_PEAR_S0 || item == TREE_PEAR_S1 || item == TREE_PEAR_S2 ||
           item == TREE_PEAR_NOFRUIT_0 || item == TREE_PEAR_NOFRUIT_1 || item == TREE_PEAR_NOFRUIT_2 ||
           item == TREE_PEAR_FRUIT || item == TREE_CHERRY_S0 || item == TREE_CHERRY_S1 ||
           item == TREE_CHERRY_S2 || item == TREE_CHERRY_NOFRUIT_0 || item == TREE_CHERRY_NOFRUIT_1 ||
           item == TREE_CHERRY_NOFRUIT_2 || item == TREE_CHERRY_FRUIT || item == TREE_PALM_S0 ||
           item == TREE_PALM_S1 || item == TREE_PALM_S2 || item == TREE_PALM_NOFRUIT_0 ||
           item == TREE_PALM_NOFRUIT_1 || item == TREE_PALM_NOFRUIT_2 || item == TREE_PALM_FRUIT ||
           item == TREE_BEES || item == TREE_FTR || item == TREE_LIGHTS || item == TREE_PRESENT ||
           item == TREE_BELLS || item == CEDAR_TREE_BELLS || item == CEDAR_TREE_FTR || item == CEDAR_TREE_BEES ||
           item == GOLD_TREE_BELLS || item == GOLD_TREE_FTR || item == GOLD_TREE_BEES ||
           item == CEDAR_TREE_LIGHTS || ITEM_IS_HOLE(item) || item == HOLE_SHINE || IS_ITEM_TREE_STUMP(item);
}

/* Stage 5B-2: exact reproduction of mTG_check_wall_put_pos() (src/game/m_tag_ovl.c:2599-2612) --
 * same real decomp mCoBG_VirtualBGCheck() call, same argument list, same "either wall bit"
 * interpretation. Returns nonzero if a wall lies between the two points. mCoBG_VirtualBGCheck()
 * only uses file-static scratch state inside m_collision_bg.c that it fully re-initializes per
 * call -- the vanilla menu code calls it at an arbitrary point in the frame the same way, so
 * calling it from pc_net_game_poll()'s main-thread dispatch is equally safe. */
static int pcnetgame_drop_wall_between(const xyz_t* start_pos_p, const xyz_t* end_pos_p) {
    mCoBG_Check_c bg_check;

    bg_check.result.hit_wall = 0;
    bg_check.result.hit_attribute_wall = 0;
    mCoBG_VirtualBGCheck(NULL, &bg_check, start_pos_p, end_pos_p, 0, FALSE, TRUE, 10.0f, -20.0f, 1, 1, 0);
    return bg_check.result.hit_wall != 0 || bg_check.result.hit_attribute_wall != 0;
}

/* Stage 5B-2: host-side reproduction of the ONE tile vanilla's drop search picks for a player at
 * (px,py,pz) facing angle_y -- i.e. mTG_search_put_pos2() (src/game/m_tag_ovl.c:2740-2867)
 * specialized to exactly the argument set every non-signboard drop uses, client seam included
 * (mTG_search_put_pos(player, &pos, FALSE, FALSE, FALSE, FALSE, FALSE) -> plant_flag=FALSE,
 * param_4=0, num_pos=1, param_6=0, unit_flag=FALSE, param_8=FALSE). Under those arguments
 * mTG_put_place_check() is exactly mCoBG_CheckPlace() and mTG_check_pos_slope() never runs.
 *
 * Deterministic and first-match-wins, exactly like vanilla with num_pos == 1:
 *   1. the player's own tile, if its FG is EMPTY_NO and mCoBG_CheckPlace() passes -- no wall
 *      check for this one (vanilla has none);
 *   2. otherwise the 8 neighbors in vanilla's zigzag around the facing octant: f, f+1, f-1, f+2,
 *      f-2, f+3, f-3, f+4 (mod 8). Each must pass the same FG/EMPTY_NO/CheckPlace test, then:
 *        - orthogonal (even index): no wall on the sweep player(y-20) -> candidate;
 *        - diagonal (odd index): NO direct sweep; instead a flanking orthogonal tile (unit+1
 *          first, then unit-1) whose FG exists and is not pcnetgame_is_drop_search_bad_item(), with
 *          no wall on player(y-20) -> flank AND flank -> candidate.
 * The octant is bucketed from angle_y with the same DEG2SHORT_ANGLE2() thresholds vanilla uses
 * against shape_info.rotation.y (m_tag_ovl.c:2782-2800). NOTE: vanilla buckets its OWN
 * shape_info.rotation.y, not world.angle.y -- the two are equal in every state a player can open
 * the drop menu from, but the wire's PCNetMoveMsg.facing_angle carries world.angle.y (except
 * during TURN_DASH, where it carries rotation.y -- see pcnetgame_sample_local_move()). Any
 * momentary divergence between the two can only make this function fail to find the tile vanilla
 * would have picked (a false REJECTION of a legitimate drop), never accept a tile vanilla would
 * not have picked -- see pcnetgame_validate_and_resolve_drop()'s own doc.
 *
 * Returns 1 and fills *out_ut_x/*out_ut_z with the chosen tile, or 0 if vanilla would find no tile
 * at all. Fails closed (returns 0) where vanilla would merely carry on with an off-field position
 * (mFI_Wpos2UtCenterWpos() failing) -- a legitimate player can never be there. Reads only; never
 * mutates field or collision state. */
static int pcnetgame_resolve_vanilla_drop_tile(float px, float py, float pz, int16_t angle_y, int* out_ut_x,
                                               int* out_ut_z) {
    /* Verbatim copy of mTG_search_put_pos2()'s own offset_pos table (m_tag_ovl.c:2743-2752). */
    static const f32 offset_pos[8][2] = {
        {                  0.0f,  mFI_UT_WORLDSIZE_Z_F },
        {  mFI_UT_WORLDSIZE_X_F,  mFI_UT_WORLDSIZE_Z_F },
        {  mFI_UT_WORLDSIZE_X_F,                  0.0f },
        {  mFI_UT_WORLDSIZE_X_F, -mFI_UT_WORLDSIZE_Z_F },
        {                  0.0f, -mFI_UT_WORLDSIZE_Z_F },
        { -mFI_UT_WORLDSIZE_X_F, -mFI_UT_WORLDSIZE_Z_F },
        { -mFI_UT_WORLDSIZE_X_F,                  0.0f },
        { -mFI_UT_WORLDSIZE_X_F,  mFI_UT_WORLDSIZE_Z_F },
    };
    xyz_t player_pos;
    xyz_t center_pos;
    xyz_t start_pos;
    mActor_name_t* fg_p;
    int unit;
    int i;

    if (gamePT == NULL) {
        return 0; /* no live play scene -- vanilla's search only ever runs inside one */
    }

    player_pos.x = px;
    player_pos.y = py;
    player_pos.z = pz;

    if (!mFI_Wpos2UtCenterWpos(&center_pos, player_pos)) {
        return 0;
    }
    center_pos.y = mCoBG_Wpos2BgUtCenterHeight_AddColumn(center_pos);

    /* 1. Own tile (m_tag_ovl.c:2773-2780). */
    fg_p = mFI_GetUnitFG(center_pos);
    if (fg_p != NULL && *fg_p == (mActor_name_t)EMPTY_NO && mCoBG_CheckPlace(center_pos)) {
        return mFI_Wpos2UtNum(out_ut_x, out_ut_z, center_pos);
    }

    /* Facing octant (m_tag_ovl.c:2784-2800) -- same thresholds, same order, same boundary sides. */
    if (angle_y > DEG2SHORT_ANGLE2(157.5f) || angle_y <= DEG2SHORT_ANGLE2(-157.5f)) {
        unit = 4;
    } else if (angle_y > DEG2SHORT_ANGLE2(112.5f)) {
        unit = 3;
    } else if (angle_y > DEG2SHORT_ANGLE2(67.5f)) {
        unit = 2;
    } else if (angle_y > DEG2SHORT_ANGLE2(22.5f)) {
        unit = 1;
    } else if (angle_y > DEG2SHORT_ANGLE2(-22.5f)) {
        unit = 0;
    } else if (angle_y > DEG2SHORT_ANGLE2(-67.5f)) {
        unit = 7;
    } else if (angle_y > DEG2SHORT_ANGLE2(-112.5f)) {
        unit = 6;
    } else {
        unit = 5;
    }

    start_pos = player_pos;
    start_pos.y -= 20.0f; /* m_tag_ovl.c:2820-2821 */

    /* 2. Zigzag (m_tag_ovl.c:2803-2864). max == 8 because param_8 == FALSE. */
    for (i = 0; i < 8; i++) {
        xyz_t pos;

        if (i & 1) {
            unit += i;
        } else {
            unit -= i;
        }
        if (unit < 0) { /* mTG_check_direction_put_pos(), m_tag_ovl.c:2591-2597 */
            unit += 8;
        } else if (unit >= 8) {
            unit -= 8;
        }

        pos.x = center_pos.x + offset_pos[unit][0];
        pos.z = center_pos.z + offset_pos[unit][1];
        pos.y = mCoBG_Wpos2BgUtCenterHeight_AddColumn(pos);

        fg_p = mFI_GetUnitFG(pos);
        if (fg_p == NULL || *fg_p != (mActor_name_t)EMPTY_NO || !mCoBG_CheckPlace(pos)) {
            continue;
        }

        if (unit & 1) {
            /* Diagonal: flank unit+1 first, then unit-1 (m_tag_ovl.c:2823-2855), same
               short-circuit order as vanilla. */
            int side;
            for (side = 0; side < 2; side++) {
                int flank = (side == 0) ? (unit + 1) : (unit - 1);
                xyz_t flank_pos;
                mActor_name_t* flank_fg_p;

                if (flank < 0) {
                    flank += 8;
                } else if (flank >= 8) {
                    flank -= 8;
                }
                flank_pos.x = center_pos.x + offset_pos[flank][0];
                flank_pos.z = center_pos.z + offset_pos[flank][1];
                flank_pos.y = mCoBG_Wpos2BgUtCenterHeight_AddColumn(flank_pos);

                flank_fg_p = mFI_GetUnitFG(flank_pos);
                if (flank_fg_p != NULL && !pcnetgame_is_drop_search_bad_item(*flank_fg_p) &&
                    !pcnetgame_drop_wall_between(&start_pos, &flank_pos) &&
                    !pcnetgame_drop_wall_between(&flank_pos, &pos)) {
                    return mFI_Wpos2UtNum(out_ut_x, out_ut_z, pos);
                }
            }
        } else if (!pcnetgame_drop_wall_between(&start_pos, &pos)) {
            /* Orthogonal (m_tag_ovl.c:2856-2862). */
            return mFI_Wpos2UtNum(out_ut_x, out_ut_z, pos);
        }
    }

    return 0;
}

/* Stage 5B-1/5B-2: every HOST-VERIFIABLE check for one drop request, in sequence, stopping at the
 * first failure -- never mutates anything (mirrors pcnetgame_validate_and_resolve_pickup()'s own
 * discipline exactly). `peer` identifies whose last-synced position/facing to validate against
 * (see pc_remote_player_get_last_position()/pc_remote_player_get_last_facing_angle()) -- never any
 * claim from `in` itself.
 *
 * TRUST BOUNDARY (see PCNetGameDropRequestMsg's own doc and the Stage 5B audit's
 * "Inventory/Authority Architecture"): this function has NO WAY to verify in->pocket_slot_idx or
 * in->claimed_item actually reflect the requester's real Private_c -- the host holds no
 * shadow/mirror of any remote inventory (unchanged since Stage 5A) and this stage deliberately does
 * not introduce one. Every check below is a check this function CAN actually perform independently
 * (bounds, tile emptiness, terrain legality, current-tile-or-neighbor-tile reach, item
 * classification, and -- via the caller's dedup cache -- duplicate/replay detection); slot/item
 * POSSESSION is deliberately not one of them, and this function must never be extended to pretend
 * otherwise.
 *
 * Reach check (Stage 5B-2): vanilla's drop search (mTG_search_put_pos2(), src/game/m_tag_ovl.c)
 * is fully deterministic -- for one player position + facing + field state it picks exactly one
 * tile: the player's own tile if legal, else the first legal neighbor in a zigzag around the facing
 * octant (with wall-sweep and diagonal-flank checks). The host re-runs that exact search itself
 * (pcnetgame_resolve_vanilla_drop_tile()) from the requester's own last-synced position and facing
 * against its OWN authoritative field, and requires (in->ut_x, in->ut_z) to EQUAL that single
 * answer -- not merely to be one of the up-to-9 tiles vanilla could ever consider. The client's
 * choice is therefore never trusted, only checked. Any host/client disagreement (stale sync, an
 * in-flight FIELD_UPDATE, host-local transient collision) can only cause a false REJECTION, never
 * an acceptance of a tile vanilla would not have chosen from the synced state. */
static int pcnetgame_validate_and_resolve_drop(PCNetPeerId peer, const PCNetGameDropRequestMsg* in,
                                               mActor_name_t* out_placed_item) {
    mActor_name_t* fg_p;
    xyz_t center;
    float px, py, pz;
    int16_t requester_facing;
    int requester_ux, requester_uz; /* Stage 5B-2: now holds vanilla's resolved tile, not the
                                       requester's own tile */

    if (in->pocket_slot_idx >= mPr_POCKETS_SLOT_COUNT) {
        return 0; /* not a real pocket slot -- never trust it */
    }
    if (!pcnetgame_is_droppable_item((mActor_name_t)in->claimed_item)) {
        return 0; /* outside Stage 5B-1's supported item classification */
    }
    if (mFI_UtNumCheck((int)in->ut_x, (int)in->ut_z, mFI_GetBlockXMax(), mFI_GetBlockZMax()) == FALSE) {
        return 0; /* out-of-range coordinate -- never trust it, whatever the client intended */
    }
    if (mFI_UtNum2DepositGet((int)in->ut_x, (int)in->ut_z)) {
        return 0; /* buried/reserved tile -- never a legal plain-drop target this stage (bury is
                     deferred, see the Stage 5B audit) */
    }

    fg_p = mFI_UtNum2UtFG((int)in->ut_x, (int)in->ut_z);
    if (fg_p == NULL || *fg_p != (mActor_name_t)EMPTY_NO) {
        return 0; /* out of range, that acre isn't resident, or -- the common case -- already
                     occupied (by another item, or RSV_NO/a hole sentinel) */
    }

    if (mFI_UtNum2CenterWpos(&center, (int)in->ut_x, (int)in->ut_z) == FALSE) {
        return 0; /* can't resolve the tile's center */
    }
    if (mCoBG_CheckPlace(center) == FALSE) {
        return 0; /* illegal terrain -- water/river/sea/waterfall/diagonal cliff-bridge-bank
                     corners, mirroring mTG_put_place_check()'s own mCoBG_CheckPlace() call */
    }

    if (!pc_remote_player_get_last_position((PCNetPlayerId)peer, &px, &py, &pz)) {
        return 0; /* no movement sample from this peer yet */
    }
    /* mFI_UtNum2CenterWpos() (see its own doc, m_field_info.c) only ever writes center.x/center.z --
       it never touches center.y, which would otherwise be read uninitialized by the vertical-distance
       check just below. Seed it from the requester's own last-synced height so that check compares
       against a defined value instead of indeterminate stack memory, without changing what the check
       is actually guarding against (a wildly stale vertical position, e.g. mid scene-transition). */
    center.y = py;
    if (fabsf(center.y - py) > PC_NETGAME_DROP_MAX_REACH_Y) {
        return 0; /* wildly stale vertical position (e.g. mid scene-transition) */
    }

    /* Stage 5B-2: read from the SAME newest snapshot pc_remote_player_get_last_position() just read
       -- both accessors index the same slot's newest snapshot, and nothing between the two calls
       can advance that snapshot (this whole function is one synchronous step inside
       pc_net_game_poll()'s event dispatch), so position and facing are always one coherent sample. */
    if (!pc_remote_player_get_last_facing_angle((PCNetPlayerId)peer, &requester_facing)) {
        return 0; /* no movement sample from this peer yet */
    }
    if (!pcnetgame_resolve_vanilla_drop_tile(px, py, pz, requester_facing, &requester_ux, &requester_uz)) {
        return 0; /* vanilla's own search finds no legal tile at all for this position/facing */
    }
    if (requester_ux != (int)in->ut_x || requester_uz != (int)in->ut_z) {
        return 0; /* not THE tile vanilla would pick -- see this function's own doc: vanilla is
                     deterministic, so "some legal-looking neighbor" is not good enough */
    }

    *out_placed_item = (mActor_name_t)in->claimed_item;
    return 1;
}

/* Host side: broadcasts a FIELD_UPDATE for one tile to every currently-READY client. The single
 * choke point for this message -- used both when a client's own PICKUP_REQUEST is accepted
 * (pcnetgame_handle_host_pickup_request()) and when the HOST's OWN local pickup mutates the field
 * (pc_net_game_notify_local_field_pickup(), Stage 5A.1) -- so the two paths can never disagree
 * about what a "field update broadcast" looks like on the wire. Never sends to the host itself
 * (there is no such peer -- see pc_net_send()'s own semantics): the host already applied this
 * mutation directly, synchronously, before calling this, so there is nothing to loop back. */
static void pcnetgame_broadcast_field_update(uint8_t ut_x, uint8_t ut_z, uint16_t new_fg_value) {
    int i;
    PCNetGameFieldUpdateMsg fu;
    memset(&fu, 0, sizeof(fu));
    fu.msg_type = (uint8_t)PC_NETGAME_MSG_FIELD_UPDATE;
    fu.ut_x = ut_x;
    fu.ut_z = ut_z;
    fu.new_fg_value = new_fg_value;
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
            pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &fu, (uint16_t)sizeof(fu));
        }
    }
}

/* Host side: a client's pickup request. Deliberately NOT Player_actor_setup_main_Pickup() run on
 * the remote player's behalf -- this never touches any ACTOR, any PLAYER_ACTOR, or Now_Private
 * (the host's OWN save is never read or written by another player's pickup); it only reads/writes
 * the shared field grid and sends back small, self-contained network messages. See the Stage 5A
 * inventory-architecture audit for why this is deliberately not a full remote-player
 * Private_c/inventory simulation. */
static void pcnetgame_handle_host_pickup_request(PCNetPeerId peer, const PCNetGamePickupRequestMsg* in) {
    PCNetGameHostPickupState* cache;
    PCNetGamePickupResultMsg out;
    mActor_name_t granted_item = (mActor_name_t)EMPTY_NO;
    int accepted;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return; /* not a known, handshake-complete peer -- never process on their behalf */
    }

    cache = &s_host_pickup_state[peer];

    if (cache->valid && cache->request_id == in->request_id) {
        /* Duplicate/retry: the client's original PICKUP_RESULT was lost, not its request -- reply
         * with the SAME decision rather than re-validating against now-changed ground truth (by
         * now, a successful original attempt has already cleared the tile, so a fresh re-check
         * would wrongly see "already empty" and reject a request that actually succeeded). See
         * s_host_pickup_state's own doc comment. */
        memset(&out, 0, sizeof(out));
        out.msg_type = (uint8_t)PC_NETGAME_MSG_PICKUP_RESULT;
        out.accepted = cache->accepted;
        out.ut_x = cache->ut_x;
        out.ut_z = cache->ut_z;
        out.request_id = cache->request_id;
        out.granted_item = cache->granted_item;
        pc_net_send(peer, PC_NET_RELIABLE, &out, (uint16_t)sizeof(out));
        return;
    }

    accepted = pcnetgame_validate_and_resolve_pickup(peer, in->ut_x, in->ut_z, &granted_item);
    if (accepted) {
        /* The one authoritative mutation -- the exact same underlying grid write
         * Player_actor_putin_item() (m_player_common.c_inc) already performs for a local pickup,
         * just addressed by tile coordinate instead of world position since that is what the wire
         * already carries (see mFI_UtNumtoFGSet_common(), src/game/m_field_info.c). */
        mFI_UtNumtoFGSet_common((mActor_name_t)EMPTY_NO, (int)in->ut_x, (int)in->ut_z, TRUE);
    }

    cache->valid = 1;
    cache->request_id = in->request_id;
    cache->accepted = (uint8_t)accepted;
    cache->ut_x = in->ut_x;
    cache->ut_z = in->ut_z;
    cache->granted_item = (uint16_t)granted_item;

    memset(&out, 0, sizeof(out));
    out.msg_type = (uint8_t)PC_NETGAME_MSG_PICKUP_RESULT;
    out.accepted = (uint8_t)accepted;
    out.ut_x = in->ut_x;
    out.ut_z = in->ut_z;
    out.request_id = in->request_id;
    out.granted_item = (uint16_t)granted_item;
    pc_net_send(peer, PC_NET_RELIABLE, &out, (uint16_t)sizeof(out));

    if (accepted) {
        pcnetgame_broadcast_field_update(in->ut_x, in->ut_z, (uint16_t)EMPTY_NO);
    }
}

/* Host side: a client's drop request. Exact structural mirror of
 * pcnetgame_handle_host_pickup_request() (dedup-cache replay, then validate-and-mutate-and-reply,
 * then broadcast) -- see that function's own doc for the shared reasoning. Never touches any ACTOR,
 * any PLAYER_ACTOR, or Now_Private; only the shared field grid and small, self-contained network
 * messages. */
static void pcnetgame_handle_host_drop_request(PCNetPeerId peer, const PCNetGameDropRequestMsg* in) {
    PCNetGameHostDropState* cache;
    PCNetGameDropResultMsg out;
    mActor_name_t placed_item = (mActor_name_t)EMPTY_NO;
    int accepted;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return; /* not a known, handshake-complete peer -- never process on their behalf */
    }

    cache = &s_host_drop_state[peer];

    if (cache->valid && cache->request_id == in->request_id) {
        /* Duplicate/retry: replay the SAME decision rather than re-validating against now-changed
           ground truth -- see s_host_drop_state's own doc comment for why this matters even more
           for drop than for pickup (a stale re-check here would see the request's OWN successful
           mutation and wrongly reject it as "no longer empty"). */
        memset(&out, 0, sizeof(out));
        out.msg_type = (uint8_t)PC_NETGAME_MSG_DROP_RESULT;
        out.accepted = cache->accepted;
        out.ut_x = cache->ut_x;
        out.ut_z = cache->ut_z;
        out.request_id = cache->request_id;
        out.placed_item = cache->placed_item;
        pc_net_send(peer, PC_NET_RELIABLE, &out, (uint16_t)sizeof(out));
        return;
    }

    accepted = pcnetgame_validate_and_resolve_drop(peer, in, &placed_item);
    if (accepted) {
        /* The one authoritative mutation. Deliberately a direct mFI_UtNumtoFGSet_common() write,
           NOT a reproduction of vanilla's local ~14-26+ frame physics-drop actor -- confirmed safe
           during the Stage 5B-1 preflight: for the ordinary-item subset this function's own
           validation already guarantees (never a hole target -- see the deposit/EMPTY_NO checks
           above), that actor's only authoritative effect at landing is this exact same bare write
           (bIT_actor_drop_move_fly's ordinary landing branch, bg_item_common.c_inc:1758-1764); the
           RSV_NO reservation and the arc animation are transient, local-only presentation state
           with no persistent effect once the (bypassed, on a network client) animation would have
           completed. See the Stage 5B audit's "Proposed Network Protocol"/"Host-Local Path". */
        mFI_UtNumtoFGSet_common(placed_item, (int)in->ut_x, (int)in->ut_z, TRUE);
    }

    cache->valid = 1;
    cache->request_id = in->request_id;
    cache->accepted = (uint8_t)accepted;
    cache->ut_x = in->ut_x;
    cache->ut_z = in->ut_z;
    cache->placed_item = (uint16_t)placed_item;

    memset(&out, 0, sizeof(out));
    out.msg_type = (uint8_t)PC_NETGAME_MSG_DROP_RESULT;
    out.accepted = (uint8_t)accepted;
    out.ut_x = in->ut_x;
    out.ut_z = in->ut_z;
    out.request_id = in->request_id;
    out.placed_item = (uint16_t)placed_item;
    pc_net_send(peer, PC_NET_RELIABLE, &out, (uint16_t)sizeof(out));

    if (accepted) {
        /* Reuses the exact same broadcast helper and PC_NETGAME_MSG_FIELD_UPDATE wire shape pickup
           already established -- unmodified, see pcnetgame_broadcast_field_update()'s own doc. */
        pcnetgame_broadcast_field_update(in->ut_x, in->ut_z, (uint16_t)placed_item);
    }
}

/* Client side: the host's authoritative answer to our own pending pickup request -- see
 * pc_net_game_request_pickup()'s doc for the overall flow. */
static void pcnetgame_handle_client_pickup_result(const PCNetGamePickupResultMsg* in) {
    if (!s_pickup_pending.valid || s_pickup_pending.request_id != in->request_id) {
        return; /* not our current pending request -- already resolved, already given up after
                  * timing out, or a stale duplicate from an earlier retry cycle. Ignore rather
                  * than re-apply anything. */
    }

    s_pickup_pending.valid = 0; /* resolved either way -- never retried or re-applied again */

    if (!in->accepted) {
        printf("[NET][PICKUP] request %u rejected by host (tile %d,%d)\n", (unsigned)in->request_id,
               (int)in->ut_x, (int)in->ut_z);
        return;
    }

    /* Stage 5A: the one place this file ever writes to Now_Private (see this file's own header
     * comment) -- via the real, unmodified mPr_SetFreePossessionItem(), exactly as
     * Player_actor_setup_main_Pickup() would have called it locally in single-player (see the
     * Stage 5A inventory-architecture audit, Parts 4/6). No slot number is sent by the host (see
     * PCNetGamePickupResultMsg's doc) -- this client's own real pocket contents are the only
     * correct basis for choosing one. */
    if (Now_Private == NULL) {
        printf("[NET][PICKUP] request %u accepted (item=%u) but no save is loaded -- item lost\n",
               (unsigned)in->request_id, (unsigned)in->granted_item);
        return;
    }
    if (!mPr_SetFreePossessionItem(Now_Private, (mActor_name_t)in->granted_item, mPr_ITEM_COND_NORMAL)) {
        /* Pockets filled up between sending the request and this result arriving, or were already
         * full when the request was sent (see this stage's Setup_main_Pickup seam, which forces
         * exchange_flag off for a network client for exactly this reason). Per the Stage 5A
         * inventory-architecture audit: never fabricate a slot, never grant twice -- log and let
         * the item be lost from this client's perspective rather than reimplementing the vanilla
         * swap/drop submenu against network-pending state. The field tile itself is unaffected:
         * it was already cleared, authoritatively, for every client, by the FIELD_UPDATE this
         * same PICKUP_RESULT is paired with. */
        printf("[NET][PICKUP] request %u accepted (item=%u) but local pockets are full -- item lost\n",
               (unsigned)in->request_id, (unsigned)in->granted_item);
    }
}

/* Client side: the host's authoritative answer to our own pending drop request -- see
 * pc_net_game_request_drop()'s doc for the overall flow. The field side of an accepted drop arrives
 * separately via the ordinary FIELD_UPDATE this same acceptance is paired with (see
 * pcnetgame_handle_host_drop_request()) -- this function's only job is the inventory side: clearing
 * the exact pocket slot this client itself chose when it sent the request. No slot number travels
 * on the wire (see PCNetGameDropResultMsg's doc) -- s_drop_pending.pocket_slot_idx, this client's
 * own already-known choice, is authoritative for which slot to clear. */
static void pcnetgame_handle_client_drop_result(const PCNetGameDropResultMsg* in) {
    if (!s_drop_pending.valid || s_drop_pending.request_id != in->request_id) {
        return; /* not our current pending request -- already resolved, already given up after
                   timing out, or a stale duplicate from an earlier retry cycle. Ignore rather than
                   re-apply anything. */
    }

    s_drop_pending.valid = 0; /* resolved either way -- never retried or re-applied again */

    if (!in->accepted) {
        printf("[NET][DROP] request %u rejected by host (tile %d,%d)\n", (unsigned)in->request_id,
               (int)in->ut_x, (int)in->ut_z);
        return; /* item stays exactly where it was -- never removed locally before this point, so
                   there is nothing to undo */
    }

    /* The one place this file writes to Now_Private for a drop -- via the real, unmodified
       mPr_SetPossessionItem(), exactly as mTG_field_put_proc() would have called it locally in
       single-player. The field tile itself is handled by the accompanying FIELD_UPDATE, not here. */
    if (Now_Private == NULL) {
        printf("[NET][DROP] request %u accepted (item=%u) but no save is loaded -- pocket not cleared\n",
               (unsigned)in->request_id, (unsigned)in->placed_item);
        return;
    }
    mPr_SetPossessionItem(Now_Private, (int)s_drop_pending.pocket_slot_idx, (mActor_name_t)EMPTY_NO,
                          mPr_ITEM_COND_NORMAL);
    printf("[NET][DROP] request %u accepted (item=%u) -- cleared pocket slot %d\n", (unsigned)in->request_id,
           (unsigned)in->placed_item, (int)s_drop_pending.pocket_slot_idx);
}

/* Stage 5A: applies a FIELD_UPDATE if the local field data can accept it right now, otherwise
 * queues it for pc_net_game_poll() to retry -- see s_pending_field_updates's own doc comment for
 * why this must never simply be dropped on failure. */
static void pcnetgame_apply_field_update_or_defer(uint8_t ut_x, uint8_t ut_z, uint16_t new_fg_value) {
    int i, target_slot;

    if (mFI_UtNumtoFGSet_common((mActor_name_t)new_fg_value, (int)ut_x, (int)ut_z, TRUE)) {
        return; /* applied immediately -- the common case */
    }

    target_slot = -1;
    for (i = 0; i < PC_NETGAME_PENDING_FIELD_UPDATE_MAX; i++) {
        if (s_pending_field_updates[i].valid && s_pending_field_updates[i].ut_x == ut_x &&
            s_pending_field_updates[i].ut_z == ut_z) {
            target_slot = i; /* an existing pending update for this SAME tile -- overwrite it,
                                * only the latest value for a tile is ever meaningful */
            break;
        }
        if (target_slot == -1 && !s_pending_field_updates[i].valid) {
            target_slot = i;
        }
    }
    if (target_slot == -1) {
        /* Every slot holds a DIFFERENT still-pending tile -- extremely unlikely (would need this
         * many distinct field mutations to arrive while the local field data genuinely isn't
         * ready, e.g. during the same early-boot window the appearance crash fix already covers),
         * but overwrite the oldest rather than drop the newest so recent activity wins. */
        target_slot = 0;
        printf("[NET][PICKUP] pending field-update queue full -- overwriting oldest deferred update\n");
    }
    s_pending_field_updates[target_slot].valid = 1;
    s_pending_field_updates[target_slot].ut_x = ut_x;
    s_pending_field_updates[target_slot].ut_z = ut_z;
    s_pending_field_updates[target_slot].new_fg_value = new_fg_value;
}

/* Client side: the host's authoritative statement about one field tile -- see
 * PCNetGameFieldUpdateMsg's own doc comment. Applies to every client, including the one whose own
 * request caused it (that client separately gets a unicast PICKUP_RESULT, handled above; this
 * message is what makes every OTHER client's world converge). */
static void pcnetgame_handle_client_field_update(const PCNetGameFieldUpdateMsg* in) {
    pcnetgame_apply_field_update_or_defer(in->ut_x, in->ut_z, in->new_fg_value);
}

/* Host side: a peer's raw PC_NET_EVENT_DATA payload. Anything that isn't a well-formed
 * IDENTITY message is ignored -- a peer is only ever marked READY by successfully validating
 * one, never merely by having sent *some* UDP packet. */
static void pcnetgame_handle_host_data(PCNetPeerId peer, const uint8_t* data, uint16_t size) {
    PCNetGameIdentityMsg in;

    if (size == sizeof(PCNetMoveMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_MOVE) {
        PCNetMoveMsg mv;
        memcpy(&mv, data, sizeof(mv));
        pcnetgame_handle_host_move(peer, &mv);
        return;
    }

    if (size == sizeof(PCNetGameAppearanceMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_APPEARANCE) {
        PCNetGameAppearanceMsg ap;
        memcpy(&ap, data, sizeof(ap));
        pcnetgame_handle_host_appearance(peer, &ap);
        return;
    }

    if (size == sizeof(PCNetGamePickupRequestMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_PICKUP_REQUEST) {
        PCNetGamePickupRequestMsg pr;
        memcpy(&pr, data, sizeof(pr));
        pcnetgame_handle_host_pickup_request(peer, &pr);
        return;
    }

    if (size == sizeof(PCNetGameDropRequestMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_DROP_REQUEST) {
        PCNetGameDropRequestMsg dr;
        memcpy(&dr, data, sizeof(dr));
        pcnetgame_handle_host_drop_request(peer, &dr);
        return;
    }

    if (size != sizeof(PCNetGameIdentityMsg) || data[0] != (uint8_t)PC_NETGAME_MSG_IDENTITY) {
        return; /* malformed, short, or not an IDENTITY message -- ignore rather than misinterpret */
    }
    memcpy(&in, data, sizeof(in));

    if (in.protocol_version != PC_NETGAME_PROTOCOL_VERSION) {
        printf("[NET] host: peer %d speaks protocol %u, we require %u -- rejecting\n", (int)peer,
               (unsigned)in.protocol_version, (unsigned)PC_NETGAME_PROTOCOL_VERSION);
        pcnetgame_send_reject(peer, PC_NETGAME_REJECT_PROTOCOL_MISMATCH);
        pc_net_disconnect(peer);
        return;
    }

    printf("[NET] host: identity received from peer %d (player_id=%u land_id=%u has_save=%d)\n", (int)peer,
           (unsigned)in.player_id, (unsigned)in.land_id, (int)in.has_save);

    {
        PCNetGameIdentityAckMsg ack;
        memset(&ack, 0, sizeof(ack));
        ack.msg_type = (uint8_t)PC_NETGAME_MSG_IDENTITY_ACK;
        ack.accepted = 1;
        ack.assigned_peer_id = (uint16_t)peer;
        ack.protocol_version = PC_NETGAME_PROTOCOL_VERSION;
        pcnetgame_capture_local_identity(ack.player_name, ack.land_name, &ack.player_id, &ack.land_id,
                                          &ack.has_save);
        pc_net_send(peer, PC_NET_RELIABLE, &ack, (uint16_t)sizeof(ack));
    }

    if (peer >= 0 && peer < PC_NET_MAX_PEERS) s_host_peer_link[peer] = PC_NETGAME_LINK_READY;
    pcnetgame_reset_all_host_peer_state(peer); /* Stage 5A/5B-1: defensive -- see
                                                  * pcnetgame_reset_all_host_peer_state()'s own doc;
                                                  * belt-and-suspenders alongside the disconnect-time
                                                  * reset in case this exact peer slot somehow became
                                                  * READY again without this file observing the
                                                  * PC_NET_EVENT_PEER_DISCONNECTED in between */
    printf("[NET] host: peer %d -> READY\n", (int)peer);

    /* Stage 4C-1 (3+ player backfill fix): give this now-READY client the host's own appearance
     * AND the last-known appearance of every other already-READY peer -- otherwise anyone who
     * joined before this peer would stay permanently invisible to it (the original Stage 4C-1
     * bug; see the investigation). See pcnetgame_host_send_full_roster()'s own doc comment. */
    pcnetgame_host_send_full_roster(peer);

    {
        /* Stage 2: give the host a visible representation of this now-READY client. */
        PCNetGameIdentity remote_identity;
        memcpy(remote_identity.player_name, in.player_name, PC_NETGAME_NAME_LEN);
        memcpy(remote_identity.land_name, in.land_name, PC_NETGAME_LAND_LEN);
        remote_identity.player_id = in.player_id;
        remote_identity.land_id = in.land_id;
        remote_identity.has_save = in.has_save;
        pc_remote_player_on_ready(peer, &remote_identity);
    }
}

/* Client side: the host's raw PC_NET_EVENT_DATA payload. */
static void pcnetgame_handle_client_data(const uint8_t* data, uint16_t size) {
    if (size == sizeof(PCNetMoveMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_MOVE) {
        PCNetMoveMsg mv;
        memcpy(&mv, data, sizeof(mv));
        pcnetgame_handle_client_move(&mv);
        return;
    }

    if (size == sizeof(PCNetGameAppearanceMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_APPEARANCE) {
        PCNetGameAppearanceMsg ap;
        memcpy(&ap, data, sizeof(ap));
        pcnetgame_handle_client_appearance(&ap);
        return;
    }

    if (size == sizeof(PCNetGamePickupResultMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_PICKUP_RESULT) {
        PCNetGamePickupResultMsg pr;
        memcpy(&pr, data, sizeof(pr));
        pcnetgame_handle_client_pickup_result(&pr);
        return;
    }

    if (size == sizeof(PCNetGameDropResultMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_DROP_RESULT) {
        PCNetGameDropResultMsg dr;
        memcpy(&dr, data, sizeof(dr));
        pcnetgame_handle_client_drop_result(&dr);
        return;
    }

    if (size == sizeof(PCNetGameFieldUpdateMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_FIELD_UPDATE) {
        PCNetGameFieldUpdateMsg fu;
        memcpy(&fu, data, sizeof(fu));
        pcnetgame_handle_client_field_update(&fu);
        return;
    }

    if (size == sizeof(PCNetGameIdentityAckMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_IDENTITY_ACK) {
        PCNetGameIdentityAckMsg in;
        memcpy(&in, data, sizeof(in));
        if (!in.accepted) return; /* host sends a separate REJECT message instead; defensive only */

        memcpy(s_client_host_identity.player_name, in.player_name, PC_NETGAME_NAME_LEN);
        memcpy(s_client_host_identity.land_name, in.land_name, PC_NETGAME_LAND_LEN);
        s_client_host_identity.player_id = in.player_id;
        s_client_host_identity.land_id = in.land_id;
        s_client_host_identity.has_save = in.has_save;
        s_client_host_identity_valid = 1;

        s_client_link = PC_NETGAME_LINK_READY;
        printf("[NET] client: handshake complete (assigned peer id %u) -> READY\n", (unsigned)in.assigned_peer_id);

        {
            /* Stage 4C-1 (ordering-race fix): send our own appearance now, right after reaching
             * READY, instead of at transport-connect time -- see the PC_NET_EVENT_PEER_CONNECTED
             * case above for why. The host can only have sent this ACK after already processing
             * our IDENTITY and marking this peer READY (see pcnetgame_handle_host_data()), so its
             * own pre-READY appearance guard can no longer discard what we're about to send. */
            PCNetGameAppearanceMsg amsg;
            pcnetgame_build_appearance_msg(&amsg, 0);
            pc_net_send(0, PC_NET_RELIABLE, &amsg, (uint16_t)sizeof(amsg));
        }

        /* Stage 2: give the client a visible representation of the host. Tracked under the
         * reserved PC_NETGAME_HOST_PLAYER_ID (a PCNetPlayerId), NOT the client's own transport
         * PCNetPeerId (which is a different, transport-only number space -- see pc_net_game.h). */
        pc_remote_player_on_ready(PC_NETGAME_HOST_PLAYER_ID, &s_client_host_identity);
        return;
    }

    if (size == sizeof(PCNetGameRejectMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_REJECT) {
        PCNetGameRejectMsg in;
        memcpy(&in, data, sizeof(in));
        printf("[NET] client: host rejected the connection (reason=%u, host requires protocol %u, we sent %u)\n",
               (unsigned)in.reason, (unsigned)in.expected_protocol_version, (unsigned)PC_NETGAME_PROTOCOL_VERSION);
        pc_net_game_shutdown(); /* clean and immediate -- no need to wait for a transport timeout */
        return;
    }

    /* malformed/short/unrecognized: ignore */
}

int pc_net_game_start_host(uint16_t port) {
    int i;
    if (s_role != PC_NETGAME_ROLE_NONE) {
        printf("[NET] start_host: networking already active, ignoring\n");
        return 0;
    }
    if (!pc_net_init()) {
        printf("[NET] start_host: pc_net_init failed -- continuing single-player\n");
        return 0;
    }
    if (!pc_net_host_start(port)) {
        printf("[NET] start_host: could not bind UDP port %u -- continuing single-player\n", (unsigned)port);
        pc_net_shutdown();
        return 0;
    }
    s_role = PC_NETGAME_ROLE_HOST;
    for (i = 0; i < PC_NET_MAX_PEERS; i++) s_host_peer_link[i] = PC_NETGAME_LINK_DISCONNECTED;
    printf("[NET] hosting on UDP port %u\n", (unsigned)port);
    return 1;
}

int pc_net_game_start_client(const char* host_ip, uint16_t port) {
    if (s_role != PC_NETGAME_ROLE_NONE) {
        printf("[NET] start_client: networking already active, ignoring\n");
        return 0;
    }
    if (!pc_net_init()) {
        printf("[NET] start_client: pc_net_init failed -- continuing single-player\n");
        return 0;
    }
    if (!pc_net_client_connect(host_ip, port)) {
        printf("[NET] start_client: could not start connecting to %s:%u -- continuing single-player\n", host_ip,
               (unsigned)port);
        pc_net_shutdown();
        return 0;
    }
    s_role = PC_NETGAME_ROLE_CLIENT;
    s_client_link = PC_NETGAME_LINK_CONNECTING;
    s_client_host_identity_valid = 0;

    /* Stage 5A: a fresh connection must never carry over a previous one's pickup state -- reset
     * the request-id counter (so it can never collide with a value the NEW host's dedup cache
     * might coincidentally still associate with a totally different prior connection) and drop
     * any pending/deferred pickup bookkeeping outright. */
    memset(&s_pickup_pending, 0, sizeof(s_pickup_pending));
    s_next_pickup_request_id = 1;
    memset(s_pending_field_updates, 0, sizeof(s_pending_field_updates));

    /* Stage 5B-1: same reasoning as the pickup reset immediately above, kept as a separate
       instance -- see s_drop_pending's own doc comment. */
    memset(&s_drop_pending, 0, sizeof(s_drop_pending));
    s_next_drop_request_id = 1;

    printf("[NET] connecting to %s:%u...\n", host_ip, (unsigned)port);
    return 1;
}

void pc_net_game_shutdown(void) {
    if (s_role == PC_NETGAME_ROLE_NONE) return;
    printf("[NET] shutting down networking (was %s)\n", s_role == PC_NETGAME_ROLE_HOST ? "host" : "client");

    /* Say goodbye before tearing down the socket, so a clean exit is detected by the other
     * side immediately (PC_NET_EVENT_PEER_DISCONNECTED) instead of only after the transport's
     * timeout. A hard kill/crash still falls back to that timeout, as it must. */
    if (s_role == PC_NETGAME_ROLE_CLIENT) {
        pc_net_disconnect(PC_NET_INVALID_PEER);
        pc_net_poll(); /* flush the just-queued send before the socket goes away */
    } else if (s_role == PC_NETGAME_ROLE_HOST) {
        int i;
        for (i = 0; i < PC_NET_MAX_PEERS; i++) {
            if (s_host_peer_link[i] != PC_NETGAME_LINK_DISCONNECTED) pc_net_disconnect((PCNetPeerId)i);
        }
        pc_net_poll();
    }

    pc_net_shutdown();
    s_role = PC_NETGAME_ROLE_NONE;
    s_client_link = PC_NETGAME_LINK_DISCONNECTED;
    s_client_host_identity_valid = 0;
    memset(s_host_peer_link, 0, sizeof(s_host_peer_link));

    /* A local, voluntary shutdown never generates a PC_NET_EVENT_PEER_DISCONNECTED for
     * ourselves (that event is how the *other* side learns we left) -- destroy our own tracked
     * remote-player actors explicitly so a manual disconnect/quit doesn't leak one. */
    pc_remote_player_shutdown();
}

/* Stage 5A.1: --pickup-test-seed (see pc_main.c/pc_platform.h). Host-only, and a complete no-op
 * unless that flag was passed on the command line -- normal single-player and normal hosted play
 * (without the flag) never execute anything in this function beyond the flag check itself.
 *
 * Exists because this project's own test save has zero naturally-occurring loose field items
 * anywhere in the addressable town (confirmed by an exhaustive scan during the Stage 5A
 * implementation pass), and Stage 5A's protocol deliberately gives a client no way to read raw
 * field truth (see pc_net_game_request_pickup()'s doc) -- so test_pickup_sync.py's own live
 * discovery scan has nothing to find without a fixture. This is that fixture: one candidate tile
 * per acre (30 total, spread evenly so at least some land on acres the host has actually streamed
 * in), each written with mFI_UtNumtoFGSet_common() -- the identical primitive real pickup already
 * uses -- and each retried every frame ONLY until it individually succeeds once, never again after
 * that (so a real, later pickup of a seeded tile stays genuinely empty for the rest of the
 * session, rather than this function fighting it back to non-empty). There is deliberately no
 * overall timeout: different acres were observed (during implementation) to become writable at
 * different, unpredictable times after boot, and this is test-only code where waiting a few extra
 * seconds costs nothing. */
static void pcnetgame_run_pickup_test_seed(void) {
    static const int s_seed_tiles[30][2] = {
        { 8, 8 },   { 24, 8 },  { 40, 8 },  { 56, 8 },  { 72, 8 },
        { 8, 24 },  { 24, 24 }, { 40, 24 }, { 56, 24 }, { 72, 24 },
        { 8, 40 },  { 24, 40 }, { 40, 40 }, { 56, 40 }, { 72, 40 },
        { 8, 56 },  { 24, 56 }, { 40, 56 }, { 56, 56 }, { 72, 56 },
        { 8, 72 },  { 24, 72 }, { 40, 72 }, { 56, 72 }, { 72, 72 },
        { 8, 88 },  { 24, 88 }, { 40, 88 }, { 56, 88 }, { 72, 88 },
    };
    static int s_seed_done[30] = { 0 };
    static int s_logged = 0;
    int i;

    if (!g_pc_pickup_test_seed || s_role != PC_NETGAME_ROLE_HOST || gamePT == NULL) {
        return;
    }
    if (!s_logged) {
        s_logged = 1;
        printf("[NET][PICKUP] --pickup-test-seed active: seeding up to 30 fixture tiles\n");
    }
    for (i = 0; i < 30; i++) {
        if (!s_seed_done[i] &&
            mFI_UtNumtoFGSet_common((mActor_name_t)ITM_FOOD_APPLE, s_seed_tiles[i][0], s_seed_tiles[i][1], TRUE)) {
            s_seed_done[i] = 1;
            printf("[NET][PICKUP] --pickup-test-seed: fixture item placed at tile (%d,%d)\n", s_seed_tiles[i][0],
                   s_seed_tiles[i][1]);
        }
    }
}

void pc_net_game_poll(void) {
    PCNetEvent ev;

    if (s_role == PC_NETGAME_ROLE_NONE) return;

    pc_net_poll(); /* never blocks */

    while (pc_net_next_event(&ev)) {
        if (s_role == PC_NETGAME_ROLE_HOST) {
            switch (ev.type) {
                case PC_NET_EVENT_PEER_CONNECTED:
                    if (ev.peer >= 0 && ev.peer < PC_NET_MAX_PEERS) {
                        s_host_peer_link[ev.peer] = PC_NETGAME_LINK_HANDSHAKE;
                    }
                    printf("[NET] host: peer %d transport-connected, awaiting identity\n", (int)ev.peer);
                    break;
                case PC_NET_EVENT_PEER_DISCONNECTED:
                    if (ev.peer >= 0 && ev.peer < PC_NET_MAX_PEERS) {
                        s_host_peer_link[ev.peer] = PC_NETGAME_LINK_DISCONNECTED;
                    }
                    printf("[NET] host: peer %d disconnected\n", (int)ev.peer);
                    pcnetgame_reset_all_host_peer_state(ev.peer); /* Stage 5A/5B-1: never let a
                                                                     * reused peer slot answer a
                                                                     * future connection with this
                                                                     * one's cached pickup/drop
                                                                     * results -- see
                                                                     * pcnetgame_reset_all_host_peer_state() */
                    pc_remote_player_on_disconnect(ev.peer);
                    break;
                case PC_NET_EVENT_DATA:
                    pcnetgame_handle_host_data(ev.peer, ev.data, ev.size);
                    break;
            }
        } else { /* PC_NETGAME_ROLE_CLIENT */
            switch (ev.type) {
                case PC_NET_EVENT_PEER_CONNECTED: {
                    PCNetGameIdentityMsg msg;
                    s_client_link = PC_NETGAME_LINK_HANDSHAKE;
                    printf("[NET] client: transport-connected to host, sending identity\n");
                    pcnetgame_build_identity_msg(&msg);
                    pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
                    /* Stage 4C-1 (ordering-race fix): appearance is NOT sent here any more. Sending
                     * it immediately alongside IDENTITY raced against the host's own IDENTITY
                     * processing -- if this process's APPEARANCE datagram was dequeued by the host
                     * before its IDENTITY was, pcnetgame_handle_host_appearance()'s pre-READY guard
                     * silently discarded it with no recovery (see the Stage 4C-1 investigation).
                     * Appearance is now sent from the PC_NETGAME_MSG_IDENTITY_ACK handler below,
                     * once this process's own link reaches PC_NETGAME_LINK_READY: by the time that
                     * ACK exists, the host has -- by construction, see pcnetgame_handle_host_data()
                     * -- already processed this peer's IDENTITY and marked it READY, so the
                     * host-side gate can no longer race against it. */
                    break;
                }
                case PC_NET_EVENT_PEER_DISCONNECTED:
                    s_client_link = PC_NETGAME_LINK_DISCONNECTED;
                    s_client_host_identity_valid = 0;
                    printf("[NET] client: host connection lost\n");
                    if (s_pickup_pending.valid) {
                        /* Stage 5A: nothing left to retry to -- drop it immediately rather than
                         * waiting for the timeout to notice (see Phase 9's "client disconnecting
                         * with a request in flight" race). */
                        s_pickup_pending.valid = 0;
                    }
                    if (s_drop_pending.valid) {
                        /* Stage 5B-1: same reasoning as the pickup case immediately above -- the
                           item was never removed locally, so there is nothing to lose or restore. */
                        s_drop_pending.valid = 0;
                    }
                    pc_remote_player_on_disconnect(PC_NETGAME_HOST_PLAYER_ID);
                    break;
                case PC_NET_EVENT_DATA:
                    pcnetgame_handle_client_data(ev.data, ev.size);
                    break;
            }
        }
    }

    /* Stage 3: throttled movement send, decoupled from the render/frame rate (see
     * PC_NETGAME_MOVE_SEND_PERIOD_60FPS_FRAMES's doc above). gamePT/the local player actor may
     * not exist yet (still at a menu, no save loaded) -- if so, there is simply nothing to sample
     * or send yet; this never blocks single-player and never fails single-player startup. */
    if (gamePT != NULL && graph_dt_period_elapsed(gamePT, &s_move_send_accum, PC_NETGAME_MOVE_SEND_PERIOD_60FPS_FRAMES)) {
        PLAYER_ACTOR* local = GET_PLAYER_ACTOR_NOW();
        if (pcnetgame_is_real_player_actor(local)) {
            PCNetMoveMsg msg;
            pcnetgame_sample_local_move(local, &msg);
            s_move_send_count_this_window++;

            if (s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY) {
                pc_net_send(0, PC_NET_UNRELIABLE, &msg, (uint16_t)sizeof(msg));
            } else if (s_role == PC_NETGAME_ROLE_HOST) {
                int i;
                msg.net_player_id = (uint8_t)PC_NETGAME_HOST_PLAYER_ID;
                for (i = 0; i < PC_NET_MAX_PEERS; i++) {
                    if (s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
                        pc_net_send((PCNetPeerId)i, PC_NET_UNRELIABLE, &msg, (uint16_t)sizeof(msg));
                    }
                }
            }
        }
    }

    if (gamePT != NULL &&
        graph_dt_period_elapsed(gamePT, &s_move_rate_log_accum, PC_NETGAME_MOVE_RATE_LOG_PERIOD_60FPS_FRAMES)) {
        printf("[NET][DIAG] movement send rate: %d Hz (target %.0f Hz)\n", s_move_send_count_this_window,
               (double)PC_NETGAME_MOVE_SEND_RATE_HZ);
        s_move_send_count_this_window = 0;
    }

    /* Stage 4C-1 (one-shot-UDP-loss fix): low-frequency appearance resend -- see
     * PC_NETGAME_APPEARANCE_RESEND_PERIOD_60FPS_FRAMES's doc above for why this exists and why the
     * interval is conservative. Same gamePT-may-not-exist-yet reasoning as the movement throttle
     * above: if there is no active GAME_PLAY there is no frame-time source to drive the timer, so
     * this simply does not tick yet (never blocks single-player, never fires before a session
     * actually exists). Client: resend only this process's own appearance, and only once actually
     * READY (mirrors the movement throttle's own client-side gate). Host: reuse
     * pcnetgame_host_send_full_roster() for every currently-READY peer, exactly the same call the
     * one-shot newcomer backfill uses -- so a peer that missed its original appearance delivery
     * (lost datagram, or the identity/appearance ordering race) receives a fresh, complete copy
     * within one interval, with no reconnect required. */
    if (gamePT != NULL && graph_dt_period_elapsed(gamePT, &s_appearance_resend_accum,
                                                  PC_NETGAME_APPEARANCE_RESEND_PERIOD_60FPS_FRAMES)) {
        if (s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY) {
            PCNetGameAppearanceMsg amsg;
            pcnetgame_build_appearance_msg(&amsg, 0);
            pc_net_send(0, PC_NET_RELIABLE, &amsg, (uint16_t)sizeof(amsg));
        } else if (s_role == PC_NETGAME_ROLE_HOST) {
            int i;
            for (i = 0; i < PC_NET_MAX_PEERS; i++) {
                if (s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
                    pcnetgame_host_send_full_roster((PCNetPeerId)i);
                }
            }
        }
    }

    /* Stage 4C-2: per-frame local appearance change detection. Deliberately NOT throttled like the
     * timers above -- a clothing change is a rare, human-triggered event (per the Stage 4C-2
     * investigation, gender/face are effectively fixed after character creation; only clothing and,
     * occasionally, sunburn rank ever change mid-session), so comparing a PCNetPlayerAppearance
     * (a handful of scalar fields, plus a fixed 544-byte comparison only meaningful when wearing a
     * custom design) once per frame costs nothing worth optimizing away, and detecting the change
     * the moment it happens -- rather than waiting for the periodic resend above -- is the entire
     * point of this stage. Gated on gamePT the same as the throttles above: no active GAME_PLAY
     * means no meaningful local appearance to capture yet either.
     *
     * First observation after this cache has never been established (s_last_local_appearance_valid
     * == 0, e.g. right after this process starts, or the moment Now_Private first becomes
     * available) is deliberately NOT treated as a change: the existing READY handshake already
     * sends the initial appearance unconditionally on its own (the client's IDENTITY_ACK handler
     * above, and pcnetgame_host_send_full_roster() on the host side), so silently adopting whatever
     * is currently captured as the baseline -- without sending anything -- avoids ever racing that
     * with a redundant immediate duplicate. From then on the cache simply persists across
     * disconnects/reconnects/scene transitions (nothing here ever clears it): if this process's own
     * appearance genuinely didn't change while briefly disconnected, the comparison correctly finds
     * no difference; if it did, the one extra immediate send is harmless (duplicates are already
     * proven safe -- see pc_remote_player_on_appearance()'s doc) and the existing unconditional
     * initial-READY-send has already delivered the current, correct appearance regardless. */
    if (gamePT != NULL) {
        PCNetPlayerAppearance current;
        pcnetgame_capture_local_appearance(&current);

        if (!s_last_local_appearance_valid) {
            s_last_local_appearance = current;
            s_last_local_appearance_valid = 1;
        } else if (memcmp(&current, &s_last_local_appearance, sizeof(current)) != 0) {
            if (s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY) {
                /* Client: send straight to the host, exactly like the periodic resend does --
                 * the host's existing, unmodified appearance receive handler
                 * (pcnetgame_handle_host_appearance()) already applies it locally and relays it
                 * to every other READY peer, so no further action is needed here. */
                PCNetGameAppearanceMsg amsg;
                pcnetgame_pack_appearance_msg(&amsg, 0, &current);
                pc_net_send(0, PC_NET_RELIABLE, &amsg, (uint16_t)sizeof(amsg));
            } else if (s_role == PC_NETGAME_ROLE_HOST) {
                /* Host: there is no equivalent "someone else relays my own change" path -- a
                 * client's changed appearance reaches other clients because the host itself
                 * receives and relays it, but the host never "receives" its own appearance over
                 * the network. Broadcast directly to every READY peer instead. Deliberately just
                 * the host's own appearance (net_player_id = PC_NETGAME_HOST_PLAYER_ID, never a
                 * peer index) -- NOT the full pcnetgame_host_send_full_roster() roster resend,
                 * which would needlessly retransmit every other peer's already-unchanged
                 * appearance too. */
                PCNetGameAppearanceMsg amsg;
                int i;
                pcnetgame_pack_appearance_msg(&amsg, (uint8_t)PC_NETGAME_HOST_PLAYER_ID, &current);
                for (i = 0; i < PC_NET_MAX_PEERS; i++) {
                    if (s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
                        pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &amsg, (uint16_t)sizeof(amsg));
                    }
                }
            }
            s_last_local_appearance = current;
        }
    }

    /* Stage 5A: pending pickup-request timeout/retry, client-only. Gated on gamePT exactly like
     * every other per-frame timer above (graph_dt_period_elapsed() needs a valid GAME* to read a
     * frame-time delta from) -- a pickup can only ever be initiated from within active gameplay
     * (see m_player_main_pickup.c_inc), so this can never need to fire before a GAME_PLAY exists,
     * and simply pauses -- rather than misfiring -- across a scene transition where gamePT is
     * briefly NULL, resuming once the new scene's GAME_PLAY exists. See
     * PC_NETGAME_PICKUP_TIMEOUT_60FPS_FRAMES/PC_NETGAME_PICKUP_MAX_RETRIES' own doc for the exact
     * values and reasoning. */
    if (gamePT != NULL && s_pickup_pending.valid &&
        graph_dt_period_elapsed(gamePT, &s_pickup_pending.timeout_accum, PC_NETGAME_PICKUP_TIMEOUT_60FPS_FRAMES)) {
        if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
            /* Disconnected (or somehow no longer the client role) while a request was pending --
             * nothing left to retry to. The disconnect-event handler above already clears this in
             * the common case; this only catches an edge not routed through that event. */
            s_pickup_pending.valid = 0;
        } else if (s_pickup_pending.retry_count >= PC_NETGAME_PICKUP_MAX_RETRIES) {
            printf("[NET][PICKUP] request %u timed out after %d retries -- giving up (tile %d,%d)\n",
                   (unsigned)s_pickup_pending.request_id, s_pickup_pending.retry_count,
                   (int)s_pickup_pending.ut_x, (int)s_pickup_pending.ut_z);
            s_pickup_pending.valid = 0;
        } else {
            PCNetGamePickupRequestMsg msg;
            s_pickup_pending.retry_count++;
            memset(&msg, 0, sizeof(msg));
            msg.msg_type = (uint8_t)PC_NETGAME_MSG_PICKUP_REQUEST;
            msg.ut_x = s_pickup_pending.ut_x;
            msg.ut_z = s_pickup_pending.ut_z;
            msg.request_id = s_pickup_pending.request_id; /* SAME id -- a retry of the same
                                                              logical request, not a new one, so
                                                              the host's dedup cache recognizes it */
            pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
        }
    }

    /* Stage 5B-1: pending drop-request timeout/retry, client-only. Exact structural mirror of the
       pickup retry block above -- see its own doc comment for the shared reasoning (gamePT gate,
       timeout/retry budget). Kept as a separate block against a separate pending instance rather
       than merged with pickup's, for the same "don't conflate pickup and drop state" reason as
       s_drop_pending's own doc comment. */
    if (gamePT != NULL && s_drop_pending.valid &&
        graph_dt_period_elapsed(gamePT, &s_drop_pending.timeout_accum, PC_NETGAME_DROP_TIMEOUT_60FPS_FRAMES)) {
        if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
            s_drop_pending.valid = 0;
        } else if (s_drop_pending.retry_count >= PC_NETGAME_DROP_MAX_RETRIES) {
            printf("[NET][DROP] request %u timed out after %d retries -- giving up (tile %d,%d)\n",
                   (unsigned)s_drop_pending.request_id, s_drop_pending.retry_count,
                   (int)s_drop_pending.ut_x, (int)s_drop_pending.ut_z);
            s_drop_pending.valid = 0;
        } else {
            PCNetGameDropRequestMsg msg;
            s_drop_pending.retry_count++;
            memset(&msg, 0, sizeof(msg));
            msg.msg_type = (uint8_t)PC_NETGAME_MSG_DROP_REQUEST;
            msg.pocket_slot_idx = s_drop_pending.pocket_slot_idx;
            msg.ut_x = s_drop_pending.ut_x;
            msg.ut_z = s_drop_pending.ut_z;
            msg.claimed_item = s_drop_pending.claimed_item;
            msg.request_id = s_drop_pending.request_id; /* SAME id -- see the pickup retry block's
                                                            own comment */
            pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
        }
    }

    /* Stage 5A: retry any FIELD_UPDATE that arrived before the local field data was ready to
     * accept it -- see s_pending_field_updates's own doc comment for why this must never simply
     * be dropped. Gated on gamePT for the same reason as every other per-frame block here: there
     * is nothing meaningful to write into before a GAME_PLAY exists. */
    if (gamePT != NULL) {
        int i;
        for (i = 0; i < PC_NETGAME_PENDING_FIELD_UPDATE_MAX; i++) {
            if (s_pending_field_updates[i].valid &&
                mFI_UtNumtoFGSet_common((mActor_name_t)s_pending_field_updates[i].new_fg_value,
                                        (int)s_pending_field_updates[i].ut_x,
                                        (int)s_pending_field_updates[i].ut_z, TRUE)) {
                s_pending_field_updates[i].valid = 0;
            }
        }
    }

    /* Stage 5A.1: see pcnetgame_run_pickup_test_seed()'s own doc -- a complete no-op unless
     * --pickup-test-seed was passed on the command line. */
    pcnetgame_run_pickup_test_seed();
}

PCNetGameRole pc_net_game_role(void) {
    return s_role;
}

PCNetGameLinkState pc_net_game_client_link_state(void) {
    return (s_role == PC_NETGAME_ROLE_CLIENT) ? s_client_link : PC_NETGAME_LINK_DISCONNECTED;
}

int pc_net_game_host_ready_peer_count(void) {
    int i, n = 0;
    if (s_role != PC_NETGAME_ROLE_HOST) return 0;
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_host_peer_link[i] == PC_NETGAME_LINK_READY) n++;
    }
    return n;
}

int pc_net_game_get_host_identity(PCNetGameIdentity* out) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || !s_client_host_identity_valid || out == NULL) return 0;
    *out = s_client_host_identity;
    return 1;
}

int pc_net_game_request_pickup(int ut_x, int ut_z) {
    PCNetGamePickupRequestMsg msg;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0; /* single-player or host -- caller should proceed with the normal local
                     mutation, unmodified */
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1; /* malformed input from the caller -- still "handled" (this process has no
                     authority to mutate locally as a client), just nothing is sent */
    }
    if (s_pickup_pending.valid) {
        /* One already in flight. The decomp state machine itself can't normally trigger a second
         * Setup_main_Pickup before the first one's animation state exits (see
         * m_player_main_pickup.c_inc), so this is a defensive no-op, not an expected path -- still
         * return 1 so the caller never falls through to a local mutation regardless. */
        return 1;
    }

    s_pickup_pending.valid = 1;
    s_pickup_pending.request_id = s_next_pickup_request_id++;
    s_pickup_pending.ut_x = (uint8_t)ut_x;
    s_pickup_pending.ut_z = (uint8_t)ut_z;
    s_pickup_pending.timeout_accum = 0.0f;
    s_pickup_pending.retry_count = 0;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_PICKUP_REQUEST;
    msg.ut_x = s_pickup_pending.ut_x;
    msg.ut_z = s_pickup_pending.ut_z;
    msg.request_id = s_pickup_pending.request_id;
    pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
    return 1;
}

/* Stage 5A.1: called from the decomp pickup state (see m_player_main_pickup.c_inc) right after
 * the HOST's OWN local pickup has already mutated the field tile through the existing, completely
 * unmodified single-player code (Player_actor_putin_item()/the PC_ENHANCEMENTS money-bag branch,
 * both in m_player_common.c_inc/m_player_main_pickup.c_inc) -- never before, and never as a
 * substitute for it. This function does not touch the field itself (no mFI_* call) -- it only
 * announces a mutation that has already happened, using the exact same wire message and broadcast
 * helper (pcnetgame_broadcast_field_update()) the client-request path already uses, so the two
 * paths can never disagree about what "the field changed" looks like to a connected client.
 *
 * A no-op for single-player (s_role != HOST) and, defensively, for a client (a client is never
 * itself authoritative for the field -- see pc_net_game_request_pickup() -- so it must never
 * broadcast this regardless of how it might be called). No self-send: the host is not its own
 * peer, so there is no feedback loop to guard against -- see pcnetgame_broadcast_field_update()'s
 * own doc comment. */
void pc_net_game_notify_local_field_pickup(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return; /* malformed input from the caller -- defensive; real decomp callers always pass a
                   value freshly computed by mFI_Wpos2UtNum() */
    }
    pcnetgame_broadcast_field_update((uint8_t)ut_x, (uint8_t)ut_z, (uint16_t)EMPTY_NO);
}

/* Stage 5B-3: host-local drop landing tracker. Single slot, not an array: the host has exactly one
 * local player, and this is only ever armed by, and consumed after, one synchronous
 * mTG_field_put_proc() menu action -- never reentrant within a frame (mirrors s_drop_pending's own
 * single-slot reasoning). Populated by pc_net_game_arm_local_drop_landing() (called from the
 * decomp drop-menu seam, m_tag_ovl.c) and consumed by pc_net_game_notify_local_drop_landing()
 * (called from the decomp drop-landing seam, bg_item_common.c_inc) -- see both functions' doc
 * comments in pc_net_game.h for exactly when each side fires. */
typedef struct {
    int valid;
    int ut_x;
    int ut_z;
} PCHostLocalDropLanding;
static PCHostLocalDropLanding s_host_local_drop_landing = {0};

/* Stage 5B-3: see the doc comment in pc_net_game.h. */
void pc_net_game_arm_local_drop_landing(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return; /* malformed input from the caller -- defensive; real decomp callers always pass a
                   value freshly computed by mFI_Wpos2UtNum() */
    }
    s_host_local_drop_landing.valid = 1;
    s_host_local_drop_landing.ut_x = ut_x;
    s_host_local_drop_landing.ut_z = ut_z;
}

/* Stage 5B-3: see the doc comment in pc_net_game.h. */
void pc_net_game_notify_local_drop_landing(int ut_x, int ut_z, int item) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!s_host_local_drop_landing.valid || s_host_local_drop_landing.ut_x != ut_x ||
        s_host_local_drop_landing.ut_z != ut_z) {
        return; /* not the tile we're watching (or nothing armed) -- some other drop-table user
                   landed, or a client's own Stage 5B-1 drop already announced itself */
    }
    s_host_local_drop_landing.valid = 0;
    pcnetgame_broadcast_field_update((uint8_t)ut_x, (uint8_t)ut_z, (uint16_t)item);
}

/* Stage 5B-1: see the doc comment in pc_net_game.h. Exact structural mirror of
 * pc_net_game_request_pickup() -- see that function's own doc for the shared reasoning
 * (client-only, one-in-flight, defensive bounds checks). */
int pc_net_game_request_drop(int pocket_slot_idx, int claimed_item, int ut_x, int ut_z) {
    PCNetGameDropRequestMsg msg;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0; /* single-player or host -- caller should proceed with the normal local
                     mutation, unmodified */
    }
    if (pocket_slot_idx < 0 || pocket_slot_idx >= mPr_POCKETS_SLOT_COUNT || ut_x < 0 || ut_x > 255 ||
        ut_z < 0 || ut_z > 255) {
        return 1; /* malformed input from the caller -- still "handled" (this process has no
                     authority to mutate locally as a client), just nothing is sent */
    }
    if (s_drop_pending.valid) {
        /* One already in flight -- defensive no-op, same posture as pickup's own equivalent check
           (see pc_net_game_request_pickup()'s doc). Unlike pickup, a menu-driven drop COULD in
           principle be re-triggered by the player before the first result arrives (the menu closes
           optimistically on send -- see the client seam in m_tag_ovl.c), so this is a real, not
           purely defensive, guard for Stage 5B-1. */
        return 1;
    }

    s_drop_pending.valid = 1;
    s_drop_pending.request_id = s_next_drop_request_id++;
    s_drop_pending.pocket_slot_idx = (uint8_t)pocket_slot_idx;
    s_drop_pending.ut_x = (uint8_t)ut_x;
    s_drop_pending.ut_z = (uint8_t)ut_z;
    s_drop_pending.claimed_item = (uint16_t)claimed_item;
    s_drop_pending.timeout_accum = 0.0f;
    s_drop_pending.retry_count = 0;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_DROP_REQUEST;
    msg.pocket_slot_idx = s_drop_pending.pocket_slot_idx;
    msg.ut_x = s_drop_pending.ut_x;
    msg.ut_z = s_drop_pending.ut_z;
    msg.claimed_item = s_drop_pending.claimed_item;
    msg.request_id = s_drop_pending.request_id;
    pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
    return 1;
}
