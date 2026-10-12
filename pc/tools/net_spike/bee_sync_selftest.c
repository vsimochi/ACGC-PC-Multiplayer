/* bee_sync_selftest.c - native model test of pc_bee_sync.c (agent 11, bees).
 * Links the REAL pc_bee_sync.c against stubs for the engine / net calls it makes and checks:
 *  1. one proxy swarm per (owner, shake seq): a duplicate SPAWN never makes a second actor; a newer seq replaces the old one;
 *  2. sting / caught / disappear for a wrong seq or unknown owner are ignored; repeated terminal events are idempotent;
 *     an out-of-range origin / unknown event never touches state; the owner leaving the scene / disconnecting ends the proxy;
 *  3. the owner side reports each event at most once per swarm (no reward/penalty path: only messages) and never for a proxy.
 * Build (from pc/build64, PATH appended with /c/msys64/ucrt64/bin):
 *   gcc -DBUGFIXES -DF3DEX_GBI_2 -DKEYBOARD_TYPING -DNDEBUG -DPC_ENHANCEMENTS -DPC_LOW_ADDRESS_64 -DTARGET_PC -DVERSION=0 -D_LANGUAGE_C
 *       @CMakeFiles/ac_pc.dir/includes_C.rsp -std=gnu11 -Dnullptr=NULL ../tools/net_spike/bee_sync_selftest.c -o bee_sync_selftest.exe */
#include "../../src/pc_bee_sync.c"

#include <stdio.h>

/* ---- stubs ---- */
GAME* gamePT = NULL;
static GAME_PLAY s_play;
static ACTOR s_actors[16];
static int s_made = 0, s_deleted = 0, s_ct_saw_proxy = 0;
static PCNetGameRole s_role_stub = PC_NETGAME_ROLE_CLIENT;
static int s_owner_scene_ok[PC_NETGAME_WIRE_ID_SPACE];
static int s_sent[8];

PCNetGameRole pc_net_game_role(void) { return s_role_stub; }
ACTOR* Actor_info_make_actor(Actor_info* ai, GAME* g, s16 profile, f32 x, f32 y, f32 z, s16 rx, s16 ry, s16 rz, s8 bx, s8 bz, s16 mv,
                             mActor_name_t npc, s16 arg, s8 ex, int dbank) {
    ACTOR* a;
    (void)ai; (void)g; (void)profile; (void)rx; (void)ry; (void)rz; (void)bx; (void)bz; (void)mv; (void)npc; (void)arg; (void)ex; (void)dbank;
    if (s_made >= 16) return NULL;
    a = &s_actors[s_made++];
    a->world.position.x = x; a->world.position.y = y; a->world.position.z = z;
    s_ct_saw_proxy += pc_bee_sync_is_proxy(a); /* the synchronous ct must already see it as a proxy */
    return a;
}
void Actor_delete(ACTOR* a) { s_deleted++; pc_bee_sync_actor_gone(a); }
int pc_remote_player_get_scene(PCNetPlayerId id, PCNetPlayerScene* out) {
    if (id < 0 || id >= PC_NETGAME_WIRE_ID_SPACE || !s_owner_scene_ok[id]) return 0;
    memset(out, 0, sizeof(*out)); out->valid = 1; out->scene_id = 5; return 1;
}
int pc_remote_player_get_last_position(PCNetPlayerId id, float* x, float* y, float* z) { (void)id; *x = 100; *y = 0; *z = 200; return 1; }
void pc_net_game_bee_swarm_send_local(int event, uint16_t seq, float x, float y, float z) { (void)seq; (void)x; (void)y; (void)z; if (event > 0 && event < 8) s_sent[event]++; }

static int s_fail = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); s_fail++; } } while (0)

