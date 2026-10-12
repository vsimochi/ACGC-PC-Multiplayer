"""test_ball_sync_real.py - REAL host + REAL client (Samsung display, volume 1): host-authoritative soccer ball / basketball (msgs 79 / 80).

  R1 the client's ac_ball.c becomes a proxy and receives the host's BALL_STATE ('[NET][BALL] client: proxy pos ... host ...')
  R2 the client's autopilot walks into the ball: the host logs 'kick from peer N accepted' (a real local pipe contact -> KICK_REQUEST)
  R3 after the ball settles the client's proxy position equals the host's ball (xz within 60 units)
  R4 exactly one ball actor on the client (ct - dt == 1), and it never fell back to the vanilla local simulation
  R5 the client leaves and rejoins (new process): a new proxy waits, and state arrives again
Diagnostics are PC_NET_TEST_HOOKS-only log lines (every ~2 s). Run: NET_SPIKE_GAME_BIN=<abs bin_fixture4_w2> python test_ball_sync_real.py --port 16220
"""
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
import net_spike_lib as L  # noqa: E402
import wplay_lib as W  # noqa: E402

BIN = os.environ.get("NET_SPIKE_GAME_BIN") or os.path.join(HERE, "..", "..", "build64", "bin_fixture4_w2")
W.BIN = BIN
PROXY_RX = re.compile(r"\[NET\]\[BALL\] client: proxy pos ([-\d.]+) ([-\d.]+) ([-\d.]+) host ([-\d.]+) ([-\d.]+) ([-\d.]+)")
HOST_RX = re.compile(r"\[NET\]\[BALL\] host: ball pos ([-\d.]+) ([-\d.]+) ([-\d.]+)")


def wait_rx(get_text, rx, timeout, since=0):
    end = time.time() + timeout
    while time.time() < end:
        m = list(rx.finditer(get_text()[since:]))
        if m:
            return m[-1]
        time.sleep(0.5)
    return None


CT_RX = re.compile(r"\[NET\]\[BALL\] ball actor ct \(mode (\d+), live (-?\d+), vanilla_spawn (\d)\)")
DT_RX = re.compile(r"\[NET\]\[BALL\] ball actor dt \(mode (\d+), live (-?\d+)\)")
SW_RX = re.compile(r"\[NET\]\[BALL\] ball actor mode (\d+) -> (\d+) \(same actor, live (-?\d+)\)")
FALLBACK = "[NET][BALL] client: no BALL_STATE within 4000 ms (old host?) -> vanilla local ball"


def check_one_ball(check, tag, cl, expect_fallback):
    """one client process: never more than ONE live ball actor (every ct/dt/mode-switch line carries the live count), the vanilla random spawn ran at most once per
    actor, and a VANILLA -> CLIENT flip (ct before the link was READY) reused the same actor"""
    cts, dts, sws = list(CT_RX.finditer(cl)), list(DT_RX.finditer(cl)), list(SW_RX.finditer(cl))
    lives = [int(m.group(2)) for m in cts] + [int(m.group(2)) for m in dts] + [int(m.group(3)) for m in sws]
    check("%s ball actor lines: ct %s | switches %s | dt %s" % (tag, [m.group(0)[24:] for m in cts], [m.group(0)[24:] for m in sws], [m.group(0)[24:] for m in dts]), len(cts) >= 1)
    check("%s never more than one live ball actor in the client process (live counts %s)" % (tag, lives), lives and max(lives) <= 1 and min(lives) >= 0)
    check("%s vanilla spawn ran at most once per actor (%d spawns, %d actors)" % (tag, sum(int(m.group(3)) for m in cts), len(cts)), sum(int(m.group(3)) for m in cts) <= len(cts))
    n_fb = cl.count(FALLBACK)
    if expect_fallback:
        check("%s the fallback line appears exactly once (%d)" % (tag, n_fb), n_fb == 1)
    else:
        check("%s proxy mode reached, no vanilla fallback (%d fallback lines)" % (tag, n_fb), n_fb == 0 and "proxy ball waiting" in cl)


