/* pc_chat.c - multiplayer text chat: the PURE logic (see pc_chat.h). No game / SDL state: the native selftest compiles this file (+ pc_ops.c) alone.
 * Charset: the game's message font codes (m_font.h CHAR_*), mirrored here as numbers so this file needs no game header; pc_chat_ui.c cross-checks the
 * letters against the game's own m_font.h constants at startup. */
#ifdef _WIN32
#include <windows.h>
#endif
#define PC_CHAT_PURE_ONLY
#include "pc_chat.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---------------------------------------------------------------- charset ---------------------------------------------------------------- */
/* ASCII 0x20..0x7E -> font code (-1 = the font has no such glyph: '?'). The font is NOT ASCII outside letters / digits / a few marks: '#' is CHAR_HASHTAG 209,
 * '/' CHAR_FORWARD_SLASH 174, ';' CHAR_SEMICOLON 208, '+' CHAR_PLUS 180, '\\' CHAR_BACKSLASH 222 (0x23 / 0x2F / 0x3B / 0x2B / 0x5C are other glyphs there). */
static const int16_t s_ascii[95] = {
    32,  33,  34,  209, -1,  37,  38,  39,  40,  41,  185, 180, 44,  45,  46,  174, /* ' ' ! " # $ % & ' ( ) * + , - . / ('*' = the star symbol) */
    48,  49,  50,  51,  52,  53,  54,  55,  56,  57,  58,  208, 60,  61,  62,  63,  /* 0-9 : ; < = > ? */
    64,  65,  66,  67,  68,  69,  70,  71,  72,  73,  74,  75,  76,  77,  78,  79,  /* @ A-O */
    80,  81,  82,  83,  84,  85,  86,  87,  88,  89,  90,  -1,  222, -1,  -1,  95,  /* P-Z [ \ ] ^ _ */
    215, 97,  98,  99,  100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, /* ` a-o ('`' = the left apostrophe) */
    112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, -1,  151, -1,  42,       /* p-z { | } ~ ('|' = the broken bar) */
};
/* U+00A0..U+00FF (-1 = '?', -2 = dropped) */
static const int16_t s_latin1[96] = {
    32,  33,  -1,  -1,  -1,  -1,  151, 152, -1,  -1,  153, 165, 161, -2,  -1,  160, /* nbsp, U+00A1 inverted '!' -> '!' (code 0 ends a string), ... */
    -1,  -1,  158, 157, -1,  156, -1,  133, -1,  159, 154, 166, -1,  -1,  -1,  1,   /* ... middle dot 133, inverted '?' 1 */
    3,   4,   5,   6,   2,   7,   162, 8,   9,   10,  11,  12,  13,  14,  15,  16,  /* U+00C0.. */
    17,  18,  19,  20,  21,  22,  23,  192, 24,  25,  26,  27,  28,  150, 30,  29,  /* U+00D0.. (x = the dimension sign 192) */
    31,  35,  36,  91,  93,  94,  163, 96,  123, 124, 125, 126, 129, 130, 131, 132, /* U+00E0.. */
    134, 135, 136, 137, 138, 139, 140, 193, 141, 142, 143, 145, 146, 147, 149, 148, /* U+00F0.. (division 193) */
};
typedef struct { uint32_t cp; int16_t code; } PCChatExtra;
static const PCChatExtra s_extra[] = {
    { 0x0152, 217 }, { 0x0153, 218 }, { 0x2018, 215 }, { 0x2019, 216 }, { 0x201C, 213 }, { 0x201D, 214 }, { 0x2665, 43 }, { 0x2764, 43 },
    { 0x266A, 47 },  { 0x266B, 47 },  { 0x2605, 185 }, { 0x2606, 185 }, { 0x221E, 175 }, { 0x2013, 45 },  { 0x2014, 45 },  { 0x2022, 133 },
};
#define PC_CHAT_EXTRA_N ((int)(sizeof(s_extra) / sizeof(s_extra[0])))

static uint8_t  s_ok[256];
static uint32_t s_rev[256]; /* font code -> a code point (logs), 0 = none */
static int      s_tables_ready = 0;

static void chat_tables(void) {
    int i;
    if (s_tables_ready) {
        return;
    }
    memset(s_ok, 0, sizeof(s_ok));
    memset(s_rev, 0, sizeof(s_rev));
    for (i = 0; i < 95; i++) {
        if (s_ascii[i] > 0) {
            s_ok[s_ascii[i]] = 1;
            if (s_rev[s_ascii[i]] == 0) s_rev[s_ascii[i]] = (uint32_t)(0x20 + i);
        }
    }
    for (i = 0; i < 96; i++) {
        if (s_latin1[i] > 0) {
            s_ok[s_latin1[i]] = 1;
            if (s_rev[s_latin1[i]] == 0) s_rev[s_latin1[i]] = (uint32_t)(0xA0 + i);
        }
    }
    for (i = 0; i < PC_CHAT_EXTRA_N; i++) {
        s_ok[s_extra[i].code] = 1;
        if (s_rev[s_extra[i].code] == 0) s_rev[s_extra[i].code] = s_extra[i].cp;
    }
    s_ok[0] = 0;    /* would end a C string */
    s_ok[0x7F] = 0; /* CHAR_CONTROL_CODE */
    s_ok[0x80] = 0; /* CHAR_MESSAGE_TAG */
    s_tables_ready = 1;
}

