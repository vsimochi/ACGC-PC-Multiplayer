#!/usr/bin/env python3
r"""make_release_package.py - builds the release package the launcher (ACLauncher.exe) installs, and verifies one. See docs/updater.md.

  python make_release_package.py build  --version 1.1.0 --build-dir pc\build64\bin --launcher pc\updater\out\ACLauncher.exe --out dist
  python make_release_package.py verify dist\ACGC-PC-Multiplayer-1.1.0-win64.zip

build: packs ONLY the managed program files found in --build-dir (AnimalCrossing.exe, SDL2.dll, shaders\) plus version.txt, LICENSE and RELEASE_NOTES.txt when
present, writes ACGC-PC-Multiplayer-<version>-win64.zip and ACGC-PC-Multiplayer-<version>-win64.zip.sha256 ("<hex>  <file name>").
It never packs rom\, save\, servers\, settings.ini, keybindings.ini, texture_pack\, logs, disc images or anything else.
The launcher itself (ACLauncher.exe) is NOT part of the package (a running launcher cannot replace itself); distribute it in the first-install zip, see docs/updater.md.
verify: re-hashes the zip against its .sha256 and applies the same safety rules as the launcher (allow-list, path rules, no links, size limits)."""
import argparse
import hashlib
import os
import re
import sys
import zipfile

SEMVER = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}(-[0-9A-Za-z.]{1,40})?$")
EXACT = {"AnimalCrossing.exe", "SDL2.dll", "version.txt", "LICENSE", "RELEASE_NOTES.txt", "UPDATING.txt"}
PREFIXES = ("shaders/", "licenses/")
SEG = re.compile(r"^[A-Za-z0-9_.+() -]+$")
RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)
FORBIDDEN_SUFFIX = (".iso", ".ciso", ".gcm", ".gcz", ".rvz", ".gci", ".dol", ".log")
SERVER_FILES = ("Start Server.bat", "server_starter.ps1", "server_config.ini", "SERVER_SETUP.txt")  # first-install bundle only (see first_install())


def managed_ok(name):
    return name in EXACT or name.startswith(PREFIXES)


def rel_ok(name):
    if not name or len(name) > 200 or name.startswith("/") or "\\" in name:
        return False
    for seg in name.rstrip("/").split("/"):
        if seg in ("", ".", "..") or not SEG.match(seg) or seg[-1] in ". " or RESERVED.match(seg):
            return False
    return True


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def collect(build_dir):
    out = []
    for name in ("AnimalCrossing.exe", "SDL2.dll", "LICENSE", "RELEASE_NOTES.txt", "UPDATING.txt"):
        if os.path.isfile(os.path.join(build_dir, name)):
            out.append(name)
    for sub in ("shaders", "licenses"):
        sh = os.path.join(build_dir, sub)
        if os.path.isdir(sh):
            for root, _d, files in os.walk(sh):
                for fn in sorted(files):
                    rel = os.path.relpath(os.path.join(root, fn), build_dir).replace(os.sep, "/")
                    out.append(rel)
    return out


