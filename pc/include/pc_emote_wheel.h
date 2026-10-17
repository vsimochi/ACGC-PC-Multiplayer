#ifndef PC_EMOTE_WHEEL_H
#define PC_EMOTE_WHEEL_H

/* PC emote wheel (local UI; see pc_emote.h for the emote model and the network presentation). Hold the Emote key (keybindings.ini Emote,
 * default F; the mouse picks relative to the window centre) or the pad Emote button (Pad_Emote, default D-pad Up; the right stick picks,
 * latched), release on a slice to play it; release in the centre = nothing. Like the tool wheel (pc_tool_wheel.c), whose UI this mirrors:
 * while it is up the buttons and the C-stick are zeroed (the main stick keeps moving the player), the analog triggers too (PADRead). The
 * two wheels and the chat line exclude each other. The choice becomes a one-frame request consumed by the player
 * (src/game/m_player_pc_emote.c_inc, Player_actor_pc_emote_check), which applies the full gate again. */

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

struct game_s;

/* PADRead hook, once per poll (after the tool wheel). stick_x / stick_y: the main stick (written only by the TEST-ONLY script). */
void pc_emote_wheel_input(int hold_kb, int hold_pad, int mx, int my, int rx, int ry, int dz, unsigned short* buttons, signed char* stick_x,
                          signed char* stick_y, signed char* cstick_x, signed char* cstick_y);
int  pc_emote_wheel_is_open(void);
void pc_emote_wheel_draw(struct game_s* game); /* graph.c, next to the tool wheel */
int  pc_emote_wheel_take_request(void);        /* 0 none, else the emote id (always taken: a request never outlives its frame) */

int      pc_emote_enabled(void); /* settings.ini [Gameplay] emotes (default 1) and env AC_EMOTES (0 = off) */
uint32_t pc_emote_now_ms(void);  /* SDL_GetTicks */

/* defined in src/game/m_player.c (TARGET_PC): the full gate for the LOCAL player now (pc_emote.h PC_EMOTE_OK / PC_EMOTE_R_*). id 0 = may the
 * wheel open; 1..8 = may that emote start. */
int pc_emote_player_gate(int id);
/* 1 while the emote overlay plays (or a pending start waits for WAIT) */
int pc_emote_player_busy(void);

#ifdef __cplusplus
}
#endif

#endif
