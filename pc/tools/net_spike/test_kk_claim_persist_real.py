#!/usr/bin/env python3
"""test_kk_claim_persist_real.py - K.K. guest claims survive a HOST RESTART (ID-F6 / WS-03), on REAL game processes (a real host, real guest clients; disposable fixture, its save is restored).

Reuses the harness of test_kk_work_guests_real.py (the TEST-ONLY AC_TEST_KK_CONCERT / AC_TEST_KK_DAY_FILE / AC_TEST_EVNPC_CLAIM hooks: the real client claim TXN, not the on-screen dialogue).
  P1  guest alpha claims K.K.'s song on the concert day: APPLIED, and the host WROTE save/mp/kk_claims.dat (file exists on disk)
  P2  the host is STOPPED and started again (same fixture, same concert day): its log says it loaded the claim file with 1 guest claim (the load is lazy: on the first claim after the restart)
  P3  alpha asks again: REJECTED with EVNPC_DONE (36) -- no second song after the restart
  P4  guest beta (another identity) still gets its song: APPLIED (the persistence did not turn into a global block)
  P5  CONTROL: the host is stopped, kk_claims.dat is deleted, the host starts again: alpha is now APPLIED again -- which shows that P3 was the file's doing and the test can fail
Not covered: the K.K. dialogue on screen (the claim TXN is the exact path its seams call); a crash (kill -9) in the middle of the write (the write itself is an atomic replace, proven in the native selftest).
Usage: python test_kk_claim_persist_real.py [--port 12310]   (about 6-8 minutes)"""
import argparse
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_kk_work_guests_real as KW  # noqa: E402  (sets NET_SPIKE_GAME_BIN to its fixture dir; the fixture is only built in main)
import town_test_support as T  # noqa: E402
import net_spike_lib as L  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12310)
    args = ap.parse_args()
    T.make_fixture(KW.NAME)
    L.require_test_bin_dir()
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    claim_file = os.path.join(save_dir, "mp", "kk_claims.dat")
    snap = os.path.join(os.environ.get("TEMP", "."), "kkpersist_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    R = KW.Rigs(args, results)
    check = R.check
    song = lambda n: {"AC_TEST_EVNPC_CLAIM": "2,%d" % n}  # noqa: E731
    try:
        KW.write_profiles(save_dir)
        host = R.host()
        if host is None:
            return L.summary_and_exit_code(results)
        a = R.guest("alpha", song(1), "p1")
        res = KW.claim_result(a)
        check("P1 guest alpha's claim is APPLIED on the concert day (%s)" % (res,), res is not None and res[0] == "APPLIED")
        time.sleep(2.0)
        check("P1 the host wrote %s (exists: %s, %d bytes)" % (claim_file, os.path.exists(claim_file), os.path.getsize(claim_file) if os.path.exists(claim_file) else -1),
              os.path.exists(claim_file) and os.path.getsize(claim_file) > 16)
        R.stop(a)
        R.stop(host)
        time.sleep(3.0)

        host = R.host()
        if host is None:
            return L.summary_and_exit_code(results)
        # the load happens on first use: make it happen with the guest's own claim below; re-check the line after it
        a2 = R.guest("alpha", song(2), "p3")
        res = KW.claim_result(a2)
        m = re.search(r"\[NET\]\[KK\] host: loaded .*kk_claims\.dat \((\d+) guest claim", host.log_text())
        check("P2 the host loaded the claim file with 1 guest claim when the first claim after the restart arrived (%s)" % (m.group(0)[-70:] if m else "no such line"),
              m is not None and int(m.group(1)) == 1)
        check("P3 alpha asks again after the host restart: REJECTED with EVNPC_DONE (%s)" % (res,), res is not None and res[0] == "REJECTED" and res[1] == KW.EVNPC_DONE)
        b = R.guest("beta", song(3), "p4")
        res = KW.claim_result(b)
        check("P4 guest beta still gets its song after the restart: APPLIED (%s)" % (res,), res is not None and res[0] == "APPLIED")
        R.stop(a2)
        R.stop(b)
        R.stop(host)
        time.sleep(3.0)

        # P5 control: without the file the host forgets (the pre-change behaviour)
        if os.path.exists(claim_file):
            os.remove(claim_file)
        host = R.host()
        if host is None:
            return L.summary_and_exit_code(results)
        L.info("waiting 65 s for the per-address guest-token window")
        time.sleep(65.0)
        a3 = R.guest("alpha", song(4), "p5")
        res = KW.claim_result(a3)
        check("P5 CONTROL: with kk_claims.dat removed alpha is APPLIED again (%s) -- the file is what blocked it in P3" % (res,), res is not None and res[0] == "APPLIED")
        check("all processes alive (host)", host.alive())
    finally:
        R.stop_all()
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
