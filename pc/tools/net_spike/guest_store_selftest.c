/* guest_store_selftest.c - native unit test of pc/src/pc_mp_guest_store.c (capacity phase 3): the per-guest store that replaced the fixed 8-entry guests.dat, its format, atomic
 * writes, corruption handling, retirement, and the migration of an existing guests.dat. No game, no sockets.
 * Usage: guest_store_selftest <scratch_dir>   (every file lives under <scratch_dir>; nothing else is touched). Prints PASS:/FAIL: and "RESULT passed=N failed=M". */
#include "pc_mp_guest_store.h"
#include "pc_mp_guests.h"
#include "pc_mp_membership.h"
#include "pc_mp_records.h"

#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#ifdef _WIN32
#include <direct.h>
#define MKDIR(p) _mkdir(p)
#else
#include <unistd.h>
#define MKDIR(p) mkdir(p, 0755)
#endif

static int s_pass, s_fail;
static void check(const char* what, int ok) {
    printf("%s: %s\n", ok ? "PASS" : "FAIL", what);
    fflush(stdout);
    if (ok) s_pass++; else s_fail++;
}

static char s_root[300];

static void put_path(char* out, size_t cap, const char* sub) {
    snprintf(out, cap, "%s/%s", s_root, sub);
}

static int exists(const char* p) {
    struct stat st;
    return stat(p, &st) == 0;
}

/* count directory entries whose name ends with `suffix` */
static int count_suffix(const char* dir, const char* suffix) {
    DIR* d = opendir(dir);
    struct dirent* e;
    int n = 0;
    const size_t sl = strlen(suffix);
    if (!d) return 0;
    while ((e = readdir(d)) != NULL) {
        const size_t l = strlen(e->d_name);
        if (l >= sl && strcmp(e->d_name + l - sl, suffix) == 0) n++;
    }
    closedir(d);
    return n;
}

/* count directory entries whose name contains `needle` ("" = all) */
static int count_names(const char* dir, const char* needle) {
    DIR* d = opendir(dir);
    struct dirent* e;
    int n = 0;
    if (!d) return 0;
    while ((e = readdir(d)) != NULL) {
        if (e->d_name[0] == '.') continue;
        if (strstr(e->d_name, needle) != NULL) n++;
    }
    closedir(d);
    return n;
}

static void rm_tree(const char* dir) {
    DIR* d = opendir(dir);
    struct dirent* e;
    char p[512];
    if (!d) return;
    while ((e = readdir(d)) != NULL) {
        if (e->d_name[0] == '.') continue;
        snprintf(p, sizeof(p), "%s/%s", dir, e->d_name);
        remove(p);
    }
    closedir(d);
    rmdir(dir);
}

/* a deterministic test guest: key n, its own token, town (1) */
static void make_entry(PCMpGuestEntry* e, int n) {
    int i;
    memset(e, 0, sizeof(*e));
    e->present = 1;
    e->confirmed = (uint8_t)(n & 1);
    for (i = 0; i < 8; i++) e->pid[i] = (uint8_t)('A' + ((n + i) % 26)); /* player name */
    for (i = 8; i < 16; i++) e->pid[i] = (uint8_t)('a' + ((n * 3 + i) % 26)); /* home town */
    e->pid[16] = (uint8_t)(n >> 8);
    e->pid[17] = (uint8_t)n;
    e->pid[18] = 0x12;
    e->pid[19] = (uint8_t)(0x30 + (n & 0x0F));
    for (i = 0; i < PC_MP_GUEST_TOKEN_SIZE; i++) e->token[i] = (uint8_t)(0x40 + i);
    e->token[8] = (uint8_t)((n + 1) & 0xFF); /* unique per n (n < 65535) */
    e->token[9] = (uint8_t)(((n + 1) >> 8) & 0xFF);
    e->epoch = 1000u + (uint32_t)n;
    e->rev = (uint32_t)(n % 5);
    e->age = (uint32_t)n + 1u;
    memcpy(e->town_land_name, "Hosttown", 8);
    e->town_land_id = 0x0777;
    e->town_terrain_hash = 0xABCD0001u;
    memcpy(e->record, e->pid, PC_MP_GUEST_PID_SIZE);
    e->record[PC_MP_GUEST_EXISTS_OFF] = 1;
    for (i = 0x40; i < 0x400; i++) e->record[i] = (uint8_t)(i * 31 + n);
}

static int s_idn;
static int seq_id_source(uint8_t out[PC_MP_GS_ID_SIZE]) {
    int i;
    memset(out, 0, PC_MP_GS_ID_SIZE);
    s_idn++;
    for (i = 0; i < 4; i++) out[12 + i] = (uint8_t)(s_idn >> (24 - 8 * i));
    out[0] = 0xA5;
    return 1;
}
static int zero_id_source(uint8_t out[PC_MP_GS_ID_SIZE]) {
    memset(out, 0, PC_MP_GS_ID_SIZE);
    return 1;
}

static void put_guest(const char* dir, int n, PCMpGsRecord* rec) {
    memset(rec, 0, sizeof(*rec));
    pc_mp_gs_new_id(rec->id);
    rec->generation = 1;
    make_entry(&rec->e, n);
    (void)pc_mp_gs_save(dir, rec);
}

