/* pc_dedicated.c - the --dedicated server mode: console attach, stdin command queue + commands, audio/window/pacing helpers, [DEDICATED] notices.
 * See pc_dedicated.h for the overview. Nothing in here runs unless g_pc_dedicated == 1 (the callers check it; most functions also check it). */
#include "pc_platform.h"
#include "pc_dedicated.h"
#include "pc_residence.h" /* PC_RESIDENCE_SLOTS / PC_RESIDENCE_HOUSES */
#include "pc_host_observer.h"
#include "pc_net_game.h"
#include "pc_net.h"       /* PCNetStats (status line) */
#include "pc_log.h"
#include "pc_ded_console.h" /* the pure line editor / redraw text / log formatter of the interactive console */
#include "pc_server.h" /* servers/<id>/ storage tree (status line, server.log) */
#include "m_name_table.h" /* item ids / categories for the host item tools (finditem, iteminfo, items, give) */
#include "m_common_data.h" /* time.rtc_time (`time get`) and the event schedule reads (`event list/status`) */
#include "m_event.h"
#include "pc_wildlife_authority.h" /* `wildlife` commands: the host's authoritative table */
#include "pc_remote_player.h"      /* `where`: the last position a peer reported */
#include "ac_gyoei.h"              /* fish species ids for `wildlife spawn` */
#include "pc_test_hooks.h"         /* test-hook gate of the behaviour-changing commands */
#include "m_item_name.h"  /* mIN_copy_name_str(): the game's own item name tables (filled from the ROM by pc_assets_init) */
#include "m_shop.h"       /* `shop-level-boost`: mSP_PlusSales / thresholds */

#include <stdarg.h>
#include <ctype.h>
#include <stdlib.h>
#ifdef _WIN32
#include <fcntl.h>
#endif

#ifdef _WIN32
#include <process.h>
#include <io.h>
#else
#include <pthread.h>
#include <unistd.h>
#endif

extern int  pcfa_save_ready(void);
extern int  pc_audio_get_buffer_fill(void);
extern int  pc_audio_consumed_samples(void);
extern u32  pc_frame_counter;
extern u32  g_frame_limiter;

#define PC_DED_QUEUE_MAX 16 /* queued console lines */
#define PC_DED_LINE_MAX  255 /* characters per line (longer lines are truncated); +1 for the NUL */

int g_pc_dedicated = 0;

/* ------------------------------------------------------------------------------------------------------------------------------------- */
/* [DEDICATED] console lines                                                                                                              */
/* ------------------------------------------------------------------------------------------------------------------------------------- */

static uint16_t s_port = 0;
static Uint32   s_start_ticks = 0;
static int      s_audio_ok = 0;
static char     s_audio_driver[32] = "?";
static int      s_stop_requested = 0;

/* last save result (any save: periodic / early / shutdown / console) */
static int      s_last_save_ok = -1; /* -1 = none yet */
static Uint32   s_last_save_ticks = 0;
static int      s_save_pending = 0;  /* console `save` waiting for the next safe point */

/* The ONE writer of every server-console line ([DEDICATED] notices, command responses). Normally it is stdout (redirected pipes / files, a console the
 * caller already owns: byte-identical to before). In INTERACTIVE mode (the exe was started by hand from a shell: pc_dedicated_early_console) it is the
 * server's OWN console window opened as a separate stream, while stdout / stderr go to NUL like every other non-verbose run: the game's thousands of debug
 * printf lines (frame counters, send rates, ...) then never interleave with the line being typed. -debug* / --verbose keep stdout on the console. */
static FILE* s_ded_out = NULL;
static int   s_ded_interactive = 0;
#ifdef _WIN32
static HANDLE s_ded_conin = NULL; /* interactive mode: the server console's OWN input buffer, opened directly (not through the CRT stdin) */
#endif

#define PC_DED_PROMPT "> "

#ifdef _WIN32
/* The interactive console's line editor (pc_ded_console.h). While it owns the console (s_con_edit) EVERY write goes through pc_ded_con_write(): under one lock it erases the prompt line, writes the
 * text and draws the prompt line (the partially typed command, the cursor) again, so a log line can never land in the middle of what the operator is typing. */
static CRITICAL_SECTION s_con_lock;
static int        s_con_lock_ready = 0;
static int        s_con_edit = 0;    /* 1 = the editor owns the console input and output */
static int        s_con_color = 0;
static int        s_con_partial = 0; /* the last write did not end with a newline (a prompt text): no prompt line is drawn until it does */
static PCDedcEdit s_con_ed;
static HANDLE     s_con_out_h = NULL;
static DWORD      s_con_in_old_mode = 0;

static int pc_ded_con_width(void) {
    CONSOLE_SCREEN_BUFFER_INFO ci;
    if (s_con_out_h != NULL && GetConsoleScreenBufferInfo(s_con_out_h, &ci)) {
        return (int)(ci.srWindow.Right - ci.srWindow.Left + 1);
    }
    return 80;
}

/* ONE WriteFile per redraw: the console applies a single write atomically, so nobody (a screen reader, the window) can see the prompt line half erased */
static void pc_ded_con_put_locked(const char* s, size_t n) {
    DWORD wr = 0;
    while (n > 0) {
        DWORD chunk = n > 32768u ? 32768u : (DWORD)n;
        if (!WriteFile(s_con_out_h, s, chunk, &wr, NULL) || wr == 0) {
            break;
        }
        s += wr;
        n -= wr;
    }
}

/* raw text (a command response, a prompt): not formatted */
static void pc_ded_con_write(const char* text) {
    size_t tl = strlen(text);
    size_t cap = tl + 1600;
    char* out = (char*)malloc(cap);
    size_t n = 0;
    int complete = tl > 0 && text[tl - 1] == '\n';
    if (out == NULL) {
        return;
    }
    EnterCriticalSection(&s_con_lock);
    if (!s_con_partial) {
        memcpy(out, "\r\x1b[2K", 5);
        n = 5;
    }
    memcpy(out + n, text, tl);
    n += tl;
    if (complete) {
        n += pc_dedc_render(&s_con_ed, PC_DED_PROMPT, pc_ded_con_width(), out + n, cap - n);
        s_con_partial = 0;
    } else if (tl > 0) {
        s_con_partial = 1;
    }
    out[n] = '\0';
    pc_ded_con_put_locked(out, n);
    LeaveCriticalSection(&s_con_lock);
    free(out);
}

/* log text: every line gets the console format (time, level, subsystem) */
static void pc_ded_con_write_log(const char* text) {
    size_t tl = strlen(text);
    size_t cap = tl * 3 + 1024;
    char* out = (char*)malloc(cap);
    char* line = (char*)malloc(tl + 1);
    SYSTEMTIME st;
    const char* p = text;
    size_t n = 0;
    if (out == NULL || line == NULL) {
        free(out);
        free(line);
        return;
    }
    out[0] = '\0';
    GetLocalTime(&st);
    while (*p != '\0') {
        const char* nl = strchr(p, '\n');
        size_t ll = nl != NULL ? (size_t)(nl - p) : strlen(p);
        memcpy(line, p, ll);
        line[ll] = '\0';
        if (ll > 0 || nl == NULL) {
            n += pc_dedc_format(line, st.wHour, st.wMinute, st.wSecond, s_con_color, pc_ded_con_width(), out + n, cap - n - 2);
            out[n++] = '\n';
            out[n] = '\0';
        }
        p += ll + (nl != NULL ? 1 : 0);
    }
    if (n > 0) {
        pc_ded_con_write(out);
    }
    free(out);
    free(line);
}
#endif

/* COMMAND RESPONSES. The console commands print their answers with pc_ded_printf(), which until now wrote them raw: not wrapped, a different style from the formatted notices (pc_dedicated_say). While a
 * command runs (s_ded_cmd_depth, set by pc_ded_execute) the interactive console routes those answers here instead, line by line, through the SAME formatter:
 *   "[TAG] text"                      a headline: formatted like every notice (time / LEVEL / subsystem, wrapped to the window)
 *   a plain line that says FAILED / REFUSED / UNSUPPORTED / usage / an error   the same, as a DEDICATED notice (so errors carry their LEVEL)
 *   an indented line (help entries, status fields, table rows)       a body line: indentation kept, long rows wrap under the description column
 *   any other plain line (data rows such as "0x2203 - Name - Category", page footers)   wrapped, no prefix, so a listing is not buried under a time stamp per row
 *   a blank line                                                     kept
 * Redirected output never comes here (it is the plain text the tests read). */
static int s_ded_cmd_depth = 0;

#ifdef _WIN32
static int pc_ded_response_is_notice(const char* line) {
    if (line[0] == '[') {
        char t[16];
        const char* e = strchr(line, ']');
        if (e != NULL && e - line - 1 > 0 && e - line - 1 < 15) {
            memcpy(t, line + 1, (size_t)(e - line - 1));
            t[e - line - 1] = '\0';
            return 1;
        }
    }
    return strstr(line, "FAILED") != NULL || strstr(line, "REFUSED") != NULL || strstr(line, "UNSUPPORTED") != NULL || strncmp(line, "usage", 5) == 0 || strstr(line, "usage:") != NULL ||
           strstr(line, "Invalid") != NULL || strstr(line, "invalid") != NULL;
}

static void pc_ded_con_write_response(const char* text) {
    size_t tl = strlen(text);
    size_t cap = tl * 4 + 2048;
    char* out = (char*)malloc(cap);
    char* line = (char*)malloc(tl + 1);
    char* row = (char*)malloc(tl * 3 + 1024);
    SYSTEMTIME st;
    const char* p = text;
    size_t n = 0;
    int width = pc_ded_con_width();
    if (out == NULL || line == NULL || row == NULL) {
        free(out);
        free(line);
        free(row);
        return;
    }
    out[0] = '\0';
    GetLocalTime(&st);
    while (*p != '\0') {
        const char* nl = strchr(p, '\n');
        size_t full = nl != NULL ? (size_t)(nl - p) : strlen(p);
        size_t ll = full;
        size_t rn;
        memcpy(line, p, ll);
        line[ll] = '\0';
        if (ll > 0 && line[ll - 1] == '\r') {
            line[--ll] = '\0';
        }
        if (nl == NULL && ll > 0 && text[tl - 1] != '\n') {
            /* a fragment without its newline (a prompt-style text): written as it is */
            memcpy(out + n, line, ll);
            n += ll;
            out[n] = '\0';
            break;
        }
        if (ll == 0) {
            out[n++] = '\n';
        } else if (line[0] == ' ' || line[0] == '\t') {
            rn = pc_dedc_wrap_body(line, width, row, tl * 3 + 1024);
            memcpy(out + n, row, rn);
            n += rn;
            out[n++] = '\n';
        } else if (pc_ded_response_is_notice(line)) {
            char tagged[PC_DEDC_LINE_MAX * 2 + 32];
            if (line[0] == '[') {
                snprintf(tagged, sizeof(tagged), "%s", line);
            } else {
                snprintf(tagged, sizeof(tagged), "[DEDICATED] %s", line);
            }
            rn = pc_dedc_format(tagged, st.wHour, st.wMinute, st.wSecond, s_con_color, width, row, tl * 3 + 1024);
            memcpy(out + n, row, rn);
            n += rn;
            out[n++] = '\n';
        } else {
            rn = pc_dedc_wrap_body(line, width, row, tl * 3 + 1024);
            memcpy(out + n, row, rn);
            n += rn;
            out[n++] = '\n';
        }
        out[n] = '\0';
        p += full + (nl != NULL ? (size_t)1 : 0);
        if (nl == NULL) {
            break;
        }
    }
    if (n > 0) {
        pc_ded_con_write(out);
    }
    free(out);
    free(line);
    free(row);
}
#endif

static FILE* pc_ded_stream(void) {
    return s_ded_out != NULL ? s_ded_out : stdout;
}

static void pc_ded_flush(void) {
    fflush(pc_ded_stream());
}

static int pc_ded_printf(const char* fmt, ...) {
    va_list ap;
    int n;
#ifdef _WIN32
    if (s_con_edit) {
        char* buf;
        va_start(ap, fmt);
        n = vsnprintf(NULL, 0, fmt, ap);
        va_end(ap);
        if (n < 0) {
            return n;
        }
        buf = (char*)malloc((size_t)n + 1);
        if (buf == NULL) {
            return -1;
        }
        va_start(ap, fmt);
        vsnprintf(buf, (size_t)n + 1, fmt, ap);
        va_end(ap);
        if (s_ded_cmd_depth > 0) {
            pc_ded_con_write_response(buf);
        } else {
            pc_ded_con_write(buf);
        }
        free(buf);
        return n;
    }
#endif
    va_start(ap, fmt);
    n = vfprintf(pc_ded_stream(), fmt, ap);
    va_end(ap);
    return n;
}

static int s_stdout_console = 0; /* pc_main.c did not redirect stdout to NUL */
void pc_dedicated_note_stdout_console(void) {
    s_stdout_console = 1;
}

int pc_dedicated_stdout_quiet(void) {
    return s_ded_interactive;
}

void pc_dedicated_say(const char* fmt, ...) {
    va_list ap;
    if (!g_pc_dedicated) {
        return;
    }
#ifdef _WIN32
    if (s_con_edit) {
        char msg[1200];
        char full[1300];
        va_start(ap, fmt);
        vsnprintf(msg, sizeof(msg), fmt, ap);
        va_end(ap);
        snprintf(full, sizeof(full), "[DEDICATED] %s", msg);
        pc_ded_con_write_log(full); /* interactive console: formatted (time / level / subsystem), drawn around the prompt line */
    } else
#endif
    {
        fputs("[DEDICATED] ", pc_ded_stream());
        va_start(ap, fmt);
        vfprintf(pc_ded_stream(), fmt, ap);
        va_end(ap);
        fputc('\n', pc_ded_stream());
        pc_ded_flush();
    }
    {
        char line[400];
        va_list ap2;
        va_start(ap2, fmt);
        vsnprintf(line, sizeof(line), fmt, ap2); /* the same notice goes to servers/<id>/logs/server.log */
        va_end(ap2);
        pc_server_log("%s", line);
    }
}

void pc_dedicated_say_detail(const char* fmt, ...) {
    char msg[1200];
    va_list ap;
    if (!g_pc_dedicated) {
        return;
    }
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
#ifdef _WIN32
    if (s_con_edit && !(g_pc_log_mask & PCL_NET) && !g_pc_verbose) {
        pc_server_log("%s", msg); /* the interactive console shows the friendly lines instead; the detail stays in server.log */
        return;
    }
#endif
    pc_dedicated_say("%s", msg);
}

/* FATAL notices: stderr, or the server console when stderr is going to NUL (interactive mode) so they are never lost. */
static void pc_ded_fatal(const char* msg_a, const char* arg) {
    FILE* f = s_ded_interactive ? pc_ded_stream() : stderr;
#ifdef _WIN32
    if (s_con_edit) {
        char msg[700];
        snprintf(msg, sizeof(msg), msg_a, arg);
        pc_ded_con_write_log(msg);
        return;
    }
#endif
    fprintf(f, msg_a, arg);
    fflush(f);
}

