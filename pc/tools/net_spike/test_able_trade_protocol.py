#!/usr/bin/env python3
"""test_able_trade_protocol.py - worker B: Able Sisters multiplayer (guests + villager outfits). PROTOCOL TESTED against a REAL host process, scripted FakeClients.

  O1 every READY client gets VILLAGER_OUTFIT (msg 90, 128 B, host -> client) at join: count 15, seq >= 1, the outfit of the villager the host test hook dressed in shop
     design 2 (AC_TEST_OUTFIT_SEED=2: cloth RSV_CLOTH 0xFE20 + cloth_original_id 2)
  O2 a resident's DESIGN page write that changes design 2 is APPLIED and the HOST runs the vanilla trend reset (that villager's shirt goes back to its default, org 0xFF),
     mirrored by a NEW VILLAGER_OUTFIT (higher seq) to every client
  G1 a GUEST is pushed all 23 pages at join (it plays a never-persisted copy of THIS town)
  G2 a guest's DESIGN write is APPLIED (vanilla visitors trade at Able Sisters) and mirrored to a resident; a guest's NOTICE write is refused BAD (residents only)
  G3 a guest's write built on a stale revision is refused STALE with the canonical copy (same rules as residents)
Run on a DISPOSABLE fixture (NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_wlB_<name>>); the host save is restored at the end.
Usage: python test_able_trade_protocol.py [--port 15410]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_town_pages_protocol as PG  # noqa: E402

MSG_OUTFIT = 90
OUT_HDR = "<BBHI"
OUT_ENT = "<HHBBH"
RSV_CLOTH = 0xFE20


def is_outfit(m):
    return m.channel == L.CH_RELIABLE and len(m.payload) == 128 and m.payload[0] == MSG_OUTFIT


def outfits(c, since=None):
    res = []
    for m in c.inbox.peek_all(is_outfit, since=since):
        t, cnt, rsv, seq = struct.unpack_from(OUT_HDR, m.payload)
        ents = [struct.unpack_from(OUT_ENT, m.payload, 8 + 8 * i) for i in range(15)]
        res.append((seq, cnt, ents))
    return res


def wait_outfit(c, pred, timeout=8.0, since=None):
    end = time.time() + timeout
    while time.time() < end:
        for o in outfits(c, since):
            if pred(o):
                return o
        L.pump_sleep(0.2)
    return None


def run(args, results, ip):
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    host = L.HostProcess(port=args.port, env={"AC_TEST_OUTFIT_SEED": "2"}, extra_args=["--bootstrap-resident", str(host_slot), "--no-house-sync"],
                         log_path=os.path.join(HERE, "able_trade_host.log")).start()
    clients = []

    def mk(label, slot=None, guest=None):
        if guest is not None:
            c = L.FakeClient(label, ip, args.port, guest=guest)
        else:
            c = L.FakeClient(label, ip, args.port, player=L.resident_player(slot))
            c.rec_resident_idx = slot
        clients.append(c)
        c.connect_and_ready(quiet=True)
        return c

    try:
        ok = host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=host_slot)
        check("host reached field-ready state", ok)
        if not ok:
            return
        a = mk("A", residents[0])
        pa = PG.wait_pages(a)
        check("A got all 23 pages", len(pa) == 23)
        m = re.search(r"AC_TEST_OUTFIT_SEED: villager slot (\d+) \(npc 0x([0-9A-F]+)\) now wears shop design 2", host.log_text())
        check("host test hook dressed a villager (without a live actor) in shop design 2", m)
        if not m:
            return
        vs, vnpc = int(m.group(1)), int(m.group(2), 16)
        o1 = wait_outfit(a, lambda o: o[2][vs][1] == RSV_CLOTH and o[2][vs][2] == 2)
        check("O1 A received VILLAGER_OUTFIT (count 15, seq >= 1) with slot %d npc 0x%04X wearing RSV_CLOTH design 2" % (vs, vnpc),
              o1 and o1[1] == 15 and o1[0] >= 1 and o1[2][vs][0] == vnpc)
        # O2: trade design 2 away
        key = (PG.DESIGN, 2)
        r0 = PG.pages(a)[key][0]
        mk_a = a.inbox.mark()
        d1 = PG.design("TRADE2", palette=5)
        PG.write(a, PG.DESIGN, 2, r0, d1)
        L.pump_sleep(1.0)
        check("O2 A's design-2 write APPLIED", any(x[1] & PG.F_APPLIED for x in PG.replies(a, PG.DESIGN, 2, mk_a)))
        ok_reset = host.wait_for_log(r"\[NET\]\[OUTFIT\] host: villager slot %d \(npc 0x%04X\) stopped wearing shop design 2" % (vs, vnpc), 8.0)
        check("O2 the host ran the vanilla trend reset for that villager", ok_reset)
        o2 = wait_outfit(a, lambda o: o[0] > o1[0] and o[2][vs][1] != RSV_CLOTH and o[2][vs][2] == 0xFF, since=mk_a)
        check("O2 a NEW VILLAGER_OUTFIT (seq > %d) shows the default shirt (cloth 0x%04X, org 0xFF)" % (o1[0], o2[2][vs][1] if o2 else 0), o2)
        # G1 / G2 / G3
        g = mk("G", guest=L.guest_identity("ABLEGST", 0x4E21))
        pg = PG.wait_pages(g)
        pushes = [v for v in pg.values() if v[1] == 0]
        check("G1 the guest was pushed ALL 23 pages (%d)" % len(pushes), len(pg) == 23 and len(pushes) >= 23)
        og = wait_outfit(g, lambda o: o[1] == 15)
        check("G1 the guest got VILLAGER_OUTFIT too", og)
        k5 = (PG.DESIGN, 5)
        r5 = pg[k5][0]
        dg = PG.design("GUESTD", palette=7)
        mg = g.inbox.mark()
        PG.write(g, PG.DESIGN, 5, r5, dg)
        L.pump_sleep(1.0)
        check("G2 the guest's design-5 write was APPLIED", any(x[1] & PG.F_APPLIED and x[3] == dg for x in PG.replies(g, PG.DESIGN, 5, mg)))
        pa5 = PG.wait_rev(a, k5, r5 + 1)
        check("G2 ... and mirrored to resident A", pa5 and pa5[3] == dg)
        kn = (PG.NOTICE, 2)
        mg = g.inbox.mark()
        PG.write(g, PG.NOTICE, 2, pg[kn][0], PG.notice("guestnote"))
        L.pump_sleep(1.0)
        rn = PG.replies(g, PG.NOTICE, 2, mg)
        check("G2 the guest's NOTICE write is refused BAD (notice board: residents only)", any(x[1] & PG.F_BAD for x in rn) and not any(x[1] & PG.F_APPLIED for x in rn))
        mg = g.inbox.mark()
        PG.write(g, PG.DESIGN, 5, r5, PG.design("GSTALE"))
        L.pump_sleep(1.0)
        rs = PG.replies(g, PG.DESIGN, 5, mg)
        check("G3 the guest's write on the stale rev %d is refused STALE with the canonical copy" % r5, any(x[1] & PG.F_STALE and x[3] == dg for x in rs))
        check("host did not crash", host.proc.poll() is None)
    finally:
        for cl in clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=15410)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4_* copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    snap = os.path.join(os.environ.get("TEMP", "."), "able_trade_snap_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    try:
        run(args, results, "127.0.0.1")
    finally:
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
