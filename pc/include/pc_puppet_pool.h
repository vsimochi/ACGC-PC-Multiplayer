/* pc_puppet_pool.h - capacity phase 6: the remote-player (puppet) SLOT POOL (pure logic, native tests in tools/net_spike/puppet_pool_selftest.c).
 *
 * The puppet table used to be a static array of 9 slots (player ids 0..7 + the host's wire id 8): every id from 9 up was rejected, so the 9th guest (wire id 9) could see everybody and
 * nobody could see it. Now a slot is allocated ON DEMAND for any usable wire id 0..254 (0xFF is "nobody"; the host's id 8 is a normal key on a CLIENT, where it names the host), kept
 * while the player is tracked and released when it leaves. What the pool bounds is only the slot STATE (memory); the number of live puppet ACTORS is a separate resource (the actor
 * pool of the running scene, pc_puppet_actor_headroom) and the collision table a third (see pc_remote_player.c).
 * Slot memory is zeroed and 32-byte aligned (the appearance buffers are DMA destinations that require it). Allocation failure is reported, never fatal. */
#ifndef PC_PUPPET_POOL_H
#define PC_PUPPET_POOL_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_PUPPET_ID_LIMIT 255 /* usable wire ids 0..254 */

typedef struct PCPuppetPool {
    void*  slot[PC_PUPPET_ID_LIMIT];
    size_t elem;
    int    used;           /* slots allocated now */
    int    peak;           /* most slots allocated at once */
    int    alloc_failures; /* acquire() calls that failed (id out of range counts too) */
    int    fail_next;      /* TEST SEAM: the next N allocations fail */
} PCPuppetPool;

void  pc_puppet_pool_init(PCPuppetPool* p, size_t elem);
int   pc_puppet_id_valid(int id);                  /* 0..254 */
void* pc_puppet_pool_find(const PCPuppetPool* p, int id);    /* NULL = no slot (never allocates) */
void* pc_puppet_pool_acquire(PCPuppetPool* p, int id);       /* the existing slot, or a new zeroed one; NULL = invalid id / out of memory */
void  pc_puppet_pool_release(PCPuppetPool* p, int id);       /* frees the slot (the caller has torn the puppet down); safe for an id without one */
void  pc_puppet_pool_release_all(PCPuppetPool* p);
/* The next allocated id at or after `from` (-1 = none): iteration without scanning the caller's own table. */
int   pc_puppet_pool_next(const PCPuppetPool* p, int from);

/* The finite resource behind a puppet's BODY is the scene's actor pool (mAc_MAX_ACTORS in total, shared with villagers, items, effects ...): how many MORE puppet actors may be created
 * while keeping `reserve` actors free for the rest of the game. */
#define PC_PUPPET_ACTOR_RESERVE 48
int pc_puppet_actor_headroom(int actors_now, int actors_max, int reserve);

#ifdef __cplusplus
}
#endif
#endif
