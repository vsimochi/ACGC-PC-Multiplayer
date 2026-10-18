/* pc_room_ui.h - ROOMMATES / MOVE-IN: the move-in dialogue inside the vanilla gyroid conversation (pc_room_ui.c; its rows: pc_room_dlg.h) and the client seam
 * of message 104 (pc_net_game.c, block "ROOMMATES"). docs/multiplayer-guest-roadmap.md "Roommates / move-in". The pure rules / wire layout are pc_roommates.h. */
#ifndef PC_ROOM_UI_H
#define PC_ROOM_UI_H

#include <stdint.h>

#include "pc_roommates.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- the gyroid conversation (pc_room_ui.c; called by ac_haniwa_move.c_inc / ac_haniwa.c, TARGET_PC) ---- */
int  pc_room_gyroid_intercept(void* actor, int house);   /* aHNW_dance: 1 = a GUEST's / the OWNER's A here waits for the host's INFO (no talk yet); 0 = talk (vanilla) */
int  pc_room_gyroid_menu_pick(void* actor, int chosen);  /* talk_with_master / talk_with_guest: -1 = the inserted move-in choice, else the vanilla choice index */
int  pc_room_gyroid_dialog(void* actor, int house);      /* the actor's PC_ROOM action, every frame: 0 = stay, 1 = the talk ends, 2 = back to the holder's menu */
void pc_room_gyroid_frame(void* actor, int house, int act); /* every frame of every gyroid: disarms when its armed talk ended */
void pc_room_gyroid_gone(void* actor);                   /* ac_haniwa.c dt */
void pc_room_ui_test_press(void);                        /* TEST-ONLY: the next talk-position frame at a gyroid counts as an A press */
unsigned pc_room_ui_test_pad(void);                      /* TEST-ONLY (AC_TEST_ROOM_UI, PADRead): 1 A, 2 D-pad down, 4 D-pad up; 0 otherwise (always 0 in normal play) */
int  pc_room_music_box_house(void);                      /* m_common_data.h GetRoomMusicBox (TARGET_PC): a roommate's shared house */
int  pc_room_ui_is_open(void);                           /* 1 = a gyroid move-in conversation is under way (asking the host / armed) */
void pc_room_ui_notice_set(const char* text);            /* queues a one-line notice (each ~5 s, in order: none overwrites another) */
const char* pc_room_ui_notice_get(void);                 /* pc_net_notice_draw: the line to draw now, NULL = none */

/* ---- the network seam (pc_net_game.c) ---- */
int      pc_net_game_room_on(void);                       /* 1 = this process may use the gyroid menu (a READY client of a host with roommates, or a listen host) */
uint32_t pc_net_game_room_send(int op, int house, const uint8_t* guest_key); /* PC_ROOM_OP_* (client ops) at the gyroid of `house`; the request serial, 0 = not sent */
int      pc_net_game_room_edit_room(int house);           /* m_field_make.c set_field_type (TARGET_PC): 1 = the local guest edits house h as a roommate */
int      pc_net_game_room_take_reply(uint32_t seq, PCRoomMsg* out); /* 1 = the INFO / RESULT answering `seq` (consumed) */
/* the house door (m_player_common.c_inc, TARGET_PC): 1 = the local guest is a roommate of house h (it walks in without knocking, like the holder), 0 = vanilla
 * (residents, other guests: they knock and visit as before, solo, an older host, no link) */
int      pc_net_game_room_house_entry(int house);

#ifdef __cplusplus
}
#endif
#endif
