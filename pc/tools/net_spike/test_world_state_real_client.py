#!/usr/bin/env python3
"""test_world_state_real_client.py - ONE real two-process verification that the WORLD STATE (weather, Stalk Market / turnip prices, game clock) of a real
client matches the host's.

TIER: REAL HOST + REAL CLIENT game processes (no GUI automation, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0 -v --world-test-force=1,2,3,100,37`   (TEST-ONLY hook, default off, loud: once the host world is
           ready it forces weather = RAIN (1) intensity 2, Stalk Market trend 3, daily prices 100..106 and shifts the HOST game clock by +37 minutes)
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot>`
The client joins after the forced values exist, so it receives them through the join snapshot (SNAPSHOT_END world state) and CLOCK_SYNC. Asserted by comparing the
two process logs: weather type / intensity, trend + the seven prices, and the game CLOCK (the time the client's clock is corrected to == the host's own time at the
CLOCK_SYNC, and it is ~37 minutes ahead of the client's previous local time; weekday and the Kabu price of that weekday agree). Run ONLY on pc\\build64\\bin_fixture4*
(NET_SPIKE_GAME_BIN=<absolute path>, ports 11600+); the fixture save is snapshotted at start and restored before the host launch and at the end.
Usage: python test_world_state_real_client.py [--port 11600]
"""
import argparse
import datetime
import os
import re
import shutil
import sys

import net_spike_lib as L
import test_txn_real_client as RC


def last(rx, text):
    ms = re.findall(rx, text)
    return ms[-1] if ms else None


def run(args, results, ip, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    host = rig.start_host("ws", args.port, ["-v", "--world-test-force=1,2,3,100,37"])
    check("host reached genuine field-ready state", host is not None)
    if host is None:
        return
    forced = host.wait_for_log(r"\[NET\]\[WORLD\]\[TEST-ONLY\] --world-test-force: host forced weather=1 intensity=2 trend=3 price_sunday=100 "
                               r"\(price\[d\]=100\+d\) clock_shift_min=37", 30.0)
    check("the host test hook forced weather 1/2, trend 3, prices 100+d and a +37 min clock shift (loud TEST-ONLY log, once)", forced is not None)
    L.resolve_host_town(ip, args.port)
    cl = rig.start_client("ws", args.port, residents[0], [])
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"\[NET\]\[CLOCK\] client: post-correction weekday=", 30.0)
    cl.wait_for_log(r"client: snapshot-end host Stalk Market schedule", 30.0)
    ct, ht = cl.log_text(), host.log_text()

    hw = last(r"\[NET\]\[WORLD\] host: weather changed -> type (\d+) intensity (\d+)", ht)
    cw = last(r"\[NET\]\[WORLD\] client: (?:snapshot-end )?host weather -> type (\d+) intensity (\d+)", ct)
    check("weather: the client's applied weather %s == the host's last broadcast %s == the forced value (1, 2)" % (cw, hw),
          hw is not None and cw == hw and cw == ("1", "2"))
    hm = last(r"\[NET\]\[WORLD\] host: Stalk Market schedule changed -> trend (\d+) sunday (\d+)", ht)
    cm = re.search(r"client: snapshot-end host Stalk Market schedule -> trend (\d+) sunday (\d+) prices ((?:\d+,){6}\d+)", ct)
    check("turnip market: trend + Sunday price %s on the client == the host's %s == forced (3, 100)" % (cm.groups()[:2] if cm else None, hm),
          hm is not None and cm is not None and (cm.group(1), cm.group(2)) == hm and hm == ("3", "100"))
    check("turnip prices: the client holds all seven daily prices 100..106 (host forced price[d] = 100 + d)",
          cm is not None and cm.group(3) == "100,101,102,103,104,105,106")

    hc = re.search(r"\[NET\]\[CLOCK\] host: initial CLOCK_SYNC sent to peer \d+ \(clock_seq \d+\) -- host time (\d+)-(\d+)-(\d+) (\d+):(\d+):(\d+) "
                   r"weekday=(\d) Kabu price \(from schedule\)=(\d+)", ht)
    cc = re.search(r"\[NET\]\[CLOCK\] client: applying host clock correction \(clock_seq \d+[^)]*\) local time (\d+)-(\d+)-(\d+) (\d+):(\d+):(\d+) -> "
                   r"(\d+)-(\d+)-(\d+) (\d+):(\d+):(\d+)", ct)
    cp = re.search(r"\[NET\]\[CLOCK\] client: post-correction weekday=(\d) Kabu price \(from schedule\)=(\d+)", ct)
    check("clock: both sides logged the CLOCK_SYNC (host initial send, client correction + post-correction line)", hc is not None and cc is not None and cp is not None)
    if hc and cc and cp:
        h_t = datetime.datetime(*[int(x) for x in hc.groups()[:6]])
        c_before = datetime.datetime(*[int(x) for x in cc.groups()[:6]])
        c_after = datetime.datetime(*[int(x) for x in cc.groups()[6:12]])
        check("clock: the time the client's clock was corrected TO (%s) equals the host's own game time at the sync (%s), to the second" % (c_after, h_t), c_after == h_t)
        delta = (c_after - c_before).total_seconds()
        check("clock: the correction moved the client's clock ahead by ~37 minutes (the host's forced shift): %.0f s (accepted 37 min +/- 3 min)" % delta,
              abs(delta - 37 * 60) <= 180)
        check("clock: weekday and the Kabu price of that weekday agree (host %s/%s, client %s/%s) and equal 100 + weekday"
              % (hc.group(7), hc.group(8), cp.group(1), cp.group(2)),
              (hc.group(7), hc.group(8)) == (cp.group(1), cp.group(2)) and int(hc.group(8)) == 100 + int(hc.group(7)))
    check("client and host still alive", cl.alive() and host.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11600)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "world_state_real_client_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        run(args, results, ip, rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
