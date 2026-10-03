#!/usr/bin/env python3
"""test_d3_real_client.py - D3-2/D3-3 CLIENT half of the host-mirrored resident record (protocol v8), REAL processes.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation of the game itself, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot>` (a NEW process for every run)
The client's adoption / upload / quit-flush C code runs for real; every assertion below comes from the processes' own
'[NET][REC]' log lines plus byte-exact checks of the host record through a scripted FakeClient session (which receives the
host's PUSH_FULL of that resident). The only synthetic inputs are the TEST-ONLY, default-off client flags
  --d3-test-wallet-add N        (once, 3 s after the adopt: local wallet += N)
  --d3-test-wallet-add-late N   (second change 900 ms after the previous upload, inside the 2 s upload gap)
and the polite window close (WM_CLOSE) used for the graceful-quit path. NOT covered: the inventory overlay UI refresh after an
adopt (no UI automation), equipment/cloth hot-swap visuals, a real shop/pickup producing the wallet change.

Run ONLY on the disposable pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN=<absolute path>, ports 9800+). The fixture save is
snapshotted at start and restored before every real-client launch (so the client's LOCAL gci is always the original, stale one)
and at the end. At most 1 real client + 1 FakeClient at a time.

  R1  first join (host rev 0): HELLO -> MIGRATE_REQUEST -> MIGRATE_UPLOAD (client's local record, imported once) -> host rev 1
      -> PUSH_FULL -> client adopts rev 1 (wallet == its local GCI wallet), state SYNCED
  R2  a NEW client process (no last_*) for the same resident does NOT re-migrate (host rev > 0): it receives PUSH_FULL and
      adopts the HOST record (wallet 4321 written by a FakeClient upload between the runs, not the stale local GCI wallet)
  R3  upload path: --d3-test-wallet-add 100 -> client uploads -> host APPLIED rev bump -> a FakeClient session then receives a
      PUSH_FULL whose record == previous host record with only the wallet changed (byte exact)
  R4  graceful quit flush: --d3-test-wallet-add 50 --d3-test-wallet-add-late 25, window closed right after the late change:
      the client logs the quit flush, the host applies it, the pushed wallet includes the late change; no save is written by
      the client
Usage: python test_d3_real_client.py [--port 9800]
"""
import argparse
import os
import re
import shutil
import sys
import time

import net_spike_lib as L

SAVE_DIR_REL = "save"
HERE = os.path.dirname(os.path.abspath(__file__))
WALLET_B = 4321


def count(rx, text):
    return len(re.findall(rx, text))


