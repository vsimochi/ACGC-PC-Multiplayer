#!/usr/bin/env python3
"""test_adopt_clock.py - regression test for the client record-adoption watchdog (resident title-menu Start Game flow).

TIERS: (1) NATIVE UNIT test: compiles adopt_clock_selftest.c with the msys2 gcc and runs it against the real header pc/include/pc_adopt_clock.h that
pc_net_game.c uses, replaying the reported timeline (the push arrives during title -> player select -> town: 26 s of scene lifecycle; a flat 10 s window
failed it, the lifecycle-aware clock must not, while a genuinely stuck blocker still fails after 10 s and an endless lifecycle after the cap);
(2) SOURCE AUDIT of the wiring in pc_net_game.c (the clock is armed when a push is staged and ticked on every blocked attempt, the flat `now - st_ms` check is
gone, the 10 s constant and the guard pcnetgame_crec_adopt_blocker are unchanged vs the baseline commit 266c746, the lifecycle predicate covers: no running
GAME_PLAY, a fade / wipe in progress, the pre-game scenes). NOT covered: the real title-menu Start Game click (needs UI automation).
Usage: python test_adopt_clock.py"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"
BASELINE = "266c746"  # the diagnostic commit: HEAD when this fix started
FIX_COMMIT = "f3d69ef"  # the adoption-clock fix commit: the diff audit compares BASELINE..FIX_COMMIT (later work touches the same file)


def main():
    results = []

    def ck(d, c):
        results.append((d, bool(c)))
        print(("PASS - " if c else "FAIL - ") + d)

    scratch = tempfile.mkdtemp(prefix="acmp_adoptclock_")
    exe = os.path.join(scratch, "adopt_clock_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include"), os.path.join(HERE, "adopt_clock_selftest.c"), "-o", exe],
                        capture_output=True, text=True, env=env)
    ck("native: the selftest compiles warning-free against the real header", cp.returncode == 0 and cp.stderr.strip() == "")
    if cp.returncode == 0:
        rp = subprocess.run([exe], capture_output=True, text=True, env=env, timeout=60)
        for ln in rp.stdout.splitlines():
            if ln.startswith("PASS: "):
                results.append((ln[6:], True))
            elif ln.startswith("FAIL: "):
                results.append((ln[6:], False))
                print("FAIL - " + ln[6:])
        m = re.search(r"RESULT passed=(\d+) failed=(\d+)", rp.stdout)
        ck("native: exit 0, RESULT line agrees, >= 9 checks", rp.returncode == 0 and m is not None and int(m.group(2)) == 0 and int(m.group(1)) >= 9)

    src = open(os.path.join(PC, "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    base = subprocess.run(["git", "-C", ROOT, "show", BASELINE + ":pc/src/pc_net_game.c"], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")

    def func(s, name):
        m = re.search(r"^[^\n;]*\b%s\([^;{]*\)\s*\{\n" % re.escape(name), s, re.M)
        if not m:
            return ""
        j = s.find("\n}\n", m.end())
        return s[m.start():j + 3]

    ty = func(src, "pcnetgame_crec_try_adopt")
    ck("src: #include of the clock header; the 10 s constant is unchanged vs the baseline",
       '#include "pc_adopt_clock.h"' in src and "#define PC_NETGAME_CREC_ADOPT_TIMEOUT_MS 10000u" in src and "#define PC_NETGAME_CREC_ADOPT_TIMEOUT_MS 10000u" in base)
    ck("src: pcnetgame_crec_adopt_blocker is byte-identical to the baseline (the guard itself is NOT weakened)",
       func(src, "pcnetgame_crec_adopt_blocker") != "" and func(src, "pcnetgame_crec_adopt_blocker") == func(base, "pcnetgame_crec_adopt_blocker"))
    ck("src: the clock is armed when a push is staged and ticked on every blocked attempt",
       "pc_adopt_clock_arm(&s_crec_adopt_clock, s_crec.st_ms);" in src and "pc_adopt_clock_tick(&s_crec_adopt_clock, now, pcnetgame_crec_adopt_lifecycle_now());" in ty)
    ck("src: ADOPT_FAILED is decided by pc_adopt_clock_expired(10 s, lifecycle cap); the flat `now - st_ms >= TIMEOUT` check is gone",
       "pc_adopt_clock_expired(&s_crec_adopt_clock, now, PC_NETGAME_CREC_ADOPT_TIMEOUT_MS, PC_NETGAME_CREC_ADOPT_LIFECYCLE_CAP_MS)" in ty
       and "(uint32_t)(now - s_crec.st_ms) >= PC_NETGAME_CREC_ADOPT_TIMEOUT_MS" not in ty and "#define PC_NETGAME_CREC_ADOPT_LIFECYCLE_CAP_MS 300000u" in src)
    lf = func(src, "pcnetgame_crec_adopt_lifecycle_now")
    ck("src: the lifecycle predicate = no running GAME_PLAY | fade / wipe in progress | a pre-game scene",
       "gamePT == NULL || gamePT->exec != play_main" in lf and "play->fb_fade_type != FADE_TYPE_NONE || play->fb_wipe_mode != WIPE_MODE_NONE" in lf
       and "return pcnetgame_scene_is_pregame(sc);" in lf
       and all(x in func(src, "pcnetgame_scene_is_pregame") for x in ("SCENE_TITLE_DEMO", "SCENE_PLAYERSELECT_2", "SCENE_PLAYERSELECT_3", "SCENE_PLAYERSELECT_SAVE", "SCENE_START_DEMO3")))
    ck("src: adoption itself is still gated by the unchanged blocker (the clock only changes WHEN the attempt is given up)",
       "why = pcnetgame_crec_adopt_blocker(&s_crec_scratch);" in ty)
    diff = subprocess.run(["git", "-C", ROOT, "diff", "-U0", "--ignore-cr-at-eol", BASELINE, FIX_COMMIT, "--", "pc/src/pc_net_game.c"], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace")
    removed = [l for l in diff.split("\n") if l.startswith("-") and not l.startswith("---")]
    ck("src: the only lines removed vs the baseline are the flat timeout check and the old ADOPT_FAILED log text (%d)" % len(removed),
       len(removed) <= 4 and all(("now - s_crec.st_ms" in l) or ("ADOPT_FAILED" in l) or ("PC_NETGAME_CREC_ADOPT_TIMEOUT_MS" in l) for l in removed))
    ck("wire: no protocol change (the wire headers / transport are untouched vs the baseline)",
       subprocess.run(["git", "-C", ROOT, "diff", "--quiet", BASELINE, "--", "pc/include/pc_net_game.h", "pc/include/pc_net.h", "pc/src/pc_net.c"]).returncode == 0)

    failed = [d for d, ok in results if not ok]
    print("-" * 60)
    print("%d/%d checks passed" % (len(results) - len(failed), len(results)))
    for d in failed:
        print("FAILED: " + d)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
