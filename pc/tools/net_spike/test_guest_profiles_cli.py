#!/usr/bin/env python3
"""test_guest_profiles_cli.py - `--guest-profile NAME`: ARGUMENT VALIDATION fails EARLY (exit 2, a diagnostic naming the rule, nothing started, nothing created).

TIER: a REAL game binary, but every launch below is REFUSED (exit code 2) inside main() right after the option parsing (or after the profile load of a corrupt file), before
the window opens, the network starts or any save is loaded: safe, milliseconds each, NO host. The exe is the one of the DISPOSABLE fixture dir (pc\\build64\\bin_fixture4 by
default; NET_SPIKE_GAME_BIN overrides; live / protected dirs are refused by net_spike_lib) run with CWD = a fresh scratch directory under the temp dir, so every
`save/mp/guest*.ini` is read / would be written ONLY there. A hang is a failure (our own child is killed by its handle).

  * the profile NAME rule table: missing value, empty, `--guest` swallowed as a value, too long (17), spaces, dots, `..`, path separators, drive colon, underscore, device names
    (CON NUL PRN AUX COM1 LPT9, any case), leading '-': exit 2, `--guest-profile: REFUSED` naming the rule, usage line, NOTHING created
  * the same option twice: exit 2
  * exclusivity (same refusals as --guest): no --connect, --host, --dedicated, --host-observer, --bootstrap-guest, --bootstrap-resident, in either argument order: exit 2 +
    usage, nothing created
  * the SELECTED profile is the one that is read: a corrupt save/mp/guest_alice.ini is named (lower case file name even for `--guest-profile Alice`), preserved byte for byte,
    save/mp/guest.ini is not touched / not created; the same with `--guest --guest-profile alice` and `--guest-profile alice --guest`; a directory in place of the file is refused
  * `--guest` ALONE still reads save/mp/guest.ini (a corrupt one is named, not guest_alice.ini)
  * --help documents --guest-profile
Not launched here: a VALID profile with --connect (that starts the game; test_guest_profiles_real.py does it with a real host).
Usage: python test_guest_profiles_cli.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402

PORT = "12478"   # nothing listens there and nothing is ever started
BAD_INI = "name = SERVER\ngender = 0\nface = 1\nhome_town = H\nplayer_id = 1\nland_id = 2\n"


def run_exe(args, cwd, timeout=20):
    L.require_launchable_bin_dir(L.GAME_BIN_DIR)
    exe = os.path.join(L.GAME_BIN_DIR, L.GAME_EXE_NAME)
    p = subprocess.Popen([exe] + list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()   # our own child only, by its handle
        p.communicate()
        return None, "", ""
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def tree(d):
    out = []
    for root, dirs, files in os.walk(d):
        for n in dirs + files:
            out.append(os.path.relpath(os.path.join(root, n), d))
    return sorted(out)


def main():
    L.require_test_bin_dir()
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    scratch = tempfile.mkdtemp(prefix="acmp_gprof_cli_")
    try:
        ck("the scratch cwd is under the temp dir and is not the fixture dir", os.path.abspath(scratch).startswith(os.path.abspath(tempfile.gettempdir())) and os.path.abspath(scratch) != os.path.abspath(L.GAME_BIN_DIR))
        conn = ["--connect", "127.0.0.1:" + PORT]

        # --- the NAME rule table ---
        names = [
            ("empty value", "", "needs a profile NAME"),
            ("17 characters", "12345678901234567", "too long"),
            ("a space", "a b", "outside"),
            ("a dot", "al.ice", "outside"),
            ("dot dot", "..", "outside"),
            ("a slash", "a/b", "outside"),
            ("a backslash", "a\\b", "outside"),
            ("traversal", "../up", "outside"),
            ("a drive colon", "C:x", "outside"),
            ("an underscore", "a_b", "outside"),
            ("leading hyphen", "-alice", "start with"),
            ("an option swallowed as the value", "--guest", "start with"),
            ("CON", "CON", "device"),
            ("nul (lower case)", "nul", "device"),
            ("Prn", "Prn", "device"),
            ("AUX", "AUX", "device"),
            ("COM1", "COM1", "device"),
            ("lpt9", "lpt9", "device"),
        ]
        for label, val, rule in names:
            cwd = tempfile.mkdtemp(dir=scratch)
            rc, out, err = run_exe(conn + ["--guest-profile", val], cwd)
            ck("name rule - %s: exit status 2 within the timeout" % label, rc == 2)
            ck("name rule - %s: stderr says `--guest-profile: REFUSED`, names the rule ('%s') and prints the usage line" % (label, rule),
               "[PC] --guest-profile: REFUSED" in err and rule in err and "usage: AnimalCrossing --connect HOST[:PORT] --guest-profile NAME" in err)
            ck("name rule - %s: nothing started (no stdout), NOTHING created in the cwd" % label, out == "" and tree(cwd) == [])
        cwd = tempfile.mkdtemp(dir=scratch)
        rc, out, err = run_exe(conn + ["--guest-profile"], cwd)
        ck("missing value (option is the last argument): exit 2, names 'needs a profile NAME', usage, nothing created",
           rc == 2 and "[PC] --guest-profile: REFUSED" in err and "needs a profile NAME" in err and "usage:" in err and out == "" and tree(cwd) == [])
        cwd = tempfile.mkdtemp(dir=scratch)
        rc, out, err = run_exe(conn + ["--guest-profile", "alice", "--guest-profile", "bob"], cwd)
        ck("the option given twice: exit 2 'more than once', nothing created", rc == 2 and "more than once" in err and out == "" and tree(cwd) == [])

        # --- exclusivity, same as --guest ---
        cases = [
            ("no --connect", ["--guest-profile", "alice"], "CLIENT-only"),
            ("no --connect (with --guest)", ["--guest", "--guest-profile", "alice"], "CLIENT-only"),
            ("with --host", ["--host", "--guest-profile", "alice"], "cannot be combined with --host"),
            ("with --host (after)", ["--guest-profile", "alice", "--host", "12479"], "cannot be combined with --host"),
            ("with --dedicated", ["--host", "--dedicated", "--guest-profile", "alice"], "cannot be combined with"),
            ("with --host-observer", ["--host", "--host-observer", "--guest-profile", "alice"], "cannot be combined with"),
            ("with --bootstrap-guest", conn + ["--guest-profile", "alice", "--bootstrap-guest", "Bella,HOMETWN,4097,23297"], "cannot be combined with --bootstrap-guest"),
            ("with --bootstrap-guest (before)", conn + ["--bootstrap-guest", "Bella,HOMETWN,4097,23297", "--guest-profile", "alice"], "cannot be combined with --bootstrap-guest"),
            ("with --bootstrap-resident", conn + ["--guest-profile", "alice", "--bootstrap-resident", "0"], "cannot be combined with --bootstrap-resident"),
        ]
        for name, args, why in cases:
            cwd = tempfile.mkdtemp(dir=scratch)
            rc, out, err = run_exe(args, cwd)
            ck("%s: exit status 2 (no hang, no running client)" % name, rc == 2)
            ck("%s: stderr says `--guest: REFUSED` / '%s' and prints the usage line (mentioning --guest-profile)" % (name, why),
               "[PC] --guest: REFUSED" in err and why in err and "usage: AnimalCrossing --connect HOST[:PORT] --guest [--guest-profile NAME]" in err)
            ck("%s: nothing started and NOTHING created in the cwd (the profile is not touched before the exclusivity check)" % name, out == "" and tree(cwd) == [])

        # --- the selected profile is the one that is read ---
        sel = [
            ("--guest-profile alice", ["--guest-profile", "alice"], "guest_alice.ini"),
            ("--guest-profile Alice (case folded to the lower case file)", ["--guest-profile", "Alice"], "guest_alice.ini"),
            ("--guest --guest-profile alice", ["--guest", "--guest-profile", "alice"], "guest_alice.ini"),
            ("--guest-profile alice --guest", ["--guest-profile", "alice", "--guest"], "guest_alice.ini"),
        ]
        for name, extra, fname in sel:
            cwd = tempfile.mkdtemp(dir=scratch)
            os.makedirs(os.path.join(cwd, "save", "mp"))
            ini = os.path.join(cwd, "save", "mp", fname)
            with open(ini, "wb") as f:
                f.write(BAD_INI.encode("ascii"))
            before = tree(cwd)
            rc, out, err = run_exe(conn + extra, cwd)
            with open(ini, "rb") as f:
                after = f.read()
            ck("%s with a corrupt %s: exit status 2" % (name, fname), rc == 2)
            ck("%s: stderr names `--guest: REFUSED: bad guest profile`, the file %s (lower case) and the bad key 'name', and is NOT about guest.ini" % (name, fname),
               "[PC] --guest: REFUSED: bad guest profile" in err and fname in err and "name:" in err and "preserved unchanged" in err and "guest.ini" not in err.replace("guest_alice.ini", ""))
            ck("%s: the profile file is preserved byte for byte, save/mp/guest.ini was NOT created, nothing else appeared, nothing started" % name,
               after == BAD_INI.encode("ascii") and tree(cwd) == before and not os.path.exists(os.path.join(cwd, "save", "mp", "guest.ini")) and out == "")
        cwd = tempfile.mkdtemp(dir=scratch)
        os.makedirs(os.path.join(cwd, "save", "mp", "guest_dirp.ini"))
        rc, out, err = run_exe(conn + ["--guest-profile", "dirp"], cwd)
        ck("a DIRECTORY named guest_dirp.ini: exit 2, the diagnostic names the file, the directory is untouched", rc == 2 and "guest_dirp.ini" in err and os.path.isdir(os.path.join(cwd, "save", "mp", "guest_dirp.ini")))
        # a corrupt default guest.ini next to a profile: the profile run is NOT affected by guest.ini unreadable...; the default run stays on guest.ini
        cwd = tempfile.mkdtemp(dir=scratch)
        os.makedirs(os.path.join(cwd, "save", "mp"))
        for fn in ("guest.ini", "guest_alice.ini"):
            with open(os.path.join(cwd, "save", "mp", fn), "wb") as f:
                f.write(BAD_INI.encode("ascii"))
        rc, out, err = run_exe(conn + ["--guest"], cwd)
        ck("`--guest` ALONE still reads save/mp/guest.ini (a corrupt one is named; guest_alice.ini is not involved): exit 2", rc == 2 and "guest.ini" in err and "guest_alice" not in err
           and "bad guest profile" in err)
        with open(os.path.join(cwd, "save", "mp", "guest.ini"), "rb") as f:
            a1 = f.read()
        with open(os.path.join(cwd, "save", "mp", "guest_alice.ini"), "rb") as f:
            a2 = f.read()
        ck("both files preserved byte for byte after the default run", a1 == BAD_INI.encode("ascii") and a2 == BAD_INI.encode("ascii") and sorted(tree(cwd)) == sorted(["save", os.path.join("save", "mp"), os.path.join("save", "mp", "guest.ini"), os.path.join("save", "mp", "guest_alice.ini")]))

        # --- --help ---
        cwd = tempfile.mkdtemp(dir=scratch)
        rc, out, err = run_exe(["--help"], cwd)
        ck("--help documents --guest-profile (NAME rule, files guest_<name>.ini / guest_token_<name>.dat, default unchanged) and exits 0",
           rc == 0 and "--guest-profile NAME  CLIENT-only" in out and "save/mp/guest_<name>.ini" in out and "save/mp/guest_token_<name>.dat" in out
           and "save/mp/guest.ini and save/mp/guest_token.dat" in out and tree(cwd) == [])
        ck("the fixture dir itself got no save/mp/guest*.ini from any of these launches", not [f for f in (os.listdir(os.path.join(L.GAME_BIN_DIR, "save", "mp")) if os.path.isdir(os.path.join(L.GAME_BIN_DIR, "save", "mp")) else [])])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
