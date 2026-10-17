/* pc_emote.c - EMOTE WHEEL pure logic (see pc_emote.h). No engine dependencies. */
#include "pc_emote.h"

#include <math.h>
#include <string.h>

/* ---- the emote table (v1: 8 slices) ----
 * Sources: YATTA1 / BIKU1 / ITAZURA1 / OMAIRI_US1 at 0.5 (pray.c_inc:28, change_cloth.c_inc:49-55), MENU_CHANGE1 at 1.0 (the try-on speed,
 * change_cloth.c_inc:55), TIRED1 0.5 (tired.c_inc:11, mouth 4 constant: tired.c_inc:13-14; its eyes come from the clip table), the radio
 * exercise command order and speeds (radio_exercise.c_inc:16-36 / :124-145: TAISOU5_x run at half speed). */
static const PCEmoteStep s_hooray[] = { { PC_EMOTE_ANIM_YATTA1, 0.5f } };
static const PCEmoteStep s_surprised[] = { { PC_EMOTE_ANIM_BIKU1, 0.5f } };
static const PCEmoteStep s_pray[] = { { PC_EMOTE_ANIM_OMAIRI_US1, 0.5f } };
static const PCEmoteStep s_phew[] = { { PC_EMOTE_ANIM_TIRED1, 0.5f }, { PC_EMOTE_ANIM_TIRED1, 0.5f } };
static const PCEmoteStep s_boo[] = { { PC_EMOTE_ANIM_ITAZURA1, 0.5f } };
static const PCEmoteStep s_twirl[] = { { PC_EMOTE_ANIM_MENU_CHANGE1, 1.0f } };
static const PCEmoteStep s_exercise[] = {
    { PC_EMOTE_ANIM_TAISOU1 + 0, 0.5f },  /* TAISOU1 */
    { PC_EMOTE_ANIM_TAISOU1 + 1, 0.5f },  /* TAISOU2_1 */
    { PC_EMOTE_ANIM_TAISOU1 + 2, 0.5f },  /* TAISOU2_2 */
    { PC_EMOTE_ANIM_TAISOU1 + 3, 0.5f },  /* TAISOU3 */
    { PC_EMOTE_ANIM_TAISOU1 + 4, 0.5f },  /* TAISOU4_1 */
    { PC_EMOTE_ANIM_TAISOU1 + 5, 0.5f },  /* TAISOU4_2 */
    { PC_EMOTE_ANIM_TAISOU1 + 6, 0.25f }, /* TAISOU5_1 */
    { PC_EMOTE_ANIM_TAISOU1 + 7, 0.25f }, /* TAISOU5_2 */
    { PC_EMOTE_ANIM_TAISOU1 + 8, 0.5f },  /* TAISOU6_1 */
    { PC_EMOTE_ANIM_TAISOU1 + 9, 0.5f },  /* TAISOU6_2 */
    { PC_EMOTE_ANIM_TAISOU1 + 10, 0.5f }, /* TAISOU7_1 */
    { PC_EMOTE_ANIM_TAISOU1 + 11, 0.5f }, /* TAISOU7_2 */
};

#define PC_EMOTE_N(a) ((uint8_t)(sizeof(a) / sizeof((a)[0])))

