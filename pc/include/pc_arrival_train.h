/* pc_arrival_train.h - SYNCED ARRIVAL TRAIN (message 105, PRESENTATION ONLY): the pure logic shared by the game and
 * pc/tools/net_spike/arrival_train_selftest.c (header-only, libc-free, no engine dependencies).
 *
 * Before: every process ran its OWN arrival train (m_train_control.c), started from what it saw on the wire (T4: a puppet entering
 * DEMO_STANDING_TRAIN; the shared arrival: a joining guest inferred RIDING / STOPPED from the other players' MOVE states). Nothing kept the trains,
 * Porter or a second passenger in step: two guests joining back-to-back raced, the second got off at once onto the first one's spot at Porter, and a
 * guest joining while the train left started a fresh train nobody else saw.
 *
 * Now the HOST owns a small ARRIVAL TIMELINE: a queue of at most PCAT_MAX_TRAINS trains, each with at most PCAT_MAX_PASS passengers.
 *   - an arriving player's ride-off demo asks the host for a seat (client -> host REQUEST; the listen host's own player calls the queue directly);
 *   - the seat is on the current train while it has not been told to depart (riding in OR stopped at the station) and has room, otherwise on the
 *     NEXT train, which starts PCAT_CLEAR_MS after the current one began to pull out (a brand-new train, the same one for everybody);
 *   - passenger k gets off only when every passenger before it has finished Porter's welcome (k waits in the caboose PCAT_PASS_DX behind each
 *     passenger still on board, and moves right when one gets off: "the next one off");
 *   - the train departs once everybody got off, no earlier than the vanilla 20 s after the stop and PCAT_DEPART_AFTER_OFF_MS after the last
 *     passenger stepped off; every wait is bounded (timeouts below), a vanished passenger never holds the train.
 * The host broadcasts the whole timeline (STATE, 48 bytes) on every change, to a peer at READY and every PCAT_RESEND_MS while a train exists; the
 * times travel as AGES relative to the send moment, so no clock sync is needed (every client is off by its own one-way latency only).
 * Every process (host included) drives its LOCAL vanilla train from that timeline (m_train_control.c: started at the timeline's start, fast-
 * forwarded with the deterministic replica of mTRC_trainControl below, held at the station until the host's depart), positions its local
 * passenger in the caboose from it (ac_train1 passenger dx), gates the get-off on it (ac_ride_off_demo), and moves Porter to the train door
 * while a passenger is still to be welcomed (ac_npc_station_master).
 * Old peers: an old client ignores 105 (no branch); a new client sends 105 only after the host's STATE arrived (an old host never sends one), so an
 * old host or AC_MP_TRAIN_SYNC=0 gives exactly the previous behaviour (T4 + the shared arrival). Solo / offline: never active. */
#ifndef PC_ARRIVAL_TRAIN_H
#define PC_ARRIVAL_TRAIN_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PCAT_MSG_ID 105u

#define PCAT_MAX_TRAINS 2
#define PCAT_MAX_PASS 3                  /* seats per train: dx 0, -40, -80 behind the vanilla passenger spot (caboose + 60) */
#define PCAT_PASS_DX (-40.0f)            /* one seat further back (the shared arrival's PCARR_JOIN_PASSENGER_DX) */
#define PCAT_PASS_DX_SPEED 0.8f          /* units per 60 fps frame when a waiting passenger moves up one seat (~50 frames per seat) */
#define PCAT_VANILLA_WAIT_MS 20000u      /* the vanilla arrival leaves 20 s of RTC time after the stop (mTRC_demo_init -290 s, +310 s at SIGNAL_STOPPED) */
#define PCAT_DEPART_AFTER_OFF_MS 4000u   /* never depart within this of the last passenger stepping off (it is still on the platform edge) */
#define PCAT_WELCOME_TIMEOUT_MS 45000u   /* a passenger stepped off but never reported the end of Porter's welcome: counted as welcomed */
#define PCAT_GETOFF_TIMEOUT_MS 25000u    /* it was a passenger's turn at the stopped train this long and it never stepped off: gone */
#define PCAT_STOP_HARD_CAP_MS 180000u    /* the train departs at the latest this long after the stop, whatever happens */
#define PCAT_CLEAR_MS 15000u             /* the next train starts this long after the previous one began to pull out (its engine is past x 2900, off the station view) */
#define PCAT_RESEND_MS 3000u             /* the host re-broadcasts the STATE this often while a train exists */
#define PCAT_REQUEST_REPLY_MS 6000u      /* client: no seat this long after the request: the arrival falls back to the previous behaviour */
#define PCAT_HOLD_CAP_MS 30000u          /* client: the seat's train has not started this long after the request: fall back */
#define PCAT_START_LATE_MS 60000u        /* a process that learns about a train older than this (not departing yet) still shows it stopped */

