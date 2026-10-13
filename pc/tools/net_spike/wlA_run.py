#!/usr/bin/env python3
"""wlA_run.py - runs one net_spike test against worker A's build: a fresh disposable fixture pc\\build64\\bin_fixture4_wlA_<name> (copied from the read-only
bin_fixture4 by the test's own make_fixture) that gets pc\\build64_w3\\wlA_AnimalCrossing.exe (a frozen copy of the pc\\build64_w3 build), never the lead's
pc\\build64\\bin exe and never a user folder. Real game windows go to the Samsung display at volume 1.
Usage: python wlA_run.py NAME TEST.py [test args...]"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
name, script, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
fixture = "bin_fixture4_wlA_" + name
os.environ["NET_SPIKE_GAME_BIN"] = os.path.abspath(os.path.join(HERE, "..", "..", "build64", fixture))  # before net_spike_lib is imported anywhere
import town_test_support as T  # noqa: E402

T.NEW_EXE = os.path.join(T.PC, "build64_w3", "wlA_AnimalCrossing.exe")
assert os.path.isfile(T.NEW_EXE), T.NEW_EXE
import wplay_lib as W  # noqa: E402

W.NAME = fixture
W.BIN = os.path.join(T.BUILD64, fixture)
sys.argv = [script] + rest
runpy.run_path(os.path.join(HERE, script), run_name="__main__")
