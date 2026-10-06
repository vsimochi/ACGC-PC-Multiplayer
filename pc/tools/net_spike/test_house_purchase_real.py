#!/usr/bin/env python3
"""test_house_purchase_real.py - guest-first town M1+M2+M3: a REAL store-character GUEST buys a house (`--house-buy-test auto`) from a REAL dedicated host and re-joins IN-PROCESS as the resident.

TIER: REAL dedicated host + ONE REAL client game process on DISPOSABLE copies of pc\\build64\\bin_fixture4 (bin_fixture4_hbuy_rc = host with resident slot 3 / house 3 free,
bin_fixture4_hbuy_rcc = client with an EMPTY save dir). Nothing else is touched (not the live save dir, bin_talkfix*, bin_fixture4, other disposable copies).
  host   = `AnimalCrossing.exe --host P --dedicated --town-serve on --resident-tokens tofu`
  client = `AnimalCrossing.exe --connect 127.0.0.1:P --character <uuid> --town-fetch --online-ui --house-buy-test auto` (the display is pinned with AC_DISPLAY_NAME=samsung, AC_MASTER_VOLUME=1)
  G  the character fetches the town and arrives as a GUEST (guest claim, READY); the hook raises the LOCAL wallet to 20000 (its one local write), waits for the D3 upload and buys house `auto`
  H  host: 'BOUGHT a house: resident slot 3, house 3 ... loan 0'; client: 'house purchase APPLIED', RESIDENT_HANDOFF -> token.dat + membership.ini (role resident, nook_intro = bought preserved)
  M  the SAME client PID re-joins in-process (no 'RELAUNCH', no second process): the in-process connect line, the town is re-fetched, role = resident, the slot is bound by PersonalID,
     the RESIDENT claim carries the handed-over token and the client reaches READY a second time; the host confirms the credential and removes the handoff
Usage: python test_house_purchase_real.py [--port 11960]
"""
import argparse
import os
import re
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402
import make_four_resident_fixture as MF  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_hbuy_rc")
CLIENT_DIR = os.path.join(T.BUILD64, "bin_fixture4_hbuy_rcc")


def make_free_slot(dest, slot=3):
    """The disposable town save: resident `slot` cleared and its house's owner cleared (checksum + backup copy recomputed), same as test_promotion_protocol.py."""
    p = os.path.join(dest, MF.GCI_REL)
    g = bytearray(open(p, "rb").read())
    clear = b"\x20" * 16 + struct.pack(">HH", 0xFFFF, 0xFFFF)
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


if __name__ == "__main__":
    T.make_fixture("bin_fixture4_hbuy_rc")
    make_free_slot(os.path.join(T.BUILD64, "bin_fixture4_hbuy_rc"))
    T.make_fixture("bin_fixture4_hbuy_rcc", empty_save=True)

import net_spike_lib as L  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
EXE = os.path.join(CLIENT_DIR, "AnimalCrossing.exe")
INI = "name = Buyer\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x1234\nland_id = 0x4321\n"
ENV = {"AC_TOWN_NO_MSGBOX": "1", "AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1"}


def say(h, cmd, settle=1.5, timeout=25.0):
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


def wait_count(client, pattern, n, timeout):
    rx = re.compile(pattern)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if len(rx.findall(client.log_text())) >= n:
            return True
        if client.proc is not None and client.proc.poll() is not None:
            return len(rx.findall(client.log_text())) >= n
        time.sleep(0.5)
    return False