int pc_chat_code_ok(uint8_t code) {
    chat_tables();
    return s_ok[code];
}

int pc_chat_cp_to_code(uint32_t cp) {
    int i;
    if (cp < 0x20 || (cp >= 0x7F && cp < 0xA0) || cp == 0xAD || (cp >= 0x200B && cp <= 0x200F) || cp == 0xFEFF || (cp >= 0xFE00 && cp <= 0xFE0F) ||
        (cp >= 0x2028 && cp <= 0x202E) || (cp >= 0x2060 && cp <= 0x206F)) {
        return -2; /* controls, zero-width / direction marks, variation selectors: dropped */
    }
    if (cp < 0x7F) {
        return s_ascii[cp - 0x20];
    }
    if (cp >= 0xA0 && cp <= 0xFF) {
        return s_latin1[cp - 0xA0];
    }
    for (i = 0; i < PC_CHAT_EXTRA_N; i++) {
        if (s_extra[i].cp == cp) {
            return s_extra[i].code;
        }
    }
    return -1;
}

int pc_chat_utf8_next(const char* s, uint32_t* cp) {
    const unsigned char* p = (const unsigned char*)s;
    uint32_t c;
    int n, i;
    if (p == NULL || p[0] == 0) {
        return 0;
    }
    if (p[0] < 0x80) {
        *cp = p[0];
        return 1;
    }
    if ((p[0] & 0xE0) == 0xC0) {
        n = 2;
        c = p[0] & 0x1F;
    } else if ((p[0] & 0xF0) == 0xE0) {
        n = 3;
        c = p[0] & 0x0F;
    } else if ((p[0] & 0xF8) == 0xF0) {
        n = 4;
        c = p[0] & 0x07;
    } else {
        *cp = 0xFFFD;
        return 1;
    }
    for (i = 1; i < n; i++) {
        if ((p[i] & 0xC0) != 0x80) {
            *cp = 0xFFFD;
            return 1; /* truncated / invalid: skip only the lead byte (a NUL is never stepped over) */
        }
        c = (c << 6) | (p[i] & 0x3F);
    }
    if ((n == 2 && c < 0x80) || (n == 3 && c < 0x800) || (n == 4 && (c < 0x10000 || c > 0x10FFFF)) || (c >= 0xD800 && c <= 0xDFFF)) {
        c = 0xFFFD; /* overlong / surrogate / out of range */
    }
    *cp = c;
    return n;
}

int pc_chat_from_utf8(const char* utf8, uint8_t* out, int cap) {
    int n = 0, adv;
    uint32_t cp;
    if (utf8 == NULL || out == NULL) {
        return 0;
    }
    while (n < cap && (adv = pc_chat_utf8_next(utf8, &cp)) > 0) {
        const int code = pc_chat_cp_to_code(cp);
        utf8 += adv;
        if (code == -2) {
            continue;
        }
        out[n++] = (uint8_t)(code < 0 ? 63 : code);
    }
    return n;
}

int pc_chat_sanitize(const uint8_t* in, int len, uint8_t* out) {
    int i, n = 0;
    chat_tables();
    if (in == NULL || out == NULL || len <= 0) {
        return 0;
    }
    if (len > PC_CHAT_MAX_LEN) {
        len = PC_CHAT_MAX_LEN;
    }
    for (i = 0; i < len; i++) {
        uint8_t c = s_ok[in[i]] ? in[i] : (uint8_t)63;
        if (c == 32 && (n == 0 || out[n - 1] == 32)) {
            continue; /* leading / doubled blanks */
        }
        out[n++] = c;
    }
    while (n > 0 && out[n - 1] == 32) {
        n--;
    }
    return n;
}

static int chat_put_utf8(char* out, int cap, int n, uint32_t cp) {
    unsigned char b[4];
    int k, i;
    if (cp < 0x80) {
        b[0] = (unsigned char)cp;
        k = 1;
    } else if (cp < 0x800) {
        b[0] = (unsigned char)(0xC0 | (cp >> 6));
        b[1] = (unsigned char)(0x80 | (cp & 0x3F));
        k = 2;
    } else {
        b[0] = (unsigned char)(0xE0 | (cp >> 12));
        b[1] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
        b[2] = (unsigned char)(0x80 | (cp & 0x3F));
        k = 3;
    }
    if (n + k >= cap) {
        return n;
    }
    for (i = 0; i < k; i++) {
        out[n + i] = (char)b[i];
    }
    return n + k;
}

int pc_chat_to_utf8(const uint8_t* codes, int len, char* out, int cap) {
    int i, n = 0;
    chat_tables();
    if (out == NULL || cap <= 0) {
        return 0;
    }
    for (i = 0; codes != NULL && i < len; i++) {
        const uint32_t cp = s_rev[codes[i]];
        n = chat_put_utf8(out, cap, n, cp != 0 ? cp : (uint32_t)'?');
    }
    out[n] = '\0';
    return n;
}

