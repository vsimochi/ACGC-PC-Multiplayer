/* pc_pvp_authority.c - see pc_pvp_authority.h. Pure logic; no engine or network calls. */
#include "pc_pvp_authority.h"

#include <math.h>
#include <string.h>

#define PCPVP_PI 3.14159265358979f

static int pcpvp_finite(float v) {
    return v == v && v > -1.0e30f && v < 1.0e30f;
}

static int pcpvp_pos_ok(float x, float z) {
    return pcpvp_finite(x) && pcpvp_finite(z) && x >= 0.0f && x < 32768.0f && z >= 0.0f && z < 32768.0f;
}

/* a is newer than b in a wrapping 16-bit sequence space */
static int pcpvp_seq_newer(uint16_t a, uint16_t b) {
    return (int16_t)(uint16_t)(a - b) > 0;
}

int pc_pvp_req_msg_valid(const PCPvpHitReqMsg* m) {
    if (m == NULL || m->rsv != 0 || m->tool < PC_PVP_TOOL_AXE || m->tool > PC_PVP_TOOL_NET || m->target == 0xFFu) {
        return 0;
    }
    return pcpvp_pos_ok(m->pos_x, m->pos_z);
}

int pc_pvp_hit_msg_valid(const PCPvpHitMsg* m) {
    if (m == NULL || m->tool < PC_PVP_TOOL_AXE || m->tool > PC_PVP_TOOL_NET || m->attacker == 0xFFu ||
        (m->reaction != PC_PVP_REACT_TUMBLE && m->reaction != PC_PVP_REACT_SHOCK)) {
        return 0;
    }
    return pcpvp_finite(m->knock_speed) && m->knock_speed >= 0.0f && m->knock_speed <= PC_PVP_KNOCK_MAX;
}

/* ---- angles / geometry ---- */
int16_t pc_pvp_angle_to(float from_x, float from_z, float to_x, float to_z) {
    const float dx = to_x - from_x, dz = to_z - from_z;
    float r;
    if (dx == 0.0f && dz == 0.0f) {
        return 0;
    }
    r = atan2f(dx, dz) * (32768.0f / PCPVP_PI);
    if (r >= 32767.0f) {
        r = 32767.0f;
    }
    if (r < -32768.0f) {
        r = -32768.0f;
    }
    return (int16_t)(int)r;
}

int pc_pvp_angle_diff_deg(int16_t a, int16_t b) {
    int d = (int)(int16_t)(uint16_t)((uint16_t)a - (uint16_t)b);
    if (d < 0) {
        d = -d;
    }
    return (int)((float)d * (180.0f / 32768.0f) + 0.5f);
}

int pc_pvp_tool_of_index(int i) {
    switch (i) {
        case PC_PVP_IDX_SWING_AXE:
        case PC_PVP_IDX_AIR_AXE:
        case PC_PVP_IDX_REFLECT_AXE:
            return PC_PVP_TOOL_AXE;
        case PC_PVP_IDX_AIR_SCOOP:
        case PC_PVP_IDX_REFLECT_SCOOP:
        case PC_PVP_IDX_DIG_SCOOP:
        case PC_PVP_IDX_FILL_SCOOP:
            return PC_PVP_TOOL_SHOVEL;
        case PC_PVP_IDX_SWING_NET:
        case PC_PVP_IDX_STOP_NET:
        case PC_PVP_IDX_PULL_NET:
            return PC_PVP_TOOL_NET;
        default:
            return 0;
    }
}

static void pcpvp_dir(int16_t a, float* sx, float* cz) {
    const float r = (float)a * (PCPVP_PI / 32768.0f);
    *sx = sinf(r);
    *cz = cosf(r);
}

/* squared distance from p to the segment a-b (2D) */
static float pcpvp_seg_dist2(float px, float pz, float ax, float az, float bx, float bz) {
    const float vx = bx - ax, vz = bz - az;
    const float l2 = vx * vx + vz * vz;
    float t = 0.0f, qx, qz;
    if (l2 > 1e-6f) {
        t = ((px - ax) * vx + (pz - az) * vz) / l2;
        t = t < 0.0f ? 0.0f : (t > 1.0f ? 1.0f : t);
    }
    qx = ax + t * vx - px;
    qz = az + t * vz - pz;
    return qx * qx + qz * qz;
}

static float pcpvp_cross(float ax, float az, float bx, float bz, float px, float pz) {
    return (bx - ax) * (pz - az) - (bz - az) * (px - ax);
}

static int pcpvp_in_tri(float px, float pz, float ax, float az, float bx, float bz, float cx, float cz) {
    const float d1 = pcpvp_cross(ax, az, bx, bz, px, pz);
    const float d2 = pcpvp_cross(bx, bz, cx, cz, px, pz);
    const float d3 = pcpvp_cross(cx, cz, ax, az, px, pz);
    const int neg = (d1 < 0.0f) || (d2 < 0.0f) || (d3 < 0.0f);
    const int pos = (d1 > 0.0f) || (d2 > 0.0f) || (d3 > 0.0f);
    return !(neg && pos);
}

