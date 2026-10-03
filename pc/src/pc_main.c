/* pc_main.c - PC entry point: SDL2/GL init and boot sequence */
#include "pc_platform.h"
#include "pc_gx_internal.h"
#include "pc_texture_pack.h"
#include "pc_settings.h"
#include "pc_keybindings.h"
#include "pc_assets.h"
#include "pc_disc.h"
#include "pc_typing.h"
#include "pc_pause_menu.h"
#include "pc_settings_menu.h"
#include "pc_profiler.h"
#include "pc_net_game.h"
#include "pc_rng_domains_selftest.h"
#include "m_kankyo.h"

/* prefer discrete GPU on laptops */
#ifdef _WIN32
__declspec(dllexport) unsigned long NvOptimusEnablement = 1;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
#endif

SDL_Window*   g_pc_window = NULL;
SDL_GLContext  g_pc_gl_context = NULL;
atomic_int    g_pc_running = 1;
int           g_pc_frame_limit_override = -1;
int           g_pc_speedhack_enabled = 0;
int           g_pc_verbose = 0;
int           g_pc_time_override = -1; /* -1=system clock, 0-23=override hour */
int           g_pc_min_override = -1; /* -1=system clock, 0-59=override minute */
int           g_pc_sec_override = -1; /* -1=system clock, 0-59=override second */
int           g_pc_date_month = -1; /* -1=system clock, 1-12=override month */
int           g_pc_date_day = -1; /* -1=system clock, 1-31=override day */
int           g_pc_date_year = -1; /* -1=system clock, else override year */
int           g_pc_weather_override = -1;
int           g_pc_weather_intensity_override = mEnv_WEATHER_INTENSITY_HEAVY;
int           g_pc_window_w = PC_SCREEN_WIDTH;
int           g_pc_window_h = PC_SCREEN_HEIGHT;
int           g_pc_widescreen_stretch = 0;

/* exe image range -- used by seg2k0 to distinguish pointers from segment addresses */
unsigned int pc_image_base = 0;
unsigned int pc_image_end  = 0;

#ifdef _WIN32
#include <signal.h>

/* Stage M1-2: headless graceful termination. Sets the SAME shutdown flag the existing SDL_QUIT
 * handling already uses (pc_platform_poll_events, below) -- the actual final save still only
 * ever runs later, on the normal single-threaded shutdown path in src/main.c, once graph_proc()
 * notices g_pc_running==0 and returns. Neither callback below touches the save path, the network
 * layer, game state, printf, malloc, or file I/O -- each sets exactly one existing int and
 * returns immediately.
 *
 * SetConsoleCtrlHandler is the authoritative Windows mechanism for exactly this: its callback
 * fires (per Windows' own documented behavior, on a separate OS-created thread -- not a POSIX
 * signal context) for CTRL_C_EVENT (Ctrl+C), CTRL_BREAK_EVENT (Ctrl+Break), CTRL_CLOSE_EVENT
 * (console window closed / an ordinary, non-forceful stop request), and CTRL_LOGOFF_EVENT /
 * CTRL_SHUTDOWN_EVENT (user logoff / system shutdown) -- covering every realistic headless-server
 * stop request in one place. plain SIGINT/SIGTERM are also registered via the C runtime's own
 * signal(), for the rarer case something raises those directly rather than going through the
 * console control mechanism; both call the exact same minimal flag-set.
 *
 * g_pc_running is declared atomic_int (C11 <stdatomic.h>, see pc_platform.h) rather than a plain
 * int: the console-ctrl callback above is a genuine cross-thread write (a real second OS thread,
 * not merely signal-context reentrancy on the main thread), so this needs real cross-thread
 * visibility, not just signal-safety. atomic_int gives that -- a sequentially-consistent load/store
 * on every read (graph_proc()'s loop condition) and write (here, and in the SDL_QUIT handling just
 * below) -- while remaining an ordinary lock-free int-sized access on this toolchain (GCC/MinGW-w64,
 * x86/x64), so no source changes are needed at any of the existing plain `g_pc_running = 0;` call
 * sites elsewhere in the codebase (pc_os.c, pc_pause_menu.c, pc_vi.c, ac_animal_logo.c) -- ordinary
 * assignment and read syntax on an atomic_int compiles to the atomic operation automatically. */
static BOOL WINAPI pc_console_ctrl_handler(DWORD ctrl_type) {
    switch (ctrl_type) {
        case CTRL_C_EVENT:
        case CTRL_BREAK_EVENT:
        case CTRL_CLOSE_EVENT:
        case CTRL_LOGOFF_EVENT:
        case CTRL_SHUTDOWN_EVENT:
            g_pc_running = 0;
            return TRUE; /* handled: suppress the default action (immediate termination) so the
                          * normal shutdown path gets a chance to run first */
        default:
            return FALSE;
    }
}

static void pc_signal_handler(int sig) {
    (void)sig;
    g_pc_running = 0;
}
#endif

void pc_platform_init(void) {
#ifdef _WIN32
    SetProcessDPIAware();
    SDL_SetHint(SDL_HINT_WINDOWS_INTRESOURCE_ICON, "1");
    SetConsoleCtrlHandler(pc_console_ctrl_handler, TRUE);
    signal(SIGINT, pc_signal_handler);
    signal(SIGTERM, pc_signal_handler);
#endif
    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_GAMECONTROLLER | SDL_INIT_AUDIO | SDL_INIT_TIMER) < 0) {
        fprintf(stderr, "SDL_Init failed: %s\n", SDL_GetError());
        exit(1);
    }

    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 3);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 3);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_CORE);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 1);
    SDL_GL_SetAttribute(SDL_GL_DEPTH_SIZE, 24);
#ifdef PC_ENHANCEMENTS
    if (g_pc_settings.msaa > 0) {
        SDL_GL_SetAttribute(SDL_GL_MULTISAMPLEBUFFERS, 1);
        SDL_GL_SetAttribute(SDL_GL_MULTISAMPLESAMPLES, g_pc_settings.msaa);
    }