/* ------------------------------------------------------------------------------------------------------------------------------------- */
/* console attach (Windows)                                                                                                               */
/* ------------------------------------------------------------------------------------------------------------------------------------- */

#ifdef _WIN32
/* A std fd is "valid" when the CRT maps it to a real OS handle of a known type (console, pipe or file). A -mwindows exe started without
 * redirection has none of them (type UNKNOWN / no handle). */
static int pc_ded_fd_valid(int fd) {
    intptr_t h = _get_osfhandle(fd);
    if (h == -1 || h == -2 || h == 0) {
        return 0;
    }
    return GetFileType((HANDLE)h) != FILE_TYPE_UNKNOWN;
}
#endif

#ifdef _WIN32
/* 1 when this exe is the GUI subsystem (-mwindows, the normal build): a shell does NOT wait for such a process. */
static int pc_ded_is_gui_subsystem(void) {
    const IMAGE_DOS_HEADER* dos = (const IMAGE_DOS_HEADER*)GetModuleHandle(NULL);
    const IMAGE_NT_HEADERS* nt;
    if (dos == NULL || dos->e_magic != IMAGE_DOS_SIGNATURE) {
        return 0;
    }
    nt = (const IMAGE_NT_HEADERS*)((const char*)dos + dos->e_lfanew);
    return nt->Signature == IMAGE_NT_SIGNATURE && nt->OptionalHeader.Subsystem == IMAGE_SUBSYSTEM_WINDOWS_GUI;
}

/* "redirected": the inherited std handle is a pipe or a file (a test harness, `> log`, `| tee`), as opposed to a console / nothing. */
static int pc_ded_std_redirected(DWORD which) {
    HANDLE h = GetStdHandle(which);
    DWORD t;
    if (h == NULL || h == INVALID_HANDLE_VALUE) {
        return 0;
    }
    t = GetFileType(h);
    return t == FILE_TYPE_PIPE || t == FILE_TYPE_DISK;
}
#endif

/* Console setup, called as soon as --dedicated is parsed.
 *
 * ROOT CAUSE of "commands are interpreted by PowerShell / the input line is corrupted": the normal exe is a GUI-subsystem (-mwindows) process, so a shell
 * returns its prompt IMMEDIATELY; the server attached to the shell's console (AttachConsole(PARENT)), and the shell and the server then both read the
 * keyboard and both write to the same console: `status` went to PowerShell and the server's output tore through whatever was being typed.
 *
 * Fix: when a GUI-subsystem server is started interactively (stdin is not a pipe / file), it detaches from the shell's console and opens its OWN console
 * window (a one-line note is left in the shell). Only the server reads that window's keyboard. Redirected runs (tests, services, `> log`) are unchanged. */
