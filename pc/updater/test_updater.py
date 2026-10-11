#!/usr/bin/env python3
r"""test_updater.py - offline tests of ACLauncher.exe against a LOCAL HTTP fixture server (no GitHub, no game, no display, no audio).

Each case builds a throw-away installation under %TEMP% (fake AnimalCrossing.exe = a tiny compiled program that records that it was launched and with which arguments),
serves a scripted GitHub-like API + assets from 127.0.0.1, runs the REAL launcher with AC_UPDATER_TEST=1 (which enables --updater-test-api / --updater-answer /
--updater-no-ui), and checks the files on disk and the launcher's updater.log. Protected user files (saves, config, ROM, texture pack, servers) are hashed before/after.
Usage: python test_updater.py [--launcher out\\ACLauncher.exe]"""
import argparse
import ctypes
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import make_release_package as MP  # noqa: E402

OWNER, REPO = "vsimochi", "ACGC-PC-Multiplayer"
results = []


def check(desc, cond):
    print(("PASS" if cond else "FAIL") + " - " + desc)
    results.append(bool(cond))


# ------------------------------------------------------------------ fake game exes
FAKE_C = r'''#include <stdio.h>
#include <string.h>
#include <windows.h>
int main(int c, char** v) {
    FILE* f = fopen("launched.txt", "w");
    int i;
    fprintf(f, "%s", MARK);
    for (i = 1; i < c; i++) fprintf(f, " %s", v[i]);
    fclose(f);
    if (c > 1 && strcmp(v[1], "--sleep") == 0) Sleep(20000);
    return 0;
}
'''


def build_fake(tmp, mark):
    src = os.path.join(tmp, "fake.c")
    open(src, "w").write(FAKE_C)
    exe = os.path.join(tmp, "fake_%s.exe" % mark)
    env = dict(os.environ, PATH=r"C:\msys64\ucrt64\bin;" + os.environ["PATH"])
    subprocess.run(["C:/msys64/ucrt64/bin/gcc.exe","-O1", '-DMARK="%s"' % mark, "-o", exe, src], check=True, env=env)
    return exe


# ------------------------------------------------------------------ the scripted server
class Scenario:
    def __init__(self):
        self.routes = {}  # path -> (status, headers, body) or callable(handler)
        self.hits = []


class Handler(http.server.BaseHTTPRequestHandler):
    scenario = None

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        sc = Handler.scenario
        sc.hits.append(self.path)
        r = sc.routes.get(self.path.split("?")[0])
        if r is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if callable(r):
            r(self)
            return
        status, headers, body = r
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def truncated(body, at):
    def fn(h):
        h.send_response(200)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body[:at])
        h.wfile.flush()
        h.connection.close()
    return fn


class Server:
    def __init__(self):
        self.sc = Handler.scenario = Scenario()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def reset(self):
        self.sc.routes.clear()
        self.sc.hits.clear()

    def publish(self, tag, zbytes, sha=None, pre=False, asset_url_repo=None, extra_assets=(), digest=None, name=None):
        ver = tag.lstrip("v")
        zname = name or "ACGC-PC-Multiplayer-%s-win64.zip" % ver
        repo_path = asset_url_repo or "%s/%s" % (OWNER, REPO)
        zurl = "%s/%s/releases/download/%s/%s" % (self.base, repo_path, tag, zname)
        surl = zurl + ".sha256"
        sha = sha if sha is not None else ("%s  %s\n" % (hashlib.sha256(zbytes).hexdigest(), zname)).encode()
        zasset = {"name": zname, "browser_download_url": zurl, "size": len(zbytes), "state": "uploaded"}
        if digest is not None:
            zasset["digest"] = digest
        rel = {"tag_name": tag, "draft": False, "prerelease": pre, "assets": [zasset, {"name": zname + ".sha256", "browser_download_url": surl, "size": len(sha), "state": "uploaded"}] + list(extra_assets)}
        for p, b in (("/%s/%s/releases/download/%s/%s" % (OWNER, REPO, tag, zname), zbytes), ("/%s/%s/releases/download/%s/%s.sha256" % (OWNER, REPO, tag, zname), sha)):
            self.sc.routes[p] = (200, {"Content-Type": "application/octet-stream"}, b)
        return rel

    def api_latest(self, rel):
        self.sc.routes["/repos/%s/%s/releases/latest" % (OWNER, REPO)] = (200, {"Content-Type": "application/json"}, json.dumps(rel).encode())

    def api_list(self, rels):
        self.sc.routes["/repos/%s/%s/releases" % (OWNER, REPO)] = (200, {"Content-Type": "application/json"}, json.dumps(rels).encode())


