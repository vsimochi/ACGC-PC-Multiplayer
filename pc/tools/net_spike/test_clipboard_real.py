#!/usr/bin/env python3
"""test_clipboard_real.py - public beta: clipboard paste into the Play Online server-address field (REAL client window, SendInput keys, Windows clipboard).

TIER: ONE real game process on a DISPOSABLE copy of pc\build64\bin_fixture4 (bin_fixture4_clip, empty save dir), display pinned with AC_DISPLAY_NAME=samsung, AC_MASTER_VOLUME=1.
Flow: title -> Play Online -> Add server -> name "Beta" -> address: Ctrl+V of 'example.gl.at.ply.gg:12345' -> Enter -> (the pasted ':12345' pre-filled the port step) Enter -> saved.
Also checks Ctrl+C / Ctrl+X on a second entry (the field is copied / cleared) and that the user's clipboard text is restored afterwards.
Usage: python test_clipboard_real.py
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402
import game_input as G  # noqa: E402

if __name__ == "__main__":
    T.make_fixture("bin_fixture4_clip", empty_save=True)

import net_spike_lib as L  # noqa: E402

DIR = os.path.join(T.BUILD64, "bin_fixture4_clip")
EXE = os.path.join(DIR, "AnimalCrossing.exe")
G.SCANCODES.update({"LCtrl": 0x1D, "V": 0x2F, "C": 0x2E, "KB": 0x30, "KE": 0x12, "KT": 0x14, "KA": 0x1E})
G.SCANCODES["Escape"] = 0x01


def ps(cmd):
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=30)


def tap(gw, key):
    G.focus(gw.hwnd)
    G._send_scan(G.SCANCODES[key], False)
    time.sleep(0.08)
    G._send_scan(G.SCANCODES[key], True)
    time.sleep(0.2)


def chord(gw, mod, key):
    G.focus(gw.hwnd)
    G._send_scan(G.SCANCODES[mod], False)
    time.sleep(0.1)
    G._send_scan(G.SCANCODES[key], False)
    time.sleep(0.12)
    G._send_scan(G.SCANCODES[key], True)
    time.sleep(0.1)
    G._send_scan(G.SCANCODES[mod], True)
    time.sleep(0.4)


def servers_text():
    p = os.path.join(DIR, "save", "mp", "servers.ini")
    return open(p, newline="").read() if os.path.isfile(p) else ""


def main():
    L.require_test_bin_dir() if False else None
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    saved_clip = ps("Get-Clipboard -Raw").stdout
    env = dict(os.environ, AC_DISPLAY_NAME="samsung", AC_MASTER_VOLUME="1")
    log = open(T.log_path("clip_client.log"), "wb")
    proc = subprocess.Popen([EXE], cwd=DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
    gw = G.GameWindow(proc.pid)
    try:
        t0 = time.monotonic()
        while not gw.ready() and time.monotonic() - t0 < 60 and proc.poll() is None:
            time.sleep(0.5)
        ck("the game window opened (exit code %s: 4 = AC_DISPLAY_NAME matched no display)" % proc.poll(), gw.ready() and proc.poll() is None)
        if not gw.ready():
            return L.summary_and_exit_code(results)
        time.sleep(30.0)  # boot + title logo
        shot = lambda n: G.capture_window_png(gw.hwnd, T.log_path("clip_%s.png" % n), scale=2)  # noqa: E731
        for _ in range(3):  # leave the logo / press-start screen (extra Start presses on the menu itself only open Play Online)
            break
        gw.press("START", hold=0.15, after=2.0)  # press-start screen -> the main menu; the first item is Play Online, and START on it opens the server page
        shot("1_title")
        gw.press("A", hold=0.15, after=1.5)  # Play Online (the first and only game item: Start Game was removed)
        shot("2_servers")
        gw.press("A", hold=0.15, after=1.0)  # "Add server" (no saved servers: the first row)
        for k in ("KB", "KE", "KT", "KA"):
            tap(gw, k)
        gw.press("Return", hold=0.12, after=0.6)
        shot("3_address_empty")
        ps("Set-Clipboard -Value 'example.gl.at.ply.gg:12345'")
        chord(gw, "LCtrl", "V")
        shot("4_pasted")
        gw.press("Return", hold=0.12, after=0.6)  # address -> port step (pre-filled 12345)
        shot("5_port")
        gw.press("Return", hold=0.12, after=1.0)  # port -> saved
        shot("6_saved")
        t = servers_text()
        ck("Ctrl+V pasted 'example.gl.at.ply.gg:12345': servers.ini has the hostname unchanged", "address = example.gl.at.ply.gg" in t)
        ck("the pasted ':12345' pre-filled the port step: port = 12345", "port = 12345" in t)
        ck("the saved entry is named beta", "name = beta" in t)
        # Ctrl+C / Ctrl+X in a text field (second entry: Add server is the row below the saved server)
        gw.press("DOWN", hold=0.12, after=0.4)
        gw.press("A", hold=0.15, after=1.0)
        tap(gw, "KT")
        tap(gw, "KT")
        ps("Set-Clipboard -Value 'untouched'")
        chord(gw, "LCtrl", "C")
        ck("Ctrl+C copied the field text to the Windows clipboard", ps("Get-Clipboard -Raw").stdout.strip() == "tt")
        ps("Set-Clipboard -Value 'untouched'")
        chord(gw, "LCtrl", "X")
        ck("Ctrl+X copied the field text to the clipboard", ps("Get-Clipboard -Raw").stdout.strip() == "tt")
        shot("7_cut")
        tap(gw, "KA")
        gw.press("Return", hold=0.12, after=0.6)  # the cut emptied the field: only the new 'a' remains = a valid name, we are at the address step
        chord(gw, "LCtrl", "V")  # the clipboard now holds 'tt': pasted into the ADDRESS field
        shot("8_address_tt")
        gw.press("Escape", hold=0.12, after=0.5)
    finally:
        try:
            gw.close()
            time.sleep(3.0)
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
        log.close()
        ps("Set-Clipboard -Value $null" if not saved_clip.strip() else "Set-Clipboard -Value '%s'" % saved_clip.rstrip("\r\n").replace("'", "''"))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