void pc_dedicated_early_console(void) {
#ifdef _WIN32
    int attached;
    int in_ok;
    if (pc_ded_is_gui_subsystem() && !pc_ded_std_redirected(STD_INPUT_HANDLE)) {
        const int keep_out = pc_ded_std_redirected(STD_OUTPUT_HANDLE);
        const int keep_err = pc_ded_std_redirected(STD_ERROR_HANDLE);
        if (AttachConsole(ATTACH_PARENT_PROCESS)) {
            FreeConsole(); /* nothing is written to the launching shell any more: the "console ready" note goes to the NEW console below */
        }
        if (GetConsoleWindow() == NULL) {
            AllocConsole();
        }
        SetConsoleTitleA("Animal Crossing dedicated server console");
        if (!s_con_lock_ready) {
            InitializeCriticalSection(&s_con_lock);
            s_con_lock_ready = 1;
        }
        s_ded_conin = CreateFileA("CONIN$", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
        if (s_ded_conin == INVALID_HANDLE_VALUE) {
            s_ded_conin = NULL;
        }
        if (!keep_out) {
            freopen("CONOUT$", "w", stdout);
        }
        if (!keep_err) {
            freopen("CONOUT$", "w", stderr);
        }
        if (!keep_out) {
            s_ded_out = fopen("CONOUT$", "w"); /* the server console's own stream; stdout / stderr are sent to NUL later (pc_dedicated_stdout_quiet) */
            if (s_ded_out != NULL) {
                setvbuf(s_ded_out, NULL, _IONBF, 0);
                s_ded_interactive = 1;
                fputs("[DEDICATED] Server console ready.\nType help / status / players / guests / save / stop\n", s_ded_out);
            }
        }
        return;
    }
    attached = AttachConsole(ATTACH_PARENT_PROCESS) ? 1 : 0; /* fails harmlessly: no parent console / already attached (console build) */
    in_ok = pc_ded_fd_valid(0);
    int out_ok = pc_ded_fd_valid(1);
    int err_ok = pc_ded_fd_valid(2);
    if (!in_ok || !out_ok || !err_ok) {
        if (!attached && GetConsoleWindow() == NULL) {
            AllocConsole();
        }
        if (!in_ok) {
            freopen("CONIN$", "r", stdin);
        }
        if (!out_ok) {
            freopen("CONOUT$", "w", stdout);
        }
        if (!err_ok) {
            freopen("CONOUT$", "w", stderr);
        }
    }
#endif
}

/* ------------------------------------------------------------------------------------------------------------------------------------- */
/* stdin reader thread + bounded queue                                                                                                    */
/* ------------------------------------------------------------------------------------------------------------------------------------- */

typedef struct PCDedLine {
    char text[PC_DED_LINE_MAX + 1];
    int  truncated;
} PCDedLine;

static PCDedLine s_queue[PC_DED_QUEUE_MAX];
static int       s_q_head = 0;  /* next to pop  (guarded by the lock) */
static int       s_q_count = 0; /* guarded by the lock */
static int       s_q_dropped = 0; /* lines dropped because the queue was full (guarded by the lock) */
static volatile long s_q_count_hint = 0; /* lock-free "maybe non-empty" hint read by the main thread every frame */
static volatile long s_stdin_eof = 0;    /* the reader exited (EOF / read error) */
static int       s_stdin_eof_reported = 0;
static int       s_stdin_started = 0;

#ifdef _WIN32
static CRITICAL_SECTION s_q_lock;
#define PC_DED_LOCK()   EnterCriticalSection(&s_q_lock)
#define PC_DED_UNLOCK() LeaveCriticalSection(&s_q_lock)
#else
static pthread_mutex_t s_q_lock = PTHREAD_MUTEX_INITIALIZER;
#define PC_DED_LOCK()   pthread_mutex_lock(&s_q_lock)
#define PC_DED_UNLOCK() pthread_mutex_unlock(&s_q_lock)
#endif

/* ---- reader thread body: ONLY reads bytes, splits lines and enqueues them. It touches no game, save, network, SDL or logging function. ---- */
static void pc_ded_enqueue(const char* text, size_t len, int truncated) {
    PC_DED_LOCK();
    if (s_q_count >= PC_DED_QUEUE_MAX) {
        s_q_dropped++;
    } else {
        PCDedLine* slot = &s_queue[(s_q_head + s_q_count) % PC_DED_QUEUE_MAX];
        memcpy(slot->text, text, len);
        slot->text[len] = '\0';
        slot->truncated = truncated;
        s_q_count++;
        s_q_count_hint = s_q_count;
    }
    PC_DED_UNLOCK();
}

static void pc_ded_reader_loop(
#ifdef _WIN32
    HANDLE h
#else
    int fd
#endif
) {
    char buf[512];
    char line[PC_DED_LINE_MAX + 1];
    size_t len = 0;
    int truncated = 0;
    for (;;) {
        size_t i;
        size_t n;
#ifdef _WIN32
        DWORD got = 0;
        if (!ReadFile(h, buf, (DWORD)sizeof(buf), &got, NULL) || got == 0) {
            break; /* EOF, broken pipe or read error */
        }
        n = (size_t)got;
#else
        ssize_t got = read(fd, buf, sizeof(buf));
        if (got <= 0) {
            break;
        }
        n = (size_t)got;
#endif
        for (i = 0; i < n; i++) {
            char c = buf[i];
            if (c == '\n') {
                pc_ded_enqueue(line, len, truncated);
                len = 0;
                truncated = 0;
            } else if (c != '\r') {
                if (len < PC_DED_LINE_MAX) {
                    line[len++] = c;
                } else {
                    truncated = 1;
                }
            }
        }
    }
    if (len > 0) {
        pc_ded_enqueue(line, len, truncated); /* a final line without a newline */
    }
    s_stdin_eof = 1;
}

#ifdef _WIN32
static HANDLE s_stdin_handle = NULL;
static unsigned __stdcall pc_ded_reader_thread(void* arg) {
    (void)arg;
    pc_ded_reader_loop(s_stdin_handle);
    return 0;
}
#else
static int s_stdin_fd = 0;
static void* pc_ded_reader_thread(void* arg) {
    (void)arg;
    pc_ded_reader_loop(s_stdin_fd);
    return NULL;
}
#endif

#ifdef _WIN32
static void pc_ded_con_restore(void) {
    if (s_con_edit && s_ded_conin != NULL) {
        SetConsoleMode(s_ded_conin, s_con_in_old_mode);
    }
}

/* Takes the server console over: no console line input / echo (the editor does both), VT output. Any failure leaves the console exactly as it was (the old cooked line reader stays in use). */
static char s_con_edit_why[96] = "";

static int pc_ded_enable_editor(void) {
    HANDLE hi = s_ded_conin;
    HANDLE ho;
    DWORD mi = 0, mo = 0;
    if (!s_ded_interactive || s_ded_out == NULL || hi == NULL || !s_con_lock_ready) {
        return 0; /* not the interactive console: nothing to report */
    }
    /* the FILE opened on CONOUT$ is write-only; querying / changing the console mode needs a handle with read access too (error 5 otherwise). The mode belongs to the screen buffer, so it applies to the FILE as well. */
    ho = CreateFileA("CONOUT$", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    if (ho == INVALID_HANDLE_VALUE || !GetConsoleMode(hi, &mi) || !GetConsoleMode(ho, &mo)) {
        snprintf(s_con_edit_why, sizeof(s_con_edit_why), "console modes unreadable (error %lu)", (unsigned long)GetLastError());
        return 0;
    }
    if (!SetConsoleMode(ho, mo | ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING)) {
        snprintf(s_con_edit_why, sizeof(s_con_edit_why), "this console has no virtual-terminal output (error %lu)", (unsigned long)GetLastError());
        return 0;
    }
    if (!SetConsoleMode(hi, (mi & ~(DWORD)(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT)) | ENABLE_PROCESSED_INPUT | ENABLE_WINDOW_INPUT)) {
        snprintf(s_con_edit_why, sizeof(s_con_edit_why), "console input mode could not be changed (error %lu)", (unsigned long)GetLastError());
        SetConsoleMode(ho, mo);
        return 0;
    }
    s_con_in_old_mode = mi;
    s_con_out_h = ho;
    pc_dedc_init(&s_con_ed);
    s_con_color = getenv("NO_COLOR") == NULL;
    s_con_edit = 1;
    atexit(pc_ded_con_restore);
    return 1;
}

static void pc_ded_con_redraw(void) {
    char out[1300];
    size_t n;
    EnterCriticalSection(&s_con_lock);
    if (!s_con_partial) {
        n = pc_dedc_render(&s_con_ed, PC_DED_PROMPT, pc_ded_con_width(), out, sizeof(out));
        pc_ded_con_put_locked(out, n);
    }
    LeaveCriticalSection(&s_con_lock);
}

static void pc_ded_enqueue(const char* text, size_t len, int truncated);

/* the keyboard reader of the editor: keys -> editor -> redraw; ENTER hands the line over once (the same bounded queue the stdin reader uses) */
static unsigned __stdcall pc_ded_editor_thread(void* arg) {
    INPUT_RECORD rec[16];
    DWORD got, i;
    (void)arg;
    pc_ded_con_redraw();
    for (;;) {
        if (!ReadConsoleInputW(s_ded_conin, rec, 16, &got) || got == 0) {
            break;
        }
        for (i = 0; i < got; i++) {
            const KEY_EVENT_RECORD* k;
            int key = 0;
            int rep, r;
            if (rec[i].EventType == WINDOW_BUFFER_SIZE_EVENT) {
                pc_ded_con_redraw(); /* the window was resized: the prompt line is re-fitted to the new width (the width is read at every write) */
                continue;
            }
            if (rec[i].EventType != KEY_EVENT || !rec[i].Event.KeyEvent.bKeyDown) {
                continue;
            }
            k = &rec[i].Event.KeyEvent;
            switch (k->wVirtualKeyCode) {
                case VK_BACK: key = PC_DEDC_KEY_BACKSPACE; break;
                case VK_DELETE: key = PC_DEDC_KEY_DELETE; break;
                case VK_LEFT: key = PC_DEDC_KEY_LEFT; break;
                case VK_RIGHT: key = PC_DEDC_KEY_RIGHT; break;
                case VK_HOME: key = PC_DEDC_KEY_HOME; break;
                case VK_END: key = PC_DEDC_KEY_END; break;
                case VK_RETURN: key = PC_DEDC_KEY_ENTER; break;
                case VK_ESCAPE: key = PC_DEDC_KEY_CLEAR; break;
                default:
                    if (k->uChar.UnicodeChar == 0x15) {
                        key = PC_DEDC_KEY_CLEAR; /* Ctrl+U */
                    } else if (k->uChar.UnicodeChar >= 32 && k->uChar.UnicodeChar <= 126) {
                        key = (int)k->uChar.UnicodeChar;
                    }
                    break;
            }
            if (key == 0) {
                continue;
            }
            rep = k->wRepeatCount > 0 ? (int)k->wRepeatCount : 1;
            for (r = 0; r < rep; r++) {
                char line[PC_DEDC_LINE_MAX + 1];
                char draw[1300];
                int done;
                EnterCriticalSection(&s_con_lock);
                done = pc_dedc_key(&s_con_ed, key, line, sizeof(line));
                if (done) {
                    char echo[PC_DEDC_LINE_MAX + 16];
                    size_t en = pc_dedc_commit_echo(PC_DED_PROMPT, line, echo, sizeof(echo));
                    size_t dn = pc_dedc_render(&s_con_ed, PC_DED_PROMPT, pc_ded_con_width(), draw, sizeof(draw));
                    pc_ded_con_put_locked(echo, en);
                    pc_ded_con_put_locked(draw, dn);
                    s_con_partial = 0;
                } else if (!s_con_partial) {
                    size_t dn = pc_dedc_render(&s_con_ed, PC_DED_PROMPT, pc_ded_con_width(), draw, sizeof(draw));
                    pc_ded_con_put_locked(draw, dn);
                }
                LeaveCriticalSection(&s_con_lock);
                if (done) {
                    pc_ded_enqueue(line, strlen(line), 0);
                }
            }
        }
    }
    s_stdin_eof = 1;
    return 0;
}

/* With -debug* / --verbose the game's own stdout / stderr stay on the console. They are routed through a pipe into the SAME locked writer, so those lines cannot tear the prompt line either. */
static HANDLE s_pump_read = NULL;
static unsigned __stdcall pc_ded_pump_thread(void* arg) {
    char buf[1024];
    char line[PC_DEDC_LINE_MAX * 2 + 8];
    size_t len = 0;
    DWORD got;
    (void)arg;
    for (;;) {
        DWORD avail = 0;
        DWORD i;
        /* stdout is unbuffered: one printf can arrive as several pieces. A partial line is held until its newline comes; only when nothing more arrives for 50 ms (a prompt-style text without a
         * newline) is it shown as it is, so a line is never split in the middle. */
        if (len > 0 && PeekNamedPipe(s_pump_read, NULL, 0, NULL, &avail, NULL) && avail == 0) {
            Sleep(50);
            if (PeekNamedPipe(s_pump_read, NULL, 0, NULL, &avail, NULL) && avail == 0) {
                line[len] = '\0';
                pc_ded_con_write_log(line);
                len = 0;
            }
        }
        if (!ReadFile(s_pump_read, buf, sizeof(buf), &got, NULL) || got == 0) {
            break;
        }
        for (i = 0; i < got; i++) {
            char c = buf[i];
            if (c == '\n') {
                line[len] = '\0';
                pc_ded_con_write_log(len > 0 ? line : " ");
                len = 0;
            } else if (c != '\r' && len < sizeof(line) - 2) {
                line[len++] = c;
            }
        }
    }
    return 0;
}

static void pc_ded_install_log_pump(void) {
    HANDLE r = NULL, w = NULL;
    int fd;
    uintptr_t th;
    /* the CRT descriptors of the stdout / stderr FILEs (a GUI-subsystem exe that reopened them on CONOUT$ does not necessarily have them on fd 1 / 2) */
    int fdo = _fileno(stdout);
    int fde = _fileno(stderr);
    intptr_t so = fdo >= 0 ? _get_osfhandle(fdo) : -1;
    /* pc_main.c sends stdout / stderr to NUL (also a "character device", and not told apart by its type) unless --verbose / a -debug* flag / PC_LOG / profiling asked for them: only then does
     * game output reach the console and need routing. Routing a NUL stdout would turn the game's thousands of discarded debug lines into console output. */
    if (!s_stdout_console) {
        return;
    }
    if (so == -1 || so == -2 || GetFileType((HANDLE)so) != FILE_TYPE_CHAR) {
        return; /* stdout is NUL / a file / nothing: nothing reaches the console */
    }
    if (!CreatePipe(&r, &w, NULL, 1 << 16)) {
        pc_dedicated_say("console: debug output capture unavailable (CreatePipe error %lu); debug lines may interrupt typing", (unsigned long)GetLastError());
        return;
    }
    fd = _open_osfhandle((intptr_t)w, _O_BINARY);
    if (fd < 0) {
        CloseHandle(r);
        CloseHandle(w);
        pc_dedicated_say("console: debug output capture unavailable (_open_osfhandle); debug lines may interrupt typing");
        return;
    }
    fflush(stdout);
    fflush(stderr);
    if (_dup2(fd, fdo) != 0 || (fde >= 0 && fde != fdo && _dup2(fd, fde) != 0)) {
        pc_dedicated_say("console: debug output capture unavailable (_dup2); debug lines may interrupt typing");
        return;
    }
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    s_pump_read = r;
    th = _beginthreadex(NULL, 0, pc_ded_pump_thread, NULL, 0, NULL);
    if (th != 0) {
        CloseHandle((HANDLE)th);
    }
}
#endif

/* Starts the reader. DETACHED on purpose: it blocks in a raw read and is never joined, so process exit never waits for it. */
static int pc_ded_start_stdin_thread(void) {
#ifdef _WIN32
    intptr_t h = _get_osfhandle(0);
    uintptr_t th;
    if (s_ded_conin != NULL) {
        h = (intptr_t)s_ded_conin; /* interactive mode: read the server console's own input buffer */
    } else if (!pc_ded_fd_valid(0)) {
        return 0;
    }
    s_stdin_handle = (HANDLE)h;
    InitializeCriticalSection(&s_q_lock);
    if (s_ded_conin != NULL && pc_ded_enable_editor()) {
        th = _beginthreadex(NULL, 0, pc_ded_editor_thread, NULL, 0, NULL);
        if (th == 0) {
            pc_ded_con_restore();
            s_con_edit = 0;
            return 0;
        }
        CloseHandle((HANDLE)th);
        pc_ded_install_log_pump();
        return 1;
    }
    th = _beginthreadex(NULL, 0, pc_ded_reader_thread, NULL, 0, NULL);
    if (th == 0) {
        return 0;
    }
    CloseHandle((HANDLE)th); /* detach */
    return 1;
#else
    pthread_t t;
    s_stdin_fd = 0;
    if (pthread_create(&t, NULL, pc_ded_reader_thread, NULL) != 0) {
        return 0;
    }
    pthread_detach(t);
    return 1;
#endif
}

void pc_dedicated_startup(uint16_t port) {
    if (!g_pc_dedicated) {
        return;
    }
    s_port = port;
    pc_dedicated_say("dedicated server starting: role=host port=%u observer=on (--host-observer init reused) console=on", (unsigned)port);
    PC_LOG(PCL_GENERAL, "dedicated mode: port=%u observer=on console=on\n", (unsigned)port);
    s_stdin_started = pc_ded_start_stdin_thread();
#ifdef _WIN32
    if (s_ded_interactive && !s_con_edit) {
        pc_dedicated_say("console: the line editor is not available (%s); typing may be interrupted by log lines", s_con_edit_why[0] ? s_con_edit_why : "the editor could not start");
    }
#endif
    if (s_stdin_started) {
        pc_dedicated_say("console input: reading commands from stdin (type help)");
    } else {
        pc_dedicated_say("console input: unavailable (no valid stdin); the server runs without commands");
    }
}

/* Pops one queued console line. 1 = a line in *out, 0 = none queued. */
static int pc_ded_pop_line(PCDedLine* out) {
    int got = 0;
    PC_DED_LOCK();
    if (s_q_count > 0) {
        *out = s_queue[s_q_head];
        s_q_head = (s_q_head + 1) % PC_DED_QUEUE_MAX;
        s_q_count--;
        s_q_count_hint = s_q_count;
        got = 1;
    }
    PC_DED_UNLOCK();
    return got;
}

/* One console line, read SYNCHRONOUSLY from the server console's own input (interactive mode: the CONIN$ buffer of the window this process opened; otherwise the redirected stdin), before the
 * stdin reader thread exists. Byte-wise on purpose: nothing beyond the newline is consumed, so the reader thread started later sees the rest. 1 = a line (no newline) in buf, 0 = EOF / error. */
int pc_dedicated_read_line_blocking(char* buf, size_t cap) {
    size_t n = 0;
    char c;
    if (buf == NULL || cap < 2) {
        return 0;
    }
#ifdef _WIN32
    {
        HANDLE h = s_ded_conin != NULL ? s_ded_conin : (HANDLE)_get_osfhandle(0);
        if (h == NULL || h == INVALID_HANDLE_VALUE) {
            return 0;
        }
        for (;;) {
            DWORD got = 0;
            if (!ReadFile(h, &c, 1, &got, NULL) || got == 0) {
                if (n == 0) {
                    return 0;
                }
                break;
            }
            if (c == '\n') {
                break;
            }
            if (c != '\r' && n + 1 < cap) {
                buf[n++] = c;
            }
        }
    }
#else
    for (;;) {
        ssize_t got = read(0, &c, 1);
        if (got <= 0) {
            if (n == 0) {
                return 0;
            }
            break;
        }
        if (c == '\n') {
            break;
        }
        if (c != '\r' && n + 1 < cap) {
            buf[n++] = c;
        }
    }
#endif
    buf[n] = '\0';
    return 1;
}

/* "> " for the plain stdin prompts; the interactive editor draws its own prompt, so nothing is added there */
static const char* pc_ded_prompt_suffix(void) {
#ifdef _WIN32
    if (s_con_edit) {
        return "";
    }
#endif
    return PC_DED_PROMPT;
}

void pc_dedicated_prompt_town_name(void) {
    uint8_t codes[8];
    char err[96], text[24];
    PCDedLine ln;
    if (!g_pc_dedicated) {
        return;
    }
    pc_ded_printf("\n========================================\n Animal Crossing Dedicated Host\n========================================\n\n");
    pc_ded_printf("No town exists for this server.\n\n");
    for (;;) {
        pc_ded_printf("Enter town name (1-8 characters):\n%s", pc_ded_prompt_suffix());
        pc_ded_flush();
        pc_server_log("waiting for the town name on the console");
        while (!pc_ded_pop_line(&ln)) {
            if (!s_stdin_started || s_stdin_eof) {
                /* the reader may have queued a last line before it saw EOF: look once more, then give up */
                if (pc_ded_pop_line(&ln)) {
                    break;
                }
                pc_server_town_name_codes(codes);
                pc_server_set_town_name(codes);
                pc_server_town_name_text(codes, text);
                pc_ded_printf("\n[DEDICATED] console input is closed / unavailable: using the default town name \"%s\".\n", text);
                pc_server_log("no console input: default town name \"%s\"", text);
                pc_ded_flush();
                return;
            }
#ifdef _WIN32
            Sleep(20);
#else
            usleep(20000);
#endif
        }
        if (!ln.truncated && pc_server_town_name_encode(ln.text, codes, err, sizeof(err))) {
            pc_server_set_town_name(codes);
            pc_server_town_name_text(codes, text);
            pc_ded_printf("Town name set to \"%s\". Generating the town...\n", text);
            pc_server_log("town name chosen: \"%s\"", text);
            pc_ded_flush();
            return;
        }
        pc_ded_printf("Invalid town name. Please enter 1-8 valid characters.\n\n");
    }
}

/* ------------------------------------------------------------------------------------------------------------------------------------- */
/* audio / window / platform                                                                                                              */
/* ------------------------------------------------------------------------------------------------------------------------------------- */

void pc_dedicated_pre_sdl_init(void) {
    if (!g_pc_dedicated) {
        return;
    }
    /* SDL_GetHint() prefers the environment variable over a normal-priority hint, so set both: the env var (overwrite) AND an OVERRIDE hint. This runs
     * BEFORE SDL_Init, which is when SDL's audio subsystem reads the driver choice. */
    SDL_setenv("SDL_AUDIODRIVER", "dummy", 1);
    SDL_SetHintWithPriority(SDL_HINT_AUDIODRIVER, "dummy", SDL_HINT_OVERRIDE);
#ifdef _WIN32
    {
        /* A hidden window makes Windows 11 free to ignore timeBeginPeriod() (power throttling): ask for the timer resolution requests of this process
         * to stay honoured, so the 60 Hz sleep pacing keeps its 1 ms granularity. Best effort (older Windows lacks the API: ignored). */
        typedef struct { ULONG Version; ULONG ControlMask; ULONG StateMask; } PcDedThrottle;
        typedef BOOL (WINAPI* SetProcInfoFn)(HANDLE, int, LPVOID, DWORD);
        HMODULE k32 = GetModuleHandleA("kernel32.dll");
        SetProcInfoFn fn = k32 ? (SetProcInfoFn)(void*)GetProcAddress(k32, "SetProcessInformation") : NULL;
        if (fn != NULL) {
            PcDedThrottle st;
            st.Version = 1;                 /* PROCESS_POWER_THROTTLING_CURRENT_VERSION */
            st.ControlMask = 0x1u | 0x4u;   /* EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION */
            st.StateMask = 0;               /* control the policy, state OFF = never throttle / never ignore */
            fn(GetCurrentProcess(), 4 /* ProcessPowerThrottling */, &st, (DWORD)sizeof(st));
        }
    }
#endif
}

int pc_dedicated_post_sdl_init(void) {
    const char* drv;
    if (!g_pc_dedicated) {
        return 1;
    }
    drv = SDL_GetCurrentAudioDriver();
    if (drv == NULL || strcmp(drv, "dummy") != 0) {
        pc_ded_fatal("[DEDICATED] FATAL: the SDL dummy audio driver is not the one that opened (driver=%s); refusing to run a dedicated server with "
                     "a real or stalled audio device\n", drv ? drv : "(none)");
        return 0;
    }
    snprintf(s_audio_driver, sizeof(s_audio_driver), "%s", drv);
    s_audio_ok = 1;
    return 1;
}

void pc_dedicated_audio_opened(int ok, int freq, int channels, int samples) {
    if (!g_pc_dedicated) {
        return;
    }
    if (!ok) {
        pc_ded_fatal("[DEDICATED] FATAL: the dummy audio device could not be opened (%s); a stalled audio producer could block fades and message "
                     "waits, so a dedicated server refuses to start\n", SDL_GetError());
        exit(1);
    }
    pc_dedicated_say("audio device opened: driver=%s freq=%d ch=%d samples=%d", s_audio_driver, freq, channels, samples);
    PC_LOG(PCL_GENERAL, "dedicated audio driver '%s' opened (freq=%d ch=%d samples=%d)\n", s_audio_driver, freq, channels, samples);
}

void pc_dedicated_platform_summary(void) {
    Uint32 wf;
    if (!g_pc_dedicated) {
        return;
    }
    s_start_ticks = SDL_GetTicks();
    wf = g_pc_window != NULL ? SDL_GetWindowFlags(g_pc_window) : 0;
    pc_dedicated_say("platform: window=%s (GL 3.3 context kept) vsync=%s audio_driver=%s render=skipped "
                     "(emu64_taskstart, pc_gx_begin_frame, pc_gx_draw_pending, buffer swap; JW_EndFrame/VIWaitForRetrace kept)",
                     (wf & SDL_WINDOW_HIDDEN) ? "hidden" : "VISIBLE", SDL_GL_GetSwapInterval() == 0 ? "off" : "ON", s_audio_ok ? s_audio_driver : "?");
    PC_LOG(PCL_GENERAL, "dedicated platform: window_hidden=%d swap_interval=%d audio_driver=%s\n", (wf & SDL_WINDOW_HIDDEN) ? 1 : 0, SDL_GL_GetSwapInterval(),
           s_audio_ok ? s_audio_driver : "?");
}

/* ------------------------------------------------------------------------------------------------------------------------------------- */
/* notices                                                                                                                                */
/* ------------------------------------------------------------------------------------------------------------------------------------- */

void pc_dedicated_notify_save_result(int ok) {
    if (!g_pc_dedicated) {
        return;
    }
    s_last_save_ok = ok ? 1 : 0;
    s_last_save_ticks = SDL_GetTicks();
    pc_dedicated_say("save result: %s (frame %lu)", ok ? "OK" : "FAILED", (unsigned long)pc_frame_counter);
}

void pc_dedicated_notify_shutdown(const char* what) {
    if (!g_pc_dedicated) {
        return;
    }
    pc_dedicated_say("shutdown: %s", what);
}

/* ------------------------------------------------------------------------------------------------------------------------------------- */
/* commands (main thread only)                                                                                                            */
/* ------------------------------------------------------------------------------------------------------------------------------------- */

static void pc_ded_cmd_help(void) {
    pc_ded_printf("[DEDICATED] commands (case-insensitive):\n"
           "  help     list the commands\n"
           "  status   server status: observer, network role/port, world+save readiness, uptime, peers, last save\n"
           "  players  connected players: peer slot, link, class (RESIDENT idx / GUEST slot), name, puppet, idle ms\n"
           "  save     request an authoritative save at the next safe point (prints 'save: OK', 'save: FAILED (...)' or 'save: refused: ...')\n"
           "  guests   guest table: slot, name, home town, host town, confirmed, rev, bound peer, recovery (never the token)\n"
           "  members  town memberships: residents and guests (kind, slot, confirmed)\n"
           "  guest-remove <slot|name> confirm        remove a guest and its character (guests.dat is backed up first; refused while the guest is connected)\n"
           "  guest-reset-token <slot|name> confirm   token lost: the next claim of that guest's key gets a NEW token for the SAME character (10 min, once; backed up)\n"
           "  promote <guest> <slot|auto> <house|auto> confirm   promote an OFFLINE guest to a resident of this town (free slot + free house; guests.dat / members.dat / records.dat are backed up first; see docs)\n"
           "  residents   resident credentials: slot, name, credential state, confirmed, armed, connected (never the token); policy = resident_tokens off|tofu|required\n"
           "  resident-reset <slot|name> confirm      delete a resident's credential (members.dat is backed up first; refused while the resident is connected)\n"
           "  resident-arm <slot|name> confirm        under resident_tokens=required: allow ONE credential mint for a resident without one (memory only, 10 min, one use)\n"
           "  finditem <text> [page]   search item names (case-insensitive); lines: 0xID - Name - Category, 15 per page\n"
           "  iteminfo <id>            name, category and whether it can be given (id = 0x2203 or decimal)\n"
           "  items <furniture|tools|clothing|wallpaper|carpet|miscellaneous|all> [page]   list items by category (categories are incomplete; see docs)\n"
           "  give <player|peer N|\"name\"> <id> [qty]   put qty (1-15, default 1) copies in the player's FREE pockets, all-or-nothing; player = resident or guest name (see `players`, `guests`)\n"
           "  where [<player>|peer N]   last reported position (x, y, z, acre) of one / every connected player (the server has no player of its own)\n"
           "  net status|peers|resync   network role / transport counters / per-peer state; resync is reported unsupported (no general resync exists)\n"
           "  time get   the game clock (date, time, weekday)\n"
           "  event list|status [name]   today's event schedule from the game's own tables (scheduled / active / running); start|stop|reset are refused with the reason\n"
           "  wildlife list|info <id>   the host's authoritative wildlife records; wildlife spawn fish <species> <x> <z> / despawn <id> need AC_TEST_HOOKS=1\n"
           "  test status|help   whether the test hooks are enabled and what they gate\n"
           "  shop-level-boost [confirm]   add the sales still missing to the NEXT shop tier once (mSP_PlusSales); the upgrade itself follows the vanilla 2-day schedule\n"
           "  tp / bell   unsupported on the server (no local player; no host bell admin API) -- see docs/console-commands.md\n"
           "  stop     graceful shutdown (final save, close networking and platform, exit 0); aliases: quit, exit\n");
    pc_ded_flush();
}

static const char* pc_ded_role_name(void) {
    switch (pc_net_game_role()) {
        case PC_NETGAME_ROLE_HOST: return "host";
        case PC_NETGAME_ROLE_CLIENT: return "client";
        default: return "none";
    }
}

static const char* pc_ded_link_name(int link) {
    switch (link) {
        case PC_NETGAME_LINK_HANDSHAKE: return "HANDSHAKE";
        case PC_NETGAME_LINK_READY: return "READY";
        case PC_NETGAME_LINK_CONNECTING: return "CONNECTING";
        default: return "DISCONNECTED";
    }
}

static void pc_ded_cmd_status(void) {
    Uint32 up_ms = SDL_GetTicks() - s_start_ticks;
    unsigned long up_s = (unsigned long)(up_ms / 1000u);
    unsigned long frames = (unsigned long)pc_frame_counter;
    int transport = 0, ready = 0, i, n = pc_net_game_dedicated_peer_slots();
    for (i = 0; i < n; i++) {
        PCNetGameDedicatedPeerInfo pi;
        if (pc_net_game_dedicated_peer_info(i, &pi)) {
            transport++;
            if (pi.link == PC_NETGAME_LINK_READY) {
                ready++;
            }
        }
    }
    pc_ded_printf("[DEDICATED] status\n");
    pc_ded_printf("  dedicated: yes\n");
    pc_ded_printf("  server: %s (%s), town dir %s\n", pc_server_id(), pc_server_dir(), pc_server_town_dir());
    pc_ded_printf("  observer: active=%s ready=%s\n", pc_host_observer_active() ? "yes" : "no", pc_host_observer_ready() ? "yes" : "no");
    pc_ded_printf("  network: role=%s listening_port=%u\n", pc_ded_role_name(), (unsigned)s_port);
    pc_ded_printf("  world ready: %s\n", pc_net_game_dedicated_world_ready() ? "yes" : "no");
    pc_ded_printf("  save ready: %s\n", pcfa_save_ready() ? "yes" : "no");
    pc_ded_printf("  uptime: %lus (%luh %02lum %02lus), frames=%lu, avg_fps=%.1f\n", up_s, up_s / 3600ul, (up_s / 60ul) % 60ul, up_s % 60ul, frames,
           up_ms > 0 ? (double)frames * 1000.0 / (double)up_ms : 0.0);
    if (g_frame_limiter > 0) {
        pc_ded_printf("  frame limit: %lu Hz\n", (unsigned long)g_frame_limiter);
    } else {
        pc_ded_printf("  frame limit: none (uncapped)\n");
    }
    pc_ded_printf("  peers: connected=%d ready=%d\n", transport, ready);
    {
        PCNetGameDedicatedGuestAdmission ga;
        if (pc_net_game_dedicated_guest_admission(&ga)) {
            pc_ded_printf("  guests: bound=%d max_guests=%d (configured), admissible now %d (limited by transport capacity), available %d\n", ga.bound, ga.configured, ga.effective,
                   ga.effective - ga.bound > 0 ? ga.effective - ga.bound : 0);
            pc_ded_printf("  guest store: %d guest(s) stored, %d entries allocated (memory budget %d), new guests %s\n", ga.stored, ga.store_cap, ga.store_budget,
                   ga.untrusted ? "REFUSED (host is UNTRUSTED: a guest file is unreadable / duplicated)" : (ga.allow_new ? "accepted" : "REFUSED (allow_new_guests = 0)"));
            pc_ded_printf("  a guest is refused when: guest admission full (bound >= max_guests) | transport full (no free peer) | token invalid | host untrusted | store budget used up\n");
        }
    }
    {
        PCNetGameDedicatedScale sc;
        if (pc_net_game_dedicated_scale(&sc)) {
            pc_ded_printf("  interest (%s): MOVE relayed near/mid/far/apart = %u/%u/%u/%u, thinned = %u/%u/%u/%u\n", sc.interest_on ? "on" : "OFF: every sample to everyone", sc.move_relayed[0], sc.move_relayed[1],
                   sc.move_relayed[2], sc.move_relayed[3], sc.move_thinned[0], sc.move_thinned[1], sc.move_thinned[2], sc.move_thinned[3]);
            pc_ded_printf("  roster: owed=%d sent=%u departures=%u blocked(window full)=%u awaiting-data=%u | reliable backlog now(max)=%d peak=%d of %d\n", sc.roster_owed, sc.roster_sent, sc.roster_gone_sent,
                   sc.roster_blocked, sc.roster_unavail, sc.backlog_now_max, sc.max_backlog, PC_NET_RELIABLE_WINDOW);
            pc_ded_printf("  puppets: slots %d (peak %d, alloc failures %d), tracked %d, actors %d, waiting for an actor %d (blocked by actor headroom %d, create failures %u); scene actors peak %d of %d\n", sc.puppet_slots,
                   sc.puppet_slots_peak, sc.puppet_slot_failures, sc.puppets_tracked, sc.puppet_actors, sc.puppet_actors_pending, sc.puppet_actors_blocked, sc.puppet_actor_create_failed, sc.actor_peak, sc.actor_max);
            pc_ded_printf("  collision: puppet colliders armed %d, shared collider table peak %d of %d, refused (table full) %d\n", sc.collide_armed, sc.collider_peak, sc.collider_table, sc.failed_setoc);
        }
    }
    {
        int pu = 0, pt = 0, rr = 0;
        PCNetStats ns;
        if (pc_net_game_dedicated_capacity(&pu, &pt, &rr)) {
            pc_ded_printf("  capacity: transport slots %d/%d used, %d held for absent residents, %d free for guests\n", pu, pt, rr, pt - pu - rr > 0 ? pt - pu - rr : 0);
        }
        pc_net_get_stats(&ns);
        pc_ded_printf("  transport: reliable sent=%lu retransmits=%lu received=%lu out_of_window=%lu | events dropped: unreliable=%lu control=%lu | budget disconnects=%lu\n",
               (unsigned long)ns.rdata_sent, (unsigned long)ns.rdata_retransmits, (unsigned long)ns.rdata_received, (unsigned long)ns.rdata_out_of_window,
               (unsigned long)ns.events_dropped_unreliable, (unsigned long)ns.events_dropped_control, (unsigned long)ns.reliable_budget_disconnects);
    }
    if (s_last_save_ok < 0) {
        pc_ded_printf("  last save: none yet\n");
    } else {
        pc_ded_printf("  last save: %s %lus ago\n", s_last_save_ok ? "OK" : "FAILED", (unsigned long)((SDL_GetTicks() - s_last_save_ticks) / 1000u));
    }
    pc_ded_printf("  audio: driver=%s ring_fill=%d consumed_samples=%d\n", s_audio_ok ? s_audio_driver : "?", pc_audio_get_buffer_fill(), pc_audio_consumed_samples());
    pc_ded_flush();
}

static void pc_ded_cmd_players(void) {
    int i, n = pc_net_game_dedicated_peer_slots(), listed = 0;
    for (i = 0; i < n; i++) {
        PCNetGameDedicatedPeerInfo pi;
        if (pc_net_game_dedicated_peer_info(i, &pi)) {
            listed++;
        }
    }
    if (listed == 0) {
        pc_ded_printf("[DEDICATED] no players connected\n");
        pc_ded_flush();
        return;
    }
    pc_ded_printf("[DEDICATED] players: %d\n", listed);
    for (i = 0; i < n; i++) {
        PCNetGameDedicatedPeerInfo pi;
        char cls[32];
        if (!pc_net_game_dedicated_peer_info(i, &pi)) {
            continue;
        }
        if (!pi.bound) {
            snprintf(cls, sizeof(cls), "unbound");
        } else if (pi.cls == 1) {
            snprintf(cls, sizeof(cls), "GUEST slot %d", pi.index);
        } else {
            snprintf(cls, sizeof(cls), "RESIDENT idx %d", pi.index);
        }
        pc_ded_printf("  peer %d: link=%s class=%s name=\"%s\" puppet=%s idle=%dms\n", pi.peer, pc_ded_link_name(pi.link), cls, pi.name,
               pi.puppet == 2 ? "live" : pi.puppet == 1 ? "pending" : "none", pi.idle_ms);
    }
    pc_ded_flush();
}

static int pc_ded_stricmp(const char* a, const char* b) {
    for (; *a != '\0' && *b != '\0'; a++, b++) {
        if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) {
            return 1;
        }
    }
    return *a != *b;
}

/* G6.2: `guests` (list, never the token) and the two guarded operator commands. */
static void pc_ded_cmd_guests(void) {
    int g, n = 0;
    const int slots = pc_net_game_dedicated_guest_slots();
    for (g = 0; g < slots; g++) {
        PCNetGameDedicatedGuestInfo gi;
        if (!pc_net_game_dedicated_guest_info(g, &gi)) {
            continue;
        }
        if (n == 0) {
            pc_ded_printf("[DEDICATED] guests (host guest table, all towns; %s):\n", gi.untrusted ? "the guest store is UNTRUSTED" : "tokens are never printed");
        }
        n++;
        pc_ded_printf("  slot %d: name=\"%s\" home_town=\"%s\" host_town=\"%s\"%s confirmed=%s rev=%u bound=%s%s\n", gi.slot, gi.name, gi.home_town, gi.town,
               gi.active ? "" : " (other town: inactive)", gi.confirmed ? "yes" : "no", gi.rev,
               gi.bound_peer >= 0 ? "yes" : "no", gi.recovery ? " RECOVERY-ARMED" : "");
        if (gi.bound_peer >= 0) {
            pc_ded_printf("    (connected on peer %d)\n", gi.bound_peer);
        }
    }
    if (n == 0) {
        pc_ded_printf("[DEDICATED] guests: none stored (or this server is not a ready host)\n");
    }
    pc_ded_flush();
}

/* M-E: `residents` (list, never the token) and the two guarded operator commands. */
static void pc_ded_cmd_residents(void) {
    int s, n = 0;
    for (s = 0; s < 4; s++) {
        PCNetGameDedicatedResidentInfo ri;
        if (!pc_net_game_dedicated_resident_info(s, &ri)) {
            continue;
        }
        if (n == 0) {
            pc_ded_printf("[DEDICATED] residents (resident_tokens=%s%s; tokens are never printed):\n", ri.policy == 2 ? "required" : ri.policy == 1 ? "tofu" : "off",
                          ri.untrusted ? ", members.dat is UNTRUSTED" : "");
        }
        n++;
        pc_ded_printf("  slot %d: name=\"%s\" credential=%s confirmed=%s armed=%s connected=%s\n", ri.slot, ri.name, ri.untrusted ? "unknown (UNTRUSTED)" : ri.has_cred ? "yes" : "no",
                      ri.has_cred ? (ri.confirmed ? "yes" : "no") : "-", ri.armed ? "yes" : "no", ri.bound_peer >= 0 ? "yes" : "no");
    }
    if (n == 0) {
        pc_ded_printf("[DEDICATED] residents: none (or this server is not a ready host)\n");
    }
    pc_ded_flush();
}

static void pc_ded_cmd_resident_admin(int op, char* args) {
    const char* cname = op == 0 ? "resident-reset" : "resident-arm";
    char sel[64];
    char tok2[32];
    char extra[8];
    char msg[640];
    int n, r;
    sel[0] = tok2[0] = extra[0] = '\0';
    n = sscanf(args != NULL ? args : "", "%63s %31s %7s", sel, tok2, extra);
    if (n < 1) {
        pc_ded_printf("[DEDICATED] %s: usage: %s <slot|name> confirm (see `residents`)\n", cname, cname);
        pc_ded_flush();
        return;
    }
    if (n >= 3 || (n == 2 && pc_ded_stricmp(tok2, "confirm") != 0)) {
        pc_ded_printf("[DEDICATED] %s: expected exactly `<slot|name> confirm` (extra / unknown arguments: nothing was changed)\n", cname);
        pc_ded_flush();
        return;
    }
    r = pc_net_game_dedicated_resident_admin(op, sel, n == 2, msg, sizeof(msg));
    pc_ded_printf("[DEDICATED] %s: %s\n", cname, msg);
    PC_LOG(PCL_GENERAL, "dedicated: %s %s -> %s\n", cname, sel, r == 1 ? "done" : r == 2 ? "needs confirm" : "refused");
    pc_ded_flush();
}

/* M-F: `promote <guest> <slot|auto> <house|auto> confirm`. Without `confirm` nothing changes and the message says what would happen. */
static void pc_ded_cmd_promote(char* args) {
    char gsel[64], ssel[16], hsel[16], tok4[16], extra[8];
    char msg[900];
    int n, r;
    gsel[0] = ssel[0] = hsel[0] = tok4[0] = extra[0] = '\0';
    n = sscanf(args != NULL ? args : "", "%63s %15s %15s %15s %7s", gsel, ssel, hsel, tok4, extra);
    if (n < 3) {
        pc_ded_printf("[DEDICATED] promote: usage: promote <guest slot|name> <resident slot 0-%d|auto> <house 0-%d|auto> confirm (see `guests`, `residents`)\n", PC_RESIDENCE_SLOTS - 1, PC_RESIDENCE_HOUSES - 1);
        pc_ded_flush();
        return;
    }
    if (n >= 5 || (n == 4 && pc_ded_stricmp(tok4, "confirm") != 0)) {
        pc_ded_printf("[DEDICATED] promote: expected exactly `<guest> <slot|auto> <house|auto> confirm` (extra / unknown arguments: nothing was changed)\n");
        pc_ded_flush();
        return;
    }
    r = pc_net_game_dedicated_promote(gsel, ssel, hsel, n == 4, msg, sizeof(msg));
    pc_ded_printf("[DEDICATED] promote: %s\n", msg);
    PC_LOG(PCL_GENERAL, "dedicated: promote %s -> %s\n", gsel, r == 1 ? "done" : r == 2 ? "needs confirm" : "refused");
    pc_ded_flush();
}

/* M2: `members`: the town's memberships (residents + guests) as the pure membership layer sees them. */
static void pc_ded_cmd_members(void) {
    PCNetGameDedicatedMemberInfo rows[16];
    int i, n = pc_net_game_dedicated_members(rows, 16);
    if (n == 0) {
        pc_ded_printf("[DEDICATED] members: none (or this server is not a ready host)\n");
        pc_ded_flush();
        return;
    }
    pc_ded_printf("[DEDICATED] members of this town (%d):\n", n);
    for (i = 0; i < n; i++) {
        pc_ded_printf("  %s %d: name=\"%s\" home_town=\"%s\" kind=%s%s\n", rows[i].is_guest_row ? "guest slot" : "resident idx", rows[i].slot, rows[i].name,
               rows[i].home_town, rows[i].kind == 3 ? "AMBIGUOUS" : rows[i].is_guest_row ? "GUEST" : "RESIDENT",
               rows[i].is_guest_row ? (rows[i].confirmed ? " confirmed=yes" : " confirmed=no") : "");
    }
    pc_ded_flush();
}

/* args = the text after the command word: "<slot|name> [confirm]". The selector is one token; the trailing token `confirm` (case-insensitive) is the explicit consent. */
static void pc_ded_cmd_guest_admin(int op, char* args) {
    const char* cname = op == 0 ? "guest-remove" : "guest-reset-token";
    char sel[64];
    char tok2[32];
    char extra[8];
    char msg[640];
    int n, r;
    sel[0] = tok2[0] = extra[0] = '\0';
    n = sscanf(args != NULL ? args : "", "%63s %31s %7s", sel, tok2, extra);
    if (n < 1) {
        pc_ded_printf("[DEDICATED] %s: usage: %s <slot|name> confirm (see `guests`)\n", cname, cname);
        pc_ded_flush();
        return;
    }
    if (n >= 3 || (n == 2 && pc_ded_stricmp(tok2, "confirm") != 0)) {
        pc_ded_printf("[DEDICATED] %s: expected exactly `<slot|name> confirm` (extra / unknown arguments: nothing was changed)\n", cname);
        pc_ded_flush();
        return;
    }
    r = pc_net_game_dedicated_guest_admin(op, sel, n == 2, msg, sizeof(msg));
    pc_ded_printf("[DEDICATED] %s: %s\n", cname, msg);
    PC_LOG(PCL_GENERAL, "dedicated: %s %s -> %s\n", cname, sel, r == 1 ? "done" : r == 2 ? "needs confirm" : "refused");
    pc_ded_flush();
}

/* ------------------------------------------------------------------------------------------------------------------------------------- */
/* host admin item tools: finditem / iteminfo / items / give (main thread only; `give` does its work in pc_net_game.c)                    */
/* ------------------------------------------------------------------------------------------------------------------------------------- */

#define PC_DED_ITEM_PAGE 15
#define PC_DED_ITEM_MAX  4096

/* Item names are 16 bytes of GAME FONT codes (include/m_font.h), NOT ASCII: code 35 is a-acute, 42 is '~', 43 a heart, 144 the hyphen, 174 '/', 180 '+', 208 ';',
 * 209 '#'. This 256-entry table folds a game code to plain ASCII for matching AND printing: accented letters lose the accent, symbols and unmapped codes are '?'. */
static char s_ded_font_ascii[256];
static int  s_ded_font_ready = 0;

static void pc_ded_font_set(int lo, int hi, char c) {
    int i;
    for (i = lo; i <= hi; i++) {
        s_ded_font_ascii[i] = c;
    }
}

static void pc_ded_font_init(void) {
    int i;
    if (s_ded_font_ready) {
        return;
    }
    for (i = 0; i < 256; i++) {
        s_ded_font_ascii[i] = '?';
    }
    s_ded_font_ascii[0] = '!';  /* inverted ! */
    s_ded_font_ascii[1] = '?';  /* inverted ? */
    pc_ded_font_set(2, 7, 'A'); /* A with diaeresis / grave / acute / circumflex / tilde / ring */
    s_ded_font_ascii[8] = 'C';
    pc_ded_font_set(9, 12, 'E');
    pc_ded_font_set(13, 16, 'I');
    s_ded_font_ascii[17] = 'D';
    s_ded_font_ascii[18] = 'N';
    pc_ded_font_set(19, 24, 'O'); /* O grave / acute / circumflex / tilde / diaeresis, OE */
    pc_ded_font_set(25, 28, 'U');
    s_ded_font_ascii[29] = 's';   /* sharp s */
    s_ded_font_ascii[31] = 'a';   /* a grave */
    s_ded_font_ascii[32] = ' ';
    s_ded_font_ascii[33] = '!';
    s_ded_font_ascii[34] = '"';
    s_ded_font_ascii[35] = 'a';   /* a acute */
    s_ded_font_ascii[36] = 'a';   /* a circumflex */
    s_ded_font_ascii[37] = '%';
    s_ded_font_ascii[38] = '&';
    s_ded_font_ascii[39] = '\'';
    s_ded_font_ascii[40] = '(';
    s_ded_font_ascii[41] = ')';
    s_ded_font_ascii[42] = '~';
    s_ded_font_ascii[43] = '?';   /* heart symbol */
    s_ded_font_ascii[44] = ',';
    s_ded_font_ascii[45] = '-';
    s_ded_font_ascii[46] = '.';
    s_ded_font_ascii[47] = '?';   /* music note */
    for (i = 0; i < 10; i++) {
        s_ded_font_ascii[48 + i] = (char)('0' + i);
    }
    s_ded_font_ascii[58] = ':';
    s_ded_font_ascii[59] = '?';   /* droplet */
    s_ded_font_ascii[60] = '<';
    s_ded_font_ascii[61] = '=';
    s_ded_font_ascii[62] = '>';
    s_ded_font_ascii[63] = '?';
    s_ded_font_ascii[64] = '@';
    for (i = 0; i < 26; i++) {
        s_ded_font_ascii[65 + i] = (char)('A' + i);
        s_ded_font_ascii[97 + i] = (char)('a' + i);
    }
    s_ded_font_ascii[91] = 'a';   /* a tilde */
    s_ded_font_ascii[92] = '?';   /* annoyed symbol */
    s_ded_font_ascii[93] = 'a';   /* a diaeresis */
    s_ded_font_ascii[94] = 'a';   /* a ring */
    s_ded_font_ascii[95] = '_';
    s_ded_font_ascii[96] = 'c';   /* c cedilla */
    pc_ded_font_set(123, 126, 'e');
    pc_ded_font_set(129, 132, 'i');
    s_ded_font_ascii[133] = '.';  /* interpunct */
    s_ded_font_ascii[134] = 'd';
    s_ded_font_ascii[135] = 'n';
    pc_ded_font_set(136, 141, 'o');
    pc_ded_font_set(142, 143, 'u');
    s_ded_font_ascii[144] = '-';  /* hyphen */
    pc_ded_font_set(145, 146, 'u');
    pc_ded_font_set(147, 148, 'y');
    s_ded_font_ascii[150] = 'Y';
    s_ded_font_ascii[162] = 'A';  /* AE */
    s_ded_font_ascii[163] = 'a';  /* ae */
    s_ded_font_ascii[174] = '/';
    s_ded_font_ascii[180] = '+';
    s_ded_font_ascii[205] = ' ';  /* new line */
    s_ded_font_ascii[208] = ';';
    s_ded_font_ascii[209] = '#';
    s_ded_font_ascii[210] = ' ';
    s_ded_font_ascii[211] = ' ';
    s_ded_font_ascii[213] = '"';
    s_ded_font_ascii[214] = '"';
    s_ded_font_ascii[215] = '\'';
    s_ded_font_ascii[216] = '\'';
    s_ded_font_ascii[222] = '\\';
    s_ded_font_ready = 1;
}

/* The item's name as printable ASCII (trailing blanks trimmed). Returns 1 when the game has a real name for it, 0 for blank / zeroed / "unknown" entries (out = "(unknown name)"). */
static int pc_ded_item_name(unsigned id, char* out, size_t cap) {
    u8 raw[mIN_ITEM_NAME_LEN];
    int i, n = 0, blank = 1;
    pc_ded_font_init();
    memset(raw, 0, sizeof(raw));
    mIN_copy_name_str(raw, (mActor_name_t)id);
    for (i = 0; i < mIN_ITEM_NAME_LEN; i++) {
        if (raw[i] != 0x00 && raw[i] != 0x20) {
            blank = 0;
        }
    }
    if (!blank) {
        for (i = 0; i < mIN_ITEM_NAME_LEN && (size_t)n + 1 < cap; i++) {
            out[n++] = s_ded_font_ascii[raw[i]];
        }
        while (n > 0 && out[n - 1] == ' ') {
            n--;
        }
        out[n] = '\0';
        if (n == 0 || strcmp(out, "unknown") == 0) {
            blank = 1;
        }
    }
    if (blank) {
        snprintf(out, cap, "(unknown name)");
        return 0;
    }
    return 1;
}

enum { PC_DED_CAT_FURN = 0, PC_DED_CAT_TOOL, PC_DED_CAT_CLOTH, PC_DED_CAT_WALL, PC_DED_CAT_CARPET, PC_DED_CAT_MISC, PC_DED_CAT_ALL };

static int pc_ded_item_cat(unsigned id, const char** label) {
    static const char* const misc[ITEM1_CAT_NUM] = { "Paper", "Money", "Tool", "Fish", "Clothing", "Other", "Carpet", "Wallpaper",
                                                     "Fruit", "Plant", "Music", "Diary", "Ticket", "Insect", "Lucky bag", "Turnips" };
    switch (ITEM_NAME_GET_TYPE(id)) {
        case NAME_TYPE_FTR0:
        case NAME_TYPE_FTR1:
            *label = "Furniture";
            return PC_DED_CAT_FURN;
        case NAME_TYPE_ITEM1:
            switch (ITEM_NAME_GET_CAT(id)) {
                case ITEM1_CAT_TOOL:
                    *label = "Tools";
                    return PC_DED_CAT_TOOL;
                case ITEM1_CAT_CLOTH:
                    *label = "Clothing";
                    return PC_DED_CAT_CLOTH;
                case ITEM1_CAT_WALL:
                    *label = "Wallpaper";
                    return PC_DED_CAT_WALL;
                case ITEM1_CAT_CARPET:
                    *label = "Carpet";
                    return PC_DED_CAT_CARPET;
                default:
                    *label = misc[ITEM_NAME_GET_CAT(id) & 15];
                    return PC_DED_CAT_MISC;
            }
        default:
            *label = "?";
            return PC_DED_CAT_MISC;
    }
}

/* The whole id space the game can put in a pocket, in id order: FTR0 (step 4 = one entry per piece), ITEM1 (the game's own per-category bound), FTR1. Built once. */
static uint16_t s_ded_cat_ids[PC_DED_ITEM_MAX];
static int      s_ded_cat_n = -1;

static void pc_ded_catalog_build(void) {
    unsigned id;
    int cat, idx;
    if (s_ded_cat_n >= 0) {
        return;
    }
    s_ded_cat_n = 0;
    for (id = FTR0_START; id < (unsigned)FTR0_END && s_ded_cat_n < PC_DED_ITEM_MAX; id += 4) {
        s_ded_cat_ids[s_ded_cat_n++] = (uint16_t)id;
    }
    for (cat = 0; cat < ITEM1_CAT_NUM; cat++) {
        for (idx = 0; idx < 256 && s_ded_cat_n < PC_DED_ITEM_MAX; idx++) {
            id = 0x2000u | ((unsigned)cat << 8) | (unsigned)idx;
            if (mNT_check_unknown((mActor_name_t)id) == FALSE) {
                s_ded_cat_ids[s_ded_cat_n++] = (uint16_t)id;
            }
        }
    }
    for (id = FTR1_START; id < (unsigned)FTR1_END && s_ded_cat_n < PC_DED_ITEM_MAX; id += 4) {
        if (mNT_check_unknown((mActor_name_t)id) == FALSE) {
            s_ded_cat_ids[s_ded_cat_n++] = (uint16_t)id;
        }
    }
}

/* Strict number: decimal digits, or 0x/0X + hex digits; at most 8 digits; no sign, no spaces. */
static int pc_ded_parse_num(const char* s, unsigned long* out) {
    unsigned long v = 0;
    int base = 10, n = 0;
    if (s == NULL || s[0] == '\0') {
        return 0;
    }
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        base = 16;
        s += 2;
    }
    if (*s == '\0') {
        return 0;
    }
    for (; *s != '\0'; s++, n++) {
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
        if (n >= 8) {
            return 0;
        }
        v = v * (unsigned long)base + (unsigned long)d;
    }
    *out = v;
    return 1;
}

