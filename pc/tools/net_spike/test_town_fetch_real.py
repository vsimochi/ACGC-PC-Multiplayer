#!/usr/bin/env python3
"""test_town_fetch_real.py - M-B TOWN TRANSFER, CLIENT half (the PRE-BOOT fetch) with REAL host and client processes.

TIER: REAL HOST + REAL CLIENT game processes (no GUI automation):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0 [--town-serve on]`   from the disposable pc\\build64\\bin_fixture4_town (a copy of bin_fixture4)
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --town-fetch --bootstrap-resident 1`   from the disposable pc\\build64\\bin_fixture4_townclient whose
           save dir is EMPTY (no save/card_a at all): the town can only come from the fetch.
The live save dir, bin_talkfix* and bin_fixture4 are never launched or modified (both dirs are fresh copies, basename bin_fixture4*, so the harness' whole-save
snapshot / restore applies).

  R1  host --town-serve full (M-G: on = sanitized, see test_town_sanitize_real.py): the client fetches the town into save/mp/towns/<townkey>/card_a, boots from it and reaches READY (snapshot applied) with NO LAND_MISMATCH;
      the cache GCI md5 == the host's GCI md5 (taken before the client started) and is NOT written by the client; nothing staged in card_a (.tmp / .part / extra .gci);
      origin.ini names the server; no save/card_a is created in the client dir
  R2  host with town_serve OFF (default): the same client (cache present) falls back to the cached town (log line) and reaches READY again (offline-capable cache)
  R3  cache deleted, host still OFF, AC_TOWN_NO_MSGBOX=1 (a modal box cannot be dismissed headlessly): the client logs 'NO USABLE TOWN', exits 3 (no wrong town is booted)
  R4  nothing listens at the address (host gone), no cache: the fetch gives up within ~3 s of the connect (TOWN_INFO wait) -> 'NO USABLE TOWN', exit 3. The time is measured
      RELATIVE to R3 (same start-up cost) and must be 2..7 s longer.
Usage: python test_town_fetch_real.py [--port 11870]
"""
import argparse
import glob
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_town")
CLIENT_DIR = os.path.join(T.BUILD64, "bin_fixture4_townclient")
if __name__ == "__main__":
    T.make_fixture("bin_fixture4_town")
    T.make_fixture("bin_fixture4_townclient", empty_save=True)

import net_spike_lib as L  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
HOST_GCI = os.path.join(HOST_DIR, L.SAVE_GCI_REL)
CLIENT_TOWNS = os.path.join(CLIENT_DIR, "save", "mp", "towns")


def start_host(port, tag, extra):
    for i in range(3):
        h = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"] + list(extra), log_path=T.log_path("town_real_host_%s_try%d.log" % (tag, i)),
                          bin_dir=HOST_DIR).start()
        if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
            return h
        h.stop()
        time.sleep(2.0)
    return None


def start_client(port, tag, extra=(), env=None):
    return L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--town-fetch", "--bootstrap-resident", "1"] + list(extra), log_path=T.log_path("town_real_client_%s.log" % tag),
                           bin_dir=CLIENT_DIR, env=env, label="townclient").start()


