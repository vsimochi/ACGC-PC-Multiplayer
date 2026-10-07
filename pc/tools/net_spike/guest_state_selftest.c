/* guest_state_selftest.c - capacity phase 7, native tests of the shared guest-state building blocks: pc/src/pc_keytab.c (dynamic table keyed by character identity),
 * pc/src/pc_tabfile.c (its durable file, work_jobs.dat v3 + the v2 reader) and pc/src/pc_dayclaims.c (K.K.'s per-guest, per-day claim). The game code that uses them
 * (pc_net_game.c: the work table, the K.K. claim) needs the game and is only syntax-checked here; these tests prove the data structures and the file format.
 * Usage: guest_state_selftest <scratch_dir> (prints PASS:/FAIL: and "RESULT passed=N failed=M") */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "pc_dayclaims.h"
#include "pc_keytab.h"
#include "pc_tabfile.h"

static int s_pass, s_fail;
static void check(const char* what, int ok) {
    printf("%s: %s\n", ok ? "PASS" : "FAIL", what);
    fflush(stdout);
    if (ok) s_pass++; else s_fail++;
}

/* the very layout of PCNetWorkChar (pc_net_game.c): 56 bytes, key (a 20-byte PersonalID) first, `used` at offset 0x2A */
typedef struct WorkChar {
    uint8_t  key[20];
    uint32_t job_id, reward, jobs_done, last_rewarded;
    uint16_t obj_item, obj_count, target_villager, carried_item;
    uint32_t tip_value;
    uint8_t  used, mode_on, state, job_type, obj_state, tip_kind, tip_paid, pad;
} WorkChar;
_Static_assert(sizeof(WorkChar) == 56, "layout");

static void key_of(uint8_t k[20], int n) {
    int i;
    for (i = 0; i < 20; i++) k[i] = (uint8_t)(i * 7 + n * 13 + (n >> 8) * 31);
    k[16] = (uint8_t)(n >> 24);
    k[17] = (uint8_t)(n >> 16);
    k[18] = (uint8_t)(n >> 8);
    k[19] = (uint8_t)n;
}

static char s_root[300];
static void path_of(char* out, size_t cap, const char* name) { snprintf(out, cap, "%s/%s", s_root, name); }

/* a v2 file exactly as the old game wrote it: header {magic, 2, next_job_id, sizeof(body)}, 64 fixed records, crc = fnv(header) ^ fnv(body) */
static void write_v2(const char* path, const WorkChar* body64, uint32_t next_id) {
    uint32_t hdr[4], crc;
    FILE* f = fopen(path, "wb");
    hdr[0] = 0x4B574341u; hdr[1] = 2u; hdr[2] = next_id; hdr[3] = (uint32_t)(64 * sizeof(WorkChar));
    crc = pc_tabfile_fnv(hdr, sizeof(hdr)) ^ pc_tabfile_fnv(body64, 64 * sizeof(WorkChar));
    fwrite(hdr, sizeof(hdr), 1, f);
    fwrite(body64, sizeof(WorkChar), 64, f);
    fwrite(&crc, sizeof(crc), 1, f);
    fclose(f);
}
static void flip(const char* path, long off) {
    FILE* f = fopen(path, "r+b");
    int c;
    if (!f) return;
    fseek(f, off, SEEK_SET); c = fgetc(f); fseek(f, off, SEEK_SET); fputc(c ^ 0x5A, f); fclose(f);
}

