#!/usr/bin/env python3
"""test_version_title_real.py - the game version on the TITLE SCREEN with ONE real game process (no host, no network), Samsung display, volume 1.

TIER: REAL CLIENT (solo, sits on the title screen). Starts AnimalCrossing.exe --verbose (the test-hook build of pc\\build64_w3) on the disposable fixture
pc\\build64\\bin_fixture4_ver_title (a fresh copy of bin_fixture4), waits for
  T1 the startup line "[GENERAL] Animal Crossing PC Multiplayer v<PC_VERSION_STRING>" (exactly once)
  T2 the title menu ("[LOGO] draw: action=3": the GAME_START action, the menu branch that draws the version corner)
  T3 an in-game frame grab (AC_TEST_FRAME_DUMP, pc_main.c: the back buffer before the swap) -> version_title.png next to this script; the bottom-right corner
     region holds bright text pixels with dark outline pixels next to them (the corner text), the bottom-LEFT corner region (same size) is the comparison
then stops the process it started (and only that one). The display / volume switches are set for the process; if the Samsung display is missing the game
exits 4 and the test reports NOT RUN (no fallback to another display).
Usage: python test_version_title_real.py [--wait 90]"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

os.environ["AC_DISPLAY_NAME"] = "samsung"
os.environ["AC_MASTER_VOLUME"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_ver_title"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
results = []


def ck(name, ok, detail=None):
    results.append(bool(ok))
    print(("PASS" if ok else "FAIL") + " - " + name + ("" if ok or detail is None else "  :: %s" % (detail,)), flush=True)


def version_string():
    h = open(os.path.join(T.PC, "include", "pc_version.h"), "rb").read().decode()
    n = {k: int(re.search(r"^#define PC_VERSION_%s +(\d+)" % k, h, re.M).group(1)) for k in ("MAJOR", "MINOR", "PATCH", "DEV")}
    return "%d.%d.%d%s" % (n["MAJOR"], n["MINOR"], n["PATCH"], "-dev" if n["DEV"] else "")


def log_text(p):
    try:
        return open(p, "rb").read().decode("utf-8", "replace")
    except OSError:
        return ""


def wait_log(p, rx, timeout, proc):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        m = re.search(rx, log_text(p))
        if m:
            return m
        if proc.poll() is not None:
            return None
        time.sleep(0.25)
    return None


def corner_stats(img, right):
    """(bright, dark-next-to-bright pixel counts, the rightmost outlined bright pixel's window x, box size) of the bottom-right (right=True) / bottom-left corner box: 18% of the width x 7% of the height"""
    w, h = img.size
    bw, bh = int(w * 0.18), int(h * 0.07)
    x0 = w - bw if right else 0
    box = img.crop((x0, h - bh, x0 + bw, h)).convert("RGB")
    px = box.load()
    bright = dark_adj = 0
    max_x = -1
    for y in range(1, bh - 1):
        for x in range(1, bw - 1):
            r, g, b = px[x, y]
            if min(r, g, b) >= 185:
                bright += 1
                for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                    xx, yy = x + dx, y + dy
                    if 0 <= xx < bw and 0 <= yy < bh and max(px[xx, yy]) <= 70:
                        dark_adj += 1
                        max_x = max(max_x, x0 + x)
                        break
    return bright, dark_adj, max_x, (bw, bh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=90.0)
    a = ap.parse_args()
    ver = version_string()
    if not os.path.isfile(W3_EXE):
        print("NOT RUN - no exe at %s" % W3_EXE)
        return 2
    d = T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(d, "AnimalCrossing.exe"))
    log = T.log_path("version_title_real.log")
    dump = os.path.join(d, "ver_dump")
    env = dict(os.environ, AC_TEST_HOOKS="1", AC_TEST_FRAME_DUMP=dump)
    fp = open(log, "wb")
    proc = subprocess.Popen([os.path.join(d, "AnimalCrossing.exe"), "--verbose"], cwd=d, stdout=fp, stderr=subprocess.STDOUT, env=env)
    png = os.path.join(HERE, "version_title.png")
    try:
        m = wait_log(log, r"\[GENERAL\] Animal Crossing PC Multiplayer v(\S+)", 60.0, proc)
        if m is None and proc.poll() is not None:
            print("process exited with %s; log tail:\n%s" % (proc.returncode, log_text(log)[-1500:]))
            if proc.returncode == 4:
                print("NOT RUN - the Samsung display was not found (exit 4); no fallback display used")
                return 2
        ck("T1 startup line '[GENERAL] Animal Crossing PC Multiplayer v%s' (exactly once)" % ver,
           m is not None and m.group(1) == ver and len(re.findall(r"Animal Crossing PC Multiplayer v", log_text(log))) == 1, m and m.group(0))
        mt = wait_log(log, r"\[LOGO\] draw: action=3 pad_connected=1", a.wait, proc)
        ck("T2 the title menu is up ([LOGO] draw: action=3, pad connected: the menu branch that draws the corner)", mt is not None)
        if mt is not None:
            time.sleep(3.0)
            bmp = dump + "_version_title.bmp"
            if os.path.isfile(bmp):
                os.remove(bmp)
            with open(dump + ".req", "w") as f:
                f.write("version_title\n")
            md = wait_log(log, r"\[TEST-ONLY\] frame dump (\d+)x(\d+) -> .*_version_title\.bmp", 20.0, proc)
            ok = False
            stats = None
            if md is not None:
                try:
                    from PIL import Image
                    time.sleep(0.3)
                    img = Image.open(bmp)
                    img.save(png)
                    br = corner_stats(img, True)
                    bl = corner_stats(img, False)
                    stats = {"bottom_right": br, "bottom_left": bl, "size": img.size}
                    ok = br[0] >= 40 and br[1] >= 10 and br[2] >= img.size[0] - max(12, int(img.size[0] * 0.025))
                except Exception as e:  # noqa: BLE001
                    stats = "PIL: %s" % e
            ck("T3 frame grab %s -> %s; bottom-right corner holds outlined bright text pixels ending within 2.5%% of the window's right edge %s" % (md and md.group(0)[-60:], png, stats), ok)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)
        fp.close()
        print("log: %s" % log)
    print("-" * 60)
    print("%d/%d checks passed" % (sum(results), len(results)))
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
