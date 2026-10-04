#!/usr/bin/env python3
"""test_guest_profiles_unit.py - NATIVE UNIT test of the named guest profiles (--guest-profile NAME) in pc/src/pc_guest_profile.c.

TIER: native unit (no game, no network, no fixture dir). Compiles pc/tools/net_spike/guest_profiles_selftest.c + pc_guest_profile.c + pc_mp_guests.c + pc_mp_records.c
with the msys2 gcc (-Wall -Wextra, must be warning-free) and runs it against a scratch directory under the temp dir: the profile-name validation table (accept / reject
incl. traversal, dots, spaces, device names, length, case folding), the file-name derivation (the default stays byte-identical to save/mp/guest.ini and
save/mp/guest_token.dat), select / persist of several profiles, the sibling scan, the id re-draw (through the test seam of the id source), the default-name rule
(truncation, SERVER / 'Guest' fallback), a corrupt sibling skipped, an existing / corrupt file never overwritten.
Usage: python test_guest_profiles_unit.py
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
SCRATCH = os.path.join(tempfile.gettempdir(), "acmp_guest_profiles_unit")

results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


def main():
    scratch = os.path.abspath(SCRATCH)
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    exe = os.path.join(scratch, "guest_profiles_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include"), os.path.join(HERE, "guest_profiles_selftest.c"),
                         os.path.join(PC, "src", "pc_guest_profile.c"), os.path.join(PC, "src", "pc_mp_guests.c"), os.path.join(PC, "src", "pc_mp_records.c"),
                         "-o", exe], capture_output=True, text=True, env=env)
    check("compile with msys2 gcc -Wall -Wextra succeeds", cp.returncode == 0)
    check("compile is warning-free", cp.stderr.strip() == "")
    if cp.returncode != 0:
        print(cp.stderr)
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
    check("harness ran a meaningful number of checks (>= 90)", len(passes) >= 90)
    check("the name-validation table ran (>= 40 name checks)", sum(1 for p in passes if p.startswith("name check:")) >= 40)
    check("the sibling / redraw / persist groups all ran", all(any(p.startswith(g) for p in passes) for g in ("paths:", "select:", "default name:", "create:", "persist:", "corrupt:",
                                                                                                         "siblings:", "redraw:", "many:", "read:", "invalid:")))
    check("the scratch dir stayed under the temp dir (nothing written into the repo)", scratch.startswith(os.path.abspath(tempfile.gettempdir())))
    bad = [d for d, ok in results if not ok]
    print("\n%d checks, %d failed" % (len(results), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
