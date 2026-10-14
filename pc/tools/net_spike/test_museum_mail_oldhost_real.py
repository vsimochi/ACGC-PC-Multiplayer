#!/usr/bin/env python3
"""test_museum_mail_oldhost_real.py - release 0.2.0 old-host compatibility: a REAL 0.2.0 client sending a museum letter with a gift (MUSEUM_MAIL, TXN kind 19)
to the REAL released 0.1.1 host program must not cost a record-protocol violation (the 0.1.1 host closes a peer at its third one).

A 0.1.1 host does not know kind 19: it answers BAD_SHAPE and counts a violation (test_old_host_compat_src.py O3). The fixed client sends kind 19 only to a host that
announced town service 6 (a 0.1.1 host never does) and refuses the letter locally (the post girl's existing refusal row; here the test hook logs "refused locally").
  N1 the OLD host (0.1.1 exe, released package) and the NEW client (this tree's test-hook build) are in the town
  N2 the client's hook wrote a museum letter with a gift and the request was refused LOCALLY (nothing sent)
  N3 the old host logged NO record violation and NO kind-19 / MAIL_SEND commit from that peer
  N4 the client is still connected 20 s later (both processes alive, no disconnect line on the host)
With --before <exe> the SAME scenario runs first with a client build WITHOUT the fix and must show the hazard (B1: the host logged a violation for it).
Fixtures: two disposable copies pc\\build64\\bin_fixture4_mmold / _mmnew of the 4-resident fixture (town_test_support.make_fixture), exes replaced.
Usage: python test_museum_mail_oldhost_real.py --oldexe <0.1.1 AnimalCrossing.exe> [--newexe <exe>] [--before <pre-fix exe>] [--port 18480]"""
import argparse
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import town_test_support as T  # noqa: E402
import net_spike_lib as L  # noqa: E402

VIOL = r"\[NET\]\[REC\] host: peer \d+ violation"


def fixture(name, exe):
    d = T.make_fixture(name) or os.path.join(T.BUILD64, name)
    shutil.copy2(exe, os.path.join(d, "AnimalCrossing.exe"))
    return d


def scenario(tag, port, old_bin, new_bin, check, expect_fixed):
    host = client = None
    try:
        host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"], env={"AC_TEST_HOOKS": "0"},
                             log_path=os.path.join(HERE, "mmold_%s_host.log" % tag), bin_dir=old_bin).start()
        ok = host.wait_listening(60) and host.boot_to_field(timeout=120, slot=0)
        check("%s1 the OLD 0.1.1 host is in the field" % tag, ok)
        if not ok:
            return
        client = L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--bootstrap-resident", "1", "--mail-test-send=museum,gift"], env={"AC_TEST_HOOKS": "1"},
                                 log_path=os.path.join(HERE, "mmold_%s_client.log" % tag), bin_dir=new_bin, label="mm" + tag).start()
        ok = client.boot_to_field(timeout=180, slot=1)
        check("%s1 the client is in the old host's field" % tag, ok)
        if not ok:
            return
        wrote = client.wait_for_log(r"--mail-test-send=museum,gift: wrote a letter into mail slot \d+ for house -2 present 0x[0-9A-F]{4}", 90.0)
        check("%s2 the client's hook wrote a museum letter with a gift" % tag, wrote is not None)
        if expect_fixed:
            check("N2 the request was refused LOCALLY (kind 19 is never sent to a host without town service 6)",
                  client.wait_for_log(r"--mail-test-send: the request was refused locally", 30.0) is not None
                  and "begin MAIL_SEND request=" not in client.log_text())
            time.sleep(20.0)
            hl = host.log_text()
            check("N3 the old host logged no record violation and no MAIL_SEND / unknown-kind commit", re.search(VIOL, hl) is None and "MAIL_SEND slot" not in hl
                  and "malformed TXN_COMMIT" not in hl)
            check("N4 20 s later both processes are alive and the host saw no disconnect", host.alive() and client.alive() and "disconnected" not in hl
                  and "closed after" not in hl)
        else:
            got = host.wait_for_log(VIOL, 60.0)
            check("B1 (before the fix) the old host answered kind 19 BAD_SHAPE and counted a record violation: %s" % (got or "-"), got is not None
                  and "malformed TXN_COMMIT" in host.log_text())
    finally:
        if client is not None:
            client.stop()
        if host is not None:
            host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oldexe", required=True, help="AnimalCrossing.exe of the RELEASED 0.1.1 package")
    ap.add_argument("--newexe", default=os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe"), help="the fixed client (a test-hook build)")
    ap.add_argument("--before", default=None, help="optional: a test-hook client build WITHOUT the fix (shows the hazard first)")
    ap.add_argument("--port", type=int, default=18480)
    a = ap.parse_args()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    old_bin = fixture("bin_fixture4_mmold", a.oldexe)
    if a.before:
        scenario("B", a.port, old_bin, fixture("bin_fixture4_mmnew", a.before), check, expect_fixed=False)
        old_bin = fixture("bin_fixture4_mmold", a.oldexe)  # a fresh host save for the fixed run
    scenario("N", a.port + 1, old_bin, fixture("bin_fixture4_mmnew", a.newexe), check, expect_fixed=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
