#!/usr/bin/env python3
"""test_evnpc_shared_real.py - SHARED EVENT NPCs (worker E), REAL processes: one REAL host + two REAL client game processes race for the same piece of Crazy Redd's lineup.

TIER: REAL HOST + REAL CLIENTS (no GUI automation: the clients drive the REAL client claim path pc_evc_begin / pc_net_game_ts_poll -> TXN_COMMIT kind 21 -> the host handler
-> TXN_RESULT -> pcnetgame_txn_apply_evc through the TEST-ONLY AC_TEST_EVC_CLAIM hook), on a DISPOSABLE copy NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_ev_<name>>
(ports 21000-21999). Host env AC_TEST_EVNPC_SEED=redd (Redd in town today, a rolled lineup); client env AC_TEST_EVC_CLAIM=1,0 (buy piece 0), AC_TEST_EVC_WALLET=99999.
  R1 the host announced service 9 and each real client adopted it (claims ON)
  R2 client A's purchase is APPLIED: the host committed REDD_BUY once, A applied the host post-image (the piece in a free slot, the wallet minus the host price)
  R3 client B, asking for the SAME piece afterwards (AC_TEST_EVC_ITEM: a lagging view; a current view already shows it sold), is REJECTED SOLD_OUT (50) and keeps its
     Bells (wallet unchanged in its log); B's mirror of the lineup shows the piece sold
  R4 host and clients alive
NOT asserted: the dialogue rows on screen (no GUI automation).
"""
import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ["AC_TEST_EVNPC_SEED"] = "redd"
os.environ["AC_TEST_EVC_CLAIM"] = "1,0"
os.environ["AC_TEST_EVC_WALLET"] = "99999"
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402
import test_txn_real_client as RC  # noqa: E402

RX_DONE = r"\[NET\]\[EVC\]\[TEST-ONLY\] client: claim REDD_BUY (APPLIED|REJECTED) \(reason (\d+), item 0x([0-9A-F]{4})\), wallet now (\d+)"


STARTED = []


def run(args, results, ip, rig):
    try:
        _run(args, results, ip, rig)
    finally:
        for c in STARTED:  # only the processes THIS test started
            try:
                if c.alive():
                    c.stop()
            except Exception:  # noqa: BLE001
                pass


def _run(args, results, ip, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has two non-host residents (slots %s)" % residents, len(residents) >= 2)
    if len(residents) < 2:
        return
    host = rig.start_host("evcreal", args.port, [])
    check("host reached genuine field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    m = host.wait_for_log(r"seeded Redd: lineup 0x([0-9A-F]{4}) 0x([0-9A-F]{4}) 0x([0-9A-F]{4}) prices (\d+) (\d+) (\d+)", 30.0)
    check("the host seeded Redd's lineup", m is not None)
    if m is None:
        return
    piece, price = m.group(1), int(m.group(4))
    a = rig.start_client("evcrealA", args.port, residents[0], [])
    STARTED.append(a) if a is not None else None
    check("real client A booted into the field and connected", a is not None)
    if a is None:
        return
    ca = a.wait_for_log(RX_DONE, 120.0)
    at = a.log_text()
    check("R1 client A adopted service 9 (claims ON)", "[NET][EVC] client: the host's shared event NPC claims are ON" in at)
    check("R2 client A's purchase of piece 0 (0x%s) was APPLIED" % piece, ca is not None and ca.group(1) == "APPLIED" and ca.group(3) == piece)
    check("R2 client A paid exactly the host price (%d): wallet 99999 -> %s" % (price, ca.group(4) if ca else "?"), ca is not None and int(ca.group(4)) == 99999 - price)
    check("R2 the host committed REDD_BUY exactly once", len(re.findall(r"REDD_BUY APPLIED \[TXN\]", host.log_text())) == 1)
    check("R2 client A applied the HOST post-image (no local grant)", re.search(r"\[NET\]\[EVC\] client: REDD_BUY APPLIED \(request \d+\): slot \d+ item 0x%s" % piece, at) is not None)
    os.environ["AC_TEST_EVC_ITEM"] = "0x" + piece  # B's view lags: it asks for the piece A just bought (its mirror would already show it sold)
    b = rig.start_client("evcrealB", args.port, residents[1], [])
    os.environ.pop("AC_TEST_EVC_ITEM", None)
    STARTED.append(b) if b is not None else None
    check("real client B booted into the field and connected", b is not None)
    if b is None:
        return
    cb = b.wait_for_log(RX_DONE, 120.0)
    check("R3 client B's purchase of the SAME piece is REJECTED SOLD_OUT (50) and B keeps its Bells (wallet 99999)",
          cb is not None and cb.group(1) == "REJECTED" and int(cb.group(2)) == 50 and int(cb.group(4)) == 99999)
    check("R3 still ONE commit on the host", len(re.findall(r"REDD_BUY APPLIED \[TXN\]", host.log_text())) == 1)
    check("R4 host and both clients alive", host.alive() and a.alive() and b.alive())
    b.stop()
    a.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=21800)
    args = ap.parse_args()
    if not (21000 <= args.port <= 21999):
        print("ports 21000-21999 only (worker E)", file=sys.stderr)
        return 2
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_<name> copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "evc_real_save_snapshot_%d" % os.getpid())
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
