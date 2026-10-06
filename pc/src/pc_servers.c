/* pc_servers.c - see pc_servers.h. */
#include "pc_servers.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#ifdef _WIN32
#include <direct.h>
#include <io.h>
#include <windows.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

#define SERVERS_DIR_DEFAULT "save/mp"
#define SERVERS_FILE_MAX 65536

static void seterr(char* err, size_t cap, const char* msg) {
    if (err != NULL && cap > 0) {
        snprintf(err, cap, "%s", msg);
    }
}

static int printable(const char* s) {
    for (; *s; s++) {
        if ((unsigned char)*s < 0x20 || (unsigned char)*s > 0x7E) return 0;
    }
    return 1;
}

static int ci_equal(const char* a, const char* b) {
    for (; *a && *b; a++, b++) {
        if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) return 0;
    }
    return *a == *b;
}

int pc_servers_name_check(const char* name, char* err, size_t errcap) {
    size_t n, i;
    if (name == NULL || (n = strlen(name)) < 1 || n > PC_SERVER_NAME_MAX) {
        seterr(err, errcap, "server name must be 1..32 characters");
        return 0;
    }
    if (!printable(name)) {
        seterr(err, errcap, "server name must be printable ASCII");
        return 0;
    }
    for (i = 0; i < n; i++) {
        if (strchr("[]=\"\\", name[i]) != NULL) {
            seterr(err, errcap, "server name must not contain [ ] = \" or backslash");
            return 0;
        }
    }
    if (name[0] == ' ' || name[n - 1] == ' ') {
        seterr(err, errcap, "server name must not start or end with a space");
        return 0;
    }
    return 1;
}

static int ipv4_literal_check(const char* addr) {
    int parts = 0;
    const char* p = addr;
    while (1) {
        int digits = 0;
        long v = 0;
        const char* start = p;
        while (*p >= '0' && *p <= '9' && digits <= 3) {
            v = v * 10 + (*p - '0');
            digits++;
            p++;
        }
        if (digits < 1 || digits > 3 || v > 255 || (digits > 1 && *start == '0')) return 0;
        parts++;
        if (*p == '.') {
            p++;
            continue;
        }
        break;
    }
    return *p == '\0' && parts == 4;
}

/* DNS hostname: 1..63 chars in total (the address buffers are 64 bytes), dot-separated labels of 1..63 [A-Za-z0-9-], a label never starts / ends with '-', no
 * trailing dot. A name made only of digits and dots is NOT a hostname (a malformed IPv4 literal such as 1.2.3 or 256.1.1.1 is refused as before). */
static int hostname_check(const char* addr, char* err, size_t errcap) {
    size_t n = strlen(addr), i, label = 0;
    int only_numeric = 1;
    if (n > PC_SERVER_ADDR_MAX - 1) {
        seterr(err, errcap, "address is too long (max 63 characters)");
        return 0;
    }
    for (i = 0; i < n; i++) {
        const unsigned char c = (unsigned char)addr[i];
        const int alnum = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
        if (!(c >= '0' && c <= '9') && c != '.') only_numeric = 0;
        if (c == '.') {
            if (label == 0 || addr[i - 1] == '-') {
                seterr(err, errcap, "address has an empty or malformed hostname label");
                return 0;
            }
            label = 0;
            continue;
        }
        if (!alnum && c != '-') {
            seterr(err, errcap, "address must be an IPv4 literal or a hostname (letters, digits, '-' and '.')");
            return 0;
        }
        if (c == '-' && label == 0) {
            seterr(err, errcap, "a hostname label must not start with '-'");
            return 0;
        }
        if (++label > 63) {
            seterr(err, errcap, "a hostname label is longer than 63 characters");
            return 0;
        }
    }
    if (label == 0 || addr[n - 1] == '-') {
        seterr(err, errcap, "address has an empty or malformed hostname label");
        return 0;
    }
    if (only_numeric) {
        seterr(err, errcap, "address must be an IPv4 literal a.b.c.d (0..255, no leading zeros) or a hostname");
        return 0;
    }
    return 1;
}

int pc_servers_address_check(const char* addr, char* err, size_t errcap) {
    if (addr == NULL || addr[0] == '\0') {
        seterr(err, errcap, "address is empty");
        return 0;
    }
    if (ipv4_literal_check(addr)) return 1;
    return hostname_check(addr, err, errcap);
}

int pc_servers_port_check(long port) {
    return port >= 1 && port <= 65535;
}

