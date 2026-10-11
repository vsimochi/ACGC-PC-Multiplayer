/* pc_ded_console.h - the PURE (no Windows, no threads, no I/O) parts of the dedicated server's interactive console: a one-line editor, the VT text that redraws the prompt around asynchronous
 * output, and the console formatter for log lines. pc_dedicated.c owns the console handles, the lock and the reader thread; everything that decides WHAT is written lives here so that it can be
 * tested byte for byte (pc/tools/net_spike/test_dedicated_console.py runs pc/tools/net_spike/ded_console_selftest.c against it).
 *
 * Why this exists: the interactive server console used to be the console host's own cooked line input (ReadFile on CONIN$). The console host keeps the typed text in ITS buffer and echoes it
 * at the cursor, so any other write to the same console from another thread ("[DEDICATED] ..." notices, log lines) lands in the middle of the half-typed line and moves the cursor away
 * from it. The editor below keeps the line itself (so every write can erase the prompt line, print, and draw the line and the cursor again). */
#ifndef PC_DED_CONSOLE_H
#define PC_DED_CONSOLE_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_DEDC_LINE_MAX 255 /* the same limit as the stdin reader's lines (PC_DED_LINE_MAX) */

typedef struct PCDedcEdit {
    char buf[PC_DEDC_LINE_MAX + 1];
    int  len; /* characters in buf */
    int  cur; /* cursor position 0..len */
} PCDedcEdit;

/* key codes: 32..126 = a printable ASCII character to insert; everything else that is not listed here is ignored */
enum {
    PC_DEDC_KEY_LEFT = 1000,
    PC_DEDC_KEY_RIGHT,
    PC_DEDC_KEY_HOME,
    PC_DEDC_KEY_END,
    PC_DEDC_KEY_BACKSPACE,
    PC_DEDC_KEY_DELETE,
    PC_DEDC_KEY_ENTER,
    PC_DEDC_KEY_CLEAR /* Escape / Ctrl+U: empties the line */
};

void pc_dedc_init(PCDedcEdit* e);
/* Applies one key. Returns 1 exactly when the key was ENTER: the finished line is copied (NUL-terminated, at most cap-1 characters) to line_out and the editor is emptied; 0 otherwise.
 * A line is therefore handed over once per ENTER, whatever was printed meanwhile. */
int  pc_dedc_key(PCDedcEdit* e, int key, char* line_out, size_t cap);

/* VT text (UTF-8 / ASCII) for the prompt line: "\r\x1b[2K" + prompt + the visible part of the line + cursor placement. width = console columns (clamped to 20..500); a line longer than the
 * window scrolls horizontally so the cursor is always visible, and the text never reaches the last column (no wrapping, so a one-row erase is always enough). Returns the length written
 * (always NUL-terminated, truncated to cap-1). */
size_t pc_dedc_render(const PCDedcEdit* e, const char* prompt, int width, char* out, size_t cap);
/* Text for one asynchronous write: erases the prompt line, writes `text` (a newline is added when missing), and - when the editor is shown (show_edit) - draws the prompt line again with the
 * partially typed command and the cursor exactly where they were. */
size_t pc_dedc_emit(const PCDedcEdit* e, const char* prompt, int width, int show_edit, const char* text, char* out, size_t cap);
/* Text that keeps a submitted command in the scrollback: "\r\x1b[2K" + prompt + line + "\n". */
size_t pc_dedc_commit_echo(const char* prompt, const char* line, char* out, size_t cap);

/* Console formatting of ONE log line (no newline in raw): "HH:MM:SS LEVEL  SUBSYSTEM   message". LEVEL is INFO / WARN / ERROR (decided from the line, see pc_dedc_classify), SUBSYSTEM is the line's
 * leading [TAG] (a second [TAG] becomes TAG/SUB), NETWORK for NET, DEDICATED for the server notices, GAME when there is none. A timestamp already in the line and the tag are NOT repeated. A
 * line that starts with whitespace is a continuation: it is indented under the message with no prefix. color != 0 wraps LEVEL and SUBSYSTEM in SGR colours (the word LEVEL is always printed,
 * colour is never the only signal). width > 0 wraps long messages at spaces with a hanging indent. Returns the length written. */
size_t pc_dedc_format(const char* raw, int hh, int mm, int ss, int color, int width, char* out, size_t cap);
/* 0 = INFO, 1 = WARN, 2 = ERROR (heuristic on the words of the line; a [TAG] such as [FATAL] counts) */
int    pc_dedc_classify(const char* raw);

#ifdef __cplusplus
}
#endif

#endif
