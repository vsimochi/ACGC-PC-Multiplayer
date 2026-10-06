#ifndef PC_PLAYER_PREVIEW_H
#define PC_PLAYER_PREVIEW_H

/* pc_player_preview.h - the REAL Animal Crossing player model (the same cKF skeleton, face / cloth textures and WAIT1 animation as the in-game player and the remote puppets) drawn
 * inside a 2D menu pass, for the Play Online character list. UI only: no actor, no Private_c, no resident, no network player, no save access. Per visible row it keeps a small
 * static slot (skeleton instance + the aligned face / cloth buffers), filled only when the row's character changes (pc_player_preview_set), never per frame.
 *
 * Appearance inputs = exactly the in-game ones: gender, face, cloth item. The shirt of a character is not stored locally (a resident's shirt is town-owned), so a character shows
 * the deterministic starter shirt derived from its identity hash (pc_guest_starter_shirt), which is what the host gives a new guest. */
#ifdef __cplusplus
extern "C" {
#endif

struct game_s;

#define PC_PLAYER_PREVIEW_SLOTS 8
#define PC_PLAYER_PREVIEW_MODEL_H 5200.0f /* tunable: the standing player model's height in model units (menu scale = pixels * 16 / this) */

struct PCCharacter;
/* Prepares slot `slot` for a stored character: its gender / face and the deterministic starter shirt of its identity. 1 = drawable. */
int  pc_player_preview_set_character(int slot, const struct PCCharacter* c);

/* Prepares slot `slot` for gender (0 male / 1 female) / face (0..7) / cloth item (an ITM_CLOTH* catalog item). Cheap when unchanged. Returns 1 when the slot can be drawn. */
int  pc_player_preview_set(int slot, int gender, int face, int cloth_item);
/* Draws slot `slot` standing with its feet (the model origin) at screen pixel (cx, cy) (320x240 menu space), `height_px` tall. Call from the menu draw (after the backdrop). No-op when the slot is not ready. */
void pc_player_preview_draw(struct game_s* game, int slot, float cx, float cy, float height_px);
/* Per-frame animation step of every prepared slot (idle breathing). */
void pc_player_preview_tick(void);
/* Forgets every slot (menu closed / the town scene replaced the title). */
void pc_player_preview_reset(void);

#ifdef __cplusplus
}
#endif
#endif
