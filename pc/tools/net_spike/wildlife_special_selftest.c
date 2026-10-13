/* wildlife_special_selftest.c - native model test of the wildlife special cases in pc_wildlife_authority.c (worker A: spirits, ants, latent bugs).
 * Links the REAL pc_wildlife_authority.c against stubs for the engine / net calls it makes and checks:
 *  ANTS   A1 an ant record is shown with a real mAc_PROFILE_ANT actor at the record position / acre when its tile holds candy or a spoiled turnip,
 *            and NOT shown (entry handled, no actor) when it does not; A2 the actor is "alive" only while it is in the BG actor list; A3 a despawn
 *            plays the vanilla fade (aANT_pc_disappear) -- not while this process's player has it in the net; A4 the net catch moves the entity to the
 *            9th-slot insect, then the ordinary label lookup (pcwld_bug_entity_id_for_local_actor, species ANT) finds it; A5 the host releases the
 *            record (abandoned) once its tile no longer holds candy, a record on candy stays; A6 a duplicate SPAWN of a live ant is a no-op, of a
 *            vanished ant re-creates it; A7 the ant is listed by the `bugs` helper; its record produces no WILDLIFE_STATE entry.
 *  SPIRIT S1 a caught spirit keeps its acre (no re-register) even after its record is gone; S2 an uncaught spirit whose record still lives keeps its
 *            acre now; S3 its record released (unattended) with no host actor re-registers it exactly once; S4 released while its host actor lives:
 *            not now (the actor's own destruct does it); S5 a client never keeps the acre (vanilla).
 *  LATENT L1 the host queue hands remote tool uses out FIFO, one per call, validates the action range, drops nothing at normal rates and only the
 *            oldest when full; L2 a client cannot queue; L3 the host refuses a catch only for a latent bug still hidden AND uncatchable (bit_1);
 *            L4 a client follows the host's action out of HIDE via the species hook (mole cricket / bagworm / spider / pill bug), never other species.
 *  ROACH  R1 the cockroach spawn pose: 4 on every tree kind of aSOI_tree_check, 6 on a spoiled turnip, 0 on a flower / grass.
 * Build (from pc/build64_w3, PATH appended with /c/msys64/ucrt64/bin):
 *   gcc -DBUGFIXES -DF3DEX_GBI_2 -DKEYBOARD_TYPING -DNDEBUG -DPC_ENHANCEMENTS -DPC_LOW_ADDRESS_64 -DTARGET_PC -DVERSION=0 -D_LANGUAGE_C -DPC_TEST_HOOKS
 *       @CMakeFiles/ac_pc.dir/includes_C.rsp -std=gnu11 -Dnullptr=NULL ../tools/net_spike/wildlife_special_selftest.c -o wildlife_special_selftest.exe */
#include "../../src/pc_wildlife_authority.c"

#include <stdio.h>
#include <stdarg.h>
#include <windows.h>

/* ---- stubs: engine ---- */
GAME* gamePT = NULL;
common_data_t common_data;
static GAME_PLAY s_play;
static GRAPH s_graph;
static aINS_Clip_c s_ins_clip;
static aGYO_Clip_c s_gyo_clip;
static ACTOR* s_ants; /* in LOW memory (VirtualAlloc below 4 GB): the game compares actors with the u32 net catch label (PC_LOW_ADDRESS_64) */
static int s_ants_made = 0, s_ant_disappear = 0, s_reregister = 0, s_last_rereg_bx = -1;
static mActor_name_t s_tile = EMPTY_NO; /* the one field tile every mFI_GetUnitFG() call reads */
static u32 s_label = 0;
static aINS_INSECT_ACTOR* s_insects;
static int s_role = 1; /* 1 host, 2 client */
static int s_reveal_calls[3];

