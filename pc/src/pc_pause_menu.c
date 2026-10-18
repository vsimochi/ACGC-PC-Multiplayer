#include "pc_platform.h"
#include "pc_pause_menu.h"
#include "pc_settings_menu.h"
#include "pc_menu_util.h"
#include "pc_text_draw.h"
#include "pc_net.h"
#include "pc_settings.h"
#include <SDL.h>
#include "pc_net_game.h" /* batch A (A3): pc_net_game_client_notice_visible() */
#include "pc_dresser_ui.h" /* 6q: the dresser screen's "Move" target hint */
#include "pc_shop_upgrade.h" /* multiplayer shop upgrade: the closure / reopening notice */
#include "pc_outdoor_furniture.h" /* outdoor furniture C1: the refusal notice of Place / Rotate / Pick up / leaf drop */
#include "pc_sell_ui.h" /* Nook bulk sell basket: the refusal notice of the basket screen */
#include "pc_room_ui.h" /* roommates: the move-in notice line */
#include "pc_version.h" /* the title screen's version corner (pc_version_corner_draw) */
#include "pc_dedicated.h" /* g_pc_dedicated */

#include "m_font.h"
#include "m_rcp.h"
#include "graph.h"
#include "main.h"       /* SCREEN_WIDTH_F */

#include <stdio.h>
#include <string.h>

int g_pc_paused = 0;

/* Pausing is blocked while either is set: main menu (set by ac_animal_logo)
 * or NES running (its own pause, and pausing it crashes the game). */
int g_pc_title_main_menu_visible = 0;
int g_pc_nes_active = 0;

/* Drains the keys still held from confirming Resume so they don't leak into play. */
int g_pc_pause_input_drain = 0;

/* Pause menu pages */
typedef enum {
    PAGE_MAIN = 0,
    PAGE_SETTINGS = 1,
    PAGE_CONFIRM_QUIT = 2,
} PauseMenuPage;

#define MAIN_ITEM_COUNT     3
#define CONFIRM_ITEM_COUNT  2

static PauseMenuPage cur_page = PAGE_MAIN;
static int main_sel = 0;    /* 0=Resume, 1=Settings, 2=Quit Game */
static int confirm_sel = 0; /* 0=No (default), 1=Yes */

void pc_pause_menu_toggle(void) {
    if (!g_pc_paused && (g_pc_title_main_menu_visible || g_pc_nes_active)) return;

    g_pc_paused = !g_pc_paused;
    if (g_pc_paused) {
        cur_page = PAGE_MAIN;
        main_sel = 0;
    } else {
        g_pc_pause_input_drain = 1;
    }
}

/* Input */

static void main_activate(void) {
    switch (main_sel) {
        case 0: /* Resume */
            pc_pause_menu_toggle();
            break;
        case 1: /* Settings */
            cur_page = PAGE_SETTINGS;
            pc_settings_menu_enter();
            break;
        case 2: /* Quit Game -> confirm page (default to No) */
            cur_page = PAGE_CONFIRM_QUIT;
            confirm_sel = 0;
            break;
    }
}

static void confirm_activate(void) {
    if (confirm_sel == 1) {
        printf("[PAUSE] Quit confirmed\n");
        g_pc_running = 0;
    } else {
        cur_page = PAGE_MAIN;
        main_sel = 0;
    }
}

/* Device-independent menu actions; keyboard and gamepad both map here. */
typedef enum {
    ACT_NONE = 0,
    ACT_UP,
    ACT_DOWN,
    ACT_LEFT,
    ACT_RIGHT,
    ACT_CONFIRM,
    ACT_CANCEL,
} MenuAction;

static void handle_action(MenuAction act) {
    /* Settings page is driven by pc_settings_menu. */
    if (cur_page == PAGE_SETTINGS) {
        switch (act) {
            case ACT_UP:    pc_settings_menu_nav_up();    break;
            case ACT_DOWN:  pc_settings_menu_nav_down();  break;
            case ACT_LEFT:  pc_settings_menu_nav_left();  break;
            case ACT_RIGHT: pc_settings_menu_nav_right(); break;
            case ACT_CONFIRM:
                if (!pc_settings_menu_confirm()) cur_page = PAGE_MAIN;
                break;
            case ACT_CANCEL:
                if (!pc_settings_menu_cancel()) cur_page = PAGE_MAIN;
                break;
            default: break;
        }
        return;
    }

    /* Pages owned here (Main, Quit-confirm). */
    {
        int item_count = (cur_page == PAGE_MAIN) ? MAIN_ITEM_COUNT : CONFIRM_ITEM_COUNT;
        int* sel       = (cur_page == PAGE_MAIN) ? &main_sel : &confirm_sel;

        switch (act) {
            case ACT_UP:
            case ACT_LEFT:
                *sel = (*sel + item_count - 1) % item_count;
                break;
            case ACT_DOWN:
            case ACT_RIGHT:
                *sel = (*sel + 1) % item_count;
                break;
            case ACT_CONFIRM:
                if (cur_page == PAGE_MAIN)              main_activate();
                else if (cur_page == PAGE_CONFIRM_QUIT) confirm_activate();
                break;
            case ACT_CANCEL:
                if (cur_page == PAGE_CONFIRM_QUIT) cur_page = PAGE_MAIN;
                else                               pc_pause_menu_toggle();
                break;
            default: break;
        }
    }
}

