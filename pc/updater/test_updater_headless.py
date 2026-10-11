#!/usr/bin/env python3
r"""test_updater_headless.py - the launcher's two HEADLESS modes, used by the dedicated-server starter (pc/server/server_starter.ps1) for its startup update check:
  --updater-check-only   same GitHub check as the normal launcher, reports AVAILABLE / UPTODATE / CHECKFAILED / UNSUPPORTED / DISABLED, changes nothing, starts nothing, shows nothing;
  --updater-install-now  (the caller already has the user's "yes") the SAME do_update() as the normal flow: download, SHA-256, package validation, staging, backup, swap, rollback -> UPDATED / UPDATEFAILED.
Both write STATUS= / INSTALLED= / LATEST= / MESSAGE= to the --updater-result file and never launch the game.
TIER: the real ACLauncher.exe against the local scripted GitHub-like server of test_updater.py (no real GitHub), throw-away installations in %TEMP%, fake game programs. The normal flow keeps its own
suite (test_updater.py).  Usage: python test_updater_headless.py --launcher <path>\ACLauncher.exe"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_updater as TU  # noqa: E402

def check(desc, cond, extra=None):
    TU.check(desc + ((" :: " + str(extra)[:300]) if (extra is not None and not cond) else ""), cond)


def snap(d):
    return TU.snapshot(d, skip=("updater.log", "launched.txt", "result.txt"))


def run_mode(inst, base, mode, extra_env=None, timeout=120):
    res = os.path.join(inst, "result.txt")
    if os.path.exists(res):
        os.remove(res)
    lf = os.path.join(inst, "launched.txt")
    if os.path.exists(lf):
        os.remove(lf)
    env = dict(os.environ, AC_UPDATER_TEST="1")
    env.update(extra_env or {})
    p = subprocess.run([os.path.join(inst, "ACLauncher.exe"), mode, "--updater-result", res, "--updater-test-api", base], cwd=inst, env=env, timeout=timeout)
    time.sleep(0.3)
    kv = {}
    if os.path.exists(res):
        for ln in open(res, encoding="utf-8").read().splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                kv[k] = v
    return p.returncode, kv, os.path.exists(lf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--launcher", default=os.path.join(HERE, "out", "ACLauncher.exe"))
    args = ap.parse_args()
    launcher = os.path.abspath(args.launcher)
    root = tempfile.mkdtemp(prefix="achl_")
    srv = TU.Server()
    try:
        v1 = TU.build_fake(root, "game-v1")
        v2 = TU.build_fake(root, "game-v2")
        v2b = open(v2, "rb").read()
        files = {"SDL2.dll": b"sdl-new", "shaders/a.glsl": b"shader-new", "shaders/b.glsl": b"shader-b"}
        good = TU.make_zip(files, v2b, "1.1.0")

        def fresh(name, version="1.0.0"):
            return TU.make_install(root, name, v1, version=version, launcher=launcher)

        # ---- check-only
        inst = fresh("up")
        srv.reset()
        srv.api_latest(srv.publish("v1.0.0", TU.make_zip(files, v2b, "1.0.0")))
        before = snap(inst)
        rc, kv, launched = run_mode(inst, srv.base, "--updater-check-only")
        check("H1 same version published: UPTODATE (exit 0), installed 1.0.0 / latest 1.0.0, nothing changed, the game was not launched: %s" % kv,
              rc == 0 and kv.get("STATUS") == "UPTODATE" and kv.get("INSTALLED") == "1.0.0" and kv.get("LATEST") == "1.0.0" and snap(inst) == before and not launched)

        srv.reset()
        srv.api_latest(srv.publish("v1.1.0", good))
        rc, kv, launched = run_mode(inst, srv.base, "--updater-check-only")
        check("H2 a newer release: AVAILABLE with both version numbers, exit 0, no download happened, nothing changed, no game launched: %s" % kv,
              rc == 0 and kv.get("STATUS") == "AVAILABLE" and kv.get("INSTALLED") == "1.0.0" and kv.get("LATEST") == "1.1.0" and not any(".zip" in h for h in srv.sc.hits) and snap(inst) == before and not launched)

        for installed, published, want in (("1.9.0", "1.10.0", "AVAILABLE"), ("1.10.0", "1.9.0", "UPTODATE"), ("1.0.9", "1.0.10", "AVAILABLE"), ("2.0.0", "10.0.0", "AVAILABLE"), ("1.2.0", "1.2.0", "UPTODATE")):
            d = fresh("cmp_%s_%s" % (installed, published), version=installed)
            srv.reset()
            srv.api_latest(srv.publish("v" + published, TU.make_zip(files, v2b, published)))
            rc, kv, _ = run_mode(d, srv.base, "--updater-check-only")
            check("H3 version comparison is numeric, not text: installed %s vs published %s -> %s" % (installed, published, want), kv.get("STATUS") == want)

        d = fresh("pre")
        srv.reset()
        srv.api_latest(srv.publish("v1.1.0-beta", good, pre=True))
        rc, kv, _ = run_mode(d, srv.base, "--updater-check-only")
        check("H4 the latest release is a prerelease: refused (CHECKFAILED, exit 3), the launcher's stable-only policy", rc == 3 and kv.get("STATUS") == "CHECKFAILED" and "prerelease" in kv.get("MESSAGE", ""), kv)

        d = fresh("limit")
        srv.reset()
        srv.sc.routes["/repos/%s/%s/releases/latest" % (TU.OWNER, TU.REPO)] = (403, {"Content-Type": "application/json", "X-RateLimit-Remaining": "0"}, b'{"message":"API rate limit exceeded"}')
        rc, kv, _ = run_mode(d, srv.base, "--updater-check-only")
        check("H5 GitHub rate limit (403): CHECKFAILED, exit 3, a readable reason, nothing changed: %s" % kv.get("MESSAGE"), rc == 3 and kv.get("STATUS") == "CHECKFAILED" and kv.get("MESSAGE"))
        srv.reset()
        srv.sc.routes["/repos/%s/%s/releases/latest" % (TU.OWNER, TU.REPO)] = (200, {"Content-Type": "application/json"}, b"{ this is not json")
        rc, kv, _ = run_mode(d, srv.base, "--updater-check-only")
        check("H6 malformed release metadata: CHECKFAILED, exit 3", rc == 3 and kv.get("STATUS") == "CHECKFAILED", kv)
        srv.reset()
        srv.sc.routes["/repos/%s/%s/releases/latest" % (TU.OWNER, TU.REPO)] = (200, {"Content-Type": "application/json"}, b'{"tag_name":"v9.9.9","draft":false,"prerelease":false,"assets":[]}')
        rc, kv, _ = run_mode(d, srv.base, "--updater-check-only")
        check("H7 a release without the package assets: CHECKFAILED (no usable package), exit 3", rc == 3 and kv.get("STATUS") == "CHECKFAILED", kv)
        srv.reset()
        rc, kv, _ = run_mode(d, srv.base, "--updater-check-only")
        check("H8 no release published (404): reported as up to date, exit 0 (nothing newer exists)", rc == 0 and kv.get("STATUS") == "UPTODATE", kv)
        t0 = time.time()
        rc, kv, _ = run_mode(d, "http://127.0.0.1:9", "--updater-check-only")
        check("H9 GitHub unreachable (connection refused): CHECKFAILED quickly (%.1f s), exit 3, a readable reason: %s" % (time.time() - t0, kv.get("MESSAGE")), rc == 3 and kv.get("STATUS") == "CHECKFAILED" and time.time() - t0 < 60)
        d2 = fresh("nover")
        os.remove(os.path.join(d2, "version.txt"))
        srv.reset()
        srv.api_latest(srv.publish("v1.1.0", good))
        rc, kv, _ = run_mode(d2, srv.base, "--updater-check-only")
        check("H10 no version.txt (not a packaged installation): UNSUPPORTED, exit 5, nothing requested from GitHub", rc == 5 and kv.get("STATUS") == "UNSUPPORTED" and not srv.sc.hits, kv)
        rc, kv, _ = run_mode(d, srv.base, "--updater-check-only", extra_env={"AC_UPDATER_DISABLE": "1"})
        check("H11 AC_UPDATER_DISABLE=1: DISABLED, exit 5", rc == 5 and kv.get("STATUS") == "DISABLED", kv)

        # ---- the launcher's own policy file still decides the channel: updater.ini allow_prerelease=1 offers prereleases, the default never does (H4)
        d = fresh("prechannel")
        open(os.path.join(d, "updater.ini"), "w").write("allow_prerelease=1\ncheck_updates=0\n")
        srv.reset()
        srv.api_list([srv.publish("v1.2.0-rc.1", TU.make_zip(files, v2b, "1.2.0-rc.1"), pre=True)])
        rc, kv, _ = run_mode(d, srv.base, "--updater-check-only")
        check("H26 updater.ini allow_prerelease=1 is honoured (AVAILABLE 1.2.0-rc.1) and its check_updates=0 does not apply to the headless check (the caller decided to ask): %s" % kv,
              rc == 0 and kv.get("STATUS") == "AVAILABLE" and kv.get("LATEST") == "1.2.0-rc.1")

        # ---- install-now
        inst = fresh("inst")
        srv.reset()
        srv.api_latest(srv.publish("v1.1.0", good))
        rc, kv, launched = run_mode(inst, srv.base, "--updater-install-now")
        got = {n: open(os.path.join(inst, *n.split("/")), "rb").read() for n in ("SDL2.dll", "shaders/a.glsl", "shaders/b.glsl", "AnimalCrossing.exe", "version.txt")}
        check("H12 install-now with a good package: UPDATED (exit 0), every managed file replaced, version.txt = 1.1.0, backup kept: %s" % kv,
              rc == 0 and kv.get("STATUS") == "UPDATED" and got["SDL2.dll"] == b"sdl-new" and got["shaders/a.glsl"] == b"shader-new" and got["AnimalCrossing.exe"] == v2b
              and got["version.txt"].decode().strip() == "1.1.0" and os.path.isdir(os.path.join(inst, ".update")))
        check("H13 the update did not launch the game, and user data (rom, saves, servers incl. town, settings, keybindings, logs) is byte-identical", not launched and TU.user_ok(inst))
        check("H13b the update asked for the UPDATE package and its checksum by name (ACGC-PC-Multiplayer-1.1.0-win64.zip + .sha256), never the first-install bundle", any(h.endswith("ACGC-PC-Multiplayer-1.1.0-win64.zip") for h in srv.sc.hits)
              and any(h.endswith("ACGC-PC-Multiplayer-1.1.0-win64.zip.sha256") for h in srv.sc.hits) and not any("first-install" in h for h in srv.sc.hits), srv.sc.hits)
        rc, kv, _ = run_mode(inst, srv.base, "--updater-install-now")
        check("H14 running install-now again when already current does nothing (UPTODATE)", kv.get("STATUS") == "UPTODATE")

        def failing(name, zbytes, sha, desc, **kw):
            d = fresh(name)
            srv.reset()
            srv.api_latest(srv.publish("v1.1.0", zbytes, sha=sha, **kw))
            before = snap(d)
            rc, kv, launched = run_mode(d, srv.base, "--updater-install-now")
            check("%s: UPDATEFAILED (exit 4), the installation is byte-identical to before, user data intact: %s" % (desc, kv.get("MESSAGE", "")[:90]),
                  rc == 4 and kv.get("STATUS") == "UPDATEFAILED" and snap(d) == before and TU.user_ok(d) and not launched, kv)

        failing("badsha", good, ("%s  ACGC-PC-Multiplayer-1.1.0-win64.zip\n" % ("0" * 64)).encode(), "H15 a checksum that does not match the package")
        failing("shajunk", good, b"not a checksum file\n", "H16 a malformed checksum file")
        failing("shaname", good, ("%s  other-name.zip\n" % hashlib.sha256(good).hexdigest()).encode(), "H17 a checksum that names a different file")
        d = fresh("nosha")
        srv.reset()
        rel = srv.publish("v1.1.0", good)
        rel["assets"] = [a for a in rel["assets"] if not a["name"].endswith(".sha256")]
        srv.api_latest(rel)
        before = snap(d)
        rc, kv, _ = run_mode(d, srv.base, "--updater-install-now")
        check("H18 the checksum asset is missing from the release: nothing is installed, the installation is unchanged: %s" % kv.get("STATUS"), kv.get("STATUS") in ("CHECKFAILED", "UPDATEFAILED") and rc in (3, 4) and snap(d) == before)
        d = fresh("nozip")
        srv.reset()
        rel = srv.publish("v1.1.0", good)
        rel["assets"] = [a for a in rel["assets"] if a["name"].endswith(".sha256")]
        srv.api_latest(rel)
        before = snap(d)
        rc, kv, _ = run_mode(d, srv.base, "--updater-install-now")
        check("H19 the package asset is missing from the release: nothing is installed, the installation is unchanged: %s" % kv.get("STATUS"), kv.get("STATUS") in ("CHECKFAILED", "UPDATEFAILED") and rc in (3, 4) and snap(d) == before)
        d = fresh("trunc")
        srv.reset()
        rel = srv.publish("v1.1.0", good)
        srv.sc.routes["/%s/%s/releases/download/v1.1.0/ACGC-PC-Multiplayer-1.1.0-win64.zip" % (TU.OWNER, TU.REPO)] = TU.truncated(good, len(good) // 2)
        srv.api_latest(rel)
        before = snap(d)
        rc, kv, launched = run_mode(d, srv.base, "--updater-install-now")
        check("H20 an interrupted download (half the bytes): UPDATEFAILED, the installation is unchanged and usable: %s" % kv.get("MESSAGE", "")[:80], rc == 4 and kv.get("STATUS") == "UPDATEFAILED" and snap(d) == before and TU.user_ok(d))
        bad_zip = TU.make_zip(files, v2b, "1.1.0", extra={"../evil.txt": b"x"})
        failing("evil", bad_zip, None, "H21 a package with an unsafe path (../evil.txt)")
        failing("wrongver", TU.make_zip(files, v2b, "1.0.5"), None, "H22 a package whose own version.txt disagrees with the release")

        # ---- only managed program files may be in a package: the launcher, the starter files and the user's config are refused as a whole
        for evil in ("ACLauncher.exe", "Start Server.bat", "server_starter.ps1", "server_config.ini", "settings.ini", "servers/default/town/card_a/x.gci", "rom/disc.iso", "SERVER_SETUP.txt"):
            failing("ev_" + evil.replace("/", "_").replace(" ", "_"), TU.make_zip(files, v2b, "1.1.0", extra={evil: b"planted"}), None, "H24 a package that also carries %s" % evil)
        # ---- user files of the server (config, records, work jobs, guests, logs, town) survive a successful update byte for byte
        d = fresh("userdata")
        srv.reset()
        srv.api_latest(srv.publish("v1.1.0", good))
        rc, kv, _ = run_mode(d, srv.base, "--updater-install-now")
        check("H25 after a successful update server_config.ini (254 / 254), members / records / work_jobs / server.log, the town, settings, keybindings, rom, saves and logs are byte-identical and ACLauncher.exe is untouched",
              kv.get("STATUS") == "UPDATED" and TU.user_ok(d) and open(os.path.join(d, "ACLauncher.exe"), "rb").read() == open(launcher, "rb").read(), kv)

        # ---- a running game is never replaced and never closed
        d = fresh("running")
        srv.reset()
        srv.api_latest(srv.publish("v1.1.0", good))
        running = subprocess.Popen([os.path.join(d, "AnimalCrossing.exe"), "--sleep"], cwd=d)
        time.sleep(1.0)
        before = snap(d)
        rc, kv, launched = run_mode(d, srv.base, "--updater-install-now")
        alive = running.poll() is None
        check("H23 the game of this folder is running: UPDATEFAILED ('still running'), nothing replaced, the game process is NOT closed: %s" % kv.get("MESSAGE", "")[:80],
              rc == 4 and kv.get("STATUS") == "UPDATEFAILED" and "running" in kv.get("MESSAGE", "") and snap(d) == before and alive)
        running.kill()
        running.wait()
    finally:
        srv.httpd.shutdown()
        shutil.rmtree(root, ignore_errors=True)
    print("-" * 60)
    print("%d/%d checks passed" % (sum(TU.results), len(TU.results)))
    return 0 if all(TU.results) else 1


if __name__ == "__main__":
    sys.exit(main())
