#!/usr/bin/env python3
"""test_guest_arrival_latency_real.py - hotfix 0.2.1 (guest arrival hold) on REAL processes over INJECTED LATENCY / LOSS.

0.2.0 field report: remote guests (never the guest on the host's own PC) stayed parked at the train start after joining: no train, no control. On their own client the arrival is held
(ac_ride_off_demo_move.c_inc aROD_first_set -> pc_remote_player.c pc_remote_arrival_join_query) until the host's first world snapshot is applied and every other town player's first
MOVE is in; over real latency that could take very long or never complete (the join-time bulk pushes and snapshot restarts), and the hold had no time limit. On localhost acks come back
within a frame, so it never shows: this test puts a UDP relay with a fixed one-way DELAY (default 150 ms each way, RTT 300 ms) and a random LOSS (default 2 % of the datagrams, both
directions, seeded) between the joining guest and the host.

Scenario (DISPOSABLE fixture pc\\build64\\bin_fixture4_hf_arrlat, recreated; the save is snapshotted and restored):
  host (resident in the field, -debug) + guest A connected DIRECTLY (finishes its arrival: a second player active in town) + guest B through the lossy relay, joining --trials times
  (each trial after the first is a RETURNING guest, like the field reports). Per trial:
    L1  B logs an arrival decision ('[TRAIN] arrival join: ...' or the hotfix's 'hold timed out') within --decide-limit s (default 6) of its link reaching READY
    L2  B's arrival finishes with control released ('[TRAIN] local arrival finished') within --max-wait s
  Diagnostics printed (not asserted): B's hold length, the host's snapshot restarts / deferrals for B, relay packet counts.
The exe under test is --exe (default pc\\build64_w3\\bin\\AnimalCrossing.exe); pass the 0.2.0 exe to show the unfixed behaviour.
--stall (fixed test builds only): B never sees its roster as known (AC_TEST_ARRIVAL_ROSTER_STALL=1): the hold limit itself must release it (L1 + timed-out line).
Usage: python test_guest_arrival_latency_real.py [--stall] [--exe PATH] [--port 19440] [--proxy-port 19441] [--delay-ms 150] [--loss 0.02] [--trials 3]"""
import argparse
import datetime
import heapq
import os
import random
import re
import select
import shutil
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

NAME = "bin_fixture4_hf_arrlat"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, NAME)
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
if __name__ == "__main__":
    T.make_fixture(NAME)
import net_spike_lib as L  # noqa: E402
import game_input as G  # noqa: E402

DEFAULT_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
READY_RX = r"\[NET\] client: handshake complete, town verified \(assigned peer id (\d+)\) -> READY"
DECIDED_RX = r"\[TRAIN\] arrival join: (no other arrival|JOINING|hold timed out)"
HOLD_RX = r"\[TRAIN\] arrival join: the guest's arrival waits for the host's roster"
TIMEOUT_RX = r"\[TRAIN\] arrival join: hold timed out after"
DONE_RX = r"\[TRAIN\] local arrival finished: the player stands in the town"
PROFILES = {"alpha": ("Alpha", 0x4D41, 24140), "beta": ("Beta", 0x4D42, 24141)}


