#ifndef PC_TOOL_WHEEL_H
#define PC_TOOL_WHEEL_H

/* PC radial tool-selection menu (local UI only; nothing here is networked). Hold the wheel key / pad button, point with the mouse (keyboard opener) or the right stick (pad opener),
 * release to equip: the selection becomes a request that Player_actor_check_and_switch_tool (src/game/m_player.c) consumes through the existing takeout / putaway path. */

#ifdef __cplusplus
extern "C" {
#endif

struct game_s;

/* PADRead hook, once per poll. hold_kb / hold_pad: the wheel key / button is down. mx,my: mouse position in window pixels. rx,ry: raw SDL right stick (-32768..32767, y down). dz: raw stick
 * deadzone. While the wheel is open the buttons and the C-stick are zeroed (the main stick keeps moving the player). */
void pc_tool_wheel_input(int hold_kb, int hold_pad, int mx, int my, int rx, int ry, int dz, unsigned short* buttons, signed char* cstick_x, signed char* cstick_y);

/* 1 while the wheel is up (PADRead zeroes the analog triggers too then). */
int pc_tool_wheel_is_open(void);

/* Overlay, drawn next to the pause menu (graph.c). */
void pc_tool_wheel_draw(struct game_s* game);

/* Consumed by Player_actor_check_and_switch_tool (always taken, so a stale request never survives a state change): 0 none, 1 equip pocket `*slot`, 2 put the held tool away. */
int pc_tool_wheel_take_request(int* slot);

/* Test seam: choose the entry index for a direction (dx,dy screen coordinates, y down) among n_tools tools; 0 = empty hand (centre), 1..n_tools = ring slots clockwise from the top. */
int pc_tool_wheel_pick(float dx, float dy, float dead, int n_tools);

#ifdef __cplusplus
}
#endif

#endif
