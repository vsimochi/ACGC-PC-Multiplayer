/* fishing_view_selftest.c - native selftest of pc_fishing_view.c (worker P3: BOBBER_VIEW id 97 relay / view logic).
 * Build (from pc/): gcc -std=gnu11 -Wall -Iinclude tools/net_spike/fishing_view_selftest.c -lm -o fv.exe */
#include "../../src/pc_fishing_view.c"

#include <stdio.h>
#include <stdlib.h>

static int s_fail = 0, s_pass = 0;
#define CHECK(c, d)                                                       \
    do {                                                                  \
        if (c) {                                                          \
            s_pass++;                                                     \
        } else {                                                          \
            s_fail++;                                                     \
            printf("FAIL - %s (line %d)\n", d, __LINE__);                 \
        }                                                                 \
    } while (0)

static PCFvSample mk(float x, float y, float z, uint8_t uki, int8_t gyo, uint8_t scene) {
    PCFvSample s;
    memset(&s, 0, sizeof(s));
    s.active = 1;
    s.uki_status = uki;
    s.gyo_status = gyo;
    s.rod_type = 0;
    s.scene_id = scene;
    s.x = x;
    s.y = y;
    s.z = z;
    s.angle = 0x1234;
    return s;
}

int main(void) {
    static PCFvHost h;
    PCFvView v;
    PCBobberViewMsg m;
    PCFvSample s, got;
    float pp[3] = { 1000.0f, 40.0f, 1000.0f };
    int rel = -1, n, i;
    uint32_t t;

    /* ---- validation ---- */
    s = mk(1100.0f, 20.0f, 1000.0f, 4, 1, 7);
    CHECK(pc_fv_sample_ok(&s, pp), "a bobber 100 units from the angler is ok");
    CHECK(pc_fv_sample_ok(&s, NULL), "an unknown angler position skips the reach check");
    s = mk(1000.0f + PC_FV_MAX_REACH + 5.0f, 20.0f, 1000.0f, 4, 1, 7);
    CHECK(!pc_fv_sample_ok(&s, pp), "beyond the reach is rejected");
    s = mk(1100.0f, 40.0f + PC_FV_MAX_DY + 1.0f, 1000.0f, 4, 1, 7);
    CHECK(!pc_fv_sample_ok(&s, pp), "too high above the angler is rejected");
    s = mk(NAN, 20.0f, 1000.0f, 4, 1, 7);
    CHECK(!pc_fv_sample_ok(&s, NULL), "NaN rejected");
    s = mk(-5.0f, 20.0f, 1000.0f, 4, 1, 7);
    CHECK(!pc_fv_sample_ok(&s, NULL), "negative x rejected");
    s = mk(1100.0f, 20.0f, 1000.0f, 2, 1, 7);
    CHECK(!pc_fv_sample_ok(&s, pp), "uki status READY (not cast) rejected");
    s = mk(1100.0f, 20.0f, 1000.0f, 8, 1, 7);
    CHECK(!pc_fv_sample_ok(&s, pp), "uki status 8 rejected");
    s = mk(1100.0f, 20.0f, 1000.0f, 4, 11, 7);
    CHECK(!pc_fv_sample_ok(&s, pp), "gyo status 11 rejected");
    s = mk(1100.0f, 20.0f, 1000.0f, 4, 1, 7);
    s.rod_type = 2;
    CHECK(!pc_fv_sample_ok(&s, pp), "rod type 2 rejected");
    memset(&s, 0, sizeof(s));
    CHECK(pc_fv_sample_ok(&s, pp), "an inactive sample is always ok (it only clears)");

    /* ---- host: rate limit (<= 10 Hz) ---- */
    memset(&h, 0, sizeof(h));
    n = 0;
    for (t = 1000; t < 3000; t += 16) { /* input at ~60 Hz for 2 s, polled every frame */
        s = mk(1100.0f, 20.0f, 1000.0f, 4, 1, 7);
        CHECK(pc_fv_host_note(&h, 3, &s, pp, 1, (int16_t)(t / 16), t), "in-order input accepted");
        if (pc_fv_host_poll(&h, 3, t, &m, &rel)) {
            n++;
            CHECK(m.active == 1 && rel == 0 && m.origin == 3 && m.scene_id == 7 && m.msg_type == 97 && m.rsv == 0, "active view: unreliable, origin, scene");
        }
    }
    CHECK(n >= 18 && n <= 21, "2 s of 60 Hz input -> about 20 views (<= 10 Hz)");
    printf("INFO - 2 s of 60 Hz input produced %d BOBBER_VIEWs\n", n);

    /* reordered seq dropped */
    s = mk(1200.0f, 20.0f, 1000.0f, 4, 1, 7);
    CHECK(!pc_fv_host_note(&h, 3, &s, pp, 1, (int16_t)5, 3000), "an older BOBBER_STATE seq is dropped");
    CHECK(h.o[3].s.x == 1100.0f, "the dropped sample changed nothing");

    /* malformed input dropped */
    s = mk(NAN, 20.0f, 1000.0f, 4, 1, 7);
    CHECK(!pc_fv_host_note(&h, 3, &s, pp, 1, (int16_t)30000, 3000), "malformed input dropped");
    CHECK(!pc_fv_host_note(&h, 300, &s, pp, 0, 0, 3000) && !pc_fv_host_note(&h, -1, &s, pp, 0, 0, 3000), "out-of-range origin dropped");

    /* reel-in: gone, reliable, once */
    memset(&s, 0, sizeof(s));
    CHECK(pc_fv_host_note(&h, 3, &s, pp, 1, (int16_t)30001, 3010), "inactive input accepted");
    CHECK(pc_fv_host_poll(&h, 3, 3011, &m, &rel) && m.active == 0 && rel == 1 && m.scene_id == 7, "reel-in -> one reliable gone to the old scene");
    CHECK(!pc_fv_host_poll(&h, 3, 3200, &m, &rel), "nothing after the gone");
    /* a late unreliable active (older seq) after the gone */
    s = mk(1100.0f, 20.0f, 1000.0f, 4, 1, 7);
    CHECK(!pc_fv_host_note(&h, 3, &s, pp, 1, (int16_t)29999, 3210), "a late active older than the gone is dropped");
    CHECK(!pc_fv_host_poll(&h, 3, 3300, &m, &rel), "... and nothing is sent");

    /* stale input -> gone */
    memset(&h, 0, sizeof(h));
    s = mk(1100.0f, 20.0f, 1000.0f, 5, 4, 7);
    pc_fv_host_note(&h, 1, &s, pp, 1, 1, 5000);
    CHECK(pc_fv_host_poll(&h, 1, 5000, &m, &rel) && m.active == 1 && m.uki_status == 5 && m.gyo_status == 4, "first view goes out at once");
    CHECK(!pc_fv_host_poll(&h, 1, 5000 + PC_FV_HOST_STALE_MS - 50, &m, &rel) || m.active == 1, "still active before the stale timeout");
    CHECK(pc_fv_host_poll(&h, 1, 5000 + PC_FV_HOST_STALE_MS + 10, &m, &rel) && m.active == 0 && rel == 1, "stale -> gone");

    /* disconnect -> gone */
    memset(&h, 0, sizeof(h));
    pc_fv_host_note(&h, 2, &s, pp, 1, 1, 100);
    pc_fv_host_poll(&h, 2, 100, &m, &rel);
    pc_fv_host_drop(&h, 2);
    CHECK(pc_fv_host_poll(&h, 2, 110, &m, &rel) && m.active == 0 && rel == 1, "disconnect -> gone");
    pc_fv_host_drop(&h, 4);
    CHECK(!pc_fv_host_poll(&h, 4, 120, &m, &rel), "drop of an angler that never sent a view sends nothing");
    s = mk(1100.0f, 20.0f, 1000.0f, 4, 1, 7);
    CHECK(pc_fv_host_note(&h, 2, &s, pp, 1, 1, 200), "a reconnected angler's seq restarts (drop forgets the seq)");

    /* scene change -> gone (old scene) then active (new scene) */
    memset(&h, 0, sizeof(h));
    s = mk(1100.0f, 20.0f, 1000.0f, 4, 1, 7);
    pc_fv_host_note(&h, 5, &s, NULL, 0, 0, 100);
    pc_fv_host_poll(&h, 5, 100, &m, &rel);
    s.scene_id = 9;
    pc_fv_host_note(&h, 5, &s, NULL, 0, 0, 150);
    CHECK(pc_fv_host_poll(&h, 5, 160, &m, &rel) && m.active == 0 && m.scene_id == 7 && rel == 1, "scene change: gone to the old scene first");
    CHECK(pc_fv_host_poll(&h, 5, 170, &m, &rel) && m.active == 1 && m.scene_id == 9, "then active in the new scene");

    /* ---- receiver ---- */
    memset(&v, 0, sizeof(v));
    memset(&h, 0, sizeof(h));
    s = mk(1100.0f, 20.0f, 1000.0f, 4, 1, 7);
    pc_fv_host_note(&h, 6, &s, NULL, 0, 0, 1000);
    pc_fv_host_poll(&h, 6, 1000, &m, &rel);
    CHECK(pc_fv_msg_ok(&m), "a host-built view is well formed");
    CHECK(pc_fv_view_apply(&v, &m, 1, 7, 1000) && pc_fv_view_get(&v, 1000, &got) && got.x == 1100.0f, "same scene: shown");
    {
        PCBobberViewMsg dup = m;
        CHECK(!pc_fv_view_apply(&v, &dup, 1, 7, 1001), "duplicate seq ignored");
    }
    {
        PCBobberViewMsg bad = m;
        bad.rsv = 1;
        CHECK(!pc_fv_msg_ok(&bad), "rsv != 0 malformed");
        bad = m;
        bad.x = INFINITY;
        CHECK(!pc_fv_msg_ok(&bad), "inf malformed");
        bad = m;
        bad.active = 2;
        CHECK(!pc_fv_msg_ok(&bad), "active 2 malformed");
        bad = m;
        bad.seq = 0;
        CHECK(!pc_fv_msg_ok(&bad), "seq 0 malformed");
        bad = m;
        bad.msg_type = 74;
        CHECK(!pc_fv_msg_ok(&bad), "wrong type malformed");
    }
    CHECK(!pc_fv_view_get(&v, 1000 + PC_FV_VIEW_STALE_MS + 1, &got), "no update for 1.5 s -> hidden");
    /* other scene */
    memset(&v, 0, sizeof(v));
    pc_fv_host_note(&h, 6, &s, NULL, 0, 0, 1200);
    pc_fv_host_poll(&h, 6, 1200, &m, &rel);
    CHECK(pc_fv_view_apply(&v, &m, 1, 8, 1200) && !pc_fv_view_get(&v, 1200, &got), "another scene: not shown");
    CHECK(pc_fv_view_apply(&v, &m, 0, 7, 1201) == 0 || !pc_fv_view_get(&v, 1201, &got), "unknown own scene: not shown");
    /* gone clears */
    memset(&v, 0, sizeof(v));
    pc_fv_host_note(&h, 6, &s, NULL, 0, 0, 1300);
    pc_fv_host_poll(&h, 6, 1300, &m, &rel);
    pc_fv_view_apply(&v, &m, 1, 7, 1300);
    CHECK(pc_fv_view_get(&v, 1300, &got), "shown");
    {
        PCFvSample off;
        PCBobberViewMsg late = m;
        memset(&off, 0, sizeof(off));
        pc_fv_host_note(&h, 6, &off, NULL, 0, 0, 1310);
        CHECK(pc_fv_host_poll(&h, 6, 1310, &m, &rel) && m.active == 0, "gone built");
        CHECK(pc_fv_view_apply(&v, &m, 1, 7, 1311) && !pc_fv_view_get(&v, 1311, &got), "gone hides it at once");
        CHECK(!pc_fv_view_apply(&v, &late, 1, 7, 1312) && !pc_fv_view_get(&v, 1312, &got), "a late active older than the gone stays ignored");
    }
    /* seq wrap */
    memset(&v, 0, sizeof(v));
    memset(&h, 0, sizeof(h));
    h.o[6].seq_out = 65534;
    for (i = 0; i < 4; i++) {
        pc_fv_host_note(&h, 6, &s, NULL, 0, 0, 2000 + 200 * i);
        CHECK(pc_fv_host_poll(&h, 6, 2000 + 200 * i, &m, &rel) && m.seq != 0, "seq never 0");
        CHECK(pc_fv_view_apply(&v, &m, 1, 7, 2000 + 200 * i), "seq wraps forward");
    }
    /* a restarted host (seq from 1) after the forget window */
    m.seq = 1;
    CHECK(pc_fv_view_apply(&v, &m, 1, 7, 2600 + PC_FV_SEQ_FORGET_MS + 10), "a restarted host is accepted after the forget window");

    printf("%s - fishing_view selftest: %d passed, %d failed\n", s_fail ? "FAIL" : "PASS", s_pass, s_fail);
    return s_fail ? 1 : 0;
}
