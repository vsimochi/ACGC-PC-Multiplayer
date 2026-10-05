/* pc_play_online_menu.c - see pc_play_online_menu.h. Modelled on pc_settings_menu.c (state + nav/confirm/cancel/draw) and on its keybinding capture (raw SDL events
 * routed by pc_main). Never blocks: every operation is a small file read/write or one CreateProcess. */
#include "pc_play_online_menu.h"
#include "pc_character.h"
#include "pc_guest_profile.h"
#include "pc_menu_util.h"
#include "pc_net_game.h"
#include "pc_relaunch.h"
#include "pc_servers.h"

#include "m_font.h"
#include "m_rcp.h"
#include "graph.h"
#include "main.h" /* SCREEN_WIDTH_F */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { PG_SERVERS, PG_ACTIONS, PG_CHARS, PG_DELETE };
enum { T_NONE, T_SRV_NAME, T_SRV_ADDR, T_SRV_PORT, T_CHAR_NAME };

#define VISIBLE 8

static int s_active = 0;
static int s_page = PG_SERVERS;
static int s_sel = 0, s_scroll = 0;
static int s_launched = 0;

static PCServer s_srv[PC_SERVER_MAX];
static int s_nsrv = 0;
static int s_srv_state = PC_SERVERS_OK; /* PC_SERVERS_CORRUPT: list unusable, writes refused */
static int s_cur = -1;                  /* chosen server index */

static PCCharacter s_chr[PC_CHARACTER_MAX];
static int s_nchr = 0;

static char s_msg[300];
static int s_msg_err = 0;
static int s_del_sel = 0; /* 0 keep, 1 delete */

/* text entry */
static int s_text = T_NONE;
static int s_text_grace = 0;
static char s_buf[40];
static int s_buf_len = 0;
static int s_edit_idx = -1; /* server being edited, -1 = add */
static PCServer s_draft;

/* ---------------- helpers ---------------- */

static void set_msg(int err, const char* fmt, const char* a) {
    snprintf(s_msg, sizeof(s_msg), fmt, a != NULL ? a : "");
    s_msg_err = err;
}

/* the same drawable subset as pc_settings_menu.c glyph_ok */
static int glyph_ok(unsigned char c) {
    if (c >= '0' && c <= '9') return 1;
    if (c >= '@' && c <= 'Z') return 1;
    if (c >= 'a' && c <= 'z') return 1;
    switch (c) {
        case ' ': case '!': case '"': case '%': case '&': case '\'':
        case '(': case ')': case ',': case '-': case '.': case ':':
        case '<': case '=': case '>': case '?': case '_':
            return 1;
    }
    return 0;
}

static void sanitize(char* s) {
    for (; *s; s++) {
        if (!glyph_ok((unsigned char)*s)) *s = '?';
    }
}

int pc_play_online_menu_available(void) {
    return pc_net_game_role() == PC_NETGAME_ROLE_NONE;
}

static void reload_servers(void) {
    char err[300];
    s_nsrv = 0;
    s_srv_state = pc_servers_load(NULL, s_srv, PC_SERVER_MAX, &s_nsrv, err, sizeof(err));
    if (s_srv_state != PC_SERVERS_OK) {
        s_nsrv = 0;
        set_msg(1, "servers.ini: %s", err);
    }
}

static void reload_characters(void) {
    int skipped = 0;
    s_nchr = pc_character_list(NULL, s_chr, PC_CHARACTER_MAX, &skipped);
    if (s_nchr < 0) s_nchr = 0;
}

static int row_count(void) {
    switch (s_page) {
        case PG_SERVERS: return s_nsrv + 2;
        case PG_ACTIONS: return 4;
        case PG_CHARS:   return s_nchr + 2;
    }
    return 2;
}

static void sel_clamp(void) {
    const int n = row_count();
    if (s_sel < 0) s_sel = 0;
    if (s_sel > n - 1) s_sel = n - 1;
    if (s_sel < s_scroll) s_scroll = s_sel;
    if (s_sel >= s_scroll + VISIBLE) s_scroll = s_sel - VISIBLE + 1;
    if (s_scroll < 0) s_scroll = 0;
}