/* glyphs: include/m_font.h CHAR_SYMBOL_HAPPY 188, LIGTNING 181, SURPRISE 187, STAR 185, DROPLET 59, SKULL 186, MUSIC_NOTE 47, SMILE 191 */
static const PCEmoteDef s_defs[PC_EMOTE_NUM] = {
    [PC_EMOTE_HOORAY] = { "Hooray", 188, PC_EMOTE_KIND_CLIP, 1, PC_EMOTE_FACE_NONE, 0, PC_EMOTE_N(s_hooray), s_hooray, 10000u },
    [PC_EMOTE_SHOCKED] = { "Shocked", 181, PC_EMOTE_KIND_SHOCK, 0, PC_EMOTE_FACE_NONE, 0, 0, NULL, 0u },
    [PC_EMOTE_SURPRISED] = { "Surprised", 187, PC_EMOTE_KIND_CLIP, 0, PC_EMOTE_FACE_NONE, 0, PC_EMOTE_N(s_surprised), s_surprised, 10000u },
    [PC_EMOTE_PRAY] = { "Pray", 185, PC_EMOTE_KIND_CLIP, 0, PC_EMOTE_FACE_NONE, 0, PC_EMOTE_N(s_pray), s_pray, 10000u },
    [PC_EMOTE_PHEW] = { "Phew", 59, PC_EMOTE_KIND_CLIP, 0, (uint8_t)((4u << 4) | 4u), 0, PC_EMOTE_N(s_phew), s_phew, 10000u },
    [PC_EMOTE_BOO] = { "Boo!", 186, PC_EMOTE_KIND_CLIP, 0, PC_EMOTE_FACE_NONE, 0, PC_EMOTE_N(s_boo), s_boo, 10000u },
    [PC_EMOTE_TWIRL] = { "Twirl", 47, PC_EMOTE_KIND_CLIP, 1, PC_EMOTE_FACE_NONE, 0, PC_EMOTE_N(s_twirl), s_twirl, 10000u },
    [PC_EMOTE_EXERCISE] = { "Exercise", 191, PC_EMOTE_KIND_CLIP, 1, PC_EMOTE_FACE_NONE, 1, PC_EMOTE_N(s_exercise), s_exercise, 60000u },
};

const PCEmoteDef* pc_emote_def(int id) {
    if (id <= PC_EMOTE_NONE || id >= PC_EMOTE_NUM) {
        return NULL;
    }
    return &s_defs[id];
}

int pc_emote_pick(float dx, float dy, float dead, int n, int prev) {
    const float tau = 6.28318530718f;
    float len = sqrtf(dx * dx + dy * dy);
    float ang, sector, centre, diff;
    int idx;

    if (n <= 0 || !(len > dead * (prev == 0 ? 1.0f : 0.8f))) { /* also NaN */
        return 0;
    }
    ang = atan2f(dx, -dy); /* 0 = straight up, clockwise positive */
    if (ang < 0.0f) {
        ang += tau;
    }
    sector = tau / (float)n;
    if (prev > 0 && prev <= n) {
        centre = sector * (float)(prev - 1);
        diff = fabsf(ang - centre);
        if (diff > tau * 0.5f) {
            diff = tau - diff;
        }
        if (diff < sector * 0.5f * 1.06f) {
            return prev;
        }
    }
    idx = (int)(ang / sector + 0.5f) % n;
    return 1 + idx;
}

int pc_emote_gate(const PCEmoteGateIn* in, int id) {
    const PCEmoteDef* d = NULL;

    if (in == NULL) {
        return PC_EMOTE_R_NO_PLAYER;
    }
    if (id != PC_EMOTE_NONE && (d = pc_emote_def(id)) == NULL) {
        return PC_EMOTE_R_BAD_ID;
    }
    if (!in->enabled) return PC_EMOTE_R_DISABLED;
    if (in->paused) return PC_EMOTE_R_PAUSED;
    if (in->title_demo) return PC_EMOTE_R_TITLE;
    if (in->transition) return PC_EMOTE_R_TRANSITION;
    if (in->menu_open) return PC_EMOTE_R_MENU;
    if (in->observer) return PC_EMOTE_R_OBSERVER;
    if (in->chat_open) return PC_EMOTE_R_CHAT;
    if (in->tool_wheel) return PC_EMOTE_R_TOOL_WHEEL;
    if (in->msg_or_demo) return PC_EMOTE_R_MSG;
    if (in->no_player) return PC_EMOTE_R_NO_PLAYER;
    if (in->main_index < PC_EMOTE_IDX_WAIT || in->main_index > PC_EMOTE_IDX_DASH) return PC_EMOTE_R_STATE;
    if (in->demo_mode) return PC_EMOTE_R_DEMO;
    if (!in->on_ground) return PC_EMOTE_R_AIR;
    if (in->in_water) return PC_EMOTE_R_WATER;
    if (d == NULL) {
        return PC_EMOTE_OK; /* the wheel may open */
    }
    if (d->full_body && !in->hands_empty) return PC_EMOTE_R_HANDS;
    if (in->since_last_ms < PC_EMOTE_COOLDOWN_MS) return PC_EMOTE_R_COOLDOWN;
    return PC_EMOTE_OK;
}

