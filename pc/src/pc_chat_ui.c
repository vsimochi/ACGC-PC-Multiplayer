/* pc_chat_ui.c - multiplayer text chat, the GAME side (see pc_chat.h): the chat key (keybindings.ini Chat, default T) opens a one-line entry while the player
 * is in free play, SDL text input fills it, Enter sends it through pc_net_game_chat_send (the host validates and relays it), Escape cancels, Backspace edits.
 * While the line is open the LOCAL pad is zeroed (pc_chat_pad_block in PADRead), nothing else. Every validated line arrives back through pc_chat_on_line and is
 * drawn as a speech bubble above the speaker (the local player or a remote puppet, projected from its world position) and in a fading chat log in the
 * bottom-left corner. Wall-clock timing (SDL_GetTicks), no heap allocation per frame (display lists come from the frame's graph arena like every PC overlay).
 * PRESENTATION ONLY: nothing here touches items, bells, the world or the save. */
#include "pc_platform.h"
#include "pc_chat.h"
#include "pc_keybindings.h"
#include "pc_pause_menu.h"
#include "pc_tool_wheel.h"
#include "pc_typing.h"
#include "pc_text_draw.h"
#include "pc_play_online_menu.h"
#include "pc_host_observer.h"
#include "pc_remote_player.h"
#include "pc_net_game.h"

#include "m_font.h"
#include "m_rcp.h"
#include "graph.h"
#include "main.h"
#include "m_play.h"
#include "m_event.h"
#include "m_demo.h"
#include "m_msg.h"
#include "m_submenu.h"
#include "m_player_lib.h"
#include "m_common_data.h"
#include "m_private.h"

#include <SDL.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* the numeric font tables of pc_chat.c against the game's own constants */
_Static_assert(CHAR_a == 97 && CHAR_z == 122 && CHAR_A == 65 && CHAR_Z == 90 && CHAR_ZERO == 48 && CHAR_SPACE == 32 && CHAR_QUESTIONMARK == 63,
               "chat font table: letters / digits / blank / '?'");
_Static_assert(CHAR_HASHTAG == 209 && CHAR_FORWARD_SLASH == 174 && CHAR_SEMICOLON == 208 && CHAR_PLUS == 180 && CHAR_BACKSLASH == 222 && CHAR_TILDE == 42 &&
                   CHAR_SYMBOL_STAR == 185 && CHAR_BROKEN_BAR == 151 && CHAR_LEFT_APOSTROPHE == 215 && CHAR_UNDERSCORE == 95,
               "chat font table: the symbols that are not ASCII in the font");
_Static_assert(CHAR_ACUTE_e == 124 && CHAR_ACUTE_E == 10 && CHAR_TILDE_n == 135 && CHAR_LOWER_BETA == 29 && CHAR_INVERT_QUESTIONMARK == 1 &&
                   CHAR_DIAERESIS_A == 2 && CHAR_DIARESIS_y == 148 && CHAR_ACUTE_Y == 150 && CHAR_ASH == 162 && CHAR_LOWER_ASH == 163 && CHAR_GRAVE_a == 31 &&
                   CHAR_INTERPUNCT == 133 && CHAR_SYMBOL_HEART == 43 && CHAR_SYMBOL_MUSIC_NOTE == 47 && CHAR_CONTROL_CODE == 127 && CHAR_MESSAGE_TAG == 128 &&
                   CHAR_INVERT_EXCLAMATION == 0,
               "chat font table: Latin-1 letters / symbols / the control codes");

static PCChatStore s_store;
static int         s_store_init = 0;
static int         s_typing = 0;
static int         s_drain = 0;
static int         s_text_was_active = 0;
static uint8_t     s_buf[PC_CHAT_MAX_LEN + 1];
static int         s_len = 0;
static uint32_t    s_open_ms = 0;
static uint32_t    s_swallow = 0; /* the opener key's character: its SDL_TEXTINPUT right after the key-down is not text */

static void chat_init_once(void) {
    if (!s_store_init) {
        pc_chat_store_init(&s_store);
        s_store_init = 1;
    }
}

int pc_chat_typing(void) {
    return s_typing;
}

