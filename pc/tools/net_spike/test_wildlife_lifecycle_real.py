#!/usr/bin/env python3
"""test_wildlife_lifecycle_real.py - REAL game processes (one host, one resident client; Samsung display, volume 1): the wildlife lifecycle fixes.

  L1  B-2   a valid fish whose local actor was culled (killfish = vanilla's own deferred-destroy flag) comes back from the host's state, ONE actor, its entry never dropped
  L2  B-2   after the fish is caught the entries are gone on the client (freed once the host announced it gone and the actor is gone)
  L3  B-7   a host fish actor that is destroyed again and again is released (despawned everywhere) instead of staying attended without an actor; the acre rolls a new fish afterwards
  L4  B-9   a session reset on the client retires the stamped fish actor (no anonymous vanilla fish is left), and re-entering the acre brings back exactly ONE
  L5  B-3   a fish announced while the actor pool is full is deferred, shown once a slot frees (no duplicate, pool never exceeded); a deferred fish the host then despawns is forgotten
  L6  B-11  fish of the KASEKI program (sea bass, red snapper, ...: a different actor program than the common fish) are tied to the remote angler's bobber by the host, bite through the
            host's decision, and are caught through the host's catch transaction like any other fish (hspawn puts one in the authoritative table)

The autopilot hooks used (TEST-ONLY, AC_TEST_HOOKS=1): killfish ENT, resetstamps, pspawn ENT SPECIES X Z, pdespawn ENT, pres (entries, actor counts, deferred count, live fish actors).
Usage: python test_wildlife_lifecycle_real.py [--port 12990] [--only l1,l2,l3,l4,l5]"""
import argparse
import os
import re
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_wildlife_proxy_capacity_real as C  # noqa: E402  (fresh fixture, Rig)
import net_spike_lib as L  # noqa: E402
import test_wildlife_sim_real as T  # noqa: E402

PRES_RX = re.compile(r"\[AUTO\] pres entity (\d+) kind (\d+) species (\d+) has_actor=(\d) alive=(\d) despawned=(\d) actors=(\d)")
DEF_RX = re.compile(r"\[AUTO\] pres deferred (\d+) fishpool (\d+)")


def pres(R, who):
    """({entity: dict}, deferred, fishpool) of the process `who`"""
    P = R.players[who]
    off = len(R.log(who))
    cid = P.send("pres")
    P.wait(cid, 20)
    txt = R.log(who)[off:]
    ents = {int(m.group(1)): dict(kind=int(m.group(2)), species=int(m.group(3)), has_actor=int(m.group(4)), alive=int(m.group(5)), despawned=int(m.group(6)), actors=int(m.group(7))) for m in PRES_RX.finditer(txt)}
    d = DEF_RX.search(txt)
    return ents, (int(d.group(1)) if d else -1), (int(d.group(2)) if d else -1)


def wait_until(fn, timeout, step=0.6):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


class Rig(C.Rig):
    def start_host(self):
        env = {"AC_TEST_AUTOPILOT": "ac_auto_host.txt", "AC_TEST_WILDLIFE_REROLL": "80", "AC_TEST_WILDLIFE_TRACE_FRAMES": "60", "AC_TEST_PROXY_DIAG": "1"}
        self.host = L.HostProcess(port=self.args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=env, log_path=os.path.join(HERE, "wlife_host.log"), bin_dir=C.W.BIN).start()
        ok = self.host.wait_listening(60) and self.host.boot_to_field(timeout=90, slot=0)
        if ok:
            L.resolve_host_town("127.0.0.1", self.args.port)
            self.players["host"] = C.W.Player(L, "host", self.host)
        return ok


def fish_in_acre(R, who="A"):
    ent = T.ensure_creature(R, who, 0)
    if ent is None:
        return None, None
    d = T.decisions(R.log("host")).get(ent)
    bx, bz = (d[2], d[3]) if d else (4, 2)
    T.enter_acre(R.players[who], bx, bz)
    time.sleep(3)
    return ent, (bx, bz)


