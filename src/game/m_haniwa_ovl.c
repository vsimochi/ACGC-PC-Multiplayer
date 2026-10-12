#include "m_haniwa_ovl.h"

#include "m_submenu_ovl.h"
#include "m_common_data.h"
#include "sys_matrix.h"
#include "m_tag_ovl.h"
#include "m_player_lib.h"
#include "m_inventory_ovl.h"
#include "m_hand_ovl.h"
#include "m_font.h"
#include "m_haniwaPortrait_ovl.h"
#ifdef TARGET_PC
#include "pc_dresser_ui.h" /* dresser mode: the overlay shows the opened dresser's 27 slots on one 9 x 3 grid (never the save's gyroid) */
#include "pc_dresser_layout.h" /* the 9-slice enlargement of the gyroid window in dresser mode */
#include <stdlib.h> /* 6t: getenv (AC_DRESSER_DEBUG_GRID) */
#endif

static mHW_Ovl_c haniwa_ovl_data;

static void mHW_set_interrupt_message(Submenu* submenu, mTG_tag_c* tag, int msg_idx) {
    mHW_Ovl_c* haniwa_ovl = submenu->overlay->haniwa_ovl;

    haniwa_ovl->msg_interrupt_idx = msg_idx;
    haniwa_ovl->table_idx = tag->table;
    haniwa_ovl->sub_idx = tag->tag_col;
    haniwa_ovl->msg_time = 120;
}

static void mHW_set_price(Submenu* submenu, u8** str) {
    mTG_tag_c* tag = &submenu->overlay->tag_ovl->tags[0];
    mSM_MenuInfo_c* menu_info = &submenu->overlay->menu_info[mSM_OVL_HANIWA];
    u8 price_str[6];
    u32 price = Save_Get(homes[menu_info->data1]).haniwa.items[tag->tag_col].extra_data;
    int len;
    int found;

    submenu->overlay->haniwa_ovl->msg_price = price;
    mFont_UnintToString(price_str, sizeof(price_str), price, sizeof(price_str) - 1, TRUE, FALSE, TRUE);
    len = mMl_strlen2(&found, price_str, sizeof(price_str), CHAR_SPACE);
    if (found == TRUE) {
        len--;
    }
    mem_copy(*str, price_str, len);
    (*str)[len] = CHAR_SPACE;
    *str += len + 1;
}

typedef struct {
    u8* str;
    int len;
} mHW_msg_c;

static void mHW_make_cond_message(Submenu* submenu, int msg_idx) {
    static u8 mes_ikaga[15] = "How can I help?";
    static u8 mes_dono[11] = "Choose one.";
    static u8 mes_dono2[15] = "May I help you?";
    static u8 mes_oikura[9] = "How much?";
    static u8 mes_kasiko[7] = "Got it!";
    static u8 mes_dorewo[11] = "Choose one.";
    static u8 mes_mou[20] = "You don't have room.";
    static u8 mes_okane[22] = "You can't afford that.";
    static u8 mes_maido[20] = "Thank you very much!";
    static u8 mes_tada[11] = "That's free";
    static u8 mes_tada2[9] = "Give Away";
    static u8 mes_mise[20] = "That's display only.";
    static u8 mes_beru[5] = "It's\xD3";
    static u8 mes_beru2[5] = "Bells";

    static mHW_msg_c mes_data_tbl[] = {
        // clang-format off
        {mes_ikaga, sizeof(mes_ikaga)},
        {mes_dono, sizeof(mes_dono)},
        {mes_dono2, sizeof(mes_dono2)},
        {mes_oikura, sizeof(mes_oikura)},
        {mes_kasiko, sizeof(mes_kasiko)},
        {mes_dorewo, sizeof(mes_dorewo)},
        {mes_mou, sizeof(mes_mou)},
        {mes_okane, sizeof(mes_okane)},
        {mes_maido, sizeof(mes_maido)},
        {mes_tada, sizeof(mes_tada)},
        {mes_tada2, sizeof(mes_tada2)},
        {mes_mise, sizeof(mes_mise)},
        {mes_beru2, sizeof(mes_beru2)},
        // clang-format on
    };

    static mHW_msg_c mes_beru_data = { mes_beru, sizeof(mes_beru) };

    mHW_msg_c* msg_p = &mes_data_tbl[msg_idx];
    u8* str_p = submenu->overlay->haniwa_ovl->msg;

    mem_clear(str_p, mHW_OVL_MSG_SIZE, CHAR_SPACE);
    if (msg_idx == mHW_MSG_BERU2) {
        mem_copy(str_p, mes_beru_data.str, mes_beru_data.len);
        str_p += mes_beru_data.len;
        mHW_set_price(submenu, &str_p);
    }

    mem_copy(str_p, msg_p->str, msg_p->len);
}

