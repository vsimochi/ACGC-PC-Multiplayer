#!/usr/bin/env python3
"""nook_visual_rig.py - guest-first town M4: a MANUAL / SCREENSHOT-DRIVEN rig for the guest Nook dialogue (NOT an automated test: a person or an agent drives the game window with
nook_visual_ctl.py and looks at game-window screenshots).

TIER: REAL dedicated host + ONE REAL store-character GUEST client on DISPOSABLE copies of pc\\build64\\bin_fixture4 (bin_fixture4_nk_<tag> = host with resident slot 3 / house 3
free, bin_fixture4_nk_<tag>c = client with an EMPTY save dir). Nothing else is touched (not the live save dir, bin_talkfix*, bin_fixture4, other disposable copies).
  host   = `AnimalCrossing.exe --host P --dedicated --town-serve on --resident-tokens tofu`
  client = `AnimalCrossing.exe --connect 127.0.0.1:P --character <uuid> --town-fetch --online-ui [--nook-test SPEC]` (AC_DISPLAY_NAME=samsung, AC_MASTER_VOLUME=1: exit code 4 = no unique display)
`--nook-test SPEC` (TEST-ONLY, client only; see pc_net_game.c pcnetgame_run_nook_test_hook): wallet=N sets the LOCAL wallet once (uploaded to the host mirror), warp = go to the shop.
The rig stays alive until the file <log dir>/nk_stop appears, then writes <log dir>/nk_report.txt (host `residents` / `guests` output + the interesting log lines) and stops everything.
Usage: python nook_visual_rig.py --tag s2 [--port 11970] [--spec wallet=20000,warp]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402
import make_four_resident_fixture as MF  # noqa: E402
import struct  # noqa: E402



def make_free_slot(dest, slot=3):
    """Same as test_house_purchase_real.make_free_slot: resident `slot` and its house's owner cleared (checksum + backup recomputed) in the DISPOSABLE copy."""
    p = os.path.join(dest, MF.GCI_REL)
    g = bytearray(open(p, "rb").read())
    clear = b" " * 16 + struct.pack(">HH", 0xFFFF, 0xFFFF)
    po = MF.MAIN_OFF + MF.PRIV_OFF + slot * MF.PRIV_STRIDE
    g[po:po + MF.PRIV_STRIDE] = bytes(MF.PRIV_STRIDE)
    g[po:po + 20] = clear
    ho = MF.MAIN_OFF + MF.HOME_OFF + slot * MF.HOME_STRIDE
    g[ho:ho + 20] = clear
    m = bytearray(g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE])
    m[MF.CHK_OFF:MF.CHK_OFF + 2] = struct.pack(">H", MF.checksum(bytes(m)))
    g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE] = m
    g[MF.BACK_OFF:MF.BACK_OFF + MF.SAVE_SECTOR_SIZE] = g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_SECTOR_SIZE]
    open(p, "wb").write(bytes(g))


ap = argparse.ArgumentParser()
ap.add_argument("--tag", required=True)
ap.add_argument("--port", type=int, default=11970)
ap.add_argument("--spec", default="")
args = ap.parse_args()
HOST_NAME = "bin_fixture4_nk_" + args.tag
CLIENT_NAME = HOST_NAME + "c"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, HOST_NAME)
T.make_fixture(HOST_NAME)
make_free_slot(os.path.join(T.BUILD64, HOST_NAME))
T.make_fixture(CLIENT_NAME, empty_save=True)

import net_spike_lib as L  # noqa: E402

CLIENT_DIR = os.path.join(T.BUILD64, CLIENT_NAME)
EXE = os.path.join(CLIENT_DIR, "AnimalCrossing.exe")
INI = "name = Buyer\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x1234\nland_id = 0x4321\n"
ENV = {"AC_TEST_HOOKS": "1", "AC_TOWN_NO_MSGBOX": "1", "AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1"}
STOP = T.log_path("nk_stop")
STATE = T.log_path("nk_state.json")
REPORT = T.log_path("nk_report.txt")
for p in (STOP, STATE, REPORT):
    if os.path.exists(p):
        os.remove(p)


