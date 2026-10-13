#!/usr/bin/env python3
"""test_guest_letters_real.py - LETTERS TO GUESTS with a REAL `--guest` game client and a REAL host (+ one scripted resident).

TIER: REAL HOST + REAL GUEST CLIENT game processes on a DISPOSABLE fixture pc\\build64\\bin_fixture4_glettr (fresh copy of bin_fixture4 with the pc\\build64_w3 exe;
port 12920; both windows on the Samsung monitor, volume 1) and a scripted FakeClient RESIDENT:
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`, env AC_TEST_HOOKS=1 AC_TEST_GUEST_EVENT_MAIL=xmas,vtday,goodbye,museum (TEST-ONLY: once the guest is
           synced, one villager becomes its best friend and the REAL event passes run: the Christmas window, the Valentine pass, a noted departure, the museum counted complete)
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --guest` (guest profile save/mp/guest.ini), env AC_TEST_MUSEUM_ROOM=postoffice, AC_TEST_GOTO, AC_TEST_AUTOPILOT (`mashm`)
What runs for real: the GUEST_DIR service on a real client, the villager event / museum letters into the guest's host-held mailbox and onto the real client, a
RESIDENT's letter (with an apple) to the real guest through MAIL_SEND, then the post office: "Check my mail." and the vanilla mailbox overlay taking every letter.
  R1  the real guest client applied the guest directory (service 7) listing itself; the scripted resident received the same directory (the guest's key)
  R2  the event passes delivered 4 letters (Christmas, Valentine, goodbye, museum) and the real client's guest copy received them
  R3  the resident's letter to the guest (key from the directory) is APPLIED and reaches the real client at once (5 letters)
  R4  at the post office the overlay took all 5 letters: 5 MAIL_TAKE APPLIED on the client / 5 guest commits on the host; the apple and the museum model travelled inside
  R5  the conversation ended cleanly, no rejection / inconsistency, the host's guest file holds the 5 letters in mail[] and an empty mailbox
Usage: python test_guest_letters_real.py [--port 12920] [--dist 40]
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

FIXTURE = "bin_fixture4_glettr"
W3_EXE = os.path.join(TT.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(TT.BUILD64, FIXTURE)
DISPLAY_ENV = {"AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1"}

import net_spike_lib as L  # noqa: E402
import test_guest_protocol as TG  # noqa: E402
import test_guest_mailbox_protocol as GP  # noqa: E402  (the independent .gst parser)
from test_txn_protocol import APPLIED  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(TT.BUILD64, FIXTURE)
PID, LID = 0x4D52, 0x5E52
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


def be_key(le):
    return bytes(le[:16]) + struct.pack(">HH", *struct.unpack("<HH", le[16:20]))


def run(args, results):
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    dst = TT.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(dst, "AnimalCrossing.exe"))
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    ini = os.path.join(save_dir, "mp", "guest.ini")
    os.makedirs(os.path.dirname(ini), exist_ok=True)
    with open(ini, "wb") as f:
        f.write(("# test profile\nname = Penpal\ngender = 1\nface = 2\nhome_town = Farvale\nplayer_id = 0x%04X\nland_id = %d\n" % (PID, LID)).encode("ascii"))
    auto = os.path.join(L.GAME_BIN_DIR, "ac_auto_gletters.txt")
    henv = dict(DISPLAY_ENV)
    henv.update({"AC_TEST_HOOKS": "1", "AC_TEST_GUEST_EVENT_MAIL": "xmas,vtday,goodbye,museum"})
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(host_slot)], log_path=os.path.join(TT.LOG_DIR, "guest_letters_real_host.log"),
                         bin_dir=L.GAME_BIN_DIR, env=henv).start()
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=120.0, slot=host_slot)
    ck("host reached genuine field-ready state", ok)
    cl = None
    grun = None
    try:
        if not ok:
            return
        L.resolve_host_town("127.0.0.1", args.port)
        cenv = dict(DISPLAY_ENV)
        cenv.update({"AC_TEST_AUTOPILOT": "ac_auto_gletters.txt", "AC_TEST_MUSEUM_ROOM": "postoffice,4000", "AC_TEST_GOTO": "1"})
        cl = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--guest"], log_path=os.path.join(TT.LOG_DIR, "guest_letters_real_client.log"), bin_dir=L.GAME_BIN_DIR,
                             env=cenv, label="guest_letters").start()
        fed = wait_log(cl, r"the GUEST mailbox is now HOST-FED", 240.0)
        ck("R0 the real guest client's GUEST mailbox became host-fed", fed is not None)
        d = wait_log(cl, r"\[NET\]\[TS\] client: applied service 7 \(GUEST_DIR\) seq \d+ digest 0x[0-9A-F]{8} len 324: 1 guest\(s\) in the town's directory", 60.0)
        ck("R1 the real guest client applied the guest directory (service 7, 324 B) listing one guest (itself; its address book hides it)", d is not None)
        grun = TG.GRun("127.0.0.1", args.port, host, results, snap_gci, host_slot, residents[:3])
        R1 = grun.ready("R1", grun.r1)
        st = R1.wait_ts_state(L.PC_NETGAME_TS_GUESTDIR, timeout=10.0, pred=lambda b: b[0] >= 1)
        key = be_key(st[1][4:24]) if st is not None else None
        ck("R1 the scripted resident received the directory: one guest, named Penpal (home town Farvale)", st is not None and st[1][0] == 1 and key[:6] == b"Penpal" and key[8:15] == b"Farvale")
        ev = wait_log(host, r"museum completion letter delivered to guest slot (\d+)", 60.0)
        ht = host.log_text()
        ck("R2 the host's event passes delivered the Christmas card, the Valentine letter, the goodbye letter and the museum letter to the guest",
           ev is not None and "Christmas is in the window: 1 card for guest slot" in ht and re.search(r"Valentine's Day: 1 present letter", ht) is not None
           and "goodbye letter of villager" in ht)
        four = wait_log(cl, r"applied GUEST mailbox\[3\] seq \d+ digest 0x[0-9A-F]{8} \(LETTER", 60.0)
        ck("R2 the 4 letters reached the real client's guest copy (slot 3 LETTER applied)", four is not None)
        if key is None:
            return
        pid_r1 = L.record_from_gci(L.GAME_BIN_DIR, grun.r1)[:20]
        letter = L.build_mail_be(key, pid_r1, present=APPLE, body=b"Dear Penpal, an apple for you.")
        _s, r, _a = R1.txn_mail_send(0, letter, rid=grun.fresh_rid(), timeout=6.0)
        ck("R3 the resident's letter (with an apple) to the guest is APPLIED", r is not None and r.outcome == APPLIED)
        five = wait_log(cl, r"applied GUEST mailbox\[4\] seq \d+ digest 0x[0-9A-F]{8} \(LETTER", 30.0)
        ck("R3 ... and reached the real client at once (slot 4 LETTER applied)", five is not None)
        grun.release(R1)
        w = wait_log(cl, r"warping into museum room 7 \(scene (\d+)\)", 120.0)
        ck("R4 the guest warped into the post office (TEST-ONLY warp, vanilla door data)", w is not None)
        if w is None:
            return
        live = wait_log(cl, r"local scene live: scene=%s " % w.group(1), 60.0)
        ck("R4 the post office scene is live on the guest", live is not None)
        time.sleep(3.0)
        with open(os.path.join(L.GAME_BIN_DIR, "ac_goto.txt"), "w") as f:
            f.write("g %d\n" % args.dist)
        g = wait_log(cl, r"goto g: the post girl at \(([-0-9.]+), ([-0-9.]+)\)", 20.0)
        ck("R4 the guest was parked in front of the post girl", g is not None)
        time.sleep(1.5)
        off = len(cl.log_text())
        offh = len(host.log_text())
        with open(auto, "w") as f:
            f.write("1 mashm 50\n")
        asked = wait_log(cl, r"post office: the guest asked for its mail \((\d+) letter\(s\) waiting\)", 60.0)
        ck("R4 'Check my mail.' chosen: the post girl saw 5 letters waiting", asked is not None and asked.group(1) == "5")
        closed = wait_log(cl, r"post office: the guest's mailbox overlay closed \((\d+) letter\(s\) left\)", 120.0)
        ct = cl.log_text()[off:]
        ht = host.log_text()[offh:]
        takes = re.findall(r"client: APPLIED request \d+ kind=MAIL_TAKE -- mailbox\[(\d+)\] letter \(present 0x([0-9A-F]{4})\) is now in mail slot (\d+)", ct)
        hcommits = re.findall(r"guest \d+ MAIL_TAKE mailbox\[(\d+)\] of house 255 -> mirror mail\[(\d+)\] present 0x([0-9A-F]{4}) letter_hash=0x[0-9A-F]{8} mailbox_slot_cleared=1 committed", ht)
        print("   INFO client takes: %s / host commits: %s" % (takes, hcommits))
        ck("R4 the overlay took all 5 letters: 5 MAIL_TAKE APPLIED on the client, 5 guest commits on the host", len(takes) == 5 and len(hcommits) == 5)
        ck("R4 the apple travelled inside the resident's letter", sum(1 for t in takes if int(t[1], 16) == APPLE) == 1)
        ck("R5 the overlay closed with the copy empty (0 letters left)", closed is not None and closed.group(1) == "0")
        time.sleep(4.0)
        ct = cl.log_text()[off:]
        ck("R5 the client is alive; no rejection / inconsistency", cl.alive() and "REJECTED(" not in ct and "inconsistent post-image" not in ct and "protocol violation" not in ct)
        ck("R5 no host INTERNAL error / CONFLICT", "*** INTERNAL" not in host.log_text() and "CONFLICT" not in host.log_text()[offh:])
        end = wait_log(cl, r"\[AUTO\] mashm 1 end: player main=(\d+) demo=(\d+) submenu=(\d+)", 90.0)
        ck("R5 the conversation is over (no demo / submenu)", end is not None and end.group(2) == "0" and end.group(3) == "0")

        def file_ok():
            for fn in glob.glob(os.path.join(save_dir, "mp", "guests", "*.gst")):
                try:
                    with open(fn, "rb") as f:
                        p = GP.parse_gst(f.read())
                except Exception:  # noqa: BLE001
                    return False
                used = [m for m in (L.record_mail(p["record"], i) for i in range(L.REC_MAIL_COUNT)) if L.mail_font(m) != L.MAIL_FONT_UNUSED]
                if p["mask"] == 0 and len(used) == 5 and sum(1 for m in used if L.mail_present(m) == APPLE) == 1:
                    return True
            return False
        t0 = time.monotonic()
        good = file_ok()
        while not good and time.monotonic() - t0 < 30.0:
            time.sleep(1.0)
            good = file_ok()
        ck("R5 the host's guest file: the mailbox is empty, the record's mail[] holds the 5 letters (one with the apple)", good)
    finally:
        if grun is not None:
            for c in grun.clients:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
        if cl is not None:
            cl.stop()
        rc = host.stop()
        ck("host did not crash (exit code before stop: %s)" % rc, rc is None)
        if os.path.exists(auto):
            os.remove(auto)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12920)
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