int pcfa_scene_is_town(void) { return 1; }
PCNetGameRole pc_net_game_role(void) { return s_role == 1 ? PC_NETGAME_ROLE_HOST : PC_NETGAME_ROLE_CLIENT; }
int pc_net_game_authoritative_wildlife_enabled(void) { return 1; }
void pc_net_game_notify_wildlife_spawn(uint32_t id, int kind, int species, int bx, int bz, float x, float y, float z) {
    (void)id; (void)kind; (void)species; (void)bx; (void)bz; (void)x; (void)y; (void)z;
}
int pc_net_game_wildlife_remote_players(PcWldRemotePlayer* out, int max) { (void)out; (void)max; return 0; }
uint32_t pc_net_game_now_ms(void) { static uint32_t t = 1000; return t += 16; }
int pc_net_game_host_out_of_town(void) { return 0; }
const char* pc_test_hook_getenv(const char* n) { (void)n; return NULL; }
int aSOI_insect_set(SET_MANAGER* sm, GAME_PLAY* p) { (void)sm; (void)p; return 0; }
int aSOG_gyoei_set(SET_MANAGER* sm, GAME_PLAY* p) { (void)sm; (void)p; return 0; }
void aGYO_pc_stamp_entity_id(ACTOR* a, u32 id) { (void)a; (void)id; }
int aGYO_pc_handle_wildlife_despawn(u32 id) { (void)id; return 0; }
void aGYO_pc_clear_all_entity_stamps(void) {}
int aGYO_pc_entity_alive(u32 id) { (void)id; return 0; }
int aGYO_pc_stamp_count(u32 id) { (void)id; return 0; }
int aGYO_pc_live_fish_count(void) { return 0; }
int aGYO_pc_entity_position(u32 id, float* p, int* bx, int* bz) { (void)id; (void)p; (void)bx; (void)bz; return 0; }
int aGYO_pc_entity_state(u32 id, float* xyz, s16* ang, int* act) { (void)id; (void)xyz; (void)ang; (void)act; return 0; }
int aGYO_pc_find_entity(u32 id, ACTOR** out) { (void)id; (void)out; return 0; }
u32 aGYO_pc_get_entity_id_stamp(ACTOR* a) { (void)a; return 0; }
int aGTT_pc_action(const ACTOR* a) { (void)a; return 0; }
int aGTT_pc_apply_bobber_event(ACTOR* f, ACTOR* u, int ev, int t, f32 x, f32 y, f32 z, s16 ang) {
    (void)f; (void)u; (void)ev; (void)t; (void)x; (void)y; (void)z; (void)ang; return 0;
}
s16 search_position_angleY(const xyz_t* a, const xyz_t* b) { (void)a; (void)b; return 0; }
u32 mCoBG_Wpos2Attribute(xyz_t p, s8* x) { (void)p; (void)x; return 0; }
int mFI_Wpos2BlockNum(int* bx, int* bz, xyz_t p) { *bx = (int)(p.x / 640.0f); *bz = (int)(p.z / 640.0f); return 1; }
mActor_name_t* mFI_GetUnitFG(xyz_t p) { (void)p; return &s_tile; }
ACTOR* Actor_info_make_actor(Actor_info* ai, GAME* g, s16 profile, f32 x, f32 y, f32 z, s16 rx, s16 ry, s16 rz, s8 bx, s8 bz, s16 mv,
                             mActor_name_t npc, s16 arg, s8 ex, int dbank) {
    ACTOR* a;
    (void)ai; (void)g; (void)rx; (void)ry; (void)rz; (void)mv; (void)npc; (void)arg; (void)ex; (void)dbank;
    if (s_ants_made >= 8) return NULL;
    a = &s_ants[s_ants_made++];
    memset(a, 0, sizeof(*a));
    a->id = profile;
    a->world.position.x = x; a->world.position.y = y; a->world.position.z = z;
    a->block_x = bx; a->block_z = bz;
    /* into the BG list (head insert) */
    a->next_actor = s_play.actor_info.list[ACTOR_PART_BG].actor;
    s_play.actor_info.list[ACTOR_PART_BG].actor = a;
    s_play.actor_info.list[ACTOR_PART_BG].num_actors++;
    return a;
}
static void bg_remove(ACTOR* gone) {
    Actor_list* l = &s_play.actor_info.list[ACTOR_PART_BG];
    ACTOR** pp = &l->actor;
    while (*pp != NULL) {
        if (*pp == gone) { *pp = gone->next_actor; l->num_actors--; return; }
        pp = &(*pp)->next_actor;
    }
}
ACTOR* Actor_info_name_search(Actor_info* ai, s16 name, int part) { (void)ai; (void)name; (void)part; return NULL; }
PLAYER_ACTOR* get_player_actor_withoutCheck(GAME_PLAY* p) { (void)p; return NULL; }
u32 mPlib_Get_item_net_catch_label(void) { return s_label; }
void aANT_pc_disappear(ACTOR* ant) { (void)ant; s_ant_disappear++; }
void aINS_pc_spirit_reregister(int bx, int bz) { (void)bz; s_reregister++; s_last_rereg_bx = bx; }
static int hide_of(int type) { return type == aINS_INSECT_TYPE_PILL_BUG ? 3 : 2; }
static int follow(ACTOR* a, int idx, int host_action) {
    aINS_INSECT_ACTOR* in = (aINS_INSECT_ACTOR*)a;
    s_reveal_calls[idx]++;
    if (in->action == hide_of(in->type) && host_action != hide_of(in->type)) { in->action = hide_of(in->type) + 1; return 1; }
    return 0;
}
int aIKR_pc_follow_reveal(ACTOR* a, GAME* g, int h) { (void)g; return follow(a, 0, h); }
int aIMN_pc_follow_reveal(ACTOR* a, GAME* g, int h) { (void)g; return follow(a, 1, h); }
int aIDG_pc_follow_reveal(ACTOR* a, GAME* g, int h) { (void)g; return follow(a, 2, h); }
int aIKR_pc_hidden(const ACTOR* a) { return ((const aINS_INSECT_ACTOR*)a)->action == 2; }
int aIMN_pc_hidden(const ACTOR* a) { return ((const aINS_INSECT_ACTOR*)a)->action == 2; }
int aIDG_pc_hidden(const ACTOR* a) { return ((const aINS_INSECT_ACTOR*)a)->action == 3; }
uint32_t g_pc_log_mask = 0;
void pc_log_write(uint32_t cat, const char* fmt, ...) { (void)cat; (void)fmt; }