class LagRelay(threading.Thread):
    """UDP relay 127.0.0.1:listen <-> 127.0.0.1:target. Every datagram (both directions) is delayed by `delay` s and dropped with probability `loss`; order is kept."""

    def __init__(self, listen_port, target_port, delay, loss, seed=1234):
        super().__init__(daemon=True)
        self.target = ("127.0.0.1", target_port)
        self.delay, self.loss, self.rng = delay, loss, random.Random(seed)
        self.sock = self._mk()
        self.sock.bind(("127.0.0.1", listen_port))
        self.up = {}  # client addr -> upstream socket
        self.down = {}  # upstream socket -> client addr
        self.q, self.seq = [], 0
        self.stop_ev = threading.Event()
        self.stats = {"fwd": 0, "drop": 0}

    @staticmethod
    def _mk():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            try:
                s.ioctl(socket.SIO_UDP_CONNRESET, False)
            except OSError:
                pass
        s.setblocking(False)
        return s

    def _push(self, sock, data, addr):
        if self.rng.random() < self.loss:
            self.stats["drop"] += 1
            return
        self.seq += 1
        heapq.heappush(self.q, (time.monotonic() + self.delay, self.seq, sock, data, addr))

    def run(self):
        while not self.stop_ev.is_set():
            now = time.monotonic()
            while self.q and self.q[0][0] <= now:
                _, _, s, data, addr = heapq.heappop(self.q)
                try:
                    s.sendto(data, addr)
                    self.stats["fwd"] += 1
                except OSError:
                    pass
            timeout = 0.005 if not self.q else max(0.0, min(0.005, self.q[0][0] - now))
            socks = [self.sock] + list(self.down.keys())
            try:
                rd, _, _ = select.select(socks, [], [], timeout)
            except OSError:
                continue
            for s in rd:
                try:
                    data, addr = s.recvfrom(65536)
                except OSError:
                    continue
                if s is self.sock:
                    u = self.up.get(addr)
                    if u is None:
                        u = self._mk()
                        u.bind(("127.0.0.1", 0))
                        self.up[addr], self.down[u] = u, addr
                    self._push(u, data, self.target)
                else:
                    self._push(self.sock, data, self.down[s])

    def close(self):
        self.stop_ev.set()
        self.join(2.0)
        for s in [self.sock] + list(self.down.keys()):
            s.close()


def wait_for_quiet_hour():
    n = datetime.datetime.now()
    s = n.minute * 60 + n.second
    if 9 * 60 <= s <= 21 * 60 + 30:
        wait = 21 * 60 + 30 - s
        L.info("local clock %s: the hourly train (hh:14:50) is near; waiting %d s" % (n.strftime("%H:%M:%S"), wait))
        time.sleep(wait)


class Watch:
    """first wall-clock time each regex appeared in a process log; presses A on its window while it is in its arrival (Porter's welcome waits for the player's A)"""

    def __init__(self, proc, rxs):
        self.proc, self.rxs, self.t = proc, rxs, {}
        self.last_press, self.win = 0.0, None

    def poll(self):
        txt = self.proc.log_text()
        now = time.monotonic()
        for k, rx in self.rxs.items():
            if k not in self.t and re.search(rx, txt):
                self.t[k] = now
        if "decided" in self.t and "done" not in self.t and now - self.last_press > 2.5:
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
    return Watch(proc, {"ready": READY_RX, "hold": HOLD_RX, "decided": DECIDED_RX, "timeout": TIMEOUT_RX, "done": DONE_RX})


