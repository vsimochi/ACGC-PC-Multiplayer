#!/usr/bin/env python3
"""test_fishing_view_real.py - worker P3, REAL game processes (a host and two resident clients A, B; Samsung display, volume 1; run through p3_run.py).

The players only play (the TEST-ONLY autopilot drives the real input: `catch fish ENT`, `catch bug ENT`); B (and the host) OBSERVE the angler's puppet with
PC_PUPPET_DIAG=1, so their logs carry the [NET][PUPPET][DIAG] lines of the P3 FISHING VIEW block (pc_remote_player.c).

  F1  A fishes a host-simulated fish with the real rod; B stands in the same acre:
        B receives BOBBER_VIEW for A ([NET][FISHVIEW] client lines) and its puppet of A DRAWS the bobber (bobber drawn), hides it after the reel-in;
        the host accepts ONE catch and B receives PLAYER_ACTION CATCH for A with the fish item; B's puppet of A shows it (catch shown, NOTICE_ROD 56)
  B1  A nets a bug with the real net, B in the same acre: CATCH received, catch shown in NOTICE_NET (47)
  F3  the HOST fishes: A / B see the host's own bobber (origin = the host id) and its catch
  W   the host's wildlife outcome is unchanged: exactly one accepted catch per creature, only the catcher gets an item (the wildlife checks of
      test_wildlife_sim_real.catch_fish / net_catch run unchanged)
Usage: python p3_run.py fvreal test_fishing_view_real.py [--port 24300] [--only f1,b1,f3]"""
import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
os.environ["PC_PUPPET_DIAG"] = "1"  # copied into every launched game process
import wplay_lib as W  # noqa: E402
import test_wildlife_sim_real as T  # noqa: E402  (Session, catch_fish, net_catch, enter_acre)
import net_spike_lib as L  # noqa: E402


class Session(T.Session):
    """test_wildlife_sim_real.Session with this test's own log files (p3_logs/), so a parallel wildlife run never shares them"""

    def start_host(self):
        env = {"AC_TEST_AUTOPILOT": "ac_auto_host.txt", "AC_TEST_WILDLIFE_REROLL": "80", "AC_TEST_WILDLIFE_TRACE_FRAMES": "60"}
        self.host = L.HostProcess(port=self.args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=env,
                                  log_path=os.path.join(LOGS, "p3_fvreal_host.log"), bin_dir=W.BIN).start()
        ok = self.host.wait_listening(60) and self.host.boot_to_field(timeout=90, slot=0)
        self.check("host in the field", ok)
        if ok:
            L.resolve_host_town("127.0.0.1", self.args.port)
            self.players["host"] = W.Player(L, "host", self.host)
        return ok

    def start_client(self, label, slot):
        self.n += 1
        env = {"AC_TEST_AUTOPILOT": "ac_auto_%s.txt" % label, "AC_TEST_WILDLIFE_TRACE_FRAMES": "60"}
        c = L.ClientProcess("127.0.0.1:%d" % self.args.port, extra_args=["--bootstrap-resident", str(slot), "-debug"], env=env,
                            log_path=os.path.join(LOGS, "p3_fvreal_%s_%d.log" % (label, self.n)), bin_dir=W.BIN, label="p3" + label).start()
        ok = c.boot_to_field(timeout=180, slot=slot)
        self.check("client %s in the field" % label, ok)
        if ok:
            self.clients[label] = c
            self.players[label] = W.Player(L, label, c)
        else:
            c.stop()
        return ok


LOGS = os.path.join(HERE, "p3_logs")


def since(S, names):
    return {w: len(S.log(w)) for w in names}


def seg(S, who, off):
    return S.log(who)[off[who]:]