static int pc_ded_all_digits(const char* s) {
    if (*s == '\0') {
        return 0;
    }
    for (; *s != '\0'; s++) {
        if (*s < '0' || *s > '9') {
            return 0;
        }
    }
    return 1;
}

static int pc_ded_ci_contains(const char* hay, const char* needle_lc) {
    size_t n = strlen(needle_lc), i, h = strlen(hay);
    if (n == 0) {
        return 1;
    }
    for (i = 0; i + n <= h; i++) {
        size_t k;
        for (k = 0; k < n; k++) {
            if (tolower((unsigned char)hay[i + k]) != (unsigned char)needle_lc[k]) {
                break;
            }
        }
        if (k == n) {
            return 1;
        }
    }
    return 0;
}

/* Prints one page of the matching catalog entries. mode_cat = PC_DED_CAT_* (ALL = no filter); needle_lc = lower-case name filter or NULL. title = e.g. `ITEM SEARCH: "dresser"`. */
static void pc_ded_item_list(const char* title, const char* again_cmd, int mode_cat, const char* needle_lc, int page) {
    static uint16_t hits[PC_DED_ITEM_MAX];
    int i, n = 0, pages, first, last;
    char nm[40];
    const char* cl;
    pc_ded_catalog_build();
    for (i = 0; i < s_ded_cat_n; i++) {
        unsigned id = s_ded_cat_ids[i];
        int known = pc_ded_item_name(id, nm, sizeof(nm));
        int c = pc_ded_item_cat(id, &cl);
        if (mode_cat != PC_DED_CAT_ALL && c != mode_cat) {
            continue;
        }
        if (needle_lc != NULL && (!known || !pc_ded_ci_contains(nm, needle_lc))) {
            continue;
        }
        hits[n++] = (uint16_t)id;
    }
    if (n == 0) {
        pc_ded_printf("%s - no results\n", title);
        pc_ded_flush();
        return;
    }
    pages = (n + PC_DED_ITEM_PAGE - 1) / PC_DED_ITEM_PAGE;
    if (page < 1 || page > pages) {
        pc_ded_printf("%s - %d result%s, but page %d does not exist (pages 1-%d)\n", title, n, n == 1 ? "" : "s", page, pages);
        pc_ded_flush();
        return;
    }
    pc_ded_printf("%s - %d result%s\n", title, n, n == 1 ? "" : "s");
    first = (page - 1) * PC_DED_ITEM_PAGE;
    last = first + PC_DED_ITEM_PAGE;
    if (last > n) {
        last = n;
    }
    for (i = first; i < last; i++) {
        unsigned id = hits[i];
        int known = pc_ded_item_name(id, nm, sizeof(nm));
        const char* why = pc_net_game_dedicated_item_giveblock(id);
        (void)pc_ded_item_cat(id, &cl);
        pc_ded_printf("0x%04X - %s - %s%s\n", id, nm, cl, !known ? " [not giveable: no name]" : (why != NULL ? " [not giveable]" : ""));
    }
    if (pages > 1 && page < pages) {
        pc_ded_printf("Page %d/%d - next: %s\n", page, pages, again_cmd);
    } else {
        pc_ded_printf("Page %d/%d\n", page, pages);
    }
    pc_ded_flush();
}

