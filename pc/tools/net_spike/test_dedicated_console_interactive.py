#!/usr/bin/env python3
"""test_dedicated_console_interactive.py - REAL-PROCESS test of the dedicated server's INTERACTIVE console (Windows only).

The bug: the normal exe is a GUI-subsystem (-mwindows) process, so a shell returns its prompt at once; the dedicated server attached to the shell's console and
the shell and the server both read the keyboard (`status` went to PowerShell) and both wrote to the console (debug printf tore through the typed line).
The fix: an interactively started GUI-subsystem server opens its OWN console window, and while it is interactive only the [DEDICATED] stream (notices and
command output) is written to it; stdout / stderr go to NUL.

How: the exe is started WITHOUT any redirection (CREATE_NEW_CONSOLE, exactly what a hand launch looks like to it) in a disposable fixture dir; this test then
attaches to THAT console as a second process (ctypes: AttachConsole(pid)), TYPES commands into its input buffer (WriteConsoleInput) and reads the screen
buffer back (ReadConsoleOutputCharacter). Checks: the window/console exists and is the server's own, help / status / players / guests / save are answered by the
SERVER console, none of the game's debug noise ([NEOS_OUT], movement send rate, ...) is on the screen, the server keeps running while idle, and `stop` shuts it
down with exit code 0 (graceful path).
NOT covered: PowerShell itself (the shell-side behaviour is that nothing reads the keyboard any more because the server left the shell's console), and visual
rendering of the window. Usage: python test_dedicated_console_interactive.py"""
import ctypes
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
GAME_DIR = os.path.join(PC, "build64", "bin_fixture4_persist")  # disposable fixture only
PORT = 7861

results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class _U(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]


class INPUT_RECORD(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("EventType", wintypes.WORD), ("u", _U)]


KEY_EVENT = 0x0001
GENERIC_RW = 0xC0000000
OPEN_EXISTING = 3
SHARE_RW = 3
INVALID = ctypes.c_void_p(-1).value

k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]


def read_screen(pid):
    """All text currently in the child's console screen buffer ('' if it cannot be attached)."""
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        return ""
    try:
        out = k32.CreateFileW("CONOUT$", GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, 0, None)
        if out in (None, INVALID):
            return ""
        info = CSBI()
        if not k32.GetConsoleScreenBufferInfo(out, ctypes.byref(info)):
            k32.CloseHandle(out)
            return ""
        w, h = info.dwSize.X, info.dwSize.Y
        buf = ctypes.create_unicode_buffer(w)
        lines = []
        for y in range(min(h, info.dwCursorPosition.Y + 1)):
            got = wintypes.DWORD(0)
            k32.ReadConsoleOutputCharacterW(out, buf, w, COORD(0, y), ctypes.byref(got))
            lines.append(buf.value[:got.value].rstrip())
        k32.CloseHandle(out)
        return "\n".join(lines)
    finally:
        k32.FreeConsole()


def type_line(pid, text):
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        return False
    try:
        inp = k32.CreateFileW("CONIN$", GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, 0, None)
        if inp in (None, INVALID):
            return False
        recs = []
        for ch in text + "\r":
            for down in (1, 0):
                r = INPUT_RECORD()
                r.EventType = KEY_EVENT
                r.KeyEvent.bKeyDown = down
                r.KeyEvent.wRepeatCount = 1
                r.KeyEvent.uChar = ch
                r.KeyEvent.wVirtualKeyCode = 0x0D if ch == "\r" else 0
                recs.append(r)
        arr = (INPUT_RECORD * len(recs))(*recs)
        wrote = wintypes.DWORD(0)
        ok = k32.WriteConsoleInputW(inp, arr, len(recs), ctypes.byref(wrote))
        k32.CloseHandle(inp)
        return bool(ok)
    finally:
        k32.FreeConsole()


def wait_for(pid, needle, secs):
    end = time.time() + secs
    scr = ""
    while time.time() < end:
        scr = read_screen(pid)
        if needle in scr:
            return True, scr
        time.sleep(0.5)
    return False, scr


