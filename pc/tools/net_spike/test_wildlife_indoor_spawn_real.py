#!/usr/bin/env python3
"""test_wildlife_indoor_spawn_real.py - WS2-A: wildlife that is announced while a CLIENT is inside a house (REAL host + REAL client process; Samsung display, volume 1).

The client walks itself into a villager's house with the TEST-ONLY hook AC_TEST_ROOM_ENTER (the game's own scene change; the process has no town scene while it is inside) and comes back out
by itself. While it is inside the host injects fish (`hspawn`), and the client's wildlife presentation must (a) not lose the announcement, (b) not present anything in the interior, (c) show
the fish once it is back in the field, (d) forget a deferred fish the host despawns meanwhile, (e) forget deferred fish across a session reset.
Evidence: the client's autopilot `pres` dump ([AUTO] pres deferred N fishpool M, one line per entry) and its log lines.
Usage: python test_wildlife_indoor_spawn_real.py [--port 12988]"""
import argparse
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
FX, FZ = 2794.0, 1404.0  # a river spot in acre (4,2), within the host player's 700-unit attendance reach


class Rig(LC.Rig):
    def start_client(self, label, slot):
        self.n += 1
        env = {"AC_TEST_AUTOPILOT": "ac_auto_%s.txt" % label, "AC_TEST_WILDLIFE_TRACE_FRAMES": "60", "AC_TEST_HOOKS": "1",
               "AC_TEST_ROOM_ENTER": "0,%d,-1,-1,%d,-1" % (self.enter_ms, self.stay_ms)}
        c = L.ClientProcess("127.0.0.1:%d" % self.args.port, extra_args=["--bootstrap-resident", str(slot), "-debug"], env=env,
                            log_path=os.path.join(HERE, "indoor_%s_%d.log" % (label, self.n)), bin_dir=W.BIN, label="in" + label).start()
        ok = c.boot_to_field(timeout=180, slot=slot)
        self.check("client %s in the field" % label, ok)
        if ok:
            self.clients[label] = c
            self.players[label] = W.Player(L, label, c)
            self.t_field = time.time()
        else:
            c.stop()
        return ok


def hspawn(R, species=8):
    ans = R.players["host"].cmd("hspawn %d %f %f" % (species, FX, FZ), 30)
    m = re.search(r"entity (\d+)", ans or "")
    return int(m.group(1)) if m and int(m.group(1)) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12988)
    args = ap.parse_args()
    results = []
    R = Rig(args, results)
    R.enter_ms, R.stay_ms = 15000, 80000
    check = R.check
    try:
        if not (R.start_host() and R.start_client("A", 1)):
            check("host and client in the field", False)
            return L.summary_and_exit_code(results)
        inside = LC.wait_until(lambda: "inside the villager's house" in R.log("A"), 120)
        check("the client is inside a house (its town scene is gone)", bool(inside))
        if not inside:
            return L.summary_and_exit_code(results)
        t_in = time.time()
        time.sleep(2)

        def alog():
            return R.log("A")

        # NOTE: the client's autopilot is dormant while it is indoors (its commands run once it is back in the field), so everything indoors is driven from the HOST and observed in the client's log.
        # the host's own player must be near the fish: the host's fish actors are culled by distance to ITS player, and a record nobody is near is (rightly) released by the host
        R.players["host"].cmd("tp 2700 1450", 20)
        time.sleep(2)
        # the actor pool holds at most 2 fish: release any naturally rolled fish so the scenario below stays within it
        for e in list(LC.T.live_kind(R, 0)):
            R.players["host"].cmd("hdespawn %d" % e, 20)
        time.sleep(4)
        e1 = hspawn(R)
        e2 = hspawn(R)
        check("the host announced two fish (entities %s, %s) while the client is indoors" % (e1, e2), e1 is not None and e2 is not None)
        time.sleep(3)
        for e in (e1, e2):
            check("(a) indoors: the client received the announcement of entity %s" % e, ("client: SPAWN entity %d " % e) in alog())
            check("(a) indoors: ... and kept it (deferred) instead of presenting it", re.search(r"entity %d dropped -- local scene not ready" % e, alog()) is not None
                  and re.search(r"entity %d kind 0 species \d+ materialized" % e, alog()) is None)
        # (d): the host despawns e2 while it is still deferred on the client
        ans = R.players["host"].cmd("hdespawn %d" % e2, 20)
        check("the host released entity %s (%s)" % (e2, ans), ans is not None and "released" in ans)
        time.sleep(3)
        e3 = hspawn(R)
        check("a third fish (entity %s) is announced indoors" % e3, e3 is not None)
        time.sleep(3)
        check("(b) indoors: nothing was presented (no 'materialized' line yet)", re.search(r"presentation: entity \d+ kind 0 species \d+ materialized", alog()) is None)
        back = LC.wait_until(lambda: "back in the field" in alog(), 150)
        check("the client came back out into the field (%.0f s after entering)" % (time.time() - t_in), bool(back))
        if back:
            def present():
                ents, d, pool = LC.pres(R, "A")
                return ents, d, pool
            LC.wait_until(lambda: e1 in present()[0] and e3 in present()[0], 40)
            ents, d, pool = present()
            L.info("client presentation after the return: %s deferred %d fish actors %d" % ({k: (v["has_actor"], v["alive"], v["actors"]) for k, v in ents.items()}, d, pool))
            for e in (e1, e3):
                v = ents.get(e)
                check("(c) back outdoors entity %s materialized exactly once (actor, alive, one stamped actor)" % e, v is not None and v["has_actor"] == 1 and v["alive"] == 1 and v["actors"] == 1)
            check("(d) entity %s (despawned by the host while deferred) never appears" % e2, e2 not in ents and re.search(r"presentation: entity %d kind 0 species \d+ materialized" % e2, alog()) is None)
            check("(c) nothing is left deferred (%d) and the pool holds exactly the two fish (%d)" % (d, pool), d == 0 and pool == 2)
        print("seconds from entering the house until the end of the checks: %.0f" % (time.time() - t_in))
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
