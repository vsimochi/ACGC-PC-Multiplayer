/* pc_dresser_layout.h - U3 dresser screen geometry (docs/dresser-storage-redesign-plan.md section 6e/6f). Pure C (no game headers): operates on the raw
 * 16-byte Vtx layout (s16 ob[3], u16 flag, s16 tc[2], u8 cn[4]) of the gyroid window table hni_win_v[128] (src/data/model/hni_win.c, loaded at runtime).
 *
 * The gyroid window is a 24-unit orange border ring around an inner fill of x -102..66, y 38..86 (168 x 48): too small for 27 cells. In dresser mode the
 * window is ENLARGED by a 9-slice remap of its vertices (the border pieces keep their size and move outward, the fill stretches):
 *   x <= -102 -> x - 18, x >= 66 -> x + 18, between: linear onto -120..84;  y <= 38 -> y - 12, y >= 86 -> y + 8, between: linear onto 26..94.
 * => window x -144..108, y 2..118, inner 204 x 68. The FILL quads (w13 background v0..3, winT ue4 fill v31..40) keep their texel density: their texture
 * coordinates follow the original affine mapping (s = s0 + ds/dx * (x' - x0), t likewise), the tiled textures repeat (GX_MIRROR) instead of stretching.
 * The border pieces keep their texture coordinates (an edge piece is stretched along its length, a corner is only moved).
 *
 * begin() remaps from a byte-exact backup of the WHOLE table (never from an already remapped table); a second begin() is a no-op; end() restores the
 * backup byte-exact; end() without begin() is a no-op. A table that does not look like the measured gyroid window is left untouched (begin() = -1). */
#ifndef PC_DRESSER_LAYOUT_H
#define PC_DRESSER_LAYOUT_H

#ifdef __cplusplus
extern "C" {
#endif

#define PC_DLAYOUT_NVTX       128
#define PC_DLAYOUT_VTX_SIZE   16
#define PC_DLAYOUT_PITCH      22
#define PC_DLAYOUT_COLS       9
#define PC_DLAYOUT_ROWS       3
#define PC_DLAYOUT_CENTER_X   (-18)
/* the inner (fill) rectangle after the remap */
#define PC_DLAYOUT_IN_X0      (-120)
#define PC_DLAYOUT_IN_X1      84
#define PC_DLAYOUT_IN_Y0      26
#define PC_DLAYOUT_IN_Y1      94

int  pc_dresser_layout_rx(int x);
int  pc_dresser_layout_ry(int y);
int  pc_dresser_layout_row_y(int row);   /* 82 / 60 / 38 */
int  pc_dresser_layout_col_x(int col);   /* -18 + 22 (col - 4) */
/* 1 = remapped now, 0 = already active (no-op), -1 = not the expected table (untouched). */
int  pc_dresser_layout_begin(void* vtx, int nvtx);
/* 1 = restored byte-exact, 0 = was not active (no-op). */
int  pc_dresser_layout_end(void);
int  pc_dresser_layout_active(void);
/* The 4 vertices (raw Vtx, same order as the w13 background quad: top-left, bottom-left, top-right, bottom-right) of the extra background quad that covers
 * the hole the skipped 4-cell strip leaves in the fill, with the background's texture mapping. Valid only while active. 1 = filled. */
int  pc_dresser_layout_hole_quad(void* out4);

#ifdef __cplusplus
}
#endif
#endif
