/* pc_action_ext.h - PUPPET ACTION_EXT (PC_NETGAME_MSG_ACTION_EXT = 96): the exact clip a remote player's allowlisted state plays.
 *
 * Why: a puppet mirrors the owner's vanilla main index through MOVE (28 B, fixed: action_state = index bits 0-7 + a 4-bit entry counter),
 * but for many states the CLIP depends on data MOVE cannot carry (furniture class, bed side, the traded item, a talk reaction ordered by
 * the conversation script, the radio command, the car-wash cycle...). Those states used to play the idle fallback. ACTION_EXT carries the
 * clip itself: the owner samples its own PLAYER_ACTOR (animation0_idx / animation1_idx / part_table_idx / keyframe0 frame control /
 * root-pin flags) once per frame while it is in an allowlisted state and sends a 16-byte message when it ENTERS the state and whenever the
 * clip changes inside it (talk reactions GAAAN1 -> GAAAN2, the radio commands, the WASH1..5 cycle, a STOP clip restarted) plus a rate-limited
 * speed retune (radio tempo). PRESENTATION ONLY: nothing here mutates game state.
 *
 * Routing (like BEE_SWARM 81): owner -> host (client -> host carries net_player_id 0; the host stamps the transport slot) -> every OTHER
 * READY peer whose announced scene (scene id AND owner) equals the sender's; a listen host's own player relays directly. RELIABLE.
 * Protocol 8 extended in place (no version bump): an older peer has no branch for id 96 and drops it silently (the dispatchers end with the
 * "malformed / short / unrecognized: ignore" fallthrough; 96 is outside the only violation-counting ranges 47-50 / 59-61).
 *
 * Receiver: the message is a DYNAMIC ROW bound to (main_index, entry_counter): it is applied only while the puppet's interpolated MOVE pair
 * equals it (a stale / mismatched counter never plays), it overrides the static row table (pc_remote_player.c) for that pair and ends with
 * the pair. Pure logic (no engine dependencies): compiled into the game and into pc/tools/net_spike/action_ext_selftest.c. */
#ifndef PC_ACTION_EXT_H
#define PC_ACTION_EXT_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_AEXT_MSG_ID 96u

/* vanilla bounds (pc_net_game.c _Static_asserts each against include/m_player.h) */
#define PC_AEXT_ANIM_NUM 157       /* mPlayer_ANIM_NUM */
#define PC_AEXT_PART_TABLE_NUM 5   /* mPlayer_PART_TABLE_NUM */
#define PC_AEXT_EYE_NUM 8          /* mPlayer_EYE_TEX_NUM */
#define PC_AEXT_MOUTH_NUM 6        /* mPlayer_MOUTH_TEX_NUM */
#define PC_AEXT_INDEX_NUM 121      /* mPlayer_INDEX_NUM */
#define PC_AEXT_ANIM_GAAAN2 111    /* mPlayer_ANIM_GAAAN2 (talk / demo_wait shock hold: constant face 6 / 5) */

/* the allowlisted vanilla mPlayer_INDEX_* values (pc_net_game.c _Static_asserts each) */
enum {
    PC_AEXT_IDX_HOLD = 19, PC_AEXT_IDX_ROTATE_FURNITURE = 22, PC_AEXT_IDX_OPEN_FURNITURE = 23, PC_AEXT_IDX_WAIT_OPEN_FURNITURE = 24,
    PC_AEXT_IDX_CLOSE_FURNITURE = 25, PC_AEXT_IDX_LIE_BED = 26, PC_AEXT_IDX_WAIT_BED = 27, PC_AEXT_IDX_ROLL_BED = 28,
    PC_AEXT_IDX_STANDUP_BED = 29, PC_AEXT_IDX_PICKUP_JUMP = 31, PC_AEXT_IDX_TALK = 65, PC_AEXT_IDX_RECIEVE_STRETCH = 67,
    PC_AEXT_IDX_RECIEVE = 68, PC_AEXT_IDX_RECIEVE_PUTAWAY = 69, PC_AEXT_IDX_GIVE = 70, PC_AEXT_IDX_GIVE_WAIT = 71,
    PC_AEXT_IDX_DEMO_WAIT = 74, PC_AEXT_IDX_WASH_CAR = 84, PC_AEXT_IDX_CHANGE_CLOTH = 101, PC_AEXT_IDX_RADIO_EXERCISE = 111,
    PC_AEXT_IDX_DEMO_GET_GOLDEN_ITEM = 118, PC_AEXT_IDX_DEMO_GET_GOLDEN_ITEM2 = 119,
};
/* (WAIT_BED 27 is NOT allowlisted: it is a fixed clip with its own static row; it is named for the bed tilt rule of the receiver.) */

