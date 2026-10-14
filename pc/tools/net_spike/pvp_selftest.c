/* pvp_selftest.c - native model test of pc_pvp_authority.c (player-vs-player tool hits, reactions only).
 * Links the REAL pc_pvp_authority.c (no engine dependencies) and checks the wire sizes, message validation, the attacker geometry
 * (axe triangle / shovel point / net segment), the host validation (every reject reason and the accept path), the victim rules
 * (allowlist, balloon guard, TTL, local interval, duplicate seq) and the attacker contact / rate logic.
 * Build + run (git-bash, PATH appended with /c/msys64/ucrt64/bin), from pc/:
 *   gcc -std=gnu11 -Wall -Iinclude tools/net_spike/pvp_selftest.c -lm -o build64_w3/pvp_selftest.exe && build64_w3/pvp_selftest.exe */
#include "../../src/pc_pvp_authority.c"

#include <stdio.h>

static int s_fail = 0, s_n = 0;
#define CHECK(c) do { s_n++; if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); s_fail++; } } while (0)

static PCPvpHitReqMsg req(int target, int tool, uint16_t seq, float x, float z, int16_t facing) {
    PCPvpHitReqMsg m;
    memset(&m, 0, sizeof(m));
    m.msg_type = 91;
    m.target = (uint8_t)target;
    m.tool = (uint8_t)tool;
    m.seq = seq;
    m.facing = facing;
    m.pos_x = x;
    m.pos_z = z;
    return m;
}

static PCPvpPlayerView view(float x, float z) {
    PCPvpPlayerView v;
    memset(&v, 0, sizeof(v));
    v.valid = 1;
    v.in_town = 1;
    v.has_pos = 1;
    v.x = x;
    v.y = 0.0f;
    v.z = z;
    return v;
}

