/* arrival_train_selftest.c - native unit test of pc/include/pc_arrival_train.h (the synced arrival train: the train replica, the host's arrival
 * timeline / seat queue, the wire shape, the client's arrival decision, Porter's door rule). Built and run by test_arrival_train_native.py. */
#include <stdio.h>
#include <string.h>
#include <math.h>
#include "pc_arrival_train.h"

static int s_pass = 0, s_fail = 0;
static void ck(const char* d, int c) {
    if (c) {
        s_pass++;
        printf("PASS: %s\n", d);
    } else {
        s_fail++;
        printf("FAIL: %s\n", d);
    }
}

static const PCATTrainParams k_v0 = { 2.0f, 6.0f, 0.01f, 0.005f, 0.00345f, 0.00345f }; /* VERSION 0 (GAFE01) mTRC_* */

static const PCATTrain* find_train(const PCATQueue* q, int id) {
    int t;
    for (t = 0; t < PCAT_MAX_TRAINS; t++) {
        if (q->train[t].used && q->train[t].id == id) {
            return &q->train[t];
        }
    }
    return NULL;
}

int main(void) {
    PCATQueue q;
    PCATTrainSim s;
    PCArrTrainMsg m;
    int t, k, r;
    unsigned af, df;
    uint32_t now, arrive_ms, depart_ms;

    /* ---- sizes ---- */
    ck("wire: PCArrTrainWire is 20 bytes, PCArrTrainMsg is 48 bytes, id 105", sizeof(PCArrTrainWire) == 20 && sizeof(PCArrTrainMsg) == 48 && PCAT_MSG_ID == 105u);

    /* ---- the train replica ---- */
    af = pcat_sim_arrive_frames(&k_v0);
    df = pcat_sim_depart_frames(&k_v0);
    ck("sim: the arrival takes 578 frames (9.6 s) from mTRC_demo_init to WAIT_STOPPED", af == 578u);
    pcat_sim_at(&s, &k_v0, 100000u);
    ck("sim: it stops at the station (x 2366 +-2, the logged stop x 2365), speed 0, action 5", s.action == 5u && fabsf(s.x - 2366.0f) < 2.0f && s.speed == 0.0f);
    pcat_sim_at(&s, &k_v0, 0u);
    ck("sim: frame 0 = mTRC_demo_init (action 2 at x 2037, slow speed)", s.action == 2u && s.x == 2037.0f && s.speed == 2.0f);
    pcat_sim_at(&s, &k_v0, 140u);
    ck("sim: after 140 frames it is braking (action 3) past x 2165", s.action == 3u && s.x > 2165.0f && s.speed < 2.0f);
    ck("sim: the departure takes 1621 frames (signal -> x > 4400)", df == 1621u);
    ck("sim: ms <-> frames (9634 ms = 578 frames)", pcat_ms_to_frames((int32_t)pcat_frames_to_ms(af)) == af && pcat_ms_to_frames(-5) == 0u && pcat_ms_to_frames(1000) == 60u);

    arrive_ms = pcat_frames_to_ms(af);
    depart_ms = pcat_frames_to_ms(df) + 500u;

    /* ---- the queue: back-to-back guests ---- */
    pcat_queue_init(&q, arrive_ms, depart_ms);
    now = 1000u;
    r = pcat_host_request(&q, 1, now, &t, &k);
    ck("queue: the first guest gets a NEW train starting now, seat 0", r == PCAT_REQ_SEATED_NEW && t == 0 && k == 0 && q.train[0].start_set && q.train[0].start_ms == now && q.train[0].id == 1);
    r = pcat_host_request(&q, 1, now + 10, &t, &k);
    ck("queue: asking again keeps the seat (ALREADY)", r == PCAT_REQ_ALREADY && t == 0 && k == 0 && q.train[0].n == 1);
    r = pcat_host_request(&q, 2, now + 2000, &t, &k);
    ck("queue: a second guest 2 s later rides the SAME train, seat 1", r == PCAT_REQ_SEATED && t == 0 && k == 1 && q.train[0].n == 2);
    {
        const uint8_t st[3] = { PCAT_PS_RIDING, PCAT_PS_RIDING, 0 };
        ck("seats: seat 0 at the door spot, seat 1 one spot (40) behind", pcat_seat_dx_states(st, 2, 0) == 0.0f && pcat_seat_dx_states(st, 2, 1) == -40.0f);
        ck("seats: only seat 0 may step off", pcat_may_get_off_states(st, 2, 0) && !pcat_may_get_off_states(st, 2, 1));
    }
    now += arrive_ms + 5; /* the train is stopped */
    ck("queue: stopped after arrive_ms", pcat_train_stopped(&q, &q.train[0], now));
    pcat_host_tick(&q, now);
    ck("queue: no depart while both passengers ride", !q.train[0].depart_set);
    ck("progress: seat 0 steps off", pcat_host_progress(&q, 1, PCAT_OP_STEPPED_OFF, now) && q.train[0].pass[0].state == PCAT_PS_OFF);
    {
        uint8_t st[3];
        st[0] = q.train[0].pass[0].state;
        st[1] = q.train[0].pass[1].state;
        ck("seats: with seat 0 off, seat 1 moves right to the door spot (dx 0) but waits (Porter is welcoming seat 0)",
           pcat_seat_dx_states(st, 2, 1) == 0.0f && !pcat_may_get_off_states(st, 2, 1));
    }
    ck("progress: a duplicate STEPPED_OFF is ignored", !pcat_host_progress(&q, 1, PCAT_OP_STEPPED_OFF, now + 1));
    now += 15000;
    ck("progress: seat 0 welcomed", pcat_host_progress(&q, 1, PCAT_OP_WELCOMED, now) && q.train[0].pass[0].state == PCAT_PS_DONE);
    {
        uint8_t st[3];
        st[0] = q.train[0].pass[0].state;
        st[1] = q.train[0].pass[1].state;
        ck("seats: seat 1 is the next one off", pcat_may_get_off_states(st, 2, 1) && pcat_door_index(&q.train[0]) == 1);
    }
    pcat_host_tick(&q, now + 1);
    ck("queue: still no depart (seat 1 aboard), 15 s after the stop", !q.train[0].depart_set);
    now += 3000;
    pcat_host_progress(&q, 2, PCAT_OP_STEPPED_OFF, now);
    pcat_host_tick(&q, now + 1000);
    ck("queue: no depart within 4 s of the last stepping off", !q.train[0].depart_set);
    pcat_host_tick(&q, now + PCAT_DEPART_AFTER_OFF_MS);
    ck("queue: departs once all are off, >= 20 s after the stop and 4 s after the last off", q.train[0].depart_set && q.train[0].depart_ms == now + PCAT_DEPART_AFTER_OFF_MS);
    now += PCAT_DEPART_AFTER_OFF_MS;

    /* ---- a guest joining while the train leaves ---- */
    r = pcat_host_request(&q, 3, now + 1000, &t, &k);
    ck("leave: a guest asking while the train pulls out gets a NEW train (seat 0), starting PCAT_CLEAR_MS after the depart",
       r == PCAT_REQ_SEATED_NEW && t == 1 && k == 0 && q.train[1].start_set && q.train[1].start_ms == now + PCAT_CLEAR_MS && q.train[1].id == 2);
    {
        pcat_build_state(&q, now + 1000, 7, &m);
        ck("state: both trains on the wire, the new one starts in the future (negative age), the old one departing",
           pcat_msg_ok(&m) && m.n_trains == 2 && m.train[1].start_ago_ms == -(int32_t)(PCAT_CLEAR_MS - 1000) && (m.train[0].flags & PCAT_TF_DEPART) && m.train[0].depart_ago_ms == 1000);
    }
    /* the first train retires once gone and everybody welcomed */
    pcat_host_tick(&q, now + depart_ms);
    ck("retire: the departed train stays while seat 1 has not been welcomed", find_train(&q, 1) != NULL);
    pcat_host_progress(&q, 2, PCAT_OP_WELCOMED, now + depart_ms);
    pcat_host_tick(&q, now + depart_ms + 1);
    ck("retire: then it is retired; the new train moves to train[0]", find_train(&q, 1) == NULL && q.train[0].used && q.train[0].id == 2 && !q.train[1].used);

    /* ---- timeouts / gone ---- */
    pcat_queue_init(&q, arrive_ms, depart_ms);
    now = 50000u;
    pcat_host_request(&q, 4, now, &t, &k);
    pcat_host_request(&q, 5, now, &t, &k);
    now += arrive_ms;
    pcat_host_tick(&q, now);
    pcat_host_tick(&q, now + PCAT_GETOFF_TIMEOUT_MS);
    ck("timeout: a passenger that never steps off on its turn is GONE after the get-off timeout", q.train[0].pass[0].state == PCAT_PS_GONE && pcat_door_index(&q.train[0]) == 1);
    pcat_host_tick(&q, now + PCAT_GETOFF_TIMEOUT_MS + 1);
    pcat_host_progress(&q, 5, PCAT_OP_STEPPED_OFF, now + PCAT_GETOFF_TIMEOUT_MS + 1);
    pcat_host_tick(&q, now + PCAT_GETOFF_TIMEOUT_MS + 2 + PCAT_WELCOME_TIMEOUT_MS);
    ck("timeout: a passenger that never reports the welcome counts as welcomed", q.train[0].pass[1].state == PCAT_PS_DONE);
    pcat_queue_init(&q, arrive_ms, depart_ms);
    pcat_host_request(&q, 6, 1000, &t, &k);
    ck("gone: a disconnected passenger is GONE", pcat_host_peer_gone(&q, 6) && q.train[0].pass[0].state == PCAT_PS_GONE && !pcat_host_peer_gone(&q, 6));
    pcat_host_tick(&q, 1000 + arrive_ms + PCAT_VANILLA_WAIT_MS);
    ck("gone: the train with nobody left departs at the vanilla 20 s", q.train[0].depart_set);
    pcat_queue_init(&q, arrive_ms, depart_ms);
    pcat_host_request(&q, 7, 1000, &t, &k);
    pcat_host_progress(&q, 7, PCAT_OP_STEPPED_OFF, 1000 + arrive_ms + 100);
    pcat_host_tick(&q, 1000 + arrive_ms + 5000);
    ck("solo: one passenger off at once: no depart before the vanilla 20 s", !q.train[0].depart_set);
    pcat_host_tick(&q, 1000 + arrive_ms + PCAT_VANILLA_WAIT_MS);
    ck("solo: departs at the vanilla 20 s after the stop (the previous timing)", q.train[0].depart_set);

    /* ---- full train: the 4th rides the next one, started after the first departs ---- */
    pcat_queue_init(&q, arrive_ms, depart_ms);
    for (k = 0; k < 3; k++) {
        pcat_host_request(&q, (uint8_t)(10 + k), 1000, &t, &r);
    }
    r = pcat_host_request(&q, 13, 1500, &t, &k);
    ck("full: a 4th guest gets a NEW train without a start yet (waits for the full one to depart)", r == PCAT_REQ_SEATED_NEW && t == 1 && !q.train[1].start_set);
    r = pcat_host_request(&q, 14, 1600, &t, &k);
    ck("full: a 5th guest joins that queued train", r == PCAT_REQ_SEATED && t == 1 && k == 1);
    r = pcat_host_request(&q, 15, 1600, &t, &k);
    r = pcat_host_request(&q, 16, 1600, &t, &k);
    ck("full: with both trains full a request is refused (FULL)", r == PCAT_REQ_FULL);
    pcat_host_tick(&q, 1000 + arrive_ms + PCAT_STOP_HARD_CAP_MS);
    ck("cap: the hard cap departs a train whose passengers never step off (they are GONE)", q.train[0].depart_set && q.train[0].pass[0].state == PCAT_PS_GONE);
    pcat_host_tick(&q, 1000 + arrive_ms + PCAT_STOP_HARD_CAP_MS + 1);
    ck("full: the queued train got its start = depart + PCAT_CLEAR_MS", q.train[1].start_set && q.train[1].start_ms == q.train[0].depart_ms + PCAT_CLEAR_MS);

    /* ---- wire shape ---- */
    pcat_queue_init(&q, arrive_ms, depart_ms);
    pcat_build_state(&q, 5, 1, &m);
    ck("wire: an empty STATE is well-formed (n_trains 0)", pcat_msg_ok(&m) && m.n_trains == 0 && m.train[0].peer[0] == PCAT_PEER_NONE);
    pcat_host_request(&q, 2, 100, &t, &k);
    pcat_build_state(&q, 600, 2, &m);
    ck("wire: one train, age 500 ms, passenger 2 riding", pcat_msg_ok(&m) && m.n_trains == 1 && m.train[0].start_ago_ms == 500 && m.train[0].n == 1 && m.train[0].peer[0] == 2 &&
                                                          m.train[0].pstate[0] == PCAT_PS_RIDING && m.train[0].peer[1] == PCAT_PEER_NONE);
    {
        PCArrTrainMsg b = m;
        b.version = 2;
        ck("wire: a wrong version is refused", !pcat_msg_ok(&b));
        b = m;
        b.train[0].flags |= 0x40u;
        ck("wire: an unknown train flag is refused", !pcat_msg_ok(&b));
        b = m;
        b.train[0].n = 4;
        ck("wire: more than 3 passengers is refused", !pcat_msg_ok(&b));
        b = m;
        b.train[0].pstate[0] = 4;
        ck("wire: an unknown passenger state is refused", !pcat_msg_ok(&b));
        b = m;
        b.n_trains = 3;
        ck("wire: more than 2 trains is refused", !pcat_msg_ok(&b));
        b = m;
        b.train[1].flags = PCAT_TF_USED;
        ck("wire: a used entry beyond n_trains is refused", !pcat_msg_ok(&b));
        b = m;
        b.train[0].flags = PCAT_TF_USED | PCAT_TF_DEPART;
        ck("wire: DEPART without START is refused", !pcat_msg_ok(&b));
        b = m;
        b.train[0].id = 0;
        ck("wire: train id 0 is refused", !pcat_msg_ok(&b));
        memset(&b, 0, sizeof(b));
        b.msg_type = PCAT_MSG_ID;
        b.version = 1;
        b.op = PCAT_OP_REQUEST;
        ck("wire: a client REQUEST (all zero) is well-formed", pcat_msg_ok(&b));
        b.train[0].id = 1;
        ck("wire: a client op with train bytes is refused", !pcat_msg_ok(&b));
        b.train[0].id = 0;
        b.op = 9;
        ck("wire: an unknown op is refused", !pcat_msg_ok(&b));
    }

    /* ---- the arriving client's decision ---- */
    ck("decide: no capability (old host / kill switch) -> 0 at once (the previous behaviour)", pcat_client_arrival_decide(0, 0, 0, 0, 0) == 0);
    ck("decide: capable, no seat yet -> hold", pcat_client_arrival_decide(1, 100, 0, 0, 0) == -1);
    ck("decide: no seat within PCAT_REQUEST_REPLY_MS -> fall back", pcat_client_arrival_decide(1, PCAT_REQUEST_REPLY_MS, 0, 0, 0) == 0);
    ck("decide: a seat on a started train -> synced", pcat_client_arrival_decide(1, 200, 1, 1, 50) == 1);
    ck("decide: a seat on a train that starts later -> hold", pcat_client_arrival_decide(1, 200, 1, 1, -9000) == -1 && pcat_client_arrival_decide(1, 200, 1, 0, 0) == -1);
    ck("decide: the train still has not started after PCAT_HOLD_CAP_MS -> fall back", pcat_client_arrival_decide(1, PCAT_HOLD_CAP_MS, 1, 1, -100) == 0);

    /* ---- Porter / dx ---- */
    {
        PCATView v;
        memset(&v, 0, sizeof(v));
        ck("porter: no train -> his place", !pcat_porter_at_door(&v));
        v.has_train = 1;
        v.started = 1;
        v.n = 1;
        v.pstate[0] = PCAT_PS_RIDING;
        ck("porter: a train coming in with a passenger -> the door", pcat_porter_at_door(&v));
        v.pstate[0] = PCAT_PS_DONE;
        ck("porter: everybody welcomed but the train not told to leave -> still the door", pcat_porter_at_door(&v));
        v.depart_set = 1;
        ck("porter: everybody welcomed and the train leaving -> back to his place", !pcat_porter_at_door(&v));
        v.pstate[0] = PCAT_PS_OFF;
        ck("porter: a passenger still being welcomed while the train leaves -> the door", pcat_porter_at_door(&v));
        v.started = 0;
        ck("porter: a train that has not started -> his place", !pcat_porter_at_door(&v));
    }
    ck("dx: moves right at PCAT_PASS_DX_SPEED per frame and never overshoots", pcat_dx_approach(-40.0f, 0.0f, 1.0f) == -40.0f + PCAT_PASS_DX_SPEED && pcat_dx_approach(-0.3f, 0.0f, 1.0f) == 0.0f &&
                                                                               pcat_dx_approach(0.0f, -40.0f, 100.0f) == -40.0f);

    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail ? 1 : 0;
}
