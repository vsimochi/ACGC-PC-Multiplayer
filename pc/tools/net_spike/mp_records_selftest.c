/* mp_records_selftest.c - standalone native unit test of pc/src/pc_mp_records.c (D3-4 storage).
 * Usage: mp_records_selftest <scratch_dir>      (every file is created under <scratch_dir>; nothing else is touched)
 * Prints "PASS: ..." / "FAIL: ..." lines and "RESULT passed=N failed=M"; exit code 0 iff failed==0.
 * Built and run by test_d3_records_storage.py (msys2 gcc). */
#include "pc_mp_records.h"

#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#ifdef _WIN32
#include <direct.h>
#define MKDIR(p) _mkdir(p)
#else
#define MKDIR(p) mkdir(p, 0755)
#endif

static int g_pass = 0, g_fail = 0;
static char g_dir[400];

static void check(const char* what, int cond) {
    if (cond) {
        g_pass++;
        printf("PASS: %s\n", what);
    } else {
        g_fail++;
        printf("FAIL: %s\n", what);
    }
}

static int exists(const char* p) {
    struct stat st;
    return stat(p, &st) == 0;
}

static long fsize(const char* p) {
    struct stat st;
    return stat(p, &st) == 0 ? (long)st.st_size : -1L;
}

static uint8_t* slurp(const char* p, size_t* n) {
    FILE* f = fopen(p, "rb");
    uint8_t* b;
    long sz;
    if (!f) {
        *n = 0;
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    b = (uint8_t*)malloc((size_t)sz + 1);
    *n = fread(b, 1, (size_t)sz, f);
    fclose(f);
    return b;
}

static void spit(const char* p, const uint8_t* b, size_t n) {
    FILE* f = fopen(p, "wb");
    if (f) {
        fwrite(b, 1, n, f);
        fclose(f);
    }
}

static void flip(const char* p, size_t off) {
    size_t n;
    uint8_t* b = slurp(p, &n);
    if (b && off < n) {
        b[off] ^= 0x10;
        spit(p, b, n);
    }
    free(b);
}

static void trunc_to(const char* p, size_t n) {
    size_t have;
    uint8_t* b = slurp(p, &have);
    if (b) {
        spit(p, b, n < have ? n : have);
    }
    free(b);
}

static void rmfile(const char* p) {
    remove(p);
}

static int count_prefix(const char* dir, const char* prefix) {
    DIR* d = opendir(dir);
    struct dirent* e;
    int n = 0;
    if (!d) {
        return -1;
    }
    while ((e = readdir(d)) != NULL) {
        if (strncmp(e->d_name, prefix, strlen(prefix)) == 0) {
            n++;
        }
    }
    closedir(d);
    return n;
}

static void clean_dir(const char* dir) {
    DIR* d = opendir(dir);
    struct dirent* e;
    char p[600];
    if (!d) {
        return;
    }
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) {
            continue;
        }
        snprintf(p, sizeof(p), "%s/%s", dir, e->d_name);
        remove(p);
    }
    closedir(d);
}

static void fill(PCMpRecFile* f, uint32_t gen) {
    int i, k;
    memset(f, 0, sizeof(*f));
    f->generation = gen;
    for (i = 0; i < PC_MP_REC_SLOTS; i++) {
        PCMpRecEntry* e = &f->e[i];
        if (i == 2) {
            continue; /* one absent slot */
        }
        e->present = 1;
        for (k = 0; k < PC_MP_REC_PID_SIZE; k++) {
            e->pid[k] = (uint8_t)(0x30 + i * 7 + k);
        }
        e->epoch = 0x1000u + (uint32_t)i * 17u + gen;
        e->rev = gen * 3u + (uint32_t)i;
        e->digest_at_save = 0xABCD0000u + gen * 256u + (uint32_t)i;
        if (i == 1) {
            e->backup_present = 1;
            for (k = 0; k < PC_MP_REC_PRIVATE_SIZE; k++) {
                e->backup[k] = (uint8_t)(k * 31 + gen + 1);
            }
            memcpy(e->backup, e->pid, PC_MP_REC_PID_SIZE);
        }
    }
}

static int same(const PCMpRecFile* a, const PCMpRecFile* b) {
    return memcmp(a, b, sizeof(*a)) == 0;
}

