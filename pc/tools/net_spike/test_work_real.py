#!/usr/bin/env python3
"""test_work_real.py - Nook Work Mode first-page menu on REAL processes: one REAL host + one REAL client game process (no GUI automation), disposable fixture copy
(NET_SPIKE_GAME_BIN=<abs path of pc\build64\bin_fixture4_<name>>, ports 11930+).
The REAL client walks the real work path through the TEST-ONLY hook AC_TEST_WORK_ENTER=1 (+ AC_TEST_DUMP_MSG=0x1092 = Tom Nook's first page): it loads Nook's first-page message through the
game's normal message loader (so the first-page patch runs against the AUTHORITATIVE job state mirrored from the host), then ENTERs a job through the real op (TXN to the host), loads it again,
LEAVEs (quits) and loads it a third time.
  W1 no job:      the loaded first page carries choice 614 (PC_NOOK_SEL_WORK, "I'd like to work")
  W2 active job:  after the real ENTER the loaded first page carries choice 618 (PC_NOOK_SEL_JOB, "Check my job"); the host created the job (WORK_STATE / commit lines)
  W3 quit:        after the real LEAVE it is back to 614
NOT asserted: the dialogue on screen (GUI). The message bytes come from the real loader of a real process.
Usage: python test_work_real.py [--port 11930]"""
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


def dumps(text):
    out = {}
    for m in re.finditer(r"\[MSG\]\[TEST-ONLY\] (initial|after-ENTER|final) message 0x1092 loaded, len (\d+), bytes:((?: [0-9A-F]{2})+)", text):
        out[m.group(1)] = bytes(int(x, 16) for x in m.group(3).split())
    return out


def has_choice(b, sel):
    return bytes([sel >> 8, sel & 0xFF]) in b


def run(args, results, ip, rig):
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    os.environ["AC_TEST_WORK_TYPE"] = "1"
    host = rig.start_host("workreal", args.port, [])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    os.environ["AC_TEST_DUMP_MSG"] = "0x1092"
    os.environ["AC_TEST_WORK_ENTER"] = "1"
    cl = rig.start_client("workreal", args.port, residents[0], [])
    os.environ.pop("AC_TEST_DUMP_MSG", None)
    os.environ.pop("AC_TEST_WORK_ENTER", None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"\[MSG\]\[TEST-ONLY\] final message 0x1092 loaded", 90.0)
    d = dumps(cl.log_text())
    check("the client loaded Nook's first page three times (%s)" % sorted(d), set(d) == {"initial", "after-ENTER", "final"})
    if set(d) != {"initial", "after-ENTER", "final"}:
        return
    check("W1 no job: the first page carries choice 614 ('I'd like to work') and not 618", has_choice(d["initial"], 614) and not has_choice(d["initial"], 618))
    check("W2 after the real ENTER the first page carries choice 618 ('Check my job') and not 614", has_choice(d["after-ENTER"], 618) and not has_choice(d["after-ENTER"], 614))
    check("W2 the HOST created the job for that character (work commit line)", re.search(r"\[NET\]\[WORK\] host: peer \d+ resident \d+ WORK op 1 committed: job_id=\d+ type=1 ", host.log_text()) is not None)
    check("W3 after the real LEAVE the first page is back to 614", has_choice(d["final"], 614) and not has_choice(d["final"], 618))
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11930)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4_* copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "work_real_snap_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    wj = os.path.join(save_dir, "mp", "work_jobs.dat")
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        if os.path.exists(wj):
            os.remove(wj)
        run(args, results, "127.0.0.1", rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
