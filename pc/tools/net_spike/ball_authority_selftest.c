/* ball_authority_selftest.c - native model test of pc_ball_authority.c (worker 2, workstream C: ball sync).
 * Links the REAL pc_ball_authority.c (it has no engine dependencies) and checks wire sizes, validation of malformed
 * values, the host kick validation (distance to sender MOVE position and to the host ball, rate limit, seq ordering,
 * no-ball rejection, bad peer ids), the host send cadence, client seq ordering / stale reset / fallback and interpolation.
 * Build + run (git-bash, PATH appended with /c/msys64/ucrt64/bin), from pc/:
 *   gcc -std=gnu11 -Wall -Iinclude tools/net_spike/ball_authority_selftest.c -lm -o build64/ball_authority_selftest.exe && build64/ball_authority_selftest.exe */
#include "../../src/pc_ball_authority.c"

#include <stdio.h>

static int s_fail = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); s_fail++; } } while (0)

static PCBallKickMsg kick(uint16_t seq, float x, float z, float sx, float sz) {
    PCBallKickMsg m;
    memset(&m, 0, sizeof(m));
    m.msg_type = 80;
    m.seq = seq;
    m.pos[0] = x; m.pos[1] = 0; m.pos[2] = z;
    m.spd_x = sx; m.spd_z = sz;
    return m;
}

