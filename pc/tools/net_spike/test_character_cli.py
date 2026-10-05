#!/usr/bin/env python3
"""test_character_cli.py - CLI check of --characters / --character-import-profile (no host, no window) in a TEMP cwd.

TIER: process. Runs pc/build64/bin/AnimalCrossing.exe with cwd = a scratch dir that holds a hand written legacy profile save/mp/guest_roger.ini; nothing under the repo
or the live save dir is touched (relative save/mp resolves inside the scratch cwd).
Usage: python test_character_cli.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
EXE = os.path.join(PC, "build64", "bin", "AnimalCrossing.exe")
SCRATCH = os.path.join(tempfile.gettempdir(), "acmp_character_cli")
INI = "name = Roger\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x1234\nland_id = 0x4321\n"

results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


def run(args, cwd):
    return subprocess.run([EXE] + args, cwd=cwd, capture_output=True, text=True, timeout=60)


def main():
    scratch = os.path.abspath(SCRATCH)
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(os.path.join(scratch, "save", "mp"))
    ini = os.path.join(scratch, "save", "mp", "guest_roger.ini")
    with open(ini, "w", newline="") as f:
        f.write(INI)
    r = run(["--characters"], scratch)
    check("--characters exits 0 and lists the legacy profile", r.returncode == 0 and "legacy" in r.stdout and "name='Roger'" in r.stdout and "roger" in r.stdout)
    r = run(["--character-import-profile", "roger"], scratch)
    check("--character-import-profile roger exits 0 and reports a uuid", r.returncode == 0 and "imported legacy profile 'roger' as character" in r.stdout)
    check("the legacy ini is byte-identical afterwards", open(ini, newline="").read() == INI)
    chars = os.path.join(scratch, "save", "mp", "characters")
    dirs = [d for d in os.listdir(chars)] if os.path.isdir(chars) else []
    check("exactly one character directory with a character.ini", len(dirs) == 1 and os.path.isfile(os.path.join(chars, dirs[0], "character.ini")))
    r = run(["--characters"], scratch)
    check("--characters now lists the store character (not the legacy file twice)", r.returncode == 0 and "store " in r.stdout and r.stdout.count("name='Roger'") == 1)
    r = run(["--character-import-profile", "roger"], scratch)
    check("a second import is REFUSED with exit 2 and 'already imported'", r.returncode == 2 and "already imported" in r.stderr)
    r = run(["--character", "Roger"], scratch)
    check("--character without --connect is REFUSED with exit 2 (client only)", r.returncode == 2 and "CLIENT-only" in r.stderr)
    bad = [d for d, ok in results if not ok]
    print("\n%d checks, %d failed" % (len(results), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
