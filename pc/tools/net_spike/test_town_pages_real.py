#!/usr/bin/env python3
"""test_town_pages_real.py - Patch 6b on REAL processes: one REAL host + one REAL client game process (no GUI automation), disposable fixture copy
(NET_SPIKE_GAME_BIN=<abs path of pc\build64\bin_fixture4_<name>>, ports 11910+).
  the CLIENT process edits a design page in ITS OWN save (hook AC_TEST_PAGE_EDIT=2,1, what a player's edit does to the save) once it adopted the host's copy:
  G1 the client adopted the host's pages at join ('adopted kind ...')
  G2 the client's real dirty scan uploaded the edit and the HOST applied it ('write kind 2 index 1 APPLIED') and the client logged it as applied
  the HOST process edits a notice post in ITS OWN save (AC_TEST_PAGE_EDIT=1,5):
  G3 the host's scan bumped the revision ('changed on the host -> rev') and the real client ADOPTED the new copy ('updated from the host')
NOT asserted: the in-game UI (Able Sisters / notice board screens).
Usage: python test_town_pages_real.py [--port 11910]"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
import net_spike_lib as L  # noqa: E402
import test_txn_real_client as RC  # noqa: E402


def run(args, results, ip, rig):
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    os.environ["AC_TEST_PAGE_EDIT"] = "1,5"
    host = rig.start_host("pages", args.port, [])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    os.environ["AC_TEST_PAGE_EDIT"] = "2,1"
    cl = rig.start_client("pages", args.port, residents[0], [])
    os.environ.pop("AC_TEST_PAGE_EDIT", None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    check("G1 the client adopted the host's pages at join", cl.wait_for_log(r"\[NET\]\[PAGE\] client: adopted kind 2 index 1 rev \d+", 60.0) is not None)
    check("G2 the client edited its own page and the HOST applied the upload", host.wait_for_log(r"\[NET\]\[PAGE\] host: peer \d+ \(resident \d+\) write kind 2 index 1 APPLIED -> rev (\d+)", 60.0) is not None)
    check("G2 the client logged its write as APPLIED", cl.wait_for_log(r"\[NET\]\[PAGE\] client: my write of kind 2 index 1 was APPLIED", 30.0) is not None)
    check("G3 the HOST's own edit bumped the notice revision", host.wait_for_log(r"\[NET\]\[PAGE\] host: page kind 1 index 5 changed on the host -> rev \d+", 60.0) is not None)
    check("G3 the real client ADOPTED the host's new copy", cl.wait_for_log(r"\[NET\]\[PAGE\] client: page kind 1 index 5 updated from the host -> rev \d+", 60.0) is not None)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11910)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4_* copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "town_pages_real_snap_%d" % os.getpid())
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