int pc_chat_can_open(void) {
    GAME_PLAY* play = (GAME_PLAY*)gamePT;
    if (gamePT == NULL || gamePT->exec != play_main || g_pc_paused || pc_tool_wheel_is_open() || pc_host_observer_active() || pc_play_online_menu_text_active()) {
        return 0;
    }
    if (g_pc_typing_mode && g_pc_editor_active) {
        return 0;
    }
    if (mEv_CheckTitleDemo() != mEv_TITLEDEMO_NONE || play->fb_wipe_mode != 0) {
        return 0; /* the title scene (its menus run in a town field) / a scene transition */
    }
    if (play->submenu.process_status != mSM_PROCESS_WAIT || play->submenu.menu_type != mSM_OVL_NONE || play->submenu.current_menu_type != mSM_OVL_NONE ||
        play->submenu.start_refuse) {
        return 0;
    }
    if (mDemo_CheckDemo() != FALSE || !mMsg_Check_MainHide(mMsg_Get_base_window_p())) {
        return 0; /* a cutscene / a conversation / a message window */
    }
    return GET_PLAYER_ACTOR_NOW() != NULL;
}

static void chat_close(int sent) {
    s_typing = 0;
    s_len = 0;
    s_drain = 1; /* the Enter / Escape that closed the line must not reach the game (Enter is Start) */
    if (!s_text_was_active) {
        SDL_StopTextInput();
    }
    printf("[CHAT] input closed (%s)\n", sent ? "sent" : "cancelled");
}

static void chat_open(SDL_Keycode opener) {
    s_typing = 1;
    s_len = 0;
    s_open_ms = SDL_GetTicks();
    s_swallow = (opener >= 0x20 && opener < 0x7F) ? (uint32_t)opener : 0u;
    s_text_was_active = SDL_IsTextInputActive() ? 1 : 0;
    SDL_StartTextInput();
    printf("[CHAT] input open\n");
}

int pc_chat_submit_utf8(const char* utf8) {
    uint8_t codes[PC_CHAT_MAX_LEN], clean[PC_CHAT_MAX_LEN];
    int n = pc_chat_from_utf8(utf8, codes, PC_CHAT_MAX_LEN);
    n = pc_chat_sanitize(codes, n, clean);
    if (n <= 0) {
        return 0;
    }
    return pc_net_game_chat_send(clean, n);
}

static void chat_submit_line(void) {
    uint8_t clean[PC_CHAT_MAX_LEN];
    const int n = pc_chat_sanitize(s_buf, s_len, clean);
    int sent = 0;
    if (n > 0) {
        sent = pc_net_game_chat_send(clean, n);
    }
    chat_close(sent);
}

int pc_chat_handle_event(const SDL_Event* e) {
    chat_init_once();
    if (e->type == SDL_KEYDOWN) {
        const SDL_Keycode k = e->key.keysym.sym;
        const Uint16 mod = e->key.keysym.mod;
        if (s_typing) {
            if (gamePT == NULL || gamePT->exec != play_main) {
                chat_close(0); /* the scene went away under the line */
                return 1;
            }
            if (k == SDLK_ESCAPE && !e->key.repeat) {
                chat_close(0);
            } else if ((k == SDLK_RETURN || k == SDLK_KP_ENTER) && !e->key.repeat) {
                chat_submit_line();
            } else if (k == SDLK_BACKSPACE) {
                s_len = pc_chat_input_backspace(s_len);
            } else if (((mod & KMOD_CTRL) && k == SDLK_v) || ((mod & KMOD_SHIFT) && k == SDLK_INSERT)) {
                if (SDL_HasClipboardText()) {
                    char* clip = SDL_GetClipboardText();
                    if (clip != NULL) {
                        char* q;
                        for (q = clip; *q; q++) {
                            if (*q == '\r' || *q == '\n' || *q == '\t') *q = ' ';
                        }
                        s_len = pc_chat_input_append(s_buf, s_len, PC_CHAT_MAX_LEN, clip);
                        SDL_free(clip);
                    }
                }
            }
            s_swallow = 0;
            return 1; /* every key belongs to the line while it is open (no pause menu, no hotkeys) */
        }
        if (!e->key.repeat && !(g_pc_keybindings.chat & PC_INPUT_MOUSE_BIT) && (int)e->key.keysym.scancode == g_pc_keybindings.chat &&
            !(mod & (KMOD_CTRL | KMOD_ALT | KMOD_GUI))) {
            const char* why = NULL;
            if (pc_net_game_role() == PC_NETGAME_ROLE_NONE || !pc_chat_can_open()) {
                return 0; /* single player / not in free play: the key does whatever else it is bound to */
            }
            if (!pc_net_game_chat_available(&why)) {
                if (why != NULL) {
                    pc_chat_local_notice(why);
                }
                return 1;
            }
            chat_open(k);
            return 1;
        }
        return 0;
    }
    if (e->type == SDL_TEXTINPUT) {
        if (!s_typing) {
            return 0;
        }
        if (s_swallow != 0) {
            const char* t = e->text.text;
            const uint32_t sw = s_swallow;
            s_swallow = 0;
            if (s_len == 0 && t[0] != 0 && t[1] == 0 && ((uint32_t)(unsigned char)t[0] == sw || (uint32_t)(unsigned char)t[0] == (sw ^ 0x20u)) &&
                (uint32_t)(SDL_GetTicks() - s_open_ms) < 500u) {
                return 1; /* the opener's own character */
            }
        }
        s_len = pc_chat_input_append(s_buf, s_len, PC_CHAT_MAX_LEN, e->text.text);
        return 1;
    }
    return 0;
}

