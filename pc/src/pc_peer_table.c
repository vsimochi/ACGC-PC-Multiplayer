/* pc_peer_table.c - see pc_peer_table.h. Pure C, libc only. */
#include "pc_peer_table.h"

#include <stdlib.h>
#include <string.h>

static void* (*s_alloc)(size_t, size_t) = NULL;
static void (*s_release)(void*) = NULL;

void pc_peer_tables_set_allocator(void* (*alloc)(size_t count, size_t size), void (*release)(void*)) {
    s_alloc = alloc;
    s_release = release;
}

static void* pt_alloc(size_t count, size_t size) {
    return s_alloc != NULL ? s_alloc(count, size) : calloc(count, size);
}

static void pt_free(void* p) {
    if (p == NULL) {
        return;
    }
    if (s_release != NULL) {
        s_release(p);
    } else {
        free(p);
    }
}

int pc_peer_capacity_max(int reserved_id) {
    return (reserved_id >= 0 && reserved_id <= PC_PEER_ID_LAST) ? PC_PEER_ID_LAST : PC_PEER_ID_LAST + 1;
}

int pc_peer_span(int capacity, int reserved_id) {
    if (capacity < 1 || capacity > pc_peer_capacity_max(reserved_id)) {
        return 0;
    }
    /* the capacity-th usable id is capacity-1 while it lies below the reserved id, else one higher */
    return (reserved_id >= 0 && capacity - 1 >= reserved_id) ? capacity + 1 : capacity;
}

int pc_peer_id_usable(int id, int span, int reserved_id) {
    return id >= 0 && id < span && id <= PC_PEER_ID_LAST && id != reserved_id;
}

int pc_peer_find_free(int span, int reserved_id, int (*is_free)(void* ctx, int id), void* ctx) {
    int id;
    if (is_free == NULL) {
        return -1;
    }
    for (id = 0; id < span && id <= PC_PEER_ID_LAST; id++) {
        if (id != reserved_id && is_free(ctx, id)) {
            return id;
        }
    }
    return -1;
}

void pc_peer_set_clear(PCPeerSet* s) {
    memset(s, 0, sizeof(*s));
}

void pc_peer_set_add(PCPeerSet* s, int id) {
    if (id >= 0 && id < PC_PEER_ID_SPACE) {
        s->w[id >> 5] |= 1u << (id & 31);
    }
}

void pc_peer_set_del(PCPeerSet* s, int id) {
    if (id >= 0 && id < PC_PEER_ID_SPACE) {
        s->w[id >> 5] &= ~(1u << (id & 31));
    }
}

int pc_peer_set_has(const PCPeerSet* s, int id) {
    return id >= 0 && id < PC_PEER_ID_SPACE && (s->w[id >> 5] & (1u << (id & 31))) != 0;
}

int pc_peer_set_empty(const PCPeerSet* s) {
    int i;
    for (i = 0; i < PC_PEER_SET_WORDS; i++) {
        if (s->w[i] != 0u) {
            return 0;
        }
    }
    return 1;
}

int pc_peer_set_count(const PCPeerSet* s) {
    int i, n = 0;
    for (i = 0; i < PC_PEER_SET_WORDS; i++) {
        uint32_t v = s->w[i];
        while (v != 0u) {
            v &= v - 1u;
            n++;
        }
    }
    return n;
}

uint32_t pc_peer_set_low32(const PCPeerSet* s) {
    return s->w[0];
}

int pc_peer_set_has_other(const PCPeerSet* s, int id) {
    PCPeerSet t = *s;
    pc_peer_set_del(&t, id);
    return !pc_peer_set_empty(&t);
}

int pc_peer_tables_resize(PCPeerTable* t, int n, int inline_n, int span) {
    int i, j;
    void* fresh[64];
    if (t == NULL || n < 1 || n > 64 || inline_n < 1 || span < 1) {
        return 0;
    }
    if (span > inline_n) {
        for (i = 0; i < n; i++) {
            fresh[i] = t[i].elem != 0u ? pt_alloc((size_t)span, t[i].elem) : NULL;
            if (fresh[i] == NULL) {
                for (j = 0; j < i; j++) {
                    pt_free(fresh[j]);
                }
                return 0;
            }
        }
    } else {
        for (i = 0; i < n; i++) {
            fresh[i] = NULL;
        }
    }
    for (i = 0; i < n; i++) {
        pt_free(t[i].heap);
        t[i].heap = fresh[i];
        if (fresh[i] != NULL) {
            t[i].bind(fresh[i]);
        } else {
            memset(t[i].inline_buf, 0, (size_t)inline_n * t[i].elem);
            t[i].bind(t[i].inline_buf);
        }
    }
    return 1;
}

void pc_peer_tables_release(PCPeerTable* t, int n, int inline_n) {
    (void)pc_peer_tables_resize(t, n, inline_n, inline_n);
}

int pc_grow_tables_ensure(PCGrowTable* t, int n, int have, int want) {
    int i, j;
    void* fresh[64];
    if (t == NULL || n < 1 || n > 64 || have < 0 || want < 1) {
        return 0;
    }
    if (want <= have) {
        return 1;
    }
    for (i = 0; i < n; i++) {
        fresh[i] = t[i].elem != 0u ? pt_alloc((size_t)(t[i].base + want), t[i].elem) : NULL;
        if (fresh[i] == NULL) {
            for (j = 0; j < i; j++) {
                pt_free(fresh[j]);
            }
            return 0;
        }
    }
    for (i = 0; i < n; i++) {
        memcpy(fresh[i], t[i].cur, (size_t)(t[i].base + have) * t[i].elem);
        t[i].bind(fresh[i]);
        pt_free(t[i].heap);
        t[i].heap = fresh[i];
        t[i].cur = fresh[i];
    }
    return 1;
}