def run(port, results):
    L.require_test_bin_dir()
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = client = None
    try:
        mp = os.path.join(CLIENT_DIR, "save", "mp")
        os.makedirs(mp, exist_ok=True)
        with open(os.path.join(mp, "guest_buyer.ini"), "w", newline="") as f:
            f.write(INI)
        with open(os.path.join(mp, "servers.ini"), "w", newline="") as f:
            f.write("[server]\nname = hbuysrv\naddress = 127.0.0.1\nport = %d\n" % port)
        r = subprocess.run([EXE, "--character-import-profile", "buyer"], cwd=CLIENT_DIR, capture_output=True, text=True, timeout=60)
        m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
        ck("setup: the legacy profile was imported as a store character", r.returncode == 0 and m is not None)
        if m is None:
            return
        uuid = m.group(1)
        cargs = ["--character", uuid, "--town-fetch", "--online-ui", "--house-buy-test", "auto"]

        host = L.HostProcess(port=port, extra_args=["--dedicated", "--town-serve", "on", "--resident-tokens", "tofu"], log_path=T.log_path("hbuy_rc_host.log"), bin_dir=HOST_DIR,
                             stdin_pipe=True, verbose=False, new_group=True).start()
        ok = host.wait_listening(60.0) and host.boot_to_dedicated(timeout=120.0)
        ck("host (dedicated, town-serve on, resident-tokens tofu) booted", ok)
        if not ok:
            return

        client = L.ClientProcess("127.0.0.1:%d" % port, extra_args=cargs, log_path=T.log_path("hbuy_rc_client.log"), bin_dir=CLIENT_DIR, env=ENV, label="hbuyrcc").start()
        mo = client.wait_for_log(r"-> READY", 200.0)
        ck("G the client fetched the town and reached READY as a guest", mo is not None and "arriving as a guest" in client.log_text())
        if client.proc.poll() is not None:
            ck("G the client process is alive (exit code %s: 4 = AC_DISPLAY_NAME matched no display)" % client.proc.returncode, False)
            return
        pid = client.proc.pid
        mo = client.wait_for_log(r"house purchase APPLIED", 120.0)
        ctext = client.log_text()
        ck("H the hook raised the wallet, requested the house through the REAL request path and the host APPLIED it ('house purchase APPLIED')",
           mo is not None and "--house-buy-test: requesting a house (auto)" in ctext and "begin HOUSE_PURCHASE" in ctext and "TXN_COMMIT sent kind=HOUSE_PURCHASE" in ctext)
        htext = host.log_text()
        ck("H host: 'BOUGHT a house: resident slot 3, house 3, price 18400, wallet 20000 -> 1600, loan 0' then the handoff", "BOUGHT a house: resident slot 3, house 3, price 18400, wallet 20000 -> 1600, loan 0" in htext
           and "RESIDENT_HANDOFF sent" in htext)

        # ---------------- M: the in-process rejoin
        mo = client.wait_for_log(r"play-online: in-process connect pid=(\d+)", 120.0)
        ctext = client.log_text()
        ck("H client: RESIDENT_HANDOFF honoured (token.dat + membership.ini written) and REJECT 6 shown", "the host PROMOTED this guest to resident slot 3" in ctext and "token.dat + membership.ini written" in ctext
           and "promoted to a resident of this town" in ctext)
        ck("M the client re-joins IN-PROCESS (no relaunch): 'M3: promoted: re-joining IN-PROCESS as the resident' and the in-process connect line carries the SAME pid %d" % pid,
           mo is not None and int(mo.group(1)) == pid and "M3: promoted: re-joining IN-PROCESS as the resident (no relaunch)" in ctext and "RELAUNCH" not in ctext and "relaunched as the resident" not in ctext)
        ok2 = wait_count(client, r"-> READY", 2, 240.0)
        ctext = client.log_text()
        ck("M the SAME process reached READY a second time", ok2 and client.proc.poll() is None and client.proc.pid == pid)
        ck("M role = resident: membership said RESIDENT, the slot was bound by PersonalID (slot 3), the RESIDENT claim (not a guest claim) went out",
           "is a RESIDENT of town" in ctext and "--resident-by-pid: resident PersonalID matches slot 3" in ctext and "playing a resident -- sent IDENTITY_EXT" in ctext
           and len(re.findall(r"\[NET\]\[GUEST\] client: playing a guest", ctext)) == 1)
        time.sleep(4.0)
        htext = host.log_text()
        ck("M host: the resident credential was confirmed and the promotion handoff entry removed", "promotion handoff removed" in htext)
        out = say(host, "residents")
        ck("M `residents`: slot 3 confirmed=yes connected=yes", re.search(r'slot 3: name="Buyer\s*" credential=yes confirmed=yes armed=no connected=yes', out) is not None)
        towns_dir = os.path.join(mp, "characters", uuid, "towns")
        towns = [os.path.join(towns_dir, d) for d in os.listdir(towns_dir)] if os.path.isdir(towns_dir) else []
        mtext = open(os.path.join(towns[0], "membership.ini")).read() if len(towns) == 1 and os.path.isfile(os.path.join(towns[0], "membership.ini")) else ""
        ck("M membership.ini: role = resident, the new town PID, AND the preserved key nook_intro = bought", "role = resident" in mtext and "nook_intro = bought" in mtext and mtext.lower().count("town_pid = ") == 1)
    finally:
        if client is not None:
            client.stop()
        if host is not None:
            host.send_line("stop")
            host.wait_exit(60.0)
            host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11960)
    args = ap.parse_args()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: disposable pc\\build64\\bin_fixture4_<name> copies only", file=sys.stderr)
        return 2
    results = []
    run(args.port, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
