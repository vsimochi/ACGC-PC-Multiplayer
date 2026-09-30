#!/usr/bin/env python3
"""game_input.py - drives a locally launched AnimalCrossing.exe window with keyboard input
(Windows only, ctypes; no third-party packages).

Why: protocol v2 only serves world state once the HOST is in real gameplay (the title demo is
deliberately excluded -- pcfa_save_ready()/pcnetgame_update_local_world_ready()), and the game has
no auto-start flag, so the test host must be walked from the title screen into the town. The game
reads the keyboard through SDL_GetKeyboardState() (pc_pad.c), which only reflects input delivered
to the FOCUSED window, so each press focuses the host window first and injects scancodes with
SendInput. Only ever targets a window owned by a process this harness started (by PID).

Key names follow keybindings.ini (A=Space, B=Left Shift, Start=Return, stick = W/A/S/D).
"""
import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SCANCODES = {
    "Return": 0x1C, "Space": 0x39, "Left Shift": 0x2A, "Escape": 0x01,
    "W": 0x11, "A": 0x1E, "S": 0x1F, "D": 0x20, "X": 0x2D, "Y": 0x15, "Z": 0x2C, "Q": 0x10, "E": 0x12,
}
# GameCube button -> keyboard key (keybindings.ini defaults)
BUTTONS = {"A": "Space", "B": "Left Shift", "START": "Return", "UP": "W", "DOWN": "S", "LEFT": "A", "RIGHT": "D"}

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
SW_RESTORE = 9
VK_MENU = 0x12


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def find_window(pid):
    found = []

    def cb(hwnd, _lp):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else None


def focus(hwnd):
    if user32.GetForegroundWindow() == hwnd:
        return True
    user32.ShowWindow(hwnd, SW_RESTORE)
    # ALT tap lets a background process take the foreground (SetForegroundWindow rules) -- works on a
    # normal interactive desktop where this harness's own controlling process has recent input focus.
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    if user32.GetForegroundWindow() == hwnd:
        return True
    # Fallback (villager population/is_home milestone verification pass): in an agent-orchestrated /
    # automation terminal session, this harness's own process never received real user-driven input
    # focus, so the ALT-tap trick above does not satisfy SetForegroundWindow's caller-eligibility rules
    # and it silently no-ops -- confirmed by direct diagnosis (GetForegroundWindow unchanged after the
    # call). AttachThreadInput() temporarily joins this thread's input queue with the CURRENT
    # foreground thread's, which is a documented, legitimate way to become eligible to call
    # SetForegroundWindow (see Win32 SetForegroundWindow docs, "AttachThreadInput" case). Detached again
    # immediately after, win or lose, so this thread's input state is never left shared.
    fg_before = user32.GetForegroundWindow()
    fg_pid = wt.DWORD()
    fg_tid = user32.GetWindowThreadProcessId(fg_before, ctypes.byref(fg_pid)) if fg_before else 0
    my_tid = kernel32.GetCurrentThreadId()
    attached = False
    if fg_tid and fg_tid != my_tid:
        attached = bool(user32.AttachThreadInput(my_tid, fg_tid, True))
    try:
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.05)
    finally:
        if attached:
            user32.AttachThreadInput(my_tid, fg_tid, False)
    return user32.GetForegroundWindow() == hwnd


def _send_scan(sc, up):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(0, sc, KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


class GameWindow:
    def __init__(self, pid):
        self.pid = pid
        self.hwnd = None

    def ready(self):
        if self.hwnd is None:
            self.hwnd = find_window(self.pid)
        return self.hwnd is not None

    def press(self, button, hold=0.12, after=0.25):
        """Press a GameCube button (A, B, START, UP, DOWN, LEFT, RIGHT) or a raw key name."""
        key = BUTTONS.get(button, button)
        sc = SCANCODES[key]
        if not self.ready():
            raise RuntimeError(f"no window for pid {self.pid}")
        focus(self.hwnd)
        _send_scan(sc, False)
        time.sleep(hold)
        _send_scan(sc, True)
        time.sleep(after)

    def hold(self, button, seconds):
        self.press(button, hold=seconds, after=0.1)

    def close(self):
        """Politely asks the window to close (WM_CLOSE -> SDL_QUIT -> the game's normal shutdown)."""
        if not self.ready():
            return False
        WM_CLOSE = 0x0010
        return bool(user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0))

    def screenshot(self, path):
        """Saves the host window's on-screen pixels as a PNG (diagnostics for boot navigation)."""
        if not self.ready():
            raise RuntimeError(f"no window for pid {self.pid}")
        focus(self.hwnd)
        return capture_window_png(self.hwnd, path)


gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def capture_window_png(hwnd, path, scale=2):
    import struct
    import zlib

    r = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = r.right - r.left, r.bottom - r.top
    sdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    gdi32.BitBlt(mdc, 0, 0, w, h, sdc, pt.x, pt.y, 0x00CC0020)  # SRCCOPY
    bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(None, sdc)
    raw = buf.raw
    ow, oh = w // scale, h // scale
    rows = []
    for y in range(0, oh):
        row = bytearray(b"\x00")
        base = y * scale * w * 4
        for x in range(0, ow):
            i = base + x * scale * 4
            row += bytes((raw[i + 2], raw[i + 1], raw[i]))
        rows.append(bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", ow, oh, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    return path