enum { PCAT_OP_STATE = 0x80u, PCAT_OP_REQUEST = 1u, PCAT_OP_STEPPED_OFF = 2u, PCAT_OP_WELCOMED = 3u, PCAT_OP_ABORT = 4u };
enum { PCAT_PS_RIDING = 0, PCAT_PS_OFF = 1, PCAT_PS_DONE = 2, PCAT_PS_GONE = 3 };   /* passenger state */
#define PCAT_TF_USED 0x01u
#define PCAT_TF_START 0x02u   /* start_ago_ms is valid (a queued next train has no start until the current one departs) */
#define PCAT_TF_DEPART 0x04u  /* depart_ago_ms is valid: the host told this train to leave */
#define PCAT_TF_MASK 0x07u
#define PCAT_PEER_NONE 0xFFu

/* ---- wire (both directions, 48 bytes, RELIABLE) ---- */
typedef struct PCArrTrainWire {
    uint16_t id;                      /* the host's train id (never 0 for a used train) */
    uint8_t  flags;                   /* PCAT_TF_* */
    uint8_t  n;                       /* passengers 0..PCAT_MAX_PASS */
    int32_t  start_ago_ms;            /* host now - start (negative: starts in the future) */
    int32_t  depart_ago_ms;           /* host now - depart */
    uint8_t  peer[PCAT_MAX_PASS];     /* wire ids (PC_NETGAME_HOST_WIRE_ID = the listen host's own player), unused = PCAT_PEER_NONE */
    uint8_t  pstate[PCAT_MAX_PASS];   /* PCAT_PS_* */
    uint16_t rsv;                     /* 0 */
} PCArrTrainWire;
typedef struct PCArrTrainMsg {
    uint8_t  msg_type;                /* PCAT_MSG_ID */
    uint8_t  op;                      /* PCAT_OP_*: STATE host -> client, the others client -> host */
    uint8_t  version;                 /* 1 */
    uint8_t  n_trains;                /* STATE: used entries of train[] (0..PCAT_MAX_TRAINS); client ops: 0 */
    uint32_t seq;                     /* STATE: the host's counter; client ops: the sender's own counter (logs) */
    PCArrTrainWire train[PCAT_MAX_TRAINS]; /* STATE only; zero in client ops */
} PCArrTrainMsg;
typedef char pcat_wire_size_check[(sizeof(PCArrTrainWire) == 20) ? 1 : -1];
typedef char pcat_msg_size_check[(sizeof(PCArrTrainMsg) == 48) ? 1 : -1];

static inline void pcat_zero(void* p, unsigned n) {
    unsigned char* b = (unsigned char*)p;
    while (n-- > 0u) {
        *b++ = 0u;
    }
}

