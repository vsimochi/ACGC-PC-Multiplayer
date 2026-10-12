/* pc_dresser_store.c - see pc_dresser_store.h. v2: one record per DRESSER identity (24 extra slots; 27 total with the 3 vanilla keep slots). */
#include "pc_dresser_store.h"

#include <stdio.h>
#include <string.h>

#include "pc_tabfile.h"

_Static_assert(sizeof(PCDresserJrnl) == 32, "PCDresserJrnl drifted (it is a persisted layout)");
_Static_assert(sizeof(PCDresserRec) == 616, "PCDresserRec v3 drifted (it is a persisted layout: bump PC_DRESSER_VERSION)");
_Static_assert(sizeof(PCDresserRecV2) == 608, "PCDresserRecV2 must equal the persisted v2 layout");
_Static_assert(sizeof(PCDresserV1Rec) == 680, "PCDresserV1Rec must equal the persisted v1 layout");

#define PC_DRESSER_SLOT_AUTO 0xFFu

int pc_dresser_store_init(PCDresserStore* s, int max_identities) {
    if (s == NULL || max_identities <= 0) {
        return 0;
    }
    memset(s, 0, sizeof(*s));
    s->ready = pc_keytab_init(&s->tab, sizeof(PCDresserRec), offsetof(PCDresserRec, key), PC_DRESSER_KEY_LEN, max_identities);
    return s->ready;
}

void pc_dresser_store_free(PCDresserStore* s) {
    if (s != NULL && s->ready) {
        pc_keytab_free(&s->tab);
        s->ready = 0;
    }
}

/* 6r: a v2 file (24-slot records) read into a temporary table and converted record by record (every field copied; items 0..23 keep their slots, 24..26 empty). */
static int load_v2(PCDresserStore* s, const char* path) {
    PCKeyTab t2;
    int rc, i, n, ok = 1;
    if (!pc_keytab_init(&t2, sizeof(PCDresserRecV2), offsetof(PCDresserRecV2, key), PC_DRESSER_KEY_LEN, s->tab.max)) {
        return -1;
    }
    rc = pc_tabfile_load(path, PC_DRESSER_MAGIC, 2u, 0u, 0, (size_t)-1, &t2, NULL, NULL, NULL);
    if (rc != PC_TABFILE_OK) {
        pc_keytab_free(&t2);
        return -1;
    }
    (void)pc_keytab_load(&s->tab, NULL, 0);
    n = pc_keytab_count(&t2);
    for (i = 0; i < n && ok; i++) {
        const PCDresserRecV2* o = (const PCDresserRecV2*)pc_keytab_at(&t2, i);
        PCDresserRec* r = o != NULL ? (PCDresserRec*)pc_keytab_get_or_create(&s->tab, o->key, NULL) : NULL;
        if (r == NULL) {
            ok = 0;
            break;
        }
        memset(r, 0, sizeof(*r));
        memcpy(r->key, o->key, sizeof(r->key));
        r->rev = o->rev;
        r->nonce = o->nonce;
        r->max_seq = o->max_seq;
        memcpy(r->fenced, o->fenced, sizeof(r->fenced));
        r->head = o->head;
        r->n = o->n;
        r->orphan = o->orphan;
        r->_r = o->_r;
        memcpy(r->items, o->items, sizeof(o->items));
        memcpy(r->jr, o->jr, sizeof(r->jr));
    }
    pc_keytab_free(&t2);
    if (!ok || pc_keytab_count(&s->tab) != n) {
        (void)pc_keytab_load(&s->tab, NULL, 0);
        return -1;
    }
    return 2;
}

int pc_dresser_store_load(PCDresserStore* s, const char* path) {
    int rc;
    if (s == NULL || !s->ready || path == NULL) {
        return -1;
    }
    rc = pc_tabfile_load(path, PC_DRESSER_MAGIC, PC_DRESSER_VERSION, 0u, 0, (size_t)-1, &s->tab, NULL, NULL, NULL);
    s->dirty = 0;
    if (rc == PC_TABFILE_OK) {
        return 1;
    }
    if (rc != PC_TABFILE_MISSING) {
        FILE* f = fopen(path, "rb");
        uint32_t hdr[2] = { 0, 0 };
        if (f != NULL) {
            if (fread(hdr, sizeof(hdr), 1, f) != 1) {
                hdr[0] = 0;
            }
            fclose(f);
        }
        if (hdr[0] == PC_DRESSER_MAGIC && hdr[1] == 2u && load_v2(s, path) == 2) {
            s->dirty = 1; /* the v3 image exists only in memory until the caller saves it */
            return 2;
        }
    }
    (void)pc_keytab_load(&s->tab, NULL, 0); /* missing or corrupt: start empty (never a half-loaded table) */
    return rc == PC_TABFILE_MISSING ? 0 : -1;
}

