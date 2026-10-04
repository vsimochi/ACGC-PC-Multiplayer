#!/usr/bin/env python3
"""test_guest_arrival.py - NATIVE unit test of the guest-arrival presentation logic (T4 remote-arrival train guard + per-puppet latch, T3 DEMO_WALK mapping).

TIER: NATIVE UNIT. Compiles guest_arrival_selftest.c with the msys2 gcc against the REAL header pc/include/pc_remote_arrival_logic.h that
src/game/m_train_control.c and pc/src/pc_remote_player.c use, and runs it. NOT covered here: the wiring (test_guest_arrival_sync_src.py),
the real process behaviour (test_guest_arrival_real.py), the look of the train / puppet (needs a human).
Usage: python test_guest_arrival.py"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"


def main():
    results = []

    def ck(d, c):
        results.append((d, bool(c)))
        print(("PASS - " if c else "FAIL - ") + d)

    scratch = tempfile.mkdtemp(prefix="acmp_guestarrival_")
    exe = os.path.join(scratch, "guest_arrival_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include"),
                         os.path.join(HERE, "guest_arrival_selftest.c"), "-o", exe], capture_output=True, text=True, env=env)
    ck("native: the selftest compiles warning-free against the real header", cp.returncode == 0 and cp.stderr.strip() == "")
    n = 0
    if cp.returncode == 0:
        rp = subprocess.run([exe], capture_output=True, text=True, env=env, timeout=60)
        for ln in rp.stdout.splitlines():
            if ln.startswith("PASS: "):
                ck(ln[6:], True)
            elif ln.startswith("FAIL: "):
                ck(ln[6:], False)
        m = re.search(r"RESULT passed=(\d+) failed=(\d+)", rp.stdout)
        n = int(m.group(1)) if m else 0
        ck("native: exit 0, RESULT line agrees, >= 30 checks", rp.returncode == 0 and m is not None and int(m.group(2)) == 0 and n >= 30)
    bad = [d for d, ok in results if not ok]
    print("-" * 60)
    print("%d/%d checks passed" % (len(results) - len(bad), len(results)))
    shutil_cleanup(scratch)
    return 1 if bad else 0


def shutil_cleanup(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
