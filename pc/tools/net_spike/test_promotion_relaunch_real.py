#!/usr/bin/env python3
"""test_promotion_relaunch_real.py - M-I: guest -> resident promotion with a REAL host and a REAL client process (store character), automatic relaunch.

TIER: REAL dedicated host + REAL client game processes on DISPOSABLE copies of pc\\build64\\bin_fixture4 (bin_fixture4_promo_rc = host with resident slot 3 / house 3 free,
bin_fixture4_promo_rcc = client with an EMPTY save dir). Nothing else is touched (not the live save dir, bin_talkfix*, bin_fixture4).
  host   = `AnimalCrossing.exe --host P --dedicated --town-serve on --resident-tokens tofu`
  G  a store character (imported from a legacy profile) fetches the town and arrives as a GUEST (membership.ini role = guest, guest token stored), the host holds its synced record
  P  the host console promotes it: `promote Roger auto auto confirm`
  D  the SAME client command line (`--character <uuid> --town-fetch --online-ui`) with AC_RELAUNCH_DRYRUN=1: it fetches the town (now containing the resident), connects as the guest,
     receives RESIDENT_HANDOFF + REJECT 6, rewrites membership.ini (role = resident) + token.dat and LOGS the relaunch command line (no process is started, the client keeps running); the
     relaunch is requested exactly once
  R  the logged command line is EXECUTED as the relaunched client: it fetches the town, resolves role = resident, binds by PersonalID, sends the RESIDENT claim, reaches READY; the host
     confirms the credential and deletes the handoff entry
Usage: python test_promotion_relaunch_real.py [--port 11940]
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

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_promo_rc")
CLIENT_DIR = os.path.join(T.BUILD64, "bin_fixture4_promo_rcc")


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
    T.make_fixture("bin_fixture4_promo_rc")
    make_free_slot(os.path.join(T.BUILD64, "bin_fixture4_promo_rc"))
    T.make_fixture("bin_fixture4_promo_rcc", empty_save=True)

import net_spike_lib as L  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
EXE = os.path.join(CLIENT_DIR, "AnimalCrossing.exe")
INI = "name = Roger\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x1234\nland_id = 0x4321\n"


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


def gtk_entries(path):
    """[(home_pid, token)] of the PRESENT entries of a PCMpGtk token file (header 32, 4 x 64-byte entries: present @0, home pid @28, token @48)."""
    b = open(path, "rb").read()
    return [(b[32 + i * 64 + 28:32 + i * 64 + 48], b[32 + i * 64 + 48:32 + i * 64 + 64]) for i in range(4) if b[32 + i * 64] == 1]


def start_client(port, args, tag, env=None):
    return L.ClientProcess("127.0.0.1:%d" % port, extra_args=args, log_path=T.log_path("promo_rc_client_%s.log" % tag), bin_dir=CLIENT_DIR, env=env, label="promorcc").start()


def run(port, results):
    L.require_test_bin_dir()
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = client = None
    try:
        mp = os.path.join(CLIENT_DIR, "save", "mp")
        os.makedirs(mp, exist_ok=True)
        with open(os.path.join(mp, "guest_roger.ini"), "w", newline="") as f:
            f.write(INI)
        with open(os.path.join(mp, "servers.ini"), "w", newline="") as f:
            f.write("[server]\nname = promosrv\naddress = 127.0.0.1\nport = %d\n" % port)
        r = subprocess.run([EXE, "--character-import-profile", "roger"], cwd=CLIENT_DIR, capture_output=True, text=True, timeout=60)
        m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
        ck("setup: the legacy profile was imported as a store character", r.returncode == 0 and m is not None)
        if m is None:
            return
        uuid = m.group(1)
        cargs = ["--character", uuid, "--town-fetch", "--online-ui"]

        host = L.HostProcess(port=port, extra_args=["--dedicated", "--town-serve", "on", "--resident-tokens", "tofu"], log_path=T.log_path("promo_rc_host.log"), bin_dir=HOST_DIR,
                             stdin_pipe=True, verbose=False, new_group=True).start()
        ok = host.wait_listening(60.0) and host.boot_to_dedicated(timeout=120.0)
        ck("host (dedicated, town-serve on, resident-tokens tofu) booted", ok)
        if not ok:
            return

        # ---------------- G: the character joins as a guest
        client = start_client(port, cargs, "guest")
        mo = client.wait_for_log(r"-> READY", 170.0)
        ck("G the client fetched the town and reached READY as a guest", mo is not None and "arriving as a guest" in client.log_text())
        time.sleep(12.0)  # the guest record is uploaded + synced (guests.dat) after READY
        client.stop()
        client = None
        time.sleep(9.0)  # the host drops the dead peer
        out = say(host, "guests")
        ck("G `guests` lists Roger", "Roger" in out)

        # ---------------- P: promote
        out = say(host, "promote Roger auto auto confirm", timeout=40.0, settle=2.5)
        ck("P promote: 'PROMOTED to RESIDENT slot 3, house 3'", "PROMOTED to RESIDENT slot 3, house 3" in out)
        if "PROMOTED to RESIDENT" not in out:
            L.info("promote output: " + out[-600:])
            return

        # ---------------- D: the same client command, relaunch dry run
        env = {"AC_TEST_HOOKS": "1", "AC_RELAUNCH_DRYRUN": "1"}
        client = start_client(port, cargs, "dryrun", env=env)
        mo = client.wait_for_log(r"RELAUNCH DRYRUN: <this executable> ([^\r\n]+)", 170.0)
        ctext = client.log_text()
        ck("D the client received RESIDENT_HANDOFF and wrote token.dat + membership.ini", "the host PROMOTED this guest to resident slot 3" in ctext and "token.dat + membership.ini written" in ctext)
        ck("D REJECT 6 PROMOTED was shown (join message)", "promoted to a resident of this town" in ctext)
        cmdline = mo.group(1).strip() if mo else ""
        want = '--connect 127.0.0.1:%d --character "%s" --town-fetch --online-ui' % (port, uuid)
        ck("D the logged relaunch command line is exactly `%s`" % want, cmdline == want or cmdline.startswith(want + " "))
        time.sleep(6.0)
        ctext = client.log_text()
        ck("D the relaunch is requested ONCE (no loop) and the dry run does not quit the process", len(re.findall(r"RELAUNCH DRYRUN", ctext)) == 1 and client.proc.poll() is None)
        towns = [os.path.join(mp, "characters", uuid, "towns", d) for d in os.listdir(os.path.join(mp, "characters", uuid, "towns"))] if os.path.isdir(os.path.join(mp, "characters", uuid, "towns")) else []
        mtext = open(os.path.join(towns[0], "membership.ini")).read() if len(towns) == 1 and os.path.isfile(os.path.join(towns[0], "membership.ini")) else ""
        ck("D membership.ini = role resident + the new town PID (Roger / the host land / a 0xF0xx player id); token.dat exists",
           "role = resident" in mtext and mtext.lower().count("town_pid = ") == 1 and "town_pid = " + b"Roger   ".hex() in mtext.lower() and len(towns) == 1 and os.path.isfile(os.path.join(towns[0], "token.dat")))
        tk = gtk_entries(os.path.join(towns[0], "token.dat")) if len(towns) == 1 and os.path.isfile(os.path.join(towns[0], "token.dat")) else []
        ck("D lifecycle B8: token.dat holds BOTH entries after the handoff: the GUEST entry is KEPT (home pid = the guest key, land = the guest's town) and the RESIDENT entry (host land) is present; "
           "membership.ini was written after it", len(tk) == 2 and tk[0][0] != tk[1][0])
        client.stop()
        client = None
        time.sleep(9.0)

        # ---------------- R: execute the logged command line (the relaunched client)
        toks = [t.strip('"') for t in cmdline.split()] if cmdline else []
        if len(toks) < 3 or toks[0] != "--connect":
            ck("R skipped: no usable relaunch command line", False)
            return
        client = L.ClientProcess(toks[1], extra_args=toks[2:], log_path=T.log_path("promo_rc_client_resident.log"), bin_dir=CLIENT_DIR, label="promorcc").start()
        mo = client.wait_for_log(r"-> READY", 170.0)
        ctext, htext = client.log_text(), host.log_text()
        ck("R the relaunched client fetched the town, resolved role = resident and bound the slot by PersonalID", "is a RESIDENT of town" in ctext and "--resident-by-pid: resident PersonalID matches slot 3" in ctext)
        ck("R it sent the RESIDENT claim (no guest claim) and reached READY", mo is not None and "playing a resident -- sent IDENTITY_EXT" in ctext and "[NET][GUEST] client: playing a guest" not in ctext)
        time.sleep(3.0)
        htext = host.log_text()
        ck("R host: the resident credential was confirmed and the promotion handoff entry removed", "promotion handoff removed" in htext)
        tk2 = gtk_entries(os.path.join(towns[0], "token.dat")) if len(towns) == 1 and os.path.isfile(os.path.join(towns[0], "token.dat")) else []
        ck("R lifecycle B8: after the first KNOWN resident login token.dat holds ONLY the resident entry (the obsolete guest entry was dropped)", len(tk2) == 1 and "obsolete guest token" in client.log_text())
        out = say(host, "residents")
        ck("R `residents`: slot 3 confirmed=yes connected=yes", re.search(r'slot 3: name="Roger\s*" credential=yes confirmed=yes armed=no connected=yes', out) is not None)
        client.stop()
        client = None
    finally:
        for p in (client,):
            if p is not None:
                p.stop()
        if host is not None:
            host.send_line("stop")
            host.wait_exit(60.0)
            host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11940)
    args = ap.parse_args()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: disposable pc\\build64\\bin_fixture4_<name> copies only", file=sys.stderr)
        return 2
    results = []
    run(args.port, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
