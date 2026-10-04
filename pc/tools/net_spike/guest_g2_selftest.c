/* guest_g2_selftest.c - standalone native unit test of Guests G2: the fresh-character predicate (pc_mp_guests.c pc_mp_guest_record_fresh_check, G2.1) and the
 * guest profile module (pc_guest_profile.c, G2.2).
 * Usage: guest_g2_selftest <scratch_dir> <fixtures_dir>
 *   <scratch_dir>    every file the profile tests create lives here (nothing else is touched)
 *   <fixtures_dir>   fresh_N.bin (N = 0..) = canonical BE 0x2440-byte FRESH guest records written by the PYTHON mirror (net_spike_lib.fresh_guest_record) of
 *                    pc_m_card.c pc_guest_build_fresh_record: an independent oracle for "what G1 produces"
 * Prints "PASS: ..." / "FAIL: ..." lines and "RESULT passed=N failed=M"; exit code 0 iff failed==0. Built and run by test_guest_g2_unit.py. */
#include "pc_guest_profile.h"
#include "pc_mp_guests.h"

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

static void spit(const char* p, const void* b, size_t n) {
    FILE* f = fopen(p, "wb");
    if (f) {
        fwrite(b, 1, n, f);
        fclose(f);
    }
}

static void be16(uint8_t* r, size_t o, unsigned v) {
    r[o] = (uint8_t)(v >> 8);
    r[o + 1] = (uint8_t)v;
}

static void be32(uint8_t* r, size_t o, unsigned v) {
    r[o] = (uint8_t)(v >> 24);
    r[o + 1] = (uint8_t)(v >> 16);
    r[o + 2] = (uint8_t)(v >> 8);
    r[o + 3] = (uint8_t)v;
}

static int count_entries(const char* dir) {
    DIR* d = opendir(dir);
    struct dirent* e;
    int n = 0;
    if (!d) {
        return -1;
    }
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") != 0 && strcmp(e->d_name, "..") != 0) {
            n++;
        }
    }
    closedir(d);
    return n;
}

/* ---------------- G2.1: the fresh predicate ---------------- */

static uint8_t g_fresh[8][PC_MP_GUEST_PRIVATE_SIZE];
static int g_nfresh = 0;

/* mutate a copy of fresh record 0 with `mut`, run the predicate, expect `reason` (0 = accepted) and, when reason != 0, the detail offset `off` */
typedef void (*MutFn)(uint8_t* r);
static void expect(const char* what, MutFn mut, int reason, unsigned off) {
    uint8_t r[PC_MP_GUEST_PRIVATE_SIZE];
    unsigned d = 0xFFFFu;
    int got;
    char msg[200];
    memcpy(r, g_fresh[0], sizeof(r));
    if (mut != NULL) {
        mut(r);
    }
    got = pc_mp_guest_record_fresh_check(r, sizeof(r), &d);
    snprintf(msg, sizeof(msg), "fresh predicate: %s -> %s", what, reason == 0 ? "ACCEPTED" : "REFUSED");
    check(msg, got == reason && (reason == 0 || d == off));
    if (got != reason || (reason != 0 && d != off)) {
        printf("   (got reason %d detail 0x%X, wanted reason %d detail 0x%X)\n", got, d, reason, off);
    }
}

