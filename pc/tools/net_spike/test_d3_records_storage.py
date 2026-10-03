#!/usr/bin/env python3
"""test_d3_records_storage.py - D3-4 storage module unit test (pc/src/pc_mp_records.c), NATIVE, no game process.

TIER: storage UNIT test. Compiles pc/tools/net_spike/mp_records_selftest.c + pc/src/pc_mp_records.c with the msys2 gcc
(C:\\msys64\\ucrt64\\bin\\gcc) and runs it against a scratch directory under the campaign scratchpad (never the repo's save dirs);
the C harness covers: new file, normal update, rotation generations, interrupted write simulation (partial tmp, complete tmp
without rename, crash after rotation, rename failure -> .bak1 restore, disk-full write failure), truncated file, bit-flip
corruption (checksum), invalid lengths/counts/identity/ranges, older version (refused), unsupported future version (refused),
all generations corrupt -> UNTRUSTED with the bad files preserved, pre-migration backup round trip, bit-rot of the current file at
save time (must not displace a good generation), only-.bak2 recovery.
This wrapper additionally checks (a) the compile is warning-free, (b) every harness PASS/FAIL line, (c) the recovery decisions are
LOGGED (log lines of the module), (d) an INDEPENDENT Python writer of the format v1 is accepted by the C parser (and a Python-
side mutation is rejected), (e) source properties: no disk path ending in gci/GAF, no deletion of unreadable files, sidecar path
is a sibling of card_a.
Usage: python test_d3_records_storage.py [--scratch DIR]
"""
import argparse
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
DEFAULT_SCRATCH = os.path.join(os.environ.get("MP_REC_SCRATCH_ROOT",
                                              os.path.join(tempfile.gettempdir(), "acmp_d3_persist")), "storage_unit")

results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