int main(int argc, char** argv) {
    PCKeyTab t, t2;
    int i, ok, created;
    uint8_t k[20];
    char p[400], p2[400];
    if (argc < 2) { fprintf(stderr, "usage: guest_state_selftest <scratch_dir>\n"); return 2; }
    snprintf(s_root, sizeof(s_root), "%s", argv[1]);
    mkdir(s_root, 0755);

    /* ================= keytab ================= */
    check("init refuses bad geometry (key past the record, zero sizes, max 0)", !pc_keytab_init(&t, 8, 4, 8, 10) && !pc_keytab_init(&t, 0, 0, 1, 10) && !pc_keytab_init(&t, 8, 0, 0, 10) && !pc_keytab_init(&t, 8, 0, 4, 0));
    check("init", pc_keytab_init(&t, sizeof(WorkChar), 0, 20, 100000));
    key_of(k, 1);
    check("find on an empty table is NULL (nothing allocated)", pc_keytab_find(&t, k) == NULL && pc_keytab_count(&t) == 0);
    {
        WorkChar* w = (WorkChar*)pc_keytab_get_or_create(&t, k, &created);
        check("create: zeroed record with the key stored, created=1", w != NULL && created == 1 && w->job_id == 0 && memcmp(w->key, k, 20) == 0);
        w->job_id = 77;
        w = (WorkChar*)pc_keytab_get_or_create(&t, k, &created);
        check("the same key finds the same record (created=0, data kept)", w != NULL && created == 0 && w->job_id == 77 && pc_keytab_count(&t) == 1);
    }
    for (i = 2; i <= 100000; i++) {
        WorkChar* w;
        key_of(k, i);
        w = (WorkChar*)pc_keytab_get_or_create(&t, k, NULL);
        if (w == NULL) break;
        w->job_id = (uint32_t)i;
    }
    check("100000 distinct characters fit (the old fixed table held 64)", i == 100001 && pc_keytab_count(&t) == 100000);
    ok = 1;
    for (i = 1; i <= 100000; i += 997) {
        const WorkChar* w;
        key_of(k, i);
        w = (const WorkChar*)pc_keytab_find(&t, k);
        ok = ok && w != NULL && w->job_id == (uint32_t)(i == 1 ? 77 : i);
    }
    check("every record is found again by its key with its own data (no collision / aliasing)", ok);
    key_of(k, 100001);
    check("the table is bounded by `max`: the 100001st character is refused, nothing breaks", pc_keytab_get_or_create(&t, k, NULL) == NULL && pc_keytab_count(&t) == 100000);
    key_of(k, 500);
    check("remove: the record is gone, the count drops, every other record is still found", pc_keytab_remove(&t, k) && pc_keytab_find(&t, k) == NULL && pc_keytab_count(&t) == 99999 && !pc_keytab_remove(&t, k));
    ok = 1;
    for (i = 1; i <= 100000; i += 331) {
        if (i == 500) continue;
        key_of(k, i);
        ok = ok && pc_keytab_find(&t, k) != NULL;
    }
    check("after a remove (the last record moved into the hole) all others are still reachable", ok);
    ok = 1;
    for (i = 0; i < pc_keytab_count(&t); i++) ok = ok && pc_keytab_at(&t, i) != NULL;
    check("dense access 0..count-1 is complete; out of range is NULL", ok && pc_keytab_at(&t, -1) == NULL && pc_keytab_at(&t, pc_keytab_count(&t)) == NULL);
    pc_keytab_free(&t);
    check("free resets", pc_keytab_count(&t) == 0 && t.data == NULL);

    check("init small table", pc_keytab_init(&t, sizeof(WorkChar), 0, 20, 4));
    for (i = 0; i < 4; i++) { key_of(k, i); (void)pc_keytab_get_or_create(&t, k, NULL); }
    key_of(k, 9);
    check("a full table refuses a NEW key but still finds the existing ones", pc_keytab_get_or_create(&t, k, NULL) == NULL && (key_of(k, 2), pc_keytab_get_or_create(&t, k, &created) != NULL && created == 0));
    pc_keytab_free(&t);
    check("init for failure injection", pc_keytab_init(&t, sizeof(WorkChar), 0, 20, 100));
    t.fail_next = 1;
    key_of(k, 1);
    check("a failed growth reports NULL and leaves the table empty and usable; the next attempt works", pc_keytab_get_or_create(&t, k, NULL) == NULL && pc_keytab_count(&t) == 0 && pc_keytab_get_or_create(&t, k, NULL) != NULL);
    pc_keytab_free(&t);

    /* ================= tabfile v3 ================= */
    path_of(p, sizeof(p), "work_v3.dat");
    check("init", pc_keytab_init(&t, sizeof(WorkChar), 0, 20, 1000000) && pc_keytab_init(&t2, sizeof(WorkChar), 0, 20, 1000000));
    for (i = 0; i < 5000; i++) {
        WorkChar* w;
        key_of(k, i);
        w = (WorkChar*)pc_keytab_get_or_create(&t, k, NULL);
        w->used = 1; w->job_id = (uint32_t)(1000 + i); w->reward = (uint32_t)(i * 3); w->state = (uint8_t)(i % 4);
    }
    check("save 5000 records (atomic: no .tmp left)", pc_tabfile_save(p, 0x4B574341u, 3u, 12345u, &t));
    snprintf(p2, sizeof(p2), "%.390s.tmp", p);
    check("...and no temporary file remains", fopen(p2, "rb") == NULL);
    {
        uint32_t aux = 0, ver = 0;
        int kept = 0;
        check("load v3 into a fresh table: OK, aux, version and record count are right", pc_tabfile_load(p, 0x4B574341u, 3u, 2u, 64, offsetof(WorkChar, used), &t2, &aux, &ver, &kept) == PC_TABFILE_OK && aux == 12345u && ver == 3u && kept == 5000 && pc_keytab_count(&t2) == 5000);
        ok = 1;
        for (i = 0; i < 5000; i += 41) {
            const WorkChar* w;
            key_of(k, i);
            w = (const WorkChar*)pc_keytab_find(&t2, k);
            ok = ok && w != NULL && w->job_id == (uint32_t)(1000 + i) && w->reward == (uint32_t)(i * 3) && w->state == (uint8_t)(i % 4);
        }
        check("every character comes back with its own job state", ok);
    }
    {
        /* a damaged file must never half-replace a good table */
        long sz;
        FILE* f = fopen(p, "rb");
        fseek(f, 0, SEEK_END); sz = ftell(f); fclose(f);
        flip(p, 16 + 56 * 2000 + 7);
        check("a flipped byte in a record: BAD, and the table already loaded is untouched (5000)", pc_tabfile_load(p, 0x4B574341u, 3u, 2u, 64, offsetof(WorkChar, used), &t2, NULL, NULL, NULL) == PC_TABFILE_BAD && pc_keytab_count(&t2) == 5000);
        flip(p, 16 + 56 * 2000 + 7);
        check("the byte flipped back: OK again (the check is exact)", pc_tabfile_load(p, 0x4B574341u, 3u, 2u, 64, offsetof(WorkChar, used), &t2, NULL, NULL, NULL) == PC_TABFILE_OK);
        {
            FILE* g = fopen(p, "r+b");
            (void)sz;
            fseek(g, 0, SEEK_END);
            fclose(g);
        }
        flip(p, 12);
        check("a damaged record count (header) is BAD", pc_tabfile_load(p, 0x4B574341u, 3u, 2u, 64, offsetof(WorkChar, used), &t2, NULL, NULL, NULL) == PC_TABFILE_BAD);
        flip(p, 12);
    }
    path_of(p2, sizeof(p2), "work_trunc.dat");
    {
        FILE* a = fopen(p, "rb");
        FILE* b = fopen(p2, "wb");
        unsigned char buf[4096];
        size_t n = fread(buf, 1, sizeof(buf), a);
        fwrite(buf, 1, n, b);
        fclose(a); fclose(b);
    }
    check("a truncated file is BAD, a missing file is MISSING", pc_tabfile_load(p2, 0x4B574341u, 3u, 2u, 64, offsetof(WorkChar, used), &t2, NULL, NULL, NULL) == PC_TABFILE_BAD &&
          pc_tabfile_load("/nonexistent/dir/none.dat", 0x4B574341u, 3u, 2u, 64, offsetof(WorkChar, used), &t2, NULL, NULL, NULL) == PC_TABFILE_MISSING);
    check("another magic is BAD", pc_tabfile_load(p, 0x12345678u, 3u, 2u, 64, offsetof(WorkChar, used), &t2, NULL, NULL, NULL) == PC_TABFILE_BAD);
    check("a record count above the table's max is refused", (pc_keytab_free(&t2), pc_keytab_init(&t2, sizeof(WorkChar), 0, 20, 100)) && pc_tabfile_load(p, 0x4B574341u, 3u, 2u, 64, offsetof(WorkChar, used), &t2, NULL, NULL, NULL) == PC_TABFILE_BAD);
    check("save to an impossible path fails without touching anything", !pc_tabfile_save("/nonexistent/dir/x.dat", 0x4B574341u, 3u, 1u, &t));
    pc_keytab_free(&t2);

    /* ================= v2 -> v3 migration of an existing work_jobs.dat ================= */
    {
        static WorkChar body[64];
        uint32_t aux = 0, ver = 0;
        int kept = 0;
        memset(body, 0, sizeof(body));
        for (i = 0; i < 64; i += 3) {   /* a sparse old table: 22 used records among the 64 */
            key_of(body[i].key, 900 + i);
            body[i].used = 1; body[i].job_id = (uint32_t)(5000 + i); body[i].state = 1; body[i].reward = 400u + (uint32_t)i;
        }
        path_of(p, sizeof(p), "work_v2.dat");
        write_v2(p, body, 4242u);
        check("init", pc_keytab_init(&t2, sizeof(WorkChar), 0, 20, 100000));
        check("the OLD 64-record file is read: OK, version 2, the 22 used records kept, next job id 4242", pc_tabfile_load(p, 0x4B574341u, 3u, 2u, 64, offsetof(WorkChar, used), &t2, &aux, &ver, &kept) == PC_TABFILE_OK && ver == 2u && aux == 4242u && kept == 22 && pc_keytab_count(&t2) == 22);
        ok = 1;
        for (i = 0; i < 64; i += 3) {
            const WorkChar* w;
            key_of(k, 900 + i);
            w = (const WorkChar*)pc_keytab_find(&t2, k);
            ok = ok && w != NULL && w->job_id == (uint32_t)(5000 + i) && w->reward == 400u + (uint32_t)i;
        }
        check("each old character keeps its job state through the migration (nothing lost, nothing paid twice: ids and tombstones are bytes of the record)", ok);
        check("saving writes version 3; reloading gives the same 22", pc_tabfile_save(p, 0x4B574341u, 3u, aux, &t2) && (pc_keytab_free(&t2), 1) && pc_keytab_init(&t2, sizeof(WorkChar), 0, 20, 100000) &&
              pc_tabfile_load(p, 0x4B574341u, 3u, 2u, 64, offsetof(WorkChar, used), &t2, &aux, &ver, &kept) == PC_TABFILE_OK && ver == 3u && kept == 22 && aux == 4242u);
        check("a v2 file whose body was altered is BAD", (write_v2(p, body, 4242u), flip(p, 20 + 56 * 3 + 5), pc_tabfile_load(p, 0x4B574341u, 3u, 2u, 64, offsetof(WorkChar, used), &t2, NULL, NULL, NULL) == PC_TABFILE_BAD));
        /* the "65th character" regression: the old code refused it; now every character gets a record */
        pc_keytab_free(&t2);
        pc_keytab_init(&t2, sizeof(WorkChar), 0, 20, 100000);
        ok = 1;
        for (i = 0; i < 300; i++) {
            WorkChar* w;
            key_of(k, 3000 + i);
            w = (WorkChar*)pc_keytab_get_or_create(&t2, k, NULL);
            ok = ok && w != NULL;
            if (w) { w->used = 1; w->job_id = (uint32_t)i + 1u; }
        }
        check("REGRESSION (65th character): 300 different guest characters each get their own work record (the old table stopped at 64)", ok && pc_keytab_count(&t2) == 300);
        {
            /* the rekey of a promoted guest: remove + re-add under the new key keeps the data and the old key is gone */
            WorkChar moved, *w;
            key_of(k, 3010);
            w = (WorkChar*)pc_keytab_find(&t2, k);
            moved = *w;
            key_of(moved.key, 7777);
            (void)pc_keytab_remove(&t2, k);
            w = (WorkChar*)pc_keytab_get_or_create(&t2, moved.key, NULL);
            *w = moved;
            key_of(k, 3010);
            check("rekey (guest promoted to a resident): the work record follows the new PersonalID, the old key finds nothing, the count is unchanged", pc_keytab_find(&t2, k) == NULL && (key_of(k, 7777), pc_keytab_find(&t2, k) != NULL) && pc_keytab_count(&t2) == 300);
        }
    }
    pc_keytab_free(&t);
    pc_keytab_free(&t2);

    /* ================= day claims (K.K.) ================= */
    {
        PCDayClaims c;
        uint8_t a[20], b[20];
        const uint32_t sat = (2001u << 9) | (6u << 5) | 14u, sun = sat + 1u;
        check("init (24-byte key limit; 20 ok, 25 refused)", pc_dayclaims_init(&c, 20, 1001) && !pc_dayclaims_init(&c, 25, 10) && (pc_dayclaims_free(&c), pc_dayclaims_init(&c, 20, 1001)));
        key_of(a, 1); key_of(b, 2);
        check("nobody has claimed anything yet", !pc_dayclaims_has(&c, a, sat) && !pc_dayclaims_has(&c, b, sat));
        check("REGRESSION (K.K.): guest A claims its song; guest B can STILL claim (the old shared foreigner bit blocked B)", pc_dayclaims_mark(&c, a, sat) && pc_dayclaims_has(&c, a, sat) && !pc_dayclaims_has(&c, b, sat));
        check("guest B claims too; both are recorded independently", pc_dayclaims_mark(&c, b, sat) && pc_dayclaims_has(&c, a, sat) && pc_dayclaims_has(&c, b, sat) && pc_dayclaims_count(&c) == 2);
        check("a claim is per identity: the SAME guest asking again (a reconnect, another peer id / table slot, a client restart) is still 'already got one'", pc_dayclaims_has(&c, a, sat));
        check("the next day the claim no longer counts", !pc_dayclaims_has(&c, a, sun));
        check("marking on the next day sweeps the old claims (only the new one remains)", pc_dayclaims_mark(&c, a, sun) && pc_dayclaims_count(&c) == 1 && pc_dayclaims_has(&c, a, sun) && !pc_dayclaims_has(&c, b, sun));
        ok = 1;
        for (i = 0; i < 1000; i++) { key_of(a, 5000 + i); ok = ok && pc_dayclaims_mark(&c, a, sun); }
        key_of(a, 6000);
        check("1000 guests can all claim on one day (+ the earlier one: the table holds up to its budget), then the budget refuses a NEW claimant without disturbing the others", ok && pc_dayclaims_count(&c) == 1001 && !pc_dayclaims_mark(&c, a, sun) && (key_of(a, 5500), pc_dayclaims_has(&c, a, sun)));
        pc_dayclaims_free(&c);
        check("uninitialised / freed claims are inert", !pc_dayclaims_has(&c, a, sat) && !pc_dayclaims_mark(&c, a, sat) && pc_dayclaims_count(&c) == 0);
    }

    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail == 0 ? 0 : 1;
}
