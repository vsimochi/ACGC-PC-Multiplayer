/* character_store_selftest.c - standalone native unit test of pc_character.c (player-owned characters) and pc_mp_membership.c (town memberships).
 * Usage: character_store_selftest <scratch_dir>   (everything lives under <scratch_dir>). Prints PASS:/FAIL: lines and "RESULT passed=N failed=M". */
#include "pc_character.h"
#include "pc_guest_profile.h"
#include "pc_mp_guests.h"
#include "pc_mp_membership.h"

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

static void spit(const char* p, const char* text) {
    FILE* f = fopen(p, "wb");
    if (f) {
        fwrite(text, 1, strlen(text), f);
        fclose(f);
    }
}

static int same_bytes(const char* a, const uint8_t* b, size_t bn) {
    size_t n = 0;
    uint8_t* x = slurp(a, &n);
    int ok = x != NULL && n == bn && memcmp(x, b, n) == 0;
    free(x);
    return ok;
}

static unsigned s_uuid_counter = 0;
static int fake_uuid(uint8_t out[16]) {
    memset(out, 0, 16);
    out[0] = 0xAB;
    out[15] = (uint8_t)(++s_uuid_counter);
    return 1;
}

int main(int argc, char** argv) {
    char dir[400], err[600], key[PC_CHARACTER_TOWNKEY_LEN + 1], key2[PC_CHARACTER_TOWNKEY_LEN + 1];
    PCCharacter a, b, r, imp;
    PCCharacter lst[PC_CHARACTER_MAX];
    int n, ntok = 0, rc;
    size_t sn;
    uint8_t ln1[8] = { 'T', 'o', 'w', 'n', 'A', ' ', ' ', ' ' }, ln2[8] = { 'T', 'o', 'w', 'n', 'B', ' ', ' ', ' ' };
    char ua[PC_CHARACTER_UUID_LEN + 1];

    if (argc < 2) {
        return 2;
    }
    snprintf(dir, sizeof(dir), "%s/mp", argv[1]);

    /* ---- town keys and paths ---- */
    pc_character_town_key_format(ln1, 0x1234, 0xDEADBEEFu, key);
    check("townkey: format <land 16 hex>_<id 4 hex>_<hash 8 hex>", strcmp(key, "546f776e41202020_1234_deadbeef") == 0 && strlen(key) == PC_CHARACTER_TOWNKEY_LEN);
    pc_character_town_key_format(ln2, 0x1234, 0xDEADBEEFu, key2);
    check("townkey: different land name -> different key", strcmp(key, key2) != 0);
    check("townkey: server address is not part of the key", strstr(key, ".") == NULL && strstr(key, ":") == NULL);

    /* ---- two characters: distinct stable uuids, reload round trip ---- */
    check("create: prepare_new alice", pc_character_prepare_new(dir, "alice", &a, err, sizeof(err)) == 1);
    check("create: alice is not on disk yet (prepare writes nothing)", !exists(dir) || pc_character_list(dir, lst, PC_CHARACTER_MAX, NULL) == 0);
    check("create: write_exclusive alice == 1", pc_character_write_exclusive(dir, &a, err, sizeof(err)) == 1);
    check("create: second write_exclusive refuses (0), nothing replaced", pc_character_write_exclusive(dir, &a, err, sizeof(err)) == 0);
    check("create: prepare_new bob + write", pc_character_prepare_new(dir, "bob", &b, err, sizeof(err)) == 1 && pc_character_write_exclusive(dir, &b, err, sizeof(err)) == 1);
    check("create: two characters have distinct uuids", strcmp(a.uuid, b.uuid) != 0 && strlen(a.uuid) == 32);
    check("create: two characters have distinct full identities", !(a.player_id == b.player_id && a.land_id == b.land_id && strcmp(a.name, b.name) == 0));
    check("create: wire identity default home town is GuestVil", strcmp(a.home_town, "GuestVil") == 0);
    memcpy(ua, a.uuid, sizeof(ua));
    check("reload: load(alice) round trips every field",
          pc_character_load(dir, a.uuid, &r, err, sizeof(err)) == PC_CHARACTER_OK && strcmp(r.uuid, a.uuid) == 0 && strcmp(r.name, a.name) == 0 &&
          strcmp(r.home_town, a.home_town) == 0 && r.player_id == a.player_id && r.land_id == a.land_id && r.gender == a.gender && r.face == a.face &&
          r.created == a.created && memcmp(r.name_bytes, a.name_bytes, 8) == 0 && !r.has_legacy && r.storage == PC_CHARACTER_STORAGE_STORE);
    check("reload: uuid stable across reloads", pc_character_load(dir, a.uuid, &r, err, sizeof(err)) == PC_CHARACTER_OK && strcmp(r.uuid, ua) == 0);
    check("list: two store characters", pc_character_list(dir, lst, PC_CHARACTER_MAX, NULL) == 2);
    check("load: unknown uuid is ABSENT", pc_character_load(dir, "00000000000000000000000000000000", &r, err, sizeof(err)) == PC_CHARACTER_ABSENT);
    check("load: non-uuid text is refused", pc_character_load(dir, "../../etc", &r, err, sizeof(err)) == PC_CHARACTER_ERR);

    /* ---- default ---- */
    check("default: none yet", !pc_character_default_get(dir, ua));
    check("default: set + get", pc_character_default_set(dir, a.uuid) && pc_character_default_get(dir, ua) && strcmp(ua, a.uuid) == 0);
    check("default: a non-uuid is refused", !pc_character_default_set(dir, "nope"));

    /* ---- resolution ---- */
    check("resolve: by name (case-insensitive)", pc_character_resolve(dir, "ALICE", &r, err, sizeof(err)) == PC_CHARACTER_OK && strcmp(r.uuid, a.uuid) == 0);
    {
        char pre[9];
        memcpy(pre, a.uuid, 8);
        pre[8] = '\0';
        check("resolve: by uuid prefix", pc_character_resolve(dir, pre, &r, err, sizeof(err)) == PC_CHARACTER_OK && strcmp(r.uuid, a.uuid) == 0);
    }
    check("resolve: unknown is ABSENT", pc_character_resolve(dir, "zed", &r, err, sizeof(err)) == PC_CHARACTER_ABSENT);

    /* ---- corrupt character is never touched ---- */
    {
        char cdir[500];
        const char* bad = "uuid = 11111111111111111111111111111111\nname = Evil\n";
        snprintf(cdir, sizeof(cdir), "%s/characters/11111111111111111111111111111111", dir);
        MKDIR(cdir);
        snprintf(cdir, sizeof(cdir), "%s/characters/11111111111111111111111111111111/character.ini", dir);
        spit(cdir, bad);
        n = pc_character_list(dir, lst, PC_CHARACTER_MAX, &rc);
        check("corrupt: listing skips it (2 good characters, 1 skipped)", n == 2 && rc == 1);
        check("corrupt: load reports ERR naming the file", pc_character_load(dir, "11111111111111111111111111111111", &r, err, sizeof(err)) == PC_CHARACTER_ERR && strstr(err, "character.ini") != NULL);
        check("corrupt: the file bytes are unchanged", same_bytes(cdir, (const uint8_t*)bad, strlen(bad)));
        check("corrupt: prepare_new + the bad dir leaves the bad file alone", pc_character_prepare_new(dir, "carol", &r, err, sizeof(err)) == 1 && same_bytes(cdir, (const uint8_t*)bad, strlen(bad)));
    }

    /* ---- legacy adapter + import ---- */
    {
        char legacy_ini[500], legacy_tok[500];
        PCGuestProfile gp;
        PCMpGtkFile gf;
        PCMpGtkEntry e;
        uint8_t pid[20];
        uint8_t* before;
        size_t bn;
        uint8_t* ini_before;
        size_t ibn;
        memset(&gp, 0, sizeof(gp));
        check("legacy: create roger via the legacy API", pc_guest_profile_load_or_create_in(dir, "roger", &gp, err, sizeof(err)) == PC_GUEST_PROFILE_CREATED);
        check("legacy: paths", pc_guest_profile_file_path(dir, "roger", 0, legacy_ini, sizeof(legacy_ini)) && pc_guest_profile_file_path(dir, "roger", 1, legacy_tok, sizeof(legacy_tok)));
        n = pc_character_list(dir, lst, PC_CHARACTER_MAX, NULL);
        check("legacy: roger is listed as a LEGACY character", n == 3 && lst[2].storage == PC_CHARACTER_STORAGE_LEGACY && strcmp(lst[2].legacy_profile, "roger") == 0);
        check("legacy: resolve_profile(roger) -> LEGACY", pc_character_resolve_profile(dir, "roger", &r, err, sizeof(err)) == PC_CHARACTER_OK && r.storage == PC_CHARACTER_STORAGE_LEGACY);
        check("legacy: resolve_profile(NULL) with no guest.ini is ABSENT", pc_character_resolve_profile(dir, NULL, &r, err, sizeof(err)) == PC_CHARACTER_ABSENT);
        /* two host towns, one token each in the 4-slot legacy file */
        memset(&gf, 0, sizeof(gf));
        memcpy(pid, gp.name, 0);
        {
            PCCharacter tmp;
            memset(&tmp, 0, sizeof(tmp));
            memcpy(tmp.name, gp.name, sizeof(tmp.name));
            memcpy(tmp.home_town, gp.home_town, sizeof(tmp.home_town));
            pc_guest_profile_name_bytes(&gp, tmp.name_bytes);
            pc_guest_profile_home_bytes(&gp, tmp.home_town_bytes);
            tmp.player_id = gp.player_id;
            tmp.land_id = gp.land_id;
            pc_character_home_pid_be(&tmp, pid);
        }
        memset(&e, 0, sizeof(e));
        e.present = 1;
        memcpy(e.host_land_name, ln1, 8);
        e.host_land_id = 0x1234;
        e.host_terrain_hash = 0xDEADBEEFu;
        memcpy(e.home_pid, pid, 20);
        memset(e.token, 0x11, 16);
        pc_mp_gtoken_put(&gf, &e);
        memcpy(e.host_land_name, ln2, 8);
        memset(e.token, 0x22, 16);
        pc_mp_gtoken_put(&gf, &e);
        check("legacy: token file saved", pc_mp_gtoken_save(legacy_tok, &gf) == PC_MP_GST_OK);
        before = slurp(legacy_tok, &bn);
        ini_before = slurp(legacy_ini, &ibn);
        check("import: legacy profile imported (2 token entries)", pc_character_import_legacy(dir, "Roger", &imp, &ntok, err, sizeof(err)) == 1 && ntok == 2);
        check("import: the imported character has the SAME wire identity", strcmp(imp.name, gp.name) == 0 && strcmp(imp.home_town, gp.home_town) == 0 &&
              imp.player_id == gp.player_id && imp.land_id == gp.land_id && imp.gender == gp.gender && imp.face == gp.face && strcmp(imp.legacy_profile, "roger") == 0);
        check("import: legacy token bytes untouched", before != NULL && same_bytes(legacy_tok, before, bn));
        check("import: legacy ini bytes untouched", ini_before != NULL && same_bytes(legacy_ini, ini_before, ibn));
        check("import: a second import refuses", pc_character_import_legacy(dir, "roger", &r, &ntok, err, sizeof(err)) == 0 && strstr(err, "already imported") != NULL);
        {
            char t1[500], t2[500], m1[500];
            PCMpGtkFile one;
            int ur = 0;
            pc_character_town_key_format(ln1, 0x1234, 0xDEADBEEFu, key);
            pc_character_town_key_format(ln2, 0x1234, 0xDEADBEEFu, key2);
            check("tokens: two towns -> two distinct token paths for ONE character", pc_character_token_path(dir, imp.uuid, key, t1, sizeof(t1)) &&
                  pc_character_token_path(dir, imp.uuid, key2, t2, sizeof(t2)) && strcmp(t1, t2) != 0 && strstr(t1, imp.uuid) != NULL && strstr(t2, imp.uuid) != NULL);
            check("tokens: both store token files exist", exists(t1) && exists(t2));
            memset(&one, 0, sizeof(one));
            check("tokens: town A file holds exactly the town A token (byte-identical 16 B)", pc_mp_gtoken_load(t1, &one, &ur) && !ur && one.e[0].present && !one.e[1].present &&
                  one.e[0].token[0] == 0x11 && one.e[0].token[15] == 0x11 && memcmp(one.e[0].home_pid, pid, 20) == 0 &&
                  pc_mp_gtoken_find(&one, ln1, 0x1234, 0xDEADBEEFu, pid) == 0 && pc_mp_gtoken_find(&one, ln2, 0x1234, 0xDEADBEEFu, pid) < 0);
            memset(&one, 0, sizeof(one));
            check("tokens: town B file holds exactly the town B token", pc_mp_gtoken_load(t2, &one, &ur) && one.e[0].token[0] == 0x22 && pc_mp_gtoken_find(&one, ln2, 0x1234, 0xDEADBEEFu, pid) == 0);
            check("tokens: identity is the same in both towns (home pid in both files)", memcmp(one.e[0].home_pid, pid, 20) == 0);
            check("membership: membership.ini role=guest written per town", pc_character_membership_path(dir, imp.uuid, key, m1, sizeof(m1)) && exists(m1));
        }
        check("resolve: --guest-profile roger now resolves to the STORE character", pc_character_resolve_profile(dir, "roger", &r, err, sizeof(err)) == PC_CHARACTER_OK &&
              r.storage == PC_CHARACTER_STORAGE_STORE && strcmp(r.uuid, imp.uuid) == 0);
        check("resolve: unrelated profile still not imported", pc_character_resolve_profile(dir, "nobody", &r, err, sizeof(err)) == PC_CHARACTER_ABSENT);
        n = pc_character_list(dir, lst, PC_CHARACTER_MAX, NULL);
        check("list: the imported profile is no longer listed twice (3 store characters)", n == 3);
        check("resolve: character by legacy profile name", pc_character_resolve(dir, "roger", &r, err, sizeof(err)) == PC_CHARACTER_OK && strcmp(r.uuid, imp.uuid) == 0);
        {
            char bt[500];
            const char* junk = "garbage";
            PCGuestProfile gq;
            PCCharacter x;
            check("import: second legacy profile", pc_guest_profile_load_or_create_in(dir, "dave", &gq, err, sizeof(err)) == PC_GUEST_PROFILE_CREATED);
            pc_guest_profile_file_path(dir, "dave", 1, bt, sizeof(bt));
            spit(bt, junk);
            check("import: corrupt legacy token file -> refused, file bytes unchanged, no character created",
                  pc_character_import_legacy(dir, "dave", &x, &ntok, err, sizeof(err)) == 0 && same_bytes(bt, (const uint8_t*)junk, strlen(junk)) &&
                  pc_character_resolve_profile(dir, "dave", &r, err, sizeof(err)) == PC_CHARACTER_OK && r.storage == PC_CHARACTER_STORAGE_LEGACY);
        }
        free(before);
        free(ini_before);
    }

    /* ---- the default guest.ini import + uuid seam ---- */
    {
        PCGuestProfile gd;
        PCCharacter d;
        pc_character_test_set_uuid_source(fake_uuid);
        check("default: create guest.ini", pc_guest_profile_load_or_create_in(dir, NULL, &gd, err, sizeof(err)) == PC_GUEST_PROFILE_CREATED);
        check("default: import of '' (guest.ini) works with the seeded uuid", pc_character_import_legacy(dir, "", &d, &ntok, err, sizeof(err)) == 1 && d.legacy_profile[0] == '\0' && d.has_legacy &&
              strcmp(d.uuid, "ab000000000000000000000000000001") == 0 && ntok == 0);
        check("default: resolve_profile(NULL) -> the store character", pc_character_resolve_profile(dir, NULL, &r, err, sizeof(err)) == PC_CHARACTER_OK && r.storage == PC_CHARACTER_STORAGE_STORE);
        pc_character_test_set_uuid_source(NULL);
        (void)sn;
    }

    /* ---- membership ---- */
    {
        PCMpTownKey ta, tb, tc;
        uint8_t res[4][20];
        uint8_t ex_a[4] = { 1, 1, 0, 0 }, ex_b[4] = { 1, 0, 0, 0 };
        static PCMpGuestFile gf;
        PCMpMembership m;
        uint8_t pid_x[20], pid_y[20], pid_new[20];
        PCMpMembership rows[16];
        char tk[PC_CHARACTER_TOWNKEY_LEN + 1];
        int i;
        memset(&ta, 0, sizeof(ta));
        memset(&tb, 0, sizeof(tb));
        memset(&tc, 0, sizeof(tc));
        memcpy(ta.land_name, ln1, 8);
        ta.land_id = 7;
        ta.terrain_hash = 1;
        memcpy(tb.land_name, ln2, 8);
        tb.land_id = 7;
        tb.terrain_hash = 1;
        memcpy(tc.land_name, "TownC   ", 8);
        tc.land_id = 9;
        tc.terrain_hash = 2;
        memset(pid_x, 0x41, 20);
        memset(pid_y, 0x42, 20);
        memset(pid_new, 0x43, 20);
        memset(res, 0, sizeof(res));
        memcpy(res[0], pid_x, 20);
        memcpy(res[1], pid_y, 20);
        memset(&gf, 0, sizeof(gf));
        /* in town B: pid_x is a GUEST (slot 0, confirmed); pid_y is a guest of town B slot 3 unconfirmed */
        gf.e[0].present = 1;
        gf.e[0].confirmed = 1;
        memcpy(gf.e[0].pid, pid_x, 20);
        memcpy(gf.e[0].town_land_name, tb.land_name, 8);
        gf.e[0].town_land_id = tb.land_id;
        gf.e[0].town_terrain_hash = tb.terrain_hash;
        gf.e[3].present = 1;
        memcpy(gf.e[3].pid, pid_y, 20);
        memcpy(gf.e[3].town_land_name, tb.land_name, 8);
        gf.e[3].town_land_id = tb.land_id;
        gf.e[3].town_terrain_hash = tb.terrain_hash;
        check("member: pid_x is RESIDENT in town A", pc_mp_membership_lookup(pid_x, &ta, (const uint8_t(*)[20])res, ex_a, &gf, &m) == PC_MP_MEMBER_RESIDENT && m.res_index == 0);
        /* town B has its own residents (only slot 0 = a different person): */
        {
            uint8_t resb[4][20];
            memset(resb, 0, sizeof(resb));
            memset(resb[0], 0x55, 20);
            check("member: the SAME pid is a GUEST (confirmed, slot 0) in town B", pc_mp_membership_lookup(pid_x, &tb, (const uint8_t(*)[20])resb, ex_b, &gf, &m) == PC_MP_MEMBER_GUEST &&
                  m.guest_slot == 0 && m.confirmed == 1);
            check("member: guest slot 3 unconfirmed in town B", pc_mp_membership_lookup(pid_y, &tb, (const uint8_t(*)[20])resb, ex_b, &gf, &m) == PC_MP_MEMBER_GUEST && m.guest_slot == 3 && m.confirmed == 0);
        }
        check("member: unknown pid is NONE", pc_mp_membership_lookup(pid_new, &ta, (const uint8_t(*)[20])res, ex_a, &gf, &m) == PC_MP_MEMBER_NONE);
        check("member: guest table is keyed per town (pid_x NOT a guest in town A/C)", pc_mp_membership_lookup(pid_y, &tc, (const uint8_t(*)[20])res, ex_b, &gf, &m) == PC_MP_MEMBER_NONE);
        check("member: a NON-existent resident slot never matches", pc_mp_membership_lookup(pid_y, &ta, (const uint8_t(*)[20])res, ex_b, &gf, &m) == PC_MP_MEMBER_NONE);
        /* ambiguity: pid_x resident of town B and guest of town B */
        {
            uint8_t resb[4][20];
            memset(resb, 0, sizeof(resb));
            memcpy(resb[2], pid_x, 20);
            ex_b[2] = 1;
            check("member: resident AND guest of the same town -> AMBIGUOUS", pc_mp_membership_lookup(pid_x, &tb, (const uint8_t(*)[20])resb, ex_b, &gf, &m) == PC_MP_MEMBER_AMBIGUOUS &&
                  m.res_index == 2 && m.guest_slot == 0);
            check("member: list of town B has 2 resident rows + 2 guest rows", pc_mp_membership_list(&tb, (const uint8_t(*)[20])resb, ex_b, &gf, rows, 16) == 4);
        }
        check("member: list town A = 2 residents, 0 guests", pc_mp_membership_list(&ta, (const uint8_t(*)[20])res, ex_a, &gf, rows, 16) == 2);
        check("member: list town C has no guests (only the supplied residents)", pc_mp_membership_list(&tc, (const uint8_t(*)[20])res, ex_a, &gf, rows, 16) == 2);
        /* five towns keep five tokens: store files, no eviction (the legacy 4-slot file would recycle the oldest) */
        {
            PCCharacter fc;
            char tp[500];
            char pre[9];
            int k, all_ok = 1;
            uint8_t land[8];
            check("five: prepare + write a fresh character", pc_character_prepare_new(dir, "erin", &fc, err, sizeof(err)) == 1 && pc_character_write_exclusive(dir, &fc, err, sizeof(err)) == 1);
            (void)pre;
            for (k = 0; k < 5; k++) {
                PCMpGtkFile f1;
                PCMpGtkEntry e;
                memset(land, 0, 8);
                land[0] = (uint8_t)('A' + k);
                pc_character_town_key_format(land, 1, (uint32_t)k, tk);
                memset(&f1, 0, sizeof(f1));
                memset(&e, 0, sizeof(e));
                e.present = 1;
                memcpy(e.host_land_name, land, 8);
                e.host_land_id = 1;
                e.host_terrain_hash = (uint32_t)k;
                pc_character_home_pid_be(&fc, e.home_pid);
                memset(e.token, 0x70 + k, 16);
                pc_mp_gtoken_put(&f1, &e);
                all_ok = all_ok && pc_character_token_path(dir, fc.uuid, tk, tp, sizeof(tp)) && pc_mp_gtoken_save(tp, &f1) == PC_MP_GST_OK;
            }
            for (k = 0; k < 5; k++) {
                PCMpGtkFile f1;
                int ur = 0;
                memset(land, 0, 8);
                land[0] = (uint8_t)('A' + k);
                pc_character_town_key_format(land, 1, (uint32_t)k, tk);
                all_ok = all_ok && pc_character_token_path(dir, fc.uuid, tk, tp, sizeof(tp)) && pc_mp_gtoken_load(tp, &f1, &ur) && f1.e[0].present && f1.e[0].token[0] == (uint8_t)(0x70 + k);
            }
            check("five: five towns keep five distinct tokens (no eviction)", all_ok);
            (void)i;
        }
    }

    printf("RESULT passed=%d failed=%d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
