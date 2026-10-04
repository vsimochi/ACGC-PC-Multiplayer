#!/usr/bin/env python3
"""test_observer_real_client.py - REAL smoke test of the hidden server observer: a REAL host `--host --host-observer` and a REAL client
`--connect ... --bootstrap-resident 0` (resident 0, the slot a plain `--host` bootstrap would occupy).

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation, no manual play, NO visual verification). Disposable
pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN overrides), ports 12200+; the fixture save is snapshotted by net_spike_lib's guard and restored before the
client launches and at the end. Every assertion comes from the two processes' own log lines.

  O1  the observer host reaches 'observer active' -> 'world ready' without a resident
  O2  the real client claims resident 0, reaches READY (snapshot applied) and ADOPTS its host record (D3: MIGRATE -> PUSH_FULL rev 1 -> adopted)
  O3  the client knows the host has no avatar: log '[NET][OBSERVER] client: host has no avatar (no host puppet)', and NO host puppet is ever created
      ('[NET][REMOTE] player 8 READY' absent); the host logs the matching 'host has no avatar' line for that peer and 'bound to resident 0'
  O4  the host never refused resident 0 as its own, and the client log has no violation / refusal line
Usage: python test_observer_real_client.py [--port 12200]
"""
import argparse
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402


def run(args, results):
    check = lambda d, c: L.check(d, bool(c), results)
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    snap = os.path.join(args.log_dir, "observer_real_save_snapshot")
    shutil.rmtree(snap, ignore_errors=True)
    shutil.copytree(save_dir, snap)
    try:
        host = None
        for i in range(3):
            h = L.HostProcess(port=args.port, extra_args=["--host-observer"], log_path=os.path.join(args.log_dir, "observer_real_host_try%d.log" % i),
                              bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_observer(timeout=90.0):
                host = h
                break
            h.stop()
            time.sleep(2.0)
        check("O1 real host with --host-observer reached 'observer active' and then 'world ready' (no resident bound)", host is not None)
        if host is None:
            return
        L.resolve_host_town("127.0.0.1", args.port)
        # the client's LOCAL gci must be the original, stale one
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        cl = None
        for i in range(3):
            c = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "0"],
                                log_path=os.path.join(args.log_dir, "observer_real_client_try%d.log" % i), bin_dir=L.GAME_BIN_DIR, label="oc").start()
            if c.boot_to_field(timeout=90.0, slot=0):
                cl = c
                break
            c.stop()
            time.sleep(2.0)
        check("O2 real client (resident 0) booted into the field and reached READY (snapshot applied)", cl is not None)
        if cl is None:
            return
        m = cl.wait_for_log(r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=(\d+) kind=FULL", 60.0)
        ct = cl.log_text()
        ht = host.log_text()
        check("O2 the client adopted its host record (MIGRATE -> PUSH_FULL rev 1 -> 'adopted rev=1')", m is not None and m.group(1) == "1"
              and "host asks for a MIGRATE upload" in ct)
        check("O3 client log: host has no avatar (no host puppet)", "[NET][OBSERVER] client: host has no avatar (no host puppet)" in ct)
        check("O3 client log: NO host puppet was created ('[NET][REMOTE] player 8 READY' absent) and no remote player at all (the host is the only peer)",
              "[NET][REMOTE] player 8 READY" not in ct and "[NET][REMOTE] player 8 " not in ct)
        check("O3 host log: peer bound to resident 0 (host-derived) and 'host has no avatar' logged for it",
              re.search(r"peer \d+ bound to resident 0 \(host-derived\)", ht) is not None and re.search(r"\[NET\]\[OBSERVER\] host: peer \d+: host has no avatar", ht) is not None)
        check("O4 the host did not refuse resident 0 as 'the host's own resident'; no identity refusal at all", "host's own resident" not in ht and "REFUSED before READY" not in ht)
        check("O4 no BAD DIGEST / violation / ADOPT_FAILED / refusal line in the client log", not re.search(r"BAD DIGEST|ADOPT_FAILED|violation[^=\n]*[1-9]|REFUSED", ct))
        check("O4 both processes are still alive", host.alive() and cl.alive())
        cl.stop()
    finally:
        for p in ("host", "cl"):
            try:
                locals()[p].stop()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12200)
    ap.add_argument("--log-dir", default=HERE)
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    try:
        run(args, results)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("test aborted by an exception: %s" % e, False, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
