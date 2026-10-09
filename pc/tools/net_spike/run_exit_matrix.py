#!/usr/bin/env python3
"""run_exit_matrix.py - exit-close matrix: 3x host-first, 3x client-first, 2 single-process controls.
Each process: own log in logs/final_suite/exit_matrix/, WM_CLOSE by PID window only, exit code recorded.
Terminates only PIDs it launched. Injects keystrokes: do not type elsewhere."""
import os, re, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from game_input import GameWindow  # noqa

import net_spike_lib as L  # noqa: E402
# Guardrail: never launch from the LIVE bin (holds the live save) or the PROTECTED bin_talkfix. Point NET_SPIKE_GAME_BIN at a
# disposable copy (e.g. pcuild64in_fixture4); with the default (live) bin this tool refuses to start.
BIN = os.path.abspath(L.GAME_BIN_DIR)
L.require_launchable_bin_dir(BIN)
EXE = os.path.join(BIN, "AnimalCrossing.exe")
OUT = os.path.join(HERE, "logs", "final_suite", "exit_matrix")
os.makedirs(OUT, exist_ok=True)
PORT = 7788
table = []   # dict rows
boots = []   # (label, pid, boot_ok, died_rc)


class G:
    def __init__(self, label, role, args):
        self.label, self.role, self.args = label, role, args
        self.log = os.path.join(OUT, label + ".log")
        self.fp = open(self.log, "wb")
        self.p = subprocess.Popen([EXE] + args, cwd=BIN, stdout=self.fp, stderr=subprocess.STDOUT)
        self.w = GameWindow(self.p.pid)
        print(f"[matrix] launched {label} pid {self.p.pid} args {args}", flush=True)

    def text(self):
        try:
            return open(self.log, "rb").read().decode("utf-8", "replace")
        except OSError:
            return ""

    def alive(self):
        return self.p.poll() is None

    def wait(self, rx, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if re.search(rx, self.text()):
                return True
            if not self.alive():
                return bool(re.search(rx, self.text()))
            time.sleep(0.25)
        return bool(re.search(rx, self.text()))

    def win(self):
        end = time.monotonic() + 20
        while not self.w.ready() and time.monotonic() < end:
            time.sleep(0.2)
        return self.w

    def boot(self, timeout=150.0):
        if not self.wait(r"press_start_opacity=255", timeout):
            return False
        w = self.win()
        w.press("START", after=3.0)
        n = 0
        end = time.monotonic() + timeout
        while not re.search(r"\[SCENE_MODE\] 3 -> 1", self.text()) and self.alive() and time.monotonic() < end:
            w.press("A", after=1.5)
            n += 1
            if n > 40:
                break
        ok = bool(re.search(r"\[SCENE_MODE\] 3 -> 1", self.text()))
        time.sleep(3.0)
        ok = ok and self.alive()
        print(f"[matrix] {self.label} boot {'OK' if ok else 'FAILED'} ({n} A)", flush=True)
        return ok

    def close(self, run, order, timeout=30.0):
        how = "WM_CLOSE"
        if not self.alive():
            how = "already exited before close"
        else:
            self.win().close()
            try:
                self.p.wait(timeout)
            except subprocess.TimeoutExpired:
                how = f"no exit in {timeout}s after WM_CLOSE; terminated"
                self.p.terminate()
                self.p.wait(10)
        rc = self.p.returncode
        self.fp.close()
        t = self.text()
        low = bool(re.search(r"\[LOWADDR\]", t.strip().splitlines()[-1])) if t.strip() else False
        lowany = "[LOWADDR]" in t
        tail = t.strip().splitlines()[-3:]
        table.append(dict(run=run, role=self.role, label=self.label, pid=self.p.pid, order=order, how=how, rc=rc,
                          last_line_lowaddr=low, lowaddr_in_log=lowany, tail=tail, log=self.log))
        print(f"[matrix] {run} {self.label} pid {self.p.pid} {how} rc={rc & 0xFFFFFFFF:#010x} lowaddr_last={low}", flush=True)
        if rc != 0:
            print("   last10:\n   " + "\n   ".join(t.strip().splitlines()[-10:]), flush=True)


def launch_boot(label, role, args):
    g = G(label, role, args)
    ok = g.boot()
    boots.append((label, g.p.pid, ok, None if g.alive() else g.p.returncode))
    if ok:
        return g, True
    if g.alive():
        g.p.terminate(); g.p.wait(10); g.fp.close()
        print(f"[matrix] {label} boot failed but alive (terminated by harness)")
    rc = g.p.returncode
    table.append(dict(run=label, role=role, label=label, pid=g.p.pid, order=0, how="BOOT FAILURE", rc=rc,
                      last_line_lowaddr=False, lowaddr_in_log=False, tail=g.text().strip().splitlines()[-3:], log=g.log))
    print(f"[matrix] BOOT FAILURE {label} rc={rc & 0xFFFFFFFF:#010x}; retry once", flush=True)
    g2 = G(label + "_retry", role, args)
    ok2 = g2.boot()
    boots.append((label + "_retry", g2.p.pid, ok2, None if g2.alive() else g2.p.returncode))
    if not ok2 and g2.alive():
        g2.p.terminate(); g2.p.wait(10); g2.fp.close()
    return g2, ok2


def pair(name, first):
    h, ok = launch_boot(name + "_host", "host", ["--host", str(PORT), "--verbose"])
    if not ok:
        return
    if not h.wait(r"\[NET\] hosting", 5):
        print("[matrix] host not hosting?")
    c, ok = launch_boot(name + "_client", "client", ["--connect", f"127.0.0.1:{PORT}", "--verbose"])
    if not ok:
        h.close(name, 1)
        return
    a = h.wait(r"created remote-player actor", 60)
    b = c.wait(r"created remote-player actor", 60)
    print(f"[matrix] {name}: remote actor host={a} client={b}", flush=True)
    time.sleep(2.0)
    order = (h, c) if first == "host" else (c, h)
    order[0].close(name, 1)
    time.sleep(2.0)
    order[1].close(name, 2)
    time.sleep(3.0)


def control(name, args, role):
    g, ok = launch_boot(name, role, args)
    if not ok:
        return
    time.sleep(3.0)
    g.close(name, 1)
    time.sleep(2.0)


if __name__ == "__main__":
    for i in range(1, 4):
        pair(f"hostfirst{i}", "host")
    for i in range(1, 4):
        pair(f"clientfirst{i}", "client")
    control("control_host_nopeer", ["--host", str(PORT), "--verbose"], "host(no peer)")
    control("control_plain", [], "plain")
    print("=" * 60)
    for t in table:
        print(f"{t['run']:<22} {t['role']:<14} pid {t['pid']:<7} order {t['order']} {t['how']:<12} rc={t['rc'] & 0xFFFFFFFF:#010x} "
              f"lowaddr_last_line={t['last_line_lowaddr']} lowaddr_in_log={t['lowaddr_in_log']} {os.path.basename(t['log'])}")
    print("BOOTS:")
    for b in boots:
        print(b)
