#!/usr/bin/env python3
"""test_wheel_text_input_real.py - the radial menu's keyboard key (G) must be TEXT while a text field has the keyboard (Play Online menu: new character name), on ONE REAL game process
driven by synthetic key events (game_input.py; keys are only sent to the game window it focuses). Disposable fixture copy (pc\\build64\\bin_fixture4_wheeltxt, recreated here), no host needed.

The wheel state is read from the live process with a read-only gdb attach that CALLS the exported pc_tool_wheel_is_open() while the key is held (nothing is written; the process is detached
right after):
  W1 title menu, no text field: holding G opens the radial menu (the unchanged behaviour: positive control)
  W2 Play Online -> server -> Connect -> New character (a text field has the keyboard): holding G does NOT open it
  W4 leaving the text field (Escape): holding G opens the radial menu again
  W3 a second entry: a held G is text (wheel closed), then Shift+G, x, g typed with real key events and confirmed: the game's own log names the new character 'gGxg'
Usage: python test_wheel_text_input_real.py"""
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

NAME = "bin_fixture4_wheeltxt"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, NAME)
if __name__ == "__main__":
    T.make_fixture(NAME)
import net_spike_lib as L  # noqa: E402
import game_input as G  # noqa: E402

GDB = r"C:\msys64\ucrt64\bin\gdb.exe"
G.SCANCODES.update({"G": 0x22, "X": 0x2D, "Return": 0x1C, "Escape": 0x01})


def wheel_open(pid):
    r = subprocess.run([GDB, "-batch", "-p", str(pid), "-ex", "print ((int(*)(void))pc_tool_wheel_is_open)()"], capture_output=True, text=True, timeout=120)
    m = re.search(r"\$\d+ = (-?\d+)", r.stdout)
    return int(m.group(1)) if m else None


def hold_and_sample(win, pid, key="G", settle=1.2):
    """hold `key`, let the game process it, sample the wheel state, release"""
    sc = G.SCANCODES[key]
    G.focus(win.hwnd)
    G._send_scan(sc, False)
    time.sleep(settle)
    v = wheel_open(pid)
    G._send_scan(sc, True)
    time.sleep(0.5)
    return v


def main():
    L.require_test_bin_dir()
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    cdir = L.GAME_BIN_DIR
    mp = os.path.join(cdir, "save", "mp")
    shutil.rmtree(mp, ignore_errors=True)
    os.makedirs(mp)
    with open(os.path.join(mp, "servers.ini"), "w", newline="") as f:
        f.write("[server]\nname = wheelsrv\naddress = 127.0.0.1\nport = 9899\n")
    log = os.path.join(HERE, "wheel_text_input.log")
    env = dict(os.environ)
    env.update({"AC_DISPLAY_NAME": os.environ.get("AC_DISPLAY_NAME", "samsung"), "AC_MASTER_VOLUME": "1"})
    fp = open(log, "wb")
    proc = subprocess.Popen([os.path.join(cdir, "AnimalCrossing.exe"), "--verbose"], cwd=cdir, stdout=fp, stderr=subprocess.STDOUT, env=env)
    try:
        win = G.GameWindow(proc.pid)
        end = time.time() + 120
        while time.time() < end and "aAL_setupAction: 2 -> 3" not in open(log, errors="replace").read():
            time.sleep(0.5)
        ck("title menu reached", "aAL_setupAction: 2 -> 3" in open(log, errors="replace").read())
        for _ in range(80):
            if win.ready():
                break
            time.sleep(0.25)
        time.sleep(2.0)
        ck("the radial wheel query works (a gdb attach can call pc_tool_wheel_is_open: %s)" % wheel_open(proc.pid), wheel_open(proc.pid) == 0)

        v = hold_and_sample(win, proc.pid)
        ck("W1 title menu, no text field: holding G opens the radial menu (unchanged behaviour): is_open=%s" % v, v == 1)
        time.sleep(1.0)

        win.press("START")  # Play Online
        win.press("START")  # the server
        win.press("START")  # Connect -> the character page (no characters: the first row is 'New character')
        win.press("START")  # New character -> the name text field has the keyboard
        time.sleep(1.0)
        v = hold_and_sample(win, proc.pid)
        ck("W2 in the character-name text field: holding G does NOT open the radial menu: is_open=%s" % v, v == 0)

        # leave the text field with Escape (the typed 'g' is discarded), then the wheel must work again on the menu page
        win.press("Escape", hold=0.12, after=0.8)
        v = hold_and_sample(win, proc.pid)
        ck("W4 after the text entry ended (Escape), holding G opens the radial menu again: is_open=%s" % v, v == 1)
        time.sleep(1.0)

        # a second entry: G (held, sampled), then Shift+G, x, g through real key events (SDL text input), confirmed with Return
        win.press("START", hold=0.12, after=0.8)  # New character again
        v = hold_and_sample(win, proc.pid)
        ck("W3 second entry: a held G is text and leaves the wheel closed: is_open=%s" % v, v == 0)
        shift = 0x2A
        G.focus(win.hwnd)
        G._send_scan(shift, False)
        G._send_scan(G.SCANCODES["G"], False)
        time.sleep(0.12)
        G._send_scan(G.SCANCODES["G"], True)
        G._send_scan(shift, True)
        time.sleep(0.3)
        for key in ("X", "G"):
            win.press(key, hold=0.12, after=0.3)
        win.press("Return", hold=0.12, after=1.0)  # confirm the name: the Play Online connect for a NEW character starts
        time.sleep(2.0)
        txt = open(log, errors="replace").read()
        ck("W3 the typed letters were TEXT: the new character's name is 'gGxg' (the game's own log of it)", "character 'gGxg' does not exist yet" in txt)
        ck("the process is still alive", proc.poll() is None)
    finally:
        try:
            proc.terminate()
            proc.wait(8)
        except Exception:
            proc.kill()
        fp.close()
        shutil.rmtree(mp, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
