/* pc_emote_wheel.c - the emote wheel UI (see pc_emote_wheel.h / pc_emote.h). Mirrors pc_tool_wheel.c: rising-edge open, mouse picks relative
 * to the window centre (keyboard opener) or the latched right stick (pad opener), hysteresis, zeroed buttons / C-stick while open, the C-stick
 * suppressed after a pad close until it was neutral once, a one-frame request, drawn in the font phase. */
#include "pc_emote_wheel.h"
#include "pc_emote.h"
#include "pc_platform.h"
#include "pc_menu_util.h"
#include "pc_pause_menu.h"
#include "pc_settings.h"
#include "pc_text_draw.h"
#include "m_common_data.h"
#include "m_play.h"
#include "m_font.h"
#include "m_rcp.h"
#include "graph.h"
#include "main.h"
#include <SDL.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int      s_open;
static int      s_pad_opened;
static int      s_sel;          /* 0 = centre (nothing), 1..8 = slices */
static int      s_req;
static int      s_prev_hold;
static int      s_armed;
static unsigned s_center_ms;
static int      s_suppress_c;

int pc_emote_enabled(void) {
    static int s_env = -1;
    if (s_env < 0) {
        const char* e = getenv("AC_EMOTES");
        s_env = (e != NULL && e[0] == '0') ? 0 : 1;
        if (!s_env) {
            printf("[EMOTE] disabled by AC_EMOTES=0\n");
        }
    }
    return s_env && g_pc_settings.emotes;
}

uint32_t pc_emote_now_ms(void) {
    return (uint32_t)SDL_GetTicks();
}

#ifdef PC_NET_TEST_HOOKS
static void pc_emote_test_tick(int* hold, int* sel, signed char* stick_x, signed char* stick_y);
#endif

void pc_emote_wheel_input(int hold_kb, int hold_pad, int mx, int my, int rx, int ry, int dz, unsigned short* buttons, signed char* stick_x,
                          signed char* stick_y, signed char* cstick_x, signed char* cstick_y) {
    int hold = hold_kb || hold_pad;
    int forced_sel = -1;

#ifdef PC_NET_TEST_HOOKS
    {
        int th = 0;
        pc_emote_test_tick(&th, &forced_sel, stick_x, stick_y);
        if (th) {
            hold = 1;
            hold_kb = 1; /* the script drives the keyboard opener (selection forced below) */
        }
    }
#else
    (void)stick_x;
    (void)stick_y;
#endif
    if (hold && !s_prev_hold && !s_open) { /* rising edge */
        const int r = pc_emote_player_gate(0);
        if (r == PC_EMOTE_OK) {
            s_open = 1;
            s_pad_opened = hold_pad && !hold_kb;
            s_sel = 0;
            s_armed = 0;
            s_center_ms = 0;
            printf("[EMOTE] wheel open (%s)\n", s_pad_opened ? "pad" : "key");
        } else if (r != PC_EMOTE_R_DISABLED) {
            printf("[EMOTE] wheel refused (%s)\n", pc_emote_reason_name(r));
        }
    }
    if (s_open) {
        const int r = pc_emote_player_gate(0);
        if (r != PC_EMOTE_OK) { /* scene / menu / state changed under the wheel: cancel, no emote */
            s_open = 0;
            printf("[EMOTE] wheel cancelled (%s)\n", pc_emote_reason_name(r));
        } else if (hold) {
            if (forced_sel >= 0) {
                s_sel = forced_sel;
            } else if (s_pad_opened) {
                float dx = (float)rx, dy = (float)ry;
                if (sqrtf(dx * dx + dy * dy) > (float)dz) { /* latched: only a deliberate push changes the selection */
                    s_sel = pc_emote_pick(dx, dy, 0.0f, PC_EMOTE_SLICES, s_sel > 0 ? s_sel : 0);
                    s_armed = 1;
                    s_center_ms = 0;
                } else if (s_armed) { /* parked in the centre for 300 ms while still holding = nothing */
                    unsigned now = SDL_GetTicks() | 1u;
                    if (s_center_ms == 0) {
                        s_center_ms = now;
                    } else if ((unsigned)(now - s_center_ms) >= 300u) {
                        s_sel = 0;
                        s_armed = 0;
                    }
                }
            } else {
                int w = 640, h = 480;
                if (g_pc_window != NULL) {
                    SDL_GetWindowSize(g_pc_window, &w, &h);
                }
                s_sel = pc_emote_pick((float)(mx - w / 2), (float)(my - h / 2), 0.08f * (float)(w < h ? w : h), PC_EMOTE_SLICES, s_sel);
            }
            *buttons = 0; /* no gameplay action while the wheel is up (the opener included: D-pad Up never reaches the game) */
            *cstick_x = 0;
            *cstick_y = 0;
        } else { /* release: submit once */
            s_open = 0;
            *buttons = 0;
            *cstick_x = 0;
            *cstick_y = 0;
            s_suppress_c = s_pad_opened;
            if (s_sel > 0 && s_sel <= PC_EMOTE_SLICES) {
                s_req = s_sel;
                printf("[EMOTE] wheel submit id=%d (%s)\n", s_sel, pc_emote_def(s_sel)->name);
            } else {
                printf("[EMOTE] wheel closed (no emote)\n");
            }
        }
    }
    if (s_suppress_c && !s_open) {
        if (sqrtf((float)rx * (float)rx + (float)ry * (float)ry) > (float)dz) {
            *cstick_x = 0;
            *cstick_y = 0;
        } else {
            s_suppress_c = 0;
        }
    }
    s_prev_hold = hold;
}