static char* pc_ded_trim(char* s) {
    char* e;
    while (*s != '\0' && isspace((unsigned char)*s)) {
        s++;
    }
    e = s + strlen(s);
    while (e > s && isspace((unsigned char)e[-1])) {
        *--e = '\0';
    }
    return s;
}

/* finditem <text> [page]: a trailing all-digit token after at least one other word is the page. */
static void pc_ded_cmd_finditem(char* args) {
    char buf[PC_DED_LINE_MAX + 1];
    char title[96], again[160];
    char* q;
    char* sp;
    int page = 1, i;
    snprintf(buf, sizeof(buf), "%s", args != NULL ? args : "");
    q = pc_ded_trim(buf);
    if (*q == '\0') {
        pc_ded_printf("ITEM SEARCH: usage: finditem <text> [page]   (e.g. finditem dresser)\n");
        pc_ded_flush();
        return;
    }
    sp = strrchr(q, ' ');
    if (sp != NULL && pc_ded_all_digits(sp + 1)) {
        unsigned long pg;
        if (!pc_ded_parse_num(sp + 1, &pg) || pg < 1 || pg > 9999) {
            pc_ded_printf("ITEM SEARCH: invalid page '%s' (1..9999)\n", sp + 1);
            pc_ded_flush();
            return;
        }
        page = (int)pg;
        *sp = '\0';
        q = pc_ded_trim(q);
    }
    if (strlen(q) > 40) {
        pc_ded_printf("ITEM SEARCH: the search text is too long (max 40 characters)\n");
        pc_ded_flush();
        return;
    }
    for (i = 0; q[i] != '\0'; i++) {
        q[i] = (char)tolower((unsigned char)q[i]);
    }
    snprintf(title, sizeof(title), "ITEM SEARCH: \"%s\"", q);
    snprintf(again, sizeof(again), "finditem %s %d", q, page + 1);
    pc_ded_item_list(title, again, PC_DED_CAT_ALL, q, page);
}