int pc_chat_name_to_utf8(const uint8_t name[PC_CHAT_NAME_LEN], char* out, int cap) {
    int len = PC_CHAT_NAME_LEN;
    uint8_t tmp[PC_CHAT_NAME_LEN];
    int i, n = 0;
    while (len > 0 && (name[len - 1] == 32 || name[len - 1] == 0)) {
        len--;
    }
    for (i = 0; i < len; i++) {
        tmp[n++] = name[i] == 0 ? (uint8_t)63 : name[i];
    }
    return pc_chat_to_utf8(tmp, n, out, cap);
}

int pc_chat_input_append(uint8_t* buf, int len, int max, const char* utf8) {
    uint8_t tmp[PC_CHAT_MAX_LEN * 2];
    int n, i;
    if (buf == NULL || len < 0 || len >= max) {
        return len < 0 ? 0 : len;
    }
    n = pc_chat_from_utf8(utf8, tmp, (int)sizeof(tmp));
    for (i = 0; i < n && len < max; i++) {
        buf[len++] = tmp[i];
    }
    return len;
}

int pc_chat_input_backspace(int len) {
    return len > 0 ? len - 1 : 0;
}

/* ---------------------------------------------------------------- layout / timing ---------------------------------------------------------------- */
int pc_chat_wrap(const uint8_t* text, int len, int width, int max_lines, int* starts, int* lens, int* truncated) {
    int pos = 0, lines = 0;
    if (truncated != NULL) {
        *truncated = 0;
    }
    if (text == NULL || width <= 0 || max_lines <= 0) {
        return 0;
    }
    while (lines < max_lines) {
        int take, k, cut = -1;
        while (pos < len && text[pos] == 32) {
            pos++;
        }
        if (pos >= len) {
            break;
        }
        if (len - pos <= width) {
            take = len - pos;
        } else {
            for (k = pos + width; k > pos; k--) {
                if (text[k] == 32) {
                    cut = k;
                    break;
                }
            }
            take = cut > pos ? cut - pos : width;
        }
        starts[lines] = pos;
        lens[lines] = take;
        while (lens[lines] > 0 && text[pos + lens[lines] - 1] == 32) {
            lens[lines]--;
        }
        pos += take;
        lines++;
    }
    while (pos < len && text[pos] == 32) {
        pos++;
    }
    if (pos < len && lines > 0) {
        if (truncated != NULL) {
            *truncated = 1;
        }
        if (lens[lines - 1] > width - 2) {
            lens[lines - 1] = width - 2; /* room for ".." */
        }
    }
    return lines;
}

uint32_t pc_chat_bubble_life_ms(int len) {
    uint32_t ms = 6000u + 100u * (uint32_t)(len < 0 ? 0 : len);
    return ms > 12000u ? 12000u : ms;
}

int pc_chat_fade_alpha(uint32_t age_ms, uint32_t life_ms, uint32_t fade_ms) {
    if (age_ms >= life_ms) {
        return 0;
    }
    if (fade_ms == 0 || age_ms + fade_ms <= life_ms) {
        return 255;
    }
    return (int)(255u * (life_ms - age_ms) / fade_ms);
}

void pc_chat_rate_reset(PCChatRate* r) {
    memset(r, 0, sizeof(*r));
}

int pc_chat_rate_take(PCChatRate* r, uint32_t now_ms) {
    const int32_t cap = (int32_t)(PC_CHAT_RATE_BURST * PC_CHAT_RATE_REFILL_MS);
    uint32_t dt;
    if (!r->init) {
        r->init = 1;
        r->credit_ms = cap;
        r->last_ms = now_ms;
    }
    dt = now_ms - r->last_ms;
    r->last_ms = now_ms;
    if (dt > (uint32_t)cap) {
        dt = (uint32_t)cap;
    }
    r->credit_ms += (int32_t)dt;
    if (r->credit_ms > cap) {
        r->credit_ms = cap;
    }
    if (r->credit_ms >= (int32_t)PC_CHAT_RATE_REFILL_MS) {
        r->credit_ms -= (int32_t)PC_CHAT_RATE_REFILL_MS;
        return 1;
    }
    return 0;
}

void pc_chat_speaker_color(const uint8_t name[PC_CHAT_NAME_LEN], int* r, int* g, int* b) {
    static const uint8_t pal[8][3] = {
        { 120, 200, 255 }, { 255, 160, 120 }, { 150, 230, 130 }, { 240, 150, 220 }, { 255, 220, 110 }, { 140, 230, 220 }, { 200, 170, 255 }, { 255, 130, 150 },
    };
    uint32_t h = 2166136261u;
    int i;
    for (i = 0; i < PC_CHAT_NAME_LEN; i++) {
        h = (h ^ name[i]) * 16777619u;
    }
    h ^= h >> 13;
    *r = pal[h & 7][0];
    *g = pal[h & 7][1];
    *b = pal[h & 7][2];
}

/* ---------------------------------------------------------------- wire ---------------------------------------------------------------- */
int pc_chat_send_valid(const PCChatSendMsg* m) {
    int i;
    chat_tables();
    if (m->msg_type != PC_CHAT_MSG_SEND || m->len == 0 || m->len > PC_CHAT_MAX_LEN) {
        return 0;
    }
    for (i = 0; i < PC_CHAT_WIRE_TEXT; i++) {
        if (i < m->len ? !s_ok[m->text[i]] : m->text[i] != 0) {
            return 0;
        }
    }
    return 1;
}