int pc_dresser_store_backup_copy(const char* path, const char* suffix, unsigned long tag) {
    char bak[600];
    unsigned char buf[4096];
    FILE* in;
    FILE* out;
    size_t n;
    int ok = 1;
    if (path == NULL || suffix == NULL) {
        return -1;
    }
    snprintf(bak, sizeof(bak), "%s%s", path, suffix);
    if ((out = fopen(bak, "rb")) != NULL) {
        fclose(out);
        snprintf(bak, sizeof(bak), "%s%s-%lu", path, suffix, tag); /* never overwrite an existing backup */
        if ((out = fopen(bak, "rb")) != NULL) {
            fclose(out);
            return -1;
        }
    }
    if ((in = fopen(path, "rb")) == NULL) {
        return -1;
    }
    if ((out = fopen(bak, "wb")) == NULL) {
        fclose(in);
        return -1;
    }
    while ((n = fread(buf, 1, sizeof(buf), in)) > 0) {
        if (fwrite(buf, 1, n, out) != n) {
            ok = 0;
            break;
        }
    }
    if (ferror(in)) {
        ok = 0;
    }
    fclose(in);
    if (fclose(out) != 0) {
        ok = 0;
    }
    if (!ok) {
        remove(bak); /* only the incomplete copy we just created */
        return -1;
    }
    return 1;
}

int pc_dresser_store_save(PCDresserStore* s, const char* path) {
    if (s == NULL || !s->ready || path == NULL) {
        return 0;
    }
    if (!pc_tabfile_save(path, PC_DRESSER_MAGIC, PC_DRESSER_VERSION, 0u, &s->tab)) {
        return 0;
    }
    s->dirty = 0;
    return 1;
}

const PCDresserRec* pc_dresser_store_find(const PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN]) {
    if (s == NULL || !s->ready || key == NULL) {
        return NULL;
    }
    return (const PCDresserRec*)pc_keytab_find(&s->tab, key);
}

static const PCDresserJrnl* jr_find(const PCDresserRec* r, uint32_t nonce, uint32_t seq) {
    int k;
    for (k = 0; k < (int)r->n; k++) {
        const PCDresserJrnl* e = &r->jr[((int)r->head + k) % PC_DRESSER_JOURNAL];
        if (e->nonce == nonce && e->seq == seq) {
            return e;
        }
    }
    return NULL;
}

const PCDresserJrnl* pc_dresser_store_last_applied(const PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN]) {
    const PCDresserRec* r = pc_dresser_store_find(s, key);
    int k;
    if (r == NULL) {
        return NULL;
    }
    for (k = (int)r->n - 1; k >= 0; k--) {
        const PCDresserJrnl* e = &r->jr[((int)r->head + k) % PC_DRESSER_JOURNAL];
        if (e->outcome == PC_DRESSER_OUT_APPLIED) {
            return e;
        }
    }
    return NULL;
}

static void jr_add(PCDresserRec* r, const PCDresserTxn* t, const PCDresserResult* o) {
    PCDresserJrnl* e;
    if ((int)r->n < PC_DRESSER_JOURNAL) {
        e = &r->jr[((int)r->head + (int)r->n) % PC_DRESSER_JOURNAL];
        r->n++;
    } else {
        e = &r->jr[r->head]; /* oldest evicted: its seq is <= max_seq, so it is FENCED from now on, never re-executed */
        r->head = (uint8_t)(((int)r->head + 1) % PC_DRESSER_JOURNAL);
    }
    memset(e, 0, sizeof(*e));
    e->nonce = t->nonce;
    e->seq = t->seq;
    e->msg_hash = t->msg_hash;
    e->pre_pocket_dig = t->pre_pocket_dig;
    e->post_pocket_dig = t->post_pocket_dig;
    e->rev = o->rev;
    e->op = t->op;
    e->outcome = o->outcome;
    e->reason = o->reason;
    e->slot = o->slot;
    e->item = o->item;
    e->pocket = t->pocket;
    e->_r = t->op == PC_DRESSER_OP_MOVE ? t->dst : 0u; /* 6q: MOVE's destination (the reserved byte; 0 for PUT / TAKE as before) */
}

