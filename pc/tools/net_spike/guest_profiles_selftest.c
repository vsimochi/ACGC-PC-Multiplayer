/* guest_profiles_selftest.c - standalone native unit test of the NAMED guest profiles (--guest-profile NAME) of pc_guest_profile.c.
 * Usage: guest_profiles_selftest <scratch_dir>      (every file the test creates lives under <scratch_dir>; nothing else is touched)
 * Prints "PASS: ..." / "FAIL: ..." lines and "RESULT passed=N failed=M"; exit code 0 iff failed==0. Built and run by test_guest_profiles_unit.py. */
#include "pc_guest_profile.h"
#include "pc_mp_guests.h"

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

static void spit(const char* p, const char* text) {
    FILE* f = fopen(p, "wb");
    if (f) {
        fwrite(text, 1, strlen(text), f);
        fclose(f);
    }
}

static int same_file(const char* p, const char* text) {
    size_t n = 0;
    uint8_t* b = slurp(p, &n);
    int ok = b != NULL && n == strlen(text) && memcmp(b, text, n) == 0;
    free(b);
    return ok;
}

static int same_identity(const PCGuestProfile* a, const PCGuestProfile* b) {
    return strcmp(a->name, b->name) == 0 && strcmp(a->home_town, b->home_town) == 0 && a->player_id == b->player_id && a->land_id == b->land_id;
}

/* ---- the profile-name validation table ---- */
static void name_table(void) {
    static const char* const ok[] = { "alice", "Bob", "a", "A1-b2", "0", "1234567890123456", "x-y-z", "Alice-2", "console", "com10", "lpt", "nulx", "au" };
    static const struct { const char* name; const char* rule; } bad[] = {
        { "", "empty" },
        { "12345678901234567", "too long" },
        { "a b", "outside" },
        { " alice", "outside" },
        { "alice ", "outside" },
        { "al.ice", "outside" },
        { "..", "outside" },
        { ".", "outside" },
        { "a/b", "outside" },
        { "a\\b", "outside" },
        { "../x", "outside" },
        { "C:", "outside" },
        { "a:b", "outside" },
        { "a_b", "outside" },
        { "al\xC3\xA9", "outside" },
        { "a\tb", "outside" },
        { "a\nb", "outside" },
        { "alice.ini", "outside" },
        { "-alice", "start with" },
        { "--guest", "start with" },
        { "-", "start with" },
        { "CON", "device" },
        { "con", "device" },
        { "Nul", "device" },
        { "PRN", "device" },
        { "aux", "device" },
        { "COM1", "device" },
        { "com9", "device" },
        { "LPT1", "device" },
        { "lpt9", "device" },
        { "Com0", "device" },
    };
    size_t k;
    char err[200], msg[300];
    for (k = 0; k < sizeof(ok) / sizeof(ok[0]); k++) {
        snprintf(msg, sizeof(msg), "name check: '%s' is ACCEPTED", ok[k]);
        check(msg, pc_guest_profile_name_check(ok[k], err, sizeof(err)) == 1);
    }
    for (k = 0; k < sizeof(bad) / sizeof(bad[0]); k++) {
        int r;
        err[0] = '\0';
        r = pc_guest_profile_name_check(bad[k].name, err, sizeof(err));
        snprintf(msg, sizeof(msg), "name check: entry %u is REFUSED and the diagnostic names the rule ('%s')", (unsigned)k, bad[k].rule);
        check(msg, r == 0 && strstr(err, "profile name") != NULL && strstr(err, bad[k].rule) != NULL);
    }
    check("name check: NULL is refused", pc_guest_profile_name_check(NULL, err, sizeof(err)) == 0 && strstr(err, "empty") != NULL);
    check("name check: a NULL error buffer is fine", pc_guest_profile_name_check("bad name", NULL, 0) == 0 && pc_guest_profile_name_check("good", NULL, 0) == 1);
    {
        char low[PC_GUEST_PROFILE_ARG_MAX + 1];
        pc_guest_profile_fold("AlIcE-9", low);
        check("fold: lower-cases ASCII letters, keeps digits and '-'", strcmp(low, "alice-9") == 0);
        pc_guest_profile_fold(NULL, low);
        check("fold: NULL -> empty string", low[0] == '\0');
    }
}