static int mHW_make_message_interrupt(Submenu* submenu) {
    mTG_Ovl_c* tag_ovl = submenu->overlay->tag_ovl;
    mTG_tag_c* tag = &tag_ovl->tags[tag_ovl->sel_tag_idx];
    mHW_Ovl_c* haniwa_ovl = submenu->overlay->haniwa_ovl;
    int timer = haniwa_ovl->msg_time;
    int msg_idx = haniwa_ovl->msg_interrupt_idx;

    if (timer - 1 < 0) {
        haniwa_ovl->msg_interrupt_idx = -1;
    }

    haniwa_ovl->msg_time = timer - 1;
    if (msg_idx != mHW_MSG_DOREWO) {
        if (tag_ovl->sel_tag_idx == 0) {
            if (tag->table != haniwa_ovl->table_idx || tag->tag_col != haniwa_ovl->sub_idx) {
                haniwa_ovl->msg_interrupt_idx = -1;
            }
            
        } else if (tag_ovl->ret_tag_idx == tag_ovl->sel_tag_idx) {
            haniwa_ovl->msg_interrupt_idx = -1;
        }
    } else {
        if (tag_ovl->sel_tag_idx != -1) {
            if (tag->table != haniwa_ovl->table_idx || tag->tag_col != haniwa_ovl->sub_idx) {
                haniwa_ovl->msg_interrupt_idx = -1;
            }
        }
    }

    return msg_idx;
}

static int mHW_make_message_normal(Submenu* submenu, mSM_MenuInfo_c* menu_info) {
    mTG_Ovl_c* tag_ovl = submenu->overlay->tag_ovl;
    mTG_tag_c* tag = &tag_ovl->tags[tag_ovl->sel_tag_idx];
    int msg_idx = submenu->overlay->haniwa_ovl->msg_idx;

#ifdef TARGET_PC
    if (pc_dresser_ui_active()) {
        /* dresser mode: the gyroid's own status lines, no price / trade lines */
        if (tag->table == mTG_TABLE_HANIWA && pc_dresser_ui_slot_item(tag_ovl->tags[0].tag_col + tag_ovl->tags[0].tag_row * PC_DRESSER_UI_COLS) != 0) {
            return mHW_MSG_DONO; /* "Choose one." */
        }
        return mHW_MSG_IKAGA; /* "How can I help?" */
    }
#endif
    switch (tag->type) {
        case mTG_TYPE_NONE:
        case mTG_TYPE_HANIWA_PUT_ITEM:
            if (tag->table == mTG_TABLE_HANIWA) {
                Haniwa_Item_c* item = &Save_Get(homes[menu_info->data1]).haniwa.items[tag_ovl->tags[0].tag_col];
                
                if (item->item != EMPTY_NO) {
                    switch (item->exchange_type) {
                        case mHm_HANIWA_TRADE_0:
                            if (menu_info->data0 == 0) {
                                msg_idx = mHW_MSG_TADA;
                            } else {
                                msg_idx = mHW_MSG_TADA2;
                            }
                            break;
                        case mHm_HANIWA_TRADE_1:
                            msg_idx = mHW_MSG_MISE;
                            break;
                        case mHm_HANIWA_TRADE_2:
                            msg_idx = mHW_MSG_BERU2;
                            break;
                        default:
                            msg_idx = mHW_MSG_MISE;
                            break;
                    }
                } else {
                    if (menu_info->data0 == 0) {
                        msg_idx = mHW_MSG_IKAGA;
                    } else {
                        msg_idx = mHW_MSG_DONO2;
                    }
                }
            } else {
                msg_idx = mHW_MSG_IKAGA;
            }
            break;
        case mTG_TYPE_HANIWA_ITEM:
        case mTG_TYPE_CATCH_ITEM:
            msg_idx = mHW_MSG_DONO;
            break;
        case mTG_TYPE_HANIWA_PRICE:
            msg_idx = mHW_MSG_OIKURA;
            break;
    }

    return msg_idx;
}