static void goto_page(int page, int sel) {
    s_page = page;
    s_sel = sel;
    s_scroll = 0;
    sel_clamp();
}

/* ---------------- text entry ---------------- */

static void text_begin(int step, const char* initial) {
    s_text = step;
    snprintf(s_buf, sizeof(s_buf), "%s", initial != NULL ? initial : "");
    s_buf_len = (int)strlen(s_buf);
    SDL_StartTextInput();
}

static void text_end(void) {
    s_text = T_NONE;
    s_text_grace = 15;
    SDL_StopTextInput();
}

static int text_max(void) {
    switch (s_text) {
        case T_SRV_NAME: return PC_SERVER_NAME_MAX;
        case T_SRV_ADDR: return 15;
        case T_SRV_PORT: return 5;
        case T_CHAR_NAME: return 16;
    }
    return 0;
}

static int text_char_ok(unsigned char c) {
    switch (s_text) {
        case T_SRV_NAME: return glyph_ok(c) && strchr("[]=\"\\", c) == NULL;
        case T_SRV_ADDR: return (c >= '0' && c <= '9') || c == '.';
        case T_SRV_PORT: return c >= '0' && c <= '9';
        case T_CHAR_NAME: return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '-';
    }
    return 0;
}

static void do_connect(const PCServer* s, int kind, const char* name, const char* display) {
    char err[200];
    if (s_launched) return;
    (void)pc_servers_set_last(NULL, s->name, display, NULL); /* UI hint only; failure is irrelevant */
    err[0] = '\0';
    if (pc_relaunch_connect(s->address, s->port, kind, name, err, sizeof(err))) {
        s_launched = 1;
        set_msg(0, "Starting the client for %s ...", s->name);
        g_pc_running = 0; /* quit THIS process cleanly; the new one is already running */
    } else {
        set_msg(1, "Could not start the client: %s", err);
    }
}

static void text_commit(void) {
    char why[200];
    why[0] = '\0';
    while (s_buf_len > 0 && s_buf[s_buf_len - 1] == ' ') s_buf[--s_buf_len] = '\0';
    switch (s_text) {
        case T_SRV_NAME: {
            int j;
            if (!pc_servers_name_check(s_buf, why, sizeof(why))) {
                set_msg(1, "%s", why);
                return;
            }
            j = pc_servers_find(s_srv, s_nsrv, s_buf);
            if (j >= 0 && j != s_edit_idx) {
                set_msg(1, "%s", "a server with that name already exists");
                return;
            }
            snprintf(s_draft.name, sizeof(s_draft.name), "%s", s_buf);
            set_msg(0, "%s", "");
            text_begin(T_SRV_ADDR, s_draft.address);
            return;
        }
        case T_SRV_ADDR:
            if (!pc_servers_address_check(s_buf, why, sizeof(why))) {
                set_msg(1, "%s", why);
                return;
            }
            snprintf(s_draft.address, sizeof(s_draft.address), "%s", s_buf);
            set_msg(0, "%s", "");
            {
                char pb[8];
                snprintf(pb, sizeof(pb), "%d", s_draft.port > 0 ? s_draft.port : PC_SERVER_DEFAULT_PORT);
                text_begin(T_SRV_PORT, pb);
            }
            return;
        case T_SRV_PORT: {
            long p = PC_SERVER_DEFAULT_PORT;
            int r;
            if (s_buf_len > 0) p = strtol(s_buf, NULL, 10);
            if (!pc_servers_port_check(p)) {
                set_msg(1, "%s", "port must be 1..65535");
                return;
            }
            s_draft.port = (int)p;
            if (s_edit_idx >= 0) {
                r = pc_servers_update(NULL, s_srv[s_edit_idx].name, &s_draft, why, sizeof(why));
            } else {
                r = pc_servers_add(NULL, &s_draft, why, sizeof(why));
            }
            text_end();
            reload_servers();
            if (r != PC_SERVERS_OK) {
                set_msg(1, "Not saved: %s", why);
            } else {
                set_msg(0, "Saved server %s", s_draft.name);
            }
            goto_page(PG_SERVERS, 0);
            return;
        }
        case T_CHAR_NAME:
            if (!pc_guest_profile_name_check(s_buf, why, sizeof(why))) {
                set_msg(1, "%s", why);
                return;
            }
            {
                char nm[40];
                snprintf(nm, sizeof(nm), "%s", s_buf);
                text_end();
                if (s_cur >= 0 && s_cur < s_nsrv) {
                    do_connect(&s_srv[s_cur], PC_RELAUNCH_CHARACTER, nm, nm);
                }
            }
            return;
    }
}

