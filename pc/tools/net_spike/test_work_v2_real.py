#!/usr/bin/env python3
"""test_work_v2_real.py - Nook Work Mode v2 with REAL game processes: the real Nook dialogue + the real villager talk, screenshots of every new step.

TIER: REAL listen HOST (resident 0) + ONE REAL resident client A (resident 1), restarted between Nook's shop and the town (5 client sessions; the job lives on the host).
Samsung display, volume 1. host: AC_TEST_WORK_TYPE=2,3,3 (fetch from a villager, then two deliveries), AC_TEST_WORK_URGENT=1 (Nook calls the first job urgent).
  S1 shop:  "Give me a job" -> the FETCH assignment row; "Check job" -> "Did you bring <item> from <villager>?" No. / Quit job -> No. -> "this one is urgent" + the bonus
  T1 town:  ac_goto "w" parks A in front of the job's villager; the talk: "Tom Nook sent you? Here you go: <item>!" WHILE the villager hands it over (the fix of the
            hand-over order), then the goodbye row
  S2 shop:  "Check job" -> Here it is! / No. / Quit job -> Here it is! -> Nook takes it while he thanks, the reward; "Give me a job" -> a DELIVERY: Nook hands the item over
            while he names it and the villager, then the pay row
  T2 town:  the villager: "<item> from Tom Nook!" WHILE the player hands it over -> the tip + Nook's pay row (job done, no return to Nook), the chat notice
  S3 shop:  "Give me a job" (another delivery); "Check job" -> No. / Quit job -> Quit job -> Nook takes the item back while he says so; the item is gone from the pockets
Asserts (host log + A's logs): the ops committed (ENTER x3, URGE x1, VILLAGER_GIVE x1, DELIVER x1, TAKE_PARCEL x2, VILLAGER_COMPLETE x1, QUIT x1), each hand-over started only
after its row was on screen and finished, the payout = reward + tip, the quit removed the item; no REJECTED / protocol violation; everybody alive.
Screenshots: pc/tools/net_spike/work_v2_shots/wv2_*.png (A's own frame buffer, AC_TEST_FRAME_DUMP).
Fixture: pc\\build64\\bin_fixture4_wv2_real (fresh copy of bin_fixture4 + the exe of pc\\build64_w3). Ports 39500-39999.
Usage: python test_work_v2_real.py [--port 39700] [--only S1,T1,...]
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

FIXTURE = "bin_fixture4_wv2_real"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)
SHOTS = os.path.join(HERE, "work_v2_shots")
END_RE = r"\[NET\]\[HOUSE\] nook dialogue: the conversation ended"


class Rig:
    def __init__(self, L, port, results):
        self.L, self.port, self.results = L, port, results
        self.auto = os.path.join(L.GAME_BIN_DIR, "ac_auto.txt")
        self.goto = os.path.join(L.GAME_BIN_DIR, "ac_goto.txt")
        self.dump = os.path.join(L.GAME_BIN_DIR, "wv2_dump")
        self.a = None
        self.n = 1000
        self.shots = []

    def ck(self, d, c):
        self.L.check(d, bool(c), self.results)
        return bool(c)

    def count(self, pattern):
        return len(re.findall(pattern, self.a.log_text()))

    def mash(self, kind, secs):
        self.n += 1
        n = self.n
        with open(self.auto, "w") as f:
            f.write("%d %s %.2f\n" % (n, kind, secs))
        end = time.monotonic() + secs + 15
        while time.monotonic() < end:
            if re.search(r"\[AUTO\] (?:done %d |mash %d end)" % (n, n), self.a.log_text()):
                return True
            time.sleep(0.15)
        return False

    def drive(self, kind, pattern, tries=60, step=0.6):
        """mash `kind` in short bursts until `pattern` appears one more time than now"""
        n0 = self.count(pattern)
        for _k in range(tries):
            if self.count(pattern) > n0:
                return True
            self.mash(kind, step)
            time.sleep(0.15)
        return self.count(pattern) > n0

    def shot(self, name, what):
        bmp = "%s_%s.bmp" % (self.dump, name)
        if os.path.isfile(bmp):
            os.remove(bmp)
        with open(self.dump + ".req", "w") as f:
            f.write(name + "\n")
        ok = self.a.wait_for_log(r"\[TEST-ONLY\] frame dump \d+x\d+ -> .*%s\.bmp" % re.escape("_" + name), 15.0) is not None
        png = os.path.join(SHOTS, name + ".png")
        if ok:
            try:
                from PIL import Image
                time.sleep(0.3)
                Image.open(bmp).save(png)
            except Exception as e:  # noqa: BLE001
                print("[INFO] frame dump conversion failed: %s" % e)
                ok = False
        self.shots.append((name, what, ok))
        print("[SHOT] %s %s: %s" % (name, "saved" if ok else "FAILED", what))
        return ok

    def end_talk(self, tries=24):
        """single A presses until Nook's conversation ended (the shop actor logs it)"""
        n0 = self.count(END_RE)
        for _k in range(tries):
            if self.count(END_RE) > n0:
                time.sleep(1.0)
                return True
            self.mash("mash", 0.5)
            time.sleep(0.35)
        return self.count(END_RE) > n0

    def start_a(self, tag, shop):
        L = self.L
        for p in (self.auto, self.goto):
            if os.path.isfile(p):
                os.remove(p)
        env = dict(os.environ, AC_TEST_AUTOPILOT="1", AC_TEST_CLEAR_ERRANDS="1", AC_TEST_FRAME_DUMP=self.dump, PC_PUPPET_DIAG="1")
        extra = ["--bootstrap-resident", "1"]
        if shop:
            env["AC_TEST_SELL_BASKET"] = "3"  # only the placement in front of Nook (no pocket writes)
            extra += ["--nook-test", "warp,resident"]
        else:
            env["AC_TEST_GOTO"] = "1"
        self.a = L.ClientProcess("127.0.0.1:%d" % self.port, extra_args=extra, log_path=T.log_path("wv2_real_A_%s.log" % tag), bin_dir=L.GAME_BIN_DIR, label="wv2A", env=env).start()
        ok = self.a.boot_to_field(timeout=180.0, slot=1)
        self.ck("%s: A booted and connected" % tag, ok)
        if not ok:
            return False
        ok = self.a.wait_for_log(r"\[NET\]\[WORK\] client: the host's Work Mode v2 is ON", 60.0) is not None
        self.ck("%s: A got the host's Work v2 capability (service 11)" % tag, ok)
        if shop:
            ok = (self.a.wait_for_log(r"--nook-test: warping the guest into the shop", 120.0) is not None
                  and self.a.wait_for_log(r"basket test \(nook\): Nook 0x[0-9A-F]{4} at .* the player placed at", 120.0) is not None)
            self.ck("%s: A warped into Nook's shop, in front of Nook" % tag, ok)
            time.sleep(8.0)
        else:
            time.sleep(6.0)
        return ok

    def stop_a(self):
        if self.a is not None:
            self.a.stop()
            self.a = None
        time.sleep(3.0)

    def ap(self, cmd, rx, timeout=20.0):
        self.n += 1
        off = len(self.a.log_text())
        with open(self.auto, "w") as f:
            f.write("%d %s\n" % (self.n, cmd))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = re.search(rx % self.n, self.a.log_text()[off:])
            if m:
                return m
            time.sleep(0.2)
        return None

    def scout_villager(self):
        """T0: the villager actor closest to A's town spawn; the host's next errands name it (AC_TEST_WORK_VILLAGER + ac_work_villager.txt)"""
        pm = self.ap("ppos", r"\[AUTO\] ppos %d x=([-\d.]+) y=([-\d.]+) z=([-\d.]+)")
        nm = self.ap("npcpos", r"\[AUTO\] npcpos %d([^\r\n]*)")
        if pm is None or nm is None:
            return None
        px, pz = float(pm.group(1)), float(pm.group(3))
        best = None
        for x in re.finditer(r"(\d+):(-?[\d.]+):(-?[\d.]+):(-?[\d.]+)", nm.group(1)):
            d = (float(x.group(2)) - px) ** 2 + (float(x.group(4)) - pz) ** 2
            if best is None or d < best[1]:
                best = (int(x.group(1)), d)
        print("[INFO] T0 player (%.0f, %.0f), villagers %s -> %s" % (px, pz, nm.group(1).strip(), best))
        return best[0] if best else None

    def goto_villager(self, tries=14):
        for _k in range(tries):
            n0 = self.count(r"goto v \d+: player parked")
            with open(self.goto, "w") as f:
                f.write("w 0\n")
            t0 = time.monotonic()
            while time.monotonic() < t0 + 5.0:
                if self.count(r"goto v \d+: player parked") > n0:
                    return True
                time.sleep(0.25)
            time.sleep(3.0)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=39700)
    args = ap.parse_args()
    if not (39500 <= args.port <= 39990):
        print("REFUSING: ports 39500-39999 only", file=sys.stderr)
        return 2
    T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
    os.makedirs(SHOTS, exist_ok=True)
    import net_spike_lib as L

    results = []
    rig = Rig(L, args.port, results)
    ck = rig.ck
    h = None
    t0 = time.time()
    try:
        h = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0"], log_path=T.log_path("wv2_real_host.log"), bin_dir=L.GAME_BIN_DIR,
                          env=dict(os.environ, AC_TEST_CLEAR_ERRANDS="1", AC_TEST_WORK_TYPE="2,3,3", AC_TEST_WORK_URGENT="1", AC_TEST_WORK_POOL_DUMP="1", AC_TEST_WORK_VILLAGER="1")).start()
        ok = h.wait_listening(60.0) and h.boot_to_field(timeout=150.0, slot=0)
        ck("listen host reached the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)

        # ---------------- T0: which villager is around A's town spawn (the errands will name it)
        if rig.start_a("T0", False):
            vs = rig.scout_villager()
            ck("T0 a villager actor is loaded around A's spawn (slot %s)" % vs, vs is not None)
            if vs is not None:
                with open(os.path.join(L.GAME_BIN_DIR, "ac_work_villager.txt"), "w") as f:
                    f.write("%d\n" % vs)
            rig.stop_a()

        # ---------------- S1: Give me a job (FETCH) / Check job / No. -> urgent
        if rig.start_a("S1", True):
            ok = rig.drive("mashj0", r"\[NET\]\[WORK\] nook dialogue v2: row 1\b")
            ck("S1 'Give me a job' -> ENTER -> the assignment row", ok)
            time.sleep(2.5)
            rig.shot("wv2_01_assign_fetch", "S1 Nook assigns the fetch job (Give me a job)")
            ck("S1 the talk ended", rig.end_talk())
            time.sleep(1.5)
            ok = rig.drive("mashj0h", r"\[AUTO\] mashj \d+ choice window: n=2 sel=0 msg=0x[0-9A-F]+ first=0")
            ck("S1 'Check job' -> the Check job row with 2 choices (No. / Quit job)", ok and rig.count(r"nook dialogue v2: Check job: job \d+ type 2 step 0") >= 1)
            time.sleep(1.2)
            rig.shot("wv2_02_check_job", "S1 Check job: did you bring <item> from <villager>? No. / Quit job")
            ok = rig.drive("mashj0", r"nook dialogue v2: the job is urgent: \+\d+ Bells, \d+ in all", tries=20)
            ck("S1 No. -> URGE -> the host says urgent (+bonus)", ok)
            time.sleep(2.0)
            rig.shot("wv2_03_urgent", "S1 No. -> 'this one is urgent' (page 1)")
            rig.mash("mash", 0.3)
            time.sleep(2.0)
            rig.shot("wv2_04_urgent_bonus", "S1 the urgent bonus and the new total (page 2)")
            ck("S1 the talk ended", rig.end_talk())
            rig.stop_a()

        # ---------------- T1: the villager gives the fetched item WHILE it says so
        if rig.start_a("T1", False):
            ok = rig.goto_villager()
            ck("T1 A is parked in front of the job's villager", ok)
            if ok:
                ok = rig.drive("mash", r"\[NET\]\[HANDOVER\] work villager talk: npc 0x[0-9A-F]{4} row 1 is on screen: the hand-over starts while it is read", tries=40, step=0.5)
                ck("T1 the villager talk: VILLAGER_GIVE applied, row 1 on screen, THEN the hand-over starts", ok)
                time.sleep(1.1)
                rig.shot("wv2_05_villager_gives", "T1 'Tom Nook sent you? Here you go: <item>!' while the villager hands it over")
                ok = rig.a.wait_for_log(r"\[NET\]\[HANDOVER\] work villager talk: npc 0x[0-9A-F]{4} hand-over 1/1 done", 30.0) is not None
                ck("T1 the hand-over finished", ok)
                time.sleep(0.8)
                rig.mash("mash", 0.3)
                time.sleep(2.0)
                rig.shot("wv2_05b_villager_bye", "T1 row 2 after the hand-over")
                rig.mash("mash", 3.0)
            rig.stop_a()

        # ---------------- S2: Here it is! (DELIVER) + Give me a job (DELIVER_VILLAGER, the parcel handed over while Nook names it)
        if rig.start_a("S2", True):
            ok = rig.drive("mashj0h", r"\[AUTO\] mashj \d+ choice window: n=3 sel=0 msg=0x[0-9A-F]+ first=0")
            ck("S2 'Check job' while carrying the fetched item: 3 choices (Here it is! / No. / Quit job)", ok and rig.count(r"nook dialogue v2: Check job: job \d+ type 2 step 1 .* has_item 1") >= 1)
            time.sleep(1.2)
            rig.shot("wv2_06_check_carrying", "S2 Check job with the item: Here it is! / No. / Quit job")
            ok = rig.drive("mashj0", r"\[NET\]\[HANDOVER\] nook work v2: op 2 -> player -> Nook item", tries=20)
            ck("S2 Here it is! -> DELIVER -> the player hands it to Nook while he thanks", ok)
            time.sleep(0.9)
            rig.shot("wv2_07_hand_in", "S2 'Just what I needed' while the player hands the item over")
            ck("S2 the talk ended", rig.end_talk())
            time.sleep(1.5)
            ok = rig.drive("mashj0", r"\[NET\]\[HANDOVER\] nook work v2: op 4 -> Nook -> player item")
            ck("S2 'Give me a job' -> a delivery: Nook hands the item over while he names it", ok)
            time.sleep(0.9)
            rig.shot("wv2_08_parcel_given", "S2 the delivery: 'Here you are: <item>' while Nook hands it over")
            ck("S2 the talk ended", rig.end_talk())
            rig.stop_a()

        # ---------------- T2: auto-complete at the villager
        if rig.start_a("T2", False):
            ok = rig.goto_villager()
            ck("T2 A is parked in front of the delivery's villager", ok)
            if ok:
                ok = rig.drive("mash", r"row 1 is on screen: the hand-over starts while it is read", tries=40, step=0.5)
                ck("T2 VILLAGER_COMPLETE applied, row 1 on screen, then the player's hand-over", ok)
                time.sleep(1.1)
                rig.shot("wv2_09_villager_receives", "T2 '<item> from Tom Nook!' while the player hands it over")
                ok = rig.a.wait_for_log(r"\[NET\]\[HANDOVER\] work villager talk: npc 0x[0-9A-F]{4} hand-over 1/1 done", 30.0) is not None
                ck("T2 the hand-over finished", ok)
                time.sleep(0.8)
                rig.mash("mash", 0.3)
                time.sleep(2.0)
                rig.shot("wv2_10_tip", "T2 the villager's tip")
                rig.mash("mash", 0.3)
                time.sleep(2.0)
                rig.shot("wv2_11_payout", "T2 Nook's pay at the villager, job done (+ the chat notice)")
                rig.mash("mash", 3.0)
            rig.stop_a()

        # ---------------- S3: Quit job -> the item goes back
        if rig.start_a("S3", True):
            ok = rig.drive("mashj0", r"\[NET\]\[HANDOVER\] nook work v2: op 4 -> Nook -> player item")
            ck("S3 'Give me a job' -> another delivery handed over", ok)
            ck("S3 the talk ended", rig.end_talk())
            time.sleep(1.5)
            ok = rig.drive("mashj1h", r"\[AUTO\] mashj \d+ choice window: n=2 sel=1 msg=0x[0-9A-F]+ first=0")
            ck("S3 'Check job' on the way: No. / Quit job", ok and rig.count(r"nook dialogue v2: Check job: job \d+ type 3 step 1") >= 1)
            time.sleep(1.2)
            rig.shot("wv2_12_check_delivery", "S3 Check job: did you deliver <item> to <villager>? No. / Quit job")
            ok = rig.drive("mashj1", r"\[NET\]\[HANDOVER\] nook work v2: op 9 -> player -> Nook item", tries=20)
            ck("S3 Quit job -> QUIT -> Nook takes the item back while he says so", ok)
            time.sleep(0.9)
            rig.shot("wv2_13_quit_takeback", "S3 Quit job: 'Then I'll take back <item>' while the item goes back")
            ck("S3 the talk ended", rig.end_talk())
            rig.stop_a()

        # ---------------- the host's view
        ht = h.log_text()
        cs = dict((o, len(re.findall(r"\[NET\]\[WORK\] host: peer \d+ resident \d+ WORK op %d committed" % o, ht))) for o in (1, 2, 4, 5, 8, 9, 10))
        ck("R1 the host committed ENTER x3, DELIVER x1, TAKE_PARCEL x2, VILLAGER_GIVE x1, URGE x1, QUIT x1, VILLAGER_COMPLETE x1 (%s)" % cs,
           cs == {1: 3, 2: 1, 4: 2, 5: 1, 8: 1, 9: 1, 10: 1})
        mm = re.search(r"job (\d+) completed at villager 0x[0-9A-F]{4}: reward (\d+) \+ tip (\d+) \(friendship (-?\d+)\) = (\d+) paid", ht)
        ck("R2 the auto-complete paid reward + tip in one step (%s)" % (mm and mm.group(0)), mm is not None and int(mm.group(2)) + int(mm.group(3)) == int(mm.group(5)) and 50 <= int(mm.group(3)) <= 550)
        ck("R3 the quit returned the item in the same step", re.search(r"job \d+ quit: its item went back in the same step", ht) is not None)
        ck("R4 every v2 job item came from the varied pools (names logged)", len(re.findall(r"host: v2 job item 0x[0-9A-F]{4} = \"[^\"]+\"", ht)) == 3)
        alltxt = ht + "".join(open(T.log_path("wv2_real_A_%s.log" % t), encoding="utf-8", errors="replace").read() for t in ("S1", "T1", "S2", "T2", "S3")
                              if os.path.isfile(T.log_path("wv2_real_A_%s.log" % t)))
        ck("R5 A's sessions: the notice of the payout was shown", "[NET][WORK] notice: Delivery done!" in alltxt)
        ck("R6 no protocol violation anywhere", "protocol violation" not in alltxt)
        ck("R7 the host is alive", h.alive())
    finally:
        rig.stop_a()
        for p in (rig.auto, rig.goto):
            if os.path.isfile(p):
                os.remove(p)
        if h is not None:
            h.stop()
        print("elapsed %.0f s" % (time.time() - t0))
        for name, what, ok in rig.shots:
            print("SHOT %-28s %s %s" % (name, "ok " if ok else "MISSING", what))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
