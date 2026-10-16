#!/usr/bin/env python3
"""test_evnpc_shared_oldhost_real.py - SHARED EVENT NPCs (worker E) old-host compatibility, REAL processes: a NEW client (this tree) on the PUBLISHED 0.2.1 host
(release_0.2.1/build_release/bin/AnimalCrossing.exe, sha256 = BUILD_MANIFEST.json exe_sha256) must never send TXN kind 21 (0.2.1 answers an unknown kind with BAD_SHAPE and a
record violation; three close the peer).
  N1 the OLD 0.2.1 host and the NEW client are in the town
  N2 the client never got service 9 (an old host) and refused its Gracie claim LOCALLY (mode 2 = REFUSE, nothing sent)
  N3 the old host logged NO record violation and no kind-21 commit
  N4 the client is still connected 15 s later (both processes alive)
Fixtures: two disposable copies pc\\build64\\bin_fixture4_ev_old (the 0.2.1 exe) / bin_fixture4_ev_shared (the new exe). Ports 21000-21999.
Usage: python test_evnpc_shared_oldhost_real.py [--oldexe <0.2.1 exe>] [--port 21850]"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402

WS = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
B64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
VIOL = r"\[NET\]\[REC\] host: peer \d+ violation"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oldexe", default=os.path.join(WS, "release_0.2.1", "build_release", "bin", "AnimalCrossing.exe"))
    ap.add_argument("--port", type=int, default=21850)
    args = ap.parse_args()
    if not (21000 <= args.port <= 21999):
        print("ports 21000-21999 only (worker E)", file=sys.stderr)
        return 2
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    man = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(args.oldexe))), "BUILD_MANIFEST.json")
    sha = hashlib.sha256(open(args.oldexe, "rb").read()).hexdigest()
    want = json.load(open(man)).get("exe_sha256") if os.path.exists(man) else None
    check("N0 the old host exe is the PUBLISHED 0.2.1 build (sha256 %s... == manifest)" % sha[:12], want is not None and sha == want)
    new_bin = os.path.join(B64, "bin_fixture4_ev_shared")
    old_bin = os.path.join(B64, "bin_fixture4_ev_old")
    if not os.path.isdir(old_bin):
        shutil.copytree(new_bin, old_bin)
    shutil.copy2(args.oldexe, os.path.join(old_bin, "AnimalCrossing.exe"))
    snaps = {}
    for d in (old_bin, new_bin):
        sd = os.path.join(d, "save")
        snaps[sd] = os.path.join(os.environ.get("TEMP", "."), "evc_old_snap_%d_%d" % (os.getpid(), len(snaps)))
        shutil.copytree(sd, snaps[sd])
    host = client = None
    try:
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0"], env={"AC_TEST_HOOKS": "0"},
                             log_path=os.path.join(HERE, "evcold_host.log"), bin_dir=old_bin).start()
        ok = host.wait_listening(60) and host.boot_to_field(timeout=120, slot=0)
        check("N1 the OLD 0.2.1 host is in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        client = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1"], env={"AC_TEST_HOOKS": "1", "AC_TEST_EVC_CLAIM": "4,0,0"},
                                 log_path=os.path.join(HERE, "evcold_client.log"), bin_dir=new_bin, label="evcold").start()
        ok = client.boot_to_field(timeout=180, slot=1)
        check("N1 the NEW client is in the old host's field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        r = client.wait_for_log(r"\[NET\]\[EVC\]\[TEST-ONLY\] client: claim refused locally \(mode (\d)\)", 90.0)
        ct = client.log_text()
        check("N2 no service 9 from the old host and the claim was refused LOCALLY (mode 2 = REFUSE), nothing sent",
              r is not None and r.group(1) == "2" and "shared event NPC claims are ON" not in ct and "begin DESIGNER_GIFT" not in ct)
        time.sleep(15.0)
        hl = host.log_text()
        check("N3 the old host logged no record violation and no kind-21 commit", re.search(VIOL, hl) is None and "kind=21" not in hl and "BAD_SHAPE" not in hl)
        check("N4 both processes alive, the client still connected", host.alive() and client.alive() and not re.search(r"\[NET\] host: peer \d+ disconnected", hl))
    finally:
        for p in (client, host):
            if p is not None:
                try:
                    p.stop()
                except Exception:  # noqa: BLE001
                    pass
        for sd, snap in snaps.items():
            shutil.rmtree(sd, ignore_errors=True)
            shutil.copytree(snap, sd)
            shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
