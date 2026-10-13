#!/usr/bin/env python3
"""test_museum_donor_promotion_protocol.py - worker B: GUEST DONOR ATTRIBUTION across a guest -> resident PROMOTION (pc_m_card.c pc_mp_promote_create),
scripted FakeClients against REAL `--dedicated` hosts on a DISPOSABLE fixture pc\\build64\\bin_fixture4_wlB_mpromo (fresh copy, resident slot 3 / house 3 freed, this
worker's pc\\build64_w3 exe; ports 15470+; Samsung display, volume 1).
  P0 a guest donates an undonated fish in person (MUSEUM_DONATE): nibble 5, MUSEUM_DONORS (service 8) names the guest, museum_donors.dat is written in the server tree
  P1 `--promote-fault fail_save`: the paid house purchase (kind 14) is REJECTED and rolled back: the fish keeps nibble 5 and the guest's name (the 63-byte museum_display
     is part of the promotion snapshot)
  P2 the retry APPLIES: the host log re-credits the guest's exhibit to resident slot 3 (nibble 4); after a graceful stop the GCI's museum_display holds nibble 4 for the
     fish; a resident session's service 8 no longer names the (former) guest for it (an orphan entry is dropped from museum_donors.dat at the next load)
Usage: python test_museum_donor_promotion_protocol.py [--port 15470]
"""
import argparse
import glob
import os
import shutil
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as TT  # noqa: E402

