#!/usr/bin/env python3
"""run_baseline_exit.py - is the exit crash (0xC0000005 on WM_CLOSE while a remote-player actor exists)
pre-existing? Runs ANY AnimalCrossing.exe (default: the HEAD-79b28f1 baseline build) as host and client,
walks both to town, waits for the remote-player actor creation lines on both sides, then WM_CLOSEs one
process (client or host), then the other, and records exit codes + last log lines. Controls: a single
process with no peer.

Also timestamps every '[PC] GCI save: written successfully' line per process (polling the logs), to see
whether the two processes -- which share CWD, save/card_a/*.gci and the *.gci.tmp temp file -- write the
save at the same time.

Usage: python run_baseline_exit.py --exe PATH [--runs 3] [--label baseline] [--modes client,host,none,plain]
  modes: client = close the client first; host = close the host first; none = one --host process, no
         peer; plain = one process, no network args.
Every process started is stopped before the script exits.
"""
import argparse
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_two_process_smoke as S  # noqa: E402

NOISE = re.compile(r"LOGO\] draw|NEOS_OUT|movement send rate|\[REMOTE\]\[DIAG\] player \d+ actor pos")
GCI_RX = re.compile(r"\[PC\] GCI save: (written successfully|[^\n]*fail[^\n]*|rename[^\n]*)")
CREATED_RX = r"\[NET\]\[REMOTE\] created remote-player actor for player (\d+)"


class Watcher(threading.Thread):
    """Polls game logs and records the wall-clock time each GCI save line first appears."""

    def __init__(self):
        super().__init__(daemon=True)
        self.games = []
        self.events = []  # (t, label, line)
        self.seen = {}
        self.stop = False

    def add(self, g):
        self.games.append(g)
        self.seen[g.label] = 0

    def run(self):
        while not self.stop:
            for g in list(self.games):
                lines = GCI_RX.findall(g.text())
                # findall returns group(1); re-scan for whole lines
                whole = [m.group(0) for m in GCI_RX.finditer(g.text())]
                n = self.seen[g.label]
                for ln in whole[n:]:
                    self.events.append((time.time(), g.label, ln))
                self.seen[g.label] = len(whole)
            time.sleep(0.2)


def tail(g, n=6):
    ls = [ln.rstrip() for ln in g.text().splitlines() if not NOISE.search(ln)]
    return ls[-n:]


def run_pair(label, first, w, run_idx):
    """host+client both to town, both see the remote actor, close `first` then the other."""
    tag = f"{label}_{first}_first_r{run_idx}"
    host = S.Game(f"{tag}_host", ["--host", "7788", "--verbose"])
    cli = S.Game(f"{tag}_client", ["--connect", "127.0.0.1:7788", "--verbose"])
    rec = {"tag": tag, "first": first}
    host.start()
    w.add(host)
    ok_h = host.boot_to_town()
    cli.start()
    w.add(cli)
    ok_c = cli.boot_to_town()
    seen_h = host.wait(CREATED_RX, 40)
    seen_c = cli.wait(CREATED_RX, 40)
    rec.update(boot_host=ok_h, boot_client=ok_c, actor_on_host=seen_h, actor_on_client=seen_c)
    print(f"[{tag}] boot host={ok_h} client={ok_c}; remote-actor created: on host={seen_h} on client={seen_c}", flush=True)
    for g in (host, cli):
        for ln in g.find(r"(\[NET\]\[REMOTE\] created remote-player actor[^\n]*)"):
            print(f"    {g.label.split('_')[-1]} | {ln}")
    time.sleep(3.0)
    order = (cli, host) if first == "client" else (host, cli)
    for g in order:
        g.close_clean(timeout=30)
        time.sleep(1.5)
    for g in (host, cli):
        role = g.label.split("_")[-1]
        rc = g.proc.returncode
        rec[role] = rc
        print(f"    {role:<6} exit {rc if rc is None else hex(rc & 0xFFFFFFFF)}; last lines:")
        for ln in tail(g, 5):
            print(f"        | {ln[:150]}")
    return rec