static void pc_ded_cmd_items(char* args) {
    static const struct {
        const char* name;
        int         cat;
    } cats[] = {
        { "furniture", PC_DED_CAT_FURN }, { "tools", PC_DED_CAT_TOOL },    { "clothing", PC_DED_CAT_CLOTH },        { "wallpaper", PC_DED_CAT_WALL },
        { "carpet", PC_DED_CAT_CARPET },  { "miscellaneous", PC_DED_CAT_MISC }, { "all", PC_DED_CAT_ALL },
    };
    char buf[PC_DED_LINE_MAX + 1];
    char tok1[40], tok2[40], extra[8];
    char title[64], again[96];
    int n, page = 1, i, cat = -1;
    snprintf(buf, sizeof(buf), "%s", args != NULL ? args : "");
    tok1[0] = tok2[0] = extra[0] = '\0';
    n = sscanf(buf, "%39s %39s %7s", tok1, tok2, extra);
    if (n < 1 || n > 2) {
        pc_ded_printf("ITEMS: usage: items <furniture|tools|clothing|wallpaper|carpet|miscellaneous|all> [page]\n");
        pc_ded_flush();
        return;
    }
    for (i = 0; tok1[i] != '\0'; i++) {
        tok1[i] = (char)tolower((unsigned char)tok1[i]);
    }
    for (i = 0; i < (int)(sizeof(cats) / sizeof(cats[0])); i++) {
        if (strcmp(tok1, cats[i].name) == 0) {
            cat = i;
        }
    }
    if (cat < 0) {
        pc_ded_printf("ITEMS: unknown category '%s' (furniture, tools, clothing, wallpaper, carpet, miscellaneous, all)\n", tok1);
        pc_ded_flush();
        return;
    }
    if (n == 2) {
        unsigned long pg;
        if (!pc_ded_all_digits(tok2) || !pc_ded_parse_num(tok2, &pg) || pg < 1 || pg > 9999) {
            pc_ded_printf("ITEMS: invalid page '%s' (1..9999)\n", tok2);
            pc_ded_flush();
            return;
        }
        page = (int)pg;
    }
    snprintf(title, sizeof(title), "ITEMS (%s)", cats[cat].name);
    snprintf(again, sizeof(again), "items %s %d", cats[cat].name, page + 1);
    pc_ded_item_list(title, again, cats[cat].cat, NULL, page);
}

/* Accepts 0x.. hex or decimal, 0..0xFFFF. Furniture ids with the rotation bits set are masked (the piece is the same). Returns 1 and the id, else prints `tag Invalid item ID ...`. */
static int pc_ded_parse_item_id(const char* tag, const char* tok, unsigned* id, int* masked_from) {
    unsigned long v;
    *masked_from = -1;
    if (!pc_ded_parse_num(tok, &v)) {
        pc_ded_printf("%s Invalid item ID '%s' (use 0x2203 or decimal 8707)\n", tag, tok);
        return 0;
    }
    if (v > 0xFFFFul) {
        pc_ded_printf("%s Invalid item ID '%s' (outside 0x0000..0xFFFF)\n", tag, tok);
        return 0;
    }
    if (v != 0 && ITEM_IS_FTR((mActor_name_t)v) && (v & 3ul) != 0) {
        *masked_from = (int)v;
        v &= ~3ul;
    }
    *id = (unsigned)v;
    return 1;
}

static void pc_ded_cmd_iteminfo(char* args) {
    char buf[PC_DED_LINE_MAX + 1];
    char tok[40], extra[8], nm[40];
    unsigned id;
    int masked, known;
    const char* cl;
    const char* why;
    snprintf(buf, sizeof(buf), "%s", args != NULL ? args : "");
    tok[0] = extra[0] = '\0';
    if (sscanf(buf, "%39s %7s", tok, extra) != 1) {
        pc_ded_printf("ITEM INFO: usage: iteminfo <id>   (0x2203 or decimal 8707)\n");
        pc_ded_flush();
        return;
    }
    if (!pc_ded_parse_item_id("ITEM INFO:", tok, &id, &masked)) {
        pc_ded_flush();
        return;
    }
    if (!pc_net_game_dedicated_item_is_legal(id)) {
        pc_ded_printf("ITEM INFO: Invalid item ID 0x%04X (not an id the game can put in a pocket)\n", id);
        pc_ded_flush();
        return;
    }
    known = pc_ded_item_name(id, nm, sizeof(nm));
    (void)pc_ded_item_cat(id, &cl);
    why = pc_net_game_dedicated_item_giveblock(id);
    pc_ded_printf("ITEM INFO: 0x%04X (%u)\n", id, id);
    if (masked >= 0) {
        pc_ded_printf("  note: furniture rotation bits cleared (0x%04X -> 0x%04X); a rotated id is the same piece\n", (unsigned)masked, id);
    }
    pc_ded_printf("  name: %s\n  category: %s\n", nm, cl);
    if (!known) {
        pc_ded_printf("  giveable: NO - the game has no name for this id\n");
    } else if (why != NULL) {
        pc_ded_printf("  giveable: NO - %s\n", why);
    } else {
        pc_ded_printf("  giveable: yes (`give <player> 0x%04X [qty 1-15]`; one inventory slot per copy)\n", id);
    }
    pc_ded_flush();
}

/* give <player | peer N | "quoted name"> <id> [qty]: the player part is every word before the id (a trailing pair of numbers is `<id> <qty>`). */
static void pc_ded_cmd_give(char* args) {
    char buf[PC_DED_LINE_MAX + 1];
    char* tk[24];
    char sel[96];
    char who[40], msg[400], nm[40], suffix[16];
    unsigned id = 0;
    int ntk = 0, i, qty = 1, masked, name_end, id_at;
    unsigned long qv;
    char* p;
    snprintf(buf, sizeof(buf), "%s", args != NULL ? args : "");
    p = pc_ded_trim(buf);
    sel[0] = '\0';
    if (*p == '"') { /* "quoted name" */
        char* q = strchr(p + 1, '"');
        if (q == NULL) {
            pc_ded_printf("GIVE FAILED: unterminated quote. usage: give <player> <id> [qty]\n");
            pc_ded_flush();
            return;
        }
        *q = '\0';
        snprintf(sel, sizeof(sel), "%s", p + 1);
        p = q + 1;
    }
    while (*p != '\0' && ntk < 23) {
        while (*p != '\0' && isspace((unsigned char)*p)) {
            p++;
        }
        if (*p == '\0') {
            break;
        }
        tk[ntk++] = p;
        while (*p != '\0' && !isspace((unsigned char)*p)) {
            p++;
        }
        if (*p != '\0') {
            *p++ = '\0';
        }
    }
    while (*p != '\0' && isspace((unsigned char)*p)) {
        p++;
    }
    if (*p != '\0') {
        pc_ded_printf("GIVE FAILED: too many words. usage: give <player> <id> [qty]\n");
        pc_ded_flush();
        return;
    }
    if (sel[0] != '\0') {
        name_end = 0; /* tk[] = id [qty] */
    } else if (ntk >= 2 && pc_ded_stricmp(tk[0], "peer") == 0) {
        snprintf(sel, sizeof(sel), "peer %s", tk[1]);
        name_end = 2;
    } else {
        if (ntk >= 3 && pc_ded_parse_num(tk[ntk - 1], &qv) && pc_ded_parse_num(tk[ntk - 2], &qv)) {
            name_end = ntk - 2;
        } else {
            name_end = ntk - 1;
        }
        for (i = 0; i < name_end; i++) {
            size_t used = strlen(sel);
            snprintf(sel + used, sizeof(sel) - used, "%s%s", i > 0 ? " " : "", tk[i]);
        }
    }
    id_at = name_end;
    if (sel[0] == '\0' || id_at >= ntk || ntk - id_at > 2) {
        pc_ded_printf("GIVE FAILED: usage: give <player-or-guest-name> <id> [qty]   (`peer <N>` or \"quoted name\" also work; see `players`, `finditem`)\n");
        pc_ded_flush();
        return;
    }
    if (!pc_ded_parse_item_id("GIVE FAILED:", tk[id_at], &id, &masked)) {
        pc_ded_flush();
        return;
    }
    if (ntk - id_at == 2) {
        if (!pc_ded_all_digits(tk[id_at + 1]) || !pc_ded_parse_num(tk[id_at + 1], &qv) || qv < 1 || qv > 15) {
            pc_ded_printf("GIVE FAILED: Invalid quantity '%s' (1..15). No items were given.\n", tk[id_at + 1]);
            pc_ded_flush();
            return;
        }
        qty = (int)qv;
    }
    if (pc_net_game_dedicated_item_giveblock(id) != NULL) {
        pc_ded_printf("GIVE FAILED: Invalid item ID 0x%04X (%s). No items were given.\n", id, pc_net_game_dedicated_item_giveblock(id));
        pc_ded_flush();
        return;
    }
    if (!pc_ded_item_name(id, nm, sizeof(nm))) {
        pc_ded_printf("GIVE FAILED: Invalid item ID 0x%04X (the game has no name for it). No items were given.\n", id);
        pc_ded_flush();
        return;
    }
    if (pc_net_game_dedicated_give(sel, id, qty, who, sizeof(who), msg, sizeof(msg))) {
        suffix[0] = '\0';
        if (qty > 1) {
            snprintf(suffix, sizeof(suffix), " x%d", qty);
        }
        if (masked >= 0) {
            pc_ded_printf("GIVE: note: furniture rotation bits cleared (0x%04X -> 0x%04X)\n", (unsigned)masked, id);
        }
        pc_ded_printf("GIVE: %s received item 0x%04X (%s)%s.\n", who, id, nm, suffix);
        PC_LOG(PCL_GENERAL, "dedicated: give %s 0x%04X x%d -> done\n", who, id, qty);
    } else {
        pc_ded_printf("GIVE FAILED: %s\n", msg);
        PC_LOG(PCL_GENERAL, "dedicated: give '%s' 0x%04X x%d -> refused\n", sel, id, qty);
    }
    pc_ded_flush();
}