int pc_servers_parse_hostport(const char* text, char address[PC_SERVER_ADDR_MAX], int* port, char* err, size_t errcap) {
    char buf[64];
    char* colon;
    size_t n;
    if (text == NULL || (n = strlen(text)) < 1 || n >= sizeof(buf)) {
        seterr(err, errcap, "expected HOST[:PORT]");
        return 0;
    }
    memcpy(buf, text, n + 1);
    *port = PC_SERVER_DEFAULT_PORT;
    colon = strchr(buf, ':');
    if (colon != NULL) {
        char* end = NULL;
        long p;
        *colon = '\0';
        p = strtol(colon + 1, &end, 10);
        if (colon[1] == '\0' || end == NULL || *end != '\0' || !pc_servers_port_check(p)) {
            seterr(err, errcap, "port must be 1..65535");
            return 0;
        }
        *port = (int)p;
    }
    if (!pc_servers_address_check(buf, err, errcap)) {
        return 0;
    }
    memcpy(address, buf, strlen(buf) + 1);
    return 1;
}

/* ---------------- files ---------------- */

static const char* dir_or_default(const char* dir) {
    return dir != NULL ? dir : SERVERS_DIR_DEFAULT;
}

static void make_dir(const char* p) {
#ifdef _WIN32
    _mkdir(p);
#else
    mkdir(p, 0755);
#endif
}

