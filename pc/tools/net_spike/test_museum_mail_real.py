#!/usr/bin/env python3
"""test_museum_mail_real.py - worker B: a REAL client's letter WITH A GIFT to the museum (MUSEUM_MAIL, TXN kind 19), REAL host + REAL client processes, hook-driven.
The client runs `--mail-test-send=museum,gift`: the mail test hook writes a send-font letter addressed to the museum (mMsm_GetMuseumMailName, the address book's entry) into a
free mail slot, moves the first pocket item into it (what the letter board's hand overlay does) and then uses the SAME entry points as the post girl
(pc_net_game_mail_begin_send / _poll): AWAIT_CLEAN -> TXN_COMMIT kind 19 -> TXN_RESULT -> pcnetgame_txn_apply_mail.
  M1 the client's begin picked kind 19 (MUSEUM_MAIL) and the host COMMITTED it on that resident's museum_record with the resident's donor nibble
  M2 the client applied APPLIED: the mail slot is EMPTY afterwards, the pockets did not change before APPLIED (no "CHANGED BEFORE APPLIED" line)
NOT covered: the post girl dialogue rows themselves (aPG_client_mail_step's mapping is source-audited by test_museum_src.py).
Run on a DISPOSABLE fixture: NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_wlB_<name>>. Usage: python test_museum_mail_real.py [--port 15480]"""
import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402
import test_txn_real_client as RC  # noqa: E402


def run(args, results, ip, rig):
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    host = rig.start_host("museum_mail", args.port, ["--no-house-sync"])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    cl = rig.start_client("museum_mail", args.port, residents[0], ["--mail-test-send=museum,gift"])
    check("real client booted and connected", cl is not None)
    if cl is None:
        return
    rx = r"--mail-test-send=museum,gift: wrote a letter into mail slot (\d+) for house -2 present 0x([0-9A-F]{4})"
    cl.wait_for_log(rx, 90.0)
    m = re.search(rx, cl.log_text())
    check("the hook wrote a museum letter with a gift (the first pocket item: 0x%s)" % (m.group(2) if m else "?"), m is not None and m.group(2) != "0000")
    check("M1 the client began the send", cl.wait_for_log(r"\[NET\]\[MAIL\] client: begin MAIL_SEND request=\d+ mail slot=\d+ present=0x[0-9A-F]{4}", 30.0) is not None)
    check("M1 the HOST committed a MUSEUM_MAIL of that resident (donor nibble = slot + 1 = %d)" % (residents[0] + 1),
          host.wait_for_log(r"\[NET\]\[MUSEUM\]\[MAIL\] host: peer \d+ resident %d MUSEUM_MAIL slot \d+ gift 0x[0-9A-F]{4} -> .* \(donor nibble %d\)" % (residents[0], residents[0] + 1), 60.0) is not None)
    check("M2 the client got APPLIED", cl.wait_for_log(r"--mail-test-send: result APPLIED", 30.0) is not None)
    check("M2 the mail slot is EMPTY afterwards", cl.wait_for_log(r"--mail-test-send: final mail slot \d+ EMPTY", 20.0) is not None)
    check("M2 nothing changed before APPLIED", "CHANGED BEFORE APPLIED" not in cl.log_text())
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=15480)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4_* copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "museum_mail_real_snap_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        run(args, results, "127.0.0.1", rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
