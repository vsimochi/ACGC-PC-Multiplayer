/* pc_ball_authority.c - see pc_ball_authority.h. Pure logic; no engine or network calls. */
#include "pc_ball_authority.h"

#include <math.h>
#include <string.h>

static int pcball_finite(float v) {
    return v == v && v > -1.0e30f && v < 1.0e30f;
}

static int pcball_pos_ok(const float p[3]) {
    return pcball_finite(p[0]) && pcball_finite(p[1]) && pcball_finite(p[2]) && p[0] >= PC_BALL_POS_MIN_XZ &&
           p[0] < PC_BALL_POS_MAX_XZ && p[2] >= PC_BALL_POS_MIN_XZ && p[2] < PC_BALL_POS_MAX_XZ && p[1] > PC_BALL_POS_MIN_Y &&
           p[1] < PC_BALL_POS_MAX_Y;
}

static float pcball_dist_xz(const float a[3], const float b[3]) {
    float dx = a[0] - b[0];
    float dz = a[2] - b[2];
    return sqrtf(dx * dx + dz * dz);
}

/* a is newer than b in a wrapping 16-bit sequence space */
static int pcball_seq_newer(uint16_t a, uint16_t b) {
    return (int16_t)(uint16_t)(a - b) > 0;
}

int pc_ball_state_msg_valid(const PCBallStateMsg* m) {
    int i;
    if (m == NULL || (m->flags & ~PC_BALL_FLAG_MASK) != 0 || m->ball_type >= PC_BALL_TYPE_COUNT || m->rsv != 0 || !pcball_pos_ok(m->pos)) {
        return 0;
    }
    for (i = 0; i < 3; i++) {
        if (!pcball_finite(m->spd[i]) || m->spd[i] > PC_BALL_STATE_MAX_SPEED || m->spd[i] < -PC_BALL_STATE_MAX_SPEED) {
            return 0;
        }
    }
    return 1;
}

int pc_ball_kick_msg_valid(const PCBallKickMsg* m) {
    if (m == NULL || m->rsv != 0 || !pcball_pos_ok(m->pos) || !pcball_finite(m->spd_x) || !pcball_finite(m->spd_z)) {
        return 0;
    }
    return sqrtf(m->spd_x * m->spd_x + m->spd_z * m->spd_z) <= PC_BALL_KICK_MAX_XZ_SPEED;
}

/* ---- host ---- */
void pc_ball_host_reset(PCBallHost* h) {
    memset(h, 0, sizeof(*h));
}

void pc_ball_host_forget_peer(PCBallHost* h, int peer) {
    if (h == NULL || peer < 0 || peer >= PC_BALL_MAX_PEERS) {
        return;
    }
    h->has_kicked[peer] = 0;
    h->last_kick_ms[peer] = 0;
    h->last_kick_seq[peer] = 0;
}

int pc_ball_host_check_kick(PCBallHost* h, int peer, const PCBallKickMsg* m, uint32_t now_ms, const float* sender_pos,
                            const float* ball_pos, int ball_active) {
    if (h == NULL || peer < 0 || peer >= PC_BALL_MAX_PEERS) {
        return PC_BALL_KICK_BAD_PEER;
    }
    if (!pc_ball_kick_msg_valid(m)) {
        return PC_BALL_KICK_MALFORMED;
    }
    if (!ball_active || ball_pos == NULL) {
        return PC_BALL_KICK_NO_BALL;
    }
    if (sender_pos == NULL) {
        return PC_BALL_KICK_NO_SENDER_POS;
    }
    if (pcball_dist_xz(m->pos, sender_pos) > PC_BALL_KICK_MAX_DIST) {
        return PC_BALL_KICK_FAR_SENDER;
    }
    if (pcball_dist_xz(m->pos, ball_pos) > PC_BALL_KICK_MAX_DIST) {
        return PC_BALL_KICK_FAR_BALL;
    }
    if (h->has_kicked[peer]) {
        if (!pcball_seq_newer(m->seq, h->last_kick_seq[peer])) {
            return PC_BALL_KICK_OLD_SEQ;
        }
        if ((uint32_t)(now_ms - h->last_kick_ms[peer]) < PC_BALL_KICK_MIN_INTERVAL_MS) {
            return PC_BALL_KICK_RATE;
        }
    }
    h->has_kicked[peer] = 1;
    h->last_kick_ms[peer] = now_ms;
    h->last_kick_seq[peer] = m->seq;
    return PC_BALL_KICK_OK;
}

