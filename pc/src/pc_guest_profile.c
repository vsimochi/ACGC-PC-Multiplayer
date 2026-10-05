/* pc_guest_profile.c - Guests G2.2: the client's guest profile file save/mp/guest.ini. See pc_guest_profile.h for the format and policy.
 * Pure C: libc + (Windows) the file APIs, plus pc_mp_guests.c for the OS CSPRNG and the shared name rules. */
#include "pc_guest_profile.h"
#include "pc_mp_guests.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#ifdef _WIN32
#include <direct.h>
#include <io.h>
#include <windows.h>
#else
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#endif

/* ---------------- helpers ---------------- */

static void set_err(char* err, size_t cap, const char* fmt, const char* a, const char* b) {
    if (err != NULL && cap > 0) {
        snprintf(err, cap, fmt, a != NULL ? a : "", b != NULL ? b : "");
    }
}

static int name_char_ok(unsigned char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == ' ' || c == '.' || c == '\'' || c == '-';
}

/* a profile text value (name / home_town): 1..8 chars of the allowed charset, not blank */
static int text_value_ok(const char* s, const char** why) {
    size_t n = strlen(s), k;
    int content = 0;
    if (n < 1) {
        *why = "is empty (1..8 characters required)";
        return 0;
    }
    if (n > PC_GUEST_PROFILE_NAME_LEN) {
        *why = "is too long (1..8 characters allowed)";
        return 0;
    }
    for (k = 0; k < n; k++) {
        if (!name_char_ok((unsigned char)s[k])) {
            *why = "has a character outside A-Z a-z 0-9 blank . ' - (the game font codes a guest name may safely use)";
            return 0;
        }
        if (s[k] != ' ') {
            content = 1;
        }
    }
    if (!content) {
        *why = "is blank (a name needs at least one non-blank character)";
        return 0;
    }
    if (s[0] == ' ' || s[n - 1] == ' ') {
        *why = "must not start or end with a blank";
        return 0;
    }
    return 1;
}

static void pad8(const char* s, uint8_t out[8]) {
    size_t n = strlen(s), k;
    memset(out, ' ', 8);
    for (k = 0; k < n && k < 8; k++) {
        out[k] = (uint8_t)s[k];
    }
}

void pc_guest_profile_name_bytes(const PCGuestProfile* p, uint8_t out[PC_GUEST_PROFILE_NAME_LEN]) {
    pad8(p->name, out);
}

void pc_guest_profile_home_bytes(const PCGuestProfile* p, uint8_t out[PC_GUEST_PROFILE_NAME_LEN]) {
    pad8(p->home_town, out);
}

/* The game's identity hash (pc_m_card.c pc_guest_identity_hash): FNV-1a32 over the canonical 20-byte PersonalID (name[8], land[8], player_id BE, land_id BE). */
static uint32_t identity_hash(const PCGuestProfile* p) {
    uint8_t b[20];
    uint32_t h = 2166136261u;
    int k;
    pad8(p->name, b);
    pad8(p->home_town, b + 8);
    b[16] = (uint8_t)(p->player_id >> 8);
    b[17] = (uint8_t)(p->player_id & 0xFFu);
    b[18] = (uint8_t)(p->land_id >> 8);
    b[19] = (uint8_t)(p->land_id & 0xFFu);
    for (k = 0; k < 20; k++) {
        h = (h ^ (uint32_t)b[k]) * 16777619u;
    }
    return h;
}

int pc_guest_profile_validate(const PCGuestProfile* p, const char** bad_key, const char** why) {
    const char* w = NULL;
    uint8_t nb[8];
    if (bad_key != NULL) {
        *bad_key = NULL;
    }
#define PROFILE_BAD(key, reason) do { if (bad_key != NULL) *bad_key = (key); if (why != NULL) *why = (reason); return 0; } while (0)
    if (p == NULL) {
        PROFILE_BAD("profile", "is NULL");
    }
    if (!text_value_ok(p->name, &w)) {
        PROFILE_BAD("name", w);
    }
    pad8(p->name, nb);
    if (!pc_mp_guests_name_valid(nb)) {
        PROFILE_BAD("name", "is not a valid game player name");
    }
    if (pc_mp_guests_name_reserved(nb)) {
        PROFILE_BAD("name", "'SERVER' is reserved for the server observer (a guest cannot take it)");
    }
    if (p->gender != 0 && p->gender != 1) {
        PROFILE_BAD("gender", "must be 0 (male) or 1 (female)");
    }
    if (p->face < 0 || p->face > 7) {
        PROFILE_BAD("face", "must be 0..7");
    }
    if (!text_value_ok(p->home_town, &w)) {
        PROFILE_BAD("home_town", w);
    }
    if (p->player_id == 0u || p->player_id == 0xFFFFu) {
        PROFILE_BAD("player_id", "must be in 1..0xFFFE");
    }
    if (p->land_id == 0u || p->land_id == 0xFFFFu) {
        PROFILE_BAD("land_id", "must be in 1..0xFFFE");
    }
#undef PROFILE_BAD
    return 1;
}