/* shape check of a received message (both roles). 1 = well-formed. */
static inline int pcat_msg_ok(const PCArrTrainMsg* m) {
    int t, k;
    if (m->msg_type != PCAT_MSG_ID || m->version != 1u) {
        return 0;
    }
    if (m->op != PCAT_OP_STATE) {
        const unsigned char* b = (const unsigned char*)m->train;
        unsigned i;
        if (m->op < PCAT_OP_REQUEST || m->op > PCAT_OP_ABORT || m->n_trains != 0u) {
            return 0;
        }
        for (i = 0; i < sizeof(m->train); i++) {
            if (b[i] != 0u) {
                return 0;
            }
        }
        return 1;
    }
    if (m->n_trains > PCAT_MAX_TRAINS) {
        return 0;
    }
    for (t = 0; t < PCAT_MAX_TRAINS; t++) {
        const PCArrTrainWire* w = &m->train[t];
        if ((w->flags & ~PCAT_TF_MASK) != 0u || w->rsv != 0u) {
            return 0;
        }
        if (t >= (int)m->n_trains) {
            if (w->flags != 0u || w->n != 0u) {
                return 0;
            }
            continue;
        }
        if ((w->flags & PCAT_TF_USED) == 0u || w->id == 0u || w->n > PCAT_MAX_PASS) {
            return 0;
        }
        if ((w->flags & PCAT_TF_DEPART) != 0u && (w->flags & PCAT_TF_START) == 0u) {
            return 0;
        }
        for (k = 0; k < PCAT_MAX_PASS; k++) {
            if (k < (int)w->n ? (w->pstate[k] > PCAT_PS_GONE || w->peer[k] == PCAT_PEER_NONE) : (w->peer[k] != PCAT_PEER_NONE || w->pstate[k] != 0u)) {
                return 0;
            }
        }
    }
    return 1;
}

/* ---- deterministic replica of mTRC_trainControl (m_train_control.c) from mTRC_demo_init, at 60 fps (dt 1, one timer tick per frame) ----
 * The constants come from the caller (the game passes its mTRC_* values, the selftest the VERSION 0 ones). Action numbers = mTRC_ACTION_*. */
typedef struct PCATTrainParams {
    float slow_speed, fast_speed, slow_rate, stop_rate, start_rate, speedup_rate;
} PCATTrainParams;
typedef struct PCATTrainSim {
    unsigned action; /* 2 BEGIN_SLOWDOWN .. 8 SPEED_UP, 0 NONE (gone) */
    float    x, speed;
    unsigned timer;
} PCATTrainSim;
#define PCAT_TRAIN_START_X 2037.0f
#define PCAT_DEPART_GONE_X 4400.0f

