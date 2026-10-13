#!/usr/bin/env python3
"""test_house_upgrade_real.py - house upgrades via Nook (worker C) with REAL game processes: a listen host + a REAL resident client.

TIER: REAL HOST + REAL CLIENT, hook-driven (no GUI automation): the client's TEST-ONLY AC_TEST_HOUSE_UPGRADE_SEQ drives the REAL client entry points the Nook dialogue calls
(pc_house_upgrade_begin / _poll -> AWAIT_CLEAN -> TXN_COMMIT kind 20 -> TXN_RESULT -> pcnetgame_txn_apply_house_upgrade); op 9 is the hook's one local test write (the loan paid
off, what the post office does; the D3 upload carries it). The host runs with AC_TEST_REHOUSE_DUE=1 (an order placed today counts as due: the construction poll builds it at once).
Fixture: the disposable pc\\build64\\bin_fixture4_wlC_hupreal (fresh copy of bin_fixture4 + the exe of pc\\build64_w3); Samsung display, volume 1.
  R1  pay -> ORDER_SIZE (roof 5) APPLIED -> the host builds it (one CONSTRUCTED line) -> the client's house canon shows size 1 -> ACK_BUILT APPLIED: the host's mirror loan and the
      client's local loan are both the vanilla 148,000 -> pay -> the NEXT order (Medium -> Large, roof 2) APPLIED. No refusal, no violation, no divergence.
  R2  LINK LOST in flight: the host drops every APPLIED result (--txn-fault=drop_result): the client resends the identical bytes (answered from the journal, the order happens ONCE)
      until its 60 s cap drops the link; the op resolves REJECTED with reason 0 and sent = 1, which the dialogue maps onto PC_NOOK_MSG_UPG_LINKLOST ("cannot say whether it went
      through").
NOT covered: the Nook dialogue GUI itself (the seams are source-audited in test_house_upgrade_src.py), the house exterior redraw (a scene reload).
Usage: python test_house_upgrade_real.py [--port 16330] [--only R1,R2]
"""
import argparse
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_wlC_hupreal"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)
os.environ["AC_DISPLAY_NAME"] = "samsung"
os.environ["AC_MASTER_VOLUME"] = "1"
os.environ["AC_TEST_HOOKS"] = "1"

if __name__ == "__main__":
    T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))

import net_spike_lib as L  # noqa: E402

results = []
ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
SAVE = os.path.join(T.BUILD64, FIXTURE, "save")
SNAP = os.path.join(os.environ.get("TEMP", "."), "hupreal_save_snapshot_%d" % os.getpid())


def start_host(tag, port, extra=(), env_extra=None):
    shutil.rmtree(SAVE, ignore_errors=True)
    shutil.copytree(SNAP, SAVE)
    env = dict(os.environ, AC_TEST_REHOUSE_DUE="1", **(env_extra or {}))
    for i in range(2):
        h = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"] + list(extra), log_path=T.log_path("hupreal_%s_host_%d.log" % (tag, i)),
                          bin_dir=L.GAME_BIN_DIR, env=env).start()
        if h.wait_listening(60.0) and h.boot_to_field(timeout=120.0, slot=0):
            return h
        h.stop()
        time.sleep(2.0)
    return None


def start_client(tag, port, slot, seq):
    env = dict(os.environ, AC_TEST_HOUSE_UPGRADE_SEQ=seq)
    for i in range(2):
        c = L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--bootstrap-resident", str(slot)], log_path=T.log_path("hupreal_%s_client_%d.log" % (tag, i)),
                            bin_dir=L.GAME_BIN_DIR, label=tag, env=env).start()
        if c.boot_to_field(timeout=150.0, slot=slot):
            return c
        c.stop()
        time.sleep(2.0)
    return None


