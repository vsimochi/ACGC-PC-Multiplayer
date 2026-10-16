/* pc_fishing_view.c - see pc_fishing_view.h. Pure logic (no engine headers): the game build and the native selftest compile this same file. */
#include "pc_fishing_view.h"

#include <math.h>
#include <string.h>

static int pc_fv_finite(float f) {
    return isfinite(f) ? 1 : 0;
}

static int pc_fv_pos_ok(float x, float y, float z) {
    return pc_fv_finite(x) && pc_fv_finite(y) && pc_fv_finite(z) && x >= 0.0f && x < 32768.0f && z >= 0.0f && z < 32768.0f && y > -4096.0f &&
           y < 8192.0f;
}

int pc_fv_sample_ok(const PCFvSample* s, const float* player_pos) {
    float dx, dz, dy;

    if (s == NULL) {
        return 0;
    }
    if (!s->active) {
        return 1;
    }
    if (!pc_fv_pos_ok(s->x, s->y, s->z) || s->uki_status < PC_FV_UKI_STATUS_CAST || s->uki_status > PC_FV_UKI_STATUS_MAX ||
        s->gyo_status < -1 || s->gyo_status > 10 || s->rod_type > 1u) {
        return 0;
    }
    if (player_pos != NULL) {
        if (!pc_fv_finite(player_pos[0]) || !pc_fv_finite(player_pos[1]) || !pc_fv_finite(player_pos[2])) {
            return 0;
        }
        dx = s->x - player_pos[0];
        dz = s->z - player_pos[2];
        dy = s->y - player_pos[1];
        if (dx * dx + dz * dz > PC_FV_MAX_REACH * PC_FV_MAX_REACH || fabsf(dy) > PC_FV_MAX_DY) {
            return 0;
        }
    }
    return 1;
}

static PCFvHostSlot* pc_fv_slot(PCFvHost* h, int origin) {
    if (h == NULL || origin < 0 || origin >= PC_FV_ORIGINS) {
        return NULL;
    }
    return &h->o[origin];
}

int pc_fv_host_note(PCFvHost* h, int origin, const PCFvSample* s, const float* player_pos, int has_seq, int16_t seq, uint32_t now) {
    PCFvHostSlot* o = pc_fv_slot(h, origin);

    if (o == NULL || s == NULL) {
        return 0;
    }
    if (has_seq && seq != 0 && o->in_seq_valid && (int16_t)(seq - o->in_seq) <= 0) {
        o->reordered++;
        return 0; /* older than (or the same as) one already applied: e.g. an unreliable 'active' after the reliable 'gone' */
    }
    if (!pc_fv_sample_ok(s, player_pos)) {
        o->rejected++;
        return 0;
    }
    if (has_seq && seq != 0) {
        o->in_seq = seq;
        o->in_seq_valid = 1;
    }
    o->accepted++;
    if (!s->active) {
        if (o->have && o->sent_any) {
            o->gone_pending = 1;
            o->gone_scene = o->last_sent_scene;
        }
        o->have = 0;
        return 1;
    }
    o->s = *s;
    o->have = 1;
    o->last_in_ms = now;
    return 1;
}

void pc_fv_host_drop(PCFvHost* h, int origin) {
    PCFvHostSlot* o = pc_fv_slot(h, origin);

    if (o == NULL) {
        return;
    }
    if (o->sent_any) {
        o->gone_pending = 1;
        o->gone_scene = o->last_sent_scene;
    }
    o->have = 0;
    o->in_seq_valid = 0; /* a reconnected angler restarts its BOBBER_STATE seq */
}

static void pc_fv_fill(PCFvHostSlot* o, int origin, int active, uint8_t scene, PCBobberViewMsg* out) {
    memset(out, 0, sizeof(*out));
    out->msg_type = (uint8_t)PC_FV_MSG_BOBBER_VIEW;
    out->origin = (uint8_t)origin;
    out->active = active ? 1u : 0u;
    out->scene_id = scene;
    if (active) {
        out->uki_status = o->s.uki_status;
        out->gyo_status = o->s.gyo_status;
        out->rod_type = o->s.rod_type;
        out->x = o->s.x;
        out->y = o->s.y;
        out->z = o->s.z;
        out->angle = o->s.angle;
    }
    o->seq_out = (uint16_t)(o->seq_out + 1u);
    if (o->seq_out == 0u) {
        o->seq_out = 1u;
    }
    out->seq = o->seq_out;
}