static inline void pcat_chase(float* v, float target, float step) {
    if (step != 0.0f) {
        if (*v > target) {
            step = -step;
        }
        *v += step;
        if (step * (*v - target) >= 0.0f) {
            *v = target;
        }
    }
}
static inline void pcat_sim_init(PCATTrainSim* s, const PCATTrainParams* p) {
    s->action = 2u;
    s->x = PCAT_TRAIN_START_X;
    s->speed = p->slow_speed;
    s->timer = 0u;
}
/* one 60 fps frame; WAIT_STOPPED (5) stays until pcat_sim_depart() */
static inline void pcat_sim_step(PCATTrainSim* s, const PCATTrainParams* p) {
    switch (s->action) {
        case 2u:
            pcat_chase(&s->speed, p->slow_speed, p->slow_rate);
            if (s->x > 2165.0f) {
                s->action = 3u;
                s->speed = p->slow_speed;
            }
            break;
        case 3u:
            pcat_chase(&s->speed, 0.0f, p->stop_rate);
            if ((s->speed < 0.0f ? -s->speed : s->speed) < 0.008f) {
                s->timer = 48u;
                s->action = 4u;
                s->speed = 0.0f;
            }
            break;
        case 4u:
            if (s->timer == 0u) {
                s->action = 5u;
            } else {
                s->timer--;
            }
            break;
        case 6u:
            if (s->timer == 0u) {
                s->timer = 180u;
                s->action = 7u;
            } else {
                s->timer--;
            }
            break;
        case 7u:
            pcat_chase(&s->speed, p->slow_speed, p->start_rate);
            if (s->timer == 0u) {
                s->action = 8u;
            } else {
                s->timer--;
            }
            break;
        case 8u:
            pcat_chase(&s->speed, p->fast_speed, p->speedup_rate);
            if (s->x > PCAT_DEPART_GONE_X) {
                s->action = 0u;
            }
            break;
        default:
            break;
    }
    if (s->action != 0u) {
        s->x += 0.5f * s->speed;
    }
}
/* WAIT_STOPPED -> SIGNAL_STARTING (the train signal goes off: timer 84) */
static inline void pcat_sim_depart(PCATTrainSim* s) {
    if (s->action == 5u) {
        s->action = 6u;
        s->timer = 84u;
    }
}
/* frames from mTRC_demo_init to WAIT_STOPPED (578 with the VERSION 0 constants) */
static inline unsigned pcat_sim_arrive_frames(const PCATTrainParams* p) {
    PCATTrainSim s;
    unsigned n = 0u;
    pcat_sim_init(&s, p);
    while (s.action != 5u && n < 100000u) {
        pcat_sim_step(&s, p);
        n++;
    }
    return n;
}
/* frames from the depart signal until the train is gone (action NONE) */
static inline unsigned pcat_sim_depart_frames(const PCATTrainParams* p) {
    PCATTrainSim s;
    unsigned n = 0u;
    pcat_sim_init(&s, p);
    while (s.action != 5u && n < 100000u) {
        pcat_sim_step(&s, p);
    }
    pcat_sim_depart(&s);
    while (s.action != 0u && n < 100000u) {
        pcat_sim_step(&s, p);
        n++;
    }
    return n;
}
/* the state `frames` 60 fps frames after mTRC_demo_init, never past WAIT_STOPPED (a late start shows the train stopped) */
static inline void pcat_sim_at(PCATTrainSim* s, const PCATTrainParams* p, unsigned frames) {
    pcat_sim_init(s, p);
    while (frames-- > 0u && s->action != 5u) {
        pcat_sim_step(s, p);
    }
}
static inline unsigned pcat_ms_to_frames(int32_t ms) {
    return ms <= 0 ? 0u : (unsigned)(((uint32_t)ms * 60u + 500u) / 1000u);
}
static inline uint32_t pcat_frames_to_ms(unsigned frames) {
    return (uint32_t)((frames * 1000u + 59u) / 60u);
}

/* ---- the HOST queue ---- */
typedef struct PCATPass {
    uint8_t  peer;
    uint8_t  state;     /* PCAT_PS_* */
    uint8_t  turn_set;  /* turn_ms valid */
    uint8_t  rsv;
    uint32_t off_ms;    /* when it stepped off */
    uint32_t turn_ms;   /* when it became the passenger at the door of the STOPPED train */
} PCATPass;
typedef struct PCATTrain {
    uint16_t id;
    uint8_t  used, start_set, depart_set, n;
    uint32_t start_ms, depart_ms, last_off_ms;
    PCATPass pass[PCAT_MAX_PASS];
} PCATTrain;
typedef struct PCATQueue {
    PCATTrain train[PCAT_MAX_TRAINS]; /* train[0] is the older one when both are used */
    uint16_t  next_id;
    uint32_t  arrive_ms;               /* mTRC_demo_init -> WAIT_STOPPED (pcat_frames_to_ms(pcat_sim_arrive_frames())) */
    uint32_t  depart_total_ms;         /* depart signal -> gone */
} PCATQueue;

