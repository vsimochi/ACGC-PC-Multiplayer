/* pc_platform.h - SDL2/OpenGL platform layer and global state */
#ifndef PC_PLATFORM_H
#define PC_PLATFORM_H

/* 32-bit required: decomp code (JSystem, emu64) casts pointers to u32 */
#include <stdint.h>
/* PC_LOW_ADDRESS_64 (experimental, set by CMake via -DPC_ALLOW_64BIT=ON) builds a 64-bit
 * process linked so every game pointer stays below 4 GB; see pc_lowaddr.h. */
#if UINTPTR_MAX != 0xFFFFFFFFu && !defined(PC_LOW_ADDRESS_64)
#error "This project must be compiled as 32-bit (pointer size != 4 bytes)"
#endif

#define SDL_MAIN_HANDLED
#include <SDL.h>
#include <glad/gl.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>
#include <time.h>

#include "pc_types.h"
#include "pc_lowaddr.h"

/* --- Configuration --- */
#define PC_GC_WIDTH       640
#define PC_GC_HEIGHT      480
#define PC_SCREEN_WIDTH   PC_GC_WIDTH
#define PC_SCREEN_HEIGHT  PC_GC_HEIGHT
#define PC_WINDOW_TITLE   "Animal Crossing"

#define PC_MAIN_MEMORY_SIZE   (24 * 1024 * 1024)
#define PC_ARAM_SIZE          (16 * 1024 * 1024)
#define PC_FIFO_SIZE          (256 * 1024)

#define PC_SPEEDHACK_MULTIPLIER 10.0

#define PC_PI  3.14159265358979323846
#define PC_PIf 3.14159265358979323846f
#define PC_DEG_TO_RAD (PC_PI / 180.0)
#define PC_DEG_TO_RADf (PC_PIf / 180.0f)

/* GC hardware clocks */
#define GC_BUS_CLOCK          162000000u
#define GC_CORE_CLOCK         486000000u
#define GC_TIMER_CLOCK        (GC_BUS_CLOCK / 4)

/* --- Platform headers --- */
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#undef near
#undef far
#else
#include <sys/mman.h>
#include <dlfcn.h>
#include <elf.h>
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* --- Global state --- */
extern SDL_Window*   g_pc_window;
extern SDL_GLContext  g_pc_gl_context;
/* g_pc_running is written from the SetConsoleCtrlHandler thread and from signal(SIGINT/SIGTERM)
 * handlers (pc/src/pc_main.c) and read every frame by the main loop (graph_proc(), src/graph.c).
 * It is declared atomic_int (C11 <stdatomic.h>) so that ordinary reads and plain `= 0` writes at
 * every existing call site compile to atomic load/store with no source changes required anywhere
 * except this header, the definition in pc_main.c, and graph.c's own separate extern.
 *
 * C++ translation units (pc_stubs_cpp.cpp, which includes this header transitively) never read or
 * write this flag; it is declared here as plain `int` under __cplusplus only because this header
 * must still parse under C++, which has no <stdatomic.h> in the C++17 mode this project builds
 * with. On this toolchain (GCC/MinGW-w64, x86/x64) a lock-free atomic_int -- guaranteed lock-free
 * for int-sized objects on this target -- has the same size, alignment, and object representation
 * as a plain int (there is no wrapper struct), so this declaration stays ABI-compatible with the
 * atomic_int definition it refers to via extern "C" linkage. */
#ifdef __cplusplus
extern int           g_pc_running;
#else
#include <stdatomic.h>
extern atomic_int    g_pc_running;
#endif
extern int           g_pc_verbose;
extern int           g_pc_pickup_test_seed; /* Stage 5A.1: --pickup-test-seed -- see pc_net_game.c */
extern int           g_pc_field_action_test_seed; /* World Ecology Stage 1: --field-action-test-seed --
                                                     * see pc_net_game.c's
                                                     * pcnetgame_run_field_action_test_seed() */
