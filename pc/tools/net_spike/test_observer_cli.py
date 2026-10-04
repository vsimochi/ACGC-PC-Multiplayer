#!/usr/bin/env python3
"""test_observer_cli.py - --host-observer ARGUMENT VALIDATION (no game is ever started past argument parsing).

TIER: a REAL game binary, but every launch below is REFUSED (exit code 2) or answered (--help, exit code 0) inside main() before stdout is
redirected, the window opens or anything is initialised, so it is safe and takes milliseconds. The exe is the one of the DISPOSABLE fixture dir
(pc\\build64\\bin_fixture4 by default; NET_SPIKE_GAME_BIN overrides; the live and protected dirs are refused by net_spike_lib).

Checks (exit codes + stderr text):
  H1  --help mentions --host-observer (and its HOST-only / exclusivity rules) and exits 0
  R1  --host-observer without --host                          -> exit 2, HOST-only message (also with --connect: a client)
  R2  --host --host-observer --bootstrap-resident N           -> exit 2 (the observer plays no resident); the order of the two flags does not matter
  R3  --host --host-observer --bootstrap-guest ...            -> exit 2 (a client-only test hook); --connect --bootstrap-guest --host-observer -> exit 2
  R4  --host --host-observer plus each host-self test hook that needs a host player (--force-friendship-delta, --force-mail-send, --force-money-rock-hit,
      --force-fish-catch, --force-bug-catch, --diag-bug-despawn-label-race, --scene-test-enter-shop, --scene-test-leave-after, --collide-test-overlap,
      --collide-test-approach)                                -> exit 2 naming the hook
  U1  the pre-existing role-bound refusal (--txn-fault without --host) is unchanged: exit 2, same message style
Not launched here: the VALID combinations (--host --host-observer [other flags]) -- test_observer_protocol.py / test_observer_real_client.py do that.
Usage: python test_observer_cli.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402


def run_exe(args, timeout=30):
    """(exit code, stdout, stderr) of `AnimalCrossing.exe <args>` from the disposable dir; a hang is a failure (code None)."""
    L.require_launchable_bin_dir(L.GAME_BIN_DIR)
    exe = os.path.join(L.GAME_BIN_DIR, L.GAME_EXE_NAME)
    try:
        p = subprocess.run([exe] + list(args), cwd=L.GAME_BIN_DIR, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", ""
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    port = "12150"

    rc, out, err = run_exe(["--help"])
    ck("H1 --help exits 0", rc == 0)
    ck("H1 --help documents --host-observer", "--host-observer" in out)
    ck("H1 --help states HOST-only, the exit code 2 refusal and the exclusivity with --bootstrap-resident / --bootstrap-guest",
       "HOST-only" in out and "exit code 2" in out and "--bootstrap-resident" in out.split("--host-observer", 1)[1][:600]
       and "--bootstrap-guest" in out.split("--host-observer", 1)[1][:600])
    ck("H1 --help says the observer is never saved / never sent to clients and that plain --host is unchanged",
       "never saved" in out and "plain --host is unchanged" in out)

    rc, out, err = run_exe(["--host-observer"])
    ck("R1 --host-observer alone: exit 2", rc == 2)
    ck("R1 message names --host-observer and 'HOST-only' ... '--host'", "[NET][OBSERVER] REFUSED" in err and "HOST-only" in err and "--host" in err)
    ck("R1 nothing was written to stdout (refused before anything started)", out == "")
    rc, out, err = run_exe(["--connect", "127.0.0.1:" + port, "--host-observer"])
    ck("R1b --connect + --host-observer (a client): exit 2, HOST-only message", rc == 2 and "HOST-only" in err)

    rc, out, err = run_exe(["--host", port, "--host-observer", "--bootstrap-resident", "0"])
    ck("R2 --host --host-observer --bootstrap-resident 0: exit 2, message names --bootstrap-resident", rc == 2 and "--bootstrap-resident" in err
       and "[NET][OBSERVER] REFUSED" in err)
    rc, out, err = run_exe(["--bootstrap-resident", "2", "--host", port, "--host-observer"])
    ck("R2b the flag order does not matter (--bootstrap-resident 2 first): exit 2", rc == 2 and "--bootstrap-resident" in err)

    rc, out, err = run_exe(["--host", port, "--host-observer", "--bootstrap-guest", "GUEST,TOWN,5,7"])
    ck("R3 --host --host-observer --bootstrap-guest: exit 2 (the guest hook is client-only)", rc == 2 and "--bootstrap-guest" in err)
    rc, out, err = run_exe(["--connect", "127.0.0.1:" + port, "--bootstrap-guest", "GUEST,TOWN,5,7", "--host-observer"])
    ck("R3b --connect --bootstrap-guest --host-observer: exit 2", rc == 2)

    hooks = [("--force-friendship-delta", ["5"]), ("--force-mail-send", []), ("--force-money-rock-hit", []), ("--force-fish-catch", []),
             ("--force-bug-catch", []), ("--diag-bug-despawn-label-race", []), ("--scene-test-enter-shop", []),
             ("--scene-test-leave-after", ["3"]), ("--collide-test-overlap", []), ("--collide-test-approach", ["2"])]
    for hook, extra in hooks:
        rc, out, err = run_exe(["--host", port, "--host-observer", hook] + extra)
        ck("R4 %s with --host --host-observer: exit 2 and the message names the hook" % hook, rc == 2 and hook in err and "[NET][OBSERVER] REFUSED" in err)
    rc, out, err = run_exe(["--collide-test-approach", "2", "--host-observer", "--host", port])
    ck("R4b the hook before the flags (--collide-test-approach first): still exit 2", rc == 2 and "--collide-test-approach" in err)

    # the pre-existing refusal style is untouched (a role-bound hook without its role): same exit code and style
    rc, out, err = run_exe(["--txn-fault=expire"])
    ck("U1 the pre-existing --txn-fault refusal is unchanged (exit 2, [NET][TXN][TEST-ONLY] REFUSED)", rc == 2 and "[NET][TXN][TEST-ONLY] REFUSED" in err)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
