/* pc_play_online_menu.c - see pc_play_online_menu.h. Modelled on pc_settings_menu.c (state + nav/confirm/cancel/draw) and on its keybinding capture (raw SDL events
 * routed by pc_main). Never blocks: every operation is a small file read/write or a queued request (the in-process connect itself runs in pc_main_play_online_poll). */
#include "pc_play_online_menu.h"
#include "pc_character.h"
#include "pc_guest_profile.h"
#include "pc_menu_util.h"
#include "pc_net_game.h"
#include "pc_player_preview.h" /* the real player model next to each character row */
#include "pc_text_draw.h"
#include "pc_relaunch.h" /* PC_RELAUNCH_* kinds (the relaunch itself is no longer used here) */
#include "pc_servers.h"

#include "m_font.h"
#include "m_rcp.h"
#include "graph.h"
#include "main.h" /* SCREEN_WIDTH_F */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { PG_SERVERS, PG_ACTIONS, PG_CHARS, PG_DELETE, PG_MCHARS, PG_MACT, PG_IMPORT, PG_LEGACY, PG_ICONF, PG_IRES };
enum { T_NONE, T_SRV_NAME, T_SRV_ADDR, T_SRV_PORT, T_CHAR_NAME, T_CHAR_LABEL, T_GCI_PATH };

#define VISIBLE 8
#define CHAR_ROWS 5 /* the character page shows fewer, taller rows (a player model beside each name) */

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
static int s_del_char = 0; /* the delete page targets a CHARACTER (s_mi) instead of the server */
static int s_del_res = 0;  /* towns the character is a resident of (stronger warning) */
static int s_mi = 0;       /* the character being managed (index into s_chr) */
static int s_leg[PC_CHARACTER_MAX]; /* legacy rows of s_chr (indices), the Legacy profiles page */
static int s_nleg = 0;
static int s_li = 0;       /* the legacy row chosen for import (index into s_chr) */
static PCCharImportReport s_rep; /* the last save-file import */
static int s_rep_ok = 0;

/* text entry */
static int s_text = T_NONE;
static int s_text_grace = 0;
static char s_buf[400];
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

/* M-J: 1 per character whose membership.ini for the chosen server's last known town (servers.ini last_town, a UI hint) says role = resident. Read when the list is
 * (re)loaded, not per frame. A server without last_town (never reached READY) marks nothing. */
static unsigned char s_chr_resident[PC_CHARACTER_MAX];

static void mark_residents(void) {
    int i;
    memset(s_chr_resident, 0, sizeof(s_chr_resident));
    if (s_cur < 0 || s_cur >= s_nsrv || s_srv[s_cur].last_town[0] == '\0') return;
    for (i = 0; i < s_nchr && i < PC_CHARACTER_MAX; i++) {
        char role[16];
        uint8_t pid[20];
        if (s_chr[i].storage == PC_CHARACTER_STORAGE_STORE && pc_character_membership_read(NULL, s_chr[i].uuid, s_srv[s_cur].last_town, role, pid) && strcmp(role, "resident") == 0) {
            s_chr_resident[i] = 1;
        }
    }
}

static void reload_characters(void) {
    int skipped = 0;
    s_nchr = pc_character_list(NULL, s_chr, PC_CHARACTER_MAX, &skipped);
    if (s_nchr < 0) s_nchr = 0;
    mark_residents();
}

/* the character as shown in the menus: its local display label when it has one, else its name */
static const char* disp_name(const PCCharacter* c) {
    return c->label[0] != '\0' ? c->label : c->name;
}

/* keep the managed character selected by uuid after a reload (reorder / rename); falls back to the clamped old index */
static void refind_mi(const char* uuid) {
    int i;
    for (i = 0; i < s_nchr; i++) {
        if (s_chr[i].storage == PC_CHARACTER_STORAGE_STORE && strcmp(s_chr[i].uuid, uuid) == 0) {
            s_mi = i;
            return;
        }
    }
    if (s_mi >= s_nchr) s_mi = s_nchr - 1;
    if (s_mi < 0) s_mi = 0;
}