def run_r1(args):
    h = start_host("r1", args.port)
    ck("R1 listen host reached the field", h is not None)
    if h is None:
        return
    c = None
    try:
        c = start_client("r1", args.port, 1, "9,1:5,4,9,1:2")
        ck("R1 real resident client (slot 1) booted and connected", c is not None)
        if c is None:
            return
        m = c.wait_for_log(r"\[NET\]\[HOUSEUP\]\[TEST-ONLY\] op 4 \(ORDER_SIZE\) -> (APPLIED|REJECTED)", 240.0)
        ct, ht = c.log_text(), h.log_text()
        ck("R1 the hook ran the whole sequence", m is not None)
        o1 = re.search(r"op 1 \(ORDER_SIZE\) -> APPLIED reason 0 sent 1", ct)
        o2 = re.search(r"op 2 \(ACK_BUILT\) -> APPLIED reason 0 sent 1; local loan (\d+); local house size (\d+)", ct)
        ck("R1 begin -> clean record -> kind-20 TXN_COMMIT -> TXN_RESULT APPLIED for the first order (client log, in order)",
           o1 is not None and re.search(r"begin ORDER_SIZE request=\d+ arg=5", ct) and re.search(r"record is clean after \d+ ms -- beginning the ORDER_SIZE transaction", ct)
           and re.search(r"TXN_COMMIT sent kind=HOUSE_UPGRADE", ct) and re.search(r"TXN_RESULT APPLIED kind=HOUSE_UPGRADE", ct))
        mh = re.search(r"\[NET\]\[HOUSEUP\] host: peer \d+ resident 1 house (\d+) ORDER_SIZE APPLIED \[TXN\]: size 0 next 1 .* pal 5 ", ht)
        ck("R1 host: ORDER_SIZE APPLIED for resident 1 (size 0 -> next 1, roof 5), saved durably", mh is not None)
        hs = mh.group(1) if mh else "?"
        ck("R1 host: house %s CONSTRUCTED exactly once for the first order (size 1, outlook 5)" % hs,
           len(re.findall(r"house %s CONSTRUCTED \(size\): size 1 next 1 renew 1 .* pal 5" % hs, ht)) == 1)
        ck("R1 ACK_BUILT APPLIED: the client's local loan is the vanilla 148000 and its house copy (canon) is size 1",
           o2 is not None and int(o2.group(1)) == 148000 and int(o2.group(2)) == 1 and "the loan is now 148000" in ct)
        ck("R1 host: ACK_BUILT APPLIED with the mirror loan 148000 (rev + 1)", re.search(r"resident 1 house %s ACK_BUILT APPLIED \[TXN\].* loan 148000 \(rev \+ 1\)" % hs, ht) is not None)
        ck("R1 the loan was paid again (op 3) and the NEXT order (Medium -> Large, roof 2) APPLIED",
           "op 4 (ORDER_SIZE) -> APPLIED reason 0" in ct and re.search(r"resident 1 house %s ORDER_SIZE APPLIED \[TXN\]: size 1 next 2 .* pal 2 " % hs, ht) is not None)
        ck("R1 no refusal / violation / divergence / unsupported latch",
           "REJECTED(" not in ct and "refused locally" not in ct and re.search(r"\[NET\]\[REC\] host: peer \d+ violation", ht) is None and
           "possible local divergence" not in ct and "does not know HOUSE_UPGRADE" not in ct)
    finally:
        if c is not None:
            c.stop()
        h.stop()


def run_r2(args):
    h = start_host("r2", args.port + 1, extra=["--txn-fault=drop_result:1:1000"])
    ck("R2 listen host (drop_result) reached the field", h is not None)
    if h is None:
        return
    c = None
    try:
        c = start_client("r2", args.port + 1, 1, "9,1:1")
        ck("R2 real resident client booted", c is not None)
        if c is None:
            return
        m = c.wait_for_log(r"\[NET\]\[HOUSEUP\]\[TEST-ONLY\] op 1 \(ORDER_SIZE\) -> (APPLIED|REJECTED) reason (\d+) sent (\d+)", 150.0)
        ct, ht = c.log_text(), h.log_text()
        ck("R2 the op resolved REJECTED with reason 0 and sent 1 (the dialogue's LINKLOST row)", m is not None and m.group(1) == "REJECTED" and m.group(2) == "0" and m.group(3) == "1")
        ck("R2 the client resent the identical bytes and then dropped the link at its 60 s cap",
           "resending the identical" in ct and "unresolved >60 s: reconnect required" in ct)
        ck("R2 the host executed the order exactly ONCE (the resends were replays)",
           len(re.findall(r"resident 1 house \d+ ORDER_SIZE APPLIED \[TXN\]", ht)) == 1 and "TXN replay APPLIED kind=HOUSE_UPGRADE" in ht)
    finally:
        if c is not None:
            c.stop()
        h.stop()


def mash(c, auto, n, kind="mashf", secs=6):
    open(auto, "w").write("%d %s %.2f\n" % (n, kind, secs))
    end = time.monotonic() + secs + 20
    while time.monotonic() < end:
        if re.search(r"\[AUTO\] done %d " % n, c.log_text()) or re.search(r"\[AUTO\] mash %d end" % n, c.log_text()):
            return True
        time.sleep(0.3)
    return False