def py_build_file(gen=7, pid=b"P" * 20, rev=3):
    """Independent writer of the v1 format (pc_mp_records.h): 4 entries, slot 1 present with a backup."""
    entry = 4 + 20 + 16 + 0x2440
    out = bytearray(32 + 4 * entry + 4)
    out[0:8] = b"ACMPREC\x00"
    struct.pack_into("<6I", out, 8, 1, 0, 4, entry, gen, 0)
    o = 32 + 1 * entry
    out[o] = 1
    out[o + 1] = 1
    out[o + 4:o + 24] = pid
    backup = bytes(pid) + bytes((i * 7) & 0xFF for i in range(0x2440 - 20))
    struct.pack_into("<4I", out, o + 24, 0x1234, rev, 0xCAFEBABE, zlib.crc32(backup) & 0xFFFFFFFF)
    out[o + 40:o + 40 + 0x2440] = backup
    struct.pack_into("<I", out, len(out) - 4, zlib.crc32(bytes(out[:-4])) & 0xFFFFFFFF)
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default=DEFAULT_SCRATCH)
    args = ap.parse_args()
    scratch = os.path.abspath(args.scratch)
    if os.path.normcase(scratch).startswith(os.path.normcase(os.path.join(PC, "build64"))):
        print("REFUSING: scratch must not be inside build64 (no repo save dirs)")
        return 2
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    exe = os.path.join(scratch, "mp_records_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include"),
                         os.path.join(HERE, "mp_records_selftest.c"), os.path.join(PC, "src", "pc_mp_records.c"), "-o", exe],
                        capture_output=True, text=True, env=env)
    check("compile with msys2 gcc -Wall -Wextra succeeds", cp.returncode == 0)
    check("compile is warning-free", cp.stderr.strip() == "")
    if cp.returncode != 0:
        print(cp.stderr)
        return 1
    data = os.path.join(scratch, "data")
    rp = subprocess.run([exe, data], capture_output=True, text=True, env=env, timeout=120)
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
    check("harness exit code 0 and RESULT line agrees with the PASS/FAIL lines",
          rp.returncode == 0 and m is not None and int(m.group(1)) == len(passes) and int(m.group(2)) == 0)
    check("harness ran a meaningful number of checks (>= 55)", len(passes) >= 55)

    # recovery decisions are LOGGED
    for needle, what in [
        ("is UNREADABLE: truncated", "truncated file logged"),
        ("is UNREADABLE: file checksum mismatch", "checksum failure logged"),
        ("is UNREADABLE: unsupported FUTURE version", "future version logged"),
        ("RECOVERED from generation .bak1", "recovery from .bak1 logged loudly"),
        ("RECOVERED from generation .bak2", "recovery from .bak2 logged loudly"),
        ("preserved as", "preservation of the bad file logged"),
        ("entering UNTRUSTED mode", "UNTRUSTED mode logged"),
        ("treating as a first run", "missing file logged"),
        ("simulated crash", "simulated crash logged"),
        ("restored previous generation from .bak1", "restore after failed rename logged"),
        ("save FAILED: write error", "write failure logged"),
        ("preserving it aside instead of rotating it over a good generation", "bad current file at save time logged"),
    ]:
        check("log: " + what, needle in out)

    # independent Python writer -> C parser
    pyf = os.path.join(scratch, "py_written.dat")
    blob = py_build_file()
    with open(pyf, "wb") as f:
        f.write(blob)
    r = subprocess.run([exe, "--parse", pyf], capture_output=True, text=True, env=env)
    check("independent Python-written v1 file is accepted by the C parser (rc 0, gen 7, rev 3)",
          r.returncode == 0 and "PARSE rc=0 gen=7 rev1=3" in r.stdout)
    bad = bytearray(blob)
    bad[32 + 9320 + 100] ^= 1
    with open(pyf, "wb") as f:
        f.write(bad)
    r = subprocess.run([exe, "--parse", pyf], capture_output=True, text=True, env=env)
    check("the same file with one flipped payload byte is rejected by the C parser (CRC)", r.returncode != 0 and "rc=" in r.stdout
          and "rc=0" not in r.stdout)
    # file written by the C module is accepted by the Python reader (layout agreement both ways)
    c_files = [os.path.join(dp, fn) for dp, _d, fns in os.walk(data) for fn in fns if fn == "records.dat"]
    ok_c = 0
    for p in c_files:
        b = open(p, "rb").read()
        if len(b) == len(blob) and b[:8] == b"ACMPREC\x00" and struct.unpack("<I", b[-4:])[0] == zlib.crc32(b[:-4]) & 0xFFFFFFFF:
            ok_c += 1
    check("C-written records.dat files left by the harness satisfy the Python-side layout/CRC check (%d files)" % len(c_files),
          len(c_files) >= 5 and ok_c >= max(1, len(c_files) - 3))  # a few scratch dirs end in deliberately corrupt/absent states

    # source properties
    src = open(os.path.join(PC, "src", "pc_mp_records.c"), encoding="utf-8").read()
    hdr = open(os.path.join(PC, "include", "pc_mp_records.h"), encoding="utf-8").read()
    check("sidecar path is save/mp/records.dat (sibling of save/card_a, not inside card_*)",
          '#define PC_MP_RECORDS_PATH        "save/mp/records.dat"' in hdr and "card_" not in hdr.split("PC_MP_RECORDS_PATH")[1].split("\n")[0])
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    check("no .gci / GAF extension or name in the module code or path macro",
          not re.search(r"gci|GAF", code, re.I) and not re.search(r'"[^"]*\.(gci|GAF)[^"]*"', re.sub(r"/\*.*?\*/", "", hdr, flags=re.S) + code, re.I))
    check("an unreadable file is only ever moved aside: remove() is applied to tmp files only",
          all("tmp" in ln or "b2" in ln for ln in code.splitlines() if re.search(r"\bremove\(", ln)))
    check("bak2 removal happens only after the current file validated (inside the 'cr == PC_MP_REC_OK' branch)",
          re.search(r"if \(cr == PC_MP_REC_OK\) \{\s*if \(file_exists\(b2\)\) \{\s*remove\(b2\);", code) is not None)
    check("fault hook default is OFF and never set by game code",
          "static int s_fault_on = 0;" in src and "pc_mp_records_set_fault" not in open(os.path.join(PC, "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read())
    check("module is self-contained (includes only libc + windows.h/io.h/direct.h, no game headers)",
          all(i in ("pc_mp_records.h", "errno.h", "stdarg.h", "stdio.h", "stdlib.h", "string.h", "sys/stat.h", "time.h", "direct.h", "io.h",
                    "windows.h", "fcntl.h", "unistd.h", "dirent.h") for i in re.findall(r'#include\s+[<"]([^>"]+)[>"]', src)))
    check("CMake lists the module in PC_SOURCES", "src/pc_mp_records.c" in open(os.path.join(PC, "CMakeLists.txt"), encoding="utf-8").read())

    failed = [d for d, ok in results if not ok]
    print("-" * 60)
    print("%d/%d checks passed" % (len(results) - len(failed), len(results)))
    for d in failed:
        print("FAILED: " + d)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
