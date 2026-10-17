#!/usr/bin/env python3
r"""test_chat_real.py - MULTIPLAYER TEXT CHAT (worker CH) with REAL processes: a listen host (resident 0) + a real client A (resident 1), Samsung display, volume 1.

A TYPES its line through the REAL keyboard path (TEST-ONLY AC_TEST_CHAT_TYPE: synthetic SDL events -> pc_main.c's event loop -> the chat key opens the line, its own
SDL_TEXTINPUT is swallowed, the text, Return); the host's line uses AC_TEST_CHAT_SEND (pc_chat_submit_utf8 -> pc_net_game_chat_send, what Enter does):
  R1 host and A reach the town field; A is teleported next to the host (autopilot)
  R2 A hears the host's capability announcement ('[NET][CHAT] client: the host announced chat ON')
  R3 A's line opens on the chat key, holds exactly the 29 typed codes (the opener 't' swallowed; '#', '/', ';' and 'e acute' are font codes), Return sends it
     (CHAT_SEND); the host validates + logs "[CHAT] <A>: Typed: hello #1 ok/yes; cafe!" and shows it (host screen); screenshot of A's open line
  R4 the host draws A's line as a bubble over its PUPPET of A ('[CHAT] bubble drawn speaker=<A> self=0'); A draws its echo over itself (self=1)
  R5 the host (waiting for a READY peer in its scene) sends "Hi from the host!": A draws it over its puppet of the host (speaker=8 self=0), the host over itself
  R6 no crash; screenshots chat_host_bubble.png / chat_A_bubble.png (best effort, PrintWindow) next to the logs
Usage: python test_chat_real.py [--port 30610]   (disposable fixture pc\build64\bin_fixture4_ch_real, exe from pc\build64_w3\bin)"""
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
import town_test_support as TS  # noqa: E402

