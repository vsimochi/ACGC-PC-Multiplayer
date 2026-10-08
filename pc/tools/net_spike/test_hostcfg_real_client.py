#!/usr/bin/env python3
"""test_hostcfg_real_client.py - batch A: the HOST decides the authoritative wildlife mode (TOWN_SVC_STATE service 4 = HOST_CONFIG), REAL processes,
plus the CLIENT "not connected" notice (A3) driven by a real host loss.

TIER: REAL HOST + REAL CLIENT game processes (no GUI automation, no manual play, no test hook except the existing --bootstrap-resident):
  H1  host  = `AnimalCrossing.exe --host <port> --bootstrap-resident 0 --authoritative-wildlife`
      client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot>`   (NO --authoritative-wildlife)
      -> the client adopts authoritative wildlife from the host's HOST_CONFIG (log marker), before any wildlife snapshot line, no mode change
  H2  host  = same with --no-authoritative-wildlife (the default is ON); client = same WITH --authoritative-wildlife
      -> the client logs that its own flag is IGNORED, adopts authoritative_wildlife=0
      then (A3) the host process is stopped while the client keeps running: after > 3 s the client prints the loud '[NET][NOTICE] ... NOT CONNECTED'
      line (the on-screen notice is drawn by the same condition; rendering itself is NOT asserted, no screenshot automation) and does not crash.
The host-side push (service 4, value, at READY, before the snapshot) is asserted from the host log. Run ONLY on pc\\build64\\bin_fixture4*
(NET_SPIKE_GAME_BIN=<absolute path>, ports 11300+); the fixture save is snapshotted at start and restored before every host launch and at the end.
One real client at a time. Usage: python test_hostcfg_real_client.py [--port 11300] [--only h1,h2]
"""
import argparse
import os
import re
import shutil
import sys

import net_spike_lib as L
import test_txn_real_client as RC


def count(rx, text):
    return len(re.findall(rx, text))


def run(args, results, ip, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    r1 = residents[0]

    def case(tag, port, host_flags, client_flags, want):
        host = rig.start_host(tag, port, host_flags)
        check("%s host reached genuine field-ready state" % tag, host is not None)
        if host is None:
            return None, None
        L.resolve_host_town(ip, port)
        cl = rig.start_client(tag, port, r1, client_flags)
        check("%s real client booted into the field and connected" % tag, cl is not None)
        if cl is None:
            return host, None
        ct = cl.log_text()
        ht = host.log_text()
        adopted = re.search(r"\[NET\]\[HOSTCFG\] client: adopted authoritative_wildlife=(\d) from the host \(previous (\w+); this client's own --authoritative-wildlife=(\d) is ignored\)", ct)
        check("%s the client adopted authoritative_wildlife=%d from the host (previous: unknown; own flag %d is irrelevant)" % (tag, want, 1 if "--authoritative-wildlife" in client_flags else 0),
              adopted is not None and int(adopted.group(1)) == want and adopted.group(2) == "unknown" and int(adopted.group(3)) == (1 if "--authoritative-wildlife" in client_flags else 0))
        check("%s the client applied service 4 (HOSTCFG) with len 8 exactly once" % tag,
              count(r"\[NET\]\[TS\] client: applied service 4 \(HOSTCFG\) seq \d+ digest 0x[0-9A-F]{8} len 8", ct) == 1)
        pushed = re.search(r"\[NET\]\[HOSTCFG\] host: pushed HOST_CONFIG authoritative_wildlife=(\d) seq (\d+) to peer \d+ \(at READY, before the snapshot\)", ht)
        check("%s the host pushed HOST_CONFIG authoritative_wildlife=%d at READY (before the snapshot), exactly once" % (tag, want),
              pushed is not None and int(pushed.group(1)) == want and count(r"\[NET\]\[HOSTCFG\] host: pushed HOST_CONFIG", ht) == 1)
        snap = re.search(r"\[NET\]\[WILDLIFE\] client: (?:wildlife snapshot|WILDLIFE_SNAPSHOT)", ct)
        check("%s the mode was adopted BEFORE the first wildlife snapshot line (if the host sent one)" % tag,
              adopted is not None and (snap is None or adopted.start() < snap.start()))
        check("%s no mode CHANGE, no refused / unknown TOWN_SVC_STATE" % tag,
              "wildlife mode changed" not in ct and "refused" not in "".join(re.findall(r"\[NET\]\[TS\] client: TOWN_SVC_STATE service 4[^\n]*", ct))
              and "reserved / unknown" not in ct)
        if "--authoritative-wildlife" in client_flags:
            check("%s the client logged that its own --authoritative-wildlife flag is IGNORED" % tag,
                  "client: --authoritative-wildlife is IGNORED on a client" in ct)
        else:
            check("%s no 'IGNORED' line without the flag" % tag, "is IGNORED on a client" not in ct)
        return host, cl

    if "h1" in args.only:
        host, cl = case("h1", args.port, ["--authoritative-wildlife"], ["--no-authoritative-wildlife"], 1)
        if cl is not None:
            check("H1 client still alive", cl.alive())
            cl.stop()
        if host is not None:
            host.stop()
            rig.host = None

    if "h2" in args.only:
        host, cl = case("h2", args.port + 1, ["--no-authoritative-wildlife"], ["--authoritative-wildlife"], 0)
        if cl is not None:
            ht_off = len(cl.log_text())
            host.stop()
            rig.host = None
            m = cl.wait_for_log(r"\[NET\]\[NOTICE\] client: NOT CONNECTED to the host \(link state \d+, \d+ s\): world actions are disabled until the link is READY \(on-screen notice shown\)",
                                60.0, since_offset=ht_off)
            check("H2/A3 after the host process was stopped the client printed the loud NOT CONNECTED notice (> 3 s, role CLIENT, link != READY)", m is not None)
            check("H2/A3 the client process did not crash while the notice was active", cl.alive())
            ct = cl.log_text()
            check("H2/A3 the notice appeared only AFTER the link was lost (never while READY)",
                  ct.find("[NET][NOTICE] client: NOT CONNECTED") > ct.find("[NET][HOSTCFG] client: adopted"))
            cl.stop()
        elif host is not None:
            host.stop()
            rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11300)
    ap.add_argument("--only", default="h1,h2")
    args = ap.parse_args()
    args.only = set(x.strip() for x in args.only.split(",") if x.strip())
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "hostcfg_real_client_save_snapshot_%d" % os.getpid())
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
