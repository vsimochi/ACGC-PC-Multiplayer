"""test_pvp_real.py - REAL host + REAL client A + REAL observer client B (Samsung display, volume 1): player-vs-player tool hits (ids 91 / 92).

Every swing is a REAL A press through the TEST-ONLY autopilot (AC_TEST_AUTOPILOT); `tp` / `face` are positioning aids only.
  P1 A (client) swings the AXE at the host: A sends PVP_HIT_REQ, the host accepts it, the HOST's own player enters TUMBLE (victim-is-host
     path) and slides (knockback measured 2 s later); A's swing is a plain AIR_AXE: no REFLECT_AXE, A's axe wear (Common axe_damage) unchanged
  P2 the host swings the AXE at A (host-local attacker path): PVP_HIT reaches A only, A enters TUMBLE
  P3 A swings the SHOVEL at the host: the swing is an AIR_SCOOP (no hole under a player), the host enters SHOCK
  P4 A swings the NET at the host: A's net stops (STOP_NET), the host enters SHOCK
  P5 the host is paused (autopilot `pause 1`, the real Esc path) while A hits it: the host accepts, but the paused victim never reacts
     (the hit is dropped when its TTL runs out)
  P6 the observer B sees the victims' puppet rows (PC_PUPPET_DIAG): main_index 11 row=tumble and main_index 99 row=shock
  P7 wallet / pockets+equipment digest / axe wear unchanged on host and A (reactions only); no process crashed
Optional --s0: before P1, the same A axe swing with the TEST-ONLY AC_TEST_PVP_OFF=1 (feature off, vanilla) shows the pre-change behaviour
(REFLECT_AXE and +3 axe wear) -- the S0 verification.
Run: python test_pvp_real.py --port 17310 [--fixture bin_fixture4_pvp_real] [--exe <abs build64_w3 exe>]
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

ITM_NET, ITM_AXE, ITM_SHOVEL = 0x2200, 0x2201, 0x2202
DEFAULT_EXE = os.path.abspath(os.path.join(HERE, "..", "..", "build64_w3", "bin", "AnimalCrossing.exe"))
STAT_RX = re.compile(r"wallet=(\d+) equip=0x([0-9A-F]+) axe_damage=(-?\d+) pockets=([0-9A-F]+) main=(-?\d+) sent=(\d+) accepted=(\d+) rejected=(\d+) applied=(\d+) dropped=(\d+)")


def make_fixture(name, exe):
    """fresh disposable copy; the given exe; every resident gets an axe (pocket 2), a shovel (pocket 3) and a net (pocket 4)"""
    assert name.startswith("bin_fixture4_pvp"), name
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
        pk[2], pk[3], pk[4] = ITM_AXE, ITM_SHOVEL, ITM_NET
        g[base + 0x68:base + 0x68 + 30] = struct.pack(">15H", *pk)
    m = bytearray(g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE])
    m[MF.CHK_OFF:MF.CHK_OFF + 2] = struct.pack(">H", MF.checksum(bytes(m)))
    g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE] = m
    g[MF.BACK_OFF:MF.BACK_OFF + MF.SAVE_SECTOR_SIZE] = g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_SECTOR_SIZE]
    open(p, "wb").write(bytes(g))
    return dst


def stat(p):
    a = p.cmd("pvpstat", 20) or ""
    m = STAT_RX.search(a)
    return None if m is None else dict(wallet=int(m.group(1)), equip=int(m.group(2), 16), wear=int(m.group(3)), pockets=m.group(4), main=int(m.group(5)),
                                       sent=int(m.group(6)), accepted=int(m.group(7)), rejected=int(m.group(8)), applied=int(m.group(9)), dropped=int(m.group(10)))


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
    return (float(m.group(1)), float(m.group(2))) if m else None


def setup(p, x, z, look_x, look_z):
    p.cmd("tp %.1f %.1f" % (x, z), 20)
    time.sleep(0.6)
    p.cmd("face %.1f %.1f" % (look_x, look_z), 20)


def equip(p, item, check, tag):
    a = p.cmd("equip %x" % item, 40)
    check("%s %s equips 0x%04X (%s)" % (tag, p.label, item, a), a is not None and " ok" in a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=17310)
    ap.add_argument("--fixture", default="bin_fixture4_pvp_real")
    ap.add_argument("--exe", default=DEFAULT_EXE)
    ap.add_argument("--s0", action="store_true", help="first run the vanilla (AC_TEST_PVP_OFF=1) axe-on-puppet check")
    args = ap.parse_args()
    if not (17000 <= args.port <= 17999):
        print("REFUSING: ports 17000-17999 only", file=sys.stderr)
        return 2
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731

    if args.s0:
        run_baseline(args, check)

    BIN = make_fixture(args.fixture, args.exe)
    W.BIN = BIN
    procs = []
    try:
        env_common = {"AC_TEST_HOOKS": "1"}
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=dict(env_common, AC_TEST_AUTOPILOT="ac_auto_H.txt"),
                             log_path=os.path.join(HERE, "pvpreal_host.log"), bin_dir=BIN).start()
        procs.append(host)
        ok = host.wait_listening(60) and host.boot_to_field(timeout=120, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        a = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"], env=dict(env_common, AC_TEST_AUTOPILOT="ac_auto_A.txt"),
                            log_path=os.path.join(HERE, "pvpreal_A.log"), bin_dir=BIN, label="pvpA").start()
        procs.append(a)
        check("client A in the field", a.boot_to_field(timeout=200, slot=1))
        b = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "2", "-debug"],
                            env=dict(env_common, AC_TEST_AUTOPILOT="ac_auto_B.txt", PC_PUPPET_DIAG="1"),
                            log_path=os.path.join(HERE, "pvpreal_B.log"), bin_dir=BIN, label="pvpB").start()
        procs.append(b)
        check("observer client B in the field", b.boot_to_field(timeout=200, slot=2))
        H, A, B = W.Player(L, "H", host), W.Player(L, "A", a), W.Player(L, "B", b)
        hp = ppos(H)
        check("host position %s" % (hp,), hp is not None)
        if hp is None:
            return L.summary_and_exit_code(results)
        X, Z = hp
        s0h, s0a = stat(H), stat(A)
        check("baseline pvpstat host %s / A %s" % (s0h, s0a), s0h is not None and s0a is not None)
        B.cmd("tp %.1f %.1f" % (X + 80.0, Z - 14.0), 20)
        time.sleep(11.0)  # arrival protection (10 s) after everybody entered the town field

        # ---- P1 A axe -> host
        equip(A, ITM_AXE, check, "P1")
        setup(H, X, Z, X, Z - 100.0)
        setup(A, X, Z - 30.0, X, Z)
        time.sleep(2.0)
        ho, ao, bo = len(host.log_text()), len(a.log_text()), len(b.log_text())
        ans = A.cmd("swing", 30)
        L.info("A swing: %s" % ans)
        m_att = wait_rx(a.log_text, r"\[NET\]\[PVP\] attacker: tool 1 contact on player 8[^\r\n]*", 6, ao)
        check("P1 A's axe contact produced a PVP_HIT_REQ on the host player (%s)" % (m_att.group(0) if m_att else a.log_text()[ao:][-400:]), m_att)
        m_acc = wait_rx(host.log_text, r"\[NET\]\[PVP\] host: hit by \d+ on 8 tool 1 seq \d+ accepted -> TUMBLE[^\r\n]*", 6, ho)
        check("P1 the host accepted it -> TUMBLE (%s)" % (m_acc.group(0) if m_acc else host.log_text()[ho:][-400:]), m_acc)
        m_ent = wait_rx(host.log_text, r"\[NET\]\[PVP\] victim: TUMBLE entered[^\r\n]*", 6, ho)
        check("P1 the host's own player entered TUMBLE (%s)" % (m_ent.group(0) if m_ent else None), m_ent)
        m_kb = wait_rx(host.log_text, r"\[NET\]\[PVP\] victim: knockback ([\d.]+) units[^\r\n]*", 8, ho)
        kb = float(m_kb.group(1)) if m_kb else 0.0
        check("P1 knockback %.1f units (expected ~20..80) (%s)" % (kb, m_kb.group(0) if m_kb else None), 15.0 <= kb <= 90.0)
        s1a = stat(A)
        check("P1 A's axe wear unchanged by the puppet contact (%s -> %s) and A never entered REFLECT_AXE" % (s0a and s0a["wear"], s1a and s1a["wear"]),
              s1a is not None and s0a is not None and s1a["wear"] == s0a["wear"] and ",39," not in "," + (ans or "").split("states ")[-1])
        check("P1 A's swing was a plain AIR_AXE (38) (states %s)" % ((ans or "").split("states ")[-1]), ",38," in "," + (ans or "").split("states ")[-1])
        m_b = wait_rx(b.log_text, r"\[NET\]\[PUPPET\]\[DIAG\] player 8 state main_index=11 [^\r\n]*row=tumble[^\r\n]*", 8, bo)
        check("P6 observer B sees the host's puppet tumble row (%s)" % (m_b.group(0) if m_b else None), m_b)
        others = re.findall(r"client: PVP_HIT from", b.log_text()[bo:]) + re.findall(r"client: PVP_HIT from", a.log_text()[ao:])
        check("P1 nobody but the victim received a PVP_HIT (%d on A / B)" % len(others), not others)
        time.sleep(3.0)

        # ---- P2 host axe -> A
        equip(H, ITM_AXE, check, "P2")
        setup(A, X, Z - 30.0, X, Z - 100.0)
        setup(H, X, Z, X, Z - 30.0)
        time.sleep(2.0)
        ho, ao, bo = len(host.log_text()), len(a.log_text()), len(b.log_text())
        H.cmd("swing", 30)
        m_acc = wait_rx(host.log_text, r"\[NET\]\[PVP\] host: hit by 8 on (\d+) tool 1 seq \d+ accepted -> TUMBLE[^\r\n]*", 6, ho)
        check("P2 the host's own swing was judged locally and accepted (%s)" % (m_acc.group(0) if m_acc else host.log_text()[ho:][-400:]), m_acc)
        m_q = wait_rx(a.log_text, r"\[NET\]\[PVP\] client: PVP_HIT from 8 tool 1 reaction 1[^\r\n]*", 6, ao)
        m_ent = wait_rx(a.log_text, r"\[NET\]\[PVP\] victim: TUMBLE entered[^\r\n]*", 6, ao)
        check("P2 A received PVP_HIT and entered TUMBLE (%s | %s)" % (m_q.group(0) if m_q else None, m_ent.group(0) if m_ent else None), m_q and m_ent)
        check("P2 B received no PVP_HIT", "client: PVP_HIT from" not in b.log_text()[bo:])
        aid = m_acc.group(1) if m_acc else "-1"
        m_b = wait_rx(b.log_text, r"\[NET\]\[PUPPET\]\[DIAG\] player %s state main_index=11 [^\r\n]*row=tumble" % aid, 8, bo)
        check("P6 observer B sees A's puppet tumble row (%s)" % (m_b.group(0) if m_b else None), m_b)
        time.sleep(3.0)

        # ---- P3 A shovel -> host
        equip(A, ITM_SHOVEL, check, "P3")
        setup(H, X, Z, X, Z - 100.0)
        setup(A, X, Z - 30.0, X, Z)
        time.sleep(2.0)
        ho, ao, bo = len(host.log_text()), len(a.log_text()), len(b.log_text())
        A.cmd("swing", 30)
        m_att = wait_rx(a.log_text, r"\[NET\]\[PVP\] attacker: tool 2 contact on player 8[^\r\n]*", 6, ao)
        m_ent = wait_rx(host.log_text, r"\[NET\]\[PVP\] victim: SHOCK entered[^\r\n]*", 6, ho)
        check("P3 A's shovel hit the host: request (%s), host SHOCK (%s)" % (m_att.group(0) if m_att else a.log_text()[ao:][-300:], m_ent.group(0) if m_ent else None),
              m_att and m_ent)
        check("P3 no dig was requested by A's shovel swing (no FIELD_ACTION dig line)", not re.search(r"dig|DIG_HOLE", a.log_text()[ao:].replace("dig_scoop", "")) or
              "request_dig" not in a.log_text()[ao:])
        m_b = wait_rx(b.log_text, r"\[NET\]\[PUPPET\]\[DIAG\] player 8 state main_index=99 [^\r\n]*row=shock", 8, bo)
        check("P6 observer B sees the host's puppet shock row (%s)" % (m_b.group(0) if m_b else None), m_b)
        time.sleep(3.0)

        # ---- P4 A net -> host
        equip(A, ITM_NET, check, "P4")
        setup(H, X, Z, X, Z - 100.0)
        setup(A, X, Z - 30.0, X, Z)
        time.sleep(2.0)
        ho, ao = len(host.log_text()), len(a.log_text())
        A.cmd("swing", 30)
        m_att = wait_rx(a.log_text, r"\[NET\]\[PVP\] attacker: tool 3 contact on player 8[^\r\n]*", 6, ao)
        m_ent = wait_rx(host.log_text, r"\[NET\]\[PVP\] victim: SHOCK entered[^\r\n]*", 6, ho)
        check("P4 A's net hit the host: request (%s), host SHOCK (%s)" % (m_att.group(0) if m_att else a.log_text()[ao:][-300:], m_ent.group(0) if m_ent else None),
              m_att and m_ent)
        time.sleep(3.0)

        # ---- P5 paused host is never interrupted
        equip(A, ITM_AXE, check, "P5")
        setup(H, X, Z, X, Z - 100.0)
        setup(A, X, Z - 30.0, X, Z)
        time.sleep(2.0)
        ho = len(host.log_text())
        H.cmd("pause 1", 20)
        A.cmd("swing", 30)
        time.sleep(2.0)
        H.cmd("pause 0", 20)
        time.sleep(2.0)
        lg = host.log_text()[ho:]
        acc = re.search(r"host: hit by \d+ on 8 tool 1 seq \d+ (accepted|rejected)[^\r\n]*", lg)
        check("P5 paused host: request judged (%s), no reaction entered, hit dropped or never applied" % (acc.group(0) if acc else None),
              "victim: TUMBLE entered" not in lg and "victim: SHOCK entered" not in lg)

        # ---- P7 conservation
        s9h, s9a = stat(H), stat(A)
        for tag, s0, s9 in (("host", s0h, s9h), ("A", s0a, s9a)):
            check("P7 %s wallet / pockets+equipment digest / axe wear unchanged (%s -> %s)" % (tag, s0, s9),
                  s0 is not None and s9 is not None and s0["wallet"] == s9["wallet"] and s0["pockets"] == s9["pockets"] and s0["wear"] == s9["wear"])
    finally:
        for p in reversed(procs):
            rc = p.stop()
            check("%s did not crash (%s)" % (getattr(p, "label", "host"), rc), rc is None)
    return L.summary_and_exit_code(results)


def run_baseline(args, check):
    """S0: the vanilla behaviour, with the SAME build and the TEST-ONLY AC_TEST_PVP_OFF=1 on both processes: A's axe at the host's puppet bounces
    (REFLECT_AXE 39, axe wear +3). Then the feature ON (P1) must show AIR_AXE and no wear."""
    BIN = make_fixture(args.fixture + "_s0", args.exe)
    W.BIN = BIN
    host = a = None
    try:
        env = {"AC_TEST_HOOKS": "1", "AC_TEST_PVP_OFF": "1"}
        host = L.HostProcess(port=args.port + 1, extra_args=["--bootstrap-resident", "0", "-debug"], env=dict(env, AC_TEST_AUTOPILOT="ac_auto_H.txt"),
                             log_path=os.path.join(HERE, "pvps0_host.log"), bin_dir=BIN).start()
        if not (host.wait_listening(60) and host.boot_to_field(timeout=120, slot=0)):
            check("S0 host in the field", False)
            return
        L.resolve_host_town("127.0.0.1", args.port + 1)
        a = L.ClientProcess("127.0.0.1:%d" % (args.port + 1), extra_args=["--bootstrap-resident", "1", "-debug"], env=dict(env, AC_TEST_AUTOPILOT="ac_auto_A.txt"),
                            log_path=os.path.join(HERE, "pvps0_A.log"), bin_dir=BIN, label="pvps0A").start()
        if not a.boot_to_field(timeout=200, slot=1):
            check("S0 client in the field", False)
            return
        H, A = W.Player(L, "H", host), W.Player(L, "A", a)
        X, Z = ppos(H)
        equip(A, ITM_AXE, check, "S0")
        setup(H, X, Z, X, Z - 100.0)
        setup(A, X, Z - 30.0, X, Z)
        time.sleep(2.0)
        s0 = stat(A)
        ans = A.cmd("swing", 30) or ""
        s1 = stat(A)
        L.info("S0 swing answer: %s" % ans)
        check("S0 vanilla (PvP OFF): A's axe at the host's puppet REFLECTS (states %s) and wears the axe by 3 (%s -> %s)"
              % (ans.split("states ")[-1], s0 and s0["wear"], s1 and s1["wear"]),
              ",39," in "," + ans.split("states ")[-1] and s0 is not None and s1 is not None and s1["wear"] - s0["wear"] == 3)
    finally:
        for p in (a, host):
            if p is not None:
                p.stop()

if __name__ == "__main__":
    sys.exit(main())
