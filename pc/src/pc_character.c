/* pc_character.c - the local character store (see pc_character.h). Pure C: libc + OS file APIs + pc_guest_profile.c / pc_mp_guests.c helpers. */
#include "pc_character.h"
#include "pc_guest_profile.h"
#include "pc_mp_guests.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#ifdef _WIN32
#include <direct.h>
#include <io.h>
#include <windows.h>
#else
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#endif

/* ---------------- small helpers ---------------- */

static void seterr(char* err, size_t cap, const char* fmt, const char* a, const char* b) {
    if (err != NULL && cap > 0) {
        snprintf(err, cap, fmt, a != NULL ? a : "", b != NULL ? b : "");
    }
}

static const char* dir_or_default(const char* dir) {
    return dir != NULL ? dir : PC_GUEST_PROFILE_DIR;
}

static int file_exists(const char* p) {
    struct stat st;
    return stat(p, &st) == 0;
}

static void hex_encode(const uint8_t* in, size_t n, char* out) {
    static const char hx[] = "0123456789abcdef";
    size_t i;
    for (i = 0; i < n; i++) {
        out[2 * i] = hx[in[i] >> 4];
        out[2 * i + 1] = hx[in[i] & 15];
    }
    out[2 * n] = '\0';
}

static int hex_val(char c) {
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

static int hex_decode(const char* s, uint8_t* out, size_t n) {
    size_t i;
    if (strlen(s) != 2 * n) {
        return 0;
    }
    for (i = 0; i < n; i++) {
        const int a = hex_val(s[2 * i]), b = hex_val(s[2 * i + 1]);
        if (a < 0 || b < 0) {
            return 0;
        }
        out[i] = (uint8_t)(a * 16 + b);
    }
    return 1;
}

static int is_uuid(const char* s) {
    size_t i;
    if (strlen(s) != PC_CHARACTER_UUID_LEN) {
        return 0;
    }
    for (i = 0; i < PC_CHARACTER_UUID_LEN; i++) {
        if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) {
            return 0;
        }
    }
    return 1;
}

static int ieq(const char* a, const char* b) {
    for (; *a != '\0' && *b != '\0'; a++, b++) {
        if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) {
            return 0;
        }
    }
    return *a == *b;
}

static void pad8(const char* s, uint8_t out[8]) {
    size_t i, n = strlen(s);
    for (i = 0; i < 8; i++) {
        out[i] = i < n ? (uint8_t)s[i] : (uint8_t)' ';
    }
}

/* ---------------- files ---------------- */

static void make_dir(const char* p) {
#ifdef _WIN32
    _mkdir(p);
#else
    mkdir(p, 0755);
#endif
}

static void ensure_parent_dirs(const char* path) {
    char buf[600];
    size_t i, n = strlen(path);
    if (n >= sizeof(buf)) {
        return;
    }
    memcpy(buf, path, n + 1);
    for (i = 1; i < n; i++) {
        if (buf[i] == '/' || buf[i] == '\\') {
            const char c = buf[i];
            buf[i] = 0;
            make_dir(buf);
            buf[i] = c;
        }
    }
}

/* tmp -> flush + commit -> move (exclusive: create-only, fails when path exists). 1 = ok */
static int write_atomic(const char* path, const char* text, size_t n, int exclusive) {
    char tmp[620];
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
    if (ok) {
#ifdef _WIN32
        ok = MoveFileExA(tmp, path, (exclusive ? 0 : MOVEFILE_REPLACE_EXISTING) | MOVEFILE_WRITE_THROUGH) != 0;
#else
        if (exclusive) {
            ok = link(tmp, path) == 0;
            if (ok) {
                remove(tmp);
            }
        } else {
            ok = rename(tmp, path) == 0;
        }
#endif
    }
    if (!ok) {
        remove(tmp);
    }
    return ok;
}

static int read_all(const char* path, char* buf, size_t cap, size_t* n) {
    FILE* fp = fopen(path, "rb");
    if (fp == NULL) {
        return 0;
    }
    *n = fread(buf, 1, cap, fp);
    fclose(fp);
    return 1;
}