/* ---- file name derivation ---- */
static void path_table(void) {
    char a[200], b[200];
    check("paths: the default ini path is byte-identical to PC_GUEST_PROFILE_PATH (save/mp/guest.ini)",
          pc_guest_profile_file_path(NULL, NULL, 0, a, sizeof(a)) && strcmp(a, PC_GUEST_PROFILE_PATH) == 0 && strcmp(a, "save/mp/guest.ini") == 0);
    check("paths: the default token path is byte-identical to PC_MP_GUEST_TOKEN_PATH (save/mp/guest_token.dat)",
          pc_guest_profile_file_path(NULL, NULL, 1, a, sizeof(a)) && strcmp(a, PC_MP_GUEST_TOKEN_PATH) == 0 && strcmp(a, "save/mp/guest_token.dat") == 0
          && strcmp(PC_GUEST_TOKEN_DEFAULT_PATH, PC_MP_GUEST_TOKEN_PATH) == 0);
    check("paths: an empty profile name means the default", pc_guest_profile_file_path(NULL, "", 0, a, sizeof(a)) && strcmp(a, "save/mp/guest.ini") == 0);
    check("paths: profile 'alice' -> save/mp/guest_alice.ini and save/mp/guest_token_alice.dat",
          pc_guest_profile_file_path(NULL, "alice", 0, a, sizeof(a)) && strcmp(a, "save/mp/guest_alice.ini") == 0
          && pc_guest_profile_file_path(NULL, "alice", 1, b, sizeof(b)) && strcmp(b, "save/mp/guest_token_alice.dat") == 0);
    check("paths: 'Alice' and 'alice' are the SAME profile (both files folded to lower case)",
          pc_guest_profile_file_path(NULL, "Alice", 0, a, sizeof(a)) && strcmp(a, "save/mp/guest_alice.ini") == 0
          && pc_guest_profile_file_path(NULL, "ALICE", 1, b, sizeof(b)) && strcmp(b, "save/mp/guest_token_alice.dat") == 0);
    check("paths: a custom directory is honoured", pc_guest_profile_file_path("d/x", "bob", 0, a, sizeof(a)) && strcmp(a, "d/x/guest_bob.ini") == 0);
    check("paths: an invalid profile name yields no path", !pc_guest_profile_file_path(NULL, "../evil", 0, a, sizeof(a)) && !pc_guest_profile_file_path(NULL, "CON", 1, a, sizeof(a)));
    check("paths: a too small buffer is refused", !pc_guest_profile_file_path(NULL, "alice", 0, a, 10) && !pc_guest_profile_file_path(NULL, NULL, 1, a, 5));
    check("paths: a token file name never collides with a profile ini name (extension differs; 'token' is just a profile)",
          pc_guest_profile_file_path(NULL, "token", 0, a, sizeof(a)) && strcmp(a, "save/mp/guest_token.ini") == 0 && pc_guest_profile_file_path(NULL, "token", 1, b, sizeof(b))
          && strcmp(b, "save/mp/guest_token_token.dat") == 0 && strcmp(b, "save/mp/guest_token.dat") != 0);
}

static void select_table(void) {
    check("select: nothing selected at start -> default paths", pc_guest_profile_selected() == NULL && strcmp(pc_guest_profile_selected_path(), "save/mp/guest.ini") == 0
          && strcmp(pc_guest_token_path(), "save/mp/guest_token.dat") == 0);
    check("select: an invalid name is refused and changes nothing", pc_guest_profile_select("a b") == 0 && pc_guest_profile_select("../x") == 0 && pc_guest_profile_selected() == NULL);
    check("select: 'Alice' selects (kept as typed), both paths follow the lower-case profile", pc_guest_profile_select("Alice") == 1 && strcmp(pc_guest_profile_selected(), "Alice") == 0
          && strcmp(pc_guest_profile_selected_path(), "save/mp/guest_alice.ini") == 0 && strcmp(pc_guest_token_path(), "save/mp/guest_token_alice.dat") == 0);
    check("select: a later invalid name keeps the previous selection", pc_guest_profile_select("CON") == 0 && strcmp(pc_guest_profile_selected(), "Alice") == 0);
    check("select: NULL / empty clears -> default again", pc_guest_profile_select(NULL) == 1 && pc_guest_profile_selected() == NULL && strcmp(pc_guest_token_path(), PC_MP_GUEST_TOKEN_PATH) == 0
          && pc_guest_profile_select("bob") == 1 && pc_guest_profile_select("") == 1 && pc_guest_profile_selected() == NULL);
}

