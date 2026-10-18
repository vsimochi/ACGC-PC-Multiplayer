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
void pc_room_ui_open(int ut_x, int ut_z, int item);      /* A at a placed gyroid (pc_ofurn_draw.c): asks the host, then shows the menu */
int  pc_room_ui_is_open(void);
void pc_room_ui_input(unsigned short* buttons, signed char* stick_x, signed char* stick_y, signed char* cstick_x, signed char* cstick_y); /* PADRead (pc_pad.c) */
void pc_room_ui_draw(struct game_s* game);                /* graph.c, after the emote wheel */
void pc_room_ui_notice_set(const char* text);            /* a one-line notice (~6 s) */
const char* pc_room_ui_notice_get(void);                 /* pc_net_notice_draw: NULL = none */

/* ---- the network seam (pc_net_game.c) ---- */
int      pc_net_game_room_on(void);                       /* 1 = this process may use the gyroid menu (a READY client of a host with roommates, or a listen host) */
int      pc_net_game_room_is_gyroid(int item);            /* a placed-furniture tile value that is a gyroid (FTR_HNW_*) */
uint32_t pc_net_game_room_send(int op, int ut_x, int ut_z, int item, const uint8_t* guest_key); /* PC_ROOM_OP_* (client ops); the request serial, 0 = not sent */
int      pc_net_game_room_take_reply(uint32_t seq, PCRoomMsg* out); /* 1 = the INFO / RESULT answering `seq` (consumed) */
/* the house door (ac_my_house_move.c_inc / m_player_common.c_inc, TARGET_PC): 1 = the local guest is a roommate of house h (walk in without knocking), -1 = a guest on a
 * roommates host that does not live there (the door stays shut), 0 = vanilla (residents, solo, an older host, no link) */
int      pc_net_game_room_house_entry(int house);
void     pc_net_game_room_door_refused(void);             /* the notice of a shut door (rate limited) */

#ifdef __cplusplus
}
#endif
#endif