static void m_pocket0(uint8_t* r) { be16(r, 0x68, 0x2000); }
static void m_pocket14(uint8_t* r) { be16(r, 0x68 + 14 * 2, 1); }
static void m_cond(uint8_t* r) { be32(r, 0x88, 1); }
static void m_wallet1(uint8_t* r) { be32(r, 0x8C, 1); }
static void m_walletmax(uint8_t* r) { be32(r, 0x8C, 99999); }
static void m_bank1(uint8_t* r) { be32(r, 0x122C, 1); }
static void m_bankmax(uint8_t* r) { be32(r, 0x122C, 999999999u); }
static void m_loan0(uint8_t* r) { be32(r, 0x90, 0); }
static void m_loan101(uint8_t* r) { be32(r, 0x90, 101); }
static void m_lotto_m(uint8_t* r) { r[0x86] = 3; }
static void m_lotto_s(uint8_t* r) { r[0x87] = 1; }
static void m_equip(uint8_t* r) { be16(r, 0x4A4, 0x2000); }
static void m_gift0(uint8_t* r) { be16(r, 0x4E0 + 0x2C, 0x2000); }
static void m_gift9_used(uint8_t* r) { r[0x4E0 + 9 * 0x12A + 0x2E] = 0; be16(r, 0x4E0 + 9 * 0x12A + 0x2C, 0x1234); }
static void m_letter_nogift(uint8_t* r) { r[0x4E0 + 2 * 0x12A + 0x2E] = 0; r[0x4E0 + 2 * 0x12A + 0x40] = 'H'; }
static void m_letter_rsvgift(uint8_t* r) { r[0x4E0 + 2 * 0x12A + 0x2E] = 0; be16(r, 0x4E0 + 2 * 0x12A + 0x2C, 0xFFFF); }
static void m_order(uint8_t* r) { be16(r, 0x10A8 + 4, 0x2000); }
static void m_order_level(uint8_t* r) { r[0x10A8 + 2] = 1; }
static void m_cat3(uint8_t* r) { r[0x1108 + 4] |= 0x01; r[0x1108 + 20] |= 0x10; r[0x11B4 - 1] |= 0x80; }
static void m_cat4(uint8_t* r) { m_cat3(r); r[0x11D0] |= 0x04; }
static void m_cat_big(uint8_t* r) { memset(r + 0x1108, 0xFF, 0xD4); }
static void m_quest_d(uint8_t* r) { r[0x94 + 3 * 0x28] = 0x00; }
static void m_quest_e(uint8_t* r) { r[0x2EC + 4 * 0x58] = 0x40; }
static void m_hint(uint8_t* r) { r[0x1087] = 1; }
static void m_destiny(uint8_t* r) { r[0x109A + 8] = 2; }
static void m_unk(uint8_t* r) { r[0x10BC + 5] = 1; }
static void m_aircheck(uint8_t* r) { r[0x10D4 + 7] = 1; }
static void m_fishflags(uint8_t* r) { r[0x1102] = 1; }
static void m_celebrated(uint8_t* r) { be16(r, 0x1104, 2001); }
static void m_map(uint8_t* r) { memcpy(r + 0x11DC + 3 * 10, "TOWN    ", 8); }
static void m_map_id(uint8_t* r) { be16(r, 0x11DC + 10 + 8, 7); }
static void m_flags_gift(uint8_t* r) { be32(r, 0x2348, 1u | (1u << 5)); }
static void m_flags0(uint8_t* r) { be32(r, 0x2348, 0); }
static void m_soncho(uint8_t* r) { r[0x23B4 + 3] = 1; }
static void m_golden(uint8_t* r) { r[0x23DA] = 1; }
static void m_soncho1(uint8_t* r) { r[0x23DC] = 0x80; }
static void m_bdaynpc(uint8_t* r) { be16(r, 0x23D8, 0xE000); }
static void m_ecard(uint8_t* r) { r[0x23E0 + 0x31] = 1; }
static void m_gender2(uint8_t* r) { r[0x14] = 2; }
static void m_face8(uint8_t* r) { r[0x15] = 8; }
static void m_resetcount(uint8_t* r) { r[0x16] = 1; }
static void m_shirt_range(uint8_t* r) { be16(r, 0x108A, 0x2410); be16(r, 0x1088, 0x10); }
static void m_shirt_idx(uint8_t* r) { be16(r, 0x1088, 3); be16(r, 0x108A, 0x2405); }
/* accepted variants */
static void a_appearance(uint8_t* r) { r[0x14] ^= 1; r[0x15] = 7; be16(r, 0x1088, 15); be16(r, 0x108A, 0x240F); }
static void a_calendar(uint8_t* r) { size_t i; for (i = 0x234C; i < 0x23B4; i++) r[i] ^= 0x5A; for (i = 0x23B8; i < 0x23D8; i++) r[i] ^= 0xA5; }
static void a_designs(uint8_t* r) { memset(r + 0x1240, 0x77, 0x2340 - 0x1240); }
static void a_cat3(uint8_t* r) { m_cat3(r); }
static void a_birthday(uint8_t* r) { r[0x10A4] = 0; r[0x10A5] = 0x7D; r[0x10A6] = 6; r[0x10A7] = 9; }

