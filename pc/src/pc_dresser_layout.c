/* pc_dresser_layout.c - see pc_dresser_layout.h. */
#include "pc_dresser_layout.h"

#include <stdint.h>
#include <string.h>

#define IN_X0 (-102)
#define IN_X1 66
#define IN_Y0 38
#define IN_Y1 86

static uint8_t s_backup[PC_DLAYOUT_NVTX * PC_DLAYOUT_VTX_SIZE];
static void* s_vtx = NULL; /* the table that is remapped now (NULL = not active) */

static int16_t vget(const uint8_t* v, int i, int off) {
    int16_t r;
    memcpy(&r, v + (size_t)i * PC_DLAYOUT_VTX_SIZE + off, 2);
    return r;
}

static void vset(uint8_t* v, int i, int off, int val) {
    const int16_t r = (int16_t)val;
    memcpy(v + (size_t)i * PC_DLAYOUT_VTX_SIZE + off, &r, 2);
}

enum { OFF_X = 0, OFF_Y = 2, OFF_S = 8, OFF_T = 10 };

/* rounded a * b / c (c > 0) */
static int muldiv(int a, int b, int c) {
    const long p = (long)a * b;
    return (int)(p >= 0 ? (p + c / 2) / c : -((-p + c / 2) / c));
}

int pc_dresser_layout_rx(int x) {
    if (x <= IN_X0) {
        return x + (PC_DLAYOUT_IN_X0 - IN_X0);
    }
    if (x >= IN_X1) {
        return x + (PC_DLAYOUT_IN_X1 - IN_X1);
    }
    return PC_DLAYOUT_IN_X0 + muldiv(x - IN_X0, PC_DLAYOUT_IN_X1 - PC_DLAYOUT_IN_X0, IN_X1 - IN_X0);
}

int pc_dresser_layout_ry(int y) {
    if (y <= IN_Y0) {
        return y + (PC_DLAYOUT_IN_Y0 - IN_Y0);
    }
    if (y >= IN_Y1) {
        return y + (PC_DLAYOUT_IN_Y1 - IN_Y1);
    }
    return PC_DLAYOUT_IN_Y0 + muldiv(y - IN_Y0, PC_DLAYOUT_IN_Y1 - PC_DLAYOUT_IN_Y0, IN_Y1 - IN_Y0);
}

int pc_dresser_layout_row_y(int row) {
    return (PC_DLAYOUT_IN_Y0 + PC_DLAYOUT_IN_Y1) / 2 + PC_DLAYOUT_PITCH * (1 - row); /* 82 / 60 / 38 */
}

int pc_dresser_layout_col_x(int col) {
    return PC_DLAYOUT_CENTER_X + PC_DLAYOUT_PITCH * (col - PC_DLAYOUT_COLS / 2);
}

/* the measured gyroid window: w13 background v0..3 = the inner rectangle, the ue4 fill corners v31 / v38, the left band v23 (refuses any other table) */
static int looks_like_gyroid_window(const uint8_t* v) {
    return vget(v, 0, OFF_X) == IN_X0 && vget(v, 0, OFF_Y) == IN_Y1 && vget(v, 1, OFF_Y) == IN_Y0 && vget(v, 2, OFF_X) == IN_X1 && vget(v, 3, OFF_Y) == IN_Y0 &&
           vget(v, 31, OFF_X) == -47 && vget(v, 31, OFF_Y) == 86 && vget(v, 38, OFF_X) == IN_X0 && vget(v, 23, OFF_X) == -126 && vget(v, 61, OFF_Y) == 110 &&
           vget(v, 2, OFF_X) != vget(v, 0, OFF_X) && vget(v, 1, OFF_Y) != vget(v, 0, OFF_Y);
}

static int is_fill(int i) {
    return (i >= 0 && i <= 3) || (i >= 31 && i <= 40); /* w13 background + winT's ue4 fill */
}

