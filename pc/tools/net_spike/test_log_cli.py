#!/usr/bin/env python3
"""test_log_cli.py - pc_log COMMAND-LINE parsing (no game is ever started past argument parsing).

TIER: a REAL game binary, but every launch below exits inside main() (exit 0 for --debug-list / --help, exit 2 for a refusal) BEFORE stdout
is redirected, the window opens or anything is initialised, so it is safe and takes milliseconds. The exe is the one of the DISPOSABLE fixture
dir (pc\\build64\\bin_fixture4 by default; NET_SPIKE_GAME_BIN overrides; the live and protected dirs are refused by net_spike_lib).

How "accepted" is proven without booting the game: a log flag is followed by `--txn-fault=expire` (no --host), whose HOST-only refusal
([NET][TXN][TEST-ONLY] REFUSED, exit 2) runs AFTER the whole argument loop. Seeing that exact refusal and no "[PC][LOG]" error proves the log
flag was accepted and parsing went on.

Checks:
  L1  -debug-list / --debug-list / -debuglist: exit 0, stdout lists all 15 categories, nothing on stderr; also exits 0 when other flags
      (--host 12300) come first or last (before any init)
  U1  unknown category via -debug<x>, --debug-<x>, --debug=<list with a bad name>, an empty --debug=: exit 2, stderr names the bad name AND
      lists the valid categories, nothing on stdout
  A1  every category in the forms -debug<cat>, --debug-<cat>, --debug=<cat> (and singular/plural villager(s), short net/txn) is accepted
  A2  -debug, -debugall, --debug=all, --debug=a,b,c (any case), -logtime, -logfile PATH, --verbose, -v, and combinations are accepted
  F1  -logfile without a path: exit 2; -logfile inside a directory named "save": exit 2 REFUSED (the log file is never created);
      -logfile naming a .gci: exit 2
  H1  --help documents -debug / --debug-list
Usage: python test_log_cli.py"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402

CATS = ["general", "network", "players", "villagers", "buildings", "items", "wildlife", "save", "events", "transactions", "records",
        "guests", "townsvc", "mail", "shop"]
EXTRA_NAMES = ["villager", "net", "txn", "player", "item", "event", "record", "guest"]
REFUSAL = "[NET][TXN][TEST-ONLY] REFUSED"


def run_exe(args, timeout=30, env=None):
    L.require_launchable_bin_dir(L.GAME_BIN_DIR)
    exe = os.path.join(L.GAME_BIN_DIR, L.GAME_EXE_NAME)
    try:
        p = subprocess.run([exe] + list(args), cwd=L.GAME_BIN_DIR, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, "", ""
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def accepted(args):
    """True when the flags were accepted and parsing reached the later --txn-fault refusal."""
    rc, out, err = run_exe(list(args) + ["--txn-fault=expire"])
    return rc == 2 and REFUSAL in err and "[PC][LOG]" not in err and out == ""


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)

    for flag in ("-debug-list", "--debug-list", "-debuglist"):
        rc, out, err = run_exe([flag])
        names = [ln.split()[0] for ln in out.splitlines() if ln.startswith("  ") and ln.split()[0] in CATS]
        ck("L1 %s: exit 0" % flag, rc == 0)
        ck("L1 %s lists all 15 categories (each once, in order)" % flag, names == CATS)
        ck("L1 %s: nothing on stderr" % flag, err == "")
    for args in (["--host", "12300", "-debug-list"], ["-debug-list", "--host", "12300", "--bootstrap-resident", "0"], ["--verbose", "--debug-list"]):
        rc, out, err = run_exe(args)
        ck("L1 %s: exits 0 before any init and lists the categories" % " ".join(args), rc == 0 and "villagers" in out and "townsvc" in out)

    for args, bad in ((["-debugbogus"], "bogus"), (["--debug-bogus"], "bogus"), (["--debug=network,bogus"], "bogus"),
                      (["--debug=bogus,network"], "bogus"), (["--debug=savevillager"], "savevillager"), (["--debug="], "(empty list)"),
                      (["-debugwild-life"], "wild-life")):
        rc, out, err = run_exe(args)
        ck("U1 %s: exit 2" % " ".join(args), rc == 2)
        ck("U1 %s: stderr names '%s' and lists every valid category" % (" ".join(args), bad),
           bad in err and "valid categories:" in err and all(c in err for c in CATS) and " all" in err)
        ck("U1 %s: nothing on stdout (refused before anything started)" % " ".join(args), out == "")

    ck("A0 control: --txn-fault=expire alone is refused (exit 2, the TXN refusal), so the accepted() probe below can tell success from failure",
       run_exe(["--txn-fault=expire"])[0] == 2)
    ck("A0 control: a bad log flag in front of it is reported as a [PC][LOG] error instead", not accepted(["-debugbogus"]))
    for c in CATS + EXTRA_NAMES:
        forms = ["-debug" + c, "--debug-" + c, "--debug=" + c, "--debug=" + c.upper()]
        bad = [f for f in forms if not accepted([f])]
        ck("A1 category '%s' accepted as %s" % (c, ", ".join(forms)), not bad)
        if bad:
            L.info("rejected forms: %s" % bad)

    tmp = tempfile.mkdtemp(prefix="pclogcli_")
    okpath = os.path.join(tmp, "x.log")
    combos = [["-debug"], ["--debug"], ["-debugall"], ["--debug-all"], ["--debug=all"], ["--debug=network,villagers,save"],
              ["--debug=Network, Villagers ,SAVE"], ["-logtime"], ["-logfile", okpath], ["--verbose"], ["-v"],
              ["-debugnetwork", "-debugsave", "-debug-villager", "--debug=mail,shop", "-logtime", "-logfile", okpath, "--verbose"],
              ["--verbose", "-debug", "-logtime"]]
    for a in combos:
        ck("A2 accepted: %s" % " ".join(a), accepted(a))
    ck("A2 -logfile accepted at parse time does not create the file before the game would start (no early side effect)", not os.path.exists(okpath))

    rc, out, err = run_exe(["-logfile"])
    ck("F1 -logfile without a path: exit 2 with a message", rc == 2 and "[PC][LOG]" in err and "path" in err)
    savedir = os.path.join(tmp, "save")
    os.makedirs(savedir, exist_ok=True)
    bad_path = os.path.join(savedir, "x.log")
    rc, out, err = run_exe(["-logfile", bad_path])
    ck("F1 -logfile inside a 'save' directory: exit 2 REFUSED", rc == 2 and "REFUSED" in err and "save" in err)
    ck("F1 ... and the file was never created", not os.path.exists(bad_path))
    rc, out, err = run_exe(["-logfile", os.path.join(tmp, "town.gci")])
    ck("F1 -logfile naming a .gci file: exit 2 REFUSED", rc == 2 and "REFUSED" in err)
    ck("F1 ... and it was never created", not os.path.exists(os.path.join(tmp, "town.gci")))
    rc, out, err = run_exe(["-logfile", os.path.join(tmp, "nodir", "x.log")])
    ck("F1 -logfile in a missing directory: exit 2 (cannot open), not a crash", rc == 2 and "REFUSED" in err)

    rc, out, err = run_exe(["--help"])
    ck("H1 --help exits 0 and documents -debug, --debug-list, -logfile and PC_LOG", rc == 0 and "-debug " in out and "--debug-list" in out
       and "-logfile" in out and "PC_LOG" in out)
    ck("H1 --help still documents --verbose", "--verbose, -v" in out)
    try:
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f)) if os.path.isfile(os.path.join(tmp, f)) else None
        os.rmdir(savedir)
        os.rmdir(tmp)
    except OSError:
        pass
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
