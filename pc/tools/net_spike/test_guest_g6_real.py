#!/usr/bin/env python3
"""test_guest_g6_real.py - GUESTS G6.1 / G6.3(d) with a REAL host process and a REAL client process.

TIER: REAL host (`AnimalCrossing.exe --host <port> --bootstrap-resident 0 --max-guests 1`) + one scripted FakeClient guest that fills the guest cap + REAL game clients
(`--connect 127.0.0.1:<port> --guest`), all from the DISPOSABLE fixture pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN; port 12600). The fixture's save dir is snapshotted
and restored (CloneSaveGuard + the library guard) and save/mp is discarded afterwards. NO visual verification: the on-screen draw of the message (pc_net_notice_draw) is
NOT observed here, only the log / stderr output of the real client process and its state.

  A  (G6.1) the cap is full (a scripted guest is bound; `--max-guests 1`): a REAL client is refused (REJECT SERVER_FULL, reason 2): its log (stdout + stderr of the real
     process) carries the readable '[NET][JOIN] CANNOT JOIN: The host refused this guest: server full / guest limit reached / all resident slots taken ...' line with the
     numeric '(reason 2)' kept, the host logs the cap line, the client is NOT hung (alive, burning CPU, still logging, no crash) and created no guest entry.
  B  (G6.3 d) a guest profile whose home town / land id equal the HOST's town: the real client refuses to arrive ('REFUSED: your guest home town ... is the same as this town ...
     Edit land_id in save/mp/guest.ini'), exits with code 2, never connected, and the host saw no identity from it.
Usage: python test_guest_g6_real.py [--port 12600]
"""
import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402
import test_guest_protocol as TG  # noqa: E402
import test_guest_g3_real as G3  # noqa: E402
import test_dedicated_runtime as DR  # noqa: E402

JOIN_RX = r"\[NET\]\[JOIN\] CANNOT JOIN: The host refused this guest: server full / guest limit reached / all resident slots taken[^\n]*\(reason 2\)"


def client(port, tag, log_dir):
    return L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--guest"], log_path=os.path.join(log_dir, "guest_g6_real_%s.log" % tag), bin_dir=L.GAME_BIN_DIR, label=tag).start()


def write_ini(text):
    ini = os.path.join(L.GAME_BIN_DIR, "save", "mp", "guest.ini")
    os.makedirs(os.path.dirname(ini), exist_ok=True)
    with open(ini, "wb") as f:
        f.write(text.encode("ascii"))