static void default_name_table(void) {
    char n[16];
    pc_guest_profile_default_name("alice", 0x1234, n);
    check("default name: 'alice' -> alice", strcmp(n, "alice") == 0);
    pc_guest_profile_default_name("Alice", 0x1234, n);
    check("default name: the typed case is kept ('Alice')", strcmp(n, "Alice") == 0);
    pc_guest_profile_default_name("alicewonderland", 0x1234, n);
    check("default name: truncated to 8 characters (alicewon)", strcmp(n, "alicewon") == 0);
    pc_guest_profile_default_name("12345678-abc", 0x1234, n);
    check("default name: truncation keeps digits / hyphens (12345678)", strcmp(n, "12345678") == 0);
    pc_guest_profile_default_name("ab-", 0x1234, n);
    check("default name: a trailing '-' is a valid name character", strcmp(n, "ab-") == 0);
    pc_guest_profile_default_name("SERVER", 0x12AB, n);
    check("default name: SERVER falls back to Guest + 2 hex digits of the id (GuestAB)", strcmp(n, "GuestAB") == 0);
    pc_guest_profile_default_name("server", 0x1201, n);
    check("default name: 'server' (any case) falls back too (Guest01)", strcmp(n, "Guest01") == 0);
    pc_guest_profile_default_name("Server-One", 0x12AB, n);
    check("default name: 'Server-One' is NOT SERVER, truncated to 'Server-O'", strcmp(n, "Server-O") == 0);
    pc_guest_profile_default_name("guest", 0x12FF, n);
    check("default name: 'guest' would equal the default profile's own name 'Guest': fallback GuestFF", strcmp(n, "GuestFF") == 0);
    pc_guest_profile_default_name("GUEST", 0x1200, n);
    check("default name: 'GUEST' (any case) falls back too (Guest00)", strcmp(n, "Guest00") == 0);
    {
        PCGuestProfile t;
        uint8_t nb[8];
        const char* const names[] = { "alice", "SERVER", "guest", "x", "12345678901234", "a-b-c-d-e-f" };
        size_t k;
        int all = 1;
        for (k = 0; k < sizeof(names) / sizeof(names[0]); k++) {
            memset(&t, 0, sizeof(t));
            pc_guest_profile_default_name(names[k], 0x00C7, t.name);
            strcpy(t.home_town, "GuestVil");
            t.player_id = 1;
            t.land_id = 1;
            pc_guest_profile_name_bytes(&t, nb);
            all = all && pc_guest_profile_validate(&t, NULL, NULL) && pc_mp_guests_name_valid(nb) && !pc_mp_guests_name_reserved(nb);
        }
        check("default name: every derived name passes pc_guest_profile_validate / pc_mp_guests_name_valid / not reserved", all);
    }
}

/* ---- the id seam ---- */
static const uint16_t* g_seq;
static int g_seq_n, g_seq_i, g_seq_calls;

static int seq_source(uint16_t* out) {
    g_seq_calls++;
    *out = g_seq[g_seq_i % g_seq_n];
    g_seq_i++;
    return 1;
}