def l1_l2(R):
    check = R.check
    ent, _ = fish_in_acre(R)
    check("L1 a live fish exists and is shown on the client", ent is not None)
    if ent is None:
        return None
    e, _d, pool = pres(R, "A")
    check("L1 the client has exactly one actor for it (%s)" % e.get(ent), ent in e and e[ent]["actors"] == 1)
    ans = R.players["A"].cmd("killfish %d" % ent, 20)
    check("L1 the client's actor was destroyed the way a cull does it (%s)" % ans, ans is not None and "flagged" in ans)
    seen_entry = []

    def back():
        e2, _d2, _p2 = pres(R, "A")
        seen_entry.append(ent in e2)
        return ent in e2 and e2[ent]["actors"] == 1 and e2[ent]["alive"] == 1

    ok = wait_until(back, 12)
    check("L1 the entry never disappeared while the creature was valid (entry seen in %d/%d polls)" % (sum(seen_entry), len(seen_entry)), all(seen_entry) and bool(seen_entry))
    check("L1 the actor was re-created from the host's state, exactly one", bool(ok))
    e3, _d3, _p3 = pres(R, "A")
    check("L1 still exactly one actor afterwards (%s)" % e3.get(ent), ent in e3 and e3[ent]["actors"] == 1)
    # L2: catch it; the entries must go
    ans = R.players["A"].cmd("catch fish %d" % ent, 270)
    check("L2 the client caught it (%s)" % ans, ans is not None and " ok" in ans)
    time.sleep(3)
    ea, _da, _pa = pres(R, "A")
    eh, _dh, _ph = pres(R, "host")
    check("L2 the entry is gone on the client (%s)" % ea.get(ent), ent not in ea)
    check("L2 the entry is gone on the host (%s)" % eh.get(ent), ent not in eh)
    return ent


def l3(R):
    check = R.check
    ent, _ = fish_in_acre(R)
    check("L3 a live fish exists", ent is not None)
    if ent is None:
        return
    off = len(R.log("host"))
    H = R.players["host"]
    # the host's actor of it is destroyed again right after every re-creation (faster than the once-a-second check can see it alive)
    for _ in range(40):
        H.cmd("killfish %d" % ent, 8)
        if re.search(r"entity %d \(kind 0 species \d+ acre \d+,\d+\) released -- its host actor could not be kept alive" % ent, R.log("host")[off:]):
            break
        time.sleep(0.15)
    hl = R.log("host")[off:]
    rec = len(re.findall(r"entity %d \(kind 0\) is attended but has no host actor -- re-creating" % ent, hl))
    rel = re.search(r"entity %d \(kind 0 species \d+ acre \d+,\d+\) released -- its host actor could not be kept alive" % ent, hl)
    check("L3 re-creations were attempted (%d) and then the record was released instead of staying attended without an actor" % rec, rel is not None and 1 <= rec <= 6)
    ea = wait_until(lambda: ent not in pres(R, "A")[0], 10)
    check("L3 the client was told (its entry is gone)", bool(ea))
    # the acre can roll a new fish again
    before = set(T.decisions(R.log("host")))
    nxt = None
    for _ in range(10):
        T.cross(R.players["A"])
        time.sleep(2)
        new = [e for e in T.decisions(R.log("host")) if e not in before and T.decisions(R.log("host"))[e][0] == 0]
        if new:
            nxt = new[-1]
            break
    check("L3 a new fish is rolled for the acre afterwards (entity %s)" % nxt, nxt is not None)
    if nxt is not None:
        d = T.decisions(R.log("host")).get(nxt)
        T.enter_acre(R.players["A"], d[2], d[3])
        time.sleep(2)
        ans = R.players["A"].cmd("catch fish %d" % nxt, 270)
        check("L3 ... and it can be fished (%s)" % ans, ans is not None and " ok" in ans)


def l4(R):
    check = R.check
    ent, _ = fish_in_acre(R)
    check("L4 a live fish exists", ent is not None)
    if ent is None:
        return
    e, _d, pool = pres(R, "A")
    check("L4 before the reset the client shows it (pool %d)" % pool, ent in e and pool >= 1)
    R.players["A"].cmd("resetstamps", 20)
    gone = wait_until(lambda: pres(R, "A")[2] == 0, 8)
    e2, _d2, pool2 = pres(R, "A")
    check("L4 the reset retired the fish actor (no anonymous vanilla fish left: pool %d, entries %s)" % (pool2, list(e2)), bool(gone) and not e2)
    # the client walks back into the acre: the host replays it -> exactly one actor, stamped
    ent_again = None
    for _ in range(6):
        T.cross(R.players["A"])
        T.enter_acre(R.players["A"], *fish_acre_of(R, ent))
        time.sleep(3)
        e3, _d3, pool3 = pres(R, "A")
        if ent in e3 and e3[ent]["actors"] >= 1:
            ent_again = (e3[ent], pool3)
            break
    check("L4 re-entering the acre brings back exactly one actor for it (%s)" % (ent_again,), ent_again is not None and ent_again[0]["actors"] == 1 and ent_again[1] == 1)


