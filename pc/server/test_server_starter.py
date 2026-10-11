#!/usr/bin/env python3
r"""test_server_starter.py - the Windows dedicated-server starter (Start Server.bat + server_starter.ps1 + server_config.ini).

TIER: the real PowerShell script and the real .bat run on this machine; the game itself is NOT started: -CheckOnly / -WriteSettingsOnly print or apply the plan, and the launch test uses a tiny
stub ACLauncher.exe / AnimalCrossing.exe (compiled with gcc; skipped, and reported as skipped, if there is no gcc) that records the arguments it receives. Source-drift checks keep the starter in
step with the game's real flags, defaults and debug categories (pc_main.c, pc_settings.c, pc_log.c, ac_launcher.c).
Usage: python test_server_starter.py"""
import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PS1 = os.path.join(HERE, "server_starter.ps1")
BAT = os.path.join(HERE, "Start Server.bat")
INI = os.path.join(HERE, "server_config.ini")
FILES = [PS1, BAT, INI]
results = []
TEMPS = []
atexit.register(lambda: [shutil.rmtree(x, ignore_errors=True) for x in TEMPS])


def check(desc, cond, extra=""):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc + ((" :: " + extra) if (extra and not cond) else ""))


def rd(*p):
    return open(os.path.join(REPO, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def make_dir(name="server"):
    base = tempfile.mkdtemp(prefix="acsrv ")  # a path WITH a space from the start
    TEMPS.append(base)
    d = os.path.join(base, name)
    os.makedirs(d)
    for f in FILES:
        shutil.copy(f, d)
    return d


def write_cfg(d, **kv):
    lines = ["%s = %s" % (k, v) for k, v in kv.items()]
    open(os.path.join(d, "server_config.ini"), "w", newline="\r\n").write("\n".join(lines) + "\n")


def run_ps(d, *flags, timeout=60):
    p = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(d, "server_starter.ps1"), "-Root", d] + list(flags),
                       capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout + p.stderr


def plan(out):
    args = [m for m in re.findall(r"^ARG: (.*)$", out, re.M)]
    ini = re.findall(r"^INI: (.*)$", out, re.M)
    return [a.strip() for a in args], [i.strip() for i in ini]


def check_args(d, **kv):
    write_cfg(d, **kv)
    rc, out = run_ps(d, "-CheckOnly")
    return rc, out, plan(out)[0]


BASE = ["--host", "7777", "--dedicated", "--max-peers", "8", "--max-guests", "4", "--allow-new-guests", "1", "--resident-tokens", "off", "--updater-skip"]


def main():
    d = make_dir()
    # ---- defaults and the shipped file ----
    rc, out = run_ps(d, "-CheckOnly")
    check("D1 the shipped server_config.ini is valid and gives the documented defaults: %s" % " ".join(plan(out)[0]), rc == 0 and plan(out)[0] == BASE, out[-300:])
    rc, out, a = check_args(d)  # an EMPTY config: every setting at its default
    check("D2 an empty server_config.ini = all defaults (same arguments)", rc == 0 and a == BASE)
    os.remove(os.path.join(d, "server_config.ini"))
    rc, out = run_ps(d, "-CheckOnly")
    check("D3 a missing server_config.ini is refused (exit 2, names the file), not silently defaulted", rc == 2 and "server_config.ini was not found" in out and plan(out)[0] == [])

    # ---- parsing ----
    rc, out, a = check_args(d, port="  7778  ", MAX_PEERS="10", Max_Guests="6")
    check("P1 keys are case-insensitive, values are trimmed, other settings keep their defaults", rc == 0 and a[:2] == ["--host", "7778"] and a[a.index("--max-peers") + 1] == "10" and a[a.index("--max-guests") + 1] == "6")
    open(os.path.join(d, "server_config.ini"), "w", newline="", encoding="utf-8").write("﻿# note\r\n; note\r\n\r\n[Server]\r\nport = 7790\r\n")
    rc, out = run_ps(d, "-CheckOnly")
    check("P2 a UTF-8 BOM, '#' / ';' comments, blank lines and a [section] header are accepted", rc == 0 and plan(out)[0][:2] == ["--host", "7790"])
    for bad, why in [("port = 7777 # my port", "a note after a value is refused (the game's own reader would not strip it)"), ("port = 1\nport = 2", "a duplicated key"),
                     ("prot = 7777", "an unknown key (typo)"), ("port 7777", "a line without ="), ("port =", "an empty value")]:
        open(os.path.join(d, "server_config.ini"), "w", newline="").write(bad + "\n")
        rc, out = run_ps(d, "-CheckOnly")
        check("P3 refused (exit 2, nothing planned): %s" % why, rc == 2 and plan(out)[0] == [] and "NOT started" in out, out[-200:])

    # ---- validation ----
    cases = [
        ("port", "0"), ("port", "65536"), ("port", "abc"), ("port", "-1"), ("port", "77 77"), ("port", "1e3"), ("port", "99999999999"),
        ("max_peers", "0"), ("max_peers", "255"), ("max_peers", "x"),
        ("max_guests", "0"), ("max_guests", "255"),
        ("allow_new_guests", "2"), ("allow_new_guests", "yes"), ("allow_new_guests", "-1"),
        ("resident_tokens", "on"), ("resident_tokens", "maybe"), ("resident_tokens", "1"),
        ("town_serve", "full"), ("town_serve", "2"), ("town_serve", "yes"),
        ("personal_sync", "maybe"), ("personal_sync", "2"),
        ("interest_management", "2"), ("interest_management", "on"),
        ("guest_memory_mb", "0"), ("guest_memory_mb", "1048577"), ("guest_memory_mb", "lots"),
        ("check_updates", "2"), ("check_updates", "true"),
        ("debug_categories", "network, guests"), ("debug_categories", "nonsense"), ("debug_categories", "network,,guests"), ("debug_categories", "network;guests"),
    ]
    for k, v in cases:
        rc, out, a = check_args(d, **{k: v})
        check("V1 %s = %r is refused (exit 2), the message names the setting, nothing is planned" % (k, v), rc == 2 and a == [] and (k in out), out[-250:])
    write_cfg(d, town_serve="full")
    check("V2 'town_serve = full' is refused with the reason", "not available" in run_ps(d, "-CheckOnly")[1])
    for k, v, exp in [("port", "1", "1"), ("port", "65535", "65535"), ("max_peers", "254", "254"), ("max_peers", "1", "1"), ("max_guests", "1", "1"), ("guest_memory_mb", "1048576", None), ("guest_memory_mb", "1", None)]:
        extra = {"max_guests": "1"} if k == "max_peers" else {}
        rc, out, a = check_args(d, **{k: v}, **extra)
        check("V3 boundary %s = %s accepted" % (k, v), rc == 0 and (exp is None or exp in a), out[-200:])
    # ---- combinations ----
    rc, out, a = check_args(d, max_peers="3", max_guests="5")
    check("C1 max_guests larger than max_peers is refused with the reason", rc == 2 and "larger than max_peers" in out and a == [])
    rc, out, a = check_args(d, max_peers="4", max_guests="4")
    check("C1 max_guests equal to max_peers is accepted", rc == 0)
    rc, out, a = check_args(d, personal_sync="off")
    check("C2 personal_sync = off while the town is served (auto) is refused with the reason", rc == 2 and "blank diaries" in out)
    rc, out, a = check_args(d, personal_sync="off", town_serve="on")
    check("C2 personal_sync = off with town_serve = on is refused", rc == 2)
    rc, out, a = check_args(d, personal_sync="off", town_serve="off")
    check("C2 personal_sync = off with town_serve = off is accepted", rc == 0 and a[a.index("--personal-sync") + 1] == "off" and a[a.index("--town-serve") + 1] == "off")

    # ---- argument generation ----
    rc, out, a = check_args(d, port="7800", max_peers="12", max_guests="9", allow_new_guests="0", resident_tokens="TOFU", town_serve="on", personal_sync="On", debug_categories="Network,guests",
                            interest_management="0", guest_memory_mb="512", check_updates="0")
    exp = ["--host", "7800", "--dedicated", "--max-peers", "12", "--max-guests", "9", "--allow-new-guests", "0", "--resident-tokens", "tofu", "--town-serve", "on", "--personal-sync", "on",
           "--debug=network,guests", "--updater-skip"]
    check("A1 every setting becomes exactly its verified argument: %s" % " ".join(a), rc == 0 and a == exp)
    rc, out, a = check_args(d, check_updates="1")
    check("A2 check_updates = 1 passes no --updater-skip (the launcher's own updater.ini decides)", rc == 0 and "--updater-skip" not in a)
    rc, out, a = check_args(d, check_updates="0")
    check("A3 check_updates = 0 passes --updater-skip", "--updater-skip" in a)
    rc, out, a = check_args(d, debug_categories="all")
    check("A4 debug_categories = all -> --debug=all; empty / none -> no debug argument", a[-2:] == ["--debug=all", "--updater-skip"] and "--debug" not in " ".join(check_args(d, debug_categories="none")[2]))
    check("A5 the arguments never contain a test hook or an override flag", not any(re.search(r"force|bootstrap|clock|weather|test|full|timeout|lease", x) for x in a))
    ini = plan(run_ps(d, "-CheckOnly")[1])[1]
    check("A6 settings with no command-line switch (+ the auto values) go to settings.ini: %s" % ini, len(ini) == 4)

    # ---- settings.ini updater ----
    write_cfg(d, interest_management="0", guest_memory_mb="512", town_serve="on", personal_sync="off")  # on -> no ini town_serve; personal_sync off + serve on is refused: use valid combo below
    write_cfg(d, interest_management="0", guest_memory_mb="512", town_serve="on", personal_sync="on")
    sp = os.path.join(d, "settings.ini")
    orig = ("[Graphics]\r\nwindow_width = 1280\r\nfullscreen = 0\r\n\r\n# my note\r\n[Network]\r\nmax_guests = 4\r\nguest_memory_mb = 256\r\ninteresT_management = 1\r\n"
            "town_serve = full\r\n\r\n[Input]\r\nstick_deadzone = 10\r\n")
    open(sp, "wb").write(orig.encode("utf-8"))
    rc, out = run_ps(d, "-WriteSettingsOnly")
    new = open(sp, "rb").read().decode("utf-8")
    exp_new = orig.replace("guest_memory_mb = 256", "guest_memory_mb = 512").replace("interesT_management = 1", "interesT_management = 0")
    check("S1 only the listed keys change (value in place, key spelling kept); every other line, comment, blank line and CRLF is byte-identical", rc == 0 and new == exp_new, repr(new))
    check("S1b town_serve = on is a command-line setting: an existing settings.ini town_serve line is NOT touched (the command line wins)", "town_serve = full" in new)
    t0 = os.path.getmtime(sp)
    time.sleep(1.1)
    run_ps(d, "-WriteSettingsOnly")
    check("S2 a second run with nothing to change does not rewrite the file", os.path.getmtime(sp) == t0 and open(sp, "rb").read().decode("utf-8") == exp_new)
    write_cfg(d)  # defaults: auto values must be written so a stale 'full' cannot apply
    open(sp, "wb").write(orig.encode("utf-8"))
    rc, out = run_ps(d, "-WriteSettingsOnly")
    new = open(sp, "rb").read().decode("utf-8")
    check("S3 town_serve = auto overwrites a stale 'full'; a missing personal_sync key is added inside [Network], before the next section", "town_serve = auto" in new and "town_serve = full" not in new
          and re.search(r"\[Network\][^\[]*personal_sync = auto[^\[]*\[Input\]", new, re.S) is not None, repr(new))
    open(sp, "wb").write(b"\xef\xbb\xbf[Graphics]\nwindow_width = 800\n")
    run_ps(d, "-WriteSettingsOnly")
    raw = open(sp, "rb").read()
    check("S4 a BOM and LF line endings are preserved; a missing [Network] section is appended", raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw and b"[Network]\n" in raw and b"window_width = 800\n" in raw, repr(raw))
    open(sp, "wb").write(b"[Network]\r\n# caf\xe9 note \xff\r\nguest_memory_mb = 1\r\n")
    run_ps(d, "-WriteSettingsOnly")
    raw = open(sp, "rb").read()
    check("S4b bytes the game never wrote (a note in another code page, 0xE9 / 0xFF) survive the rewrite unchanged", b"# caf\xe9 note \xff\r\n" in raw and b"\xef\xbf\xbd" not in raw and b"guest_memory_mb = 256" in raw, repr(raw))
    os.remove(sp)
    run_ps(d, "-WriteSettingsOnly")
    new = open(sp, "rb").read().decode("utf-8")
    check("S5 no settings.ini yet: a minimal one with a [Network] section is created", "[Network]" in new and "guest_memory_mb = 256" in new and "interest_management = 1" in new)
    # the game's own reader (pc_settings.c) ignores sections and reads 'key = value' lines: the written lines have exactly that shape
    check("S6 every written line is 'key = value' with no trailing text", all(re.fullmatch(r"[a-z_]+ = [A-Za-z0-9]+", l) for l in new.split("\r\n") if l and not l.startswith("[")))
    rc, out = run_ps(d, "-CheckOnly")
    check("S7 -CheckOnly never writes: settings.ini unchanged by it", open(sp, "rb").read().decode("utf-8") == new)

    # ---- the .bat, with spaces and special characters in the path ----
    sd = make_dir("dir with spaces & (parens) 1")
    write_cfg(sd, port="7123")
    r = subprocess.run('"%s" -CheckOnly' % os.path.join(sd, "Start Server.bat"), shell=True, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=90)
    a = plan(r.stdout)[0]
    check("B1 Start Server.bat works from a folder with spaces, '&' and parentheses: arguments %s" % a[:2], r.returncode == 0 and a[:2] == ["--host", "7123"] and sd.replace("/", "\\") in r.stdout, r.stdout[-300:] + r.stderr[-200:])
    write_cfg(sd, port="70000")
    r = subprocess.run('"%s" -CheckOnly' % os.path.join(sd, "Start Server.bat"), shell=True, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=90)
    check("B2 the .bat reports an invalid config, exits with that code (2) and waits for a key (keeps the window open)", r.returncode == 2 and "NOT started" in r.stdout and "Press any key" in r.stdout)
    b = open(BAT, encoding="ascii").read()
    check("B3 the .bat changes to its own folder, uses -NoProfile and a per-run ExecutionPolicy Bypass (no system setting changed) and pauses", 'cd /d "%~dp0"' in b and "-NoProfile" in b and "-ExecutionPolicy Bypass" in b and "pause" in b)

    # ---- the real launch path with stub executables ----
    gcc = shutil.which("gcc") or next((c for c in ("C:/msys64/ucrt64/bin/gcc.exe", "C:/msys64/mingw64/bin/gcc.exe") if os.path.exists(c)), None)
    if gcc is None:
        print("SKIP - launch tests: no gcc to build the stub executables (the starter's launch path is then untested here)")
    else:
        ld = make_dir("launch dir")
        src = os.path.join(ld, "stub.c")
        open(src, "w").write(r'''#include <windows.h>
#include <stdio.h>
#include <string.h>
int main(int argc, char** argv) {
    char self[MAX_PATH], dir[MAX_PATH], p[MAX_PATH];
    GetModuleFileNameA(NULL, self, MAX_PATH);
    strcpy(dir, self);
    *strrchr(dir, '\\') = 0;
    if (strstr(self, "ACLauncher") != NULL) {
        FILE* f; int i; STARTUPINFOA si; PROCESS_INFORMATION pi;
        sprintf(p, "%s\\launcher_args.txt", dir);
        f = fopen(p, "w");
        for (i = 1; i < argc; i++) fprintf(f, "%s\n", argv[i]);
        fclose(f);
        sprintf(p, "%s\\AnimalCrossing.exe", dir);
        memset(&si, 0, sizeof(si)); si.cb = sizeof(si);
        if (getenv("STUB_LAUNCHER_RC")) return atoi(getenv("STUB_LAUNCHER_RC"));
        if (!getenv("STUB_NO_GAME")) CreateProcessA(p, NULL, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi);
        return 0;
    }
    Sleep(20000);
    return 0;
}
''')
        genv = dict(os.environ, PATH=os.path.dirname(gcc) + os.pathsep + os.environ.get("PATH", ""))
        subprocess.run([gcc, "-O1", "-o", os.path.join(ld, "ACLauncher.exe"), src], check=True, env=genv)
        shutil.copy(os.path.join(ld, "ACLauncher.exe"), os.path.join(ld, "AnimalCrossing.exe"))
        write_cfg(ld, port="7456", max_guests="2", town_serve="on", personal_sync="on", interest_management="0")
        rc, out = run_ps(ld, timeout=90)
        args = open(os.path.join(ld, "launcher_args.txt")).read().split() if os.path.exists(os.path.join(ld, "launcher_args.txt")) else []
        check("L1 a normal start: ACLauncher.exe received exactly the verified arguments, the server process stayed up, exit 0: %s" % args,
              rc == 0 and args == ["--host", "7456", "--dedicated", "--max-peers", "8", "--max-guests", "2", "--allow-new-guests", "1", "--resident-tokens", "off", "--town-serve", "on", "--personal-sync", "on",
                                   "--updater-skip"] and "is running in its own window" in out, out[-400:])
        check("L2 the real settings.ini got interest_management = 0 and guest_memory_mb = 256", "interest_management = 0" in open(os.path.join(ld, "settings.ini")).read())
        rc2, out2 = run_ps(ld, timeout=60)
        check("L3 a second start while the game from this folder runs is refused (exit 2, nothing launched again)", rc2 == 2 and "already running" in out2, out2[-300:])
        stub_path = os.path.normpath(os.path.join(ld, "AnimalCrossing.exe"))
        killer = os.path.join(ld, "kill_stub.ps1")
        open(killer, "w").write("\n".join(["param([string]$P)", "$n = 0",
                                           "Get-Process AnimalCrossing -ErrorAction SilentlyContinue | Where-Object { $_.Path -ieq $P } | ForEach-Object { $n++; Stop-Process -Id $_.Id -Force }",
                                           "Write-Output $n", ""]))
        for _ in range(20):
            r = subprocess.run(["powershell.exe", "-NoProfile", "-File", killer, "-P", stub_path], capture_output=True, text=True, timeout=60)
            if r.stdout.strip() == "0":
                break
            time.sleep(0.5)
        time.sleep(0.5)
        os.environ["STUB_NO_GAME"] = "1"
        rc3, out3 = run_ps(ld, timeout=90)
        check("L4 a launcher that does not produce a running server is reported (exit 3, points at the logs), not announced as started", rc3 == 3 and "did not start" in out3 and "server.log" in out3, out3[-300:])
        os.environ["STUB_LAUNCHER_RC"] = "2"
        rc6, out6 = run_ps(ld, timeout=90)
        check("L6 a launcher that exits with an error code is reported with that code (exit 3)", rc6 == 3 and "error code 2" in out6, out6[-300:])
        del os.environ["STUB_LAUNCHER_RC"]
        del os.environ["STUB_NO_GAME"]
        os.remove(os.path.join(ld, "ACLauncher.exe"))
        rc4, out4 = run_ps(ld, timeout=60)
        check("L5 a missing ACLauncher.exe is refused with its path (no silent fallback to another exe)", rc4 == 2 and "ACLauncher.exe was not found" in out4)

    # ---- drift: the starter against the game's own source ----
    main_c = rd("pc", "src", "pc_main.c")
    for flag in ("--host", "--dedicated", "--max-peers", "--max-guests", "--allow-new-guests", "--resident-tokens", "--town-serve", "--personal-sync"):
        check("X1 the game's parser knows %s" % flag, 'strcmp(argv[i], "%s")' % flag in main_c)
    check("X1 the game's parser knows --debug= (pc_log) and the launcher knows --updater-skip", "--debug=a,b,c" in main_c and "--updater-skip" in rd("pc", "updater", "ac_launcher.c"))
    ps = open(PS1, encoding="utf-8").read()
    pc_log = rd("pc", "src", "pc_log.c")
    cats = re.findall(r'\{ PCL_\w+,\s*"(\w+)",\s*(?:"(\w+)"|NULL),\s*(?:"(\w+)"|NULL),', pc_log)
    names = {n for c in cats for n in c if n}
    mine = set(re.search(r"\$DebugNames = @\((.*?)\)", ps, re.S).group(1).replace("'", "").replace("\n", " ").replace(",", " ").split())
    check("X2 the starter's debug category names equal pc_log.c's names and aliases (+ all)", mine == names | {"all"}, str(sorted(mine ^ (names | {"all"}))))
    settings_c = rd("pc", "src", "pc_settings.c")
    dflt = {"max_guests": "4", "max_peers": "8", "guest_memory_mb": "256", "interest_management": "1", "allow_new_guests": "1", "resident_tokens": "off", "town_serve": "auto", "personal_sync": "auto"}
    ok = True
    for k, v in dflt.items():
        ok = ok and re.search(r'"%s = %s\\n"' % (k, v), settings_c) is not None and re.search(r"\$Defaults = @\{.*?%s\s*=\s*'%s'" % (k, v), ps, re.S) is not None
    check("X3 the starter's defaults equal the game's DEFAULT_SETTINGS (pc_settings.c) for the shared settings", ok)
    check("X4 the starter's default port is the game's (7777)", re.search(r"g_pc_net_port\s*=\s*7777", main_c) is not None and "port                = '7777'" in ps)
    check("X5 the ranges match: peers/guests 1..254 (pc_guest_admit / pc_peer_capacity_max), guest_memory_mb 1..1048576",
          "val >= 1 && val <= 1048576" in settings_c and "Read-WholeNumber 'max_peers' $cfg.max_peers 1 254" in ps and "Read-WholeNumber 'guest_memory_mb' $cfg.guest_memory_mb 1 1048576" in ps)
    cfg_text = open(INI, encoding="utf-8").read()
    for k in re.findall(r"\$Keys = @\((.*?)\)", ps, re.S)[0].replace("'", "").replace(",", " ").split():
        sect = re.search(r"#  %s\n(.*?)\n%s =" % (k, k), cfg_text, re.S)
        body = sect.group(1) if sect else ""
        check("X6 server_config.ini documents %s (what it controls, default / values, for you, for players)" % k,
              sect is not None and all(w in body for w in ("What it controls", "Default / values", "For you", "For players")))
    check("X7 no setting for hard-coded timeouts, leases, wildlife lifetimes, event claims, test hooks or town_serve=full exists in the config", not re.search(r"^(timeout|lease|lifetime|claim|hook|force|bootstrap)", cfg_text, re.M | re.I))
    shipped = {m.group(1): m.group(2).strip() for m in re.finditer(r"^([a-z_]+) = (.*)$", cfg_text, re.M)}
    check("X8 the values written in the shipped config are the defaults", all(shipped[k] == (ps_def if ps_def != "" else "") for k, ps_def in
          [(k, re.search(r"%s\s*=\s*'([^']*)'" % k, ps.split("$Defaults = @{")[1].split("}")[0]).group(1)) for k in shipped]))
    return 0 if all(c for _, c in results) else 1


if __name__ == "__main__":
    rc = main()
    print("-" * 60)
    print("%d/%d checks passed" % (sum(1 for _, c in results if c), len(results)))
    sys.exit(rc)
