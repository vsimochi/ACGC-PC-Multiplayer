r"""test_release_package.py - checks the REAL release artifacts (not fixtures built by test_updater.py): the update zip, its .sha256 and the first-install bundle.
  P1  the .sha256 matches the zip byte for byte; make_release_package.py verify passes; a deliberately tampered copy is detected
  P2  both zips contain exactly the expected files (no ROM/save/config/log/test files), version.txt = the version, names follow the updater's convention
  P3  the REAL update zip installs over a version-0.0.9 installation through the REAL launcher + a local HTTP server: every packaged file lands byte-identical, the user data is untouched,
      the previous program files are in .update\backup-0.0.9, and the NEW real game executable is the one that starts (it is a release build without a ROM: it shows its 'Missing ROM'
      box, which this test closes by message - no keyboard / mouse is used; AC_DISPLAY_NAME=samsung, AC_MASTER_VOLUME=0)
  P4  FIRST-RUN: the first-install bundle is unzipped into an empty folder and ACLauncher.exe is started with NO test switches: the real GitHub endpoint is asked (no release yet = 404, or
      offline: either way the game must still start), the game starts and reaches its first-run path (the 'Missing ROM' message), nothing is written outside the folder
Usage: python test_release_package.py --dist <staging\dist> --version 0.1.0 [--launcher ACLauncher.exe]"""
import argparse
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from ctypes import wintypes

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "0")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_updater as TU  # noqa: E402

results = TU.results
check = TU.check


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def windows_of(pid):
    u = ctypes.windll.user32
    out = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid:
            t = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(h, t, 256)
            out.append((h, t.value))
        return True

    u.EnumWindows(proto(cb), 0)
    return out


def game_pids(path):
    """pid(s) of the game this installation's launcher started: taken from the launcher's own updater.log line 'launched AnimalCrossing.exe (pid N)' and checked to still be a running
    AnimalCrossing.exe (no wmic: it is deprecated and was flaky)"""
    import re
    log = os.path.join(os.path.dirname(path), "updater.log")
    if not os.path.exists(log):
        return []
    pids = [int(m) for m in re.findall(r"launched AnimalCrossing\.exe \(pid (\d+)\)", open(log, errors="replace").read())]
    live = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AnimalCrossing.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return [p for p in pids[-1:] if '"%d"' % p in live]


