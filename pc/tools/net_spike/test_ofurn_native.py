#!/usr/bin/env python3
"""test_ofurn_native.py - OUTDOOR FURNITURE (C1) + the OPERATOR LIST: compiles the PURE logic (pc/src/pc_outdoor_furniture.c, pc/src/pc_ops.c; no game state) alone with
-Wall -Wextra -Werror and runs pc_ofurn_selftest() (the marker table, the acre / ceiling rules, the outdoor_furniture.dat round trip + corruption, the message 98 / service 10
shapes, the kind 22 tag shape of every op, the facing / front tile / reach geometry, the rate limiter, the notice texts) and pc_ops_selftest() (add / remove / find, the
ops.ini round trip, the tolerant parser, the bounds).

TIER: NATIVE (no game process). Usage: python test_ofurn_native.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))


def main():
    cc = shutil.which("cc") or shutil.which("gcc") or r"C:\msys64\ucrt64\bin\cc.exe"
    work = tempfile.mkdtemp(prefix="ofurn_native_")
    ok_all = True
    try:
        main_c = os.path.join(work, "main.c")
        exe = os.path.join(work, "ofurn_selftest.exe")
        with open(main_c, "w") as f:
            f.write("int pc_ofurn_selftest(void);\nint pc_ops_selftest(void);\nint main(void){return pc_ofurn_selftest() + pc_ops_selftest();}\n")
        cmd = [cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + os.path.join(PC, "include"), main_c, os.path.join(PC, "src", "pc_outdoor_furniture.c"),
               os.path.join(PC, "src", "pc_ops.c"), "-o", exe]
        b = subprocess.run(cmd, capture_output=True, text=True, cwd=work)
        ok_build = b.returncode == 0
        print(("PASS" if ok_build else "FAIL") + " - N1 pc_outdoor_furniture.c + pc_ops.c compile alone with -Wall -Wextra -Werror (no game header)")
        if not ok_build:
            print(b.stdout + b.stderr)
            return 1
        r = subprocess.run([exe], capture_output=True, text=True, cwd=work, timeout=60)
        print(r.stdout.rstrip())
        ok1 = r.returncode == 0 and "[OFURN] selftest: PASS" in r.stdout
        ok2 = r.returncode == 0 and "[OPS] selftest: PASS" in r.stdout
        print(("PASS" if ok1 else "FAIL") + " - N2 pc_ofurn_selftest(): every pure check passes")
        print(("PASS" if ok2 else "FAIL") + " - N3 pc_ops_selftest(): every pure check passes")
        ok_all = ok_build and ok1 and ok2
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("-" * 60)
    print("%s" % ("ALL PASS" if ok_all else "FAILURES"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
