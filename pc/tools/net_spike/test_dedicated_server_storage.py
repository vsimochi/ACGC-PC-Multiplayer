#!/usr/bin/env python3
"""test_dedicated_server_storage.py - the dedicated server's own storage tree (servers/<id>/) and first-launch town generation.

TIER: REAL dedicated host + ONE REAL guest client on DISPOSABLE copies of pc\\build64\\bin_fixture4 with EMPTY save dirs (bin_fixture4_srv = host, bin_fixture4_srvc = client). Nothing else is
touched (not the live save dir, bin_talkfix*, bin_fixture4, other disposable copies).
  S1  CLEAN SERVER CREATION: `AnimalCrossing.exe --host P --dedicated` in a directory with no save/card_a and no servers/: servers/default/ + subdirectories, a NEW town generated
      and saved to servers/default/town/card_a/DobutsunomoriP_MURA.gci, server.ini [server] + [town] identity (origin = generated), residents/members.dat created, logs/server.log, the
      host reaches 'world ready'; save/card_a and save/mp host files are NOT created
  S2  RESTART: stop (exit 0), start again: NO new town ('generating' absent), the same identity ('town identity confirmed'), the same town key / land id, the GCI is the same town
  S3  REAL CLIENT: a guest store character connects with --town-fetch (transfer of the server's town), reaches READY, the host lists it; the server tree holds citizens/guests.dat
Usage: python test_dedicated_server_storage.py [--port 12995]
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_srv")
CLIENT_DIR = os.path.join(T.BUILD64, "bin_fixture4_srvc")

if __name__ == "__main__":
    T.make_fixture("bin_fixture4_srv", empty_save=True)
    T.make_fixture("bin_fixture4_srvc", empty_save=True)

import net_spike_lib as L  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
EXE = os.path.join(CLIENT_DIR, "AnimalCrossing.exe")
SRV = os.path.join(HOST_DIR, "servers", "default")
GCI = os.path.join(SRV, "town", "card_a", "DobutsunomoriP_MURA.gci")
INI = "name = Visitor\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x1236\nland_id = 0x4322\n"
ENV = {"AC_TOWN_NO_MSGBOX": "1", "AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1"}


def read(p):
    with open(p, "rb") as f:
        return f.read()


def ini_value(path, key, section=None):
    sec = None
    for line in read(path).decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("["):
            sec = line
        elif "=" in line and (section is None or sec == section):
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return None


def say(h, cmd, settle=1.2, timeout=10.0):
    off = len(h.log_text())
    assert h.send_line(cmd)
    end = time.monotonic() + timeout
    last, last_t = off, time.monotonic()
    while time.monotonic() < end:
        time.sleep(0.2)
        n = len(h.log_text())
        if n != last:
            last, last_t = n, time.monotonic()
        elif time.monotonic() - last_t >= settle and n > off:
            break
    return h.log_text()[off:].replace("\r\n", "\n")


def start_host(port, tag):
    h = L.HostProcess(port=port, extra_args=["--dedicated", "--town-serve", "on", "--resident-tokens", "tofu"], log_path=T.log_path("srv_%s_host.log" % tag), bin_dir=HOST_DIR,
                      stdin_pipe=True, verbose=False, new_group=True).start()
    ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=180.0)
    return h, ok


def run(port, results):
    L.require_test_bin_dir()
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    ck("(setup) the host directory has no save/card_a and no servers/ yet", not os.path.exists(os.path.join(HOST_DIR, "servers")) and not os.path.exists(os.path.join(HOST_DIR, "save", "card_a")))
    # ---------------- S1
    h, ok = start_host(port, "1")
    ck("S1 the dedicated host reached world ready on a CLEAN directory (no town existed)", ok)
    if not ok:
        h.stop()
        return
    log1 = h.log_text().replace("\r\n", "\n")
    ck("S1 the log says the server was opened with 'NOT FOUND -> a new town is generated' and then 'generated and saved'", "a new town is generated on this first launch" in log1 and b"generated and saved to" in read(os.path.join(SRV, "logs", "server.log")))
    for sub in ("town/card_a", "citizens", "residents", "backups", "logs"):
        ck("S1 servers/default/%s exists" % sub, os.path.isdir(os.path.join(SRV, *sub.split("/"))))
    ck("S1 the town GCI exists at servers/default/town/card_a/DobutsunomoriP_MURA.gci (467008 bytes)", os.path.isfile(GCI) and os.path.getsize(GCI) == 467008)
    ini = os.path.join(SRV, "server.ini")
    ck("S1 server.ini: [server] id = default and [town] with land_name / land_id / terrain_hash / key, origin = generated", os.path.isfile(ini) and ini_value(ini, "id", "[server]") == "default"
       and ini_value(ini, "key", "[town]") and ini_value(ini, "land_id", "[town]") and ini_value(ini, "terrain_hash", "[town]") and ini_value(ini, "origin", "[town]") == "generated")
    key1 = ini_value(ini, "key", "[town]")
    lid1 = ini_value(ini, "land_id", "[town]")
    ck("S1 residents/members.dat was created (the server's resident state exists from the start)", os.path.isfile(os.path.join(SRV, "residents", "members.dat")))
    ck("S1 logs/server.log exists and mentions the new town", os.path.isfile(os.path.join(SRV, "logs", "server.log")) and b"generated and saved" in read(os.path.join(SRV, "logs", "server.log")))
    ck("S1 the client tree is not used by the server: no save/card_a, no save/mp/guests.dat / members.dat / records.dat",
       not os.path.exists(os.path.join(HOST_DIR, "save", "card_a")) and not any(os.path.exists(os.path.join(HOST_DIR, "save", "mp", n)) for n in ("guests.dat", "members.dat", "records.dat")))
    out = say(h, "status")
    ck("S1 `status` names the server tree", "server: default (servers/default)" in out)
    h.send_line("stop")
    ck("S1 graceful stop (exit 0)", h.wait_exit(90.0) == 0)
    h.stop()
    # ---------------- S2
    h, ok = start_host(port, "2")
    ck("S2 the restarted dedicated host reached world ready", ok)
    if not ok:
        h.stop()
        return
    log2 = h.log_text().replace("\r\n", "\n")
    ck("S2 NO new town: 'generating a new town' / 'a new town is generated' are absent, 'town found' is logged", "generating a new town" not in log2 and "a new town is generated" not in log2 and "town found" in log2)
    ck("S2 the same town identity is confirmed (log: 'town identity confirmed (%s)') and server.ini is unchanged" % key1,
       ("town identity confirmed (%s)" % key1) in log2 and ini_value(ini, "key", "[town]") == key1 and ini_value(ini, "land_id", "[town]") == lid1)
    ck("S2 the authoritative GCI is still servers/default/town/card_a/... (the loaded path is logged)", os.path.isfile(GCI) and "town dir servers/default/town" in say(h, "status").replace("\\", "/"))
    # ---------------- S3
    mp = os.path.join(CLIENT_DIR, "save", "mp")
    os.makedirs(mp, exist_ok=True)
    with open(os.path.join(mp, "guest_visitor.ini"), "w", newline="") as f:
        f.write(INI)
    r = subprocess.run([EXE, "--character-import-profile", "visitor"], cwd=CLIENT_DIR, capture_output=True, text=True, timeout=60)
    m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
    ck("S3 (setup) the legacy profile was imported as a store character", r.returncode == 0 and m is not None)
    client = None
    try:
        if m is not None:
            client = L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--character", m.group(1), "--town-fetch"], log_path=T.log_path("srv_client.log"), bin_dir=CLIENT_DIR, env=ENV,
                                     label="srvc").start()
            mo = client.wait_for_log(r"-> READY", 240.0)
            ctext = client.log_text().replace("\r\n", "\n")
            ck("S3 the real guest client fetched the server's town and reached READY", mo is not None and "arriving as a guest" in ctext)
            out = say(h, "guests")
            ck("S3 the host lists the guest (admission works against the server tree)", "VISITOR" in out.upper() or "Visitor" in out)
            time.sleep(8.0)
            ck("S3 citizens/guests.dat exists in the server tree (and not in save/mp)", os.path.isfile(os.path.join(SRV, "citizens", "guests.dat"))
               and not os.path.exists(os.path.join(HOST_DIR, "save", "mp", "guests.dat")))
    finally:
        if client is not None:
            client.stop()
        h.send_line("stop")
        h.wait_exit(90.0)
        h.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12995)
    args = ap.parse_args()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: disposable pc\\build64\\bin_fixture4_<name> copies only", file=sys.stderr)
        return 2
    results = []
    run(args.port, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