typedef struct Seen {
    int n, backup, order_ok, token_match;
    uint8_t last[PC_MP_GS_ID_SIZE];
    uint8_t first_token[16];
    uint8_t first_id[PC_MP_GS_ID_SIZE];
    uint32_t first_rev, first_gen, last_age;
} Seen;
static int visit(void* ctx, const PCMpGsRecord* r, int bk) {
    Seen* s = (Seen*)ctx;
    if (s->n > 0 && (r->e.age < s->last_age || (r->e.age == s->last_age && memcmp(s->last, r->id, PC_MP_GS_ID_SIZE) >= 0))) s->order_ok = 0;
    s->last_age = r->e.age;
    if (s->n == 0) {
        memcpy(s->first_token, r->e.token, 16);
        memcpy(s->first_id, r->id, PC_MP_GS_ID_SIZE);
        s->first_rev = r->e.rev;
        s->first_gen = r->generation;
    }
    memcpy(s->last, r->id, PC_MP_GS_ID_SIZE);
    s->n++;
    s->backup += bk > 0;
    return 1;
}
static void load(const char* dir, Seen* s, PCMpGsLoadInfo* i) {
    memset(s, 0, sizeof(*s));
    s->order_ok = 1;
    (void)pc_mp_gs_load(dir, visit, s, i);
}

static void flip(const char* path, long off) {
    FILE* f = fopen(path, "r+b");
    int c;
    if (!f) return;
    fseek(f, off, SEEK_SET);
    c = fgetc(f);
    fseek(f, off, SEEK_SET);
    fputc(c ^ 0x5A, f);
    fclose(f);
}

typedef struct Rows { int n; PCMpGuestRow* r; } Rows;
static int rows_fn(void* ctx, int idx, PCMpGuestRow* row) {
    const Rows* r = (const Rows*)ctx;
    if (idx < 0 || idx >= r->n) return 0;
    *row = r->r[idx];
    return 1;
}

typedef struct AgeSeq { int n; uint32_t age[16]; } AgeSeq;
static int age_visit(void* ctx, const PCMpGsRecord* rec, int bk) {
    AgeSeq* a = (AgeSeq*)ctx;
    (void)bk;
    if (a->n < 16) a->age[a->n] = rec->e.age;
    a->n++;
    return 1;
}