#endif

    {
        Uint32 flags = SDL_WINDOW_OPENGL | SDL_WINDOW_SHOWN | SDL_WINDOW_RESIZABLE;
        int win_w = g_pc_settings.window_width;
        int win_h = g_pc_settings.window_height;
        if (g_pc_settings.fullscreen == 1) {
            flags |= SDL_WINDOW_FULLSCREEN;
        } else if (g_pc_settings.fullscreen == 2) {
            flags |= SDL_WINDOW_FULLSCREEN_DESKTOP;
        }
        g_pc_window = SDL_CreateWindow(
            PC_WINDOW_TITLE,
            SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
            win_w, win_h, flags
        );
    }
    if (!g_pc_window) {
        fprintf(stderr, "SDL_CreateWindow failed: %s\n", SDL_GetError());
        SDL_Quit();
        exit(1);
    }

    g_pc_gl_context = SDL_GL_CreateContext(g_pc_window);
    if (!g_pc_gl_context) {
        fprintf(stderr, "SDL_GL_CreateContext failed: %s\n", SDL_GetError());
        SDL_DestroyWindow(g_pc_window);
        SDL_Quit();
        exit(1);
    }

    if (!gladLoadGL((GLADloadfunc)SDL_GL_GetProcAddress)) {
        fprintf(stderr, "gladLoadGL failed\n");
        SDL_GL_DeleteContext(g_pc_gl_context);
        SDL_DestroyWindow(g_pc_window);
        SDL_Quit();
        exit(1);
    }

    SDL_GL_SetSwapInterval(g_pc_settings.vsync);

    pc_platform_update_window_size();

#ifdef PC_ENHANCEMENTS
    if (g_pc_settings.msaa > 0) {
        glEnable(GL_MULTISAMPLE);
    }
#endif

    pc_gx_init();
    pc_texture_pack_init();
#ifdef PC_ENHANCEMENTS
    if (g_pc_settings.preload_textures) {
        pc_texture_pack_preload_all();
    }
#endif
}

extern void PADCleanup(void);

static void pc_speedhack_toggle(void) {
    g_pc_speedhack_enabled = !g_pc_speedhack_enabled;
    if (g_pc_window != NULL) {
        SDL_SetWindowTitle(g_pc_window, g_pc_speedhack_enabled ? "Animal Crossing [5x]" : PC_WINDOW_TITLE);
    }

    if (g_pc_verbose) {
        printf("[PC] speedhack %s\n", g_pc_speedhack_enabled ? "5x" : "off");
    }
}

void pc_platform_shutdown(void) {
    pc_audio_shutdown();
    pc_audio_mq_shutdown();
    PADCleanup();
    pc_texture_pack_shutdown();
    pc_gx_shutdown();

    if (g_pc_gl_context) {
        SDL_GL_DeleteContext(g_pc_gl_context);
        g_pc_gl_context = NULL;
    }
    if (g_pc_window) {
        SDL_DestroyWindow(g_pc_window);
        g_pc_window = NULL;
    }
    SDL_Quit();
}

void pc_platform_update_window_size(void) {
    SDL_GL_GetDrawableSize(g_pc_window, &g_pc_window_w, &g_pc_window_h);
    if (g_pc_window_w <= 0) g_pc_window_w = PC_SCREEN_WIDTH;
    if (g_pc_window_h <= 0) g_pc_window_h = PC_SCREEN_HEIGHT;
}

void pc_platform_swap_buffers(void) {
    pc_gx_draw_pending();
    SDL_GL_SwapWindow(g_pc_window);
}

int pc_platform_poll_events(void) {
    SDL_Event event;

    pc_typing_update();

    while (SDL_PollEvent(&event)) {
        switch (event.type) {
            case SDL_QUIT:
                g_pc_running = 0;
                return 0;
            case SDL_WINDOWEVENT:
                if (event.window.event == SDL_WINDOWEVENT_SIZE_CHANGED) {
                    pc_platform_update_window_size();
                }
                break;
            case SDL_KEYDOWN:
                /* Keybinding capture eats all input first (works from both
                 * the pause menu and the title Options menu). */
                if (pc_settings_menu_capture_active()) {
                    pc_settings_menu_handle_capture_event(&event);
                    break;
                }
                if (event.key.keysym.sym == SDLK_F3 && !event.key.repeat) {
                    pc_speedhack_toggle();
                    break;
                }
                if (event.key.keysym.sym == SDLK_ESCAPE && !event.key.repeat) {
                    if (g_pc_paused) {
                        pc_pause_menu_handle_event(&event);
                    } else {
                        pc_pause_menu_toggle();
                    }
                    break;
                }
                if (g_pc_paused) {
                    pc_pause_menu_handle_event(&event);
                    break; /* swallow all keys while paused */
                }
                pc_typing_handle_event(&event);
                break;
            case SDL_MOUSEBUTTONDOWN:
                if (pc_settings_menu_capture_active()) {
                    pc_settings_menu_handle_capture_event(&event);
                }
                break;
            case SDL_CONTROLLERBUTTONDOWN:
                if (pc_settings_menu_capture_active()) {
                    pc_settings_menu_handle_capture_event(&event);
                    break;
                }
                if (g_pc_paused) {
                    pc_pause_menu_handle_event(&event);
                    break;
                }
                /* Back/Select opens the pause menu (controller Esc). */
                if (event.cbutton.button == SDL_CONTROLLER_BUTTON_BACK) {
                    pc_pause_menu_toggle();
                }
                break;
            case SDL_CONTROLLERAXISMOTION:
                if (pc_settings_menu_capture_active()) {
                    pc_settings_menu_handle_capture_event(&event);
                    break;
                }
                if (g_pc_paused) {
                    pc_pause_menu_handle_event(&event);
                }
                break;
            case SDL_TEXTINPUT:
                if (g_pc_paused) break;
                pc_typing_handle_event(&event);
                break;
        }
    }
    return 1;
}

/* game's main() renamed to ac_entry via -Dmain=ac_entry, boot.c's to boot_main */
extern void ac_entry(void);
extern int boot_main(int argc, const char** argv);

static int pc_parse_rain_intensity(const char* text) {
    if (strcmp(text, "light") == 0) {
        return mEnv_WEATHER_INTENSITY_LIGHT;
    }

    if (strcmp(text, "normal") == 0) {
        return mEnv_WEATHER_INTENSITY_NORMAL;
    }

    if (strcmp(text, "heavy") == 0) {
        return mEnv_WEATHER_INTENSITY_HEAVY;
    }

    return -1;
}

#ifdef PC_LOW_ADDRESS_64
static int g_pc_lowaddr_selftest = 0; /* --lowaddr-selftest: exercise the real allocators without a ROM */
#endif

/* --rng-selftest: regression test for the RNG-domain-separation foundation (libc64/qrand_domains.h)
 * and for the pre-existing global qrand()/fqrand() stream (libc64/qrand.h), without needing a ROM.
 * Not gated behind PC_LOW_ADDRESS_64 -- unlike --lowaddr-selftest this exercises plain game-side
 * RNG code, unrelated to the 64-bit low-address-space experiment. */
