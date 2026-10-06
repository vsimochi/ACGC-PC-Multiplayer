#!/usr/bin/env python3
"""test_shop_catalog_protocol.py - Patch 1: the shop's CATALOG GENERATION (stale-purchase guard), the closed-while-restocking purchase gate and the restock -> new authoritative catalog
chain, on top of the existing host-authoritative SHOP_BUY (see test_shop_protocol.py for the single purchase / race / sold-propagation coverage).

TIER: PROTOCOL TESTED against a REAL host process (`--host --bootstrap-resident 0`, scripted resident FakeClients), exactly the harness of test_shop_protocol.py; run it on a DISPOSABLE
copy: NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>> (the fixture save of that copy is snapshotted and restored). The host gets AC_TEST_HOOKS=1 and
AC_TEST_RESTOCK_WAIT_MS=8000 so the manual restock lasts 8 s instead of 60 s (the 60 s itself is covered by test_restock_protocol.py).
  G1 HOST_CONFIG byte 3 carries the catalog generation; a purchase that claims the CURRENT generation (tag.flags = 1 + gen % 255) succeeds, the stock slot is sold in the mirror
  G2 two clients race for the SAME unique item: exactly one APPLIED, the other NOT_AVAILABLE, only one debit, the loser's wallet intact; the pushed catalog has it sold
  G3 a resident pays 500 for a restock: while it runs a purchase is REFUSED with RESTOCKING (31) and nothing is debited
  G4 the restock ends: a NEW catalog (SHOP digest differs) and a NEW generation (HOST_CONFIG byte 3 changed) reach every client
  G5 a purchase that still claims the OLD generation is refused STALE_CATALOG (32), nothing debited; the same purchase with the new generation is APPLIED; an item that only existed in
     the old catalog is NOT_AVAILABLE
Usage: python test_shop_catalog_protocol.py [--port 11140]
"""
import argparse
import os
import shutil
import time
import sys

os.environ["AC_TEST_HOOKS"] = "1"
os.environ["AC_TEST_RESTOCK_WAIT_MS"] = "8000"

import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_ts_protocol as TS  # noqa: E402
import test_shop_protocol as SP  # noqa: E402
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE  # noqa: E402

SVC_HOSTCFG = 4
K_RESTOCK = L.PC_NETGAME_TXN_KIND_SHOP_RESTOCK
REASON_RESTOCKING, REASON_STALE_CATALOG = 31, 32


def cat_tag(gen_byte):
    return 1 + (gen_byte & 0xFF) % 255


def cfg(c):
    r = c.ts_latest(SVC_HOSTCFG)
    return None if r is None else r[1]


