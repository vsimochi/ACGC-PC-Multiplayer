/* pc_log.c - category-based diagnostic logging (see pc/include/pc_log.h for the design, the switches and the category->module map). */
#include "pc_log.h"

#include <ctype.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#else
#include <time.h>
#endif

uint32_t g_pc_log_mask = 0;

extern unsigned long pc_frame_counter; /* pc_vi.c: incremented in VIWaitForRetrace */

typedef struct {
    uint32_t    bit;
    const char* name;  /* canonical lower-case name (also the "[NAME]" prefix, upper-cased) */
    const char* alt1;  /* accepted aliases (may be NULL) */
    const char* alt2;
    const char* what;
} PCLogCat;

static const PCLogCat k_cats[] = {
    { PCL_GENERAL,   "general",      NULL,           NULL,       "startup, platform, settings, bootstrap summaries (what -debug enables)" },
    { PCL_NET,       "network",      "net",          NULL,       "transport and session: listen, world ready, peers connecting / leaving" },
    { PCL_PLAYERS,   "players",      "player",       NULL,       "resident binding, observer / roster, remote player lifecycle" },
    { PCL_VILLAGERS, "villagers",    "villager",     "npc",      "villager roster, arrival / departure, state sync" },
    { PCL_BUILDINGS, "buildings",    "building",     "houses",   "field structures, houses, shop-level changes" },
    { PCL_ITEMS,     "items",        "item",         NULL,       "pickup / drop / bury / field actions on ground items" },
    { PCL_WILDLIFE,  "wildlife",     "fish",         "bugs",     "host-authoritative fish and bug spawns / catches" },
    { PCL_SAVE,      "save",         "saves",        NULL,       "host periodic / early / shutdown save results" },
    { PCL_EVENTS,    "events",       "event",        NULL,       "town events and special visitors" },
    { PCL_TXN,       "transactions", "transaction",  "txn",      "host-transactional commits (pocket / wallet / world)" },
    { PCL_RECORDS,   "records",      "record",       "rec",      "resident record exchange between host and clients" },
    { PCL_GUESTS,    "guests",       "guest",        NULL,       "guest (foreign resident) identities and slots" },
    { PCL_TOWNSVC,   "townsvc",      "townservices", "townservice", "town services: museum, lost and found, clock, stalk market" },
    { PCL_MAIL,      "mail",         "letters",      NULL,       "mailboxes and letters" },
    { PCL_SHOP,      "shop",         "shops",        NULL,       "shop purchases and sales" },
};
#define PCL_NCATS ((int)(sizeof(k_cats) / sizeof(k_cats[0])))

static int      s_want_console = 0;    /* a -debug* flag / PC_LOG env asked for output */
static int      s_logtime = 0;
static char*    s_logfile_path = NULL; /* copy of the -logfile argument; opened by pc_log_finish_cli() */
static FILE*    s_logfile = NULL;
static atomic_flag s_lock = ATOMIC_FLAG_INIT;
static unsigned long long s_t0_ms = 0;

static unsigned long long pcl_now_ms(void) {
#ifdef _WIN32
    return (unsigned long long)GetTickCount64();
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (unsigned long long)ts.tv_sec * 1000ull + (unsigned long long)(ts.tv_nsec / 1000000);
#endif
}

static int pcl_ieq(const char* a, const char* b) {
    while (*a && *b) {
        if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) {
            return 0;
        }
        a++;
        b++;
    }
    return *a == '\0' && *b == '\0';
}

uint32_t pc_log_parse_category(const char* name) {
    int i;
    if (name == NULL || *name == '\0') {
        return 0;
    }
    for (i = 0; i < PCL_NCATS; i++) {
        if (pcl_ieq(name, k_cats[i].name) || (k_cats[i].alt1 && pcl_ieq(name, k_cats[i].alt1)) ||
            (k_cats[i].alt2 && pcl_ieq(name, k_cats[i].alt2))) {
            return k_cats[i].bit;
        }
    }
    return 0;
}

void pc_log_print_categories(void) {
    int i;
    printf("Debug categories (enable with -debug<name>, --debug-<name>, --debug=a,b,c or env PC_LOG=a,b,c; 'all' = every category):\n");
    for (i = 0; i < PCL_NCATS; i++) {
        printf("  %-13s %s\n", k_cats[i].name, k_cats[i].what);
    }
    printf("  -debug alone enables GENERAL only; -debugall enables all of the above; --verbose keeps the legacy output.\n");
}

