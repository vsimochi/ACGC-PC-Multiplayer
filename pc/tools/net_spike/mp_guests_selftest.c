/* mp_guests_selftest.c - standalone native unit test of pc/src/pc_mp_guests.c (guests.dat host table + guest_token.dat client file).
 * Usage: mp_guests_selftest <scratch_dir>      (every file is created under <scratch_dir>; nothing else is touched)
 * Prints "PASS: ..." / "FAIL: ..." lines and "RESULT passed=N failed=M"; exit code 0 iff failed==0.
 * Built and run by test_guest_storage.py (msys2 gcc, with pc_mp_guests.c + pc_mp_records.c for the shared CRC32). */
#include "pc_mp_guests.h"
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

/* names starting with `base` that contain ".corrupt-" (guests.dat.corrupt-*, guests.dat.bak1.corrupt-*, ...) */
static int count_corrupt(const char* dir, const char* base) {
    DIR* d = opendir(dir);
    struct dirent* e;
    int n = 0;
    if (!d) {
        return 0;
    }
    while ((e = readdir(d)) != NULL) {
        if (strncmp(e->d_name, base, strlen(base)) == 0 && strstr(e->d_name, ".corrupt-") != NULL) {
            n++;
        }
    }
    closedir(d);
    return n;
}

static void rm_all(const char* dir) {
    DIR* d = opendir(dir);
    struct dirent* e;
    char p[600];
    if (!d) {
        return;
    }
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") && strcmp(e->d_name, "..")) {
            snprintf(p, sizeof(p), "%s/%s", dir, e->d_name);
            remove(p);
        }
    }
    closedir(d);
}