static int g_pc_rng_selftest = 0;

/* Stage 5A.1: --pickup-test-seed. Test-only, off by default, never active in normal single-player
 * or hosted play -- see pc_net_game.c's own use of this flag (pcnetgame_run_pickup_test_seed())
 * for exactly what it does and why it exists (this save's field data has no naturally-occurring
 * loose items, and the pickup accept-path regression test has no other way to establish one
 * without a fixture). Global (not static), matching g_pc_verbose's own exact pattern
 * (pc_platform.h), so pc_net_game.c can read it without any new coupling to pc_main.c. */
int g_pc_pickup_test_seed = 0;

/* World Ecology Stage 1: --field-action-test-seed. Test-only, off by default, never active in normal
 * single-player or hosted play -- mirrors g_pc_pickup_test_seed's own exact pattern/reasoning above,
 * for the same reason: this save's field data has no guaranteed naturally-occurring buried item or
 * money rock, and the DIG_BURIED/MONEY_ROCK_HIT regression tests have no other way to establish one
 * without a fixture. See pc_net_game.c's pcnetgame_run_field_action_test_seed(). */
int g_pc_field_action_test_seed = 0;

/* World Ecology T3: --bury-test-seed. Test-only, off by default, never active in normal single-player
 * or hosted play -- mirrors g_pc_field_action_test_seed's own exact pattern/reasoning above: the
 * BURY_REQUEST/RESULT regression tests need a guaranteed HOLE00../HOLE_SHINE tile the same way the
 * dig-family tests need a guaranteed hole/pitfall/rock. See pc_net_game.c's
 * pcnetgame_run_bury_test_seed(). */
int g_pc_bury_test_seed = 0;

/* World Ecology Wildlife Sync T0/T1 gate: --authoritative-wildlife. Off (0) by default. See
 * pc_platform.h's own doc comment on this global for why it is a persistent MODE flag (like --host/
 * --connect) rather than a one-shot TEST trigger, and exactly what falls through to pre-T0 vanilla
 * behavior when it is not passed. */
int g_pc_authoritative_wildlife = 0;

/* Villager population/is_home milestone, TEST-ONLY: --force-villager-grow / --force-villager-remove.
 * See pc_platform.h's own doc comment on these two globals. */
int g_pc_force_villager_grow = 0;
int g_pc_force_villager_remove = 0;

/* Friendship/mail sync milestone, TEST-ONLY: --force-friendship-delta N / --force-mail-send. See
 * pc_platform.h's own doc comment on these two globals. */
int g_pc_force_friendship_delta = 0;
int g_pc_force_mail_send = 0;

/* World Ecology Stage 1 money-rock review, TEST-ONLY: --force-money-rock-hit / --force-money-bag-pickup.
 * See pc_platform.h's own doc comment on these two globals. */
int g_pc_force_money_rock_hit = 0;
int g_pc_force_money_bag_pickup = 0;

/* P1 (World Ecology T-dig) real-gameplay verification, TEST-ONLY: --force-dig-hole. See
 * pc_platform.h's own doc comment on this global. */
int g_pc_force_dig_hole = 0;

/* World Ecology Wildlife Sync T1 real-gameplay verification, TEST-ONLY: --force-wildlife-trigger.
 * See pc_platform.h's own doc comment on this global. */
int g_pc_force_wildlife_trigger = 0;

/* World Ecology Wildlife Sync T-catch real-gameplay verification, TEST-ONLY: --force-fish-catch.
 * See pc_platform.h's own doc comment on this global. */
int g_pc_force_fish_catch = 0;

/* World Ecology Wildlife Sync T4 real-gameplay verification, TEST-ONLY: --force-bug-catch.
 * See pc_platform.h's own doc comment on this global. */
int g_pc_force_bug_catch = 0;

/* M9-A TEST-ONLY: --scene-test-enter-shop / --scene-test-leave-after N. See pc_platform.h. */
int g_pc_scene_test_enter_shop = 0;
int g_pc_scene_test_leave_after = 0;

/* M9-B TEST-ONLY: --collide-test-overlap / --collide-test-approach N. See pc_platform.h. */
int g_pc_collide_test_overlap = 0;
int g_pc_collide_test_approach = 0;

/* T8 audit verification, TEST-ONLY: --diag-bug-ttl-lookup <frames>. See pc_platform.h's own doc comment
 * on this global. */
int g_pc_diag_bug_ttl_lookup_frames = 0;

/* T8 review fix verification, TEST-ONLY: --diag-bug-despawn-label-race. See pc_platform.h's own doc
 * comment on this global. */
int g_pc_diag_bug_despawn_label_race = 0;

/* T8 audit verification, TEST-ONLY: --diag-role-link-state. See pc_platform.h's own doc comment on this
 * global. */
int g_pc_diag_role_link_state = 0;

/* D3 real-client verification, TEST-ONLY: --d3-test-wallet-add <N>. See pc_platform.h's own doc comment on this global. */
int g_pc_d3_test_wallet_add = 0;
int g_pc_d3_test_wallet_add_late = 0; /* second change, see pc_platform.h */

/* X1b real-client transaction verification, TEST-ONLY: --txn-test-pickup-drop. See pc_platform.h's own doc comment on this global. */
int g_pc_txn_test_pickup_drop = 0;

/* X3 real-client grant verification, TEST-ONLY: --txn-test-dig-grant. See pc_platform.h's own doc comment on this global. */
int g_pc_txn_test_dig_grant = 0;

/* Town services milestone 1 TEST-ONLY hooks: --ts-test-seed-police / --ts-test-donate / --ts-test-claim. See pc_platform.h's own doc comment. */
const char* g_pc_ts_test_seed_police = NULL;
int g_pc_ts_test_donate = 0;
int g_pc_ts_test_claim = 0;
/* Town services milestone 2 (shop) TEST-ONLY hooks: --shop-test-buy / --shop-test-sell. See pc_platform.h's own doc comment. */
int g_pc_shop_test_buy = 0;
int g_pc_shop_test_sell = 0;
/* Mail milestone 1 TEST-ONLY hooks: --mail-test-send=<house>[,gift] / --mail-test-force-delivery. See pc_platform.h's own doc comment. */
const char* g_pc_mail_test_send = NULL;
int g_pc_mail_test_force_delivery = 0;
int g_pc_mail_test_poke_museum = -1;
/* Mail milestone 2 TEST-ONLY hooks: --mail-test-take[=N] (client), --mail-test-seed-mailbox=... / --mail-test-seed-reply=... (host). See pc_platform.h. */
int g_pc_mail_test_take = 0;
const char* g_pc_mail_test_seed_mailbox = NULL;
const char* g_pc_mail_test_seed_reply = NULL;