static void pcl_print_valid(FILE* f) {
    int i;
    fprintf(f, "valid categories:");
    for (i = 0; i < PCL_NCATS; i++) {
        fprintf(f, " %s", k_cats[i].name);
    }
    fprintf(f, " all\n");
}

/* Parses a comma list ("a,b,c", "all"). Returns 0 and ORs *mask on success; on an unknown name copies it to bad (size badn) and returns -1. */
static int pcl_parse_list(const char* csv, uint32_t* mask, char* bad, size_t badn) {
    const char* p = csv;
    uint32_t acc = 0;
    int any = 0;
    while (*p) {
        char tok[64];
        size_t n = 0;
        uint32_t b;
        while (*p == ',' || *p == ' ') {
            p++;
        }
        if (!*p) {
            break;
        }
        while (*p && *p != ',' && n + 1 < sizeof(tok)) {
            tok[n++] = *p++;
        }
        while (*p && *p != ',') { /* over-long token: skip the rest */
            p++;
        }
        while (n > 0 && tok[n - 1] == ' ') {
            n--;
        }
        tok[n] = '\0';
        any = 1;
        if (pcl_ieq(tok, "all")) {
            acc |= PCL_ALL_CATEGORIES;
            continue;
        }
        b = pc_log_parse_category(tok);
        if (b == 0) {
            snprintf(bad, badn, "%s", tok);
            return -1;
        }
        acc |= b;
    }
    if (!any) {
        snprintf(bad, badn, "%s", "(empty list)");
        return -1;
    }
    *mask |= acc;
    return 0;
}

int pc_log_cli_arg(const char* arg, const char* next, int* consumed_next) {
    const char* r;
    char bad[64];
    *consumed_next = 0;
    if (arg == NULL || arg[0] != '-') {
        return 0;
    }
    r = arg + ((arg[1] == '-') ? 2 : 1);
    if (strcmp(r, "logtime") == 0) {
        s_logtime = 1;
        return 1;
    }
    if (strcmp(r, "logfile") == 0) {
        if (next == NULL || next[0] == '\0') {
            fprintf(stderr, "[PC][LOG] %s needs a path argument\n", arg);
            return 2;
        }
        free(s_logfile_path);
        s_logfile_path = (char*)malloc(strlen(next) + 1);
        if (s_logfile_path == NULL) {
            return 2;
        }
        strcpy(s_logfile_path, next);
        *consumed_next = 1;
        return 1;
    }
    if (strncmp(r, "debug", 5) != 0) {
        return 0;
    }
    r += 5;
    if (*r == '\0') { /* -debug / --debug: GENERAL only */
        g_pc_log_mask |= PCL_GENERAL;
        s_want_console = 1;
        return 1;
    }
    if (strcmp(r, "-list") == 0 || strcmp(r, "list") == 0) {
        pc_log_print_categories();
        return 3;
    }
    if (*r == '=') {
        uint32_t m = 0;
        if (pcl_parse_list(r + 1, &m, bad, sizeof(bad)) != 0) {
            fprintf(stderr, "[PC][LOG] unknown debug category '%s' in %s; ", bad, arg);
            pcl_print_valid(stderr);
            return 2;
        }
        g_pc_log_mask |= m;
        s_want_console = 1;
        return 1;
    }
    if (*r == '-') {
        r++;
    }
    if (pcl_ieq(r, "all")) {
        g_pc_log_mask |= PCL_ALL_CATEGORIES;
        s_want_console = 1;
        return 1;
    }
    {
        uint32_t b = pc_log_parse_category(r);
        if (b == 0) {
            fprintf(stderr, "[PC][LOG] unknown debug category '%s' in %s; ", r, arg);
            pcl_print_valid(stderr);
            return 2;
        }
        g_pc_log_mask |= b;
        s_want_console = 1;
    }
    return 1;
}

static int pcl_env_set(const char* name) {
    const char* v = getenv(name);
    return v != NULL && v[0] != '\0' && v[0] != '0';
}

/* The log file path may not sit inside a directory named "save" (the live / fixture save dirs), and may not name a memory-card image. */
static int pcl_logfile_path_ok(const char* path) {
    const char* p = path;
    const char* seg = path;
    size_t n;
    for (;; p++) {
        if (*p == '/' || *p == '\\' || *p == '\0') {
            n = (size_t)(p - seg);
            if (*p != '\0' && n == 4 && (seg[0] | 0x20) == 's' && (seg[1] | 0x20) == 'a' && (seg[2] | 0x20) == 'v' && (seg[3] | 0x20) == 'e') {
                return 0;
            }
            if (*p == '\0') {
                if (n >= 4 && pcl_ieq(seg + n - 4, ".gci")) {
                    return 0;
                }
                break;
            }
            seg = p + 1;
        }
    }
    return 1;
}

