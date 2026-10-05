#!/usr/bin/env python3
"""test_play_online_real.py - M-C Play Online wiring with REAL host and client processes (no GUI automation).

TIER: REAL HOST + REAL CLIENT game processes, on disposable copies of pc\\build64\\bin_fixture4 (bin_fixture4_mc = host, bin_fixture4_mcclient = client with an EMPTY
save dir; nothing else is touched: not the live save dir, bin_talkfix*, bin_fixture4).
  host   = `AnimalCrossing.exe --host P --bootstrap-resident 0 --town-serve on --resident-tokens tofu`
  client = the command line pc_relaunch_build_args() produces for a store character:
           `--connect 127.0.0.1:P --character <uuid> --town-fetch --online-ui`   (cwd = the client dir; the character is imported from a legacy profile first)

  G  the character has NO membership yet: the client fetches the town (empty save dir), arrives as a GUEST, reaches READY and writes
     characters/<uuid>/towns/<townkey>/membership.ini (role = guest, town_pid = the character's home PID) client-side; the saved server's last_town hint is set.
  R  membership.ini is replaced by role = resident + town_pid = the PersonalID of fixture resident 1: the client resolves the membership after the fetch, binds the
     resident by PID through the --bootstrap-resident path (slot 1), sends the RESIDENT claim under tofu (host mints a credential, the client stores it in the
     character's token.dat) and reaches READY; membership.ini stays resident.
Usage: python test_play_online_real.py [--port 11890]
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_mc")
CLIENT_DIR = os.path.join(T.BUILD64, "bin_fixture4_mcclient")
if __name__ == "__main__":
    T.make_fixture("bin_fixture4_mc")
    T.make_fixture("bin_fixture4_mcclient", empty_save=True)

import net_spike_lib as L  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
HOST_GCI = os.path.join(HOST_DIR, L.SAVE_GCI_REL)
EXE = os.path.join(CLIENT_DIR, "AnimalCrossing.exe")
INI = "name = Roger\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x1234\nland_id = 0x4321\n"


def resident_pid_hex(slot):
    with open(HOST_GCI, "rb") as f:
        f.seek(L._GCI_PRIVATE_BASE + slot * L._GCI_PRIVATE_STRIDE)
        return f.read(20).hex()


def start_client(port, uuid, tag):
    return L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--character", uuid, "--town-fetch", "--online-ui"], log_path=T.log_path("mc_client_%s.log" % tag),
                           bin_dir=CLIENT_DIR, label="mcclient").start()


def run(port, results):
    L.require_test_bin_dir()
    ck = lambda d, c: L.check(d, c, results)  # noqa: E731
    host = client = None
    try:
        mp = os.path.join(CLIENT_DIR, "save", "mp")
        os.makedirs(mp, exist_ok=True)
        with open(os.path.join(mp, "guest_roger.ini"), "w", newline="") as f:
            f.write(INI)
        with open(os.path.join(mp, "servers.ini"), "w", newline="") as f:
            f.write("[server]\nname = mcsrv\naddress = 127.0.0.1\nport = %d\n" % port)
        r = subprocess.run([EXE, "--character-import-profile", "roger"], cwd=CLIENT_DIR, capture_output=True, text=True, timeout=60)
        m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
        ck("setup: the legacy profile was imported as a store character", r.returncode == 0 and m is not None)
        if m is None:
            return
        uuid = m.group(1)
        home_pid = (b"Roger   " + b"GuestVil" + (0x1234).to_bytes(2, "big") + (0x4321).to_bytes(2, "big")).hex()

        for i in range(3):
            host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--town-serve", "on", "--resident-tokens", "tofu"],
                                 log_path=T.log_path("mc_host_try%d.log" % i), bin_dir=HOST_DIR).start()
            if host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=0):
                break
            host.stop()
            host = None
            time.sleep(2.0)
        ck("host (town-serve on, resident-tokens tofu) booted", host is not None)
        if host is None:
            return

        # ---------------- G: guest, no membership yet
        client = start_client(port, uuid, "guest")
        mo = client.wait_for_log(r"-> READY", 170.0)
        ctext = client.log_text()
        ck("G client fetched the town and reached READY", mo is not None and re.search(r"fetch: installed town ([0-9a-f_]{30})", ctext) is not None)
        ck("G the client log says 'no membership ... arriving as a guest'", "has no membership of town" in ctext and "arriving as a guest" in ctext)
        towns = glob.glob(os.path.join(mp, "characters", uuid, "towns", "*"))
        ck("G exactly one town directory under the character", len(towns) == 1)
        key = os.path.basename(towns[0]) if towns else ""
        mpath = os.path.join(towns[0], "membership.ini") if towns else ""
        time.sleep(1.0)
        mtext = open(mpath).read() if os.path.isfile(mpath) else ""
        ck("G membership.ini written client-side: role = guest, town_pid = the character's home PID", "role = guest" in mtext and ("town_pid = " + home_pid) in mtext)
        ck("G log line '[PC] membership: character ... now a guest of town <key>'", ("now a guest of town " + key) in client.log_text())
        ck("G servers.ini last_town hint = the townkey", ("last_town = " + key) in open(os.path.join(mp, "servers.ini")).read())
        client.stop()
        client = None
        time.sleep(7.0)  # host: the dropped client's slot times out

        # ---------------- R: resident membership
        rpid = resident_pid_hex(1)
        with open(mpath, "w", newline="") as f:
            f.write("role = resident\ntown_pid = %s\nlast_server = mcsrv\n" % rpid)
        client = start_client(port, uuid, "resident")
        mo = client.wait_for_log(r"-> READY", 170.0)
        ctext, htext = client.log_text(), host.log_text()
        ck("R the client log says RESIDENT membership, bound by PersonalID", "is a RESIDENT of town " + key in ctext)
        ck("R the PID resolved to slot 1 through the --bootstrap-resident path", "--resident-by-pid: resident PersonalID matches slot 1" in ctext and "--bootstrap-resident 1: resident bound" in ctext)
        ck("R the RESIDENT claim was sent (M-E) and the host answered under tofu", "playing a resident -- sent IDENTITY_EXT" in ctext and "[NET][RESIDENT]" in htext)
        ck("R the client reached READY as the resident (no guest arrival)", mo is not None and "[NET][GUEST] client: playing a guest" not in ctext)
        time.sleep(1.0)
        ck("R first claim: the resident token was stored in the character's token.dat", "first claim: resident token" in client.log_text() and os.path.isfile(os.path.join(towns[0], "token.dat")))
        mtext = open(mpath).read()
        ck("R membership.ini stays role = resident with the resident PID", "role = resident" in mtext and ("town_pid = " + rpid) in mtext)
    finally:
        for p in (client, host):
            if p is not None:
                p.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11890)
    args = ap.parse_args()
    results = []
    run(args.port, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
