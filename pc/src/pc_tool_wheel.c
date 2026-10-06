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

int pc_tool_wheel_pick(float dx, float dy, float dead, int n_tools) {
    float len = sqrtf(dx * dx + dy * dy);
    float ang;
    int idx;
    if (n_tools <= 0 || len <= dead) {
        return 0;
    }
    ang = atan2f(dx, -dy); /* 0 = straight up, clockwise positive */
    if (ang < 0.0f) {
        ang += 6.28318530718f;
    }
    idx = (int)(ang / (6.28318530718f / (float)n_tools) + 0.5f) % n_tools;
    return 1 + idx;
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
                s_sel = pc_tool_wheel_pick(dx, dy, (float)dz, s_n);
            } else {
                int w = 640, h = 480;
                if (g_pc_window != NULL) {
                    SDL_GetWindowSize(g_pc_window, &w, &h);
                }
                s_sel = pc_tool_wheel_pick((float)(mx - w / 2), (float)(my - h / 2), 0.08f * (float)(w < h ? w : h), s_n);
            }
            *buttons = 0; /* no gameplay action while the wheel is up (the opener button / mouse clicks included) */
            *cstick_x = 0;
            *cstick_y = 0;
        } else { /* release: submit once */
            s_open = 0;
            if (s_sel == 0) {
                s_req_kind = 2;
            } else if (s_sel - 1 < s_n && s_entries[s_sel - 1].slot >= 0) {
                s_req_kind = 1;
                s_req_slot = s_entries[s_sel - 1].slot;
            } /* the tool already in hand: nothing to do */
        }
    }
    s_prev_hold = hold;
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

void pc_tool_wheel_draw(struct game_s* game) {
    int i;
    if (!s_open || game == NULL || game->graph == NULL) {
        return;
    }
    mFont_SetMatrix(game->graph, mFont_MODE_FONT);
    pc_menu_dim_rect(game->graph, 140);
    for (i = 0; i <= s_n; i++) {
        float ang = (s_n > 0 && i > 0) ? (6.28318530718f * (float)(i - 1) / (float)s_n) : 0.0f;
        float x = 160.0f + (i > 0 ? 78.0f * sinf(ang) : 0.0f);
        float y = 112.0f - (i > 0 ? 62.0f * cosf(ang) : 0.0f);
        char label[40];
        int sel = (i == s_sel);
        if (i == 0) {
            snprintf(label, sizeof(label), "%sEmpty hand", sel ? "> " : "");
        } else {
            snprintf(label, sizeof(label), "%s%s%s", sel ? "> " : "", tool_name(s_entries[i - 1].item), s_entries[i - 1].slot == -2 ? " *" : "");
        }
        pc_text_draw(game, label, x - (float)pc_text_width(label) * 0.5f, y, sel ? 255 : 200, sel ? 230 : 200, sel ? 60 : 200, 255, sel ? 1.25f : 1.0f);
    }
    mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
}
