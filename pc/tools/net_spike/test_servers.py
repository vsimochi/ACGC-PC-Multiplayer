#!/usr/bin/env python3
"""test_servers.py - NATIVE UNIT test of the saved server profiles (pc/src/pc_servers.c) + the Play Online relaunch command line (pc/src/pc_relaunch.c), plus the
--servers / --server-add / --server-delete / --server CLI checks (no host, no window) in a TEMP cwd.

TIER: native unit + process (refusal paths only). Compiles servers_selftest.c + pc_servers.c + pc_relaunch.c with the msys2 gcc (-Wall -Wextra, must be warning-free) and
runs it against a scratch dir under the temp dir; then runs pc/build64/bin/AnimalCrossing.exe with cwd = a scratch dir (relative save/mp resolves inside it). Nothing under
the repo or the live save dir is touched. The CLI checks never start the game (every --server case here exits 2 before any initialisation).
Usage: python test_servers.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"
EXE = os.path.join(PC, "build64", "bin", "AnimalCrossing.exe")
SCRATCH = os.path.join(tempfile.gettempdir(), "acmp_servers_test")

results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


def run(args, cwd):
    return subprocess.run([EXE] + args, cwd=cwd, capture_output=True, text=True, timeout=60)


def main():
    scratch = os.path.abspath(SCRATCH)
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    exe = os.path.join(scratch, "servers_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    src = [os.path.join(HERE, "servers_selftest.c")] + [os.path.join(PC, "src", f) for f in ("pc_servers.c", "pc_relaunch.c")]
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include")] + src + ["-o", exe],
                        capture_output=True, text=True, env=env)
    check("compile with msys2 gcc -Wall -Wextra succeeds", cp.returncode == 0)
    check("compile is warning-free", cp.stderr.strip() == "")
    if cp.returncode != 0:
        print(cp.stderr)
        return 1
    rp = subprocess.run([exe, os.path.join(scratch, "data")], capture_output=True, text=True, env=env, timeout=120)
    out = rp.stdout
    passes = [ln[6:] for ln in out.splitlines() if ln.startswith("PASS: ")]
    fails = [ln[6:] for ln in out.splitlines() if ln.startswith("FAIL: ")]
    m = re.search(r"RESULT passed=(\d+) failed=(\d+)", out)
    for p in passes:
        results.append((p, True))
    for p in fails:
        results.append((p, False))
        print("FAIL - " + p)
    print("native: %d passed, %d failed (exit %d)" % (len(passes), len(fails), rp.returncode))
    check("native harness exit 0 and the RESULT line agrees", rp.returncode == 0 and m is not None and int(m.group(1)) == len(passes) and int(m.group(2)) == 0)

    # ---- CLI in a temp cwd ----
    cwd = os.path.join(scratch, "cwd")
    os.makedirs(cwd)
    ini = os.path.join(cwd, "save", "mp", "servers.ini")
    r = run(["--servers"], cwd)
    check("--servers on a fresh cwd exits 0 with 0 servers", r.returncode == 0 and "servers (save/mp/servers.ini): 0" in r.stdout)
    r = run(["--server-add", "Friends", "192.168.1.20:7778"], cwd)
    check("--server-add exits 0 and reports the entry", r.returncode == 0 and "'Friends' = 192.168.1.20:7778" in r.stdout and os.path.isfile(ini))
    r = run(["--server-add", "Home", "127.0.0.1"], cwd)
    check("--server-add without a port defaults to 7777", r.returncode == 0 and "127.0.0.1:7777" in r.stdout)
    r = run(["--servers"], cwd)
    check("--servers lists both", r.returncode == 0 and "name='Friends' address=192.168.1.20 port=7778" in r.stdout and "name='Home'" in r.stdout and ": 2" in r.stdout)
    r = run(["--server-add", "friends", "1.2.3.4"], cwd)
    check("--server-add duplicate name exits 2", r.returncode == 2 and "already exists" in r.stderr)
    r = run(["--server-add", "Bad", "bad_host!"], cwd)
    check("--server-add malformed hostname exits 2", r.returncode == 2 and "hostname" in r.stderr)
    r = run(["--server-add", "Tun", "example.gl.at.ply.gg:12345"], cwd)
    check("--server-add hostname exits 0 and reports it", r.returncode == 0 and "example.gl.at.ply.gg:12345" in r.stdout)
    r = run(["--server-delete", "Tun"], cwd)
    r = run(["--server-add", "Bad", "1.2.3.4:0"], cwd)
    check("--server-add port 0 exits 2", r.returncode == 2)
    r = run(["--server-add", "Bad"], cwd)
    check("--server-add missing HOST exits 2 + usage", r.returncode == 2 and "usage:" in r.stderr)
    r = run(["--server", "Nope"], cwd)
    check("--server unknown name exits 2 + usage", r.returncode == 2 and "no saved server named 'Nope'" in r.stderr and "usage:" in r.stderr)
    r = run(["--server", "Friends", "--connect", "1.2.3.4:7777"], cwd)
    check("--server with --connect exits 2", r.returncode == 2 and "cannot be combined with --connect" in r.stderr)
    r = run(["--server", "Friends", "--host"], cwd)
    check("--server with --host exits 2", r.returncode == 2 and "cannot be combined with --host" in r.stderr)
    r = run(["--server", "Friends", "--dedicated"], cwd)
    check("--server with --dedicated exits 2", r.returncode == 2 and "cannot be combined with --dedicated" in r.stderr)
    r = run(["--server"], cwd)
    check("--server without NAME exits 2", r.returncode == 2 and "usage:" in r.stderr)
    r = run(["--server-delete", "Nope"], cwd)
    check("--server-delete unknown exits 2", r.returncode == 2)
    r = run(["--server-delete", "friends"], cwd)
    check("--server-delete exits 0", r.returncode == 0 and "deleted server 'friends'" in r.stdout)
    r = run(["--servers"], cwd)
    check("--servers afterwards: only Home", r.returncode == 0 and "name='Home'" in r.stdout and "Friends" not in r.stdout)
    # corrupt file: refused everywhere, byte-identical afterwards
    bad = b"[server]\nname = X\naddress = bad_host!\n"
    with open(ini, "wb") as f:
        f.write(bad)
    r1 = run(["--servers"], cwd)
    r2 = run(["--server-add", "New", "1.2.3.4"], cwd)
    r3 = run(["--server-delete", "X"], cwd)
    r4 = run(["--server", "X"], cwd)
    check("a corrupt servers.ini: --servers / --server-add / --server-delete / --server all exit 2 and name the file",
          r1.returncode == 2 and r2.returncode == 2 and r3.returncode == 2 and r4.returncode == 2 and "corrupt" in r1.stderr and "servers.ini" in r2.stderr)
    check("the corrupt servers.ini is byte-identical and no .tmp remains", open(ini, "rb").read() == bad and not os.path.exists(ini + ".tmp"))
    bad_n = [d for d, ok in results if not ok]
    print("\n%d checks, %d failed" % (len(results), len(bad_n)))
    return 1 if bad_n else 0


if __name__ == "__main__":
    sys.exit(main())