FIXTURE = "bin_fixture4_ch_real"
BIN = os.path.join(TS.BUILD64, FIXTURE)
W3_EXE = os.path.join(TS.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = BIN
import net_spike_lib as L  # noqa: E402
import wplay_lib as W  # noqa: E402
import test_menu_world_live_real as MWL  # noqa: E402  (the PrintWindow screenshot helper)

W.BIN = BIN
TYPED = "Typed: hello #1 ok/yes; café!"


def wait_rx(get_text, rx, timeout, since=0):
    rx = re.compile(rx)
    end = time.time() + timeout
    while time.time() < end:
        m = list(rx.finditer(get_text()[since:]))
        if m:
            return m[-1]
        time.sleep(0.25)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=30610)
    args = ap.parse_args()
    if not (30500 <= args.port <= 30999):
        print("REFUSING: ports 30500-30999 only", file=sys.stderr)
        return 2
    TS.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(BIN, "AnimalCrossing.exe"))
    names = {i: p.player_name.rstrip(b"\x00 ").decode("latin-1") for i, p, ex in L.read_test_save_residents(BIN) if ex}
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = A = None
    t0 = time.time()
    try:
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"],
                             env={"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_host.txt", "AC_TEST_CHAT_SEND": "Hi from the host!",
                                  "AC_TEST_CHAT_SEND_DELAY_MS": "12000"},
                             log_path=os.path.join(HERE, "chat_real_host.log"), bin_dir=BIN).start()
        ok = host.wait_listening(60) and host.boot_to_field(timeout=120, slot=0)
        check("R1 host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        A = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"],
                            env={"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_A.txt", "AC_TEST_CHAT_TYPE": TYPED,
                                 "AC_TEST_CHAT_TYPE_DELAY_MS": "9000"},
                            log_path=os.path.join(HERE, "chat_real_A.log"), bin_dir=BIN, label="chatA").start()
        ok = A.boot_to_field(timeout=200, slot=1)
        check("R1 client A in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        H, PA = W.Player(L, "host", host), W.Player(L, "A", A)
        hp = H.pos()
        L.info("host pos %s, A pos %s" % (hp, PA.pos()))
        if hp is not None:
            L.info("A tp: %s" % PA.cmd("tp %.1f %.1f" % (hp[0] + 50.0, hp[1] + 30.0), 20))
        check("R2 A heard the host's chat capability (CAPS ON)", wait_rx(A.log_text, r"\[NET\]\[CHAT\] client: the host announced chat ON", 20.0))
        op = wait_rx(A.log_text, r"\[CHAT\] input open", 60.0)
        check("R3 the pushed chat key opened A's chat line through the event loop", op is not None)
        if wait_rx(A.log_text, r"AC_TEST_CHAT_TYPE: pushed the text", 10.0):
            time.sleep(1.2)
            MWL.shot(A, os.path.join(HERE, "chat_A_typing.png"))
        m = wait_rx(A.log_text, r"AC_TEST_CHAT_TYPE: the line holds (\d+) code\(s\) before Return \(typing=(\d)\)", 15.0)
        check("R3 the line held exactly the %d typed codes (the opener's own 't' swallowed) (%s)" % (len(TYPED), m and m.group(0)),
              m is not None and int(m.group(1)) == len(TYPED) and m.group(2) == "1")
        check("R3 Return closed the line and sent CHAT_SEND seq 1 len %d" % len(TYPED), wait_rx(A.log_text, r"\[CHAT\] input closed \(sent\)", 5.0) and
              wait_rx(A.log_text, r"\[NET\]\[CHAT\] client: CHAT_SEND seq 1 len %d" % len(TYPED), 5.0))
        hl = wait_rx(host.log_text, r"\[CHAT\] %s: Typed: hello #1 ok/yes; caf.{1,2}!" % re.escape(names.get(1, "?")), 10.0)
        hd = wait_rx(host.log_text, r"\[NET\]\[CHAT\] host: chat from (\d+) .*-> 0 peer\(s\) \+ echo \+ host screen", 10.0)
        check("R3 the host validated, logged and showed A's line (%s / %s)" % (hl and hl.group(0), hd and hd.group(0)), hl is not None and hd is not None)
        a_id = hd.group(1) if hd else "-1"
        hb = wait_rx(host.log_text, r"\[CHAT\] bubble drawn speaker=%s self=0 stack=0 at \(([-\d]+),([-\d]+)\)" % a_id, 10.0)
        ab = wait_rx(A.log_text, r"\[CHAT\] bubble drawn speaker=%s self=1 stack=0 at \(([-\d]+),([-\d]+)\)" % a_id, 10.0)
        time.sleep(0.5)
        MWL.shot(host, os.path.join(HERE, "chat_host_bubble.png"))
        MWL.shot(A, os.path.join(HERE, "chat_A_bubble.png"))
        check("R4 the host drew A's bubble over its puppet of A (%s)" % (hb and hb.group(0)), hb is not None)
        check("R4 A drew its own echo over itself (%s)" % (ab and ab.group(0)), ab is not None)
        hm = wait_rx(host.log_text, r"\[CHAT\]\[TEST\] AC_TEST_CHAT_SEND through the real send path: (\w+)", 60.0)
        check("R5 the host's line went through the real send path (%s)" % (hm and hm.group(0)), hm is not None and hm.group(1) == "sent")
        ah = wait_rx(A.log_text, r"\[CHAT\] bubble drawn speaker=8 self=0 stack=0 at \(([-\d]+),([-\d]+)\)", 15.0)
        hh = wait_rx(host.log_text, r"\[CHAT\] bubble drawn speaker=8 self=1 stack=0", 15.0)
        time.sleep(0.5)
        MWL.shot(A, os.path.join(HERE, "chat_A_host_bubble.png"))
        MWL.shot(host, os.path.join(HERE, "chat_host_self_bubble.png"))
        check("R5 A drew the host's line over its puppet of the host (%s)" % (ah and ah.group(0)), ah is not None)
        check("R5 the host drew its own line over itself (%s)" % (hh and hh.group(0)), hh is not None)
        check("R6 both processes still running", host.exit_code() is None and A.exit_code() is None)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("test aborted by an exception: %s" % e, False)
    finally:
        for p in (A, host):
            if p is not None:
                try:
                    p.stop()
                except Exception:  # noqa: BLE001
                    pass
    print("elapsed %.0f s" % (time.time() - t0))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