static char P[500], B1[520], B2[520], TMP[520];
static void paths(const char* sub) {
    snprintf(g_dir, sizeof(g_dir), "%s/%s", getenv("MPREC_SCRATCH"), sub);
    MKDIR(g_dir);
    clean_dir(g_dir);
    snprintf(P, sizeof(P), "%s/records.dat", g_dir);
    snprintf(B1, sizeof(B1), "%s.bak1", P);
    snprintf(B2, sizeof(B2), "%s.bak2", P);
    snprintf(TMP, sizeof(TMP), "%s.tmp", P);
}

static int load_gen_counter(void) {
    PCMpRecFile f;
    PCMpRecLoadInfo i;
    PCMpRecLoadMode m = pc_mp_records_load(P, &f, &i);
    return (m == PC_MP_REC_LOAD_OK || m == PC_MP_REC_LOAD_OK_BACKUP) ? (int)f.generation : -1;
}

/* crafts a file buffer with a fixed-up CRC */
static void recrc(uint8_t* b) {
    uint32_t c = pc_mp_records_crc32(b, PC_MP_REC_FILE_SIZE - 4);
    b[PC_MP_REC_FILE_SIZE - 4] = (uint8_t)c;
    b[PC_MP_REC_FILE_SIZE - 3] = (uint8_t)(c >> 8);
    b[PC_MP_REC_FILE_SIZE - 2] = (uint8_t)(c >> 16);
    b[PC_MP_REC_FILE_SIZE - 1] = (uint8_t)(c >> 24);
}

