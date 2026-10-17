/* emote_selftest.c - native model test of pc_emote.c (EMOTE WHEEL pure logic: the emote table, slice picking with hysteresis, the gate, the
 * overlay sequencer with its wall-clock cap (frame-rate independent), the DEMO_WAIT wire masking and the station zone).
 * Build + run (git-bash, PATH appended with /c/msys64/ucrt64/bin), from pc/:
 *   gcc -std=gnu11 -Wall -Wextra -Iinclude tools/net_spike/emote_selftest.c -o build64_w3/emote_selftest.exe -lm && build64_w3/emote_selftest.exe */
#include "../../src/pc_emote.c"

#include <stdio.h>

static int s_fail = 0, s_n = 0;
#define CHECK(c) do { s_n++; if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); s_fail++; } } while (0)

static PCEmoteGateIn free_play(void) {
    PCEmoteGateIn g;
    memset(&g, 0, sizeof(g));
    g.enabled = 1;
    g.main_index = PC_EMOTE_IDX_WAIT;
    g.on_ground = 1;
    g.hands_empty = 1;
    g.since_last_ms = 0xFFFFFFFFu;
    return g;
}

/* runs an emote with constant dt; the clip "ends" every clip_ms; returns the elapsed wall time at END (or -1) and the step count */
static float run(int id, float dt_ms, float clip_ms, int* steps, int* reason) {
    PCEmoteSeq s;
    float t = 0.0f, in_clip = 0.0f;
    int i, n = 0;

    if (!pc_emote_seq_begin(&s, id)) {
        return -1.0f;
    }
    for (i = 0; i < 10000000; i++) {
        int ended, r;
        t += dt_ms;
        in_clip += dt_ms;
        ended = in_clip >= clip_ms;
        if (ended) {
            in_clip = 0.0f;
        }
        r = pc_emote_seq_tick(&s, ended, dt_ms, 0);
        if (r == PC_EMOTE_T_NEXT) {
            n++;
        } else if (r == PC_EMOTE_T_END) {
            *steps = n;
            *reason = s.end_reason;
            return t;
        }
    }
    return -1.0f;
}