int pc_dresser_store_peek(const PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN], const PCDresserTxn* t, PCDresserResult* out) {
    const PCDresserRec* r = pc_dresser_store_find(s, key);
    const PCDresserJrnl* old;
    if (r == NULL || t == NULL || out == NULL) {
        return 0;
    }
    old = jr_find(r, t->nonce, t->seq);
    if (old == NULL) {
        return 0;
    }
    memset(out, 0, sizeof(*out));
    if (old->msg_hash != t->msg_hash) {
        out->outcome = PC_DRESSER_OUT_REJECTED;
        out->reason = PC_DRESSER_R_CONFLICT;
        out->slot = t->slot;
        out->item = t->item;
        out->rev = r->rev;
        return 1;
    }
    out->outcome = old->outcome;
    out->reason = old->outcome == PC_DRESSER_OUT_APPLIED ? (uint8_t)PC_DRESSER_R_REPLAYED : old->reason;
    out->slot = old->slot;
    out->item = old->item;
    out->rev = old->rev;
    return 1;
}

int pc_dresser_store_restore(PCDresserStore* s, const PCDresserRec* rec) {
    PCDresserRec* r;
    if (s == NULL || !s->ready || rec == NULL) {
        return 0;
    }
    r = (PCDresserRec*)pc_keytab_get_or_create(&s->tab, rec->key, NULL);
    if (r == NULL) {
        return 0;
    }
    memcpy(r, rec, sizeof(*r));
    return 1;
}

static int nonce_fenced(const PCDresserRec* r, uint32_t nonce) {
    int k;
    for (k = 0; k < PC_DRESSER_FENCED_NUM; k++) {
        if (r->fenced[k] != 0 && r->fenced[k] == nonce) {
            return 1;
        }
    }
    return 0;
}

static int reject(PCDresserResult* out, const PCDresserRec* r, const PCDresserTxn* t, uint8_t reason) {
    out->outcome = PC_DRESSER_OUT_REJECTED;
    out->reason = reason;
    out->slot = t->slot;
    out->item = t->item;
    out->rev = r != NULL ? r->rev : 0u;
    return 1;
}