/* ---------------- parse ---------------- */

static char* trim(char* s) {
    char* e;
    while (*s == ' ' || *s == '\t') {
        s++;
    }
    e = s + strlen(s);
    while (e > s && (e[-1] == ' ' || e[-1] == '\t' || e[-1] == '\r')) {
        e--;
    }
    *e = '\0';
    return s;
}

/* decimal digits only, or 0x / 0X hex digits only (no sign, no octal); 1 = ok */
static int parse_uint(const char* s, unsigned long* out) {
    unsigned long v = 0;
    int base = 10;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        base = 16;
        s += 2;
    }
    if (*s == '\0') {
        return 0;
    }
    for (; *s != '\0'; s++) {
        int d;
        if (*s >= '0' && *s <= '9') {
            d = *s - '0';
        } else if (base == 16 && *s >= 'a' && *s <= 'f') {
            d = *s - 'a' + 10;
        } else if (base == 16 && *s >= 'A' && *s <= 'F') {
            d = *s - 'A' + 10;
        } else {
            return 0;
        }
        v = v * (unsigned long)base + (unsigned long)d;
        if (v > 0xFFFFFFul) {
            return 0;
        }
    }
    *out = v;
    return 1;
}

enum { K_NAME, K_GENDER, K_FACE, K_HOME, K_PID, K_LID, K_COUNT };
static const char* const k_keys[K_COUNT] = { "name", "gender", "face", "home_town", "player_id", "land_id" };

int pc_guest_profile_parse(const char* text, size_t len, PCGuestProfile* out, char* err, size_t errcap) {
    char buf[PC_GUEST_PROFILE_MAX_FILE + 1];
    unsigned seen = 0;
    char* line;
    char* nextp;
    int lineno = 0;
    PCGuestProfile p;
    const char* bad_key = NULL;
    const char* why = NULL;
    size_t i;
    memset(&p, 0, sizeof(p));
    if (text == NULL || len > PC_GUEST_PROFILE_MAX_FILE) {
        set_err(err, errcap, "%s", "the file is too large (more than 4096 bytes)", NULL);
        return 0;
    }
    memcpy(buf, text, len);
    buf[len] = '\0';
    if (strlen(buf) != len) {
        set_err(err, errcap, "%s", "the file contains a NUL byte (not a text file)", NULL);
        return 0;
    }
    line = buf;
    if ((unsigned char)line[0] == 0xEF && (unsigned char)line[1] == 0xBB && (unsigned char)line[2] == 0xBF) {
        line += 3; /* UTF-8 BOM */
    }
    for (; line != NULL && *line != '\0'; line = nextp) {
        char* eq;
        char* key;
        char* val;
        int ki;
        unsigned long u;
        nextp = strchr(line, '\n');
        if (nextp != NULL) {
            *nextp++ = '\0';
        }
        lineno++;
        key = trim(line);
        if (*key == '\0' || *key == '#' || *key == ';' || *key == '[') {
            continue;
        }
        eq = strchr(key, '=');
        if (eq == NULL) {
            char msg[64];
            snprintf(msg, sizeof(msg), "line %d", lineno);
            set_err(err, errcap, "%s: expected 'key = value'%s", msg, "");
            return 0;
        }
        *eq = '\0';
        val = trim(eq + 1);
        key = trim(key);
        for (ki = 0; ki < K_COUNT; ki++) {
            if (strcmp(key, k_keys[ki]) == 0) {
                break;
            }
        }
        if (ki == K_COUNT) {
            set_err(err, errcap, "%s: unknown key (valid keys: name, gender, face, home_town, player_id, land_id)%s", key, "");
            return 0;
        }
        if (seen & (1u << ki)) {
            set_err(err, errcap, "%s: the key appears more than once%s", key, "");
            return 0;
        }
        seen |= 1u << ki;
        switch (ki) {
            case K_NAME:
            case K_HOME: {
                char* dst = ki == K_NAME ? p.name : p.home_town;
                if (!text_value_ok(val, &why)) {
                    set_err(err, errcap, "%s: %s", key, why);
                    return 0;
                }
                memcpy(dst, val, strlen(val) + 1);
                break;
            }
            case K_GENDER:
                if (!parse_uint(val, &u) || u > 1ul) {
                    set_err(err, errcap, "%s: must be 0 (male) or 1 (female)%s", key, "");
                    return 0;
                }
                p.gender = (int)u;
                break;
            case K_FACE:
                if (!parse_uint(val, &u) || u > 7ul) {
                    set_err(err, errcap, "%s: must be 0..7%s", key, "");
                    return 0;
                }
                p.face = (int)u;
                break;
            case K_PID:
            case K_LID:
                if (!parse_uint(val, &u) || u == 0ul || u >= 0xFFFFul) {
                    set_err(err, errcap, "%s: must be a decimal or 0x-hex number in 1..0xFFFE%s", key, "");
                    return 0;
                }
                if (ki == K_PID) {
                    p.player_id = (uint16_t)u;
                } else {
                    p.land_id = (uint16_t)u;
                }
                break;
            default:
                break;
        }
    }
    for (i = 0; i < K_COUNT; i++) {
        if (!(seen & (1u << i))) {
            set_err(err, errcap, "%s: missing (every key is required: name, gender, face, home_town, player_id, land_id)%s", k_keys[i], "");
            return 0;
        }
    }
    if (!pc_guest_profile_validate(&p, &bad_key, &why)) {
        set_err(err, errcap, "%s: %s", bad_key, why);
        return 0;
    }
    *out = p;
    return 1;
}

