/* pc_town_cache.c - see pc_town_cache.h. Pure C, no game / network / SDL dependency. */
#include "pc_town_cache.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#ifdef _WIN32
#include <direct.h>
#include <windows.h>
#endif

/* ================================================================== M-A: runtime card dir */
#define PC_CARD_DEFAULT_A_DIR "save/card_a"

/* dst = a + b (b starts with its own separator); 0 (dst emptied) when it does not fit. Explicit lengths instead of snprintf: exact, and no format-truncation noise. */
static int path_cat(char* dst, size_t cap, const char* a, const char* b) {
    const size_t la = strlen(a), lb = strlen(b);
    if (la + lb + 1 > cap) {
        if (cap > 0) dst[0] = '\0';
        return 0;
    }
    memcpy(dst, a, la);
    memcpy(dst + la, b, lb + 1);
    return 1;
}
static char s_town_dir[PC_TOWN_PATH_MAX];
static char s_card_a_dir[PC_TOWN_PATH_MAX] = PC_CARD_DEFAULT_A_DIR;
static char s_gci_path[PC_TOWN_PATH_MAX + 40] = PC_CARD_DEFAULT_A_DIR "/" PC_TOWN_GCI_FILENAME;
static char s_gci_tmp_path[PC_TOWN_PATH_MAX + 48] = PC_CARD_DEFAULT_A_DIR "/" PC_TOWN_GCI_FILENAME ".tmp";

int pc_card_set_town_dir(const char* town_dir) {
    size_t n;
    if (town_dir == NULL || town_dir[0] == '\0') {
        return 0;
    }
    n = strlen(town_dir);
    while (n > 1 && (town_dir[n - 1] == '/' || town_dir[n - 1] == '\\')) {
        n--; /* trailing separators are not part of the name */
    }
    if (n == 0 || n + 8 >= PC_TOWN_PATH_MAX) {
        return 0;
    }
    if (s_town_dir[0] != '\0') {
        return (strlen(s_town_dir) == n && strncmp(s_town_dir, town_dir, n) == 0) ? 1 : 0;
    }
    memcpy(s_town_dir, town_dir, n);
    s_town_dir[n] = '\0';
    path_cat(s_card_a_dir, sizeof(s_card_a_dir), s_town_dir, "/card_a");
    path_cat(s_gci_path, sizeof(s_gci_path), s_card_a_dir, "/" PC_TOWN_GCI_FILENAME);
    path_cat(s_gci_tmp_path, sizeof(s_gci_tmp_path), s_gci_path, ".tmp");
    return 1;
}

const char* pc_card_town_dir(void) { return s_town_dir[0] != '\0' ? s_town_dir : NULL; }
int pc_card_town_dir_active(void) { return s_town_dir[0] != '\0'; }
const char* pc_card_a_dir(void) { return s_card_a_dir; }
const char* pc_gci_path(void) { return s_gci_path; }
const char* pc_gci_tmp_path(void) { return s_gci_tmp_path; }

void pc_card_reset_town_dir_for_test(void) {
    s_town_dir[0] = '\0';
    snprintf(s_card_a_dir, sizeof(s_card_a_dir), "%s", PC_CARD_DEFAULT_A_DIR);
    path_cat(s_gci_path, sizeof(s_gci_path), s_card_a_dir, "/" PC_TOWN_GCI_FILENAME);
    path_cat(s_gci_tmp_path, sizeof(s_gci_tmp_path), s_gci_path, ".tmp");
}

/* ================================================================== identity / key / paths */
int pc_town_id_equal(const PCTownId* a, const PCTownId* b) {
    return a->land_id == b->land_id && a->terrain_hash == b->terrain_hash && memcmp(a->land_name, b->land_name, 8) == 0;
}

void pc_town_key_format(const PCTownId* t, char out[PC_TOWN_KEY_LEN + 1]) {
    static const char hx[] = "0123456789abcdef";
    int i, o = 0;
    for (i = 0; i < 8; i++) {
        out[o++] = hx[t->land_name[i] >> 4];
        out[o++] = hx[t->land_name[i] & 15];
    }
    snprintf(out + o, PC_TOWN_KEY_LEN + 1 - (size_t)o, "_%04x_%08x", (unsigned)t->land_id, (unsigned)t->terrain_hash);
}