int pc_dresser_store_apply(PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN], const PCDresserTxn* t, PCDresserResult* out) {
    PCDresserRec* r;
    const PCDresserJrnl* old;
    int slot, i;
    if (s == NULL || !s->ready || key == NULL || t == NULL || out == NULL) {
        return 0;
    }
    memset(out, 0, sizeof(*out));
    /* Shape errors are deterministic: answered, never journalled, and they never create a pool. */
    if (t->nonce == 0u || t->seq == 0u ||
        (t->op != PC_DRESSER_OP_PUT && t->op != PC_DRESSER_OP_TAKE && t->op != PC_DRESSER_OP_MOVE && t->op != PC_DRESSER_OP_SWAP) || t->item == 0u ||
        (t->op == PC_DRESSER_OP_SWAP && (t->item2 == 0u || t->slot >= PC_DRESSER_SLOTS)) ||
        (t->slot >= PC_DRESSER_SLOTS && !(t->op == PC_DRESSER_OP_PUT && t->slot == PC_DRESSER_SLOT_AUTO)) ||
        (t->op == PC_DRESSER_OP_MOVE && (t->dst >= PC_DRESSER_SLOTS || t->dst == t->slot))) {
        return reject(out, pc_dresser_store_find(s, key), t, PC_DRESSER_R_BAD_SHAPE);
    }
    r = (PCDresserRec*)pc_keytab_get_or_create(&s->tab, key, NULL);
    if (r == NULL) {
        out->outcome = PC_DRESSER_OUT_REJECTED;
        out->reason = PC_DRESSER_R_FAULT;
        return 0;
    }
    /* 1. idempotency: a journalled (nonce, seq) is replayed, never executed again (also across a host restart: the journal is persisted) */
    old = jr_find(r, t->nonce, t->seq);
    if (old != NULL) {
        if (old->msg_hash != t->msg_hash) {
            return reject(out, r, t, PC_DRESSER_R_CONFLICT);
        }
        out->outcome = old->outcome;
        out->reason = old->outcome == PC_DRESSER_OUT_APPLIED ? (uint8_t)PC_DRESSER_R_REPLAYED : old->reason;
        out->slot = old->slot;
        out->item = old->item;
        out->rev = old->rev;
        return 1;
    }
    /* 2. fencing: superseded nonces and evicted / skipped-back seqs never execute */
    if (nonce_fenced(r, t->nonce) || (t->nonce == r->nonce && t->seq <= r->max_seq)) {
        return reject(out, r, t, PC_DRESSER_R_FENCED);
    }
    if (t->nonce != r->nonce) {
        if (r->nonce != 0u) {
            memmove(&r->fenced[1], &r->fenced[0], sizeof(r->fenced[0]) * (PC_DRESSER_FENCED_NUM - 1));
            r->fenced[0] = r->nonce;
        }
        r->nonce = t->nonce;
        r->max_seq = 0u;
    }
    r->max_seq = t->seq;
    /* 3. execute */
    out->item = t->item;
    if (t->op == PC_DRESSER_OP_PUT) {
        slot = -1;
        if (t->slot == PC_DRESSER_SLOT_AUTO) {
            for (i = 0; i < PC_DRESSER_SLOTS; i++) {
                if (r->items[i] == 0u) {
                    slot = i;
                    break;
                }
            }
        } else if (r->items[t->slot] == 0u) {
            slot = t->slot;
        }
        if (slot < 0) {
            reject(out, r, t, PC_DRESSER_R_NO_ROOM);
            jr_add(r, t, out);
            s->dirty = 1;
            return 1;
        }
        r->items[slot] = t->item;
    } else if (t->op == PC_DRESSER_OP_SWAP) {
        /* 6r: the slot must still hold `item` (it goes to the pocket); the pocket item `item2` takes its place. One record, one journal entry: atomic. */
        slot = t->slot;
        if (r->items[slot] != t->item) {
            reject(out, r, t, PC_DRESSER_R_NOT_AVAILABLE);
            jr_add(r, t, out);
            s->dirty = 1;
            return 1;
        }
        r->items[slot] = t->item2;
    } else if (t->op == PC_DRESSER_OP_MOVE) {
        /* 6q (S3): the item the client saw in `slot` goes to `dst`; whatever `dst` held (if anything) goes to `slot` (a swap). Nothing enters or leaves the pool. */
        const uint16_t other = r->items[t->dst];
        slot = t->slot;
        if (r->items[slot] != t->item) {
            reject(out, r, t, PC_DRESSER_R_NOT_AVAILABLE);
            jr_add(r, t, out);
            s->dirty = 1;
            return 1;
        }
        r->items[t->dst] = t->item;
        r->items[slot] = other;
    } else {
        slot = t->slot;
        if (r->items[slot] != t->item) {
            reject(out, r, t, PC_DRESSER_R_NOT_AVAILABLE);
            jr_add(r, t, out);
            s->dirty = 1;
            return 1;
        }
        r->items[slot] = 0u;
    }
    r->rev++;
    out->outcome = PC_DRESSER_OUT_APPLIED;
    out->reason = PC_DRESSER_R_NONE;
    out->slot = (uint8_t)slot;
    out->rev = r->rev;
    jr_add(r, t, out);
    s->dirty = 1;
    return 1;
}

void pc_dresser_key_make(uint8_t key[PC_DRESSER_KEY_LEN], const uint8_t owner8[8], int floor, int layer, int ut_x, int ut_z, uint16_t ftr_base) {
    memset(key, 0, PC_DRESSER_KEY_LEN);
    memcpy(key, owner8, 8);
    key[8] = (uint8_t)floor;
    key[9] = (uint8_t)layer;
    key[10] = (uint8_t)ut_x;
    key[11] = (uint8_t)ut_z;
    key[12] = (uint8_t)(ftr_base & 0xFFu);
    key[13] = (uint8_t)(ftr_base >> 8);
}

