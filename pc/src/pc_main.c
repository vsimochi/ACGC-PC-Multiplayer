/* pc_main.c - PC entry point: SDL2/GL init and boot sequence */
#include "pc_platform.h"
#include "pc_nook_house.h" /* guest Nook dialogue (M4): the rejoin hold */
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
#include "pc_town_cache.h" /* M-A: pc_card_set_town_dir() */
#include "pc_town_sanitize.h" /* M-J: pc_town_gci_find_resident() for the stale-membership check */
#include "pc_host_observer.h"
#include "pc_dedicated.h"
#include "pc_guest_profile.h"
#include "pc_play_online_menu.h" /* M4: text entry routing for the title "Play Online" menu */
#include "pc_servers.h"
#include "pc_relaunch.h" /* M-C: pc_relaunch_forward_capture() */
#include "pc_session.h" /* M2: characters / town memberships: --character, --characters, --character-import-profile */
#include "pc_log.h"
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

/* Test aid: AC_DISPLAY_NAME=<text> puts the window on the display whose name contains <text> (case-insensitive). Matched against the SDL display name, the Windows monitor
 * friendly name (QueryDisplayConfig / DISPLAYCONFIG_TARGET_DEVICE_NAME, joined to the SDL display through its GDI device) and the EDID vendor (3 letters + a short vendor
 * name, e.g. SAM = "Samsung": the friendly name of a Samsung monitor is a model number). EVERY display is logged (name, friendly name, vendor, bounds) whenever the variable
 * is set. No match, or more than one, exits 4 before any window exists, so a test never lands on the wrong monitor. Unset = SDL_WINDOWPOS_CENTERED as before. */
static void pc_display_lower(char* s) {
    for (; *s != '\0'; s++) {
        if (*s >= 'A' && *s <= 'Z') *s = (char)(*s - 'A' + 'a');
    }
}

#ifdef _WIN32
/* The Windows friendly name + 3-letter EDID vendor of the monitor that shows SDL display `idx`. 1 = found. */
static int pc_win_display_target(int idx, char* friendly, size_t fcap, char* vendor, size_t vcap) {
    SDL_Rect r;
    POINT pt;
    HMONITOR hm;
    MONITORINFOEXW mi;
    UINT32 np = 0, nm = 0, i;
    DISPLAYCONFIG_PATH_INFO* paths;
    DISPLAYCONFIG_MODE_INFO* modes;
    int found = 0;
    friendly[0] = '\0';
    vendor[0] = '\0';
    if (SDL_GetDisplayBounds(idx, &r) != 0) return 0;
    pt.x = r.x + r.w / 2;
    pt.y = r.y + r.h / 2;
    hm = MonitorFromPoint(pt, MONITOR_DEFAULTTONULL);
    if (hm == NULL) return 0;
    memset(&mi, 0, sizeof(mi));
    mi.cbSize = sizeof(mi);
    if (!GetMonitorInfoW(hm, (MONITORINFO*)&mi)) return 0;
    if (GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &np, &nm) != ERROR_SUCCESS || np == 0) return 0;
    paths = (DISPLAYCONFIG_PATH_INFO*)calloc(np, sizeof(*paths));
    modes = (DISPLAYCONFIG_MODE_INFO*)calloc(nm > 0 ? nm : 1, sizeof(*modes));
    if (paths != NULL && modes != NULL && QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &np, paths, &nm, modes, NULL) == ERROR_SUCCESS) {
        for (i = 0; i < np && !found; i++) {
            DISPLAYCONFIG_SOURCE_DEVICE_NAME sn;
            DISPLAYCONFIG_TARGET_DEVICE_NAME tn;
            memset(&sn, 0, sizeof(sn));
            sn.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
            sn.header.size = sizeof(sn);
            sn.header.adapterId = paths[i].sourceInfo.adapterId;
            sn.header.id = paths[i].sourceInfo.id;
            if (DisplayConfigGetDeviceInfo(&sn.header) != ERROR_SUCCESS || wcscmp(sn.viewGdiDeviceName, mi.szDevice) != 0) continue;
            memset(&tn, 0, sizeof(tn));
            tn.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME;
            tn.header.size = sizeof(tn);
            tn.header.adapterId = paths[i].targetInfo.adapterId;
            tn.header.id = paths[i].targetInfo.id;
            if (DisplayConfigGetDeviceInfo(&tn.header) != ERROR_SUCCESS) continue;
            WideCharToMultiByte(CP_UTF8, 0, tn.monitorFriendlyDeviceName, -1, friendly, (int)fcap, NULL, NULL);
            friendly[fcap - 1] = '\0';
            {
                /* the EDID manufacturer id is stored big-endian (3 x 5 bits, 'A' = 1): try the swapped reading first, then the direct one */
                unsigned v[2], k, j;
                v[0] = (unsigned)(((tn.edidManufactureId & 0xFFu) << 8) | (tn.edidManufactureId >> 8));
                v[1] = (unsigned)tn.edidManufactureId;
                for (k = 0; k < 2 && vendor[0] == '\0' && vcap >= 4; k++) {
                    char c[3];
                    c[0] = (char)('@' + ((v[k] >> 10) & 31));
                    c[1] = (char)('@' + ((v[k] >> 5) & 31));
                    c[2] = (char)('@' + (v[k] & 31));
                    for (j = 0; j < 3 && c[j] >= 'A' && c[j] <= 'Z'; j++) {
                    }
                    if (j == 3 && (v[k] & 0x8000u) == 0) {
                        memcpy(vendor, c, 3);
                        vendor[3] = '\0';
                    }
                }
            }
            found = 1;
        }
    }
    free(paths);
    free(modes);
    return found;
}
#endif

static const char* pc_display_vendor_name(const char* v) {
    static const char* const k[][2] = { { "SAM", "samsung" }, { "GSM", "lg" }, { "DEL", "dell" }, { "ACR", "acer" }, { "AUS", "asus" }, { "BNQ", "benq" },
                                        { "AOC", "aoc" }, { "HWP", "hp" }, { "LEN", "lenovo" }, { "MSI", "msi" }, { "PHL", "philips" }, { "VSC", "viewsonic" } };
    size_t i;
    for (i = 0; i < sizeof(k) / sizeof(k[0]); i++) {
        if (strcmp(v, k[i][0]) == 0) return k[i][1];
    }
    return "";
}

/* 1 = a display was picked (*pos = SDL_WINDOWPOS_CENTERED_DISPLAY(i)), 0 = AC_DISPLAY_NAME is not set. Exits 4 on no / ambiguous match. */
static int pc_display_pick_from_env(int* pos) {
    const char* want = getenv("AC_DISPLAY_NAME");
    char needle[64];
    int n, i, matches = 0, first = -1;
    if (want == NULL || want[0] == '\0') return 0;
    snprintf(needle, sizeof(needle), "%s", want);
    pc_display_lower(needle);
    n = SDL_GetNumVideoDisplays();
    printf("[PC] display: AC_DISPLAY_NAME='%s': %d display(s)\n", want, n);
    for (i = 0; i < n; i++) {
        SDL_Rect r;
        char friendly[128], vendor[8], hay[512];
        const char* sdln = SDL_GetDisplayName(i);
        friendly[0] = '\0';
        vendor[0] = '\0';
        memset(&r, 0, sizeof(r));
        (void)SDL_GetDisplayBounds(i, &r);
#ifdef _WIN32
        (void)pc_win_display_target(i, friendly, sizeof(friendly), vendor, sizeof(vendor));
#endif
        snprintf(hay, sizeof(hay), "%s|%s|%s|%s", sdln != NULL ? sdln : "", friendly, vendor, pc_display_vendor_name(vendor));
        pc_display_lower(hay);
        printf("[PC] display %d: sdl-name='%s' friendly-name='%s' vendor='%s' bounds=%d,%d %dx%d%s\n", i, sdln != NULL ? sdln : "", friendly, vendor, r.x, r.y, r.w, r.h,
               strstr(hay, needle) != NULL ? "  <-- matches" : "");
        if (strstr(hay, needle) != NULL) {
            if (first < 0) first = i;
            matches++;
        }
    }
    fflush(stdout);
    if (matches != 1) {
        fprintf(stderr, "[PC] display: AC_DISPLAY_NAME='%s' matched %d display(s) (need exactly 1): REFUSING to open a window (exit 4)\n", want, matches);
        fflush(NULL);
        SDL_Quit();
        exit(4);
    }
    *pos = SDL_WINDOWPOS_CENTERED_DISPLAY(first);
    printf("[PC] display: window goes to display %d\n", first);
    return 1;
}