static void ensure_parent_dirs(const char* path) {
    char buf[400];
    size_t i, n = strlen(path);
    if (n >= sizeof(buf)) return;
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

static int servers_path(const char* dir, char* out, size_t cap) {
    return snprintf(out, cap, "%s/servers.ini", dir_or_default(dir)) < (int)cap;
}

static int write_atomic(const char* path, const char* text, size_t n) {
    char tmp[420];
    FILE* fp;
    int ok;
    if (strlen(path) + 5 >= sizeof(tmp)) return 0;
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    ensure_parent_dirs(path);
    fp = fopen(tmp, "wb");
    if (fp == NULL) return 0;
    ok = fwrite(text, 1, n, fp) == n && fflush(fp) == 0;
#ifdef _WIN32
    ok = ok && _commit(_fileno(fp)) == 0;
#else
    ok = ok && fsync(fileno(fp)) == 0;
#endif
    ok = (fclose(fp) == 0) && ok;
    if (ok) {
#ifdef _WIN32
        ok = MoveFileExA(tmp, path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != 0;
#else
        ok = rename(tmp, path) == 0;
#endif
    }
    if (!ok) remove(tmp);
    return ok;
}

static char* trim(char* s) {
    char* e;
    while (*s == ' ' || *s == '\t') s++;
    e = s + strlen(s);
    while (e > s && (e[-1] == ' ' || e[-1] == '\t' || e[-1] == '\r')) *--e = '\0';
    return s;
}

static void copy_hint(char dst[PC_SERVER_NAME_MAX + 1], const char* src) {
    size_t i, n = 0;
    for (i = 0; src != NULL && src[i] != '\0' && n < PC_SERVER_NAME_MAX; i++) {
        const unsigned char c = (unsigned char)src[i];
        dst[n++] = (c < 0x20 || c > 0x7E) ? '?' : (char)c;
    }
    dst[n] = '\0';
    while (n > 0 && dst[n - 1] == ' ') dst[--n] = '\0';
}

/* 1 = entry complete (or no entry open) */
static int entry_ok(int in_section, unsigned have, int lineno, char* err, size_t errcap) {
    if (in_section && have != 3u) {
        snprintf(err, errcap, "line %d: the [server] section before it has no name or no address", lineno);
        return 0;
    }
    return 1;
}

static int parse_text(char* text, PCServer* out, int cap, int* count, char* err, size_t errcap) {
    char* line = text;
    int lineno = 0, n = 0, in_section = 0;
    unsigned have = 0;
    *count = 0;
    while (line != NULL && *line != '\0') {
        char* nl = strchr(line, '\n');
        char* s;
        lineno++;
        if (nl != NULL) *nl = '\0';
        s = trim(line);
        line = nl != NULL ? nl + 1 : NULL;
        if (*s == '\0' || *s == '#' || *s == ';') continue;
        if (!printable(s)) {
            snprintf(err, errcap, "line %d: non-printable character", lineno);
            return 0;
        }
        if (*s == '[') {
            if (!entry_ok(in_section, have, lineno, err, errcap)) return 0;
            if (strcmp(s, "[server]") != 0) {
                snprintf(err, errcap, "line %d: unknown section '%s' (only [server])", lineno, s);
                return 0;
            }
            if (n >= PC_SERVER_MAX || n >= cap) {
                snprintf(err, errcap, "line %d: more than %d servers", lineno, PC_SERVER_MAX);
                return 0;
            }
            memset(&out[n], 0, sizeof(PCServer));
            out[n].port = PC_SERVER_DEFAULT_PORT;
            n++;
            in_section = 1;
            have = 0;
        } else {
            char* eq = strchr(s, '=');
            char *k, *v;
            PCServer* e;
            char why[200];
            if (eq == NULL || !in_section) {
                snprintf(err, errcap, "line %d: expected '[server]' or 'key = value' inside a section", lineno);
                return 0;
            }
            *eq = '\0';
            k = trim(s);
            v = trim(eq + 1);
            e = &out[n - 1];
            if (strcmp(k, "name") == 0) {
                int i;
                if (!pc_servers_name_check(v, why, sizeof(why))) {
                    snprintf(err, errcap, "line %d: %s", lineno, why);
                    return 0;
                }
                for (i = 0; i < n - 1; i++) {
                    if (ci_equal(out[i].name, v)) {
                        snprintf(err, errcap, "line %d: duplicate server name '%s'", lineno, v);
                        return 0;
                    }
                }
                memcpy(e->name, v, strlen(v) + 1);
                have |= 1u;
            } else if (strcmp(k, "address") == 0) {
                if (!pc_servers_address_check(v, why, sizeof(why))) {
                    snprintf(err, errcap, "line %d: %s", lineno, why);
                    return 0;
                }
                memcpy(e->address, v, strlen(v) + 1);
                have |= 2u;
            } else if (strcmp(k, "port") == 0) {
                char* end = NULL;
                const long p = strtol(v, &end, 10);
                if (*v == '\0' || end == NULL || *end != '\0' || !pc_servers_port_check(p)) {
                    snprintf(err, errcap, "line %d: port must be 1..65535", lineno);
                    return 0;
                }
                e->port = (int)p;
            } else if (strcmp(k, "last_character") == 0) {
                copy_hint(e->last_character, v);
            } else if (strcmp(k, "last_town") == 0) {
                copy_hint(e->last_town, v);
            } else {
                snprintf(err, errcap, "line %d: unknown key '%s'", lineno, k);
                return 0;
            }
        }
    }
    if (!entry_ok(in_section, have, lineno, err, errcap)) return 0;
    *count = n;
    return 1;
}

int pc_servers_load(const char* dir, PCServer* out, int cap, int* count, char* err, size_t errcap) {
    static char buf[SERVERS_FILE_MAX + 2];
    char path[300];
    FILE* fp;
    size_t n;
    char perr[300];
    *count = 0;
    if (err != NULL && errcap > 0) err[0] = '\0';
    if (!servers_path(dir, path, sizeof(path))) {
        seterr(err, errcap, "path too long");
        return PC_SERVERS_ERR;
    }
    fp = fopen(path, "rb");
    if (fp == NULL) {
        return PC_SERVERS_OK; /* no file = no servers */
    }
    n = fread(buf, 1, SERVERS_FILE_MAX + 1, fp);
    fclose(fp);
    if (n > SERVERS_FILE_MAX) {
        snprintf(err, errcap, "%s: larger than 64 KiB (corrupt; left untouched)", path);
        return PC_SERVERS_CORRUPT;
    }
    buf[n] = '\0';
    if (strlen(buf) != n) {
        snprintf(err, errcap, "%s: contains a NUL byte (corrupt; left untouched)", path);
        return PC_SERVERS_CORRUPT;
    }
    perr[0] = '\0';
    if (!parse_text(buf, out, cap, count, perr, sizeof(perr))) {
        *count = 0;
        snprintf(err, errcap, "%s: %s (corrupt; the file is left untouched and no server operation will write it)", path, perr);
        return PC_SERVERS_CORRUPT;
    }
    return PC_SERVERS_OK;
}

int pc_servers_find(const PCServer* list, int count, const char* name) {
    int i;
    if (name == NULL) return -1;
    for (i = 0; i < count; i++) {
        if (ci_equal(list[i].name, name)) return i;
    }
    return -1;
}

static int save_list(const char* dir, const PCServer* list, int count, char* err, size_t errcap) {
    static char text[PC_SERVER_MAX * 256 + 256];
    char path[300];
    size_t off = 0;
    int i;
    if (!servers_path(dir, path, sizeof(path))) {
        seterr(err, errcap, "path too long");
        return PC_SERVERS_ERR;
    }
    off += (size_t)snprintf(text + off, sizeof(text) - off, "# Saved servers (Play Online / --server NAME). Edit with --server-add / --server-delete or by hand;\n"
                                                           "# an unparseable file is never rewritten. address = IPv4 literal (no hostname resolution).\n");
    for (i = 0; i < count; i++) {
        off += (size_t)snprintf(text + off, sizeof(text) - off, "\n[server]\nname = %s\naddress = %s\nport = %d\n", list[i].name, list[i].address, list[i].port);
        if (list[i].last_character[0] != '\0') {
            off += (size_t)snprintf(text + off, sizeof(text) - off, "last_character = %s\n", list[i].last_character);
        }
        if (list[i].last_town[0] != '\0') {
            off += (size_t)snprintf(text + off, sizeof(text) - off, "last_town = %s\n", list[i].last_town);
        }
    }
    if (!write_atomic(path, text, off)) {
        snprintf(err, errcap, "cannot write %s", path);
        return PC_SERVERS_ERR;
    }
    return PC_SERVERS_OK;
}

static int check_entry(const PCServer* s, char* err, size_t errcap) {
    if (!pc_servers_name_check(s->name, err, errcap) || !pc_servers_address_check(s->address, err, errcap)) return 0;
    if (!pc_servers_port_check(s->port)) {
        seterr(err, errcap, "port must be 1..65535");
        return 0;
    }
    return 1;
}

int pc_servers_add(const char* dir, const PCServer* s, char* err, size_t errcap) {
    static PCServer list[PC_SERVER_MAX];
    int n, r;
    PCServer e;
    if (!check_entry(s, err, errcap)) return PC_SERVERS_INVALID;
    r = pc_servers_load(dir, list, PC_SERVER_MAX, &n, err, errcap);
    if (r != PC_SERVERS_OK) return r;
    if (pc_servers_find(list, n, s->name) >= 0) {
        seterr(err, errcap, "a server with that name already exists");
        return PC_SERVERS_EXISTS;
    }
    if (n >= PC_SERVER_MAX) {
        seterr(err, errcap, "too many saved servers (max 32)");
        return PC_SERVERS_FULL;
    }
    e = *s;
    copy_hint(e.last_character, s->last_character);
    copy_hint(e.last_town, s->last_town);
    list[n++] = e;
    return save_list(dir, list, n, err, errcap);
}

int pc_servers_update(const char* dir, const char* name, const PCServer* s, char* err, size_t errcap) {
    static PCServer list[PC_SERVER_MAX];
    int n, r, i, j;
    if (!check_entry(s, err, errcap)) return PC_SERVERS_INVALID;
    r = pc_servers_load(dir, list, PC_SERVER_MAX, &n, err, errcap);
    if (r != PC_SERVERS_OK) return r;
    i = pc_servers_find(list, n, name);
    if (i < 0) {
        seterr(err, errcap, "no such server");
        return PC_SERVERS_NOTFOUND;
    }
    j = pc_servers_find(list, n, s->name);
    if (j >= 0 && j != i) {
        seterr(err, errcap, "a server with that name already exists");
        return PC_SERVERS_EXISTS;
    }
    memcpy(list[i].name, s->name, sizeof(list[i].name));
    memcpy(list[i].address, s->address, sizeof(list[i].address));
    list[i].port = s->port;
    return save_list(dir, list, n, err, errcap);
}

int pc_servers_delete(const char* dir, const char* name, char* err, size_t errcap) {
    static PCServer list[PC_SERVER_MAX];
    int n, r, i;
    r = pc_servers_load(dir, list, PC_SERVER_MAX, &n, err, errcap);
    if (r != PC_SERVERS_OK) return r;
    i = pc_servers_find(list, n, name);
    if (i < 0) {
        seterr(err, errcap, "no such server");
        return PC_SERVERS_NOTFOUND;
    }
    for (; i + 1 < n; i++) list[i] = list[i + 1];
    return save_list(dir, list, n - 1, err, errcap);
}

int pc_servers_set_last(const char* dir, const char* name, const char* character, const char* town) {
    static PCServer list[PC_SERVER_MAX];
    char err[300];
    int n, r, i;
    r = pc_servers_load(dir, list, PC_SERVER_MAX, &n, err, sizeof(err));
    if (r != PC_SERVERS_OK) return r;
    i = pc_servers_find(list, n, name);
    if (i < 0) return PC_SERVERS_NOTFOUND;
    if (character != NULL) copy_hint(list[i].last_character, character);
    if (town != NULL) copy_hint(list[i].last_town, town);
    return save_list(dir, list, n, err, sizeof(err));
}