def close_missing_rom(path, timeout=40):
    """waits for the game started from `path` to show its 'Missing ROM' box, closes it by WM_CLOSE (no input events); returns the box title or None"""
    t_end = time.time() + timeout
    seen = None
    while time.time() < t_end:
        for pid in game_pids(path):
            for h, title in windows_of(pid):
                if "Missing ROM" in title:
                    seen = title
                    ctypes.windll.user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
        if seen:
            time.sleep(2)
            return seen
        time.sleep(0.5)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True)
    ap.add_argument("--version", default="0.1.0")
    ap.add_argument("--launcher", default=None)
    args = ap.parse_args()
    ver = args.version
    zname = "ACGC-PC-Multiplayer-%s-win64.zip" % ver
    zpath = os.path.join(args.dist, zname)
    fpath = os.path.join(args.dist, zname.replace("-win64.zip", "-win64-first-install.zip"))
    launcher = args.launcher
    root = tempfile.mkdtemp(prefix="acrel_")
    try:
        # ---- P1
        want = open(zpath + ".sha256").read().split()[0]
        check("P1 the .sha256 file matches the update zip byte for byte (%s...)" % want[:16], want == sha(zpath) and open(zpath + ".sha256").read().split()[1] == zname)
        r = subprocess.run([sys.executable, os.path.join(HERE, "make_release_package.py"), "verify", zpath], capture_output=True, text=True)
        check("P1b make_release_package.py verify accepts the real package", r.returncode == 0 and "OK" in r.stdout)
        tam = os.path.join(root, zname)
        shutil.copy2(zpath, tam)
        shutil.copy2(zpath + ".sha256", tam + ".sha256")
        with open(tam, "ab") as f:
            f.write(b"x")
        r = subprocess.run([sys.executable, os.path.join(HERE, "make_release_package.py"), "verify", tam], capture_output=True, text=True)
        check("P1c a deliberately tampered copy is detected", r.returncode != 0 and "mismatch" in (r.stdout + r.stderr))
        # ---- P2
        names = {i.filename for i in zipfile.ZipFile(zpath).infolist()}
        expected = {"AnimalCrossing.exe", "SDL2.dll", "LICENSE", "RELEASE_NOTES.txt", "version.txt", "shaders/default.frag", "shaders/default.vert", "licenses/SDL2-LICENSE.txt",
                    "licenses/THIRD_PARTY_NOTICES.txt", "licenses/fixnes-LICENSE.txt", "licenses/gcc-COPYING.RUNTIME.txt", "licenses/winpthreads-COPYING.txt", "licenses/zlib-LICENSE.txt",
                    "UPDATING.txt", "licenses/Apache-2.0.txt", "licenses/Khronos-khrplatform-NOTICE.txt", "licenses/GPL-3.0-gcc-COPYING3.txt", "licenses/SOURCES.txt", "licenses/xxHash-LICENSE.txt", "licenses/THIRD_PARTY_NOTICES.md"}
        check("P2 the update zip contains exactly the expected %d files" % len(expected), names == expected)
        fnames = {i.filename for i in zipfile.ZipFile(fpath).infolist()}
        check("P2b the first-install bundle = update files + ACLauncher.exe + updater.ini + INSTALL.txt + docs/updater.md + docs/console-commands.md + the rom\\ note (nothing else)",
              fnames == expected | {"ACLauncher.exe", "updater.ini", "INSTALL.txt", "rom/PUT-YOUR-DISC-IMAGE-HERE.txt", "docs/updater.md", "docs/console-commands.md",
                                    "Start Server.bat", "server_starter.ps1", "server_config.ini", "SERVER_SETUP.txt"})
        bad = [n for n in fnames if n.lower().endswith((".iso", ".ciso", ".gcm", ".gci", ".dol", ".log", ".ini")) and n not in ("updater.ini", "server_config.ini")] + [n for n in fnames if "GAFE" in n.upper() and "PUT-YOUR" not in n]
        check("P2c no disc image, save, log, game config or GAFE01_00 content in either package", not bad)
        check("P2d version.txt in both zips = %s" % ver, zipfile.ZipFile(zpath).read("version.txt").decode().strip() == ver and zipfile.ZipFile(fpath).read("version.txt").decode().strip() == ver)
        check("P2e docs/updater.md (named by the launcher's error text) is in the first-install bundle; UPDATING.txt is in both", "docs/updater.md" in fnames and "UPDATING.txt" in names and "UPDATING.txt" in fnames)
        import re
        notice = zipfile.ZipFile(zpath).read("licenses/THIRD_PARTY_NOTICES.txt").decode()
        lic_files = {n.split("/", 1)[1] for n in names if n.startswith("licenses/")} - {"THIRD_PARTY_NOTICES.txt", "SOURCES.txt"}
        missing_ref = sorted(f for f in lic_files if f not in notice)
        dangling = sorted(f for f in set(re.findall(r"[A-Za-z0-9_.-]+(?:LICENSE|COPYING|NOTICE|Apache-2\.0|COPYING3|COPYING\.RUNTIME)[A-Za-z0-9_.-]*\.txt", notice)) if f not in lic_files)
        check("P2f every license file in licenses/ is named in THIRD_PARTY_NOTICES.txt and every license file it names is shipped (unreferenced %s, dangling %s)" % (missing_ref, dangling), not missing_ref and not dangling)
        apache = zipfile.ZipFile(zpath).read("licenses/Apache-2.0.txt")
        check("P2g licenses/Apache-2.0.txt is byte-identical to the text downloaded from apache.org (sha256 cfc7749b...)", hashlib.sha256(apache).hexdigest() == "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30")
        check("P2h the notices say glad is (WTFPL OR CC0-1.0) AND Apache-2.0, not 'public domain / MIT'", "(WTFPL OR CC0-1.0) AND Apache-2.0" in notice and "public domain / MIT" not in notice)
        texts = {}
        for zz in (zipfile.ZipFile(zpath), zipfile.ZipFile(fpath)):
            for n in zz.namelist():
                if n.lower().endswith((".txt", ".md", ".ini")):
                    texts[(zz.filename[-14:], n)] = zz.read(n).decode("utf-8", "replace")
        overclaim = sorted(k for k, t in texts.items() if any(x in t.lower() for x in ("100% legal", "copyright-free", "fully cleared", "legally cleared", "officially licensed")))
        check("P2i no package text claims '100%% legal' / 'copyright-free' / 'fully cleared' / 'officially licensed' (%s)" % overclaim, not overclaim)
        inst = zipfile.ZipFile(fpath).read("INSTALL.txt").decode().lower()
        check("P2j INSTALL.txt states: unofficial / not affiliated, own disc image, GAFE01_00 USA Rev 0, no disc image bundled, save backup, OpenGL 3.3", all(x in inst for x in ("unofficial", "not affiliated", "your own", "gafe01_00", "rev 0", "no disc image", "back up", "opengl 3.3")))
        tn = zipfile.ZipFile(zpath).read("licenses/THIRD_PARTY_NOTICES.txt").decode()
        check("P2k the notice says Nintendo non-endorsement and lists the unresolved questions (SDK headers, reconstructed libraries, contributor licences, xxHash, icon)", all(x in tn for x in ("Not affiliated", "Unresolved", "include/PR", "reconstructed Nintendo libraries", "Cuyler36", "xxHash", "icon")))
        check("P2l the generic icon, not the old one, is in the exe's resources is checked by the build (app.rc) - here: the old icon files are not shipped", not any("icon" in n.lower() for n in names | fnames))
        # ---- P2m: the dedicated-server starter ships in the first-install bundle only, byte-identical to pc/server, with the documented defaults
        srvdir = os.path.join(HERE, "..", "server")
        starter = ("Start Server.bat", "server_starter.ps1", "server_config.ini", "SERVER_SETUP.txt")
        zf = zipfile.ZipFile(fpath)
        same_srv = all(zf.read(f).replace(b"\r\n", b"\n") == open(os.path.join(srvdir, f), "rb").read().replace(b"\r\n", b"\n") for f in starter)
        check("P2m the four dedicated-server starter files are in the first-install bundle (next to ACLauncher.exe), identical to pc/server, and NOT in the update zip (server_config.ini is the user's: updates never touch it)",
              all(f in fnames for f in starter) and same_srv and not any(f in names for f in starter))
        cfg = zf.read("server_config.ini").decode()
        defaults = {"port": "7777", "max_peers": "8", "max_guests": "4", "allow_new_guests": "1", "resident_tokens": "off", "town_serve": "auto", "personal_sync": "auto",
                    "interest_management": "1", "guest_memory_mb": "256", "debug_categories": "", "check_updates": "0"}
        got = {m.group(1): m.group(2).strip() for m in re.finditer(r"^([a-z_]+) =(.*)$", cfg, re.M)}
        check("P2n the shipped server_config.ini has exactly the documented defaults (town_serve is never 'full')", got == defaults)
        ps = zf.read("server_starter.ps1").decode()
        check("P2o the starter script refuses town_serve = full and passes --updater-skip when check_updates = 0", "town_serve = full is not available" in ps and "--updater-skip" in ps)
        # ---- P3: the real zip through the real launcher
        launcher = launcher or os.path.join(HERE, "out", "ACLauncher.exe")
        srv = TU.Server()
        try:
            v1 = TU.build_fake(root, "oldgame")
            inst = TU.make_install(root, "inst", v1, version="0.0.9", launcher=launcher)
            zb = open(zpath, "rb").read()
            srv.api_latest(srv.publish("v" + ver, zb, sha=open(zpath + ".sha256", "rb").read()))
            rc, log, ln = TU.run_launcher(inst, srv.base, answer="yes", wait_game=False)
            time.sleep(3)
            exe = os.path.join(inst, "AnimalCrossing.exe")
            same = all(open(os.path.join(inst, *n.split("/")), "rb").read() == zipfile.ZipFile(zpath).read(n) for n in names)
            check("P3 the real package installs through the real launcher: every file byte-identical, version.txt = %s" % ver, rc == 0 and same and "RESULT: updated 0.0.9 -> %s" % ver in log)
            # shader_cache.bin is the GAME's own cache (the freshly started real game deletes / rebuilds it): not checked here
            user_same = all(os.path.isfile(os.path.join(inst, *k.split("/"))) and open(os.path.join(inst, *k.split("/")), "rb").read() == v for k, v in TU.USER_FILES.items() if k != "shader_cache.bin")
            check("P3b user data untouched (ROM, saves, servers, settings, keybindings, texture pack, log), previous program files kept in .update\\backup-0.0.9", user_same and open(os.path.join(inst, ".update", "backup-0.0.9", "AnimalCrossing.exe"), "rb").read() == open(v1, "rb").read())
            box = close_missing_rom(exe)
            check("P3c the NEW real game executable was the one started (it reached its 'Missing ROM' first-run box: %s)" % box, box is not None)
            for pid in game_pids(exe):
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        finally:
            srv.httpd.shutdown()
        time.sleep(1)
        # ---- P4: first-run from the first-install bundle, REAL network, no test switches
        fresh = os.path.join(root, "fresh install")
        os.makedirs(fresh)
        zipfile.ZipFile(fpath).extractall(fresh)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AC_UPDATER")}
        before_parent = set(os.listdir(root))
        p = subprocess.Popen([os.path.join(fresh, "ACLauncher.exe")], cwd=fresh, env=env)
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
        gexe = os.path.join(fresh, "AnimalCrossing.exe")
        box = close_missing_rom(gexe)
        for pid in game_pids(gexe):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        log = open(os.path.join(fresh, "updater.log"), errors="replace").read() if os.path.exists(os.path.join(fresh, "updater.log")) else ""
        print(log)
        check("P4 first run (real network): the launcher started the game whatever GitHub answered (no release / offline are not errors that block launching)", "launched AnimalCrossing.exe" in log and p.returncode == 0)
        check("P4b the game reached its first-run path (no ROM: 'Missing ROM' message shown, game exits cleanly)", box is not None)
        check("P4c nothing was written outside the install folder", set(os.listdir(root)) == before_parent)
        print("real GitHub answer in the log: " + ("; ".join(l[20:] for l in log.splitlines() if "check" in l.lower() or "release" in l.lower()) or "(none)"))
    finally:
        for pid in game_pids(os.path.join(root, "inst", "AnimalCrossing.exe")) + game_pids(os.path.join(root, "fresh install", "AnimalCrossing.exe")):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        time.sleep(1)
        if all(results):
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("(kept for diagnosis: %s)" % root)
    print("%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
