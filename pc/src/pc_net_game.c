/* pc_net_game.c - Stage 1: in-game networking integration (role selection + identity handshake).
 *
 * See pc_net_game.h for the design summary. This file is the ONLY place that mixes the
 * Stage 0 transport (pc_net.h) with decomp game state -- it reads (never writes) a handful of
 * plain-integer/fixed-byte-array fields out of the currently active save slot (Now_Private) to
 * build an explicit, hand-written wire message. It never touches an ACTOR, never reads pad
 * state, and never calls into rendering/audio.
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
#include "pc_lowaddr.h"    /* PC_LOWADDR_LIMIT -- see pcnetgame_is_real_player_actor() */

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

typedef struct PCNetGameRejectMsg {
    uint8_t  msg_type;              /* PC_NETGAME_MSG_REJECT */
    uint8_t  reason;                /* PCNetGameRejectReason */
    uint16_t _reserved;
    uint32_t expected_protocol_version;
} PCNetGameRejectMsg;
_Static_assert(sizeof(PCNetGameRejectMsg) == 8, "PCNetGameRejectMsg wire size drifted");

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
    printf("[NET] host: peer %d -> READY\n", (int)peer);

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
                    break;
                }
                case PC_NET_EVENT_PEER_DISCONNECTED:
                    s_client_link = PC_NETGAME_LINK_DISCONNECTED;
                    s_client_host_identity_valid = 0;
                    printf("[NET] client: host connection lost\n");
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
