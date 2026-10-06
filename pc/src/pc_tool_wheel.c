#include "pc_tool_wheel.h"
#include "pc_platform.h"
#include "pc_menu_util.h"
#include "pc_pause_menu.h"
#include "pc_text_draw.h"
#include "m_common_data.h"
#include "m_play.h"
#include "m_field_info.h"
#include "m_name_table.h"
#include "m_private.h"
#include "m_font.h"
#include "m_rcp.h"
#include "graph.h"
#include "main.h"
#include <SDL.h>
#include <math.h>
#include <stdio.h>

#define WHEEL_MAX 16

typedef struct {
    mActor_name_t item;
    int           slot; /* pocket slot, or -2 = the tool in hand right now */
} WheelEntry;

static int        s_open;          /* the wheel is up */
static int        s_pad_opened;    /* opened with the pad button (right stick selects) instead of the key (mouse selects) */
static int        s_sel;           /* 0 = empty hand (centre), 1..s_n = ring entries */
static int        s_n;
static WheelEntry s_entries[WHEEL_MAX];
static int        s_req_kind, s_req_slot;
static int        s_prev_hold;
static int        s_suppress_c;    /* pad opener: the right stick stays consumed after the wheel closed until it has been back inside the deadzone once */

/* the same gates Player_actor_check_and_switch_tool applies, evaluated early so the wheel never opens where an equip is impossible */
static int wheel_can_use(void) {
    GAME_PLAY* play = (GAME_PLAY*)gamePT;
    if (play == NULL || g_pc_paused) {
        return 0;
    }
    if (mFI_GET_TYPE(mFI_GetFieldId()) != mFI_FIELDTYPE2_FG) {
        return 0;
    }
    return !play->submenu.start_refuse && play->submenu.current_menu_type == mSM_OVL_NONE && play->submenu.menu_type == mSM_OVL_NONE;
}

static void wheel_build(void) {
    Private_c* priv = Common_Get(now_private);
    int i;
    s_n = 0;
    if (priv == NULL) {
        return;
    }
    if (ITEM_IS_TOOL(priv->equipment) && s_n < WHEEL_MAX) {
        s_entries[s_n].item = priv->equipment;
        s_entries[s_n].slot = -2;
        s_n++;
    }
    for (i = 0; i < mPr_POCKETS_SLOT_COUNT && s_n < WHEEL_MAX; i++) {
        mActor_name_t it = priv->inventory.pockets[i];
        if (mPr_GET_ITEM_COND(priv->inventory.item_conditions, i) == mPr_ITEM_COND_NORMAL && ITEM_IS_TOOL(it)) {
            s_entries[s_n].item = it;
            s_entries[s_n].slot = i;
            s_n++;
        }
    }
}

/* prev = the current selection: it is kept a little beyond its own sector edge / the centre deadzone, so a cursor or stick resting on a boundary or near the middle does not flicker */
static int wheel_pick_h(float dx, float dy, float dead, int n_tools, int prev) {
    const float tau = 6.28318530718f;
    float len = sqrtf(dx * dx + dy * dy);
    float ang, sector, centre, diff;
    int idx;
    if (n_tools <= 0 || len <= dead * (prev == 0 ? 1.0f : 0.8f)) {
        return 0;
    }
    ang = atan2f(dx, -dy); /* 0 = straight up, clockwise positive */
    if (ang < 0.0f) {
        ang += tau;
    }
    sector = tau / (float)n_tools;
    if (prev > 0 && prev <= n_tools) {
        centre = sector * (float)(prev - 1);
        diff = fabsf(ang - centre);
        if (diff > tau * 0.5f) {
            diff = tau - diff;
        }
        if (diff < sector * 0.5f * 1.06f) {
            return prev;
        }
    }
    idx = (int)(ang / sector + 0.5f) % n_tools;
    return 1 + idx;
}

int pc_tool_wheel_pick(float dx, float dy, float dead, int n_tools) {
    return wheel_pick_h(dx, dy, dead, n_tools, 0);
}

void pc_tool_wheel_input(int hold_kb, int hold_pad, int mx, int my, int rx, int ry, int dz, unsigned short* buttons, signed char* cstick_x, signed char* cstick_y) {
    int hold = hold_kb || hold_pad;
    if (hold && !s_prev_hold && !s_open && wheel_can_use()) { /* rising edge */
        wheel_build();
        s_open = 1;
        s_pad_opened = hold_pad && !hold_kb;
        s_sel = 0;
    }
    if (s_open) {
        if (!wheel_can_use()) { /* scene / menu / pause changed under the wheel: cancel, no equip */
            s_open = 0;
        } else if (hold) {
            if (s_pad_opened) {
                float dx = (float)rx, dy = (float)ry;
                if (sqrtf(dx * dx + dy * dy) > (float)dz) { /* LATCHED: only a deliberate push outside the deadzone changes the selection; recentering keeps it */
                    s_sel = wheel_pick_h(dx, dy, 0.0f, s_n, s_sel > 0 ? s_sel : 0);
                }
            } else {
                int w = 640, h = 480;
                if (g_pc_window != NULL) {
                    SDL_GetWindowSize(g_pc_window, &w, &h);
                }
                s_sel = wheel_pick_h((float)(mx - w / 2), (float)(my - h / 2), 0.08f * (float)(w < h ? w : h), s_n, s_sel);
            }
            *buttons = 0; /* no gameplay action while the wheel is up (the opener button / mouse clicks included) */
            *cstick_x = 0;
            *cstick_y = 0;
        } else { /* release: submit once */
            s_open = 0;
            *buttons = 0; /* this frame still belongs to the wheel */
            *cstick_x = 0;
            *cstick_y = 0;
            s_suppress_c = s_pad_opened;
            if (s_sel == 0) {
                s_req_kind = 2;
            } else if (s_sel - 1 < s_n && s_entries[s_sel - 1].slot >= 0) {
                s_req_kind = 1;
                s_req_slot = s_entries[s_sel - 1].slot;
            } /* the tool already in hand: nothing to do */
        }
    }
    if (s_suppress_c && !s_open) { /* camera input resumes once the stick has been neutral */
        if (sqrtf((float)rx * (float)rx + (float)ry * (float)ry) > (float)dz) {
            *cstick_x = 0;
            *cstick_y = 0;
        } else {
            s_suppress_c = 0;
        }
    }
    s_prev_hold = hold;
}

