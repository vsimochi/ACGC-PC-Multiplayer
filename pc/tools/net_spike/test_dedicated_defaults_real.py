#!/usr/bin/env python3
"""test_dedicated_defaults_real.py - defaults of a `--host P --dedicated` process: furniture (house) sync ON, town serving AUTO = ON/sanitized, personal sync (auto) follows town serve.

TIER: REAL dedicated host processes (stdin-pipe console) + scripted FakeClients (TOWN_FETCH, HOST_CONFIG) + ONE REAL client process, on DISPOSABLE copies of
pc\\build64\\bin_fixture4 (bin_fixture4_dfl = host, bin_fixture4_dflc = client with an EMPTY save dir; the freshly built exe is copied into THEM only). Nothing else is touched.
  1  `--dedicated` alone: startup line 'house sync ON (default ...), town serve ON (sanitized) (default ...)'; TOWN_FETCH -> STREAM + SANITIZED flag; HOST_CONFIG byte 1 has bit0 (house)
     and bit1 (personal); a REAL guest client (`--character <uuid> --town-fetch --online-ui`) fetches the town and reaches READY, no 'town_serve is off'
  2  `--town-serve off`: REFUSED (explicit choice respected), house sync still ON (bit0), personal sync auto follows town serve (bit1 absent)
  3  `--no-house-sync`: HOST_CONFIG bit0 absent, startup line 'house sync off', town serve still ON (STREAM)
  4  settings.ini: a FRESH copy (no settings.ini) gets one written by the game containing `town_serve = auto` (not 0) and serves (auto -> ON); `town_serve = off` (no CLI) -> REFUSED;
     explicit `town_serve = auto` -> STREAM
  5  NON-dedicated `--host P --bootstrap-resident 0` with no flags: TOWN_FETCH REFUSED, HOST_CONFIG byte 1 bit0 absent (unchanged)
Usage: python test_dedicated_defaults_real.py [--port 12010]
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

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_dfl")
CLIENT_DIR = os.path.join(T.BUILD64, "bin_fixture4_dflc")
if __name__ == "__main__":
    T.make_fixture("bin_fixture4_dfl")
    T.make_fixture("bin_fixture4_dflc", empty_save=True)

import net_spike_lib as L  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
EXE = os.path.join(CLIENT_DIR, "AnimalCrossing.exe")
INI = os.path.join(HOST_DIR, "settings.ini")
IP = "127.0.0.1"
HOUSE, PERSONAL = L.PC_NETGAME_HOSTCFG_FLAG_HOUSE_SYNC, L.HOSTCFG_FLAG_PERSONAL_SYNC
LINE_RX = r"\[PC\] dedicated host services: house sync (ON|off) \(([^)]*)\), town serve (off|ON \(sanitized\)|ON \(FULL, unsanitized\)) \(([^)]*)\)"


class Rig:
    def __init__(self, port0, results):
        self.port0, self.n = port0, 0
        self.ck = lambda d, c: L.check(d, bool(c), results)

    def start(self, tag, extra, dedicated=True):
        self.n += 1
        port = self.port0 + self.n
        for i in range(2):
            if dedicated:
                h = L.HostProcess(port=port, extra_args=["--dedicated"] + list(extra), log_path=T.log_path("dfl_%s_%d.log" % (tag, i)), bin_dir=HOST_DIR,
                                  stdin_pipe=True, verbose=False, new_group=True).start()
                ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
            else:
                h = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"] + list(extra), log_path=T.log_path("dfl_%s_%d.log" % (tag, i)), bin_dir=HOST_DIR).start()
                ok = h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0)
            if ok:
                L.resolve_host_town(IP, port)
                return h, port
            h.stop()
            time.sleep(2.0)
        return None, port

    @staticmethod
    def stop(h, dedicated=True):
        if h is None:
            return
        if dedicated:
            h.send_line("stop")
            if h.wait_exit(40.0) is None:
                h.stop()
        else:
            h.stop()
        time.sleep(1.0)

    def fetch_status(self, port):
        L.pump_sleep(0.3)
        c = L.FakeClient("dfl-fetch", IP, port, player=None)
        try:
            c.connect(timeout=30.0)
            info = L.town_fetch_start(c)
            if info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_STREAM:
                try:
                    c.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            return info
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            L.pump_sleep(0.5)

    def hostcfg_byte1(self, port):
        """HOST_CONFIG byte 1 as pushed to a READY resident-1 FakeClient (None when none arrived)."""
        c = L.FakeClient("dfl-cfg", IP, port, player=L.resident_player(1), record_hello=True, record_wait=False, wait_snapshot=False)
        c.rec_resident_idx = 1
        try:
            c.connect_and_ready(timeout=8.0, quiet=True)
            c.wait_ts_state(L.PC_NETGAME_TS_HOSTCFG, timeout=5.0)
            hc = c.ts_latest(L.PC_NETGAME_TS_HOSTCFG)
            return hc[1][1] if hc is not None and len(hc[1]) == 8 else None
        except Exception as e:  # noqa: BLE001
            print("[hostcfg_byte1] %r" % (e,))
            return None
        finally:
            try:
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            L.pump_sleep(0.8)


def set_ini_town_serve(value):
    """Rewrite the town_serve line of the disposable host copy's settings.ini (append a [Network] block if the file has none)."""
    txt = open(INI, "r", errors="replace").read()
    if re.search(r"(?m)^town_serve\s*=", txt):
        txt = re.sub(r"(?m)^town_serve\s*=.*$", "town_serve = " + value, txt)
    else:
        txt = txt.rstrip() + "\n\n[Network]\ntown_serve = %s\n" % value
    open(INI, "w", newline="").write(txt)