static void text_cancel(void) {
    text_end();
    set_msg(0, "%s", "");
}

int pc_play_online_menu_text_active(void) {
    return s_active && s_text != T_NONE;
}

int pc_play_online_menu_text_blocking(void) {
    return s_active && (s_text != T_NONE || s_text_grace > 0);
}

int pc_play_online_menu_handle_text_event(const SDL_Event* e) {
    if (!pc_play_online_menu_text_active()) return 0;
    switch (e->type) {
        case SDL_TEXTINPUT: {
            const char* p;
            for (p = e->text.text; *p; p++) {
                const unsigned char c = (unsigned char)*p;
                if (s_buf_len < text_max() && s_buf_len < (int)sizeof(s_buf) - 1 && text_char_ok(c)) {
                    s_buf[s_buf_len++] = (char)c;
                    s_buf[s_buf_len] = '\0';
                }
            }
            return 1;
        }
        case SDL_KEYDOWN:
            switch (e->key.keysym.sym) {
                case SDLK_BACKSPACE:
                    if (s_buf_len > 0) s_buf[--s_buf_len] = '\0';
                    break;
                case SDLK_RETURN:
                case SDLK_KP_ENTER:
                    if (!e->key.repeat) text_commit();
                    break;
                case SDLK_ESCAPE:
                    if (!e->key.repeat) text_cancel();
                    break;
                default: break;
            }
            return 1;
        case SDL_CONTROLLERBUTTONDOWN:
            if (e->cbutton.button == SDL_CONTROLLER_BUTTON_A) text_commit();
            else if (e->cbutton.button == SDL_CONTROLLER_BUTTON_B) text_cancel();
            return 1;
    }
    return 1;
}

/* ---------------- menu driver ---------------- */

void pc_play_online_menu_enter(void) {
    s_active = 1;
    s_launched = 0;
    s_text = T_NONE;
    s_text_grace = 0;
    s_msg[0] = '\0';
    s_cur = -1;
    reload_servers();
    goto_page(PG_SERVERS, 0);
}

int pc_play_online_menu_active(void) {
    return s_active;
}

int pc_play_online_menu_nav_up(void) {
    if (s_text != T_NONE || s_page == PG_DELETE) return 1;
    s_sel--;
    sel_clamp();
    return 1;
}

int pc_play_online_menu_nav_down(void) {
    if (s_text != T_NONE || s_page == PG_DELETE) return 1;
    s_sel++;
    sel_clamp();
    return 1;
}

int pc_play_online_menu_nav_left(void) {
    if (s_page == PG_DELETE) s_del_sel = 0;
    return 1;
}

int pc_play_online_menu_nav_right(void) {
    if (s_page == PG_DELETE) s_del_sel = 1;
    return 1;
}

static void char_connect(int idx) {
    const PCCharacter* c = &s_chr[idx];
    if (s_cur < 0 || s_cur >= s_nsrv) return;
    if (c->storage == PC_CHARACTER_STORAGE_STORE) {
        do_connect(&s_srv[s_cur], PC_RELAUNCH_CHARACTER, c->uuid, c->name);
    } else if (c->legacy_profile[0] != '\0') {
        do_connect(&s_srv[s_cur], PC_RELAUNCH_PROFILE, c->legacy_profile, c->name);
    } else {
        do_connect(&s_srv[s_cur], PC_RELAUNCH_DEFAULT_GUEST, NULL, c->name);
    }
}

