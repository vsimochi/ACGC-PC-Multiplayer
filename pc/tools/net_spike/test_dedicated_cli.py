#!/usr/bin/env python3
"""test_dedicated_cli.py - --dedicated ARGUMENT VALIDATION (no game is ever started past argument parsing).

TIER: a REAL game binary, but every launch below is REFUSED (exit code 2) or answered (--help, exit code 0) inside main() before the window opens or
anything is initialised, so it is safe and takes milliseconds. The exe is the one of the DISPOSABLE fixture dir (pc\\build64\\bin_fixture4 by default;
NET_SPIKE_GAME_BIN overrides; the live and protected dirs are refused by net_spike_lib).

Checks (exit codes + stderr text):
  H1  --help mentions --dedicated (HOST-only, exit code 2, the exclusions, implies --host-observer, hidden window / dummy audio / commands) and exits 0
  R1  --dedicated without --host                              -> exit 2, [DEDICATED] REFUSED ... HOST-only + usage text on stderr, nothing on stdout
      (also with --connect: a client; and --host together with --connect)
  R2  --host --dedicated --bootstrap-resident N               -> exit 2 (the dedicated server plays no resident); flag order does not matter
  R3  --host --dedicated --bootstrap-guest ...                -> exit 2
  R4  --host --dedicated plus each host-self test hook the observer refuses (same list as --host-observer) -> exit 2 naming the hook
  A1  ACCEPTED combinations pass the dedicated validation: they reach the LATER, unrelated refusal (-logfile naming a .gci file, exit 2 with the
      [PC][LOG] REFUSED text and NO [DEDICATED] REFUSED text): --host P --dedicated, --dedicated --host P (order), --host --dedicated without a port,
      --host P --dedicated --host-observer (redundant but legal), --host P --dedicated --verbose
  U1  the pre-existing refusals are unchanged: --host-observer without --host (observer text) and --txn-fault without --host
Not launched here: the VALID run itself -- test_dedicated_runtime.py does that.
Usage: python test_dedicated_cli.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402

HOOKS = [("--force-friendship-delta", ["5"]), ("--force-mail-send", []), ("--force-money-rock-hit", []), ("--force-fish-catch", []),
         ("--force-bug-catch", []), ("--diag-bug-despawn-label-race", []), ("--scene-test-enter-shop", []),
         ("--scene-test-leave-after", ["3"]), ("--collide-test-overlap", []), ("--collide-test-approach", ["2"])]


def run_exe(args, timeout=30):
    """(exit code, stdout, stderr) of `AnimalCrossing.exe <args>` from the disposable dir; a hang is a failure (code None)."""
    L.require_launchable_bin_dir(L.GAME_BIN_DIR)
    exe = os.path.join(L.GAME_BIN_DIR, L.GAME_EXE_NAME)
    try:
        p = subprocess.run([exe] + list(args), cwd=L.GAME_BIN_DIR, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "", ""
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def main():
    L.require_test_bin_dir()
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    port = "12450"

    rc, out, err = run_exe(["--help"])
    ck("H1 --help exits 0", rc == 0)
    ck("H1 --help documents --dedicated", "--dedicated" in out)
    tail = out.split("  --dedicated ", 1)[1][:1100] if "  --dedicated " in out else ""
    ck("H1 --help states HOST-only, the exit code 2 refusal and the exclusions (--connect, --bootstrap-resident, --bootstrap-guest)",
       "HOST-only" in tail and "exit code 2" in tail and "--connect" in tail and "--bootstrap-resident" in tail and "--bootstrap-guest" in tail)
    ck("H1 --help says it implies --host-observer, hidden window, no vsync, dummy audio and lists the commands help/status/players/save/stop",
       "Implies --host-observer" in tail and "HIDDEN window" in tail and "no vsync" in tail and "dummy audio" in tail
       and "help, status, players, save, stop" in tail)
    ck("H1 --help says plain --host / --host-observer / --bootstrap-resident are unchanged", "plain --host," in tail and "unchanged" in tail)

    rc, out, err = run_exe(["--dedicated"])
    ck("R1 --dedicated alone: exit 2", rc == 2)
    ck("R1 stderr: [DEDICATED] REFUSED, HOST-only, names --host", "[DEDICATED] REFUSED" in err and "HOST-only" in err and "--host" in err)
    ck("R1 stderr carries a usage line", "usage: AnimalCrossing --host [port] --dedicated" in err)
    ck("R1 nothing was written to stdout (refused before anything started)", out == "")
    rc, out, err = run_exe(["--connect", "127.0.0.1:" + port, "--dedicated"])
    ck("R1b --connect + --dedicated (a client): exit 2, HOST-only message", rc == 2 and "[DEDICATED] REFUSED" in err and "HOST-only" in err)
    rc, out, err = run_exe(["--dedicated", "--host", port, "--connect", "127.0.0.1:" + port])
    ck("R1c --host + --connect + --dedicated: exit 2 (the later --connect makes it a client)", rc == 2 and "[DEDICATED] REFUSED" in err)
    rc, out, err = run_exe(["--connect", "127.0.0.1:" + port, "--host", port, "--dedicated"])
    ck("R1d --connect first, --host last (role ends up host) + --dedicated: still exit 2, message names --connect",
       rc == 2 and "[DEDICATED] REFUSED" in err and "--connect" in err)

    rc, out, err = run_exe(["--host", port, "--dedicated", "--bootstrap-resident", "0"])
    ck("R2 --host --dedicated --bootstrap-resident 0: exit 2, message names --bootstrap-resident",
       rc == 2 and "--bootstrap-resident" in err and "[DEDICATED] REFUSED" in err)
    rc, out, err = run_exe(["--bootstrap-resident", "2", "--host", port, "--dedicated"])
    ck("R2b the flag order does not matter (--bootstrap-resident 2 first): exit 2", rc == 2 and "--bootstrap-resident" in err)

    rc, out, err = run_exe(["--host", port, "--dedicated", "--bootstrap-guest", "GUEST,TOWN,5,7"])
    # --bootstrap-guest is a CLIENT-only hook: with --host the pre-existing guest refusal ("[NET][GUEST][TEST-ONLY] REFUSED") fires BEFORE the dedicated block,
    # so the dedicated one is a second line of defence that can never be reached with a host role. Either way: exit 2 naming the flag.
    ck("R3 --host --dedicated --bootstrap-guest: exit 2, the message names --bootstrap-guest (pre-existing client-only refusal comes first)",
       rc == 2 and "--bootstrap-guest" in err and "REFUSED" in err)

    for hook, extra in HOOKS:
        rc, out, err = run_exe(["--host", port, "--dedicated", hook] + extra)
        ck("R4 %s with --host --dedicated: exit 2 and the message names the hook" % hook, rc == 2 and hook in err and "[DEDICATED] REFUSED" in err)
    rc, out, err = run_exe(["--collide-test-approach", "2", "--dedicated", "--host", port])
    ck("R4b the hook before the flags (--collide-test-approach first): still exit 2", rc == 2 and "--collide-test-approach" in err)
    rc, out, err = run_exe(["--host", port, "--dedicated", "--host-observer", "--force-mail-send"])
    ck("R4c --dedicated with a redundant --host-observer and a refused hook: exit 2 (the dedicated refusal fires first)",
       rc == 2 and "--force-mail-send" in err)

    later = ["-logfile", "dedicated_cli_probe.gci"]
    for label, args in [("--host P --dedicated", ["--host", port, "--dedicated"]),
                        ("--dedicated --host P (order)", ["--dedicated", "--host", port]),
                        ("--host --dedicated (default port)", ["--host", "--dedicated"]),
                        ("--host P --dedicated --host-observer", ["--host", port, "--dedicated", "--host-observer"]),
                        ("--host P --dedicated --verbose", ["--host", port, "--dedicated", "--verbose"])]:
        rc, out, err = run_exe(args + later)
        ck("A1 accepted combination '%s' passes the dedicated validation and reaches the later [PC][LOG] refusal (exit 2, no [DEDICATED] REFUSED)" % label,
           rc == 2 and "[PC][LOG] REFUSED" in err and "[DEDICATED] REFUSED" not in err)

    rc, out, err = run_exe(["--host-observer"])
    ck("U1 --host-observer alone is still refused with the observer text (exit 2)", rc == 2 and "[NET][OBSERVER] REFUSED" in err)
    rc, out, err = run_exe(["--txn-fault=expire"])
    ck("U1 the pre-existing --txn-fault refusal is unchanged (exit 2, [NET][TXN][TEST-ONLY] REFUSED)", rc == 2 and "[NET][TXN][TEST-ONLY] REFUSED" in err)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