def observe_checks(S, tag, angler, observers, off, item_lo, item_hi, notice_state):
    check = S.check
    for o in observers:
        lg = seg(S, o, off)
        if o != "host":
            rx = re.findall(r"\[NET\]\[FISHVIEW\] client: BOBBER_VIEW origin=(\d+) active=1 shown=1", lg)
            check("%s %s received BOBBER_VIEW for %s and shows it (origins %s)" % (tag, o, angler, sorted(set(rx))), bool(rx))
        drawn = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) bobber drawn", lg)
        check("%s %s's puppet of %s DREW the bobber (%d diag lines)" % (tag, o, angler, len(drawn)), bool(drawn))
        check("%s %s's puppet hid the bobber after the reel-in" % (tag, o), re.search(r"\[NET\]\[PUPPET\]\[DIAG\] player \d+ bobber hidden", lg) is not None)
        cat = [int(m.group(2), 16) for m in re.finditer(r"PLAYER_ACTION CATCH origin=(\d+) item=0x([0-9A-F]+)", lg)]
        check("%s %s received PLAYER_ACTION CATCH with a %s item (%s)" % (tag, o, "fish" if notice_state == 56 else "bug", [hex(c) for c in cat]),
              any(item_lo <= c <= item_hi for c in cat))
        shown = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player \d+ catch shown item=0x([0-9A-F]+) state=(\d+)", lg)
        check("%s %s's puppet SHOWED the caught creature in state %d (%s)" % (tag, o, notice_state, shown),
              any(int(s[1]) == notice_state and item_lo <= int(s[0], 16) <= item_hi for s in shown))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=24300)
    ap.add_argument("--only", default="f1,b1,f3")
    args = ap.parse_args()
    results = []
    W.make_fixture()
    os.makedirs(LOGS, exist_ok=True)
    S = Session(args, results)
    only = args.only.split(",")
    try:
        if not (S.start_host() and S.start_client("A", 1) and S.start_client("B", 2)):
            return L.summary_and_exit_code(results)
        names = ["host", "A", "B"]
        if "f1" in only:
            print("=" * 72 + "\nF1 A fishes, B observes")
            off = since(S, names)
            ent = T.catch_fish(S, "A", ["B"], "F1")
            time.sleep(2)
            if ent is not None:
                observe_checks(S, "F1", "A", ["B"], off, 0x2300, 0x2327 + 0x40, 56)
        if "b1" in only:
            print("=" * 72 + "\nB1 A nets a bug, B observes")
            S.players["B"].cmd("mash 8", 20)
            T.enter_acre(S.players["B"], 4, 2)
            off = since(S, names)
            e = None
            for attempt in range(4):  # test_wildlife_sim_real.net_catch returns False (not None) when a round found no catchable bug: retry with fresh rolls
                e = T.net_catch(S, "A", "B1")
                if e:
                    break
                L.info("B1: no bug caught in attempt %d, re-rolling" % (attempt + 1))
                T.cross(S.players["A"])
                time.sleep(3)
            S.check("B1 A caught a bug with the real net (entity %s)" % e, bool(e))
            time.sleep(2)
            if e:
                lg = seg(S, "B", off)
                cat = [int(m.group(1), 16) for m in re.finditer(r"PLAYER_ACTION CATCH origin=\d+ item=0x([0-9A-F]+)", lg)]
                S.check("B1 B received PLAYER_ACTION CATCH with an insect item (%s)" % [hex(c) for c in cat], any(0x2D00 <= c <= 0x2D2B for c in cat))
                shown = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player \d+ catch shown item=0x([0-9A-F]+) state=(\d+)", lg)
                S.check("B1 B's puppet of A SHOWED the insect in NOTICE_NET 47 (%s)" % shown, any(int(s[1]) == 47 for s in shown))
                S.check("B1 B's puppet of A played the notice_net row (row=notice_net)", re.search(r"state main_index=47 counter=\d+ row=notice_net", lg) is not None)
            time.sleep(5)
            S.check("B1 B's process is still running after showing the insect (regression: an insect ground-item draw crashed it)",
                    "B" in S.clients and S.clients["B"].proc.poll() is None)
        if "f3" in only:
            print("=" * 72 + "\nF3 the host fishes, A and B observe")
            off = since(S, names)
            ent = T.catch_fish(S, "host", ["A", "B"], "F3")
            time.sleep(2)
            if ent is not None:
                observe_checks(S, "F3", "host", ["B"], off, 0x2300, 0x2327 + 0x40, 56)
        if "f1" in only or "f3" in only:
            S.check("W the host log has BOBBER_VIEW relay lines", "[NET][FISHVIEW] host: BOBBER_VIEW origin=" in S.log("host"))
    finally:
        S.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
