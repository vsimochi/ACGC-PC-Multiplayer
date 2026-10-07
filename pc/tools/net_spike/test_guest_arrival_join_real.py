#!/usr/bin/env python3
"""test_guest_arrival_join_real.py - SHARED train arrival on REAL processes: one REAL host (resident in the field) + up to three REAL guest clients (each a different --guest-profile), no GUI
automation beyond pressing A for Porter's welcome (a normal message that waits for the player, exactly like for a human), all with `-debug`. DISPOSABLE fixture
(pc\\build64\\bin_fixture4_arrjoin, recreated here); the fixture save and save/mp are snapshotted and restored.

A guest that connects while another player is still arriving JOINS that arrival (pc_remote_player.c pc_remote_arrival_join_query <- ac_ride_off_demo_move.c_inc aROD_first_set): its own local
train is moved to the phase of the other passenger instead of running a second independent approach; it stands behind the first passenger. Nothing about the train is sent over the network:
the information is the other player's streamed state + position (the same MOVE state the puppets use). Logged lines:
  "[TRAIN] arrival join: no other arrival: the normal arrival starts ..."          nobody was arriving
  "[TRAIN] arrival join: JOINING the arrival that is at the station ..."          another player was getting off / in Porter's welcome / walking
  "[TRAIN] shared arrival: local train moved to the joined phase (...)"           the local train was started stopped, doors open
  "[TRAIN] local arrival finished: the player stands in the town"                 the ride-off demo ended AND the wait request that restores player control was accepted
  Q1  guest A arrives on an idle town: the normal arrival (its own train approaches: action 2), nobody to join
  Q2  guest B connects while A is at the station (A waits in Porter's welcome until the test presses A, so the overlap is deterministic): B JOINS (its train starts stopped, action 5, and it
      never ran the approach action 2), A's process starts NO second train for B, B stands behind A; then both finish their arrival and are back under control (A and B both stand at the
      scripted walk target (2220, 840): the walk is not blocked by the puppet)
  Q3  guest C connects after A and B are in the town: nobody arriving -> the normal arrival (action 2) and C finishes too
NOT covered here (pure logic only, test_guest_arrival.py): a join while the other passenger is still RIDING (the window is ~9 s of the approach, not reproducible without timing games).
The hourly train (hh:14:50) would make the T4 guard refuse on the other processes, so the test waits out minutes 9..21 of the hour first.
Usage: python test_guest_arrival_join_real.py [--port 9880] [--max-wait 300]"""
import argparse
import datetime
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

NAME = "bin_fixture4_arrjoin"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, NAME)
if __name__ == "__main__":
    T.make_fixture(NAME)
import net_spike_lib as L  # noqa: E402
import game_input as G  # noqa: E402

NORMAL_RX = r"\[TRAIN\] arrival join: no other arrival: the normal arrival starts"
JOIN_RX = r"\[TRAIN\] arrival join: JOINING the arrival that is at the station"
MOVED_RX = r"\[TRAIN\] shared arrival: local train moved to the joined phase \(class 2, train x 2365, action 5"
DONE_RX = r"\[TRAIN\] local arrival finished: the player stands in the town"
STARTED_RX = r"\[TRAIN\] arrival join: (no other arrival|JOINING)"
SECOND_TRAIN_RX = r"\[TRAIN\] remote arrival of player \d+: starting the local arrival train"
PROFILES = {"alpha": ("Alpha", 0x4D31, 24130), "beta": ("Beta", 0x4D32, 24131), "gamma": ("Gamma", 0x4D33, 24132)}


def wait_for_quiet_hour():
    n = datetime.datetime.now()
    s = n.minute * 60 + n.second
    if 9 * 60 <= s <= 21 * 60 + 30:
        wait = 21 * 60 + 30 - s
        L.info("local clock %s: the hourly train (hh:14:50) is near; waiting %d s" % (n.strftime("%H:%M:%S"), wait))
        time.sleep(wait)


class Watch:
    """first wall-clock time each regex appeared in a process log (polled by the test loop). press_ok: while this guest is IN its arrival (between its first 'arrival join' line and its
    'finished' line) the test presses A on its window every few seconds: Porter's welcome is a normal message that waits for the player's A."""

    def __init__(self, proc, rxs):
        self.proc, self.rxs, self.t = proc, rxs, {}
        self.press_ok, self.last_press, self.win = True, 0.0, None

    def poll(self):
        txt = self.proc.log_text()
        now = time.monotonic()
        for k, rx in self.rxs.items():
            if k not in self.t and re.search(rx, txt):
                self.t[k] = now
        if self.press_ok and "arrival" in self.t and "done" not in self.t and now - self.last_press > 2.5:
            self.last_press = now
            try:
                if self.win is None:
                    self.win = G.GameWindow(self.proc.proc.pid)
                if self.win.ready():
                    self.win.press("A", after=0.2)
            except Exception:
                pass
        return self.t


def watch(proc):
    return Watch(proc, {"arrival": STARTED_RX, "normal": NORMAL_RX, "join": JOIN_RX, "moved": MOVED_RX, "done": DONE_RX})