static void mHW_make_message(Submenu* submenu, mSM_MenuInfo_c* menu_info) {
    mTG_tag_c* tag = &submenu->overlay->tag_ovl->tags[0];
    mHW_Ovl_c* haniwa_ovl = submenu->overlay->haniwa_ovl;
    int msg_idx;
    int msg_idx_bak;
    int ctr;

    ctr = haniwa_ovl->msg_counter;
    ctr++;
    if (ctr > mHW_OVL_MSG_SIZE) {
        ctr = mHW_OVL_MSG_SIZE;
    }

    haniwa_ovl->msg_counter = ctr;
    msg_idx_bak = haniwa_ovl->msg_idx;
    if (haniwa_ovl->msg_interrupt_idx != -1) {
        msg_idx = mHW_make_message_interrupt(submenu);
    } else {
        msg_idx = mHW_make_message_normal(submenu, menu_info);
    }

#ifdef TARGET_PC
    if (pc_dresser_ui_active()) {
        if (msg_idx != msg_idx_bak) { /* dresser mode never shows a price (mHW_MSG_BERU2): the save's gyroid is not read */
            mHW_make_cond_message(submenu, msg_idx);
            haniwa_ovl->msg_idx = msg_idx;
            haniwa_ovl->msg_counter = 0;
        }
        return;
    }
#endif
    if (msg_idx != msg_idx_bak || (msg_idx == mHW_MSG_BERU2 &&
        haniwa_ovl->msg_price != Save_Get(homes[menu_info->data1]).haniwa.items[tag->tag_col].extra_data)) {
        mHW_make_cond_message(submenu, msg_idx);
        haniwa_ovl->msg_idx = msg_idx;
        haniwa_ovl->msg_counter = 0;
    }
}

static void mHW_move_Move(Submenu* submenu, mSM_MenuInfo_c* menu_info) {
    submenu->overlay->move_Move_proc(submenu, menu_info);
}

static void mHW_move_Play(Submenu* submenu, mSM_MenuInfo_c* menu_info) {
    if (!menu_info->open_flag) {
        submenu->overlay->tag_ovl->chg_tag_func_proc(submenu, mTG_TABLE_HANIWA, mTG_TYPE_NONE, 0, 0.0f, 0.0f);
        menu_info->open_flag = TRUE;
    }
    
    submenu->overlay->menu_control.tag_move_func(submenu, menu_info);
}

static void mHW_move_End(Submenu* submenu, mSM_MenuInfo_c* menu_info) {
    submenu->overlay->move_End_proc(submenu, menu_info);
}

static void mHW_haniwa_ovl_move(Submenu* submenu) {
    static mSM_MOVE_PROC ovl_move_proc[] = {
        // clang-format off
        mHW_move_Move,
        mHW_move_Play,
        (mSM_MOVE_PROC)none_proc1,
        (mSM_MOVE_PROC)none_proc1,
        mHW_move_End,
        // clang-format on
    };
    
    mSM_MenuInfo_c* menu_info = &submenu->overlay->menu_info[mSM_OVL_HANIWA];

    menu_info->pre_move_func(submenu);
#ifdef TARGET_PC
    /* the menus below (the inventory in HANIWA_ENTRUST mode follows this menu: OBEY) can END inside pre_move_func on the same frame as this one (variable dt):
     * mSM_return_func then unloads every overlay (mHW_haniwa_ovl_destruct: haniwa_ovl = NULL) and nothing of this menu may run any more */
    if (submenu->overlay->haniwa_ovl == NULL) {
        printf("[NET][DRESSER] gyroid overlay: the menus below ended on this frame (overlays unloaded): haniwa move stopped\n");
        return;
    }
#endif
    (*ovl_move_proc[menu_info->proc_status])(submenu, menu_info);
#ifdef TARGET_PC
    if (submenu->overlay->haniwa_ovl == NULL) {
        printf("[NET][DRESSER] gyroid overlay: this menu ended and unloaded the overlays on this frame: haniwa move stopped\n");
        return; /* mHW_move_End -> mSM_return_func unloaded the overlays (crash fix: mHW_make_message read haniwa_ovl->msg_counter of a NULL overlay) */
    }
    if (pc_dresser_ui_active()) {
        /* dresser mode: the host's answer to a take / put of an EXTRA slot (the pockets follow the host post-image in the transaction layer) */
        const int act = pc_dresser_ui_poll();

        if (act == PC_DRESSER_UI_ACT_DONE) {
            mHW_set_interrupt_message(submenu, &submenu->overlay->tag_ovl->tags[0], mHW_MSG_KASIKO);
        } else if (act == PC_DRESSER_UI_ACT_FULL) {
            mHW_set_interrupt_message(submenu, &submenu->overlay->tag_ovl->tags[0], mHW_MSG_MOU);
        }
    }
#endif

    mHW_make_message(submenu, menu_info);

    switch (menu_info->proc_status) {
        case mSM_OVL_PROC_PLAY:
#ifdef TARGET_PC
            if (!pc_dresser_ui_active()) /* dresser mode: no gyroid portrait */
#endif
            submenu->overlay->hanwiaPortrait_ovl->haniwaPortrait_shape_move_proc(submenu);
            submenu->overlay->menu_control.animation_flag = TRUE;
            break;
        case mSM_OVL_PROC_MOVE:
            submenu->overlay->menu_control.animation_flag = FALSE;
            break;
    }
}

