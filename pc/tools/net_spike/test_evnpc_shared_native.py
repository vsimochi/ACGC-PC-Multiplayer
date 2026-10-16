#!/usr/bin/env python3
"""test_evnpc_shared_native.py - SHARED EVENT NPCs (worker E): compiles the PURE logic (pc/src/pc_evnpc_shared.c, no game state) with a small main and runs
pc_evc_selftest(): the tag shape of every op, the spirit points / strip (vanilla aEGH_hitodama_num / aEGH_delete_hitodama), Saharah's price doubling, the visit date range,
the service 9 blob round trip and its validator. TIER: NATIVE (cc from PATH or CC; on Windows add C:\\msys64\\ucrt64\\bin to PATH). No game process.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))


def main():
    cc = os.environ.get("CC", "cc")
    tmp = os.environ.get("TEMP", "/tmp")
    main_c = os.path.join(tmp, "evc_native_main.c")
    exe = os.path.join(tmp, "evc_native.exe")
    with open(main_c, "w") as f:
        f.write("int pc_evc_selftest(void);\nint main(void){return pc_evc_selftest();}\n")
    cmd = [cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + os.path.join(PC, "include"), main_c, os.path.join(PC, "src", "pc_evnpc_shared.c"), "-o", exe]
    cp = subprocess.run(cmd, capture_output=True, text=True)
    ok_build = cp.returncode == 0
    print(("PASS" if ok_build else "FAIL") + " - N1 pc_evnpc_shared.c compiles alone with -Wall -Wextra -Werror (no game header)")
    if not ok_build:
        print(cp.stdout + cp.stderr)
        print("1 check(s) failed")
        return 1
    rp = subprocess.run([exe], capture_output=True, text=True, timeout=60)
    print(rp.stdout.strip())
    ok = rp.returncode == 0 and "selftest: PASS" in rp.stdout
    print(("PASS" if ok else "FAIL") + " - N2 pc_evc_selftest(): every pure check passes")
    print("-" * 60)
    print("%d/2 checks passed" % (1 + (1 if ok else 0)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