/* flags */
#define PC_AEXT_FL_REVERSE 0x01u   /* the owner's frame speed is negative (no allowlisted state does this today; receivers play forward) */
#define PC_AEXT_FL_PIN_SHIFT 1     /* bits 1-3: root-pin mask = keyframe0.animation_enabled (cKF TRANS_XZ 1 / TRANS_Y 2 / ROT_Y 4) */
#define PC_AEXT_FL_PIN_MASK 0x0Eu
#define PC_AEXT_FL_REPEAT 0x10u    /* cKF_FRAMECONTROL_REPEAT (else STOP) */
#define PC_AEXT_FL_RESERVED 0xE0u  /* must be 0 */
#define PC_AEXT_FACE_NONE 0xFFu    /* no constant face: the receiver uses the clip's own eye / mouth tables */

typedef struct PCActionExtMsg {
    uint8_t  msg_type;      /* PC_NETGAME_MSG_ACTION_EXT (96) */
    uint8_t  net_player_id; /* client -> host: 0 (the host stamps the transport slot); host -> client: the owner's wire id */
    uint8_t  main_index;    /* an allowlisted mPlayer_INDEX_* */
    uint8_t  entry_counter; /* 0..15: the MOVE action_state counter of that state entry */
    uint8_t  anim0;         /* keyframe0 clip (mPlayer_ANIM_*, < PC_AEXT_ANIM_NUM) */
    uint8_t  anim1;         /* keyframe1 clip (upper body / item pose) */
    uint8_t  part_table;    /* < PC_AEXT_PART_TABLE_NUM */
    uint8_t  flags;         /* PC_AEXT_FL_* */
    uint8_t  speed_q;       /* |keyframe0 frame speed| * 32, saturated at 255 (0.5 = 16) */
    uint8_t  start_frame;   /* the owner's keyframe0 current frame when sampled (floor, 0..255) */
    uint8_t  face;          /* eye << 4 | mouth (eye < 8, mouth < 6), PC_AEXT_FACE_NONE = none */
    uint8_t  variant;       /* state detail (trades: the surface flag; wash car: the WASH cycle index; DEMO_WAIT: the emote id 1..8 of the
                             * emote overlay (pc_emote.h), 0 = a real scripted DEMO_WAIT; else 0) */
    uint16_t aux;           /* trades: the item id (GIVE / RECIEVE_STRETCH / RECIEVE); radio: the command; else 0 */
    uint8_t  clip_seq;      /* per-owner clip instance counter: a new value = (re)start the clip, the same value = retune only */
    uint8_t  rsv;           /* 0 */
} PCActionExtMsg;
_Static_assert(sizeof(PCActionExtMsg) == 16, "PCActionExtMsg is a fixed 16-byte wire struct");
_Static_assert(__builtin_offsetof(PCActionExtMsg, aux) == 12, "PCActionExtMsg.aux at offset 12");

int pc_aext_index_allowed(int main_index);
/* 1 = well formed: id 96, allowlisted index, counter < 16, anims < PC_AEXT_ANIM_NUM, part table < 5, reserved flag bits 0, face NONE or
 * (eye < 8, mouth < 6), rsv 0. net_player_id is NOT checked (it differs per direction). */
int pc_aext_msg_ok(const PCActionExtMsg* m);
/* The constant face a state shows for a clip without eye / mouth tables (talk.c_inc:170-183: GAAAN2 -> eye 6, mouth 5), else NONE. */
uint8_t pc_aext_face_rule(int main_index, int anim0);
uint8_t pc_aext_speed_q(float speed);
float pc_aext_speed_f(uint8_t q); /* q / 32 */

