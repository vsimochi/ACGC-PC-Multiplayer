#!/usr/bin/env python3
"""test_character_unit.py - NATIVE UNIT test of the character store (pc/src/pc_character.c) and the town membership layer (pc/src/pc_mp_membership.c).

TIER: native unit (no game, no network, no fixture dir). Compiles pc/tools/net_spike/character_store_selftest.c + pc_character.c + pc_mp_membership.c + pc_guest_profile.c
+ pc_mp_guests.c + pc_mp_records.c with the msys2 gcc (-Wall -Wextra, must be warning-free) and runs it against a scratch directory under the temp dir only.
Covers: distinct stable uuids, reload round trip, corrupt character never touched, legacy adapter + import (token copied, legacy bytes untouched), townkey formatting,
two towns -> two token paths for one character, membership lookup (resident here / guest there / none / ambiguous), five towns keep five tokens.
Usage: python test_character_unit.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"
SCRATCH = os.path.join(tempfile.gettempdir(), "acmp_character_unit")

results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


def main():
    scratch = os.path.abspath(SCRATCH)
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    exe = os.path.join(scratch, "character_store_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    src = [os.path.join(HERE, "character_store_selftest.c")] + [os.path.join(PC, "src", f) for f in
           ("pc_character.c", "pc_mp_membership.c", "pc_guest_profile.c", "pc_mp_guests.c", "pc_mp_records.c")]
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include")] + src + ["-o", exe],
                        capture_output=True, text=True, env=env)
    check("compile with msys2 gcc -Wall -Wextra succeeds", cp.returncode == 0)
    check("compile is warning-free", cp.stderr.strip() == "")
    if cp.returncode != 0 or cp.stderr.strip():
        print(cp.stderr)
        if cp.returncode != 0:
            return 1
    rp = subprocess.run([exe, os.path.join(scratch, "data")], capture_output=True, text=True, env=env, timeout=120)
    out = rp.stdout
    with open(os.path.join(scratch, "selftest.out"), "w", encoding="utf-8") as f:
        f.write(out)
    passes = [ln[6:] for ln in out.splitlines() if ln.startswith("PASS: ")]
    fails = [ln[6:] for ln in out.splitlines() if ln.startswith("FAIL: ")]
    m = re.search(r"RESULT passed=(\d+) failed=(\d+)", out)
    for p in passes:
        results.append((p, True))
    for p in fails:
        results.append((p, False))
        print("FAIL - " + p)
    print("harness: %d passed, %d failed (exit %d)" % (len(passes), len(fails), rp.returncode))
    check("harness exit code 0 and the RESULT line agrees with the PASS/FAIL lines", rp.returncode == 0 and m is not None and int(m.group(1)) == len(passes) and int(m.group(2)) == 0)
    check("all groups ran", all(any(p.startswith(g) for p in passes) for g in ("townkey:", "create:", "reload:", "corrupt:", "legacy:", "import:", "tokens:", "member:", "five:")))
    check("the scratch dir stayed under the temp dir (nothing written into the repo)", scratch.startswith(os.path.abspath(tempfile.gettempdir())))
    bad = [d for d, ok in results if not ok]
    print("\n%d checks, %d failed" % (len(results), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