static int row_count(void) {
    switch (s_page) {
        case PG_SERVERS: return s_nsrv + 2;
        case PG_ACTIONS: return 4;
        case PG_CHARS:   return s_nchr + 4;
        case PG_MCHARS:  return s_nchr + 1;
        case PG_MACT:    return 5;
        case PG_IMPORT:  return 3;
        case PG_LEGACY:  return s_nleg + 1;
    }
    return 2;
}

static void sel_clamp(void) {
    const int n = row_count();
    if (s_sel < 0) s_sel = 0;
    if (s_sel > n - 1) s_sel = n - 1;
    if (s_sel < s_scroll) s_scroll = s_sel;
    {
        const int vis = (s_page == PG_CHARS || s_page == PG_MCHARS) ? CHAR_ROWS : VISIBLE;
        if (s_sel >= s_scroll + vis) s_scroll = s_sel - vis + 1;
    }
    if (s_scroll < 0) s_scroll = 0;
}

static void goto_page(int page, int sel) {
    s_page = page;
    s_sel = sel;
    s_scroll = 0;
    sel_clamp();
}

/* ---------------- text entry ---------------- */

static int s_paste_port = 0; /* a 'host:port' pasted into the address field: the port step starts with this value */

static void text_begin(int step, const char* initial) {
    s_text = step;
    if (step == T_SRV_NAME) s_paste_port = 0;
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
        case T_SRV_ADDR: return PC_SERVER_ADDR_MAX - 1;
        case T_SRV_PORT: return 5;
        case T_CHAR_NAME: return 16;
        case T_CHAR_LABEL: return 16;
        case T_GCI_PATH: return 330;
    }
    return 0;
}

static int text_char_ok(unsigned char c) {
    switch (s_text) {
        case T_SRV_NAME: return glyph_ok(c) && strchr("[]=\"\\", c) == NULL;
        case T_SRV_ADDR: return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '.' || c == '-';
        case T_SRV_PORT: return c >= '0' && c <= '9';
        case T_GCI_PATH: return c >= 0x20 && c < 0x7F && c != '"';
        case T_CHAR_NAME:
        case T_CHAR_LABEL: return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '-';
    }
    return 0;
}

static void do_connect(const PCServer* s, int kind, const char* name, const char* display) {
    if (s_launched) return;
    (void)pc_servers_set_last(NULL, s->name, display, NULL); /* UI hint only; failure is irrelevant */
    /* In-process connect (no relaunch): the request is executed by pc_main_play_online_poll() (pc_vi.c) at the end of this frame. The menu ignores input meanwhile. */
    if (pc_main_play_online_request(s->address, s->port, kind, name)) {
        s_launched = 1;
        set_msg(0, "Connecting to %s ...", s->name);
    } else {
        set_msg(1, "%s", "Could not start the connection (bad server or character name)");
    }
}

void pc_play_online_menu_connect_result(int ok, const char* msg) {
    if (ok) {
        set_msg(0, "%s", msg != NULL ? msg : "Connected: loading the town ...");
        /* s_launched stays 1: the menu is inert (and drawn) while the title fades out; pc_main closes it once the title scene is gone */
    } else {
        s_launched = 0; /* back to the menu, the character page, with the reason */
        set_msg(1, "%s", msg != NULL ? msg : "Could not connect");
    }
}

