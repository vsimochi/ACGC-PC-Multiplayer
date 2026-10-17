#!/usr/bin/env python3
"""test_handover_real.py - HAND-OVER ANIMATIONS with REAL game processes: Nook's Work Mode in the REAL Nook dialogue, both directions.

TIER: REAL listen HOST + REAL resident client A (the worker) + REAL resident client B (the observer, standing in the same shop). Samsung display, volume 1.
  host: AC_TEST_WORK_TYPE=1,3 (the 1st job is the town fruit for Nook, the 2nd a parcel delivery to a villager).
  A:    --nook-test warp,resident (into Nook's shop) + AC_TEST_SELL_BASKET=2 (its placement: 55 units in front of Nook, facing him; it also stages a fish / an insect /
        a peach / a 1,000 Bells bag in pockets 0..3) + AC_TEST_HANDOVER_FRUIT=1 (the town fruit into a free pocket) = its local test writes (the D3 upload carries them)
        + the autopilot "mashw" (A presses; Nook's first page -> the Work Mode choice, every other choice -> the first one):
        talk 1: "I'd like to work" -> ENTER (job 1, FRUIT) -> the job row -> "Here it is!" -> DELIVER APPLIED -> the PLAYER hands the fruit to NOOK -> the closing row
        talk 2: "I'd like to work" -> ENTER (job 2, parcel) -> the job row -> "take the parcel" -> TAKE_PARCEL APPLIED -> NOOK hands the parcel to the PLAYER -> the closing row
  B:    --nook-test warp,resident, PC_PUPPET_DIAG=1; reads Nook's welcome away with single A presses (its own autopilot file), then stands there.
Asserts (per talk; D = DELIVER, T = TAKE_PARCEL):
  R1 the dialogue path: the Work Mode choice, the job row choice -> op 2 / op 4; the host committed ENTER x2, DELIVER x1, TAKE_PARCEL x1
  R2 A's Nook plays the hand-over: the plan (op 2 -> player -> Nook, the fruit / op 4 -> Nook -> player, the parcel), the 103 report, "hand-over 1/1 ... done"
  R3 A's REAL player runs the vanilla states with the item: D = GIVE 70 (aux = the fruit) + GIVE_WAIT 71; T = RECIEVE_STRETCH 67 / RECIEVE 68 (aux = the parcel) / RECIEVE_PUTAWAY 69
  R4 each closing row came after its hand-over (the job row was locked until then); both talks ended
  R5 the host relayed both shop hand-overs to B (1 peer each); B's keeper played them (D: steps GET 3 -> GET_PULL 4 -> GET_PUTAWAY 5 -> done; T: TRANS_WAIT 2 -> done)
  R6 B shows A's puppet in 70 / 71 and 67 / 68 / 69 (ACTION_EXT rows)
  R7 no REJECTED / protocol violation; everybody alive
  screenshots: handover_A_give.png / handover_A_nook.png (A's view), handover_B_observer.png (B's view)
Fixture: pc\\build64\\bin_fixture4_ho_real (fresh copy of bin_fixture4 + the exe of pc\\build64_w3). Ports 39500-39999.
Usage: python test_handover_real.py [--port 39600]
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

FIXTURE = "bin_fixture4_ho_real"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)
DONE_RE = r"\[NET\]\[HANDOVER\] nook work: hand-over 1/1 item 0x[0-9A-F]{4} (?:done|abandoned)"
END_RE = r"\[NET\]\[HOUSE\] nook dialogue: the conversation ended"


def mash(c, auto, n, kind, secs):
    with open(auto, "w") as f:
        f.write("%d %s %.2f\n" % (n, kind, secs))
    end = time.monotonic() + secs + 15
    while time.monotonic() < end:
        if re.search(r"\[AUTO\] (?:done %d |mash %d end)" % (n, n), c.log_text()):
            return True
        time.sleep(0.2)
    return False


def count(c, pattern):
    return len(re.findall(pattern, c.log_text()))


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=39600)
    args = ap.parse_args()
    if not (39500 <= args.port <= 39990):
        print("REFUSING: ports 39500-39999 only", file=sys.stderr)
        return 2
    T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
    import net_spike_lib as L

    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    auto = os.path.join(L.GAME_BIN_DIR, "ac_auto.txt")
    auto_b = os.path.join(L.GAME_BIN_DIR, "ac_auto_b.txt")
    for p in (auto, auto_b):
        if os.path.isfile(p):
            os.remove(p)
    dump_a = os.path.join(L.GAME_BIN_DIR, "ho_dump_a")
    dump_b = os.path.join(L.GAME_BIN_DIR, "ho_dump_b")
    h = a = b = None
    t0 = time.time()

    def talk(tag, op, base, pattern_trigger, shot_a):
        """one Nook conversation with mashw up to the hand-over's end, then mashc to the end of the talk; returns (did the hand-over finish, screenshot)"""
        n_done0, n_end0 = count(a, DONE_RE), count(a, END_RE)
        shot = False
        for k in range(1, 60):
            if count(a, DONE_RE) > n_done0:
                break
            if not shot and count(a, pattern_trigger) > 0:
                shot = frame_dump(a, dump_a, shot_a, os.path.join(HERE, "handover_A_%s.png" % shot_a))
                if tag == "T":
                    frame_dump(b, dump_b, "obs", os.path.join(HERE, "handover_B_observer.png"))
                continue
            mash(a, auto, base + k, "mashw", 0.6)
            time.sleep(0.2)
        ok = a.wait_for_log(DONE_RE, 30.0) is not None and count(a, DONE_RE) > n_done0
        time.sleep(1.0)
        for k in range(1, 8):
            if count(a, END_RE) > n_end0:
                break
            mash(a, auto, base + 100 + k, "mashc", 2.0)
        ended = count(a, END_RE) > n_end0
        ck("%s the talk ended after the hand-over" % tag, ended)
        time.sleep(3.0)
        return ok, shot

    try:
        h = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0"], log_path=T.log_path("ho_real_host.log"), bin_dir=L.GAME_BIN_DIR,
                          env=dict(os.environ, AC_TEST_CLEAR_ERRANDS="1", AC_TEST_WORK_TYPE="1,3")).start()
        ok = h.wait_listening(60.0) and h.boot_to_field(timeout=150.0, slot=0)
        ck("listen host reached the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        envb = dict(os.environ, AC_TEST_CLEAR_ERRANDS="1", PC_PUPPET_DIAG="1", AC_TEST_FRAME_DUMP=dump_b, AC_TEST_AUTOPILOT=auto_b)
        b = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "2", "--nook-test", "warp,resident"], log_path=T.log_path("ho_real_B.log"),
                            bin_dir=L.GAME_BIN_DIR, label="hoB", env=envb).start()
        ok = b.boot_to_field(timeout=180.0, slot=2)
        ck("observer B booted and connected", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        ck("B warped into Nook's shop", b.wait_for_log(r"--nook-test: warping the guest into the shop", 120.0) is not None)
        # B's Nook greets B on entry ("Welcome!"): single A presses until no demo runs (a 0.1 s mash is a press-less probe: its end line reports the demo state)
        time.sleep(6.0)
        idle = False
        for k in range(1, 16):
            mash(b, auto_b, 50 + 2 * k, "mash", 0.1)
            m = re.findall(r"\[AUTO\] mash %d end: player main=(\d+) demo=(\d+)" % (50 + 2 * k), b.log_text())
            if m and m[-1][1] == "0":
                idle = True
                break
            mash(b, auto_b, 51 + 2 * k, "mash", 0.35)
            time.sleep(1.5)
        ck("B read Nook's welcome away (no demo running on B)", idle)
        enva = dict(os.environ, AC_TEST_SELL_BASKET="2", AC_TEST_HANDOVER_FRUIT="1", AC_TEST_AUTOPILOT="1", AC_TEST_CLEAR_ERRANDS="1", PC_PUPPET_DIAG="1",
                    AC_TEST_FRAME_DUMP=dump_a)
        a = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "--nook-test", "warp,resident"], log_path=T.log_path("ho_real_A.log"),
                            bin_dir=L.GAME_BIN_DIR, label="hoA", env=enva).start()
        ok = a.boot_to_field(timeout=180.0, slot=1)
        ck("worker A booted and connected", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        ok = (a.wait_for_log(r"--nook-test: warping the guest into the shop", 120.0) is not None
              and a.wait_for_log(r"basket test \(nook\): Nook 0x[0-9A-F]{4} at .* the player placed at", 120.0) is not None)
        ck("A warped into the shop and was placed in front of Nook", ok)
        mf = a.wait_for_log(r"\[HANDOVER\]\[TEST-ONLY\] the town fruit 0x([0-9A-F]{4}) (?:put into local pocket \d+|is already in pocket \d+)", 60.0)
        ck("A holds the town fruit (%s)" % (mf and mf.group(0)), mf is not None)
        if not ok or mf is None:
            return L.summary_and_exit_code(results)
        fruit = mf.group(1)
        time.sleep(8.0)  # the D3 upload carries the test writes to the host
        # ---- talk 1: DELIVER (player -> Nook)
        d_ok, d_shot = talk("D", 2, 100, r"\[NET\]\[AEXT\] send idx=71 ", "give")
        # ---- talk 2: TAKE_PARCEL (Nook -> player)
        t_ok, t_shot = talk("T", 4, 400, r"\[NET\]\[AEXT\] send idx=67 ", "nook")
        ck("R0 screenshots of A's view during both hand-overs saved (handover_A_give.png / handover_A_nook.png)", d_shot and t_shot)
        time.sleep(2.0)
        at, ht, bt = a.log_text(), h.log_text(), b.log_text()
        # ---- R1
        ck("R1 the job row choices were 'Here it is' (op 2) and 'take the parcel' (op 4)",
           re.search(r"\[NET\]\[WORK\] nook dialogue: job row choice \d+ -> action \d+ \(op 2\)", at) and re.search(r"\[NET\]\[WORK\] nook dialogue: job row choice \d+ -> action \d+ \(op 4\)", at))
        cs = dict((o, len(re.findall(r"\[NET\]\[WORK\] host: peer \d+ resident \d+ WORK op %d committed" % o, ht))) for o in (1, 2, 4))
        ck("R1 the host committed ENTER x2, DELIVER x1, TAKE_PARCEL x1 (%s)" % cs, cs == {1: 2, 2: 1, 4: 1})
        # ---- R2
        mpd = re.search(r"\[NET\]\[HANDOVER\] nook work: op 2 -> player -> Nook item 0x([0-9A-F]{4})", at)
        mpt = re.search(r"\[NET\]\[HANDOVER\] nook work: op 4 -> Nook -> player item 0x([0-9A-F]{4})", at)
        parcel = mpt.group(1) if mpt else "XXXX"
        ck("R2 D: the hand-over was planned for the town fruit 0x%s (%s)" % (fruit, mpd and mpd.group(1)), mpd is not None and mpd.group(1) == fruit)
        ck("R2 T: the hand-over was planned for the parcel 0x%s" % parcel, mpt is not None)
        ck("R2 both were reported (103: shop keeper NPC_TAKES the fruit / NPC_GIVES the parcel)",
           re.search(r"local: shop keeper NPC_TAKES npc 0x[0-9A-F]{4} slot -1 item 0x%s" % fruit, at) and re.search(r"local: shop keeper NPC_GIVES npc 0x[0-9A-F]{4} slot -1 item 0x%s" % parcel, at))
        dn = re.findall(r"\[NET\]\[HANDOVER\] nook work: hand-over 1/1 item 0x([0-9A-F]{4}) (done|abandoned: .*)", at)
        ck("R2 both hand-overs finished (%s)" % dn, d_ok and t_ok and (fruit, "done") in dn and (parcel, "done") in dn)
        # ---- R3
        sends = [(int(x), int(y)) for x, y in re.findall(r"\[NET\]\[AEXT\] send idx=(6[7-9]|7[01]) ctr=\d+ .*? aux=(\d+) seq=", at)]
        idxs = [i for i, _x in sends]
        ck("R3 D: A's REAL player ran GIVE 70 with the fruit and GIVE_WAIT 71 (%s)" % sends, (70, int(fruit, 16)) in sends and 71 in idxs)
        ck("R3 T: A's REAL player ran RECIEVE_STRETCH 67 / RECIEVE 68 with the parcel and RECIEVE_PUTAWAY 69",
           mpt is not None and (67, int(parcel, 16)) in sends and (68, int(parcel, 16)) in sends and 69 in idxs)
        # ---- R4
        ends = [mm.start() for mm in re.finditer(END_RE, at)]
        dones = [mm.start() for mm in re.finditer(DONE_RE, at)]
        ck("R4 each conversation ended after its hand-over (%s / %s)" % (dones, ends), len(ends) >= 2 and len(dones) == 2 and dones[0] < ends[0] < dones[1] < ends[1])
        # ---- R5
        rel = re.findall(r"\[NET\]\[HANDOVER\] host: shop hand-over from (\d+) \((NPC_\w+) item 0x([0-9A-F]{4}) keeper 0x[0-9A-F]{4} scene \d+\) -> (\d+) peer", ht)
        ck("R5 the host relayed both shop hand-overs to B (1 peer each): %s" % rel, len(rel) == 2 and all(r[3] == "1" for r in rel))
        steps = [int(x) for x in re.findall(r"\[NET\]\[HANDOVER\] shop keeper 0x[0-9A-F]{4} clip step (\d)", bt)]
        ck("R5 B's keeper played them: NPC_TAKES (GET_PULL 4, GET_PUTAWAY 5, done 6) then NPC_GIVES (TRANS_WAIT 2, done 6): %s" % steps,
           count(b, r"\[NET\]\[HANDOVER\] client: shop keeper 0x[0-9A-F]{4} plays NPC_TAKES") == 1 and count(b, r"client: shop keeper 0x[0-9A-F]{4} plays NPC_GIVES") == 1
           and steps == [4, 5, 6, 2, 6])
        # ---- R6
        a_id = rel[0][0] if rel else "X"
        rows = set(re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player %s action_ext rx main_index=(6[6-9]|7[01])" % a_id, bt))
        ck("R6 B received A's hand-over clips for its puppet (ACTION_EXT rx %s)" % sorted(rows), {"67", "68", "70", "71"} <= rows)
        st = set(re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player %s state main_index=(6[7-9]|7[01]) counter=\d+ row=action_ext" % a_id, bt))
        ck("R6 B's puppet of A played them as dynamic rows (%s)" % sorted(st), {"67", "68", "70"} <= st)
        # ---- R7
        ck("R7 no REJECTED / protocol violation on A, B or the host", "REJECTED(" not in at and "protocol violation" not in at + bt + ht)
        ck("R7 host, A and B alive", h.alive() and a.alive() and b.alive())
    finally:
        for p in (auto, auto_b):
            if os.path.isfile(p):
                os.remove(p)
        for p in (a, b, h):
            if p is not None:
                p.stop()
        print("elapsed %.0f s" % (time.time() - t0))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
