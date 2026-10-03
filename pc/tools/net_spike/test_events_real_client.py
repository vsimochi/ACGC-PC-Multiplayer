#!/usr/bin/env python3
"""test_events_real_client.py - events milestone: the HOST decides the town's events / visitors (TOWN_SVC_STATE service 5 = EVENT_STATE), REAL processes.

TIER: REAL HOST + REAL CLIENT game processes (no GUI automation, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0 --world-test-force=1,2,3,100,0,1115`   (TEST-ONLY hook: the 6th value pokes the host's
           Wisp date, event_save_common.ghost_day, to Nov 15 four seconds after a client is READY -> a digest change)
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot>`
Asserted from the two process logs: the client applied the host's EVENT_STATE (service 5, len 214) at READY and again after the host's change; the decoded state
lines of host and client are identical in order (the client adopted exactly the host's event state, incl. the poked date); the digest the client applied == the
digest the host logged for that seq; the client's own roll of events is SUPPRESSED (init_special_event / init_weekly_event gate lines, none on the host); the client
re-derived today's event table after a mirror (vanilla new-day path); the client's own event state digest equals the applied host digest at the end (MATCH,
i.e. the local daily derivation leaves the mirrored decision fields alone); nothing refused / crashed. Run ONLY on pc\\build64\\bin_fixture4*
(NET_SPIKE_GAME_BIN=<absolute path>, ports 11500+); the fixture save is snapshotted at start and restored before the host launch and at the end.
NOT asserted (no UI automation): the visible visitors / stalls themselves; their spawn from the mirrored flags is by the unchanged local event code.
Usage: python test_events_real_client.py [--port 11500]
"""
import argparse
import os
import re
import shutil
import sys
import time

import net_spike_lib as L
import test_txn_real_client as RC


def count(rx, text):
    return len(re.findall(rx, text))


def decoded(text, who):
    return [m.group(1) for m in re.finditer(r"\[NET\]\[EVENT\] %s: state ([^\n]*)" % who, text)]


def run(args, results, ip, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    host = rig.start_host("ev", args.port, ["--world-test-force=1,2,3,100,0,1115"])
    check("host reached genuine field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    cl = rig.start_client("ev", args.port, residents[0], [])
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    m = cl.wait_for_log(r"\[NET\]\[EVENT\] client: state [^\n]*ghost_day=0x0B0F", 40.0)
    check("the client adopted the host's CHANGED event state (Wisp date poked to 0x0B0F) pushed after READY", m is not None)
    cl.wait_for_log(r"\[NET\]\[EVENT\] client: local event state digest", 10.0)
    time.sleep(3.0)
    ct, ht = cl.log_text(), host.log_text()
    check("the host poked the test field exactly once",
          count(r"\[NET\]\[EVENT\]\[TEST-ONLY\] --world-test-force: host poked event_save_common.ghost_day = 0x0B0F", ht) == 1)
    ap = re.findall(r"\[NET\]\[TS\] client: applied service 5 \(EVENT\) seq (\d+) digest 0x([0-9A-F]{8}) len 214", ct)
    hd = {int(s): d for s, d in re.findall(r"\[NET\]\[TS\] host: service 5 \(EVENT\) state -> seq (\d+) digest 0x([0-9A-F]{8}) len 214", ht)}
    check("the client applied service 5 (len 214) at least twice (READY + the host change), seqs strictly increasing",
          len(ap) >= 2 and [int(s) for s, _ in ap] == sorted(set(int(s) for s, _ in ap)))
    check("every digest the client applied is the digest the HOST logged for that seq (blob == host state)",
          bool(ap) and all(hd.get(int(s)) == d for s, d in ap))
    hdec, cdec = decoded(ht, "host"), decoded(ct, "client")
    check("the decoded state lines of host and client are IDENTICAL, in order, and the last one carries the poked Wisp date (the client adopted exactly the host's "
          "event state): host %d lines, client %d lines" % (len(hdec), len(cdec)),
          len(cdec) >= 2 and cdec == hdec[len(hdec) - len(cdec):] and "ghost_day=0x0B0F" in cdec[-1])
    check("the client's own roll of events is SUPPRESSED: the init_special_event and init_weekly_event gate lines exist (once each), the host has no such line",
          count(r"\[NET\]\[EVENT\] client: local init_special_event \(special visitor roll\) suppressed", ct) == 1
          and count(r"\[NET\]\[EVENT\] client: local init_weekly_event \(weekly event / Gulliver / Wisp roll\) suppressed", ct) == 1
          and "[NET][EVENT] client:" not in ht)
    check("the client re-derived today's event table through the vanilla new-day path after a mirror (>= 1 time)",
          count(r"\[NET\]\[EVENT\] client: host event state applied -> re-deriving today's event table once", ct) >= 1)
    sc = re.findall(r"\[NET\]\[EVENT\] client: local event state digest 0x([0-9A-F]{8}), last applied host digest 0x([0-9A-F]{8}) \((MATCH|DIFFERS)\)", ct)
    check("the client's OWN event state digest ends equal to the applied host digest (last self-check line MATCH; %d line(s), %d DIFFERS)"
          % (len(sc), sum(1 for x in sc if x[2] == "DIFFERS")), bool(sc) and sc[-1][2] == "MATCH" and bool(ap) and sc[-1][1] == ap[-1][1])
    check("no refused / unknown / stale TOWN_SVC_STATE for service 5 on the client, no WARNING about the host's own event state",
          not re.search(r"TOWN_SVC_STATE service 5[^\n]*(refused|stale|stashed)", ct) and "event state would be REFUSED" not in ht)
    check("client and host still alive", cl.alive() and host.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11500)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "events_real_client_save_snapshot_%d" % os.getpid())
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
