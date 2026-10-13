#!/usr/bin/env python3
"""test_able_trade_real.py - worker B: the Able Sisters TRADE JOURNAL on REAL processes (one REAL host + REAL client processes, hook-driven, no GUI automation).
The client walks into the Able Sisters shop (AC_TEST_MUSEUM_ROOM=needlework) and, once every design page is adopted and SEEN, its shop actor requests ONE exchange through
its own clip procs (AC_TEST_ABLE_TRADE, the path Mable's talk uses: aNI_RequestExchangeCloth -> aNI_ExchangeCloth in the actor's move).
  T1 APPLIED: exchange of design 2 with my_org[0]: the client journals it on the SEEN revision, the host APPLIES the page write, the client settles; outcome: my_org[0] holds
     the shop's previous design and the shop holds the player's previous design (a real swap, nothing lost)
  T2 STALE -> ROLLBACK: a NEW client process exchanges design 2 with my_org[1] on a base one revision older than displayed (',stale': a write the host must refuse);
     the host answers STALE, the client logs '[NET][ABLE] trade rolled back (write STALE)' and my_org[1] is restored to the player's own design, the shop shows the
     host's copy again (the live refresh redraws it)
NOT covered: Mable's dialogue itself (the trade gate's "Oh, I see." row), a superseding push / reconnect reset (same rollback function, see test_able_src.py).
Run on a DISPOSABLE fixture: NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_wlB_<name>>. Usage: python test_able_trade_real.py [--port 15430]"""
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

RX_REQ = r"\[NET\]\[ABLE\]\[TEST-ONLY\] AC_TEST_ABLE_TRADE: requesting exchange of design (\d) with my_org\[(\d)\] org_before=0x([0-9A-F]{8}) shop_before=0x([0-9A-F]{8})"
RX_OUT = r"\[NET\]\[ABLE\]\[TEST-ONLY\] AC_TEST_ABLE_TRADE outcome: my_org\[(\d)\]=0x([0-9A-F]{8}) \(before 0x([0-9A-F]{8})\) shop design (\d)=0x([0-9A-F]{8}) \(before 0x([0-9A-F]{8})\)"


def client(rig, tag, port, slot, trade):
    os.environ["AC_TEST_MUSEUM_ROOM"] = "needlework,3000"
    os.environ["AC_TEST_ABLE_TRADE"] = trade
    try:
        return rig.start_client(tag, port, slot, [])
    finally:
        os.environ.pop("AC_TEST_MUSEUM_ROOM", None)
        os.environ.pop("AC_TEST_ABLE_TRADE", None)


def run(args, results, ip, rig):
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    host = rig.start_host("able", args.port, ["--no-house-sync"])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    # T1
    cl = client(rig, "able_t1", args.port, residents[0], "x,2,0")
    check("T1 real client booted and connected", cl is not None)
    if cl is None:
        return
    check("T1 the client walked into the Able Sisters shop", cl.wait_for_log(r"warping into museum room 5", 60.0) is not None)
    m = cl.wait_for_log(RX_REQ, 90.0)
    check("T1 the shop actor requested the exchange", m is not None)
    if m is None:
        return
    req = re.search(RX_REQ, cl.log_text())
    check("T1 precondition: the player's design differs from the shop's (0x%s vs 0x%s)" % (req.group(3), req.group(4)), req.group(3) != req.group(4))
    check("T1 the trade was journaled on the SEEN revision", cl.wait_for_log(r"\[NET\]\[ABLE\] trade journaled: exchange of design 2 with my_org\[0\], built on the SEEN rev \d+", 20.0) is not None)
    check("T1 the HOST applied the page write", host.wait_for_log(r"\[NET\]\[PAGE\] host: peer \d+ \(resident \d+\) write kind 2 index 2 APPLIED -> rev \d+", 30.0) is not None)
    check("T1 the client settled the trade on APPLIED", cl.wait_for_log(r"\[NET\]\[ABLE\] trade settled \(APPLIED, rev \d+\): exchange of design 2", 30.0) is not None)
    o = cl.wait_for_log(RX_OUT, 30.0)
    out = re.search(RX_OUT, cl.log_text())
    check("T1 outcome: a real swap (my_org[0] = the shop's old design, the shop = the player's old design)",
          out is not None and out.group(2) == req.group(4) and out.group(5) == req.group(3))
    check("T1 nothing rolled back", "trade rolled back" not in cl.log_text())
    cl.stop()
    # T2
    cl = client(rig, "able_t2", args.port, residents[0], "x,2,1,stale")
    check("T2 a NEW real client process booted and connected", cl is not None)
    if cl is None:
        return
    m = cl.wait_for_log(r"AC_TEST_ABLE_TRADE: requesting exchange of design 2 with my_org\[1\].*\(stale base injected\)", 120.0)
    check("T2 the shop actor requested the exchange on a STALE base", m is not None)
    if m is None:
        return
    req = re.search(RX_REQ, cl.log_text())
    check("T2 precondition: the player's design differs from the shop's (0x%s vs 0x%s)" % (req.group(3), req.group(4)), req.group(3) != req.group(4))
    check("T2 the HOST refused the write STALE", host.wait_for_log(r"\[NET\]\[PAGE\] host: peer \d+ write kind 2 index 2 STALE", 30.0) is not None)
    check("T2 the client ROLLED BACK the trade (my_org restored)", cl.wait_for_log(r"\[NET\]\[ABLE\] trade rolled back \(write STALE\): exchange of design 2 -- my_org\[1\] restored", 30.0) is not None)
    cl.wait_for_log(RX_OUT, 30.0)
    out = re.search(RX_OUT, cl.log_text())
    check("T2 outcome: my_org[1] is the player's own design again and the shop shows the host's copy (nothing lost, nothing duplicated)",
          out is not None and out.group(2) == req.group(3) and out.group(5) == req.group(4))
    check("T2 the shop redrew the host's copy (live refresh)", cl.wait_for_log(r"\[NET\]\[ABLE\] mannequin 2 redrawn from the shared design", 20.0) is not None)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=15430)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4_* copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "able_trade_real_snap_%d" % os.getpid())
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
