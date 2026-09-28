/* pc_net_game.c - Stage 1: in-game networking integration (role selection + identity handshake).
 *
 * See pc_net_game.h for the design summary. This file is the ONLY place that mixes the
 * Stage 0 transport (pc_net.h) with decomp game state -- for identity/appearance/movement it only
 * ever reads a handful of plain-integer/fixed-byte-array fields out of the currently active save
 * slot (Now_Private) to build an explicit, hand-written wire message. Stage 5A adds the first
 * exception: a client that receives a (provisional) PICKUP_RESULT grant, and has validated its own
 * pockets, writes the resulting item into Now_Private's own pockets via the real, unmodified
 * mPr_SetFreePossessionItem(), and a client that receives a DROP_RESULT and has validated that the
 * pocket slot still holds the claimed item clears it with mPr_SetPossessionItem() (see
 * pcnetgame_handle_client_pickup_result()/pcnetgame_handle_client_drop_result(); both then answer with
 * an INTERACT_CONFIRM). Protocol v2 adds the world-state writers: the host writes
 * the persistent town field (Save fg/deposit) through pcfa_set_tile()/pcfa_set_deposit() for confirmed
 * client pickups/drops and a client applies host snapshots/deltas and the host's all_grow_renew_time
 * to its own Save through the same pcfa_* API; everything else in this file remains read-only. This
 * file never touches an ACTOR, never reads pad state, and never calls into rendering/audio.
 *
 * SCOPE POLICY (intentional, do not "fix"): the TOWN field is the one shared, host-authoritative
 * world (pickup/drop below); a PRIVATE HOUSE / room inventory is local to its owner and is NOT
 * networked at all (nothing here touches any room/house inventory path).
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
 *
 * =============================================================================================
 * WORLD PROTOCOL (v2) -- summary of the multiplayer-foundation additions. Every wire struct is
 * defined further down with an exact-size and a <= PC_NET_MAX_PAYLOAD static assert; all integers
 * little-endian (asserted below). All v2 world/handshake messages are PC_NET_RELIABLE, which the
 * transport now guarantees to be reliable + ordered per peer per direction (pc_net.h).
 *
 * Handshake / town identity
 *   - Client: on transport connect the link enters HANDSHAKE but IDENTITY is NOT sent until the
 *     client's own gameplay save is loaded (pcnetgame_update_local_world_ready(): pcfa_save_ready()
 *     plus "a GAME_PLAY has run since it became ready"). IDENTITY carries the client's town
 *     identity (PCNetGameTownIdentity: land name, land id, terrain hash).
 *   - Host: a protocol-version mismatch is rejected immediately (version is always at byte offset
 *     4 of any IDENTITY-typed message). Otherwise the IDENTITY is parked per peer until the host's
 *     own save is loaded (the peer stays in HANDSHAKE -- never rejected for this), then validated:
 *     has_save==0 -> REJECT(NO_SAVE), town differs -> REJECT(LAND_MISMATCH); both carry the
 *     host's town identity (24-byte REJECT form). After ANY reject the host puts the peer in a
 *     short "closing" state (see pcnetgame_host_reject_and_close()): the transport peer stays
 *     connected, all further game messages from it are ignored and it never reaches READY or
 *     receives world data, until the reliable REJECT is acknowledged (backlog 0) or 500 ms have
 *     passed, and only then pc_net_disconnect() runs (REJECT first, DISCONNECT after). Only a match
 *     produces IDENTITY_ACK (which carries the host town identity; the client re-validates it and
 *     disconnects on mismatch) -> READY -> appearance roster -> world snapshot.
 *   - No gameplay message (MOVE/APPEARANCE/PICKUP/DROP/INTERACT_CONFIRM/PLAYER_CONTEXT/RESYNC) is processed from a
 *     peer that is not READY, and no world message is ever sent to one.
 *
 * World addressing / sequence (contract section 4)
 *   - Every field fact is (grid, acre, tile), grid 0 = town, acre = az*5+ax (0..29),
 *     tile = uz*16+ux (0..255) -- never scene-relative. Values are absolute (never deltas of
 *     values): a tile's item id + its deposit ("buried") bit.
 *   - Host keeps a STABLE SHADOW of every tile/deposit bit it has committed, plus a per-tile
 *     "known" bit. A commit (tile whose persistent value is stable and differs from the shadow, or
 *     is not yet known) updates the shadow and increments the host-global u32 world_seq (starts at
 *     0 when hosting starts, never reset during that host session). By pcfa_transient_kind():
 *     TRANSIENT values (DUMMY_* live-actor placeholders, pitfall/money-seed values) are never
 *     committed or sent -- the last committed value stays on the wire, or, if none is known yet,
 *     the tile goes out with valid=0 in FIELD_BLOCKs (client keeps its own value) and never as a
 *     FIELD_UPDATE. AMBIGUOUS values (RSV_NO, RSV_SIGNBOARD; generic over pcfa_transient_kind()):
 *       - while the HOST is in the town scene (pcfa_scene_is_town()) they are ALWAYS withheld, like
 *         TRANSIENT: live structure actors keep door-front RSV_NO placeholders there and restore
 *         EMPTY_NO on destroy, so committing them would churn on every town entry/exit. A host that
 *         stays in town forever simply never learns those tiles (valid=0 in snapshots; clients keep
 *         their own value -- same verified town). Nothing is pinned dirty and no timer runs.
 *       - while the host is NOT in town there are no live actors: a value that has stayed
 *         unchanged for PC_NETGAME_AMBIGUOUS_SETTLE_MS is a permanent footprint (or a vanilla
 *         leak) and is committed as stable.
 *     Clients never overwrite a local TRANSIENT tile (a live local actor owns it) and overwrite a
 *     local AMBIGUOUS tile only while NOT in town and after the same settle time; such host values
 *     are parked and written once the local tile frees up.
 *   - Client keeps per-acre applied_seq[30] (reset on every new connection). FIELD_UPDATE applies
 *     iff seq > applied_seq[acre]; FIELD_BLOCK applies iff seq >= applied_seq[acre]; both then set
 *     applied_seq[acre] = seq. With ordered delivery and blocks built at SEND time from the
 *     committed shadow, a block for an acre always reflects every delta sent before it, and every
 *     later delta has a larger seq -- the guard makes a stale block unable to overwrite a newer
 *     delta even if that invariant were ever broken.
 *   - Snapshot = SNAPSHOT_BEGIN{epoch, world_seq} + 30 x FIELD_BLOCK{IN_SNAPSHOT, epoch} +
 *     SNAPSHOT_END{epoch, world_seq, all_grow_renew_time}. epoch is a host-global counter, new per
 *     snapshot start; a client ignores IN_SNAPSHOT blocks / END whose epoch is not the latest BEGIN.
 *     Blocks are built one at a time at send time, paced by pc_net_reliable_backlog(). Used for
 *     initial join, late join, reconnect, client RESYNC_REQUEST, host window overflow, and post-
 *     growth resync (a flush that would need more than PC_NETGAME_FLUSH_BURST_MAX messages).
 *   - WORLD_META{world_seq, all_grow_renew_time} is sent when the host's renew time changes (to
 *     READY peers not mid-snapshot; a snapshot in progress carries the current value in its END).
 *
 * Host mutation flush (pcnetgame_host_flush_mask())
 *   - Sources: pcfa_take_dirty_acres() (C's hooks + pcfa_set_* + D's pcfa_mark_all_dirty()), a
 *     forced re-diff of every acre when the host save comes back, and a round-robin safety-net scan
 *     of ONE acre per poll (all 30 acres every 30 polls, ~0.5 s). The scan catches writers that
 *     bypass the hooks AND re-checks acres holding transient/ambiguous tiles (they are not kept
 *     dirty), so a settling tile is noticed within ~0.5 s.
 *   - Per changed acre: <= PC_NETGAME_FLUSH_DELTA_MAX changed tiles -> one FIELD_UPDATE each,
 *     otherwise one FIELD_BLOCK. Every explicit Stage 5A/5B broadcast (client pickup/drop accepted,
 *     host-local pickup notify, host-local drop landing) goes through the same function for its
 *     acre, so the dirty flush can never double-send: the shadow already holds the value.
 *
 * Shared-world interactions: two-phase reserve -> confirm -> commit (PICKUP / DROP)
 *   A client never mutates the shared field and the host never mutates it on a bare request either.
 *     1. client -> host  PICKUP_REQUEST / DROP_REQUEST (request_id, tile[, slot, claimed_item])
 *     2. host validates EVERYTHING it can (READY, IN_TOWN context, persistent-array reads, reach with
 *        finite/valid positions, classification, money-bag/unknown-item exclusion, reservation table)
 *        and, on success, RESERVES the tile for that peer and answers RESULT(accepted=1). accepted=1 is
 *        PROVISIONAL: "the tile is reserved for you; the field has NOT changed; you must send
 *        INTERACT_CONFIRM". accepted=0 is a final rejection (no reservation).
 *     3. client validates its OWN inventory (owner stamp unchanged, drop: slot still holds exactly the
 *        claimed item, pickup: a free pocket exists), applies its inventory step, and sends
 *        INTERACT_CONFIRM(kind, COMMIT) -- or, if it cannot/will not, INTERACT_CONFIRM(kind, ABORT,
 *        reason). An accepted RESULT with no matching pending request is answered ABORT(STALE).
 *     4. host COMMIT: re-reads the tile (pickup: still the reserved raw item and deposit 0; drop: still
 *        EMPTY_NO and deposit 0) and only then writes it with pcfa_set_tile() and commits through the
 *        single flush path (FIELD_UPDATE to every READY client incl. the requester). ABORT, expiry,
 *        replacement by the same peer's newer request of the SAME kind, disconnect/reset all just release the
 *        reservation. Releasing NEVER mutates the field.
 *   Host per-peer state has one record PER KIND (a pickup record and a drop record), each with phases
 *   NONE / PENDING (tile reserved) / DONE (final: committed or rejected) / ABORTED / EXPIRED. A peer can
 *   therefore hold up to TWO pending interactions at once (one pickup + one drop), which are fully
 *   independent: a new request replaces/aborts only the same peer's pending interaction of the SAME
 *   kind, matching the client, which keeps its own pending pickup and pending drop independently
 *   (aborting the other kind would let the client apply an in-flight provisional RESULT that the host
 *   has already released -> item lost or duplicated). Reservations are the PENDING entries of all
 *   peers and both kinds (no separate table): a tile reserved by any peer -- including by the SAME peer
 *   with the other kind -- is unavailable to every pickup/drop request and to the host player's own
 *   local pickup/drop (pc_net_game_field_tile_reserved()).
 *   A reservation lives at most PC_NETGAME_CONFIRM_TIMEOUT_MS (> the client's ~15 s retry budget).
 *   Retries of the same request_id replay per phase (PENDING: same provisional RESULT, timer NOT
 *   extended; DONE: cached RESULT; ABORTED/EXPIRED: RESULT(accepted=0), never a re-grant).
 *
 * Per-peer reset: every per-peer cache (identity deferral, player context, snapshot progress, dedup
 * caches + reservations) is cleared on transport connect, disconnect, reject, and acceptance; the client clears
 * all of its connection state in one function on start, connect, disconnect and shutdown.
 * =============================================================================================
 */
#include "pc_net_game.h"
#include "pc_net.h"
#include "pc_remote_player.h"
#include "pc_field_authority.h" /* v2: persistent town-field addressing/authority (Workstream C) */

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
#include "m_field_info.h"  /* Stage 5A/5B: mFI_* grid/collision helpers used by the drop search and the
                            * --pickup-test-seed fixture, mFI_UT_WORLDSIZE_* constants. (v2: world
                            * state itself is read/written through pc_field_authority.h.) */
#include "m_collision_bg.h" /* Stage 5B-1: mCoBG_CheckPlace() -- see
                             * pcnetgame_validate_and_resolve_drop(). Already extern-declared
                             * (include/m_collision_bg.h:440); no decomp changes needed to reach it. */
#include "pc_lowaddr.h"    /* PC_LOWADDR_LIMIT -- see pcnetgame_is_real_player_actor() */
#include "pc_platform.h"   /* Stage 5A.1: g_pc_pickup_test_seed -- see pcnetgame_run_pickup_test_seed().
                            * v2: g_pc_verbose (chatty-log gate), SDL_GetPerformanceCounter()
                            * (pcnetgame_now_ms()) */
#include "m_play.h"        /* v2: play_main -- see pcnetgame_update_local_world_ready() */
#include "m_npc.h"         /* villager population/is_home milestone: Animal_c, ANIMAL_NUM_MAX,
                            * mNpc_PcApplyVillagerArrival()/mNpc_PcApplyVillagerDeparture() */

#include <math.h>   /* fabsf(), isfinite() -- see pcnetgame_pos_valid() and the reach checks */
#include <stddef.h> /* offsetof() */
#include <stdio.h>
#include <string.h>

_Static_assert(PC_NETGAME_NAME_LEN == PLAYER_NAME_LEN, "PC_NETGAME_NAME_LEN must match PLAYER_NAME_LEN (m_personal_id.h)");
_Static_assert(PC_NETGAME_LAND_LEN == LAND_NAME_SIZE, "PC_NETGAME_LAND_LEN must match LAND_NAME_SIZE (m_land_h.h)");
/* v2 wire structs are copied to/from the socket byte-for-byte: little-endian hosts only. */
_Static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "pc_net_game wire format assumes a little-endian host");
/* The wire acre/tile addressing IS the save layout (pc_field_authority.h). */
_Static_assert(PCFA_ACRE_NUM == FG_BLOCK_TOTAL_NUM && PCFA_TILE_NUM == UT_TOTAL_NUM &&
                   PCFA_DEPOSIT_ROWS == UT_Z_NUM && PCFA_ACRE_NUM <= 32,
               "pcfa acre/tile constants must match the save's fg layout");
_Static_assert(sizeof(mActor_name_t) == sizeof(uint16_t), "field tile values are 16-bit on the wire");

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
                                         * world, not tied to any one request. v2: re-laid-out as
                                         * (grid, acre, tile, value, deposit, world_seq), see
                                         * PCNetGameFieldUpdateMsg. */
    PC_NETGAME_MSG_DROP_REQUEST   = 9,  /* Stage 5B-1: client -> host only. See
                                          * pcnetgame_handle_host_drop_request(). */
    PC_NETGAME_MSG_DROP_RESULT    = 10, /* Stage 5B-1: host -> the one requesting client only (never
                                          * broadcast -- PC_NETGAME_MSG_FIELD_UPDATE, reused
                                          * unchanged, is what every OTHER client needs). */
    /* ---- protocol v2 ---- */
    PC_NETGAME_MSG_PLAYER_CONTEXT = 11, /* client -> host, after READY and on change */
    PC_NETGAME_MSG_SNAPSHOT_BEGIN = 12, /* host -> one client */
    PC_NETGAME_MSG_FIELD_BLOCK    = 13, /* host -> one client (snapshot) or every READY client (flush) */
    PC_NETGAME_MSG_SNAPSHOT_END   = 14, /* host -> one client */
    PC_NETGAME_MSG_RESYNC_REQUEST = 15, /* client -> host: "send me a fresh snapshot" */
    PC_NETGAME_MSG_WORLD_META     = 16, /* host -> READY clients not mid-snapshot: renew time changed */
    PC_NETGAME_MSG_INTERACT_CONFIRM = 17, /* client -> host (8 bytes, reliable): the second phase of a
                                           * pickup/drop -- COMMIT (the client did its inventory step)
                                           * or ABORT (it did not and will not). See
                                           * PCNetGameInteractConfirmMsg and the "Shared-world
                                           * interactions" block above. Dispatched by exact size + type
                                           * (other 8-byte messages differ in the type byte). */
    /* ---- villager population / is_home milestone ---- */
    PC_NETGAME_MSG_VILLAGER_ARRIVAL    = 18, /* host -> every READY client, reliable: mNpc_Grow() +
                                              * mNpc_SetNpcHome() resolved a new villager into a slot.
                                              * See PCNetGameVillagerArrivalMsg. */
    PC_NETGAME_MSG_VILLAGER_DEPARTURE  = 19, /* host -> every READY client, reliable: mNpc_ForceRemove()
                                              * cleared a slot (and its house footprint). See
                                              * PCNetGameVillagerDepartureMsg. */
    PC_NETGAME_MSG_VILLAGER_SNAPSHOT   = 20, /* host -> one client, reliable: full villager population
                                              * sent once per snapshot (initial join / RESYNC_REQUEST /
                                              * reconnect), between the last FIELD_BLOCK and
                                              * SNAPSHOT_END. See PCNetGameVillagerSnapshotMsg. */
} PCNetGameMsgType;

typedef enum PCNetGameRejectReason {
    PC_NETGAME_REJECT_PROTOCOL_MISMATCH = 1, /* 8-byte PCNetGameRejectMsg (version-stable form) */
    PC_NETGAME_REJECT_SERVER_FULL       = 2, /* reserved, never sent */
    PC_NETGAME_REJECT_LAND_MISMATCH     = 3, /* v2: 24-byte PCNetGameRejectTownMsg (host town identity) */
    PC_NETGAME_REJECT_NO_SAVE           = 4, /* v2: 24-byte form; IDENTITY had has_save == 0 */
} PCNetGameRejectReason;

/* v2 layout. Byte offset 4 (protocol_version) and the 32-byte size are FROZEN across protocol
 * versions: a v1 host that received this would read version 2 at the same offset and REJECT it,
 * and this host reads a v1 (or any future) IDENTITY's version the same way (see
 * pcnetgame_handle_host_identity()), so an IDENTITY exchange between mismatched versions ends in an
 * explicit REJECT rather than a misparse. (A legacy v1 peer is separately detected at the transport
 * handshake and disconnected promptly -- see pc_net.c.) land_name/
 * land_id/terrain_hash are the sender's PCNetGameTownIdentity. has_save is always 1 from a v2
 * client (it defers IDENTITY until its save is loaded); 0 is rejected (NO_SAVE). */
typedef struct PCNetGameIdentityMsg {
    uint8_t  msg_type;              /* PC_NETGAME_MSG_IDENTITY */
    uint8_t  has_save;
    uint16_t _reserved0;
    uint32_t protocol_version;      /* offset 4 -- frozen */
    uint8_t  player_name[PC_NETGAME_NAME_LEN];
    uint8_t  land_name[PC_NETGAME_LAND_LEN];
    uint16_t player_id;
    uint16_t land_id;
    uint32_t terrain_hash;
} PCNetGameIdentityMsg;
_Static_assert(sizeof(PCNetGameIdentityMsg) == 32, "PCNetGameIdentityMsg wire size drifted");
_Static_assert(offsetof(PCNetGameIdentityMsg, protocol_version) == 4, "IDENTITY protocol_version offset is frozen");
/* Every wire message below carries TWO compile-time checks, not one: the exact-size assert pins
 * this module's own sub-protocol layout (catches accidental padding/field drift between builds),
 * while this second `<= PC_NET_MAX_PAYLOAD` assert pins the TRANSPORT's hard ceiling (PC_NET_MAX_PAYLOAD, pc_net.h).
 * pc_net_send() returns 0 for any payload above that ceiling and the receive path discards one
 * as malformed (see pc_net.c) -- so a future message that outgrows it
 * would otherwise compile cleanly and then just never arrive, with no error at either end. The
 * exact-size assert alone does not prevent this: whoever grows a struct simply updates its pinned
 * number too. Failing the build here is the only place that mistake is guaranteed to surface. */