int main(int argc, char** argv) {
    PCMpRecFile a, b, got;
    PCMpRecLoadInfo info;
    PCMpRecLoadMode m;
    uint8_t buf[PC_MP_REC_FILE_SIZE], mod[PC_MP_REC_FILE_SIZE];
    PCMpRecFault flt;
    int r, i;
    char sub[600];

    if (argc < 2) {
        fprintf(stderr, "usage: %s <scratch_dir> | --parse <file>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "--parse") == 0 && argc >= 3) {
        /* cross-implementation check: parse a file written by the Python test's independent writer */
        size_t n;
        uint8_t* x = slurp(argv[2], &n);
        PCMpRecFile pf;
        int pr = x ? pc_mp_records_parse(x, n, &pf) : PC_MP_REC_ERR_IO;
        printf("PARSE rc=%d gen=%u rev1=%u\n", pr, pr == 0 ? (unsigned)pf.generation : 0u, pr == 0 ? (unsigned)pf.e[1].rev : 0u);
        free(x);
        return pr == 0 ? 0 : 1;
    }
    MKDIR(argv[1]);
#ifdef _WIN32
    _putenv_s("MPREC_SCRATCH", argv[1]);
#else
    setenv("MPREC_SCRATCH", argv[1], 1);
#endif
    setvbuf(stdout, NULL, _IONBF, 0);

    check("format constants: entry 9320 B, file 37316 B", PC_MP_REC_ENTRY_SIZE == 9320 && PC_MP_REC_FILE_SIZE == 37316);

    /* ---- T1 missing / new file ---- */
    paths("t1");
    m = pc_mp_records_load(P, &got, &info);
    check("T1 missing file -> LOAD_MISSING, nothing created", m == PC_MP_REC_LOAD_MISSING && !exists(P) && !exists(B1));
    snprintf(sub, sizeof(sub), "%s/nested/deeper/records.dat", g_dir);
    fill(&a, 1);
    r = pc_mp_records_save(sub, &a);
    check("T1 first save creates nested parent directories and the file (exact size)", r == PC_MP_REC_OK && fsize(sub) == PC_MP_REC_FILE_SIZE);
    snprintf(sub, sizeof(sub), "%s/nested/deeper/records.dat.bak1", g_dir);
    check("T1 first save leaves no .bak1 and no tmp", !exists(sub));
    snprintf(sub, sizeof(sub), "%s/nested/deeper/records.dat", g_dir);
    m = pc_mp_records_load(sub, &got, &info);
    check("T1 round trip: LOAD_OK gen 0 and identical content", m == PC_MP_REC_LOAD_OK && info.gen_used == 0 && same(&a, &got));
    clean_dir(g_dir);

    /* ---- T2/T3 update + rotation ---- */
    paths("t2");
    for (i = 1; i <= 4; i++) {
        fill(&a, (uint32_t)i);
        r = pc_mp_records_save(P, &a);
        if (r != PC_MP_REC_OK) {
            check("T2 save ok", 0);
        }
        if (i == 2) {
            check("T2 normal update: main=gen2, .bak1=gen1", load_gen_counter() == 2 && exists(B1) && !exists(B2));
        }
    }
    {
        PCMpRecFile x;
        size_t n;
        uint8_t* bb;
        check("T3 after 4 saves main=gen4", load_gen_counter() == 4);
        bb = slurp(B1, &n);
        check("T3 .bak1 is the gen3 file (valid)", bb && pc_mp_records_parse(bb, n, &x) == PC_MP_REC_OK && x.generation == 3);
        free(bb);
        bb = slurp(B2, &n);
        check("T3 .bak2 is the gen2 file (valid), no .bak3", bb && pc_mp_records_parse(bb, n, &x) == PC_MP_REC_OK && x.generation == 2);
        free(bb);
    }
    check("T3 no tmp file left over and no .bak3 (two generations kept)", !exists(TMP) && count_prefix(g_dir, "records.dat.bak3") == 0);

    /* ---- T4 interrupted writes ---- */
    paths("t4a");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    fill(&b, 2);
    memset(&flt, 0, sizeof(flt));
    flt.fail_write_after = -1;
    flt.crash_partial_tmp = 100;
    pc_mp_records_set_fault(&flt);
    r = pc_mp_records_save(P, &b);
    pc_mp_records_set_fault(NULL);
    check("T4a crash with a PARTIAL tmp: save reports the simulated crash, tmp left behind (100 B)", r == PC_MP_REC_ERR_FAULT_CRASH && fsize(TMP) == 100);
    check("T4a previous generation untouched: loads gen1 from records.dat", load_gen_counter() == 1 && !exists(B1));
    r = pc_mp_records_save(P, &b);
    check("T4a next normal save overwrites the stale tmp and succeeds (gen2, .bak1 gen1)", r == PC_MP_REC_OK && !exists(TMP) && load_gen_counter() == 2 && exists(B1));

    paths("t4b");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    fill(&b, 2);
    memset(&flt, 0, sizeof(flt));
    flt.fail_write_after = -1;
    flt.crash_partial_tmp = -1;
    flt.crash_after_tmp = 1;
    pc_mp_records_set_fault(&flt);
    r = pc_mp_records_save(P, &b);
    pc_mp_records_set_fault(NULL);
    check("T4b complete tmp present but never renamed: main is still gen1 (the uncommitted tmp is ignored by load)",
          r == PC_MP_REC_ERR_FAULT_CRASH && fsize(TMP) == PC_MP_REC_FILE_SIZE && load_gen_counter() == 1);

    paths("t4c");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    fill(&b, 2);
    pc_mp_records_save(P, &b);
    fill(&a, 3);
    memset(&flt, 0, sizeof(flt));
    flt.fail_write_after = -1;
    flt.crash_partial_tmp = -1;
    flt.crash_after_rotate = 1;
    pc_mp_records_set_fault(&flt);
    r = pc_mp_records_save(P, &a);
    pc_mp_records_set_fault(NULL);
    check("T4c crash after rotation, before rename: records.dat is absent", r == PC_MP_REC_ERR_FAULT_CRASH && !exists(P));
    m = pc_mp_records_load(P, &got, &info);
    check("T4c load RECOVERS from .bak1 (gen2 content), not 'missing', not untrusted",
          m == PC_MP_REC_LOAD_OK_BACKUP && info.gen_used == 1 && got.generation == 2);

    paths("t4d");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    fill(&b, 2);
    memset(&flt, 0, sizeof(flt));
    flt.fail_write_after = -1;
    flt.crash_partial_tmp = -1;
    flt.fail_rename = 1;
    pc_mp_records_set_fault(&flt);
    r = pc_mp_records_save(P, &b);
    pc_mp_records_set_fault(NULL);
    m = pc_mp_records_load(P, &got, &info);
    check("T4d rename failure: save reports error, .bak1 restored to records.dat, content is the previous generation (gen1)",
          r == PC_MP_REC_ERR_IO && exists(P) && m == PC_MP_REC_LOAD_OK && got.generation == 1 && !exists(TMP));

    paths("t4e");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    fill(&b, 2);
    memset(&flt, 0, sizeof(flt));
    flt.fail_write_after = 5000;
    flt.crash_partial_tmp = -1;
    pc_mp_records_set_fault(&flt);
    r = pc_mp_records_save(P, &b);
    pc_mp_records_set_fault(NULL);
    check("T4e disk-full style write failure: error returned, tmp removed, previous generation intact", r == PC_MP_REC_ERR_IO && !exists(TMP) && load_gen_counter() == 1 && !exists(B1));

    /* ---- T5 truncated ---- */
    paths("t5");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    fill(&b, 2);
    pc_mp_records_save(P, &b);
    trunc_to(P, PC_MP_REC_FILE_SIZE / 2);
    m = pc_mp_records_load(P, &got, &info);
    check("T5 truncated main -> .bak1 used (gen1), error classified TRUNCATED",
          m == PC_MP_REC_LOAD_OK_BACKUP && info.gen_used == 1 && got.generation == 1 && info.err[0] == PC_MP_REC_ERR_TRUNCATED);
    check("T5 the truncated file was moved aside (never deleted), original path free", !exists(P) && count_prefix(g_dir, "records.dat.corrupt-") == 1 && info.moved_aside == 1);
    {
        uint8_t tiny[10] = { 'A', 'C' };
        spit(P, tiny, sizeof(tiny));
        m = pc_mp_records_load(P, &got, &info);
        check("T5 10-byte file: TRUNCATED, recovered from .bak1", m == PC_MP_REC_LOAD_OK_BACKUP && info.err[0] == PC_MP_REC_ERR_TRUNCATED);
    }

    /* ---- T6 bit flips ---- */
    {
        size_t offs[6] = { 3, 10, 40, PC_MP_REC_HEADER_SIZE + 9320 + 100, PC_MP_REC_HEADER_SIZE + 40 + 9320 + 500, PC_MP_REC_FILE_SIZE - 2 };
        const char* names[6] = { "magic", "version", "entry0 pid", "entry1 (backup blob) payload", "entry1 backup payload deep", "crc trailer" };
        int k;
        for (k = 0; k < 6; k++) {
            char what[200];
            paths("t6");
            fill(&a, 1);
            pc_mp_records_save(P, &a);
            fill(&b, 2);
            pc_mp_records_save(P, &b);
            flip(P, offs[k]);
            m = pc_mp_records_load(P, &got, &info);
            snprintf(what, sizeof(what), "T6 bit flip in %s: main rejected (err %d), .bak1 used (gen1), flipped file preserved", names[k], info.err[0]);
            check(what, m == PC_MP_REC_LOAD_OK_BACKUP && got.generation == 1 && info.err[0] > 0 && info.moved_aside == 1);
        }
        paths("t6b");
        fill(&a, 1);
        pc_mp_records_save(P, &a);
        flip(P, 100);
        {
            size_t n;
            uint8_t* x = slurp(P, &n);
            check("T6 flip in the middle of entry 0 -> CRC error code", pc_mp_records_parse(x, n, &got) == PC_MP_REC_ERR_CRC);
            free(x);
        }
    }

    /* ---- T7 invalid shapes (valid CRC, bad content) ---- */
    paths("t7");
    fill(&a, 1);
    pc_mp_records_serialize(&a, buf, sizeof(buf));
    check("T7 baseline buffer parses", pc_mp_records_parse(buf, sizeof(buf), &got) == PC_MP_REC_OK && same(&a, &got));
    check("T7 length+1 -> SHAPE", ({ uint8_t t[PC_MP_REC_FILE_SIZE + 1]; memcpy(t, buf, sizeof(buf)); t[sizeof(buf)] = 0; pc_mp_records_parse(t, sizeof(t), &got) == PC_MP_REC_ERR_SHAPE; }));
    check("T7 length-1 -> TRUNCATED", pc_mp_records_parse(buf, sizeof(buf) - 1, &got) == PC_MP_REC_ERR_TRUNCATED);
    check("T7 empty -> TRUNCATED", pc_mp_records_parse(buf, 0, &got) == PC_MP_REC_ERR_TRUNCATED);
#define MUT(desc, expr_stmt, code) do { memcpy(mod, buf, sizeof(buf)); expr_stmt; recrc(mod); check(desc, pc_mp_records_parse(mod, sizeof(mod), &got) == (code)); } while (0)
    MUT("T7 slot_count 5 -> SHAPE", mod[16] = 5, PC_MP_REC_ERR_SHAPE);
    MUT("T7 slot_count 0 -> SHAPE", mod[16] = 0, PC_MP_REC_ERR_SHAPE);
    MUT("T7 entry_size wrong -> SHAPE", mod[20] ^= 1, PC_MP_REC_ERR_SHAPE);
    MUT("T7 header flags non-zero -> SHAPE", mod[12] = 1, PC_MP_REC_ERR_SHAPE);
    MUT("T7 header reserved non-zero -> SHAPE", mod[28] = 1, PC_MP_REC_ERR_SHAPE);
    MUT("T7 present=2 -> FIELD", mod[PC_MP_REC_HEADER_SIZE] = 2, PC_MP_REC_ERR_FIELD);
    MUT("T7 backup_present=7 -> FIELD", mod[PC_MP_REC_HEADER_SIZE + 1] = 7, PC_MP_REC_ERR_FIELD);
    MUT("T7 entry reserved non-zero -> FIELD", mod[PC_MP_REC_HEADER_SIZE + 2] = 1, PC_MP_REC_ERR_FIELD);
    MUT("T7 absent entry (slot 2) with stray data -> FIELD", mod[PC_MP_REC_HEADER_SIZE + 2 * 9320 + 30] = 9, PC_MP_REC_ERR_FIELD);
    MUT("T7 present entry with all-zero PersonalID (bad identity) -> FIELD", memset(mod + PC_MP_REC_HEADER_SIZE + 4, 0, PC_MP_REC_PID_SIZE), PC_MP_REC_ERR_FIELD);
    MUT("T7 epoch 0 -> FIELD", memset(mod + PC_MP_REC_HEADER_SIZE + 24, 0, 4), PC_MP_REC_ERR_FIELD);
    MUT("T7 rev 0x80000000 (out of range) -> FIELD", (mod[PC_MP_REC_HEADER_SIZE + 31] = 0x80), PC_MP_REC_ERR_FIELD);
    MUT("T7 backup crc wrong (file crc fixed up) -> BACKUP_CRC", mod[PC_MP_REC_HEADER_SIZE + 9320 + 36] ^= 1, PC_MP_REC_ERR_BACKUP_CRC);
    MUT("T7 backup payload altered (file crc fixed up, backup crc stale) -> BACKUP_CRC", mod[PC_MP_REC_HEADER_SIZE + 9320 + 40 + 5000] ^= 1, PC_MP_REC_ERR_BACKUP_CRC);
    MUT("T7 backup_present=0 but payload present -> FIELD", mod[PC_MP_REC_HEADER_SIZE + 9320 + 1] = 0, PC_MP_REC_ERR_FIELD);
    MUT("T7 backup blob with a zero embedded pid -> FIELD", ({ uint32_t c; memset(mod + PC_MP_REC_HEADER_SIZE + 9320 + 40, 0, PC_MP_REC_PID_SIZE); c = pc_mp_records_crc32(mod + PC_MP_REC_HEADER_SIZE + 9320 + 40, PC_MP_REC_PRIVATE_SIZE); mod[PC_MP_REC_HEADER_SIZE + 9320 + 36] = (uint8_t)c; mod[PC_MP_REC_HEADER_SIZE + 9320 + 37] = (uint8_t)(c >> 8); mod[PC_MP_REC_HEADER_SIZE + 9320 + 38] = (uint8_t)(c >> 16); mod[PC_MP_REC_HEADER_SIZE + 9320 + 39] = (uint8_t)(c >> 24); }), PC_MP_REC_ERR_FIELD);
    check("T7 bad magic -> MAGIC", ({ memcpy(mod, buf, sizeof(buf)); mod[0] = 'X'; recrc(mod); pc_mp_records_parse(mod, sizeof(mod), &got) == PC_MP_REC_ERR_MAGIC; }));
    check("T7 serialize refuses a too-small output buffer", pc_mp_records_serialize(&a, mod, 100) == PC_MP_REC_ERR_SHAPE);

    /* ---- T8 versions ---- */
    paths("t8");
    fill(&a, 1);
    pc_mp_records_serialize(&a, buf, sizeof(buf));
    memcpy(mod, buf, sizeof(buf));
    mod[8] = 0;
    recrc(mod);
    check("T8 OLDER version 0 (valid otherwise) is REFUSED (documented: no older format ever shipped)", pc_mp_records_parse(mod, sizeof(mod), &got) == PC_MP_REC_ERR_VERSION_OLD);
    memcpy(mod, buf, sizeof(buf));
    mod[8] = 2;
    recrc(mod);
    check("T8 FUTURE version 2 is REFUSED, never parsed", pc_mp_records_parse(mod, sizeof(mod), &got) == PC_MP_REC_ERR_VERSION_FUTURE);
    memcpy(mod, buf, sizeof(buf));
    mod[8] = 9;
    check("T8 future version with a stale CRC is still classified FUTURE (version is checked before CRC)", pc_mp_records_parse(mod, sizeof(mod), &got) == PC_MP_REC_ERR_VERSION_FUTURE);
    /* a future-version main with a good .bak1: recovered from the backup, future file preserved intact */
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    fill(&b, 2);
    pc_mp_records_save(P, &b);
    {
        size_t n;
        uint8_t* x = slurp(P, &n);
        x[8] = 2;
        recrc(x);
        spit(P, x, n);
        free(x);
    }
    m = pc_mp_records_load(P, &got, &info);
    check("T8 future-version main + good .bak1 -> OK_BACKUP gen1, future file preserved aside", m == PC_MP_REC_LOAD_OK_BACKUP && got.generation == 1 && info.err[0] == PC_MP_REC_ERR_VERSION_FUTURE && count_prefix(g_dir, "records.dat.corrupt-") == 1);

    /* ---- T9 all generations corrupt -> UNTRUSTED ---- */
    paths("t9");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    fill(&a, 2);
    pc_mp_records_save(P, &a);
    fill(&a, 3);
    pc_mp_records_save(P, &a);
    flip(P, 200);
    flip(B1, 300);
    trunc_to(B2, 1000);
    m = pc_mp_records_load(P, &got, &info);
    check("T9 all three generations unreadable -> LOAD_UNTRUSTED (NOT missing)", m == PC_MP_REC_LOAD_UNTRUSTED && info.err[0] > 0 && info.err[1] > 0 && info.err[2] > 0);
    check("T9 all three bad files preserved aside (3 corrupt-* files), none deleted, none left in place", info.moved_aside == 3 && count_prefix(g_dir, "records.dat.corrupt-") + count_prefix(g_dir, "records.dat.bak1.corrupt-") + count_prefix(g_dir, "records.dat.bak2.corrupt-") == 3 && !exists(P) && !exists(B1) && !exists(B2));
    check("T9 UNTRUSTED output record is empty (caller must apply the no-migration policy)", ({ PCMpRecFile z; memset(&z, 0, sizeof(z)); same(&z, &got); }));
    fill(&a, 9);
    r = pc_mp_records_save(P, &a);
    check("T9 a later save writes a fresh valid file and the preserved corrupt files remain", r == PC_MP_REC_OK && load_gen_counter() == 9 && count_prefix(g_dir, "records.dat.corrupt-") + count_prefix(g_dir, "records.dat.bak1.corrupt-") + count_prefix(g_dir, "records.dat.bak2.corrupt-") == 3);
    paths("t9b");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    trunc_to(P, 7);
    m = pc_mp_records_load(P, &got, &info);
    check("T9 single bad file with no backup at all -> UNTRUSTED (not silently empty)", m == PC_MP_REC_LOAD_UNTRUSTED && count_prefix(g_dir, "records.dat.corrupt-") == 1);

    /* ---- T10 backup blob round trip ---- */
    paths("t10");
    fill(&a, 5);
    r = pc_mp_records_save(P, &a);
    m = pc_mp_records_load(P, &got, &info);
    check("T10 pre-migration backup (0x2440 B + own checksum) round-trips byte-exact", r == PC_MP_REC_OK && m == PC_MP_REC_LOAD_OK && got.e[1].backup_present && memcmp(got.e[1].backup, a.e[1].backup, PC_MP_REC_PRIVATE_SIZE) == 0 && got.e[0].backup_present == 0 && got.e[2].present == 0);

    /* ---- T11 bit rot of main between load and save must not displace a good generation ---- */
    paths("t11");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    fill(&a, 2);
    pc_mp_records_save(P, &a);
    fill(&a, 3);
    pc_mp_records_save(P, &a);
    flip(P, 400);
    fill(&a, 4);
    r = pc_mp_records_save(P, &a);
    {
        PCMpRecFile x;
        size_t n;
        uint8_t* bb = slurp(B1, &n);
        check("T11 save over an unreadable current file: new main=gen4, .bak1 is STILL the good gen2, bad file preserved",
              r == PC_MP_REC_OK && load_gen_counter() == 4 && bb && pc_mp_records_parse(bb, n, &x) == PC_MP_REC_OK && x.generation == 2 &&
                  count_prefix(g_dir, "records.dat.corrupt-") == 1);
        free(bb);
    }

    /* ---- T12 only a .bak2 exists ---- */
    paths("t12");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    fill(&a, 2);
    pc_mp_records_save(P, &a);
    fill(&a, 3);
    pc_mp_records_save(P, &a);
    rmfile(P);
    rmfile(B1);
    m = pc_mp_records_load(P, &got, &info);
    check("T12 only .bak2 left: OK_BACKUP gen_used 2", m == PC_MP_REC_LOAD_OK_BACKUP && info.gen_used == 2 && got.generation == 1);

    /* ---- T13 UNTRUSTED is sticky across restarts until deliberately reset ---- */
    paths("t13");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    fill(&a, 2);
    pc_mp_records_save(P, &a);
    flip(P, 300);
    flip(B1, 300);
    m = pc_mp_records_load(P, &got, &info);
    check("T13 all generations corrupt -> UNTRUSTED (first start)", m == PC_MP_REC_LOAD_UNTRUSTED && info.moved_aside == 2);
    m = pc_mp_records_load(P, &got, &info);
    check("T13 restart WITHOUT any save in between: no generation exists but preserved *.corrupt-* files do -> still UNTRUSTED, NOT missing",
          m == PC_MP_REC_LOAD_UNTRUSTED && !exists(P) && info.first_corrupt_path[0] != 0);
    m = pc_mp_records_load(P, &got, &info);
    check("T13 and again on a third start (sticky until a valid file is written or the operator resets)", m == PC_MP_REC_LOAD_UNTRUSTED);
    {
        DIR* d = opendir(g_dir);
        struct dirent* e;
        char q[700];
        while (d && (e = readdir(d)) != NULL) {
            if (strstr(e->d_name, ".corrupt-")) {
                snprintf(q, sizeof(q), "%s/%s", g_dir, e->d_name);
                remove(q);
            }
        }
        if (d) {
            closedir(d);
        }
    }
    m = pc_mp_records_load(P, &got, &info);
    check("T13 deliberate operator reset (records.dat, .bak and *.corrupt-* all removed) -> MISSING", m == PC_MP_REC_LOAD_MISSING);
    paths("t13b");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    flip(P, 300);
    m = pc_mp_records_load(P, &got, &info);
    fill(&b, 7);
    r = pc_mp_records_save(P, &b);
    m = pc_mp_records_load(P, &got, &info);
    check("T13 after UNTRUSTED a valid save makes the next start OK (the preserved corrupt file no longer matters)",
          r == PC_MP_REC_OK && m == PC_MP_REC_LOAD_OK && got.generation == 7);
    paths("t13c");
    fill(&a, 1);
    pc_mp_records_save(P, &a);
    check("T13 sync_file on an existing file succeeds, on a missing file fails", pc_mp_records_sync_file(P) == 0 && pc_mp_records_sync_file(TMP) != 0);

    printf("RESULT passed=%d failed=%d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