int pc_dresser_store_backup_v1(const char* path, unsigned long tag) {
    FILE* f;
    uint32_t hdr[2];
    char bak[600];
    if (path == NULL || (f = fopen(path, "rb")) == NULL) {
        return 0;
    }
    if (fread(hdr, sizeof(hdr), 1, f) != 1 || hdr[0] != PC_DRESSER_MAGIC || hdr[1] != 1u) {
        fclose(f);
        return 0; /* not a v1 dresser file: nothing to do (a v2 / corrupt file is handled by the load) */
    }
    fclose(f);
    snprintf(bak, sizeof(bak), "%s.v1.bak", path);
    f = fopen(bak, "rb");
    if (f != NULL) {
        fclose(f);
        snprintf(bak, sizeof(bak), "%s.v1.bak-%lu", path, tag); /* never overwrite an existing backup */
        f = fopen(bak, "rb");
        if (f != NULL) {
            fclose(f);
            return -1;
        }
    }
    return rename(path, bak) == 0 ? 1 : -1;
}

int pc_dresser_store_used(const PCDresserRec* r) {
    int i, n = 0;
    for (i = 0; r != NULL && i < PC_DRESSER_SLOTS; i++) {
        n += r->items[i] != 0u;
    }
    return n;
}

int pc_dresser_store_rekey(PCDresserStore* s, const uint8_t from[PC_DRESSER_KEY_LEN], const uint8_t to[PC_DRESSER_KEY_LEN]) {
    PCDresserRec tmp;
    const PCDresserRec* src;
    const PCDresserRec* dst;
    PCDresserRec* r;
    if (s == NULL || !s->ready || from == NULL || to == NULL || memcmp(from, to, PC_DRESSER_KEY_LEN) == 0) {
        return 0;
    }
    src = pc_dresser_store_find(s, from);
    if (src == NULL) {
        return 0;
    }
    dst = pc_dresser_store_find(s, to);
    if (dst != NULL && (pc_dresser_store_used(dst) != 0 || dst->orphan)) {
        return 0; /* never merged */
    }
    memcpy(&tmp, src, sizeof(tmp));
    memcpy(tmp.key, to, PC_DRESSER_KEY_LEN);
    tmp.orphan = 0;
    if (dst != NULL) {
        (void)pc_keytab_remove(&s->tab, to); /* an empty, non-orphaned record at the destination: replaced (it holds no item) */
    }
    r = (PCDresserRec*)pc_keytab_get_or_create(&s->tab, to, NULL);
    if (r == NULL) {
        return 0;
    }
    memcpy(r, &tmp, sizeof(*r));
    (void)pc_keytab_remove(&s->tab, from);
    s->dirty = 1;
    return 1;
}

int pc_dresser_store_set_orphan(PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN], int orphan) {
    PCDresserRec* r;
    if (s == NULL || !s->ready || key == NULL) {
        return 0;
    }
    r = (PCDresserRec*)pc_keytab_find(&s->tab, key);
    if (r == NULL) {
        return 0;
    }
    r->orphan = orphan ? 1u : 0u;
    s->dirty = 1;
    return 1;
}

const PCDresserRec* pc_dresser_store_find_orphan(const PCDresserStore* s, const uint8_t prefix[PC_DRESSER_KEY_PREFIX], uint16_t ftr_base) {
    const PCDresserRec* best = NULL;
    int i, n;
    if (s == NULL || !s->ready || prefix == NULL) {
        return NULL;
    }
    n = pc_keytab_count(&s->tab);
    for (i = 0; i < n; i++) {
        const PCDresserRec* r = (const PCDresserRec*)pc_keytab_at(&s->tab, i);
        if (r == NULL || !r->orphan || memcmp(r->key, prefix, PC_DRESSER_KEY_PREFIX) != 0 || r->key[12] != (uint8_t)(ftr_base & 0xFFu) ||
            r->key[13] != (uint8_t)(ftr_base >> 8)) {
            continue;
        }
        if (best == NULL || r->key[11] < best->key[11] || (r->key[11] == best->key[11] && r->key[10] < best->key[10])) {
            best = r;
        }
    }
    return best;
}

