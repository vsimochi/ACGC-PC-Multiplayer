#!/usr/bin/env python3
"""run_wildlife_protocol_tests.py - runs the older wildlife PROTOCOL tests one after another (they share the game binary, the working directory and UDP ports, so never in parallel) and writes a
one-line result per test to proto_summary.txt, the full output of each to proto_<name>.txt.

Each test is a real `AnimalCrossing.exe` host (some also a real client) with scripted FakeClient peers. They need the disposable fixture directory NET_SPIKE_GAME_BIN (default: the
bin_fixture4_wplay copy that the wplay-based tests create with the freshly built exe). test_wildlife_spawn_wire.py talks to an already-running host, so this runner starts one for it.
These tests predate the host releasing records by itself (attendance release, B-7 abandonment, natural-end release): they seed records with a scripted peer that leaves again, boot a second process
for 30-60 s and expect the records to still exist. Their subject is the catch/validation protocol, so by default the host runs with the TEST-ONLY AC_TEST_WILDLIFE_KEEP_RECORDS=1 (records are never
released by the host itself). `--no-keep` runs them against the real record lifetime (this reproduces the failures that are due to the lifetime change; see TODO.md section 2 / WS3).
Usage: python run_wildlife_protocol_tests.py [--no-keep] [name ...]      (names: catch bug_catch catch_realgrant exchange_gate flag_gate spawn_wire; default all)"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import wplay_lib as W  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = W.BIN
if "--no-keep" in sys.argv:
    sys.argv.remove("--no-keep")
else:
    os.environ["AC_TEST_WILDLIFE_KEEP_RECORDS"] = "1"
TESTS = [
    ("catch", "test_wildlife_catch.py", ["--port", "7799"]),
    ("bug_catch", "test_wildlife_bug_catch.py", ["--port", "7820"]),
    ("catch_realgrant", "test_wildlife_catch_realgrant.py", ["--port", "7801"]),
    ("exchange_gate", "test_wildlife_catch_exchange_gate.py", ["--port", "7810"]),
    ("flag_gate", "test_wildlife_flag_gate_bugfix.py", ["--port", "7788"]),
    ("spawn_wire", "test_wildlife_spawn_wire.py", None),
]


def kill_games():
    """stops only the game processes that run from the test fixture directory (never a normal game the user may have open)"""
    ps = "Get-Process AnimalCrossing -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '%s*' } | Stop-Process -Force" % W.BIN.replace("'", "''")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)


def run_one(name, script, args):
    out = os.path.join(HERE, "proto_%s.txt" % name)
    t0 = time.time()
    host = None
    try:
        if args is None:  # spawn_wire: needs a running host
            import net_spike_lib as L
            host = L.HostProcess(port=7831, extra_args=["--bootstrap-resident", "0", "-debug"], log_path=os.path.join(HERE, "proto_spawn_wire_host.log"), bin_dir=W.BIN).start()
            if not (host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)):
                return "host did not reach the field", out, time.time() - t0
            L.resolve_host_town("127.0.0.1", 7831)
            cmd = [sys.executable, "-u", script, "127.0.0.1", "7831"]
        else:
            cmd = [sys.executable, "-u", script] + args
        with open(out, "w") as f:
            try:
                r = subprocess.run(cmd, cwd=HERE, stdout=f, stderr=subprocess.STDOUT, timeout=1500)
                rc = r.returncode
            except subprocess.TimeoutExpired:
                rc = "TIMEOUT"
    finally:
        if host is not None:
            host.stop()
        kill_games()
    txt = open(out, errors="replace").read()
    m = re.findall(r"(\d+)/(\d+) checks passed", txt)
    fails = len(re.findall(r"^FAIL", txt, re.M))
    passes = len(re.findall(r"^PASS", txt, re.M))
    summ = ("%s/%s checks passed" % m[-1]) if m else ("%d PASS / %d FAIL lines" % (passes, fails))
    return "rc=%s :: %s :: FAIL lines: %d" % (rc, summ, fails), out, time.time() - t0


def main():
    want = sys.argv[1:] or [t[0] for t in TESTS]
    kill_games()
    W.make_fixture()  # a fresh disposable copy with the freshly built exe (a stale copy would silently test an old build)
    summary = os.path.join(HERE, "proto_summary.txt")
    open(summary, "a").write("%s RUN %s\n" % (time.strftime("%H:%M:%S"), " ".join(want)))
    for name, script, args in TESTS:
        if name not in want:
            continue
        kill_games()
        open(summary, "a").write("%s START %s\n" % (time.strftime("%H:%M:%S"), name))
        res, out, dt = run_one(name, script, args)
        open(summary, "a").write("%s DONE  %s (%.0fs) :: %s\n" % (time.strftime("%H:%M:%S"), name, dt, res))
    open(summary, "a").write("%s ALL DONE\n" % time.strftime("%H:%M:%S"))


if __name__ == "__main__":
    main()
