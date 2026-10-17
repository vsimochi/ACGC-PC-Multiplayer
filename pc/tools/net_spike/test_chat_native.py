#!/usr/bin/env python3
"""test_chat_native.py - MULTIPLAYER TEXT CHAT (worker CH): compiles the PURE logic (pc/src/pc_chat.c + pc/src/pc_ops.c; no game state, no SDL) alone with
-Wall -Wextra -Werror and runs pc_chat_selftest(): the font charset (ASCII / Latin-1 / symbols -> the game's message font codes, '?' for a missing glyph, controls
dropped, code 0 / 0x7F / 0x80 never produced, UTF-8 round trip of every chat code, invalid UTF-8), sanitizing, line editing, word wrap (24 x 3, truncation),
bubble life / fade, the host rate limiter (burst 3, 0.7 s), the speaker colour, the CHAT_SEND / CHAT_MSG validators, the bubble / log store (2 per speaker,
eviction, expiry, tick wrap) and the mutes.ini format / tolerant parser.

TIER: NATIVE (no game process). Usage: python test_chat_native.py
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
    work = tempfile.mkdtemp(prefix="chat_native_")
    ok_all = True
    try:
        main_c = os.path.join(work, "main.c")
        exe = os.path.join(work, "chat_selftest.exe")
        with open(main_c, "w") as f:
            f.write("int pc_chat_selftest(void);\nint pc_ops_selftest(void);\nint main(void){return pc_chat_selftest() + pc_ops_selftest();}\n")
        cmd = [cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + os.path.join(PC, "include"), main_c, os.path.join(PC, "src", "pc_chat.c"),
               os.path.join(PC, "src", "pc_ops.c"), "-o", exe]
        b = subprocess.run(cmd, capture_output=True, text=True, cwd=work)
        ok_build = b.returncode == 0
        print(("PASS" if ok_build else "FAIL") + " - N1 pc_chat.c + pc_ops.c compile alone with -Wall -Wextra -Werror (no game / SDL header)")
        if not ok_build:
            print(b.stdout + b.stderr)
            return 1
        r = subprocess.run([exe], capture_output=True, text=True, cwd=work, timeout=60)
        print(r.stdout.rstrip())
        ok1 = r.returncode == 0 and "[CHAT] selftest: PASS" in r.stdout
        ok2 = r.returncode == 0 and "[OPS] selftest: PASS" in r.stdout
        print(("PASS" if ok1 else "FAIL") + " - N2 pc_chat_selftest(): every pure check passes")
        print(("PASS" if ok2 else "FAIL") + " - N3 pc_ops_selftest() still passes (the mute list reuses its container)")
        ok_all = ok_build and ok1 and ok2
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("-" * 60)
    print("%s" % ("ALL PASS" if ok_all else "FAILURES"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
