#!/usr/bin/env python3
"""test_guest_storage.py - UNIT test of the guest storage module (pc/src/pc_mp_guests.c: the host table save/mp/guests.dat and the client token file
save/mp/guest_token.dat).

TIER: storage UNIT test. Compiles pc/tools/net_spike/mp_guests_selftest.c + pc/src/pc_mp_guests.c + pc/src/pc_mp_records.c (the shared CRC32) with the
msys2 gcc (-Wall -Wextra, must be warning-free) and runs it against a scratch directory under the temp dir (never a repo save dir): format round trip,
every parse refusal (magic, older / future version, flags, slot count, truncation, file CRC, per-record CRC, flag bytes, absent entries, zero key / token /
epoch, rev range, record <-> key binding, exists byte, duplicate key / token), the rotation .bak1/.bak2, recovery from .bak1, preservation of unreadable
files (*.corrupt-*, never deleted), the sticky UNTRUSTED mode, the fault-injection crash windows, the OS random token source, and the client token file.
Plus an INDEPENDENT Python writer of the v2 format (town-keyed entries, confirmed, age) parsed by the C parser's twin in test_guest_protocol (layout cross-check) and source checks.
Usage: python test_guest_storage.py
"""
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"
SCRATCH = os.path.join(tempfile.gettempdir(), "acmp_guest_storage_unit")

results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