def run(port, results):
    L.require_test_bin_dir()
    rig = Rig(port, results)
    ck = rig.ck
    client = None
    host = None
    try:
        # ------------------------------------------------------------- case 1
        host, p = rig.start("c1", [])
        ck("1 dedicated host (no --house-sync / --town-serve) booted", host is not None)
        if host is not None:
            t = host.log_text()
            m = re.search(LINE_RX, t)
            ck("1 startup line: house sync ON (default for every host), town serve ON (sanitized) (default for --dedicated)",
               m is not None and m.group(1) == "ON" and m.group(2).startswith("default") and m.group(3) == "ON (sanitized)" and m.group(4).startswith("default"))
            info = rig.fetch_status(p)
            ck("1 TOWN_FETCH -> STREAM with the SANITIZED flag (status=%s flags=%s)" % (None if info is None else info.status, None if info is None else info.flags),
               info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_STREAM and (info.flags & L.PC_NETGAME_TOWN_INFO_FLAG_SANITIZED) == L.PC_NETGAME_TOWN_INFO_FLAG_SANITIZED)
            b1 = rig.hostcfg_byte1(p)
            ck("1 HOST_CONFIG byte 1 = %s has bit0 (house sync) and bit1 (personal sync)" % b1, b1 is not None and (b1 & HOUSE) and (b1 & PERSONAL))
            # real guest client
            mp = os.path.join(CLIENT_DIR, "save", "mp")
            os.makedirs(mp, exist_ok=True)
            with open(os.path.join(mp, "guest_roger.ini"), "w", newline="") as f:
                f.write("name = Roger\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x1234\nland_id = 0x4321\n")
            with open(os.path.join(mp, "servers.ini"), "w", newline="") as f:
                f.write("[server]\nname = dflsrv\naddress = 127.0.0.1\nport = %d\n" % p)
            r = subprocess.run([EXE, "--character-import-profile", "roger"], cwd=CLIENT_DIR, capture_output=True, text=True, timeout=60)
            mm = re.search(r"as character ([0-9a-f]{32})", r.stdout)
            ck("1 setup: legacy profile imported as a store character", r.returncode == 0 and mm is not None)
            if mm is not None:
                time.sleep(7.0)
                client = L.ClientProcess("%s:%d" % (IP, p), extra_args=["--character", mm.group(1), "--town-fetch", "--online-ui"], log_path=T.log_path("dfl_client.log"),
                                         bin_dir=CLIENT_DIR, label="dflc").start()
                mo = client.wait_for_log(r"-> READY", 170.0)
                ct = client.log_text()
                ck("1 REAL guest client fetched the town (fetch: installed) and reached READY", mo is not None and re.search(r"fetch: installed town ([0-9a-f_]{30})", ct) is not None)
                ck("1 ... with no 'town_serve is off' message and no REFUSED", "town_serve is off" not in ct and "REFUSED" not in ct and "refused the town transfer" not in ct)
                ck("1 ... the host logged a sanitized STREAM to it", re.search(r"STREAM 467008 bytes .* SANITIZED", host.log_text()) is not None)
                client.stop()
                client = None
        rig.stop(host)
        host = None

        # ------------------------------------------------------------- case 2
        host, p = rig.start("c2", ["--town-serve", "off"])
        ck("2 dedicated host --town-serve off booted", host is not None)
        if host is not None:
            m = re.search(LINE_RX, host.log_text())
            ck("2 startup line: house sync ON, town serve off (explicit setting)", m is not None and m.group(1) == "ON" and m.group(3) == "off" and m.group(4) == "explicit setting")
            info = rig.fetch_status(p)
            ck("2 TOWN_FETCH -> REFUSED (explicit off respected; status=%s)" % (None if info is None else info.status), info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_REFUSED)
            b1 = rig.hostcfg_byte1(p)
            ck("2 HOST_CONFIG byte 1 = %s: house sync bit0 set, personal sync bit1 ABSENT (auto follows town serve)" % b1, b1 is not None and (b1 & HOUSE) and not (b1 & PERSONAL))
        rig.stop(host)
        host = None

        # ------------------------------------------------------------- case 3
        host, p = rig.start("c3", ["--no-house-sync"])
        ck("3 dedicated host --no-house-sync booted", host is not None)
        if host is not None:
            m = re.search(LINE_RX, host.log_text())
            ck("3 startup line: house sync off (explicit option), town serve ON (sanitized)", m is not None and m.group(1) == "off" and m.group(2) == "explicit option" and m.group(3) == "ON (sanitized)")
            b1 = rig.hostcfg_byte1(p)
            ck("3 HOST_CONFIG byte 1 = %s: house sync bit0 ABSENT (personal bit1 still set: town serve on)" % b1, b1 is not None and not (b1 & HOUSE) and (b1 & PERSONAL))
            info = rig.fetch_status(p)
            ck("3 TOWN_FETCH -> STREAM (town serve still ON; status=%s)" % (None if info is None else info.status), info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_STREAM)
        rig.stop(host)
        host = None

        # ------------------------------------------------------------- case 4
        if os.path.isfile(INI):
            os.remove(INI)  # the disposable copy only: a FRESH copy, the game writes a default settings.ini
        host, p = rig.start("c4a", [])
        ck("4a dedicated host on a copy WITHOUT settings.ini booted", host is not None)
        if host is not None:
            txt = open(INI, "r", errors="replace").read() if os.path.isfile(INI) else ""
            ck("4a the game wrote a default settings.ini containing 'town_serve = auto' (not 0)", re.search(r"(?m)^town_serve\s*=\s*auto\s*$", txt) is not None and not re.search(r"(?m)^town_serve\s*=\s*0", txt))
            info = rig.fetch_status(p)
            ck("4a fresh settings.ini (town_serve = auto) on a dedicated host -> STREAM", info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_STREAM)
        rig.stop(host)
        host = None
        set_ini_town_serve("off")
        host, p = rig.start("c4b", [])
        ck("4b dedicated host with settings.ini town_serve = off (no CLI override) booted", host is not None)
        if host is not None:
            m = re.search(LINE_RX, host.log_text())
            ck("4b startup line: town serve off (explicit setting)", m is not None and m.group(3) == "off" and m.group(4) == "explicit setting")
            info = rig.fetch_status(p)
            ck("4b TOWN_FETCH -> REFUSED (status=%s)" % (None if info is None else info.status), info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_REFUSED)
        rig.stop(host)
        host = None
        set_ini_town_serve("auto")
        host, p = rig.start("c4c", [])
        ck("4c dedicated host with settings.ini town_serve = auto (explicit text) booted", host is not None)
        if host is not None:
            info = rig.fetch_status(p)
            ck("4c TOWN_FETCH -> STREAM (status=%s)" % (None if info is None else info.status), info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_STREAM)
        rig.stop(host)
        host = None

        # ------------------------------------------------------------- case 5
        set_ini_town_serve("auto")  # the default text in the settings.ini: auto must stay OFF for a non-dedicated host
        host, p = rig.start("c5", [], dedicated=False)
        ck("5 NON-dedicated host (no flags) booted", host is not None)
        if host is not None:
            ck("5 no 'dedicated host services' line", "dedicated host services" not in host.log_text())
            info = rig.fetch_status(p)
            ck("5 TOWN_FETCH -> REFUSED (status=%s)" % (None if info is None else info.status), info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_REFUSED)
            b1 = rig.hostcfg_byte1(p)
            ck("5 HOST_CONFIG byte 1 = %s: bit0 (house sync) PRESENT (Patch 5: house sync is the default of every host, dedicated or not), personal sync bit1 absent (town serving stays a --dedicated default)" % b1,
               b1 is not None and (b1 & HOUSE) and not (b1 & PERSONAL))
        rig.stop(host, dedicated=False)
        host = None
    finally:
        if client is not None:
            client.stop()
        if host is not None:
            host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12010)
    args = ap.parse_args()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: disposable pc\\build64\\bin_fixture4_<name> copies only", file=sys.stderr)
        return 2
    results = []
    run(args.port, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
