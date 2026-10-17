/* pc_action_ext.c - PUPPET ACTION_EXT pure logic (see pc_action_ext.h). No engine dependencies. */
#include "pc_action_ext.h"

#include <string.h>

int pc_aext_index_allowed(int main_index) {
    switch (main_index) {
        case PC_AEXT_IDX_HOLD:
        case PC_AEXT_IDX_ROTATE_FURNITURE:
        case PC_AEXT_IDX_OPEN_FURNITURE:
        case PC_AEXT_IDX_WAIT_OPEN_FURNITURE:
        case PC_AEXT_IDX_CLOSE_FURNITURE:
        case PC_AEXT_IDX_LIE_BED:
        case PC_AEXT_IDX_ROLL_BED:
        case PC_AEXT_IDX_STANDUP_BED:
        case PC_AEXT_IDX_PICKUP_JUMP:
        case PC_AEXT_IDX_TALK:
        case PC_AEXT_IDX_RECIEVE_STRETCH:
        case PC_AEXT_IDX_RECIEVE:
        case PC_AEXT_IDX_RECIEVE_PUTAWAY:
        case PC_AEXT_IDX_GIVE:
        case PC_AEXT_IDX_GIVE_WAIT:
        case PC_AEXT_IDX_DEMO_WAIT:
        case PC_AEXT_IDX_WASH_CAR:
        case PC_AEXT_IDX_CHANGE_CLOTH:
        case PC_AEXT_IDX_RADIO_EXERCISE:
        case PC_AEXT_IDX_DEMO_GET_GOLDEN_ITEM:
        case PC_AEXT_IDX_DEMO_GET_GOLDEN_ITEM2:
            return 1;
        default:
            return 0;
    }
}

int pc_aext_msg_ok(const PCActionExtMsg* m) {
    if (m == NULL || m->msg_type != (uint8_t)PC_AEXT_MSG_ID || !pc_aext_index_allowed((int)m->main_index) || m->entry_counter > 15u ||
        m->anim0 >= (uint8_t)PC_AEXT_ANIM_NUM || m->anim1 >= (uint8_t)PC_AEXT_ANIM_NUM || m->part_table >= (uint8_t)PC_AEXT_PART_TABLE_NUM ||
        (m->flags & PC_AEXT_FL_RESERVED) != 0u || m->rsv != 0u) {
        return 0;
    }
    if (m->face != PC_AEXT_FACE_NONE && ((m->face >> 4) >= PC_AEXT_EYE_NUM || (m->face & 0x0Fu) >= PC_AEXT_MOUTH_NUM)) {
        return 0;
    }
    return 1;
}

uint8_t pc_aext_face_rule(int main_index, int anim0) {
    if ((main_index == PC_AEXT_IDX_TALK || main_index == PC_AEXT_IDX_DEMO_WAIT) && anim0 == PC_AEXT_ANIM_GAAAN2) {
        return (uint8_t)((6u << 4) | 5u);
    }
    return PC_AEXT_FACE_NONE;
}

uint8_t pc_aext_speed_q(float speed) {
    float a = speed < 0.0f ? -speed : speed;
    float q = a * 32.0f + 0.5f;

    if (!(q >= 0.0f)) { /* NaN */
        return 0;
    }
    return q >= 255.0f ? (uint8_t)255 : (uint8_t)q;
}

float pc_aext_speed_f(uint8_t q) {
    return (float)q / 32.0f; /* 0 = a held clip (wash car before the car drives it) */
}

void pc_aext_sender_reset(PCAextSender* s) {
    memset(s, 0, sizeof(*s));
}

static uint8_t pc_aext_u8_clamp(float f) {
    if (!(f >= 0.0f)) {
        return 0;
    }
    return f >= 255.0f ? (uint8_t)255 : (uint8_t)f;
}