void pc_platform_init(void) {
#ifdef _WIN32
    SetProcessDPIAware();
    SDL_SetHint(SDL_HINT_WINDOWS_INTRESOURCE_ICON, "1");
    SetConsoleCtrlHandler(pc_console_ctrl_handler, TRUE);
    signal(SIGINT, pc_signal_handler);
    signal(SIGTERM, pc_signal_handler);
#endif
    if (g_pc_dedicated) {
        pc_dedicated_pre_sdl_init(); /* --dedicated: dummy audio driver, selected BEFORE SDL_Init reads the driver choice */
    }
    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_GAMECONTROLLER | SDL_INIT_AUDIO | SDL_INIT_TIMER) < 0) {
        fprintf(stderr, "SDL_Init failed: %s\n", SDL_GetError());
        exit(1);
    }
    if (g_pc_dedicated && !pc_dedicated_post_sdl_init()) {
        SDL_Quit();
        exit(1); /* the dummy audio driver did not open: a dedicated server must not run with a real/stalled audio device */
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
        if (g_pc_dedicated) {
            flags = SDL_WINDOW_OPENGL | SDL_WINDOW_HIDDEN; /* --dedicated: hidden, never shown (SDL_ShowWindow is never called), not fullscreen/resizable; the GL 3.3 context below still exists */
        }
        int win_pos = SDL_WINDOWPOS_CENTERED;
        (void)pc_display_pick_from_env(&win_pos); /* test aid AC_DISPLAY_NAME (exits 4 when it matches no / several displays) */
        g_pc_window = SDL_CreateWindow(
            PC_WINDOW_TITLE,
            win_pos, win_pos,
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

    SDL_GL_SetSwapInterval(g_pc_dedicated ? 0 : g_pc_settings.vsync); /* --dedicated: no vsync */

    pc_platform_update_window_size();

#ifdef PC_ENHANCEMENTS
    if (g_pc_settings.msaa > 0) {
        glEnable(GL_MULTISAMPLE);
    }
#endif

    pc_gx_init();
    pc_texture_pack_init();
#ifdef PC_ENHANCEMENTS
    if (g_pc_settings.preload_textures && !g_pc_dedicated) { /* --dedicated: nothing is drawn, no GL texture preload */
        pc_texture_pack_preload_all();
    }
#endif
    if (g_pc_dedicated) {
        pc_dedicated_platform_summary();
    }
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
                /* M4: the Play Online menu text entry (add / edit server, new character name) eats keys first. */
                if (pc_play_online_menu_text_active()) {
                    pc_play_online_menu_handle_text_event(&event);
                    break;
                }
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
                if (pc_play_online_menu_text_active()) {
                    pc_play_online_menu_handle_text_event(&event);
                    break;
                }
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
                if (pc_play_online_menu_text_active()) {
                    pc_play_online_menu_handle_text_event(&event);
                    break;
                }
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

/* Furniture sync (Stage 1): --house-sync (HOST decides, like --authoritative-wildlife; announced to every client in HOST_CONFIG byte 1 bit 0). Off by
 * default: without it nothing about player houses is networked and no client gate is armed. TEST-ONLY: --house-test-host-in-house H (the host treats
 * house H as 'its player is inside' for every safety decision), --house-test-host-edit H,floor,cell,item (once the host world is ready the host
 * writes `item` into layer 0 cell `cell` of floor `floor` of house H of its own Save, standing in for a host-originated change such as turnip
 * spoilage). See pc_platform.h. */
int g_pc_house_sync = 0;
static int s_house_sync_explicit = 0; /* --house-sync / --no-house-sync given: a --dedicated host otherwise turns furniture sync ON by default */
int g_pc_house_test_host_in_house = -1;
const char* g_pc_house_test_host_edit = NULL;
int g_pc_house_test_fidelity = 0; /* TEST-ONLY --house-test-fidelity (see pc_platform.h) */

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

/* WEEDS real-client verification, TEST-ONLY: --force-weed-pull. See pc_platform.h's own doc comment on this global. */
int g_pc_force_weed_pull = 0;

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
const char* g_pc_world_test_force = NULL; /* --world-test-force=W,I,T,P[,M] (HOST only, TEST-ONLY): see pc_platform.h */
int g_pc_ts_test_donate = 0;
int g_pc_ts_test_claim = 0;
/* Town services milestone 2 (shop) TEST-ONLY hooks: --shop-test-buy / --shop-test-sell. See pc_platform.h's own doc comment. */
int g_pc_shop_test_buy = 0;
int g_pc_shop_test_sell = 0;
/* Guest-first town TEST-ONLY hook: --house-buy-test H|auto (client, a guest). See pc_platform.h. */
const char* g_pc_house_buy_test = NULL;
/* Guest Nook dialogue (M4) TEST-ONLY hook: --nook-test SPEC (client only). See pc_platform.h. */
const char* g_pc_nook_test = NULL;
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

/* Guests G2: --bootstrap-guest NAME,LAND,PLAYER_ID,LAND_ID[,GENDER[,FACE]] (TEST-ONLY, default OFF, CLIENT role only). Non-interactively makes this process a
 * GUEST (a foreigner whose HOME PersonalID is the given one: name <= 8 chars, land <= 8 chars, ids decimal or 0x hex) visiting the town it
 * loaded -- the state a train arrival produces -- and spawns it at the station. Guests G1: a FRESH character (never a resident clone); the optional
 * GENDER (0|1) / FACE (0..7) are derived deterministically from the identity when omitted. See pc_m_card.c's pc_bootstrap_guest_poll(). NULL = off. */
const char* g_pc_bootstrap_guest = NULL;

/* Guests G2.3: --guest (CLIENT only, requires --connect): play as a GUEST with the persistent profile save/mp/guest.ini (created with defaults on first use; see
 * pc_guest_profile.h). It is validated at startup (exit 2 on any problem), then drives the SAME arrival path as --bootstrap-guest: the profile is turned into the
 * --bootstrap-guest spec stored in g_pc_guest_spec (the test hook stays as is).
 * --guest-profile NAME (implies --guest, same exclusivity) selects the independent profile save/mp/guest_<name>.ini + save/mp/guest_token_<name>.dat via
 * pc_guest_profile_select(): that module call is the ONE source of truth for the profile (also used by the title-menu item and the client token file). */
static int g_pc_guest = 0;
static char g_pc_guest_spec[96];

/* M2 (characters): --character NAME|UUIDPREFIX plays a character of the local store save/mp/characters (implies --guest; client only); --characters lists them
 * (+ the legacy guest profiles) and exits 0; --character-import-profile NAME copies a legacy guest profile (NAME "" / default = guest.ini) into the store, prints
 * the result and exits 0. The legacy files are never moved or deleted. See pc_character.h and docs/multiplayer-guest-roadmap.md. */
static const char* g_pc_character_spec = NULL;
static const char* g_pc_character_import = NULL;
static int g_pc_characters_list = 0;
/* M3 (servers): --server NAME (saved destination from save/mp/servers.ini, fills the --connect host:port; alone it implies --guest), --servers (list, exit 0),
 * --server-add NAME HOST[:PORT] / --server-delete NAME (one-shot admin, exit 0 / 2). See pc_servers.h and docs/multiplayer-guest-roadmap.md. */
static const char* g_pc_server_name = NULL;
static int g_pc_servers_list = 0;
static const char* g_pc_server_add_name = NULL;
static const char* g_pc_server_add_addr = NULL;
static const char* g_pc_server_delete = NULL;

/* First-run guest creation: --guest-profile NAME whose guest_<name>.ini does NOT exist plays the REAL vanilla Rover scene (name, gender, face) and the result is
 * written to the profile (pc_m_card.c pc_guest_creation_finish). --guest-creation-test NAME,GENDER,FACE is the TEST-ONLY hook that types those answers
 * without any UI once the Rover scene runs (requires --guest-profile; inert when the profile already exists). NULL = off. */
const char* g_pc_guest_creation_test = NULL;

/* --host-observer (opt-in, HOST role only): the host plays NO resident. It binds a hidden, inert, static observer identity (outside
 * Save_t.private_data[]) so that all four residents are claimable by clients. See pc/include/pc_host_observer.h and pc_m_card.c's
 * pc_host_observer_poll(). 0 = off (default: plain --host is unchanged). */
int g_pc_host_observer = 0;

/* --host / --connect: Stage 1 role selection. No settings.ini persistence (matches --time/
 * --date/--rain: a per-launch dev override, not a saved preference), no UI yet. */
static int      g_pc_net_role = 0; /* 0 = none/single-player, 1 = host, 2 = client */

/* Effective town_serve mode: --town-serve override, else an explicit settings.ini value, else AUTO = 1 (sanitized) for a --host --dedicated process and 0 otherwise. */
int pc_settings_town_serve_effective(void) {
    if (g_pc_town_serve_override >= 0) {
        return g_pc_town_serve_override;
    }
    if (g_pc_settings.town_serve >= 0) {
        return g_pc_settings.town_serve;
    }
    return (g_pc_dedicated && g_pc_net_role == 1) ? 1 : 0;
}
static uint16_t g_pc_net_port = 7777;
static char     g_pc_net_host_ip[64] = "127.0.0.1";

/* M-A / M-B (town cache, multiplayer phase 2): --town-dir DIR (hidden, CLIENT-only, requires --connect) selects DIR (e.g. save/mp/towns/<townkey>) as the Card-A
 * parent: the save is read from DIR/card_a instead of save/card_a (pc_card_set_town_dir, set BEFORE boot_main). --town-fetch (CLIENT-only, requires --connect,
 * exclusive with --town-dir) downloads the host's town first (pc_net_game_town_prefetch, after pc_platform_init and before boot_main). --town-serve on|off
 * (HOST-only) overrides settings.ini [Network] town_serve (g_pc_town_serve_override). */
static const char* g_pc_town_dir = NULL;
static int         g_pc_town_fetch = 0;
/* M-C (Play Online): --online-ui (hidden, set by the relaunch): failure boxes of the pre-boot fetch offer Retry / Use saved copy / Quit. Without it the fetch keeps its
 * silent fallback ladder (scripts / tests). g_pc_bootstrap_resident_pid(_set): a RESIDENT membership of the fetched town (20 BE PersonalID bytes) read by the thin
 * pc_bootstrap_resident_pid_poll() in pc_m_card.c, which resolves the slot and then drives the unchanged --bootstrap-resident path. */
static int         g_pc_online_ui = 0;
int                g_pc_bootstrap_resident_pid_set = 0;
unsigned char      g_pc_bootstrap_resident_pid[20];
/* Play Online without relaunch (pc_main_play_online_poll, below): 1 while the in-process connect runs; a STALE membership then is reported through s_po_stale instead of the
 * box + exit(3) of the CLI path. */
static int         s_po_inproc = 0;
static int         s_po_stale = 0;

/* M-G: pc_save_read_gci() found a SANITIZED town transfer image (a client cache) in a process that is not a network client: it must never be hosted, played offline as
 * a town or saved. Box (suppressed by the test hook AC_TOWN_NO_MSGBOX) + exit 3. */
void pc_main_refuse_sanitized_town(void) {
    const char* msg = "This save file is a sanitized town copy downloaded from a server (other residents' data is blanked). It cannot be hosted, played offline or saved as a town.\n"
                      "Join the server as a client, or restore your own town save.";
    fprintf(stderr, "[PC] REFUSED: %s\n", msg);
    if (getenv("AC_TOWN_NO_MSGBOX") == NULL) {
        SDL_ShowSimpleMessageBox(SDL_MESSAGEBOX_ERROR, "Animal Crossing - sanitized town copy", msg, g_pc_window);
    }
    fflush(NULL);
    exit(3);
}

/* SDL box for a failed fetch: 0 = Retry, 1 = Use saved copy (only when `offline`; the client STILL connects in the background), 2 = Quit (also when the box could not be shown). */
static int pc_town_failure_box(const char* msg, int offline) {
    SDL_MessageBoxButtonData btn[3];
    SDL_MessageBoxData box;
    int hit = -1, nb = 0;
    memset(btn, 0, sizeof(btn));
    btn[nb].flags = SDL_MESSAGEBOX_BUTTON_RETURNKEY_DEFAULT;
    btn[nb].buttonid = 0;
    btn[nb++].text = "Retry";
    if (offline) {
        btn[nb].buttonid = 1;
        btn[nb++].text = "Use saved copy";
    }
    btn[nb].flags = SDL_MESSAGEBOX_BUTTON_ESCAPEKEY_DEFAULT;
    btn[nb].buttonid = 2;
    btn[nb++].text = "Quit";
    memset(&box, 0, sizeof(box));
    box.flags = SDL_MESSAGEBOX_WARNING;
    box.window = g_pc_window;
    box.title = "Animal Crossing - Play Online";
    box.message = msg;
    box.numbuttons = nb;
    box.buttons = btn;
    if (SDL_ShowMessageBox(&box, &hit) != 0 || hit < 0) {
        return 2;
    }
    return hit;
}

/* M-I: polled every frame (pc_vi.c). A --town-fetch client whose guest was promoted (handoff stored + REJECT 6) restarts itself as
 * `--connect HOST:PORT --town-fetch --character UUID`: the new process fetches the town that now contains the resident and joins as it. g_pc_running is cleared ONLY
 * after the new process exists. A CLI client without --town-fetch (local town = card_a, which lacks the resident) only keeps the message. AC_RELAUNCH_DRYRUN=1 logs the
 * command line and keeps running. Consumed once, so it cannot loop (a resident process sends no guest claim and never gets a handoff). */
static int pc_main_play_online_rejoin(const char* host, int port, const char* uuid); /* the Play Online block below */

void pc_main_relaunch_poll(void) {
    char uuid[64], err[200];
    if (!pc_net_game_client_take_relaunch(uuid, sizeof(uuid))) {
        return;
    }
    if (!g_pc_town_fetch) {
        printf("[PC] M-I: promoted; this client was not started with --town-fetch: no automatic relaunch (restart it with --town-fetch / Play Online)\n");
        pc_net_game_promote_notice_show(); /* the legacy / CLI path still tells the user to restart */
        return;
    }
    err[0] = '\0';
    if (getenv("AC_RELAUNCH_DRYRUN") == NULL) {
        /* Guest-first town (M3): the promoted client re-joins IN THIS PROCESS (no restart, same window / audio / PID): the session is shut down and the Play Online
         * connect runs again as the resident (re-fetch of the town that now holds the resident, role = resident, bind by PersonalID, RESIDENT claim with the handed-over
         * token). The process relaunch below is the FALLBACK when the rejoin cannot even be requested (another Play Online request is running). */
        if (pc_main_play_online_rejoin(g_pc_net_host_ip, (int)g_pc_net_port, uuid)) {
            printf("[PC] M3: promoted: re-joining IN-PROCESS as the resident (no relaunch)\n");
            return;
        }
        printf("[PC] M3: promoted: the in-process rejoin could not be requested -- falling back to the process relaunch\n");
    }
    if (pc_relaunch_connect(g_pc_net_host_ip, (int)g_pc_net_port, PC_RELAUNCH_CHARACTER, uuid, err, sizeof(err))) {
        printf("[PC] M-I: promoted: relaunched as the resident (new process started), quitting this one\n");
        g_pc_running = 0;
    } else if (getenv("AC_RELAUNCH_DRYRUN") == NULL) {
        printf("[PC] M-I: promoted: relaunch FAILED: %s\n", err);
        pc_net_game_client_relaunch_failed(err);
    }
}

/* M-C: after the pre-boot fetch the town directory (save/mp/towns/<townkey>) is known. A STORE character with a RESIDENT membership of that town plays its resident
 * (session join kind RESIDENT, the guest arrival is disarmed, the slot is bound by PersonalID once the save is loaded); a guest membership / none keeps the guest
 * arrival (an existing character) or the Rover first-run creation (a new one). No town dir (legacy fallback) = no key = guest. */
static void pc_main_resolve_membership(void) {
    PCConnectSession* ss = pc_session();
    const char* dir = pc_card_town_dir();
    const char* base;
    char role[16];
    uint8_t pid[20];
    if (ss->storage != PC_CHARACTER_STORAGE_STORE || ss->creating || dir == NULL) {
        return;
    }
    base = strrchr(dir, '/');
    if (strrchr(dir, '\\') != NULL && (base == NULL || strrchr(dir, '\\') > base)) {
        base = strrchr(dir, '\\');
    }
    base = base != NULL ? base + 1 : dir;
    if (!pc_town_key_valid(base)) {
        return;
    }
    snprintf(ss->town_key, sizeof(ss->town_key), "%s", base);
    if (!pc_character_membership_read(NULL, ss->character.uuid, base, role, pid)) {
        printf("[PC] membership: character %s has no membership of town %s yet: arriving as a guest\n", ss->character.uuid, base);
        return;
    }
    if (strcmp(role, "resident") != 0) {
        printf("[PC] membership: character %s is a guest of town %s\n", ss->character.uuid, base);
        return;
    }
    /* M-J: a stale membership (the resident was removed, or the town was reset) must not leave the player on a silent title screen. The fetched GCI is checked here;
     * the membership is never downgraded (the operator may restore the resident). An unreadable file skips the check (the later PID poll still logs / shows a notice). */
    {
        static uint8_t gci[PC_TS_GCI_SIZE];
        size_t n = 0;
        int slot = -1;
        FILE* f = fopen(pc_gci_path(), "rb");
        if (f != NULL) {
            n = fread(gci, 1, sizeof(gci), f);
            fclose(f);
        }
        if (n == sizeof(gci) && !pc_town_gci_find_resident(gci, n, pid, &slot)) {
            const char* msg = "This character is no longer a resident of this town (removed or town reset): ask the operator.";
            fprintf(stderr, "[PC] membership: STALE: character %s is recorded as a resident of town %s but its PersonalID is not a resident of the fetched town: %s\n", ss->character.uuid, base, msg);
            if (s_po_inproc) { /* Play Online in this process: no box, no exit; the caller cancels the connect and shows the message in the menu */
                s_po_stale = 1;
                return;
            }
            if (getenv("AC_TOWN_NO_MSGBOX") == NULL) {
                SDL_MessageBoxButtonData btn;
                SDL_MessageBoxData box;
                int hit = -1;
                memset(&btn, 0, sizeof(btn));
                btn.flags = SDL_MESSAGEBOX_BUTTON_RETURNKEY_DEFAULT | SDL_MESSAGEBOX_BUTTON_ESCAPEKEY_DEFAULT;
                btn.buttonid = 2;
                btn.text = "Quit";
                memset(&box, 0, sizeof(box));
                box.flags = SDL_MESSAGEBOX_WARNING;
                box.window = g_pc_window;
                box.title = "Animal Crossing - Play Online";
                box.message = msg;
                box.numbuttons = 1;
                box.buttons = &btn;
                (void)SDL_ShowMessageBox(&box, &hit);
            }
            fflush(NULL);
            pc_platform_shutdown();
            exit(3);
        }
    }
    ss->join_kind = PC_SESSION_JOIN_RESIDENT;
    memcpy(ss->resident_pid, pid, sizeof(pid));
    memcpy(g_pc_bootstrap_resident_pid, pid, sizeof(pid));
    g_pc_bootstrap_resident_pid_set = 1;
    g_pc_bootstrap_guest = NULL; /* a resident is not a guest arrival */
    printf("[PC] membership: character %s is a RESIDENT of town %s: the resident will be bound by PersonalID after the save loads (credential token: characters/%s/towns/%s/token.dat)\n",
           ss->character.uuid, base, ss->character.uuid, base);
}

static void pc_town_title_progress(const char* text) {
    if (g_pc_window != NULL && text != NULL) {
        SDL_SetWindowTitle(g_pc_window, text);
    }
}

/* M2: decide whether this --guest / --guest-profile / --character session plays a STORE character. Returns 1 = handled (store character: the --bootstrap-guest spec
 * is built, a new one is armed for the Rover scene), 0 = use the LEGACY guest profile flow unchanged (also when --character names a legacy profile: it is
 * selected like --guest-profile), -1 = refused (message printed). */
static int pc_main_prepare_store_character(void) {
    PCConnectSession* ss = pc_session();
    PCCharacter c;
    PCGuestProfile gp;
    char err[600];
    int r, creating = 0;
    const char* bk = NULL;
    const char* why = NULL;
    ss->role = g_pc_net_role;
    snprintf(ss->host, sizeof(ss->host), "%s", g_pc_net_host_ip);
    ss->port = (int)g_pc_net_port;
    ss->join_kind = PC_SESSION_JOIN_GUEST;
    err[0] = '\0';
    if (g_pc_character_spec != NULL) {
        r = pc_character_resolve(NULL, g_pc_character_spec, &c, err, sizeof(err));
        if (r == PC_CHARACTER_AMBIGUOUS || r == PC_CHARACTER_ERR) {
            fprintf(stderr, "[PC] --character: REFUSED: %s\n", err);
            return -1;
        }
        if (r == PC_CHARACTER_ABSENT) {
            if (!pc_character_prepare_new(NULL, g_pc_character_spec, &c, err, sizeof(err))) {
                fprintf(stderr, "[PC] --character: REFUSED: no character '%s' exists and a new one cannot be created: %s\n", g_pc_character_spec, err);
                return -1;
            }
            creating = 1;
        }
    } else {
        r = pc_character_resolve_profile(NULL, pc_guest_profile_selected(), &c, err, sizeof(err));
        if (r != PC_CHARACTER_OK) {
            return 0; /* absent (first run) or unreadable: the legacy flow creates / reports exactly as before */
        }
    }
    if (c.storage == PC_CHARACTER_STORAGE_LEGACY) {
        (void)pc_guest_profile_select(c.legacy_profile);
        ss->storage = PC_CHARACTER_STORAGE_LEGACY;
        return 0;
    }
    memset(&gp, 0, sizeof(gp));
    memcpy(gp.name, c.name, sizeof(gp.name));
    memcpy(gp.home_town, c.home_town, sizeof(gp.home_town));
    gp.gender = c.gender;
    gp.face = c.face;
    gp.player_id = c.player_id;
    gp.land_id = c.land_id;
    if (!pc_guest_profile_validate(&gp, &bk, &why) || !pc_guest_profile_spec(&gp, g_pc_guest_spec, sizeof(g_pc_guest_spec))) {
        fprintf(stderr, "[PC] --guest: REFUSED: bad character %s: %s\n", c.uuid, bk != NULL ? bk : "internal error (spec buffer)");
        return -1;
    }
    ss->storage = PC_CHARACTER_STORAGE_STORE;
    ss->creating = creating;
    ss->character = c;
    snprintf(ss->guest_spec, sizeof(ss->guest_spec), "%s", g_pc_guest_spec);
    if (creating) {
        extern void pc_guest_creation_arm(const PCGuestProfile* p); /* pc_m_card.c */
        pc_guest_creation_arm(&gp);
        printf("[PC] --character: character '%s' does not exist yet: FIRST-RUN CREATION -- the real Rover scene (name, gender, face) will create "
               "characters/%s/character.ini; nothing is written before it finishes (placeholder name '%s', home town '%s', player id 0x%04X, land id 0x%04X)\n",
               g_pc_character_spec, c.uuid, gp.name, gp.home_town, (unsigned)gp.player_id, (unsigned)gp.land_id);
    } else {
        (void)pc_character_default_set(NULL, c.uuid);
        printf("[PC] --guest: loaded character %s (%s): name '%s', home town '%s', gender %d, face %d, player id 0x%04X, land id 0x%04X "
               "(wire identity = home PersonalID; the uuid is local only; tokens live in characters/%s/towns/<townkey>/token.dat)\n",
               c.uuid, c.path, gp.name, gp.home_town, gp.gender, gp.face, (unsigned)gp.player_id, (unsigned)gp.land_id, c.uuid);
    }
    g_pc_bootstrap_guest = g_pc_guest_spec;
    return 1;
}

/* ===== Play Online WITHOUT relaunch =====
 * The Play Online menu (pc_play_online_menu.c) files a request; pc_main_play_online_poll() (pc_vi.c, every frame, between pc_net_game_poll and the bootstrap polls) executes it
 * in THIS process: the steps are exactly what a relaunched `--connect H:P (--character U | --guest-profile N | --guest) --town-fetch --online-ui` process does before and at its
 * first title frame, then the play scene fades back to the title with the vanilla FADE_TYPE_OUT_RETURN_TITLE (trademark_init -> common_data_reinit -> pc_save_reload of the
 * fetched town), and only after the scene was left the existing one-shot bootstrap polls are armed so they fire on the NEW title:
 *   1 guards (role NONE, idle play scene, menu open) 2 snapshot 3 session + character 4 pre-boot town fetch (blocking, up to ~30 s; Retry / Use saved copy / Quit boxes)
 *   5 membership + GCI check 6 pc_net_game_start_client 7 COMMIT: load the fetched town (role is CLIENT before any sanitized image is read) 8 fade to the title
 *   9 arm the bootstrap polls once the scene is gone.
 * Any failure before the fade ROLLS BACK everything (town dir, session, guest selection, globals, net, save flags; the single-player town is re-read from save/card_a only if the
 * RAM save was already replaced) and returns to the menu with a message. Known limits: the fetch blocks the frame loop (window title shows the progress); an arrival failure after
 * the fade still exits(2) like the CLI path; the promoted-guest relaunch (pc_main_relaunch_poll) stays a relaunch. */
extern int  pc_play_online_scene_ready(void);                 /* pc_m_card.c */
extern int  pc_play_online_scene_left(void);
extern void pc_play_online_begin_return_title(void);
extern int  pc_play_online_save_load(void);
extern int  pc_play_online_save_flags_get(int* ready);
extern void pc_play_online_save_flags_set(int loaded, int ready);
extern void pc_guest_creation_disarm(void);
extern void pc_title_notice_post(const char* head, const char* msg, int secs); /* pc_m_card.c: a title-screen notice (the rejoin failure) */

enum { PO_IDLE = 0, PO_REQUESTED = 1, PO_FADING = 2 };
static struct {
    int  state;
    char host[64];
    int  port;
    int  kind;
    char name[40];
    int  wait;       /* frames waiting for an idle title scene */
    int  arm_guest;  /* arm g_pc_bootstrap_guest once the scene was left */
    int  arm_pid;    /* arm g_pc_bootstrap_resident_pid_set once the scene was left */
    int  rejoin;     /* guest-first purchase: the promoted guest re-joins as the resident from a LIVE (field / building) scene, no Play Online menu is open */
} s_po;
static char s_po_character[48]; /* backing store of g_pc_character_spec for the in-process connect */

int pc_main_play_online_request(const char* host, int port, int kind, const char* name) {
    char tmp[320];
    if (s_po.state != PO_IDLE || host == NULL || strlen(host) >= sizeof(s_po.host)) {
        return 0;
    }
    if (!pc_relaunch_build_args(host, port, kind, name, tmp, sizeof(tmp))) { /* the same validation as the relaunch had: IPv4 literal, port, [A-Za-z0-9-] name */
        return 0;
    }
    memset(&s_po, 0, sizeof(s_po));
    snprintf(s_po.host, sizeof(s_po.host), "%s", host);
    s_po.port = port;
    s_po.kind = kind;
    snprintf(s_po.name, sizeof(s_po.name), "%s", name != NULL ? name : "");
    s_po.state = PO_REQUESTED;
    return 1;
}

/* Guest-first town (M3): the in-process REJOIN of a promoted guest (called by pc_main_relaunch_poll after RESIDENT_HANDOFF + REJECT PROMOTED). The client session is shut
 * down (role NONE: the sanitized town image may only be read by a CLIENT, the new connect makes it one again), then the NORMAL Play Online connect (pc_po_connect) runs for
 * the same store character: the host serves the town that now contains the resident, the membership says role = resident, the slot is bound by PersonalID and the RESIDENT
 * claim carries the handed-over token. Differences to a menu-started connect: no Play Online menu is open (the guard is waived) and the start is a LIVE play scene (a field,
 * a building), so the existing fade to the title (FADE_TYPE_OUT_RETURN_TITLE) is what ends the old scene; for ~2 s that scene still runs on the freshly loaded RAM
 * (pc_save_ready is cleared first, so no save writer runs and the net client waits for the new title). A FAILURE shows a title notice and returns to the title: it NEVER
 * reloads save/card_a into the old live session. 1 = requested, 0 = refused (the caller falls back to the process relaunch). */
static int pc_main_play_online_rejoin(const char* host, int port, const char* uuid) {
    char tmp[320];
    if (s_po.state != PO_IDLE || host == NULL || uuid == NULL || uuid[0] == '\0' || strlen(host) >= sizeof(s_po.host)) {
        return 0;
    }
    if (!pc_relaunch_build_args(host, port, PC_RELAUNCH_CHARACTER, uuid, tmp, sizeof(tmp))) {
        return 0;
    }
    if (pc_net_game_role() != PC_NETGAME_ROLE_NONE) {
        pc_net_game_shutdown(); /* the REJECT left the (refused) client session standing: end it, the connect below starts a new one */
    }
    if (pc_net_game_role() != PC_NETGAME_ROLE_NONE) {
        return 0;
    }
    memset(&s_po, 0, sizeof(s_po));
    snprintf(s_po.host, sizeof(s_po.host), "%s", host);
    s_po.port = port;
    s_po.kind = PC_RELAUNCH_CHARACTER;
    snprintf(s_po.name, sizeof(s_po.name), "%s", uuid);
    s_po.rejoin = 1;
    s_po.state = PO_REQUESTED;
    return 1;
}

/* A rejoin that fails after the guest was promoted: the title shows why and the play scene fades back to it (never the Play Online menu, never card_a into this session). */
static void pc_main_rejoin_failed(const char* why) {
    static char s_msg[320];
    snprintf(s_msg, sizeof(s_msg), "You are now a resident of the town, but joining it again failed: %s Choose Play Online to join as the resident.", why != NULL ? why : "?");
    printf("[PC] M3: rejoin FAILED: %s -- returning to the title\n", s_msg);
    pc_title_notice_post("Promoted to a resident:", s_msg, 600);
    if (pc_play_online_scene_ready()) {
        pc_play_online_begin_return_title();
    }
}

/* The ONLY guest-profile selection of the in-process connect (the CLI has its own two call sites). NULL / "" = the default profile. */
static int pc_po_select_profile(const char* name) {
    return pc_guest_profile_select(name);
}

static void pc_po_progress(const char* text) {
    pc_town_title_progress(text);
    SDL_PumpEvents(); /* keep the window answering to Windows while the fetch blocks the frame loop (events stay queued for the next frame) */
}

/* The legacy guest-profile flow of main()'s `if (g_pc_guest)` block (same calls), for a Play Online connect whose character is NOT a store character. 1 = ok, 0 = refused. */
static int pc_po_prepare_legacy_guest(char* err, size_t errcap) {
    PCGuestProfile gp;
    char gerr[512];
    int gres, creating = 0;
    if (pc_guest_profile_selected() != NULL) {
        gres = pc_guest_profile_read_selected(&gp, gerr, sizeof(gerr));
        if (gres == PC_GUEST_PROFILE_ABSENT) {
            if (!pc_guest_profile_prepare_new_selected(&gp, gerr, sizeof(gerr))) {
                gres = PC_GUEST_PROFILE_ERR;
            } else {
                creating = 1;
            }
        }
    } else {
        gres = pc_guest_profile_load_or_create_selected(&gp, gerr, sizeof(gerr));
    }
    if (gres == PC_GUEST_PROFILE_ERR || !pc_guest_profile_spec(&gp, g_pc_guest_spec, sizeof(g_pc_guest_spec))) {
        fprintf(stderr, "[PC] play-online: guest profile REFUSED: %s\n", gres == PC_GUEST_PROFILE_ERR ? gerr : "internal error (spec buffer)");
        snprintf(err, errcap, "The guest profile is not usable: %.200s", gres == PC_GUEST_PROFILE_ERR ? gerr : "internal error");
        return 0;
    }
    if (creating) {
        extern void pc_guest_creation_arm(const PCGuestProfile* p); /* pc_m_card.c */
        pc_guest_creation_arm(&gp);
    }
    printf("[PC] play-online: guest profile %s: name '%s', player id 0x%04X, land id 0x%04X%s\n", pc_guest_profile_selected_path(), gp.name, (unsigned)gp.player_id, (unsigned)gp.land_id,
           creating ? " (FIRST-RUN CREATION in the Rover scene)" : "");
    g_pc_bootstrap_guest = g_pc_guest_spec;
    return 1;
}

/* Steps 2..8. 1 = committed (the fade to the title was requested), 0 = rolled back (err holds the menu message). */
static int pc_po_connect(char* err, size_t errcap) {
    PCConnectSession* ss = pc_session();
    PCConnectSession ss_snap = *ss;
    char host_snap[sizeof(g_pc_net_host_ip)];
    char gspec_snap[sizeof(g_pc_guest_spec)];
    char gprof_snap[48];
    char town_err[700];
    const char* chr_snap = g_pc_character_spec;
    const char* bg_snap = g_pc_bootstrap_guest;
    const int role_snap = g_pc_net_role, fetch_snap = g_pc_town_fetch, ui_snap = g_pc_online_ui, guest_snap = g_pc_guest, pidset_snap = g_pc_bootstrap_resident_pid_set;
    const uint16_t port_snap = g_pc_net_port;
    int ready_snap = 0, loaded_snap, net_started = 0, ram_touched = 0, rc, town_rc;
    const char* sel;

    loaded_snap = pc_play_online_save_flags_get(&ready_snap);
    memcpy(host_snap, g_pc_net_host_ip, sizeof(host_snap));
    memcpy(gspec_snap, g_pc_guest_spec, sizeof(gspec_snap));
    sel = pc_guest_profile_selected();
    snprintf(gprof_snap, sizeof(gprof_snap), "%s", sel != NULL ? sel : "");
    err[0] = '\0';

    /* step 1 (defensive): no town dir may be selected in a role-NONE process; a different one would be refused by pc_card_set_town_dir */
    if (pc_card_town_dir_active()) {
        pc_card_reset_town_dir_for_test();
    }

    /* step 3: the state the CLI parse leaves for `--connect H:P ... --town-fetch --online-ui` */
    s_po_inproc = 1;
    s_po_stale = 0;
    g_pc_net_role = 2;
    snprintf(g_pc_net_host_ip, sizeof(g_pc_net_host_ip), "%s", s_po.host);
    g_pc_net_port = (uint16_t)s_po.port;
    g_pc_town_fetch = 1;
    g_pc_online_ui = 1;
    g_pc_guest = 1;
    g_pc_bootstrap_guest = NULL;
    g_pc_bootstrap_resident_pid_set = 0;
    memset(ss, 0, sizeof(*ss));
    if (s_po.kind == PC_RELAUNCH_CHARACTER) {
        snprintf(s_po_character, sizeof(s_po_character), "%s", s_po.name);
        g_pc_character_spec = s_po_character;
        (void)pc_po_select_profile(NULL);
    } else if (s_po.kind == PC_RELAUNCH_PROFILE) {
        g_pc_character_spec = NULL;
        if (!pc_po_select_profile(s_po.name)) {
            snprintf(err, errcap, "Bad guest profile name");
            goto rollback;
        }
    } else {
        g_pc_character_spec = NULL;
        (void)pc_po_select_profile(NULL);
    }
    rc = pc_main_prepare_store_character();
    if (rc < 0) {
        snprintf(err, errcap, "This character cannot be used (see the log)");
        goto rollback;
    }
    if (rc == 0 && !pc_po_prepare_legacy_guest(err, errcap)) {
        goto rollback;
    }

    /* step 4: the pre-boot town fetch, with the box mapping Retry = loop, Use saved copy = continue, Quit / no usable town = CANCEL */
    for (;;) {
        town_err[0] = '\0';
        town_rc = pc_net_game_town_prefetch(g_pc_net_host_ip, g_pc_net_port, 30000u, pc_po_progress, g_pc_server_name, town_err, sizeof(town_err));
        if (town_rc == PC_TOWN_PREFETCH_ERROR) {
            fprintf(stderr, "[PC] play-online: town fetch: %s\n", town_err);
            if (getenv("AC_TOWN_NO_MSGBOX") == NULL && pc_town_failure_box(town_err, 0) == 0) {
                continue;
            }
            snprintf(err, errcap, "Could not get the host's town: %.120s", town_err);
            if (strchr(err, '\n') != NULL) *strchr(err, '\n') = '\0';
            goto rollback;
        }
        if ((town_rc == PC_TOWN_PREFETCH_CACHE || town_rc == PC_TOWN_PREFETCH_LEGACY) && getenv("AC_TOWN_NO_MSGBOX") == NULL) {
            char msg[800];
            int pick;
            snprintf(msg, sizeof(msg), "Could not get the host's current town (%s).\n\nRetry, or continue with the %s you already have?\n\"Use saved copy\" is not offline play: the game still tries to connect in the background.", town_err,
                     town_rc == PC_TOWN_PREFETCH_CACHE ? "saved copy of this server's town" : "town in save/card_a");
            pick = pc_town_failure_box(msg, 1);
            if (pick == 0) {
                pc_card_reset_town_dir_for_test();
                continue;
            }
            if (pick == 2) {
                snprintf(err, errcap, "%s", "Cancelled: the connection was not started");
                goto rollback;
            }
        }
        break;
    }

    /* step 5: membership (a stale one cancels instead of exit(3)) and a GCI pre-validation of the town that is about to be loaded */
    pc_main_resolve_membership();
    if (s_po_stale) {
        snprintf(err, errcap, "%s", "This character is no longer a resident of this town (removed or town reset): ask the operator.");
        goto rollback;
    }
    {
        PCTownId tid;
        if (!pc_save_validate_gci_file(pc_gci_path(), &tid)) {
            snprintf(err, errcap, "%s", "The town file is missing or not valid (see the log)");
            fprintf(stderr, "[PC] play-online: pre-validation of '%s' FAILED\n", pc_gci_path());
            goto rollback;
        }
    }
    /* the polls fire only on the NEW title: remember what the CLI would have armed and keep the globals clear until the scene was left */
    s_po.arm_guest = g_pc_bootstrap_guest != NULL;
    s_po.arm_pid = g_pc_bootstrap_resident_pid_set;
    g_pc_bootstrap_guest = bg_snap;
    g_pc_bootstrap_resident_pid_set = pidset_snap;

    /* step 6 */
    if (!pc_net_game_start_client(g_pc_net_host_ip, g_pc_net_port)) {
        snprintf(err, errcap, "%s", "Could not start the network client");
        goto rollback;
    }
    net_started = 1;
    if (s_po.rejoin) {
        pc_net_game_client_world_hold(1); /* the old live scene must not produce an identity claim (see pc_net_game.c): released when that scene was left */
    }

    /* step 7: COMMIT. The role is CLIENT now, so a sanitized town image may be read; pc_save_ready is cleared inside. */
    if (s_po.rejoin) {
        /* guest-first rejoin: the live scene keeps running for ~2 s while it fades out. It stays on its OWN (old, consistent) town RAM: only pc_save_ready is cleared (no save
         * writer, the net client waits), and the fetched town (already pre-validated above) is read by the title's trademark_init -> pc_save_reload, with the role CLIENT. */
        int rdy_now = 0;
        const int ld_now = pc_play_online_save_flags_get(&rdy_now);
        pc_play_online_save_flags_set(ld_now, 0);
        printf("[PC] play-online: rejoin: the old scene keeps its RAM; the fetched town is loaded by the title reload\n");
    } else {
        ram_touched = 1;
        if (!pc_play_online_save_load()) {
            snprintf(err, errcap, "%s", "The town could not be loaded (see the log)");
            goto rollback;
        }
    }

    /* step 8 */
    printf("[PC] play-online: in-process connect pid=%lu (no relaunch)\n",
#ifdef _WIN32
           (unsigned long)GetCurrentProcessId()
#else
           (unsigned long)getpid()
#endif
    );
    printf("[PC] play-online: connected to %s:%d as %s, town dir %s: fading back to the title to enter the town\n", g_pc_net_host_ip, (int)g_pc_net_port,
           ss->storage == PC_CHARACTER_STORAGE_STORE ? ss->character.uuid : "(legacy guest)", pc_card_town_dir() != NULL ? pc_card_town_dir() : "save/card_a");
    s_po_inproc = 0;
    pc_play_online_begin_return_title();
    return 1;

rollback:
    s_po_inproc = 0;
    printf("[PC] play-online: connect cancelled / failed, rolling back: %s\n", err);
    if (net_started) {
        pc_net_game_shutdown();
    }
    pc_card_reset_town_dir_for_test();
    pc_guest_creation_disarm();
    *ss = ss_snap;
    memcpy(g_pc_net_host_ip, host_snap, sizeof(g_pc_net_host_ip));
    memcpy(g_pc_guest_spec, gspec_snap, sizeof(g_pc_guest_spec));
    (void)pc_po_select_profile(gprof_snap);
    g_pc_character_spec = chr_snap;
    g_pc_bootstrap_guest = bg_snap;
    g_pc_bootstrap_resident_pid_set = pidset_snap;
    g_pc_net_role = role_snap;
    g_pc_net_port = port_snap;
    g_pc_town_fetch = fetch_snap;
    g_pc_online_ui = ui_snap;
    g_pc_guest = guest_snap;
    if (s_po.rejoin) {
        /* guest-first rejoin: the live scene belongs to the OLD client session -- never re-read save/card_a into it. Before the town RAM was replaced nothing changed (flags
         * restored); after it, pc_save_ready stays 0 (set by the failed load) and the caller fades to the title, whose reload reads whatever town dir the role allows. */
        if (!ram_touched) {
            pc_play_online_save_flags_set(loaded_snap, ready_snap);
        }
        return 0;
    }
    if (ram_touched && loaded_snap) {
        (void)pc_play_online_save_load(); /* the single-player town again (role NONE, default dir) */
    }
    pc_play_online_save_flags_set(loaded_snap, ready_snap);
    return 0;
}

void pc_main_play_online_poll(void) {
    char err[300];
    if (s_po.state == PO_IDLE) {
        return;
    }
    if (s_po.state == PO_REQUESTED) {
        if (s_po.rejoin) {
            /* guest-first rejoin: no menu; the play scene is live (field / building). Wait for an idle scene (no entrance / exit wipe), then the same connect. */
            if (pc_net_game_role() != PC_NETGAME_ROLE_NONE) {
                pc_net_game_shutdown();
            }
            if (pc_nook_house_rejoin_hold()) {
                return; /* guest Nook dialogue (M4): the congratulation row of the purchase is still being read (bounded, see pc_nook_house.h) */
            }
            if (!pc_play_online_scene_ready()) {
                if (++s_po.wait > 600) {
                    s_po.state = PO_IDLE;
                    s_po.rejoin = 0;
                    pc_main_rejoin_failed("the game never became idle.");
                }
                return;
            }
            if (pc_po_connect(err, sizeof(err))) {
                s_po.state = PO_FADING;
            } else {
                s_po.state = PO_IDLE;
                pc_main_rejoin_failed(err);
                s_po.rejoin = 0;
            }
            return;
        }
        if (!pc_play_online_menu_active()) {
            s_po.state = PO_IDLE;
            return;
        }
        if (pc_net_game_role() != PC_NETGAME_ROLE_NONE) {
            s_po.state = PO_IDLE;
            pc_play_online_menu_connect_result(0, "Already online");
            return;
        }
        if (!pc_play_online_scene_ready()) {
            if (++s_po.wait > 600) {
                s_po.state = PO_IDLE;
                pc_play_online_menu_connect_result(0, "The title is busy, try again");
            }
            return;
        }
        if (pc_po_connect(err, sizeof(err))) {
            s_po.state = PO_FADING;
            pc_play_online_menu_connect_result(1, "Connected: entering the town ...");
        } else {
            s_po.state = PO_IDLE;
            pc_play_online_menu_connect_result(0, err);
        }
        return;
    }
    /* PO_FADING: the fade-to-title runs in the play scene; arm the one-shot bootstrap polls only after the scene was left, otherwise they would fire on the OLD title */
    if (pc_play_online_scene_left()) {
        if (s_po.arm_guest) {
            g_pc_bootstrap_guest = g_pc_guest_spec;
        }
        if (s_po.arm_pid) {
            g_pc_bootstrap_resident_pid_set = 1;
        }
        pc_net_game_client_world_hold(0); /* guest-first rejoin: the old scene is gone, the normal readiness rules apply to the new title / town */
        printf("[PC] play-online: title scene left: bootstrap %s armed for the new title\n", s_po.arm_pid ? "resident-by-PID" : s_po.arm_guest ? "guest arrival" : "(none)");
        s_po.state = PO_IDLE;
        s_po.rejoin = 0;
        pc_play_online_menu_close();
    }
}

int main(int argc, char* argv[]) {
    pc_relaunch_forward_capture(argc, argv); /* M-C: the whitelisted display / diagnostic options are forwarded by a Play Online relaunch */
    for (int i = 1; i < argc; i++) {
        /* pc_log: -debug*, --debug*, -debug-list, -logtime, -logfile PATH (see pc_log.h). -debug-list exits 0 and a bad value exits 2 HERE,
         * before any game init. Anything that is not a log flag falls through to the chain below untouched. */
        {
            int log_consumed = 0;
            int log_r = pc_log_cli_arg(argv[i], i + 1 < argc ? argv[i + 1] : NULL, &log_consumed);
            if (log_r == 3) {
                return 0;
            }
            if (log_r == 2) {
                return 2;
            }
            if (log_r == 1) {
                i += log_consumed;
                continue;
            }
        }
        if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            printf("Usage: AnimalCrossing [options]\n");
            printf("  --verbose, -v       Enable diagnostic output\n");
            printf("  -debug              Enable GENERAL debug output only (see --debug-list); -debug<category> / --debug=a,b,c / -debugall\n");
            printf("                      select categories; env PC_LOG=a,b,c is ORed in; -logtime stamps lines; -logfile PATH appends them to a file\n");
            printf("  --debug-list        List the debug categories and exit\n");
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
            printf("  --host-observer     HOST-only option (requires --host; exit code 2 otherwise; cannot be combined with\n");
            printf("                      --bootstrap-resident, --bootstrap-guest or the host-self test hooks that need a host\n");
            printf("                      player): the host plays NO resident. It binds a hidden, inert, static server identity\n");
            printf("                      (never saved, never sent to clients) parked in the town field, so all four residents\n");
            printf("                      can be claimed by clients. Default off; plain --host is unchanged. See pc_host_observer.h.\n");
            printf("  --dedicated         HOST-only option (requires --host; exit code 2 otherwise; cannot be combined with --connect,\n");
            printf("                      --bootstrap-resident, --bootstrap-guest or the host-self test hooks): run as an unattended\n");
            printf("                      server. Implies --host-observer (no host resident), creates a HIDDEN window, no vsync, draws\n");
            printf("                      nothing, uses SDL's dummy audio driver, keeps the console on and reads commands from stdin:\n");
            printf("                      help, status, players, save, stop (Ctrl+C also stops gracefully). Default off; plain --host,\n");
            printf("                      --host-observer and --bootstrap-resident are unchanged. See pc_dedicated.h.\n");
            printf("  --bootstrap-guest NAME,LAND,PLAYER_ID,LAND_ID[,GENDER[,FACE]]  TEST-ONLY, CLIENT role only, default off:\n");
            printf("                      become a GUEST (foreigner with that HOME PersonalID) in the loaded town\n");
            printf("                      and spawn at the station; no save is ever written. The guest is a FRESH\n");
            printf("                      character (empty pockets / wallet, never a copy of a resident). Optional\n");
            printf("                      GENDER (0 male, 1 female) and FACE (0..7); omitted values are derived\n");
            printf("                      deterministically from the identity. The name must be a valid game name\n");
            printf("                      and must not equal a resident's name (otherwise exit code 2).\n");
            printf("                      See pc_m_card.c pc_bootstrap_guest_poll().\n");
            printf("  --max-guests N      HOST: most guests (visitors with their own character) connected at once, 1..8 (default 4 or settings.ini\n");
            printf("                      max_guests); a new guest beyond the cap is refused like a full server, residents are never refused.\n");
            printf("  --allow-new-guests 0|1  HOST-only (exit 2 otherwise): 0 = refuse guests whose key this host does not know yet (known guests still return); default 1 or\n");
            printf("                      settings.ini [Network] allow_new_guests.\n");
            printf("  --resident-tokens off|tofu|required  HOST-only (exit 2 otherwise): resident credentials (default off or settings.ini resident_tokens). tofu: a resident's\n");
            printf("                      first claim mints its token, later claims must present it. required: a resident without a credential is refused until the\n");
            printf("                      operator runs resident-arm. See docs/multiplayer-guest-roadmap.md.\n");
            printf("  --town-fetch        CLIENT-only (requires --connect; exit 2 otherwise; not with --town-dir): before the game boots, download the host's town\n");
            printf("                      save into save/mp/towns/<townkey>/ and play it (progress in the window title). Falls back to the cached town of that\n");
            printf("                      server, then to save/card_a. The host must serve it (--town-serve on).\n");
            printf("  --personal-sync on|off HOST-only (exit 2 otherwise): authenticated per-resident diary sync (default: on only while the town is served, i.e. town_serve is not off; settings.ini personal_sync).\n");
            printf("  --town-serve off|on|full HOST-only (exit 2 otherwise): serve the town save to --town-fetch clients (default: ON/sanitized for a --dedicated host, off otherwise; settings.ini town_serve = auto|off|on|full).\n");
            printf("                      on = a SANITIZED copy (other residents' pockets, mail, diary, designs and villager letters are blanked; names / houses / furniture are NOT);\n");
            printf("                      full = the whole file, unsanitized (every resident's private data: friends / LAN only). While 'on' the legacy 'client wins once' import is off.\n");
            printf("  --guest             CLIENT-only (requires --connect; exit code 2 with usage otherwise; cannot be combined with\n");
            printf("                      --host, --dedicated, --host-observer, --bootstrap-resident or --bootstrap-guest): join the host as a\n");
            printf("                      GUEST with your own new character instead of a resident. The profile lives in save/mp/guest.ini\n");
            printf("                      (name, gender, face, home_town, player_id, land_id); it is created with defaults on first use (edit the\n");
            printf("                      name / look BEFORE the first join; the ids are permanent: the same file is the same guest on every run).\n");
            printf("                      A broken guest.ini is never overwritten: the game prints the bad key and exits with code 2. A guest\n");
            printf("                      starts with empty pockets and wallet (the host enforces it) and needs a copy of the host town save.\n");
            printf("  --guest-profile NAME  CLIENT-only (same rules and refusals as --guest, which it implies; it may also be given together\n");
            printf("                      with --guest): play as the independent guest profile NAME so SEVERAL guests can run on ONE PC. NAME is\n");
            printf("                      1..16 characters of A-Z a-z 0-9 - (not starting with '-', no dots / spaces / path characters, not a\n");
            printf("                      Windows device name such as CON, NUL, COM1, LPT1; exit code 2 otherwise). Files: save/mp/guest_<name>.ini\n");
            printf("                      and save/mp/guest_token_<name>.dat, <name> in lower case ('Alice' and 'alice' are the same profile). A new\n");
            printf("                      profile gets its own random ids and the display name NAME (cut to 8 characters; 'GuestXX' if unusable) and\n");
            printf("                      differs from every other profile of the folder. Without --guest-profile, --guest keeps using\n");
            printf("                      save/mp/guest.ini and save/mp/guest_token.dat. One profile must not run in two processes at once.\n");
            printf("                      FIRST RUN of a --guest-profile NAME whose file does not exist: the profile is NOT auto-created; you play the real\n");
            printf("                      Rover train scene (name, gender, face) and the answers are saved to save/mp/guest_<name>.ini when it ends\n");
            printf("                      (an interrupted creation writes nothing and replays next launch). TEST-ONLY: --guest-creation-test\n");
            printf("                      NAME,GENDER,FACE answers the Rover scene without UI. Plain --guest keeps auto-creating guest.ini.\n");
            printf("  --character NAME|UUIDPREFIX  CLIENT-only (implies --guest; not with --guest-profile): play a character of the local store\n");
            printf("                      save/mp/characters/<uuid>/ (player-owned identity; one token file PER host town). An unknown NAME starts the\n");
            printf("                      first-run creation in the Rover scene. --characters lists the store + legacy guest profiles and exits 0;\n");
            printf("                      --character-import-profile NAME (\"\" / default = guest.ini) copies a legacy profile and its tokens into the\n");
            printf("                      store and exits 0 (the legacy files are never modified; afterwards --guest-profile NAME uses the character).\n");
            printf("  --server NAME       CLIENT: connect to the saved server NAME (save/mp/servers.ini; fills --connect HOST:PORT; refused with --connect /\n");
            printf("                      --host / --dedicated or an unknown NAME, exit 2). Use it with --character NAME|UUID (or --guest-profile NAME);\n");
            printf("                      alone it implies --guest (the default guest profile). A server is only a destination; characters are not tied to it.\n");
            printf("  --servers           list the saved servers and exit 0.  --server-add NAME HOST[:PORT]  save a server (HOST = IPv4 literal, port default\n");
            printf("                      7777; no hostname resolution).  --server-delete NAME  remove one. Exit 0, or 2 on a refusal.\n");
            printf("  --no-house-sync     HOST: turn OFF furniture sync, which a --dedicated host enables by default.\n");
            printf("  --house-sync        HOST opt-in (already the DEFAULT for --dedicated): the host is authoritative for the furniture of player houses (the owner's edits are committed to\n");
            printf("                      the host together with the pocket record; announced to every client in HOST_CONFIG). Off by default.\n");
            printf("  --authoritative-wildlife  Opt-in MODE flag (persistent, like --host/--connect --\n");
            printf("                      not a one-shot test hook): activates the host-authoritative\n");
            printf("                      fish/bug spawn adapter (pc_wildlife_authority.c). Off by default;\n");
            printf("                      without it, every role spawns wildlife locally exactly as before\n");
            printf("                      this milestone -- see pc_platform.h and ac_set_manager.c.\n");
            printf("                      HOST decides: the host announces the mode to every client at READY\n");
            printf("                      (TOWN_SVC_STATE service 4); on a CLIENT the flag is ignored.\n");
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
            printf("  --force-weed-pull   Client-only test hook: force a real Player_actor_request_main_remove_grass()\n");
            printf("                      at the --field-action-test-seed weed fixture (24,108), driving the REAL\n");
            printf("                      pull animation to the WEED_PULL network seam -- see pc_net_game.c.\n");
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
            printf("  --world-test-force=W,I,T,P[,M[,E]]  HOST-only TEST hook (default off; loud logs): force weather W,\n"
                   "                      intensity I, Stalk Market trend T, daily prices P+weekday and optionally a\n"
                   "                      clock shift of M minutes once the world is ready; E (month*100+day) pokes the\n"
                   "                      host's Wisp date 4 s after a client is READY. See pc_platform.h.\n");
            printf("  --ts-test-donate    Client-only TEST hook (default off; loud logs): drive ONE real museum donation\n"
                   "                      through the town-service transaction path. See pc_platform.h.\n");
            printf("  --ts-test-claim     Client-only TEST hook (default off; loud logs): drive ONE real lost-and-found\n"
                   "                      claim through the town-service transaction path. See pc_platform.h.\n");
            printf("  --shop-test-buy     Client-only TEST hook (default off; loud logs): drive ONE real shop purchase\n"
                   "                      through the town-service transaction path. See pc_platform.h.\n");
            printf("  --shop-test-sell    Client-only TEST hook (default off; loud logs): drive ONE real shop sale\n"
                   "                      through the town-service transaction path. See pc_platform.h.\n");
            printf("  --house-buy-test H|auto  Client-only TEST hook (default off; loud logs): as a GUEST buy house H (0..3) or `auto`\n"
                   "                      through the HOUSE_PURCHASE transaction, then re-join as the resident. See pc_platform.h.\n");
            printf("  --nook-test SPEC    Client-only TEST hook (default off; loud logs) for the guest Nook dialogue: SPEC = wallet=N (set the LOCAL\n"
                   "                      wallet once) and / or warp (go into Nook's shop). See pc_net_game.c pcnetgame_run_nook_test_hook().\n");
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
            g_pc_log_mask |= PCL_LEGACY | PCL_GENERAL; /* pc_log: --verbose == LEGACY + GENERAL; g_pc_verbose itself is unchanged */
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
        } else if (strcmp(argv[i], "--force-weed-pull") == 0) {
            g_pc_force_weed_pull = 1;
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
        } else if (strncmp(argv[i], "--world-test-force=", 19) == 0) {
            g_pc_world_test_force = argv[i] + 19;
            printf("[NET][WORLD][TEST-ONLY] --world-test-force=%s armed (a TEST hook: not for normal play)\n", g_pc_world_test_force);
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
        } else if (strcmp(argv[i], "--house-buy-test") == 0 && i + 1 < argc) {
            g_pc_house_buy_test = argv[++i];
            printf("[NET][HOUSE][TEST-ONLY] --house-buy-test %s armed (a TEST hook: not for normal play)\n", g_pc_house_buy_test);
        } else if (strcmp(argv[i], "--nook-test") == 0 && i + 1 < argc) {
            g_pc_nook_test = argv[++i];
            printf("[NET][HOUSE][TEST-ONLY] --nook-test %s armed (a TEST hook: not for normal play)\n", g_pc_nook_test);
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
        } else if (strcmp(argv[i], "--bootstrap-guest") == 0 && i + 1 < argc) {
            g_pc_bootstrap_guest = argv[i + 1];
            i++;
        } else if (strcmp(argv[i], "--bootstrap-guest") == 0) {
            /* Guests G1.1: the option is the last argument (no spec follows): never silently ignored */
            fprintf(stderr, "[PC] --bootstrap-guest: REFUSED: the option needs a spec argument NAME,LAND,PLAYER_ID,LAND_ID[,GENDER[,FACE]]\n");
            return 2;
        } else if (strcmp(argv[i], "--max-guests") == 0) {
            /* Guests G4: HOST-side cap on simultaneously bound guests (overrides settings.ini max_guests); a bad / missing value is never ignored. */
            const int mg = (i + 1 < argc) ? atoi(argv[i + 1]) : 0;
            if (i + 1 >= argc || mg < 1 || mg > 8) {
                fprintf(stderr, "[PC] --max-guests: REFUSED: the option needs a number 1..8 (most guests connected at once)\n");
                return 2;
            }
            g_pc_max_guests_override = mg;
            i++;
        } else if (strcmp(argv[i], "--town-dir") == 0) {
            if (i + 1 >= argc || argv[i + 1][0] == '\0' || g_pc_town_dir != NULL) {
                fprintf(stderr, "[PC] --town-dir: REFUSED: the option needs one DIRECTORY (the town cache dir, e.g. save/mp/towns/<townkey>)\n"
                                "usage: AnimalCrossing --connect HOST[:PORT] --town-dir DIR\n");
                return 2;
            }
            g_pc_town_dir = argv[i + 1];
            i++;
        } else if (strcmp(argv[i], "--town-fetch") == 0) {
            g_pc_town_fetch = 1;
        } else if (strcmp(argv[i], "--online-ui") == 0) {
            g_pc_online_ui = 1; /* hidden: set by the Play Online relaunch (interactive fetch failure boxes) */
        } else if (strcmp(argv[i], "--town-serve") == 0) {
            if (i + 1 >= argc || (strcmp(argv[i + 1], "on") != 0 && strcmp(argv[i + 1], "off") != 0 && strcmp(argv[i + 1], "full") != 0)) {
                fprintf(stderr, "[PC] --town-serve: REFUSED: the option needs off, on or full\n"
                                "usage: AnimalCrossing --host [port] --town-serve off|on|full\n");
                return 2;
            }
            g_pc_town_serve_override = strcmp(argv[i + 1], "full") == 0 ? 2 : strcmp(argv[i + 1], "on") == 0 ? 1 : 0; /* M-G: 0 off | 1 sanitized | 2 full (unsanitized) */
            i++;
        } else if (strcmp(argv[i], "--personal-sync") == 0) {
            if (i + 1 >= argc || (strcmp(argv[i + 1], "on") != 0 && strcmp(argv[i + 1], "off") != 0)) {
                fprintf(stderr, "[PC] --personal-sync: REFUSED: the option needs on or off\n"
                                "usage: AnimalCrossing --host [port] --personal-sync on|off\n");
                return 2;
            }
            g_pc_personal_sync_override = strcmp(argv[i + 1], "on") == 0 ? 1 : 0; /* personal data sync (diary): host operator override of settings.ini personal_sync */
            i++;
        } else if (strcmp(argv[i], "--allow-new-guests") == 0) {
            if (i + 1 >= argc || (strcmp(argv[i + 1], "0") != 0 && strcmp(argv[i + 1], "1") != 0)) {
                fprintf(stderr, "[PC] --allow-new-guests: REFUSED: the option needs 0 or 1\n"
                                "usage: AnimalCrossing --host [port] --allow-new-guests 0|1\n");
                return 2;
            }
            g_pc_allow_new_guests_override = argv[i + 1][0] == '1' ? 1 : 0;
            i++;
        } else if (strcmp(argv[i], "--resident-tokens") == 0) {
            if (i + 1 >= argc || (strcmp(argv[i + 1], "off") != 0 && strcmp(argv[i + 1], "tofu") != 0 && strcmp(argv[i + 1], "required") != 0)) {
                fprintf(stderr, "[PC] --resident-tokens: REFUSED: the option needs off, tofu or required\n"
                                "usage: AnimalCrossing --host [port] --resident-tokens off|tofu|required\n");
                return 2;
            }
            g_pc_resident_tokens_override = strcmp(argv[i + 1], "required") == 0 ? 2 : strcmp(argv[i + 1], "tofu") == 0 ? 1 : 0;
            i++;
        } else if (strcmp(argv[i], "--guest") == 0) {
            g_pc_guest = 1;
        } else if (strcmp(argv[i], "--guest-creation-test") == 0) {
            if (i + 1 >= argc || argv[i + 1][0] == '\0') {
                fprintf(stderr, "[PC] --guest-creation-test: REFUSED: the option needs NAME,GENDER,FACE (TEST-ONLY, with --guest-profile)\n");
                return 2;
            }
            g_pc_guest_creation_test = argv[i + 1];
            i++;
        } else if (strcmp(argv[i], "--guest-profile") == 0) {
            /* Several guests on ONE PC: --guest-profile NAME implies --guest and selects the independent profile save/mp/guest_<name>.ini (+ its own token file
             * save/mp/guest_token_<name>.dat). A missing / invalid / repeated value is never ignored: exit 2 naming the rule. */
            char perr[160];
            if (i + 1 >= argc || argv[i + 1][0] == '\0') {
                fprintf(stderr, "[PC] --guest-profile: REFUSED: the option needs a profile NAME (1..16 characters of A-Z a-z 0-9 -)\n"
                                "usage: AnimalCrossing --connect HOST[:PORT] --guest-profile NAME   (see --help)\n");
                return 2;
            }
            if (!pc_guest_profile_name_check(argv[i + 1], perr, sizeof(perr))) {
                fprintf(stderr, "[PC] --guest-profile: REFUSED: '%s': %s\n"
                                "usage: AnimalCrossing --connect HOST[:PORT] --guest-profile NAME   (see --help)\n", argv[i + 1], perr);
                return 2;
            }
            if (pc_guest_profile_selected() != NULL) {
                fprintf(stderr, "[PC] --guest-profile: REFUSED: the option was given more than once (one process plays one guest profile)\n"
                                "usage: AnimalCrossing --connect HOST[:PORT] --guest-profile NAME   (see --help)\n");
                return 2;
            }
            (void)pc_guest_profile_select(argv[i + 1]);
            g_pc_guest = 1;
            i++;
        } else if (strcmp(argv[i], "--character") == 0) {
            if (i + 1 >= argc || argv[i + 1][0] == '\0' || g_pc_character_spec != NULL) {
                fprintf(stderr, "[PC] --character: REFUSED: the option needs ONE NAME or UUID-PREFIX (see --characters)\n"
                                "usage: AnimalCrossing --connect HOST[:PORT] --character NAME|UUIDPREFIX   (see --help)\n");
                return 2;
            }
            g_pc_character_spec = argv[i + 1];
            g_pc_guest = 1;
            i++;
        } else if (strcmp(argv[i], "--character-import-profile") == 0) {
            if (i + 1 >= argc) {
                fprintf(stderr, "[PC] --character-import-profile: REFUSED: the option needs a legacy profile NAME (\"\" or default = save/mp/guest.ini)\n"
                                "usage: AnimalCrossing --character-import-profile NAME   (see --help)\n");
                return 2;
            }
            g_pc_character_import = argv[i + 1];
            i++;
        } else if (strcmp(argv[i], "--characters") == 0) {
            g_pc_characters_list = 1;
        } else if (strcmp(argv[i], "--servers") == 0) {
            g_pc_servers_list = 1;
        } else if (strcmp(argv[i], "--server") == 0) {
            if (i + 1 >= argc || argv[i + 1][0] == '\0' || g_pc_server_name != NULL) {
                fprintf(stderr, "[PC] --server: REFUSED: the option needs ONE saved server NAME (see --servers)\n"
                                "usage: AnimalCrossing --server NAME [--character NAME|UUIDPREFIX]   (see --help)\n");
                return 2;
            }
            g_pc_server_name = argv[i + 1];
            i++;
        } else if (strcmp(argv[i], "--server-add") == 0) {
            if (i + 2 >= argc || argv[i + 1][0] == '\0' || g_pc_server_add_name != NULL) {
                fprintf(stderr, "[PC] --server-add: REFUSED: the option needs NAME HOST[:PORT]\n"
                                "usage: AnimalCrossing --server-add NAME HOST[:PORT]   (HOST = IPv4 literal, port default 7777; see --help)\n");
                return 2;
            }
            g_pc_server_add_name = argv[i + 1];
            g_pc_server_add_addr = argv[i + 2];
            i += 2;
        } else if (strcmp(argv[i], "--server-delete") == 0) {
            if (i + 1 >= argc || argv[i + 1][0] == '\0' || g_pc_server_delete != NULL) {
                fprintf(stderr, "[PC] --server-delete: REFUSED: the option needs a saved server NAME\n"
                                "usage: AnimalCrossing --server-delete NAME   (see --servers)\n");
                return 2;
            }
            g_pc_server_delete = argv[i + 1];
            i++;
        } else if (strcmp(argv[i], "--host-observer") == 0) {
            g_pc_host_observer = 1;
        } else if (strcmp(argv[i], "--dedicated") == 0) {
            g_pc_dedicated = 1;
            pc_dedicated_early_console(); /* attach/allocate a console now so even the refusal text below is visible (valid redirected handles are kept) */
        } else if (strcmp(argv[i], "--authoritative-wildlife") == 0) {
            g_pc_authoritative_wildlife = 1;
        } else if (strcmp(argv[i], "--house-sync") == 0) {
            g_pc_house_sync = 1;
            s_house_sync_explicit = 1;
        } else if (strcmp(argv[i], "--no-house-sync") == 0) {
            g_pc_house_sync = 0;
            s_house_sync_explicit = 1;
        } else if (strcmp(argv[i], "--house-test-host-in-house") == 0 && i + 1 < argc) {
            g_pc_house_test_host_in_house = atoi(argv[i + 1]);
            printf("[NET][HOUSE][TEST-ONLY] --house-test-host-in-house %d armed (a TEST hook: not for normal play)\n", g_pc_house_test_host_in_house);
            i++;
        } else if (strcmp(argv[i], "--house-test-fidelity") == 0) {
            g_pc_house_test_fidelity = 1;
            printf("[NET][HOUSE][TEST-ONLY] --house-test-fidelity armed (logs MATCH / MISMATCH of the live room export at every room teardown; not for normal play)\n");
        } else if (strcmp(argv[i], "--house-test-host-edit") == 0 && i + 1 < argc) {
            g_pc_house_test_host_edit = argv[i + 1];
            printf("[NET][HOUSE][TEST-ONLY] --house-test-host-edit %s armed (a TEST hook: not for normal play)\n", g_pc_house_test_host_edit);
            i++;
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

    if (g_pc_nook_test != NULL && g_pc_net_role != 2) {
        fprintf(stderr, "[NET][HOUSE][TEST-ONLY] REFUSED: --nook-test is a CLIENT-only test hook (use it together with --connect)\n");
        return 2;
    }

    if (g_pc_house_buy_test != NULL && (g_pc_net_role != 2 || !(strcmp(g_pc_house_buy_test, "auto") == 0 || (g_pc_house_buy_test[0] >= '0' && g_pc_house_buy_test[0] <= '3' && g_pc_house_buy_test[1] == '\0')))) {
        fprintf(stderr, "[NET][HOUSE][TEST-ONLY] REFUSED: --house-buy-test is a CLIENT-only test hook (use it together with --connect) and takes 0, 1, 2, 3 or auto\n");
        return 2;
    }

    /* M3: the one-shot server profile commands (no network, no window): add, delete, list, then exit 0 (2 on a refusal). */
    if (g_pc_servers_list || g_pc_server_add_name != NULL || g_pc_server_delete != NULL) {
        char serr[400];
        if (g_pc_server_add_name != NULL) {
            PCServer ns;
            const int too_long = strlen(g_pc_server_add_name) > PC_SERVER_NAME_MAX;
            memset(&ns, 0, sizeof(ns));
            if (!too_long) {
                snprintf(ns.name, sizeof(ns.name), "%s", g_pc_server_add_name);
            }
            if (too_long || !pc_servers_parse_hostport(g_pc_server_add_addr, ns.address, &ns.port, serr, sizeof(serr))) {
                fprintf(stderr, "[PC] --server-add: REFUSED: %s\nusage: AnimalCrossing --server-add NAME HOST[:PORT]   (HOST = IPv4 literal)\n",
                        too_long ? "server name must be 1..32 characters" : serr);
                return 2;
            }
            if (pc_servers_add(NULL, &ns, serr, sizeof(serr)) != PC_SERVERS_OK) {
                fprintf(stderr, "[PC] --server-add: REFUSED: %s\n", serr);
                return 2;
            }
            printf("[PC] --server-add: saved server '%s' = %s:%d\n", ns.name, ns.address, ns.port);
        }
        if (g_pc_server_delete != NULL) {
            if (pc_servers_delete(NULL, g_pc_server_delete, serr, sizeof(serr)) != PC_SERVERS_OK) {
                fprintf(stderr, "[PC] --server-delete: REFUSED: %s\n", serr);
                return 2;
            }
            printf("[PC] --server-delete: deleted server '%s'\n", g_pc_server_delete);
        }
        if (g_pc_servers_list) {
            static PCServer sl[PC_SERVER_MAX];
            int sn = 0, k;
            if (pc_servers_load(NULL, sl, PC_SERVER_MAX, &sn, serr, sizeof(serr)) != PC_SERVERS_OK) {
                fprintf(stderr, "[PC] --servers: REFUSED: %s\n", serr);
                return 2;
            }
            printf("[PC] servers (%s/servers.ini): %d\n", PC_GUEST_PROFILE_DIR, sn);
            for (k = 0; k < sn; k++) {
                printf("  name='%s' address=%s port=%d last_character='%s' last_town='%s'\n", sl[k].name, sl[k].address, sl[k].port, sl[k].last_character, sl[k].last_town);
            }
        }
        fflush(stdout);
        return 0;
    }

    /* M3: --server NAME = a saved destination: refused (exit 2) with --connect / --host / --dedicated / --host-observer or an unknown NAME; otherwise it fills the
     * --connect host:port (role 2) and implies --guest (with --character / --guest-profile that character plays, else the default guest profile). */
    if (g_pc_server_name != NULL) {
        static const char k_server_usage[] = "usage: AnimalCrossing --server NAME [--character NAME|UUIDPREFIX]   (see --servers, --help)\n";
        static PCServer sl[PC_SERVER_MAX];
        char serr[400];
        const char* conflict = NULL;
        int sn = 0, a, idx;
        for (a = 1; a < argc && conflict == NULL; a++) {
            if (strcmp(argv[a], "--connect") == 0 || strcmp(argv[a], "--host") == 0 || strcmp(argv[a], "--dedicated") == 0 || strcmp(argv[a], "--host-observer") == 0) {
                conflict = argv[a];
            }
        }
        if (conflict != NULL || g_pc_net_role != 0) {
            fprintf(stderr, "[PC] --server: REFUSED: --server cannot be combined with %s (a saved server IS the --connect target of a client)\n%s", conflict != NULL ? conflict : "--connect / --host", k_server_usage);
            return 2;
        }
        if (pc_servers_load(NULL, sl, PC_SERVER_MAX, &sn, serr, sizeof(serr)) != PC_SERVERS_OK) {
            fprintf(stderr, "[PC] --server: REFUSED: %s\n%s", serr, k_server_usage);
            return 2;
        }
        idx = pc_servers_find(sl, sn, g_pc_server_name);
        if (idx < 0) {
            fprintf(stderr, "[PC] --server: REFUSED: no saved server named '%s' (--servers lists them, --server-add NAME HOST[:PORT] saves one)\n%s", g_pc_server_name, k_server_usage);
            return 2;
        }
        g_pc_net_role = 2;
        snprintf(g_pc_net_host_ip, sizeof(g_pc_net_host_ip), "%s", sl[idx].address);
        g_pc_net_port = (uint16_t)sl[idx].port;
        pc_session_apply_server(&sl[idx]);
        g_pc_guest = 1;
        if (g_pc_character_spec != NULL) {
            (void)pc_servers_set_last(NULL, sl[idx].name, g_pc_character_spec, NULL); /* UI hint only */
        }
        printf("[PC] --server: '%s' = %s:%d\n", sl[idx].name, sl[idx].address, sl[idx].port);
    }

    /* M2: the one-shot character store commands (no network, no window): list / import, then exit 0 (2 on a refusal). */
    if (g_pc_characters_list || g_pc_character_import != NULL) {
        char cerr[600];
        if (g_pc_characters_list) {
            static PCCharacter lst[PC_CHARACTER_MAX];
            int skipped = 0, k;
            const int cn = pc_character_list(NULL, lst, PC_CHARACTER_MAX, &skipped);
            printf("[PC] characters (%s/characters + legacy guest profiles): %d%s\n", PC_GUEST_PROFILE_DIR, cn, skipped > 0 ? " (some unreadable files were skipped, see stderr)" : "");
            for (k = 0; k < cn; k++) {
                printf("  %s %s name='%s' home_town='%s' gender=%d face=%d player_id=0x%04X land_id=0x%04X%s%s\n",
                       lst[k].storage == PC_CHARACTER_STORAGE_STORE ? "store " : "legacy", lst[k].storage == PC_CHARACTER_STORAGE_STORE ? lst[k].uuid : "(profile file)",
                       lst[k].name, lst[k].home_town, lst[k].gender, lst[k].face, (unsigned)lst[k].player_id, (unsigned)lst[k].land_id,
                       lst[k].has_legacy ? " legacy_profile=" : "", lst[k].has_legacy ? (lst[k].legacy_profile[0] != '\0' ? lst[k].legacy_profile : "(default guest.ini)") : "");
            }
        }
        if (g_pc_character_import != NULL) {
            PCCharacter imp;
            int ntok = 0;
            if (!pc_character_import_legacy(NULL, g_pc_character_import, &imp, &ntok, cerr, sizeof(cerr))) {
                fprintf(stderr, "[PC] --character-import-profile: REFUSED: %s\n", cerr);
                return 2;
            }
            printf("[PC] --character-import-profile: imported legacy profile '%s' as character %s (name '%s', %d token entr%s copied; the legacy files were NOT modified)\n",
                   imp.legacy_profile[0] != '\0' ? imp.legacy_profile : "(default guest.ini)", imp.uuid, imp.name, ntok, ntok == 1 ? "y" : "ies");
        }
        fflush(stdout);
        return 0;
    }
    if (g_pc_character_spec != NULL && pc_guest_profile_selected() != NULL) {
        fprintf(stderr, "[PC] --character: REFUSED: --character cannot be combined with --guest-profile (pick one)\n"
                        "usage: AnimalCrossing --connect HOST[:PORT] --character NAME|UUIDPREFIX   (see --help)\n");
        return 2;
    }

    if (g_pc_guest_creation_test != NULL && pc_guest_profile_selected() == NULL && g_pc_character_spec == NULL) {
        fprintf(stderr, "[PC] --guest-creation-test: REFUSED: the TEST-ONLY hook needs --guest-profile NAME\n");
        return 2;
    }

    /* Guests G2.3: --guest = CLIENT only (needs --connect), exclusive with --host / --dedicated / --host-observer / --bootstrap-resident / --bootstrap-guest.
     * Refused (exit 2 + usage) otherwise, BEFORE anything is initialised or the profile file is touched. Then the profile is loaded / created and validated
     * here (a bad guest.ini exits 2 with the bad key named; it is never overwritten) and converted into the --bootstrap-guest spec: the SAME arrival path. */
    if (g_pc_guest) {
        static const char k_guest_usage[] = "usage: AnimalCrossing --connect HOST[:PORT] --guest [--guest-profile NAME]   (see --help)\n";
        const char* conflict = NULL;
        int a;
        for (a = 1; a < argc; a++) {
            if (strcmp(argv[a], "--host") == 0) {
                conflict = "--host";
            } else if (strcmp(argv[a], "--dedicated") == 0) {
                conflict = "--dedicated";
            } else if (strcmp(argv[a], "--host-observer") == 0) {
                conflict = "--host-observer";
            } else if (strcmp(argv[a], "--bootstrap-resident") == 0) {
                conflict = "--bootstrap-resident";
            } else if (strcmp(argv[a], "--bootstrap-guest") == 0) {
                conflict = "--bootstrap-guest";
            }
            if (conflict != NULL) {
                break;
            }
        }
        if (conflict != NULL) {
            fprintf(stderr, "[PC] --guest: REFUSED: --guest cannot be combined with %s (a guest is a CLIENT of someone else's town)\n%s", conflict, k_guest_usage);
            return 2;
        }
        if (g_pc_net_role != 2) {
            fprintf(stderr, "[PC] --guest: REFUSED: --guest is a CLIENT-only option (use it together with --connect HOST[:PORT])\n%s", k_guest_usage);
            return 2;
        }
        const int store_rc = pc_main_prepare_store_character();
        if (store_rc < 0) {
            return 2;
        }
        if (store_rc == 0)
        {
            PCGuestProfile gp;
            char gerr[512];
            int gres;
            int creating = 0;
            if (pc_guest_profile_selected() != NULL) {
                /* a NAMED profile is never auto-created any more: an existing file is loaded (and validated), a missing one is created by the Rover scene
                 * (first-run creation): ids + a placeholder name are drawn in memory here, NOTHING is written until the Rover scene finished */
                gres = pc_guest_profile_read_selected(&gp, gerr, sizeof(gerr));
                if (gres == PC_GUEST_PROFILE_ABSENT) {
                    if (!pc_guest_profile_prepare_new_selected(&gp, gerr, sizeof(gerr))) {
                        gres = PC_GUEST_PROFILE_ERR;
                    } else {
                        creating = 1;
                    }
                }
            } else {
                gres = pc_guest_profile_load_or_create_selected(&gp, gerr, sizeof(gerr));
            }
            if (gres == PC_GUEST_PROFILE_ERR || !pc_guest_profile_spec(&gp, g_pc_guest_spec, sizeof(g_pc_guest_spec))) {
                fprintf(stderr, "[PC] --guest: REFUSED: bad guest profile: %s\n", gres == PC_GUEST_PROFILE_ERR ? gerr : "internal error (spec buffer)");
                return 2;
            }
            if (creating) {
                extern void pc_guest_creation_arm(const PCGuestProfile* p); /* pc_m_card.c */
                pc_guest_creation_arm(&gp);
                printf("[PC] --guest: guest profile %s does not exist yet: FIRST-RUN CREATION -- the real Rover scene (name, gender, face) will create it; "
                       "nothing is written before it finishes (placeholder name '%s', home town '%s', player id 0x%04X, land id 0x%04X)\n",
                       pc_guest_profile_selected_path(), gp.name, gp.home_town, (unsigned)gp.player_id, (unsigned)gp.land_id);
                g_pc_bootstrap_guest = g_pc_guest_spec;
            } else {
            printf("[PC] --guest: %s guest profile %s: name '%s', home town '%s', gender %d, face %d, player id 0x%04X, land id 0x%04X "
                   "(the ids are permanent; edit name / look before the first join)\n",
                   gres == PC_GUEST_PROFILE_CREATED ? (pc_guest_profile_selected() != NULL ? "CREATED the new" : "CREATED the default") : "loaded",
                   pc_guest_profile_selected_path(), gp.name, gp.home_town, gp.gender, gp.face,
                   (unsigned)gp.player_id, (unsigned)gp.land_id);
            g_pc_bootstrap_guest = g_pc_guest_spec;
            }
        }
    }

    /* Guests G2: --bootstrap-guest is a CLIENT-only TEST hook and excludes --bootstrap-resident: refused (exit 2) otherwise. */
    if (g_pc_bootstrap_guest != NULL && (g_pc_net_role != 2 || g_pc_bootstrap_resident >= 0)) {
        fprintf(stderr, "[NET][GUEST][TEST-ONLY] REFUSED: --bootstrap-guest is a CLIENT-only test hook (use it together with --connect) and cannot be combined with --bootstrap-resident\n");
        return 2;
    }

    /* Guests G1.1: validate the guest spec NOW (before any window / network / save work): every bad part gets a stderr diagnostic and exit status 2. */
    if (g_pc_bootstrap_guest != NULL) {
        extern int pc_bootstrap_guest_validate(const char* spec); /* pc_m_card.c */
        if (!pc_bootstrap_guest_validate(g_pc_bootstrap_guest)) {
            fflush(stdout);
            return 2;
        }
    }

    /* M-A / M-B: --town-dir / --town-fetch are CLIENT-only (need --connect, mutually exclusive); --town-serve is HOST-only. Refused (exit 2 + usage) before anything is
     * initialised. A set town dir is applied here, long before boot_main loads the save (second_game_init). */
    if ((g_pc_town_dir != NULL || g_pc_town_fetch) && g_pc_net_role != 2) {
        fprintf(stderr, "[PC] --town-dir / --town-fetch: REFUSED: they are CLIENT-only options (use them together with --connect HOST[:PORT])\n"
                        "usage: AnimalCrossing --connect HOST[:PORT] --town-fetch   (see --help)\n");
        return 2;
    }
    if (g_pc_town_dir != NULL && g_pc_town_fetch) {
        fprintf(stderr, "[PC] --town-dir: REFUSED: --town-dir cannot be combined with --town-fetch (the fetch picks the town directory)\n"
                        "usage: AnimalCrossing --connect HOST[:PORT] --town-fetch   (see --help)\n");
        return 2;
    }
    if (g_pc_personal_sync_override >= 0 && g_pc_net_role != 1) {
        fprintf(stderr, "[PC] --personal-sync: REFUSED: it is a HOST-only option (use it together with --host)\n"
                        "usage: AnimalCrossing --host [port] --personal-sync on|off   (see --help)\n");
        return 2;
    }
    /* Dedicated multiplayer host defaults: furniture sync ON (unless --no-house-sync) and town serving AUTO = ON/sanitized (unless --town-serve off or settings.ini town_serve is explicit). */
    if (g_pc_dedicated && g_pc_net_role == 1) {
        if (!s_house_sync_explicit) {
            g_pc_house_sync = 1;
        }
    }
    if (g_pc_town_serve_override >= 0 && g_pc_net_role != 1) {
        fprintf(stderr, "[PC] --town-serve: REFUSED: it is a HOST-only option (use it together with --host)\n"
                        "usage: AnimalCrossing --host [port] --town-serve off|on|full   (see --help)\n");
        return 2;
    }
    if ((g_pc_allow_new_guests_override >= 0 || g_pc_resident_tokens_override >= 0) && g_pc_net_role != 1) {
        fprintf(stderr, "[PC] --allow-new-guests / --resident-tokens: REFUSED: they are HOST-only options (use them together with --host)\n"
                        "usage: AnimalCrossing --host [port] --allow-new-guests 0|1 --resident-tokens off|tofu|required   (see --help)\n");
        return 2;
    }
    if (g_pc_town_dir != NULL && !pc_card_set_town_dir(g_pc_town_dir)) {
        fprintf(stderr, "[PC] --town-dir: REFUSED: bad directory '%s'\n", g_pc_town_dir);
        return 2;
    }

    /* --dedicated: HOST-only; incompatible with --connect, --bootstrap-resident, --bootstrap-guest and the SAME host-self test hooks --host-observer refuses.
     * Refused (exit 2, usage text on stderr) before anything is initialised. It then sets the SAME observer flag --host-observer sets: the observer init
     * (pc_host_observer_poll) is reused as is, there is no second init path. */
    if (g_pc_dedicated) {
        static const char* const k_dedicated_refused_hooks[] = {
            "--force-friendship-delta", "--force-mail-send", "--force-money-rock-hit", "--force-fish-catch", "--force-bug-catch",
            "--diag-bug-despawn-label-race", "--scene-test-enter-shop", "--scene-test-leave-after", "--collide-test-overlap",
            "--collide-test-approach",
        };
        size_t k;
        int a;
        if (g_pc_net_role != 1) {
            fprintf(stderr, "[DEDICATED] REFUSED: --dedicated is a HOST-only option (use it together with --host)\n"
                            "usage: AnimalCrossing --host [port] --dedicated   (see --help)\n");
            return 2;
        }
        if (g_pc_bootstrap_resident >= 0) {
            fprintf(stderr, "[DEDICATED] REFUSED: --dedicated cannot be combined with --bootstrap-resident (the dedicated server plays no resident)\n"
                            "usage: AnimalCrossing --host [port] --dedicated   (see --help)\n");
            return 2;
        }
        if (g_pc_bootstrap_guest != NULL) {
            fprintf(stderr, "[DEDICATED] REFUSED: --dedicated cannot be combined with --bootstrap-guest\n"
                            "usage: AnimalCrossing --host [port] --dedicated   (see --help)\n");
            return 2;
        }
        for (a = 1; a < argc; a++) {
            if (strcmp(argv[a], "--connect") == 0) {
                fprintf(stderr, "[DEDICATED] REFUSED: --dedicated cannot be combined with --connect (a dedicated server only hosts)\n"
                                "usage: AnimalCrossing --host [port] --dedicated   (see --help)\n");
                return 2;
            }
            for (k = 0; k < sizeof(k_dedicated_refused_hooks) / sizeof(k_dedicated_refused_hooks[0]); k++) {
                if (strcmp(argv[a], k_dedicated_refused_hooks[k]) == 0) {
                    fprintf(stderr, "[DEDICATED] REFUSED: --dedicated cannot be combined with %s (a host-self test hook that needs a host player)\n"
                                    "usage: AnimalCrossing --host [port] --dedicated   (see --help)\n", argv[a]);
                    return 2;
                }
            }
        }
        g_pc_host_observer = 1; /* implies --host-observer: the existing observer init is reused */
        if (g_pc_frame_limit_override < 0) {
            g_pc_frame_limit_override = 60; /* a server runs at the 60 Hz tick whatever the GUI's settings.ini max_fps says (--framelimit N / --no-framelimit still win) */
        }
    }

    /* --host-observer: HOST-only, exclusive with --bootstrap-resident / --bootstrap-guest, and incompatible with the host-self TEST hooks that
     * need a host player (the observer has no avatar to act with): refused (exit 2, before stdout/stderr are redirected) otherwise. */
    if (g_pc_host_observer) {
        static const char* const k_observer_refused_hooks[] = {
            "--force-friendship-delta", "--force-mail-send", "--force-money-rock-hit", "--force-fish-catch", "--force-bug-catch",
            "--diag-bug-despawn-label-race", "--scene-test-enter-shop", "--scene-test-leave-after", "--collide-test-overlap",
            "--collide-test-approach",
        };
        size_t k;
        int a;
        if (g_pc_net_role != 1) {
            fprintf(stderr, "[NET][OBSERVER] REFUSED: --host-observer is a HOST-only option (use it together with --host)\n");
            return 2;
        }
        if (g_pc_bootstrap_resident >= 0) {
            fprintf(stderr, "[NET][OBSERVER] REFUSED: --host-observer cannot be combined with --bootstrap-resident (the observer plays no resident)\n");
            return 2;
        }
        if (g_pc_bootstrap_guest != NULL) {
            fprintf(stderr, "[NET][OBSERVER] REFUSED: --host-observer cannot be combined with --bootstrap-guest\n");
            return 2;
        }
        for (a = 1; a < argc; a++) {
            for (k = 0; k < sizeof(k_observer_refused_hooks) / sizeof(k_observer_refused_hooks[0]); k++) {
                if (strcmp(argv[a], k_observer_refused_hooks[k]) == 0) {
                    fprintf(stderr, "[NET][OBSERVER] REFUSED: --host-observer cannot be combined with %s (a host-self test hook that needs a host player)\n",
                            argv[a]);
                    return 2;
                }
            }
        }
    }

    /* pc_log: env PC_LOG, legacy env aliases, -logfile. A -debug-flag or PC_LOG request keeps stdout/stderr like --verbose does. */
    int log_want_console = 0;
    if (pc_log_finish_cli(&log_want_console) != 0) {
        return 2;
    }

    /* Redirect stdout/stderr to NUL unless verbose — unbuffered terminal writes
     * are extremely slow on Windows and tank FPS. */
    if (!g_pc_verbose && !g_pc_profile_enabled && !log_want_console && (!g_pc_dedicated || pc_dedicated_stdout_quiet())) { /* --dedicated: the console is always on (a debug flag for routing), except in its own interactive window where only the console stream shows it */
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
    if (g_pc_dedicated) {
        pc_dedicated_startup(g_pc_net_port); /* "[DEDICATED] ..." startup line + the stdin command reader (only enqueues; drained on the main thread) */
    }
    if (log_want_console) { /* new line only for an explicit -debug-flag or PC_LOG request: plain --verbose output stays byte-identical */
        PC_LOG(PCL_GENERAL, "log mask=0x%08X (verbose=%d console=%d)\n", (unsigned)g_pc_log_mask, g_pc_verbose, log_want_console);
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
    if (g_pc_dedicated && g_pc_net_role == 1) { /* printed AFTER the settings load so an explicit settings.ini town_serve is reflected */
        printf("[PC] dedicated host services: house sync %s (%s), town serve %s (%s)\n", g_pc_house_sync ? "ON" : "off",
               s_house_sync_explicit ? "explicit option" : "default for --dedicated; --no-house-sync disables",
               pc_settings_town_serve_effective() == 0 ? "off" : pc_settings_town_serve_effective() == 1 ? "ON (sanitized)" : "ON (FULL, unsanitized)",
               (g_pc_town_serve_override >= 0 || g_pc_settings.town_serve >= 0) ? "explicit setting" : "default for --dedicated; --town-serve off disables");
    }
    pc_keybindings_load();

    /* Stage 1: role selection only. A failure here (bad port, bad address, Winsock
     * unavailable) is logged inside pc_net_game_start_*() and just leaves g_pc_net_role's
     * request unfulfilled -- single-player continues exactly as if neither flag was passed. */
    if (g_pc_net_role == 1) {
        pc_net_game_start_host(g_pc_net_port);
    } else if (g_pc_net_role == 2 && !g_pc_town_fetch) { /* --town-fetch starts the client after the pre-boot fetch (below) */
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
    if (g_pc_net_role == 2 && g_pc_town_fetch) {
        /* M-B: the PRE-BOOT town fetch (a window exists for the title progress and the error box; nothing of the game is initialised yet). */
        char town_err[700];
        int town_rc;
        for (;;) {
            town_rc = pc_net_game_town_prefetch(g_pc_net_host_ip, g_pc_net_port, 30000u, pc_town_title_progress, g_pc_server_name, town_err, sizeof(town_err));
            if (town_rc == PC_TOWN_PREFETCH_ERROR) {
                fprintf(stderr, "[PC] --town-fetch: %s\n", town_err);
                if (getenv("AC_TOWN_NO_MSGBOX") == NULL) { /* test hook: the box cannot be dismissed headlessly */
                    if (g_pc_online_ui) {
                        /* M-C: no usable town at all: Retry or Quit */
                        if (pc_town_failure_box(town_err, 0) == 0) {
                            continue;
                        }
                    } else {
                        SDL_ShowSimpleMessageBox(SDL_MESSAGEBOX_ERROR, "Animal Crossing - town transfer", town_err, g_pc_window);
                    }
                }
                pc_platform_shutdown();
                return 3;
            }
            if ((town_rc == PC_TOWN_PREFETCH_CACHE || town_rc == PC_TOWN_PREFETCH_LEGACY) && g_pc_online_ui && getenv("AC_TOWN_NO_MSGBOX") == NULL) {
                /* M-C: the fetch failed but an older copy of the town exists: ask instead of silently playing it */
                char msg[800];
                int pick;
                snprintf(msg, sizeof(msg), "Could not get the host's current town (%s).\n\nRetry, or continue with the %s you already have?\n\"Use saved copy\" is not offline play: the game still tries to connect in the background.", town_err,
                         town_rc == PC_TOWN_PREFETCH_CACHE ? "saved copy of this server's town" : "town in save/card_a");
                pick = pc_town_failure_box(msg, 1);
                if (pick == 0) {
                    pc_card_reset_town_dir_for_test(); /* un-select the fallback cache so the retry can pick the (possibly different) fetched town */
                    continue;
                }
                if (pick == 2) {
                    pc_platform_shutdown();
                    return 0;
                }
            }
            break;
        }
        pc_main_resolve_membership(); /* M-C: resident / guest / new, from characters/<uuid>/towns/<townkey>/membership.ini */
        pc_net_game_start_client(g_pc_net_host_ip, g_pc_net_port);
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
