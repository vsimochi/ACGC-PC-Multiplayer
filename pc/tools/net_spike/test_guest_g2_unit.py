#!/usr/bin/env python3
"""test_guest_g2_unit.py - Guests G2 NATIVE UNIT test: the fresh-guest-record predicate (G2.1, pc/src/pc_mp_guests.c) and the guest profile module (G2.2,
pc/src/pc_guest_profile.c, save/mp/guest.ini).

TIER: native unit (no game, no network, no fixture dir). Compiles pc/tools/net_spike/guest_g2_selftest.c + pc_guest_profile.c + pc_mp_guests.c + pc_mp_records.c (the
shared CRC32) with the msys2 gcc (-Wall -Wextra, must be warning-free) and runs it against a scratch directory under the temp dir. The fresh records the
predicate must ACCEPT are written here by the PYTHON mirror (net_spike_lib.fresh_guest_record = pc_guest_build_fresh_record): an independent oracle of what G1
produces, including a gender / face given explicitly. The harness then mutates them (a pocket item, wallet, bank, loan, lotto, equipment, a gift letter, catalog
order / bits, quests, progress spans, state flags, appearance ranges) and checks the refusal reason + offset, plus the profile module: create / load /
atomic write / id stability / never SERVER / every bad key refused naming the key / a corrupt file preserved byte for byte.
Usage: python test_guest_g2_unit.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.abspath(os.path.join(HERE, "..", "..", "build64", "bin_fixture4")))
import net_spike_lib as L  # noqa: E402

PC = os.path.abspath(os.path.join(HERE, "..", ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"
SCRATCH = os.path.join(tempfile.gettempdir(), "acmp_guest_g2_unit")

results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


def main():
    scratch = os.path.abspath(SCRATCH)
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    fx = os.path.join(scratch, "fixtures")
    os.makedirs(fx)
    recs = [
        L.fresh_guest_record("GUESTR", "HOMETWN", 0x4A07, 0x5B01),
        L.fresh_guest_record("GUESTS", "HOMETWN", 0x4A08, 0x5B01, gender=1, face=5),
        L.fresh_guest_record("Bella", "GuestVil", 1, 65534, gender=0, face=0),
        L.fresh_guest_record("A", "B", 0x1234, 0x4321),
        L.fresh_guest_record("Zed 12", "Home Twn", 0xEFFF, 2, gender=1, face=7),
    ]
    for i, r in enumerate(recs):
        assert len(r) == 0x2440
        with open(os.path.join(fx, "fresh_%d.bin" % i), "wb") as f:
            f.write(r)
    exe = os.path.join(scratch, "guest_g2_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include"), os.path.join(HERE, "guest_g2_selftest.c"),
                         os.path.join(PC, "src", "pc_guest_profile.c"), os.path.join(PC, "src", "pc_mp_guests.c"), os.path.join(PC, "src", "pc_mp_records.c"),
                         "-o", exe], capture_output=True, text=True, env=env)
    check("compile with msys2 gcc -Wall -Wextra succeeds", cp.returncode == 0)
    check("compile is warning-free", cp.stderr.strip() == "")
    if cp.returncode != 0:
        print(cp.stderr)
        return 1
    rp = subprocess.run([exe, os.path.join(scratch, "data"), fx], capture_output=True, text=True, env=env, timeout=120)
    out = rp.stdout
    with open(os.path.join(scratch, "selftest.out"), "w", encoding="utf-8") as f:
        f.write(out)
    passes = [ln[6:] for ln in out.splitlines() if ln.startswith("PASS: ")]
    fails = [ln[6:] for ln in out.splitlines() if ln.startswith("FAIL: ")]
    m = re.search(r"RESULT passed=(\d+) failed=(\d+)", out)
    for p in passes:
        results.append((p, True))
    for p in fails:
        results.append((p, False))
        print("FAIL - " + p)
    print("harness: %d passed, %d failed (exit %d)" % (len(passes), len(fails), rp.returncode))
    check("harness exit code 0 and the RESULT line agrees with the PASS/FAIL lines", rp.returncode == 0 and m is not None and int(m.group(1)) == len(passes) and int(m.group(2)) == 0)
    check("harness ran a meaningful number of checks (>= 100)", len(passes) >= 100)
    check("the python-mirror fresh records were accepted by the native predicate (5 of 5)", sum(1 for p in passes if "is ACCEPTED" in p and "G1 fresh record" in p) == len(recs))
    check("the scratch dir stayed under the temp dir (nothing written into the repo)", scratch.startswith(os.path.abspath(tempfile.gettempdir())))
    bad = [d for d, ok in results if not ok]
    print("\n%d checks, %d failed" % (len(results), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