def run_r3(args):
    """The REAL Nook dialogue of a resident client (autopilot A presses, every choice answered with its FIRST option): the roof-colour choice of the DONE_REHOUSE offer goes
    through the choice seam (the message locked while kind 20 runs, APPLIED -> the vanilla follow-up row), and the next talk after the construction is the PC_HOUSEUP talk start
    (wait row -> ACK_BUILT -> the vanilla 'built' row, loan 148,000)."""
    h = start_host("r3", args.port + 2, env_extra={"AC_TEST_CLEAR_ERRANDS": "1"})  # the fixture residents are still in the vanilla first job (Nook's own flow)
    ck("R3 listen host reached the field", h is not None)
    if h is None:
        return
    c = None
    auto = os.path.join(L.GAME_BIN_DIR, "ac_auto.txt")
    if os.path.isfile(auto):
        os.remove(auto)
    try:
        os.environ["AC_TEST_AUTOPILOT"] = "1"
        c = L.ClientProcess("127.0.0.1:%d" % (args.port + 2), extra_args=["--bootstrap-resident", "1", "--nook-test", "warp,resident"],
                            log_path=T.log_path("hupreal_r3_client.log"), bin_dir=L.GAME_BIN_DIR, label="r3",
                            env=dict(os.environ, AC_TEST_HOUSE_UPGRADE_SEQ="9", AC_TEST_AUTOPILOT="1", AC_TEST_CLEAR_ERRANDS="1")).start()
        del os.environ["AC_TEST_AUTOPILOT"]
        ok = c.boot_to_field(timeout=150.0, slot=1)
        ck("R3 real resident client booted", ok)
        if not ok:
            return
        ck("R3 the loan was paid off (the hook's op 9) and the client warped into Nook's shop",
           c.wait_for_log(r"--nook-test: warping the guest into the shop", 120.0) is not None and "op 0: the local loan" in c.log_text())
        sent = None
        for k in range(1, 12):
            mash(c, auto, 100 + k, "mashf", 6)
            sent = re.search(r"\[NET\]\[HOUSEUP\] nook dialogue: order op 1 \(arg (\d+)\) sent to the host; the message waits", c.log_text())
            if sent and re.search(r"order op 1 (APPLIED|not done)", c.log_text()):
                break
        ct, ht = c.log_text(), h.log_text()
        ck("R3 the roof-colour choice of the DONE_REHOUSE offer started ORDER_SIZE through the choice seam (the message waited)", sent is not None)
        ck("R3 ... APPLIED -> the vanilla follow-up row (no refusal row)", "order op 1 APPLIED -> the vanilla follow-up row" in ct and "instead of the vanilla follow-up" not in ct)
        ck("R3 host: ORDER_SIZE APPLIED for resident 1 (roof = the chosen colour %s)" % (sent and sent.group(1)),
           sent is not None and re.search(r"resident 1 house \d+ ORDER_SIZE APPLIED \[TXN\]: size 0 next 1 .* pal %s " % sent.group(1), ht) is not None)
        ck("R3 host: the order was CONSTRUCTED", W_wait(h, r"house \d+ CONSTRUCTED \(size\)", 30.0))
        ck("R3 the client's own house canon arrived (size 1)", W_wait(c, r"house \d+ canonical seq \d+ written into the local save \(own house\)", 60.0))
        talk = None
        # Nook calls a player (the REHOUSE talk) only when the player ENTERS the shop (aNSC_start_wait is the entry action): the client is restarted and warped in again
        for k in range(20, 26):
            mash(c, auto, k, "mashc", 4)  # end the current conversation (cancel at every choice)
        c.stop()
        if os.path.isfile(auto):
            os.remove(auto)  # a command left over would be read by the next process while it boots (the autopilot is TEST-ONLY)
        c = L.ClientProcess("127.0.0.1:%d" % (args.port + 2), extra_args=["--bootstrap-resident", "1", "--nook-test", "warp,resident"],
                            log_path=T.log_path("hupreal_r3_client2.log"), bin_dir=L.GAME_BIN_DIR, label="r3b",
                            env=dict(os.environ, AC_TEST_AUTOPILOT="1", AC_TEST_CLEAR_ERRANDS="1")).start()
        ck("R3 the client rejoined and warped into the shop again",
           c.boot_to_field(timeout=150.0, slot=1) and c.wait_for_log(r"--nook-test: warping the guest into the shop", 120.0) is not None)
        for k in range(60, 80):
            mash(c, auto, k, "mashf", 3)
            talk = re.search(r"\[NET\]\[HOUSEUP\] nook dialogue: op 4 APPLIED -> the vanilla row (\d+)", c.log_text())
            if talk and k > 62:
                break
        ct, ht = c.log_text(), h.log_text()
        ck("R3 the next talk was the PC_HOUSEUP talk start (op 4 = ACK_BUILT, then the vanilla MEDIUM_BUILT row 3)",
           "talk start needs the host first: op 4 (then vanilla row 3)" in ct and talk is not None and talk.group(1) == "3")
        ck("R3 the loan is 148000 on the client and on the host", "the loan is now 148000" in ct and re.search(r"ACK_BUILT APPLIED \[TXN\].* loan 148000", ht) is not None)
        for k in range(40, 44):
            mash(c, auto, k, "mashf", 4)
        ck("R3 both processes alive after the talks", c.alive() and h.alive())
    finally:
        os.environ.pop("AC_TEST_AUTOPILOT", None)
        if c is not None:
            c.stop()
        h.stop()


def W_wait(p, rx, timeout):
    return p.wait_for_log(rx, timeout) is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16330)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    shutil.copytree(SAVE, SNAP)
    try:
        for name, fn in (("R1", run_r1), ("R2", run_r2), ("R3", run_r3)):
            if args.only and name not in args.only.split(","):
                continue
            try:
                fn(args)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                ck("%s raised %r" % (name, e), False)
    finally:
        shutil.rmtree(SNAP, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
