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

void pc_dedicated_say(const char* fmt, ...) {
    va_list ap;
    if (!g_pc_dedicated) {
        return;
    }
    fputs("[DEDICATED] ", stdout);
    va_start(ap, fmt);
    vfprintf(stdout, fmt, ap);
    va_end(ap);
    fputc('\n', stdout);
    fflush(stdout);
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

void pc_dedicated_early_console(void) {
#ifdef _WIN32
    int attached = AttachConsole(ATTACH_PARENT_PROCESS) ? 1 : 0; /* fails harmlessly: no parent console / already attached (console build) */
    int in_ok = pc_ded_fd_valid(0);
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
    if (!pc_ded_fd_valid(0)) {
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
        fprintf(stderr, "[DEDICATED] FATAL: the SDL dummy audio driver is not the one that opened (driver=%s); refusing to run a dedicated server with "
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
        fprintf(stderr, "[DEDICATED] FATAL: the dummy audio device could not be opened (%s); a stalled audio producer could block fades and message "
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
    printf("[DEDICATED] commands (case-insensitive):\n"
           "  help     list the commands\n"
           "  status   server status: observer, network role/port, world+save readiness, uptime, peers, last save\n"
           "  players  connected players: peer slot, link, class (RESIDENT idx / GUEST slot), name, puppet, idle ms\n"
           "  save     request an authoritative save at the next safe point (prints 'save: OK', 'save: FAILED (...)' or 'save: refused: ...')\n"
           "  stop     graceful shutdown (final save, close networking and platform, exit 0); aliases: quit, exit\n");
    fflush(stdout);
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
    printf("[DEDICATED] status\n");
    printf("  dedicated: yes\n");
    printf("  observer: active=%s ready=%s\n", pc_host_observer_active() ? "yes" : "no", pc_host_observer_ready() ? "yes" : "no");
    printf("  network: role=%s listening_port=%u\n", pc_ded_role_name(), (unsigned)s_port);
    printf("  world ready: %s\n", pc_net_game_dedicated_world_ready() ? "yes" : "no");
    printf("  save ready: %s\n", pcfa_save_ready() ? "yes" : "no");
    printf("  uptime: %lus (%luh %02lum %02lus), frames=%lu, avg_fps=%.1f\n", up_s, up_s / 3600ul, (up_s / 60ul) % 60ul, up_s % 60ul, frames,
           up_ms > 0 ? (double)frames * 1000.0 / (double)up_ms : 0.0);
    if (g_frame_limiter > 0) {
        printf("  frame limit: %lu Hz\n", (unsigned long)g_frame_limiter);
    } else {
        printf("  frame limit: none (uncapped)\n");
    }
    printf("  peers: connected=%d ready=%d\n", transport, ready);
    if (s_last_save_ok < 0) {
        printf("  last save: none yet\n");
    } else {
        printf("  last save: %s %lus ago\n", s_last_save_ok ? "OK" : "FAILED", (unsigned long)((SDL_GetTicks() - s_last_save_ticks) / 1000u));
    }
    printf("  audio: driver=%s ring_fill=%d consumed_samples=%d\n", s_audio_ok ? s_audio_driver : "?", pc_audio_get_buffer_fill(), pc_audio_consumed_samples());
    fflush(stdout);
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
        printf("[DEDICATED] no players connected\n");
        fflush(stdout);
        return;
    }
    printf("[DEDICATED] players: %d\n", listed);
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
        printf("  peer %d: link=%s class=%s name=\"%s\" puppet=%s idle=%dms\n", pi.peer, pc_ded_link_name(pi.link), cls, pi.name,
               pi.puppet == 2 ? "live" : pi.puppet == 1 ? "pending" : "none", pi.idle_ms);
    }
    fflush(stdout);
}

static void pc_ded_cmd_stop(void) {
    if (s_stop_requested) {
        printf("[DEDICATED] already stopping...\n");
    } else {
        s_stop_requested = 1;
        printf("[DEDICATED] stopping...\n");
        PC_LOG(PCL_GENERAL, "dedicated: console stop requested\n");
        g_pc_running = 0; /* same flag as Ctrl+C: the normal shutdown path (final save once, net shutdown, platform shutdown) follows */
    }
    fflush(stdout);
}

static void pc_ded_execute(char* line) {
    char* p = line;
    char* end;
    char* cmd;
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
    }
    if (strcmp(cmd, "help") == 0 || strcmp(cmd, "?") == 0) {
        pc_ded_cmd_help();
    } else if (strcmp(cmd, "status") == 0) {
        pc_ded_cmd_status();
    } else if (strcmp(cmd, "players") == 0) {
        pc_ded_cmd_players();
    } else if (strcmp(cmd, "save") == 0) {
        if (s_save_pending) {
            printf("[DEDICATED] save: already requested (waiting for the next safe point)\n");
        } else {
            s_save_pending = 1;
            printf("[DEDICATED] save: requested (runs at the next safe point; the result follows)\n");
        }
        fflush(stdout);
    } else if (strcmp(cmd, "stop") == 0 || strcmp(cmd, "quit") == 0 || strcmp(cmd, "exit") == 0) {
        pc_ded_cmd_stop();
    } else {
        printf("[DEDICATED] unknown command: %s (type help)\n", cmd);
        PC_LOG(PCL_GENERAL, "dedicated: unknown console command '%s'\n", cmd);
        fflush(stdout);
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
                    printf("[DEDICATED] warning: console input queue was full, %d line(s) dropped\n", dropped);
                    fflush(stdout);
                }
                break;
            }
            ln = s_queue[s_q_head];
            s_q_head = (s_q_head + 1) % PC_DED_QUEUE_MAX;
            s_q_count--;
            s_q_count_hint = s_q_count;
            PC_DED_UNLOCK();
            if (ln.truncated) {
                printf("[DEDICATED] warning: console line truncated to %d characters\n", PC_DED_LINE_MAX);
            }
            pc_ded_execute(ln.text);
        }
    }
    if (s_stdin_eof && !s_stdin_eof_reported) {
        s_stdin_eof_reported = 1;
        printf("[DEDICATED] console input closed (EOF); the server keeps running (stop it with Ctrl+C or a termination request)\n");
        PC_LOG(PCL_GENERAL, "dedicated: console input closed (EOF), server keeps running\n");
        fflush(stdout);
    }
}

int pc_dedicated_save_request_pending(void) {
    return g_pc_dedicated && s_save_pending;
}

void pc_dedicated_save_report(int ok, const char* refusal_reason) {
    s_save_pending = 0;
    if (refusal_reason != NULL) {
        printf("[DEDICATED] save: refused: %s\n", refusal_reason);
        PC_LOG(PCL_GENERAL, "dedicated: console save refused: %s\n", refusal_reason);
    } else if (ok) {
        printf("[DEDICATED] save: OK\n");
    } else {
        printf("[DEDICATED] save: FAILED (the authoritative write reported an error; see the log)\n");
    }
    fflush(stdout);
}