extern Gfx hni_win_mode[];
extern Gfx hni_win_modelT[];

#ifdef TARGET_PC
extern Gfx hni_nwin2_win2T_model[];
extern Vtx hni_win_v[];

/* dresser mode (U3): the gyroid window without the portrait backdrop (hni_nwin_3DmT), the dialogue frame (hni_nwin2_wakuT) and the 4-cell strip
 * (hni_nwin2_win2T); instead 3 rows of 9 slot cells in the gyroid's own cell style (win2T's texture / colours, its strip quad re-spanned over 9 cells).
 * hni_win_modelT[0..17] = background layers w1..w13, the render-state setup and the window panel (winT); [18] win2T, [19] 3DmT, [20] wakuT. */
static Vtx mHW_pc_dresser_cell_v[PC_DRESSER_UI_ROWS * 4];
static Vtx mHW_pc_dresser_dbg_v[PC_DRESSER_UI_SLOTS * 4]; /* 6t: AC_DRESSER_DEBUG_GRID markers (window space) */

/* 6t: 1 = AC_DRESSER_DEBUG_GRID=1 (read once): draw the cell-centre / icon-anchor markers */
static int mHW_pc_dresser_debug_grid(void) {
    static int v = -1;
    if (v < 0) {
        const char* e = getenv("AC_DRESSER_DEBUG_GRID");
        v = (e != NULL && e[0] == '1') ? 1 : 0;
    }
    return v;
}
static Vtx mHW_pc_dresser_panel_v[4]; /* 6u / 6v: the solid panel (inner rectangle) */
extern Gfx hni_win_w13_model[];

/* 6w: the window interior's flat tan = hni_nwin2_winT's env colour (215,185,155): the border pieces' inner tan, the w-layers and the panel all use it */
#define MHW_PC_DRESSER_TAN_R 215
#define MHW_PC_DRESSER_TAN_G 185
#define MHW_PC_DRESSER_TAN_B 155
/* 6w: a 4x4 GX RGBA8 texture (one 4x4 tile: 16 A,R pairs then 16 G,B pairs) of that EXACT tan (RGBA8, not a 5-bit RGB5A3 palette, so no quantisation
 * shift). It replaces ONLY TEXEL0 (hni_nwin2_hani_tex, the gyroid pattern, tile 0) of hni_win_mode: the combiner, the render mode / blend and TEXEL1 (each
 * w-layer's "shita" shape mask, tile 1 = the alpha) stay vanilla, so the layers keep their oval shape (transparent outside, room visible). (6v's
 * ENVIRONMENT/TEXEL1 combiner override lost the mask alpha: opaque white quads.) */
#define MHW_PC_TAN_AR 255, MHW_PC_DRESSER_TAN_R
#define MHW_PC_TAN_GB MHW_PC_DRESSER_TAN_G, MHW_PC_DRESSER_TAN_B
static u8 mHW_pc_dresser_flat_tex[64] ATTRIBUTE_ALIGN(32) = {
    MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR,
    MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR, MHW_PC_TAN_AR,
    MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB,
    MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB, MHW_PC_TAN_GB,
};

