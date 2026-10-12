#!/usr/bin/env python3
"""test_able_refresh_real.py - REAL-PROCESS test of the Able Sisters live refresh.
A real host is walked into the Able Sisters shop (AC_TEST_MUSEUM_ROOM=needlework, goto_other_scene). A scripted FakeClient sends PAGE (msg 71) kind DESIGN
writes: the host applies them to its Save while its player stands in the shop and its Needlework_Indoor actor must log '[NET][ABLE] mannequin N / umbrella N
redrawn from the shared design (live refresh)' exactly once per changed design, and nothing when the same design is re-sent.
Design index 0..3 = mannequin 0..3, 4..7 = umbrella 0..3. Usage: NET_SPIKE_GAME_BIN=<...>\\bin_fixture4_w6 python test_able_refresh_real.py [--port 16620]"""
import argparse
import os
import shutil
import sys

import net_spike_lib as L
import test_txn_protocol as TP
import test_ts_protocol as TS
import test_town_pages_protocol as PG


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16620)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: fixture bin_fixture4* only", file=sys.stderr)
        return 2
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    snap = os.path.join(os.environ.get("TEMP", "."), "able_refresh_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    host = L.HostProcess(port=args.port, env={"AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1", "AC_TEST_MUSEUM_ROOM": "needlework,3000"},
                         extra_args=["--bootstrap-resident", str(host_slot), "--no-house-sync"],
                         log_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "able_refresh_host.log")).start()
    a = None
    try:
        ok = host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=host_slot)
        ck("host reached field-ready state", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town(args.ip if hasattr(args, "ip") else "127.0.0.1", args.port)
        a = L.FakeClient("A", "127.0.0.1", args.port, player=L.resident_player(residents[0]))
        a.rec_resident_idx = residents[0]
        a.connect_and_ready(quiet=True)
        pages = PG.wait_pages(a)
        ck("client received all 23 pages", len(pages) == 23)
        ck("hook walked the host player into the Able Sisters shop", TS.wait_log(type("R", (), {"n_log": lambda s, rx, off=0: len(__import__("re").findall(rx, host.log_text()[off:]))})(), r"warping into museum room 5", 30.0))
        L.pump_sleep(12.0)  # wipe + room build + actor first moves
        rx = lambda rx: len(__import__("re").findall(rx, host.log_text()))  # noqa: E731
        base_all = rx(r"\[NET\]\[ABLE\]")
        for idx, what, label in ((2, "mannequin 2", r"mannequin 2 redrawn"), (5, "umbrella 1", r"umbrella 1 redrawn")):
            key = (PG.DESIGN, idx)
            r0 = PG.pages(a)[key][0]
            d = PG.design("ABLE%d" % idx, palette=4 + idx)
            n0 = rx(r"\[NET\]\[ABLE\] " + label)
            m = a.inbox.mark()
            PG.write(a, PG.DESIGN, idx, r0, d)
            L.pump_sleep(1.0)
            ck("PAGE write of design %d APPLIED by the host" % idx, any(x[1] & PG.F_APPLIED for x in PG.replies(a, PG.DESIGN, idx, m)))
            L.pump_sleep(3.0)
            n1 = rx(r"\[NET\]\[ABLE\] " + label)
            ck("host in the shop logged '%s redrawn' exactly once for the changed design (%d -> %d)" % (what, n0, n1), n1 == n0 + 1)
            # re-send the same content on the new revision: nothing changes -> no new log
            r1 = PG.wait_rev(a, key, r0 + 1)[0]
            PG.write(a, PG.DESIGN, idx, r1, d)
            L.pump_sleep(4.0)
            ck("re-sending the SAME design logs nothing (%d)" % rx(r"\[NET\]\[ABLE\] " + label), rx(r"\[NET\]\[ABLE\] " + label) == n1)
        ck("exactly two refresh lines in total (no other mannequin/umbrella touched): %d" % (rx(r"\[NET\]\[ABLE\]") - base_all), rx(r"\[NET\]\[ABLE\]") - base_all == 2)
        ck("host did not crash", host.proc.poll() is None)
    finally:
        if a is not None:
            try:
                a.close()
            except Exception:  # noqa: BLE001
                pass
        host.stop()
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