int pc_chat_msg_valid(const PCChatMsg* m) {
    int i;
    chat_tables();
    if (m->msg_type != PC_CHAT_MSG_MSG || (m->flags & ~PC_CHAT_F_KNOWN) != 0 || m->len > PC_CHAT_MAX_LEN || m->rsv != 0) {
        return 0;
    }
    if ((m->flags & PC_CHAT_F_CAPS) != 0 ? m->len != 0 : m->len == 0) {
        return 0; /* CAPS carries no text; every other kind does */
    }
    if ((m->flags & PC_CHAT_F_ON) != 0 && (m->flags & PC_CHAT_F_CAPS) == 0) {
        return 0;
    }
    for (i = 0; i < PC_CHAT_WIRE_TEXT; i++) {
        if (i < m->len ? !s_ok[m->text[i]] : m->text[i] != 0) {
            return 0;
        }
    }
    return 1;
}

/* ---------------------------------------------------------------- mute list ---------------------------------------------------------------- */
static int chat_hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* 1 = an entry added, 0 = nothing (comment / version / blank / duplicate), -1 = malformed */
static int chat_mute_line(PCOpsList* l, char* p) {
    char* eq;
    char* v;
    int kind, i;
    uint8_t key[PC_OPS_KEY_LEN];
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '\0' || *p == '#' || *p == ';') return 0;
    if ((eq = strchr(p, '=')) == NULL) return -1;
    *eq = '\0';
    v = eq + 1;
    while (eq > p && (eq[-1] == ' ' || eq[-1] == '\t')) *--eq = '\0';
    while (*v == ' ' || *v == '\t') v++;
    if (strcmp(p, "version") == 0) return 0;
    if (strcmp(p, "mute") != 0) return -1;
    if (strncmp(v, "resident", 8) == 0 && (v[8] == ' ' || v[8] == '\t')) {
        kind = PC_OPS_KIND_RESIDENT;
        v += 8;
    } else if (strncmp(v, "guest", 5) == 0 && (v[5] == ' ' || v[5] == '\t')) {
        kind = PC_OPS_KIND_GUEST;
        v += 5;
    } else {
        return -1;
    }
    while (*v == ' ' || *v == '\t') v++;
    for (i = 0; i < PC_OPS_KEY_LEN; i++) {
        const int hi = chat_hexval(v[2 * i]);
        const int lo = hi < 0 ? -1 : chat_hexval(v[2 * i + 1]);
        if (hi < 0 || lo < 0) return -1;
        key[i] = (uint8_t)((hi << 4) | lo);
    }
    v += 2 * PC_OPS_KEY_LEN;
    if (*v != '\0' && *v != ' ' && *v != '\t') return -1;
    while (*v == ' ' || *v == '\t') v++;
    return pc_ops_add(l, kind, key, v) == 1 ? 1 : (pc_ops_find(l, kind, key) >= 0 ? 0 : -1);
}

int pc_chat_mutes_parse(PCOpsList* l, const char* text, size_t len, int* bad_lines) {
    char line[256];
    size_t i = 0;
    int bad = 0;
    pc_ops_init(l);
    while (i < len) {
        size_t n = 0;
        int too_long = 0;
        while (i < len && text[i] != '\n') {
            if (n + 1 < sizeof(line)) line[n++] = text[i];
            else too_long = 1;
            i++;
        }
        if (i < len) i++;
        while (n > 0 && (line[n - 1] == '\r' || line[n - 1] == ' ' || line[n - 1] == '\t')) n--;
        line[n] = '\0';
        if (too_long || chat_mute_line(l, line) < 0) bad++;
    }
    l->gen = 1;
    if (bad_lines != NULL) *bad_lines = bad;
    return l->n;
}

int pc_chat_mutes_format(const PCOpsList* l, char* out, size_t cap) {
    size_t used;
    int i, k, w;
    w = snprintf(out, cap,
                 "# mutes.ini - players muted in the chat of this server. Written by the server; edit with the console commands mute / unmute / mutes.\n"
                 "# One line per player: mute = resident|guest <identity key: 40 hex digits> <name (informational only)>.\n"
                 "version = 1\n");
    if (w < 0 || (size_t)w >= cap) return -1;
    used = (size_t)w;
    for (i = 0; i < l->n; i++) {
        char hex[2 * PC_OPS_KEY_LEN + 1];
        static const char d[] = "0123456789abcdef";
        for (k = 0; k < PC_OPS_KEY_LEN; k++) {
            hex[2 * k] = d[l->e[i].key[k] >> 4];
            hex[2 * k + 1] = d[l->e[i].key[k] & 15];
        }
        hex[2 * PC_OPS_KEY_LEN] = '\0';
        w = snprintf(out + used, cap - used, "mute = %s %s %s\n", pc_ops_kind_name(l->e[i].kind), hex, l->e[i].name);
        if (w < 0 || (size_t)w >= cap - used) return -1;
        used += (size_t)w;
    }
    return (int)used;
}

int pc_chat_mutes_load(const char* path, PCOpsList* l, int* bad_lines) {
    FILE* f;
    char* buf;
    long sz;
    size_t got;
    pc_ops_init(l);
    if (bad_lines != NULL) *bad_lines = 0;
    if (path == NULL || (f = fopen(path, "rb")) == NULL) return PC_OPS_MISSING;
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
    (void)pc_chat_mutes_parse(l, buf, got, bad_lines);
    free(buf);
    return PC_OPS_OK;
}