def run(port, results):
    """R1..R4 (see the module docstring). The hosts of R1 / R2 / R3 all listen on `port` one after the other (the cache is keyed by address:port); R4 uses port + 3."""
    L.require_test_bin_dir()

    class A:  # noqa: D106
        pass
    args = A()
    args.port = port
    ck = lambda d, c: L.check(d, c, results)  # noqa: E731
    host = client = None
    try:
        # ---------------- R1
        host = start_host(args.port, "on", ["--town-serve", "full"])  # M-G: R1 compares the cache md5 with the host GCI -> the FULL image
        ck("R1 host (--town-serve full) started and booted to the field", host is not None)
        if host is None:
            return
        host_md5 = T.md5_file(HOST_GCI)
        ck("R1 the client dir has NO save/card_a (empty save dir)", not os.path.exists(os.path.join(CLIENT_DIR, "save", "card_a")))
        client = start_client(args.port, "r1")
        ok = client.boot_to_field(timeout=170.0, slot=1)
        ctext, htext = client.log_text(), host.log_text()
        ck("R1 the client fetched the town: '[NET][TOWN] fetch: installed town <key> (467008 bytes ...' ", re.search(r"\[NET\]\[TOWN\] fetch: installed town ([0-9a-f_]{30}) \(467008 bytes", ctext) is not None)
        ck("R1 the client booted from the fetched town, bound resident 1 and applied the host snapshot (READY, snapshot applied)", ok and "-> READY" in ctext)
        ck("R1 NO LAND_MISMATCH on either side", "LAND_MISMATCH" not in ctext and "LAND_MISMATCH" not in htext and "does not match your town" not in ctext)
        ck("R1 the host served it: STREAM + TOWN_DONE status 0", "STREAM 467008 bytes" in htext and re.search(r"TOWN_DONE status 0", htext) is not None)
        gcis = glob.glob(os.path.join(CLIENT_TOWNS, "*", "card_a", "DobutsunomoriP_MURA.gci"))
        ck("R1 exactly one cached town: save/mp/towns/<townkey>/card_a/DobutsunomoriP_MURA.gci", len(gcis) == 1)
        if gcis:
            cache = gcis[0]
            card_dir = os.path.dirname(cache)
            town_dir = os.path.dirname(card_dir)
            ck("R1 the cached GCI md5 == the host's GCI md5 (%s)" % host_md5, T.md5_file(cache) == host_md5)
            ck("R1 nothing but the GCI in card_a (no .tmp / .part / extra .gci staged there)", sorted(os.listdir(card_dir)) == ["DobutsunomoriP_MURA.gci"])
            origin = open(os.path.join(town_dir, "origin.ini")).read()
            ck("R1 origin.ini records the server address, port and a fetch time", "address = 127.0.0.1" in origin and ("port = %d" % args.port) in origin and "last_fetch = " in origin)
            ck("R1 the town dir name is the townkey (16 hex _ 4 hex _ 8 hex)", re.fullmatch(r"[0-9a-f]{16}_[0-9a-f]{4}_[0-9a-f]{8}", os.path.basename(town_dir)) is not None)
            cache_md5_running = T.md5_file(cache)
        else:
            cache_md5_running = None
        client.stop()
        client = None
        ck("R1 the client never wrote the cache GCI (md5 unchanged after the session)", gcis and T.md5_file(gcis[0]) == cache_md5_running == host_md5)
        ck("R1 the client dir still has no save/card_a", not os.path.exists(os.path.join(CLIENT_DIR, "save", "card_a")))
        host.stop()
        host = None

        # ---------------- R2: town_serve OFF (default) + a cache -> fallback to the cache
        host = start_host(args.port, "off", [])
        ck("R2 host (town_serve default OFF) started and booted to the field", host is not None)
        if host is None:
            return
        client = start_client(args.port, "r2")
        ok = client.boot_to_field(timeout=170.0, slot=1)
        ctext, htext = client.log_text(), host.log_text()
        ck("R2 host log: REFUSED (town_serve is off ...)", "REFUSED (town_serve is off" in htext)
        ck("R2 client log: 'the host refused the town transfer' and 'fallback: using the cached town'", "the host refused the town transfer" in ctext and "fallback: using the cached town" in ctext)
        ck("R2 the client booted from the cached town and reached READY with no LAND_MISMATCH", ok and "-> READY" in ctext and "LAND_MISMATCH" not in ctext and "LAND_MISMATCH" not in htext)
        client.stop()
        client = None
        host.stop()
        host = None

        # ---------------- R3 / R4: no cache -> no usable town, exit 3 (no message box)
        shutil.rmtree(os.path.join(CLIENT_DIR, "save", "mp"), ignore_errors=True)
        host = start_host(args.port, "off2", [])
        ck("R3 host (town_serve OFF) started", host is not None)
        if host is None:
            return
        t0 = time.monotonic()
        client = start_client(args.port, "r3", env={"AC_TEST_HOOKS": "1", "AC_TOWN_NO_MSGBOX": "1"})
        rc3 = client.wait_exit(90.0)
        d3 = time.monotonic() - t0
        ctext = client.log_text()
        ck("R3 refused + no cache + no legacy save: 'fallback: NO USABLE TOWN' and exit code 3 (took %.1f s)" % d3, rc3 == 3 and "fallback: NO USABLE TOWN" in ctext and "refused the town transfer" in ctext)
        ck("R3 nothing was booted / written: no save dir content in the client dir", not os.path.exists(os.path.join(CLIENT_DIR, "save", "card_a")) and not glob.glob(os.path.join(CLIENT_TOWNS, "*", "card_a", "*.gci")))
        client.stop()
        client = None
        host.stop()
        host = None
        # R4: nobody listens
        t0 = time.monotonic()
        client = start_client(args.port + 3, "r4", env={"AC_TEST_HOOKS": "1", "AC_TOWN_NO_MSGBOX": "1"})
        rc4 = client.wait_exit(90.0)
        d4 = time.monotonic() - t0
        ctext = client.log_text()
        ck("R4 no host: 'no TOWN_INFO ... within 3000 ms' then 'NO USABLE TOWN' and exit 3 (took %.1f s)" % d4, rc4 == 3 and "within 3000 ms" in ctext and "fallback: NO USABLE TOWN" in ctext)
        ck("R4 the give-up took ~3 s longer than the immediate refusal of R3 (%.1f s vs %.1f s: expected +2..+7 s)" % (d4, d3), 2.0 <= d4 - d3 <= 7.0)
    finally:
        for p in (client, host):
            if p is not None:
                p.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11870)
    args = ap.parse_args()
    results = []
    run(args.port, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
