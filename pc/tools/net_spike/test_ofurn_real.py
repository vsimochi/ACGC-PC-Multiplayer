#!/usr/bin/env python3
"""test_ofurn_real.py - OUTDOOR FURNITURE (C2: multi-tile + grab / push / pull / rotate), REAL processes: one REAL listen host + one REAL client game process.

TIER: REAL HOST + REAL CLIENT (no GUI automation of the menus: the client drives the REAL wait-state seam pc_ofurn_front_press and the REAL vanilla player states
HOLD / PULL / PUSH / ROTATE_FURNITURE through the TEST-ONLY AC_TEST_OFURN=grab hook, whose synthetic A button / stick replaces the pad only inside the outdoor grab
driver; the host places a 2x1 DRESSER next to the client through AC_TEST_OFURN_SEED + _SEED_NEAR), on a DISPOSABLE copy
NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_of2_<name>> (ports 38000-38999). The host runs PC_PUPPET_DIAG=1: it is the OBSERVER of the client's
player (its puppet rows). Optional --drawcycle: both processes also build / draw / tear down the model of EVERY furniture id (the C1 crash check).
  G1 the host placed a 2x1 dresser (0x1064, a storage piece) next to the real client; the client adopted the C2 CAPS (message 102) and the piece
  G2 the client built the 2x1 MODEL (fp 1) and registered its COLLISION box
  G3 A in front of the piece GRABS it: the real HOLD state (main index 19)
  G4 stick away = PULL: the request went out, the host APPLIED the MOVE, the client saw APPLIED; stick towards = PUSH: the same
  G5 stick sideways = ROTATE (LTURN / RTURN): at least one rotation APPLIED (or refused locally with the "won't budge" puff when the swing cell is taken)
  G6 the OBSERVER (host) saw the client's puppet in HOLD 19, PULL 21 and PUSH 20 and slid its model of the moved piece
  G7 the grab released (A up), then B picks the piece up: the host APPLIED the PICKUP, the client released the model
  G8 host and client alive
NOT asserted: what the models / animations look like on screen (no image comparison).
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

ITEM = 0x1064  # iam_sum_blue_lowchest01: a 2x1 DRESSER (aFTR_SHAPE_TYPEB_0, storage interaction)
SCRIPT = "grab,pull,push,left,right,release"
STARTED = []
ENV_KEYS = ("AC_TEST_OFURN_SEED", "AC_TEST_OFURN_SEED_NEAR", "AC_TEST_OFURN_DRAWCYCLE", "AC_TEST_OFURN", "AC_TEST_OFURN_GRAB", "PC_PUPPET_DIAG")


def _run(args, rig):
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    os.environ["AC_TEST_OFURN_SEED"] = "0x%04X,0" % ITEM
    os.environ["AC_TEST_OFURN_SEED_NEAR"] = "1"
    os.environ["PC_PUPPET_DIAG"] = "1"
    if args.drawcycle:
        os.environ["AC_TEST_OFURN_DRAWCYCLE"] = "1"
    os.environ.pop("AC_TEST_OFURN", None)
    host = rig.start_host("of2real", args.port, [])
    check("host reached genuine field-ready state", host is not None)
    if host is None:
        return
    STARTED.append(host)
    L.resolve_host_town("127.0.0.1", args.port)
    os.environ.pop("PC_PUPPET_DIAG", None)
    os.environ["AC_TEST_OFURN"] = "grab"
    os.environ["AC_TEST_OFURN_GRAB"] = SCRIPT
    a = rig.start_client("of2realA", args.port, residents[0], [])
    check("real client booted into the field and connected", a is not None)
    if a is None:
        return
    STARTED.append(a)
    m = host.wait_for_log(r"seeded placed piece at \((\d+),(\d+)\) item 0x%04X rot 0 -> placed \(next to peer (\d+)" % ITEM, 120.0)
    check("G1 the host placed a 2x1 dresser next to the real client", m is not None)
    if m is None:
        return
    ux, uz, peer = int(m.group(1)), int(m.group(2)), int(m.group(3))
    lst = a.wait_for_log(r"\[NET\]\[OFURN\] client: (?:C2 list of every placed piece: \d+ piece|C2 SET tile \(%d,%d\))" % (ux, uz), 60.0)
    at = a.log_text()
    check("G1 the client adopted the C2 CAPS (message 102: multi-tile + move) and got the piece in its 102 list / a SET",
          "[NET][OFURN] client: the host supports outdoor furniture C2: multi-tile ON, move (grab / push / pull) ON" in at and lst is not None)
    built = a.wait_for_log(r"\[OFURN\]\[DRAW\] model built: tile \(%d,%d\) item 0x%04X rot 0 shape 3 fp 1" % (ux, uz, ITEM), 60.0)
    check("G2 the client's renderer built the 2x1 MODEL of the dresser (shape TYPEB_0, footprint 2x1)", built is not None)
    col = a.wait_for_log(r"\[OFURN\]\[COL\] collision registered: tile \(%d,%d\) item 0x%04X type \d+ fp 1" % (ux, uz, ITEM), 30.0)
    check("G2 ... and registered its COLLISION box (the 2x1 move-bg type)", col is not None)
    gr = a.wait_for_log(r"\[OFURN\]\[GRAB\] grabbed the piece at \(%d,%d\) item 0x%04X fp 1" % (ux, uz, ITEM), 120.0)
    check("G3 A in front of the piece grabbed it (the real wait-state seam)", gr is not None)
    hs = a.wait_for_log(r"grab: step 1 of .*\(player state 19, grab active 1", 30.0)
    check("G3 the player is in the vanilla HOLD state (main index 19) while A is held", hs is not None)
    pl = a.wait_for_log(r"\[OFURN\]\[GRAB\] PULL dir 0: \(%d,%d\) -> \(%d,%d\) \(request sent" % (ux, uz, ux, uz + 1), 40.0)
    hm1 = host.wait_for_log(r"MOVE APPLIED \[TXN\]: tile \(%d,%d\) item 0x%04X rot 4" % (ux, uz, ITEM), 30.0)
    cm1 = a.wait_for_log(r"\[OFURN\]\[GRAB\] MOVE APPLIED: \(%d,%d\) rot 0 -> \(%d,%d\)" % (ux, uz, ux, uz + 1), 30.0)
    check("G4 stick AWAY = PULL: request sent, the host APPLIED the MOVE (dir S | PULL), the client saw APPLIED", pl is not None and hm1 is not None and cm1 is not None)
    ps = a.wait_for_log(r"\[OFURN\]\[GRAB\] PUSH dir 2: \(%d,%d\) -> \(%d,%d\) \(request sent" % (ux, uz + 1, ux, uz), 40.0)
    hm2 = host.wait_for_log(r"MOVE APPLIED \[TXN\]: tile \(%d,%d\) item 0x%04X rot 2" % (ux, uz + 1, ITEM), 30.0)
    check("G4 stick TOWARDS = PUSH: request sent, the host APPLIED the MOVE (dir N)", ps is not None and hm2 is not None)
    done = a.wait_for_log(r"grab: script done .*; B -> (\d)", 60.0)
    at = a.log_text()
    ht = host.log_text()
    rot_ok = len(re.findall(r"ROTATE APPLIED \[TXN\]: tile \(%d,%d\)" % (ux, uz), ht))
    rot_blocked = len(re.findall(r"\[OFURN\]\[GRAB\] ROTATE: blocked locally", at))
    check("G5 stick SIDEWAYS = ROTATE: %d rotation(s) APPLIED by the host, %d refused locally (swing cell taken / the player's own cell)" % (rot_ok, rot_blocked),
          rot_ok >= 1 or rot_blocked >= 1)
    pd = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player %d state main_index=(\d+)" % peer, ht)
    check("G6 the OBSERVER (host) saw the client's puppet in HOLD 19, PULL 21 and PUSH 20 (main indexes %s)" % sorted(set(pd)),
          "19" in pd and "20" in pd and "21" in pd)
    if re.search(r"\[OFURN\]\[DRAW\] model built: tile \(%d,%d\)" % (ux, uz), ht):
        check("G6 the observer (whose renderer had the piece in range) slid its own model of the moved piece (MOVE -> slide)", "(slides from its old pose)" in ht)
    else:
        print("NOTE - G6b not asserted: the host player stood too far away to draw the piece (renderer range 24 tiles)")
    check("G7 the grab was released (A up)", "[OFURN][GRAB] released the piece" in at)
    pk = host.wait_for_log(r"PICKUP APPLIED \[TXN\]: tile \(%d,%d\) item 0x%04X" % (ux, uz, ITEM), 30.0)
    rel = a.wait_for_log(r"\[OFURN\]\[DRAW\] model released: tile \(%d,%d\)" % (ux, uz), 30.0)
    check("G7 B in front of it (not grabbing): the host APPLIED the PICKUP of the 2x1 and the client released the model", done is not None and pk is not None and rel is not None)
    if args.drawcycle:
        dc = a.wait_for_log(r"\[OFURN\]\[DRAWTEST\]\[TEST-ONLY\] done: (\d+) furniture model", 180.0)
        check("DC the model draw cycle finished on the client", dc is not None)
    check("G8 host and client alive", host.alive() and a.alive())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=38700)
    ap.add_argument("--drawcycle", action="store_true")
    args = ap.parse_args()
    if not (38000 <= args.port <= 38999):
        print("ports 38000-38999 only", file=sys.stderr)
        return 2
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_of"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_of2_<name> copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
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
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
