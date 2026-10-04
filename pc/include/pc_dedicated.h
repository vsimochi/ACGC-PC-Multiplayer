/* pc_dedicated.h - the opt-in DEDICATED SERVER mode (--dedicated, HOST role only).
 *
 * `AnimalCrossing.exe --host [port] --dedicated` is a hosted process meant to run unattended:
 *   - it implies --host-observer: the SAME observer init is reused (pc_main.c sets g_pc_host_observer; there is no second init), so the host plays
 *     no resident and all four residents are claimable by clients;
 *   - the window is created HIDDEN (SDL_WINDOW_HIDDEN, never shown; a GL 3.3 context still exists on it), vsync is off;
 *   - nothing is drawn: the per-frame display-list interpretation (emu64_taskstart), pc_gx_begin_frame, pc_gx_draw_pending and the buffer swap are
 *     skipped. Game logic, JW_EndFrame/VIWaitForRetrace (network poll, saves, 60 Hz pacing) and audio state run exactly as in a normal host;
 *   - audio uses SDL's DUMMY driver (selected before SDL_Init) so the audio state machine keeps advancing and the producer thread keeps draining;
 *     startup FAILS if the dummy device cannot be opened (a stalled producer could stall fades / message waits);
 *   - the console is always on (never redirected to NUL, unbuffered); the parent console is attached (or one allocated) when the std handles
 *     are invalid, and a stdin reader thread feeds a small bounded queue that the MAIN thread drains once per frame (pc_dedicated_console_poll).
 *
 * Everything here is inert unless g_pc_dedicated == 1; plain --host, --host-observer, --bootstrap-resident, clients and single-player never
 * reach any of it. See pc_dedicated.c for the command set. */
#ifndef PC_DEDICATED_H
#define PC_DEDICATED_H

#include <stdint.h>
#include "pc_net_game.h" /* PC_NETGAME_NAME_LEN */