FIXTURE = "bin_fixture4_wlB_mpromo"
W3_EXE = os.path.join(TT.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(TT.BUILD64, FIXTURE)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")

import net_spike_lib as L  # noqa: E402

FISH0 = 0x2300
MUS_FISH = 0x15
GCI_MUSEUM = 0x40 + 0x26000 + 0x213A8
GST_ENTRY = 72 + 0x2440


def nib_fish(blob, i):
    return (blob[MUS_FISH + (i >> 1)] >> ((i & 1) * 4)) & 0xF


def donors_key(c, exhibit):
    st = c.ts_latest(8)
    if st is None:
        return None
    b = st[1]
    ix = b[4 + exhibit]
    if ix == 0xFF or ix >= b[0]:
        return None
    k = bytes(b[124 + 20 * ix:124 + 20 * ix + 20])
    return k[:16] + struct.pack(">HH", *struct.unpack("<HH", k[16:20]))


def store_of(pid):
    d = L.server_file("guests.dat")[:-len(".dat")]
    for fn in glob.glob(os.path.join(d, "*.gst")):
        b = open(fn, "rb").read()
        if bytes(b[48 + 4:48 + 24]) == pid:
            return fn, bytearray(b)
    return None, None


def patch_wallet(pid, wallet):
    fn, b = store_of(pid)
    if fn is None:
        return False
    rec = bytearray(b[48 + 72:48 + 72 + 0x2440])
    struct.pack_into(">I", rec, L.REC_OFF_WALLET, wallet)
    b[48 + 72:48 + 72 + 0x2440] = rec
    struct.pack_into("<I", b, 48 + 52, zlib.crc32(bytes(rec)) & 0xFFFFFFFF)
    struct.pack_into("<I", b, len(b) - 4, zlib.crc32(bytes(b[:-4])) & 0xFFFFFFFF)
    open(fn, "wb").write(bytes(b))
    return True


def wait_until(fn, timeout):
    import time
    t0 = time.monotonic()
    v = fn()
    while not v and time.monotonic() - t0 < timeout:
        L.pump_sleep(0.5)
        v = fn()
    return v


def start_ded(rig, tag, port, extra=()):
    """A dedicated host on the fixture; the FIRST launch adopts the fixture's legacy town ("[1] Use this town", like test_guest_mailbox_protocol.py)."""
    first = not os.path.isfile(L.server_gci())
    h = L.HostProcess(port=port, extra_args=["--dedicated"] + list(extra), log_path=TT.log_path("wlB_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR,
                      stdin_pipe=True, verbose=False, new_group=True).start()
    orig = h.log_text
    h.log_text = lambda: orig().replace(chr(13) + chr(10), chr(10))
    if first:
        h.send_line("1")
    rig.port = port
    ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
    rig.ck("[%s] dedicated host booted (%s)" % (tag, " ".join(extra) or "defaults"), ok)
    if ok:
        L.resolve_host_town("127.0.0.1", port)
    return h, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=15470)
    args = ap.parse_args()
    import test_guest_lifecycle_protocol as LC  # noqa: E402  (make_free_slot, Rig, buy)
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    dst = TT.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(dst, "AnimalCrossing.exe"))
    LC.make_free_slot(dst)
    L.server_wipe_sidecars()
    rig = LC.Rig(results)
    g = L.guest_identity("DONORG", 0x4D31, "HOMETWN", 0x5E31)
    pid = L.guest_pid_be(g)
    ex = None
    # ---- P0
    h, ok = start_ded(rig, "mdp0", args.port)
    if ok:
        try:
            c = rig.join("DG", g, bytes(L.fresh_guest_record_for(g)))
            tok = bytes(c.guest_token)
            st = c.wait_ts_state(2, 6.0)
            free = [i for i in range(40) if st is not None and nib_fish(st[1], i) == 0]
            ck("P0 fixture precondition: an undonated fish", free)
            fi = free[0]
            ex = 80 + fi
            L.pump_sleep(2.0)
            pockets, conds, wallet = c.txn_pre_image()
            slot = list(pockets).index(0)
            pk = tuple(FISH0 + fi if i == slot else p for i, p in enumerate(pockets))
            c.rec_local = L.record_set_inventory(c.rec_local if c.rec_local is not None else c.own_record(), pk, conds, wallet)
            c.upload_local_record()
            s = c.send_txn_commit(L.PC_NETGAME_TXN_KIND_MUSEUM_DONATE, 1, L.PC_NETGAME_TXN_DEST_NONE, slot, FISH0 + fi)
            r = c.wait_txn_result(s.seq, 4.0)
            ck("P0 the guest's in-person donation is APPLIED", r is not None and r.outcome == 0)
            ck("P0 MUSEUM_DONORS names the guest for exhibit %d" % ex, wait_until(lambda: donors_key(c, ex) == pid, 8.0))
            ck("P0 museum_donors.dat is written in the server tree", os.path.isfile(L.server_file("museum_donors.dat")))
            L.pump_sleep(2.0)
            rig.release(c)
            L.pump_sleep(4.0)
        finally:
            rig.closeall()
            ck("P0 graceful stop", rig.stop(h) == 0)
    if ex is None:
        return L.summary_and_exit_code(results)
    ck("P0 the guest's stored wallet patched to 25000 (it pays the house)", patch_wallet(pid, 25000))
    # ---- P1 / P2
    h, ok = start_ded(rig, "mdp1", args.port + 1, ["--resident-tokens", "tofu", "--promote-fault", "fail_save"])
    if ok:
        try:
            a = rig.join("DG", g, bytes(L.fresh_guest_record_for(g)), token=tok)
            L.pump_sleep(6.0)
            s1 = LC.buy(a, rid=1)
            r1 = a.wait_txn_result(s1.seq, 10.0)
            ck("P1 `fail_save`: the purchase is REJECTED and rolled back", r1 is not None and r1.outcome == 1)
            L.pump_sleep(2.0)
            t = h.log_text()
            ck("P1 the promotion re-credited the exhibit inside the attempt and rolled it back",
               "promotion: 1 exhibit(s) of the guest are now credited to resident slot 3 (donor nibble 4)" in t and t.count("the in-memory change was ROLLED BACK") == 1)
            st = a.wait_ts_state(2, 4.0)
            ck("P1 after the rollback the fish still has nibble 5", st is not None and nib_fish(st[1], ex - 80) == 5)
            ck("P1 ... and MUSEUM_DONORS still names the guest", donors_key(a, ex) == pid)
            s2 = LC.buy(a, rid=2)
            r2 = a.wait_txn_result(s2.seq, 15.0)
            ck("P2 the retry APPLIES (the guest is promoted to resident slot 3)", r2 is not None and r2.outcome == 0)
            L.pump_sleep(2.0)
            ck("P2 host log: the exhibit was re-credited twice (rolled back try + retry)",
               h.log_text().count("promotion: 1 exhibit(s) of the guest are now credited to resident slot 3 (donor nibble 4)") == 2)
        finally:
            rig.closeall()
            ck("P2 graceful stop", rig.stop(h) == 0)
    gci = open(LC.gci_path(), "rb").read()
    ck("P2 the saved GCI's museum_display holds nibble 4 (resident slot 3) for the fish", nib_fish(gci[GCI_MUSEUM:GCI_MUSEUM + 63], ex - 80) == 4)
    # ---- P3 the orphan is dropped at the next load
    h, ok = start_ded(rig, "mdp3", args.port + 2)
    if ok:
        try:
            ck("P3 host log: museum_donors.dat loaded with the orphan dropped", wait_until(lambda: "1 orphan(s) dropped" in h.log_text(), 30.0) or "0 attribution(s)" in h.log_text())
        finally:
            rig.closeall()
            ck("P3 graceful stop", rig.stop(h) == 0)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