/* the axe triangle sits at y + 31 (m_player_common.c_inc:2098); the pipe spans [ty, ty + height] */
static int pcpvp_height_ok(float hit_y, float ty) {
    return hit_y >= ty - 10.0f && hit_y <= ty + PC_PVP_PUPPET_HEIGHT + 10.0f;
}

int pc_pvp_axe_hits(float px, float py, float pz, int16_t facing, float tx, float ty, float tz) {
    const int16_t half = (int16_t)(int)(PC_PVP_AXE_HALF_ANGLE_DEG * (32768.0f / 180.0f));
    float rs, rc, ls, lc, rx, rz, lx, lz;
    const float r2 = PC_PVP_PUPPET_RADIUS * PC_PVP_PUPPET_RADIUS;
    if (!pcpvp_height_ok(py + 31.0f, ty)) {
        return 0;
    }
    pcpvp_dir((int16_t)(facing - half), &rs, &rc);
    pcpvp_dir((int16_t)(facing + half), &ls, &lc);
    rx = px + PC_PVP_AXE_REACH * rs;
    rz = pz + PC_PVP_AXE_REACH * rc;
    lx = px + PC_PVP_AXE_REACH * ls;
    lz = pz + PC_PVP_AXE_REACH * lc;
    if (pcpvp_in_tri(tx, tz, px, pz, rx, rz, lx, lz)) {
        return 1;
    }
    return pcpvp_seg_dist2(tx, tz, px, pz, rx, rz) <= r2 || pcpvp_seg_dist2(tx, tz, rx, rz, lx, lz) <= r2 ||
           pcpvp_seg_dist2(tx, tz, lx, lz, px, pz) <= r2;
}

int pc_pvp_shovel_hits(float px, float py, float pz, int16_t facing, float tx, float ty, float tz) {
    float s, c, sx, sz, dx, dz;
    if (ty - py > PC_PVP_MAX_DY || py - ty > PC_PVP_MAX_DY) {
        return 0;
    }
    pcpvp_dir(facing, &s, &c);
    sx = px + PC_PVP_SHOVEL_AHEAD * s;
    sz = pz + PC_PVP_SHOVEL_AHEAD * c;
    dx = tx - sx;
    dz = tz - sz;
    return dx * dx + dz * dz < PC_PVP_SHOVEL_RADIUS * PC_PVP_SHOVEL_RADIUS;
}

int pc_pvp_net_hits(const float a[3], const float b[3], float tx, float ty, float tz) {
    const float r = PC_PVP_PUPPET_RADIUS + PC_PVP_NET_RADIUS;
    const float lo = a[1] < b[1] ? a[1] : b[1];
    const float hi = (a[1] < b[1] ? b[1] : a[1]) + 10.0f; /* net_top = end + 10 (m_player_common.c_inc:2125) */
    if (hi < ty - 5.0f || lo > ty + PC_PVP_PUPPET_HEIGHT + 5.0f) {
        return 0;
    }
    return pcpvp_seg_dist2(tx, tz, a[0], a[2], b[0], b[2]) <= r * r;
}

/* ---- host ---- */
void pc_pvp_host_reset(PCPvpHost* h) {
    memset(h, 0, sizeof(*h));
}

void pc_pvp_host_forget(PCPvpHost* h, int p) {
    if (h == NULL || p < 0 || p >= PC_PVP_MAX_PLAYERS) {
        return;
    }
    h->last_hit_ms[p] = 0;
    h->immune_until[p] = 0;
    h->last_seq[p] = 0;
    h->has_hit[p] = 0;
    h->has_immune[p] = 0;
    memset(h->swing_ms[p], 0, sizeof(h->swing_ms[p]));
    h->has_swing[p] = 0;
    h->town_known[p] = 0;
    h->town_state[p] = 0;
    h->town_since[p] = 0;
}

void pc_pvp_host_note_motion(PCPvpHost* h, int p, int main_index, uint32_t now_ms) {
    const int tool = pc_pvp_tool_of_index(main_index);
    if (h == NULL || p < 0 || p >= PC_PVP_MAX_PLAYERS || tool == 0) {
        return;
    }
    h->swing_ms[p][tool] = now_ms;
    h->has_swing[p] |= (uint8_t)(1u << tool);
}

void pc_pvp_host_note_scene(PCPvpHost* h, int p, int in_town, uint32_t now_ms) {
    if (h == NULL || p < 0 || p >= PC_PVP_MAX_PLAYERS) {
        return;
    }
    in_town = in_town ? 1 : 0;
    if (!h->town_known[p] || h->town_state[p] != (uint8_t)in_town) {
        h->town_known[p] = 1;
        h->town_state[p] = (uint8_t)in_town;
        h->town_since[p] = now_ms; /* first sighting counts as an arrival */
    }
}

