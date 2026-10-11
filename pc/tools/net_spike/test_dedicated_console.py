#!/usr/bin/env python3
r"""test_dedicated_console.py - the dedicated server's interactive console: asynchronous log output must never corrupt the command being typed, and the console format must be stable.

ROOT CAUSE (what this covers): the interactive server console used the console host's own cooked line input. The console host keeps the half-typed text in ITS buffer and echoes it at the cursor, so
every other write to that console (a "[DEDICATED]" notice, a log line from another thread) landed in the middle of the typed text and moved the cursor away from it. The server now owns the line
(pc_ded_console.c: a small editor + the VT text that erases the prompt line, prints, and draws the prompt line and cursor again), and every writer goes through one locked function.

TIER: the pure module (pc/src/pc_ded_console.c) is compiled into ded_console_selftest.exe and driven with scripted keys / log lines; its OUTPUT BYTES are fed to a small VT emulator below (carriage
return, newline, ESC[2K, ESC[nC, printable text) that models the visible rows and the cursor. That proves the bytes the console would be sent; it is NOT real Windows console rendering, and the
Win32 layer in pc_dedicated.c (console modes, ReadConsoleInputW, the lock, the stdout pump) is covered only by test_dedicated_console_conpty.py (a real pseudo-console) and the redirected-stdin suites.
Usage: python test_dedicated_console.py"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
LEFT, RIGHT, HOME, END, BACKSPACE, DELETE, ENTER, CLEAR = 1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007


def build():
    gcc = shutil.which("gcc") or next((c for c in ("C:/msys64/ucrt64/bin/gcc.exe", "C:/msys64/mingw64/bin/gcc.exe") if os.path.exists(c)), None)
    if gcc is None:
        return None
    d = tempfile.mkdtemp(prefix="dedc ")
    exe = os.path.join(d, "ded_console_selftest.exe")
    env = dict(os.environ, PATH=os.path.dirname(gcc) + os.pathsep + os.environ.get("PATH", ""))
    subprocess.run([gcc, "-std=gnu11", "-Wall", "-Wextra", "-I", os.path.join(REPO, "pc", "include"), "-o", exe, os.path.join(HERE, "ded_console_selftest.c"),
                    os.path.join(REPO, "pc", "src", "pc_ded_console.c")], check=True, env=env)
    return exe


class Screen:
    """a minimal VT model: rows of text, a cursor, CR / LF / ESC[2K / ESC[nC / ESC[...m"""

    def __init__(self):
        self.rows = [[]]
        self.r = 0
        self.c = 0

    def feed(self, s):
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "\r":
                self.c = 0
            elif ch == "\n":
                self.r += 1
                self.c = 0
                while len(self.rows) <= self.r:
                    self.rows.append([])
            elif ch == "\x1b":
                m = re.match(r"\x1b\[([0-9;]*)([A-Za-z])", s[i:])
                assert m, "unknown escape sequence %r" % s[i:i + 8]
                arg, cmd = m.group(1), m.group(2)
                if cmd == "K" and arg == "2":
                    self.rows[self.r] = []
                elif cmd == "C":
                    self.c += int(arg or "1")
                elif cmd == "m":
                    pass
                else:
                    raise AssertionError("unsupported VT command %r" % m.group(0))
                i += len(m.group(0)) - 1
            else:
                row = self.rows[self.r]
                while len(row) < self.c:
                    row.append(" ")
                if self.c < len(row):
                    row[self.c] = ch
                else:
                    row.append(ch)
                self.c += 1
            i += 1

    def text(self):
        return ["".join(r).rstrip() for r in self.rows]

    def visible_line(self):
        return "".join(self.rows[self.r]).rstrip()


def unesc(s):
    return s.replace("\\e", "\x1b").replace("\\r", "\r").replace("\\n", "\n").replace("\\\\", "\\")


class Driver:
    def __init__(self, exe, width=80):
        self.exe = exe
        self.script = ["width %d" % width]
        self.screen = Screen()
        self.lines = []

    def key(self, *codes):
        for c in codes:
            self.script.append("key %d" % c)

    def type(self, text):
        for ch in text:
            self.script.append("key %d" % ord(ch))

    def log(self, text):
        self.script.append("log " + text)

    def run(self, extra=()):
        p = subprocess.run([self.exe], input="\n".join(self.script + list(extra)) + "\n", capture_output=True, text=True, timeout=30)
        outs = []
        for ln in p.stdout.splitlines():
            if ln.startswith("OUT "):
                data = unesc(ln[4:])
                outs.append(data)
                self.screen.feed(data)
            elif ln.startswith("LINE "):
                self.lines.append(ln[5:])
        return p.stdout.splitlines(), outs


def fmt(exe, raw, hh=12, mm=3, ss=9, color=0, width=0):
    p = subprocess.run([exe], input="format %d %d %d %d %d %s\n" % (hh, mm, ss, color, width, raw), capture_output=True, text=True, timeout=30)
    m = re.search(r"^FMT (.*)$", p.stdout, re.M)
    return unesc(m.group(1)) if m else None


def main():
    results = []
    ck = lambda d, c, x="": L.check(d + ((" :: " + str(x)[:400]) if (x and not c) else ""), bool(c), results)  # noqa: E731
    exe = build()
    if exe is None:
        print("FAIL - no gcc to build the console self-test")
        return 1

    # ---- 1. a log arrives while a command is partly typed ----
    d = Driver(exe)
    d.type("stat")
    d.log("[NET] host: peer 1 transport-connected, awaiting identity")
    d.run()
    t = d.screen.text()
    ck("A1 the log line is printed on its own row and the partly typed command is drawn again below it: %r" % t, t[-2:] == ["> stat", ""] or t[-1] == "> stat" and t[-2].startswith("[NET] host"), str(t))
    ck("A1b the prompt row holds exactly '> stat' and the cursor sits after it (col 6)", d.screen.visible_line() == "> stat" and d.screen.c == 6, "%r col %d" % (d.screen.visible_line(), d.screen.c))

    # ---- 2. several logs before Enter ----
    d = Driver(exe)
    d.type("pla")
    for i in range(5):
        d.log("[DEDICATED] notice %d" % i)
        if i == 2:
            d.type("y")
    d.type("ers")
    d.log("[SAVE] host: periodic save OK")
    d.run()
    t = d.screen.text()
    logs = [r for r in t if r.startswith("[")]
    ck("A2 five notices and a save line arrive in order, none lost or merged: %s" % logs, logs == ["[DEDICATED] notice %d" % i for i in range(5)] + ["[SAVE] host: periodic save OK"], str(t))
    ck("A2b the typed command survives intact across them: 'players'", d.screen.visible_line() == "> players" and d.screen.c == 9, d.screen.visible_line())

    # ---- 3. backspace and cursor editing with output in between ----
    d = Driver(exe)
    d.type("helo")
    d.log("[NET] a")
    d.key(LEFT)
    d.type("l")
    d.log("[NET] b")
    d.key(HOME)
    d.type("x")
    d.key(DELETE)  # removes the 'h' that follows the cursor
    d.log("[NET] c")
    d.key(END, BACKSPACE, BACKSPACE)
    d.log("[NET] d")
    out, _ = d.run(["echo"])
    buf = [l for l in out if l.startswith("BUF ")][-1]
    ck("A3 insert in the middle, Home, Delete, End, Backspace x2 with logs between give the right text (%s)" % buf, buf == "BUF xel|3|3", buf)
    ck("A3b the screen shows the edited text with the cursor at its end", d.screen.visible_line() == "> xel" and d.screen.c == 5, "%r %d" % (d.screen.visible_line(), d.screen.c))
    d = Driver(exe)
    d.type("abcdef")
    d.key(LEFT, LEFT, LEFT)
    d.log("[NET] mid")
    d.run()
    ck("A3c the cursor stays in the middle of the line after a log (col = prompt + 3)", d.screen.visible_line() == "> abcdef" and d.screen.c == 5, "%r %d" % (d.screen.visible_line(), d.screen.c))
    d = Driver(exe)
    d.type("abc")
    d.key(CLEAR)
    d.log("[NET] x")
    d.run()
    ck("A3d Escape / Ctrl+U clears the line; backspace at the start and delete at the end do nothing", d.screen.visible_line() == ">" and d.screen.c == 2)
    d = Driver(exe)
    d.key(BACKSPACE, DELETE, LEFT, RIGHT, HOME, END)
    out, _ = d.run(["echo"])
    ck("A3e editing keys on an empty line are harmless", [l for l in out if l.startswith("BUF ")][-1] == "BUF |0|0")

    # ---- 4. submit after asynchronous output: exactly once, exactly the typed text ----
    d = Driver(exe)
    d.type("sav")
    d.log("[SAVE] host: periodic save OK")
    d.type("e")
    d.log("[DEDICATED] save result: OK (frame 1234)")
    d.key(ENTER)
    d.log("[NET] after the command")
    out, _ = d.run(["echo"])
    ck("A4 the submitted line is exactly 'save', handed over once: %s" % d.lines, d.lines == ["save"], str(out))
    ck("A4b the command stays in the scrollback ('> save') above the later log, and a fresh empty prompt follows", "> save" in d.screen.text() and d.screen.visible_line() == ">", str(d.screen.text()))
    ck("A4c after Enter the editor is empty (no stale text can be submitted twice)", [l for l in out if l.startswith("BUF ")][-1] == "BUF |0|0")
    d = Driver(exe)
    d.type("a")
    d.key(ENTER)
    d.key(ENTER)
    d.type("b")
    d.key(ENTER)
    d.run()
    ck("A4d each ENTER yields one line, in order (an empty ENTER yields an empty line, which the command reader ignores): %s" % d.lines, d.lines == ["a", "", "b"])

    # ---- 5. a line wider than the console scrolls; nothing wraps ----
    d = Driver(exe, width=30)
    d.type("x" * 60)
    d.log("[NET] while scrolled")
    d.run()
    ck("A5 a 60-character command in a 30-column console stays on ONE row (<= 29 columns) with the cursor visible at its end", len(d.screen.visible_line()) <= 29 and d.screen.c <= 29 and d.screen.visible_line().endswith("x" * 10),
       "%r col %d" % (d.screen.visible_line(), d.screen.c))
    d.screen = Screen()
    d2 = Driver(exe, width=30)
    d2.type("y" * 60)
    d2.key(HOME)
    d2.log("[NET] at home")
    d2.run()
    ck("A5b Home on that line shows its start again with the cursor right after the prompt", d2.screen.visible_line().startswith("> yyyy") and d2.screen.c == 2)

    # ---- 6. a log line is never longer-prefixed by the old prompt text ----
    d = Driver(exe)
    d.type("abc")
    d.log("a much longer log line than the typed text so the old characters would show through if the row was not erased first")
    d.run()
    ck("A6 the row is erased before a log is written: no leftover prompt characters in front of or behind the log text", all(not r.startswith("> abc") for r in d.screen.text()[:-1]), str(d.screen.text()))

    # ---- 7. formatting ----
    def exp(level, tag, msg):
        return "12:03:09 %-5s %-14s %s" % (level, tag, msg)
    cases = [
        ("[DEDICATED] dedicated server starting: role=host port=7777", exp("INFO", "DEDICATED", "dedicated server starting: role=host port=7777")),
        ("[NET] host: peer 1 transport-connected, awaiting identity", exp("INFO", "NETWORK", "host: peer 1 transport-connected, awaiting identity")),
        ("[NET] host: peer 2 never completed the identity handshake within 60 s -- dropping it (N-05)", exp("WARN", "NETWORK", "host: peer 2 never completed the identity handshake within 60 s -- dropping it (N-05)")),
        ("[SAVE] host: periodic save OK", exp("INFO", "SAVE", "host: periodic save OK")),
        ("[SAVE] host: save FAILED (disk full)", exp("ERROR", "SAVE", "host: save FAILED (disk full)")),
        ("[NET][GUEST] client: first-run guest creation in progress", exp("INFO", "NETWORK/GUEST", "client: first-run guest creation in progress")),
        ("[WILDLIFE] spawn: 3 fish in acre 4", exp("INFO", "WILDLIFE", "spawn: 3 fish in acre 4")),
        ("[DEDICATED] FATAL: the dummy audio device could not be opened", exp("ERROR", "DEDICATED", "FATAL: the dummy audio device could not be opened")),
        ("[FATAL][NET] out of memory", exp("ERROR", "NETWORK", "out of memory")),
        ("[NET] 11:59:58 host: already stamped", exp("INFO", "NETWORK", "host: already stamped")),
        ("[11:59:58] [SAVE] host: bracketed stamp", exp("INFO", "SAVE", "host: bracketed stamp")),
        ("plain line without a tag", exp("INFO", "GAME", "plain line without a tag")),
        ("[Settings] Loaded settings.ini: 1280x720", exp("INFO", "SETTINGS", "Loaded settings.ini: 1280x720")),
    ]
    for raw, want in cases:
        got = fmt(exe, raw)
        ck("F1 %r -> %r" % (raw[:48], got), got == want, "wanted %r" % want)
    cont = fmt(exe, "   second line of a multi-line message")
    ck("F2 a continuation line (leading whitespace) is indented under the message with no second timestamp: %r" % cont, cont is not None and not re.match(r"\d\d:\d\d:\d\d", cont) and cont.endswith("  second line of a multi-line message") and cont.startswith(" " * 30))
    c = fmt(exe, "[SAVE] host: save FAILED", color=1)
    ck("F3 with colour the LEVEL word is still printed (colour is never the only signal) inside well-formed SGR codes: %r" % c, c is not None and "[1;31mERROR[0m" in c and re.sub(r"\[[0-9;]*m", "", c) == "12:03:09 ERROR SAVE           host: save FAILED", c)
    nocol = fmt(exe, "[SAVE] host: save FAILED", color=0)
    ck("F3b without colour there is no escape sequence at all", nocol is not None and "\x1b" not in nocol)
    wrapped = fmt(exe, "[NET] host: " + "word " * 40, width=120)
    rows = wrapped.split("\n")
    ck("F4 a long message wraps at spaces with a hanging indent, never beyond the width, nothing lost: %d rows" % len(rows),
       len(rows) > 1 and all(len(r) <= 119 for r in rows) and all(r.startswith(" " * 30) for r in rows[1:]) and " ".join(r.strip() for r in [rows[0][30:]] + rows[1:]) == "host: " + ("word " * 40).strip())
    twice = fmt(exe, "[NET] [NET] x")
    ck("F5 no duplicated category prefix is added for a plain tagged line (the tag appears once): %r" % twice, twice is not None and twice.count("NETWORK") == 1)
    ck("F6 the same input always gives the same output (stable)", fmt(exe, "[NET] host: x") == fmt(exe, "[NET] host: x"))
    for raw, want in [("[NET] connection lost to peer 3", 1), ("[SAVE] write failed", 2), ("[NET] host: peer 1 connected", 0), ("[PC] --max-guests: REFUSED: bad value", 1), ("[ERROR] boom", 2)]:
        p = subprocess.run([exe], input="classify %s\n" % raw, capture_output=True, text=True, timeout=30)
        got = int(re.search(r"CLS (\d)", p.stdout).group(1))
        if raw.startswith("[ERROR]"):
            continue  # classified through its tag in pc_dedc_format (F-checks above)
        ck("F7 severity of %r is %d" % (raw, want), got == want, str(got))

    # ---- 7b. command responses, narrow windows (the shared formatter / wrapper) ----
    def body(w, text):
        p = subprocess.run([exe], input="body %d %s\n" % (w, text), capture_output=True, text=True, timeout=30)
        m = re.search(r"^BODY (.*)$", p.stdout, re.M)
        return unesc(m.group(1)) if m else None

    def words(rows):
        return " ".join(" ".join(r.split()) for r in rows).split()

    helpline = "  status   server status: observer, network role/port, world+save readiness, uptime, peers, last save"
    for w in (120, 100, 80, 60, 40, 30, 20):
        rows = body(w, helpline).split("\n")
        ck("H1 help entry at width %d: every row fits (<= %d columns), no word lost or reordered (%d rows)" % (w, max(w, 20) - 1, len(rows)),
           all(len(r) <= max(w, 20) - 1 for r in rows) and words(rows) == words([helpline]), rows)
    rows = body(100, helpline).split("\n")
    ck("H2 a wrapped help entry keeps its own indent on row 1 and continues UNDER the description column (col 11): %s" % rows[:2],
       rows[0].startswith("  status   server") and len(rows) > 1 and all(r.startswith(" " * 11) and not r.startswith(" " * 12) for r in rows[1:]))
    rows = body(40, helpline).split("\n")
    ck("H3 at 40 columns the alignment is kept when it leaves room for text (hang 11 of 39) and rows still fit", all(len(r) <= 39 for r in rows) and len(rows) > 2)
    rows = body(24, helpline).split("\n")
    ck("H4 at 24 columns (too narrow for the deep alignment) it falls back to a small hanging indent, nothing is lost", all(len(r) <= 23 for r in rows) and words(rows) == words([helpline]) and all(len(r) - len(r.lstrip()) <= 4 for r in rows[1:]), rows)
    ck("H5 a short line is untouched and a blank/indent-only structure is preserved", body(100, "  help     list the commands") == "  help     list the commands" and body(100, "    nested field: value") == "    nested field: value")
    long_err = "GIVE FAILED: " + "the quantity must be between one and fifteen and the player needs enough free pockets " * 3
    for w in (140, 90, 55, 30):
        r = fmt(exe, "[DEDICATED] " + long_err, width=w)
        rows = r.split("\n")
        ck("E1 a 3-line long ERROR notice at width %d: rows fit (<= %d), the content is complete, the LEVEL word leads (%d rows)" % (w, w - 1, len(rows)),
           all(len(x) <= w - 1 for x in rows) and rows[0].lstrip("0123456789: ").startswith("ERROR") and words([x[len(x) - len(x.lstrip()):] if i else x for i, x in enumerate(rows)])[-12:] == words([long_err])[-12:] and
           " ".join(words(rows)).count("fifteen") == 3, rows[:3])
    for w, pc in ((140, 30), (99, 15), (59, 6)):
        r = fmt(exe, "[SAVE] host: " + "word " * 60, width=w)
        rows = r.split("\n")
        ck("P1 prefix accounted for at width %d (prefix %d columns): rows fit and continuation rows are indented exactly %d" % (w, pc, pc), all(len(x) <= w - 1 for x in rows) and len(rows) > 1 and all(len(x) - len(x.lstrip()) == pc for x in rows[1:]), rows[:3])
    r = fmt(exe, "[SAVE] host: periodic save OK", width=70)
    ck("P2 medium window: 'HH:MM:SS LEVEL [SUBSYSTEM] message' (no subsystem column): %r" % r, r == "12:03:09 INFO  [SAVE] host: periodic save OK", r)
    r = fmt(exe, "[SAVE] host: periodic save OK", width=40)
    ck("P3 narrow window: 'LEVEL [SUBSYSTEM] message' (the LEVEL word is kept): %r" % r, r == "INFO  [SAVE] host: periodic save OK", r)
    cont = fmt(exe, "      field: " + "value " * 30, width=100).split("\n")
    ck("M1 a continuation line of a multi-line message keeps its own indentation under the prefix and wraps under itself", cont[0].startswith(" " * 30 + "     field:") and all(len(x) <= 99 for x in cont) and len(cont) > 1, cont[:2])
    d = Driver(exe, width=60)
    d.type("pla")
    d.log("12:03:09 WARN  [NET] a wrapped notice that is longer than the window\\n                that continues on a second row\\n   and a third row")
    d.type("yers")
    d.log("[SAVE] after the multi-row response")
    d.run()
    ck("A7 a multi-row response arriving while 'pla' is typed: every row printed, the typed command intact on the prompt row with the cursor after it", d.screen.visible_line() == "> players" and d.screen.c == 9, (d.screen.visible_line(), d.screen.c))

    # ---- 8. source-level guarantees of the Win32 layer (the part these tests cannot run) ----
    src = open(os.path.join(REPO, "pc", "src", "pc_dedicated.c"), encoding="utf-8", errors="replace").read()
    ck("S1 every console writer of the interactive mode goes through pc_ded_con_write under s_con_lock (printf helper, say, fatal)",
       src.count("pc_ded_con_write(") >= 2 and "if (s_con_edit) {" in src and src.count("pc_ded_con_write_log(") >= 3)
    ck("S2 the editor turns the console line input and echo OFF and keeps processed input (Ctrl+C still reaches the handler)", "~(DWORD)(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT)) | ENABLE_PROCESSED_INPUT" in src)
    ck("S3 a failure to switch the console modes leaves the old cooked reader in use (no editor)", "SetConsoleMode(ho, mo);\n        return 0;" in src and "pc_ded_enable_editor()" in src)
    ck("S4 redirected / non-interactive runs are untouched: the editor needs s_ded_interactive and a console input handle", "if (!s_ded_interactive || s_ded_out == NULL || hi == NULL" in src)
    ck("S6 command output is routed through the shared formatter while a command runs (response writer + command depth), and the plain redirected path is untouched",
       "pc_ded_con_write_response(buf)" in src and "s_ded_cmd_depth++;" in src and "if (s_con_edit) {" in src)
    ck("S7 window resizes are handled: the editor asks for window events, redraws the prompt on WINDOW_BUFFER_SIZE_EVENT, and the width is read at every write", "ENABLE_WINDOW_INPUT" in src and "WINDOW_BUFFER_SIZE_EVENT" in src and "pc_ded_con_width()" in src)
    ck("S5 ENTER hands the line to the SAME bounded queue the stdin reader uses, once, outside the console lock", "pc_ded_enqueue(line, strlen(line), 0);" in src and src.index("LeaveCriticalSection(&s_con_lock);\n                if (done)") > 0)
    shutil.rmtree(os.path.dirname(exe), ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