int main(int argc, char** argv) {
    static PCMpGsRecord a, b;
    static uint8_t buf[PC_MP_GS_FILE_SIZE + 8];
    char dir[400], p[900], p2[920], leg[400], legdir[400];
    PCMpGsLoadInfo li;
    Seen sn;
    int i, ok;
    if (argc < 2) {
        fprintf(stderr, "usage: guest_store_selftest <scratch_dir>\n");
        return 2;
    }
    snprintf(s_root, sizeof(s_root), "%s", argv[1]);
    MKDIR(s_root);
    pc_mp_gs_set_id_source(seq_id_source);

    /* ---------------- format ---------------- */
    check("file size is 32 + 16 + 9352 + 4 = 9404", PC_MP_GS_FILE_SIZE == 9404 && PC_MP_GUEST_ENTRY_SIZE == 9352);
    memset(&a, 0, sizeof(a));
    seq_id_source(a.id);
    a.generation = 7;
    make_entry(&a.e, 3);
    check("serialize + parse round trip", pc_mp_gs_serialize(&a, buf, sizeof(buf)) == PC_MP_GST_OK && pc_mp_gs_parse(buf, PC_MP_GS_FILE_SIZE, &b) == PC_MP_GST_OK &&
          b.generation == 7 && memcmp(b.id, a.id, 16) == 0 && memcmp(&b.e, &a.e, sizeof(a.e)) == 0);
    {
        uint8_t good[PC_MP_GS_FILE_SIZE];
        pc_mp_gs_serialize(&a, good, sizeof(good));
        memcpy(buf, good, sizeof(good));
        buf[0] ^= 1;
        check("bad magic is refused", pc_mp_gs_parse(buf, PC_MP_GS_FILE_SIZE, &b) == PC_MP_GST_ERR_MAGIC);
        memcpy(buf, good, sizeof(good));
        buf[8] = 2;
        check("an older / newer version is refused, never reinterpreted", pc_mp_gs_parse(buf, PC_MP_GS_FILE_SIZE, &b) == PC_MP_GST_ERR_VERSION_OLD && (buf[8] = 4, pc_mp_gs_parse(buf, PC_MP_GS_FILE_SIZE, &b) == PC_MP_GST_ERR_VERSION_FUTURE));
        memcpy(buf, good, sizeof(good));
        check("truncated / oversized files are refused", pc_mp_gs_parse(good, PC_MP_GS_FILE_SIZE - 1, &b) == PC_MP_GST_ERR_TRUNCATED && pc_mp_gs_parse(good, 10, &b) == PC_MP_GST_ERR_TRUNCATED &&
              (buf[PC_MP_GS_FILE_SIZE] = 0, pc_mp_gs_parse(buf, PC_MP_GS_FILE_SIZE + 1, &b) == PC_MP_GST_ERR_SHAPE));
        memcpy(buf, good, sizeof(good));
        buf[200] ^= 1;
        check("a flipped byte fails the CRC", pc_mp_gs_parse(buf, PC_MP_GS_FILE_SIZE, &b) == PC_MP_GST_ERR_CRC);
        memcpy(buf, good, sizeof(good));
        memset(buf + 32, 0, 16);
        {
            uint32_t crc = pc_mp_records_crc32(buf, PC_MP_GS_FILE_SIZE - 4);
            buf[PC_MP_GS_FILE_SIZE - 4] = (uint8_t)crc;
            buf[PC_MP_GS_FILE_SIZE - 3] = (uint8_t)(crc >> 8);
            buf[PC_MP_GS_FILE_SIZE - 2] = (uint8_t)(crc >> 16);
            buf[PC_MP_GS_FILE_SIZE - 1] = (uint8_t)(crc >> 24);
        }
        check("an all-zero id (with a valid CRC) is refused", pc_mp_gs_parse(buf, PC_MP_GS_FILE_SIZE, &b) == PC_MP_GST_ERR_FIELD);
    }
    {
        PCMpGsRecord e = a;
        e.e.present = 0;
        check("an absent entry cannot be serialized as a guest", pc_mp_gs_serialize(&e, buf, sizeof(buf)) == PC_MP_GST_ERR_SHAPE);
    }
    {
        PCMpGuestEntry x = a.e, y;
        const uint32_t d0 = pc_mp_gs_entry_digest(&x);
        y = x;
        y.epoch += 5;
        check("digest: an epoch re-roll alone does not change it", pc_mp_gs_entry_digest(&y) == d0);
        y = x;
        y.rev++;
        ok = pc_mp_gs_entry_digest(&y) != d0;
        y = x;
        y.confirmed ^= 1;
        ok = ok && pc_mp_gs_entry_digest(&y) != d0;
        y = x;
        y.token[3] ^= 1;
        ok = ok && pc_mp_gs_entry_digest(&y) != d0;
        y = x;
        y.record[0x200] ^= 1;
        ok = ok && pc_mp_gs_entry_digest(&y) != d0;
        y = x;
        y.age++;
        ok = ok && pc_mp_gs_entry_digest(&y) != d0;
        check("digest: rev / confirmed / token / record / age each change it", ok);
    }

    /* ---------------- ids and paths ---------------- */
    {
        uint8_t id[16], id2[16];
        char hex[33];
        for (i = 0; i < 16; i++) id[i] = (uint8_t)(i * 17 + 3);
        pc_mp_gs_id_hex(id, hex);
        check("id hex / parse round trip, lower-case 32 digits", strlen(hex) == 32 && pc_mp_gs_id_parse(hex, id2) && memcmp(id, id2, 16) == 0);
        check("id parse refuses upper case, short and non-hex", !pc_mp_gs_id_parse("ABCDEF0123456789ABCDEF0123456789", id2) && !pc_mp_gs_id_parse("abc", id2) && !pc_mp_gs_id_parse("zz23456789abcdef0123456789abcdef", id2));
        pc_mp_gs_path("d", id, 0, p, sizeof(p));
        pc_mp_gs_path("d", id, 2, p2, sizeof(p2));
        check("paths: <dir>/<hex>.gst and .bakN", strcmp(p + 2 + 32, ".gst") == 0 && strcmp(p2 + 2 + 32, ".gst.bak2") == 0);
        pc_mp_gs_dir_for_legacy("save/mp/guests.dat", p, sizeof(p));
        pc_mp_gs_dir_for_legacy("servers/x/citizens/guests.dat", p2, sizeof(p2));
        check("the store dir of a guests.dat path: <x>/guests", strcmp(p, "save/mp/guests") == 0 && strcmp(p2, "servers/x/citizens/guests") == 0);
        pc_mp_gs_set_id_source(zero_id_source);
        check("an all-zero id from the source is refused (no randomness)", !pc_mp_gs_new_id(id));
        pc_mp_gs_set_id_source(seq_id_source);
    }

    /* ---------------- save / load / rotation ---------------- */
    put_path(dir, sizeof(dir), "store1");
    rm_tree(dir);
    load(dir, &sn, &li);
    check("a missing directory is a first run: nothing visited, not untrusted", li.dir_missing && sn.n == 0 && !li.untrusted);
    put_guest(dir, 1, &a);
    load(dir, &sn, &li);
    check("one guest saved and loaded back (token, rev, generation)", sn.n == 1 && li.loaded == 1 && !li.untrusted && memcmp(sn.first_token, a.e.token, 16) == 0 && sn.first_gen == 1 && sn.first_rev == a.e.rev);
    a.generation = 2;
    a.e.rev = 9;
    ok = pc_mp_gs_save(dir, &a) == PC_MP_GST_OK;
    pc_mp_gs_path(dir, a.id, 1, p, sizeof(p));
    check("a second write rotates the first into .bak1", ok && exists(p));
    a.generation = 3;
    a.e.rev = 10;
    ok = pc_mp_gs_save(dir, &a) == PC_MP_GST_OK;
    pc_mp_gs_path(dir, a.id, 2, p, sizeof(p));
    load(dir, &sn, &li);
    check("a third write keeps .bak1 and .bak2; the newest generation is loaded", ok && exists(p) && sn.n == 1 && sn.first_rev == 10 && sn.first_gen == 3 && sn.backup == 0);
    check("no tmp file is left behind", count_names(dir, ".tmp") == 0);

    /* ---------------- far beyond the old 8-entry table ---------------- */
    put_path(dir, sizeof(dir), "store2");
    rm_tree(dir);
    {
        clock_t t0 = clock();
        for (i = 0; i < 1000; i++) put_guest(dir, i, &b);
        printf("INFO: 1000 guests written in %.2f s\n", (double)(clock() - t0) / CLOCKS_PER_SEC);
        t0 = clock();
        load(dir, &sn, &li);
        printf("INFO: 1000 guests loaded in %.2f s\n", (double)(clock() - t0) / CLOCKS_PER_SEC);
    }
    check("1000 guests (125x the old table) load back, once each, oldest first (mint order)", sn.n == 1000 && li.loaded == 1000 && li.files == 1000 && sn.order_ok && !li.untrusted && li.duplicates == 0 && sn.backup == 0);
    check("each guest is its own 9404-byte file", count_names(dir, ".gst") == 1000);
    {
        struct stat st;
        pc_mp_gs_path(dir, b.id, 0, p, sizeof(p));
        check("the file size on disk is exactly PC_MP_GS_FILE_SIZE", stat(p, &st) == 0 && (size_t)st.st_size == PC_MP_GS_FILE_SIZE);
    }
    /* changing ONE guest rewrites only that file */
    {
        struct stat st1, st2;
        char q[900];
        pc_mp_gs_path(dir, a.id, 0, q, sizeof(q));
        pc_mp_gs_path(dir, b.id, 0, p, sizeof(p));
        stat(p, &st1);
        b.generation++;
        b.e.rev += 3;
        ok = pc_mp_gs_save(dir, &b) == PC_MP_GST_OK;
        stat(p, &st2);
        check("writing one guest touches only that guest's files (exactly 1 .bak1 appears, still 1000 .gst)", ok && count_suffix(dir, ".gst.bak1") == 1 && count_suffix(dir, ".gst") == 1000);
        (void)st1;
        (void)st2;
    }

    /* ---------------- corruption ---------------- */
    put_path(dir, sizeof(dir), "store3");
    rm_tree(dir);
    {
        static PCMpGsRecord g[5];
        for (i = 0; i < 5; i++) {
            put_guest(dir, 100 + i, &g[i]);
            g[i].generation = 2;
            g[i].e.rev = 50 + (uint32_t)i;
            pc_mp_gs_save(dir, &g[i]); /* now every guest has a .bak1 */
        }
        /* g[0]: flip a byte of the primary -> recovered from .bak1 */
        pc_mp_gs_path(dir, g[0].id, 0, p, sizeof(p));
        flip(p, 5000);
        load(dir, &sn, &li);
        check("a corrupt primary is recovered from .bak1 (older generation), the bad file preserved as *.corrupt-*, NOT untrusted",
              sn.n == 5 && li.from_backup == 1 && sn.backup == 1 && li.moved_aside == 1 && !li.untrusted && count_names(dir, ".corrupt-") == 1 && li.unreadable == 0);
        /* the next load: the primary is gone (moved aside), .bak1 still serves it; the preserved corrupt file is not 'stray' because the guest is valid */
        load(dir, &sn, &li);
        check("loading again is stable: still 5 guests, the preserved corrupt file of a VALID guest is not an alarm", sn.n == 5 && !li.untrusted && li.stray_corrupt == 0);
        /* rewriting g[0] restores a primary */
        g[0].generation = 3;
        pc_mp_gs_save(dir, &g[0]);
        pc_mp_gs_path(dir, g[0].id, 0, p, sizeof(p));
        check("a rewrite after recovery restores the primary file", exists(p));
        /* g[1]: destroy primary AND both backups */
        for (i = 0; i < 3; i++) {
            pc_mp_gs_path(dir, g[1].id, i, p, sizeof(p));
            if (exists(p)) flip(p, 100);
        }
        load(dir, &sn, &li);
        check("a guest with NO readable generation -> UNTRUSTED (its token is unknown), the others still load, files preserved", li.unreadable == 1 && li.untrusted && sn.n == 4 && count_names(dir, ".corrupt-") >= 3);
        load(dir, &sn, &li);
        check("UNTRUSTED persists across loads (stray *.corrupt-* with no valid guest) until the operator removes them deliberately", li.untrusted && li.stray_corrupt >= 1);
        /* operator clears the preserved files of that guest */
        {
            DIR* d = opendir(dir);
            struct dirent* e;
            char hex[33];
            pc_mp_gs_id_hex(g[1].id, hex);
            while (d && (e = readdir(d)) != NULL) {
                if (strncmp(e->d_name, hex, 32) == 0) {
                    snprintf(p, sizeof(p), "%s/%s", dir, e->d_name);
                    remove(p);
                }
            }
            if (d) closedir(d);
        }
        load(dir, &sn, &li);
        check("after the operator removes that guest's files the store is trusted again", !li.untrusted && sn.n == 4);
    }
    /* odd files */
    put_path(dir, sizeof(dir), "store4");
    rm_tree(dir);
    put_guest(dir, 7, &a);
    {
        FILE* f;
        pc_mp_gs_path(dir, a.id, 0, p, sizeof(p));
        snprintf(p2, sizeof(p2), "%s.tmp", p);
        f = fopen(p2, "wb");
        fwrite("garbage", 1, 7, f);
        fclose(f);
        snprintf(p2, sizeof(p2), "%s/notes.txt", dir);
        f = fopen(p2, "wb");
        fwrite("hello", 1, 5, f);
        fclose(f);
        snprintf(p2, sizeof(p2), "%s/%s.gst.bak-20200101-000000", dir, "00000000000000000000000000000000");
        f = fopen(p2, "wb");
        fwrite("operator backup", 1, 15, f);
        fclose(f);
    }
    load(dir, &sn, &li);
    check("stray tmp files, foreign files and operator .bak-<ts> copies are ignored", sn.n == 1 && li.files == 1 && !li.untrusted);
    {
        FILE* f;
        pc_mp_gs_path(dir, a.id, 0, p, sizeof(p));
        f = fopen(p, "wb");
        fclose(f);
        load(dir, &sn, &li);
        check("a zero-length primary with no backup -> that guest is unreadable -> UNTRUSTED", li.unreadable == 1 && li.untrusted && sn.n == 0);
    }
    /* a file copied under another id's name */
    put_path(dir, sizeof(dir), "store5");
    rm_tree(dir);
    {
        static PCMpGsRecord x, y;
        FILE *fi, *fo;
        int c;
        put_guest(dir, 11, &x);
        put_guest(dir, 12, &y);
        pc_mp_gs_path(dir, x.id, 0, p, sizeof(p));
        pc_mp_gs_path(dir, y.id, 0, p2, sizeof(p2));
        fi = fopen(p, "rb");
        fo = fopen(p2, "wb");
        while ((c = fgetc(fi)) != EOF) fputc(c, fo);
        fclose(fi);
        fclose(fo);
        load(dir, &sn, &li);
        check("a guest file whose inner id is not its name is NOT trusted (renamed / copied)", li.unreadable == 1 && li.untrusted && sn.n == 1);
    }
    /* duplicates */
    put_path(dir, sizeof(dir), "store6");
    rm_tree(dir);
    {
        static PCMpGsRecord x, y;
        put_guest(dir, 21, &x);
        memset(&y, 0, sizeof(y));
        pc_mp_gs_new_id(y.id);
        y.generation = 1;
        make_entry(&y.e, 22);
        memcpy(y.e.token, x.e.token, 16); /* same token, different guest */
        pc_mp_gs_save(dir, &y);
        load(dir, &sn, &li);
        check("two guests sharing a token -> UNTRUSTED (inconsistent store)", sn.n == 2 && li.duplicates == 1 && li.untrusted);
        rm_tree(dir);
        put_guest(dir, 21, &x);
        memset(&y, 0, sizeof(y));
        pc_mp_gs_new_id(y.id);
        y.generation = 1;
        y.e = x.e;
        y.e.token[0] ^= 0x20;
        y.e.token[1] ^= 0x01; /* same (town, key), another token */
        pc_mp_gs_save(dir, &y);
        load(dir, &sn, &li);
        check("two guests with the same (town, key) -> UNTRUSTED", sn.n == 2 && li.duplicates == 1 && li.untrusted);
    }

    /* ---------------- crash / failure injection ---------------- */
    put_path(dir, sizeof(dir), "store7");
    rm_tree(dir);
    put_guest(dir, 31, &a);
    a.generation = 2;
    a.e.rev = 77;
    {
        PCMpGuestFault f;
        memset(&f, 0, sizeof(f));
        f.fail_write_after = -1;
        f.crash_partial_tmp = 3000;
        pc_mp_gs_set_fault(&f);
        ok = pc_mp_gs_save(dir, &a) == PC_MP_GST_ERR_FAULT_CRASH;
        pc_mp_gs_set_fault(NULL);
        load(dir, &sn, &li);
        check("crash with a half-written tmp: the previous generation is intact (rev unchanged), nothing is untrusted", ok && sn.n == 1 && sn.first_rev == (uint32_t)(31 % 5) && !li.untrusted);
        memset(&f, 0, sizeof(f));
        f.fail_write_after = 100;
        f.crash_partial_tmp = -1;
        pc_mp_gs_set_fault(&f);
        ok = pc_mp_gs_save(dir, &a) == PC_MP_GST_ERR_IO;
        pc_mp_gs_set_fault(NULL);
        load(dir, &sn, &li);
        check("a failed write leaves the previous generation and no tmp", ok && sn.n == 1 && sn.first_rev == (uint32_t)(31 % 5) && count_names(dir, ".tmp") == 0 && !li.untrusted);
        memset(&f, 0, sizeof(f));
        f.fail_write_after = -1;
        f.crash_partial_tmp = -1;
        f.crash_after_rotate = 1;
        pc_mp_gs_set_fault(&f);
        ok = pc_mp_gs_save(dir, &a) == PC_MP_GST_ERR_FAULT_CRASH;
        pc_mp_gs_set_fault(NULL);
        load(dir, &sn, &li);
        check("crash after the rotation (no primary, a committed tmp): the guest is recovered from .bak1, not lost, not untrusted", ok && sn.n == 1 && li.from_backup == 1 && !li.untrusted && sn.first_rev == (uint32_t)(31 % 5));
        {
            char hex[33];
            pc_mp_gs_id_hex(a.id, hex);
            snprintf(p, sizeof(p), "%s/%s.gst.tmp", dir, hex);
            remove(p);
        }
        memset(&f, 0, sizeof(f));
        f.fail_write_after = -1;
        f.crash_partial_tmp = -1;
        f.fail_rename = 1;
        pc_mp_gs_set_fault(&f);
        ok = pc_mp_gs_save(dir, &a) == PC_MP_GST_ERR_IO;
        pc_mp_gs_set_fault(NULL);
        load(dir, &sn, &li);
        check("a failed final rename restores the previous generation", ok && sn.n == 1 && !li.untrusted && sn.first_rev == (uint32_t)(31 % 5));
    }

    /* ---------------- retire / backup ---------------- */
    put_path(dir, sizeof(dir), "store8");
    rm_tree(dir);
    {
        static PCMpGsRecord x, y;
        char moved[460], bak[460];
        put_guest(dir, 41, &x);
        put_guest(dir, 42, &y);
        x.generation = 2;
        pc_mp_gs_save(dir, &x);
        ok = pc_mp_gs_backup(dir, x.id, bak, sizeof(bak));
        check("backup copies the guest's file to <file>.bak-<ts> and load ignores it", ok && exists(bak) && count_names(dir, ".bak-") == 1);
        ok = pc_mp_gs_retire(dir, x.id, moved, sizeof(moved));
        check("retire moves the file AND its .bak1 aside as *.removed-<ts> (nothing deleted)", ok && moved[0] != 0 && exists(moved) && count_names(dir, ".removed-") == 2);
        load(dir, &sn, &li);
        check("a retired guest is gone from the store; the others stay; *.removed-* is not an alarm", sn.n == 1 && !li.untrusted && li.stray_corrupt == 0 && memcmp(sn.first_id, y.id, 16) == 0);
        check("retiring a guest that has no files is fine", pc_mp_gs_retire(dir, x.id, moved, sizeof(moved)) && moved[0] == 0);
        check("backup of a guest without a file reports failure and leaves nothing", !pc_mp_gs_backup(dir, x.id, bak, sizeof(bak)));
    }

    /* ---------------- migration from guests.dat v2 ---------------- */
    put_path(leg, sizeof(leg), "mig1/citizens/guests.dat");
    pc_mp_gs_dir_for_legacy(leg, legdir, sizeof(legdir));
    {
        static PCMpGuestFile lf;
        static PCMpGuestEntry orig[8];
        PCMpGsMigInfo mi;
        char d1[400];
        memset(&lf, 0, sizeof(lf));
        for (i = 0; i < 8; i++) {
            make_entry(&lf.e[i], 50 + i);
            lf.e[i].confirmed = (uint8_t)(i % 2);
            lf.e[i].rev = (uint32_t)(i * 4);
            orig[i] = lf.e[i];
        }
        lf.generation = 5;
        snprintf(d1, sizeof(d1), "%s/mig1", s_root);
        MKDIR(d1);
        check("setup: a FULL 8-entry guests.dat with a .bak1", pc_mp_guests_save(leg, &lf) == PC_MP_GST_OK && (lf.generation = 6, pc_mp_guests_save(leg, &lf) == PC_MP_GST_OK));
        snprintf(p, sizeof(p), "%s.bak1", leg);
        check("setup: guests.dat and guests.dat.bak1 exist", exists(leg) && exists(p));
        ok = pc_mp_gs_migrate_legacy(leg, legdir, &mi) == PC_MP_GS_MIG_DONE;
        check("migration DONE: 8 entries, 8 written, none skipped", ok && mi.legacy_entries == 8 && mi.written == 8 && mi.skipped_existing == 0 && mi.retired[0] != 0);
        load(legdir, &sn, &li);
        check("the store now holds exactly the 8 legacy guests", sn.n == 8 && !li.untrusted && li.loaded == 8);
        {
            /* every legacy entry must be present byte for byte (the store is in id order, so match by token) */
            typedef struct M { const PCMpGuestEntry* o; int found; } M;
            static M ms[8];
            DIR* d = opendir(legdir);
            struct dirent* e;
            int allok = 1, n_found = 0;
            for (i = 0; i < 8; i++) { ms[i].o = &orig[i]; ms[i].found = 0; }
            while (d && (e = readdir(d)) != NULL) {
                static PCMpGsRecord r;
                FILE* f;
                size_t n;
                uint8_t id[16];
                if (strlen(e->d_name) != 36 || !pc_mp_gs_id_parse(e->d_name, id) || strcmp(e->d_name + 32, ".gst") != 0) continue;
                snprintf(p, sizeof(p), "%s/%s", legdir, e->d_name);
                f = fopen(p, "rb");
                n = fread(buf, 1, sizeof(buf), f);
                fclose(f);
                if (pc_mp_gs_parse(buf, n, &r) != PC_MP_GST_OK) { allok = 0; continue; }
                for (i = 0; i < 8; i++) if (memcmp(ms[i].o->token, r.e.token, 16) == 0) { ms[i].found = memcmp(ms[i].o, &r.e, sizeof(r.e)) == 0; }
            }
            if (d) closedir(d);
            for (i = 0; i < 8; i++) n_found += ms[i].found;
            check("every legacy entry (key, token, confirmed, epoch, rev, age, town, record) survived byte for byte", allok && n_found == 8);
        }
        snprintf(p, sizeof(p), "%s/mig1/citizens", s_root);
        snprintf(p2, sizeof(p2), "%s.bak1", leg);
        check("the legacy guests.dat and its .bak1 were RENAMED to *.v2-migrated-* (kept, not deleted, not loadable any more)", !exists(leg) && !exists(p2) && count_names(p, ".v2-migrated-") == 2 && exists(mi.retired));
        {
            PCMpGuestFile chk;
            PCMpGuestLoadInfo gi;
            check("the v2 loader now sees MISSING (so a later start never re-imports stale data)", pc_mp_guests_load(leg, &chk, &gi) == PC_MP_GST_LOAD_MISSING);
        }
        check("running the migration again is a no-op", pc_mp_gs_migrate_legacy(leg, legdir, &mi) == PC_MP_GS_MIG_NONE);
        /* the 9th guest: past the old boundary */
        put_guest(legdir, 90, &a);
        load(legdir, &sn, &li);
        check("a 9th guest (beyond the old 8-entry table) can be added to the migrated store", sn.n == 9 && !li.untrusted);
    }
    /* crash-resume: some guests were already written when the process died */
    put_path(leg, sizeof(leg), "mig2/guests.dat");
    pc_mp_gs_dir_for_legacy(leg, legdir, sizeof(legdir));
    {
        static PCMpGuestFile lf;
        PCMpGsMigInfo mi;
        char d1[400];
        PCMpGsRecord pre;
        snprintf(d1, sizeof(d1), "%s/mig2", s_root);
        MKDIR(d1);
        memset(&lf, 0, sizeof(lf));
        for (i = 0; i < 6; i++) make_entry(&lf.e[i], 60 + i);
        lf.generation = 1;
        pc_mp_guests_save(leg, &lf);
        for (i = 0; i < 3; i++) { /* three of them are already in the store, one of them with a NEWER rev than the legacy file */
            memset(&pre, 0, sizeof(pre));
            pc_mp_gs_new_id(pre.id);
            pre.generation = 4;
            pre.e = lf.e[i];
            if (i == 0) pre.e.rev += 100;
            pc_mp_gs_save(legdir, &pre);
        }
        ok = pc_mp_gs_migrate_legacy(leg, legdir, &mi) == PC_MP_GS_MIG_DONE;
        load(legdir, &sn, &li);
        check("resume after a crash: 3 skipped (already there), 3 written, 6 guests total, nothing duplicated", ok && mi.skipped_existing == 3 && mi.written == 3 && sn.n == 6 && !li.untrusted && li.duplicates == 0);
        check("an existing store guest is never overwritten by the legacy copy (its newer rev is kept)", sn.n == 6 && count_names(legdir, ".bak1") == 0);
    }
    /* conflict: the store has the same key under another token */
    put_path(leg, sizeof(leg), "mig3/guests.dat");
    pc_mp_gs_dir_for_legacy(leg, legdir, sizeof(legdir));
    {
        static PCMpGuestFile lf;
        PCMpGsMigInfo mi;
        char d1[400];
        PCMpGsRecord pre;
        snprintf(d1, sizeof(d1), "%s/mig3", s_root);
        MKDIR(d1);
        memset(&lf, 0, sizeof(lf));
        make_entry(&lf.e[0], 70);
        pc_mp_guests_save(leg, &lf);
        memset(&pre, 0, sizeof(pre));
        pc_mp_gs_new_id(pre.id);
        pre.generation = 1;
        pre.e = lf.e[0];
        pre.e.token[2] ^= 0x44;
        pc_mp_gs_save(legdir, &pre);
        ok = pc_mp_gs_migrate_legacy(leg, legdir, &mi) == PC_MP_GS_MIG_FAILED;
        check("a same-key / different-token conflict aborts the migration and leaves guests.dat UNTOUCHED", ok && exists(leg) && mi.error[0] != 0);
    }
    /* failing write: legacy untouched, resumable */
    put_path(leg, sizeof(leg), "mig4/guests.dat");
    pc_mp_gs_dir_for_legacy(leg, legdir, sizeof(legdir));
    {
        static PCMpGuestFile lf;
        PCMpGsMigInfo mi;
        PCMpGuestFault f;
        char d1[400];
        snprintf(d1, sizeof(d1), "%s/mig4", s_root);
        MKDIR(d1);
        memset(&lf, 0, sizeof(lf));
        for (i = 0; i < 4; i++) make_entry(&lf.e[i], 80 + i);
        pc_mp_guests_save(leg, &lf);
        memset(&f, 0, sizeof(f));
        f.fail_write_after = 50;
        f.crash_partial_tmp = -1;
        pc_mp_gs_set_fault(&f);
        ok = pc_mp_gs_migrate_legacy(leg, legdir, &mi) == PC_MP_GS_MIG_FAILED;
        pc_mp_gs_set_fault(NULL);
        check("a failing store write aborts the migration; guests.dat is untouched and still loads", ok && exists(leg) && count_names(legdir, ".gst") == 0);
        ok = pc_mp_gs_migrate_legacy(leg, legdir, &mi) == PC_MP_GS_MIG_DONE;
        load(legdir, &sn, &li);
        check("after the fault is gone the same migration succeeds (4 guests)", ok && sn.n == 4 && mi.written == 4);
    }
    /* an unreadable legacy file */
    put_path(leg, sizeof(leg), "mig5/guests.dat");
    pc_mp_gs_dir_for_legacy(leg, legdir, sizeof(legdir));
    {
        PCMpGsMigInfo mi;
        char d1[400];
        FILE* f;
        snprintf(d1, sizeof(d1), "%s/mig5", s_root);
        MKDIR(d1);
        f = fopen(leg, "wb");
        fwrite("not a guests file at all", 1, 24, f);
        fclose(f);
        ok = pc_mp_gs_migrate_legacy(leg, legdir, &mi) == PC_MP_GS_MIG_LEGACY_UNTRUSTED;
        check("an unreadable legacy file is reported UNTRUSTED (never treated as empty), nothing was written to the store", ok && count_names(legdir, ".gst") == 0);
    }
    /* no legacy at all */
    put_path(leg, sizeof(leg), "mig6/guests.dat");
    pc_mp_gs_dir_for_legacy(leg, legdir, sizeof(legdir));
    {
        PCMpGsMigInfo mi;
        check("no legacy file: nothing to migrate", pc_mp_gs_migrate_legacy(leg, legdir, &mi) == PC_MP_GS_MIG_NONE);
    }

    /* ---------------- load order = arrival order, not id order ---------------- */
    put_path(dir, sizeof(dir), "store9");
    rm_tree(dir);
    {
        static PCMpGsRecord r;
        static const uint32_t ages[5] = { 50, 10, 40, 20, 30 };
        AgeSeq as;
        memset(&as, 0, sizeof(as));
        for (i = 0; i < 5; i++) { /* ids ascend with i, ages do not */
            memset(&r, 0, sizeof(r));
            pc_mp_gs_new_id(r.id);
            r.generation = 1;
            make_entry(&r.e, 300 + i);
            r.e.age = ages[i];
            pc_mp_gs_save(dir, &r);
        }
        (void)pc_mp_gs_load(dir, age_visit, &as, &li);
        check("load order follows the guests' mint age (10,20,30,40,50), not the file ids: slot numbers follow arrival order and survive a restart",
              as.n == 5 && as.age[0] == 10 && as.age[1] == 20 && as.age[2] == 30 && as.age[3] == 40 && as.age[4] == 50);
    }

    /* ---------------- membership over a store of any size ---------------- */
    {
        enum { N = 2000 };
        static PCMpGuestRow rr[N];
        static PCMpMembership list[N + 8];
        Rows rows;
        PCMpGuestSource src;
        PCMpTownKey town;
        uint8_t res_pid[4][20];
        const uint8_t res_exists[4] = { 1, 0, 0, 0 };
        PCMpMembership m;
        PCMpAdmitIn ai;
        PCMpAdmitView v;
        PCMpGuestEntry e;
        static PCMpGuestFile fixed;
        int n, k, same = 1;
        for (k = 0; k < N; k++) {
            make_entry(&e, 1000 + k);
            rr[k].present = 1;
            rr[k].confirmed = e.confirmed;
            memcpy(rr[k].pid, e.pid, 20);
            memcpy(rr[k].town_land_name, e.town_land_name, 8);
            rr[k].town_land_id = e.town_land_id;
            rr[k].town_terrain_hash = e.town_terrain_hash;
        }
        rr[500].present = 0; /* a hole: a removed guest */
        rows.n = N;
        rows.r = rr;
        src.fn = rows_fn;
        src.ctx = &rows;
        make_entry(&e, 1000);
        memcpy(town.land_name, e.town_land_name, 8);
        town.land_id = e.town_land_id;
        town.terrain_hash = e.town_terrain_hash;
        memset(res_pid, 0, sizeof(res_pid));
        memset(res_pid[0], 'R', 20);
        check("membership_src: guest 1500 of 2000 is found (slot 1500, confirmed bit kept)", pc_mp_membership_lookup_src(rr[1500].pid, &town, res_pid, res_exists, &src, &m) == PC_MP_MEMBER_GUEST &&
              m.guest_slot == 1500 && m.confirmed == rr[1500].confirmed);
        check("membership_src: a removed guest (hole) and an unknown pid are NONE", pc_mp_membership_lookup_src(rr[500].pid, &town, res_pid, res_exists, &src, &m) == PC_MP_MEMBER_NONE &&
              (res_pid[0][0] = 'R', pc_mp_membership_lookup_src(res_pid[1], &town, res_pid, res_exists, &src, &m) == PC_MP_MEMBER_NONE));
        n = pc_mp_membership_list_src(&town, res_pid, res_exists, &src, list, (int)(sizeof(list) / sizeof(list[0])));
        check("membership_src: the list is 1 resident + 1999 guests (the hole is skipped), guest rows keep their table slot", n == 1 + (N - 1) && list[0].res_index == 0 && list[1].guest_slot == 0 && list[n - 1].guest_slot == N - 1);
        check("membership_src: a small cap truncates like the file form", pc_mp_membership_list_src(&town, res_pid, res_exists, &src, list, 3) == 3);
        /* the file form and the source form agree on the same 8 rows */
        memset(&fixed, 0, sizeof(fixed));
        for (k = 0; k < 8; k++) {
            make_entry(&fixed.e[k], 1000 + k);
            same = same && 1;
        }
        {
            Rows r8;
            PCMpGuestSource s8;
            PCMpMembership a, b;
            int ok8 = 1;
            r8.n = 8;
            r8.r = rr;
            s8.fn = rows_fn;
            s8.ctx = &r8;
            for (k = 0; k < 8; k++) {
                const int ka = pc_mp_membership_lookup(fixed.e[k].pid, &town, res_pid, res_exists, &fixed, &a);
                const int kb = pc_mp_membership_lookup_src(fixed.e[k].pid, &town, res_pid, res_exists, &s8, &b);
                ok8 = ok8 && ka == kb && a.guest_slot == b.guest_slot && a.confirmed == b.confirmed && a.kind == b.kind;
            }
            check("membership: the PCMpGuestFile form and the row-source form give identical answers for the same 8 guests", ok8 && same);
        }
        /* the admission classifier through the source */
        memset(&ai, 0, sizeof(ai));
        memcpy(&ai.town, &town, sizeof(town));
        ai.ext_kind = PC_MP_EXT_GUEST;
        ai.guest_src = &src;
        ai.own_idx = -1;
        memcpy(ai.claim_pid, rr[700].pid, 20);
        memcpy(ai.ext_home_pid, rr[700].pid, 20);
        ai.claim_pid[18] = 0x00; /* the claim is the visitor's own id (not a resident of this town) */
        (void)pc_mp_membership_resolve(&ai, &v);
        check("admission via a 2000-row source: a known guest key resolves to its slot (700)", v.kind == PC_MP_ADMIT_GUEST && v.guest_slot == 700);
        memcpy(ai.ext_home_pid, rr[500].pid, 20);
        (void)pc_mp_membership_resolve(&ai, &v);
        check("admission: the key of a removed guest is a NEW key (slot -1)", v.kind == PC_MP_ADMIT_GUEST && v.guest_slot == -1);
        memcpy(ai.ext_home_pid, rr[1999].pid, 20);
        (void)pc_mp_membership_resolve(&ai, &v);
        check("admission: the LAST of 2000 guests is found", v.guest_slot == 1999);
        ai.guest_src = NULL;
        (void)pc_mp_membership_resolve(&ai, &v);
        check("admission: no guest source (untrusted store) = no guest slot is ever matched", v.guest_slot == -1);
    }

    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail == 0 ? 0 : 1;
}