int main(void) {
    static PCPvpHost h;
    PCPvpHitReqMsg m;
    PCPvpHitMsg out;
    PCPvpPlayerView av, tv;
    PCPvpVictim vic;
    PCPvpAttacker atk;
    uint32_t t = 100000u;
    uint16_t seq = 0;
    int dropped = 0, i;
    float nanv = 0.0f;
    nanv = nanv / nanv;

    CHECK(sizeof(PCPvpHitReqMsg) == 16 && sizeof(PCPvpHitMsg) == 12);

    /* angles: engine convention x += sin(a), z += cos(a) */
    CHECK(pc_pvp_angle_to(0, 0, 0, 10) == 0);
    CHECK(pc_pvp_angle_to(0, 0, 10, 0) == 16384);
    CHECK(pc_pvp_angle_to(0, 0, -10, 0) == -16384);
    CHECK(pc_pvp_angle_diff_deg(0, 16384) == 90 && pc_pvp_angle_diff_deg(-32768, 32767) == 0 && pc_pvp_angle_diff_deg(-30000, 30000) <= 30);

    /* message validation */
    m = req(3, PC_PVP_TOOL_AXE, 1, 1000, 1000, 0);
    CHECK(pc_pvp_req_msg_valid(&m));
    m.tool = 4; CHECK(!pc_pvp_req_msg_valid(&m)); m.tool = 0; CHECK(!pc_pvp_req_msg_valid(&m)); m.tool = 1;
    m.rsv = 1; CHECK(!pc_pvp_req_msg_valid(&m)); m.rsv = 0;
    m.target = 0xFF; CHECK(!pc_pvp_req_msg_valid(&m)); m.target = 3;
    m.pos_x = nanv; CHECK(!pc_pvp_req_msg_valid(&m)); m.pos_x = -5; CHECK(!pc_pvp_req_msg_valid(&m)); m.pos_x = 40000; CHECK(!pc_pvp_req_msg_valid(&m));
    memset(&out, 0, sizeof(out));
    out.msg_type = 92; out.attacker = 8; out.tool = 1; out.reaction = 1; out.knock_speed = 3.5f;
    CHECK(pc_pvp_hit_msg_valid(&out));
    out.knock_speed = 9.0f; CHECK(!pc_pvp_hit_msg_valid(&out)); out.knock_speed = nanv; CHECK(!pc_pvp_hit_msg_valid(&out)); out.knock_speed = 1;
    out.reaction = 3; CHECK(!pc_pvp_hit_msg_valid(&out)); out.reaction = 2; CHECK(pc_pvp_hit_msg_valid(&out));

    /* tool classes */
    CHECK(pc_pvp_tool_of_index(37) == PC_PVP_TOOL_AXE && pc_pvp_tool_of_index(38) == PC_PVP_TOOL_AXE && pc_pvp_tool_of_index(39) == PC_PVP_TOOL_AXE);
    CHECK(pc_pvp_tool_of_index(40) == 0); /* BROKEN_AXE never hits */
    CHECK(pc_pvp_tool_of_index(61) == PC_PVP_TOOL_SHOVEL && pc_pvp_tool_of_index(60) == PC_PVP_TOOL_SHOVEL);
    CHECK(pc_pvp_tool_of_index(44) == PC_PVP_TOOL_NET && pc_pvp_tool_of_index(46) == PC_PVP_TOOL_NET);
    CHECK(pc_pvp_tool_of_index(7) == 0 && pc_pvp_tool_of_index(50) == 0 /* rod */ && pc_pvp_tool_of_index(103) == 0 /* umbrella */ && pc_pvp_tool_of_index(109) == 0 /* fan */);

    /* axe geometry: player at (1000, 0, 1000) facing +z */
    CHECK(pc_pvp_axe_hits(1000, 0, 1000, 0, 1000, 0, 1030));   /* inside the triangle */
    CHECK(pc_pvp_axe_hits(1000, 0, 1000, 0, 1000, 0, 1045));   /* pipe radius reaches past the tips */
    CHECK(!pc_pvp_axe_hits(1000, 0, 1000, 0, 1000, 0, 1060));  /* too far */
    CHECK(!pc_pvp_axe_hits(1000, 0, 1000, 0, 1000, 0, 970));   /* behind */
    CHECK(pc_pvp_axe_hits(1000, 0, 1000, 0, 1015, 0, 1025));   /* off-axis but within the pipe radius */
    CHECK(!pc_pvp_axe_hits(1000, 0, 1000, 0, 1040, 0, 1020));  /* to the side */
    CHECK(pc_pvp_axe_hits(1000, 0, 1000, 16384, 1030, 0, 1000)); /* facing +x */
    CHECK(!pc_pvp_axe_hits(1000, 0, 1000, 0, 1000, 120, 1030)); /* a cliff above */
    /* shovel point (40 ahead, r 39) */
    CHECK(pc_pvp_shovel_hits(1000, 0, 1000, 0, 1000, 0, 1040) && pc_pvp_shovel_hits(1000, 0, 1000, 0, 1000, 0, 1010));
    CHECK(!pc_pvp_shovel_hits(1000, 0, 1000, 0, 1000, 0, 1085) && !pc_pvp_shovel_hits(1000, 0, 1000, 0, 1000, 0, 990));
    /* net segment */
    {
        float a[3] = { 1000, 20, 1010 }, b[3] = { 1000, 20, 1050 };
        CHECK(pc_pvp_net_hits(a, b, 1000, 0, 1040));
        CHECK(pc_pvp_net_hits(a, b, 1020, 0, 1030));
        CHECK(!pc_pvp_net_hits(a, b, 1040, 0, 1030));
        CHECK(!pc_pvp_net_hits(a, b, 1000, 200, 1040));
    }

    /* ---- host validation ---- */
    pc_pvp_host_reset(&h);
    av = view(1000, 1000);
    tv = view(1000, 1030);
    pc_pvp_host_note_scene(&h, 3, 1, t);
    pc_pvp_host_note_scene(&h, 8, 1, t);
    m = req(8, PC_PVP_TOOL_AXE, 1, 1000, 1000, 0);
    /* protected right after the arrival (needs a recent swing first to reach the protection test) */
    pc_pvp_host_note_motion(&h, 3, 38, t);
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t + 100, 92, &out) == PC_PVP_REJ_PROTECTED);
    t += PC_PVP_PROTECT_MS + 10;
    /* no swing in the window */
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_NO_SWING);
    pc_pvp_host_note_motion(&h, 3, 61, t); /* a shovel swing does not cover an axe request */
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_NO_SWING);
    pc_pvp_host_note_motion(&h, 3, 37, t);
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t + PC_PVP_SWING_WINDOW_MS + 1, 92, &out) == PC_PVP_REJ_NO_SWING);
    /* malformed / bad attacker / bad target / scene / no pos */
    m.tool = 9; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_MALFORMED); m.tool = 1;
    av.valid = 0; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_BAD_ATTACKER); av.valid = 1;
    tv.valid = 0; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_BAD_TARGET); tv.valid = 1;
    m.target = 3; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_BAD_TARGET); m.target = 8;
    tv.in_town = 0; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_SCENE); tv.in_town = 1;
    av.in_town = 0; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_SCENE); av.in_town = 1;
    tv.has_pos = 0; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_NO_POS); tv.has_pos = 1;
    /* claimed position far from the MOVE position */
    m.pos_x = 1100; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_CLAIM_FAR); m.pos_x = 1000;
    /* range */
    tv.z = 1070; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_RANGE); tv.z = 1030;
    tv.y = 80; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_RANGE); tv.y = 0;
    /* cone: facing away (-z) / sideways (+x) */
    m.facing = -32768; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_CONE);
    m.facing = 16384; CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_CONE);
    m.facing = 0;
    /* the target arrived recently */
    pc_pvp_host_note_scene(&h, 8, 0, t);
    pc_pvp_host_note_scene(&h, 8, 1, t);
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_REJ_PROTECTED);
    t += PC_PVP_PROTECT_MS;
    pc_pvp_host_note_motion(&h, 3, 37, t);
    /* accepted */
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_OK);
    CHECK(out.msg_type == 92 && out.attacker == 3 && out.tool == PC_PVP_TOOL_AXE && out.reaction == PC_PVP_REACT_TUMBLE && out.knock_speed == PC_PVP_KNOCK_AXE);
    CHECK(pc_pvp_angle_diff_deg(out.knock_angle, 0) <= 1); /* away from the attacker: +z */
    CHECK(pc_pvp_hit_msg_valid(&out));
    /* replay of the same seq / rate / immunity */
    pc_pvp_host_note_motion(&h, 3, 37, t + 1000);
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t + 1000, 92, &out) == PC_PVP_REJ_OLD_SEQ);
    m.seq = 2;
    pc_pvp_host_note_motion(&h, 3, 37, t + 300);
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t + 300, 92, &out) == PC_PVP_REJ_RATE);
    pc_pvp_host_note_motion(&h, 3, 37, t + 900);
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t + 900, 92, &out) == PC_PVP_REJ_IMMUNE);
    /* a second attacker is also blocked by the victim's immunity */
    pc_pvp_host_note_scene(&h, 4, 1, t - PC_PVP_PROTECT_MS - 1);
    pc_pvp_host_note_motion(&h, 4, 44, t + 900);
    {
        PCPvpHitReqMsg m4 = req(8, PC_PVP_TOOL_NET, 1, 1000, 1000, 0);
        CHECK(pc_pvp_host_validate(&h, 4, &m4, &av, &tv, t + 900, 92, &out) == PC_PVP_REJ_IMMUNE);
        CHECK(pc_pvp_host_validate(&h, 4, &m4, &av, &tv, t + PC_PVP_IMMUNE_MS + 10, 92, &out) == PC_PVP_REJ_NO_SWING);
        pc_pvp_host_note_motion(&h, 4, 46, t + PC_PVP_IMMUNE_MS + 10);
        CHECK(pc_pvp_host_validate(&h, 4, &m4, &av, &tv, t + PC_PVP_IMMUNE_MS + 10, 92, &out) == PC_PVP_OK);
        CHECK(out.reaction == PC_PVP_REACT_SHOCK && out.knock_speed == 0.0f && out.tool == PC_PVP_TOOL_NET && out.attacker == 4);
    }
    /* shovel -> shock */
    t += 10000;
    pc_pvp_host_note_motion(&h, 3, 61, t);
    m = req(8, PC_PVP_TOOL_SHOVEL, 5, 1000, 1000, 0);
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_OK && out.reaction == PC_PVP_REACT_SHOCK);
    /* seq wraps */
    t += 10000;
    pc_pvp_host_note_motion(&h, 3, 37, t);
    m = req(8, PC_PVP_TOOL_AXE, 0, 1000, 1000, 0);
    h.last_seq[3] = 0xFFFF;
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t, 92, &out) == PC_PVP_OK);
    /* forget clears the records (a reused slot is protected again: first sighting) */
    pc_pvp_host_forget(&h, 3);
    CHECK(h.has_hit[3] == 0 && h.has_swing[3] == 0 && h.town_known[3] == 0);
    pc_pvp_host_note_scene(&h, 3, 1, t + 5000);
    pc_pvp_host_note_motion(&h, 3, 37, t + 5000);
    m.seq = 7;
    CHECK(pc_pvp_host_validate(&h, 3, &m, &av, &tv, t + 5000, 92, &out) == PC_PVP_REJ_PROTECTED);
    CHECK(strcmp(pc_pvp_reason_name(PC_PVP_REJ_IMMUNE), "IMMUNE") == 0 && strcmp(pc_pvp_reason_name(99), "?") == 0);

    /* ---- victim ---- */
    for (i = 0; i < 121; i++) {
        const int ok = pc_pvp_victim_state_ok(i);
        const int want = (i == 7 || i == 8 || i == 9 || i == 10 || i == 13 || i == 42 || i == 43);
        CHECK(ok == want);
    }
    CHECK(pc_pvp_victim_reaction(PC_PVP_REACT_TUMBLE, 1) == PC_PVP_REACT_SHOCK && pc_pvp_victim_reaction(PC_PVP_REACT_TUMBLE, 0) == PC_PVP_REACT_TUMBLE);
    CHECK(pc_pvp_victim_reaction(PC_PVP_REACT_SHOCK, 1) == PC_PVP_REACT_SHOCK);
    pc_pvp_victim_reset(&vic);
    memset(&out, 0, sizeof(out));
    out.msg_type = 92; out.attacker = 8; out.tool = 1; out.reaction = 1; out.knock_speed = 3.5f; out.seq = 10;
    CHECK(pc_pvp_victim_offer(&vic, &out, 5000));
    CHECK(!pc_pvp_victim_offer(&vic, &out, 5001)); /* duplicate seq */
    {
        PCPvpHitMsg got;
        CHECK(!pc_pvp_victim_take(&vic, 5100, 66 /* TALK */, 0, &got, &dropped) && !dropped && vic.has_pending);
        CHECK(!pc_pvp_victim_take(&vic, 5100, 7, 1 /* menu / demo */, &got, &dropped) && vic.has_pending);
        CHECK(pc_pvp_victim_take(&vic, 5200, 8, 0, &got, &dropped) && got.seq == 10 && !vic.has_pending);
        out.seq = 11;
        CHECK(pc_pvp_victim_offer(&vic, &out, 5300));
        CHECK(!pc_pvp_victim_take(&vic, 5400, 7, 0, &got, &dropped) && dropped); /* within the local minimum interval */
        out.seq = 12;
        CHECK(pc_pvp_victim_offer(&vic, &out, 9000));
        CHECK(!pc_pvp_victim_take(&vic, 9000 + PC_PVP_VICTIM_TTL_MS + 1, 7, 0, &got, &dropped) && dropped); /* stale (the player was busy) */
        out.seq = 13;
        out.reaction = 7;
        CHECK(!pc_pvp_victim_offer(&vic, &out, 12000)); /* malformed */
    }

    /* ---- attacker ---- */
    pc_pvp_attacker_reset(&atk);
    pc_pvp_attacker_on_entry(&atk, 38);
    CHECK(!pc_pvp_attacker_contact(&atk, 38, 10.0f));
    CHECK(pc_pvp_attacker_contact(&atk, 38, 15.0f));
    CHECK(!pc_pvp_attacker_contact(&atk, 38, 16.0f)); /* once per entry */
    pc_pvp_attacker_on_entry(&atk, 58); /* DIG_SCOOP: a hole was dug, nobody stood there */
    CHECK(!pc_pvp_attacker_contact(&atk, 58, 20.0f));
    pc_pvp_attacker_on_entry(&atk, 61);
    CHECK(!pc_pvp_attacker_contact(&atk, 7, 20.0f)); /* left the state before the contact */
    CHECK(pc_pvp_attacker_contact(&atk, 61, 13.0f));
    CHECK(pc_pvp_attacker_next(&atk, 1000, &seq) && seq == 1);
    CHECK(!pc_pvp_attacker_next(&atk, 1300, &seq));
    CHECK(pc_pvp_attacker_next(&atk, 1700, &seq) && seq == 2);

    printf("pvp_selftest: %d checks, %d failed\n", s_n, s_fail);
    return s_fail ? 1 : 0;
}