int main(void) {
    static PCBallHost h;
    PCBallClient c;
    PCBallStateMsg st;
    PCBallKickMsg k;
    float ball[3] = { 1000, 0, 1000 }, sender[3] = { 1010, 0, 1000 }, zero[3] = { 0, 0, 0 }, spd[3] = { 2, 0, 0 };
    float cur[3];
    uint16_t seq = 0;
    float nanv = 0.0f;
    nanv = nanv / nanv;

    CHECK(sizeof(PCBallStateMsg) == 32 && sizeof(PCBallKickMsg) == 24);

    /* state validation */
    CHECK(pc_ball_host_fill_state(&st, 79, 1, PC_BALL_FLAG_ACTIVE, 2, 0x1234, ball, spd) == 1 && st.ball_type == 2 && st.msg_type == 79);
    st.ball_type = 3; CHECK(!pc_ball_state_msg_valid(&st)); st.ball_type = 0;
    st.flags = 0x80; CHECK(!pc_ball_state_msg_valid(&st)); st.flags = PC_BALL_FLAG_ACTIVE;
    st.pos[0] = nanv; CHECK(!pc_ball_state_msg_valid(&st)); st.pos[0] = 1000;
    st.pos[2] = -1; CHECK(!pc_ball_state_msg_valid(&st)); st.pos[2] = 1000;
    st.spd[1] = 1000; CHECK(!pc_ball_state_msg_valid(&st)); st.spd[1] = 0;
    st.rsv = 1; CHECK(!pc_ball_state_msg_valid(&st)); st.rsv = 0;
    CHECK(pc_ball_state_msg_valid(&st));

    /* kick validation */
    pc_ball_host_reset(&h);
    k = kick(1, 1010, 1000, 3, 0);
    CHECK(pc_ball_host_check_kick(&h, -1, &k, 1000, sender, ball, 1) == PC_BALL_KICK_BAD_PEER);
    CHECK(pc_ball_host_check_kick(&h, PC_BALL_MAX_PEERS, &k, 1000, sender, ball, 1) == PC_BALL_KICK_BAD_PEER);
    CHECK(pc_ball_host_check_kick(&h, 1, &k, 1000, sender, NULL, 0) == PC_BALL_KICK_NO_BALL);
    CHECK(pc_ball_host_check_kick(&h, 1, &k, 1000, NULL, ball, 1) == PC_BALL_KICK_NO_SENDER_POS);
    k = kick(1, 1010, 1000, 30, 0); CHECK(pc_ball_host_check_kick(&h, 1, &k, 1000, sender, ball, 1) == PC_BALL_KICK_MALFORMED);
    k = kick(1, 1010, 1000, nanv, 0); CHECK(pc_ball_host_check_kick(&h, 1, &k, 1000, sender, ball, 1) == PC_BALL_KICK_MALFORMED);
    k = kick(1, 1010, 1000, 3, 0); k.rsv = 9; CHECK(pc_ball_host_check_kick(&h, 1, &k, 1000, sender, ball, 1) == PC_BALL_KICK_MALFORMED);
    k = kick(1, 1100, 1000, 3, 0); CHECK(pc_ball_host_check_kick(&h, 1, &k, 1000, sender, ball, 1) == PC_BALL_KICK_FAR_SENDER);
    {
        float far_sender[3] = { 1100, 0, 1000 };
        CHECK(pc_ball_host_check_kick(&h, 1, &k, 1000, far_sender, ball, 1) == PC_BALL_KICK_FAR_BALL); /* teleport claim near itself, far from ball */
    }
    /* none of the rejections armed the rate limit */
    k = kick(1, 1010, 1000, 3, 0);
    CHECK(pc_ball_host_check_kick(&h, 1, &k, 1000, sender, ball, 1) == PC_BALL_KICK_OK);
    k = kick(2, 1010, 1000, 3, 0);
    CHECK(pc_ball_host_check_kick(&h, 1, &k, 1200, sender, ball, 1) == PC_BALL_KICK_RATE);
    CHECK(pc_ball_host_check_kick(&h, 2, &k, 1200, sender, ball, 1) == PC_BALL_KICK_OK); /* per-peer limit */
    k = kick(1, 1010, 1000, 3, 0);
    CHECK(pc_ball_host_check_kick(&h, 1, &k, 2000, sender, ball, 1) == PC_BALL_KICK_OLD_SEQ); /* replay */
    k = kick(2, 1010, 1000, 3, 0);
    CHECK(pc_ball_host_check_kick(&h, 1, &k, 2000, sender, ball, 1) == PC_BALL_KICK_OK);
    /* seq wrap */
    h.last_kick_seq[1] = 0xFFFF;
    k = kick(0, 1010, 1000, 3, 0);
    CHECK(pc_ball_host_check_kick(&h, 1, &k, 3000, sender, ball, 1) == PC_BALL_KICK_OK);
    pc_ball_host_forget_peer(&h, 1);
    k = kick(0, 1010, 1000, 3, 0);
    CHECK(pc_ball_host_check_kick(&h, 1, &k, 3001, sender, ball, 1) == PC_BALL_KICK_OK); /* reconnect starts fresh */
    pc_ball_host_forget_peer(&h, 999); /* bounds: no crash */

    /* host cadence */
    pc_ball_host_reset(&h);
    CHECK(pc_ball_host_should_send(&h, 0, 0, 0, &seq) == 1 && seq == 1);
    CHECK(pc_ball_host_should_send(&h, 500, 0, 0, &seq) == 0);
    CHECK(pc_ball_host_should_send(&h, 600, 1, 0, &seq) == 1 && seq == 2); /* started moving: immediately */
    CHECK(pc_ball_host_should_send(&h, 650, 1, 0, &seq) == 0);
    CHECK(pc_ball_host_should_send(&h, 700, 1, 0, &seq) == 1);
    CHECK(pc_ball_host_should_send(&h, 750, 0, 1, &seq) == 1); /* forced (kick applied / ball deleted) */
    CHECK(pc_ball_host_should_send(&h, 1700, 0, 0, &seq) == 0 && pc_ball_host_should_send(&h, 1750, 0, 0, &seq) == 1);

    /* client ordering / fallback */
    pc_ball_client_reset(&c);
    pc_ball_client_begin_wait(&c, 10000);
    CHECK(!pc_ball_client_should_fallback(&c, 13000));
    CHECK(pc_ball_client_should_fallback(&c, 14000));
    CHECK(pc_ball_client_step(&c, cur, 14000, 0.5f) == -1);
    pc_ball_host_fill_state(&st, 79, 10, PC_BALL_FLAG_ACTIVE, 1, 0, ball, zero);
    CHECK(pc_ball_client_accept_state(&c, &st, 14100) == 1 && !pc_ball_client_should_fallback(&c, 20000));
    CHECK(pc_ball_client_accept_state(&c, &st, 14150) == 0); /* duplicate */
    st.seq = 9; CHECK(pc_ball_client_accept_state(&c, &st, 14200) == 0); /* reordered */
    st.seq = 3; CHECK(pc_ball_client_accept_state(&c, &st, 14100 + PC_BALL_STATE_STALE_MS) == 1); /* restarted host after silence */
    st.seq = 4; st.ball_type = 7; CHECK(pc_ball_client_accept_state(&c, &st, 17200) == 0); /* malformed */

    /* interpolation / snap */
    cur[0] = 990; cur[1] = 0; cur[2] = 1000;
    CHECK(pc_ball_client_step(&c, cur, c.last_rx_ms, 0.5f) == 0 && cur[0] == 995.0f);
    cur[0] = 500;
    CHECK(pc_ball_client_step(&c, cur, c.last_rx_ms, 0.5f) == 1 && cur[0] == 1000.0f && cur[2] == 1000.0f);
    pc_ball_host_fill_state(&st, 79, 50, PC_BALL_FLAG_ACTIVE, 1, 0, ball, spd);
    CHECK(pc_ball_client_accept_state(&c, &st, 20000) == 1);
    cur[0] = 1000; cur[1] = 0; cur[2] = 1000;
    CHECK(pc_ball_client_step(&c, cur, 20100, 1.0f) == 0 && cur[0] > 1011.9f && cur[0] < 1012.1f); /* 6 frames * 2 */
    cur[0] = 1000;
    pc_ball_client_step(&c, cur, 25000, 1.0f); /* capped extrapolation: 12 frames */
    CHECK(cur[0] > 1023.9f && cur[0] < 1024.1f);

    /* client kick limiter */
    CHECK(pc_ball_client_next_kick(&c, 30000, &seq) == 1 && seq == 1);
    CHECK(pc_ball_client_next_kick(&c, 30100, &seq) == 0);
    CHECK(pc_ball_client_next_kick(&c, 30500, &seq) == 1 && seq == 2);

    if (s_fail == 0) {
        printf("ball_authority_selftest: ALL PASS\n");
        return 0;
    }
    printf("ball_authority_selftest: %d FAILURES\n", s_fail);
    return 1;
}