int pc_town_key_valid(const char* k) {
    size_t i;
    if (k == NULL || strlen(k) != PC_TOWN_KEY_LEN) {
        return 0;
    }
    for (i = 0; i < PC_TOWN_KEY_LEN; i++) {
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

int pc_town_paths(const char* root, const char* key, PCTownPaths* out) {
    int n;
    memset(out, 0, sizeof(*out));
    if (!pc_town_key_valid(key)) {
        return 0;
    }
    if (root == NULL || root[0] == '\0') {
        root = PC_TOWN_DEFAULT_ROOT;
    }
    n = snprintf(out->town_dir, sizeof(out->town_dir), "%s/%s", root, key);
    if (n <= 0 || (size_t)n >= sizeof(out->town_dir) - 40) {
        memset(out, 0, sizeof(*out));
        return 0;
    }
    if (!path_cat(out->card_dir, sizeof(out->card_dir), out->town_dir, "/card_a") ||
        !path_cat(out->gci, sizeof(out->gci), out->card_dir, "/" PC_TOWN_GCI_FILENAME) ||
        !path_cat(out->incoming_dir, sizeof(out->incoming_dir), out->town_dir, "/incoming") ||
        !path_cat(out->origin_ini, sizeof(out->origin_ini), out->town_dir, "/origin.ini")) {
        memset(out, 0, sizeof(*out));
        return 0;
    }
    {
        const size_t il = strlen(out->incoming_dir);
        if (il + 11 >= sizeof(out->part)) {
            memset(out, 0, sizeof(*out));
            return 0;
        }
        memcpy(out->part, out->incoming_dir, il);
        memcpy(out->part + il, "/town.part", 11); /* incl. the NUL */
    }
    return 1;
}

static int dir_exists(const char* p) {
    struct stat st;
    return stat(p, &st) == 0 && (st.st_mode & S_IFDIR) != 0;
}

int pc_town_mkdirs(const char* path) {
    char buf[PC_TOWN_PATH_MAX];
    size_t i, n;
    if (path == NULL || path[0] == '\0' || strlen(path) >= sizeof(buf)) {
        return 0;
    }
    snprintf(buf, sizeof(buf), "%s", path);
    n = strlen(buf);
    for (i = 1; i <= n; i++) {
        if (buf[i] == '\\') {
            buf[i] = '/';
        }
        if (buf[i] == '/' || buf[i] == '\0') {
            char c = buf[i];
            buf[i] = '\0';
            if (!(i == 2 && buf[1] == ':') && !dir_exists(buf)) {
#ifdef _WIN32
                _mkdir(buf);
#else
                mkdir(buf, 0755);
#endif
            }
            buf[i] = c;
        }
    }
    return dir_exists(buf);
}

/* ================================================================== CRC32 */
uint32_t pc_town_crc32_update(uint32_t crc, const void* data, size_t len) {
    static uint32_t table[256];
    static int init = 0;
    const uint8_t* p = (const uint8_t*)data;
    size_t i;
    if (!init) {
        uint32_t n, k;
        for (n = 0; n < 256; n++) {
            uint32_t c = n;
            for (k = 0; k < 8; k++) {
                c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
            }
            table[n] = c;
        }
        init = 1;
    }
    crc = ~crc;
    for (i = 0; i < len; i++) {
        crc = table[(crc ^ p[i]) & 0xFFu] ^ (crc >> 8);
    }
    return ~crc;
}

uint32_t pc_town_crc32(const void* data, size_t len) {
    return pc_town_crc32_update(0, data, len);
}

int pc_town_file_crc(const char* path, uint32_t* crc_out, uint32_t* size_out) {
    FILE* f = fopen(path, "rb");
    uint8_t buf[4096];
    uint32_t crc = 0, total = 0;
    size_t n;
    if (f == NULL) {
        return 0;
    }
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) {
        crc = pc_town_crc32_update(crc, buf, n);
        total += (uint32_t)n;
    }
    if (ferror(f)) {
        fclose(f);
        return 0;
    }
    fclose(f);
    if (crc_out) *crc_out = crc;
    if (size_out) *size_out = total;
    return 1;
}

/* ================================================================== atomic install / replace */
static int replace_file(const char* from, const char* to) {
#ifdef _WIN32
    return MoveFileExA(from, to, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) ? 1 : 0;
#else
    return rename(from, to) == 0;
#endif
}

int pc_town_cache_install(const char* part, const char* gci) {
    char dir[PC_TOWN_PATH_MAX];
    char* slash;
    struct stat st;
    if (part == NULL || gci == NULL || stat(part, &st) != 0 || strlen(gci) >= sizeof(dir)) {
        return 0; /* a missing part never touches the existing cache */
    }
    snprintf(dir, sizeof(dir), "%s", gci);
    slash = strrchr(dir, '/');
    if (slash == NULL) {
        slash = strrchr(dir, '\\');
    }
    if (slash != NULL) {
        *slash = '\0';
        if (!pc_town_mkdirs(dir)) {
            return 0;
        }
    }
    return replace_file(part, gci);
}

/* ================================================================== origin.ini */
int pc_town_origin_write(const char* path, const PCTownOrigin* o) {
    char tmp[PC_TOWN_PATH_MAX + 8];
    char dir[PC_TOWN_PATH_MAX];
    char* slash;
    FILE* f;
    if (path == NULL || o == NULL || strlen(path) >= PC_TOWN_PATH_MAX) {
        return 0;
    }
    snprintf(dir, sizeof(dir), "%s", path);
    slash = strrchr(dir, '/');
    if (slash == NULL) {
        slash = strrchr(dir, '\\');
    }
    if (slash != NULL) {
        *slash = '\0';
        if (!pc_town_mkdirs(dir)) {
            return 0;
        }
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    f = fopen(tmp, "wb");
    if (f == NULL) {
        return 0;
    }
    fprintf(f, "[origin]\nserver = %s\naddress = %s\nport = %u\nlast_fetch = %lld\n", o->server_name, o->address, (unsigned)o->port, (long long)o->last_fetch);
    if (fflush(f) != 0 || ferror(f)) {
        fclose(f);
        remove(tmp);
        return 0;
    }
    fclose(f);
    if (!replace_file(tmp, path)) {
        remove(tmp);
        return 0;
    }
    return 1;
}

int pc_town_origin_read(const char* path, PCTownOrigin* o) {
    FILE* f = fopen(path, "rb");
    char line[256];
    int have_addr = 0, have_port = 0;
    if (f == NULL) {
        return 0;
    }
    memset(o, 0, sizeof(*o));
    while (fgets(line, sizeof(line), f) != NULL) {
        char* eq = strchr(line, '=');
        char* k = line;
        char* v;
        size_t n;
        if (eq == NULL) {
            continue;
        }
        *eq = '\0';
        v = eq + 1;
        while (*k == ' ' || *k == '\t') k++;
        n = strlen(k);
        while (n > 0 && (k[n - 1] == ' ' || k[n - 1] == '\t')) k[--n] = '\0';
        while (*v == ' ' || *v == '\t') v++;
        n = strlen(v);
        while (n > 0 && (v[n - 1] == '\r' || v[n - 1] == '\n' || v[n - 1] == ' ' || v[n - 1] == '\t')) v[--n] = '\0';
        if (strcmp(k, "server") == 0) {
            snprintf(o->server_name, sizeof(o->server_name), "%s", v);
        } else if (strcmp(k, "address") == 0) {
            snprintf(o->address, sizeof(o->address), "%s", v);
            have_addr = v[0] != '\0';
        } else if (strcmp(k, "port") == 0) {
            long p = strtol(v, NULL, 10);
            if (p > 0 && p <= 65535) {
                o->port = (uint16_t)p;
                have_port = 1;
            }
        } else if (strcmp(k, "last_fetch") == 0) {
            o->last_fetch = (int64_t)strtoll(v, NULL, 10);
        }
    }
    fclose(f);
    return have_addr && have_port;
}

int pc_town_cache_find_by_server(const char* root, const char* address, uint16_t port, char key_out[PC_TOWN_KEY_LEN + 1], PCTownPaths* paths_out) {
    DIR* d;
    struct dirent* ent;
    int found = 0;
    int64_t best = -1;
    if (root == NULL || root[0] == '\0') {
        root = PC_TOWN_DEFAULT_ROOT;
    }
    d = opendir(root);
    if (d == NULL) {
        return 0;
    }
    while ((ent = readdir(d)) != NULL) {
        PCTownPaths p;
        PCTownOrigin o;
        struct stat st;
        if (!pc_town_key_valid(ent->d_name) || !pc_town_paths(root, ent->d_name, &p)) {
            continue;
        }
        if (stat(p.gci, &st) != 0 || !pc_town_origin_read(p.origin_ini, &o)) {
            continue;
        }
        if (o.port == port && strcmp(o.address, address) == 0 && o.last_fetch > best) {
            best = o.last_fetch;
            found = 1;
            memcpy(key_out, ent->d_name, PC_TOWN_KEY_LEN + 1);
            if (paths_out) *paths_out = p;
        }
    }
    closedir(d);
    return found;
}

/* ================================================================== .part writer */
int pc_town_part_begin(PCTownPart* p, const char* incoming_dir, const char* part_path) {
    memset(p, 0, sizeof(*p));
    if (part_path == NULL || strlen(part_path) >= sizeof(p->path) || !pc_town_mkdirs(incoming_dir)) {
        p->failed = 1;
        return 0;
    }
    snprintf(p->path, sizeof(p->path), "%s", part_path);
    p->fp = fopen(part_path, "wb");
    if (p->fp == NULL) {
        p->failed = 1;
        return 0;
    }
    return 1;
}

int pc_town_part_append(PCTownPart* p, uint32_t offset, const void* data, uint32_t len, uint32_t max_total) {
    if (p->failed || p->fp == NULL) {
        return 0;
    }
    if (offset != p->expected || len == 0 || (uint64_t)offset + len > max_total) {
        p->failed = 1;
        return 0;
    }
    if (fwrite(data, 1, len, (FILE*)p->fp) != len) {
        p->failed = 1;
        return 0;
    }
    p->crc = pc_town_crc32_update(p->crc, data, len);
    p->expected += len;
    return 1;
}

int pc_town_part_finish(PCTownPart* p, uint32_t* size_out, uint32_t* crc_out) {
    int ok = !p->failed && p->fp != NULL;
    if (p->fp != NULL) {
        if (fflush((FILE*)p->fp) != 0 || fclose((FILE*)p->fp) != 0) {
            ok = 0;
        }
        p->fp = NULL;
    }
    if (!ok) {
        p->failed = 1;
        return 0;
    }
    if (size_out) *size_out = p->expected;
    if (crc_out) *crc_out = p->crc;
    return 1;
}

void pc_town_part_abort(PCTownPart* p) {
    if (p->fp != NULL) {
        fclose((FILE*)p->fp);
        p->fp = NULL;
    }
    p->failed = 1;
}
