#!/usr/bin/env python3
"""test_guest_g2_cli.py - Guests G2.3: the `--guest` flag's ARGUMENT VALIDATION fails EARLY (exit 2, a diagnostic, nothing started).

TIER: a REAL game binary, but every launch below is REFUSED (exit code 2) inside main() right after the option parsing, before the window opens, the network starts or
any save is loaded, so it is safe and takes milliseconds. The exe is the one of the DISPOSABLE fixture dir (pc\\build64\\bin_fixture4 by default; NET_SPIKE_GAME_BIN
overrides; the live / protected dirs are refused by net_spike_lib) but it is run with its CWD = a fresh scratch directory under the temp dir, so `save/mp/guest.ini`
(relative to the cwd) is read / would be written ONLY there, never in a protected or fixture dir. A hang is a failure; the child (our own process, killed by its handle)
never survives.

  * exclusivity: `--guest` without --connect, with --host, with --dedicated (+ --host), with --host-observer (+ --host), with --bootstrap-guest, with --bootstrap-resident:
    exit 2, stderr `--guest: REFUSED` naming the conflict + the usage line, NOTHING created in the cwd (the profile file is not touched before the exclusivity check)
  * a CORRUPT / INVALID existing save/mp/guest.ini: exit 2 BEFORE any init, stderr names the file and the bad key, the file is preserved BYTE FOR BYTE (never
    regenerated, never rewritten), no tmp / other file appears
  * --help documents --guest; --bootstrap-guest (the TEST hook) still refuses the same bad specs
Not launched here: a VALID profile with --connect (that starts the game; test_guest_g2_real.py does it with a real host).
Usage: python test_guest_g2_cli.py
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


def run_exe(args, cwd, timeout=20):
    """(exit code, stdout, stderr) of `AnimalCrossing.exe <args>` run with cwd = `cwd`; a hang -> (None, '', '') after the child was killed."""
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


VALID = "name = Bella\ngender = 1\nface = 4\nhome_town = Hometown\nplayer_id = 4097\nland_id = 23297\n"


def main():
    L.require_test_bin_dir()
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    scratch = tempfile.mkdtemp(prefix="acmp_g2_cli_")
    try:
        ck("the scratch cwd is under the temp dir and is not the fixture dir", os.path.abspath(scratch).startswith(os.path.abspath(tempfile.gettempdir())) and os.path.abspath(scratch) != os.path.abspath(L.GAME_BIN_DIR))
        conn = ["--connect", "127.0.0.1:" + PORT]
        cases = [
            ("no --connect", ["--guest"], "CLIENT-only"),
            ("with --host", ["--host", "--guest"], "cannot be combined with --host"),
            ("with --host (after)", ["--guest", "--host", "12479"], "cannot be combined with --host"),
            ("with --dedicated", ["--host", "--dedicated", "--guest"], "cannot be combined with"),
            ("with --host-observer", ["--host", "--host-observer", "--guest"], "cannot be combined with"),
            ("with --bootstrap-guest", conn + ["--guest", "--bootstrap-guest", "Bella,HOMETWN,4097,23297"], "cannot be combined with --bootstrap-guest"),
            ("with --bootstrap-guest (before)", conn + ["--bootstrap-guest", "Bella,HOMETWN,4097,23297", "--guest"], "cannot be combined with --bootstrap-guest"),
            ("with --bootstrap-resident", conn + ["--guest", "--bootstrap-resident", "0"], "cannot be combined with --bootstrap-resident"),
        ]
        for name, args, why in cases:
            cwd = tempfile.mkdtemp(dir=scratch)
            rc, out, err = run_exe(args, cwd)
            ck("%s: exit status 2 within the timeout (no hang, no running client)" % name, rc == 2)
            ck("%s: stderr says `--guest: REFUSED` / '%s' and prints the usage line" % (name, why), "[PC] --guest: REFUSED" in err and why in err and "usage: AnimalCrossing --connect HOST[:PORT] --guest" in err)
            ck("%s: nothing started (no stdout) and NOTHING was created in the cwd (the profile file is not touched before the exclusivity check)" % name, out == "" and tree(cwd) == [])

        # a corrupt / invalid profile: refused early, preserved byte for byte
        bad = [
            ("gender 9", VALID.replace("gender = 1", "gender = 9"), "gender"),
            ("face 8", VALID.replace("face = 4", "face = 8"), "face"),
            ("empty name", VALID.replace("name = Bella", "name ="), "name"),
            ("reserved name SERVER", VALID.replace("name = Bella", "name = SERVER"), "name"),
            ("9-character name", VALID.replace("name = Bella", "name = Abcdefghi"), "name"),
            ("name with a comma", VALID.replace("name = Bella", "name = A,B"), "name"),
            ("player_id 0", VALID.replace("player_id = 4097", "player_id = 0"), "player_id"),
            ("land_id 0xFFFF", VALID.replace("land_id = 23297", "land_id = 0xFFFF"), "land_id"),
            ("land_id junk", VALID.replace("land_id = 23297", "land_id = abc"), "land_id"),
            ("missing land_id (no silent regeneration)", VALID.replace("land_id = 23297\n", ""), "land_id"),
            ("missing player_id (no silent regeneration)", VALID.replace("player_id = 4097\n", ""), "player_id"),
            ("unknown key", VALID + "nmae = X\n", "nmae"),
            ("duplicate key", VALID + "face = 2\n", "face"),
            ("garbage line", VALID + "this is not a profile\n", "line 7"),
            ("empty file", "", "name"),
            ("home_town too long", VALID.replace("home_town = Hometown", "home_town = Hometownn"), "home_town"),
        ]
        for name, body, key in bad:
            cwd = tempfile.mkdtemp(dir=scratch)
            os.makedirs(os.path.join(cwd, "save", "mp"))
            ini = os.path.join(cwd, "save", "mp", "guest.ini")
            with open(ini, "wb") as f:
                f.write(body.encode("ascii"))
            before = tree(cwd)
            rc, out, err = run_exe(conn + ["--guest"], cwd)
            with open(ini, "rb") as f:
                after = f.read()
            ck("%s: exit status 2" % name, rc == 2)
            ck("%s: stderr names `--guest: REFUSED: bad guest profile`, the file (guest.ini) and the bad key '%s'" % (name, key),
               "[PC] --guest: REFUSED: bad guest profile" in err and "guest.ini" in err and (key + ":") in err and "preserved unchanged" in err)
            ck("%s: the file is preserved byte for byte, nothing else appeared (no tmp, no .bak, no new file) and nothing was started" % name,
               after == body.encode("ascii") and tree(cwd) == before and out == "")

        # a directory instead of the file is refused, nothing replaced
        cwd = tempfile.mkdtemp(dir=scratch)
        os.makedirs(os.path.join(cwd, "save", "mp", "guest.ini"))
        rc, out, err = run_exe(conn + ["--guest"], cwd)
        ck("a DIRECTORY named guest.ini: exit 2, the diagnostic names the file, the directory is untouched", rc == 2 and "guest.ini" in err and os.path.isdir(os.path.join(cwd, "save", "mp", "guest.ini")))

        # --help documents the flag
        cwd = tempfile.mkdtemp(dir=scratch)
        rc, out, err = run_exe(["--help"], cwd)
        ck("--help documents --guest (CLIENT-only, save/mp/guest.ini, exit code 2 rule) and exits 0", rc == 0 and "--guest             CLIENT-only" in out and "save/mp/guest.ini" in out and tree(cwd) == [])
        # the TEST hook is unchanged: a bad spec is still refused by its own validator
        rc, out, err = run_exe(conn + ["--bootstrap-guest", "SERVER,HOMETWN,4097,23297"], cwd)
        ck("--bootstrap-guest (the test hook) still refuses a bad spec with its own diagnostic (exit 2)", rc == 2 and "--bootstrap-guest" in err and "reserved for the server observer" in err and tree(cwd) == [])
        ck("the fixture dir itself got no save/mp/guest.ini from any of these launches", not os.path.exists(os.path.join(L.GAME_BIN_DIR, "save", "mp", "guest.ini")))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
