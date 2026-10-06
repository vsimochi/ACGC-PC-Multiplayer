#!/usr/bin/env python3
"""nook_visual_ctl.py - companion of nook_visual_rig.py: sends synthetic keys to, and captures the client rectangle of, the rig's GAME window only.

  shot NAME [SCALE]        save <log dir>/nk_NAME.png: the CLIENT area of the game window only (nothing else of the desktop is captured); refuses when the window is not foreground
  key K [K ...]            tap keys (A B START UP DOWN LEFT RIGHT, or a raw key name); each tap is preceded by a foreground check (the game window must be the foreground window)
  hold K SECONDS           hold one key
  info                     window rectangle (for the monitor sanity check) + foreground state
Keys are keybd/SendInput scancodes (game_input.py), the same technique as the earlier boot-navigation harnesses.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402
import game_input as G  # noqa: E402

state = json.load(open(T.log_path("nk_state.json")))
gw = G.GameWindow(state["pid"])
if not gw.ready():
    print("no game window for pid %s" % state["pid"])
    sys.exit(3)
G.focus(gw.hwnd)
fg = G.user32.GetForegroundWindow() == gw.hwnd


def need_fg():
    G.focus(gw.hwnd)
    if G.user32.GetForegroundWindow() != gw.hwnd:
        print("REFUSED: the game window is not the foreground window")
        sys.exit(5)


cmd = sys.argv[1]
if cmd == "info":
    r = G.RECT()
    G.user32.GetWindowRect(gw.hwnd, G.ctypes.byref(r))
    print("pid %d hwnd %s window rect (%d,%d)-(%d,%d) foreground=%s" % (state["pid"], gw.hwnd, r.left, r.top, r.right, r.bottom, fg))
elif cmd == "shot":
    need_fg()
    scale = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    path = T.log_path("nk_%s.png" % sys.argv[2])
    G.capture_window_png(gw.hwnd, path, scale=scale)
    print(path)
elif cmd == "key":
    for k in sys.argv[2:]:
        need_fg()
        gw.press(k, hold=0.12, after=0.35)
    print("ok")
elif cmd == "hold":
    need_fg()
    gw.hold(sys.argv[2], float(sys.argv[3]))
    print("ok")