static int pcpvp_protected(const PCPvpHost* h, int p, uint32_t now_ms) {
    if (!h->town_known[p] || !h->town_state[p]) {
        return 1;
    }
    return (uint32_t)(now_ms - h->town_since[p]) < PC_PVP_PROTECT_MS;
}

const char* pc_pvp_reason_name(int rc) {
    static const char* const names[] = { "OK", "MALFORMED", "BAD_ATTACKER", "BAD_TARGET", "SCENE", "NO_POS", "NO_SWING", "CLAIM_FAR",
                                         "RANGE", "CONE", "RATE", "IMMUNE", "PROTECTED", "OLD_SEQ" };
    return (rc >= 0 && rc < (int)(sizeof(names) / sizeof(names[0]))) ? names[rc] : "?";
}

int pc_pvp_host_validate(PCPvpHost* h, int attacker, const PCPvpHitReqMsg* m, const PCPvpPlayerView* av, const PCPvpPlayerView* tv,
                         uint32_t now_ms, uint8_t hit_msg_type, PCPvpHitMsg* out) {
    const int target = (m != NULL) ? (int)m->target : -1;
    float dx, dz, dist, cdx, cdz;
    int16_t dir;
    if (h == NULL || m == NULL || av == NULL || tv == NULL || out == NULL || !pc_pvp_req_msg_valid(m)) {
        return PC_PVP_REJ_MALFORMED;
    }
    if (attacker < 0 || attacker >= PC_PVP_MAX_PLAYERS || !av->valid) {
        return PC_PVP_REJ_BAD_ATTACKER;
    }
    if (target == attacker || !tv->valid) {
        return PC_PVP_REJ_BAD_TARGET;
    }
    if (!av->in_town || !tv->in_town) {
        return PC_PVP_REJ_SCENE;
    }
    if (!av->has_pos || !tv->has_pos) {
        return PC_PVP_REJ_NO_POS;
    }
    if (!(h->has_swing[attacker] & (1u << m->tool)) || (uint32_t)(now_ms - h->swing_ms[attacker][m->tool]) > PC_PVP_SWING_WINDOW_MS) {
        return PC_PVP_REJ_NO_SWING;
    }
    cdx = m->pos_x - av->x;
    cdz = m->pos_z - av->z;
    if (cdx * cdx + cdz * cdz > PC_PVP_CLAIM_DIST * PC_PVP_CLAIM_DIST) {
        return PC_PVP_REJ_CLAIM_FAR;
    }
    dx = tv->x - av->x;
    dz = tv->z - av->z;
    dist = sqrtf(dx * dx + dz * dz);
    if (!(dist <= PC_PVP_MAX_DIST) || tv->y - av->y > PC_PVP_MAX_DY || av->y - tv->y > PC_PVP_MAX_DY) {
        return PC_PVP_REJ_RANGE;
    }
    {
        const float ex = tv->x - m->pos_x, ez = tv->z - m->pos_z;
        if (ex * ex + ez * ez >= PC_PVP_CONE_MIN_DIST * PC_PVP_CONE_MIN_DIST &&
            pc_pvp_angle_diff_deg(pc_pvp_angle_to(m->pos_x, m->pos_z, tv->x, tv->z), m->facing) > (int)PC_PVP_CONE_DEG) {
            return PC_PVP_REJ_CONE;
        }
    }
    if (pcpvp_protected(h, attacker, now_ms) || pcpvp_protected(h, target, now_ms)) {
        return PC_PVP_REJ_PROTECTED;
    }
    if (h->has_hit[attacker] && !pcpvp_seq_newer(m->seq, h->last_seq[attacker])) {
        return PC_PVP_REJ_OLD_SEQ;
    }
    if (h->has_hit[attacker] && (uint32_t)(now_ms - h->last_hit_ms[attacker]) < PC_PVP_RATE_MS) {
        return PC_PVP_REJ_RATE;
    }
    if (h->has_immune[target] && (int32_t)(h->immune_until[target] - now_ms) > 0) {
        return PC_PVP_REJ_IMMUNE;
    }
    h->has_hit[attacker] = 1;
    h->last_hit_ms[attacker] = now_ms;
    h->last_seq[attacker] = m->seq;
    h->has_immune[target] = 1;
    h->immune_until[target] = now_ms + PC_PVP_IMMUNE_MS;
    dir = (dist >= 1.0f) ? pc_pvp_angle_to(av->x, av->z, tv->x, tv->z) : m->facing; /* away from the attacker */
    memset(out, 0, sizeof(*out));
    out->msg_type = hit_msg_type;
    out->attacker = (uint8_t)attacker;
    out->tool = m->tool;
    out->reaction = (m->tool == PC_PVP_TOOL_AXE) ? PC_PVP_REACT_TUMBLE : PC_PVP_REACT_SHOCK;
    out->knock_angle = dir;
    out->knock_speed = (m->tool == PC_PVP_TOOL_AXE) ? PC_PVP_KNOCK_AXE : (m->tool == PC_PVP_TOOL_SHOVEL ? PC_PVP_KNOCK_SHOVEL : 0.0f);
    out->seq = ++h->hit_seq;
    return PC_PVP_OK;
}

