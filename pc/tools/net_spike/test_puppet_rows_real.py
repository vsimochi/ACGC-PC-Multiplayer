"""test_puppet_rows_real.py - REAL host + REAL client A (actor) + REAL observer client B (Samsung display, volume 1): P1 puppet animation
coverage seen from another process (PC_PUPPET_DIAG=1 on B).

Every action is a REAL game action of A driven by the TEST-ONLY autopilot (AC_TEST_AUTOPILOT); `tp` / `place` are positioning aids.
  R1 take-out / put-in: A equips an umbrella, a balloon, a pinwheel and the net through the game's own D-pad tool cycle (equip):
     B's puppet of A shows the take-out row (72, row=takeout_item) / put-in row (73, row=putin_item) and the held item classes
     umbrella / balloon / pinwheel / net (`held item kind=K model=...`)
  R2 pitfall: the host places a buried pitfall (BURIED_PITFALL_HOLE00 0x2A) on a tile next to A, A walks onto it: B sees ready_pitfall (92),
     fall_pitfall (93) and struggle_pitfall (94) rows, A mashes A until it climbs out: climbup_pitfall (95); the ct_base states log their
     root pin (mask 3 = TRANS_XZ|TRANS_Y) and the puppet root (= its synced position) is within 40 units of A's own position
  R3 nobody crashed
Not covered here (no autopilot path): sitting / pushing furniture (interior furniture), the train / boat demos, the shrine and the
lighthouse, a real bee sting (the swell flag is PROTOCOL-tested in test_puppet_held_item.py --only s4).
Run: python test_puppet_rows_real.py --port 22310 [--fixture bin_fixture4_p1_rows] [--exe <abs build64_w3 exe>]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
import town_test_support as T  # noqa: E402
import make_four_resident_fixture as MF  # noqa: E402
import wplay_lib as W  # noqa: E402

ITM_NET, ITM_UMBRELLA00, ITM_RED_BALLOON, ITM_YELLOW_PINWHEEL = 0x2200, 0x2204, 0x2244, 0x224C
BURIED_PITFALL_HOLE00 = 0x002A
DEFAULT_EXE = os.path.abspath(os.path.join(HERE, "..", "..", "build64_w3", "bin", "AnimalCrossing.exe"))


def make_fixture(name, exe):
    """fresh disposable copy; the given exe; every resident gets a net, an umbrella, a balloon and a pinwheel (pockets 2..5)"""
    assert name.startswith("bin_fixture4_p1_"), name
    for _ in range(15):
        try:
            T.make_fixture(name)
            break
        except PermissionError:
            time.sleep(1.0)
    dst = os.path.join(T.BUILD64, name)
    shutil.copy2(exe, os.path.join(dst, "AnimalCrossing.exe"))
    p = os.path.join(dst, MF.GCI_REL)
    g = bytearray(open(p, "rb").read())
    for slot in range(4):
        base = MF.MAIN_OFF + MF.PRIV_OFF + slot * MF.PRIV_STRIDE
        pk = list(struct.unpack(">15H", g[base + 0x68:base + 0x68 + 30]))
        pk[2], pk[3], pk[4], pk[5] = ITM_NET, ITM_UMBRELLA00, ITM_RED_BALLOON, ITM_YELLOW_PINWHEEL
        g[base + 0x68:base + 0x68 + 30] = struct.pack(">15H", *pk)
    m = bytearray(g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE])
    m[MF.CHK_OFF:MF.CHK_OFF + 2] = struct.pack(">H", MF.checksum(bytes(m)))
    g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE] = m
    g[MF.BACK_OFF:MF.BACK_OFF + MF.SAVE_SECTOR_SIZE] = g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_SECTOR_SIZE]
    open(p, "wb").write(bytes(g))
    return dst


def wait_rx(get_text, rx, timeout, since=0):
    end = time.time() + timeout
    while time.time() < end:
        m = re.search(rx, get_text()[since:])
        if m:
            return m
        time.sleep(0.3)
    return None


def ppos(p):
    a = p.cmd("pos", 20) or ""
    m = re.search(r"x=([-\d.]+) z=([-\d.]+)", a)
    return (float(m.group(1)), 0.0, float(m.group(2))) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=22310)
    ap.add_argument("--fixture", default="bin_fixture4_p1_rows")
    ap.add_argument("--exe", default=DEFAULT_EXE)
    args = ap.parse_args()
    if not (22000 <= args.port <= 22999):
        print("REFUSING: ports 22000-22999 only", file=sys.stderr)
        return 2
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731

    BIN = make_fixture(args.fixture, args.exe)
    W.BIN = BIN
    procs = []
    try:
        env_common = {"AC_TEST_HOOKS": "1"}
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=dict(env_common, AC_TEST_AUTOPILOT="ac_auto_H.txt"),
                             log_path=os.path.join(HERE, "p1rows_host.log"), bin_dir=BIN).start()
        procs.append(host)
        ok = host.wait_listening(60) and host.boot_to_field(timeout=120, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        a = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"], env=dict(env_common, AC_TEST_AUTOPILOT="ac_auto_A.txt"),
                            log_path=os.path.join(HERE, "p1rows_A.log"), bin_dir=BIN, label="p1A").start()
        procs.append(a)
        check("client A in the field", a.boot_to_field(timeout=200, slot=1))
        b = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "2", "-debug"],
                            env=dict(env_common, AC_TEST_AUTOPILOT="ac_auto_B.txt", PC_PUPPET_DIAG="1"),
                            log_path=os.path.join(HERE, "p1rows_B.log"), bin_dir=BIN, label="p1B").start()
        procs.append(b)
        check("observer client B in the field", b.boot_to_field(timeout=200, slot=2))
        H, A, B = W.Player(L, "H", host), W.Player(L, "A", a), W.Player(L, "B", b)
        hp = ppos(H)
        check("host position %s" % (hp,), hp is not None)
        if hp is None:
            return L.summary_and_exit_code(results)
        X, _, Z = hp
        AX, AZ = X + 40.0, Z
        A.cmd("tp %.1f %.1f" % (AX, AZ), 20)
        B.cmd("tp %.1f %.1f" % (X + 100.0, Z + 40.0), 20)
        time.sleep(6.0)

        # ---- R1 take-out / put-in + held classes
        aid = None
        for item, model in ((ITM_UMBRELLA00, "umbrella"), (ITM_RED_BALLOON, "balloon"), (ITM_YELLOW_PINWHEEL, "pinwheel"), (ITM_NET, "net")):
            bo = len(b.log_text())
            ans = A.cmd("equip %x" % item, 60)
            check("R1 A equips 0x%04X (%s)" % (item, ans), ans is not None and " ok" in ans)
            m_t = wait_rx(b.log_text, r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) state main_index=72 [^\r\n]*row=takeout_item[^\r\n]*", 10, bo)
            check("R1 B sees A's take-out row for %s (%s)" % (model, m_t.group(0) if m_t else None), m_t)
            if m_t and aid is None:
                aid = m_t.group(1)
            pid = aid or r"\d+"
            m_h = wait_rx(b.log_text, r"\[NET\]\[PUPPET\]\[DIAG\] player %s held item kind=\d+ model=%s [^\r\n]*" % (pid, model), 10, bo)
            check("R1 B's puppet of A carries the %s (%s)" % (model, m_h.group(0) if m_h else None), m_h)
            if item != ITM_UMBRELLA00:
                # the D-pad cycle swaps tools directly (TAKEOUT only, observed): PUTIN_ITEM is not reached by this path; its row is
                # exercised by test_move_action_state_wire.py --only s3 (every index) -- informational here
                m_p = re.search(r"\[NET\]\[PUPPET\]\[DIAG\] player %s state main_index=73 [^\r\n]*row=putin_item" % pid, b.log_text()[bo:])
                L.info("R1 switching to %s: put-in row on B: %s" % (model, m_p.group(0) if m_p else None))
            time.sleep(2.0)
        A.cmd("equip %x" % ITM_NET, 30)

        # ---- R2 pitfall
        ap_ = ppos(A)
        check("R2 A position %s" % (ap_,), ap_ is not None)
        if ap_ is not None:
            ux, uz = int(ap_[0] // 40), int(ap_[2] // 40)
            tx, tz = ux + 2, uz
            ans = H.cmd("place %x %d %d" % (BURIED_PITFALL_HOLE00, tx, tz), 30)
            check("R2 the host placed a buried pitfall at ut(%d,%d) (%s)" % (tx, tz, ans), ans is not None and "placed" in ans and "NOT" not in ans)
            time.sleep(3.0)
            bo = len(b.log_text())
            A.send("walk %.1f %.1f" % ((tx + 0.5) * 40.0, (tz + 0.5) * 40.0))
            pid = aid or r"\d+"
            names = {}
            for idx, nm in ((92, "ready_pitfall"), (93, "fall_pitfall"), (94, "struggle_pitfall")):
                m = wait_rx(b.log_text, r"\[NET\]\[PUPPET\]\[DIAG\] player %s state main_index=%d [^\r\n]*row=%s" % (pid, idx, nm), 25, bo)
                names[idx] = m
                check("R2 B sees A's %s row (%d) (%s)" % (nm, idx, m.group(0) if m else None), m)
            pins = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player %s root pin row=(\w+) mask=(\d+) base_z=(-?\d+) pos=\(([-\d.]+),([-\d.]+),([-\d.]+)\)" % pid,
                              b.log_text()[bo:])
            got = {r[0]: int(r[1]) for r in pins}
            check("R2 the pitfall ct_base rows pinned TRANS_XZ|TRANS_Y (mask 3) on B (%s)" % got,
                  got.get("ready_pitfall") == 3 and got.get("fall_pitfall") == 3)
            ap2 = ppos(A)
            if pins and ap2 is not None:
                px, pz = float(pins[-1][3]), float(pins[-1][5])
                d = ((px - ap2[0]) ** 2 + (pz - ap2[2]) ** 2) ** 0.5
                check("R2 the pinned puppet root (its synced position %.1f,%.1f) is at A's own position (%.1f,%.1f): %.1f <= 40 units" %
                      (px, pz, ap2[0], ap2[2], d), d <= 40.0)
            bo2 = len(b.log_text())
            A.cmd("mash 14", 40)
            m = wait_rx(b.log_text, r"\[NET\]\[PUPPET\]\[DIAG\] player %s state main_index=95 [^\r\n]*row=climbup_pitfall" % pid, 25, bo2)
            check("R2 A mashed its way out: B sees the climbup_pitfall row (95) (%s)" % (m.group(0) if m else None), m)
            mp = wait_rx(b.log_text, r"\[NET\]\[PUPPET\]\[DIAG\] player %s root pin row=climbup_pitfall mask=3 " % pid, 10, bo2)
            check("R2 climbup_pitfall pinned with mask 3 on B", mp)
            m_sw = re.search(r"player %s cosmetic effect=pitfall_sweat\w* [^\r\n]*" % pid, b.log_text()[bo:])
            L.info("R2 sweat effect on B: %s" % (m_sw.group(0) if m_sw else None))
    finally:
        for p in reversed(procs):
            rc = p.stop()
            check("%s did not crash (%s)" % (getattr(p, "label", "host"), rc), rc is None)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