static void put32(uint8_t* p, uint32_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static PCMpGuestFile* mkfile(unsigned gen, int n) {
    static PCMpGuestFile f;
    int i, k;
    memset(&f, 0, sizeof(f));
    f.generation = gen;
    for (i = 0; i < n; i++) {
        PCMpGuestEntry* e = &f.e[i];
        e->present = 1;
        for (k = 0; k < PC_MP_GUEST_PID_SIZE; k++) {
            e->pid[k] = (uint8_t)(0x30 + i * 7 + k);
        }
        for (k = 0; k < PC_MP_GUEST_TOKEN_SIZE; k++) {
            e->token[k] = (uint8_t)(0xA0 + i * 11 + k);
        }
        e->epoch = 0x1000u + (uint32_t)i;
        e->rev = (uint32_t)i + (uint32_t)gen;
        e->confirmed = (uint8_t)(i & 1);
        e->age = 100u + (uint32_t)i * 3u;
        memcpy(e->town_land_name, "TOWN-A\x00\x00", 8);
        e->town_land_name[5] = (uint8_t)('A' + (i & 1)); /* two host towns alternate */
        e->town_land_id = (uint16_t)(0x2000u + (uint32_t)(i & 1));
        e->town_terrain_hash = 0xC0DE0000u + (uint32_t)(i & 1);
        memset(e->record, (int)(0x40 + i), sizeof(e->record));
        memcpy(e->record, e->pid, PC_MP_GUEST_PID_SIZE); /* the record carries the key */
        e->record[PC_MP_GUEST_EXISTS_OFF] = 1;
    }
    return &f;
}

static int same(const PCMpGuestFile* a, const PCMpGuestFile* b) {
    return a->generation == b->generation && memcmp(a->e, b->e, sizeof(a->e)) == 0;
}

/* re-signs a serialized image after a deliberate edit (file CRC only) */
static void resign(uint8_t* buf) {
    put32(buf + PC_MP_GUEST_FILE_SIZE - 4, pc_mp_records_crc32(buf, PC_MP_GUEST_FILE_SIZE - 4));
}

int main(int argc, char** argv) {
    static uint8_t buf[PC_MP_GUEST_FILE_SIZE], bad[PC_MP_GUEST_FILE_SIZE];
    static PCMpGuestFile out, loaded;
    PCMpGuestLoadInfo info;
    PCMpGuestFault flt;
    char path[500], b1[520], b2[520], tmp[520], sub[500];
    PCMpGuestFile* f;
    int r, i;
    size_t n;

    if (argc < 2) {
        printf("usage: %s <scratch_dir>\n", argv[0]);
        return 2;
    }
    snprintf(g_dir, sizeof(g_dir), "%s", argv[1]);
    MKDIR(g_dir);
    snprintf(sub, sizeof(sub), "%s/mp", g_dir);
    snprintf(path, sizeof(path), "%s/mp/guests.dat", g_dir);
    snprintf(b1, sizeof(b1), "%s.bak1", path);
    snprintf(b2, sizeof(b2), "%s.bak2", path);
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);

    /* ---------- format constants ---------- */
    check("v2 entry size = 9352, file size = 32 + 8 * 9352 + 4", PC_MP_GUEST_ENTRY_SIZE == 9352 && PC_MP_GUEST_FILE_SIZE == 32 + 8 * 9352 + 4 && PC_MP_GUEST_VERSION == 2u);
    check("the path is save/mp/guests.dat (never .gci, never under card_a / card_b)", strcmp(PC_MP_GUESTS_PATH, "save/mp/guests.dat") == 0 &&
          strstr(PC_MP_GUESTS_PATH, "gci") == NULL && strstr(PC_MP_GUESTS_PATH, "card_") == NULL && strcmp(PC_MP_GUEST_TOKEN_PATH, "save/mp/guest_token.dat") == 0);

    /* ---------- Guests G1: the guest NAME rule (shared by the client's early check and the host's authority check) ---------- */
    {
        static const uint8_t ok1[8] = { 'B', 'e', 'l', 'l', 'a', ' ', ' ', ' ' };
        static const uint8_t ok8[8] = { 'A', 'n', 'g', 'e', 'l', 'i', 'c', 'a' };
        static const uint8_t ok1c[8] = { 'Z', ' ', ' ', ' ', ' ', ' ', ' ', ' ' };
        static const uint8_t okhigh[8] = { 222, ' ', ' ', ' ', ' ', ' ', ' ', ' ' };   /* the last printable font code */
        static const uint8_t blank[8] = { ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ' };
        static const uint8_t wide[8] = { 211, 211, 211, 211, 211, 211, 211, 211 };     /* CHAR_SPACE_3 only: still blank */
        static const uint8_t mixed_blank[8] = { 32, 210, 211, 32, 210, 211, 32, 32 };
        static const uint8_t nul_in[8] = { 'A', 'B', 0, ' ', ' ', ' ', ' ', ' ' };
        static const uint8_t nul_first[8] = { 0, 'B', 'C', ' ', ' ', ' ', ' ', ' ' };
        static const uint8_t ctl[8] = { 'A', 127, ' ', ' ', ' ', ' ', ' ', ' ' };
        static const uint8_t tag[8] = { 'A', 128, ' ', ' ', ' ', ' ', ' ', ' ' };
        static const uint8_t nl[8] = { 'A', 205, ' ', ' ', ' ', ' ', ' ', ' ' };
        static const uint8_t unused[8] = { 'A', 223, ' ', ' ', ' ', ' ', ' ', ' ' };
        static const uint8_t ff[8] = { 'A', 255, ' ', ' ', ' ', ' ', ' ', ' ' };
        check("G1 name rule: 'Bella', an 8-character name, a 1-character name and the last printable code 222 are valid",
              pc_mp_guests_name_valid(ok1) == 1 && pc_mp_guests_name_valid(ok8) == 1 && pc_mp_guests_name_valid(ok1c) == 1 && pc_mp_guests_name_valid(okhigh) == 1);
        check("G1 name rule: an all-space name, an all-wide-space name and a mix of the three blank codes (32 / 210 / 211) are NOT valid (vanilla refuses an empty name)",
              pc_mp_guests_name_valid(blank) == 0 && pc_mp_guests_name_valid(wide) == 0 && pc_mp_guests_name_valid(mixed_blank) == 0);
        check("G1 name rule: NUL (inside or first), CHAR_CONTROL_CODE 127, CHAR_MESSAGE_TAG 128, CHAR_NEW_LINE 205 and the unused codes 223 / 255 are NOT valid",
              pc_mp_guests_name_valid(nul_in) == 0 && pc_mp_guests_name_valid(nul_first) == 0 && pc_mp_guests_name_valid(ctl) == 0 && pc_mp_guests_name_valid(tag) == 0 &&
              pc_mp_guests_name_valid(nl) == 0 && pc_mp_guests_name_valid(unused) == 0 && pc_mp_guests_name_valid(ff) == 0 && pc_mp_guests_name_valid(NULL) == 0);
        check("G1 name rule: exactly 8 bytes are examined (PC_MP_GUEST_NAME_LEN)", PC_MP_GUEST_NAME_LEN == 8);
        {
            /* Guests G1.1: the reserved observer name: EXACT 8-byte "SERVER" + 2 spaces, nothing else (no case folding, no trimming, no prefix match) */
            static const uint8_t r_ok[8] = { 'S', 'E', 'R', 'V', 'E', 'R', ' ', ' ' };
            static const uint8_t r_lc[8] = { 'S', 'e', 'r', 'v', 'e', 'r', ' ', ' ' };
            static const uint8_t r_lower[8] = { 's', 'e', 'r', 'v', 'e', 'r', ' ', ' ' };
            static const uint8_t r_x[8] = { 'S', 'E', 'R', 'V', 'E', 'R', 'X', ' ' };
            static const uint8_t r_short[8] = { 'S', 'E', 'R', 'V', 'E', ' ', ' ', ' ' };
            static const uint8_t r_lead[8] = { ' ', 'S', 'E', 'R', 'V', 'E', 'R', ' ' };
            static const uint8_t r_wide[8] = { 'S', 'E', 'R', 'V', 'E', 'R', 210, 211 };
            static const uint8_t r_nul[8] = { 'S', 'E', 'R', 'V', 'E', 'R', 0, ' ' };
            static const uint8_t r_tail[8] = { 'S', 'E', 'R', 'V', 'E', 'R', ' ', 'X' };
            check("G1.1 reserved name: exactly \"SERVER  \" (6 letters + 2 spaces) is reserved, the macro is that literal, and it is itself a valid game name",
                  pc_mp_guests_name_reserved(r_ok) == 1 && memcmp(PC_MP_GUEST_RESERVED_NAME, "SERVER  ", 8) == 0 && pc_mp_guests_name_valid(r_ok) == 1);
            check("G1.1 reserved name: 'Server  ', 'server  ', 'SERVERX ', 'SERVE   ', ' SERVER ', 'SERVER'+wide blanks, 'SERVER'+NUL, 'SERVER X' are NOT reserved (exact comparison, not case-folded)",
                  pc_mp_guests_name_reserved(r_lc) == 0 && pc_mp_guests_name_reserved(r_lower) == 0 && pc_mp_guests_name_reserved(r_x) == 0 && pc_mp_guests_name_reserved(r_short) == 0 &&
                  pc_mp_guests_name_reserved(r_lead) == 0 && pc_mp_guests_name_reserved(r_wide) == 0 && pc_mp_guests_name_reserved(r_nul) == 0 && pc_mp_guests_name_reserved(r_tail) == 0 &&
                  pc_mp_guests_name_reserved(blank) == 0 && pc_mp_guests_name_reserved(ok8) == 0);
            check("G1.1 reserved name: NULL is not reserved", pc_mp_guests_name_reserved(NULL) == 0);
        }
    }

    /* ---------- serialize / parse ---------- */
    f = mkfile(7, 5);
    r = pc_mp_guests_serialize(f, buf, sizeof(buf));
    check("serialize ok", r == PC_MP_GST_OK);
    check("magic ACMPGST + version 2 + 8 slots in the header", memcmp(buf, "ACMPGST", 8) == 0 && buf[8] == 2 && buf[16] == 8);
    r = pc_mp_guests_parse(buf, sizeof(buf), &out);
    check("parse round-trips the 5-guest table (pid, token, epoch, rev, record, generation)", r == PC_MP_GST_OK && same(f, &out));
    check("absent entries stay all-zero", out.e[5].present == 0 && out.e[7].present == 0);
    check("an empty table (no guests) is a valid file", pc_mp_guests_serialize(mkfile(1, 0), buf, sizeof(buf)) == PC_MP_GST_OK && pc_mp_guests_parse(buf, sizeof(buf), &out) == PC_MP_GST_OK);
    pc_mp_guests_serialize(mkfile(7, 5), buf, sizeof(buf));

    memcpy(bad, buf, sizeof(buf)); bad[0] ^= 1;
    check("bad magic refused", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_MAGIC);
    memcpy(bad, buf, sizeof(buf)); put32(bad + 8, 0); resign(bad);
    check("OLDER version (0) refused", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_VERSION_OLD);
    memcpy(bad, buf, sizeof(buf)); put32(bad + 8, 1); resign(bad);
    check("the v1 format (one town, no confirmed / age) is refused as OLDER, never reinterpreted", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_VERSION_OLD);
    memcpy(bad, buf, sizeof(buf)); put32(bad + 8, 3); resign(bad);
    check("FUTURE version (3) refused", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_VERSION_FUTURE);
    memcpy(bad, buf, sizeof(buf)); put32(bad + 12, 1); resign(bad);
    check("non-zero flags refused", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_SHAPE);
    memcpy(bad, buf, sizeof(buf)); put32(bad + 16, 9); resign(bad);
    check("slot_count != 8 refused", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_SHAPE);
    check("truncated (header only / half) refused", pc_mp_guests_parse(buf, 10, &out) == PC_MP_GST_ERR_TRUNCATED && pc_mp_guests_parse(buf, sizeof(buf) / 2, &out) == PC_MP_GST_ERR_TRUNCATED);
    check("one byte too long refused", pc_mp_guests_parse(buf, sizeof(buf) + 1, &out) != PC_MP_GST_OK);
    memcpy(bad, buf, sizeof(buf)); bad[100] ^= 0x40;
    check("file CRC mismatch refused", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_CRC);
    memcpy(bad, buf, sizeof(buf)); bad[PC_MP_GUEST_HEADER_SIZE + 100] ^= 0x01; resign(bad);
    check("a record byte changed with a valid file CRC: the per-record CRC refuses it", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_RECORD_CRC);
    memcpy(bad, buf, sizeof(buf)); bad[PC_MP_GUEST_HEADER_SIZE] = 2; resign(bad);
    check("present flag > 1 refused", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
    memcpy(bad, buf, sizeof(buf)); bad[PC_MP_GUEST_HEADER_SIZE + 5 * PC_MP_GUEST_ENTRY_SIZE + 9] = 1; resign(bad);
    check("an ABSENT entry with non-zero bytes refused", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
    {
        PCMpGuestFile g2;
        g2 = *mkfile(7, 5); memset(g2.e[1].pid, 0, 20);
        check("present entry with an all-zero key refuses to serialize+parse", pc_mp_guests_serialize(&g2, bad, sizeof(bad)) == PC_MP_GST_OK && pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
        g2 = *mkfile(7, 5); memset(g2.e[1].token, 0, 16);
        check("present entry with an all-zero TOKEN refused (a zero token is 'no token')", pc_mp_guests_serialize(&g2, bad, sizeof(bad)) == PC_MP_GST_OK && pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
        g2 = *mkfile(7, 5); g2.e[1].epoch = 0;
        check("present entry with epoch 0 refused", pc_mp_guests_serialize(&g2, bad, sizeof(bad)) == PC_MP_GST_OK && pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
        g2 = *mkfile(7, 5); g2.e[1].rev = 0x80000000u;
        check("rev > 0x7FFFFFFF refused", pc_mp_guests_serialize(&g2, bad, sizeof(bad)) == PC_MP_GST_OK && pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
        g2 = *mkfile(7, 5); g2.e[1].record[3] ^= 1;
        check("a record whose first 20 bytes differ from the key refused (a record always belongs to its key)", pc_mp_guests_serialize(&g2, bad, sizeof(bad)) == PC_MP_GST_OK && pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
        g2 = *mkfile(7, 5); g2.e[1].record[PC_MP_GUEST_EXISTS_OFF] = 0;
        check("a record with exists == 0 refused", pc_mp_guests_serialize(&g2, bad, sizeof(bad)) == PC_MP_GST_OK && pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
        g2 = *mkfile(7, 5); memcpy(g2.e[3].pid, g2.e[1].pid, 20); memcpy(g2.e[3].record, g2.e[1].pid, 20);
        check("two entries with the same (town, key) refused (entries 1 and 3 are in the same town)", pc_mp_guests_serialize(&g2, bad, sizeof(bad)) == PC_MP_GST_OK && pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
        g2 = *mkfile(7, 5); memcpy(g2.e[2].pid, g2.e[1].pid, 20); memcpy(g2.e[2].record, g2.e[1].pid, 20);
        check("M1: the SAME key in two DIFFERENT host towns is valid (the table is keyed by (town, key))", pc_mp_guests_serialize(&g2, bad, sizeof(bad)) == PC_MP_GST_OK && pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_OK &&
              memcmp(out.e[1].pid, out.e[2].pid, 20) == 0 && out.e[1].town_land_name[5] != out.e[2].town_land_name[5]);
        g2 = *mkfile(7, 5); memset(g2.e[1].town_land_name, 0, 8);
        check("a present entry with an empty town name refused", pc_mp_guests_serialize(&g2, bad, sizeof(bad)) == PC_MP_GST_OK && pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
        g2 = *mkfile(7, 5);
        pc_mp_guests_serialize(&g2, bad, sizeof(bad)); bad[PC_MP_GUEST_HEADER_SIZE + 1 * PC_MP_GUEST_ENTRY_SIZE + 1] = 2; resign(bad);
        check("confirmed flag > 1 refused", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
        pc_mp_guests_serialize(&g2, bad, sizeof(bad)); bad[PC_MP_GUEST_HEADER_SIZE + 1 * PC_MP_GUEST_ENTRY_SIZE + 66] = 1; resign(bad);
        check("the town reserved bytes must be zero", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
        pc_mp_guests_serialize(&g2, bad, sizeof(bad));
        check("confirmed / age / town (name, id, terrain hash) round-trip", pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_OK && out.e[1].confirmed == 1 && out.e[2].confirmed == 0 &&
              out.e[3].age == 109u && out.e[1].town_land_id == 0x2001u && out.e[1].town_terrain_hash == 0xC0DE0001u && out.e[2].town_land_id == 0x2000u);
        g2 = *mkfile(7, 5); memcpy(g2.e[3].token, g2.e[1].token, 16);
        check("two guests with the same token refused (also across towns)", pc_mp_guests_serialize(&g2, bad, sizeof(bad)) == PC_MP_GST_OK && pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
        g2 = *mkfile(7, 5); memcpy(g2.e[2].token, g2.e[1].token, 16);
        check("two guests with the same token refused", pc_mp_guests_serialize(&g2, bad, sizeof(bad)) == PC_MP_GST_OK && pc_mp_guests_parse(bad, sizeof(bad), &out) == PC_MP_GST_ERR_FIELD);
    }
    check("serialize into a too-small buffer refused", pc_mp_guests_serialize(f, buf, 100) == PC_MP_GST_ERR_SHAPE);

    /* ---------- load / save ---------- */
    rm_all(sub);
    check("MISSING when nothing exists (no file, no .bak, no corrupt siblings)", pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_MISSING);
    r = pc_mp_guests_save(path, mkfile(1, 2));
    check("save creates save/mp/ (parent dirs) and the file", r == PC_MP_GST_OK && exists(path) && !exists(tmp) && !exists(b1));
    check("load OK == saved", pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_OK && same(&loaded, mkfile(1, 2)));
    pc_mp_guests_save(path, mkfile(2, 3));
    check("2nd save rotates the first into .bak1", exists(b1) && !exists(b2));
    pc_mp_guests_save(path, mkfile(3, 4));
    check("3rd save: .bak1 -> .bak2, previous -> .bak1 (3 generations)", exists(b1) && exists(b2));
    check("load OK == the newest save (generation 3, 4 guests)", pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_OK && same(&loaded, mkfile(3, 4)) && info.gen_used == 0);
    pc_mp_guests_save(path, mkfile(4, 5));

    flip(path, 200);
    r = pc_mp_guests_load(path, &loaded, &info);
    check("a flipped byte in guests.dat: the .bak1 generation is used (OK_BACKUP, generation 3), the bad file is MOVED ASIDE not deleted",
          r == PC_MP_GST_LOAD_OK_BACKUP && same(&loaded, mkfile(3, 4)) && info.gen_used == 1 && info.moved_aside == 1 && count_corrupt(sub, "guests.dat") == 1 && !exists(path));
    pc_mp_guests_save(path, mkfile(5, 6));
    check("a save after a recovery works and the preserved corrupt file stays", exists(path) && count_corrupt(sub, "guests.dat") == 1);
    flip(path, 300);
    flip(b1, 300);
    flip(b2, 300);
    r = pc_mp_guests_load(path, &loaded, &info);
    check("EVERY generation unreadable -> UNTRUSTED (not 'missing'): the table is empty, every bad file preserved",
          r == PC_MP_GST_LOAD_UNTRUSTED && info.moved_aside == 3 && loaded.e[0].present == 0 && count_corrupt(sub, "guests.dat") >= 4);
    check("UNTRUSTED is STICKY: with only preserved *.corrupt-* files left, the next load is UNTRUSTED again, never MISSING", pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_UNTRUSTED && info.first_corrupt_path[0] != 0);
    rm_all(sub);
    check("only a deliberate operator reset (remove guests.dat, .bak files AND *.corrupt-*) gives MISSING again", pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_MISSING);

    /* ---------- fault injection ---------- */
    pc_mp_guests_save(path, mkfile(10, 2));
    pc_mp_guests_save(path, mkfile(11, 3));
    memset(&flt, 0, sizeof(flt)); flt.fail_write_after = -1; flt.crash_partial_tmp = 5000;
    pc_mp_guests_set_fault(&flt);
    r = pc_mp_guests_save(path, mkfile(12, 4));
    pc_mp_guests_set_fault(NULL);
    check("crash with a PARTIAL tmp: the previous generation is untouched and loads; the leftover tmp is never a candidate",
          r == PC_MP_GST_ERR_FAULT_CRASH && exists(tmp) && pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_OK && same(&loaded, mkfile(11, 3)));
    remove(tmp);
    memset(&flt, 0, sizeof(flt)); flt.fail_write_after = -1; flt.crash_partial_tmp = -1; flt.crash_after_tmp = 1;
    pc_mp_guests_set_fault(&flt);
    r = pc_mp_guests_save(path, mkfile(12, 4));
    pc_mp_guests_set_fault(NULL);
    check("crash after a COMPLETE tmp (before rotation): previous generation still current, the tmp (uncommitted) is ignored",
          r == PC_MP_GST_ERR_FAULT_CRASH && pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_OK && same(&loaded, mkfile(11, 3)));
    remove(tmp);
    memset(&flt, 0, sizeof(flt)); flt.fail_write_after = -1; flt.crash_partial_tmp = -1; flt.crash_after_rotate = 1;
    pc_mp_guests_set_fault(&flt);
    r = pc_mp_guests_save(path, mkfile(12, 4));
    pc_mp_guests_set_fault(NULL);
    check("crash after the rotation (no guests.dat): the .bak1 generation (the previous file) is recovered", r == PC_MP_GST_ERR_FAULT_CRASH && !exists(path)
          && pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_OK_BACKUP && same(&loaded, mkfile(11, 3)));
    rm_all(sub);
    pc_mp_guests_save(path, mkfile(20, 2));
    pc_mp_guests_save(path, mkfile(21, 3));
    memset(&flt, 0, sizeof(flt)); flt.fail_write_after = -1; flt.crash_partial_tmp = -1; flt.fail_rename = 1;
    pc_mp_guests_set_fault(&flt);
    r = pc_mp_guests_save(path, mkfile(22, 4));
    pc_mp_guests_set_fault(NULL);
    check("a failing tmp -> main replace restores the previous file from .bak1 (and reports an error)", r == PC_MP_GST_ERR_IO && exists(path)
          && pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_OK && same(&loaded, mkfile(21, 3)) && !exists(tmp));
    memset(&flt, 0, sizeof(flt)); flt.fail_write_after = 1000; flt.crash_partial_tmp = -1;
    pc_mp_guests_set_fault(&flt);
    r = pc_mp_guests_save(path, mkfile(23, 4));
    pc_mp_guests_set_fault(NULL);
    check("a write error (disk full style) leaves the previous generation untouched, no tmp left", r == PC_MP_GST_ERR_IO && !exists(tmp)
          && pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_OK && same(&loaded, mkfile(21, 3)));
    rm_all(sub);
    pc_mp_guests_save(path, mkfile(30, 2));
    pc_mp_guests_save(path, mkfile(31, 3));
    pc_mp_guests_save(path, mkfile(32, 4));
    flip(path, 123);
    pc_mp_guests_save(path, mkfile(33, 5));
    check("a save over an UNREADABLE current file preserves it aside instead of rotating it over a good .bak1/.bak2",
          count_corrupt(sub, "guests.dat") == 1 && exists(b1) && exists(b2) && pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_OK && same(&loaded, mkfile(33, 5)));
    {
        PCMpGuestFile* big = mkfile(40, 8);
        check("a FULL table (8 guests) saves and loads", pc_mp_guests_save(path, big) == PC_MP_GST_OK && pc_mp_guests_load(path, &loaded, &info) == PC_MP_GST_LOAD_OK && same(&loaded, big));
    }

    /* ---------- random tokens ---------- */
    {
        uint8_t t1[16], t2[16], z[16];
        int ok1, ok2, nz = 0;
        memset(z, 0, sizeof(z));
        ok1 = pc_mp_guests_random_bytes(t1, 16);
        ok2 = pc_mp_guests_random_bytes(t2, 16);
        for (i = 0; i < 16; i++) {
            nz += t1[i] != 0;
        }
        check("OS random: two 16-byte draws succeed, are not all zero and differ", ok1 && ok2 && nz > 0 && memcmp(t1, t2, 16) != 0 && memcmp(t1, z, 16) != 0);
    }

    /* ---------- client token file ---------- */
    {
        static PCMpGtkFile tf, lf;
        static uint8_t tbuf[PC_MP_GTK_FILE_SIZE], tbad[PC_MP_GTK_FILE_SIZE];
        PCMpGtkEntry e;
        char tpath[500], tb1[520], ttmp[520];
        int unreadable = 0, k;
        snprintf(tpath, sizeof(tpath), "%s/mp/guest_token.dat", g_dir);
        snprintf(tb1, sizeof(tb1), "%s.bak1", tpath);
        snprintf(ttmp, sizeof(ttmp), "%s.tmp", tpath);
        rm_all(sub);
        check("token entry size 64, file size 32 + 4 * 64 + 4", PC_MP_GTK_ENTRY_SIZE == 64 && PC_MP_GTK_FILE_SIZE == 32 + 4 * 64 + 4);
        memset(&tf, 0, sizeof(tf));
        memset(&e, 0, sizeof(e));
        e.present = 1;
        memcpy(e.host_land_name, "HOSTTOWN", 8);
        e.host_land_id = 0x1234;
        e.host_terrain_hash = 0xCAFEBABEu;
        for (k = 0; k < 20; k++) {
            e.home_pid[k] = (uint8_t)(0x41 + k);
        }
        for (k = 0; k < 16; k++) {
            e.token[k] = (uint8_t)(0x90 + k);
        }
        check("put adds an entry; find locates it by (host town, home pid) and not by anything else",
              pc_mp_gtoken_put(&tf, &e) == 0 && pc_mp_gtoken_find(&tf, e.host_land_name, 0x1234, 0xCAFEBABEu, e.home_pid) == 0
              && pc_mp_gtoken_find(&tf, e.host_land_name, 0x1235, 0xCAFEBABEu, e.home_pid) < 0 && pc_mp_gtoken_find(&tf, e.host_land_name, 0x1234, 0xCAFEBABFu, e.home_pid) < 0
              && pc_mp_gtoken_find(&tf, (const uint8_t*)"OTHERTWN", 0x1234, 0xCAFEBABEu, e.home_pid) < 0);
        e.token[0] ^= 0xFF;
        check("put on the same key REPLACES the entry (still one entry)", pc_mp_gtoken_put(&tf, &e) == 0 && tf.e[1].present == 0 && tf.e[0].token[0] == e.token[0]);
        check("serialize + parse round-trip", pc_mp_gtoken_serialize(&tf, tbuf, sizeof(tbuf)) == PC_MP_GST_OK && pc_mp_gtoken_parse(tbuf, sizeof(tbuf), &lf) == PC_MP_GST_OK
              && memcmp(lf.e, tf.e, sizeof(tf.e)) == 0 && memcmp(tbuf, "ACMPGTK", 8) == 0);
        memcpy(tbad, tbuf, sizeof(tbuf)); tbad[40] ^= 1;
        check("token file: CRC mismatch refused", pc_mp_gtoken_parse(tbad, sizeof(tbad), &lf) == PC_MP_GST_ERR_CRC);
        memcpy(tbad, tbuf, sizeof(tbuf)); put32(tbad + 8, 2); put32(tbad + PC_MP_GTK_FILE_SIZE - 4, pc_mp_records_crc32(tbad, PC_MP_GTK_FILE_SIZE - 4));
        check("token file: FUTURE version refused", pc_mp_gtoken_parse(tbad, sizeof(tbad), &lf) == PC_MP_GST_ERR_VERSION_FUTURE);
        check("token file: truncated refused", pc_mp_gtoken_parse(tbuf, 20, &lf) == PC_MP_GST_ERR_TRUNCATED);
        {
            PCMpGtkFile z = tf;
            memset(z.e[0].token, 0, 16);
            check("token file: an all-zero token refused", pc_mp_gtoken_serialize(&z, tbad, sizeof(tbad)) == PC_MP_GST_OK && pc_mp_gtoken_parse(tbad, sizeof(tbad), &lf) == PC_MP_GST_ERR_FIELD);
        }
        check("load: no file at all -> ok, empty, not unreadable", pc_mp_gtoken_load(tpath, &lf, &unreadable) == 1 && unreadable == 0 && lf.e[0].present == 0);
        check("save creates the file (atomic: no tmp left)", pc_mp_gtoken_save(tpath, &tf) == PC_MP_GST_OK && exists(tpath) && !exists(ttmp));
        check("load returns what was saved", pc_mp_gtoken_load(tpath, &lf, &unreadable) == 1 && unreadable == 0 && memcmp(lf.e, tf.e, sizeof(tf.e)) == 0);
        tf.generation++;
        pc_mp_gtoken_save(tpath, &tf);
        check("a second save keeps the previous file as .bak1", exists(tb1));
        flip(tpath, 70);
        check("a corrupt main file falls back to .bak1 (valid, not unreadable) and the bad file is preserved aside", pc_mp_gtoken_load(tpath, &lf, &unreadable) == 1 && unreadable == 0
              && count_corrupt(sub, "guest_token.dat") == 1);
        /* the bad main file was moved aside by the load above: write a fresh main, then corrupt main AND .bak1 */
        pc_mp_gtoken_save(tpath, &tf);
        flip(tpath, 70);
        flip(tb1, 70);
        check("both main and .bak1 corrupt -> UNREADABLE reported (tokens unknown), every bad file preserved, nothing deleted",
              pc_mp_gtoken_load(tpath, &lf, &unreadable) == 0 && unreadable == 1 && lf.e[0].present == 0 && count_corrupt(sub, "guest_token.dat") >= 3);
        rm_all(sub);
        for (k = 0; k < 6; k++) {
            memset(&e, 0, sizeof(e));
            e.present = 1;
            memcpy(e.host_land_name, "TOWN", 4);
            e.host_land_id = (uint16_t)(0x100 + k);
            e.host_terrain_hash = 1;
            e.home_pid[0] = 7;
            e.token[0] = (uint8_t)(k + 1);
            (void)pc_mp_gtoken_put(&tf, &e);
        }
        check("a full token table recycles the oldest slot: 4 entries remain, the newest present, the oldest gone",
              tf.e[0].present && tf.e[3].present && tf.e[3].host_land_id == 0x105 && pc_mp_gtoken_find(&tf, (const uint8_t*)"TOWN\0\0\0\0", 0x100, 1, e.home_pid) < 0);
        (void)n;
    }

    printf("RESULT passed=%d failed=%d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