int pc_ball_host_should_send(PCBallHost* h, uint32_t now_ms, int moving, int force, uint16_t* seq_out) {
    uint32_t interval = moving ? PC_BALL_SEND_MOVING_MS : PC_BALL_SEND_RESTING_MS;
    if (h == NULL) {
        return 0;
    }
    /* a ball that just started moving goes out immediately instead of waiting for the 1 s resting slot */
    if (!force && h->sent_any && (uint32_t)(now_ms - h->last_send_ms) < interval && !(moving && !h->last_sent_moving)) {
        return 0;
    }
    h->sent_any = 1;
    h->last_send_ms = now_ms;
    h->last_sent_moving = moving ? 1 : 0;
    h->send_seq++;
    if (seq_out != NULL) {
        *seq_out = h->send_seq;
    }
    return 1;
}

int pc_ball_host_fill_state(PCBallStateMsg* m, uint8_t msg_type, uint16_t seq, int flags, int ball_type, int16_t angle_y,
                            const float pos[3], const float spd[3]) {
    memset(m, 0, sizeof(*m));
    m->msg_type = msg_type;
    m->flags = (uint8_t)(flags & PC_BALL_FLAG_MASK);
    m->ball_type = (uint8_t)((ball_type >= 0 && ball_type < PC_BALL_TYPE_COUNT) ? ball_type : 0);
    m->seq = seq;
    m->angle_y = angle_y;
    memcpy(m->pos, pos, sizeof(m->pos));
    memcpy(m->spd, spd, sizeof(m->spd));
    return pc_ball_state_msg_valid(m);
}

/* ---- client ---- */
void pc_ball_client_reset(PCBallClient* c) {
    memset(c, 0, sizeof(*c));
}

void pc_ball_client_begin_wait(PCBallClient* c, uint32_t now_ms) {
    c->have_state = 0;
    c->waiting = 1;
    c->wait_start_ms = now_ms;
}

int pc_ball_client_accept_state(PCBallClient* c, const PCBallStateMsg* m, uint32_t now_ms) {
    if (c == NULL || !pc_ball_state_msg_valid(m)) {
        return 0;
    }
    if (c->have_state && (uint32_t)(now_ms - c->last_rx_ms) < PC_BALL_STATE_STALE_MS && !pcball_seq_newer(m->seq, c->last.seq)) {
        return 0; /* reordered / duplicated datagram */
    }
    c->last = *m;
    c->last_rx_ms = now_ms;
    c->have_state = 1;
    return 1;
}

int pc_ball_client_should_fallback(const PCBallClient* c, uint32_t now_ms) {
    return c != NULL && c->waiting && !c->have_state && (uint32_t)(now_ms - c->wait_start_ms) >= PC_BALL_CLIENT_FALLBACK_MS;
}

int pc_ball_client_step(const PCBallClient* c, float cur[3], uint32_t now_ms, float frac) {
    float target[3];
    float age_frames;
    float dx, dy, dz;
    uint32_t age_ms;
    if (c == NULL || !c->have_state) {
        return -1;
    }
    age_ms = (uint32_t)(now_ms - c->last_rx_ms);
    if (age_ms > 200u) {
        age_ms = 200u; /* never extrapolate further than two missed packets */
    }
    age_frames = (float)age_ms * (60.0f / 1000.0f); /* position_speed is per 60 fps frame */
    target[0] = c->last.pos[0] + c->last.spd[0] * age_frames;
    target[1] = c->last.pos[1]; /* gravity/bounce make y extrapolation wrong: hold the host y */
    target[2] = c->last.pos[2] + c->last.spd[2] * age_frames;
    dx = target[0] - cur[0];
    dy = target[1] - cur[1];
    dz = target[2] - cur[2];
    if (!(frac > 0.0f) || frac > 1.0f) {
        frac = 1.0f;
    }
    if (sqrtf(dx * dx + dy * dy + dz * dz) > PC_BALL_SNAP_DIST || !pcball_pos_ok(cur)) {
        memcpy(cur, target, sizeof(target));
        return 1;
    }
    cur[0] += dx * frac;
    cur[1] += dy * frac;
    cur[2] += dz * frac;
    return 0;
}

int pc_ball_client_next_kick(PCBallClient* c, uint32_t now_ms, uint16_t* seq_out) {
    if (c == NULL) {
        return 0;
    }
    if (c->kicked_any && (uint32_t)(now_ms - c->last_kick_ms) < PC_BALL_KICK_MIN_INTERVAL_MS) {
        return 0;
    }
    c->kicked_any = 1;
    c->last_kick_ms = now_ms;
    c->kick_seq++;
    if (seq_out != NULL) {
        *seq_out = c->kick_seq;
    }
    return 1;
}
