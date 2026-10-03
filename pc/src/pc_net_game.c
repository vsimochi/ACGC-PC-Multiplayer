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
#include "pc_wildlife_authority.h" /* World Ecology Wildlife Sync T0: host-authoritative wildlife table */
#include "ac_gyoei.h"       /* World Ecology Wildlife Sync T-catch: aGYO_TYPE_NUM/aGYO_TYPE_SALMON2 --
                                see pcnetgame_validate_catch()/PCNetGameCatchRequestMsg's own doc */
#include "ac_insect_h.h"    /* World Ecology Wildlife Sync T4: aINS_INSECT_TYPE_ANT/aINS_INSECT_TYPE_SPIRIT
                                -- see pcnetgame_validate_and_commit_catch()/pcnetgame_handle_client_
                                catch_result()'s own doc */
#include "m_submenu.h"      /* World Ecology Wildlife Sync T-catch: mSM_COLLECT_FISH_SET()/
                                mSM_COLLECT_INSECT_SET() -- see pcnetgame_handle_client_catch_result()'s
                                own doc */

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
#include "m_field_assessment.h" /* World Ecology Stage 1, Item 4: mFAs_SetFieldRank() -- see
                                  * pcnetgame_handle_client_snapshot_end()/pcnetgame_handle_client_world_meta() */
#include "m_npc_schedule.h" /* N3 Channel B snapshot extension: mNPS_get_schedule_area(), mNPS_schedule_c
                            * (forced_type/forced_timer) -- see pcnetgame_build_villager_snapshot() */
#include "m_shop.h"         /* World Ecology T1: mSP_SelectRandomItem_New(), mSP_LISTTYPE_xxx, and
                            * mSP_KIND_FURNITURE -- see pcnetgame_resolve_tree_drop_item(), the exact
                            * furniture-roll vanilla's own drop_fruit() (bg_item_common.c_inc) uses */
#include "libc64/qrand.h"   /* World Ecology T1: fqrand() -- see pcnetgame_resolve_tree_drop_item() */
#include "m_snowman.h"      /* World Ecology: snowmen -- mSN_snowman_data_c/mSN_SAVE_COUNT/mSN_snowman_save_c
                            * and SNOWMAN0/SNOWMAN8 (m_name_table.h, already reachable). */
#include "ac_psnowman.h"    /* World Ecology: snowmen -- PSNOWMAN_ACTOR's own npc_id/home fields are just
                            * ACTOR's, but this is included for parity with the actor this milestone's
                            * host-side live-actor overlay walks (see
                            * pcnetgame_host_resolve_snowman_tile_overlay()). */
#include "m_police_box.h"   /* World Ecology: snowmen -- mPB_keep_item(), mirroring m_police_box.c:76-94's
                            * own ITEM1/FTR-only lost-and-found classification for a displaced tile item */

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
    /* ---- friendship/mail sync milestone ---- */
    PC_NETGAME_MSG_FRIENDSHIP_REQUEST = 21, /* client -> host only, reliable: a local dialogue/mail
                                             * interaction changed Anmmem_c.friendship by `delta` for
                                             * THIS connection's own identity (never sent by/applied
                                             * to any other player). See PCNetGameFriendshipRequestMsg
                                             * and mNpc_AddFriendship()'s doc comment (m_npc.c). */
    PC_NETGAME_MSG_FRIENDSHIP_UPDATE  = 22, /* host -> every READY client, reliable, world_seq-stamped:
                                             * the resulting (already-clamped) friendship value for one
                                             * (villager slot, PersonalID) pair. See
                                             * PCNetGameFriendshipUpdateMsg. */
    PC_NETGAME_MSG_FRIENDSHIP_SNAPSHOT_ENTRY = 23, /* host -> one client, reliable: one occupied
                                             * Anmmem_c memory entry (late-join/reconnect coverage),
                                             * sent once per occupied entry between VILLAGER_SNAPSHOT
                                             * and SNAPSHOT_END. Also carries mail-sync's letter/
                                             * letter_info (see PCNetGameFriendshipSnapshotEntryMsg). */
    PC_NETGAME_MSG_MAIL_REQUEST       = 24, /* client -> host only, reliable: this connection's own
                                             * local player sent Mail_c `mail` to an NPC via the post
                                             * office. See PCNetGameMailRequestMsg and
                                             * mNpc_SendMailtoNpc()'s doc comment (m_npc.c). */
    PC_NETGAME_MSG_MAIL_DELIVERED     = 25, /* host -> every READY client, reliable, world_seq-stamped:
                                             * the resulting villager-memory outcome of a delivered
                                             * letter (letter content + letter_info + friendship). See
                                             * PCNetGameMailDeliveredMsg. */
    PC_NETGAME_MSG_NPC_MOVE           = 26, /* N2: host -> every READY client, UNRELIABLE, ~20 Hz:
                                             * one on-screen villager NPC_ACTOR's resolved
                                             * position/facing for this frame. See
                                             * PCNetGameNpcMoveMsg and pc_net_game.h's design note
                                             * above pc_net_game_notify_npc_move(). */
    PC_NETGAME_MSG_CLOCK_SYNC         = 27, /* N-clock milestone: host -> every READY client,
                                             * RELIABLE, low-rate: the host's authoritative wall/game
                                             * clock, so a client's Common(time.rtc_time)/now_sec
                                             * (lbRTC_GetGameTime(), see lb_rtc.c) can be corrected to
                                             * agree with the host's rather than free-running from
                                             * this process's own OSInit() seed. See
                                             * PCNetGameClockSyncMsg and
                                             * pcnetgame_host_check_clock_sync()/
                                             * pcnetgame_handle_client_clock_sync() below. Deliberately
                                             * its own message, NOT an extension of WORLD_META: renew_time
                                             * there is Save_t.all_grow_renew_time, an unrelated
                                             * daily-growth/weather/Kabu RNG day-epoch anchor -- folding
                                             * a clock correction into that field would corrupt it, and
                                             * WORLD_META's reliable+world_seq-churning shape is a poor
                                             * fit for a periodic clock ping anyway. */
    PC_NETGAME_MSG_NPC_STATE          = 28, /* N3 Channel B: host -> every READY client, RELIABLE,
                                             * sent on CHANGE (not every frame): a villager's
                                             * is_home/hide/forced_type/forced_timer, applied
                                             * atomically client-side. See PCNetGameNpcStateMsg and
                                             * pc_net_game.h's design note above
                                             * pc_net_game_notify_npc_state(). */
    PC_NETGAME_MSG_FIELD_ACTION_REQUEST = 29, /* World Ecology Stage 1: client -> host only, reliable.
                                             * One shot (no separate CONFIRM phase -- see
                                             * PCNetGameFieldActionRequestMsg's own doc for why this
                                             * differs from PICKUP/DROP's two-phase shape). See
                                             * pc_net_game_request_dig_buried()/
                                             * pc_net_game_request_money_rock_hit(). */
    PC_NETGAME_MSG_FIELD_ACTION_RESULT  = 30, /* World Ecology Stage 1: host -> the one requesting
                                             * client only, reliable. See
                                             * PCNetGameFieldActionResultMsg. */
    PC_NETGAME_MSG_BURY_REQUEST         = 31, /* World Ecology T3 (host-authoritative bury): client ->
                                             * host only, reliable. Mirrors PICKUP/DROP's two-phase
                                             * reserve -> INTERACT_CONFIRM shape
                                             * (PC_NETGAME_INTERACT_KIND_BURY), not
                                             * FIELD_ACTION_REQUEST's one-shot shape. See
                                             * PCNetGameBuryRequestMsg and
                                             * pcnetgame_handle_host_bury_request(). */
    PC_NETGAME_MSG_BURY_RESULT          = 32, /* World Ecology T3: host -> the one requesting client
                                             * only, reliable. See PCNetGameBuryResultMsg. */
    PC_NETGAME_MSG_SNOWMAN_BUILD_REQUEST = 33, /* World Ecology: snowmen. Client -> host only,
                                             * reliable. See PCNetGameSnowmanBuildRequestMsg /
                                             * pc_net_game_request_snowman_build(). */
    PC_NETGAME_MSG_SNOWMAN_BUILD_RESULT  = 34, /* World Ecology: snowmen. Host -> the one requesting
                                             * client only, reliable. See
                                             * PCNetGameSnowmanBuildResultMsg. */
    PC_NETGAME_MSG_SNOWMAN_STATE         = 35, /* World Ecology: snowmen. Host -> every READY client,
                                             * reliable, world_seq-stamped (its OWN sequence,
                                             * s_snowman_world_seq -- independent of s_world_seq). See
                                             * PCNetGameSnowmanStateMsg. */
    PC_NETGAME_MSG_WILDLIFE_SPAWN_TRIGGER_REQUEST = 36, /* World Ecology Wildlife Sync T0: client ->
                                             * host only, reliable. Reports a genuine wade-start event
                                             * into an acre -- carries ONLY that acre, never a
                                             * species/RNG result/position. See
                                             * PCNetGameWildlifeSpawnTriggerRequestMsg and
                                             * pc_net_game_request_wildlife_spawn_trigger(). */
    PC_NETGAME_MSG_WILDLIFE_SPAWN        = 37, /* World Ecology Wildlife Sync T0/T1: host -> every
                                             * READY client, reliable. One freshly-created authoritative
                                             * wildlife record. See PCNetGameWildlifeSpawnMsg and
                                             * pc_net_game_notify_wildlife_spawn(). T1 UPDATE (stale as
                                             * of T0): the client handler
                                             * (pcnetgame_handle_client_wildlife_spawn()) is no longer
                                             * log-only -- it now materializes a REAL local vanilla
                                             * fish/bug actor via pcwld_presentation_create() (see
                                             * pc_wildlife_authority.h/.c), gated on
                                             * g_pc_authoritative_wildlife being enabled on this
                                             * client. */
    PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_BEGIN = 38, /* World Ecology Wildlife Sync T2: host -> one
                                             * client, reliable. Late-join/reconnect coverage for the
                                             * authoritative wildlife table (which WILDLIFE_SPAWN alone
                                             * never gives a client that connects/reconnects AFTER
                                             * wildlife already exists) -- sent once per snapshot pass,
                                             * between the VILLAGER_SNAPSHOT stage and the first
                                             * WILDLIFE_SNAPSHOT_ENTRY (see pcnetgame_host_pump_
                                             * snapshots()'s own doc, snap_stage 3). See
                                             * PCNetGameWildlifeSnapshotBeginMsg. */
    PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_ENTRY = 39, /* World Ecology Wildlife Sync T2: host -> one
                                             * client, reliable. One currently-live authoritative
                                             * wildlife record (SAME minimum field shape as
                                             * WILDLIFE_SPAWN -- entity_id/kind/species/acre/position,
                                             * nothing more), sent once per active table slot, flat-
                                             * iterated over the FULL table (every acre, never filtered
                                             * to the joining client's own acre -- see this milestone's
                                             * own "multi-acre snapshot" requirement). See
                                             * PCNetGameWildlifeSnapshotEntryMsg. */
    PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_END = 40, /* World Ecology Wildlife Sync T2: host -> one client,
                                             * reliable. Closes one WILDLIFE_SNAPSHOT_BEGIN/ENTRY* run --
                                             * this is the signal the client's reconciliation logic
                                             * waits for (the correct "stale" set can only be known once
                                             * the FULL snapshot has arrived; see pcwld_presentation_
                                             * reconcile(), pc_wildlife_authority.h/.c). See
                                             * PCNetGameWildlifeSnapshotEndMsg. */
    PC_NETGAME_MSG_CATCH_REQUEST         = 41, /* World Ecology Wildlife Sync T-catch (ordinary fish
                                             * catching only -- see this milestone's own ABSOLUTE SCOPE
                                             * LIMIT: no bug catching, tournaments, or ant/bee handling).
                                             * Client -> host only, reliable. Claims a catch on an
                                             * authoritative wildlife entity_id -- the item is NEVER
                                             * granted locally before this resolves (see
                                             * pc_net_game_request_catch_fish()'s own doc: Option A, no
                                             * award-then-claw-back). See PCNetGameCatchRequestMsg. */
    PC_NETGAME_MSG_CATCH_RESULT          = 42, /* World Ecology Wildlife Sync T-catch: host -> the one
                                             * requesting client only, reliable. See
                                             * PCNetGameCatchResultMsg. */
    PC_NETGAME_MSG_WILDLIFE_DESPAWN      = 43, /* World Ecology Wildlife Sync T-catch: host -> every
                                             * READY client, reliable, sent immediately after an accepted
                                             * CATCH_REQUEST (or an accepted host-local catch) removes an
                                             * entity from the authoritative table. Each receiver
                                             * (including the host's own local bookkeeping) reconciles its
                                             * OWN local presentation actor, if any, via
                                             * pcwld_handle_wildlife_despawn() -- see that function's own
                                             * 3-case doc (pc_wildlife_authority.h/.c). See
                                             * PCNetGameWildlifeDespawnMsg. */
    PC_NETGAME_MSG_PLAYER_SCENE          = 44, /* M9-A (protocol v5): scene identity / player presence.
                                             * RELIABLE, 12 bytes. client -> host (the client's own scene;
                                             * net_player_id ignored), host -> every OTHER READY client (a
                                             * relay, net_player_id = the TRUE originating peer id) and
                                             * host -> every READY client (the host's own scene,
                                             * net_player_id = PC_NETGAME_HOST_PLAYER_ID). Also replayed
                                             * host -> one newly READY client (host + every other READY
                                             * peer's last known scene), and sent host -> clients with
                                             * PC_NETGAME_SCENE_FLAG_CLEARED when a peer is removed.
                                             * See PCNetGamePlayerSceneMsg. */
    PC_NETGAME_MSG_NPC_TALK              = 45, /* M9-C (protocol v6): client -> host ONLY, RELIABLE, 8 bytes. A
                                             * ready client reports the rising (flags=1, begin) and falling
                                             * (flags=0, end) edge of ITS OWN local villager-talk lease so the
                                             * host can hold that villager still (host-authoritative hold). The
                                             * client never sends NPC positions. See PCNetGameNpcTalkMsg. */
    PC_NETGAME_MSG_PLAYER_ACTION         = 46, /* M9-C (protocol v7): host -> READY clients ONLY, RELIABLE, 10 bytes.
                                             * A cosmetic "this player just did X" presentation hint (kind 1 =
                                             * PICKUP: the item id + tile the host committed). Host-originated and
                                             * relayed to every READY client except the originating peer; a client
                                             * never sends it and the host drops any that arrives. See
                                             * PCNetGamePlayerActionMsg. */
} PCNetGameMsgType;

typedef enum PCNetGameRejectReason {
    PC_NETGAME_REJECT_PROTOCOL_MISMATCH = 1, /* 8-byte PCNetGameRejectMsg (version-stable form) */
    PC_NETGAME_REJECT_SERVER_FULL       = 2, /* 8-byte form; M9 Stage 1A: identity unavailable (own/connected/ambiguous) */
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
    uint16_t action_state;  /* protocol v7 (was `_reserved0`, always 0 before): low byte = sender's vanilla
                             * now_main_index (mPlayer_INDEX_*, 0..mPlayer_INDEX_NUM-1 = 0..120), bits 8..11 = 4-bit
                             * state-ENTRY COUNTER (bumped each time the sender's player ACTUALLY enters a main index,
                             * incl. re-entry of the same index), bits 12..15 reserved = 0. Receivers compare the
                             * (index, counter) pair by equality only. An out-of-range index or non-zero reserved bits
                             * never reject the MOVE: the host zeroes the field before relaying, a receiver ignores
                             * it (see pcnetgame_sanitize_move_action()). */
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
/* World Ecology milestone (Stage 1, Item 4): fish/bug TERM MIRRORING. gyoei_term (Save_t, 0..23 --
 * an unchecked r_month[term>>1][term&1] index, see ac_set_ovl_gyoei.c's aSOG_gyoei_make_*_range_data())
 * and insect_term (Save_t, 0..11 -- an unchecked l_insect_month[month][...] index, see
 * ac_set_ovl_insect.c's aSOI_ins_make_insect_normal_range_data()) are NOT gated (aSOG_gyoei_renew_term_info()/
 * aSOI_ins_renew_term_info() keep running everywhere -- gating them would only cause host/client fish
 * and bug spawn-table DIVERGENCE, with no safety benefit: see the World Ecology audit). Instead the
 * host's 4 term bytes are mirrored here, exactly like weather/market above, and the client applies them
 * verbatim -- EXCEPT it must clamp/reject any received gyoei_term/insect_term outside its valid range
 * BEFORE storing it (pcnetgame_client_apply_term_state(), below), because those two fields are used as
 * unchecked table indices client-side. The two *_transition_offset bytes are cosmetic day-arithmetic
 * inputs only (never an index), but are clamped to their own valid 0..aSO{G,I}_TERM_TRANSITION_MAX_DAYS
 * range too, purely for belt-and-suspenders sanity. Grows this struct by 2 bytes (28 -> 30): the 2
 * bytes of prior padding plus 2 new ones -- see PC_NET_MAX_PAYLOAD's headroom (1024 bytes; this struct
 * is embedded in two ~50-byte messages, nowhere close). */
typedef struct PCNetGameWorldStateWire {
    uint16_t daily_price[7];        /* Kabu_price_c.daily_price[lbRTC_SUNDAY..lbRTC_SATURDAY] */
    uint16_t trade_market;          /* Kabu_price_c.trade_market (Kabu_TRADE_MARKET_TYPE_*) */
    PCNetGameRtcWire kabu_update_time; /* Kabu_price_c.update_time */
    uint8_t  weather;               /* Common_Get(weather), an mEnv_WEATHER_... value */
    uint8_t  weather_intensity;     /* Common_Get(weather_intensity), an mEnv_WEATHER_INTENSITY_... value */
    uint8_t  gyoei_term;                    /* Save_t.gyoei_term, valid range 0..23 -- see doc above */
    uint8_t  gyoei_term_transition_offset;  /* Save_t.gyoei_term_transition_offset, valid range 0..5 */
    uint8_t  insect_term;                   /* Save_t.insect_term, valid range 0..11 -- see doc above */
    uint8_t  insect_term_transition_offset; /* Save_t.insect_term_transition_offset, valid range 0..5 */
} PCNetGameWorldStateWire;
_Static_assert(sizeof(PCNetGameWorldStateWire) == 30, "PCNetGameWorldStateWire wire size drifted");

/* v2: host -> one client, reliable. Closes snapshot `epoch`; the client applies renew_time (the
 * host's Save all_grow_renew_time, read at send time) atomically here, then counts the world as
 * synced. world_seq = host world_seq at send time. Weather + Stalk Market milestone: also carries
 * world_state (WEATHER_VALID/MARKET_VALID in flags) so a fresh or late-joining client gets both
 * domains' current authoritative state in the same message that already closes its snapshot --
 * see the "initial state on join" / "late join" requirements this satisfies. */
#define PC_NETGAME_META_FLAG_RENEW_TIME_VALID 0x01u
#define PC_NETGAME_META_FLAG_WEATHER_VALID    0x02u
#define PC_NETGAME_META_FLAG_MARKET_VALID     0x04u
#define PC_NETGAME_META_FLAG_TERM_VALID       0x08u /* World Ecology Stage 1: gyoei_term/insect_term
                                                       * + their transition offsets, in world_state */
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
_Static_assert(sizeof(PCNetGameSnapshotEndMsg) == 52, "PCNetGameSnapshotEndMsg wire size drifted"); /* 50
    logical bytes padded to 52 for uint32_t alignment (struct alignment 4) -- see PCNetGameWorldStateWire's
    +2-byte growth, World Ecology Stage 1 Item 4 */
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
    uint8_t  hide;                   /* N3 Channel B extension: best-effort, same caveat as is_home
                                       * above -- when the host has no live NPC_ACTOR for this slot (S1
                                       * limitation: out of the host's own local player's spawn scope),
                                       * there is no actor-level hide_flg to read, so this is
                                       * approximated as == is_home (a villager that is home is hidden
                                       * the overwhelming majority of the time; the brief into/out-of-
                                       * house door animation window is the only place this can be
                                       * momentarily wrong, and a subsequent live NPC_STATE broadcast --
                                       * or the villager's own actor being created near this joining
                                       * client -- corrects it immediately, same self-healing property
                                       * NPC_MOVE already relies on). */
    uint8_t  forced_type;            /* mNPS_schedule_c.forced_type at snapshot-build time, or 0 if this
                                       * slot has no registered schedule area yet (see FIX S3) */
    uint16_t forced_timer_remaining; /* mNPS_schedule_c.forced_timer, clamped to uint16_t range, or 0 */
} PCNetGameVillagerSlotWire;
_Static_assert(sizeof(PCNetGameVillagerSlotWire) == 12, "PCNetGameVillagerSlotWire wire size drifted");

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
_Static_assert(sizeof(PCNetGameVillagerSnapshotMsg) == 192,
               "PCNetGameVillagerSnapshotMsg wire size drifted (N3 Channel B extended "
               "PCNetGameVillagerSlotWire by 4 bytes/slot: hide, forced_type, forced_timer_remaining)");
_Static_assert(sizeof(PCNetGameVillagerSnapshotMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameVillagerSnapshotMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");
_Static_assert(ANIMAL_NUM_MAX == 15, "PCNetGameVillagerSnapshotMsg.slots[15] no longer matches ANIMAL_NUM_MAX");

/* N2 villager movement sync: host -> every READY client, UNRELIABLE, ~20 Hz. One on-screen villager
 * NPC_ACTOR's resolved position/facing for the frame it was sampled on. See pc_net_game.h's design
 * note above pc_net_game_notify_npc_move()/pc_net_game_get_npc_move_pose() for full scope/identity
 * rationale -- summarized here: `slot`+`npc_id` reuse the EXACT identity pairing the villager
 * population-sync messages above already use (never a new scheme), and `frame` is this stream's own
 * sender-local monotonic counter, following PCNetMoveMsg.frame's exact convention (only ever
 * comparable against another sample from this same stream, never this process's own frame/time
 * domain -- see PCNetMoveMsg's doc comment). Still excludes speed/move_state (position is applied
 * directly each frame, never integrated from a speed model). N3 Channel A repurposed the byte that
 * was `_reserved0` into `action_type` -- the host's real, unmodified aNPC_action_proc()-selected
 * coarse action/animation type, sampled at the same point as pos/facing -- so walk-vs-idle (and other
 * coarse actions) are now host-authoritative rather than purely inferred client-side from position
 * deltas; see pcnetgame_npc_move_interpolate()/pc_net_game_get_npc_move_pose() for how the two are
 * combined. */
typedef struct PCNetGameNpcMoveMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_NPC_MOVE */
    uint8_t  slot;          /* Save_t.animals[] index, 0..ANIMAL_NUM_MAX-1 */
    uint8_t  action_type;   /* N3 Channel A: repurposes what was _reserved0. Host's
                              * NPC_ACTOR::condition_info.action (aNPC_ACTION_TYPE_*, ac_npc.h) at the
                              * same sample point as pos/facing below -- the coarse action/animation
                              * type the real, unmodified host aNPC_action_proc()/aNPC_setupAction()
                              * just selected. Applied client-side via the existing aNPC_setupAction()
                              * call (see aNPC_pc_client_consume_move(), ac_npc_move.c_inc), now given a
                              * real host-authoritative value instead of inferring walk-vs-idle purely
                              * from position deltas. No wire size change -- still 28 bytes; see the
                              * _Static_assert below. */
    uint8_t  _reserved1;
    uint16_t npc_id;        /* Animal_c.id.npc_id at sample time -- slot-reuse identity guard */
    uint16_t _reserved2;
    uint32_t frame;         /* sender's own monotonically increasing per-send counter (shared across
                              * every slot -- see pc_net_game_notify_npc_move()'s doc) */
    float    pos_x;
    float    pos_y;
    float    pos_z;
    int16_t  facing_angle;  /* world.angle.y, native engine angle units */
    int16_t  _reserved3;
} PCNetGameNpcMoveMsg;
_Static_assert(sizeof(PCNetGameNpcMoveMsg) == 28,
               "PCNetGameNpcMoveMsg wire size drifted -- N3 Channel A repurposed _reserved0 as "
               "action_type in place, no size change intended");
_Static_assert(sizeof(PCNetGameNpcMoveMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameNpcMoveMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* N-clock milestone: host -> every READY client, RELIABLE. clock_seq is monotonic HOST-side (one
 * shared counter across the initial per-peer send and every periodic/discontinuity broadcast --
 * see s_host_clock_seq) -- a client applies a received sync only if clock_seq > its own last-applied
 * value (pcnetgame_handle_client_clock_sync()), exactly mirroring s_client_meta_seq's own
 * strict-monotonic contract (WORLD_META) rather than inventing a new one.
 *
 * host_game_ticks is the RAW OSGetTime()+Save_Get(time_delta) tick value at send time -- deliberately
 * NOT a PCNetGameRtcWire (calendar fields): the client's correction is a single subtraction
 * (host_game_ticks - (OSGetTime()+Save_Get(time_delta)) on the client's own clock read at receipt) that
 * preserves sub-second precision, rather than a lossy round-trip through calendar-second fields. See
 * lb_rtc.c's pc_lb_rtc_set_net_clock_offset()/lbRTC_GetGameTime() for exactly how the resulting offset
 * is folded back in.
 *
 * flags bit 0 (DISCONTINUITY) means the host itself detected its own Save_Get(time_delta) change
 * between two polls (a manual clock-adjust via the in-game menu, lbRTC_SetTime()) -- see
 * pcnetgame_host_check_clock_sync()'s time_delta watch. A client always re-applies immediately on this
 * flag regardless of the normal drift-tolerance check (see PC_NETGAME_CLOCK_SYNC_TOLERANCE_SEC below),
 * since a manual adjust is a deliberate, large, intentional change, not clock jitter. */
#define PC_NETGAME_CLOCK_SYNC_FLAG_DISCONTINUITY 0x01u
typedef struct PCNetGameClockSyncMsg {
    uint8_t  msg_type;       /* PC_NETGAME_MSG_CLOCK_SYNC */
    uint8_t  flags;          /* PC_NETGAME_CLOCK_SYNC_FLAG_* */
    uint16_t _reserved0;
    uint32_t clock_seq;      /* monotonic, host-side; client applies only if > last applied */
    int64_t  host_game_ticks; /* OSGetTime() + Save_Get(time_delta) at send time -- raw ticks */
} PCNetGameClockSyncMsg;
_Static_assert(sizeof(PCNetGameClockSyncMsg) == 16, "PCNetGameClockSyncMsg wire size drifted");
_Static_assert(sizeof(PCNetGameClockSyncMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameClockSyncMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* N-clock milestone tuning constants -- see the design note above PC_NETGAME_MSG_CLOCK_SYNC and
 * pcnetgame_host_check_clock_sync()/pcnetgame_handle_client_clock_sync() below. */
#define PC_NETGAME_CLOCK_SYNC_INTERVAL_MS    30000u /* periodic host broadcast cadence (real ms) */
#define PC_NETGAME_CLOCK_SYNC_TOLERANCE_SEC  2      /* client re-applies only past this much drift */
#define PC_NETGAME_CLOCK_SYNC_LOUD_LOG_SEC   3600   /* log loudly above this many seconds of correction */

/* N3 Channel B: host -> every READY client, RELIABLE, sent only on CHANGE (never every frame). Carries
 * the small cluster of villager schedule/home state the audit found is written together and must
 * never be observed by a client in a contradictory intermediate combination: is_home, hide (the
 * actor's hide_flg/hide_request state), forced_type/forced_timer_remaining (mNPS_schedule_c's forced
 * schedule override -- see ac_npc_think_sleep.c_inc's aNPC_think_sleep_set_force_schedule(), the
 * actual site that sets forced_type together with forced_timer; aNPC_setup_stay_my_house() and
 * aNPC_act_leave_house_wait() (ac_npc_action.c_inc / ac_npc_act_leave_house.c_inc) are the is_home
 * transition points, sourced this way rather than hooked individually because forced_timer counts
 * down continuously outside any single write site -- see pc_net_game_notify_npc_state()'s own doc
 * comment for why a per-frame dirty-check against a host-side shadow is used instead of hooking every
 * call site individually).
 *
 * `slot`/`npc_id` reuse the exact identity pairing every other villager message already uses.
 * `state_seq` is a single, global (not per-slot) monotonic counter -- mirroring WORLD_META's
 * s_client_meta_seq strict-`>` convention -- incremented once per host broadcast of ANY slot; a client
 * applies a received NPC_STATE only if state_seq > its own s_client_npc_state_seq[slot] (per-slot last-
 * applied value, so one slot's broadcast can never suppress another's). forced_timer_remaining is
 * uint16_t: mNPS_schedule_c.forced_timer is only ever set to 7200 (aNPC_think_sleep_set_force_schedule)
 * and only ever decremented from there, so it never exceeds 7200 -- see m_npc_schedule_h.h's
 * declaration (`int forced_timer`) and m_npc_schedule.c's mNPS_schedule_manager_sub() clamp-at-0 logic;
 * pc_net_game_notify_npc_state() clamps defensively anyway before narrowing. */
typedef struct PCNetGameNpcStateMsg {
    uint8_t  msg_type;               /* PC_NETGAME_MSG_NPC_STATE */
    uint8_t  slot;                   /* Save_t.animals[] index */
    uint16_t npc_id;                 /* Animal_c.id.npc_id at sample time -- slot-reuse identity guard */
    uint8_t  is_home;
    uint8_t  hide;
    uint8_t  forced_type;
    uint8_t  _reserved0;
    uint16_t forced_timer_remaining; /* mNPS_schedule_c.forced_timer, clamped to uint16_t range */
    uint16_t _reserved1;
    uint32_t state_seq;              /* global monotonic; client applies only if > its own per-slot last */
} PCNetGameNpcStateMsg;
_Static_assert(sizeof(PCNetGameNpcStateMsg) == 16, "PCNetGameNpcStateMsg wire size drifted");
_Static_assert(sizeof(PCNetGameNpcStateMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameNpcStateMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* Friendship/mail sync milestone.
 *
 * Design note: Anmmem_c.memories[] is NOT a fixed per-player slot -- it is a small (7-entry, see
 * ANIMAL_MEMORY_NUM) content-addressed cache keyed by PersonalID_c, searched by
 * mNpc_GetAnimalMemoryIdx()/allocated by mNpc_ForceGetFreeAnimalMemoryIdx() (m_npc.c). A network
 * CLIENT has exactly ONE PersonalID_c for its whole connection (established once at IDENTITY
 * handshake and cached in PCNetGameHostPeerState -- see pcnetgame_handle_host_identity()), so a
 * FRIENDSHIP_REQUEST never needs to say WHICH player it's for: the host resolves (find-or-create)
 * the memory slot from the SENDING PEER's own cached identity, exactly mirroring how
 * mNpc_SetAnimalLastTalk() resolves it from Common_Get(now_private) for a same-machine interaction
 * (see mNpc_PcHostResolveAndApplyFriendshipDelta(), m_npc.c). player_name/land_name/player_id/
 * land_id in the broadcasts below are the exact same 4 PersonalID_c fields PCNetGameIdentity already
 * puts on the wire today (see this file's top-of-file design note) -- not new exposure, every
 * connected peer already learns every other peer's identity at READY (pc_remote_player_on_ready()).
 * A remote client never receives more than that: not the human-readable letter TEXT of another
 * player's mail beyond what mNpc_SendMailtoNpc() itself would already reveal to anyone who later
 * reads that same in-game letter from the villager. */

/* client -> host only, reliable. Sent from mNpc_AddFriendship()'s client-intercept branch INSTEAD
 * OF applying `delta` locally. `slot` is the Save_t.animals[] index owning the Anmmem_c the caller
 * resolved via mNpc_FindAnimalSlotForMemory(). No sequencing needed on the request itself -- pc_net's
 * reliable transport is already ordered per peer per direction, and the host applies deltas as they
 * arrive against its own live, authoritative Anmmem_c (there is nothing to reorder against). */
typedef struct PCNetGameFriendshipRequestMsg {
    uint8_t msg_type; /* PC_NETGAME_MSG_FRIENDSHIP_REQUEST */
    uint8_t slot;
    int8_t  delta;
    uint8_t _reserved0;
} PCNetGameFriendshipRequestMsg;
_Static_assert(sizeof(PCNetGameFriendshipRequestMsg) == 4, "PCNetGameFriendshipRequestMsg wire size drifted");
_Static_assert(sizeof(PCNetGameFriendshipRequestMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameFriendshipRequestMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* host -> every READY client, reliable, world_seq-stamped (the SAME shared s_world_seq counter
 * ARRIVAL/DEPARTURE/WORLD_META already use -- see this file's design note above
 * PCNetGameVillagerArrivalMsg for why one counter, not a new per-feature scheme). Sent after the
 * host has ALREADY applied the resulting, already-clamped (0..127) friendship value to its own
 * Anmmem_c -- never before. A client applies this iff world_seq > its own last-applied
 * s_client_friendship_seq (strict `>`, exactly mirroring ARRIVAL/DEPARTURE's own rule: this is a
 * one-shot delta result, not a full resync that a same-or-later value should always supersede). */
typedef struct PCNetGameFriendshipUpdateMsg {
    uint8_t  msg_type; /* PC_NETGAME_MSG_FRIENDSHIP_UPDATE */
    uint8_t  slot;
    int8_t   friendship; /* final, already-clamped 0..127 value */
    uint8_t  _reserved0;
    uint8_t  player_name[PC_NETGAME_NAME_LEN];
    uint8_t  land_name[PC_NETGAME_LAND_LEN];
    uint16_t player_id;
    uint16_t land_id;
    uint32_t world_seq;
} PCNetGameFriendshipUpdateMsg;
_Static_assert(sizeof(PCNetGameFriendshipUpdateMsg) == 28, "PCNetGameFriendshipUpdateMsg wire size drifted");
_Static_assert(sizeof(PCNetGameFriendshipUpdateMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameFriendshipUpdateMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* host -> one client, reliable. Sent once per OCCUPIED Anmmem_c entry (late-join/reconnect coverage
 * for memories[], which VILLAGER_SNAPSHOT never carries), between VILLAGER_SNAPSHOT and
 * SNAPSHOT_END (see pcnetgame_host_pump_snapshots()'s snap_stage 4 -- World Ecology Wildlife Sync T2
 * later inserted its own wildlife sub-pass as stage 3 between VILLAGER_SNAPSHOT and this stage,
 * pushing FRIENDSHIP_SNAPSHOT_ENTRY from its original stage 3 to stage 4) -- iterated flat over
 * (ANIMAL_NUM_MAX * ANIMAL_MEMORY_NUM) = 105 (slot, memory_idx) pairs, empty ones skipped (never
 * sent) rather than padding out a fixed-size batch, since most villagers have far fewer than 7
 * remembered players. `>=` rule (matches FIELD_BLOCK/VILLAGER_SNAPSHOT, not WORLD_META/
 * FRIENDSHIP_UPDATE's strict `>`): a snapshot entry always supersedes whatever came before, at the
 * same or a later world_seq. has_letter/letter mirror mail-sync's outcome (Anmplmail_c, raw bytes,
 * opaque here -- decoded by mNpc_PcApplyFriendshipUpdate()/its snapshot counterpart in m_npc.c,
 * exactly like PCNetPlayerAppearance's design_record is an opaque blob at this layer). */
typedef struct PCNetGameFriendshipSnapshotEntryMsg {
    uint8_t  msg_type; /* PC_NETGAME_MSG_FRIENDSHIP_SNAPSHOT_ENTRY */
    uint8_t  slot;
    int8_t   friendship;
    uint8_t  letter_info; /* raw Anmlet_c byte */
    uint8_t  player_name[PC_NETGAME_NAME_LEN];
    uint8_t  land_name[PC_NETGAME_LAND_LEN];
    uint16_t player_id;
    uint16_t land_id;
    uint32_t world_seq;
    uint8_t  has_letter;
    uint8_t  _reserved0[3];
    uint8_t  letter[258]; /* raw Anmplmail_c bytes; meaningful only when has_letter */
} PCNetGameFriendshipSnapshotEntryMsg;
_Static_assert(sizeof(PCNetGameFriendshipSnapshotEntryMsg) == 292,
               "PCNetGameFriendshipSnapshotEntryMsg wire size drifted");
_Static_assert(sizeof(PCNetGameFriendshipSnapshotEntryMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameFriendshipSnapshotEntryMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* client -> host only, reliable. Sent from mNpc_SendMailtoNpc()'s client-intercept branch INSTEAD
 * OF running the real function locally (which would mutate this client's own copy of the shared
 * villager's Anmmem_c out of authority). `mail` is a raw, opaque copy of the decomp Mail_c the
 * player composed at the post office (flat/POD, no pointers -- verified against m_mail.h, same
 * "safe to transmit, not decoded here" treatment PCNetGameIdentity's player_name/land_name and
 * PCNetPlayerAppearance's design_record already get in this file). */
typedef struct PCNetGameMailRequestMsg {
    uint8_t msg_type; /* PC_NETGAME_MSG_MAIL_REQUEST */
    uint8_t _reserved0[3];
    uint8_t mail[298]; /* raw Mail_c bytes; sizeof(Mail_c) _Static_assert'd in m_npc.c's PC helper */
} PCNetGameMailRequestMsg;
_Static_assert(sizeof(PCNetGameMailRequestMsg) == 302, "PCNetGameMailRequestMsg wire size drifted");
_Static_assert(sizeof(PCNetGameMailRequestMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameMailRequestMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* host -> every READY client, reliable, world_seq-stamped (same shared s_world_seq counter, same
 * strict `>` rule as FRIENDSHIP_UPDATE -- a one-shot delta result). Sent after the host has already
 * run the real mail-delivery logic (mNpc_PcApplyMailToVillagerMemory(), m_npc.c) against its own
 * authoritative Anmmem_c. friendship is redundant with any FRIENDSHIP_UPDATE the same delivery also
 * triggers (mNpc_SendMailtoNpc() calls mNpc_AddFriendship() internally) -- included anyway so a
 * client that applies this message alone (e.g. one that joins between the two broadcasts) still ends
 * up fully consistent, matching WORLD_META's "always carry every sub-state's current value" policy. */
typedef struct PCNetGameMailDeliveredMsg {
    uint8_t  msg_type; /* PC_NETGAME_MSG_MAIL_DELIVERED */
    uint8_t  slot;
    int8_t   friendship;
    uint8_t  letter_info;
    uint8_t  player_name[PC_NETGAME_NAME_LEN];
    uint8_t  land_name[PC_NETGAME_LAND_LEN];
    uint16_t player_id;
    uint16_t land_id;
    uint32_t world_seq;
    uint8_t  letter[258];
} PCNetGameMailDeliveredMsg;
_Static_assert(sizeof(PCNetGameMailDeliveredMsg) == 288, "PCNetGameMailDeliveredMsg wire size drifted");
_Static_assert(sizeof(PCNetGameMailDeliveredMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameMailDeliveredMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

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
_Static_assert(sizeof(PCNetGameWorldMetaMsg) == 48, "PCNetGameWorldMetaMsg wire size drifted"); /* 46
    logical bytes padded to 48 for uint32_t alignment -- see PCNetGameSnapshotEndMsg's assert above */
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

/* World Ecology milestone (Stage 1): client -> host only. Deliberately ONE ROUND TRIP -- unlike
 * PICKUP/DROP's two-phase (provisional accept + separate CONFIRM), both actions below commit their
 * field write synchronously, inside the SAME host function call that validates the request and
 * before it replies -- see pcnetgame_handle_host_field_action_request(). This is safe without a
 * provisional-reservation window because the host processes one incoming message to completion
 * before the next: a second request for the same tile (a retry, or two players racing) is validated
 * against whatever the FIRST request already committed, so it naturally fails closed (deposit
 * already cleared / hit-count window already advanced) with no explicit reservation table needed.
 * PICKUP's two-phase design exists for a DIFFERENT reason this stage's two actions don't share: an
 * accepted pickup could still fail client-side (pockets full) after the field already appeared to
 * commit, so it needs the client's own confirmation before the host actually removes the item. Both
 * actions here either need no client-side step at all (MONEY_ROCK_HIT), or the caller already
 * verified a free pocket slot BEFORE ever sending the request (DIG_BURIED -- see
 * pc_net_game_request_dig_buried()'s own doc) or by the same 5A pattern.
 * ut_x/ut_z are global town-tile coordinates, exactly like PCNetGamePickupRequestMsg's own.
 * request_id is this connection's own monotonically advancing counter (shared across both kinds --
 * see s_next_field_action_request_id), meaningful only paired with the sender's PCNetPeerId. */
#define PC_NETGAME_FIELD_ACTION_KIND_DIG_BURIED   1u
#define PC_NETGAME_FIELD_ACTION_KIND_MONEY_ROCK_HIT 2u
/* World Ecology T1: tree shake (fruit/furniture/bells/bee-birth drop, or a bee-tree's destination-tree
 * conversion only) and tree chop (host-tracked cut-count decrement, drop resolution, eventual stump) --
 * see pc_net_game_request_tree_shake()/pc_net_game_request_tree_chop() (pc_net_game.h) and
 * pcnetgame_host_commit_tree_shake()/pcnetgame_host_commit_tree_chop() below. */
#define PC_NETGAME_FIELD_ACTION_KIND_TREE_SHAKE 3u
#define PC_NETGAME_FIELD_ACTION_KIND_TREE_CHOP  4u
/* World Ecology T-dig (dig/pitfall/shine family): kinds 5-8. DIG_BURIED (kind 1, above) is EXTENDED --
 * not replaced -- to also accept the "buried pitfall being dug up" signature (deposit OFF, tile in
 * BURIED_PITFALL_HOLE00..24) and resolve it to ITM_PITFALL; see pcnetgame_validate_and_resolve_dig()'s
 * own updated doc. The four kinds below are genuinely new outcomes DIG_BURIED's own shape cannot
 * express (no item granted, or a fundamentally different tile transition):
 *   DIG_HOLE        digging a brand-new hole into EMPTY_NO ground, or removing a flower/stump/sapling/
 *                    grass tuft (mFI_CheckDigRemoveItem()'s own list, m_field_info.c) -- grants NOTHING
 *                    (a removed plant flies off and fades; it is never placed in a pocket -- see
 *                    pcnetgame_fa_validate_adapter_dig_hole()'s own doc for the historical confusion
 *                    this corrects). Commits tile -> HOLE_START + the client-supplied, RANGE-VALIDATED
 *                    hole_variant (0..24), deposit OFF.
 *   FILL_HOLE       filling an existing EMPTY hole (deposit OFF) back in. Commits tile -> EMPTY_NO.
 *   PITFALL_CONSUME a player/villager falling INTO an already-buried pitfall (a *trigger*, distinct
 *                    from DIGGING one up, which stays on the extended DIG_BURIED path above). Commits
 *                    tile -> EMPTY_NO directly (skips vanilla's transient HOLE_n stage -- safe since no
 *                    item is ever granted on this path). Client plays its fall animation optimistically,
 *                    unconditionally, before the RESULT ever arrives (see
 *                    pc_net_game_request_pitfall_consume()'s own doc).
 *   DIG_SHINE       digging up an UNBURIED SHINE_SPOT (deposit OFF). Commits tile -> HOLE_SHINE
 *                    unconditionally; hole_variant is ignored for this kind (a shine hole is not
 *                    variant-shaped). Grants NOTHING host-side -- the digging CLIENT rolls its own bell
 *                    amount locally and grants it privately on accept (matches vanilla's per-digger-luck
 *                    design; no duplication risk since only one digger can win the tile-consumption
 *                    race). */
#define PC_NETGAME_FIELD_ACTION_KIND_DIG_HOLE        5u
#define PC_NETGAME_FIELD_ACTION_KIND_FILL_HOLE       6u
#define PC_NETGAME_FIELD_ACTION_KIND_PITFALL_CONSUME 7u
#define PC_NETGAME_FIELD_ACTION_KIND_DIG_SHINE       8u
/* World Ecology: snowmen -- breaking an existing snowman (kind 9) reuses this SAME
 * FIELD_ACTION_REQUEST/FIELD_ACTION_RESULT pair; granted_item is always 0 (nothing is granted --
 * see aPSM_actor_move(), ac_psnowman.c). Building a NEW snowman is a separate, dedicated
 * request/result pair (PCNetGameSnowmanBuildRequestMsg/PCNetGameSnowmanBuildResultMsg below), not a
 * FIELD_ACTION kind, because unlike every kind above it needs no reach/IN_TOWN precondition at all
 * (see pc_net_game_request_snowman_build()'s own doc, pc_net_game.h). */
#define PC_NETGAME_FIELD_ACTION_KIND_SNOWMAN_BREAK 9u
/* Protocol v3: widened from 8 to 12 bytes to add `hole_variant` -- the CLIENT's own proposed hole-shape
 * pick (0..24, mirroring HOLE_START..HOLE_END's own range), used ONLY by DIG_HOLE (kind 5) and the
 * pitfall-dig sub-case of the extended DIG_BURIED (kind 1); every other kind (2/3/4/6/7/8/9) ignores it
 * and every sender explicitly sets it to 0 for those kinds (see each pc_net_game_request_*() below).
 * TRUST BOUNDARY: this is authoritative, persisted tile-shape state once committed, NOT a cosmetic
 * hint -- the host MUST range-check it (0..24) before ever using it to compute HOLE_START + variant;
 * an out-of-range value is rejected outright rather than clamped, so a forged/corrupted value can never
 * write an unintended tile value (see pcnetgame_validate_hole_variant()). kind/ut_x/ut_z/request_id
 * keep their EXACT pre-v3 offsets/meanings -- only `hole_variant` + 3 bytes of trailing padding were
 * appended; a v2 hole-variant-unaware kind (1-4, 9) still reads correctly as long as both ends agree on
 * the (now 12-byte) size, which is exactly why the protocol version bump forces that agreement instead
 * of letting an old 8-byte peer silently misparse the new layout. */
typedef struct PCNetGameFieldActionRequestMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_FIELD_ACTION_REQUEST */
    uint8_t  kind;          /* PC_NETGAME_FIELD_ACTION_KIND_* */
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint32_t request_id;
    uint8_t  hole_variant;  /* 0..24 -- DIG_HOLE / DIG_BURIED's pitfall sub-case only; 0 for every other
                                kind. TRUST BOUNDARY -- see this struct's own doc above. */
    uint8_t  _reserved0;
    uint16_t _reserved1;
} PCNetGameFieldActionRequestMsg;
_Static_assert(sizeof(PCNetGameFieldActionRequestMsg) == 12, "PCNetGameFieldActionRequestMsg wire size drifted");
_Static_assert(sizeof(PCNetGameFieldActionRequestMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameFieldActionRequestMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* World Ecology milestone (Stage 1): host -> the one requesting client only. accepted == 0 is a
 * final rejection (nothing was committed, nothing to undo). accepted == 1 means the host ALREADY
 * committed the field write(s) for this action (see PCNetGameFieldActionRequestMsg's own doc) --
 * every client, including the requester, also converges on the tile via the ordinary FIELD_UPDATE
 * broadcast pcfa_set_tile() already triggers; this message additionally carries whatever the
 * REQUESTER alone needs to finish its own side:
 *   DIG_BURIED       granted_item is the item to grant into a free pocket slot (never re-derived
 *                    locally -- see pcnetgame_handle_client_field_action_result()). Never a
 *                    present/dummy sentinel (resolved once, host-side, exactly like
 *                    pcnetgame_resolve_pickup_item()).
 *   MONEY_ROCK_HIT   granted_item is unused (always 0) -- the dropped money bag is an entirely
 *                    ordinary field item (Bug 4 fix) and is picked up later through the completely
 *                    ordinary pickup path, exactly like any other dropped item. */
typedef struct PCNetGameFieldActionResultMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_FIELD_ACTION_RESULT */
    uint8_t  kind;          /* PC_NETGAME_FIELD_ACTION_KIND_* */
    uint8_t  accepted;
    uint8_t  ut_x;
    uint32_t request_id;
    uint16_t granted_item;
    uint8_t  ut_z;
    uint8_t  _reserved0;
} PCNetGameFieldActionResultMsg;
_Static_assert(sizeof(PCNetGameFieldActionResultMsg) == 12, "PCNetGameFieldActionResultMsg wire size drifted");
_Static_assert(sizeof(PCNetGameFieldActionResultMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameFieldActionResultMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* World Ecology T3 (host-authoritative bury): client -> host only, reliable, 12 bytes. Mirrors
 * PICKUP_REQUEST/DROP_REQUEST's two-phase shape (reserve -> the client does its own inventory step ->
 * INTERACT_CONFIRM), not FIELD_ACTION_REQUEST's synchronous one-shot shape, because
 * bIT_common_bury_after()/bIT_common_hole_throw() (bg_item_common.c_inc) show burying removes an item
 * from a POCKET SLOT first -- exactly PICKUP/DROP's own "client must confirm its local inventory step
 * actually happened before the host commits the field" problem, not DIG_BURIED/MONEY_ROCK_HIT's
 * "nothing client-side can fail" one.
 * pocket_slot_idx/claimed_item are a TRUST BOUNDARY exactly like DROP_REQUEST's own (the host holds no
 * shadow of any remote player's inventory) -- the host never assumes claimed_item is actually in the
 * requester's pocket; pcnetgame_is_buryable_item() classifies it on its own claimed value alone.
 * hole_variant is the client's own locally-computed mCoBG_GetHoleNumber(shovel_pos) result (0..24), or
 * 0xFF for that function's -1 ("no valid hole shape") sentinel. UNLIKE the T0 scaffolding's original
 * claim, this field is NOT purely cosmetic: it is AUTHORITATIVE for exactly one sub-case -- burying
 * ITM_PITFALL into a HOLE_SHINE tile (the host can derive the hole's shape itself from the tile value
 * for an ordinary HOLE00..24 target, but a HOLE_SHINE tile carries no shape information of its own, so
 * the client's own locally-computed hole_variant is trusted there; see the BURY commit branch in
 * pcnetgame_handle_host_confirm() for the exact rule). Every OTHER bury outcome discards this field
 * entirely (the host derives the hole shape itself from its own re-read tile, or the outcome doesn't
 * need one at all) -- so "never trusted for gameplay outcome" remains true for every case except that
 * one, which is called out explicitly at its own point of use. request_id is this connection's own
 * counter, mirroring DROP_REQUEST's own retry/dedup pattern. */
typedef struct PCNetGameBuryRequestMsg {
    uint8_t  msg_type;         /* PC_NETGAME_MSG_BURY_REQUEST */
    uint8_t  pocket_slot_idx;  /* 0..mPr_POCKETS_SLOT_COUNT-1 -- mirrors PCNetGameDropRequestMsg's own */
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint16_t claimed_item;     /* TRUST BOUNDARY -- mirrors PCNetGameDropRequestMsg's own claimed_item */
    uint8_t  hole_variant;     /* AUTHORITATIVE for PITFALL-into-HOLE_SHINE only -- see doc above */
    uint8_t  _reserved0;       /* always sent 0 */
    uint32_t request_id;
} PCNetGameBuryRequestMsg;
_Static_assert(sizeof(PCNetGameBuryRequestMsg) == 12, "PCNetGameBuryRequestMsg wire size drifted");
_Static_assert(sizeof(PCNetGameBuryRequestMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameBuryRequestMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* World Ecology T3: host -> the one requesting client only, reliable, 12 bytes. Mirrors
 * PCNetGameDropResultMsg's exact shape: accepted == 1 is PROVISIONAL (reserve; the client must still
 * answer INTERACT_CONFIRM), accepted == 0 is a final rejection.
 *   buried_item  on ACCEPT: echoes the client's own claimed_item back (a cross-check against the
 *                client's own pending record, mirroring DROP_RESULT's placed_item echo -- never the
 *                RESOLVED outcome tile: the resolved sapling/flower/pitfall/plain-bury tile value is
 *                delivered to every client, including this one, through the ordinary ambient
 *                FIELD_UPDATE broadcast the commit triggers, exactly like every other two-phase
 *                interaction). On REJECT: the HOST's CURRENT actual tile value at (ut_x, ut_z),
 *                re-read fresh at send time (never a value captured earlier) -- the reconciliation
 *                echo, following the same pattern already proven for PITFALL_CONSUME's reject echo
 *                (pcnetgame_fa_pitfall_consume_echo_current()). Meaningful only when flags'
 *                RECONCILE_VALID bit is set.
 *   flags        bit0 PC_NETGAME_BURY_FLAG_RECONCILE_VALID: set whenever buried_item on a REJECT is a
 *                genuine current-tile echo (vs. simply 0 because the tile couldn't even be resolved).
 *                bit1 PC_NETGAME_BURY_FLAG_DEPOSIT_ON: the CURRENT deposit-bit state of that tile --
 *                needed because, unlike PITFALL_CONSUME (whose reject post-states are always deposit
 *                OFF), a bury race can leave deposit ON (another peer's bury already committed there),
 *                and the client must apply it via the same (value, dep_valid, dep_on) triple
 *                pcnetgame_client_apply_tile() already uses for FIELD_UPDATE, not just the raw value.
 *   reason       diagnostic only, PC_NETGAME_BURY_REASON_* (0 = unused/none); never load-bearing --
 *                the client's accept/reject branch already fully determines its own behavior from
 *                accepted/flags/buried_item alone. */
#define PC_NETGAME_BURY_FLAG_RECONCILE_VALID 0x01u
#define PC_NETGAME_BURY_FLAG_DEPOSIT_ON      0x02u
#define PC_NETGAME_BURY_REASON_NONE 0u
typedef struct PCNetGameBuryResultMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_BURY_RESULT */
    uint8_t  accepted;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint32_t request_id;
    uint16_t buried_item;
    uint8_t  flags;         /* PC_NETGAME_BURY_FLAG_* -- see doc above (was _reserved0's low byte) */
    uint8_t  reason;        /* PC_NETGAME_BURY_REASON_* -- diagnostic only (was _reserved0's high byte) */
} PCNetGameBuryResultMsg;
_Static_assert(sizeof(PCNetGameBuryResultMsg) == 12, "PCNetGameBuryResultMsg wire size drifted");
_Static_assert(sizeof(PCNetGameBuryResultMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameBuryResultMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* World Ecology: snowmen. Client -> host only, reliable, 12 bytes. Sent from
 * aSNOWMAN_Set_PSnowman_info() (ac_snowman.c) -- see pc_net_game_request_snowman_build()'s own doc
 * (pc_net_game.h) for why this has no reach/IN_TOWN precondition on the sender. head_size/body_size/
 * score are the exact 0..255 (0..3 for score) fields mSN_snowman_info_c.data (m_snowman.h) already
 * carries -- a TRUST BOUNDARY like every other claimed/raw client-asserted field in this file
 * (score is clamped, not rejected, if it somehow arrives above 3 -- see
 * pcnetgame_handle_host_snowman_build_request()'s own doc). request_id is this connection's own
 * counter (shares s_next_field_action_request_id's space -- meaningful only paired with the sender's
 * PCNetPeerId, never persisted). */
typedef struct PCNetGameSnowmanBuildRequestMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_SNOWMAN_BUILD_REQUEST */
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint8_t  head_size;
    uint8_t  body_size;
    uint8_t  score;
    uint16_t _reserved0;
    uint32_t request_id;
} PCNetGameSnowmanBuildRequestMsg;
_Static_assert(sizeof(PCNetGameSnowmanBuildRequestMsg) == 12, "PCNetGameSnowmanBuildRequestMsg wire size drifted");
_Static_assert(sizeof(PCNetGameSnowmanBuildRequestMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameSnowmanBuildRequestMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* World Ecology: snowmen. Host -> the one requesting client only, reliable, 12 bytes. accepted == 1
 * means the host already committed Save_t.snowmen/the field tile/the completion dates (see
 * pcnetgame_handle_host_snowman_build_request()) -- every client, including the requester, also
 * converges via the ordinary FIELD_UPDATE broadcast pcfa_set_tile() triggers PLUS the dedicated
 * PC_NETGAME_MSG_SNOWMAN_STATE broadcast (slot/head/body/score/dates -- data a bare tile value can't
 * carry). `slot` is the Save_t.snowmen.snowmen_data[] index the host actually used (0xFF when
 * rejected). `reason` is only meaningful when accepted == 0: SLOTS_FULL (1, every slot already
 * occupied -- matches vanilla's own silent-drop-on-full, no queueing), TILE_INVALID (2, the target
 * tile could not host a new snowman), NOT_READY (3, host world/save not ready). The client has nothing
 * to visually undo on a rejection -- the local two-half snowman actor is already gone (Actor_delete()
 * already ran) by the time this arrives; it is only ever logged (see
 * pcnetgame_handle_client_snowman_build_result()). */
#define PC_NETGAME_SNOWMAN_REASON_NONE         0u
#define PC_NETGAME_SNOWMAN_REASON_SLOTS_FULL   1u
#define PC_NETGAME_SNOWMAN_REASON_TILE_INVALID 2u
#define PC_NETGAME_SNOWMAN_REASON_NOT_READY    3u
typedef struct PCNetGameSnowmanBuildResultMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_SNOWMAN_BUILD_RESULT */
    uint8_t  accepted;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint32_t request_id;
    uint8_t  slot;
    uint8_t  reason;
    uint16_t _reserved0;
} PCNetGameSnowmanBuildResultMsg;
_Static_assert(sizeof(PCNetGameSnowmanBuildResultMsg) == 12, "PCNetGameSnowmanBuildResultMsg wire size drifted");
_Static_assert(sizeof(PCNetGameSnowmanBuildResultMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameSnowmanBuildResultMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* World Ecology: snowmen. Host -> every READY client, reliable, world_seq-stamped like WORLD_META --
 * but stamped with its OWN independent sequence (s_snowman_world_seq / s_client_snowman_seq), never
 * s_world_seq, because this carries off-tile snowman data (size/score/dates) that changes on a
 * completely different cadence (build/break/melt) than the ordinary field diff. UNLIKE WORLD_META (and
 * pcnetgame_broadcast_villager_msg()'s own convention), this deliberately does NOT skip a peer whose
 * snap_active is set: it is a full, idempotent, self-contained snapshot of ALL THREE slots + all 4
 * date bytes, with no SNAPSHOT_END fallback ever carrying it, so a peer mid-snapshot still needs every
 * broadcast to converge (see pcnetgame_broadcast_snowman_state()'s own doc). `flags` bit 0
 * (IN_SNAPSHOT) is set only on the one copy sent as part of a peer's own late-join/RESYNC_REQUEST
 * snapshot sequence (pcnetgame_host_pump_snapshots()) -- purely informational, never checked on
 * receipt. `snowmen[12]` is Save_t.snowmen.snowmen_data[3] (mSN_snowman_data_c, 4 bytes each: exists,
 * head_size, body_size, score), copied element-wise, byte-for-byte -- no byte-swap needed, every field
 * is u8. year/month/day/hour are Save_t.snowman_year/month/day/hour -- NOT contiguous with
 * snowmen_data in Save_t (m_common_data.h), so these 16 bytes are gathered by hand, never memcpy'd as
 * one block (see pcnetgame_build_snowman_state_bytes()). The client applies iff received world_seq >=
 * its own s_client_snowman_seq, after independently re-validating every field against the exact same
 * rules sChk_snowman_save_check() (save_check_take.c_inc) already enforces for a loaded save --
 * dropped and logged, never applied, on failure (see pcnetgame_handle_client_snowman_state()). */
#define PC_NETGAME_SNOWMAN_STATE_FLAG_IN_SNAPSHOT 0x01u
typedef struct PCNetGameSnowmanStateMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_SNOWMAN_STATE */
    uint8_t  flags;         /* PC_NETGAME_SNOWMAN_STATE_FLAG_* */
    uint16_t _reserved0;
    uint32_t world_seq;
    uint8_t  snowmen[12];   /* Save_t.snowmen.snowmen_data[3], 4 bytes/slot: exists, head, body, score */
    uint8_t  year;
    uint8_t  month;
    uint8_t  day;
    uint8_t  hour;
} PCNetGameSnowmanStateMsg;
_Static_assert(sizeof(PCNetGameSnowmanStateMsg) == 24, "PCNetGameSnowmanStateMsg wire size drifted");
_Static_assert(sizeof(PCNetGameSnowmanStateMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameSnowmanStateMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");
_Static_assert(sizeof(((PCNetGameSnowmanStateMsg*)0)->snowmen) == 12 &&
                   sizeof(mSN_snowman_data_c) == 4 && mSN_SAVE_COUNT == 3,
               "PCNetGameSnowmanStateMsg.snowmen size drifted from Save_t.snowmen.snowmen_data[3]");

/* World Ecology Wildlife Sync T0: client -> host only, reliable, 4 bytes. Sent from the decomp
 * wade-trigger seam (aSetMgr_move_set(), ac_set_manager.c) -- see
 * pc_net_game_request_wildlife_spawn_trigger()'s own doc (pc_net_game.h). bx/bz are SET_MANAGER's
 * own raw block-number player_pos.next_bx/next_bz -- a TRUST BOUNDARY (bounds-checked host-side,
 * see pcnetgame_handle_host_wildlife_spawn_trigger_request()) but otherwise no species/RNG
 * result/position is ever carried: the host alone decides what (if anything) spawns. Deliberately
 * has no request_id/reply -- unlike PICKUP/DROP/BURY's two-phase shape or even
 * FIELD_ACTION_REQUEST's one-shot-with-RESULT shape, nothing is granted back to the requester (the
 * eventual WILDLIFE_SPAWN broadcast, if any, reaches every READY client identically, requester
 * included -- there is nothing requester-specific to reply with). A lost/dropped send here simply
 * means this one wade event's acre never gets evaluated -- an accepted, low-stakes gap exactly like
 * a real vanilla wade that happens not to roll a spawn. */
typedef struct PCNetGameWildlifeSpawnTriggerRequestMsg {
    uint8_t msg_type; /* PC_NETGAME_MSG_WILDLIFE_SPAWN_TRIGGER_REQUEST */
    uint8_t bx;
    uint8_t bz;
    uint8_t _reserved0;
} PCNetGameWildlifeSpawnTriggerRequestMsg;
_Static_assert(sizeof(PCNetGameWildlifeSpawnTriggerRequestMsg) == 4,
               "PCNetGameWildlifeSpawnTriggerRequestMsg wire size drifted");
_Static_assert(sizeof(PCNetGameWildlifeSpawnTriggerRequestMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameWildlifeSpawnTriggerRequestMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net "
               "would drop it");

/* World Ecology Wildlife Sync T0: host -> every READY client, reliable, 24 bytes. One freshly
 * created authoritative wildlife record -- see pc_wildlife_authority.h/.c for how entity_id/
 * kind/species/bx,bz/position are derived (the host's own narrow adapter around the unmodified
 * vanilla aSOI_insect_set()/aSOG_gyoei_set() decision functions). Never carries a raw pointer,
 * internal actor structure, AI state, animation state, or camera state -- only the minimum record
 * pc_wildlife_authority.h's PcWildlifeRecord itself holds. Wildlife Sync T1: the client-side handler
 * (pcnetgame_handle_client_wildlife_spawn()) now materializes a real local vanilla fish/bug actor
 * from this data via pcwld_presentation_create() (pc_wildlife_authority.h/.c) -- see that function's
 * own doc for validation/duplicate-suppression/deferred-species (ants) details. Catching, a full
 * late-join/reconnect snapshot, tournaments, and bee/ant/latent-bug special-case networking remain
 * explicitly out of scope (later Wildlife Sync stages). */
typedef struct PCNetGameWildlifeSpawnMsg {
    uint8_t  msg_type; /* PC_NETGAME_MSG_WILDLIFE_SPAWN */
    uint8_t  kind;     /* PC_WILDLIFE_KIND_* (pc_wildlife_authority.h) */
    uint8_t  bx;
    uint8_t  bz;
    uint32_t entity_id;
    int32_t  species;  /* the vanilla gyo_type/insect_type enum value, verbatim */
    float    pos_x;
    float    pos_y;
    float    pos_z;
} PCNetGameWildlifeSpawnMsg;
_Static_assert(sizeof(PCNetGameWildlifeSpawnMsg) == 24, "PCNetGameWildlifeSpawnMsg wire size drifted");
_Static_assert(sizeof(PCNetGameWildlifeSpawnMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameWildlifeSpawnMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* ================================================================================================
 * World Ecology Wildlife Sync T2: late-join/reconnect snapshot (SNAPSHOT_BEGIN/.../SNAPSHOT_END
 * pump, snap_stage 3 -- see pcnetgame_host_pump_snapshots()'s own doc). NOT a new independent packet
 * flow: nested inside the existing outer snapshot exactly like VILLAGER_SNAPSHOT/
 * FRIENDSHIP_SNAPSHOT_ENTRY already are. `epoch` on every one of these three messages is the SAME
 * outer PCNetGameSnapshotBeginMsg.epoch this peer's whole snapshot pass is using -- staleness/
 * supersession (a peer's snapshot restarting mid-pump, e.g. from a large field change or a dropped
 * send) is therefore already fully covered by the EXISTING epoch-match rule FIELD_BLOCK itself uses
 * (pcnetgame_handle_client_field_block()), reused verbatim rather than inventing a second, wildlife-
 * specific staleness mechanism (see the milestone brief's own explicit guidance on this point).
 * ================================================================================================ */
/* host -> one client, reliable, 16 bytes. Opens one wildlife-snapshot sub-pass. `generation` is the
 * host's pcwld_session_generation() at pass-start (pc_wildlife_authority.h) -- the signal a client
 * uses to tell "the SAME authoritative wildlife session I already have local bookkeeping for" (any
 * generation it already saw once) apart from "a NEW session began since I last saw one" (0 -- never
 * seen one yet -- or a DIFFERENT value than last time): see pcnetgame_handle_client_wildlife_
 * snapshot_begin()'s own doc for exactly what each case does. `count` is purely an advance-notice/
 * diagnostic aid (how many WILDLIFE_SNAPSHOT_ENTRY messages will follow before END), mirroring
 * PCNetGameSnapshotBeginMsg's own acre_count field's role for FIELD_BLOCK -- never itself load-bearing
 * for correctness (the client's actual "snapshot complete" signal is WILDLIFE_SNAPSHOT_END, not a
 * count reaching zero). */
typedef struct PCNetGameWildlifeSnapshotBeginMsg {
    uint8_t  msg_type; /* PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_BEGIN */
    uint8_t  _reserved0[3];
    uint32_t epoch;
    uint32_t generation;
    uint32_t count;
} PCNetGameWildlifeSnapshotBeginMsg;
_Static_assert(sizeof(PCNetGameWildlifeSnapshotBeginMsg) == 16,
               "PCNetGameWildlifeSnapshotBeginMsg wire size drifted");
_Static_assert(sizeof(PCNetGameWildlifeSnapshotBeginMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameWildlifeSnapshotBeginMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would "
               "drop it");

/* host -> one client, reliable, 28 bytes. One currently-live authoritative wildlife record -- the
 * EXACT SAME minimum field shape as PCNetGameWildlifeSpawnMsg above (entity_id/kind/species/bx,bz/
 * position; never a raw pointer, actor address, local animation/flee-timer state, or camera state --
 * see that struct's own doc), with `epoch` appended for the staleness rule described above. Sent once
 * per currently-active pc_wildlife_authority.c table slot, flat-iterated over the WHOLE table (every
 * acre unconditionally -- see pcnetgame_build_wildlife_snapshot_entry()'s own doc), mirroring
 * pcnetgame_build_friendship_snapshot_entry()'s own flat-iteration-with-skip convention exactly. The
 * receiving client always materializes a real local actor for every one of these via the SAME
 * pcwld_presentation_create() an ordinary WILDLIFE_SPAWN already uses (T1) -- this milestone does NOT
 * add acre-relevance filtering on the client side, deliberately matching T1's own existing
 * unconditional-creation behavior rather than inventing a new client-side scoping rule. */
typedef struct PCNetGameWildlifeSnapshotEntryMsg {
    uint8_t  msg_type; /* PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_ENTRY */
    uint8_t  kind;     /* PC_WILDLIFE_KIND_* (pc_wildlife_authority.h) */
    uint8_t  bx;
    uint8_t  bz;
    uint32_t entity_id;
    int32_t  species;  /* the vanilla gyo_type/insect_type enum value, verbatim */
    float    pos_x;
    float    pos_y;
    float    pos_z;
    uint32_t epoch;
} PCNetGameWildlifeSnapshotEntryMsg;
_Static_assert(sizeof(PCNetGameWildlifeSnapshotEntryMsg) == 28,
               "PCNetGameWildlifeSnapshotEntryMsg wire size drifted");
_Static_assert(sizeof(PCNetGameWildlifeSnapshotEntryMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameWildlifeSnapshotEntryMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would "
               "drop it");

/* host -> one client, reliable, 16 bytes. Closes one WILDLIFE_SNAPSHOT_BEGIN/ENTRY* run.
 * `count_sent` is the actual number of WILDLIFE_SNAPSHOT_ENTRY messages this pass sent (cross-checked
 * only for logging/diagnostics against BEGIN's advance-notice `count` -- see that struct's own doc;
 * the two are expected to always match since nothing else can change the authoritative table
 * mid-pump, but a mismatch is not treated as fatal, only logged, matching this codebase's general
 * "log and continue" posture for a diagnostic-only cross-check). Receiving this is the client's
 * trigger to run pcwld_presentation_reconcile() (pc_wildlife_authority.h/.c) -- see
 * pcnetgame_handle_client_wildlife_snapshot_end()'s own doc. */
typedef struct PCNetGameWildlifeSnapshotEndMsg {
    uint8_t  msg_type; /* PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_END */
    uint8_t  _reserved0[3];
    uint32_t epoch;
    uint32_t generation;
    uint32_t count_sent;
} PCNetGameWildlifeSnapshotEndMsg;
_Static_assert(sizeof(PCNetGameWildlifeSnapshotEndMsg) == 16,
               "PCNetGameWildlifeSnapshotEndMsg wire size drifted");
_Static_assert(sizeof(PCNetGameWildlifeSnapshotEndMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameWildlifeSnapshotEndMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would "
               "drop it");

/* ============================================================================================
 * World Ecology Wildlife Sync T-catch (ordinary fish catching only -- see this milestone's own
 * ABSOLUTE SCOPE LIMIT: no bug catching, no tournament sync, no ant/bee special-case handling, no
 * general despawn policy beyond what catching itself needs).
 * ============================================================================================ */

/* client -> host only, reliable, 20 bytes. Claims a catch on an authoritative wildlife entity_id --
 * see pc_net_game_request_catch_fish()'s own doc (pc_net_game.h) for the full client-side seam this is
 * sent from (Player_actor_setup_main_Notice_rod(), m_player_main_notice_rod.c_inc). Deliberately NOT a
 * FIELD_ACTION kind (there is no tile involved) -- its own dedicated request/result pair, mirroring
 * SNOWMAN_BUILD_REQUEST/RESULT's own "not a FIELD_ACTION kind" precedent (single round trip, no
 * INTERACT_CONFIRM phase: the host atomically removes the entity from its table before it ever answers,
 * so there is no reservation window that a CONFIRM would need to commit or release -- see
 * pcnetgame_handle_host_catch_request()'s own doc for the race-safety argument).
 *   entity_id        the authoritative wildlife entity being claimed (pc_wildlife_authority.h). 0 is
 *                    never valid and is rejected outright.
 *   generation       this client's own last-known authoritative wildlife session generation
 *                    (pcwld_session_generation()) at send time -- rejected if it no longer matches the
 *                    host's CURRENT generation (closes the "stale entity_id reused by a NEW session's
 *                    table slot" gap: entity_id values are only unique WITHIN one generation).
 *   claimed_species  TRUST BOUNDARY -- this requester's own uki->gyo_type (the BOBBER's own settled
 *                    species, aGYO_TYPE_* domain, already reflecting any trash substitution roll).
 *                    Cross-checked against the authoritative record's own species by
 *                    pcwld_fish_species_matches_claim() (pc_wildlife_authority.h/.c) -- an exact match,
 *                    the vanilla SALMON2->SALMON conversion, or any recognized trash item are all
 *                    accepted; anything else is rejected. Never used to look up or derive the actual
 *                    granted ITEM id -- see PCNetGameCatchResultMsg's own doc for why. */
typedef struct PCNetGameCatchRequestMsg {
    uint8_t  msg_type;   /* PC_NETGAME_MSG_CATCH_REQUEST */
    uint8_t  _reserved0[3];
    uint32_t entity_id;
    uint32_t generation;
    uint32_t request_id;
    int32_t  claimed_species;
} PCNetGameCatchRequestMsg;
_Static_assert(sizeof(PCNetGameCatchRequestMsg) == 20, "PCNetGameCatchRequestMsg wire size drifted");
_Static_assert(sizeof(PCNetGameCatchRequestMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameCatchRequestMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* host -> the one requesting client only, reliable, 12 bytes.
 *   accepted      1: the host already atomically removed entity_id from its authoritative table and
 *                 broadcast WILDLIFE_DESPAWN (see pcnetgame_handle_host_catch_request()'s own doc) --
 *                 the requester must now grant the item, exactly like DIG_SHINE/DIG_HOLE's own
 *                 client-rolled bonus grant (see pc_net_game_request_catch_fish()'s own doc for why the
 *                 grant is applied from the CLIENT's own locally-computed item, not from this message).
 *                 0: rejected -- nothing was mutated host-side; no item is ever granted.
 *   granted_item  DIAGNOSTIC ECHO ONLY on accept (the requester's own claimed local item, echoed back
 *                 for logging/cross-check symmetry with BURY_RESULT's buried_item echo) -- NEVER the
 *                 authoritative source of the grant. The requester always applies its OWN locally
 *                 remembered item (computed once, at request time, from uki->get_fish_type_proc()) so
 *                 the host never needs to duplicate aUKI_get_fish_type()'s fish_data[]/trash table. 0
 *                 on reject. */
typedef struct PCNetGameCatchResultMsg {
    uint8_t  msg_type;     /* PC_NETGAME_MSG_CATCH_RESULT */
    uint8_t  accepted;
    uint16_t granted_item;
    uint32_t entity_id;
    uint32_t request_id;
} PCNetGameCatchResultMsg;
_Static_assert(sizeof(PCNetGameCatchResultMsg) == 12, "PCNetGameCatchResultMsg wire size drifted");
_Static_assert(sizeof(PCNetGameCatchResultMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameCatchResultMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* host -> every READY client, reliable, 8 bytes. Sent immediately after an accepted CATCH_REQUEST (or
 * an accepted host-local catch, pc_net_game_host_local_wildlife_catch()) removes entity_id from the
 * authoritative table -- see pcnetgame_commit_catch_despawn()'s own doc. Each receiver (including the
 * host's own local bookkeeping) reconciles its own local presentation actor, if any, via
 * pcwld_handle_wildlife_despawn() (pc_wildlife_authority.h/.c) -- see that function's own 3-case doc. */
typedef struct PCNetGameWildlifeDespawnMsg {
    uint8_t  msg_type; /* PC_NETGAME_MSG_WILDLIFE_DESPAWN */
    uint8_t  _reserved0[3];
    uint32_t entity_id;
} PCNetGameWildlifeDespawnMsg;
_Static_assert(sizeof(PCNetGameWildlifeDespawnMsg) == 8, "PCNetGameWildlifeDespawnMsg wire size drifted");
_Static_assert(sizeof(PCNetGameWildlifeDespawnMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameWildlifeDespawnMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* M9-A (protocol v5): PLAYER_SCENE, id 44, exactly 12 bytes, RELIABLE (a scene change is rare and must
 * never be lost or it would leave a peer permanently mis-located; identity deliberately does NOT ride
 * the 20 Hz unreliable MOVE stream).
 *   sender   client -> host (own scene), host -> client (relay of a peer's scene, the host's own scene,
 *            the join-time replay, or a CLEARED notice).
 *   receiver host (validates: sender READY, exact size, announceable scene_id, flags only IN_TOWN, seq
 *            strictly newer than the last accepted seq of that sender; overwrites net_player_id from the
 *            transport peer slot -- a client-claimed id is NEVER trusted) / client (applies if READY).
 *   scene_id raw enum scene_table value (play->scene_id of the sender's live GAME_PLAY).
 *   flags    PC_NETGAME_SCENE_FLAG_IN_TOWN, or (host -> client only) PC_NETGAME_SCENE_FLAG_CLEARED, which
 *            drops the named player's scene and bypasses the seq check (scene_id/owner/seq ignored).
 *   owner    house_owner_name for SCENE_NPC_HOUSE / player rooms, else 0 (host forces 0 otherwise).
 *   seq      the SENDER's own location sequence: starts at 1 per session (a reconnect re-announces with a
 *            freshly incremented value), compared only against earlier messages of the SAME sender; a
 *            plain `>` is sufficient (see PCNetMoveMsg's wraparound note). The ground truth is always the
 *            sender's own live game; the host only checks what is cheaply knowable. */
typedef struct PCNetGamePlayerSceneMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_PLAYER_SCENE */
    uint8_t  net_player_id; /* ignored client -> host; host -> client: originating player id */
    uint8_t  scene_id;
    uint8_t  flags;         /* PC_NETGAME_SCENE_FLAG_* */
    uint16_t owner;
    uint16_t _reserved0;
    uint32_t seq;
} PCNetGamePlayerSceneMsg;
_Static_assert(sizeof(PCNetGamePlayerSceneMsg) == 12, "PCNetGamePlayerSceneMsg wire size drifted");
_Static_assert(sizeof(PCNetGamePlayerSceneMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGamePlayerSceneMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");
_Static_assert(SCENE_NUM <= 255, "PCNetGamePlayerSceneMsg.scene_id is a u8: SCENE_NUM must fit");

/* M9-C: client -> host, reliable, 8 bytes (little-endian, natural alignment; like every other message here).
 * `slot` is the Save_t.animals[] index, `npc_id` the Animal_c.id.npc_id identity guard (same pairing as
 * NPC_MOVE/NPC_STATE), flags bit0: 1 = begin talking, 0 = end. `seq` is the sender's own u16 sequence (starts
 * at 1 per session, incremented per message, begin and end share it); the host accepts a message only when
 * it is NEWER than the last one seen from that peer, by u16 serial-number arithmetic ((int16_t)(seq-last) > 0,
 * wrap-safe), so a stale/duplicated message can never resurrect an old hold. */
#define PC_NETGAME_NPC_TALK_FLAG_BEGIN 0x01u
typedef struct PCNetGameNpcTalkMsg {
    uint8_t  msg_type; /* PC_NETGAME_MSG_NPC_TALK */
    uint8_t  slot;
    uint8_t  flags;
    uint8_t  _reserved0;
    uint16_t npc_id;
    uint16_t seq;
} PCNetGameNpcTalkMsg;
_Static_assert(sizeof(PCNetGameNpcTalkMsg) == 8, "PCNetGameNpcTalkMsg wire size drifted");
_Static_assert(sizeof(PCNetGameNpcTalkMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGameNpcTalkMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

/* M9-C (protocol v7): PLAYER_ACTION, id 46, exactly 10 bytes, RELIABLE, host -> client ONLY. PRESENTATION ONLY: the
 * receiver never changes the field, inventory or any other state from it (the FIELD_UPDATE / host-authoritative
 * pickup path is untouched and independent). Emitted by the host exactly where it already KNOWS the answer:
 *   - a client's pickup, at the host's COMMIT (pcnetgame_handle_host_confirm: after the INTERACT_CONFIRM COMMIT was
 *     validated and the tile cleared; an ABORTed/rejected/expired pickup therefore never produces one);
 *   - the host's own pickup (pc_net_game_notify_local_field_pickup), item taken from the committed shadow value of the
 *     tile (the pre-pickup content).
 * The host sends it to every READY client EXCEPT the originating peer (that process already plays its own vanilla
 * visuals) and presents it to its OWN puppet of a client origin through the same receiver function (no network loop).
 *   net_player_id  the TRUE originating player id (a READY peer id, or PC_NETGAME_HOST_PLAYER_ID); host-assigned
 *   kind           PC_NETGAME_PLAYER_ACTION_KIND_*; unknown kinds are ignored (reserved for DIG/FILL/...)
 *   flags          reserved, must be 0 (a receiver ignores a message with unknown bits)
 *   ut_x, ut_z     the town unit (tile) the item was on (same u8 width as PICKUP_REQUEST / the host records)
 *   item           the raw ground item id that was picked up (what the vanilla flying item shows); non-zero
 *   seq            host-assigned per-ORIGIN u16 sequence, monotonic for the host session (NOT reset when a peer
 *                  reconnects: receivers keep their per-origin last value until their own session resets); a client
 *                  accepts only (int16_t)(seq - last) > 0 (wrap-safe), so a duplicate/stale one is dropped. */
#define PC_NETGAME_PLAYER_ACTION_KIND_PICKUP 1u
typedef struct PCNetGamePlayerActionMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_PLAYER_ACTION */
    uint8_t  net_player_id; /* originating player id (host-assigned) */
    uint8_t  kind;
    uint8_t  flags;         /* reserved 0 */
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint16_t item;
    uint16_t seq;
} PCNetGamePlayerActionMsg;
_Static_assert(sizeof(PCNetGamePlayerActionMsg) == 10, "PCNetGamePlayerActionMsg wire size drifted");
_Static_assert(offsetof(PCNetGamePlayerActionMsg, kind) == 2 && offsetof(PCNetGamePlayerActionMsg, ut_x) == 4 &&
                   offsetof(PCNetGamePlayerActionMsg, item) == 6 && offsetof(PCNetGamePlayerActionMsg, seq) == 8,
               "PCNetGamePlayerActionMsg field offsets drifted");
_Static_assert(sizeof(PCNetGamePlayerActionMsg) <= PC_NET_MAX_PAYLOAD,
               "PCNetGamePlayerActionMsg exceeds PC_NET_MAX_PAYLOAD (pc_net.h) -- pc_net would drop it");

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
#define PC_NETGAME_INTERACT_KIND_BURY   3u /* World Ecology T3 -- see PCNetGameBuryRequestMsg's doc. The
                                            * generic INTERACT_CONFIRM dispatch
                                            * (pcnetgame_handle_host_confirm()) still finds this kind's
                                            * PENDING record via pcnetgame_host_interaction() exactly
                                            * like PICKUP/DROP, but COMMIT for this kind branches into
                                            * its own bury-specific outcome resolution/tile write instead
                                            * of the generic pickup/drop logic -- see that function's own
                                            * BURY branch. */
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

/* World Ecology T1: tree shake/chop's own reach bound -- deliberately a SEPARATE constant/check from
 * pickup's PC_NETGAME_PICKUP_MAX_REACH_SQ (see pcnetgame_tree_reach_check(), further down, which is a
 * full standalone duplicate of pcnetgame_field_action_reach_check()'s math, not a refactor of it, so
 * DIG_BURIED/MONEY_ROCK_HIT's existing reach check is left completely untouched -- this milestone runs
 * concurrently with other workstreams touching this same file). Vanilla's own tree collision
 * (mCoBG_CheckPlace() against a full-size tree's hit volume) keeps a player's ROOT position noticeably
 * farther from a trunk's own center than pickup's ~50-unit envelope already assumes for a hand-reach
 * pickup -- 80 is a deliberately generous starting bound with the same anti-cheat intent (catch "claims
 * to be on the other side of the map"), not a pixel-accurate reach model; see this task's own design
 * brief for the reasoning and the note that it should be tuned via a focused test if one becomes
 * practical in this sandbox. */
#define PC_NETGAME_TREE_REACH_SQ (80.0f * 80.0f)

/* World Ecology Wildlife Sync T-catch: fish-catch reach bound -- another deliberately SEPARATE
 * constant/check (pcnetgame_fish_catch_reach_check(), further down), never reused from pickup/tree.
 * Unlike a tile-anchored dig/bury/pickup/tree target, a fish is a DYNAMIC actor the authoritative record
 * only ever pins to its ORIGINAL spawn-decision position (PcWildlifeRecord.pos_x/z, pc_wildlife_
 * authority.h) -- it can swim, and a cast line can legitimately be flicked well away from the player's
 * own standing position before the bite happens. 800 units (10x pickup's ~50-unit envelope) is a
 * deliberately generous bound reflecting that dynamic range while still catching a genuinely
 * implausible claim (e.g. "caught" a fish on the other side of the map) -- not a tuned, pixel-accurate
 * casting-range model. XZ-ONLY, no Y check: PcWildlifeRecord.pos_y is ALWAYS 0.0 in this milestone (see
 * that struct's own doc -- neither aSOG_gyoei_set() nor aGYO_make_gyoei() ever touch it), so comparing
 * against the requester's own real (non-zero) Y would incorrectly reject a legitimate catch on elevated
 * terrain or at a waterfall. */
#define PC_NETGAME_FISH_CATCH_REACH_SQ (800.0f * 800.0f)

/* M9-D F4 (supersedes the original 150-unit bound described below): the host compares the claiming
 * client's position against the bug's SPAWN position (PcWildlifeRecord.pos_x/z, set once; wildlife AI
 * runs per process and nothing streams the bug's current position), but a bug moves after spawning.
 * Verified in source: a butterfly (ac_ins_chou.c) wanders between flowers and flees the local player
 * (aICH_avoid_player) while steering back toward its acre centre once outside the 1..14 unit ring
 * (aICH_avoid_move_ctrl), so it can end up anywhere in its acre: at most one acre diagonal from its
 * spawn, 16 units * 40 * sqrt(2) ~= 905. A banded dragonfly roams up to 12 units = 480 from its home
 * point (ac_ins_tonbo.c aITB_ONIYAMA_MAX_RANGE), other dragonflies 6 units = 240. Add the net/catch
 * envelope below (~74-84) and the worst case is ~990, so the bound is 1000 units. It stays an
 * anti-teleport sanity bound (a claim from outside the acre-scale envelope is still rejected), same
 * shape as the fish bound (800). The original rationale follows for the geometry facts. */
/* World Ecology Wildlife Sync T4 (ordinary bug catching): a SEPARATE reach bound from fish's. The real vanilla
 * geometry (verified in source): the net swing's own forward sweep is 50 units (60 for the gold net,
 * Player_actor_CheckCapture_forNet(), m_player_main_swing_net.c_inc), and a bug's own catch-registration
 * radius around itself is 8 units ordinarily, up to 24 for the ground-pool species that use aINS_get_
 * catch_range_sub() (beetles, a grounded cockroach) -- ac_insect_move.c_inc. (The ORIGINAL 150-unit bound
 * was a deliberately generous bound on top of that combined ~74-84 unit envelope (net sweep + catch radius), leaving slack
 * for the bug's own small movement between the local player's last reported position sample and the
 * moment of the swing, and for ordinary network position latency -- while still rejecting a genuinely
 * implausible claim (e.g. "caught" a bug on the other side of the acre; it ignored that the bug itself moves.) XZ-only, same reasoning as
 * PC_NETGAME_FISH_CATCH_REACH_SQ's own doc (PcWildlifeRecord.pos_y is always 0.0 for a bug record too --
 * neither aSOI_insect_set() nor aINS_make_insect() ever touch it). */
#define PC_NETGAME_BUG_CATCH_REACH_SQ (1000.0f * 1000.0f)

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

/* N2 villager movement sync tuning constants (named so they're easy to find and retune later, same
 * convention as the Stage 3 player-movement constants above -- see pc_net_game.h's design note above
 * pc_net_game_notify_npc_move() for the full rationale).
 *
 * Send rate: matches the player stream exactly (PC_NETGAME_MOVE_SEND_RATE_HZ) -- no reason for
 * villager walking, which is slower and smoother than player dashing, to need a HIGHER rate, and
 * matching it keeps one throttle mental model for the whole file.
 *
 * Interpolation delay: shorter than the player's PC_REMOTE_PLAYER_INTERP_DELAY_FRAMES (9.0, ~150ms,
 * pc_remote_player.c) because a villager's presentation has much lower stakes than another player's
 * avatar (no interactive collision with the local player driven by this position, no aim/reach
 * checks) -- 6 frames (~100ms) still comfortably covers one full send period (3 frames at 20Hz) plus
 * slack for jitter, while shaving visible lag off what is, after all, a background character.
 *
 * Teleport threshold: PC_REMOTE_PLAYER_TELEPORT_DIST_SQ (400^2) is sized for the player's dash speed;
 * a villager's fastest real gait (aNPC_spd_data's RUN entry) is far slower, so one real 1/20s tick of
 * villager movement is a much shorter hop than for a dashing player. 150 world units is roughly
 * triple a plausible single-tick run-speed displacement -- generous slack against jitter/latency
 * spikes while still catching the actual discontinuities N2 needs to snap across (see Part 8: an
 * acre/scene/house-entry teleport, or the off-screen-simulation handoff described in
 * PCNetGameNpcMoveMsg's own doc comment above). */
#define PC_NETGAME_NPC_MOVE_SEND_RATE_HZ 20.0f
#define PC_NETGAME_NPC_MOVE_SEND_PERIOD_60FPS_FRAMES (60.0f / PC_NETGAME_NPC_MOVE_SEND_RATE_HZ)
#define PC_NETGAME_NPC_MOVE_INTERP_DELAY_FRAMES 6.0
#define PC_NETGAME_NPC_MOVE_TELEPORT_DIST_SQ (150.0f * 150.0f)
#define PC_NETGAME_NPC_MOVE_RING_SIZE 4
/* Below this squared per-straddling-pair displacement, treat the villager as stationary for
 * walk-vs-idle animation purposes -- small enough to never mistake real walking for idle, large
 * enough to absorb float noise/near-zero jitter from a villager that is genuinely standing still. */
#define PC_NETGAME_NPC_MOVE_MOVING_DIST_SQ (0.25f * 0.25f)

/* One accepted movement sample for one villager slot, timestamped in THIS process's own
 * graph_dt_frame_time() domain at the moment it was accepted -- exactly PCRemoteMoveSnapshot's own
 * convention (pc_remote_player.c), reused here rather than duplicating a third representation. */
typedef struct PCNetNpcMoveSnapshot {
    double  recv_local_frame;
    float   pos_x, pos_y, pos_z;
    int16_t facing_angle;
} PCNetNpcMoveSnapshot;

/* Per-Save_t.animals[]-slot receive-side state (client only; the host never populates this). */
typedef struct PCNetNpcMoveSlot {
    uint16_t cached_npc_id;       /* npc_id carried by the most recently ACCEPTED sample for this
                                    * slot -- pc_net_game_get_npc_move_pose()'s slot-reuse guard
                                    * compares the CALLER's live Save_t.animals[slot].id.npc_id
                                    * against this at consume time; see pc_net_game.h's doc. */
    uint32_t last_accepted_frame;
    int      have_frame;          /* 0 until the first sample for this slot ever arrives (frame 0 is
                                    * a legitimate value, so this can't be inferred from frame alone) */
    uint8_t  last_action_type;    /* N3 Channel A: latest accepted action_type for this slot -- coarse
                                    * and deliberately NOT interpolated between snapshots (an action
                                    * type has no "halfway" value); applied as-is the instant a new
                                    * sample is accepted. */
    int      snapshot_count;
    int      snapshot_head;
    PCNetNpcMoveSnapshot snapshots[PC_NETGAME_NPC_MOVE_RING_SIZE];
} PCNetNpcMoveSlot;

static PCNetNpcMoveSlot s_npc_move_slot[ANIMAL_NUM_MAX];       /* client-side receive/interpolation state */
static float            s_npc_move_send_accum[ANIMAL_NUM_MAX]; /* host-side per-slot send throttle */
static uint32_t         s_npc_move_send_counter = 0;           /* host-side: this process's own
                                                                 * per-send counter, shared across every
                                                                 * slot -- see PCNetGameNpcMoveMsg.frame's
                                                                 * doc comment above */

/* N3 Channel B: host-side per-slot shadow of the last BROADCAST is_home/hide/forced_type/
 * forced_active, used by pc_net_game_notify_npc_state()'s dirty-check (see its own doc comment for
 * why forced_timer's continuous countdown is tracked as forced_active -- active/inactive -- rather
 * than by exact value, to avoid broadcasting every single frame). have_sent is 0 until the first
 * check for a slot (so the very first observed state, even if it happens to equal the struct's
 * zero-init, is still sent once). */
/* N3 finalization pass (Bug 2 fix): the shadow used to be keyed by slot ONLY, with no record of which
 * npc_id it represented. That meant: villager A (slot N) broadcasts state X, departs, and villager B
 * (slot N) arrives with state that happens to equal X byte-for-byte -- the dirty-check below saw "no
 * change" and silently never broadcast B's initial state, leaving the client stuck on stale/default
 * values until some later real change. cached_npc_id fixes this: a sample whose npc_id doesn't match
 * the shadow's stored npc_id is unconditionally treated as dirty regardless of value equality. The
 * shadow is also explicitly reset (have_sent=0) at every population-changing event -- see
 * pc_net_game_notify_villager_arrival()/_departure() -- and at full session reset (already existing,
 * see pc_net_game_host_start()). */
typedef struct PCNetNpcStateShadow {
    int      have_sent;
    uint16_t cached_npc_id;
    uint8_t  is_home;
    uint8_t  hide;
    uint8_t  forced_type;
    uint8_t  forced_active; /* forced_timer_remaining > 0 */
} PCNetNpcStateShadow;
static PCNetNpcStateShadow s_npc_state_shadow[ANIMAL_NUM_MAX]; /* host-side only */
static uint32_t            s_npc_state_seq_counter = 0;        /* host-side: global monotonic, shared
                                                                 * across every slot (see
                                                                 * PCNetGameNpcStateMsg.state_seq's doc) */

/* Client-side: latest APPLIED NPC_STATE per slot, plus the per-slot last-applied state_seq for the
 * strict-`>` staleness/slot-reuse guard (see pc_net_game_get_npc_move_pose()'s own identity-guard
 * doc comment for the established pattern this mirrors -- npc_id is re-checked at CONSUME time
 * against the caller's own live Save_t.animals[slot].id.npc_id, never trusted from receive time
 * alone). */
typedef struct PCNetNpcStateSlot {
    int      have_state;
    uint16_t cached_npc_id;
    uint32_t last_applied_seq;
    uint8_t  is_home;
    uint8_t  hide;
    uint8_t  forced_type;
    uint16_t forced_timer_remaining;
} PCNetNpcStateSlot;
static PCNetNpcStateSlot s_npc_state_slot[ANIMAL_NUM_MAX]; /* client-side receive state */

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
    uint16_t item;              /* pickup: resolved granted item; drop: item to place; bury: the
                                    requester's claimed_item (the item being buried) */
    uint16_t raw_item;          /* pickup: the raw field value observed at reservation; drop: unused;
                                    bury: the target tile's raw value observed AT RESERVATION time (a
                                    HOLE00..24 or HOLE_SHINE value) -- re-checked unchanged at COMMIT */
    uint8_t  phase;             /* PCNetGameHostPhase */
    uint8_t  accepted;          /* the cached provisional/final decision (1 = accepted) */
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint8_t  acre;              /* persistent address of the reserved tile (meaningful when accepted) */
    uint8_t  tile;
    uint8_t  hole_variant;      /* World Ecology T3: bury only -- the requester's own claimed
                                    hole_variant (0..24, or 0xFF), captured at reservation time and
                                    consumed at COMMIT for the PITFALL-into-HOLE_SHINE sub-case (see
                                    PCNetGameBuryRequestMsg's doc). Memory-only: never echoed back on
                                    the wire except via the general reconciliation mechanism. Always 0
                                    for PICKUP/DROP records. */
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

/* World Ecology T3: host-only BURY record per peer -- same type, phases and reasoning as
 * s_host_pickup_state/s_host_drop_state above (see PCNetGameHostInteraction's doc), kept in its own
 * array for the same "don't conflate different kinds' retries" reason: a peer may have a pickup, a
 * drop, AND a bury pending simultaneously, fully independently. item/raw_item/hole_variant are set by
 * pcnetgame_handle_host_bury_request() at reservation time and consumed by the BURY branch of
 * pcnetgame_handle_host_confirm() at COMMIT. Reset on disconnect and defensively on fresh READY via
 * pcnetgame_reset_host_bury_state(), exactly mirroring the pickup/drop precedent. */
static PCNetGameHostInteraction s_host_bury_state[PC_NET_MAX_PEERS];

/* World Ecology milestone (Stage 1): host-only, per-peer dedup for the last PROCESSED
 * FIELD_ACTION_REQUEST (both kinds share one slot per peer -- a peer only ever has one field action
 * animation/interaction active client-side at a time, exactly like s_host_pickup_state's own single-
 * slot reasoning). Unlike PICKUP/DROP there is no PENDING phase (see
 * PCNetGameFieldActionRequestMsg's doc) -- this exists purely so a retried request_id (the sender's
 * own reliable-window retry, or a duplicate delivery) replays the SAME already-decided outcome
 * instead of re-validating (and, for MONEY_ROCK_HIT, re-incrementing hit_count) a second time. */
typedef struct PCNetGameFieldActionDedup {
    int      valid;
    uint32_t request_id;
    uint8_t  kind;
    uint8_t  accepted;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint16_t granted_item;
} PCNetGameFieldActionDedup;
static PCNetGameFieldActionDedup s_host_field_action_dedup[PC_NET_MAX_PEERS];

/* World Ecology: snowmen -- host-only, per-peer dedup for the last PROCESSED
 * SNOWMAN_BUILD_REQUEST, mirroring PCNetGameFieldActionDedup's own single-slot reasoning (BUILD is not
 * a FIELD_ACTION kind, so it needs its own dedup record rather than sharing that table). */
typedef struct PCNetGameSnowmanBuildDedup {
    int      valid;
    uint32_t request_id;
    uint8_t  accepted;
    uint8_t  slot;
    uint8_t  reason;
} PCNetGameSnowmanBuildDedup;
static PCNetGameSnowmanBuildDedup s_host_snowman_build_dedup[PC_NET_MAX_PEERS];

/* World Ecology Wildlife Sync T-catch -- host-only, per-peer dedup for the last PROCESSED
 * CATCH_REQUEST, mirroring PCNetGameSnowmanBuildDedup's own single-slot reasoning exactly (CATCH is not
 * a FIELD_ACTION kind either -- see PCNetGameCatchRequestMsg's own doc). A replayed request_id (the
 * client's own send failed and it never actually got the first RESULT, or the RESULT itself was lost)
 * answers from this cache -- it never re-validates or re-removes the entity a second time. */
typedef struct PCNetGameCatchDedup {
    int      valid;
    uint32_t request_id;
    uint8_t  accepted;
    uint32_t entity_id;
} PCNetGameCatchDedup;
static PCNetGameCatchDedup s_host_catch_dedup[PC_NET_MAX_PEERS];

/* World Ecology milestone (Stage 1, Item 2): host-only per-tile "money rock hit window" bookkeeping
 * -- deliberately separate from (and much simpler than) the vanilla bg_item_ten_coin_c runtime array
 * (bg_item.h) that drives the LOCAL wobble animation: the host needs none of that graphics/timing
 * state, only hit_count and an expiry, both purely deterministic (see
 * bIT_actor_ten_coin_entryR()'s reward switch, bg_item_common.c_inc -- hit_count/destiny_type only,
 * no RNG). Sized bIT_TEN_COIN_NUM (5) to match vanilla's own concurrent-money-rock limit. money_power
 * is captured from whichever request OPENED the window (first hit) and reused for every hit in that
 * window, matching vanilla's own "swing_time set once, at the first hit" behavior
 * (bIT_actor_ten_coin_entryR() only reads mPr_GetMoneyPower() in its mode!=2 branch); destiny_type is
 * re-read from the REQUESTING player's context on every hit, matching vanilla's own per-hit
 * Common_Get(now_private)->destiny.type read.
 *
 * Bug 4 fix: the dropped money bag itself carries NO special-case tracking any more (no drop_acre/
 * drop_tile/drop_valid/drop_pickup_seq, no pc_net_game_is_money_bag_pickup_allowed()). Once
 * pcfa_set_tile() writes the reward item onto its chosen tile, it is an entirely ORDINARY field item,
 * pickupable by anyone through the completely ordinary reserve/validate/commit pickup path
 * (pcnetgame_validate_and_resolve_pickup()) -- the same path every other dropped item already uses.
 * That also means it survives the window's own expiry, is covered by the ordinary dirty-flush/
 * snapshot/late-join mechanisms for free, and multiple simultaneous bags (from separate hits/windows)
 * are all independently trackable, because none of them are tracked here at all any more. */
typedef struct PCNetGameMoneyRockState {
    int      active;
    int      acre;
    int      tile;
    mActor_name_t orig_item;    /* the MONEY_ROCK_x/MONEY_FLOWER_SEED value at window-open, for the
                                  * eventual revert (orig_item - 7), mirroring
                                  * bIT_actor_ten_coin_move()'s own `ten_coin->fg_item - 7` exactly */
    int      hit_count;
    int16_t  money_power;       /* captured once, at window-open -- see doc above */
    float    expire_accum;      /* frames-as-60fps-units since window-open; see
                                  * PC_NETGAME_MONEY_ROCK_EXPIRE_60FPS_FRAMES-style per-window target
                                  * below (expire_target_frames) */
    float    expire_target_frames;
} PCNetGameMoneyRockState;
#define PC_NETGAME_MONEY_ROCK_SLOTS 5 /* == bIT_TEN_COIN_NUM (bg_item.h) */
static PCNetGameMoneyRockState s_host_money_rock[PC_NETGAME_MONEY_ROCK_SLOTS];

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

/* M9-D G4-2: client-only 'deferred exchange' record for the catch-exchange menu (mSM_IV_OPEN_EXCHANGE).
 * In that menu the item the player swaps OUT of a full pocket slot S ends up in the HAND, while the new
 * (caught) item already sits in pockets[S]. When the hand item is then put on the ground
 * (mTG_exchange_proc) a network client must not write it into its own local field (never synced, erased
 * at the next resync), so the hand item is sent through the ordinary authoritative drop request instead.
 * That request needs the hand item in a pocket, so pockets[S] temporarily holds it again and the item
 * that was in pockets[S] (the replacement) is parked here. If the host ACCEPTS the matching request,
 * pcnetgame_handle_client_drop_result() clears slot S as for any drop and then writes the replacement
 * into it (one write: the record is consumed on the first matching RESULT, accepted or not).
 * On reject / timeout / not sent / session reset / owner change / slot mismatch the pocket simply keeps
 * the hand item and the replacement is lost -- the same posture as the 5A fallback 'a client whose
 * pockets are full does not receive the item'. Matching is by request id only; a stale record can never
 * apply to another request. s_exchange_swap_* remembers the pocket slot the last hand swap wrote into
 * (set by mHD_drop_item2 via pc_net_game_exchange_note_swap, client role only). */
typedef struct PCNetGameExchangeDeferred {
    int      valid;
    uint32_t request_id;
    uint8_t  slot;
    uint16_t replacement;
    uint8_t  replacement_cond;
    PCNetGameOwnerStamp owner;
} PCNetGameExchangeDeferred;
static PCNetGameExchangeDeferred s_exchange_deferred;
static int                       s_exchange_swap_slot = -1;
static uint16_t                  s_exchange_swap_item;

#define PC_NETGAME_DROP_TIMEOUT_60FPS_FRAMES 30.0f /* ~500ms, mirrors pickup's own budget */
#define PC_NETGAME_DROP_MAX_RETRIES 30 /* see PC_NETGAME_PICKUP_MAX_RETRIES for the budget reasoning */
_Static_assert((PC_NETGAME_DROP_MAX_RETRIES + 1) * 500u < PC_NETGAME_CONFIRM_TIMEOUT_MS,
               "the host's reservation timeout must exceed the client's drop retry budget (30 x 500 ms + last interval)");

/* World Ecology T3: client-only. Exactly one outstanding bury request at a time -- exact structural
 * mirror of PCNetGameDropPending (see its own doc for the shared reasoning), plus hole_variant (this
 * client's own locally-computed mCoBG_GetHoleNumber() result, or 0xFF -- resent byte-for-byte on every
 * retry, same reasoning as claimed_item). Kept as a wholly separate instance from pickup/drop for the
 * same "don't conflate different kinds' retries" reason. */
typedef struct PCNetGameBuryPending {
    int      valid;
    uint32_t request_id;
    uint8_t  pocket_slot_idx;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint8_t  hole_variant;
    PCNetGameOwnerStamp owner; /* the local player at send time (see PCNetGameOwnerStamp) */
    uint16_t claimed_item;
    float    timeout_accum;
    int      retry_count;
    int      unsent;         /* 1: the FIRST send failed (window full) -- see PCNetGamePickupPending.unsent */
} PCNetGameBuryPending;
static PCNetGameBuryPending s_bury_pending;
static uint32_t             s_next_bury_request_id = 1;

/* M9-D G2-3: client-only. The bury request whose provisional accept was acted on (COMMIT sent, pocket
 * slot cleared) is RETAINED here, separate from s_bury_pending (which is released at the accept so no
 * retry can follow), until its fate is known. On a SUCCESSFUL commit the host sends BURY_RESULT
 * accepted=1 (same request_id) plus the ambient FIELD_UPDATE; the client then takes the accepted-but-
 * unmatched path and sends ABORT(STALE), which the host ignores (request phase DONE). That accepted=1
 * cannot be told apart from a replayed provisional accept, so the record is deliberately NOT cleared on
 * it (comment-only; the record simply times out). The host sends a BURY reject (accepted=0) carrying the
 * same request_id when the commit itself fails (tile rewritten / deposit set since the reservation, host
 * world not ready). Such a reject means the host did NOT bury the item, so it is restored into the
 * pocket (same slot if still empty, else a free slot, else logged as LOST). Discarded on: the matching
 * reject (restored), the next bury request, session reset, owner-stamp mismatch, or a safety timeout
 * (PC_NETGAME_BURY_COMMITTED_KEEP_60FPS_FRAMES) -- after which a stray reject is ignored as before. */
#define PC_NETGAME_BURY_COMMITTED_KEEP_60FPS_FRAMES 900.0f /* ~15 s: far above a reliable COMMIT round trip */
typedef struct PCNetGameBuryCommitted {
    int      valid;
    uint32_t request_id;
    uint8_t  pocket_slot_idx;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint16_t item;
    PCNetGameOwnerStamp owner;
    float    keep_accum;
} PCNetGameBuryCommitted;
static PCNetGameBuryCommitted s_bury_committed;

/* World Ecology Wildlife Sync T-catch: client-only. Exactly one outstanding CATCH request at a time
 * (a player can only ever be mid-reeling-in ONE fish, via ONE fishing rod) -- no timeout/retry fields at
 * all, deliberately: this is a fire-and-forget single round trip, mirroring
 * pc_net_game_request_snowman_build()'s own "dropped, not retried" shape on a send failure, NOT
 * pickup/drop/bury's own retry-queue shape (see pc_net_game_request_catch_fish()'s own doc for why a
 * retry would be actively harmful here: a resend after the fish has already moved on/been caught by
 * someone else has nothing useful to retry against). `local_grant` is THIS client's own
 * uki->get_fish_type_proc() result, computed ONCE at request time and applied on ACCEPT only -- see
 * PCNetGameCatchResultMsg's own doc for why the host never computes or re-derives this item.
 * `claimed_species` is kept only so the mSM_COLLECT_FISH_SET() collection-bit commit can be correctly
 * deferred to ACCEPT too (see pcnetgame_handle_client_catch_result()'s own doc). */
typedef struct PCNetGameCatchPending {
    int      valid;
    uint32_t request_id;
    uint32_t entity_id;
    int      kind; /* T4: PC_WILDLIFE_KIND_FISH or PC_WILDLIFE_KIND_BUG -- which collection-bit commit
                       (mSM_COLLECT_FISH_SET() vs mSM_COLLECT_INSECT_SET()) pcnetgame_handle_client_
                       catch_result() must run on accept; see that function's own doc. A player can only
                       ever be mid-reeling-in ONE fish OR mid-net-catching ONE bug at a time (never both --
                       the fishing rod and net are mutually exclusive player main-index states), so this
                       single shared struct's own "exactly one outstanding request" precedent (see this
                       struct's own top-of-file doc) already covers both kinds safely. */
    int      claimed_species;
    uint16_t local_grant;
    PCNetGameOwnerStamp owner; /* the local player at send time (see PCNetGameOwnerStamp) */
} PCNetGameCatchPending;
static PCNetGameCatchPending s_catch_pending;
static uint32_t              s_next_catch_request_id = 1;

/* World Ecology Wildlife Sync T-catch (residual review fix): the last RESOLVED catch decision for a
 * given entity_id, consumed by pc_net_game_query_catch_outcome() -- see that function's own doc
 * (pc_net_game.h). Populated by pcnetgame_handle_client_catch_result() (client: on CATCH_RESULT, or
 * immediately on a send failure in pc_net_game_request_catch_fish() -- a dropped request can never
 * produce a RESULT, so it is recorded as rejected right away instead of leaving the exchange screen's
 * gate stuck reading PENDING forever) and by pc_net_game_host_local_wildlife_catch() (host: recorded
 * synchronously, since that decision never leaves this process). Exactly one entity_id's outcome is
 * remembered at a time -- sufficient here for the exact same "a player can only ever be mid-reeling-in
 * ONE fish" reason PCNetGameCatchPending's own doc gives; a stale, superseded entry is simply
 * overwritten. */
typedef struct PCNetGameCatchOutcome {
    int      valid;
    uint32_t entity_id;
    int      accepted; /* 0 = rejected/no-grant, 1 = accepted/safe-to-grant */
} PCNetGameCatchOutcome;
static PCNetGameCatchOutcome s_catch_last_outcome;

#define PC_NETGAME_BURY_TIMEOUT_60FPS_FRAMES 30.0f /* ~500ms, mirrors pickup/drop's own budget */
#define PC_NETGAME_BURY_MAX_RETRIES 30 /* see PC_NETGAME_PICKUP_MAX_RETRIES for the budget reasoning */
_Static_assert((PC_NETGAME_BURY_MAX_RETRIES + 1) * 500u < PC_NETGAME_CONFIRM_TIMEOUT_MS,
               "the host's reservation timeout must exceed the client's bury retry budget (30 x 500 ms + last interval)");

/* World Ecology milestone (Stage 1 / T0-C): client-only. One entry describes one outstanding or
 * queued field-action request, SHARED across both existing kinds (DIG_BURIED / MONEY_ROCK_HIT) and any
 * future kind. No provisional/CONFIRM phase (see PCNetGameFieldActionRequestMsg's doc). */
typedef struct PCNetGameFieldActionPending {
    int      valid;
    uint8_t  kind;
    uint32_t request_id;
    uint8_t  ut_x;
    uint8_t  ut_z;
    uint8_t  hole_variant; /* protocol v3 -- see PCNetGameFieldActionRequestMsg's own doc; 0 for every
                               kind that doesn't use it */
    PCNetGameOwnerStamp owner; /* the local player at send time (see PCNetGameOwnerStamp) */
    float    timeout_accum;
    int      retry_count;
    int      unsent;
    uint16_t local_grant; /* World Ecology T-dig (D / A-2): a PRIVATE, client-rolled item id to grant to a
                              free pocket slot on ACCEPT only -- used by DIG_SHINE's own bell roll and
                              DIG_HOLE's golden-shovel ITM_MONEY_100 bonus, both of which the host never
                              rolls/knows about (see pc_net_game_request_dig_shine_with_grant()'s /
                              pc_net_game_request_dig_hole_with_grant()'s own doc). 0 means "no grant" --
                              every existing kind/caller leaves this zeroed via memset(). */
} PCNetGameFieldActionPending;

/* T0-C: was a single instance (s_field_action_pending) through Stage 1 -- a local player can only ever
 * be mid-dig or mid-swing for ONE interaction at once, so DIG_BURIED/MONEY_ROCK_HIT never legitimately
 * needed more than one in flight (mirrors s_pickup_pending's own single-slot reasoning), and
 * pc_net_game_request_dig_buried()/pc_net_game_request_money_rock_hit() still enforce exactly that by
 * refusing to enqueue while ANYTHING is already queued (see their own "one already in flight" guard) --
 * so for those two kinds this queue can still never hold more than 1 entry, byte-for-byte the same
 * behavior as before this change.
 * Widened into a small bounded FIFO (depth PC_NETGAME_FIELD_ACTION_QUEUE_DEPTH) because a FUTURE kind
 * (T1: tree chops in particular) will legitimately want several DISTINCT field actions in flight
 * client-side without dropping a request purely because one is already outstanding, the way the old
 * single-slot design forced every caller to. Compacted at index 0 (the head): index 0 is the ONLY
 * entry ever actually on the wire (see pcnetgame_field_action_queue_kick_head()) -- this is what keeps
 * the host's per-peer dedup (a single slot, request_id-keyed) correct: it still only ever needs to
 * remember one in-flight request per peer at a time, exactly as before. A later queued entry is sent
 * only once the one ahead of it is popped (its RESULT arrived, or it gave up after
 * PC_NETGAME_FIELD_ACTION_MAX_RETRIES) -- see pcnetgame_field_action_queue_pop_head(). */
#define PC_NETGAME_FIELD_ACTION_QUEUE_DEPTH 4
static PCNetGameFieldActionPending s_field_action_queue[PC_NETGAME_FIELD_ACTION_QUEUE_DEPTH];
static int                         s_field_action_queue_len = 0; /* occupied entries, compacted at [0..len-1] */
static uint32_t                    s_next_field_action_request_id = 1;

#define PC_NETGAME_FIELD_ACTION_TIMEOUT_60FPS_FRAMES 30.0f /* ~500ms, mirrors pickup's own budget */
#define PC_NETGAME_FIELD_ACTION_MAX_RETRIES 30 /* see PC_NETGAME_PICKUP_MAX_RETRIES for the budget reasoning.
    NOTE: unlike PICKUP/DROP there is no host-side reservation this must stay under -- this stage's
    field actions commit synchronously with no provisional phase (see
    PCNetGameFieldActionRequestMsg's own doc), so this budget only bounds how long a client waits
    before giving up on a send that keeps failing (reliable window full), not a host expiry. This
    budget applies only to the QUEUE HEAD (the one entry actually in flight on the wire) -- a queued
    entry behind it does not start its own timeout/retry clock until it becomes the head. */

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
    /* Friendship/mail sync milestone: this peer's own PersonalID_c fields, captured once at READY
     * from the same IDENTITY payload pc_remote_player_on_ready() already receives (see
     * pcnetgame_handle_host_identity()) -- retained for the lifetime of the connection (unlike
     * pending_identity, which is cleared right after the handshake) so a later FRIENDSHIP_REQUEST/
     * MAIL_REQUEST never needs the peer to re-assert who it is. */
    int                  ready_identity_valid;
    uint8_t              ready_player_name[PC_NETGAME_NAME_LEN];
    uint8_t              ready_land_name[PC_NETGAME_LAND_LEN];
    uint16_t             ready_player_id;
    uint16_t             ready_land_id;
    /* M9 identity Stage 1A: the HOST-DERIVED resident binding of this peer, set only at READY by
     * pcnetgame_host_process_identity() from the host's OWN Save_Get(private_data[]) (never from
     * anything the peer claims except the PersonalID it is matched with). bound_valid != 0 <=> the
     * peer is READY and bound_resident_idx (0..PLAYER_NUM-1) is the resident record it is bound to.
     * It is distinct from the transport slot (index into this array), assigned_peer_id (== slot),
     * the claimed PLAYER_CONTEXT player_no and the PersonalID claim. Cleared (bound_valid = 0,
     * bound_resident_idx = -1) by the same memset in pcnetgame_reset_all_host_peer_state() that
     * clears everything else, i.e. on connect, disconnect, reject, drop and timeout. */
    int                  bound_valid;
    int                  bound_resident_idx;
    PersonalID_c         bound_pid; /* the host's saved PersonalID of that resident AT BIND TIME (re-validation) */
    int                  ctx_player_no_warned; /* PLAYER_CONTEXT player_no mismatch warned once per connection */
    int                  ctx_clamp_logged;     /* PLAYER_CONTEXT clamp notice logged once per connection */
    /* snapshot progress */
    int                  snap_active;
    int                  snap_stage;      /* 0 = BEGIN next, 1 = FIELD_BLOCKs, 2 = VILLAGER_SNAPSHOT next,
                                            * 3 = WILDLIFE_SNAPSHOT_BEGIN/ENTRYs-then-END (World Ecology
                                            * Wildlife Sync T2: inserted between the villager population
                                            * snapshot and the friendship snapshot), 4 = FRIENDSHIP_
                                            * SNAPSHOT_ENTRYs (friendship/mail sync milestone: inserted
                                            * between the wildlife sub-pass and SNAPSHOT_END), 5 = END
                                            * next */
    int                  snap_next_acre;
    int                  snap_next_friendship_idx; /* flat 0..(ANIMAL_NUM_MAX*ANIMAL_MEMORY_NUM)-1 */
    int                  snap_blocks_sent;
    uint32_t             snap_epoch;
    /* World Ecology: snowmen -- one-shot PC_NETGAME_MSG_SNOWMAN_STATE sent right after SNAPSHOT_BEGIN
     * succeeds (stage 0 -> 1), before the first FIELD_BLOCK, so a late-joiner's SNOWMAN_STATE always
     * arrives before the FIELD_UPDATE/FIELD_BLOCK carrying the matching tile (see
     * pcnetgame_host_pump_snapshots()'s own doc). Reset to 0 by pcnetgame_host_start_snapshot() (a
     * fresh epoch always resends it) and by the whole-struct memset in
     * pcnetgame_reset_all_host_peer_state(). */
    int                  snap_snowman_sent;
    /* World Ecology Wildlife Sync T2: snap_stage 3 (WILDLIFE_SNAPSHOT_BEGIN/ENTRY-then-END, inserted
     * between VILLAGER_SNAPSHOT and FRIENDSHIP_SNAPSHOT_ENTRY -- see pcnetgame_host_pump_snapshots()'s
     * own doc). snap_wildlife_begin_sent gates the one-shot BEGIN (reset to 0 whenever this peer
     * transitions INTO stage 3, exactly like snap_snowman_sent's own reset-on-(re)entry shape);
     * snap_next_wildlife_idx is the flat 0..PCWLD_PUBLIC_MAX_ENTITIES-1 table-slot cursor (mirrors
     * snap_next_friendship_idx); snap_wildlife_sent_count is the running ENTRY count, used to fill
     * WILDLIFE_SNAPSHOT_END's diagnostic count_sent field. */
    int                  snap_wildlife_begin_sent;
    int                  snap_next_wildlife_idx;
    int                  snap_wildlife_sent_count;
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
/* N-clock milestone: host-side clock broadcast state. s_host_clock_seq is the ONE shared monotonic
 * counter for every CLOCK_SYNC this hosting session ever sends (initial per-peer sends at
 * SNAPSHOT_END, periodic broadcasts, and discontinuity broadcasts alike) -- a client's
 * strictly-greater-than check (s_client_clock_seq_applied) is what makes a stale/reordered/duplicate
 * reliable delivery a no-op, exactly like s_world_seq/s_client_meta_seq for WORLD_META. */
static uint32_t               s_host_clock_seq = 0;
static uint32_t               s_host_clock_sync_last_ms = 0; /* pcnetgame_now_ms() of the last periodic broadcast */
static OSTime                 s_host_clock_last_delta;       /* last-seen Save_Get(time_delta), to detect a manual clock adjust */
static int                    s_host_clock_last_delta_valid = 0;

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
/* Friendship/mail sync milestone: strictly-monotonic last-applied world_seq for
 * FRIENDSHIP_UPDATE/MAIL_DELIVERED (strict `>`) and FRIENDSHIP_SNAPSHOT_ENTRY (`>=`, only ever
 * advances it) -- same shared s_world_seq generation, same contract shape as
 * s_client_population_seq immediately above, just its own counter (a friendship/mail event and a
 * population event are unrelated deltas; comparing them against the same counter would make an
 * unrelated population change spuriously "supersede" a pending friendship/mail one or vice versa). */
static uint32_t              s_client_friendship_seq = 0;
/* N-clock milestone: last-applied host s_host_clock_seq (strict `>` rule, matching s_client_meta_seq/
 * s_client_population_seq/s_client_friendship_seq's own contract shape exactly). Reset to 0 on every
 * fresh connection/reconnect by pcnetgame_reset_client_session_state() below (Rule 2 of the design: a
 * reconnect is treated like a fresh join, so the first post-reconnect CLOCK_SYNC is always accepted).
 * The actual clock OFFSET this drives (pc_lb_rtc_set_net_clock_offset(), lb_rtc.c) is DELIBERATELY NOT
 * reset here and has no variable in this file at all -- it lives in lb_rtc.c precisely so a disconnect
 * never resets it to 0 and visibly jumps the displayed clock backward while the connection is down
 * (Rule 2). ROLE reverts on disconnect, so pc_net_game_world_is_host_authoritative() naturally goes
 * false and every host-gated consumer stops caring what the (now stale but frozen) offset says. */
static uint32_t              s_client_clock_seq_applied = 0;
/* World Ecology: snowmen -- this connection's last-applied SNOWMAN_STATE world_seq (its OWN sequence,
 * independent of s_client_meta_seq/s_world_seq -- see PCNetGameSnowmanStateMsg's own doc). Reset to 0
 * on (re)connect by pcnetgame_reset_client_session_state(), exactly like every other *_seq_applied /
 * *_seq tracker in this file, so a reconnect's first SNOWMAN_STATE is always accepted. */
static uint32_t              s_client_snowman_seq = 0;
static int                   s_client_snap_active = 0;
static uint32_t              s_client_snap_epoch = 0;
static int                   s_client_snap_blocks = 0;
/* World Ecology Wildlife Sync T2: late-join/reconnect wildlife snapshot, client side.
 *
 * s_client_wildlife_known_generation: the LAST authoritative wildlife session generation
 * (pcwld_session_generation(), pc_wildlife_authority.h) this client has ever applied a snapshot
 * from. 0 means "never applied one" (matches this module's own 0-is-reserved convention).
 * DELIBERATELY NOT reset by pcnetgame_reset_client_session_state() -- see that function's own doc
 * for why: this is the one piece of state that MUST survive a disconnect so a later reconnect to the
 * SAME still-running host can tell "this is the session I already have local wildlife bookkeeping
 * for" (reconcile against it, see pcwld_presentation_reconcile()) apart from "a genuinely new/
 * different session began" (discard local bookkeeping first). Compared, never mutated, from
 * pcnetgame_handle_client_wildlife_snapshot_begin() only.
 *
 * s_client_wildlife_snap_active / s_client_wildlife_snap_epoch: this peer's current wildlife
 * sub-pass, gated against the SAME outer snapshot epoch FIELD_BLOCK itself uses (see
 * PCNetGameWildlifeSnapshotBeginMsg's own doc for why no separate staleness scheme is invented) --
 * reset on every connect/disconnect by pcnetgame_reset_client_session_state(), exactly like
 * s_client_snap_active itself (a lost/interrupted wildlife sub-pass across a disconnect is simply
 * abandoned; the next connection's own SNAPSHOT_BEGIN starts a fresh one).
 *
 * s_client_wildlife_seen / s_client_wildlife_seen_count: entity_ids actually named by the wildlife
 * sub-pass currently in progress -- accumulated by each WILDLIFE_SNAPSHOT_ENTRY, consumed exactly
 * once by pcwld_presentation_reconcile() at WILDLIFE_SNAPSHOT_END. Sized to
 * PCWLD_PUBLIC_MAX_ENTITIES (pc_wildlife_authority.h), the same capacity bound the host's own
 * authoritative table uses -- never a different, invented limit. */
static uint32_t              s_client_wildlife_known_generation = 0;
static int                   s_client_wildlife_snap_active = 0;
static uint32_t              s_client_wildlife_snap_epoch = 0;
static uint32_t              s_client_wildlife_seen[PCWLD_PUBLIC_MAX_ENTITIES];
static int                   s_client_wildlife_seen_count = 0;
/* World Ecology Wildlife Sync T-catch, TEST-ONLY: see pcnetgame_run_fish_catch_test_trigger_client()'s
 * own doc for why this exists (--force-fish-catch). Updated unconditionally by
 * pcnetgame_handle_client_wildlife_spawn() whenever it is 1) cheap, and 2) otherwise unused unless the
 * flag is on. */
static uint32_t              s_force_catch_last_fish_entity_id = 0;
static int                   s_force_catch_last_fish_species = 0;
static float                 s_force_catch_last_fish_x = 0.0f;
static float                 s_force_catch_last_fish_z = 0.0f;
/* World Ecology Wildlife Sync T4, TEST-ONLY: identical bookkeeping to s_force_catch_last_fish_* above,
 * for --force-bug-catch. Only ever updated for a NON-ANT BUG record (see the two write sites' own doc,
 * pcnetgame_handle_client_wildlife_spawn()/pcnetgame_handle_client_wildlife_snapshot_entry()) -- an ant
 * is deliberately never latched here, since pcnetgame_validate_and_commit_catch() unconditionally rejects
 * any claim against it and latching one would just make the client trigger waste its one-shot fire on a
 * guaranteed rejection. */
static uint32_t              s_force_catch_last_bug_entity_id = 0;
static int                   s_force_catch_last_bug_species = 0;
static float                 s_force_catch_last_bug_x = 0.0f;
static float                 s_force_catch_last_bug_z = 0.0f;
/* s_client_wildlife_snap_incomplete: set by pcnetgame_handle_client_wildlife_snapshot_entry() when an
 * ENTRY arrives mid-pass while pcnetgame_client_can_apply_world() is false (e.g. a save-not-ready
 * latch drops partway through, then recovers before WILDLIFE_SNAPSHOT_END arrives). Without this,
 * s_client_wildlife_seen[]/seen_count would silently be missing every entity skipped during that
 * window, and pcwld_presentation_reconcile() at END would treat those still-live entities as stale
 * and discard their bookkeeping -- risking a duplicate actor on the next same-generation resync. When
 * set, WILDLIFE_SNAPSHOT_END abandons the pass instead of reconciling against an incomplete list; reset
 * at WILDLIFE_SNAPSHOT_BEGIN (a fresh pass starts clean) and at WILDLIFE_SNAPSHOT_END (consumed). */
static int                   s_client_wildlife_snap_incomplete = 0;
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
    w->gyoei_term = (uint8_t)Save_Get(gyoei_term);
    w->gyoei_term_transition_offset = (uint8_t)Save_Get(gyoei_term_transition_offset);
    w->insect_term = (uint8_t)Save_Get(insect_term);
    w->insect_term_transition_offset = (uint8_t)Save_Get(insect_term_transition_offset);
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

/* Protocol v7: the MOVE `action_state` field. Low byte = vanilla now_main_index, bits 8..11 = entry counter. */
#define PC_NETGAME_ACTION_INDEX_MASK 0x00FFu
#define PC_NETGAME_ACTION_COUNTER_SHIFT 8
#define PC_NETGAME_ACTION_COUNTER_MASK 0x0Fu
#define PC_NETGAME_ACTION_RESERVED_MASK 0xF000u

/* 4-bit state-entry counter of the LOCAL player (see pc_net_game_note_player_main_entry()). */
static uint8_t s_local_main_entry_counter = 0;

/* Read-only observation hook, called from the vanilla Player_actor_change_main_index() (src/game/m_player.c,
 * TARGET_PC only) after the local player's setup_main_<X>() ran, i.e. the player ACTUALLY entered `new_index` --
 * including a re-entry of the very same index (repeated swing/dig/pickup), which an edge detector on now_main_index
 * can never see. Only the local player's change_main_index calls this (a puppet is not a PLAYER_ACTOR and never runs
 * it). It writes nothing the game reads and is one increment when no session is active (the counter is simply
 * unused outside a session: the receiver compares (index, counter) by equality only). */
void pc_net_game_note_player_main_entry(int new_index) {
    (void)new_index;
    s_local_main_entry_counter = (uint8_t)((s_local_main_entry_counter + 1u) & PC_NETGAME_ACTION_COUNTER_MASK);
}

static uint16_t pcnetgame_encode_action_state(int now_main_index, uint8_t counter) {
    if (now_main_index < 0 || now_main_index > (int)PC_NETGAME_ACTION_INDEX_MASK || now_main_index >= mPlayer_INDEX_NUM) {
        return 0;
    }
    return (uint16_t)(((uint16_t)(counter & PC_NETGAME_ACTION_COUNTER_MASK) << PC_NETGAME_ACTION_COUNTER_SHIFT) |
                      (uint16_t)now_main_index);
}

/* 1 if `action_state` is well formed: main index < mPlayer_INDEX_NUM (121) and the reserved bits are zero. Index 0
 * (mPlayer_INDEX_DMA, boot) is well formed but carries no information (see pcnetgame_move_msg_to_sample()). */
static int pcnetgame_action_state_wellformed(uint16_t action_state) {
    return (action_state & PC_NETGAME_ACTION_RESERVED_MASK) == 0u &&
           (int)(action_state & PC_NETGAME_ACTION_INDEX_MASK) < (int)mPlayer_INDEX_NUM;
}

/* Never rejects the MOVE: a malformed action_state is only zeroed (movement keeps working). The host does this
 * before relaying (so no peer ever sees a malformed field from the host); a client does it on receive (a relay from
 * the host, or the host's own MOVE). Rate-limited, verbose-only log of the first few. */
static uint32_t s_bad_action_count = 0;
static void pcnetgame_sanitize_move_action(PCNetMoveMsg* msg, const char* side, int id) {
    if (pcnetgame_action_state_wellformed(msg->action_state)) {
        return;
    }
    ++s_bad_action_count;
    if (g_pc_verbose && s_bad_action_count <= 8u) {
        printf("[NET] %s %d: MOVE action_state 0x%04x malformed (index>=%d or reserved bits) -> zeroed, MOVE kept [%u so far]\n",
               side, id, (unsigned)msg->action_state, (int)mPlayer_INDEX_NUM, (unsigned)s_bad_action_count);
    }
    msg->action_state = 0;
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
    /* Protocol v7: the exact vanilla main index + the entry counter (see pc_net_game_note_player_main_entry()). An
     * out-of-range index (cannot happen for a live player actor) is sent as 0 = "no action info". */
    msg->action_state = pcnetgame_encode_action_state(local->now_main_index, s_local_main_entry_counter);
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
    /* Protocol v7: only a well-formed, non-zero index carries action information (callers sanitize first; this
     * re-check keeps the sample safe regardless of the caller). */
    if (pcnetgame_action_state_wellformed(in->action_state) &&
        (in->action_state & PC_NETGAME_ACTION_INDEX_MASK) != 0u) {
        sample->action_index = (uint8_t)(in->action_state & PC_NETGAME_ACTION_INDEX_MASK);
        sample->action_counter =
            (uint8_t)((in->action_state >> PC_NETGAME_ACTION_COUNTER_SHIFT) & PC_NETGAME_ACTION_COUNTER_MASK);
        sample->action_valid = 1;
    } else {
        sample->action_index = 0;
        sample->action_counter = 0;
        sample->action_valid = 0;
    }
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
static void pcnetgame_handle_host_move(PCNetPeerId peer, const PCNetMoveMsg* in_wire) {
    PCNetMoveMsg msg = *in_wire; /* private copy: action_state may be zeroed below (never the position fields) */
    const PCNetMoveMsg* in = &msg;
    PCNetMoveSample sample;
    int i;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }
    if (!pcnetgame_move_msg_valid(in)) {
        pcnetgame_note_bad_move("host: peer", (int)peer, in);
        return;
    }
    pcnetgame_sanitize_move_action(&msg, "host: peer", (int)peer); /* v7: zero a malformed action_state, keep the MOVE */

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
static void pcnetgame_handle_client_move(const PCNetMoveMsg* in_wire) {
    PCNetMoveMsg msg = *in_wire; /* private copy: action_state may be zeroed below */
    const PCNetMoveMsg* in = &msg;
    PCNetMoveSample sample;
    if (!pcnetgame_move_msg_valid(in)) {
        pcnetgame_note_bad_move("client: relayed player", (int)in->net_player_id, in);
        return; /* never let a non-finite/absurd position reach pc_remote_player's interpolation */
    }
    pcnetgame_sanitize_move_action(&msg, "client: relayed player", (int)in->net_player_id); /* v7: ignore a malformed field */
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

/* ======================= M9-A: scene identity / player presence =======================
 * What is announced (the ONLY whitelist -- everything else maps to "do not announce"): the scene ids a
 * player can really be standing in during normal play (the switch below). NOT announced: SCENE_TEST1/2/3/5,
 * WATER_TEST, FOOTPRINT_TEST, NPC_TEST, RANDOM_NPC_TEST, BG_TEST_NO_RIVER/RIVER, FIELD_TOOL,
 * FIELD_TOOL_INSIDE, START_DEMO/2/3 (after player select), PLAYERSELECT/2/3/SAVE, TITLE_DEMO,
 * EVENT_ANNOUNCEMENT, and any out-of-range value. There is no separate train-station scene id: the station
 * is part of SCENE_FG, so no STATION kind exists. */
PCNetSceneKind pc_net_game_scene_kind(int scene_id) {
    switch (scene_id) {
        case SCENE_FG:
            return PC_NETSCENE_KIND_FIELD;
        case SCENE_SHOP0:
        case SCENE_BROKER_SHOP:
        case SCENE_CONVENI:
        case SCENE_SUPER:
        case SCENE_DEPART:
        case SCENE_DEPART_2:
        case SCENE_NEEDLEWORK:
            return PC_NETSCENE_KIND_SHOP;
        case SCENE_POST_OFFICE:
            return PC_NETSCENE_KIND_POST_OFFICE;
        case SCENE_POLICE_BOX:
            return PC_NETSCENE_KIND_POLICE;
        case SCENE_MUSEUM_ENTRANCE:
        case SCENE_MUSEUM_ROOM_PAINTING:
        case SCENE_MUSEUM_ROOM_FOSSIL:
        case SCENE_MUSEUM_ROOM_INSECT:
        case SCENE_MUSEUM_ROOM_FISH:
            return PC_NETSCENE_KIND_MUSEUM;
        case SCENE_MY_ROOM_S:
        case SCENE_MY_ROOM_M:
        case SCENE_MY_ROOM_L:
        case SCENE_MY_ROOM_LL1:
        case SCENE_MY_ROOM_LL2:
        case SCENE_MY_ROOM_BASEMENT_S:
        case SCENE_MY_ROOM_BASEMENT_M:
        case SCENE_MY_ROOM_BASEMENT_L:
        case SCENE_MY_ROOM_BASEMENT_LL1:
        case SCENE_COTTAGE_MY:
            return PC_NETSCENE_KIND_PLAYER_HOUSE;
        case SCENE_NPC_HOUSE:
        case SCENE_COTTAGE_NPC:
            return PC_NETSCENE_KIND_VILLAGER_HOUSE;
        case SCENE_KAMAKURA:
        case SCENE_BUGGY:
        case SCENE_LIGHTHOUSE:
        case SCENE_TENT:
            return PC_NETSCENE_KIND_OTHER_INTERIOR;
        default:
            return PC_NETSCENE_KIND_UNKNOWN;
    }
}

int pc_net_game_scene_is_announceable(int scene_id) {
    return pc_net_game_scene_kind(scene_id) != PC_NETSCENE_KIND_UNKNOWN;
}

/* `owner` is only meaningful for the two shared house scene families (see PCNetPlayerScene). */
static uint16_t pcnetgame_scene_owner_for(int scene_id, uint16_t raw_owner) {
    PCNetSceneKind k = pc_net_game_scene_kind(scene_id);
    return (k == PC_NETSCENE_KIND_PLAYER_HOUSE || scene_id == SCENE_NPC_HOUSE) ? raw_owner : 0;
}

/* The local player's own announced scene: the last scene this process saw LIVE (a real GAME_PLAY running
 * play_main with a real player actor whose play->scene_id is announceable). valid == 0 until then. */
static PCNetPlayerScene s_local_scene;
static uint32_t s_local_scene_seq = 0;  /* monotonic send counter (never reset within a process run) */
static int s_local_scene_sent = 0;      /* client: the current s_local_scene was sent in THIS session */

static void pcnetgame_scene_fill(PCNetPlayerScene* out, int scene_id, uint8_t flags, uint16_t owner, uint32_t seq) {
    memset(out, 0, sizeof(*out));
    out->valid = 1;
    out->scene_id = (uint8_t)scene_id;
    out->flags = (uint8_t)(flags & PC_NETGAME_SCENE_FLAG_IN_TOWN);
    out->kind = (uint8_t)pc_net_game_scene_kind(scene_id);
    out->owner = pcnetgame_scene_owner_for(scene_id, owner);
    out->seq = seq;
}

static void pcnetgame_scene_pack(PCNetGamePlayerSceneMsg* msg, uint8_t net_player_id, const PCNetPlayerScene* s) {
    memset(msg, 0, sizeof(*msg));
    msg->msg_type = (uint8_t)PC_NETGAME_MSG_PLAYER_SCENE;
    msg->net_player_id = net_player_id;
    msg->scene_id = s->scene_id;
    msg->flags = s->flags;
    msg->owner = s->owner;
    msg->seq = s->seq;
}

/* Host -> every READY client except `skip` (pass -1 for none). */
static void pcnetgame_host_send_scene_to_all(PCNetPeerId skip, const PCNetGamePlayerSceneMsg* msg) {
    int i;
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (i != (int)skip && s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
            pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, msg, (uint16_t)sizeof(*msg));
        }
    }
}

/* M9-C: defined with the host talk-hold table further down. */
static void pcnetgame_host_talk_hold_clear_peer(PCNetPeerId peer, const char* why, int reset_seq);

/* Host: a client's own scene. READY-gated; id taken from the transport slot; announceable/flags/owner/seq
 * validated; relayed to every OTHER READY client only when accepted (a stale/duplicate seq is dropped and
 * never relayed). */
static void pcnetgame_handle_host_player_scene(PCNetPeerId peer, const PCNetGamePlayerSceneMsg* in) {
    PCNetPlayerScene s;
    PCNetGamePlayerSceneMsg out;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }
    if ((in->flags & ~PC_NETGAME_SCENE_FLAG_IN_TOWN) != 0 || !pc_net_game_scene_is_announceable(in->scene_id) ||
        in->seq == 0) {
        printf("[NET][SCENE] host: peer %d sent an invalid PLAYER_SCENE (scene=%u flags=0x%02X seq=%u) -- dropped\n",
               (int)peer, (unsigned)in->scene_id, (unsigned)in->flags, (unsigned)in->seq);
        return;
    }
    pcnetgame_scene_fill(&s, in->scene_id, in->flags, in->owner, in->seq);
    if (!pc_remote_player_on_scene((PCNetPlayerId)peer, &s)) {
        printf("[NET][SCENE] host: peer %d stale PLAYER_SCENE (seq %u) -- ignored\n", (int)peer, (unsigned)in->seq);
        return;
    }
    if (s.kind != (uint8_t)PC_NETSCENE_KIND_FIELD || (s.flags & PC_NETGAME_SCENE_FLAG_IN_TOWN) == 0 ||
        s.scene_id != (uint8_t)SCENE_FG) {
        pcnetgame_host_talk_hold_clear_peer(peer, "left the town field", 0); /* M9-C */
    }
    printf("[NET][SCENE] host: peer %d now in scene %u (kind %u, owner 0x%04X, flags 0x%02X, seq %u)\n", (int)peer,
           (unsigned)s.scene_id, (unsigned)s.kind, (unsigned)s.owner, (unsigned)s.flags, (unsigned)s.seq);
    pcnetgame_scene_pack(&out, (uint8_t)peer, &s); /* TRUE originating peer id, normalized fields */
    pcnetgame_host_send_scene_to_all(peer, &out);
}

/* Client: the host's own scene, a relayed peer's scene, a join-time replay entry, or a CLEARED notice. */
static void pcnetgame_handle_client_player_scene(const PCNetGamePlayerSceneMsg* in) {
    PCNetPlayerScene s;

    if (s_client_link != PC_NETGAME_LINK_READY) {
        return;
    }
    if (in->net_player_id > (uint8_t)PC_NETGAME_HOST_PLAYER_ID ||
        (in->net_player_id != (uint8_t)PC_NETGAME_HOST_PLAYER_ID &&
         (PCNetPlayerId)in->net_player_id == (PCNetPlayerId)s_client_assigned_peer_id)) {
        return; /* out of range, or "about me" (the host never echoes a sender's own scene back) */
    }
    if (in->flags & PC_NETGAME_SCENE_FLAG_CLEARED) {
        pc_remote_player_clear_scene((PCNetPlayerId)in->net_player_id);
        printf("[NET][SCENE] client: player %u scene cleared\n", (unsigned)in->net_player_id);
        return;
    }
    if ((in->flags & ~PC_NETGAME_SCENE_FLAG_IN_TOWN) != 0 || !pc_net_game_scene_is_announceable(in->scene_id) ||
        in->seq == 0) {
        return;
    }
    pcnetgame_scene_fill(&s, in->scene_id, in->flags, in->owner, in->seq);
    if (pc_remote_player_on_scene((PCNetPlayerId)in->net_player_id, &s)) {
        printf("[NET][SCENE] client: player %u now in scene %u (kind %u, owner 0x%04X, flags 0x%02X, seq %u)\n",
               (unsigned)in->net_player_id, (unsigned)s.scene_id, (unsigned)s.kind, (unsigned)s.owner,
               (unsigned)s.flags, (unsigned)s.seq);
    }
}

/* ---- M9-C Phase 5: PLAYER_ACTION (cosmetic pickup presentation hint, host -> client) ---- */
static uint16_t s_action_seq_out[PC_NET_MAX_PEERS + 1];   /* host: last seq assigned per origin id (peer ids + the host) */
static uint16_t s_action_last_seq[PC_NET_MAX_PEERS + 1];  /* client: last accepted seq per origin id */
static uint8_t  s_action_last_valid[PC_NET_MAX_PEERS + 1];

static int pcnetgame_action_diag(void) {
    static int s_d = -1;
    if (s_d < 0) {
        const char* e = getenv("PC_PUPPET_DIAG");
        s_d = (e != NULL && e[0] == '1') ? 1 : 0;
    }
    return s_d;
}

/* Host only. `origin` = a READY peer id or PC_NETGAME_HOST_PLAYER_ID. Sends to every READY client except the origin peer and
 * presents locally for a client origin. The caller has already committed the tile; nothing here touches the field. */
static void pcnetgame_host_emit_player_action(int origin, uint8_t kind, int ut_x, int ut_z, uint16_t item) {
    PCNetGamePlayerActionMsg m;
    int i, sent = 0;

    if (s_role != PC_NETGAME_ROLE_HOST || kind != (uint8_t)PC_NETGAME_PLAYER_ACTION_KIND_PICKUP) {
        return;
    }
    if (origin < 0 || origin > (int)PC_NETGAME_HOST_PLAYER_ID || ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255 ||
        item == 0u || item == 0xFFFFu) {
        return;
    }
    if (origin < PC_NET_MAX_PEERS) {
        if (s_host_peer_link[origin] != PC_NETGAME_LINK_READY || !s_host_peer[origin].ctx_valid ||
            !(s_host_peer[origin].ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
            return; /* a peer that is not READY / not in the town field never gets a presentation event */
        }
    } else if (!pcfa_scene_is_town()) {
        return;
    }
    memset(&m, 0, sizeof(m));
    m.msg_type = (uint8_t)PC_NETGAME_MSG_PLAYER_ACTION;
    m.net_player_id = (uint8_t)origin;
    m.kind = kind;
    m.ut_x = (uint8_t)ut_x;
    m.ut_z = (uint8_t)ut_z;
    m.item = item;
    s_action_seq_out[origin] = (uint16_t)(s_action_seq_out[origin] + 1u);
    m.seq = s_action_seq_out[origin];
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (i != origin && s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
            if (pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &m, (uint16_t)sizeof(m))) {
                sent++;
            }
        }
    }
    if (origin < PC_NET_MAX_PEERS) {
        /* the host process renders this client's puppet itself: same receiver, no network loop */
        (void)pc_remote_player_on_action((PCNetPlayerId)origin, (int)kind, ut_x, ut_z, item, m.seq);
    }
    if (pcnetgame_action_diag()) {
        printf("[NET][ACTION][DIAG] host: PLAYER_ACTION kind=%u origin=%d seq=%u tile=(%d,%d) item=0x%04X relayed_to=%d\n",
               (unsigned)kind, origin, (unsigned)m.seq, ut_x, ut_z, (unsigned)item, sent);
    }
}

/* Client: the host's PLAYER_ACTION (about the host or a relayed peer). Presentation hint only. */
static void pcnetgame_handle_client_player_action(const PCNetGamePlayerActionMsg* in) {
    int origin = (int)in->net_player_id;
    int acre = 0;

    if (s_client_link != PC_NETGAME_LINK_READY) {
        return;
    }
    if (origin > (int)PC_NETGAME_HOST_PLAYER_ID ||
        (origin != (int)PC_NETGAME_HOST_PLAYER_ID && (PCNetPlayerId)origin == (PCNetPlayerId)s_client_assigned_peer_id)) {
        return; /* out of range, or "about me" (the originator plays its own visuals) */
    }
    if (in->kind != (uint8_t)PC_NETGAME_PLAYER_ACTION_KIND_PICKUP || in->flags != 0 || in->item == 0u ||
        in->item == 0xFFFFu || !pcfa_town_ut_to_acre_tile((int)in->ut_x, (int)in->ut_z, &acre, NULL)) {
        if (pcnetgame_action_diag()) {
            printf("[NET][ACTION][DIAG] client: ignored invalid PLAYER_ACTION (kind=%u flags=0x%02X tile=(%u,%u) item=0x%04X)\n",
                   (unsigned)in->kind, (unsigned)in->flags, (unsigned)in->ut_x, (unsigned)in->ut_z, (unsigned)in->item);
        }
        return;
    }
    if (s_action_last_valid[origin] && (int16_t)(uint16_t)(in->seq - s_action_last_seq[origin]) <= 0) {
        if (pcnetgame_action_diag()) {
            printf("[NET][ACTION][DIAG] client: stale/duplicate PLAYER_ACTION origin=%d seq=%u (last %u) ignored\n", origin,
                   (unsigned)in->seq, (unsigned)s_action_last_seq[origin]);
        }
        return;
    }
    s_action_last_seq[origin] = in->seq;
    s_action_last_valid[origin] = 1;
    (void)pc_remote_player_on_action((PCNetPlayerId)origin, (int)in->kind, (int)in->ut_x, (int)in->ut_z, in->item,
                                     in->seq);
}

/* Host: `peer` was READY and is gone (disconnect/timeout/drop/reject). Its own slot is cleared by
 * pc_remote_player_on_disconnect(); this tells every other READY client to drop the relayed entry too (a
 * reliable, ordered CLEARED, so it always precedes the same peer's post-reconnect announcement). */
static void pcnetgame_host_peer_scene_gone(PCNetPeerId peer) {
    PCNetGamePlayerSceneMsg msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_PLAYER_SCENE;
    msg.net_player_id = (uint8_t)peer;
    msg.flags = (uint8_t)PC_NETGAME_SCENE_FLAG_CLEARED;
    printf("[NET][SCENE] host: peer %d left -- its scene was cleared (other clients notified)\n", (int)peer);
    pcnetgame_host_send_scene_to_all(peer, &msg);
}

/* Host: join-time replay for a newly READY `dest` (reuses the existing READY hook, like the appearance
 * roster): the host's own scene plus the last known scene of every OTHER READY peer, read from
 * pc_remote_player.c's canonical per-slot storage (no second cache). Nothing is sent for a player with no
 * known scene. The late joiner's own slots are fresh, so original seqs are accepted. */
static void pcnetgame_host_send_scene_roster(PCNetPeerId dest) {
    PCNetGamePlayerSceneMsg msg;
    PCNetPlayerScene s;
    int i;

    if (s_local_scene.valid) {
        pcnetgame_scene_pack(&msg, (uint8_t)PC_NETGAME_HOST_PLAYER_ID, &s_local_scene);
        pc_net_send(dest, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
    }
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (i == (int)dest || s_host_peer_link[i] != PC_NETGAME_LINK_READY) {
            continue;
        }
        if (pc_remote_player_get_scene((PCNetPlayerId)i, &s)) {
            pcnetgame_scene_pack(&msg, (uint8_t)i, &s);
            pc_net_send(dest, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
        }
    }
}

/* Local scene detection, called once per pc_net_game_poll() (every VI frame), both roles.
 * Source of truth: gamePT's own GAME_PLAY->scene_id, which is assigned by play_init() ->
 * Gameplay_Scene_Read() when a NEW GAME_PLAY is constructed and never changes for that object's life --
 * unlike Save.scene_no, which Game_play_change_scene_move_end() overwrites BEFORE teardown. So the old
 * scene's last frames keep reading the old id, and the new id appears only once the new GAME_PLAY is
 * running play_main with a real player actor (the same liveness test the movement sender uses). Wipe /
 * teardown intermediates (gamePT NULL, exec != play_main, no real player actor) and non-announceable
 * scenes simply produce no event. The latch also requires s_local_world_latched, so the title demo,
 * player select and an unbound resident never announce. Detection is by VALUE change of
 * (scene_id, owner, flags), so no new hook in m_play.c/m_scene.c is needed.
 * Known limits (doc only): (a) a connected client cannot announce "no scene": if it moves into a live scene
 * that is not announceable (or has no real player actor), peers keep its LAST announced scene until it
 * announces another or disconnects. (b) a house-scene `owner` is relayed by the host without range
 * validation (presence data only). (c) host scene sends ignore pc_net_send() failure, the same best-effort
 * pattern as the appearance roster. */
static void pcnetgame_scene_tick(void) {
    if (s_local_world_latched && gamePT != NULL && gamePT->exec == play_main) {
        GAME_PLAY* play = (GAME_PLAY*)gamePT;
        int sid = (int)play->scene_id;

        if (pc_net_game_scene_is_announceable(sid) && pcnetgame_is_real_player_actor(GET_PLAYER_ACTOR_NOW())) {
            uint8_t flags = (sid == SCENE_FG && pcfa_scene_is_town()) ? (uint8_t)PC_NETGAME_SCENE_FLAG_IN_TOWN : 0;
            uint16_t owner = pcnetgame_scene_owner_for(sid, (uint16_t)Common_Get(house_owner_name));

            if (!s_local_scene.valid || s_local_scene.scene_id != (uint8_t)sid || s_local_scene.owner != owner ||
                s_local_scene.flags != flags) {
                pcnetgame_scene_fill(&s_local_scene, sid, flags, owner, s_local_scene.seq);
                s_local_scene_sent = 0;
                if (s_role == PC_NETGAME_ROLE_HOST) {
                    PCNetGamePlayerSceneMsg msg;
                    s_local_scene.seq = ++s_local_scene_seq;
                    s_local_scene_sent = 1;
                    printf("[NET][SCENE] local scene live: scene=%d kind=%u owner=0x%04X flags=0x%02X seq=%u "
                           "(announcing to clients)\n",
                           sid, (unsigned)s_local_scene.kind, (unsigned)s_local_scene.owner,
                           (unsigned)s_local_scene.flags, (unsigned)s_local_scene.seq);
                    pcnetgame_scene_pack(&msg, (uint8_t)PC_NETGAME_HOST_PLAYER_ID, &s_local_scene);
                    pcnetgame_host_send_scene_to_all((PCNetPeerId)-1, &msg);
                }
            }
        }
    }

    if (s_role == PC_NETGAME_ROLE_CLIENT && s_local_scene.valid && !s_local_scene_sent &&
        s_client_link == PC_NETGAME_LINK_READY) {
        PCNetGamePlayerSceneMsg msg;
        s_local_scene.seq = ++s_local_scene_seq; /* a re-announce after a reconnect also gets a fresh seq */
        pcnetgame_scene_pack(&msg, 0, &s_local_scene);
        if (pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
            s_local_scene_sent = 1;
            printf("[NET][SCENE] local scene live: scene=%d kind=%u owner=0x%04X flags=0x%02X seq=%u "
                   "(announcing to host)\n",
                   (int)s_local_scene.scene_id, (unsigned)s_local_scene.kind, (unsigned)s_local_scene.owner,
                   (unsigned)s_local_scene.flags, (unsigned)s_local_scene.seq);
        }
    }
}

/* M9-A TEST-ONLY hook: --scene-test-enter-shop [--scene-test-leave-after N]. A complete no-op unless the
 * flag was passed (see g_pc_scene_test_enter_shop's doc, pc_platform.h). Drives a REAL scene transition
 * through the REAL goto_other_scene() path on the already-running field, so the real scene-live detection
 * (pcnetgame_scene_tick()) and announcement are exercised. It is NOT manual play: it replaces only the
 * walk-into-the-door step. Stage 0: once the local player has been announced as in the IN_TOWN field and
 * stayed there ~2 s, request the door exactly as ac_shop_move.c_inc's aSHOP_pl_into_wait() does (same
 * Door_data_c values, same out-data rewrite as aSHOP_rewrite_out_data(), but placed at the player's own
 * position). Stage 1: once SCENE_SHOP0 is announced, optionally (--scene-test-leave-after N polls) leave
 * through the interior's exit data exactly as Player_actor_set_nextgoto_info_type0() does. Retries while a
 * wipe is already running. */
static void pcnetgame_run_scene_test_hook(void) {
    static int s_stage = 0;
    static int s_wait = 0;
    GAME_PLAY* play;

    if (!g_pc_scene_test_enter_shop || s_stage >= 3 || gamePT == NULL || gamePT->exec != play_main ||
        !s_local_world_latched || !s_local_scene.valid) {
        return;
    }
    play = (GAME_PLAY*)gamePT;
    if (play->fb_wipe_mode != WIPE_MODE_NONE || !pcnetgame_is_real_player_actor(GET_PLAYER_ACTOR_NOW())) {
        return;
    }

    if (s_stage == 0) {
        if (s_local_scene.scene_id != (uint8_t)SCENE_FG || !(s_local_scene.flags & PC_NETGAME_SCENE_FLAG_IN_TOWN) ||
            (s_role == PC_NETGAME_ROLE_CLIENT && !s_local_scene_sent)) {
            s_wait = 0;
            return;
        }
        if (++s_wait < 180) {
            return;
        }
        {
            static Door_data_c door = { SCENE_SHOP0, mSc_DIRECT_NORTH, FALSE, 0, { 160, 0, 300 }, EMPTY_NO, 1,
                                        { 0, 0, 0 } };
            PLAYER_ACTOR* player = GET_PLAYER_ACTOR_NOW();
            Door_data_c* out = Common_GetPointer(structure_exit_door_data);
            xyz_t pos = player->actor_class.world.position;
            int res;

            out->next_scene_id = Save_Get(scene_no);
            out->exit_orientation = mSc_DIRECT_SOUTH_WEST;
            out->exit_type = 0;
            out->extra_data = 3;
            out->exit_position.x = pos.x;
            out->exit_position.y = mCoBG_GetBgY_OnlyCenter_FromWpos2(pos, 0.0f);
            out->exit_position.z = pos.z;
            out->door_actor_name = SHOP0;
            out->wipe_type = WIPE_TYPE_TRIFORCE;

            res = goto_other_scene(play, &door, FALSE);
            printf("[NET][SCENE][TEST] hook: goto_other_scene(SCENE_SHOP0) res=%d (hook-driven, not manual play)\n",
                   res);
            if (res == 1) {
                s_stage = 1;
                s_wait = 0;
            }
        }
    } else if (s_stage == 1) {
        if (s_local_scene.scene_id != (uint8_t)SCENE_SHOP0) {
            return;
        }
        if (g_pc_scene_test_leave_after <= 0) {
            s_stage = 3; /* stay inside */
            return;
        }
        if (++s_wait < g_pc_scene_test_leave_after) {
            return;
        }
        {
            int res = goto_other_scene(play, Common_GetPointer(structure_exit_door_data), TRUE);
            printf("[NET][SCENE][TEST] hook: goto_other_scene(exit) res=%d (hook-driven, not manual play)\n", res);
            if (res == 1) {
                s_stage = 3;
            }
        }
    }
}

/* M9-B TEST-ONLY hooks: --collide-test-overlap and --collide-test-approach N. Complete no-ops unless a flag was
 * passed (see their docs in pc_platform.h). Both only ever write the LOCAL player's actor world.position -- the same
 * field and the same between-frames timing the --force-dig-hole teleport above uses -- and never touch the
 * collision pipeline: the vanilla solver then sees the overlap through the player's and the puppet's normally
 * registered pipes (the player re-registers its pipe at its new position during its next move, so the first push
 * lands 2 game frames later) and pushes the local player via status_data.collision_vec inside the player's own
 * Actor_position_move(), exactly as for any NPC. Gating: this process is announced in the IN_TOWN field, a real
 * local player actor exists, no wipe is running, and a same-scene puppet with snapshots + visual has been present
 * for ~2 s (120 polls), and the local player is in the standing state (mPlayer_INDEX_WAIT).
 *   overlap: once, local.xz = first puppet's current position + (10, 0).
 *   approach: from the same point on, once per NEW game frame, move the local player 2 units toward the puppet,
 *     for N game frames, then stop (the per-game-frame step is detected via gamePT->frame_counter because the poll
 *     runs per VI frame, not per game frame). */
static void pcnetgame_run_collide_test_hook(void) {
    static int s_wait = 0;
    static int s_overlap_done = 0;
    static int s_approach_left = -1; /* -1 = not started */
    static uint32_t s_last_frame = 0;
    PLAYER_ACTOR* local;
    float px, py, pz;

    if ((!g_pc_collide_test_overlap || s_overlap_done) && (g_pc_collide_test_approach <= 0 || s_approach_left == 0)) {
        return;
    }
    if (gamePT == NULL || gamePT->exec != play_main || s_role == PC_NETGAME_ROLE_NONE || !s_local_world_latched ||
        !s_local_scene.valid || s_local_scene.scene_id != (uint8_t)SCENE_FG ||
        !(s_local_scene.flags & PC_NETGAME_SCENE_FLAG_IN_TOWN) ||
        ((GAME_PLAY*)gamePT)->fb_wipe_mode != WIPE_MODE_NONE) {
        s_wait = 0;
        return;
    }
    local = GET_PLAYER_ACTOR_NOW();
    /* The local player must be in the plain standing state (mPlayer_INDEX_WAIT): right after the bootstrap the
     * player is still in its house-exit mPlayer_INDEX_OUTDOOR state (immovable weight 255, a different collision
     * routine), where no push can apply and a hook-driven overlap would prove nothing. */
    if (!pcnetgame_is_real_player_actor(local) || local->now_main_index != mPlayer_INDEX_WAIT ||
        !pc_remote_player_collide_test_target(&px, &py, &pz)) {
        s_wait = 0;
        return;
    }
    if (s_wait < 120) {
        s_wait++;
        return;
    }

    if (g_pc_collide_test_overlap && !s_overlap_done) {
        printf("[NET][COLLIDE][TEST] --collide-test-overlap: frame=%u teleporting local (%.2f,%.2f) -> puppet+(10,0) "
               "(%.2f,%.2f)\n", (unsigned)gamePT->frame_counter, (double)local->actor_class.world.position.x,
               (double)local->actor_class.world.position.z, (double)(px + 10.0f), (double)pz);
        local->actor_class.world.position.x = px + 10.0f;
        local->actor_class.world.position.z = pz;
        s_overlap_done = 1;
        return;
    }

    if (g_pc_collide_test_approach > 0 && s_approach_left != 0) {
        if (s_approach_left < 0) {
            s_approach_left = g_pc_collide_test_approach;
            s_last_frame = gamePT->frame_counter;
            printf("[NET][COLLIDE][TEST] --collide-test-approach: frame=%u starting, %d steps of 2 units toward "
                   "puppet (%.2f,%.2f) from local (%.2f,%.2f)\n", (unsigned)gamePT->frame_counter, s_approach_left,
                   (double)px, (double)pz, (double)local->actor_class.world.position.x,
                   (double)local->actor_class.world.position.z);
            return;
        }
        if (gamePT->frame_counter != s_last_frame) {
            float dx = px - local->actor_class.world.position.x;
            float dz = pz - local->actor_class.world.position.z;
            float d = sqrtf(dx * dx + dz * dz);

            s_last_frame = gamePT->frame_counter;
            if (d > 2.0f) {
                local->actor_class.world.position.x += 2.0f * dx / d;
                local->actor_class.world.position.z += 2.0f * dz / d;
            }
            if (--s_approach_left == 0) {
                printf("[NET][COLLIDE][TEST] --collide-test-approach: frame=%u done (stopped stepping)\n",
                       (unsigned)gamePT->frame_counter);
            }
        }
    }
}

int pc_net_game_get_local_scene(PCNetPlayerScene* out) {
    if (out == NULL || !s_local_scene.valid) {
        return 0;
    }
    *out = s_local_scene;
    return 1;
}

int pc_net_game_get_peer_scene(PCNetPlayerId player_id, PCNetPlayerScene* out) {
    if (out == NULL) {
        return 0;
    }
    if (s_role == PC_NETGAME_ROLE_HOST && player_id == PC_NETGAME_HOST_PLAYER_ID) {
        return pc_net_game_get_local_scene(out);
    }
    if (s_role == PC_NETGAME_ROLE_NONE) {
        return 0;
    }
    return pc_remote_player_get_scene(player_id, out);
}

/* Log tag / record accessor for one interaction kind (PC_NETGAME_INTERACT_KIND_*). */
static const char* pcnetgame_kind_tag(int kind) {
    if (kind == (int)PC_NETGAME_INTERACT_KIND_DROP) {
        return "DROP";
    }
    if (kind == (int)PC_NETGAME_INTERACT_KIND_BURY) {
        return "BURY"; /* World Ecology T0 scaffolding -- see PC_NETGAME_INTERACT_KIND_BURY's doc */
    }
    return "PICKUP";
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
    if (kind == (int)PC_NETGAME_INTERACT_KIND_BURY) {
        return &s_host_bury_state[peer]; /* World Ecology T0 scaffolding -- see s_host_bury_state's doc.
                                            * pcnetgame_handle_host_confirm() can therefore already
                                            * dispatch a BURY INTERACT_CONFIRM through this generic
                                            * accessor, but it is a provable no-op until T3 ever sets
                                            * this record's phase to PENDING. */
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

/* World Ecology T0: PROTOCOL SCAFFOLDING ONLY -- exact mirror of pcnetgame_reset_host_drop_state(),
 * for s_host_bury_state (see its own doc). Safe to call for an out-of-range peer (no-op); a no-op in
 * practice today either way, since s_host_bury_state is never PENDING until T3. Registered in
 * pcnetgame_reset_all_host_peer_state() now so T3 does not also have to remember to wire this up. */
static void pcnetgame_reset_host_bury_state(PCNetPeerId peer) {
    if (peer < 0 || peer >= PC_NET_MAX_PEERS) {
        return;
    }
    pcnetgame_host_release(peer, (int)PC_NETGAME_INTERACT_KIND_BURY, &s_host_bury_state[peer],
                           PC_NETGAME_PHASE_ABORTED, "peer reset/disconnect");
    memset(&s_host_bury_state[peer], 0, sizeof(s_host_bury_state[peer]));
}

/* The three PENDING-reservation interaction kinds pcnetgame_host_tile_reserved_by()/
 * pcnetgame_host_expire_reservations() scan across every peer: PICKUP, DROP, and (since T3) BURY --
 * a bury reservation must block a competing pickup/drop/bury of the SAME tile exactly like pickup and
 * drop already block each other, so it shares this same table rather than a separate one. */
static const int s_host_reservation_kinds[3] = { (int)PC_NETGAME_INTERACT_KIND_PICKUP,
                                                  (int)PC_NETGAME_INTERACT_KIND_DROP,
                                                  (int)PC_NETGAME_INTERACT_KIND_BURY };

/* Returns the peer (0..PC_NET_MAX_PEERS-1) whose PENDING pickup/drop(/bury -- see
 * s_host_reservation_kinds's doc) currently reserves persistent tile (acre, tile), or -1 if none does.
 * A PENDING record whose age already reached PC_NETGAME_CONFIRM_TIMEOUT_MS does not count (expiry
 * proper runs at the top of every poll; this keeps the answer exact between polls). */
static int pcnetgame_host_tile_reserved_by(int acre, int tile) {
    uint32_t now = 0;
    int i, k;

    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        for (k = 0; k < 3; k++) {
            const PCNetGameHostInteraction* it = pcnetgame_host_interaction((PCNetPeerId)i, s_host_reservation_kinds[k]);
            if (it == NULL || it->phase != (uint8_t)PC_NETGAME_PHASE_PENDING || it->acre != (uint8_t)acre ||
                it->tile != (uint8_t)tile) {
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
        for (k = 0; k < 3; k++) { /* T0-D: PICKUP/DROP/BURY -- see s_host_reservation_kinds's doc */
            int kind = s_host_reservation_kinds[k];
            PCNetGameHostInteraction* it = pcnetgame_host_interaction((PCNetPeerId)i, kind);
            if (it == NULL || it->phase != (uint8_t)PC_NETGAME_PHASE_PENDING) {
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

/* ---- M9-C: host-authoritative villager talk hold -------------------------------------------------------
 * A ready client that talks to villager X takes a LOCAL talk lease (ac_npc_move.c_inc) so its local copy of X can
 * run the talk action; it reports the lease's rising/falling edge to the host with PC_NETGAME_MSG_NPC_TALK. The
 * host validates and keeps a per-animal-slot hold (a u16 peer bitmask: X stays held while ANY peer holds it);
 * ac_npc_move.c_inc's host gate asks pc_net_game_host_npc_talk_held() every frame and, while held and the host
 * itself is not talking to X, stops X's decisions and holds it still. Clients keep mirroring the host. Nothing
 * here ever moves or authors an NPC. Expiry (default 30 s, PC_NPC_TALKHOLD_TIMEOUT_MS overrides, test only) is a
 * safety net for a lost END only: a real dialogue longer than that releases the hold early (X resumes walking on
 * the host and the client snaps at its lease end -- the pre-hold behaviour). */
typedef struct PCNetGameNpcTalkHold {
    uint16_t peer_mask; /* bit i = PCNetPeerId i currently holds this slot */
    uint16_t npc_id;    /* identity the mask was set for */
    uint32_t last_ms;   /* pcnetgame_now_ms() of the last valid BEGIN */
} PCNetGameNpcTalkHold;
static PCNetGameNpcTalkHold s_host_talk_hold[ANIMAL_NUM_MAX];
static uint16_t s_host_talk_last_seq[PC_NET_MAX_PEERS];
static uint8_t  s_host_talk_seq_valid[PC_NET_MAX_PEERS];
static uint16_t s_npc_talk_seq = 0;   /* client: per-process, per-session u16 sequence of sent NPC_TALK */
static int      s_npc_talk_epoch = 0; /* client: bumped on every client session reset (see the NPC edge code) */
static int      s_host_talk_reject_logs = 0;

/* Client keepalive: every BEGIN this client sent whose END has not been sent yet. pc_net_game_poll() (runs every VI
 * frame, even while a submenu/message window freezes the actors) re-sends the BEGIN (same wire message, fresh seq)
 * every refresh interval while the NPC code's probe still reports the lease active for that slot/npc. Cleared by
 * END, by the session reset, and by a failed probe. */
typedef struct PCNetGameNpcTalkOut {
    uint8_t  active;
    uint16_t npc_id;
    uint32_t last_ms;
} PCNetGameNpcTalkOut;
static PCNetGameNpcTalkOut s_client_talk_out[ANIMAL_NUM_MAX];
static PCNetGameNpcTalkLeaseProbe s_client_talk_probe = NULL;

/* Refresh interval in ms; PC_NPC_TALKHOLD_REFRESH_MS overrides (test only; 0 disables the keepalive). */
static uint32_t pcnetgame_talk_refresh_ms(void) {
    static int s_init = 0;
    static uint32_t s_ms = 10000u;
    if (!s_init) {
        const char* e = getenv("PC_NPC_TALKHOLD_REFRESH_MS");
        s_init = 1;
        if (e != NULL && e[0] != 0) {
            long v = atol(e);
            s_ms = (v > 0) ? (uint32_t)v : 0u;
        }
    }
    return s_ms;
}

static int pcnetgame_talk_diag(void) {
    static int s_diag = -1;
    if (s_diag < 0) {
        const char* e = getenv("PC_NPC_TALKHOLD_DIAG");
        s_diag = (e != NULL && e[0] == '1') ? 1 : 0;
    }
    return s_diag;
}

uint32_t pc_net_game_now_ms(void) {
    return pcnetgame_now_ms();
}

void pc_net_game_set_npc_talk_lease_probe(PCNetGameNpcTalkLeaseProbe fn) {
    s_client_talk_probe = fn;
}

static uint32_t pcnetgame_talk_hold_timeout_ms(void) {
    static uint32_t s_timeout = 0;
    if (s_timeout == 0) {
        const char* e = getenv("PC_NPC_TALKHOLD_TIMEOUT_MS");
        long v = (e != NULL) ? atol(e) : 0;
        s_timeout = (v > 0) ? (uint32_t)v : 30000u;
    }
    return s_timeout;
}

/* Clear `peer`'s bit in every slot (and, when `reset_seq`, its sequence record). */
static void pcnetgame_host_talk_hold_clear_peer(PCNetPeerId peer, const char* why, int reset_seq) {
    int slot;
    if (peer < 0 || peer >= PC_NET_MAX_PEERS) {
        return;
    }
    for (slot = 0; slot < ANIMAL_NUM_MAX; slot++) {
        PCNetGameNpcTalkHold* h = &s_host_talk_hold[slot];
        if ((h->peer_mask & (uint16_t)(1u << peer)) != 0) {
            h->peer_mask = (uint16_t)(h->peer_mask & ~(uint16_t)(1u << peer));
            if (h->peer_mask == 0) {
                printf("[NPC][TALKNET] RELEASE slot=%d npc=0x%04X (peer %d %s)\n", slot, (unsigned)h->npc_id,
                       (int)peer, why);
            } else {
                printf("[NPC][TALKNET] HOLD slot=%d npc=0x%04X peers=0x%04X (peer %d %s)\n", slot,
                       (unsigned)h->npc_id, (unsigned)h->peer_mask, (int)peer, why);
            }
        }
    }
    if (reset_seq) {
        s_host_talk_last_seq[peer] = 0;
        s_host_talk_seq_valid[peer] = 0;
    }
}

/* Poll-time sweep: expire holds whose last valid BEGIN is older than the timeout (so a hold on a villager with
 * no live actor is cleared too). Cheap: 15 slots, nothing logged unless something expires. */
static void pcnetgame_host_talk_hold_sweep(void) {
    int slot;
    uint32_t now = 0;
    for (slot = 0; slot < ANIMAL_NUM_MAX; slot++) {
        PCNetGameNpcTalkHold* h = &s_host_talk_hold[slot];
        if (h->peer_mask == 0) {
            continue;
        }
        if (now == 0) {
            now = pcnetgame_now_ms();
        }
        if ((uint32_t)(now - h->last_ms) >= pcnetgame_talk_hold_timeout_ms()) {
            printf("[NPC][TALKNET] EXPIRE slot=%d npc=0x%04X peers=0x%04X after %u ms\n", slot, (unsigned)h->npc_id,
                   (unsigned)h->peer_mask, (unsigned)(now - h->last_ms));
            h->peer_mask = 0;
        }
    }
}

static void pcnetgame_host_talk_reject(PCNetPeerId peer, const PCNetGameNpcTalkMsg* in, const char* reason) {
    if (s_host_talk_reject_logs < 40) {
        s_host_talk_reject_logs++;
        printf("[NPC][TALKNET] REJECT peer=%d slot=%u npc=0x%04X %s: %s\n", (int)peer, (unsigned)in->slot,
               (unsigned)in->npc_id, (in->flags & PC_NETGAME_NPC_TALK_FLAG_BEGIN) ? "begin" : "end", reason);
    }
}

/* Host: a client's talk-lease edge. READY-gated, peer identity from the transport slot. */
static void pcnetgame_handle_host_npc_talk(PCNetPeerId peer, const PCNetGameNpcTalkMsg* in) {
    PCNetGameNpcTalkHold* h;
    int begin;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }
    begin = (in->flags & PC_NETGAME_NPC_TALK_FLAG_BEGIN) != 0;
    if ((in->flags & ~PC_NETGAME_NPC_TALK_FLAG_BEGIN) != 0 || in->slot >= ANIMAL_NUM_MAX) {
        pcnetgame_host_talk_reject(peer, in, "bad flags or slot out of range");
        return;
    }
    if (s_host_talk_seq_valid[peer] && (int16_t)(uint16_t)(in->seq - s_host_talk_last_seq[peer]) <= 0) {
        pcnetgame_host_talk_reject(peer, in, "stale/duplicate seq");
        return;
    }
    s_host_talk_last_seq[peer] = in->seq;
    s_host_talk_seq_valid[peer] = 1;
    h = &s_host_talk_hold[in->slot];

    if (!begin) {
        /* END: idempotent; clears only THIS peer's bit; unknown slot/npc/peer is ignored. */
        if (h->npc_id == in->npc_id && (h->peer_mask & (uint16_t)(1u << peer)) != 0) {
            printf("[NPC][TALKNET] END peer=%d slot=%u npc=0x%04X\n", (int)peer, (unsigned)in->slot,
                   (unsigned)in->npc_id);
            h->peer_mask = (uint16_t)(h->peer_mask & ~(uint16_t)(1u << peer));
            if (h->peer_mask == 0) {
                printf("[NPC][TALKNET] RELEASE slot=%u npc=0x%04X\n", (unsigned)in->slot, (unsigned)in->npc_id);
            } else {
                printf("[NPC][TALKNET] HOLD slot=%u npc=0x%04X peers=0x%04X\n", (unsigned)in->slot,
                       (unsigned)in->npc_id, (unsigned)h->peer_mask);
            }
        }
        return;
    }

    {
        Animal_c* animal = Save_GetPointer(animals[in->slot]);
        PCNetPlayerScene sc;

        if (ITEM_NAME_GET_TYPE(animal->id.npc_id) != NAME_TYPE_NPC || (uint16_t)animal->id.npc_id != in->npc_id) {
            pcnetgame_host_talk_reject(peer, in, "npc_id does not match the host animal table");
            return;
        }
        if (!pc_remote_player_get_scene((PCNetPlayerId)peer, &sc) || sc.kind != (uint8_t)PC_NETSCENE_KIND_FIELD ||
            (sc.flags & PC_NETGAME_SCENE_FLAG_IN_TOWN) == 0 || sc.scene_id != (uint8_t)SCENE_FG) {
            pcnetgame_host_talk_reject(peer, in, "peer is not in the town field scene");
            return;
        }
    }
    /* Distance validation intentionally skipped: the host NPC actor is not cheaply/safely resolvable from this
     * file by animal slot, and the last known peer position lags; slot+npc_id+town-scene are the guards. */
    if ((h->peer_mask & (uint16_t)(1u << peer)) == 0 || h->npc_id != in->npc_id) { /* a keepalive repeat logs nothing */
        printf("[NPC][TALKNET] BEGIN peer=%d slot=%u npc=0x%04X (distance check skipped)\n", (int)peer,
               (unsigned)in->slot, (unsigned)in->npc_id);
    } else if (pcnetgame_talk_diag()) {
        printf("[NPC][TALKNET][DIAG] refresh peer=%d slot=%u npc=0x%04X seq=%u\n", (int)peer, (unsigned)in->slot,
               (unsigned)in->npc_id, (unsigned)in->seq);
    }
    if (h->peer_mask != 0 && h->npc_id != in->npc_id) {
        h->peer_mask = 0; /* stale mask for a previous occupant of the slot */
    }
    h->npc_id = in->npc_id;
    h->last_ms = pcnetgame_now_ms();
    if ((h->peer_mask & (uint16_t)(1u << peer)) == 0) {
        h->peer_mask = (uint16_t)(h->peer_mask | (uint16_t)(1u << peer));
        printf("[NPC][TALKNET] HOLD slot=%u npc=0x%04X peers=0x%04X\n", (unsigned)in->slot, (unsigned)in->npc_id,
               (unsigned)h->peer_mask);
    }
}

/* See pc_net_game.h. Host-only gate query (called every frame per villager by ac_npc_move.c_inc). */
int pc_net_game_host_npc_talk_held(int slot, int npc_id) {
    PCNetGameNpcTalkHold* h;

    if (s_role != PC_NETGAME_ROLE_HOST || slot < 0 || slot >= ANIMAL_NUM_MAX) {
        return 0;
    }
    h = &s_host_talk_hold[slot];
    if (h->peer_mask == 0 || (int)h->npc_id != npc_id) {
        return 0;
    }
    if ((uint32_t)(pcnetgame_now_ms() - h->last_ms) >= pcnetgame_talk_hold_timeout_ms()) {
        printf("[NPC][TALKNET] EXPIRE slot=%d npc=0x%04X peers=0x%04X\n", slot, (unsigned)h->npc_id,
               (unsigned)h->peer_mask);
        h->peer_mask = 0;
        return 0;
    }
    return 1;
}

int pc_net_game_npc_talk_session_epoch(void) {
    return s_npc_talk_epoch;
}

/* See pc_net_game.h. Client only; returns 1 iff the message was queued. `refresh` = keepalive re-send of a BEGIN
 * (same wire message; silent unless PC_NPC_TALKHOLD_DIAG=1). */
static int pcnetgame_send_npc_talk(int slot, uint16_t npc_id, int begin, int refresh) {
    PCNetGameNpcTalkMsg msg;
    static int s_fail_logs = 0;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (slot < 0 || slot >= ANIMAL_NUM_MAX) {
        return 0;
    }
    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_NPC_TALK;
    msg.slot = (uint8_t)slot;
    msg.flags = begin ? (uint8_t)PC_NETGAME_NPC_TALK_FLAG_BEGIN : 0;
    msg.npc_id = npc_id;
    msg.seq = (uint16_t)(s_npc_talk_seq + 1u);
    if (!pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
        if (s_fail_logs < 5) {
            s_fail_logs++;
            printf("[NPC][TALKNET] SEND %s FAILED slot=%d npc=0x%04X (will %s)\n", begin ? "begin" : "end", slot,
                   (unsigned)npc_id, begin ? "retry" : "rely on the host timeout");
        }
        return 0;
    }
    s_npc_talk_seq = msg.seq;
    if (!refresh) {
        printf("[NPC][TALKNET] SEND %s slot=%d npc=0x%04X seq=%u\n", begin ? "begin" : "end", slot,
               (unsigned)npc_id, (unsigned)msg.seq);
    } else if (pcnetgame_talk_diag()) {
        printf("[NPC][TALKNET][DIAG] SEND refresh slot=%d npc=0x%04X seq=%u\n", slot, (unsigned)npc_id,
               (unsigned)msg.seq);
    }
    return 1;
}

int pc_net_game_notify_local_npc_talk(int slot, uint16_t npc_id, int begin) {
    int ok = pcnetgame_send_npc_talk(slot, npc_id, begin, 0);
    if (slot >= 0 && slot < ANIMAL_NUM_MAX) {
        if (begin && ok) {
            s_client_talk_out[slot].active = 1;
            s_client_talk_out[slot].npc_id = npc_id;
            s_client_talk_out[slot].last_ms = pcnetgame_now_ms();
        } else if (!begin) {
            s_client_talk_out[slot].active = 0; /* END sent (or attempted: the host timeout covers a lost END) */
        }
    }
    return ok;
}

/* Client: per-poll keepalive tick (see s_client_talk_out). */
static void pcnetgame_client_talk_refresh_tick(void) {
    int slot;
    uint32_t now = 0;
    uint32_t interval = pcnetgame_talk_refresh_ms();
    for (slot = 0; slot < ANIMAL_NUM_MAX; slot++) {
        PCNetGameNpcTalkOut* o = &s_client_talk_out[slot];
        if (!o->active) {
            continue;
        }
        if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
            o->active = 0;
            continue;
        }
        if (interval == 0) {
            continue;
        }
        if (now == 0) {
            now = pcnetgame_now_ms();
        }
        if ((uint32_t)(now - o->last_ms) < interval) {
            continue;
        }
        if (s_client_talk_probe == NULL || !s_client_talk_probe(slot, o->npc_id)) {
            o->active = 0; /* lease gone/unknown: stop refreshing (the END path or the host timeout releases) */
            continue;
        }
        (void)pcnetgame_send_npc_talk(slot, o->npc_id, 1, 1);
        o->last_ms = now; /* also on a failed send: retry one interval later, never every poll */
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
    pcnetgame_reset_host_bury_state(peer); /* World Ecology T0 scaffolding -- see its own doc */
    if (peer >= 0 && peer < PC_NET_MAX_PEERS) {
        /* v2: identity deferral, PLAYER_CONTEXT, snapshot progress/epoch. v2 additionally calls this
         * on PC_NET_EVENT_PEER_CONNECTED (a freshly allocated transport slot starts clean even if a
         * disconnect was somehow never observed) and on every host-initiated reject/drop. */
        memset(&s_host_peer[peer], 0, sizeof(s_host_peer[peer]));
        s_host_peer[peer].bound_resident_idx = -1; /* M9 identity Stage 1A: no resident binding */
        /* World Ecology Stage 1: this peer's field-action dedup record -- a dead/reused peer's last
         * decision must never be replayed against a NEW connection's request_id space (request ids
         * are not shared across peers, so this cheaply avoids any chance of stale cross-peer replay). */
        memset(&s_host_field_action_dedup[peer], 0, sizeof(s_host_field_action_dedup[peer]));
        /* World Ecology: snowmen -- same reasoning as the field-action dedup record just above, for
         * SNOWMAN_BUILD_REQUEST's own separate dedup table. */
        memset(&s_host_snowman_build_dedup[peer], 0, sizeof(s_host_snowman_build_dedup[peer]));
        /* World Ecology Wildlife Sync T-catch -- same reasoning again, for CATCH_REQUEST's own
         * separate dedup table. */
        memset(&s_host_catch_dedup[peer], 0, sizeof(s_host_catch_dedup[peer]));
        /* M9-C: this peer's villager talk holds + sequence record (a dead/reused peer must never keep X frozen). */
        pcnetgame_host_talk_hold_clear_peer(peer, "reset", 1);
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
 * Money bags are excluded here too, by the same pcnetgame_is_money_bag_item() test, but this is now a
 * DROP-only policy -- pickup no longer mirrors it. Historically pickup used the identical test to
 * reject a money bag outright (pcnetgame_validate_and_resolve_pickup() used to refuse it), so pickup
 * and drop took the same position; the World Ecology Stage 1 Bug 4 fix removed that pickup-side
 * exclusion (a money-rock-dropped bag, and any other field money bag, is now an ordinary
 * NAME_TYPE_ITEM1 field item any client can pick up through the normal path -- see
 * pcnetgame_validate_and_resolve_pickup()'s own "Bug 4 fix" comment). Drop deliberately still refuses
 * money bags for the reasons below: without this, a money item would pass the NAME_TYPE_ITEM1 case
 * below just like any ordinary pocket item: the money bags (0x2100-0x2103, ITEM1_CAT_MONEY) and
 * ITM_MONEY1000BELL (0x250D, ITEM1_CAT_ETC) are the only entries mNT_get_itemTableNo() maps to
 * mNT_ITEM_TYPE_BAG (src/game/m_name_table.c:100,127). Note that a money bag CAN reach a pocket slot
 * through vanilla's own UI. The wallet's "make a sack" action (mTG_make_money_sack(),
 * src/game/m_tag_ovl.c) puts one in the hand for pocketing, and PC_ENHANCEMENTS' full-wallet pickup
 * fallback (m_player_main_pickup.c_inc) pockets one too. ITEM1_CAT_MONEY also gets the ordinary
 * mTG_TYPE_FIELD_DEFAULT tag menu, which includes "put on ground". So without this drop-side check, a
 * client's money-bag drop would be accepted and placed as a plain field item, which is still not a
 * deliberate path here even after Bug 4: dropping one back onto the field would let it re-enter the
 * ordinary pickup path above and be picked up by anyone (including crediting a DIFFERENT client's
 * wallet than the one that originally received it), which is not something this protocol's simple
 * field-value model is designed to arbitrate. Rejecting the drop instead gives the client vanilla's
 * own local "can't place that" feedback (the m_tag_ovl.c drop seam calls
 * pc_net_game_is_droppable_item() before sending), and
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

/* World Ecology T3: does `item` classify as an item this build's host-authoritative bury path will
 * accept as claimed_item? Deliberately NOT pcnetgame_is_droppable_item() reused -- bury has its own
 * distinct rule set (see this milestone's design brief): REJECT EMPTY_NO, RSV_NO (via
 * mNT_check_unknown() for any ITEM1/FTR1 id outside the game's tables -- REQUIRED, not decoration,
 * for the exact out-of-bounds-read reason pcnetgame_is_droppable_item() already documents),
 * ITM_SIGNBOARD, any ITEM1_CAT_INSECT item, any ITEM1_CAT_FISH item, and HONEYCOMB. ACCEPT money-bag
 * items (ITM_MONEY_*), FTR0/FTR1 furniture, and every other ITEM1 item -- unlike drop, bury does NOT
 * exclude money bags (burying one into a HOLE_SHINE tile is the money-tree mechanic itself; see the
 * BURY commit branch in pcnetgame_handle_host_confirm()). Present-wrapped/quest-flagged items never
 * reach this path per vanilla's own menu gating (mTG_bury_proc()/mTG_plant_proc() only offer this menu
 * entry for an already-unwrapped pocket item), so condition bits are not re-checked here. */
static int pcnetgame_is_buryable_item(mActor_name_t item) {
    if (item == (mActor_name_t)EMPTY_NO || item == ITM_SIGNBOARD || item == (mActor_name_t)HONEYCOMB) {
        return 0;
    }
    if (mNT_check_unknown(item)) {
        return 0; /* ITEM1/FTR1 id outside the game's tables (incl. RSV_NO): never written into the field */
    }
    if (ITEM_NAME_GET_TYPE(item) == NAME_TYPE_ITEM1 &&
        (ITEM_NAME_GET_CAT(item) == ITEM1_CAT_INSECT || ITEM_NAME_GET_CAT(item) == ITEM1_CAT_FISH)) {
        return 0; /* live catches are never buryable -- see this function's own doc */
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

/* World Ecology T3: TRUST BOUNDARY range check for a bury request's hole_variant -- unlike
 * pcnetgame_validate_hole_variant() (dig-family: 0..24 only), bury's own valid set ALSO includes the
 * 0xFF sentinel (mCoBG_GetHoleNumber()'s own -1 "no valid hole shape" case -- see
 * PCNetGameBuryRequestMsg's doc). A separate function rather than extending
 * pcnetgame_validate_hole_variant() itself: that function's existing 0..24-only contract is relied on
 * verbatim by every dig-family validator, and must not change. */
static int pcnetgame_validate_bury_hole_variant(uint8_t v) {
    return v <= 24u || v == 0xFFu;
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

    if (!pcnetgame_is_pickupable_field_item(raw_item) || mNT_check_unknown(raw_item)) {
        return 0; /* wrong classification (incl. transient RSV_NO) */
    }
    /* Bug 4 fix: a field money-bag (e.g. one a money-rock hit dropped) is no longer specially
       excluded here -- it is an ordinary NAME_TYPE_ITEM1 field item, already accepted above by
       pcnetgame_is_pickupable_field_item(), and pickupable by any client through this same ordinary
       path. pcnetgame_is_money_bag_item() remains in use elsewhere: DROP still refuses money bags
       (pcnetgame_is_droppable_item(), a separate deliberate policy), and the client pickup-result
       handler still uses it to detect a wallet-creditable item. */

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
    st->snap_snowman_sent = 0;
    /* World Ecology Wildlife Sync T2: a fresh/restarted epoch always redoes stage 3 (WILDLIFE_
     * SNAPSHOT_BEGIN/ENTRY-then-END) from scratch too, exactly like every other snapshot sub-stage
     * here. */
    st->snap_wildlife_begin_sent = 0;
    st->snap_next_wildlife_idx = 0;
    st->snap_wildlife_sent_count = 0;
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

/* ==================== World Ecology: snowmen (host-authoritative build/break/melt sync) ==================== */

/* Finding A fix / shared helper: ac_birth_control.c's own SNOWMAN0..8 special-case
 * (aBC_setupOtherActor(), m_actor.c) sets clear_item = EMPTY_NO for a SNOWMAN0..8 tile the moment its
 * PSNOWMAN actor spawns, and only restores the real tile value at that actor's OWN destruction
 * (restore_fgdata(), m_actor.c:841-844,922, gated on actor->restore_fg -- set TRUE at spawn,
 * m_actor.c:922-ish). So for as long as a snowman's actor is alive, Save_t.fg itself reads EMPTY_NO at
 * that tile -- reading it directly (as pcnetgame_host_diff_commit_acre()'s own pcfa_read_acre() does)
 * would broadcast EMPTY_NO as if it were the host's own authoritative value, making that snowman
 * flicker/vanish for every OTHER client while the host's own actor is alive. This resolves what tile
 * (acre, tile) should actually be treated as holding: if *value is EMPTY_NO and a live PSNOWMAN actor's
 * home position maps to this exact (acre, tile), overlays *value with that actor's own npc_id (its
 * SNOWMAN0+slot*3+age tile value). A no-op (0) unless the HOST's own process actually has the town
 * scene loaded right now (gamePT->exec == play_main, pcfa_scene_is_town()) -- which is also the only
 * time ac_birth_control.c's EMPTY_NO substitution is even in effect for THIS host's own Save_t.fg.
 * Shared verbatim by pcnetgame_host_diff_commit_acre() (this fix), and by the SNOWMAN_BUILD/
 * SNOWMAN_BREAK validators below (so a rejected-looking-free tile that is actually still a live
 * actor's home is never misread as free, and a break target still recognizes its own live actor). */
static int pcnetgame_host_resolve_snowman_tile_overlay(int acre, int tile, uint16_t* value) {
    GAME_PLAY* play;
    ACTOR* actor;

    if (*value != (uint16_t)EMPTY_NO) {
        return 0;
    }
    if (gamePT == NULL || gamePT->exec != play_main || !pcfa_scene_is_town()) {
        return 0;
    }
    play = (GAME_PLAY*)gamePT;
    for (actor = play->actor_info.list[ACTOR_PART_BG].actor; actor != NULL; actor = actor->next_actor) {
        int ut_x, ut_z, home_acre, home_tile;

        if (!actor->restore_fg) {
            continue;
        }
        if (actor->npc_id < (mActor_name_t)SNOWMAN0 || actor->npc_id > (mActor_name_t)SNOWMAN8) {
            continue;
        }
        if (!mFI_Wpos2UtNum(&ut_x, &ut_z, actor->home.position)) {
            continue;
        }
        if (!pcfa_town_ut_to_acre_tile(ut_x, ut_z, &home_acre, &home_tile)) {
            continue;
        }
        if (home_acre == acre && home_tile == tile) {
            *value = (uint16_t)actor->npc_id;
            return 1;
        }
    }
    return 0;
}

/* Host-only: this stream's own world_seq (independent of s_world_seq -- see PCNetGameSnowmanStateMsg's
 * own doc) and the last-broadcast 16 bytes (12 bytes of Save_t.snowmen.snowmen_data[3] + 4 date bytes),
 * used purely to detect a change worth broadcasting (pcnetgame_host_check_snowman_state_diff()). Not
 * valid (cache_valid == 0) until the first check ever runs, so that first check always broadcasts once
 * (matching every other "first sample always sends" cache in this file, e.g. s_host_meta_valid). */
static uint32_t s_snowman_world_seq = 0;
static uint8_t  s_snowman_state_cache[16];
static int      s_snowman_state_cache_valid = 0;

/* Gathers the current 16 bytes this stream tracks -- Save_t.snowmen.snowmen_data[3] (12 bytes, 4/slot:
 * exists, head_size, body_size, score) followed by snowman_year/month/day/hour (4 bytes) -- BY HAND,
 * never as one memcpy: these fields are not contiguous in Save_t (m_common_data.h:109,132-135). */
static void pcnetgame_build_snowman_state_bytes(uint8_t out[16]) {
    mSN_snowman_data_c* data = Save_GetPointer(snowmen.snowmen_data[0]);
    int i;

    for (i = 0; i < mSN_SAVE_COUNT; i++) {
        out[i * 4 + 0] = data[i].exists;
        out[i * 4 + 1] = data[i].head_size;
        out[i * 4 + 2] = data[i].body_size;
        out[i * 4 + 3] = data[i].score;
    }
    out[12] = Save_Get(snowman_year);
    out[13] = Save_Get(snowman_month);
    out[14] = Save_Get(snowman_day);
    out[15] = Save_Get(snowman_hour);
}

/* Broadcasts one freshly-built PC_NETGAME_MSG_SNOWMAN_STATE (world_seq == the CURRENT
 * s_snowman_world_seq, `flags` as given by the caller) to every READY peer. Deliberately mirrors
 * pcnetgame_broadcast_villager_msg()'s send-failure handling (a full window / failed send falls back
 * to a full resync for that one peer) but NOT its snap_active skip -- see PCNetGameSnowmanStateMsg's
 * own doc for why a peer mid-snapshot must still receive every one of these. */
static void pcnetgame_broadcast_snowman_state(uint8_t flags) {
    PCNetGameSnowmanStateMsg msg;
    uint8_t bytes[16];
    int i;

    pcnetgame_build_snowman_state_bytes(bytes);

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_SNOWMAN_STATE;
    msg.flags = flags;
    msg.world_seq = s_snowman_world_seq;
    memcpy(msg.snowmen, bytes, sizeof(msg.snowmen));
    msg.year = bytes[12];
    msg.month = bytes[13];
    msg.day = bytes[14];
    msg.hour = bytes[15];

    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        int backlog;
        if (s_host_peer_link[i] != PC_NETGAME_LINK_READY) {
            continue;
        }
        backlog = pc_net_reliable_backlog((PCNetPeerId)i);
        if (backlog < 0) {
            continue;
        }
        if (backlog + 1 > PC_NET_RELIABLE_WINDOW - PC_NETGAME_WINDOW_HEADROOM ||
            !pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
            pcnetgame_host_start_snapshot((PCNetPeerId)i, "snowman state send failed");
        }
    }
}

/* THE generic snowman-state commit function: called explicitly, right after BOTH the BUILD and BREAK
 * commits below (so SNOWMAN_STATE reaches clients as promptly as their own tile change), AND once per
 * pcnetgame_host_world_poll() call, immediately BEFORE that poll's pcnetgame_host_flush_mask() -- this
 * second call site is what catches melt (mAGrw_MeltSnowman()/mAGrw_AllMeltSnowman(), m_all_grow_ovl.c,
 * already host-gated by mFM_PcFieldInitGrowth()) and any other cause of a Save_t.snowmen/date change,
 * with no per-cause plumbing needed. memcmp against the cached last-broadcast bytes; a genuine
 * difference (or the very first call, cache_valid == 0) increments s_snowman_world_seq and broadcasts.
 * No-op until the host world is ready (save loaded) -- mirrors pcnetgame_host_flush_mask()'s own gate. */
static void pcnetgame_host_check_snowman_state_diff(void) {
    uint8_t bytes[16];

    if (!s_host_world_ready || !pcfa_save_ready()) {
        return;
    }
    pcnetgame_build_snowman_state_bytes(bytes);
    if (s_snowman_state_cache_valid && memcmp(bytes, s_snowman_state_cache, sizeof(bytes)) == 0) {
        return;
    }
    memcpy(s_snowman_state_cache, bytes, sizeof(bytes));
    s_snowman_state_cache_valid = 1;
    ++s_snowman_world_seq;
    pcnetgame_broadcast_snowman_state(0);
    if (g_pc_verbose) {
        printf("[NET][SNOWMAN] host: state changed -- broadcast SNOWMAN_STATE (snowman_world_seq %u)\n",
               (unsigned)s_snowman_world_seq);
    }
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
        if (v == (uint16_t)EMPTY_NO && pcnetgame_host_resolve_snowman_tile_overlay(acre, t, &v)) {
            items[t] = v; /* Finding A fix: a live PSNOWMAN actor still owns this tile -- see
                              pcnetgame_host_resolve_snowman_tile_overlay()'s own doc */
        }
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
    if (!s_host_meta_world_state_valid ||
        cur_state.gyoei_term != s_host_meta_world_state.gyoei_term ||
        cur_state.gyoei_term_transition_offset != s_host_meta_world_state.gyoei_term_transition_offset ||
        cur_state.insect_term != s_host_meta_world_state.insect_term ||
        cur_state.insect_term_transition_offset != s_host_meta_world_state.insect_term_transition_offset) {
        changed_flags |= (uint8_t)PC_NETGAME_META_FLAG_TERM_VALID;
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
    if (changed_flags & PC_NETGAME_META_FLAG_TERM_VALID) {
        printf("[NET][WORLD] host: fish/bug term changed -> gyoei_term=%u insect_term=%u (world_seq %u)\n",
               (unsigned)cur_state.gyoei_term, (unsigned)cur_state.insect_term, (unsigned)s_world_seq);
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

/* N-clock milestone: builds one CLOCK_SYNC message from the CURRENT host clock (OSGetTime() +
 * Save_Get(time_delta), read fresh at call time -- never cached) and the given flags. Does not touch
 * s_host_clock_seq; callers own bumping it exactly once per distinct broadcast/send event (see the
 * two send helpers below), matching s_world_seq's own "caller bumps, builder just stamps" convention. */
static void pcnetgame_build_clock_sync_msg(PCNetGameClockSyncMsg* msg, uint8_t flags) {
    memset(msg, 0, sizeof(*msg));
    msg->msg_type = (uint8_t)PC_NETGAME_MSG_CLOCK_SYNC;
    msg->flags = flags;
    msg->clock_seq = s_host_clock_seq;
    msg->host_game_ticks = (int64_t)(OSGetTime() + Save_Get(time_delta));
}

/* N-clock milestone: the initial sync for ONE peer, sent right after its SNAPSHOT_END (join/late-join/
 * reconnect) -- see the call site in pcnetgame_host_pump_snapshots() below. Always its own fresh
 * clock_seq (the shared counter is bumped here), so it is unconditionally newer than that peer's
 * freshly-reset s_client_clock_seq_applied (0, or whatever an earlier peer's unrelated broadcast last
 * left it at -- either way this send's clock_seq is greater) and therefore always applied by the
 * client's Rule 1 gate the moment it is READY with world latched. Not gated on backlog/snap_active
 * (the snapshot for this exact peer just finished) -- a dropped send here is not fatal since the next
 * periodic broadcast (<=30s later) or a future reconnect will cover it. */
static void pcnetgame_host_send_clock_sync_initial(PCNetPeerId peer) {
    PCNetGameClockSyncMsg msg;
    ++s_host_clock_seq;
    pcnetgame_build_clock_sync_msg(&msg, 0);
    if (!pc_net_send(peer, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
        if (g_pc_verbose) {
            printf("[NET][CLOCK] host: initial CLOCK_SYNC to peer %d could not be queued (window full)\n",
                   (int)peer);
        }
        return;
    }
    if (g_pc_verbose) {
        OSCalendarTime cal;
        OSTicksToCalendarTime((OSTime)msg.host_game_ticks, &cal);
        printf("[NET][CLOCK] host: initial CLOCK_SYNC sent to peer %d (clock_seq %u) -- host time "
               "%04d-%02d-%02d %02d:%02d:%02d weekday=%d Kabu price (from schedule)=%u\n",
               (int)peer, (unsigned)msg.clock_seq, cal.year, cal.mon + 1, cal.mday, cal.hour, cal.min,
               cal.sec, cal.wday, (unsigned)Save_Get(kabu_price_schedule.daily_price[cal.wday]));
    }
}

/* N-clock milestone: one broadcast event (periodic cadence or a detected manual clock-adjust
 * discontinuity) -- ONE clock_seq bump, ONE host_game_ticks sample, sent identically to every READY
 * peer. Peers with an active snapshot are skipped (mirroring pcnetgame_host_check_world_meta()'s own
 * convention exactly): they get their own initial sync moments later, right when their SNAPSHOT_END
 * goes out (see pcnetgame_host_send_clock_sync_initial() above), so skipping them here never leaves a
 * peer permanently uncovered. Runs with zero, one, or many READY peers -- with zero this still bumps
 * s_host_clock_seq and updates the host's own free-running clock is unaffected either way (the host is
 * never offset -- see s_pc_net_clock_offset's doc comment, lb_rtc.c). */
static void pcnetgame_host_broadcast_clock_sync(uint8_t flags) {
    PCNetGameClockSyncMsg msg;
    int i;

    ++s_host_clock_seq;
    pcnetgame_build_clock_sync_msg(&msg, flags);
    if (flags & PC_NETGAME_CLOCK_SYNC_FLAG_DISCONTINUITY) {
        printf("[NET][CLOCK] host: manual clock adjustment detected -- broadcasting CLOCK_SYNC "
               "(discontinuity, clock_seq %u, host_game_ticks %lld)\n",
               (unsigned)s_host_clock_seq, (long long)msg.host_game_ticks);
    } else if (g_pc_verbose) {
        printf("[NET][CLOCK] host: periodic CLOCK_SYNC broadcast (clock_seq %u, host_game_ticks %lld)\n",
               (unsigned)s_host_clock_seq, (long long)msg.host_game_ticks);
    }
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_host_peer_link[i] != PC_NETGAME_LINK_READY || s_host_peer[i].snap_active) {
            continue;
        }
        if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg)) && g_pc_verbose) {
            printf("[NET][CLOCK] host: CLOCK_SYNC to peer %d could not be queued (window full)\n", i);
        }
    }
}

/* N-clock milestone, host side: watches Save_Get(time_delta) for a change between polls (mirroring
 * pcnetgame_host_check_world_meta()'s own before-vs-after watch pattern exactly -- no new hook into
 * lbRTC_SetTime() needed) to detect a manual clock adjustment via the in-game menu, and otherwise
 * broadcasts on a plain PC_NETGAME_CLOCK_SYNC_INTERVAL_MS real-time cadence. Runs once per
 * pcnetgame_host_world_poll() call, after the renew-time/weather/market watch. The very first call
 * after the host world becomes ready only records the current time_delta/timer baseline and sends
 * nothing here -- every already-READY-or-about-to-be-READY peer gets its own initial sync via
 * pcnetgame_host_send_clock_sync_initial() at its SNAPSHOT_END, so no peer is ever left waiting up to
 * a full 30s for its first clock correction. */
static void pcnetgame_host_check_clock_sync(void) {
    OSTime cur_delta;

    if (!s_host_world_ready) {
        return;
    }
    cur_delta = Save_Get(time_delta);
    if (!s_host_clock_last_delta_valid) {
        s_host_clock_last_delta = cur_delta;
        s_host_clock_last_delta_valid = 1;
        s_host_clock_sync_last_ms = pcnetgame_now_ms();
        return;
    }
    if (cur_delta != s_host_clock_last_delta) {
        s_host_clock_last_delta = cur_delta;
        pcnetgame_host_broadcast_clock_sync((uint8_t)PC_NETGAME_CLOCK_SYNC_FLAG_DISCONTINUITY);
        s_host_clock_sync_last_ms = pcnetgame_now_ms();
        return;
    }
    if ((uint32_t)(pcnetgame_now_ms() - s_host_clock_sync_last_ms) >= PC_NETGAME_CLOCK_SYNC_INTERVAL_MS) {
        pcnetgame_host_broadcast_clock_sync(0);
        s_host_clock_sync_last_ms = pcnetgame_now_ms();
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
/* Forward-declared: defined near the other friendship-sync host-side functions, below; used here
 * (snap_stage 4) before that point in the file. */
static int pcnetgame_build_friendship_snapshot_entry(PCNetGameFriendshipSnapshotEntryMsg* fe, int slot,
                                                      int memory_idx);
/* World Ecology Wildlife Sync T2: defined near the other wildlife-sync host-side functions, below;
 * used here (snap_stage 3, inserted between VILLAGER_SNAPSHOT and FRIENDSHIP_SNAPSHOT_ENTRY) before
 * that point in the file. */
static int pcnetgame_build_wildlife_snapshot_entry(PCNetGameWildlifeSnapshotEntryMsg* we, int slot,
                                                    uint32_t epoch);

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
            } else if (st->snap_stage == 1 && !st->snap_snowman_sent) {
                /* World Ecology: snowmen -- one-shot, sent right after SNAPSHOT_BEGIN succeeds, before
                 * the first FIELD_BLOCK (see PCNetGameHostPeerState.snap_snowman_sent's own doc for
                 * why: a late-joiner must never see a SNOWMAN0..8 tile arrive before the slot/size/
                 * score/date data that explains it). Retried next poll on a failed send, exactly like
                 * every other snapshot sub-stage here. */
                PCNetGameSnowmanStateMsg sm;
                uint8_t bytes[16];

                pcnetgame_build_snowman_state_bytes(bytes);
                memset(&sm, 0, sizeof(sm));
                sm.msg_type = (uint8_t)PC_NETGAME_MSG_SNOWMAN_STATE;
                sm.flags = (uint8_t)PC_NETGAME_SNOWMAN_STATE_FLAG_IN_SNAPSHOT;
                sm.world_seq = s_snowman_world_seq;
                memcpy(sm.snowmen, bytes, sizeof(sm.snowmen));
                sm.year = bytes[12];
                sm.month = bytes[13];
                sm.day = bytes[14];
                sm.hour = bytes[15];
                if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &sm, (uint16_t)sizeof(sm))) {
                    break;
                }
                st->snap_snowman_sent = 1;
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
                st->snap_wildlife_begin_sent = 0;
                st->snap_next_wildlife_idx = 0;
                st->snap_wildlife_sent_count = 0;
            } else if (st->snap_stage == 3 && !st->snap_wildlife_begin_sent) {
                /* World Ecology Wildlife Sync T2: one-shot, opens this peer's wildlife-snapshot
                 * sub-pass -- see PCNetGameWildlifeSnapshotBeginMsg's own doc. Sent after
                 * VILLAGER_SNAPSHOT (no ordering dependency on it, just a convenient, already-
                 * established insertion point) and before the first WILDLIFE_SNAPSHOT_ENTRY. */
                PCNetGameWildlifeSnapshotBeginMsg wb;
                memset(&wb, 0, sizeof(wb));
                wb.msg_type = (uint8_t)PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_BEGIN;
                wb.epoch = st->snap_epoch;
                wb.generation = pcwld_session_generation();
                wb.count = (uint32_t)pcwld_active_count();
                if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &wb, (uint16_t)sizeof(wb))) {
                    break;
                }
                st->snap_wildlife_begin_sent = 1;
            } else if (st->snap_stage == 3) {
                /* World Ecology Wildlife Sync T2: one PCNetGameWildlifeSnapshotEntryMsg per ACTIVE
                 * pc_wildlife_authority.c table slot, flat-iterated over the WHOLE table (every acre
                 * unconditionally -- see that struct's own doc), empty slots skipped without sending,
                 * mirroring the friendship-snapshot flat-iteration-with-skip convention immediately
                 * below exactly. */
                static PCNetGameWildlifeSnapshotEntryMsg we; /* 28 B, built and sent synchronously */
                int found = 0;
                while (st->snap_next_wildlife_idx < PCWLD_PUBLIC_MAX_ENTITIES) {
                    if (pcnetgame_build_wildlife_snapshot_entry(&we, st->snap_next_wildlife_idx, st->snap_epoch)) {
                        found = 1;
                        break;
                    }
                    st->snap_next_wildlife_idx++;
                }
                if (!found) {
                    /* Every slot scanned (whole table empty, or we've reached the end) -- close this
                     * sub-pass with WILDLIFE_SNAPSHOT_END rather than advancing snap_stage directly,
                     * so the client always gets an END to trigger its reconciliation pass, even for a
                     * completely empty authoritative table (0 entries is an entirely ordinary,
                     * expected outcome -- e.g. a fresh town with nobody having waded yet). */
                    PCNetGameWildlifeSnapshotEndMsg we_end;
                    memset(&we_end, 0, sizeof(we_end));
                    we_end.msg_type = (uint8_t)PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_END;
                    we_end.epoch = st->snap_epoch;
                    we_end.generation = pcwld_session_generation();
                    we_end.count_sent = (uint32_t)st->snap_wildlife_sent_count;
                    if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &we_end, (uint16_t)sizeof(we_end))) {
                        break;
                    }
                    st->snap_stage = 4;
                    st->snap_next_friendship_idx = 0;
                    sent++; /* still counts against the budget -- a fully-empty table must not spin */
                    continue;
                }
                if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &we, (uint16_t)sizeof(we))) {
                    break;
                }
                st->snap_wildlife_sent_count++;
                st->snap_next_wildlife_idx++;
            } else if (st->snap_stage == 4) {
                /* Friendship/mail sync milestone: one PCNetGameFriendshipSnapshotEntryMsg per
                 * OCCUPIED Anmmem_c entry, flat-iterated over (slot, memory_idx) pairs; empty
                 * entries are skipped without sending (see the struct's own doc comment). */
                static PCNetGameFriendshipSnapshotEntryMsg fe; /* 292 B, built and sent synchronously */
                int found = 0;
                while (st->snap_next_friendship_idx < ANIMAL_NUM_MAX * ANIMAL_MEMORY_NUM) {
                    int slot = st->snap_next_friendship_idx / ANIMAL_MEMORY_NUM;
                    int memory_idx = st->snap_next_friendship_idx % ANIMAL_MEMORY_NUM;
                    if (pcnetgame_build_friendship_snapshot_entry(&fe, slot, memory_idx)) {
                        found = 1;
                        break;
                    }
                    st->snap_next_friendship_idx++;
                }
                if (!found) {
                    st->snap_stage = 5;
                    sent++; /* still counts against the budget -- a fully-empty town must not spin */
                    continue;
                }
                if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &fe, (uint16_t)sizeof(fe))) {
                    break;
                }
                st->snap_next_friendship_idx++;
                if (st->snap_next_friendship_idx >= ANIMAL_NUM_MAX * ANIMAL_MEMORY_NUM) {
                    st->snap_stage = 5;
                }
            } else {
                PCNetGameSnapshotEndMsg e;
                memset(&e, 0, sizeof(e));
                e.msg_type = (uint8_t)PC_NETGAME_MSG_SNAPSHOT_END;
                e.grid = (uint8_t)PC_NETGAME_GRID_TOWN;
                e.acre_count = (uint8_t)st->snap_blocks_sent;
                e.flags = (uint8_t)(PC_NETGAME_META_FLAG_RENEW_TIME_VALID | PC_NETGAME_META_FLAG_WEATHER_VALID |
                                    PC_NETGAME_META_FLAG_MARKET_VALID | PC_NETGAME_META_FLAG_TERM_VALID);
                e.epoch = st->snap_epoch;
                e.world_seq = s_world_seq;
                e.renew_time = s_host_meta_renew; /* == the Save value: the meta check ran this poll */
                e.world_state = s_host_meta_world_state; /* == current weather + market: same guarantee */
                if (!pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &e, (uint16_t)sizeof(e))) {
                    break;
                }
                st->snap_active = 0;
                /* N-clock milestone: this peer's snapshot (initial join, late-join/RESYNC_REQUEST, or
                 * reconnect) just closed -- send its initial CLOCK_SYNC now, unconditionally, per the
                 * design's "always applied unconditionally (first sync)" rule. See
                 * pcnetgame_host_send_clock_sync_initial()'s doc comment above for why this is always
                 * newer than the peer's just-reset s_client_clock_seq_applied. */
                pcnetgame_host_send_clock_sync_initial((PCNetPeerId)i);
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
        pcnetgame_host_peer_scene_gone(peer);
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
        pcnetgame_host_peer_scene_gone(peer);
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
/* Forward-declared: defined near the other World Ecology T1 tree-cut host-side state, below; used here
 * (town-change branch) before that point in the file -- see s_host_tree_cut_count's own doc. */
static void pcnetgame_reset_host_tree_cut_state(void);

static void pcnetgame_host_revalidate_bound_peers(void); /* defined with the Stage 1A identity helpers */

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
        /* World Ecology Stage 1, Item 2 / review finding: a money-rock bookkeeping slot stores
         * acre/tile indices into the OLD town's field data. Without this, the SAME running host
         * process could carry stale s_host_money_rock[] entries into a genuinely different town
         * (this branch is reached precisely because the town changed without a process restart --
         * e.g. the host travelled through a friend's gate or loaded a different save), where those
         * acre/tile indices could coincidentally alias real tiles in the new town's field and later
         * be wrongly reverted/expired against them. Matches pcnetgame_reset_host_world_state()'s own
         * full-session-reset clear of this same table. */
        memset(s_host_money_rock, 0, sizeof(s_host_money_rock));
        /* World Ecology T1 review fix: the tree-cut cut-count table has the exact same stale-town
         * hazard as s_host_money_rock above (a per-tile in-progress hit count surviving into a
         * genuinely different town's field, where its acre/tile indices could coincidentally alias a
         * real tile and wrongly resume mid-chop instead of requiring the full hit count again). Reset
         * alongside the money-rock table, in this same branch. */
        pcnetgame_reset_host_tree_cut_state();
        /* World Ecology Wildlife Sync T0: a wildlife record's bx/bz is only meaningful for the town
         * it was created in -- same stale-town hazard as s_host_money_rock/s_host_tree_cut_count
         * above, reset alongside them in this same branch. */
        pcwld_reset();
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
        pcnetgame_host_revalidate_bound_peers(); /* the host's save/resident may have changed while paused */
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
    /* World Ecology: snowmen -- runs BEFORE this poll's flush so a melt/other off-tile Save_t.snowmen
     * change is always broadcast no later than the tile change it may accompany (see
     * pcnetgame_host_check_snowman_state_diff()'s own doc for why this catches melt with no per-cause
     * plumbing; BUILD/BREAK additionally call it directly, right after their own commit). */
    pcnetgame_host_check_snowman_state_diff();
    pcnetgame_host_flush_mask(mask);
    pcnetgame_host_check_world_meta();
    pcnetgame_host_check_clock_sync();
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

/* World Ecology T3: sends a BURY_RESULT. `buried_item` on accept is the claimed_item echo (see
 * PCNetGameBuryResultMsg's own doc); on reject it is the reconciliation echo (the host's CURRENT tile
 * value) and `flags`/`reason` carry the reconciliation bits -- callers pass 0 for a plain, no-echo
 * rejection (nothing valid could be read) or the result of pcnetgame_bury_echo_current() otherwise. */
static void pcnetgame_host_send_bury_result(PCNetPeerId peer, uint32_t request_id, uint8_t ut_x, uint8_t ut_z,
                                            int accepted, uint16_t buried_item, uint8_t flags, uint8_t reason) {
    PCNetGameBuryResultMsg out;
    memset(&out, 0, sizeof(out));
    out.msg_type = (uint8_t)PC_NETGAME_MSG_BURY_RESULT;
    out.accepted = (uint8_t)(accepted != 0);
    out.ut_x = ut_x;
    out.ut_z = ut_z;
    out.request_id = request_id;
    out.buried_item = buried_item;
    out.flags = flags;
    out.reason = reason;
    pcnetgame_host_send_result(peer, &out, (uint16_t)sizeof(out), "BURY_RESULT");
}

/* Forward-declared: defined further down alongside pcnetgame_validate_and_resolve_bury() (needs
   pcnetgame_tree_reach_check(), which is itself defined after this point in the file); used here. */
static int pcnetgame_bury_echo_current(uint8_t ut_x, uint8_t ut_z, uint16_t* out_value, int* out_dep_on);

/* World Ecology T3: builds a reject BURY_RESULT's reconciliation fields from the host's CURRENT tile
 * state at (ut_x, ut_z) -- shared by every BURY reject path (validate-time and commit-time alike) so
 * the echo logic lives in exactly one place. */
static void pcnetgame_host_send_bury_reject(PCNetPeerId peer, uint32_t request_id, uint8_t ut_x, uint8_t ut_z) {
    uint16_t value = 0;
    int dep_on = 0;
    uint8_t flags = 0;
    if (pcnetgame_bury_echo_current(ut_x, ut_z, &value, &dep_on)) {
        flags = (uint8_t)PC_NETGAME_BURY_FLAG_RECONCILE_VALID;
        if (dep_on) {
            flags |= (uint8_t)PC_NETGAME_BURY_FLAG_DEPOSIT_ON;
        }
    }
    pcnetgame_host_send_bury_result(peer, request_id, ut_x, ut_z, 0, value, flags, (uint8_t)PC_NETGAME_BURY_REASON_NONE);
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
    } else if (kind == (int)PC_NETGAME_INTERACT_KIND_BURY) {
        /* World Ecology T3: a replayed BURY reject carries no fresh reconciliation echo (this is a
           retry of an ALREADY-decided request, not a new rejection) -- accepted=0/buried_item=0/flags=0
           is correct here; a genuine reject always goes through pcnetgame_host_send_bury_reject()
           instead, at the point the decision is actually made. */
        pcnetgame_host_send_bury_result(peer, rec->request_id, rec->ut_x, rec->ut_z, accepted, accepted ? rec->item : 0,
                                        0, (uint8_t)PC_NETGAME_BURY_REASON_NONE);
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

/* World Ecology Stage 1, Item 1: read-only validation for one DIG_BURIED request, mirroring
 * pcnetgame_validate_and_resolve_pickup()'s own shape (reservation check, reach check) but keyed off
 * the DEPOSIT bit being SET (a buried item) rather than clear. The granted item is the host's own
 * pcfa_get_tile() read at the target tile, resolved through the same pcnetgame_resolve_pickup_item()
 * pickup already uses (handles the present/golden-tool sentinels uniformly; a no-op passthrough for
 * every ordinary buried item) -- never RNG, never the client's claim. */
/* T0-A: the reach-check block previously duplicated verbatim at TWO call sites --
 * pcnetgame_validate_and_resolve_dig() and pcnetgame_validate_money_rock_hit() -- extracted here with
 * ZERO behavioral change: identical tile-center formula, identical PC_NETGAME_PICKUP_MAX_REACH_SQ/_Y
 * constants, identical `center.y = py` convention (see pcnetgame_validate_and_resolve_pickup()'s own
 * doc on why that's deliberate). Returns 1 iff (ut_x, ut_z)'s tile center is within reach of
 * (px, py, pz) -- the requester's own last-synced position. Both callers already validated
 * pcnetgame_pos_valid(px, py, pz) before calling this. */
static int pcnetgame_field_action_reach_check(uint8_t ut_x, uint8_t ut_z, float px, float py, float pz) {
    xyz_t center;
    float dx, dz, dy;

    center.x = (f32)ut_x * mFI_UT_WORLDSIZE_X_F + mFI_UT_WORLDSIZE_HALF_X_F;
    center.z = (f32)ut_z * mFI_UT_WORLDSIZE_Z_F + mFI_UT_WORLDSIZE_HALF_Z_F;
    center.y = py;
    dx = center.x - px;
    dz = center.z - pz;
    dy = center.y - py;
    return (dx * dx + dz * dz) <= PC_NETGAME_PICKUP_MAX_REACH_SQ && fabsf(dy) <= PC_NETGAME_PICKUP_MAX_REACH_Y;
}

/* World Ecology T1: standalone tree-specific reach check -- see PC_NETGAME_TREE_REACH_SQ's own doc for
 * why this is a full, deliberate duplicate of pcnetgame_field_action_reach_check()'s math above (not a
 * refactor of it) rather than a shared parameterized helper. */
static int pcnetgame_tree_reach_check(uint8_t ut_x, uint8_t ut_z, float px, float py, float pz) {
    xyz_t center;
    float dx, dz, dy;

    center.x = (f32)ut_x * mFI_UT_WORLDSIZE_X_F + mFI_UT_WORLDSIZE_HALF_X_F;
    center.z = (f32)ut_z * mFI_UT_WORLDSIZE_Z_F + mFI_UT_WORLDSIZE_HALF_Z_F;
    center.y = py;
    dx = center.x - px;
    dz = center.z - pz;
    dy = center.y - py;
    return (dx * dx + dz * dz) <= PC_NETGAME_TREE_REACH_SQ && fabsf(dy) <= PC_NETGAME_PICKUP_MAX_REACH_Y;
}

/* World Ecology Wildlife Sync T-catch: fish-catch reach check -- see PC_NETGAME_FISH_CATCH_REACH_SQ's
 * own doc for why this is XZ-ONLY (never Y) and uses a raw actor-position comparison rather than the
 * tile-center formula every other reach check above uses (a fish record's position is not tile-quantized
 * -- it is the exact xyz_t the spawn decision produced, PcWildlifeRecord.pos_x/z). */
static int pcnetgame_fish_catch_reach_check(float rec_x, float rec_z, float px, float pz) {
    float dx = rec_x - px;
    float dz = rec_z - pz;
    return (dx * dx + dz * dz) <= PC_NETGAME_FISH_CATCH_REACH_SQ;
}

/* World Ecology Wildlife Sync T4: bug-catch reach check -- same XZ-only shape as the fish check just
 * above, using PC_NETGAME_BUG_CATCH_REACH_SQ's own bound (see that constant's own doc). */
static int pcnetgame_bug_catch_reach_check(float rec_x, float rec_z, float px, float pz) {
    float dx = rec_x - px;
    float dz = rec_z - pz;
    return (dx * dx + dz * dz) <= PC_NETGAME_BUG_CATCH_REACH_SQ;
}

/* World Ecology T3: every read-only validation step for one BURY request/host-local action, in
 * sequence, stopping at the first failure -- never mutates anything. `is_host_local` skips the reach
 * and PLAYER_CONTEXT/IN_TOWN checks (the host's own real local targeting already guarantees those),
 * mirroring pcnetgame_fa_validate_adapter_money_rock()'s own is_host_local exemption pattern; `peer` is
 * only read when !is_host_local. Uses PC_NETGAME_TREE_REACH_SQ (~80 units), NOT
 * PC_NETGAME_PICKUP_MAX_REACH_SQ (~50 units) -- a shovel bury target can legitimately be up to ~63.25
 * units from the player (same shovel-range reasoning PC_NETGAME_TREE_REACH_SQ's own doc already
 * establishes for tree targets), so the tighter pickup envelope would falsely reject legitimate buries.
 * On success, fills *out_acre/*out_tile (the persistent address of the reserved tile) and
 * *out_raw_item (the tile's raw value observed now -- HOLE00..24 or HOLE_SHINE; the COMMIT re-checks
 * this is still exactly that value before writing anything). */
static int pcnetgame_validate_and_resolve_bury(int is_host_local, PCNetPeerId peer, uint8_t pocket_slot_idx,
                                               uint8_t ut_x, uint8_t ut_z, mActor_name_t claimed_item,
                                               uint8_t hole_variant, int* out_acre, int* out_tile,
                                               uint16_t* out_raw_item) {
    uint16_t raw_value;
    int acre, tile;
    int holder;

    if (!s_host_world_ready) {
        return 0; /* host save not loaded (title / save scene) -- nothing authoritative to read */
    }
    if (!is_host_local) {
        if (!s_host_peer[peer].ctx_valid || !(s_host_peer[peer].ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
            return 0; /* requester is not (known to be) in the town scene */
        }
    }
    if (pocket_slot_idx >= mPr_POCKETS_SLOT_COUNT) {
        return 0; /* not a real pocket slot -- never trust it (the host cannot verify actual
                     possession any more than pickup/drop already can -- see their own doc) */
    }
    if (!pcnetgame_is_buryable_item(claimed_item)) {
        return 0; /* outside this build's supported bury item classification */
    }
    if (!pcnetgame_validate_bury_hole_variant(hole_variant)) {
        return 0; /* TRUST BOUNDARY -- out-of-range hole_variant is rejected, never clamped */
    }
    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile)) {
        return 0; /* not a persistent town tile -- never trust it, whatever the client intended */
    }
    holder = pcnetgame_host_tile_reserved_by(acre, tile);
    if (holder >= 0) {
        if (g_pc_verbose) {
            printf("[NET][BURY] host: peer %d bury at tile (%d,%d) rejected: tile reserved by peer %d\n",
                   is_host_local ? -1 : (int)peer, (int)ut_x, (int)ut_z, holder);
        }
        return 0; /* another peer's pending pickup/drop/bury holds this tile */
    }
    if (pcfa_get_deposit(acre, tile) != 0) {
        return 0; /* already buried -- never a legal bury target */
    }
    if (!pcfa_get_tile(acre, tile, &raw_value)) {
        return 0;
    }
    if (!ITEM_IS_HOLE(raw_value) && raw_value != (uint16_t)HOLE_SHINE) {
        return 0; /* not a hole at all -- only a HOLE00..24 or HOLE_SHINE tile can receive a bury */
    }

    if (!is_host_local) {
        float px, py, pz;
        if (!pc_remote_player_get_last_position((PCNetPlayerId)peer, &px, &py, &pz)) {
            return 0; /* no movement sample from this peer yet */
        }
        if (!pcnetgame_pos_valid(px, py, pz)) {
            return 0;
        }
        if (!pcnetgame_tree_reach_check(ut_x, ut_z, px, py, pz)) {
            return 0; /* too far from this peer's last-known position -- see this function's own doc
                         for why the tree envelope, not the tighter pickup one, is used here */
        }
    }

    *out_acre = acre;
    *out_tile = tile;
    *out_raw_item = raw_value;
    return 1;
}

/* World Ecology T3: best-effort re-read of the host's CURRENT tile value + deposit bit at (ut_x, ut_z)
 * for a BURY_RESULT reject echo -- mirrors pcnetgame_fa_pitfall_consume_echo_current()'s own doc
 * exactly (pure address-space reads, safe even when s_host_world_ready is false or the tile can't be
 * resolved -- *out_value/*out_dep_on are simply left at their caller-supplied defaults then). Unlike
 * PITFALL_CONSUME, a bury race can leave deposit ON (another peer's bury already committed), so this
 * also reports the deposit bit -- see PCNetGameBuryResultMsg's own DEPOSIT_ON flag doc. Returns 1 iff
 * both reads succeeded (a genuine echo the client should trust -- RECONCILE_VALID). */
static int pcnetgame_bury_echo_current(uint8_t ut_x, uint8_t ut_z, uint16_t* out_value, int* out_dep_on) {
    int acre, tile;
    uint16_t value;

    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile) || !pcfa_get_tile(acre, tile, &value)) {
        return 0;
    }
    /* Bug 2 fix: a TRANSIENT (or AMBIGUOUS) current value -- e.g. RSV_NO mid another player's
     * bury-commit -- must never be echoed as a hard reconciliation value. A client that applies it via
     * pcnetgame_client_apply_tile() would then treat the tile as "not overwritable while in town"
     * (see that function's own doc), parking/blocking future legitimate updates for it. Fall back to
     * the existing "nothing valid to echo" behavior (return 0, caller sends flags=0 / no
     * RECONCILE_VALID) exactly like an unreadable tile already does above. */
    if (pcfa_transient_kind(value) != PCFA_VALUE_STABLE) {
        return 0;
    }
    *out_value = value;
    *out_dep_on = pcfa_get_deposit(acre, tile) != 0;
    return 1;
}

static int pcnetgame_validate_and_resolve_dig(PCNetPeerId peer, uint8_t ut_x, uint8_t ut_z,
                                              mActor_name_t* out_granted_item, int* out_acre, int* out_tile) {
    uint16_t raw_value;
    mActor_name_t raw_item;
    float px, py, pz;
    int acre, tile;

    if (!s_host_world_ready) {
        return 0;
    }
    if (!s_host_peer[peer].ctx_valid || !(s_host_peer[peer].ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
        return 0;
    }
    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile)) {
        return 0;
    }
    if (pcnetgame_host_tile_reserved_by(acre, tile) >= 0) {
        return 0; /* another peer's pending pickup/drop reservation holds this tile */
    }
    if (!pcfa_get_tile(acre, tile, &raw_value)) {
        return 0;
    }
    raw_item = (mActor_name_t)raw_value;

    if (pcfa_get_deposit(acre, tile) != 0) {
        /* Ordinary buried item (case 5a, unchanged): fail closed on EMPTY_NO/unknown. */
        if (raw_item == (mActor_name_t)EMPTY_NO || mNT_check_unknown(raw_item)) {
            return 0;
        }
        *out_granted_item = pcnetgame_resolve_pickup_item(raw_item);
    } else if (ITEM_IS_BURIED_PITFALL_HOLE(raw_value)) {
        /* World Ecology T-dig, case 5b: deposit OFF but the tile is a BURIED_PITFALL_HOLE00..24 value --
         * this is DIGGING UP an already-buried pitfall (distinct from PITFALL_CONSUME, kind 7, which is
         * a player/villager falling INTO one) -- folded into this same extended DIG_BURIED validator per
         * this task's design brief rather than given its own kind, since the host resolves the item
         * either way. Always grants ITM_PITFALL, never RNG, never the client's claim. */
        *out_granted_item = (mActor_name_t)ITM_PITFALL;
    } else {
        return 0; /* neither an ordinary buried item nor a buried-pitfall-dig signature */
    }

    if (!pc_remote_player_get_last_position((PCNetPlayerId)peer, &px, &py, &pz)) {
        return 0;
    }
    if (!pcnetgame_pos_valid(px, py, pz)) {
        return 0;
    }
    if (!pcnetgame_field_action_reach_check(ut_x, ut_z, px, py, pz)) {
        return 0;
    }

    *out_acre = acre;
    *out_tile = tile;
    return 1;
}

/* World Ecology T-dig fix: HOLE_START + a HOST-VALIDATED hole_variant (0..24, see
 * pcnetgame_validate_hole_variant()) is now committed as the dig-hole graphic, instead of the previous
 * Stage 1 simplification of always hardcoding HOLE_START -- the caller (pcnetgame_fa_commit_adapter_dig())
 * has already range-checked req->hole_variant via the adapter's own validate step before this is ever
 * called, so hole_variant here is trusted to be in range. Used for BOTH of DIG_BURIED's (kind 1) two
 * outcomes (ordinary buried item AND the pitfall-dig sub-case) as well as DIG_HOLE (kind 5) -- all three
 * commit the exact same tile transition. */
static void pcnetgame_host_commit_dig_buried(int acre, int tile, uint8_t hole_variant) {
    pcfa_set_tile(acre, tile, (uint16_t)(HOLE_START + hole_variant));
    pcfa_set_deposit(acre, tile, 0);
}

/* World Ecology Stage 1, Item 2: finds a free (EMPTY_NO) neighbor tile for a money-rock drop, exactly
 * like mFI_search_unit_around_high(pos, EMPTY_NO, cur_pos) does in bg_item_common.c_inc, but entirely
 * in persistent ut/acre/tile space via pcfa_get_tile() -- never mFI_GetUnitFG()/mCoBG_* scene-relative
 * calls, which would read the HOST's own currently-loaded scene rather than the persistent town (the
 * host may be indoors). Deliberate simplifications versus vanilla: no height/collision fit check
 * (mCoBG_CheckPlace(), mCoBG_GetBgY_OnlyCenter_FromWpos2()'s +/-40 band) -- cosmetic-only concerns
 * that need a loaded collision mesh (see below); and neighbor SEARCH ORDER need not match vanilla's exact
 * BI_chk_pos table since any free neighbor is an equally valid drop spot. Center tile (the rock's own
 * tile) is checked last, matching vanilla's own fallback-to-origin order. Returns 1 and fills
 * *out_acre/*out_tile on success, 0 if every candidate is occupied, RESERVED, or off the town grid.
 *
 * Bug 2 fix: a candidate tile currently RESERVED by another peer's pending pickup/drop
 * (pcnetgame_host_tile_reserved_by() >= 0) is skipped too -- that peer's own pending COMMIT is about
 * to write that exact tile, and dropping a money bag onto it first would either corrupt that peer's
 * own commit re-validation or silently destroy the freshly dropped bag. If NO candidate tile is valid
 * (all occupied/deposited/reserved, or off-grid), this hit's drop is skipped entirely by the caller --
 * matching vanilla's own bIT_actor_ten_coin_entryR() behavior, which still advances hit_count/window
 * state regardless and only skips the drop+sound when no valid position was found. No retry, no
 * deferral, no queue: the hit still counts.
 *
 * Review finding, investigated and accepted as a documented limitation (no fix possible without new
 * persistent data): this search has no water/river/cliff exclusion, so a reward bag could rarely land
 * on genuinely unplaceable terrain even for the HOST's own local hit. A field-data-only equivalent was
 * traced and confirmed NOT to exist -- pcfa_get_tile()/pcfa_get_deposit() only ever read Save_t.fg
 * (the per-tile item/actor-name array) and Save_t.deposit (the buried-tile bitmap); nothing in Save_t
 * records terrain shape. Water/river/cliff/waterfall/diagonal-corner legality is determined solely by
 * mCoBG_CheckPlace() against the CURRENTLY LOADED scene's collision mesh (g_fdinfo->block_info[...]
 * .bg_info.collision, populated per BG block only while that acre's model is resident) -- there is no
 * per-acre/per-tile terrain-type table anywhere in the save to consult instead. Since this same search
 * also runs for a REMOTE peer's money-rock hit while the HOST may be indoors or elsewhere in the town
 * (i.e. with no relevant collision loaded at all), it cannot conditionally call mCoBG_CheckPlace()
 * either: that would make the outcome depend on where the host's own local camera happens to be,
 * a much worse, non-deterministic behavior than simply never checking terrain. Impact is limited to a
 * rare cosmetic inconvenience (a bag occasionally lands somewhere unreachable), never item loss,
 * duplication, or corruption -- the hit-count/window bookkeeping above is entirely unaffected, and
 * this mirrors vanilla's own documented fallback-to-origin behavior when a search finds no valid
 * neighbor at all. */
static int pcnetgame_find_money_rock_drop_tile(int center_ut_x, int center_ut_z, int* out_acre, int* out_tile) {
    static const int dxs[9] = { -1, -1, 1, 1, -1, 0, 1, 0, 0 };
    static const int dzs[9] = { -1, 1, 1, -1, 0, 1, 0, -1, 0 };
    int i;

    for (i = 0; i < 9; i++) {
        int ut_x = center_ut_x + dxs[i];
        int ut_z = center_ut_z + dzs[i];
        int acre, tile;
        uint16_t value;

        if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
            continue;
        }
        if (!pcfa_town_ut_to_acre_tile(ut_x, ut_z, &acre, &tile)) {
            continue;
        }
        if (pcnetgame_host_tile_reserved_by(acre, tile) >= 0) {
            continue; /* another peer's pending pickup/drop holds this tile -- Bug 2 fix, see doc above */
        }
        if (pcfa_get_deposit(acre, tile) != 0) {
            continue; /* a buried tile is never a valid drop spot */
        }
        if (!pcfa_get_tile(acre, tile, &value) || value != (uint16_t)EMPTY_NO) {
            continue;
        }
        *out_acre = acre;
        *out_tile = tile;
        return 1;
    }
    return 0;
}

/* ==================== World Ecology T1: tree shake / chop / bee-birth ==================== */

/* World Ecology T1: generalizes pcnetgame_find_money_rock_drop_tile()'s free-neighbor search to an
 * arbitrary center -- a NEW function; money-rock's own search and its call site above are completely
 * untouched. Candidate order: origin, then the 4 orthogonal neighbors (N/E/S/W), then the 4 diagonals --
 * origin FIRST (unlike money-rock's own order, which checks its rock's own tile LAST) because a tree
 * drop's most natural landing spot is at/around the tree's own tile itself, per this task's design
 * brief. Same filters as money-rock's search: on-grid, not reserved by another peer's pending pickup/
 * drop (pcnetgame_host_tile_reserved_by()), not a buried tile, and EMPTY_NO. Returns 1 and fills
 * *out_acre/*out_tile on success, 0 if every candidate is occupied/reserved/off-grid -- the caller
 * (pcnetgame_host_place_tree_drops()) simply loses that one dropped copy, exactly like vanilla's own
 * fallback-to-origin behavior and money-rock's own documented loss-on-no-space precedent. */
static int pcnetgame_find_free_tile_around(int center_ut_x, int center_ut_z, int* out_acre, int* out_tile) {
    static const int dxs[9] = { 0, 0, 1, 0, -1, -1, 1, 1, -1 };
    static const int dzs[9] = { 0, -1, 0, 1, 0, -1, -1, 1, 1 };
    int i;

    for (i = 0; i < 9; i++) {
        int ut_x = center_ut_x + dxs[i];
        int ut_z = center_ut_z + dzs[i];
        int acre, tile;
        uint16_t value;

        if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
            continue;
        }
        if (!pcfa_town_ut_to_acre_tile(ut_x, ut_z, &acre, &tile)) {
            continue;
        }
        if (pcnetgame_host_tile_reserved_by(acre, tile) >= 0) {
            continue;
        }
        if (pcfa_get_deposit(acre, tile) != 0) {
            continue;
        }
        if (!pcfa_get_tile(acre, tile, &value) || value != (uint16_t)EMPTY_NO) {
            continue;
        }
        *out_acre = acre;
        *out_tile = tile;
        return 1;
    }
    return 0;
}

/* World Ecology T1: a byte-exact duplicate of drop_fruit()'s own 21-row fg_ytable
 * (bg_item_common.c_inc) -- duplicated here (rather than shared) because the original is `static` inside
 * a different translation unit, exactly the same reason pc_net_game.c already duplicates
 * bIT_actor_ten_coin_entryR()'s reward switch for MONEY_ROCK_HIT instead of calling it. Keep this in
 * sync with drop_fruit()'s own table by hand if that table ever changes -- there is no way to enforce
 * this at compile time across translation units. */
typedef struct PCNetGameTreeDropRow {
    mActor_name_t src_tree_item;
    mActor_name_t dropped_item;
    mActor_name_t dst_tree_item;
    s16            drop_count;
} PCNetGameTreeDropRow;

static const PCNetGameTreeDropRow s_tree_drop_tbl[] = {
    { TREE_APPLE_FRUIT, ITM_FOOD_APPLE, TREE_APPLE_NOFRUIT_0, 3 },
    { TREE_ORANGE_FRUIT, ITM_FOOD_ORANGE, TREE_ORANGE_NOFRUIT_0, 3 },
    { TREE_PEACH_FRUIT, ITM_FOOD_PEACH, TREE_PEACH_NOFRUIT_0, 3 },
    { TREE_PEAR_FRUIT, ITM_FOOD_PEAR, TREE_PEAR_NOFRUIT_0, 3 },
    { TREE_CHERRY_FRUIT, ITM_FOOD_CHERRY, TREE_CHERRY_NOFRUIT_0, 3 },
    { TREE_1000BELLS, ITM_MONEY_1000, TREE, 3 },
    { TREE_10000BELLS, ITM_MONEY_10000, TREE, 3 },
    { TREE_30000BELLS, ITM_MONEY_30000, TREE, 3 },
    { TREE_100BELLS, ITM_MONEY_100, TREE, 3 },
    { TREE_FTR, FTR_START(FTR_NOG_FAN01), TREE, 1 },
    { TREE_BEES, HONEYCOMB, TREE, 1 },
    { TREE_PRESENT, ITM_PRESENT, TREE, 1 },
    { TREE_BELLS, ITM_MONEY_100, TREE, 1 },
    { TREE_PALM_FRUIT, ITM_FOOD_COCONUT, TREE_PALM_NOFRUIT_0, 2 },
    { CEDAR_TREE_BELLS, ITM_MONEY_100, CEDAR_TREE, 1 },
    { CEDAR_TREE_FTR, FTR_START(FTR_NOG_FAN01), CEDAR_TREE, 1 },
    { CEDAR_TREE_BEES, HONEYCOMB, CEDAR_TREE, 1 },
    { GOLD_TREE_BELLS, ITM_MONEY_100, GOLD_TREE, 1 },
    { GOLD_TREE_FTR, FTR_START(FTR_NOG_FAN01), GOLD_TREE, 1 },
    { GOLD_TREE_BEES, HONEYCOMB, GOLD_TREE, 1 },
    { GOLD_TREE_SHOVEL, ITM_GOLDEN_SHOVEL, GOLD_TREE, 1 },
};
#define PC_NETGAME_TREE_DROP_TBL_COUNT (sizeof(s_tree_drop_tbl) / sizeof(s_tree_drop_tbl[0]))

static const PCNetGameTreeDropRow* pcnetgame_find_tree_drop_row(mActor_name_t item) {
    size_t i;
    for (i = 0; i < PC_NETGAME_TREE_DROP_TBL_COUNT; i++) {
        if (s_tree_drop_tbl[i].src_tree_item == item) {
            return &s_tree_drop_tbl[i];
        }
    }
    return NULL;
}

/* See pc_net_game.h's own doc. */
int pc_net_game_is_tree_fruit_drop_source(int item) {
    return pcnetgame_find_tree_drop_row((mActor_name_t)item) != NULL;
}

/* World Ecology T1: a byte-exact duplicate of bIT_common_clear_treeatr()'s own 80-row tree_cut_tbl
 * (bg_item_common.c_inc) -- same "original is static in a different translation unit" reason as
 * s_tree_drop_tbl above. Returns the vanilla hit-count for `item` (1/2/3, matching *_S0/*_S1/*_S2 or a
 * full-size/special tree exactly), or -1 if `item` is not a choppable tree at all (a sapling stage
 * below S0, a stump, or any non-tree item) -- TREE_CHOP's own validators reject on -1. */
typedef struct PCNetGameTreeCutHitsRow {
    mActor_name_t tree;
    s16            cut_count;
} PCNetGameTreeCutHitsRow;

static const PCNetGameTreeCutHitsRow s_tree_cut_hits_tbl[] = {
    { TREE_S0, 1 }, { TREE_S1, 2 }, { TREE_S2, 3 }, { TREE, 3 },
    { TREE_APPLE_S0, 1 }, { TREE_APPLE_S1, 2 }, { TREE_APPLE_S2, 3 },
    { TREE_APPLE_NOFRUIT_0, 3 }, { TREE_APPLE_NOFRUIT_1, 3 }, { TREE_APPLE_NOFRUIT_2, 3 },
    { TREE_APPLE_FRUIT, 3 },
    { TREE_ORANGE_S0, 1 }, { TREE_ORANGE_S1, 2 }, { TREE_ORANGE_S2, 3 },
    { TREE_ORANGE_NOFRUIT_0, 3 }, { TREE_ORANGE_NOFRUIT_1, 3 }, { TREE_ORANGE_NOFRUIT_2, 3 },
    { TREE_ORANGE_FRUIT, 3 },
    { TREE_PEACH_S0, 1 }, { TREE_PEACH_S1, 2 }, { TREE_PEACH_S2, 3 },
    { TREE_PEACH_NOFRUIT_0, 3 }, { TREE_PEACH_NOFRUIT_1, 3 }, { TREE_PEACH_NOFRUIT_2, 3 },
    { TREE_PEACH_FRUIT, 3 },
    { TREE_PEAR_S0, 1 }, { TREE_PEAR_S1, 2 }, { TREE_PEAR_S2, 3 },
    { TREE_PEAR_NOFRUIT_0, 3 }, { TREE_PEAR_NOFRUIT_1, 3 }, { TREE_PEAR_NOFRUIT_2, 3 },
    { TREE_PEAR_FRUIT, 3 },
    { TREE_CHERRY_S0, 1 }, { TREE_CHERRY_S1, 2 }, { TREE_CHERRY_S2, 3 },
    { TREE_CHERRY_NOFRUIT_0, 3 }, { TREE_CHERRY_NOFRUIT_1, 3 }, { TREE_CHERRY_NOFRUIT_2, 3 },
    { TREE_CHERRY_FRUIT, 3 },
    { TREE_1000BELLS_S0, 1 }, { TREE_1000BELLS_S1, 2 }, { TREE_1000BELLS_S2, 3 }, { TREE_1000BELLS, 3 },
    { TREE_10000BELLS_S0, 1 }, { TREE_10000BELLS_S1, 2 }, { TREE_10000BELLS_S2, 3 }, { TREE_10000BELLS, 3 },
    { TREE_30000BELLS_S0, 1 }, { TREE_30000BELLS_S1, 2 }, { TREE_30000BELLS_S2, 3 }, { TREE_30000BELLS, 3 },
    { TREE_BEES, 3 }, { TREE_FTR, 3 }, { TREE_LIGHTS, 3 }, { TREE_PRESENT, 3 }, { TREE_BELLS, 3 },
    { TREE_100BELLS_S0, 1 }, { TREE_100BELLS_S1, 2 }, { TREE_100BELLS_S2, 3 }, { TREE_100BELLS, 3 },
    { TREE_PALM_S0, 1 }, { TREE_PALM_S1, 2 }, { TREE_PALM_S2, 3 },
    { TREE_PALM_NOFRUIT_0, 3 }, { TREE_PALM_NOFRUIT_1, 3 }, { TREE_PALM_NOFRUIT_2, 3 },
    { TREE_PALM_FRUIT, 3 },
    { CEDAR_TREE_S0, 1 }, { CEDAR_TREE_S1, 2 }, { CEDAR_TREE_S2, 3 }, { CEDAR_TREE, 3 },
    { CEDAR_TREE_BELLS, 3 }, { CEDAR_TREE_FTR, 3 }, { CEDAR_TREE_BEES, 3 }, { CEDAR_TREE_LIGHTS, 3 },
    { GOLD_TREE_S0, 1 }, { GOLD_TREE_S1, 2 }, { GOLD_TREE_S2, 3 }, { GOLD_TREE_SHOVEL, 3 },
    { GOLD_TREE, 3 }, { GOLD_TREE_BELLS, 3 }, { GOLD_TREE_FTR, 3 }, { GOLD_TREE_BEES, 3 },
};
#define PC_NETGAME_TREE_CUT_HITS_TBL_COUNT (sizeof(s_tree_cut_hits_tbl) / sizeof(s_tree_cut_hits_tbl[0]))

static int pcnetgame_tree_cut_hits_for(mActor_name_t item) {
    size_t i;
    for (i = 0; i < PC_NETGAME_TREE_CUT_HITS_TBL_COUNT; i++) {
        if (s_tree_cut_hits_tbl[i].tree == item) {
            return (int)s_tree_cut_hits_tbl[i].cut_count;
        }
    }
    return -1;
}

/* World Ecology T1: resolves drop_fruit()'s own per-row special cases -- the furniture roll
 * (mSP_SelectRandomItem_New(), keyed on the requester's own destiny_type exactly like vanilla's
 * Common_Get(now_private)->destiny.type read) and the TREE_BELLS/CEDAR_TREE_BELLS/GOLD_TREE_BELLS
 * MONEY_LUCK upgrade (ITM_MONEY_100 -> ITM_MONEY_1000, deterministic, no roll) -- verbatim mirroring
 * drop_fruit()'s own switch (bg_item_common.c_inc). Every other row's dropped_item passes through
 * unchanged (the TREE_1000/10000/30000/100BELLS "money tree" rows already carry a fixed amount in the
 * table itself and are never adjusted by destiny). */
static mActor_name_t pcnetgame_resolve_tree_drop_item(const PCNetGameTreeDropRow* row, const PCNetPlayerContext* ctx) {
    if (row->dropped_item == (mActor_name_t)FTR_START(FTR_NOG_FAN01)) {
        int list = mSP_LISTTYPE_ABC;
        mActor_name_t ftr_item;

        if ((int)ctx->destiny_type == mPr_DESTINY_GOODS_LUCK) {
            list = (fqrand() >= 0.5f) ? mSP_LISTTYPE_EVENT : mSP_LISTTYPE_LOTTERY;
        }
        mSP_SelectRandomItem_New(NULL, &ftr_item, 1, NULL, 0, mSP_KIND_FURNITURE, list, FALSE);
        return ftr_item;
    }
    if ((row->src_tree_item == (mActor_name_t)TREE_BELLS || row->src_tree_item == (mActor_name_t)CEDAR_TREE_BELLS ||
         row->src_tree_item == (mActor_name_t)GOLD_TREE_BELLS) &&
        (int)ctx->destiny_type == mPr_DESTINY_MONEY_LUCK) {
        return (mActor_name_t)ITM_MONEY_1000;
    }
    return row->dropped_item;
}

/* World Ecology T1: places up to `drop_count` copies of `resolved_item` on free neighbor tiles around
 * (ut_x, ut_z), using pcnetgame_find_free_tile_around() from a tree-relative offset per copy (mirroring
 * fruit_set()'s own per-index unit_offset_part table, bg_item_common.c_inc, though purely for PLACEMENT
 * here -- this file has no collision mesh to reproduce fruit_set()'s own visual drop-arc positions, see
 * pcnetgame_find_money_rock_drop_tile()'s own doc on that same limitation). Guards: never places
 * EMPTY_NO, HONEYCOMB (bees are handled purely client-side visually -- see pc_tree_birth_bee_visual()'s
 * own doc, bg_item_common.c_inc -- and must never reach the host commit path), or anything
 * mNT_check_unknown() rejects; the furniture "no list" dummy id vanilla can legitimately roll is NOT
 * filtered by this (only a truly empty/unknown result is). If no free tile is found for a given copy,
 * that copy is simply lost, exactly matching vanilla's/money-rock's own documented loss-on-no-space
 * behavior. */
static void pcnetgame_host_place_tree_drops(int ut_x, int ut_z, mActor_name_t resolved_item, int drop_count) {
    static const int kOffX[3] = { -1, 1, 0 };
    static const int kOffZ[3] = { 0, 0, 1 };
    int i;

    if (resolved_item == (mActor_name_t)EMPTY_NO || resolved_item == (mActor_name_t)HONEYCOMB ||
        mNT_check_unknown(resolved_item)) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] host: tree drop at tile (%d,%d) resolved to invalid item 0x%04X -- skipped\n",
                   ut_x, ut_z, (unsigned)resolved_item);
        }
        return;
    }

    for (i = 0; i < drop_count; i++) {
        int off = i % 3;
        int center_x = ut_x + kOffX[off];
        int center_z = ut_z + kOffZ[off];
        int drop_acre, drop_tile;

        if (pcnetgame_find_free_tile_around(center_x, center_z, &drop_acre, &drop_tile)) {
            pcfa_set_tile(drop_acre, drop_tile, (uint16_t)resolved_item);
        } else if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] host: tree drop copy %d/%d at tile (%d,%d) lost -- no free neighbor tile\n",
                   i + 1, drop_count, ut_x, ut_z);
        }
    }
}

/* World Ecology T1: read-only core validation for TREE_SHAKE -- the target tile must currently be one
 * of s_tree_drop_tbl's own 21 rows (this is what makes S1's own pc_net_game_is_tree_fruit_drop_source()
 * pre-filter client-side safe to trust loosely: the host re-derives the SAME classification here, fresh,
 * from its own authoritative tile, never from anything the client claims). Shared by both the peer-
 * request path and the host's own local shake/bee-birth. */
static int pcnetgame_validate_tree_shake_core(uint8_t ut_x, uint8_t ut_z, int* out_acre, int* out_tile,
                                              mActor_name_t* out_item) {
    uint16_t raw_value;
    mActor_name_t raw_item;
    int acre, tile;

    if (!s_host_world_ready) {
        return 0;
    }
    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile)) {
        return 0;
    }
    if (!pcfa_get_tile(acre, tile, &raw_value)) {
        return 0;
    }
    raw_item = (mActor_name_t)raw_value;
    if (pcnetgame_find_tree_drop_row(raw_item) == NULL) {
        return 0;
    }
    *out_acre = acre;
    *out_tile = tile;
    *out_item = raw_item;
    return 1;
}

/* World Ecology T1: shared peer-only requirement for both TREE_SHAKE and TREE_CHOP -- a valid, in-town
 * network context, and the peer's last-synced position must reach the target tile within the tree-
 * specific envelope (pcnetgame_tree_reach_check(), wider than pickup's own). Never called for an
 * is_host_local requester (see PCNetGameRequester's own doc: the host's own real local targeting already
 * guarantees correctness, exactly like MONEY_ROCK_HIT's own precedent). */
static int pcnetgame_validate_tree_peer_reach(PCNetPeerId peer, uint8_t ut_x, uint8_t ut_z) {
    float px, py, pz;

    if (!s_host_peer[peer].ctx_valid || !(s_host_peer[peer].ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
        return 0;
    }
    if (!pc_remote_player_get_last_position((PCNetPlayerId)peer, &px, &py, &pz)) {
        return 0;
    }
    if (!pcnetgame_pos_valid(px, py, pz)) {
        return 0;
    }
    return pcnetgame_tree_reach_check(ut_x, ut_z, px, py, pz);
}

/* World Ecology T1: commits one already-validated TREE_SHAKE -- re-reads the tile FRESH (never trusting
 * *inout_item's validate-time value) to close the staleness window between a client's own shake-frame-10
 * item capture (or, for a bee-birth request, up to ~5 more frames of Check_BirthBee_Shake_tree's own
 * retry timer) and this request actually reaching the host; see this task's own design brief. If the
 * fresh tile no longer matches any s_tree_drop_tbl row (another commit already changed it -- e.g. a
 * felling TREE_CHOP raced ahead of a queued bee-birth TREE_SHAKE for the same tile), this rejects with
 * no mutation, exactly the "stump already committed" ordering guarantee the brief calls for.
 * A bee-tree row (TREE_BEES/CEDAR_TREE_BEES/GOLD_TREE_BEES) writes ONLY the destination tree -- no item
 * drop -- because bee-birth's honeycomb is handled purely client-side, visually, by every role via
 * pc_tree_birth_bee_visual() (bg_item_common.c_inc); every other row resolves its drop via
 * pcnetgame_resolve_tree_drop_item()/pcnetgame_host_place_tree_drops() before writing the destination
 * tree. granted_item (via *inout_item on return) becomes the resulting tree-tile value, so the
 * requester's own client can play whatever cue it likes off it (optional polish; not required for
 * correctness -- see PCNetGameFieldActionResultMsg's own doc). */
static void pcnetgame_host_commit_tree_shake(const PCNetPlayerContext* ctx, uint8_t ut_x, uint8_t ut_z, int acre,
                                             int tile, mActor_name_t* inout_item) {
    uint16_t fresh_value;
    mActor_name_t fresh_item;
    const PCNetGameTreeDropRow* row;

    if (!pcfa_get_tile(acre, tile, &fresh_value)) {
        *inout_item = (mActor_name_t)EMPTY_NO;
        return;
    }
    fresh_item = (mActor_name_t)fresh_value;
    row = pcnetgame_find_tree_drop_row(fresh_item);
    if (row == NULL) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] host: TREE_SHAKE at tile (%d,%d) rejected at commit -- tile 0x%04X no "
                   "longer matches any tree-drop row (raced by another commit)\n",
                   (int)ut_x, (int)ut_z, (unsigned)fresh_item);
        }
        *inout_item = (mActor_name_t)EMPTY_NO;
        return;
    }

    if (IS_ITEM_BEE_TREE(row->src_tree_item)) {
        pcfa_set_tile(acre, tile, (uint16_t)row->dst_tree_item);
        *inout_item = row->dst_tree_item;
        return;
    }

    {
        mActor_name_t resolved = pcnetgame_resolve_tree_drop_item(row, ctx);
        pcnetgame_host_place_tree_drops((int)ut_x, (int)ut_z, resolved, (int)row->drop_count);
    }
    pcfa_set_tile(acre, tile, (uint16_t)row->dst_tree_item);
    *inout_item = row->dst_tree_item;
}

/* World Ecology T1: the host's own transient, per-session cut-count table for TREE_CHOP -- mirrors
 * vanilla's own BIT_actor_tree_cutcount_check() (bg_item_common.c_inc), which decrements a per-BLOCK
 * table only covering blocks the CALLING process currently has loaded, and therefore cannot serve a
 * remote peer's chop (the host may have a different set of blocks loaded, or none, if indoors). No
 * persistence needed -- like vanilla's own table, this is meant to reset across a session boundary. A
 * small fixed slot count is enough: normally only the tile(s) actively being chopped right now are
 * ever tracked; an idle slot (no hit for PC_NETGAME_TREE_CUT_IDLE_60FPS_FRAMES) is forgotten so a
 * long-idle tile doesn't permanently pin a slot, and re-validates from scratch (pcnetgame_tree_cut_hits_for())
 * on its next hit. */
#define PC_NETGAME_TREE_CUT_SLOTS 16
#define PC_NETGAME_TREE_CUT_IDLE_60FPS_FRAMES (60.0f * 30.0f) /* 30 seconds idle -> forgotten */
typedef struct PCNetGameTreeCutSlot {
    int   active;
    int   acre;
    int   tile;
    int   hits_remaining;
    float idle_accum;
} PCNetGameTreeCutSlot;
static PCNetGameTreeCutSlot s_host_tree_cut_count[PC_NETGAME_TREE_CUT_SLOTS];

static PCNetGameTreeCutSlot* pcnetgame_find_tree_cut_slot(int acre, int tile) {
    int i;
    for (i = 0; i < PC_NETGAME_TREE_CUT_SLOTS; i++) {
        if (s_host_tree_cut_count[i].active && s_host_tree_cut_count[i].acre == acre &&
            s_host_tree_cut_count[i].tile == tile) {
            return &s_host_tree_cut_count[i];
        }
    }
    return NULL;
}

/* Allocates a free slot, or evicts the most-idle occupied one if every slot is busy -- a legitimate
 * chop should never be rejected purely for lack of bookkeeping space; losing the LEAST-recently-hit
 * tile's in-progress count (it simply restarts counting from this hit) is a far smaller behavioral
 * hiccup than refusing to track a chop at all. */
static PCNetGameTreeCutSlot* pcnetgame_alloc_tree_cut_slot(int acre, int tile) {
    PCNetGameTreeCutSlot* victim = NULL;
    int i;

    for (i = 0; i < PC_NETGAME_TREE_CUT_SLOTS; i++) {
        if (!s_host_tree_cut_count[i].active) {
            victim = &s_host_tree_cut_count[i];
            break;
        }
    }
    if (victim == NULL) {
        victim = &s_host_tree_cut_count[0];
        for (i = 1; i < PC_NETGAME_TREE_CUT_SLOTS; i++) {
            if (s_host_tree_cut_count[i].idle_accum > victim->idle_accum) {
                victim = &s_host_tree_cut_count[i];
            }
        }
    }
    memset(victim, 0, sizeof(*victim));
    victim->active = 1;
    victim->acre = acre;
    victim->tile = tile;
    return victim;
}

/* World Ecology T1: per-poll idle-expiry tick, host-only, mirroring
 * pcnetgame_host_check_field_action_money_rock()'s own "run once per poll" placement/gamePT gate. Only
 * ever forgets a slot -- never mutates the field (the tile's own value is untouched either way; a
 * forgotten slot simply re-derives its hit count fresh from pcnetgame_tree_cut_hits_for() next hit). */
static void pcnetgame_host_check_tree_cut_idle(void) {
    int i;
    float dt;

    if (gamePT == NULL || !s_host_world_ready) {
        return;
    }
    dt = (f32)gamePT->graph->dt_num_60fps_frames;
    for (i = 0; i < PC_NETGAME_TREE_CUT_SLOTS; i++) {
        PCNetGameTreeCutSlot* slot = &s_host_tree_cut_count[i];
        if (!slot->active) {
            continue;
        }
        slot->idle_accum += dt;
        if (slot->idle_accum >= PC_NETGAME_TREE_CUT_IDLE_60FPS_FRAMES) {
            memset(slot, 0, sizeof(*slot));
        }
    }
}

/* World Ecology T1: read-only core validation for TREE_CHOP -- the target tile must currently be a
 * choppable tree per s_tree_cut_hits_tbl (pcnetgame_tree_cut_hits_for() >= 0); unlike TREE_SHAKE this
 * deliberately accepts EVERY choppable stage (saplings included), not just the 21 fruit-drop rows --
 * a chop on an already-NOFRUIT tree or a young sapling is still a legitimate hit that must decrement
 * the host's own cut-count. */
static int pcnetgame_validate_tree_chop_core(uint8_t ut_x, uint8_t ut_z, int* out_acre, int* out_tile,
                                             mActor_name_t* out_item) {
    uint16_t raw_value;
    mActor_name_t raw_item;
    int acre, tile;

    if (!s_host_world_ready) {
        return 0;
    }
    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile)) {
        return 0;
    }
    if (!pcfa_get_tile(acre, tile, &raw_value)) {
        return 0;
    }
    raw_item = (mActor_name_t)raw_value;
    if (pcnetgame_tree_cut_hits_for(raw_item) < 0) {
        return 0;
    }
    *out_acre = acre;
    *out_tile = tile;
    *out_item = raw_item;
    return 1;
}

/* World Ecology T1: commits one already-validated TREE_CHOP hit. Re-reads the tile FRESH (same
 * staleness-window reasoning as TREE_SHAKE's own commit) and re-validates it is STILL choppable --
 * if not (e.g. a racing hit already felled it into a stump, or converted it some other way), any
 * stale cut-count slot for this tile is dropped and the hit is rejected with no mutation, which is
 * exactly the ordering guarantee this task's design brief calls out: "a bee conversion requested after
 * a felling hit should find a stump already committed and be rejected on re-validation, not
 * overwritten" -- the symmetric case (a chop hit arriving after a bee-birth TREE_SHAKE already
 * converted the tile) is handled the same way, generically, by this same fresh re-validate.
 * Finds or allocates this tile's host-owned cut-count slot, decrements it, then -- unless this is a
 * bee-tree row (no drop/conversion; see pcnetgame_host_commit_tree_shake()'s own doc on why bee-birth
 * is a separate, later TREE_SHAKE request, never part of an ordinary chop hit) -- runs the SAME drop
 * resolution TREE_SHAKE uses and writes the row's destination tree (a hit on a tile with no matching
 * drop row, e.g. an already-NOFRUIT tree or a sapling stage, produces no drop/conversion at all, which
 * is correct: vanilla's own Get_TreeNoToStumpNo() only converts a tile item_tree_fruit_drop_proc()
 * actually has a table row for). If this hit exhausts the counter, computes the stump via the real,
 * unmodified bg_item_fg_sub() from the ORIGINAL pre-hit tile value (mirroring vanilla's own
 * Get_TreeNoToStumpNo(), which stumps the axe_common.item captured at swing start, never a
 * mid-swing-converted value) and writes it LAST, so it overrides any fruit-conversion write earlier in
 * this same commit -- matching vanilla's own ordering and closing the same-frame bee/stump race the
 * brief identifies. */
static void pcnetgame_host_commit_tree_chop(const PCNetPlayerContext* ctx, uint8_t ut_x, uint8_t ut_z, int acre,
                                            int tile, mActor_name_t* inout_item) {
    uint16_t fresh_value;
    mActor_name_t fresh_item;
    int hits;
    PCNetGameTreeCutSlot* slot;
    const PCNetGameTreeDropRow* row;

    if (!pcfa_get_tile(acre, tile, &fresh_value)) {
        *inout_item = (mActor_name_t)EMPTY_NO;
        return;
    }
    fresh_item = (mActor_name_t)fresh_value;
    hits = pcnetgame_tree_cut_hits_for(fresh_item);
    if (hits < 0) {
        slot = pcnetgame_find_tree_cut_slot(acre, tile);
        if (slot != NULL) {
            memset(slot, 0, sizeof(*slot));
        }
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] host: TREE_CHOP at tile (%d,%d) rejected at commit -- tile 0x%04X no "
                   "longer choppable (raced by another commit)\n",
                   (int)ut_x, (int)ut_z, (unsigned)fresh_item);
        }
        *inout_item = (mActor_name_t)EMPTY_NO;
        return;
    }

    slot = pcnetgame_find_tree_cut_slot(acre, tile);
    if (slot == NULL) {
        slot = pcnetgame_alloc_tree_cut_slot(acre, tile);
        slot->hits_remaining = hits;
    }
    slot->idle_accum = 0.0f;
    slot->hits_remaining--;

    *inout_item = fresh_item; /* provisional -- overwritten below by any conversion/stump write */

    row = pcnetgame_find_tree_drop_row(fresh_item);
    if (row != NULL && !IS_ITEM_BEE_TREE(row->src_tree_item)) {
        mActor_name_t resolved = pcnetgame_resolve_tree_drop_item(row, ctx);
        pcnetgame_host_place_tree_drops((int)ut_x, (int)ut_z, resolved, (int)row->drop_count);
        pcfa_set_tile(acre, tile, (uint16_t)row->dst_tree_item);
        *inout_item = row->dst_tree_item;
    }

    if (slot->hits_remaining <= 0) {
        mActor_name_t stump = bg_item_fg_sub(fresh_item, 0);
        if (IS_ITEM_TREE_STUMP(stump)) {
            pcfa_set_tile(acre, tile, (uint16_t)stump); /* LAST write -- overrides any conversion write above */
            *inout_item = stump;
        } else if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] host: TREE_CHOP felled tile (%d,%d) but bg_item_fg_sub(0x%04X) did not "
                   "yield a stump (0x%04X) -- leaving the last conversion write in place\n",
                   (int)ut_x, (int)ut_z, (unsigned)fresh_item, (unsigned)stump);
        }
        memset(slot, 0, sizeof(*slot));
    }
}

/* World Ecology T1: per-poll idle-expiry tick, host-only -- see pcnetgame_host_check_tree_cut_idle()'s
 * own doc. Called from pc_net_game_poll() alongside pcnetgame_host_check_field_action_money_rock(). */
static void pcnetgame_host_check_tree_cut(void) {
    pcnetgame_host_check_tree_cut_idle();
}

/* World Ecology T1 review fix: clears every tree-cut cut-count slot -- called at the same two sites
 * (and under the exact same conditions) as s_host_money_rock's own reset: a detected town change on an
 * already-running host (pcnetgame_host_world_tick()), and a fresh hosting session's full state reset
 * (pcnetgame_reset_host_world_state()). Forward-declared above pcnetgame_host_world_tick() since this
 * table is defined later in the file than that function. */
static void pcnetgame_reset_host_tree_cut_state(void) {
    memset(s_host_tree_cut_count, 0, sizeof(s_host_tree_cut_count));
}

/* World Ecology P2 audit fix: called from mFM_PcFieldInitGrowth() (m_field_make.c) whenever the
 * host's own daily-growth pass actually changed at least one town acre (mFM_PcGrowCountChangedAcres()
 * > 0). Growth can change a tile's tree species/stage (e.g. a sapling growing into a full tree, which
 * needs a different hit count to fell) -- s_host_tree_cut_count is keyed only by acre/tile and a stale
 * entry for that tile from a hit shortly before growth ran (within its own 30s idle window, see
 * PC_NETGAME_TREE_CUT_IDLE_60FPS_FRAMES above) would otherwise wrongly carry over into the NEW tree's
 * state, letting it fall in fewer hits than it should. Vanilla itself avoids this because its own
 * per-block cut-count table (BIT_actor_tree_cutcount_check(), bg_item_common.c_inc) is naturally
 * re-derived fresh whenever a field block loads, which happens to coincide with growth timing; this
 * host-authoritative table has no such reload to piggyback on, so growth must clear it explicitly.
 * Just a thin wrapper around pcnetgame_reset_host_tree_cut_state() (same table, same reset as the
 * existing town-change/session-reset call sites) -- a no-op for single-player and for a client, since
 * only the host ever populates or consults this table. */
void pc_net_game_notify_field_growth(void) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    pcnetgame_reset_host_tree_cut_state();
}

/* World Ecology Stage 1, Item 2: finds this host's money-rock slot for (acre, tile) -- an ACTIVE
 * window that has not yet expired (see pcnetgame_host_check_field_action_money_rock()'s own tick).
 * NULL if none. */
static PCNetGameMoneyRockState* pcnetgame_find_active_money_rock(int acre, int tile) {
    int i;
    for (i = 0; i < PC_NETGAME_MONEY_ROCK_SLOTS; i++) {
        if (s_host_money_rock[i].active && s_host_money_rock[i].acre == acre && s_host_money_rock[i].tile == tile) {
            return &s_host_money_rock[i];
        }
    }
    return NULL;
}

/* World Ecology Stage 1, Item 2 / Bug 1 fix: the CORE, source-independent half of MONEY_ROCK_HIT
 * validation -- world-ready, resolves acre/tile, confirms a genuine IS_ITEM_STONE_TC tile
 * (MONEY_ROCK_A..E, or MONEY_FLOWER_SEED for a tile already mid-window -- see
 * bIT_actor_ten_coin_entryR()'s own money_stone_flag test, bg_item_common.c_inc, which this mirrors
 * exactly), and confirms either an already-tracked active window or a free s_host_money_rock slot.
 * Shared verbatim by both the peer-request path (pcnetgame_validate_money_rock_hit() below) and the
 * host's own local hit (pc_net_game_host_local_money_rock_hit()) -- neither adds or removes anything
 * from this half. Never mutates -- see pcnetgame_host_commit_money_rock_hit() for the actual commit. */
static int pcnetgame_validate_money_rock_hit_core(uint8_t ut_x, uint8_t ut_z, int* out_acre, int* out_tile,
                                                  mActor_name_t* out_raw_item) {
    uint16_t raw_value;
    mActor_name_t raw_item;
    int acre, tile;
    int i;
    int has_free_slot;

    if (!s_host_world_ready) {
        return 0;
    }
    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile)) {
        return 0;
    }
    if (!pcfa_get_tile(acre, tile, &raw_value)) {
        return 0;
    }
    raw_item = (mActor_name_t)raw_value;
    if (!IS_ITEM_STONE_TC(raw_item)) {
        return 0; /* not a money rock (or an active money-rock window) tile */
    }

    if (pcnetgame_find_active_money_rock(acre, tile) == NULL) {
        has_free_slot = 0;
        for (i = 0; i < PC_NETGAME_MONEY_ROCK_SLOTS; i++) {
            if (!s_host_money_rock[i].active) {
                has_free_slot = 1;
                break;
            }
        }
        if (!has_free_slot) {
            return 0; /* every slot is busy with a different money rock -- deterministic rejection,
                         mirrors vanilla's own bIT_TEN_COIN_NUM concurrency limit */
        }
    }

    *out_acre = acre;
    *out_tile = tile;
    *out_raw_item = raw_item;
    return 1;
}

/* World Ecology Stage 1, Item 2: read-only validation for one REMOTE PEER's MONEY_ROCK_HIT request --
 * the core check above, plus this request's own PEER-specific requirements: a valid, in-town network
 * context, and the peer's last-synced position must actually reach this tile. Never mutates -- see
 * pcnetgame_host_commit_money_rock_hit() for the actual commit. */
static int pcnetgame_validate_money_rock_hit(PCNetPeerId peer, uint8_t ut_x, uint8_t ut_z, int* out_acre,
                                             int* out_tile, mActor_name_t* out_raw_item) {
    float px, py, pz;
    int acre, tile;
    mActor_name_t raw_item;

    if (!s_host_peer[peer].ctx_valid || !(s_host_peer[peer].ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
        return 0;
    }
    if (!pcnetgame_validate_money_rock_hit_core(ut_x, ut_z, &acre, &tile, &raw_item)) {
        return 0;
    }

    if (!pc_remote_player_get_last_position((PCNetPlayerId)peer, &px, &py, &pz)) {
        return 0;
    }
    if (!pcnetgame_pos_valid(px, py, pz)) {
        return 0;
    }
    if (!pcnetgame_field_action_reach_check(ut_x, ut_z, px, py, pz)) {
        return 0;
    }

    *out_acre = acre;
    *out_tile = tile;
    *out_raw_item = raw_item;
    return 1;
}

/* World Ecology Stage 1, Item 2: commits one already-validated money-rock hit -- opens a new window
 * (mode 1) or advances an existing one (mode 2), exactly mirroring bIT_actor_ten_coin_entryR()'s own
 * two branches (bg_item_common.c_inc), then computes the deterministic reward
 * (hit_count/destiny_type only, no RNG) and drops it on a free neighbor tile if one exists. Returns
 * the granted... nothing (see PCNetGameFieldActionResultMsg's doc: MONEY_ROCK_HIT's granted_item is
 * always 0 -- the bag is picked up later through the ordinary pickup path).
 *
 * Bug 1 fix: takes a PCNetPlayerContext directly (destiny_type/money_power only), rather than a peer
 * index -- this is now called for BOTH a remote peer's request (passing that peer's own stored
 * context, &s_host_peer[peer].ctx) and the HOST's OWN local hit (passing a context filled fresh from
 * pcnetgame_capture_local_context(), via pc_net_game_host_local_money_rock_hit() below). destiny_type
 * is read fresh from `ctx` on every hit (matching vanilla's own per-hit
 * Common_Get(now_private)->destiny.type read); money_power is only read (and only matters) when this
 * call opens a brand new window. */
static void pcnetgame_host_commit_money_rock_hit(const PCNetPlayerContext* ctx, int acre, int tile,
                                                  mActor_name_t raw_item) {
    PCNetGameMoneyRockState* slot = pcnetgame_find_active_money_rock(acre, tile);
    int destiny_type = (int)ctx->destiny_type;
    mActor_name_t reward_item;
    int ut_x = 0, ut_z = 0;

    if (slot == NULL) {
        int i;
        for (i = 0; i < PC_NETGAME_MONEY_ROCK_SLOTS; i++) {
            if (!s_host_money_rock[i].active) {
                slot = &s_host_money_rock[i];
                break;
            }
        }
        if (slot == NULL) {
            return; /* validated moments ago, but defensively bail rather than corrupt another window */
        }

        /* Bug 1 fix: if this tile is already MONEY_FLOWER_SEED (mid-window graphic) with no active
         * bookkeeping slot for it (an orphaned/leftover state -- e.g. left over from before this fix
         * existed, or some other edge case), sanitize orig_item to a safe, always-valid MONEY_ROCK_A
         * rather than storing the already-wrong raw value: this guarantees the eventual revert
         * (orig_item - 7) always produces a real ROCK_x tile, never FLOWER_SEED. */
        {
            mActor_name_t stored_orig_item =
                (raw_item == (mActor_name_t)MONEY_FLOWER_SEED) ? (mActor_name_t)MONEY_ROCK_A : raw_item;
            int16_t money_power = ctx->money_power;
            int swing_time = (int)money_power;
            if (destiny_type == mPr_DESTINY_MONEY_LUCK) {
                swing_time -= 100;
            }
            if (swing_time > 100) {
                swing_time = 100;
            }
            slot->active = 1;
            slot->acre = acre;
            slot->tile = tile;
            slot->orig_item = stored_orig_item;
            slot->hit_count = 0;
            slot->money_power = money_power;
            slot->expire_accum = 0.0f;
            slot->expire_target_frames = 386.0f + ((f32)swing_time) * 0.59999999f;
        }
        pcfa_set_tile(acre, tile, (uint16_t)MONEY_FLOWER_SEED);
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] host: player %u opened money-rock window at acre %d tile %d (orig=0x%04X)\n",
                   (unsigned)ctx->player_no, acre, tile, (unsigned)raw_item);
        }
    } else {
        slot->hit_count++;
    }

    /* Deterministic reward -- verbatim mirror of bIT_actor_ten_coin_entryR()'s own switch
       (bg_item_common.c_inc), keyed on THIS request's player context. */
    switch (slot->hit_count) {
        case 0:
        case 1:
        case 2:
            reward_item = (destiny_type == mPr_DESTINY_MONEY_LUCK) ? (mActor_name_t)ITM_MONEY_1000
                                                                    : (mActor_name_t)ITM_MONEY_100;
            break;
        case 3:
        case 4:
        case 5:
            reward_item = (destiny_type == mPr_DESTINY_MONEY_LUCK) ? (mActor_name_t)ITM_MONEY_10000
                                                                    : (mActor_name_t)ITM_MONEY_1000;
            break;
        default:
            reward_item = (destiny_type == mPr_DESTINY_MONEY_LUCK) ? (mActor_name_t)ITM_MONEY_30000
                                                                    : (mActor_name_t)ITM_MONEY_10000;
            break;
    }

    /* Bug 4 fix: no bookkeeping slot for the drop any more -- pcfa_set_tile() below is the exact same
     * ordinary field-write path a regular dropped item already uses (dirty-flush, FIELD_UPDATE
     * broadcast, snapshot coverage, late-join coverage all inherited for free). If no valid neighbor
     * tile is found, this hit's drop is skipped entirely (matches vanilla: the hit-count/window state
     * above is still updated regardless, only the drop+sound is skipped -- see
     * pcnetgame_find_money_rock_drop_tile()'s own doc). */
    if (pcfa_acre_tile_to_town_ut(acre, tile, &ut_x, &ut_z)) {
        int drop_acre, drop_tile;
        if (pcnetgame_find_money_rock_drop_tile(ut_x, ut_z, &drop_acre, &drop_tile)) {
            pcfa_set_tile(drop_acre, drop_tile, (uint16_t)reward_item);
            if (g_pc_verbose) {
                printf("[NET][FIELD_ACTION] host: money-rock hit_count=%d dropped item=0x%04X at acre %d tile %d\n",
                       slot->hit_count, (unsigned)reward_item, drop_acre, drop_tile);
            }
        }
    }
}

/* Bug 1 fix: called from the decomp money-rock hit seam (Player_actor_Search_STONE_TC(),
 * m_player_common.c_inc) INSTEAD of vanilla's ten_coin_entry_ex_proc() -- but for the HOST's OWN LOCAL
 * hit, not a remote peer's request. Single ownership: every money-rock hit, from every source, must
 * update the SAME s_host_money_rock bookkeeping, or two independent writers (vanilla's own local
 * mutation and this table, both touching the same tile) could corrupt a window -- double-revert, a
 * miscounted hit, or a tile stranded on MONEY_FLOWER_SEED. Routing the host's own hit through this
 * table instead makes vanilla's bg_item_ten_coin_c path structurally unreachable for a HOST on a
 * money-rock tile (see Player_actor_Search_STONE_TC()'s own doc).
 * Runs ONLY the source-independent core validation (pcnetgame_validate_money_rock_hit_core()) plus
 * confirming this process is genuinely the host and genuinely in the town scene -- no reach/distance
 * check of its own, because vanilla's own targeting already guarantees this is the host's own real
 * local player standing at this exact tile (unlike a remote peer's unverified claim). No-op (and safe)
 * for single-player or a client, and for any tile that fails validation.
 *
 * Review finding, verified safe: this silently no-ops when s_host_world_ready (checked inside
 * pcnetgame_validate_money_rock_hit_core()) is false. That is unreachable for a genuine player swing:
 * this function is only ever called from Player_actor_Search_STONE_TC()'s own hit-detection, which
 * requires a live, controllable player actor already running in the town scene -- the same
 * precondition (a loaded local save) that also drives pcfa_save_ready()/s_host_world_ready. Vanilla
 * itself blocks player control and actor interaction entirely during a scene/travel transition (no
 * player actor is ticking to call this seam at all), so there is no window where a real swing could
 * reach here while s_host_world_ready is still 0. No fix needed.
 *
 * T0-A: now routes through the GENERIC pcnetgame_host_dispatch_local_field_action() (defined further
 * below, forward-declared here) instead of calling pcnetgame_validate_money_rock_hit_core()/
 * pcnetgame_host_commit_money_rock_hit() directly -- this is a PURE refactor with zero behavioral
 * change: the generic dispatch's is_host_local branch for MONEY_ROCK_HIT (see
 * pcnetgame_fa_validate_adapter_money_rock()) calls the exact same core-validate function this
 * function used to call inline, and its commit adapter calls the exact same commit function with a
 * freshly captured local context, exactly as this function used to do itself. This exists so a FUTURE
 * kind (e.g. T1's tree shake/chop) can get its own host-local entry point for free, by adding one more
 * thin wrapper like this one, instead of hand-rolling its own bespoke validate+commit call pair. */
static int pcnetgame_host_dispatch_local_field_action(uint8_t kind, uint8_t ut_x, uint8_t ut_z);

void pc_net_game_host_local_money_rock_hit(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!pcfa_scene_is_town()) {
        return; /* the host itself is indoors -- ut_x/ut_z from a room are not town coordinates */
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return;
    }
    pcnetgame_host_dispatch_local_field_action((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_MONEY_ROCK_HIT,
                                               (uint8_t)ut_x, (uint8_t)ut_z);
}

/* See pc_net_game.h's own doc. Mirrors pc_net_game_host_local_money_rock_hit()'s own shape exactly. */
void pc_net_game_host_local_tree_shake(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!pcfa_scene_is_town()) {
        return;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return;
    }
    pcnetgame_host_dispatch_local_field_action((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_TREE_SHAKE, (uint8_t)ut_x,
                                               (uint8_t)ut_z);
}

/* See pc_net_game.h's own doc. Mirrors pc_net_game_host_local_money_rock_hit()'s own shape exactly. */
void pc_net_game_host_local_tree_chop(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!pcfa_scene_is_town()) {
        return;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return;
    }
    pcnetgame_host_dispatch_local_field_action((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_TREE_CHOP, (uint8_t)ut_x,
                                               (uint8_t)ut_z);
}

/* World Ecology Stage 1, Item 2: per-poll expiry tick, host-only, mirroring
 * pcnetgame_host_expire_reservations()'s own "run once per poll before this poll's events" placement.
 * Gated on gamePT exactly like the pickup/drop retry timers (needs a valid GAME* for a frame-time
 * delta) -- money rocks can only ever be hit from within active gameplay. On expiry, reverts the tile
 * to orig_item - 7 (ROCK_x), verbatim matching bIT_actor_ten_coin_move()'s own revert
 * (bg_item_common.c_inc) -- deliberately NOT gated on the vanilla oscillator-settle sub-state (mode
 * 1 -> 2 transition), which is a pure LOCAL animation-timing detail this host-side table never
 * models; the host simply reverts once its own deterministic expire_target_frames elapses.
 *
 * Bug 1 fix: before writing the revert, re-check that the tile still actually holds MONEY_FLOWER_SEED
 * (pcfa_get_tile()). With single ownership (all hits, including the host's own, now go through this
 * same bookkeeping) this should always be true, but it costs nothing to verify rather than assume, and
 * it guards against any remaining edge case (e.g. some other path having already changed the tile):
 * if the tile no longer holds MONEY_FLOWER_SEED, the bookkeeping slot is simply cleared without
 * writing anything, rather than clobbering whatever is there now. */
static void pcnetgame_host_check_field_action_money_rock(void) {
    int i;
    float dt;

    if (gamePT == NULL || !s_host_world_ready) {
        return;
    }
    dt = (f32)gamePT->graph->dt_num_60fps_frames;
    for (i = 0; i < PC_NETGAME_MONEY_ROCK_SLOTS; i++) {
        PCNetGameMoneyRockState* slot = &s_host_money_rock[i];
        if (!slot->active) {
            continue;
        }
        slot->expire_accum += dt;
        if (slot->expire_accum >= slot->expire_target_frames) {
            uint16_t cur_value;
            if (pcfa_get_tile(slot->acre, slot->tile, &cur_value) && cur_value == (uint16_t)MONEY_FLOWER_SEED) {
                pcfa_set_tile(slot->acre, slot->tile, (uint16_t)(slot->orig_item - 7));
                if (g_pc_verbose) {
                    printf("[NET][FIELD_ACTION] host: money-rock window at acre %d tile %d expired -- reverted to 0x%04X\n",
                           slot->acre, slot->tile, (unsigned)(slot->orig_item - 7));
                }
            } else if (g_pc_verbose) {
                printf("[NET][FIELD_ACTION] host: money-rock window at acre %d tile %d expired -- tile no longer "
                       "MONEY_FLOWER_SEED (0x%04X), bookkeeping cleared without writing\n",
                       slot->acre, slot->tile, (unsigned)cur_value);
            }
            memset(slot, 0, sizeof(*slot));
        }
    }
}

/* ==================== T0-A: generic field-action validate/commit dispatch table ==================== */

/* World Ecology T0: identifies who is asking for a field-action validate/commit -- either a remote
 * peer (is_host_local == 0; peer/ctx are that peer's own PCNetPeerId/stored PCNetPlayerContext) or the
 * HOST's OWN local action (is_host_local == 1; peer is meaningless, ctx is freshly captured via
 * pcnetgame_capture_local_context()). Exists so a validate/commit function's own body never needs its
 * own bespoke "am I serving a network request or the host's own local action" branch -- see
 * pc_net_game_host_local_money_rock_hit()'s pre-T0 form for why that distinction already existed for
 * MONEY_ROCK_HIT alone, and pcnetgame_host_dispatch_local_field_action() below for how a FUTURE kind
 * (e.g. T1's tree shake/chop) can reuse the same local-dispatch path without inventing its own
 * pc_net_game_host_local_*() function from scratch. */
typedef struct PCNetGameRequester {
    int                is_host_local;
    PCNetPeerId        peer; /* meaningful only when !is_host_local */
    PCNetPlayerContext ctx;  /* the peer's own stored ctx, or the host's own freshly captured local ctx */
    uint8_t            hole_variant; /* protocol v3 -- the requester's own claimed hole_variant (0..24
                                         expected, but NOT YET range-validated here -- see
                                         pcnetgame_validate_hole_variant(), which every validator that
                                         actually consumes this field must call before using it). 0 for
                                         every kind that doesn't use it (see PCNetGameFieldActionRequestMsg's
                                         own doc). */
} PCNetGameRequester;

/* World Ecology T-dig: TRUST BOUNDARY range check for hole_variant (see PCNetGameFieldActionRequestMsg's
 * own doc) -- 1 iff v is a valid HOLE_START offset (0..24, i.e. HOLE00..HOLE24). Any validator that
 * commits HOLE_START + hole_variant to a tile MUST call this first and reject (never clamp) on failure,
 * so a forged/corrupted out-of-range value can never write an unintended tile value. */
static int pcnetgame_validate_hole_variant(uint8_t v) {
    return v <= 24u;
}

/* On entry, *inout_item is unused input (reserved for a future kind that needs one); on a successful
 * (1) return, *inout_item is this kind's own private per-request payload for `commit` to consume
 * (DIG_BURIED: unused; MONEY_ROCK_HIT: the raw_item pcnetgame_validate_money_rock_hit_core() resolved).
 * `commit` then overwrites *inout_item with whatever PCNetGameFieldActionResultMsg.granted_item should
 * actually carry back to the requester (see that struct's own doc: DIG_BURIED's real grant;
 * MONEY_ROCK_HIT's always-0) -- exactly the transformation pcnetgame_handle_host_field_action_request()
 * used to do inline per kind before this table existed. */
typedef int  (*PCNetGameFAValidateFn)(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                       mActor_name_t* inout_item, int* out_acre, int* out_tile);
typedef void (*PCNetGameFACommitFn)(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z, int acre, int tile,
                                     mActor_name_t* inout_item);

typedef struct PCNetGameFAHandler {
    uint8_t                kind;
    PCNetGameFAValidateFn  validate; /* never NULL, even for a stub -- see pcnetgame_fa_validate_stub() */
    PCNetGameFACommitFn    commit;   /* NULL for every stub kind: validate always fails, so commit is
                                         never actually reached for one, but stays NULL defensively */
    const char*            tag;
} PCNetGameFAHandler;

/* T0-A pure-refactor adapters: DIG_BURIED and MONEY_ROCK_HIT. Each calls the SAME, UNMODIFIED
 * validate/commit function this file had before this table existed -- see the SAFETY RULE this
 * milestone's own doc requires (every existing DIG_BURIED/MONEY_ROCK_HIT test must still pass,
 * byte-identically, after this refactor). */
static int pcnetgame_fa_validate_adapter_dig(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                             mActor_name_t* inout_item, int* out_acre, int* out_tile) {
    if (req->is_host_local) {
        return 0; /* no host-local DIG_BURIED path exists yet -- reserved for a future stage */
    }
    if (!pcnetgame_validate_hole_variant(req->hole_variant)) {
        return 0; /* protocol v3 TRUST BOUNDARY -- out-of-range hole_variant is rejected, never clamped */
    }
    return pcnetgame_validate_and_resolve_dig(req->peer, ut_x, ut_z, inout_item, out_acre, out_tile);
}

static void pcnetgame_fa_commit_adapter_dig(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z, int acre,
                                            int tile, mActor_name_t* inout_item) {
    pcnetgame_host_commit_dig_buried(acre, tile, req->hole_variant);
    printf("[NET][FIELD_ACTION] host: peer %d DIG_BURIED at tile (%d,%d) granted item=0x%04X\n",
           req->is_host_local ? -1 : (int)req->peer, (int)ut_x, (int)ut_z, (unsigned)*inout_item);
}

static int pcnetgame_fa_validate_adapter_money_rock(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                    mActor_name_t* inout_item, int* out_acre, int* out_tile) {
    if (req->is_host_local) {
        /* Mirrors pc_net_game_host_local_money_rock_hit()'s pre-T0 form exactly: core validation only,
           no reach/ctx check (the host's own real local targeting already guarantees this is correct --
           see that function's own doc). */
        return pcnetgame_validate_money_rock_hit_core(ut_x, ut_z, out_acre, out_tile, inout_item);
    }
    return pcnetgame_validate_money_rock_hit(req->peer, ut_x, ut_z, out_acre, out_tile, inout_item);
}

static void pcnetgame_fa_commit_adapter_money_rock(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                    int acre, int tile, mActor_name_t* inout_item) {
    (void)ut_x;
    (void)ut_z;
    /* Bug 1 fix precedent, unchanged: commit takes a context pointer, not a peer index -- req->ctx is
       either the peer's own stored PCNetPlayerContext or the host's own freshly captured local one (see
       PCNetGameRequester's doc). *inout_item on entry is the raw_item validate resolved. */
    pcnetgame_host_commit_money_rock_hit(&req->ctx, acre, tile, *inout_item);
    *inout_item = (mActor_name_t)EMPTY_NO; /* see PCNetGameFieldActionResultMsg's doc: MONEY_ROCK_HIT's
                                               granted_item is always 0 */
}

/* World Ecology T1: TREE_SHAKE/TREE_CHOP adapters -- placed here (after PCNetGameRequester exists),
 * unwrapping req->ctx/req->is_host_local/req->peer exactly like the MONEY_ROCK_HIT adapters above, and
 * calling the plain-ctx core validate/commit functions defined earlier in this file (alongside
 * s_tree_drop_tbl/s_tree_cut_hits_tbl). */
static int pcnetgame_fa_validate_adapter_tree_shake(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                    mActor_name_t* inout_item, int* out_acre, int* out_tile) {
    mActor_name_t item;

    if (!pcnetgame_validate_tree_shake_core(ut_x, ut_z, out_acre, out_tile, &item)) {
        return 0;
    }
    if (!req->is_host_local && !pcnetgame_validate_tree_peer_reach(req->peer, ut_x, ut_z)) {
        return 0;
    }
    *inout_item = item;
    return 1;
}

static void pcnetgame_fa_commit_adapter_tree_shake(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                    int acre, int tile, mActor_name_t* inout_item) {
    pcnetgame_host_commit_tree_shake(&req->ctx, ut_x, ut_z, acre, tile, inout_item);
}

static int pcnetgame_fa_validate_adapter_tree_chop(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                   mActor_name_t* inout_item, int* out_acre, int* out_tile) {
    mActor_name_t item;

    if (!pcnetgame_validate_tree_chop_core(ut_x, ut_z, out_acre, out_tile, &item)) {
        return 0;
    }
    if (!req->is_host_local && !pcnetgame_validate_tree_peer_reach(req->peer, ut_x, ut_z)) {
        return 0;
    }
    *inout_item = item;
    return 1;
}

static void pcnetgame_fa_commit_adapter_tree_chop(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                   int acre, int tile, mActor_name_t* inout_item) {
    pcnetgame_host_commit_tree_chop(&req->ctx, ut_x, ut_z, acre, tile, inout_item);
}

/* ==================== World Ecology T-dig: DIG_HOLE / FILL_HOLE / PITFALL_CONSUME / DIG_SHINE ==================== */

/* Byte-exact duplicate of mFI_CheckDigRemoveItem()'s own classification (m_field_info.c) -- duplicated
 * here rather than shared for the same reason MONEY_ROCK_HIT/TREE_SHAKE already duplicate their own
 * vanilla tables in this file: the original is `static` inside a different translation unit. Keep this
 * in sync BY HAND with mFI_CheckDigRemoveItem() if that list ever changes. Per this task's design brief
 * (and mFI_GetDigStatus()'s own T0 doc comment above, m_field_info.c), digging one of these up grants
 * NOTHING -- the removed plant flies off and fades, it is never placed in a pocket. */
static int pcnetgame_is_dig_removable_plant(mActor_name_t item) {
    return (item >= FLOWER_LEAVES_PANSIES0 && item <= FLOWER_TULIP2) ||
           (item >= TREE_STUMP001 && item <= TREE_STUMP004) || (item >= GRASS_A && item <= GRASS_C) ||
           (item == TREE_SAPLING) || (item == TREE_APPLE_SAPLING) || (item == TREE_ORANGE_SAPLING) ||
           (item == TREE_PEACH_SAPLING) || (item == TREE_PEAR_SAPLING) || (item == TREE_CHERRY_SAPLING) ||
           (item == TREE_1000BELLS_SAPLING) || (item == TREE_10000BELLS_SAPLING) ||
           (item == TREE_30000BELLS_SAPLING) || (item == TREE_100BELLS_SAPLING) || (item == DEAD_SAPLING) ||
           (item >= TREE_PALM_STUMP001 && item <= TREE_PALM_STUMP004) || (item == TREE_PALM_SAPLING) ||
           (item == DEAD_PALM_SAPLING) || (item >= CEDAR_TREE_STUMP001 && item <= CEDAR_TREE_STUMP004) ||
           (item == CEDAR_TREE_SAPLING) || (item == DEAD_CEDAR_SAPLING) ||
           (item >= GOLD_TREE_STUMP001 && item <= GOLD_TREE_STUMP004) || (item == GOLD_TREE_SAPLING) ||
           (item == DEAD_GOLD_SAPLING);
}

/* DIG_HOLE (kind 5): tile == EMPTY_NO (a brand-new hole) OR a removable plant (see
 * pcnetgame_is_dig_removable_plant() above). Rejects any hole-type value (HOLE00..24/HOLE_SHINE -- those
 * are FILL_HOLE's or DIG_BURIED's job), RSV_NO, and anything else. hole_variant is TRUST-BOUNDARY range
 * validated by the caller (see PC_NETGAME_FIELD_ACTION_KIND_DIG_HOLE's own handler-table entry). No
 * host-local path is wired to a decomp caller by this task (see this task's own report for the
 * documented client-seam scope decision) but the entry point exists via
 * pc_net_game_host_local_dig_hole() for a future caller, exactly like every other kind's precedent. */
static int pcnetgame_fa_validate_adapter_dig_hole(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                  mActor_name_t* inout_item, int* out_acre, int* out_tile) {
    uint16_t raw_value;
    mActor_name_t raw_item;
    int acre, tile;

    if (!pcnetgame_validate_hole_variant(req->hole_variant)) {
        return 0; /* TRUST BOUNDARY -- see PCNetGameFieldActionRequestMsg's own doc */
    }
    if (!s_host_world_ready) {
        return 0;
    }
    if (!req->is_host_local) {
        if (!s_host_peer[req->peer].ctx_valid || !(req->ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
            return 0;
        }
    }
    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile)) {
        return 0;
    }
    if (pcnetgame_host_tile_reserved_by(acre, tile) >= 0) {
        return 0;
    }
    if (!pcfa_get_tile(acre, tile, &raw_value)) {
        return 0;
    }
    raw_item = (mActor_name_t)raw_value;
    if (raw_item != (mActor_name_t)EMPTY_NO && !pcnetgame_is_dig_removable_plant(raw_item)) {
        return 0; /* not plain ground and not a removable plant -- includes every hole-type value */
    }

    if (!req->is_host_local) {
        float px, py, pz;
        if (!pc_remote_player_get_last_position((PCNetPlayerId)req->peer, &px, &py, &pz)) {
            return 0;
        }
        if (!pcnetgame_pos_valid(px, py, pz)) {
            return 0;
        }
        if (!pcnetgame_field_action_reach_check(ut_x, ut_z, px, py, pz)) {
            return 0;
        }
    }

    *out_acre = acre;
    *out_tile = tile;
    *inout_item = raw_item; /* the PRE-commit tile value -- informational only, see PC_NETGAME_FIELD_ACTION_KIND_DIG_HOLE's own doc */
    return 1;
}

static void pcnetgame_fa_commit_adapter_dig_hole(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z, int acre,
                                                 int tile, mActor_name_t* inout_item) {
    (void)ut_x;
    (void)ut_z;
    pcnetgame_host_commit_dig_buried(acre, tile, req->hole_variant); /* same tile transition as DIG_BURIED's
                                                                         own commit -- tile -> HOLE_START +
                                                                         hole_variant, deposit OFF */
    /* *inout_item already holds the pre-commit tile value on entry -- left as-is, purely informational
       (see PC_NETGAME_FIELD_ACTION_KIND_DIG_HOLE's own doc: nothing is granted). */
}

/* FILL_HOLE (kind 6): tile is a hole-type value (HOLE00..24 or HOLE_SHINE) with deposit OFF -- filling
 * an EMPTY hole back in. A hole-type value with deposit ON is a CANCEL/reflect per this task's design
 * brief (the client already reflects locally; no request should ever be sent for that case, but this
 * validator rejects it defensively regardless, matching "never trust the client's own classification"). */
static int pcnetgame_fa_validate_adapter_fill_hole(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                    mActor_name_t* inout_item, int* out_acre, int* out_tile) {
    uint16_t raw_value;
    int acre, tile;

    if (!s_host_world_ready) {
        return 0;
    }
    if (!req->is_host_local) {
        if (!s_host_peer[req->peer].ctx_valid || !(req->ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
            return 0;
        }
    }
    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile)) {
        return 0;
    }
    if (pcnetgame_host_tile_reserved_by(acre, tile) >= 0) {
        return 0;
    }
    if (pcfa_get_deposit(acre, tile) != 0) {
        return 0; /* a hole with a deposit is CANCEL, not FILL -- see this function's own doc */
    }
    if (!pcfa_get_tile(acre, tile, &raw_value)) {
        return 0;
    }
    if (!ITEM_IS_HOLE(raw_value) && raw_value != (uint16_t)HOLE_SHINE) {
        return 0; /* not a hole at all */
    }

    if (!req->is_host_local) {
        float px, py, pz;
        if (!pc_remote_player_get_last_position((PCNetPlayerId)req->peer, &px, &py, &pz)) {
            return 0;
        }
        if (!pcnetgame_pos_valid(px, py, pz)) {
            return 0;
        }
        if (!pcnetgame_field_action_reach_check(ut_x, ut_z, px, py, pz)) {
            return 0;
        }
    }

    *out_acre = acre;
    *out_tile = tile;
    *inout_item = (mActor_name_t)EMPTY_NO;
    return 1;
}

static void pcnetgame_fa_commit_adapter_fill_hole(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                   int acre, int tile, mActor_name_t* inout_item) {
    (void)req;
    (void)ut_x;
    (void)ut_z;
    pcfa_set_tile(acre, tile, (uint16_t)EMPTY_NO); /* HOLE_SHINE also just becomes EMPTY_NO -- filling it
                                                       destroys the shine spot, correct vanilla behavior,
                                                       not a bug (see this task's design brief) */
    *inout_item = (mActor_name_t)EMPTY_NO;
}

/* PITFALL_CONSUME (kind 7): a player/villager falling INTO an already-buried pitfall -- a TRIGGER,
 * distinct from DIGGING one up (which stays on the extended DIG_BURIED path, kind 1). Validates tile is
 * BURIED_PITFALL_HOLE00..24 with deposit OFF and not reserved; commits tile -> EMPTY_NO directly
 * (deliberately skipping vanilla's transient HOLE_n stage -- safe, no item is ever granted here). On
 * REJECT (another peer already consumed it, or DIG_BURIED already dug it up), granted_item in the RESULT
 * carries the host's OWN CURRENT tile value so the client can reconcile its optimistic local fall
 * animation against reality without a full resync -- see pcnetgame_handle_client_field_action_result()'s
 * own PITFALL_CONSUME branch and pc_net_game_request_pitfall_consume()'s own doc. No reach check beyond
 * the ordinary field-action envelope: unlike a deliberate dig, a fall's positional legitimacy is already
 * governed by the actor simply being on that tile (the host does not additionally verify this -- a
 * forged PITFALL_CONSUME at worst clears a pitfall the sender wasn't actually standing on, which is a
 * bounded, non-duplicating, non-crashing outcome no worse than an ordinary out-of-reach pickup attempt). */
/* Best-effort re-read of the host's CURRENT tile value at (ut_x,ut_z) for a PITFALL_CONSUME reject
 * echo -- see pcnetgame_fa_validate_adapter_pitfall_consume()'s own doc: on ANY reject, not only the
 * "tile no longer a pitfall" case, the client needs the host's actual current tile value to reconcile
 * its optimistic local fall animation. A fresh, independent pcfa_town_ut_to_acre_tile()/pcfa_get_tile()
 * pair -- never reuses any value captured earlier in the caller. Pure address-space reads (no state
 * mutation), safe to attempt even when s_host_world_ready is false or the requester's context hasn't
 * been validated yet: pcfa_town_ut_to_acre_tile() is bounds-checked arithmetic and pcfa_get_tile() only
 * ever reads the always-resident Save_t tile grid (see pc_field_authority.c). Silently leaves *out_item
 * at its EMPTY_NO initializer if the tile can't be resolved/read at all (nothing valid to echo). */
static void pcnetgame_fa_pitfall_consume_echo_current(uint8_t ut_x, uint8_t ut_z, mActor_name_t* out_item) {
    int acre, tile;
    uint16_t value;

    if (pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile) && pcfa_get_tile(acre, tile, &value)) {
        /* Bug 2 fix: a TRANSIENT or AMBIGUOUS current value (e.g. RSV_NO mid another player's pitfall-
         * fall/bury-commit) must never be echoed as a hard reconciliation value -- the client applies
         * granted_item unconditionally via pcnetgame_client_apply_tile() on a PITFALL_CONSUME reject
         * (unlike a FIELD_UPDATE broadcast, which drops TRANSIENT but still lets AMBIGUOUS through and
         * then treats it as "not overwritable" until it settles, per that function's own doc). Silently
         * leave *out_item at its EMPTY_NO initializer instead, matching this function's own documented
         * "nothing valid to echo" behavior for an unresolvable tile. */
        if (pcfa_transient_kind(value) == PCFA_VALUE_STABLE) {
            *out_item = (mActor_name_t)value;
        }
    }
}

static int pcnetgame_fa_validate_adapter_pitfall_consume(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                          mActor_name_t* inout_item, int* out_acre, int* out_tile) {
    uint16_t raw_value;
    int acre, tile;

    if (!s_host_world_ready) {
        /* Reject-reconciliation fix (Opus T2 review): echo the host's current tile value on EVERY
           reject path, not only the "no longer a pitfall" case below -- see
           pcnetgame_fa_pitfall_consume_echo_current()'s own doc. */
        pcnetgame_fa_pitfall_consume_echo_current(ut_x, ut_z, inout_item);
        return 0;
    }
    if (!req->is_host_local) {
        if (!s_host_peer[req->peer].ctx_valid || !(req->ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
            pcnetgame_fa_pitfall_consume_echo_current(ut_x, ut_z, inout_item);
            return 0;
        }
    }
    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile)) {
        return 0; /* can't even resolve an acre/tile for this ut -- nothing valid to echo */
    }
    if (pcnetgame_host_tile_reserved_by(acre, tile) >= 0) {
        pcnetgame_fa_pitfall_consume_echo_current(ut_x, ut_z, inout_item);
        return 0;
    }
    if (pcfa_get_deposit(acre, tile) != 0) {
        pcnetgame_fa_pitfall_consume_echo_current(ut_x, ut_z, inout_item);
        return 0;
    }
    if (!pcfa_get_tile(acre, tile, &raw_value)) {
        return 0; /* the read itself failed -- nothing valid to echo */
    }
    if (!ITEM_IS_BURIED_PITFALL_HOLE(raw_value)) {
        /* Already consumed by a racing peer, or already dug up -- reject, and hand back the host's own
           current value so the client can reconcile its optimistic fall (see this function's own doc).
           Bug 2 fix: only when that current value is STABLE -- a TRANSIENT/AMBIGUOUS raw_value (e.g.
           RSV_NO mid another player's own racing pitfall-consume/bury-commit) must not be handed to the
           client as a hard reconciliation value (see pcnetgame_fa_pitfall_consume_echo_current()'s own
           doc); leave *inout_item at its EMPTY_NO initializer instead. */
        if (pcfa_transient_kind(raw_value) == PCFA_VALUE_STABLE) {
            *inout_item = (mActor_name_t)raw_value;
        }
        return 0;
    }

    *out_acre = acre;
    *out_tile = tile;
    *inout_item = (mActor_name_t)EMPTY_NO;
    return 1;
}

static void pcnetgame_fa_commit_adapter_pitfall_consume(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                         int acre, int tile, mActor_name_t* inout_item) {
    (void)req;
    (void)ut_x;
    (void)ut_z;
    pcfa_set_tile(acre, tile, (uint16_t)EMPTY_NO);
    *inout_item = (mActor_name_t)EMPTY_NO;
}

/* DIG_SHINE (kind 8): tile == SHINE_SPOT (unburied) -- deposit is always OFF for a shine spot in
 * practice, checked anyway for defense-in-depth. hole_variant is IGNORED for this kind (a shine hole is
 * not variant-shaped) -- commits tile -> HOLE_SHINE unconditionally. Grants NOTHING host-side: the
 * digging client rolls its own bell amount locally and grants it privately on accept (see this task's
 * own design brief and PC_NETGAME_FIELD_ACTION_KIND_DIG_SHINE's own doc). */
static int pcnetgame_fa_validate_adapter_dig_shine(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                    mActor_name_t* inout_item, int* out_acre, int* out_tile) {
    uint16_t raw_value;
    int acre, tile;

    if (!s_host_world_ready) {
        return 0;
    }
    if (!req->is_host_local) {
        if (!s_host_peer[req->peer].ctx_valid || !(req->ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
            return 0;
        }
    }
    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile)) {
        return 0;
    }
    if (pcnetgame_host_tile_reserved_by(acre, tile) >= 0) {
        return 0;
    }
    if (pcfa_get_deposit(acre, tile) != 0) {
        return 0;
    }
    if (!pcfa_get_tile(acre, tile, &raw_value)) {
        return 0;
    }
    if (raw_value != (uint16_t)SHINE_SPOT) {
        return 0;
    }

    if (!req->is_host_local) {
        float px, py, pz;
        if (!pc_remote_player_get_last_position((PCNetPlayerId)req->peer, &px, &py, &pz)) {
            return 0;
        }
        if (!pcnetgame_pos_valid(px, py, pz)) {
            return 0;
        }
        if (!pcnetgame_field_action_reach_check(ut_x, ut_z, px, py, pz)) {
            return 0;
        }
    }

    *out_acre = acre;
    *out_tile = tile;
    *inout_item = (mActor_name_t)EMPTY_NO;
    return 1;
}

static void pcnetgame_fa_commit_adapter_dig_shine(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                   int acre, int tile, mActor_name_t* inout_item) {
    (void)req;
    (void)ut_x;
    (void)ut_z;
    pcfa_set_tile(acre, tile, (uint16_t)HOLE_SHINE); /* hole_variant ignored -- see this kind's own doc */
    *inout_item = (mActor_name_t)EMPTY_NO;
}

/* World Ecology: snowmen -- SNOWMAN_BREAK (kind 9) adapters. No host-local path: the HOST's own local
 * break already goes through vanilla's aPSM_actor_move()/mSN_ClearSnowman() completely unmodified
 * (single ownership is preserved not by routing it through here, but because
 * pcnetgame_host_check_snowman_state_diff() picks up that local Save_t.snowmen write on its own, the
 * same way it picks up melt -- see this task's own doc). `*inout_item` carries the resolved
 * SNOWMANx tile value (the private per-request payload validate hands to commit); the RESULT's own
 * granted_item is always 0 (see PCNetGameFieldActionResultMsg's doc -- nothing is ever granted by a
 * break), so commit clears it before returning. */
static int pcnetgame_fa_validate_adapter_snowman_break(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                        mActor_name_t* inout_item, int* out_acre, int* out_tile) {
    uint16_t value;
    int acre, tile, slot;
    float px, py, pz;
    mSN_snowman_data_c* data;

    if (req->is_host_local) {
        return 0; /* see this function's own doc: the host's own local break stays on vanilla's path */
    }
    if (!s_host_world_ready) {
        return 0;
    }
    if (!s_host_peer[req->peer].ctx_valid || !(req->ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
        return 0;
    }
    if (!pcfa_town_ut_to_acre_tile((int)ut_x, (int)ut_z, &acre, &tile)) {
        return 0;
    }
    if (!pcfa_get_tile(acre, tile, &value)) {
        return 0;
    }
    pcnetgame_host_resolve_snowman_tile_overlay(acre, tile, &value);
    if (value < (uint16_t)SNOWMAN0 || value > (uint16_t)SNOWMAN8) {
        return 0; /* not (or no longer) a snowman tile -- nothing to break */
    }
    slot = (int)(value - (uint16_t)SNOWMAN0) / mSN_SAVE_COUNT;
    if (slot < 0 || slot >= mSN_SAVE_COUNT) {
        return 0;
    }
    data = Save_GetPointer(snowmen.snowmen_data[slot]);
    if (!data->exists) {
        return 0; /* the tile still shows a SNOWMANx value but the slot itself is already gone */
    }

    /* Lenient reach check -- reuses the SAME envelope DIG_BURIED/MONEY_ROCK_HIT/tree shake/chop already
       share (pcnetgame_field_action_reach_check()), not a tight exact-tile match: players break
       snowmen at ordinary melee range, same as trees (see this task's own brief). */
    if (!pc_remote_player_get_last_position((PCNetPlayerId)req->peer, &px, &py, &pz)) {
        return 0;
    }
    if (!pcnetgame_pos_valid(px, py, pz)) {
        return 0;
    }
    if (!pcnetgame_field_action_reach_check(ut_x, ut_z, px, py, pz)) {
        return 0;
    }

    *out_acre = acre;
    *out_tile = tile;
    *inout_item = (mActor_name_t)value;
    return 1;
}

static void pcnetgame_fa_commit_adapter_snowman_break(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                                       int acre, int tile, mActor_name_t* inout_item) {
    int slot = (int)((uint16_t)*inout_item - (uint16_t)SNOWMAN0) / mSN_SAVE_COUNT;
    int cleared_via_actor = 0;

    memset(Save_GetPointer(snowmen.snowmen_data[slot]), 0, sizeof(mSN_snowman_data_c));

    /* If the HOST's own process still has a live PSNOWMAN actor for this exact snowman, clear its
       npc_id to EMPTY_NO and delete it through the ordinary actor path -- its own restore-on-destroy
       (restore_fgdata(), m_actor.c) then writes nothing back (name == EMPTY_NO), exactly mirroring what
       a client applying this same break via SNOWMAN_STATE does on ITS side (see
       pcnetgame_handle_client_snowman_state()). Otherwise the field write below is the only thing that
       needs to happen -- no live actor is straddling this tile on the host. */
    if (gamePT != NULL && gamePT->exec == play_main && pcfa_scene_is_town()) {
        GAME_PLAY* play = (GAME_PLAY*)gamePT;
        ACTOR* actor;
        for (actor = play->actor_info.list[ACTOR_PART_BG].actor; actor != NULL; actor = actor->next_actor) {
            if (actor->restore_fg && actor->npc_id == (mActor_name_t)*inout_item) {
                actor->npc_id = (mActor_name_t)EMPTY_NO;
                /* Safety: if this actor is the local player's live dialogue partner right now
                 * (m_demo.c sets ACTOR_STATE_IN_DEMO on demo->current.actor for the duration of the
                 * talk and writes through that same pointer when the talk ends), do NOT delete it out
                 * from under the open dialogue -- that would leave m_demo.c holding a dangling
                 * actor pointer (use-after-free on dialogue-close). npc_id is already severed above,
                 * so the actor is already "gone" for every save/tile purpose; its own per-frame
                 * mRlib_PSnowman_NormalTalk() check (aPSM_actor_move(), ac_psnowman.c) will call
                 * Actor_delete() itself the moment the talk actually ends, exactly like vanilla's
                 * own end-of-conversation cleanup. */
                if (!(actor->state_bitfield & ACTOR_STATE_IN_DEMO)) {
                    Actor_delete(actor);
                }
                cleared_via_actor = 1;
                break;
            }
        }
    }
    if (!cleared_via_actor) {
        pcfa_set_tile(acre, tile, (uint16_t)EMPTY_NO);
    }

    printf("[NET][SNOWMAN] host: peer %d SNOWMAN_BREAK at tile (%d,%d) slot %d\n",
           req->is_host_local ? -1 : (int)req->peer, (int)ut_x, (int)ut_z, slot);

    pcnetgame_host_check_snowman_state_diff();
    *inout_item = (mActor_name_t)EMPTY_NO; /* granted_item is always 0 for a break */
}

/* T0-A: stub validate for every FIELD_ACTION kind reserved for a FUTURE task (T1 trees, T2 dig-
 * classification fixes, T3 bury, T5 snowmen -- see this milestone's own scope doc) but not yet
 * implemented in this build. Always rejects -- a request carrying one of these kinds (none of which
 * any caller in this build ever sends) gets an ordinary RESULT(accepted=0), the same shape DIG_BURIED/
 * MONEY_ROCK_HIT's own validators already produce for a request that fails their real checks. */
static int pcnetgame_fa_validate_stub(const PCNetGameRequester* req, uint8_t ut_x, uint8_t ut_z,
                                      mActor_name_t* inout_item, int* out_acre, int* out_tile) {
    (void)req;
    (void)ut_x;
    (void)ut_z;
    (void)inout_item;
    (void)out_acre;
    (void)out_tile;
    return 0;
}

/* T0-A: the FIELD_ACTION_REQUEST dispatch table -- replaces the previous if/else kind chain in
 * pcnetgame_handle_host_field_action_request(). Kinds 3-10 are placeholders: a future task (T1/T2/T3/
 * T5) implements its kind by replacing its stub `validate`/`commit` pair here, never by touching this
 * table's shape or pcnetgame_handle_host_field_action_request() itself. A kind with NO entry here at
 * all (e.g. 0, or 11+) is still silently ignored with no reply at all, exactly like the pre-refactor
 * if/else's final `else { return; }` branch -- see pcnetgame_find_field_action_handler(). */
static const PCNetGameFAHandler s_field_action_handlers[] = {
    { (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_BURIED, pcnetgame_fa_validate_adapter_dig,
      pcnetgame_fa_commit_adapter_dig, "DIG_BURIED" },
    { (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_MONEY_ROCK_HIT, pcnetgame_fa_validate_adapter_money_rock,
      pcnetgame_fa_commit_adapter_money_rock, "MONEY_ROCK_HIT" },
    { (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_TREE_SHAKE, pcnetgame_fa_validate_adapter_tree_shake,
      pcnetgame_fa_commit_adapter_tree_shake, "TREE_SHAKE" },
    { (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_TREE_CHOP, pcnetgame_fa_validate_adapter_tree_chop,
      pcnetgame_fa_commit_adapter_tree_chop, "TREE_CHOP" },
    { (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_HOLE, pcnetgame_fa_validate_adapter_dig_hole,
      pcnetgame_fa_commit_adapter_dig_hole, "DIG_HOLE" },
    { (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_FILL_HOLE, pcnetgame_fa_validate_adapter_fill_hole,
      pcnetgame_fa_commit_adapter_fill_hole, "FILL_HOLE" },
    { (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_PITFALL_CONSUME, pcnetgame_fa_validate_adapter_pitfall_consume,
      pcnetgame_fa_commit_adapter_pitfall_consume, "PITFALL_CONSUME" },
    { (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_SHINE, pcnetgame_fa_validate_adapter_dig_shine,
      pcnetgame_fa_commit_adapter_dig_shine, "DIG_SHINE" },
    { (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_SNOWMAN_BREAK, pcnetgame_fa_validate_adapter_snowman_break,
      pcnetgame_fa_commit_adapter_snowman_break, "SNOWMAN_BREAK" },
    { 10, pcnetgame_fa_validate_stub, NULL, "RESERVED_10" },
};
#define PC_NETGAME_FIELD_ACTION_HANDLER_COUNT \
    (sizeof(s_field_action_handlers) / sizeof(s_field_action_handlers[0]))

static const PCNetGameFAHandler* pcnetgame_find_field_action_handler(uint8_t kind) {
    size_t i;
    for (i = 0; i < PC_NETGAME_FIELD_ACTION_HANDLER_COUNT; i++) {
        if (s_field_action_handlers[i].kind == kind) {
            return &s_field_action_handlers[i];
        }
    }
    return NULL;
}

/* T0-A: generic host-local dispatch -- given a FIELD_ACTION kind and the HOST's own local ut_x/ut_z,
 * builds a host-local PCNetGameRequester and runs that kind's table entry exactly as if a peer had
 * requested it, without ever touching the network. Mirrors pc_net_game_host_local_money_rock_hit()'s
 * own pre-T0 reasoning (single ownership: every hit/dig/etc., from every source, must update the same
 * host-side bookkeeping through the same validate/commit pair) but generically, for any kind in the
 * table. Returns 1 iff the action was accepted and committed. No result message is sent (there is no
 * peer to send one to); a committed field mutation still reaches every client the ordinary way, via
 * the commit function's own pcfa_set_tile() plus the caller's later dirty-flush/pcnetgame_host_flush_mask()
 * pass. NOT wired to any new caller in T0 -- only pc_net_game_host_local_money_rock_hit() below uses it,
 * exactly reproducing its own pre-T0 behavior. */
static int pcnetgame_host_dispatch_local_field_action_ex(uint8_t kind, uint8_t ut_x, uint8_t ut_z,
                                                          uint8_t hole_variant) {
    const PCNetGameFAHandler* h;
    PCNetGameRequester req;
    mActor_name_t item = (mActor_name_t)EMPTY_NO;
    int acre = 0, tile = 0;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return 0;
    }
    h = pcnetgame_find_field_action_handler(kind);
    if (h == NULL) {
        return 0;
    }

    memset(&req, 0, sizeof(req));
    req.is_host_local = 1;
    req.peer = -1;
    req.hole_variant = hole_variant;
    pcnetgame_capture_local_context(&req.ctx);

    if (!h->validate(&req, ut_x, ut_z, &item, &acre, &tile)) {
        return 0;
    }
    if (h->commit != NULL) {
        h->commit(&req, ut_x, ut_z, acre, tile, &item);
    }
    return 1;
}

/* Pre-v3 call shape, kept for every EXISTING caller (MONEY_ROCK_HIT, TREE_SHAKE, TREE_CHOP) unmodified --
 * none of these kinds use hole_variant. */
static int pcnetgame_host_dispatch_local_field_action(uint8_t kind, uint8_t ut_x, uint8_t ut_z) {
    return pcnetgame_host_dispatch_local_field_action_ex(kind, ut_x, ut_z, 0);
}

/* World Ecology Stage 1: client -> host FIELD_ACTION_REQUEST, dispatched by kind through the T0-A
 * table above. No two-phase reservation (see PCNetGameFieldActionRequestMsg's own doc) -- each kind
 * validates and commits synchronously in this same call before replying, so a retried/duplicate
 * request_id is answered from s_host_field_action_dedup[peer] instead of being re-validated (which for
 * MONEY_ROCK_HIT would otherwise double-count a hit). */
static void pcnetgame_handle_host_field_action_request(PCNetPeerId peer, const PCNetGameFieldActionRequestMsg* in) {
    PCNetGameFieldActionDedup* dedup;
    const PCNetGameFAHandler* h;
    PCNetGameRequester req;
    int accepted = 0;
    mActor_name_t item = (mActor_name_t)EMPTY_NO;
    int acre = 0, tile = 0;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }

    dedup = &s_host_field_action_dedup[peer];
    if (dedup->valid && dedup->request_id == in->request_id && dedup->kind == in->kind) {
        PCNetGameFieldActionResultMsg out;
        memset(&out, 0, sizeof(out));
        out.msg_type = (uint8_t)PC_NETGAME_MSG_FIELD_ACTION_RESULT;
        out.kind = dedup->kind;
        out.accepted = dedup->accepted;
        out.ut_x = dedup->ut_x;
        out.ut_z = dedup->ut_z;
        out.request_id = dedup->request_id;
        out.granted_item = dedup->granted_item;
        pcnetgame_host_send_result(peer, &out, (uint16_t)sizeof(out), "FIELD_ACTION_RESULT (replay)");
        return;
    }

    h = pcnetgame_find_field_action_handler(in->kind);
    if (h == NULL) {
        return; /* unknown kind -- ignore rather than misinterpret (identical to the pre-refactor
                    if/else's own final `else { return; }` branch) */
    }

    memset(&req, 0, sizeof(req));
    req.is_host_local = 0;
    req.peer = peer;
    req.ctx = s_host_peer[peer].ctx;
    req.hole_variant = in->hole_variant; /* protocol v3 -- TRUST BOUNDARY, see PCNetGameFieldActionRequestMsg's
                                             own doc; each validator that actually uses this must range-check
                                             it itself via pcnetgame_validate_hole_variant() */

    accepted = h->validate(&req, in->ut_x, in->ut_z, &item, &acre, &tile);
    if (accepted && h->commit != NULL) {
        h->commit(&req, in->ut_x, in->ut_z, acre, tile, &item);
    }

    dedup->valid = 1;
    dedup->request_id = in->request_id;
    dedup->kind = in->kind;
    dedup->accepted = (uint8_t)(accepted != 0);
    dedup->ut_x = in->ut_x;
    dedup->ut_z = in->ut_z;
    /* World Ecology T-dig: on REJECT, `item` is still propagated (not forced to EMPTY_NO) -- every
       validator except PITFALL_CONSUME's leaves it at its EMPTY_NO initializer on a reject anyway, so
       this is a no-op for them; PITFALL_CONSUME's own validator (see
       pcnetgame_fa_validate_adapter_pitfall_consume()) deliberately sets it to the host's CURRENT tile
       value on reject so the client can reconcile its optimistic local fall (see that function's own
       doc and pc_net_game_request_pitfall_consume()'s own doc). */
    dedup->granted_item = (uint16_t)item;

    {
        PCNetGameFieldActionResultMsg out;
        memset(&out, 0, sizeof(out));
        out.msg_type = (uint8_t)PC_NETGAME_MSG_FIELD_ACTION_RESULT;
        out.kind = in->kind;
        out.accepted = dedup->accepted;
        out.ut_x = in->ut_x;
        out.ut_z = in->ut_z;
        out.request_id = in->request_id;
        out.granted_item = dedup->granted_item;
        pcnetgame_host_send_result(peer, &out, (uint16_t)sizeof(out), "FIELD_ACTION_RESULT");
    }
}

/* World Ecology: snowmen. Host side: client -> host SNOWMAN_BUILD_REQUEST. NOT a FIELD_ACTION kind
 * (see PCNetGameSnowmanBuildRequestMsg's own doc for why) -- its own dedicated request/result pair,
 * dispatched directly from pcnetgame_handle_host_data(), with its own per-peer dedup record
 * (s_host_snowman_build_dedup) mirroring s_host_field_action_dedup's own replay-on-retry shape.
 * Deliberately does NOT check reach or IN_TOWN for the requester (see this function's own header doc,
 * pc_net_game.h) -- acceptance is judged purely from the HOST's own field/slot state. Mirrors
 * mSN_regist_snowman_society() (m_snowman.c) in Save-space: on acceptance, stamps
 * Save_t.snowmen.snowmen_data[slot], keeps a displaced ITEM1/FTR item via mPB_keep_item() (matching
 * m_police_box.c:76-94's own classification -- anything else is simply lost on this path, same as
 * vanilla), writes the SNOWMANx tile, and stamps the completion dates from the host's OWN currently-
 * synced clock (Common_Get(time.rtc_time...), exactly like aSMAN_process_combine_head_jump_init()'s own
 * vanilla stamp, ac_snowman.c) -- the host is the sole clock authority (see the N-clock milestone).
 * Calls pcnetgame_host_check_snowman_state_diff() itself, right after the write, so SNOWMAN_STATE
 * reaches every client no later than the ordinary FIELD_UPDATE the tile write also triggers. */
static void pcnetgame_handle_host_snowman_build_request(PCNetPeerId peer, const PCNetGameSnowmanBuildRequestMsg* in) {
    PCNetGameSnowmanBuildDedup* dedup;
    PCNetGameSnowmanBuildResultMsg out;
    int acre = 0, tile = 0;
    uint16_t value = (uint16_t)EMPTY_NO;
    int slot = 0xFF;
    uint8_t reason = (uint8_t)PC_NETGAME_SNOWMAN_REASON_NONE;
    int accepted = 0;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }

    dedup = &s_host_snowman_build_dedup[peer];
    if (dedup->valid && dedup->request_id == in->request_id) {
        memset(&out, 0, sizeof(out));
        out.msg_type = (uint8_t)PC_NETGAME_MSG_SNOWMAN_BUILD_RESULT;
        out.accepted = dedup->accepted;
        out.ut_x = in->ut_x;
        out.ut_z = in->ut_z;
        out.request_id = in->request_id;
        out.slot = dedup->slot;
        out.reason = dedup->reason;
        pcnetgame_host_send_result(peer, &out, (uint16_t)sizeof(out), "SNOWMAN_BUILD_RESULT (replay)");
        return;
    }

    if (!s_host_world_ready || !pcfa_save_ready()) {
        reason = (uint8_t)PC_NETGAME_SNOWMAN_REASON_NOT_READY;
    } else if (!pcfa_town_ut_to_acre_tile((int)in->ut_x, (int)in->ut_z, &acre, &tile) ||
               !pcfa_get_tile(acre, tile, &value)) {
        reason = (uint8_t)PC_NETGAME_SNOWMAN_REASON_TILE_INVALID;
    } else {
        pcnetgame_host_resolve_snowman_tile_overlay(acre, tile, &value);

        if ((value >= (uint16_t)SNOWMAN0 && value <= (uint16_t)SNOWMAN8) || value == (uint16_t)RSV_NO ||
            value == (uint16_t)RSV_WALL_NO || pcfa_is_transient_value(value) ||
            ITEM_NAME_GET_TYPE((mActor_name_t)value) == NAME_TYPE_STRUCT ||
            ITEM_NAME_GET_TYPE((mActor_name_t)value) == NAME_TYPE_PROPS ||
            pcnetgame_host_tile_reserved_by(acre, tile) >= 0) {
            reason = (uint8_t)PC_NETGAME_SNOWMAN_REASON_TILE_INVALID;
        } else {
            mSN_snowman_data_c* snowmen = Save_GetPointer(snowmen.snowmen_data[0]);
            int free_slot = -1;
            int i;

            for (i = 0; i < mSN_SAVE_COUNT; i++) {
                if (!snowmen[i].exists) {
                    free_slot = i;
                    break;
                }
            }
            if (free_slot < 0) {
                reason = (uint8_t)PC_NETGAME_SNOWMAN_REASON_SLOTS_FULL;
            } else {
                uint16_t prior = value;
                uint8_t score = in->score;

                if (score > 3) {
                    printf("[NET][SNOWMAN] host: peer %d BUILD_REQUEST score %u clamped to 3\n", (int)peer,
                           (unsigned)score);
                    score = 3;
                }

                snowmen[free_slot].exists = 1;
                snowmen[free_slot].head_size = in->head_size;
                snowmen[free_slot].body_size = in->body_size;
                snowmen[free_slot].score = score;

                if (prior != (uint16_t)EMPTY_NO) {
                    mPB_keep_item((mActor_name_t)prior); /* no-op unless ITEM1/FTR -- see doc above */
                    pcfa_set_deposit(acre, tile, 0);
                }
                pcfa_set_tile(acre, tile, (uint16_t)(SNOWMAN0 + free_slot * mSN_SAVE_COUNT));

                Save_Set(snowman_year, (u8)(Common_Get(time.rtc_time.year) % 100));
                Save_Set(snowman_month, Common_Get(time.rtc_time.month));
                Save_Set(snowman_day, Common_Get(time.rtc_time.day));
                Save_Set(snowman_hour, Common_Get(time.rtc_time.hour));

                slot = free_slot;
                accepted = 1;
                reason = (uint8_t)PC_NETGAME_SNOWMAN_REASON_NONE;

                pcnetgame_host_check_snowman_state_diff();

                printf("[NET][SNOWMAN] host: peer %d BUILD accepted at tile (%d,%d) slot %d\n", (int)peer,
                       (int)in->ut_x, (int)in->ut_z, slot);
            }
        }
    }

    dedup->valid = 1;
    dedup->request_id = in->request_id;
    dedup->accepted = (uint8_t)accepted;
    dedup->slot = (uint8_t)slot;
    dedup->reason = reason;

    memset(&out, 0, sizeof(out));
    out.msg_type = (uint8_t)PC_NETGAME_MSG_SNOWMAN_BUILD_RESULT;
    out.accepted = (uint8_t)accepted;
    out.ut_x = in->ut_x;
    out.ut_z = in->ut_z;
    out.request_id = in->request_id;
    out.slot = (uint8_t)slot;
    out.reason = reason;
    pcnetgame_host_send_result(peer, &out, (uint16_t)sizeof(out), "SNOWMAN_BUILD_RESULT");
    if (!accepted) {
        printf("[NET][SNOWMAN] host: peer %d BUILD rejected at tile (%d,%d) reason %u\n", (int)peer, (int)in->ut_x,
               (int)in->ut_z, (unsigned)reason);
    }
}

/* ==================== World Ecology Wildlife Sync T-catch (ordinary fish catching only) ==================== */

/* Forward-declared: defined later in this file alongside the other broadcast-to-every-READY-client
   helpers (pcnetgame_broadcast_villager_msg()'s own doc, further down); used here
   (pcnetgame_commit_catch_despawn()) before that point in the file -- same forward-declaration shape
   already used elsewhere in this file for other later-defined helpers. */
static void pcnetgame_broadcast_villager_msg(const void* msg, size_t msg_size);

/* Broadcasts WILDLIFE_DESPAWN to every READY client (pcnetgame_broadcast_villager_msg() -- see that
 * function's own doc: peers mid-snapshot are skipped, since their own in-flight snapshot, built from the
 * CURRENT authoritative table, already reflects this removal) AND reconciles the HOST's OWN local
 * presentation actor, if any, via pcwld_handle_wildlife_despawn() -- the host is itself a "process" that
 * may have materialized a local fish actor for entity_id (T1 self-presentation), so it needs exactly the
 * same 3-case reconciliation every other receiver gets. Called ONLY after pcwld_remove_by_id() has
 * already removed the entity from the authoritative table (both call sites below do this atomically,
 * before calling here) -- never the other way around. */
static void pcnetgame_commit_catch_despawn(uint32_t entity_id) {
    PCNetGameWildlifeDespawnMsg msg;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_WILDLIFE_DESPAWN;
    msg.entity_id = entity_id;
    pcnetgame_broadcast_villager_msg(&msg, sizeof(msg));

    pcwld_handle_wildlife_despawn(entity_id);
}

/* Every read-only validation step for one CATCH claim, in sequence, stopping at the first failure, PLUS
 * the atomic accept-time removal itself -- see this function's own race-safety doc below. `is_host_local`
 * skips the PLAYER_CONTEXT/IN_TOWN/reach checks for the SAME reason every other is_host_local exemption
 * in this file does (the host's own real local targeting -- its own fishing rod actually hooking this
 * exact fish -- already guarantees correctness); `peer` is only read when !is_host_local.
 *
 * RACE SAFETY (the CORE requirement for T-catch): this file's host-side message processing is single-
 * threaded (confirmed by this milestone's own audit) -- two CATCH_REQUESTs for the SAME entity_id can
 * therefore never be "in progress" at the same instant; whichever is processed first reaches
 * pcwld_remove_by_id() first, which immediately makes pcwld_find_by_id() start returning 0 for that
 * entity_id. The SECOND request (processed strictly after the first, on this same single-threaded loop)
 * therefore fails this function's own pcwld_find_by_id() check above and is rejected -- no window ever
 * exists where both could pass validation. This is why acceptance and removal happen TOGETHER, inside
 * this one function, with nothing else able to run in between -- never "validate now, remove later". */
static int pcnetgame_validate_and_commit_catch(int is_host_local, PCNetPeerId peer, uint32_t entity_id,
                                               uint32_t generation, int claimed_species) {
    PcWildlifeRecord rec;

    if (!pc_net_game_authoritative_wildlife_enabled()) {
        return 0; /* opt-in gate off -- never accept a catch the host itself isn't running this milestone
                     for (mirrors every other wildlife handler's own gate, pc_net_game.c) */
    }
    if (!pcfa_scene_is_town()) {
        return 0; /* the host's own loaded scene must be the town -- mirrors pcwld_host_spawn_trigger()'s
                     own precedent for touching town-scene-only authoritative wildlife state */
    }
    if (entity_id == 0) {
        return 0;
    }
    if (!pcwld_find_by_id(entity_id, &rec)) {
        return 0; /* unknown/already-removed/stale -- closes Gap 1 together with the generation check
                     below, and is also this function's OWN race-safety guarantee (see doc above) */
    }
    if (generation != pcwld_session_generation()) {
        return 0; /* stale cross-session claim -- entity_id values are only unique WITHIN one session
                     (pcwld_session_generation()'s own doc, pc_wildlife_authority.h) */
    }
    /* World Ecology Wildlife Sync T4: ordinary bug catching now shares this exact core with fish --
     * see pc_net_game_request_catch_bug()'s own top-of-section doc. Ants (kind == BUG, species == ANT)
     * are deliberately excluded: they are never materialized as a real local actor at all (pcwld_
     * presentation_create()'s own doc) and have no ordinary net-catch path -- a T5-scoped special case,
     * out of scope here. */
    if (rec.kind == PC_WILDLIFE_KIND_FISH) {
        if (!pcwld_fish_species_matches_claim(rec.species, claimed_species)) {
            return 0;
        }
    } else if (rec.kind == PC_WILDLIFE_KIND_BUG) {
        if (rec.species == aINS_INSECT_TYPE_ANT) {
            return 0;
        }
        if (!pcwld_bug_species_matches_claim(rec.species, claimed_species)) {
            return 0;
        }
    } else {
        return 0; /* unknown kind -- defense in depth, should never happen (PC_WILDLIFE_KIND_NUM-bounded) */
    }

    if (!is_host_local) {
        float px = 0.0f, py = 0.0f, pz = 0.0f;
        int reach_ok;

        if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
            return 0;
        }
        if (!s_host_peer[peer].ctx_valid || !(s_host_peer[peer].ctx.flags & PC_NETGAME_CTX_FLAG_IN_TOWN)) {
            return 0;
        }
        if (!pc_remote_player_get_last_position((PCNetPlayerId)peer, &px, &py, &pz)) {
            return 0;
        }
        reach_ok = (rec.kind == PC_WILDLIFE_KIND_FISH)
                       ? pcnetgame_fish_catch_reach_check(rec.pos_x, rec.pos_z, px, pz)
                       : pcnetgame_bug_catch_reach_check(rec.pos_x, rec.pos_z, px, pz);
        if (!reach_ok) {
            return 0;
        }
    }

    /* ACCEPT: remove from the authoritative table BEFORE anything else can observe/claim it again --
       see this function's own race-safety doc above for why this must happen here, synchronously,
       rather than being deferred to the caller. */
    pcwld_remove_by_id(entity_id);
    return 1;
}

/* Host side: client -> host CATCH_REQUEST. NOT a FIELD_ACTION kind (no tile is involved) -- its own
 * dedicated request/result pair with its own per-peer dedup record (s_host_catch_dedup), mirroring
 * pcnetgame_handle_host_snowman_build_request()'s own shape exactly (single round trip, no
 * INTERACT_CONFIRM phase). On accept, broadcasts WILDLIFE_DESPAWN and reconciles the host's own local
 * bookkeeping (pcnetgame_commit_catch_despawn()) BEFORE sending CATCH_RESULT to the requester -- so a
 * client that immediately re-renders after receiving its own accept never races its own despawn
 * broadcast (the broadcast + host-local reconciliation always go out first). */
static void pcnetgame_handle_host_catch_request(PCNetPeerId peer, const PCNetGameCatchRequestMsg* in) {
    PCNetGameCatchDedup* dedup;
    PCNetGameCatchResultMsg out;
    int accepted;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }

    dedup = &s_host_catch_dedup[peer];
    if (dedup->valid && dedup->request_id == in->request_id) {
        memset(&out, 0, sizeof(out));
        out.msg_type = (uint8_t)PC_NETGAME_MSG_CATCH_RESULT;
        out.accepted = dedup->accepted;
        out.entity_id = in->entity_id;
        out.request_id = in->request_id;
        pcnetgame_host_send_result(peer, &out, (uint16_t)sizeof(out), "CATCH_RESULT (replay)");
        return;
    }

    accepted = pcnetgame_validate_and_commit_catch(0, peer, in->entity_id, in->generation,
                                                   (int)in->claimed_species);

    dedup->valid = 1;
    dedup->request_id = in->request_id;
    dedup->accepted = (uint8_t)accepted;
    dedup->entity_id = in->entity_id;

    if (accepted) {
        printf("[NET][WILDLIFE] host: peer %d CATCH entity %u (species claim %d) accepted -- removed "
               "from authoritative table\n",
               (int)peer, (unsigned)in->entity_id, (int)in->claimed_species);
        pcnetgame_commit_catch_despawn(in->entity_id);
    } else if (g_pc_verbose) {
        printf("[NET][WILDLIFE] host: peer %d CATCH entity %u (species claim %d) rejected\n", (int)peer,
               (unsigned)in->entity_id, (int)in->claimed_species);
    }

    memset(&out, 0, sizeof(out));
    out.msg_type = (uint8_t)PC_NETGAME_MSG_CATCH_RESULT;
    out.accepted = (uint8_t)accepted;
    out.entity_id = in->entity_id;
    out.request_id = in->request_id;
    pcnetgame_host_send_result(peer, &out, (uint16_t)sizeof(out), "CATCH_RESULT");
}

/* Bug 4 fix: pc_net_game_is_money_bag_pickup_allowed() and its per-tile drop bookkeeping are gone --
 * a money-rock-dropped bag is now an entirely ordinary field item (see PCNetGameMoneyRockState's own
 * doc above and pcnetgame_validate_and_resolve_pickup()), so no special allow-list is needed to pick
 * one up any more. */

/* World Ecology T3: bury outcome kinds -- exact duplicate of bIT_common_bury_after()'s own
 * bIT_BURY_ACTION_* enum (bg_item_common.c_inc, `static` in a different translation unit) -- see
 * pcnetgame_resolve_bury_outcome()'s own doc for why this file duplicates the table instead, mirroring
 * T1's already-proven pattern for MONEY_ROCK_HIT's/TREE_SHAKE/CHOP's own vanilla-table duplicates. */
enum {
    PC_NETGAME_BURY_ACTION_BURY,
    PC_NETGAME_BURY_ACTION_PLANT,
    PC_NETGAME_BURY_ACTION_PITFALL
};

/* World Ecology T3: resolves ONE bury commit's outcome. EXACT duplicate of bIT_common_bury_after()'s
 * own table (bg_item_common.c_inc), checked in the SAME order, using `item` (the requester's own
 * claimed/stored item -- already vetted by pcnetgame_is_buryable_item() at reserve time) and
 * `hole_tile` (the HOST's own freshly re-read tile value -- HOLE00..24 or HOLE_SHINE, never the
 * client's claim) as bIT_common_bury_after()'s own fg_bury_item/fg_hole_item parameters. `ctx` is the
 * REQUESTER's own stored PCNetPlayerContext (never the host's local one for a remote peer) for the
 * money-tree roll; `claimed_hole_variant` is consulted ONLY for the PITFALL-into-HOLE_SHINE sub-case
 * (see PCNetGameBuryRequestMsg's own doc) -- every other outcome derives everything from item/hole_tile
 * alone, exactly like vanilla. Returns one PC_NETGAME_BURY_ACTION_* and fills *out_tile_value (the
 * tile's new persistent value); the caller decides the deposit bit purely from the returned action
 * (BURY -> ON, PLANT/PITFALL -> OFF), matching bIT_common_hole_throw()'s own mode!=1 (the "resolved
 * outcome", not the "restore the raw item" mode 1 branch a DIFFERENT vanilla call site uses -- see that
 * function's own doc; every bury this protocol handles is the mode!=1 case). This function is READ-ONLY
 * except for its own RNG draw (fqrand(), for the money-tree case) -- it never touches Save_t/the field
 * itself; the caller writes the result. */
static int pcnetgame_resolve_bury_outcome(const PCNetPlayerContext* ctx, mActor_name_t item, uint16_t hole_tile,
                                          uint8_t claimed_hole_variant, mActor_name_t* out_tile_value) {
    int action = PC_NETGAME_BURY_ACTION_BURY;
    *out_tile_value = item;

    if (item == ITM_FOOD_APPLE || item == ITM_FOOD_CHERRY || item == ITM_FOOD_PEAR || item == ITM_FOOD_PEACH ||
        item == ITM_FOOD_ORANGE || item == ITM_FOOD_COCONUT) {
        static const struct {
            mActor_name_t fruit;
            mActor_name_t tree;
        } fr2tr[] = {
            { ITM_FOOD_APPLE, TREE_APPLE_SAPLING },   { ITM_FOOD_CHERRY, TREE_CHERRY_SAPLING },
            { ITM_FOOD_PEAR, TREE_PEAR_SAPLING },     { ITM_FOOD_PEACH, TREE_PEACH_SAPLING },
            { ITM_FOOD_ORANGE, TREE_ORANGE_SAPLING }, { ITM_FOOD_COCONUT, TREE_PALM_SAPLING },
        };
        size_t i;
        *out_tile_value = (mActor_name_t)EMPTY_NO;
        for (i = 0; i < sizeof(fr2tr) / sizeof(fr2tr[0]); i++) {
            if (item == fr2tr[i].fruit) {
                *out_tile_value = fr2tr[i].tree;
                return PC_NETGAME_BURY_ACTION_PLANT;
            }
        }
    } else if (item == (mActor_name_t)ITM_MONEY_1000 || item == (mActor_name_t)ITM_MONEY_10000 ||
               item == (mActor_name_t)ITM_MONEY_30000 || item == (mActor_name_t)ITM_MONEY_100) {
        if (hole_tile == (uint16_t)HOLE_SHINE) {
            /* EXACT duplicate of bIT_common_moneytree_check() (bg_item_common.c_inc: rnd =
               RANDOM_F(100.0f) i.e. fqrand()*100.0f; rnd <= 50 + money_power*0.5 || destiny ==
               MONEY_LUCK) -- the RNG draw happens UNCONDITIONALLY first, then the destiny
               short-circuit is checked, exactly vanilla's own order, using the REQUESTER's own ctx. */
            float rnd = fqrand() * 100.0f;
            int win = (rnd <= (50.0f + (float)(int)ctx->money_power * 0.5f)) ||
                      ((int)ctx->destiny_type == mPr_DESTINY_MONEY_LUCK);
            if (win) {
                static const struct {
                    mActor_name_t money;
                    mActor_name_t tree;
                } fr2tr[] = {
                    { ITM_MONEY_1000, TREE_1000BELLS_SAPLING },
                    { ITM_MONEY_10000, TREE_10000BELLS_SAPLING },
                    { ITM_MONEY_30000, TREE_30000BELLS_SAPLING },
                    { ITM_MONEY_100, TREE_100BELLS_SAPLING },
                };
                size_t i;
                *out_tile_value = TREE_SAPLING;
                for (i = 0; i < sizeof(fr2tr) / sizeof(fr2tr[0]); i++) {
                    if (item == fr2tr[i].money) {
                        *out_tile_value = fr2tr[i].tree;
                        break;
                    }
                }
            } else {
                *out_tile_value = TREE_SAPLING;
            }
            action = PC_NETGAME_BURY_ACTION_PLANT;
        }
        /* else: not a shine hole -- falls through untouched: action stays BURY, *out_tile_value stays
           `item` (the money bag itself) -- byte-for-byte what bIT_common_bury_after() does too (its own
           money branch only ever touches buried_item_p/res inside the `fg_hole_item == HOLE_SHINE`
           check). */
    } else if (item >= (mActor_name_t)ITM_WHITE_PANSY_BAG && item <= (mActor_name_t)ITM_YELLOW_TULIP_BAG) {
        *out_tile_value = (mActor_name_t)(FLOWER_PANSIES0 + (item - ITM_WHITE_PANSY_BAG));
        action = PC_NETGAME_BURY_ACTION_PLANT;
    } else if (item == (mActor_name_t)ITM_SAPLING) {
        *out_tile_value = TREE_SAPLING;
        action = PC_NETGAME_BURY_ACTION_PLANT;
    } else if (item == (mActor_name_t)ITM_CEDAR_SAPLING) {
        *out_tile_value = CEDAR_TREE_SAPLING;
        action = PC_NETGAME_BURY_ACTION_PLANT;
    } else if (item == (mActor_name_t)ITM_PITFALL) {
        if (ITEM_IS_HOLE(hole_tile)) {
            /* The host derives the hole shape itself from its own re-read tile -- see
               PCNetGameBuryRequestMsg's own doc: this HOLE00..24 sub-case does NOT consult
               claimed_hole_variant at all, unlike the HOLE_SHINE sub-case below. */
            *out_tile_value = (mActor_name_t)(BURIED_PITFALL_HOLE_START + (hole_tile - (uint16_t)HOLE_START));
        } else {
            /* HOLE_SHINE: the host has no collision data to derive a shape from -- the client's own
               locally-computed hole_variant IS authoritative here (the ONE genuinely-trusted case --
               see PCNetGameBuryRequestMsg's own doc). 0xFF is mCoBG_GetHoleNumber()'s own -1 sentinel. */
            if (claimed_hole_variant <= 24u) {
                *out_tile_value = (mActor_name_t)(BURIED_PITFALL_HOLE_START + claimed_hole_variant);
            } else {
                *out_tile_value = (mActor_name_t)EMPTY_NO;
            }
        }
        action = PC_NETGAME_BURY_ACTION_PITFALL;
    } else if (item == (mActor_name_t)ITM_SHOVEL) {
        if (hole_tile == (uint16_t)HOLE_SHINE) {
            *out_tile_value = GOLD_TREE_SAPLING;
            action = PC_NETGAME_BURY_ACTION_PLANT;
        }
        /* else: falls through untouched -- action stays BURY, *out_tile_value stays ITM_SHOVEL itself,
           matching vanilla (bIT_common_bury_after()'s own ITM_SHOVEL branch only acts inside the
           HOLE_SHINE check too). */
    }
    /* else: any other item pcnetgame_is_buryable_item() already classified as buryable -- action stays
       BURY, *out_tile_value stays `item` itself, matching vanilla's own default case. */

    return action;
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

    if (in->kind == (uint8_t)PC_NETGAME_INTERACT_KIND_BURY) {
        /* World Ecology T3: BURY commit -- deliberately NOT the generic pickup/drop logic below (that
         * logic assumes DROP semantics: it requires an empty destination and never sets the deposit
         * bit, confirmed unsuitable for bury by direct trace of pcnetgame_handle_host_confirm()'s own
         * pre-T3 body -- see this milestone's design brief). Re-validates the tile is STILL exactly the
         * raw hole value observed at reservation time (nothing else mutated it in between) and deposit
         * is still 0, resolves the outcome via pcnetgame_resolve_bury_outcome() (an exact duplicate of
         * bIT_common_bury_after()'s own table, checked in the same order), writes the tile then the
         * deposit bit (ON only for a BURY-action outcome), and replies. Any failure aborts with NO
         * partial mutation and the same reconciliation echo the reserve-phase reject already uses. */
        uint16_t bury_cur = 0;
        int bury_dep = 0;
        const char* bury_fail = NULL;

        if (!s_host_world_ready) {
            bury_fail = "host world not ready";
        } else if (!pcfa_get_tile(rec->acre, rec->tile, &bury_cur)) {
            bury_fail = "tile unreadable";
        } else {
            bury_dep = pcfa_get_deposit(rec->acre, rec->tile);
            if (bury_dep != 0) {
                bury_fail = "tile deposit bit set";
            } else if (bury_cur != rec->raw_item) {
                bury_fail = "tile no longer holds the reserved hole value";
            }
        }
        if (bury_fail != NULL) {
            printf("[NET][BURY] host: peer %d request %u COMMIT FAILED (%s): tile (%d,%d) found 0x%04X dep %d "
                   "expected 0x%04X -- no mutation, reservation released\n",
                   (int)peer, (unsigned)rec->request_id, bury_fail, (int)rec->ut_x, (int)rec->ut_z,
                   (unsigned)bury_cur, bury_dep, (unsigned)rec->raw_item);
            rec->phase = (uint8_t)PC_NETGAME_PHASE_ABORTED;
            pcnetgame_host_send_bury_reject(peer, rec->request_id, rec->ut_x, rec->ut_z);
            return;
        }
        {
            mActor_name_t resolved = (mActor_name_t)EMPTY_NO;
            int action = pcnetgame_resolve_bury_outcome(&s_host_peer[peer].ctx, (mActor_name_t)rec->item, bury_cur,
                                                        rec->hole_variant, &resolved);
            int dep_on = (action == PC_NETGAME_BURY_ACTION_BURY);

            if (!pcfa_set_tile(rec->acre, rec->tile, (uint16_t)resolved)) {
                printf("[NET][BURY] host: peer %d request %u COMMIT FAILED (pcfa_set_tile refused): tile (%d,%d) "
                       "-- no mutation, reservation released\n",
                       (int)peer, (unsigned)rec->request_id, (int)rec->ut_x, (int)rec->ut_z);
                rec->phase = (uint8_t)PC_NETGAME_PHASE_ABORTED;
                pcnetgame_host_send_bury_reject(peer, rec->request_id, rec->ut_x, rec->ut_z);
                return;
            }
            if (dep_on) {
                pcfa_set_deposit(rec->acre, rec->tile, 1);
            }
            rec->phase = (uint8_t)PC_NETGAME_PHASE_DONE;
            printf("[NET][BURY] host: peer %d request %u committed tile (%d,%d) 0x%04X -> 0x%04X (action=%d dep=%d)\n",
                   (int)peer, (unsigned)rec->request_id, (int)rec->ut_x, (int)rec->ut_z, (unsigned)bury_cur,
                   (unsigned)resolved, action, dep_on);
            pcnetgame_host_flush_mask((uint32_t)1u << rec->acre); /* shadow + world_seq + FIELD_UPDATE to every READY peer */
            pcnetgame_host_send_bury_result(peer, rec->request_id, rec->ut_x, rec->ut_z, 1, rec->item, 0,
                                            (uint8_t)PC_NETGAME_BURY_REASON_NONE);
        }
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
    if (is_pickup) {
        /* M9-C Phase 5: presentation-only hint, after the authoritative commit above (never for ABORT/failed COMMIT, which
         * returned earlier). rec->raw_item = the ground item the pickup really took. */
        pcnetgame_host_emit_player_action((int)peer, (uint8_t)PC_NETGAME_PLAYER_ACTION_KIND_PICKUP, (int)rec->ut_x,
                                          (int)rec->ut_z, rec->raw_item);
    }
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
    if (s_bury_pending.valid && (!need_stamp_mismatch || !pcnetgame_owner_stamp_matches(&s_bury_pending.owner))) {
        printf("[NET][BURY] request %u cancelled: %s\n", (unsigned)s_bury_pending.request_id, why);
        pcnetgame_client_send_confirm((uint8_t)PC_NETGAME_INTERACT_KIND_BURY, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STATE_CHANGED, s_bury_pending.request_id);
        s_bury_pending.valid = 0;
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
 * Bug 3 fix: BOTH possible destinations for the granted item -- the wallet (money bag) and a pocket
 * slot -- are checked BEFORE the COMMIT is ever sent, exactly like the plain pocket-only check already
 * did for an ordinary item. wallet_fits and pocket_free are computed here, read-only, from
 * Now_Private->inventory.wallet / mPr_GetPossessionItemIdx() -- nothing is mutated yet. Only if
 * NEITHER destination has room do we ABORT(POCKETS_FULL) and return without ever sending COMMIT: the
 * bag is left completely untouched on the field, nothing is granted, nothing is lost. Otherwise COMMIT
 * is sent, and then (synchronously, nothing else can run in between and mutate wallet/pocket state
 * before this) the item is granted: mPr_GivePossessionBells() if the wallet has room, otherwise
 * mPr_SetFreePossessionItem() -- and step 2's pre-check guarantees pocket_free is true whenever the
 * pocket branch runs, so that call cannot fail for this item. There is no remaining "item lost" path
 * for a money bag: either the wallet takes it, or a pocket slot (already proven free) does. */
static void pcnetgame_handle_client_pickup_result(const PCNetGamePickupResultMsg* in) {
    const uint8_t kind = (uint8_t)PC_NETGAME_INTERACT_KIND_PICKUP;
    int matches = s_pickup_pending.valid && s_pickup_pending.request_id == in->request_id;
    int is_money_bag;
    u32 bell_amount;
    int wallet_fits;
    int pocket_free;

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
    /* Owner stamp matched, so Now_Private is non-NULL and the save is loaded.
     * Bug 3 fix: compute both possible destinations, read-only, before anything is sent or mutated.
     * is_money_bag/bell_amount detect a wallet-creditable item exactly as before
     * (mPr_GetAmountForMoneyItem() > 0 -- covers a money-rock-dropped bag, which is now an ordinary
     * field item per Bug 4, same as any other money bag this path could ever legitimately see). */
    is_money_bag = 0;
    bell_amount = 0;
#ifdef PC_ENHANCEMENTS
    bell_amount = mPr_GetAmountForMoneyItem((mActor_name_t)in->granted_item);
    is_money_bag = bell_amount > 0;
#endif

    wallet_fits = 0;
#ifdef PC_ENHANCEMENTS
    wallet_fits = is_money_bag && Now_Private->inventory.wallet <= mPr_WALLET_MAX &&
                  bell_amount <= (mPr_WALLET_MAX - Now_Private->inventory.wallet);
#endif
    pocket_free = mPr_GetPossessionItemIdx(Now_Private, (mActor_name_t)EMPTY_NO) >= 0;

    if (!wallet_fits && !pocket_free) {
        printf("[NET][PICKUP] request %u accepted (item=%u) but neither the wallet nor local pockets have room -- pickup "
               "aborted, the item stays in the world\n",
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

    /* Bug 3 fix: one of these two is now GUARANTEED to succeed -- wallet_fits was proven against the
     * current wallet value above, and whenever wallet_fits is false, step 2's precondition
     * (!wallet_fits && !pocket_free -> abort, above) guarantees pocket_free is true. Nothing runs
     * between that check and this application (one synchronous handler call), so neither fact can go
     * stale. There is no remaining failure/"item lost" path for this item. */
#ifdef PC_ENHANCEMENTS
    if (wallet_fits) {
        mPr_GivePossessionBells(bell_amount);
        printf("[NET][PICKUP] request %u accepted (item=%u) -- credited %u bells straight to the wallet, CONFIRM(COMMIT) sent\n",
               (unsigned)in->request_id, (unsigned)in->granted_item, (unsigned)bell_amount);
        return;
    }
#endif

    /* Stage 5A: the one place this file grants an ordinary item -- via the real, unmodified
     * mPr_SetFreePossessionItem(), exactly as Player_actor_setup_main_Pickup() would have called it
     * locally in single-player (see the Stage 5A inventory-architecture audit, Parts 4/6). No slot
     * number is sent by the host (see PCNetGamePickupResultMsg's doc) -- this client's own real pocket
     * contents are the only correct basis for choosing one.
     * Bug 3 fix: this call is now GUARANTEED to succeed whenever it runs -- either this item was never
     * a money bag (pocket_free was verified above, unchanged since), or it was a money bag whose
     * wallet_fits was false, which (per the pre-check above) means pocket_free was proven true before
     * COMMIT was ever sent. There is no remaining "item lost" outcome to log for this item. */
    mPr_SetFreePossessionItem(Now_Private, (mActor_name_t)in->granted_item, mPr_ITEM_COND_NORMAL);
    printf("[NET][PICKUP] request %u accepted (item=%u) -- granted to a free pocket slot, CONFIRM(COMMIT) sent\n",
           (unsigned)in->request_id, (unsigned)in->granted_item);
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
    /* M9-D G4-2: take the deferred-exchange record for THIS request id (if any) and consume it right
     * away: whatever happens below it can be applied at most once, only on the success path at the end. */
    PCNetGameExchangeDeferred deferred;
    int have_deferred = s_exchange_deferred.valid && s_exchange_deferred.request_id == in->request_id;

    memset(&deferred, 0, sizeof(deferred));
    if (have_deferred) {
        deferred = s_exchange_deferred;
        s_exchange_deferred.valid = 0;
    }

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
    /* M9-D G4-2: exchange-menu drop -- the item that was swapped into this slot now takes its place. The
     * claim check above guarantees the slot held exactly the dropped item, so the slot is empty here. */
    if (have_deferred && (int)deferred.slot == slot && pcnetgame_owner_stamp_matches(&deferred.owner) &&
        Now_Private->inventory.pockets[slot] == (mActor_name_t)EMPTY_NO) {
        mPr_SetPossessionItem(Now_Private, slot, (mActor_name_t)deferred.replacement, (int)deferred.replacement_cond);
        printf("[NET][DROP] request %u (exchange): replacement item 0x%04X written to pocket slot %d\n",
               (unsigned)in->request_id, (unsigned)deferred.replacement, slot);
    }
}

/* Forward-declared here too (see the canonical forward-declare comment further down, above
   pcnetgame_handle_client_field_action_result()) -- a duplicate prototype is harmless in C and lets
   this function use it before its own definition. */
static void pcnetgame_client_apply_tile(int acre, int tile, uint16_t value, int dep_valid, int dep_on);

/* World Ecology T3 (bugfix): client side of the host's answer to our own pending bury request -- see
 * pc_net_game_request_bury()'s doc for the overall flow. Structural mirror of
 * pcnetgame_handle_client_drop_result() -- pocket-clear timing was deliberately moved HERE (to
 * provisional-accept-confirmed time, gated on a slot-still-holds-the-claimed-item re-check) instead of
 * happening synchronously at menu-action time in the m_tag_ovl.c bury seam. Naively matching vanilla's
 * own immediate-clear timing would have been misleading to call "vanilla-matching": vanilla can never
 * fail once its menu action closes, so its immediate clear is truly a point of no return, but the
 * NETWORKED path can genuinely fail for reasons that have nothing to do with vanilla's own risk shape --
 * a lost network race, the pending request's own retry timeout expiring, an owner-stamp mismatch, or an
 * outright host reject -- and clearing the pocket before any of those are ruled out would destroy the
 * item with zero chance of it ever landing. So:
 *   accepted == 1   the caller's pocket slot is STILL INTACT at this point (the m_tag_ovl.c seam left it
 *                   untouched). Re-verify the tile/item echo, the owner stamp, and that the slot still
 *                   holds exactly the claimed item (mirrors pcnetgame_handle_client_drop_result()'s own
 *                   checks); only once INTERACT_CONFIRM(COMMIT) is actually queued do we clear the
 *                   pocket slot here. The resolved outcome (sapling/flower/pitfall/plain-bury tile,
 *                   deposit bit) arrives to EVERY client, including this one, through the ordinary
 *                   ambient FIELD_UPDATE the host's commit triggers -- this function's clear is purely
 *                   the inventory side, same division of labor as drop's own handler.
 *   accepted == 0   the item is still in this client's pockets (never cleared before this point) --
 *                   there is nothing to restore. What CAN and MUST still be corrected is the shared
 *                   WORLD tile: buried_item/flags carry the host's CURRENT tile value + deposit state
 *                   (see PCNetGameBuryResultMsg's own doc), applied via the same
 *                   pcnetgame_client_apply_tile() primitive FIELD_UPDATE uses, so this client's local
 *                   view of (ut_x, ut_z) converges immediately instead of waiting for the next ambient
 *                   update. */
static void pcnetgame_handle_client_bury_result(const PCNetGameBuryResultMsg* in) {
    const uint8_t kind = (uint8_t)PC_NETGAME_INTERACT_KIND_BURY;
    int matches = s_bury_pending.valid && s_bury_pending.request_id == in->request_id;
    int slot;

    if (!in->accepted) {
        if ((in->flags & (uint8_t)PC_NETGAME_BURY_FLAG_RECONCILE_VALID) != 0) {
            int acre, tile;
            if (pcfa_town_ut_to_acre_tile((int)in->ut_x, (int)in->ut_z, &acre, &tile)) {
                pcnetgame_client_apply_tile(acre, tile, in->buried_item, 1,
                                            (in->flags & (uint8_t)PC_NETGAME_BURY_FLAG_DEPOSIT_ON) != 0);
            }
        }
        if (!matches) {
            /* M9-D G2-3: a reject for a request whose provisional accept we already acted on (COMMIT sent,
             * pocket cleared) = the host's commit FAILED, nothing was buried: give the item back. */
            if (s_bury_committed.valid && s_bury_committed.request_id == in->request_id) {
                const uint16_t item = s_bury_committed.item;
                int rslot = (int)s_bury_committed.pocket_slot_idx;
                const int stamp_ok = pcnetgame_owner_stamp_matches(&s_bury_committed.owner);
                s_bury_committed.valid = 0; /* one restore per request, never twice */
                if (!stamp_ok) {
                    printf("[NET][BURY] request %u commit rejected by host but the local player/save changed -- "
                           "item 0x%04X not restored\n",
                           (unsigned)in->request_id, (unsigned)item);
                } else if (rslot >= 0 && rslot < mPr_POCKETS_SLOT_COUNT &&
                           Now_Private->inventory.pockets[rslot] == (mActor_name_t)EMPTY_NO) {
                    mPr_SetPossessionItem(Now_Private, rslot, (mActor_name_t)item, mPr_ITEM_COND_NORMAL);
                    printf("[NET][BURY] request %u commit rejected by host -- RESTORED item 0x%04X to pocket "
                           "slot %d\n",
                           (unsigned)in->request_id, (unsigned)item, rslot);
                } else if (mPr_SetFreePossessionItem(Now_Private, (mActor_name_t)item, mPr_ITEM_COND_NORMAL)) {
                    printf("[NET][BURY] request %u commit rejected by host -- RESTORED item 0x%04X to a free "
                           "pocket slot (slot %d was taken)\n",
                           (unsigned)in->request_id, (unsigned)item, rslot);
                } else {
                    printf("[NET][BURY] request %u commit rejected by host -- item 0x%04X LOST (pockets full)\n",
                           (unsigned)in->request_id, (unsigned)item);
                }
            }
            return; /* not our current pending request -- already resolved, given up, or a stale duplicate */
        }
        s_bury_pending.valid = 0; /* resolved -- never retried or re-applied again */
        printf("[NET][BURY] request %u rejected by host (tile %d,%d) -- item was never cleared from pockets, "
               "nothing to restore; world tile reconciled\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z);
        return;
    }

    if (!matches) {
        printf("[NET][BURY] provisional accept for request %u has no matching pending request -- sending ABORT(STALE)\n",
               (unsigned)in->request_id);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STALE, in->request_id);
        return;
    }

    s_bury_pending.valid = 0; /* the RESULT arrived: no retry may follow, whatever happens next */
    slot = (int)s_bury_pending.pocket_slot_idx;

    if (in->ut_x != s_bury_pending.ut_x || in->ut_z != s_bury_pending.ut_z ||
        in->buried_item != s_bury_pending.claimed_item || slot < 0 || slot >= mPr_POCKETS_SLOT_COUNT) {
        printf("[NET][BURY] request %u accepted but the RESULT disagrees with the request (tile %d,%d item=%u) -- "
               "aborting, nothing cleared\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)in->buried_item);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STATE_CHANGED, in->request_id);
        return;
    }
    if (!pcnetgame_owner_stamp_matches(&s_bury_pending.owner)) {
        printf("[NET][BURY] request %u accepted (item=%u) but the local player/save changed -- aborting, nothing "
               "cleared\n",
               (unsigned)in->request_id, (unsigned)in->buried_item);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_STATE_CHANGED, in->request_id);
        return;
    }
    /* Owner stamp matched, so Now_Private is non-NULL and the save is loaded. */
    if (Now_Private->inventory.pockets[slot] != (mActor_name_t)s_bury_pending.claimed_item) {
        printf("[NET][BURY] request %u accepted (item=%u) but pocket slot %d no longer holds it (now 0x%04X) -- "
               "aborting, nothing cleared\n",
               (unsigned)in->request_id, (unsigned)in->buried_item, slot,
               (unsigned)Now_Private->inventory.pockets[slot]);
        pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                      (uint8_t)PC_NETGAME_CONFIRM_REASON_SLOT_CHANGED, in->request_id);
        return;
    }

    if (!pcnetgame_client_send_confirm(kind, (uint8_t)PC_NETGAME_CONFIRM_COMMIT, (uint8_t)PC_NETGAME_CONFIRM_REASON_NONE,
                                       in->request_id)) {
        printf("[NET][BURY] request %u accepted (item=%u) but the CONFIRM could not be queued -- cancelled, "
               "nothing cleared (the host's reservation will expire)\n",
               (unsigned)in->request_id, (unsigned)in->buried_item);
        return;
    }

    /* The one place this file writes to Now_Private for a bury -- via the real, unmodified
       mPr_SetPossessionItem(), exactly as mTG_bury_proc()/mTG_plant_proc() would have called it locally
       in single-player, after the slot was verified above. The field tile itself is handled by the
       ordinary FIELD_UPDATE the host sends on COMMIT, not here. */
    mPr_SetPossessionItem(Now_Private, slot, (mActor_name_t)EMPTY_NO, mPr_ITEM_COND_NORMAL);
    /* M9-D G2-3: retain the claim so a later commit-failure reject can restore it (see s_bury_committed). */
    s_bury_committed.valid = 1;
    s_bury_committed.request_id = in->request_id;
    s_bury_committed.pocket_slot_idx = (uint8_t)slot;
    s_bury_committed.ut_x = in->ut_x;
    s_bury_committed.ut_z = in->ut_z;
    s_bury_committed.item = (uint16_t)in->buried_item;
    s_bury_committed.owner = s_bury_pending.owner;
    s_bury_committed.keep_accum = 0.0f;
    printf("[NET][BURY] request %u accepted (item=%u) -- CONFIRM(COMMIT) sent, cleared pocket slot %d; the "
           "resolved outcome arrives via the ordinary FIELD_UPDATE broadcast\n",
           (unsigned)in->request_id, (unsigned)in->buried_item, slot);
}

/* T0-C: sends (or resends) the QUEUE HEAD (index 0) -- the only entry ever placed on the wire. Returns
 * pc_net_send()'s result, exactly like the pre-queue code's own inline send did. */
static int pcnetgame_field_action_queue_send(const PCNetGameFieldActionPending* e) {
    PCNetGameFieldActionRequestMsg msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_FIELD_ACTION_REQUEST;
    msg.kind = e->kind;
    msg.ut_x = e->ut_x;
    msg.ut_z = e->ut_z;
    msg.request_id = e->request_id; /* SAME id on a retry -- the host's dedup cache recognizes it, see
                                        s_host_field_action_dedup */
    msg.hole_variant = e->hole_variant; /* protocol v3 -- 0 for every kind that doesn't use it */
    return pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
}

/* T0-C: (re)kicks the current head -- called whenever a NEW entry becomes the head (freshly enqueued
 * into an empty queue, or promoted after the previous head was popped). Mirrors the pre-queue code's
 * own initial-send failure handling exactly: a failed send (window full) is retried on the very next
 * timeout tick rather than waiting a whole interval. */
static void pcnetgame_field_action_queue_kick_head(void) {
    if (s_field_action_queue_len <= 0) {
        return;
    }
    if (!pcnetgame_field_action_queue_send(&s_field_action_queue[0])) {
        s_field_action_queue[0].unsent = 1;
        s_field_action_queue[0].timeout_accum = PC_NETGAME_FIELD_ACTION_TIMEOUT_60FPS_FRAMES;
    }
}

/* T0-C: removes the head entry (its RESULT arrived, or it gave up after MAX_RETRIES) and, if another
 * request is queued behind it, promotes and sends that one now -- preserving "only one request
 * outstanding on the wire at a time" even though several may now be queued client-side. */
static void pcnetgame_field_action_queue_pop_head(void) {
    int i;
    if (s_field_action_queue_len <= 0) {
        return;
    }
    for (i = 1; i < s_field_action_queue_len; i++) {
        s_field_action_queue[i - 1] = s_field_action_queue[i];
    }
    s_field_action_queue_len--;
    if (s_field_action_queue_len > 0) {
        s_field_action_queue[0].timeout_accum = 0.0f;
        s_field_action_queue[0].retry_count = 0;
        s_field_action_queue[0].unsent = 0;
        pcnetgame_field_action_queue_kick_head();
    }
}

/* World Ecology Stage 1: client side, the host's answer to our own pending FIELD_ACTION_REQUEST. No
 * provisional phase and no CONFIRM to send back (see PCNetGameFieldActionRequestMsg's own doc) -- the
 * host already committed everything on its own side before replying, so this function's only jobs
 * are (1) pop the queue head (T0-C: so the caller's every-frame recheck can send a new request, and so
 * the next QUEUED request, if any, is promoted and sent), and (2) for an accepted DIG_BURIED, grant the
 * item straight into a free pocket slot, exactly like pcnetgame_handle_client_pickup_result() already
 * does -- never re-deriving the item locally. MONEY_ROCK_HIT needs no client-side inventory step at
 * all: the dropped bag is picked up later through the ordinary pickup path.
 * T0-C: only the QUEUE HEAD can ever match an incoming RESULT -- identical in effect to the pre-queue
 * code's single s_field_action_pending, since that was likewise the only entry ever placed on the
 * wire. */
/* Forward-declared for the PITFALL_CONSUME reconciliation branch below -- defined later in this file
   alongside the rest of the client-side v2 world-apply machinery. */
static void pcnetgame_client_apply_tile(int acre, int tile, uint16_t value, int dep_valid, int dep_on);

static void pcnetgame_handle_client_field_action_result(const PCNetGameFieldActionResultMsg* in) {
    PCNetGameFieldActionPending saved;
    int matches;

    if (s_field_action_queue_len <= 0) {
        matches = 0;
    } else {
        const PCNetGameFieldActionPending* head = &s_field_action_queue[0];
        matches = head->valid && head->request_id == in->request_id && head->kind == in->kind;
    }

    if (!matches) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] client: RESULT for request %u (kind %u) has no matching pending request -- ignored\n",
                   (unsigned)in->request_id, (unsigned)in->kind);
        }
        return;
    }
    saved = s_field_action_queue[0]; /* copy out before popping -- pop_head() shifts the array */
    pcnetgame_field_action_queue_pop_head(); /* the RESULT arrived: no retry may follow, whatever happens
                                                 next; also promotes+sends the next queued request, if any */

    if (in->kind == (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_PITFALL_CONSUME) {
        /* World Ecology T-dig: PITFALL_CONSUME is handled BEFORE the generic accepted/reject branch
           below, since it needs to act on a REJECT too (unlike every other kind, where a reject means
           "nothing happened, nothing to do") -- see pc_net_game_request_pitfall_consume()'s own doc. The
           caller already played its local fall animation optimistically, before this RESULT ever
           arrived, so there is nothing to undo visually either way. */
        if (in->accepted) {
            printf("[NET][FIELD_ACTION] client: request %u PITFALL_CONSUME accepted at tile (%d,%d)\n",
                   (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z);
        } else {
            /* Reconcile: granted_item carries the host's OWN CURRENT tile value (see
               pcnetgame_fa_validate_adapter_pitfall_consume()'s own doc) -- applied directly through the
               same client-side apply primitive FIELD_UPDATE uses, rather than forcing a full resync for
               one already-known stale tile. Deposit is always OFF for every legitimate post-state here
               (a fresh EMPTY_NO after a racing peer's consume, or a HOLE_START+variant after a racing
               DIG_BURIED dig-up -- both deposit-OFF outcomes). */
            int acre, tile;
            if (pcfa_town_ut_to_acre_tile((int)in->ut_x, (int)in->ut_z, &acre, &tile)) {
                pcnetgame_client_apply_tile(acre, tile, in->granted_item, 1, 0);
            }
            printf("[NET][FIELD_ACTION] client: request %u PITFALL_CONSUME rejected at tile (%d,%d) -- "
                   "reconciled local tile to host's own value 0x%04X\n",
                   (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)in->granted_item);
        }
        return;
    }

    if (!in->accepted) {
        printf("[NET][FIELD_ACTION] client: request %u (kind %u) rejected by host (tile %d,%d)\n",
               (unsigned)in->request_id, (unsigned)in->kind, (int)in->ut_x, (int)in->ut_z);
        return;
    }

    if (in->kind == (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_HOLE) {
        /* World Ecology T-dig: the tile (HOLE_START + this client's own requested hole_variant) already
           updated, or will moments later, via the ordinary FIELD_UPDATE broadcast the host's own commit
           triggers. Nothing is granted host-side (see this kind's own doc) -- EXCEPT a private, locally-
           rolled golden-shovel ITM_MONEY_100 bonus (A-2), carried in `saved.local_grant` by
           pc_net_game_request_dig_hole_with_grant() and applied here, on ACCEPT only, exactly like
           DIG_BURIED's own grant below. granted_item here is still the PRE-commit tile value (EMPTY_NO or
           the removed plant's id), carried purely as optional polish. */
        printf("[NET][FIELD_ACTION] client: request %u DIG_HOLE accepted at tile (%d,%d) (pre-dig tile was "
               "0x%04X)\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)in->granted_item);
        if (saved.local_grant != 0) {
            if (in->ut_x != saved.ut_x || in->ut_z != saved.ut_z) {
                printf("[NET][FIELD_ACTION] client: request %u DIG_HOLE golden-shovel bonus (item=%u) skipped -- "
                       "RESULT tile disagrees with the request\n",
                       (unsigned)in->request_id, (unsigned)saved.local_grant);
            } else if (!pcnetgame_owner_stamp_matches(&saved.owner)) {
                printf("[NET][FIELD_ACTION] client: request %u DIG_HOLE golden-shovel bonus (item=%u) skipped -- "
                       "local player/save changed\n",
                       (unsigned)in->request_id, (unsigned)saved.local_grant);
            } else if (mPr_SetFreePossessionItem(Now_Private, (mActor_name_t)saved.local_grant, mPr_ITEM_COND_NORMAL)) {
                printf("[NET][FIELD_ACTION] client: request %u DIG_HOLE golden-shovel bonus (item=%u) granted to a "
                       "free pocket slot\n",
                       (unsigned)in->request_id, (unsigned)saved.local_grant);
            } else {
                /* Accepted gap, same shape as DIG_BURIED's own: the pocket filled up between send and
                   this RESULT arriving. Documented, not solved with new mechanism (see this task's own
                   design brief). */
                printf("[NET][FIELD_ACTION] client: request %u DIG_HOLE golden-shovel bonus (item=%u) LOST -- free "
                       "slot vanished before it could be granted\n",
                       (unsigned)in->request_id, (unsigned)saved.local_grant);
            }
        }
        return;
    }
    if (in->kind == (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_FILL_HOLE) {
        printf("[NET][FIELD_ACTION] client: request %u FILL_HOLE accepted at tile (%d,%d)\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z);
        return;
    }
    if (in->kind == (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_SHINE) {
        /* World Ecology T-dig (D): the tile (now HOLE_SHINE) already updated, or will moments later, via
           the ordinary FIELD_UPDATE broadcast. Per this kind's own doc, the digging client rolls and
           grants its own bell amount locally -- carried in `saved.local_grant` by
           pc_net_game_request_dig_shine_with_grant() and applied here, on ACCEPT only, exactly mirroring
           DIG_HOLE's own golden-shovel bonus handling above. */
        printf("[NET][FIELD_ACTION] client: request %u DIG_SHINE accepted at tile (%d,%d)\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z);
        if (saved.local_grant != 0) {
            if (in->ut_x != saved.ut_x || in->ut_z != saved.ut_z) {
                printf("[NET][FIELD_ACTION] client: request %u DIG_SHINE bell grant (item=%u) skipped -- RESULT "
                       "tile disagrees with the request\n",
                       (unsigned)in->request_id, (unsigned)saved.local_grant);
            } else if (!pcnetgame_owner_stamp_matches(&saved.owner)) {
                printf("[NET][FIELD_ACTION] client: request %u DIG_SHINE bell grant (item=%u) skipped -- local "
                       "player/save changed\n",
                       (unsigned)in->request_id, (unsigned)saved.local_grant);
            } else if (mPr_SetFreePossessionItem(Now_Private, (mActor_name_t)saved.local_grant, mPr_ITEM_COND_NORMAL)) {
                printf("[NET][FIELD_ACTION] client: request %u DIG_SHINE bell grant (item=%u) granted to a free "
                       "pocket slot\n",
                       (unsigned)in->request_id, (unsigned)saved.local_grant);
            } else {
                printf("[NET][FIELD_ACTION] client: request %u DIG_SHINE bell grant (item=%u) LOST -- free slot "
                       "vanished before it could be granted\n",
                       (unsigned)in->request_id, (unsigned)saved.local_grant);
            }
        }
        return;
    }

    if (in->kind == (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_TREE_SHAKE) {
        /* World Ecology T1: nothing further to do here -- the field (dropped item(s) and/or the
           destination tree conversion) already updated, or will moments later, via the ordinary
           FIELD_UPDATE broadcast the host's own commit triggers. granted_item is the resulting
           tree-tile value, carried purely as optional polish (see PCNetGameFieldActionResultMsg's own
           doc) -- no client-side mutation depends on it. */
        printf("[NET][FIELD_ACTION] client: request %u TREE_SHAKE accepted at tile (%d,%d) -> tree=0x%04X\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)in->granted_item);
        return;
    }
    if (in->kind == (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_TREE_CHOP) {
        printf("[NET][FIELD_ACTION] client: request %u TREE_CHOP accepted at tile (%d,%d) -> tree=0x%04X\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)in->granted_item);
        return;
    }

    if (in->kind == (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_SNOWMAN_BREAK) {
        /* World Ecology: snowmen -- nothing to undo (the local two-half snowman actor is already gone;
           see pc_net_game_request_snowman_break()'s own doc). The tile update and the accompanying
           SNOWMAN_STATE (slot cleared) arrive moments later via their own ordinary broadcasts. */
        printf("[NET][FIELD_ACTION] client: request %u SNOWMAN_BREAK accepted at tile (%d,%d)\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z);
        return;
    }

    if (in->kind != (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_BURIED) {
        /* MONEY_ROCK_HIT (or any other kind not yet given its own branch above): nothing further to do
           here -- the field already updated (or will, moments later) via the ordinary FIELD_UPDATE
           broadcast. */
        printf("[NET][FIELD_ACTION] client: request %u MONEY_ROCK_HIT accepted at tile (%d,%d)\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z);
        return;
    }

    if (in->ut_x != saved.ut_x || in->ut_z != saved.ut_z || in->granted_item == (uint16_t)EMPTY_NO) {
        printf("[NET][FIELD_ACTION] client: request %u DIG_BURIED accepted but the RESULT disagrees with the request "
               "(tile %d,%d item=%u) -- no inventory change\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)in->granted_item);
        return;
    }
    if (!pcnetgame_owner_stamp_matches(&saved.owner)) {
        printf("[NET][FIELD_ACTION] client: request %u DIG_BURIED accepted (item=%u) but the local player/save changed "
               "-- no inventory change\n",
               (unsigned)in->request_id, (unsigned)in->granted_item);
        return;
    }
    /* Owner stamp matched, so Now_Private is non-NULL and the save is loaded. The free slot was
       already verified before the request was sent (see pc_net_game_request_dig_buried()); a race
       that fills it in the meantime is the same rare, logged-not-fabricated edge case
       pcnetgame_handle_client_pickup_result() already accepts. */
    if (mPr_SetFreePossessionItem(Now_Private, (mActor_name_t)in->granted_item, mPr_ITEM_COND_NORMAL)) {
        printf("[NET][FIELD_ACTION] client: request %u DIG_BURIED accepted (item=%u) -- granted to a free pocket slot\n",
               (unsigned)in->request_id, (unsigned)in->granted_item);
    } else {
        printf("[NET][FIELD_ACTION] client: request %u DIG_BURIED accepted (item=%u) -- INTERNAL ERROR: free slot "
               "vanished before it could be granted\n",
               (unsigned)in->request_id, (unsigned)in->granted_item);
    }
}

/* World Ecology: snowmen. Client side: the host's answer to our own SNOWMAN_BUILD_REQUEST. Purely
 * informational -- there is nothing to visually undo on a rejection (the local two-half snowman actor
 * is already gone; Actor_delete() already ran by the time this arrives -- see
 * pc_net_game_request_snowman_build()'s own doc for the accepted gap this implies). No pending-state
 * tracking needed (unlike DIG_BURIED/PICKUP/DROP, nothing here needs to be matched against a queue
 * entry to finish a client-side inventory step). */
static void pcnetgame_handle_client_snowman_build_result(const PCNetGameSnowmanBuildResultMsg* in) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return;
    }
    if (in->accepted) {
        printf("[NET][SNOWMAN] client: BUILD request %u accepted at tile (%d,%d) slot %d\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (int)in->slot);
    } else {
        printf("[NET][SNOWMAN] client: BUILD request %u rejected at tile (%d,%d) reason %u\n",
               (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)in->reason);
    }
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

/* World Ecology Stage 1, Item 4: mirrors the host's broadcast fish/bug term state into this client's
 * own Save_t.gyoei_term/insect_term (+ their transition offsets) -- see PCNetGameWorldStateWire's own
 * doc comment for why this is a straight mirror rather than a gate (aSOG_gyoei_renew_term_info()/
 * aSOI_ins_renew_term_info() are NOT gated -- gating them would cause spawn-table divergence with no
 * safety benefit). CRITICAL: gyoei_term/insect_term are used as UNCHECKED array indices deep in
 * ac_set_ovl_gyoei.c/ac_set_ovl_insect.c (r_month[term>>1][term&1], l_insect_month[month][...]) --
 * a corrupted or malicious wire value must never be written into Save_t verbatim, so every field here
 * is clamped to its own real valid range before being stored: gyoei_term to 0..23 (12 months x 2
 * half-month terms, see r_month's [lbRTC_MONTHS_MAX][aSOG_TERM_NUM] declaration), insect_term to
 * 0..11 (a month index into l_insect_month's first dimension), and both *_transition_offset bytes to
 * 0..5 (aSOG_TERM_TRANSITION_MAX_DAYS/aSOI_TERM_TRANSITION_MAX_DAYS, used only in date arithmetic --
 * never an index -- but bounded here too as cheap insurance). Not persisted to disk for the same
 * reason noted on pcnetgame_client_apply_weather_state() above. */
static void pcnetgame_client_apply_term_state(const PCNetGameWorldStateWire* w) {
    uint8_t gyoei_term = w->gyoei_term;
    uint8_t gyoei_offset = w->gyoei_term_transition_offset;
    uint8_t insect_term = w->insect_term;
    uint8_t insect_offset = w->insect_term_transition_offset;

    if (gyoei_term > 23u) {
        printf("[NET][WORLD] client: received out-of-range gyoei_term=%u -- clamped to 23\n", (unsigned)gyoei_term);
        gyoei_term = 23u;
    }
    if (gyoei_offset > 5u) {
        gyoei_offset = 5u;
    }
    if (insect_term > 11u) {
        printf("[NET][WORLD] client: received out-of-range insect_term=%u -- clamped to 11\n", (unsigned)insect_term);
        insect_term = 11u;
    }
    if (insect_offset > 5u) {
        insect_offset = 5u;
    }

    Save_Set(gyoei_term, gyoei_term);
    Save_Set(gyoei_term_transition_offset, gyoei_offset);
    Save_Set(insect_term, insect_term);
    Save_Set(insect_term_transition_offset, insect_offset);
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

/* World Ecology: snowmen. Client side: applies one PC_NETGAME_MSG_SNOWMAN_STATE broadcast. Own
 * strict `>=` staleness rule against s_client_snowman_seq (its own independent sequence -- see
 * PCNetGameSnowmanStateMsg's own doc), THEN independently re-validates every field against the exact
 * same rules sChk_snowman_save_check() (save_check_take.c_inc) already enforces for a loaded save --
 * a failure is dropped and logged, never applied, never a disconnect. On success: memcpy's the 12
 * snowmen_data bytes and the 4 date bytes into Save_t, then for every slot whose `exists` just went
 * 1 -> 0 (a build this client didn't see accepted, break, or melt happened host-side while this slot
 * still looked occupied here), mirrors the host's own BREAK commit locally: scans the loaded field for
 * a tile still holding that slot's SNOWMANx value and clears it (via pcnetgame_client_apply_tile(),
 * which already handles "not currently overwritable"/parking correctly), and deletes any live local
 * PSNOWMAN actor for that slot (clearing its npc_id to EMPTY_NO first) so its own restore-on-destroy
 * never resurrects the tile. */
static void pcnetgame_handle_client_snowman_state(const PCNetGameSnowmanStateMsg* in) {
    mSN_snowman_data_c prev[mSN_SAVE_COUNT];
    mSN_snowman_data_c* cur;
    int i;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return;
    }
    if (!pcnetgame_client_can_apply_world()) {
        return;
    }
    if (in->world_seq < s_client_snowman_seq) {
        if (g_pc_verbose) {
            printf("[NET][SNOWMAN] client: stale SNOWMAN_STATE seq %u < %u ignored\n", (unsigned)in->world_seq,
                   (unsigned)s_client_snowman_seq);
        }
        return;
    }

    /* Save-validity rules mirrored from sChk_snowman_save_check() (save_check_take.c_inc) -- dropped
       and logged, never applied. */
    if (in->year > (uint8_t)(GAME_YEAR_MAX - 1) || in->month > (uint8_t)lbRTC_DECEMBER || in->day > 31 ||
        in->hour >= 24) {
        printf("[NET][SNOWMAN] client: SNOWMAN_STATE (seq %u) failed date validity (y=%u m=%u d=%u h=%u) -- "
               "dropped\n",
               (unsigned)in->world_seq, (unsigned)in->year, (unsigned)in->month, (unsigned)in->day,
               (unsigned)in->hour);
        return;
    }
    for (i = 0; i < mSN_SAVE_COUNT; i++) {
        const uint8_t* d = &in->snowmen[i * 4];
        if (d[3] >= 4 || d[0] > 1) {
            printf("[NET][SNOWMAN] client: SNOWMAN_STATE (seq %u) slot %d failed validity (exists=%u score=%u) -- "
                   "dropped\n",
                   (unsigned)in->world_seq, i, (unsigned)d[0], (unsigned)d[3]);
            return;
        }
    }
    if (!pcfa_save_ready()) {
        return;
    }

    s_client_snowman_seq = in->world_seq;

    cur = Save_GetPointer(snowmen.snowmen_data[0]);
    for (i = 0; i < mSN_SAVE_COUNT; i++) {
        prev[i] = cur[i];
    }
    for (i = 0; i < mSN_SAVE_COUNT; i++) {
        const uint8_t* d = &in->snowmen[i * 4];
        cur[i].exists = d[0];
        cur[i].head_size = d[1];
        cur[i].body_size = d[2];
        cur[i].score = d[3];
    }
    Save_Set(snowman_year, in->year);
    Save_Set(snowman_month, in->month);
    Save_Set(snowman_day, in->day);
    Save_Set(snowman_hour, in->hour);

    for (i = 0; i < mSN_SAVE_COUNT; i++) {
        uint16_t lo, hi;
        int acre, tile;

        if (!prev[i].exists || cur[i].exists) {
            continue; /* only a 1 -> 0 transition needs any field/actor cleanup */
        }
        lo = (uint16_t)(SNOWMAN0 + i * mSN_SAVE_COUNT);
        hi = (uint16_t)(lo + mSN_SAVE_COUNT - 1);

        for (acre = 0; acre < PCFA_ACRE_NUM; acre++) {
            int found = 0;
            for (tile = 0; tile < PCFA_TILE_NUM; tile++) {
                uint16_t v;
                if (!pcfa_get_tile(acre, tile, &v) || v < lo || v > hi) {
                    continue;
                }
                pcnetgame_client_apply_tile(acre, tile, (uint16_t)EMPTY_NO, 1, 0);
                found = 1;
                break;
            }
            if (found) {
                break;
            }
        }

        if (gamePT != NULL && gamePT->exec == play_main && pcfa_scene_is_town()) {
            GAME_PLAY* play = (GAME_PLAY*)gamePT;
            ACTOR* actor;
            for (actor = play->actor_info.list[ACTOR_PART_BG].actor; actor != NULL; actor = actor->next_actor) {
                if (actor->restore_fg && actor->npc_id >= (mActor_name_t)lo && actor->npc_id <= (mActor_name_t)hi) {
                    actor->npc_id = (mActor_name_t)EMPTY_NO;
                    /* Safety: same ACTOR_STATE_IN_DEMO guard as the host's own BREAK commit
                     * (pcnetgame_fa_commit_adapter_snowman_break()) -- if this actor is this
                     * client's own live dialogue partner right now, deleting it here would leave
                     * m_demo.c holding a dangling pointer when the talk closes (use-after-free).
                     * npc_id is already cleared above; the actor's own per-frame NormalTalk check
                     * (aPSM_actor_move(), ac_psnowman.c) deletes it for us once the talk actually
                     * ends. */
                    if (!(actor->state_bitfield & ACTOR_STATE_IN_DEMO)) {
                        Actor_delete(actor);
                    }
                    break;
                }
            }
        }
    }

    if (g_pc_verbose) {
        printf("[NET][SNOWMAN] client: applied SNOWMAN_STATE (seq %u)\n", (unsigned)in->world_seq);
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
        if (in->flags & PC_NETGAME_META_FLAG_TERM_VALID) {
            pcnetgame_client_apply_term_state(&in->world_state);
        }
        s_client_meta_seq = in->world_seq;
    }
    /* World Ecology Stage 1, Item 4: field rank (Save.dust_flag, good_field) goes stale on a client --
     * it is derived purely from already-synced fg data (mFAs_GetFieldGoodBlockNum() reads Save_Get(fg)
     * directly, no scene dependency -- verified against m_field_assessment.c), so recomputing it here,
     * redundantly, after every full snapshot apply is harmless and keeps fish/bug spawn-weight
     * multipliers (which read good_field) correct on this client. */
    mFAs_SetFieldRank();
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
        /* World Ecology Stage 1, Item 4: RENEW_TIME_VALID signals a daily growth renewal (see this
         * flag's own doc above) -- the field rank derived from fg goes stale across one, so recompute
         * it on this client too (see the matching call in pcnetgame_handle_client_snapshot_end()). */
        mFAs_SetFieldRank();
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
    if (in->flags & PC_NETGAME_META_FLAG_TERM_VALID) {
        pcnetgame_client_apply_term_state(&in->world_state);
        printf("[NET][WORLD] client: host fish/bug term -> gyoei_term=%u insect_term=%u (world_seq %u)\n",
               (unsigned)in->world_state.gyoei_term, (unsigned)in->world_state.insect_term,
               (unsigned)in->world_seq);
    }
    s_client_meta_seq = in->world_seq;
}

/* N-clock milestone, client side. Rule 1 (apply only once READY AND the local world is latched -- see
 * pcnetgame_client_can_apply_world()) and the strict `>` monotonic clock_seq rule (matching
 * s_client_meta_seq's own contract shape) are both checked before anything else, exactly like every
 * other WORLD-message handler above.
 *
 * The offset itself: candidate_offset = host_game_ticks - (this process's own OSGetTime()+time_delta,
 * read fresh right here, NOT any previously cached value) -- a single subtraction, matching
 * lbRTC_GetGameTime()'s own single-addition use of it (lb_rtc.c). This automatically absorbs whatever
 * this client's own --date/--time CLI overrides and its own Save_t.time_delta contributed to its local
 * clock: both already fed into the OSGetTime()+time_delta term being subtracted here, so nothing about
 * them needs special-casing.
 *
 * Tolerance (PC_NETGAME_CLOCK_SYNC_TOLERANCE_SEC): a periodic re-sync that only confirms the offset is
 * still correct to within ~2 real seconds is deliberately NOT re-applied, so tiny drift/rounding
 * between periodic syncs can never cause a visible back-and-forth jitter in Common(time.now_sec). The
 * DISCONTINUITY flag (a detected host-side manual clock adjustment) always forces an immediate
 * re-apply regardless of this tolerance -- see PC_NETGAME_MSG_CLOCK_SYNC's own doc comment. The very
 * first sync this client ever applies (s_client_clock_seq_applied == 0, including right after a
 * reconnect -- see Rule 2 / pcnetgame_reset_client_session_state()) is likewise always applied
 * unconditionally, matching the design's "initial sync ... always applied unconditionally" rule.
 *
 * No separate backward-jump guard is needed here: CLOCK_SYNC is sent PC_NET_RELIABLE (see
 * PCNetGameClockSyncMsg's send sites above), and pc_net.h documents that reliable transport as
 * delivering every payload to a peer's game layer "exactly once and in send order" per peer/
 * direction -- combined with the strict `clock_seq >` staleness check just above, any packet that
 * reaches this point is by construction the host's genuinely newer state, never a stale/reordered/
 * duplicate one. A resulting backward correction is therefore always either legitimate oscillator-
 * drift correction or a legitimate host-side clock change (DISCONTINUITY), and both must be applied,
 * not blocked. */
static void pcnetgame_handle_client_clock_sync(const PCNetGameClockSyncMsg* in) {
    OSTime local_now;
    int64_t candidate_offset;
    int64_t current_offset;
    int64_t diff;
    int discontinuity;

    if (!pcnetgame_client_can_apply_world()) {
        return;
    }
    if (in->clock_seq <= s_client_clock_seq_applied) {
        return;
    }

    /* Edge case (see lb_rtc.h's design note): when this process's RTC is disabled/crashed, mTM_time()
     * takes the add_sec fallback branch (m_time.c) which treats Common(time.rtc_time) as the source of
     * truth directly and never re-derives it from lbRTC_GetGameTime()'s OSGetTime()+time_delta+offset
     * sum -- so the offset we are about to (still) record has no observable effect on this client's
     * clock at all. --bootstrap-resident force-enables rtc_enabled, so this is a narrow edge case in
     * practice; detect and log loudly rather than attempting a fix (per the design's explicit scoping). */
    if (Common_Get(time.rtc_enabled) != TRUE || Common_Get(time.rtc_crashed)) {
        printf("[NET][CLOCK] client: WARNING -- received CLOCK_SYNC but this process's RTC is "
               "disabled/crashed (rtc_enabled=%d rtc_crashed=%d); the clock-sync offset will be recorded "
               "but has NO EFFECT until the RTC add_sec fallback path is no longer active\n",
               (int)Common_Get(time.rtc_enabled), (int)Common_Get(time.rtc_crashed));
    }

    discontinuity = (in->flags & PC_NETGAME_CLOCK_SYNC_FLAG_DISCONTINUITY) != 0;
    local_now = OSGetTime() + Save_Get(time_delta);
    candidate_offset = in->host_game_ticks - (int64_t)local_now;
    current_offset = (int64_t)pc_lb_rtc_get_net_clock_offset();
    diff = candidate_offset - current_offset;
    if (diff < 0) {
        diff = -diff;
    }

    if (s_client_clock_seq_applied == 0 || discontinuity ||
        diff > (int64_t)PC_NETGAME_CLOCK_SYNC_TOLERANCE_SEC * (int64_t)GC_TIMER_CLOCK) {
        int64_t drift_sec = diff / (int64_t)GC_TIMER_CLOCK;
        /* Test/diagnostic evidence: log the actual PRE- and POST-correction calendar time (not just
         * the raw tick delta) so a real two-process run can show convergence in the log directly. */
        OSCalendarTime before_cal, after_cal;
        OSTime before_ticks = local_now + current_offset;
        OSTime after_ticks = local_now + candidate_offset;
        OSTicksToCalendarTime(before_ticks, &before_cal);
        OSTicksToCalendarTime(after_ticks, &after_cal);
        pc_lb_rtc_set_net_clock_offset((s64)candidate_offset);
        /* OSCalendarTime.year is an ABSOLUTE proleptic-Gregorian year (e.g. 2027), not
         * years-since-1900 -- see pc_os.c's GetDates()/BIAS -- so no +1900 offset here. mon is
         * 0-based (January == 0), mday is already 1-based. */
        printf("[NET][CLOCK] client: applying host clock correction (clock_seq %u%s, drift ~%lld sec) "
               "local time %04d-%02d-%02d %02d:%02d:%02d -> %04d-%02d-%02d %02d:%02d:%02d\n",
               (unsigned)in->clock_seq, discontinuity ? ", DISCONTINUITY" : "", (long long)drift_sec,
               before_cal.year, before_cal.mon + 1, before_cal.mday, before_cal.hour, before_cal.min,
               before_cal.sec, after_cal.year, after_cal.mon + 1, after_cal.mday, after_cal.hour,
               after_cal.min, after_cal.sec);
        /* Diagnostic only (test/log evidence, no functional purpose): Common(time.rtc_time) itself is
         * NOT updated here -- it is only recomputed once per frame by mTM_time()'s own
         * lbRTC_GetTime() call, so calling the real Kabu_get_price() (which reads Common_Get directly)
         * at this exact instant would report last frame's STALE pre-correction value. Instead, derive
         * the weekday from after_cal (the calendar time this correction implies, computed above) and
         * index Save_Get(kabu_price_schedule.daily_price[...]) directly -- the exact same schedule
         * array Kabu_get_price() reads, exactly mirroring pcnetgame_capture_world_state()'s own
         * precedent for reading it here. This is what Kabu_get_price() will itself return, starting
         * next frame, once Common(time.rtc_time.weekday) catches up. */
        printf("[NET][CLOCK] client: post-correction weekday=%d Kabu price (from schedule)=%u\n",
               after_cal.wday, (unsigned)Save_Get(kabu_price_schedule.daily_price[after_cal.wday]));
        if (drift_sec > (int64_t)PC_NETGAME_CLOCK_SYNC_LOUD_LOG_SEC) {
            printf("[NET][CLOCK] client: WARNING -- clock correction implies ~%lld seconds of divergence "
                   "from the host\n", (long long)drift_sec);
        }
    }
    s_client_clock_seq_applied = in->clock_seq;
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
            /* N3 Channel B late-join coverage: seed this slot's NPC_STATE shadow directly from the
             * snapshot (rather than routing hide/forced_type/forced_timer through
             * mNpc_PcApplyVillagerSnapshotSlot(), which only ever touches Save_t/Animal_c -- these are
             * actor-runtime/network-layer concerns, same separation NPC_MOVE's own snapshot-vs-Animal_c
             * split already keeps). last_applied_seq stays 0 (below any real state_seq, which starts at
             * 1 -- see s_npc_state_seq_counter's pre-increment in pc_net_game_notify_npc_state()), so a
             * genuine NPC_STATE broadcast the host sends later always supersedes this seed, never the
             * reverse. aNPC_pc_client_consume_state() (ac_npc_move.c_inc) applies this the first frame
             * this villager's NPC_ACTOR exists on this client, whenever that ends up being. */
            {
                PCNetNpcStateSlot* seed = &s_npc_state_slot[i];
                seed->have_state = 1;
                seed->cached_npc_id = slot->npc_id;
                seed->last_applied_seq = 0;
                seed->is_home = slot->is_home;
                seed->hide = slot->hide;
                seed->forced_type = slot->forced_type;
                seed->forced_timer_remaining = slot->forced_timer_remaining;
            }
        } else {
            mNpc_PcApplyVillagerDeparture(i, in->now_npc_max);
            /* N3 Channel B / slot-reuse: an unoccupied snapshot slot must also discard any stale
             * NPC_STATE this client happened to have for it (e.g. from a PREVIOUS occupant, before a
             * RESYNC_REQUEST/reconnect rebuilt this snapshot) -- same slot-reuse discipline
             * pc_net_game_get_npc_move_pose() already applies at consume time, applied proactively here
             * since a full snapshot is authoritative over the whole slot. */
            memset(&s_npc_state_slot[i], 0, sizeof(s_npc_state_slot[i]));
        }
    }
    s_client_population_seq = in->world_seq;
    printf("[NET][NPC] client: villager population snapshot applied (world_seq %u, now_npc_max %u)\n",
           (unsigned)in->world_seq, (unsigned)in->now_npc_max);
}

/* ---- M9 identity Stage 1A: host-side identity classification --------------------------------------
 * The ONE place that decides who a joining peer is, using the HOST's own saved resident records only.
 * (Temporary Stage-1A rule: only a RESIDENT claim is admitted; there is no guest class yet. A later stage
 * adds PCNETGAME_IDCLASS_GUEST here without touching the callers' structure.)
 *
 * What the claim is: IDENTITY carries player_name[8] + player_id + the TOWN land_name/land_id (the
 * client sends the Save town fields, pcnetgame_build_identity_msg()). For a resident the town land
 * fields equal its PersonalID land fields (m_private.c / pcnetgame_build_identity_msg() comment), so the
 * claim is a full PersonalID_c and is compared with the vanilla comparator mPr_CheckCmpPersonalID()
 * (land_id, player_id, non-null land name, non-null player name -- exactly how vanilla recognises a
 * resident, cf. mPr_GetPrivateIdx()). A record only counts if it EXISTS: mPr_CheckPrivate() (valid land
 * id) and Private_c.exists == TRUE (exists == FALSE is vanilla's "resident away travelling" state, see
 * the --bootstrap-resident comment in pc_m_card.c; such a record is not an admissible resident here).
 * The client's own player_no is never consulted.
 *
 *   RESIDENT  exactly one record matches; *out_idx = its index 0..PLAYER_NUM-1 (host-derived)
 *   UNKNOWN   no record matches (a guest / foreigner / garbage claim)
 *   AMBIGUOUS more than one record matches (two residents with identical name AND player_id; vanilla's
 *             uniqueness check cannot prevent that, ~1/253 per pair) -- cannot be bound safely */
typedef enum PCNetGameIdentityClass {
    PCNETGAME_IDCLASS_RESIDENT = 0,
    PCNETGAME_IDCLASS_UNKNOWN,
    PCNETGAME_IDCLASS_AMBIGUOUS
} PCNetGameIdentityClass;

static PCNetGameIdentityClass pcnetgame_host_classify_identity(const PCNetGameIdentityMsg* in, int* out_idx) {
    PersonalID_c claim;
    Private_c* priv = Save_Get(private_data);
    int i;
    int matches = 0;
    int idx = -1;

    *out_idx = -1;
    memcpy(claim.player_name, in->player_name, PC_NETGAME_NAME_LEN);
    memcpy(claim.land_name, in->land_name, PC_NETGAME_LAND_LEN);
    claim.player_id = in->player_id;
    claim.land_id = in->land_id;

    for (i = 0; i < PLAYER_NUM; i++) {
        Private_c* p = &priv[i];
        /* NOTE: requiring exists == TRUE is STRICTER than vanilla mPr_GetPrivateIdx() (which ignores it): a resident
         * marked away/travelling is classified UNKNOWN and refused with NO_SAVE until Stage 2 handles it. */
        if (mPr_CheckPrivate(p) == TRUE && p->exists == TRUE && mPr_NullCheckPersonalID(&p->player_ID) == FALSE &&
            mPr_CheckCmpPersonalID(&claim, &p->player_ID) == TRUE) {
            matches++;
            if (idx < 0) {
                idx = i;
            }
        }
    }
    if (matches == 0) {
        return PCNETGAME_IDCLASS_UNKNOWN;
    }
    if (matches > 1) {
        return PCNETGAME_IDCLASS_AMBIGUOUS;
    }
    *out_idx = idx;
    return PCNETGAME_IDCLASS_RESIDENT;
}

/* The resident index the HOST process itself is playing (-1 if none/foreigner): Now_Private points
 * into Save_Get(private_data[]) for a resident (m_start_data_init.c), so the pointer identifies it;
 * the PersonalID comparison is the fallback. */
static int pcnetgame_host_own_resident_idx(void) {
    Private_c* priv = Save_Get(private_data);
    int i;
    if (Now_Private == NULL) {
        return -1;
    }
    for (i = 0; i < PLAYER_NUM; i++) {
        if (&priv[i] == Now_Private) {
            return i;
        }
    }
    for (i = 0; i < PLAYER_NUM; i++) {
        if (mPr_NullCheckPersonalID(&priv[i].player_ID) == FALSE &&
            mPr_CheckCmpPersonalID(&Now_Private->player_ID, &priv[i].player_ID) == TRUE) {
            return i;
        }
    }
    return -1;
}

/* The peer READY and bound to resident `idx` other than `peer` itself (-1 if none). A peer that is
 * merely in HANDSHAKE/closing has bound_valid == 0 (reset), so only live READY peers count. */
static int pcnetgame_host_peer_bound_to_resident(int idx, PCNetPeerId except_peer) {
    int j;
    for (j = 0; j < PC_NET_MAX_PEERS; j++) {
        if (j != (int)except_peer && s_host_peer_link[j] == PC_NETGAME_LINK_READY && s_host_peer[j].bound_valid &&
            s_host_peer[j].bound_resident_idx == idx) {
            return j;
        }
    }
    return -1;
}

/* The PersonalID the peer was VALIDATED with at bind time (cached st->bound_pid, copied from the host save in the
 * same place bound_valid/bound_resident_idx are set). It deliberately does NOT reread Save_Get(private_data): while
 * the host is mid-load that array can hold another save or zeros (peers stay READY until re-validation closes
 * them). 0 if the peer is not bound (never for a READY peer) -- callers must then ignore the request. */
static int pcnetgame_host_bound_personal_id(PCNetPeerId peer, PersonalID_c* out) {
    int idx;
    if (peer < 0 || peer >= PC_NET_MAX_PEERS || !s_host_peer[peer].bound_valid) {
        return 0;
    }
    idx = s_host_peer[peer].bound_resident_idx;
    if (idx < 0 || idx >= PLAYER_NUM) {
        return 0;
    }
    mPr_CopyPersonalID(out, &s_host_peer[peer].bound_pid);
    return 1;
}

/* Stage 1A refusal helper: log, send the (existing, wire-unchanged) REJECT and tear the peer down through
 * the normal reject-and-close path. `use_no_save` selects REJECT(NO_SAVE) (the 24-byte host-town form)
 * instead of REJECT(SERVER_FULL) (the 8-byte form) -- see the comment at the call sites. */
static void pcnetgame_host_refuse_identity(PCNetPeerId peer, const char* why, int use_no_save) {
    printf("[NET][IDENTITY] host: peer %d REFUSED before READY: %s\n", (int)peer, why);
    if (use_no_save) {
        pcnetgame_host_reject_and_close(peer, pcnetgame_send_reject_town(peer, PC_NETGAME_REJECT_NO_SAVE, &s_host_town));
    } else {
        pcnetgame_host_reject_and_close(peer, pcnetgame_send_reject(peer, PC_NETGAME_REJECT_SERVER_FULL));
    }
}

/* M9 identity Stage 1A re-validation: bindings are made at admission, but the host's save can change while the
 * process lives (world paused -> ready again: the host loaded another save / switched resident). Called on the
 * !ready -> ready transition of pcnetgame_host_world_tick(). Closes (existing refuse/reject-and-close path) every
 * READY peer whose bound resident is now the host's OWN resident, or whose saved record no longer matches the
 * PersonalID cached at bind time or no longer exists. Nothing differs -> no effect. */
static void pcnetgame_host_revalidate_bound_peers(void) {
    int i;
    int own_idx = pcnetgame_host_own_resident_idx();
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        PCNetGameHostPeerState* st = &s_host_peer[i];
        const char* why = NULL;
        if (s_host_peer_link[i] != PC_NETGAME_LINK_READY || !st->bound_valid) {
            continue;
        }
        if (own_idx >= 0 && st->bound_resident_idx == own_idx) {
            why = "bound resident became the host's own resident";
        } else if (st->bound_resident_idx < 0 || st->bound_resident_idx >= PLAYER_NUM ||
                   Save_Get(private_data)[st->bound_resident_idx].exists != TRUE ||
                   mPr_CheckCmpPersonalID(&st->bound_pid, &Save_Get(private_data)[st->bound_resident_idx].player_ID) != TRUE) {
            why = "bound resident record changed or no longer exists in the host save";
        }
        if (why != NULL) {
            pcnetgame_host_refuse_identity((PCNetPeerId)i, why, 0);
        }
    }
}

/* v2 host side: validate a parked IDENTITY against the host's own town, then either reject+drop or
 * accept (ACK -> READY -> roster -> snapshot). Only called with the host world ready. */
static void pcnetgame_host_process_identity(PCNetPeerId peer) {
    PCNetGameIdentityMsg in = s_host_peer[peer].pending_identity; /* copy: the reset below clears it */
    PCNetGameTownIdentity peer_town;
    char host_buf[96], peer_buf[96];
    PCNetGameIdentityClass id_class;
    int resident_idx = -1;
    int own_idx;
    int other;

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

    /* M9 identity Stage 1A: classify the claim against the HOST's own resident records BEFORE any ACK/READY.
     * The client's player_no is not part of the claim and is never read. Reject codes (no new wire value --
     * the REJECT reason enum is frozen in v7): "this identity is unavailable" (ambiguous, the host's own
     * resident, already connected) reuses REJECT(SERVER_FULL), the reserved-but-unsent code, whose meaning
     * ("no room for you") is the closest; "the host has no such resident" reuses REJECT(NO_SAVE), the 24-byte
     * form, whose meaning ("no usable save record for this player") is the closest. The client treats every
     * REJECT identically (log + shutdown), so neither has a behavioural downside; only the log wording is
     * imprecise. The host-side log line carries the real reason. */
    id_class = pcnetgame_host_classify_identity(&in, &resident_idx);
    if (id_class == PCNETGAME_IDCLASS_AMBIGUOUS) {
        pcnetgame_host_refuse_identity(peer, "claimed identity matches more than one resident record (ambiguous)", 0);
        return;
    }
    if (id_class != PCNETGAME_IDCLASS_RESIDENT) {
        /* Temporary Stage-1A rule: unknown identities (guests) are refused until guest admission exists. */
        pcnetgame_host_refuse_identity(peer, "claimed identity matches no resident record of this town", 1);
        return;
    }
    own_idx = pcnetgame_host_own_resident_idx();
    if (own_idx >= 0 && resident_idx == own_idx) {
        printf("[NET][IDENTITY] host: peer %d claims resident %d, which is the host's own active resident\n", (int)peer,
               resident_idx);
        pcnetgame_host_refuse_identity(peer, "claimed resident is the host's own resident", 0);
        return;
    }
    other = pcnetgame_host_peer_bound_to_resident(resident_idx, peer);
    if (other >= 0) {
        /* No replacement policy in Stage 1A: the live connection wins, the newcomer is refused. A reconnect
         * from a new address while the old session is still silent-but-alive (up to the transport timeout)
         * is therefore refused until the old peer is gone -- Stage 1B decides replace-stale. A restart from
         * the SAME ip:port reuses its transport slot (pc_net.c) and is reset on PEER_CONNECTED, so it never
         * collides with itself (the scan excludes `peer`). */
        printf("[NET][IDENTITY] host: peer %d claims resident %d, already bound to live peer %d\n", (int)peer,
               resident_idx, other);
        pcnetgame_host_refuse_identity(peer, "claimed resident is already connected on another peer", 0);
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
    s_host_peer[peer].bound_valid = 1;
    s_host_peer[peer].bound_resident_idx = resident_idx;
    mPr_CopyPersonalID(&s_host_peer[peer].bound_pid, &Save_Get(private_data)[resident_idx].player_ID);
    printf("[NET][IDENTITY] host: peer %d bound to resident %d (host-derived)\n", (int)peer, resident_idx);
    printf("[NET] host: peer %d identity OK (player_id=%u, %s) -> READY\n", (int)peer, (unsigned)in.player_id, peer_buf);

    /* Friendship/mail sync milestone: cache this peer's own PersonalID_c fields for the lifetime of
     * the connection (see PCNetGameHostPeerState's doc comment on ready_identity_valid). */
    memcpy(s_host_peer[peer].ready_player_name, in.player_name, PC_NETGAME_NAME_LEN);
    memcpy(s_host_peer[peer].ready_land_name, in.land_name, PC_NETGAME_LAND_LEN);
    s_host_peer[peer].ready_player_id = in.player_id;
    s_host_peer[peer].ready_land_id = in.land_id;
    s_host_peer[peer].ready_identity_valid = 1;

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

    /* M9-A late join / reconnect: tell this now-READY client where the host and every other READY peer
     * currently are (the smallest correct mechanism: the existing READY hook + the per-slot scene storage;
     * no snapshot machinery). The reliable channel orders this after the IDENTITY_ACK. */
    pcnetgame_host_send_scene_roster(peer);

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
    /* M9 identity Stage 1A: the peer's claimed player_no is NOT used for anything. ctx.player_no (read only by
     * log lines) holds the HOST-DERIVED bound resident index; a differing claim is warned about once. */
    st->ctx.player_no = (uint8_t)(st->bound_valid ? st->bound_resident_idx : 0);
    if (st->bound_valid && (int)in->player_no != st->bound_resident_idx && !st->ctx_player_no_warned) {
        st->ctx_player_no_warned = 1;
        printf("[NET][IDENTITY] host: peer %d PLAYER_CONTEXT claims player_no=%u but is bound to resident %d -- "
               "claim ignored\n", (int)peer, (unsigned)in->player_no, st->bound_resident_idx);
    }
    {
        /* Clamp to the legal ranges before any gameplay calc reads them (sources per line):
         *  - destiny_type: mPr_DESTINY_* enum, 0..mPr_DESTINY_NUM-1 (m_private.h); anything else -> NORMAL.
         *  - goods_power: mPr_GetGoodsPower() returns it clamped to [mPr_GOODS_POWER_MIN, mPr_GOODS_POWER_MAX]
         *    (-30..50, m_private.c) -- exactly the range an honest client can send.
         *  - money_power: mPr_GetMoneyPower() clamps its low end at mPr_MONEY_POWER_MIN (-80) and has NO upper
         *    clamp in vanilla (it is Common money_power from the house's feng-shui score plus +100 for
         *    MONEY_LUCK, bounded only by the furniture in the house). So only the low end is clamped; every
         *    consumer saturates a large value anyway (the money-rock swing_time is capped at 100, the
         *    money-tree win test is already certain once money_power >= 100).
         *  - flags: only PC_NETGAME_CTX_FLAG_IN_TOWN is defined.
         * Valid values pass through unchanged. */
        uint8_t destiny = in->destiny_type;
        uint8_t flags = (uint8_t)(in->flags & PC_NETGAME_CTX_FLAG_IN_TOWN);
        int16_t money = in->money_power;
        int16_t goods = in->goods_power;
        int clamped = 0;
        if (destiny >= (uint8_t)mPr_DESTINY_NUM) {
            destiny = (uint8_t)mPr_DESTINY_NORMAL;
            clamped = 1;
        }
        if (money < (int16_t)mPr_MONEY_POWER_MIN) {
            money = (int16_t)mPr_MONEY_POWER_MIN;
            clamped = 1;
        }
        if (goods < (int16_t)mPr_GOODS_POWER_MIN) {
            goods = (int16_t)mPr_GOODS_POWER_MIN;
            clamped = 1;
        } else if (goods > (int16_t)mPr_GOODS_POWER_MAX) {
            goods = (int16_t)mPr_GOODS_POWER_MAX;
            clamped = 1;
        }
        if (flags != in->flags) {
            clamped = 1;
        }
        if (clamped && (g_pc_verbose || !st->ctx_clamp_logged)) {
            st->ctx_clamp_logged = 1;
            printf("[NET][IDENTITY] host: peer %d PLAYER_CONTEXT out-of-range values clamped: destiny %u->%u "
                   "money %d->%d goods %d->%d flags 0x%02X->0x%02X\n", (int)peer, (unsigned)in->destiny_type,
                   (unsigned)destiny, (int)in->money_power, (int)money, (int)in->goods_power, (int)goods,
                   (unsigned)in->flags, (unsigned)flags);
        }
        st->ctx.destiny_type = destiny;
        st->ctx.flags = flags;
        st->ctx.money_power = money;
        st->ctx.goods_power = goods;
    }
    if (!st->ctx_valid || g_pc_verbose) {
        printf("[NET] host: peer %d context player_no=%u destiny=%u flags=0x%02X money=%d goods=%d\n", (int)peer,
               (unsigned)st->ctx.player_no, (unsigned)st->ctx.destiny_type, (unsigned)st->ctx.flags,
               (int)st->ctx.money_power, (int)st->ctx.goods_power);
    }
    st->ctx_valid = 1;
}

/* Host side: a peer's raw PC_NET_EVENT_DATA payload. Anything that isn't a well-formed message of
 * a known type/size is ignored -- a peer is only ever marked READY by successfully validating an
 * IDENTITY, never merely by having sent *some* UDP packet. */
/* Forward-declared: defined near the other friendship/mail-sync host-side functions, below; used
 * here (pcnetgame_handle_host_data()) before that point in the file. */
static void pcnetgame_handle_host_friendship_request(PCNetPeerId peer, const PCNetGameFriendshipRequestMsg* in);
static void pcnetgame_handle_host_mail_request(PCNetPeerId peer, const PCNetGameMailRequestMsg* in);

/* World Ecology T3: host side of a client's bury request. Exact structural mirror of
 * pcnetgame_handle_host_drop_request() (replay per phase, abort the peer's pending BURY only -- a
 * pending pickup/drop of the same peer is independent --, validate, reserve-or-record, reply) -- see
 * that function's own doc for the shared reasoning. Never touches any ACTOR, any PLAYER_ACTOR, or
 * Now_Private; only reads the shared field and sends small, self-contained network messages. The field
 * write happens only on COMMIT (pcnetgame_handle_host_confirm()'s own BURY branch), where the actual
 * bury/plant/pitfall outcome table runs. A rejection here (unlike a rejection from the drop validator)
 * carries the reconciliation echo (pcnetgame_host_send_bury_reject()) -- a bury client applies it via
 * the same tile+deposit reconciliation path it uses for a COMMIT-time reject, so both reject points
 * share one client-side code path. */
static void pcnetgame_handle_host_bury_request(PCNetPeerId peer, const PCNetGameBuryRequestMsg* in) {
    PCNetGameHostInteraction* rec;
    int accepted;
    int acre = 0, tile = 0;
    uint16_t raw_item = 0;
    int prev_valid;
    uint32_t prev_rid;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return; /* not a known, handshake-complete peer -- never process on their behalf */
    }

    rec = &s_host_bury_state[peer];

    if (rec->phase != (uint8_t)PC_NETGAME_PHASE_NONE && rec->request_id == in->request_id) {
        pcnetgame_host_replay(peer, (int)PC_NETGAME_INTERACT_KIND_BURY, rec);
        return;
    }
    if (rec->prev_valid && rec->prev_request_id == in->request_id) {
        pcnetgame_host_send_bury_reject(peer, in->request_id, in->ut_x, in->ut_z);
        return;
    }

    pcnetgame_host_abort_pending_of_peer(peer, (int)PC_NETGAME_INTERACT_KIND_BURY,
                                         "replaced by a newer request from the same peer");

    accepted = pcnetgame_validate_and_resolve_bury(0, peer, in->pocket_slot_idx, in->ut_x, in->ut_z,
                                                   (mActor_name_t)in->claimed_item, in->hole_variant, &acre, &tile,
                                                   &raw_item);

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
        rec->item = in->claimed_item;
        rec->raw_item = raw_item;
        rec->acre = (uint8_t)acre;
        rec->tile = (uint8_t)tile;
        rec->hole_variant = in->hole_variant;
        rec->reserved_since_ms = pcnetgame_now_ms();
        printf("[NET][BURY] host: peer %d request %u reserved tile (%d,%d) claimed_item=0x%04X hole_variant=%u "
               "(field unchanged until CONFIRM)\n",
               (int)peer, (unsigned)in->request_id, (int)in->ut_x, (int)in->ut_z, (unsigned)in->claimed_item,
               (unsigned)in->hole_variant);
        pcnetgame_host_send_bury_result(peer, in->request_id, in->ut_x, in->ut_z, 1, in->claimed_item, 0,
                                        (uint8_t)PC_NETGAME_BURY_REASON_NONE);
    } else {
        rec->phase = (uint8_t)PC_NETGAME_PHASE_DONE; /* final rejection; replayed verbatim on a retry */
        rec->accepted = 0;
        pcnetgame_host_send_bury_reject(peer, in->request_id, in->ut_x, in->ut_z);
    }
}

/* World Ecology Wildlife Sync T0: defined near the other wildlife-sync functions, below; used here
 * (pcnetgame_handle_host_data()) before that point in the file. */
static void pcnetgame_handle_host_wildlife_spawn_trigger_request(PCNetPeerId peer,
                                                                 const PCNetGameWildlifeSpawnTriggerRequestMsg* in);

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

    if (size == sizeof(PCNetGamePlayerSceneMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_PLAYER_SCENE) {
        PCNetGamePlayerSceneMsg ps;
        memcpy(&ps, data, sizeof(ps));
        pcnetgame_handle_host_player_scene(peer, &ps);
        return;
    }

    if (size == sizeof(PCNetGameNpcTalkMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_NPC_TALK) {
        PCNetGameNpcTalkMsg nt;
        memcpy(&nt, data, sizeof(nt));
        pcnetgame_handle_host_npc_talk(peer, &nt);
        return;
    }

    if (data[0] == (uint8_t)PC_NETGAME_MSG_PLAYER_ACTION) {
        /* M9-C: PLAYER_ACTION is host -> client only; a client never originates it. Dropped, never relayed. */
        if (pcnetgame_action_diag()) {
            printf("[NET][ACTION][DIAG] host: peer %d sent a client-originated PLAYER_ACTION (size %u) -- dropped\n",
                   (int)peer, (unsigned)size);
        }
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

    if (size == sizeof(PCNetGameFieldActionRequestMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_FIELD_ACTION_REQUEST) {
        PCNetGameFieldActionRequestMsg far;
        memcpy(&far, data, sizeof(far));
        pcnetgame_handle_host_field_action_request(peer, &far);
        return;
    }

    if (size == sizeof(PCNetGameBuryRequestMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_BURY_REQUEST) {
        PCNetGameBuryRequestMsg br;
        memcpy(&br, data, sizeof(br));
        pcnetgame_handle_host_bury_request(peer, &br);
        return;
    }

    if (size == sizeof(PCNetGameFriendshipRequestMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_FRIENDSHIP_REQUEST) {
        PCNetGameFriendshipRequestMsg fr;
        memcpy(&fr, data, sizeof(fr));
        pcnetgame_handle_host_friendship_request(peer, &fr);
        return;
    }

    if (size == sizeof(PCNetGameMailRequestMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_MAIL_REQUEST) {
        PCNetGameMailRequestMsg mr;
        memcpy(&mr, data, sizeof(mr));
        pcnetgame_handle_host_mail_request(peer, &mr);
        return;
    }

    if (size == sizeof(PCNetGameSnowmanBuildRequestMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_SNOWMAN_BUILD_REQUEST) {
        PCNetGameSnowmanBuildRequestMsg sr;
        memcpy(&sr, data, sizeof(sr));
        pcnetgame_handle_host_snowman_build_request(peer, &sr);
        return;
    }

    if (size == sizeof(PCNetGameWildlifeSpawnTriggerRequestMsg) &&
        data[0] == (uint8_t)PC_NETGAME_MSG_WILDLIFE_SPAWN_TRIGGER_REQUEST) {
        PCNetGameWildlifeSpawnTriggerRequestMsg wr;
        memcpy(&wr, data, sizeof(wr));
        pcnetgame_handle_host_wildlife_spawn_trigger_request(peer, &wr);
        return;
    }

    if (size == sizeof(PCNetGameCatchRequestMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_CATCH_REQUEST) {
        PCNetGameCatchRequestMsg cr;
        memcpy(&cr, data, sizeof(cr));
        pcnetgame_handle_host_catch_request(peer, &cr);
        return;
    }

    /* malformed / short / unrecognized: ignore rather than misinterpret */
}

/* Client side: the host's raw PC_NET_EVENT_DATA payload. */
/* Forward-declared: defined near the other friendship/mail-sync client-side functions, below; used
 * here (pcnetgame_handle_client_data()) before that point in the file. */
static void pcnetgame_handle_client_friendship_update(const PCNetGameFriendshipUpdateMsg* in);
static void pcnetgame_handle_client_friendship_snapshot_entry(const PCNetGameFriendshipSnapshotEntryMsg* in);
static void pcnetgame_handle_client_mail_delivered(const PCNetGameMailDeliveredMsg* in);
static void pcnetgame_handle_client_npc_move(const PCNetGameNpcMoveMsg* in);
static void pcnetgame_handle_client_npc_state(const PCNetGameNpcStateMsg* in);
static void pcnetgame_handle_client_field_action_result(const PCNetGameFieldActionResultMsg* in);
static void pcnetgame_handle_client_wildlife_spawn(const PCNetGameWildlifeSpawnMsg* in);
static void pcnetgame_handle_client_wildlife_snapshot_begin(const PCNetGameWildlifeSnapshotBeginMsg* in);
static void pcnetgame_handle_client_wildlife_snapshot_entry(const PCNetGameWildlifeSnapshotEntryMsg* in);
static void pcnetgame_handle_client_wildlife_snapshot_end(const PCNetGameWildlifeSnapshotEndMsg* in);
static void pcnetgame_handle_client_catch_result(const PCNetGameCatchResultMsg* in);
static void pcnetgame_handle_client_wildlife_despawn(const PCNetGameWildlifeDespawnMsg* in);

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

    if (size == sizeof(PCNetGamePlayerSceneMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_PLAYER_SCENE) {
        PCNetGamePlayerSceneMsg ps;
        memcpy(&ps, data, sizeof(ps));
        pcnetgame_handle_client_player_scene(&ps);
        return;
    }

    if (size == sizeof(PCNetGamePlayerActionMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_PLAYER_ACTION) {
        PCNetGamePlayerActionMsg pa;
        memcpy(&pa, data, sizeof(pa));
        pcnetgame_handle_client_player_action(&pa);
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

    if (size == sizeof(PCNetGameFieldActionResultMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_FIELD_ACTION_RESULT) {
        PCNetGameFieldActionResultMsg far;
        memcpy(&far, data, sizeof(far));
        pcnetgame_handle_client_field_action_result(&far);
        return;
    }

    if (size == sizeof(PCNetGameBuryResultMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_BURY_RESULT) {
        PCNetGameBuryResultMsg br;
        memcpy(&br, data, sizeof(br));
        pcnetgame_handle_client_bury_result(&br);
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

    if (size == sizeof(PCNetGameFriendshipUpdateMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_FRIENDSHIP_UPDATE) {
        PCNetGameFriendshipUpdateMsg fu;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&fu, data, sizeof(fu));
        pcnetgame_handle_client_friendship_update(&fu);
        return;
    }

    if (size == sizeof(PCNetGameFriendshipSnapshotEntryMsg) &&
        data[0] == (uint8_t)PC_NETGAME_MSG_FRIENDSHIP_SNAPSHOT_ENTRY) {
        static PCNetGameFriendshipSnapshotEntryMsg fe; /* 292 B: static, handled synchronously */
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&fe, data, sizeof(fe));
        pcnetgame_handle_client_friendship_snapshot_entry(&fe);
        return;
    }

    if (size == sizeof(PCNetGameMailDeliveredMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_MAIL_DELIVERED) {
        static PCNetGameMailDeliveredMsg md; /* 288 B: static, handled synchronously */
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&md, data, sizeof(md));
        pcnetgame_handle_client_mail_delivered(&md);
        return;
    }

    if (size == sizeof(PCNetGameNpcMoveMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_NPC_MOVE) {
        PCNetGameNpcMoveMsg nm;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&nm, data, sizeof(nm));
        pcnetgame_handle_client_npc_move(&nm);
        return;
    }

    if (size == sizeof(PCNetGameNpcStateMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_NPC_STATE) {
        PCNetGameNpcStateMsg ns;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&ns, data, sizeof(ns));
        pcnetgame_handle_client_npc_state(&ns);
        return;
    }

    if (size == sizeof(PCNetGameSnowmanBuildResultMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_SNOWMAN_BUILD_RESULT) {
        PCNetGameSnowmanBuildResultMsg br;
        memcpy(&br, data, sizeof(br));
        pcnetgame_handle_client_snowman_build_result(&br);
        return;
    }

    if (size == sizeof(PCNetGameSnowmanStateMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_SNOWMAN_STATE) {
        PCNetGameSnowmanStateMsg ss;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&ss, data, sizeof(ss));
        pcnetgame_handle_client_snowman_state(&ss);
        return;
    }

    if (size == sizeof(PCNetGameWildlifeSpawnMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_WILDLIFE_SPAWN) {
        PCNetGameWildlifeSpawnMsg ws;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&ws, data, sizeof(ws));
        pcnetgame_handle_client_wildlife_spawn(&ws);
        return;
    }

    if (size == sizeof(PCNetGameWildlifeSnapshotBeginMsg) &&
        data[0] == (uint8_t)PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_BEGIN) {
        PCNetGameWildlifeSnapshotBeginMsg wb;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&wb, data, sizeof(wb));
        pcnetgame_handle_client_wildlife_snapshot_begin(&wb);
        return;
    }

    if (size == sizeof(PCNetGameWildlifeSnapshotEntryMsg) &&
        data[0] == (uint8_t)PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_ENTRY) {
        PCNetGameWildlifeSnapshotEntryMsg we;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&we, data, sizeof(we));
        pcnetgame_handle_client_wildlife_snapshot_entry(&we);
        return;
    }

    if (size == sizeof(PCNetGameWildlifeSnapshotEndMsg) &&
        data[0] == (uint8_t)PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_END) {
        PCNetGameWildlifeSnapshotEndMsg we_end;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&we_end, data, sizeof(we_end));
        pcnetgame_handle_client_wildlife_snapshot_end(&we_end);
        return;
    }

    if (size == sizeof(PCNetGameCatchResultMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_CATCH_RESULT) {
        PCNetGameCatchResultMsg cr;
        memcpy(&cr, data, sizeof(cr));
        pcnetgame_handle_client_catch_result(&cr);
        return;
    }

    if (size == sizeof(PCNetGameWildlifeDespawnMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_WILDLIFE_DESPAWN) {
        PCNetGameWildlifeDespawnMsg wd;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&wd, data, sizeof(wd));
        pcnetgame_handle_client_wildlife_despawn(&wd);
        return;
    }

    if (size == sizeof(PCNetGameClockSyncMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_CLOCK_SYNC) {
        PCNetGameClockSyncMsg cs;
        if (s_client_link != PC_NETGAME_LINK_READY) return;
        memcpy(&cs, data, sizeof(cs));
        pcnetgame_handle_client_clock_sync(&cs);
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
    s_local_scene_sent = 0; /* M9-A: a new connection must re-announce the (still live) local scene */
    s_npc_talk_seq = 0; /* M9-C: new session: sequence restarts; the NPC edge code drops its sent bits on the epoch */
    memset(s_action_last_seq, 0, sizeof(s_action_last_seq)); /* M9-C Phase 5: per-origin PLAYER_ACTION seq memory */
    memset(s_action_last_valid, 0, sizeof(s_action_last_valid));
    s_npc_talk_epoch++;
    memset(s_client_talk_out, 0, sizeof(s_client_talk_out)); /* keepalive table dies with the session */
    {
        /* M9-A: every scene this client holds for OTHER players (the host and relayed clients) came over the
         * old host link; once that link is lost or replaced it can no longer be trusted (the new connection
         * replays join-time scenes), and entries stored before any MOVE arrived are never reaped by the
         * puppet liveness timeout. Only scene identity is cleared -- puppets/movement state are untouched. */
        int scene_pid;
        for (scene_pid = 0; scene_pid <= (int)PC_NETGAME_HOST_PLAYER_ID; scene_pid++) {
            pc_remote_player_clear_scene((PCNetPlayerId)scene_pid);
        }
    }
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
    memset(&s_exchange_deferred, 0, sizeof(s_exchange_deferred)); /* M9-D G4-2 */
    s_exchange_swap_slot = -1;
    memset(&s_bury_pending, 0, sizeof(s_bury_pending));
    memset(&s_bury_committed, 0, sizeof(s_bury_committed)); /* M9-D G2-3 */
    s_next_bury_request_id = 1;
    memset(s_field_action_queue, 0, sizeof(s_field_action_queue)); /* T0-C: whole queue, not one slot */
    s_field_action_queue_len = 0;
    s_next_field_action_request_id = 1;

    /* M9-D F3: the catch request in flight dies with the session too (its RESULT can never match the
     * new connection). A still-PENDING catch is converted to a REJECTED outcome for the same entity
     * instead of being silently dropped: the putaway exchange gate (pc_net_game_query_catch_outcome)
     * keeps denying that catch (the host may or may not have accepted it; either way this client must
     * not grant it), but PENDING no longer lingers across sessions. A stale outcome is cleared when the
     * next catch request starts (pcnetgame_request_catch_common). s_next_catch_request_id is NOT reset:
     * it stays monotonic across reconnects (see the catch request handler's id match). */
    if (s_catch_pending.valid) {
        s_catch_last_outcome.valid = 1;
        s_catch_last_outcome.entity_id = s_catch_pending.entity_id;
        s_catch_last_outcome.accepted = 0;
    }
    memset(&s_catch_pending, 0, sizeof(s_catch_pending));

    memset(s_client_acre_seq, 0, sizeof(s_client_acre_seq));
    s_client_meta_seq = 0;
    s_client_population_seq = 0;
    s_client_friendship_seq = 0;
    /* World Ecology Wildlife Sync T2 REVISION (was T1: an unconditional pcwld_presentation_reset()
     * here -- REMOVED, see below for why): a client's local entity_id -> presentation-actor map must
     * NOT be blindly wiped on every mere disconnect/reconnect any more. A real actor T1 already
     * created is NEVER destroyed by anything in pc_wildlife_authority.c (see pcwld_presentation_
     * reset()'s own doc), so if this call ran here unconditionally, a reconnect to the SAME
     * still-running host (whose authoritative table and entity_ids never changed) would blindly
     * forget every entity it already correctly tracked, then treat every entry in the very next
     * WILDLIFE_SNAPSHOT_ENTRY as brand-new and materialize a SECOND, duplicate real actor for
     * anything still alive from before the disconnect -- exactly the bug the T2 milestone brief's own
     * "reconciliation" requirement (pcwld_presentation_reconcile()) exists to prevent. The correct
     * "did the authoritative session actually change" check now happens precisely once, at
     * pcnetgame_handle_client_wildlife_snapshot_begin() (comparing the host's current
     * pcwld_session_generation() against s_client_wildlife_known_generation, which is DELIBERATELY
     * NOT reset here -- see that variable's own doc, above) -- a genuine new/different session (host
     * restarted, or this is truly a different host) still fully clears the local map there, just
     * later and more precisely than an unconditional reset on every ordinary reconnect would. */
    s_client_wildlife_snap_active = 0;
    s_client_wildlife_snap_epoch = 0;
    s_client_wildlife_seen_count = 0;
    /* N-clock milestone, Rule 2: reset the sequence tracker (a reconnect is a fresh join -- the first
     * post-reconnect CLOCK_SYNC must always be accepted) but deliberately do NOT touch the clock
     * offset itself (pc_lb_rtc_get/set_net_clock_offset(), lb_rtc.c) here -- it is process-memory-only,
     * has no variable here, and must survive an ordinary disconnect (s_role stays
     * PC_NETGAME_ROLE_CLIENT, only s_client_link changes -- e.g. PC_NET_EVENT_PEER_DISCONNECTED) so
     * the displayed clock stays smooth rather than snapping back to this process's own free-running
     * local clock while a reconnect might still restore the same host relationship. Clock hardening
     * (concern #1): the offset IS explicitly reset, but only in pc_net_game_shutdown() -- the one
     * place s_role actually reverts to PC_NETGAME_ROLE_NONE -- never here. */
    s_client_clock_seq_applied = 0;
    s_client_snowman_seq = 0; /* World Ecology: snowmen -- see this variable's own doc */
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

    /* N2: drop every buffered villager-movement sample/identity across a disconnect/reconnect --
     * otherwise a slot's stale ring from the PREVIOUS session (a fresh session's villager population
     * for that same slot index may well be a completely different npc_id, or none at all) could
     * satisfy pc_net_game_get_npc_move_pose()'s identity check by sheer coincidence in the tiny
     * window before this session's own first NPC_MOVE/ARRIVAL/SNAPSHOT for that slot arrives.
     * memset-zero is the correct empty state: cached_npc_id 0 is never a real mActor_name_t NPC id
     * (see ITEM_NAME_GET_TYPE(...) != NAME_TYPE_NPC's use elsewhere in this file), and
     * snapshot_count/have_frame 0 already mean "no data yet" everywhere they're read. */
    memset(s_npc_move_slot, 0, sizeof(s_npc_move_slot));

    /* N3 Channel B: same reasoning as the NPC_MOVE ring reset just above -- a previous session's
     * slot-indexed is_home/hide/forced_type state must never survive into a fresh session where that
     * same slot index may hold a different villager (or none). */
    memset(s_npc_state_slot, 0, sizeof(s_npc_state_slot));
}

/* v2: clears every piece of host world state (start_host / shutdown). */
static void pcnetgame_reset_host_world_state(void) {
    int i;
    memset(&s_host_local_drop_landing, 0, sizeof(s_host_local_drop_landing)); /* no stale arm across sessions */
    memset(s_action_seq_out, 0, sizeof(s_action_seq_out)); /* M9-C Phase 5: PLAYER_ACTION seq restarts per host session (see PCNetGamePlayerActionMsg) */
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

    /* N2: reset every per-slot host send-throttle timer and the shared send counter across a
     * start_host/shutdown boundary, so a fresh hosting session never inherits a mid-cycle throttle
     * phase or a huge previous-session frame counter value in its very first log line. */
    memset(s_npc_move_send_accum, 0, sizeof(s_npc_move_send_accum));
    s_npc_move_send_counter = 0;

    /* N3 Channel B: fresh hosting session starts every slot's dirty-check shadow and the shared
     * state_seq counter clean, so it never suppresses this session's genuinely-first broadcast for a
     * slot by comparing against a previous session's shadow, and a client's own per-slot
     * last_applied_seq (reset above/on connect) is always < this session's first state_seq. */
    memset(s_npc_state_shadow, 0, sizeof(s_npc_state_shadow));
    s_npc_state_seq_counter = 0;

    /* N-clock milestone: fresh hosting session starts its own clock_seq at 0 (a client's own
     * s_client_clock_seq_applied is reset to 0 on (re)connect too, so the first CLOCK_SYNC of any
     * hosting session is always accepted) and re-arms the manual-adjust watch/periodic timer so a
     * stale reading from a PREVIOUS hosting session in this same process can never be compared
     * against. A host process is never itself offset (s_pc_net_clock_offset defaults to, and stays,
     * 0 unless pc_lb_rtc_set_net_clock_offset() is called -- which only ever happens client-side). */
    s_host_clock_seq = 0;
    s_host_clock_sync_last_ms = 0;
    s_host_clock_last_delta_valid = 0;

    /* World Ecology Stage 1, Item 2: a fresh hosting session starts with every money-rock window
     * closed -- a stale window from a previous session must never be judged against, or reverted
     * into, this session's (possibly freshly loaded/regenerated) field. */
    memset(s_host_money_rock, 0, sizeof(s_host_money_rock));
    /* World Ecology T1 review fix: a fresh hosting session likewise starts with every tree-cut
     * cut-count slot cleared -- see pcnetgame_reset_host_tree_cut_state()'s own doc. */
    pcnetgame_reset_host_tree_cut_state();
    /* World Ecology Wildlife Sync T0: a fresh hosting session likewise starts with an empty wildlife
     * table and entity_id counter reset to 1 -- see pcwld_reset()'s own doc. */
    pcwld_reset();
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
    memset(&s_local_scene, 0, sizeof(s_local_scene)); /* M9-A: back to single-player: nothing announced */
    s_local_scene_seq = 0;

    /* Clock hardening (concern #1): this is the ONE place the process genuinely stops being a
     * network client and returns to ROLE_NONE (single-player) -- see the doc comment on
     * pcnetgame_reset_client_session_state() above for why that function itself deliberately does
     * NOT touch the offset (an ordinary disconnect that leaves s_role == PC_NETGAME_ROLE_CLIENT,
     * e.g. PC_NET_EVENT_PEER_DISCONNECTED while still hoping to reconnect, must keep the last-known
     * host offset so the displayed clock stays smooth). Once we are actually back to ROLE_NONE,
     * though, lb_rtc.h's contract ("always exactly 0 for ROLE_NONE") must hold: reset it here so a
     * subsequent single-player session -- including on a different local save the player loads next
     * -- never silently inherits a stale host clock correction. A host process never set this
     * non-zero in the first place, so this is a harmless no-op on host shutdown. */
    pc_lb_rtc_set_net_clock_offset(0);

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

/* Friendship/mail sync milestone, TEST-ONLY: fires mNpc_DebugForceFriendshipDelta()/
 * mNpc_DebugForceMailSend() (m_npc.c) exactly once each, on WHICHEVER role this process is -- see
 * pc_platform.h's doc comment on g_pc_force_friendship_delta/g_pc_force_mail_send. Unlike
 * pcnetgame_run_villager_test_triggers() (host-only: population growth is inherently host-
 * authoritative), these deliberately run for a CLIENT too, since the whole point is to exercise
 * mNpc_AddFriendship()/mNpc_SendMailtoNpc()'s client-intercept-and-request path for real, not just
 * the host-apply-and-broadcast path. */
static void pcnetgame_run_friendship_mail_test_triggers(void) {
    static int s_friendship_done = 0;
    static int s_mail_done = 0;
    int world_ready;

    if (s_role == PC_NETGAME_ROLE_NONE || gamePT == NULL) {
        return;
    }
    if (s_role == PC_NETGAME_ROLE_HOST) {
        world_ready = s_host_world_ready;
    } else {
        world_ready = (s_client_link == PC_NETGAME_LINK_READY) && s_local_world_latched;
    }
    if (!world_ready) {
        return;
    }

    if (g_pc_force_friendship_delta != 0 && !s_friendship_done) {
        s_friendship_done = 1;
        mNpc_DebugForceFriendshipDelta(g_pc_force_friendship_delta);
    }
    if (g_pc_force_mail_send && !s_mail_done) {
        s_mail_done = 1;
        mNpc_DebugForceMailSend();
    }
}

/* World Ecology Stage 1 money-rock review, TEST-ONLY: fires --force-money-rock-hit /
 * --force-money-bag-pickup exactly once each -- see pc_platform.h's doc comment on these two globals.
 * Mirrors pcnetgame_run_villager_test_triggers()/pcnetgame_run_friendship_mail_test_triggers()'s own
 * exact gating pattern. Both target the fixed (24,104) money-rock tile --field-action-test-seed seeds
 * (pcnetgame_run_field_action_test_seed()), and the 9-candidate neighborhood
 * pcnetgame_find_money_rock_drop_tile() itself searches from that same center. */
static void pcnetgame_run_money_rock_test_triggers(void) {
    static int s_hit_done = 0;
    static int s_pickup_done = 0;

    if (gamePT == NULL) {
        return;
    }

    if (g_pc_force_money_rock_hit && !s_hit_done && s_role == PC_NETGAME_ROLE_HOST && s_host_world_ready) {
        /* Wait for the tile to ACTUALLY be a money rock before hitting it -- --field-action-test-seed's
         * own fixture write is itself retried every poll until it succeeds (see its own doc: a
         * candidate tile it declines is retried, with no fixed timing), so this must not assume the
         * seed has already landed on the very first world-ready poll. Retried every poll, no timeout,
         * same reasoning as the seed fixture itself: test-only code where waiting costs nothing. */
        int acre, tile;
        uint16_t value;
        if (pcfa_town_ut_to_acre_tile(24, 104, &acre, &tile) && pcfa_get_tile(acre, tile, &value) &&
            IS_ITEM_STONE_TC((mActor_name_t)value)) {
            s_hit_done = 1;
            printf("[NET][FIELD_ACTION] --force-money-rock-hit active: forcing the host's own local money-rock hit "
                   "at tile (24,104)\n");
            pc_net_game_host_local_money_rock_hit(24, 104);
        }
    }

    if (g_pc_force_money_bag_pickup && !s_pickup_done && s_role == PC_NETGAME_ROLE_CLIENT &&
        s_client_link == PC_NETGAME_LINK_READY && s_local_world_latched && pcfa_scene_is_town()) {
        /* Same 9-tile candidate order pcnetgame_find_money_rock_drop_tile() searches, centered on the
         * --field-action-test-seed fixture's own money-rock tile (24,104) -- read from this client's
         * own already-applied local field copy (see pc_platform.h's doc: kept current by real
         * FIELD_UPDATE messages, the same data a real player's vanilla pickup targeting would see). */
        static const int dxs[9] = { -1, -1, 1, 1, -1, 0, 1, 0, 0 };
        static const int dzs[9] = { -1, 1, 1, -1, 0, 1, 0, -1, 0 };
        int i;
        for (i = 0; i < 9; i++) {
            int ut_x = 24 + dxs[i];
            int ut_z = 104 + dzs[i];
            int acre, tile;
            uint16_t value;
            if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
                continue;
            }
            if (!pcfa_town_ut_to_acre_tile(ut_x, ut_z, &acre, &tile) || !pcfa_get_tile(acre, tile, &value)) {
                continue;
            }
            if (pcnetgame_is_money_bag_item((mActor_name_t)value)) {
                s_pickup_done = 1;
                printf("[NET][PICKUP] --force-money-bag-pickup active: forcing a real pickup attempt at tile "
                       "(%d,%d) (item=0x%04X)\n",
                       ut_x, ut_z, (unsigned)value);
                pc_net_game_request_pickup(ut_x, ut_z, (mActor_name_t)value);
                break;
            }
        }
        /* No matching tile found yet (the host's hit may not have landed/been seeded yet) -- retried
         * every poll until s_pickup_done, no timeout: test-only code where waiting costs nothing, same
         * reasoning as pcnetgame_run_pickup_test_seed()'s own doc. */
    }
}

/* P1 (World Ecology T-dig) real-gameplay verification, TEST-ONLY: fires --force-dig-hole exactly once
 * -- see pc_platform.h's doc comment on g_pc_force_dig_hole. Mirrors
 * pcnetgame_run_money_rock_test_triggers()'s own exact gating pattern (CLIENT-only, retried every poll
 * with no timeout until it succeeds, same reasoning as that function's own doc).
 *
 * Unlike every other test trigger in this file, this one does not call a pc_net_game_*() seam directly
 * -- it calls PC_Test_ForceRequestDigScoop() (m_player.c/m_player_lib.h), a thin pass-through to the
 * REAL, unmodified Player_actor_request_main_dig_scoop_all(). Everything from there on (the real
 * dig-scoop animation state machine, and -- once it reaches its real commit point -- the real
 * pc_net_game_request_dig_hole() network seam m_player_main_dig_scoop.c_inc's own P1 wiring calls) runs
 * via the ordinary, unmodified per-frame player update; this function's only job is the one-time
 * request. Targets the --field-action-test-seed fixture's own DIG_HOLE tile (56,105) -- requires
 * --field-action-test-seed too, same precondition pc_net_game_host_local_money_rock_hit()'s own trigger
 * has on its money-rock fixture.
 *
 * Stage 0 first teleports the local player's own actor next to the fixture tile. This is NOT part of
 * the dig-scoop request/commit chain being verified -- it only satisfies
 * pcnetgame_fa_validate_adapter_dig_hole()'s own, unmodified pcnetgame_field_action_reach_check() call,
 * which (correctly) trusts only this client's own last-synced MOVE position
 * (pc_remote_player_get_last_position()) and rejects a request from anywhere else, exactly as it must
 * for a real player. Only x/z are moved; y is left for the game's own ground collision to resolve on
 * the next frame, same as any other teleport-style warp -- and the reach check itself only ever
 * compares y to itself (see pcnetgame_field_action_reach_check()'s own math), so this is harmless to
 * the check either way. Stage 1 then waits ~1.5s (comfortably more than
 * PC_NETGAME_MOVE_SEND_PERIOD_60FPS_FRAMES) for the throttled MOVE send to actually carry the new
 * position to the host before requesting -- otherwise the host would reach-check against the player's
 * stale pre-teleport position and (correctly) reject. */
static void pcnetgame_run_dig_hole_test_trigger(void) {
    static int s_stage = 0; /* 0 = not yet teleported, 1 = teleported/waiting for move-sync, 2 = done */
    static int s_wait_frames = 0;
    xyz_t pos;

    if (!g_pc_force_dig_hole || s_stage >= 2 || gamePT == NULL) {
        return;
    }
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY || !s_local_world_latched) {
        return;
    }
    if (!pcfa_scene_is_town()) {
        return;
    }

    /* mFI_UtNum2CenterWpos() ignores pos.y (see its own definition) -- mFI_Wpos2UtNum(), the function
       the real commit point uses to recover (ut_x, ut_z) from this position, only ever reads x/z. */
    if (!mFI_UtNum2CenterWpos(&pos, 56, 105)) {
        return; /* out of range -- should never happen for this fixed fixture tile */
    }

    if (s_stage == 0) {
        PLAYER_ACTOR* local = GET_PLAYER_ACTOR_NOW();
        if (!pcnetgame_is_real_player_actor(local)) {
            return; /* no local save/actor yet -- retried next poll */
        }
        local->actor_class.world.position.x = pos.x;
        local->actor_class.world.position.z = pos.z;
        printf("[NET][FIELD_ACTION] --force-dig-hole: teleported the local player to tile (56,105) so the "
               "host's real reach check will accept the upcoming request\n");
        s_stage = 1;
        s_wait_frames = 0;
        return;
    }

    /* s_stage == 1 */
    s_wait_frames++;
    if (s_wait_frames < 90) {
        return;
    }

    printf("[NET][FIELD_ACTION] --force-dig-hole active: forcing a real Player_actor_request_main_dig_scoop_all() "
           "request at tile (56,105)\n");
    if (PC_Test_ForceRequestDigScoop(gamePT, &pos, (mActor_name_t)EMPTY_NO)) {
        s_stage = 2;
    }
    /* else: rejected by Player_actor_check_request_main_able() -- e.g. a higher-priority main index is
       already pending, or a reset is in progress. Not fatal: retried every poll, still at stage 1, same
       reasoning as every other test-only trigger in this file. */
}

/* World Ecology Wildlife Sync T1 real-gameplay verification, TEST-ONLY: --force-wildlife-trigger.
 * CLIENT-only, fires exactly once, mirroring pcnetgame_run_dig_hole_test_trigger()'s own established
 * pattern (this project's proven convention for reaching a real gameplay path that is impractical to
 * drive via blind keyboard navigation -- see g_pc_force_dig_hole's own doc, pc_platform.h). Unlike
 * dig-hole, no teleport/reach-check/wait is needed here: calls the REAL, unmodified client-side
 * network seam pc_net_game_request_wildlife_spawn_trigger() directly, once per acre across a fixed
 * burst of 10 different addressable acres in a single frame (see s_acres[] below -- NOT a single
 * fixed acre; a lone acre's decision may legitimately roll "nothing spawns", which would make this
 * test trigger unreliable for verification purposes) -- the EXACT same function a real
 * aSetMgr_move_set()/mFI_CheckPlayerWade() wade-entry sequence calls (ac_set_manager.c), carrying no
 * species/position/RNG result of its own (see that
 * message's own doc, PCNetGameWildlifeSpawnTriggerRequestMsg). From there on -- the host's spawn
 * decision (pcwld_host_spawn_trigger()), the host's own local presentation actor, the
 * WILDLIFE_SPAWN broadcast, and this client's own presentation actor on receipt
 * (pcnetgame_handle_client_wildlife_spawn() -> pcwld_presentation_create()) -- everything is the
 * real, unmodified, already-shipping code path. This bypasses ONLY the real wade/water-tile
 * detection (walking a real player actor into water) -- the same class of bypass --force-dig-hole
 * already uses for "walk up and swing a shovel" -- never any part of the spawn decision, broadcast,
 * or presentation logic itself. */
static void pcnetgame_run_wildlife_trigger_test_trigger(void) {
    /* Several addressable acres, not just one: the underlying vanilla decision may legitimately
     * decide "nothing spawns" for any single acre (an entirely ordinary outcome, same as a real
     * wade that rolls nothing -- see pc_wildlife_authority.c's own doc), so triggering only one acre
     * is not a reliable way to observe an actual spawn for verification purposes. Mirrors
     * test_wildlife_spawn_wire.py's own Test W3 approach (many acres in one pass) rather than
     * inventing a new fake-decision shortcut. */
    static const int s_acres[][2] = { { 1, 1 }, { 2, 2 }, { 3, 3 }, { 4, 4 }, { 5, 5 },
                                       { 2, 4 }, { 3, 5 }, { 1, 6 }, { 5, 6 }, { 4, 2 } };
    static int s_done = 0;
    static int s_wait_frames = 0;

    if (!g_pc_force_wildlife_trigger || s_done) {
        return;
    }
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY || !s_local_world_latched) {
        return;
    }
    if (!pcfa_scene_is_town()) {
        return;
    }
    /* A real wade always happens well after a player has finished joining; firing this
     * synthetic trigger on the very first eligible poll can race the host's own initial full-world
     * SNAPSHOT_BEGIN/FIELD_BLOCK/SNAPSHOT_END send to this peer, during which
     * pcnetgame_broadcast_villager_msg() (the same broadcast helper WILDLIFE_SPAWN uses) correctly,
     * silently skips a peer with snap_active still set -- exactly like it would for ANY other
     * host-broadcast message mid-snapshot, not a wildlife-specific bug. Waiting a couple of seconds
     * avoids racing this test harness against that ordinary, unrelated snapshot window. */
    if (s_wait_frames < 180) {
        s_wait_frames++;
        return;
    }

    {
        size_t i;
        for (i = 0; i < sizeof(s_acres) / sizeof(s_acres[0]); i++) {
            printf("[NET][WILDLIFE] --force-wildlife-trigger active: forcing a real "
                   "pc_net_game_request_wildlife_spawn_trigger() at acre (%d,%d)\n",
                   s_acres[i][0], s_acres[i][1]);
            pc_net_game_request_wildlife_spawn_trigger(s_acres[i][0], s_acres[i][1]);
        }
    }
    s_done = 1;
}

/* World Ecology Wildlife Sync T-catch real-gameplay verification, TEST-ONLY: --force-fish-catch.
 * TEST-ONLY duplicate of aUKI_get_fish_type()'s own fish_data[] table (ac_uki_move.c_inc, `static` in a
 * different translation unit) -- same duplication precedent already established for MONEY_ROCK_HIT/
 * TREE_SHAKE/CHOP/SNOWMAN's own vanilla-table copies in this file. Used ONLY here, to compute the item
 * this test trigger should expect to see granted -- NEVER used by any production code path (the real
 * client-side seam always uses the REAL uki->get_fish_type_proc(), and the real host-side validation
 * never derives an item at all, only a species match -- see PCNetGameCatchResultMsg's own doc for why).
 * Kept in sync BY HAND with aUKI_get_fish_type() if that table ever changes. */
static mActor_name_t pcnetgame_test_fish_species_to_item(int species) {
    static const mActor_name_t fish_data[] = {
        ITM_FISH00, ITM_FISH01, ITM_FISH02, ITM_FISH03, ITM_FISH04, ITM_FISH05, ITM_FISH06, ITM_FISH07,
        ITM_FISH08, ITM_FISH09, ITM_FISH10, ITM_FISH11, ITM_FISH12, ITM_FISH13, ITM_FISH14, ITM_FISH15,
        ITM_FISH16, ITM_FISH17, ITM_FISH18, ITM_FISH19, ITM_FISH20, ITM_FISH21, ITM_FISH22, ITM_FISH23,
        ITM_FISH24, ITM_FISH25, ITM_FISH26, ITM_FISH27, ITM_FISH28, ITM_FISH29, ITM_FISH30, ITM_FISH31,
        ITM_FISH32, ITM_FISH33, ITM_FISH34, ITM_FISH35, ITM_FISH36, ITM_FISH37, ITM_FISH38, ITM_FISH39,
        ITM_FISH39, ITM_DUST0_EMPTY_CAN, ITM_DUST1_BOOT, ITM_DUST2_OLD_TIRE, ITM_FISH22,
    };
    if (species >= 0 && species < (int)(sizeof(fish_data) / sizeof(fish_data[0]))) {
        return fish_data[species];
    }
    return (mActor_name_t)EMPTY_NO;
}

/* World Ecology Wildlife Sync T4 real-gameplay verification, TEST-ONLY: --force-bug-catch. Unlike
 * pcnetgame_test_fish_species_to_item() above, this is NOT a duplicate of a hidden vanilla table: an
 * ordinary bug's item is read straight off its own aINS_INSECT_ACTOR::item field at catch time (see
 * Player_actor_setup_main_Notice_net(), m_player_main_notice_net.c_inc), never derived from insect_type
 * alone. m_name_table.h's own ITM_INSECT00..ITM_INSECT39 constants are, however, a direct linear mapping
 * of enum insect_type's own 0..39 ordering (ac_insect_h.h) -- confirmed by inspection, not merely
 * assumed -- so this table is a reasonable, clearly-labeled TEST-ONLY stand-in used only to compute the
 * item this test trigger should expect to see granted; NEVER used by any production code path. Index 38
 * (aINS_INSECT_TYPE_ANT) is never reached here in practice: the host trigger below skips ANT records
 * entirely (see s_force_catch_last_bug_entity_id's own doc) and pcnetgame_validate_and_commit_catch()
 * would reject any claim against one regardless. */
static mActor_name_t pcnetgame_test_bug_species_to_item(int species) {
    static const mActor_name_t insect_data[] = {
        ITM_INSECT00, ITM_INSECT01, ITM_INSECT02, ITM_INSECT03, ITM_INSECT04, ITM_INSECT05, ITM_INSECT06,
        ITM_INSECT07, ITM_INSECT08, ITM_INSECT09, ITM_INSECT10, ITM_INSECT11, ITM_INSECT12, ITM_INSECT13,
        ITM_INSECT14, ITM_INSECT15, ITM_INSECT16, ITM_INSECT17, ITM_INSECT18, ITM_INSECT19, ITM_INSECT20,
        ITM_INSECT21, ITM_INSECT22, ITM_INSECT23, ITM_INSECT24, ITM_INSECT25, ITM_INSECT26, ITM_INSECT27,
        ITM_INSECT28, ITM_INSECT29, ITM_INSECT30, ITM_INSECT31, ITM_INSECT32, ITM_INSECT33, ITM_INSECT34,
        ITM_INSECT35, ITM_INSECT36, ITM_INSECT37, ITM_INSECT38, ITM_INSECT39,
    };
    if (species >= 0 && species < (int)(sizeof(insect_data) / sizeof(insect_data[0]))) {
        return insect_data[species];
    }
    return (mActor_name_t)EMPTY_NO;
}

/* HOST-only half of --force-fish-catch: see g_pc_force_fish_catch's own doc (pc_platform.h) for the
 * full rationale (fishing's own cast/float/bite/hook state machine is judged too complex/non-
 * deterministic for a simple force-flag, unlike a single dig-scoop request or wade-trigger send).
 * Waits for at least one live FISH record in the authoritative table (typically seeded by
 * --force-wildlife-trigger run on a peer, or an ordinary real wade by any connected player), then calls
 * the REAL pc_net_game_host_local_wildlife_catch() directly with that record's own species -- the exact
 * function the real Player_actor_setup_main_Notice_rod() seam calls for the host's own local catch --
 * and, on accept, grants the item via the REAL mPr_SetFreePossessionItem(), exactly mirroring what that
 * seam does immediately afterward. Fires exactly once. */
static void pcnetgame_run_fish_catch_test_trigger_host(void) {
    static int s_done = 0;
    int i;

    if (!g_pc_force_fish_catch || s_done || s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!g_pc_authoritative_wildlife || !s_host_world_ready || !pcfa_scene_is_town() || Now_Private == NULL) {
        return;
    }

    for (i = 0; i < PCWLD_PUBLIC_MAX_ENTITIES; i++) {
        PcWildlifeRecord rec;
        if (!pcwld_get_by_slot(i, &rec) || rec.kind != PC_WILDLIFE_KIND_FISH) {
            continue;
        }

        printf("[NET][WILDLIFE] --force-fish-catch active: forcing a real "
               "pc_net_game_host_local_wildlife_catch() for entity %u (species %d)\n",
               (unsigned)rec.entity_id, rec.species);
        if (pc_net_game_host_local_wildlife_catch(rec.entity_id, rec.species)) {
            mActor_name_t item = pcnetgame_test_fish_species_to_item(rec.species);
            if (mPr_SetFreePossessionItem(Now_Private, item, mPr_ITEM_COND_NORMAL)) {
                printf("[NET][WILDLIFE] --force-fish-catch (host): entity %u accepted -- item 0x%04X "
                       "granted\n",
                       (unsigned)rec.entity_id, (unsigned)item);
            } else {
                printf("[NET][WILDLIFE] --force-fish-catch (host): entity %u accepted but no free "
                       "pocket slot -- item LOST\n",
                       (unsigned)rec.entity_id);
            }
        } else {
            printf("[NET][WILDLIFE] --force-fish-catch (host): entity %u rejected\n",
                   (unsigned)rec.entity_id);
        }

        /* Residual review fix, TEST-ONLY: immediately attempt a SECOND host-local catch on the exact
         * SAME entity_id, which pcnetgame_validate_and_commit_catch() must now reject -- the accepted
         * attempt just above already removed it from the authoritative table (or, if that first attempt
         * was itself somehow rejected, entity_id was never live to begin with, and this second call is
         * rejected for the same reason). This exercises the IDENTICAL rejection code path a genuine
         * two-peer race produces (pcnetgame_validate_and_commit_catch() has no notion of "which peer" --
         * only "is this entity_id still present"), fully deterministically, without depending on real
         * cross-process timing -- then queries pc_net_game_query_catch_outcome() for the SAME entity_id,
         * printing the result so an automated test can confirm the exact status the putaway-rod
         * exchange-screen gate (m_player_main_putaway_rod.c_inc) would have read: REJECTED (3), never
         * ACCEPTED (2). Safe to consume here (query_catch_outcome() clears a resolved entry once read --
         * see its own doc) since --force-fish-catch never drives this entity_id through the real
         * Notice_rod/Putaway_rod seam at all. */
        {
            int second_accepted = pc_net_game_host_local_wildlife_catch(rec.entity_id, rec.species);
            int outcome = pc_net_game_query_catch_outcome(rec.entity_id);
            printf("[NET][WILDLIFE] --force-fish-catch (host): SECOND local catch attempt on the SAME "
                   "entity %u %s (as expected) -- query_catch_outcome=%d\n",
                   (unsigned)rec.entity_id, second_accepted ? "was ACCEPTED (unexpected!)" : "was REJECTED",
                   outcome);
        }

        s_done = 1;
        return;
    }
}

/* CLIENT-only half of --force-fish-catch: see g_pc_force_fish_catch's own doc (pc_platform.h). Waits
 * for this client to have observed at least one WILDLIFE_SPAWN broadcast (or late-join snapshot entry)
 * for a FISH entity (s_force_catch_last_fish_*, set unconditionally by
 * pcnetgame_handle_client_wildlife_spawn()/pcnetgame_handle_client_wildlife_snapshot_entry()), teleports
 * the local player to that entity's own recorded position -- mirroring
 * pcnetgame_run_dig_hole_test_trigger()'s own established teleport-then-wait-for-move-sync pattern
 * exactly (see that function's own doc for why the wait is needed: the host must see this client's NEW
 * position via an ordinary MOVE before a CATCH_REQUEST arrives, or the real reach check would correctly
 * reject it against the stale pre-teleport position) -- then calls the REAL
 * pc_net_game_request_catch_fish() network seam directly with that entity's own species and its own
 * correct item (pcnetgame_test_fish_species_to_item() above). The REAL
 * pcnetgame_handle_client_catch_result() then grants the item on accept, exactly as a genuine catch
 * would; nothing about the reach check, host validation, or RESULT-handling path is bypassed -- only the
 * requirement that a real UKI (fishing rod/bobber) actor be cast, floated, and bitten first, and the
 * requirement that the player physically walk to the fish. Fires exactly once. */
static void pcnetgame_run_fish_catch_test_trigger_client(void) {
    static int s_stage = 0; /* 0 = not yet teleported, 1 = teleported/waiting for move-sync,
                                2 = request sent/waiting for CATCH_RESULT, 3 = done */
    static int s_wait_frames = 0;
    static int s_result_wait_frames = 0;
    /* Latched at stage 0 -- deliberately NOT re-read from s_force_catch_last_fish_* at fire time: this
       shared bookkeeping can legitimately be overwritten by a LATER, unrelated WILDLIFE_SPAWN/SNAPSHOT_
       ENTRY while this trigger is still waiting out its move-sync delay (e.g. a real ambient wade by any
       connected player), which would otherwise fire the eventual CATCH_REQUEST for a DIFFERENT entity
       than the one the player was just teleported next to -- a self-inflicted, test-only reach-check
       failure, not a real bug in the production catch path itself. */
    static uint32_t s_latched_entity_id = 0;
    static int      s_latched_species = 0;

    if (!g_pc_force_fish_catch || s_stage >= 3 || s_role != PC_NETGAME_ROLE_CLIENT ||
        s_client_link != PC_NETGAME_LINK_READY || gamePT == NULL) {
        return;
    }
    if (!g_pc_authoritative_wildlife || !pcfa_scene_is_town() || s_force_catch_last_fish_entity_id == 0) {
        return;
    }

    if (s_stage == 0) {
        PLAYER_ACTOR* local = GET_PLAYER_ACTOR_NOW();
        if (!pcnetgame_is_real_player_actor(local)) {
            return; /* no local save/actor yet -- retried next poll */
        }
        s_latched_entity_id = s_force_catch_last_fish_entity_id;
        s_latched_species = s_force_catch_last_fish_species;
        local->actor_class.world.position.x = s_force_catch_last_fish_x;
        local->actor_class.world.position.z = s_force_catch_last_fish_z;
        printf("[NET][WILDLIFE] --force-fish-catch: teleported the local player to entity %u's own "
               "recorded position (%.1f,%.1f) so the host's real reach check will accept the upcoming "
               "request\n",
               (unsigned)s_latched_entity_id, s_force_catch_last_fish_x, s_force_catch_last_fish_z);
        s_stage = 1;
        s_wait_frames = 0;
        return;
    }

    if (s_stage == 1) {
        s_wait_frames++;
        /* 240 frames (~4s @ 60fps -- bumped up from the original 90/~1.5s move-sync-only budget):
           residual review fix's own TEST H (test_wildlife_catch_exchange_gate.py) needs a real wall-clock
           window wide enough for a separate FakeClient process to win a race against this exact request,
           which the original 90-frame budget (sized only for the move-sync wait this trigger shares with
           pcnetgame_run_dig_hole_test_trigger()'s own precedent) left too tight to reliably automate.
           TEST-ONLY (--force-fish-catch gates this whole function) -- never reachable from real
           gameplay. */
        if (s_wait_frames < 240) {
            return;
        }

        {
            mActor_name_t item = pcnetgame_test_fish_species_to_item(s_latched_species);

            printf("[NET][WILDLIFE] --force-fish-catch active: forcing a real "
                   "pc_net_game_request_catch_fish() for entity %u (species %d, item 0x%04X)\n",
                   (unsigned)s_latched_entity_id, s_latched_species, (unsigned)item);
            pc_net_game_request_catch_fish(s_latched_entity_id, s_latched_species, (int)item);
        }
        s_stage = 2;
        s_result_wait_frames = 0;
        return;
    }

    /* s_stage == 2: residual review fix, TEST-ONLY -- give pcnetgame_handle_client_catch_result() up to
     * ~2 seconds (120 frames @ 60fps) to process the CATCH_RESULT this request should trigger, then query
     * pc_net_game_query_catch_outcome() for the SAME entity_id and print the result, so an automated test
     * can confirm the exact status the putaway-rod exchange-screen gate (m_player_main_putaway_rod.c_inc)
     * would have read for this catch: ACCEPTED (2) for an ordinary unraced catch, or REJECTED (3) if a
     * FakeClient (or another real peer) claimed the SAME entity_id first. Safe to consume here (see that
     * function's own doc) since --force-fish-catch never drives this entity_id through the real
     * Notice_rod/Putaway_rod seam at all. */
    s_result_wait_frames++;
    if (s_result_wait_frames < 120) {
        return;
    }
    {
        int outcome = pc_net_game_query_catch_outcome(s_latched_entity_id);
        printf("[NET][WILDLIFE] --force-fish-catch (client): query_catch_outcome(entity %u) after "
               "CATCH_RESULT wait = %d\n",
               (unsigned)s_latched_entity_id, outcome);
    }
    s_stage = 3;
}

/* HOST-only half of --force-bug-catch: faithful mirror of pcnetgame_run_fish_catch_test_trigger_host()
 * above, adapted for ordinary (non-ant) BUG catching -- see g_pc_force_bug_catch's own doc (pc_platform.h)
 * for the full rationale. Waits for at least one live, non-ant BUG record in the authoritative table
 * (typically seeded by a real WILDLIFE_SPAWN_TRIGGER_REQUEST burst, exactly like --force-fish-catch's own
 * precedent), then calls the REAL pc_net_game_host_local_wildlife_catch() directly with that record's own
 * species -- the exact function the real Player_actor_setup_main_Notice_net() seam calls for the host's
 * own local catch -- and, on accept, grants the item via the REAL mPr_SetFreePossessionItem(), exactly
 * mirroring what that seam does immediately afterward. Fires exactly once. */
static void pcnetgame_run_bug_catch_test_trigger_host(void) {
    static int s_done = 0;
    int i;

    if (!g_pc_force_bug_catch || s_done || s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!g_pc_authoritative_wildlife || !s_host_world_ready || !pcfa_scene_is_town() || Now_Private == NULL) {
        return;
    }

    for (i = 0; i < PCWLD_PUBLIC_MAX_ENTITIES; i++) {
        PcWildlifeRecord rec;
        if (!pcwld_get_by_slot(i, &rec) || rec.kind != PC_WILDLIFE_KIND_BUG) {
            continue;
        }
        if (rec.species == aINS_INSECT_TYPE_ANT) {
            continue; /* never a legitimate ordinary catch target -- see s_force_catch_last_bug_entity_id's
                         own doc; skip and keep looking rather than wasting the one-shot fire */
        }

        printf("[NET][WILDLIFE] --force-bug-catch active: forcing a real "
               "pc_net_game_host_local_wildlife_catch() for entity %u (species %d)\n",
               (unsigned)rec.entity_id, rec.species);
        if (pc_net_game_host_local_wildlife_catch(rec.entity_id, rec.species)) {
            mActor_name_t item = pcnetgame_test_bug_species_to_item(rec.species);
            if (mPr_SetFreePossessionItem(Now_Private, item, mPr_ITEM_COND_NORMAL)) {
                printf("[NET][WILDLIFE] --force-bug-catch (host): entity %u accepted -- item 0x%04X "
                       "granted\n",
                       (unsigned)rec.entity_id, (unsigned)item);
            } else {
                printf("[NET][WILDLIFE] --force-bug-catch (host): entity %u accepted but no free "
                       "pocket slot -- item LOST\n",
                       (unsigned)rec.entity_id);
            }
        } else {
            printf("[NET][WILDLIFE] --force-bug-catch (host): entity %u rejected\n",
                   (unsigned)rec.entity_id);
        }

        /* Mirrors pcnetgame_run_fish_catch_test_trigger_host()'s own residual review fix exactly: a
         * SECOND host-local catch attempt on the exact SAME entity_id, deterministically exercising the
         * identical rejection code path a genuine two-peer race produces, then queries
         * pc_net_game_query_catch_outcome() for the SAME entity_id -- see that function's own doc for why
         * this is safe to consume here. */
        {
            int second_accepted = pc_net_game_host_local_wildlife_catch(rec.entity_id, rec.species);
            int outcome = pc_net_game_query_catch_outcome(rec.entity_id);
            printf("[NET][WILDLIFE] --force-bug-catch (host): SECOND local catch attempt on the SAME "
                   "entity %u %s (as expected) -- query_catch_outcome=%d\n",
                   (unsigned)rec.entity_id, second_accepted ? "was ACCEPTED (unexpected!)" : "was REJECTED",
                   outcome);
        }

        s_done = 1;
        return;
    }
}

/* CLIENT-only half of --force-bug-catch: faithful mirror of pcnetgame_run_fish_catch_test_trigger_client()
 * above, adapted for ordinary (non-ant) BUG catching -- see g_pc_force_bug_catch's own doc (pc_platform.h).
 * Waits for this client to have observed at least one WILDLIFE_SPAWN broadcast (or late-join snapshot
 * entry) for a non-ant BUG entity (s_force_catch_last_bug_*, set unconditionally by
 * pcnetgame_handle_client_wildlife_spawn()/pcnetgame_handle_client_wildlife_snapshot_entry()), teleports
 * the local player to that entity's own recorded position -- reusing pcnetgame_run_fish_catch_test_
 * trigger_client()'s own established teleport-then-wait-for-move-sync pattern and its own 240-frame wait
 * budget verbatim (the same MOVE-then-CATCH_REQUEST race-timing concern that budget was tuned for applies
 * identically here -- nothing about bug catching makes that race window wider or narrower) -- then calls
 * the REAL pc_net_game_request_catch_bug() network seam directly with that entity's own species and its
 * own correct item (pcnetgame_test_bug_species_to_item() above). The REAL
 * pcnetgame_handle_client_catch_result() then grants the item on accept, exactly as a genuine catch would.
 * Fires exactly once per role. */
static void pcnetgame_run_bug_catch_test_trigger_client(void) {
    static int s_stage = 0; /* 0 = not yet teleported, 1 = teleported/waiting for move-sync,
                                2 = request sent/waiting for CATCH_RESULT, 3 = done */
    static int s_wait_frames = 0;
    static int s_result_wait_frames = 0;
    static uint32_t s_latched_entity_id = 0;
    static int      s_latched_species = 0;

    if (!g_pc_force_bug_catch || s_stage >= 3 || s_role != PC_NETGAME_ROLE_CLIENT ||
        s_client_link != PC_NETGAME_LINK_READY || gamePT == NULL) {
        return;
    }
    if (!g_pc_authoritative_wildlife || !pcfa_scene_is_town() || s_force_catch_last_bug_entity_id == 0) {
        return;
    }

    if (s_stage == 0) {
        PLAYER_ACTOR* local = GET_PLAYER_ACTOR_NOW();
        if (!pcnetgame_is_real_player_actor(local)) {
            return; /* no local save/actor yet -- retried next poll */
        }
        s_latched_entity_id = s_force_catch_last_bug_entity_id;
        s_latched_species = s_force_catch_last_bug_species;
        local->actor_class.world.position.x = s_force_catch_last_bug_x;
        local->actor_class.world.position.z = s_force_catch_last_bug_z;
        printf("[NET][WILDLIFE] --force-bug-catch: teleported the local player to entity %u's own "
               "recorded position (%.1f,%.1f) so the host's real reach check will accept the upcoming "
               "request\n",
               (unsigned)s_latched_entity_id, s_force_catch_last_bug_x, s_force_catch_last_bug_z);
        s_stage = 1;
        s_wait_frames = 0;
        return;
    }

    if (s_stage == 1) {
        s_wait_frames++;
        /* 240 frames -- same budget --force-fish-catch's own client trigger already established (see
           that function's own doc for the full rationale); reused verbatim, not re-derived, since the
           underlying race-timing concern is identical. TEST-ONLY (--force-bug-catch gates this whole
           function) -- never reachable from real gameplay. */
        if (s_wait_frames < 240) {
            return;
        }

        {
            mActor_name_t item = pcnetgame_test_bug_species_to_item(s_latched_species);

            printf("[NET][WILDLIFE] --force-bug-catch active: forcing a real "
                   "pc_net_game_request_catch_bug() for entity %u (species %d, item 0x%04X)\n",
                   (unsigned)s_latched_entity_id, s_latched_species, (unsigned)item);
            pc_net_game_request_catch_bug(s_latched_entity_id, s_latched_species, (int)item);
        }
        s_stage = 2;
        s_result_wait_frames = 0;
        return;
    }

    /* s_stage == 2: mirrors pcnetgame_run_fish_catch_test_trigger_client()'s own residual review fix --
     * give pcnetgame_handle_client_catch_result() up to ~2 seconds (120 frames @ 60fps) to process the
     * CATCH_RESULT this request should trigger, then query pc_net_game_query_catch_outcome() for the SAME
     * entity_id and print the result, so an automated test can confirm the exact status the putaway-net
     * exchange-screen gate (m_player_main_putaway_net.c_inc) would have read for this catch. */
    s_result_wait_frames++;
    if (s_result_wait_frames < 120) {
        return;
    }
    {
        int outcome = pc_net_game_query_catch_outcome(s_latched_entity_id);
        printf("[NET][WILDLIFE] --force-bug-catch (client): query_catch_outcome(entity %u) after "
               "CATCH_RESULT wait = %d\n",
               (unsigned)s_latched_entity_id, outcome);
    }
    s_stage = 3;
}

/* T8 audit verification, TEST-ONLY: --diag-bug-ttl-lookup <frames>. See its own doc, pc_platform.h, for
 * the full rationale (re-verifying Bug 1's fix through the REAL pc_net_game_bug_entity_id_for_label() ->
 * pcwld_bug_entity_id_for_local_actor() lookup, which --force-bug-catch's host branch never calls). HOST
 * role only -- there is no client-side equivalent of this diagnostic, since the property under test
 * (a bug's own s_presentation[] mapping surviving past the idle-expiry threshold while its actor is
 * alive) is purely local, per-process bookkeeping identical on host and client alike (see pc_wildlife_
 * authority.c's own top-of-file doc); the host is simply the easier role to seed a live BUG entity on
 * without a second process. */
static void pcnetgame_run_bug_ttl_lookup_diag_host(void) {
    static int      s_ttl_applied  = 0;
    static uint32_t s_latched_id   = 0;
    static int      s_latched_species = 0;
    static void*    s_latched_actor   = NULL;
    static int      s_poll_frames     = 0;
    static int      s_done            = 0;

    if (g_pc_diag_bug_ttl_lookup_frames <= 0 || s_done || s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!g_pc_authoritative_wildlife || !s_host_world_ready || !pcfa_scene_is_town()) {
        return;
    }

    if (!s_ttl_applied) {
        /* Apply the override exactly once, as early as possible -- before any bug we later latch onto
           has a chance to accumulate real age under the normal ~10-minute constant. */
        pcwld_test_set_ttl_override_frames((float)g_pc_diag_bug_ttl_lookup_frames);
        printf("[NET][WILDLIFE] --diag-bug-ttl-lookup active: idle-expiry TTL overridden to %d frames "
               "(~%.1fs @ 60fps) for this test run\n",
               g_pc_diag_bug_ttl_lookup_frames, (double)g_pc_diag_bug_ttl_lookup_frames / 60.0);
        s_ttl_applied = 1;
    }

    if (s_latched_id == 0) {
        int i;
        for (i = 0; i < PCWLD_PUBLIC_MAX_ENTITIES; i++) {
            PcWildlifeRecord rec;
            void* actor;
            int species;

            if (!pcwld_get_by_slot(i, &rec) || rec.kind != PC_WILDLIFE_KIND_BUG) {
                continue;
            }
            if (rec.species == aINS_INSECT_TYPE_ANT) {
                continue; /* ants are never given a local presentation actor (T1 scope) -- see
                             pcwld_presentation_create()'s own doc; pcwld_bug_local_actor_for_entity()
                             would just return NULL for one, uninteresting for this diagnostic */
            }
            actor = pcwld_bug_local_actor_for_entity(rec.entity_id, &species);
            if (actor == NULL) {
                continue; /* not locally materialized on THIS process (e.g. presentation queue was full) */
            }
            s_latched_id      = rec.entity_id;
            s_latched_species = species;
            s_latched_actor   = actor;
            printf("[NET][WILDLIFE] --diag-bug-ttl-lookup: latched entity %u (species %d, local_actor "
                   "%p) -- will re-query pc_net_game_bug_entity_id_for_label() every ~0.5s past the "
                   "overridden TTL\n",
                   (unsigned)s_latched_id, s_latched_species, s_latched_actor);
            break;
        }
        return; /* first bug found this poll, or none yet -- either way, start querying next poll */
    }

    s_poll_frames++;
    if (s_poll_frames % 30 != 0) {
        return; /* ~0.5s at 60fps -- frequent enough to observe the TTL boundary, sparse enough to read */
    }

    {
        uint32_t looked_up = pc_net_game_bug_entity_id_for_label(s_latched_actor, s_latched_species);
        printf("[NET][WILDLIFE] --diag-bug-ttl-lookup: t+%.1fs pc_net_game_bug_entity_id_for_label("
               "local_actor=%p, species=%d) = %u (expected %u if Bug 1's fix holds; 0 would mean the "
               "mapping was lost while the actor is still alive -- the exact bug the audit found)\n",
               (double)s_poll_frames / 60.0, s_latched_actor, s_latched_species, (unsigned)looked_up,
               (unsigned)s_latched_id);

        /* Run for 6x the overridden TTL so the log clearly shows the lookup surviving well past the
           point the pre-fix code would have dropped it, not just barely past the threshold once. */
        if (s_poll_frames >= g_pc_diag_bug_ttl_lookup_frames * 6) {
            printf("[NET][WILDLIFE] --diag-bug-ttl-lookup: diagnostic window complete (entity %u)\n",
                   (unsigned)s_latched_id);
            s_done = 1;
        }
    }
}

/* T8 review fix verification, TEST-ONLY: --diag-bug-despawn-label-race. See its own doc, pc_platform.h,
 * for the full rationale and the exact regression this reproduces (Bug 2 part (b)'s original "clear
 * local_actor immediately on despawn" change, since reverted -- see pcwld_bug_handle_wildlife_despawn()'s
 * own doc, pc_wildlife_authority.c). HOST role only. Driven together with a second peer's raced
 * CATCH_REQUEST for the SAME entity_id (test_wildlife_bug_catch.py's own TEST for this flag sends it),
 * since this diagnostic only forces the LABEL side of the race -- the actual despawn still has to arrive
 * from a genuine competing catch, exactly like a real two-peer race would produce one. */
static void pcnetgame_run_bug_despawn_label_race_diag(void) {
    static int      s_stage         = 0; /* 0 = find+latch+teleport+force label, 1 = settle wait, 2 =
                                             waiting for the raced despawn, 3 = done */
    static uint32_t s_latched_id    = 0;
    static int      s_latched_species = 0;
    static void*    s_latched_actor   = NULL;
    static int      s_settle_frames   = 0;

    if (!g_pc_diag_bug_despawn_label_race || s_stage >= 3 || s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!g_pc_authoritative_wildlife || !s_host_world_ready || !pcfa_scene_is_town()) {
        return;
    }

    if (s_stage == 0) {
        int i;
        PLAYER_ACTOR* local = GET_PLAYER_ACTOR_NOW();

        if (!pcnetgame_is_real_player_actor(local)) {
            return; /* no local save/actor yet -- retried next poll, mirrors --force-bug-catch's own
                       client-trigger precondition check */
        }
        for (i = 0; i < PCWLD_PUBLIC_MAX_ENTITIES; i++) {
            PcWildlifeRecord rec;
            void* actor;
            int species;

            if (!pcwld_get_by_slot(i, &rec) || rec.kind != PC_WILDLIFE_KIND_BUG) {
                continue;
            }
            if (rec.species == aINS_INSECT_TYPE_ANT) {
                continue; /* ants have no local presentation actor and no ordinary net-catch path -- see
                             pcnetgame_validate_and_commit_catch()'s own doc */
            }
            actor = pcwld_bug_local_actor_for_entity(rec.entity_id, &species);
            if (actor == NULL) {
                continue; /* not locally materialized on THIS process */
            }

            s_latched_id      = rec.entity_id;
            s_latched_species = species;
            s_latched_actor   = actor;

            /* Teleport THIS process's own local player right on top of the bug's own recorded position --
             * same safe, already-established teleport pattern pcnetgame_run_bug_catch_test_trigger_client()
             * uses (--force-bug-catch) -- so aINS_cull_check()'s own OTHER two cull rules (not visible on
             * camera + actor_specific==1; too far away in a different acre) both read this actor as
             * "right next to the player" and never call aINS_destruct() on it while this diagnostic's race
             * window is open, keeping exist_flag genuinely TRUE regardless of the label below.
             *
             * ALSO best-effort force this process's own item_net_catch_label onto the exact actor backing
             * entity_id, mirroring the real net-swing assignment (m_player_main_swing_net.c_inc) -- honest
             * caveat: Player_actor_Get_item_net_catch_label() (m_player_common.c_inc) only ever returns a
             * non-zero label while player->now_main_index is one of the four real net states (SWING/PULL/
             * NOTICE/PUTAWAY), which this diagnostic deliberately does NOT force this process's own player
             * into (doing so would drive real per-frame net-animation logic this test hook has no business
             * running) -- so this call is confirmed-by-testing a harmless no-op outside those states, kept
             * here only so the log accurately shows the same call a real catch would make. The actor is
             * kept alive by the teleport above, not by this label -- see this diagnostic's own doc,
             * pc_platform.h, for the full honest-scope note. */
            mPlib_Change_item_net_catch_label((u32)actor, mPlayer_NET_CATCH_TYPE_INSECT);
            local->actor_class.world.position.x = rec.pos_x;
            local->actor_class.world.position.y = rec.pos_y;
            local->actor_class.world.position.z = rec.pos_z;
            printf("[NET][WILDLIFE] --diag-bug-despawn-label-race: latched entity %u (species %d, "
                   "local_actor %p) -- teleported this process's own local player on top of it and forced "
                   "its own item_net_catch_label onto it -- settling briefly before waiting for a raced "
                   "CATCH_REQUEST from another peer for the SAME entity\n",
                   (unsigned)s_latched_id, s_latched_species, s_latched_actor);
            s_stage = 1;
            s_settle_frames = 0;
            return;
        }
        return; /* no eligible bug yet this poll -- retried next poll */
    }

    if (s_stage == 1) {
        /* Half a second @ 60fps -- gives the engine's own per-frame block_x/block_z and camera-distance
           bookkeeping (read by aINS_cull_check()'s own distance/acre rule) a moment to catch up with the
           teleport above before the race is allowed to proceed. */
        s_settle_frames++;
        if (s_settle_frames < 30) {
            return;
        }
        printf("[NET][WILDLIFE] --diag-bug-despawn-label-race: settled -- now waiting for the raced "
               "CATCH_REQUEST\n");
        s_stage = 2;
        return;
    }

    /* s_stage == 2: poll every frame for the raced despawn (driven externally by a second peer's
       CATCH_REQUEST) to actually land -- pcwld_remove_by_id() (inside pcnetgame_validate_and_commit_
       catch()) is what makes this entity_id stop being found, and pcnetgame_commit_catch_despawn() calls
       pcwld_handle_wildlife_despawn() synchronously in that SAME call, on this SAME host process, before
       CATCH_RESULT is ever sent out -- so the instant pcwld_find_by_id() reports it gone, the despawn
       reconciliation (and the property under test) has already happened. */
    {
        PcWildlifeRecord rec;
        int still_present = pcwld_find_by_id(s_latched_id, &rec);
        uint32_t looked_up;
        const aINS_INSECT_ACTOR* insect;

        if (still_present) {
            return; /* no competing catch has landed yet -- keep waiting */
        }

        looked_up = pc_net_game_bug_entity_id_for_label(s_latched_actor, s_latched_species);
        insect = (const aINS_INSECT_ACTOR*)s_latched_actor;
        printf("[NET][WILDLIFE] --diag-bug-despawn-label-race: entity %u despawned by a competing catch "
               "while this process's own label was still active -- pc_net_game_bug_entity_id_for_label("
               "local_actor=%p, species=%d) = %u (expected %u if the fix holds; 0 would mean the mapping "
               "was wiped despite the active label -- the exact regression this diagnostic targets); local "
               "actor exist_flag=%d insect_flags.destruct=%d (both are expected to show the deferred-"
               "destroy shape -- still alive, destruct flag now set -- exactly like ac_insect_move.c_inc's "
               "own aINS_cull_check() label check keeps it alive until the label itself releases)\n",
               (unsigned)s_latched_id, s_latched_actor, s_latched_species, (unsigned)looked_up,
               (unsigned)s_latched_id, (int)insect->exist_flag, (int)insect->insect_flags.destruct);
        printf("[NET][WILDLIFE] --diag-bug-despawn-label-race: RESULT %s\n",
               (looked_up == s_latched_id) ? "PASS" : "FAIL");
        s_stage = 3;
    }
}

/* T8 audit verification, TEST-ONLY: --diag-role-link-state. See its own doc, pc_platform.h. Any role;
 * a complete no-op unless the flag is set. Prints on a plain frame-count throttle (not gated on
 * s_host_world_ready/pcfa_scene_is_town()) so the DISCONNECTED window itself -- which by definition has
 * no live host session to be "world ready" against -- is still visible in the log. */
static void pcnetgame_run_role_link_state_diag(void) {
    static int s_frames = 0;

    if (!g_pc_diag_role_link_state) {
        return;
    }
    s_frames++;
    if (s_frames % 60 != 0) {
        return; /* once per second @ 60fps */
    }
    printf("[NET][DIAG] --diag-role-link-state: role=%d link=%d world_is_host_authoritative()=%d "
           "pcwld_should_suppress_local_wildlife()=%d\n",
           (int)s_role, (int)pc_net_game_client_link_state(), pc_net_game_world_is_host_authoritative(),
           pcwld_should_suppress_local_wildlife());
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

/* World Ecology Stage 1, TEST-ONLY: --field-action-test-seed. Mirrors
 * pcnetgame_run_pickup_test_seed()'s own exact structure/reasoning -- this save's field data has no
 * guaranteed naturally-occurring buried item or money rock, so the DIG_BURIED/MONEY_ROCK_HIT
 * regression tests need a fixture the same way the pickup test does. Seeds exactly two tiles, once
 * each, at fixed coordinates deliberately distinct from --pickup-test-seed's own 30-tile grid (so
 * both flags may be combined without collision): (40, 104) gets a buried ITM_FOOD_APPLE (deposit
 * ON), (24, 104) gets a MONEY_ROCK_A. Never touches gameplay unless explicitly passed. A candidate
 * tile that mFI_UtNumtoFGSet_common() declines (a structure footprint it refuses to overwrite -- it
 * does NOT force-overwrite like pcnetgame_run_pickup_test_seed() does) is simply retried every poll
 * until it succeeds; acceptable for a test-only, off-by-default fixture.
 *
 * World Ecology T1 review follow-up: also seeds three tree fixtures, on this SAME flag (no new CLI
 * flag needed) and the same off-by-default/retry-until-success pattern, since T1's TREE_SHAKE/
 * TREE_CHOP live host+fake-client regression tests have no other way to reach a guaranteed
 * furniture/fruit/bee tree -- (56, 104): TREE_APPLE_FRUIT for the TREE_SHAKE race test, (72, 104): a
 * second, independent TREE_APPLE_FRUIT for the alternating-chop test (kept distinct from the shake
 * tile so the two tests never contend for the same tree/slot), (88, 104): TREE_BEES for the
 * bee-birth-after-felling-chop ordering test -- same z=104 row/acre neighborhood as the dig/rock/other
 * two tree fixtures (all of which land successfully, i.e. that acre row is loaded from bootstrap), one
 * acre column further out than the chop fixture (mFI_UtNumtoFGSet_common() only writes into an
 * ALREADY-LOADED block -- see m_field_info.c -- so a tile in an unloaded acre is silently skipped and
 * retried forever; (8,104)/(8,120) were tried first and never landed, apparently unloaded acres). */
/* World Ecology T-dig review follow-up: extends this SAME flag/pattern (no new CLI flag) with five more
 * fixtures needed by the DIG_HOLE/FILL_HOLE/PITFALL_CONSUME/DIG_SHINE regression tests. Placed at z=105
 * (one tile off the existing z=104 row) directly UNDER the SAME four already-confirmed-loaded acre
 * columns the dig/rock/tree fixtures above use (24/40/56/72) -- every tile within an already-loaded acre
 * loads together, so this avoids the unloaded-acre landing failure this file's own T1 doc already flags
 * for any untried column (see the T1 doc just above): (24,105) a BURIED_PITFALL_HOLE00 fixture (deposit
 * OFF) for the extended DIG_BURIED pitfall-dig-up sub-case test; (40,105) a SECOND, independent
 * BURIED_PITFALL_HOLE00 fixture (deposit OFF) dedicated to the PITFALL_CONSUME race test (kept distinct
 * from (24,105) so the two tests never contend for the same tile); (56,105) EMPTY_NO (explicit,
 * force-set like every other fixture here) for the DIG_HOLE test; (72,105) an existing HOLE_START
 * (deposit OFF) for the FILL_HOLE test; (88,105) a SHINE_SPOT for the DIG_SHINE test. */
static void pcnetgame_run_field_action_test_seed(void) {
    static int s_dig_seed_done = 0;
    static int s_rock_seed_done = 0;
    static int s_tree_shake_seed_done = 0;
    static int s_tree_chop_seed_done = 0;
    static int s_tree_bee_seed_done = 0;
    static int s_pitfall_dig_seed_done = 0;
    static int s_pitfall_consume_seed_done = 0;
    static int s_dig_hole_seed_done = 0;
    static int s_fill_hole_seed_done = 0;
    static int s_dig_shine_seed_done = 0;
    static int s_logged = 0;

    if (!g_pc_field_action_test_seed || s_role != PC_NETGAME_ROLE_HOST || gamePT == NULL || !s_host_world_ready ||
        !pcfa_scene_is_town()) {
        return;
    }
    if (!s_logged) {
        s_logged = 1;
        printf("[NET][FIELD_ACTION] --field-action-test-seed active: seeding a buried item + a money rock + "
               "3 tree fixtures + 5 dig-family fixtures\n");
    }
    if (!s_dig_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)ITM_FOOD_APPLE, 40, 104, TRUE)) {
        mFI_UtNum2DepositON(40, 104);
        s_dig_seed_done = 1;
        printf("[NET][FIELD_ACTION] --field-action-test-seed: buried fixture item placed at tile (40,104)\n");
    }
    if (!s_rock_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)MONEY_ROCK_A, 24, 104, TRUE)) {
        s_rock_seed_done = 1;
        printf("[NET][FIELD_ACTION] --field-action-test-seed: money rock fixture placed at tile (24,104)\n");
    }
    if (!s_tree_shake_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)TREE_APPLE_FRUIT, 56, 104, TRUE)) {
        s_tree_shake_seed_done = 1;
        printf("[NET][FIELD_ACTION] --field-action-test-seed: TREE_APPLE_FRUIT (shake) fixture placed at tile "
               "(56,104)\n");
    }
    if (!s_tree_chop_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)TREE_APPLE_FRUIT, 72, 104, TRUE)) {
        s_tree_chop_seed_done = 1;
        printf("[NET][FIELD_ACTION] --field-action-test-seed: TREE_APPLE_FRUIT (chop) fixture placed at tile "
               "(72,104)\n");
    }
    if (!s_tree_bee_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)TREE_BEES, 88, 104, TRUE)) {
        s_tree_bee_seed_done = 1;
        printf("[NET][FIELD_ACTION] --field-action-test-seed: TREE_BEES fixture placed at tile (88,104)\n");
    }
    if (!s_pitfall_dig_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)BURIED_PITFALL_HOLE00, 24, 105, TRUE)) {
        s_pitfall_dig_seed_done = 1;
        printf("[NET][FIELD_ACTION] --field-action-test-seed: BURIED_PITFALL_HOLE00 (dig-up) fixture placed at "
               "tile (24,105)\n");
    }
    if (!s_pitfall_consume_seed_done &&
        mFI_UtNumtoFGSet_common((mActor_name_t)BURIED_PITFALL_HOLE00, 40, 105, TRUE)) {
        s_pitfall_consume_seed_done = 1;
        printf("[NET][FIELD_ACTION] --field-action-test-seed: BURIED_PITFALL_HOLE00 (consume race) fixture "
               "placed at tile (40,105)\n");
    }
    if (!s_dig_hole_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)EMPTY_NO, 56, 105, TRUE)) {
        s_dig_hole_seed_done = 1;
        printf("[NET][FIELD_ACTION] --field-action-test-seed: EMPTY_NO (DIG_HOLE) fixture placed at tile "
               "(56,105)\n");
    }
    if (!s_fill_hole_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)HOLE_START, 72, 105, TRUE)) {
        s_fill_hole_seed_done = 1;
        printf("[NET][FIELD_ACTION] --field-action-test-seed: HOLE_START (FILL_HOLE) fixture placed at tile "
               "(72,105)\n");
    }
    if (!s_dig_shine_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)SHINE_SPOT, 88, 105, TRUE)) {
        s_dig_shine_seed_done = 1;
        printf("[NET][FIELD_ACTION] --field-action-test-seed: SHINE_SPOT (DIG_SHINE) fixture placed at tile "
               "(88,105)\n");
    }
}

/* World Ecology T3, TEST-ONLY: --bury-test-seed. Mirrors pcnetgame_run_field_action_test_seed()'s own
 * exact structure/reasoning -- this save's field data has no guaranteed naturally-occurring hole/shine
 * tile, and the BURY_REQUEST/RESULT regression tests need one the same way the dig-family tests do.
 * Placed at z=106/107, one/two tiles off the existing z=104/105 rows, directly under the SAME
 * already-confirmed-loaded acre columns (24/40/56/72/88) the fixtures above use -- see that function's
 * own doc for why this avoids the unloaded-acre landing failure. All deposit OFF (holes, not buried
 * items): (24,106)/(40,106)/(56,106) HOLE_START (variant 0) for the generic-item-bury+dig-up-round-trip,
 * pitfall-into-hole, and same-tile-race tests respectively (kept on separate tiles so no two tests ever
 * contend for a tile they don't mean to); (72,106)/(88,106) HOLE_SHINE for the pitfall-into-shine
 * (valid hole_variant / 0xFF sentinel) tests; (24,107) a further, independent HOLE_SHINE for the
 * money-bury-into-shine test. */
static void pcnetgame_run_bury_test_seed(void) {
    static int s_generic_seed_done = 0;
    static int s_pitfall_hole_seed_done = 0;
    static int s_race_seed_done = 0;
    static int s_shine_variant_seed_done = 0;
    static int s_shine_sentinel_seed_done = 0;
    static int s_money_shine_seed_done = 0;
    static int s_logged = 0;

    if (!g_pc_bury_test_seed || s_role != PC_NETGAME_ROLE_HOST || gamePT == NULL || !s_host_world_ready ||
        !pcfa_scene_is_town()) {
        return;
    }
    if (!s_logged) {
        s_logged = 1;
        printf("[NET][BURY] --bury-test-seed active: seeding 3 HOLE00 tiles + 3 HOLE_SHINE tiles\n");
    }
    if (!s_generic_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)HOLE_START, 24, 106, TRUE)) {
        s_generic_seed_done = 1;
        printf("[NET][BURY] --bury-test-seed: HOLE_START (generic bury + dig-up round trip) fixture placed at "
               "tile (24,106)\n");
    }
    if (!s_pitfall_hole_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)HOLE_START, 40, 106, TRUE)) {
        s_pitfall_hole_seed_done = 1;
        printf("[NET][BURY] --bury-test-seed: HOLE_START (pitfall-into-hole) fixture placed at tile (40,106)\n");
    }
    if (!s_race_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)HOLE_START, 56, 106, TRUE)) {
        s_race_seed_done = 1;
        printf("[NET][BURY] --bury-test-seed: HOLE_START (same-tile race) fixture placed at tile (56,106)\n");
    }
    if (!s_shine_variant_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)HOLE_SHINE, 72, 106, TRUE)) {
        s_shine_variant_seed_done = 1;
        printf("[NET][BURY] --bury-test-seed: HOLE_SHINE (pitfall, valid hole_variant) fixture placed at tile "
               "(72,106)\n");
    }
    if (!s_shine_sentinel_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)HOLE_SHINE, 88, 106, TRUE)) {
        s_shine_sentinel_seed_done = 1;
        printf("[NET][BURY] --bury-test-seed: HOLE_SHINE (pitfall, 0xFF sentinel) fixture placed at tile "
               "(88,106)\n");
    }
    if (!s_money_shine_seed_done && mFI_UtNumtoFGSet_common((mActor_name_t)HOLE_SHINE, 24, 107, TRUE)) {
        s_money_shine_seed_done = 1;
        printf("[NET][BURY] --bury-test-seed: HOLE_SHINE (money-tree roll) fixture placed at tile (24,107)\n");
    }
}

void pc_net_game_poll(void) {
    PCNetEvent ev;

    if (s_role == PC_NETGAME_ROLE_NONE) return;

    if (s_role == PC_NETGAME_ROLE_CLIENT) {
        pcnetgame_client_talk_refresh_tick(); /* M9-C: keep an outstanding talk BEGIN alive on the host */
    }

    /* v2: refresh the "gameplay save loaded" latch once, before anything consults it. */
    {
        int local_ready = pcnetgame_update_local_world_ready();
        if (s_role == PC_NETGAME_ROLE_HOST) {
            pcnetgame_host_world_tick(local_ready);
            /* Two-phase interactions: release timed-out reservations BEFORE this poll's events are
               handled, so a late CONFIRM / a competing request is judged against the up-to-date table. */
            pcnetgame_host_expire_reservations();
            pcnetgame_host_talk_hold_sweep(); /* M9-C: expire stale villager talk holds */
            /* World Ecology Stage 1, Item 2: same "before this poll's events" placement -- a money-rock
               window that just expired must revert before a same-tile hit arriving this poll is judged. */
            pcnetgame_host_check_field_action_money_rock();
            /* World Ecology T1: same "before this poll's events" placement -- an idle tree cut-count
               slot that just expired must be forgotten before a same-tile chop arriving this poll is
               judged (it simply re-derives its hit count fresh either way, but this keeps the ordering
               consistent with every other per-poll expiry tick in this function). */
            pcnetgame_host_check_tree_cut();
            /* World Ecology T1 review fix: authoritative wildlife-table idle expiry -- host-only,
             * same "before this poll's events" placement as the other per-poll expiry ticks above.
             * See pcwld_host_check_idle()'s own doc (pc_wildlife_authority.c/.h) for the bug this
             * closes. */
            pcwld_host_check_idle();
        }
        /* World Ecology T1 review fix: local presentation-map idle expiry -- ANY role (a client
         * maintains its own local presentation map too, see pcwld_presentation_check_idle()'s own
         * doc). Placed outside the host-only block above but still before this poll's events are
         * handled, matching the same ordering convention. */
        pcwld_presentation_check_idle();
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
                        int was_ready_peer = (s_host_peer_link[ev.peer] == PC_NETGAME_LINK_READY);
                        s_host_peer_link[ev.peer] = PC_NETGAME_LINK_DISCONNECTED;
                        if (was_ready_peer) {
                            pcnetgame_host_peer_scene_gone(ev.peer); /* M9-A: clients drop the relayed scene */
                        }
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
                    {
                        /* M9-A: observability for the scene clear done by pcnetgame_reset_client_session_state()
                         * below (counted BEFORE it runs): how many other players' scenes this client was holding. */
                        int scene_pid;
                        int held = 0;
                        PCNetPlayerScene scene_tmp;
                        for (scene_pid = 0; scene_pid <= (int)PC_NETGAME_HOST_PLAYER_ID; scene_pid++) {
                            held += pc_remote_player_get_scene((PCNetPlayerId)scene_pid, &scene_tmp) ? 1 : 0;
                        }
                        printf("[NET][SCENE] client: host link lost -- clearing %d stored peer scene(s)\n", held);
                    }
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

    /* M9-A: local scene detection + announcement (both roles); then the off-by-default test hook. */
    pcnetgame_scene_tick();
    pcnetgame_run_scene_test_hook();
    pcnetgame_run_collide_test_hook(); /* M9-B: off-by-default --collide-test-* hooks */

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

    /* M9-D G2-3: safety timeout of the retained committed-bury claim (see s_bury_committed). */
    if (gamePT != NULL && s_bury_committed.valid &&
        graph_dt_period_elapsed(gamePT, &s_bury_committed.keep_accum, PC_NETGAME_BURY_COMMITTED_KEEP_60FPS_FRAMES)) {
        s_bury_committed.valid = 0;
    }

    /* World Ecology T3: pending bury-request timeout/retry, client-only. Exact structural mirror of
       the pickup/drop retry blocks above -- see their own doc comments for the shared reasoning (gamePT
       gate, timeout/retry budget). Kept as a separate block against a separate pending instance for the
       same "don't conflate different kinds' retries" reason as s_bury_pending's own doc comment. */
    if (gamePT != NULL && s_bury_pending.valid &&
        graph_dt_period_elapsed(gamePT, &s_bury_pending.timeout_accum, PC_NETGAME_BURY_TIMEOUT_60FPS_FRAMES)) {
        if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
            s_bury_pending.valid = 0;
        } else if (s_bury_pending.retry_count >= PC_NETGAME_BURY_MAX_RETRIES) {
            printf("[NET][BURY] request %u timed out after %d retries -- giving up (tile %d,%d)\n",
                   (unsigned)s_bury_pending.request_id, s_bury_pending.retry_count,
                   (int)s_bury_pending.ut_x, (int)s_bury_pending.ut_z);
            pcnetgame_client_send_confirm((uint8_t)PC_NETGAME_INTERACT_KIND_BURY, (uint8_t)PC_NETGAME_CONFIRM_ABORT,
                                          (uint8_t)PC_NETGAME_CONFIRM_REASON_CANCELLED, s_bury_pending.request_id);
            s_bury_pending.valid = 0;
        } else {
            PCNetGameBuryRequestMsg msg;
            memset(&msg, 0, sizeof(msg));
            msg.msg_type = (uint8_t)PC_NETGAME_MSG_BURY_REQUEST;
            msg.pocket_slot_idx = s_bury_pending.pocket_slot_idx;
            msg.ut_x = s_bury_pending.ut_x;
            msg.ut_z = s_bury_pending.ut_z;
            msg.claimed_item = s_bury_pending.claimed_item;
            msg.hole_variant = s_bury_pending.hole_variant;
            msg.request_id = s_bury_pending.request_id; /* SAME id -- see the pickup retry block's own comment */
            if (pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
                if (s_bury_pending.unsent) {
                    s_bury_pending.unsent = 0; /* this was the FIRST send, not a retry: not counted */
                } else {
                    s_bury_pending.retry_count++;
                }
            } else {
                s_bury_pending.timeout_accum = PC_NETGAME_BURY_TIMEOUT_60FPS_FRAMES; /* not a retry */
            }
        }
    }

    /* World Ecology Stage 1 / T0-C: pending field-action (DIG_BURIED/MONEY_ROCK_HIT, and any future
       kind) request timeout/retry, client-only, operating on the QUEUE HEAD only -- the only entry
       ever actually in flight on the wire (see s_field_action_queue's own doc). Structural mirror of
       the pickup/drop retry blocks above -- same gamePT gate, same timeout/retry budget shape
       (PC_NETGAME_FIELD_ACTION_TIMEOUT_60FPS_FRAMES/_MAX_RETRIES). No ABORT to send on giving up
       (unlike pickup/drop) -- there is no host-side reservation to release (see
       PCNetGameFieldActionRequestMsg's own doc): giving up here just pops the head (abandoning that one
       request) so the caller's next frame can try again from scratch, and promotes whatever is queued
       behind it, if anything (pcnetgame_field_action_queue_pop_head()). */
    if (gamePT != NULL && s_field_action_queue_len > 0 &&
        graph_dt_period_elapsed(gamePT, &s_field_action_queue[0].timeout_accum,
                                PC_NETGAME_FIELD_ACTION_TIMEOUT_60FPS_FRAMES)) {
        PCNetGameFieldActionPending* head = &s_field_action_queue[0];
        if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
            /* The connection itself is gone -- every queued request is moot, not just the head. */
            s_field_action_queue_len = 0;
        } else if (head->retry_count >= PC_NETGAME_FIELD_ACTION_MAX_RETRIES) {
            printf("[NET][FIELD_ACTION] request %u (kind %u) timed out after %d retries -- giving up (tile %d,%d)\n",
                   (unsigned)head->request_id, (unsigned)head->kind, head->retry_count, (int)head->ut_x,
                   (int)head->ut_z);
            pcnetgame_field_action_queue_pop_head();
        } else {
            if (pcnetgame_field_action_queue_send(head)) {
                if (head->unsent) {
                    head->unsent = 0;
                } else {
                    head->retry_count++;
                }
            } else {
                head->timeout_accum = PC_NETGAME_FIELD_ACTION_TIMEOUT_60FPS_FRAMES;
            }
        }
    }

    /* (v2: the Stage 5A deferred FIELD_UPDATE retry loop that lived here is gone -- see the note
     * where s_pending_field_updates used to be declared.) */

    /* Stage 5A.1: see pcnetgame_run_pickup_test_seed()'s own doc -- a complete no-op unless
     * --pickup-test-seed was passed on the command line. */
    pcnetgame_run_pickup_test_seed();

    /* World Ecology Stage 1: see pcnetgame_run_field_action_test_seed()'s own doc -- a complete no-op
     * unless --field-action-test-seed was passed on the command line. */
    pcnetgame_run_field_action_test_seed();

    /* World Ecology T3: see pcnetgame_run_bury_test_seed()'s own doc -- a complete no-op unless
     * --bury-test-seed was passed on the command line. */
    pcnetgame_run_bury_test_seed();

    /* Villager population/is_home milestone: see pcnetgame_run_villager_test_triggers()'s own doc --
     * a complete no-op unless --force-villager-grow/--force-villager-remove was passed. */
    pcnetgame_run_villager_test_triggers();

    /* Friendship/mail sync milestone: see pcnetgame_run_friendship_mail_test_triggers()'s own doc --
     * a complete no-op unless --force-friendship-delta/--force-mail-send was passed. Unlike the
     * villager-population trigger above, this one is NOT host-only. */
    pcnetgame_run_friendship_mail_test_triggers();

    /* World Ecology Stage 1 money-rock review: see pcnetgame_run_money_rock_test_triggers()'s own doc --
     * a complete no-op unless --force-money-rock-hit/--force-money-bag-pickup was passed. */
    pcnetgame_run_money_rock_test_triggers();

    /* P1 (World Ecology T-dig) real-gameplay verification: see pcnetgame_run_dig_hole_test_trigger()'s
     * own doc -- a complete no-op unless --force-dig-hole was passed. */
    pcnetgame_run_dig_hole_test_trigger();

    /* World Ecology Wildlife Sync T1 real-gameplay verification: see
     * pcnetgame_run_wildlife_trigger_test_trigger()'s own doc -- a complete no-op unless
     * --force-wildlife-trigger was passed. */
    pcnetgame_run_wildlife_trigger_test_trigger();

    /* World Ecology Wildlife Sync T-catch real-gameplay verification: see
     * pcnetgame_run_fish_catch_test_trigger_host()/_client()'s own doc -- a complete no-op unless
     * --force-fish-catch was passed. Both are safe to call unconditionally every poll, regardless of
     * role (each checks its own role internally, mirroring every other force-* trigger pair). */
    pcnetgame_run_fish_catch_test_trigger_host();
    pcnetgame_run_fish_catch_test_trigger_client();

    /* World Ecology Wildlife Sync T4 real-gameplay verification: see
     * pcnetgame_run_bug_catch_test_trigger_host()/_client()'s own doc -- a complete no-op unless
     * --force-bug-catch was passed. Both are safe to call unconditionally every poll, regardless of role
     * (each checks its own role internally, mirroring the --force-fish-catch pair above). */
    pcnetgame_run_bug_catch_test_trigger_host();
    pcnetgame_run_bug_catch_test_trigger_client();

    /* T8 audit verification, TEST-ONLY: see pcnetgame_run_bug_ttl_lookup_diag_host()'s own doc -- a
     * complete no-op unless --diag-bug-ttl-lookup was passed. */
    pcnetgame_run_bug_ttl_lookup_diag_host();

    /* T8 review fix verification, TEST-ONLY: see pcnetgame_run_bug_despawn_label_race_diag()'s own doc --
     * a complete no-op unless --diag-bug-despawn-label-race was passed. */
    pcnetgame_run_bug_despawn_label_race_diag();

    /* T8 audit verification, TEST-ONLY: see pcnetgame_run_role_link_state_diag()'s own doc -- a complete
     * no-op unless --diag-role-link-state was passed. */
    pcnetgame_run_role_link_state_diag();
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

int pc_net_game_request_pickup(int ut_x, int ut_z, int item) {
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
        /* No free pocket: vanilla leaves the item where it is when the pockets are full, so ordinarily
         * send nothing and change nothing (the host never reserves/removes an item nobody can receive).
         *
         * Review finding fix: EXCEPT a money bag the WALLET might still have room for -- vanilla/
         * PC_ENHANCEMENTS allows a money bag to bypass the pocket check entirely via direct wallet
         * credit (see Player_actor_setup_main_Pickup(), m_player_main_pickup.c_inc), so a client with
         * full pockets must still be allowed to ATTEMPT collecting a wallet-fitting money bag -- refusing
         * to even send the request made that impossible for any network client, full pockets or not.
         * `item` is only this client's own local guess at what the tile holds (see this function's own
         * doc, pc_net_game.h); it decides only whether to still try, never the actual outcome. The real
         * safety net is unchanged: pcnetgame_handle_client_pickup_result()'s own Bug 3 pre-check
         * re-verifies wallet_fits/pocket_free against the host's actual granted_item before ever sending
         * INTERACT_CONFIRM, so a wrong local guess here can only cause a harmless rejection/abort, never
         * an incorrect grant or a lost bag. */
        int wallet_might_fit = 0;
#ifdef PC_ENHANCEMENTS
        {
            u32 possible_bell_amount = mPr_GetAmountForMoneyItem((mActor_name_t)item);
            wallet_might_fit = possible_bell_amount > 0 && Now_Private->inventory.wallet <= mPr_WALLET_MAX &&
                               possible_bell_amount <= (mPr_WALLET_MAX - Now_Private->inventory.wallet);
        }
#endif
        if (!wallet_might_fit) {
            printf("[NET][PICKUP] pockets full -- pickup at (%d,%d) not sent (the item stays in the world)\n", ut_x, ut_z);
            return 1;
        }
        printf("[NET][PICKUP] pockets full but item=0x%04X may still fit the wallet -- pickup at (%d,%d) sent anyway; the "
               "host's own granted item and this client's Bug 3 pre-check are the real authority on the outcome\n",
               (unsigned)item, ut_x, ut_z);
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

/* World Ecology Stage 1 / T0-C: shared ENQUEUE helper for both pc_net_game_request_dig_buried() and
 * pc_net_game_request_money_rock_hit() -- the two functions differ only in their pocket-space
 * precondition (DIG_BURIED needs a free slot; MONEY_ROCK_HIT needs none), which each checks itself
 * before calling this, and both still refuse to call this at all while anything is already queued (see
 * their own "one already in flight" guard) -- so for these two kinds the queue this appends to can
 * still never hold more than 1 entry, exactly as before T0-C. Appends to s_field_action_queue and, if
 * the queue was empty (this is now the head), sends it immediately -- exactly like the pre-queue code's
 * own single-slot send. A FUTURE caller (e.g. T1) that enqueues without that same guard can rely on
 * this appending behind whatever is already queued instead of being dropped, up to
 * PC_NETGAME_FIELD_ACTION_QUEUE_DEPTH. */
static int pcnetgame_send_field_action_request_ex_grant(uint8_t kind, int ut_x, int ut_z, uint8_t hole_variant,
                                                          uint16_t local_grant) {
    PCNetGameOwnerStamp stamp;
    PCNetGameFieldActionPending* e;

    if (!pcnetgame_capture_owner_stamp(&stamp)) {
        printf("[NET][FIELD_ACTION] no gameplay save loaded -- kind %u at (%d,%d) not sent\n", (unsigned)kind, ut_x,
               ut_z);
        return 1;
    }
    if (s_field_action_queue_len >= PC_NETGAME_FIELD_ACTION_QUEUE_DEPTH) {
        printf("[NET][FIELD_ACTION] queue full (%d) -- kind %u at (%d,%d) dropped\n",
               PC_NETGAME_FIELD_ACTION_QUEUE_DEPTH, (unsigned)kind, ut_x, ut_z);
        return 1;
    }

    e = &s_field_action_queue[s_field_action_queue_len];
    memset(e, 0, sizeof(*e));
    e->valid = 1;
    e->kind = kind;
    e->request_id = s_next_field_action_request_id++;
    e->owner = stamp;
    e->ut_x = (uint8_t)ut_x;
    e->ut_z = (uint8_t)ut_z;
    e->hole_variant = hole_variant;
    e->timeout_accum = 0.0f;
    e->retry_count = 0;
    e->unsent = 0;
    e->local_grant = local_grant;
    s_field_action_queue_len++;

    if (s_field_action_queue_len == 1) {
        /* This is now the head -- the only in-flight slot -- send it immediately, exactly like the
           pre-queue code's own single-slot send did for its one-and-only pending request. */
        pcnetgame_field_action_queue_kick_head();
    }
    return 1;
}

static int pcnetgame_send_field_action_request_ex(uint8_t kind, int ut_x, int ut_z, uint8_t hole_variant) {
    return pcnetgame_send_field_action_request_ex_grant(kind, ut_x, ut_z, hole_variant, 0);
}

/* World Ecology T-dig (D / A-2): single-flight guard for the two grant-carrying kinds, mirroring
 * DIG_BURIED's/MONEY_ROCK_HIT's own "one already in flight" precedent (see their own request functions'
 * doc) -- unlike plain DIG_HOLE/DIG_SHINE, a player must never have more than one privately-rolled grant
 * outstanding at once (stacking two would risk a double-grant if both somehow got accepted). Scans the
 * whole queue, not just the head, since an earlier grant-carrying request may still be queued behind
 * something else. */
static int pcnetgame_field_action_grant_already_pending(void) {
    int i;
    for (i = 0; i < s_field_action_queue_len; i++) {
        if (s_field_action_queue[i].valid && s_field_action_queue[i].local_grant != 0) {
            return 1;
        }
    }
    return 0;
}

/* Pre-v3 call shape, kept for every EXISTING caller (DIG_BURIED, MONEY_ROCK_HIT, TREE_SHAKE, TREE_CHOP,
 * SNOWMAN_BREAK) unmodified -- none of these kinds use hole_variant, so this thin wrapper just pins it
 * to 0, exactly matching PCNetGameFieldActionRequestMsg's own doc ("every sender explicitly sets it to 0
 * for those kinds"). */
static int pcnetgame_send_field_action_request(uint8_t kind, int ut_x, int ut_z) {
    return pcnetgame_send_field_action_request_ex(kind, ut_x, ut_z, 0);
}

/* See pc_net_game.h's own doc. */
int pc_net_game_request_dig_buried(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0; /* single-player or host -- caller proceeds with the normal local dig, unmodified */
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] not in the town scene -- DIG_BURIED at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    if (s_field_action_queue_len > 0) {
        return 1; /* one already in flight/queued -- see s_field_action_queue's own doc: this guard
                     keeps DIG_BURIED's own queue usage at exactly 1 entry, byte-for-byte like the
                     pre-T0-C single-slot behavior */
    }
    if (mPr_GetPossessionItemIdx(Now_Private, (mActor_name_t)EMPTY_NO) < 0) {
        /* No free pocket: vanilla leaves the buried item where it is (see
           Player_actor_setup_main_Get_scoop()'s own free_space check, m_player_main_get_scoop.c_inc) --
           the real granted item is not known yet, so this is a plain EMPTY_NO check, exactly like
           pc_net_game_request_pickup()'s own (see this function's doc in pc_net_game.h). */
        printf("[NET][FIELD_ACTION] pockets full -- DIG_BURIED at (%d,%d) not sent\n", ut_x, ut_z);
        return 1;
    }
    return pcnetgame_send_field_action_request((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_BURIED, ut_x, ut_z);
}

/* See pc_net_game.h's own doc. */
int pc_net_game_request_money_rock_hit(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] not in the town scene -- MONEY_ROCK_HIT at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    if (s_field_action_queue_len > 0) {
        return 1; /* one already in flight/queued -- see s_field_action_queue's own doc */
    }
    return pcnetgame_send_field_action_request((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_MONEY_ROCK_HIT, ut_x, ut_z);
}

/* See pc_net_game.h's own doc. Unlike pc_net_game_request_dig_buried()/_money_rock_hit() above,
 * deliberately has NO "one already in flight/queued" guard -- several distinct tree tiles may
 * legitimately have a shake/bee-birth request outstanding at once, and the shared 4-deep
 * s_field_action_queue FIFO (T0-C) already exists to support exactly this (see that queue's own doc). */
int pc_net_game_request_tree_shake(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] not in the town scene -- TREE_SHAKE at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    return pcnetgame_send_field_action_request((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_TREE_SHAKE, ut_x, ut_z);
}

/* See pc_net_game.h's own doc. No "one already in flight" guard either -- repeated axe swings need the
 * 4-deep FIFO so a chop-spam sequence is not silently dropped down to one hit (see
 * pc_net_game_request_tree_shake()'s own doc, just above). */
int pc_net_game_request_tree_chop(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] not in the town scene -- TREE_CHOP at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    return pcnetgame_send_field_action_request((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_TREE_CHOP, ut_x, ut_z);
}

/* See pc_net_game.h's own doc. Deliberately no reach/IN_TOWN precondition on the sender -- the host
 * alone judges acceptance -- and no free-pocket-slot-style check either (nothing is granted back to
 * the caller on success). request_id shares s_next_field_action_request_id's space (see
 * PCNetGameSnowmanBuildRequestMsg's own doc); a send failure (reliable window full) is simply dropped
 * and logged, not retried -- this is a documented, accepted gap (see this function's own header doc,
 * pc_net_game.h): a lost BUILD leaves the host never learning this particular snowman. */
int pc_net_game_request_snowman_build(int ut_x, int ut_z, int head_size, int body_size, int score) {
    PCNetGameSnowmanBuildRequestMsg msg;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_SNOWMAN_BUILD_REQUEST;
    msg.ut_x = (uint8_t)ut_x;
    msg.ut_z = (uint8_t)ut_z;
    msg.head_size = (uint8_t)head_size;
    msg.body_size = (uint8_t)body_size;
    msg.score = (uint8_t)score;
    msg.request_id = s_next_field_action_request_id++;
    if (!pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
        printf("[NET][SNOWMAN] client: BUILD request %u at (%d,%d) could not be queued (window full) -- dropped, "
               "not retried (see pc_net_game_request_snowman_build()'s own accepted-gap doc)\n",
               (unsigned)msg.request_id, ut_x, ut_z);
    }
    return 1;
}

/* See pc_net_game.h's own doc. Deliberately no "one already in flight" guard -- uses the full 4-deep
 * FIFO like TREE_SHAKE/TREE_CHOP above, not DIG_BURIED/MONEY_ROCK_HIT's single-slot emulation. */
int pc_net_game_request_snowman_break(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] not in the town scene -- SNOWMAN_BREAK at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    return pcnetgame_send_field_action_request((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_SNOWMAN_BREAK, ut_x, ut_z);
}

/* World Ecology T-dig: DIG_HOLE (kind 5) -- digging a brand-new hole into EMPTY_NO ground, or removing a
 * flower/stump/sapling/grass tuft. `hole_variant` is THIS client's own proposed hole-shape pick (0..24,
 * e.g. from mCoBG_GetHoleNumber()'s own local collision-mesh-driven choice) -- the host range-validates
 * it (see pcnetgame_validate_hole_variant()) and, if in range, commits EXACTLY that shape; out-of-range
 * is rejected outright, never clamped (see PCNetGameFieldActionRequestMsg's own TRUST BOUNDARY doc).
 * Nothing is granted back on accept (a removed plant is never placed in a pocket -- see this kind's own
 * doc above); the caller may still use a locally-rolled golden-shovel bonus (see this task's own design
 * brief) after a free-pocket-slot pre-check, entirely independent of this request. No "one already in
 * flight" guard -- shares the same 4-deep FIFO as tree shake/chop. */
int pc_net_game_request_dig_hole(int ut_x, int ut_z, int hole_variant) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] not in the town scene -- DIG_HOLE at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    if (hole_variant < 0 || hole_variant > 24) {
        hole_variant = 0; /* a malformed LOCAL value is simply normalized before sending -- the host
                              re-validates independently regardless, this is not the trust boundary */
    }
    return pcnetgame_send_field_action_request_ex((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_HOLE, ut_x, ut_z,
                                                   (uint8_t)hole_variant);
}

/* World Ecology T-dig (A-2): identical to pc_net_game_request_dig_hole() above, EXCEPT the resulting
 * queue entry also carries `local_grant` -- a PRIVATE, client-rolled item id (ITM_MONEY_100 for the
 * golden-shovel 10% bonus) granted to a free pocket slot via mPr_SetFreePossessionItem() if and only if
 * this exact request is later ACCEPTED (see pcnetgame_handle_client_field_action_result()'s DIG_HOLE
 * branch). The host is never told about the grant and never rolls or validates it -- it only ever sees an
 * ordinary DIG_HOLE request and commits an ordinary hole, exactly like pc_net_game_request_dig_hole()'s
 * own doc. On REJECT, the pending grant is simply dropped with the rest of the queue entry (the caller
 * never played any local animation for this path, so there is nothing to undo). */
int pc_net_game_request_dig_hole_with_grant(int ut_x, int ut_z, int hole_variant, int local_grant) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] not in the town scene -- DIG_HOLE(grant) at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    if (hole_variant < 0 || hole_variant > 24) {
        hole_variant = 0;
    }
    if (local_grant != 0 && pcnetgame_field_action_grant_already_pending()) {
        return 1; /* one grant already in flight/queued -- see this guard's own doc */
    }
    return pcnetgame_send_field_action_request_ex_grant((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_HOLE, ut_x, ut_z,
                                                         (uint8_t)hole_variant, (uint16_t)local_grant);
}

/* World Ecology T-dig: FILL_HOLE (kind 6) -- filling an existing EMPTY hole back in. No hole_variant
 * needed (filling always yields plain EMPTY_NO). No "one already in flight" guard, same as DIG_HOLE. */
int pc_net_game_request_fill_hole(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] not in the town scene -- FILL_HOLE at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    return pcnetgame_send_field_action_request((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_FILL_HOLE, ut_x, ut_z);
}

/* World Ecology T-dig: PITFALL_CONSUME (kind 7) -- a player (or, on the host, a villager) falling INTO
 * an already-buried pitfall. Deliberately OPTIMISTIC: unlike every other kind in this family, the
 * caller plays its local fall animation immediately and unconditionally, WITHOUT waiting for this
 * request's RESULT (the animation itself has no gameplay consequence -- see this task's own design
 * brief) -- this function only sends the request so the host can authoritatively consume the tile and
 * resolve any duplicate-fall race. On REJECT (someone else already consumed it, or it was already dug
 * up), pcnetgame_handle_client_field_action_result() reconciles by applying the host's own current tile
 * value, echoed in the RESULT's granted_item field (chosen over a full-resync trigger: this is a single,
 * already-known tile, so echoing its value is simpler than forcing a resync for one stale write). */
int pc_net_game_request_pitfall_consume(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] not in the town scene -- PITFALL_CONSUME at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    return pcnetgame_send_field_action_request((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_PITFALL_CONSUME, ut_x, ut_z);
}

/* World Ecology T-dig: DIG_SHINE (kind 8) -- digging up an unburied SHINE_SPOT. hole_variant is ignored
 * host-side for this kind (a shine hole is not variant-shaped) -- always sent as 0. Grants NOTHING
 * host-side: the caller rolls its own bell amount locally (after its own free-pocket-slot pre-check) and
 * grants it privately on accept, matching vanilla's per-digger-luck design. No "one already in flight"
 * guard. */
int pc_net_game_request_dig_shine(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] not in the town scene -- DIG_SHINE at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    return pcnetgame_send_field_action_request((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_SHINE, ut_x, ut_z);
}

/* World Ecology T-dig (D): identical to pc_net_game_request_dig_shine() above, EXCEPT the resulting queue
 * entry also carries `local_grant` -- THIS client's own privately-rolled bell amount (1000/10000/30000),
 * granted to a free pocket slot via mPr_SetFreePossessionItem() if and only if this exact request is
 * later ACCEPTED (see pcnetgame_handle_client_field_action_result()'s DIG_SHINE branch). The host never
 * rolls or knows the bell amount -- matching vanilla's per-digger-luck design (see this kind's own doc).
 * On REJECT, the pending grant is simply dropped with the rest of the queue entry (no local animation
 * was played for this path, so there is nothing to undo). */
int pc_net_game_request_dig_shine_with_grant(int ut_x, int ut_z, int local_grant) {
    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return 1;
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][FIELD_ACTION] not in the town scene -- DIG_SHINE(grant) at (%d,%d) not sent\n", ut_x, ut_z);
        }
        return 1;
    }
    if (local_grant != 0 && pcnetgame_field_action_grant_already_pending()) {
        return 1; /* one grant already in flight/queued -- see pcnetgame_field_action_grant_already_pending()'s own doc */
    }
    return pcnetgame_send_field_action_request_ex_grant((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_SHINE, ut_x, ut_z,
                                                         0, (uint16_t)local_grant);
}

/* World Ecology T-dig: HOST-LOCAL wrappers, mirroring pc_net_game_host_local_money_rock_hit()'s/
 * pc_net_game_host_local_tree_shake()'s own precedent exactly -- routes the HOST's own local dig/fill/
 * pitfall/shine action through the SAME validate/commit adapters a remote peer's request uses, via
 * pcnetgame_host_dispatch_local_field_action(), so there is exactly one owner per tile action. */
void pc_net_game_host_local_dig_hole(int ut_x, int ut_z, int hole_variant) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!pcfa_scene_is_town()) {
        return;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return;
    }
    if (hole_variant < 0 || hole_variant > 24) {
        hole_variant = 0;
    }
    pcnetgame_host_dispatch_local_field_action_ex((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_HOLE, (uint8_t)ut_x,
                                                  (uint8_t)ut_z, (uint8_t)hole_variant);
}

void pc_net_game_host_local_fill_hole(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!pcfa_scene_is_town()) {
        return;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return;
    }
    pcnetgame_host_dispatch_local_field_action((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_FILL_HOLE, (uint8_t)ut_x,
                                               (uint8_t)ut_z);
}

void pc_net_game_host_local_pitfall_consume(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!pcfa_scene_is_town()) {
        return;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return;
    }
    pcnetgame_host_dispatch_local_field_action((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_PITFALL_CONSUME, (uint8_t)ut_x,
                                               (uint8_t)ut_z);
}

void pc_net_game_host_local_dig_shine(int ut_x, int ut_z) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!pcfa_scene_is_town()) {
        return;
    }
    if (ut_x < 0 || ut_x > 255 || ut_z < 0 || ut_z > 255) {
        return;
    }
    pcnetgame_host_dispatch_local_field_action((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_SHINE, (uint8_t)ut_x,
                                               (uint8_t)ut_z);
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
    {
        /* M9-C Phase 5: the tile is already cleared here, but the committed SHADOW still holds its pre-pickup value (the
         * flush below is what updates it), so the picked item id is read from there (no vanilla seam touched).
         * Known edge (review R2-L2, cosmetic only, logic deliberately unchanged): if the tile changed EARLIER in the same frame
         * before the per-poll flush (for example a host drop + pickup of the same tile in one frame), the shadow is stale: no
         * PICKUP event is emitted (shadow EMPTY) or the event shows the previous item (stale value). */
        int a_acre = 0, a_tile = 0;
        uint16_t prev_item = (uint16_t)EMPTY_NO, now_item = 0;
        int have_prev = s_host_world_ready && pcfa_town_ut_to_acre_tile(ut_x, ut_z, &a_acre, &a_tile) &&
                        pcnetgame_dep_bit(s_shadow_known[a_acre], a_tile);

        if (have_prev) {
            prev_item = s_shadow_items[a_acre][a_tile];
        }
        pcnetgame_host_commit_town_ut(ut_x, ut_z);
        if (have_prev && prev_item != (uint16_t)EMPTY_NO && pcfa_get_tile(a_acre, a_tile, &now_item) &&
            now_item == (uint16_t)EMPTY_NO) {
            pcnetgame_host_emit_player_action((int)PC_NETGAME_HOST_PLAYER_ID, (uint8_t)PC_NETGAME_PLAYER_ACTION_KIND_PICKUP,
                                              ut_x, ut_z, prev_item);
        }
    }
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

/* M9-D G4-2: see pc_net_game.h. Remembers where the last hand swap wrote into the pockets. */
void pc_net_game_exchange_note_swap(int slot, int swapped, int new_item) {
    if (s_role != PC_NETGAME_ROLE_CLIENT) {
        return; /* host / single-player: never records anything */
    }
    if (swapped && slot >= 0 && slot < mPr_POCKETS_SLOT_COUNT) {
        s_exchange_swap_slot = slot;
        s_exchange_swap_item = (uint16_t)new_item;
    } else {
        s_exchange_swap_slot = -1;
    }
}

/* M9-D G4-2: see pc_net_game.h. Called at most once per exchange-menu close (the swap note is consumed). */
int pc_net_game_exchange_request_drop(int hand_item, int hand_cond, int ut_x, int ut_z) {
    const int slot = s_exchange_swap_slot;
    mActor_name_t replacement;
    int replacement_cond;
    const char* why;

    s_exchange_swap_slot = -1; /* consumed whatever happens below */
    if (s_role != PC_NETGAME_ROLE_CLIENT) {
        return 0;
    }
    if (Now_Private == NULL || slot < 0 || slot >= mPr_POCKETS_SLOT_COUNT ||
        Now_Private->inventory.pockets[slot] != (mActor_name_t)s_exchange_swap_item) {
        /* No (or a stale) swap note: nowhere to hold the hand item for the request. It is lost. */
        printf("[NET][DROP] exchange: hand item 0x%04X cannot be routed (no valid swap slot) -- item lost\n",
               (unsigned)hand_item);
        return 0;
    }
    replacement = Now_Private->inventory.pockets[slot];
    replacement_cond = mPr_GET_ITEM_COND(Now_Private->inventory.item_conditions, slot);

    /* Failure posture: every failure below leaves the HAND item in pockets[slot] (the drop did not happen,
     * so there is no room for the replacement, which is lost). Never a local field write. */
    mPr_SetPossessionItem(Now_Private, slot, (mActor_name_t)hand_item, hand_cond);

    if (hand_cond != mPr_ITEM_COND_NORMAL) {
        why = "item is a present/quest item";
    } else if (ut_x < 0 || ut_z < 0) {
        why = "no legal drop tile";
    } else if (s_drop_pending.valid) {
        why = "another drop request is in flight";
    } else if (!pc_net_game_is_droppable_item(hand_item)) {
        why = "item is not droppable over the network";
    } else if (!pc_net_game_request_drop(slot, hand_item, ut_x, ut_z) || !s_drop_pending.valid) {
        why = "request not sent";
    } else {
        s_exchange_deferred.valid = 1;
        s_exchange_deferred.request_id = s_drop_pending.request_id;
        s_exchange_deferred.slot = (uint8_t)slot;
        s_exchange_deferred.replacement = (uint16_t)replacement;
        s_exchange_deferred.replacement_cond = (uint8_t)replacement_cond;
        s_exchange_deferred.owner = s_drop_pending.owner;
        printf("[NET][DROP] exchange: hand item 0x%04X sent as drop request %u from slot %d; replacement 0x%04X deferred\n",
               (unsigned)hand_item, (unsigned)s_drop_pending.request_id, slot, (unsigned)replacement);
        return 1;
    }
    printf("[NET][DROP] exchange: hand item 0x%04X not dropped (%s) -- kept in slot %d, replacement 0x%04X lost\n",
           (unsigned)hand_item, why, slot, (unsigned)replacement);
    return 0;
}

/* World Ecology T3 (bugfix): see pc_net_game.h's own doc for the full contract -- the caller must NOT
 * clear the pocket based on this function's return value; that now happens in
 * pcnetgame_handle_client_bury_result() upon provisional accept, after a slot-still-holds-the-item
 * re-check, exactly like pc_net_game_request_drop()/pcnetgame_handle_client_drop_result(). Structural
 * mirror of pc_net_game_request_drop() -- same client-only/one-in-flight/defensive-bounds/owner-stamp/
 * slot-still-holds-claimed-item shape -- plus hole_variant, which is simply carried along (never itself
 * validated client-side beyond its own 0..24-or-0xFF range: the host is the one that actually
 * enforces/consumes it). */
int pc_net_game_request_bury(int pocket_slot_idx, int claimed_item, int ut_x, int ut_z, int hole_variant) {
    PCNetGameBuryRequestMsg msg;
    PCNetGameOwnerStamp stamp;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0; /* single-player or host -- caller should proceed with the normal local
                     mutation, unmodified */
    }
    if (pocket_slot_idx < 0 || pocket_slot_idx >= mPr_POCKETS_SLOT_COUNT || ut_x < 0 || ut_x > 255 ||
        ut_z < 0 || ut_z > 255 || (hole_variant < 0 || (hole_variant > 24 && hole_variant != 0xFF))) {
        return 1; /* malformed input from the caller -- still "handled", just nothing is sent */
    }
    if (!pcfa_scene_is_town()) {
        return 0; /* the m_tag_ovl.c client seam shows vanilla's "can't do that" warning */
    }
    if (s_bury_pending.valid) {
        return 1; /* one already in flight -- defensive no-op, same posture as drop's own equivalent */
    }

    if (!pcnetgame_capture_owner_stamp(&stamp)) {
        printf("[NET][BURY] no gameplay save loaded -- bury at (%d,%d) not sent\n", ut_x, ut_z);
        return 0;
    }
    if (Now_Private->inventory.pockets[pocket_slot_idx] != (mActor_name_t)claimed_item) {
        printf("[NET][BURY] pocket slot %d holds 0x%04X, not the claimed 0x%04X -- bury not sent\n", pocket_slot_idx,
               (unsigned)Now_Private->inventory.pockets[pocket_slot_idx], (unsigned)claimed_item);
        return 0;
    }

    s_bury_committed.valid = 0; /* M9-D G2-3: a new bury supersedes the retained previous claim */
    s_bury_pending.valid = 1;
    s_bury_pending.request_id = s_next_bury_request_id++;
    s_bury_pending.owner = stamp;
    s_bury_pending.pocket_slot_idx = (uint8_t)pocket_slot_idx;
    s_bury_pending.ut_x = (uint8_t)ut_x;
    s_bury_pending.ut_z = (uint8_t)ut_z;
    s_bury_pending.claimed_item = (uint16_t)claimed_item;
    s_bury_pending.hole_variant = (uint8_t)hole_variant;
    s_bury_pending.timeout_accum = 0.0f;
    s_bury_pending.retry_count = 0;
    s_bury_pending.unsent = 0;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_BURY_REQUEST;
    msg.pocket_slot_idx = s_bury_pending.pocket_slot_idx;
    msg.ut_x = s_bury_pending.ut_x;
    msg.ut_z = s_bury_pending.ut_z;
    msg.claimed_item = s_bury_pending.claimed_item;
    msg.hole_variant = s_bury_pending.hole_variant;
    msg.request_id = s_bury_pending.request_id;
    if (!pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
        s_bury_pending.unsent = 1;
        s_bury_pending.timeout_accum = PC_NETGAME_BURY_TIMEOUT_60FPS_FRAMES;
    }
    return 1;
}

/* World Ecology T3: see pc_net_game.h's own doc. Single-ownership host-local bury -- routes the host's
 * own local bury action through the SAME pcnetgame_validate_and_resolve_bury()/
 * pcnetgame_resolve_bury_outcome() logic a remote peer's request uses (is_host_local=1: skips the
 * reach/IN_TOWN checks), runs synchronously (no reservation/CONFIRM round trip needed for a local call),
 * and on success clears the HOST's OWN pocket slot itself. Returns 1 iff it actually handled the bury
 * (whether or not it turned out to be a legal one -- the caller must NOT fall back to vanilla's local
 * mutation on a 0..1 either way once this returns 1 for a genuine HOST role, mirroring
 * pc_net_game_request_bury()'s own "returns 1 == handled" convention); returns 0 for single-player or a
 * client, or for malformed input, in which case the caller falls back to vanilla, unmodified. */
int pc_net_game_host_local_bury(int pocket_slot_idx, int ut_x, int ut_z, int claimed_item, int hole_variant) {
    PCNetPlayerContext ctx;
    int acre = 0, tile = 0;
    uint16_t raw_item = 0;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return 0;
    }
    if (!pcfa_scene_is_town()) {
        return 0; /* the host itself is indoors -- ut_x/ut_z from a room are not town coordinates */
    }
    if (pocket_slot_idx < 0 || pocket_slot_idx >= mPr_POCKETS_SLOT_COUNT || ut_x < 0 || ut_x > 255 || ut_z < 0 ||
        ut_z > 255 || (hole_variant < 0 || (hole_variant > 24 && hole_variant != 0xFF))) {
        return 0;
    }
    if (Now_Private == NULL) {
        /* no gameplay save loaded -- defensive; a host-local bury can only ever be requested from live
           gameplay, so this should be unreachable in practice */
        return 0;
    }
    if (Now_Private->inventory.pockets[pocket_slot_idx] != (mActor_name_t)claimed_item) {
        return 0; /* the slot does not hold what the caller claims -- never trust stale menu state */
    }

    pcnetgame_capture_local_context(&ctx);
    /* peer is unused whenever is_host_local (the first argument) is 1 -- see
       pcnetgame_validate_and_resolve_bury()'s own doc -- so (PCNetPeerId)0 here is a meaningless
       placeholder, never actually read. */
    if (!pcnetgame_validate_and_resolve_bury(1, (PCNetPeerId)0, (uint8_t)pocket_slot_idx, (uint8_t)ut_x,
                                             (uint8_t)ut_z, (mActor_name_t)claimed_item, (uint8_t)hole_variant, &acre,
                                             &tile, &raw_item)) {
        return 0; /* not a legal bury -- caller falls back to vanilla's own "can't do that" handling */
    }

    {
        mActor_name_t resolved = (mActor_name_t)EMPTY_NO;
        int action = pcnetgame_resolve_bury_outcome(&ctx, (mActor_name_t)claimed_item, raw_item, (uint8_t)hole_variant,
                                                    &resolved);
        int dep_on = (action == PC_NETGAME_BURY_ACTION_BURY);

        if (!pcfa_set_tile(acre, tile, (uint16_t)resolved)) {
            return 0;
        }
        if (dep_on) {
            pcfa_set_deposit(acre, tile, 1);
        }
        mPr_SetPossessionItem(Now_Private, pocket_slot_idx, (mActor_name_t)EMPTY_NO, mPr_ITEM_COND_NORMAL);
        pcnetgame_host_flush_mask((uint32_t)1u << acre);
        printf("[NET][BURY] host: local bury at tile (%d,%d) 0x%04X -> 0x%04X (action=%d dep=%d)\n", ut_x, ut_z,
               (unsigned)raw_item, (unsigned)resolved, action, dep_on);
    }
    return 1;
}

/* Bug 3 fix: see this function's own doc in pc_net_game.h. Mirrors the tile-validity half of
 * pcnetgame_validate_and_resolve_bury() exactly (raw tile value must be HOLE00..24 or HOLE_SHINE) --
 * deliberately NOT the reservation check (the caller already has its own precedent for that, see
 * mTG_host_put_tile_free()) and NOT any of the other bury-specific checks (pocket slot, claimed item,
 * deposit state) that don't depend on the tile's own current value. */
int pc_net_game_host_bury_tile_is_valid(int ut_x, int ut_z) {
    int acre, tile;
    uint16_t raw_value;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return 1; /* single-player/client: never this function's business, caller proceeds as before */
    }
    if (!pcfa_town_ut_to_acre_tile(ut_x, ut_z, &acre, &tile)) {
        return 1; /* can't resolve -- fail open, exactly like every other best-effort read in this file */
    }
    if (!pcfa_get_tile(acre, tile, &raw_value)) {
        return 1;
    }
    return ITEM_IS_HOLE(raw_value) || raw_value == (uint16_t)HOLE_SHINE;
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

    /* N3 finalization pass (Bug 2 fix): invalidate this slot's NPC_STATE dirty-check shadow so the
     * new occupant's first state sample is always broadcast, even if its is_home/hide/forced_type
     * values happen to coincidentally equal whatever the previous occupant last had. cached_npc_id
     * also gets reset here in case the arrival's own npc_id somehow matched an even-older shadow entry
     * (harmless belt-and-suspenders; the npc_id mismatch check in pc_net_game_notify_npc_state()
     * would already catch that case). */
    memset(&s_npc_state_shadow[slot], 0, sizeof(s_npc_state_shadow[slot]));

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

    /* N3 finalization pass (Bug 2 fix): invalidate this slot's NPC_STATE dirty-check shadow now that
     * it no longer represents a live occupant, so whichever villager takes this slot next always gets
     * its initial state broadcast (see pc_net_game_notify_villager_arrival()'s matching reset, and
     * pc_net_game_notify_npc_state()'s doc comment for the full Bug 2 rationale). */
    memset(&s_npc_state_shadow[slot], 0, sizeof(s_npc_state_shadow[slot]));

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

        /* N3 Channel B: best-effort hide (see PCNetGameVillagerSlotWire's own doc comment on this
         * approximation) and the current forced-schedule override, if this slot has a registered
         * schedule area (see FIX S3 -- it might not, in the narrow window before the very first
         * ARRIVAL/SNAPSHOT-apply's re-registration call has run on THIS host, which cannot happen for
         * a slot the host itself is about to snapshot since the host applied its own arrival
         * synchronously long before any snapshot could be built for it). */
        slot->hide = slot->is_home;
        {
            mNPS_schedule_c* sched_p = mNPS_get_schedule_area(&animal->id);
            if (sched_p != NULL) {
                int forced_timer = sched_p->forced_timer;
                slot->forced_type = sched_p->forced_type;
                slot->forced_timer_remaining =
                    (uint16_t)((forced_timer < 0) ? 0 : (forced_timer > 65535 ? 65535 : forced_timer));
            }
        }
    }
}

/* ---- N2 villager movement sync ---- */

/* Host side. See pc_net_game.h's doc comment on pc_net_game_notify_npc_move() for full scope and
 * identity rationale. Called from ac_npc_move.c_inc once per frame per currently-instantiated,
 * on-screen villager NPC_ACTOR, right after the unmodified host physics/AI has already finished
 * resolving this frame's position/facing -- this function only ever SAMPLES and (throttled) SENDS,
 * never mutates anything decomp-side. */
void pc_net_game_notify_npc_move(int slot, uint16_t npc_id, float pos_x, float pos_y, float pos_z,
                                 int16_t facing_angle, uint8_t action_type) {
    PCNetGameNpcMoveMsg msg;
    int i;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (slot < 0 || slot >= ANIMAL_NUM_MAX) {
        return; /* malformed input from the caller -- defensive */
    }
    if (pc_net_game_host_ready_peer_count() == 0) {
        return; /* nobody to send to -- skip the throttle tick too, so a peer that joins later gets
                 * this slot's very first sample promptly rather than mid-cycle */
    }
    if (gamePT == NULL || !graph_dt_period_elapsed(gamePT, &s_npc_move_send_accum[slot],
                                                    PC_NETGAME_NPC_MOVE_SEND_PERIOD_60FPS_FRAMES)) {
        return; /* not this slot's turn yet */
    }
    if (!pcnetgame_pos_valid(pos_x, pos_y, pos_z)) {
        if (g_pc_verbose) {
            printf("[NET][NPC][DIAG] host: dropped invalid NPC_MOVE sample slot %d pos=(%g,%g,%g)\n", slot,
                   (double)pos_x, (double)pos_y, (double)pos_z);
        }
        return;
    }

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_NPC_MOVE;
    msg.slot = (uint8_t)slot;
    msg.action_type = action_type;
    msg.npc_id = npc_id;
    msg.frame = ++s_npc_move_send_counter;
    msg.pos_x = pos_x;
    msg.pos_y = pos_y;
    msg.pos_z = pos_z;
    msg.facing_angle = facing_angle;

    {
        static uint8_t s_logged_send[ANIMAL_NUM_MAX];
        if (!s_logged_send[slot]) {
            s_logged_send[slot] = 1;
            printf("[NET][NPC] host: NPC_MOVE stream started slot %d npc_id 0x%04X frame %u pos=(%.1f,%.1f,%.1f)\n",
                   slot, (unsigned)npc_id, (unsigned)msg.frame, (double)pos_x, (double)pos_y, (double)pos_z);
        }
    }

    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
            pc_net_send((PCNetPeerId)i, PC_NET_UNRELIABLE, &msg, (uint16_t)sizeof(msg));
        }
    }
}

/* Client side: ingest one NPC_MOVE. Stale/duplicate/reordered (frame <= last accepted for this slot)
 * is dropped -- exactly PCNetMoveMsg.frame's own convention (see pcnetgame_handle_host_move()'s
 * sibling doc comment). Identity (npc_id vs. the receiver's own live population state) is
 * DELIBERATELY NOT checked here -- see pc_net_game_get_npc_move_pose()'s doc comment for why the
 * authoritative check belongs at consume time, against the caller's live Save_t.animals[slot], not
 * at receive time against whatever this ring cell happens to already hold. */
static void pcnetgame_handle_client_npc_move(const PCNetGameNpcMoveMsg* in) {
    PCNetNpcMoveSlot* ns;
    PCNetNpcMoveSnapshot* dst;

    if (in->slot >= ANIMAL_NUM_MAX) {
        return; /* malformed */
    }
    if (!pcnetgame_pos_valid(in->pos_x, in->pos_y, in->pos_z)) {
        if (g_pc_verbose) {
            printf("[NET][NPC][DIAG] client: dropped invalid NPC_MOVE sample slot %u\n", (unsigned)in->slot);
        }
        return;
    }

    ns = &s_npc_move_slot[in->slot];
    if (ns->have_frame && in->frame <= ns->last_accepted_frame) {
        return; /* stale, duplicate, or reordered relative to this slot's own stream */
    }
    if (!ns->have_frame) {
        printf("[NET][NPC] client: NPC_MOVE first sample accepted slot %u npc_id 0x%04X frame %u pos=(%.1f,%.1f,%.1f)\n",
               (unsigned)in->slot, (unsigned)in->npc_id, (unsigned)in->frame, (double)in->pos_x, (double)in->pos_y,
               (double)in->pos_z);
    }
    ns->have_frame = 1;
    ns->last_accepted_frame = in->frame;
    ns->cached_npc_id = in->npc_id;
    ns->last_action_type = in->action_type;

    if (gamePT == NULL) {
        return; /* no frame-time source to timestamp this sample with -- drop rather than mistimestamp
                 * (should not happen for an already-READY client, but never crash on it) */
    }

    dst = &ns->snapshots[ns->snapshot_head];
    dst->recv_local_frame = graph_dt_frame_time(gamePT);
    dst->pos_x = in->pos_x;
    dst->pos_y = in->pos_y;
    dst->pos_z = in->pos_z;
    dst->facing_angle = in->facing_angle;

    ns->snapshot_head = (ns->snapshot_head + 1) % PC_NETGAME_NPC_MOVE_RING_SIZE;
    if (ns->snapshot_count < PC_NETGAME_NPC_MOVE_RING_SIZE) {
        ns->snapshot_count++;
    }
}

/* Delayed two-snapshot interpolation over one slot's ring -- the SAME algorithm/contract as
 * pc_remote_player_interpolate() (pc_remote_player.c): target_time and every snapshot's
 * recv_local_frame share this process's own graph_dt_frame_time() domain; returns 0 if the slot has
 * no snapshots yet, else fills *out_pos_x/y/z, *out_facing_angle and *out_moving and returns 1
 * (interpolated between straddling snapshots, held at the newest if target_time is past all of them,
 * held at the oldest if target_time is before all of them, snapped straight to the newer position on
 * an implausible jump). Deliberately re-implemented locally (not shared with pc_remote_player.c)
 * since that function is `static` to a different translation unit and operates on
 * PCRemoteMoveSnapshot, a distinct type with a different ring-size constant -- duplicating this small,
 * self-contained algorithm is clearer here than introducing a cross-file generic. */
static int pcnetgame_npc_move_interpolate(const PCNetNpcMoveSlot* ns, double target_time, float* out_pos_x,
                                          float* out_pos_y, float* out_pos_z, int16_t* out_facing_angle,
                                          int* out_moving) {
    int oldest, i, n;
    const PCNetNpcMoveSnapshot* s0 = NULL;
    const PCNetNpcMoveSnapshot* s1 = NULL;

    n = ns->snapshot_count;
    if (n == 0) {
        return 0;
    }

    oldest = (ns->snapshot_head - n + PC_NETGAME_NPC_MOVE_RING_SIZE) % PC_NETGAME_NPC_MOVE_RING_SIZE;
    for (i = 0; i < n; i++) {
        int idx = (oldest + i) % PC_NETGAME_NPC_MOVE_RING_SIZE;
        const PCNetNpcMoveSnapshot* snap = &ns->snapshots[idx];
        if (snap->recv_local_frame <= target_time) {
            s0 = snap;
        } else if (s1 == NULL) {
            s1 = snap;
        }
    }

    *out_moving = 0;

    if (s0 != NULL && s1 != NULL) {
        double span = s1->recv_local_frame - s0->recv_local_frame;
        float t = (span > 0.0) ? (float)((target_time - s0->recv_local_frame) / span) : 1.0f;
        float dx = s1->pos_x - s0->pos_x;
        float dy = s1->pos_y - s0->pos_y;
        float dz = s1->pos_z - s0->pos_z;
        float dist_sq = dx * dx + dy * dy + dz * dz;
        s16 angle_delta;

        if (t < 0.0f) t = 0.0f;
        if (t > 1.0f) t = 1.0f;

        if (dist_sq > PC_NETGAME_NPC_MOVE_TELEPORT_DIST_SQ) {
            *out_pos_x = s1->pos_x;
            *out_pos_y = s1->pos_y;
            *out_pos_z = s1->pos_z;
            *out_moving = 0; /* a teleport/discontinuity, not a walk -- see Part 8 of the design */
        } else {
            *out_pos_x = s0->pos_x + dx * t;
            *out_pos_y = s0->pos_y + dy * t;
            *out_pos_z = s0->pos_z + dz * t;
            *out_moving = dist_sq > PC_NETGAME_NPC_MOVE_MOVING_DIST_SQ;
        }

        /* Shortest-path angle interpolation -- same (s16) wraparound idiom as
         * pc_remote_player_interpolate() / Actor_player_look_direction_check(). */
        angle_delta = (s16)(s1->facing_angle - s0->facing_angle);
        *out_facing_angle = (s16)(s0->facing_angle + (s16)((f32)angle_delta * t));
    } else if (s0 != NULL) {
        *out_pos_x = s0->pos_x;
        *out_pos_y = s0->pos_y;
        *out_pos_z = s0->pos_z;
        *out_facing_angle = s0->facing_angle;
    } else { /* s1 != NULL */
        *out_pos_x = s1->pos_x;
        *out_pos_y = s1->pos_y;
        *out_pos_z = s1->pos_z;
        *out_facing_angle = s1->facing_angle;
    }

    return 1;
}

/* See pc_net_game.h's doc comment for the full contract, in particular the slot-reuse guard. */
int pc_net_game_get_npc_move_pose(int slot, uint16_t expected_npc_id, float* out_pos_x, float* out_pos_y,
                                  float* out_pos_z, int16_t* out_facing_angle, int* out_moving,
                                  uint8_t* out_action_type) {
    PCNetNpcMoveSlot* ns;
    double target_time;
    int ok;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (slot < 0 || slot >= ANIMAL_NUM_MAX) {
        return 0;
    }

    ns = &s_npc_move_slot[slot];
    if (!ns->have_frame) {
        return 0; /* nothing received yet for this slot */
    }
    if (ns->cached_npc_id != expected_npc_id) {
        /* Slot-reuse guard: the identity this ring cell was last filled under no longer matches the
         * caller's own live population state (a departure/arrival already applied this slot
         * differently). Discard the stale data now, so a coincidental future npc_id match can never
         * resurrect it. */
        printf("[NET][NPC] client: rejected NPC_MOVE pose slot %d -- identity mismatch (cached npc_id "
               "0x%04X, current 0x%04X) -- slot reused, discarding stale ring\n",
               slot, (unsigned)ns->cached_npc_id, (unsigned)expected_npc_id);
        memset(ns, 0, sizeof(*ns));
        return 0;
    }
    if (gamePT == NULL) {
        return 0; /* no frame-time source to build a presentation timeline from */
    }

    target_time = graph_dt_frame_time(gamePT) - PC_NETGAME_NPC_MOVE_INTERP_DELAY_FRAMES;
    ok = pcnetgame_npc_move_interpolate(ns, target_time, out_pos_x, out_pos_y, out_pos_z, out_facing_angle,
                                        out_moving);
    if (ok && out_action_type != NULL) {
        /* N3 Channel A: action_type is coarse and carried as-is from the latest accepted sample --
         * never interpolated (see PCNetNpcMoveSlot::last_action_type's doc comment). */
        *out_action_type = ns->last_action_type;
    }
    return ok;
}

/* ---- N3 Channel B: villager is_home/hide/forced-schedule sync ---- */

/* Host side. Called UNCONDITIONALLY (no caller-side role check needed, matching
 * pc_net_game_notify_villager_arrival()'s own established pattern) once per frame per real,
 * Save_t.animals[]-backed villager NPC_ACTOR -- from BOTH aNPC_actor_move_show_before() (visible) and
 * aNPC_actor_move_hide() (hidden/culled), since this state must be tracked and broadcast regardless of
 * hide status (see ac_npc_move.c_inc's aNPC_pc_host_check_state()). A no-op for single-player/client.
 * Internally dirty-checks against s_npc_state_shadow[slot] and only sends on an actual is_home/hide/
 * forced_type/forced-active change -- NOT on forced_timer's continuous per-frame countdown, which
 * would otherwise make this fire every single frame while any forced override is active. A client that
 * needs the exact forced_timer_remaining (e.g. a late joiner) gets it from the extended
 * VILLAGER_SNAPSHOT instead (see pcnetgame_build_villager_snapshot()), which always carries the
 * CURRENT value regardless of this change-detector. */
void pc_net_game_notify_npc_state(int slot, uint16_t npc_id, uint8_t is_home, uint8_t hide, uint8_t forced_type,
                                  int forced_timer) {
    PCNetNpcStateShadow* sh;
    PCNetGameNpcStateMsg msg;
    uint8_t forced_active;
    uint16_t timer_clamped;
    int i;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (slot < 0 || slot >= ANIMAL_NUM_MAX) {
        return; /* malformed input from the caller -- defensive */
    }

    is_home = is_home ? 1 : 0;
    hide = hide ? 1 : 0;
    forced_active = (forced_timer > 0) ? 1 : 0;
    timer_clamped = (forced_timer < 0) ? 0 : (forced_timer > 65535 ? 65535 : (uint16_t)forced_timer);

    sh = &s_npc_state_shadow[slot];
    if (sh->have_sent && sh->cached_npc_id == npc_id && sh->is_home == is_home && sh->hide == hide &&
        sh->forced_type == forced_type && sh->forced_active == forced_active) {
        return; /* unchanged since the last broadcast for this slot AND same occupant -- nothing to send */
    }

    sh->have_sent = 1;
    sh->cached_npc_id = npc_id;
    sh->is_home = is_home;
    sh->hide = hide;
    sh->forced_type = forced_type;
    sh->forced_active = forced_active;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_NPC_STATE;
    msg.slot = (uint8_t)slot;
    msg.npc_id = npc_id;
    msg.is_home = is_home;
    msg.hide = hide;
    msg.forced_type = forced_type;
    msg.forced_timer_remaining = timer_clamped;
    msg.state_seq = ++s_npc_state_seq_counter;

    if (g_pc_verbose) {
        printf("[NET][NPC] host: NPC_STATE change slot %d npc_id 0x%04X is_home=%d hide=%d forced_type=%d "
               "forced_timer=%u seq=%u\n",
               slot, (unsigned)npc_id, is_home, hide, forced_type, (unsigned)timer_clamped,
               (unsigned)msg.state_seq);
    }

    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_host_peer_link[i] == PC_NETGAME_LINK_READY) {
            pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
        }
    }
}

/* Client side: ingest one NPC_STATE. Applies the strict per-slot `state_seq > last_applied` guard
 * (mirrors WORLD_META's own s_client_meta_seq convention) -- identity (npc_id) is deliberately NOT
 * checked here, same rationale as pcnetgame_handle_client_npc_move(): the authoritative check belongs
 * at CONSUME time (pc_net_game_get_npc_state()) against the caller's live Save_t.animals[slot].id.npc_id,
 * never at receive time against whatever this cell happens to already hold. */
static void pcnetgame_handle_client_npc_state(const PCNetGameNpcStateMsg* in) {
    PCNetNpcStateSlot* ns;

    if (in->slot >= ANIMAL_NUM_MAX) {
        return; /* malformed */
    }

    ns = &s_npc_state_slot[in->slot];
    if (ns->have_state && in->state_seq <= ns->last_applied_seq) {
        return; /* stale, duplicate, or reordered relative to this slot's own stream */
    }

    ns->have_state = 1;
    ns->last_applied_seq = in->state_seq;
    ns->cached_npc_id = in->npc_id;
    ns->is_home = in->is_home;
    ns->hide = in->hide;
    ns->forced_type = in->forced_type;
    ns->forced_timer_remaining = in->forced_timer_remaining;

    if (g_pc_verbose) {
        printf("[NET][NPC] client: NPC_STATE applied slot %u npc_id 0x%04X is_home=%d hide=%d forced_type=%d "
               "forced_timer=%u seq=%u\n",
               (unsigned)in->slot, (unsigned)in->npc_id, in->is_home, in->hide, in->forced_type,
               (unsigned)in->forced_timer_remaining, (unsigned)in->state_seq);
    }
}

/* Client only: fills the out params with the latest applied NPC_STATE for slot, gated by the SAME
 * slot-reuse identity guard pc_net_game_get_npc_move_pose() uses (expected_npc_id is the caller's own
 * live Save_t.animals[slot].id.npc_id). Returns 1 if a state was written, 0 -- meaning the caller must
 * not touch is_home/hide/forced_type/forced_timer at all -- if: not a READY client, out-of-range slot,
 * nothing received yet for this slot, or an npc_id mismatch (slot-reuse; the stale cached state is
 * discarded so it can never resurrect under a coincidental future match). */
int pc_net_game_get_npc_state(int slot, uint16_t expected_npc_id, uint8_t* out_is_home, uint8_t* out_hide,
                              uint8_t* out_forced_type, uint16_t* out_forced_timer_remaining) {
    PCNetNpcStateSlot* ns;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (slot < 0 || slot >= ANIMAL_NUM_MAX) {
        return 0;
    }

    ns = &s_npc_state_slot[slot];
    if (!ns->have_state) {
        return 0;
    }
    if (ns->cached_npc_id != expected_npc_id) {
        printf("[NET][NPC] client: rejected NPC_STATE slot %d -- identity mismatch (cached npc_id 0x%04X, "
               "current 0x%04X) -- slot reused, discarding stale state\n",
               slot, (unsigned)ns->cached_npc_id, (unsigned)expected_npc_id);
        memset(ns, 0, sizeof(*ns));
        return 0;
    }

    *out_is_home = ns->is_home;
    *out_hide = ns->hide;
    *out_forced_type = ns->forced_type;
    *out_forced_timer_remaining = ns->forced_timer_remaining;
    return 1;
}

/* Friendship/mail sync milestone: builds a PCNetGameFriendshipSnapshotEntryMsg for one (slot,
 * memory_idx) pair directly from the host's current Save_t.animals[] -- returns 0 (nothing built)
 * if that Anmmem_c entry is currently free (mNpc_CheckFreeAnimalMemory()), 1 otherwise. `slot` is
 * NOT range-checked here (both call sites already keep it in [0, ANIMAL_NUM_MAX)). */
static int pcnetgame_build_friendship_snapshot_entry(PCNetGameFriendshipSnapshotEntryMsg* fe, int slot,
                                                      int memory_idx) {
    Animal_c* animal = Save_GetPointer(animals[slot]);
    Anmmem_c* memory = &animal->memories[memory_idx];

    if (mNpc_CheckFreeAnimalMemory(memory) == TRUE) {
        return 0;
    }

    memset(fe, 0, sizeof(*fe));
    fe->msg_type = (uint8_t)PC_NETGAME_MSG_FRIENDSHIP_SNAPSHOT_ENTRY;
    fe->slot = (uint8_t)slot;
    fe->friendship = (int8_t)memory->friendship;
    memcpy(&fe->letter_info, &memory->letter_info, sizeof(fe->letter_info));
    memcpy(fe->player_name, memory->memory_player_id.player_name, PC_NETGAME_NAME_LEN);
    memcpy(fe->land_name, memory->memory_player_id.land_name, PC_NETGAME_LAND_LEN);
    fe->player_id = memory->memory_player_id.player_id;
    fe->land_id = memory->memory_player_id.land_id;
    fe->world_seq = s_world_seq;
    fe->has_letter = memory->letter_info.exists ? 1 : 0;
    _Static_assert(sizeof(fe->letter) == sizeof(memory->letter), "PCNetGameFriendshipSnapshotEntryMsg.letter size drifted from Anmplmail_c");
    memcpy(fe->letter, &memory->letter, sizeof(fe->letter));
    return 1;
}

/* Host side: FRIENDSHIP_REQUEST from a READY peer -- see mNpc_AddFriendship()'s doc comment
 * (m_npc.c) for the full contract. Processed only for a READY peer with a valid host-derived binding AND only
 * while the host world is ready (s_host_world_ready); otherwise dropped silently (fire-and-forget: the client
 * keeps no pending state). Resolves (find-or-create) the memory slot from the CACHED validated bound_pid
 * captured at bind time (never the client's claim, never a live private_data reread), applies the SAME
 * mNpc_AddFriendship() the host's own local interactions use, then broadcasts the result to every READY client
 * including the requester. Also ignored (no reply) for an out-of-range slot or an unoccupied villager slot. */
static void pcnetgame_handle_host_friendship_request(PCNetPeerId peer, const PCNetGameFriendshipRequestMsg* in) {
    PersonalID_c pid;
    int friendship;

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }
    if (!s_host_world_ready) {
        /* Stage 1A hardening: same predicate the other authoritative host handlers use. The client sends this
         * fire-and-forget (no pending state, no reply expected), and an unresolvable request is already dropped
         * silently, so ignoring it while the host save is not loaded is safe. */
        if (g_pc_verbose) {
            printf("[NET][IDENTITY] host: peer %d %s ignored -- host world not ready\n", (int)peer, "FRIENDSHIP_REQUEST");
        }
        return;
    }
    if (in->slot >= ANIMAL_NUM_MAX) {
        return;
    }

    /* M9 identity Stage 1A: the key is the HOST's own saved PersonalID of the resident this peer was validated
     * and bound to at READY -- no longer the peer's cached IDENTITY claim (ready_*). */
    if (!pcnetgame_host_bound_personal_id(peer, &pid)) {
        return; /* should not happen -- a READY peer is always bound */
    }

    friendship = mNpc_PcHostResolveAndApplyFriendshipDelta((int)in->slot, &pid, (int)in->delta);
    if (friendship < 0) {
        if (g_pc_verbose) {
            printf("[NET][NPC] host: peer %d FRIENDSHIP_REQUEST slot %u could not be applied (bad/unoccupied slot "
                   "or no free memory)\n", (int)peer, (unsigned)in->slot);
        }
        return;
    }

    /* mNpc_PcHostResolveAndApplyFriendshipDelta() calls the real mNpc_AddFriendship() internally,
     * which (running here on the HOST) already broadcasts PC_NETGAME_MSG_FRIENDSHIP_UPDATE itself
     * via its own post-apply pc_net_game_notify_local_friendship_change() call -- see that
     * function's doc comment. Calling it again here would just double-send the same result. */
    printf("[NET][NPC] host: peer %d friendship slot %u delta %d -> %d\n", (int)peer, (unsigned)in->slot,
           (int)in->delta, friendship);
}

/* Client side: FRIENDSHIP_UPDATE from the host. Strict `>` staleness rule -- see
 * PCNetGameFriendshipUpdateMsg's own doc comment. */
static void pcnetgame_handle_client_friendship_update(const PCNetGameFriendshipUpdateMsg* in) {
    PersonalID_c pid;

    if (!pcnetgame_client_can_apply_world()) {
        return;
    }
    if (in->world_seq <= s_client_friendship_seq) {
        if (g_pc_verbose) {
            printf("[NET][NPC] client: stale FRIENDSHIP_UPDATE slot %u world_seq %u <= %u ignored\n",
                   (unsigned)in->slot, (unsigned)in->world_seq, (unsigned)s_client_friendship_seq);
        }
        return;
    }
    memcpy(pid.player_name, in->player_name, PC_NETGAME_NAME_LEN);
    memcpy(pid.land_name, in->land_name, PC_NETGAME_LAND_LEN);
    pid.player_id = in->player_id;
    pid.land_id = in->land_id;
    mNpc_PcApplyFriendshipUpdate((int)in->slot, &pid, (int)in->friendship);
    s_client_friendship_seq = in->world_seq;
    printf("[NET][NPC] client: friendship slot %u -> %d (world_seq %u)\n", (unsigned)in->slot,
           (int)in->friendship, (unsigned)in->world_seq);
}

/* Client side: one FRIENDSHIP_SNAPSHOT_ENTRY (late-join/reconnect). `>=` rule -- see the struct's
 * own doc comment. Only advances s_client_friendship_seq (never regresses it), matching
 * VILLAGER_SNAPSHOT's own convention. */
static void pcnetgame_handle_client_friendship_snapshot_entry(const PCNetGameFriendshipSnapshotEntryMsg* in) {
    PersonalID_c pid;

    if (!s_client_snap_active || !pcnetgame_client_can_apply_world()) {
        return;
    }
    if (in->world_seq < s_client_friendship_seq) {
        if (g_pc_verbose) {
            printf("[NET][NPC] client: stale FRIENDSHIP_SNAPSHOT_ENTRY slot %u world_seq %u < %u ignored\n",
                   (unsigned)in->slot, (unsigned)in->world_seq, (unsigned)s_client_friendship_seq);
        }
        return;
    }
    memcpy(pid.player_name, in->player_name, PC_NETGAME_NAME_LEN);
    memcpy(pid.land_name, in->land_name, PC_NETGAME_LAND_LEN);
    pid.player_id = in->player_id;
    pid.land_id = in->land_id;
    mNpc_PcApplyFriendshipUpdate((int)in->slot, &pid, (int)in->friendship);
    if (in->has_letter) {
        mNpc_PcApplyMailSnapshotEntry((int)in->slot, &pid, in->letter_info, in->letter, sizeof(in->letter));
    }
    if (in->world_seq > s_client_friendship_seq) {
        s_client_friendship_seq = in->world_seq;
    }
}

/* Host side: MAIL_REQUEST from a READY peer -- see mNpc_SendMailtoNpc()'s doc comment (m_npc.c)
 * for the full contract. Processed only for a READY peer with a valid host-derived binding AND only while the
 * host world is ready (s_host_world_ready); otherwise dropped silently (fire-and-forget: the client keeps no
 * pending state; the caller already checks pc_net_game_role() == CLIENT before ever sending one). The host
 * OVERWRITES any client-supplied sender PersonalID with the CACHED validated bound_pid captured at bind time
 * (never the client's claim, never a live private_data reread) before the villager lookup. */
static void pcnetgame_handle_host_mail_request(PCNetPeerId peer, const PCNetGameMailRequestMsg* in) {
    Mail_c mail;
    int slot;
    int friendship = 0;
    uint8_t letter_info = 0;
    uint8_t letter[258];

    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }

    if (!s_host_world_ready) {
        /* Stage 1A hardening: same predicate the other authoritative host handlers use. The client sends this
         * fire-and-forget (no pending state, no reply expected), and an unresolvable request is already dropped
         * silently, so ignoring it while the host save is not loaded is safe. */
        if (g_pc_verbose) {
            printf("[NET][IDENTITY] host: peer %d %s ignored -- host world not ready\n", (int)peer, "MAIL_REQUEST");
        }
        return;
    }

    _Static_assert(sizeof(in->mail) == sizeof(Mail_c), "PCNetGameMailRequestMsg.mail size drifted from Mail_c");
    memcpy(&mail, in->mail, sizeof(mail));

    /* M9 identity Stage 1A: the sender PersonalID is not taken from the payload. It is overwritten with the
     * HOST's own saved PersonalID of the resident this peer is bound to (an honest client's value is
     * identical -- it sends its own Now_Private->player_ID, which equals the host's record). Everything
     * downstream (villager memory key, MAIL_DELIVERED broadcast) therefore sees the validated sender. */
    {
        PersonalID_c bound_pid;
        if (!pcnetgame_host_bound_personal_id(peer, &bound_pid)) {
            return; /* should not happen -- a READY peer is always bound */
        }
        if (memcmp(&mail.header.sender.personalID, &bound_pid, sizeof(bound_pid)) != 0) {
            printf("[NET][IDENTITY] host: peer %d MAIL_REQUEST sender PersonalID differs from its bound resident %d -- "
                   "overwritten\n", (int)peer, s_host_peer[peer].bound_resident_idx);
        }
        mPr_CopyPersonalID(&mail.header.sender.personalID, &bound_pid);
    }

    slot = mNpc_PcApplyMailToVillagerMemory(&mail, &friendship, &letter_info, letter, sizeof(letter));
    if (slot < 0) {
        if (g_pc_verbose) {
            printf("[NET][NPC] host: peer %d MAIL_REQUEST could not be delivered (unresolvable recipient)\n",
                   (int)peer);
        }
        return;
    }

    printf("[NET][NPC] host: peer %d mail delivered to slot %d (friendship now %d)\n", (int)peer, slot, friendship);
    pc_net_game_notify_local_mail_delivered(slot, mail.header.sender.personalID.player_name,
                                            mail.header.sender.personalID.land_name,
                                            mail.header.sender.personalID.player_id,
                                            mail.header.sender.personalID.land_id, friendship, letter_info, letter);
}

/* Client side: MAIL_DELIVERED from the host. Strict `>` staleness rule, exactly mirroring
 * FRIENDSHIP_UPDATE. */
static void pcnetgame_handle_client_mail_delivered(const PCNetGameMailDeliveredMsg* in) {
    PersonalID_c pid;

    if (!pcnetgame_client_can_apply_world()) {
        return;
    }
    if (in->world_seq <= s_client_friendship_seq) {
        if (g_pc_verbose) {
            printf("[NET][NPC] client: stale MAIL_DELIVERED slot %u world_seq %u <= %u ignored\n",
                   (unsigned)in->slot, (unsigned)in->world_seq, (unsigned)s_client_friendship_seq);
        }
        return;
    }
    memcpy(pid.player_name, in->player_name, PC_NETGAME_NAME_LEN);
    memcpy(pid.land_name, in->land_name, PC_NETGAME_LAND_LEN);
    pid.player_id = in->player_id;
    pid.land_id = in->land_id;
    mNpc_PcApplyFriendshipUpdate((int)in->slot, &pid, (int)in->friendship);
    mNpc_PcApplyMailSnapshotEntry((int)in->slot, &pid, in->letter_info, in->letter, sizeof(in->letter));
    s_client_friendship_seq = in->world_seq;
    printf("[NET][NPC] client: mail delivered to slot %u (friendship now %d, world_seq %u)\n", (unsigned)in->slot,
           (int)in->friendship, (unsigned)in->world_seq);
}

/* See pc_net_game.h. Called from mNpc_AddFriendship()'s client-intercept branch. */
void pc_net_game_request_friendship_delta(int slot, int delta) {
    PCNetGameFriendshipRequestMsg msg;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return;
    }
    if (slot < 0 || slot >= ANIMAL_NUM_MAX) {
        return;
    }
    if (delta < -127 || delta > 127) {
        return; /* malformed input from the caller -- defensive */
    }

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_FRIENDSHIP_REQUEST;
    msg.slot = (uint8_t)slot;
    msg.delta = (int8_t)delta;
    pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
}

/* See pc_net_game.h. Called from mNpc_AddFriendship() (m_npc.c) AFTER it has already applied
 * `friendship` locally on the host (or in single-player, where this is a no-op). */
void pc_net_game_notify_local_friendship_change(int slot, const uint8_t* player_name, const uint8_t* land_name,
                                                uint16_t player_id, uint16_t land_id, int friendship) {
    PCNetGameFriendshipUpdateMsg msg;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (slot < 0 || slot >= ANIMAL_NUM_MAX) {
        return;
    }

    ++s_world_seq;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_FRIENDSHIP_UPDATE;
    msg.slot = (uint8_t)slot;
    msg.friendship = (int8_t)friendship;
    memcpy(msg.player_name, player_name, PC_NETGAME_NAME_LEN);
    memcpy(msg.land_name, land_name, PC_NETGAME_LAND_LEN);
    msg.player_id = player_id;
    msg.land_id = land_id;
    msg.world_seq = s_world_seq;
    pcnetgame_broadcast_villager_msg(&msg, sizeof(msg));
}

/* See pc_net_game.h. Called from mNpc_SendMailtoNpc()'s client-intercept branch. The host
 * re-resolves the recipient from `mail`'s own header -- `recipient_anm_idx` is accepted only for a
 * cheap early "does this even look like a villager" check by the caller and is otherwise unused.
 * `mail_size` must be exactly sizeof(Mail_c) (the caller passes sizeof(*mail)); kept as a plain
 * (void pointer, size) pair, not Mail_c*, so pc_net_game.h's declaration of this function stays
 * decomp-independent, matching every other function in that header. */
void pc_net_game_request_mail_delivery(int recipient_anm_idx, const void* mail, size_t mail_size) {
    PCNetGameMailRequestMsg msg;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return;
    }
    (void)recipient_anm_idx;
    if (mail_size != sizeof(msg.mail)) {
        return; /* defensive: caller/decomp Mail_c size mismatch -- never send a truncated/garbage mail */
    }

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_MAIL_REQUEST;
    memcpy(msg.mail, mail, mail_size);
    pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg));
}

/* See pc_net_game.h. Called from mNpc_PcApplyMailToVillagerMemory() (m_npc.c) AFTER the host has
 * already applied the delivery locally (or, in single-player, never -- mNpc_SendMailtoNpc()'s own
 * unmodified body runs directly and this notify function is simply never reached). */
void pc_net_game_notify_local_mail_delivered(int slot, const uint8_t* player_name, const uint8_t* land_name,
                                             uint16_t player_id, uint16_t land_id, int friendship,
                                             uint8_t letter_info, const uint8_t* letter) {
    PCNetGameMailDeliveredMsg msg;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (slot < 0 || slot >= ANIMAL_NUM_MAX) {
        return;
    }

    ++s_world_seq;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_MAIL_DELIVERED;
    msg.slot = (uint8_t)slot;
    msg.friendship = (int8_t)friendship;
    msg.letter_info = letter_info;
    memcpy(msg.player_name, player_name, PC_NETGAME_NAME_LEN);
    memcpy(msg.land_name, land_name, PC_NETGAME_LAND_LEN);
    msg.player_id = player_id;
    msg.land_id = land_id;
    msg.world_seq = s_world_seq;
    memcpy(msg.letter, letter, sizeof(msg.letter));
    pcnetgame_broadcast_villager_msg(&msg, sizeof(msg));
}

/* ============================================================================================
 * World Ecology Wildlife Sync T0 (authority seam foundation only -- see pc_wildlife_authority.h)
 * ============================================================================================ */

/* See pc_net_game.h's own doc. */
int pc_net_game_authoritative_wildlife_enabled(void) {
    return g_pc_authoritative_wildlife ? 1 : 0;
}

/* See pc_net_game.h's own doc. No "one already in flight" guard, matching TREE_SHAKE's own
 * precedent -- a wade event for a different acre may legitimately arrive while an earlier one is
 * still being processed host-side. */
int pc_net_game_request_wildlife_spawn_trigger(int bx, int bz) {
    PCNetGameWildlifeSpawnTriggerRequestMsg msg;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (!g_pc_authoritative_wildlife) {
        return 0; /* opt-in gate off -- caller falls through to plain vanilla behavior, matching
                     every other single-player/disabled-feature early-return in this file */
    }
    if (bx < 0 || bx > 255 || bz < 0 || bz > 255) {
        return 1; /* swallow silently -- the caller must never run the local decision while
                     host-authoritative, even for input the host would reject anyway */
    }
    if (!pcfa_scene_is_town()) {
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] not in the town scene -- SPAWN_TRIGGER_REQUEST at acre (%d,%d) not sent\n",
                   bx, bz);
        }
        return 1; /* matches pc_net_game_request_tree_shake()'s own scene-check precedent */
    }

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_WILDLIFE_SPAWN_TRIGGER_REQUEST;
    msg.bx = (uint8_t)bx;
    msg.bz = (uint8_t)bz;
    if (!pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] client: SPAWN_TRIGGER_REQUEST at acre (%d,%d) could not be queued "
                   "(window full) -- dropped, not retried (matches TREE_SHAKE's own accepted-gap "
                   "precedent)\n",
                   bx, bz);
        }
    }
    return 1;
}

/* See pc_net_game.h's own doc. Mirrors pc_net_game_host_local_tree_shake()'s own shape exactly. */
void pc_net_game_host_local_wildlife_spawn_trigger(int bx, int bz) {
    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (!g_pc_authoritative_wildlife) {
        return; /* opt-in gate off -- caller falls through to plain vanilla behavior */
    }
    pcwld_host_spawn_trigger(bx, bz);
}

/* ============================================================================================
 * World Ecology Wildlife Sync T-catch (ordinary fish catching), extended by T4 to also cover ordinary
 * bug catching -- see pc_net_game_request_catch_bug()'s own doc (pc_net_game.h) for the T4 design.
 * Tournaments and ant/bee special-case handling remain out of scope (T5).
 * ============================================================================================ */

/* Shared core behind pc_net_game_request_catch_fish() and pc_net_game_request_catch_bug() (pc_net_game.h)
 * -- identical client-side seam for either kind: neither the wire message nor this function's own logic
 * cares which domain `claimed_species` belongs to (that is resolved entirely host-side, against the
 * authoritative record's own `kind`, by pcnetgame_validate_and_commit_catch()) -- the only thing this
 * function itself needs `kind` for is remembering which collection-bit commit pcnetgame_handle_client_
 * catch_result() must run later, on accept. See pc_net_game_request_catch_fish()'s own original doc
 * (pc_net_game.h) for the full interception design (Option A: defer the grant until the host accepts,
 * never award-then-claw-back) -- unchanged by this refactor. No "one already in flight" guard, matching
 * pc_net_game_request_dig_shine()'s own precedent (a stale, superseded s_catch_pending is simply
 * overwritten; the earlier request's own eventual RESULT will then fail the request_id match in the
 * handler below and is harmlessly ignored). Fire-and-forget on a send failure (window full) --
 * deliberately NOT retried, mirroring pc_net_game_request_snowman_build()'s own "dropped, not retried"
 * precedent: by the time any retry could matter, the underlying fish/bug may already be gone (caught by
 * someone else, or despawned), so resending has nothing useful to retry against. */
static int pcnetgame_request_catch_common(uint32_t entity_id, int kind, int claimed_species,
                                          int local_grant_item) {
    PCNetGameCatchRequestMsg msg;
    PCNetGameOwnerStamp stamp;

    if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY) {
        return 0;
    }
    if (!g_pc_authoritative_wildlife) {
        return 0; /* opt-in gate off -- caller falls through to plain vanilla behavior */
    }
    if (entity_id == 0) {
        return 1; /* malformed input from the caller (no stamped entity_id) -- "handled" (the caller's
                     own doc says it then falls back to the ordinary vanilla grant itself), nothing sent */
    }
    if (!pcfa_scene_is_town()) {
        return 0;
    }
    if (!pcnetgame_capture_owner_stamp(&stamp)) {
        printf("[NET][WILDLIFE] client: no gameplay save loaded -- CATCH request for entity %u not sent\n",
               (unsigned)entity_id);
        return 0;
    }

    s_catch_last_outcome.valid = 0; /* M9-D F3: a stale outcome (e.g. a REJECTED left by a disconnect) must not
                                       bleed into this new catch */
    s_catch_pending.valid = 1;
    s_catch_pending.request_id = s_next_catch_request_id++;
    s_catch_pending.entity_id = entity_id;
    s_catch_pending.kind = kind;
    s_catch_pending.claimed_species = claimed_species;
    s_catch_pending.local_grant = (uint16_t)local_grant_item;
    s_catch_pending.owner = stamp;

    memset(&msg, 0, sizeof(msg));
    msg.msg_type = (uint8_t)PC_NETGAME_MSG_CATCH_REQUEST;
    msg.entity_id = entity_id;
    msg.generation = s_client_wildlife_known_generation;
    msg.request_id = s_catch_pending.request_id;
    msg.claimed_species = (int32_t)claimed_species;
    if (!pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))) {
        printf("[NET][WILDLIFE] client: CATCH request %u (entity %u) could not be queued (window full) "
               "-- dropped, not retried\n",
               (unsigned)s_catch_pending.request_id, (unsigned)entity_id);
        /* Residual review fix: a dropped send can never produce a CATCH_RESULT, so leaving
           s_catch_pending.valid set would make pc_net_game_query_catch_outcome() report PENDING
           forever for this entity_id -- the putaway-rod exchange screen's gate would then fall
           through to its PENDING policy only by accident of timing, never resolving. Record it as
           rejected immediately instead, exactly like a genuine host rejection: nothing was sent, so
           nothing can ever be granted for this catch. */
        s_catch_pending.valid = 0;
        s_catch_last_outcome.valid = 1;
        s_catch_last_outcome.entity_id = entity_id;
        s_catch_last_outcome.accepted = 0;
    }
    return 1;
}

/* See pc_net_game.h's own doc. Thin wrapper around pcnetgame_request_catch_common() above. */
int pc_net_game_request_catch_fish(uint32_t entity_id, int claimed_species, int local_grant_item) {
    return pcnetgame_request_catch_common(entity_id, PC_WILDLIFE_KIND_FISH, claimed_species,
                                          local_grant_item);
}

/* World Ecology Wildlife Sync T4. See pc_net_game.h's own doc. Thin wrapper around pcnetgame_request_
 * catch_common() above. */
int pc_net_game_request_catch_bug(uint32_t entity_id, int claimed_species, int local_grant_item) {
    return pcnetgame_request_catch_common(entity_id, PC_WILDLIFE_KIND_BUG, claimed_species,
                                          local_grant_item);
}

/* World Ecology Wildlife Sync T4. See pc_net_game.h's own doc. Thin wrapper around pcwld_bug_entity_id_
 * for_local_actor() (pc_wildlife_authority.h). */
uint32_t pc_net_game_bug_entity_id_for_label(const void* label_actor, int insect_type) {
    return pcwld_bug_entity_id_for_local_actor(label_actor, insect_type);
}

/* See pc_net_game.h's own doc. Mirrors pc_net_game_host_local_tree_shake()'s own shape: the host's own
 * local catch is resolved synchronously against the SAME pcnetgame_validate_and_commit_catch() core a
 * remote peer's CATCH_REQUEST uses (is_host_local=1: skips the reach/IN_TOWN checks, exactly like every
 * other is_host_local exemption in this file). Returns 1 iff accepted (the authoritative table entry is
 * already removed and WILDLIFE_DESPAWN already broadcast by the time this returns) -- the caller must
 * then run the vanilla grant line UNMODIFIED, with NO network round-trip to itself (see this milestone's
 * design brief). Returns 0 if rejected (a genuine race loss to a peer's own simultaneous request, a
 * stale/unknown entity_id, or authoritative wildlife not enabled) -- the caller must NOT grant anything. */
int pc_net_game_host_local_wildlife_catch(uint32_t entity_id, int claimed_species) {
    int accepted;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return 0;
    }
    if (!g_pc_authoritative_wildlife) {
        return 0; /* opt-in gate off -- caller falls through to plain vanilla behavior */
    }

    accepted = pcnetgame_validate_and_commit_catch(1, (PCNetPeerId)0, entity_id, pcwld_session_generation(),
                                                   claimed_species);
    if (accepted) {
        printf("[NET][WILDLIFE] host: local CATCH entity %u (species claim %d) accepted -- removed from "
               "authoritative table\n",
               (unsigned)entity_id, claimed_species);
        pcnetgame_commit_catch_despawn(entity_id);
    } else if (g_pc_verbose) {
        printf("[NET][WILDLIFE] host: local CATCH entity %u (species claim %d) rejected (likely lost a "
               "race to a peer's own simultaneous claim)\n",
               (unsigned)entity_id, claimed_species);
    }

    /* Residual review fix: record this synchronous decision the same way the client records its own
       (eventual) CATCH_RESULT, so pc_net_game_query_catch_outcome() can gate the putaway-rod exchange
       screen for the HOST'S own local full-pockets catch exactly like it gates a client's -- see that
       call site's own doc (m_player_main_putaway_rod.c_inc). Without this, a host that raced and LOST
       to a peer's own simultaneous claim (free_space < 0 either way) would still see the exchange
       dialogue and could still choose "exchange," granting itself an item for a catch it never actually
       won. */
    s_catch_last_outcome.valid = 1;
    s_catch_last_outcome.entity_id = entity_id;
    s_catch_last_outcome.accepted = accepted;

    return accepted;
}

/* Host-side receive: validates peer READY, world ready, and that bx/bz names an addressable town
 * acre BEFORE invoking the spawn adapter -- deliberately no reach/position validation (the client
 * sends no position at all, only "I wade-entered this acre"), matching this milestone's own stated
 * T0 scope (a full reach/position-validation system is later work). */
static void pcnetgame_handle_host_wildlife_spawn_trigger_request(PCNetPeerId peer,
                                                                 const PCNetGameWildlifeSpawnTriggerRequestMsg* in) {
    if (!g_pc_authoritative_wildlife) {
        /* Opt-in gate off -- ignore. Without this check a flag-off host would still run the full
         * adapter (real vanilla spawn decision + real actor creation + broadcast) the moment ANY
         * peer sends this message type, on top of its own already-running vanilla local spawning,
         * with no rate limit -- including from a flagged client, or a malicious/crafted peer.
         * NOTE (flag-mismatch risk): if this host has the flag on but a connected peer does not (or
         * vice versa), there is currently no protocol-level handshake that surfaces that mismatch to
         * either side (unlike e.g. protocol-version, which IS checked at connect time) -- a
         * mismatched peer's wildlife requests/spawns are simply silently ignored by whichever side
         * has the flag off. Wiring a real mismatch notice would need a new handshake field; out of
         * scope for this fix, so it's called out here instead. */
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] host: SPAWN_TRIGGER_REQUEST from peer %d ignored -- "
                   "authoritative wildlife is disabled on this host\n",
                   (int)peer);
        }
        return;
    }
    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {
        return;
    }
    if (!s_host_world_ready || !pcfa_save_ready()) {
        return;
    }
    /* Same addressable-acre bound pcwld_host_spawn_trigger() itself re-checks -- validated here too,
     * up front, so a malformed/out-of-bounds request from a misbehaving client never even reaches
     * the adapter (defense in depth, matching this file's own layered-validation convention). */
    if ((int)in->bx - 1 < 0 || (int)in->bx - 1 >= PCFA_ACRE_X_NUM || (int)in->bz - 1 < 0 ||
        (int)in->bz - 1 >= PCFA_ACRE_Z_NUM) {
        return;
    }
    pcwld_host_spawn_trigger((int)in->bx, (int)in->bz);
}

/* T1 client handler: validates the message (a TRUST BOUNDARY -- this data comes straight off the
 * wire from the host) then materializes a REAL local vanilla fish/bug actor via
 * pcwld_presentation_create(), which does its own full range validation (kind/species/acre/
 * position) and its own duplicate-entity_id suppression -- see that function's own doc
 * (pc_wildlife_authority.h/.c). This client NEVER rolls RNG, NEVER runs the decision functions,
 * NEVER chooses its own species/position, and NEVER invents an entity_id or writes back to any
 * authoritative state -- it only constructs a local presentation actor from data the host already
 * decided. */
static void pcnetgame_handle_client_wildlife_spawn(const PCNetGameWildlifeSpawnMsg* in) {
    if (!g_pc_authoritative_wildlife) {
        /* Opt-in gate off -- ignore. Without this check a flag-off client connected to a flag-on
         * host would still materialize the host's spawns via pcwld_presentation_create() ON TOP OF
         * its own vanilla local spawning. See the matching comment in
         * pcnetgame_handle_host_wildlife_spawn_trigger_request() above re: flag-mismatch risk. */
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] client: SPAWN entity %u ignored -- authoritative wildlife is "
                   "disabled on this client\n",
                   (unsigned)in->entity_id);
        }
        return;
    }
    if (in->entity_id == 0) {
        return; /* 0 is never a valid entity_id (pc_wildlife_authority.h) -- malformed, ignore */
    }
    if ((int)in->kind >= PC_WILDLIFE_KIND_NUM) { /* in->kind is uint8_t -- never negative */
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] client: SPAWN entity %u rejected -- unknown kind %u\n",
                   (unsigned)in->entity_id, (unsigned)in->kind);
        }
        return;
    }

    if (g_pc_verbose) {
        printf("[NET][WILDLIFE] client: SPAWN entity %u kind %u species %d acre(%u,%u) "
               "pos(%.1f,%.1f,%.1f)\n",
               (unsigned)in->entity_id, (unsigned)in->kind, (int)in->species, (unsigned)in->bx,
               (unsigned)in->bz, in->pos_x, in->pos_y, in->pos_z);
    }

    /* Remaining validation (species range, acre range, position sanity) and duplicate-entity_id
     * suppression are pcwld_presentation_create()'s own job -- shared, byte-for-byte, with the
     * host's own self-presentation call (pcwld_host_spawn_trigger(), pc_wildlife_authority.c) so
     * both roles apply exactly the same rules to exactly the same data. */
    pcwld_presentation_create(in->entity_id, (int)in->kind, (int)in->species, (int)in->bx,
                              (int)in->bz, in->pos_x, in->pos_y, in->pos_z);

    /* World Ecology Wildlife Sync T-catch, TEST-ONLY bookkeeping: remembers the most recently observed
       FISH entity_id/species so pcnetgame_run_fish_catch_test_trigger_client() (--force-fish-catch) has
       a real, currently-live entity to catch without needing an actual UKI (fishing rod/bobber) actor --
       see that function's own doc. Cheap and harmless when the flag is off (a plain store, never read),
       so it is not itself gated on g_pc_force_fish_catch. */
    if ((int)in->kind == PC_WILDLIFE_KIND_FISH) {
        s_force_catch_last_fish_entity_id = in->entity_id;
        s_force_catch_last_fish_species = (int)in->species;
        s_force_catch_last_fish_x = in->pos_x;
        s_force_catch_last_fish_z = in->pos_z;
    } else if ((int)in->kind == PC_WILDLIFE_KIND_BUG && (int)in->species != aINS_INSECT_TYPE_ANT) {
        /* World Ecology Wildlife Sync T4, TEST-ONLY bookkeeping for --force-bug-catch -- see
           s_force_catch_last_bug_entity_id's own doc for why ants are excluded here. */
        s_force_catch_last_bug_entity_id = in->entity_id;
        s_force_catch_last_bug_species = (int)in->species;
        s_force_catch_last_bug_x = in->pos_x;
        s_force_catch_last_bug_z = in->pos_z;
    }
}

/* See pc_net_game.h's own doc. Called ONLY from pcwld_table_insert() (pc_wildlife_authority.c). */
void pc_net_game_notify_wildlife_spawn(uint32_t entity_id, int kind, int species, int bx, int bz,
                                        float pos_x, float pos_y, float pos_z) {
    PCNetGameWildlifeSpawnMsg msg;

    if (s_role != PC_NETGAME_ROLE_HOST) {
        return;
    }

    memset(&msg, 0, sizeof(msg));
    msg.msg_type   = (uint8_t)PC_NETGAME_MSG_WILDLIFE_SPAWN;
    msg.kind       = (uint8_t)kind;
    msg.bx         = (uint8_t)bx;
    msg.bz         = (uint8_t)bz;
    msg.entity_id  = entity_id;
    msg.species    = (int32_t)species;
    msg.pos_x      = pos_x;
    msg.pos_y      = pos_y;
    msg.pos_z      = pos_z;
    pcnetgame_broadcast_villager_msg(&msg, sizeof(msg));
}

/* ============================================================================================
 * World Ecology Wildlife Sync T2 (late-join/reconnect snapshot -- see pc_wildlife_authority.h's own
 * T2 doc and this file's own PCNetGameWildlifeSnapshot{Begin,Entry,End}Msg doc comments for the full
 * design). Snapshot-pump integration only -- catching, tournaments, latent-bug networking, ant/bee
 * special-case handling, a final despawn policy, and cockroach extra_data are all still explicitly
 * out of scope (same ABSOLUTE SCOPE LIMIT as T0/T1).
 * ============================================================================================ */

/* Host side: builds a PCNetGameWildlifeSnapshotEntryMsg for authoritative table slot `slot` --
 * returns 0 (nothing built) if that slot is not currently active, 1 otherwise. Mirrors
 * pcnetgame_build_friendship_snapshot_entry()'s own shape exactly (a plain build-or-skip helper the
 * pump's flat-iteration loop calls once per slot). */
static int pcnetgame_build_wildlife_snapshot_entry(PCNetGameWildlifeSnapshotEntryMsg* we, int slot,
                                                    uint32_t epoch) {
    PcWildlifeRecord rec;

    if (!pcwld_get_by_slot(slot, &rec)) {
        return 0;
    }

    memset(we, 0, sizeof(*we));
    we->msg_type  = (uint8_t)PC_NETGAME_MSG_WILDLIFE_SNAPSHOT_ENTRY;
    we->kind      = (uint8_t)rec.kind;
    we->bx        = (uint8_t)rec.bx;
    we->bz        = (uint8_t)rec.bz;
    we->entity_id = rec.entity_id;
    we->species   = (int32_t)rec.species;
    we->pos_x     = rec.pos_x;
    we->pos_y     = rec.pos_y;
    we->pos_z     = rec.pos_z;
    we->epoch     = epoch;
    return 1;
}

/* Client side: WILDLIFE_SNAPSHOT_BEGIN. Gated against the SAME outer snapshot epoch FIELD_BLOCK
 * itself uses (a superseded/restarted snapshot pass is dropped here exactly like it would be for any
 * other in-snapshot message -- see this file's own doc on why no separate staleness scheme is
 * invented for wildlife specifically).
 *
 * The core T2 decision happens here: compare the host's `generation` against
 * s_client_wildlife_known_generation (persists across an ordinary disconnect -- see that variable's
 * own doc). A different value (including this client's very first-ever wildlife snapshot, where the
 * tracker is still its reserved 0) means "treat this as a brand-new authoritative wildlife session" --
 * the local presentation/bookkeeping map is fully cleared FIRST (pcwld_presentation_reset()), so a
 * host restart or a genuinely different host can never have its low-numbered fresh entity_ids
 * misread as continuations of unrelated old local state (see pcwld_session_generation()'s own doc,
 * pc_wildlife_authority.h, for the seeding choice that keeps two different host PROCESSES' own
 * generations from colliding in practice). The SAME value means "this is the session I already have
 * local bookkeeping for" -- nothing is cleared; pcwld_presentation_reconcile() at
 * WILDLIFE_SNAPSHOT_END will instead precisely diff against whatever survived (the whole point of
 * this milestone: entity 100, still alive and still authoritative, must NOT be destroyed and
 * recreated just because the client's transport connection blipped). */
static void pcnetgame_handle_client_wildlife_snapshot_begin(const PCNetGameWildlifeSnapshotBeginMsg* in) {
    if (!g_pc_authoritative_wildlife) {
        /* Opt-in gate off -- ignore. Without this check a flag-off client joining LATE (after
         * wildlife already exists on the host) would still run the known-generation/reset logic
         * below and flip s_client_wildlife_snap_active on, letting the ENTRY/END handlers that
         * follow materialize real local actors via pcwld_presentation_create() -- reintroducing
         * the exact double-spawn problem pcnetgame_handle_client_wildlife_spawn()'s own gate above
         * was added to close, just via the snapshot path instead of the real-time WILDLIFE_SPAWN
         * broadcast path. See the matching comment there. */
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] client: WILDLIFE_SNAPSHOT_BEGIN ignored -- authoritative "
                   "wildlife is disabled on this client\n");
        }
        return;
    }
    if (!s_client_snap_active || in->epoch != s_client_snap_epoch) {
        return; /* part of a superseded outer snapshot */
    }
    if (!pcnetgame_client_can_apply_world()) {
        return;
    }

    if (s_client_wildlife_known_generation == 0 || in->generation != s_client_wildlife_known_generation) {
        printf("[NET][WILDLIFE] client: wildlife snapshot generation %u is %s (previously %u) -- "
               "local presentation bookkeeping cleared before applying this snapshot\n",
               (unsigned)in->generation,
               (s_client_wildlife_known_generation == 0) ? "the FIRST this client has ever seen" : "NEW",
               (unsigned)s_client_wildlife_known_generation);
        pcwld_presentation_reset();
        pcwld_clear_local_actor_stamps(); /* Bug fix (post-T3 review, Bug C): this process's own local
                                              fish-actor pool may still hold a live actor stamped with an
                                              entity_id from the SUPERSEDED session -- clear it here too
                                              so it can never be mismatched against a same-numbered
                                              entity_id in this new session (see that function's own
                                              doc, pc_wildlife_authority.h) */
    } else if (g_pc_verbose) {
        printf("[NET][WILDLIFE] client: wildlife snapshot generation %u matches the previously-known "
               "session -- reconciling against existing local bookkeeping, not clearing it\n",
               (unsigned)in->generation);
    }
    s_client_wildlife_known_generation = in->generation;

    s_client_wildlife_snap_active = 1;
    s_client_wildlife_snap_epoch = in->epoch;
    s_client_wildlife_seen_count = 0;
    s_client_wildlife_snap_incomplete = 0; /* fresh pass -- see this flag's own doc */

    printf("[NET][WILDLIFE] client: wildlife snapshot begin (generation %u, %u entries expected)\n",
           (unsigned)in->generation, (unsigned)in->count);
}

/* Client side: one WILDLIFE_SNAPSHOT_ENTRY. Gated against both the outer snapshot epoch AND this
 * peer's own wildlife sub-pass state (redundant with the outer check in practice, but cheap and
 * matches this file's general layered-validation convention). Framing validation only here
 * (entity_id != 0) -- kind/species/acre/position range validation is entirely pcwld_presentation_
 * create()'s own job (same function, same rules, T1 already established), never duplicated. Every
 * entry -- regardless of whether local actor creation itself succeeds -- is recorded into the
 * "seen this snapshot" list: the ENTITY existing per the host's authoritative table is what matters
 * for reconciliation, independent of whether THIS process could materialize a local actor for it
 * (e.g. an ant, deliberately deferred with no actor -- see pcwld_presentation_create()'s own doc --
 * must still count as "seen", or the very next reconciliation pass would immediately treat it as
 * stale and discard its already-correct "handled, no actor" bookkeeping for no reason). This
 * milestone does NOT filter by acre relevance -- every entry is unconditionally materialized via
 * pcwld_presentation_create(), mirroring T1's own existing unconditional-creation behavior for an
 * ordinary WILDLIFE_SPAWN (see PCNetGameWildlifeSnapshotEntryMsg's own doc). */
static void pcnetgame_handle_client_wildlife_snapshot_entry(const PCNetGameWildlifeSnapshotEntryMsg* in) {
    if (!g_pc_authoritative_wildlife) {
        /* Opt-in gate off -- ignore. This is the handler that actually calls
         * pcwld_presentation_create() and materializes a real local actor -- without this check a
         * flag-off client would do so for every entry in a late-join snapshot, on top of its own
         * vanilla local spawning. See the matching comment in
         * pcnetgame_handle_client_wildlife_snapshot_begin() above and
         * pcnetgame_handle_client_wildlife_spawn() (T1) for the original form of this bug. */
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] client: WILDLIFE_SNAPSHOT_ENTRY entity %u ignored -- "
                   "authoritative wildlife is disabled on this client\n",
                   (unsigned)in->entity_id);
        }
        return;
    }
    if (!s_client_snap_active || in->epoch != s_client_snap_epoch) {
        return;
    }
    if (!s_client_wildlife_snap_active || in->epoch != s_client_wildlife_snap_epoch) {
        return;
    }
    if (!pcnetgame_client_can_apply_world()) {
        /* Save-not-ready (or similar) latch dropped mid-pass -- this entry is skipped, so
         * s_client_wildlife_seen[] will be missing it. Mark the whole pass incomplete so
         * WILDLIFE_SNAPSHOT_END abandons the reconcile instead of treating this (and every other
         * live entity skipped during this window) as stale. See s_client_wildlife_snap_incomplete's
         * own doc. */
        s_client_wildlife_snap_incomplete = 1;
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] client: WILDLIFE_SNAPSHOT_ENTRY entity %u skipped -- world "
                   "cannot currently be applied; this snapshot pass is now marked incomplete\n",
                   (unsigned)in->entity_id);
        }
        return;
    }
    if (in->entity_id == 0) {
        return; /* malformed -- 0 is never a valid entity_id */
    }
    if ((int)in->kind >= PC_WILDLIFE_KIND_NUM) { /* in->kind is uint8_t -- never negative */
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] client: WILDLIFE_SNAPSHOT_ENTRY entity %u rejected -- unknown "
                   "kind %u\n",
                   (unsigned)in->entity_id, (unsigned)in->kind);
        }
        return;
    }

    if (s_client_wildlife_seen_count < PCWLD_PUBLIC_MAX_ENTITIES) {
        s_client_wildlife_seen[s_client_wildlife_seen_count++] = in->entity_id;
    } else if (g_pc_verbose) {
        printf("[NET][WILDLIFE] client: wildlife snapshot 'seen' list full (%d) -- entity %u will not "
               "be protected from this pass's reconciliation if it was already tracked (host table is "
               "itself bounded to the same capacity, so this should not happen in practice)\n",
               PCWLD_PUBLIC_MAX_ENTITIES, (unsigned)in->entity_id);
    }

    /* Same validation + duplicate-suppression pcwld_presentation_create() already gives an ordinary
     * WILDLIFE_SPAWN (T1) -- reused verbatim, never duplicated here. */
    pcwld_presentation_create(in->entity_id, (int)in->kind, (int)in->species, (int)in->bx, (int)in->bz,
                              in->pos_x, in->pos_y, in->pos_z);

    /* World Ecology Wildlife Sync T-catch, TEST-ONLY bookkeeping -- see
     * pcnetgame_handle_client_wildlife_spawn()'s own matching doc. A late-joining client only ever
     * learns about a pre-existing fish through THIS handler (never the real-time WILDLIFE_SPAWN one),
     * so --force-fish-catch needs this same bookkeeping here too. */
    if ((int)in->kind == PC_WILDLIFE_KIND_FISH) {
        s_force_catch_last_fish_entity_id = in->entity_id;
        s_force_catch_last_fish_species = (int)in->species;
        s_force_catch_last_fish_x = in->pos_x;
        s_force_catch_last_fish_z = in->pos_z;
    } else if ((int)in->kind == PC_WILDLIFE_KIND_BUG && (int)in->species != aINS_INSECT_TYPE_ANT) {
        /* World Ecology Wildlife Sync T4, TEST-ONLY bookkeeping -- see
         * pcnetgame_handle_client_wildlife_spawn()'s own matching doc. A late-joining client only ever
         * learns about a pre-existing bug through THIS handler, so --force-bug-catch needs this same
         * bookkeeping here too. */
        s_force_catch_last_bug_entity_id = in->entity_id;
        s_force_catch_last_bug_species = (int)in->species;
        s_force_catch_last_bug_x = in->pos_x;
        s_force_catch_last_bug_z = in->pos_z;
    }
}

/* Client side: WILDLIFE_SNAPSHOT_END -- closes this peer's wildlife sub-pass and runs the actual
 * reconciliation (Part 6 of the T2 milestone brief): anything currently in the local presentation map
 * that was NOT named by this snapshot (not in s_client_wildlife_seen[]) is stale and is removed from
 * LOCAL bookkeeping only (pcwld_presentation_reconcile() -- see that function's own doc for why it
 * deliberately never attempts to destroy the real local actor: a full despawn policy is explicitly
 * out of this milestone's scope). This can only ever matter for a RECONNECT to the SAME session (see
 * pcnetgame_handle_client_wildlife_snapshot_begin()'s own doc) -- a fresh late-join's local map is
 * already empty at this point, so the reconciliation below is a guaranteed no-op removing 0 entries,
 * exactly as the milestone brief itself describes. */
static void pcnetgame_handle_client_wildlife_snapshot_end(const PCNetGameWildlifeSnapshotEndMsg* in) {
    int removed;

    if (!g_pc_authoritative_wildlife) {
        /* Opt-in gate off -- ignore. In practice s_client_wildlife_snap_active can never be 1 here
         * when the flag is off (BEGIN's own gate above refuses to set it), so this is defense in
         * depth / consistency with the BEGIN and ENTRY gates rather than something this path can
         * currently reach -- but it keeps ALL of the known-generation and reconcile logic below
         * from ever running on a flag-off client, not just the actor-creation step, per the same
         * reasoning as the other two gates. */
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] client: WILDLIFE_SNAPSHOT_END ignored -- authoritative wildlife "
                   "is disabled on this client\n");
        }
        return;
    }
    if (!s_client_snap_active || in->epoch != s_client_snap_epoch) {
        return;
    }
    if (!s_client_wildlife_snap_active || in->epoch != s_client_wildlife_snap_epoch) {
        return;
    }
    s_client_wildlife_snap_active = 0;
    if (!pcnetgame_client_can_apply_world()) {
        s_client_wildlife_snap_incomplete = 0; /* whole pass discarded; nothing left to abandon */
        return; /* entries were discarded too; the resync fetches a complete snapshot */
    }
    if (s_client_wildlife_snap_incomplete) {
        /* One or more ENTRYs were skipped mid-pass while the world could not be applied (see
         * s_client_wildlife_snap_incomplete's own doc) -- s_client_wildlife_seen[] is missing
         * whatever was skipped, so running pcwld_presentation_reconcile() against it now would
         * incorrectly treat those still-live entities as stale. Abandon this pass instead; the next
         * snapshot (or a resync) will supply a complete list. */
        s_client_wildlife_snap_incomplete = 0;
        printf("[NET][WILDLIFE] client: wildlife snapshot end (generation %u) -- pass was incomplete "
               "(world could not be applied for part of it), reconciliation skipped\n",
               (unsigned)in->generation);
        return;
    }

    if ((uint32_t)s_client_wildlife_seen_count != in->count_sent && g_pc_verbose) {
        printf("[NET][WILDLIFE] client: WILDLIFE_SNAPSHOT_END count_sent %u != %d entries actually "
               "seen this pass (diagnostic only, not fatal -- see this struct's own doc)\n",
               (unsigned)in->count_sent, s_client_wildlife_seen_count);
    }

    removed = pcwld_presentation_reconcile(s_client_wildlife_seen, s_client_wildlife_seen_count);
    printf("[NET][WILDLIFE] client: wildlife snapshot end (generation %u) -- %d entities live, %d stale "
           "entries reconciled away\n",
           (unsigned)in->generation, s_client_wildlife_seen_count, removed);
}

/* ============================================================================================
 * World Ecology Wildlife Sync T-catch (ordinary fish catching only -- see this milestone's own
 * ABSOLUTE SCOPE LIMIT).
 * ============================================================================================ */

/* Client side: the host's answer to our own pending CATCH_REQUEST. On accept, grants THIS client's own
 * locally-remembered item (s_catch_pending.local_grant, computed once at request time from
 * uki->get_fish_type_proc()) via mPr_SetFreePossessionItem() -- mirroring DIG_SHINE/DIG_HOLE's own
 * client-rolled bonus grant exactly (pcnetgame_handle_client_field_action_result()'s own DIG_SHINE/
 * DIG_HOLE branches) -- and commits the collection-bit write (mSM_COLLECT_FISH_SET()) that
 * Player_actor_setup_main_Notice_rod() (m_player_main_notice_rod.c_inc) deliberately deferred until now.
 * On reject, neither ever happens -- nothing was ever cleared or removed locally for a still-pending
 * catch, so there is nothing to undo. */
static void pcnetgame_handle_client_catch_result(const PCNetGameCatchResultMsg* in) {
    int matches = s_catch_pending.valid && s_catch_pending.request_id == in->request_id;
    uint32_t resolved_entity_id;

    if (!matches) {
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] client: CATCH_RESULT for request %u (entity %u) has no matching "
                   "pending request -- ignored\n",
                   (unsigned)in->request_id, (unsigned)in->entity_id);
        }
        return;
    }
    resolved_entity_id = s_catch_pending.entity_id;
    s_catch_pending.valid = 0; /* the RESULT arrived: resolved either way, never re-applied */

    /* Residual review fix: EVERY early return below now also records a REJECTED outcome for
       resolved_entity_id via s_catch_last_outcome (see its own doc just above PCNetGameCatchOutcome)
       before returning -- not just the plain "!in->accepted" case. From the putaway-rod exchange
       screen's point of view all three are identical: nothing was granted here, so it must not grant
       either, regardless of which specific reason blocked the grant. */
    if (!in->accepted) {
        printf("[NET][WILDLIFE] client: CATCH request %u (entity %u) rejected by host -- no item "
               "granted\n",
               (unsigned)in->request_id, (unsigned)in->entity_id);
        s_catch_last_outcome.valid = 1;
        s_catch_last_outcome.entity_id = resolved_entity_id;
        s_catch_last_outcome.accepted = 0;
        return;
    }
    if (in->entity_id != s_catch_pending.entity_id) {
        printf("[NET][WILDLIFE] client: CATCH request %u accepted but the RESULT's entity %u disagrees "
               "with the request's entity %u -- grant skipped\n",
               (unsigned)in->request_id, (unsigned)in->entity_id, (unsigned)s_catch_pending.entity_id);
        s_catch_last_outcome.valid = 1;
        s_catch_last_outcome.entity_id = resolved_entity_id;
        s_catch_last_outcome.accepted = 0;
        return;
    }
    if (!pcnetgame_owner_stamp_matches(&s_catch_pending.owner)) {
        printf("[NET][WILDLIFE] client: CATCH request %u (entity %u) accepted but the local player/save "
               "changed -- grant skipped\n",
               (unsigned)in->request_id, (unsigned)in->entity_id);
        s_catch_last_outcome.valid = 1;
        s_catch_last_outcome.entity_id = resolved_entity_id;
        s_catch_last_outcome.accepted = 0;
        return;
    }

    s_catch_last_outcome.valid = 1;
    s_catch_last_outcome.entity_id = resolved_entity_id;
    s_catch_last_outcome.accepted = 1;

    if (s_catch_pending.local_grant != 0) {
        if (mPr_SetFreePossessionItem(Now_Private, (mActor_name_t)s_catch_pending.local_grant,
                                      mPr_ITEM_COND_NORMAL)) {
            printf("[NET][WILDLIFE] client: CATCH request %u (entity %u) accepted -- item 0x%04X "
                   "granted to a free pocket slot\n",
                   (unsigned)in->request_id, (unsigned)in->entity_id,
                   (unsigned)s_catch_pending.local_grant);
        } else {
            /* Accepted gap, same shape as DIG_SHINE/DIG_HOLE's own: the free pocket slot vanished
               between send and this RESULT arriving. Documented, not solved with new mechanism. */
            printf("[NET][WILDLIFE] client: CATCH request %u (entity %u) accepted but the free pocket "
                   "slot vanished before the grant could be applied -- item LOST\n",
                   (unsigned)in->request_id, (unsigned)in->entity_id);
        }
    }

    /* The collection-bit commit deferred by Player_actor_setup_main_Notice_rod()/Player_actor_setup_main_
       Notice_net() (see those call sites' own TARGET_PC doc) -- applied now, on ACCEPT only, exactly
       mirroring vanilla's own unconditional commit timing had this been a plain local catch. Branches on
       s_catch_pending.kind (T4) since claimed_species alone cannot distinguish the two domains (both
       start at 0) -- see that field's own doc. */
    if (s_catch_pending.kind == PC_WILDLIFE_KIND_FISH) {
        /* Trash species (>= aGYO_TYPE_NUM) are never tracked in the collection log, matching vanilla's
           own `type < aGYO_TYPE_NUM + 1` gate exactly. */
        if (s_catch_pending.claimed_species < aGYO_TYPE_NUM + 1) {
            mSM_COLLECT_FISH_SET(s_catch_pending.claimed_species);
        }
    } else if (s_catch_pending.kind == PC_WILDLIFE_KIND_BUG) {
        /* aINS_INSECT_TYPE_SPIRIT is never tracked, matching Player_actor_setup_main_Notice_net()'s own
           `idx != aINS_INSECT_TYPE_SPIRIT` gate exactly (m_player_main_notice_net.c_inc). */
        if (s_catch_pending.claimed_species != aINS_INSECT_TYPE_SPIRIT) {
            mSM_COLLECT_INSECT_SET(s_catch_pending.claimed_species);
        }
    }
}

/* See pc_net_game.h's own doc for the full contract and rationale. Consumes (clears back to NONE) a
 * resolved entry the first time it is observed, mirroring s_catch_pending's own "resolved either way,
 * never re-applied" precedent. */
int pc_net_game_query_catch_outcome(uint32_t entity_id) {
    if (entity_id == 0) {
        return PC_NETGAME_CATCH_STATUS_NONE;
    }
    if (s_catch_pending.valid && s_catch_pending.entity_id == entity_id) {
        return PC_NETGAME_CATCH_STATUS_PENDING;
    }
    if (s_catch_last_outcome.valid && s_catch_last_outcome.entity_id == entity_id) {
        int accepted = s_catch_last_outcome.accepted;
        s_catch_last_outcome.valid = 0;
        return accepted ? PC_NETGAME_CATCH_STATUS_ACCEPTED : PC_NETGAME_CATCH_STATUS_REJECTED;
    }
    return PC_NETGAME_CATCH_STATUS_NONE;
}

/* M9-D F2: see pc_net_game.h. Records the same REJECTED outcome a host reject would, so the putaway
 * exchange gate (pc_net_game_query_catch_outcome) denies the exchange for a catch whose grant the
 * seam suppressed locally (disconnected client: no host to ask, entity still live on the host). */
void pc_net_game_record_local_catch_denied(uint32_t entity_id) {
    if (entity_id == 0) {
        return;
    }
    s_catch_last_outcome.valid = 1;
    s_catch_last_outcome.entity_id = entity_id;
    s_catch_last_outcome.accepted = 0;
}

/* Client side: WILDLIFE_DESPAWN -- see PCNetGameWildlifeDespawnMsg's own doc and
 * pcwld_handle_wildlife_despawn()'s own 3-case contract (pc_wildlife_authority.h/.c, which itself wraps
 * aGYO_pc_handle_wildlife_despawn(), ac_gyoei.h/.c). This client never decides accept/reject itself --
 * it only reconciles whatever local fish presentation actor it may have for entity_id, exactly like
 * every other passive wildlife broadcast receiver in this milestone. */
static void pcnetgame_handle_client_wildlife_despawn(const PCNetGameWildlifeDespawnMsg* in) {
    if (!g_pc_authoritative_wildlife) {
        /* Opt-in gate off -- ignore, same defense-in-depth reasoning as every other wildlife handler's
           own gate in this file (a flag-off client never materialized a local presentation actor to
           begin with, so this is a no-op in practice, but keeps the invariant explicit and uniform). */
        if (g_pc_verbose) {
            printf("[NET][WILDLIFE] client: WILDLIFE_DESPAWN entity %u ignored -- authoritative "
                   "wildlife is disabled on this client\n",
                   (unsigned)in->entity_id);
        }
        return;
    }
    if (in->entity_id == 0) {
        return; /* malformed -- 0 is never a valid entity_id */
    }
    if (pcwld_handle_wildlife_despawn(in->entity_id) && g_pc_verbose) {
        printf("[NET][WILDLIFE] client: WILDLIFE_DESPAWN entity %u reconciled against this process's "
               "own local presentation\n",
               (unsigned)in->entity_id);
    }
}