static MenuAction translate_key(SDL_Keycode k) {
    switch (k) {
        case SDLK_UP:
        case SDLK_w:        return ACT_UP;
        case SDLK_DOWN:
        case SDLK_s:        return ACT_DOWN;
        case SDLK_LEFT:
        case SDLK_a:        return ACT_LEFT;
        case SDLK_RIGHT:
        case SDLK_d:        return ACT_RIGHT;
        case SDLK_RETURN:
        case SDLK_KP_ENTER:
        case SDLK_SPACE:    return ACT_CONFIRM;
        case SDLK_ESCAPE:   return ACT_CANCEL;
    }
    return ACT_NONE;
}

static MenuAction translate_pad_button(int button) {
    switch (button) {
        case SDL_CONTROLLER_BUTTON_DPAD_UP:    return ACT_UP;
        case SDL_CONTROLLER_BUTTON_DPAD_DOWN:  return ACT_DOWN;
        case SDL_CONTROLLER_BUTTON_DPAD_LEFT:  return ACT_LEFT;
        case SDL_CONTROLLER_BUTTON_DPAD_RIGHT: return ACT_RIGHT;
        case SDL_CONTROLLER_BUTTON_A:          return ACT_CONFIRM;
        case SDL_CONTROLLER_BUTTON_B:
        case SDL_CONTROLLER_BUTTON_BACK:
        case SDL_CONTROLLER_BUTTON_START:      return ACT_CANCEL;
    }
    return ACT_NONE;
}

/* Left-stick nav with a per-axis latch so one push = one step. */
#define STICK_NAV_PRESS   18000
#define STICK_NAV_RELEASE 12000

static MenuAction translate_pad_axis(int axis, int value) {
    static int latch_x = 0, latch_y = 0;
    int* latch;
    MenuAction neg, pos;

    if (axis == SDL_CONTROLLER_AXIS_LEFTX)      { latch = &latch_x; neg = ACT_LEFT; pos = ACT_RIGHT; }
    else if (axis == SDL_CONTROLLER_AXIS_LEFTY) { latch = &latch_y; neg = ACT_UP;   pos = ACT_DOWN; }
    else return ACT_NONE;

    if (*latch != 0) {
        if (value > -STICK_NAV_RELEASE && value < STICK_NAV_RELEASE) *latch = 0;
        return ACT_NONE;
    }
    if (value <= -STICK_NAV_PRESS) { *latch = -1; return neg; }
    if (value >=  STICK_NAV_PRESS) { *latch =  1; return pos; }
    return ACT_NONE;
}

int pc_pause_menu_handle_event(const SDL_Event* e) {
    if (!g_pc_paused) return 0;

    switch (e->type) {
        case SDL_KEYDOWN:
            if (e->key.repeat) return 1;
            handle_action(translate_key(e->key.keysym.sym));
            return 1; /* swallow all keys while paused */
        case SDL_CONTROLLERBUTTONDOWN:
            handle_action(translate_pad_button(e->cbutton.button));
            return 1;
        case SDL_CONTROLLERAXISMOTION:
            handle_action(translate_pad_axis(e->caxis.axis, e->caxis.value));
            return 1;
    }
    return 0;
}

/* Drawing */

static void draw_main_page(struct game_s* game) {
    static const char* items[MAIN_ITEM_COUNT] = { "Resume", "Settings", "Quit Game" };

    pc_menu_draw_centered(game, "- Paused -", 80.0f, 255, 255, 255, 255, 1.0f);

    f32 y = 110.0f;
    f32 line_h = 18.0f;
    for (int i = 0; i < MAIN_ITEM_COUNT; i++) {
        int r, g, b, a;
        int selected = (i == main_sel);
        pc_menu_row_colors(selected, &r, &g, &b, &a);
        pc_menu_draw_centered(game, items[i], y + i * line_h, r, g, b, a,
                              selected ? PC_MENU_SCALE_SELECTED : 1.0f);
    }
}

