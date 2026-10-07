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
#include <stddef.h>
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
/* First launch of a dedicated server (no authoritative town yet): asks on the server console for the town name (1-8 valid characters, re-asks until valid), SYNCHRONOUSLY,
 * through the SAME stdin reader / queue the console commands use (no second reader thread). Called once after pc_dedicated_startup(), before the game boots. When stdin is
 * closed / unavailable (EOF before a valid name) the fallback name "Village" is used. The chosen name goes to pc_server_set_town_name(). */
void pc_dedicated_prompt_town_name(void);
/* One console line read synchronously from the server console (interactive window or redirected stdin); 1 = line, 0 = EOF. Used by the pre-boot legacy-town question. */
int  pc_dedicated_read_line_blocking(char* buf, size_t cap);

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
typedef struct PCNetGameDedicatedGuestAdmission {
    int configured;   /* max_guests */
    int effective;    /* min(max_guests, transport capacity) */
    int bound;        /* guests bound now */
    int stored;       /* guests in the store */
    int store_cap;    /* guest entries allocated */
    int store_budget; /* the most guest_memory_mb allows */
    int untrusted;    /* the guest store refuses new guests */
    int allow_new;    /* allow_new_guests */
} PCNetGameDedicatedGuestAdmission;
int  pc_net_game_dedicated_guest_admission(PCNetGameDedicatedGuestAdmission* out); /* capacity phase 4: 1 iff HOST */
/* Capacity phases 5 + 6: how the host's scale machinery is doing (all counters since the host started). */
typedef struct PCNetGameDedicatedScale {
    int      interest_on;                                      /* settings.ini interest_management */
    unsigned move_relayed[4], move_thinned[4];                 /* MOVE samples by the receiver's tier: NEAR / MID / FAR / APART */
    unsigned roster_sent, roster_gone_sent, roster_blocked, roster_unavail;
    int      roster_owed;                                      /* roster entries + departures still owed to READY peers */
    int      max_backlog, backlog_now_max;                     /* reliable window use: highest ever / highest right now (of PC_NET_RELIABLE_WINDOW) */
    int      puppet_slots, puppet_slots_peak, puppet_slot_failures;
    int      puppets_tracked, puppet_actors, puppet_actors_pending, puppet_actors_blocked;
    unsigned puppet_actor_create_failed;
    int      actor_peak, actor_max, collide_armed, collider_peak, collider_table, failed_setoc;
} PCNetGameDedicatedScale;
int  pc_net_game_dedicated_scale(PCNetGameDedicatedScale* out); /* 1 iff HOST */
int  pc_net_game_dedicated_guest_counts(int* bound, int* cap); /* G4: guests bound now / the max_guests cap; 1 iff HOST */
int  pc_net_game_dedicated_capacity(int* peers_used, int* peers_total, int* resident_reserve); /* transport slots used / total / held for absent residents; 1 iff HOST */
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
/* M-E: resident credentials (HOST only, main thread). `residents` lists through _resident_info (never a token); _resident_admin: op 0 = resident-reset (delete the credential), 1 = resident-arm
 * (allow ONE mint under resident_tokens=required: memory only, 10 minutes, one use). Returns 1 = done, 2 = nothing changed (no `confirm`: msg says what would happen), 0 = refused (msg says why).
 * Order of checks like guest-remove: world ready, members.dat not UNTRUSTED, selector, refused while the resident is connected, the `confirm` word, members.dat.bak-<timestamp> (reset only), then the change. */
typedef struct PCNetGameDedicatedResidentInfo {
    int  slot;
    char name[PC_NETGAME_NAME_LEN + 1];
    int  policy;      /* 0 off, 1 tofu, 2 required */
    int  untrusted;   /* members.dat is UNTRUSTED */
    int  has_cred;    /* a credential is stored for this resident (keyed by its PersonalID in the host town) */
    int  confirmed;   /* the client presented the token at least once */
    int  armed;       /* resident-arm pending (required) */
    int  bound_peer;  /* transport peer the resident is connected on, -1 = not bound */
} PCNetGameDedicatedResidentInfo;
int  pc_net_game_dedicated_resident_info(int slot, PCNetGameDedicatedResidentInfo* out); /* 1 iff the slot holds a resident (HOST only) */
int  pc_net_game_dedicated_resident_admin(int op, const char* sel, int confirm, char* msg, size_t cap);
/* M-F: `promote <guest> <slot|auto> <house|auto> confirm` (HOST only, main thread). Checks like guest-remove (world ready, guests.dat / members.dat trusted, ONE guest of this town, offline, a free
 * resident slot + a free house, unique resident name, room in members.dat, the `confirm` word, backups of guests.dat / members.dat / records.dat), then: new resident + house, records lineage rev 1,
 * members.dat (resident credential + PROMOTION_HANDOFF), the town save, and LAST the removal of the guests.dat entry. Returns 1 = done, 2 = nothing changed (no `confirm`: msg says what would
 * happen), 0 = REFUSED (msg says why; nothing was changed). Never prints a token. */
int  pc_net_game_dedicated_promote(const char* guest_sel, const char* slot_sel, const char* house_sel, int confirm, char* msg, size_t cap);
/* M2: `members` -- residents + active guests of the host town through pc_mp_membership_list(). kind = PC_MP_MEMBER_* (1 resident, 2 guest, 3 ambiguous);
 * slot = resident index or guest table slot (is_guest_row says which). Returns the number of rows (0 unless HOST with a loaded guest store). */
typedef struct PCNetGameDedicatedMemberInfo {
    int  kind;
    int  slot;
    int  is_guest_row;
    int  confirmed;
    char name[PC_NETGAME_NAME_LEN + 1];
    char home_town[PC_NETGAME_NAME_LEN + 1];
} PCNetGameDedicatedMemberInfo;
int  pc_net_game_dedicated_members(PCNetGameDedicatedMemberInfo* rows, int cap);
int  pc_net_game_dedicated_guest_slots(void); /* guest table entries allocated right now (the `guests` console loops 0..n-1); 0 unless a HOST has loaded the store */
/* Host admin item tools (HOST only, main thread, no wire change). _item_giveblock: NULL when `id` may be given, else a short reason (invalid pocket id, money, tickets,
 * my-design items, furniture id with rotation bits). _give: fill `qty` (1..15) EMPTY pockets of a resident / guest (name or `peer N`) with `id`, all-or-nothing; 1 = given,
 * 0 = refused (msg = the text that follows "GIVE FAILED: "). A connected owner receives a PUSH_FULL record push (client-unsynced edits are discarded on adoption). */
int  pc_net_game_dedicated_item_is_legal(unsigned id); /* pcnetgame_is_pocket_legal_item(), the record validator's rule (id != 0) */
const char* pc_net_game_dedicated_item_giveblock(unsigned id);
int  pc_net_game_dedicated_give(const char* sel, unsigned id, int qty, char* who_out, size_t whocap, char* msg, size_t cap);
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
