#!/usr/bin/env python3
"""test_harness_protected_dirs.py - harness safety guard (test-infrastructure; NO game process is ever started).

Asserts that net_spike_lib REFUSES to construct/launch HostProcess / ClientProcess for the PROTECTED pc/build64/bin_talkfix and
for the LIVE pc/build64/bin (live: unless the pre-existing NET_SPIKE_ALLOW_LIVE_BIN=1), that there is NO override for
bin_talkfix, that require_test_bin_dir() agrees, and that the disposable clone / fixture dirs are accepted. Only the refusal
path is exercised: nothing is launched (subprocess.Popen is replaced by a tripwire).
Usage: python test_harness_protected_dirs.py      Exit code 0 = all checks passed."""
import os
import subprocess
import sys

import net_spike_lib as L

BUILD64 = os.path.dirname(L.LIVE_GAME_BIN_DIR)


def refused(fn):
    try:
        fn()
        return None
    except RuntimeError as e:
        return str(e)


def main():
    results = []
    chk = lambda d, c: L.check(d, c, results)

    def tripwire(*a, **k):
        raise AssertionError("subprocess.Popen reached: a game process would have been launched")
    real_popen, subprocess.Popen = subprocess.Popen, tripwire
    saved_env = os.environ.get("NET_SPIKE_ALLOW_LIVE_BIN")
    try:
        talk = os.path.join(BUILD64, "bin_talkfix")
        alt = os.path.join(BUILD64, "..", "build64", "bin_talkfix", "")  # non-canonical spelling of the same dir
        for env_allow in (None, "1"):  # no override may exist for bin_talkfix, with or without the live override
            if env_allow is None:
                os.environ.pop("NET_SPIKE_ALLOW_LIVE_BIN", None)
            else:
                os.environ["NET_SPIKE_ALLOW_LIVE_BIN"] = env_allow
            for label, mk in (("HostProcess", lambda d: L.HostProcess(port=19999, bin_dir=d)),
                              ("ClientProcess", lambda d: L.ClientProcess("127.0.0.1:19999", bin_dir=d))):
                for d in (talk, alt):
                    m = refused(lambda: mk(d))
                    chk("%s(bin_talkfix%s) refused at construction, names the dir (ALLOW_LIVE=%s)" %
                        (label, " alt-spelling" if d is alt else "", env_allow), m is not None and "bin_talkfix" in m)
            m = refused(lambda: L.require_launchable_bin_dir(talk))
            chk("require_launchable_bin_dir(bin_talkfix) refused (ALLOW_LIVE=%s)" % env_allow, m is not None)
            saved = L.GAME_BIN_DIR
            L.GAME_BIN_DIR = talk
            try:
                try:
                    L.require_test_bin_dir()
                    code = None
                except SystemExit as e:
                    code = e.code
            finally:
                L.GAME_BIN_DIR = saved
            chk("require_test_bin_dir() exits 2 for GAME_BIN_DIR=bin_talkfix (ALLOW_LIVE=%s)" % env_allow, code == 2)
        os.environ.pop("NET_SPIKE_ALLOW_LIVE_BIN", None)
        m = refused(lambda: L.HostProcess(port=19999, bin_dir=L.LIVE_GAME_BIN_DIR))
        chk("HostProcess(live bin) refused, names the dir", m is not None and L.LIVE_GAME_BIN_DIR in m)
        m = refused(lambda: L.ClientProcess("127.0.0.1:19999", bin_dir=L.LIVE_GAME_BIN_DIR))
        chk("ClientProcess(live bin) refused", m is not None)
        os.environ["NET_SPIKE_ALLOW_LIVE_BIN"] = "1"
        chk("live bin honours the pre-existing NET_SPIKE_ALLOW_LIVE_BIN=1 (construction only)",
            refused(lambda: L.HostProcess(port=19999, bin_dir=L.LIVE_GAME_BIN_DIR)) is None)
        os.environ.pop("NET_SPIKE_ALLOW_LIVE_BIN", None)
        for name in ("bin_talkfix_clone", "bin_fixture4", "bin_fixture4_ambig", "bin_fixture4_persist"):
            d = os.path.join(BUILD64, name)
            chk("%s accepted (construction)" % name,
                refused(lambda: L.HostProcess(port=19999, bin_dir=d)) is None and
                refused(lambda: L.ClientProcess("127.0.0.1:19999", bin_dir=d)) is None)
        # start() re-checks (a caller may mutate bin_dir after construction): refused before any file or process is touched
        h = L.HostProcess(port=19999, bin_dir=os.path.join(BUILD64, "bin_talkfix_clone"))
        h.bin_dir = talk
        chk("start() re-checks bin_dir (mutated to bin_talkfix -> refused)", refused(h.start) is not None and h.proc is None)
        # the read-only parse helper may still read the protected save
        try:
            ok = len(L.read_test_save_residents(talk)) >= 2
        except Exception as e:  # noqa: BLE001
            ok = False
            print("parse error:", e)
        chk("read-only read_test_save_residents(bin_talkfix) still works", ok)
    finally:
        subprocess.Popen = real_popen
        if saved_env is None:
            os.environ.pop("NET_SPIKE_ALLOW_LIVE_BIN", None)
        else:
            os.environ["NET_SPIKE_ALLOW_LIVE_BIN"] = saved_env
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