def phase_catalog(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    ck("G0 setup: A and B READY and SYNCED", a.rec_synced and b.rec_synced)
    sa = SP.wait_shop(a, 1, 6.0)
    sb = SP.wait_shop(b, 1, 6.0)
    if sa is None or sb is None:
        ck("G0 both clients received the SHOP service", False)
        return
    cfg_a = cfg(a)
    ck("G1 HOST_CONFIG (service 4) reached A and carries the catalog generation in byte 3, restocking = 0", cfg_a is not None and len(cfg_a) >= 8 and cfg_a[2] == 0)
    gen0 = cfg_a[3]
    blob0 = sa[1]
    cand = SP.listed(run, blob0)
    ck("G0 the stock offers at least 4 buyable LISTED items", len(cand) >= 4)
    if len(cand) < 4:
        return
    (i1, it1, p1), (i2, it2, p2), (i3, it3, p3), (i4, it4, p4) = cand[:4]
    slots = SP.free_slots(a, 4)
    # ---- G1: a purchase claiming the current generation
    off = len(run.log())
    rid, sent, r = SP.buy(run, a, slots[0], it1, i1, p1, p1 + 500, flags=cat_tag(gen0))
    run.tx_ok("G1 SHOP_BUY claiming the CURRENT catalog generation (tag.flags=%d)" % cat_tag(gen0), r, APPLIED, R["NONE"])
    s_after = SP.wait_shop(a, sa[0].seq + 1, 4.0)
    ck("G1 the pushed catalog has the bought slot SOLD", s_after is not None and SP.marker_ok(it1, SP.shop_items(s_after[1])[i1]))
    # ---- G2: the race for ONE unique item
    off = len(run.log())
    sent_a = a.send_txn_commit(SP.K_BUY, run.fresh_rid(), SP.D_POCKET, slots[1], it2, pre=SP.pre_free(a, slots[1], p2 + 100), aux_cond=i2, aux_item=p2, flags=cat_tag(gen0))
    bslot = SP.free_slots(b, 1)[0]
    sent_b = b.send_txn_commit(SP.K_BUY, run.fresh_rid(), SP.D_POCKET, bslot, it2, pre=SP.pre_free(b, bslot, p2 + 200), aux_cond=i2, aux_item=p2, flags=cat_tag(gen0))
    ra, rb = a.wait_txn_result(sent_a.seq, 4.0), b.wait_txn_result(sent_b.seq, 4.0)
    wins = [x for x in (ra, rb) if x is not None and x.outcome == APPLIED]
    lose = [x for x in (ra, rb) if x is not None and x.outcome == REJECTED and x.reason == R["NOT_AVAILABLE"]]
    ck("G2 two clients racing for the same item: exactly ONE APPLIED and ONE NOT_AVAILABLE (16)", len(wins) == 1 and len(lose) == 1)
    ck("G2 the host logged exactly one committed purchase of that item", run.n_log(r"SHOP_BUY item=0x%04X stock=0x%02X .* committed \[TXN\]" % (it2, i2), off) == 1)
    # ---- G3: restock; purchases are refused while it runs
    off = len(run.log())
    pre = (a.txn_pre_image()[0], a.txn_pre_image()[1], 2000)
    rs = a.send_txn_commit(K_RESTOCK, run.fresh_rid(), D_NONE, 0, 0, pre=pre, aux_cond=0, aux_item=500)
    rr = a.wait_txn_result(rs.seq, 4.0)
    run.tx_ok("G3 the restock request (500 Bells) is APPLIED", rr, APPLIED, R["NONE"])
    ck("G3 post wallet = 2000 - 500", rr is not None and rr.post_wallet == 1500)
    L.pump_sleep(1.0)
    cfg_mid = cfg(b)
    ck("G3 HOST_CONFIG tells B: restocking = 1", cfg_mid is not None and cfg_mid[2] == 1)
    s_cur = b.ts_latest(SP.SVC_SHOP)[1]
    left = [t for t in SP.listed(run, s_cur) if t[0] not in (i1, i2)]
    if left:
        (ix, itx, px) = left[0]
        _rid, _s, rx = SP.buy(run, b, bslot, itx, ix, px, px + 100, flags=cat_tag(gen0))
        ck("G3 a SHOP_BUY during the restock is REFUSED with RESTOCKING (31), nothing debited", rx is not None and rx.outcome == REJECTED and rx.reason == REASON_RESTOCKING)
    # ---- G4: the restock ends
    end = time.monotonic() + 25.0
    while time.monotonic() < end and not (cfg(b) is not None and cfg(b)[2] == 0 and cfg(b)[3] != gen0):
        L.pump_sleep(0.25)
    cb = cfg(b)
    ck("G4 the restock ended: HOST_CONFIG restocking = 0 AND the catalog generation changed (%s -> %s)" % (gen0, None if cb is None else cb[3]), cb is not None and cb[2] == 0 and cb[3] != gen0)
    L.pump_sleep(1.5)
    new_a, new_b = a.ts_latest(SP.SVC_SHOP)[1], b.ts_latest(SP.SVC_SHOP)[1]
    ck("G4 a NEW catalog reached both clients (the SHOP digest differs from the day's catalog) and both hold the same one", new_a != blob0 and new_a == new_b)
    gen1 = cb[3]
    # ---- G5: stale generation / old item / new generation
    cand1 = SP.listed(run, new_a)
    ck("G5 the new catalog offers a buyable item", len(cand1) >= 1)
    if not cand1:
        return
    (j, itj, pj) = cand1[0]
    wallet0 = 5000
    _rid, _s, r_stale = SP.buy(run, b, bslot, itj, j, pj, wallet0, flags=cat_tag(gen0))
    ck("G5 a purchase that still claims the OLD generation is refused STALE_CATALOG (32), nothing debited", r_stale is not None and r_stale.outcome == REJECTED and r_stale.reason == REASON_STALE_CATALOG)
    _rid, _s, r_ok = SP.buy(run, b, bslot, itj, j, pj, wallet0, flags=cat_tag(gen1))
    run.tx_ok("G5 the same purchase claiming the NEW generation is APPLIED", r_ok, APPLIED, R["NONE"])
    old_only = [t for t in cand if t[1] not in SP.shop_items(new_a)]
    if old_only:
        (_k, itk, pk) = old_only[0]
        k_code = [t[0] for t in cand if t[1] == itk][0]
        _rid, _s, r_old = SP.buy(run, a, slots[2], itk, k_code, pk, pk + 100, flags=cat_tag(gen1))
        ck("G5 an item that only existed in the OLD catalog is NOT_AVAILABLE (16)", r_old is not None and r_old.outcome == REJECTED and r_old.reason == R["NOT_AVAILABLE"])
    run.release(a)
    run.release(b)


PHASES = [("SC-catalog", [], phase_catalog)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11140)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: this test mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_<name> copy only (NET_SPIKE_GAME_BIN=<abs path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "shop_catalog_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        for i, (tag, extra, body) in enumerate(PHASES):
            TS.run_phase(results, ip, args.port + i, tag, extra, body, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
