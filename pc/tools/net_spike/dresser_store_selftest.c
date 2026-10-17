/* dresser_store_selftest.c - native unit test of pc/src/pc_dresser_store.c (GROUNDWORK module, not wired into gameplay).
 * Build + run (from the repo root, git-bash, PATH has /c/msys64/ucrt64/bin appended):
 *   gcc -std=gnu11 -Wall -Wextra -Ipc/include pc/tools/net_spike/dresser_store_selftest.c pc/src/pc_dresser_store.c pc/src/pc_tabfile.c pc/src/pc_keytab.c -o <scratch>/dresser_selftest.exe
 *   <scratch>/dresser_selftest.exe <scratch dir>
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pc_dresser_store.h"
#include "pc_tabfile.h"

static int s_fail = 0;
#define CHECK(c)                                                    \
    do {                                                            \
        if (!(c)) {                                                 \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);     \
            s_fail++;                                               \
        }                                                           \
    } while (0)

static PCDresserTxn txn(uint32_t nonce, uint32_t seq, uint8_t op, uint8_t slot, uint16_t item) {
    PCDresserTxn t;
    memset(&t, 0, sizeof(t));
    t.nonce = nonce;
    t.seq = seq;
    t.op = op;
    t.slot = slot;
    t.item = item;
    t.msg_hash = nonce * 31u + seq * 7u + op * 3u + slot + item * 13u;
    t.pre_pocket_dig = 0x1111u * seq;
    t.post_pocket_dig = 0x2222u * seq;
    return t;
}

int main(int argc, char** argv) {
    char path[600];
    uint8_t ka[PC_DRESSER_KEY_LEN], kb[PC_DRESSER_KEY_LEN];
    PCDresserStore s, s2;
    PCDresserResult r;
    PCDresserTxn t;
    const PCDresserRec* rec;
    FILE* f;
    int i;

    snprintf(path, sizeof(path), "%s/dresser_store_test.dat", argc > 1 ? argv[1] : ".");
    remove(path);
    memset(ka, 0, sizeof(ka));
    memset(kb, 0, sizeof(kb));
    memcpy(ka, "resident-A", 10);
    memcpy(kb, "guest-B", 7);

    CHECK(pc_dresser_store_init(&s, 64));
    /* pre-feature save: no file = empty store */
    CHECK(pc_dresser_store_load(&s, path) == 0);
    CHECK(pc_dresser_store_find(&s, ka) == NULL);

    /* put into an explicit slot */
    t = txn(100, 1, PC_DRESSER_OP_PUT, 5, 0x2A01);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED && r.slot == 5 && r.rev == 1);
    /* duplicate (lost TXN_RESULT retry): replayed, not executed twice */
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED && r.reason == PC_DRESSER_R_REPLAYED && r.rev == 1);
    rec = pc_dresser_store_find(&s, ka);
    CHECK(rec != NULL && rec->rev == 1 && rec->items[5] == 0x2A01);
    /* same (nonce, seq) different bytes: conflict */
    t = txn(100, 1, PC_DRESSER_OP_PUT, 6, 0x2A02);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.outcome == PC_DRESSER_OUT_REJECTED && r.reason == PC_DRESSER_R_CONFLICT);
    /* occupied slot */
    t = txn(100, 2, PC_DRESSER_OP_PUT, 5, 0x2A02);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.reason == PC_DRESSER_R_NO_ROOM);
    rec = pc_dresser_store_find(&s, ka);
    CHECK(rec->items[5] == 0x2A01 && rec->rev == 1);
    /* auto slot */
    t = txn(100, 3, PC_DRESSER_OP_PUT, 0xFF, 0x2A03);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED && r.slot == 0);
    /* take of a wrong expected item */
    t = txn(100, 4, PC_DRESSER_OP_TAKE, 5, 0x2A03);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.reason == PC_DRESSER_R_NOT_AVAILABLE);
    /* stale seq (skipped back) is fenced */
    t = txn(100, 2, PC_DRESSER_OP_TAKE, 5, 0x2A01);
    t.msg_hash ^= 0x55u;
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.outcome == PC_DRESSER_OUT_REJECTED && (r.reason == PC_DRESSER_R_CONFLICT || r.reason == PC_DRESSER_R_FENCED));
    t = txn(100, 1, PC_DRESSER_OP_TAKE, 0, 0x2A03); /* hash differs from the journalled seq 1 */
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.reason == PC_DRESSER_R_CONFLICT);
    /* take ok */
    t = txn(100, 5, PC_DRESSER_OP_TAKE, 5, 0x2A01);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED && r.item == 0x2A01);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.reason == PC_DRESSER_R_REPLAYED); /* second take does not grant again */
    rec = pc_dresser_store_find(&s, ka);
    CHECK(rec->items[5] == 0 && rec->rev == 3);
    /* peek: journalled -> answered without executing; unknown -> 0; different bytes -> CONFLICT */
    CHECK(pc_dresser_store_peek(&s, ka, &t, &r) == 1 && r.outcome == PC_DRESSER_OUT_APPLIED && r.reason == PC_DRESSER_R_REPLAYED && r.rev == 3);
    {
        PCDresserTxn u = txn(100, 99, PC_DRESSER_OP_PUT, 9, 0x2A09);
        PCDresserTxn c = t;
        c.msg_hash ^= 1u;
        CHECK(pc_dresser_store_peek(&s, ka, &u, &r) == 0);
        CHECK(pc_dresser_store_peek(&s, ka, &c, &r) == 1 && r.reason == PC_DRESSER_R_CONFLICT);
        CHECK(pc_dresser_store_peek(&s, kb, &u, &r) == 0);
        rec = pc_dresser_store_find(&s, ka);
        CHECK(rec->rev == 3 && rec->items[9] == 0); /* peek never executes */
    }
    /* pocket slot is journalled (restart roll-forward) */
    {
        PCDresserTxn p = txn(100, 7, PC_DRESSER_OP_PUT, 7, 0x2A07);
        const PCDresserJrnl* j;
        PCDresserRec snap;
        p.pocket = 11;
        memcpy(&snap, pc_dresser_store_find(&s, ka), sizeof(snap));
        CHECK(pc_dresser_store_apply(&s, ka, &p, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        j = pc_dresser_store_last_applied(&s, ka);
        CHECK(j != NULL && j->pocket == 11 && j->item == 0x2A07 && j->seq == 7 && j->post_pocket_dig == p.post_pocket_dig);
        /* restore (the write-ahead save failed): the pool AND the journal are back, the same txn then executes normally */
        CHECK(pc_dresser_store_restore(&s, &snap));
        rec = pc_dresser_store_find(&s, ka);
        CHECK(rec->rev == 3 && rec->items[7] == 0 && pc_dresser_store_peek(&s, ka, &p, &r) == 0);
        CHECK(pc_dresser_store_apply(&s, ka, &p, &r) && r.outcome == PC_DRESSER_OUT_APPLIED && r.rev == 4);
        t = txn(100, 8, PC_DRESSER_OP_TAKE, 7, 0x2A07);
        CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED && r.rev == 5);
    }
    /* bad shapes */
    t = txn(100, 6, PC_DRESSER_OP_PUT, PC_DRESSER_SLOTS, 0x2A01);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.reason == PC_DRESSER_R_BAD_SHAPE);
    t = txn(100, 6, PC_DRESSER_OP_PUT, 1, 0);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.reason == PC_DRESSER_R_BAD_SHAPE);
    t = txn(0, 6, PC_DRESSER_OP_PUT, 1, 1);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.reason == PC_DRESSER_R_BAD_SHAPE);
    /* identities are isolated */
    t = txn(100, 1, PC_DRESSER_OP_PUT, 5, 0x3000);
    CHECK(pc_dresser_store_apply(&s, kb, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED && r.rev == 1);
    CHECK(pc_dresser_store_find(&s, ka)->items[5] == 0);
    /* reconnect = new client process nonce: old nonce fenced, its journalled entries still replay */
    t = txn(200, 1, PC_DRESSER_OP_PUT, 7, 0x2A07);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
    t = txn(100, 5, PC_DRESSER_OP_TAKE, 5, 0x2A01);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.reason == PC_DRESSER_R_REPLAYED);
    t = txn(100, 9, PC_DRESSER_OP_PUT, 9, 0x2A09);
    CHECK(pc_dresser_store_apply(&s, ka, &t, &r) && r.reason == PC_DRESSER_R_FENCED);
    CHECK(pc_dresser_store_find(&s, ka)->items[9] == 0);
    CHECK(pc_dresser_store_last_applied(&s, ka) != NULL && pc_dresser_store_last_applied(&s, ka)->post_pocket_dig == 0x2222u);

    /* persistence: save, reload, replay after "restart" */
    CHECK(s.dirty == 1);
    CHECK(pc_dresser_store_save(&s, path) == 1 && s.dirty == 0);
    CHECK(pc_dresser_store_init(&s2, 64));
    CHECK(pc_dresser_store_load(&s2, path) == 1);
    rec = pc_dresser_store_find(&s2, ka);
    CHECK(rec != NULL && rec->items[0] == 0x2A03 && rec->items[7] == 0x2A07 && rec->items[5] == 0 && rec->nonce == 200);
    t = txn(200, 1, PC_DRESSER_OP_PUT, 7, 0x2A07);
    CHECK(pc_dresser_store_apply(&s2, ka, &t, &r) && r.reason == PC_DRESSER_R_REPLAYED);
    CHECK(pc_dresser_store_find(&s2, kb)->items[5] == 0x3000);

    /* journal eviction: an evicted seq is fenced, never re-executed */
    for (i = 2; i < 2 + PC_DRESSER_JOURNAL + 2; i++) {
        t = txn(200, (uint32_t)i, PC_DRESSER_OP_PUT, 0xFF, (uint16_t)(0x4000 + i));
        CHECK(pc_dresser_store_apply(&s2, ka, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
    }
    t = txn(200, 2, PC_DRESSER_OP_PUT, 0xFF, 0x4002);
    CHECK(pc_dresser_store_apply(&s2, ka, &t, &r) && r.reason == PC_DRESSER_R_FENCED);
    /* pool full */
    {
        int applied = 0;
        for (i = 100; i < 100 + PC_DRESSER_SLOTS + 2; i++) {
            t = txn(200, (uint32_t)i, PC_DRESSER_OP_PUT, 0xFF, 0x5000);
            CHECK(pc_dresser_store_apply(&s2, ka, &t, &r));
            applied += r.outcome == PC_DRESSER_OUT_APPLIED;
        }
        CHECK(r.reason == PC_DRESSER_R_NO_ROOM);
        CHECK(applied == PC_DRESSER_SLOTS - 2 - PC_DRESSER_JOURNAL - 2 + 0); /* 2 + 18 slots were used before */
    }

    /* corruption fallback: a flipped byte is refused, the store comes up empty and load reports -1 */
    {
        FILE* g = fopen(path, "r+b");
        CHECK(g != NULL);
        if (g != NULL) {
            int c;
            fseek(g, 40, SEEK_SET);
            c = fgetc(g);
            fseek(g, 40, SEEK_SET);
            fputc(c ^ 0xA5, g);
            fclose(g);
        }
    }
    CHECK(pc_dresser_store_load(&s2, path) == -1);
    CHECK(pc_dresser_store_find(&s2, ka) == NULL);
    /* wrong magic / truncated file */
    f = fopen(path, "wb");
    if (f != NULL) {
        fputs("junk", f);
        fclose(f);
    }
    CHECK(pc_dresser_store_load(&s2, path) == -1);

    pc_dresser_store_free(&s);
    pc_dresser_store_free(&s2);
    remove(path);

    /* ===== v2: per-DRESSER identities ===== */
    {
        static const uint8_t own_a[8] = { 'O', 'W', 'N', 'E', 'R', '-', 'A', 0 };
        static const uint8_t own_b[8] = { 'O', 'W', 'N', 'E', 'R', '-', 'B', 0 };
        uint8_t d1[PC_DRESSER_KEY_LEN], d2[PC_DRESSER_KEY_LEN], d3[PC_DRESSER_KEY_LEN], dmv[PC_DRESSER_KEY_LEN], dmv2[PC_DRESSER_KEY_LEN];
        uint8_t pre[PC_DRESSER_KEY_PREFIX];
        PCDresserStore v;
        const PCDresserRec* o;
        char p1[600], bak[700], bak2[700];
        int k, applied;

        CHECK(PC_DRESSER_TOTAL == 27 && PC_DRESSER_SLOTS == 27 && PC_DRESSER_VANILLA == 3); /* 6r: 27 host slots */
        CHECK(pc_dresser_store_init(&v, 64));
        pc_dresser_key_make(d1, own_a, 0, 0, 3, 4, 0x1100);
        pc_dresser_key_make(d2, own_a, 0, 0, 7, 4, 0x1100); /* the same furniture id, another cell of the same room */
        pc_dresser_key_make(d3, own_b, 0, 0, 3, 4, 0x1100); /* the same cell in another owner's house */
        CHECK(memcmp(d1, d2, PC_DRESSER_KEY_LEN) != 0 && memcmp(d1, d3, PC_DRESSER_KEY_LEN) != 0);
        /* V1 independent identities: two dressers in one room never share */
        t = txn(500, 1, PC_DRESSER_OP_PUT, 0xFF, 0x2401);
        CHECK(pc_dresser_store_apply(&v, d1, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        CHECK(pc_dresser_store_used(pc_dresser_store_find(&v, d1)) == 1 && pc_dresser_store_find(&v, d2) == NULL && pc_dresser_store_find(&v, d3) == NULL);
        t = txn(500, 2, PC_DRESSER_OP_TAKE, 0, 0x2401);
        CHECK(pc_dresser_store_apply(&v, d2, &t, &r) && r.reason == PC_DRESSER_R_NOT_AVAILABLE); /* d2 is empty: d1's item is not reachable through d2 */
        CHECK(pc_dresser_store_used(pc_dresser_store_find(&v, d1)) == 1);
        /* V2 the 27-slot limit (6r): full refuses, nothing lost */
        applied = 1;
        for (k = 0; k < PC_DRESSER_SLOTS + 3; k++) {
            t = txn(500, (uint32_t)(10 + k), PC_DRESSER_OP_PUT, 0xFF, (uint16_t)(0x2500 + k));
            CHECK(pc_dresser_store_apply(&v, d1, &t, &r));
            applied += r.outcome == PC_DRESSER_OUT_APPLIED;
        }
        CHECK(applied == PC_DRESSER_SLOTS && r.reason == PC_DRESSER_R_NO_ROOM && pc_dresser_store_used(pc_dresser_store_find(&v, d1)) == PC_DRESSER_SLOTS);
        /* V3 persistence: save, reload */
        snprintf(p1, sizeof(p1), "%s/dresser_store_v2_test.dat", argc > 1 ? argv[1] : ".");
        remove(p1);
        CHECK(pc_dresser_store_save(&v, p1) == 1);
        CHECK(pc_dresser_store_init(&s2, 64) && pc_dresser_store_load(&s2, p1) == 1);
        CHECK(pc_dresser_store_used(pc_dresser_store_find(&s2, d1)) == PC_DRESSER_SLOTS && pc_dresser_store_find(&s2, d2) != NULL &&
              pc_dresser_store_used(pc_dresser_store_find(&s2, d2)) == 0);
        t = txn(500, 32, PC_DRESSER_OP_PUT, 0xFF, (uint16_t)(0x2500 + 22)); /* replay of the last APPLIED put after the reload (seq 1 was evicted from the 16-deep journal) */
        CHECK(pc_dresser_store_apply(&s2, d1, &t, &r) && r.reason == PC_DRESSER_R_REPLAYED);
        pc_dresser_store_free(&s2);
        /* V4 re-key (an unambiguous move): the record and its journal follow; the old key is gone; never merged into a non-empty one */
        pc_dresser_key_make(dmv, own_a, 0, 0, 5, 6, 0x1100);
        CHECK(pc_dresser_store_rekey(&v, d1, d2) == 1); /* d2 held an EMPTY record: replaced */
        CHECK(pc_dresser_store_find(&v, d1) == NULL && pc_dresser_store_used(pc_dresser_store_find(&v, d2)) == PC_DRESSER_SLOTS);
        t = txn(500, 32, PC_DRESSER_OP_PUT, 0xFF, (uint16_t)(0x2500 + 22));
        CHECK(pc_dresser_store_apply(&v, d2, &t, &r) && r.reason == PC_DRESSER_R_REPLAYED); /* the journal travelled with it */
        t = txn(500, 1, PC_DRESSER_OP_PUT, 0xFF, 0x2401);
        CHECK(pc_dresser_store_apply(&v, d2, &t, &r) && r.reason == PC_DRESSER_R_FENCED); /* evicted seq: fenced, never re-executed */
        t = txn(501, 1, PC_DRESSER_OP_PUT, 0xFF, 0x2601);
        CHECK(pc_dresser_store_apply(&v, d3, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        CHECK(pc_dresser_store_rekey(&v, d2, d3) == 0 && pc_dresser_store_used(pc_dresser_store_find(&v, d2)) == PC_DRESSER_SLOTS); /* d3 not empty: refused */
        CHECK(pc_dresser_store_rekey(&v, dmv, d1) == 0); /* no source */
        /* V5 orphan + deterministic re-attach (the lowest cell of that owner / floor / layer / furniture id) */
        CHECK(pc_dresser_store_set_orphan(&v, d2, 1));
        pc_dresser_key_make(dmv2, own_a, 0, 0, 1, 9, 0x1100);
        t = txn(502, 1, PC_DRESSER_OP_PUT, 0xFF, 0x2701);
        CHECK(pc_dresser_store_apply(&v, dmv2, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        CHECK(pc_dresser_store_set_orphan(&v, dmv2, 1));
        memcpy(pre, d2, PC_DRESSER_KEY_PREFIX);
        o = pc_dresser_store_find_orphan(&v, pre, 0x1100);
        CHECK(o != NULL && o->key[10] == 7 && o->key[11] == 4); /* (7,4) before (1,9): lower ut_z first */
        CHECK(pc_dresser_store_find_orphan(&v, pre, 0x1104) == NULL); /* another furniture id never matches */
        CHECK(pc_dresser_store_rekey(&v, o->key, dmv) == 1 && pc_dresser_store_find(&v, dmv)->orphan == 0 && pc_dresser_store_used(pc_dresser_store_find(&v, dmv)) == PC_DRESSER_SLOTS);
        o = pc_dresser_store_find_orphan(&v, pre, 0x1100);
        CHECK(o != NULL && o->key[10] == 1 && o->key[11] == 9 && pc_dresser_store_used(o) == 1); /* the other orphan is still kept */
        pc_dresser_store_free(&v);
        remove(p1);
        /* V6 the v1 file is backed up ONCE and an existing backup is never overwritten */
        {
            FILE* g;
            uint32_t h1[4] = { PC_DRESSER_MAGIC, 1u, 0u, 0u };
            snprintf(bak, sizeof(bak), "%s.v1.bak", p1);
            snprintf(bak2, sizeof(bak2), "%s.v1.bak-77", p1);
            remove(bak);
            remove(bak2);
            g = fopen(p1, "wb");
            if (g != NULL) {
                fwrite(h1, sizeof(h1), 1, g);
                fclose(g);
            }
            CHECK(pc_dresser_store_backup_v1(p1, 77) == 1);
            g = fopen(bak, "rb");
            CHECK(g != NULL);
            if (g != NULL) {
                fclose(g);
            }
            CHECK(pc_dresser_store_backup_v1(p1, 77) == 0); /* nothing left to back up */
            g = fopen(p1, "wb");
            if (g != NULL) {
                fwrite(h1, sizeof(h1), 1, g);
                fclose(g);
            }
            CHECK(pc_dresser_store_backup_v1(p1, 77) == 1); /* .v1.bak exists: a timestamped name */
            g = fopen(bak2, "rb");
            CHECK(g != NULL);
            if (g != NULL) {
                fclose(g);
            }
            g = fopen(p1, "wb");
            if (g != NULL) {
                fwrite(h1, sizeof(h1), 1, g);
                fclose(g);
            }
            CHECK(pc_dresser_store_backup_v1(p1, 77) == -1); /* both names taken: refused, nothing overwritten */
            CHECK(pc_dresser_store_init(&v, 8) && pc_dresser_store_load(&v, p1) == -1); /* a v1 file is never read as v2 */
            pc_dresser_store_free(&v);
            remove(p1);
            remove(bak);
            remove(bak2);
        }
    }
    /* V7 v1 -> v2 MIGRATION: deterministic, restart-safe, exactly once, overflow kept and claimable */
    {
        PCKeyTab t1;
        PCDresserStore m;
        PCDresserV1Rec* vr;
        uint8_t own[8] = { 9, 8, 7, 6, 5, 4, 3, 2 }, v1key[PC_DRESSER_KEY_LEN] = { 'A', 'B' }, keys[2][PC_DRESSER_KEY_LEN], lk[PC_DRESSER_KEY_LEN];
        uint32_t dig = 0, dig2 = 0;
        int i, over = -1, total;
        char pv1[600], pv2[600];
        snprintf(pv1, sizeof(pv1), "%s/mig_v1.dat", argc > 1 ? argv[1] : ".");
        snprintf(pv2, sizeof(pv2), "%s/mig_v2.dat", argc > 1 ? argv[1] : ".");
        remove(pv1);
        remove(pv2);
        CHECK(pc_keytab_init(&t1, sizeof(PCDresserV1Rec), 0, PC_DRESSER_KEY_LEN, 8));
        vr = (PCDresserV1Rec*)pc_keytab_get_or_create(&t1, v1key, NULL);
        CHECK(vr != NULL);
        for (i = 0; vr != NULL && i < PC_DRESSER_V1_SLOTS; i++) {
            vr->items[i] = (uint16_t)(0x2500 + i); /* a FULL v1 pool: 60 distinct items */
        }
        CHECK(pc_tabfile_save(pv1, PC_DRESSER_MAGIC, 1u, 0u, &t1));
        pc_keytab_free(&t1);
        CHECK(pc_dresser_v1_load(pv1, &t1, 8, &dig) == 1 && pc_keytab_count(&t1) == 1);
        vr = (PCDresserV1Rec*)pc_keytab_find(&t1, v1key);
        CHECK(vr != NULL && vr->items[59] == 0x253B);
        CHECK(pc_dresser_v1_load(pv1, &t1, 8, &dig2) == 1 && dig2 == dig); /* the digest is stable */
        vr = (PCDresserV1Rec*)pc_keytab_find(&t1, v1key);
        CHECK(pc_dresser_store_init(&m, 32) && pc_dresser_store_load(&m, pv1) == -1); /* a v1 file is never read as v2 */
        pc_dresser_key_make(keys[0], own, 0, 0, 3, 4, 0x1068);
        pc_dresser_key_make(keys[1], own, 0, 0, 9, 4, 0x1068);
        {   /* dresser 0 already holds 20 items (used before the migration): only its 7 free slots are filled (6r: 27 slots) */
            PCDresserRec* d0 = (PCDresserRec*)pc_keytab_get_or_create(&m.tab, keys[0], NULL);
            for (i = 0; d0 != NULL && i < 20; i++) {
                d0->items[i] = (uint16_t)(0x2600 + i);
            }
        }
        CHECK(pc_dresser_store_save(&m, pv2));
        /* "crash before the save": migrate in memory, do NOT save, reload = nothing migrated; then again -> the SAME result */
        CHECK(pc_dresser_store_migrate_v1(&m, own, vr->items, dig, (const uint8_t(*)[PC_DRESSER_KEY_LEN])keys, 2, &over) == 60 && over == 26);
        CHECK(pc_dresser_store_load(&m, pv2) == 1 && !pc_dresser_store_migrated(&m, own, dig) && pc_dresser_store_used(pc_dresser_store_find(&m, keys[0])) == 20);
        CHECK(pc_dresser_store_migrate_v1(&m, own, vr->items, dig, (const uint8_t(*)[PC_DRESSER_KEY_LEN])keys, 2, &over) == 60 && over == 26);
        CHECK(pc_dresser_store_find(&m, keys[0])->items[20] == 0x2500 && pc_dresser_store_find(&m, keys[0])->items[26] == 0x2506); /* v1 slot order */
        CHECK(pc_dresser_store_used(pc_dresser_store_find(&m, keys[1])) == 27 && pc_dresser_store_find(&m, keys[1])->items[0] == 0x2507);
        CHECK(pc_dresser_store_overflow_count(&m, own) == 26);
        pc_dresser_key_legacy(lk, own, 0);
        CHECK(pc_dresser_store_find(&m, lk)->items[0] == 0x2522 && pc_dresser_store_migrated(&m, own, dig));
        CHECK(pc_dresser_store_save(&m, pv2));
        /* "crash after the save": a restart sees the marker -> nothing again (no duplication) */
        CHECK(pc_dresser_store_load(&m, pv2) == 1 && pc_dresser_store_migrated(&m, own, dig));
        CHECK(pc_dresser_store_migrate_v1(&m, own, vr->items, dig, (const uint8_t(*)[PC_DRESSER_KEY_LEN])keys, 2, &over) == -1);
        /* conservation: every v1 item exactly once across the owner's dressers + overflow */
        total = 0;
        for (i = 0; i < PC_DRESSER_V1_SLOTS; i++) {
            int k, p, cnt = 0;
            for (k = 0; k < 2; k++) {
                const PCDresserRec* r = pc_dresser_store_find(&m, keys[k]);
                for (p = 0; r != NULL && p < PC_DRESSER_SLOTS; p++) {
                    cnt += r->items[p] == (uint16_t)(0x2500 + i);
                }
            }
            for (k = 0; k < PC_DRESSER_LEGACY_PAGES; k++) {
                const PCDresserRec* r;
                pc_dresser_key_legacy(lk, own, k);
                r = pc_dresser_store_find(&m, lk);
                for (p = 0; r != NULL && p < PC_DRESSER_SLOTS; p++) {
                    cnt += r->items[p] == (uint16_t)(0x2500 + i);
                }
            }
            total += cnt == 1;
        }
        CHECK(total == 60);
        /* claim path: a dresser with free room takes overflow items (page / slot order), nothing duplicated */
        {
            uint8_t k3[PC_DRESSER_KEY_LEN];
            pc_dresser_key_make(k3, own, 1, 0, 5, 5, 0x1070);
            CHECK(pc_dresser_store_claim_overflow(&m, k3) == 26 && pc_dresser_store_overflow_count(&m, own) == 0 &&
                  pc_dresser_store_find(&m, k3)->items[0] == 0x2522);
            CHECK(pc_dresser_store_claim_overflow(&m, k3) == 0); /* the overflow is empty: nothing more */
            CHECK(pc_dresser_store_migrated(&m, own, dig)); /* the marker survives an emptied page 0 */
        }
        /* no dresser at all: everything to the overflow (27 + 27 + 6) */
        {
            uint8_t own2[8] = { 1, 1, 1, 1, 1, 1, 1, 1 };
            CHECK(pc_dresser_store_migrate_v1(&m, own2, vr->items, dig, NULL, 0, &over) == 60 && over == 60 && pc_dresser_store_overflow_count(&m, own2) == 60);
        }
        /* 5 items + 2 empty dressers: all 5 in the first dresser, in v1 slot order */
        {
            uint8_t own3[8] = { 3, 3, 3, 3, 3, 3, 3, 3 }, k5[2][PC_DRESSER_KEY_LEN];
            uint16_t five[PC_DRESSER_V1_SLOTS];
            memset(five, 0, sizeof(five));
            five[0] = 0x2701; five[7] = 0x2702; five[8] = 0x2703; five[30] = 0x2704; five[59] = 0x2705;
            pc_dresser_key_make(k5[0], own3, 0, 0, 1, 1, 0x1068);
            pc_dresser_key_make(k5[1], own3, 0, 0, 2, 1, 0x1068);
            CHECK(pc_dresser_store_migrate_v1(&m, own3, five, dig, (const uint8_t(*)[PC_DRESSER_KEY_LEN])k5, 2, &over) == 5 && over == 0);
            CHECK(pc_dresser_store_used(pc_dresser_store_find(&m, k5[0])) == 5 && pc_dresser_store_find(&m, k5[0])->items[4] == 0x2705 &&
                  pc_dresser_store_find(&m, k5[1]) == NULL);
        }
        pc_keytab_free(&t1);
        pc_dresser_store_free(&m);
        remove(pv1);
        remove(pv2);
    }
    /* 6q (S3): MOVE = move into an empty slot / swap with an occupied one; journalled + idempotent; nothing enters or leaves the pool */
    {
        PCDresserStore mv;
        uint8_t km[PC_DRESSER_KEY_LEN];
        const PCDresserRec* q;
        const PCDresserJrnl* j;
        PCDresserTxn a;
        int before, after, k;
        memset(km, 0, sizeof(km));
        memcpy(km, "move-test", 9);
        CHECK(pc_dresser_store_init(&mv, 8));
        a = txn(900, 1, PC_DRESSER_OP_PUT, 0, 0x2B01);
        CHECK(pc_dresser_store_apply(&mv, km, &a, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        a = txn(900, 2, PC_DRESSER_OP_PUT, 1, 0x1304);
        CHECK(pc_dresser_store_apply(&mv, km, &a, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        q = pc_dresser_store_find(&mv, km);
        before = pc_dresser_store_used(q);
        a = txn(900, 3, PC_DRESSER_OP_MOVE, 0, 0x2B01); /* 0 -> 5 (empty): a move */
        a.dst = 5;
        CHECK(pc_dresser_store_apply(&mv, km, &a, &r) && r.outcome == PC_DRESSER_OUT_APPLIED && r.slot == 0);
        q = pc_dresser_store_find(&mv, km);
        CHECK(q->items[0] == 0 && q->items[5] == 0x2B01 && q->items[1] == 0x1304);
        j = pc_dresser_store_last_applied(&mv, km);
        CHECK(j != NULL && j->op == PC_DRESSER_OP_MOVE && j->slot == 0 && j->_r == 5 && j->item == 0x2B01);
        CHECK(pc_dresser_store_apply(&mv, km, &a, &r) && r.reason == PC_DRESSER_R_REPLAYED); /* replayed, never executed twice */
        CHECK(pc_dresser_store_find(&mv, km)->items[5] == 0x2B01 && pc_dresser_store_find(&mv, km)->items[0] == 0);
        a = txn(900, 4, PC_DRESSER_OP_MOVE, 1, 0x1304); /* 1 -> 5 (occupied): a swap */
        a.dst = 5;
        CHECK(pc_dresser_store_apply(&mv, km, &a, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        q = pc_dresser_store_find(&mv, km);
        CHECK(q->items[5] == 0x1304 && q->items[1] == 0x2B01);
        a = txn(900, 5, PC_DRESSER_OP_MOVE, 2, 0x1304); /* the source does not hold the item: NOT_AVAILABLE, nothing moves */
        a.dst = 6;
        CHECK(pc_dresser_store_apply(&mv, km, &a, &r) && r.outcome == PC_DRESSER_OUT_REJECTED && r.reason == PC_DRESSER_R_NOT_AVAILABLE);
        a = txn(900, 6, PC_DRESSER_OP_MOVE, 5, 0x1304); /* src == dst / dst out of range: BAD_SHAPE */
        a.dst = 5;
        CHECK(pc_dresser_store_apply(&mv, km, &a, &r) && r.reason == PC_DRESSER_R_BAD_SHAPE);
        a.dst = PC_DRESSER_SLOTS;
        CHECK(pc_dresser_store_apply(&mv, km, &a, &r) && r.reason == PC_DRESSER_R_BAD_SHAPE);
        q = pc_dresser_store_find(&mv, km);
        after = pc_dresser_store_used(q);
        for (k = 0; k < PC_DRESSER_SLOTS; k++) {
            CHECK(q->items[k] == 0 || q->items[k] == 0x1304 || q->items[k] == 0x2B01);
        }
        CHECK(before == 2 && after == 2 && q->items[5] == 0x1304 && q->items[1] == 0x2B01); /* conserved: the same 2 items, each once */
        pc_dresser_store_free(&mv);
    }
    /* 6r: SWAP = pool slot <-> pocket item in one transaction: the slot gets item2, `item` (the pool item) is journalled as the one entering the pocket */
    {
        PCDresserStore sw;
        uint8_t ks[PC_DRESSER_KEY_LEN];
        const PCDresserJrnl* j;
        PCDresserTxn a;
        memset(ks, 0, sizeof(ks));
        memcpy(ks, "swap-test", 9);
        CHECK(pc_dresser_store_init(&sw, 8));
        a = txn(700, 1, PC_DRESSER_OP_PUT, 26, 0x1304); /* the last cell (27th slot) is a host slot now */
        CHECK(pc_dresser_store_apply(&sw, ks, &a, &r) && r.outcome == PC_DRESSER_OUT_APPLIED && r.slot == 26);
        a = txn(700, 2, PC_DRESSER_OP_SWAP, 26, 0x1304);
        a.item2 = 0x2037;
        a.pocket = 4;
        CHECK(pc_dresser_store_apply(&sw, ks, &a, &r) && r.outcome == PC_DRESSER_OUT_APPLIED && r.slot == 26 && r.item == 0x1304);
        CHECK(pc_dresser_store_find(&sw, ks)->items[26] == 0x2037 && pc_dresser_store_used(pc_dresser_store_find(&sw, ks)) == 1);
        j = pc_dresser_store_last_applied(&sw, ks);
        CHECK(j != NULL && j->op == PC_DRESSER_OP_SWAP && j->item == 0x1304 && j->pocket == 4 && j->slot == 26);
        CHECK(pc_dresser_store_apply(&sw, ks, &a, &r) && r.reason == PC_DRESSER_R_REPLAYED && pc_dresser_store_find(&sw, ks)->items[26] == 0x2037);
        a = txn(700, 3, PC_DRESSER_OP_SWAP, 26, 0x1304); /* the slot no longer holds 0x1304: NOT_AVAILABLE, nothing changes */
        a.item2 = 0x2401;
        CHECK(pc_dresser_store_apply(&sw, ks, &a, &r) && r.reason == PC_DRESSER_R_NOT_AVAILABLE && pc_dresser_store_find(&sw, ks)->items[26] == 0x2037);
        a = txn(700, 4, PC_DRESSER_OP_SWAP, 26, 0x2037); /* item2 = 0: BAD_SHAPE */
        CHECK(pc_dresser_store_apply(&sw, ks, &a, &r) && r.reason == PC_DRESSER_R_BAD_SHAPE);
        pc_dresser_store_free(&sw);
    }
    /* 6r: v2 (24-slot) file -> v3 (27 slots): loaded + converted losslessly (load = 2), the v2 bytes are backed up (never overwritten), the v3 save reloads as v3 */
    {
        PCKeyTab t2;
        PCDresserRecV2* o;
        PCDresserStore cv;
        const PCDresserRec* q;
        uint8_t k2[PC_DRESSER_KEY_LEN];
        char p2[600], b2[700], b3[700];
        FILE* fa;
        FILE* fb;
        int same = 1, ca, cb2, k;
        snprintf(p2, sizeof(p2), "%s/dresser_store_v2conv_test.dat", argc > 1 ? argv[1] : ".");
        snprintf(b2, sizeof(b2), "%s.v2.bak", p2);
        snprintf(b3, sizeof(b3), "%s.v2.bak-77", p2);
        remove(p2);
        remove(b2);
        remove(b3);
        memset(k2, 0, sizeof(k2));
        memcpy(k2, "v2-dresser", 10);
        CHECK(pc_keytab_init(&t2, sizeof(PCDresserRecV2), offsetof(PCDresserRecV2, key), PC_DRESSER_KEY_LEN, 8));
        o = (PCDresserRecV2*)pc_keytab_get_or_create(&t2, k2, NULL);
        CHECK(o != NULL);
        if (o != NULL) {
            o->rev = 9;
            o->nonce = 0xABCD;
            o->max_seq = 5;
            o->n = 1;
            o->jr[0].nonce = 0xABCD;
            o->jr[0].seq = 5;
            o->jr[0].op = PC_DRESSER_OP_PUT;
            o->jr[0].item = 0x1304;
            for (k = 0; k < PC_DRESSER_V2_SLOTS; k++) {
                o->items[k] = (uint16_t)(k % 3 == 0 ? 0x2400 + k : 0);
            }
            o->items[23] = 0x1304;
        }
        CHECK(pc_tabfile_save(p2, PC_DRESSER_MAGIC, 2u, 0u, &t2));
        CHECK(pc_dresser_store_init(&cv, 8) && pc_dresser_store_load(&cv, p2) == 2 && cv.dirty == 1);
        q = pc_dresser_store_find(&cv, k2);
        CHECK(q != NULL && q->rev == 9 && q->nonce == 0xABCD && q->max_seq == 5 && q->n == 1 && q->jr[0].item == 0x1304 && q->jr[0].seq == 5);
        for (k = 0; q != NULL && o != NULL && k < PC_DRESSER_SLOTS; k++) {
            same &= q->items[k] == (k < PC_DRESSER_V2_SLOTS ? o->items[k] : 0u);
        }
        CHECK(same && pc_dresser_store_used(q) == 9); /* every v2 item kept in its slot, 24..26 empty */
        t = txn(0xABCD, 5, PC_DRESSER_OP_PUT, 0xFF, 0x1304); /* the converted journal still answers (replay, never re-executed) */
        t.msg_hash = 0;
        CHECK(pc_dresser_store_peek(&cv, k2, &t, &r) == 1);
        CHECK(pc_dresser_store_backup_copy(p2, ".v2.bak", 77) == 1 && pc_dresser_store_backup_copy(p2, ".v2.bak", 77) == 1 &&
              pc_dresser_store_backup_copy(p2, ".v2.bak", 77) == -1); /* .v2.bak, then .v2.bak-77, then refused: never overwritten */
        fa = fopen(p2, "rb");
        fb = fopen(b2, "rb");
        do {
            ca = fa != NULL ? fgetc(fa) : -2;
            cb2 = fb != NULL ? fgetc(fb) : -3;
            same &= ca == cb2;
        } while (ca != EOF && same);
        if (fa != NULL) {
            fclose(fa);
        }
        if (fb != NULL) {
            fclose(fb);
        }
        CHECK(same); /* the backup is byte-identical to the v2 file */
        CHECK(pc_dresser_store_save(&cv, p2) == 1);
        pc_dresser_store_free(&cv);
        CHECK(pc_dresser_store_init(&cv, 8) && pc_dresser_store_load(&cv, p2) == 1 && pc_dresser_store_used(pc_dresser_store_find(&cv, k2)) == 9 &&
              pc_dresser_store_find(&cv, k2)->items[23] == 0x1304);
        pc_dresser_store_free(&cv);
        pc_keytab_free(&t2);
        remove(p2);
        remove(b2);
        remove(b3);
    }
    { /* ---- C3: the OUTDOOR key + the shared-record apply + drop_empty ---- */
        PCDresserStore o;
        uint8_t k0[PC_DRESSER_KEY_LEN], k0r[PC_DRESSER_KEY_LEN], k1[PC_DRESSER_KEY_LEN], own[8], hk[PC_DRESSER_KEY_LEN];
        char p3[640];
        pc_dresser_key_outdoor(k0, 40, 57, 0x1064);
        pc_dresser_key_outdoor(k0r, 40, 57, 0x1067); /* the same piece rotated: rotation masked -> the SAME key */
        pc_dresser_key_outdoor(k1, 40, 56, 0x1064);  /* the piece moved one tile north: a different key */
        pc_dresser_town_owner8(own);
        CHECK(memcmp(k0, k0r, PC_DRESSER_KEY_LEN) == 0);
        CHECK(memcmp(k0, k1, PC_DRESSER_KEY_LEN) != 0);
        CHECK(pc_dresser_key_is_outdoor(k0) && k0[8] == 0xFC && k0[9] == 0 && k0[10] == 40 && k0[11] == 57 && k0[12] == 0x64 && k0[13] == 0x10 && k0[14] == 0 &&
              k0[15] == 0 && memcmp(k0, own, 8) == 0);
        pc_dresser_key_outdoor(k1, 95, 111, 0x3FFF);
        CHECK(k1[10] == 95 && k1[11] == 111 && k1[12] == 0xFC && k1[13] == 0x3F); /* the town corner fits a byte; rotation masked */
        pc_dresser_key_outdoor(k1, 40, 56, 0x1064);
        pc_dresser_key_make(hk, own, 0, 0, 40, 57, 0x1064); /* a HOUSE key never equals / is never taken for an outdoor key (floor 0) */
        CHECK(!pc_dresser_key_is_outdoor(hk) && !pc_dresser_key_is_outdoor(ka));
        CHECK(pc_dresser_store_init(&o, 8));
        /* two residents (two nonces) take turns at one shared piece: nobody is FENCED by the other one */
        t = txn(0xA1, 1, PC_DRESSER_OP_PUT, 0, 0x2A01);
        CHECK(pc_dresser_store_apply_shared(&o, k0, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        t = txn(0xB2, 1, PC_DRESSER_OP_PUT, 1, 0x2A02);
        CHECK(pc_dresser_store_apply_shared(&o, k0, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        t = txn(0xA1, 2, PC_DRESSER_OP_TAKE, 1, 0x2A02); /* A again (its nonce would be FENCED by the record fence of the house apply) */
        CHECK(pc_dresser_store_apply_shared(&o, k0, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        CHECK(pc_dresser_store_apply_shared(&o, k0, &t, &r) && r.reason == PC_DRESSER_R_REPLAYED); /* the record journal still replays */
        t.msg_hash ^= 1u;
        CHECK(pc_dresser_store_apply_shared(&o, k0, &t, &r) && r.reason == PC_DRESSER_R_CONFLICT);
        rec = pc_dresser_store_find(&o, k0);
        CHECK(rec != NULL && rec->items[0] == 0x2A01 && rec->items[1] == 0 && rec->rev == 3);
        /* the same sequence with the HOUSE apply fences A (why the shared apply exists) */
        pc_dresser_key_make(hk, own, 0, 0, 1, 1, 0x1064);
        t = txn(0xA1, 1, PC_DRESSER_OP_PUT, 0, 0x2A01);
        CHECK(pc_dresser_store_apply(&o, hk, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        t = txn(0xB2, 1, PC_DRESSER_OP_PUT, 1, 0x2A02);
        CHECK(pc_dresser_store_apply(&o, hk, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        t = txn(0xA1, 2, PC_DRESSER_OP_PUT, 2, 0x2A03);
        CHECK(pc_dresser_store_apply(&o, hk, &t, &r) && r.reason == PC_DRESSER_R_FENCED);
        /* MOVE of the piece: re-key keeps the contents; ROTATE: same key */
        CHECK(pc_dresser_store_rekey(&o, k0, k1) == 1 && pc_dresser_store_find(&o, k0) == NULL && pc_dresser_store_find(&o, k1)->items[0] == 0x2A01);
        pc_dresser_key_outdoor(k0, 40, 57, 0x1064);
        CHECK(pc_dresser_store_rekey(&o, k1, k0) == 1 && pc_dresser_store_used(pc_dresser_store_find(&o, k0)) == 1);
        /* orphan + re-attach by id (the outdoor prefix): an orphan of the same id is found, a different id is not */
        CHECK(pc_dresser_store_set_orphan(&o, k0, 1) == 1);
        {
            uint8_t pre[PC_DRESSER_KEY_PREFIX];
            memcpy(pre, k0, PC_DRESSER_KEY_PREFIX);
            CHECK(pc_dresser_store_find_orphan(&o, pre, 0x1064) == pc_dresser_store_find(&o, k0));
            CHECK(pc_dresser_store_find_orphan(&o, pre, 0x1068) == NULL);
        }
        /* drop_empty never drops an item / an orphan */
        CHECK(pc_dresser_store_drop_empty(&o, k0) == 0);
        CHECK(pc_dresser_store_set_orphan(&o, k0, 0) == 1);
        CHECK(pc_dresser_store_drop_empty(&o, k0) == 0); /* still 1 item */
        t = txn(0xA1, 3, PC_DRESSER_OP_TAKE, 0, 0x2A01);
        CHECK(pc_dresser_store_apply_shared(&o, k0, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        CHECK(pc_dresser_store_drop_empty(&o, k0) == 1 && pc_dresser_store_find(&o, k0) == NULL);
        CHECK(pc_dresser_store_drop_empty(&o, k0) == 0);
        /* persisted like every record (no format change: v3) */
        t = txn(0xA1, 4, PC_DRESSER_OP_PUT, 3, 0x2A05);
        CHECK(pc_dresser_store_apply_shared(&o, k1, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        snprintf(p3, sizeof(p3), "%s/dresser_outdoor_test.dat", argc > 1 ? argv[1] : ".");
        remove(p3);
        CHECK(pc_dresser_store_save(&o, p3) == 1);
        pc_dresser_store_free(&o);
        CHECK(pc_dresser_store_init(&o, 8) && pc_dresser_store_load(&o, p3) == 1 && pc_dresser_store_find(&o, k1) != NULL &&
              pc_dresser_store_find(&o, k1)->items[3] == 0x2A05 && pc_dresser_key_is_outdoor(pc_dresser_store_find(&o, k1)->key));
        pc_dresser_store_free(&o);
        remove(p3);
    }
    printf("dresser_store_selftest: %s (%d failures)\n", s_fail == 0 ? "PASS" : "FAIL", s_fail);
    return s_fail == 0 ? 0 : 1;
}