static void draw_confirm_page(struct game_s* game) {
    pc_menu_draw_centered(game, "- Quit Game -", 80.0f, 255, 255, 255, 255, 1.0f);
    pc_menu_draw_centered(game, "Are you sure you want to quit?",
                          115.0f, 230, 230, 230, 255, 1.0f);
    pc_menu_draw_two_choice(game, "No", "Yes", confirm_sel, 150.0f);
}

void pc_pause_menu_draw(struct game_s* game) {
    if (!g_pc_paused || game == NULL || game->graph == NULL) return;

    /* Font ortho projection + identity modelview, else our screen-space quads
     * inherit whatever projection the scene ended its frame with. */
    mFont_SetMatrix(game->graph, mFont_MODE_FONT);
    pc_settings_menu_tick();

    if (cur_page == PAGE_SETTINGS) {
        /* Settings module owns its own dim backdrop. */
        pc_settings_menu_draw(game, /*with_dim_backdrop=*/1);
        /* The user may have closed it from inside (Back). */
        if (!pc_settings_menu_active()) cur_page = PAGE_MAIN;
    } else {
        pc_menu_dim_rect(game->graph, 180);
        if (cur_page == PAGE_MAIN)              draw_main_page(game);
        else if (cur_page == PAGE_CONFIRM_QUIT) draw_confirm_page(game);
    }

    mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
}

/* Guests G6.1: the join-failure message, word wrapped (the same rule as the title menu's guest message): header + up to 8 rows. */
static void pc_net_join_message_draw(struct game_s* game, const char* msg, int is_warning) {
    const int row_chars = 44;
    const int max_rows = 8;
    f32 y = 14.0f;
    int pos = 0;
    int len = (int)strlen(msg);
    int rows = 0;

    pc_menu_draw_centered(game, is_warning ? "Waiting for the host:" : "Cannot join the host:", y, 255, 90, 90, 255, 1.0f);
    y += 16.0f;
    while (pos < len && rows < max_rows) {
        char row[64];
        int take = len - pos;
        int k;

        if (take > row_chars) {
            take = row_chars;
            for (k = take; k > row_chars / 2; k--) { /* break at the last blank of the window */
                if (msg[pos + k] == ' ') {
                    take = k;
                    break;
                }
            }
        }
        memcpy(row, msg + pos, (size_t)take);
        row[take] = '\0';
        pc_menu_draw_centered(game, row, y, 255, 255, 255, 235, 1.0f);
        y += 16.0f;
        pos += take;
        while (pos < len && msg[pos] == ' ') {
            pos++;
        }
        rows++;
    }
}

/* Optional client ping counter (settings.ini show_ping / F4): a read-only view of the reliable layer's own DATA->ACK RTT, refreshed once a second; only while
 * connected as a client. Draws nothing on the title / single player and never touches heartbeat, timeout or reconnect state. */
static void pc_net_ping_draw(struct game_s* game) {
    static uint32_t shown_tick = 0;
    static char     line[24] = "";
    static int      last_ok = 0;
    static int      announced = 0;
    uint32_t rtt, age, now;
    if (!g_pc_settings.show_ping || game == NULL || game->graph == NULL || pc_net_is_host() || !pc_net_is_connected() || pc_net_game_client_link_state() != PC_NETGAME_LINK_READY) return;
    if (!announced) {
        announced = 1;
        printf("[NET][PING] counter ON (client)\n");
    }
    now = SDL_GetTicks();
    if (shown_tick == 0 || (uint32_t)(now - shown_tick) >= 1000u) {
        const int was_ok = last_ok;
        shown_tick = now ? now : 1u;
        last_ok = pc_net_client_rtt_ms(&rtt, &age) && age <= 30000u;
        if (last_ok && !was_ok) printf("[NET][PING] counter: first measurement %u ms (sample age %u ms)\n", (unsigned)rtt, (unsigned)age);
        if (last_ok) snprintf(line, sizeof(line), "Ping: %u ms", (unsigned)rtt);
        else snprintf(line, sizeof(line), "Ping: --");
    }
    mFont_SetMatrix(game->graph, mFont_MODE_FONT);
    pc_menu_draw_left(game, line, SCREEN_WIDTH_F - (f32)pc_text_width(line) * 0.75f - 8.0f, 4.0f, last_ok ? 200 : 170, last_ok ? 255 : 170, last_ok ? 200 : 170, 230, 0.75f);
    mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
}

