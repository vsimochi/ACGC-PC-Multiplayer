/* action_ext_selftest.c - native model test of pc_action_ext.c (PUPPET ACTION_EXT, msg 96, presentation only).
 * Links the REAL pc_action_ext.c (no engine dependencies) and checks the 16-byte wire layout, the validation (every field range), the sender
 * change detector (entry / clip change / STOP restart / speed retune / leaving the allowlist / rate cap) and the receiver dynamic-row store
 * (binding to (main_index, entry_counter), stale / mismatched counters, newest record wins, END on leaving, TTL expiry, retune vs restart).
 * Build + run (git-bash, PATH appended with /c/msys64/ucrt64/bin), from pc/:
 *   gcc -std=gnu11 -Wall -Iinclude tools/net_spike/action_ext_selftest.c -o build64_w3/action_ext_selftest.exe && build64_w3/action_ext_selftest.exe */
#include "../../src/pc_action_ext.c"

#include <stddef.h>
#include <stdio.h>

static int s_fail = 0, s_n = 0;
#define CHECK(c) do { s_n++; if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); s_fail++; } } while (0)

static PCActionExtMsg good(void) {
    PCActionExtMsg m;
    memset(&m, 0, sizeof(m));
    m.msg_type = 96;
    m.main_index = PC_AEXT_IDX_LIE_BED;
    m.entry_counter = 3;
    m.anim0 = 41;
    m.anim1 = 41;
    m.part_table = 0;
    m.flags = (uint8_t)(3u << PC_AEXT_FL_PIN_SHIFT);
    m.speed_q = 16;
    m.face = PC_AEXT_FACE_NONE;
    return m;
}

static PCAextSample smp(int idx, int ctr, int a0, float frame, float speed, int repeat) {
    PCAextSample s;
    memset(&s, 0, sizeof(s));
    s.main_index = idx;
    s.counter = ctr;
    s.anim0 = a0;
    s.anim1 = a0;
    s.part_table = 0;
    s.repeat = repeat;
    s.speed = speed;
    s.frame = frame;
    return s;
}

