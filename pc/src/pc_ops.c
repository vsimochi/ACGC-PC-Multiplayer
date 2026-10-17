/* pc_ops.c - the host's operator list (pure logic, see pc_ops.h). No game state: the native selftest compiles this file alone. */
#ifdef _WIN32
#include <windows.h>
#endif
#include "pc_ops.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void pc_ops_init(PCOpsList* l) {
    memset(l, 0, sizeof(*l));
}

const char* pc_ops_kind_name(int kind) {
    return kind == PC_OPS_KIND_RESIDENT ? "resident" : kind == PC_OPS_KIND_GUEST ? "guest" : "?";
}

int pc_ops_find(const PCOpsList* l, int kind, const uint8_t key[PC_OPS_KEY_LEN]) {
    int i;
    for (i = 0; i < l->n; i++) {
        if ((int)l->e[i].kind == kind && memcmp(l->e[i].key, key, PC_OPS_KEY_LEN) == 0) {
            return i;
        }
    }
    return -1;
}

static void ops_copy_name(char* dst, const char* src) {
    size_t i, n = 0;
    if (src != NULL) {
        for (i = 0; src[i] != '\0' && n + 1 < PC_OPS_NAME_MAX; i++) {
            const unsigned char c = (unsigned char)src[i];
            dst[n++] = (c >= 0x20 && c <= 0x7E && c != '#') ? (char)c : '?';
        }
    }
    while (n > 0 && dst[n - 1] == ' ') {
        n--;
    }
    dst[n] = '\0';
}

int pc_ops_add(PCOpsList* l, int kind, const uint8_t key[PC_OPS_KEY_LEN], const char* name) {
    PCOpsEntry* e;
    if (kind != PC_OPS_KIND_RESIDENT && kind != PC_OPS_KIND_GUEST) {
        return -1;
    }
    if (pc_ops_find(l, kind, key) >= 0) {
        return 0;
    }
    if (l->n >= PC_OPS_MAX) {
        return -1;
    }
    e = &l->e[l->n++];
    memset(e, 0, sizeof(*e));
    e->kind = (uint8_t)kind;
    memcpy(e->key, key, PC_OPS_KEY_LEN);
    ops_copy_name(e->name, name);
    l->gen++;
    return 1;
}

int pc_ops_remove(PCOpsList* l, int kind, const uint8_t key[PC_OPS_KEY_LEN]) {
    const int i = pc_ops_find(l, kind, key);
    if (i < 0) {
        return 0;
    }
    memmove(&l->e[i], &l->e[i + 1], (size_t)(l->n - i - 1) * sizeof(l->e[0]));
    l->n--;
    memset(&l->e[l->n], 0, sizeof(l->e[0]));
    l->gen++;
    return 1;
}