void pc_play_online_menu_close(void) {
    s_active = 0;
    s_launched = 0;
    s_text = T_NONE;
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
                snprintf(pb, sizeof(pb), "%d", s_paste_port > 0 ? s_paste_port : (s_draft.port > 0 ? s_draft.port : PC_SERVER_DEFAULT_PORT));
                s_paste_port = 0;
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
        case T_GCI_PATH: {
            char path[400], gerr[200];
            const char* q = s_buf;
            size_t ql;
            while (*q == ' ') q++;
            snprintf(path, sizeof(path), "%s", q);
            ql = strlen(path);
            while (ql > 0 && path[ql - 1] == ' ') path[--ql] = '\0';
            text_end();
            if (path[0] == '\0') {
                goto_page(PG_IMPORT, 1);
                return;
            }
            s_rep_ok = pc_character_import_gci(NULL, path, &s_rep, gerr, sizeof(gerr)); /* read-only: the file is only read into memory */
            if (!s_rep_ok) {
                memset(&s_rep, 0, sizeof(s_rep));
                snprintf(s_rep.notes[0], sizeof(s_rep.notes[0]), "%.70s", gerr);
                s_rep.nnotes = 1;
            }
            reload_characters();
            s_page = PG_IRES;
            s_sel = 0;
            s_scroll = 0;
            return;
        }
        case T_CHAR_LABEL:
            if (s_mi < 0 || s_mi >= s_nchr || s_chr[s_mi].storage != PC_CHARACTER_STORAGE_STORE) {
                text_end();
                return;
            }
            if (!pc_guest_profile_name_check(s_buf, why, sizeof(why))) {
                set_msg(1, "%s", why);
                return;
            }
            {
                char uuid[PC_CHARACTER_UUID_LEN + 1];
                snprintf(uuid, sizeof(uuid), "%s", s_chr[s_mi].uuid);
                text_end();
                if (pc_character_label_set(NULL, uuid, s_buf)) { /* display label ONLY: name / name_bytes / uuid / ids are untouched */
                    set_msg(0, "Renamed to %s", s_buf);
                } else {
                    set_msg(1, "%s", "Could not save the new name");
                }
                reload_characters();
                refind_mi(uuid);
            }
            return;
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

/* Clipboard (Ctrl+V / Ctrl+C / Ctrl+X, Shift+Insert / Ctrl+Insert too). Paste goes through the SAME per-character validation and length limit as typing; whitespace and
 * line breaks around / inside the text are dropped. In the address field a trailing ':port' (1..65535) is split off and pre-fills the port step; an invalid ':...' is cut. */
static void text_paste(void) {
    char* clip;
    const char* p;
    char tmp[400];
    size_t n = 0;
    if (!SDL_HasClipboardText()) return;
    clip = SDL_GetClipboardText();
    if (clip == NULL) return;
    for (p = clip; *p != '\0' && n < sizeof(tmp) - 1; p++) {
        if ((unsigned char)*p < 0x20 || (*p == ' ' && s_text != T_SRV_NAME && s_text != T_GCI_PATH)) continue; /* control chars / line breaks; spaces only inside a server NAME */
        tmp[n++] = *p;
    }
    tmp[n] = '\0';
    SDL_free(clip);
    if (s_text == T_SRV_ADDR) {
        char* colon = strchr(tmp, ':');
        if (colon != NULL) {
            char* end = NULL;
            const long port = strtol(colon + 1, &end, 10);
            *colon = '\0';
            if (colon[1] != '\0' && end != NULL && *end == '\0' && pc_servers_port_check(port)) s_paste_port = (int)port;
        }
    }
    for (p = tmp; *p != '\0'; p++) {
        const unsigned char c = (unsigned char)*p;
        if (c == ' ' && s_buf_len == 0) {
            continue;
        }
        if (s_buf_len < text_max() && s_buf_len < (int)sizeof(s_buf) - 1 && text_char_ok(c)) {
            s_buf[s_buf_len++] = (char)c;
            s_buf[s_buf_len] = '\0';
        }
    }
}

static void text_copy(int cut) {
    if (s_buf_len > 0) SDL_SetClipboardText(s_buf);
    if (cut) {
        s_buf[0] = '\0';
        s_buf_len = 0;
    }
}

int pc_play_online_menu_handle_text_event(const SDL_Event* e) {
    if (!pc_play_online_menu_text_active()) return 0;
    switch (e->type) {
        case SDL_TEXTINPUT: {
            const char* p;
            for (p = e->text.text; *p; p++) {
                const unsigned char c = (unsigned char)*p;
                if (c == ' ' && s_buf_len == 0) {
                    continue; /* a field never starts with a space (also swallows the Space key that opened the field from the menu) */
                }
                if (s_buf_len < text_max() && s_buf_len < (int)sizeof(s_buf) - 1 && text_char_ok(c)) {
                    s_buf[s_buf_len++] = (char)c;
                    s_buf[s_buf_len] = '\0';
                }
            }
            return 1;
        }
        case SDL_KEYDOWN:
            if ((e->key.keysym.mod & KMOD_CTRL) != 0 || (e->key.keysym.mod & KMOD_SHIFT) != 0) {
                const int ctrl = (e->key.keysym.mod & KMOD_CTRL) != 0;
                const SDL_Keycode k = e->key.keysym.sym;
                if ((ctrl && k == SDLK_v) || (!ctrl && k == SDLK_INSERT)) {
                    if (!e->key.repeat) text_paste();
                    return 1;
                }
                if ((ctrl && k == SDLK_c) || (ctrl && k == SDLK_INSERT)) {
                    if (!e->key.repeat) text_copy(0);
                    return 1;
                }
                if (ctrl && k == SDLK_x) {
                    if (!e->key.repeat) text_copy(1);
                    return 1;
                }
            }
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
    if (s_text != T_NONE || s_page == PG_DELETE || s_page == PG_ICONF || s_page == PG_IRES) return 1;
    s_sel--;
    sel_clamp();
    return 1;
}

int pc_play_online_menu_nav_down(void) {
    if (s_text != T_NONE || s_page == PG_DELETE || s_page == PG_ICONF || s_page == PG_IRES) return 1;
    s_sel++;
    sel_clamp();
    return 1;
}

int pc_play_online_menu_nav_left(void) {
    if (s_page == PG_DELETE || s_page == PG_ICONF) s_del_sel = 0;
    return 1;
}

int pc_play_online_menu_nav_right(void) {
    if (s_page == PG_DELETE || s_page == PG_ICONF) s_del_sel = 1;
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
                s_del_char = 0;
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
            } else if (s_sel == s_nchr + 1) { /* Import characters */
                goto_page(PG_IMPORT, 0);
            } else if (s_sel == s_nchr + 2) { /* Manage characters (never connects) */
                goto_page(PG_MCHARS, 0);
            } else {
                goto_page(PG_ACTIONS, 0);
            }
            return 1;
        case PG_IMPORT:
            if (s_sel == 0) { /* Legacy profiles not imported yet */
                int i;
                s_nleg = 0;
                for (i = 0; i < s_nchr; i++) {
                    if (s_chr[i].storage == PC_CHARACTER_STORAGE_LEGACY && s_nleg < PC_CHARACTER_MAX) s_leg[s_nleg++] = i;
                }
                goto_page(PG_LEGACY, 0);
            } else if (s_sel == 1) { /* From save file */
                text_begin(T_GCI_PATH, "");
            } else {
                goto_page(PG_CHARS, s_nchr + 1);
            }
            return 1;
        case PG_LEGACY:
            if (s_sel < s_nleg) {
                s_li = s_leg[s_sel];
                s_del_sel = 0;
                s_page = PG_ICONF;
            } else {
                goto_page(PG_IMPORT, 0);
            }
            return 1;
        case PG_ICONF:
            if (s_del_sel == 1 && s_li >= 0 && s_li < s_nchr) {
                PCCharacter imp;
                char err[200], nm[24];
                int ntok = 0;
                snprintf(nm, sizeof(nm), "%s", s_chr[s_li].name);
                if (pc_character_import_legacy(NULL, s_chr[s_li].legacy_profile, &imp, &ntok, err, sizeof(err))) { /* the legacy files are only read */
                    int i;
                    reload_characters();
                    goto_page(PG_CHARS, 0);
                    for (i = 0; i < s_nchr; i++) {
                        if (s_chr[i].storage == PC_CHARACTER_STORAGE_STORE && strcmp(s_chr[i].uuid, imp.uuid) == 0) {
                            goto_page(PG_CHARS, i);
                            break;
                        }
                    }
                    set_msg(0, "Imported %s", nm);
                } else {
                    set_msg(1, "Not imported: %s", err);
                    goto_page(PG_LEGACY, 0);
                }
            } else {
                goto_page(PG_LEGACY, 0);
            }
            return 1;
        case PG_IRES: { /* Continue: the character list, the newest imported character selected */
            int i;
            reload_characters();
            goto_page(PG_CHARS, 0);
            for (i = 0; s_rep.last_uuid[0] != '\0' && i < s_nchr; i++) {
                if (s_chr[i].storage == PC_CHARACTER_STORAGE_STORE && strcmp(s_chr[i].uuid, s_rep.last_uuid) == 0) {
                    goto_page(PG_CHARS, i);
                    break;
                }
            }
            return 1;
        }
        case PG_MCHARS:
            if (s_sel < s_nchr) {
                s_mi = s_sel;
                goto_page(PG_MACT, 0);
            } else {
                goto_page(PG_CHARS, 0);
            }
            return 1;
        case PG_MACT: {
            const PCCharacter* c;
            if (s_mi < 0 || s_mi >= s_nchr) {
                goto_page(PG_MCHARS, 0);
                return 1;
            }
            c = &s_chr[s_mi];
            if (s_sel == 4) { /* Back */
                goto_page(PG_MCHARS, s_mi);
                return 1;
            }
            if (c->storage != PC_CHARACTER_STORAGE_STORE) {
                set_msg(1, "%s", s_sel == 3 ? "Legacy profile files are never deleted" : "Import it first: Import characters > Legacy profiles");
                return 1;
            }
            if (s_sel == 0) { /* Rename (display label only) */
                text_begin(T_CHAR_LABEL, disp_name(c));
            } else if (s_sel == 1 || s_sel == 2) { /* Move up / down */
                char uuid[PC_CHARACTER_UUID_LEN + 1];
                snprintf(uuid, sizeof(uuid), "%s", c->uuid);
                if (pc_character_move(NULL, uuid, s_sel == 1 ? -1 : 1)) {
                    reload_characters();
                    refind_mi(uuid);
                } else {
                    set_msg(0, "%s", s_sel == 1 ? "Already first" : "Already last");
                }
            } else { /* Delete (confirm page) */
                s_del_sel = 0;
                s_del_char = 1;
                s_del_res = pc_character_resident_count(NULL, c->uuid);
                s_page = PG_DELETE;
            }
            return 1;
        }
        case PG_DELETE:
            if (s_del_char) {
                if (s_del_sel == 1 && s_mi >= 0 && s_mi < s_nchr) {
                    char err[200], uuid[PC_CHARACTER_UUID_LEN + 1], nm[24];
                    snprintf(uuid, sizeof(uuid), "%s", s_chr[s_mi].uuid);
                    snprintf(nm, sizeof(nm), "%s", disp_name(&s_chr[s_mi]));
                    if (pc_character_delete(NULL, uuid, err, sizeof(err))) {
                        set_msg(0, "Deleted character %s", nm);
                    } else {
                        set_msg(1, "Not deleted: %s", err);
                    }
                    reload_characters();
                    s_del_char = 0;
                    goto_page(PG_MCHARS, s_mi < s_nchr ? s_mi : (s_nchr > 0 ? s_nchr - 1 : 0));
                } else {
                    s_del_char = 0;
                    goto_page(PG_MACT, 3);
                }
                return 1;
            }
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
        case PG_MCHARS:
            goto_page(PG_CHARS, s_nchr + 2);
            return 1;
        case PG_IMPORT:
            goto_page(PG_CHARS, s_nchr + 1);
            return 1;
        case PG_LEGACY:
            goto_page(PG_IMPORT, 0);
            return 1;
        case PG_ICONF:
            goto_page(PG_LEGACY, 0);
            return 1;
        case PG_IRES:
            return pc_play_online_menu_confirm();
        case PG_MACT:
            goto_page(PG_MCHARS, s_mi);
            return 1;
        case PG_DELETE:
            if (s_del_char) {
                s_del_char = 0;
                goto_page(PG_MACT, 3);
            } else {
                goto_page(PG_ACTIONS, 2);
            }
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
            if (i < s_nsrv) snprintf(out, cap, "%s", s_srv[i].name); /* the configured name only; address / port stay stored and used for the connection */
            else if (i == s_nsrv) snprintf(out, cap, "Add server");
            else snprintf(out, cap, "Back");
            break;
        case PG_ACTIONS: {
            static const char* const k[4] = { "Connect", "Edit", "Delete", "Back" };
            snprintf(out, cap, "%s", k[i & 3]);
            break;
        }
        case PG_IMPORT: {
            static const char* const k[3] = { "Legacy profiles", "From save file", "Back" };
            snprintf(out, cap, "%s", k[i % 3]);
            break;
        }
        case PG_LEGACY:
            if (i < s_nleg) snprintf(out, cap, "%s", s_chr[s_leg[i]].name);
            else snprintf(out, cap, "Back");
            break;
        case PG_MACT: {
            static const char* const k[5] = { "Rename", "Move up", "Move down", "Delete", "Back" };
            snprintf(out, cap, "%s", k[i % 5]);
            break;
        }
        case PG_CHARS:
        case PG_MCHARS:
            if (i < s_nchr) {
                int dup = 0, j;
                const char* nm = disp_name(&s_chr[i]);
                for (j = 0; j < s_nchr; j++) {
                    if (j != i && strcmp(disp_name(&s_chr[j]), nm) == 0) dup = 1;
                }
                if (s_chr[i].storage == PC_CHARACTER_STORAGE_STORE) {
                    const char* res = (i < PC_CHARACTER_MAX && s_chr_resident[i]) ? " (resident)" : "";
                    if (dup) snprintf(out, cap, "%s (%.4s)%s", nm, s_chr[i].uuid, res);
                    else snprintf(out, cap, "%s%s", nm, res);
                } else {
                    snprintf(out, cap, "%s", nm); /* legacy / imported profiles show the plain name */
                }
            } else if (s_page == PG_CHARS && i == s_nchr) {
                snprintf(out, cap, "New character");
            } else if (s_page == PG_CHARS && i == s_nchr + 1) {
                snprintf(out, cap, "Import characters");
            } else if (s_page == PG_CHARS && i == s_nchr + 2) {
                snprintf(out, cap, "Manage characters");
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

/* The character page: one row per character, the real player model (UI only, see pc_player_preview.h) left of its name. Taller rows than the other lists. */
#define CHAR_ROW_H 30.0f
static void draw_char_list(struct game_s* game) {
    const int n = row_count();
    int i, r, g, b, a;
    const int first = s_scroll;
    pc_menu_draw_centered(game, s_page == PG_MCHARS ? "- Manage characters -" : "- Choose a character -", 26.0f, 255, 255, 255, 255, 1.0f);
    pc_player_preview_tick();
    for (i = 0; i < CHAR_ROWS && first + i < n; i++) {
        const int row = first + i;
        const int selected = (row == s_sel);
        const f32 y = 44.0f + (f32)i * CHAR_ROW_H;
        char buf[96];
        row_label(row, buf, sizeof(buf));
        pc_menu_row_colors(selected, &r, &g, &b, &a);
        pc_menu_draw_centered(game, buf, y + 8.0f, r, g, b, a, selected ? PC_MENU_SCALE_SELECTED : 1.0f);
        if (row < s_nchr && pc_player_preview_set_character(i, &s_chr[row])) {
            const f32 w = (f32)pc_text_width(buf) * (selected ? PC_MENU_SCALE_SELECTED : 1.0f);
            pc_player_preview_draw(game, i, 160.0f - w * 0.5f - 24.0f, y + 30.0f, 26.0f); /* (x, y) = the model's FEET (its origin), the body centered on the row's text line */
        }
    }
    if (first > 0) pc_menu_draw_left(game, "...", 14.0f, 44.0f, 180, 180, 180, 200, 1.0f);
    if (first + CHAR_ROWS < n) pc_menu_draw_left(game, "...", 14.0f, 44.0f + (CHAR_ROWS - 1) * CHAR_ROW_H, 180, 180, 180, 200, 1.0f);
    pc_menu_draw_centered(game, s_page == PG_MCHARS ? "Pick a character to manage (nothing connects)" : "Fetches the town, then joins (no restart)", 218.0f, 150, 150, 150, 200, 1.0f);
}

static void draw_text_entry(struct game_s* game) {
    static const char* const k_title[] = { "", "- Server name -", "- Server address -", "- Server port -", "- New character name -", "- Display name -", "- Save file (.gci) path -" };
    static const char* const k_hint[] = { "", "1..32 characters", "IPv4 or hostname, Ctrl+V pastes", "1..65535 (empty = 7777)", "A-Z a-z 0-9 - (1..16)", "Local label only (A-Z a-z 0-9 -)", "Ctrl+V pastes the full path of the .gci (read only)" };
    char line[64];
    pc_menu_draw_centered(game, k_title[s_text], 70.0f, 255, 255, 255, 255, 1.0f);
    {
        const int skip = (s_text == T_GCI_PATH && s_buf_len > 40) ? s_buf_len - 40 : 0; /* a long path shows its tail */
        snprintf(line, sizeof(line), "%s%s_", skip > 0 ? "..." : "", s_buf + skip);
    }
    sanitize(line);
    pc_menu_draw_centered(game, line, 105.0f, 255, 235, 120, 255, 1.0f);
    pc_menu_draw_centered(game, k_hint[s_text], 135.0f, 170, 170, 170, 220, 1.0f);
    pc_menu_draw_centered(game, "Enter = next or OK, Esc = cancel", 218.0f, 150, 150, 150, 200, 1.0f);
}

void pc_play_online_menu_draw(struct game_s* game, int with_dim_backdrop) {
    char buf[128];
    if (!s_active || game == NULL || game->graph == NULL) return;
    if (with_dim_backdrop) pc_menu_dim_rect(game->graph, 215); /* M-J: 180 let the title logo compete with the rows */
    if (s_text != T_NONE) {
        draw_text_entry(game);
    } else if (s_page == PG_SERVERS) {
        draw_list(game, "- Play Online: choose a server -", s_nsrv == 0 ? "No saved servers: Add server (or --server-add)" : "Confirm = select, Cancel = back");
    } else if (s_page == PG_ACTIONS) {
        snprintf(buf, sizeof(buf), "- %s -", s_srv[s_cur].name);
        sanitize(buf);
        draw_list(game, buf, "Connect picks a character next");
    } else if (s_page == PG_CHARS || s_page == PG_MCHARS) {
        draw_char_list(game);
    } else if (s_page == PG_IMPORT) {
        draw_list(game, "- Import characters -", "Legacy profiles or a .gci save (read only)");
    } else if (s_page == PG_LEGACY) {
        draw_list(game, "- Legacy profiles -", s_nleg == 0 ? "No legacy profiles left to import" : "Confirm = import (the profile files are never changed)");
    } else if (s_page == PG_ICONF) {
        snprintf(buf, sizeof(buf), "Import %s?", s_li >= 0 && s_li < s_nchr ? s_chr[s_li].name : "?");
        sanitize(buf);
        pc_menu_draw_centered(game, "- Import legacy profile -", 70.0f, 255, 255, 255, 255, 1.0f);
        pc_menu_draw_centered(game, buf, 100.0f, 230, 230, 230, 255, 1.0f);
        pc_menu_draw_centered(game, "Creates a character with the same identity;", 125.0f, 190, 190, 190, 230, 1.0f);
        pc_menu_draw_centered(game, "the legacy files stay untouched", 139.0f, 190, 190, 190, 230, 1.0f);
        pc_menu_draw_two_choice(game, "Cancel", "Import", s_del_sel, 170.0f);
        pc_menu_draw_centered(game, "Left or Right, Confirm = OK, Cancel = back", 218.0f, 150, 150, 150, 200, 1.0f);
    } else if (s_page == PG_IRES) {
        int k;
        pc_menu_draw_centered(game, "- Import characters -", 40.0f, 255, 255, 255, 255, 1.0f);
        if (!s_rep_ok) {
            pc_menu_draw_centered(game, "Could not read that save", 80.0f, 255, 120, 110, 255, 1.0f);
        } else if (s_rep.found == 0) {
            pc_menu_draw_centered(game, "No importable characters were found.", 80.0f, 230, 230, 230, 255, 1.0f);
        } else if (s_rep.imported == 0 && s_rep.skipped == 0 && s_rep.existing == s_rep.found) {
            pc_menu_draw_centered(game, "All characters from this save are", 80.0f, 230, 230, 230, 255, 1.0f);
            pc_menu_draw_centered(game, "already in your character list.", 94.0f, 230, 230, 230, 255, 1.0f);
        } else {
            snprintf(buf, sizeof(buf), "Found: %d   Imported: %d", s_rep.found, s_rep.imported);
            pc_menu_draw_centered(game, buf, 72.0f, 230, 230, 230, 255, 1.0f);
            snprintf(buf, sizeof(buf), "Already imported: %d   Skipped: %d", s_rep.existing, s_rep.skipped);
            pc_menu_draw_centered(game, buf, 88.0f, 230, 230, 230, 255, 1.0f);
        }
        for (k = 0; k < s_rep.nnotes; k++) {
            char nb[80];
            snprintf(nb, sizeof(nb), "%.60s", s_rep.notes[k]);
            sanitize(nb);
            pc_menu_draw_centered(game, nb, 112.0f + (f32)k * 14.0f, 255, 195, 85, 230, 1.0f);
        }
        pc_menu_draw_centered(game, "Continue", 176.0f, 255, 235, 120, 255, PC_MENU_SCALE_SELECTED);
        pc_menu_draw_centered(game, "The save file itself is never changed", 218.0f, 150, 150, 150, 200, 1.0f);
    } else if (s_page == PG_MACT) {
        snprintf(buf, sizeof(buf), "- %s -", s_mi >= 0 && s_mi < s_nchr ? disp_name(&s_chr[s_mi]) : "?");
        sanitize(buf);
        draw_list(game, buf, "Rename changes the local display name only");
    } else if (s_del_char) {
        snprintf(buf, sizeof(buf), "Delete %s?", s_mi >= 0 && s_mi < s_nchr ? disp_name(&s_chr[s_mi]) : "?");
        sanitize(buf);
        pc_menu_draw_centered(game, "- Delete character -", 62.0f, 255, 255, 255, 255, 1.0f);
        pc_menu_draw_centered(game, buf, 92.0f, 230, 230, 230, 255, 1.0f);
        pc_menu_draw_centered(game, "Removes this character from this PC only", 112.0f, 190, 190, 190, 230, 1.0f);
        if (s_del_res > 0) {
            pc_menu_draw_centered(game, "RESIDENT: its credential is deleted here;", 130.0f, 255, 120, 110, 255, 1.0f);
            pc_menu_draw_centered(game, "this PC may never reclaim that resident.", 144.0f, 255, 120, 110, 255, 1.0f);
        }
        pc_menu_draw_two_choice(game, "Keep", "Delete", s_del_sel, 170.0f);
        pc_menu_draw_centered(game, "Left or Right, Confirm = OK, Cancel = keep", 218.0f, 150, 150, 150, 200, 1.0f);
    } else {
        snprintf(buf, sizeof(buf), "Delete %s?", s_srv[s_cur].name);
        sanitize(buf);
        pc_menu_draw_centered(game, "- Delete server -", 80.0f, 255, 255, 255, 255, 1.0f);
        pc_menu_draw_centered(game, buf, 115.0f, 230, 230, 230, 255, 1.0f);
        pc_menu_draw_two_choice(game, "Keep", "Delete", s_del_sel, 160.0f);
        pc_menu_draw_centered(game, "Left or Right, Confirm = OK, Cancel = keep", 218.0f, 150, 150, 150, 200, 1.0f);
    }
    if (s_msg[0] != '\0') {
        snprintf(buf, sizeof(buf), "%.64s", s_msg);
        sanitize(buf);
        if (s_msg_err) pc_menu_draw_centered(game, buf, 176.0f, 255, 120, 110, 255, 1.0f);
        else pc_menu_draw_centered(game, buf, 176.0f, 255, 195, 85, 230, 1.0f);
    }
}