int pc_dresser_store_peek_prefix(const PCDresserStore* s, const uint8_t* prefix, size_t plen, const PCDresserTxn* t, PCDresserResult* out) {
    int i, n;
    if (s == NULL || !s->ready || prefix == NULL || plen > PC_DRESSER_KEY_LEN || t == NULL || out == NULL) {
        return 0;
    }
    n = pc_keytab_count(&s->tab);
    for (i = 0; i < n; i++) {
        const PCDresserRec* r = (const PCDresserRec*)pc_keytab_at(&s->tab, i);
        if (r != NULL && memcmp(r->key, prefix, plen) == 0 && pc_dresser_store_peek(s, r->key, t, out)) {
            return 1;
        }
    }
    return 0;
}

/* ---- v1 -> v2 migration ---- */
int pc_dresser_v1_load(const char* path, PCKeyTab* t, int max_identities, uint32_t* digest) {
    FILE* f;
    uint8_t buf[4096];
    uint32_t h = 2166136261u;
    size_t got, i;
    int rc;
    if (path == NULL || t == NULL || max_identities <= 0 || !pc_keytab_init(t, sizeof(PCDresserV1Rec), offsetof(PCDresserV1Rec, key), PC_DRESSER_KEY_LEN, max_identities)) {
        return -1;
    }
    if ((f = fopen(path, "rb")) == NULL) {
        return 0;
    }
    while ((got = fread(buf, 1, sizeof(buf), f)) > 0) {
        for (i = 0; i < got; i++) {
            h = (h ^ buf[i]) * 16777619u;
        }
    }
    fclose(f);
    rc = pc_tabfile_load(path, PC_DRESSER_MAGIC, 1u, 0u, 0, (size_t)-1, t, NULL, NULL, NULL);
    if (rc != PC_TABFILE_OK) {
        (void)pc_keytab_load(t, NULL, 0);
        return -1;
    }
    if (digest != NULL) {
        *digest = h;
    }
    return 1;
}

void pc_dresser_key_legacy(uint8_t key[PC_DRESSER_KEY_LEN], const uint8_t owner8[8], int page) {
    memset(key, 0, PC_DRESSER_KEY_LEN);
    memcpy(key, owner8, 8);
    key[8] = (uint8_t)PC_DRESSER_LEGACY_FLOOR;
    key[9] = (uint8_t)page;
}

int pc_dresser_store_migrated(const PCDresserStore* s, const uint8_t owner8[8], uint32_t v1_digest) {
    uint8_t k[PC_DRESSER_KEY_LEN];
    const PCDresserRec* m;
    pc_dresser_key_legacy(k, owner8, 0);
    m = pc_dresser_store_find(s, k);
    return m != NULL && m->_r == 1u && m->nonce == v1_digest;
}