/* ---- victim ---- */
int pc_pvp_victim_state_ok(int i) {
    switch (i) {
        case PC_PVP_IDX_WAIT:
        case PC_PVP_IDX_WALK:
        case PC_PVP_IDX_RUN:
        case PC_PVP_IDX_DASH:
        case PC_PVP_IDX_TURN_DASH:
        case PC_PVP_IDX_READY_NET:
        case PC_PVP_IDX_READY_WALK_NET:
            return 1;
        default:
            return 0;
    }
}

int pc_pvp_victim_reaction(int reaction, int holding_balloon) {
    if (reaction == PC_PVP_REACT_TUMBLE && holding_balloon) {
        return PC_PVP_REACT_SHOCK; /* TUMBLE_GETUP would release the balloon (m_player_main_tumble_getup.c_inc:110-118): no item loss */
    }
    return reaction;
}

void pc_pvp_victim_reset(PCPvpVictim* v) {
    memset(v, 0, sizeof(*v));
}

int pc_pvp_victim_offer(PCPvpVictim* v, const PCPvpHitMsg* m, uint32_t now_ms) {
    if (v == NULL || !pc_pvp_hit_msg_valid(m)) {
        return 0;
    }
    if (v->has_seq && m->seq == v->last_seq) {
        return 0; /* duplicate */
    }
    v->has_seq = 1;
    v->last_seq = m->seq;
    v->pending = *m;
    v->pending_ms = now_ms;
    v->has_pending = 1;
    return 1;
}

int pc_pvp_victim_take(PCPvpVictim* v, uint32_t now_ms, int main_index, int extra_block, PCPvpHitMsg* out, int* dropped) {
    if (dropped != NULL) {
        *dropped = 0;
    }
    if (v == NULL || !v->has_pending) {
        return 0;
    }
    if ((uint32_t)(now_ms - v->pending_ms) > PC_PVP_VICTIM_TTL_MS ||
        (v->applied_any && (uint32_t)(now_ms - v->last_applied_ms) < PC_PVP_VICTIM_MIN_MS)) {
        v->has_pending = 0;
        if (dropped != NULL) {
            *dropped = 1;
        }
        return 0;
    }
    if (extra_block || !pc_pvp_victim_state_ok(main_index)) {
        return 0; /* stays pending until its TTL: a talk / menu / fishing / door / cutscene is never interrupted */
    }
    *out = v->pending;
    v->has_pending = 0;
    v->applied_any = 1;
    v->last_applied_ms = now_ms;
    return 1;
}

/* ---- attacker ---- */
void pc_pvp_attacker_reset(PCPvpAttacker* a) {
    memset(a, 0, sizeof(*a));
    a->armed_index = -1;
}

void pc_pvp_attacker_on_entry(PCPvpAttacker* a, int main_index) {
    const int tool = pc_pvp_tool_of_index(main_index);
    if (tool == PC_PVP_TOOL_AXE || (tool == PC_PVP_TOOL_SHOVEL && (main_index == PC_PVP_IDX_AIR_SCOOP || main_index == PC_PVP_IDX_REFLECT_SCOOP))) {
        a->armed_index = main_index;
        a->fired = 0;
    } else {
        a->armed_index = -1;
    }
}

int pc_pvp_attacker_contact(PCPvpAttacker* a, int main_index, float frame) {
    float contact;
    if (a->armed_index < 0 || a->fired || main_index != a->armed_index) {
        return 0;
    }
    contact = (pc_pvp_tool_of_index(main_index) == PC_PVP_TOOL_AXE) ? PC_PVP_AXE_CONTACT_FRAME : PC_PVP_SHOVEL_CONTACT_FRAME;
    if (frame < contact) {
        return 0;
    }
    a->fired = 1;
    return 1;
}

int pc_pvp_attacker_next(PCPvpAttacker* a, uint32_t now_ms, uint16_t* seq_out) {
    if (a->sent_any && (uint32_t)(now_ms - a->last_ms) < PC_PVP_ATTACK_MIN_MS) {
        return 0;
    }
    a->sent_any = 1;
    a->last_ms = now_ms;
    *seq_out = ++a->seq;
    return 1;
}
