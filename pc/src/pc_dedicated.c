/* pc_dedicated.c - the --dedicated server mode: console attach, stdin command queue + commands, audio/window/pacing helpers, [DEDICATED] notices.
 * See pc_dedicated.h for the overview. Nothing in here runs unless g_pc_dedicated == 1 (the callers check it; most functions also check it). */
#include "pc_platform.h"
#include "pc_dedicated.h"
#include "pc_host_observer.h"
#include "pc_net_game.h"
#include "pc_log.h"

#include <stdarg.h>
#include <ctype.h>

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

static FILE* pc_ded_stream(void) {
    return s_ded_out != NULL ? s_ded_out : stdout;
}

static void pc_ded_flush(void) {
    fflush(pc_ded_stream());
}

static int pc_ded_printf(const char* fmt, ...) {
    va_list ap;
    int n;
    va_start(ap, fmt);
    n = vfprintf(pc_ded_stream(), fmt, ap);
    va_end(ap);
    return n;
}

int pc_dedicated_stdout_quiet(void) {
    return s_ded_interactive;
}

void pc_dedicated_say(const char* fmt, ...) {
    va_list ap;
    if (!g_pc_dedicated) {
        return;
    }
    fputs("[DEDICATED] ", pc_ded_stream());
    va_start(ap, fmt);
    vfprintf(pc_ded_stream(), fmt, ap);
    va_end(ap);
    fputc('\n', pc_ded_stream());
    pc_ded_flush();
}

/* FATAL notices: stderr, or the server console when stderr is going to NUL (interactive mode) so they are never lost. */
static void pc_ded_fatal(const char* msg_a, const char* arg) {
    FILE* f = s_ded_interactive ? pc_ded_stream() : stderr;
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
            HANDLE ph = CreateFileA("CONOUT$", GENERIC_WRITE, FILE_SHARE_WRITE | FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
            if (ph != INVALID_HANDLE_VALUE) {
                static const char note[] = "[DEDICATED] the server console opened in its own window: type help / status / players / guests / save / stop THERE "
                                           "(this shell is not the server's console).\r\n";
                DWORD w = 0;
                WriteFile(ph, note, (DWORD)(sizeof(note) - 1), &w, NULL);
                CloseHandle(ph);
            }
            FreeConsole();
        }
        if (GetConsoleWindow() == NULL) {
            AllocConsole();
        }
        SetConsoleTitleA("Animal Crossing dedicated server console");
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
    if (s_stdin_started) {
        pc_dedicated_say("console input: reading commands from stdin (type help)");
    } else {
        pc_dedicated_say("console input: unavailable (no valid stdin); the server runs without commands");
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
           "  guest-remove <slot|name> confirm        remove a guest and its character (guests.dat is backed up first; refused while the guest is connected)\n"
           "  guest-reset-token <slot|name> confirm   token lost: the next claim of that guest's key gets a NEW token for the SAME character (10 min, once; backed up)\n"
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
        int gb = 0, gc = 0;
        if (pc_net_game_dedicated_guest_counts(&gb, &gc)) {
            pc_ded_printf("  guests: bound=%d max_guests=%d\n", gb, gc);
        }
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
    for (g = 0; g < 8; g++) {
        PCNetGameDedicatedGuestInfo gi;
        if (!pc_net_game_dedicated_guest_info(g, &gi)) {
            continue;
        }
        if (n == 0) {
            pc_ded_printf("[DEDICATED] guests (host guest table, all towns; %s):\n", gi.untrusted ? "guests.dat is UNTRUSTED" : "tokens are never printed");
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
    } else if (strcmp(cmd, "guest-remove") == 0) {
        pc_ded_cmd_guest_admin(0, args);
    } else if (strcmp(cmd, "guest-reset-token") == 0) {
        pc_ded_cmd_guest_admin(1, args);
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
            pc_ded_execute(ln.text);
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
