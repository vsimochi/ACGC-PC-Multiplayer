#!/usr/bin/env python3
"""test_guest_mailbox_real.py - the GUEST MAILBOX with a REAL `--guest` game client and a REAL host (autopilot-driven post office dialogue).

TIER: REAL HOST + REAL GUEST CLIENT game processes on a DISPOSABLE fixture pc\\build64\\bin_fixture4_gmailr (fresh copy of bin_fixture4 with the pc\\build64_w3 exe;
port 12900; both windows on the Samsung monitor, volume 1):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`, env AC_TEST_GUEST_MAIL_SEED=3,2800 (TEST-ONLY: 3 villager-style letters, the first with an apple,
           delivered into the guest's host-held mailbox through the real delivery function once its record is synced)
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --guest` (guest profile save/mp/guest.ini written by this test), env AC_TEST_MUSEUM_ROOM=postoffice (TEST-ONLY warp into
           the post office with the vanilla door data), AC_TEST_GOTO (parked in front of the post girl: ac_goto.txt `g <dist>`), AC_TEST_AUTOPILOT (the `mashm` command: A presses,
           a choice takes "Check my mail." when the row carries it, else its cancel option; no A while a submenu is open; B closes the overlay once the copy is empty)
What runs for real: the MAILBOX_LETTER copy (house 0xFF) into the client's guest copy, the post girl's talk with the patched greeting row, her new action that opens the VANILLA
mailbox overlay on pc_ui_mailbox() (the guest copy), the overlay's own automatic transfer (one MAIL_TAKE per letter through the host), the closing row.
  R1  the guest client is a guest and its GUEST mailbox became host-fed (3 letters)
  R2  warp into the post office, parked in front of the post girl
  R3  the greeting row got 'Check my mail.' and the autopilot chose it; the post girl saw 3 letters waiting; the overlay opened
  R4  the overlay's transfer took the 3 letters: 3 MAIL_TAKE APPLIED on the client, 3 guest commits on the host (guest file written), the copy is empty afterwards
  R5  the overlay closed, the closing row (PC_NOOK_MSG_POST_DONE) was loaded, the client is alive, nothing inconsistent
  R6  the host's guest file: the mailbox is empty again (written back as v3), the record's mail[] holds the 3 letters (one with the apple)
Usage: python test_guest_mailbox_real.py [--port 12900] [--dist 40]
"""
import argparse
import glob
import os
import re
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as TT  # noqa: E402

