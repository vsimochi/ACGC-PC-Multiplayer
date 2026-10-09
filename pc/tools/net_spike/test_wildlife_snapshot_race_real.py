#!/usr/bin/env python3
"""test_wildlife_snapshot_race_real.py - REAL game processes (host, resident A, late-joining resident B; Samsung display, volume 1): B-4, wildlife changes DURING a late joiner's snapshot.

The host's TEST-ONLY hook (env AC_TEST_WLD_SNAPSHOT_RACE=1, pc_net_game.c pcnetgame_test_wld_snapshot_race) fires once, right after the first wildlife snapshot entry of a peer went out:
  - the record of slot 0 (already sent) is removed and announced gone               (race C/D: it despawns while the snapshot is being transmitted)
  - a new record is made (the freed slot 0, BEHIND the table cursor)                (race E: it appears after the walk passed its slot)
  - the record of slot 1 (not yet sent) is removed and announced gone               (it must never reach the peer)
  - a new record is made (slot 1, AHEAD of the cursor)                              (race B: announced AND walked)
Before B joins, two records exist (spawn before the snapshot starts, race A). Required: after B's snapshot B shows exactly the two new records, one actor each, no ghost of the removed ones,
no duplicate, and A (connected all the time) shows the same set.
Usage: python test_wildlife_snapshot_race_real.py [--port 12995]"""
import argparse
import os
import re
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_wildlife_proxy_capacity_real as C  # noqa: E402
import test_wildlife_lifecycle_real as W  # noqa: E402
import net_spike_lib as L  # noqa: E402
import test_wildlife_sim_real as T  # noqa: E402


class Rig(W.Rig):
    def start_host(self):
        env = {"AC_TEST_AUTOPILOT": "ac_auto_host.txt", "AC_TEST_WILDLIFE_REROLL": "80", "AC_TEST_WILDLIFE_TRACE_FRAMES": "60", "AC_TEST_WLD_SNAPSHOT_RACE": "1"}
        self.host = L.HostProcess(port=self.args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=env, log_path=os.path.join(HERE, "wsnap_host.log"), bin_dir=C.W.BIN).start()
        ok = self.host.wait_listening(60) and self.host.boot_to_field(timeout=90, slot=0)
        if ok:
            L.resolve_host_town("127.0.0.1", self.args.port)
            self.players["host"] = C.W.Player(L, "host", self.host)
        return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12995)
    args = ap.parse_args()
    results = []
    R = Rig(args, results)
    check = R.check
    try:
        if not (R.start_host() and R.start_client("A", 1)):
            check("host and client A in the field", False)
            return L.summary_and_exit_code(results)
        # a spot of water: where the host rolled a fish
        ent0, _ = W.fish_in_acre(R)
        check("a fish exists to find water", ent0 is not None)
        m = re.findall(r"spawn decision entity %d kind 0 species \d+ acre\(\d+,\d+\) pos\(([-\d.]+),[-\d.]+,([-\d.]+)\)" % (ent0 or 0), R.log("host"))
        x, z = (float(m[-1][0]), float(m[-1][1])) if m else (2900.0, 1400.0)
        ans = R.players["A"].cmd("catch fish %d" % ent0, 270) if ent0 else None
        check("the starting fish is caught (%s)" % ans, ans is not None and " ok" in ans)
        time.sleep(2)
        H = R.players["host"]
        a1 = H.cmd("hspawn 5 %.0f %.0f" % (x, z), 20)
        a2 = H.cmd("hspawn 6 %.0f %.0f" % (x + 30, z), 20)
        e1 = int(re.search(r"entity (\d+)", a1 or "entity 0").group(1))
        e2 = int(re.search(r"entity (\d+)", a2 or "entity 0").group(1))
        check("two records exist before B joins (%s, %s)" % (e1, e2), e1 != 0 and e2 != 0)
        time.sleep(3)
        eA, _d, poolA = W.pres(R, "A")
        check("A shows both before the race (%s)" % sorted(eA), e1 in eA and e2 in eA)

        # B joins late: the hook fires inside its wildlife snapshot walk
        ok = R.start_client("B", 2)
        check("B joined and is in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        hl = R.log("host")
        mm = re.search(r"\[RACE-TEST\] during a snapshot walk: removed (\d+) \(sent\) and (\d+) \(not yet sent\), recorded (\d+) \(behind the cursor\) and (\d+) \(ahead of it\)", hl)
        check("the race hook fired inside B's snapshot (%s)" % (mm.group(0) if mm else None), mm is not None)
        if not mm:
            return L.summary_and_exit_code(results)
        r0, r1, n0, n1 = (int(g) for g in mm.groups())
        check("the hook made two new records (%d, %d)" % (n0, n1), n0 != 0 and n1 != 0)
        # The host's actor pool holds 2 fish: a third record never gets an actor, so the host releases it after a few failed re-creations (and tells everybody). The oracle is therefore the
        # host's table at the end (decisions - catches - releases - the two records the hook removed), whatever its size.
        time.sleep(10)
        dec = T.decisions(R.log("host"))
        expected = {e for e in set(T.table_now(R)) - {r0, r1} if dec[e][0] == 0}  # fish only (bugs are listed differently by the dump)
        L.info("expected final fish set (host table): %s" % sorted(expected))
        T.enter_acre(R.players["B"], 4, 2)  # B walks to the fish: its actors are no longer culled at once
        time.sleep(4)
        for who in ("B", "A", "host"):
            e, d, pool = W.pres(R, who)
            ids = set(e)
            check("%s's presentation equals the host's table (entries %s, expected %s)" % (who, sorted(ids), sorted(expected)), ids == expected)
            check("%s: none of the removed records is left (%s)" % (who, sorted({r0, r1} & ids)), not ({r0, r1} & ids))
            check("%s: no duplicate actor, pool within its limit (actors %s, pool %d)" % (who, {k: v["actors"] for k, v in e.items()}, pool), all(v["actors"] <= 1 for v in e.values()) and pool <= 2)
            check("%s: nothing is waiting to be shown (deferred %d)" % (who, d), d == 0 or who == "host")
        eb, _db, poolb = W.pres(R, "B")
        check("B shows every record that fits the pool (actors %s)" % {k: v["actors"] for k, v in eb.items()}, sum(v["actors"] for v in eb.values()) == min(len(expected), 2))
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