/* ---------------- format / default ---------------- */

size_t pc_guest_profile_format(const PCGuestProfile* p, char* out, size_t cap) {
    int n = snprintf(out, cap,
                     "# Animal Crossing PC - multiplayer GUEST profile (used by --guest).\n"
                     "# Edit name / gender / face / home_town BEFORE your first join of a host.\n"
                     "# player_id and land_id were drawn once at random and are PERMANENT: together with name and home_town\n"
                     "# they are your guest identity. Changing any of those four after joining makes a NEW guest (the old\n"
                     "# character stays on the host). gender / face only matter for a brand-new guest.\n"
                     "# name / home_town: 1..8 characters of A-Z a-z 0-9 blank . ' -   (name must not be SERVER)\n"
                     "name = %s\n"
                     "gender = %d\n"
                     "face = %d\n"
                     "home_town = %s\n"
                     "player_id = %u\n"
                     "land_id = %u\n",
                     p->name, p->gender, p->face, p->home_town, (unsigned)p->player_id, (unsigned)p->land_id);
    return (n > 0 && (size_t)n < cap) ? (size_t)n : 0;
}

static PCGuestProfileIdSource s_id_source = NULL; /* TEST SEAM (pc_guest_profile_test_set_id_source); NULL = the OS CSPRNG, the only production value */

static int random_id(uint16_t* out) {
    uint8_t b[2];
    if (s_id_source != NULL) {
        return s_id_source(out);
    }
    if (!pc_mp_guests_random_bytes(b, sizeof(b))) {
        return 0;
    }
    *out = (uint16_t)(1u + (((unsigned)b[0] << 8 | b[1]) % 0xFFFEu)); /* 1..0xFFFE */
    return 1;
}

int pc_guest_profile_make_default(PCGuestProfile* out) {
    PCGuestProfile p;
    uint32_t h;
    memset(&p, 0, sizeof(p));
    memcpy(p.name, PC_GUEST_PROFILE_DEFAULT_NAME, sizeof(PC_GUEST_PROFILE_DEFAULT_NAME));
    memcpy(p.home_town, PC_GUEST_PROFILE_DEFAULT_HOME, sizeof(PC_GUEST_PROFILE_DEFAULT_HOME));
    if (!random_id(&p.player_id) || !random_id(&p.land_id)) {
        return 0;
    }
    h = identity_hash(&p);
    p.gender = (int)((h >> 31) & 1u);
    p.face = (int)((h >> 16) & 7u);
    *out = p;
    return 1;
}

/* ---------------- files ---------------- */

static int rename_over(const char* src, const char* dst) {
#ifdef _WIN32
    return MoveFileExA(src, dst, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) ? 0 : -1;
#else
    return rename(src, dst);
#endif
}

/* create-only move: fails (-1) when `dst` exists, never replaces it */
static int rename_exclusive(const char* src, const char* dst) {
#ifdef _WIN32
    return MoveFileExA(src, dst, MOVEFILE_WRITE_THROUGH) ? 0 : -1; /* no MOVEFILE_REPLACE_EXISTING */
#else
    if (link(src, dst) != 0) {
        return -1;
    }
    remove(src);
    return 0;
#endif
}

static void make_dir(const char* p) {
#ifdef _WIN32
    _mkdir(p);
#else
    mkdir(p, 0755);
#endif
}