extern int           g_pc_bury_test_seed; /* World Ecology T3: --bury-test-seed -- see pc_net_game.c's
                                             * pcnetgame_run_bury_test_seed() */
/* Villager population/is_home milestone, TEST-ONLY: --force-villager-grow / --force-villager-remove.
 * Fire mNpc_DebugForceGrow()/mNpc_DebugForceRemove() (m_npc.c) once, as soon as the host world is
 * ready -- see pc_net_game.c's pcnetgame_run_villager_test_triggers(). Off by default; never active
 * in normal single-player or hosted play. Bypasses only mNpc_CheckGrow()'s/mNpc_ForceRemove()'s real
 * (multi-day) trigger CONDITIONS, never the RNG selection logic itself, and never the host-authority
 * gate -- exercises the exact same gate/notify/wire path a real (rare) trigger would. */
extern int           g_pc_force_villager_grow;
extern int           g_pc_force_villager_remove;
/* Friendship/mail sync milestone, TEST-ONLY: --force-friendship-delta N / --force-mail-send. Fire
 * mNpc_DebugForceFriendshipDelta()/mNpc_DebugForceMailSend() (m_npc.c) once, as soon as this
 * process's own local world is ready (and, on a CLIENT, its link is READY) -- see pc_net_game.c's
 * pcnetgame_run_friendship_mail_test_triggers(). Unlike the villager-population hooks above, these
 * are NOT host-only: they exercise mNpc_AddFriendship()/mNpc_SendMailtoNpc()'s real
 * client-intercept-and-request path exactly as well as the host-apply-and-broadcast path, on
 * whichever role this process is. Off by default; never active in normal single-player or hosted
 * play. g_pc_force_friendship_delta is the signed delta to apply (0 = not requested). */
extern int           g_pc_force_friendship_delta;
extern int           g_pc_force_mail_send;
/* World Ecology Stage 1 money-rock review, TEST-ONLY: --force-money-rock-hit / --force-money-bag-pickup.
 * Give REAL C-executable coverage to the two money-rock paths no other test hook can reach: the HOST's
 * own local hit (pc_net_game_host_local_money_rock_hit()) and the real CLIENT-side wallet-credit/
 * pocket-grant handler (pcnetgame_handle_client_pickup_result()'s money-bag branch). Mirror
 * g_pc_force_villager_grow/g_pc_force_friendship_delta's own exact pattern: off by default, never
 * active in normal single-player or hosted play, fire exactly once as soon as their own precondition
 * is met -- see pc_net_game.c's pcnetgame_run_money_rock_test_triggers().
 *   g_pc_force_money_rock_hit    HOST-only. Once the host world is ready, calls
 *                                 pc_net_game_host_local_money_rock_hit() directly at the
 *                                 --field-action-test-seed fixture's own money-rock tile (24,104) --
 *                                 exactly the real decomp hit seam a local player's tool swing would
 *                                 call, bypassing only the swing/animation itself. Requires
 *                                 --field-action-test-seed too (no seeded rock, no tile to hit).
 *   g_pc_force_money_bag_pickup  CLIENT-only. Once this client's world is synced, scans the fixed
 *                                 9-tile neighborhood around (24,104) (pcnetgame_find_money_rock_drop_tile()'s
 *                                 own candidate set) via the client's own already-applied local field
 *                                 copy (pcfa_get_tile() -- kept current by real FIELD_UPDATE messages,
 *                                 the same data this client's real vanilla pickup targeting would read)
 *                                 for a money-bag item, and calls pc_net_game_request_pickup() on it --
 *                                 the exact real seam m_player_main_pickup.c_inc uses, bypassing only
 *                                 the player's own walk-up-and-swing input. Retries every poll until a
 *                                 bag is found (the host-side hit may not have landed yet). */