def say(h, cmd, settle=1.5, timeout=25.0):
    off = len(h.log_text())
    h.send_line(cmd)
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


host = client = None
try:
    L.require_test_bin_dir()
    mp = os.path.join(CLIENT_DIR, "save", "mp")
    os.makedirs(mp, exist_ok=True)
    open(os.path.join(mp, "guest_buyer.ini"), "w", newline="").write(INI)
    open(os.path.join(mp, "servers.ini"), "w", newline="").write("[server]\nname = nksrv\naddress = 127.0.0.1\nport = %d\n" % args.port)
    r = subprocess.run([EXE, "--character-import-profile", "buyer"], cwd=CLIENT_DIR, capture_output=True, text=True, timeout=60)
    m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
    assert r.returncode == 0 and m, "profile import failed: " + r.stdout + r.stderr
    uuid = m.group(1)
    cargs = ["--character", uuid, "--town-fetch", "--online-ui"]
    if args.spec:
        cargs += ["--nook-test", args.spec]
    host = L.HostProcess(port=args.port, extra_args=["--dedicated", "--town-serve", "on", "--resident-tokens", "tofu"], log_path=T.log_path("nk_%s_host.log" % args.tag),
                         bin_dir=L.GAME_BIN_DIR, stdin_pipe=True, verbose=False, new_group=True).start()
    assert host.wait_listening(60.0) and host.boot_to_dedicated(timeout=120.0), "host did not boot"
    client = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=cargs, log_path=T.log_path("nk_%s_client.log" % args.tag), bin_dir=CLIENT_DIR, env=ENV, label="nkc").start()
    mo = client.wait_for_log(r"-> READY", 240.0)
    ready = mo is not None and client.proc.poll() is None
    json.dump({"pid": client.proc.pid, "host_pid": host.proc.pid, "uuid": uuid, "client_dir": CLIENT_DIR, "host_dir": L.GAME_BIN_DIR, "ready": ready,
               "client_log": client.log_path, "host_log": host.log_path, "exit": client.proc.poll()}, open(STATE, "w"))
    print("rig up: client pid %d ready=%s (exit %s); waiting for %s" % (client.proc.pid, ready, client.proc.poll(), STOP), flush=True)
    while not os.path.exists(STOP):
        time.sleep(1.0)
    out = []
    out.append("client alive at stop: %s pid %s" % (client.proc.poll() is None, client.proc.pid))
    out.append("--- host `residents`\n" + say(host, "residents"))
    out.append("--- host `guests`\n" + say(host, "guests"))
    towns_dir = os.path.join(CLIENT_DIR, "save", "mp", "characters", uuid, "towns")
    for d in (os.listdir(towns_dir) if os.path.isdir(towns_dir) else []):
        mf = os.path.join(towns_dir, d, "membership.ini")
        out.append("--- membership.ini (%s)\n%s" % (d, open(mf).read() if os.path.isfile(mf) else "(none)"))
    rx = re.compile(r"nook dialogue|NOOK|house purchase|HOUSE_PURCHASE|BOUGHT|first-job|first job|intro|Unhandled|nook-test|M3:|play-online: in-process|-> READY|promoted|PROMOTED")
    out.append("--- client log (filtered)\n" + "\n".join(l for l in client.log_text().replace("\r\n", "\n").split("\n") if rx.search(l)))
    out.append("--- host log (filtered)\n" + "\n".join(l for l in host.log_text().replace("\r\n", "\n").split("\n") if re.search(r"BOUGHT|HOUSE|PROMOT|guest|resident", l))[-6000:])
    open(REPORT, "w", encoding="utf-8").write("\n".join(out))
finally:
    if client is not None:
        client.stop()
    if host is not None:
        try:
            host.send_line("stop")
            host.wait_exit(60.0)
        except Exception:
            pass
        host.stop()
    print("rig down", flush=True)
