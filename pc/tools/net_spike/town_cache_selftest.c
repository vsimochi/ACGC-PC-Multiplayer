/* town_cache_selftest.c - NATIVE unit test of pc/src/pc_town_cache.c (M-A runtime card dir, M-B town cache). Driven by test_town_cache_unit.py.
 * usage: town_cache_selftest <scratch dir>   (everything happens under the scratch dir; the cwd is never written).
 * Output: "PASS: ..." / "FAIL: ..." lines and a final "RESULT passed=N failed=M". */
#include "pc_town_cache.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

static int g_pass = 0, g_fail = 0;
#define CHECK(desc, cond) do { if (cond) { g_pass++; printf("PASS: %s\n", desc); } else { g_fail++; printf("FAIL: %s\n", desc); } } while (0)

static int exists(const char* p) { struct stat st; return stat(p, &st) == 0; }

static int write_file(const char* path, const void* data, size_t n) {
    FILE* f = fopen(path, "wb");
    if (!f) return 0;
    fwrite(data, 1, n, f);
    fclose(f);
    return 1;
}

static int file_equals(const char* path, const void* data, size_t n) {
    FILE* f = fopen(path, "rb");
    unsigned char* b;
    size_t got;
    int ok;
    if (!f) return 0;
    b = (unsigned char*)malloc(n + 1);
    got = fread(b, 1, n + 1, f);
    fclose(f);
    ok = got == n && memcmp(b, data, n) == 0;
    free(b);
    return ok;
}

/* does dir (non-recursive) hold an entry whose name ends with `suffix`? */
#include <dirent.h>
static int dir_has_suffix(const char* dir, const char* suffix) {
    DIR* d = opendir(dir);
    struct dirent* e;
    int found = 0;
    size_t sl = strlen(suffix);
    if (!d) return 0;
    while ((e = readdir(d)) != NULL) {
        size_t n = strlen(e->d_name);
        if (n >= sl && strcmp(e->d_name + n - sl, suffix) == 0) found = 1;
    }
    closedir(d);
    return found;
}

