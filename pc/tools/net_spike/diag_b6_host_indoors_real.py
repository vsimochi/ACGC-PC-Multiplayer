#!/usr/bin/env python3
"""diag_b6_host_indoors_real.py - WS4 / B-6: what wildlife does while the HOST player is inside a house (REAL host + REAL client; Samsung display, volume 1). A DIAGNOSTIC, not a pass/fail test:
it measures and prints the facts; the checks only assert that the measurement itself worked.

The host walks itself into a villager's house with the TEST-ONLY hook AC_TEST_ROOM_ENTER (the game's own scene change). Before that the host injects a river fish next to itself and the client
stands in the fish's acre. While the host is inside the script records, from the logs of both processes:
  1. does the host keep broadcasting WILDLIFE_STATE ("host: collected N creature state(s)")?
  2. does the client's copy of the fish keep moving (trace positions)?
  3. does a spawn trigger from the client roll anything ("spawn decision" lines)?
  4. is a catch attempt by the client answered, and how ("catch validation failed" / granted)?
and afterwards whether everything resumes when the host is back in the field.
Usage: python diag_b6_host_indoors_real.py [--port 12986]"""
import argparse
import math
import os
import re
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_wildlife_lifecycle_real as LC  # noqa: E402
import test_wildlife_proxy_capacity_real as C  # noqa: E402
import net_spike_lib as L  # noqa: E402

W = C.W
T = LC.T
FX, FZ = 2794.0, 1404.0


class Rig(LC.Rig):
    def start_host(self):
        env = {"AC_TEST_AUTOPILOT": "ac_auto_host.txt", "AC_TEST_WILDLIFE_REROLL": "80", "AC_TEST_WILDLIFE_TRACE_FRAMES": "60", "AC_TEST_PROXY_DIAG": "1",
               "AC_TEST_ROOM_ENTER": "0,%d,-1,-1,%d,-1" % (self.enter_ms, self.stay_ms)}
        self.host = L.HostProcess(port=self.args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=env, log_path=os.path.join(HERE, "b6_host.log"), bin_dir=W.BIN).start()
        ok = self.host.wait_listening(60) and self.host.boot_to_field(timeout=90, slot=0)
        if ok:
            L.resolve_host_town("127.0.0.1", self.args.port)
            self.players["host"] = W.Player(L, "host", self.host)
        return ok


def moved(trace):
    return max((math.dist(p, trace[0]) for p in trace), default=0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12986)
    args = ap.parse_args()
    results = []
    R = Rig(args, results)
    R.enter_ms, R.stay_ms = 150000, 70000
    check = R.check
    info = L.info
    try:
        if not (R.start_host() and R.start_client("A", 1)):
            check("host and client in the field", False)
            return L.summary_and_exit_code(results)
        H, A = R.players["host"], R.players["A"]
        H.cmd("tp 2700 1450", 20)
        time.sleep(2)
        for e in list(T.live_kind(R, 0)):
            H.cmd("hdespawn %d" % e, 20)
        time.sleep(3)
        ans = H.cmd("hspawn 8 %f %f" % (FX, FZ), 30)
        m = re.search(r"entity (\d+)", ans or "")
        ent = int(m.group(1)) if m else 0
        check("the host injected a river fish (entity %d)" % ent, ent > 0)
        T.enter_acre(A, 4, 2)
        time.sleep(8)
        off_a = len(R.log("A"))
        time.sleep(10)
        tr = T.traces(R, "A", {"A": off_a}["A"]).get(ent, [])
        info("before: the client's copy of fish %d: %d samples, moved up to %.0f units in 10 s" % (ent, len(tr), moved(tr)))
        check("measurement: the client sees the fish before the host goes indoors", len(tr) > 3)

        inside = LC.wait_until(lambda: "inside the villager's house" in R.log("host"), 200)
        check("the host is inside a house (its town scene is gone)", bool(inside))
        if not inside:
            return L.summary_and_exit_code(results)
        t_in = time.time()
        time.sleep(5)
        off_h, off_a = len(R.log("host")), len(R.log("A"))
        time.sleep(20)
        hl, al = R.log("host")[off_h:], R.log("A")[off_a:]
        states = re.findall(r"host: collected (\d+) creature state", hl)
        info("indoors (20 s): host STATE broadcasts: %d lines, entity counts %s" % (len(states), sorted(set(states))))
        tr = T.traces(R, "A", off_a).get(ent, [])
        info("indoors (20 s): the client's copy of fish %d: %d samples, moved up to %.0f units" % (ent, len(tr), moved(tr)))

        # 3. a spawn trigger from the client while the host is indoors
        off_h = len(R.log("host"))
        T.cross(A)
        time.sleep(4)
        hl = R.log("host")[off_h:]
        dec = re.findall(r"host: spawn decision entity (\d+)", hl)
        info("indoors: the client crossed an acre boundary (spawn trigger): the host rolled %d new creature(s) %s" % (len(dec), dec))

        # 4. a catch by the client
        off_h, off_a = len(R.log("host")), len(R.log("A"))
        T.enter_acre(A, 4, 2)
        ans = A.cmd("catch fish %d" % ent, 200)
        L.pump_sleep(2) if hasattr(L, "pump_sleep") else None
        hl = R.log("host")[off_h:]
        refused = re.findall(r"catch validation failed", hl)
        granted = re.findall(r"CATCH entity %d .*accepted" % ent, hl)
        info("indoors: the client's catch attempt answered %r; host: %d 'catch validation failed' line(s), %d accepted catch(es)" % (ans, len(refused), len(granted)))

        back = LC.wait_until(lambda: "back in the field" in R.log("host"), 120)
        check("the host came back out into the field (%.0f s after entering)" % (time.time() - t_in), bool(back))
        if back:
            time.sleep(8)
            off_h, off_a = len(R.log("host")), len(R.log("A"))
            time.sleep(15)
            hl = R.log("host")[off_h:]
            states = re.findall(r"host: collected (\d+) creature state", hl)
            info("after the host is back: host STATE broadcasts: %d lines, entity counts %s; the table still holds: %s" % (len(states), sorted(set(states)), sorted(T.table_now(R))))
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
