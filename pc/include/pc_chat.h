/* pc_chat.h - multiplayer TEXT CHAT (press T, type, Enter): the pure logic (pc_chat.c, no game / SDL state: native selftest pc_chat_selftest()), the wire
 * layouts of messages 100 CHAT_SEND / 101 CHAT_MSG, and the game-side entry points (pc_chat_ui.c: input, bubbles above the speaker, the chat log).
 *
 * TEXT: chat text is carried and stored in the GAME'S OWN FONT CODES (m_font.h CHAR_*: one byte per glyph of the in-game message font, the same codes
 * player names use), never as UTF-8. The typed UTF-8 is converted on the sender (pc_chat_from_utf8: ASCII + Latin-1 letters + a few symbols; a character the
 * font cannot show becomes '?', control characters are dropped). Code 0 (inverted '!') is never used (it would end a C string: U+00A1 is sent as '!'), nor
 * 0x7F (the message control code) or 0x80 (the message tag). The host re-validates every byte (pc_chat_sanitize: anything outside the table becomes '?').
 * Drawn with the game font atlas (pc_text_draw), so what the sender sees is exactly what everybody sees.
 *
 * WIRE (protocol 8 extended in place, NO version bump; old peers ignore both ids: the dispatchers' silent 'unrecognized: ignore' fallthrough):
 *   100 CHAT_SEND  client -> host ONLY, RELIABLE, 68 bytes: u8 msg_type, u8 len (1..60), u16 seq, u8 text[64] (font codes, bytes >= len are 0)
 *   101 CHAT_MSG   host -> client, RELIABLE, 80 bytes: u8 msg_type, u8 origin (the speaker's wire id; 0xFF = the server), u8 flags (PC_CHAT_F_*),
 *                  u8 len (0..60), u16 seq (the speaker's seq), u8 scene_id (the speaker's scene), u8 rsv (0), u8 name[8] (font codes, the bound identity's
 *                  name), u8 text[64]
 *     flags CAPS (+ ON): the host's chat capability, sent once to every READY peer and again when the operator toggles `chat on|off` (len 0). A client
 *     enables the chat key only after a CAPS with ON arrived in this session; an old host never sends one ("Chat is not available on this server").
 *     flags SERVER: an announcement (`say`), shown to everyone in the log (no bubble). flags NOTICE: a line for the sender only (muted / too fast / off).
 * ROUTING (host): a validated message goes to every READY peer whose announced scene (scene id AND owner) equals the speaker's, and back to the speaker
 * (its echo, so every client shows the host-validated text), and is presented on the listen host's own screen when its player is in that scene.
 * PRESENTATION ONLY: chat never touches items, bells, the world or the save; text is never parsed as a command. Logged as "[CHAT] name: text". */
#ifndef PC_CHAT_H
#define PC_CHAT_H

#include <stddef.h>
#include <stdint.h>

#include "pc_ops.h" /* the mute list reuses the operator list container (identity kind + 20-byte key) */