int pc_play_online_menu_confirm(void) {
    if (s_text != T_NONE || s_launched) return 1;
    s_msg[0] = '\0';
    switch (s_page) {
        case PG_SERVERS:
            if (s_sel < s_nsrv) {
                s_cur = s_sel;
                goto_page(PG_ACTIONS, 0);
            } else if (s_sel == s_nsrv) { /* Add server */
                if (s_srv_state != PC_SERVERS_OK) {
                    set_msg(1, "%s", "servers.ini is unreadable and is never overwritten; fix or remove it by hand");
                } else if (s_nsrv >= PC_SERVER_MAX) {
                    set_msg(1, "%s", "too many saved servers (max 32)");
                } else {
                    memset(&s_draft, 0, sizeof(s_draft));
                    s_draft.port = PC_SERVER_DEFAULT_PORT;
                    s_edit_idx = -1;
                    text_begin(T_SRV_NAME, "");
                }
            } else {
                s_active = 0; /* Back */
                return 0;
            }
            return 1;
        case PG_ACTIONS:
            if (s_sel == 0) { /* Connect */
                reload_characters();
                goto_page(PG_CHARS, 0);
                {
                    int i;
                    for (i = 0; i < s_nchr; i++) {
                        if (strcmp(s_chr[i].name, s_srv[s_cur].last_character) == 0) {
                            goto_page(PG_CHARS, i);
                            break;
                        }
                    }
                }
            } else if (s_sel == 1) { /* Edit */
                s_draft = s_srv[s_cur];
                s_edit_idx = s_cur;
                text_begin(T_SRV_NAME, s_draft.name);
            } else if (s_sel == 2) { /* Delete (confirm page) */
                s_del_sel = 0;
                s_page = PG_DELETE;
            } else {
                goto_page(PG_SERVERS, s_cur);
            }
            return 1;
        case PG_CHARS:
            if (s_sel < s_nchr) {
                char_connect(s_sel);
            } else if (s_sel == s_nchr) { /* New character */
                text_begin(T_CHAR_NAME, "");
            } else {
                goto_page(PG_ACTIONS, 0);
            }
            return 1;
        case PG_DELETE:
            if (s_del_sel == 1) {
                char err[200];
                const int r = pc_servers_delete(NULL, s_srv[s_cur].name, err, sizeof(err));
                if (r == PC_SERVERS_OK) {
                    set_msg(0, "Deleted server %s", s_srv[s_cur].name);
                } else {
                    set_msg(1, "Not deleted: %s", err);
                }
                reload_servers();
                goto_page(PG_SERVERS, 0);
            } else {
                goto_page(PG_ACTIONS, 2);
            }
            return 1;
    }
    return 1;
}

int pc_play_online_menu_cancel(void) {
    if (s_text != T_NONE || s_launched) return 1;
    s_msg[0] = '\0';
    switch (s_page) {
        case PG_SERVERS:
            s_active = 0;
            return 0;
        case PG_ACTIONS:
            goto_page(PG_SERVERS, s_cur);
            return 1;
        case PG_CHARS:
            goto_page(PG_ACTIONS, 0);
            return 1;
        case PG_DELETE:
            goto_page(PG_ACTIONS, 2);
            return 1;
    }
    return 1;
}

void pc_play_online_menu_tick(void) {
    if (s_text_grace > 0) s_text_grace--;
}

/* ---------------- drawing ---------------- */

static void row_label(int i, char* out, size_t cap) {
    out[0] = '\0';
    switch (s_page) {
        case PG_SERVERS:
            if (i < s_nsrv) snprintf(out, cap, "%s  %s:%d", s_srv[i].name, s_srv[i].address, s_srv[i].port);
            else if (i == s_nsrv) snprintf(out, cap, "Add server");
            else snprintf(out, cap, "Back");
            break;
        case PG_ACTIONS: {
            static const char* const k[4] = { "Connect", "Edit", "Delete", "Back" };
            snprintf(out, cap, "%s", k[i & 3]);
            break;
        }
        case PG_CHARS:
            if (i < s_nchr) {
                int dup = 0, j;
                for (j = 0; j < s_nchr; j++) {
                    if (j != i && strcmp(s_chr[j].name, s_chr[i].name) == 0) dup = 1;
                }
                if (s_chr[i].storage == PC_CHARACTER_STORAGE_STORE) {
                    if (dup) snprintf(out, cap, "%s (%.4s)", s_chr[i].name, s_chr[i].uuid);
                    else snprintf(out, cap, "%s", s_chr[i].name);
                } else {
                    snprintf(out, cap, "%s (profile)", s_chr[i].name);
                }
            } else if (i == s_nchr) {
                snprintf(out, cap, "New character");
            } else {
                snprintf(out, cap, "Back");
            }
            break;
    }
    sanitize(out);
}

