/* pc_bee_sync.h - multiplayer bee swarm presentation (PC_NETGAME_MSG_BEE_SWARM, PRESENTATION ONLY).
 *
 * The shaker's own process stays authoritative for its bee: ac_bee.c targets only the local player and
 * runs the sting / dodge / net-catch tests against it, exactly as in single player. This module only
 * (1) reports that local bee's lifecycle (spawn / sting / caught / disappear) to pc_net_game.c, which
 * relays it to the other READY players in the same scene, and (2) on an observer, spawns a COSMETIC proxy
 * bee that orbits the owner's puppet. A proxy never calls mPlib_*, never registers a net catch, never
 * sets Common player_bee_chase_flag, and deletes itself on DISAPPEAR / CAUGHT / owner gone or in another
 * scene / lifetime cap; the local scene change destroys it like any actor (its dt clears the table).
 * One proxy per (owner id, shake seq): a duplicate SPAWN of the same seq is ignored.
 * Everything is inert in single player (no message is ever sent or received there). */
#ifndef PC_BEE_SYNC_H
#define PC_BEE_SYNC_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum {
    PC_BEE_EV_SPAWN = 1,
    PC_BEE_EV_STING = 2,
    PC_BEE_EV_CAUGHT = 3,
    PC_BEE_EV_DISAPPEAR = 4,
};

#define PC_BEE_PROXY_MAX_FRAMES (60 * 120) /* safety cap: a proxy fades after ~2 minutes of frames even without DISAPPEAR */

/* ---- owner side (the process whose player shook the tree) ---- */
/* bg_item.c pc_tree_birth_bee_visual(): a local bee actor was born at the honeycomb drop position. */
void pc_bee_sync_local_spawn(void* actor, float x, float y, float z);
/* ac_bee.c: the local bee reached STING / CAUGHT / DISAPPEAR (sent once per event per swarm; ignored for proxies). */
void pc_bee_sync_local_event(void* actor, int event, float x, float y, float z);

/* ---- ac_bee.c, both sides ---- */
int  pc_bee_sync_is_proxy(const void* actor);          /* 1 for an observer proxy (also while its own ct runs inside the spawn) */
int  pc_bee_sync_proxy_target(const void* actor, float* x, float* y, float* z); /* 0 => the proxy must disappear */
int  pc_bee_sync_proxy_stung(const void* actor);        /* the owner was stung (cosmetic: tighter orbit) */
void pc_bee_sync_actor_gone(const void* actor);         /* the bee actor's dt: clears a proxy entry / sends a pending owner DISAPPEAR */

/* ---- pc_net_game.c ---- */
/* A validated relay of another player's bee event, already checked to be in the local scene. Never mutates game state. */
void pc_bee_sync_present(int origin, int event, uint16_t seq, int scene_id, float x, float y, float z);
/* Defined in pc_net_game.c: send the local bee event (client -> host, host -> same-scene READY clients). Inert unless networked. */
void pc_net_game_bee_swarm_send_local(int event, uint16_t seq, float x, float y, float z);

/* test/diagnostic */
int  pc_bee_sync_proxy_count(void);

#ifdef __cplusplus
}
#endif

#endif
