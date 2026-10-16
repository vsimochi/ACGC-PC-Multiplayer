"""test_action_ext_real.py - PUPPET ACTION_EXT (96, worker P2) with a REAL listen host + 2 REAL clients (Samsung display, volume 1), all in the
town field. Every process runs a TEST-ONLY script of REAL vanilla states (AC_TEST_HOOKS=1, AC_TEST_AEXT_SCRIPT, started together through
AC_TEST_AEXT_GO_FILE once all three are in the field) and observes the others with PC_PUPPET_DIAG=1:
  host  'bed'   LIE_BED (left INBED_L1 40, then right INBED_R1 41, pin 3) -> WAIT_BED -> ROLL_BED (NEGAERI 46/47) -> STANDUP_BED (OUTBED 48/49)
  A     'talk'  DEMO_WAIT -> demo order 2: GAAAN1 (110) then GAAAN2 (111, face 6 / 5) -> order 0xFE: BIKU1 (118) -> WAIT
  B     'radio' RADIO_EXERCISE command 0 (TAISOU1 144, speed retunes)
  E1 each sender logs [NET][AEXT] send lines for its states
  E2 each observer applies them as dynamic rows: [NET][PUPPET][DIAG] player N state main_index=I ... row=action_ext anim0=X
  E3 the talk reaction's GAAAN2 face reaches the observers (face eye=6 mouth=5 anim0=111) and the SHOCK effect / bed sounds are produced
  E4 the bed tilt / pin: LIE_BED applied with pin=3; the overlay ends when the owner leaves the state
  E5 nobody crashed; no process applied its own clip (a client never stores its own id)
Run: python test_action_ext_real.py --port 23120 [--fixture bin_fixture4_p2_real] [--exe <abs build64_w3 exe>]
"""
import argparse
import os
import re
import shutil
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
import town_test_support as T  # noqa: E402

DEFAULT_EXE = os.path.abspath(os.path.join(HERE, "..", "..", "build64_w3", "bin", "AnimalCrossing.exe"))