static inline void pcat_queue_init(PCATQueue* q, uint32_t arrive_ms, uint32_t depart_total_ms) {
    pcat_zero(q, sizeof(*q));
    q->next_id = 1u;
    q->arrive_ms = arrive_ms;
    q->depart_total_ms = depart_total_ms;
}
static inline int pcat_ms_reached(uint32_t now, uint32_t t) {
    return (int32_t)(now - t) >= 0;
}
static inline int pcat_train_stopped(const PCATQueue* q, const PCATTrain* t, uint32_t now) {
    return t->used && t->start_set && pcat_ms_reached(now, t->start_ms + q->arrive_ms);
}
/* the passenger index whose turn it is: the first one not welcomed yet (RIDING or OFF), -1 none */
static inline int pcat_door_index(const PCATTrain* t) {
    int k;
    for (k = 0; k < (int)t->n; k++) {
        if (t->pass[k].state == PCAT_PS_RIDING || t->pass[k].state == PCAT_PS_OFF) {
            return k;
        }
    }
    return -1;
}
/* 1 = passenger k may step off now (the train is stopped is checked by the caller): every passenger before it is welcomed / gone */
static inline int pcat_may_get_off_states(const uint8_t* pstate, int n, int k) {
    int i;
    if (k < 0 || k >= n || pstate[k] != PCAT_PS_RIDING) {
        return 0;
    }
    for (i = 0; i < k; i++) {
        if (pstate[i] == PCAT_PS_RIDING || pstate[i] == PCAT_PS_OFF) {
            return 0;
        }
    }
    return 1;
}
/* the caboose offset of passenger k: one seat back for every passenger before it that is still on board */
static inline float pcat_seat_dx_states(const uint8_t* pstate, int n, int k) {
    int i, ahead = 0;
    for (i = 0; i < k && i < n; i++) {
        if (pstate[i] == PCAT_PS_RIDING) {
            ahead++;
        }
    }
    return PCAT_PASS_DX * (float)ahead;
}
/* moves the shown dx toward the seat at PCAT_PASS_DX_SPEED per frame ("moving right"); returns the new value */
static inline float pcat_dx_approach(float cur, float target, float frames) {
    const float step = PCAT_PASS_DX_SPEED * frames;
    if (cur < target) {
        cur += step;
        return cur > target ? target : cur;
    }
    cur -= step;
    return cur < target ? target : cur;
}
static inline int pcat_find_peer(const PCATQueue* q, uint8_t peer, int* out_t, int* out_k) {
    int t, k;
    for (t = 0; t < PCAT_MAX_TRAINS; t++) {
        const PCATTrain* tr = &q->train[t];
        if (!tr->used) {
            continue;
        }
        for (k = 0; k < (int)tr->n; k++) {
            if (tr->pass[k].peer == peer && tr->pass[k].state != PCAT_PS_GONE) {
                *out_t = t;
                *out_k = k;
                return 1;
            }
        }
    }
    return 0;
}
static inline PCATTrain* pcat_newest(PCATQueue* q) {
    if (q->train[1].used) {
        return &q->train[1];
    }
    return q->train[0].used ? &q->train[0] : (PCATTrain*)0;
}

enum { PCAT_REQ_NONE = 0, PCAT_REQ_SEATED = 1, PCAT_REQ_SEATED_NEW = 2, PCAT_REQ_ALREADY = 3, PCAT_REQ_FULL = 4 };

/* A seat for `peer`. SEATED: on the current train (riding in or stopped, not told to depart, room left); SEATED_NEW: on a new train (started now,
 * PCAT_CLEAR_MS after the previous one's depart, or - previous not departed yet but full - once it departs); ALREADY: it has a seat; FULL: both trains taken.
 * *out_t / *out_k: the train / passenger index (not for FULL). */
