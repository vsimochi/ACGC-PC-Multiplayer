#!/usr/bin/env python3
"""gp_ctl.py - small controller daemon for the interactive two-process gameplay regression.

  python gp_ctl.py serve            # long-running daemon; owns the Popen objects (so exit codes are captured)
  python gp_ctl.py <cmd> [args...]  # thin client: sends one command to the daemon and prints the reply

Commands (label = a name you choose for a process, e.g. host, client):
  launch <label> <args...>          start AnimalCrossing.exe (CWD = pc/build64/bin, stdout+stderr -> logs/gameplay_final/<label>.log)
  boot <label> [timeout]            title -> START -> A... until "[SCENE_MODE] 3 -> 1" (same as run_two_process_smoke.py)
  press <label> <BTN> [hold] [after]
  seq <label> <step> ...            step = BTN | BTN:seconds | wait:seconds ; e.g. seq client W:1.2 A wait:1
  shot <label> <name>               window screenshot -> logs/gameplay_final/<name>.png
  grep <label> <regex> [maxlines]   matching log lines with line numbers
  wait <label> <regex> <timeout> [count]
  close <label>                     WM_CLOSE to that PID's window, wait for exit, record exit code
  kill <label>                      TerminateProcess on that PID only
  status                            table of all launched processes
  quit                              stop daemon (does not touch running games)
Only ever acts on PIDs it launched itself.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from game_input import GameWindow, capture_window_png  # noqa: E402

import net_spike_lib as L  # noqa: E402
# Guardrail: never launch from the LIVE bin (holds the live save) or the PROTECTED bin_talkfix. Point NET_SPIKE_GAME_BIN at a
# disposable copy (e.g. pcuild64in_fixture4); with the default (live) bin this tool refuses to start.
BIN = os.path.abspath(L.GAME_BIN_DIR)
L.require_launchable_bin_dir(BIN)
EXE = os.path.join(BIN, "AnimalCrossing.exe")
OUT = os.path.join(HERE, "logs", "gameplay_final")
PORT = 47788

procs = {}      # label -> dict(proc, log, win, fp, order, how, rc)
order = [0]


def text(label):
    try:
        with open(procs[label]["log"], "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def alive(label):
    return procs[label]["proc"].poll() is None


def win(label):
    p = procs[label]
    if p["win"] is None:
        p["win"] = GameWindow(p["proc"].pid)
    deadline = time.monotonic() + 20
    while not p["win"].ready() and time.monotonic() < deadline:
        time.sleep(0.2)
    return p["win"]


def cmd_launch(label, *args):
    if label in procs and alive(label):
        return f"ERR {label} already running"
    if label in procs:
        return f"ERR label {label} already used; choose a new label"
    log = os.path.join(OUT, f"{label}.log")
    fp = open(log, "wb")
    pr = subprocess.Popen([EXE] + list(args), cwd=BIN, stdout=fp, stderr=subprocess.STDOUT)
    order[0] += 1
    procs[label] = dict(proc=pr, log=log, win=None, fp=fp, order=order[0], how=None, rc=None, args=list(args),
                        t0=time.strftime("%H:%M:%S"))
    return f"launched {label} pid {pr.pid} order {order[0]} args {list(args)}"


def cmd_boot(label, timeout="150"):
    timeout = float(timeout)
    deadline = time.monotonic() + timeout
    while not re.search(r"press_start_opacity=255", text(label)):
        if not alive(label):
            return f"BOOT DIED rc={procs[label]['proc'].poll():#x} (before title)"
        if time.monotonic() > deadline:
            return "BOOT TIMEOUT waiting for title"
        time.sleep(0.25)
    w = win(label)
    w.press("START", after=3.0)
    n = 0
    while not re.search(r"\[SCENE_MODE\] 3 -> 1", text(label)) and alive(label) and time.monotonic() < deadline:
        w.press("A", after=1.5)
        n += 1
        if n > 40:
            break
    ok = bool(re.search(r"\[SCENE_MODE\] 3 -> 1", text(label)))
    time.sleep(3.0)
    if not alive(label):
        return f"BOOT DIED rc={procs[label]['proc'].poll() & 0xFFFFFFFF:#010x} after {n} A presses"
    return f"boot {'OK' if ok else 'FAILED'} ({n} x A)"


def cmd_press(label, btn, hold="0.12", after="0.25"):
    win(label).press(btn, hold=float(hold), after=float(after))
    return "ok"


def cmd_seq(label, *steps):
    w = win(label)
    for s in steps:
        if s.startswith("wait:"):
            time.sleep(float(s[5:]))
        elif ":" in s:
            b, sec = s.split(":", 1)
            w.hold(b, float(sec))
            time.sleep(0.15)
        else:
            w.press(s)
    return "ok"


def cmd_shot(label, name, scale="2"):
    path = os.path.join(OUT, f"{name}.png")
    w = win(label)
    from game_input import focus
    focus(w.hwnd)
    time.sleep(0.15)
    capture_window_png(w.hwnd, path, scale=int(scale))
    return path


def cmd_grep(label, rx, maxlines="60"):
    out = []
    for i, ln in enumerate(text(label).splitlines(), 1):
        if re.search(rx, ln):
            out.append(f"{i}: {ln.strip()[:260]}")
    mx = int(maxlines)
    if len(out) > mx:
        out = out[:mx // 2] + [f"... ({len(out) - mx} omitted) ..."] + out[-(mx // 2):]
    return "\n".join(out) if out else "(no match)"


def cmd_wait(label, rx, timeout, count="1"):
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        if len(re.findall(rx, text(label))) >= int(count):
            return "found"
        if not alive(label):
            break
        time.sleep(0.25)
    return "found" if len(re.findall(rx, text(label))) >= int(count) else "TIMEOUT"


def _finish(label, how):
    p = procs[label]
    p["how"], p["rc"] = how, p["proc"].returncode
    if p["fp"]:
        p["fp"].close()
        p["fp"] = None
    rc = p["rc"]
    return f"{label} pid {p['proc'].pid}: {how}, exit code {rc & 0xFFFFFFFF:#010x}"


def cmd_close(label, timeout="30"):
    p = procs[label]
    if not alive(label):
        return _finish(label, "already exited before close")
    win(label).close()
    try:
        p["proc"].wait(float(timeout))
        return _finish(label, "WM_CLOSE (clean shutdown)")
    except subprocess.TimeoutExpired:
        p["proc"].terminate()
        p["proc"].wait(10)
        return _finish(label, "did NOT exit within timeout of WM_CLOSE; terminated")


def cmd_kill(label):
    p = procs[label]
    if alive(label):
        p["proc"].terminate()
        p["proc"].wait(10)
    return _finish(label, "killed by harness (TerminateProcess, no WM_CLOSE)")


def cmd_status():
    rows = []
    for l, p in sorted(procs.items(), key=lambda kv: kv[1]["order"]):
        rc = p["proc"].poll()
        rows.append(f"#{p['order']} {l:<14} pid {p['proc'].pid:<7} started {p['t0']} "
                    f"{'RUNNING' if rc is None else 'exited ' + format(rc & 0xFFFFFFFF, '#010x')} "
                    f"how={p['how']}")
    return "\n".join(rows) if rows else "(none)"


CMDS = {"launch": cmd_launch, "boot": cmd_boot, "press": cmd_press, "seq": cmd_seq, "shot": cmd_shot,
        "grep": cmd_grep, "wait": cmd_wait, "close": cmd_close, "kill": cmd_kill, "status": cmd_status}


def serve():
    os.makedirs(OUT, exist_ok=True)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(4)
    print("gp_ctl daemon listening", flush=True)
    while True:
        c, _ = srv.accept()
        data = b""
        while not data.endswith(b"\n"):
            chunk = c.recv(65536)
            if not chunk:
                break
            data += chunk
        try:
            argv = json.loads(data.decode())
            if argv[0] == "quit":
                c.sendall(b"bye")
                c.close()
                return
            res = CMDS[argv[0]](*argv[1:])
        except Exception as e:  # noqa
            res = f"ERR {type(e).__name__}: {e}"
        c.sendall(res.encode("utf-8", "replace"))
        c.close()


def client(argv):
    s = socket.socket()
    s.settimeout(900)
    s.connect(("127.0.0.1", PORT))
    s.sendall((json.dumps(argv) + "\n").encode())
    buf = b""
    while True:
        ch = s.recv(65536)
        if not ch:
            break
        buf += ch
    print(buf.decode("utf-8", "replace"))


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve()
    else:
        client(sys.argv[1:])