FIXTURE = "bin_fixture4_gmailr"
W3_EXE = os.path.join(TT.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(TT.BUILD64, FIXTURE)
DISPLAY_ENV = {"AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1"}

import net_spike_lib as L  # noqa: E402
import test_guest_mailbox_protocol as GP  # noqa: E402  (the independent .gst parser)

PID, LID = 0x4D41, 0x5E41
APPLE = 0x2800


def wait_log(proc, pattern, timeout, flags=0):
    rx = re.compile(pattern, flags)
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        mo = rx.search(proc.log_text())
        if mo:
            return mo
        if not proc.alive():
            return None
        time.sleep(0.5)
    return None


def run(args, results):
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    dst = TT.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(dst, "AnimalCrossing.exe"))
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    ini = os.path.join(save_dir, "mp", "guest.ini")
    os.makedirs(os.path.dirname(ini), exist_ok=True)
    with open(ini, "wb") as f:
        f.write(("# test profile\nname = Postie\ngender = 1\nface = 3\nhome_town = Farvale\nplayer_id = 0x%04X\nland_id = %d\n" % (PID, LID)).encode("ascii"))
    auto = os.path.join(L.GAME_BIN_DIR, "ac_auto_gmail.txt")
    henv = dict(DISPLAY_ENV)
    henv["AC_TEST_GUEST_MAIL_SEED"] = "3,%04X" % APPLE
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT)], log_path=os.path.join(TT.LOG_DIR, "guest_mailbox_real_host.log"),
                         bin_dir=L.GAME_BIN_DIR, env=henv).start()
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=120.0, slot=L.TEST_HOST_RESIDENT)
    ck("host reached genuine field-ready state", ok)
    cl = None
    try:
        if not ok:
            return
        cenv = dict(DISPLAY_ENV)
        cenv.update({"AC_TEST_AUTOPILOT": "ac_auto_gmail.txt", "AC_TEST_MUSEUM_ROOM": "postoffice,4000", "AC_TEST_GOTO": "1"})
        cl = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--guest"], log_path=os.path.join(TT.LOG_DIR, "guest_mailbox_real_client.log"), bin_dir=L.GAME_BIN_DIR,
                             env=cenv, label="guest_mailbox").start()
        fed = wait_log(cl, r"the GUEST mailbox is now HOST-FED", 240.0)
        ck("R1 the guest client's GUEST mailbox became host-fed (MAILBOX_LETTER house 0xFF accepted)", fed is not None)
        ct = cl.log_text()
        ck("R1 the client is a guest (the guest arrival path ran, player_no 4)", re.search(r"guest init ran WITHOUT the gateway[^\n]*player_no = 4", ct) is not None)
        three = wait_log(cl, r"applied GUEST mailbox\[2\] seq \d+ digest 0x[0-9A-F]{8} \(LETTER", 60.0)
        ck("R1 the 3 seeded letters reached the guest copy (slot 2 LETTER applied)", three is not None)
        w = wait_log(cl, r"warping into museum room 7 \(scene (\d+)\)", 120.0)
        ck("R2 the guest warped into the post office (TEST-ONLY warp, vanilla door data)", w is not None)
        if w is None:
            return
        live = wait_log(cl, r"local scene live: scene=%s " % w.group(1), 60.0)
        ck("R2 the post office scene is live on the guest", live is not None)
        time.sleep(3.0)
        with open(os.path.join(L.GAME_BIN_DIR, "ac_goto.txt"), "w") as f:
            f.write("g %d\n" % args.dist)
        g = wait_log(cl, r"goto g: the post girl at \(([-0-9.]+), ([-0-9.]+)\)", 20.0)
        ck("R2 the guest was parked in front of the post girl (positioning aid)", g is not None)
        time.sleep(1.5)
        off = len(cl.log_text())
        offh = len(host.log_text())
        with open(auto, "w") as f:
            f.write("1 mashm 40\n")
        asked = wait_log(cl, r"post office: the guest asked for its mail \((\d+) letter\(s\) waiting\)", 60.0)
        ct = cl.log_text()[off:]
        ck("R3 the greeting row got 'Check my mail.' (patched choice list)", re.search(r"post office message 0x[0-9A-F]{4}: 'Check my mail\.' added as choice \d+", ct) is not None)
        ck("R3 the autopilot chose it and the post girl saw 3 letters waiting", asked is not None and asked.group(1) == "3")
        sub = wait_log(cl, r"\[AUTO\] mashm 1: a submenu opened \(guest mailbox copy holds (\d+) letter", 30.0)
        ck("R3 the mailbox overlay opened on the guest copy", sub is not None)
        closed = wait_log(cl, r"post office: the guest's mailbox overlay closed \((\d+) letter\(s\) left\)", 90.0)
        ct = cl.log_text()[off:]
        ht = host.log_text()[offh:]
        takes = re.findall(r"client: APPLIED request \d+ kind=MAIL_TAKE -- mailbox\[(\d+)\] letter \(present 0x([0-9A-F]{4})\) is now in mail slot (\d+)", ct)
        hcommits = re.findall(r"guest \d+ MAIL_TAKE mailbox\[(\d+)\] of house 255 -> mirror mail\[(\d+)\] present 0x([0-9A-F]{4}) letter_hash=0x[0-9A-F]{8} mailbox_slot_cleared=1 committed \[TXN\] \(guest file written\)", ht)
        print("   INFO client takes: %s / host commits: %s" % (takes, hcommits))
        ck("R4 the overlay's own transfer took all 3 letters: 3 MAIL_TAKE APPLIED on the client, 3 guest commits on the host (guest file written each time)",
           len(takes) == 3 and len(hcommits) == 3 and sorted(t[0] for t in takes) == ["0", "1", "2"])
        ck("R4 the apple travelled inside its letter (one take with present 0x%04X)" % APPLE, sum(1 for t in takes if int(t[1], 16) == APPLE) == 1)
        ck("R5 the overlay closed with the copy empty (0 letters left)", closed is not None and closed.group(1) == "0")
        time.sleep(4.0)
        ct = cl.log_text()[off:]
        ck("R5 the client is alive; no rejection / inconsistency / CHANGED BEFORE APPLIED", cl.alive() and "REJECTED(" not in ct and "inconsistent post-image" not in ct
           and "protocol violation" not in ct and "CHANGED BEFORE APPLIED" not in ct)
        ck("R5 no host INTERNAL error / CONFLICT", "*** INTERNAL" not in ht and "CONFLICT" not in ht)
        end = wait_log(cl, r"\[AUTO\] mashm 1 end: player main=(\d+) demo=(\d+) submenu=(\d+)", 60.0)
        ck("R5 after the closing row the conversation is OVER (autopilot end: no demo / talk running, no submenu open)", end is not None and end.group(2) == "0" and end.group(3) == "0")

        def file_ok():
            for fn in glob.glob(os.path.join(save_dir, "mp", "guests", "*.gst")):
                try:
                    with open(fn, "rb") as f:
                        p = GP.parse_gst(f.read())
                except Exception:  # noqa: BLE001
                    return False
                mails = [L.record_mail(p["record"], i) for i in range(L.REC_MAIL_COUNT)]
                used = [m for m in mails if L.mail_font(m) != L.MAIL_FONT_UNUSED]
                if p["ver"] == 3 and p["mask"] == 0 and len(used) == 3 and sum(1 for m in used if L.mail_present(m) == APPLE) == 1:
                    return True
            return False
        t0 = time.monotonic()
        good = file_ok()
        while not good and time.monotonic() - t0 < 30.0:
            time.sleep(1.0)
            good = file_ok()
        ck("R6 the host's guest file: the mailbox is EMPTY (written back as v3) and the record's mail[] holds the 3 letters (one with the apple)", good)
    finally:
        if cl is not None:
            cl.stop()
        rc = host.stop()
        ck("host did not crash (exit code before stop: %s)" % rc, rc is None)
        if os.path.exists(auto):
            os.remove(auto)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12900)
    ap.add_argument("--dist", type=int, default=40)
    args = ap.parse_args()
    if not os.path.isfile(W3_EXE):
        print("missing %s" % W3_EXE, file=sys.stderr)
        return 2
    results = []
    try:
        run(args, results)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("run raised %r" % exc, False, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
