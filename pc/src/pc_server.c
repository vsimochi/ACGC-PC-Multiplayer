/* pc_server.c - the dedicated server's storage tree (servers/<id>/); see pc_server.h. Pure C + libc + pc_town_cache. */
#include "pc_server.h"

#include "pc_dedicated.h" /* pc_dedicated_say(): the server console */
#include "pc_mp_guests.h"  /* PC_MP_GUESTS_PATH (the legacy default) */
#include "pc_mp_members.h" /* PC_MP_MEMBERS_PATH */
#include "pc_mp_records.h" /* PC_MP_RECORDS_PATH */

#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>

#define PS_PATH 320

typedef struct PSIni {
    char name[48];
    int  has_port;
    int  has_town;
    char town_name_hex[24];
    unsigned long land_id;
    unsigned long terrain_hash;
    char key[48];
    char origin[24];
    char created[40];
} PSIni;

static int  s_active = 0;
static int  s_town_missing = 0;
static int  s_adopted = 0;
static char s_id[PC_SERVER_ID_MAX + 1];
static char s_dir[PS_PATH];
static char s_town_dir[PS_PATH];
static char s_ini[PS_PATH + 16];
static char s_log[PS_PATH + 16];
static char s_guests[PS_PATH + 32], s_members[PS_PATH + 32], s_records[PS_PATH + 32], s_restock[PS_PATH + 32], s_work[PS_PATH + 32];
static PSIni s_cfg;
static char s_server_name[48];
static unsigned s_port = 0;

int pc_server_id_valid(const char* id) {
    size_t i, n;
    if (id == NULL) {
        return 0;
    }
    n = strlen(id);
    if (n < 1 || n > PC_SERVER_ID_MAX) {
        return 0;
    }
    for (i = 0; i < n; i++) {
        const unsigned char c = (unsigned char)id[i];
        if (!(isalnum(c) || c == '_' || c == '-')) {
            return 0;
        }
    }
    return 1;
}

static int ps_exists(const char* p) {
    struct stat st;
    return stat(p, &st) == 0;
}

static long ps_size(const char* p) {
    struct stat st;
    return stat(p, &st) == 0 ? (long)st.st_size : -1;
}

static void ps_seterr(char* err, size_t cap, const char* fmt, ...) {
    va_list ap;
    if (err == NULL || cap == 0) {
        return;
    }
    va_start(ap, fmt);
    vsnprintf(err, cap, fmt, ap);
    va_end(ap);
}