int main(int argc, char** argv) {
    char root[300], buf[700];
    PCTownId t1, t2;
    char key[PC_TOWN_KEY_LEN + 1], key2[PC_TOWN_KEY_LEN + 1];
    PCTownPaths p1, p2, p;
    PCTownPart part;
    uint32_t sz = 0, crc = 0;
    unsigned char data[2500], oldc[777];
    size_t i;

    if (argc < 2) { printf("usage: town_cache_selftest <scratch>\n"); return 2; }
    snprintf(root, sizeof(root), "%s/towns", argv[1]);
    pc_town_mkdirs(argv[1]);

    /* ---- M-A: accessors ---- */
    CHECK("default pc_card_a_dir() is save/card_a (byte-identical to the old macro)", strcmp(pc_card_a_dir(), "save/card_a") == 0);
    CHECK("default pc_gci_path() is save/card_a/DobutsunomoriP_MURA.gci", strcmp(pc_gci_path(), "save/card_a/DobutsunomoriP_MURA.gci") == 0);
    CHECK("default pc_gci_tmp_path() is save/card_a/DobutsunomoriP_MURA.gci.tmp", strcmp(pc_gci_tmp_path(), "save/card_a/DobutsunomoriP_MURA.gci.tmp") == 0);
    CHECK("no town dir is active by default", !pc_card_town_dir_active() && pc_card_town_dir() == NULL);
    CHECK("empty / NULL town dir is refused", !pc_card_set_town_dir("") && !pc_card_set_town_dir(NULL) && !pc_card_town_dir_active());
    snprintf(buf, sizeof(buf), "%s/mp/towns/abc/", argv[1]);
    CHECK("set town dir (trailing separator trimmed)", pc_card_set_town_dir(buf) && pc_card_town_dir_active());
    snprintf(buf, sizeof(buf), "%s/mp/towns/abc/card_a", argv[1]);
    CHECK("pc_card_a_dir() = <town>/card_a after set", strcmp(pc_card_a_dir(), buf) == 0);
    snprintf(buf, sizeof(buf), "%s/mp/towns/abc/card_a/DobutsunomoriP_MURA.gci", argv[1]);
    CHECK("pc_gci_path() follows", strcmp(pc_gci_path(), buf) == 0);
    strcat(buf, ".tmp");
    CHECK("pc_gci_tmp_path() follows", strcmp(pc_gci_tmp_path(), buf) == 0);
    snprintf(buf, sizeof(buf), "%s/mp/towns/other", argv[1]);
    CHECK("a DIFFERENT town dir cannot replace the one set (set once)", !pc_card_set_town_dir(buf));
    snprintf(buf, sizeof(buf), "%s/mp/towns/abc", argv[1]);
    CHECK("the same dir again is a harmless success", pc_card_set_town_dir(buf));
    pc_card_reset_town_dir_for_test();
    CHECK("test reset restores save/card_a", strcmp(pc_card_a_dir(), "save/card_a") == 0 && !pc_card_town_dir_active());

    /* ---- key / paths ---- */
    memset(&t1, 0, sizeof(t1));
    memcpy(t1.land_name, "\x01\x02\x03\x0a\x0b\x0c\xde\xff", 8);
    t1.land_id = 0x5b01;
    t1.terrain_hash = 0xA1B2C3D4u;
    pc_town_key_format(&t1, key);
    CHECK("townkey = land_name 16 hex _ land_id 4 hex _ terrain_hash 8 hex (lowercase)", strcmp(key, "0102030a0b0cdeff_5b01_a1b2c3d4") == 0);
    CHECK("key length is PC_TOWN_KEY_LEN (30) and valid", strlen(key) == PC_TOWN_KEY_LEN && pc_town_key_valid(key));
    CHECK("invalid keys are rejected (upper case, short, bad separator, traversal)",
          !pc_town_key_valid("0102030A0B0CDEFF_5B01_A1B2C3D4") && !pc_town_key_valid("0102030a0b0cdeff_5b01_a1b2c3d") &&
          !pc_town_key_valid("0102030a0b0cdeff-5b01-a1b2c3d4") && !pc_town_key_valid("../../../../etc/passwd_______") && !pc_town_key_valid(NULL));
    memcpy(&t2, &t1, sizeof(t2));
    t2.terrain_hash ^= 1u;
    CHECK("PCTownId equality: equal / different hash", pc_town_id_equal(&t1, &t1) && !pc_town_id_equal(&t1, &t2));
    pc_town_key_format(&t2, key2);
    CHECK("a different terrain hash gives a different key", strcmp(key, key2) != 0);
    CHECK("paths for a valid key", pc_town_paths(root, key, &p1));
    snprintf(buf, sizeof(buf), "%s/%s/card_a/DobutsunomoriP_MURA.gci", root, key);
    CHECK("gci path = <root>/<key>/card_a/DobutsunomoriP_MURA.gci", strcmp(p1.gci, buf) == 0);
    snprintf(buf, sizeof(buf), "%s/%s/incoming/town.part", root, key);
    CHECK("part path = <root>/<key>/incoming/town.part (NOT inside card_a)", strcmp(p1.part, buf) == 0 && strstr(p1.part, "card_a") == NULL);
    snprintf(buf, sizeof(buf), "%s/%s/origin.ini", root, key);
    CHECK("origin.ini path", strcmp(p1.origin_ini, buf) == 0);
    CHECK("invalid key gives no paths", !pc_town_paths(root, "bad", &p));
    CHECK("NULL root = save/mp/towns", pc_town_paths(NULL, key, &p) && strncmp(p.town_dir, "save/mp/towns/", 14) == 0);

    /* ---- CRC32 ---- */
    CHECK("crc32(\"123456789\") == 0xCBF43926 (zlib / python zlib.crc32)", pc_town_crc32("123456789", 9) == 0xCBF43926u);
    CHECK("crc32 incremental == one shot", pc_town_crc32_update(pc_town_crc32("1234", 4), "56789", 5) == 0xCBF43926u);
    CHECK("crc32 of nothing is 0", pc_town_crc32("", 0) == 0);

    /* ---- origin.ini ---- */
    {
        PCTownOrigin o, r;
        memset(&o, 0, sizeof(o));
        snprintf(o.server_name, sizeof(o.server_name), "Friends Island");
        snprintf(o.address, sizeof(o.address), "192.168.1.20");
        o.port = 7777;
        o.last_fetch = 1730000000;
        CHECK("origin.ini write (creates the town dir)", pc_town_origin_write(p1.origin_ini, &o));
        memset(&r, 0, sizeof(r));
        CHECK("origin.ini round trip", pc_town_origin_read(p1.origin_ini, &r) && strcmp(r.server_name, o.server_name) == 0 && strcmp(r.address, o.address) == 0 &&
                                           r.port == 7777 && r.last_fetch == 1730000000);
        snprintf(buf, sizeof(buf), "%s.tmp", p1.origin_ini);
        CHECK("origin.ini leaves no .tmp behind", !exists(buf));
        CHECK("missing origin.ini reads as failure", !pc_town_origin_read("/nonexistent/dir/origin.ini", &r));
        write_file(p1.origin_ini, "[origin]\nserver = x\nport = 99\n", 30);
        CHECK("origin.ini without an address is malformed", !pc_town_origin_read(p1.origin_ini, &r));
        pc_town_origin_write(p1.origin_ini, &o);
    }

    /* ---- find by server (needs the GCI to exist) ---- */
    CHECK("find_by_server: no GCI yet -> not found", !pc_town_cache_find_by_server(root, "192.168.1.20", 7777, key2, &p));
    for (i = 0; i < sizeof(oldc); i++) oldc[i] = (unsigned char)(i * 7 + 1);
    pc_town_mkdirs(p1.card_dir);
    write_file(p1.gci, oldc, sizeof(oldc));
    CHECK("find_by_server: found by address+port once the GCI exists", pc_town_cache_find_by_server(root, "192.168.1.20", 7777, key2, &p) && strcmp(key2, key) == 0 && strcmp(p.gci, p1.gci) == 0);
    CHECK("find_by_server: other port / other address -> not found",
          !pc_town_cache_find_by_server(root, "192.168.1.20", 7778, key2, NULL) && !pc_town_cache_find_by_server(root, "192.168.1.21", 7777, key2, NULL));
    {
        PCTownOrigin o2;
        pc_town_paths(root, key2, &p2);
        pc_town_key_format(&t2, key2);
        pc_town_paths(root, key2, &p2);
        memset(&o2, 0, sizeof(o2));
        snprintf(o2.address, sizeof(o2.address), "192.168.1.20");
        o2.port = 7777;
        o2.last_fetch = 1740000000; /* newer */
        pc_town_origin_write(p2.origin_ini, &o2);
        pc_town_mkdirs(p2.card_dir);
        write_file(p2.gci, "x", 1);
        CHECK("find_by_server: the newest last_fetch wins among several towns of one server",
              pc_town_cache_find_by_server(root, "192.168.1.20", 7777, key, NULL) && strcmp(key, key2) == 0);
        pc_town_key_format(&t1, key);
    }

    /* ---- .part writer + atomic install ---- */
    for (i = 0; i < sizeof(data); i++) data[i] = (unsigned char)((i * 31 + 5) & 0xFF);
    CHECK("part begin creates incoming/", pc_town_part_begin(&part, p1.incoming_dir, p1.part) && exists(p1.incoming_dir));
    CHECK("in-order append 1", pc_town_part_append(&part, 0, data, 1000, sizeof(data)));
    CHECK("an out-of-order chunk (gap) is refused and fails the part", !pc_town_part_append(&part, 2000, data + 2000, 500, sizeof(data)) && part.failed);
    CHECK("after a failure the part refuses everything and finish reports failure", !pc_town_part_append(&part, 1000, data + 1000, 1000, sizeof(data)) && !pc_town_part_finish(&part, &sz, &crc));
    CHECK("a failed / partial part: install of it is the CALLER's decision; the old cache is byte-identical until then", file_equals(p1.gci, oldc, sizeof(oldc)));

    /* corrupt part (wrong content) is never installed by the flow: simulate the client rule (CRC mismatch -> no install) */
    CHECK("part begin (2nd, truncates the previous part)", pc_town_part_begin(&part, p1.incoming_dir, p1.part));
    CHECK("append all in order", pc_town_part_append(&part, 0, data, 1000, sizeof(data)) && pc_town_part_append(&part, 1000, data + 1000, 1000, sizeof(data)) &&
                                     pc_town_part_append(&part, 2000, data + 2000, 500, sizeof(data)));
    CHECK("an overrun beyond max_total is refused", !pc_town_part_append(&part, 2500, data, 1, sizeof(data)));
    pc_town_part_abort(&part);
    CHECK("partial part exists only in incoming/, never in card_a (no .tmp / .gci staged there)", exists(p1.part) && !dir_has_suffix(p1.card_dir, ".tmp") && !dir_has_suffix(p1.card_dir, ".part"));
    CHECK("old cache still byte-identical after the aborted download", file_equals(p1.gci, oldc, sizeof(oldc)));

    CHECK("part begin (3rd)", pc_town_part_begin(&part, p1.incoming_dir, p1.part));
    pc_town_part_append(&part, 0, data, 1000, sizeof(data));
    pc_town_part_append(&part, 1000, data + 1000, 1000, sizeof(data));
    pc_town_part_append(&part, 2000, data + 2000, 500, sizeof(data));
    CHECK("finish returns size and crc of what was written", pc_town_part_finish(&part, &sz, &crc) && sz == sizeof(data) && crc == pc_town_crc32(data, sizeof(data)));
    {
        uint32_t fc = 0, fs = 0;
        CHECK("pc_town_file_crc of the part matches", pc_town_file_crc(p1.part, &fc, &fs) && fc == crc && fs == sz);
    }
    CHECK("install of a MISSING part fails and leaves the cache untouched", !pc_town_cache_install("/nonexistent/town.part", p1.gci) && file_equals(p1.gci, oldc, sizeof(oldc)));
    CHECK("atomic install replaces the existing cache file", pc_town_cache_install(p1.part, p1.gci) && file_equals(p1.gci, data, sizeof(data)));
    CHECK("the part is consumed by the install", !exists(p1.part));
    CHECK("no .tmp / .part left in card_a after install", !dir_has_suffix(p1.card_dir, ".tmp") && !dir_has_suffix(p1.card_dir, ".part"));
    /* fresh install into a town that has no card_a yet */
    pc_town_key_format(&t2, key2);
    CHECK("install creates the card dir of a brand-new town", pc_town_paths(root, "ffffffffffffffff_0001_00000001", &p2) && pc_town_part_begin(&part, p2.incoming_dir, p2.part) &&
                                                                 pc_town_part_append(&part, 0, data, 100, sizeof(data)) && pc_town_part_finish(&part, &sz, &crc) &&
                                                                 pc_town_cache_install(p2.part, p2.gci) && file_equals(p2.gci, data, 100));

    printf("RESULT passed=%d failed=%d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