int pc_chat_pad_block(void) {
    if (s_typing) {
        if (gamePT == NULL || gamePT->exec != play_main) {
            chat_close(0);
        }
        return 1;
    }
    if (s_drain) {
        const Uint8* keys = SDL_GetKeyboardState(NULL);
        const int ck = (g_pc_keybindings.chat & PC_INPUT_MOUSE_BIT) ? 0 : g_pc_keybindings.chat;
        if (keys[SDL_SCANCODE_RETURN] || keys[SDL_SCANCODE_KP_ENTER] || keys[SDL_SCANCODE_ESCAPE] || (ck > 0 && ck < SDL_NUM_SCANCODES && keys[ck])) {
            return 1;
        }
        s_drain = 0;
    }
    return 0;
}

void pc_chat_on_line(int speaker, int self, uint8_t flags, const uint8_t name[PC_CHAT_NAME_LEN], const uint8_t* text, int len) {
    char u[200], nm[40];
    int b;
    chat_init_once();
    b = pc_chat_store_add(&s_store, speaker, self, flags, name, text, len, SDL_GetTicks());
    pc_chat_to_utf8(text, len, u, (int)sizeof(u));
    pc_chat_name_to_utf8(name, nm, (int)sizeof(nm));
    printf("[CHAT] %s %s%s: %s (speaker %d%s%s)\n", b >= 0 ? "bubble" : "line", (flags & PC_CHAT_F_SERVER) ? "[server] " : (flags & PC_CHAT_F_NOTICE) ? "[notice] " : "",
           (flags & (PC_CHAT_F_SERVER | PC_CHAT_F_NOTICE)) ? "" : nm, u, speaker, self ? ", self" : "", b >= 0 ? "" : ", log only");
}

void pc_chat_local_notice(const char* ascii) {
    uint8_t codes[PC_CHAT_MAX_LEN];
    static const uint8_t none[PC_CHAT_NAME_LEN] = { 0 };
    const int n = pc_chat_from_utf8(ascii, codes, PC_CHAT_MAX_LEN);
    if (n > 0) {
        pc_chat_on_line(-1, 1, PC_CHAT_F_NOTICE, none, codes, n);
    }
}

/* TEST-ONLY (PC_NET_TEST_HOOKS builds + AC_TEST_HOOKS=1, through pc_test_hook_getenv; AC_TEST_CHAT_TYPE=<text>, optional AC_TEST_CHAT_TYPE_DELAY_MS, default 4000):
 * drives the REAL keyboard path with synthetic SDL events: the chat key-down + its own SDL_TEXTINPUT (must be swallowed), the text, then Return after 2.5 s
 * (the line stays open for a screenshot). Called every frame from the network glue; never active in normal play. */
