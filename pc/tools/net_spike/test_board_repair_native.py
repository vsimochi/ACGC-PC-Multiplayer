#!/usr/bin/env python3
r"""test_board_repair_native.py - NATIVE test of the real pc_board_repair_zeroed_posts text (extracted from pc/src/pc_m_card.c) via board_repair_selftest.c.
Tier: native model (real function text, stub types). Usage: python test_board_repair_native.py"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"


def main():
    src = open(os.path.join(PC, "src", "pc_m_card.c"), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    a = re.search(r"static int pc_board_post_is_zeroed.*?\n\}\n", src, re.S).group(0)
    b = re.search(r"int pc_board_repair_zeroed_posts.*?\n\}\n", src, re.S).group(0)
    d = tempfile.mkdtemp(prefix="acmp_board_")
    open(os.path.join(d, "board_repair_extract.inc"), "w").write(a + "\n" + b)
    exe = os.path.join(d, "t.exe")
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + os.path.dirname(GCC)
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-O1", "-I", d, os.path.join(HERE, "board_repair_selftest.c"), "-o", exe], capture_output=True, text=True, env=env)
    print(cp.stderr[:2000])
    if cp.returncode:
        return 1
    rp = subprocess.run([exe], capture_output=True, text=True, timeout=60)
    print(rp.stdout)
    return rp.returncode


if __name__ == "__main__":
    sys.exit(main())
