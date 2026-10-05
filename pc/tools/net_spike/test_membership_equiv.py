#!/usr/bin/env python3
"""test_membership_equiv.py - M-H NATIVE equivalence test: pc_mp_membership_resolve (pc/src/pc_mp_membership.c) vs a C copy of the old host classifier
(membership_equiv_selftest.c). TIER: native unit, no game, no network, no fixture dir. Compiles with the msys2 gcc (-Wall -Wextra, warning-free) and runs the table.
Usage: python test_membership_equiv.py"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"
results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


def main():
    scratch = os.path.join(tempfile.gettempdir(), "acmp_membership_equiv")
    os.makedirs(scratch, exist_ok=True)
    exe = os.path.join(scratch, "membership_equiv_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include"), os.path.join(HERE, "membership_equiv_selftest.c"),
                         os.path.join(PC, "src", "pc_mp_membership.c"), "-o", exe], capture_output=True, text=True, env=env)
    check("compile with msys2 gcc -Wall -Wextra succeeds", cp.returncode == 0)
    check("compile is warning-free", cp.stderr.strip() == "")
    if cp.returncode != 0:
        print(cp.stderr)
        return 1
    rp = subprocess.run([exe], capture_output=True, text=True, env=env, timeout=60)
    out = rp.stdout + rp.stderr
    passes = [ln[6:] for ln in out.splitlines() if ln.startswith("PASS: ")]
    fails = [ln[6:] for ln in out.splitlines() if ln.startswith("FAIL: ")]
    m = re.search(r"RESULT passed=(\d+) failed=(\d+)", out)
    for p in passes:
        check("U " + p, True)
    for p in fails:
        check("U " + p, False)
    check("harness exit 0, RESULT agrees with the PASS/FAIL lines, >= 25 checks", rp.returncode == 0 and m is not None and int(m.group(1)) == len(passes) and int(m.group(2)) == 0 and len(passes) >= 25)
    cm = open(os.path.join(PC, "src", "pc_net_game.c"), encoding="utf-8", errors="replace", newline="").read().replace("\r\n", "\n")
    a = cm.index("static void pcnetgame_host_admission_decide(")
    dec = cm[a:cm.index("/* Applies a REFUSE decision", a)]
    check("S the decide function calls the resolver view BEFORE classify, logs `ADMISSION EQUIV differs` and refuses on ANY disagreement, switches on the resolve result before guest_check / the credential check",
          dec.index("pcnetgame_host_admission_resolve_view(") < dec.index("pcnetgame_host_classify_identity(") < dec.index("ADMISSION EQUIV differs") < dec.index("pcnetgame_host_guest_check(")
          < dec.index("pcnetgame_host_resident_credential_check(") and "claim is STALE" in dec and "PC_MP_ADMIT_AMBIGUOUS" in dec and "CROSS-CHECK" not in cm)
    check("S the membership module is pure (no game headers) and in CMake", "#include" in open(os.path.join(PC, "src", "pc_mp_membership.c")).read() and "src/pc_mp_membership.c" in open(os.path.join(PC, "CMakeLists.txt")).read())
    print("%d / %d checks passed" % (sum(1 for _d, c in results if c), len(results)))
    return 0 if all(c for _d, c in results) else 1


if __name__ == "__main__":
    sys.exit(main())