extern const char* pc_test_hook_getenv(const char* name);
void pc_chat_test_type_tick(void) {
    static int stage = 0;
    static uint32_t t = 0, delay = 4000u;
    const uint32_t now = SDL_GetTicks();
    const char* e;
    SDL_Event ev;
    if (stage >= 3 || (e = pc_test_hook_getenv("AC_TEST_CHAT_TYPE")) == NULL || e[0] == '\0') {
        return;
    }
    if (stage == 0) {
        const char* d = pc_test_hook_getenv("AC_TEST_CHAT_TYPE_DELAY_MS");
        if (d != NULL && atoi(d) > 0) {
            delay = (uint32_t)atoi(d);
        }
        if (!pc_net_game_chat_available(NULL) || !pc_chat_can_open() || (g_pc_keybindings.chat & PC_INPUT_MOUSE_BIT)) {
            t = 0;
            return;
        }
        if (t == 0) {
            t = now | 1u;
            return;
        }
        if ((uint32_t)(now - t) < delay) {
            return;
        }
        memset(&ev, 0, sizeof(ev));
        ev.type = SDL_KEYDOWN;
        ev.key.state = SDL_PRESSED;
        ev.key.keysym.scancode = (SDL_Scancode)g_pc_keybindings.chat;
        ev.key.keysym.sym = SDL_GetKeyFromScancode((SDL_Scancode)g_pc_keybindings.chat);
        SDL_PushEvent(&ev);
        if (ev.key.keysym.sym >= 0x20 && ev.key.keysym.sym < 0x7F) {
            memset(&ev, 0, sizeof(ev));
            ev.type = SDL_TEXTINPUT;
            ev.text.text[0] = (char)SDL_GetKeyFromScancode((SDL_Scancode)g_pc_keybindings.chat);
            SDL_PushEvent(&ev); /* what Windows delivers for the opener key: must not land in the line */
        }
        printf("[CHAT][TEST] AC_TEST_CHAT_TYPE: pushed the chat key\n");
        stage = 1;
        t = now;
        return;
    }
    if (stage == 1 && (uint32_t)(now - t) >= 300u) {
        size_t n = strlen(e), off = 0;
        while (off < n) {
            size_t k = n - off > 20 ? 20 : n - off;
            while (k > 1 && off + k < n && (((unsigned char)e[off + k]) & 0xC0) == 0x80) {
                k--; /* never split a UTF-8 sequence */
            }
            memset(&ev, 0, sizeof(ev));
            ev.type = SDL_TEXTINPUT;
            memcpy(ev.text.text, e + off, k);
            SDL_PushEvent(&ev);
            off += k;
        }
        printf("[CHAT][TEST] AC_TEST_CHAT_TYPE: pushed the text (%u bytes)\n", (unsigned)n);
        stage = 2;
        t = now;
        return;
    }
    if (stage == 2 && (uint32_t)(now - t) >= 2500u) {
        printf("[CHAT][TEST] AC_TEST_CHAT_TYPE: the line holds %d code(s) before Return (typing=%d)\n", s_len, s_typing);
        memset(&ev, 0, sizeof(ev));
        ev.type = SDL_KEYDOWN;
        ev.key.state = SDL_PRESSED;
        ev.key.keysym.scancode = SDL_SCANCODE_RETURN;
        ev.key.keysym.sym = SDLK_RETURN;
        SDL_PushEvent(&ev);
        stage = 3;
    }
}

void pc_chat_reset_session(void) {
    chat_init_once();
    pc_chat_store_clear(&s_store);
    if (s_typing) {
        chat_close(0);
    }
}

/* ---------------------------------------------------------------- drawing ---------------------------------------------------------------- */
static float s_vis_l = 0.0f, s_vis_r = 320.0f; /* the visible x range of the font space this frame (chat_visible_x) */

/* A flat translucent rectangle in FONT-space coordinates (x may be < 0 or > 320 on a wide window). Texture rectangles take unsigned coordinates, so the
 * rectangle is drawn in the full-window STRETCH mode (the PC_NOOP_WIDESCREEN_STRETCH tags, as pc_menu_dim_rect does), mapping the visible range onto 0..320. */
static void chat_rect(GAME* game, float x, float y, float w, float h, int r, int g, int b, int a) {
    Gfx* gfx;
    const float k = 320.0f / (s_vis_r - s_vis_l);
    float x0 = (x - s_vis_l) * k, x1 = (x + w - s_vis_l) * k;
    if (w <= 0.0f || h <= 0.0f || a <= 0) {
        return;
    }
    if (x0 < 0.0f) x0 = 0.0f;
    if (x1 > 320.0f) x1 = 320.0f;
    if (y < 0.0f) {
        h += y;
        y = 0.0f;
    }
    if (x1 <= x0 || h <= 0.0f) {
        return;
    }
    OPEN_DISP(game->graph);
    gfx = NOW_FONT_DISP;
    gDPNoOpTag(gfx++, PC_NOOP_WIDESCREEN_STRETCH);
    gDPPipeSync(gfx++);
    gDPSetOtherMode(gfx++, G_AD_DISABLE | G_CD_MAGICSQ | G_CK_NONE | G_TC_FILT | G_TF_POINT | G_TT_NONE | G_TL_TILE | G_TD_CLAMP | G_TP_NONE | G_CYC_1CYCLE | G_PM_NPRIMITIVE,
                    G_AC_NONE | G_ZS_PRIM | G_RM_XLU_SURF | G_RM_XLU_SURF2);
    gDPSetCombineMode(gfx++, G_CC_PRIMITIVE, G_CC_PRIMITIVE);
    gDPSetPrimColor(gfx++, 0, 0, r, g, b, a);
    gfx = gfx_gSPTextureRectangle1(gfx, (int)(x0 * 4.0f), (int)(y * 4.0f), (int)(x1 * 4.0f), (int)((y + h) * 4.0f), 0, 0, 0, 0, 0);
    gDPPipeSync(gfx++);
    gDPNoOpTag(gfx++, PC_NOOP_WIDESCREEN_STRETCH_OFF);
    SET_FONT_DISP(gfx);
    CLOSE_DISP(game->graph);
}