static void ensure_parent_dirs(const char* path) {
    char buf[512];
    size_t i, n = strlen(path);
    if (n >= sizeof(buf)) {
        return;
    }
    memcpy(buf, path, n + 1);
    for (i = 1; i < n; i++) {
        if (buf[i] == '/' || buf[i] == '\\') {
            char c = buf[i];
            buf[i] = 0;
            make_dir(buf);
            buf[i] = c;
        }
    }
}

/* atomic create: tmp -> flush + commit -> replace (exclusive: create-only, fails when `path` exists); returns 1 on success */
static int write_atomic_ex(const char* path, const char* text, size_t n, int exclusive) {
    char tmp[600];
    FILE* fp;
    int ok;
    if (strlen(path) + 5 >= sizeof(tmp)) {
        return 0;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    ensure_parent_dirs(path);
    fp = fopen(tmp, "wb");
    if (fp == NULL) {
        return 0;
    }
    ok = fwrite(text, 1, n, fp) == n && fflush(fp) == 0;
#ifdef _WIN32
    ok = ok && _commit(_fileno(fp)) == 0;
#else
    ok = ok && fsync(fileno(fp)) == 0;
#endif
    ok = (fclose(fp) == 0) && ok;
    if (!ok || (exclusive ? rename_exclusive(tmp, path) : rename_over(tmp, path)) != 0) {
        remove(tmp);
        return 0;
    }
    return 1;
}

static int write_atomic(const char* path, const char* text, size_t n) {
    return write_atomic_ex(path, text, n, 0);
}

int pc_guest_profile_load_or_create(const char* path, PCGuestProfile* out, char* err, size_t errcap) {
    FILE* fp = fopen(path, "rb");
    char text[PC_GUEST_PROFILE_MAX_FILE + 2];
    char perr[256];
    char fmt[PC_GUEST_PROFILE_MAX_FILE];
    PCGuestProfile p;
    size_t n;
    if (fp == NULL) {
        if (errno != ENOENT) {
            set_err(err, errcap, "%s: cannot open the profile file (%s); it was not modified", path, strerror(errno));
            return PC_GUEST_PROFILE_ERR;
        }
        if (!pc_guest_profile_make_default(&p)) {
            set_err(err, errcap, "%s: the OS random source failed; no guest identity can be created%s", path, "");
            return PC_GUEST_PROFILE_ERR;
        }
        n = pc_guest_profile_format(&p, fmt, sizeof(fmt));
        if (n == 0 || !write_atomic(path, fmt, n)) {
            set_err(err, errcap, "%s: cannot create the default profile file (is the directory writable?)%s", path, "");
            return PC_GUEST_PROFILE_ERR;
        }
        *out = p;
        return PC_GUEST_PROFILE_CREATED;
    }
    n = fread(text, 1, sizeof(text) - 1, fp);
    if (ferror(fp)) {
        fclose(fp);
        set_err(err, errcap, "%s: read error; the file was not modified%s", path, "");
        return PC_GUEST_PROFILE_ERR;
    }
    fclose(fp);
    if (n > PC_GUEST_PROFILE_MAX_FILE) {
        set_err(err, errcap, "%s: the file is too large (more than 4096 bytes); it was not modified%s", path, "");
        return PC_GUEST_PROFILE_ERR;
    }
    if (!pc_guest_profile_parse(text, n, &p, perr, sizeof(perr))) {
        if (err != NULL && errcap > 0) {
            snprintf(err, errcap, "%s: %s (the file was preserved unchanged; fix it, or delete it to get a NEW guest identity)", path, perr);
        }
        return PC_GUEST_PROFILE_ERR;
    }
    *out = p;
    return PC_GUEST_PROFILE_LOADED;
}

int pc_guest_profile_spec(const PCGuestProfile* p, char* out, size_t cap) {
    int n = snprintf(out, cap, "%s,%s,%u,%u,%d,%d", p->name, p->home_town, (unsigned)p->player_id, (unsigned)p->land_id, p->gender, p->face);
    return n > 0 && (size_t)n < cap;
}

/* ================= named guest profiles (--guest-profile NAME) ================= */

#define PROFILE_MAX_SIBLINGS 64
#define PROFILE_ID_DRAWS 32

void pc_guest_profile_test_set_id_source(PCGuestProfileIdSource fn) {
    s_id_source = fn;
}

static char lower_c(char c) {
    return (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
}

void pc_guest_profile_fold(const char* name, char* out) {
    size_t k = 0;
    if (name != NULL) {
        for (; name[k] != '\0' && k < PC_GUEST_PROFILE_ARG_MAX; k++) {
            out[k] = lower_c(name[k]);
        }
    }
    out[k] = '\0';
}

static int is_device_name(const char* low) {
    static const char* const k_dev[] = { "con", "prn", "aux", "nul" };
    size_t k;
    for (k = 0; k < sizeof(k_dev) / sizeof(k_dev[0]); k++) {
        if (strcmp(low, k_dev[k]) == 0) {
            return 1;
        }
    }
    return strlen(low) == 4 && (strncmp(low, "com", 3) == 0 || strncmp(low, "lpt", 3) == 0) && low[3] >= '0' && low[3] <= '9';
}

int pc_guest_profile_name_check(const char* name, char* err, size_t errcap) {
    size_t n, k;
    char low[PC_GUEST_PROFILE_ARG_MAX + 1];
    if (name == NULL || name[0] == '\0') {
        set_err(err, errcap, "%s", "profile name is empty (rule: 1..16 characters of A-Z a-z 0-9 -)", NULL);
        return 0;
    }
    n = strlen(name);
    if (n > PC_GUEST_PROFILE_ARG_MAX) {
        set_err(err, errcap, "%s", "profile name is too long (rule: 1..16 characters of A-Z a-z 0-9 -)", NULL);
        return 0;
    }
    for (k = 0; k < n; k++) {
        const unsigned char c = (unsigned char)name[k];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) {
            set_err(err, errcap, "%s", "profile name has a character outside A-Z a-z 0-9 - (rule: no path separators, dots, spaces or other characters)", NULL);
            return 0;
        }
    }
    if (name[0] == '-') {
        set_err(err, errcap, "%s", "profile name must not start with '-' (rule: it would be confused with an option)", NULL);
        return 0;
    }
    pc_guest_profile_fold(name, low);
    if (is_device_name(low)) {
        set_err(err, errcap, "%s", "profile name is a reserved Windows device name (rule: not CON PRN AUX NUL COM0..COM9 LPT0..LPT9)", NULL);
        return 0;
    }
    return 1;
}

int pc_guest_profile_file_path(const char* dir, const char* profile, int token_file, char* out, size_t cap) {
    char low[PC_GUEST_PROFILE_ARG_MAX + 1];
    int n;
    if (dir == NULL) {
        dir = PC_GUEST_PROFILE_DIR;
    }
    if (profile == NULL || profile[0] == '\0') {
        n = snprintf(out, cap, token_file ? "%s/guest_token.dat" : "%s/guest.ini", dir);
    } else {
        if (!pc_guest_profile_name_check(profile, NULL, 0)) {
            return 0;
        }
        pc_guest_profile_fold(profile, low);
        n = snprintf(out, cap, token_file ? "%s/guest_token_%s.dat" : "%s/guest_%s.ini", dir, low);
    }
    return n > 0 && (size_t)n < cap;
}

static char s_selected[PC_GUEST_PROFILE_ARG_MAX + 1];

int pc_guest_profile_select(const char* name) {
    if (name == NULL || name[0] == '\0') {
        s_selected[0] = '\0';
        return 1;
    }
    if (!pc_guest_profile_name_check(name, NULL, 0)) {
        return 0;
    }
    memcpy(s_selected, name, strlen(name) + 1);
    return 1;
}

const char* pc_guest_profile_selected(void) {
    return s_selected[0] != '\0' ? s_selected : NULL;
}

const char* pc_guest_profile_selected_path(void) {
    static char buf[96];
    if (!pc_guest_profile_file_path(NULL, pc_guest_profile_selected(), 0, buf, sizeof(buf))) {
        snprintf(buf, sizeof(buf), "%s", PC_GUEST_PROFILE_PATH);
    }
    return buf;
}

static char s_token_override[300]; /* M2: a STORE character's per-town token file (pc_session.c); empty = the legacy file below, unchanged */

void pc_guest_token_path_set_override(const char* path) {
    snprintf(s_token_override, sizeof(s_token_override), "%s", path != NULL ? path : "");
}

const char* pc_guest_token_path(void) {
    static char buf[96];
    if (s_token_override[0] != '\0') {
        return s_token_override;
    }
    if (!pc_guest_profile_file_path(NULL, pc_guest_profile_selected(), 1, buf, sizeof(buf))) {
        snprintf(buf, sizeof(buf), "%s", PC_GUEST_TOKEN_DEFAULT_PATH);
    }
    return buf;
}

int pc_guest_profile_read(const char* path, PCGuestProfile* out, char* err, size_t errcap) {
    FILE* fp = fopen(path, "rb");
    char text[PC_GUEST_PROFILE_MAX_FILE + 2];
    char perr[256];
    PCGuestProfile p;
    size_t n;
    if (fp == NULL) {
        if (errno == ENOENT) {
            return PC_GUEST_PROFILE_ABSENT;
        }
        set_err(err, errcap, "%s: cannot open the profile file (%s); it was not modified", path, strerror(errno));
        return PC_GUEST_PROFILE_ERR;
    }
    n = fread(text, 1, sizeof(text) - 1, fp);
    if (ferror(fp)) {
        fclose(fp);
        set_err(err, errcap, "%s: read error; the file was not modified%s", path, "");
        return PC_GUEST_PROFILE_ERR;
    }
    fclose(fp);
    if (n > PC_GUEST_PROFILE_MAX_FILE) {
        set_err(err, errcap, "%s: the file is too large (more than 4096 bytes); it was not modified%s", path, "");
        return PC_GUEST_PROFILE_ERR;
    }
    if (!pc_guest_profile_parse(text, n, &p, perr, sizeof(perr))) {
        if (err != NULL && errcap > 0) {
            snprintf(err, errcap, "%s: %s (the file was preserved unchanged; fix it, or delete it to get a NEW guest identity)", path, perr);
        }
        return PC_GUEST_PROFILE_ERR;
    }
    *out = p;
    return PC_GUEST_PROFILE_LOADED;
}

static int streq_ci(const char* a, const char* b) {
    for (; *a != '\0' && *b != '\0'; a++, b++) {
        if (lower_c(*a) != lower_c(*b)) {
            return 0;
        }
    }
    return *a == *b;
}

void pc_guest_profile_default_name(const char* profile, uint16_t id, char* out) {
    char base[PC_GUEST_PROFILE_NAME_LEN + 1];
    PCGuestProfile t;
    size_t n = profile != NULL ? strlen(profile) : 0;
    if (n > PC_GUEST_PROFILE_NAME_LEN) {
        n = PC_GUEST_PROFILE_NAME_LEN;
    }
    memcpy(base, profile != NULL ? profile : "", n);
    base[n] = '\0';
    memset(&t, 0, sizeof(t));
    memcpy(t.name, base, n + 1);
    memcpy(t.home_town, PC_GUEST_PROFILE_DEFAULT_HOME, sizeof(PC_GUEST_PROFILE_DEFAULT_HOME));
    t.player_id = 1;
    t.land_id = 1;
    /* the profile name cut to 8 characters, unless it is not a valid guest name, is SERVER (any case: the reserved name is an exact match, a look-alike is just
     * confusing) or is the default profile's own name "Guest" (two guests of a town may not share a name): then "Guest" + 2 hex digits of the id */
    if (n == 0 || !pc_guest_profile_validate(&t, NULL, NULL) || streq_ci(base, "SERVER") || streq_ci(base, PC_GUEST_PROFILE_DEFAULT_NAME)) {
        snprintf(out, PC_GUEST_PROFILE_NAME_LEN + 1, "Guest%02X", (unsigned)(id & 0xFFu));
        return;
    }
    memcpy(out, base, n + 1);
}

static int sibling_conflict(const PCGuestProfile* c, const PCGuestProfile* sib, int nsib) {
    int k;
    for (k = 0; k < nsib; k++) {
        if (streq_ci(c->name, sib[k].name)) {
            return 1; /* the host refuses a NEW guest whose name equals another guest's in the town */
        }
        if (strcmp(c->home_town, sib[k].home_town) == 0 && c->player_id == sib[k].player_id && c->land_id == sib[k].land_id && strcmp(c->name, sib[k].name) == 0) {
            return 1; /* the full identity (name, home town, ids) is the host's guest key */
        }
    }
    return 0;
}

int pc_guest_profile_make_unique(const char* profile, const PCGuestProfile* sib, int nsib, PCGuestProfile* out) {
    int attempt;
    for (attempt = 0; attempt < PROFILE_ID_DRAWS; attempt++) {
        PCGuestProfile p;
        memset(&p, 0, sizeof(p));
        memcpy(p.home_town, PC_GUEST_PROFILE_DEFAULT_HOME, sizeof(PC_GUEST_PROFILE_DEFAULT_HOME));
        if (!random_id(&p.player_id) || !random_id(&p.land_id)) {
            return 0;
        }
        pc_guest_profile_default_name(profile, p.player_id, p.name);
        if (sibling_conflict(&p, sib, nsib)) {
            /* the profile-name based name collides with a sibling: use the id based fallback name for this draw, else draw again */
            snprintf(p.name, sizeof(p.name), "Guest%02X", (unsigned)(p.player_id & 0xFFu));
            if (sibling_conflict(&p, sib, nsib)) {
                continue;
            }
        }
        {
            const uint32_t h = identity_hash(&p);
            p.gender = (int)((h >> 31) & 1u);
            p.face = (int)((h >> 16) & 7u);
        }
        if (!pc_guest_profile_validate(&p, NULL, NULL)) {
            continue;
        }
        *out = p;
        return 1;
    }
    return 0;
}

/* the file names that count as profiles of a directory: guest.ini and guest_<x>.ini */
static int sibling_file_name(const char* nm) {
    const size_t len = strlen(nm);
    return streq_ci(nm, "guest.ini") || (len > 10 && strncmp(nm, "guest_", 6) == 0 && streq_ci(nm + len - 4, ".ini"));
}

static int sibling_take(const char* dir, const char* nm, PCGuestProfile* sib, int n) {
    char full[560], err[400];
    PCGuestProfile p;
    snprintf(full, sizeof(full), "%s/%s", dir, nm);
    if (pc_guest_profile_read(full, &p, err, sizeof(err)) == PC_GUEST_PROFILE_LOADED) {
        sib[n] = p;
        return 1;
    }
    fprintf(stderr, "[PC] guest profile: sibling profile skipped (not readable / not a valid profile): %s\n", err);
    return 0;
}

/* the sibling profiles of `dir` except the file `self_file`; read-only, an unreadable / invalid one is skipped with a warning */
static int scan_siblings(const char* dir, const char* self_file, PCGuestProfile* sib, int cap) {
    int n = 0;
#ifdef _WIN32
    char pat[512];
    WIN32_FIND_DATAA fd;
    HANDLE h;
    snprintf(pat, sizeof(pat), "%s/guest*.ini", dir);
    h = FindFirstFileA(pat, &fd);
    if (h == INVALID_HANDLE_VALUE) {
        return 0;
    }
    do {
        if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) == 0 && n < cap && !streq_ci(fd.cFileName, self_file) && sibling_file_name(fd.cFileName)) {
            n += sibling_take(dir, fd.cFileName, sib, n);
        }
    } while (FindNextFileA(h, &fd));
    FindClose(h);