extern int           g_pc_force_money_rock_hit;
extern int           g_pc_force_money_bag_pickup;
/* P1 (World Ecology T-dig) real-gameplay verification, TEST-ONLY: --force-dig-hole. CLIENT-only,
 * mirrors g_pc_force_money_bag_pickup's own exact pattern (off by default, fires exactly once as soon
 * as its own precondition is met -- see pc_net_game.c's pcnetgame_run_dig_hole_test_trigger()). Unlike
 * every other force-* hook above, this one does NOT call an existing pc_net_game_*() seam directly --
 * it calls PC_Test_ForceRequestDigScoop() (m_player.c, declared in m_player_lib.h), a thin exported
 * wrapper around the REAL, completely unmodified static Player_actor_request_main_dig_scoop_all()
 * (m_player_main_dig_scoop.c_inc) -- the same request-boundary entry point genuine controller input
 * reaches via Player_actor_CheckAndRequest_main_scoop_all(). Once the request is accepted, the
 * player's own ordinary per-frame main-index update takes over completely (unmodified
 * Player_actor_setup_main_Dig_scoop() / Player_actor_main_Dig_scoop() / Player_actor_Put_Hole_Dig_scoop()),
 * driving the real animation state machine to its real commit point, which (for a host-authoritative
 * client) calls the real pc_net_game_request_dig_hole() network seam exactly as it would from a real
 * shovel swing. Targets the --field-action-test-seed fixture's own DIG_HOLE tile (56,105) -- requires
 * --field-action-test-seed too. Bypasses only raw controller polling and shovel-equip state (this
 * request path does not require a shovel to be out), never any part of the dig/fill/commit logic
 * itself. */
extern int           g_pc_force_dig_hole;
extern int           g_pc_frame_limit_override;
extern int           g_pc_speedhack_enabled;
extern int           g_pc_time_override;
extern int           g_pc_min_override;
extern int           g_pc_sec_override;
extern int           g_pc_date_month;
extern int           g_pc_date_day;
extern int           g_pc_date_year;
extern int           g_pc_weather_override;
extern int           g_pc_weather_intensity_override;
extern u32           g_frame_limiter;

extern int g_pc_window_w;
extern int g_pc_window_h;
void pc_platform_update_window_size(void);

/* --- Widescreen mode (3-state) ---
 * 0 = hor+ (default): full-window viewport, FOV-corrected projection. Resets each frame.
 * 1 = stretch: full-window, no correction. For transitions/inventory BG.
 * 2 = pillarbox: centered 4:3 with black bars. For inventory UI alignment.
 *
 * m_play.c inserts NOOPTag markers in POLY_OPA to switch between states:
 *   ON (0xAC5701) -> 1, OFF (0xAC5700) -> 2. emu64 reads these during DL processing. */
#define PC_NOOP_WIDESCREEN_STRETCH     0xAC5701u
#define PC_NOOP_WIDESCREEN_STRETCH_OFF 0xAC5700u
extern int g_pc_widescreen_stretch;

/* --- Functions --- */
void pc_platform_init(void);
void pc_platform_shutdown(void);
void pc_platform_swap_buffers(void);
int  pc_platform_poll_events(void);

/* EXE image range for seg2k0 pointer disambiguation (vs N64 segment addresses) */
extern unsigned int pc_image_base;
extern unsigned int pc_image_end;

/* --- Model viewer --- */
extern int g_pc_model_viewer;
extern int g_pc_model_viewer_start;
extern int g_pc_model_viewer_no_cull;

/* --- Per-frame diagnostics --- */
extern int pc_emu64_frame_cmds;
extern int pc_emu64_frame_noop_cmds;
extern int pc_emu64_frame_tri_cmds;
extern int pc_emu64_frame_vtx_cmds;
extern int pc_emu64_frame_dl_cmds;
extern int pc_emu64_frame_cull_visible;
extern int pc_emu64_frame_cull_rejected;
extern int pc_gx_draw_call_count;

/* --- Audio --- */
extern int pc_save_loaded;
int  pc_audio_get_buffer_fill(void);
int  pc_audio_is_active(void);
void pc_audio_set_paused(int paused);
void pc_audio_shutdown(void);
void pc_audio_start_producer_thread(void);
void pc_audio_mq_init(void);
void pc_audio_mq_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* PC_PLATFORM_H */