int main(void) {
    float x, y, z;
    ACTOR* p1;
    ACTOR local_bee, other_bee;
    gamePT = (GAME*)&s_play;
    s_owner_scene_ok[2] = 1;

    /* 1. one swarm per shake event */
    pc_bee_sync_present(2, PC_BEE_EV_SPAWN, 10, 5, 100, 0, 200);
    CHECK(s_made == 1 && pc_bee_sync_proxy_count() == 1 && s_ct_saw_proxy == 1);
    pc_bee_sync_present(2, PC_BEE_EV_SPAWN, 10, 5, 100, 0, 200); /* duplicate relay */
    CHECK(s_made == 1 && pc_bee_sync_proxy_count() == 1);
    p1 = &s_actors[0];
    CHECK(pc_bee_sync_is_proxy(p1) && pc_bee_sync_proxy_target(p1, &x, &y, &z) == 1 && x == 100 && z == 200);
    pc_bee_sync_present(2, PC_BEE_EV_SPAWN, 11, 5, 100, 0, 200); /* a newer shake replaces the old swarm */
    CHECK(s_made == 2 && s_deleted == 1 && pc_bee_sync_proxy_count() == 1 && !pc_bee_sync_is_proxy(p1));

    /* 2. sting / terminal idempotency, wrong seq, bad origin, bad event */
    pc_bee_sync_present(2, PC_BEE_EV_STING, 10, 5, 0, 0, 0); /* stale seq */
    CHECK(!pc_bee_sync_proxy_stung(&s_actors[1]));
    pc_bee_sync_present(2, PC_BEE_EV_STING, 11, 5, 0, 0, 0);
    pc_bee_sync_present(2, PC_BEE_EV_STING, 11, 5, 0, 0, 0);
    CHECK(pc_bee_sync_proxy_stung(&s_actors[1]));
    pc_bee_sync_present(-1, PC_BEE_EV_SPAWN, 1, 5, 0, 0, 0);
    pc_bee_sync_present(PC_NETGAME_WIRE_ID_SPACE, PC_BEE_EV_SPAWN, 1, 5, 0, 0, 0);
    pc_bee_sync_present(3, 99, 1, 5, 0, 0, 0);
    pc_bee_sync_present(3, PC_BEE_EV_DISAPPEAR, 1, 5, 0, 0, 0); /* no swarm known for owner 3 */
    CHECK(s_made == 2 && pc_bee_sync_proxy_count() == 1);
    pc_bee_sync_present(2, PC_BEE_EV_DISAPPEAR, 11, 5, 0, 0, 0);
    pc_bee_sync_present(2, PC_BEE_EV_CAUGHT, 11, 5, 0, 0, 0);
    CHECK(pc_bee_sync_proxy_target(&s_actors[1], &x, &y, &z) == 0); /* fades on its next move */
    pc_bee_sync_actor_gone(&s_actors[1]);                            /* its own Actor_delete -> dt */
    pc_bee_sync_actor_gone(&s_actors[1]);
    CHECK(pc_bee_sync_proxy_count() == 0);
    pc_bee_sync_present(2, PC_BEE_EV_SPAWN, 11, 5, 100, 0, 200); /* a late duplicate of an ended shake never respawns */
    CHECK(s_made == 2 && pc_bee_sync_proxy_count() == 0);
    pc_bee_sync_present(2, PC_BEE_EV_SPAWN, 12, 5, 100, 0, 200);
    CHECK(s_made == 3 && pc_bee_sync_proxy_target(&s_actors[2], &x, &y, &z) == 1);
    s_owner_scene_ok[2] = 0; /* owner disconnected / changed scene */
    CHECK(pc_bee_sync_proxy_target(&s_actors[2], &x, &y, &z) == 0);
    pc_bee_sync_actor_gone(&s_actors[2]);

    /* 3. owner side: each event once per swarm, never for a proxy / foreign actor, inert in single player */
    memset(&local_bee, 0, sizeof(local_bee));
    memset(&other_bee, 0, sizeof(other_bee));
    s_role_stub = PC_NETGAME_ROLE_NONE;
    pc_bee_sync_local_spawn(&local_bee, 1, 2, 3);
    pc_bee_sync_local_event(&local_bee, PC_BEE_EV_STING, 0, 0, 0);
    CHECK(s_sent[PC_BEE_EV_SPAWN] == 0 && s_sent[PC_BEE_EV_STING] == 0);
    s_role_stub = PC_NETGAME_ROLE_CLIENT;
    pc_bee_sync_local_spawn(&local_bee, 1, 2, 3);
    pc_bee_sync_local_event(&local_bee, PC_BEE_EV_STING, 0, 0, 0);
    pc_bee_sync_local_event(&local_bee, PC_BEE_EV_STING, 0, 0, 0); /* attack re-requests the sting every frame */
    pc_bee_sync_local_event(&other_bee, PC_BEE_EV_STING, 0, 0, 0);
    pc_bee_sync_local_event(&local_bee, PC_BEE_EV_DISAPPEAR, 0, 0, 0);
    pc_bee_sync_actor_gone(&local_bee); /* dt after the disappear: no second DISAPPEAR */
    CHECK(s_sent[PC_BEE_EV_SPAWN] == 1 && s_sent[PC_BEE_EV_STING] == 1 && s_sent[PC_BEE_EV_DISAPPEAR] == 1);
    pc_bee_sync_local_spawn(&local_bee, 1, 2, 3);
    pc_bee_sync_actor_gone(&local_bee); /* destroyed by a scene change: DISAPPEAR sent once */
    pc_bee_sync_local_event(&local_bee, PC_BEE_EV_DISAPPEAR, 0, 0, 0);
    CHECK(s_sent[PC_BEE_EV_SPAWN] == 2 && s_sent[PC_BEE_EV_DISAPPEAR] == 2);

    printf(s_fail ? "bee_sync_selftest: %d FAILED\n" : "bee_sync_selftest: ALL PASSED%.0d\n", s_fail);
    return s_fail ? 1 : 0;
}