/* Copies src -> dst through dst.tmp (never overwrites: refuses when dst exists). 1 = copied. */
static int ps_copy_new(const char* src, const char* dst) {
    FILE *in, *out;
    char tmp[PS_PATH + 40];
    unsigned char buf[8192];
    size_t n;
    int ok = 1;
    if (ps_exists(dst)) {
        return 0;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", dst);
    in = fopen(src, "rb");
    if (in == NULL) {
        return 0;
    }
    out = fopen(tmp, "wb");
    if (out == NULL) {
        fclose(in);
        return 0;
    }
    while ((n = fread(buf, 1, sizeof(buf), in)) > 0) {
        if (fwrite(buf, 1, n, out) != n) {
            ok = 0;
            break;
        }
    }
    fclose(in);
    if (fflush(out) != 0) {
        ok = 0;
    }
    if (fclose(out) != 0) {
        ok = 0;
    }
    if (!ok || rename(tmp, dst) != 0) {
        remove(tmp);
        return 0;
    }
    return 1;
}

/* a legacy GCI we can be CONFIDENT about: exact size and the GAFE game code of the CARDDir header (the game's own loader validates the rest when it loads it) */
static int ps_gci_plausible(const char* path) {
    FILE* f;
    unsigned char h[4];
    int ok = 0;
    if (ps_size(path) != (long)PC_TOWN_GCI_SIZE) {
        return 0;
    }
    f = fopen(path, "rb");
    if (f == NULL) {
        return 0;
    }
    if (fread(h, 1, 4, f) == 4 && memcmp(h, "GAFE", 4) == 0) {
        ok = 1;
    }
    fclose(f);
    return ok;
}

static void ps_trim(char* s) {
    size_t n = strlen(s);
    while (n > 0 && (s[n - 1] == '\r' || s[n - 1] == '\n' || s[n - 1] == ' ' || s[n - 1] == '\t')) {
        s[--n] = '\0';
    }
}

static int ps_ini_read(const char* path, PSIni* c) {
    FILE* f = fopen(path, "rb");
    char line[200], sec[24] = "";
    memset(c, 0, sizeof(*c));
    if (f == NULL) {
        return 0;
    }
    while (fgets(line, (int)sizeof(line), f) != NULL) {
        char *k, *v, *eq;
        ps_trim(line);
        k = line;
        while (*k == ' ' || *k == '\t') {
            k++;
        }
        if (*k == '\0' || *k == '#' || *k == ';') {
            continue;
        }
        if (*k == '[') {
            snprintf(sec, sizeof(sec), "%.22s", k);
            continue;
        }
        eq = strchr(k, '=');
        if (eq == NULL) {
            continue;
        }
        *eq = '\0';
        v = eq + 1;
        ps_trim(k);
        while (*v == ' ' || *v == '\t') {
            v++;
        }
        if (strcmp(sec, "[server]") == 0) {
            if (strcmp(k, "name") == 0) {
                snprintf(c->name, sizeof(c->name), "%s", v);
            } else if (strcmp(k, "port") == 0) {
                c->has_port = 1;
            } else if (strcmp(k, "created") == 0) {
                snprintf(c->created, sizeof(c->created), "%s", v);
            }
        } else if (strcmp(sec, "[town]") == 0) {
            if (strcmp(k, "land_name") == 0) {
                snprintf(c->town_name_hex, sizeof(c->town_name_hex), "%s", v);
                c->has_town = 1;
            } else if (strcmp(k, "land_id") == 0) {
                c->land_id = strtoul(v, NULL, 0);
            } else if (strcmp(k, "terrain_hash") == 0) {
                c->terrain_hash = strtoul(v, NULL, 0);
            } else if (strcmp(k, "key") == 0) {
                snprintf(c->key, sizeof(c->key), "%s", v);
            } else if (strcmp(k, "origin") == 0) {
                snprintf(c->origin, sizeof(c->origin), "%s", v);
            }
        }
    }
    fclose(f);
    return 1;
}

static void ps_ini_write(const char* path, const PSIni* c, const char* id, unsigned port) {
    char tmp[PS_PATH + 40];
    FILE* f;
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    f = fopen(tmp, "wb");
    if (f == NULL) {
        return;
    }
    fprintf(f, "# Dedicated server configuration (servers/<id>/server.ini). [server] is yours to edit (name: shown in logs and used for the name of a NEW town);\n"
               "# [town] is the authoritative town identity this server owns, written by the server itself: never edit it.\n");
    fprintf(f, "[server]\nid = %s\nname = %s\nport = %u\ncreated = %s\n", id, c->name[0] != '\0' ? c->name : id, port, c->created);
    if (c->has_town) {
        fprintf(f, "\n[town]\nland_name = %s\nland_id = 0x%04lX\nterrain_hash = 0x%08lX\nkey = %s\norigin = %s\n", c->town_name_hex, c->land_id, c->terrain_hash, c->key,
                c->origin[0] != '\0' ? c->origin : "generated");
    }
    fflush(f);
    fclose(f);
    remove(path);
    if (rename(tmp, path) != 0) {
        remove(tmp);
    }
}

int pc_server_open(const char* id, uint16_t port, char* err, size_t errcap) {
    char p[PS_PATH + 64], legacy_gci[PS_PATH];
    const char* sub[] = { "town/card_a", "citizens", "residents", "backups", "logs" };
    size_t i;
    PSIni ini;
    int had_ini;
    if (s_active) {
        return 1;
    }
    if (id == NULL || id[0] == '\0') {
        id = PC_SERVER_DEFAULT_ID;
    }
    if (!pc_server_id_valid(id)) {
        ps_seterr(err, errcap, "bad server id '%s' (letters, digits, '_' and '-', 1..%d characters)", id, PC_SERVER_ID_MAX);
        return 0;
    }
    s_port = (unsigned)port;
    snprintf(s_id, sizeof(s_id), "%s", id);
    snprintf(s_dir, sizeof(s_dir), "%s/%s", PC_SERVER_ROOT, s_id);
    snprintf(s_town_dir, sizeof(s_town_dir), "%s/town", s_dir);
    for (i = 0; i < sizeof(sub) / sizeof(sub[0]); i++) {
        snprintf(p, sizeof(p), "%s/%s", s_dir, sub[i]);
        if (!pc_town_mkdirs(p)) {
            ps_seterr(err, errcap, "cannot create %s", p);
            return 0;
        }
    }
    snprintf(s_ini, sizeof(s_ini), "%s/server.ini", s_dir);
    snprintf(s_log, sizeof(s_log), "%s/logs/server.log", s_dir);
    snprintf(s_guests, sizeof(s_guests), "%s/citizens/guests.dat", s_dir);
    snprintf(s_members, sizeof(s_members), "%s/residents/members.dat", s_dir);
    snprintf(s_records, sizeof(s_records), "%s/residents/records.dat", s_dir);
    snprintf(s_restock, sizeof(s_restock), "%s/shop_restock.ini", s_dir);
    snprintf(s_work, sizeof(s_work), "%s/work_jobs.dat", s_dir);

    had_ini = ps_ini_read(s_ini, &ini);
    if (!had_ini) {
        time_t now = time(NULL);
        struct tm* t = gmtime(&now);
        memset(&ini, 0, sizeof(ini));
        snprintf(ini.name, sizeof(ini.name), "%s", s_id);
        if (t != NULL) {
            strftime(ini.created, sizeof(ini.created), "%Y-%m-%dT%H:%M:%SZ", t);
        }
    } else if (ini.name[0] == '\0') {
        snprintf(ini.name, sizeof(ini.name), "%s", s_id);
    }

    snprintf(p, sizeof(p), "%s/card_a/" PC_TOWN_GCI_FILENAME, s_town_dir);
    snprintf(legacy_gci, sizeof(legacy_gci), "save/card_a/" PC_TOWN_GCI_FILENAME);
    if (ps_exists(p)) {
        s_town_missing = 0;
    } else if (!ini.has_town && strcmp(s_id, PC_SERVER_DEFAULT_ID) == 0 && ps_gci_plausible(legacy_gci)) {
        /* An existing legacy dedicated town: ADOPT it by COPY (the legacy files are never moved, deleted or modified; nothing here is overwritten). */
        if (!ps_copy_new(legacy_gci, p)) {
            ps_seterr(err, errcap, "the legacy town %s could not be copied to %s (nothing was changed there)", legacy_gci, p);
            return 0;
        }
        s_town_missing = 0;
        s_adopted = 1;
        snprintf(ini.origin, sizeof(ini.origin), "legacy");
        printf("[SERVER] adopted the legacy town %s by COPY into %s (the legacy file is untouched and no longer used by this dedicated server)\n", legacy_gci, p);
        if (ps_copy_new(PC_MP_GUESTS_PATH, s_guests)) {
            printf("[SERVER] adopted the legacy guest table %s -> %s\n", PC_MP_GUESTS_PATH, s_guests);
        }
        if (ps_copy_new(PC_MP_MEMBERS_PATH, s_members)) {
            printf("[SERVER] adopted the legacy resident credentials %s -> %s\n", PC_MP_MEMBERS_PATH, s_members);
        }
        if (ps_copy_new(PC_MP_RECORDS_PATH, s_records)) {
            printf("[SERVER] adopted the legacy record lineage %s -> %s\n", PC_MP_RECORDS_PATH, s_records);
        }
    } else if (ini.has_town) {
        /* server.ini says a town was established here but its GCI is gone: never silently generate a different town over a server that owned one. */
        ps_seterr(err, errcap, "server '%s' owned a town (server.ini [town] key %s) but %s is missing. Restore the file from a backup, or delete %s to start this server over with a NEW town",
                  s_id, ini.key, p, s_ini);
        return 0;
    } else {
        s_town_missing = 1;
    }
    ps_ini_write(s_ini, &ini, s_id, (unsigned)port);
    s_cfg = ini;
    snprintf(s_server_name, sizeof(s_server_name), "%s", ini.name);
    if (!pc_card_set_town_dir(s_town_dir)) {
        ps_seterr(err, errcap, "cannot select %s as the town directory", s_town_dir);
        return 0;
    }
    s_active = 1;
    printf("[SERVER] server '%s' (%s): town %s%s; residents %s, guests %s\n", s_id, s_dir, s_town_missing ? "NOT FOUND -> a new town is generated on this first launch" : "found",
           s_adopted ? " (adopted from the legacy save)" : "", s_members, s_guests);
    pc_server_log("server '%s' opened (port %u, town %s)", s_id, (unsigned)port, s_town_missing ? "to be generated" : "existing");
    if (ini.has_town) {
        /* a restart: the authoritative identity in server.ini carries the town name (land_name hex = the 8 game character codes) */
        uint8_t codes[8];
        char text[24];
        int k;
        for (k = 0; k < 8; k++) {
            unsigned v = 0x20;
            if (strlen(ini.town_name_hex) >= (size_t)(2 * k + 2)) {
                char b[3] = { ini.town_name_hex[2 * k], ini.town_name_hex[2 * k + 1], 0 };
                v = (unsigned)strtoul(b, NULL, 16);
            }
            codes[k] = (uint8_t)v;
        }
        pc_server_town_name_text(codes, text);
        pc_dedicated_say("Loading town \"%s\"...", text);
    }
    return 1;
}

int pc_server_active(void) { return s_active; }
const char* pc_server_id(void) { return s_active ? s_id : ""; }
const char* pc_server_dir(void) { return s_active ? s_dir : ""; }
const char* pc_server_town_dir(void) { return s_active ? s_town_dir : ""; }
int pc_server_town_missing(void) { return s_active && s_town_missing; }
int pc_server_adopted_legacy(void) { return s_active && s_adopted; }
const char* pc_server_log_path(void) { return s_active ? s_log : ""; }
const char* pc_server_guests_path(void) { return s_active ? s_guests : PC_MP_GUESTS_PATH; }
const char* pc_server_members_path(void) { return s_active ? s_members : PC_MP_MEMBERS_PATH; }
const char* pc_server_records_path(void) { return s_active ? s_records : PC_MP_RECORDS_PATH; }
const char* pc_server_restock_path(void) { return s_active ? s_restock : "save/mp/shop_restock.ini"; }
const char* pc_server_work_path(void) { return s_active ? s_work : "save/mp/work_jobs.dat"; }

extern int pc_utf8_to_game_code(const char* text); /* pc_typing.c: the game's keyboard-editor charset (-1 = not a valid game character) */

int pc_server_town_name_encode(const char* utf8, uint8_t out[8], char* err, size_t errcap) {
    size_t i = 0, n = 0;
    uint8_t tmp[8];
    int code;
    if (err != NULL && errcap > 0) {
        err[0] = '\0';
    }
    if (utf8 == NULL || utf8[0] == '\0' || utf8[0] == ' ' || utf8[strlen(utf8) - 1] == ' ') {
        goto bad;
    }
    while (utf8[i] != '\0') {
        const unsigned char c = (unsigned char)utf8[i];
        size_t len = c < 0x80 ? 1u : (c >= 0xC0 && c <= 0xDF) ? 2u : 0u;
        if (len == 0 || n >= 8 || (len == 2 && utf8[i + 1] == '\0')) {
            goto bad;
        }
        code = pc_utf8_to_game_code(utf8 + i);
        if (code < 0 || code > 255) {
            goto bad;
        }
        tmp[n++] = (uint8_t)code;
        i += len;
    }
    memset(out, ' ', 8); /* CHAR_SPACE == ' ' (the keyboard map's own value for a space) */
    memcpy(out, tmp, n);
    return 1;
bad:
    ps_seterr(err, errcap, "Invalid town name. Please enter 1-8 valid characters.");
    return 0;
}

static uint8_t s_town_name[8];
static int s_town_name_set = 0;

void pc_server_set_town_name(const uint8_t codes[8]) {
    memcpy(s_town_name, codes, 8);
    s_town_name_set = 1;
}

void pc_server_town_name_codes(uint8_t out[8]) {
    if (s_town_name_set) {
        memcpy(out, s_town_name, 8);
    } else {
        memcpy(out, "Village ", 8);
    }
}

void pc_server_town_name_text(const uint8_t codes[8], char out[24]) {
    size_t i, n = 0;
    for (i = 0; i < 8; i++) {
        out[n++] = (codes[i] >= 0x20 && codes[i] < 0x7F) ? (char)codes[i] : '?';
    }
    while (n > 0 && out[n - 1] == ' ') {
        n--;
    }
    out[n] = '\0';
}

int pc_server_note_town(const PCTownId* t, int generated) {
    char hex[24], key[PC_TOWN_KEY_LEN + 1];
    PSIni cur;
    int i;
    if (!s_active || t == NULL) {
        return 1;
    }
    for (i = 0; i < 8; i++) {
        snprintf(hex + 2 * i, sizeof(hex) - (size_t)(2 * i), "%02X", (unsigned)t->land_name[i]);
    }
    pc_town_key_format(t, key);
    (void)ps_ini_read(s_ini, &cur);
    if (cur.has_town) {
        if (strcmp(cur.town_name_hex, hex) != 0 || cur.land_id != (unsigned long)t->land_id) {
            printf("[SERVER] *** server '%s': server.ini [town] says %s (land_id 0x%04lX) but the loaded town is %s (land_id 0x%04X): REFUSING to serve a different town under this server id ***\n", s_id,
                   cur.key, cur.land_id, key, (unsigned)t->land_id);
            pc_server_log("REFUSED: server.ini town %s != loaded town %s", cur.key, key);
            return 0;
        }
        if (cur.terrain_hash != (unsigned long)t->terrain_hash) {
            printf("[SERVER] server '%s': the town's terrain hash changed (0x%08lX -> 0x%08X); server.ini keeps the first one\n", s_id, cur.terrain_hash, (unsigned)t->terrain_hash);
        }
        printf("[SERVER] server '%s': town identity confirmed (%s)\n", s_id, key);
        pc_dedicated_say("Town loaded.");
        pc_dedicated_say("Server ready.");
        return 1;
    }
    if (cur.name[0] == '\0') {
        snprintf(cur.name, sizeof(cur.name), "%s", s_cfg.name[0] != '\0' ? s_cfg.name : s_id);
    }
    if (cur.created[0] == '\0') {
        snprintf(cur.created, sizeof(cur.created), "%s", s_cfg.created);
    }
    cur.has_town = 1;
    snprintf(cur.town_name_hex, sizeof(cur.town_name_hex), "%s", hex);
    cur.land_id = t->land_id;
    cur.terrain_hash = t->terrain_hash;
    snprintf(cur.key, sizeof(cur.key), "%s", key);
    snprintf(cur.origin, sizeof(cur.origin), "%s", generated ? "generated" : (s_adopted ? "legacy" : "existing"));
    ps_ini_write(s_ini, &cur, s_id, s_port);
    printf("[SERVER] server '%s': town identity recorded in %s (%s, %s)\n", s_id, s_ini, key, cur.origin);
    pc_server_log("town identity recorded: %s (%s)", key, cur.origin);
    {
        char text[24];
        pc_server_town_name_text(t->land_name, text);
        pc_dedicated_say(generated ? "Town \"%s\" generated." : (s_adopted ? "Town \"%s\" adopted from the legacy save." : "Town \"%s\" loaded."), text);
        pc_dedicated_say("Server ready.");
    }
    return 1;
}

void pc_server_log(const char* fmt, ...) {
    FILE* f;
    va_list ap;
    time_t now;
    struct tm* t;
    char ts[32];
    if (!s_active) {
        return;
    }
    f = fopen(s_log, "ab");
    if (f == NULL) {
        return;
    }
    now = time(NULL);
    t = localtime(&now);
    if (t != NULL) {
        strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", t);
    } else {
        snprintf(ts, sizeof(ts), "?");
    }
    fprintf(f, "%s [SERVER] ", ts);
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}
