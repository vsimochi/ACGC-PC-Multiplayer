#!/usr/bin/env python3
"""test_shop_nook_sync_real.py - SHOP KEEPER TARGET SYNC with REAL game processes: Nook follows the NEAREST player of the shop on every game.

TIER: REAL listen HOST (stays in town) + REAL resident client A + REAL resident client B, both warped into Nook's shop (--nook-test warp,resident).
Samsung display, volume 1. No wire: each client's own Nook picks among its own player + the other client's puppet (pc_shop_keeper_target.h).
  1. both read Nook's welcome away (single A presses until no demo runs)
  2. A is parked near Nook (front left), B farther (front right) (AC_TEST_NOOK_SYNC `tpk DX DZ`, TEST-ONLY): on BOTH clients the keeper's target is A and Nook faces A
  3. swap: B near (front right), A farther (front left): on BOTH clients the target switches to B (once, after the dwell) and Nook faces B
  4. hysteresis: A and B about equally far (less than one tile apart): the target does not change for several seconds
Asserts: S1 both in the shop; S2 both clients' target = A and |da(A)| < 30 deg at the end of phase 2; S3 both switched to B and face B; S4 no target change in
phase 4; S5 both clients agree (the same target) in every phase; S6 no protocol violation, everybody alive.
Screenshots: nooksync_A_near_A.png / nooksync_B_near_A.png (phase 2, the view of A / B) and nooksync_A_near_B.png / nooksync_B_near_B.png (phase 3).
Fixture: pc\\build64\\bin_fixture4_nooksync (fresh copy of bin_fixture4 + the exe of pc\\build64_w3). Ports 39500-39999.
Usage: python test_shop_nook_sync_real.py [--port 39720] [--off]   (--off: AC_MP_SHOP_NOOK_SYNC=0 on both clients: the kill switch, vanilla)
"""
import argparse
import os
import re
import shutil
import sys
import time