/* X1 host-side fault injection, TEST-ONLY: --txn-fault=<mode>[:N[:K]]. See pc_platform.h's own doc comment on these globals. */
int g_pc_txn_fault_mode = 0;
int g_pc_txn_fault_nth = 1;
int g_pc_txn_fault_arg = 1;

/* Parses "<mode>[:N[:K]]". Returns 1 and fills the three globals, or 0 for an unknown mode / malformed number (the caller refuses). */
static int pc_parse_txn_fault(const char* spec) {
    static const struct { const char* name; int mode; } modes[] = {
        { "ignore_commit", PC_TXN_FAULT_IGNORE_COMMIT },
        { "fail_world", PC_TXN_FAULT_FAIL_WORLD },
        { "expire", PC_TXN_FAULT_EXPIRE },
        { "drop_result", PC_TXN_FAULT_DROP_RESULT },
        { "kill_peer_after_commit", PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT },
    };
    char buf[96];
    char* parts[3] = { NULL, NULL, NULL };
    char* p;
    int n = 0;
    size_t k;
    int nth = 1, cnt = 1;
    if (strlen(spec) >= sizeof(buf)) {
        return 0;
    }
    strcpy(buf, spec);
    p = buf;
    while (n < 3) {
        parts[n++] = p;
        p = strchr(p, ':');
        if (p == NULL) {
            break;
        }
        *p++ = '\0';
    }
    if (p != NULL) {
        return 0; /* more than three fields */
    }
    for (k = 0; k < sizeof(modes) / sizeof(modes[0]); k++) {
        if (strcmp(parts[0], modes[k].name) == 0) {
            break;
        }
    }
    if (k == sizeof(modes) / sizeof(modes[0])) {
        return 0;
    }
    if (n >= 2) {
        char* end = NULL;
        long v = strtol(parts[1], &end, 10);
        if (parts[1][0] == '\0' || *end != '\0' || v < 1 || v > 1000000) {
            return 0;
        }
        nth = (int)v;
    }
    if (n >= 3) {
        char* end = NULL;
        long v = strtol(parts[2], &end, 10);
        if (parts[2][0] == '\0' || *end != '\0' || v < 1 || v > 1000000) {
            return 0;
        }
        cnt = (int)v;
    }
    g_pc_txn_fault_mode = modes[k].mode;
    g_pc_txn_fault_nth = nth;
    g_pc_txn_fault_arg = cnt;
    return 1;
}

/* Stage 0: --bootstrap-resident N. Non-interactively binds an EXISTING resident from save slot N
 * (0..PLAYER_NUM-1) and transitions into gameplay (SCENE_FG), without driving the interactive
 * Rover/player-select flow. Off by default (-1); never active in normal single-player or hosted
 * play unless explicitly requested. See pc_m_card.c's pc_bootstrap_resident_poll() for exactly
 * what it does and why (reuses mSDI_StartDataInit()/goto_other_scene() verbatim -- see the Stage 0
 * bootstrap audit). Global (not static), matching g_pc_pickup_test_seed's own exact pattern, so
 * pc_m_card.c/pc_vi.c can read it without any new coupling to pc_main.c. */
int g_pc_bootstrap_resident = -1;

/* --host / --connect: Stage 1 role selection. No settings.ini persistence (matches --time/
 * --date/--rain: a per-launch dev override, not a saved preference), no UI yet. */
static int      g_pc_net_role = 0; /* 0 = none/single-player, 1 = host, 2 = client */
static uint16_t g_pc_net_port = 7777;
static char     g_pc_net_host_ip[64] = "127.0.0.1";

