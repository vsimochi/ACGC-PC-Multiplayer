"""test_cloth_change_real.py - worker CL: other players see a clothes change. A REAL listen host + 1 REAL client (Samsung display, volume 1),
both in the town field, both run AC_TEST_AEXT_SCRIPT=clothes (started together through AC_TEST_AEXT_GO_FILE) and observe each other with
PC_PUPPET_DIAG=1:
  step 1 the INVENTORY path (the menu's own writes: Now_Private->cloth + mPlib_change_player_cloth, the real actor stays in WAIT)
  step 2 a real 101 change (Halloween request, try_on FALSE: ITAZURA1 125, Now_Private written by the state at frame 18)
  step 3 WAIT, step 4 the menu path back to the original outfit
  C1 each observer HOLDS the new outfit ([NET][REMOTE] ... cloth change held) after its APPEARANCE arrives
  C2 menu path: the observer plays the synthesized clip (row=cloth_menu anim0=73 = MENU_CHANGE1), the wear sound + KIGAE cue fire, the shirt is swapped
     at clip frame >= 36 (not at arrival)
  C3 101 path: the sender sends ACTION_EXT idx=101 anim0=125, the observer applies it (row=action_ext), the HALLOWEEN cue fires and the shirt
     is swapped by the state101 rule (frame >= 18), never by a menu clip
  C4 the restore is shown too (a second menu clip); the last appearance received equals the original outfit
  C5 nobody crashed
Run: python test_cloth_change_real.py --port 30110 [--fixture bin_fixture4_cl_real] [--exe <abs build64_w3 exe>]
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
    ap.add_argument("--port", type=int, default=30110)
    ap.add_argument("--fixture", default="bin_fixture4_cl_real")
    ap.add_argument("--exe", default=DEFAULT_EXE)
    args = ap.parse_args()
    if not (30000 <= args.port <= 30499):
        print("REFUSING: ports 30000-30499 only", file=sys.stderr)
        return 2
    if not args.fixture.startswith("bin_fixture4_cl_"):
        print("REFUSING: fixture must be bin_fixture4_cl_<name>", file=sys.stderr)
        return 2
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    BIN = T.make_fixture(args.fixture)
    shutil.copy2(args.exe, os.path.join(BIN, "AnimalCrossing.exe"))
    go = os.path.join(os.environ.get("TEMP", HERE), "cloth_go_%d.flag" % os.getpid())
    if os.path.exists(go):
        os.remove(go)
    procs = []
    t0 = time.time()
    try:
        env = {"AC_TEST_HOOKS": "1", "PC_PUPPET_DIAG": "1", "AC_TEST_AEXT_GO_FILE": go, "AC_TEST_AEXT_SCRIPT": "clothes"}
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=dict(env),
                             log_path=os.path.join(HERE, "clothreal_host.log"), bin_dir=BIN).start()
        procs.append(host)
        ok = host.wait_listening(60) and host.boot_to_field(timeout=120, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        a = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"], env=dict(env),
                            log_path=os.path.join(HERE, "clothreal_A.log"), bin_dir=BIN, label="clothA").start()
        procs.append(a)
        ok = a.boot_to_field(timeout=200, slot=1)
        check("client A in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        time.sleep(6.0)
        offs = {p: len(p.log_text()) for p in procs}
        open(go, "w").write("go\n")
        L.info("GO: scripts started")
        names = {host: "host", a: "A"}
        for p in procs:
            m = wait_rx(p.log_text, r"\[NET\]\[AEXT\]\[TEST\] script clothes done", 90, offs[p])
            steps = re.findall(r"\[NET\]\[AEXT\]\[TEST\] [^\r\n]*", p.log_text()[offs[p]:])
            check("%s script finished (%s)" % (names[p], steps[:8]), m)
        time.sleep(4.0)
        txt = {p: p.log_text()[offs[p]:] for p in procs}
        sender_items = {}
        for p in procs:
            mi = re.search(r"menu cloth item=0x([0-9A-F]{4}) idx=\d+ \(orig 0x([0-9A-F]{4})", txt[p])
            c1 = re.search(r"cloth101 item=0x([0-9A-F]{4}) request=(\d)", txt[p])
            sender_items[p] = (mi.group(1) if mi else None, mi.group(2) if mi else None, c1.group(1) if c1 else None)
            check("%s: test steps logged (menu %s, 101 %s request=%s)" % (names[p], sender_items[p][0], sender_items[p][2], c1.group(2) if c1 else None),
                  mi and c1 and c1.group(2) == "1")
            s101 = re.findall(r"\[NET\]\[AEXT\] send idx=101 ctr=\d+ anim0=(\d+)", txt[p])
            check("C3 %s sent ACTION_EXT idx=101 anim0=125 (ITAZURA1) for its 101 change (%s)" % (names[p], s101[:3]), "125" in s101)
        for obs, snd in ((host, a), (a, host)):
            ob = txt[obs]
            ids = re.findall(r"\[NET\]\[REMOTE\] player (\d+): cloth change held", ob)
            pid = ids[0] if ids else None
            check("C1 %s held the other player's new outfit(s) (player %s, %d holds)" % (names[obs], pid, len(ids)), pid is not None and len(ids) >= 2)
            if pid is None:
                continue
            ev = re.findall(r"\[NET\]\[REMOTE\] player %s: cloth change ([^(\r\n]+) \((\w+)\) frame=([\d.]+)" % pid, ob)
            check("C2 %s: event sequence (%s)" % (names[obs], ev[:10]), ev)
            menu_clips = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player %s state main_index=\d+ counter=\d+ row=cloth_menu anim0=73" % pid, ob)
            check("C2 %s played the synthesized MENU_CHANGE1 clip on the menu changes (%d clips)" % (names[obs], len(menu_clips)), len(menu_clips) >= 2)
            sw_menu = [float(f) for w, why, f in ev if w.startswith("shirt swapped") and why == "menu_clip"]
            check("C2 %s swapped the shirt at clip frame >= 36 on the menu path (%s)" % (names[obs], sw_menu), sw_menu and all(f >= 35.5 for f in sw_menu))
            kig = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player %s cosmetic effect=menu_cloth_kigae[^\r\n]*" % pid, ob)
            wear = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player %s menu_cloth_wear sound[^\r\n]*" % pid, ob)
            check("C2 %s: KIGAE + wear cues of the menu clip (%s / %s)" % (names[obs], kig[:1], wear[:1]), kig and wear)
            r101 = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player %s state main_index=101 counter=\d+ row=action_ext anim0=125" % pid, ob)
            check("C3 %s applied the 101 ITAZURA1 row (%d)" % (names[obs], len(r101)), r101)
            sw101 = [float(f) for w, why, f in ev if w.startswith("shirt swapped") and why == "state101"]
            check("C3 %s swapped the shirt by the state101 rule at frame >= 18 (%s)" % (names[obs], sw101), sw101 and all(f >= 17.5 for f in sw101))
            hal = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player %s cosmetic effect=cloth_halloween[^\r\n]*" % pid, ob)
            check("C3 %s: HALLOWEEN cue of the 101 row (%s)" % (names[obs], hal[:1]), hal)
            apps = re.findall(r"\[NET\]\[REMOTE\]\[DIAG\] player %s: appearance received \([^)]*cloth_item=(\d+)" % pid, ob)
            orig = sender_items[snd][1]
            check("C4 %s: the last appearance is the original outfit again (%s .. last %s, orig 0x%s)" % (names[obs], apps[:2], apps[-1:], orig),
                  apps and orig and int(apps[-1]) == int(orig, 16))
            # the shirt must not be swapped at arrival: each "held" line comes before its swap line
            seq = [w for w, _, _ in ev]
            check("C1 %s: every hold is followed by a swap (no instant swap at arrival) (%s)" % (names[obs], seq[:10]),
                  seq.count("held for the change animation ") <= sum(1 for s in seq if s.startswith("shirt swapped")) and not any(why == "timeout" for _, why, _ in ev))
        for p in procs:
            check("C5 %s still running" % names[p], p.exit_code() is None)
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
