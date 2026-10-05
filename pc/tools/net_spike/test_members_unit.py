#!/usr/bin/env python3
"""test_members_unit.py - M-E UNIT test of the resident credential storage (pc/src/pc_mp_members.c, host file save/mp/members.dat).

TIER: NATIVE UNIT. Compiles members_selftest.c + pc_mp_members.c + pc_mp_records.c (the shared CRC32) with the msys2 gcc (-Wall -Wextra, must be warning-free) and runs it
against a scratch directory under the temp dir (never a repo save dir): format + field offsets, every parse refusal, the rotation .bak1/.bak2, recovery from .bak1, a CRC-corrupt
file moved to .corrupt-<timestamp> (preserved), the sticky UNTRUSTED mode, save self-validation, the find / free-slot helpers. Plus:
  - an INDEPENDENT Python writer of the v1 format parsed by the C parser (readfile mode): the layout cross-check
  - the whole program output is searched for the recognisable token bytes: the module never prints a token
  - source audit: pc_mp_members.c prints no token, the file is in CMake PC_SOURCES, the format constants match the header comment
Usage: python test_members_unit.py
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
SCRATCH = os.path.join(tempfile.gettempdir(), "acmp_members_unit")
results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


def read(rel):
    with open(os.path.join(PC, rel), encoding="utf-8", errors="replace", newline="") as f:
        return f.read().replace("\r\n", "\n")


def py_image(entries, generation=3):
    """INDEPENDENT writer of the members.dat v1 image (header 32, 16 x 80, crc32 trailer)."""
    buf = bytearray(32 + 16 * 80 + 4)
    buf[0:8] = b"ACMPMBR\x00"
    struct.pack_into("<IIIIII", buf, 8, 1, 0, 16, 80, generation, 0)
    for i, e in entries.items():
        o = 32 + i * 80
        buf[o:o + 4] = bytes([1, e.get("confirmed", 0), e["slot"], e["kind"]])
        buf[o + 4:o + 12] = e["land"]
        struct.pack_into("<HHI", buf, o + 12, e["land_id"], 0, e["hash"])
        buf[o + 20:o + 40] = e["pid"]
        buf[o + 40:o + 56] = e["token"]
        struct.pack_into("<I", buf, o + 56, e["age"])
        buf[o + 60:o + 80] = e.get("aux", b"\x00" * 20)
    struct.pack_into("<I", buf, len(buf) - 4, zlib.crc32(bytes(buf[:-4])) & 0xFFFFFFFF)
    return bytes(buf)


def main():
    scratch = os.path.abspath(SCRATCH)
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    exe = os.path.join(scratch, "members_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include"), os.path.join(HERE, "members_selftest.c"),
                         os.path.join(PC, "src", "pc_mp_members.c"), os.path.join(PC, "src", "pc_mp_records.c"), "-o", exe],
                        capture_output=True, text=True, env=env)
    check("compile with msys2 gcc -Wall -Wextra succeeds", cp.returncode == 0)
    check("compile is warning-free", cp.stderr.strip() == "")
    if cp.returncode != 0:
        print(cp.stderr)
        return 1
    rp = subprocess.run([exe, os.path.join(scratch, "data")], capture_output=True, text=True, env=env, timeout=120)
    out = rp.stdout + rp.stderr
    with open(os.path.join(scratch, "selftest.out"), "w", encoding="utf-8") as f:
        f.write(out)
    passes = [ln[6:] for ln in out.splitlines() if ln.startswith("PASS: ")]
    fails = [ln[6:] for ln in out.splitlines() if ln.startswith("FAIL: ")]
    m = re.search(r"RESULT passed=(\d+) failed=(\d+)", out)
    for p in passes:
        results.append(("U " + p, True))
    for p in fails:
        check("U " + p, False)
    print("unit harness: %d passed, %d failed (exit %d)" % (len(passes), len(fails), rp.returncode))
    check("harness exit code 0 and the RESULT line agrees with the PASS/FAIL lines", rp.returncode == 0 and m is not None and int(m.group(1)) == len(passes) and int(m.group(2)) == 0
          and len(passes) >= 40)
    check("a token NEVER appears in the program output (the module logs the corruption / UNTRUSTED paths; the test tokens are the ASCII string TOKENTOKENTOKEN<n>)",
          "TOKENTOKENTOKEN" not in out)
    check("the module's own log lines were exercised (UNREADABLE / preserved / UNTRUSTED)", "UNREADABLE" in out and "preserved as" in out and "UNTRUSTED mode" in out)

    # independent writer -> C reader
    land = b"INDEPTWN"
    ents = {
        3: dict(slot=2, kind=1, land=land, land_id=0xA1B2, hash=0x11223344, pid=bytes(range(0x41, 0x41 + 20)), token=b"INDEPENDENTTOKEN", age=5, confirmed=1),
        9: dict(slot=1, kind=2, land=land, land_id=0xA1B2, hash=0x11223344, pid=bytes(range(0x61, 0x61 + 20)), token=b"INDEPENDENTTOKE2", age=6, aux=bytes(range(0x30, 0x30 + 20))),
    }
    path = os.path.join(scratch, "indep.dat")
    with open(path, "wb") as f:
        f.write(py_image(ents, generation=42))
    rr = subprocess.run([exe, os.path.join(scratch, "data2"), "readfile", path], capture_output=True, text=True, env=env, timeout=60)
    t = rr.stdout
    check("independent Python image: the C reader loads it (mode OK, generation 42)", "READ mode=1 generation=42" in t)
    check("independent image: entry 3 = resident token slot 2, confirmed, age 5, land INDEPTWN, id 41394, hash, pid 41..54",
          re.search(r"ENTRY 3 kind=1 slot=2 confirmed=1 age=5 land=INDEPTWN land_id=41394 hash=287454020 pid=" + bytes(range(0x41, 0x41 + 20)).hex(), t) is not None)
    check("independent image: entry 9 = promotion handoff slot 1, age 6", re.search(r"ENTRY 9 kind=2 slot=1 confirmed=0 age=6 ", t) is not None)
    check("the reader prints only the FIRST token byte of the independent image (a full token is never printed)", "INDEPENDENTTOKEN" not in t and "token_first_byte=49" in t)

    # source audit
    src = read("src/pc_mp_members.c")
    hdr = read("include/pc_mp_members.h")
    log_lines = [ln for ln in src.splitlines() if "mlog(" in ln or "printf(" in ln]
    check("pc_mp_members.c never passes a token to a log call (no mlog / printf line mentions a token member)", not [ln for ln in log_lines if ".token" in ln or "->token" in ln or "token[" in ln])
    check("pc_mp_members.c is in CMake PC_SOURCES", "src/pc_mp_members.c" in read("CMakeLists.txt"))
    check("the header documents the 80-byte entry (the design text said 72; its own field list adds up to 80)", "entry_size(80)" in hdr and "#define PC_MP_MEMBERS_ENTRY_SIZE    80" in hdr)
    check("the module writes tmp -> commit -> rotate .bak1/.bak2 -> atomic replace (MoveFileEx REPLACE_EXISTING | WRITE_THROUGH) like pc_mp_guests.c",
          "commit_file(fp)" in src and "rename_over(b1, b2)" in src and "rename_over(path, b1)" in src and "rename_over(tmp, path)" in src
          and "MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH" in src)
    print("%d / %d checks passed" % (sum(1 for _d, c in results if c), len(results)))
    return 0 if all(c for _d, c in results) else 1


if __name__ == "__main__":
    sys.exit(main())