static inline int pcat_host_request(PCATQueue* q, uint8_t peer, uint32_t now, int* out_t, int* out_k) {
    PCATTrain* cur;
    PCATTrain* nt;
    int t, k;
    if (pcat_find_peer(q, peer, out_t, out_k)) {
        return PCAT_REQ_ALREADY;
    }
    cur = pcat_newest(q);
    if (cur != (PCATTrain*)0 && !cur->depart_set && cur->n < PCAT_MAX_PASS) {
        k = cur->n++;
        cur->pass[k].peer = peer;
        cur->pass[k].state = PCAT_PS_RIDING;
        cur->pass[k].turn_set = 0u;
        *out_t = (int)(cur - q->train);
        *out_k = k;
        return PCAT_REQ_SEATED;
    }
    if (q->train[0].used && q->train[1].used) {
        return PCAT_REQ_FULL;
    }
    t = q->train[0].used ? 1 : 0;
    nt = &q->train[t];
    pcat_zero(nt, sizeof(*nt));
    nt->used = 1u;
    nt->id = q->next_id;
    q->next_id = (uint16_t)(q->next_id == 0xFFFFu ? 1u : q->next_id + 1u);
    if (cur == (PCATTrain*)0) {
        nt->start_set = 1u;
        nt->start_ms = now;
    } else if (cur->depart_set) {
        nt->start_set = 1u;
        nt->start_ms = pcat_ms_reached(now, cur->depart_ms + PCAT_CLEAR_MS) ? now : cur->depart_ms + PCAT_CLEAR_MS;
    } /* else: the current train is full and still at / coming to the station: this one starts PCAT_CLEAR_MS after it departs (pcat_host_tick) */
    nt->n = 1u;
    nt->pass[0].peer = peer;
    nt->pass[0].state = PCAT_PS_RIDING;
    *out_t = t;
    *out_k = 0;
    return PCAT_REQ_SEATED_NEW;
}
/* a passenger's progress. 1 = the state changed. A step back (WELCOMED before STEPPED_OFF is accepted: it implies it) or an unknown peer is ignored. */
static inline int pcat_host_progress(PCATQueue* q, uint8_t peer, unsigned op, uint32_t now) {
    int t, k;
    PCATPass* p;
    if (!pcat_find_peer(q, peer, &t, &k)) {
        return 0;
    }
    p = &q->train[t].pass[k];
    if (op == PCAT_OP_STEPPED_OFF && p->state == PCAT_PS_RIDING) {
        p->state = PCAT_PS_OFF;
        p->off_ms = now;
        q->train[t].last_off_ms = now;
        return 1;
    }
    if (op == PCAT_OP_WELCOMED && (p->state == PCAT_PS_RIDING || p->state == PCAT_PS_OFF)) {
        if (p->state == PCAT_PS_RIDING) {
            q->train[t].last_off_ms = now;
        }
        p->state = PCAT_PS_DONE;
        return 1;
    }
    if (op == PCAT_OP_ABORT) {
        p->state = PCAT_PS_GONE;
        return 1;
    }
    return 0;
}
/* the passenger left the session / the town: it never holds a train again. 1 = changed. */
static inline int pcat_host_peer_gone(PCATQueue* q, uint8_t peer) {
    int t, k, ch = 0;
    for (t = 0; t < PCAT_MAX_TRAINS; t++) {
        PCATTrain* tr = &q->train[t];
        for (k = 0; tr->used && k < (int)tr->n; k++) {
            if (tr->pass[k].peer == peer && (tr->pass[k].state == PCAT_PS_RIDING || tr->pass[k].state == PCAT_PS_OFF)) {
                tr->pass[k].state = PCAT_PS_GONE;
                ch = 1;
            }
        }
    }
    return ch;
}
/* Timeouts, the depart decision, the queued train's start, retirement. 1 = the broadcast state changed. */
static inline int pcat_host_tick(PCATQueue* q, uint32_t now) {
    int t, k, ch = 0;
    for (t = 0; t < PCAT_MAX_TRAINS; t++) {
        PCATTrain* tr = &q->train[t];
        uint32_t stop_ms;
        int door, all_off = 1;
        if (!tr->used) {
            continue;
        }
        if (!tr->start_set) {
            const PCATTrain* prev = &q->train[t == 0 ? 1 : 0];
            if (!prev->used || !prev->start_set) {
                tr->start_set = 1u; /* the train it waited for is gone */
                tr->start_ms = now;
                ch = 1;
            } else if (prev->depart_set) {
                tr->start_set = 1u;
                tr->start_ms = pcat_ms_reached(now, prev->depart_ms + PCAT_CLEAR_MS) ? now : prev->depart_ms + PCAT_CLEAR_MS;
                ch = 1;
            }
            continue;
        }
        stop_ms = tr->start_ms + q->arrive_ms;
        if (pcat_train_stopped(q, tr, now) && !tr->depart_set) {
            door = pcat_door_index(tr);
            if (door >= 0) {
                PCATPass* p = &tr->pass[door];
                if (!p->turn_set) {
                    p->turn_set = 1u;
                    p->turn_ms = now;
                }
                if (p->state == PCAT_PS_RIDING && pcat_ms_reached(now, p->turn_ms + PCAT_GETOFF_TIMEOUT_MS)) {
                    p->state = PCAT_PS_GONE;
                    ch = 1;
                }
            }
        }
        for (k = 0; k < (int)tr->n; k++) {
            PCATPass* p = &tr->pass[k];
            if (p->state == PCAT_PS_OFF && pcat_ms_reached(now, p->off_ms + PCAT_WELCOME_TIMEOUT_MS)) {
                p->state = PCAT_PS_DONE;
                ch = 1;
            }
            if (p->state == PCAT_PS_RIDING) {
                all_off = 0;
            }
        }
        if (!tr->depart_set && pcat_train_stopped(q, tr, now) &&
            ((all_off && pcat_ms_reached(now, stop_ms + PCAT_VANILLA_WAIT_MS) && (tr->last_off_ms == 0u || pcat_ms_reached(now, tr->last_off_ms + PCAT_DEPART_AFTER_OFF_MS))) ||
             pcat_ms_reached(now, stop_ms + PCAT_STOP_HARD_CAP_MS))) {
            tr->depart_set = 1u;
            tr->depart_ms = now;
            for (k = 0; k < (int)tr->n; k++) {
                if (tr->pass[k].state == PCAT_PS_RIDING) {
                    tr->pass[k].state = PCAT_PS_GONE; /* only at the hard cap: whoever is still aboard no longer holds anything */
                }
            }
            ch = 1;
        }
        if (tr->depart_set && pcat_ms_reached(now, tr->depart_ms + q->depart_total_ms) && pcat_door_index(tr) < 0) {
            pcat_zero(tr, sizeof(*tr)); /* gone and every passenger welcomed: retired */
            ch = 1;
        }
    }
    if (!q->train[0].used && q->train[1].used) { /* keep train[0] the older one */
        q->train[0] = q->train[1];
        pcat_zero(&q->train[1], sizeof(q->train[1]));
    }
    return ch;
}
static inline int pcat_any_train(const PCATQueue* q) {
    return q->train[0].used || q->train[1].used;
}
/* the STATE message at host time `now` */
static inline void pcat_build_state(const PCATQueue* q, uint32_t now, uint32_t seq, PCArrTrainMsg* m) {
    int t, k, n = 0;
    pcat_zero(m, sizeof(*m));
    m->msg_type = (uint8_t)PCAT_MSG_ID;
    m->op = (uint8_t)PCAT_OP_STATE;
    m->version = 1u;
    m->seq = seq;
    for (t = 0; t < PCAT_MAX_TRAINS; t++) {
        const PCATTrain* tr = &q->train[t];
        PCArrTrainWire* w;
        if (!tr->used) {
            continue;
        }
        w = &m->train[n++];
        w->id = tr->id;
        w->flags = (uint8_t)(PCAT_TF_USED | (tr->start_set ? PCAT_TF_START : 0u) | (tr->depart_set ? PCAT_TF_DEPART : 0u));
        w->n = tr->n;
        w->start_ago_ms = tr->start_set ? (int32_t)(now - tr->start_ms) : 0;
        w->depart_ago_ms = tr->depart_set ? (int32_t)(now - tr->depart_ms) : 0;
        for (k = 0; k < PCAT_MAX_PASS; k++) {
            w->peer[k] = k < (int)tr->n ? tr->pass[k].peer : (uint8_t)PCAT_PEER_NONE;
            w->pstate[k] = k < (int)tr->n ? tr->pass[k].state : 0u;
        }
    }
    for (; n < PCAT_MAX_TRAINS; n++) {
        for (k = 0; k < PCAT_MAX_PASS; k++) {
            m->train[n].peer[k] = (uint8_t)PCAT_PEER_NONE;
        }
    }
    m->n_trains = (uint8_t)(q->train[0].used + q->train[1].used);
}

