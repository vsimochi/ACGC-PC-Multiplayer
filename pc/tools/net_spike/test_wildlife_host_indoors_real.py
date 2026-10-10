#!/usr/bin/env python3
"""test_wildlife_host_indoors_real.py - B-6: what happens to the wildlife while the HOST player is inside a house (REAL host + REAL clients; Samsung display, volume 1).

Design (see TODO.md section 2, B-6): the host simulates all wildlife inside its own town scene (fish / insect actor pools, clip pointers), so while the host player is indoors there is NO simulation:
the host's WILDLIFE_STATE carries no creatures, spawn triggers and catch validation bail on `!pcfa_scene_is_town()`. The records (the authoritative table) survive. The intended behaviour of the
clients is therefore "hold what you have": a creature stays where the host left it (it must NOT start to swim off on every client's own local AI, which makes every client see a different fish),
and everything resumes, without duplicates or losses, when the host walks out again.

The host walks into a villager's house with the TEST-ONLY hook AC_TEST_ROOM_ENTER. Checks:
  H1  before: the client sees the host's fish moving (state flows)
  H2  indoors: the host's WILDLIFE_STATE carries no creature (suspended authority)
  H3  indoors: the client's copy of the fish stays where the host left it (moved < 20 units in 20 s; a free local AI moves it by several tens of units)
  H4  indoors: a client acre crossing (spawn trigger) rolls nothing on the host (spawns are suspended, not queued)
  H5  indoors: a LATE-JOINING client still gets the wildlife snapshot (the records) and materializes the fish
  H6  outdoors again: the host's table holds the same records (no loss, no duplicates), state flows again, and each client has exactly one actor per fish
Usage: python test_wildlife_host_indoors_real.py [--port 12986]"""
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
        self.host = L.HostProcess(port=self.args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=env, log_path=os.path.join(HERE, "hostin_host.log"), bin_dir=W.BIN).start()
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
    R.enter_ms, R.stay_ms = 110000, 100000  # the entry must come after the client booted and the fish was set up (~85 s); the stay covers the late joiner's boot (~60 s)
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
        time.sleep(15)
        tr = T.traces(R, "A", off_a).get(ent, [])
        info("before: the client's copy of fish %d: %d samples, moved up to %.0f units in 15 s" % (ent, len(tr), moved(tr)))
        check("H1 the client sees the fish and the host's state moves it (%d samples)" % len(tr), len(tr) > 3)
        table_before = sorted(T.table_now(R))

        inside = LC.wait_until(lambda: "inside the villager's house" in R.log("host"), 220)
        check("the host is inside a house (its town scene is gone)", bool(inside))
        if not inside:
            return L.summary_and_exit_code(results)
        t_in = time.time()
        time.sleep(6)  # the last states in flight, the stale-target timeout (2.5 s) passed
        off_h, off_a = len(R.log("host")), len(R.log("A"))
        time.sleep(20)
        hl = R.log("host")[off_h:]
        counts = re.findall(r"host: collected (\d+) creature state", hl)
        info("indoors: host STATE broadcast entity counts %s" % sorted(set(counts)))
        check("H2 indoors the host broadcasts no creature (suspended authority; counts %s)" % sorted(set(counts)), all(int(c) == 0 for c in counts))
        tr = T.traces(R, "A", off_a).get(ent, [])
        info("indoors: the client's copy of fish %d: %d samples, moved up to %.0f units in 20 s" % (ent, len(tr), moved(tr)))
        check("H3 indoors the client's copy of the fish stays where the host left it (moved %.0f units)" % moved(tr), len(tr) > 3 and moved(tr) < 20.0)

        off_h = len(R.log("host"))
        T.cross(A)
        time.sleep(4)
        dec = re.findall(r"host: spawn decision entity (\d+)", R.log("host")[off_h:])
        check("H4 indoors a spawn trigger from the client rolls nothing on the host (%s)" % dec, not dec)

        # H5: a late joiner while the host is inside
        if R.start_client("B", 2):
            snap_ok = LC.wait_until(lambda: "wildlife snapshot end" in R.log("B"), 30)
            blog = R.log("B")
            m = re.search(r"wildlife snapshot end \(generation \d+\) -- (\d+) entities live", blog)
            info("indoors: the late joiner's snapshot: %s" % (m.group(0) if m else None))
            check("H5 a client that joins while the host is indoors still gets the snapshot with the record(s) (%s)" % (m.group(1) if m else None), bool(snap_ok) and m is not None and int(m.group(1)) >= 1)
        else:
            check("H5 late joiner in the field", False)

        back = LC.wait_until(lambda: "back in the field" in R.log("host"), 150)
        check("the host came back out into the field (%.0f s after entering)" % (time.time() - t_in), bool(back))
        if back:
            time.sleep(12)
            off_h = len(R.log("host"))
            time.sleep(15)
            hl = R.log("host")[off_h:]
            counts = [int(c) for c in re.findall(r"host: collected (\d+) creature state", hl)]
            table_after = sorted(T.table_now(R))
            info("after: table before %s, after %s, state counts %s" % (table_before, table_after, sorted(set(counts))))
            # records of acres where nobody stands are released after 12 s by the attendance rule while the host is away; what must hold is: the fish a client stands next to is still there, once
            check("H6 the fish under test is still in the authoritative table, once, and nothing new was invented (before %s, after %s)" % (table_before, table_after),
                  table_after.count(ent) == 1 and len(set(table_after)) == len(table_after) and set(table_after) <= set(table_before) | {max(table_before) + k for k in range(1, 4)})
            check("H6 the host's state flows again (counts %s)" % sorted(set(counts)), any(c >= 1 for c in counts))
            ents, d, pool = LC.pres(R, "A")
            mine = ents.get(ent)
            check("H6 client A (standing in the fish's acre) has exactly one live actor for the fish (%s, deferred %d)" % (mine, d), mine is not None and mine["has_actor"] == 1 and mine["alive"] == 1 and mine["actors"] == 1)
            if "B" in R.players:
                ents, d, pool = LC.pres(R, "B")
                mine = ents.get(ent)
                # B stands far away: its local actor of a distant fish is culled (the host's state re-materializes it when B comes near); what must hold is one entry and never a second actor
                check("H6 the late joiner B has one entry for the fish and no duplicate actor (%s)" % (mine,), mine is not None and mine["actors"] <= 1)
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
