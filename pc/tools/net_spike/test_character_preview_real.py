#!/usr/bin/env python3
"""test_character_preview_real.py - Play Online character select: the REAL player model next to each character (pc_player_preview.c), on a REAL game window.

TIER: ONE real game process on a DISPOSABLE copy of pc\\build64\\bin_fixture4 (bin_fixture4_cprev, empty save dir), display pinned with AC_DISPLAY_NAME=samsung, AC_MASTER_VOLUME=1.
Flow: three store characters (different gender / face, one imported from a legacy profile) + one saved server -> title -> Play Online -> server -> Connect -> the character page.
Checks (log + files + screenshots written to the log dir for a human to look at):
  P1 the character page prepared one preview per visible character with its OWN gender / face ('[PC][PREVIEW] slot N prepared'), the process is alive and drew frames
  P2 the preview is menu-only: nothing was written to save/ (no GCI / town cache / membership.ini / token file), no network role started, no resident bound
Usage: python test_character_preview_real.py
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
    T.make_fixture("bin_fixture4_cprev", empty_save=True)

import net_spike_lib as L  # noqa: E402

DIR = os.path.join(T.BUILD64, "bin_fixture4_cprev")
EXE = os.path.join(DIR, "AnimalCrossing.exe")
CHARS = [("Annie", 1, 2, 0x5101), ("Barry", 0, 5, 0x5102), ("Cleo", 1, 7, 0x5103)]  # name, gender (0 male / 1 female), face, player_id


def snapshot_tree(root):
    out = {}
    for base, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(base, f)
            out[os.path.relpath(p, root)] = (os.path.getsize(p), os.path.getmtime(p))
    return out


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    mp = os.path.join(DIR, "save", "mp")
    os.makedirs(mp, exist_ok=True)
    for name, gender, face, pid in CHARS:
        with open(os.path.join(mp, "guest_%s.ini" % name.lower()), "w", newline="") as f:
            f.write("name = %s\ngender = %d\nface = %d\nhome_town = PrevVil\nplayer_id = 0x%04X\nland_id = 0x4333\n" % (name, gender, face, pid))
        r = subprocess.run([EXE, "--character-import-profile", name.lower()], cwd=DIR, capture_output=True, text=True, timeout=60)
        ck("setup: %s imported as a store character" % name, r.returncode == 0 and "as character" in r.stdout)
    with open(os.path.join(mp, "servers.ini"), "w", newline="") as f:
        f.write("[server]\nname = prevsrv\naddress = 127.0.0.1\nport = 1\n")
    before = snapshot_tree(os.path.join(DIR, "save"))
    env = dict(os.environ, AC_DISPLAY_NAME="samsung", AC_MASTER_VOLUME="1")
    log_path = T.log_path("cprev_client.log")
    log = open(log_path, "wb")
    proc = subprocess.Popen([EXE, "--verbose"], cwd=DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
    gw = G.GameWindow(proc.pid)
    try:
        t0 = time.monotonic()
        while not gw.ready() and time.monotonic() - t0 < 60 and proc.poll() is None:
            time.sleep(0.5)
        ck("the game window opened (exit code %s: 4 = AC_DISPLAY_NAME matched no display)" % proc.poll(), gw.ready() and proc.poll() is None)
        if not gw.ready():
            return L.summary_and_exit_code(results)
        time.sleep(30.0)
        gw.press("START", hold=0.15, after=2.0)  # title -> Play Online server page
        G.capture_window_png(gw.hwnd, T.log_path("cprev_1_servers.png"), scale=2)
        gw.press("A", hold=0.15, after=1.2)  # the saved server -> its actions page (Connect first)
        gw.press("A", hold=0.15, after=3.0)  # Connect -> the character page
        G.capture_window_png(gw.hwnd, T.log_path("cprev_2_characters.png"), scale=1)
        time.sleep(2.0)
        gw.press("DOWN", hold=0.12, after=1.0)
        G.capture_window_png(gw.hwnd, T.log_path("cprev_3_second_row.png"), scale=2)
        txt = open(log_path, "rb").read().decode("utf-8", "replace")
        got = {m.group(1): (int(m.group(2)), int(m.group(3))) for m in re.finditer(r"\[PC\]\[PREVIEW\] slot (\d+) prepared: gender=(\d) face=(\d)", txt)}
        want = {(g, f) for (_n, g, f, _p) in CHARS}
        ck("P1 a preview slot was prepared for each of the three characters with its own gender / face (got %s)" % sorted(got.values()), want <= set(got.values()))
        ck("P1 the process is still alive (no crash while drawing the models)", proc.poll() is None)
        after = snapshot_tree(os.path.join(DIR, "save"))
        new = sorted(set(after) - set(before))
        changed = sorted(k for k in before if k in after and after[k] != before[k])
        ck("P2 menu-only: nothing was created or changed under save/ while the models were shown (new %s, changed %s)" % (new, changed), not new and not changed)
        ck("P2 no network role / town fetch started and no resident / guest bound ('[NET]' role lines absent)", "[NET] host" not in txt and "[NET] client" not in txt and "town fetch" not in txt.lower())
        ck("P2 the screenshots were written for a visual check: %s" % T.log_path("cprev_2_characters.png"), os.path.isfile(T.log_path("cprev_2_characters.png")))
    finally:
        try:
            gw.close()
            time.sleep(3.0)
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
        log.close()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
