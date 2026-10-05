/* pc_play_online_menu.h - M4: the title-screen "Play Online" menu (server selection -> character selection -> connect).
 *
 * Pages: SERVERS (saved servers from save/mp/servers.ini + "Add server" + "Back") -> ACTIONS for the chosen server (Connect / Edit / Delete / Back) ->
 * CHARACTERS (local store characters + legacy guest profiles + "New character" (asks for a name) + "Back") -> connect.
 * Connect does NOT start a client in this process: it RELAUNCHES the executable (pc_relaunch.c) with `--connect HOST:PORT --character UUID|NAME` (or
 * `--guest-profile NAME` / `--guest` for a legacy-only character) and then quits the title process. Add / Edit use a text-entry mode fed by SDL_TEXTINPUT /
 * SDL_KEYDOWN routed from pc_main (like the keybinding capture); a controller user can only use the existing entries (Enter = confirm / Esc = cancel on a keyboard).
 * Same driver contract as pc_settings_menu.h: the caller owns the pad debounce and calls nav / confirm / cancel. */
#ifndef PC_PLAY_ONLINE_MENU_H
#define PC_PLAY_ONLINE_MENU_H

#include "pc_platform.h"

#ifdef __cplusplus
extern "C" {
#endif

struct game_s;

/* 1 when the title may show the item: no network role is active (not --connect, not --host / --dedicated). */
int  pc_play_online_menu_available(void);

void pc_play_online_menu_enter(void);
int  pc_play_online_menu_active(void);

/* Each returns 1 if the menu stays open, 0 if the caller should close it. */
int  pc_play_online_menu_nav_up(void);
int  pc_play_online_menu_nav_down(void);
int  pc_play_online_menu_nav_left(void);
int  pc_play_online_menu_nav_right(void);
int  pc_play_online_menu_confirm(void);
int  pc_play_online_menu_cancel(void);

void pc_play_online_menu_tick(void);

/* Text entry (add / edit server, new character name). While _active pc_main routes SDL key / text / controller-button events to _handle_text_event; the pad
 * driver must stand down while _blocking (active + a few frames after, so the Enter that finished the entry is not read as a confirm). */
int  pc_play_online_menu_text_active(void);
int  pc_play_online_menu_text_blocking(void);
int  pc_play_online_menu_handle_text_event(const SDL_Event* e); /* 1 = consumed */

void pc_play_online_menu_draw(struct game_s* game, int with_dim_backdrop);

#ifdef __cplusplus
}
#endif
#endif
