#!/usr/bin/env python3
"""test_sell_basket_native.py - Nook bulk sell basket: compiles the PURE rules (pc/src/pc_sell_logic.c; item-id macros of m_name_table.h only, no game function) alone
and runs pc_sell_logic_selftest(): the shared "what Nook buys" predicate against the vanilla refusal list (m_submenu.c mSM_check_item_for_sell + the dialogue's quest /
Sunday-turnip refusals + the host's stationery rule + unknown ids), the sale values (price / 4, turnip bundles, spoiled 0), the Bells formatting, the notice charset and
the basket staging (next free cell, no duplicates, take back, the 15-bit slot mask, the selection in cell order, prune of changed / no longer sellable pockets).

TIER: NATIVE (no game process). The include / define flags come from pc/build64_w3 (or pc/build64) CMakeFiles/ac_pc.dir. Usage: python test_sell_basket_native.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
DEFS = ["-DTARGET_PC", "-DPC_LOW_ADDRESS_64", "-D_LANGUAGE_C", "-DF3DEX_GBI_2", "-DBUGFIXES", "-DPC_ENHANCEMENTS", "-DVERSION=0", "-Dnullptr=NULL"]


def main():
    cc = shutil.which("cc") or shutil.which("gcc") or r"C:\msys64\ucrt64\bin\cc.exe"
    rsp = None
    for b in ("build64_w3", "build64"):
        p = os.path.join(PC, b, "CMakeFiles", "ac_pc.dir", "includes_C.rsp")
        if os.path.isfile(p):
            rsp = p
            break
    if rsp is None:
        print("FAIL - no includes_C.rsp (configure pc/build64_w3 first)")
        return 1
    work = tempfile.mkdtemp(prefix="sell_native_")
    ok_all = True
    try:
        main_c = os.path.join(work, "main.c")
        exe = os.path.join(work, "sell_selftest.exe")
        with open(main_c, "w") as f:
            f.write("int pc_sell_logic_selftest(void);\nint main(void){return pc_sell_logic_selftest();}\n")
        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + os.pathsep + r"C:\msys64\ucrt64\bin"
        # the game headers are included as SYSTEM headers (their own warnings are not this module's); pc_sell_logic.c itself is -Wall -Wextra -Werror
        incs = []
        with open(rsp) as f:
            for tok in f.read().replace('"', "").split():
                if tok.startswith("-I"):
                    incs += ["-isystem", tok[2:]]
                elif tok != "-isystem" and not tok.startswith("C:/msys64"):
                    incs.append(tok)
        incs = [x for x in incs if x]
        cmd = [cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + os.path.join(PC, "include")] + DEFS + incs + [main_c, os.path.join(PC, "src", "pc_sell_logic.c"), "-o", exe]
        b = subprocess.run(cmd, capture_output=True, text=True, cwd=work, env=env)
        ok_build = b.returncode == 0
        print(("PASS" if ok_build else "FAIL") + " - N1 pc_sell_logic.c compiles alone with -Wall -Wextra -Werror (game headers = macros only, no game function)")
        if not ok_build:
            print(b.stdout + b.stderr)
            return 1
        r = subprocess.run([exe], capture_output=True, text=True, cwd=work, timeout=60, env=env)
        print(r.stdout.rstrip())
        ok1 = r.returncode == 0 and "[SELL] selftest: PASS" in r.stdout
        print(("PASS" if ok1 else "FAIL") + " - N2 pc_sell_logic_selftest(): predicate / values / formatting / basket staging")
        ok_all = ok_build and ok1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("-" * 60)
    print("%s" % ("ALL PASS" if ok_all else "FAILURES"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