def wait_pumping(proc, rx, timeout):
    """Waits for `rx` in the real process' log while PUMPING the scripted FakeClients (a silent peer is timed out by the host, which would free the guest cap)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        m = re.search(rx, proc.log_text())
        if m:
            return m
        L.pump_sleep(1.0)
    return None


def run(args, results):
    ck = lambda d, c: L.check(d, bool(c), results)
    ip = "127.0.0.1"
    host = None
    c1 = c2 = fake = None
    try:
        for i in range(3):
            host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "--max-guests", "1"], log_path=os.path.join(args.log_dir, "guest_g6_real_host_try%d.log" % i),
                                 bin_dir=L.GAME_BIN_DIR).start()
            if host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=0):
                break
            host.stop()
            host = None
            time.sleep(2.0)
        ck("host reached genuine field-ready state (cap: --max-guests 1)", host is not None)
        if host is None:
            return
        town = L.resolve_host_town(ip, args.port)
        fake = L.FakeClient("CAPFILL", ip, args.port, guest=TG.gid(0), bind_ip="127.0.77.2")
        fake.connect_and_ready(quiet=True)
        ck("a scripted guest is bound (the cap of 1 is full)", fake.is_connected() and fake.token_msgs and "bound to GUEST slot 0" in host.log_text())

        # ---------------- A: a REAL client is refused with a readable message
        write_ini(G3.ini_text("Quill", 1, 5, "Hometwn", G3.PID, G3.LID))
        c1 = client(args.port, "a", args.log_dir)
        m = wait_pumping(c1, JOIN_RX, 150.0)
        t = c1.log_text()
        print("--- real client (refused) log tail ---")
        print("\n".join(t.splitlines()[-12:]))
        ck("A the real client printed the readable refusal: 'The host refused this guest: server full / guest limit reached / all resident slots taken ... (reason 2)'", m is not None)
        ck("A the numeric reason is kept in the log ('host rejected the connection (reason=2 ...') AND the message went to BOTH stdout and stderr ([NET][JOIN] CANNOT JOIN appears twice in the "
           "combined log)", "host rejected the connection (reason=2" in t and len(re.findall(r"\[NET\]\[JOIN\] CANNOT JOIN", t)) >= 2)
        ck("A the text names the other causes and the token recovery (no token held: guest-reset-token)", "guest table is full" in t and "guest-reset-token" in t)
        ht = host.log_text()
        ck("A the host refused it with the cap line and minted nothing for it", "guest limit reached (1 of max_guests=1 guests are connected)" in ht and ht.count("guests.dat written (new guest token minted") == 1)
        L.pump_sleep(3.0)
        cpu0, n0 = DR.cpu_seconds(c1.proc.pid), len(c1.log_text())
        L.pump_sleep(6.0)
        cpu1, n1 = DR.cpu_seconds(c1.proc.pid), len(c1.log_text())
        ck("A the refused client is NOT hung: still alive and burning CPU (%.2f s -> %.2f s) and no crash line" % (cpu0 or 0, cpu1 or 0), c1.alive() and cpu0 is not None and cpu1 is not None and cpu1 - cpu0 > 0.3
           and "Assertion" not in c1.log_text() and "*** INTERNAL" not in c1.log_text())
        ck("A the refused client went back to single player (networking shut down, 'shutting down networking (was client)')", "shutting down networking (was client)" in c1.log_text())
        pf = TG.parse_guests(TG.guests_path())
        ck("A guests.dat holds ONLY the scripted guest (the refused real client created no entry)", sum(1 for e in pf["e"] if e["present"]) == 1)
        c1.stop()
        c1 = None
        time.sleep(2.0)

        # ---------------- B: home town == the host's town: the client refuses to arrive
        name = bytes(town.land_name).rstrip(b" \x00")
        try:
            home = name.decode("ascii")
            ok_name = bool(re.fullmatch(r"[A-Za-z0-9 .'-]{1,8}", home))
        except UnicodeDecodeError:
            home, ok_name = "", False
        ck("B the fixture town name is usable as a guest home_town (%r)" % name, ok_name)
        if not ok_name:
            return
        write_ini(G3.ini_text("Quill", 1, 5, home, G3.PID, town.land_id))
        off = len(host.log_text())
        c2 = client(args.port, "b", args.log_dir)
        t_end = time.monotonic() + 180.0
        while c2.alive() and time.monotonic() < t_end:
            L.pump_sleep(1.0)
        code = c2.exit_code()
        t2 = c2.log_text()
        print("--- real client (home land == town) log tail ---")
        print("\n".join(t2.splitlines()[-8:]))
        ck("B the real client REFUSED to arrive and exited with code 2 (got %s)" % code, code == 2)
        ck("B the message names the home town, says it is the same as this town and tells the user to edit land_id in save/mp/guest.ini (a new guest)",
           re.search(r"REFUSED: your guest home town '%s *' \(land id 0x%04X\) is the same as this town" % (re.escape(home), town.land_id), t2) is not None
           and "Edit land_id in save/mp/guest.ini" in t2 and "NEW guest" in t2)
        ck("B it never bound or connected: no 'bound as a foreigner', no guest client line, the host bound nobody new", "bound as a foreigner" not in t2 and "[NET][GUEST] client:" not in t2
           and "bound to GUEST" not in host.log_text()[off:] and "identity OK" not in host.log_text()[off:] and "rejecting" not in host.log_text()[off:])
        ck("B the host is alive and holds still only the one scripted guest", host.alive() and sum(1 for e in TG.parse_guests(TG.guests_path())["e"] if e["present"]) == 1)
    finally:
        for c in (c1, c2):
            if c is not None:
                c.stop()
        if fake is not None:
            fake.close()
        if host is not None:
            host.stop()
        time.sleep(2.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12600)
    ap.add_argument("--log-dir", default=HERE)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test mutates the game save; run it on pc\\build64\\bin_fixture4 only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    with L.CloneSaveGuard():
        try:
            run(args, results)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            L.check("test aborted by an exception: %s" % e, False, results)
    L.discard_fixture_guest_mp(L.GAME_BIN_DIR)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