# ------------------------------------------------------------------ install fixture
USER_FILES = {
    "settings.ini": b"[settings]\nmax_fps=60\n",
    "server_config.ini": b"port = 7777\nmax_peers = 254\nmax_guests = 254\ncheck_updates = 1\n",
    "servers/default/residents/members.dat": os.urandom(256),
    "servers/default/residents/records.dat": os.urandom(256),
    "servers/default/work_jobs.dat": os.urandom(128),
    "servers/default/logs/server.log": b"server log\n",
    "keybindings.ini": b"[keys]\na=Z\n",
    "rom/GAFE01_00.iso": b"NOT-A-REAL-DISC-IMAGE-" * 100,
    "save/card_a/Animal Crossing.gci": os.urandom(4096),
    "save/mp/guest.ini": b"name=Zed\n",
    "servers/default/town/card_a/town.gci": os.urandom(1024),
    "texture_pack/tex1.dds": b"DDS " * 64,
    "shader_cache.bin": b"cache",
    "game.log": b"old log\n",
}


def make_install(root, name, fake_v1, version="1.0.0", launcher=None, with_version=True):
    d = os.path.join(root, name)
    os.makedirs(d)
    shutil.copy2(launcher, os.path.join(d, "ACLauncher.exe"))
    shutil.copy2(fake_v1, os.path.join(d, "AnimalCrossing.exe"))
    open(os.path.join(d, "SDL2.dll"), "wb").write(b"sdl-old")
    os.makedirs(os.path.join(d, "shaders"))
    open(os.path.join(d, "shaders", "a.glsl"), "wb").write(b"shader-old")
    if with_version:
        open(os.path.join(d, "version.txt"), "w").write(version + "\n")
    for rel, data in USER_FILES.items():
        p = os.path.join(d, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(data)
    return d


def snapshot(d, skip=("updater.log", "launched.txt")):
    out = {}
    for r, ds, fs in os.walk(d):
        ds[:] = [x for x in ds if x != ".update"]
        for f in fs:
            if f in skip:
                continue
            p = os.path.join(r, f)
            out[os.path.relpath(p, d).replace("\\", "/")] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def user_ok(d):
    return all(os.path.isfile(os.path.join(d, *k.split("/"))) and open(os.path.join(d, *k.split("/")), "rb").read() == v for k, v in USER_FILES.items())


def make_zip(files, fake_exe_bytes, ver, extra=None, order=None):
    """a valid package (via the same builder logic) -> bytes"""
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        zi = zipfile.ZipInfo("AnimalCrossing.exe", (2020, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(zi, fake_exe_bytes)
        for n, b in files.items():
            zi = zipfile.ZipInfo(n, (2020, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, b)
        for n, b in (extra or {}).items():
            if isinstance(n, zipfile.ZipInfo):
                z.writestr(n, b)
            else:
                zi = zipfile.ZipInfo(n, (2020, 1, 1, 0, 0, 0))
                z.writestr(zi, b)
        zi = zipfile.ZipInfo("version.txt", (2020, 1, 1, 0, 0, 0))
        z.writestr(zi, ver + "\n")
    return buf.getvalue()


def run_launcher(launcher_dir, base, answer="yes", extra_args=(), env_extra=None, wait_game=True):
    env = dict(os.environ, AC_UPDATER_TEST="1")
    env.update(env_extra or {})
    args = [os.path.join(launcher_dir, "ACLauncher.exe"), "--updater-test-api", base, "--updater-answer", answer, "--updater-no-ui"] + list(extra_args)
    lf = os.path.join(launcher_dir, "launched.txt")
    if os.path.exists(lf):
        os.remove(lf)
    p = subprocess.run(args, cwd=launcher_dir, env=env, timeout=120)
    if wait_game:
        t = time.time() + 10
        while not os.path.exists(lf) and time.time() < t:
            time.sleep(0.1)
        time.sleep(0.2)
    log = open(os.path.join(launcher_dir, "updater.log"), errors="replace").read() if os.path.exists(os.path.join(launcher_dir, "updater.log")) else ""
    launched = open(lf).read() if os.path.exists(lf) else None
    return p.returncode, log, launched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--launcher", default=os.path.join(HERE, "out", "ACLauncher.exe"))
    args = ap.parse_args()
    launcher = os.path.abspath(args.launcher)
    root = tempfile.mkdtemp(prefix="actest_")
    srv = Server()
    try:
        v1 = build_fake(root, "game-v1")
        v2 = build_fake(root, "game-v2")
        v2b = open(v2, "rb").read()
        good_files = {"SDL2.dll": b"sdl-new", "shaders/a.glsl": b"shader-new", "shaders/b.glsl": b"shader-b"}
        good_zip = make_zip(good_files, v2b, "1.1.0")
        n = [0]

        def fresh(**kw):
            n[0] += 1
            srv.reset()
            return make_install(root, "inst%d" % n[0], v1, launcher=launcher, **kw)

        # 1. no newer release
        d = fresh()
        srv.api_latest(srv.publish("v1.0.0", make_zip({}, v2b, "1.0.0")))
        before = snapshot(d)
        rc, log, ln = run_launcher(d, srv.base)
        check("1 no newer release: the installed game is launched, nothing changed", rc == 0 and ln == "game-v1" and snapshot(d) == before and "up to date" in log)

        # 1b. semantic comparison: 1.9.0 installed, 1.10.0 published => update offered (numeric, not string, comparison); decline leaves it alone
        d = fresh(version="1.9.0")
        srv.api_latest(srv.publish("v1.10.0", make_zip({}, v2b, "1.10.0")))
        before = snapshot(d)
        rc, log, ln = run_launcher(d, srv.base, answer="no")
        check("1b 1.10.0 is newer than 1.9.0 (numeric semver comparison)", "newer release available: 1.10.0" in log and snapshot(d) == before)
        d = fresh(version="1.1.0-rc.1")
        srv.api_latest(srv.publish("v1.1.0", good_zip))
        rc, log, ln = run_launcher(d, srv.base, answer="no")
        check("1c 1.1.0 is newer than 1.1.0-rc.1", "newer release available: 1.1.0" in log)
        d = fresh(version="1.1.0")
        srv.api_latest(srv.publish("v1.1.0-rc.1", make_zip({}, v2b, "1.1.0-rc.1"), pre=True))
        rc, log, ln = run_launcher(d, srv.base, answer="yes")
        check("1d a prerelease 'latest' is refused by default and never installed", ln == "game-v1" and "prerelease" in log and open(os.path.join(d, "version.txt")).read().strip() == "1.1.0")

        # 2. newer release, user says No
        d = fresh()
        srv.api_latest(srv.publish("v1.1.0", good_zip))
        before = snapshot(d)
        rc, log, ln = run_launcher(d, srv.base, answer="no", extra_args=["--host", "7788"])
        check("2 newer release + No: installed game launched with the user's own arguments, no file modified, package not even downloaded",
              ln == "game-v1 --host 7788" and snapshot(d) == before and not any("download" in h for h in srv.sc.hits) and "declined" in log)

        # 3. newer release, user says Yes
        d = fresh()
        srv.api_latest(srv.publish("v1.1.0", good_zip))
        rc, log, ln = run_launcher(d, srv.base, answer="yes")
        exe_new = open(os.path.join(d, "AnimalCrossing.exe"), "rb").read() == v2b
        check("3 newer release + Yes: package downloaded, SHA-256 verified, installed; the NEW game (v2) is launched", ln == "game-v2" and exe_new and "sha256 verified" in log and "RESULT: updated 1.0.0 -> 1.1.0" in log)
        check("3b managed files replaced (SDL2.dll, shaders/a.glsl, new shaders/b.glsl), version.txt = 1.1.0", open(os.path.join(d, "SDL2.dll"), "rb").read() == b"sdl-new" and open(os.path.join(d, "shaders", "a.glsl"), "rb").read() == b"shader-new"
              and os.path.isfile(os.path.join(d, "shaders", "b.glsl")) and open(os.path.join(d, "version.txt")).read().strip() == "1.1.0")
        check("3c the previous program files are kept in .update\\backup-1.0.0 (old exe intact), staging cleaned", open(os.path.join(d, ".update", "backup-1.0.0", "AnimalCrossing.exe"), "rb").read() == open(v1, "rb").read()
              and not os.path.exists(os.path.join(d, ".update", "stage")))
        check("9a user data (ROM, saves, servers, settings, keybindings, texture pack, shader cache, log) byte-identical after a successful update", user_ok(d))
        rc, log, ln = run_launcher(d, srv.base, answer="yes")
        check("3d the next launch is up to date (installed 1.1.0)", ln == "game-v2" and "up to date" in log)

        # 4. interrupted download
        d = fresh()
        rel = srv.publish("v1.1.0", good_zip)
        srv.api_latest(rel)
        srv.sc.routes["/%s/%s/releases/download/v1.1.0/ACGC-PC-Multiplayer-1.1.0-win64.zip" % (OWNER, REPO)] = truncated(good_zip, len(good_zip) // 2)
        before = snapshot(d)
        rc, log, ln = run_launcher(d, srv.base)
        check("4 interrupted download: old installation intact and launched, error reported, no partial package left", ln == "game-v1" and snapshot(d) == before and ("incomplete" in log or "interrupted" in log)
              and not os.path.exists(os.path.join(d, ".update", "package.zip.part")) and user_ok(d))

        # 5. checksum mismatch
        d = fresh()
        evil = make_zip(good_files, b"EVIL-EXE", "1.1.0")
        rel = srv.publish("v1.1.0", good_zip, sha=("%s  ACGC-PC-Multiplayer-1.1.0-win64.zip\n" % hashlib.sha256(evil).hexdigest()).encode())
        srv.api_latest(rel)
        before = snapshot(d)
        rc, log, ln = run_launcher(d, srv.base)
        check("5 checksum mismatch: installation refused, old game launched", ln == "game-v1" and snapshot(d) == before and "does not match its published SHA-256" in log)
        d = fresh()
        rel = srv.publish("v1.1.0", good_zip, digest="sha256:" + "0" * 64)
        srv.api_latest(rel)
        rc, log, ln = run_launcher(d, srv.base)
        check("5b the .sha256 file disagreeing with GitHub's own asset digest is refused", ln == "game-v1" and "disagrees" in log)
        d = fresh()
        srv.api_latest(srv.publish("v1.1.0", good_zip, sha=b"not a digest at all\n"))
        rc, log, ln = run_launcher(d, srv.base)
        check("5c a malformed .sha256 file is refused", ln == "game-v1" and "malformed" in log)

        # 6. malformed release metadata
        cases = {
            "invalid JSON": (200, {}, b"{not json"),
            "JSON of the wrong shape": (200, {}, b"[1,2,3]"),
            "non-semver tag": (200, {}, json.dumps({"tag_name": "../../evil", "draft": False, "prerelease": False, "assets": []}).encode()),
            "tag / prerelease flag disagree": (200, {}, json.dumps({"tag_name": "v9.0.0-rc.1", "draft": False, "prerelease": False, "assets": []}).encode()),
            "plain tag v0.1.0 flagged as a GitHub pre-release": (200, {}, json.dumps({"tag_name": "v0.1.0", "draft": False, "prerelease": True, "assets": []}).encode()),
            "release without assets": (200, {}, json.dumps({"tag_name": "v1.2.0", "draft": False, "prerelease": False, "assets": []}).encode()),
        }
        for label, resp in cases.items():
            d = fresh()
            srv.sc.routes["/repos/%s/%s/releases/latest" % (OWNER, REPO)] = resp
            before = snapshot(d)
            rc, log, ln = run_launcher(d, srv.base)
            check("6 malformed metadata (%s): refused, old game launched, nothing changed" % label, ln == "game-v1" and snapshot(d) == before and "CHECK FAILED" in log)
        d = fresh()
        srv.api_latest(srv.publish("v1.1.0", good_zip, asset_url_repo="attacker/other-repo"))
        rc, log, ln = run_launcher(d, srv.base)
        check("6b an asset URL pointing at a different repository is refused", ln == "game-v1" and "does not match the configured repository" in log and not any("attacker" in h for h in srv.sc.hits))
        d = fresh()
        srv.api_latest(srv.publish("v1.1.0", good_zip, name="evil.exe"))
        rc, log, ln = run_launcher(d, srv.base)
        check("6c an unexpected asset name / type is not installed", ln == "game-v1" and open(os.path.join(d, "version.txt")).read().strip() == "1.0.0")

        # 7. archive path traversal & other unsafe content (each with a CORRECT checksum)
        sym = zipfile.ZipInfo("shaders/link", (2020, 1, 1, 0, 0, 0))
        sym.create_system = 3
        sym.external_attr = 0o120777 << 16
        bad = {
            "../evil.txt": {"../evil.txt": b"x"},
            "shaders/../../evil.txt": {"shaders/../../evil.txt": b"x"},
            "absolute path /evil.txt": {"/evil.txt": b"x"},
            "backslash traversal": {"shaders\\..\\..\\evil.txt": b"x"},
            "drive letter": {"C:/evil.txt": b"x"},
            "overwrite settings.ini": {"settings.ini": b"hacked"},
            "write into rom/": {"rom/GAFE01_00.iso": b"hacked"},
            "write into save/": {"save/card_a/Animal Crossing.gci": b"hacked"},
            "symlink entry": {sym: b"..\\..\\"},
            "duplicate entry": {"shaders/a.glsl": b"1", "SHADERS/A.GLSL": b"2"},
        }
        for label, extra in bad.items():
            d = fresh()
            z = make_zip({}, v2b, "1.1.0", extra=extra)
            srv.api_latest(srv.publish("v1.1.0", z))
            before = snapshot(d)
            parent_before = set(os.listdir(os.path.dirname(d)))
            rc, log, ln = run_launcher(d, srv.base)
            outside = set(os.listdir(os.path.dirname(d))) - parent_before
            check("7 unsafe archive (%s): rejected, nothing written inside or outside the installation, old game launched" % label, ln == "game-v1" and snapshot(d) == before and not outside
                  and not os.path.exists(os.path.join(os.path.dirname(d), "evil.txt")) and ("refused" in log or "not a managed" in log))
        d = fresh()
        z = make_zip({}, v2b, "1.1.0")
        z = z.replace(b"AnimalCrossing.exe", b"..\\..\\evil.exe!!!!", 1) if False else z
        bomb_name = "shaders/bomb.bin"
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zz:
            zz.writestr("AnimalCrossing.exe", v2b)
            zz.writestr("version.txt", "1.1.0\n")
            zz.writestr(bomb_name, b"\0" * 1000)
        raw = bytearray(buf.getvalue())
        # corrupt the declared uncompressed size of bomb.bin in BOTH headers so the real stream is larger than declared
        idx = [i for i in range(len(raw) - 4) if raw[i:i + 4] == b"PK\x01\x02"]
        for i in idx:
            nl = int.from_bytes(raw[i + 28:i + 30], "little")
            if bytes(raw[i + 46:i + 46 + nl]) == bomb_name.encode():
                raw[i + 24:i + 28] = (10).to_bytes(4, "little")
        srv.api_latest(srv.publish("v1.1.0", bytes(raw)))
        before = snapshot(d)
        rc, log, ln = run_launcher(d, srv.base)
        check("7b an entry larger than declared (decompression bomb shape) is refused; installation untouched", ln == "game-v1" and snapshot(d) == before and ("larger than declared" in log or "CRC" in log or "corrupt" in log))
        d = fresh()
        z_nover = make_zip({}, v2b, "1.1.0").replace(b"version.txt", b"versionXtxt")
        srv.api_latest(srv.publish("v1.1.0", z_nover))
        rc, log, ln = run_launcher(d, srv.base)
        check("7c a package without version.txt / wrong layout is refused", ln == "game-v1" and open(os.path.join(d, "version.txt")).read().strip() == "1.0.0")
        d = fresh()
        srv.api_latest(srv.publish("v1.1.0", make_zip({}, v2b, "1.1.1")))
        rc, log, ln = run_launcher(d, srv.base)
        check("7d version.txt inside the package not matching the release version is refused", ln == "game-v1" and "does not match the release" in log)

        # 8. installation failure -> rollback (a late file is locked by another process)
        d = fresh()
        srv.api_latest(srv.publish("v1.1.0", good_zip))
        locked = os.path.join(d, "shaders", "a.glsl")
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = ctypes.c_void_p
        before = snapshot(d)
        h = k32.CreateFileW(locked, 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, share mode 0: nobody may rename / delete it
        try:
            rc, log, ln = run_launcher(d, srv.base)
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
        check("8 install failure after the first files were swapped: everything rolled back (exe, SDL2.dll, version.txt) and the previous game launched", ln == "game-v1" and snapshot(d) == before
              and "rolled back" in log and "RESULT: update failed" in log and user_ok(d))

        # 11. the game is still running (its exe cannot be replaced)
        d = fresh()
        srv.api_latest(srv.publish("v1.1.0", good_zip))
        running = subprocess.Popen([os.path.join(d, "AnimalCrossing.exe"), "--sleep"], cwd=d)
        time.sleep(1.0)
        before = snapshot(d)
        try:
            rc, log, ln = run_launcher(d, srv.base)
        finally:
            running.kill()
            running.wait()
        check("11 running executable cannot be replaced: update fails safely ('is the game still running?'), files intact, rolled back", snapshot(d) == before and "still running" in log and open(os.path.join(d, "version.txt")).read().strip() == "1.0.0")

        # 10. no connection / GitHub errors never read as 'up to date'
        d = fresh()
        before = snapshot(d)
        rc, log, ln = run_launcher(d, "http://127.0.0.1:1")  # nothing listens
        check("10 no connection: 'CHECK FAILED' (not 'up to date'), installed game launched unchanged", ln == "game-v1" and snapshot(d) == before and "CHECK FAILED" in log and "up to date" not in log)
        for label, resp, needle in (("rate limited", (403, {"X-RateLimit-Remaining": "0"}, b"{}"), "rate limit"), ("server error", (500, {}, b"oops"), "HTTP status 500"), ("403 without rate limit", (403, {}, b"{}"), "refused the request")):
            d = fresh()
            srv.sc.routes["/repos/%s/%s/releases/latest" % (OWNER, REPO)] = resp
            rc, log, ln = run_launcher(d, srv.base)
            check("10b GitHub %s: reported as a failed check, game launched" % label, ln == "game-v1" and "CHECK FAILED" in log and needle in log and "up to date" not in log)
        d = fresh()
        rc, log, ln = run_launcher(d, srv.base)  # no route at all -> 404
        check("10c no release published yet (404): clear message, game launched, not an update", ln == "game-v1" and "no official release has been published yet" in log)

        # 12. development checkout / unversioned install: never auto-updated
        d = fresh()
        os.makedirs(os.path.join(os.path.dirname(d), ".git"), exist_ok=True)
        srv.api_latest(srv.publish("v1.1.0", good_zip))
        before = snapshot(d)
        rc, log, ln = run_launcher(d, srv.base)
        check("12 launcher inside a Git working tree: no check, no update, game launched", ln == "game-v1" and snapshot(d) == before and "development checkouts" in log and not srv.sc.hits)
        shutil.rmtree(os.path.join(os.path.dirname(d), ".git"))
        d = fresh(with_version=False)
        srv.api_latest(srv.publish("v1.1.0", good_zip))
        before = snapshot(d)
        rc, log, ln = run_launcher(d, srv.base)
        check("12b an installation without version.txt (a developer's bin folder) is never updated", ln == "game-v1" and snapshot(d) == before and "not a packaged installation" in log and not srv.sc.hits)

        # settings: disable switch; test switches ignored without AC_UPDATER_TEST
        d = fresh()
        open(os.path.join(d, "updater.ini"), "w").write("check_updates=0\n")
        srv.api_latest(srv.publish("v1.1.0", good_zip))
        rc, log, ln = run_launcher(d, srv.base)
        check("13 updater.ini check_updates=0 disables the check (no request made)", ln == "game-v1" and not srv.sc.hits and "disabled" in log)
        d = fresh()
        srv.api_latest(srv.publish("v1.1.0", good_zip))
        p = subprocess.run([os.path.join(d, "ACLauncher.exe"), "--updater-test-api", srv.base, "--updater-answer", "yes", "--updater-no-ui", "--updater-skip", "--x"], cwd=d, timeout=60, env={k: v for k, v in os.environ.items() if k != "AC_UPDATER_TEST"})
        time.sleep(1.0)
        check("13b --updater-skip launches directly; launcher switches are not passed to the game", open(os.path.join(d, "launched.txt")).read() == "game-v1 --x" if os.path.exists(os.path.join(d, "launched.txt")) else False)
        d = fresh()
        open(os.path.join(d, "updater.ini"), "w").write("allow_prerelease=1\n")
        srv.api_list([srv.publish("v1.2.0-rc.1", make_zip({}, v2b, "1.2.0-rc.1"), pre=True), srv.publish("v1.1.0", good_zip)])
        rc, log, ln = run_launcher(d, srv.base, answer="no")
        check("14 allow_prerelease=1 picks the highest release incl. prereleases (1.2.0-rc.1), still asks first", "newer release available: 1.2.0-rc.1" in log and ln == "game-v1")

        # 14b. Beta 0.1.0 channel rules: a stable-flagged v0.1.0 is what the default setting installs; a pre-release-flagged plain tag can never be installed
        d = fresh(version="0.0.9")
        zb = make_zip({}, v2b, "0.1.0")
        srv.api_latest(srv.publish("v0.1.0", zb))
        srv.api_list([srv.publish("v0.1.0-beta.1", make_zip({}, v2b, "0.1.0-beta.1"), pre=True)])  # must NOT be consulted in the default mode
        rc, log, ln = run_launcher(d, srv.base, answer="no")
        check("14b default setting: the stable v0.1.0 (non-pre-release) is offered for 0.0.9 via /releases/latest; the release list is never queried",
              "newer release available: 0.1.0" in log and not any(h.split("?")[0].endswith("/releases") for h in srv.sc.hits))
        d = fresh(version="0.0.9")
        srv.api_latest(srv.publish("v0.1.0", make_zip({}, v2b, "0.1.0"), pre=True))  # what ticking "pre-release" on a plain v0.1.0 would publish
        rc, log, ln = run_launcher(d, srv.base, answer="yes")
        check("14c a plain v0.1.0 tag published with GitHub's pre-release flag is refused (tag / flag disagree): nothing installed, game launched", ln == "game-v1" and "disagree" in log
              and open(os.path.join(d, "version.txt")).read().strip() == "0.0.9")
        d = fresh(version="0.0.9")  # only pre-releases exist: /releases/latest answers 404
        srv.api_list([srv.publish("v0.1.0-beta.1", make_zip({}, v2b, "0.1.0-beta.1"), pre=True)])
        rc, log, ln = run_launcher(d, srv.base, answer="yes")
        check("14d only a pre-release exists and the default setting is on: 'no official release has been published yet', nothing installed", ln == "game-v1" and "no official release" in log and open(os.path.join(d, "version.txt")).read().strip() == "0.0.9")
        d = fresh(version="0.0.9")
        open(os.path.join(d, "updater.ini"), "w").write("allow_prerelease=1\n")
        draft = srv.publish("v0.3.0", make_zip({}, v2b, "0.3.0"))
        draft["draft"] = True
        badflag = srv.publish("v0.2.0", make_zip({}, v2b, "0.2.0"), pre=True)
        srv.api_list([draft, badflag, srv.publish("v0.1.0-beta.1", make_zip({}, v2b, "0.1.0-beta.1"), pre=True), srv.publish("v0.1.0", zb)])
        rc, log, ln = run_launcher(d, srv.base, answer="no")
        check("14e allow_prerelease=1: drafts and tag/flag mismatches are skipped; the stable 0.1.0 beats 0.1.0-beta.1", "newer release available: 0.1.0" in log and "0.3.0" not in log and "0.2.0" not in log)
        d = fresh(version="0.1.0-beta.1")
        open(os.path.join(d, "updater.ini"), "w").write("allow_prerelease=1\n")
        srv.api_list([srv.publish("v0.1.0", zb)])
        rc, log, ln = run_launcher(d, srv.base, answer="no")
        check("14f a 0.1.0-beta.1 installation is offered the final 0.1.0", "newer release available: 0.1.0" in log)

        # 15. launcher without a game exe
        d = fresh()
        os.remove(os.path.join(d, "AnimalCrossing.exe"))
        srv.api_latest(srv.publish("v1.0.0", good_zip))
        rc, log, ln = run_launcher(d, srv.base, wait_game=False)
        check("15 missing AnimalCrossing.exe: launcher reports it and exits with code 2 (no crash)", rc == 2 and "cannot start the game" in log)
    finally:
        srv.httpd.shutdown()
        if all(results) and len(results) > 0:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("(fixtures kept for diagnosis: %s)" % root)
    print("-" * 60)
    print("%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