def start_guest(port, key):
    return L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--guest", "--guest-profile", key, "-debug"], log_path=os.path.join(HERE, "guest_arrival_join_%s.log" % key),
                           bin_dir=L.GAME_BIN_DIR, label="gaj" + key).start()


def pump(watches, cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for w in watches:
            w.poll()
        if cond():
            return True
        time.sleep(0.25)
    return False


def train_actions_after(text, rx):
    """the train actions (mTRC [TRAIN] move lines) logged after the first match of rx"""
    m = re.search(rx, text)
    if m is None:
        return None
    return [int(x) for x in re.findall(r"\[TRAIN\] move: action=(\d+) ", text[m.end():])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9880)
    ap.add_argument("--max-wait", type=float, default=300.0)
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    snap = os.path.join(os.environ.get("TEMP", "."), "guest_arrival_join_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    procs = []
    try:
        os.makedirs(os.path.join(save_dir, "mp"), exist_ok=True)
        for key, (nm, pid, lid) in PROFILES.items():
            with open(os.path.join(save_dir, "mp", "guest_%s.ini" % key), "wb") as f:
                f.write(("# test profile\nname = %s\ngender = 1\nface = 5\nhome_town = Hometwn\nplayer_id = 0x%04X\nland_id = %d\n" % (nm, pid, lid)).encode("ascii"))
        wait_for_quiet_hour()
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT), "-debug"], log_path=os.path.join(HERE, "guest_arrival_join_host.log"),
                             bin_dir=L.GAME_BIN_DIR).start()
        procs.append(host)
        check("host listening and in the field", host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT))

        # ---------------- Q1: A arrives on an idle town (normal arrival); it then waits in Porter's welcome for the test's A press
        a = start_guest(args.port, "alpha")
        procs.append(a)
        wa = watch(a)
        wa.press_ok = False
        check("Q1 guest A started the NORMAL arrival (nobody else was arriving)", pump([wa], lambda: "normal" in wa.t, args.max_wait))
        # A's own train approaches (action 2) and stops (5); A gets off and waits in Porter's welcome: a stable 'someone is at the station' state for B to join
        check("Q1 A's own train approached and stopped (actions %s)" % train_actions_after(a.log_text(), NORMAL_RX), pump([wa], lambda: 5 in (train_actions_after(a.log_text(), NORMAL_RX) or []), args.max_wait)
              and 2 in (train_actions_after(a.log_text(), NORMAL_RX) or []))

        # ---------------- Q2: B connects while A is at the station: B joins A's arrival
        time.sleep(12.0)  # A: train stopped -> get off -> Porter's welcome (waits for A); B then connects into 'A is at the station'
        b = start_guest(args.port, "beta")
        procs.append(b)
        wb = watch(b)
        check("Q2 B decided to JOIN A's arrival (it is at the station)", pump([wa, wb], lambda: "join" in wb.t, args.max_wait))
        check("Q2 B's local train was moved to the joined phase: stopped at the station, doors open (action 5)", pump([wa, wb], lambda: "moved" in wb.t, args.max_wait))
        bt = b.log_text()
        acts = train_actions_after(bt, MOVED_RX) or []
        check("Q2 B ran NO approach of its own: after its train was moved to the station it only waits / leaves (actions after that %s: no approach 2, 3, 4)" % sorted(set(acts)),
              not (set(acts) & {2, 3, 4}))
        check("Q2 A's process started NO second train for B (no 'starting the local arrival train' line): %s" % re.findall(SECOND_TRAIN_RX, a.log_text()), re.search(SECOND_TRAIN_RX, a.log_text()) is None)
        wa.press_ok = True  # now A confirms Porter's welcome; B (also at the station) confirms its own
        check("Q2 A's arrival finished (control restored)", pump([wa, wb], lambda: "done" in wa.t, args.max_wait))
        check("Q2 B's arrival finished: it got off beside A and walked to the scripted target although A stands there (control restored)", pump([wa, wb], lambda: "done" in wb.t, args.max_wait))
        b_after = train_actions_after(b.log_text(), MOVED_RX) or []
        check("Q2 B's train waited at the station and left on the vanilla timing (actions after the join %s)" % sorted(set(b_after)), 5 in b_after or 6 in b_after or not b_after)

        # ---------------- Q3: C connects after A and B stand in the town: nobody arriving -> the normal arrival
        c = start_guest(args.port, "gamma")
        procs.append(c)
        wc = watch(c)
        check("Q3 guest C started the NORMAL arrival (nobody was arriving)", pump([wc], lambda: "normal" in wc.t, args.max_wait))
        check("Q3 C never joined anything", "join" not in wc.t)
        check("Q3 C's arrival finished (control restored)", pump([wc], lambda: "done" in wc.t, args.max_wait))
        check("all processes alive", all(p.alive() for p in procs))
    finally:
        for p in reversed(procs):
            try:
                p.stop()
            except Exception:
                pass
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