static void predicate_tests(const char* fx) {
    int i;
    uint8_t shortb[10] = { 0 };
    uint8_t big[PC_MP_GUEST_PRIVATE_SIZE + 1];
    unsigned d = 7;
    for (i = 0; i < 8; i++) {
        char p[500];
        size_t n;
        uint8_t* b;
        snprintf(p, sizeof(p), "%s/fresh_%d.bin", fx, i);
        b = slurp(p, &n);
        if (b == NULL) {
            break;
        }
        if (n == PC_MP_GUEST_PRIVATE_SIZE) {
            memcpy(g_fresh[g_nfresh++], b, n);
        }
        free(b);
    }
    check("fixtures: at least 4 fresh records of the python mirror (G1 oracle) were loaded", g_nfresh >= 4);
    for (i = 0; i < g_nfresh; i++) {
        char msg[120];
        unsigned dd = 9;
        snprintf(msg, sizeof(msg), "fresh predicate: the G1 fresh record #%d (python mirror of pc_guest_build_fresh_record) is ACCEPTED", i);
        check(msg, pc_mp_guest_record_fresh_check(g_fresh[i], PC_MP_GUEST_PRIVATE_SIZE, &dd) == PC_MP_FRESH_OK && dd == 0);
    }
    if (g_nfresh == 0) {
        return;
    }
    expect("fresh record + the 3 starter catalog bits", a_cat3, 0, 0);
    expect("fresh record + date dependent calendar / day counter tail changed", a_calendar, 0, 0);
    expect("fresh record + other gender / face 7 / last starter shirt", a_appearance, 0, 0);
    expect("fresh record + different Able Sisters designs", a_designs, 0, 0);
    expect("fresh record + a different (non-empty) birthday marker", a_birthday, 0, 0);
    expect("fresh record + a used letter WITHOUT a gift", m_letter_nogift, 0, 0);
    expect("fresh record + a used letter with the RSV_NO 'no gift' marker", m_letter_rsvgift, 0, 0);
    expect("pocket slot 0 holds an item", m_pocket0, PC_MP_FRESH_BAD_POCKET, 0x68);
    expect("pocket slot 14 holds an item", m_pocket14, PC_MP_FRESH_BAD_POCKET, 0x68 + 28);
    expect("an item condition bit is set", m_cond, PC_MP_FRESH_BAD_POCKET, 0x88);
    expect("wallet = 1", m_wallet1, PC_MP_FRESH_BAD_WALLET, 0x8C);
    expect("wallet = 99999", m_walletmax, PC_MP_FRESH_BAD_WALLET, 0x8C);
    expect("bank = 1", m_bank1, PC_MP_FRESH_BAD_BANK, 0x122C);
    expect("bank = 999999999", m_bankmax, PC_MP_FRESH_BAD_BANK, 0x122C);
    expect("loan = 0 (not the vanilla 100)", m_loan0, PC_MP_FRESH_BAD_LOAN, 0x90);
    expect("loan = 101", m_loan101, PC_MP_FRESH_BAD_LOAN, 0x90);
    expect("lotto expiry month set", m_lotto_m, PC_MP_FRESH_BAD_LOTTO, 0x86);
    expect("lotto ticket mail storage set", m_lotto_s, PC_MP_FRESH_BAD_LOTTO, 0x86);
    expect("equipment set", m_equip, PC_MP_FRESH_BAD_EQUIPMENT, 0x4A4);
    expect("letter 0 carries a gift", m_gift0, PC_MP_FRESH_BAD_MAIL_GIFT, 0x4E0 + 0x2C);
    expect("letter 9 (used) carries a gift", m_gift9_used, PC_MP_FRESH_BAD_MAIL_GIFT, 0x4E0 + 9 * 0x12A + 0x2C);
    expect("catalog order item set", m_order, PC_MP_FRESH_BAD_CATALOG_ORDER, 0x10A8);
    expect("catalog order shop level set", m_order_level, PC_MP_FRESH_BAD_CATALOG_ORDER, 0x10A8);
    expect("4 catalog bits (one more than the starter bits)", m_cat4, PC_MP_FRESH_BAD_CATALOG, 0x1108);
    expect("a full catalog", m_cat_big, PC_MP_FRESH_BAD_CATALOG, 0x1108);
    expect("a delivery quest is active", m_quest_d, PC_MP_FRESH_BAD_QUEST, 0x94 + 3 * 0x28);
    expect("an errand quest is active", m_quest_e, PC_MP_FRESH_BAD_QUEST, 0x2EC + 4 * 0x58);
    expect("hint_count != 0", m_hint, PC_MP_FRESH_BAD_PROGRESS, 0x1087);
    expect("a fortune (destiny) is set", m_destiny, PC_MP_FRESH_BAD_PROGRESS, 0x109A);
    expect("unk_10A8 span non-zero", m_unk, PC_MP_FRESH_BAD_PROGRESS, 0x10BC);
    expect("aircheck bitfield non-zero", m_aircheck, PC_MP_FRESH_BAD_PROGRESS, 0x10BC);
    expect("complete fish/insect flags set", m_fishflags, PC_MP_FRESH_BAD_PROGRESS, 0x1102);
    expect("celebrated birthday year set", m_celebrated, PC_MP_FRESH_BAD_PROGRESS, 0x1102);
    expect("a foreign map is collected (land name)", m_map, PC_MP_FRESH_BAD_PROGRESS, 0x11DC + 30);
    expect("a foreign map is collected (land id)", m_map_id, PC_MP_FRESH_BAD_PROGRESS, 0x11DC + 18);
    expect("state_flags carries a post office gift bit", m_flags_gift, PC_MP_FRESH_BAD_PROGRESS, 0x2348);
    expect("state_flags = 0 (not the vanilla 1)", m_flags0, PC_MP_FRESH_BAD_PROGRESS, 0x2348);
    expect("tortimer trophy field 0 set", m_soncho, PC_MP_FRESH_BAD_PROGRESS, 0x23B4);
    expect("golden item collected", m_golden, PC_MP_FRESH_BAD_PROGRESS, 0x23B4);
    expect("tortimer trophy field 1 set", m_soncho1, PC_MP_FRESH_BAD_PROGRESS, 0x23B4);
    expect("birthday present npc set", m_bdaynpc, PC_MP_FRESH_BAD_PROGRESS, 0x23B4);
    expect("e-Card letter data set", m_ecard, PC_MP_FRESH_BAD_PROGRESS, 0x23B4);
    expect("gender 2", m_gender2, PC_MP_FRESH_BAD_APPEARANCE, 0x14);
    expect("face 8", m_face8, PC_MP_FRESH_BAD_APPEARANCE, 0x14);
    expect("reset_count 1", m_resetcount, PC_MP_FRESH_BAD_APPEARANCE, 0x14);
    expect("shirt item outside ITM_CLOTH000..015", m_shirt_range, PC_MP_FRESH_BAD_APPEARANCE, 0x1088);
    expect("shirt idx != item - 0x2400", m_shirt_idx, PC_MP_FRESH_BAD_APPEARANCE, 0x1088);
    check("fresh predicate: a short buffer and NULL are refused as BAD_SHAPE, a too long one too",
          pc_mp_guest_record_fresh_check(shortb, sizeof(shortb), &d) == PC_MP_FRESH_BAD_SHAPE && pc_mp_guest_record_fresh_check(NULL, PC_MP_GUEST_PRIVATE_SIZE, NULL) == PC_MP_FRESH_BAD_SHAPE
              && pc_mp_guest_record_fresh_check(big, sizeof(big), NULL) == PC_MP_FRESH_BAD_SHAPE);
    {
        int r, ok = 1;
        for (r = 0; r < PC_MP_FRESH_REASON_COUNT; r++) {
            const char* s = pc_mp_guest_fresh_reason_str(r);
            if (s == NULL || s[0] == 0 || strcmp(s, "unknown") == 0) {
                ok = 0;
            }
        }
        check("every PC_MP_FRESH_* reason has a message", ok && strcmp(pc_mp_guest_fresh_reason_str(999), "unknown") == 0);
    }
    {
        /* every single-bit flip of the economy bytes of a fresh record is refused: pockets (0x68..0x86), item cond / wallet (0x88..0x90 except loan low byte), bank */
        int bad_accept = 0;
        size_t o;
        for (o = 0x68; o < 0x86; o++) {
            uint8_t r[PC_MP_GUEST_PRIVATE_SIZE];
            int b;
            for (b = 0; b < 8; b++) {
                memcpy(r, g_fresh[0], sizeof(r));
                r[o] ^= (uint8_t)(1u << b);
                if (pc_mp_guest_record_fresh_check(r, sizeof(r), NULL) == PC_MP_FRESH_OK) {
                    bad_accept++;
                }
            }
        }
        for (o = 0x122C; o < 0x1230; o++) {
            uint8_t r[PC_MP_GUEST_PRIVATE_SIZE];
            int b;
            for (b = 0; b < 8; b++) {
                memcpy(r, g_fresh[0], sizeof(r));
                r[o] ^= (uint8_t)(1u << b);
                if (pc_mp_guest_record_fresh_check(r, sizeof(r), NULL) == PC_MP_FRESH_OK) {
                    bad_accept++;
                }
            }
        }
        check("fresh predicate: no single-bit change of the pockets / bank is accepted", bad_accept == 0);
    }
}