int pc_tool_wheel_is_open(void) {
    return s_open;
}

int pc_tool_wheel_take_request(int* slot) {
    int k = s_req_kind;
    s_req_kind = 0;
    if (slot != NULL) {
        *slot = s_req_slot;
    }
    return k;
}

static const char* tool_name(mActor_name_t it) {
    if (it == ITM_NET || it == ITM_GOLDEN_NET) return "Net";
    if (IS_ITEM_AXE(it)) return "Axe";
    if (it == ITM_SHOVEL || it == ITM_GOLDEN_SHOVEL) return "Shovel";
    if (it == ITM_ROD || it == ITM_GOLDEN_ROD) return "Rod";
    if (ITEM_IS_UMBRELLA(it) || ITEM_IS_MYUMBRELLA_TOOL(it)) return "Umbrella";
    if (ITEM_IS_BALLOON(it)) return "Balloon";
    if (ITEM_IS_SCOOP(it)) return "Scoop";
    return "Tool";
}

/* flat translucent rectangle in the font phase (same display-list recipe as pc_menu_dim_rect) */
static void wheel_rect(struct game_s* game, float x, float y, float w, float h, int r, int g, int b, int a) {
    Gfx* gfx;
    OPEN_DISP(game->graph);
    gfx = NOW_FONT_DISP;
    gDPPipeSync(gfx++);
    gDPSetOtherMode(gfx++, G_AD_DISABLE | G_CD_MAGICSQ | G_CK_NONE | G_TC_FILT | G_TF_POINT | G_TT_NONE | G_TL_TILE | G_TD_CLAMP | G_TP_NONE | G_CYC_1CYCLE | G_PM_NPRIMITIVE,
                    G_AC_NONE | G_ZS_PRIM | G_RM_XLU_SURF | G_RM_XLU_SURF2);
    gDPSetCombineMode(gfx++, G_CC_PRIMITIVE, G_CC_PRIMITIVE);
    gDPSetPrimColor(gfx++, 0, 0, r, g, b, a);
    gfx = gfx_gSPTextureRectangle1(gfx, (int)(x * 4.0f), (int)(y * 4.0f), (int)((x + w) * 4.0f), (int)((y + h) * 4.0f), 0, 0, 0, 0, 0);
    gDPPipeSync(gfx++);
    SET_FONT_DISP(gfx);
    CLOSE_DISP(game->graph);
}

void pc_tool_wheel_draw(struct game_s* game) {
    int i;
    const float cx = 160.0f, cy = 120.0f, R = 74.0f; /* a true circle: the screen is a uniform scale of this 320x240 space, so a slot sits at exactly the angle its sector is selected at */
    if (!s_open || game == NULL || game->graph == NULL) {
        return;
    }
    mFont_SetMatrix(game->graph, mFont_MODE_FONT);
    pc_menu_dim_rect(game->graph, 120);
    wheel_rect(game, cx - R - 44.0f, cy - R - 20.0f, 2.0f * (R + 44.0f), 2.0f * (R + 20.0f), 0, 0, 0, 70); /* a darker ring area */
    for (i = 0; i <= s_n; i++) {
        float ang = (s_n > 0 && i > 0) ? (6.28318530718f * (float)(i - 1) / (float)s_n) : 0.0f;
        float x = cx + (i > 0 ? R * sinf(ang) : 0.0f);
        float y = cy - 9.0f - (i > 0 ? R * cosf(ang) : 0.0f); /* y = top of the box; its centre is the circle point */
        char label[40];
        int sel = (i == s_sel);
        float tw, bw;
        if (i == 0) {
            snprintf(label, sizeof(label), "Empty hand");
        } else {
            snprintf(label, sizeof(label), "%s%s", tool_name(s_entries[i - 1].item), s_entries[i - 1].slot == -2 ? " *" : "");
        }
        tw = (float)pc_text_width(label) * (sel ? 1.15f : 1.0f);
        bw = tw + 12.0f;
        if (i > 0) { /* spoke from the centre to the slot */
            wheel_rect(game, (cx + x) * 0.5f - 1.0f, (cy + y) * 0.5f - 1.0f, 2.0f, 2.0f, 255, 255, 255, 60);
        }
        wheel_rect(game, x - bw * 0.5f - 2.0f, y - 3.0f, bw + 4.0f, 22.0f, sel ? 255 : 0, sel ? 220 : 0, sel ? 90 : 0, sel ? 235 : 120); /* frame */
        wheel_rect(game, x - bw * 0.5f, y - 1.0f, bw, 18.0f, sel ? 150 : 25, sel ? 105 : 25, sel ? 20 : 40, sel ? 240 : 210);
        pc_text_draw(game, label, x - tw * 0.5f, y + 1.0f, sel ? 255 : 215, sel ? 245 : 215, sel ? 190 : 215, 255, sel ? 1.15f : 1.0f);
    }
    mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
}