int pc_aext_sender_step(PCAextSender* s, const PCAextSample* smp, double now, PCActionExtMsg* out) {
    PCActionExtMsg m;
    int entry, clip_change, restart, retune;
    float dspeed;

    if (!s->bucket_init) {
        s->bucket_init = 1;
        s->tokens = PC_AEXT_BUCKET_MAX;
        s->bucket_time = now;
    } else if (now > s->bucket_time) {
        s->tokens += (float)(now - s->bucket_time) * PC_AEXT_REFILL;
        if (s->tokens > PC_AEXT_BUCKET_MAX) {
            s->tokens = PC_AEXT_BUCKET_MAX;
        }
        s->bucket_time = now;
    }
    if (!pc_aext_index_allowed(smp->main_index) || smp->counter < 0 || smp->counter > 15 || smp->anim0 < 0 || smp->anim0 >= PC_AEXT_ANIM_NUM ||
        smp->anim1 < 0 || smp->anim1 >= PC_AEXT_ANIM_NUM) {
        s->have = 0; /* left the allowlist (or an unknown clip): the next entry always sends */
        return 0;
    }
    memset(&m, 0, sizeof(m));
    m.msg_type = (uint8_t)PC_AEXT_MSG_ID;
    m.main_index = (uint8_t)smp->main_index;
    m.entry_counter = (uint8_t)smp->counter;
    m.anim0 = (uint8_t)smp->anim0;
    m.anim1 = (uint8_t)smp->anim1;
    m.part_table = (uint8_t)((smp->part_table >= 0 && smp->part_table < PC_AEXT_PART_TABLE_NUM) ? smp->part_table : 0);
    m.flags = (uint8_t)((smp->speed < 0.0f ? PC_AEXT_FL_REVERSE : 0u) | (((unsigned)smp->pin & 7u) << PC_AEXT_FL_PIN_SHIFT) |
                        (smp->repeat ? PC_AEXT_FL_REPEAT : 0u));
    m.speed_q = pc_aext_speed_q(smp->speed);
    m.start_frame = pc_aext_u8_clamp(smp->frame);
    m.face = smp->face_set ? smp->face : pc_aext_face_rule(smp->main_index, smp->anim0);
    m.variant = (uint8_t)(smp->variant & 0xFF);
    m.aux = smp->aux;

    entry = !s->have || s->last.main_index != m.main_index || s->last.entry_counter != m.entry_counter;
    clip_change = !entry && (s->last.anim0 != m.anim0 || s->last.anim1 != m.anim1 || s->last.part_table != m.part_table ||
                             s->last.flags != m.flags || s->last.face != m.face);
    restart = !entry && !clip_change && !smp->repeat && smp->frame + 0.5f < s->last_frame;
    dspeed = smp->speed - s->last_speed;
    if (dspeed < 0.0f) {
        dspeed = -dspeed;
    }
    /* a STOP clip that ended drops its speed to 0 (nothing left to retune: the receiver's copy holds its last frame as well) */
    retune = !entry && !clip_change && !restart && dspeed >= PC_AEXT_RETUNE_DELTA && (now - s->last_send) >= PC_AEXT_RETUNE_FRAMES &&
             !(!smp->repeat && smp->speed == 0.0f);
    s->last_frame = smp->frame;
    if (!(entry || clip_change || restart || retune)) {
        return 0;
    }
    if (s->tokens < 1.0f) {
        s->suppressed++; /* retried next frame (the comparison is against the last SENT message); a restart edge is lost */
        return 0;
    }
    s->tokens -= 1.0f;
    if (entry || clip_change || restart) {
        s->clip_seq++;
    }
    m.clip_seq = s->clip_seq;
    s->last = m;
    s->have = 1;
    s->last_speed = smp->speed;
    s->last_send = now;
    s->sent++;
    *out = m;
    return 1;
}

void pc_aext_store_reset(PCAextStore* st) {
    memset(st, 0, sizeof(*st));
}

void pc_aext_store_put(PCAextStore* st, const PCActionExtMsg* m, double now) {
    PCAextRecord* dst = NULL;
    int i;

    for (i = 0; i < PC_AEXT_RING; i++) {
        if (!st->r[i].valid) {
            dst = &st->r[i];
            break;
        }
    }
    if (dst == NULL) { /* full: replace the oldest */
        dst = &st->r[0];
        for (i = 1; i < PC_AEXT_RING; i++) {
            if (st->r[i].gen < dst->gen) {
                dst = &st->r[i];
            }
        }
    }
    dst->valid = 1;
    dst->seen = 0;
    dst->gen = ++st->next_gen;
    dst->recv_time = now;
    dst->m = *m;
}

int pc_aext_decide(PCAextStore* st, PCAextView* v, int pair_valid, int idx, int ctr, double now, const PCAextRecord** out) {
    PCAextRecord* best = NULL;
    int i;
    int same_clip;

    *out = NULL;
    for (i = 0; i < PC_AEXT_RING; i++) {
        PCAextRecord* r = &st->r[i];

        if (!r->valid) {
            continue;
        }
        if (pair_valid && (int)r->m.main_index == idx && (int)r->m.entry_counter == ctr) {
            r->seen = 1;
            if (best == NULL || r->gen > best->gen) {
                best = r;
            }
        } else if (r->seen || (now - r->recv_time) > PC_AEXT_PENDING_TTL || now < r->recv_time) {
            r->valid = 0; /* its pair was shown and has been left, or it never came (stale / mismatched counter) */
        }
    }
    if (best == NULL) {
        if (v->on) {
            v->on = 0;
            return PC_AEXT_D_END;
        }
        return PC_AEXT_D_NONE;
    }
    *out = best;
    if (v->on && v->applied_gen == best->gen) {
        return PC_AEXT_D_HOLD;
    }
    same_clip = v->on && v->main_index == best->m.main_index && v->entry_counter == best->m.entry_counter && v->anim0 == best->m.anim0 &&
                v->anim1 == best->m.anim1 && v->part_table == best->m.part_table && v->flags == best->m.flags &&
                v->clip_seq == best->m.clip_seq;
    v->on = 1;
    v->applied_gen = best->gen;
    v->anim0 = best->m.anim0;
    v->anim1 = best->m.anim1;
    v->part_table = best->m.part_table;
    v->flags = best->m.flags;
    v->clip_seq = best->m.clip_seq;
    v->main_index = best->m.main_index;
    v->entry_counter = best->m.entry_counter;
    return same_clip ? PC_AEXT_D_RETUNE : PC_AEXT_D_START;
}