/* ---- sender (the owner): change detector over a once-per-frame sample of the local player ---- */
typedef struct PCAextSample {
    int      main_index, counter;  /* now_main_index, the MOVE entry counter */
    int      anim0, anim1, part_table;
    int      repeat;               /* keyframe0 frame control mode == REPEAT */
    int      pin;                  /* keyframe0.animation_enabled & 7 */
    float    speed, frame;         /* keyframe0 frame control speed / current frame */
    int      variant;
    uint16_t aux;
    int      face_set;             /* 1 = send `face` instead of pc_aext_face_rule (the emote overlay: its constant face, pc_emote.h) */
    uint8_t  face;
} PCAextSample;

typedef struct PCAextSender {
    int            have;       /* `last` is the message of the current (index, counter) */
    PCActionExtMsg last;
    float          last_frame; /* previous sample's frame (STOP restart detection) */
    float          last_speed; /* speed of the last SENT message */
    double         last_send;  /* frame time of the last send */
    float          tokens;     /* hard cap: PC_AEXT_BUCKET_MAX messages, refilled PC_AEXT_REFILL per frame */
    double         bucket_time;
    int            bucket_init;
    uint8_t        clip_seq;
    uint32_t       sent, suppressed;
} PCAextSender;

#define PC_AEXT_RETUNE_DELTA 0.0625f   /* resend for a speed change of at least this ... */
#define PC_AEXT_RETUNE_FRAMES 15.0     /* ... at most every this many frames */
#define PC_AEXT_BUCKET_MAX 12.0f       /* burst */
#define PC_AEXT_REFILL (8.0f / 60.0f)  /* sustained: 8 messages per second per owner */

void pc_aext_sender_reset(PCAextSender* s);
/* 1 = send *out (filled except net_player_id = 0). `now` = the owner's frame time (60 fps frames). Leaving the allowlist forgets the state
 * so the next entry always sends. */
int pc_aext_sender_step(PCAextSender* s, const PCAextSample* smp, double now, PCActionExtMsg* out);

/* ---- receiver: a small per-owner store of DYNAMIC ROWS keyed by (main_index, entry_counter) ---- */
#define PC_AEXT_RING 4
#define PC_AEXT_PENDING_TTL 300.0 /* frames an unseen record waits for the puppet's MOVE pair to reach it */

typedef struct PCAextRecord {
    int            valid, seen;
    uint32_t       gen;
    double         recv_time;
    PCActionExtMsg m;
} PCAextRecord;

typedef struct PCAextStore {
    PCAextRecord r[PC_AEXT_RING];
    uint32_t     next_gen;
} PCAextStore;

/* What the puppet currently plays from this store. */
typedef struct PCAextView {
    int      on;
    uint32_t applied_gen;
    uint8_t  anim0, anim1, part_table, flags, clip_seq, main_index, entry_counter;
} PCAextView;

enum { PC_AEXT_D_NONE = 0, PC_AEXT_D_END, PC_AEXT_D_HOLD, PC_AEXT_D_START, PC_AEXT_D_RETUNE };

void pc_aext_store_reset(PCAextStore* st);
/* Stores a validated message (pc_aext_msg_ok) as the newest record. */
void pc_aext_store_put(PCAextStore* st, const PCActionExtMsg* m, double now);
/* Per puppet frame: the newest record matching the puppet's MOVE pair. Expires records whose pair was shown and left, and unseen records
 * older than PC_AEXT_PENDING_TTL. Returns PC_AEXT_D_*; *out is the record for START / RETUNE / HOLD. Updates *v (on, applied clip). */
int pc_aext_decide(PCAextStore* st, PCAextView* v, int pair_valid, int idx, int ctr, double now, const PCAextRecord** out);

/* Game glue (defined in pc_remote_player.c, PUPPET ACTION_EXT block): store a validated message of owner `player_id` (PCNetPlayerId). */
void pc_remote_player_on_action_ext(int player_id, const PCActionExtMsg* m);

#ifdef __cplusplus
}
#endif

#endif /* PC_ACTION_EXT_H */
