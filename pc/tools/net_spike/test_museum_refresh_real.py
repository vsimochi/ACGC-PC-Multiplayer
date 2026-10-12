#!/usr/bin/env python3
"""test_museum_refresh_real.py - REAL-PROCESS test of the museum fish / insect live refresh.

A REAL host process (fixture bin_fixture4_w6, resident 0) is walked INTO the fish (then the insect) room by the test-only hook
AC_TEST_MUSEUM_ROOM=<fish|insect>,<ms> (goto_other_scene with the museum entrance's own door data). A scripted FakeClient (resident 1) then
donates an undonated fish / insect through the REAL MUSEUM_DONATE transaction (TXN_COMMIT kind 8); the host commits, the donor nibble changes and
the host's fish / insect actor must log '[NET][MUSEUM] <room> room: newly donated <kind> N now on display (live refresh)' for exactly that index,
once. Tier: runtime (real process in the room) + protocol harness (scripted donor). Usage: set NET_SPIKE_GAME_BIN=<...>\\bin_fixture4_w6;
python test_museum_refresh_real.py [--port 16600] [--only fish,insect]"""
import argparse
import os
import re
import shutil
import sys
import time

import net_spike_lib as L
import test_txn_protocol as TP
import test_ts_protocol as TS
from test_txn_protocol import APPLIED, R


def phase(results, ip, port, kind, snap_gci, restore):
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    cat, base, label = ("fish", TS.FISH0, "fish") if kind == "fish" else ("insect", TS.INSECT0, "insect") if kind == "insect" else ("fossil", 0x1EEC, "fossil")
    log_dir = os.path.dirname(os.path.abspath(__file__))
    host = L.HostProcess(port=port, env={"AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1", "AC_TEST_MUSEUM_ROOM": kind + ",3000"},
                         extra_args=["--bootstrap-resident", str(host_slot), "--no-house-sync"],
                         log_path=os.path.join(log_dir, "museum_refresh_%s_host.log" % kind)).start()
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=host_slot)
    L.check("[%s] host reached field-ready state" % kind, ok, results)
    run = TP.Run(ip, port, host, results, snap_gci, host_slot, residents[:3])
    try:
        if not ok:
            return
        L.resolve_host_town(ip, port)
        a = run.ready("A", run.r1)
        g0, blob0 = TS.latest(a, TS.SVC_MUSEUM, 1, 5.0)
        in_room = TS.wait_log(run, r"warping into museum room", 30.0)
        L.check("[%s] hook walked the host player into the museum room" % kind, in_room, results)
        L.pump_sleep(12.0)  # scene wipe + room build + the actors' first moves
        free = TS.free_idx(blob0, cat, 2) if cat != "fossil" else [i for i in range(25) if TS.nib(blob0, "fossil", i) == 0][:2]
        item = base + free[0] if cat != "fossil" else base + 4 * free[0]  # fossils: FTR_DINO_START (0x1EEC) + FTR_NO_2_IDX(index)
        off = len(run.log())
        rid, sent, r = TS.donate(run, a, 3, item)
        run.tx_ok("[%s] MUSEUM_DONATE of an undonated %s" % (kind, kind), r, APPLIED, R["NONE"])
        if cat == "fossil":
            got = TS.wait_log(run, r"\[NET\]\[MUSEUM\] fossil room: the exhibits were rebuilt live \(\d+ changed cell\(s\), fossil bits 0x([0-9A-F]+)\)", 15.0)
            m = re.search(r"fossil room: the exhibits were rebuilt live \((\d+) changed cell\(s\), fossil bits 0x([0-9A-F]+)\)", run.log(off))
            L.check("[fossil] host (standing in the fossil room) rebuilt the exhibits live with fossil %d now set (%s)" % (free[0], m.group(0) if m else None),
                    m is not None and (int(m.group(2), 16) >> free[0]) & 1 == 1 and int(m.group(1)) >= 1, results)
            L.pump_sleep(3.0)
            n = run.n_log(r"fossil room: the exhibits were rebuilt live", off)
            L.check("[fossil] ... exactly once (no rebuild per frame)", n == 1, results)
            L.check("[fossil] host did not crash", host.proc.poll() is None, results)
            return
        got = TS.wait_log(run, r"\[NET\]\[MUSEUM\] %s room: newly donated %s %d now on display \(live refresh\)" % (label, label, free[0]), 15.0)
        L.check("[%s] host (standing in the room) built exactly the newly donated %s %d in place" % (kind, label, free[0]), got, results)
        L.pump_sleep(2.0)
        n = run.n_log(r"\[NET\]\[MUSEUM\] %s room: newly donated %s %d now on display" % (label, label, free[0]), off)
        L.check("[%s] ... exactly once (no rebuild per frame)" % kind, n == 1, results)
        other = run.n_log(r"\[NET\]\[MUSEUM\] %s room: newly donated" % label, off)
        L.check("[%s] ... and no other exhibit was built (%d lines)" % (kind, other), other == 1, results)
        L.check("[%s] host did not crash" % kind, host.proc.poll() is None, results)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("[%s] phase raised %r" % (kind, exc), False, results)
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        host.stop()
        restore()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16600)
    ap.add_argument("--only", default="fish,insect")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: fixture bin_fixture4* only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "museum_refresh_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        for i, kind in enumerate([k for k in args.only.split(",") if k]):
            phase(results, "127.0.0.1", args.port + i, kind, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