/* ---- developer commands (where / net / time / event / wildlife / test). Read-only unless noted; every refusal says why. ---- */
static const char* const s_ded_event_names[] = {
    "rumor_new_years_day",
    "new_years_day",
    "rumor_kamakura",
    "kamakura",
    "soncho_vacation_january",
    "soncho_vacation_february",
    "rumor_groundhog_day",
    "groundhog_day",
    "rumor_valentines_day",
    "valentines_day",
    "rumor_spring_sports_fair",
    "spring_equinox",
    "sports_fair_ball_toss",
    "sports_fair_aerobics",
    "sports_fair_tug_of_war",
    "sports_fair_foot_race",
    "sports_fair",
    "aprilfools_day",
    "rumor_aprilfools_day",
    "rumor_cherry_blossom_festival",
    "cherry_blossom_festival",
    "nature_day",
    "spring_cleaning",
    "mothers_day",
    "summer_camper",
    "graduation_day",
    "fathers_day",
    "rumor_fishing_tourney_1",
    "talk_fishing_tourney_1",
    "fishing_tourney_1",
    "town_day",
    "rumor_fireworks_show",
    "fireworks_show",
    "rumor_morning_aerobics",
    "talk_morning_aerobics",
    "morning_aerobics",
    "rumor_meteor_shower",
    "meteor_shower",
    "founders_day",
    "labor_day",
    "rumor_fall_sports_fair",
    "autumn_equinox",
    "rumor_harvest_moon_day",
    "harvest_moon_festival",
    "explorers_day",
    "rumor_mushroom_season",
    "talk_mushroom_season",
    "mushroom_season",
    "rumor_halloween",
    "halloween",
    "mayors_day",
    "officers_day",
    "rumor_fishing_tourney_2",
    "talk_fishing_tourney_2",
    "fishing_tourney_2",
    "rumor_harvest_festival",
    "harvest_festival",
    "sale_day",
    "snow_day",
    "toy_day_soncho",
    "rumor_toy_day",
    "toy_day_jingle",
    "snowman_season",
    "rumor_new_years_eve_countdown",
    "new_years_eve_countdown",
    "cherry_blossom_petals",
    "talk_toy_day",
    "player_birthday",
    "dozaemon",
    "kabu_peddler",
    "lottery",
    "kk_slider",
    "handbill_broker",
    "handbill_shop_sale",
    "artist",
    "broker_sale",
    "designer",
    "gypsy",
    "shop_sale",
    "carpet_peddler",
    "soncho_new_years_day",
    "soncho_groundhog_day",
    "soncho_spring_sports_fair",
    "soncho_aprilfools_day",
    "soncho_cherry_blossom_festival",
    "soncho_nature_day",
    "soncho_spring_cleaning",
    "soncho_mothers_day",
    "soncho_graduation_day",
    "soncho_fathers_day",
    "soncho_fishing_tourney_1",
    "soncho_town_day",
    "soncho_fireworks_show",
    "soncho_meteor_shower",
    "soncho_founders_day",
    "soncho_labor_day",
    "soncho_fall_sports_fair",
    "soncho_harvest_moon_festival",
    "soncho_explorers_day",
    "soncho_halloween",
    "soncho_mayors_day",
    "soncho_officers_day",
    "soncho_fishing_tourney_2",
    "soncho_harvest_festival",
    "soncho_sale_day",
    "soncho_snow_day",
    "soncho_toy_day",
    "talk_new_years_countdown",
    "harvest_festival_franklin",
    "weather_clear",
    "weather_snow",
    "weather_sports_fair",
    "bridge_make",
    "soncho_bridge_make",
    "ghost",
    "mask_npc",
    "74",
    "koinobori",
    "76"
};
#define PC_DED_EVENT_COUNT ((int)(sizeof(s_ded_event_names) / sizeof(s_ded_event_names[0])))

static char* pc_ded_skip_ws(char* p) {
    while (p != NULL && *p != '\0' && isspace((unsigned char)*p)) {
        p++;
    }
    return p;
}

/* splits "sub rest" in place: returns the sub-command, *rest = the remaining text (never NULL) */
static char* pc_ded_split_sub(char* args, char** rest) {
    char* sub = pc_ded_skip_ws(args);
    char* r = sub;
    if (sub == NULL) {
        static char empty[1] = { 0 };
        *rest = empty;
        return empty;
    }
    while (*r != '\0' && !isspace((unsigned char)*r)) {
        r++;
    }
    if (*r != '\0') {
        *r++ = '\0';
        r = pc_ded_skip_ws(r);
    }
    *rest = r;
    return sub;
}

static int pc_ded_find_peer(char* args, PCNetGameDedicatedPeerInfo* out) {
    int i, n = pc_net_game_dedicated_peer_slots();
    unsigned long pv;
    args = pc_ded_skip_ws(args);
    if (args == NULL || *args == '\0') {
        return 0;
    }
    if (strncmp(args, "peer ", 5) == 0 && pc_ded_parse_num(args + 5, &pv)) {
        return pv < (unsigned long)n && pc_net_game_dedicated_peer_info((int)pv, out) ? 1 : 0;
    }
    for (i = 0; i < n; i++) {
        if (pc_net_game_dedicated_peer_info(i, out) && pc_ded_stricmp(out->name, args) == 0) {
            return 1;
        }
    }
    return 0;
}

static void pc_ded_print_where(const PCNetGameDedicatedPeerInfo* pi) {
    float x, y, z;
    if (pc_remote_player_get_last_position((PCNetPlayerId)pi->peer, &x, &y, &z)) {
        pc_ded_printf("  peer %d \"%s\": x=%.1f y=%.1f z=%.1f acre=(%d,%d) link=%s\n", pi->peer, pi->name, (double)x, (double)y, (double)z, (int)(x / 640.0f), (int)(z / 640.0f), pc_ded_link_name(pi->link));
    } else {
        pc_ded_printf("  peer %d \"%s\": no position reported yet (link=%s)\n", pi->peer, pi->name, pc_ded_link_name(pi->link));
    }
}

static void pc_ded_cmd_where(char* args) {
    PCNetGameDedicatedPeerInfo pi;
    int i, n = pc_net_game_dedicated_peer_slots(), listed = 0;
    args = pc_ded_skip_ws(args);
    if (args != NULL && *args != '\0') {
        if (pc_ded_find_peer(args, &pi)) {
            pc_ded_printf("[DEDICATED] where:\n");
            pc_ded_print_where(&pi);
        } else {
            pc_ded_printf("WHERE FAILED: no connected player '%s' (see `players`; `peer <N>` also works)\n", args);
        }
        pc_ded_flush();
        return;
    }
    pc_ded_printf("[DEDICATED] where: the server has no player of its own; connected players (world units; acre = x/640, z/640):\n");
    for (i = 0; i < n; i++) {
        if (pc_net_game_dedicated_peer_info(i, &pi)) {
            pc_ded_print_where(&pi);
            listed++;
        }
    }
    if (listed == 0) {
        pc_ded_printf("  (no players connected)\n");
    }
    pc_ded_flush();
}

static void pc_ded_cmd_net(char* args) {
    PCNetStats ns;
    int i, n = pc_net_game_dedicated_peer_slots(), ready = 0, tr = 0;
    args = pc_ded_skip_ws(args);
    if (args != NULL && pc_ded_stricmp(args, "status") == 0) {
        for (i = 0; i < n; i++) {
            PCNetGameDedicatedPeerInfo pi;
            if (pc_net_game_dedicated_peer_info(i, &pi)) {
                tr++;
                ready += pi.link == PC_NETGAME_LINK_READY;
            }
        }
        pc_net_get_stats(&ns);
        pc_ded_printf("[DEDICATED] net status: role=%s port=%u peers=%d ready=%d reliable sent=%lu retransmits=%lu events dropped unreliable=%lu control=%lu\n", pc_ded_role_name(), (unsigned)s_port, tr, ready,
               (unsigned long)ns.rdata_sent, (unsigned long)ns.rdata_retransmits, (unsigned long)ns.events_dropped_unreliable, (unsigned long)ns.events_dropped_control);
    } else if (args != NULL && pc_ded_stricmp(args, "peers") == 0) {
        pc_ded_cmd_players();
        return;
    } else if (args != NULL && pc_ded_stricmp(args, "resync") == 0) {
        pc_ded_printf("NET RESYNC UNSUPPORTED: there is no general resynchronisation API. Each system resyncs itself on (re)connect (field, wildlife session generation, roster); nothing was sent.\n");
    } else {
        pc_ded_printf("usage: net status | net peers | net resync\n");
    }
    pc_ded_flush();
}

static void pc_ded_cmd_time(char* args) {
    static const char* const wd[] = { "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday" };
    args = pc_ded_skip_ws(args);
    if (args == NULL || pc_ded_stricmp(args, "get") != 0) {
        pc_ded_printf("usage: time get   (changing the game clock is not offered)\n");
    } else if (!pc_net_game_dedicated_world_ready()) {
        pc_ded_printf("TIME FAILED: the world is not ready yet\n");
    } else {
        const lbRTC_time_c t = Common_Get(time.rtc_time);
        pc_ded_printf("[DEDICATED] time: %04d-%02d-%02d %02d:%02d:%02d %s\n", (int)t.year, (int)t.month, (int)t.day, (int)t.hour, (int)t.min, (int)t.sec, (t.weekday < 7) ? wd[t.weekday] : "?");
    }
    pc_ded_flush();
}

static int pc_ded_find_event(const char* name) {
    int i;
    unsigned long v;
    for (i = 0; i < PC_DED_EVENT_COUNT; i++) {
        if (pc_ded_stricmp(s_ded_event_names[i], name) == 0) {
            return i;
        }
    }
    return pc_ded_parse_num(name, &v) && v < (unsigned long)PC_DED_EVENT_COUNT ? (int)v : -1;
}

static void pc_ded_print_event(int ev) {
    pc_ded_printf("  %3d %-34s scheduled-today=%s active-now=%s status:%s%s%s\n", ev, s_ded_event_names[ev], mEv_check_run_today(ev) ? "yes" : "no", mEv_check_schedule(ev) ? "yes" : "no",
           mEv_check_status(ev, mEv_STATUS_RUN) ? " RUN" : "", mEv_check_status(ev, mEv_STATUS_ACTIVE) ? " ACTIVE" : "", mEv_check_status(ev, mEv_STATUS_ERROR) ? " ERROR" : "");
}

static void pc_ded_cmd_event(char* args) {
    char* rest;
    char* sub = pc_ded_split_sub(args, &rest);
    int i, ev, shown = 0;
    if (*sub == '\0') {
        pc_ded_printf("usage: event list | event status [name] | event start|stop|reset <name>\n");
    } else if (pc_ded_stricmp(sub, "start") == 0 || pc_ded_stricmp(sub, "stop") == 0 || pc_ded_stricmp(sub, "reset") == 0) {
        pc_ded_printf("EVENT %s REFUSED: no safe forced start exists. The only game entry (mEv_init_force) re-rolls the WHOLE day's event schedule and renewal, which would change ordinary scheduling and can\n"
                      "duplicate reward / claim state; setting a status flag alone would not create the event's actors. Nothing was changed. `event status <name>` shows when an event runs; events need the right\n"
                      "date and hour, their NPC, and on a client a READY host.\n", sub);
    } else if (!pc_net_game_dedicated_world_ready()) {
        pc_ded_printf("EVENT FAILED: the world is not ready yet\n");
    } else if (pc_ded_stricmp(sub, "list") == 0 || (pc_ded_stricmp(sub, "status") == 0 && *rest == '\0')) {
        const int all = pc_ded_stricmp(sub, "list") == 0;
        pc_ded_printf("[DEDICATED] events (the game's schedule for today; `event list` = every type, `event status` = only scheduled / running ones):\n");
        for (i = 0; i < PC_DED_EVENT_COUNT; i++) {
            if (all || mEv_check_run_today(i) || mEv_check_status(i, mEv_STATUS_RUN) || mEv_check_status(i, mEv_STATUS_ACTIVE)) {
                pc_ded_print_event(i);
                shown++;
            }
        }
        if (shown == 0) {
            pc_ded_printf("  (no event is scheduled or running today)\n");
        }
    } else if (pc_ded_stricmp(sub, "status") == 0) {
        ev = pc_ded_find_event(rest);
        if (ev < 0) {
            pc_ded_printf("EVENT FAILED: unknown event '%s' (see `event list`)\n", rest);
        } else {
            pc_ded_print_event(ev);
        }
    } else {
        pc_ded_printf("usage: event list | event status [name] | event start|stop|reset <name>\n");
    }
    pc_ded_flush();
}

static int pc_ded_fish_species(const char* nm) {
    unsigned long v;
    if (pc_ded_stricmp(nm, "seabass") == 0) return aGYO_TYPE_SEA_BASS;
    if (pc_ded_stricmp(nm, "snapper") == 0) return aGYO_TYPE_RED_SNAPPER;
    if (pc_ded_stricmp(nm, "coelacanth") == 0) return aGYO_TYPE_COELACANTH;
    if (pc_ded_stricmp(nm, "jellyfish") == 0) return aGYO_TYPE_JELLYFISH;
    if (pc_ded_stricmp(nm, "knifejaw") == 0) return aGYO_TYPE_BARRED_KNIFEJAW;
    if (pc_ded_stricmp(nm, "whale") == 0) return aGYO_TYPE_WHALE;
    if (pc_ded_stricmp(nm, "salmon2") == 0) return aGYO_TYPE_SALMON2;
    return pc_ded_parse_num(nm, &v) && v < 256ul ? (int)v : -1;
}

static void pc_ded_print_wl(const PcWildlifeRecord* r) {
    pc_ded_printf("  id %u %s species %d acre (%d,%d) at (%.1f, %.1f, %.1f)\n", (unsigned)r->entity_id, r->kind == PC_WILDLIFE_KIND_FISH ? "fish" : "bug", r->species, r->bx, r->bz, (double)r->pos_x,
           (double)r->pos_y, (double)r->pos_z);
}

