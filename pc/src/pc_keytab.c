/* pc_keytab.c - see pc_keytab.h. Pure logic. */
#include "pc_keytab.h"

#include <stdlib.h>
#include <string.h>

static uint32_t hash_key(const unsigned char* k, size_t n) {
    uint32_t h = 2166136261u;
    size_t i;
    for (i = 0; i < n; i++) {
        h = (h ^ k[i]) * 16777619u;
    }
    return h;
}

int pc_keytab_init(PCKeyTab* t, size_t elem, size_t key_off, size_t key_len, int max) {
    if (t == NULL || elem == 0 || key_len == 0 || key_off + key_len > elem || max < 1) {
        return 0;
    }
    memset(t, 0, sizeof(*t));
    t->elem = elem;
    t->key_off = key_off;
    t->key_len = key_len;
    t->max = max;
    return 1;
}

void pc_keytab_free(PCKeyTab* t) {
    if (t == NULL) {
        return;
    }
    free(t->data);
    free(t->idx);
    t->data = NULL;
    t->idx = NULL;
    t->count = t->cap = t->idx_cap = 0;
}

static unsigned char* rec(const PCKeyTab* t, int i) {
    return t->data + (size_t)i * t->elem;
}

static void index_insert(PCKeyTab* t, int i) {
    uint32_t h = hash_key(rec(t, i) + t->key_off, t->key_len) & (uint32_t)(t->idx_cap - 1);
    while (t->idx[h] != 0u) {
        h = (h + 1u) & (uint32_t)(t->idx_cap - 1);
    }
    t->idx[h] = (uint32_t)i + 1u;
}

static int reindex(PCKeyTab* t, int idx_cap) {
    int i;
    uint32_t* n = (uint32_t*)calloc((size_t)idx_cap, sizeof(uint32_t));
    if (n == NULL) {
        return 0;
    }
    free(t->idx);
    t->idx = n;
    t->idx_cap = idx_cap;
    for (i = 0; i < t->count; i++) {
        index_insert(t, i);
    }
    return 1;
}

static int find_index(const PCKeyTab* t, const void* key) {
    uint32_t h;
    if (t == NULL || key == NULL || t->idx_cap == 0) {
        return -1;
    }
    h = hash_key((const unsigned char*)key, t->key_len) & (uint32_t)(t->idx_cap - 1);
    while (t->idx[h] != 0u) {
        const int i = (int)t->idx[h] - 1;
        if (memcmp(rec(t, i) + t->key_off, key, t->key_len) == 0) {
            return i;
        }
        h = (h + 1u) & (uint32_t)(t->idx_cap - 1);
    }
    return -1;
}

void* pc_keytab_find(const PCKeyTab* t, const void* key) {
    const int i = find_index(t, key);
    return i < 0 ? NULL : rec(t, i);
}

static int grow(PCKeyTab* t) {
    int ncap = t->cap == 0 ? 16 : t->cap * 2;
    int nidx;
    unsigned char* nd;
    if (t->fail_next > 0) {
        t->fail_next--;
        return 0;
    }
    if (ncap > t->max) {
        ncap = t->max;
    }
    if (ncap <= t->cap) {
        return 0;
    }
    nd = (unsigned char*)realloc(t->data, (size_t)ncap * t->elem);
    if (nd == NULL) {
        return 0;
    }
    t->data = nd;
    t->cap = ncap;
    nidx = 16;
    while (nidx < ncap * 2) {
        nidx *= 2;
    }
    if (nidx != t->idx_cap && !reindex(t, nidx)) {
        return 0; /* the data block is bigger than needed: harmless */
    }
    return 1;
}

void* pc_keytab_get_or_create(PCKeyTab* t, const void* key, int* created) {
    int i;
    if (created != NULL) {
        *created = 0;
    }
    if (t == NULL || key == NULL) {
        return NULL;
    }
    i = find_index(t, key);
    if (i >= 0) {
        return rec(t, i);
    }
    if (t->count >= t->max) {
        return NULL;
    }
    if (t->count >= t->cap && !grow(t)) {
        return NULL;
    }
    if (t->idx_cap == 0 && !reindex(t, 16)) {
        return NULL;
    }
    memset(rec(t, t->count), 0, t->elem);
    memcpy(rec(t, t->count) + t->key_off, key, t->key_len);
    index_insert(t, t->count);
    t->count++;
    if (created != NULL) {
        *created = 1;
    }
    return rec(t, t->count - 1);
}

int pc_keytab_remove(PCKeyTab* t, const void* key) {
    const int i = find_index(t, key);
    if (i < 0) {
        return 0;
    }
    if (i != t->count - 1) {
        memcpy(rec(t, i), rec(t, t->count - 1), t->elem);
    }
    t->count--;
    { /* rebuild in place (no allocation, no tombstones) */
        int k;
        memset(t->idx, 0, (size_t)t->idx_cap * sizeof(uint32_t));
        for (k = 0; k < t->count; k++) {
            index_insert(t, k);
        }
    }
    return 1;
}

void* pc_keytab_at(const PCKeyTab* t, int i) {
    return (t != NULL && i >= 0 && i < t->count) ? rec(t, i) : NULL;
}

int pc_keytab_count(const PCKeyTab* t) {
    return t != NULL ? t->count : 0;
}

int pc_keytab_load(PCKeyTab* t, const void* recs, int n) {
    int i, kept = 0;
    if (t == NULL || (n > 0 && recs == NULL)) {
        return 0;
    }
    t->count = 0;
    if (t->idx != NULL) {
        memset(t->idx, 0, (size_t)t->idx_cap * sizeof(uint32_t));
    }
    for (i = 0; i < n && kept < t->max; i++) {
        const unsigned char* src = (const unsigned char*)recs + (size_t)i * t->elem;
        int created = 0;
        unsigned char* dst = (unsigned char*)pc_keytab_get_or_create(t, src + t->key_off, &created);
        if (dst == NULL) {
            break;
        }
        if (created) {
            memcpy(dst, src, t->elem);
            kept++;
        }
    }
    return kept;
}