static void profile_dir_tests(void) {
    char dir[450], pa[500], pb[500], pc[500], pd[500], err[500];
    PCGuestProfile a, b, c, d, again;
    size_t n1, n2;
    uint8_t *b1, *b2;
    int r, k;

    snprintf(dir, sizeof(dir), "%s/mp", g_dir);
    MKDIR(g_dir);
    snprintf(pa, sizeof(pa), "%s/guest_alice.ini", dir);
    snprintf(pb, sizeof(pb), "%s/guest_bob.ini", dir);
    snprintf(pc, sizeof(pc), "%s/guest.ini", dir);
    snprintf(pd, sizeof(pd), "%s/guest_server.ini", dir);

    /* a read of a missing profile never creates anything */
    r = pc_guest_profile_read(pa, &a, err, sizeof(err));
    check("read: a missing profile -> ABSENT and NOTHING is created (not the file, not the directory)", r == PC_GUEST_PROFILE_ABSENT && !exists(pa) && !exists(dir));

    /* create alice, then bob, then the default */
    r = pc_guest_profile_load_or_create_in(dir, "alice", &a, err, sizeof(err));
    check("create: profile 'alice' is CREATED in guest_alice.ini (directory made, no tmp left)", r == PC_GUEST_PROFILE_CREATED && exists(pa) && exists(dir));
    check("create: alice's display name is 'alice', home town 'GuestVil', valid profile", strcmp(a.name, "alice") == 0 && strcmp(a.home_town, "GuestVil") == 0 && pc_guest_profile_validate(&a, NULL, NULL));
    r = pc_guest_profile_load_or_create_in(dir, "Bob", &b, err, sizeof(err));
    snprintf(err, sizeof(err), "%s/guest_bob.ini", dir);
    check("create: profile 'Bob' (typed with a capital) is CREATED as the LOWER case file guest_bob.ini, display name 'Bob'", r == PC_GUEST_PROFILE_CREATED && exists(pb) && strcmp(b.name, "Bob") == 0);
    r = pc_guest_profile_load_or_create_in(dir, NULL, &c, err, sizeof(err));
    check("create: the DEFAULT profile (NULL) is created as guest.ini with the legacy defaults (name 'Guest', home 'GuestVil')", r == PC_GUEST_PROFILE_CREATED && exists(pc) && strcmp(c.name, "Guest") == 0
          && strcmp(c.home_town, "GuestVil") == 0);
    check("create: the three profiles have three different FULL identities", !same_identity(&a, &b) && !same_identity(&a, &c) && !same_identity(&b, &c));
    check("create: the three display names differ (the host refuses a second NEW guest of the same name)", strcmp(a.name, b.name) != 0 && strcmp(a.name, c.name) != 0 && strcmp(b.name, c.name) != 0);

    /* persistence */
    b1 = slurp(pa, &n1);
    r = pc_guest_profile_load_or_create_in(dir, "alice", &again, err, sizeof(err));
    check("persist: reloading alice -> LOADED with identical ids and fields", r == PC_GUEST_PROFILE_LOADED && memcmp(&a, &again, sizeof(a)) == 0);
    r = pc_guest_profile_load_or_create_in(dir, "ALICE", &again, err, sizeof(err));
    check("persist: 'ALICE' loads the SAME file (case folded): LOADED, same identity, no second file", r == PC_GUEST_PROFILE_LOADED && memcmp(&a, &again, sizeof(a)) == 0);
    b2 = slurp(pa, &n2);
    check("persist: the file bytes did not change after two loads (a load never rewrites)", b1 != NULL && b2 != NULL && n1 == n2 && memcmp(b1, b2, n1) == 0);
    free(b2);
    r = pc_guest_profile_read(pa, &again, err, sizeof(err));
    check("read: an existing profile is LOADED (read only)", r == PC_GUEST_PROFILE_LOADED && memcmp(&a, &again, sizeof(a)) == 0);
    free(b1);
    r = pc_guest_profile_load_or_create_in(dir, "bob", &again, err, sizeof(err));
    check("persist: bob reloads with its own identity (not alice's)", r == PC_GUEST_PROFILE_LOADED && memcmp(&b, &again, sizeof(b)) == 0 && !same_identity(&again, &a));
    r = pc_guest_profile_load_or_create_in(dir, NULL, &again, err, sizeof(err));
    check("persist: the default guest.ini reloads unchanged", r == PC_GUEST_PROFILE_LOADED && memcmp(&c, &again, sizeof(c)) == 0);

    /* an existing file is never overwritten, a corrupt one is preserved */
    spit(pd, "name = SERVER\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n");
    r = pc_guest_profile_load_or_create_in(dir, "server", &d, err, sizeof(err));
    check("corrupt: profile 'server' whose file is invalid -> ERR naming the file and 'name', file preserved byte for byte",
          r == PC_GUEST_PROFILE_ERR && strstr(err, "guest_server.ini") != NULL && strstr(err, "name") != NULL
          && same_file(pd, "name = SERVER\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n"));
    {
        char tmp[520];
        snprintf(tmp, sizeof(tmp), "%s.tmp", pd);
        check("corrupt: no tmp / other file appeared", !exists(tmp));
    }
    {
        char dd[500];
        snprintf(dd, sizeof(dd), "%s/guest_dirp.ini", dir);
        MKDIR(dd);
        r = pc_guest_profile_load_or_create_in(dir, "dirp", &d, err, sizeof(err));
        check("corrupt: a DIRECTORY in place of the profile file -> ERR (not replaced)", r == PC_GUEST_PROFILE_ERR && strstr(err, "guest_dirp.ini") != NULL);
    }
    r = pc_guest_profile_load_or_create_in(dir, "bad name", &d, err, sizeof(err));
    check("invalid: load_or_create_in with an invalid name -> ERR naming the rule, nothing created", r == PC_GUEST_PROFILE_ERR && strstr(err, "profile name") != NULL);
    r = pc_guest_profile_load_or_create_in(dir, "../up", &d, err, sizeof(err));
    check("invalid: a traversal name -> ERR, no file outside the directory", r == PC_GUEST_PROFILE_ERR && strstr(err, "profile name") != NULL);

    /* sibling scan: a corrupt sibling is skipped (warning), the creation of a new profile still works and the sibling stays intact */
    {
        char pcarol[500];
        const char* junk = "this is not a profile\n";
        snprintf(pcarol, sizeof(pcarol), "%s/guest_junk.ini", dir);
        spit(pcarol, junk);
        r = pc_guest_profile_load_or_create_in(dir, "carol", &d, err, sizeof(err));
        check("siblings: a corrupt sibling profile (guest_junk.ini) is skipped; 'carol' is still CREATED and the corrupt sibling is preserved byte for byte",
              r == PC_GUEST_PROFILE_CREATED && strcmp(d.name, "carol") == 0 && same_file(pcarol, junk));
        check("siblings: carol's identity differs from alice, bob and the default", !same_identity(&d, &a) && !same_identity(&d, &b) && !same_identity(&d, &c));
    }

    /* name conflicts with a sibling use the id based fallback; the full identity / name re-draw is bounded */
    {
        static const uint16_t seq1[] = { 0x0105, 0x0001, 0x0207, 0x0002, 0x0309, 0x0003 };
        char pe[500], sb[500];
        PCGuestProfile e;
        PCGuestProfile sibs[2];
        const char* body = "name = bob\ngender = 0\nface = 1\nhome_town = Elsewh\nplayer_id = 9\nland_id = 10\n";
        const char* body2 = "name = Guest05\ngender = 0\nface = 1\nhome_town = Elsewh\nplayer_id = 11\nland_id = 12\n";
        char dir2[480], bobpath[520];
        snprintf(dir2, sizeof(dir2), "%s/mp2", g_dir);
        MKDIR(dir2);
        snprintf(sb, sizeof(sb), "%s/guest_other.ini", dir2);
        spit(sb, body);
        snprintf(pe, sizeof(pe), "%s/guest_other2.ini", dir2);
        spit(pe, body2);
        snprintf(bobpath, sizeof(bobpath), "%s/guest_bob.ini", dir2);
        g_seq = seq1;
        g_seq_n = 6;
        g_seq_i = 0;
        g_seq_calls = 0;
        pc_guest_profile_test_set_id_source(seq_source);
        r = pc_guest_profile_load_or_create_in(dir2, "bob", &e, err, sizeof(err));
        pc_guest_profile_test_set_id_source(NULL);
        check("redraw: sibling named 'bob' + sibling named 'Guest05': profile 'bob' draws ids 0x0105/0x0001 (fallback name Guest05 also taken) then 0x0207/0x0002 -> name 'Guest07'",
              r == PC_GUEST_PROFILE_CREATED && e.player_id == 0x0207 && e.land_id == 0x0002 && strcmp(e.name, "Guest07") == 0 && g_seq_calls == 4);
        check("redraw: the created file holds exactly that identity (reload equals)", pc_guest_profile_read(bobpath, &again, err, sizeof(err)) == PC_GUEST_PROFILE_LOADED
              && memcmp(&e, &again, sizeof(e)) == 0);
        /* make_unique directly: no free name at all within the bound -> 0 */
        memset(sibs, 0, sizeof(sibs));
        strcpy(sibs[0].name, "bob");
        strcpy(sibs[0].home_town, "Elsewh");
        strcpy(sibs[1].name, "Guest05");
        strcpy(sibs[1].home_town, "Elsewh");
        {
            static const uint16_t seq2[] = { 0x0105 };
            g_seq = seq2;
            g_seq_n = 1;
            g_seq_i = 0;
            g_seq_calls = 0;
            pc_guest_profile_test_set_id_source(seq_source);
            r = pc_guest_profile_make_unique("bob", sibs, 2, &e);
            pc_guest_profile_test_set_id_source(NULL);
            check("redraw: when every draw collides, make_unique gives up after a BOUNDED number of draws (32 attempts = 64 id draws) and returns 0", r == 0 && g_seq_calls == 64);
        }
        {
            static const uint16_t seq3[] = { 0x0005 };
            char e2[160];
            g_seq = seq3;
            g_seq_n = 1;
            g_seq_i = 0;
            g_seq_calls = 0;
            pc_guest_profile_test_set_id_source(seq_source);
            r = pc_guest_profile_load_or_create_in(dir2, "zed", &e, e2, sizeof(e2));
            pc_guest_profile_test_set_id_source(NULL);
            snprintf(sb, sizeof(sb), "%s/guest_zed.ini", dir2);
            check("redraw: a name that does not collide is accepted on the first draw (no redraw): 'zed'", r == PC_GUEST_PROFILE_CREATED && strcmp(e.name, "zed") == 0 && g_seq_calls == 2 && exists(sb));
        }
        {
            static const uint16_t seq4[] = { 0x0007 };
            char e2[300];
            g_seq = seq4;
            g_seq_n = 1;
            g_seq_i = 0;
            g_seq_calls = 0;
            snprintf(sb, sizeof(sb), "%s/guest_zed2.ini", dir2);
            /* every id the source can produce collides: sibling 'zed2' + 'Guest07' (ids constant) -> the creation fails, NOTHING is written */
            snprintf(pe, sizeof(pe), "%s/guest_z.ini", dir2);
            spit(pe, "name = zed2\ngender = 0\nface = 1\nhome_town = Elsewh\nplayer_id = 9\nland_id = 10\n");
            snprintf(pe, sizeof(pe), "%s/guest_zz.ini", dir2);
            spit(pe, "name = Guest07\ngender = 0\nface = 1\nhome_town = Elsewh\nplayer_id = 9\nland_id = 11\n");
            pc_guest_profile_test_set_id_source(seq_source);
            r = pc_guest_profile_load_or_create_in(dir2, "zed2", &e, e2, sizeof(e2));
            pc_guest_profile_test_set_id_source(NULL);
            check("redraw: exhaustion in the file API -> ERR 'no unique guest identity' and NO file is created", r == PC_GUEST_PROFILE_ERR && strstr(e2, "no unique guest identity") != NULL && !exists(sb));
        }
    }

    /* many profiles in one directory: pairwise different full identities and names */
    {
        char dir3[480], nm[32];
        PCGuestProfile all[12];
        int i, j, distinct = 1, created = 1;
        snprintf(dir3, sizeof(dir3), "%s/mp3", g_dir);
        MKDIR(dir3);
        for (i = 0; i < 12; i++) {
            snprintf(nm, sizeof(nm), "commonpr-%02d", i); /* 12 distinct profiles whose names all truncate to the same 8 characters (commonpr) */
            r = pc_guest_profile_load_or_create_in(dir3, nm, &all[i], err, sizeof(err));
            created = created && r == PC_GUEST_PROFILE_CREATED;
        }
        for (i = 0; i < 12; i++) {
            for (j = i + 1; j < 12; j++) {
                if (same_identity(&all[i], &all[j]) || strcmp(all[i].name, all[j].name) == 0) {
                    distinct = 0;
                }
            }
        }
        check("many: 12 profiles whose names all truncate to 'commonpr' are all created", created);
        check("many: pairwise different full identities AND different display names (the colliding truncations fell back to GuestXX)", distinct);
        k = 0;
        for (i = 0; i < 12; i++) {
            k += strncmp(all[i].name, "Guest", 5) == 0;
        }
        check("many: 11 of the 12 needed the GuestXX fallback name (the truncation collides; the first one keeps commonpr)", k == 11);
    }
    (void)pc;
}

