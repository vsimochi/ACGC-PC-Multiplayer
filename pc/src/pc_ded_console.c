/* pc_ded_console.c - see pc_ded_console.h. Pure functions only: no Windows API, no I/O, no global state. */
#include "pc_ded_console.h"

#include <stdio.h>
#include <string.h>
#include <ctype.h>

/* ---- output buffer helper (always NUL-terminated, silently truncating) ---- */
typedef struct {
    char*  p;
    size_t cap;
    size_t n;
} Ob;

static void ob_putc(Ob* o, char c) {
    if (o->n + 1 < o->cap) {
        o->p[o->n++] = c;
        o->p[o->n] = '\0';
    }
}

static void ob_puts(Ob* o, const char* s) {
    while (*s != '\0') {
        ob_putc(o, *s++);
    }
}

static void ob_putn(Ob* o, const char* s, size_t n) {
    size_t i;
    for (i = 0; i < n; i++) {
        ob_putc(o, s[i]);
    }
}

static void ob_init(Ob* o, char* p, size_t cap) {
    o->p = p;
    o->cap = cap;
    o->n = 0;
    if (cap > 0) {
        p[0] = '\0';
    }
}

/* ---- the one-line editor ---- */
void pc_dedc_init(PCDedcEdit* e) {
    memset(e, 0, sizeof(*e));
}

int pc_dedc_key(PCDedcEdit* e, int key, char* line_out, size_t cap) {
    if (key >= 32 && key <= 126) {
        if (e->len < PC_DEDC_LINE_MAX) {
            memmove(e->buf + e->cur + 1, e->buf + e->cur, (size_t)(e->len - e->cur));
            e->buf[e->cur] = (char)key;
            e->cur++;
            e->len++;
            e->buf[e->len] = '\0';
        }
        return 0;
    }
    switch (key) {
        case PC_DEDC_KEY_LEFT:
            if (e->cur > 0) {
                e->cur--;
            }
            break;
        case PC_DEDC_KEY_RIGHT:
            if (e->cur < e->len) {
                e->cur++;
            }
            break;
        case PC_DEDC_KEY_HOME:
            e->cur = 0;
            break;
        case PC_DEDC_KEY_END:
            e->cur = e->len;
            break;
        case PC_DEDC_KEY_BACKSPACE:
            if (e->cur > 0) {
                memmove(e->buf + e->cur - 1, e->buf + e->cur, (size_t)(e->len - e->cur));
                e->cur--;
                e->len--;
                e->buf[e->len] = '\0';
            }
            break;
        case PC_DEDC_KEY_DELETE:
            if (e->cur < e->len) {
                memmove(e->buf + e->cur, e->buf + e->cur + 1, (size_t)(e->len - e->cur - 1));
                e->len--;
                e->buf[e->len] = '\0';
            }
            break;
        case PC_DEDC_KEY_CLEAR:
            e->len = 0;
            e->cur = 0;
            e->buf[0] = '\0';
            break;
        case PC_DEDC_KEY_ENTER:
            if (line_out != NULL && cap > 0) {
                size_t n = (size_t)e->len;
                if (n > cap - 1) {
                    n = cap - 1;
                }
                memcpy(line_out, e->buf, n);
                line_out[n] = '\0';
            }
            e->len = 0;
            e->cur = 0;
            e->buf[0] = '\0';
            return 1;
        default:
            break;
    }
    return 0;
}

/* ---- prompt line rendering ---- */
static int clamp_width(int width) {
    return width < 20 ? 20 : (width > 500 ? 500 : width);
}

size_t pc_dedc_render(const PCDedcEdit* e, const char* prompt, int width, char* out, size_t cap) {
    Ob o;
    int pl = (int)strlen(prompt);
    int avail;
    int start = 0;
    int shown;
    int col;
    char tmp[16];
    width = clamp_width(width);
    avail = width - 1 - pl; /* never write the last column */
    if (avail < 4) {
        avail = 4;
    }
    if (e->cur > avail - 1) {
        start = e->cur - (avail - 1);
    }
    shown = e->len - start;
    if (shown > avail) {
        shown = avail;
    }
    ob_init(&o, out, cap);
    ob_puts(&o, "\r\x1b[2K");
    ob_puts(&o, prompt);
    ob_putn(&o, e->buf + start, (size_t)shown);
    col = pl + (e->cur - start);
    ob_putc(&o, '\r');
    if (col > 0) {
        snprintf(tmp, sizeof(tmp), "\x1b[%dC", col);
        ob_puts(&o, tmp);
    }
    return o.n;
}

