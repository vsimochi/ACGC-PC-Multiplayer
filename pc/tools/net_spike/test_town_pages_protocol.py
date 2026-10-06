#!/usr/bin/env python3
"""test_town_pages_protocol.py - Patch 6b: host-authoritative TOWN-SHARED pages (notice board posts + Able Sisters designs), PC_NETGAME_MSG_PAGE (id 71, 560 B, reliable, both directions).

TIER: PROTOCOL TESTED against a REAL host process (`--host --bootstrap-resident 0`), scripted FakeClients (residents 1..3). Run on a DISPOSABLE fixture copy
(NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>>): the host save is snapshotted at start and restored at the end (the host restart phase writes the GCI).
  P1 initial page state: a client gets ALL 23 pages (15 notice posts + 8 designs) at join, each with revision >= 1
  P2 a resident writes a design page built on the current revision: APPLIED, new revision; a SECOND client receives the new content and revision
  P3 a write built on a STALE revision is refused with STALE + the canonical copy (no silent last-writer-wins); two clients racing from the same base: exactly one applies
  P4 a notice post write is applied and mirrored; invalid content (control byte, bad flag, bad length, bad index) is refused BAD and nothing changes
  P5 independent pages: writes to two different pages both apply
  P6 late join / reconnect: a NEW client is pushed the CURRENT canonical copies
  P7 a guest cannot write (BAD / refused)
  P8 host restart: the canonical content survives (the host save), a client joining the restarted host receives it
Usage: python test_town_pages_protocol.py [--port 11890]
"""
import argparse
import os
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402

MSG_PAGE = 71
FMT = "<BBBBIIHH"
HDR = struct.calcsize(FMT)
SIZE = HDR + 544
NOTICE, DESIGN = 1, 2
F_WRITE, F_APPLIED, F_STALE, F_BAD = 1, 2, 4, 8
NOTICE_LEN, DESIGN_LEN = 200, 0x220


def is_page(m):
    return m.channel == L.CH_RELIABLE and len(m.payload) == SIZE and m.payload[0] == MSG_PAGE


def pages(c):
    """{(kind, index): (rev, flags, aux, data)} -- the newest (highest rev, then latest) page message per page"""
    out = {}
    for m in c.inbox.peek_all(is_page):
        t, kind, idx, flags, rev, aux, ln, _r = struct.unpack_from(FMT, m.payload)
        cur = out.get((kind, idx))
        if cur is None or rev >= cur[0]:
            out[(kind, idx)] = (rev, flags, aux, bytes(m.payload[HDR:HDR + ln]))
    return out


def replies(c, kind, idx, since):
    res = []
    for m in c.inbox.peek_all(is_page, since=since):
        t, k, i, flags, rev, aux, ln, _r = struct.unpack_from(FMT, m.payload)
        if k == kind and i == idx:
            res.append((rev, flags, aux, bytes(m.payload[HDR:HDR + ln])))
    return res


def write(c, kind, idx, base_rev, data):
    ln = len(data)
    c.send_reliable(struct.pack(FMT, MSG_PAGE, kind, idx, F_WRITE, 0, base_rev, ln, 0) + data + bytes(544 - ln))


def design(tag, palette=3, flag=1):
    name = (tag.encode() + b"\x20" * 16)[:16]
    return name + bytes([palette, flag]) + bytes(14) + bytes((i * 7 + len(tag)) & 0xFF for i in range(0x200))


def notice(tag):
    return (tag.encode() * 200)[:192] + bytes(8)