int pc_emote_wheel_is_open(void) {
    return s_open;
}

int pc_emote_wheel_take_request(void) {
    int r = s_req;
    s_req = 0;
    return r;
}

static void emote_rect(struct game_s* game, float x, float y, float w, float h, int r, int g, int b, int a) {
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

void pc_emote_wheel_draw(struct game_s* game) {
    const float cx = 160.0f, cy = 120.0f, R = 78.0f; /* the same uniform 320x240 space as the tool wheel: a slot sits at its pick angle */
    int i;

    if (!s_open || game == NULL || game->graph == NULL) {
        return;
    }
    mFont_SetMatrix(game->graph, mFont_MODE_FONT);
    pc_menu_dim_rect(game->graph, 120);
    emote_rect(game, cx - R - 48.0f, cy - R - 20.0f, 2.0f * (R + 48.0f), 2.0f * (R + 20.0f), 0, 0, 0, 70);
    for (i = 0; i <= PC_EMOTE_SLICES; i++) {
        float ang = (i > 0) ? (6.28318530718f * (float)(i - 1) / (float)PC_EMOTE_SLICES) : 0.0f;
        float x = cx + (i > 0 ? R * sinf(ang) : 0.0f);
        float y = cy - 9.0f - (i > 0 ? R * cosf(ang) : 0.0f);
        char label[40];
        int sel = (i == s_sel);
        int dim = 0;
        float tw, bw;

        if (i == 0) {
            snprintf(label, sizeof(label), "%s", s_sel == 0 ? "Cancel" : "Emotes");
        } else {
            const PCEmoteDef* d = pc_emote_def(i);
            snprintf(label, sizeof(label), "%c %s", (char)d->glyph, d->name);
            dim = pc_emote_player_gate(i) == PC_EMOTE_R_HANDS; /* full-body emote with a tool in hand */
        }
        tw = (float)pc_text_width(label) * (sel ? 1.15f : 1.0f);
        bw = tw + 12.0f;
        if (i > 0) {
            emote_rect(game, (cx + x) * 0.5f - 1.0f, (cy + y) * 0.5f - 1.0f, 2.0f, 2.0f, 255, 255, 255, 60);
        }
        emote_rect(game, x - bw * 0.5f - 2.0f, y - 3.0f, bw + 4.0f, 22.0f, sel ? 255 : 0, sel ? 220 : 0, sel ? 90 : 0, sel ? 235 : 120);
        emote_rect(game, x - bw * 0.5f, y - 1.0f, bw, 18.0f, sel ? 150 : 25, sel ? 105 : 25, sel ? 20 : 40, sel ? 240 : 210);
        if (dim) {
            pc_text_draw(game, label, x - tw * 0.5f, y + 1.0f, 120, 120, 120, 200, sel ? 1.15f : 1.0f);
        } else {
            pc_text_draw(game, label, x - tw * 0.5f, y + 1.0f, sel ? 255 : 215, sel ? 245 : 215, sel ? 190 : 215, 255, sel ? 1.15f : 1.0f);
        }
    }
    if (s_sel > 0 && pc_emote_player_gate(s_sel) == PC_EMOTE_R_HANDS) {
        const char* hint = "Needs empty hands";
        pc_text_draw(game, hint, cx - (float)pc_text_width(hint) * 0.5f, cy + R + 14.0f, 255, 200, 120, 255, 1.0f);
    }
    mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
}

#ifdef PC_NET_TEST_HOOKS
/* ===== TEST-ONLY (PC_NET_TEST_HOOKS build + AC_TEST_HOOKS=1): AC_TEST_EMOTE_SCRIPT=all|watch =====
 * Drives the REAL wheel (a forced hold with a forced selection, then the release = the same submit path as a player) and, for the gates,
 * the real game states. Starts once AC_TEST_EMOTE_GO_FILE exists (when set) and the wheel gate has been open for 4 s. Steps (all):
 *   every emote 1..8 in turn (the first wheel is held open 2.5 s for a screenshot); Exercise is cancelled after 3 s by a 0.4 s stick push;
 *   gates: a second emote within the cooldown, DEMO_WAIT (talk) refuses the wheel, the open chat line refuses it.
 * The inventory / equipment hash is logged at the start and the end. Logs [EMOTE][TEST]. */
#include "pc_test_hooks.h"
#include "pc_keybindings.h"
#include "m_player_lib.h"
#include "m_private.h"
extern int PC_Test_AextRequest(GAME* game, int what, int arg);
extern int pc_chat_typing(void);

static uint32_t pc_emote_test_inv_hash(void) {
    Private_c* priv = Common_Get(now_private);
    const unsigned char* p;
    uint32_t h = 2166136261u;
    size_t i;

    if (priv == NULL) {
        return 0;
    }
    p = (const unsigned char*)&priv->inventory;
    for (i = 0; i < sizeof(priv->inventory); i++) {
        h = (h ^ p[i]) * 16777619u;
    }
    p = (const unsigned char*)&priv->equipment;
    for (i = 0; i < sizeof(priv->equipment); i++) {
        h = (h ^ p[i]) * 16777619u;
    }
    p = (const unsigned char*)&priv->cloth;
    for (i = 0; i < sizeof(priv->cloth); i++) {
        h = (h ^ p[i]) * 16777619u;
    }
    return h;
}

static void pc_emote_test_push_key(SDL_Scancode sc) {
    SDL_Event ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = SDL_KEYDOWN;
    ev.key.state = SDL_PRESSED;
    ev.key.keysym.scancode = sc;
    ev.key.keysym.sym = SDL_GetKeyFromScancode(sc);
    SDL_PushEvent(&ev);
}

static void pc_emote_test_tick(int* hold, int* sel, signed char* stick_x, signed char* stick_y) {
    static const char* s_script = NULL;
    static int s_init = 0, s_step = 0, s_id = 0, s_phase = 0, s_shot = 0;
    static uint32_t s_t = 0, s_ok_since = 0;
    const uint32_t now = SDL_GetTicks();
    GAME* game = (GAME*)gamePT;

    if (!s_init) {
        s_init = 1;
        s_script = pc_test_hook_getenv("AC_TEST_EMOTE_SCRIPT");
    }
    if (s_script == NULL || s_step < 0 || strcmp(s_script, "all") != 0 || game == NULL || gamePT->exec != play_main) {
        return;
    }
#define ET(ms) ((uint32_t)(now - s_t) >= (uint32_t)(ms))
#define ENEXT(fmt, ...) do { s_step++; s_phase = 0; s_t = now; printf("[EMOTE][TEST] " fmt "\n", ##__VA_ARGS__); } while (0)
    if (s_step == 0) {
        const char* go = pc_test_hook_getenv("AC_TEST_EMOTE_GO_FILE");
        if (pc_emote_player_gate(0) != PC_EMOTE_OK) {
            s_ok_since = 0;
            return;
        }
        if (s_ok_since == 0) {
            s_ok_since = now | 1u;
            return;
        }
        if ((uint32_t)(now - s_ok_since) < 4000u) {
            return;
        }
        if (go != NULL) {
            FILE* gf = fopen(go, "rb");
            if (gf == NULL) {
                return;
            }
            fclose(gf);
        }
        s_id = 1;
        ENEXT("script all start inv=%08X", (unsigned)pc_emote_test_inv_hash());
        return;
    }
    if (s_step == 1) { /* play emote s_id through the wheel */
        if (s_phase == 0) {
            *hold = 1;
            *sel = s_id;
            if (ET(s_id == 1 ? 2500u : 300u)) {
                s_phase = 1; /* the next poll releases */
                s_t = now;
                printf("[EMOTE][TEST] release on id=%d (wheel %s)\n", s_id, pc_emote_wheel_is_open() ? "open" : "closed");
            } else if (s_id == 1 && ET(800u) && !s_shot && (s_shot = 1) != 0) {
                printf("[EMOTE][TEST] wheel open for screenshot\n");
            }
            return;
        }
        if (s_phase == 1) {
            if (s_id == PC_EMOTE_EXERCISE && ET(3000u) && !ET(3400u)) {
                *stick_x = 0;
                *stick_y = 60; /* a deliberate movement input: cancels the emote (WAIT -> WALK) */
                return;
            }
            if (ET(400u) && !pc_emote_player_busy() && GET_PLAYER_ACTOR_NOW() != NULL &&
                GET_PLAYER_ACTOR_NOW()->now_main_index == mPlayer_INDEX_WAIT) {
                if (s_ok_since == 0 || !ET(600u)) {
                    s_ok_since = now | 1u;
                }
                if ((uint32_t)(now - s_ok_since) >= 1300u) { /* idle again for 1.3 s (also covers the cooldown) */
                    printf("[EMOTE][TEST] id=%d finished after %u ms inv=%08X\n", s_id, (unsigned)(now - s_t), (unsigned)pc_emote_test_inv_hash());
                    s_ok_since = 0;
                    if (s_id < PC_EMOTE_SLICES) {
                        s_id++;
                        s_phase = 0;
                        s_t = now;
                    } else {
                        ENEXT("emotes done");
                    }
                }
            } else {
                s_ok_since = 0;
            }
            if (ET(20000u)) {
                printf("[EMOTE][TEST] id=%d TIMEOUT (busy=%d)\n", s_id, pc_emote_player_busy());
                s_ok_since = 0;
                if (s_id < PC_EMOTE_SLICES) {
                    s_id++;
                    s_phase = 0;
                    s_t = now;
                } else {
                    ENEXT("emotes done");
                }
            }
        }
        return;
    }
    switch (s_step) {
        case 2: /* cooldown: Surprised, then Pray right after -> refused (cooldown) */
            if (s_phase == 0) { *hold = 1; *sel = PC_EMOTE_SURPRISED; if (ET(200u)) { s_phase = 1; s_t = now; } }
            else if (s_phase == 1) { if (ET(150u)) { s_phase = 2; s_t = now; } }
            else if (s_phase == 2) { *hold = 1; *sel = PC_EMOTE_PRAY; if (ET(200u)) { s_phase = 3; s_t = now; } }
            else if (ET(4000u)) { ENEXT("gate cooldown checked"); }
            break;
        case 3:
            if (ET(1500u)) {
                ENEXT("talk: demo_wait request=%d", mPlib_request_main_demo_wait_type1(game, FALSE, (void*)0x50324131));
            }
            break;
        case 4: /* the wheel must refuse in DEMO_WAIT (a talk / cutscene state) */
            if (s_phase == 0) { if (ET(1000u)) { s_phase = 1; s_t = now; printf("[EMOTE][TEST] gate talk: state %d\n", GET_PLAYER_ACTOR_NOW() != NULL ? GET_PLAYER_ACTOR_NOW()->now_main_index : -1); } }
            else if (s_phase == 1) { *hold = 1; *sel = PC_EMOTE_HOORAY; if (ET(300u)) { s_phase = 2; s_t = now; } }
            else if (ET(500u)) { ENEXT("talk: wait request=%d", PC_Test_AextRequest(game, 7, 0)); }
            break;
        case 5: /* the chat line: open it, the wheel must refuse, close it */
            if (s_phase == 0) { if (ET(2000u)) { pc_emote_test_push_key((SDL_Scancode)g_pc_keybindings.chat); s_phase = 1; s_t = now; } }
            else if (s_phase == 1) { if (ET(600u)) { printf("[EMOTE][TEST] gate chat: typing=%d\n", pc_chat_typing()); s_phase = 2; s_t = now; } }
            else if (s_phase == 2) { *hold = 1; *sel = PC_EMOTE_HOORAY; if (ET(300u)) { s_phase = 3; s_t = now; } }
            else if (s_phase == 3) { if (ET(300u)) { pc_emote_test_push_key(SDL_SCANCODE_ESCAPE); s_phase = 4; s_t = now; } }
            else if (ET(1500u)) { ENEXT("chat: closed typing=%d", pc_chat_typing()); }
            break;
        default:
            if (ET(1000u)) {
                printf("[EMOTE][TEST] script all done inv=%08X state=%d\n", (unsigned)pc_emote_test_inv_hash(),
                       GET_PLAYER_ACTOR_NOW() != NULL ? GET_PLAYER_ACTOR_NOW()->now_main_index : -1);
                s_step = -1;
            }
            break;
    }
#undef ET
#undef ENEXT
}
#endif
