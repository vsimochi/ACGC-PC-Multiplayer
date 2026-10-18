/* pc_room_ui.h - ROOMMATES / MOVE-IN: the gyroid menu (a small PC overlay) and the client seam of message 104 (pc_net_game.c, block "ROOMMATES").
 * docs/multiplayer-guest-roadmap.md "Roommates / move-in". The pure rules / wire layout are pc_roommates.h. */
#ifndef PC_ROOM_UI_H
#define PC_ROOM_UI_H

#include <stdint.h>

#include "pc_roommates.h"

#ifdef __cplusplus
extern "C" {
#endif

struct game_s;

/* ---- the gyroid menu (pc_room_ui.c) ---- */
void pc_room_ui_open(int house, void* gyroid_actor);     /* A at the gyroid of house h (pc_room_gyroid_intercept): asks the host, then shows the menu */
int  pc_room_gyroid_intercept(void* actor, int house);   /* ac_haniwa_move.c_inc aHNW_dance: 1 = the menu took this A (the vanilla talk request is skipped) */
void pc_room_gyroid_gone(void* actor);                   /* ac_haniwa.c dt */
void pc_room_ui_test_press(void);                        /* TEST-ONLY: the next talk-position frame at a gyroid counts as an A press */
int  pc_room_music_box_house(void);                      /* m_common_data.h GetRoomMusicBox (TARGET_PC): a roommate's shared house */
int  pc_room_ui_is_open(void);
void pc_room_ui_input(unsigned short* buttons, signed char* stick_x, signed char* stick_y, signed char* cstick_x, signed char* cstick_y); /* PADRead (pc_pad.c) */
void pc_room_ui_draw(struct game_s* game);                /* graph.c, after the emote wheel */
void pc_room_ui_notice_set(const char* text);            /* a one-line notice (~6 s) */
const char* pc_room_ui_notice_get(void);                 /* pc_net_notice_draw: NULL = none */

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
