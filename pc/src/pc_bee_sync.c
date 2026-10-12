/* pc_bee_sync.c - multiplayer bee swarm presentation. See pc_bee_sync.h. PRESENTATION ONLY:
 * nothing here writes the field, the inventory, Common_Data or any player state. */
#include "pc_bee_sync.h"
#include "pc_net_game.h"
#include "pc_remote_player.h"

#include "m_actor.h"
#include "m_play.h"
#include "m_name_table.h"

#include <string.h>
#include <stdio.h>
extern int g_pc_verbose;
#include <time.h>

/* ---- owner side: the one local bee actor this process reports ---- */
static void* s_local_actor = NULL;
static uint16_t s_local_seq = 0;
static int s_local_seeded = 0;
static uint8_t s_local_stung_sent = 0;

/* ---- observer side: at most one proxy per origin wire id ---- */
typedef struct PCBeeProxy {
    void* actor;       /* the proxy bee actor (NULL = none) */
    uint16_t seq;      /* shake seq this proxy (or the last ended one) belongs to */
    uint8_t seen;      /* seq is valid (a SPAWN of it was handled) */
    uint8_t ending;    /* DISAPPEAR / CAUGHT received: the proxy fades out */
    uint8_t stung;     /* STING received (cosmetic) */
    uint8_t why_logged; /* test log: the reason the proxy lost its target was printed */
    uint8_t scene_id;  /* the scene it was spawned in */
    uint32_t frames;   /* move frames, for the lifetime cap */
} PCBeeProxy;

static PCBeeProxy s_proxy[PC_NETGAME_WIRE_ID_SPACE];
static int s_spawning_active = 0; /* set while our own Actor_info_make_actor() runs (its ct may run synchronously) */

static int pcbee_find(const void* actor) {
    int i;
    if (actor == NULL) {
        return -1;
    }
    for (i = 0; i < PC_NETGAME_WIRE_ID_SPACE; i++) {
        if (s_proxy[i].actor == actor) {
            return i;
        }
    }
    return -1;
}

/* ---------------- owner side ---------------- */
void pc_bee_sync_local_spawn(void* actor, float x, float y, float z) {
    PCNetGameRole role = pc_net_game_role();
    if (actor == NULL || (role != PC_NETGAME_ROLE_HOST && role != PC_NETGAME_ROLE_CLIENT) || pcbee_find(actor) >= 0) {
        return;
    }
    if (!s_local_seeded) {
        s_local_seq = (uint16_t)time(NULL); /* a restarted owner does not reuse the seq an observer still remembers */
        s_local_seeded = 1;
    }
    s_local_seq++;
    s_local_actor = actor;
    s_local_stung_sent = 0;
    pc_net_game_bee_swarm_send_local(PC_BEE_EV_SPAWN, s_local_seq, x, y, z);
}

void pc_bee_sync_local_event(void* actor, int event, float x, float y, float z) {
    if (actor == NULL || actor != s_local_actor) {
        return; /* not the reported local bee (a proxy, an older replaced bee, or single player) */
    }
    if (event == PC_BEE_EV_STING) {
        if (s_local_stung_sent) {
            return;
        }
        s_local_stung_sent = 1;
    } else if (event == PC_BEE_EV_CAUGHT || event == PC_BEE_EV_DISAPPEAR) {
        s_local_actor = NULL; /* terminal: sent exactly once */
    } else {
        return;
    }
    pc_net_game_bee_swarm_send_local(event, s_local_seq, x, y, z);
}

/* ---------------- observer side ---------------- */
int pc_bee_sync_is_proxy(const void* actor) {
    if (actor == NULL) {
        return 0;
    }
    if (s_spawning_active) {
        return 1; /* ct running synchronously inside our own make_actor call */
    }
    return pcbee_find(actor) >= 0;
}

int pc_bee_sync_proxy_target(const void* actor, float* x, float* y, float* z) {
    PCNetPlayerScene sc;
    int i = pcbee_find(actor);
    if (i < 0) {
        return 0;
    }
    if (++s_proxy[i].frames > PC_BEE_PROXY_MAX_FRAMES || s_proxy[i].ending) {
        return 0;
    }
    if (!pc_remote_player_get_scene((PCNetPlayerId)i, &sc) || !sc.valid || sc.scene_id != s_proxy[i].scene_id) {
        if (g_pc_verbose && !s_proxy[i].why_logged) {
            s_proxy[i].why_logged = 1;
            printf("[NET][BEE] proxy origin %d target lost: owner scene gone/changed\n", i);
        }
        return 0; /* owner disconnected / left the scene */
    }
    {
        const int have_pos = pc_remote_player_get_last_position((PCNetPlayerId)i, x, y, z);
        if (!have_pos && g_pc_verbose && !s_proxy[i].why_logged) {
            s_proxy[i].why_logged = 1;
            printf("[NET][BEE] proxy origin %d target lost: no position snapshot of the owner\n", i);
        }
        return have_pos;
    }
}

