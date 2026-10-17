"""test_emote_real.py - EMOTE WHEEL with a REAL listen host + 2 REAL clients (Samsung display, volume 1), all in the town field.
Client A runs the TEST-ONLY script AC_TEST_EMOTE_SCRIPT=all (AC_TEST_HOOKS=1, started through AC_TEST_EMOTE_GO_FILE once everybody stands
together): it drives the REAL wheel (a forced hold + selection, then the release = the player's own submit path) for every emote 1..8, cancels
Exercise with a stick push, then probes the gates (cooldown, DEMO_WAIT talk state, the open chat line). The host and client B observe with
PC_PUPPET_DIAG=1.
  R1 A played every emote: [EMOTE] start id=1,3..8 (overlay) and id=2 through the vanilla SHOCK request
  R2 A's ACTION_EXT sends: idx=74 with the emote clips (128 118 156 97 125 73 144..) and variant = the emote id; Phew face 0x44
  R3 host + B apply A's rows (row=action_ext main_index=74 anim0=...) and A's SHOCK through the static row (main_index=99)
  R4 the observers fire the emote cues at A's puppet: surprised '!' / boo smoke / twirl sparkle + wear / phew dust + araiiki / hooray
     flowers; the continuous ones are killed when the row ends
  R5 the overlay ends: clip end (done), Exercise cancelled by the movement input (state -> WALK), the observers end the rows
  R6 gates: cooldown, talk (DEMO_WAIT) and chat refuse; inventory / equipment / cloth hash identical at the start and the end
  R7 nobody crashed; screenshots emote_A_wheel.png (A's open wheel), emote_host_view.png (the host sees A emote)
Run: python test_emote_real.py --port 33110 [--fixture bin_fixture4_emo_real] [--exe <abs build64_w3 exe>]
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
import town_test_support as T  # noqa: E402

DEFAULT_FIXTURE = "bin_fixture4_emo_real"
DEFAULT_EXE = os.path.abspath(os.path.join(HERE, "..", "..", "build64_w3", "bin", "AnimalCrossing.exe"))


def wait_rx(get_text, rx, timeout, since=0):
    end = time.time() + timeout
    while time.time() < end:
        m = re.search(rx, get_text()[since:])
        if m:
            return m
        time.sleep(0.4)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=33110)
    ap.add_argument("--fixture", default=DEFAULT_FIXTURE)
    ap.add_argument("--exe", default=DEFAULT_EXE)
    args = ap.parse_args()
    if not (33000 <= args.port <= 33499):
        print("REFUSING: ports 33000-33499 only", file=sys.stderr)
        return 2
    if not args.fixture.startswith("bin_fixture4_emo_"):
        print("REFUSING: fixture must be bin_fixture4_emo_<name>", file=sys.stderr)
        return 2
    BIN = T.make_fixture(args.fixture)
    os.environ["NET_SPIKE_GAME_BIN"] = BIN
    import net_spike_lib as L  # noqa: E402
    import wplay_lib as W  # noqa: E402
    import test_menu_world_live_real as MWL  # noqa: E402  (PrintWindow screenshot helper)
    W.BIN = BIN
    shutil.copy2(args.exe, os.path.join(BIN, "AnimalCrossing.exe"))
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    go = os.path.join(os.environ.get("TEMP", HERE), "emote_go_%d.flag" % os.getpid())
    if os.path.exists(go):
        os.remove(go)
    procs = []
    t0 = time.time()
    try:
        base = {"AC_TEST_HOOKS": "1", "PC_PUPPET_DIAG": "1"}
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"],
                             env=dict(base, AC_TEST_AUTOPILOT="ac_auto_host.txt"),
                             log_path=os.path.join(HERE, "emotereal_host.log"), bin_dir=BIN).start()
        procs.append(host)
        ok = host.wait_listening(60) and host.boot_to_field(timeout=120, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        a = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"],
                            env=dict(base, AC_TEST_AUTOPILOT="ac_auto_A.txt", AC_TEST_EMOTE_SCRIPT="all", AC_TEST_EMOTE_GO_FILE=go),
                            log_path=os.path.join(HERE, "emotereal_A.log"), bin_dir=BIN, label="emoA").start()
        procs.append(a)
        check("client A in the field", a.boot_to_field(timeout=200, slot=1))
        b = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "2", "-debug"],
                            env=dict(base, AC_TEST_AUTOPILOT="ac_auto_B.txt"),
                            log_path=os.path.join(HERE, "emotereal_B.log"), bin_dir=BIN, label="emoB").start()
        procs.append(b)
        check("client B in the field", b.boot_to_field(timeout=200, slot=2))
        names = {host: "host", a: "A", b: "B"}
        H, PA, PB = W.Player(L, "host", host), W.Player(L, "A", a), W.Player(L, "B", b)
        hp = H.pos()
        L.info("host pos %s" % (hp,))
        if hp is not None:
            L.info("A tp: %s" % PA.cmd("tp %.1f %.1f" % (hp[0] + 60.0, hp[1] + 40.0), 20))
            L.info("B tp: %s" % PB.cmd("tp %.1f %.1f" % (hp[0] - 60.0, hp[1] + 40.0), 20))
            check("host is outside the station zone (masking applies) %s" % (hp,), not (abs(hp[0] - 2200.0) < 180.0 and abs(hp[1] - 820.0) < 180.0))
        time.sleep(6.0)  # scene announcements, first MOVEs after the teleports
        offs = {p: len(p.log_text()) for p in procs}
        open(go, "w").write("go\n")
        L.info("GO: emote script started")
        m = wait_rx(a.log_text, r"\[EMOTE\]\[TEST\] wheel open for screenshot", 60, offs[a])
        if m:
            time.sleep(0.6)
            MWL.shot(a, os.path.join(HERE, "emote_A_wheel.png"))
        m6 = wait_rx(a.log_text, r"\[EMOTE\] start id=7 ", 150, offs[a])
        if m6:
            time.sleep(0.5)
            MWL.shot(host, os.path.join(HERE, "emote_host_view.png"))
            MWL.shot(a, os.path.join(HERE, "emote_A_twirl.png"))
        done = wait_rx(a.log_text, r"\[EMOTE\]\[TEST\] script all done inv=([0-9A-F]{8}) state=(-?\d+)", 240, offs[a])
        steps = re.findall(r"\[EMOTE\]\[TEST\] [^\r\n]*", a.log_text()[offs[a]:])
        check("A script finished (%s)" % steps[-14:], done)
        time.sleep(3.0)
        txt = {p: p.log_text()[offs[p]:] for p in procs}
        ta = txt[a]
        # R1
        starts = re.findall(r"\[EMOTE\] start id=(\d) \((\w+!?)\)", ta)
        check("R1 A started the overlay emotes 1,3..8 (%s)" % starts, {"1", "3", "4", "5", "6", "7", "8"} <= set(i for i, _ in starts))
        check("R1 A's Shocked went through the vanilla SHOCK request", re.search(r"\[EMOTE\] start id=2 \(Shocked\) vanilla SHOCK request=1", ta))
        timeouts = re.findall(r"\[EMOTE\]\[TEST\] id=\d TIMEOUT[^\r\n]*", ta)
        check("R1 no emote step timed out (%s)" % timeouts, not timeouts)
        # R2 sender
        sends = re.findall(r"\[NET\]\[AEXT\] send idx=(\d+) ctr=\d+ anim0=(\d+) anim1=\d+ pt=\d+ flags=0x[0-9A-F]+ speed_q=\d+ frame=\d+ face=0x([0-9A-F]+) variant=(\d+)", ta)
        emo = {(a0, v) for i, a0, _f, v in sends if i == "74"}
        want = {("128", "1"), ("118", "3"), ("156", "4"), ("97", "5"), ("125", "6"), ("73", "7"), ("144", "8")}
        check("R2 A sent its emotes as DEMO_WAIT rows with the emote id in variant (%s)" % sorted(emo), want <= emo)
        check("R2 Phew carries the constant face 0x44", any(i == "74" and a0 == "97" and f == "44" for i, a0, f, _v in sends))
        check("R2 Exercise switched clips inside the row (%s)" % sorted({a0 for a0, v in emo if v == "8"}), len({a0 for a0, v in emo if v == "8"}) >= 2)
        # R3 observers
        rowrx = r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) state main_index=(\d+) counter=\d+ row=action_ext anim0=(\d+)"
        for obs in (host, b):
            rows = [(pid, i, a0) for pid, i, a0 in re.findall(rowrx, txt[obs]) if i == "74"]
            got = {a0 for _p, _i, a0 in rows}
            check("R3 %s applied A's emote rows (%s)" % (names[obs], sorted(got)), {"128", "118", "156", "97", "125", "73", "144"} <= got)
            sh = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player \d+ state main_index=99 counter=\d+ row=\w+", txt[obs])
            check("R3 %s showed A's Shocked through the SHOCK row (%s)" % (names[obs], sh[:2]), sh)
        # R4 cues
        for obs in (host, b):
            fx = set(re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player \d+ cosmetic effect=(emote_\w+)", txt[obs]))
            snd = set(re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player \d+ (emote_\w+) sound label=", txt[obs]))
            check("R4 %s: A's emote marks / effects fired (%s)" % (names[obs], sorted(fx)),
                  {"emote_surprised_mark", "emote_boo_smoke", "emote_twirl_sparkle", "emote_hooray_flowers"} <= fx)
            check("R4 %s: Phew dust + sweat (%s)" % (names[obs], sorted(fx)), {"emote_phew_dust", "emote_phew_sweat"} <= fx)
            check("R4 %s: A's emote sounds (%s)" % (names[obs], sorted(snd)), {"emote_twirl_wear", "emote_phew_araiiki"} <= snd)
            kills = set(re.findall(r"cosmetic effect kill (emote_\w+) reason=", txt[obs]))
            check("R4 %s: the continuous emote effects were killed at the row end (%s)" % (names[obs], sorted(kills)),
                  {"emote_hooray_flowers", "emote_phew_sweat"} <= kills)
        # R5 ends
        ends = re.findall(r"\[EMOTE\] end id=(\d) \(\w+!?\) reason=(\w+) state=(-?\d+)", ta)
        check("R5 A's clip emotes ended by themselves (%s)" % ends, all(any(i == k and r == "done" for i, r, _s in ends) for k in ("1", "3", "4", "5", "6", "7")))
        check("R5 Exercise was cancelled by the movement input (left WAIT) (%s)" % [e for e in ends if e[0] == "8"],
              any(i == "8" and r in ("state", "input") for i, r, _s in ends))
        for obs in (host, b):
            oe = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player \d+ action_ext end main_index=74", txt[obs])
            check("R5 %s ended A's emote rows (%d)" % (names[obs], len(oe)), len(oe) >= 6)
        # R6 gates + inventory
        check("R6 the cooldown refused a second emote", re.search(r"\[EMOTE\] refused id=4 reason=cooldown", ta))
        check("R6 the talk state refused the wheel", re.search(r"\[EMOTE\] wheel refused \((?:state|demo|msg|menu)\)", ta))
        check("R6 the open chat line refused the wheel", re.search(r"\[EMOTE\] wheel refused \(chat\)", ta) and re.search(r"gate chat: typing=1", ta))
        h0 = re.search(r"script all start inv=([0-9A-F]{8})", ta)
        check("R6 inventory / equipment / cloth unchanged (%s -> %s)" % (h0 and h0.group(1), done and done.group(1)), h0 and done and h0.group(1) == done.group(1))
        # R7
        for p in procs:
            check("R7 %s still running" % names[p], p.exit_code() is None)
        own = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) action_ext rx main_index=74 counter=\d+ anim0=(?:128|97|125)", ta)
        check("R7 A never stored its own emote rows (%s)" % own[:3], not own)
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