def pump(watches, cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for w in watches:
            w.poll()
        if cond():
            return True
        if any(w.proc.proc.poll() is not None for w in watches):
            for w in watches:
                w.poll()
            return cond()
        time.sleep(0.2)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=DEFAULT_EXE)
    ap.add_argument("--port", type=int, default=19440)
    ap.add_argument("--proxy-port", type=int, default=19441)
    ap.add_argument("--delay-ms", type=float, default=150.0)
    ap.add_argument("--loss", type=float, default=0.02)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--decide-limit", type=float, default=6.0)
    ap.add_argument("--max-wait", type=float, default=150.0)
    ap.add_argument("--tag", default="")
    ap.add_argument("--stall", action="store_true", help="B runs with AC_TEST_ARRIVAL_ROSTER_STALL=1 (test builds): its roster never counts as known, so the hold LIMIT must release it")
    args = ap.parse_args()
    assert 19000 <= args.port <= 19999 and 19000 <= args.proxy_port <= 19999, "ports 19000-19999 only"
    shutil.copy2(args.exe, os.path.join(L.GAME_BIN_DIR, "AnimalCrossing.exe"))
    L.info("exe under test: %s (%d bytes); relay delay %.0f ms each way, loss %.1f %%" % (args.exe, os.path.getsize(args.exe), args.delay_ms, args.loss * 100.0))
    L.require_test_bin_dir()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    snap = os.path.join(os.environ.get("TEMP", "."), "guest_arrival_latency_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    procs, relay = [], None
    logp = lambda k: os.path.join(HERE, "hf_arrlat%s_%s.log" % (("_" + args.tag) if args.tag else "", k))  # noqa: E731
    try:
        os.makedirs(os.path.join(save_dir, "mp"), exist_ok=True)
        for key, (nm, pid, lid) in PROFILES.items():
            with open(os.path.join(save_dir, "mp", "guest_%s.ini" % key), "wb") as f:
                f.write(("# test profile\nname = %s\ngender = 1\nface = 5\nhome_town = Hometwn\nplayer_id = 0x%04X\nland_id = %d\n" % (nm, pid, lid)).encode("ascii"))
        wait_for_quiet_hour()
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT), "-debug"], log_path=logp("host"), bin_dir=L.GAME_BIN_DIR).start()
        procs.append(host)
        check("host listening and in the field", host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT))
        a = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--guest", "--guest-profile", "alpha", "-debug"], log_path=logp("alpha"), bin_dir=L.GAME_BIN_DIR, label="hfa").start()
        procs.append(a)
        wa = watch(a)
        check("guest A (direct link) arrived and stands in the town: a second player active in town", pump([wa], lambda: "done" in wa.t, args.max_wait))

        relay = LagRelay(args.proxy_port, args.port, args.delay_ms / 1000.0, args.loss)
        relay.start()
        for trial in range(1, args.trials + 1):
            b = L.ClientProcess("127.0.0.1:%d" % args.proxy_port, extra_args=["--guest", "--guest-profile", "beta", "-debug"], log_path=logp("beta_t%d" % trial),
                                bin_dir=L.GAME_BIN_DIR, label="hfb%d" % trial,
                                env={"AC_TEST_ARRIVAL_ROSTER_STALL": "1"} if args.stall else None).start()
            procs.append(b)
            wb = watch(b)
            host_off = len(host.log_text())
            got_ready = pump([wb], lambda: "ready" in wb.t, args.max_wait)
            check("T%d B (through the relay) reached READY" % trial, got_ready)
            if not got_ready:
                break
            decided = pump([wb], lambda: "decided" in wb.t, max(args.decide_limit, 30.0) + 5.0)
            dt = (wb.t["decided"] - wb.t["ready"]) if decided else None
            peer = re.search(READY_RX, b.log_text()).group(1)
            check("T%d L1 B decided its arrival within %.0f s of READY (took %s; held=%s, hold timed out=%s)" % (
                trial, args.decide_limit, ("%.1f s" % dt) if dt is not None else "NEVER (> %.0f s)" % (max(args.decide_limit, 30.0) + 5.0), "hold" in wb.t, "timeout" in wb.t),
                decided and dt <= args.decide_limit)
            if args.stall:
                check("T%d L1s the stalled roster was released by the hold LIMIT ([TRAIN] arrival join: hold timed out ...)" % trial, "timeout" in wb.t)
            done = pump([wb], lambda: "done" in wb.t, args.max_wait)
            check("T%d L2 B's arrival finished with control released" % trial, done)
            ht = host.log_text()[host_off:]
            restarts = len(re.findall(r"host: peer %s snapshot epoch \d+ queued" % peer, ht))
            deferred = len(re.findall(r"host: peer %s snapshot epoch \d+ is under way .* deferred" % peer, ht))
            bt = b.log_text()
            L.info("T%d diagnostics: host snapshots queued for peer %s: %d, deferred restarts: %d; B snapshot begins %d, applied %d; relay fwd %d drop %d" % (
                trial, peer, restarts, deferred, len(re.findall(r"client: snapshot epoch \d+ begin", bt)), len(re.findall(r"client: snapshot epoch \d+ applied", bt)),
                relay.stats["fwd"], relay.stats["drop"]))
            check("T%d all processes alive" % trial, all(p.alive() for p in procs))
            b.stop()
            procs.remove(b)
            time.sleep(4.0)
    finally:
        for p in reversed(procs):
            try:
                p.stop()
            except Exception:
                pass
        if relay is not None:
            relay.close()
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