int pc_bee_sync_proxy_stung(const void* actor) {
    int i = pcbee_find(actor);
    return i >= 0 && s_proxy[i].stung;
}

void pc_bee_sync_actor_gone(const void* actor) {
    int i;
    if (actor == NULL) {
        return;
    }
    if (actor == s_local_actor) {
        /* the local bee was destroyed without its own DISAPPEAR (scene change): tell the observers once */
        ACTOR* a = (ACTOR*)actor;
        pc_bee_sync_local_event((void*)actor, PC_BEE_EV_DISAPPEAR, a->world.position.x, a->world.position.y, a->world.position.z);
    }
    i = pcbee_find(actor);
    if (i >= 0) {
        s_proxy[i].actor = NULL; /* seq/seen kept: a duplicate SPAWN of the same shake stays ignored */
        if (g_pc_verbose) {
            printf("[NET][BEE] proxy gone origin %d -> proxy count %d\n", i, pc_bee_sync_proxy_count());
        }
    }
}

static void pcbee_spawn_proxy(int origin, uint16_t seq, int scene_id, float x, float y, float z) {
    GAME_PLAY* play = (GAME_PLAY*)gamePT;
    ACTOR* actor;
    PCBeeProxy* p = &s_proxy[origin];

    if (p->actor != NULL) {
        /* a newer shake replaces the old swarm: the old proxy fades (detached; its dt finds nothing) */
        ACTOR* old = (ACTOR*)p->actor;
        p->actor = NULL;
        Actor_delete(old);
    }
    p->seq = seq;
    p->seen = 1;
    p->ending = 0;
    p->stung = 0;
    p->why_logged = 0;
    p->frames = 0;
    p->scene_id = (uint8_t)scene_id;
    if (play == NULL) {
        return;
    }
    s_spawning_active = 1;
    actor = Actor_info_make_actor(&play->actor_info, (GAME*)play, mAc_PROFILE_BEE, x, y + 50.0f, z, 0, 0, 0, play->block_table.block_x,
                                  play->block_table.block_z, -1, EMPTY_NO, 0, -1, -1);
    s_spawning_active = 0;
    p->actor = actor; /* NULL on allocation failure: nothing shown, nothing retried */
    if (g_pc_verbose) {
        printf("[NET][BEE] proxy spawn origin %d seq %u actor %s\n", origin, (unsigned)seq, actor != NULL ? "created" : "NULL");
    }
}

void pc_bee_sync_present(int origin, int event, uint16_t seq, int scene_id, float x, float y, float z) {
    PCBeeProxy* p;
    if (origin < 0 || origin >= PC_NETGAME_WIRE_ID_SPACE) {
        return;
    }
    p = &s_proxy[origin];
    switch (event) {
        case PC_BEE_EV_SPAWN:
            if (p->seen && p->seq == seq) {
                return; /* duplicate SPAWN of the same shake: one swarm per shake event */
            }
            if (p->seen && p->actor != NULL && !p->ending && (int16_t)(uint16_t)(seq - p->seq) < 0) {
                return; /* a late SPAWN of an OLDER shake must not replace the live swarm (wraparound-safe); only while a proxy is live, so a restarted owner (time-seeded seq) is never locked out */
            }
            pcbee_spawn_proxy(origin, seq, scene_id, x, y, z);
            break;
        case PC_BEE_EV_STING:
            if (p->seen && p->seq == seq && p->actor != NULL) {
                p->stung = 1;
            }
            break;
        case PC_BEE_EV_CAUGHT:
        case PC_BEE_EV_DISAPPEAR:
            if (p->seen && p->seq == seq) {
                p->ending = 1; /* the proxy fades on its next move; idempotent */
            }
            break;
        default:
            break;
    }
    if (g_pc_verbose) {
        printf("[NET][BEE] present origin %d event %d seq %u scene %d -> proxy count %d\n", origin, event, (unsigned)seq, scene_id, pc_bee_sync_proxy_count());
    }
}

int pc_bee_sync_proxy_count(void) {
    int i, n = 0;
    for (i = 0; i < PC_NETGAME_WIRE_ID_SPACE; i++) {
        n += s_proxy[i].actor != NULL;
    }
    return n;
}