/* ---------------- G2.2: the profile module ---------------- */

static uint32_t fnv_ident(const PCGuestProfile* p) {
    uint8_t b[20];
    uint32_t h = 2166136261u;
    int k;
    pc_guest_profile_name_bytes(p, b);
    pc_guest_profile_home_bytes(p, b + 8);
    b[16] = (uint8_t)(p->player_id >> 8);
    b[17] = (uint8_t)p->player_id;
    b[18] = (uint8_t)(p->land_id >> 8);
    b[19] = (uint8_t)p->land_id;
    for (k = 0; k < 20; k++) {
        h = (h ^ b[k]) * 16777619u;
    }
    return h;
}

static void parse_case(const char* what, const char* text, const char* want_key) {
    PCGuestProfile p;
    char err[300];
    char msg[300];
    int ok = pc_guest_profile_parse(text, strlen(text), &p, err, sizeof(err));
    snprintf(msg, sizeof(msg), "profile parse: %s -> refused naming '%s'", what, want_key);
    check(msg, !ok && strstr(err, want_key) != NULL);
    if (ok || strstr(err, want_key) == NULL) {
        printf("   (ok=%d err='%s')\n", ok, err);
    }
}

static const char* const k_valid_body = "name = Bella\ngender = 1\nface = 4\nhome_town = Hometown\nplayer_id = 4097\nland_id = 23297\n";

