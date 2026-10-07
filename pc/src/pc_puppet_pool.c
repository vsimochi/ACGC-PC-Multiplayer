/* pc_puppet_pool.c - see pc_puppet_pool.h. Pure logic. */
#include "pc_puppet_pool.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define PCPP_ALIGN 32

void pc_puppet_pool_init(PCPuppetPool* p, size_t elem) {
    if (p == NULL) {
        return;
    }
    memset(p, 0, sizeof(*p));
    p->elem = elem;
}

int pc_puppet_id_valid(int id) {
    return id >= 0 && id < PC_PUPPET_ID_LIMIT;
}

/* an aligned block with the raw pointer stored just before it */
static void* aligned_zalloc(size_t n) {
    void* raw = calloc(1, n + PCPP_ALIGN + sizeof(void*));
    uintptr_t a;
    if (raw == NULL) {
        return NULL;
    }
    a = ((uintptr_t)raw + sizeof(void*) + (PCPP_ALIGN - 1)) & ~(uintptr_t)(PCPP_ALIGN - 1);
    ((void**)a)[-1] = raw;
    return (void*)a;
}

static void aligned_free(void* p) {
    if (p != NULL) {
        free(((void**)p)[-1]);
    }
}

void* pc_puppet_pool_find(const PCPuppetPool* p, int id) {
    return (p != NULL && pc_puppet_id_valid(id)) ? p->slot[id] : NULL;
}

void* pc_puppet_pool_acquire(PCPuppetPool* p, int id) {
    void* s;
    if (p == NULL || p->elem == 0 || !pc_puppet_id_valid(id)) {
        if (p != NULL) {
            p->alloc_failures++;
        }
        return NULL;
    }
    if (p->slot[id] != NULL) {
        return p->slot[id];
    }
    if (p->fail_next > 0) {
        p->fail_next--;
        p->alloc_failures++;
        return NULL;
    }
    s = aligned_zalloc(p->elem);
    if (s == NULL) {
        p->alloc_failures++;
        return NULL;
    }
    p->slot[id] = s;
    p->used++;
    if (p->used > p->peak) {
        p->peak = p->used;
    }
    return s;
}

void pc_puppet_pool_release(PCPuppetPool* p, int id) {
    if (p == NULL || !pc_puppet_id_valid(id) || p->slot[id] == NULL) {
        return;
    }
    aligned_free(p->slot[id]);
    p->slot[id] = NULL;
    p->used--;
}

void pc_puppet_pool_release_all(PCPuppetPool* p) {
    int i;
    for (i = 0; p != NULL && i < PC_PUPPET_ID_LIMIT; i++) {
        pc_puppet_pool_release(p, i);
    }
}

int pc_puppet_pool_next(const PCPuppetPool* p, int from) {
    int i;
    if (p == NULL) {
        return -1;
    }
    for (i = from < 0 ? 0 : from; i < PC_PUPPET_ID_LIMIT; i++) {
        if (p->slot[i] != NULL) {
            return i;
        }
    }
    return -1;
}

int pc_puppet_actor_headroom(int actors_now, int actors_max, int reserve) {
    const int room = actors_max - actors_now - reserve;
    return room > 0 ? room : 0;
}