def wait_rx(get_text, rx, timeout, since=0):
    end = time.time() + timeout
    while time.time() < end:
        m = re.search(rx, get_text()[since:])
        if m:
            return m
        time.sleep(0.5)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=23120)
    ap.add_argument("--fixture", default="bin_fixture4_p2_real")
    ap.add_argument("--exe", default=DEFAULT_EXE)
    args = ap.parse_args()
    if not (23000 <= args.port <= 23999):
        print("REFUSING: ports 23000-23999 only", file=sys.stderr)
        return 2
    if not args.fixture.startswith("bin_fixture4_p2_"):
        print("REFUSING: fixture must be bin_fixture4_p2_<name>", file=sys.stderr)
        return 2
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    BIN = T.make_fixture(args.fixture)
    shutil.copy2(args.exe, os.path.join(BIN, "AnimalCrossing.exe"))
    go = os.path.join(os.environ.get("TEMP", HERE), "aext_go_%d.flag" % os.getpid())
    if os.path.exists(go):
        os.remove(go)
    procs = []
    t0 = time.time()
    try:
        env = {"AC_TEST_HOOKS": "1", "PC_PUPPET_DIAG": "1", "AC_TEST_AEXT_GO_FILE": go}
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=dict(env, AC_TEST_AEXT_SCRIPT="bed"),
                             log_path=os.path.join(HERE, "aextreal_host.log"), bin_dir=BIN).start()
        procs.append(host)
        ok = host.wait_listening(60) and host.boot_to_field(timeout=120, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        a = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"], env=dict(env, AC_TEST_AEXT_SCRIPT="talk"),
                            log_path=os.path.join(HERE, "aextreal_A.log"), bin_dir=BIN, label="aextA").start()
        procs.append(a)
        check("client A in the field", a.boot_to_field(timeout=200, slot=1))
        b = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "2", "-debug"], env=dict(env, AC_TEST_AEXT_SCRIPT="radio"),
                            log_path=os.path.join(HERE, "aextreal_B.log"), bin_dir=BIN, label="aextB").start()
        procs.append(b)
        check("client B in the field", b.boot_to_field(timeout=200, slot=2))
        time.sleep(6.0)  # every process sees the other two (scene announcements, first MOVEs)
        offs = {p: len(p.log_text()) for p in procs}
        open(go, "w").write("go\n")
        L.info("GO: scripts started")
        names = {host: "host", a: "A", b: "B"}
        # scripts: talk ~9 s, bed ~2 x 8 s, radio ~5 s after the 4 s WAIT guard
        for p, rx in ((host, r"\[NET\]\[AEXT\]\[TEST\] script bed done"), (a, r"\[NET\]\[AEXT\]\[TEST\] script talk done"),
                      (b, r"\[NET\]\[AEXT\]\[TEST\] script radio done")):
            m = wait_rx(p.log_text, rx, 90, offs[p])
            steps = re.findall(r"\[NET\]\[AEXT\]\[TEST\] [^\r\n]*", p.log_text()[offs[p]:])
            check("%s script finished (%s)" % (names[p], steps[:10]), m)
        time.sleep(3.0)
        txt = {p: p.log_text()[offs[p]:] for p in procs}
        # E1 senders
        sends = {p: re.findall(r"\[NET\]\[AEXT\] send idx=(\d+) ctr=\d+ anim0=(\d+)", txt[p]) for p in procs}
        check("E1 host sent the bed states (%s)" % sends[host][:12], {"26", "28", "29"} <= set(i for i, _ in sends[host]))
        check("E1 A sent the talk states (%s)" % sends[a][:12], ("74", "110") in sends[a] and ("74", "111") in sends[a] and ("74", "118") in sends[a])
        check("E1 B sent the radio state (%s)" % sends[b][:6], ("111", "144") in sends[b])
        # E2 observers (ids: host 8; a client's id read from the observer lines)
        rowrx = r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) state main_index=(\d+) counter=\d+ row=action_ext anim0=(\d+) anim1=\d+ part_table=\d+ mode=\w+ pin=(\d+)"
        rows = {p: re.findall(rowrx, txt[p]) for p in procs}
        for obs in (a, b):
            hb = [(i, a0, pin) for pid, i, a0, pin in rows[obs] if pid == "8"]
            check("E2 %s applies the host's bed rows (%s)" % (names[obs], hb[:10]),
                  any(i == "26" and a0 in ("40", "41") and pin == "3" for i, a0, pin in hb) and any(i == "28" and a0 in ("46", "47") for i, a0, pin in hb)
                  and any(i == "29" and a0 in ("48", "49") for i, a0, pin in hb))
            check("E2 %s saw both bed sides (INBED_L1 40 and INBED_R1 41)" % names[obs], {"40", "41"} <= set(a0 for i, a0, _ in hb if i == "26"))
        for obs in (host, b):
            ta = [(pid, i, a0) for pid, i, a0, _ in rows[obs] if i == "74"]
            check("E2 %s applies A's talk reaction rows GAAAN1 110 -> GAAAN2 111 -> BIKU1 118 (%s)" % (names[obs], ta[:8]),
                  {"110", "111", "118"} <= set(a0 for _, _, a0 in ta))
        for obs in (host, a):
            ra = [(pid, i, a0) for pid, i, a0, _ in rows[obs] if i == "111"]
            check("E2 %s applies B's radio row TAISOU1 144 (%s)" % (names[obs], ra[:4]), any(a0 == "144" for _, _, a0 in ra))
        # E3 face / cues
        for obs in (host, b):
            # the face line is printed only when the pattern CHANGES: GAAAN1's last frames already end on 6 / 5, so the face shown during
            # GAAAN2 is the last face line before (or any face line during) its row, and no other pattern may appear while it plays
            g2 = re.search(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) state main_index=74 counter=\d+ row=action_ext anim0=111", txt[obs])
            ok_face, seen = False, []
            if g2:
                pid = g2.group(1)
                before = re.findall(r"player %s face eye=(\d+) mouth=(\d+) anim0=\d+" % pid, txt[obs][:g2.start()])
                during = re.findall(r"player %s face eye=(\d+) mouth=(\d+) anim0=111 " % pid, txt[obs][g2.start():])
                seen = (before[-1:], during[:4])
                ok_face = ((before and before[-1] == ("6", "5")) or ("6", "5") in during) and all(f == ("6", "5") for f in during)
            check("E3 %s shows the GAAAN2 face 6 / 5 on A's puppet (last before / during: %s)" % (names[obs], seen), ok_face)
            sh = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player \d+ cosmetic effect=(?:demo_shock|talk_shock)[^\r\n]*", txt[obs])
            check("E3 %s: A's SHOCK effect cue fired (%s)" % (names[obs], sh[:2]), sh)
        for obs in (a, b):
            bs = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player 8 (bed_\w+) sound label=-?\d+ frame=[\d.]+ budget=(\w+)", txt[obs])
            check("E3 %s: the host's bed sounds were produced at its puppet (%s)" % (names[obs], bs[:6]), any(n == "bed_jump" for n, _ in bs) and any(n == "bed_out" for n, _ in bs))
        # E4 ends
        for obs in (a, b):
            ends = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player 8 action_ext end main_index=(\d+)", txt[obs])
            check("E4 %s: the host's dynamic rows ended with its states (%s)" % (names[obs], ends[:8]), "29" in ends)
        # E5
        for p in procs:
            check("E5 %s still running" % names[p], p.exit_code() is None)
        own_a = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) action_ext rx main_index=74", txt[a])
        check("E5 A never stored its own talk clip (%s)" % own_a[:3], not own_a)
    finally:
        for p in procs:
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                pass
        if os.path.exists(go):
            os.remove(go)
        print("elapsed %.0f s" % (time.time() - t0))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
