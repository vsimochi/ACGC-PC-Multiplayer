#!/usr/bin/env python3
"""test_work_reward_overflow_real.py - Work Mode REWARD OVERFLOW on the REAL client apply path (pcnetgame_txn_apply_work, post_wallet < pre_wallet): one REAL host + one REAL client
(disposable fixture, NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>>, ports 11990+).

The REAL client runs ENTER -> TAKE_PARCEL -> VILLAGER_RECEIVE -> REPORT through the real work ops with the TEST-ONLY hook AC_TEST_WORK_REWARD (pc_net_game.c; the host is forced to
job type 3 with AC_TEST_WORK_TYPE=3). The hook first raises the LOCAL wallet to 99990 (the normal D3 upload carries it to the host record), so the REPORT reward overflows the wallet into a
30000 money bag on the host (pcnetgame_work_plan) and the client must apply "wallet - dec + 30000 bag" and never create Bells.
  mode 1: client state == the request's pre-image -> the HOST POST-IMAGE branch.
  mode 2: a foreign item is put into a free local pocket while the REPORT is in flight -> the DELTA branch.
Assertions (client + host logs): all 4 ops APPLIED; the REPORT carries a bag (final bags >= 1) and the final total (wallet + 30000 per bag) == total before the REPORT + the paid reward
(from the client's APPLIED line == the host's 'paid='); final wallet <= 99999; no 'inconsistent post-image'; the apply branch is the expected one.
Usage: python test_work_reward_overflow_real.py [--port 11990] [--only 1,2]"""
import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
import net_spike_lib as L  # noqa: E402
import test_txn_real_client as RC  # noqa: E402


def run_mode(mode, port, results, rig):
    check = rig.check
    tag = "wrew%d" % mode
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    os.environ["AC_TEST_WORK_TYPE"] = "3"
    host = rig.start_host(tag, port, [])
    check("M%d host reached field-ready state" % mode, host is not None)
    if host is None:
        return
    L.resolve_host_town("127.0.0.1", port)
    os.environ["AC_TEST_WORK_REWARD"] = str(mode)
    cl = rig.start_client(tag, port, residents[0], [])
    os.environ.pop("AC_TEST_WORK_REWARD", None)
    check("M%d real client booted into the field and connected" % mode, cl is not None)
    if cl is None:
        return
    m = cl.wait_for_log(r"\[WORKREWARD\]\[TEST-ONLY\] (final|op \d+ refused locally)", 150.0)
    ct, ht = cl.log_text(), host.log_text()
    check("M%d the hook reached its final report" % mode, m is not None and m.group(1) == "final")
    mf = re.search(r"final: wallet=(\d+) bags=(\d+) total=(\d+) start_total=(\d+)", ct)
    ms = re.search(r"start: wallet=(\d+) bags=(\d+) total=(\d+)", ct)
    mb = re.search(r"before-REPORT: job_type=(\d) obj_state=(\d) reward=(\d+) tip_kind=(\d) tip_value=(\d+) wallet=(\d+) total=(\d+)", ct)
    ops = re.findall(r"\[WORKREWARD\]\[TEST-ONLY\] op (\d) result (APPLIED|REJECTED)", ct)
    check("M%d all four ops APPLIED (%s)" % (mode, ops), [o for o, _r in ops] == ["1", "4", "6", "7"] and all(r == "APPLIED" for _o, r in ops))
    check("M%d the local wallet was raised to 99990 and the start total is 99990 (%s)" % (mode, ms and ms.groups()), ms is not None and int(ms.group(1)) == 99990 and int(ms.group(3)) == 99990)
    mh = re.search(r"WORK op 7 committed: job_id=\d+ type=3 obj_state=\d+ mode=\d+ state=\d+ paid=(\d+) wallet (\d+) -> (\d+)", ht)
    check("M%d host committed REPORT with the reward paid and the wallet reduced by the bag (%s)" % (mode, mh and mh.groups()),
          mh is not None and int(mh.group(1)) > 0 and int(mh.group(3)) < int(mh.group(2)))
    ma = re.search(r"\[NET\]\[WORK\] client: work op 7 APPLIED \(request \d+\): item 0x[0-9A-F]{4} slot \d+, paid (\d+) Bells, wallet now (\d+) \((host post-image|delta)\)", ct)
    want = "host post-image" if mode == 1 else "delta"
    check("M%d client apply branch = %s (%s)" % (mode, want, ma and ma.groups()), ma is not None and ma.group(3) == want)
    check("M%d no inconsistent post-image / protocol violation on the client" % mode, "inconsistent post-image" not in ct and "protocol violation" not in ct)
    if mf is not None and mb is not None and ma is not None:
        wallet, bags, total, _st = (int(x) for x in mf.groups())
        paid = int(ma.group(1))
        pre_total = int(mb.group(7))
        check("M%d CONSERVATION: total after (%d) == total before the REPORT (%d) + paid reward (%d)%s" % (mode, total, pre_total, paid, " [mode 2: + no foreign Bells]" if mode == 2 else ""),
              total == pre_total + paid)
        check("M%d the overflow produced a money bag (bags=%d) and the wallet stayed <= 99999 (%d); paid == the job reward %s" % (mode, bags, wallet, mb.group(3)),
              bags >= 1 and wallet <= 99999 and paid == int(mb.group(3)))
        if mh is not None:
            check("M%d client wallet after == host wallet after (%s vs %s)" % (mode, ma.group(2), mh.group(3)), mode == 2 or int(ma.group(2)) == int(mh.group(3)))
    else:
        check("M%d the final / before-REPORT / APPLIED lines exist" % mode, False)
    check("M%d host and client alive" % mode, host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11990)
    ap.add_argument("--only", default="1,2")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4_* copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "work_rew_snap_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    wj = os.path.join(save_dir, "mp", "work_jobs.dat")
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        for i, mode in enumerate(int(x) for x in args.only.split(",")):
            if os.path.exists(wj):
                os.remove(wj)
            run_mode(mode, args.port + i, results, rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