#else
    DIR* d = opendir(dir);
    struct dirent* de;
    if (d == NULL) {
        return 0;
    }
    while ((de = readdir(d)) != NULL && n < cap) {
        if (!streq_ci(de->d_name, self_file) && sibling_file_name(de->d_name)) {
            n += sibling_take(dir, de->d_name, sib, n);
        }
    }
    closedir(d);
#endif
    return n;
}

int pc_guest_profile_load_or_create_in(const char* dir, const char* profile, PCGuestProfile* out, char* err, size_t errcap) {
    char path[512], self[64], fmt[PC_GUEST_PROFILE_MAX_FILE];
    PCGuestProfile sib[PROFILE_MAX_SIBLINGS];
    PCGuestProfile p;
    int r, nsib;
    size_t n;
    if (dir == NULL) {
        dir = PC_GUEST_PROFILE_DIR;
    }
    if (profile == NULL || profile[0] == '\0') {
        if (!pc_guest_profile_file_path(dir, NULL, 0, path, sizeof(path))) {
            set_err(err, errcap, "the profile directory path is too long%s", "", NULL);
            return PC_GUEST_PROFILE_ERR;
        }
        return pc_guest_profile_load_or_create(path, out, err, errcap);
    }
    if (err != NULL && errcap > 0) {
        err[0] = '\0';
    }
    if (!pc_guest_profile_name_check(profile, err, errcap)) {
        return PC_GUEST_PROFILE_ERR;
    }
    if (!pc_guest_profile_file_path(dir, profile, 0, path, sizeof(path))) {
        set_err(err, errcap, "the profile path is too long%s", "", NULL);
        return PC_GUEST_PROFILE_ERR;
    }
    r = pc_guest_profile_read(path, &p, err, errcap);
    if (r != PC_GUEST_PROFILE_ABSENT) {
        if (r == PC_GUEST_PROFILE_LOADED) {
            *out = p;
        }
        return r;
    }
    /* a NEW profile: unique among its siblings, then the atomic create */
    pc_guest_profile_file_path(".", profile, 0, self, sizeof(self)); /* "./guest_<x>.ini": only the file name part is used */
    nsib = scan_siblings(dir, self + 2, sib, PROFILE_MAX_SIBLINGS);
    if (!pc_guest_profile_make_unique(profile, sib, nsib, &p)) {
        set_err(err, errcap, "%s: no unique guest identity could be drawn (random source failed or too many collisions)%s", path, "");
        return PC_GUEST_PROFILE_ERR;
    }
    n = pc_guest_profile_format(&p, fmt, sizeof(fmt));
    if (n == 0 || !write_atomic(path, fmt, n)) {
        set_err(err, errcap, "%s: cannot create the profile file (is the directory writable?)%s", path, "");
        return PC_GUEST_PROFILE_ERR;
    }
    *out = p;
    return PC_GUEST_PROFILE_CREATED;
}