def run_oldhost(args, check):
    """F: the NEW real client joins the OLD v0.1.1 host (no ids 79/80): after 4 s without BALL_STATE it uses the vanilla local ball (and logs it once)"""
    host = client = None
    try:
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0"], env={"AC_TEST_HOOKS": "0"},
                             log_path=os.path.join(HERE, "ballreal_oldhost.log"), bin_dir=args.oldhost).start()
        ok = host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)
        check("F old v0.1.1 host in the field", ok)
        if not ok:
            return
        client = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"], env={"AC_TEST_HOOKS": "1"},
                                 log_path=os.path.join(HERE, "ballreal_A_old.log"), bin_dir=BIN, label="ballAold").start()
        ok = client.boot_to_field(timeout=180, slot=1)
        check("F new client in the old host's field", ok)
        if not ok:
            return
        end = time.time() + 20
        while time.time() < end and FALLBACK not in client.log_text():
            time.sleep(0.5)
        time.sleep(5)  # the vanilla ball keeps running: no further fallback / proxy lines
        cl = client.log_text()
        check_one_ball(check, "F", cl, expect_fallback=True)
        check("F no BALL_STATE ever arrived (no proxy pos line) -> the ball is a normal vanilla ball", PROXY_RX.search(cl) is None)
    finally:
        if client is not None:
            client.stop()
        if host is not None:
            host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16220)
    ap.add_argument("--oldhost", default=None, help="abs path of the OLD v0.1.1 fixture (bin_fixture_rv_d_old): run only the fallback scenario F")
    args = ap.parse_args()
    if args.oldhost:
        results = []
        run_oldhost(args, lambda d, c: L.check(d, bool(c), results))
        return L.summary_and_exit_code(results)
    if not os.path.basename(os.path.normpath(BIN)).startswith("bin_fixture4_"):
        print("REFUSING: disposable bin_fixture4_<name> only", file=sys.stderr)
        return 2
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = client = None
    try:
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env={"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_host.txt"},
                             log_path=os.path.join(HERE, "ballreal_host.log"), bin_dir=BIN).start()
        ok = host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)

        def start_client(n):
            c = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"], env={"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_A.txt"},
                                log_path=os.path.join(HERE, "ballreal_A_%d.log" % n), bin_dir=BIN, label="ballA%d" % n).start()
            return c, c.boot_to_field(timeout=180, slot=1)

        client, ok = start_client(1)
        check("client A in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        m = wait_rx(client.log_text, PROXY_RX, 30)
        check("R1 the client proxy is driven by the host's BALL_STATE (%s)" % (m.group(0) if m else None), m is not None)
        if m is None:
            return L.summary_and_exit_code(results)
        hx, hz = float(m.group(4)), float(m.group(6))
        A = W.Player(L, "A", client)
        L.info("tp answer: %s" % A.cmd("tp %.1f %.1f" % (hx - 40.0, hz), 20))  # park 40 units west (terrain between the spawn and the ball can block a long walk)
        time.sleep(1.0)
        off = len(host.log_text())
        ans = A.cmd("walk %.1f %.1f 4" % (hx + 30.0, hz), 30)  # THROUGH the ball: still moving at the contact
        L.info("walk answer: %s" % ans)
        acc = wait_rx(host.log_text, re.compile(r"\[NET\]\[BALL\] host: kick from peer \d+ accepted[^\r\n]*"), 10, off)
        check("R2 a real walk into the proxy ball produced a host-accepted kick (%s)" % (acc.group(0) if acc else None), acc is not None)
        time.sleep(8)  # let the ball settle (the host's diagnostic ball line comes only every ~2 s: judge the movement after this)
        moved = [x for x in HOST_RX.finditer(host.log_text()[off:])]
        check("R2 the host ball moved after the kick (%s)" % ([(x.group(1), x.group(3)) for x in moved[-3:]]),
              any(math.hypot(float(x.group(1)) - hx, float(x.group(3)) - hz) > 5 for x in moved))
        coff = len(client.log_text())
        hoff = len(host.log_text())
        cm = wait_rx(client.log_text, PROXY_RX, 6, coff)
        hm = wait_rx(host.log_text, HOST_RX, 6, hoff)
        if cm and hm:
            d_host = math.hypot(float(cm.group(1)) - float(hm.group(1)), float(cm.group(3)) - float(hm.group(3)))
            d_state = math.hypot(float(cm.group(1)) - float(cm.group(4)), float(cm.group(3)) - float(cm.group(6)))
        else:
            d_host = d_state = 1e9
        check("R3 proxy converges with the host ball: proxy-host %.1f, proxy-state %.1f (<60) [%s | %s]" % (d_host, d_state, cm.group(0) if cm else None, hm.group(0) if hm else None),
              d_host < 60 and d_state < 60)
        check_one_ball(check, "R4", client.log_text(), expect_fallback=False)
        # R5 leave + rejoin
        hoff = len(host.log_text())
        client.stop()
        client = None
        dis = wait_rx(host.log_text, re.compile(r"host: peer \d+ disconnected"), 20, hoff)
        check("R5 host saw the client leave", dis is not None)
        client, ok = start_client(2)
        check("R5 client rejoined the field", ok)
        if ok:
            m2 = wait_rx(client.log_text, PROXY_RX, 30)
            cl = client.log_text()
            check("R5 after the rejoin a new proxy waited and state arrived again (%s)" % (m2.group(0) if m2 else None), m2 is not None and "proxy ball waiting" in cl)
            check_one_ball(check, "R5", cl, expect_fallback=False)
    finally:
        if client is not None:
            client.stop()
        if host is not None:
            rc = host.stop()
            check("host did not crash (%s)" % rc, rc is None)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