static int ops_hexval(char c) {
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

/* one line (NUL-terminated, may be modified): 1 = an op entry was added, 0 = nothing to add (comment / version / blank), -1 = malformed */
static int ops_parse_line(PCOpsList* l, char* s) {
    char* p = s;
    char* eq;
    char* v;
    int kind, i;
    uint8_t key[PC_OPS_KEY_LEN];
    while (*p == ' ' || *p == '\t') {
        p++;
    }
    if (*p == '\0' || *p == '#' || *p == ';') {
        return 0;
    }
    eq = strchr(p, '=');
    if (eq == NULL) {
        return -1;
    }
    *eq = '\0';
    v = eq + 1;
    {
        char* e = eq;
        while (e > p && (e[-1] == ' ' || e[-1] == '\t')) {
            *--e = '\0';
        }
    }
    while (*v == ' ' || *v == '\t') {
        v++;
    }
    if (strcmp(p, "version") == 0) {
        return 0; /* any version: read what is understood */
    }
    if (strcmp(p, "op") != 0) {
        return -1; /* unknown key: skipped */
    }
    if (strncmp(v, "resident", 8) == 0 && (v[8] == ' ' || v[8] == '\t')) {
        kind = PC_OPS_KIND_RESIDENT;
        v += 8;
    } else if (strncmp(v, "guest", 5) == 0 && (v[5] == ' ' || v[5] == '\t')) {
        kind = PC_OPS_KIND_GUEST;
        v += 5;
    } else {
        return -1;
    }
    while (*v == ' ' || *v == '\t') {
        v++;
    }
    for (i = 0; i < PC_OPS_KEY_LEN; i++) {
        const int hi = ops_hexval(v[2 * i]);
        const int lo = hi < 0 ? -1 : ops_hexval(v[2 * i + 1]);
        if (hi < 0 || lo < 0) {
            return -1;
        }
        key[i] = (uint8_t)((hi << 4) | lo);
    }
    v += 2 * PC_OPS_KEY_LEN;
    if (*v != '\0' && *v != ' ' && *v != '\t' && *v != '#') {
        return -1; /* 41+ hex digits */
    }
    while (*v == ' ' || *v == '\t') {
        v++;
    }
    if (*v == '#') {
        v++;
        while (*v == ' ') {
            v++;
        }
    }
    return pc_ops_add(l, kind, key, v) == 1 ? 1 : (pc_ops_find(l, kind, key) >= 0 ? 0 : -1); /* a duplicate line is harmless */
}

int pc_ops_parse(PCOpsList* l, const char* text, size_t len, int* bad_lines) {
    char line[256];
    size_t i = 0;
    int bad = 0;
    pc_ops_init(l);
    while (i < len) {
        size_t n = 0;
        int too_long = 0;
        while (i < len && text[i] != '\n') {
            if (n + 1 < sizeof(line)) {
                line[n++] = text[i];
            } else {
                too_long = 1;
            }
            i++;
        }
        if (i < len) {
            i++; /* the '\n' */
        }
        while (n > 0 && (line[n - 1] == '\r' || line[n - 1] == ' ' || line[n - 1] == '\t')) {
            n--;
        }
        line[n] = '\0';
        if (too_long || ops_parse_line(l, line) < 0) {
            bad++;
        }
    }
    l->gen = 1;
    if (bad_lines != NULL) {
        *bad_lines = bad;
    }
    return l->n;
}

int pc_ops_format(const PCOpsList* l, char* out, size_t cap) {
    size_t used = 0;
    int i, k, w;
    w = snprintf(out, cap,
                 "# ops.ini - the operators of this server (like Minecraft ops). Written by the server; edit with the console commands op / deop / ops.\n"
                 "# One line per operator: op = resident|guest <identity key: 40 hex digits> <name (informational only)>.\n"
                 "# The key is the identity the host binds a connection to (PersonalID: name + town + player id + land id), never the display name alone.\n"
                 "version = %d\n",
                 PC_OPS_VERSION);
    if (w < 0 || (size_t)w >= cap) {
        return -1;
    }
    used = (size_t)w;
    for (i = 0; i < l->n; i++) {
        char hex[2 * PC_OPS_KEY_LEN + 1];
        for (k = 0; k < PC_OPS_KEY_LEN; k++) {
            static const char d[] = "0123456789abcdef";
            hex[2 * k] = d[l->e[i].key[k] >> 4];
            hex[2 * k + 1] = d[l->e[i].key[k] & 15];
        }
        hex[2 * PC_OPS_KEY_LEN] = '\0';
        w = snprintf(out + used, cap - used, "op = %s %s %s\n", pc_ops_kind_name(l->e[i].kind), hex, l->e[i].name);
        if (w < 0 || (size_t)w >= cap - used) {
            return -1;
        }
        used += (size_t)w;
    }
    return (int)used;
}

int pc_ops_load(const char* path, PCOpsList* l, int* bad_lines) {
    FILE* f;
    char* buf;
    long sz;
    size_t got;
    pc_ops_init(l);
    if (bad_lines != NULL) {
        *bad_lines = 0;
    }
    if (path == NULL || (f = fopen(path, "rb")) == NULL) {
        return PC_OPS_MISSING;
    }
    if (fseek(f, 0, SEEK_END) != 0 || (sz = ftell(f)) < 0 || sz > 256 * 1024 || fseek(f, 0, SEEK_SET) != 0) {
        fclose(f);
        return PC_OPS_BAD;
    }
    buf = (char*)malloc((size_t)sz + 1);
    if (buf == NULL) {
        fclose(f);
        return PC_OPS_BAD;
    }
    got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    if (got != (size_t)sz) {
        free(buf);
        return PC_OPS_BAD;
    }
    (void)pc_ops_parse(l, buf, got, bad_lines);
    free(buf);
    return PC_OPS_OK;
}

int pc_ops_save(const char* path, const PCOpsList* l) {
    char tmp[600];
    char* buf;
    int n, ok;
    FILE* f;
    const size_t cap = 512 + (size_t)PC_OPS_MAX * 128;
    if (path == NULL || path[0] == '\0' || strlen(path) + 5 > sizeof(tmp)) {
        return 0;
    }
    buf = (char*)malloc(cap);
    if (buf == NULL) {
        return 0;
    }
    n = pc_ops_format(l, buf, cap);
    if (n < 0) {
        free(buf);
        return 0;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    f = fopen(tmp, "wb");
    if (f == NULL) {
        free(buf);
        return 0;
    }
    ok = fwrite(buf, 1, (size_t)n, f) == (size_t)n && fflush(f) == 0;
    ok = (fclose(f) == 0) && ok;
    free(buf);
    if (!ok) {
        remove(tmp);
        return 0;
    }
#ifdef _WIN32
    if (!MoveFileExA(tmp, path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        remove(tmp);
        return 0;
    }
#else
    if (rename(tmp, path) != 0) {
        remove(tmp);
        return 0;
    }
#endif
    return 1;
}

/* ---- native selftest ---- */
static int s_ops_fail;
static void ops_ck(int ok, const char* what) {
    if (!ok) {
        s_ops_fail++;
        printf("[OPS] selftest FAIL: %s\n", what);
    }
}

int pc_ops_selftest(void) {
    PCOpsList a, b;
    uint8_t k1[PC_OPS_KEY_LEN], k2[PC_OPS_KEY_LEN];
    char txt[4096];
    int n, bad, i;
    s_ops_fail = 0;
    for (i = 0; i < PC_OPS_KEY_LEN; i++) {
        k1[i] = (uint8_t)(i * 7 + 1);
        k2[i] = (uint8_t)(0xF0 - i);
    }
    pc_ops_init(&a);
    ops_ck(pc_ops_add(&a, PC_OPS_KIND_RESIDENT, k1, "Alice") == 1, "add resident");
    ops_ck(pc_ops_add(&a, PC_OPS_KIND_RESIDENT, k1, "Alice2") == 0, "add twice = already an op");
    ops_ck(pc_ops_add(&a, PC_OPS_KIND_GUEST, k1, "SameKeyGuest") == 1, "the same key as a GUEST is another identity");
    ops_ck(pc_ops_add(&a, PC_OPS_KIND_GUEST, k2, "Bob # evil\nx") == 1, "add guest (name sanitised)");
    ops_ck(pc_ops_add(&a, 7, k2, "bad") == -1, "unknown kind refused");
    ops_ck(pc_ops_find(&a, PC_OPS_KIND_GUEST, k2) == 2 && strchr(a.e[2].name, '#') == NULL && strchr(a.e[2].name, '\n') == NULL, "find + no '#' / newline in a name");
    n = pc_ops_format(&a, txt, sizeof(txt));
    ops_ck(n > 0, "format");
    pc_ops_parse(&b, txt, (size_t)n, &bad);
    ops_ck(b.n == 3 && bad == 0, "round trip: 3 entries, no bad line");
    ops_ck(pc_ops_find(&b, PC_OPS_KIND_RESIDENT, k1) == 0 && strcmp(b.e[0].name, "Alice") == 0, "round trip keeps kind / key / name");
    ops_ck(pc_ops_find(&b, PC_OPS_KIND_GUEST, k2) >= 0, "round trip guest");
    {
        const char* t = "\xEF\xBB\xBFgarbage line\r\n# comment\r\nversion = 9\r\nop = resident 0102\r\nop = wizard 0102030405060708090a0b0c0d0e0f1011121314 X\r\n"
                        "unknown = 3\r\nop = guest 0102030405060708090A0B0C0D0E0F1011121314   Carol  \r\nop = guest 0102030405060708090a0b0c0d0e0f1011121314 dup\r\n"
                        "op=resident 0102030405060708090a0b0c0d0e0f10111213141 toolong";
        pc_ops_parse(&b, t, strlen(t), &bad);
        ops_ck(b.n == 1 && b.e[0].kind == PC_OPS_KIND_GUEST && b.e[0].key[0] == 1 && b.e[0].key[19] == 0x14 && strcmp(b.e[0].name, "Carol") == 0,
               "tolerant parser: one valid guest (upper-case hex), a duplicate line kept once");
        ops_ck(bad == 5, "tolerant parser: 5 bad lines counted (BOM garbage, short key, unknown kind, unknown key, 41 hex digits)");
    }
    ops_ck(pc_ops_remove(&a, PC_OPS_KIND_GUEST, k1) == 1 && a.n == 2 && pc_ops_find(&a, PC_OPS_KIND_GUEST, k2) == 1, "remove shifts the rest");
    ops_ck(pc_ops_remove(&a, PC_OPS_KIND_GUEST, k1) == 0, "remove twice = not an op");
    pc_ops_init(&a);
    for (i = 0; i < PC_OPS_MAX + 2; i++) {
        k1[0] = (uint8_t)i;
        k1[1] = (uint8_t)(i >> 8);
        (void)pc_ops_add(&a, PC_OPS_KIND_RESIDENT, k1, "n");
    }
    ops_ck(a.n == PC_OPS_MAX, "the list is bounded");
    ops_ck(pc_ops_format(&a, txt, 64) == -1, "format refuses a too small buffer");
    printf("[OPS] selftest: %s (%d failure(s))\n", s_ops_fail ? "FAIL" : "PASS", s_ops_fail);
    return s_ops_fail;
}