static void mHW_pc_dresser_frame(Gfx** gfxp) {
    /* = m_tag_ovl.c mTG_pc_dresser_line_pos: the rows of the ENLARGED window (pc_dresser_layout: inner rectangle x -120..84, y 26..94) */
    static const f32 row_y[PC_DRESSER_UI_ROWS] = { 82.0f, 60.0f, 38.0f };
    const Vtx* tpl = &hni_win_v[68]; /* the 4-cell strip: 68 top-left (-47, 58), 69 bottom-left, 70 top-right (49, 58), 71 bottom-right; 24 per cell */
    Gfx* gfx = *gfxp;
    int r;
    int i;

    /* 6w: the window interior is ONE flat colour: the background layers w1..w13 normally colour the oval with the gyroid pattern (hni_win_mode: colour =
     * TEXEL0 = hni_nwin2_hani_tex on tile 0, alpha = TEXEL1 = the w-layer's shape mask on tile 1). In dresser mode tile 0 is re-pointed at a flat texture
     * of the window's own tan (mHW_pc_dresser_flat_tex); the combiner / render mode / TEXEL1 mask stay vanilla. The w-layer lists only load tile 1 and
     * set the prim colour, so tile 0 holds for all 13. (Runtime commands: a static Gfx[] with a pointer is not a constant initializer on PC.) */
    gDPPipeSync(gfx++);
    gDPSetTextureImage_Dolphin(gfx++, G_IM_FMT_RGBA, G_IM_SIZ_32b, 4, 4, mHW_pc_dresser_flat_tex); /* (pkt, fmt, siz, h, w, img) */
    gDPSetTile_Dolphin(gfx++, G_DOLPHIN_TLUT_DEFAULT_MODE, 0, 0, GX_REPEAT, GX_REPEAT, 0, 0);
    for (i = 0; i <= 12; i++) { /* the background layers w1..w13 (flat, see above) */
        *gfx++ = hni_win_modelT[i];
    }
    /* 6u: the w13 "hole" quad under the skipped strip is NOT drawn any more (it showed the gyroid pattern + an orange bar as a dark block in row 3) */
    for (i = 13; i <= 17; i++) { /* the render-state setup and the window panel (winT): border ring + fill */
        *gfx++ = hni_win_modelT[i];
    }
    {
        /* 6u / 6v: an OPAQUE solid panel right behind the 3 x 9 grid (the enlarged inner rectangle x -120..84, y 26..94) in the SAME flat tan as the rest of
         * the interior (so no edge line shows where the panel meets the window's own fill); then the window's XLU mode again. (The 6u orange "gap" quad
         * is gone: that band is inside the window, not part of its border.) */
        for (i = 0; i < 4; i++) {
            Vtx* d = &mHW_pc_dresser_panel_v[i];
            *d = tpl[i];
            d->v.ob[0] = (s16)((i >= 2) ? PC_DLAYOUT_IN_X1 : PC_DLAYOUT_IN_X0);
            d->v.ob[1] = (s16)((i & 1) ? PC_DLAYOUT_IN_Y0 : PC_DLAYOUT_IN_Y1);
        }
        gDPPipeSync(gfx++);
        gDPSetRenderMode(gfx++, G_RM_OPA_SURF, G_RM_OPA_SURF2);
        gDPSetCombineMode(gfx++, G_CC_PRIMITIVE, G_CC_PRIMITIVE);
        gSPVertex(gfx++, mHW_pc_dresser_panel_v, 4, 0);
        gDPSetPrimColor(gfx++, 0, 0, MHW_PC_DRESSER_TAN_R, MHW_PC_DRESSER_TAN_G, MHW_PC_DRESSER_TAN_B, 255); /* 6w: = the interior AS RENDERED: flat w-layers (tan) under winT's inner tan (env, any alpha) = tan */
        gSP2Triangles(gfx++, 0, 1, 2, 0, 1, 3, 2, 0);
        gDPPipeSync(gfx++);
        gDPSetRenderMode(gfx++, G_RM_XLU_SURF, G_RM_XLU_SURF2); /* = hni_win_modelT's mode for the cells that follow */
    }
    for (r = 0; r < PC_DRESSER_UI_ROWS; r++) {
        Vtx* v = &mHW_pc_dresser_cell_v[r * 4];

        for (i = 0; i < 4; i++) {
            const int right = (i >= 2);

            v[i] = tpl[i];
            v[i].v.ob[0] = (s16)(right ? -18 + 99 : -18 - 99); /* 9 cells x 22 = 198, centred on the window (x -126..90) */
            v[i].v.ob[1] = (s16)(row_y[r] + ((i & 1) ? -11.0f : 11.0f));
            if (right) { /* 9 cells of texture instead of 4 */
                v[i].v.tc[0] = (s16)(tpl[0].v.tc[0] + ((tpl[2].v.tc[0] - tpl[0].v.tc[0]) * PC_DRESSER_UI_COLS) / 4);
            }
        }
    }
    for (i = 0; i <= 5; i++) { /* win2T's texture + colour setup (everything before its vertex load) */
        *gfx++ = hni_nwin2_win2T_model[i];
    }
    gSPVertex(gfx++, mHW_pc_dresser_cell_v, PC_DRESSER_UI_ROWS * 4, 0);
    gSP2Triangles(gfx++, 0, 1, 2, 0, 1, 3, 2, 0);
    gSP2Triangles(gfx++, 4, 5, 6, 0, 5, 7, 6, 0);
    gSP2Triangles(gfx++, 8, 9, 10, 0, 9, 11, 10, 0);
    if (mHW_pc_dresser_debug_grid()) { /* 6t: RED marks = the drawn cell centres (x -106 + 22 c, y 82 / 60 / 38), through the window's own transform */
        int c, k;
        gDPPipeSync(gfx++);
        gDPSetCombineMode(gfx++, G_CC_PRIMITIVE, G_CC_PRIMITIVE);
        gDPSetPrimColor(gfx++, 0, 0, 255, 0, 0, 255);
        for (k = 0; k < PC_DRESSER_UI_SLOTS; k++) {
            const int cx = -106 + 22 * (k % PC_DRESSER_UI_COLS), cy = (int)row_y[k / PC_DRESSER_UI_COLS];
            for (c = 0; c < 4; c++) {
                Vtx* d = &mHW_pc_dresser_dbg_v[k * 4 + c];
                *d = tpl[c];
                d->v.ob[0] = (s16)(cx + ((c >= 2) ? 1 : -1));
                d->v.ob[1] = (s16)(cy + ((c & 1) ? -1 : 1));
            }
        }
        for (k = 0; k < PC_DRESSER_UI_SLOTS; k += 8) { /* the vertex cache holds 32: load in groups of 8 cells */
            const int n = (PC_DRESSER_UI_SLOTS - k) < 8 ? (PC_DRESSER_UI_SLOTS - k) : 8;
            gSPVertex(gfx++, &mHW_pc_dresser_dbg_v[k * 4], n * 4, 0);
            for (c = 0; c < n; c++) {
                gSP2Triangles(gfx++, c * 4 + 0, c * 4 + 1, c * 4 + 2, 0, c * 4 + 1, c * 4 + 3, c * 4 + 2, 0);
            }
        }
    }
    *gfxp = gfx;
}
#endif

