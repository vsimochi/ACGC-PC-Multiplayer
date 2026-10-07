#!/usr/bin/env python3
"""test_new_character.py - Play Online "New character": the name is entered ONCE, in the Rover scene (focused test, no game process).

TIER: native unit + SOURCE AUDIT.
  1. native: new_character_selftest.c (+ servers_selftest.c for the new request kind) are compiled with $CC / gcc (-Wall -Wextra) and run in a scratch dir:
     a NEW request draws a fresh uuid without resolving by name (an existing character with the placeholder's name is untouched), nothing is written before the Rover finish,
     the finish saves Rover's name / gender / face under the SAME uuid (never the placeholder), an EXISTING uuid is not creating, a finish is refused for it and its character.ini
     stays byte-identical.
  2. source: the menu row has no name field and connects with PC_RELAUNCH_NEW_CHARACTER; pc_main_prepare_store_character takes the explicit flag BEFORE any name lookup and never
     resolves; the flag is set only for that request kind and cleared right after; Rover's source, pc_m_card.c and the radial-menu files are unchanged vs cd989b4.
NOT covered (needs a Windows game build + extracted assets, not available here): driving the real Rover scene (--guest-creation-test) and clicking the menu.
Exit code 0 when all checks pass."""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
CC = os.environ.get("CC", "gcc")
BASE = "cd989b4"
results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("PASS - " if ok else "FAIL - ") + name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def func_body(src, header):
    i = src.index(header)
    j = src.index("\n}\n", i)
    return src[i:j]


def native(scratch, name, srcs, extra=()):
    exe = os.path.join(scratch, name + (".exe" if os.name == "nt" else ""))
    cp = subprocess.run([CC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include")] + [os.path.join(HERE, name + ".c")] +
                        [os.path.join(PC, "src", f) for f in srcs] + ["-o", exe], capture_output=True, text=True)
    check("compile " + name, cp.returncode == 0)
    if cp.returncode != 0:
        print(cp.stderr)
        return
    data = os.path.join(scratch, name + "_data")
    os.makedirs(data, exist_ok=True)
    rp = subprocess.run([exe, data], capture_output=True, text=True, timeout=120)
    m = re.search(r"RESULT passed=(\d+) failed=(\d+)", rp.stdout)
    for ln in rp.stdout.splitlines():
        if ln.startswith("FAIL: "):
            check(name + ": " + ln[6:], False)
    check(name + ": all checks pass (exit 0, %s)" % (m.group(0) if m else "?"), rp.returncode == 0 and m is not None and m.group(2) == "0")
    if name == "servers_selftest":
        check("servers_selftest covers the new request kind", "new character" in rp.stdout and "never relaunched" in rp.stdout)
    else:
        check("new_character_selftest ran the A/B/C/D groups", all(("PASS: " + g) in rp.stdout for g in
              ("A: fresh uuid != existing uuid", "C: saved name is Rover's, not the placeholder", "C: session uuid unchanged by the finish", "D: existing character.ini byte-identical")))


def main():
    scratch = os.path.join(tempfile.gettempdir(), "acmp_new_character")
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    native(scratch, "new_character_selftest", ["pc_session.c", "pc_character.c", "pc_servers.c", "pc_mp_membership.c", "pc_guest_profile.c", "pc_mp_guests.c", "pc_mp_records.c"])
    native(scratch, "servers_selftest", ["pc_servers.c", "pc_relaunch.c"])

    menu = read("pc/src/pc_play_online_menu.c")
    row = menu[menu.index("} else if (s_sel == s_nchr) {"):menu.index("} else if (s_sel == s_nchr + 1)")]
    check("menu: the New character row opens no text field", "text_begin" not in row)
    check("menu: the New character row connects with PC_RELAUNCH_NEW_CHARACTER and no name", "do_connect(&s_srv[s_cur], PC_RELAUNCH_NEW_CHARACTER, NULL, NULL)" in row)
    check("menu: nothing begins / commits T_CHAR_NAME any more", "text_begin(T_CHAR_NAME" not in menu and "case T_CHAR_NAME:\n            if (!pc_guest_profile_name_check" not in menu)

    main_c = read("pc/src/pc_main.c")
    prep = func_body(main_c, "static int pc_main_prepare_store_character(void) {")
    i_new = prep.index("if (s_pc_character_new) {")
    i_spec = prep.index("else if (g_pc_character_spec != NULL) {")
    nb = prep[i_new:i_spec]
    check("main: the explicit NEW branch comes before any name lookup", i_new < i_spec and prep.count("pc_character_resolve(") == 1 and prep.index("pc_character_resolve(") > i_spec)
    check("main: the NEW branch calls pc_character_prepare_new, sets creating, never resolves", "pc_character_prepare_new(NULL, PC_NEW_CHARACTER_PLACEHOLDER" in nb and "creating = 1;" in nb and "pc_character_resolve" not in nb)
    check("main: creating state + Rover creation are armed from `creating` as before", "ss->creating = creating;" in prep and "pc_guest_creation_arm(&gp);" in prep)
    conn = func_body(main_c, "static int pc_po_connect(char* err, size_t errcap) {")
    check("main: the flag is set only for the NEW request kind", main_c.count("s_pc_character_new = 1;") == 1 and "if (s_po.kind == PC_RELAUNCH_NEW_CHARACTER) {" in conn)
    check("main: the flag is cleared right after the prepare and on rollback", "rc = pc_main_prepare_store_character();\n    s_pc_character_new = 0;" in conn and "rollback:\n    s_po_inproc = 0;\n    s_pc_character_new = 0;" in conn)
    check("main: existing kinds still pass their name / uuid as the spec", "g_pc_character_spec = s_po_character;" in conn and conn.index("PC_RELAUNCH_NEW_CHARACTER") < conn.index("s_po.kind == PC_RELAUNCH_CHARACTER"))
    check("main: the placeholder is a valid in-memory name only", re.search(r'#define PC_NEW_CHARACTER_PLACEHOLDER "[A-Za-z0-9-]{1,8}"', main_c) is not None)

    untouched = ["src/actor/npc/ac_npc_guide2_move.c_inc", "pc/src/pc_m_card.c", "pc/src/pc_pad.c", "pc/src/pc_tool_wheel.c", "pc/src/pc_session.c", "pc/src/pc_character.c", "pc/include/pc_session.h"]
    for rel in untouched:
        rc = subprocess.run(["git", "-C", ROOT, "diff", "--quiet", BASE, "--", rel]).returncode
        check("unchanged since %s: %s" % (BASE, rel), rc == 0)

    bad = [n for n, ok in results if not ok]
    print("test_new_character: %d checks, %d failed" % (len(results), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