size_t pc_dedc_emit(const PCDedcEdit* e, const char* prompt, int width, int show_edit, const char* text, char* out, size_t cap) {
    Ob o;
    size_t tl = strlen(text);
    ob_init(&o, out, cap);
    ob_puts(&o, "\r\x1b[2K");
    ob_puts(&o, text);
    if (tl == 0 || text[tl - 1] != '\n') {
        ob_putc(&o, '\n');
    }
    if (show_edit) {
        char tmp[1200];
        pc_dedc_render(e, prompt, width, tmp, sizeof(tmp));
        ob_puts(&o, tmp);
    }
    return o.n;
}

size_t pc_dedc_commit_echo(const char* prompt, const char* line, char* out, size_t cap) {
    Ob o;
    ob_init(&o, out, cap);
    ob_puts(&o, "\r\x1b[2K");
    ob_puts(&o, prompt);
    ob_puts(&o, line);
    ob_putc(&o, '\n');
    return o.n;
}

/* ---- log line formatting ---- */
static int ci_contains(const char* hay, const char* needle) {
    size_t nl = strlen(needle);
    size_t i;
    for (i = 0; hay[i] != '\0'; i++) {
        size_t j = 0;
        while (j < nl && hay[i + j] != '\0' && tolower((unsigned char)hay[i + j]) == tolower((unsigned char)needle[j])) {
            j++;
        }
        if (j == nl) {
            return 1;
        }
    }
    return 0;
}

int pc_dedc_classify(const char* raw) {
    static const char* const err[] = { "error", "fatal", "failed", "failure", "crash", "cannot ", "could not", "unable to" };
    static const char* const warn[] = { "warn", "refus", "reject", "dropp", "timed out", "timeout", "unreadable", "mismatch", "invalid", "not allowed", "connection lost", "stall" };
    size_t i;
    for (i = 0; i < sizeof(err) / sizeof(err[0]); i++) {
        if (ci_contains(raw, err[i])) {
            return 2;
        }
    }
    for (i = 0; i < sizeof(warn) / sizeof(warn[0]); i++) {
        if (ci_contains(raw, warn[i])) {
            return 1;
        }
    }
    return 0;
}

#define PFX_LEVEL 5   /* INFO / WARN / ERROR (padded) */
#define PFX_SUB 14    /* subsystem column */
#define PFX_TOTAL (8 + 1 + PFX_LEVEL + 1 + PFX_SUB + 1)

/* "[TAG]" at s -> copies TAG (upper case, max 15) to tag and returns the length of "[TAG]", else 0 */
static size_t take_tag(const char* s, char* tag) {
    size_t n = 0;
    size_t i;
    if (s[0] != '[') {
        return 0;
    }
    for (i = 1; s[i] != '\0' && s[i] != ']' && n < 15; i++) {
        if (!(isalnum((unsigned char)s[i]) || s[i] == '_' || s[i] == '-')) {
            return 0;
        }
        tag[n++] = (char)toupper((unsigned char)s[i]);
    }
    if (s[i] != ']' || n == 0) {
        return 0;
    }
    tag[n] = '\0';
    return i + 1;
}

/* a leading "HH:MM:SS" or "[HH:MM:SS]" (an upstream timestamp) is dropped so the line carries one time only */
static const char* skip_timestamp(const char* s) {
    const char* p = s;
    int br = 0;
    if (*p == '[') {
        br = 1;
        p++;
    }
    if (isdigit((unsigned char)p[0]) && isdigit((unsigned char)p[1]) && p[2] == ':' && isdigit((unsigned char)p[3]) && isdigit((unsigned char)p[4]) && p[5] == ':' &&
        isdigit((unsigned char)p[6]) && isdigit((unsigned char)p[7])) {
        p += 8;
        if (br) {
            if (*p != ']') {
                return s;
            }
            p++;
        }
        while (*p == ' ') {
            p++;
        }
        return p;
    }
    return s;
}