/* The game version in the bottom-right corner of the title screen (pc_version.h; called by ac_animal_logo.c's title menu draw, inside its font phase, and
 * nowhere else: never in game). Small, translucent white over a dark 1.5-pixel outline so it reads on any title background. Placed against the RIGHT EDGE
 * OF THE WINDOW: on a window wider than 4:3 the aspect-corrected font space shows x < 0 / x > 320 (as pc_chat_ui.c's chat_visible_x; the text is not inside
 * PC_NOOP_WIDESCREEN_STRETCH tags. g_pc_widescreen_stretch is NOT read here: it is emu64's render-time state of the previous display list, not a setting);
 * the margin and the outline are given in WINDOW pixels (converted with the window height: 240 font rows = g_pc_window_h).
 * overlay_open: the Play Online / Options overlay is up; its hint line (y 218, centered) can reach the corner of a 4:3 window, so then the text is only
 * drawn when the window is wide enough for the corner to lie outside 0..320. */
void pc_version_corner_draw(struct game_s* game, int overlay_open) {
    static const char text[] = "v" PC_VERSION_STRING;
    const f32 scale = 0.6f;
    f32 right = SCREEN_WIDTH_F, px = 1.0f, w, x, y, o;
    if (g_pc_dedicated || game == NULL || game->graph == NULL) return;
    if (g_pc_window_w > 0 && g_pc_window_h > 0) {
        const f32 win = (f32)g_pc_window_w / (f32)g_pc_window_h;
        const f32 gc = 4.0f / 3.0f;
        px = SCREEN_HEIGHT_F / (f32)g_pc_window_h; /* one window pixel in font units */
        if (win > gc + 0.01f) {
            right = 160.0f + 160.0f * win / gc;
        }
    }
    w = (f32)pc_text_width(text) * scale;
    if (overlay_open && right - w - 8.0f * px < SCREEN_WIDTH_F) return;
    x = right - w - 8.0f * px;
    y = SCREEN_HEIGHT_F - (f32)mFont_TEX_CHAR_HEIGHT_F32 * scale - 6.0f * px;
    o = 1.5f * px;
    if (o < 0.25f) o = 0.25f;
    pc_text_draw(game, text, x - o, y, 0, 0, 0, 150, scale);
    pc_text_draw(game, text, x + o, y, 0, 0, 0, 150, scale);
    pc_text_draw(game, text, x, y - o, 0, 0, 0, 150, scale);
    pc_text_draw(game, text, x, y + o, 0, 0, 0, 150, scale);
    pc_text_draw(game, text, x, y, 255, 255, 255, 200, scale);
}