def read(rel):
    with open(os.path.join(PC, rel), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def main():
    scratch = os.path.abspath(SCRATCH)
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    exe = os.path.join(scratch, "mp_guests_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include"), os.path.join(HERE, "mp_guests_selftest.c"),
                         os.path.join(PC, "src", "pc_mp_guests.c"), os.path.join(PC, "src", "pc_mp_records.c"), "-o", exe],
                        capture_output=True, text=True, env=env)
    check("compile with msys2 gcc -Wall -Wextra succeeds", cp.returncode == 0)
    check("compile is warning-free", cp.stderr.strip() == "")
    if cp.returncode != 0:
        print(cp.stderr)
        return 1
    rp = subprocess.run([exe, os.path.join(scratch, "data")], capture_output=True, text=True, env=env, timeout=120)
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
    check("harness exit code 0 and the RESULT line agrees with the PASS/FAIL lines", rp.returncode == 0 and m is not None and int(m.group(1)) == len(passes)
          and int(m.group(2)) == 0)
    check("harness ran a meaningful number of checks (>= 55)", len(passes) >= 55)
    for needle, what in [("is UNREADABLE: file checksum mismatch", "checksum failure logged"), ("RECOVERED from generation .bak1", "recovery from .bak1 logged loudly"),
                         ("preserved as", "preservation of the bad file logged"), ("UNTRUSTED mode", "UNTRUSTED mode logged"),
                         ("no guests file at", "missing file logged"), ("simulated crash", "simulated crash logged"),
                         ("restored previous generation from .bak1", "restore after a failed rename logged"),
                         ("preserving it aside instead of rotating it over a good generation", "bad current file at save time logged")]:
        check("recovery decision is logged: " + what, needle in out)

    # independent Python writer of the v2 layout, cross-checked by the independent Python parser of the protocol test
    entry = 4 + 20 + 16 + 16 + 16 + 0x2440
    assert entry == 9352
    pid = bytes(range(0x41, 0x41 + 20))
    rec = pid + bytes((i * 5) & 0xFF for i in range(0x2440 - 20))
    rec = bytearray(rec)
    rec[0x1086] = 1
    rec = bytes(rec)
    buf = bytearray(32 + 8 * entry + 4)
    buf[0:8] = b"ACMPGST\x00"
    struct.pack_into("<6I", buf, 8, 2, 0, 8, entry, 5, 0)
    o = 32 + 3 * entry
    buf[o] = 1
    buf[o + 1] = 1  # confirmed
    buf[o + 4:o + 24] = pid
    buf[o + 24:o + 40] = bytes(range(0xB0, 0xC0))
    struct.pack_into("<4I", buf, o + 40, 0x77, 4, 9, zlib.crc32(rec) & 0xFFFFFFFF)  # epoch, rev, age, record crc
    buf[o + 56:o + 64] = b"TOWNNAME"
    struct.pack_into("<HHI", buf, o + 64, 0x1234, 0, 0xCAFEF00D)
    buf[o + 72:o + 72 + 0x2440] = rec
    struct.pack_into("<I", buf, len(buf) - 4, zlib.crc32(bytes(buf[:-4])) & 0xFFFFFFFF)
    pth = os.path.join(scratch, "py_guests.dat")
    with open(pth, "wb") as f:
        f.write(bytes(buf))
    sys.path.insert(0, HERE)
    os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(PC, "build64", "bin_fixture4"))
    import test_guest_protocol as TG
    try:
        pf = TG.parse_guests(pth)
        check("an independently written v2 image parses with the protocol test's parser (layout agrees)", pf["e"][3]["pid"] == pid and pf["e"][3]["rev"] == 4
              and pf["e"][3]["token"] == bytes(range(0xB0, 0xC0)) and pf["e"][0]["present"] == 0 and pf["e"][3]["confirmed"] == 1 and pf["e"][3]["age"] == 9
              and pf["e"][3]["town_land_name"] == b"TOWNNAME" and pf["e"][3]["town_land_id"] == 0x1234 and pf["e"][3]["town_terrain_hash"] == 0xCAFEF00D)
    except AssertionError as exc:
        check("independent image parses (%s)" % exc, False)

    # source checks of the module and its header
    c = read("src/pc_mp_guests.c")
    h = read("include/pc_mp_guests.h")
    check("the module is self-contained: only libc + the OS file APIs + the CRC32 of pc_mp_records (no game headers)",
          not re.search(r'#include "(?!pc_mp_guests\.h|pc_mp_records\.h)', c) and "SDL" not in c)
    check("paths: save/mp/guests.dat and save/mp/guest_token.dat; no .gci / GAF anywhere in the module's own file names",
          '"save/mp/guests.dat"' in h and '"save/mp/guest_token.dat"' in h and not re.search(r'\.gci|GAF', re.sub(r"/\*.*?\*/", "", c + h, flags=re.S)))
    check("writes are atomic: tmp -> flush + commit -> validated rotation -> MoveFileEx(REPLACE_EXISTING | WRITE_THROUGH); the serialized image is self-parsed BEFORE it is written",
          "MoveFileExA(src, dst, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)" in c and "commit_file(fp)" in c and "failed self-validation" in c)
    check("an unreadable file is MOVED ASIDE (never deleted): no remove() of a guests / token file except the tmp and the oldest valid generation being rotated out",
          sorted(set(re.findall(r"\bremove\((\w+)\)", c))) == ["b2", "tmp"] and len(re.findall(r"\bremove\(", c)) == len(re.findall(r"\bremove\((?:tmp|b2)\)", c))
          and "move_aside(" in c)
    check("the OS random source has NO weak fallback (RtlGenRandom / SystemFunction036 or /dev/urandom, failure returns 0)",
          "SystemFunction036" in c and "/dev/urandom" in c and "return 0;" in c[c.index("int pc_mp_guests_random_bytes"):c.index("/* ---------------- file helpers")] and "rand()" not in c)
    check("M2: the header documents the bearer-token limitation and the TOFU exposure (replay on an unencrypted transport, re-mint / eviction rules, per-address limit)",
          "BEARER token" in h and "TRUST ON FIRST USE" in h and "per-address limit" in h and "CONFIRMED" in h and "v1" in h and "never released" in h)
    check("versioning: older AND newer versions are refused", "PC_MP_GST_ERR_VERSION_OLD" in c and "PC_MP_GST_ERR_VERSION_FUTURE" in c)

    bad = [d for d, ok in results if not ok]
    print("\n%d checks, %d failed" % (len(results), len(bad)))
    shutil.rmtree(os.path.join(scratch, "data"), ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