#ifdef __cplusplus
extern "C" {
#endif

#define PC_CHAT_MSG_SEND 100
#define PC_CHAT_MSG_MSG  101

#define PC_CHAT_MAX_LEN   60 /* font codes per message */
#define PC_CHAT_WIRE_TEXT 64
#define PC_CHAT_NAME_LEN  8
#define PC_CHAT_ORIGIN_SERVER 0xFFu

#define PC_CHAT_F_SERVER 0x01u /* an operator announcement (`say`), origin 0xFF */
#define PC_CHAT_F_CAPS   0x02u /* capability announcement, no text */
#define PC_CHAT_F_ON     0x04u /* with CAPS: chat is on */
#define PC_CHAT_F_NOTICE 0x08u /* a host notice to this player only (muted / too fast / off), no bubble */
#define PC_CHAT_F_KNOWN  (PC_CHAT_F_SERVER | PC_CHAT_F_CAPS | PC_CHAT_F_ON | PC_CHAT_F_NOTICE)

typedef struct PCChatSendMsg {
    uint8_t  msg_type; /* 100 */
    uint8_t  len;      /* 1..PC_CHAT_MAX_LEN */
    uint16_t seq;      /* the sender's message counter (diagnostics / dedup) */
    uint8_t  text[PC_CHAT_WIRE_TEXT];
} PCChatSendMsg;
_Static_assert(sizeof(PCChatSendMsg) == 68, "PCChatSendMsg is a fixed 68-byte wire struct");

typedef struct PCChatMsg {
    uint8_t  msg_type; /* 101 */
    uint8_t  origin;   /* the speaker's wire id, PC_CHAT_ORIGIN_SERVER for the server */
    uint8_t  flags;    /* PC_CHAT_F_* */
    uint8_t  len;      /* 0..PC_CHAT_MAX_LEN */
    uint16_t seq;
    uint8_t  scene_id; /* the speaker's scene (0 for SERVER / CAPS / NOTICE) */
    uint8_t  rsv;      /* 0 */
    uint8_t  name[PC_CHAT_NAME_LEN];
    uint8_t  text[PC_CHAT_WIRE_TEXT];
} PCChatMsg;
_Static_assert(sizeof(PCChatMsg) == 80, "PCChatMsg is a fixed 80-byte wire struct");

/* ---- charset ---- */
int pc_chat_code_ok(uint8_t code);                                   /* 1 = a font code allowed in chat text */
int pc_chat_cp_to_code(uint32_t cp);                                 /* code, -1 = unmappable (becomes '?'), -2 = control (dropped) */
int pc_chat_utf8_next(const char* s, uint32_t* cp);                  /* bytes consumed (0 at the end); an invalid sequence = 1 byte, cp 0xFFFD */
int pc_chat_from_utf8(const char* utf8, uint8_t* out, int cap);      /* typed text -> font codes; returns the count */
int pc_chat_sanitize(const uint8_t* in, int len, uint8_t* out);     /* host + receiver: bad codes -> '?', leading / trailing / doubled blanks trimmed; returns the new len (0 = drop) */
int pc_chat_to_utf8(const uint8_t* codes, int len, char* out, int cap); /* logs: font codes -> UTF-8 ('?' for a glyph without a Unicode twin); returns bytes written */
int pc_chat_name_to_utf8(const uint8_t name[PC_CHAT_NAME_LEN], char* out, int cap); /* trailing blanks / NULs trimmed */

/* ---- input line editing (pure) ---- */
int pc_chat_input_append(uint8_t* buf, int len, int max, const char* utf8); /* returns the new len */
int pc_chat_input_backspace(int len);

/* ---- layout ---- */
#define PC_CHAT_BUBBLE_COLS  24
#define PC_CHAT_BUBBLE_LINES 3
/* word wrap: lines of at most `width` codes (a word longer than the width is cut), at most `max_lines`; *truncated (optional) = 1 when text was left over
 * (the caller draws ".." after the last line, which is then shortened to leave room for it). starts[] / lens[] receive the lines (blanks at the breaks are
 * not part of a line). Returns the number of lines (0 for empty text). */
int pc_chat_wrap(const uint8_t* text, int len, int width, int max_lines, int* starts, int* lens, int* truncated);

/* ---- timing ---- */
uint32_t pc_chat_bubble_life_ms(int len);                  /* 6 s + 0.1 s per char, at most 12 s */
int      pc_chat_fade_alpha(uint32_t age_ms, uint32_t life_ms, uint32_t fade_ms); /* 255 .. 0 over the last fade_ms; 0 once expired */
#define PC_CHAT_LOG_SHOW_MS 10000u
#define PC_CHAT_FADE_MS     800u

/* ---- host rate limiter (token bucket) ---- */
#define PC_CHAT_RATE_BURST    3
#define PC_CHAT_RATE_REFILL_MS 700u
typedef struct PCChatRate {
    uint32_t last_ms;
    int32_t  credit_ms; /* tokens * PC_CHAT_RATE_REFILL_MS */
    uint8_t  init;
} PCChatRate;
void pc_chat_rate_reset(PCChatRate* r);
int  pc_chat_rate_take(PCChatRate* r, uint32_t now_ms); /* 1 = allowed (a token taken), 0 = too fast */

/* ---- colours ---- */
void pc_chat_speaker_color(const uint8_t name[PC_CHAT_NAME_LEN], int* r, int* g, int* b); /* stable per name */

/* ---- wire validation ---- */
int pc_chat_send_valid(const PCChatSendMsg* m); /* type, 1..60, every code ok, zero tail */
int pc_chat_msg_valid(const PCChatMsg* m);      /* type, known flags, len, codes, rsv */

/* ---- the mute list (host): PCOpsList entries, file `mutes.ini` next to ops.ini, same tolerant / atomic style ----
 *   # comments
 *   version = 1
 *   mute = resident|guest <40 hex digits> <name (informational)> */
int pc_chat_mutes_parse(PCOpsList* l, const char* text, size_t len, int* bad_lines);
int pc_chat_mutes_format(const PCOpsList* l, char* out, size_t cap);
int pc_chat_mutes_load(const char* path, PCOpsList* l, int* bad_lines); /* PC_OPS_OK / PC_OPS_MISSING / PC_OPS_BAD */
int pc_chat_mutes_save(const char* path, const PCOpsList* l);           /* 1 = written + renamed */

/* ---- the local presentation store (bubbles + log), pure: pc_chat_ui.c draws it ---- */
#define PC_CHAT_BUBBLES      16
#define PC_CHAT_BUBBLES_PER  2  /* per speaker: the newest sits on the head, the previous one above it */
#define PC_CHAT_LOG_LINES    8
#define PC_CHAT_LOG_SHOWN    6
typedef struct PCChatEntry {
    uint8_t  used;
    uint8_t  flags;  /* PC_CHAT_F_SERVER / NOTICE */
    uint8_t  self;   /* the local player's own line */
    uint8_t  len;
    int      speaker; /* wire id, -1 = server / notice */
    uint32_t t0, life;
    uint32_t serial;  /* arrival order */
    uint8_t  drawn_logged;
    uint8_t  name[PC_CHAT_NAME_LEN];
    uint8_t  text[PC_CHAT_MAX_LEN + 1]; /* NUL-terminated (code 0 never occurs) */
} PCChatEntry;
typedef struct PCChatStore {
    PCChatEntry bubble[PC_CHAT_BUBBLES];
    PCChatEntry log[PC_CHAT_LOG_LINES]; /* ring */
    int         log_head;                /* next write */
    uint32_t    serial;
} PCChatStore;
void pc_chat_store_init(PCChatStore* s);
/* adds a line: always to the log; a bubble too unless flags carry SERVER / NOTICE. Returns the bubble index or -1. */
int  pc_chat_store_add(PCChatStore* s, int speaker, int self, uint8_t flags, const uint8_t name[PC_CHAT_NAME_LEN], const uint8_t* text, int len, uint32_t now);
void pc_chat_store_expire(PCChatStore* s, uint32_t now);
void pc_chat_store_clear(PCChatStore* s);
/* the k-th newest bubble of one speaker (k 0 = newest), NULL when none */
const PCChatEntry* pc_chat_store_bubble_of(const PCChatStore* s, int speaker, int k);
/* log line i (0 = newest .. PC_CHAT_LOG_LINES-1), NULL when unused */
const PCChatEntry* pc_chat_store_log(const PCChatStore* s, int i);

int pc_chat_selftest(void); /* 0 = pass, else the number of failures */

/* ===================== game side (pc_chat_ui.c; not part of the native selftest) ===================== */
#ifndef PC_CHAT_PURE_ONLY
struct game_s;
union SDL_Event;
int  pc_chat_handle_event(const union SDL_Event* e); /* pc_main.c event loop: 1 = consumed (typing / the chat key opened the line) */
int  pc_chat_pad_block(void);                       /* pc_pad.c PADRead: 1 = zero the local pad (typing, or the Enter / Esc that closed it still held) */
int  pc_chat_typing(void);
int  pc_chat_can_open(void);                        /* free play: a field / room, no menu, message, demo, pause, wheel or text field */
void pc_chat_draw(struct game_s* game);             /* graph.c, after the PC menus: bubbles + log + the input line */
/* text typed by the local player (UTF-8): converted, then sent through the real path (pc_net_game_chat_send). 1 = sent. */
int  pc_chat_submit_utf8(const char* utf8);
/* from the network glue (pc_net_game.c CHAT block): one validated line for this screen */
void pc_chat_on_line(int speaker, int self, uint8_t flags, const uint8_t name[PC_CHAT_NAME_LEN], const uint8_t* text, int len);
void pc_chat_local_notice(const char* ascii);       /* a local grey line in the log (e.g. "Chat is not available on this server") */
void pc_chat_reset_session(void);                   /* drops bubbles + log (session end) */
void pc_chat_test_type_tick(void);                  /* TEST-ONLY AC_TEST_CHAT_TYPE (synthetic SDL key / text events through the real path); inert otherwise */
/* pc_net_game.c: 1 = the chat is usable right now (role + caps + setting), *why (optional) = the notice when not */
int  pc_net_game_chat_available(const char** why);
int  pc_net_game_chat_send(const uint8_t* codes, int len); /* 1 = sent / presented */
int  pc_net_game_chat_enabled_local(void);                 /* settings.ini [Network] chat + env AC_MP_CHAT */
/* the dedicated console: cmd = "say" | "mute" | "unmute" | "mutes" | "chat"; args = the rest of the line (case kept) */
void pc_net_game_dedicated_chat(const char* cmd, const char* args, void (*out)(const char*));
#endif

#ifdef __cplusplus
}
#endif
#endif