/* ---- every process: the train to PRESENT now (pc_arrtrain_view) ----
 * Of the received / own timeline: the NEWEST used train that has started and not finished pulling out, else none. Ages are local ms. */
typedef struct PCATView {
    int      has_train;
    uint16_t id;
    int      started;          /* age_ms >= 0 */
    int32_t  age_ms;           /* local now - start */
    int      depart_set;
    int32_t  depart_age_ms;
    int      n;
    uint8_t  peer[PCAT_MAX_PASS];
    uint8_t  pstate[PCAT_MAX_PASS];
    int      local_idx;        /* the local player's passenger index on THIS train, -1 none */
    int      local_seat_train; /* 1 = the local player has a seat on ANY train of the timeline (also a queued one) */
    int32_t  local_seat_age_ms;/* age of the local player's seat's train (negative = starts later); valid with local_seat_train */
    int      local_seat_started; /* that train has a start time */
} PCATView;

/* Porter (station master) at the train door: a presented train has started and either is not pulling out yet, or still has a passenger to be welcomed. */
static inline int pcat_porter_at_door(const PCATView* v) {
    int k;
    if (!v->has_train || !v->started) {
        return 0;
    }
    for (k = 0; k < v->n; k++) {
        if (v->pstate[k] == PCAT_PS_RIDING || v->pstate[k] == PCAT_PS_OFF) {
            return 1;
        }
    }
    return !v->depart_set;
}