/* directory iteration: cb(name, is_dir, ud) */
typedef void (*DirCb)(const char* name, int is_dir, void* ud);
static void list_dir(const char* dir, DirCb cb, void* ud) {
#ifdef _WIN32
    char pat[620];
    WIN32_FIND_DATAA fd;
    HANDLE h;
    snprintf(pat, sizeof(pat), "%s/*", dir);
    h = FindFirstFileA(pat, &fd);
    if (h == INVALID_HANDLE_VALUE) {
        return;
    }
    do {
        if (strcmp(fd.cFileName, ".") != 0 && strcmp(fd.cFileName, "..") != 0) {
            cb(fd.cFileName, (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0, ud);
        }
    } while (FindNextFileA(h, &fd));
    FindClose(h);
#else
    DIR* d = opendir(dir);
    struct dirent* de;
    char full[620];
    struct stat st;
    if (d == NULL) {
        return;
    }
    while ((de = readdir(d)) != NULL) {
        if (strcmp(de->d_name, ".") == 0 || strcmp(de->d_name, "..") == 0) {
            continue;
        }
        snprintf(full, sizeof(full), "%s/%s", dir, de->d_name);
        cb(de->d_name, stat(full, &st) == 0 && S_ISDIR(st.st_mode), ud);
    }
    closedir(d);
#endif
}

/* ---------------- uuid ---------------- */

static PCCharacterUuidSource s_uuid_source = NULL;

void pc_character_test_set_uuid_source(PCCharacterUuidSource fn) {
    s_uuid_source = fn;
}

static int uuid_new(char out[PC_CHARACTER_UUID_LEN + 1]) {
    uint8_t b[16];
    if (s_uuid_source != NULL) {
        if (!s_uuid_source(b)) {
            return 0;
        }
    } else if (!pc_mp_guests_random_bytes(b, sizeof(b))) {
        return 0;
    }
    hex_encode(b, sizeof(b), out);
    return 1;
}

/* ---------------- text form ---------------- */

static void to_profile(const PCCharacter* c, PCGuestProfile* p) {
    memset(p, 0, sizeof(*p));
    memcpy(p->name, c->name, sizeof(p->name));
    memcpy(p->home_town, c->home_town, sizeof(p->home_town));
    p->gender = c->gender;
    p->face = c->face;
    p->player_id = c->player_id;
    p->land_id = c->land_id;
}

void pc_character_home_pid_be(const PCCharacter* c, uint8_t out[20]) {
    memcpy(out, c->name_bytes, 8);
    memcpy(out + 8, c->home_town_bytes, 8);
    out[16] = (uint8_t)(c->player_id >> 8);
    out[17] = (uint8_t)c->player_id;
    out[18] = (uint8_t)(c->land_id >> 8);
    out[19] = (uint8_t)c->land_id;
}

size_t pc_character_format(const PCCharacter* c, char* out, size_t cap) {
    char nb[17], hb[17];
    int n;
    hex_encode(c->name_bytes, 8, nb);
    hex_encode(c->home_town_bytes, 8, hb);
    n = snprintf(out, cap,
                 "# ACGC PC multiplayer character (player-owned identity; see docs/multiplayer-guest-roadmap.md). uuid is local only.\n"
                 "uuid = %s\nname = %s\nname_bytes = %s\nhome_town = %s\nhome_town_bytes = %s\nplayer_id = 0x%04X\nland_id = 0x%04X\ngender = %d\nface = %d\n"
                 "created = %lu\n%s%s%s",
                 c->uuid, c->name, nb, c->home_town, hb, (unsigned)c->player_id, (unsigned)c->land_id, c->gender, c->face, (unsigned long)c->created,
                 c->has_legacy ? "legacy_profile = " : "", c->has_legacy ? c->legacy_profile : "", c->has_legacy ? "\n" : "");
    return (n > 0 && (size_t)n < cap) ? (size_t)n : 0;
}

static int parse_num(const char* s, unsigned long* out) {
    char* end = NULL;
    unsigned long v;
    if (*s == '\0') {
        return 0;
    }
    v = strtoul(s, &end, 0);
    if (end == NULL || *end != '\0') {
        return 0;
    }
    *out = v;
    return 1;
}

enum { C_UUID, C_NAME, C_NAMEB, C_HOME, C_HOMEB, C_PID, C_LID, C_GENDER, C_FACE, C_CREATED, C_LEGACY, C_COUNT };
static const char* const k_ckeys[C_COUNT] = { "uuid", "name", "name_bytes", "home_town", "home_town_bytes", "player_id", "land_id", "gender", "face", "created", "legacy_profile" };

int pc_character_parse(const char* text, size_t len, PCCharacter* out, char* err, size_t errcap) {
    char buf[PC_GUEST_PROFILE_MAX_FILE + 1];
    char* line;
    char* nextp;
    unsigned seen = 0;
    int lineno = 0, k;
    PCCharacter c;
    PCGuestProfile p;
    const char* bk = NULL;
    const char* why = NULL;
    memset(&c, 0, sizeof(c));
    if (text == NULL || len > PC_GUEST_PROFILE_MAX_FILE) {
        seterr(err, errcap, "%s", "the file is too large (more than 4096 bytes)", NULL);
        return 0;
    }
    memcpy(buf, text, len);
    buf[len] = '\0';
    line = buf;
    if ((unsigned char)line[0] == 0xEF && (unsigned char)line[1] == 0xBB && (unsigned char)line[2] == 0xBF) {
        line += 3;
    }
    for (; line != NULL && *line != '\0'; line = nextp) {
        char* eq;
        char* key;
        char* val;
        char* e;
        nextp = strchr(line, '\n');
        if (nextp != NULL) {
            *nextp++ = '\0';
        }
        lineno++;
        while (*line == ' ' || *line == '\t') {
            line++;
        }
        e = line + strlen(line);
        while (e > line && (e[-1] == ' ' || e[-1] == '\t' || e[-1] == '\r')) {
            *--e = '\0';
        }
        if (*line == '\0' || *line == '#' || *line == ';' || *line == '[') {
            continue;
        }
        eq = strchr(line, '=');
        if (eq == NULL) {
            char m[40];
            snprintf(m, sizeof(m), "%d", lineno);
            seterr(err, errcap, "line %s: expected key = value", m, NULL);
            return 0;
        }
        *eq = '\0';
        key = line;
        val = eq + 1;
        e = key + strlen(key);
        while (e > key && (e[-1] == ' ' || e[-1] == '\t')) {
            *--e = '\0';
        }
        while (*val == ' ' || *val == '\t') {
            val++;
        }
        for (k = 0; k < C_COUNT; k++) {
            if (strcmp(key, k_ckeys[k]) == 0) {
                break;
            }
        }
        if (k == C_COUNT) {
            seterr(err, errcap, "%s: unknown key", key, NULL);
            return 0;
        }
        if (seen & (1u << k)) {
            seterr(err, errcap, "%s: key given twice", key, NULL);
            return 0;
        }
        seen |= 1u << k;
        {
            unsigned long v = 0;
            int ok = 1;
            switch (k) {
                case C_UUID: ok = is_uuid(val); if (ok) memcpy(c.uuid, val, PC_CHARACTER_UUID_LEN + 1); break;
                case C_NAME: ok = strlen(val) >= 1 && strlen(val) <= 8; if (ok) memcpy(c.name, val, strlen(val) + 1); break;
                case C_NAMEB: ok = hex_decode(val, c.name_bytes, 8); break;
                case C_HOME: ok = strlen(val) >= 1 && strlen(val) <= 8; if (ok) memcpy(c.home_town, val, strlen(val) + 1); break;
                case C_HOMEB: ok = hex_decode(val, c.home_town_bytes, 8); break;
                case C_PID: ok = parse_num(val, &v) && v <= 0xFFFFul; c.player_id = (uint16_t)v; break;
                case C_LID: ok = parse_num(val, &v) && v <= 0xFFFFul; c.land_id = (uint16_t)v; break;
                case C_GENDER: ok = parse_num(val, &v) && v <= 1; c.gender = (int)v; break;
                case C_FACE: ok = parse_num(val, &v) && v <= 7; c.face = (int)v; break;
                case C_CREATED: ok = parse_num(val, &v); c.created = (uint32_t)v; break;
                case C_LEGACY:
                    c.has_legacy = 1;
                    ok = val[0] == '\0' || (pc_guest_profile_name_check(val, NULL, 0) && strlen(val) <= PC_GUEST_PROFILE_ARG_MAX);
                    if (ok) pc_guest_profile_fold(val, c.legacy_profile);
                    break;
                default: break;
            }
            if (!ok) {
                seterr(err, errcap, "%s: invalid value", key, NULL);
                return 0;
            }
        }
    }
    for (k = 0; k < C_COUNT; k++) {
        if (k != C_LEGACY && !(seen & (1u << k))) {
            seterr(err, errcap, "%s: required key missing", k_ckeys[k], NULL);
            return 0;
        }
    }
    to_profile(&c, &p);
    if (!pc_guest_profile_validate(&p, &bk, &why)) {
        seterr(err, errcap, "%s: %s", bk, why);
        return 0;
    }
    {
        uint8_t nb[8], hb[8];
        pad8(c.name, nb);
        pad8(c.home_town, hb);
        if (memcmp(nb, c.name_bytes, 8) != 0) {
            seterr(err, errcap, "%s: does not match the name", "name_bytes", NULL);
            return 0;
        }
        if (memcmp(hb, c.home_town_bytes, 8) != 0) {
            seterr(err, errcap, "%s: does not match the home town", "home_town_bytes", NULL);
            return 0;
        }
    }
    c.storage = PC_CHARACTER_STORAGE_STORE;
    *out = c;
    return 1;
}

/* ---------------- paths ---------------- */

static int char_dir(const char* dir, const char* uuid, char* out, size_t cap) {
    int n;
    if (!is_uuid(uuid)) {
        return 0;
    }
    n = snprintf(out, cap, "%s/characters/%s", dir_or_default(dir), uuid);
    return n > 0 && (size_t)n < cap;
}

static int char_file(const char* dir, const char* uuid, char* out, size_t cap) {
    char d[300];
    int n;
    if (!char_dir(dir, uuid, d, sizeof(d))) {
        return 0;
    }
    n = snprintf(out, cap, "%s/character.ini", d);
    return n > 0 && (size_t)n < cap;
}

void pc_character_town_key_format(const uint8_t land_name[8], uint16_t land_id, uint32_t terrain_hash, char out[PC_CHARACTER_TOWNKEY_LEN + 1]) {
    char ln[17];
    hex_encode(land_name, 8, ln);
    snprintf(out, PC_CHARACTER_TOWNKEY_LEN + 1, "%s_%04x_%08x", ln, (unsigned)land_id, (unsigned)terrain_hash);
}

static int townkey_ok(const char* k) {
    size_t i;
    if (strlen(k) != PC_CHARACTER_TOWNKEY_LEN) {
        return 0;
    }
    for (i = 0; i < PC_CHARACTER_TOWNKEY_LEN; i++) {
        if (i == 16 || i == 21) {
            if (k[i] != '_') {
                return 0;
            }
        } else if (!((k[i] >= '0' && k[i] <= '9') || (k[i] >= 'a' && k[i] <= 'f'))) {
            return 0;
        }
    }
    return 1;
}

static int town_file(const char* dir, const char* uuid, const char* townkey, const char* leaf, char* out, size_t cap) {
    char d[300];
    int n;
    if (!char_dir(dir, uuid, d, sizeof(d)) || !townkey_ok(townkey)) {
        return 0;
    }
    n = snprintf(out, cap, "%s/towns/%s/%s", d, townkey, leaf);
    return n > 0 && (size_t)n < cap;
}

int pc_character_token_path(const char* dir, const char* uuid, const char* townkey, char* out, size_t cap) {
    return town_file(dir, uuid, townkey, "token.dat", out, cap);
}

int pc_character_membership_path(const char* dir, const char* uuid, const char* townkey, char* out, size_t cap) {
    return town_file(dir, uuid, townkey, "membership.ini", out, cap);
}

/* Guest-first purchase: membership.ini carries a few CLIENT-side extra keys besides the three the writer owns (role, town_pid, last_server), e.g. `nook_intro =
 * declined | bought` (the Nook "buy a house" offer state). The writer must NEVER drop them: this collects every `key = value` line of the existing file whose key is not
 * one of the three (comment lines and junk are not kept), each as a `key = value\n` line, into `extra` (at most cap-1 bytes; a line that does not fit is skipped whole). */
static void membership_collect_extra(const char* path, char* extra, size_t cap) {
    char text[900];
    size_t n = 0, used = 0;
    const char* p;
    extra[0] = '\0';
    if (!file_exists(path) || !read_all(path, text, sizeof(text) - 1, &n)) {
        return;
    }
    text[n] = '\0';
    for (p = text; *p != '\0';) {
        char line[200];
        size_t l = 0;
        char* eq;
        while (*p != '\0' && *p != '\n' && l + 1 < sizeof(line)) {
            if (*p != '\r') line[l++] = *p;
            p++;
        }
        while (*p != '\0' && *p != '\n') p++;
        if (*p == '\n') p++;
        line[l] = '\0';
        eq = strchr(line, '=');
        if (line[0] == '#' || eq == NULL) continue;
        {
            char* k = line;
            char* v = eq + 1;
            char* ke = eq;
            size_t vl, need;
            while (*k == ' ' || *k == '\t') k++;
            while (ke > k && (ke[-1] == ' ' || ke[-1] == '\t')) ke--;
            *ke = '\0';
            while (*v == ' ' || *v == '\t') v++;
            vl = strlen(v);
            while (vl > 0 && (v[vl - 1] == ' ' || v[vl - 1] == '\t')) v[--vl] = '\0';
            if (k[0] == '\0' || strcmp(k, "role") == 0 || strcmp(k, "town_pid") == 0 || strcmp(k, "last_server") == 0) continue;
            need = strlen(k) + vl + 4; /* "k = v\n" */
            if (used + need + 1 > cap) continue;
            used += (size_t)snprintf(extra + used, cap - used, "%s = %s\n", k, v);
        }
    }
}

int pc_character_membership_write(const char* dir, const char* uuid, const char* townkey, const char* role, const uint8_t town_pid[20], const char* last_server) {
    char path[400], text[700], pid[41], extra[220];
    int n;
    if (!pc_character_membership_path(dir, uuid, townkey, path, sizeof(path)) || role == NULL || (strcmp(role, "guest") != 0 && strcmp(role, "resident") != 0)) {
        return 0;
    }
    hex_encode(town_pid, 20, pid);
    membership_collect_extra(path, extra, sizeof(extra)); /* unknown keys (nook_intro, ...) survive a rewrite */
    n = snprintf(text, sizeof(text), "role = %s\ntown_pid = %s\nlast_server = %s\n%s", role, pid, last_server != NULL ? last_server : "", extra);
    return n > 0 && (size_t)n < sizeof(text) && write_atomic(path, text, (size_t)n, 0);
}

/* Reads the value of an EXTRA key (never role / town_pid / last_server) of membership.ini into out. 1 = present, 0 = absent / no file. */
int pc_character_membership_get_key(const char* dir, const char* uuid, const char* townkey, const char* key, char* out, size_t cap) {
    char path[400], extra[220], needle[64];
    const char* p;
    if (out == NULL || cap < 2 || key == NULL || key[0] == '\0' || strlen(key) > 40 || !pc_character_membership_path(dir, uuid, townkey, path, sizeof(path))) {
        return 0;
    }
    out[0] = '\0';
    membership_collect_extra(path, extra, sizeof(extra));
    snprintf(needle, sizeof(needle), "%s = ", key);
    for (p = extra; *p != '\0';) {
        const char* e = strchr(p, '\n');
        size_t l = e != NULL ? (size_t)(e - p) : strlen(p);
        if (strncmp(p, needle, strlen(needle)) == 0) {
            size_t vl = l - strlen(needle);
            if (vl >= cap) vl = cap - 1;
            memcpy(out, p + strlen(needle), vl);
            out[vl] = '\0';
            return 1;
        }
        p += l + (e != NULL ? 1 : 0);
    }
    return 0;
}

/* Sets (or replaces) ONE extra key of an EXISTING membership.ini, keeping role / town_pid / last_server and every other extra key. 1 = written, 0 = no file / bad
 * arguments / no room. key: [A-Za-z0-9_]{1,40}, value: printable, at most 60 chars. */
int pc_character_membership_set_key(const char* dir, const char* uuid, const char* townkey, const char* key, const char* value) {
    char path[400], text[900], extra[220], nextra[220], needle[64];
    char role[16];
    uint8_t pid_bytes[20];
    char pid[41], last[200];
    size_t i, nl = 0, n;
    const char* p;
    if (key == NULL || value == NULL || key[0] == '\0' || strlen(key) > 40 || strlen(value) > 60 || !pc_character_membership_path(dir, uuid, townkey, path, sizeof(path))) {
        return 0;
    }
    for (i = 0; key[i] != '\0'; i++) {
        const char c = key[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_')) return 0;
    }
    for (i = 0; value[i] != '\0'; i++) {
        if ((unsigned char)value[i] < 0x20 || (unsigned char)value[i] > 0x7E) return 0;
    }
    if (strcmp(key, "role") == 0 || strcmp(key, "town_pid") == 0 || strcmp(key, "last_server") == 0) {
        return 0;
    }
    if (!pc_character_membership_read(dir, uuid, townkey, role, pid_bytes)) {
        return 0;
    }
    hex_encode(pid_bytes, 20, pid);
    last[0] = '\0';
    {   /* last_server is read raw (pc_character_membership_read does not return it) */
        char t2[900];
        size_t n2 = 0;
        if (read_all(path, t2, sizeof(t2) - 1, &n2)) {
            t2[n2] = '\0';
            for (p = t2; *p != '\0';) {
                const char* e = strchr(p, '\n');
                size_t l = e != NULL ? (size_t)(e - p) : strlen(p);
                if (strncmp(p, "last_server", 11) == 0) {
                    const char* eq = memchr(p, '=', l);
                    if (eq != NULL) {
                        size_t vl;
                        eq++;
                        while (eq < p + l && (*eq == ' ' || *eq == '\t')) eq++;
                        vl = (size_t)(p + l - eq);
                        while (vl > 0 && (eq[vl - 1] == ' ' || eq[vl - 1] == '\t' || eq[vl - 1] == '\r')) vl--;
                        if (vl >= sizeof(last)) vl = sizeof(last) - 1;
                        memcpy(last, eq, vl);
                        last[vl] = '\0';
                    }
                    break;
                }
                p += l + (e != NULL ? 1 : 0);
            }
        }
    }
    membership_collect_extra(path, extra, sizeof(extra));
    snprintf(needle, sizeof(needle), "%s = ", key);
    nextra[0] = '\0';
    for (p = extra; *p != '\0';) { /* every extra line except the replaced key */
        const char* e = strchr(p, '\n');
        size_t l = e != NULL ? (size_t)(e - p) : strlen(p);
        if (strncmp(p, needle, strlen(needle)) != 0 && nl + l + 2 < sizeof(nextra)) {
            memcpy(nextra + nl, p, l);
            nextra[nl + l] = '\n';
            nl += l + 1;
            nextra[nl] = '\0';
        }
        p += l + (e != NULL ? 1 : 0);
    }
    if (nl + strlen(key) + strlen(value) + 5 >= sizeof(nextra)) {
        return 0;
    }
    nl += (size_t)snprintf(nextra + nl, sizeof(nextra) - nl, "%s = %s\n", key, value);
    {
        int w = snprintf(text, sizeof(text), "role = %s\ntown_pid = %s\nlast_server = %s\n%s", role, pid, last, nextra);
        n = w > 0 ? (size_t)w : 0;
    }
    return n > 0 && n < sizeof(text) && write_atomic(path, text, n, 0);
}

/* M-C: reads membership.ini. 1 = a valid file (role "guest" / "resident" and a 40-hex town_pid), 0 = absent / unreadable / malformed (never an error: the caller treats
 * "none" as a guest). Read-only. */
int pc_character_membership_read(const char* dir, const char* uuid, const char* townkey, char role_out[16], uint8_t town_pid[20]) {
    char path[400], text[900];
    size_t n = 0;
    const char* p;
    int have_role = 0, have_pid = 0;
    role_out[0] = '\0';
    if (!pc_character_membership_path(dir, uuid, townkey, path, sizeof(path)) || !file_exists(path) || !read_all(path, text, sizeof(text) - 1, &n)) {
        return 0;
    }
    text[n] = '\0';
    for (p = text; *p != '\0';) {
        char line[200];
        size_t l = 0;
        char* eq;
        while (*p != '\0' && *p != '\n' && l + 1 < sizeof(line)) {
            if (*p != '\r') line[l++] = *p;
            p++;
        }
        while (*p != '\0' && *p != '\n') p++;
        if (*p == '\n') p++;
        line[l] = '\0';
        eq = strchr(line, '=');
        if (line[0] == '#' || eq == NULL) continue;
        {
            char* k = line;
            char* v = eq + 1;
            char* ke = eq;
            size_t vl;
            while (ke > k && (ke[-1] == ' ' || ke[-1] == '\t')) ke--;
            *ke = '\0';
            while (*v == ' ' || *v == '\t') v++;
            vl = strlen(v);
            while (vl > 0 && (v[vl - 1] == ' ' || v[vl - 1] == '\t')) v[--vl] = '\0';
            if (strcmp(k, "role") == 0 && (strcmp(v, "guest") == 0 || strcmp(v, "resident") == 0)) {
                snprintf(role_out, 16, "%s", v);
                have_role = 1;
            } else if (strcmp(k, "town_pid") == 0 && strlen(v) == 40 && hex_decode(v, town_pid, 20)) {
                have_pid = 1;
            }
        }
    }
    if (!have_role || !have_pid) {
        role_out[0] = '\0';
        return 0;
    }
    return 1;
}

/* ---------------- load / list ---------------- */

int pc_character_load(const char* dir, const char* uuid, PCCharacter* out, char* err, size_t errcap) {
    char path[320], text[PC_GUEST_PROFILE_MAX_FILE + 2], why[200];
    size_t n = 0;
    PCCharacter c;
    if (!char_file(dir, uuid, path, sizeof(path))) {
        seterr(err, errcap, "%s: not a character uuid", uuid, NULL);
        return PC_CHARACTER_ERR;
    }
    if (!file_exists(path)) {
        return PC_CHARACTER_ABSENT;
    }
    if (!read_all(path, text, PC_GUEST_PROFILE_MAX_FILE + 1, &n)) {
        seterr(err, errcap, "%s: cannot be read", path, NULL);
        return PC_CHARACTER_ERR;
    }
    why[0] = '\0';
    if (!pc_character_parse(text, n, &c, why, sizeof(why))) {
        seterr(err, errcap, "%s: %s", path, why);
        return PC_CHARACTER_ERR;
    }
    if (strcmp(c.uuid, uuid) != 0) {
        seterr(err, errcap, "%s: uuid does not match the directory name", path, NULL);
        return PC_CHARACTER_ERR;
    }
    snprintf(c.path, sizeof(c.path), "%s", path);
    *out = c;
    return PC_CHARACTER_OK;
}

#define CINI_CAP 8192
static int cini_load(const char* dir, char* text);
static int cini_get(const char* text, const char* key, char* out, size_t cap);
static int csv_has(const char* list, const char* tok);
static void apply_order(const char* dir, PCCharacter* out, int n);
static void label_key(const char* uuid, char* out, size_t cap);
static int cmp_legacy_profile(const void* a, const void* b);

typedef struct ListCtx {
    const char* dir;
    PCCharacter* out;
    int cap, n, skipped;
    int legacy_pass;
    char hidden[512]; /* characters.ini hidden_profiles: deliberately deleted imported profiles that must not reappear as legacy rows */
} ListCtx;

static void from_profile(const PCGuestProfile* p, PCCharacter* c) {
    memset(c, 0, sizeof(*c));
    memcpy(c->name, p->name, sizeof(c->name));
    memcpy(c->home_town, p->home_town, sizeof(c->home_town));
    pc_guest_profile_name_bytes(p, c->name_bytes);
    pc_guest_profile_home_bytes(p, c->home_town_bytes);
    c->gender = p->gender;
    c->face = p->face;
    c->player_id = p->player_id;
    c->land_id = p->land_id;
}

static void list_store_cb(const char* name, int is_dir, void* ud) {
    ListCtx* x = (ListCtx*)ud;
    PCCharacter c;
    char err[400];
    int r;
    if (!is_dir || !is_uuid(name) || x->n >= x->cap) {
        return;
    }
    r = pc_character_load(x->dir, name, &c, err, sizeof(err));
    if (r == PC_CHARACTER_OK) {
        x->out[x->n++] = c;
    } else if (r == PC_CHARACTER_ERR) {
        fprintf(stderr, "[PC] characters: skipped (not readable / not a valid character, NOT touched): %s\n", err);
        x->skipped++;
    }
}

static void list_legacy_cb(const char* nm, int is_dir, void* ud) {
    ListCtx* x = (ListCtx*)ud;
    const size_t len = strlen(nm);
    char prof[PC_GUEST_PROFILE_ARG_MAX + 1], full[400], err[400];
    PCGuestProfile gp;
    PCCharacter c;
    int k;
    if (is_dir || x->n >= x->cap) {
        return;
    }
    if (ieq(nm, "guest.ini")) {
        prof[0] = '\0';
    } else if (len > 10 && ieq(nm + len - 4, ".ini") && strncmp(nm, "guest_", 6) == 0 && len - 10 <= PC_GUEST_PROFILE_ARG_MAX) {
        memcpy(prof, nm + 6, len - 10);
        prof[len - 10] = '\0';
        if (!pc_guest_profile_name_check(prof, NULL, 0)) {
            return;
        }
        pc_guest_profile_fold(prof, prof);
    } else {
        return;
    }
    if (x->hidden[0] != '\0' && csv_has(x->hidden, prof[0] != '\0' ? prof : "(default)")) {
        return;
    }
    for (k = 0; k < x->n; k++) { /* already imported (a store character names this legacy profile) */
        if (x->out[k].storage == PC_CHARACTER_STORAGE_STORE && x->out[k].has_legacy && strcmp(x->out[k].legacy_profile, prof) == 0) {
            return;
        }
    }
    snprintf(full, sizeof(full), "%s/%s", x->dir, nm);
    if (pc_guest_profile_read(full, &gp, err, sizeof(err)) != PC_GUEST_PROFILE_LOADED) {
        fprintf(stderr, "[PC] characters: legacy profile skipped (not readable / not valid, NOT touched): %s\n", err);
        x->skipped++;
        return;
    }
    from_profile(&gp, &c);
    c.storage = PC_CHARACTER_STORAGE_LEGACY;
    c.has_legacy = 1;
    memcpy(c.legacy_profile, prof, strlen(prof) + 1);
    snprintf(c.path, sizeof(c.path), "%s", full);
    x->out[x->n++] = c;
}

static int cmp_uuid(const void* a, const void* b) {
    return strcmp(((const PCCharacter*)a)->uuid, ((const PCCharacter*)b)->uuid);
}

int pc_character_list(const char* dir, PCCharacter* out, int cap, int* skipped) {
    ListCtx x;
    char cdir[300];
    dir = dir_or_default(dir);
    memset(&x, 0, sizeof(x));
    x.dir = dir;
    x.out = out;
    x.cap = cap;
    snprintf(cdir, sizeof(cdir), "%s/characters", dir);
    list_dir(cdir, list_store_cb, &x);
    qsort(out, (size_t)x.n, sizeof(PCCharacter), cmp_uuid);
    apply_order(dir, out, x.n);
    {
        const int ns = x.n;
        char ci[CINI_CAP];
        int li;
        if (cini_load(dir, ci)) {
            cini_get(ci, "hidden_profiles", x.hidden, sizeof(x.hidden));
        }
        for (li = 0; li < ns; li++) {
            char lk[64];
            out[li].label[0] = '\0';
            label_key(out[li].uuid, lk, sizeof(lk));
            if (cini_get(ci, lk, out[li].label, sizeof(out[li].label))) {
                out[li].label[16] = '\0';
            }
        }
        list_dir(dir, list_legacy_cb, &x);
        if (x.n > ns) {
            qsort(out + ns, (size_t)(x.n - ns), sizeof(PCCharacter), cmp_legacy_profile);
        }
    }
    if (skipped != NULL) {
        *skipped = x.skipped;
    }
    return x.n;
}

/* ---------------- create ---------------- */

int pc_character_prepare_new(const char* dir, const char* profile_name, PCCharacter* out, char* err, size_t errcap) {
    static PCCharacter all[PC_CHARACTER_MAX];
    PCGuestProfile sib[PC_CHARACTER_MAX], np;
    char why[200];
    int n, i, attempt;
    PCCharacter c;
    if (!pc_guest_profile_name_check(profile_name, why, sizeof(why))) {
        seterr(err, errcap, "%s", why, NULL);
        return 0;
    }
    n = pc_character_list(dir, all, PC_CHARACTER_MAX, NULL);
    for (i = 0; i < n; i++) {
        to_profile(&all[i], &sib[i]);
    }
    for (attempt = 0; attempt < 8; attempt++) {
        if (!pc_guest_profile_make_unique(profile_name, sib, n, &np)) {
            seterr(err, errcap, "%s", "could not draw a unique character identity (random source failed)", NULL);
            return 0;
        }
        from_profile(&np, &c);
        c.storage = PC_CHARACTER_STORAGE_STORE;
        if (!uuid_new(c.uuid)) {
            seterr(err, errcap, "%s", "the OS random source failed", NULL);
            return 0;
        }
        for (i = 0; i < n && strcmp(all[i].uuid, c.uuid) != 0; i++) {
        }
        if (i == n) {
            c.created = (uint32_t)time(NULL);
            *out = c;
            return 1;
        }
    }
    seterr(err, errcap, "%s", "could not draw a unique uuid", NULL);
    return 0;
}

int pc_character_write_exclusive(const char* dir, const PCCharacter* c, char* err, size_t errcap) {
    char path[320], text[PC_GUEST_PROFILE_MAX_FILE], why[200];
    size_t n;
    PCCharacter chk;
    if (!char_file(dir, c->uuid, path, sizeof(path))) {
        seterr(err, errcap, "%s", "bad character uuid", NULL);
        return -1;
    }
    n = pc_character_format(c, text, sizeof(text));
    if (n == 0 || !pc_character_parse(text, n, &chk, why, sizeof(why))) {
        seterr(err, errcap, "%s: the character is not valid: %s", path, why);
        return -1;
    }
    if (file_exists(path)) {
        return 0;
    }
    if (!write_atomic(path, text, n, 1)) {
        if (file_exists(path)) {
            return 0;
        }
        seterr(err, errcap, "%s: could not be written", path, NULL);
        return -1;
    }
    return 1;
}

/* ---------------- legacy import ---------------- */

int pc_character_import_legacy(const char* dir, const char* profile, PCCharacter* out, int* tokens, char* err, size_t errcap) {
    static PCCharacter all[PC_CHARACTER_MAX];
    char ini[320], tok[320], fold[PC_GUEST_PROFILE_ARG_MAX + 1], why[300];
    PCGuestProfile gp;
    PCCharacter c;
    int n, i, copied = 0, r;
    const char* use = profile;
    static uint8_t raw[PC_MP_GTK_FILE_SIZE + 8];
    size_t rn = 0;
    uint8_t pid[20];
    dir = dir_or_default(dir);
    if (tokens != NULL) {
        *tokens = 0;
    }
    if (use != NULL && ieq(use, "default")) {
        char probe[320];
        use = (pc_guest_profile_file_path(dir, "default", 0, probe, sizeof(probe)) && file_exists(probe)) ? "default" : NULL;
    }
    if (use != NULL && use[0] == '\0') {
        use = NULL;
    }
    if (use != NULL && !pc_guest_profile_name_check(use, why, sizeof(why))) {
        seterr(err, errcap, "%s", why, NULL);
        return 0;
    }
    pc_guest_profile_fold(use, fold);
    if (!pc_guest_profile_file_path(dir, use, 0, ini, sizeof(ini)) || !pc_guest_profile_file_path(dir, use, 1, tok, sizeof(tok))) {
        seterr(err, errcap, "%s", "bad profile name", NULL);
        return 0;
    }
    n = pc_character_list(dir, all, PC_CHARACTER_MAX, NULL);
    for (i = 0; i < n; i++) {
        if (all[i].storage == PC_CHARACTER_STORAGE_STORE && all[i].has_legacy && strcmp(all[i].legacy_profile, fold) == 0) {
            seterr(err, errcap, "legacy profile '%s' is already imported as character %s (refused)", fold[0] != '\0' ? fold : "(default)", all[i].uuid);
            return 0;
        }
    }
    r = pc_guest_profile_read(ini, &gp, why, sizeof(why));
    if (r != PC_GUEST_PROFILE_LOADED) {
        seterr(err, errcap, "%s", r == PC_GUEST_PROFILE_ABSENT ? "the legacy profile file does not exist" : why, NULL);
        return 0;
    }
    from_profile(&gp, &c);
    c.storage = PC_CHARACTER_STORAGE_STORE;
    c.has_legacy = 1;
    memcpy(c.legacy_profile, fold, strlen(fold) + 1);
    c.created = (uint32_t)time(NULL);
    {
        int tries = 0;
        do {
            if (!uuid_new(c.uuid)) {
                seterr(err, errcap, "%s", "the OS random source failed", NULL);
                return 0;
            }
            for (i = 0; i < n && strcmp(all[i].uuid, c.uuid) != 0; i++) {
            }
        } while (i != n && ++tries < 8);
        if (i != n) {
            seterr(err, errcap, "%s", "could not draw a unique uuid", NULL);
            return 0;
        }
    }
    pc_character_home_pid_be(&c, pid);
    /* the legacy token file is only READ (never moved aside: pc_mp_gtoken_load would) */
    if (file_exists(tok)) {
        static PCMpGtkFile gf;
        if (!read_all(tok, (char*)raw, sizeof(raw), &rn) || pc_mp_gtoken_parse(raw, rn, &gf) != PC_MP_GST_OK) {
            seterr(err, errcap, "%s: the legacy token file is not readable; nothing was imported (it was NOT touched)", tok, NULL);
            return 0;
        }
        for (i = 0; i < PC_MP_GTK_SLOTS; i++) {
            char key[PC_CHARACTER_TOWNKEY_LEN + 1], tp[400];
            PCMpGtkFile one;
            if (!gf.e[i].present || memcmp(gf.e[i].home_pid, pid, 20) != 0) {
                continue;
            }
            pc_character_town_key_format(gf.e[i].host_land_name, gf.e[i].host_land_id, gf.e[i].host_terrain_hash, key);
            memset(&one, 0, sizeof(one));
            one.e[0] = gf.e[i];
            if (!pc_character_token_path(dir, c.uuid, key, tp, sizeof(tp)) || pc_mp_gtoken_save(tp, &one) != PC_MP_GST_OK ||
                !pc_character_membership_write(dir, c.uuid, key, "guest", pid, "")) {
                seterr(err, errcap, "%s: could not write the imported token (character not created)", tp, NULL);
                return 0;
            }
            copied++;
        }
    }
    r = pc_character_write_exclusive(dir, &c, why, sizeof(why));
    if (r != 1) {
        seterr(err, errcap, "%s", r == 0 ? "the character file already exists" : why, NULL);
        return 0;
    }
    char_file(dir, c.uuid, c.path, sizeof(c.path));
    if (tokens != NULL) {
        *tokens = copied;
    }
    *out = c;
    return 1;
}

/* ---------------- resolve ---------------- */

int pc_character_resolve_profile(const char* dir, const char* profile, PCCharacter* out, char* err, size_t errcap) {
    static PCCharacter all[PC_CHARACTER_MAX];
    char fold[PC_GUEST_PROFILE_ARG_MAX + 1], ini[320], why[300];
    PCGuestProfile gp;
    int n, i, r;
    dir = dir_or_default(dir);
    if (profile != NULL && profile[0] != '\0' && !pc_guest_profile_name_check(profile, why, sizeof(why))) {
        seterr(err, errcap, "%s", why, NULL);
        return PC_CHARACTER_ERR;
    }
    pc_guest_profile_fold(profile, fold);
    n = pc_character_list(dir, all, PC_CHARACTER_MAX, NULL);
    for (i = 0; i < n; i++) {
        if (all[i].storage == PC_CHARACTER_STORAGE_STORE && all[i].has_legacy && strcmp(all[i].legacy_profile, fold) == 0) {
            *out = all[i];
            return PC_CHARACTER_OK;
        }
    }
    if (!pc_guest_profile_file_path(dir, fold[0] != '\0' ? fold : NULL, 0, ini, sizeof(ini))) {
        return PC_CHARACTER_ABSENT;
    }
    r = pc_guest_profile_read(ini, &gp, why, sizeof(why));
    if (r == PC_GUEST_PROFILE_ABSENT) {
        return PC_CHARACTER_ABSENT;
    }
    if (r != PC_GUEST_PROFILE_LOADED) {
        seterr(err, errcap, "%s", why, NULL);
        return PC_CHARACTER_ERR;
    }
    from_profile(&gp, out);
    out->storage = PC_CHARACTER_STORAGE_LEGACY;
    out->has_legacy = 1;
    memcpy(out->legacy_profile, fold, strlen(fold) + 1);
    snprintf(out->path, sizeof(out->path), "%s", ini);
    return PC_CHARACTER_OK;
}

int pc_character_resolve(const char* dir, const char* spec, PCCharacter* out, char* err, size_t errcap) {
    static PCCharacter all[PC_CHARACTER_MAX];
    int n, i, hit = -1, hits, hexspec;
    size_t sl;
    if (spec == NULL || spec[0] == '\0') {
        seterr(err, errcap, "%s", "empty character name", NULL);
        return PC_CHARACTER_ERR;
    }
    n = pc_character_list(dir, all, PC_CHARACTER_MAX, NULL);
    /* 1: an imported character that names this legacy profile */
    for (i = 0; i < n; i++) {
        if (all[i].storage == PC_CHARACTER_STORAGE_STORE && all[i].has_legacy && all[i].legacy_profile[0] != '\0' && ieq(all[i].legacy_profile, spec)) {
            *out = all[i];
            return PC_CHARACTER_OK;
        }
    }
    /* 2: exact display name among all characters (store + legacy) */
    for (i = 0, hits = 0; i < n; i++) {
        if (ieq(all[i].name, spec)) {
            hit = i;
            hits++;
        }
    }
    if (hits > 1) {
        seterr(err, errcap, "'%s' matches %s characters: use the uuid prefix (see --characters)", spec, "several");
        return PC_CHARACTER_AMBIGUOUS;
    }
    if (hits == 1) {
        *out = all[hit];
        return PC_CHARACTER_OK;
    }
    /* 3: uuid prefix (>= 4 hex digits) */
    sl = strlen(spec);
    hexspec = sl >= 4 && sl <= PC_CHARACTER_UUID_LEN;
    for (i = 0; hexspec && (size_t)i < sl; i++) {
        hexspec = (spec[i] >= '0' && spec[i] <= '9') || (spec[i] >= 'a' && spec[i] <= 'f');
    }
    if (hexspec) {
        for (i = 0, hits = 0; i < n; i++) {
            if (all[i].storage == PC_CHARACTER_STORAGE_STORE && strncmp(all[i].uuid, spec, sl) == 0) {
                hit = i;
                hits++;
            }
        }
        if (hits > 1) {
            seterr(err, errcap, "uuid prefix '%s' matches %s characters: give more digits", spec, "several");
            return PC_CHARACTER_AMBIGUOUS;
        }
        if (hits == 1) {
            *out = all[hit];
            return PC_CHARACTER_OK;
        }
    }
    /* 4: a legacy profile file named spec (imported ones were handled in 1) */
    for (i = 0; i < n; i++) {
        if (all[i].storage == PC_CHARACTER_STORAGE_LEGACY && ieq(all[i].legacy_profile, spec)) {
            *out = all[i];
            return PC_CHARACTER_OK;
        }
    }
    return PC_CHARACTER_ABSENT;
}

/* ---------------- characters.ini: local UI sidecar ---------------- */

static int cini_load(const char* dir, char* text) {
    char path[320];
    size_t n = 0;
    snprintf(path, sizeof(path), "%s/characters.ini", dir_or_default(dir));
    if (!read_all(path, text, CINI_CAP - 1, &n)) {
        text[0] = '\0';
        return 0;
    }
    text[n] = '\0';
    return 1;
}

/* does the line [p, p+ll) start with `key` followed by optional blanks and '='? returns the value start or NULL */
static const char* cini_line_value(const char* p, size_t ll, const char* key) {
    const size_t kl = strlen(key);
    const char* q;
    const char* end = p + ll;
    if (ll <= kl || strncmp(p, key, kl) != 0) {
        return NULL;
    }
    q = p + kl;
    while (q < end && (*q == ' ' || *q == '\t')) {
        q++;
    }
    if (q >= end || *q != '=') {
        return NULL;
    }
    q++;
    while (q < end && (*q == ' ' || *q == '\t')) {
        q++;
    }
    return q;
}

static int cini_get(const char* text, const char* key, char* out, size_t cap) {
    const char* p = text;
    while (*p) {
        const char* e = strchr(p, '\n');
        const size_t ll = e != NULL ? (size_t)(e - p) : strlen(p);
        const char* v = cini_line_value(p, ll, key);
        if (v != NULL) {
            const char* end = p + ll;
            size_t vl;
            while (end > v && (end[-1] == '\r' || end[-1] == ' ')) {
                end--;
            }
            vl = (size_t)(end - v);
            if (vl >= cap) {
                vl = cap - 1;
            }
            memcpy(out, v, vl);
            out[vl] = '\0';
            return 1;
        }
        p = e != NULL ? e + 1 : p + ll;
    }
    return 0;
}

/* replace (or drop, value NULL / "") ONE key, every other line is kept as it was. Atomic. */
static int cini_put(const char* dir, const char* key, const char* value) {
    char text[CINI_CAP], nw[CINI_CAP], path[320];
    size_t w = 0;
    const char* p;
    cini_load(dir, text);
    p = text;
    while (*p) {
        const char* e = strchr(p, '\n');
        const size_t ll = e != NULL ? (size_t)(e - p) : strlen(p);
        const size_t take = e != NULL ? ll + 1 : ll;
        if (cini_line_value(p, ll, key) == NULL && ll > 0 && w + take < sizeof(nw) - 2) {
            memcpy(nw + w, p, ll);
            w += ll;
            nw[w++] = '\n';
        }
        p += take;
    }
    if (value != NULL && value[0] != '\0') {
        int n = snprintf(nw + w, sizeof(nw) - w, "%s = %s\n", key, value);
        if (n < 0 || (size_t)n >= sizeof(nw) - w) {
            return 0;
        }
        w += (size_t)n;
    }
    snprintf(path, sizeof(path), "%s/characters.ini", dir_or_default(dir));
    return write_atomic(path, nw, w, 0);
}

static int csv_has(const char* list, const char* tok) {
    const size_t tl = strlen(tok);
    const char* p = list;
    while (*p) {
        const char* e = strchr(p, ',');
        const size_t l = e != NULL ? (size_t)(e - p) : strlen(p);
        if (l == tl && strncmp(p, tok, tl) == 0) {
            return 1;
        }
        p += l + (e != NULL ? 1 : 0);
    }
    return 0;
}

static int csv_remove(char* list, const char* tok) {
    char out[CINI_CAP];
    size_t w = 0;
    const char* p = list;
    while (*p) {
        const char* e = strchr(p, ',');
        const size_t l = e != NULL ? (size_t)(e - p) : strlen(p);
        if (!(l == strlen(tok) && strncmp(p, tok, l) == 0) && l > 0) {
            if (w > 0) out[w++] = ',';
            memcpy(out + w, p, l);
            w += l;
        }
        p += l + (e != NULL ? 1 : 0);
    }
    out[w] = '\0';
    memcpy(list, out, w + 1);
    return 1;
}

static void label_key(const char* uuid, char* out, size_t cap) {
    snprintf(out, cap, "label_%s", uuid);
}

int pc_character_label_set(const char* dir, const char* uuid, const char* label) {
    char key[64];
    if (!is_uuid(uuid)) {
        return 0;
    }
    label_key(uuid, key, sizeof(key));
    return cini_put(dir, key, label);
}

/* the stored order (persisted uuids first, the rest stay in uuid order); out[0..n) are the STORE characters */
static void apply_order(const char* dir, PCCharacter* out, int n) {
    char text[CINI_CAP], ord[CINI_CAP];
    const char* p;
    int pos = 0;
    cini_load(dir, text);
    if (!cini_get(text, "order", ord, sizeof(ord))) {
        return;
    }
    p = ord;
    while (*p && pos < n) {
        const char* e = strchr(p, ',');
        const size_t l = e != NULL ? (size_t)(e - p) : strlen(p);
        if (l == PC_CHARACTER_UUID_LEN) {
            int i;
            for (i = pos; i < n; i++) {
                if (strncmp(out[i].uuid, p, l) == 0) {
                    PCCharacter t = out[i];
                    int k;
                    for (k = i; k > pos; k--) {
                        out[k] = out[k - 1];
                    }
                    out[pos++] = t;
                    break;
                }
            }
        }
        p += l + (e != NULL ? 1 : 0);
    }
}

static int cmp_legacy_profile(const void* a, const void* b) {
    return strcmp(((const PCCharacter*)a)->legacy_profile, ((const PCCharacter*)b)->legacy_profile);
}

int pc_character_move(const char* dir, const char* uuid, int delta) {
    PCCharacter all[PC_CHARACTER_MAX];
    char csv[CINI_CAP];
    int n, ns = 0, i, idx = -1, j;
    if (!is_uuid(uuid) || (delta != -1 && delta != 1)) {
        return 0;
    }
    n = pc_character_list(dir, all, PC_CHARACTER_MAX, NULL);
    for (i = 0; i < n; i++) {
        if (all[i].storage == PC_CHARACTER_STORAGE_STORE) {
            if (strcmp(all[i].uuid, uuid) == 0) {
                idx = ns;
            }
            if (ns != i) {
                all[ns] = all[i];
            }
            ns++;
        }
    }
    j = idx + delta;
    if (idx < 0 || j < 0 || j >= ns) {
        return 0;
    }
    {
        PCCharacter t = all[idx];
        all[idx] = all[j];
        all[j] = t;
    }
    csv[0] = '\0';
    for (i = 0; i < ns; i++) {
        if (i > 0) strcat(csv, ",");
        strcat(csv, all[i].uuid);
    }
    return cini_put(dir, "order", csv);
}

typedef struct ResCtx {
    const char* dir;
    const char* uuid;
    int n;
} ResCtx;

static void res_cb(const char* name, int is_dir, void* ud) {
    ResCtx* x = (ResCtx*)ud;
    char role[16];
    uint8_t pid[20];
    if (is_dir && townkey_ok(name) && pc_character_membership_read(x->dir, x->uuid, name, role, pid) && strcmp(role, "resident") == 0) {
        x->n++;
    }
}

int pc_character_resident_count(const char* dir, const char* uuid) {
    ResCtx x;
    char d[400];
    if (!char_dir(dir, uuid, d, sizeof(d) - 16)) {
        return 0;
    }
    x.dir = dir;
    x.uuid = uuid;
    x.n = 0;
    strcat(d, "/towns");
    list_dir(d, res_cb, &x);
    return x.n;
}

typedef struct RmCtx {
    char dir[620];
    int fail;
} RmCtx;

static void rm_cb(const char* name, int is_dir, void* ud) {
    RmCtx* x = (RmCtx*)ud;
    char p[700];
    snprintf(p, sizeof(p), "%s/%s", x->dir, name);
    if (is_dir) {
        RmCtx sub;
        snprintf(sub.dir, sizeof(sub.dir), "%s", p);
        sub.fail = 0;
        list_dir(p, rm_cb, &sub);
        if (sub.fail) x->fail = 1;
#ifdef _WIN32
        if (!RemoveDirectoryA(p)) x->fail = 1;
#else
        if (rmdir(p) != 0) x->fail = 1;
#endif
    } else if (remove(p) != 0) {
        x->fail = 1;
    }
}

int pc_character_delete(const char* dir, const char* uuid, char* err, size_t errcap) {
    PCCharacter c;
    char d[400], text[CINI_CAP], buf[CINI_CAP], key[64], ierr[300];
    RmCtx x;
    int r;
    if (!char_dir(dir, uuid, d, sizeof(d))) {
        seterr(err, errcap, "not a character uuid", NULL, NULL);
        return 0;
    }
    r = pc_character_load(dir, uuid, &c, ierr, sizeof(ierr));
    if (r == PC_CHARACTER_ABSENT) {
        seterr(err, errcap, "no such character", NULL, NULL);
        return 0;
    }
    if (r == PC_CHARACTER_OK && c.has_legacy) { /* hide the imported profile BEFORE the store entry goes, so it can never flash back as a legacy row */
        const char* tok = c.legacy_profile[0] != '\0' ? c.legacy_profile : "(default)";
        cini_load(dir, text);
        if (!cini_get(text, "hidden_profiles", buf, sizeof(buf))) {
            buf[0] = '\0';
        }
        if (!csv_has(buf, tok)) {
            if (buf[0] != '\0') strcat(buf, ",");
            strncat(buf, tok, sizeof(buf) - strlen(buf) - 1);
            if (!cini_put(dir, "hidden_profiles", buf)) {
                seterr(err, errcap, "could not record the hidden legacy profile (nothing deleted)", NULL, NULL);
                return 0;
            }
        }
    }
    snprintf(x.dir, sizeof(x.dir), "%s", d);
    x.fail = 0;
    list_dir(d, rm_cb, &x);
#ifdef _WIN32
    if (!RemoveDirectoryA(d)) x.fail = 1;
#else
    if (rmdir(d) != 0) x.fail = 1;
#endif
    cini_load(dir, text);
    if (cini_get(text, "default", buf, sizeof(buf)) && strncmp(buf, uuid, PC_CHARACTER_UUID_LEN) == 0) {
        cini_put(dir, "default", NULL);
    }
    if (cini_get(text, "order", buf, sizeof(buf))) {
        csv_remove(buf, uuid);
        cini_put(dir, "order", buf);
    }
    label_key(uuid, key, sizeof(key));
    cini_put(dir, key, NULL);
    if (x.fail) {
        seterr(err, errcap, "some files of the character could not be removed", NULL, NULL);
        return 0;
    }
    return 1;
}

/* ---------------- default ---------------- */

int pc_character_default_get(const char* dir, char out[PC_CHARACTER_UUID_LEN + 1]) {
    char text[CINI_CAP], v[128];
    if (!cini_load(dir, text) || !cini_get(text, "default", v, sizeof(v)) || strlen(v) < PC_CHARACTER_UUID_LEN) {
        return 0;
    }
    memcpy(out, v, PC_CHARACTER_UUID_LEN);
    out[PC_CHARACTER_UUID_LEN] = '\0';
    return is_uuid(out);
}

int pc_character_default_set(const char* dir, const char* uuid) {
    if (!is_uuid(uuid)) {
        return 0;
    }
    return cini_put(dir, "default", uuid);
}