os.environ["AC_DISPLAY_NAME"] = "samsung"
os.environ["AC_MASTER_VOLUME"] = "1"
os.environ["AC_TEST_HOOKS"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_nooksync"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)
LOG_RE = r"\[NET\]\[NOOKSYNC\]\[TEST-ONLY\] keeper 0x([0-9A-F]{4}) at \(([-\d.]+),([-\d.]+)\) rot=(-?\d+) target=(-?\d+)((?: p\d+:d=\d+,da=-?\d+(?:,talk)?)*)"
DA_MAX = 5461  # 30 degrees in s16 angle units


def mash(c, auto, n, kind, secs):
    with open(auto, "w") as f:
        f.write("%d %s %.2f\n" % (n, kind, secs))
    end = time.monotonic() + secs + 15
    while time.monotonic() < end:
        if re.search(r"\[AUTO\] (?:done %d |mash %d end)" % (n, n), c.log_text()):
            return True
        time.sleep(0.2)
    return False


def frame_dump(c, prefix, name, png):
    bmp = "%s_%s.bmp" % (prefix, name)
    if os.path.isfile(bmp):
        os.remove(bmp)
    with open(prefix + ".req", "w") as f:
        f.write(name + "\n")
    if c.wait_for_log(r"\[TEST-ONLY\] frame dump \d+x\d+ -> .*%s\.bmp" % re.escape("_" + name), 15.0) is None:
        return False
    try:
        from PIL import Image
        time.sleep(0.3)
        Image.open(bmp).save(png)
        return True
    except Exception as e:  # noqa: BLE001
        print("[INFO] frame dump conversion failed: %s" % e)
        return False


def last_state(c):
    """the newest keeper log line: (target, {player: (d, da, talk)}, keeper x, z)"""
    m = list(re.finditer(LOG_RE, c.log_text()))
    if not m:
        return None
    g = m[-1]
    players = {}
    for pid, d, da, talk in re.findall(r" p(\d+):d=(\d+),da=(-?\d+)(,talk)?", g.group(6)):
        players[int(pid)] = (int(d), int(da), bool(talk))
    return int(g.group(5)), players, float(g.group(2)), float(g.group(3))


def targets_since(c, pos):
    return [int(mm.group(5)) for mm in re.finditer(LOG_RE, c.log_text()[pos:])]


def tpk(c, path, n, dx, dz):
    with open(path, "w") as f:
        f.write("%d tpk %.1f %.1f\n" % (n, dx, dz))
    return c.wait_for_log(r"\[NET\]\[NOOKSYNC\]\[TEST-ONLY\] tpk %d done" % n, 15.0) is not None


def read_welcome(c, auto, base):
    for k in range(1, 16):
        mash(c, auto, base + 2 * k, "mash", 0.1)
        m = re.findall(r"\[AUTO\] mash %d end: player main=(\d+) demo=(\d+)" % (base + 2 * k), c.log_text())
        if m and m[-1][1] == "0":
            return True
        mash(c, auto, base + 1 + 2 * k, "mash", 0.35)
        time.sleep(1.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=39720)
    ap.add_argument("--off", action="store_true")
    args = ap.parse_args()
    if not (39500 <= args.port <= 39990):
        print("REFUSING: ports 39500-39999 only", file=sys.stderr)
        return 2
    T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
    import net_spike_lib as L

    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    bin_dir = L.GAME_BIN_DIR
    auto_a, auto_b = os.path.join(bin_dir, "ns_auto_a.txt"), os.path.join(bin_dir, "ns_auto_b.txt")
    tp_a, tp_b = os.path.join(bin_dir, "ns_tp_a.txt"), os.path.join(bin_dir, "ns_tp_b.txt")
    dump_a, dump_b = os.path.join(bin_dir, "ns_dump_a"), os.path.join(bin_dir, "ns_dump_b")
    for p in (auto_a, auto_b, tp_a, tp_b):
        if os.path.isfile(p):
            os.remove(p)
    h = a = b = None
    t0 = time.time()
    tag = "_off" if args.off else ""
    shots = {}
    try:
        h = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0"], log_path=T.log_path("ns_real_host%s.log" % tag), bin_dir=bin_dir,
                          env=dict(os.environ, AC_TEST_CLEAR_ERRANDS="1")).start()
        ok = h.wait_listening(60.0) and h.boot_to_field(timeout=150.0, slot=0)
        ck("listen host reached the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        common = dict(os.environ, AC_TEST_CLEAR_ERRANDS="1", PC_PUPPET_DIAG="1")
        if args.off:
            common["AC_MP_SHOP_NOOK_SYNC"] = "0"
        b = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "2", "--nook-test", "warp,resident"], log_path=T.log_path("ns_real_B%s.log" % tag),
                            bin_dir=bin_dir, label="nsB", env=dict(common, AC_TEST_FRAME_DUMP=dump_b, AC_TEST_AUTOPILOT=auto_b, AC_TEST_NOOK_SYNC=tp_b)).start()
        ok = b.boot_to_field(timeout=180.0, slot=2) and b.wait_for_log(r"--nook-test: warping the guest into the shop", 120.0) is not None
        ck("S1 B booted, connected and warped into Nook's shop", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        time.sleep(6.0)
        ck("S1 B read Nook's welcome away", read_welcome(b, auto_b, 50))
        a = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "--nook-test", "warp,resident"], log_path=T.log_path("ns_real_A%s.log" % tag),
                            bin_dir=bin_dir, label="nsA", env=dict(common, AC_TEST_FRAME_DUMP=dump_a, AC_TEST_AUTOPILOT=auto_a, AC_TEST_NOOK_SYNC=tp_a)).start()
        ok = a.boot_to_field(timeout=180.0, slot=1) and a.wait_for_log(r"--nook-test: warping the guest into the shop", 120.0) is not None
        ck("S1 A booted, connected and warped into Nook's shop", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        time.sleep(6.0)
        ck("S1 A read Nook's welcome away", read_welcome(a, auto_a, 50))
        # the wire ids of A and B (the host announces them in the [NOOKSYNC] lines: "local N")
        time.sleep(3.0)
        sa, sb = last_state(a), last_state(b)
        print("[INFO] A state %s / B state %s" % (sa, sb))
        ids_a = re.findall(r"\[NET\]\[NOOKSYNC\] keeper 0x[0-9A-F]{4} target -?\d+ -> -?\d+ \(.*?local (\d+)\)", a.log_text())
        ids_b = re.findall(r"\[NET\]\[NOOKSYNC\] keeper 0x[0-9A-F]{4} target -?\d+ -> -?\d+ \(.*?local (\d+)\)", b.log_text())
        id_a = int(ids_a[-1]) if ids_a else -1
        id_b = int(ids_b[-1]) if ids_b else -1
        ck("S1 both clients run the keeper target picker (A id %d, B id %d)" % (id_a, id_b), (id_a >= 0 and id_b >= 0 and id_a != id_b) or args.off)
        both = lambda: (last_state(a), last_state(b))  # noqa: E731

        def phase(name, near, far, near_off, far_off, settle):
            n0a, n0b = len(a.log_text()), len(b.log_text())
            ok1 = tpk(a if near == "A" else b, tp_a if near == "A" else tp_b, 1000 + len(shots), near_off[0], near_off[1])
            ok2 = tpk(a if far == "A" else b, tp_a if far == "A" else tp_b, 1100 + len(shots), far_off[0], far_off[1])
            time.sleep(settle)
            return ok1 and ok2, n0a, n0b

        # ---- phase 2: A near, B far
        okp, _p0a, _p0b = phase("near_A", "A", "B", (-50.0, 35.0), (95.0, 55.0), 6.0)
        ck("S2 positioned A near Nook and B far", okp or args.off)  # --off: the TEST-ONLY placement lives in the disabled feature, nothing moves
        (s2a, s2b) = both()
        print("[INFO] phase 2: A sees %s / B sees %s" % (s2a, s2b))
        shots["A_near_A"] = frame_dump(a, dump_a, "nearA", os.path.join(HERE, "nooksync_A_near_A%s.png" % tag))
        shots["B_near_A"] = frame_dump(b, dump_b, "nearA", os.path.join(HERE, "nooksync_B_near_A%s.png" % tag))
        if not args.off:
            ck("S2 A's Nook targets A (%s)" % (s2a and s2a[0]), s2a is not None and s2a[0] == id_a)
            ck("S2 B's Nook targets A too (%s)" % (s2b and s2b[0]), s2b is not None and s2b[0] == id_a)
            ck("S2 both Nooks face A (|da| < 30 deg: A %s / B %s)" % (s2a and s2a[1].get(id_a), s2b and s2b[1].get(id_a)),
               s2a and s2b and id_a in s2a[1] and id_a in s2b[1] and abs(s2a[1][id_a][1]) < DA_MAX and abs(s2b[1][id_a][1]) < DA_MAX)
            ck("S2 ... and NOT toward B (|da(B)| > 30 deg: A %s / B %s)" % (s2a and s2a[1].get(id_b), s2b and s2b[1].get(id_b)),
               s2a and s2b and id_b in s2a[1] and id_b in s2b[1] and abs(s2a[1][id_b][1]) > DA_MAX and abs(s2b[1][id_b][1]) > DA_MAX)
        else:
            ck("OFF: no target picker line on A / B", "[NET][NOOKSYNC]" not in a.log_text() + b.log_text())
        # ---- phase 3: B near, A far
        okp, p3a, p3b = phase("near_B", "B", "A", (50.0, 35.0), (-95.0, 55.0), 6.0)
        ck("S3 positioned B near Nook and A far", okp or args.off)  # --off: the TEST-ONLY placement lives in the disabled feature, nothing moves
        (s3a, s3b) = both()
        print("[INFO] phase 3: A sees %s / B sees %s" % (s3a, s3b))
        shots["A_near_B"] = frame_dump(a, dump_a, "nearB", os.path.join(HERE, "nooksync_A_near_B%s.png" % tag))
        shots["B_near_B"] = frame_dump(b, dump_b, "nearB", os.path.join(HERE, "nooksync_B_near_B%s.png" % tag))
        if not args.off:
            ck("S3 A's Nook switched to B (%s)" % (s3a and s3a[0]), s3a is not None and s3a[0] == id_b)
            ck("S3 B's Nook switched to B (%s)" % (s3b and s3b[0]), s3b is not None and s3b[0] == id_b)
            ck("S3 both Nooks face B (|da| < 30 deg: A %s / B %s)" % (s3a and s3a[1].get(id_b), s3b and s3b[1].get(id_b)),
               s3a and s3b and id_b in s3a[1] and id_b in s3b[1] and abs(s3a[1][id_b][1]) < DA_MAX and abs(s3b[1][id_b][1]) < DA_MAX)
            ck("S3 ... and NOT toward A any more (|da(A)| > 30 deg: A %s / B %s)" % (s3a and s3a[1].get(id_a), s3b and s3b[1].get(id_a)),
               s3a and s3b and id_a in s3a[1] and id_a in s3b[1] and abs(s3a[1][id_a][1]) > DA_MAX and abs(s3b[1][id_a][1]) > DA_MAX)
            sw_a = re.findall(r"\[NET\]\[NOOKSYNC\] keeper 0x[0-9A-F]{4} target (-?\d+) -> (-?\d+) \(nearest", a.log_text()[p3a:])
            sw_b = re.findall(r"\[NET\]\[NOOKSYNC\] keeper 0x[0-9A-F]{4} target (-?\d+) -> (-?\d+) \(nearest", b.log_text()[p3b:])
            ck("S3 exactly one switch A -> B on each client (A %s / B %s)" % (sw_a, sw_b),
               sw_a == [(str(id_a), str(id_b))] and sw_b == [(str(id_a), str(id_b))])
        # ---- phase 4: about equally far (B 10 units nearer than A): no change for 8 s
        okp, p4a, p4b = phase("tie", "B", "A", (-60.0, 60.0), (60.0, 70.0), 8.0)
        ck("S4 positioned A and B about equally far", okp or args.off)  # --off: the TEST-ONLY placement lives in the disabled feature, nothing moves
        if not args.off:
            ta, tb = targets_since(a, p4a), targets_since(b, p4b)
            ck("S4 no flicker: A's target stays %d (%s)" % (id_b, sorted(set(ta))), ta and set(ta) == {id_b})
            ck("S4 no flicker: B's target stays %d (%s)" % (id_b, sorted(set(tb))), tb and set(tb) == {id_b})
        ck("S0 screenshots saved (%s)" % shots, all(shots.values()) or args.off)
        at, bt, ht = a.log_text(), b.log_text(), h.log_text()
        ck("S6 no protocol violation", "protocol violation" not in at + bt + ht)
        ck("S6 host, A and B alive", h.alive() and a.alive() and b.alive())
    finally:
        for p in (auto_a, auto_b, tp_a, tp_b):
            if os.path.isfile(p):
                os.remove(p)
        for p in (a, b, h):
            if p is not None:
                p.stop()
        print("elapsed %.0f s" % (time.time() - t0))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