static void draw_list(struct game_s* game, const char* title, const char* hint) {
    const int n = row_count();
    int i, r, g, b, a;
    pc_menu_draw_centered(game, title, 30.0f, 255, 255, 255, 255, 1.0f);
    for (i = 0; i < VISIBLE && s_scroll + i < n; i++) {
        const int row = s_scroll + i;
        const int selected = (row == s_sel);
        char buf[96];
        row_label(row, buf, sizeof(buf));
        pc_menu_row_colors(selected, &r, &g, &b, &a);
        pc_menu_draw_centered(game, buf, 55.0f + (f32)i * 15.0f, r, g, b, a, selected ? PC_MENU_SCALE_SELECTED : 1.0f);
    }
    if (s_scroll > 0) pc_menu_draw_left(game, "...", 14.0f, 55.0f, 180, 180, 180, 200, 1.0f);
    if (s_scroll + VISIBLE < n) pc_menu_draw_left(game, "...", 14.0f, 55.0f + (VISIBLE - 1) * 15.0f, 180, 180, 180, 200, 1.0f);
    pc_menu_draw_centered(game, hint, 218.0f, 150, 150, 150, 200, 1.0f);
}

static void draw_text_entry(struct game_s* game) {
    static const char* const k_title[] = { "", "- Server name -", "- Server address (IPv4) -", "- Server port -", "- New character name -" };
    static const char* const k_hint[] = { "", "1..32 characters", "e.g. 192.168.1.20 (no hostnames)", "1..65535 (empty = 7777)", "A-Z a-z 0-9 - (1..16)" };
    char line[64];
    pc_menu_draw_centered(game, k_title[s_text], 70.0f, 255, 255, 255, 255, 1.0f);
    snprintf(line, sizeof(line), "%s_", s_buf);
    sanitize(line);
    pc_menu_draw_centered(game, line, 105.0f, 255, 235, 120, 255, 1.0f);
    pc_menu_draw_centered(game, k_hint[s_text], 135.0f, 170, 170, 170, 220, 1.0f);
    pc_menu_draw_centered(game, "Enter = next / OK, Esc = cancel", 218.0f, 150, 150, 150, 200, 1.0f);
}

void pc_play_online_menu_draw(struct game_s* game, int with_dim_backdrop) {
    char buf[128];
    if (!s_active || game == NULL || game->graph == NULL) return;
    if (with_dim_backdrop) pc_menu_dim_rect(game->graph, 180);
    if (s_text != T_NONE) {
        draw_text_entry(game);
    } else if (s_page == PG_SERVERS) {
        draw_list(game, "- Play Online: choose a server -", s_nsrv == 0 ? "No saved servers: Add server (or --server-add)" : "Confirm = select, Cancel = back");
    } else if (s_page == PG_ACTIONS) {
        snprintf(buf, sizeof(buf), "- %s -", s_srv[s_cur].name);
        sanitize(buf);
        draw_list(game, buf, "Connect picks a character next");
    } else if (s_page == PG_CHARS) {
        draw_list(game, "- Choose a character -", "The client restarts the game to connect");
    } else {
        snprintf(buf, sizeof(buf), "Delete %s?", s_srv[s_cur].name);
        sanitize(buf);
        pc_menu_draw_centered(game, "- Delete server -", 80.0f, 255, 255, 255, 255, 1.0f);
        pc_menu_draw_centered(game, buf, 115.0f, 230, 230, 230, 255, 1.0f);
        pc_menu_draw_two_choice(game, "Keep", "Delete", s_del_sel, 160.0f);
    }
    if (s_msg[0] != '\0') {
        snprintf(buf, sizeof(buf), "%.60s", s_msg);
        sanitize(buf);
        if (s_msg_err) pc_menu_draw_centered(game, buf, 195.0f, 255, 120, 110, 255, 1.0f);
        else pc_menu_draw_centered(game, buf, 195.0f, 255, 195, 85, 230, 1.0f);
    }
}
