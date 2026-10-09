#!/usr/bin/env python3
"""diag_boot_crash.py - statistics + stack capture for the intermittent boot crash (0xC0000005 after
"[PC] GCI save loaded successfully"). Launches the game N times (no input, no networking role), each
time waiting for the title screen ("press_start_opacity=255") or an early exit, and records exit codes.
--gdb runs each launch under `gdb -batch` and prints the backtrace of a crash.

Usage: python diag_boot_crash.py [--n 20] [--gdb] [--gap 0.0]
  --gap S   seconds to wait between trials (to test whether back-to-back launches matter)
Every process is stopped before the script exits.
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

L.require_launchable_bin_dir(L.GAME_BIN_DIR)
EXE = os.path.join(L.GAME_BIN_DIR, "AnimalCrossing.exe")
GDB = os.environ.get("NET_SPIKE_GDB", r"C:\msys64\ucrt64\bin\gdb.exe")
OUT = os.path.join(HERE, "logs", "bootdiag")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--gdb", action="store_true")
    ap.add_argument("--gap", type=float, default=0.0)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    cmds = os.path.join(OUT, "gdb_cmds.txt")
    with open(cmds, "w") as f:
        f.write("set pagination off\nset confirm off\nhandle SIGSEGV stop print\nrun\nbt full 25\n"
                "info registers rip rsp rax rcx rdx\nx/6i $pc\nkill\nquit\n")
    crashes = 0
    rows = []
    for i in range(a.n):
        log = os.path.join(OUT, f"{a.tag}trial{i:02d}{'_gdb' if a.gdb else ''}.log")
        fp = open(log, "wb")
        if a.gdb:
            p = subprocess.Popen([GDB, "-batch", "-x", cmds, "--args", EXE, "--host", "7788", "--verbose"],
                                 cwd=L.GAME_BIN_DIR, stdout=fp, stderr=subprocess.STDOUT)
        else:
            p = subprocess.Popen([EXE, "--host", "7788", "--verbose"], cwd=L.GAME_BIN_DIR, stdout=fp, stderr=subprocess.STDOUT)
        t0 = time.time()
        outcome = None
        while time.time() - t0 < 60:
            txt = open(log, "rb").read().decode("utf-8", "replace")
            if "press_start_opacity=255" in txt:
                outcome = "title reached"
                break
            if p.poll() is not None:
                outcome = "EXITED EARLY"
                break
            if "received signal SIGSEGV" in txt:
                outcome = "SIGSEGV (gdb)"
                time.sleep(3)
                break
            time.sleep(0.25)
        outcome = outcome or "timeout"
        rc = p.poll()
        if outcome != "title reached":
            crashes += 1
        if rc is None:
            subprocess.run(["taskkill", "/F", "/IM", "AnimalCrossing.exe"], capture_output=True)
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
        fp.close()
        txt = open(log, "rb").read().decode("utf-8", "replace")
        last = [ln for ln in txt.splitlines() if "LOGO] draw" not in ln and "NEOS_OUT" not in ln][-1:]
        rows.append((i, outcome, rc))
        print(f"trial {i:02d}: {outcome}; exit before stop={rc if rc is None else hex(rc & 0xFFFFFFFF)}; "
              f"last log line: {last[0][:90] if last else ''}", flush=True)
        if a.gdb and outcome != "title reached":
            m = re.search(r"received signal SIGSEGV.*", txt, re.S)
            print((m.group(0) if m else txt[-2500:])[:3500])
        time.sleep(a.gap)
    print(f"boots: {a.n}, failed: {crashes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