int pc_guest_profile_load_or_create_selected(PCGuestProfile* out, char* err, size_t errcap) {
    return pc_guest_profile_load_or_create_in(NULL, pc_guest_profile_selected(), out, err, errcap);
}

int pc_guest_profile_read_selected(PCGuestProfile* out, char* err, size_t errcap) {
    return pc_guest_profile_read(pc_guest_profile_selected_path(), out, err, errcap);
}

/* ================= first-run guest creation (see pc_guest_profile.h) ================= */

int pc_guest_profile_prepare_new(const char* dir, const char* profile, PCGuestProfile* out, char* err, size_t errcap) {
    char path[512], tokpath[512], self[64];
    PCGuestProfile sib[PROFILE_MAX_SIBLINGS];
    PCGuestProfile p;
    FILE* fp;
    int nsib;
    if (dir == NULL) {
        dir = PC_GUEST_PROFILE_DIR;
    }
    if (profile == NULL || profile[0] == '\0') {
        set_err(err, errcap, "%s", "first-run creation needs a named profile (--guest-profile NAME)", NULL);
        return 0;
    }
    if (!pc_guest_profile_name_check(profile, err, errcap)) {
        return 0;
    }
    if (!pc_guest_profile_file_path(dir, profile, 0, path, sizeof(path)) || !pc_guest_profile_file_path(dir, profile, 1, tokpath, sizeof(tokpath))) {
        set_err(err, errcap, "the profile path is too long%s", "", NULL);
        return 0;
    }
    fp = fopen(path, "rb");
    if (fp != NULL) {
        fclose(fp);
        set_err(err, errcap, "%s: the profile file already exists (nothing to create)%s", path, "");
        return 0;
    }
    fp = fopen(tokpath, "rb");
    if (fp != NULL) {
        fclose(fp);
        set_err(err, errcap, "%s: this guest token file exists but its profile file %s is missing; refusing to create a NEW guest over an old token (restore the "
                             "profile file, or move the token file away yourself; it was NOT deleted)", tokpath, path);
        return 0;
    }
    pc_guest_profile_file_path(".", profile, 0, self, sizeof(self)); /* "./guest_<x>.ini": only the file name part is used */
    nsib = scan_siblings(dir, self + 2, sib, PROFILE_MAX_SIBLINGS);
    if (!pc_guest_profile_make_unique(profile, sib, nsib, &p)) {
        set_err(err, errcap, "%s: no unique guest identity could be drawn (random source failed or too many collisions)%s", path, "");
        return 0;
    }
    *out = p;
    return 1;
}

