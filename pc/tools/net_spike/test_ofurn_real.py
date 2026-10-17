#!/usr/bin/env python3
"""test_ofurn_real.py - OUTDOOR FURNITURE (C1), REAL processes: one REAL listen host + one REAL client game process.

TIER: REAL HOST + REAL CLIENT (no GUI automation of the menus: the client drives the REAL input seam pc_ofurn_front_press -- the wait-state A / B hook of
m_player_main_wait.c_inc -- through the TEST-ONLY AC_TEST_OFURN=rotpick hook; the host places a piece NEXT TO the client through AC_TEST_OFURN_SEED + _SEED_NEAR), on a
DISPOSABLE copy NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_of_<name>> (ports 25000-25999). Both processes also run AC_TEST_OFURN_DRAWCYCLE=1: the model of
EVERY furniture id (incl. the ones with behaviour code) is built, drawn for a frame and torn down in the town -- nothing may crash.
  R1 the host placed a piece next to the real client; the client adopted service 10 and its rights, and its marker list holds the piece
  R2 the client's renderer built the MODEL of the piece (shape 1x1, rotation 0) and registered its COLLISION box (the piece is next to the player)
  R3 A in front of the piece (the real input seam) -> kind 22 ROTATE -> the host APPLIED it, the client re-built the model with rotation 1
  R4 B in front of it -> kind 22 PICKUP -> the host APPLIED it (the base item into a pocket), the client released the model
  R5 the model draw cycle finished on the client and on the host (every furniture id built / drawn / torn down)
  R6 host and client alive
NOT asserted: what the model looks like on screen (no image comparison).
"""
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

ITEM = 0x1320  # a 1x1 furniture WITH a model (the draw cycle reports index 200 = 0x1320 shape 4 drawable 1)
STARTED = []


def _run(args, rig):
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    os.environ["AC_TEST_OFURN_SEED"] = "0x%04X,0" % ITEM
    os.environ["AC_TEST_OFURN_SEED_NEAR"] = "1"
    os.environ["AC_TEST_OFURN_DRAWCYCLE"] = "1"
    os.environ.pop("AC_TEST_OFURN", None)
    host = rig.start_host("ofreal", args.port, [])
    check("host reached genuine field-ready state", host is not None)
    if host is None:
        return
    STARTED.append(host)
    L.resolve_host_town("127.0.0.1", args.port)
    os.environ["AC_TEST_OFURN"] = "rotpick"
    a = rig.start_client("ofrealA", args.port, residents[0], [])
    check("real client booted into the field and connected", a is not None)
    if a is None:
        return
    STARTED.append(a)
    m = host.wait_for_log(r"seeded placed piece at \((\d+),(\d+)\) item 0x%04X rot 0 -> placed \(next to peer (\d+)" % ITEM, 120.0)
    check("R1 the host placed a piece next to the real client", m is not None)
    if m is None:
        return
    ux, uz = int(m.group(1)), int(m.group(2))
    lst = a.wait_for_log(r"\[NET\]\[OFURN\] client: (?:placed-furniture list: \d+ piece|SET tile \(%d,%d\))" % (ux, uz), 60.0)
    at = a.log_text()
    check("R1 the client adopted service 10, its rights (place|pickup) and the piece (list or SET delta)",
          "[NET][OFURN] client: the host supports outdoor furniture: Place ON" in at and "[NET][OFURN] client: my rights: place=1 pickup=1" in at and lst is not None)
    built = a.wait_for_log(r"\[OFURN\]\[DRAW\] model built: tile \(%d,%d\) item 0x%04X rot 0 shape 4" % (ux, uz, ITEM), 60.0)
    check("R2 the client's renderer built the 1x1 MODEL of the piece (rotation 0)", built is not None)
    col = a.wait_for_log(r"\[OFURN\]\[COL\] collision registered: tile \(%d,%d\) item 0x%04X" % (ux, uz, ITEM), 30.0)
    check("R2 ... and registered its COLLISION box (move-bg)", col is not None)
    rot = a.wait_for_log(r"\[OFURN\]\[INPUT\] A \(rotate\) in front of the placed piece at \(%d,%d\) -> handled" % (ux, uz), 90.0)
    check("R3 A in front of the piece went through the real input seam", rot is not None)
    hr = host.wait_for_log(r"ROTATE APPLIED \[TXN\]: tile \(%d,%d\) item 0x%04X rot 1" % (ux, uz, ITEM), 30.0)
    check("R3 the host APPLIED the kind 22 ROTATE (rotation 1)", hr is not None)
    rb = a.wait_for_log(r"\[OFURN\]\[DRAW\] model built: tile \(%d,%d\) item 0x%04X rot 1" % (ux, uz, ITEM + 1), 30.0)
    check("R3 the client re-built the model with rotation 1", rb is not None)
    pk = host.wait_for_log(r"PICKUP APPLIED \[TXN\]: tile \(%d,%d\) item 0x%04X" % (ux, uz, ITEM + 1), 60.0)
    check("R4 B in front of it: the host APPLIED the kind 22 PICKUP", pk is not None)
    rel = a.wait_for_log(r"\[OFURN\]\[DRAW\] model released: tile \(%d,%d\)" % (ux, uz), 30.0)
    ca = a.wait_for_log(r"\[NET\]\[OFURN\] client: PICKUP APPLIED \(request \d+\): tile \(%d,%d\)" % (ux, uz), 10.0)
    check("R4 the client applied the pickup (base item into a pocket) and released the model", rel is not None and ca is not None)
    dc = a.wait_for_log(r"\[OFURN\]\[DRAWTEST\]\[TEST-ONLY\] done: (\d+) furniture model", 180.0)
    dh = host.wait_for_log(r"\[OFURN\]\[DRAWTEST\]\[TEST-ONLY\] done: (\d+) furniture model", 60.0)
    check("R5 the model draw cycle finished on the client (%s models) and on the host (%s models)" % (dc.group(1) if dc else "?", dh.group(1) if dh else "?"),
          dc is not None and dh is not None and int(dc.group(1)) > 1000 and int(dh.group(1)) > 1000)
    check("R6 host and client alive", host.alive() and a.alive())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=25700)
    args = ap.parse_args()
    if not (25000 <= args.port <= 25999):
        print("ports 25000-25999 only", file=sys.stderr)
        return 2
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_of"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_of_<name> copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "ofurn_real_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        _run(args, rig)
    finally:
        for p in STARTED:  # only the processes THIS test started
            try:
                if p.alive():
                    p.stop()
            except Exception:  # noqa: BLE001
                pass
        for k in ("AC_TEST_OFURN_SEED", "AC_TEST_OFURN_SEED_NEAR", "AC_TEST_OFURN_DRAWCYCLE", "AC_TEST_OFURN"):
            os.environ.pop(k, None)
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
