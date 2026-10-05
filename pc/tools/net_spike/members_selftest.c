/* members_selftest.c - native unit test of pc/src/pc_mp_members.c (M-E host file save/mp/members.dat). Usage: members_selftest <scratch dir> [readfile <path>]
 * Prints "PASS: ..." / "FAIL: ..." lines and "RESULT passed=N failed=M". The module itself must never print a token: the Python runner greps this program's whole
 * output for the (ASCII, recognisable) token bytes used here. */
#include "pc_mp_members.h"
#include "pc_mp_records.h"

#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#ifdef _WIN32
#include <direct.h>
#include <windows.h>
#define MKDIR(p) _mkdir(p)
#else
#define MKDIR(p) mkdir(p, 0755)
#endif

static int g_pass = 0, g_fail = 0;
static char g_dir[300];

static void check(const char* d, int c) {
    if (c) {
        g_pass++;
        printf("PASS: %s\n", d);
    } else {
        g_fail++;
        printf("FAIL: %s\n", d);
    }
}

static void path_of(char* out, size_t cap, const char* name) {
    snprintf(out, cap, "%s/%s", g_dir, name);
}

static int exists(const char* p) {
    struct stat st;
    return stat(p, &st) == 0;
}

static int count_corrupt(const char* base) {
    DIR* d = opendir(g_dir);
    struct dirent* e;
    size_t bl = strlen(base);
    int n = 0;
    if (d == NULL) {
        return 0;
    }
    while ((e = readdir(d)) != NULL) {
        if (strncmp(e->d_name, base, bl) == 0 && strstr(e->d_name + bl, ".corrupt-") != NULL) {
            n++;
        }
    }
    closedir(d);
    return n;
}

static void rm_all(void) {
    DIR* d = opendir(g_dir);
    struct dirent* e;
    char p[600];
    if (d == NULL) {
        return;
    }
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") != 0 && strcmp(e->d_name, "..") != 0) {
            snprintf(p, sizeof(p), "%s/%s", g_dir, e->d_name);
            remove(p);
        }
    }
    closedir(d);
}

static void fill_entry(PCMpMemberEntry* e, int idx, int kind, const char* land, uint16_t land_id, uint32_t hash, const char* tok16) {
    memset(e, 0, sizeof(*e));
    e->present = 1;
    e->kind = (uint8_t)kind;
    e->res_slot = (uint8_t)(idx & 3);
    memcpy(e->land_name, land, 8);
    e->land_id = land_id;
    e->terrain_hash = hash;
    memset(e->pid, 0x30 + idx, 8);
    memcpy(e->pid + 8, land, 8);
    e->pid[16] = 0x4A;
    e->pid[17] = (uint8_t)idx;
    e->pid[18] = (uint8_t)(land_id >> 8);
    e->pid[19] = (uint8_t)land_id;
    memcpy(e->token, tok16, 16);
    e->age = (uint32_t)(idx + 1);
    if (kind == PC_MP_MEMBER_KIND_PROMOTION_HANDOFF) {
        memset(e->aux_pid, 0x61, PC_MP_MEMBERS_PID_SIZE);
    }
}

static void sample(PCMpMemberFile* f) {
    memset(f, 0, sizeof(*f));
    f->generation = 7;
    fill_entry(&f->e[0], 0, PC_MP_MEMBER_KIND_RESIDENT_TOKEN, "TESTTWN ", 0x1234, 0xCAFEBABEu, "TOKENTOKENTOKEN1");
    fill_entry(&f->e[1], 1, PC_MP_MEMBER_KIND_RESIDENT_TOKEN, "TESTTWN ", 0x1234, 0xCAFEBABEu, "TOKENTOKENTOKEN2");
    f->e[1].confirmed = 1;
    fill_entry(&f->e[5], 2, PC_MP_MEMBER_KIND_PROMOTION_HANDOFF, "OTHERTWN", 0x4321, 0x0BADF00Du, "TOKENTOKENTOKEN3");
    f->e[5].res_slot = 2;
}

static int same(const PCMpMemberFile* a, const PCMpMemberFile* b) {
    return a->generation == b->generation && memcmp(a->e, b->e, sizeof(a->e)) == 0;
}