int pc_guest_profile_prepare_new_selected(PCGuestProfile* out, char* err, size_t errcap) {
    return pc_guest_profile_prepare_new(NULL, pc_guest_profile_selected(), out, err, errcap);
}

int pc_guest_profile_create_exclusive(const char* path, const PCGuestProfile* p, char* err, size_t errcap) {
    char fmt[PC_GUEST_PROFILE_MAX_FILE];
    const char* bad_key = NULL;
    const char* why = NULL;
    FILE* fp;
    size_t n;
    if (!pc_guest_profile_validate(p, &bad_key, &why)) {
        set_err(err, errcap, "the new profile is invalid: %s %s", bad_key, why);
        return -1;
    }
    fp = fopen(path, "rb");
    if (fp != NULL) {
        fclose(fp);
        return 0;
    }
    n = pc_guest_profile_format(p, fmt, sizeof(fmt));
    if (n == 0 || !write_atomic_ex(path, fmt, n, 1)) {
        fp = fopen(path, "rb");
        if (fp != NULL) {
            fclose(fp);
            return 0; /* lost a race with another creator: the existing file wins */
        }
        set_err(err, errcap, "%s: cannot create the profile file (is the directory writable?)%s", path, "");
        return -1;
    }
    return 1;
}

int pc_guest_profile_name_from_game(const uint8_t name[PC_GUEST_PROFILE_NAME_LEN], char* out) {
    size_t len = PC_GUEST_PROFILE_NAME_LEN, k;
    const char* why = NULL;
    out[0] = '\0';
    while (len > 0 && name[len - 1] == ' ') {
        len--;
    }
    if (len == 0) {
        return 0;
    }
    for (k = 0; k < len; k++) {
        if (!name_char_ok(name[k])) {
            return 0;
        }
        out[k] = (char)name[k];
    }
    out[len] = '\0';
    if (!text_value_ok(out, &why) || !pc_mp_guests_name_valid(name) || pc_mp_guests_name_reserved(name)) {
        out[0] = '\0';
        return 0;
    }
    return 1;
}