int pc_dresser_layout_begin(void* vtx, int nvtx) {
    uint8_t* v = (uint8_t*)vtx;
    const uint8_t* b = s_backup;
    int i, ds_num, ds_den, dt_num, dt_den;
    if (s_vtx != NULL) {
        return 0; /* already active: never remap a remapped table */
    }
    if (v == NULL || nvtx != PC_DLAYOUT_NVTX || !looks_like_gyroid_window(v)) {
        return -1;
    }
    memcpy(s_backup, v, sizeof(s_backup));
    /* the fill's affine texture mapping, from the pristine background quad (s along x: v0 -> v2, t along y: v0 -> v1) */
    ds_num = vget(b, 2, OFF_S) - vget(b, 0, OFF_S);
    ds_den = vget(b, 2, OFF_X) - vget(b, 0, OFF_X);
    dt_num = vget(b, 1, OFF_T) - vget(b, 0, OFF_T);
    dt_den = vget(b, 1, OFF_Y) - vget(b, 0, OFF_Y);
    for (i = 0; i < PC_DLAYOUT_NVTX; i++) {
        const int x = vget(b, i, OFF_X), y = vget(b, i, OFF_Y), x2 = pc_dresser_layout_rx(x), y2 = pc_dresser_layout_ry(y);
        vset(v, i, OFF_X, x2);
        vset(v, i, OFF_Y, y2);
        if (is_fill(i)) {
            const int ddx = x2 - x, ddy = y2 - y;
            vset(v, i, OFF_S, vget(b, i, OFF_S) + (ds_den > 0 ? muldiv(ddx, ds_num, ds_den) : -muldiv(ddx, ds_num, -ds_den)));
            vset(v, i, OFF_T, vget(b, i, OFF_T) + (dt_den > 0 ? muldiv(ddy, dt_num, dt_den) : -muldiv(ddy, dt_num, -dt_den)));
        }
    }
    s_vtx = vtx;
    return 1;
}

int pc_dresser_layout_end(void) {
    if (s_vtx == NULL) {
        return 0;
    }
    memcpy(s_vtx, s_backup, sizeof(s_backup));
    s_vtx = NULL;
    return 1;
}

int pc_dresser_layout_active(void) {
    return s_vtx != NULL;
}

int pc_dresser_layout_hole_quad(void* out4) {
    /* the skipped strip (x -47..49, y 34..58) leaves the band-to-cells gap x rx(-47)..rx(49), y ry(34)..IN_Y0' uncovered by any fill: one quad with the
     * background's texture mapping (evaluated from the remapped background quad v0..3) */
    static const int corner[4][2] = { { 0, 1 }, { 0, 0 }, { 1, 1 }, { 1, 0 } }; /* (right?, top?) in the w13 order TL, BL, TR, BR */
    const uint8_t* v = (const uint8_t*)s_vtx;
    uint8_t* o = (uint8_t*)out4;
    int k, x0, x1, y0, y1, bx0, bx1, by0, by1;
    if (v == NULL || o == NULL) {
        return 0;
    }
    x0 = pc_dresser_layout_rx(-47) - 1;
    x1 = pc_dresser_layout_rx(49) + 1;
    y0 = pc_dresser_layout_ry(34);
    y1 = PC_DLAYOUT_IN_Y0 + 2;
    bx0 = vget(v, 0, OFF_X);
    bx1 = vget(v, 2, OFF_X);
    by1 = vget(v, 0, OFF_Y);
    by0 = vget(v, 1, OFF_Y);
    for (k = 0; k < 4; k++) {
        const int x = corner[k][0] ? x1 : x0, y = corner[k][1] ? y1 : y0;
        memcpy(o + (size_t)k * PC_DLAYOUT_VTX_SIZE, v + (size_t)k * PC_DLAYOUT_VTX_SIZE, PC_DLAYOUT_VTX_SIZE);
        vset(o, k, OFF_X, x);
        vset(o, k, OFF_Y, y);
        vset(o, k, OFF_S, vget(v, 0, OFF_S) + muldiv(x - bx0, vget(v, 2, OFF_S) - vget(v, 0, OFF_S), bx1 - bx0));
        vset(o, k, OFF_T, vget(v, 0, OFF_T) + muldiv(by1 - y, vget(v, 1, OFF_T) - vget(v, 0, OFF_T), by1 - by0));
    }
    return 1;
}