#ifdef __cplusplus
extern "C" {
#endif

/* pc_main.c: 1 iff --dedicated was passed AND validated (only with --host, never with --connect / --bootstrap-resident / --bootstrap-guest /
 * the host-self test hooks). Default 0. */
extern int g_pc_dedicated;

/* Windows only (no-op elsewhere): attach to the parent console (so Ctrl+C / Ctrl+Break reach the handler and the terminal shows output), and when
 * stdin/stdout/stderr are not valid handles (a -mwindows exe started without redirection) allocate a console if none could be attached and reopen
 * the invalid ones on CONIN$/CONOUT$. Valid (redirected) std handles are NEVER replaced. Called from the argument parser as soon as --dedicated is
 * seen, so even the refusal text is visible. */
void pc_dedicated_early_console(void);
/* 1 when the server console is its OWN window (interactive launch of the GUI-subsystem exe): stdout / stderr then go to NUL (non-verbose) so debug printf noise
 * cannot tear the line being typed; [DEDICATED] lines and command output use the console stream directly. 0 for redirected runs / console builds. */
int  pc_dedicated_stdout_quiet(void);

/* pc_main.c, after the output routing is decided: remember the listen port, print the "[DEDICATED] ..." startup line, start the stdin reader. */
void pc_dedicated_startup(uint16_t port);

/* pc_platform_init(): BEFORE SDL_Init select SDL's dummy audio driver (env + OVERRIDE hint) and keep timer resolution requests honoured for a hidden
 * window; AFTER SDL_Init verify the dummy driver is the one that opened (returns 0 = refuse to continue). */
void pc_dedicated_pre_sdl_init(void);
int  pc_dedicated_post_sdl_init(void);
/* pc_platform_init() end: logs the platform summary (window hidden? vsync off, audio driver, what rendering is skipped) and starts the uptime clock. */
void pc_dedicated_platform_summary(void);
/* pc_audio.c AIInit: the audio device opened (or did not). A failed open in dedicated mode ends the process with exit code 1. */
void pc_dedicated_audio_opened(int ok, int freq, int channels, int samples);

/* pc_vi.c, once per frame next to pc_net_game_poll(): drain the stdin queue and execute the commands (help, status, players, save, stop). */
void pc_dedicated_console_poll(void);
/* pc_vi.c periodic-save block: a console `save` was requested and not yet handled / handle it. The caller applies all save gates, runs
 * pc_save_write_authoritative() and reports through pc_dedicated_save_report(). */
int  pc_dedicated_save_request_pending(void);
void pc_dedicated_save_report(int ok, const char* refusal_reason);

/* --dedicated console accessors implemented in pc_net_game.c (read-only, main thread only; they return 0 / do nothing unless this process is the HOST).
 * They live HERE, not in pc_net_game.h, so the wire header stays byte-identical to HEAD (wire_baseline). */
typedef struct PCNetGameDedicatedPeerInfo {
    int  peer;     /* transport slot */
    int  link;     /* PCNetGameLinkState */
    int  bound;    /* 1 once the host bound the peer to a resident / guest slot (READY) */
    int  cls;      /* 0 = RESIDENT, 1 = GUEST (valid when bound) */
    int  index;    /* resident idx (RESIDENT) or guest slot (GUEST); -1 when unbound */
    char name[PC_NETGAME_NAME_LEN + 1]; /* the peer's PersonalID name as ASCII ('?' for non-ASCII font codes); NEVER the guest token */
    int  idle_ms;  /* pc_net_peer_idle_ms() */
    int  puppet;   /* 0 none, 1 pending creation, 2 live (pc_remote_player_puppet_state) */
} PCNetGameDedicatedPeerInfo;
int  pc_net_game_dedicated_world_ready(void);   /* HOST && s_host_world_ready */
int  pc_net_game_dedicated_peer_slots(void);    /* number of transport slots to iterate (0 unless HOST) */
int  pc_net_game_dedicated_peer_info(int slot, PCNetGameDedicatedPeerInfo* out); /* 1 iff the slot is not DISCONNECTED */
int  pc_net_game_dedicated_guest_counts(int* bound, int* cap); /* G4: guests bound now / the max_guests cap; 1 iff HOST */
/* Guests G6.2: host operator tools (HOST only, main thread). `guests` lists through _guest_info (never the token); _guest_admin: op 0 = remove, 1 = reset-token.
 * Returns 1 = done, 2 = nothing changed (no `confirm`: msg says what would happen), 0 = refused (msg says why). Both refuse while the guest is bound / guests.dat is
 * UNTRUSTED, and back guests.dat up (guests.dat.bak-<timestamp>) before changing anything. */
typedef struct PCNetGameDedicatedGuestInfo {
    int  slot;
    char name[PC_NETGAME_NAME_LEN + 1];      /* the guest's player name (ASCII, '?' for other font codes) */
    char home_town[PC_NETGAME_NAME_LEN + 1]; /* the guest's HOME town name */
    char town[PC_NETGAME_NAME_LEN + 1];      /* the HOST town this entry belongs to */
    int  active;     /* 1 = the entry belongs to this host's current town */
    int  confirmed;
    unsigned rev;    /* 0 = the first upload never completed */
    int  bound_peer; /* transport peer the guest is connected on, -1 = not bound */
    int  recovery;   /* 1 = an operator token recovery is armed */
    int  untrusted;  /* guests.dat is UNTRUSTED */
} PCNetGameDedicatedGuestInfo;
int  pc_net_game_dedicated_guest_info(int slot, PCNetGameDedicatedGuestInfo* out); /* 1 iff the slot is in use */
int  pc_net_game_dedicated_guest_admin(int op, const char* sel, int confirm, char* msg, size_t cap);
/* what: 0 = transport-connected, 1 = READY (bound), 2 = disconnected (call BEFORE the per-peer state is reset). Prints one "[DEDICATED] ..." line. */
void pc_net_game_dedicated_announce(int peer, int what);

/* Always-on "[DEDICATED] ..." console line (dedicated mode only; main thread). */
void pc_dedicated_say(const char* fmt, ...)
#if defined(__GNUC__)
    __attribute__((format(printf, 1, 2)))
#endif
    ;
/* pc_save_write_authoritative wrapper: every save result (periodic / early / shutdown / console) lands here. */
void pc_dedicated_notify_save_result(int ok);
/* src/main.c shutdown path: what the final save did ("final save OK", "final save FAILED", "final save skipped (world not ready)", "complete"). */
void pc_dedicated_notify_shutdown(const char* what);

#ifdef __cplusplus
}
#endif

#endif /* PC_DEDICATED_H */
