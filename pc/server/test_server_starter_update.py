#!/usr/bin/env python3
r"""test_server_starter_update.py - the dedicated-server starter's STARTUP UPDATE CHECK (server_starter.ps1, check_updates = 1).

TIER: the real starter script + the real (new) ACLauncher.exe in its headless modes (--updater-check-only / --updater-install-now) against the local scripted GitHub-like server of
pc/updater/test_updater.py, in throw-away installations under %TEMP%. The "game" is a tiny stub program (compiled with gcc; the test is reported as SKIPPED without it) that records how it was started.
No real GitHub, no real game, no gameplay. The interactive prompt is exercised twice for real: the starter runs in its own console window and the answers are written into that console's input buffer
through the console API (ded_con_helper.py): no OS keyboard, mouse or focus.
Usage: python test_server_starter_update.py --launcher <new ACLauncher.exe>"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "pc", "updater"))
sys.path.insert(0, os.path.join(REPO, "pc", "tools", "net_spike"))
import test_updater as TU  # noqa: E402

PS1 = os.path.join(HERE, "server_starter.ps1")
FILES = ["server_starter.ps1", "Start Server.bat", "server_config.ini", "SERVER_SETUP.txt"]
results = TU.results


def check(desc, cond, extra=None):
    TU.check(desc + ((" :: " + str(extra)[:400]) if (extra is not None and not cond) else ""), cond)


STUB_C = r'''#include <windows.h>
#include <stdio.h>
int main(int argc, char** argv) {
    FILE* f = fopen("game_started.txt", "w");
    int i;
    fprintf(f, "%s", MARK);
    for (i = 1; i < argc; i++) fprintf(f, " %s", argv[i]);
    fclose(f);
    Sleep(90000);
    return 0;
}
'''


def build_stub(tmp, mark, gcc):
    src = os.path.join(tmp, "stub_%s.c" % mark)
    open(src, "w").write(STUB_C)
    exe = os.path.join(tmp, "stub_%s.exe" % mark)
    env = dict(os.environ, PATH=os.path.dirname(gcc) + os.pathsep + os.environ.get("PATH", ""))
    subprocess.run([gcc, "-O1", '-DMARK="%s"' % mark, "-o", exe, src], check=True, env=env)
    return exe


def write_cfg(inst, **kv):
    base = {"port": "7461", "max_peers": "254", "max_guests": "254", "check_updates": "1"}
    base.update(kv)
    open(os.path.join(inst, "server_config.ini"), "w", newline="\r\n").write("\n".join("%s = %s" % (k, v) for k, v in base.items()) + "\n")


def make_inst(root, name, stub, launcher, version="1.0.0", with_launcher=True, **cfg):
    d = TU.make_install(root, name, stub, version=version, launcher=launcher)
    if not with_launcher:
        pass
    for f in FILES:
        if f != "server_config.ini":
            shutil.copy(os.path.join(HERE, f), d)
    write_cfg(d, **cfg)
    return d


def run_starter(inst, base, *flags, answer=None, env_extra=None, timeout=240):
    env = dict(os.environ, AC_UPDATER_TEST="1", AC_UPDATER_TEST_API=base)
    env.update(env_extra or {})
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(inst, "server_starter.ps1"), "-Root", inst] + list(flags)
    if answer is not None:
        cmd += ["-UpdateAnswer", answer]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout + r.stderr


def stop_stubs(inst):
    k = os.path.join(inst, "kill_stubs.ps1")
    open(k, "w").write("\n".join(["param([string]$P)", "Get-CimInstance Win32_Process -Filter \"Name='AnimalCrossing.exe'\" | Where-Object { $_.ExecutablePath -ieq $P } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }", ""]))
    subprocess.run(["powershell.exe", "-NoProfile", "-File", k, "-P", os.path.normpath(os.path.join(inst, "AnimalCrossing.exe"))], capture_output=True, timeout=60)
    time.sleep(0.8)


def started(inst):
    p = os.path.join(inst, "game_started.txt")
    return open(p).read().split() if os.path.exists(p) else None


def prog_state(inst):
    """the files an update would replace, plus the user's own: hashes (settings.ini is rewritten by every start on purpose and is checked separately)"""
    out = TU.snapshot(inst, skip=("updater.log", "game_started.txt", "result.txt", "kill_stubs.ps1", "settings.ini", "screen.json"))
    return {k: v for k, v in out.items() if not k.startswith("ac-update")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--launcher", required=True)
    args = ap.parse_args()
    launcher = os.path.abspath(args.launcher)
    gcc = shutil.which("gcc") or next((c for c in ("C:/msys64/ucrt64/bin/gcc.exe",) if os.path.exists(c)), None)
    if gcc is None:
        print("SKIP - no gcc for the stub game")
        return 0
    root = tempfile.mkdtemp(prefix="acsu ")
    srv = TU.Server()
    try:
        s1 = build_stub(root, "v1", gcc)
        s2 = build_stub(root, "v2", gcc)
        v2b = open(s2, "rb").read()
        files = {"SDL2.dll": b"sdl-new", "shaders/a.glsl": b"shader-new", "shaders/b.glsl": b"shader-b"}
        good = TU.make_zip(files, v2b, "1.1.0")

        # ---- the pure helpers (dot-sourced): the yes / no reader
        def reader_case(answers):
            lit = ",".join("$null" if a is None else "'" + a.replace("'", "''") + "'" for a in answers)
            script = ". '%s' -Root '%s'; $script:a = @(%s); $script:i = 0; Read-YesNo { if ($script:i -ge $script:a.Count) { $null } else { $script:a[$script:i++] } }; \"used=$($script:i)\"" % (PS1, HERE.replace("'", "''"), lit)
            r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
            return lines[-2] if len(lines) >= 2 else None, lines[-1] if lines else None
        for ans, want in ((["y"], "yes"), (["YES"], "yes"), ([" Yes "], "yes"), (["n"], "no"), (["No"], "no"), ([None], "no"), (["maybe", "y"], "yes"), (["", "x", "n"], "no"), (["yes please", "ok", "sure"], "no"),
                          (["ja"], "no"), (["1", "2", "3", "y"], "no"), ([""], "no")):
            got, used = reader_case(ans)
            check("Y1 answer sequence %s -> %s (an ambiguous answer is never consent): got %s" % (ans, want, got), got == want, (got, used))

        def fresh(name, **kw):
            return make_inst(root, name, s1, launcher, **kw)

        # ---- no newer release
        d = fresh("same")
        srv.reset()
        srv.api_latest(srv.publish("v1.0.0", TU.make_zip(files, v2b, "1.0.0")))
        cfg_bytes = open(os.path.join(d, "server_config.ini"), "rb").read()
        try:
            rc, out = run_starter(d, srv.base)
            time.sleep(1.0)
            st = started(d)
            check("U1 no newer release: no prompt, the server starts (exit 0) with the 254 / 254 limits, the check said it is up to date", rc == 0 and "is up to date" in out and "newer version" not in out and st is not None and st[st.index("--max-peers") + 1] == "254" and st[st.index("--max-guests") + 1] == "254", (rc, "is up to date" in out, "newer version" in out, st))
            log = open(os.path.join(d, "updater.log"), errors="replace").read()
            check("U1b the launcher itself did not check again (its log says the check is disabled: --updater-skip was sent; the game never receives that switch) and GitHub was asked exactly once; server_config.ini unchanged",
                  "update check disabled" in log and len([h for h in srv.sc.hits if h.endswith("/releases/latest")]) == 1 and open(os.path.join(d, "server_config.ini"), "rb").read() == cfg_bytes, (srv.sc.hits, log[-300:]))
        finally:
            stop_stubs(d)

        # ---- newer release, answered on the command line
        for ans, label in (("yes", "yes"), ("no", "no")):
            d = fresh("newer_" + label)
            srv.reset()
            srv.api_latest(srv.publish("v1.1.0", good))
            before = prog_state(d)
            cfg_bytes = open(os.path.join(d, "server_config.ini"), "rb").read()
            try:
                rc, out = run_starter(d, srv.base, answer=ans)
                time.sleep(1.0)
                st = started(d)
                after = prog_state(d)
                changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
                if ans == "yes":
                    check("U2 newer release + yes: the prompt text shows installed 1.0.0 and available 1.1.0, the launcher's installer ran, the update was verified: %s" % [l for l in out.splitlines() if "ersion" in l][:3],
                          rc == 0 and "A newer version of Animal Crossing PC Multiplayer is available." in out and "Installed version: 1.0.0" in out and "Available version: 1.1.0" in out
                          and "Would you like to download and install the update now? [Y/N]" in out and "Updated 1.0.0 -> 1.1.0 (verified)" in out, out[-700:])
                    check("U3 the UPDATED server is the one that started: new AnimalCrossing.exe launched (mark v2) with the 254 / 254 arguments", st is not None and st[0] == "v2" and st[st.index("--max-peers") + 1] == "254" and st[st.index("--max-guests") + 1] == "254", st)
                    check("U3b only managed program files changed (AnimalCrossing.exe, SDL2.dll, shaders, version.txt); server_config.ini (254/254), rom, saves, servers, keybindings, logs, texture pack are byte-identical: %s" % changed,
                          set(changed) <= {"AnimalCrossing.exe", "SDL2.dll", "shaders/a.glsl", "shaders/b.glsl", "shaders/b.glsl", "version.txt", "ACLauncher.exe"} and "ACLauncher.exe" not in changed and TU.user_ok(d) is not None
                          and open(os.path.join(d, "server_config.ini"), "rb").read() == cfg_bytes and open(os.path.join(d, "version.txt")).read().strip() == "1.1.0")
                    check("U3c user data kept (ROM, saves, the servers\\default town, settings, keybindings, texture pack, log)", all(os.path.isfile(os.path.join(d, *k.split("/"))) and open(os.path.join(d, *k.split("/")), "rb").read() == v for k, v in TU.USER_FILES.items() if k not in ("settings.ini", "server_config.ini")))
                else:
                    check("U4 newer release + no: nothing installed (every file identical, version.txt still 1.0.0), the INSTALLED server (mark v1) starts", rc == 0 and "Not updating" in out and not changed and st is not None and st[0] == "v1", (changed, out[-400:]))
            finally:
                stop_stubs(d)

        # ---- newer release, nothing to ask on (redirected input): never installs
        d = fresh("noninteractive")
        srv.reset()
        srv.api_latest(srv.publish("v1.1.0", good))
        before = prog_state(d)
        t0 = time.time()
        try:
            rc, out = run_starter(d, srv.base)
            time.sleep(1.0)
            check("U5 redirected input and no -UpdateAnswer: it reports the update, installs NOTHING, does not wait for input (%.0f s) and starts the installed server" % (time.time() - t0),
                  rc == 0 and "Not installing: there is no console to ask on" in out and prog_state(d) == before and (started(d) or [""])[0] == "v1" and srv.sc.hits and not any(".zip" in h for h in srv.sc.hits), out[-500:])
        finally:
            stop_stubs(d)

        # ---- GitHub problems: clear message, the installed version starts (nothing to ask on => continues)
        def problem(name, setup, desc, expect_text):
            d = fresh(name)
            srv.reset()
            setup(d)
            before = prog_state(d)
            try:
                rc, out = run_starter(d, srv.base if name != "unreach" else "http://127.0.0.1:9", answer="yes")
                time.sleep(1.0)
                check("%s: the problem is reported ('%s'), nothing installed, the installed server starts" % (desc, expect_text), rc == 0 and "The update check did not work" in out and expect_text in out.lower() and prog_state(d) == before and (started(d) or [""])[0] == "v1", out[-500:])
            finally:
                stop_stubs(d)
        problem("unreach", lambda d: None, "G1 GitHub unreachable (connection refused)", "")
        problem("limit", lambda d: srv.sc.routes.__setitem__("/repos/%s/%s/releases/latest" % (TU.OWNER, TU.REPO), (403, {"Content-Type": "application/json"}, b'{"message":"API rate limit exceeded"}')), "G2 rate limit (403)", "")
        problem("badjson", lambda d: srv.sc.routes.__setitem__("/repos/%s/%s/releases/latest" % (TU.OWNER, TU.REPO), (200, {"Content-Type": "application/json"}, b"<html>oops")), "G3 malformed release metadata", "json")
        problem("prerelease", lambda d: srv.api_latest(srv.publish("v1.1.0-beta", good, pre=True)), "G4 the newest release is a prerelease (the stable-only policy of the launcher)", "prerelease")

        def noasset(d):
            rel = srv.publish("v1.1.0", good)
            rel["assets"] = [a for a in rel["assets"] if not a["name"].endswith(".sha256")]
            srv.api_latest(rel)
        problem("nosha", noasset, "G5 the checksum asset is missing", "")

        # ---- the update itself fails: installed version kept and started
        def failing(name, zbytes, sha, desc, route_override=None):
            d = fresh(name)
            srv.reset()
            srv.api_latest(srv.publish("v1.1.0", zbytes, sha=sha))
            if route_override:
                srv.sc.routes[route_override[0]] = route_override[1]
            before = prog_state(d)
            try:
                rc, out = run_starter(d, srv.base, answer="yes")
                time.sleep(1.0)
                check("%s: 'The update was not installed', every file identical to before, the installed server (v1) starts" % desc, rc == 0 and "The update was not installed" in out and prog_state(d) == before and (started(d) or [""])[0] == "v1", out[-500:])
            finally:
                stop_stubs(d)
        failing("badsha", good, ("%s  ACGC-PC-Multiplayer-1.1.0-win64.zip\n" % ("0" * 64)).encode(), "F1 a checksum that does not match")
        failing("trunc", good, None, "F2 an interrupted download",
                ("/%s/%s/releases/download/v1.1.0/ACGC-PC-Multiplayer-1.1.0-win64.zip" % (TU.OWNER, TU.REPO), TU.truncated(good, len(good) // 2)))

        # ---- a game client of the same folder is running: not touched, update postponed with an explanation
        d = fresh("client")
        srv.reset()
        srv.api_latest(srv.publish("v1.1.0", good))
        before = prog_state(d)
        client = subprocess.Popen([os.path.join(d, "AnimalCrossing.exe"), "--connect", "127.0.0.1:7461"], cwd=d)
        time.sleep(1.0)
        try:
            rc, out = run_starter(d, srv.base, answer="yes")
            time.sleep(1.0)
            st = started(d)
            check("C1 your game of this folder is running: the update is NOT installed (explained), your game is untouched (still alive), the server still starts", rc == 0 and "cannot be installed while the game of this folder is running" in out and client.poll() is None and prog_state(d) == before, out[-500:])
            rc2, out2 = run_starter(d, srv.base, answer="yes")
            check("C2 a second dedicated server is still refused (exit 2) - before any update check (no request to GitHub: %d)" % len([h for h in srv.sc.hits if "releases" in h]), rc2 == 2 and "already running" in out2 and client.poll() is None, out2[-400:])
        finally:
            stop_stubs(d)
            client.kill()
            client.wait()

        # ---- older launcher / switches
        d = fresh("oldlauncher")
        shutil.copy(s1, os.path.join(d, "ACLauncher.exe"))  # a stand-in without the headless capability marker
        srv.reset()
        srv.api_latest(srv.publish("v1.1.0", good))
        try:
            rc, out = run_starter(d, srv.base, answer="yes")
            time.sleep(1.0)
            check("O1 an older ACLauncher.exe (no headless modes) is never sent the new switches: the check is skipped with a note and no request reached GitHub (the stand-in launcher starts no server, so the start itself is reported as failed, exit 3)", "older and cannot check from here" in out and not srv.sc.hits and rc == 3, (rc, out[-400:]))
            check("O2 the note gives the exact one-time migration: the four files to copy, from the first-install zip, and what never to copy",
                  all(x in out for x in ("first-install.zip", "ACLauncher.exe", "Start Server.bat", "server_starter.ps1", "SERVER_SETUP.txt", "never server_config.ini", "start ACLauncher.exe normally")), out[-900:])
        finally:
            stop_stubs(d)
        for flags, kw, desc in ((("-SkipUpdateCheck",), {}, "S1 -SkipUpdateCheck"), ((), {"check_updates": "0"}, "S2 check_updates = 0")):
            d = fresh("skip_" + desc[:2], **kw)
            srv.reset()
            srv.api_latest(srv.publish("v1.1.0", good))
            try:
                rc, out = run_starter(d, srv.base, *flags, answer="yes")
                time.sleep(1.0)
                check("%s: no check at all (no request to GitHub), nothing installed, the server starts" % desc, rc == 0 and not srv.sc.hits and (started(d) or [""])[0] == "v1", out[-300:])
            finally:
                stop_stubs(d)

        # ---- the prompt in a real console window (answers written into that console's input buffer)
        helper = os.path.join(REPO, "pc", "tools", "net_spike", "ded_con_helper.py")

        def interactive(name, answers, wait_rx, desc):
            d = fresh(name)
            srv.reset()
            srv.api_latest(srv.publish("v1.1.0", good))
            before = prog_state(d)
            env = dict(os.environ, AC_UPDATER_TEST="1", AC_UPDATER_TEST_API=srv.base)
            p = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(d, "server_starter.ps1"), "-Root", d], env=env, creationflags=0x10)
            out_json = os.path.join(d, "screen.json")

            def screen():
                subprocess.run([sys.executable, helper, str(p.pid), "read", out_json], capture_output=True, timeout=30)
                try:
                    return "\n".join(json.load(open(out_json))["rows"])
                except Exception:  # noqa: BLE001
                    return ""
            try:
                t0 = time.time()
                txt = ""
                while time.time() - t0 < 90 and "[Y/N]" not in txt:
                    time.sleep(1.0)
                    txt = screen()
                q = ("A newer version of Animal Crossing PC Multiplayer is available." in txt and "Installed version: 1.0.0" in txt and "Available version: 1.1.0" in txt
                     and "Would you like to download and install the update now? [Y/N]" in txt)
                check("%s - the console shows the exact question with both version numbers" % desc, q, txt[-600:])
                for a in answers:
                    subprocess.run([sys.executable, helper, str(p.pid), "send", a + "{ENTER}"], capture_output=True, timeout=30)
                    time.sleep(1.5)
                t0 = time.time()
                while time.time() - t0 < 120 and not re.search(wait_rx, txt):
                    time.sleep(1.5)
                    txt = screen()
                return d, before, txt
            finally:
                time.sleep(1.0)
                stop_stubs(d)
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        d, before, txt = interactive("tty_no", ["n"], r"Not updating", "T1 typed N")
        check("T1b typed N: 'Not updating', nothing changed, the installed (v1) server starts", "Not updating" in txt and prog_state(d) == before, txt[-500:])
        d, before, txt = interactive("tty_bad", ["maybe", "yes"], r"Updated 1\.0\.0 -> 1\.1\.0", "T2 typed 'maybe' then 'yes'")
        check("T2b an unclear answer asks again ('Please answer Y or N.') and only the later 'yes' installs: updated and verified, v2 started", "Please answer Y or N." in txt and "Updated 1.0.0 -> 1.1.0 (verified)" in txt and (started(d) or [""])[0] == "v2", txt[-600:])
    finally:
        srv.httpd.shutdown()
        shutil.rmtree(root, ignore_errors=True)
    print("-" * 60)
    print("%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