/* a make_insect_proc that hands out pool slots 0..7 (aINS_MAKE_NEW) or 8 (aINS_MAKE_EXIST) */
static ACTOR* stub_make_insect(aINS_Init_c* init, int make_type) {
    int i;
    if (make_type != aINS_MAKE_NEW) {
        if (s_insects[8].exist_flag) return NULL;
        i = 8;
    } else {
        for (i = 0; i < 8 && s_insects[i].exist_flag; i++) {}
        if (i == 8) return NULL;
    }
    memset(&s_insects[i], 0, sizeof(s_insects[i]));
    s_insects[i].exist_flag = TRUE;
    s_insects[i].type = init->insect_type;
    s_insects[i].tools_actor.actor_class.world.position = init->position;
    s_insects[i].tools_actor.actor_class.actor_specific = init->extra_data;
    return (ACTOR*)&s_insects[i];
}

static int s_fail = 0, s_pass = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); s_fail++; } else { s_pass++; } } while (0)

static int always_attended(const PcWildlifeRecord* r) { (void)r; return 1; }
static int never_attended(const PcWildlifeRecord* r) { (void)r; return 0; }

int main(void) {
    uint32_t ant_id, ant2_id, sp_id, sp2_id, kera_id, mino_id, bf_id, ids[8];
    PcWildlifeRecord rec;
    int i, n, a, ux, uz;
    float fx, fz;
    uint32_t e;
    int sp;
    PcWldStateEntry st[8];

    {
        uint8_t* low = NULL;
        uintptr_t base;
        for (base = 0x10000000u; low == NULL && base < 0x80000000u; base += 0x01000000u) {
            low = (uint8_t*)VirtualAlloc((void*)base, 1 << 20, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
        }
        if (low == NULL) {
            printf("FAIL: no low memory for the actors\n");
            return 1;
        }
        memset(low, 0, 1 << 20);
        s_ants = (ACTOR*)low;
        s_insects = (aINS_INSECT_ACTOR*)(low + 8 * sizeof(ACTOR) + 64);
    }
    gamePT = (GAME*)&s_play;
    s_play.game.graph = &s_graph;
    s_graph.dt_num_60fps_frames = 60; /* one call = one second of the host's once-a-second ticks */
    common_data.clip.insect_clip = &s_ins_clip;
    common_data.clip.gyo_clip = &s_gyo_clip;
    s_ins_clip.make_insect_proc = stub_make_insect;
    pcwld_reset();

    /* ---------------- ANTS ---------------- */
    s_tile = ITM_FOOD_CANDY;
    ant_id = pcwld_test_host_inject_kind(PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_ANT, 1300.0f, 1940.0f);
    CHECK(ant_id != 0 && s_ants_made == 1); /* A1 shown */
    CHECK(s_ants[0].id == mAc_PROFILE_ANT && s_ants[0].world.position.x == 1300.0f && s_ants[0].block_x == 2 && s_ants[0].block_z == 3);
    CHECK(pcwld_presentation_has(ant_id));
    CHECK(pcwld_test_live_bug(0, &e, &sp, &fx, &fz) && e == ant_id && sp == aINS_INSECT_TYPE_ANT); /* A7 listed */
    CHECK(pcwld_host_collect_state(st, 8) == 0); /* A7 no state for an ant */
    CHECK(pcwld_presentation_create(ant_id, PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_ANT, 2, 3, 1300.0f, 0.0f, 1940.0f) == 1 && s_ants_made == 1); /* A6 dup no-op */
    /* A3 despawn: in this player's net -> nothing; otherwise the fade */
    s_label = (u32)(uintptr_t)&s_ants[0];
    CHECK(pcwld_bug_handle_wildlife_despawn(ant_id) == 1 && s_ant_disappear == 0);
    s_label = 0;
    CHECK(pcwld_bug_handle_wildlife_despawn(ant_id) == 1 && s_ant_disappear == 1);
    /* A2 / A6: the actor leaves the BG list (vanilla deleted it: another acre) -> not alive; a replayed SPAWN re-creates it */
    bg_remove(&s_ants[0]);
    CHECK(!pcwld_test_live_bug(0, &e, &sp, &fx, &fz));
    CHECK(pcwld_presentation_create(ant_id, PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_ANT, 2, 3, 1300.0f, 0.0f, 1940.0f) == 1 && s_ants_made == 2);
    /* A4 the net catch: entity follows the 9th-slot insect, the label lookup with species ANT finds it */
    {
        aINS_Init_c ii;
        ACTOR* nine;
        memset(&ii, 0, sizeof(ii));
        ii.insect_type = aINS_INSECT_TYPE_ANT;
        nine = stub_make_insect(&ii, aINS_MAKE_EXIST);
        CHECK(nine == (ACTOR*)&s_insects[8]);
        CHECK(pcwld_bug_entity_id_for_local_actor(nine, aINS_INSECT_TYPE_ANT) == 0);
        pcwld_ant_caught_moved(&s_ants[1], nine);
        CHECK(pcwld_bug_entity_id_for_local_actor(nine, aINS_INSECT_TYPE_ANT) == ant_id);
        CHECK(pcwld_bug_entity_id_for_local_actor(&s_ants[1], aINS_INSECT_TYPE_ANT) == 0);
        /* a despawn now flags the insect (the catch flow protects it while it is the label), not the fade */
        CHECK(pcwld_bug_handle_wildlife_despawn(ant_id) == 1 && s_insects[8].insect_flags.destruct == TRUE && s_ant_disappear == 1);
        s_insects[8].exist_flag = FALSE;
    }
    /* A1 not shown when the tile no longer holds candy / turnip; A5 the host releases a record whose tile changed, keeps one on a spoiled turnip */
    s_tile = EMPTY_NO;
    ant2_id = pcwld_test_host_inject_kind(PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_ANT, 700.0f, 700.0f);
    CHECK(ant2_id != 0 && s_ants_made == 2 && pcwld_presentation_has(ant2_id));
    pcwld_host_ensure_actors(always_attended);
    n = pcwld_host_collect_abandoned(ids, 8);
    CHECK(n == 2 && !pcwld_find_by_id(ant_id, &rec) && !pcwld_find_by_id(ant2_id, &rec));
    s_tile = ITM_KABU_SPOILED;
    ant2_id = pcwld_test_host_inject_kind(PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_ANT, 700.0f, 700.0f);
    pcwld_host_ensure_actors(always_attended);
    CHECK(pcwld_host_collect_abandoned(ids, 8) == 0 && pcwld_find_by_id(ant2_id, &rec) && s_ants_made == 3);
    pcwld_remove_by_id(ant2_id);

    /* ---------------- SPIRITS (G2) ---------------- */
    s_tile = EMPTY_NO;
    sp_id = pcwld_test_host_inject_kind(PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_SPIRIT, 650.0f, 1300.0f);
    CHECK(sp_id != 0 && pcwld_bug_local_actor_for_entity(sp_id, &sp) != NULL && sp == aINS_INSECT_TYPE_SPIRIT);
    {
        ACTOR* sa = (ACTOR*)pcwld_bug_local_actor_for_entity(sp_id, &sp);
        CHECK(pcwld_spirit_keep_acre(sa) == 1); /* S2 the record lives: not now */
        pcwld_remove_by_id(sp_id);              /* the catch removes the record ... */
        pcwld_note_caught(sp_id);               /* ... and marks it caught */
        CHECK(pcwld_spirit_keep_acre(sa) == 1 && s_reregister == 0); /* S1 */
        ((aINS_INSECT_ACTOR*)sa)->exist_flag = FALSE;
    }
    sp2_id = pcwld_test_host_inject_kind(PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_SPIRIT, 1950.0f, 1300.0f);
    {
        ACTOR* sa = (ACTOR*)pcwld_bug_local_actor_for_entity(sp2_id, &sp);
        /* S4 released while its host actor lives: not now (the despawn's destruct does it, keep_acre then answers 0) */
        for (i = 0; i < 13; i++) {
            n = pcwld_host_collect_unattended(never_attended, 60.0f, ids, 8);
        }
        CHECK(!pcwld_find_by_id(sp2_id, &rec) && s_reregister == 0);
        CHECK(pcwld_spirit_keep_acre(sa) == 0);
        ((aINS_INSECT_ACTOR*)sa)->exist_flag = FALSE;
    }
    sp2_id = pcwld_test_host_inject_kind(PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_SPIRIT, 1950.0f, 1300.0f);
    {
        ACTOR* sa = (ACTOR*)pcwld_bug_local_actor_for_entity(sp2_id, &sp);
        CHECK(pcwld_spirit_keep_acre(sa) == 1);  /* culled while the record lives: the acre stays */
        ((aINS_INSECT_ACTOR*)sa)->exist_flag = FALSE;
        for (i = 0; i < 13; i++) {
            n = pcwld_host_collect_unattended(never_attended, 60.0f, ids, 8);
        }
        CHECK(!pcwld_find_by_id(sp2_id, &rec) && s_reregister == 1 && s_last_rereg_bx == 3); /* S3 exactly once, from the record's acre */
    }
    s_role = 2;
    CHECK(pcwld_spirit_keep_acre(&s_insects[0]) == 0); /* S5 a client: vanilla */
    s_role = 1;

    /* ---------------- LATENT ---------------- */
    CHECK(pcwld_host_queue_pl_action(aINS_PL_ACT_SHAKE_TREE, 30, 40) == 1);
    CHECK(pcwld_host_queue_pl_action(aINS_PL_ACT_DIG_SCOOP, 31, 41) == 1);
    CHECK(pcwld_host_queue_pl_action(0, 1, 1) == 0 && pcwld_host_queue_pl_action(5, 1, 1) == 0 && pcwld_host_queue_pl_action(1, -1, 1) == 0);
    CHECK(pcwld_host_take_pl_action(&a, &ux, &uz) == 1 && a == aINS_PL_ACT_SHAKE_TREE && ux == 30 && uz == 40);
    CHECK(pcwld_host_take_pl_action(&a, &ux, &uz) == 1 && a == aINS_PL_ACT_DIG_SCOOP && ux == 31 && uz == 41);
    CHECK(pcwld_host_take_pl_action(&a, &ux, &uz) == 0);
    for (i = 0; i < 20; i++) {
        pcwld_host_queue_pl_action(aINS_PL_ACT_REFLECT_AXE, i, i);
    }
    CHECK(pcwld_host_take_pl_action(&a, &ux, &uz) == 1 && ux == 4); /* full: the 4 oldest dropped */
    while (pcwld_host_take_pl_action(&a, &ux, &uz)) {}
    s_role = 2;
    CHECK(pcwld_host_queue_pl_action(aINS_PL_ACT_SHAKE_TREE, 30, 40) == 0); /* L2 */
    s_role = 1;
    /* L3 */
    kera_id = pcwld_test_host_inject_kind(PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_MOLE_CRICKET, 900.0f, 900.0f);
    mino_id = pcwld_test_host_inject_kind(PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_BAGWORM, 950.0f, 900.0f);
    bf_id = pcwld_test_host_inject_kind(PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_COMMON_BUTTERFLY, 990.0f, 900.0f);
    {
        aINS_INSECT_ACTOR* kin = (aINS_INSECT_ACTOR*)pcwld_bug_local_actor_for_entity(kera_id, &sp);
        aINS_INSECT_ACTOR* min = (aINS_INSECT_ACTOR*)pcwld_bug_local_actor_for_entity(mino_id, &sp);
        aINS_INSECT_ACTOR* bin = (aINS_INSECT_ACTOR*)pcwld_bug_local_actor_for_entity(bf_id, &sp);
        kin->action = 2; kin->insect_flags.bit_1 = FALSE;          /* hidden mole cricket: vanilla still lets the net reach it */
        min->action = 2; min->insect_flags.bit_1 = TRUE;           /* hidden bagworm: uncatchable */
        bin->action = 2; bin->insect_flags.bit_1 = TRUE;           /* not a latent species */
        CHECK(pcwld_host_bug_catch_blocked(kera_id) == 0);
        CHECK(pcwld_host_bug_catch_blocked(mino_id) == 1);
        CHECK(pcwld_host_bug_catch_blocked(bf_id) == 0);
        min->action = 3; /* revealed */
        CHECK(pcwld_host_bug_catch_blocked(mino_id) == 0);
        s_role = 2;
        min->action = 2;
        CHECK(pcwld_host_bug_catch_blocked(mino_id) == 0); /* a client never refuses */
        /* L4 the client follows the host's action out of HIDE through the species hook, never for other species */
        pcwld_latent_follow_host((ACTOR*)min, aINS_INSECT_TYPE_BAGWORM, 2);
        CHECK(min->action == 2 && s_reveal_calls[1] == 1);
        pcwld_latent_follow_host((ACTOR*)min, aINS_INSECT_TYPE_BAGWORM, 3);
        CHECK(min->action == 3);
        pcwld_latent_follow_host((ACTOR*)kin, aINS_INSECT_TYPE_MOLE_CRICKET, 3);
        CHECK(kin->action == 3 && s_reveal_calls[0] == 1);
        pcwld_latent_follow_host((ACTOR*)bin, aINS_INSECT_TYPE_COMMON_BUTTERFLY, 0);
        CHECK(bin->action == 2 && s_reveal_calls[0] + s_reveal_calls[1] + s_reveal_calls[2] == 3);
        s_role = 1;
    }

    /* L5 a hidden latent bug keeps its record position (its hiding UNIT) while its drawn position drifts; once out, the record follows it */
    {
        aINS_INSECT_ACTOR* kin = (aINS_INSECT_ACTOR*)pcwld_bug_local_actor_for_entity(kera_id, &sp);
        PcWildlifeRecord r0, r1;
        CHECK(kin != NULL && pcwld_find_by_id(kera_id, &r0));
        kin->action = 2;
        kin->tools_actor.actor_class.world.position.z = r0.pos_z - 25.0f;
        (void)pcwld_host_collect_state(st, 8);
        CHECK(pcwld_find_by_id(kera_id, &r1) && r1.pos_z == r0.pos_z);
        kin->action = 3;
        (void)pcwld_host_collect_state(st, 8);
        CHECK(pcwld_find_by_id(kera_id, &r1) && r1.pos_z == r0.pos_z - 25.0f);
    }

    /* ---------------- COCKROACH ---------------- */
    {
        static const mActor_name_t trees[] = { TREE, TREE_FTR, TREE_LIGHTS, TREE_BELLS, TREE_APPLE_FRUIT, TREE_ORANGE_FRUIT, TREE_PEACH_FRUIT, TREE_PEAR_FRUIT,
                                               TREE_CHERRY_FRUIT, TREE_1000BELLS, TREE_10000BELLS, TREE_30000BELLS, TREE_100BELLS, CEDAR_TREE, CEDAR_TREE_BELLS,
                                               CEDAR_TREE_FTR, CEDAR_TREE_LIGHTS, GOLD_TREE, GOLD_TREE_BELLS, GOLD_TREE_FTR, GOLD_TREE_SHOVEL };
        int ok = 1;
        for (i = 0; i < (int)(sizeof(trees) / sizeof(trees[0])); i++) {
            s_tile = trees[i];
            ok &= pcwld_cockroach_extra_data(10.0f, 10.0f) == 4;
        }
        CHECK(ok);
        s_tile = ITM_KABU_SPOILED;
        CHECK(pcwld_cockroach_extra_data(10.0f, 10.0f) == 6 && ITM_KABU_SPOILED == 0x2F03);
        s_tile = EMPTY_NO;
        CHECK(pcwld_cockroach_extra_data(10.0f, 10.0f) == 0);
        s_tile = TREE;
        e = pcwld_test_host_inject_kind(PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_COCKROACH, 1100.0f, 1100.0f);
        {
            aINS_INSECT_ACTOR* g = (aINS_INSECT_ACTOR*)pcwld_bug_local_actor_for_entity(e, &sp);
            CHECK(g != NULL && g->tools_actor.actor_class.actor_specific == 4);
        }
    }

    printf("wildlife_special_selftest: %d checks, %d failed\n", s_pass + s_fail, s_fail);
    return s_fail == 0 ? 0 : 1;
}