def fish_acre_of(R, ent):
    d = T.decisions(R.log("host")).get(ent)
    return (d[2], d[3]) if d else (4, 2)


def l5(R):
    check = R.check
    ent, _ = fish_in_acre(R)
    check("L5 a live real fish exists (one pool slot used)", ent is not None)
    A = R.players["A"]
    x, z = 2900.0, 1400.0
    a1 = A.cmd("pspawn 9001 5 %.0f %.0f" % (x, z), 20)
    e, d, pool = pres(R, "A")
    check("L5 the second fish fits (pool %d, deferred %d)" % (pool, d), pool == 2 and d == 0)
    a2 = A.cmd("pspawn 9002 6 %.0f %.0f" % (x + 40, z), 20)
    e, d, pool = pres(R, "A")
    check("L5 a third fish does not fit: deferred, pool unchanged (pool %d, deferred %d, answer %s)" % (pool, d, a2), pool == 2 and d == 1 and 9002 not in {k for k, v in e.items() if v["actors"]})
    A.cmd("killfish 9001", 20)
    shown = wait_until(lambda: (lambda r: 9002 in r[0] and r[0][9002]["actors"] == 1 and r[1] == 0)(pres(R, "A")), 10)
    e, d, pool = pres(R, "A")
    check("L5 once a slot freed the deferred fish was shown, one actor, pool still 2 (pool %d, deferred %d, %s)" % (pool, d, e.get(9002)), bool(shown) and pool == 2)
    A.cmd("pspawn 9003 7 %.0f %.0f" % (x - 40, z), 20)
    e, d, pool = pres(R, "A")
    check("L5 another one is deferred (deferred %d)" % d, d == 1)
    A.cmd("pdespawn 9003", 20)
    e, d, pool = pres(R, "A")
    check("L5 a deferred fish the host then despawns is forgotten (deferred %d)" % d, d == 0 and 9003 not in e)
    A.cmd("pdespawn 9002", 20)


def l6(R):
    check = R.check
    # kaseki-program fish are OCEAN fish (they flee the shallows of the river: placed in a pond they are in the escape state for good), so they are put in the sea off the south-west beach
    x, z = 1000.0, 4296.0
    R.players["A"].cmd("tp 960 4100", 20)  # the client is near the sea: a creature far from every player is culled at once (and the host releases its record)
    time.sleep(3)
    for name in ("seabass", "snapper"):  # (a whale swims too far from the beach for the autopilot to find a casting spot)
        a = R.players["host"].cmd("hspawn %s %.0f %.0f" % (name, x, z), 20)
        mm = re.search(r"entity (\d+)", a or "")
        ent = int(mm.group(1)) if mm else 0
        check("L6 %s: the host recorded an authoritative fish (%s)" % (name, a), ent != 0)
        if not ent:
            continue
        shown = wait_until(lambda: ent in pres(R, "A")[0] and pres(R, "A")[0][ent]["actors"] == 1, 10)
        e, _d, _p = pres(R, "A")
        check("L6 %s: the client shows it, one actor (%s)" % (name, e.get(ent)), bool(shown))
        off = len(R.log("host"))
        ans = R.players["A"].cmd("catch fish %d" % ent, 270)
        hl = R.log("host")[off:]
        ev = sorted({int(g) for g in re.findall(r"bobber event (\d) entity %d " % ent, hl)})
        acc = re.findall(r"peer \d+ CATCH entity %d .*accepted" % ent, hl)
        check("L6 %s: the host's AI tied it to the angler's bobber (events %s), the client hooked it and the host accepted the catch once (%s | %s)" % (name, ev, ans, len(acc)),
              ans is not None and " ok" in ans and 1 in ev and len(acc) == 1)
        time.sleep(3)
        e2, _d2, _p2 = pres(R, "A")
        check("L6 %s: it is gone from the client (%s)" % (name, e2.get(ent)), ent not in e2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12990)
    ap.add_argument("--only", default="l1,l3,l4,l5,l6")
    args = ap.parse_args()
    results = []
    R = Rig(args, results)
    only = args.only.split(",")
    try:
        if not (R.start_host() and R.start_client("A", 1)):
            R.check("host and client in the field", False)
            return L.summary_and_exit_code(results)
        if "l1" in only or "l2" in only:
            l1_l2(R)
        if "l3" in only:
            l3(R)
        if "l4" in only:
            l4(R)
        if "l5" in only:
            l5(R)
        if "l6" in only:
            l6(R)
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