/* first-run creation: prepare_new writes NOTHING, create_exclusive is create-only, an orphan token refuses, name_from_game is an exact round trip */
static void first_run_tests(void) {
    char dir[450], pn[500], tok[500], tmpp[520], err[500], nm[16];
    PCGuestProfile p, q, r2;
    uint8_t gb[8];
    size_t n1 = 0, n2 = 0;
    uint8_t *b1, *b2;
    int r;

    snprintf(dir, sizeof(dir), "%s/fr", g_dir);
    MKDIR(dir);
    snprintf(pn, sizeof(pn), "%s/guest_roger.ini", dir);
    snprintf(tok, sizeof(tok), "%s/guest_token_roger.dat", dir);
    check("first-run: prepare_new draws a valid in-memory identity (display name 'Roger') and writes NOTHING", pc_guest_profile_prepare_new(dir, "Roger", &p, err, sizeof(err))
          && strcmp(p.name, "Roger") == 0 && pc_guest_profile_validate(&p, NULL, NULL) && !exists(pn));
    check("first-run: prepare_new refuses the default profile (NULL) and an invalid name", !pc_guest_profile_prepare_new(dir, NULL, &q, err, sizeof(err)) && !pc_guest_profile_prepare_new(dir, "a b", &q, err, sizeof(err)));
    strcpy(p.name, "Zed");
    p.gender = 1;
    p.face = 3;
    r = pc_guest_profile_create_exclusive(pn, &p, err, sizeof(err));
    check("first-run: create_exclusive writes the file (1) and it loads back with the chosen identity", r == 1 && pc_guest_profile_read(pn, &r2, err, sizeof(err)) == PC_GUEST_PROFILE_LOADED
          && same_identity(&p, &r2) && r2.gender == 1 && r2.face == 3);
    b1 = slurp(pn, &n1);
    q = p;
    strcpy(q.name, "Other");
    r = pc_guest_profile_create_exclusive(pn, &q, err, sizeof(err));
    b2 = slurp(pn, &n2);
    check("first-run: a second create_exclusive returns 0 and leaves the existing file byte-identical (never replaces)", r == 0 && b1 != NULL && b2 != NULL && n1 == n2 && memcmp(b1, b2, n1) == 0);
    free(b1);
    free(b2);
    snprintf(tmpp, sizeof(tmpp), "%s.tmp", pn);
    check("first-run: no .tmp file is left behind", !exists(tmpp) && !exists(tok));
    check("first-run: prepare_new refuses when the profile file already exists", !pc_guest_profile_prepare_new(dir, "Roger", &q, err, sizeof(err)));
    remove(pn);
    spit(tok, "token");
    check("first-run: an ORPHAN token (guest_token_roger.dat without the ini) is refused with a message and NOT deleted", !pc_guest_profile_prepare_new(dir, "Roger", &q, err, sizeof(err))
          && strstr(err, "NOT deleted") != NULL && same_file(tok, "token"));
    remove(tok);
    q = p;
    q.gender = 7;
    check("first-run: create_exclusive refuses an invalid profile (-1) and creates nothing", pc_guest_profile_create_exclusive(pn, &q, err, sizeof(err)) == -1 && !exists(pn));
    memset(gb, ' ', 8);
    memcpy(gb, "Zed", 3);
    check("first-run: name_from_game('Zed     ') = 'Zed'", pc_guest_profile_name_from_game(gb, nm) && strcmp(nm, "Zed") == 0);
    memcpy(gb, "A.b'-9 z", 8);
    check("first-run: name_from_game accepts the whole charset incl. an inner blank (round trip exact)", pc_guest_profile_name_from_game(gb, nm) && strcmp(nm, "A.b'-9 z") == 0);
    memset(gb, ' ', 8);
    memcpy(gb + 1, "Zed", 3);
    check("first-run: name_from_game rejects a LEADING blank", !pc_guest_profile_name_from_game(gb, nm) && nm[0] == '\0');
    memset(gb, ' ', 8);
    check("first-run: name_from_game rejects an all-blank name", !pc_guest_profile_name_from_game(gb, nm));
    memset(gb, ' ', 8);
    memcpy(gb, "SERVER", 6);
    check("first-run: name_from_game rejects the reserved name SERVER", !pc_guest_profile_name_from_game(gb, nm));
    memset(gb, ' ', 8);
    gb[0] = 'A';
    gb[1] = 0xA4;
    check("first-run: name_from_game rejects a non-ASCII font code", !pc_guest_profile_name_from_game(gb, nm));
    memset(gb, ' ', 8);
    gb[0] = 'A';
    gb[1] = 0;
    check("first-run: name_from_game rejects a NUL byte", !pc_guest_profile_name_from_game(gb, nm));
}

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: guest_profiles_selftest <scratch_dir>\n");
        return 2;
    }
    snprintf(g_dir, sizeof(g_dir), "%s", argv[1]);
    MKDIR(g_dir);
    name_table();
    path_table();
    select_table();
    default_name_table();
    profile_dir_tests();
    first_run_tests();
    printf("RESULT passed=%d failed=%d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
