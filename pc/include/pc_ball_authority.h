/* pc_ball_authority.h - host-authoritative soccer ball / basketball (ac_ball.c) synchronization model.
 *
 * Pure logic, no engine dependencies (compiled into the game and into pc/tools/net_spike/ball_authority_selftest.c):
 *  - the two fixed-size wire structs (PC_NETGAME_MSG_BALL_STATE = 79, PC_NETGAME_MSG_BALL_KICK_REQUEST = 80),
 *  - their value validation (finite, in-town bounds, bounded speed, known flags / type),
 *  - the host's kick-request acceptance (READY + field scene are checked by the caller; this checks the claimed
 *    position against the sender's last MOVE position and the host ball, a per-peer rate limit and seq ordering),
 *  - the client's state acceptance (seq ordering with wrap; a long silence resets ordering for a restarted host),
 *    interpolation toward the host state (snap when the error is large) and the "no state arrived" fallback timer,
 *  - the host send cadence (~10 Hz while moving, 1 Hz while resting).
 * The host owns the only simulated ball. A client never publishes BALL_STATE; the host ignores BALL_STATE it receives.
 * Nothing here runs in single player (pc_net_game.c only calls it with an active multiplayer role). */
#ifndef PC_BALL_AUTHORITY_H
#define PC_BALL_AUTHORITY_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- wire ---- */
enum {
    PC_BALL_FLAG_ACTIVE = 0x01,  /* a host ball actor exists and is simulated (0 => resting snapshot of Common ball_pos) */
    PC_BALL_FLAG_IN_HOLE = 0x02, /* aBALL_STATE_IN_HOLE */
    PC_BALL_FLAG_DEAD = 0x04,    /* aBALL_STATE_DEAD (fell in water) */
    PC_BALL_FLAG_MASK = 0x07,
};

#define PC_BALL_TYPE_COUNT 3      /* ball_model_tbl[] in ac_ball.c */
#define PC_BALL_POS_MIN_XZ 0.0f
#define PC_BALL_POS_MAX_XZ 32768.0f
#define PC_BALL_POS_MIN_Y (-4096.0f)
#define PC_BALL_POS_MAX_Y 8192.0f
#define PC_BALL_STATE_MAX_SPEED 40.0f /* |each component| of the host's position_speed (ball speed clamps at 11, gravity cap 20) */
#define PC_BALL_KICK_MAX_XZ_SPEED 15.0f /* a walking/running player's xz position_speed stays well below this */
#define PC_BALL_KICK_MAX_DIST 40.0f   /* claimed kicker position vs. its last MOVE position and vs. the host ball (xz) */
#define PC_BALL_KICK_MIN_INTERVAL_MS 500u
#define PC_BALL_SEND_MOVING_MS 100u
#define PC_BALL_SEND_RESTING_MS 1000u
#define PC_BALL_SNAP_DIST 60.0f
#define PC_BALL_CLIENT_FALLBACK_MS 4000u /* no BALL_STATE within this => the client uses the vanilla local ball (old host) */
#define PC_BALL_STATE_STALE_MS 3000u      /* this long without state => seq ordering restarts (host restart / reconnect) */

typedef struct PCBallStateMsg {
    uint8_t  msg_type;  /* PC_NETGAME_MSG_BALL_STATE */
    uint8_t  flags;     /* PC_BALL_FLAG_* */
    uint8_t  ball_type; /* 0..2 */
    uint8_t  rsv;       /* 0 */
    uint16_t seq;       /* host send counter (wraps) */
    int16_t  angle_y;   /* world.angle.y (travel direction) */
    float    pos[3];    /* world.position */
    float    spd[3];    /* position_speed */
} PCBallStateMsg;
_Static_assert(sizeof(PCBallStateMsg) == 32, "PCBallStateMsg is a fixed 32-byte wire struct");

typedef struct PCBallKickMsg {
    uint8_t  msg_type; /* PC_NETGAME_MSG_BALL_KICK_REQUEST */
    uint8_t  rsv;      /* 0 */
    uint16_t seq;      /* per-client kick counter (wraps) */
    float    pos[3];   /* the kicker's world position when its pipe touched the local ball view */
    float    spd_x, spd_z; /* the kicker's xz position_speed */
} PCBallKickMsg;
_Static_assert(sizeof(PCBallKickMsg) == 24, "PCBallKickMsg is a fixed 24-byte wire struct");

int pc_ball_state_msg_valid(const PCBallStateMsg* m);
int pc_ball_kick_msg_valid(const PCBallKickMsg* m);

/* ---- host ---- */
#define PC_BALL_MAX_PEERS 256

enum {
    PC_BALL_KICK_OK = 0,
    PC_BALL_KICK_BAD_PEER = 1,
    PC_BALL_KICK_MALFORMED = 2,
    PC_BALL_KICK_NO_BALL = 3,   /* host has no simulated ball (e.g. listen host inside a building) */
    PC_BALL_KICK_NO_SENDER_POS = 4,
    PC_BALL_KICK_FAR_SENDER = 5,
    PC_BALL_KICK_FAR_BALL = 6,
    PC_BALL_KICK_RATE = 7,
    PC_BALL_KICK_OLD_SEQ = 8,
};