int main(int argc, char* argv[]) {
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            printf("Usage: AnimalCrossing [options]\n");
            printf("  --verbose, -v       Enable diagnostic output\n");
            printf("  --no-framelimit     Alias for --framelimit 0 (uncapped)\n");
            printf("  --framelimit N      Set the target frame rate (default 60, 0 = uncapped)\n");
            printf("  --profile [N]       Print frame profiler summary every N frames (default 120)\n");
            printf("  --model-viewer [N]  Launch model viewer (optional start index)\n");
            printf("  --time H[:M[:S]]    Override in-game time (e.g. 5, 17:30, 5:55:00)\n");
            printf("  --date M/D[/Y]      Override in-game date (e.g. 7/4, 12/24/2026)\n");
            printf("  --rain [intensity]  Force rainy weather; intensity is light, normal, or heavy\n");
            printf("  --uber-shader       Disable shader specialization (single uber shader)\n");
            printf("  --host [port]       Start hosting (default port 7777); game plays normally while waiting\n");
            printf("  --connect ip[:port] Connect to a host (default port 7777); game plays normally while connecting\n");
            printf("  --pickup-test-seed  Host-only test fixture: seeds a few field tiles with an ordinary\n");
            printf("                      item for the pickup regression test. Never touches gameplay\n");
            printf("                      otherwise; see pc_net_game.c.\n");
            printf("  --field-action-test-seed  Host-only test fixture: seeds one buried item and one\n");
            printf("                      money rock tile for the DIG_BURIED/MONEY_ROCK_HIT regression\n");
            printf("                      tests. Never touches gameplay otherwise; see pc_net_game.c.\n");
            printf("  --bury-test-seed    Host-only test fixture: seeds HOLE00.. and HOLE_SHINE tiles for\n");
            printf("                      the BURY_REQUEST/RESULT regression tests. Never touches gameplay\n");
            printf("                      otherwise; see pc_net_game.c.\n");
            printf("  --bootstrap-resident N  Non-interactively bind existing resident slot N and enter\n");
            printf("                      gameplay, bypassing the Rover/player-select flow. See pc_m_card.c.\n");
            printf("  --authoritative-wildlife  Opt-in MODE flag (persistent, like --host/--connect --\n");
            printf("                      not a one-shot test hook): activates the host-authoritative\n");
            printf("                      fish/bug spawn adapter (pc_wildlife_authority.c). Off by default;\n");
            printf("                      without it, every role spawns wildlife locally exactly as before\n");
            printf("                      this milestone -- see pc_platform.h and ac_set_manager.c.\n");
            printf("  --force-villager-grow    Host-only test hook: force a villager to grow in once the\n");
            printf("                      world is ready, bypassing the real (multi-day) trigger condition\n");
            printf("                      only -- see pc_net_game.c.\n");
            printf("  --force-villager-remove  Host-only test hook: force a villager to be removed once the\n");
            printf("                      world is ready, bypassing the real (multi-day) trigger condition\n");
            printf("                      only -- see pc_net_game.c.\n");
            printf("  --force-friendship-delta N  Test hook (host OR client): apply friendship delta N to\n");
            printf("                      the local player's memory of the first occupied villager once the\n");
            printf("                      world/link is ready -- see pc_net_game.c.\n");
            printf("  --force-mail-send   Test hook (host OR client): send a test letter to the first\n");
            printf("                      occupied villager once the world/link is ready -- see pc_net_game.c.\n");
            printf("  --force-money-rock-hit  Host-only test hook: force this host's own local\n");
            printf("                      pc_net_game_host_local_money_rock_hit() at the\n");
            printf("                      --field-action-test-seed fixture's money-rock tile (24,104) --\n");
            printf("                      see pc_net_game.c.\n");
            printf("  --force-money-bag-pickup  Client-only test hook: force a real pc_net_game_request_pickup()\n");
            printf("                      attempt at whichever neighbor tile of (24,104) holds a money bag --\n");
            printf("                      see pc_net_game.c.\n");
            printf("  --force-dig-hole    Client-only test hook: force a real Player_actor_request_main_dig_scoop_all()\n");
            printf("                      request at the --field-action-test-seed fixture's DIG_HOLE tile\n");
            printf("                      (56,105), driving the REAL unmodified dig-scoop gameplay chain to its\n");
            printf("                      real network commit point -- see pc_net_game.c and m_player.c.\n");
            printf("  --force-wildlife-trigger  Client-only test hook: force a real\n");
            printf("                      pc_net_game_request_wildlife_spawn_trigger() across a burst of 10\n");
            printf("                      different acres in one frame (not a single fixed acre) --\n");
            printf("                      requires --authoritative-wildlife -- see pc_net_game.c.\n");
            printf("  --force-fish-catch  Host or client test hook: force a real host-local or\n");
            printf("                      CATCH_REQUEST catch of whichever FISH entity is currently known --\n");
            printf("                      requires --authoritative-wildlife -- see pc_net_game.c.\n");
            printf("  --force-bug-catch   Host or client test hook: force a real host-local or\n");
            printf("                      CATCH_REQUEST catch of whichever ordinary (non-ant) BUG entity is\n");
            printf("                      currently known -- requires --authoritative-wildlife -- see\n");
            printf("                      pc_net_game.c.\n");
            printf("  --diag-bug-despawn-label-race  Host-only test hook: force this process's own\n");
            printf("                      item_net_catch_label onto a live BUG entity's local actor, then\n");
            printf("                      verify the entity_id<->local_actor mapping survives a raced\n");
            printf("                      despawn (a second peer's CATCH_REQUEST for the same entity) while\n");
            printf("                      the label is still active -- requires --authoritative-wildlife --\n");
            printf("                      see pc_net_game.c/pc_platform.h.\n");
            printf("  --d3-test-wallet-add N  Client-only TEST hook: 3 s after the host's resident record is\n");
            printf("                      adopted, add N bells to the local wallet once (exercises the record\n");
            printf("                      upload path) -- see pc_platform.h. Never use in normal play.\n");
            printf("  --d3-test-wallet-add-late N  Client-only TEST hook: a second wallet change inside the upload\n"
                   "                      gap (only the quit flush can send it); see pc_platform.h.\n");
            printf("  --txn-test-pickup-drop  Client-only TEST hook (default off; loud logs): once the resident record\n"
                   "                      is synced, drop the first droppable pocket item at the player's own drop\n"
                   "                      tile and then pick it up again through the real request functions, so the\n"
                   "                      host-transactional TXN_COMMIT path runs without GUI input; see\n"
                   "                      pc_platform.h. Never use in normal play.\n");
            printf("  --txn-test-dig-grant  Client-only TEST hook (default off; loud logs): once the resident record is\n"
                   "                      synced, request the three host-transactional dig GRANTS at the\n"
                   "                      --field-action-test-seed fixtures through the real request functions\n"
                   "                      (DIG_BURIED (40,104), DIG_HOLE+golden-shovel bonus (56,105), DIG_SHINE+bell\n"
                   "                      roll (88,105)); see pc_platform.h. Never use in normal play.\n");
            printf("  --txn-fault=MODE[:N[:K]]  HOST-only TEST hook (refused for any other role; off by default; loud\n"
                   "                      logs): inject a fault into the host's transactional pickup/drop/bury\n"
                   "                      commit. MODE = ignore_commit | fail_world | expire | drop_result |\n"
                   "                      kill_peer_after_commit; fires on the N-th visit of the mode's site (default 1),\n"
                   "                      K times (default 1). See pc_platform.h. Never use in normal play.\n");
            printf("  --ts-test-seed-police=HEX[,HEX...]  HOST-only TEST hook (default off; loud logs): keep the listed\n"
                   "                      items in the host's lost and found once the world is ready. See pc_platform.h.\n");
            printf("  --ts-test-donate    Client-only TEST hook (default off; loud logs): drive ONE real museum donation\n"
                   "                      through the town-service transaction path. See pc_platform.h.\n");
            printf("  --ts-test-claim     Client-only TEST hook (default off; loud logs): drive ONE real lost-and-found\n"
                   "                      claim through the town-service transaction path. See pc_platform.h.\n");
            printf("  --shop-test-buy     Client-only TEST hook (default off; loud logs): drive ONE real shop purchase\n"
                   "                      through the town-service transaction path. See pc_platform.h.\n");
            printf("  --shop-test-sell    Client-only TEST hook (default off; loud logs): drive ONE real shop sale\n"
                   "                      through the town-service transaction path. See pc_platform.h.\n");
            printf("  --mail-test-send=HOUSE[,gift]  Client-only TEST hook (default off; loud logs): drive ONE real letter to\n"
                   "                      the resident of local house HOUSE through the MAIL_SEND transaction path. See pc_platform.h.\n");
            printf("  --mail-test-force-delivery  HOST-only TEST hook (default off; loud logs): run the vanilla post office\n"
                   "                      delivery every 2 s and log the mailboxes. See pc_platform.h.\n");
            printf("  --mail-test-poke-museum=N  HOST-only TEST hook (default off; loud logs): once resident N is synced, change its\n"
                   "                      museum_record like the host's day-change mail does. See pc_platform.h.\n");
            printf("  --mail-test-take[=N]  Client-only TEST hook (default off; loud logs): take up to N (default 10) letters out of the\n"
                   "                      host-fed mailbox through the MAIL_TAKE transaction path. See pc_platform.h.\n");
            printf("  --mail-test-seed-mailbox=RES,COUNT,DELAY_MS[,GIFTHEX]  HOST-only TEST hook (default off; loud logs): write COUNT\n"
                   "                      letters into resident RES's house mailbox after DELAY_MS (a simulated postman). See pc_platform.h.\n");
            printf("  --mail-test-seed-reply=RES[,RES...]  HOST-only TEST hook (default off; loud logs): make the first villager owe each\n"
                   "                      listed resident a reply dated a year ago. See pc_platform.h.\n");
            printf("  --help, -h          Show this help message\n");
            return 0;
        } else if (strcmp(argv[i], "--framelimit") == 0) {
            if (i + 1 < argc && argv[i + 1][0] != '-') {
                int v = atoi(argv[i + 1]);
                if (v > 0) {
                    g_pc_frame_limit_override = v;
                } else if (v == 0) {
                    g_pc_frame_limit_override = 0;
                }
                i++;
            }
        } else if (strcmp(argv[i], "--no-framelimit") == 0) {
            g_pc_frame_limit_override = 0;
        } else if (strcmp(argv[i], "--uber-shader") == 0) {
            extern int g_pc_uber_shader_only;
            g_pc_uber_shader_only = 1;
        } else if (strcmp(argv[i], "--verbose") == 0 || strcmp(argv[i], "-v") == 0) {
            g_pc_verbose = 1;
#ifdef PC_LOW_ADDRESS_64
        } else if (strcmp(argv[i], "--lowaddr-selftest") == 0) {
            g_pc_lowaddr_selftest = 1;
#endif
        } else if (strcmp(argv[i], "--rng-selftest") == 0) {
            g_pc_rng_selftest = 1;
        } else if (strcmp(argv[i], "--pickup-test-seed") == 0) {
            g_pc_pickup_test_seed = 1;
        } else if (strcmp(argv[i], "--field-action-test-seed") == 0) {
            g_pc_field_action_test_seed = 1;
        } else if (strcmp(argv[i], "--bury-test-seed") == 0) {
            g_pc_bury_test_seed = 1;
        } else if (strcmp(argv[i], "--force-villager-grow") == 0) {
            g_pc_force_villager_grow = 1;
        } else if (strcmp(argv[i], "--force-villager-remove") == 0) {
            g_pc_force_villager_remove = 1;
        } else if (strcmp(argv[i], "--force-friendship-delta") == 0 && i + 1 < argc) {
            g_pc_force_friendship_delta = atoi(argv[i + 1]);
            i++;
        } else if (strcmp(argv[i], "--force-mail-send") == 0) {
            g_pc_force_mail_send = 1;
        } else if (strcmp(argv[i], "--force-money-rock-hit") == 0) {
            g_pc_force_money_rock_hit = 1;
        } else if (strcmp(argv[i], "--force-money-bag-pickup") == 0) {
            g_pc_force_money_bag_pickup = 1;
        } else if (strcmp(argv[i], "--force-dig-hole") == 0) {
            g_pc_force_dig_hole = 1;
        } else if (strcmp(argv[i], "--force-wildlife-trigger") == 0) {
            g_pc_force_wildlife_trigger = 1;
        } else if (strcmp(argv[i], "--force-fish-catch") == 0) {
            g_pc_force_fish_catch = 1;
        } else if (strcmp(argv[i], "--scene-test-enter-shop") == 0) {
            g_pc_scene_test_enter_shop = 1;
        } else if (strcmp(argv[i], "--scene-test-leave-after") == 0 && i + 1 < argc) {
            g_pc_scene_test_leave_after = atoi(argv[i + 1]);
            i++;
        } else if (strcmp(argv[i], "--collide-test-overlap") == 0) {
            g_pc_collide_test_overlap = 1;
        } else if (strcmp(argv[i], "--collide-test-approach") == 0 && i + 1 < argc) {
            g_pc_collide_test_approach = atoi(argv[i + 1]);
            i++;
        } else if (strcmp(argv[i], "--force-bug-catch") == 0) {
            g_pc_force_bug_catch = 1;
        } else if (strcmp(argv[i], "--diag-bug-ttl-lookup") == 0 && i + 1 < argc) {
            g_pc_diag_bug_ttl_lookup_frames = atoi(argv[i + 1]);
            i++;
        } else if (strcmp(argv[i], "--diag-bug-despawn-label-race") == 0) {
            g_pc_diag_bug_despawn_label_race = 1;
        } else if (strcmp(argv[i], "--diag-role-link-state") == 0) {
            g_pc_diag_role_link_state = 1;
        } else if (strcmp(argv[i], "--d3-test-wallet-add") == 0 && i + 1 < argc) {
            g_pc_d3_test_wallet_add = atoi(argv[i + 1]);
            printf("[NET][REC][TEST-ONLY] --d3-test-wallet-add %d armed (a TEST hook: not for normal play)\n",
                   g_pc_d3_test_wallet_add);
            i++;
        } else if (strcmp(argv[i], "--d3-test-wallet-add-late") == 0 && i + 1 < argc) {
            g_pc_d3_test_wallet_add_late = atoi(argv[i + 1]);
            printf("[NET][REC][TEST-ONLY] --d3-test-wallet-add-late %d armed (a TEST hook: not for normal play)\n",
                   g_pc_d3_test_wallet_add_late);
            i++;
        } else if (strcmp(argv[i], "--txn-test-pickup-drop") == 0) {
            g_pc_txn_test_pickup_drop = 1;
            printf("[NET][TXN][TEST-ONLY] --txn-test-pickup-drop armed (a TEST hook: not for normal play)\n");
        } else if (strcmp(argv[i], "--txn-test-dig-grant") == 0) {
            g_pc_txn_test_dig_grant = 1;
            printf("[NET][TXN][TEST-ONLY] --txn-test-dig-grant armed (a TEST hook: not for normal play)\n");
        } else if (strncmp(argv[i], "--ts-test-seed-police=", 22) == 0) {
            g_pc_ts_test_seed_police = argv[i] + 22;
            printf("[NET][TS][TEST-ONLY] --ts-test-seed-police=%s armed (a TEST hook: not for normal play)\n", g_pc_ts_test_seed_police);
        } else if (strcmp(argv[i], "--ts-test-donate") == 0) {
            g_pc_ts_test_donate = 1;
            printf("[NET][TS][TEST-ONLY] --ts-test-donate armed (a TEST hook: not for normal play)\n");
        } else if (strcmp(argv[i], "--ts-test-claim") == 0) {
            g_pc_ts_test_claim = 1;
            printf("[NET][TS][TEST-ONLY] --ts-test-claim armed (a TEST hook: not for normal play)\n");
        } else if (strcmp(argv[i], "--shop-test-buy") == 0) {
            g_pc_shop_test_buy = 1;
            printf("[NET][SHOP][TEST-ONLY] --shop-test-buy armed (a TEST hook: not for normal play)\n");
        } else if (strcmp(argv[i], "--shop-test-sell") == 0) {
            g_pc_shop_test_sell = 1;
            printf("[NET][SHOP][TEST-ONLY] --shop-test-sell armed (a TEST hook: not for normal play)\n");
        } else if (strncmp(argv[i], "--mail-test-send=", 17) == 0) {
            g_pc_mail_test_send = argv[i] + 17;
            printf("[NET][MAIL][TEST-ONLY] --mail-test-send=%s armed (a TEST hook: not for normal play)\n", g_pc_mail_test_send);
        } else if (strcmp(argv[i], "--mail-test-force-delivery") == 0) {
            g_pc_mail_test_force_delivery = 1;
            printf("[NET][MAIL][TEST-ONLY] --mail-test-force-delivery armed (a TEST hook: not for normal play)\n");
        } else if (strncmp(argv[i], "--mail-test-poke-museum=", 24) == 0) {
            g_pc_mail_test_poke_museum = atoi(argv[i] + 24);
            printf("[NET][MAIL][TEST-ONLY] --mail-test-poke-museum=%d armed (a TEST hook: not for normal play)\n", g_pc_mail_test_poke_museum);
        } else if (strcmp(argv[i], "--mail-test-take") == 0) {
            g_pc_mail_test_take = 10;
            printf("[NET][MAIL][TEST-ONLY] --mail-test-take armed (a TEST hook: not for normal play)\n");
        } else if (strncmp(argv[i], "--mail-test-take=", 17) == 0) {
            g_pc_mail_test_take = atoi(argv[i] + 17);
            printf("[NET][MAIL][TEST-ONLY] --mail-test-take=%d armed (a TEST hook: not for normal play)\n", g_pc_mail_test_take);
        } else if (strncmp(argv[i], "--mail-test-seed-mailbox=", 25) == 0) {
            g_pc_mail_test_seed_mailbox = argv[i] + 25;
            printf("[NET][MAIL][TEST-ONLY] --mail-test-seed-mailbox=%s armed (a TEST hook: not for normal play)\n", g_pc_mail_test_seed_mailbox);
        } else if (strncmp(argv[i], "--mail-test-seed-reply=", 23) == 0) {
            g_pc_mail_test_seed_reply = argv[i] + 23;
            printf("[NET][MAIL][TEST-ONLY] --mail-test-seed-reply=%s armed (a TEST hook: not for normal play)\n", g_pc_mail_test_seed_reply);
        } else if (strncmp(argv[i], "--txn-fault=", 12) == 0) {
            if (!pc_parse_txn_fault(argv[i] + 12)) {
                fprintf(stderr, "[NET][TXN][TEST-ONLY] REFUSED: bad --txn-fault spec '%s' (expected MODE[:N[:K]], MODE = ignore_commit | "
                                "fail_world | expire | drop_result | kill_peer_after_commit)\n", argv[i] + 12);
                return 2;
            }
        } else if (strcmp(argv[i], "--bootstrap-resident") == 0 && i + 1 < argc) {
            g_pc_bootstrap_resident = atoi(argv[i + 1]);
            i++;
        } else if (strcmp(argv[i], "--authoritative-wildlife") == 0) {
            g_pc_authoritative_wildlife = 1;
        } else if (strcmp(argv[i], "--profile") == 0) {
            g_pc_profile_enabled = 1;
            if (i + 1 < argc && argv[i + 1][0] != '-') {
                int interval = atoi(argv[i + 1]);
                if (interval > 0) g_pc_profile_interval = interval;
                i++;
            }
        } else if (strcmp(argv[i], "--model-viewer") == 0) {
            g_pc_model_viewer = 1;
            if (i + 1 < argc && argv[i + 1][0] != '-') {
                g_pc_model_viewer_start = atoi(argv[i + 1]);
                i++;
            }
        } else if (strcmp(argv[i], "--time") == 0 && i + 1 < argc) {
            int h = -1, m = -1, s = -1;
            sscanf(argv[i + 1], "%d:%d:%d", &h, &m, &s);
            if (h >= 0 && h <= 23) g_pc_time_override = h;
            if (m >= 0 && m <= 59) g_pc_min_override = m;
            if (s >= 0 && s <= 59) g_pc_sec_override = s;
            i++;
        } else if (strcmp(argv[i], "--date") == 0 && i + 1 < argc) {
            int mo = -1, d = -1, y = -1;
            sscanf(argv[i + 1], "%d/%d/%d", &mo, &d, &y);
            if (mo >= 1 && mo <= 12 && d >= 1 && d <= 31) {
                g_pc_date_month = mo;
                g_pc_date_day = d;
                if (y >= 2000) g_pc_date_year = y;
            }
            i++;
        } else if (strcmp(argv[i], "--rain") == 0) {
            g_pc_weather_override = mEnv_WEATHER_RAIN;
            g_pc_weather_intensity_override = mEnv_WEATHER_INTENSITY_HEAVY;
            if (i + 1 < argc && argv[i + 1][0] != '-') {
                int intensity = pc_parse_rain_intensity(argv[i + 1]);
                if (intensity >= 0) {
                    g_pc_weather_intensity_override = intensity;
                    i++;
                }
            }
        } else if (strcmp(argv[i], "--host") == 0) {
            g_pc_net_role = 1;
            if (i + 1 < argc && argv[i + 1][0] != '-') {
                int p = atoi(argv[i + 1]);
                if (p > 0 && p <= 65535) g_pc_net_port = (uint16_t)p;
                i++;
            }
        } else if (strcmp(argv[i], "--connect") == 0 && i + 1 < argc) {
            char* colon;
            g_pc_net_role = 2;
            strncpy(g_pc_net_host_ip, argv[i + 1], sizeof(g_pc_net_host_ip) - 1);
            g_pc_net_host_ip[sizeof(g_pc_net_host_ip) - 1] = '\0';
            colon = strchr(g_pc_net_host_ip, ':');
            if (colon) {
                int p = atoi(colon + 1);
                *colon = '\0';
                if (p > 0 && p <= 65535) g_pc_net_port = (uint16_t)p;
            }
            i++;
        }
    }

    /* X1: --txn-fault is a HOST-only TEST hook: refused (before stdout/stderr are redirected) for any other role. */
    if (g_pc_txn_fault_mode != 0) {
        if (g_pc_net_role != 1) {
            fprintf(stderr, "[NET][TXN][TEST-ONLY] REFUSED: --txn-fault is a HOST-only test hook (use it together with --host)\n");
            return 2;
        }
        printf("[NET][TXN][TEST-ONLY] FAULT INJECTION ENABLED mode=%d nth=%d count=%d (a TEST hook: not for normal play)\n",
               g_pc_txn_fault_mode, g_pc_txn_fault_nth, g_pc_txn_fault_arg);
    }

    /* Mail milestone 1: the mail TEST hooks are role-bound like --txn-fault: refused (exit 2) for any other role. */
    if ((g_pc_mail_test_force_delivery || g_pc_mail_test_poke_museum >= 0 || g_pc_mail_test_seed_mailbox != NULL || g_pc_mail_test_seed_reply != NULL) &&
        g_pc_net_role != 1) {
        fprintf(stderr, "[NET][MAIL][TEST-ONLY] REFUSED: --mail-test-force-delivery / --mail-test-poke-museum / --mail-test-seed-mailbox / --mail-test-seed-reply are HOST-only test hooks (use them together with --host)\n");
        return 2;
    }
    if ((g_pc_mail_test_send != NULL || g_pc_mail_test_take != 0) && g_pc_net_role != 2) {
        fprintf(stderr, "[NET][MAIL][TEST-ONLY] REFUSED: --mail-test-send / --mail-test-take are CLIENT-only test hooks (use them together with --connect)\n");
        return 2;
    }

    /* Redirect stdout/stderr to NUL unless verbose — unbuffered terminal writes
     * are extremely slow on Windows and tank FPS. */
    if (!g_pc_verbose && !g_pc_profile_enabled) {
#ifdef _WIN32
        freopen("NUL", "w", stdout);
        freopen("NUL", "w", stderr);
#else
        freopen("/dev/null", "w", stdout);
        freopen("/dev/null", "w", stderr);
#endif
    } else {
        setvbuf(stdout, NULL, _IONBF, 0);
        setvbuf(stderr, NULL, _IONBF, 0);
    }

    /* exe image range for seg2k0 — BSS can overlap N64 segment addresses */