def main():
    if os.name != "nt":
        print("SKIP: Windows only")
        return 0
    exe = os.path.join(GAME_DIR, "AnimalCrossing.exe")
    norm = os.path.normcase(GAME_DIR)
    if not (norm.endswith("bin_fixture4_persist") and os.path.exists(exe)):
        print("REFUSING: this test only runs from the disposable fixture dir bin_fixture4_persist with a copied exe")
        return 2
    save = os.path.join(GAME_DIR, "save", "card_a", "DobutsunomoriP_MURA.gci")
    ref = os.path.join(PC, "build64", "bin_fixture4", "save", "card_a", "DobutsunomoriP_MURA.gci")
    shutil.copy2(save, os.path.join(HERE, "dedicated_console_interactive_save_backup.gci"))
    CREATE_NEW_CONSOLE = 0x00000010
    proc = subprocess.Popen([exe, "--host", str(PORT), "--dedicated"], cwd=GAME_DIR, creationflags=CREATE_NEW_CONSOLE)
    try:
        ok, scr = wait_for(proc.pid, "console input: reading commands", 60)
        check("the server opened ITS OWN console and printed its startup notices there (not in the launching shell)", ok)
        check("the console says commands are read from the server console", "(type help)" in scr)
        time.sleep(8)  # let the world come up; the game would be printing frame / send-rate debug lines by now
        scr = read_screen(proc.pid)
        noise = [m for m in ("[NEOS_OUT]", "movement send rate", "[NET][DIAG]", "FIELD][GUARD") if m in scr]
        check("none of the game's debug printf noise is on the server console (%s)" % noise, not noise)
        check("the server is still running while waiting for commands", proc.poll() is None)

        check("typing `help` is accepted by the console", type_line(proc.pid, "help"))
        ok, scr = wait_for(proc.pid, "[DEDICATED] commands (case-insensitive)", 20)
        check("`help` is answered by the SERVER console", ok)
        for cmd in ("status", "players", "guests"):
            type_line(proc.pid, cmd)
        ok1, scr = wait_for(proc.pid, "[DEDICATED] status", 20)
        ok2 = "no players connected" in scr or "[DEDICATED] players:" in scr
        ok3 = "[DEDICATED] guests" in scr
        check("`status` is answered by the server (status block incl. 'world ready')", ok1 and "world ready:" in scr)
        check("`players` is answered by the server", ok2)
        check("`guests` is answered by the server", ok3)
        check("none of the typed commands reached a shell ('is not recognized' / 'CommandNotFound' absent)", "not recognized" not in scr and "CommandNotFound" not in scr)

        type_line(proc.pid, "save")
        ok_req, scr = wait_for(proc.pid, "[DEDICATED] save: requested", 20)
        ok_res, scr = wait_for(proc.pid, "[DEDICATED] save: OK", 40) if ok_req else (False, scr)
        check("`save` is answered by the server console: 'save: requested' then 'save: OK'", ok_req and (ok_res or "[DEDICATED] save: refused:" in scr))
        check("the server is still running after the commands", proc.poll() is None)

        type_line(proc.pid, "stop")
        try:
            rc = proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            rc = None
        check("`stop` shuts the server down gracefully (exit code 0)", rc == 0)
    finally:
        if proc.poll() is None:
            proc.kill()
        k32.FreeConsole()
        # restore the disposable fixture save the server may have re-written (known-good copy)
        try:
            shutil.copy2(ref, save)
        except OSError:
            pass
        try:
            os.remove(os.path.join(HERE, "dedicated_console_interactive_save_backup.gci"))
        except OSError:
            pass
        shutil.rmtree(os.path.join(GAME_DIR, "save", "mp"), ignore_errors=True)

    failed = [d for d, ok in results if not ok]
    print("-" * 60)
    print("%d/%d checks passed" % (len(results) - len(failed), len(results)))
    for d in failed:
        print("FAILED: " + d)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