const char* pc_emote_reason_name(int reason) {
    static const char* const names[PC_EMOTE_R_NUM] = { "ok", "disabled", "paused", "menu", "title", "transition", "observer", "msg", "chat",
                                                       "tool_wheel", "no_player", "state", "demo", "air", "water", "hands", "cooldown", "bad_id" };
    return (reason >= 0 && reason < PC_EMOTE_R_NUM) ? names[reason] : "?";
}

int pc_emote_seq_begin(PCEmoteSeq* s, int id) {
    const PCEmoteDef* d = pc_emote_def(id);

    memset(s, 0, sizeof(*s));
    if (d == NULL || d->kind != PC_EMOTE_KIND_CLIP || d->n_steps == 0 || d->steps == NULL) {
        return 0;
    }
    s->active = 1;
    s->id = id;
    s->step = 0;
    return 1;
}

void pc_emote_seq_stop(PCEmoteSeq* s, int reason) {
    if (s->active) {
        s->active = 0;
        s->end_reason = reason;
    }
}

int pc_emote_seq_tick(PCEmoteSeq* s, int clip_ended, float dt_ms, int input) {
    const PCEmoteDef* d;

    if (!s->active || (d = pc_emote_def(s->id)) == NULL) {
        s->active = 0;
        return PC_EMOTE_T_END;
    }
    if (input) {
        pc_emote_seq_stop(s, PC_EMOTE_END_INPUT);
        return PC_EMOTE_T_END;
    }
    if (dt_ms > 0.0f && dt_ms < 1000.0f) { /* a hitch (load, breakpoint) never eats the cap at once */
        s->elapsed_ms += dt_ms;
    }
    if (s->elapsed_ms >= (float)d->cap_ms) {
        pc_emote_seq_stop(s, PC_EMOTE_END_CAP);
        return PC_EMOTE_T_END;
    }
    if (!clip_ended) {
        return PC_EMOTE_T_CONTINUE;
    }
    if (s->step + 1 < (int)d->n_steps) {
        s->step++;
        return PC_EMOTE_T_NEXT;
    }
    if (d->loop) {
        s->step = 0;
        return PC_EMOTE_T_NEXT;
    }
    pc_emote_seq_stop(s, PC_EMOTE_END_DONE);
    return PC_EMOTE_T_END;
}

const char* pc_emote_end_name(int reason) {
    switch (reason) {
        case PC_EMOTE_END_DONE: return "done";
        case PC_EMOTE_END_CAP: return "cap";
        case PC_EMOTE_END_INPUT: return "input";
        case PC_EMOTE_END_STATE: return "state";
        default: return "none";
    }
}

int pc_emote_wire_index(int real_index, int overlay_active, int in_station_zone) {
    if (overlay_active && real_index == PC_EMOTE_IDX_WAIT && !in_station_zone) {
        return PC_EMOTE_IDX_DEMO_WAIT;
    }
    return real_index;
}

int pc_emote_station_zone(int in_town_field, float x, float z) {
    const float dx = x - 2200.0f, dz = z - 820.0f;
    return in_town_field && dx > -120.0f && dx < 120.0f && dz > -120.0f && dz < 120.0f;
}

static int s_net_active = 0, s_net_id = 0;
static uint8_t s_net_face = PC_EMOTE_FACE_NONE;

void pc_emote_net_set(int active, int id, uint8_t face) {
    s_net_active = active ? 1 : 0;
    s_net_id = active ? id : 0;
    s_net_face = active ? face : (uint8_t)PC_EMOTE_FACE_NONE;
}

int pc_emote_net_active(int* id, uint8_t* face) {
    if (id != NULL) {
        *id = s_net_id;
    }
    if (face != NULL) {
        *face = s_net_face;
    }
    return s_net_active;
}
