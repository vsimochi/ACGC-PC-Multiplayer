#!/usr/bin/env python3
r"""ded_con_helper.py - attaches to ANOTHER process's console (the dedicated server's own console window) to read its screen buffer or to write input records into its input buffer.
This is the console-API equivalent of looking at the window and typing into it, WITHOUT the OS keyboard / mouse / focus (WriteConsoleInputW queues key events straight into that console's input buffer;
the server reads them through the same ReadConsoleInputW it uses for real keystrokes). Runs as its own short-lived process because AttachConsole changes the calling process's console.
Usage:  python ded_con_helper.py PID read OUT.json
        python ded_con_helper.py PID send "text with {BS} {LEFT} {RIGHT} {HOME} {END} {DEL} {ENTER} {ESC} tokens"  """
import ctypes
import json
import sys
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_RW = 3
OPEN_EXISTING = 3
KEY_EVENT = 1
VK = {"BS": 0x08, "ENTER": 0x0D, "ESC": 0x1B, "END": 0x23, "HOME": 0x24, "LEFT": 0x25, "RIGHT": 0x27, "DEL": 0x2E}


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD), ("uChar", wintypes.WCHAR),
                ("dwControlKeyState", wintypes.DWORD)]


class _EV(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EV)]


def attach(pid):
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        raise OSError("AttachConsole(%d) failed: %d" % (pid, ctypes.get_last_error()))


def open_dev(name, access):
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(name, access, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
    if h in (None, wintypes.HANDLE(-1).value, -1):
        raise OSError("open %s failed: %d" % (name, ctypes.get_last_error()))
    return wintypes.HANDLE(h)


def read_screen(out_path):
    h = open_dev("CONOUT$", GENERIC_READ | GENERIC_WRITE)
    ci = CSBI()
    k32.GetConsoleScreenBufferInfo(h, ctypes.byref(ci))
    width = ci.dwSize.X
    last = ci.dwCursorPosition.Y
    rows = []
    buf = ctypes.create_unicode_buffer(width + 1)
    got = wintypes.DWORD()
    for y in range(0, last + 1):
        k32.ReadConsoleOutputCharacterW(h, buf, width, COORD(0, y), ctypes.byref(got))
        rows.append(buf.value[:got.value].rstrip())
    json.dump({"rows": rows, "cx": ci.dwCursorPosition.X, "cy": ci.dwCursorPosition.Y, "width": width}, open(out_path, "w"))


def send(spec):
    h = open_dev("CONIN$", GENERIC_READ | GENERIC_WRITE)
    recs = []

    def key(vk, ch):
        for down in (1, 0):
            r = INPUT_RECORD()
            r.EventType = KEY_EVENT
            r.Event.KeyEvent.bKeyDown = down
            r.Event.KeyEvent.wRepeatCount = 1
            r.Event.KeyEvent.wVirtualKeyCode = vk
            r.Event.KeyEvent.wVirtualScanCode = 0
            r.Event.KeyEvent.uChar = ch
            r.Event.KeyEvent.dwControlKeyState = 0
            recs.append(r)
    i = 0
    while i < len(spec):
        if spec[i] == "{":
            j = spec.index("}", i)
            name = spec[i + 1:j]
            key(VK[name], "\r" if name == "ENTER" else "\0")
            i = j + 1
        else:
            c = spec[i]
            key(ord(c.upper()) if c.isalnum() else 0, c)
            i += 1
    arr = (INPUT_RECORD * len(recs))(*recs)
    n = wintypes.DWORD()
    if not k32.WriteConsoleInputW(h, arr, len(recs), ctypes.byref(n)) or n.value != len(recs):
        raise OSError("WriteConsoleInputW wrote %d of %d" % (n.value, len(recs)))


if __name__ == "__main__":
    pid = int(sys.argv[1])
    attach(pid)
    if sys.argv[2] == "read":
        read_screen(sys.argv[3])
    else:
        send(sys.argv[3])