/* a rounded box from non-overlapping strips (translucent strips must not overlap) */
static void chat_round_rect(GAME* game, float x, float y, float w, float h, int r, int g, int b, int a) {
    if (w < 6.0f || h < 6.0f) {
        chat_rect(game, x, y, w, h, r, g, b, a);
        return;
    }
    chat_rect(game, x + 2.0f, y, w - 4.0f, 1.0f, r, g, b, a);
    chat_rect(game, x + 1.0f, y + 1.0f, w - 2.0f, 1.0f, r, g, b, a);
    chat_rect(game, x, y + 2.0f, w, h - 4.0f, r, g, b, a);
    chat_rect(game, x + 1.0f, y + h - 2.0f, w - 2.0f, 1.0f, r, g, b, a);
    chat_rect(game, x + 2.0f, y + h - 1.0f, w - 4.0f, 1.0f, r, g, b, a);
}

/* the visible x range of the 320-wide font space: the PC port widens the view (hor+), the font ortho is scaled by the same factor */
static void chat_visible_x(float* l, float* r) {
    float half = 160.0f;
    if (g_pc_window_w > 0 && g_pc_window_h > 0) {
        const float win = (float)g_pc_window_w / (float)g_pc_window_h;
        const float gc = 4.0f / 3.0f;
        if (win > gc + 0.01f) {
            half = 160.0f * win / gc;
        }
    }
    *l = 160.0f - half;
    *r = 160.0f + half;
}

static int chat_project(GAME_PLAY* play, float x, float y, float z, float* sx, float* sy) {
    const MtxF* m = &play->projection_matrix;
    const float px = m->xx * x + m->xy * y + m->xz * z + m->xw;
    const float py = m->yx * x + m->yy * y + m->yz * z + m->yw;
    const float w = m->wx * x + m->wy * y + m->wz * z + m->ww;
    if (w < 1.0f) {
        return 0; /* behind the camera */
    }
    *sx = (SCREEN_WIDTH_F / 2.0f) + (px / w) * (SCREEN_WIDTH_F / 2.0f);
    *sy = (SCREEN_HEIGHT_F / 2.0f) - (py / w) * (SCREEN_HEIGHT_F / 2.0f);
    return 1;
}

#define CHAT_HEAD_Y   62.0f /* world units above the feet: the bubble's tail sits just over the head */
#define CHAT_TXT_S    0.75f
#define CHAT_LINE_H   12.0f

static int chat_name_codes(const uint8_t name[PC_CHAT_NAME_LEN], char* out) {
    int n = PC_CHAT_NAME_LEN, i;
    while (n > 0 && (name[n - 1] == 32 || name[n - 1] == 0)) {
        n--;
    }
    for (i = 0; i < n; i++) {
        out[i] = (char)(pc_chat_code_ok(name[i]) ? name[i] : 63);
    }
    out[n] = 0;
    return n;
}

