/* dresser_layout_selftest.c - native test of pc/src/pc_dresser_layout.c (the U3 9-slice enlargement of the gyroid window).
 * Build + run (git-bash, PATH has /c/msys64/ucrt64/bin appended; <scratch> OUTSIDE pc/build64):
 *   gcc -std=gnu11 -Wall -Wextra -Ipc/include pc/tools/net_spike/dresser_layout_selftest.c pc/src/pc_dresser_layout.c -o <scratch>/dresser_layout_selftest.exe
 *   python pc/tools/net_spike/extract_hni_win_v.py "<disc image>" <scratch>/hni_win_v.bin      (the REAL table, from the user's disc; optional)
 *   <scratch>/dresser_layout_selftest.exe [<scratch>/hni_win_v.bin]
 * Without the extracted table the test runs on a SYNTHETIC table with the measured positions and the measured fill mapping (s 0..7168 over x -102..66,
 * t 0..2048 over y 86..38). */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pc_dresser_layout.h"

static int s_fail = 0;
#define CHECK(c)                                                                 \
    do {                                                                         \
        if (!(c)) {                                                              \
            printf("FAIL line %d: %s\n", __LINE__, #c);                          \
            s_fail++;                                                            \
        }                                                                        \
    } while (0)

typedef struct {
    int16_t ob[3];
    uint16_t flag;
    int16_t tc[2];
    uint8_t cn[4];
} V;

/* measured positions (the [NET][DRESSER][LAYOUT] runtime dump) */
static const int16_t k_pos[128][2] = {
    { -102, 86 }, { -102, 38 }, { 66, 86 }, { 66, 38 }, { 42, 110 }, { 42, 86 }, { 90, 110 }, { 90, 86 }, { -126, 14 }, { -78, 14 }, { -78, 38 }, { -126, 38 },
    { -6, 86 }, { 42, 86 }, { 42, 110 }, { -6, 110 }, { -78, 38 }, { -78, 34 }, { -47, 38 }, { -47, 34 }, { -78, 14 }, { -30, 34 }, { -30, 14 }, { -126, 86 },
    { -126, 38 }, { -102, 86 }, { -102, 38 }, { 66, 38 }, { 90, 38 }, { 90, 86 }, { 66, 86 }, { -47, 86 }, { -47, 58 }, { 49, 86 }, { 49, 58 }, { 49, 38 },
    { 66, 86 }, { 66, 38 }, { -102, 86 }, { -102, 38 }, { -47, 38 }, { -30, 34 }, { -30, 14 }, { -6, 34 }, { -6, 14 }, { -30, 110 }, { -30, 86 }, { -6, 110 },
    { -6, 86 }, { -6, 34 }, { -6, 14 }, { 42, 34 }, { 42, 14 }, { -78, 86 }, { -30, 86 }, { -30, 110 }, { -78, 110 }, { 49, 38 }, { 49, 34 }, { 90, 38 },
    { 90, 34 }, { -126, 110 }, { -126, 86 }, { -78, 110 }, { -78, 86 }, { 42, 14 }, { 90, 14 }, { 42, 34 }, { -47, 58 }, { -47, 34 }, { 49, 58 }, { 49, 34 },
    { -116, 87 }, { -116, 39 }, { -68, 87 }, { -68, 39 }, { -89, 97 }, { -89, 62 }, { 86, 97 }, { 86, 62 }, { -126, 110 }, { -126, 86 }, { -78, 110 }, { -78, 86 },
    { -78, 86 }, { -30, 86 }, { -30, 110 }, { -78, 110 }, { -30, 110 }, { -30, 86 }, { -6, 110 }, { -6, 86 }, { -6, 86 }, { 42, 86 }, { 42, 110 }, { -6, 110 },
    { 42, 110 }, { 42, 86 }, { 90, 110 }, { 90, 86 }, { 66, 38 }, { 90, 38 }, { 90, 86 }, { 66, 86 }, { 42, 14 }, { 90, 14 }, { 90, 38 }, { 42, 38 },
    { -6, 38 }, { -6, 14 }, { 42, 38 }, { 42, 14 }, { -30, 38 }, { -30, 14 }, { -6, 38 }, { -6, 14 }, { -78, 38 }, { -78, 14 }, { -30, 38 }, { -30, 14 },
    { -126, 14 }, { -78, 14 }, { -78, 38 }, { -126, 38 }, { -126, 86 }, { -126, 38 }, { -102, 86 }, { -102, 38 },
};

/* the ORANGE ring (winT ue1/2/3/5/6/7) + its w1..w12 shadow copies, as vertex quadruples (hni_win.c display lists) */
static const int k_ring[][4] = {
    { 4, 5, 6, 7 },     { 8, 9, 10, 11 },   { 12, 13, 14, 15 },   { 16, 17, 18, 19 },   { 17, 20, 21, 22 },   { 23, 24, 25, 26 },   { 27, 28, 29, 30 },
    { 41, 42, 43, 44 }, { 45, 46, 47, 48 }, { 49, 50, 51, 52 },   { 53, 54, 55, 56 },   { 57, 58, 59, 60 },   { 61, 62, 63, 64 },   { 60, 65, 66, 67 },
    { 80, 81, 82, 83 }, { 84, 85, 86, 87 }, { 88, 89, 90, 91 },   { 92, 93, 94, 95 },   { 96, 97, 98, 99 },   { 100, 101, 102, 103 }, { 104, 105, 106, 107 },
    { 108, 109, 110, 111 }, { 112, 113, 114, 115 }, { 116, 117, 118, 119 }, { 120, 121, 122, 123 }, { 124, 125, 126, 127 },
};

static void quad_rect(const V* v, const int q[4], int r[4]) {
    int k;
    r[0] = r[2] = 32767;
    r[1] = r[3] = -32768;
    for (k = 0; k < 4; k++) {
        const V* p = &v[q[k]];
        if (p->ob[0] < r[0]) r[0] = p->ob[0];
        if (p->ob[0] > r[1]) r[1] = p->ob[0];
        if (p->ob[1] < r[2]) r[2] = p->ob[1];
        if (p->ob[1] > r[3]) r[3] = p->ob[1];
    }
}

static int overlap(const int a[4], const int b[4]) {
    return a[0] < b[1] && b[0] < a[1] && a[2] < b[3] && b[2] < a[3];
}

static int fill_vertex(int i) {
    return (i >= 0 && i <= 3) || (i >= 31 && i <= 40);
}

int main(int argc, char** argv) {
    static V tab[128], orig[128], other[128];
    int i, r, c, k, real = 0;
    memset(tab, 0, sizeof(tab));
    if (argc > 1) {
        FILE* f = fopen(argv[1], "rb");
        if (f != NULL) {
            real = fread(tab, sizeof(tab), 1, f) == 1;
            fclose(f);
        }
    }
    if (!real) {
        for (i = 0; i < 128; i++) {
            tab[i].ob[0] = k_pos[i][0];
            tab[i].ob[1] = k_pos[i][1];
            tab[i].flag = 1;
            tab[i].tc[0] = (int16_t)(fill_vertex(i) ? (k_pos[i][0] + 102) * 7168 / 168 : (i * 37) % 2048);
            tab[i].tc[1] = (int16_t)(fill_vertex(i) ? (86 - k_pos[i][1]) * 2048 / 48 : (i * 53) % 2048);
            tab[i].cn[3] = 255;
        }
    }
    printf("table: %s\n", real ? "REAL (extracted from the disc)" : "synthetic (measured positions)");
    for (i = 0; i < 128; i++) {
        CHECK(tab[i].ob[0] == k_pos[i][0] && tab[i].ob[1] == k_pos[i][1]); /* the measured positions ARE the table */
    }
    memcpy(orig, tab, sizeof(tab));
    /* L1 end without begin = no-op */
    CHECK(pc_dresser_layout_end() == 0 && !pc_dresser_layout_active() && memcmp(tab, orig, sizeof(tab)) == 0);
    /* L2 a table that is not the gyroid window is refused untouched */
    memcpy(other, orig, sizeof(other));
    other[0].ob[0] = 5;
    {
        V keep[128];
        memcpy(keep, other, sizeof(keep));
        CHECK(pc_dresser_layout_begin(other, 128) == -1 && !pc_dresser_layout_active() && memcmp(other, keep, sizeof(keep)) == 0);
        CHECK(pc_dresser_layout_begin(tab, 64) == -1 && memcmp(tab, orig, sizeof(tab)) == 0);
    }
    /* L3 begin: 9-slice positions, window bounds, inner rectangle */
    CHECK(pc_dresser_layout_begin(tab, 128) == 1 && pc_dresser_layout_active());
    {
        int wx0 = 999, wx1 = -999, wy0 = 999, wy1 = -999;
        for (i = 0; i < 128; i++) {
            CHECK(tab[i].ob[0] == pc_dresser_layout_rx(orig[i].ob[0]) && tab[i].ob[1] == pc_dresser_layout_ry(orig[i].ob[1]));
            if (i >= 72 && i < 80) {
                continue; /* 3DmT / wakuT: not drawn in dresser mode */
            }
            if (i >= 68 && i < 72) {
                continue; /* win2T: not drawn in dresser mode */
            }
            if (tab[i].ob[0] < wx0) wx0 = tab[i].ob[0];
            if (tab[i].ob[0] > wx1) wx1 = tab[i].ob[0];
            if (tab[i].ob[1] < wy0) wy0 = tab[i].ob[1];
            if (tab[i].ob[1] > wy1) wy1 = tab[i].ob[1];
        }
        printf("window x %d..%d y %d..%d\n", wx0, wx1, wy0, wy1);
        CHECK(wx0 == -144 && wx1 == 108 && wy0 == 2 && wy1 == 118);
        CHECK(wy0 >= -10 + 12 && wx0 >= -160 && wx1 <= 160 && wy1 <= 120); /* above the pockets' top edge, inside 320 x 240 */
        CHECK(tab[0].ob[0] == PC_DLAYOUT_IN_X0 && tab[2].ob[0] == PC_DLAYOUT_IN_X1 && tab[1].ob[1] == PC_DLAYOUT_IN_Y0 && tab[0].ob[1] == PC_DLAYOUT_IN_Y1);
    }
    /* L4 texture coordinates: the fill keeps ONE affine mapping (same texel density, continuous across the shared vertices); the ring keeps its UVs */
    {
        const double bs = (double)(orig[2].tc[0] - orig[0].tc[0]) / (orig[2].ob[0] - orig[0].ob[0]);
        const double bt = (double)(orig[1].tc[1] - orig[0].tc[1]) / (orig[1].ob[1] - orig[0].ob[1]);
        for (i = 0; i < 128; i++) {
            if (fill_vertex(i)) {
                const double es = orig[0].tc[0] + bs * (tab[i].ob[0] - orig[0].ob[0]);
                const double et = orig[0].tc[1] + bt * (tab[i].ob[1] - orig[0].ob[1]);
                CHECK(abs(tab[i].tc[0] - (int)(es + (es >= 0 ? 0.5 : -0.5))) <= 1 && abs(tab[i].tc[1] - (int)(et + (et >= 0 ? 0.5 : -0.5))) <= 1);
            } else {
                CHECK(tab[i].tc[0] == orig[i].tc[0] && tab[i].tc[1] == orig[i].tc[1]);
            }
            CHECK(tab[i].flag == orig[i].flag && tab[i].ob[2] == orig[i].ob[2] && memcmp(tab[i].cn, orig[i].cn, 4) == 0);
        }
        printf("fill mapping: ds/dx %.3f dt/dy %.3f (texel density kept; the tiled textures repeat)\n", bs, bt);
    }
    /* L5 the 9 x 3 cells: inside the inner rectangle, no overlap with the remapped ring, uniform 22 pitch */
    {
        int in = 1, hit = 0;
        for (r = 0; r < 3; r++) {
            for (c = 0; c < 9; c++) {
                const int x = pc_dresser_layout_col_x(c), y = pc_dresser_layout_row_y(r);
                const int cell[4] = { x - 11, x + 11, y - 11, y + 11 };
                in &= cell[0] >= PC_DLAYOUT_IN_X0 && cell[1] <= PC_DLAYOUT_IN_X1 && cell[2] >= PC_DLAYOUT_IN_Y0 && cell[3] <= PC_DLAYOUT_IN_Y1;
                for (k = 0; k < (int)(sizeof(k_ring) / sizeof(k_ring[0])); k++) {
                    int q[4];
                    quad_rect(tab, k_ring[k], q);
                    hit += overlap(cell, q);
                }
            }
        }
        CHECK(in && hit == 0);
        CHECK(pc_dresser_layout_col_x(0) == -106 && pc_dresser_layout_col_x(8) == 70 && pc_dresser_layout_row_y(0) == 82 && pc_dresser_layout_row_y(2) == 38);
        /* the ORIGINAL (23:12) placement overlapped the ORIGINAL ring: the defect */
        hit = 0;
        for (r = 0; r < 3; r++) {
            for (c = 0; c < 9; c++) {
                const int x = -18 + 22 * (c - 4), y = 84 - 22 * r;
                const int cell[4] = { x - 11, x + 11, y - 11, y + 11 };
                for (k = 0; k < (int)(sizeof(k_ring) / sizeof(k_ring[0])); k++) {
                    int q[4];
                    quad_rect(orig, k_ring[k], q);
                    hit += overlap(cell, q);
                }
            }
        }
        printf("23:12 layout vs original ring: %d overlaps (the reported defect)\n", hit);
        CHECK(hit > 0);
    }
    /* L6 the hole quad covers the gap between the bottom band (strip column) and the cells */
    {
        V h[4];
        CHECK(pc_dresser_layout_hole_quad(h) == 1);
        CHECK(h[0].ob[0] <= pc_dresser_layout_rx(-47) && h[2].ob[0] >= pc_dresser_layout_rx(49) && h[1].ob[1] <= pc_dresser_layout_ry(34) &&
              h[0].ob[1] >= pc_dresser_layout_row_y(2) - 11);
    }
    /* L7 double begin = no-op (never remapped twice); end = byte-exact restore; end again = no-op */
    {
        V snap[128];
        memcpy(snap, tab, sizeof(snap));
        CHECK(pc_dresser_layout_begin(tab, 128) == 0 && memcmp(tab, snap, sizeof(snap)) == 0);
    }
    CHECK(pc_dresser_layout_end() == 1 && !pc_dresser_layout_active() && memcmp(tab, orig, sizeof(tab)) == 0);
    CHECK(pc_dresser_layout_end() == 0 && memcmp(tab, orig, sizeof(tab)) == 0);
    {
        V h[4];
        CHECK(pc_dresser_layout_hole_quad(h) == 0); /* not active */
    }
    /* L8 repeated entry / exit stays byte-exact */
    for (i = 0; i < 50; i++) {
        CHECK(pc_dresser_layout_begin(tab, 128) == 1);
        CHECK(pc_dresser_layout_end() == 1);
    }
    CHECK(memcmp(tab, orig, sizeof(tab)) == 0);
    printf("dresser_layout_selftest: %s (%d failures)\n", s_fail == 0 ? "PASS" : "FAIL", s_fail);
    return s_fail == 0 ? 0 : 1;
}
