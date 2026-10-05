#!/usr/bin/env python3
"""test_client_reconnect_real.py - client auto-reconnect, REAL host + REAL client processes (no wire change).

  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident 1`

  T1  resident: client READY + record adopted -> the host process is killed -> '[NET][RECONNECT] ... host link lost', >= 2
      attempts with growing delays, the client stays alive (role CLIENT, no shutdown) -> the host is restarted on the same
      port + save -> 'reconnect successful ... resident 1', HELLO with have_last=1 (lineage kept), exactly ONE new host puppet,
      no 'CANNOT JOIN'.
  T2  permanent refusal: the host is killed again and restarted with --bootstrap-resident 1 (it answers REJECT SERVER_FULL
      to a client claiming resident 1) -> 'permanent refusal', no further attempts for the observation window, client alive.

Run ONLY on a disposable fixture copy (NET_SPIKE_GAME_BIN=<abs path of a bin_fixture4* dir>, e.g. pc/build64/bin_fixture4_reconnect,
a copy of bin_fixture4 holding the exe under test). The fixture save is snapshotted at start and restored at the end.
Usage: python test_client_reconnect_real.py [--port 9830] [--quiet-s 30]
"""
import argparse
import os
import re
import shutil
import sys
import time

import net_spike_lib as L

HERE = os.path.dirname(os.path.abspath(__file__))


def count(rx, text):
    return len(re.findall(rx, text))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9830)
    ap.add_argument("--quiet-s", type=float, default=30.0)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4* copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    check = lambda d, c: L.check(d, c, results)  # noqa: E731
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "client_reconnect_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    host = cl = None
    n_host = [0]

    def start_host(res):
        for _ in range(3):
            n_host[0] += 1
            h = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(res)],
                              log_path=os.path.join(HERE, "client_reconnect_host%d.log" % n_host[0]), bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=res):
                return h
            h.stop()
            time.sleep(2.0)
        return None

    try:
        host = start_host(0)
        check("host reached field-ready", host is not None)
        if host is None:
            return L.summary_and_exit_code(results)
        cl = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1"],
                             log_path=os.path.join(HERE, "client_reconnect_client.log"), bin_dir=L.GAME_BIN_DIR, label="rc").start()
        check("client booted into the field", cl.boot_to_field(timeout=90.0, slot=1))
        m = cl.wait_for_log(r"\[NET\]\[REC\] client: adopted rev=", 60.0)
        check("client READY + record adopted", m is not None)
        time.sleep(3.0)  # let the record reach SYNCED and the host puppet exist
        off0 = len(cl.log_text())

        # ---------------- T1 ----------------
        host.stop()
        host = None
        t_kill = time.monotonic()
        check("T1 client logs 'host link lost' (within 15 s of the host kill)",
              cl.wait_for_log(r"\[NET\]\[RECONNECT\] client: host link lost \(cause=", 15.0, since_offset=off0) is not None)
        while time.monotonic() - t_kill < 22.0 and cl.alive():
            time.sleep(0.5)
        txt = cl.log_text()[off0:]
        delays = [int(x) for x in re.findall(r"host link lost \(cause=[^)]*\) -- retry \d+ in (\d+) ms", txt)]
        att = count(r"\[NET\]\[RECONNECT\] client: attempt \d+: HELLO to ", txt)
        check("T1 >= 2 attempts while the host is down (got %d), delays %s grow" % (att, delays),
              att >= 2 and len(delays) >= 2 and all(b > a for a, b in zip(delays, delays[1:])) and delays[0] == 1000)
        check("T1 client still alive ~22 s after the host died, no shutdown, no 'CANNOT JOIN'",
              cl.alive() and "shutting down networking" not in txt and "CANNOT JOIN" not in txt)
        check("T1 no per-frame spam: < 12 RECONNECT lines in 22 s", count(r"\[NET\]\[RECONNECT\]", txt) < 12)

        off1 = len(cl.log_text())
        host = start_host(0)
        check("T1 host restarted (same port + save)", host is not None)
        if host is None:
            return L.summary_and_exit_code(results)
        m = cl.wait_for_log(r"\[NET\]\[RECONNECT\] client: reconnect successful \(attempt (\d+), offline (\d+) ms\) -- identity re-established "
                            r"\(resident: resident 1 \| guest slot 0\)", 120.0, since_offset=off1)
        check("T1 'reconnect successful ... resident 1'", m is not None)
        if m is None:
            return L.summary_and_exit_code(results)
        t_ok = off1 + m.end()
        time.sleep(8.0)
        after = cl.log_text()[t_ok:]
        n_pup = count(r"created remote-player actor for player \d+", after)
        check("T1 HELLO sent with have_last=1 after the reconnect (lineage kept)", "HELLO sent (have_last=1" in after)
        check("T1 exactly one 'created remote-player actor' (host puppet) after success (got %d)" % n_pup, n_pup == 1)
        full = cl.log_text()
        check("T1 no 'CANNOT JOIN' / protocol violation / BAD DIGEST in the client log",
              "CANNOT JOIN" not in full and "protocol violation" not in full and "BAD DIGEST" not in full)
        check("T1 client alive, no second 'host link lost' after success", cl.alive() and "host link lost" not in after)

        # ---------------- T2 ----------------
        off2 = len(cl.log_text())
        host.stop()
        host = None
        check("T2 client sees the second loss",
              cl.wait_for_log(r"\[NET\]\[RECONNECT\] client: host link lost", 15.0, since_offset=off2) is not None)
        host = start_host(1)
        check("T2 host restarted with --bootstrap-resident 1 (its own resident == the client's)", host is not None)
        if host is None:
            return L.summary_and_exit_code(results)
        m = cl.wait_for_log(r"\[NET\]\[RECONNECT\] client: permanent refusal \(([^)]*)\) -- giving up, staying offline", 120.0, since_offset=off2)
        check("T2 client logs 'permanent refusal' (%s)" % (m.group(1) if m else "-"), m is not None)
        if m is None:
            return L.summary_and_exit_code(results)
        base = cl.log_text()
        n_att = count(r"client: attempt \d+: HELLO to ", base)
        time.sleep(args.quiet_s)
        later = cl.log_text()
        check("T2 no further attempts for %.0f s after the refusal" % args.quiet_s,
              count(r"client: attempt \d+: HELLO to ", later) == n_att and count(r"permanent refusal", later) == count(r"permanent refusal", base))
        check("T2 client still running (role CLIENT, not shut down)", cl.alive() and "shutting down networking" not in later[off2:])
    finally:
        for p in (cl, host):
            if p is not None:
                try:
                    p.stop()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