/* one bubble whose bottom (tail tip) is at (cx, tip_y); returns its top y. draw = 0 only measures (*bx0 / *bx1 = the box's x range). */
static float chat_draw_bubble(GAME* game, const PCChatEntry* e, float cx, float tip_y, int tail, float vl, float vr, uint32_t now, int draw, float* bx0, float* bx1) {
    int st[PC_CHAT_BUBBLE_LINES], ln[PC_CHAT_BUBBLE_LINES], tr = 0, lines, i;
    char row[PC_CHAT_BUBBLE_LINES][PC_CHAT_BUBBLE_COLS + 4];
    char nm[PC_CHAT_NAME_LEN + 1];
    float w = 0.0f, box_w, box_h, x0, y0, head_h;
    const int alpha = pc_chat_fade_alpha((uint32_t)(now - e->t0), e->life, PC_CHAT_FADE_MS);
    int nr, ng, nb;
    if (alpha <= 0) {
        return tip_y;
    }
    lines = pc_chat_wrap(e->text, e->len, PC_CHAT_BUBBLE_COLS, PC_CHAT_BUBBLE_LINES, st, ln, &tr);
    if (lines <= 0) {
        return tip_y;
    }
    for (i = 0; i < lines; i++) {
        float lw;
        memcpy(row[i], e->text + st[i], (size_t)ln[i]);
        row[i][ln[i]] = 0;
        if (tr && i == lines - 1) {
            row[i][ln[i]] = 46;
            row[i][ln[i] + 1] = 46;
            row[i][ln[i] + 2] = 0;
        }
        lw = (float)pc_text_width(row[i]) * CHAT_TXT_S;
        if (lw > w) w = lw;
    }
    head_h = 0.0f;
    nm[0] = 0;
    if (!e->self && chat_name_codes(e->name, nm) > 0) {
        const float nw = (float)pc_text_width(nm) * 0.62f;
        head_h = 10.0f;
        if (nw > w) w = nw;
    }
    box_w = w + 10.0f;
    box_h = (float)lines * CHAT_LINE_H + head_h + 5.0f;
    y0 = tip_y - (tail ? 5.0f : 2.0f) - box_h;
    x0 = cx - box_w * 0.5f;
    if (x0 < vl + 2.0f) x0 = vl + 2.0f;
    if (x0 + box_w > vr - 2.0f) x0 = vr - 2.0f - box_w;
    if (bx0 != NULL) *bx0 = x0 - 1.0f;
    if (bx1 != NULL) *bx1 = x0 + box_w + 1.0f;
    if (!draw) {
        return y0 - 2.0f;
    }
    chat_round_rect(game, x0 - 1.0f, y0 - 1.0f, box_w + 2.0f, box_h + 2.0f, 60, 45, 30, alpha * 3 / 4);
    chat_round_rect(game, x0, y0, box_w, box_h, 255, 251, 236, alpha);
    if (tail) {
        float tx = cx;
        if (tx < x0 + 6.0f) tx = x0 + 6.0f;
        if (tx > x0 + box_w - 6.0f) tx = x0 + box_w - 6.0f;
        chat_rect(game, tx - 4.0f, y0 + box_h, 8.0f, 2.0f, 255, 251, 236, alpha);
        chat_rect(game, tx - 2.5f, y0 + box_h + 2.0f, 5.0f, 2.0f, 255, 251, 236, alpha);
        chat_rect(game, tx - 1.0f, y0 + box_h + 4.0f, 2.0f, 1.0f, 255, 251, 236, alpha);
    }
    if (head_h > 0.0f) {
        pc_chat_speaker_color(e->name, &nr, &ng, &nb);
        pc_text_draw(game, nm, x0 + 5.0f, y0 + 1.5f, nr * 3 / 5, ng * 3 / 5, nb * 3 / 5, alpha, 0.62f);
    }
    for (i = 0; i < lines; i++) {
        pc_text_draw(game, row[i], x0 + 5.0f, y0 + 2.5f + head_h + (float)i * CHAT_LINE_H, 70, 52, 36, alpha, CHAT_TXT_S);
    }
    return y0 - 2.0f;
}

static int chat_speaker_anchor(GAME_PLAY* play, const PCChatEntry* e, float* x, float* y, float* z) {
    if (e->self) {
        PLAYER_ACTOR* pl = GET_PLAYER_ACTOR_NOW();
        if (pl == NULL) return 0;
        *x = pl->actor_class.world.position.x;
        *y = pl->actor_class.world.position.y;
        *z = pl->actor_class.world.position.z;
        return 1;
    }
    (void)play;
    return pc_remote_player_get_draw_position((PCNetPlayerId)e->speaker, x, y, z);
}

typedef struct ChatStack {
    const PCChatEntry* b[PC_CHAT_BUBBLES_PER];
    int   n;
    float sx, sy, x0, x1, top, wx, wy, wz, shift;
} ChatStack;

