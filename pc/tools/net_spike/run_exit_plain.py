#!/usr/bin/env python3
"""run_exit_plain.py - plain-launch exit controls. (a) no args at all: log is empty (no --verbose), so walk
blind (wait for window, START, A x N, screenshot) then WM_CLOSE; (b) --verbose only (no network args), walk by log.
Terminates only its own PIDs. Injects keystrokes."""
import os, re, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import run_exit_matrix as M
from game_input import GameWindow
OUT = M.OUT

# (b) --verbose only
g, ok = M.launch_boot("control_plain_verbose", "plain(--verbose only)", ["--verbose"])
if ok:
    time.sleep(3.0); g.close("control_plain_verbose", 1)
time.sleep(2.0)

# (a) bare
log = os.path.join(OUT, "control_bare.log")
fp = open(log, "wb")
p = subprocess.Popen([M.EXE], cwd=M.BIN, stdout=fp, stderr=subprocess.STDOUT)
print("[plain] bare launch pid", p.pid, flush=True)
w = GameWindow(p.pid)
end = time.monotonic() + 60
while not w.ready() and time.monotonic() < end and p.poll() is None:
    time.sleep(0.5)
print("[plain] window ready", w.ready(), "alive", p.poll() is None, flush=True)
time.sleep(25)   # title screen appears ~ (verbose boots reach press_start in ~20-30s)
w.press("START", after=3.0)
for i in range(30):
    if p.poll() is not None: break
    w.press("A", after=1.5)
time.sleep(8)
alive = p.poll() is None
try:
    w.screenshot(os.path.join(OUT, "control_bare_before_close.png"))
except Exception as e:
    print("[plain] screenshot failed", e)
how = "WM_CLOSE"
if alive:
    w.close()
    try:
        p.wait(30)
    except subprocess.TimeoutExpired:
        how = "no exit in 30s; terminated"; p.terminate(); p.wait(10)
else:
    how = "already exited before close"
print(f"[plain] bare pid {p.pid} {how} rc={p.returncode & 0xFFFFFFFF:#010x}", flush=True)