int pc_log_finish_cli(int* want_console) {
    const char* env = getenv("PC_LOG");
    s_t0_ms = pcl_now_ms();
    if (env != NULL && env[0] != '\0') {
        uint32_t m = 0;
        char bad[64];
        if (env[0] == '0' && (env[1] == 'x' || env[1] == 'X')) {
            m = (uint32_t)strtoul(env, NULL, 16) & (PCL_ALL_CATEGORIES | PCL_LEGACY);
        } else if (pcl_parse_list(env, &m, bad, sizeof(bad)) != 0) {
            fprintf(stderr, "[PC][LOG] env PC_LOG: unknown category '%s' ignored; ", bad);
            pcl_print_valid(stderr);
        }
        if (m != 0) {
            g_pc_log_mask |= m;
            s_want_console = 1;
        }
    }
    /* Existing opt-in diagnostics become aliases of the matching category bits (their printf text and their own env switch are unchanged;
     * the alias bits never change where stdout goes). */
    if (pcl_env_set("PC_PUPPET_DIAG") || pcl_env_set("PC_COLLIDE_DIAG")) {
        g_pc_log_mask |= PCL_PLAYERS;
    }
    if (pcl_env_set("PC_NPC_TALKHOLD_DIAG")) {
        g_pc_log_mask |= PCL_VILLAGERS;
    }
    if (s_logfile_path != NULL) {
        if (!pcl_logfile_path_ok(s_logfile_path)) {
            fprintf(stderr, "[PC][LOG] REFUSED: -logfile '%s' is inside a save directory or names a .gci file\n", s_logfile_path);
            return 1;
        }
        s_logfile = fopen(s_logfile_path, "ab");
        if (s_logfile == NULL) {
            fprintf(stderr, "[PC][LOG] REFUSED: cannot open -logfile '%s' for append\n", s_logfile_path);
            return 1;
        }
        setvbuf(s_logfile, NULL, _IOLBF, 4096);
        g_pc_log_mask |= PCL_GENERAL; /* a log file with no category selected would stay empty */
        fprintf(s_logfile, "[GENERAL] log file opened, mask=0x%08X\n", (unsigned)g_pc_log_mask);
        fflush(s_logfile);
    }
    *want_console = s_want_console;
    return 0;
}

void pc_log_write(uint32_t cat, const char* fmt, ...) {
    char buf[1024];
    int n = 0, i;
    va_list ap;
    int console = s_want_console || (g_pc_log_mask & PCL_LEGACY) != 0;

    for (i = 0; i < PCL_NCATS; i++) {
        if (cat & k_cats[i].bit) {
            const char* s = k_cats[i].name;
            buf[n++] = '[';
            while (*s && n < 64) {
                buf[n++] = (char)toupper((unsigned char)*s++);
            }
            buf[n++] = ']';
            buf[n++] = ' ';
            break;
        }
    }
    if (i == PCL_NCATS) {
        n += snprintf(buf + n, sizeof(buf) - (size_t)n, "[LEGACY] ");
    }
    if (s_logtime) {
        unsigned long long ms = pcl_now_ms() - s_t0_ms;
        n += snprintf(buf + n, sizeof(buf) - (size_t)n, "[t+%llu.%03llu f%lu] ", ms / 1000ull, ms % 1000ull, pc_frame_counter);
    }
    va_start(ap, fmt);
    i = vsnprintf(buf + n, sizeof(buf) - (size_t)n - 2u, fmt, ap);
    va_end(ap);
    if (i < 0) {
        i = 0;
    }
    if ((size_t)i >= sizeof(buf) - (size_t)n - 2u) {
        i = (int)(sizeof(buf) - (size_t)n - 3u);
    }
    n += i;
    if (n == 0 || buf[n - 1] != '\n') {
        buf[n++] = '\n';
    }
    buf[n] = '\0';

    while (atomic_flag_test_and_set_explicit(&s_lock, memory_order_acquire)) {
        /* spin: PC_LOG callers are the main thread in practice; this only protects against a rare second thread */
    }
    if (s_logfile == NULL || console) {
        fwrite(buf, 1, (size_t)n, stdout);
    }
    if (s_logfile != NULL) {
        fwrite(buf, 1, (size_t)n, s_logfile);
        fflush(s_logfile);
    }
    atomic_flag_clear_explicit(&s_lock, memory_order_release);
}