static void put32(uint8_t* p, uint32_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static void refix_crc(uint8_t* buf) {
    put32(buf + PC_MP_MEMBERS_FILE_SIZE - 4, pc_mp_records_crc32(buf, PC_MP_MEMBERS_FILE_SIZE - 4));
}

static int write_raw(const char* p, const uint8_t* buf, size_t n) {
    FILE* fp = fopen(p, "wb");
    if (fp == NULL) {
        return 0;
    }
    fwrite(buf, 1, n, fp);
    fclose(fp);
    return 1;
}

static int read_raw(const char* p, uint8_t* buf, size_t cap, size_t* n) {
    FILE* fp = fopen(p, "rb");
    if (fp == NULL) {
        return 0;
    }
    *n = fread(buf, 1, cap, fp);
    fclose(fp);
    return 1;
}

static void test_format(void) {
    static uint8_t buf[PC_MP_MEMBERS_FILE_SIZE], b2[PC_MP_MEMBERS_FILE_SIZE];
    static PCMpMemberFile f, g;
    int i;
    check("layout: header 32 + 16 x 80 + 4 = 1316 bytes", PC_MP_MEMBERS_FILE_SIZE == 1316 && PC_MP_MEMBERS_ENTRY_SIZE == 80 && PC_MP_MEMBERS_SLOTS == 16);
    sample(&f);
    check("serialize ok", pc_mp_members_serialize(&f, buf, sizeof(buf)) == PC_MP_MBR_OK);
    check("header: magic ACMPMBR, version 1, flags 0, slot_count 16, entry_size 80, generation 7",
          memcmp(buf, "ACMPMBR\0", 8) == 0 && buf[8] == 1 && buf[12] == 0 && buf[16] == 16 && buf[20] == 80 && buf[24] == 7 && buf[28] == 0);
    check("entry 0 field offsets: present, confirmed, res_slot, kind at 0..3; land_name at 4; land_id LE at 12; hash LE at 16; pid at 20; token at 40; age at 56; aux at 60",
          buf[32] == 1 && buf[33] == 0 && buf[34] == 0 && buf[35] == 1 && memcmp(buf + 32 + 4, "TESTTWN ", 8) == 0 && buf[32 + 12] == 0x34 && buf[32 + 13] == 0x12 &&
              buf[32 + 16] == 0xBE && buf[32 + 19] == 0xCA && memcmp(buf + 32 + 20 + 8, "TESTTWN ", 8) == 0 && memcmp(buf + 32 + 40, "TOKENTOKENTOKEN1", 16) == 0 &&
              buf[32 + 56] == 1 && buf[32 + 60] == 0);
    check("handoff entry (slot 5) carries aux_pid, kind 2", buf[32 + 5 * 80 + 3] == 2 && buf[32 + 5 * 80 + 60] == 0x61);
    check("absent entries are all-zero", memcmp(buf + 32 + 2 * 80, "\0\0\0\0\0\0\0\0", 8) == 0);
    check("parse round trip equals the source table", pc_mp_members_parse(buf, sizeof(buf), &g) == PC_MP_MBR_OK && same(&f, &g));
    check("trailer CRC32 covers everything before it", ((uint32_t)buf[PC_MP_MEMBERS_FILE_SIZE - 4] | ((uint32_t)buf[PC_MP_MEMBERS_FILE_SIZE - 3] << 8) |
                                                         ((uint32_t)buf[PC_MP_MEMBERS_FILE_SIZE - 2] << 16) | ((uint32_t)buf[PC_MP_MEMBERS_FILE_SIZE - 1] << 24)) ==
                                                            pc_mp_records_crc32(buf, PC_MP_MEMBERS_FILE_SIZE - 4));

    /* refusals */
#define REFUSE(desc, mutate, expect)                                         \
    do {                                                                     \
        memcpy(b2, buf, sizeof(b2));                                         \
        mutate;                                                              \
        check(desc, pc_mp_members_parse(b2, sizeof(b2), &g) == (expect));    \
    } while (0)
    REFUSE("refuse: bad magic", b2[0] = 'X', PC_MP_MBR_ERR_MAGIC);
    REFUSE("refuse: OLDER version", b2[8] = 0, PC_MP_MBR_ERR_VERSION_OLD);
    REFUSE("refuse: FUTURE version", b2[8] = 2, PC_MP_MBR_ERR_VERSION_FUTURE);
    REFUSE("refuse: header flags != 0", (b2[12] = 1, refix_crc(b2)), PC_MP_MBR_ERR_SHAPE);
    REFUSE("refuse: slot count != 16", (b2[16] = 8, refix_crc(b2)), PC_MP_MBR_ERR_SHAPE);
    REFUSE("refuse: entry size != 80", (b2[20] = 72, refix_crc(b2)), PC_MP_MBR_ERR_SHAPE);
    REFUSE("refuse: reserved header word != 0", (b2[28] = 1, refix_crc(b2)), PC_MP_MBR_ERR_SHAPE);
    REFUSE("refuse: file CRC (one flipped entry byte)", b2[32 + 41] ^= 0x01, PC_MP_MBR_ERR_CRC);
    check("refuse: truncated (1315 bytes)", pc_mp_members_parse(buf, sizeof(buf) - 1, &g) == PC_MP_MBR_ERR_TRUNCATED);
    check("refuse: shorter than the header", pc_mp_members_parse(buf, 10, &g) == PC_MP_MBR_ERR_TRUNCATED);
    {
        static uint8_t big[PC_MP_MEMBERS_FILE_SIZE + 1];
        memcpy(big, buf, sizeof(buf));
        big[sizeof(buf)] = 0;
        check("refuse: one byte too long", pc_mp_members_parse(big, sizeof(big), &g) == PC_MP_MBR_ERR_SHAPE);
    }
    REFUSE("refuse: present byte 2", (b2[32] = 2, refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: confirmed byte 2", (b2[33] = 2, refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: unknown kind 3", (b2[35] = 3, refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: res_slot 4 (only 0..3)", (b2[34] = 4, refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: empty land name", (memset(b2 + 32 + 4, 0, 8), refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: nonzero town key reserved bytes", (b2[32 + 14] = 1, refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: zero pid", (memset(b2 + 32 + 20, 0, 20), refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: zero token", (memset(b2 + 32 + 40, 0, 16), refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: a resident token entry with an aux pid", (b2[32 + 60] = 1, refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: a handoff entry without an aux pid", (memset(b2 + 32 + 5 * 80 + 60, 0, 20), refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: an absent entry with stray bytes", (b2[32 + 2 * 80 + 50] = 9, refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: two entries with the same token", (memcpy(b2 + 32 + 1 * 80 + 40, b2 + 32 + 40, 16), refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    REFUSE("refuse: two entries with the same (town, pid, kind)", (memcpy(b2 + 32 + 1 * 80 + 20, b2 + 32 + 20, 20), refix_crc(b2)), PC_MP_MBR_ERR_FIELD);
    check("an empty table (all absent) is valid", (memset(&f, 0, sizeof(f)), pc_mp_members_serialize(&f, b2, sizeof(b2)) == PC_MP_MBR_OK && pc_mp_members_parse(b2, sizeof(b2), &g) == PC_MP_MBR_OK));
    check("serialize refuses a too small buffer", pc_mp_members_serialize(&f, b2, 100) == PC_MP_MBR_ERR_SHAPE);

    sample(&f);
    check("find: a resident token by (town, pid, kind)", pc_mp_members_find(&f, PC_MP_MEMBER_KIND_RESIDENT_TOKEN, (const uint8_t*)"TESTTWN ", 0x1234, 0xCAFEBABEu, f.e[1].pid) == 1);
    check("find: the same pid in ANOTHER town (land id differs) is not found", pc_mp_members_find(&f, PC_MP_MEMBER_KIND_RESIDENT_TOKEN, (const uint8_t*)"TESTTWN ", 0x1235, 0xCAFEBABEu, f.e[1].pid) < 0);
    check("find: the same pid with another terrain hash is not found", pc_mp_members_find(&f, PC_MP_MEMBER_KIND_RESIDENT_TOKEN, (const uint8_t*)"TESTTWN ", 0x1234, 0xCAFEBABFu, f.e[1].pid) < 0);
    check("find: a handoff is not a resident token (kind filter)", pc_mp_members_find(&f, PC_MP_MEMBER_KIND_RESIDENT_TOKEN, (const uint8_t*)"OTHERTWN", 0x4321, 0x0BADF00Du, f.e[5].pid) < 0 &&
                                                                   pc_mp_members_find(&f, PC_MP_MEMBER_KIND_PROMOTION_HANDOFF, (const uint8_t*)"OTHERTWN", 0x4321, 0x0BADF00Du, f.e[5].pid) == 5);
    check("free_slot is the first absent entry (2) and max_age is 3", pc_mp_members_free_slot(&f) == 2 && pc_mp_members_max_age(&f) == 3);
    for (i = 0; i < PC_MP_MEMBERS_SLOTS; i++) {
        if (!f.e[i].present) {
            fill_entry(&f.e[i], 3, PC_MP_MEMBER_KIND_RESIDENT_TOKEN, "FULLTWN ", (uint16_t)(0x100 + i), 1, "TOKENTOKENTOKEN1");
            f.e[i].token[15] = (uint8_t)('A' + i);
            f.e[i].pid[19] = (uint8_t)i;
        }
    }
    check("free_slot is -1 on a full table", pc_mp_members_free_slot(&f) == -1);
}

static void test_files(void) {
    static PCMpMemberFile f, g, h;
    static uint8_t raw[PC_MP_MEMBERS_FILE_SIZE];
    PCMpMemberLoadInfo info;
    char p[400], b1[400], b2[400], tmp[400];
    size_t n = 0;
    rm_all();
    path_of(p, sizeof(p), "members.dat");
    path_of(b1, sizeof(b1), "members.dat.bak1");
    path_of(b2, sizeof(b2), "members.dat.bak2");
    path_of(tmp, sizeof(tmp), "members.dat.tmp");

    check("load of nothing = MISSING (first run)", pc_mp_members_load(p, &g, &info) == PC_MP_MBR_LOAD_MISSING);
    sample(&f);
    check("save #1 ok, no .tmp left behind", pc_mp_members_save(p, &f) == PC_MP_MBR_OK && exists(p) && !exists(tmp) && !exists(b1));
    check("load = OK and equal", pc_mp_members_load(p, &g, &info) == PC_MP_MBR_LOAD_OK && same(&f, &g) && info.gen_used == 0);
    f.generation = 8;
    pc_mp_members_save(p, &f);
    check("save #2 rotates the previous file to .bak1", exists(b1) && !exists(b2));
    f.generation = 9;
    pc_mp_members_save(p, &f);
    check("save #3 rotates .bak1 -> .bak2 and the previous -> .bak1", exists(b1) && exists(b2));
    check("the three generations carry 9 / 8 / 7", pc_mp_members_load(p, &g, &info) == PC_MP_MBR_LOAD_OK && g.generation == 9);
    read_raw(b1, raw, sizeof(raw), &n);
    check(".bak1 holds generation 8", n == sizeof(raw) && pc_mp_members_parse(raw, n, &h) == PC_MP_MBR_OK && h.generation == 8);
    read_raw(b2, raw, sizeof(raw), &n);
    check(".bak2 holds generation 7", n == sizeof(raw) && pc_mp_members_parse(raw, n, &h) == PC_MP_MBR_OK && h.generation == 7);

    /* the main file is gone (crash between rotation and replace): .bak1 is used */
    remove(p);
    check("main file missing, .bak1 present: RECOVERED from .bak1 (OK_BACKUP)", pc_mp_members_load(p, &g, &info) == PC_MP_MBR_LOAD_OK_BACKUP && info.gen_used == 1 && g.generation == 8);

    /* CRC corruption of the main file: moved aside, recovered from .bak1 */
    pc_mp_members_save(p, &f); /* generation 9 again (rotates .bak1 = 8 stays? re-created below) */
    read_raw(p, raw, sizeof(raw), &n);
    raw[32 + 45] ^= 0xFF; /* a token byte: the CRC no longer matches */
    write_raw(p, raw, n);
    check("a CRC-corrupt main file is moved to .corrupt-<ts> (preserved, never deleted) and the load recovers from .bak1",
          pc_mp_members_load(p, &g, &info) == PC_MP_MBR_LOAD_OK_BACKUP && info.moved_aside == 1 && count_corrupt("members.dat") == 1 && !exists(p));
    {
        uint8_t keep[PC_MP_MEMBERS_FILE_SIZE];
        size_t kn = 0;
        char cp[600];
        DIR* d = opendir(g_dir);
        struct dirent* e;
        cp[0] = 0;
        while (d != NULL && (e = readdir(d)) != NULL) {
            if (strstr(e->d_name, ".corrupt-") != NULL) {
                snprintf(cp, sizeof(cp), "%s/%s", g_dir, e->d_name);
            }
        }
        if (d != NULL) {
            closedir(d);
        }
        check("the preserved .corrupt file still holds the corrupt bytes", cp[0] != 0 && read_raw(cp, keep, sizeof(keep), &kn) && kn == sizeof(keep) && memcmp(keep, raw, kn) == 0);
    }

    /* every generation unreadable: UNTRUSTED, and the mode is STICKY */
    rm_all();
    sample(&f);
    pc_mp_members_save(p, &f);
    f.generation = 8;
    pc_mp_members_save(p, &f);
    f.generation = 9;
    pc_mp_members_save(p, &f);
    {
        int k;
        const char* names[3] = { "members.dat", "members.dat.bak1", "members.dat.bak2" };
        for (k = 0; k < 3; k++) {
            char q[400];
            path_of(q, sizeof(q), names[k]);
            read_raw(q, raw, sizeof(raw), &n);
            raw[100] ^= 0x5A;
            write_raw(q, raw, n);
        }
    }
    check("ALL generations corrupt => UNTRUSTED, three .corrupt-* files preserved, no live file left",
          pc_mp_members_load(p, &g, &info) == PC_MP_MBR_LOAD_UNTRUSTED && info.moved_aside == 3 && count_corrupt("members.dat") == 3 && !exists(p) && !exists(b1) && !exists(b2));
    {
        int k, any = 0;
        for (k = 0; k < PC_MP_MEMBERS_SLOTS; k++) {
            any |= g.e[k].present;
        }
        check("the table of an UNTRUSTED load is empty (no credential is trusted)", !any);
    }
    check("UNTRUSTED is STICKY: a later load (no live files, only preserved *.corrupt-*) is UNTRUSTED again, not MISSING",
          pc_mp_members_load(p, &g, &info) == PC_MP_MBR_LOAD_UNTRUSTED && info.first_corrupt_path[0] != 0);

    /* save refuses an image that fails its own validation */
    rm_all();
    sample(&f);
    f.e[0].kind = 9;
    check("save REFUSES an invalid table (self-validation) and writes nothing", pc_mp_members_save(p, &f) == PC_MP_MBR_ERR_FIELD && !exists(p) && !exists(tmp));

    /* an unreadable current file at save time is preserved, never rotated over a good generation */
    rm_all();
    sample(&f);
    pc_mp_members_save(p, &f);
    read_raw(p, raw, sizeof(raw), &n);
    raw[40] ^= 0xFF;
    write_raw(p, raw, n);
    f.generation = 11;
    check("save over an UNREADABLE current file preserves it aside (.corrupt-*), writes the new one and keeps no bad .bak1",
          pc_mp_members_save(p, &f) == PC_MP_MBR_OK && count_corrupt("members.dat") == 1 && exists(p) && !exists(b1) && pc_mp_members_load(p, &g, &info) == PC_MP_MBR_LOAD_OK && g.generation == 11);
}

static int read_mode(const char* path) {
    static PCMpMemberFile g;
    PCMpMemberLoadInfo info;
    int i;
    PCMpMemberLoadMode m = pc_mp_members_load(path, &g, &info);
    printf("READ mode=%d generation=%u\n", (int)m, (unsigned)g.generation);
    for (i = 0; i < PC_MP_MEMBERS_SLOTS; i++) {
        if (g.e[i].present) {
            int k;
            printf("ENTRY %d kind=%d slot=%d confirmed=%d age=%u land=%.8s land_id=%u hash=%u pid=", i, g.e[i].kind, g.e[i].res_slot, g.e[i].confirmed, (unsigned)g.e[i].age,
                   (const char*)g.e[i].land_name, g.e[i].land_id, (unsigned)g.e[i].terrain_hash);
            for (k = 0; k < PC_MP_MEMBERS_PID_SIZE; k++) {
                printf("%02x", g.e[i].pid[k]);
            }
            printf(" token_first_byte=%02x\n", g.e[i].token[0]);
        }
    }
    return 0;
}

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: members_selftest <scratch dir> [readfile <path>]\n");
        return 2;
    }
    snprintf(g_dir, sizeof(g_dir), "%s", argv[1]);
    MKDIR(g_dir);
    if (argc >= 4 && strcmp(argv[2], "readfile") == 0) {
        return read_mode(argv[3]);
    }
    test_format();
    test_files();
    printf("RESULT passed=%d failed=%d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