static void profile_tests(void) {
    char sub[450], path[500], tmp[510], sub2[500];
    PCGuestProfile a, b, c;
    char err[400];
    size_t n1, n2;
    uint8_t* b1;
    uint8_t* b2;
    int r;

    snprintf(sub, sizeof(sub), "%s/prof", g_dir);
    MKDIR(sub);
    snprintf(path, sizeof(path), "%s/save/mp/guest.ini", sub);
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    /* --- create --- */
    r = pc_guest_profile_load_or_create(path, &a, err, sizeof(err));
    check("create: a missing file is CREATED (result CREATED, parent directories made, no tmp left)", r == PC_GUEST_PROFILE_CREATED && exists(path) && !exists(tmp));
    check("create: default name 'Guest' / home_town 'GuestVil'; never the reserved name SERVER", strcmp(a.name, "Guest") == 0 && strcmp(a.home_town, "GuestVil") == 0);
    {
        uint8_t nb[8];
        pc_guest_profile_name_bytes(&a, nb);
        check("create: the default name is a valid game name and is not SERVER", pc_mp_guests_name_valid(nb) && !pc_mp_guests_name_reserved(nb));
    }
    check("create: ids in 1..0xFFFE", a.player_id >= 1 && a.player_id <= 0xFFFE && a.land_id >= 1 && a.land_id <= 0xFFFE);
    {
        const uint32_t h = fnv_ident(&a);
        check("create: gender / face are derived deterministically from the identity hash (gender bit 31, face bits 16..18)", a.gender == (int)((h >> 31) & 1u) && a.face == (int)((h >> 16) & 7u));
    }
    check("create: the result passes validation", pc_guest_profile_validate(&a, NULL, NULL) == 1);
    b1 = slurp(path, &n1);
    r = pc_guest_profile_load_or_create(path, &b, err, sizeof(err));
    check("load: an existing file is LOADED with exactly the created values (identity stable across two loads)",
          r == PC_GUEST_PROFILE_LOADED && memcmp(&a, &b, sizeof(a)) == 0);
    r = pc_guest_profile_load_or_create(path, &c, err, sizeof(err));
    b2 = slurp(path, &n2);
    check("load: a third load is the same again and the file is byte-identical (a load never rewrites)", r == PC_GUEST_PROFILE_LOADED && memcmp(&a, &c, sizeof(a)) == 0
          && b1 != NULL && b2 != NULL && n1 == n2 && memcmp(b1, b2, n1) == 0 && !exists(tmp));
    free(b1);
    free(b2);
    {
        char out[2048];
        size_t n = pc_guest_profile_format(&a, out, sizeof(out));
        PCGuestProfile rt;
        check("format -> parse round trip equals the profile; a too-small buffer returns 0", n > 0 && pc_guest_profile_parse(out, n, &rt, err, sizeof(err)) && memcmp(&a, &rt, sizeof(a)) == 0
              && pc_guest_profile_format(&a, out, 20) == 0);
    }
    {
        PCGuestProfile x, y;
        int diff = 0, k;
        for (k = 0; k < 8; k++) {
            if (pc_guest_profile_make_default(&x) && pc_guest_profile_make_default(&y) && (x.player_id != y.player_id || x.land_id != y.land_id)) {
                diff++;
            }
        }
        check("create: fresh CSPRNG ids (8 pairs of independent defaults differ in at least one id: >= 7 of 8)", diff >= 7);
    }
    /* a second create in another dir gets other ids than the first */
    snprintf(sub2, sizeof(sub2), "%s/prof2/guest.ini", g_dir);
    {
        PCGuestProfile o;
        r = pc_guest_profile_load_or_create(sub2, &o, err, sizeof(err));
        check("create: a second directory creates its own profile (CREATED) with different ids", r == PC_GUEST_PROFILE_CREATED && (o.player_id != a.player_id || o.land_id != a.land_id));
    }

    /* --- parse of a hand-written file --- */
    {
        PCGuestProfile p;
        const char* t = "\xEF\xBB\xBF# a comment\r\n; another\r\n[guest]\r\n  name =  Bella  \r\n\r\ngender=1\r\nface = 4\r\nhome_town = Home Twn\r\nplayer_id = 0x1001\r\nland_id = 23297\r\n";
        int ok = pc_guest_profile_parse(t, strlen(t), &p, err, sizeof(err));
        check("parse: BOM, CRLF, comments, [section], blanks around keys and values, hex id", ok && strcmp(p.name, "Bella") == 0 && p.gender == 1 && p.face == 4 && strcmp(p.home_town, "Home Twn") == 0
              && p.player_id == 0x1001 && p.land_id == 23297);
        {
            char spec[100];
            uint8_t nb[8], hb[8];
            check("spec string: NAME,LAND,PLAYER_ID,LAND_ID,GENDER,FACE (the --bootstrap-guest grammar)", pc_guest_profile_spec(&p, spec, sizeof(spec)) && strcmp(spec, "Bella,Home Twn,4097,23297,1,4") == 0
                  && !pc_guest_profile_spec(&p, spec, 10));
            pc_guest_profile_name_bytes(&p, nb);
            pc_guest_profile_home_bytes(&p, hb);
            check("name / home bytes are the 8-byte space padded PersonalID fields", memcmp(nb, "Bella   ", 8) == 0 && memcmp(hb, "Home Twn", 8) == 0);
        }
        {
            const char* t2 = "name = 12345678\ngender = 0\nface = 7\nhome_town = 12345678\nplayer_id = 1\nland_id = 65534\n";
            check("parse: 8-character name / town and the id range edges 1 and 65534 are accepted", pc_guest_profile_parse(t2, strlen(t2), &p, err, sizeof(err)) && p.player_id == 1 && p.land_id == 0xFFFE);
        }
        {
            const char* t3 = "name = Server\ngender = 0\nface = 7\nhome_town = Town\nplayer_id = 010\nland_id = 2\n";
            check("parse: 'Server' (not exactly SERVER) is a legal name, '010' is decimal 10 (no octal)", pc_guest_profile_parse(t3, strlen(t3), &p, err, sizeof(err)) && p.player_id == 10);
        }
        check("parse: the canonical valid body parses", pc_guest_profile_parse(k_valid_body, strlen(k_valid_body), &p, err, sizeof(err)));
    }
    /* --- every bad input names its key --- */
    parse_case("empty text (every key missing)", "", "name");
    parse_case("missing gender", "name = A\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "gender");
    parse_case("missing land_id", "name = A\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\n", "land_id");
    parse_case("missing player_id", "name = A\ngender = 0\nface = 1\nhome_town = H\nland_id = 2\n", "player_id");
    parse_case("unknown key", "name = A\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\nnmae = X\n", "nmae");
    parse_case("duplicate key", "name = A\nname = B\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "name");
    parse_case("a line without '='", "name = A\ngarbage line\n", "line 2");
    parse_case("empty name", "name = \ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "name");
    parse_case("9-character name", "name = Abcdefghi\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "name");
    parse_case("name with a comma", "name = A,B\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "name");
    parse_case("name with a control byte", "name = A\x01\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "name");
    parse_case("name with a non-ASCII byte", "name = A\xC3\xA9\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "name");
    parse_case("reserved name SERVER", "name = SERVER\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "reserved");
    parse_case("gender 2", "name = A\ngender = 2\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "gender");
    parse_case("gender x", "name = A\ngender = x\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "gender");
    parse_case("face 8", "name = A\ngender = 0\nface = 8\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "face");
    parse_case("face -1", "name = A\ngender = 0\nface = -1\nhome_town = H\nplayer_id = 1\nland_id = 2\n", "face");
    parse_case("empty home_town", "name = A\ngender = 0\nface = 1\nhome_town =\nplayer_id = 1\nland_id = 2\n", "home_town");
    parse_case("9-character home_town", "name = A\ngender = 0\nface = 1\nhome_town = Abcdefghi\nplayer_id = 1\nland_id = 2\n", "home_town");
    parse_case("player_id 0", "name = A\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 0\nland_id = 2\n", "player_id");
    parse_case("player_id 0xFFFF", "name = A\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 0xFFFF\nland_id = 2\n", "player_id");
    parse_case("player_id 65536", "name = A\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 65536\nland_id = 2\n", "player_id");
    parse_case("player_id abc", "name = A\ngender = 0\nface = 1\nhome_town = H\nplayer_id = abc\nland_id = 2\n", "player_id");
    parse_case("player_id 0x (no digits)", "name = A\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 0x\nland_id = 2\n", "player_id");
    parse_case("land_id 0", "name = A\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 0\n", "land_id");
    parse_case("land_id 0xFFFF", "name = A\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 0xFFFF\n", "land_id");
    parse_case("land_id 1e3", "name = A\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 1e3\n", "land_id");
    {
        PCGuestProfile p;
        const char nul[] = "name = A\0B\ngender = 0\n";
        char big[PC_GUEST_PROFILE_MAX_FILE + 10];
        memset(big, 'x', sizeof(big));
        check("parse: an embedded NUL byte is refused", !pc_guest_profile_parse(nul, sizeof(nul) - 1, &p, err, sizeof(err)) && strstr(err, "NUL") != NULL);
        check("parse: a file larger than 4096 bytes is refused", !pc_guest_profile_parse(big, sizeof(big), &p, err, sizeof(err)) && strstr(err, "too large") != NULL);
    }
    {
        PCGuestProfile p;
        const char* key = NULL;
        const char* why = NULL;
        memset(&p, 0, sizeof(p));
        strcpy(p.name, " Bad");
        strcpy(p.home_town, "T");
        p.player_id = 1;
        p.land_id = 1;
        check("validate: a name starting with a blank is refused (key 'name')", !pc_guest_profile_validate(&p, &key, &why) && key != NULL && strcmp(key, "name") == 0 && why != NULL);
        strcpy(p.name, "SERVER");
        check("validate: SERVER refused", !pc_guest_profile_validate(&p, &key, &why) && strcmp(key, "name") == 0);
    }

    /* --- a corrupt file is preserved, never rewritten --- */
    {
        char dir3[500], p3[560], t3[570];
        const char* bad = "name = Bella\ngender = 9\nface = 4\nhome_town = Hometown\nplayer_id = 4097\nland_id = 23297\n";
        uint8_t* before;
        uint8_t* after;
        size_t nb, na;
        PCGuestProfile p;
        snprintf(dir3, sizeof(dir3), "%s/prof3", g_dir);
        MKDIR(dir3);
        snprintf(p3, sizeof(p3), "%s/guest.ini", dir3);
        snprintf(t3, sizeof(t3), "%s.tmp", p3);
        spit(p3, bad, strlen(bad));
        before = slurp(p3, &nb);
        r = pc_guest_profile_load_or_create(p3, &p, err, sizeof(err));
        after = slurp(p3, &na);
        check("corrupt file: load REFUSES it (ERR) and the message names the file and the bad key 'gender'", r == PC_GUEST_PROFILE_ERR && strstr(err, "guest.ini") != NULL && strstr(err, "gender") != NULL);
        check("corrupt file: it is preserved byte-for-byte (not regenerated, not rewritten, not moved), no tmp file, no other file created",
              before != NULL && after != NULL && nb == na && memcmp(before, after, nb) == 0 && !exists(t3) && count_entries(dir3) == 1);
        free(before);
        free(after);
        /* a file with a valid body but no ids must not get ids invented */
        spit(p3, "name = Bella\ngender = 1\nface = 4\nhome_town = Hometown\n", 54);
        r = pc_guest_profile_load_or_create(p3, &p, err, sizeof(err));
        after = slurp(p3, &na);
        check("incomplete file (no ids): refused naming 'player_id', ids are NOT silently generated and the file is untouched", r == PC_GUEST_PROFILE_ERR && strstr(err, "player_id") != NULL && na == 54 && count_entries(dir3) == 1);
        free(after);
        /* an empty (0 byte) file exists but is not a profile: refused too */
        spit(p3, "", 0);
        r = pc_guest_profile_load_or_create(p3, &p, err, sizeof(err));
        check("an existing EMPTY file is refused (name missing), not replaced by a default", r == PC_GUEST_PROFILE_ERR && strstr(err, "name") != NULL && exists(p3) && count_entries(dir3) == 1);
        /* the path is a directory: cannot be read, refused, nothing created */
        {
            char dpath[560];
            snprintf(dpath, sizeof(dpath), "%s/asdir.ini", dir3);
            MKDIR(dpath);
            r = pc_guest_profile_load_or_create(dpath, &p, err, sizeof(err));
            check("a directory in place of the file is refused (ERR), nothing replaced", r == PC_GUEST_PROFILE_ERR && strstr(err, "asdir.ini") != NULL);
        }
        /* unwritable location: the parent 'directory' is a plain file */
        {
            char blocked[560], under[620];
            snprintf(blocked, sizeof(blocked), "%s/blocked", dir3);
            spit(blocked, "x", 1);
            snprintf(under, sizeof(under), "%s/blocked/save/guest.ini", dir3);
            r = pc_guest_profile_load_or_create(under, &p, err, sizeof(err));
            check("create under a path that cannot be a directory fails cleanly (ERR, message names the file)", r == PC_GUEST_PROFILE_ERR && strstr(err, "guest.ini") != NULL);
        }
    }
}

int main(int argc, char** argv) {
    if (argc < 3) {
        printf("usage: guest_g2_selftest <scratch_dir> <fixtures_dir>\n");
        return 2;
    }
    snprintf(g_dir, sizeof(g_dir), "%s", argv[1]);
    MKDIR(g_dir);
    predicate_tests(argv[2]);
    profile_tests();
    printf("RESULT passed=%d failed=%d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