def run_transition(label, mover, w, run_idx):
    """Disconnect-while-scene-transition: both to town, then the `mover` walks into the house (holds UP;
    the door is next to the spawn point) while the OTHER process is closed ~1 s into the walk. Records
    SCENE_MODE lines the mover logged after the walk started (proof a transition really happened), whether
    the mover survived, and both exit codes (mover closed cleanly afterwards)."""
    tag = f"{label}_trans_{mover}_r{run_idx}"
    host = S.Game(f"{tag}_host", ["--host", "7788", "--verbose"])
    cli = S.Game(f"{tag}_client", ["--connect", "127.0.0.1:7788", "--verbose"])
    host.start()
    w.add(host)
    ok_h = host.boot_to_town()
    cli.start()
    w.add(cli)
    ok_c = cli.boot_to_town()
    seen = host.wait(CREATED_RX, 40) and cli.wait(CREATED_RX, 40)
    print(f"[{tag}] boot host={ok_h} client={ok_c}; remote actors on both={seen}", flush=True)
    time.sleep(3.0)
    mv, other = (host, cli) if mover == "host" else (cli, host)
    before = len(re.findall(r"\[SCENE_MODE\]", mv.text()))
    th = threading.Thread(target=lambda: mv.hold("UP", 3.5), daemon=True)
    th.start()
    time.sleep(1.0)
    other.close_clean(timeout=30)
    th.join()
    time.sleep(4.0)
    scenes = re.findall(r"\[SCENE_MODE\][^\n]*", mv.text())[before:]
    alive = mv.alive()
    mv.shot(f"trans_{mover}_r{run_idx}")
    print(f"    mover={mover} alive after the other closed: {alive}; scene transitions logged by the mover: {scenes}")
    mv.close_clean(timeout=30)
    for g in (host, cli):
        role = g.label.split("_")[-1]
        rc = g.proc.returncode
        print(f"    {role:<6} exit {rc if rc is None else hex(rc & 0xFFFFFFFF)}; last lines:")
        for ln in tail(g, 4):
            print(f"        | {ln[:150]}")
    return {"tag": tag, "first": "trans_" + mover, "host": host.proc.returncode, "client": cli.proc.returncode,
            "scenes": len(scenes), "mover_alive": alive}


def run_single(label, mode, w, run_idx):
    tag = f"{label}_{mode}_r{run_idx}"
    args = ["--host", "7788", "--verbose"] if mode == "none" else ["--verbose"]
    g = S.Game(tag, args)
    g.start()
    w.add(g)
    ok = g.boot_to_town()
    time.sleep(6.0)
    g.close_clean(timeout=30)
    rc = g.proc.returncode
    print(f"[{tag}] boot={ok}; exit {rc if rc is None else hex(rc & 0xFFFFFFFF)}; last lines:")
    for ln in tail(g, 5):
        print(f"        | {ln[:150]}")
    return {"tag": tag, "first": mode, "boot_host": ok, "host": rc}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--label", default="baseline")
    ap.add_argument("--modes", default="client,host,none,plain")
    a = ap.parse_args()
    S.EXE = a.exe
    S.OUT = os.path.join(HERE, "logs", f"exit_{a.label}")
    os.makedirs(S.OUT, exist_ok=True)
    running = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AnimalCrossing.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
    if "AnimalCrossing.exe" in running:
        print("ABORT: an AnimalCrossing.exe is already running")
        return 1
    w = Watcher()
    w.start()
    rows = []
    try:
        for mode in a.modes.split(","):
            for i in range(1, a.runs + 1):
                if mode in ("client", "host"):
                    rows.append(run_pair(a.label, mode, w, i))
                elif mode in ("trans_host", "trans_client"):
                    rows.append(run_transition(a.label, mode.split("_")[1], w, i))
                else:
                    rows.append(run_single(a.label, mode, w, i))
    finally:
        w.stop = True
        for g in S.procs:
            if g.alive():
                g.kill()
        subprocess.run(["taskkill", "/F", "/IM", "AnimalCrossing.exe"], capture_output=True)
    print("=" * 78)
    print(f"EXIT-CODE TABLE for {a.exe}")
    fmt = lambda v: "-" if v is None else hex(v & 0xFFFFFFFF)
    for r in rows:
        if r["first"] in ("client", "host"):
            print(f"  {r['tag']:<30} closed {r['first']:<6} first | host {fmt(r.get('host')):<11} client {fmt(r.get('client')):<11}"
                  f" | remote actor on host={r.get('actor_on_host')} client={r.get('actor_on_client')}")
        elif r["first"].startswith("trans_"):
            print(f"  {r['tag']:<30} {r['first']:<12} | host {fmt(r.get('host')):<11} client {fmt(r.get('client')):<11}"
                  f" | scene transitions logged by mover: {r.get('scenes')}, mover alive after other closed: {r.get('mover_alive')}")
        else:
            print(f"  {r['tag']:<30} single process, no peer      | exit {fmt(r.get('host'))}")
    print("GCI save writes (wall clock, by process):")
    t0 = w.events[0][0] if w.events else 0
    for t, lbl, ln in w.events:
        print(f"  +{t - t0:7.1f}s {lbl:<36} {ln}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
