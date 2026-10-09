#!/usr/bin/env python3
"""diag_exit_crash.py - diagnostic for the 0xC0000005 seen when a game process is closed cleanly
(WM_CLOSE) while a remote player is connected. Runs a normal host and a CLIENT UNDER gdb (batch),
walks both into town, waits for READY, closes the client window, and prints gdb's backtrace of the
access violation. Diagnostic only -- it changes nothing in the game.

Usage: python diag_exit_crash.py [--role client|host]   (which side runs under gdb; default client)
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_two_process_smoke as S  # noqa: E402
import net_spike_lib as L  # noqa: E402

GDB = os.environ.get("NET_SPIKE_GDB", r"C:\msys64\ucrt64\bin\gdb.exe")


def pids(name="AnimalCrossing.exe"):
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return {int(m.group(1)) for m in re.finditer(r'"%s","(\d+)"' % re.escape(name), out)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="client", choices=("client", "host"))
    a = ap.parse_args()
    os.makedirs(S.OUT, exist_ok=True)
    cmds = os.path.join(S.OUT, "diag_gdb_cmds.txt")
    with open(cmds, "w") as f:
        f.write("set pagination off\nset confirm off\nhandle SIGSEGV stop print\nrun\nbt full 30\n"
                "info registers rip rsp rax rcx rdx\nx/6i $pc\ninfo threads\nthread apply all bt 12\nkill\nquit\n")
    port = 7788
    host_args = ["--host", str(port), "--verbose", "--pickup-test-seed"]
    cli_args = ["--connect", f"127.0.0.1:{port}", "--verbose"]
    gdb_log = os.path.join(S.OUT, f"diag_gdb_{a.role}.log")
    procs = []
    try:
        if a.role == "client":
            host, ok = S.launch_and_boot("diag_host", host_args)
            base = pids()
            gp = subprocess.Popen([GDB, "-batch", "-x", cmds, "--args", S.EXE] + cli_args, cwd=S.BIN,
                                  stdout=open(gdb_log, "wb"), stderr=subprocess.STDOUT)
            procs.append(gp)
            t0 = time.time()
            while time.time() - t0 < 30 and not (pids() - base):
                time.sleep(0.3)
            new = pids() - base
            print(f"[diag] game pid under gdb: {new}")
            target = S.Game("diag_client_gdb", [])
            target.log_path = gdb_log
            target.proc = type("P", (), {"pid": next(iter(new)), "poll": lambda self: None if next(iter(new)) in pids() else 0})()
            target._fp = None
            S.procs.append(target)
            other = host
        else:
            base = pids()
            gp = subprocess.Popen([GDB, "-batch", "-x", cmds, "--args", S.EXE] + host_args, cwd=S.BIN,
                                  stdout=open(gdb_log, "wb"), stderr=subprocess.STDOUT)
            procs.append(gp)
            t0 = time.time()
            while time.time() - t0 < 30 and not (pids() - base):
                time.sleep(0.3)
            new = pids() - base
            print(f"[diag] game pid under gdb: {new}")
            target = S.Game("diag_host_gdb", [])
            target.log_path = gdb_log
            target.proc = type("P", (), {"pid": next(iter(new)), "poll": lambda self: None if next(iter(new)) in pids() else 0})()
            target._fp = None
            S.procs.append(target)
            other = None
        ok = target.boot_to_town()
        print(f"[diag] gdb-run process reached town: {ok}")
        if a.role == "host":
            other, ok2 = S.launch_and_boot("diag_client", cli_args)
        else:
            pass
        target.wait(r"handshake complete|identity OK", 60)
        time.sleep(4)
        print("[diag] closing the process under gdb (WM_CLOSE) with the peer connected")
        target.window().close()
        gp.wait(60)
        print(open(gdb_log, "rb").read().decode("utf-8", "replace")[-6000:])
    finally:
        for g in S.procs:
            try:
                if g.alive() and hasattr(g.proc, "terminate"):
                    g.kill()
            except Exception:
                pass
        for pid in pids():
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        for p in procs:
            if p.poll() is None:
                p.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
