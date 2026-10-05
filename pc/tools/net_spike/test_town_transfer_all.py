#!/usr/bin/env python3
"""test_town_transfer_all.py - ONE run of the whole M-B town transfer process tier: the scripted-FakeClient protocol test against real hosts
(test_town_transfer_protocol.py: P1, P2) followed by the real host + real client test (test_town_fetch_real.py: R1..R4). Both parts use fresh disposable copies of
pc\\build64\\bin_fixture4 (bin_fixture4_town for the hosts, bin_fixture4_townclient with an EMPTY save dir for the real client); bin_fixture4 itself, the live save dir and
bin_talkfix* are never launched or modified.
Usage: python test_town_transfer_all.py [--port 11850] [--only P,R]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_town")
T.make_fixture("bin_fixture4_town")
T.make_fixture("bin_fixture4_townclient", empty_save=True)

import net_spike_lib as L  # noqa: E402
import test_town_fetch_real as R  # noqa: E402
import test_town_transfer_protocol as P  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11850)
    ap.add_argument("--only", default="P,R")
    args = ap.parse_args()
    results = []
    if "P" in args.only.split(","):
        P.run(args.port, results)
    if "R" in args.only.split(","):
        if "P" in args.only.split(","):  # the protocol hosts may have re-saved / written save/mp: start the real phase from pristine copies again
            T.make_fixture("bin_fixture4_town")
            T.make_fixture("bin_fixture4_townclient", empty_save=True)
        R.run(args.port + 20, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