int main(void) {
    PCActionExtMsg m, out;
    PCAextSender snd;
    PCAextStore st;
    PCAextView v;
    const PCAextRecord* rec;
    int i, n, d;

    /* ---- W: wire layout */
    CHECK(sizeof(PCActionExtMsg) == 16);
    CHECK(offsetof(PCActionExtMsg, main_index) == 2 && offsetof(PCActionExtMsg, entry_counter) == 3 && offsetof(PCActionExtMsg, anim0) == 4);
    CHECK(offsetof(PCActionExtMsg, flags) == 7 && offsetof(PCActionExtMsg, speed_q) == 8 && offsetof(PCActionExtMsg, face) == 10);
    CHECK(offsetof(PCActionExtMsg, aux) == 12 && offsetof(PCActionExtMsg, clip_seq) == 14 && offsetof(PCActionExtMsg, rsv) == 15);

    /* ---- V: validation */
    m = good();
    CHECK(pc_aext_msg_ok(&m));
    m = good(); m.msg_type = 95; CHECK(!pc_aext_msg_ok(&m));
    m = good(); m.main_index = 7; CHECK(!pc_aext_msg_ok(&m));            /* WAIT: not allowlisted */
    m = good(); m.main_index = 27; CHECK(!pc_aext_msg_ok(&m));           /* WAIT_BED keeps its static row */
    m = good(); m.main_index = 112; CHECK(!pc_aext_msg_ok(&m));          /* boat demos: won't fix */
    m = good(); m.main_index = 200; CHECK(!pc_aext_msg_ok(&m));
    m = good(); m.entry_counter = 16; CHECK(!pc_aext_msg_ok(&m));
    m = good(); m.anim0 = PC_AEXT_ANIM_NUM; CHECK(!pc_aext_msg_ok(&m));
    m = good(); m.anim0 = PC_AEXT_ANIM_NUM - 1; CHECK(pc_aext_msg_ok(&m));
    m = good(); m.anim1 = 200; CHECK(!pc_aext_msg_ok(&m));
    m = good(); m.part_table = 5; CHECK(!pc_aext_msg_ok(&m));
    m = good(); m.part_table = 4; CHECK(pc_aext_msg_ok(&m));
    m = good(); m.flags |= 0x20; CHECK(!pc_aext_msg_ok(&m));
    m = good(); m.flags = 0x1F; CHECK(pc_aext_msg_ok(&m));
    m = good(); m.face = (uint8_t)((8u << 4) | 0u); CHECK(!pc_aext_msg_ok(&m));
    m = good(); m.face = (uint8_t)((7u << 4) | 6u); CHECK(!pc_aext_msg_ok(&m));
    m = good(); m.face = (uint8_t)((7u << 4) | 5u); CHECK(pc_aext_msg_ok(&m));
    m = good(); m.rsv = 1; CHECK(!pc_aext_msg_ok(&m));
    m = good(); m.net_player_id = 8; CHECK(pc_aext_msg_ok(&m));          /* the relay stamps it: not validated here */
    CHECK(!pc_aext_msg_ok(NULL));
    n = 0;
    for (i = 0; i < 256; i++) {
        n += pc_aext_index_allowed(i);
    }
    CHECK(n == 21);
    CHECK(pc_aext_face_rule(PC_AEXT_IDX_TALK, PC_AEXT_ANIM_GAAAN2) == 0x65 && pc_aext_face_rule(PC_AEXT_IDX_DEMO_WAIT, PC_AEXT_ANIM_GAAAN2) == 0x65);
    CHECK(pc_aext_face_rule(PC_AEXT_IDX_TALK, 110) == PC_AEXT_FACE_NONE && pc_aext_face_rule(PC_AEXT_IDX_LIE_BED, PC_AEXT_ANIM_GAAAN2) == PC_AEXT_FACE_NONE);
    CHECK(pc_aext_speed_q(0.5f) == 16 && pc_aext_speed_q(1.0f) == 32 && pc_aext_speed_q(-0.5f) == 16 && pc_aext_speed_q(100.0f) == 255);
    CHECK(pc_aext_speed_f(16) == 0.5f && pc_aext_speed_f(0) == 0.0f);

    /* ---- S: sender */
    pc_aext_sender_reset(&snd);
    {
        PCAextSample s = smp(7, 1, 0, 3.0f, 0.5f, 1); /* WAIT: nothing */
        CHECK(!pc_aext_sender_step(&snd, &s, 10.0, &out));
    }
    {
        PCAextSample s = smp(PC_AEXT_IDX_LIE_BED, 2, 41, 1.5f, 0.5f, 0);
        s.pin = 3;
        CHECK(pc_aext_sender_step(&snd, &s, 11.0, &out));             /* entry */
        CHECK(pc_aext_msg_ok(&out) && out.main_index == 26 && out.entry_counter == 2 && out.anim0 == 41 && out.speed_q == 16 && out.start_frame == 1 &&
              out.flags == (uint8_t)(3u << 1) && out.face == PC_AEXT_FACE_NONE && out.net_player_id == 0 && out.clip_seq == 1);
        s.frame = 2.0f;
        CHECK(!pc_aext_sender_step(&snd, &s, 12.0, &out));            /* same clip, advancing */
        s.frame = 30.0f;
        CHECK(!pc_aext_sender_step(&snd, &s, 40.0, &out));
        s.frame = 1.0f;                                               /* a STOP clip went backwards: restarted */
        CHECK(pc_aext_sender_step(&snd, &s, 41.0, &out) && out.clip_seq == 2);
        s.counter = 3;                                                /* re-entry of the same index: new counter */
        CHECK(pc_aext_sender_step(&snd, &s, 42.0, &out) && out.entry_counter == 3 && out.clip_seq == 3);
    }
    {
        PCAextSample s = smp(PC_AEXT_IDX_TALK, 4, 0, 1.0f, 0.5f, 1);  /* talk: WAIT1 */
        CHECK(pc_aext_sender_step(&snd, &s, 50.0, &out) && out.anim0 == 0 && (out.flags & PC_AEXT_FL_REPEAT));
        s.frame = 30.0f; s.frame = 2.0f;                              /* REPEAT wrap: not a restart */
        CHECK(!pc_aext_sender_step(&snd, &s, 51.0, &out));
        s.anim0 = s.anim1 = 110; s.repeat = 0; s.frame = 1.0f;       /* demo order 2: GAAAN1 */
        CHECK(pc_aext_sender_step(&snd, &s, 52.0, &out) && out.anim0 == 110 && !(out.flags & PC_AEXT_FL_REPEAT) && out.face == PC_AEXT_FACE_NONE);
        s.anim0 = s.anim1 = PC_AEXT_ANIM_GAAAN2; s.repeat = 1;        /* the end of GAAAN1: GAAAN2 loop, face 6 / 5 */
        CHECK(pc_aext_sender_step(&snd, &s, 64.0, &out) && out.anim0 == PC_AEXT_ANIM_GAAAN2 && out.face == 0x65);
        s.speed = 0.52f;                                              /* a small drift: no retune */
        CHECK(!pc_aext_sender_step(&snd, &s, 90.0, &out));
    }
    {
        PCAextSample s = smp(PC_AEXT_IDX_RADIO_EXERCISE, 5, 144, 1.0f, 0.5f, 0);
        CHECK(pc_aext_sender_step(&snd, &s, 100.0, &out));
        s.speed = 0.7f; s.frame = 2.0f;
        CHECK(!pc_aext_sender_step(&snd, &s, 105.0, &out));          /* too soon for a retune */
        s.frame = 3.0f;
        CHECK(pc_aext_sender_step(&snd, &s, 116.0, &out) && out.speed_q == pc_aext_speed_q(0.7f) && out.clip_seq == snd.clip_seq);
        {
            uint8_t seq = out.clip_seq;
            s.frame = 4.0f; s.speed = 0.71f;
            CHECK(!pc_aext_sender_step(&snd, &s, 140.0, &out));
            CHECK(seq == snd.clip_seq);                               /* a retune keeps the clip instance */
            s.frame = 60.0f; s.speed = 0.0f;                          /* the STOP clip ended (speed 0): no retune */
            CHECK(!pc_aext_sender_step(&snd, &s, 400.0, &out));
        }
    }
    {
        PCAextSample s = smp(7, 5, 0, 1.0f, 0.5f, 1);                 /* leave */
        CHECK(!pc_aext_sender_step(&snd, &s, 150.0, &out) && !snd.have);
        s = smp(PC_AEXT_IDX_RADIO_EXERCISE, 5, 144, 1.0f, 0.5f, 0);   /* same pair again after leaving: sends (the detector forgot it) */
        CHECK(pc_aext_sender_step(&snd, &s, 151.0, &out));
        s.anim0 = 157;                                                /* an out-of-range clip is never sent */
        CHECK(!pc_aext_sender_step(&snd, &s, 152.0, &out));
    }
    {
        /* rate cap: a pathological state that changes clip every frame sends a burst of 12, then 8 per second */
        int sent = 0;
        pc_aext_sender_reset(&snd);
        for (i = 0; i < 120; i++) {
            PCAextSample s = smp(PC_AEXT_IDX_WASH_CAR, 1, 98 + (i & 1), 1.0f, 0.5f, 0);
            sent += pc_aext_sender_step(&snd, &s, 1000.0 + i, &out);
        }
        CHECK(sent >= 12 + 14 && sent <= 12 + 17);
        CHECK(snd.suppressed > 0);
    }

    /* ---- R: receiver store */
    pc_aext_store_reset(&st);
    memset(&v, 0, sizeof(v));
    m = good();
    pc_aext_store_put(&st, &m, 100.0);
    d = pc_aext_decide(&st, &v, 1, 7, 2, 101.0, &rec);                /* puppet still in WAIT: pending */
    CHECK(d == PC_AEXT_D_NONE && rec == NULL && st.r[0].valid);
    d = pc_aext_decide(&st, &v, 1, PC_AEXT_IDX_LIE_BED, 2, 102.0, &rec); /* the same index, a different (stale) counter: never plays */
    CHECK(d == PC_AEXT_D_NONE);
    d = pc_aext_decide(&st, &v, 1, PC_AEXT_IDX_LIE_BED, 3, 103.0, &rec);
    CHECK(d == PC_AEXT_D_START && rec != NULL && rec->m.anim0 == 41 && v.on);
    d = pc_aext_decide(&st, &v, 1, PC_AEXT_IDX_LIE_BED, 3, 104.0, &rec);
    CHECK(d == PC_AEXT_D_HOLD);
    m.speed_q = 20;                                                   /* same clip instance, new speed: retune */
    pc_aext_store_put(&st, &m, 105.0);
    d = pc_aext_decide(&st, &v, 1, PC_AEXT_IDX_LIE_BED, 3, 105.0, &rec);
    CHECK(d == PC_AEXT_D_RETUNE && rec->m.speed_q == 20);
    m.clip_seq = 9;                                                   /* new clip instance (restart) */
    pc_aext_store_put(&st, &m, 106.0);
    d = pc_aext_decide(&st, &v, 1, PC_AEXT_IDX_LIE_BED, 3, 106.0, &rec);
    CHECK(d == PC_AEXT_D_START && rec->m.clip_seq == 9);
    d = pc_aext_decide(&st, &v, 1, PC_AEXT_IDX_WAIT_BED, 4, 107.0, &rec); /* the pair moved on: END, the shown records expire */
    CHECK(d == PC_AEXT_D_END && !v.on);
    n = 0;
    for (i = 0; i < PC_AEXT_RING; i++) {
        n += st.r[i].valid;
    }
    CHECK(n == 0);
    d = pc_aext_decide(&st, &v, 1, PC_AEXT_IDX_LIE_BED, 3, 108.0, &rec); /* a counter wrap back to (26, 3) does NOT replay the consumed record */
    CHECK(d == PC_AEXT_D_NONE);
    /* two quick entries arriving before the puppet reaches either: both are kept, each plays with its own pair */
    m = good(); m.main_index = PC_AEXT_IDX_OPEN_FURNITURE; m.entry_counter = 5; m.anim0 = m.anim1 = 43;
    pc_aext_store_put(&st, &m, 200.0);
    m = good(); m.main_index = PC_AEXT_IDX_WAIT_OPEN_FURNITURE; m.entry_counter = 6; m.anim0 = m.anim1 = 54;
    pc_aext_store_put(&st, &m, 201.0);
    d = pc_aext_decide(&st, &v, 1, PC_AEXT_IDX_OPEN_FURNITURE, 5, 205.0, &rec);
    CHECK(d == PC_AEXT_D_START && rec->m.anim0 == 43);
    d = pc_aext_decide(&st, &v, 1, PC_AEXT_IDX_WAIT_OPEN_FURNITURE, 6, 240.0, &rec);
    CHECK(d == PC_AEXT_D_START && rec->m.anim0 == 54);
    /* newest record of the same pair wins (talk WAIT1 -> GAAAN1 -> GAAAN2 arriving together) */
    pc_aext_store_reset(&st);
    memset(&v, 0, sizeof(v));
    m = good(); m.main_index = PC_AEXT_IDX_TALK; m.entry_counter = 1; m.anim0 = m.anim1 = 0; m.clip_seq = 1;
    pc_aext_store_put(&st, &m, 300.0);
    m.anim0 = m.anim1 = 110; m.clip_seq = 2;
    pc_aext_store_put(&st, &m, 300.0);
    m.anim0 = m.anim1 = 111; m.clip_seq = 3; m.face = 0x65;
    pc_aext_store_put(&st, &m, 300.0);
    d = pc_aext_decide(&st, &v, 1, PC_AEXT_IDX_TALK, 1, 301.0, &rec);
    CHECK(d == PC_AEXT_D_START && rec->m.anim0 == 111 && rec->m.face == 0x65);
    /* an invalid pair (v6 sender / index 0) never plays and ends a running overlay */
    d = pc_aext_decide(&st, &v, 0, 0, 0, 302.0, &rec);
    CHECK(d == PC_AEXT_D_END);
    /* TTL: an unseen record expires */
    pc_aext_store_reset(&st);
    memset(&v, 0, sizeof(v));
    m = good();
    pc_aext_store_put(&st, &m, 400.0);
    d = pc_aext_decide(&st, &v, 1, 7, 0, 400.0 + PC_AEXT_PENDING_TTL + 1.0, &rec);
    CHECK(d == PC_AEXT_D_NONE && !st.r[0].valid);
    d = pc_aext_decide(&st, &v, 1, PC_AEXT_IDX_LIE_BED, 3, 400.0 + PC_AEXT_PENDING_TTL + 2.0, &rec);
    CHECK(d == PC_AEXT_D_NONE);
    /* ring overflow keeps the 4 newest */
    pc_aext_store_reset(&st);
    for (i = 0; i < 6; i++) {
        m = good(); m.entry_counter = (uint8_t)i;
        pc_aext_store_put(&st, &m, 500.0);
    }
    n = 0;
    for (i = 0; i < PC_AEXT_RING; i++) {
        n += (st.r[i].valid && st.r[i].m.entry_counter >= 2);
    }
    CHECK(n == 4);

    printf("%d/%d checks passed\n", s_n - s_fail, s_n);
    return s_fail ? 1 : 0;
}