int pc_fv_host_poll(PCFvHost* h, int origin, uint32_t now, PCBobberViewMsg* out, int* reliable) {
    PCFvHostSlot* o = pc_fv_slot(h, origin);

    if (o == NULL || out == NULL || reliable == NULL) {
        return 0;
    }
    if (o->have && (int32_t)(now - o->last_in_ms) > (int32_t)PC_FV_HOST_STALE_MS) {
        o->have = 0; /* the angler stopped reporting (disconnect without a gone, a lost reliable, a frozen client) */
        if (o->sent_any) {
            o->gone_pending = 1;
            o->gone_scene = o->last_sent_scene;
        }
    }
    if (o->have && o->sent_any && !o->gone_pending && o->s.scene_id != o->last_sent_scene) {
        o->gone_pending = 1; /* the angler is in another scene now: clear the old scene first */
        o->gone_scene = o->last_sent_scene;
    }
    if (o->gone_pending) {
        pc_fv_fill(o, origin, 0, o->gone_scene, out);
        o->gone_pending = 0;
        o->sent_any = 0;
        o->last_sent_ms = 0u;
        o->sent_gone++;
        *reliable = 1;
        return 1;
    }
    if (!o->have) {
        return 0;
    }
    if (o->last_sent_ms != 0u && (int32_t)(now - o->last_sent_ms) < (int32_t)PC_FV_SEND_INTERVAL_MS) {
        return 0;
    }
    pc_fv_fill(o, origin, 1, o->s.scene_id, out);
    o->sent_any = 1;
    o->last_sent_scene = o->s.scene_id;
    o->last_sent_uki = o->s.uki_status;
    o->last_sent_gyo = o->s.gyo_status;
    o->last_sent_ms = (now == 0u) ? 1u : now;
    o->sent_active++;
    *reliable = 0;
    return 1;
}

int pc_fv_msg_ok(const PCBobberViewMsg* m) {
    if (m == NULL || m->msg_type != (uint8_t)PC_FV_MSG_BOBBER_VIEW || m->rsv != 0u || m->active > 1u || m->seq == 0u) {
        return 0;
    }
    if (!m->active) {
        return 1;
    }
    return pc_fv_pos_ok(m->x, m->y, m->z) && m->uki_status >= PC_FV_UKI_STATUS_CAST && m->uki_status <= PC_FV_UKI_STATUS_MAX &&
           m->gyo_status >= -1 && m->gyo_status <= 10 && m->rod_type <= 1u;
}

int pc_fv_view_apply(PCFvView* v, const PCBobberViewMsg* m, int my_scene_valid, uint8_t my_scene, uint32_t now) {
    if (v == NULL || !pc_fv_msg_ok(m)) {
        if (v != NULL) {
            v->ignored++;
        }
        return 0;
    }
    if (v->seq_valid && (int32_t)(now - v->last_rx_ms) <= (int32_t)PC_FV_SEQ_FORGET_MS && (int16_t)(uint16_t)(m->seq - v->seq) <= 0) {
        v->ignored++;
        return 0; /* stale / duplicate (a late unreliable 'active' after the reliable 'gone') */
    }
    v->seq = m->seq;
    v->seq_valid = 1;
    v->last_rx_ms = now;
    if (!m->active || !my_scene_valid || m->scene_id != my_scene) {
        v->active = 0; /* gone, or the angler is in a scene this process does not show */
        v->applied++;
        return 1;
    }
    if (!v->active) {
        v->since_ms = now;
    }
    v->active = 1;
    v->s.active = 1;
    v->s.uki_status = m->uki_status;
    v->s.gyo_status = m->gyo_status;
    v->s.rod_type = m->rod_type;
    v->s.scene_id = m->scene_id;
    v->s.x = m->x;
    v->s.y = m->y;
    v->s.z = m->z;
    v->s.angle = m->angle;
    v->applied++;
    return 1;
}

int pc_fv_view_get(PCFvView* v, uint32_t now, PCFvSample* out) {
    if (v == NULL || !v->active) {
        return 0;
    }
    if ((int32_t)(now - v->last_rx_ms) > (int32_t)PC_FV_VIEW_STALE_MS) {
        v->active = 0;
        return 0;
    }
    if (out != NULL) {
        *out = v->s;
    }
    return 1;
}