def build(a):
    if not SEMVER.match(a.version):
        sys.exit("error: --version must be MAJOR.MINOR.PATCH[-prerelease] (no leading v)")
    if not os.path.isfile(os.path.join(a.build_dir, "AnimalCrossing.exe")):
        sys.exit("error: %s has no AnimalCrossing.exe" % a.build_dir)
    files = collect(a.build_dir)
    for f in files:
        if not rel_ok(f) or not managed_ok(f) or f.lower().endswith(FORBIDDEN_SUFFIX):
            sys.exit("error: refusing to package %r" % f)
    os.makedirs(a.out, exist_ok=True)
    name = "ACGC-PC-Multiplayer-%s-win64.zip" % a.version
    zpath = os.path.join(a.out, name)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:  # AnimalCrossing.exe first: deterministic order
            zi = zipfile.ZipInfo(f, (2020, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o100644 << 16
            with open(os.path.join(a.build_dir, f), "rb") as fh:
                z.writestr(zi, fh.read())
        zi = zipfile.ZipInfo("version.txt", (2020, 1, 1, 0, 0, 0))
        zi.external_attr = 0o100644 << 16
        z.writestr(zi, a.version + "\n")
    digest = sha256_of(zpath)
    with open(zpath + ".sha256", "w", newline="\n") as f:
        f.write("%s  %s\n" % (digest, name))
    print("wrote %s (%d files + version.txt)\nsha256 %s" % (zpath, len(files), digest))
    verify_zip(zpath)
    if a.launcher:
        first_install(a, files, name)


def first_install(a, files, name):
    """a convenience bundle for NEW users (NOT an updater asset; the launcher never installs it): the package files + ACLauncher.exe + updater.ini + a rom/ note"""
    if not os.path.isfile(a.launcher):
        sys.exit("error: --launcher %s not found" % a.launcher)
    fpath = os.path.join(a.out, name.replace("-win64.zip", "-win64-first-install.zip"))
    with zipfile.ZipFile(fpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(a.launcher, "ACLauncher.exe")
        for f in files:
            z.write(os.path.join(a.build_dir, f), f)
        z.writestr("version.txt", a.version + "\n")
        z.write(os.path.join(os.path.dirname(os.path.abspath(__file__)), "updater.ini.sample"), "updater.ini")
        z.write(os.path.join(os.path.dirname(os.path.abspath(__file__)), "INSTALL.txt"), "INSTALL.txt")
        # user docs: the launcher's own messages point at docs/updater.md. The update zip cannot carry docs/ (the launcher's managed-file list has no docs/ prefix), so only this bundle does.
        docs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs")
        for d in ("updater.md", "console-commands.md"):
            z.write(os.path.join(docs, d), "docs/" + d)
        # the dedicated-server starter (pc/server): next to ACLauncher.exe, FIRST-INSTALL ONLY. They are not in the update zip on purpose: server_config.ini belongs to the user (an update must never
        # overwrite it) and the launcher's managed-file list does not contain them, so updates leave them alone.
        srv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server")
        for f in SERVER_FILES:
            z.write(os.path.join(srv, f), f)
        z.writestr("rom/PUT-YOUR-DISC-IMAGE-HERE.txt", "Place your own Animal Crossing (USA, GAFE01_00) disc image in this folder. It is not included and is never downloaded by the updater.\n")
    fdigest = sha256_of(fpath)
    with open(fpath + ".sha256", "w", newline="\n") as sf:  # same "<hex>  <file name>" format as the update zip's sidecar (it was written by hand for 0.1.0)
        sf.write("%s  %s\n" % (fdigest, os.path.basename(fpath)))
    print("wrote %s (first-install bundle: unzip into a writable folder, then start ACLauncher.exe)\nsha256 %s" % (fpath, fdigest))


def verify_zip(zpath):
    name = os.path.basename(zpath)
    m = re.match(r"^ACGC-PC-Multiplayer-(.+)-win64\.zip$", name)
    if not m or not SEMVER.match(m.group(1)):
        sys.exit("FAIL: package name must be ACGC-PC-Multiplayer-<version>-win64.zip")
    try:
        want = open(zpath + ".sha256").read().split()[0].lower()
    except OSError:
        sys.exit("FAIL: %s.sha256 is missing" % name)
    if want != sha256_of(zpath):
        sys.exit("FAIL: SHA-256 mismatch")
    with zipfile.ZipFile(zpath) as z:
        names = []
        total = 0
        for zi in z.infolist():
            n = zi.filename
            if zi.is_dir():
                continue
            mode = (zi.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                sys.exit("FAIL: symlink entry %r" % n)
            if zi.flag_bits & 1:
                sys.exit("FAIL: encrypted entry")
            if not rel_ok(n) or not managed_ok(n) or n.lower().endswith(FORBIDDEN_SUFFIX):
                sys.exit("FAIL: unsafe or unmanaged entry %r" % n)
            names.append(n.lower())
            total += zi.file_size
        if len(set(names)) != len(names):
            sys.exit("FAIL: duplicate entries")
        if total > 1 << 30 or "animalcrossing.exe" not in names or "version.txt" not in names:
            sys.exit("FAIL: bad layout / size")
        if z.read("version.txt").decode().strip() != m.group(1):
            sys.exit("FAIL: version.txt does not match the file name")
        bad = z.testzip()
        if bad:
            sys.exit("FAIL: CRC error in %r" % bad)
    print("OK: %s verifies (%d files)" % (name, len(names)))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--version", required=True)
    b.add_argument("--build-dir", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--launcher", help="(informational) path of ACLauncher.exe; it is not packed")
    v = sub.add_parser("verify")
    v.add_argument("zip")
    a = ap.parse_args()
    if a.cmd == "build":
        build(a)
    else:
        verify_zip(a.zip)


if __name__ == "__main__":
    main()