static void chat_draw_bubbles(GAME* game, float vl, float vr, uint32_t now) {
    GAME_PLAY* play = (GAME_PLAY*)game;
    static ChatStack st[PC_CHAT_BUBBLES]; /* static: no per-frame allocation */
    int i, j, k, n = 0;
    if (game != gamePT || game->exec != play_main || play->submenu.process_status != mSM_PROCESS_WAIT || mEv_CheckTitleDemo() != mEv_TITLEDEMO_NONE) {
        return; /* only over the live field / room, not over a submenu or the title */
    }
    /* 1. one stack per speaker on screen (newest bubble on the head, the previous one above it), measured */
    for (i = 0; i < PC_CHAT_BUBBLES; i++) {
        const PCChatEntry* e = &s_store.bubble[i];
        ChatStack* s;
        float top, a, b;
        if (!e->used || pc_chat_store_bubble_of(&s_store, e->speaker, 0) != e) continue;
        s = &st[n];
        if (!chat_speaker_anchor(play, e, &s->wx, &s->wy, &s->wz) || !chat_project(play, s->wx, s->wy + CHAT_HEAD_Y, s->wz, &s->sx, &s->sy)) continue;
        if (s->sx < vl - 40.0f || s->sx > vr + 40.0f || s->sy < -20.0f || s->sy > SCREEN_HEIGHT_F + 10.0f) continue; /* off screen */
        s->n = 0;
        s->x0 = 1e9f;
        s->x1 = -1e9f;
        top = s->sy;
        for (k = 0; k < PC_CHAT_BUBBLES_PER; k++) {
            const PCChatEntry* bb = pc_chat_store_bubble_of(&s_store, e->speaker, k);
            if (bb == NULL) break;
            s->b[s->n++] = bb;
            a = b = s->sx; /* a faded-out bubble measures as nothing */
            top = chat_draw_bubble(game, bb, s->sx, top, k == 0, vl, vr, now, 0, &a, &b);
            if (a < s->x0) s->x0 = a;
            if (b > s->x1) s->x1 = b;
        }
        s->top = top;
        s->shift = 0.0f;
        n++;
    }
    /* 2. the stack nearest the bottom of the screen keeps its place; one that would overlap a placed stack moves up above it */
    for (i = 1; i < n; i++) {
        ChatStack t = st[i];
        for (j = i - 1; j >= 0 && st[j].sy < t.sy; j--) st[j + 1] = st[j];
        st[j + 1] = t;
    }
    for (i = 1; i < n; i++) {
        int moved = 1, guard = 0;
        while (moved && guard++ < PC_CHAT_BUBBLES) {
            moved = 0;
            for (j = 0; j < i; j++) {
                const float itop = st[i].top - st[i].shift, ibot = st[i].sy - st[i].shift;
                const float jtop = st[j].top - st[j].shift, jbot = st[j].sy - st[j].shift;
                if (st[i].x0 < st[j].x1 + 2.0f && st[j].x0 < st[i].x1 + 2.0f && itop < jbot && jtop < ibot) {
                    st[i].shift += ibot - jtop + 2.0f;
                    moved = 1;
                }
            }
        }
    }
    /* 3. draw (each tail still points down at its speaker) */
    for (i = 0; i < n; i++) {
        float top = st[i].sy - st[i].shift;
        for (k = 0; k < st[i].n; k++) {
            PCChatEntry* b = (PCChatEntry*)st[i].b[k];
            top = chat_draw_bubble(game, b, st[i].sx, top, k == 0, vl, vr, now, 1, NULL, NULL);
            if (!b->drawn_logged) {
                b->drawn_logged = 1;
                printf("[CHAT] bubble drawn speaker=%d self=%d stack=%d at (%.0f,%.0f) world (%.0f,%.0f,%.0f)%s\n", b->speaker, (int)b->self, k, (double)st[i].sx, (double)top,
                       (double)st[i].wx, (double)st[i].wy, (double)st[i].wz, st[i].shift > 0.0f ? " (moved up: overlap)" : "");
            }
        }
    }
}