typedef struct PCBallHost {
    uint32_t last_kick_ms[PC_BALL_MAX_PEERS];
    uint16_t last_kick_seq[PC_BALL_MAX_PEERS];
    uint8_t  has_kicked[PC_BALL_MAX_PEERS];
    uint32_t last_send_ms;
    uint16_t send_seq;
    uint8_t  sent_any;
    uint8_t  last_sent_moving;
} PCBallHost;

void pc_ball_host_reset(PCBallHost* h);
void pc_ball_host_forget_peer(PCBallHost* h, int peer); /* disconnect */
/* sender_pos may be NULL when the host has no MOVE position for the sender. ball_pos NULL / ball_active 0 => NO_BALL.
 * On PC_BALL_KICK_OK the peer's rate-limit clock and seq are recorded. */
int pc_ball_host_check_kick(PCBallHost* h, int peer, const PCBallKickMsg* m, uint32_t now_ms, const float* sender_pos,
                            const float* ball_pos, int ball_active);
/* Returns 1 when a state should be sent now (and records it); fills the next seq. moving: speed / vertical motion != 0. */
int pc_ball_host_should_send(PCBallHost* h, uint32_t now_ms, int moving, int force, uint16_t* seq_out);
/* Fills a state message from the host ball (clamps nothing: returns 0 when the values are not sendable). */
int pc_ball_host_fill_state(PCBallStateMsg* m, uint8_t msg_type, uint16_t seq, int flags, int ball_type, int16_t angle_y,
                            const float pos[3], const float spd[3]);

/* ---- client ---- */
typedef struct PCBallClient {
    PCBallStateMsg last;   /* latest accepted state */
    uint32_t last_rx_ms;
    uint32_t wait_start_ms;
    uint8_t  have_state;
    uint8_t  waiting;      /* a proxy ball is waiting for its first state */
    uint16_t kick_seq;
    uint32_t last_kick_ms;
    uint8_t  kicked_any;
} PCBallClient;

void pc_ball_client_reset(PCBallClient* c);
void pc_ball_client_begin_wait(PCBallClient* c, uint32_t now_ms);
/* 1 = accepted (newer seq, or first / after a stale gap), 0 = dropped (malformed / old / duplicate). */
int pc_ball_client_accept_state(PCBallClient* c, const PCBallStateMsg* m, uint32_t now_ms);
/* 1 => still waiting and no state ever arrived within PC_BALL_CLIENT_FALLBACK_MS: use the vanilla local ball. */
int pc_ball_client_should_fallback(const PCBallClient* c, uint32_t now_ms);
/* Moves cur toward the latest state's position extrapolated by its speed for (now - last_rx): snaps when the error
 * exceeds PC_BALL_SNAP_DIST, otherwise closes ~frac of the gap. Returns 1 when it snapped, 0 when it interpolated,
 * -1 when there is no state. frac in (0,1]. */
int pc_ball_client_step(const PCBallClient* c, float cur[3], uint32_t now_ms, float frac);
/* Local rate limit before sending a kick request; returns 1 and fills seq when one may be sent now. */
int pc_ball_client_next_kick(PCBallClient* c, uint32_t now_ms, uint16_t* seq_out);

/* ---- game glue: defined in pc_net_game.c (worker-2 ball block), called from src/actor/ac_ball.c under TARGET_PC ---- */
enum { PC_BALL_MODE_VANILLA = 0, PC_BALL_MODE_HOST = 1, PC_BALL_MODE_CLIENT = 2 };
int  pc_net_game_ball_diag(void); /* 1 in PC_NET_TEST_HOOKS builds: ac_ball.c logs ct / dt / mode switches with the live actor count */
int  pc_net_game_ball_mode(void); /* VANILLA in single player / no session / client not READY / not in the town field */
/* host: ac_ball.c's move publishes its simulated ball every frame; dt reports it gone (the periodic tick then sends the
 * resting Common ball_pos snapshot, or DEAD when there is none). */
void pc_net_game_ball_host_publish(const float pos[3], const float spd[3], int16_t angle_y, int ball_type, int flags);
void pc_net_game_ball_host_gone(void);
int  pc_net_game_ball_host_take_kick(float pos[3], float spd_xz[2]); /* 1 once per validated kick request */
/* client: ct starts the wait; move drives the proxy: -1 = run the vanilla ball (no state yet and the fallback expired, or
 * not a client any more), 0 = waiting for the first state (hide), 1 = cur/spd/angle/type/flags were filled from the host. */
void pc_net_game_ball_client_begin(void);
int  pc_net_game_ball_client_drive(float cur[3], float spd[3], int16_t* angle_y, int* ball_type, int* flags);
void pc_net_game_ball_client_kick(const float player_pos[3], float spd_x, float spd_z);
void pc_net_game_ball_peer_gone(int peer); /* host cleanup (pcnetgame_reset_all_host_peer_state) */

#ifdef __cplusplus
}
#endif

#endif