static void mHW_set_frame_dl(Submenu* submenu, GRAPH* graph, f32 pos_x, f32 pos_y) {
    int tex_x;
    int tex_y;

#ifdef TARGET_PC
    if (pc_dresser_ui_active()) {
        static int warned = 0;
        if (pc_dresser_layout_begin(hni_win_v, PC_DLAYOUT_NVTX) < 0 && !warned) { /* idempotent: remapped once from a byte-exact backup */
            warned = 1;
            printf("[NET][DRESSER] gyroid overlay: *** the gyroid window table is not the expected one: the window is NOT enlarged ***\n");
        }
    } else {
        (void)pc_dresser_layout_end(); /* guard: a normal gyroid window never draws remapped vertices (restored byte-exact; no-op when not remapped) */
    }
#endif

    Matrix_scale(16.0f, 16.0f, 1.0f, MTX_LOAD);
    Matrix_translate(pos_x, pos_y, -809.0f, MTX_MULT);

    OPEN_POLY_OPA_DISP(graph);

    gSPDisplayList(POLY_OPA_DISP++, hni_win_mode);
    gSPMatrix(POLY_OPA_DISP++, _Matrix_to_Mtx_new(graph), G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
    gDPTileSync(POLY_OPA_DISP++);
    tex_x = -submenu->overlay->menu_control.texture_pos[0] * 8;
    tex_y = -submenu->overlay->menu_control.texture_pos[1] * 8;
#ifdef TARGET_PC
    if (pc_dresser_ui_active()) {
        /* 6p: dresser mode keeps the window's background pattern STILL (vanilla scrolls it with the menus' shared texture_pos: up-left drift across the
         * enlarged window); every other screen keeps the vanilla scroll */
        tex_x = 0;
        tex_y = 0;
    }
#endif
    gDPSetTileSize_Dolphin(POLY_OPA_DISP++, G_TX_RENDERTILE, (u8)tex_x, (u8)tex_y, 32, 32);
#ifdef TARGET_PC
    if (pc_dresser_ui_active()) {
        mHW_pc_dresser_frame(&POLY_OPA_DISP);
    } else
#endif
    gSPDisplayList(POLY_OPA_DISP++, hni_win_modelT);

    CLOSE_POLY_OPA_DISP(graph);
}

extern Gfx inv_item_mode[];

static void mHW_set_item(Submenu* submenu, GRAPH* graph, mSM_MenuInfo_c* menu_info, f32 pos_x, f32 pos_y) {
    Haniwa_Item_c* item = Save_Get(homes[menu_info->data1]).haniwa.items;
    f32 pos[2];
    int i;

    OPEN_POLY_OPA_DISP(graph);
    
    gSPDisplayList(POLY_OPA_DISP++, inv_item_mode);
    
    CLOSE_POLY_OPA_DISP(graph);
#ifdef TARGET_PC
    if (pc_dresser_ui_active()) {
        /* dresser mode: all 27 slots (grid 0..2 vanilla keep slots, 3..26 extra storage) at the 9 x 3 grid positions, the inventory's icons at 7/8 size
         * (21 px in a 22 px cell); an empty slot shows its empty cell */
        static unsigned int drawn_sig = 0;
        unsigned int sig = 2166136261u;

        for (i = 0; i < PC_DRESSER_UI_SLOTS; i++) { /* evidence (printed only when the shown contents change): which grid cell draws which item */
            sig = (sig ^ (unsigned int)pc_dresser_ui_slot_item(i)) * 16777619u;
        }
        if (sig != drawn_sig) {
            drawn_sig = sig;
            printf("[NET][DRESSER] gyroid overlay: drawn cells:");
            for (i = 0; i < PC_DRESSER_UI_SLOTS; i++) {
                if (pc_dresser_ui_slot_item(i) != 0) {
                    printf(" grid %d = 0x%04X", i, (unsigned)pc_dresser_ui_slot_item(i));
                }
            }
            printf("\n");
        }
        for (i = 0; i < PC_DRESSER_UI_SLOTS; i++) {
            const int it = pc_dresser_ui_slot_item(i);

            if (it != 0 && !pc_dresser_ui_carry_hides(0, i)) { /* 6s: the grabbed item is drawn by the hand only */
                submenu->overlay->tag_ovl->set_hand_pos_proc(submenu, pos, mTG_TABLE_HANIWA, i);
                submenu->overlay->draw_item_proc(graph, pos_x + pos[0], pos_y + pos[1], 0.875f, (mActor_name_t)it, FALSE, TRUE, 0, FALSE, FALSE);
            }
        }
        if (mHW_pc_dresser_debug_grid()) { /* 6t: GREEN marks = the icon anchors (the icon's own centre: 160 + (int)(X * 0.975), 120 - Y, as mSM_draw_item) */
            OPEN_POLY_OPA_DISP(graph);
            gDPPipeSync(POLY_OPA_DISP++);
            gDPSetOtherMode(POLY_OPA_DISP++,
                            G_AD_DISABLE | G_CD_MAGICSQ | G_CK_NONE | G_TC_FILT | G_TF_POINT | G_TT_NONE | G_TL_TILE | G_TD_CLAMP | G_TP_NONE |
                                G_CYC_1CYCLE | G_PM_NPRIMITIVE,
                            G_AC_NONE | G_ZS_PRIM | G_RM_OPA_SURF | G_RM_OPA_SURF2);
            gDPSetCombineMode(POLY_OPA_DISP++, G_CC_PRIMITIVE, G_CC_PRIMITIVE);
            gDPSetPrimColor(POLY_OPA_DISP++, 0, 0, 0, 255, 0, 255);
            for (i = 0; i < PC_DRESSER_UI_SLOTS; i++) {
                f32 ax;
                f32 ay;

                submenu->overlay->tag_ovl->set_hand_pos_proc(submenu, pos, mTG_TABLE_HANIWA, i);
                ax = 160.0f + (f32)(int)((pos_x + pos[0]) * 0.975f);
                ay = 120.0f - (pos_y + pos[1]);
                POLY_OPA_DISP = gfx_gSPTextureRectangle1(POLY_OPA_DISP, (int)((ax - 1.0f) * 4.0f), (int)((ay - 1.0f) * 4.0f), (int)((ax + 1.0f) * 4.0f),
                                                         (int)((ay + 1.0f) * 4.0f), 0, 0, 0, 0, 0);
            }
            gDPPipeSync(POLY_OPA_DISP++);
            CLOSE_POLY_OPA_DISP(graph);
        }
        return;
    }
#endif

    for (i = 0; i < HANIWA_ITEM_HOLD_NUM; i++) {
        if (item->item != EMPTY_NO) {
            submenu->overlay->tag_ovl->set_hand_pos_proc(submenu, pos, mTG_TABLE_HANIWA, i);
            submenu->overlay->draw_item_proc(graph, pos_x + pos[0], pos_y + pos[1], 1.0f, item->item, FALSE, TRUE, 0, FALSE, FALSE);
        }

        item++;
    }
}

static void mHW_set_message(Submenu* submenu, GAME* game, mSM_MenuInfo_c* menu_info) {
    GRAPH* graph = game->graph;
    mHW_Ovl_c* haniwa_ovl = submenu->overlay->haniwa_ovl;

    submenu->overlay->set_char_matrix_proc(graph);
    // clang-format off
    mFont_SetLineStrings(
        game,
        haniwa_ovl->msg, haniwa_ovl->msg_counter,
        160.0f + (26.0f + (menu_info->position[0] - 89.0f)), 120.0f - ((97.0f + menu_info->position[1]) - 11.0f),
        45, 50, 0, 255,
        FALSE, TRUE,
        0.875f, 0.875f,
        mFont_MODE_POLY
    );
    // clang-format on
}

static void mHW_set_dl(mSM_MenuInfo_c* menu_info, Submenu* submenu, GAME* game) {
    GRAPH* graph = game->graph;
    f32 pos_x = menu_info->position[0];
    f32 pos_y = menu_info->position[1];

    mHW_set_frame_dl(submenu, graph, pos_x, pos_y);
    mHW_set_item(submenu, graph, menu_info, pos_x, pos_y);
#ifdef TARGET_PC
    if (!pc_dresser_ui_active()) /* dresser mode: no gyroid portrait */
#endif
    submenu->overlay->hanwiaPortrait_ovl->set_haniwaPortrait_proc(submenu, menu_info, graph, game, 39.0f + pos_x, 19.0f - pos_y);
#ifdef TARGET_PC
    if (!pc_dresser_ui_active()) /* dresser mode: no gyroid dialogue (its box is not drawn either); feedback = sounds + the tag windows */
#endif
    mHW_set_message(submenu, game, menu_info);
    submenu->overlay->menu_control.tag_draw_func(submenu, game, mSM_OVL_HANIWA);
}

static void mHW_haniwa_ovl_draw(Submenu* submenu, GAME* game) {
    mSM_MenuInfo_c* menu_info = &submenu->overlay->menu_info[mSM_OVL_HANIWA];

    menu_info->pre_draw_func(submenu, game);
#ifdef TARGET_PC
    if (submenu->overlay->haniwa_ovl == NULL || submenu->overlay->tag_ovl == NULL) {
        return; /* the overlays were unloaded (see mHW_haniwa_ovl_move) */
    }
#endif
    mHW_set_dl(menu_info, submenu, game);
}

extern void mHW_haniwa_ovl_set_proc(Submenu* submenu) {
    mSM_Control_c* ctrl = &submenu->overlay->menu_control;
    mSM_MenuInfo_c* menu_info = &submenu->overlay->menu_info[mSM_OVL_HANIWA];

    ctrl->menu_move_func = mHW_haniwa_ovl_move;
    ctrl->menu_draw_func = mHW_haniwa_ovl_draw;
    if (submenu->overlay->hand_ovl != NULL) {
        submenu->overlay->hand_ovl->set_hand_func(submenu);
    }
}

static void mHW_haniwa_ovl_init(Submenu* submenu) {
    mSM_MenuInfo_c* menu_info = &submenu->overlay->menu_info[mSM_OVL_HANIWA];
    mHW_Ovl_c* haniwa_ovl = submenu->overlay->haniwa_ovl;

    submenu->overlay->menu_control.animation_flag = FALSE;

    switch (menu_info->data0) {
        case 0:
            haniwa_ovl->msg_interrupt_idx = -1;
            haniwa_ovl->msg_time = 0;
            break;
        case 1:
            haniwa_ovl->msg_interrupt_idx = mHW_MSG_DOREWO;
            haniwa_ovl->table_idx = mTG_TABLE_HANIWA;
            haniwa_ovl->sub_idx = 0;
            haniwa_ovl->msg_time = 120;
            break;
    }

    mem_clear(haniwa_ovl->msg, mHW_OVL_MSG_SIZE, CHAR_SPACE);
    menu_info->proc_status = mSM_OVL_PROC_MOVE;
    menu_info->move_drt = mSM_MOVE_IN_LEFT;
    menu_info->next_proc_status = mSM_OVL_PROC_PLAY;
}

extern void mHW_haniwa_ovl_construct(Submenu* submenu) {
    Submenu_Overlay_c* ovl = submenu->overlay;
    mHW_Ovl_c* haniwa_ovl = ovl->haniwa_ovl;

    if (haniwa_ovl == NULL) {
        mem_clear((u8*)&haniwa_ovl_data, sizeof(haniwa_ovl_data), 0);
        ovl->haniwa_ovl = &haniwa_ovl_data;
        submenu->overlay->haniwa_ovl->set_interrupt_message_proc = mHW_set_interrupt_message;
        haniwa_ovl_data.msg_idx = -1;
        haniwa_ovl_data.table_idx = mTG_TABLE_ITEM;
        haniwa_ovl_data.sub_idx = 0;
    }

    mHW_haniwa_ovl_init(submenu);
    mHW_haniwa_ovl_set_proc(submenu);
}

extern void mHW_haniwa_ovl_destruct(Submenu* submenu) {
#ifdef TARGET_PC
    (void)pc_dresser_layout_end(); /* the overlay goes away: the gyroid window table back to its pristine bytes (no-op when not remapped) */
#endif
    submenu->overlay->haniwa_ovl = NULL;
}