_Static_assert(sizeof(PCNetGameIdentityMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameIdentityMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* v2 layout (same 32-byte size as v1). Only ever sent after the host validated the client's town,
 * so land_name/land_id/terrain_hash (the host's PCNetGameTownIdentity) must equal what the client
 * claimed in its IDENTITY -- the client re-checks and disconnects otherwise
 * (pcnetgame_handle_client_identity_ack()). */
typedef struct PCNetGameIdentityAckMsg {
    uint8_t  msg_type;              /* PC_NETGAME_MSG_IDENTITY_ACK */
    uint8_t  accepted;              /* always 1 here; a rejection is a separate message (below) */
    uint16_t assigned_peer_id;      /* the PCNetPeerId pc_net.c assigned this connection, widened */
    uint32_t protocol_version;      /* host's own version -- the client requires it to equal its own */
    uint8_t  player_name[PC_NETGAME_NAME_LEN]; /* host's identity, so the client knows who it reached */
    uint8_t  land_name[PC_NETGAME_LAND_LEN];
    uint16_t player_id;
    uint16_t land_id;
    uint32_t terrain_hash;
} PCNetGameIdentityAckMsg;
_Static_assert(sizeof(PCNetGameIdentityAckMsg) == 32, "PCNetGameIdentityAckMsg wire size drifted");
_Static_assert(sizeof(PCNetGameIdentityAckMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameIdentityAckMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* Version-stable 8-byte form, used for PROTOCOL_MISMATCH so that a client of ANY version (whose
 * dispatch expects exactly this layout) can report why it was refused. */
typedef struct PCNetGameRejectMsg {
    uint8_t  msg_type;              /* PC_NETGAME_MSG_REJECT */
    uint8_t  reason;                /* PCNetGameRejectReason */
    uint16_t _reserved;
    uint32_t expected_protocol_version;
} PCNetGameRejectMsg;
_Static_assert(sizeof(PCNetGameRejectMsg) == 8, "PCNetGameRejectMsg wire size drifted");
_Static_assert(sizeof(PCNetGameRejectMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameRejectMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* v2: 24-byte REJECT form (same msg_type, same first 8 bytes as PCNetGameRejectMsg) used for
 * LAND_MISMATCH / NO_SAVE. Carries the HOST's town identity so a client or test harness can report
 * exactly what it mismatched against. Only a same-version peer can receive it (a version mismatch is
 * always rejected first, with the 8-byte form). */
typedef struct PCNetGameRejectTownMsg {
    uint8_t  msg_type;              /* PC_NETGAME_MSG_REJECT */
    uint8_t  reason;                /* PC_NETGAME_REJECT_LAND_MISMATCH / _NO_SAVE */
    uint16_t _reserved0;
    uint32_t expected_protocol_version;
    uint8_t  land_name[PC_NETGAME_LAND_LEN]; /* host PCNetGameTownIdentity */
    uint16_t land_id;
    uint16_t _reserved1;
    uint32_t terrain_hash;
} PCNetGameRejectTownMsg;
_Static_assert(sizeof(PCNetGameRejectTownMsg) == 24, "PCNetGameRejectTownMsg wire size drifted");
_Static_assert(sizeof(PCNetGameRejectTownMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameRejectTownMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

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
 * accepted == 0.
 * Two-phase semantics: accepted == 1 is PROVISIONAL -- the tile is RESERVED for this client, the
 * field has NOT changed yet, and the client must answer with INTERACT_CONFIRM. accepted == 0 is a
 * final rejection (no reservation, nothing to confirm). The item is resolved (incl. the present RNG)
 * ONCE at request time; retries replay the same value. */
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

/* v2 (replaces the v1 8-byte scene-relative ut_x/ut_z form): host -> every READY client. The
 * authoritative, ABSOLUTE state of one persistent town tile after a committed change -- item id
 * plus (when DEPOSIT_VALID) its buried bit. Never a transient value. world_seq is the host-global
 * sequence number this commit was assigned; the client applies it only if world_seq >
 * applied_seq[acre] (see the WORLD PROTOCOL block). No request_id: this is a statement about shared
 * world state (the original requester also gets its own unicast PICKUP/DROP_RESULT). */
#define PC_NETGAME_FU_FLAG_DEPOSIT_VALID 0x01u
#define PC_NETGAME_FU_FLAG_DEPOSIT_ON    0x02u
typedef struct PCNetGameFieldUpdateMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_FIELD_UPDATE */
    uint8_t  grid;          /* PC_NETGAME_GRID_TOWN (only value this phase) */
    uint8_t  acre;          /* 0..29 */
    uint8_t  tile;          /* 0..255 */
    uint8_t  flags;         /* PC_NETGAME_FU_FLAG_* (this host always sends DEPOSIT_VALID) */
    uint8_t  _reserved0;
    uint16_t value;         /* mActor_name_t */
    uint32_t world_seq;
} PCNetGameFieldUpdateMsg;
_Static_assert(sizeof(PCNetGameFieldUpdateMsg) == 12, "PCNetGameFieldUpdateMsg wire size drifted");
_Static_assert(sizeof(PCNetGameFieldUpdateMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameFieldUpdateMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

#define PC_NETGAME_GRID_TOWN 0u /* the only grid this phase (island / house floors are out of scope) */

/* v2: client -> host, reliable. Sent right after READY and again whenever the per-frame comparison
 * in pcnetgame_client_tick() sees it change. See PCNetPlayerContext (pc_net_game.h). */
typedef struct PCNetGamePlayerContextMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_PLAYER_CONTEXT */
    uint8_t  player_no;
    uint8_t  destiny_type;
    uint8_t  flags;         /* PC_NETGAME_CTX_FLAG_* */
    int16_t  money_power;
    int16_t  goods_power;
} PCNetGamePlayerContextMsg;
_Static_assert(sizeof(PCNetGamePlayerContextMsg) == 8, "PCNetGamePlayerContextMsg wire size drifted");
_Static_assert(sizeof(PCNetGamePlayerContextMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGamePlayerContextMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* v2: lbRTC_time_c (OSRTCTime) spelled out field by field -- never memcpy'd from the decomp struct.
 * The "clear code" (all 0xFF / year 0xFFFF) is copied verbatim like any other value. */
typedef struct PCNetGameRtcWire {
    uint8_t  sec;
    uint8_t  min;
    uint8_t  hour;
    uint8_t  day;
    uint8_t  weekday;
    uint8_t  month;
    uint16_t year;
} PCNetGameRtcWire;
_Static_assert(sizeof(PCNetGameRtcWire) == 8, "PCNetGameRtcWire wire size drifted");

/* v2: host -> one client, reliable. Opens snapshot `epoch`. world_seq = host world_seq when the
 * snapshot started (informational; each block carries its own build-time seq). */
typedef struct PCNetGameSnapshotBeginMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_SNAPSHOT_BEGIN */
    uint8_t  grid;          /* PC_NETGAME_GRID_TOWN */
    uint8_t  acre_count;    /* number of FIELD_BLOCKs this snapshot will contain (30) */
    uint8_t  _reserved0;
    uint32_t epoch;
    uint32_t world_seq;
} PCNetGameSnapshotBeginMsg;
_Static_assert(sizeof(PCNetGameSnapshotBeginMsg) == 12, "PCNetGameSnapshotBeginMsg wire size drifted");
_Static_assert(sizeof(PCNetGameSnapshotBeginMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameSnapshotBeginMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* v2: one whole persistent town acre, absolute. Host -> one client inside a snapshot
 * (flags IN_SNAPSHOT, epoch = that snapshot) or host -> every READY client from the mutation flush
 * (flags 0, epoch 0) when one acre changed too much for per-tile FIELD_UPDATEs. Built at SEND time
 * from the committed stable shadow (a tile that is transient right now carries its last committed
 * value). world_seq = host world_seq at build time; applied iff world_seq >= applied_seq[acre].
 * items[] is in tile order (uz*16+ux); deposit[uz] bit (1<<ux) = buried -- identical to Save
 * fg/deposit. valid[uz] bit (1<<ux) = the host KNOWS a stable value for that tile; a tile whose only
 * value the host has ever seen is transient (e.g. a DUMMY_* structure placeholder because the host
 * was already in town when hosting started) is sent with valid=0 and its items/deposit bits are
 * meaningless -- the client leaves its own local value untouched (both sides are identity-verified
 * to hold the same town, so the client's own copy is right). */
#define PC_NETGAME_FB_FLAG_IN_SNAPSHOT 0x01u
typedef struct PCNetGameFieldBlockMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_FIELD_BLOCK */
    uint8_t  grid;          /* PC_NETGAME_GRID_TOWN */
    uint8_t  acre;          /* 0..29 */
    uint8_t  flags;         /* PC_NETGAME_FB_FLAG_* */
    uint32_t epoch;
    uint32_t world_seq;
    uint16_t items[PCFA_TILE_NUM];
    uint16_t deposit[PCFA_DEPOSIT_ROWS];
    uint16_t valid[PCFA_DEPOSIT_ROWS];
} PCNetGameFieldBlockMsg;
_Static_assert(sizeof(PCNetGameFieldBlockMsg) == 588, "PCNetGameFieldBlockMsg wire size drifted");
_Static_assert(sizeof(PCNetGameFieldBlockMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameFieldBlockMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* Weather + Stalk Market milestone: the smallest wire representation of both domains' shared,
 * persistent, host-authoritative state -- reused as-is by both PCNetGameWorldMetaMsg (change
 * notification) and PCNetGameSnapshotEndMsg (initial/late-join/reconnect sync), exactly like
 * renew_time (PCNetGameRtcWire) already is above. Weather is Common_Get(weather) and
 * Common_Get(weather_intensity) (mEnv_WEATHER_... and mEnv_WEATHER_INTENSITY_... values -- see
 * m_kankyo_weather.c_inc); the Stalk Market fields mirror Save_Get(kabu_price_schedule) (Kabu_price_c,
 * m_kabu_manager.h) field-for-field, including its own kabu_update_time so a client's
 * Kabu_manager()-style "has this week already been set" question (never asked directly -- clients
 * never call Kabu_manager() at all, see its host-authority gate) is still answerable from the state
 * this carries. Deliberately NOT a new persisted representation: both sides are the exact same
 * Save_t/Common_t fields the vanilla single-player code already reads and writes -- see
 * pcnetgame_client_apply_weather_state()/pcnetgame_client_apply_market_state() below. Turnip
 * INVENTORY (a player's own held turnips) is never part of this -- it stays in that player's own
 * private Save_t, exactly as before; only the shared market PRICE schedule is here. */
typedef struct PCNetGameWorldStateWire {
    uint16_t daily_price[7];        /* Kabu_price_c.daily_price[lbRTC_SUNDAY..lbRTC_SATURDAY] */
    uint16_t trade_market;          /* Kabu_price_c.trade_market (Kabu_TRADE_MARKET_TYPE_*) */
    PCNetGameRtcWire kabu_update_time; /* Kabu_price_c.update_time */
    uint8_t  weather;               /* Common_Get(weather), an mEnv_WEATHER_... value */
    uint8_t  weather_intensity;     /* Common_Get(weather_intensity), an mEnv_WEATHER_INTENSITY_... value */
    uint8_t  _reserved0[2];
} PCNetGameWorldStateWire;
_Static_assert(sizeof(PCNetGameWorldStateWire) == 28, "PCNetGameWorldStateWire wire size drifted");

/* v2: host -> one client, reliable. Closes snapshot `epoch`; the client applies renew_time (the
 * host's Save all_grow_renew_time, read at send time) atomically here, then counts the world as
 * synced. world_seq = host world_seq at send time. Weather + Stalk Market milestone: also carries
 * world_state (WEATHER_VALID/MARKET_VALID in flags) so a fresh or late-joining client gets both
 * domains' current authoritative state in the same message that already closes its snapshot --
 * see the "initial state on join" / "late join" requirements this satisfies. */
#define PC_NETGAME_META_FLAG_RENEW_TIME_VALID 0x01u
#define PC_NETGAME_META_FLAG_WEATHER_VALID    0x02u
#define PC_NETGAME_META_FLAG_MARKET_VALID     0x04u
typedef struct PCNetGameSnapshotEndMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_SNAPSHOT_END */
    uint8_t  grid;          /* PC_NETGAME_GRID_TOWN */
    uint8_t  acre_count;    /* number of FIELD_BLOCKs actually sent in this snapshot */
    uint8_t  flags;         /* PC_NETGAME_META_FLAG_* */
    uint32_t epoch;
    uint32_t world_seq;
    PCNetGameRtcWire renew_time;
    PCNetGameWorldStateWire world_state;
} PCNetGameSnapshotEndMsg;
_Static_assert(sizeof(PCNetGameSnapshotEndMsg) == 48, "PCNetGameSnapshotEndMsg wire size drifted");
_Static_assert(sizeof(PCNetGameSnapshotEndMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameSnapshotEndMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* Villager population/is_home milestone.
 *
 * is_home design note (Part 2/6 of the milestone): Animal_c.is_home's ~8 runtime write sites
 * (ac_set_npc_manager.c, ac_npc_action.c_inc, ac_npc2_action.c_inc, ac_npc2_think_into_room.c_inc,
 * ac_npc_act_leave_house.c_inc) are all inside the per-NPC_ACTOR schedule/AI state machine -- the
 * SAME unsynced, explicitly out-of-scope "villager movement/animation" system this milestone must
 * NOT touch. That AI already runs independently and unsynced on host and every client (matching
 * vanilla single-player; no prior milestone changed this), so is_home's live, continuously-oscillating
 * value cannot be kept authoritatively in sync without also syncing the schedule/position state that
 * drives it -- doing so here would silently become a movement-sync feature. Two things make this
 * safe to scope down rather than a blocking problem: (1) pc_save_write_authoritative()
 * (pc_m_card.c) already unconditionally rejects every save write from a network CLIENT, so a
 * client's locally-AI-driven is_home value can NEVER reach persisted Save_t regardless of what this
 * module does; (2) the only is_home write sites that ARE genuine population-lifecycle state (not AI
 * schedule) are mNpc_ClearAnimalInfo()'s/mNpc_ClearIslandAnimalInfo()'s `is_home = TRUE` reset
 * (m_npc.c), which both ARRIVAL and a fresh/late-join snapshot already reproduce deterministically
 * (mNpc_PcApplyVillagerArrival() calls mNpc_ClearAnimalInfo() first). So: is_home is synchronized
 * host->client at population-change time (implicitly, via the deterministic TRUE reset) and at
 * snapshot/join time (explicitly, PCNetGameVillagerSlotWire.is_home, a best-effort read of the
 * host's current value at snapshot-build time) -- but is NOT a live, continuously-updated channel.
 * This is the full extent of what "host-authoritative is_home" can mean without expanding this
 * milestone into villager movement sync. */

/* v2: host -> every READY client, reliable, world_seq-stamped. A new villager grew into `slot` via
 * mNpc_Grow()+mNpc_SetNpcHome() (m_npc.c) -- see mNpc_PcApplyVillagerArrival() for the exact,
 * deterministic client-side replay this drives (no RNG re-rolled). reserved_ut_x/reserved_ut_z are
 * the RAW reserved-house-slot coordinates (Anmhome_c, before mNpc_SetNpcHome()'s ut_z+1 adjustment),
 * matching exactly what the host itself passed into mNpc_BuildHouseBeforeFieldct(). now_npc_max is
 * the authoritative Save_t.now_npc_max value AFTER this arrival (applied directly, never
 * incremented client-side, to rule out counter drift from a missed/duplicate message). */
typedef struct PCNetGameVillagerArrivalMsg {
    uint8_t  msg_type;          /* PC_NETGAME_MSG_VILLAGER_ARRIVAL */
    uint8_t  slot;               /* Save_t.animals[] index, 0..ANIMAL_NUM_MAX-1 */
    uint8_t  reserved_block_x;
    uint8_t  reserved_block_z;
    uint8_t  reserved_ut_x;
    uint8_t  reserved_ut_z;
    uint8_t  now_npc_max;
    uint8_t  _reserved0;
    uint16_t npc_id;              /* mActor_name_t: the host-chosen villager identity */
    uint16_t _reserved1;
    uint32_t world_seq;
} PCNetGameVillagerArrivalMsg;
_Static_assert(sizeof(PCNetGameVillagerArrivalMsg) == 16, "PCNetGameVillagerArrivalMsg wire size drifted");
_Static_assert(sizeof(PCNetGameVillagerArrivalMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameVillagerArrivalMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* v2: host -> every READY client, reliable, world_seq-stamped. mNpc_ForceRemove() (m_npc.c) cleared
 * `slot` -- both its Animal_c reset AND its 3x3 house-footprint teardown (Save_t.fg, via
 * mNpc_DestroyHouse()) are already complete on the host by the time this is sent (see
 * pc_net_game_notify_villager_departure()'s call site in m_npc.c). mNpc_PcApplyVillagerDeparture()
 * reproduces both mutations, in the same order, from the slot's own (still-present) home_info --
 * never a separate tile-diff protocol; see the field-atomicity note on that function. now_npc_max is
 * the authoritative Save_t.now_npc_max value AFTER this departure (applied directly, never
 * decremented client-side). */
typedef struct PCNetGameVillagerDepartureMsg {
    uint8_t  msg_type;    /* PC_NETGAME_MSG_VILLAGER_DEPARTURE */
    uint8_t  slot;
    uint8_t  now_npc_max;
    uint8_t  _reserved0;
    uint32_t world_seq;
} PCNetGameVillagerDepartureMsg;
_Static_assert(sizeof(PCNetGameVillagerDepartureMsg) == 8, "PCNetGameVillagerDepartureMsg wire size drifted");
_Static_assert(sizeof(PCNetGameVillagerDepartureMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameVillagerDepartureMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* One Save_t.animals[] slot's minimal reconstructable state, as carried inside
 * PCNetGameVillagerSnapshotMsg. occupied = 0 means every other field is meaningless (the client
 * clears/leaves the slot empty). is_home is a best-effort read of the host's current value at
 * snapshot-build time -- see the is_home design note above, this is NOT a live channel.
 * home_block_x/home_block_z/home_ut_x/home_ut_z mirror Animal_c.home_info directly (the FINAL,
 * already-adjusted values, i.e. AFTER mNpc_SetNpcHome()'s ut_z+1) -- unlike
 * PCNetGameVillagerArrivalMsg's reserved_ut_x/reserved_ut_z, which are the RAW pre-adjustment
 * Anmhome_c values because that message must also drive mNpc_BuildHouseBeforeFieldct() on the
 * client. The snapshot never needs to rebuild a house (see pcnetgame_build_villager_snapshot(): a
 * snapshot never runs mNpc_BuildHouseBeforeFieldct() again, since the house already exists in the
 * persistent field this same snapshot's FIELD_BLOCKs cover), so it carries the simpler,
 * already-final form; the client applies it by subtracting 1 before calling
 * mNpc_PcApplyVillagerArrival() -- see pcnetgame_handle_client_villager_snapshot(). */
typedef struct PCNetGameVillagerSlotWire {
    uint16_t npc_id;
    uint8_t  occupied;
    uint8_t  is_home;
    uint8_t  home_block_x;
    uint8_t  home_block_z;
    uint8_t  home_ut_x;
    uint8_t  home_ut_z;
} PCNetGameVillagerSlotWire;
_Static_assert(sizeof(PCNetGameVillagerSlotWire) == 8, "PCNetGameVillagerSlotWire wire size drifted");

/* v2: host -> one client, reliable. Sent once per snapshot sequence (initial join / RESYNC_REQUEST /
 * reconnect), between the last FIELD_BLOCK and SNAPSHOT_END (see pcnetgame_host_pump_snapshots()'s
 * snap_stage 2) -- late-join/reconnect coverage for Save_t.animals[], which FIELD_BLOCK/SNAPSHOT_END
 * never carry (they cover Save_t.fg only). world_seq/epoch follow the exact same semantics as every
 * other snapshot message; the client applies this iff world_seq >= its single last-applied
 * s_client_population_seq (matching FIELD_BLOCK's >= rule, since a full snapshot always supersedes
 * anything older, not WORLD_META's strict >). now_npc_max is the authoritative population count at
 * snapshot-build time. */
typedef struct PCNetGameVillagerSnapshotMsg {
    uint8_t  msg_type;   /* PC_NETGAME_MSG_VILLAGER_SNAPSHOT */
    uint8_t  grid;       /* PC_NETGAME_GRID_TOWN */
    uint8_t  now_npc_max;
    uint8_t  _reserved0;
    uint32_t epoch;
    uint32_t world_seq;
    PCNetGameVillagerSlotWire slots[15]; /* ANIMAL_NUM_MAX; spelled out numerically to keep this
                                          * header/struct decomp-independent like every other wire
                                          * struct in this file -- pcnetgame_build_villager_snapshot()
                                          * _Static_assert's this matches ANIMAL_NUM_MAX exactly. */
} PCNetGameVillagerSnapshotMsg;
_Static_assert(sizeof(PCNetGameVillagerSnapshotMsg) == 132, "PCNetGameVillagerSnapshotMsg wire size drifted");
_Static_assert(sizeof(PCNetGameVillagerSnapshotMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameVillagerSnapshotMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");
_Static_assert(ANIMAL_NUM_MAX == 15, "PCNetGameVillagerSnapshotMsg.slots[15] no longer matches ANIMAL_NUM_MAX");

/* v2: host -> every READY client that is NOT mid-snapshot, reliable, when the host's Save
 * all_grow_renew_time changes (daily renewal), OR (weather + Stalk Market milestone) when
 * Common_Get(weather)/Common_Get(weather_intensity) or Save_Get(kabu_price_schedule) changes.
 * world_seq is the SAME shared committed sequence counter already used for renew time (and for
 * FIELD_UPDATE/FIELD_BLOCK) -- a change to any of renew time/weather/market bumps it once and
 * (re)sends this message with only the sub-state(s) that actually changed VALID-flagged, but always
 * carrying every sub-state's current value (so a client that applies it also picks up anything it
 * had missed). The client applies each VALID sub-state iff world_seq > its single last-applied
 * s_client_meta_seq (a SNAPSHOT_END with world_seq >= that also applies, same as today). Reusing one
 * counter (rather than inventing a second/third per sub-state) keeps this exactly the same
 * "monotonic seq, missed packet self-corrects on the next update" shape the field-update/renew-time
 * protocol already established, without a bigger generic multi-stream sequencing abstraction that
 * only these three fields would ever use. */
typedef struct PCNetGameWorldMetaMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_WORLD_META */
    uint8_t  flags;         /* PC_NETGAME_META_FLAG_* */
    uint16_t _reserved0;
    uint32_t world_seq;
    PCNetGameRtcWire renew_time;
    PCNetGameWorldStateWire world_state;
} PCNetGameWorldMetaMsg;
_Static_assert(sizeof(PCNetGameWorldMetaMsg) == 44, "PCNetGameWorldMetaMsg wire size drifted");
_Static_assert(sizeof(PCNetGameWorldMetaMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameWorldMetaMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* v2: client -> host, reliable. Asks for a fresh snapshot (the host starts a new epoch for this
 * peer). Sent when the client's save went away and came back while connected (the Save may have
 * been reloaded from disk) or world messages had to be discarded because the save was not loaded.
 * reason is informational only. */
#define PC_NETGAME_RESYNC_REASON_SAVE_RELOADED 1u
typedef struct PCNetGameResyncRequestMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_RESYNC_REQUEST */
    uint8_t  grid;          /* PC_NETGAME_GRID_TOWN */
    uint8_t  reason;
    uint8_t  _reserved0;
} PCNetGameResyncRequestMsg;
_Static_assert(sizeof(PCNetGameResyncRequestMsg) == 4, "PCNetGameResyncRequestMsg wire size drifted");
_Static_assert(sizeof(PCNetGameResyncRequestMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameResyncRequestMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

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
 * the field. Meaningless when accepted == 0.
 * Two-phase semantics (same as PCNetGamePickupResultMsg): accepted == 1 is PROVISIONAL -- the tile is
 * RESERVED, the field has NOT changed, and placed_item is what the host WILL place on COMMIT. The
 * client clears its pocket slot only after re-validating that the slot still holds exactly
 * claimed_item, then sends INTERACT_CONFIRM. */
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

/* Two-phase interactions: client -> host, reliable, exactly 8 bytes (little-endian, natural
 * alignment). Answers a provisional RESULT (accepted == 1) -- or withdraws from a request the client
 * gave up on / lost state for. The host only acts on it if (peer READY, kind matches, request_id ==
 * that peer's PENDING request of that kind); anything else is ignored with no mutation.
 *   kind     PC_NETGAME_INTERACT_KIND_PICKUP (1) / _DROP (2)
 *   outcome  PC_NETGAME_CONFIRM_COMMIT (1): the client did its inventory step (drop: cleared the slot;
 *            pickup: stored the item) -- the host now commits the field write.
 *            PC_NETGAME_CONFIRM_ABORT (0): the client did NOT and will not -- the host releases the
 *            reservation and never mutates the field.
 *   reason   PC_NETGAME_CONFIRM_REASON_* (informational; 0 for a COMMIT) */
#define PC_NETGAME_INTERACT_KIND_PICKUP 1u
#define PC_NETGAME_INTERACT_KIND_DROP   2u
#define PC_NETGAME_CONFIRM_ABORT  0u
#define PC_NETGAME_CONFIRM_COMMIT 1u
#define PC_NETGAME_CONFIRM_REASON_NONE          0u
#define PC_NETGAME_CONFIRM_REASON_POCKETS_FULL  1u /* pickup: no free pocket slot */
#define PC_NETGAME_CONFIRM_REASON_SLOT_CHANGED  2u /* drop: the slot no longer holds the claimed item */
#define PC_NETGAME_CONFIRM_REASON_STATE_CHANGED 3u /* the local player / save changed, or the RESULT disagreed with the request */
#define PC_NETGAME_CONFIRM_REASON_STALE         4u /* an accepted RESULT with no matching pending request */
#define PC_NETGAME_CONFIRM_REASON_CANCELLED     5u /* the client gave up / cancelled the request */
typedef struct PCNetGameInteractConfirmMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_INTERACT_CONFIRM */
    uint8_t  kind;          /* PC_NETGAME_INTERACT_KIND_* */
    uint8_t  outcome;       /* PC_NETGAME_CONFIRM_COMMIT / _ABORT */
    uint8_t  reason;        /* PC_NETGAME_CONFIRM_REASON_* */
    uint32_t request_id;    /* the request being confirmed/aborted */
} PCNetGameInteractConfirmMsg;
_Static_assert(sizeof(PCNetGameInteractConfirmMsg) == 8, "PCNetGameInteractConfirmMsg wire size drifted");
_Static_assert(sizeof(PCNetGameInteractConfirmMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameInteractConfirmMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* How long the host holds a PENDING reservation while waiting for the client's INTERACT_CONFIRM.
 * Must exceed the client's own give-up horizon (PC_NETGAME_PICKUP/DROP_MAX_RETRIES x ~500 ms, ~15.5 s
 * incl. the last interval) so a slow-but-alive client can still confirm, yet be bounded so a dead
 * client can never pin a tile forever. Timestamps are unsigned 32-bit ms compared by subtraction
 * (wrap-safe). Expiry only ever RELEASES a reservation -- it never mutates the field. */
#define PC_NETGAME_CONFIRM_TIMEOUT_MS 20000u

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

/* Any peer-supplied world coordinate must be finite and within +/- this many world units. The whole
 * town is ~6400 x ~7680 units, so 100000 is generous slack for a legitimate sample while making NaN/
 * Inf/1e30 (which make a naive `dist > MAX` reach check FAIL OPEN, since every comparison with NaN
 * is false) unrepresentable -- see pcnetgame_pos_valid(). */
#define PC_NETGAME_POS_ABS_LIMIT 100000.0f

/* A MOVE sample's speed must satisfy |speed| <= this. The real player's speeds are all in the
 * single digits (walk/run/dash/bounce peak around 13-26; the remote avatar's animation tempo is
 * sqrtf(speed / 7.5) -- see pc_remote_player_mv()), so 1000 is enormously generous while keeping a
 * finite-but-absurd value (1e30) from driving the receiver's animation frame control to huge/inf
 * frames. Enforced at both the host ingest and the client relay ingest. */
#define PC_NETGAME_SPEED_ABS_LIMIT 1000.0f

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

/* Stage 4C-1 (one-shot-UDP-loss fix): low-frequency periodic appearance resend. (Originally added
 * when PC_NET_RELIABLE did not retransmit; the transport is now reliable + ordered per peer, so
 * this is a redundant, harmless safety net that only costs a little window space -- kept
 * unchanged on purpose.) Appearance is otherwise sent only a handful of times total (once at
 * READY, once per newcomer backfill). This is deliberately NOT a generic reliability layer: no
 * ack, no sequence number, no per-message retry/timeout bookkeeping -- just an unconditional,
 * idempotent full resend every few seconds, exactly like a coarse heartbeat. Whether or not the appearance actually changed since the last
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

/* Host phase of one per-peer interaction record (see PCNetGameHostInteraction). */
typedef enum PCNetGameHostPhase {
    PC_NETGAME_PHASE_NONE = 0, /* no record: this peer never sent (or was reset of) a request of this kind */
    PC_NETGAME_PHASE_PENDING,  /* provisionally accepted: the tile is RESERVED, the field is unchanged, awaiting CONFIRM */
    PC_NETGAME_PHASE_DONE,     /* final. accepted == 1: committed (field written); accepted == 0: rejected */
    PC_NETGAME_PHASE_ABORTED,  /* was PENDING; released by the client's ABORT / a newer request of the same kind / a peer reset */
    PC_NETGAME_PHASE_EXPIRED,  /* was PENDING; released because no CONFIRM arrived within PC_NETGAME_CONFIRM_TIMEOUT_MS */
} PCNetGameHostPhase;

/* Stage 5A/5B + two-phase interactions: host-only, one PICKUP record and one DROP record per pc_net
 * peer (same indexing as s_host_peer_link), in s_host_pickup_state[] / s_host_drop_state[]. Each is
 * deliberately NOT a queue -- a player can only ever have one pickup (or drop) in flight (the decomp
 * state machine itself serializes this; see m_player_main_pickup.c_inc), so "the most recently
 * PROCESSED request of this kind from this peer, and exactly what we decided" is all that's ever
 * needed. The two kinds are kept in separate arrays (a retry of one must never replay the other's
 * decision) and are fully independent: a peer may have up to TWO pending interactions (one pickup and
 * one drop), exactly like the client's independent s_pickup_pending / s_drop_pending. A new request
 * of a kind aborts ONLY the same peer's pending interaction of that same kind (the client gave up on
 * it); the other kind's pending interaction is left alone. Two pendings of one peer can never hold
 * the same tile: the reservation check rejects the second one (a pickup reserves a tile holding an
 * item, a drop an EMPTY tile, and either way the tile is already reserved).
 *
 * Retry (same request_id) replays per phase instead of re-validating against now-changed ground
 * truth (a committed original has already changed the tile, so a naive re-check would wrongly
 * reject a request that actually succeeded; a released reservation must never be re-granted):
 *   PENDING          replay the cached provisional RESULT (reservation stays; timer NOT extended)
 *   DONE             replay the cached RESULT (accepted as cached -- harmless to a client that
 *                    already committed: it answers a stale accepted RESULT with ABORT(STALE), which
 *                    the host ignores because the phase is no longer PENDING)
 *   ABORTED/EXPIRED  RESULT(accepted=0)
 * Phase transitions (only these):
 *   NONE/any -> PENDING   new request validated OK (reserve; field NOT touched)
 *   NONE/any -> DONE(0)   new request rejected (no reservation)
 *   PENDING  -> DONE(1)   CONFIRM(COMMIT) and the re-validated tile write succeeded
 *   PENDING  -> ABORTED   CONFIRM(ABORT) / COMMIT whose re-validation failed (no mutation) /
 *                         replaced by a newer request of this peer and kind / (record cleared on peer reset)
 *   PENDING  -> EXPIRED   PC_NETGAME_CONFIRM_TIMEOUT_MS elapsed without a CONFIRM
 * The reservation table IS the set of PENDING records over all peers (acre + tile): a tile reserved
 * by any peer is unavailable to every other pickup/drop request and to the host's own local
 * pickup/drop (pc_net_game_field_tile_reserved()). Everything is cleared by
 * pcnetgame_reset_all_host_peer_state() -- on disconnect and defensively on connect/READY -- so a
 * reused pc_net peer slot never inherits a previous connection's record or reservation. */
typedef struct PCNetGameHostInteraction {
    uint32_t request_id;        /* the request this record answers */
    uint32_t reserved_since_ms; /* pcnetgame_now_ms() when it became PENDING (unsigned, wrap-safe compare) */
    uint16_t item;              /* pickup: resolved granted item; drop: item to place */
    uint16_t raw_item;          /* pickup: the raw field value observed at reservation; drop: unused */
    uint8_t  phase;             /* PCNetGameHostPhase */
    uint8_t  accepted;          /* the cached provisional/final decision (1 = accepted) */
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint8_t  acre;              /* persistent address of the reserved tile (meaningful when accepted) */
    uint8_t  tile;
    uint8_t  prev_valid;        /* 1: prev_request_id is the request this record REPLACED after that one's
                                 * reservation was released (ABORTED/EXPIRED). A retry of exactly that id is
                                 * answered accepted=0 instead of being processed as a new request -- so a
                                 * released reservation cannot be re-granted by a late retry of the
                                 * request that a newer one replaced. (Only the immediate predecessor is
                                 * remembered; an older id is indistinguishable from a new request, which
                                 * is harmless: a real client answers such a grant ABORT(STALE).) */
    uint32_t prev_request_id;
} PCNetGameHostInteraction;
static PCNetGameHostInteraction s_host_pickup_state[PC_NET_MAX_PEERS];

/* Client-only OWNER STAMP: who the local player was when a pickup/drop request was sent. A late
 * RESULT must never be applied to a different inventory (quit to title and load another save, another
 * resident of the same town, ...), so every accepted RESULT is applied only if the stamp taken NOW is
 * byte-equal to the one recorded at send time (pcnetgame_owner_stamp_matches()). Built field by field
 * from Common player_no and Now_Private->player_ID (land id + player id + both name blobs) into a
 * zeroed struct, so a plain memcmp is exact (no indeterminate padding). */
typedef struct PCNetGameOwnerStamp {
    uint16_t player_id;
    uint16_t land_id;
    uint8_t  player_no;
    uint8_t  player_name[PC_NETGAME_NAME_LEN];
    uint8_t  land_name[PC_NETGAME_LAND_LEN];
    uint8_t  _pad;
} PCNetGameOwnerStamp;

/* Stage 5A: client-only. Exactly one outstanding pickup request at a time, for the same reason as
 * s_host_pickup_state above -- the local pickup animation state can't request a second one before
 * the first resolves. request_id is this connection's own counter, reset to 1 on every fresh
 * pc_net_game_start_client() (see there) so it can never collide with a previous, unrelated
 * connection's in-flight id on the host side. `valid` is cleared the moment the RESULT (accepted or
 * not) is received -- so no retry can ever be sent after the RESULT arrived -- and on cancel/give-up. */
typedef struct PCNetGamePickupPending {
    int      valid;
    uint32_t request_id;
    uint8_t  ut_x;
    uint8_t  ut_z;
    PCNetGameOwnerStamp owner; /* the local player at send time (see PCNetGameOwnerStamp) */
    float    timeout_accum;  /* frames-as-60fps-units since this attempt (send or last retry) --
                               * see PC_NETGAME_PICKUP_TIMEOUT_60FPS_FRAMES */
    int      retry_count;    /* number of retries already sent for this request_id */
    int      unsent;         /* 1: the FIRST send failed (window full); the next poll's "retry" block sends
                              * it, and that send is NOT counted in retry_count */
} PCNetGamePickupPending;
static PCNetGamePickupPending s_pickup_pending;
static uint32_t               s_next_pickup_request_id = 1;

/* ~500ms: generous relative to a LAN round-trip (movement/appearance already prove typical
 * latency is well under this), but short enough that a genuinely lost request/result is retried
 * quickly rather than leaving the pickup animation waiting. The transport is now reliable, so a
 * retry is normally redundant -- but the RESULT can legitimately arrive late (queued behind a
 * world snapshot, or held back by a full reliable window), and giving up before it does makes a
 * late accepted RESULT be ignored (a lost item on pickup, a duplicated item on drop). So the total
 * budget deliberately exceeds the transport's own worst case (retransmit budget ~9 s, see pc_net.h):
 * 30 retries x 500 ms = ~15 s (+ the last interval) before giving up. Retries are safe: the host's
 * dedup cache replays the cached RESULT for a repeated request_id (which also repairs a RESULT
 * whose send failed on a full window). A retry whose pc_net_send() fails (window full) is NOT
 * counted -- it is simply attempted again on the next poll. */
#define PC_NETGAME_PICKUP_TIMEOUT_60FPS_FRAMES 30.0f /* ~500ms */
#define PC_NETGAME_PICKUP_MAX_RETRIES 30
_Static_assert((PC_NETGAME_PICKUP_MAX_RETRIES + 1) * 500u < PC_NETGAME_CONFIRM_TIMEOUT_MS,
               "the host's reservation timeout must exceed the client's retry budget (30 x 500 ms + last interval)");

/* v2: the Stage 5A client-side s_pending_field_updates[8] deferral queue is GONE. It existed because
 * v1 applied FIELD_UPDATE through the scene-relative mFI_UtNumtoFGSet_common(), which fails whenever
 * the town grid is not the loaded scene (client indoors, mid scene transition) -- and it was lossy
 * (a 9th distinct pending tile overwrote the oldest). v2 applies every field fact straight to the
 * PERSISTENT save (pcfa_set_tile/pcfa_set_deposit/pcfa_write_acre), which succeeds indoors and
 * outdoors alike, so no queue is needed for the scene case at all. The only remaining "cannot apply
 * now" case is the client save itself not being loaded (title/save scenes); world messages are then
 * discarded and a RESYNC_REQUEST fetches a complete snapshot once it is loaded again -- lossless by
 * construction, unbounded in the number of tiles, and ordered by world_seq. See
 * pcnetgame_client_apply_*(). */

/* Stage 5B-1: host-only DROP record per peer -- same type, phases and reasoning as
 * s_host_pickup_state above (see PCNetGameHostInteraction's doc): the most recently PROCESSED drop
 * request from this peer and exactly what was decided (for a drop: item = the item the host will
 * place; raw_item unused), reserved while PENDING and replayed per phase on a retry. Kept entirely
 * SEPARATE from s_host_pickup_state rather than overloading it: a peer could have one pickup and one
 * drop resolve in close succession, and conflating the two would let a retry of one replay the
 * other's decision. Reset on disconnect and defensively on fresh READY, exactly mirroring
 * pcnetgame_reset_host_pickup_state(). */
static PCNetGameHostInteraction s_host_drop_state[PC_NET_MAX_PEERS];

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
    PCNetGameOwnerStamp owner; /* the local player at send time (see PCNetGameOwnerStamp) */
    uint16_t claimed_item;
    float    timeout_accum;
    int      retry_count;
    int      unsent;         /* 1: the FIRST send failed (window full) -- see PCNetGamePickupPending.unsent */
} PCNetGameDropPending;
static PCNetGameDropPending s_drop_pending;
static uint32_t             s_next_drop_request_id = 1;

#define PC_NETGAME_DROP_TIMEOUT_60FPS_FRAMES 30.0f /* ~500ms, mirrors pickup's own budget */
#define PC_NETGAME_DROP_MAX_RETRIES 30 /* see PC_NETGAME_PICKUP_MAX_RETRIES for the budget reasoning */
_Static_assert((PC_NETGAME_DROP_MAX_RETRIES + 1) * 500u < PC_NETGAME_CONFIRM_TIMEOUT_MS,
               "the host's reservation timeout must exceed the client's drop retry budget (30 x 500 ms + last interval)");

/* ---- v2 tuning ---- */

/* Snapshot pacing: a snapshot only sends while the peer's reliable backlog is below half the
 * transport window (the other half stays free for live traffic: deltas, results, appearance), and
 * at most this many snapshot messages per peer per poll (30 blocks -> ~8 polls at LAN RTT). */
#define PC_NETGAME_SNAPSHOT_BACKLOG_LIMIT (PC_NET_RELIABLE_WINDOW / 2)
#define PC_NETGAME_SNAPSHOT_MSGS_PER_POLL 4
/* Mutation flush: an acre with more than this many changed tiles goes as one FIELD_BLOCK (588 B)
 * instead of per-tile FIELD_UPDATEs (12 B each). */
#define PC_NETGAME_FLUSH_DELTA_MAX 4
/* A flush needing more than this many messages (e.g. daily growth touching every acre) is not
 * broadcast at all: the shadow/world_seq are committed and every READY peer gets a paced snapshot
 * instead (contract: post-growth resync uses the snapshot path). */
#define PC_NETGAME_FLUSH_BURST_MAX 16
/* Live world traffic is only sent to a peer if backlog + messages <= window - this; otherwise that
 * peer is resynced with a snapshot instead (never silently skipped). */
#define PC_NETGAME_WINDOW_HEADROOM 4
/* AMBIGUOUS values (pcfa_transient_kind() == PCFA_VALUE_AMBIGUOUS: RSV_NO, RSV_SIGNBOARD) are
 * withheld as "in flight" -- always while the host is in the town scene (live structure actors keep
 * door-front placeholders there), and outside town until the value has stayed UNCHANGED on that tile
 * for this long, after which it is committed as stable. Workstream C's audit: in-flight
 * reservations (drops, digs, burials, pitfalls, door fronts) settle within ~2 s, but these values
 * are ALSO persistent structure-footprint fill and can get stuck permanently in vanilla, so with no
 * live actors around they must not be withheld forever. Clients use the same time to decide when a
 * local ambiguous tile may be overwritten (never while the client is in town). */
#define PC_NETGAME_AMBIGUOUS_SETTLE_MS 10000u
_Static_assert(PC_NETGAME_SNAPSHOT_BACKLOG_LIMIT + PC_NETGAME_FLUSH_BURST_MAX + PC_NETGAME_WINDOW_HEADROOM <=
                   PC_NET_RELIABLE_WINDOW,
               "snapshot pacing + largest live burst must fit the transport's reliable window");

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

/* ---- v2 shared state ---- */

/* Local "gameplay save is loaded" latch -- see pcnetgame_update_local_world_ready(). Refreshed at
 * the top of every pc_net_game_poll(); both roles gate every world read/write on it. */
static int s_local_world_latched = 0;

/* ---- v2 host state ---- */

/* Per-peer state added by the world protocol (parallel to s_host_peer_link; cleared in ONE place,
 * pcnetgame_reset_all_host_peer_state()). */
typedef struct PCNetGameHostPeerState {
    /* identity deferral: a version-checked IDENTITY parked until the host save is loaded */
    int                  identity_pending;
    int                  identity_defer_logged;
    PCNetGameIdentityMsg pending_identity;
    /* "closing" (see pcnetgame_host_reject_and_close()): a REJECT was sent, transport still up */
    int                  closing;
    uint32_t             closing_since_ms;
    /* PLAYER_CONTEXT */
    int                  ctx_valid;
    PCNetPlayerContext   ctx;
    /* snapshot progress */
    int                  snap_active;
    int                  snap_stage;      /* 0 = BEGIN next, 1 = FIELD_BLOCKs, 2 = VILLAGER_SNAPSHOT next,
                                            * 3 = END next (villager population/is_home milestone: inserted
                                            * stage 2 between the field blocks and SNAPSHOT_END) */
    int                  snap_next_acre;
    int                  snap_blocks_sent;
    uint32_t             snap_epoch;
} PCNetGameHostPeerState;
static PCNetGameHostPeerState s_host_peer[PC_NET_MAX_PEERS];

/* Host world: valid only while s_host_world_ready. */
static int                   s_host_world_ready = 0;  /* shadow + town identity valid, save loaded */
static int                   s_host_shadow_valid = 0; /* shadow initialized at least once for s_host_town */
static PCNetGameTownIdentity s_host_town;
static int                   s_host_town_valid = 0;
static uint16_t              s_shadow_items[PCFA_ACRE_NUM][PCFA_TILE_NUM];   /* committed stable values */
static uint16_t              s_shadow_deposit[PCFA_ACRE_NUM][PCFA_DEPOSIT_ROWS];
static uint16_t              s_shadow_known[PCFA_ACRE_NUM][PCFA_DEPOSIT_ROWS]; /* bit = a stable value is known */
static uint32_t              s_host_ambig_since[PCFA_ACRE_NUM][PCFA_TILE_NUM]; /* ms the tile's current AMBIGUOUS value was first seen unchanged (0 = none) */
static uint16_t              s_host_ambig_val[PCFA_ACRE_NUM][PCFA_TILE_NUM];   /* that value */
static uint32_t              s_host_carry_dirty = 0;  /* acres force-re-diffed next flush (save came back) */
static int                   s_host_scan_cursor = 0;  /* round-robin safety-net scan position */
static uint32_t              s_world_seq = 0;         /* host-global, per hosting session */
static uint32_t              s_snapshot_epoch_counter = 0;
static PCNetGameRtcWire      s_host_meta_renew;       /* last committed all_grow_renew_time */
static int                   s_host_meta_valid = 0;
static PCNetGameWorldStateWire s_host_meta_world_state; /* last committed weather + market state */
static int                   s_host_meta_world_state_valid = 0;

/* Outgoing world messages of one flush (materialized once, sent per peer). */
typedef struct PCNetGameOutMsg {
    uint16_t size;
    union {
        PCNetGameFieldUpdateMsg fu;
        PCNetGameFieldBlockMsg  fb;
    } u;
} PCNetGameOutMsg;
static PCNetGameOutMsg s_out_msgs[PC_NETGAME_FLUSH_BURST_MAX];
static int             s_out_count = 0;
static int             s_out_overflow = 0;

/* ---- v2 client state (all cleared by pcnetgame_reset_client_session_state()) ---- */

static int                   s_client_identity_sent = 0;
static int                   s_client_identity_defer_logged = 0;
static PCNetGameTownIdentity s_client_claimed_town;     /* what our IDENTITY claimed */
static uint16_t              s_client_assigned_peer_id = 0;
static uint32_t              s_client_acre_seq[PCFA_ACRE_NUM];
static uint32_t              s_client_meta_seq = 0;
/* Villager population/is_home milestone: strictly-monotonic last-applied world_seq for
 * VILLAGER_ARRIVAL/VILLAGER_DEPARTURE/VILLAGER_SNAPSHOT, exactly mirroring s_client_meta_seq's own
 * generation/stale-event-protection contract (no new sequencing scheme). ARRIVAL/DEPARTURE apply iff
 * world_seq > s_client_population_seq (stale/duplicate/reordered reliable delivery is dropped, same
 * strict rule as WORLD_META); VILLAGER_SNAPSHOT applies iff world_seq >= s_client_population_seq
 * (same relaxed rule as a fresh snapshot always superseding older state, matching FIELD_BLOCK/
 * SNAPSHOT_END's own >= rule). */
static uint32_t              s_client_population_seq = 0;
static int                   s_client_snap_active = 0;
static uint32_t              s_client_snap_epoch = 0;
static int                   s_client_snap_blocks = 0;
/* Diagnostics for the snapshot being applied (reset at SNAPSHOT_BEGIN and on session reset): tiles
 * whose local value/deposit bit really changed, acres that had at least one, host values parked by
 * the DUMMY/RSV rules, and valid=0 / transient tiles left alone. Only IN_SNAPSHOT blocks count. */
#define PC_NETGAME_SNAPSHOT_TILE_LOG_MAX 64
#define PC_NETGAME_SNAPSHOT_PARK_LOG_MAX 32 /* per snapshot, parked and skipped each */
static struct {
    int tiles, acres, parked, skipped, logged, parked_logged, skipped_logged;
} s_client_snap_stats;

/* One capped line for a snapshot tile that was NOT written (parked or left alone), so a test can see
 * exactly which tiles and why. DEBUG-ONLY (--verbose): the per-snapshot summary line always prints the
 * parked/skipped_transient COUNTS. Separate caps for parked and skipped so the (many) valid=0 tiles
 * cannot crowd out the parked ones. */
static void pcnetgame_client_log_snapshot_nowrite(int parked, int acre, int tile, uint16_t local, uint16_t host,
                                                   const char* reason) {
    int* logged = parked ? &s_client_snap_stats.parked_logged : &s_client_snap_stats.skipped_logged;
    if (!g_pc_verbose) {
        return;
    }
    if (*logged >= PC_NETGAME_SNAPSHOT_PARK_LOG_MAX) {
        return;
    }
    (*logged)++;
    printf("[NET][WORLD] client: snapshot tile %s acre %d tile %d: local 0x%04X host 0x%04X (%s)\n",
           parked ? "parked" : "skipped", acre, tile, (unsigned)local, (unsigned)host, reason);
}
static int                   s_client_world_synced = 0;
static int                   s_client_need_resync = 0;
static int                   s_client_paused = 0;       /* READY but local save not loaded */
static PCNetPlayerContext    s_client_last_ctx;
static int                   s_client_last_ctx_valid = 0;
/* Tiles whose LOCAL value is transient (e.g. RSV_NO placed by one of this client's own structure
 * actors, or a door-front reservation) when a host value arrived: the local reservation is kept and
 * the host value parked here, then written as soon as the local tile settles
 * (pcnetgame_client_retry_deferred()). */
static uint32_t s_client_deferred_acres = 0;
static uint16_t s_client_deferred_mask[PCFA_ACRE_NUM][PCFA_DEPOSIT_ROWS];
static uint16_t s_client_deferred_value[PCFA_ACRE_NUM][PCFA_TILE_NUM];
static uint16_t s_client_deferred_dep_valid[PCFA_ACRE_NUM][PCFA_DEPOSIT_ROWS];
static uint16_t s_client_deferred_dep_on[PCFA_ACRE_NUM][PCFA_DEPOSIT_ROWS];
static uint32_t s_client_deferred_since[PCFA_ACRE_NUM][PCFA_TILE_NUM]; /* ms when first parked */
static int      s_client_dummy_skip_logged = 0;

/* Monotonic milliseconds (never 0, so 0 can mean "no timestamp"). */
static uint32_t pcnetgame_now_ms(void) {
    uint64_t freq = (uint64_t)SDL_GetPerformanceFrequency();
    uint64_t c = (uint64_t)SDL_GetPerformanceCounter();
    uint32_t ms = (freq != 0) ? (uint32_t)((c / freq) * 1000u + ((c % freq) * 1000u) / freq) : 1u;
    return ms != 0 ? ms : 1u;
}

static int pcnetgame_dep_bit(const uint16_t deposit[PCFA_DEPOSIT_ROWS], int tile) {
    return (deposit[tile >> 4] >> (tile & 15)) & 1;
}

static void pcnetgame_set_dep_bit(uint16_t deposit[PCFA_DEPOSIT_ROWS], int tile, int on) {
    uint16_t bit = (uint16_t)(1u << (tile & 15));
    if (on) {
        deposit[tile >> 4] |= bit;
    } else {
        deposit[tile >> 4] &= (uint16_t)~bit;
    }
}

/* "Is this process's gameplay save loaded" -- the gate for sending IDENTITY (client), answering
 * one (host), and every world read/write on either side. pcfa_save_ready() already excludes the
 * title demo, player-select and save/restart scenes, a foreigner, and a zeroed Save. On top of that
 * this LATCHES only once a real GAME_PLAY (gamePT->exec == play_main) has run since it became ready,
 * so a debug GAME_PLAYER_SELECT (player_select.c, which calls mSDI_StartInitAfter() -> stone/shine
 * rewrites of fg with a possibly-stale Save scene_no) can never be mistaken for a loaded session.
 * The latch then holds across door/scene transitions (gamePT briefly NULL) and drops the moment
 * pcfa_save_ready() does. */
static int pcnetgame_update_local_world_ready(void) {
    if (!pcfa_save_ready()) {
        s_local_world_latched = 0;
        return 0;
    }
    if (!s_local_world_latched && gamePT != NULL && gamePT->exec == play_main) {
        s_local_world_latched = 1;
    }
    return s_local_world_latched;
}

static uint32_t pcnetgame_fnv1a_u16(uint32_t h, uint16_t v) {
    h ^= (uint32_t)(v & 0xFFu);
    h *= 16777619u;
    h ^= (uint32_t)(v >> 8);
    h *= 16777619u;
    return h;
}

/* v2: see PCNetGameTownIdentity (pc_net_game.h) for why exactly these fields. The combi entry is
 * packed from its two bitfields explicitly (never memcpy'd) so the hash is a function of the values
 * the game itself uses. Town acre (ax, az) is combi block (ax + 1, az + 1) -- m_field_make.c's
 * block<->fg mapping (FGIDX_2_BLOCK_X/Z). Only meaningful when the local world is ready. */
static void pcnetgame_capture_town_identity(PCNetGameTownIdentity* out) {
    uint32_t h = 2166136261u;
    int ax, az;

    memset(out, 0, sizeof(*out));
    memcpy(out->land_name, Save_Get(land_info.name), PC_NETGAME_LAND_LEN);
    out->land_id = (uint16_t)Save_Get(land_info.id);
    for (az = 0; az < PCFA_ACRE_Z_NUM; az++) {
        for (ax = 0; ax < PCFA_ACRE_X_NUM; ax++) {
            const mFM_combination_c* c = &Save_Get(combi_table[az + 1][ax + 1]);
            uint16_t v = (uint16_t)(((unsigned)c->combination_type & 0x3FFFu) | (((unsigned)c->height & 3u) << 14));
            h = pcnetgame_fnv1a_u16(h, v);
        }
    }
    out->terrain_hash = h;
}

static int pcnetgame_town_equal(const PCNetGameTownIdentity* a, const PCNetGameTownIdentity* b) {
    return a->land_id == b->land_id && a->terrain_hash == b->terrain_hash &&
           memcmp(a->land_name, b->land_name, PC_NETGAME_LAND_LEN) == 0;
}

static void pcnetgame_format_town(const PCNetGameTownIdentity* t, char* buf, size_t buf_size) {
    snprintf(buf, buf_size, "land_id=0x%04X hash=0x%08X name=%02X%02X%02X%02X%02X%02X%02X%02X",
             (unsigned)t->land_id, (unsigned)t->terrain_hash, t->land_name[0], t->land_name[1], t->land_name[2],
             t->land_name[3], t->land_name[4], t->land_name[5], t->land_name[6], t->land_name[7]);
}

static void pcnetgame_capture_local_identity(uint8_t* player_name, uint8_t* land_name, uint16_t* player_id,
                                              uint16_t* land_id, uint8_t* has_save) {
    /* Now_Private is NULL until a save slot is actually loaded (e.g. still at the title/select
     * screen when a handshake happens) -- never dereference it without checking first. v2 callers
     * only call this once the local world is ready, so has_save is 1 in practice. */
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

/* v2: player fields from Now_Private (as v1), town fields from the Save's town identity (land_name/
 * land_id there are Save land_info, which for a resident equals PersonalID's land fields). */
static void pcnetgame_build_identity_msg(PCNetGameIdentityMsg* msg, const PCNetGameTownIdentity* town) {
    uint8_t unused_land_name[PC_NETGAME_LAND_LEN];
    uint16_t unused_land_id;

    memset(msg, 0, sizeof(*msg));
    msg->msg_type = (uint8_t)PC_NETGAME_MSG_IDENTITY;
    msg->protocol_version = PC_NETGAME_PROTOCOL_VERSION;
    pcnetgame_capture_local_identity(msg->player_name, unused_land_name, &msg->player_id, &unused_land_id,
                                      &msg->has_save);
    memcpy(msg->land_name, town->land_name, PC_NETGAME_LAND_LEN);
    msg->land_id = town->land_id;
    msg->terrain_hash = town->terrain_hash;
}

/* v2: this process's own PLAYER_CONTEXT. Caller must have checked the local world is ready. */
static void pcnetgame_capture_local_context(PCNetPlayerContext* out) {
    memset(out, 0, sizeof(*out));
    out->player_no = (uint8_t)Common_Get(player_no);
    out->destiny_type = (Now_Private != NULL) ? (uint8_t)Now_Private->destiny.type : 0;
    out->flags = pcfa_scene_is_town() ? (uint8_t)PC_NETGAME_CTX_FLAG_IN_TOWN : 0;
    out->money_power = (int16_t)mPr_GetMoneyPower();
    out->goods_power = (int16_t)mPr_GetGoodsPower();
}

static void pcnetgame_rtc_to_wire(const lbRTC_time_c* t, PCNetGameRtcWire* w) {
    memset(w, 0, sizeof(*w));
    w->sec = t->sec;
    w->min = t->min;
    w->hour = t->hour;
    w->day = t->day;
    w->weekday = t->weekday;
    w->month = t->month;
    w->year = t->year;
}

static void pcnetgame_capture_renew_time(PCNetGameRtcWire* w) {
    pcnetgame_rtc_to_wire(Save_GetPointer(all_grow_renew_time), w);
}

/* Weather + Stalk Market milestone: read-only sample of both domains' current shared,
 * host-authoritative state into PCNetGameWorldStateWire. Only ever called host-side (see
 * pcnetgame_host_check_world_meta() below) -- mirrors pcnetgame_capture_renew_time()'s own
 * read-only, no-side-effects convention exactly. */
static void pcnetgame_capture_world_state(PCNetGameWorldStateWire* w) {
    Kabu_price_c* kabu = Save_GetPointer(kabu_price_schedule);
    int i;

    memset(w, 0, sizeof(*w));
    for (i = 0; i < lbRTC_WEEKDAYS_MAX; i++) {
        w->daily_price[i] = kabu->daily_price[i];
    }
    w->trade_market = kabu->trade_market;
    pcnetgame_rtc_to_wire(&kabu->update_time, &w->kabu_update_time);
    w->weather = (uint8_t)Common_Get(weather);
    w->weather_intensity = (uint8_t)Common_Get(weather_intensity);
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

static int pcnetgame_send_reject(PCNetPeerId peer, PCNetGameRejectReason reason) {
    PCNetGameRejectMsg msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_REJECT;
    msg.reason = (uint8_t)reason;
    msg.expected_protocol_version = PC_NETGAME_PROTOCOL_VERSION;
    return pc_net_send(peer, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
}

/* v2: LAND_MISMATCH / NO_SAVE, carrying the host's town identity. */
static int pcnetgame_send_reject_town(PCNetPeerId peer, PCNetGameRejectReason reason, const PCNetGameTownIdentity* town) {
    PCNetGameRejectTownMsg msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_REJECT;
    msg.reason = (uint8_t)reason;
    msg.expected_protocol_version = PC_NETGAME_PROTOCOL_VERSION;
    memcpy(msg.land_name, town->land_name, PC_NETGAME_LAND_LEN);
    msg.land_id = town->land_id;
    msg.terrain_hash = town->terrain_hash;
    return pc_net_send(peer, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
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

/* A peer-supplied world position is usable only if every coordinate is finite and within
 * +/- PC_NETGAME_POS_ABS_LIMIT. NaN/Inf make plain `dist > MAX` reach checks fail OPEN (every
 * comparison with NaN is false), and absurd magnitudes (1e30) would feed float->int conversions in
 * the tile math; both are rejected here, before anything spatial ever sees them. */
static int pcnetgame_pos_valid(float x, float y, float z) {
    return isfinite(x) && isfinite(y) && isfinite(z) && fabsf(x) <= PC_NETGAME_POS_ABS_LIMIT &&
           fabsf(y) <= PC_NETGAME_POS_ABS_LIMIT && fabsf(z) <= PC_NETGAME_POS_ABS_LIMIT;
}

/* A MOVE sample is ingested only with a valid position and a finite, sane speed (see
 * pcnetgame_pos_valid() and PC_NETGAME_SPEED_ABS_LIMIT). The speed test is in the fail-closed form:
 * NaN and +/-Inf fail `<=` and are rejected along with anything beyond the limit. */
static int pcnetgame_move_msg_valid(const PCNetMoveMsg* in) {
    return pcnetgame_pos_valid(in->pos_x, in->pos_y, in->pos_z) && (fabsf(in->speed) <= PC_NETGAME_SPEED_ABS_LIMIT);
}

/* Rate-limited (verbose-only) note that an invalid MOVE sample was dropped: the first
 * PC_NETGAME_BAD_MOVE_LOG_BURST drops always log, after that at most one line per second. `who` is
 * "host: peer N" / "client: relayed player N". */
#define PC_NETGAME_BAD_MOVE_LOG_BURST 8u
static uint32_t s_bad_move_count = 0;
static uint32_t s_bad_move_last_log_ms = 0;
static void pcnetgame_note_bad_move(const char* side, int id, const PCNetMoveMsg* in) {
    uint32_t now;
    ++s_bad_move_count;
    if (!g_pc_verbose) {
        return;
    }
    now = pcnetgame_now_ms();
    if (s_bad_move_count > PC_NETGAME_BAD_MOVE_LOG_BURST && (uint32_t)(now - s_bad_move_last_log_ms) < 1000u) {
        return;
    }
    s_bad_move_last_log_ms = now;
    printf("[NET] %s %d: dropped invalid MOVE sample (frame %u pos=(%g,%g,%g) speed=%g) [%u dropped so far]\n", side,
           id, (unsigned)in->frame, (double)in->pos_x, (double)in->pos_y, (double)in->pos_z, (double)in->speed,
           (unsigned)s_bad_move_count);
}

/* Host side: a client's movement. Only accepted from a peer that has already completed the
 * identity handshake (PC_NETGAME_LINK_READY) -- matches the existing "READY is earned, not
 * assumed" precedent used for IDENTITY. Validation performed: packet size/type (by the caller),
 * peer state here, a finite/in-range position and a finite speed with |speed| <=
 * PC_NETGAME_SPEED_ABS_LIMIT (an invalid sample is DROPPED -- it
 * never reaches pc_remote_player and is never relayed, so it can neither create a teleporting remote
 * avatar nor poison the position later read by pickup/drop validation), plus stale/duplicate
 * rejection inside pc_remote_player_on_move(). No movement/physics validation, no anti-cheat --
 * explicitly out of scope for Stage 3. */
static void pcnetgame_handle_host_move(PCNetPeerId peer, const PCNetMoveMsg* in) {
    PCNetMoveSample sample;
    int i;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }
    if (!pcnetgame_move_msg_valid(in)) {
        pcnetgame_note_bad_move("host: peer", (int)peer, in);
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
    if (!pcnetgame_move_msg_valid(in)) {
        pcnetgame_note_bad_move("client: relayed player", (int)in->net_player_id, in);
        return; /* never let a non-finite/absurd position reach pc_remote_player's interpolation */
    }
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

/* Log tag / record accessor for one interaction kind (PC_NETGAME_INTERACT_KIND_*). */
static const char* pcnetgame_kind_tag(int kind) {
    return kind == (int)PC_NETGAME_INTERACT_KIND_DROP ? "DROP" : "PICKUP";
}

static PCNetGameHostInteraction* pcnetgame_host_interaction(PCNetPeerId peer, int kind) {
    if (peer < 0 || peer >= PC_NET_MAX_PEERS) {
        return NULL;
    }
    if (kind == (int)PC_NETGAME_INTERACT_KIND_PICKUP) {
        return &s_host_pickup_state[peer];
    }
    if (kind == (int)PC_NETGAME_INTERACT_KIND_DROP) {
        return &s_host_drop_state[peer];
    }
    return NULL;
}

/* Releases a PENDING reservation (phase -> `new_phase`, ABORTED or EXPIRED). NEVER touches the field.
 * One log line per release (a state transition, not per packet). No-op unless the record is PENDING. */
static void pcnetgame_host_release(PCNetPeerId peer, int kind, PCNetGameHostInteraction* it, PCNetGameHostPhase new_phase,
                                   const char* why) {
    if (it == NULL || it->phase != (uint8_t)PC_NETGAME_PHASE_PENDING) {
        return;
    }
    printf("[NET][%s] host: peer %d request %u reservation released: %s (tile %d,%d)\n", pcnetgame_kind_tag(kind),
           (int)peer, (unsigned)it->request_id, why, (int)it->ut_x, (int)it->ut_z);
    it->phase = (uint8_t)new_phase;
}

/* Stage 5A: clears one peer's cached pickup dedup/replay state (and releases its reservation, if any)
 * -- see s_host_pickup_state's own doc comment for why this exists and why it must run on disconnect
 * (a reused pc_net peer slot must never answer a new connection's request with a previous, unrelated
 * connection's cached result, nor keep a dead connection's tile reserved). Safe to call for an
 * out-of-range peer (no-op). */
static void pcnetgame_reset_host_pickup_state(PCNetPeerId peer) {
    if (peer < 0 || peer >= PC_NET_MAX_PEERS) {
        return;
    }
    pcnetgame_host_release(peer, (int)PC_NETGAME_INTERACT_KIND_PICKUP, &s_host_pickup_state[peer],
                           PC_NETGAME_PHASE_ABORTED, "peer reset/disconnect");
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
    pcnetgame_host_release(peer, (int)PC_NETGAME_INTERACT_KIND_DROP, &s_host_drop_state[peer],
                           PC_NETGAME_PHASE_ABORTED, "peer reset/disconnect");
    memset(&s_host_drop_state[peer], 0, sizeof(s_host_drop_state[peer]));
}

/* Returns the peer (0..PC_NET_MAX_PEERS-1) whose PENDING pickup/drop currently reserves persistent
 * tile (acre, tile), or -1 if none does. A PENDING record whose age already reached
 * PC_NETGAME_CONFIRM_TIMEOUT_MS does not count (expiry proper runs at the top of every poll; this
 * keeps the answer exact between polls). */
static int pcnetgame_host_tile_reserved_by(int acre, int tile) {
    uint32_t now = 0;
    int i, k;

    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        for (k = 0; k < 2; k++) {
            const PCNetGameHostInteraction* it = (k == 0) ? &s_host_pickup_state[i] : &s_host_drop_state[i];
            if (it->phase != (uint8_t)PC_NETGAME_PHASE_PENDING || it->acre != (uint8_t)acre || it->tile != (uint8_t)tile) {
                continue;
            }
            if (now == 0) {
                now = pcnetgame_now_ms();
            }
            if ((uint32_t)(now - it->reserved_since_ms) < PC_NETGAME_CONFIRM_TIMEOUT_MS) {
                return i;
            }
        }
    }
    return -1;
}

/* A peer may hold one pending pickup AND one pending drop, independently. Releases the peer's PENDING
 * interaction of exactly `kind` (called when a NEW request of that kind -- a different request_id --
 * arrives, meaning the client gave up on the old one). The other kind's pending interaction is NOT
 * touched: the client tracks the two kinds independently and may still apply that one's RESULT. */
static void pcnetgame_host_abort_pending_of_peer(PCNetPeerId peer, int kind, const char* why) {
    pcnetgame_host_release(peer, kind, pcnetgame_host_interaction(peer, kind), PC_NETGAME_PHASE_ABORTED, why);
}

/* Once per host poll, BEFORE events are drained: PENDING reservations older than
 * PC_NETGAME_CONFIRM_TIMEOUT_MS are EXPIRED (released, field never touched). Unsigned subtraction, so
 * it is correct across a wrap of the 32-bit millisecond clock. Runs whether or not the host world is
 * ready. */
static void pcnetgame_host_expire_reservations(void) {
    uint32_t now = 0;
    int i, k;

    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        for (k = 0; k < 2; k++) {
            int kind = (k == 0) ? (int)PC_NETGAME_INTERACT_KIND_PICKUP : (int)PC_NETGAME_INTERACT_KIND_DROP;
            PCNetGameHostInteraction* it = (k == 0) ? &s_host_pickup_state[i] : &s_host_drop_state[i];
            if (it->phase != (uint8_t)PC_NETGAME_PHASE_PENDING) {
                continue;
            }
            if (now == 0) {
                now = pcnetgame_now_ms();
            }
            if ((uint32_t)(now - it->reserved_since_ms) >= PC_NETGAME_CONFIRM_TIMEOUT_MS) {
                pcnetgame_host_release((PCNetPeerId)i, kind, it, PC_NETGAME_PHASE_EXPIRED, "expired (no CONFIRM within the reservation timeout)");
            }
        }
    }
}

/* The ONE place every host-side per-peer cache gets cleared. It exists so the list of caches lives
 * in one spot instead of being copied at each call site. Protocol v2 calls it on PEER_CONNECTED, on
 * PEER_DISCONNECTED (the real reset, so a reused pc_net peer slot never answers a new connection
 * with the previous one's cached results), on IDENTITY acceptance (the defensive re-reset for a
 * slot that somehow went READY again without this file seeing the disconnect), on every
 * host-initiated reject/drop, and from pcnetgame_reset_host_world_state(). Any new per-peer host
 * state belongs in this function.
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
    /* Stage 5A / 5B-1 / two-phase: clear the dedup records AND release any reservation they hold (the
     * reservation table is the set of PENDING records, so this is the single place a dead or reused
     * peer's reservations disappear). Never touches the field. */
    pcnetgame_reset_host_pickup_state(peer);
    pcnetgame_reset_host_drop_state(peer);
    if (peer >= 0 && peer < PC_NET_MAX_PEERS) {
        /* v2: identity deferral, PLAYER_CONTEXT, snapshot progress/epoch. v2 additionally calls this
         * on PC_NET_EVENT_PEER_CONNECTED (a freshly allocated transport slot starts clean even if a
         * disconnect was somehow never observed) and on every host-initiated reject/drop. */
        memset(&s_host_peer[peer], 0, sizeof(s_host_peer[peer]));
    }
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
    if (mNT_check_unknown(item)) {
        return 0; /* mNT_get_itemTableNo() indexes its tables unchecked -- never look up an unknown id */
    }
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
 * the lookup safe for every ITEM1 id it lets through. Any ITEM1/FTR1 id it flags as out of range is
 * rejected outright here (an unknown id must never be written into the shared field, and would later
 * reach pickup validation's own money-bag lookup, which is guarded the same way). For non-ITEM1
 * types, mNT_get_itemTableNo() never returns mNT_ITEM_TYPE_BAG, so the money-bag test needs no
 * other type. */
static int pcnetgame_is_droppable_item(mActor_name_t item) {
    if (item == (mActor_name_t)EMPTY_NO || item == ITM_SIGNBOARD || item == (mActor_name_t)HONEYCOMB) {
        return 0;
    }
    if (mNT_check_unknown(item)) {
        return 0; /* ITEM1/FTR1 id outside the game's tables: never written into the shared field */
    }
    if (ITEM_IS_ITEM1(item) && pcnetgame_is_money_bag_item(item)) {
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
 * itself.
 *
 * v2: every read is from the PERSISTENT town field (pcfa_get_tile/pcfa_get_deposit), never the
 * host's loaded scene grid, so a pickup validates correctly while the host itself is indoors. The
 * requester must have told us (PLAYER_CONTEXT) that it is in the town scene -- otherwise its ut_x/
 * ut_z and last-synced position are room coordinates that merely look like town ones. The tile
 * center is pure arithmetic (what mFI_UtNum2CenterWpos() computes), independent of the host scene.
 * out_acre/out_tile receive the persistent address of the tile on success; out_raw_item the raw
 * field value observed (the COMMIT re-validates the tile still holds exactly this).
 * Two-phase: a tile reserved by ANY peer's pending pickup/drop is rejected, the requester's last-known
 * position must be finite and in range (pcnetgame_pos_valid()), and the reach comparison is written
 * in the fail-closed form. A success here only RESERVES the tile (see the caller) -- the field is
 * written later, on the client's COMMIT. */
static int pcnetgame_validate_and_resolve_pickup(PCNetPeerId peer, uint8_t ut_x, uint8_t ut_z,
                                                 mActor_name_t* out_granted_item, mActor_name_t* out_raw_item,
                                                 int* out_acre, int* out_tile) {
    uint16_t raw_value;
    mActor_name_t raw_item;
    xyz_t center;
    float px, py, pz;
    int acre, tile;
    int holder;

    if (!s_host_world_ready) {
        return 0; /* host save not loaded (title / save scene) -- nothing authoritative to read */
    }
    if (!s_host_peer[peer].ctx_valid || !(s_host_peer[peer].ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
        return 0; /* requester is not (known to be) in the town scene -- see above */
    }
    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile)) {
        return 0; /* not a persistent town tile (border/beach/ocean/island or out of range) */
    }
    holder = pcnetgame_host_tile_reserved_by(acre, tile);
    if (holder >= 0) {
        if (g_pc_verbose) {
            printf("[NET][PICKUP] host: peer %d pickup at tile (%d,%d) rejected: tile reserved by peer %d\n", (int)peer,
                   (int)ut_x, (int)ut_z, holder);
        }
        return 0; /* another peer's pending pickup/drop holds this tile -- deterministic rejection */
    }
    if (pcfa_get_deposit(acre, tile) != 0) {
        return 0; /* buried/reserved tile -- never an ordinary pickupable ground item */
    }
    if (!pcfa_get_tile(acre, tile, &raw_value)) {
        return 0;
    }
    raw_item = (mActor_name_t)raw_value; /* the host's own authoritative read, taken now -- never the
                                           * client's claim, and never cached from any earlier moment */

    if (!pcnetgame_is_pickupable_field_item(raw_item) || mNT_check_unknown(raw_item) ||
        pcnetgame_is_money_bag_item(raw_item)) {
        return 0; /* wrong classification (incl. transient RSV_NO), or explicitly excluded for Stage 5A */
    }

    if (!pc_remote_player_get_last_position((PCNetPlayerId)peer, &px, &py, &pz)) {
        return 0; /* no movement sample from this peer yet */
    }
    if (!pcnetgame_pos_valid(px, py, pz)) {
        return 0; /* non-finite / absurd last-known position: fail closed (see pcnetgame_pos_valid()) */
    }
    center.x = (f32)ut_x * mFI_UT_WORLDSIZE_X_F + mFI_UT_WORLDSIZE_HALF_X_F;
    center.z = (f32)ut_z * mFI_UT_WORLDSIZE_Z_F + mFI_UT_WORLDSIZE_HALF_Z_F;
    /* Seed center.y from the requester's own last-synced height (as before: the tile center has no
       height of its own here) so the vertical-distance check compares defined values. NOTE: that
       makes dy identically 0 -- the vertical check is effectively dead (pre-existing; a real one would
       need the terrain height of the tile and could false-reject legitimate slopes/cliffs, so it is
       deliberately left alone). It is still written in the fail-closed form below. */
    center.y = py;
    {
        float dx = center.x - px;
        float dz = center.z - pz;
        float dy = center.y - py;
        /* Fail-closed comparison form: anything that is not PROVEN in range (incl. NaN) is rejected. */
        if (!((dx * dx + dz * dz) <= PC_NETGAME_PICKUP_MAX_REACH_SQ && fabsf(dy) <= PC_NETGAME_PICKUP_MAX_REACH_Y)) {
            return 0; /* too far from this peer's last-known position -- see the reach macros' doc */
        }
    }

    *out_granted_item = pcnetgame_resolve_pickup_item(raw_item);
    *out_raw_item = raw_item;
    *out_acre = acre;
    *out_tile = tile;
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
    if (!pcnetgame_pos_valid(px, py, pz)) {
        return 0; /* defense in depth: the caller already checked, but never feed NaN/absurd values to tile math */
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
 * an acceptance of a tile vanilla would not have chosen from the synced state.
 *
 * v2: the target tile's value/deposit are read from the PERSISTENT field (pcfa_*). The vanilla
 * neighbor search and mCoBG_CheckPlace() still need the host's LOADED town collision, so this fails
 * closed (rejects) whenever the host itself is not in the town scene (pcfa_scene_is_town() == 0),
 * and -- like pickup -- whenever the requester is not known (PLAYER_CONTEXT) to be in town.
 * out_acre/out_tile receive the persistent address on success.
 * Two-phase: a tile reserved by ANY peer's pending pickup/drop is rejected, and the requester's
 * last-known position must be finite and in range (pcnetgame_pos_valid()) before any tile math. A
 * success only RESERVES the (empty) tile; the field is written on the client's COMMIT (which
 * re-checks it is still empty). */
static int pcnetgame_validate_and_resolve_drop(PCNetPeerId peer, const PCNetGameDropRequestMsg* in,
                                               mActor_name_t* out_placed_item, int* out_acre, int* out_tile) {
    uint16_t target_value;
    xyz_t center;
    float px, py, pz;
    int16_t requester_facing;
    int requester_ux, requester_uz; /* Stage 5B-2: now holds vanilla's resolved tile, not the
                                       requester's own tile */
    int acre, tile;
    int holder;

    if (!s_host_world_ready || !pcfa_scene_is_town()) {
        return 0; /* host save not loaded, or host not in the town scene: no loaded collision to run
                     vanilla's search against -- fail closed */
    }
    if (!s_host_peer[peer].ctx_valid || !(s_host_peer[peer].ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
        return 0; /* requester is not (known to be) in the town scene */
    }
    if (in->pocket_slot_idx >= mPr_POCKETS_SLOT_COUNT) {
        return 0; /* not a real pocket slot -- never trust it */
    }
    if (!pcnetgame_is_droppable_item((mActor_name_t)in->claimed_item)) {
        return 0; /* outside Stage 5B-1's supported item classification */
    }
    if (!pcfa_town_ut_to_acre_tile((int)in->ut_x, (int)in->ut_z, &acre, &tile)) {
        return 0; /* not a persistent town tile -- never trust it, whatever the client intended */
    }
    holder = pcnetgame_host_tile_reserved_by(acre, tile);
    if (holder >= 0) {
        if (g_pc_verbose) {
            printf("[NET][DROP] host: peer %d drop at tile (%d,%d) rejected: tile reserved by peer %d\n", (int)peer,
                   (int)in->ut_x, (int)in->ut_z, holder);
        }
        return 0; /* another peer's pending pickup/drop holds this tile -- deterministic rejection */
    }
    if (pcfa_get_deposit(acre, tile) != 0) {
        return 0; /* buried/reserved tile -- never a legal plain-drop target this stage (bury is
                     deferred, see the Stage 5B audit) */
    }
    if (!pcfa_get_tile(acre, tile, &target_value) || target_value != (uint16_t)EMPTY_NO) {
        return 0; /* already occupied (by another item, or RSV_NO/a hole sentinel) -- the common case */
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
    if (!pcnetgame_pos_valid(px, py, pz)) {
        return 0; /* non-finite / absurd last-known position: fail closed (see pcnetgame_pos_valid()) */
    }
    /* mFI_UtNum2CenterWpos() (see its own doc, m_field_info.c) only ever writes center.x/center.z --
       it never touches center.y, which would otherwise be read uninitialized by the vertical-distance
       check just below. Seed it from the requester's own last-synced height so that check compares
       against a defined value instead of indeterminate stack memory. NOTE: that makes the check
       identically 0 -- effectively dead (pre-existing; a real vertical check would need the terrain
       height of the tile and could false-reject legitimate slopes, so it is deliberately left alone).
       Still written in the fail-closed form. The horizontal exactness is enforced by the vanilla
       tile-search match below. */
    center.y = py;
    if (!(fabsf(center.y - py) <= PC_NETGAME_DROP_MAX_REACH_Y)) {
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
    *out_acre = acre;
    *out_tile = tile;
    return 1;
}

/* =============================== v2 host world machinery =============================== */

/* Starts (or restarts, with a new epoch) a world snapshot to one READY peer. The snapshot itself is
 * sent incrementally by pcnetgame_host_pump_snapshots(), each block built at send time. */
static void pcnetgame_host_start_snapshot(PCNetPeerId peer, const char* why) {
    PCNetGameHostPeerState* st;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }
    st = &s_host_peer[peer];
    if (st->snap_active && st->snap_stage == 0) {
        return; /* already queued and nothing of it sent yet: every block will be built later anyway */
    }
    st->snap_active = 1;
    st->snap_stage = 0;
    st->snap_next_acre = 0;
    st->snap_blocks_sent = 0;
    st->snap_epoch = ++s_snapshot_epoch_counter;
    printf("[NET][WORLD] host: peer %d snapshot epoch %u queued (%s)\n", (int)peer, (unsigned)st->snap_epoch, why);
}

static void pcnetgame_host_resync_all(const char* why) {
    int i;
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
            pcnetgame_host_start_snapshot((PCNetPeerId)i, why);
        }
    }
}

/* (Re)initializes the committed shadow from the persistent field. A tile whose value is not STABLE
 * right now has NO known stable value (known bit 0 -> sent as valid=0, never invented): TRANSIENT
 * values (DUMMY_* structure placeholders while the host is in town, pitfall/money-seed values) wait
 * until their owner restores the real value; AMBIGUOUS values (RSV_NO, RSV_SIGNBOARD) are unknown
 * too and their settle timer (outside town only) is started by the first diff that sees them. */
static void pcnetgame_host_init_shadow(void) {
    int acre, t;

    memset(s_shadow_known, 0, sizeof(s_shadow_known));
    memset(s_host_ambig_since, 0, sizeof(s_host_ambig_since));
    memset(s_host_ambig_val, 0, sizeof(s_host_ambig_val));
    for (acre = 0; acre < PCFA_ACRE_NUM; acre++) {
        pcfa_read_acre(acre, s_shadow_items[acre], s_shadow_deposit[acre]);
        for (t = 0; t < PCFA_TILE_NUM; t++) {
            int kind = pcfa_transient_kind(s_shadow_items[acre][t]);
            if (kind == PCFA_VALUE_STABLE) {
                pcnetgame_set_dep_bit(s_shadow_known[acre], t, 1);
            } else {
                s_shadow_items[acre][t] = (uint16_t)EMPTY_NO; /* meaningless while unknown */
                pcnetgame_set_dep_bit(s_shadow_deposit[acre], t, 0);
            }
        }
    }
    (void)pcfa_take_dirty_acres(); /* bits accumulated before the session: the snapshot covers them */
    pcnetgame_capture_renew_time(&s_host_meta_renew);
    s_host_meta_valid = 1;
    pcnetgame_capture_world_state(&s_host_meta_world_state);
    s_host_meta_world_state_valid = 1;
    s_host_shadow_valid = 1;
}

static void pcnetgame_out_reset(void) {
    s_out_count = 0;
    s_out_overflow = 0;
}

static PCNetGameOutMsg* pcnetgame_out_append(void) {
    if (s_out_count >= PC_NETGAME_FLUSH_BURST_MAX) {
        s_out_overflow = 1;
        return NULL;
    }
    return &s_out_msgs[s_out_count++];
}

static void pcnetgame_build_field_block(PCNetGameFieldBlockMsg* fb, int acre, uint8_t flags, uint32_t epoch) {
    memset(fb, 0, sizeof(*fb));
    fb->msg_type = (uint8_t)PC_NETGAME_MSG_FIELD_BLOCK;
    fb->grid = (uint8_t)PC_NETGAME_GRID_TOWN;
    fb->acre = (uint8_t)acre;
    fb->flags = flags;
    fb->epoch = epoch;
    fb->world_seq = s_world_seq;
    memcpy(fb->items, s_shadow_items[acre], sizeof(fb->items));
    memcpy(fb->deposit, s_shadow_deposit[acre], sizeof(fb->deposit));
    memcpy(fb->valid, s_shadow_known[acre], sizeof(fb->valid));
}

/* Diffs one acre of the persistent field against the shadow and COMMITS every stable change:
 * shadow updated (and marked known), world_seq incremented (per tile for FIELD_UPDATEs, once for a
 * FIELD_BLOCK), message appended to the out list (or s_out_overflow set -- the commit still
 * happens). Per tile, by pcfa_transient_kind() of the CURRENT value:
 *   STABLE     commit if unknown or value/deposit differ from the shadow.
 *   TRANSIENT  never committed; the shadow keeps the last stable value (the tile is simply dropped
 *              from this flush -- the acre is NOT kept dirty; the owner's restore write is hooked
 *              and the round-robin safety scan (1 acre per poll) re-examines every acre every
 *              PCFA_ACRE_NUM polls anyway).
 *   AMBIGUOUS  (RSV_NO, RSV_SIGNBOARD; generic over pcfa_transient_kind()) withheld exactly like
 *              TRANSIENT while the host is in the town scene (no timer runs); outside town, withheld
 *              until that same value has been seen unchanged on the tile for
 *              PC_NETGAME_AMBIGUOUS_SETTLE_MS, then committed as a stable value like any other.
 *              A tile whose committed shadow already equals the ambiguous value needs no wait (e.g.
 *              only its deposit bit changed). Timers reset whenever the tile's value changes or
 *              stops being ambiguous. Nothing here pins an acre dirty. */
static void pcnetgame_host_diff_commit_acre(int acre) {
    uint16_t items[PCFA_TILE_NUM];
    uint16_t deposit[PCFA_DEPOSIT_ROWS];
    uint8_t changed[PCFA_TILE_NUM];
    uint32_t now = 0;
    int in_town = -1; /* lazily: pcfa_scene_is_town() is only needed once an AMBIGUOUS tile turns up */
    int n = 0;
    int t, i;

    pcfa_read_acre(acre, items, deposit);
    for (t = 0; t < PCFA_TILE_NUM; t++) {
        uint16_t v = items[t];
        int d = pcnetgame_dep_bit(deposit, t);
        int known = pcnetgame_dep_bit(s_shadow_known[acre], t);
        int same = known && v == s_shadow_items[acre][t] && d == pcnetgame_dep_bit(s_shadow_deposit[acre], t);
        int kind = pcfa_transient_kind(v);

        if (kind != PCFA_VALUE_AMBIGUOUS || s_host_ambig_val[acre][t] != v) {
            s_host_ambig_since[acre][t] = 0; /* not ambiguous, or a different ambiguous value: restart */
        }
        if (same) {
            continue;
        }
        if (kind == PCFA_VALUE_TRANSIENT) {
            continue;
        }
        if (kind == PCFA_VALUE_AMBIGUOUS && !(known && s_shadow_items[acre][t] == v)) {
            if (in_town == -1) {
                in_town = pcfa_scene_is_town();
            }
            if (in_town) {
                s_host_ambig_since[acre][t] = 0; /* live structure actors may own it: never learn it here */
                continue;
            }
            if (now == 0) {
                now = pcnetgame_now_ms();
            }
            if (s_host_ambig_since[acre][t] == 0) {
                s_host_ambig_since[acre][t] = now;
                s_host_ambig_val[acre][t] = v;
                continue;
            }
            if ((uint32_t)(now - s_host_ambig_since[acre][t]) < PC_NETGAME_AMBIGUOUS_SETTLE_MS) {
                continue;
            }
            s_host_ambig_since[acre][t] = 0; /* settled: v is this tile's stable value now */
        }
        changed[n++] = (uint8_t)t;
    }
    if (n == 0) {
        return;
    }

    for (i = 0; i < n; i++) {
        t = changed[i];
        s_shadow_items[acre][t] = items[t];
        pcnetgame_set_dep_bit(s_shadow_deposit[acre], t, pcnetgame_dep_bit(deposit, t));
        pcnetgame_set_dep_bit(s_shadow_known[acre], t, 1);
    }

    if (n <= PC_NETGAME_FLUSH_DELTA_MAX) {
        for (i = 0; i < n; i++) {
            PCNetGameOutMsg* om;
            t = changed[i];
            ++s_world_seq;
            om = pcnetgame_out_append();
            if (om != NULL) {
                PCNetGameFieldUpdateMsg* fu = &om->u.fu;
                memset(fu, 0, sizeof(*fu));
                fu->msg_type = (uint8_t)PC_NETGAME_MSG_FIELD_UPDATE;
                fu->grid = (uint8_t)PC_NETGAME_GRID_TOWN;
                fu->acre = (uint8_t)acre;
                fu->tile = (uint8_t)t;
                fu->flags = (uint8_t)(PC_NETGAME_FU_FLAG_DEPOSIT_VALID |
                                      (pcnetgame_dep_bit(s_shadow_deposit[acre], t) ? PC_NETGAME_FU_FLAG_DEPOSIT_ON : 0));
                fu->value = s_shadow_items[acre][t];
                fu->world_seq = s_world_seq;
                om->size = (uint16_t)sizeof(*fu);
            }
        }
    } else {
        PCNetGameOutMsg* om;
        ++s_world_seq;
        om = pcnetgame_out_append();
        if (om != NULL) {
            pcnetgame_build_field_block(&om->u.fb, acre, 0, 0);
            om->size = (uint16_t)sizeof(om->u.fb);
        }
    }
    if (g_pc_verbose) {
        printf("[NET][WORLD] host: committed acre %d (%d tile(s)) -> world_seq %u\n", acre, n, (unsigned)s_world_seq);
    }
}

/* Sends the out list to every READY peer, in order. A peer whose reliable window cannot take the
 * whole list (or a send that fails) is resynced with a fresh snapshot instead -- a READY peer is
 * never left having silently missed a committed change. */
static void pcnetgame_host_send_out_list(void) {
    int i, m;

    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        int backlog;
        if (s_host_peer_link[i] != PC_NETGAME_LINK_READY) {
            continue;
        }
        backlog = pc_net_reliable_backlog((PCNetPeerId)i);
        if (backlog < 0) {
            continue; /* transport already dropped it; the DISCONNECTED event is on its way */
        }
        if (backlog + s_out_count > PC_NET_RELIABLE_WINDOW - PC_NETGAME_WINDOW_HEADROOM) {
            pcnetgame_host_start_snapshot((PCNetPeerId)i, "reliable window full");
            continue;
        }
        for (m = 0; m < s_out_count; m++) {
            if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &s_out_msgs[m].u, s_out_msgs[m].size)) {
                pcnetgame_host_start_snapshot((PCNetPeerId)i, "world send failed");
                break;
            }
        }
    }
}

/* THE commit function: every host-side field change reaches clients through here -- the per-poll
 * dirty flush, the round-robin safety scan, and each explicit Stage 5A/5B path (which passes just
 * its own acre right after its write). Because a commit updates the shadow, any later flush of the
 * same acre finds nothing to send: no double-send is possible. No-op until the host world is ready. */
static void pcnetgame_host_flush_mask(uint32_t mask) {
    int acre;

    if (!s_host_world_ready) {
        return;
    }
    mask &= PCFA_DIRTY_ALL_MASK;
    if (mask == 0) {
        return;
    }

    pcnetgame_out_reset();
    for (acre = 0; acre < PCFA_ACRE_NUM; acre++) {
        if (mask & ((uint32_t)1u << acre)) {
            pcnetgame_host_diff_commit_acre(acre);
        }
    }

    if (s_out_overflow) {
        printf("[NET][WORLD] host: large field change (> %d messages) -> snapshot resync to all peers (world_seq %u)\n",
               PC_NETGAME_FLUSH_BURST_MAX, (unsigned)s_world_seq);
        pcnetgame_host_resync_all("large field change");
    } else if (s_out_count > 0) {
        pcnetgame_host_send_out_list();
    }
    pcnetgame_out_reset();
}

/* Explicit-path helper: commit whatever changed in the acre holding town tile (ut_x, ut_z). A tile
 * that is not a persistent town tile (e.g. a house-room coordinate from a host-local seam while the
 * host is indoors) is ignored -- it is not shared state. */
static void pcnetgame_host_commit_town_ut(int ut_x, int ut_z) {
    int acre;
    if (!pcfa_town_ut_to_acre_tile(ut_x, ut_z, &acre, NULL)) {
        return;
    }
    pcnetgame_host_flush_mask((uint32_t)1u << acre);
}

/* Renew-time / weather / Stalk-Market watch (Workstream D, extended by the weather + Stalk Market
 * milestone): the host's Save all_grow_renew_time, Common_Get(weather)/Common_Get(weather_intensity)
 * or Save_Get(kabu_price_schedule) changed since the last poll. Runs AFTER the field flush in each
 * poll, so the field results precede the new renew time on the wire. Peers mid-snapshot are skipped:
 * their END is built at send time and will carry the current value (see pcnetgame_host_pump_snapshots()
 * below). Deliberately polled here rather than hooked into mEnv_DecideWeather_NormalGameStart()/
 * Kabu_manager() directly: those already run unconditionally on the host (or solo) every relevant
 * frame/day-check with no network awareness, so comparing their resulting Save/Common state
 * before-vs-after -- exactly like the pre-existing renew_time watch already does -- needs no new hook
 * into either system and can never miss a change regardless of which code path produced it. This
 * function runs with zero, one, or many connected clients; with zero it still updates
 * s_host_meta_renew/s_host_meta_world_state and s_world_seq so a client that joins later gets the
 * accumulated result via SNAPSHOT_END, exactly like the field shadow does. */
static void pcnetgame_host_check_world_meta(void) {
    PCNetGameRtcWire cur_renew;
    PCNetGameWorldStateWire cur_state;
    PCNetGameWorldMetaMsg wm;
    uint8_t changed_flags = 0;
    int i;

    if (!s_host_world_ready) {
        return;
    }
    pcnetgame_capture_renew_time(&cur_renew);
    pcnetgame_capture_world_state(&cur_state);

    if (!s_host_meta_valid || memcmp(&cur_renew, &s_host_meta_renew, sizeof(cur_renew)) != 0) {
        changed_flags |= (uint8_t)PC_NETGAME_META_FLAG_RENEW_TIME_VALID;
    }
    if (!s_host_meta_world_state_valid ||
        cur_state.weather != s_host_meta_world_state.weather ||
        cur_state.weather_intensity != s_host_meta_world_state.weather_intensity) {
        changed_flags |= (uint8_t)PC_NETGAME_META_FLAG_WEATHER_VALID;
    }
    if (!s_host_meta_world_state_valid ||
        memcmp(cur_state.daily_price, s_host_meta_world_state.daily_price, sizeof(cur_state.daily_price)) != 0 ||
        cur_state.trade_market != s_host_meta_world_state.trade_market ||
        memcmp(&cur_state.kabu_update_time, &s_host_meta_world_state.kabu_update_time, sizeof(cur_state.kabu_update_time)) != 0) {
        changed_flags |= (uint8_t)PC_NETGAME_META_FLAG_MARKET_VALID;
    }
    if (changed_flags == 0) {
        return;
    }

    s_host_meta_renew = cur_renew;
    s_host_meta_valid = 1;
    s_host_meta_world_state = cur_state;
    s_host_meta_world_state_valid = 1;
    ++s_world_seq;

    memset(&wm, 0, sizeof(wm));
    wm.msg_type = (uint8_t)PC_NETGAME_MSG_WORLD_META;
    wm.flags = changed_flags;
    wm.world_seq = s_world_seq;
    wm.renew_time = cur_renew;
    wm.world_state = cur_state;
    if (changed_flags & PC_NETGAME_META_FLAG_RENEW_TIME_VALID) {
        printf("[NET][WORLD] host: all_grow_renew_time changed -> %04u-%02u-%02u %02u:%02u:%02u (world_seq %u)\n",
               (unsigned)cur_renew.year, (unsigned)cur_renew.month, (unsigned)cur_renew.day, (unsigned)cur_renew.hour,
               (unsigned)cur_renew.min, (unsigned)cur_renew.sec, (unsigned)s_world_seq);
    }
    if (changed_flags & PC_NETGAME_META_FLAG_WEATHER_VALID) {
        printf("[NET][WORLD] host: weather changed -> type %u intensity %u (world_seq %u)\n",
               (unsigned)cur_state.weather, (unsigned)cur_state.weather_intensity, (unsigned)s_world_seq);
    }
    if (changed_flags & PC_NETGAME_META_FLAG_MARKET_VALID) {
        printf("[NET][WORLD] host: Stalk Market schedule changed -> trend %u sunday %u (world_seq %u)\n",
               (unsigned)cur_state.trade_market, (unsigned)cur_state.daily_price[0], (unsigned)s_world_seq);
    }

    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        int backlog;
        if (s_host_peer_link[i] != PC_NETGAME_LINK_READY || s_host_peer[i].snap_active) {
            continue;
        }
        backlog = pc_net_reliable_backlog((PCNetPeerId)i);
        if (backlog < 0) {
            continue;
        }
        if (backlog + 1 > PC_NET_RELIABLE_WINDOW - PC_NETGAME_WINDOW_HEADROOM ||
            !pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &wm, (uint16_t)sizeof(wm))) {
            pcnetgame_host_start_snapshot((PCNetPeerId)i, "world meta send failed");
        }
    }
}

/* Sends queued snapshot messages to every READY peer with an active snapshot, a few per poll and
 * only while that peer's reliable backlog is under PC_NETGAME_SNAPSHOT_BACKLOG_LIMIT. Each block is
 * built at send time: its acre is flushed first (committing -- and broadcasting to everyone,
 * including this peer, ahead of the block -- anything that changed), then the block is the
 * committed shadow at the current world_seq. Only runs while the host world is ready (save loaded). */
/* Forward-declared: defined near the other villager-population host-side functions, below; used here
 * (snap_stage 2) before that point in the file. */
static void pcnetgame_build_villager_snapshot(PCNetGameVillagerSnapshotMsg* vs, uint32_t epoch);

static void pcnetgame_host_pump_snapshots(void) {
    int i;

    if (!s_host_world_ready) {
        return;
    }
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        PCNetGameHostPeerState* st = &s_host_peer[i];
        int sent = 0;

        while (s_host_peer_link[i] == PC_NETGAME_LINK_READY && st->snap_active &&
               sent < PC_NETGAME_SNAPSHOT_MSGS_PER_POLL) {
            int backlog = pc_net_reliable_backlog((PCNetPeerId)i);
            if (backlog < 0 || backlog >= PC_NETGAME_SNAPSHOT_BACKLOG_LIMIT) {
                break;
            }

            if (st->snap_stage == 0) {
                PCNetGameSnapshotBeginMsg b;
                memset(&b, 0, sizeof(b));
                b.msg_type = (uint8_t)PC_NETGAME_MSG_SNAPSHOT_BEGIN;
                b.grid = (uint8_t)PC_NETGAME_GRID_TOWN;
                b.acre_count = (uint8_t)PCFA_ACRE_NUM;
                b.epoch = st->snap_epoch;
                b.world_seq = s_world_seq;
                if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &b, (uint16_t)sizeof(b))) {
                    break; /* retried next poll */
                }
                st->snap_stage = 1;
                st->snap_next_acre = 0;
                st->snap_blocks_sent = 0;
            } else if (st->snap_stage == 1) {
                static PCNetGameFieldBlockMsg fb; /* 588 B, built and sent synchronously */
                uint32_t epoch_before = st->snap_epoch;
                int acre = st->snap_next_acre;

                pcnetgame_host_flush_mask((uint32_t)1u << acre);
                if (!st->snap_active || st->snap_epoch != epoch_before || st->snap_stage != 1 ||
                    s_host_peer_link[i] != PC_NETGAME_LINK_READY) {
                    sent++; /* bounded: counts against this poll's budget */
                    continue; /* that flush restarted this peer's snapshot (large change / send failure) */
                }
                pcnetgame_build_field_block(&fb, acre, (uint8_t)PC_NETGAME_FB_FLAG_IN_SNAPSHOT, st->snap_epoch);
                if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &fb, (uint16_t)sizeof(fb))) {
                    break;
                }
                st->snap_blocks_sent++;
                st->snap_next_acre++;
                if (st->snap_next_acre >= PCFA_ACRE_NUM) {
                    st->snap_stage = 2;
                }
            } else if (st->snap_stage == 2) {
                /* Villager population/is_home milestone: one-shot full population snapshot, sent
                 * after every FIELD_BLOCK (so the villagers' house footprints are already correct in
                 * the persistent field by the time the client applies this) and before SNAPSHOT_END. */
                static PCNetGameVillagerSnapshotMsg vs; /* 132 B, built and sent synchronously */
                pcnetgame_build_villager_snapshot(&vs, st->snap_epoch);
                if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &vs, (uint16_t)sizeof(vs))) {
                    break;
                }
                st->snap_stage = 3;
            } else {
                PCNetGameSnapshotEndMsg e;
                memset(&e, 0, sizeof(e));
                e.msg_type = (uint8_t)PC_NETGAME_MSG_SNAPSHOT_END;
                e.grid = (uint8_t)PC_NETGAME_GRID_TOWN;
                e.acre_count = (uint8_t)st->snap_blocks_sent;
                e.flags = (uint8_t)(PC_NETGAME_META_FLAG_RENEW_TIME_VALID | PC_NETGAME_META_FLAG_WEATHER_VALID |
                                    PC_NETGAME_META_FLAG_MARKET_VALID);
                e.epoch = st->snap_epoch;
                e.world_seq = s_world_seq;
                e.renew_time = s_host_meta_renew; /* == the Save value: the meta check ran this poll */
                e.world_state = s_host_meta_world_state; /* == current weather + market: same guarantee */
                if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &e, (uint16_t)sizeof(e))) {
                    break;
                }
                st->snap_active = 0;
                int known = 0, a, t;
                for (a = 0; a < PCFA_ACRE_NUM; a++) {
                    for (t = 0; t < PCFA_TILE_NUM; t++) {
                        known += pcnetgame_dep_bit(s_shadow_known[a], t);
                    }
                }
                printf("[NET][WORLD] host: peer %d snapshot epoch %u sent (%d acres, world_seq %u) "
                       "known_tiles=%d unknown_tiles=%d\n", i, (unsigned)st->snap_epoch, st->snap_blocks_sent,
                       (unsigned)s_world_seq, known, PCFA_ACRE_NUM * PCFA_TILE_NUM - known);
            }
            sent++;
        }
    }
}

/* Final host-initiated removal of a peer: pc_net_disconnect() (which frees the transport slot and
 * queues no local event), then this file's own per-peer reset. Rejects do NOT call this directly --
 * they go through pcnetgame_host_reject_and_close() first so the REJECT can be retransmitted. */
static void pcnetgame_host_drop_peer(PCNetPeerId peer) {
    int was_ready;
    if (peer < 0 || peer >= PC_NET_MAX_PEERS) {
        return;
    }
    was_ready = (s_host_peer_link[peer] == PC_NETGAME_LINK_READY);
    pc_net_disconnect(peer);
    s_host_peer_link[peer] = PC_NETGAME_LINK_DISCONNECTED;
    pcnetgame_reset_all_host_peer_state(peer);
    if (was_ready) {
        pc_remote_player_on_disconnect((PCNetPlayerId)peer);
    }
}

/* Window (ms) a rejected peer is kept connected so the reliable REJECT can be retransmitted if the
 * first transmission was lost, before the transport peer is dropped anyway. */
#define PC_NETGAME_REJECT_LINGER_MS 500u

/* After a REJECT was sent to `peer` (sent_ok == pc_net_send()'s result): put the peer in the
 * "closing" state instead of disconnecting at once. The transport peer stays connected -- so the
 * reliable REJECT keeps being retransmitted until acknowledged -- but the peer is demoted to
 * HANDSHAKE (never READY, so it gets no world/movement/appearance traffic and its
 * remote-player actor, if any, is destroyed), all its parked state is cleared, and every game
 * message it sends is ignored (pcnetgame_handle_host_data()). pcnetgame_host_closing_tick() then
 * finishes with pc_net_disconnect() (REJECT first, DISCONNECT after) once the reliable backlog is 0
 * or PC_NETGAME_REJECT_LINGER_MS has elapsed. A genuinely dead peer is still removed by the
 * transport's own timeout (its DISCONNECTED event clears this state). If the REJECT could not even
 * be queued the peer is dropped immediately. */
static void pcnetgame_host_reject_and_close(PCNetPeerId peer, int sent_ok) {
    int was_ready;
    if (peer < 0 || peer >= PC_NET_MAX_PEERS) {
        return;
    }
    if (!sent_ok) {
        pcnetgame_host_drop_peer(peer);
        return;
    }
    was_ready = (s_host_peer_link[peer] == PC_NETGAME_LINK_READY);
    pcnetgame_reset_all_host_peer_state(peer);
    s_host_peer[peer].closing = 1;
    s_host_peer[peer].closing_since_ms = pcnetgame_now_ms();
    s_host_peer_link[peer] = PC_NETGAME_LINK_HANDSHAKE;
    if (was_ready) {
        pc_remote_player_on_disconnect((PCNetPlayerId)peer);
    }
}

/* Once per poll: finishes every closing peer whose REJECT was acknowledged or whose linger expired. */
static void pcnetgame_host_closing_tick(void) {
    uint32_t now = 0;
    int i;

    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        int backlog;
        if (!s_host_peer[i].closing) {
            continue;
        }
        if (s_host_peer_link[i] == PC_NETGAME_LINK_DISCONNECTED) {
            s_host_peer[i].closing = 0; /* defensive: transport already dropped it */
            continue;
        }
        if (now == 0) {
            now = pcnetgame_now_ms();
        }
        backlog = pc_net_reliable_backlog((PCNetPeerId)i);
        if (backlog <= 0 || (uint32_t)(now - s_host_peer[i].closing_since_ms) >= PC_NETGAME_REJECT_LINGER_MS) {
            pcnetgame_host_drop_peer((PCNetPeerId)i);
        }
    }
}

/* Host world readiness, once per poll before events: tracks the host's own save, captures the host
 * town identity, (re)initializes the shadow, and handles the save going away / coming back.
 * pcfa_save_ready() dropping to 0 PAUSES world service (no identity answers, no snapshots, no
 * commits, requests rejected) -- it is not a session reset. When it comes back:
 *   - same town: everything is re-diffed (the Save may have been reloaded from disk) and committed
 *     through the normal flush (a big difference becomes a snapshot resync of every peer);
 *   - different town: every READY peer is dropped with REJECT(LAND_MISMATCH) and the shadow is
 *     rebuilt for the new town (peers still in HANDSHAKE are validated against it normally). */
static void pcnetgame_host_world_tick(int local_ready) {
    PCNetGameTownIdentity cur;

    if (!local_ready) {
        if (s_host_world_ready) {
            printf("[NET][WORLD] host: local save not loaded -- world service paused\n");
            s_host_world_ready = 0;
        }
        return;
    }

    pcnetgame_capture_town_identity(&cur);
    if (s_host_town_valid && !pcnetgame_town_equal(&cur, &s_host_town)) {
        char a[96], b[96];
        int i;
        pcnetgame_format_town(&s_host_town, a, sizeof(a));
        pcnetgame_format_town(&cur, b, sizeof(b));
        printf("[NET][WORLD] host: loaded town changed (%s -> %s) -- dropping READY peers\n", a, b);
        for (i = 0; i < PC_NET_MAX_PEERS; i++) {
            if (s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
                pcnetgame_host_reject_and_close((PCNetPeerId)i,
                    pcnetgame_send_reject_town((PCNetPeerId)i, PC_NETGAME_REJECT_LAND_MISMATCH, &cur));
            }
        }
        s_host_shadow_valid = 0;
        s_host_world_ready = 0;
    }
    s_host_town = cur;
    s_host_town_valid = 1;

    if (!s_host_world_ready) {
        char a[96];
        if (!s_host_shadow_valid) {
            s_host_carry_dirty = 0;
            pcnetgame_host_init_shadow();
        } else {
            s_host_carry_dirty = PCFA_DIRTY_ALL_MASK; /* re-diff everything against the shadow */
        }
        s_host_world_ready = 1;
        pcnetgame_format_town(&s_host_town, a, sizeof(a));
        printf("[NET][WORLD] host: world ready (%s, world_seq %u)\n", a, (unsigned)s_world_seq);
    }
}

/* Per-poll host world work, after events: pending identities are handled by the caller; this runs
 * the dirty flush (+1 round-robin safety-scan acre), then the renew-time watch, then snapshots. */
static void pcnetgame_host_world_poll(void) {
    uint32_t mask;

    if (!s_host_world_ready) {
        return;
    }
    mask = pcfa_take_dirty_acres() | s_host_carry_dirty | ((uint32_t)1u << s_host_scan_cursor);
    s_host_carry_dirty = 0;
    s_host_scan_cursor = (s_host_scan_cursor + 1) % PCFA_ACRE_NUM;
    pcnetgame_host_flush_mask(mask);
    pcnetgame_host_check_world_meta();
    pcnetgame_host_pump_snapshots();
}

/* v2: sends a PICKUP_RESULT / DROP_RESULT and checks the outcome. A failed send (reliable window
 * full) is NOT fatal and NOT silent: the per-peer interaction record was populated BEFORE this call,
 * so the client's next retry of the same request_id (it retries every ~500 ms for ~15 s) replays this
 * exact decision per the record's phase -- nothing is re-validated or re-granted and the result is
 * never lost. Logged in --verbose only (a stalled peer would otherwise log on every retry). */
static void pcnetgame_host_send_result(PCNetPeerId peer, const void* msg, uint16_t size, const char* what) {
    if (!pc_net_send(peer, PC_NET_RELIABLE, msg, size) && g_pc_verbose) {
        printf("[NET] host: %s to peer %d could not be queued (window full) -- will replay on the client's retry\n",
               what, (int)peer);
    }
}

static void pcnetgame_host_send_pickup_result(PCNetPeerId peer, uint32_t request_id, uint8_t ut_x, uint8_t ut_z,
                                              int accepted, uint16_t granted_item) {
    PCNetGamePickupResultMsg out;
    memset(&out, 0, sizeof(out));
    out.msg_type = (uint8_t)PC_NETGAME_MSG_PICKUP_RESULT;
    out.accepted = (uint8_t)(accepted != 0);
    out.ut_x = ut_x;
    out.ut_z = ut_z;
    out.request_id = request_id;
    out.granted_item = accepted ? granted_item : (uint16_t)EMPTY_NO;
    pcnetgame_host_send_result(peer, &out, (uint16_t)sizeof(out), "PICKUP_RESULT");
}

static void pcnetgame_host_send_drop_result(PCNetPeerId peer, uint32_t request_id, uint8_t ut_x, uint8_t ut_z,
                                            int accepted, uint16_t placed_item) {
    PCNetGameDropResultMsg out;
    memset(&out, 0, sizeof(out));
    out.msg_type = (uint8_t)PC_NETGAME_MSG_DROP_RESULT;
    out.accepted = (uint8_t)(accepted != 0);
    out.ut_x = ut_x;
    out.ut_z = ut_z;
    out.request_id = request_id;
    out.placed_item = accepted ? placed_item : (uint16_t)EMPTY_NO;
    pcnetgame_host_send_result(peer, &out, (uint16_t)sizeof(out), "DROP_RESULT");
}

/* Answers a repeated request_id from the peer's cached record, per phase (see
 * PCNetGameHostInteraction's doc): PENDING replays the provisional accept (reservation and timer
 * untouched), DONE replays what was cached, ABORTED/EXPIRED answer accepted=0 so a released
 * reservation can never be re-granted. */
static void pcnetgame_host_replay(PCNetPeerId peer, int kind, const PCNetGameHostInteraction* rec) {
    int accepted = (rec->phase == (uint8_t)PC_NETGAME_PHASE_PENDING || rec->phase == (uint8_t)PC_NETGAME_PHASE_DONE)
                       ? rec->accepted
                       : 0;
    if (kind == (int)PC_NETGAME_INTERACT_KIND_PICKUP) {
        pcnetgame_host_send_pickup_result(peer, rec->request_id, rec->ut_x, rec->ut_z, accepted, rec->item);
    } else {
        pcnetgame_host_send_drop_result(peer, rec->request_id, rec->ut_x, rec->ut_z, accepted, rec->item);
    }
}

/* Host side: a client's pickup request. Deliberately NOT Player_actor_setup_main_Pickup() run on
 * the remote player's behalf -- this never touches any ACTOR, any PLAYER_ACTOR, or Now_Private
 * (the host's OWN save is never read or written by another player's pickup); it only reads the
 * shared field, records a reservation, and sends back small, self-contained network messages. See the
 * Stage 5A inventory-architecture audit for why this is deliberately not a full remote-player
 * Private_c/inventory simulation.
 *
 * Two-phase: a request that validates RESERVES the tile and answers RESULT(accepted=1, provisional);
 * the field is NOT written here -- only pcnetgame_handle_host_confirm() does that, on COMMIT. Order:
 * (1) a repeated request_id replays per phase; (2) otherwise the peer's pending PICKUP (if any) is
 * aborted -- a NEW pickup means the client gave up on the old one (a pending DROP of the same peer is
 * independent and stays reserved); (3) validate (incl. the
 * reservation table and finite-position checks); (4) reserve or record the rejection; (5) answer. */
static void pcnetgame_handle_host_pickup_request(PCNetPeerId peer, const PCNetGamePickupRequestMsg* in) {
    PCNetGameHostInteraction* rec;
    mActor_name_t granted_item = (mActor_name_t)EMPTY_NO;
    mActor_name_t raw_item = (mActor_name_t)EMPTY_NO;
    int accepted;
    int acre = 0, tile = 0;
    int prev_valid;
    uint32_t prev_rid;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return; /* not a known, handshake-complete peer -- never process on their behalf */
    }

    rec = &s_host_pickup_state[peer];

    if (rec->phase != (uint8_t)PC_NETGAME_PHASE_NONE && rec->request_id == in->request_id) {
        /* Duplicate/retry: replay per phase rather than re-validating against now-changed ground
         * truth (a committed original has already cleared the tile, so a fresh re-check would wrongly
         * see "already empty" and reject a request that actually succeeded; a released reservation
         * must never be re-granted). See PCNetGameHostInteraction's doc. */
        pcnetgame_host_replay(peer, (int)PC_NETGAME_INTERACT_KIND_PICKUP, rec);
        return;
    }
    if (rec->prev_valid && rec->prev_request_id == in->request_id) {
        /* Late retry of the request this record replaced (its reservation was released): never re-grant. */
        pcnetgame_host_send_pickup_result(peer, in->request_id, in->ut_x, in->ut_z, 0, (uint16_t)EMPTY_NO);
        return;
    }

    pcnetgame_host_abort_pending_of_peer(peer, (int)PC_NETGAME_INTERACT_KIND_PICKUP,
                                         "replaced by a newer request from the same peer");

    accepted = pcnetgame_validate_and_resolve_pickup(peer, in->ut_x, in->ut_z, &granted_item, &raw_item, &acre, &tile);

    prev_valid = (rec->phase == (uint8_t)PC_NETGAME_PHASE_ABORTED || rec->phase == (uint8_t)PC_NETGAME_PHASE_EXPIRED);
    prev_rid = rec->request_id;
    memset(rec, 0, sizeof(*rec));
    rec->prev_valid = (uint8_t)prev_valid;
    rec->prev_request_id = prev_rid;
    rec->request_id = in->request_id;
    rec->ut_x = in->ut_x;
    rec->ut_z = in->ut_z;
    if (accepted) {
        /* RESERVE. The field is NOT touched: it changes only on the client's COMMIT. */
        rec->phase = (uint8_t)PC_NETGAME_PHASE_PENDING;
        rec->accepted = 1;
        rec->item = (uint16_t)granted_item;
        rec->raw_item = (uint16_t)raw_item;
        rec->acre = (uint8_t)acre;
        rec->tile = (uint8_t)tile;
        rec->reserved_since_ms = pcnetgame_now_ms();
        printf("[NET][PICKUP] host: peer %d request %u reserved tile (%d,%d) item=0x%04X (field unchanged until CONFIRM)\n",
               (int)peer, (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)granted_item);
    } else {
        rec->phase = (uint8_t)PC_NETGAME_PHASE_DONE; /* final rejection; replayed verbatim on a retry */
        rec->accepted = 0;
    }

    pcnetgame_host_send_pickup_result(peer, in->request_id, in->ut_x, in->ut_z, accepted, (uint16_t)granted_item);
}

/* Host side: a client's drop request. Exact structural mirror of
 * pcnetgame_handle_host_pickup_request() (replay per phase, abort the peer's pending DROP only -- a
 * pending pickup of the same peer is independent --, validate, reserve-or-record, reply) -- see that function's own doc for the shared
 * reasoning. Never touches any ACTOR, any PLAYER_ACTOR, or Now_Private; only reads the shared field
 * and sends small, self-contained network messages. The field write happens only on COMMIT. */
static void pcnetgame_handle_host_drop_request(PCNetPeerId peer, const PCNetGameDropRequestMsg* in) {
    PCNetGameHostInteraction* rec;
    mActor_name_t placed_item = (mActor_name_t)EMPTY_NO;
    int accepted;
    int acre = 0, tile = 0;
    int prev_valid;
    uint32_t prev_rid;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return; /* not a known, handshake-complete peer -- never process on their behalf */
    }

    rec = &s_host_drop_state[peer];

    if (rec->phase != (uint8_t)PC_NETGAME_PHASE_NONE && rec->request_id == in->request_id) {
        /* Duplicate/retry: replay per phase -- see the pickup handler. (A stale re-check here would see
           the request's OWN committed mutation and wrongly reject it as "no longer empty".) */
        pcnetgame_host_replay(peer, (int)PC_NETGAME_INTERACT_KIND_DROP, rec);
        return;
    }
    if (rec->prev_valid && rec->prev_request_id == in->request_id) {
        /* Late retry of the request this record replaced (its reservation was released): never re-grant. */
        pcnetgame_host_send_drop_result(peer, in->request_id, in->ut_x, in->ut_z, 0, (uint16_t)EMPTY_NO);
        return;
    }

    pcnetgame_host_abort_pending_of_peer(peer, (int)PC_NETGAME_INTERACT_KIND_DROP,
                                         "replaced by a newer request from the same peer");

    accepted = pcnetgame_validate_and_resolve_drop(peer, in, &placed_item, &acre, &tile);

    prev_valid = (rec->phase == (uint8_t)PC_NETGAME_PHASE_ABORTED || rec->phase == (uint8_t)PC_NETGAME_PHASE_EXPIRED);
    prev_rid = rec->request_id;
    memset(rec, 0, sizeof(*rec));
    rec->prev_valid = (uint8_t)prev_valid;
    rec->prev_request_id = prev_rid;
    rec->request_id = in->request_id;
    rec->ut_x = in->ut_x;
    rec->ut_z = in->ut_z;
    if (accepted) {
        /* RESERVE (an EMPTY tile). The field is NOT touched here. On COMMIT the write is a direct
           persistent pcfa_set_tile() -- deliberately NOT a reproduction of vanilla's local ~14-26+
           frame physics-drop actor: confirmed safe during the Stage 5B-1 preflight, for the
           ordinary-item subset this function's own validation guarantees (never a hole target -- see
           the deposit/EMPTY_NO checks), that actor's only authoritative effect at landing is this same
           bare write (bIT_actor_drop_move_fly's ordinary landing branch,
           bg_item_common.c_inc:1758-1764); the RSV_NO reservation and the arc animation are
           transient, local-only presentation state. See the Stage 5B audit's "Proposed Network
           Protocol"/"Host-Local Path". */
        rec->phase = (uint8_t)PC_NETGAME_PHASE_PENDING;
        rec->accepted = 1;
        rec->item = (uint16_t)placed_item;
        rec->raw_item = (uint16_t)EMPTY_NO;
        rec->acre = (uint8_t)acre;
        rec->tile = (uint8_t)tile;
        rec->reserved_since_ms = pcnetgame_now_ms();
        printf("[NET][DROP] host: peer %d request %u reserved tile (%d,%d) item=0x%04X (field unchanged until CONFIRM)\n",
               (int)peer, (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)placed_item);
    } else {
        rec->phase = (uint8_t)PC_NETGAME_PHASE_DONE; /* final rejection; replayed verbatim on a retry */
        rec->accepted = 0;
    }

    pcnetgame_host_send_drop_result(peer, in->request_id, in->ut_x, in->ut_z, accepted, (uint16_t)placed_item);
}

/* Host side: INTERACT_CONFIRM from a peer. Acts ONLY if (peer READY [closing peers never reach here],
 * kind matches, outcome is COMMIT or ABORT, the peer's record of that kind is PENDING, and
 * request_id equals that record's request_id); anything else is stale/duplicate/forged: ignored, no
 * mutation, one verbose-only log line. Never rolls an RNG: the item was resolved ONCE at request time.
 *   ABORT   release the reservation (phase ABORTED); the field is never touched.
 *   COMMIT  re-validate the tile NOW (pickup: still exactly the reserved raw item and deposit 0;
 *           drop: still EMPTY_NO and deposit 0; host world still ready), then pcfa_set_tile() and the
 *           single commit path (FIELD_UPDATE to every READY client incl. the requester); phase DONE.
 *           If re-validation fails (should be impossible with reservations + the host-local guards
 *           around pc_net_game_field_tile_reserved()) the reservation is released, NOTHING is
 *           mutated and an error line is logged -- the client has already done its inventory step, so
 *           this is the one place the two sides could diverge (see the report's residual risks). */
static void pcnetgame_handle_host_confirm(PCNetPeerId peer, const PCNetGameInteractConfirmMsg* in) {
    PCNetGameHostInteraction* rec;
    const char* tag;
    const char* fail = NULL;
    uint16_t cur = 0;
    uint16_t new_value;
    int dep = 0;
    int is_pickup;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }
    rec = pcnetgame_host_interaction(peer, (int)in->kind);
    if (rec == NULL || (in->outcome != (uint8_t)PC_NETGAME_CONFIRM_COMMIT && in->outcome != (uint8_t)PC_NETGAME_CONFIRM_ABORT) ||
        rec->phase != (uint8_t)PC_NETGAME_PHASE_PENDING || rec->request_id != in->request_id) {
        if (g_pc_verbose) {
            printf("[NET] host: peer %d ignored stale/invalid INTERACT_CONFIRM (kind=%u outcome=%u reason=%u request %u)\n",
                   (int)peer, (unsigned)in->kind, (unsigned)in->outcome, (unsigned)in->reason, (unsigned)in->request_id);
        }
        return;
    }

    is_pickup = (in->kind == (uint8_t)PC_NETGAME_INTERACT_KIND_PICKUP);
    tag = pcnetgame_kind_tag((int)in->kind);

    if ((uint32_t)(pcnetgame_now_ms() - rec->reserved_since_ms) >= PC_NETGAME_CONFIRM_TIMEOUT_MS) {
        /* Past the reservation lifetime already (the per-poll expiry pass has not run yet): the tile may
           already be free for others, so this late confirm must not act. */
        pcnetgame_host_release(peer, (int)in->kind, rec, PC_NETGAME_PHASE_EXPIRED,
                               "expired (no CONFIRM within the reservation timeout)");
        return;
    }

    if (in->outcome == (uint8_t)PC_NETGAME_CONFIRM_ABORT) {
        char why[64];
        snprintf(why, sizeof(why), "aborted by client (reason=%u)", (unsigned)in->reason);
        pcnetgame_host_release(peer, (int)in->kind, rec, PC_NETGAME_PHASE_ABORTED, why);
        return;
    }

    /* COMMIT: re-validate, then write through the one commit path. */
    new_value = is_pickup ? (uint16_t)EMPTY_NO : rec->item;
    if (!s_host_world_ready) {
        fail = "host world not ready";
    } else if (!pcfa_get_tile(rec->acre, rec->tile, &cur)) {
        fail = "tile unreadable";
    } else {
        dep = pcfa_get_deposit(rec->acre, rec->tile);
        if (dep != 0) {
            fail = "tile deposit bit set";
        } else if (is_pickup ? (cur != rec->raw_item) : (cur != (uint16_t)EMPTY_NO)) {
            fail = is_pickup ? "tile no longer holds the reserved item" : "tile no longer empty";
        } else if (!pcfa_set_tile(rec->acre, rec->tile, new_value)) {
            fail = "pcfa_set_tile refused";
        }
    }
    if (fail != NULL) {
        printf("[NET][%s] host: peer %d request %u COMMIT FAILED (%s): tile (%d,%d) found 0x%04X dep %d expected 0x%04X "
               "-- no mutation, reservation released\n",
               tag, (int)peer, (unsigned)rec->request_id, fail, (int)rec->ut_x, (int)rec->ut_z, (unsigned)cur, dep,
               (unsigned)(is_pickup ? rec->raw_item : (uint16_t)EMPTY_NO));
        rec->phase = (uint8_t)PC_NETGAME_PHASE_ABORTED;
        return;
    }

    rec->phase = (uint8_t)PC_NETGAME_PHASE_DONE;
    printf("[NET][%s] host: peer %d request %u committed tile (%d,%d) 0x%04X -> 0x%04X\n", tag, (int)peer,
           (unsigned)rec->request_id, (int)rec->ut_x, (int)rec->ut_z, (unsigned)cur, (unsigned)new_value);
    pcnetgame_host_flush_mask((uint32_t)1u << rec->acre); /* the single commit path: shadow + world_seq + FIELD_UPDATE to every READY peer */
}

/* ---- client side of the two-phase interactions ---- */

/* Captures who the local player is right now (see PCNetGameOwnerStamp). Returns 0 (and a zeroed
 * stamp) if no gameplay save is loaded/latched. */
static int pcnetgame_capture_owner_stamp(PCNetGameOwnerStamp* out) {
    memset(out, 0, sizeof(*out));
    if (!s_local_world_latched || Now_Private == NULL) {
        return 0;
    }
    out->player_no = (uint8_t)Common_Get(player_no);
    memcpy(out->player_name, Now_Private->player_ID.player_name, PC_NETGAME_NAME_LEN);
    memcpy(out->land_name, Now_Private->player_ID.land_name, PC_NETGAME_LAND_LEN);
    out->player_id = Now_Private->player_ID.player_id;
    out->land_id = Now_Private->player_ID.land_id;
    return 1;
}

/* 1 iff a gameplay save is loaded AND the local player is byte-for-byte the one recorded in `rec`. */
static int pcnetgame_owner_stamp_matches(const PCNetGameOwnerStamp* rec) {
    PCNetGameOwnerStamp cur;
    return pcnetgame_capture_owner_stamp(&cur) && memcmp(&cur, rec, sizeof(cur)) == 0;
}

/* Sends INTERACT_CONFIRM to the host. Returns pc_net_send()'s result (0 = could not be queued). */
static int pcnetgame_client_send_confirm(uint8_t kind, uint8_t outcome, uint8_t reason, uint32_t request_id) {
    PCNetGameInteractConfirmMsg m;
    if (s_role != PC_NETGAME_ROLE_CLIENT) {
        return 0;
    }
    memset(&m, 0, sizeof(m));
    m.msg_type = (uint8_t)PC_NETGAME_MSG_INTERACT_CONFIRM;
    m.kind = kind;
    m.outcome = outcome;
    m.reason = reason;
    m.request_id = request_id;
    return pc_net_send(0, PC_NET_RELIABLE, &m, (uint16_t)sizeof(m));
}

/* Client side: drops the pending pickup/drop request(s) -- either unconditionally (need_stamp_mismatch
 * == 0: the local save/latch went away) or only those whose owner stamp no longer matches -- and
 * tells the host to release the reservation immediately (best effort: if the message cannot be
 * queued, or the link is gone, the host's expiry / peer reset handles it). Nothing was ever moved
 * locally for a still-pending request, so there is nothing to undo. */
static void pcnetgame_client_cancel_pending(int need_stamp_mismatch, const char* why) {
    if (s_pickup_pending.valid && (!need_stamp_mismatch || !pcnetgame_owner_stamp_matches(&s_pickup_pending.owner))) {
        printf("[NET][PICKUP] request %u cancelled: %s\n", (unsigned)s_pickup_pending.request_id, why);
        pcnetgame_client_send_confirm((uint8_t)PC_NETGAME_INTERACT_KIND_PICKUP, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STATE_CHANGED, s_pickup_pending.request_id);
        s_pickup_pending.valid = 0;
    }
    if (s_drop_pending.valid && (!need_stamp_mismatch || !pcnetgame_owner_stamp_matches(&s_drop_pending.owner))) {
        printf("[NET][DROP] request %u cancelled: %s\n", (unsigned)s_drop_pending.request_id, why);
        pcnetgame_client_send_confirm((uint8_t)PC_NETGAME_INTERACT_KIND_DROP, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STATE_CHANGED, s_drop_pending.request_id);
        s_drop_pending.valid = 0;
    }
}

/* Client side: the host's answer to our own pending pickup request -- see
 * pc_net_game_request_pickup()'s doc for the overall flow.
 *   accepted == 0   final rejection (as before): nothing reserved, nothing to confirm.
 *   accepted == 1   PROVISIONAL: the tile is reserved for us and the field has NOT changed. We only
 *                   store the item if it is still safe to (see below), and answer with
 *                   INTERACT_CONFIRM; only the host's COMMIT handling then clears the tile.
 * An accepted RESULT with no matching pending request (we gave up / were reset / unlatched, or a
 * replay of a RESULT we already handled) is answered ABORT(STALE) so the host releases at once. The
 * pending record is cleared the moment a RESULT for it arrives, so no retry can follow it.
 *
 * Validation before touching the inventory (never grant outside this path): the RESULT's tile must be
 * the one we asked for, the owner stamp must be unchanged (same save/player), and a free pocket must
 * exist -- otherwise ABORT (STATE_CHANGED / POCKETS_FULL; the item stays in the world). The COMMIT is
 * queued BEFORE the inventory is written: if it cannot be queued (window full) we do nothing to the
 * inventory (the host's reservation just expires), so the two sides can never diverge on a send
 * failure. Everything here is one synchronous step, so the free slot found cannot vanish before the
 * write. */
static void pcnetgame_handle_client_pickup_result(const PCNetGamePickupResultMsg* in) {
    const uint8_t kind = (uint8_t)PC_NETGAME_INTERACT_KIND_PICKUP;
    int matches = s_pickup_pending.valid && s_pickup_pending.request_id == in->request_id;

    if (!in->accepted) {
        if (!matches) {
            return; /* not our current pending request -- already resolved, given up, or a stale duplicate */
        }
        s_pickup_pending.valid = 0; /* resolved -- never retried or re-applied again */
        printf("[NET][PICKUP] request %u rejected by host (tile %d,%d)\n", (unsigned)in->request_id,
               (int)in->ut_x, (int)in->ut_z);
        return;
    }

    if (!matches) {
        printf("[NET][PICKUP] provisional accept for request %u has no matching pending request -- sending ABORT(STALE)\n",
               (unsigned)in->request_id);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STALE, in->request_id);
        return;
    }

    s_pickup_pending.valid = 0; /* the RESULT arrived: no retry may follow, whatever happens next */

    if (in->ut_x != s_pickup_pending.ut_x || in->ut_z != s_pickup_pending.ut_z || in->granted_item == (uint16_t)EMPTY_NO) {
        printf("[NET][PICKUP] request %u accepted but the RESULT disagrees with the request (tile %d,%d item=%u) -- aborting, "
               "no inventory change\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)in->granted_item);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STATE_CHANGED, in->request_id);
        return;
    }
    if (!pcnetgame_owner_stamp_matches(&s_pickup_pending.owner)) {
        printf("[NET][PICKUP] request %u accepted (item=%u) but the local player/save changed -- aborting, no inventory change\n",
               (unsigned)in->request_id, (unsigned)in->granted_item);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STATE_CHANGED, in->request_id);
        return;
    }
    /* Owner stamp matched, so Now_Private is non-NULL and the save is loaded. */
    if (mPr_GetPossessionItemIdx(Now_Private, (mActor_name_t)EMPTY_NO) < 0) {
        printf("[NET][PICKUP] request %u accepted (item=%u) but local pockets are full -- pickup aborted, the item stays in the world\n",
               (unsigned)in->request_id, (unsigned)in->granted_item);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_POCKETS_FULL, in->request_id);
        return;
    }
    if (!pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_COMMIT, (uint8_t)PC_NETGAME_CONFIRM_REASON_NONE,
                                       in->request_id)) {
        printf("[NET][PICKUP] request %u accepted (item=%u) but the CONFIRM could not be queued -- cancelled, no inventory change "
               "(the host's reservation will expire)\n",
               (unsigned)in->request_id, (unsigned)in->granted_item);
        return;
    }

    /* Stage 5A: the one place this file grants an item -- via the real, unmodified
     * mPr_SetFreePossessionItem(), exactly as Player_actor_setup_main_Pickup() would have called it
     * locally in single-player (see the Stage 5A inventory-architecture audit, Parts 4/6). No slot
     * number is sent by the host (see PCNetGamePickupResultMsg's doc) -- this client's own real pocket
     * contents are the only correct basis for choosing one. The free slot was verified above and
     * nothing runs in between, so this cannot fail. */
    if (mPr_SetFreePossessionItem(Now_Private, (mActor_name_t)in->granted_item, mPr_ITEM_COND_NORMAL)) {
        printf("[NET][PICKUP] request %u accepted (item=%u) -- granted to a free pocket slot, CONFIRM(COMMIT) sent\n",
               (unsigned)in->request_id, (unsigned)in->granted_item);
    } else {
        printf("[NET][PICKUP] request %u accepted (item=%u) -- INTERNAL ERROR: free slot vanished after CONFIRM(COMMIT)\n",
               (unsigned)in->request_id, (unsigned)in->granted_item);
    }
}

/* Client side: the host's answer to our own pending drop request -- see
 * pc_net_game_request_drop()'s doc for the overall flow, and the pickup handler above for the shared
 * two-phase reasoning (provisional accept, stale answered ABORT(STALE), record cleared on arrival,
 * COMMIT queued before the inventory write). The field side of an accepted drop arrives separately
 * via the ordinary FIELD_UPDATE the host broadcasts on COMMIT -- this function's only job is the
 * inventory side: clearing the exact pocket slot this client itself chose when it sent the request,
 * and ONLY if that slot STILL holds exactly the claimed item (the inventory may have been rearranged
 * during the pending window; clearing blindly would destroy the wrong item / duplicate one). No slot
 * number travels on the wire (see PCNetGameDropResultMsg's doc) -- s_drop_pending is authoritative. */
static void pcnetgame_handle_client_drop_result(const PCNetGameDropResultMsg* in) {
    const uint8_t kind = (uint8_t)PC_NETGAME_INTERACT_KIND_DROP;
    int matches = s_drop_pending.valid && s_drop_pending.request_id == in->request_id;
    int slot;

    if (!in->accepted) {
        if (!matches) {
            return;
        }
        s_drop_pending.valid = 0; /* resolved -- never retried or re-applied again */
        printf("[NET][DROP] request %u rejected by host (tile %d,%d)\n", (unsigned)in->request_id,
               (int)in->ut_x, (int)in->ut_z);
        return; /* item stays exactly where it was -- never removed locally before this point, so
                   there is nothing to undo */
    }

    if (!matches) {
        printf("[NET][DROP] provisional accept for request %u has no matching pending request -- sending ABORT(STALE)\n",
               (unsigned)in->request_id);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STALE, in->request_id);
        return;
    }

    s_drop_pending.valid = 0; /* the RESULT arrived: no retry may follow, whatever happens next */
    slot = (int)s_drop_pending.pocket_slot_idx;

    if (in->ut_x != s_drop_pending.ut_x || in->ut_z != s_drop_pending.ut_z ||
        in->placed_item != s_drop_pending.claimed_item || slot < 0 || slot >= mPr_POCKETS_SLOT_COUNT) {
        printf("[NET][DROP] request %u accepted but the RESULT disagrees with the request (tile %d,%d item=%u) -- aborting, "
               "nothing cleared\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)in->placed_item);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STATE_CHANGED, in->request_id);
        return;
    }
    if (!pcnetgame_owner_stamp_matches(&s_drop_pending.owner)) {
        printf("[NET][DROP] request %u accepted (item=%u) but the local player/save changed -- aborting, nothing cleared\n",
               (unsigned)in->request_id, (unsigned)in->placed_item);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STATE_CHANGED, in->request_id);
        return;
    }
    /* Owner stamp matched, so Now_Private is non-NULL and the save is loaded. */
    if (Now_Private->inventory.pockets[slot] != (mActor_name_t)s_drop_pending.claimed_item) {
        printf("[NET][DROP] request %u accepted (item=%u) but pocket slot %d no longer holds it (now 0x%04X) -- aborting, "
               "nothing cleared\n",
               (unsigned)in->request_id, (unsigned)in->placed_item, slot,
               (unsigned)Now_Private->inventory.pockets[slot]);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_SLOT_CHANGED, in->request_id);
        return;
    }
    if (!pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_COMMIT, (uint8_t)PC_NETGAME_CONFIRM_REASON_NONE,
                                       in->request_id)) {
        printf("[NET][DROP] request %u accepted (item=%u) but the CONFIRM could not be queued -- cancelled, nothing cleared "
               "(the host's reservation will expire)\n",
               (unsigned)in->request_id, (unsigned)in->placed_item);
        return;
    }

    /* The one place this file writes to Now_Private for a drop -- via the real, unmodified
       mPr_SetPossessionItem(), exactly as mTG_field_put_proc() would have called it locally in
       single-player, after the slot was verified above. The field tile itself is handled by the
       FIELD_UPDATE the host sends on COMMIT, not here. */
    mPr_SetPossessionItem(Now_Private, slot, (mActor_name_t)EMPTY_NO, mPr_ITEM_COND_NORMAL);
    printf("[NET][DROP] request %u accepted (item=%u) -- cleared pocket slot %d\n", (unsigned)in->request_id,
           (unsigned)in->placed_item, slot);
}

/* =============================== v2 client world apply =============================== */

static void pcnetgame_client_defer_clear(int acre, int tile) {
    s_client_deferred_mask[acre][tile >> 4] &= (uint16_t)~(1u << (tile & 15));
}

static void pcnetgame_client_defer_store(int acre, int tile, uint16_t value, int dep_valid, int dep_on,
                                         uint16_t local) {
    if (!(s_client_deferred_mask[acre][tile >> 4] & (1u << (tile & 15)))) {
        s_client_deferred_since[acre][tile] = pcnetgame_now_ms(); /* keep the FIRST park time on updates */
    }
    s_client_deferred_mask[acre][tile >> 4] |= (uint16_t)(1u << (tile & 15));
    s_client_deferred_value[acre][tile] = value;
    pcnetgame_set_dep_bit(s_client_deferred_dep_valid[acre], tile, dep_valid);
    pcnetgame_set_dep_bit(s_client_deferred_dep_on[acre], tile, dep_on);
    s_client_deferred_acres |= (uint32_t)1u << acre;
    if (g_pc_verbose && !s_client_dummy_skip_logged && pcfa_transient_kind(local) == PCFA_VALUE_TRANSIENT) {
        s_client_dummy_skip_logged = 1;
        printf("[NET][WORLD] client: keeping local placeholder 0x%04X (acre %d tile %d) owned by a live local actor; "
               "host value 0x%04X parked until it is released (logged once)\n",
               (unsigned)local, acre, tile, (unsigned)value);
    }
}

/* Whether a host value may overwrite the local tile value right now:
 *   local STABLE    yes
 *   local TRANSIENT no  (DUMMY_* etc.: a live LOCAL actor owns the tile and will restore it)
 *   local AMBIGUOUS (RSV_NO, RSV_SIGNBOARD) never while this client is in the town scene (live
 *                   local structure actors keep door-front placeholders there -- same reasoning as
 *                   the host's rule); when not in town, once the host value has been parked for
 *                   PC_NETGAME_AMBIGUOUS_SETTLE_MS -- an in-flight local reservation clears well
 *                   within that, a stuck one must not block the host's value forever. */
static int pcnetgame_client_local_overwritable(int acre, int tile, uint16_t local, int parked) {
    int kind = pcfa_transient_kind(local);
    if (kind == PCFA_VALUE_STABLE) {
        return 1;
    }
    if (kind == PCFA_VALUE_AMBIGUOUS && parked && !pcfa_scene_is_town() &&
        (uint32_t)(pcnetgame_now_ms() - s_client_deferred_since[acre][tile]) >= PC_NETGAME_AMBIGUOUS_SETTLE_MS) {
        return 1;
    }
    return 0;
}

/* World messages may only be applied while the local gameplay save is loaded; otherwise they are
 * discarded and a RESYNC_REQUEST is scheduled (sent once the save is back). Returns 1 if apply may
 * proceed. */
static int pcnetgame_client_can_apply_world(void) {
    if (s_local_world_latched) {
        return 1;
    }
    s_client_need_resync = 1;
    return 0;
}

static int pcnetgame_client_is_parked(int acre, int tile) {
    return (s_client_deferred_mask[acre][tile >> 4] >> (tile & 15)) & 1;
}

/* Writes one tile's authoritative state into the persistent field, unless the LOCAL tile is not
 * overwritable right now (pcnetgame_client_local_overwritable()) -- then the host value is parked
 * and written by pcnetgame_client_retry_deferred() once it is. */
static void pcnetgame_client_apply_tile(int acre, int tile, uint16_t value, int dep_valid, int dep_on) {
    uint16_t local;

    if (pcfa_transient_kind(value) == PCFA_VALUE_TRANSIENT) {
        return; /* never world state (contract section 4) -- a well-behaved host never sends this */
    }
    if (!pcfa_get_tile(acre, tile, &local)) {
        return;
    }
    if (!pcnetgame_client_local_overwritable(acre, tile, local, pcnetgame_client_is_parked(acre, tile))) {
        pcnetgame_client_defer_store(acre, tile, value, dep_valid, dep_on, local);
        return;
    }
    pcnetgame_client_defer_clear(acre, tile);

    pcfa_set_net_apply_active(1);
    if (dep_valid && pcfa_get_deposit(acre, tile) != (dep_on ? 1 : 0)) {
        if (!pcfa_set_deposit(acre, tile, dep_on)) {
            s_client_need_resync = 1; /* refused (save not ready): never silently lost */
        }
    }
    if (local != value && !pcfa_set_tile(acre, tile, value)) {
        s_client_need_resync = 1;
    }
    pcfa_set_net_apply_active(0);
}

/* Writes a whole acre (FIELD_BLOCK) into the persistent field with one pcfa_write_acre() (which
 * itself skips the write/refresh if nothing changed). Tiles the host marked valid=0 are left
 * untouched; non-overwritable local tiles are parked exactly as in pcnetgame_client_apply_tile(). */
static void pcnetgame_client_apply_block(const PCNetGameFieldBlockMsg* fb, int count_stats) {
    uint16_t items[PCFA_TILE_NUM];
    uint16_t deposit[PCFA_DEPOSIT_ROWS];
    int acre = fb->acre;
    int acre_changed = 0;
    int t;

    if (!pcfa_read_acre(acre, items, deposit)) {
        return;
    }
    for (t = 0; t < PCFA_TILE_NUM; t++) {
        uint16_t in_v = fb->items[t];
        int in_d = pcnetgame_dep_bit(fb->deposit, t);

        if (!pcnetgame_dep_bit(fb->valid, t) || pcfa_transient_kind(in_v) == PCFA_VALUE_TRANSIENT) {
            if (count_stats) {
                s_client_snap_stats.skipped++;
                pcnetgame_client_log_snapshot_nowrite(0, acre, t, items[t], in_v,
                                                      !pcnetgame_dep_bit(fb->valid, t) ? "host valid=0" : "host transient");
            }
            continue; /* host knows no stable value for this tile -- keep local */
        }
        if (!pcnetgame_client_local_overwritable(acre, t, items[t], pcnetgame_client_is_parked(acre, t))) {
            pcnetgame_client_defer_store(acre, t, in_v, 1, in_d, items[t]);
            if (count_stats) {
                const char* why = (pcfa_transient_kind(items[t]) == PCFA_VALUE_TRANSIENT) ? "local live-placeholder"
                                  : pcfa_scene_is_town()                                   ? "local ambiguous in town"
                                                                                           : "local ambiguous settling";
                s_client_snap_stats.parked++;
                pcnetgame_client_log_snapshot_nowrite(1, acre, t, items[t], in_v, why);
            }
            continue;
        }
        pcnetgame_client_defer_clear(acre, t);
        if (count_stats && (items[t] != in_v || pcnetgame_dep_bit(deposit, t) != in_d)) {
            int old_d = pcnetgame_dep_bit(deposit, t);
            acre_changed = 1;
            s_client_snap_stats.tiles++;
            if (g_pc_verbose && s_client_snap_stats.logged < PC_NETGAME_SNAPSHOT_TILE_LOG_MAX) {
                s_client_snap_stats.logged++;
                printf("[NET][WORLD] client: snapshot tile acre %d tile %d: 0x%04X -> 0x%04X", acre, t,
                       (unsigned)items[t], (unsigned)in_v);
                if (old_d != in_d) {
                    printf(" (deposit %d -> %d)", old_d, in_d);
                }
                printf("\n");
            }
        }
        items[t] = in_v;
        pcnetgame_set_dep_bit(deposit, t, in_d);
    }
    if (count_stats && acre_changed) {
        s_client_snap_stats.acres++;
    }
    pcfa_set_net_apply_active(1);
    if (!pcfa_write_acre(acre, items, deposit)) {
        s_client_need_resync = 1;
    }
    pcfa_set_net_apply_active(0);
}

/* Parked host values whose local reservation has since cleared are written now. */
static void pcnetgame_client_retry_deferred(void) {
    int acre;

    if (s_client_deferred_acres == 0 || !s_local_world_latched) {
        return;
    }
    for (acre = 0; acre < PCFA_ACRE_NUM; acre++) {
        int row, any = 0;
        if (!(s_client_deferred_acres & ((uint32_t)1u << acre))) {
            continue;
        }
        for (row = 0; row < PCFA_DEPOSIT_ROWS; row++) {
            uint16_t bits = s_client_deferred_mask[acre][row];
            int ux;
            for (ux = 0; bits != 0 && ux < 16; ux++) {
                int tile = row * 16 + ux;
                uint16_t local;
                if (!(bits & (1u << ux))) {
                    continue;
                }
                if (pcfa_get_tile(acre, tile, &local) && pcnetgame_client_local_overwritable(acre, tile, local, 1)) {
                    pcnetgame_client_apply_tile(acre, tile, s_client_deferred_value[acre][tile],
                                                pcnetgame_dep_bit(s_client_deferred_dep_valid[acre], tile),
                                                pcnetgame_dep_bit(s_client_deferred_dep_on[acre], tile));
                }
            }
            if (s_client_deferred_mask[acre][row] != 0) {
                any = 1;
            }
        }
        if (!any) {
            s_client_deferred_acres &= ~((uint32_t)1u << acre);
        }
    }
}

/* Isolated on purpose (Workstream D owns the semantics): the host's all_grow_renew_time becomes the
 * client's, so a client never runs its own daily renewal for a day the host already renewed. */
static void pcnetgame_client_apply_renew_time(const PCNetGameRtcWire* w) {
    lbRTC_time_c* t = Save_GetPointer(all_grow_renew_time);
    t->sec = w->sec;
    t->min = w->min;
    t->hour = w->hour;
    t->day = w->day;
    t->weekday = w->weekday;
    t->month = w->month;
    t->year = w->year;
}

/* Weather + Stalk Market milestone: mirrors the host's broadcast weather into this client's own
 * Common_t exactly (Common_Get(weather)/Common_Get(weather_intensity), read by every rendering/
 * gameplay site via mEnv_NowWeather() etc. -- see m_kankyo_weather.c_inc), and into the packed
 * Save_Get(weather) byte too, purely so that a later call into vanilla weather code (e.g.
 * mEnv_DecideWeather_NormalGameStart()'s else-branch, which still reads Save_Get(weather) when this
 * client's own local mTM_check_renew_time flag is NOT set) sees a value consistent with what was just
 * applied here -- never persisted to disk (pc_save_write_authoritative() already refuses every write
 * from a network CLIENT process, so this in-memory mirror can never reach the client's own save
 * file). A client never independently rolls a new value (see mEnv_DecideWeather_NormalGameStart()'s
 * host-authority gate) -- this function is the ONLY place a client's weather ever changes. */
static void pcnetgame_client_apply_weather_state(const PCNetGameWorldStateWire* w) {
    Common_Set(weather, (s16)w->weather);
    Common_Set(weather_intensity, (s16)w->weather_intensity);
    Save_Set(weather, (u8)(w->weather_intensity | (w->weather << 4)));
}

/* Weather + Stalk Market milestone: mirrors the host's broadcast Stalk Market schedule into this
 * client's own Save_Get(kabu_price_schedule) field-for-field, so Kabu_get_price() (m_kabu_manager.c,
 * reads Save_Get(kabu_price_schedule) directly, unchanged) returns the host's authoritative price on
 * this client too. Not persisted to disk for the same reason noted on
 * pcnetgame_client_apply_weather_state() above. A client never independently regenerates a schedule
 * (see Kabu_manager()'s host-authority gate) -- this function is the ONLY place a client's Stalk
 * Market state ever changes. Player-private turnip INVENTORY is untouched here -- it is not part of
 * PCNetGameWorldStateWire at all (see that struct's own doc comment). */
static void pcnetgame_client_apply_market_state(const PCNetGameWorldStateWire* w) {
    Kabu_price_c* kabu = Save_GetPointer(kabu_price_schedule);
    int i;

    for (i = 0; i < lbRTC_WEEKDAYS_MAX; i++) {
        kabu->daily_price[i] = w->daily_price[i];
    }
    kabu->trade_market = w->trade_market;
    kabu->update_time.sec = w->kabu_update_time.sec;
    kabu->update_time.min = w->kabu_update_time.min;
    kabu->update_time.hour = w->kabu_update_time.hour;
    kabu->update_time.day = w->kabu_update_time.day;
    kabu->update_time.weekday = w->kabu_update_time.weekday;
    kabu->update_time.month = w->kabu_update_time.month;
    kabu->update_time.year = w->kabu_update_time.year;
}

/* Client side: the host's authoritative statement about one field tile (v2) -- see
 * PCNetGameFieldUpdateMsg. Applies to every client, including the requester (which also gets its
 * own PICKUP/DROP_RESULT). */
static void pcnetgame_handle_client_field_update(const PCNetGameFieldUpdateMsg* in) {
    if (in->grid != PC_NETGAME_GRID_TOWN || in->acre >= PCFA_ACRE_NUM) {
        return; /* tile is a uint8_t, always 0..255 */
    }
    if (!pcnetgame_client_can_apply_world()) {
        return;
    }
    if (in->world_seq <= s_client_acre_seq[in->acre]) {
        if (g_pc_verbose) {
            printf("[NET][WORLD] client: stale FIELD_UPDATE acre %u seq %u <= %u ignored\n", (unsigned)in->acre,
                   (unsigned)in->world_seq, (unsigned)s_client_acre_seq[in->acre]);
        }
        return;
    }
    pcnetgame_client_apply_tile(in->acre, in->tile, in->value, (in->flags & PC_NETGAME_FU_FLAG_DEPOSIT_VALID) != 0,
                                (in->flags & PC_NETGAME_FU_FLAG_DEPOSIT_ON) != 0);
    s_client_acre_seq[in->acre] = in->world_seq;
    if (g_pc_verbose) {
        printf("[NET][WORLD] client: FIELD_UPDATE acre %u tile %u = 0x%04X (seq %u)\n", (unsigned)in->acre,
               (unsigned)in->tile, (unsigned)in->value, (unsigned)in->world_seq);
    }
}

static void pcnetgame_handle_client_field_block(const PCNetGameFieldBlockMsg* in) {
    int in_snapshot = (in->flags & PC_NETGAME_FB_FLAG_IN_SNAPSHOT) != 0;

    if (in->grid != PC_NETGAME_GRID_TOWN || in->acre >= PCFA_ACRE_NUM) {
        return;
    }
    if (in_snapshot && (!s_client_snap_active || in->epoch != s_client_snap_epoch)) {
        return; /* part of a superseded snapshot */
    }
    if (!pcnetgame_client_can_apply_world()) {
        return;
    }
    if (in->world_seq < s_client_acre_seq[in->acre]) {
        if (g_pc_verbose) {
            printf("[NET][WORLD] client: stale FIELD_BLOCK acre %u seq %u < %u ignored\n", (unsigned)in->acre,
                   (unsigned)in->world_seq, (unsigned)s_client_acre_seq[in->acre]);
        }
        return;
    }
    pcnetgame_client_apply_block(in, in_snapshot);
    s_client_acre_seq[in->acre] = in->world_seq;
    if (in_snapshot) {
        s_client_snap_blocks++;
    }
}

static void pcnetgame_handle_client_snapshot_begin(const PCNetGameSnapshotBeginMsg* in) {
    if (in->grid != PC_NETGAME_GRID_TOWN) {
        return;
    }
    s_client_snap_active = 1;
    s_client_snap_epoch = in->epoch;
    s_client_snap_blocks = 0;
    memset(&s_client_snap_stats, 0, sizeof(s_client_snap_stats));
    printf("[NET][WORLD] client: snapshot epoch %u begin (%u acres, host world_seq %u)\n", (unsigned)in->epoch,
           (unsigned)in->acre_count, (unsigned)in->world_seq);
}

static void pcnetgame_handle_client_snapshot_end(const PCNetGameSnapshotEndMsg* in) {
    if (in->grid != PC_NETGAME_GRID_TOWN || !s_client_snap_active || in->epoch != s_client_snap_epoch) {
        return;
    }
    s_client_snap_active = 0;
    if (!pcnetgame_client_can_apply_world()) {
        return; /* blocks were discarded too; the resync fetches a complete one */
    }
    if (in->world_seq >= s_client_meta_seq) {
        if (in->flags & PC_NETGAME_META_FLAG_RENEW_TIME_VALID) {
            pcnetgame_client_apply_renew_time(&in->renew_time);
        }
        if (in->flags & PC_NETGAME_META_FLAG_WEATHER_VALID) {
            pcnetgame_client_apply_weather_state(&in->world_state);
        }
        if (in->flags & PC_NETGAME_META_FLAG_MARKET_VALID) {
            pcnetgame_client_apply_market_state(&in->world_state);
        }
        s_client_meta_seq = in->world_seq;
    }
    if (s_client_snap_blocks != (int)in->acre_count) {
        printf("[NET][WORLD] client: snapshot epoch %u ended with %d/%u blocks applied\n", (unsigned)in->epoch,
               s_client_snap_blocks, (unsigned)in->acre_count);
    }
    s_client_world_synced = 1;
    /* Per-tile detail (and its "(N more)" overflow lines) is DEBUG-ONLY; the summary below always prints. */
    if (g_pc_verbose && s_client_snap_stats.parked > s_client_snap_stats.parked_logged) {
        printf("[NET][WORLD] client: snapshot tile parked ... (%d more)\n",
               s_client_snap_stats.parked - s_client_snap_stats.parked_logged);
    }
    if (g_pc_verbose && s_client_snap_stats.skipped > s_client_snap_stats.skipped_logged) {
        printf("[NET][WORLD] client: snapshot tile skipped ... (%d more)\n",
               s_client_snap_stats.skipped - s_client_snap_stats.skipped_logged);
    }
    if (g_pc_verbose && s_client_snap_stats.tiles > s_client_snap_stats.logged) {
        printf("[NET][WORLD] client: snapshot tile ... (%d more)\n", s_client_snap_stats.tiles - s_client_snap_stats.logged);
    }
    printf("[NET][WORLD] client: snapshot epoch %u applied (%d acres, world_seq %u, renew %04u-%02u-%02u) "
           "changed_tiles=%d changed_acres=%d parked=%d skipped_transient=%d\n",
           (unsigned)in->epoch, s_client_snap_blocks, (unsigned)in->world_seq, (unsigned)in->renew_time.year,
           (unsigned)in->renew_time.month, (unsigned)in->renew_time.day, s_client_snap_stats.tiles,
           s_client_snap_stats.acres, s_client_snap_stats.parked, s_client_snap_stats.skipped);
}

static void pcnetgame_handle_client_world_meta(const PCNetGameWorldMetaMsg* in) {
    if (!pcnetgame_client_can_apply_world()) {
        return;
    }
    if (in->flags == 0 || in->world_seq <= s_client_meta_seq) {
        return;
    }
    if (in->flags & PC_NETGAME_META_FLAG_RENEW_TIME_VALID) {
        pcnetgame_client_apply_renew_time(&in->renew_time);
        printf("[NET][WORLD] client: host renew time -> %04u-%02u-%02u (world_seq %u)\n",
               (unsigned)in->renew_time.year, (unsigned)in->renew_time.month, (unsigned)in->renew_time.day,
               (unsigned)in->world_seq);
    }
    if (in->flags & PC_NETGAME_META_FLAG_WEATHER_VALID) {
        pcnetgame_client_apply_weather_state(&in->world_state);
        printf("[NET][WORLD] client: host weather -> type %u intensity %u (world_seq %u)\n",
               (unsigned)in->world_state.weather, (unsigned)in->world_state.weather_intensity,
               (unsigned)in->world_seq);
    }
    if (in->flags & PC_NETGAME_META_FLAG_MARKET_VALID) {
        pcnetgame_client_apply_market_state(&in->world_state);
        printf("[NET][WORLD] client: host Stalk Market schedule -> trend %u sunday %u (world_seq %u)\n",
               (unsigned)in->world_state.trade_market, (unsigned)in->world_state.daily_price[0],
               (unsigned)in->world_seq);
    }
    s_client_meta_seq = in->world_seq;
}

/* Villager population/is_home milestone, client side. Strict `>` staleness rule (matches WORLD_META,
 * not the `>=` snapshot rule) -- an ARRIVAL/DEPARTURE is a one-shot delta, not a full resync, so a
 * duplicate or reordered-behind delivery of the SAME world_seq must never be re-applied (re-applying
 * an ARRIVAL would re-clear-then-regrow the slot, losing nothing but wastefully rebuilding npclist;
 * re-applying a DEPARTURE is a harmless no-op via mNpc_PcApplyVillagerDeparture()'s own
 * mNpc_CheckFreeAnimalInfo() guard -- but the seq check makes the intent explicit and cheap rather
 * than relying on that incidental idempotency). All fields are validated again inside
 * mNpc_PcApplyVillagerArrival()/mNpc_PcApplyVillagerDeparture() themselves (slot range, npc_id shape)
 * as defense in depth -- a malformed or adversarial message can never crash or corrupt Save_t. */
static void pcnetgame_handle_client_villager_arrival(const PCNetGameVillagerArrivalMsg* in) {
    if (!pcnetgame_client_can_apply_world()) {
        return;
    }
    if (in->world_seq <= s_client_population_seq) {
        if (g_pc_verbose) {
            printf("[NET][NPC] client: stale VILLAGER_ARRIVAL slot %u world_seq %u <= %u ignored\n",
                   (unsigned)in->slot, (unsigned)in->world_seq, (unsigned)s_client_population_seq);
        }
        return;
    }
    mNpc_PcApplyVillagerArrival((int)in->slot, in->npc_id, in->reserved_block_x, in->reserved_block_z,
                                in->reserved_ut_x, in->reserved_ut_z, in->now_npc_max);
    s_client_population_seq = in->world_seq;
    printf("[NET][NPC] client: villager arrived slot %u npc_id 0x%04X (world_seq %u, now_npc_max %u)\n",
           (unsigned)in->slot, (unsigned)in->npc_id, (unsigned)in->world_seq, (unsigned)in->now_npc_max);
}

static void pcnetgame_handle_client_villager_departure(const PCNetGameVillagerDepartureMsg* in) {
    if (!pcnetgame_client_can_apply_world()) {
        return;
    }
    if (in->world_seq <= s_client_population_seq) {
        if (g_pc_verbose) {
            printf("[NET][NPC] client: stale VILLAGER_DEPARTURE slot %u world_seq %u <= %u ignored\n",
                   (unsigned)in->slot, (unsigned)in->world_seq, (unsigned)s_client_population_seq);
        }
        return;
    }
    mNpc_PcApplyVillagerDeparture((int)in->slot, in->now_npc_max);
    s_client_population_seq = in->world_seq;
    printf("[NET][NPC] client: villager departed slot %u (world_seq %u, now_npc_max %u)\n", (unsigned)in->slot,
           (unsigned)in->world_seq, (unsigned)in->now_npc_max);
}

/* Villager population/is_home milestone, client side: full late-join/reconnect population sync.
 * `>=` rule (matches FIELD_BLOCK/SNAPSHOT_END, not WORLD_META's strict `>`): a full snapshot always
 * supersedes whatever came before, including one at the same world_seq (e.g. a RESYNC_REQUEST
 * re-fetch after no population change occurred). Applies every slot unconditionally (occupied or not)
 * so a slot the client thinks is occupied but the host's snapshot says is empty gets correctly
 * cleared too -- never a partial/merge apply. */
static void pcnetgame_handle_client_villager_snapshot(const PCNetGameVillagerSnapshotMsg* in) {
    int i;

    if (in->grid != PC_NETGAME_GRID_TOWN) {
        return;
    }
    if (!s_client_snap_active || !pcnetgame_client_can_apply_world()) {
        return;
    }
    if (in->world_seq < s_client_population_seq) {
        if (g_pc_verbose) {
            printf("[NET][NPC] client: stale VILLAGER_SNAPSHOT world_seq %u < %u ignored\n",
                   (unsigned)in->world_seq, (unsigned)s_client_population_seq);
        }
        return;
    }
    for (i = 0; i < ANIMAL_NUM_MAX; i++) {
        const PCNetGameVillagerSlotWire* slot = &in->slots[i];
        if (slot->occupied) {
            mNpc_PcApplyVillagerSnapshotSlot(i, slot->npc_id, slot->home_block_x, slot->home_block_z,
                                             slot->home_ut_x, slot->home_ut_z, slot->is_home, in->now_npc_max);
        } else {
            mNpc_PcApplyVillagerDeparture(i, in->now_npc_max);
        }
    }
    s_client_population_seq = in->world_seq;
    printf("[NET][NPC] client: villager population snapshot applied (world_seq %u, now_npc_max %u)\n",
           (unsigned)in->world_seq, (unsigned)in->now_npc_max);
}

/* v2 host side: validate a parked IDENTITY against the host's own town, then either reject+drop or
 * accept (ACK -> READY -> roster -> snapshot). Only called with the host world ready. */
static void pcnetgame_host_process_identity(PCNetPeerId peer) {
    PCNetGameIdentityMsg in = s_host_peer[peer].pending_identity; /* copy: the reset below clears it */
    PCNetGameTownIdentity peer_town;
    char host_buf[96], peer_buf[96];

    s_host_peer[peer].identity_pending = 0;

    memcpy(peer_town.land_name, in.land_name, PC_NETGAME_LAND_LEN);
    peer_town.land_id = in.land_id;
    peer_town.terrain_hash = in.terrain_hash;
    pcnetgame_format_town(&s_host_town, host_buf, sizeof(host_buf));
    pcnetgame_format_town(&peer_town, peer_buf, sizeof(peer_buf));

    if (!in.has_save) {
        printf("[NET] host: peer %d sent IDENTITY without a loaded save -- rejecting (NO_SAVE)\n", (int)peer);
        pcnetgame_host_reject_and_close(peer, pcnetgame_send_reject_town(peer, PC_NETGAME_REJECT_NO_SAVE, &s_host_town));
        return;
    }
    if (!pcnetgame_town_equal(&peer_town, &s_host_town)) {
        printf("[NET] host: peer %d is in a different town (peer %s, host %s) -- rejecting (LAND_MISMATCH)\n",
               (int)peer, peer_buf, host_buf);
        pcnetgame_host_reject_and_close(peer, pcnetgame_send_reject_town(peer, PC_NETGAME_REJECT_LAND_MISMATCH, &s_host_town));
        return;
    }

    /* Accept. Reset every per-peer cache FIRST (a reused slot inherits nothing), then build state. */
    pcnetgame_reset_all_host_peer_state(peer);

    {
        PCNetGameIdentityAckMsg ack;
        uint8_t unused_land_name[PC_NETGAME_LAND_LEN];
        uint16_t unused_land_id;
        uint8_t unused_has_save;
        memset(&ack, 0, sizeof(ack));
        ack.msg_type = (uint8_t)PC_NETGAME_MSG_IDENTITY_ACK;
        ack.accepted = 1;
        ack.assigned_peer_id = (uint16_t)peer;
        ack.protocol_version = PC_NETGAME_PROTOCOL_VERSION;
        pcnetgame_capture_local_identity(ack.player_name, unused_land_name, &ack.player_id, &unused_land_id,
                                          &unused_has_save);
        memcpy(ack.land_name, s_host_town.land_name, PC_NETGAME_LAND_LEN);
        ack.land_id = s_host_town.land_id;
        ack.terrain_hash = s_host_town.terrain_hash;
        if (!pc_net_send(peer, PC_NET_RELIABLE, &ack, (uint16_t)sizeof(ack))) {
            /* Never a half-READY peer: without the ACK the client would never become READY. */
            printf("[NET] host: peer %d IDENTITY_ACK could not be queued -- dropping peer\n", (int)peer);
            pcnetgame_host_drop_peer(peer);
            return;
        }
    }
    s_host_peer_link[peer] = PC_NETGAME_LINK_READY;
    printf("[NET] host: peer %d identity OK (player_id=%u, %s) -> READY\n", (int)peer, (unsigned)in.player_id, peer_buf);

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

    /* v2: bootstrap the client's world (initial connect, late join and reconnect all land here). */
    pcnetgame_host_start_snapshot(peer, "joined");
}

/* v2 host side: an IDENTITY-typed payload from a peer still in HANDSHAKE. The protocol version is
 * checked immediately (offset 4 is frozen, so any version's IDENTITY is readable); a same-version
 * IDENTITY is parked and validated as soon as the host's own save is loaded -- the peer is never
 * rejected just because the host is still on the title screen. */
static void pcnetgame_handle_host_identity(PCNetPeerId peer, const uint8_t* data, uint16_t size) {
    PCNetGameHostPeerState* st = &s_host_peer[peer];
    uint32_t version;

    if (s_host_peer_link[peer] != PC_NETGAME_LINK_HANDSHAKE) {
        if (g_pc_verbose) {
            printf("[NET] host: ignoring IDENTITY from peer %d (link state %d)\n", (int)peer, (int)s_host_peer_link[peer]);
        }
        return;
    }
    memcpy(&version, data + offsetof(PCNetGameIdentityMsg, protocol_version), sizeof(version));
    if (version != PC_NETGAME_PROTOCOL_VERSION) {
        printf("[NET] host: peer %d speaks protocol %u, we require %u -- rejecting\n", (int)peer, (unsigned)version,
               (unsigned)PC_NETGAME_PROTOCOL_VERSION);
        pcnetgame_host_reject_and_close(peer, pcnetgame_send_reject(peer, PC_NETGAME_REJECT_PROTOCOL_MISMATCH));
        return;
    }
    if (size != sizeof(PCNetGameIdentityMsg)) {
        return; /* right version, wrong size: malformed -- ignore rather than misinterpret */
    }

    memcpy(&st->pending_identity, data, sizeof(st->pending_identity));
    st->identity_pending = 1;
    printf("[NET] host: identity received from peer %d (player_id=%u land_id=0x%04X hash=0x%08X has_save=%d)\n",
           (int)peer, (unsigned)st->pending_identity.player_id, (unsigned)st->pending_identity.land_id,
           (unsigned)st->pending_identity.terrain_hash, (int)st->pending_identity.has_save);

    if (s_host_world_ready) {
        pcnetgame_host_process_identity(peer);
    } else if (!st->identity_defer_logged) {
        st->identity_defer_logged = 1;
        printf("[NET] host: peer %d identity deferred -- host save not loaded yet (peer stays in HANDSHAKE)\n", (int)peer);
    }
}

/* v2 host side, once per poll: validates identities that were parked while the host save was not
 * loaded. A peer that disconnected meanwhile had its parked identity cleared by the reset. */
static void pcnetgame_host_process_pending_identities(void) {
    int i;
    if (!s_host_world_ready) {
        return;
    }
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_host_peer_link[i] == PC_NETGAME_LINK_HANDSHAKE && s_host_peer[i].identity_pending) {
            printf("[NET] host: host save loaded -- validating deferred identity of peer %d\n", i);
            pcnetgame_host_process_identity((PCNetPeerId)i);
        }
    }
}

/* v2 host side: PLAYER_CONTEXT from a READY peer. */
static void pcnetgame_handle_host_player_context(PCNetPeerId peer, const PCNetGamePlayerContextMsg* in) {
    PCNetGameHostPeerState* st = &s_host_peer[peer];
    if (s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }
    st->ctx.player_no = in->player_no;
    st->ctx.destiny_type = in->destiny_type;
    st->ctx.flags = in->flags;
    st->ctx.money_power = in->money_power;
    st->ctx.goods_power = in->goods_power;
    if (!st->ctx_valid || g_pc_verbose) {
        printf("[NET] host: peer %d context player_no=%u destiny=%u flags=0x%02X money=%d goods=%d\n", (int)peer,
               (unsigned)in->player_no, (unsigned)in->destiny_type, (unsigned)in->flags, (int)in->money_power,
               (int)in->goods_power);
    }
    st->ctx_valid = 1;
}

/* Host side: a peer's raw PC_NET_EVENT_DATA payload. Anything that isn't a well-formed message of
 * a known type/size is ignored -- a peer is only ever marked READY by successfully validating an
 * IDENTITY, never merely by having sent *some* UDP packet. */
static void pcnetgame_handle_host_data(PCNetPeerId peer, const uint8_t* data, uint16_t size) {
    if (peer < 0 || peer >= PC_NET_MAX_PEERS || size == 0) {
        return;
    }
    if (s_host_peer[peer].closing) {
        return; /* rejected peer lingering only so its REJECT can be retransmitted -- ignore everything */
    }

    if (data[0] == (uint8_t)PC_NETGAME_MSG_IDENTITY && size >= 8) {
        pcnetgame_handle_host_identity(peer, data, size);
        return;
    }

    if (size == sizeof(PCNetGamePlayerContextMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_PLAYER_CONTEXT) {
        PCNetGamePlayerContextMsg pc;
        memcpy(&pc, data, sizeof(pc));
        pcnetgame_handle_host_player_context(peer, &pc);
        return;
    }

    if (size == sizeof(PCNetGameResyncRequestMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_RESYNC_REQUEST) {
        if (s_host_peer_link[peer] == PC_NETGAME_LINK_READY) {
            pcnetgame_host_start_snapshot(peer, "client resync request");
        }
        return;
    }

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

    if (size == sizeof(PCNetGameInteractConfirmMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_INTERACT_CONFIRM) {
        PCNetGameInteractConfirmMsg cf;
        memcpy(&cf, data, sizeof(cf));
        pcnetgame_handle_host_confirm(peer, &cf);
        return;
    }

    /* malformed / short / unrecognized: ignore rather than misinterpret */
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
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&fu, data, sizeof(fu));
        pcnetgame_handle_client_field_update(&fu);
        return;
    }

    if (size == sizeof(PCNetGameFieldBlockMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_FIELD_BLOCK) {
        static PCNetGameFieldBlockMsg fb; /* 588 B: static, handled synchronously */
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&fb, data, sizeof(fb));
        pcnetgame_handle_client_field_block(&fb);
        return;
    }

    if (size == sizeof(PCNetGameSnapshotBeginMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_SNAPSHOT_BEGIN) {
        PCNetGameSnapshotBeginMsg b;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&b, data, sizeof(b));
        pcnetgame_handle_client_snapshot_begin(&b);
        return;
    }

    if (size == sizeof(PCNetGameSnapshotEndMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_SNAPSHOT_END) {
        PCNetGameSnapshotEndMsg e;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&e, data, sizeof(e));
        pcnetgame_handle_client_snapshot_end(&e);
        return;
    }

    if (size == sizeof(PCNetGameWorldMetaMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_WORLD_META) {
        PCNetGameWorldMetaMsg wm;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&wm, data, sizeof(wm));
        pcnetgame_handle_client_world_meta(&wm);
        return;
    }

    if (size == sizeof(PCNetGameVillagerArrivalMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_VILLAGER_ARRIVAL) {
        PCNetGameVillagerArrivalMsg va;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&va, data, sizeof(va));
        pcnetgame_handle_client_villager_arrival(&va);
        return;
    }

    if (size == sizeof(PCNetGameVillagerDepartureMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_VILLAGER_DEPARTURE) {
        PCNetGameVillagerDepartureMsg vd;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&vd, data, sizeof(vd));
        pcnetgame_handle_client_villager_departure(&vd);
        return;
    }

    if (size == sizeof(PCNetGameVillagerSnapshotMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_VILLAGER_SNAPSHOT) {
        static PCNetGameVillagerSnapshotMsg vs; /* 132 B: static, handled synchronously */
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&vs, data, sizeof(vs));
        pcnetgame_handle_client_villager_snapshot(&vs);
        return;
    }

    if (size == sizeof(PCNetGameIdentityAckMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_IDENTITY_ACK) {
        PCNetGameIdentityAckMsg in;
        PCNetGameTownIdentity host_town;
        memcpy(&in, data, sizeof(in));
        if (!in.accepted) return; /* host sends a separate REJECT message instead; defensive only */
        if (s_client_link != PC_NETGAME_LINK_HANDSHAKE || !s_client_identity_sent) {
            return; /* not answering anything we sent on this connection */
        }

        /* v2: re-validate. The host must speak our protocol and hold exactly the town we claimed. */
        memcpy(host_town.land_name, in.land_name, PC_NETGAME_LAND_LEN);
        host_town.land_id = in.land_id;
        host_town.terrain_hash = in.terrain_hash;
        if (in.protocol_version != PC_NETGAME_PROTOCOL_VERSION ||
            !pcnetgame_town_equal(&host_town, &s_client_claimed_town)) {
            char a[96], b[96];
            pcnetgame_format_town(&host_town, a, sizeof(a));
            pcnetgame_format_town(&s_client_claimed_town, b, sizeof(b));
            printf("[NET] client: IDENTITY_ACK does not match (host protocol %u %s, ours %u %s) -- disconnecting\n",
                   (unsigned)in.protocol_version, a, (unsigned)PC_NETGAME_PROTOCOL_VERSION, b);
            pc_net_game_shutdown();
            return;
        }

        memcpy(s_client_host_identity.player_name, in.player_name, PC_NETGAME_NAME_LEN);
        memcpy(s_client_host_identity.land_name, in.land_name, PC_NETGAME_LAND_LEN);
        s_client_host_identity.player_id = in.player_id;
        s_client_host_identity.land_id = in.land_id;
        s_client_host_identity.has_save = 1;
        s_client_host_identity_valid = 1;
        s_client_assigned_peer_id = in.assigned_peer_id;

        s_client_link = PC_NETGAME_LINK_READY;
        printf("[NET] client: handshake complete, town verified (assigned peer id %u) -> READY\n",
               (unsigned)in.assigned_peer_id);

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
        /* v2: PLAYER_CONTEXT goes out from pcnetgame_client_tick() later in this same poll (the
         * "last sent" cache was cleared by the connection reset, so it always sends once). */

        /* Stage 2: give the client a visible representation of the host. Tracked under the
         * reserved PC_NETGAME_HOST_PLAYER_ID (a PCNetPlayerId), NOT the client's own transport
         * PCNetPeerId (which is a different, transport-only number space -- see pc_net_game.h). */
        pc_remote_player_on_ready(PC_NETGAME_HOST_PLAYER_ID, &s_client_host_identity);
        return;
    }

    if ((size == sizeof(PCNetGameRejectMsg) || size == sizeof(PCNetGameRejectTownMsg)) &&
        data[0] == (uint8_t)PC_NETGAME_MSG_REJECT) {
        PCNetGameRejectMsg in;
        memcpy(&in, data, sizeof(in)); /* first 8 bytes are common to both forms */
        printf("[NET] client: host rejected the connection (reason=%u, host requires protocol %u, we sent %u)\n",
               (unsigned)in.reason, (unsigned)in.expected_protocol_version, (unsigned)PC_NETGAME_PROTOCOL_VERSION);
        if (size == sizeof(PCNetGameRejectTownMsg)) {
            PCNetGameRejectTownMsg rt;
            PCNetGameTownIdentity host_town;
            char a[96], b[96];
            memcpy(&rt, data, sizeof(rt));
            memcpy(host_town.land_name, rt.land_name, PC_NETGAME_LAND_LEN);
            host_town.land_id = rt.land_id;
            host_town.terrain_hash = rt.terrain_hash;
            pcnetgame_format_town(&host_town, a, sizeof(a));
            pcnetgame_format_town(&s_client_claimed_town, b, sizeof(b));
            printf("[NET] client: %s -- host town %s, ours %s\n",
                   rt.reason == PC_NETGAME_REJECT_LAND_MISMATCH ? "LAND_MISMATCH" :
                   rt.reason == PC_NETGAME_REJECT_NO_SAVE       ? "NO_SAVE" : "rejected",
                   a, b);
        }
        pc_net_game_shutdown(); /* clean and immediate -- no need to wait for a transport timeout */
        return;
    }

    /* malformed/short/unrecognized: ignore */
}

/* v2: THE one place the client's per-connection state is cleared -- called from
 * pc_net_game_start_client(), on PC_NET_EVENT_PEER_CONNECTED (a fresh transport connection), on
 * PC_NET_EVENT_PEER_DISCONNECTED, and from pc_net_game_shutdown(). Everything a new connection must
 * not inherit lives here: handshake progress, host identity, request ids and pending requests (so a
 * new connection -- whose host-side slot has fresh dedup caches -- starts at id 1), per-acre world
 * seqs, snapshot epoch/progress, resync/pause flags, the PLAYER_CONTEXT change cache and parked
 * tiles. (Stage 5A/5B used to reset these separately in start_client and the disconnect handler.) */
static void pcnetgame_reset_client_session_state(void) {
    memset(&s_host_local_drop_landing, 0, sizeof(s_host_local_drop_landing)); /* (host-only, harmless here) */
    s_client_host_identity_valid = 0;
    memset(&s_client_host_identity, 0, sizeof(s_client_host_identity));
    s_client_identity_sent = 0;
    s_client_identity_defer_logged = 0;
    memset(&s_client_claimed_town, 0, sizeof(s_client_claimed_town));
    s_client_assigned_peer_id = 0;

    memset(&s_pickup_pending, 0, sizeof(s_pickup_pending));
    s_next_pickup_request_id = 1;
    memset(&s_drop_pending, 0, sizeof(s_drop_pending));
    s_next_drop_request_id = 1;

    memset(s_client_acre_seq, 0, sizeof(s_client_acre_seq));
    s_client_meta_seq = 0;
    s_client_population_seq = 0;
    s_client_snap_active = 0;
    s_client_snap_epoch = 0;
    s_client_snap_blocks = 0;
    memset(&s_client_snap_stats, 0, sizeof(s_client_snap_stats));
    s_client_world_synced = 0;
    s_client_need_resync = 0;
    s_client_paused = 0;
    memset(&s_client_last_ctx, 0, sizeof(s_client_last_ctx));
    s_client_last_ctx_valid = 0;

    s_client_deferred_acres = 0;
    memset(s_client_deferred_mask, 0, sizeof(s_client_deferred_mask));
    s_client_dummy_skip_logged = 0;
}

/* v2: clears every piece of host world state (start_host / shutdown). */
static void pcnetgame_reset_host_world_state(void) {
    int i;
    memset(&s_host_local_drop_landing, 0, sizeof(s_host_local_drop_landing)); /* no stale arm across sessions */
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        s_host_peer_link[i] = PC_NETGAME_LINK_DISCONNECTED;
        pcnetgame_reset_all_host_peer_state((PCNetPeerId)i);
    }
    s_host_world_ready = 0;
    s_host_shadow_valid = 0;
    s_host_town_valid = 0;
    memset(&s_host_town, 0, sizeof(s_host_town));
    s_host_carry_dirty = 0;
    s_host_scan_cursor = 0;
    s_world_seq = 0;
    s_snapshot_epoch_counter = 0;
    s_host_meta_valid = 0;
    pcnetgame_out_reset();
}

/* v2 client, once per poll after events: deferred IDENTITY send, save pause/resume + resync,
 * town-change detection, PLAYER_CONTEXT change detection, parked-tile retry. */
static void pcnetgame_client_tick(void) {
    int ready = s_local_world_latched;

    if (s_client_link == PC_NETGAME_LINK_HANDSHAKE && !s_client_identity_sent) {
        if (ready) {
            PCNetGameIdentityMsg msg;
            char buf[96];
            pcnetgame_capture_town_identity(&s_client_claimed_town);
            pcnetgame_build_identity_msg(&msg, &s_client_claimed_town);
            if (pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
                s_client_identity_sent = 1;
                pcnetgame_format_town(&s_client_claimed_town, buf, sizeof(buf));
                printf("[NET] client: save loaded -- sending identity (%s)\n", buf);
            }
        } else if (!s_client_identity_defer_logged) {
            s_client_identity_defer_logged = 1;
            printf("[NET] client: waiting for a loaded save before sending identity\n");
        }
        return;
    }

    if (s_client_link != PC_NETGAME_LINK_READY) {
        return;
    }

    if (!ready) {
        if (!s_client_paused) {
            s_client_paused = 1;
            printf("[NET][WORLD] client: local save not loaded -- world apply paused\n");
        }
        /* Pending pickup/drop requests belong to the save that is going away: cancel them now (and
           tell the host to release), so a late RESULT can never be applied to whatever gets loaded
           next. Also re-checked by owner stamp below for a same-frame save swap. */
        pcnetgame_client_cancel_pending(0, "local save not loaded");
        s_client_need_resync = 1; /* the Save may be reloaded from disk before it comes back */
        return;
    }
    /* The local player must still be the one that sent a pending request (quit to title and load
       another resident of the same town, ...): otherwise cancel it. */
    pcnetgame_client_cancel_pending(1, "local player/save changed");

    {
        PCNetGameTownIdentity cur;
        pcnetgame_capture_town_identity(&cur);
        if (!pcnetgame_town_equal(&cur, &s_client_claimed_town)) {
            char a[96], b[96];
            pcnetgame_format_town(&cur, a, sizeof(a));
            pcnetgame_format_town(&s_client_claimed_town, b, sizeof(b));
            printf("[NET] client: local save now holds a different town (%s, connected as %s) -- disconnecting\n", a, b);
            pc_net_game_shutdown();
            return;
        }
    }

    if (s_client_paused) {
        s_client_paused = 0;
        printf("[NET][WORLD] client: local save loaded again -- world apply resumed\n");
    }

    if (s_client_need_resync) {
        PCNetGameResyncRequestMsg rr;
        memset(&rr, 0, sizeof(rr));
        rr.msg_type = (uint8_t)PC_NETGAME_MSG_RESYNC_REQUEST;
        rr.grid = (uint8_t)PC_NETGAME_GRID_TOWN;
        rr.reason = (uint8_t)PC_NETGAME_RESYNC_REASON_SAVE_RELOADED;
        if (pc_net_send(0, PC_NET_RELIABLE, &rr, (uint16_t)sizeof(rr))) {
            s_client_need_resync = 0;
            printf("[NET][WORLD] client: requested a world resync from the host\n");
        }
    }

    pcnetgame_client_retry_deferred();

    {
        PCNetPlayerContext ctx;
        pcnetgame_capture_local_context(&ctx);
        if (!s_client_last_ctx_valid || ctx.player_no != s_client_last_ctx.player_no ||
            ctx.destiny_type != s_client_last_ctx.destiny_type || ctx.flags != s_client_last_ctx.flags ||
            ctx.money_power != s_client_last_ctx.money_power || ctx.goods_power != s_client_last_ctx.goods_power) {
            PCNetGamePlayerContextMsg m;
            memset(&m, 0, sizeof(m));
            m.msg_type = (uint8_t)PC_NETGAME_MSG_PLAYER_CONTEXT;
            m.player_no = ctx.player_no;
            m.destiny_type = ctx.destiny_type;
            m.flags = ctx.flags;
            m.money_power = ctx.money_power;
            m.goods_power = ctx.goods_power;
            if (pc_net_send(0, PC_NET_RELIABLE, &m, (uint16_t)sizeof(m))) {
                s_client_last_ctx = ctx;
                s_client_last_ctx_valid = 1;
                if (g_pc_verbose) {
                    printf("[NET] client: context sent (player_no=%u destiny=%u flags=0x%02X money=%d goods=%d)\n",
                           (unsigned)ctx.player_no, (unsigned)ctx.destiny_type, (unsigned)ctx.flags,
                           (int)ctx.money_power, (int)ctx.goods_power);
                }
            }
        }
    }
}

int pc_net_game_start_host(uint16_t port) {
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
    pcnetgame_reset_host_world_state();
    printf("[NET] hosting on UDP port %u (protocol %u)\n", (unsigned)port, (unsigned)PC_NETGAME_PROTOCOL_VERSION);
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
    /* Stage 5A/5B/v2: a fresh connection never carries over a previous one's state (request ids,
     * pending requests, world seqs, snapshot progress...) -- see pcnetgame_reset_client_session_state(). */
    pcnetgame_reset_client_session_state();

    printf("[NET] connecting to %s:%u (protocol %u)...\n", host_ip, (unsigned)port, (unsigned)PC_NETGAME_PROTOCOL_VERSION);
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
    pcnetgame_reset_client_session_state();
    pcnetgame_reset_host_world_state(); /* also clears s_host_peer_link[] and every per-peer cache */

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
/* Villager population/is_home milestone, TEST-ONLY: fires mNpc_DebugForceGrow()/
 * mNpc_DebugForceRemove() (m_npc.c) exactly once each, as soon as the host world is ready -- see
 * pc_platform.h's doc comment on g_pc_force_villager_grow/g_pc_force_villager_remove. Mirrors
 * pcnetgame_run_pickup_test_seed()'s own gating (host role, gamePT loaded, world ready). */
static void pcnetgame_run_villager_test_triggers(void) {
    static int s_grow_done = 0;
    static int s_remove_done = 0;

    if (s_role != PC_NETGAME_ROLE_HOST || gamePT == NULL || !s_host_world_ready) {
        return;
    }
    /* Verification-pass addition: wait for at least one READY client whose initial snapshot has
     * already COMPLETED (not merely connected) before firing, so a real connected client observes
     * the LIVE PC_NETGAME_MSG_VILLAGER_ARRIVAL/_DEPARTURE broadcast itself (proving the client's own
     * receive-and-apply path for the delta messages, not just its initial join snapshot picking up
     * the already-changed state). A no-op (never fires) if run with zero clients, e.g. the earlier
     * host-only smoke checks, or while every connected peer is still mid-snapshot. */
    {
        int i, have_settled_peer = 0;
        for (i = 0; i < PC_NET_MAX_PEERS; i++) {
            if (s_host_peer_link[i] == PC_NETGAME_LINK_READY && !s_host_peer[i].snap_active) {
                have_settled_peer = 1;
                break;
            }
        }
        if (!have_settled_peer) {
            return;
        }
    }
    if (g_pc_force_villager_grow && !s_grow_done) {
        s_grow_done = 1;
        printf("[NET][NPC] --force-villager-grow active: forcing a villager to grow in\n");
        mNpc_DebugForceGrow();
    }
    if (g_pc_force_villager_remove && !s_remove_done) {
        s_remove_done = 1;
        printf("[NET][NPC] --force-villager-remove active: forcing a villager to be removed\n");
        mNpc_DebugForceRemove();
    }
}

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

    /* v2: only into the real, loaded town (not the title-demo field, whose Save is reloaded from
     * disk afterwards). The writes are hooked, so the flush broadcasts the fixtures to clients. */
    if (!g_pc_pickup_test_seed || s_role != PC_NETGAME_ROLE_HOST || gamePT == NULL || !s_host_world_ready ||
        !pcfa_scene_is_town()) {
        return;
    }
    if (!s_logged) {
        s_logged = 1;
        printf("[NET][PICKUP] --pickup-test-seed active: seeding up to 30 fixture tiles\n");
    }
    for (i = 0; i < 30; i++) {
        uint16_t prev = 0;
        int have_prev = 0;
        if (s_seed_done[i]) {
            continue;
        }
        {
            /* v2 diagnostic: what the fixture is about to overwrite (the seed does not check for
             * occupancy -- a structure footprint such as RSV_NO gets overwritten too). */
            int acre, tile;
            if (pcfa_town_ut_to_acre_tile(s_seed_tiles[i][0], s_seed_tiles[i][1], &acre, &tile)) {
                have_prev = pcfa_get_tile(acre, tile, &prev);
            }
        }
        if (mFI_UtNumtoFGSet_common((mActor_name_t)ITM_FOOD_APPLE, s_seed_tiles[i][0], s_seed_tiles[i][1], TRUE)) {
            s_seed_done[i] = 1;
            printf("[NET][PICKUP] --pickup-test-seed: fixture item placed at tile (%d,%d)", s_seed_tiles[i][0],
                   s_seed_tiles[i][1]);
            if (have_prev && prev != (uint16_t)EMPTY_NO) {
                printf(" (OVERWROTE non-empty 0x%04X)", (unsigned)prev);
            }
            printf("\n");
        }
    }
}

void pc_net_game_poll(void) {
    PCNetEvent ev;

    if (s_role == PC_NETGAME_ROLE_NONE) return;

    /* v2: refresh the "gameplay save loaded" latch once, before anything consults it. */
    {
        int local_ready = pcnetgame_update_local_world_ready();
        if (s_role == PC_NETGAME_ROLE_HOST) {
            pcnetgame_host_world_tick(local_ready);
            /* Two-phase interactions: release timed-out reservations BEFORE this poll's events are
               handled, so a late CONFIRM / a competing request is judged against the up-to-date table. */
            pcnetgame_host_expire_reservations();
        }
    }

    pc_net_poll(); /* never blocks */

    while (s_role != PC_NETGAME_ROLE_NONE && pc_net_next_event(&ev)) {
        if (s_role == PC_NETGAME_ROLE_HOST) {
            switch (ev.type) {
                case PC_NET_EVENT_PEER_CONNECTED:
                    if (ev.peer >= 0 && ev.peer < PC_NET_MAX_PEERS) {
                        /* v2: a freshly allocated transport slot starts from a clean slate even if
                         * the previous occupant's disconnect was never observed here. */
                        pcnetgame_reset_all_host_peer_state(ev.peer);
                        s_host_peer_link[ev.peer] = PC_NETGAME_LINK_HANDSHAKE;
                    }
                    printf("[NET] host: peer %d transport-connected, awaiting identity\n", (int)ev.peer);
                    break;
                case PC_NET_EVENT_PEER_DISCONNECTED:
                    if (ev.peer >= 0 && ev.peer < PC_NET_MAX_PEERS) {
                        s_host_peer_link[ev.peer] = PC_NETGAME_LINK_DISCONNECTED;
                    }
                    printf("[NET] host: peer %d disconnected\n", (int)ev.peer);
                    pcnetgame_reset_all_host_peer_state(ev.peer); /* Stage 5A/5B-1/v2: never let a
                                                                     * reused peer slot inherit this
                                                                     * connection's dedup caches,
                                                                     * deferred identity, context or
                                                                     * snapshot progress -- see
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
                    /* v2: a fresh transport connection -- clear every per-connection state, then
                     * wait in HANDSHAKE; IDENTITY is sent by pcnetgame_client_tick() as soon as the
                     * local save is loaded (possibly later this same poll). */
                    pcnetgame_reset_client_session_state();
                    s_client_link = PC_NETGAME_LINK_HANDSHAKE;
                    printf("[NET] client: transport-connected to host\n");
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
                    printf("[NET] client: host connection lost\n");
                    /* Stage 5A/5B-1/v2: pending pickup/drop requests are dropped immediately (the
                     * item was never moved locally, so nothing is lost or needs restoring), and
                     * every other per-connection state goes with them. */
                    pcnetgame_reset_client_session_state();
                    pc_remote_player_on_disconnect(PC_NETGAME_HOST_PLAYER_ID);
                    break;
                case PC_NET_EVENT_DATA:
                    pcnetgame_handle_client_data(ev.data, ev.size);
                    break;
            }
        }
    }
    if (s_role == PC_NETGAME_ROLE_NONE) {
        return; /* a REJECT / failed ACK validation shut networking down mid-drain */
    }

    /* v2 world protocol, after events. */
    if (s_role == PC_NETGAME_ROLE_HOST) {
        pcnetgame_host_closing_tick();
        pcnetgame_host_process_pending_identities();
        pcnetgame_host_world_poll();
    } else {
        pcnetgame_client_tick();
        if (s_role == PC_NETGAME_ROLE_NONE) {
            return; /* local town changed under a READY link -> shut down */
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
            /* Best effort: tell the host to release any reservation it may hold for this request right
               away instead of waiting for its expiry (a late RESULT is then also answered ABORT(STALE)). */
            pcnetgame_client_send_confirm((uint8_t)PC_NETGAME_INTERACT_KIND_PICKUP, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                          (uint8_t)PC_NETGAME_CONFIRM_REASON_CANCELLED, s_pickup_pending.request_id);
            s_pickup_pending.valid = 0;
        } else {
            PCNetGamePickupRequestMsg msg;
            memset(&msg, 0, sizeof(msg));
            msg.msg_type = (uint8_t)PC_NETGAME_MSG_PICKUP_REQUEST;
            msg.ut_x = s_pickup_pending.ut_x;
            msg.ut_z = s_pickup_pending.ut_z;
            msg.request_id = s_pickup_pending.request_id; /* SAME id -- a retry of the same
                                                              logical request, not a new one, so
                                                              the host's dedup cache recognizes it */
            if (pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
                if (s_pickup_pending.unsent) {
                    s_pickup_pending.unsent = 0; /* this was the FIRST send, not a retry: not counted */
                } else {
                    s_pickup_pending.retry_count++;
                }
            } else {
                /* window full: not a retry -- fire again on the very next poll */
                s_pickup_pending.timeout_accum = PC_NETGAME_PICKUP_TIMEOUT_60FPS_FRAMES;
            }
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
            pcnetgame_client_send_confirm((uint8_t)PC_NETGAME_INTERACT_KIND_DROP, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                          (uint8_t)PC_NETGAME_CONFIRM_REASON_CANCELLED, s_drop_pending.request_id);
            s_drop_pending.valid = 0;
        } else {
            PCNetGameDropRequestMsg msg;
            memset(&msg, 0, sizeof(msg));
            msg.msg_type = (uint8_t)PC_NETGAME_MSG_DROP_REQUEST;
            msg.pocket_slot_idx = s_drop_pending.pocket_slot_idx;
            msg.ut_x = s_drop_pending.ut_x;
            msg.ut_z = s_drop_pending.ut_z;
            msg.claimed_item = s_drop_pending.claimed_item;
            msg.request_id = s_drop_pending.request_id; /* SAME id -- see the pickup retry block's
                                                            own comment */
            if (pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
                if (s_drop_pending.unsent) {
                    s_drop_pending.unsent = 0; /* this was the FIRST send, not a retry: not counted */
                } else {
                    s_drop_pending.retry_count++;
                }
            } else {
                s_drop_pending.timeout_accum = PC_NETGAME_DROP_TIMEOUT_60FPS_FRAMES; /* not a retry */
            }
        }
    }

    /* (v2: the Stage 5A deferred FIELD_UPDATE retry loop that lived here is gone -- see the note
     * where s_pending_field_updates used to be declared.) */

    /* Stage 5A.1: see pcnetgame_run_pickup_test_seed()'s own doc -- a complete no-op unless
     * --pickup-test-seed was passed on the command line. */
    pcnetgame_run_pickup_test_seed();

    /* Villager population/is_home milestone: see pcnetgame_run_villager_test_triggers()'s own doc --
     * a complete no-op unless --force-villager-grow/--force-villager-remove was passed. */
    pcnetgame_run_villager_test_triggers();
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

int pc_net_game_world_is_host_authoritative(void) {
    return s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY;
}

int pc_net_game_get_local_player_context(PCNetPlayerContext* out) {
    if (out == NULL || !pcfa_save_ready()) {
        return 0;
    }
    pcnetgame_capture_local_context(out);
    return 1;
}

int pc_net_game_get_player_context(PCNetPlayerId player_id, PCNetPlayerContext* out) {
    if (out == NULL || s_role != PC_NETGAME_ROLE_HOST) {
        return 0;
    }
    if (player_id == PC_NETGAME_HOST_PLAYER_ID) {
        return pc_net_game_get_local_player_context(out);
    }
    if (player_id < 0 || player_id >= PC_NET_MAX_PEERS || s_host_peer_link[player_id] != PC_NETGAME_LINK_READY ||
        !s_host_peer[player_id].ctx_valid) {
        return 0;
    }
    *out = s_host_peer[player_id].ctx;
    return 1;
}

int pc_net_game_get_local_town_identity(PCNetGameTownIdentity* out) {
    if (out == NULL || !pcfa_save_ready()) {
        return 0;
    }
    pcnetgame_capture_town_identity(out);
    return 1;
}

int pc_net_game_client_world_synced(void) {
    return s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY && s_client_world_synced;
}

int pc_net_game_request_pickup(int ut_x, int ut_z) {
    PCNetGamePickupRequestMsg msg;
    PCNetGameOwnerStamp stamp;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0; /* single-player or host -- caller should proceed with the normal local
                     mutation, unmodified */
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1; /* malformed input from the caller -- still "handled" (this process has no
                     authority to mutate locally as a client), just nothing is sent */
    }
    if (!pcfa_scene_is_town()) {
        /* v2: indoors (house room etc.) ut_x/ut_z are room coordinates, not town ones, and room
           floors are not shared state this phase -- never send them to the host as if they were.
           Still "handled" (the caller's client branch never mutates locally regardless). */
        if (g_pc_verbose) {
            printf("[NET][PICKUP] not in the town scene -- pickup at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    if (s_pickup_pending.valid) {
        /* One already in flight. The decomp state machine itself can't normally trigger a second
         * Setup_main_Pickup before the first one's animation state exits (see
         * m_player_main_pickup.c_inc), so this is a defensive no-op, not an expected path -- still
         * return 1 so the caller never falls through to a local mutation regardless. */
        return 1;
    }

    if (!pcnetgame_capture_owner_stamp(&stamp)) {
        printf("[NET][PICKUP] no gameplay save loaded -- pickup at (%d,%d) not sent\n", ut_x, ut_z);
        return 1; /* (a client with a READY link always has one; defensive) */
    }
    if (mPr_GetPossessionItemIdx(Now_Private, (mActor_name_t)EMPTY_NO) < 0) {
        /* No free pocket: vanilla leaves the item where it is when the pockets are full, so send
           nothing and change nothing (the host never reserves/removes an item nobody can receive). One
           line per attempt (the pickup animation starts once per attempt). */
        printf("[NET][PICKUP] pockets full -- pickup at (%d,%d) not sent (the item stays in the world)\n", ut_x, ut_z);
        return 1;
    }

    s_pickup_pending.valid = 1;
    s_pickup_pending.request_id = s_next_pickup_request_id++;
    s_pickup_pending.owner = stamp;
    s_pickup_pending.ut_x = (uint8_t)ut_x;
    s_pickup_pending.ut_z = (uint8_t)ut_z;
    s_pickup_pending.timeout_accum = 0.0f;
    s_pickup_pending.retry_count = 0;
    s_pickup_pending.unsent = 0;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_PICKUP_REQUEST;
    msg.ut_x = s_pickup_pending.ut_x;
    msg.ut_z = s_pickup_pending.ut_z;
    msg.request_id = s_pickup_pending.request_id;
    if (!pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
        /* Window full: the request is still pending; the next poll's retry block sends it (uncounted,
           and every following poll until it is queued) instead of waiting a whole ~500 ms interval. */
        s_pickup_pending.unsent = 1;
        s_pickup_pending.timeout_accum = PC_NETGAME_PICKUP_TIMEOUT_60FPS_FRAMES;
    }
    return 1;
}

/* Host-local guard API (see pc_net_game.h): 1 iff town tile (ut_x, ut_z) is currently reserved by any
 * peer's PENDING network pickup/drop. */
int pc_net_game_field_tile_reserved(int ut_x, int ut_z) {
    int acre, tile;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return 0;
    }
    if (!pcfa_scene_is_town()) {
        return 0; /* indoors (ut_x, ut_z) are room coordinates, not town ones */
    }
    if (!pcfa_town_ut_to_acre_tile(ut_x, ut_z, &acre, &tile)) {
        return 0; /* out of range / not a persistent town tile */
    }
    return pcnetgame_host_tile_reserved_by(acre, tile) >= 0;
}

/* Stage 5A.1: called from the decomp pickup state (see m_player_main_pickup.c_inc) right after
 * the HOST's OWN local pickup has already mutated the field tile through the existing, completely
 * unmodified single-player code (Player_actor_putin_item()/the PC_ENHANCEMENTS money-bag branch,
 * both in m_player_common.c_inc/m_player_main_pickup.c_inc) -- never before, and never as a
 * substitute for it. This function does not touch the field itself -- it only announces a mutation
 * that has already happened. v2: through the single commit path (pcnetgame_host_flush_mask()) the
 * client-request path and the dirty flush also use, so all of them agree on the wire format and
 * none of them can double-send.
 *
 * A no-op for single-player (s_role != HOST) and, defensively, for a client (a client is never
 * itself authoritative for the field -- see pc_net_game_request_pickup() -- so it must never
 * broadcast this regardless of how it might be called). No self-send: the host is not its own
 * peer. */
void pc_net_game_notify_local_field_pickup(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return; /* malformed input from the caller -- defensive; real decomp callers always pass a
                   value freshly computed by mFI_Wpos2UtNum() */
    }
    /* v2: now just an early, explicit trigger of the single commit path for this tile's acre
       (redundant-but-harmless: C's write hook marks the acre dirty and the per-poll flush would
       commit the same change a moment later; whichever runs first updates the shadow, the other
       finds nothing to send). Indoors, (ut_x, ut_z) is a room coordinate -- not shared state -- so
       nothing is committed; the persistent read inside the flush would find no change anyway. */
    if (!pcfa_scene_is_town()) {
        return;
    }
    pcnetgame_host_commit_town_ut(ut_x, ut_z);
}

/* (Stage 5B-3's s_host_local_drop_landing is declared with the other module state, above.) */

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
    /* v2: redundant-but-harmless early trigger of the single commit path (see
       pc_net_game_notify_local_field_pickup()). `item` is no longer put on the wire directly: the
       commit reads the persistent value that actually landed. Other landings (which this watch
       never tracked) are now broadcast too, by the dirty flush / safety scan. */
    (void)item;
    if (!pcfa_scene_is_town()) {
        return;
    }
    pcnetgame_host_commit_town_ut(ut_x, ut_z);
}

/* Stage 5B-1: see the doc comment in pc_net_game.h. Exact structural mirror of
 * pc_net_game_request_pickup() -- see that function's own doc for the shared reasoning
 * (client-only, one-in-flight, defensive bounds checks). */
int pc_net_game_request_drop(int pocket_slot_idx, int claimed_item, int ut_x, int ut_z) {
    PCNetGameDropRequestMsg msg;
    PCNetGameOwnerStamp stamp;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0; /* single-player or host -- caller should proceed with the normal local
                     mutation, unmodified */
    }
    if (pocket_slot_idx < 0 || pocket_slot_idx >= mPr_POCKETS_SLOT_COUNT || ut_x < 0 || ut_x > 255 ||
        ut_z < 0 || ut_z > 255) {
        return 1; /* malformed input from the caller -- still "handled" (this process has no
                     authority to mutate locally as a client), just nothing is sent */
    }
    if (!pcfa_scene_is_town()) {
        /* v2: not in the town scene -- (ut_x, ut_z) is a room coordinate and room floors are not
           shared state this phase. Returns 0 so the m_tag_ovl.c client seam shows vanilla's own
           "can't place that here" warning instead of closing the menu on a request that could
           never succeed (the seam only calls this for a network client, so 0 never falls through
           to a local mutation there). */
        return 0;
    }
    if (s_drop_pending.valid) {
        /* One already in flight -- defensive no-op, same posture as pickup's own equivalent check
           (see pc_net_game_request_pickup()'s doc). Unlike pickup, a menu-driven drop COULD in
           principle be re-triggered by the player before the first result arrives (the menu closes
           optimistically on send -- see the client seam in m_tag_ovl.c), so this is a real, not
           purely defensive, guard for Stage 5B-1. */
        return 1;
    }

    if (!pcnetgame_capture_owner_stamp(&stamp)) {
        printf("[NET][DROP] no gameplay save loaded -- drop at (%d,%d) not sent\n", ut_x, ut_z);
        return 0; /* the m_tag_ovl.c client seam shows vanilla's "can't place that" warning */
    }
    if (Now_Private->inventory.pockets[pocket_slot_idx] != (mActor_name_t)claimed_item) {
        /* The slot does not hold what the caller claims (the menu state is stale): never ask the host
           to place an item this client does not have. */
        printf("[NET][DROP] pocket slot %d holds 0x%04X, not the claimed 0x%04X -- drop not sent\n", pocket_slot_idx,
               (unsigned)Now_Private->inventory.pockets[pocket_slot_idx], (unsigned)claimed_item);
        return 0;
    }

    s_drop_pending.valid = 1;
    s_drop_pending.request_id = s_next_drop_request_id++;
    s_drop_pending.owner = stamp;
    s_drop_pending.pocket_slot_idx = (uint8_t)pocket_slot_idx;
    s_drop_pending.ut_x = (uint8_t)ut_x;
    s_drop_pending.ut_z = (uint8_t)ut_z;
    s_drop_pending.claimed_item = (uint16_t)claimed_item;
    s_drop_pending.timeout_accum = 0.0f;
    s_drop_pending.retry_count = 0;
    s_drop_pending.unsent = 0;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_DROP_REQUEST;
    msg.pocket_slot_idx = s_drop_pending.pocket_slot_idx;
    msg.ut_x = s_drop_pending.ut_x;
    msg.ut_z = s_drop_pending.ut_z;
    msg.claimed_item = s_drop_pending.claimed_item;
    msg.request_id = s_drop_pending.request_id;
    if (!pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
        /* Window full -- see pc_net_game_request_pickup(): retried (uncounted) on the next poll. */
        s_drop_pending.unsent = 1;
        s_drop_pending.timeout_accum = PC_NETGAME_DROP_TIMEOUT_60FPS_FRAMES;
    }
    return 1;
}

/* Villager population/is_home milestone: broadcasts `msg` (already fully built, including
 * world_seq) to every READY client, exactly mirroring pcnetgame_host_check_world_meta()'s own
 * broadcast loop -- peers mid-snapshot are skipped (their snapshot, built at send time from current
 * Save_t.animals[], already carries this result; see pcnetgame_build_villager_snapshot()) and a send
 * failure (reliable window full) falls back to a full resync for that one peer rather than leaving it
 * silently behind. */
static void pcnetgame_broadcast_villager_msg(const void* msg, size_t msg_size) {
    int i;
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        int backlog;
        if (s_host_peer_link[i] != PC_NETGAME_LINK_READY || s_host_peer[i].snap_active) {
            continue;
        }
        backlog = pc_net_reliable_backlog((PCNetPeerId)i);
        if (backlog < 0) {
            continue;
        }
        if (backlog + 1 > PC_NET_RELIABLE_WINDOW - PC_NETGAME_WINDOW_HEADROOM ||
            !pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, msg, (uint16_t)msg_size)) {
            pcnetgame_host_start_snapshot((PCNetPeerId)i, "villager population send failed");
        }
    }
}

/* See pc_net_game.h. Called from mNpc_InitNpcData() (m_npc.c), right after mNpc_SetNpcHome() has
 * finished assigning `slot`'s home_info -- Save_t.animals[slot] is fully populated by the time this
 * runs. A no-op for single-player and for a client (mirrors every other pc_net_game_notify_local_*()
 * function's own pattern) -- the vanilla call site in m_npc.c is unconditional. */
void pc_net_game_notify_villager_arrival(int slot) {
    Animal_c* animal;
    PCNetGameVillagerArrivalMsg msg;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (slot < 0 || slot >= ANIMAL_NUM_MAX) {
        return; /* malformed input from the caller -- defensive */
    }
    animal = Save_GetPointer(animals[slot]);
    if (ITEM_NAME_GET_TYPE(animal->id.npc_id) != NAME_TYPE_NPC) {
        return; /* defensive: mNpc_Grow() always fills the slot before this fires */
    }

    ++s_world_seq;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_VILLAGER_ARRIVAL;
    msg.slot = (uint8_t)slot;
    msg.reserved_block_x = animal->home_info.block_x;
    msg.reserved_block_z = animal->home_info.block_z;
    msg.reserved_ut_x = animal->home_info.ut_x;
    msg.reserved_ut_z = (uint8_t)(animal->home_info.ut_z - 1); /* undo mNpc_SetNpcHome()'s ut_z+1 */
    msg.now_npc_max = (uint8_t)Save_Get(now_npc_max);
    msg.npc_id = (uint16_t)animal->id.npc_id;
    msg.world_seq = s_world_seq;

    printf("[NET][NPC] host: villager arrived slot %d npc_id 0x%04X home(%u,%u,%u,%u) (world_seq %u, now_npc_max %u)\n",
           slot, (unsigned)msg.npc_id, (unsigned)msg.reserved_block_x, (unsigned)msg.reserved_block_z,
           (unsigned)msg.reserved_ut_x, (unsigned)msg.reserved_ut_z, (unsigned)s_world_seq,
           (unsigned)msg.now_npc_max);
    pcnetgame_broadcast_villager_msg(&msg, sizeof(msg));
}

/* See pc_net_game.h. Called from mNpc_ForceRemove() (m_npc.c) right after BOTH Save_t mutations
 * (Animal_c slot reset + the 3x3 house-footprint teardown via mNpc_DestroyHouse()) are complete --
 * `slot` is already empty by the time this runs, so nothing is read from Save_t.animals[slot] here;
 * only the slot index and the already-updated now_npc_max are needed. A no-op for single-player and
 * for a client. */
void pc_net_game_notify_villager_departure(int slot) {
    PCNetGameVillagerDepartureMsg msg;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (slot < 0 || slot >= ANIMAL_NUM_MAX) {
        return;
    }

    ++s_world_seq;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_VILLAGER_DEPARTURE;
    msg.slot = (uint8_t)slot;
    msg.now_npc_max = (uint8_t)Save_Get(now_npc_max);
    msg.world_seq = s_world_seq;

    printf("[NET][NPC] host: villager departed slot %d (world_seq %u, now_npc_max %u)\n", slot,
           (unsigned)s_world_seq, (unsigned)msg.now_npc_max);
    pcnetgame_broadcast_villager_msg(&msg, sizeof(msg));
}

/* Villager population/is_home milestone: builds the one-shot full population snapshot sent during
 * snap_stage 2 (see pcnetgame_host_pump_snapshots()), directly from the host's current
 * Save_t.animals[] -- never from any cached/shadow copy (there is no shadow for population, unlike
 * the field: ANIMAL_NUM_MAX (15) slots is cheap enough to always send in full). is_home is read
 * as-is at build time (best-effort; see the is_home design note above PCNetGameVillagerSlotWire). */
static void pcnetgame_build_villager_snapshot(PCNetGameVillagerSnapshotMsg* vs, uint32_t epoch) {
    int i;

    memset(vs, 0, sizeof(*vs));
    vs->msg_type = (uint8_t)PC_NETGAME_MSG_VILLAGER_SNAPSHOT;
    vs->grid = (uint8_t)PC_NETGAME_GRID_TOWN;
    vs->now_npc_max = (uint8_t)Save_Get(now_npc_max);
    vs->epoch = epoch;
    vs->world_seq = s_world_seq;

    for (i = 0; i < ANIMAL_NUM_MAX; i++) {
        Animal_c* animal = Save_GetPointer(animals[i]);
        PCNetGameVillagerSlotWire* slot = &vs->slots[i];

        if (ITEM_NAME_GET_TYPE(animal->id.npc_id) != NAME_TYPE_NPC) {
            continue; /* occupied already 0 from the memset above */
        }
        slot->occupied = 1;
        slot->npc_id = (uint16_t)animal->id.npc_id;
        slot->is_home = animal->is_home ? 1 : 0;
        slot->home_block_x = animal->home_info.block_x;
        slot->home_block_z = animal->home_info.block_z;
        slot->home_ut_x = animal->home_info.ut_x;
        slot->home_ut_z = animal->home_info.ut_z;
    }
}