class Rig:
    def __init__(self, port, results, save_dir, snap_dir):
        self.port, self.results, self.save_dir, self.snap_dir = port, results, save_dir, snap_dir
        self.check = lambda d, c: L.check(d, c, results)
        self.host = None
        self.n = 0

    def restore_save(self):
        shutil.rmtree(self.save_dir, ignore_errors=True)
        shutil.copytree(self.snap_dir, self.save_dir)

    def start_host(self):
        for i in range(3):
            h = L.HostProcess(port=self.port, extra_args=["--bootstrap-resident", "0"],
                              log_path=os.path.join(HERE, "d3_real_host_try%d.log" % i), bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
                self.host = h
                return True
            h.stop()
            time.sleep(2.0)
        return False

    def start_client(self, name, slot, extra=()):
        """A fresh client process; the fixture save is restored first so its local GCI is the original (stale) one."""
        self.restore_save()
        for i in range(3):
            self.n += 1
            c = L.ClientProcess("127.0.0.1:%d" % self.port, extra_args=["--bootstrap-resident", str(slot)] + list(extra),
                                log_path=os.path.join(HERE, "d3_real_%s_try%d.log" % (name, i)), bin_dir=L.GAME_BIN_DIR,
                                label=name).start()
            if c.boot_to_field(timeout=90.0, slot=slot):
                return c
            c.stop()
            time.sleep(2.0)
        return None

    def wait_host_peer_gone(self, off, timeout=14.0):
        return self.host.wait_for_log(r"\[NET\] host: peer \d+ disconnected", timeout, since_offset=off) is not None


def fake_session(rig, ip, slot, label):
    c = L.FakeClient(label, ip, rig.port, player=L.resident_player(slot))
    c.rec_resident_idx = slot
    c.connect_and_ready(quiet=True)
    return c


def release(c):
    try:
        if c.state in (c.STATE_CONNECTED, c.STATE_PENDING):
            c.disconnect()
    except Exception:  # noqa: BLE001
        pass
    c.close()
    L.pump_sleep(0.8)


def run(args, results, ip, snap_gci, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    r1 = residents[0]
    gci = L.record_from_gci(snap_gci, r1)
    w_gci = L.record_get_u32(gci, L.REC_OFF_WALLET)
    check("fixture resident %d: local GCI wallet %d differs from the test wallets" % (r1, w_gci), w_gci not in (WALLET_B, WALLET_B + 100))
    if not rig.start_host():
        check("host reached genuine field-ready state", False)
        return
    check("host reached genuine field-ready state", True)
    host = rig.host
    L.resolve_host_town(ip, args.port)

    # ---------------- R1: first join -> MIGRATE -> adopt ----------------
    cl = rig.start_client("r1", r1)
    check("R1 real client booted into the field and connected (snapshot applied)", cl is not None)
    if cl is None:
        return
    m = cl.wait_for_log(r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=(\d+) kind=FULL", 60.0)
    ct = cl.log_text()
    check("R1 client adopted the host record (log 'adopted rev=')", m is not None)
    epoch1 = int(m.group(2)) if m else 0
    check("R1 client log: HELLO sent without have_last (new process)", "HELLO sent (have_last=0" in ct)
    check("R1 client log: host asked for a MIGRATE upload; MIGRATE xfer 1 started and APPLIED (rev 1); then PUSH_FULL rev 1",
          "host asks for a MIGRATE upload" in ct and "MIGRATE upload xfer 1 started" in ct
          and re.search(r"MIGRATE upload xfer 1 APPLIED by the host \(rev 1\)", ct)
          and re.search(r"push FULL xfer 1 incoming \(epoch %d rev 1\)" % epoch1, ct))
    check("R1 client adopted exactly rev 1 of the pushed lineage, wallet == its own local GCI wallet (%d)" % w_gci,
          m is not None and m.group(1) == "1"
          and re.search(r"adopted rev=1 epoch=%d kind=FULL xfer=1 \(wallet=%d " % (epoch1, w_gci), ct) is not None)
    check("R1 exactly one adoption and no violation/BAD DIGEST/ADOPT_FAILED/refusal in the client log",
          count(r"client: adopted rev=", ct) == 1 and "protocol violation" not in ct and "BAD DIGEST" not in ct
          and "ADOPT_FAILED" not in ct and "REFUSED record transfer" not in ct)
    ht = host.log_text()
    check("R1 host log: MIGRATE_REQUEST sent once, MIGRATE import applied -> rev 1, push FULL queued for rev 1",
          count(r"never synced \(rev 0\): MIGRATE_REQUEST sent", ht) == 1
          and re.search(r"MIGRATE xfer 1 APPLIED \(resident %d epoch %d rev 1\)" % (r1, epoch1), ht)
          and re.search(r"push FULL queued \(resident %d epoch %d rev 1 " % (r1, epoch1), ht))
    off = len(host.log_text())
    cl.stop()
    check("R1 host noticed the client leaving", rig.wait_host_peer_gone(off - 1))

    # ---------------- R2: change the host record, a new client process adopts the HOST record ----------------
    f = fake_session(rig, ip, r1, "F1")
    push = f.rec_pushes[-1]
    # The client's IN-MEMORY record is not byte-identical to the GCI file (the game normalises/changes fields between load and
    # play), so "the migrated record" is what the client reported: its HELLO local_digest == the digest of the host's record.
    ld = re.search(r"HELLO sent \(have_last=0 .*local digest 0x([0-9A-Fa-f]+)\)", ct)
    check("R2 FakeClient (new session) got PUSH_FULL rev 1 == exactly the record the real client migrated (digest == its HELLO local digest)",
          push["rev"] == 1 and push["epoch"] == epoch1 and ld is not None and L.fnv1a32(push["data"]) == int(ld.group(1), 16))
    check("R1 first-join adopt did not report discarded local edits (nothing was unsynced)", "discarded local client-owned edits" not in ct)
    L.pump_sleep(1.7)
    rec_b = L.record_set_u32(push["data"], L.REC_OFF_WALLET, WALLET_B)
    x = f.upload_record(rec_b, base=(epoch1, 1))
    ack = f.wait_record_ack(xfer_id=x)
    check("R2 FakeClient upload of the changed wallet (%d) APPLIED rev 2" % WALLET_B,
          ack is not None and ack.status == 0 and ack.rev == 2)
    release(f)
    host_view = rec_b
    mig_before = count(r"MIGRATE_REQUEST sent", host.log_text())
    cl = rig.start_client("r2", r1)
    check("R2 second real client process booted", cl is not None)
    if cl is None:
        return
    m = cl.wait_for_log(r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=(\d+) kind=FULL xfer=\d+ \(wallet=(\d+)", 60.0)
    ct = cl.log_text()
    check("R2 client adopted rev 2 with the HOST wallet %d (not its stale local GCI wallet %d)" % (WALLET_B, w_gci),
          m is not None and m.group(1) == "2" and int(m.group(3)) == WALLET_B)
    check("R2 NO re-migration: client log has no MIGRATE request/upload; host log has no new MIGRATE_REQUEST",
          "host asks for a MIGRATE upload" not in ct and "MIGRATE upload" not in ct
          and count(r"MIGRATE_REQUEST sent", host.log_text()) == mig_before)
    check("R2 client log: HELLO have_last=0 (new process never claims a continuation), push FULL received & staged, one adoption",
          "HELLO sent (have_last=0" in ct and "push received & staged" in ct and count(r"client: adopted rev=", ct) == 1)
    off = len(host.log_text())
    cl.stop()
    rig.wait_host_peer_gone(off - 1)

    # ---------------- R3: upload path ----------------
    cl = rig.start_client("r3", r1, ["--d3-test-wallet-add", "100"])
    check("R3 real client (test hook armed) booted", cl is not None)
    if cl is None:
        return
    off_h = len(host.log_text())
    m = cl.wait_for_log(r"\[NET\]\[REC\]\[TEST-ONLY\] --d3-test-wallet-add 100: local wallet (\d+) -> (\d+)", 60.0)
    check("R3 client adopted rev 2 first, then the test hook changed the wallet %d -> %d" % (WALLET_B, WALLET_B + 100),
          m is not None and int(m.group(1)) == WALLET_B and int(m.group(2)) == WALLET_B + 100
          and "adopted rev=2" in cl.log_text())
    m2 = cl.wait_for_log(r"\[NET\]\[REC\] client: upload xfer (\d+) APPLIED \(epoch (\d+) rev (\d+)\)", 20.0)
    check("R3 client upload APPLIED by the host -> rev 3, epoch unchanged",
          m2 is not None and m2.group(3) == "3" and int(m2.group(2)) == epoch1)
    check("R3 host log: upload from resident %d APPLIED rev 3" % r1,
          re.search(r"upload xfer \d+ APPLIED \(resident %d epoch %d rev 3\)" % (r1, epoch1), host.log_text()[off_h:]) is not None)
    L.pump_sleep(3.0)
    check("R3 no spurious second upload (the clean record stays quiet): exactly one UPLOAD started",
          count(r"UPLOAD upload xfer \d+ started", cl.log_text()) == 1)
    cm = cl.wait_for_log(r"dirty-check cost .* averages ([0-9.]+) us over 20 samples", 20.0)
    check("R3 dirty-check cost line printed and trivial (< 500 us per 500 ms check)",
          cm is not None and float(cm.group(1)) < 500.0)
    off = len(host.log_text())
    cl.stop()
    rig.wait_host_peer_gone(off - 1)
    f = fake_session(rig, ip, r1, "F2")
    push = f.rec_pushes[-1]
    exp = L.record_set_u32(host_view, L.REC_OFF_WALLET, WALLET_B + 100)
    check("R3 host record after the upload (new FakeClient session's PUSH_FULL): rev 3, wallet %d, otherwise unchanged (byte exact)"
          % (WALLET_B + 100), push["rev"] == 3 and L.record_get_u32(push["data"], L.REC_OFF_WALLET) == WALLET_B + 100
          and push["data"] == exp)
    host_view = push["data"]
    release(f)

    # ---------------- R4: graceful quit flush ----------------
    cl = rig.start_client("r4", r1, ["--d3-test-wallet-add", "50", "--d3-test-wallet-add-late", "25"])
    check("R4 real client (flush hooks armed) booted", cl is not None)
    if cl is None:
        return
    off_h = len(host.log_text())
    m = cl.wait_for_log(r"--d3-test-wallet-add-late 25: local wallet (\d+) -> (\d+)", 90.0)
    check("R4 late change happened after the first upload (wallet %d -> %d)" % (WALLET_B + 150, WALLET_B + 175),
          m is not None and int(m.group(1)) == WALLET_B + 150 and int(m.group(2)) == WALLET_B + 175)
    if m is not None:
        from game_input import GameWindow  # Windows-only helper
        win = GameWindow(cl.proc.pid)
        t0 = time.monotonic()
        while not win.ready() and time.monotonic() - t0 < 5.0:
            time.sleep(0.1)
        check("R4 window close (WM_CLOSE) posted", win.close())
        deadline = time.monotonic() + 30.0
        while cl.alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        check("R4 client exited by itself after the graceful close (exit code 0)", not cl.alive() and cl.proc.returncode == 0)
    ct = cl.log_text()
    mm = re.search(r"quit flush done: record APPLIED by the host \(rev (\d+)\) in (\d+) ms", ct)
    check("R4 client log: quit flush uploaded the dirty record and the host APPLIED it (rev 5)",
          "quit flush: uploading the dirty record" in ct and mm is not None and mm.group(1) == "5")
    check("R4 flush took < 1500 ms", mm is not None and int(mm.group(2)) < 1500)
    check("R4 the client wrote NO save (final shutdown save SKIPPED, no authoritative save line)",
          "final shutdown save SKIPPED" in ct and "final authoritative save" not in ct)
    check("R4 host log: flush upload APPLIED rev 5 (rev 4 was the first test upload)",
          re.search(r"upload xfer \d+ APPLIED \(resident %d epoch %d rev 5\)" % (r1, epoch1), host.log_text()[off_h:]) is not None)
    cl.stop()
    L.pump_sleep(1.0)
    f = fake_session(rig, ip, r1, "F3")
    push = f.rec_pushes[-1]
    exp = L.record_set_u32(host_view, L.REC_OFF_WALLET, WALLET_B + 175)
    check("R4 host record: rev 5, wallet %d (includes the change only the flush could send), otherwise unchanged" % (WALLET_B + 175),
          push["rev"] == 5 and push["data"] == exp)
    release(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9800)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "d3_real_client_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    rig = Rig(args.port, results, save_dir, snap_dir)
    try:
        run(args, results, ip, snap_gci, rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