void pc_net_notice_draw(struct game_s* game) {
    int join_warning = 0;
    const char* join_msg;

    pc_net_ping_draw(game);

    if (g_pc_paused || game == NULL || game->graph == NULL) return;
    {
        /* House-sync offline edit lock: a refused house edit shows one short line for ~3 s (below the other notices, drawn in addition to them). */
        const int edit_notice = pc_net_game_room_edit_notice();
        if (edit_notice != 0) {
            mFont_SetMatrix(game->graph, mFont_MODE_FONT);
            pc_menu_draw_centered(game,
                                  edit_notice == 1   ? "Not connected - house changes are disabled"
                                  : edit_notice == 3 ? "Take the stored items out first" /* dresser storage: a dresser with extra items stays put */
                                  : edit_notice == 4 ? "The room is being saved..."      /* dresser storage: a just-moved dresser opens once its move is committed */
                                  : edit_notice == 5 ? "Close the dresser to save room changes," /* + the 2nd line below (one line is too wide) */
                                  : edit_notice == 6 ? "Try again in a moment"           /* dresser storage: a transient refusal */
                                  : edit_notice == 7 ? "Not connected - extra storage is unavailable"
                                  : edit_notice == 8 ? "Items in the last 3 slots can only be taken out" /* 6q (no longer raised since 6r) */
                                  : edit_notice == 9 ? "Old drawer items were moved to your pockets"      /* 6r: residual vanilla keep items */
                                  : edit_notice == 10 ? "Make room in your pockets for the old drawer items"
                                  : edit_notice == 11 ? "That item can't be stored in a dresser"         /* 6r: grab swap / put predicate */
                                  : edit_notice == 12 ? "Asking the host for your stored items..."       /* direct open: the host's view is on the way */
                                                     : "Leave the house to resync",
                                  62.0f, 255, 200, 90, 255, 1.0f);
            if (edit_notice == 5) { /* dresser storage 6l: an extra-slot transaction refused because the room holds uncommitted edits */
                pc_menu_draw_centered(game, "then reopen it to store more items.", 74.0f, 255, 200, 90, 255, 1.0f);
            }
            mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
        }
    }
    {
        /* Multiplayer shop upgrade: "closed for N minutes for an upgrade" / "upgraded and open again", ~8 s after the change (drawn in addition to the other notices). */
        char up_line[96];
        if (pc_shop_upgrade_notice(up_line, (int)sizeof(up_line))) {
            mFont_SetMatrix(game->graph, mFont_MODE_FONT);
            pc_menu_draw_centered(game, up_line, 46.0f, 255, 230, 140, 255, 1.0f);
            mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
        }
    }
    {
        /* Roommates: the other side's news (a request arrived, accepted, turned down, moved out), one queued line at a time, ~5 s each (pc_room_ui.c). At the TOP
         * of the screen: clear of the player (94 crossed the character), the vanilla message window and the clock (bottom); below the shop upgrade line when that one shows */
        const char* rm_line = pc_room_ui_notice_get();
        if (rm_line != NULL) {
            char up_probe[8];
            mFont_SetMatrix(game->graph, mFont_MODE_FONT);
            pc_menu_draw_centered(game, rm_line, pc_shop_upgrade_notice(up_probe, (int)sizeof(up_probe)) ? 58.0f : 46.0f, 200, 255, 200, 255, 1.0f);
            mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
        }
    }
    {
        /* Outdoor furniture C1: why a Place / Rotate / Pick up / furniture leaf drop was refused, ~4 s (drawn in addition to the other notices). */
        const char* of_line = pc_net_game_ofurn_notice();
        if (of_line != NULL) {
            mFont_SetMatrix(game->graph, mFont_MODE_FONT);
            pc_menu_draw_centered(game, of_line, 30.0f, 255, 200, 90, 255, 1.0f);
            mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
        }
    }
    {
        /* Nook bulk sell basket: why a pocket item cannot go into the basket ("Nook won't buy that"), ~3 s (drawn in addition to the other notices). At the BOTTOM of
         * the screen: the top lines would cross the basket window (real-run screenshot sell_basket_open.png). */
        const char* sell_line = pc_sell_ui_notice();
        const char* sell_hint = pc_sell_ui_hint(); /* the controls line while the basket screen is open; a notice takes its place for ~3 s */
        if (sell_line != NULL) {
            mFont_SetMatrix(game->graph, mFont_MODE_FONT);
            pc_menu_draw_centered(game, sell_line, 216.0f, 255, 200, 90, 255, 1.0f);
            mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
        } else if (sell_hint != NULL) {
            mFont_SetMatrix(game->graph, mFont_MODE_FONT);
            pc_menu_draw_centered(game, sell_hint, 218.0f, 255, 255, 255, 230, 0.75f);
            mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
        }
    }
    {
        /* Client auto-reconnect: its status wins over the join message / the generic "Not connected" notice. */
        int rc_attempt = 0, rc_next_s = 0;
        const int rc = pc_net_game_client_reconnect_status(&rc_attempt, &rc_next_s);
        if (rc != 0) {
            char line[96];
            mFont_SetMatrix(game->graph, mFont_MODE_FONT);
            if (rc == 1) {
                snprintf(line, sizeof(line), "Connection lost - Reconnecting... (attempt %d)", rc_attempt);
                pc_menu_draw_centered(game, line, 14.0f, 255, 200, 90, 255, 1.0f);
            } else if (rc == 2) {
                pc_menu_draw_centered(game, "Reconnected", 14.0f, 120, 255, 120, 255, 1.0f);
            } else {
                pc_menu_draw_centered(game, "Disconnected from host (refused) - restart to rejoin", 14.0f, 255, 90, 90, 255, 1.0f);
            }
            mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
            return;
        }
    }
    join_msg = pc_net_game_join_message(&join_warning);
    if (join_msg != NULL) { /* G6.1: works on the title screen and in game (graph_main draws this every frame) */
        mFont_SetMatrix(game->graph, mFont_MODE_FONT);
        pc_net_join_message_draw(game, join_msg, join_warning);
        mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
        return;
    }
    if (!pc_net_game_client_notice_visible()) return;
    mFont_SetMatrix(game->graph, mFont_MODE_FONT);
    pc_menu_draw_centered(game, "Not connected to the host", 14.0f, 255, 90, 90, 255, 1.0f);
    pc_menu_draw_centered(game, "Waiting for the connection...", 30.0f, 255, 255, 255, 230, 1.0f);
    mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
}