/* The arriving client's decision at the ride-off demo's first step: -1 hold (parked, no control), 0 not synced (the previous behaviour),
 * 1 synced (the seat's train has started: the local train is driven by the timeline).
 *   capable        the host's STATE arrived in this session (and the kill switch is on)
 *   requested_ms   ms since the REQUEST was sent (the caller sends it at the first poll while capable)
 *   have_seat / seat_started / seat_age_ms    the local player's seat in the latest STATE (see PCATView) */
static inline int pcat_client_arrival_decide(int capable, uint32_t requested_ms, int have_seat, int seat_started, int32_t seat_age_ms) {
    if (!capable) {
        return 0;
    }
    if (!have_seat) {
        return requested_ms >= PCAT_REQUEST_REPLY_MS ? 0 : -1;
    }
    if (seat_started && seat_age_ms >= 0) {
        return 1;
    }
    return requested_ms >= PCAT_HOLD_CAP_MS ? 0 : -1;
}

#ifdef __cplusplus
}
#endif

/* ---- the game-facing API (pc/src/pc_net_game_arrtrain.c_inc; TARGET_PC only) ---- */
#ifdef TARGET_PC
#ifdef __cplusplus
extern "C" {
#endif
int   pc_arrtrain_active(void);                 /* 1 = a session whose host runs the synced timeline (host: kill switch on; client: the host's STATE arrived) */
int   pc_arrtrain_view(PCATView* out);          /* fills the train to present; 0 = not active (out->has_train 0) */
int   pc_arrtrain_arrival_query(void);          /* the local ride-off demo's first step: pcat_client_arrival_decide() (sends the REQUEST / seats the host's own player) */
void  pc_arrtrain_local_progress(unsigned op);  /* PCAT_OP_STEPPED_OFF / WELCOMED / ABORT of the local passenger (no-op when not seated) */
int   pc_arrtrain_peer_is_passenger(int peer);  /* 1 = that wire id holds a seat in the timeline (T4 then stays out) */
int   pc_arrtrain_local_seated(void);           /* 1 = the local player holds a seat of the presented train (its arrival is synced) */
int   pc_arrtrain_passenger_pos(int peer, float* x, float* z); /* the drawn town position of a passenger (a remote puppet; the local player: 0) */
#ifdef __cplusplus
}
#endif
#endif

#endif /* PC_ARRIVAL_TRAIN_H */