static void chat_draw_log(GAME* game, float vl, float vr, uint32_t now) {
    float y = SCREEN_HEIGHT_F - 34.0f;
    const float x = vl + 6.0f;
    int i;
    for (i = 0; i < PC_CHAT_LOG_SHOWN; i++) {
        const PCChatEntry* e = pc_chat_store_log(&s_store, i);
        int st[2], ln[2], tr = 0, rows, r, alpha, cr, cg, cb;
        char nm[PC_CHAT_NAME_LEN + 4];
        float nw = 0.0f;
        if (e == NULL) break;
        alpha = s_typing ? 255 : pc_chat_fade_alpha((uint32_t)(now - e->t0), PC_CHAT_LOG_SHOW_MS, PC_CHAT_FADE_MS);
        if (alpha <= 0) break; /* older lines are older still */
        nm[0] = 0;
        if (e->flags & PC_CHAT_F_SERVER) {
            static const char srv[] = { 'S', 'e', 'r', 'v', 'e', 'r', ':', ' ', 0 };
            memcpy(nm, srv, sizeof(srv));
            cr = 255, cg = 205, cb = 80;
        } else if (e->flags & PC_CHAT_F_NOTICE) {
            cr = 190, cg = 190, cb = 190;
        } else {
            const int n = chat_name_codes(e->name, nm);
            nm[n] = 58;
            nm[n + 1] = 32;
            nm[n + 2] = 0;
            pc_chat_speaker_color(e->name, &cr, &cg, &cb);
        }
        if (nm[0]) nw = (float)pc_text_width(nm) * 0.7f;
        rows = pc_chat_wrap(e->text, e->len, 44, 2, st, ln, &tr);
        for (r = rows - 1; r >= 0; r--) {
            char row[52];
            const float rx = (r == 0) ? x + nw : x + 8.0f;
            float rw;
            memcpy(row, e->text + st[r], (size_t)ln[r]);
            row[ln[r]] = 0;
            if (tr && r == rows - 1) {
                row[ln[r]] = 46;
                row[ln[r] + 1] = 46;
                row[ln[r] + 2] = 0;
            }
            rw = (float)pc_text_width(row) * 0.7f + (rx - x);
            if (rw > vr - vl - 16.0f) rw = vr - vl - 16.0f;
            chat_rect(game, x - 3.0f, y - 1.0f, rw + 6.0f, 12.0f, 0, 0, 0, alpha * 45 / 100);
            if (r == 0 && nm[0]) {
                pc_text_draw(game, nm, x, y, cr, cg, cb, alpha, 0.7f);
            }
            pc_text_draw(game, row, rx, y, (e->flags & PC_CHAT_F_NOTICE) ? cr : (e->flags & PC_CHAT_F_SERVER) ? 255 : 245,
                         (e->flags & PC_CHAT_F_NOTICE) ? cg : (e->flags & PC_CHAT_F_SERVER) ? 235 : 245, (e->flags & PC_CHAT_F_NOTICE) ? cb : (e->flags & PC_CHAT_F_SERVER) ? 170 : 245,
                         alpha, 0.7f);
            y -= 12.0f;
        }
    }
}

static void chat_draw_input(GAME* game, float vl, float vr, uint32_t now) {
    static const char say[] = { 'S', 'a', 'y', ':', 0 };
    char line[PC_CHAT_MAX_LEN + 2];
    char cnt[12];
    const float y = SCREEN_HEIGHT_F - 19.0f;
    const float x = vl + 6.0f;
    const float sw = (float)pc_text_width(say) * 0.75f + 4.0f;
    const float avail = (vr - vl) - 12.0f - sw - 30.0f;
    int start = 0, n;
    chat_rect(game, x - 3.0f, y - 2.0f, (vr - vl) - 6.0f, 15.0f, 0, 0, 0, 175);
    pc_text_draw(game, say, x, y, 255, 220, 110, 255, 0.75f);
    for (;;) { /* show the tail of a line wider than the bar */
        n = s_len - start;
        memcpy(line, s_buf + start, (size_t)n);
        line[n] = ((now - s_open_ms) / 500u) % 2u == 0u ? (char)95 : (char)32; /* the blinking cursor */
        line[n + 1] = 0;
        if (n <= 0 || (float)pc_text_width(line) * 0.75f <= avail) break;
        start++;
    }
    pc_text_draw(game, line, x + sw, y, 255, 255, 255, 255, 0.75f);
    {
        char* slash;
        snprintf(cnt, sizeof(cnt), "%d/%d", s_len, PC_CHAT_MAX_LEN);
        if ((slash = strchr(cnt, '/')) != NULL) {
            *slash = (char)174; /* the font's '/' (0x2F is the music note there) */
        }
        pc_text_draw(game, cnt, vr - 8.0f - (float)pc_text_width(cnt) * 0.6f, y + 1.5f, 170, 170, 170, 255, 0.6f);
    }
}

void pc_chat_draw(GAME* game) {
    const uint32_t now = SDL_GetTicks();
    float vl, vr;
    int any = s_typing, i;
    chat_init_once();
    pc_chat_store_expire(&s_store, now);
    if (game == NULL || game->graph == NULL || g_pc_paused) {
        return;
    }
    for (i = 0; !any && i < PC_CHAT_BUBBLES; i++) any = s_store.bubble[i].used;
    if (!any) {
        const PCChatEntry* e = pc_chat_store_log(&s_store, 0);
        any = e != NULL && (uint32_t)(now - e->t0) < PC_CHAT_LOG_SHOW_MS;
    }
    if (!any) {
        return;
    }
    chat_visible_x(&vl, &vr);
    s_vis_l = vl;
    s_vis_r = vr;
    mFont_SetMatrix(game->graph, mFont_MODE_FONT);
    chat_draw_bubbles(game, vl, vr, now);
    chat_draw_log(game, vl, vr, now);
    if (s_typing) {
        chat_draw_input(game, vl, vr, now);
    }
    mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);
}
