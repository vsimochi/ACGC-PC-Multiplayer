#!/usr/bin/env python3
"""diag_gyroid_dup_real.py - gyroid (save-gyroid prop actor, ac_haniwa.c) duplicates: a DIAGNOSTIC with a REAL host and a REAL client (Samsung display, volume 1).

The test-only diagnostics AC_TEST_GYROID_DIAG=1 print [NET][GYROID] lines for every spawn request of ac_birth_control.c (with the field tile found at the position), every Haniwa actor ct / dt, and how
many Haniwa actors are alive at that moment (total and with the same prop id). This script lets the client walk a tour of acres (and optionally enter a villager's house and come back, which unloads
and reloads the town scene), then summarises per process: spawn requests, ct, dt, the highest number of live gyroids with the same prop id, and the positions. More than one live gyroid with the same
prop id = a duplicate.
Usage: python diag_gyroid_dup_real.py [--port 12980] [--house]"""
import argparse
import collections
import os
import re
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
os.environ["AC_TEST_GYROID_DIAG"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_wildlife_lifecycle_real as LC  # noqa: E402
import net_spike_lib as L  # noqa: E402

GY = re.compile(r"\[NET\]\[GYROID\] (.*)")


def summarise(name, log):
    """prints every [NET][GYROID] line of one process and the counts; returns (lines, highest live_gyroids_same_prop seen at a ct)"""
    lines = [m.group(1) for m in GY.finditer(log)]
    kinds = collections.Counter(l.split(" ")[0] for l in lines)
    peak = 0
    for l in lines:
        m = re.match(r"ct .*live_gyroids_same_prop=(\d+)", l)
        if m:
            peak = max(peak, int(m.group(1)))
    L.info("%s: %s; highest live count of one prop id at a creation: %d (1 = no duplicate)" % (name, dict(kinds), peak))
    for l in lines:
        L.info("   %s %s" % (name, l))
    return lines, peak


class SystemExit_Marker(Exception):
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12980)
    ap.add_argument("--hostrestart", action="store_true", help="the host process is stopped and started again while the client keeps running (an in-process reconnect over live gyroids)")
    ap.add_argument("--house", action="store_true", help="the client enters a villager's house and comes back (town scene unload / reload)")
    args = ap.parse_args()
    results = []
    if args.house:
        import test_wildlife_indoor_spawn_real as IR
        R = IR.Rig(args, results)
        R.enter_ms, R.stay_ms = 15000, 25000
    else:
        R = LC.Rig(args, results)
    t0 = time.time()
    try:
        if not (R.start_host() and R.start_client("A", 1)):
            R.check("host and client in the field", False)
            return L.summary_and_exit_code(results)
        A = R.players["A"]
        if args.hostrestart:
            time.sleep(25)
            R.host.stop()
            time.sleep(8)
            L.info("host restarted: %s" % R.start_host())
            LC.wait_until(lambda: "READY" in R.log("A")[-4000:] or False, 5)
            time.sleep(75)
            raise SystemExit_Marker()
        if args.house:
            LC.wait_until(lambda: "back in the field" in R.log("A"), 150)
            time.sleep(12)
            raise SystemExit_Marker()
        time.sleep(10)
        H = R.players["host"]
        A.walk(2240, 1640, timeout=60, radius=120)  # the four house gyroids stand around (2060..2420, 1500..1780)
        time.sleep(5)
        # a field update for the gyroids' acre arrives at the client while its gyroid actors are alive
        for k in range(3):
            L.info("host edits the acre (%s)" % H.cmd("place 2806 %d %d" % (50 + k, 40), 20))
            time.sleep(6)
        A.walk(3700, 1640, timeout=60, radius=120)   # away: acres further than the actor window
        time.sleep(5)
        A.walk(2240, 1640, timeout=60, radius=120)   # and back: the gyroids are spawned again
        time.sleep(8)
        for k in range(2):
            L.info("host edits the acre again (%s)" % H.cmd("place 2806 %d %d" % (53 + k, 41), 20))
            time.sleep(6)
        time.sleep(5)
    except SystemExit_Marker:
        pass
    finally:
        hl, al = R.log("host"), R.log("A")
        R.stop_all()
    hrows, hpeak = summarise("host", hl)
    arows, apeak = summarise("client A", al)
    R.check("diagnostic output was produced", bool(hrows or arows))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