int main(void) {
    int i, steps = 0, reason = 0;
    float t;
    PCEmoteGateIn g;
    PCEmoteSeq s;

    /* ---- T: the table */
    CHECK(PC_EMOTE_SLICES == 8);
    CHECK(pc_emote_def(0) == NULL && pc_emote_def(9) == NULL && pc_emote_def(-1) == NULL);
    for (i = 1; i <= 8; i++) {
        const PCEmoteDef* d = pc_emote_def(i);
        int k;
        CHECK(d != NULL && d->name != NULL && d->name[0] != '\0' && d->glyph != 0);
        if (d == NULL) {
            continue;
        }
        CHECK(d->kind == (i == PC_EMOTE_SHOCKED ? PC_EMOTE_KIND_SHOCK : PC_EMOTE_KIND_CLIP));
        CHECK(d->full_body == (i == PC_EMOTE_HOORAY || i == PC_EMOTE_TWIRL || i == PC_EMOTE_EXERCISE));
        CHECK(d->face == PC_EMOTE_FACE_NONE || ((d->face >> 4) < 8 && (d->face & 0x0F) < 6));
        if (d->kind == PC_EMOTE_KIND_CLIP) {
            CHECK(d->n_steps >= 1 && d->steps != NULL && d->cap_ms >= 5000u && d->cap_ms <= 60000u);
        }
        for (k = 0; k < (int)d->n_steps; k++) {
            CHECK(d->steps[k].anim < PC_EMOTE_ANIM_NUM && d->steps[k].speed > 0.0f && d->steps[k].speed <= 1.0f);
        }
    }
    CHECK(pc_emote_def(PC_EMOTE_HOORAY)->steps[0].anim == 128 && pc_emote_def(PC_EMOTE_SURPRISED)->steps[0].anim == 118 &&
          pc_emote_def(PC_EMOTE_PRAY)->steps[0].anim == 156 && pc_emote_def(PC_EMOTE_PHEW)->steps[0].anim == 97 &&
          pc_emote_def(PC_EMOTE_PHEW)->n_steps == 2 && pc_emote_def(PC_EMOTE_BOO)->steps[0].anim == 125 &&
          pc_emote_def(PC_EMOTE_TWIRL)->steps[0].anim == 73 && pc_emote_def(PC_EMOTE_EXERCISE)->n_steps == 12 &&
          pc_emote_def(PC_EMOTE_EXERCISE)->steps[11].anim == 155 && pc_emote_def(PC_EMOTE_EXERCISE)->loop == 1);
    CHECK(pc_emote_def(PC_EMOTE_PHEW)->face == 0x44);

    /* ---- P: slice picking (screen space, y down; 8 slices clockwise from the top) */
    CHECK(pc_emote_pick(0.0f, -100.0f, 10.0f, 8, 0) == 1);
    CHECK(pc_emote_pick(100.0f, 0.0f, 10.0f, 8, 0) == 3);
    CHECK(pc_emote_pick(0.0f, 100.0f, 10.0f, 8, 0) == 5);
    CHECK(pc_emote_pick(-100.0f, 0.0f, 10.0f, 8, 0) == 7);
    CHECK(pc_emote_pick(70.0f, -70.0f, 10.0f, 8, 0) == 2);
    CHECK(pc_emote_pick(3.0f, 3.0f, 10.0f, 8, 0) == 0);  /* centre */
    CHECK(pc_emote_pick(0.0f / 0.0f, 1.0f, 10.0f, 8, 0) == 0); /* NaN */
    /* hysteresis: 23.5 deg (just past the 22.5 deg edge) keeps slice 1 when it was selected, picks 2 from scratch */
    {
        float a = 23.5f * 3.14159265f / 180.0f;
        float dx = 100.0f * sinf(a), dy = -100.0f * cosf(a);
        CHECK(pc_emote_pick(dx, dy, 10.0f, 8, 1) == 1);
        CHECK(pc_emote_pick(dx, dy, 10.0f, 8, 0) == 2);
        a = 26.0f * 3.14159265f / 180.0f; /* beyond the 6 % margin */
        CHECK(pc_emote_pick(100.0f * sinf(a), -100.0f * cosf(a), 10.0f, 8, 1) == 2);
    }
    /* the centre deadzone shrinks to 80 % for a held selection */
    CHECK(pc_emote_pick(0.0f, -9.0f, 10.0f, 8, 1) == 1 && pc_emote_pick(0.0f, -9.0f, 10.0f, 8, 0) == 0);

    /* ---- G: the gate */
    g = free_play();
    CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_OK);
    for (i = 1; i <= 8; i++) {
        CHECK(pc_emote_gate(&g, i) == PC_EMOTE_OK);
    }
    CHECK(pc_emote_gate(&g, 9) == PC_EMOTE_R_BAD_ID);
    g = free_play(); g.enabled = 0; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_DISABLED);
    g = free_play(); g.paused = 1; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_PAUSED);
    g = free_play(); g.menu_open = 1; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_MENU && pc_emote_gate(&g, 3) == PC_EMOTE_R_MENU);
    g = free_play(); g.title_demo = 1; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_TITLE);
    g = free_play(); g.transition = 1; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_TRANSITION);
    g = free_play(); g.observer = 1; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_OBSERVER);
    g = free_play(); g.chat_open = 1; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_CHAT);
    g = free_play(); g.tool_wheel = 1; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_TOOL_WHEEL);
    g = free_play(); g.msg_or_demo = 1; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_MSG); /* a conversation / message window */
    g = free_play(); g.no_player = 1; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_NO_PLAYER);
    for (i = 0; i < 121; i++) { /* only WAIT 7 .. DASH 10 */
        g = free_play(); g.main_index = i;
        CHECK((pc_emote_gate(&g, 0) == PC_EMOTE_OK) == (i >= 7 && i <= 10));
    }
    g = free_play(); g.main_index = 15; CHECK(pc_emote_gate(&g, 4) == PC_EMOTE_R_STATE); /* WADE */
    g = free_play(); g.main_index = 65; CHECK(pc_emote_gate(&g, 4) == PC_EMOTE_R_STATE); /* TALK */
    g = free_play(); g.main_index = 74; CHECK(pc_emote_gate(&g, 4) == PC_EMOTE_R_STATE); /* DEMO_WAIT */
    g = free_play(); g.demo_mode = 1; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_DEMO);
    g = free_play(); g.on_ground = 0; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_AIR);
    g = free_play(); g.in_water = 1; CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_R_WATER);
    /* hands: the wheel opens, the full-body emotes are refused, the others play with the tool */
    g = free_play(); g.hands_empty = 0;
    CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_OK);
    for (i = 1; i <= 8; i++) {
        int fb = (i == PC_EMOTE_HOORAY || i == PC_EMOTE_TWIRL || i == PC_EMOTE_EXERCISE);
        CHECK(pc_emote_gate(&g, i) == (fb ? PC_EMOTE_R_HANDS : PC_EMOTE_OK));
    }
    /* cooldown 1 s, only for a start (the wheel may open) */
    g = free_play(); g.since_last_ms = 500u;
    CHECK(pc_emote_gate(&g, 0) == PC_EMOTE_OK && pc_emote_gate(&g, 2) == PC_EMOTE_R_COOLDOWN);
    g.since_last_ms = PC_EMOTE_COOLDOWN_MS; CHECK(pc_emote_gate(&g, 2) == PC_EMOTE_OK);
    CHECK(pc_emote_gate(NULL, 1) == PC_EMOTE_R_NO_PLAYER);
    CHECK(strcmp(pc_emote_reason_name(PC_EMOTE_R_HANDS), "hands") == 0 && strcmp(pc_emote_reason_name(PC_EMOTE_R_COOLDOWN), "cooldown") == 0 &&
          strcmp(pc_emote_reason_name(999), "?") == 0);

    /* ---- S: the sequencer */
    CHECK(pc_emote_seq_begin(&s, PC_EMOTE_SHOCKED) == 0 && !s.active); /* vanilla SHOCK: no overlay */
    CHECK(pc_emote_seq_begin(&s, 0) == 0 && pc_emote_seq_begin(&s, 42) == 0);
    t = run(PC_EMOTE_HOORAY, 16.6667f, 2000.0f, &steps, &reason);
    CHECK(t > 1990.0f && t < 2030.0f && steps == 0 && reason == PC_EMOTE_END_DONE);
    t = run(PC_EMOTE_PHEW, 16.6667f, 1500.0f, &steps, &reason); /* two breaths */
    CHECK(t > 2990.0f && t < 3040.0f && steps == 1 && reason == PC_EMOTE_END_DONE);
    /* Exercise loops its 12 clips until the cap (60 s), at 60 fps and at 1000 fps alike (wall-clock, not frames) */
    t = run(PC_EMOTE_EXERCISE, 16.6667f, 1000.0f, &steps, &reason);
    CHECK(reason == PC_EMOTE_END_CAP && t >= 60000.0f && t < 60040.0f && steps >= 58);
    t = run(PC_EMOTE_EXERCISE, 1.0f, 1000.0f, &steps, &reason);
    CHECK(reason == PC_EMOTE_END_CAP && t >= 60000.0f && t < 60002.0f && steps >= 58);
    /* a clip that never ends (missing data) is capped at 10 s, also at 1000 fps */
    t = run(PC_EMOTE_PRAY, 1.0f, 1e9f, &steps, &reason);
    CHECK(reason == PC_EMOTE_END_CAP && t >= 10000.0f && t < 10002.0f);
    /* input ends it at once; a 5 s hitch is not counted */
    CHECK(pc_emote_seq_begin(&s, PC_EMOTE_EXERCISE));
    CHECK(pc_emote_seq_tick(&s, 0, 5000.0f, 0) == PC_EMOTE_T_CONTINUE && s.elapsed_ms == 0.0f);
    CHECK(pc_emote_seq_tick(&s, 1, 16.0f, 0) == PC_EMOTE_T_NEXT && s.step == 1);
    CHECK(pc_emote_seq_tick(&s, 0, 16.0f, 1) == PC_EMOTE_T_END && s.end_reason == PC_EMOTE_END_INPUT && !s.active);
    CHECK(pc_emote_seq_tick(&s, 0, 16.0f, 0) == PC_EMOTE_T_END); /* inactive stays ended */
    CHECK(strcmp(pc_emote_end_name(PC_EMOTE_END_CAP), "cap") == 0 && strcmp(pc_emote_end_name(PC_EMOTE_END_STATE), "state") == 0);

    /* ---- W: the wire masking */
    CHECK(pc_emote_wire_index(7, 1, 0) == 74);
    CHECK(pc_emote_wire_index(7, 0, 0) == 7);
    CHECK(pc_emote_wire_index(7, 1, 1) == 7);   /* station zone: the real index */
    CHECK(pc_emote_wire_index(8, 1, 0) == 8);   /* only inside WAIT */
    CHECK(pc_emote_wire_index(99, 1, 0) == 99); /* SHOCK is the vanilla state */
    CHECK(pc_emote_station_zone(1, 2200.0f, 820.0f) && pc_emote_station_zone(1, 2300.0f, 900.0f));
    CHECK(!pc_emote_station_zone(1, 2330.0f, 820.0f) && !pc_emote_station_zone(1, 2200.0f, 950.0f) && !pc_emote_station_zone(0, 2200.0f, 820.0f));
    {
        int id = -1;
        uint8_t f = 0;
        CHECK(pc_emote_net_active(&id, &f) == 0 && id == 0 && f == PC_EMOTE_FACE_NONE);
        pc_emote_net_set(1, PC_EMOTE_PHEW, 0x44);
        CHECK(pc_emote_net_active(&id, &f) == 1 && id == PC_EMOTE_PHEW && f == 0x44);
        pc_emote_net_set(0, PC_EMOTE_PHEW, 0x44);
        CHECK(pc_emote_net_active(&id, &f) == 0 && id == 0 && f == PC_EMOTE_FACE_NONE);
    }

    printf("%d/%d checks passed\n", s_n - s_fail, s_n);
    return s_fail ? 1 : 0;
}
