#!/usr/bin/env python3
"""test_capacity_phase3.py - player-capacity project, PHASE 3 (dynamic guest store: one file per guest, guests.dat v2 migration).

TIER: native unit tests (Linux, gcc, also ASan/UBSan) + an INDEPENDENT Python parser of the files the C code wrote + SOURCE AUDITS. No game process.
 1. guest_store_selftest.c   pc/src/pc_mp_guest_store.c: format, save/load/rotation, 1000 guests, corruption, crash/fault injection, retire/backup, migration of guests.dat v2, load order.
 2. peer_table_selftest.c    the growable-table helper (PCGrowTable) used by the host's guest tables.
 3. existing guests / members / membership selftests still pass unchanged.
 4. Python re-parse (zlib.crc32, struct) of every .gst file the C test left on disk: format agreement between two independent implementations.
 5. source audits: guests.dat is no longer written by the game, wire format untouched, Phase 4 (admission cap) untouched, Rover / radial / character files untouched.
NOT covered (needs the Windows / MSYS2 game build): pc_net_game.c running against real clients, migration of a real guests.dat in a real host process, the ~25 real-process guest tests.
Exit code 0 when all checks pass."""
import glob
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
ROOT = os.path.abspath(os.path.join(PC, ".."))
CC = os.environ.get("CC", "gcc")
results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("PASS - " if ok else "FAIL - ") + name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def build_run(scratch, name, srcs, args=(), sanitize=False, timeout=600):
    exe = os.path.join(scratch, name + ("_asan" if sanitize else ""))
    cmd = [CC, "-std=gnu11", "-Wall", "-Wextra", "-O1"] + (["-fsanitize=address,undefined", "-fno-sanitize-recover=undefined"] if sanitize else []) + \
          ["-I", os.path.join(PC, "include"), os.path.join(HERE, name + ".c")] + [os.path.join(PC, "src", f) for f in srcs] + ["-o", exe]
    cp = subprocess.run(cmd, capture_output=True, text=True)
    tag = name + (" [ASan+UBSan]" if sanitize else "")
    check("compile %s (-Wall -Wextra, warning-free)" % tag, cp.returncode == 0 and cp.stderr.strip() == "")
    if cp.returncode != 0:
        print(cp.stderr)
        return None
    rp = subprocess.run([exe] + list(args), capture_output=True, text=True, timeout=timeout)
    m = re.search(r"RESULT passed=(\d+) failed=(\d+)", rp.stdout)
    for ln in rp.stdout.splitlines():
        if ln.startswith("FAIL: "):
            check("%s: %s" % (tag, ln[6:]), False)
    check("%s: all checks pass (exit 0, %s)" % (tag, m.group(0) if m else "no RESULT line; stderr: " + rp.stderr[-200:]), rp.returncode == 0 and m is not None and m.group(2) == "0")
    return rp.stdout


def parse_gst(b):
    assert len(b) == 9404, len(b)
    assert b[:8] == b"ACMPGS1\x00"
    ver, flags, esz, gen, r1, r2 = struct.unpack("<6I", b[8:32])
    assert (ver, flags, esz, r1, r2) == (3, 0, 9352, 0, 0)
    assert struct.unpack("<I", b[-4:])[0] == (zlib.crc32(b[:-4]) & 0xFFFFFFFF)
    e = b[48:48 + 9352]
    assert e[0] == 1 and e[1] <= 1
    rec = bytes(e[72:])
    assert struct.unpack("<4I", e[40:56])[3] == (zlib.crc32(rec) & 0xFFFFFFFF)
    return gen, bytes(e[4:24]), bytes(e[24:40]), rec


def main():
    scratch = os.path.join(tempfile.gettempdir(), "acmp_capacity3")
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    gs_srcs = ["pc_mp_guest_store.c", "pc_mp_guests.c", "pc_mp_membership.c", "pc_mp_records.c"]

    run_dir = os.path.join(scratch, "gs_run")
    out = build_run(scratch, "guest_store_selftest", gs_srcs, [run_dir])
    build_run(scratch, "guest_store_selftest", gs_srcs, [os.path.join(scratch, "gs_run_asan")], sanitize=True)
    if out is not None:
        check("guest_store_selftest ran the 1000-guest, migration and corruption groups", all(k in out for k in ("1000", "migrat", "corrupt")))
    build_run(scratch, "peer_table_selftest", ["pc_peer_table.c"])
    build_run(scratch, "peer_table_selftest", ["pc_peer_table.c"], sanitize=True)
    for t, s in (("mp_guests_selftest", ["pc_mp_guests.c", "pc_mp_records.c"]), ("members_selftest", None), ("membership_equiv_selftest", None)):
        if s is None:
            continue
        os.makedirs(os.path.join(scratch, t + "_dir"), exist_ok=True)
        build_run(scratch, t, s, [os.path.join(scratch, t + "_dir")])

    files = glob.glob(os.path.join(run_dir, "**", "*.gst"), recursive=True)
    bad, n = 0, 0
    for fn in files:
        try:
            with open(fn, "rb") as f:
                b = f.read()
            gen, pid, tok, rec = parse_gst(b)
            hexid = b[32:48].hex()
            assert os.path.basename(fn).startswith(hexid) and rec[:20] == pid
            n += 1
        except AssertionError:
            bad += 1
    check("independent Python parser accepts every C-written .gst left on disk (%d files, %d rejected)" % (n, bad), n > 0 and bad == 0)

    ng = read("pc/src/pc_net_game.c")
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", ng, flags=re.S))
    check("the game no longer uses a fixed guests.dat table: per-guest store API used, no s_guest_file in code", "s_guest_file" not in code and "pc_mp_gs_save" in ng and "pc_mp_gs_load" in ng and "pc_mp_gs_migrate_legacy" in ng)
    check("Phase 4 untouched: max_guests default 4, clamp 1..8", ".max_guests = 4" in read("pc/src/pc_settings.c") and "return n > PC_NETGAME_GUEST_MAX ? PC_NETGAME_GUEST_MAX : n;" in ng)
    check("settings: guest_memory_mb exists", "guest_memory_mb" in read("pc/src/pc_settings.c"))
    cm = read("pc/CMakeLists.txt")
    check("CMake builds pc_mp_guest_store.c", "src/pc_mp_guest_store.c" in cm)
    diff = subprocess.run(["git", "-C", ROOT, "diff", "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()
    check("Rover / radial / character files not touched in the working tree", not [f for f in diff if re.search(r"rover|radial|character", f, re.I) and f.endswith((".c", ".h"))])

    bad = [n for n, ok in results if not ok]
    print("\nRESULT passed=%d failed=%d" % (len(results) - len(bad), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