def wait_pages(c, n=23, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if len(pages(c)) >= n:
            return pages(c)
        L.pump_sleep(0.2)
    return pages(c)


def wait_rev(c, key, rev, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        p = pages(c).get(key)
        if p and p[0] >= rev:
            return p
        L.pump_sleep(0.2)
    return pages(c).get(key)


def run(args, results, ip):
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    log = os.path.join(HERE, "town_pages_host.log")
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(host_slot)], log_path=log).start()
    clients = []

    def mk(label, slot):
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
        b = mk("B", residents[1])
        pa, pb = wait_pages(a), wait_pages(b)
        check("P1 both clients got ALL 23 pages at join (%d, %d)" % (len(pa), len(pb)), len(pa) == 23 and len(pb) == 23)
        check("P1 every page has revision >= 1 and the right size", all(v[0] >= 1 for v in pa.values()) and all(len(v[3]) == (NOTICE_LEN if k[0] == NOTICE else DESIGN_LEN) for k, v in pa.items()))
        key = (DESIGN, 2)
        r0 = pa[key][0]
        # P2
        d1 = design("ALPHA")
        ma = a.inbox.mark()
        write(a, DESIGN, 2, r0, d1)
        L.pump_sleep(1.0)
        rp = replies(a, DESIGN, 2, ma)
        check("P2 A's write built on rev %d was APPLIED (reply flags %s)" % (r0, [x[1] for x in rp]), any(x[1] & F_APPLIED and x[0] == r0 + 1 and x[3] == d1 for x in rp))
        pbb = wait_rev(b, key, r0 + 1)
        check("P2 client B received the new content at rev %d" % (r0 + 1), pbb and pbb[0] == r0 + 1 and pbb[3] == d1)
        # P3 stale write
        d2 = design("BRAVO")
        mb = b.inbox.mark()
        write(b, DESIGN, 2, r0, d2)  # built on the OLD revision
        L.pump_sleep(1.0)
        rp = replies(b, DESIGN, 2, mb)
        check("P3 B's write built on the stale rev %d is refused STALE with A's canonical copy" % r0, any(x[1] & F_STALE and x[3] == d1 and x[0] == r0 + 1 for x in rp))
        # race on page 3 from the same base
        key3 = (DESIGN, 3)
        r3 = pa[key3][0]
        ma, mb = a.inbox.mark(), b.inbox.mark()
        write(a, DESIGN, 3, r3, design("RACE-A"))
        write(b, DESIGN, 3, r3, design("RACE-B"))
        L.pump_sleep(1.5)
        ra, rb = replies(a, DESIGN, 3, ma), replies(b, DESIGN, 3, mb)
        wins = sum(1 for rr in (ra, rb) for x in rr if x[1] & F_APPLIED and x[2] == r3)
        loses = sum(1 for rr in (ra, rb) for x in rr if x[1] & F_STALE)
        check("P3 two clients racing from the same base: exactly ONE applied (%d), the other STALE (%d)" % (wins, loses), wins == 1 and loses == 1)
        # P4 notice + invalid
        n1 = notice("hello")
        kn = (NOTICE, 3)
        rn = pa[kn][0]
        ma = a.inbox.mark()
        write(a, NOTICE, 3, rn, n1)
        L.pump_sleep(1.0)
        check("P4 a notice post was APPLIED", any(x[1] & F_APPLIED and x[3] == n1 for x in replies(a, NOTICE, 3, ma)))
        pbn = wait_rev(b, kn, rn + 1)
        check("P4 ... and mirrored to B", pbn and pbn[3] == n1)
        rn += 1
        bad = [("control byte in a notice", NOTICE, 4, notice("x")[:10] + b"\x7f" + notice("x")[11:]),
               ("message tag in a notice", NOTICE, 4, b"\x80" + notice("x")[1:]),
               ("bad flag_design_set", DESIGN, 4, design("BAD", flag=5)),
               ("palette out of range", DESIGN, 4, design("BAD", palette=200)),
               ("wrong length", NOTICE, 4, notice("x")[:50]),
               ("index out of range", NOTICE, 40, notice("x"))]
        for what, k, i, data in bad:
            rr0 = pages(a).get((k, i), (1,))[0]
            m0 = a.inbox.mark()
            write(a, k, i, rr0, data)
            L.pump_sleep(0.7)
            rp = replies(a, k, i, m0)
            if i == 40:
                check("P4 %s: no reply, nothing changes" % what, not rp)
            else:
                check("P4 %s: refused BAD or ignored, never applied" % what, not any(x[1] & F_APPLIED for x in rp))
        # P5 independent pages
        ka, kb = (DESIGN, 5), (DESIGN, 6)
        ra_, rb_ = pa[ka][0], pa[kb][0]
        write(a, DESIGN, 5, ra_, design("IND5"))
        write(b, DESIGN, 6, rb_, design("IND6"))
        pa5, pb6 = wait_rev(b, ka, ra_ + 1), wait_rev(a, kb, rb_ + 1)
        check("P5 two writes to two different pages BOTH applied and cross-mirrored", pa5 and pb6 and pa5[3] == design("IND5") and pb6[3] == design("IND6"))
        # P6 late join
        c = mk("C", residents[2])
        pc_ = wait_pages(c)
        check("P6 a LATE joiner is pushed the current canonical copies", len(pc_) == 23 and pc_[key][3] == d1 and pc_[kn][3] == n1 and pc_[ka][3] == design("IND5"))
        # reconnect: A leaves and returns
        a.disconnect()
        a.close()
        clients.remove(a)
        L.pump_sleep(1.0)
        a2 = mk("A2", residents[0])
        pa2 = wait_pages(a2)
        check("P6 a RECONNECTING client gets the current canonical copies", len(pa2) == 23 and pa2[key][3] == d1 and pa2[kb][3] == design("IND6"))
        # P7 guest
        g = L.FakeClient("G", ip, args.port, guest=L.guest_identity("PAGEGUEST", 0x4E01))
        clients.append(g)
        g.connect_and_ready(quiet=True)
        L.pump_sleep(1.0)
        mg = g.inbox.mark()
        write(g, DESIGN, 7, 1, design("GUEST"))
        L.pump_sleep(1.0)
        gp = pages(g)
        pushes = [v for v in gp.values() if v[1] == 0]
        check("P7 a guest is never pushed a page (%d pushes), its write is refused BAD and not applied" % len(pushes), len(pushes) == 0 and any(x[1] & F_BAD for x in replies(g, DESIGN, 7, mg)) and not any(x[1] & F_APPLIED for x in replies(g, DESIGN, 7, mg)))
        # P8 persistence over a host restart
        for cl in list(clients):
            try:
                cl.disconnect()
            except Exception:  # noqa: BLE001
                pass
            cl.close()
        clients.clear()
        host.wait_for_log(r"\[PC\] GCI save: written successfully", 30.0)
        L.pump_sleep(1.0)
        host.stop()
        L.pump_sleep(2.0)
        host2 = L.HostProcess(port=args.port + 1, extra_args=["--bootstrap-resident", str(host_slot)], log_path=os.path.join(HERE, "town_pages_host2.log")).start()
        try:
            ok2 = host2.wait_listening(60.0) and host2.boot_to_field(timeout=90.0, slot=host_slot)
            check("the host restarted", ok2)
            if ok2:
                args.port += 1
                d = mk("D", residents[0])
                pd = wait_pages(d)
                check("P8 after the host restart the canonical pages are the committed ones (design 2, notice 3, design 5)", len(pd) == 23 and pd[key][3] == d1 and pd[kn][3] == n1 and pd[ka][3] == design("IND5"))
        finally:
            for cl in clients:
                cl.close()
            host2.stop()
        host = None
    finally:
        for cl in clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        if host is not None:
            host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11890)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4_* copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    snap = os.path.join(os.environ.get("TEMP", "."), "town_pages_snap_%d" % os.getpid())
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