int pc_chat_mutes_save(const char* path, const PCOpsList* l) {
    char tmp[600];
    char* buf;
    int n, ok;
    FILE* f;
    const size_t cap = 512 + (size_t)PC_OPS_MAX * 128;
    if (path == NULL || path[0] == '\0' || strlen(path) + 5 > sizeof(tmp)) return 0;
    if ((buf = (char*)malloc(cap)) == NULL) return 0;
    n = pc_chat_mutes_format(l, buf, cap);
    if (n < 0) {
        free(buf);
        return 0;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    if ((f = fopen(tmp, "wb")) == NULL) {
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

/* ---------------------------------------------------------------- presentation store ---------------------------------------------------------------- */
void pc_chat_store_init(PCChatStore* s) {
    memset(s, 0, sizeof(*s));
}

void pc_chat_store_clear(PCChatStore* s) {
    pc_chat_store_init(s);
}

static void chat_entry_fill(PCChatEntry* e, int speaker, int self, uint8_t flags, const uint8_t* name, const uint8_t* text, int len, uint32_t now, uint32_t life,
                            uint32_t serial) {
    memset(e, 0, sizeof(*e));
    e->used = 1;
    e->flags = flags;
    e->self = (uint8_t)(self != 0);
    e->speaker = speaker;
    e->len = (uint8_t)len;
    e->t0 = now;
    e->life = life;
    e->serial = serial;
    if (name != NULL) memcpy(e->name, name, PC_CHAT_NAME_LEN);
    memcpy(e->text, text, (size_t)len);
    e->text[len] = 0;
}

int pc_chat_store_add(PCChatStore* s, int speaker, int self, uint8_t flags, const uint8_t name[PC_CHAT_NAME_LEN], const uint8_t* text, int len, uint32_t now) {
    int i, free_i = -1, n_mine = 0, oldest_mine = -1, oldest = -1;
    if (text == NULL || len <= 0) return -1;
    if (len > PC_CHAT_MAX_LEN) len = PC_CHAT_MAX_LEN;
    s->serial++;
    chat_entry_fill(&s->log[s->log_head], speaker, self, flags, name, text, len, now, PC_CHAT_LOG_SHOW_MS, s->serial);
    s->log_head = (s->log_head + 1) % PC_CHAT_LOG_LINES;
    if ((flags & (PC_CHAT_F_SERVER | PC_CHAT_F_NOTICE)) != 0 || speaker < 0) return -1;
    for (i = 0; i < PC_CHAT_BUBBLES; i++) {
        PCChatEntry* e = &s->bubble[i];
        if (!e->used) {
            if (free_i < 0) free_i = i;
            continue;
        }
        if (e->speaker == speaker) {
            n_mine++;
            if (oldest_mine < 0 || e->serial < s->bubble[oldest_mine].serial) oldest_mine = i;
        }
        if (oldest < 0 || e->serial < s->bubble[oldest].serial) oldest = i;
    }
    if (n_mine >= PC_CHAT_BUBBLES_PER) {
        free_i = oldest_mine; /* the speaker's oldest bubble makes room */
    } else if (free_i < 0) {
        free_i = oldest; /* the table is full: the globally oldest goes */
    }
    chat_entry_fill(&s->bubble[free_i], speaker, self, flags, name, text, len, now, pc_chat_bubble_life_ms(len), s->serial);
    return free_i;
}

void pc_chat_store_expire(PCChatStore* s, uint32_t now) {
    int i;
    for (i = 0; i < PC_CHAT_BUBBLES; i++) {
        if (s->bubble[i].used && (uint32_t)(now - s->bubble[i].t0) >= s->bubble[i].life) s->bubble[i].used = 0;
    }
}

const PCChatEntry* pc_chat_store_bubble_of(const PCChatStore* s, int speaker, int k) {
    const PCChatEntry* pick[PC_CHAT_BUBBLES];
    int n = 0, i, j;
    for (i = 0; i < PC_CHAT_BUBBLES; i++) {
        if (s->bubble[i].used && s->bubble[i].speaker == speaker) {
            const PCChatEntry* e = &s->bubble[i];
            for (j = n; j > 0 && pick[j - 1]->serial < e->serial; j--) pick[j] = pick[j - 1];
            pick[j] = e;
            n++;
        }
    }
    return k >= 0 && k < n ? pick[k] : NULL;
}

const PCChatEntry* pc_chat_store_log(const PCChatStore* s, int i) {
    int idx;
    if (i < 0 || i >= PC_CHAT_LOG_LINES) return NULL;
    idx = ((s->log_head - 1 - i) % PC_CHAT_LOG_LINES + PC_CHAT_LOG_LINES) % PC_CHAT_LOG_LINES;
    return s->log[idx].used ? &s->log[idx] : NULL;
}

/* ---------------------------------------------------------------- native selftest ---------------------------------------------------------------- */
static int s_chat_fail;
static void chat_ck(int ok, const char* what) {
    if (!ok) {
        s_chat_fail++;
        printf("[CHAT] selftest FAIL: %s\n", what);
    }
}

static int chat_eq(const uint8_t* a, int an, const char* ascii_codes) {
    return an == (int)strlen(ascii_codes) && memcmp(a, ascii_codes, (size_t)an) == 0;
}

int pc_chat_selftest(void) {
    uint8_t c[128], d[128];
    char u[256];
    int n, i, st[8], ln[8], tr, bad;
    PCChatRate r;
    PCChatStore s;
    PCOpsList ml, m2;
    PCChatSendMsg sm;
    PCChatMsg mm;
    const uint8_t nameA[8] = { 'A', 'n', 'n', 'a', ' ', ' ', ' ', ' ' }, nameB[8] = { 'B', 'o', 'b', 32, 32, 32, 32, 32 };
    s_chat_fail = 0;

    /* charset */
    n = pc_chat_from_utf8("Hi there! 42?", c, 64);
    chat_ck(chat_eq(c, n, "Hi there! 42?"), "letters / digits / ! ? are their own codes");
    n = pc_chat_from_utf8("a#b/c;d+e\\f", c, 64);
    chat_ck(n == 11 && c[1] == 209 && c[3] == 174 && c[5] == 208 && c[7] == 180 && c[9] == 222, "# / ; + \\ use the font's own codes (209 174 208 180 222)");
    n = pc_chat_from_utf8("$[]^{}", c, 64);
    chat_ck(chat_eq(c, n, "??????"), "glyphs the font lacks become '?'");
    n = pc_chat_from_utf8("a\tb\r\nc\x7f" "d", c, 64);
    chat_ck(chat_eq(c, n, "abcd"), "control characters are dropped");
    n = pc_chat_from_utf8("\xC3\xA9\xC3\x89\xC3\xB1\xC3\x9F\xC2\xBF\xC2\xA1", c, 64); /* é É ñ ß ¿ ¡ */
    chat_ck(n == 6 && c[0] == 124 && c[1] == 10 && c[2] == 135 && c[3] == 29 && c[4] == 1 && c[5] == 33, "Latin-1 letters map to the font codes (¡ -> '!': code 0 is never produced)");
    n = pc_chat_from_utf8("\xE2\x99\xA5\xE2\x99\xAA\xF0\x9F\x98\x80\xE2\x80\x8B", c, 64); /* heart, note, emoji, zero-width space */
    chat_ck(n == 3 && c[0] == 43 && c[1] == 47 && c[2] == 63, "heart / note symbols kept, an emoji becomes '?', a zero-width space is dropped");
    n = pc_chat_from_utf8("\xC3(\xE2\x82", c, 64);
    chat_ck(n == 4 && c[0] == 63 && c[1] == 40 && c[2] == 63 && c[3] == 63, "invalid / truncated UTF-8 never swallows following text or a NUL");
    chat_ck(!pc_chat_code_ok(0) && !pc_chat_code_ok(0x7F) && !pc_chat_code_ok(0x80) && !pc_chat_code_ok(210) && pc_chat_code_ok('a') && pc_chat_code_ok(124),
            "code 0 / control 0x7F / tag 0x80 / unmapped glyphs are not chat codes");
    for (i = 0, bad = 0; i < 256; i++) {
        if (pc_chat_code_ok((uint8_t)i)) {
            uint8_t one = (uint8_t)i, back[4];
            pc_chat_to_utf8(&one, 1, u, (int)sizeof(u));
            if (pc_chat_from_utf8(u, back, 4) != 1 || back[0] != one) bad++;
        }
    }
    chat_ck(bad == 0, "every chat code round-trips through UTF-8 (logs)");
    {
        const uint8_t in[] = { 32, 32, 'a', 32, 32, 32, 'b', 0, 0x7F, 210, 32, 32 };
        n = pc_chat_sanitize(in, (int)sizeof(in), d);
        chat_ck(chat_eq(d, n, "a b???"), "sanitize: blanks trimmed / collapsed, bad codes -> '?'");
        chat_ck(pc_chat_sanitize((const uint8_t*)"   ", 3, d) == 0, "sanitize: blank text is dropped");
        memset(c, 'x', sizeof(c));
        chat_ck(pc_chat_sanitize(c, 100, d) == PC_CHAT_MAX_LEN, "sanitize: at most 60 codes");
    }
    pc_chat_name_to_utf8(nameA, u, (int)sizeof(u));
    chat_ck(strcmp(u, "Anna") == 0, "names: trailing blanks trimmed");

    /* input editing */
    n = pc_chat_input_append(c, 0, PC_CHAT_MAX_LEN, "hello");
    n = pc_chat_input_append(c, n, PC_CHAT_MAX_LEN, " world");
    n = pc_chat_input_backspace(n);
    chat_ck(chat_eq(c, n, "hello worl"), "input: append + backspace");
    for (i = 0; i < 20; i++) n = pc_chat_input_append(c, n, PC_CHAT_MAX_LEN, "abcd");
    chat_ck(n == PC_CHAT_MAX_LEN, "input: capped at 60");
    chat_ck(pc_chat_input_backspace(0) == 0, "input: backspace on empty");

    /* wrap */
    {
        const char* t = "the quick brown fox jumps over the lazy dog again and again ok";
        n = pc_chat_wrap((const uint8_t*)t, (int)strlen(t), 24, 3, st, ln, &tr);
        chat_ck(n == 3 && ln[0] <= 24 && ln[1] <= 24 && ln[2] <= 24 && t[st[0] + ln[0]] == ' ' && t[st[1]] != ' ', "wrap: 3 lines of <= 24 at word breaks");
        chat_ck(tr == 0 && st[2] + ln[2] == (int)strlen(t), "wrap: 62 chars fit 3 lines here, nothing truncated");
        t = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"; /* 79 */
        n = pc_chat_wrap((const uint8_t*)t, (int)strlen(t), 24, 3, st, ln, &tr);
        chat_ck(n == 3 && ln[0] == 24 && ln[1] == 24 && tr == 1 && ln[2] == 22, "wrap: a long word is cut; overflow marks truncation and leaves room for ..");
        chat_ck(pc_chat_wrap((const uint8_t*)"   ", 3, 24, 3, st, ln, &tr) == 0 && tr == 0, "wrap: blank text = 0 lines");
        n = pc_chat_wrap((const uint8_t*)"hi", 2, 24, 3, st, ln, NULL);
        chat_ck(n == 1 && st[0] == 0 && ln[0] == 2, "wrap: a short text = 1 line");
    }

    /* timing */
    chat_ck(pc_chat_bubble_life_ms(0) == 6000u && pc_chat_bubble_life_ms(20) == 8000u && pc_chat_bubble_life_ms(60) == 12000u && pc_chat_bubble_life_ms(200) == 12000u,
            "bubble life 6 s + 0.1 s/char, max 12 s");
    chat_ck(pc_chat_fade_alpha(0, 6000, 800) == 255 && pc_chat_fade_alpha(5200, 6000, 800) == 255 && pc_chat_fade_alpha(5600, 6000, 800) == 127 &&
                pc_chat_fade_alpha(6000, 6000, 800) == 0 && pc_chat_fade_alpha(9000, 6000, 800) == 0,
            "fade: full until the last 0.8 s, then linear to 0");

    /* rate */
    pc_chat_rate_reset(&r);
    chat_ck(pc_chat_rate_take(&r, 1000) && pc_chat_rate_take(&r, 1000) && pc_chat_rate_take(&r, 1001) && !pc_chat_rate_take(&r, 1002), "rate: burst of 3, the 4th refused");
    chat_ck(!pc_chat_rate_take(&r, 1500) && pc_chat_rate_take(&r, 1710) && !pc_chat_rate_take(&r, 1720), "rate: one more token after 0.7 s");
    chat_ck(pc_chat_rate_take(&r, 100000) && pc_chat_rate_take(&r, 100000) && pc_chat_rate_take(&r, 100000) && !pc_chat_rate_take(&r, 100000), "rate: refill caps at the burst");
    pc_chat_rate_reset(&r);
    chat_ck(pc_chat_rate_take(&r, 0xFFFFFF00u) && pc_chat_rate_take(&r, 0x100u) && pc_chat_rate_take(&r, 0x100u), "rate: tick wrap-around is harmless");

    /* colours */
    {
        int r1, g1, b1, r2, g2, b2;
        pc_chat_speaker_color(nameA, &r1, &g1, &b1);
        pc_chat_speaker_color(nameA, &r2, &g2, &b2);
        chat_ck(r1 == r2 && g1 == g2 && b1 == b2, "speaker colour is stable");
    }

    /* wire */
    memset(&sm, 0, sizeof(sm));
    sm.msg_type = PC_CHAT_MSG_SEND;
    sm.len = 2;
    sm.text[0] = 'h';
    sm.text[1] = 'i';
    chat_ck(pc_chat_send_valid(&sm), "CHAT_SEND valid");
    sm.text[2] = 'x';
    chat_ck(!pc_chat_send_valid(&sm), "CHAT_SEND: a non-zero tail is refused");
    sm.text[2] = 0;
    sm.text[1] = 0x7F;
    chat_ck(!pc_chat_send_valid(&sm), "CHAT_SEND: a control code is refused");
    sm.text[1] = 'i';
    sm.len = 0;
    chat_ck(!pc_chat_send_valid(&sm), "CHAT_SEND: empty refused");
    sm.len = 61;
    chat_ck(!pc_chat_send_valid(&sm), "CHAT_SEND: 61 refused");
    memset(&mm, 0, sizeof(mm));
    mm.msg_type = PC_CHAT_MSG_MSG;
    mm.flags = PC_CHAT_F_CAPS | PC_CHAT_F_ON;
    chat_ck(pc_chat_msg_valid(&mm), "CHAT_MSG CAPS valid");
    mm.flags = PC_CHAT_F_ON;
    chat_ck(!pc_chat_msg_valid(&mm), "CHAT_MSG: ON without CAPS refused");
    mm.flags = 0x40;
    chat_ck(!pc_chat_msg_valid(&mm), "CHAT_MSG: unknown flag refused");
    mm.flags = 0;
    mm.len = 1;
    mm.text[0] = 'k';
    chat_ck(pc_chat_msg_valid(&mm), "CHAT_MSG text valid");
    mm.rsv = 1;
    chat_ck(!pc_chat_msg_valid(&mm), "CHAT_MSG: rsv != 0 refused");
    mm.rsv = 0;
    mm.flags = PC_CHAT_F_CAPS;
    chat_ck(!pc_chat_msg_valid(&mm), "CHAT_MSG: CAPS with text refused");

    /* store */
    pc_chat_store_init(&s);
    chat_ck(pc_chat_store_add(&s, 3, 0, 0, nameA, (const uint8_t*)"one", 3, 1000) >= 0, "store: bubble added");
    chat_ck(pc_chat_store_add(&s, 3, 0, 0, nameA, (const uint8_t*)"two", 3, 1100) >= 0 && pc_chat_store_add(&s, 3, 0, 0, nameA, (const uint8_t*)"three", 5, 1200) >= 0,
            "store: more bubbles");
    chat_ck(pc_chat_store_bubble_of(&s, 3, 0) != NULL && memcmp(pc_chat_store_bubble_of(&s, 3, 0)->text, "three", 6) == 0 &&
                memcmp(pc_chat_store_bubble_of(&s, 3, 1)->text, "two", 4) == 0 && pc_chat_store_bubble_of(&s, 3, 2) == NULL,
            "store: 2 bubbles per speaker, newest first, the oldest replaced");
    chat_ck(pc_chat_store_add(&s, -1, 0, PC_CHAT_F_SERVER, nameB, (const uint8_t*)"srv", 3, 1300) == -1 && pc_chat_store_log(&s, 0) != NULL &&
                memcmp(pc_chat_store_log(&s, 0)->text, "srv", 4) == 0 && memcmp(pc_chat_store_log(&s, 3)->text, "one", 4) == 0 && pc_chat_store_log(&s, 4) == NULL,
            "store: a server line goes to the log only; the log is newest first");
    for (i = 0; i < 20; i++) (void)pc_chat_store_add(&s, 10 + i, 0, 0, nameB, (const uint8_t*)"x", 1, 2000 + (uint32_t)i);
    for (i = 0, n = 0; i < PC_CHAT_BUBBLES; i++) n += s.bubble[i].used;
    chat_ck(n == PC_CHAT_BUBBLES && pc_chat_store_bubble_of(&s, 29, 0) != NULL && pc_chat_store_bubble_of(&s, 10, 0) == NULL, "store: a full table evicts the oldest");
    chat_ck(pc_chat_store_log(&s, PC_CHAT_LOG_LINES - 1) != NULL && pc_chat_store_log(&s, PC_CHAT_LOG_LINES) == NULL, "store: the log is a bounded ring");
    pc_chat_store_expire(&s, 2000 + 6100 + 50);
    for (i = 0, n = 0; i < PC_CHAT_BUBBLES; i++) n += s.bubble[i].used;
    chat_ck(n == 0, "store: bubbles expire after their life");
    (void)pc_chat_store_add(&s, 5, 1, 0, nameA, (const uint8_t*)"me", 2, 0xFFFFFFF0u);
    pc_chat_store_expire(&s, 0x100u);
    chat_ck(pc_chat_store_bubble_of(&s, 5, 0) != NULL, "store: tick wrap-around does not expire early");

    /* mutes */
    {
        uint8_t k1[PC_OPS_KEY_LEN], k2[PC_OPS_KEY_LEN];
        char txt[4096];
        for (i = 0; i < PC_OPS_KEY_LEN; i++) {
            k1[i] = (uint8_t)(i * 5 + 3);
            k2[i] = (uint8_t)(0xE0 - i);
        }
        pc_ops_init(&ml);
        chat_ck(pc_ops_add(&ml, PC_OPS_KIND_RESIDENT, k1, "Spam Er") == 1 && pc_ops_add(&ml, PC_OPS_KIND_GUEST, k2, "G#uest\n") == 1, "mutes: add");
        n = pc_chat_mutes_format(&ml, txt, sizeof(txt));
        chat_ck(n > 0 && strstr(txt, "mute = resident ") != NULL && strstr(txt, "\nop =") == NULL, "mutes: format uses `mute =` lines");
        pc_chat_mutes_parse(&m2, txt, (size_t)n, &bad);
        chat_ck(m2.n == 2 && bad == 0 && pc_ops_find(&m2, PC_OPS_KIND_RESIDENT, k1) == 0 && strcmp(m2.e[0].name, "Spam Er") == 0 && pc_ops_find(&m2, PC_OPS_KIND_GUEST, k2) == 1,
                "mutes: round trip");
        {
            const char* t = "# c\r\nversion = 3\r\nop = resident 0102030405060708090a0b0c0d0e0f1011121314 X\r\nmute = resident 0102\r\n"
                            "mute = guest 0102030405060708090A0B0C0D0E0F1011121314  Carol \r\nmute = guest 0102030405060708090a0b0c0d0e0f1011121314 dup\r\n";
            pc_chat_mutes_parse(&m2, t, strlen(t), &bad);
            chat_ck(m2.n == 1 && m2.e[0].kind == PC_OPS_KIND_GUEST && strcmp(m2.e[0].name, "Carol") == 0 && bad == 2, "mutes: tolerant parser (an op line / short key skipped)");
        }
        chat_ck(pc_chat_mutes_format(&ml, txt, 32) == -1, "mutes: too small a buffer refused");
    }

    printf("[CHAT] selftest: %s (%d failure(s))\n", s_chat_fail ? "FAIL" : "PASS", s_chat_fail);
    return s_chat_fail;
}