int pc_dresser_store_migrate_v1(PCDresserStore* s, const uint8_t owner8[8], const uint16_t items[PC_DRESSER_V1_SLOTS], uint32_t v1_digest,
                                const uint8_t (*keys)[PC_DRESSER_KEY_LEN], int n, int* to_overflow) {
    uint8_t k[PC_DRESSER_KEY_LEN];
    int i, j, total = 0, cap = 0, need_new = 0, placed = 0, over = 0, d, slot;
    if (s == NULL || !s->ready || owner8 == NULL || items == NULL || n < 0 || (n > 0 && keys == NULL) || pc_dresser_store_migrated(s, owner8, v1_digest)) {
        return -1;
    }
    for (i = 0; i < PC_DRESSER_V1_SLOTS; i++) {
        total += items[i] != 0u;
    }
    /* capacity check FIRST (nothing changes on failure): dresser records that must be created + the legacy pages needed */
    for (d = 0; d < n; d++) {
        const PCDresserRec* r = pc_dresser_store_find(s, keys[d]);
        if (memcmp(keys[d], owner8, 8) != 0 || keys[d][8] == (uint8_t)PC_DRESSER_LEGACY_FLOOR || (r != NULL && r->orphan)) {
            return -1;
        }
        if (r == NULL && cap < total) {
            need_new++;
        }
        cap += r == NULL ? PC_DRESSER_SLOTS : PC_DRESSER_SLOTS - pc_dresser_store_used(r);
    }
    over = total > cap ? total - cap : 0;
    for (i = 0; i < PC_DRESSER_LEGACY_PAGES; i++) {
        pc_dresser_key_legacy(k, owner8, i);
        if (pc_dresser_store_find(s, k) == NULL && (i == 0 || over > i * PC_DRESSER_SLOTS)) {
            need_new++;
        }
    }
    if (over > PC_DRESSER_LEGACY_PAGES * PC_DRESSER_SLOTS - pc_dresser_store_overflow_count(s, owner8) || pc_keytab_count(&s->tab) + need_new > s->tab.max) {
        return -1;
    }
    d = 0;
    slot = 0;
    over = 0;
    for (i = 0; i < PC_DRESSER_V1_SLOTS; i++) {
        PCDresserRec* r = NULL;
        if (items[i] == 0u) {
            continue;
        }
        while (d < n) {
            r = (PCDresserRec*)pc_keytab_get_or_create(&s->tab, keys[d], NULL);
            while (r != NULL && slot < PC_DRESSER_SLOTS && r->items[slot] != 0u) {
                slot++;
            }
            if (r != NULL && slot < PC_DRESSER_SLOTS) {
                break;
            }
            d++;
            slot = 0;
            r = NULL;
        }
        if (r != NULL && d < n) {
            r->items[slot] = items[i];
            r->rev++;
            placed++;
            continue;
        }
        for (j = 0; j < PC_DRESSER_LEGACY_PAGES; j++) { /* the legacy overflow, page / slot order */
            int q;
            pc_dresser_key_legacy(k, owner8, j);
            r = (PCDresserRec*)pc_keytab_get_or_create(&s->tab, k, NULL);
            for (q = 0; r != NULL && q < PC_DRESSER_SLOTS && r->items[q] != 0u; q++) {
            }
            if (r != NULL && q < PC_DRESSER_SLOTS) {
                r->items[q] = items[i];
                r->rev++;
                over++;
                break;
            }
        }
    }
    pc_dresser_key_legacy(k, owner8, 0);
    {
        PCDresserRec* m = (PCDresserRec*)pc_keytab_get_or_create(&s->tab, k, NULL);
        if (m != NULL) {
            m->_r = 1u;
            m->nonce = v1_digest; /* the per-owner marker: migrated from THIS v1 file */
        }
    }
    s->dirty = 1;
    if (to_overflow != NULL) {
        *to_overflow = over;
    }
    return placed + over;
}

int pc_dresser_store_overflow_count(const PCDresserStore* s, const uint8_t owner8[8]) {
    uint8_t k[PC_DRESSER_KEY_LEN];
    int i, n = 0;
    for (i = 0; i < PC_DRESSER_LEGACY_PAGES; i++) {
        pc_dresser_key_legacy(k, owner8, i);
        n += pc_dresser_store_used(pc_dresser_store_find(s, k));
    }
    return n;
}

int pc_dresser_store_claim_overflow(PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN]) {
    uint8_t k[PC_DRESSER_KEY_LEN];
    PCDresserRec* dst;
    int p, q, slot = 0, moved = 0;
    if (s == NULL || !s->ready || key == NULL || key[8] == (uint8_t)PC_DRESSER_LEGACY_FLOOR || pc_dresser_store_overflow_count(s, key) == 0) {
        return 0;
    }
    if (pc_dresser_store_find(s, key) == NULL && pc_keytab_count(&s->tab) >= s->tab.max) {
        return 0;
    }
    dst = (PCDresserRec*)pc_keytab_get_or_create(&s->tab, key, NULL);
    if (dst == NULL || dst->orphan) {
        return 0;
    }
    for (p = 0; p < PC_DRESSER_LEGACY_PAGES; p++) {
        PCDresserRec* src;
        pc_dresser_key_legacy(k, key, p);
        src = (PCDresserRec*)pc_keytab_find(&s->tab, k);
        dst = (PCDresserRec*)pc_keytab_find(&s->tab, key); /* re-find: no pointer is kept across a table change */
        for (q = 0; src != NULL && dst != NULL && q < PC_DRESSER_SLOTS; q++) {
            if (src->items[q] == 0u) {
                continue;
            }
            while (slot < PC_DRESSER_SLOTS && dst->items[slot] != 0u) {
                slot++;
            }
            if (slot >= PC_DRESSER_SLOTS) {
                break;
            }
            dst->items[slot] = src->items[q];
            src->items[q] = 0u;
            dst->rev++;
            src->rev++;
            moved++;
        }
    }
    if (moved > 0) {
        s->dirty = 1;
    }
    return moved;
}
