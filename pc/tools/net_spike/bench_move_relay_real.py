#!/usr/bin/env python3
"""bench_move_relay_real.py - WS4: baseline measurement of the host's player-MOVE relay (pcnetgame_handle_host_move) in a REAL host process (Samsung display, volume 1).

K scripted peers (FakeClient, protocol level) each send MOVE at 20 Hz for --seconds; the host runs with AC_TEST_MOVE_PROFILE=1 and prints, every 5 s, moves handled, destination iterations, relayed /
thinned counts and where the time went: the whole handler, the pc_net_send calls (the actual transmission) and the rest (interest views, tier decision, message copy = the lookup work).
  near   all peers stand in the same acre            -> every sample is relayed to everybody (worst case for sends)
  spread peers stand in different acres, far apart   -> distant receivers are thinned (interest management)
Only as many peers as the fixture has non-host residents can connect as a FakeClient (3 in the 4-resident wplay fixture); larger counts are covered by the native model benchmark bench_move_relay_model.c.
Usage: python bench_move_relay_real.py [--peers 2,4,7] [--seconds 25] [--port 12982]"""
import argparse
import os
import re
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wplay_lib as W  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = W.BIN
W.make_fixture()  # before net_spike_lib is imported: its default FakeClient identities are read from this copy's save
import net_spike_lib as L  # noqa: E402,F811
PROF = re.compile(r"\[NET\]\[PROFILE\] host MOVE relay: ([\d.]+) s, (\d+) moves \(([\d.]+)/s\), (\d+) destination iterations \(([\d.]+) per move\), (\d+) relayed, (\d+) thinned; "
                  r"handler ([\d.]+) us/move total = pc_net_send ([\d.]+) us/move \(([\d.]+) us/send\) \+ lookup/tier/copy ([\d.]+) us/move \(([\d.]+) us/iteration\); CPU share of one core ([\d.]+)%")


def run(port, k, spread, seconds):
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "-debug"], env={"AC_TEST_MOVE_PROFILE": "1"}, bin_dir=W.BIN,
                         log_path=os.path.join(HERE, "bench_move_host_%d_%s.log" % (k, "spread" if spread else "near"))).start()
    clients = []
    try:
        if not (host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)):
            return None
        L.resolve_host_town("127.0.0.1", port)
        for i in range(k):
            c = L.FakeClient("B%d" % i, "127.0.0.1", port)
            c.connect_and_ready()
            clients.append(c)
        off = len(host.log_text())
        t_end = time.time() + seconds
        frame = 0
        nxt = time.time()
        while time.time() < t_end:
            for i, c in enumerate(clients):
                x = 1000.0 + (i * 700.0 if spread else 40.0 * i)
                z = 1500.0 + (i * 400.0 if spread else 0.0)
                c.claim_position(x + (frame % 40), 160.0, z)
            frame += 1
            nxt += 0.05  # 20 Hz
            L.pump_sleep(max(0.0, nxt - time.time()))
        txt = host.log_text()[off:]
        return [tuple(float(v) for v in m.groups()) for m in PROF.finditer(txt)]
    finally:
        for c in clients:
            try:
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--peers", default="1,2,3")
    ap.add_argument("--seconds", type=int, default=25)
    ap.add_argument("--port", type=int, default=12982)
    args = ap.parse_args()
    print("%-7s %-6s %9s %9s %10s %9s %11s %11s %12s %9s" % ("layout", "peers", "moves/s", "iter/mv", "relayed", "thinned", "us/move", "send us/mv", "lookup us/mv", "us/iter"))
    for spread in (False, True):
        for k in [int(x) for x in args.peers.split(",")]:
            rows = run(args.port, k, spread, args.seconds)
            if not rows:
                print("%-7s %-6d (no profile line: the host or a peer did not start)" % ("spread" if spread else "near", k))
                continue
            # skip the first window (peers still connecting) when there is more than one
            r = rows[-1] if len(rows) == 1 else rows[len(rows) // 2]
            print("%-7s %-6d %9.0f %9.1f %10d %9d %11.2f %11.2f %12.2f %9.3f   (CPU share of a core %.2f%%, %d window(s))" % ("spread" if spread else "near", k, r[2], r[4], r[5], r[6], r[7], r[8], r[10], r[11], r[12], len(rows)))


if __name__ == "__main__":
    sys.exit(main())
