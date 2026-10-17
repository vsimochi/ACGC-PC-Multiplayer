#!/usr/bin/env python3
"""test_version_native.py - the version header pc/include/pc_version.h, compiled for real (gcc + windres), and the built exe's version resource.

N1  a C file including ONLY pc_version.h compiles with -Wall -Wextra -Werror and prints PC_VERSION_STRING / BASE / NUMERIC / RC numbers / DEV
N2  the values are exactly the header's numbers: "X.Y.Z-dev" (DEV 1) | "X.Y.Z" | "X.Y.Z.0" | {X,Y,Z,0}
N3  a temp copy of the header with PC_VERSION_DEV 0 (what the release commit does) gives PC_VERSION_STRING "X.Y.Z" (no "-dev")
N4  windres compiles pc/res/app.rc (it #includes the header) and its decompiled resource carries FILEVERSION / PRODUCTVERSION X,Y,Z,0 and both strings "X.Y.Z.0"
N5  (if pc\\build64_w3\\bin\\AnimalCrossing.exe exists) the BUILT exe's version resource reports X.Y.Z.0 (PowerShell VersionInfo)
TIER: NATIVE (no game process). Usage: python test_version_native.py [--exe PATH]"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
UCRT = r"C:\msys64\ucrt64\bin"
results = []


def ck(name, ok, detail=None):
    results.append(bool(ok))
    print(("PASS" if ok else "FAIL") + " - " + name + ("" if ok or detail is None else "  :: %s" % (detail,)))


PROBE = r'''#include "pc_version.h"
#include <stdio.h>
static const int rcn[4] = { PC_VERSION_RC_NUMBERS };
int main(void) {
    printf("STRING=%s\n", PC_VERSION_STRING);
    printf("BASE=%s\n", PC_VERSION_BASE_STRING);
    printf("NUMERIC=%s\n", PC_VERSION_NUMERIC_STRING);
    printf("RC=%d,%d,%d,%d\n", rcn[0], rcn[1], rcn[2], rcn[3]);
    printf("DEV=%d\n", PC_VERSION_DEV);
    return 0;
}
'''


def probe(cc, incdir, work, tag):
    c = os.path.join(work, "probe_%s.c" % tag)
    exe = os.path.join(work, "probe_%s.exe" % tag)
    with open(c, "w") as f:
        f.write(PROBE)
    b = subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + incdir, c, "-o", exe], capture_output=True, text=True, cwd=work)
    if b.returncode != 0:
        return None, b.stdout + b.stderr
    r = subprocess.run([exe], capture_output=True, text=True, cwd=work, timeout=30)
    return dict(ln.split("=", 1) for ln in r.stdout.split() if "=" in ln), r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=os.path.join(PC, "build64_w3", "bin", "AnimalCrossing.exe"))
    a = ap.parse_args()
    env_path = UCRT + os.pathsep + os.environ.get("PATH", "")
    os.environ["PATH"] = env_path
    cc = shutil.which("gcc") or os.path.join(UCRT, "gcc.exe")
    windres = shutil.which("windres") or os.path.join(UCRT, "windres.exe")
    hdr = open(os.path.join(PC, "include", "pc_version.h"), "rb").read().decode()
    nums = {k: int(re.search(r"^#define PC_VERSION_%s +(\d+)" % k, hdr, re.M).group(1)) for k in ("MAJOR", "MINOR", "PATCH", "DEV")}
    base = "%d.%d.%d" % (nums["MAJOR"], nums["MINOR"], nums["PATCH"])
    work = tempfile.mkdtemp(prefix="version_native_")
    try:
        got, out = probe(cc, os.path.join(PC, "include"), work, "tree")
        ck("N1 a C file including only pc_version.h compiles with -Wall -Wextra -Werror and runs", got is not None, out)
        if got is not None:
            want = {"STRING": base + ("-dev" if nums["DEV"] else ""), "BASE": base, "NUMERIC": base + ".0",
                    "RC": "%d,%d,%d,0" % (nums["MAJOR"], nums["MINOR"], nums["PATCH"]), "DEV": str(nums["DEV"])}
            ck("N2 the string / number forms derive from the header's numbers %s" % got, got == want, want)
            ck("N2b this development line shows '0.3.1-dev' (resource numbers 0.3.1.0)", got.get("STRING") == "0.3.1-dev" and got.get("NUMERIC") == "0.3.1.0")
        rel_inc = os.path.join(work, "relinc")
        os.makedirs(rel_inc)
        rel_hdr = re.sub(r"^#define PC_VERSION_DEV +1", "#define PC_VERSION_DEV   0", hdr, flags=re.M)
        with open(os.path.join(rel_inc, "pc_version.h"), "w", newline="") as f:
            f.write(rel_hdr)
        got0, out0 = probe(cc, rel_inc, work, "release")
        ck("N3 with PC_VERSION_DEV 0 (the release commit) the shown string is exactly '%s' (no -dev) %s" % (base, got0),
           got0 is not None and got0.get("STRING") == base and got0.get("NUMERIC") == base + ".0", out0)
        o = os.path.join(work, "app.o")
        rcout = os.path.join(work, "app_decompiled.rc")
        r1 = subprocess.run([windres, "-O", "coff", "app.rc", "-o", o], capture_output=True, text=True, cwd=os.path.join(PC, "res"))
        r2 = subprocess.run([windres, "-O", "rc", "app.rc", "-o", rcout], capture_output=True, text=True, cwd=os.path.join(PC, "res")) if r1.returncode == 0 else r1
        txt = open(rcout, "rb").read().decode("utf-8", "replace") if r2.returncode == 0 and os.path.isfile(rcout) else ""
        n4 = "%d,%d,%d,0" % (nums["MAJOR"], nums["MINOR"], nums["PATCH"])
        fv = re.search(r"FILEVERSION\s+(\d+),\s*(\d+),\s*(\d+),\s*(\d+)", txt)
        pv = re.search(r"PRODUCTVERSION\s+(\d+),\s*(\d+),\s*(\d+),\s*(\d+)", txt)
        ck("N4 windres compiles app.rc (#include of pc_version.h); the resource has FILEVERSION / PRODUCTVERSION %s and FileVersion / ProductVersion \"%s.0\"" % (n4, base),
           r1.returncode == 0 and fv is not None and ",".join(fv.groups()) == n4 and pv is not None and ",".join(pv.groups()) == n4
           and re.search(r'VALUE "FileVersion", "%s\.0"' % re.escape(base), txt) and re.search(r'VALUE "ProductVersion", "%s\.0"' % re.escape(base), txt),
           (r1.stderr + r2.stderr)[-400:] or txt[-600:])
        if os.path.isfile(a.exe):
            ps = ("$v=(Get-Item '%s').VersionInfo; $v.ProductVersion + '|' + $v.FileVersion + '|' + ($v.FileMajorPart,$v.FileMinorPart,$v.FileBuildPart,$v.FilePrivatePart -join '.') + '|' + $v.ProductName" % a.exe)
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
            vi = r.stdout.strip()
            ck("N5 the built exe (%s, %d bytes) reports %s.0 in ProductVersion, FileVersion and FILEVERSION: %s" % (a.exe, os.path.getsize(a.exe), base, vi),
               vi.startswith("%s.0|%s.0|%s.0|" % (base, base, base)))
        else:
            print("INFO - N5 skipped: no exe at %s" % a.exe)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("-" * 60)
    print("%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