#ifdef _WIN32
    {
        HMODULE exe = GetModuleHandle(NULL);
        IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)exe;
        IMAGE_NT_HEADERS* nt = (IMAGE_NT_HEADERS*)((char*)exe + dos->e_lfanew);
        pc_image_base = PC_PTR32("executable image base", exe);
        pc_image_end = pc_image_base + nt->OptionalHeader.SizeOfImage;
    }
#else
    {
        Dl_info dl;
        if (dladdr((void*)main, &dl) && dl.dli_fbase) {
            pc_image_base = (unsigned int)(uintptr_t)dl.dli_fbase;
            Elf32_Ehdr* ehdr = (Elf32_Ehdr*)dl.dli_fbase;
            Elf32_Phdr* phdr = (Elf32_Phdr*)((char*)dl.dli_fbase + ehdr->e_phoff);
            unsigned int max_end = 0;
            for (int i = 0; i < ehdr->e_phnum; i++) {
                if (phdr[i].p_type == PT_LOAD) {
                    unsigned int seg_end = phdr[i].p_vaddr + phdr[i].p_memsz;
                    if (seg_end > max_end) max_end = seg_end;
                }
            }
            /* ET_EXEC: p_vaddr is absolute. ET_DYN (PIE): relative to load address. */
            if (ehdr->e_type == ET_DYN) {
                pc_image_end = pc_image_base + max_end;
            } else {
                pc_image_end = max_end;
            }
        }
    }