size_t pc_dedc_format(const char* raw, int hh, int mm, int ss, int color, int width, char* out, size_t cap) {
    static const char* const lvl_name[3] = { "INFO ", "WARN ", "ERROR" };
    static const char* const lvl_col[3] = { "\x1b[0m", "\x1b[33m", "\x1b[1;31m" };
    char line[PC_DEDC_LINE_MAX * 2 + 8];
    char tag[40];
    char sub[16];
    char t1[16];
    size_t n;
    int lvl;
    const char* msg;
    Ob o;
    char pfx[64];
    int body_w;

    /* strip the line ending(s) */
    n = strlen(raw);
    if (n >= sizeof(line)) {
        n = sizeof(line) - 1;
    }
    memcpy(line, raw, n);
    line[n] = '\0';
    while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r')) {
        line[--n] = '\0';
    }

    ob_init(&o, out, cap);
    lvl = pc_dedc_classify(line);
    if (line[0] == ' ' || line[0] == '\t') {
        /* continuation of the previous message: indented under it, no prefix */
        memset(pfx, ' ', PFX_TOTAL);
        pfx[PFX_TOTAL] = '\0';
        ob_puts(&o, pfx);
        ob_puts(&o, line + 1);
        return o.n;
    }

    msg = skip_timestamp(line);
    t1[0] = '\0';
    sub[0] = '\0';
    if (take_tag(msg, t1) > 0) {
        size_t a = take_tag(msg, t1);
        msg += a;
        if (take_tag(msg, sub) > 0) {
            msg += take_tag(msg, sub);
        } else {
            sub[0] = '\0';
        }
        while (*msg == ' ') {
            msg++;
        }
        if (strcmp(t1, "FATAL") == 0 || strcmp(t1, "ERROR") == 0) {
            lvl = 2;
            snprintf(t1, sizeof(t1), "%s", sub[0] ? sub : "GAME");
            sub[0] = '\0';
        } else if (strcmp(t1, "WARN") == 0 || strcmp(t1, "WARNING") == 0) {
            if (lvl < 1) {
                lvl = 1;
            }
            snprintf(t1, sizeof(t1), "%s", sub[0] ? sub : "GAME");
            sub[0] = '\0';
        }
        if (strcmp(t1, "NET") == 0) {
            snprintf(tag, sizeof(tag), "%s", "NETWORK");
        } else {
            snprintf(tag, sizeof(tag), "%s", t1);
        }
        if (sub[0] != '\0' && strcmp(sub, tag) != 0) {
            size_t tl = strlen(tag);
            snprintf(tag + tl, sizeof(tag) - tl, "/%s", sub);
        }
        msg = skip_timestamp(msg); /* "[NET] 12:00:01 text": the time belongs to the console prefix only */
    } else {
        snprintf(tag, sizeof(tag), "%s", "GAME");
    }
    if (strlen(tag) > PFX_SUB) {
        tag[PFX_SUB] = '\0';
    }

    snprintf(pfx, sizeof(pfx), "%02d:%02d:%02d ", hh % 100, mm % 100, ss % 100);
    ob_puts(&o, pfx);
    if (color) {
        ob_puts(&o, lvl_col[lvl]);
    }
    ob_puts(&o, lvl_name[lvl]);
    if (color) {
        ob_puts(&o, "\x1b[0m");
    }
    ob_putc(&o, ' ');
    if (color) {
        ob_puts(&o, "\x1b[36m");
    }
    {
        char col[64];
        snprintf(col, sizeof(col), "%-*.*s", PFX_SUB, PFX_SUB, tag);
        ob_puts(&o, col);
    }
    if (color) {
        ob_puts(&o, "\x1b[0m");
    }
    ob_putc(&o, ' ');

    body_w = width > 0 ? clamp_width(width) - PFX_TOTAL - 1 : 0;
    if (body_w < 20) {
        body_w = 0; /* too narrow to wrap sensibly */
    }
    if (body_w == 0 || (int)strlen(msg) <= body_w) {
        ob_puts(&o, msg);
    } else {
        /* wrap at spaces; continuation rows are indented under the message */
        const char* p = msg;
        int first = 1;
        while (*p != '\0') {
            int len = (int)strlen(p);
            int cut = len;
            int k;
            if (len > body_w) {
                cut = body_w;
                for (k = body_w; k > body_w / 2; k--) {
                    if (p[k] == ' ') {
                        cut = k;
                        break;
                    }
                }
            }
            if (!first) {
                ob_putc(&o, '\n');
                memset(pfx, ' ', PFX_TOTAL);
                pfx[PFX_TOTAL] = '\0';
                ob_puts(&o, pfx);
            }
            ob_putn(&o, p, (size_t)cut);
            p += cut;
            while (*p == ' ') {
                p++;
            }
            first = 0;
        }
    }
    return o.n;
}
