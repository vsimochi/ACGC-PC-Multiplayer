#!/usr/bin/env python3
"""test_museum_bits_protocol.py - Patch 6: the museum-completion letter bits (mPr_FLAG_MUSEUM_COMP_HANDBILL_SCHEDULED|RECEIVED = 0xC0 of state_flags, Private_c 0x2348)
are HOST-owned bits inside a client-owned u32. PROTOCOL test against a REAL host process (AC_TEST_HOOKS=1 AC_TEST_MUSEUM_BITS=<slot>,C0 sets them at world-ready, what the
day-change museum code does) with a scripted FakeClient on a DISPOSABLE bin_fixture4_* copy (NET_SPIKE_GAME_BIN).
  M1 the client's first FULL push carries the host-set bits
  M2 an upload that CLEARS the bits (a client built before the host set them / a hostile client) while toggling a client-owned bit is merged: the wallet edit lands, the bits stay
  M3 a NEW session (fresh push) still carries the bits
Usage: python test_museum_bits_protocol.py [--port 11700]"""
import argparse
import os
import shutil
import sys

os.environ["AC_TEST_HOOKS"] = "1"
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402

SF = 0x2348 + 3  # BE byte holding bits 0..7 of state_flags
MASK = 0xC0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11700)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4_* copy only", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    snap = os.path.join(os.environ.get("TEMP", "."), "museum_bits_snap_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host_slot = L.TEST_HOST_RESIDENT
    r1 = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot][0]
    os.environ["AC_TEST_MUSEUM_BITS"] = "%d,%X" % (r1, MASK)
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(host_slot)], log_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "museum_bits_host.log")).start()
    clients = []
    try:
        ok = host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=host_slot)
        ck("host reached field-ready state", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town(ip, args.port)
        ck("the host test hook set the bits", "[NET][REC][TEST-ONLY] host: resident %d state_flags |= 0x%X" % (r1, MASK) in host.log_text())
        a = L.FakeClient("A", ip, args.port, player=L.resident_player(r1)); a.rec_resident_idx = r1; clients.append(a)
        a.connect_and_ready(quiet=True)
        ck("A synced", a.rec_synced and a.rec_last is not None)
        p = a.rec_pushes[-1]["data"]
        ck("M1 the first push carries the host-set museum bits (0x%02X)" % p[SF], (p[SF] & MASK) == MASK)
        up = bytearray(p)
        up[SF] &= ~MASK & 0xFF
        up[SF] ^= 0x02  # an ordinary client-owned bit toggles too (the real edit that makes the upload valid)
        want = up[SF] & 0x02
        L.pump_sleep(6.0)  # past the upload rate limit that follows the migrate
        x = a.upload_record(bytes(up))
        ack = a.wait_record_ack(x, timeout=5.0)
        ck("M2 the upload with cleared bits was answered (status %s)" % (getattr(ack, "status", None),), ack is not None)
        L.pump_sleep(1.0)
        a.disconnect(); a.close(); clients.remove(a)
        L.pump_sleep(0.8)
        b = L.FakeClient("B", ip, args.port, player=L.resident_player(r1)); b.rec_resident_idx = r1; clients.append(b)
        b.connect_and_ready(quiet=True)
        q = b.rec_pushes[-1]["data"]
        ck("M2 the upload was APPLIED (status %s) and the client-owned bit 0x02 landed (0x%02X)" % (ack.status, q[SF]), (q[SF] & 0x02) == want and q[SF] != p[SF])
        ck("M3 the host-owned museum bits survived the upload that cleared them (0x%02X)" % q[SF], (q[SF] & MASK) == MASK)
    finally:
        for c in clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        host.stop()
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