#endif

    pc_lowaddr_init(); /* no-op unless built with PC_LOW_ADDRESS_64 */
    SDL_SetMainReady();
    pc_settings_load();
    pc_keybindings_load();

    /* Stage 1: role selection only. A failure here (bad port, bad address, Winsock
     * unavailable) is logged inside pc_net_game_start_*() and just leaves g_pc_net_role's
     * request unfulfilled -- single-player continues exactly as if neither flag was passed. */
    if (g_pc_net_role == 1) {
        pc_net_game_start_host(g_pc_net_port);
    } else if (g_pc_net_role == 2) {
        pc_net_game_start_client(g_pc_net_host_ip, g_pc_net_port);
    }

    pc_platform_init();
#ifdef PC_LOW_ADDRESS_64
    if (g_pc_lowaddr_selftest) {
        int failures = pc_lowaddr_selftest();
        int violations = pc_lowaddr_report();
        pc_platform_shutdown();
        return failures + violations;
    }
#endif
    if (g_pc_rng_selftest) {
        int failures = pc_rng_domains_selftest();
        pc_platform_shutdown();
        return failures;
    }
    pc_disc_init();
    if (!pc_assets_init()) {
        const char* msg =
            "No game data found.\n\n"
            "Animal Crossing needs the original GameCube ROM to run.\n"
            "Place a disc image (.iso, .gcm, or .ciso) to the \"rom\" subfolder.";
        fprintf(stderr, "[PC] %s\n", msg);
        pc_lowaddr_report();
        SDL_ShowSimpleMessageBox(SDL_MESSAGEBOX_ERROR,
                                 "Animal Crossing - Missing ROM", msg, g_pc_window);
        pc_platform_shutdown();
        return 1;
    }

    pc_lowaddr_report();
    ac_entry();                         /* sets HotStartEntry = &entry */
    boot_main(argc, (const char**)argv); /* full init → HotStartEntry → game loop */

    /* NOTE: boot_main() never actually returns here on TARGET_PC -- mainproc() (src/main.c) calls
     * exit(0) directly once graph_proc() returns (the game's only quit path), so the report below is
     * unreachable in practice. The real final report is emitted from src/main.c right before that
     * exit(0); kept here too in case a future code path restores a normal return from boot_main(). */
    pc_disc_shutdown();
    pc_platform_shutdown();
    pc_lowaddr_report();
    return 0;
}
