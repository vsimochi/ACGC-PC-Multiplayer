#!/usr/bin/env python3
"""test_guest_bootstrap_cli.py - Guests G1.1: INVALID --bootstrap-guest ARGUMENTS fail EARLY with a diagnostic (no game is ever started past argument parsing).

TIER: a REAL game binary, but every launch below is REFUSED (exit code 2) inside main() right after the option parsing, before the window opens, the network
starts or any save is loaded, so it is safe and takes milliseconds. The exe is the one of the DISPOSABLE fixture dir (pc\\build64\\bin_fixture4 by default;
NET_SPIKE_GAME_BIN overrides; the live and protected dirs are refused by net_spike_lib). Every launch is `AnimalCrossing.exe --connect 127.0.0.1:<unused port>
--bootstrap-guest <spec>` with a short timeout; a hang is a failure and the child (our own process, killed by its PID through subprocess) never survives.

For every bad spec: exit code 2 (nonzero), a stderr diagnostic that names the wrong part, nothing started (no window: the process is gone within the timeout).
Specs: empty; 3 fields; 7 fields; empty NAME; 9-character NAME; empty LAND; 9-character LAND; PLAYER_ID 0 / 0xFFFF / "abc" / 70000 / empty; LAND_ID 0 / 0xFFFF /
"xyz" / 70000; GENDER 2 / "x"; FACE 8 / "x"; an all-space NAME; a control-character NAME; NAME "SERVER" (reserved); an over-long spec; and the option as the LAST
argument without a value. Also pinned: the role refusal still wins without --connect, and a VALID spec is NOT refused by the validator (it passes the early
check and is refused only by the later, unrelated role rule when --connect is missing: no game is started).
Not launched here: a VALID run with --connect (that starts the game: test_guest_real_client.py does it).
Usage: python test_guest_bootstrap_cli.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402

PORT = "12477"   # nothing listens there and nothing is ever started: the process exits during option validation


def run_exe(args, timeout=20):
    """(exit code, stdout, stderr) of `AnimalCrossing.exe <args>` from the disposable dir; a hang (timeout) -> (None, '', '') after the child was killed."""
    L.require_launchable_bin_dir(L.GAME_BIN_DIR)
    exe = os.path.join(L.GAME_BIN_DIR, L.GAME_EXE_NAME)
    p = subprocess.Popen([exe] + list(args), cwd=L.GAME_BIN_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()   # our own child only, by its handle (never a by-name kill)
        p.communicate()
        return None, "", ""
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def main():
    L.require_test_bin_dir()
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    ctrl = "AB\x7f"   # CHAR_CONTROL_CODE (127): a code the name entry cannot produce
    cases = [
        ("empty spec", "", "wrong field count"),
        ("1 field", "Bella", "wrong field count"),
        ("3 fields", "Bella,HOMETWN,4097", "wrong field count"),
        ("7 fields", "Bella,HOMETWN,4097,23297,0,1,9", "too many fields"),
        ("empty NAME", ",HOMETWN,4097,23297", "NAME is empty"),
        ("9-character NAME", "Bellaaaaa,HOMETWN,4097,23297", "NAME is too long"),
        ("empty LAND", "Bella,,4097,23297", "LAND (the home town name) is empty"),
        ("9-character LAND", "Bella,HOMETOWNN,4097,23297", "LAND (the home town name) is too long"),
        ("PLAYER_ID 0", "Bella,HOMETWN,0,23297", "bad PLAYER_ID"),
        ("PLAYER_ID 0xFFFF", "Bella,HOMETWN,0xFFFF,23297", "bad PLAYER_ID"),
        ("PLAYER_ID abc", "Bella,HOMETWN,abc,23297", "bad PLAYER_ID"),
        ("PLAYER_ID 70000", "Bella,HOMETWN,70000,23297", "bad PLAYER_ID"),
        ("PLAYER_ID empty", "Bella,HOMETWN,,23297", "bad PLAYER_ID"),
        ("LAND_ID 0", "Bella,HOMETWN,4097,0", "bad LAND_ID"),
        ("LAND_ID 0xFFFF", "Bella,HOMETWN,4097,0xFFFF", "bad LAND_ID"),
        ("LAND_ID xyz", "Bella,HOMETWN,4097,xyz", "bad LAND_ID"),
        ("LAND_ID 70000", "Bella,HOMETWN,4097,70000", "bad LAND_ID"),
        ("GENDER 2", "Bella,HOMETWN,4097,23297,2", "bad GENDER"),
        ("GENDER x", "Bella,HOMETWN,4097,23297,x", "bad GENDER"),
        ("FACE 8", "Bella,HOMETWN,4097,23297,0,8", "bad FACE"),
        ("FACE x", "Bella,HOMETWN,4097,23297,1,x", "bad FACE"),
        ("blank (all spaces) NAME", "        ,HOMETWN,4097,23297", "not a valid game player name"),
        ("single-space NAME", " ,HOMETWN,4097,23297", "not a valid game player name"),
        ("control-character NAME", ctrl + ",HOMETWN,4097,23297", "not a valid game player name"),
        ("reserved NAME SERVER", "SERVER,HOMETWN,4097,23297", "reserved for the server observer"),
        ("reserved NAME SERVER (explicit padding)", "SERVER  ,HOMETWN,4097,23297", "reserved for the server observer"),
        ("over-long spec", "Bella,HOMETWN,4097,23297,0,1," + "9" * 120, "too long"),
    ]
    for name, spec, why in cases:
        rc, out, err = run_exe(["--connect", "127.0.0.1:" + PORT, "--bootstrap-guest", spec])
        ck("%s: exits with status 2 within the timeout (no hang, no running client)" % name, rc == 2)
        ck("%s: stderr names the problem ('%s') and the option" % (name, why), "--bootstrap-guest" in err and "REFUSED" in err and why in err)
        ck("%s: nothing was started (no stdout output from the game)" % name, out == "")

    # the option as the last argument (no spec follows) is refused, not silently ignored
    rc, out, err = run_exe(["--connect", "127.0.0.1:" + PORT, "--bootstrap-guest"])
    ck("--bootstrap-guest as the LAST argument (no spec): exit 2 with 'needs a spec argument' on stderr", rc == 2 and "--bootstrap-guest" in err and "needs a spec argument" in err and out == "")

    # the role rule still comes first / unchanged: no --connect -> the CLIENT-only refusal (exit 2), for a bad AND for a valid spec; combination with --bootstrap-resident too
    rc, out, err = run_exe(["--bootstrap-guest", "Bella,HOMETWN,4097,23297"])
    ck("a VALID spec without --connect: refused by the unchanged CLIENT-only rule (exit 2, nothing started)", rc == 2 and "CLIENT-only" in err and out == "")
    rc, out, err = run_exe(["--bootstrap-guest", "SERVER,HOMETWN,4097,23297"])
    ck("a bad spec without --connect: the CLIENT-only role rule is reported first (exit 2)", rc == 2 and "CLIENT-only" in err)
    rc, out, err = run_exe(["--connect", "127.0.0.1:" + PORT, "--bootstrap-resident", "0", "--bootstrap-guest", "Bella,HOMETWN,4097,23297"])
    ck("--bootstrap-resident together with --bootstrap-guest: still refused (exit 2)", rc == 2 and "cannot be combined with --bootstrap-resident" in err)

    # --help still documents the option
    rc, out, err = run_exe(["--help"])
    ck("--help documents --bootstrap-guest and exits 0", rc == 0 and "--bootstrap-guest NAME,LAND,PLAYER_ID,LAND_ID" in out)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