static void pc_ded_cmd_wildlife(char* args) {
    char* rest;
    char* sub = pc_ded_split_sub(args, &rest);
    PcWildlifeRecord r;
    unsigned long id;
    int i;
    if (*sub == '\0') {
        pc_ded_printf("usage: wildlife list | info <id> | spawn fish <species> <x> <z> | despawn <id>   (spawn / despawn: AC_TEST_HOOKS=1)\n");
    } else if (pc_ded_stricmp(sub, "list") == 0) {
        pc_ded_printf("[DEDICATED] wildlife: %d authoritative record(s), session generation %u\n", pcwld_active_count(), (unsigned)pcwld_session_generation());
        for (i = 0; i < PCWLD_PUBLIC_MAX_ENTITIES; i++) {
            if (pcwld_get_by_slot(i, &r)) {
                pc_ded_print_wl(&r);
            }
        }
    } else if (pc_ded_stricmp(sub, "info") == 0) {
        if (!pc_ded_parse_num(rest, &id) || id <= 0) {
            pc_ded_printf("usage: wildlife info <id>\n");
        } else if (!pcwld_find_by_id((uint32_t)id, &r)) {
            pc_ded_printf("WILDLIFE FAILED: no authoritative record with id %lu (see `wildlife list`)\n", id);
        } else {
            pc_ded_printf("[DEDICATED] wildlife record:\n");
            pc_ded_print_wl(&r);
        }
    } else if (pc_ded_stricmp(sub, "spawn") == 0 || pc_ded_stricmp(sub, "despawn") == 0) {
        const int spawn = pc_ded_stricmp(sub, "spawn") == 0;
        if (!pc_test_hooks_enabled()) {
            pc_ded_printf("WILDLIFE %s REFUSED: test-only command; needs a test-hook build and AC_TEST_HOOKS=1 (nothing changed)\n", spawn ? "SPAWN" : "DESPAWN");
        } else if (!pc_net_game_dedicated_world_ready()) {
            pc_ded_printf("WILDLIFE FAILED: the world is not ready yet\n");
        } else if (spawn) {
            char kind[16] = { 0 }, sp[24] = { 0 };
            float x = 0.0f, z = 0.0f;
            if (sscanf(rest, "%15s %23s %f %f", kind, sp, &x, &z) != 4 || pc_ded_stricmp(kind, "fish") != 0) {
                pc_ded_printf("usage: wildlife spawn fish <seabass|snapper|coelacanth|jellyfish|knifejaw|whale|salmon2|N> <x> <z>   (bugs: UNSUPPORTED, there is no host bug-injection path)\n");
            } else if (!(x > -1.0f && x < 3200.0f && z > -1.0f && z < 3840.0f)) { /* NaN / inf compare false; the town is 5 x 6 acres of 640 */
                pc_ded_printf("WILDLIFE SPAWN FAILED: coordinates outside the town (x 0..3200, z 0..3840) or not a number\n");
            } else if (pc_ded_fish_species(sp) < 0) {
                pc_ded_printf("WILDLIFE SPAWN FAILED: unknown fish species '%s'\n", sp);
            } else {
                const uint32_t nid = pcwld_test_host_inject(pc_ded_fish_species(sp), x, z);
                if (nid != 0u) {
                    pc_ded_printf("WILDLIFE SPAWN: fish species %d created as authoritative record %u at (%.1f, %.1f)\n", pc_ded_fish_species(sp), (unsigned)nid, (double)x, (double)z);
                } else {
                    pc_ded_printf("WILDLIFE SPAWN FAILED: the host refused the injection (no field scene, no usable spot, or the table / actor pool is full)\n");
                }
            }
        } else if (!pc_ded_parse_num(rest, &id) || id <= 0) {
            pc_ded_printf("usage: wildlife despawn <id>\n");
        } else if (pcwld_test_host_release((uint32_t)id)) {
            pc_ded_printf("WILDLIFE DESPAWN: record %lu released (clients despawn it)\n", id);
        } else {
            pc_ded_printf("WILDLIFE DESPAWN FAILED: no record with id %lu\n", id);
        }
    } else if (pc_ded_stricmp(sub, "sync") == 0) {
        pc_ded_printf("WILDLIFE SYNC UNSUPPORTED: wildlife state streams automatically (5 Hz) and a (re)connecting client gets the full table; there is no manual trigger. See `wildlife list`.\n");
    } else {
        pc_ded_printf("usage: wildlife list | info <id> | spawn fish <species> <x> <z> | despawn <id>\n");
    }
    pc_ded_flush();
}

static void pc_ded_cmd_test(char* args) {
    args = pc_ded_skip_ws(args);
    if (args != NULL && pc_ded_stricmp(args, "help") == 0) {
        pc_ded_printf("[DEDICATED] test hooks: behaviour-changing commands (wildlife spawn / despawn) and the --force-* / --*-test-* flags need a test-hook build (-DPC_TEST_HOOKS=ON) AND AC_TEST_HOOKS=1.\n"
                      "  Read-only commands (where, net, time, event list/status, wildlife list/info, items) always work. A refused command prints the reason and changes nothing.\n");
    } else {
        pc_ded_printf("[DEDICATED] test status: hooks %s\n", pc_test_hooks_enabled() ? "ENABLED (test-hook build and AC_TEST_HOOKS=1)" : "DISABLED (not a test-hook build, or AC_TEST_HOOKS is not 1)");
    }
    pc_ded_flush();
}

/* `shop-level-boost [confirm]`: dev/admin progression aid. Adds (once, through the game's own mSP_PlusSales) exactly the Bells still missing to the NEXT shop tier's
 * sales threshold. It never writes shop_level, renewal_time or mEv_SAVED_RENEWSHOP: the vanilla Shop_Level actor (present in SCENE_FG, where the observer stands) then schedules
 * the renewal (aSL_JudgeRenewShop: 2 days, skipped while a sale / lottery day is near) exactly as if the Bells had been spent. Without `confirm` it only prints what it would do. */
static void pc_ded_cmd_shop_level_boost(char* args) {
    static const char* names[] = { "Nook's Cranny", "Nook 'n' Go", "Nookway", "Nookington's" };
    const int level = mSP_GetShopLevel();
    const u32 sum = mSP_GetSalesSum();
    u32 target;
    int real_after;

    args = pc_ded_skip_ws(args);
    if (!pc_net_game_dedicated_world_ready() || !pcfa_save_ready()) {
        pc_ded_printf("SHOP-LEVEL-BOOST REFUSED: the world / save is not ready yet (see `status`)\n");
    } else if (level < mSP_SHOP_TYPE_ZAKKA || level >= mSP_SHOP_TYPE_DSUPER) {
        pc_ded_printf("SHOP-LEVEL-BOOST REFUSED: the shop is already at the maximum tier (level %d)\n", level);
    } else if (pc_net_game_shop_restocking()) {
        pc_ded_printf("SHOP-LEVEL-BOOST REFUSED: Nook's shop is restocking; try again when it is open\n");
    } else if (mEv_CheckEvent(mEv_SAVED_RENEWSHOP)) {
        pc_ded_printf("SHOP-LEVEL-BOOST REFUSED: an upgrade is already scheduled (mEv_SAVED_RENEWSHOP is set); it happens at the scheduled renewal time\n");
    } else if ((target = (level == mSP_SHOP_TYPE_ZAKKA) ? (u32)mSP_COMBINI_SUM : (level == mSP_SHOP_TYPE_COMBINI) ? (u32)mSP_SUPER_SUM : (u32)mSP_DSUPER_SUM) <= sum) {
        pc_ded_printf("SHOP-LEVEL-BOOST REFUSED: sales already reached the next threshold (%u >= %u)%s\n", (unsigned)sum, (unsigned)target,
                      level == mSP_SHOP_TYPE_SUPER ? "; Nookington's also needs a foreign-town shopper (visitor_flag) unless disable_shop_visitor_req = 1" : "");
    } else if (args == NULL || pc_ded_stricmp(args, "confirm") != 0) {
        pc_ded_printf("SHOP-LEVEL-BOOST (dry run): %s (level %d), sales %u; would add %u Bells of sales once (mSP_PlusSales) to reach %u (%s). Nothing changed; type `shop-level-boost confirm`.\n",
                      names[level], level, (unsigned)sum, (unsigned)(target - sum), (unsigned)target, names[level + 1]);
    } else {
        mSP_PlusSales(target - sum);
        real_after = mSP_GetRealShopLevel();
        pc_ded_printf("SHOP-LEVEL-BOOST: sales %u -> %u (added %u once via mSP_PlusSales); shop level stays %d now, qualifying level %d.\n", (unsigned)sum,
                      (unsigned)mSP_GetSalesSum(), (unsigned)(target - sum), level, real_after);
        if (real_after > level) {
            pc_ded_printf("  NOT immediate: the vanilla shop actor schedules the renewal on its next town frame and the new shop opens about 2 days later (renewal_time); a sale / lottery day within 2 days postpones the scheduling. Not written here: shop_level, renewal_time.\n");
        } else {
            pc_ded_printf("  No upgrade will be scheduled yet: Nookington's needs a foreign-town shopper (visitor_flag) unless disable_shop_visitor_req = 1 in settings.\n");
        }
        pc_ded_printf("  The change is in memory; it is written to disk by the next save (`save`).\n");
        PC_LOG(PCL_GENERAL, "dedicated: shop-level-boost: sales %u -> %u\n", (unsigned)sum, (unsigned)mSP_GetSalesSum());
    }
    pc_ded_flush();
}

static void pc_ded_cmd_stop(void) {
    if (s_stop_requested) {
        pc_ded_printf("[DEDICATED] already stopping...\n");
    } else {
        s_stop_requested = 1;
        pc_ded_printf("[DEDICATED] stopping...\n");
        PC_LOG(PCL_GENERAL, "dedicated: console stop requested\n");
        g_pc_running = 0; /* same flag as Ctrl+C: the normal shutdown path (final save once, net shutdown, platform shutdown) follows */
    }
    pc_ded_flush();
}

static void pc_ded_execute(char* line) {
    char* p = line;
    char* end;
    char* cmd;
    char* args = NULL;
    size_t i;
    while (*p != '\0' && isspace((unsigned char)*p)) {
        p++;
    }
    end = p + strlen(p);
    while (end > p && isspace((unsigned char)end[-1])) {
        *--end = '\0';
    }
    if (*p == '\0') {
        return; /* blank line */
    }
    cmd = p; /* first token, lower-cased; extra arguments are tolerated and ignored */
    for (i = 0; cmd[i] != '\0' && !isspace((unsigned char)cmd[i]); i++) {
        cmd[i] = (char)tolower((unsigned char)cmd[i]);
    }
    if (cmd[i] != '\0') {
        cmd[i] = '\0';
        args = cmd + i + 1; /* the rest of the line, case preserved (guest names) */
    }
    if (strcmp(cmd, "help") == 0 || strcmp(cmd, "?") == 0) {
        pc_ded_cmd_help();
    } else if (strcmp(cmd, "status") == 0) {
        pc_ded_cmd_status();
    } else if (strcmp(cmd, "players") == 0) {
        pc_ded_cmd_players();
    } else if (strcmp(cmd, "guests") == 0) {
        pc_ded_cmd_guests();
    } else if (strcmp(cmd, "residents") == 0) {
        pc_ded_cmd_residents();
    } else if (strcmp(cmd, "resident-reset") == 0) {
        pc_ded_cmd_resident_admin(0, args);
    } else if (strcmp(cmd, "resident-arm") == 0) {
        pc_ded_cmd_resident_admin(1, args);
    } else if (strcmp(cmd, "members") == 0) {
        pc_ded_cmd_members();
    } else if (strcmp(cmd, "promote") == 0) {
        pc_ded_cmd_promote(args);
    } else if (strcmp(cmd, "guest-remove") == 0) {
        pc_ded_cmd_guest_admin(0, args);
    } else if (strcmp(cmd, "guest-reset-token") == 0) {
        pc_ded_cmd_guest_admin(1, args);
    } else if (strcmp(cmd, "finditem") == 0) {
        pc_ded_cmd_finditem(args);
    } else if (strcmp(cmd, "iteminfo") == 0) {
        pc_ded_cmd_iteminfo(args);
    } else if (strcmp(cmd, "items") == 0) {
        pc_ded_cmd_items(args);
    } else if (strcmp(cmd, "give") == 0) {
        pc_ded_cmd_give(args);
    } else if (strcmp(cmd, "where") == 0) {
        pc_ded_cmd_where(args);
    } else if (strcmp(cmd, "net") == 0) {
        pc_ded_cmd_net(args);
    } else if (strcmp(cmd, "time") == 0) {
        pc_ded_cmd_time(args);
    } else if (strcmp(cmd, "event") == 0) {
        pc_ded_cmd_event(args);
    } else if (strcmp(cmd, "wildlife") == 0) {
        pc_ded_cmd_wildlife(args);
    } else if (strcmp(cmd, "test") == 0) {
        pc_ded_cmd_test(args);
    } else if (strcmp(cmd, "shop-level-boost") == 0) {
        pc_ded_cmd_shop_level_boost(args);
    } else if (strcmp(cmd, "tp") == 0 || strcmp(cmd, "bell") == 0) {
        pc_ded_printf("%s UNSUPPORTED on the dedicated server: %s\n", cmd, strcmp(cmd, "tp") == 0 ? "it has no local player, and a remote player's movement is owned by that client (the host never moves it)" : "bells are per-player save data with no host-side admin API in this build; use `give` for items");
        pc_ded_flush();
    } else if (strcmp(cmd, "save") == 0) {
        if (s_save_pending) {
            pc_ded_printf("[DEDICATED] save: already requested (waiting for the next safe point)\n");
        } else {
            s_save_pending = 1;
            pc_ded_printf("[DEDICATED] save: requested (runs at the next safe point; the result follows)\n");
        }
        pc_ded_flush();
    } else if (strcmp(cmd, "stop") == 0 || strcmp(cmd, "quit") == 0 || strcmp(cmd, "exit") == 0) {
        pc_ded_cmd_stop();
    } else {
        pc_ded_printf("[DEDICATED] unknown command: %s (type help)\n", cmd);
        PC_LOG(PCL_GENERAL, "dedicated: unknown console command '%s'\n", cmd);
        pc_ded_flush();
    }
}

void pc_dedicated_console_poll(void) {
    if (!g_pc_dedicated) {
        return;
    }
    if (s_q_count_hint != 0) {
        for (;;) {
            PCDedLine ln;
            int dropped = 0;
            PC_DED_LOCK();
            if (s_q_count == 0) {
                dropped = s_q_dropped;
                s_q_dropped = 0;
                s_q_count_hint = 0;
                PC_DED_UNLOCK();
                if (dropped > 0) {
                    pc_ded_printf("[DEDICATED] warning: console input queue was full, %d line(s) dropped\n", dropped);
                    pc_ded_flush();
                }
                break;
            }
            ln = s_queue[s_q_head];
            s_q_head = (s_q_head + 1) % PC_DED_QUEUE_MAX;
            s_q_count--;
            s_q_count_hint = s_q_count;
            PC_DED_UNLOCK();
            if (ln.truncated) {
                pc_ded_printf("[DEDICATED] warning: console line truncated to %d characters\n", PC_DED_LINE_MAX);
            }
            s_ded_cmd_depth++;
            pc_ded_execute(ln.text);
            s_ded_cmd_depth--;
        }
    }
    if (s_stdin_eof && !s_stdin_eof_reported) {
        s_stdin_eof_reported = 1;
        pc_ded_printf("[DEDICATED] console input closed (EOF); the server keeps running (stop it with Ctrl+C or a termination request)\n");
        PC_LOG(PCL_GENERAL, "dedicated: console input closed (EOF), server keeps running\n");
        pc_ded_flush();
    }
}

int pc_dedicated_save_request_pending(void) {
    return g_pc_dedicated && s_save_pending;
}

void pc_dedicated_save_report(int ok, const char* refusal_reason) {
    s_save_pending = 0;
    if (refusal_reason != NULL) {
        pc_ded_printf("[DEDICATED] save: refused: %s\n", refusal_reason);
        PC_LOG(PCL_GENERAL, "dedicated: console save refused: %s\n", refusal_reason);
    } else if (ok) {
        pc_ded_printf("[DEDICATED] save: OK\n");
    } else {
        pc_ded_printf("[DEDICATED] save: FAILED (the authoritative write reported an error; see the log)\n");
    }
    pc_ded_flush();
}
