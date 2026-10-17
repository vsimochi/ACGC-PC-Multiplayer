#!/usr/bin/env python3
"""test_pk_pocket_police_protocol.py - worker PK: full-pocket pickup + police lost-and-found multi-claim, HOST half against a REAL host.

PROTOCOL test (real host binary, scripted FakeClients; net_spike_lib / test_txn_protocol / test_ts_protocol helpers). The host is started from the
fixture named by NET_SPIKE_GAME_BIN (a pc\\build64\\bin_fixture4* copy ONLY; the save dir is snapshotted and restored) with
`--bootstrap-resident 0 --pickup-test-seed --bury-test-seed --field-action-test-seed --no-house-sync --ts-test-seed-police=0x2800,0x2801,0x2802`.

  FP  full pockets (A's record is uploaded with all 15 pockets used, the ordinary D3 upload):
      FP1 the upload is APPLIED and the host mirror holds 15 used pockets
      FP2 a pickup COMMIT into a pocket that is used in the (full) pre-image -> REJECTED(PRECOND); WALLET for a non-money item -> PRECOND;
          the tile still holds the item, the record rev is unchanged, the reservation was released (a new request is accepted again)
      FP3 a MONEY BAG with full pockets still goes to the WALLET (the one full-pocket pickup the real client still sends): APPLIED, pockets unchanged
      FP4 cleanup: the pockets are uploaded back with free slots
  PC  police multi-claim (the in-game bug: only one claim per visit):
      PC1 A claims SEEDS[0], then -- in the same session, no reconnect -- SEEDS[1] at the index of the NEW (compacted) mirror: both APPLIED with
          reason NONE (never STALE_IMAGE / CONFLICT / NOT_AVAILABLE), rev + 1 each, each item enters the pocket once
      PC2 the mirror is refreshed after EACH claim (seq + 1 each) for A AND for B (another player in the building), compacted, identical blobs
      PC3 the value-relocated index (what the fixed client sends after a compaction: the same item found at its new index) is APPLIED; the stale
          pre-compaction index stays NOT_AVAILABLE (never the wrong item)
      PC4 two clients race for the SAME item: exactly one APPLIED, the other REJECTED(NOT_AVAILABLE); the item is in exactly one post-image and gone
          from the lost and found
Usage: python test_pk_pocket_police_protocol.py [--port 29600]
"""
import argparse
import os
import shutil
import sys

import net_spike_lib as L
import test_txn_protocol as TP
import test_ts_protocol as TS
from test_txn_protocol import APPLIED, REJECTED, R, D_POCKET, D_WALLET, S_WALLET, KP, ROCK_TILE, ROCK_DROP_TILE, ITM_MONEY_100, first_free

SEEDS = TS.SEEDS
FILL = 0x2203  # a legal ITEM1 id (a fishing rod, not a police seed) used to fill every pocket


def fp_full_pockets(run, a, b):
    ck = run.check
    pockets0, conds0, wallet0 = a.txn_pre_image()
    full = tuple(p if p else FILL for p in pockets0)
    a.rec_local = L.record_set_inventory(a.rec_local if a.rec_local is not None else a.own_record(), full, conds0, wallet0)
    ack = a.upload_local_record()
    ck("FP1 the full-pocket record upload is APPLIED and A's image now has no EMPTY pocket", ack is not None and 0 not in a.txn_pre_image()[0])
    t = run.reserve(a)
    ck("FP2 setup: a seeded tile reserved (the host does not look at pockets at request time; the real client no longer sends this)", t is not None)
    if t is None:
        return
    tile, rid, prov = t
    rev0 = a.rec_last[2]
    mark_b = b.inbox.mark()
    _s, r1 = run.commit(a, KP, rid, D_POCKET, 0, prov.granted_item)
    run.tx_ok("FP2 pickup POCKET with full pockets (slot 0 used in the pre-image)", r1, REJECTED, R["PRECOND"])
    again = run.reserve(a, tile=tile)
    ck("FP2 the reject released the reservation (the same tile is accepted again)", again is not None)
    if again is not None:
        _s, r2 = run.commit(a, KP, again[1], D_WALLET, S_WALLET, again[2].granted_item)
        run.tx_ok("FP2 pickup WALLET of a non-money item with full pockets", r2, REJECTED, R["PRECOND"])
    L.pump_sleep(0.5)
    ck("FP2 nothing moved: the tile still holds the item (no FIELD_UPDATE), A's rev unchanged, pockets still full",
       run.tile_of(b, tile) == prov.granted_item and not run.field_updates(b, tile, mark_b) and a.rec_last[2] == rev0
       and tuple(a.txn_pre_image()[0]) == full)
    # FP3: money bag -> wallet with full pockets
    a.claim_position(*L.tile_center(*ROCK_TILE))
    a.send_reliable(L.build_field_action_request(2, ROCK_TILE[0], ROCK_TILE[1], run.fresh_rid()))
    L.pump_sleep(0.8)
    bag = run.reserve(a, tile=ROCK_DROP_TILE)
    ck("FP3 setup: the money rock dropped a bag A can reserve", bag is not None and bag[2].granted_item == ITM_MONEY_100)
    if bag is not None:
        w0 = a.txn_pre_image()[2]
        _s, rw = run.commit(a, KP, bag[1], D_WALLET, S_WALLET, ITM_MONEY_100)
        run.tx_ok("FP3 money bag with FULL pockets -> WALLET", rw, APPLIED, R["NONE"])
        ck("FP3 wallet + 100, pockets unchanged (still full)", rw is not None and rw.post_wallet == w0 + 100 and tuple(rw.post_pockets) == full)
    p, c, w = a.txn_pre_image()
    a.rec_local = L.record_set_inventory(a.rec_local, tuple(0 if i >= 5 else x for i, x in enumerate(p)), c, w)
    ack2 = a.upload_local_record()
    ck("FP4 cleanup: pockets 5..14 freed again (upload APPLIED)", ack2 is not None and a.txn_pre_image()[0].count(0) >= 10)


def pc_police(run, a, b):
    ck = run.check
    g0, blob0 = TS.latest(a, TS.SVC_POLICE, 1, 5.0)
    gb0 = TS.latest(b, TS.SVC_POLICE, 1, 5.0)
    ck("PC setup: A and B hold the same police mirror at READY", g0 is not None and gb0 is not None and gb0[1] == blob0)
    old = TS.pol_items(blob0)
    if any(s not in old for s in SEEDS):
        ck("PC setup: the three seeds are in the lost and found", False)
        return
    # PC1 / PC2: two claims in a row, same session
    rev0 = a.rec_last[2]
    before = list(a.txn_pre_image()[0])
    slot1 = first_free(before)
    _rid, _s, r1 = TS.claim(run, a, slot1, SEEDS[0], old.index(SEEDS[0]))
    run.tx_ok("PC1 first claim (SEEDS[0])", r1, APPLIED, R["NONE"])
    sa1 = a.wait_ts_state(TS.SVC_POLICE, 3.0, min_seq=g0.seq + 1)
    sb1 = b.wait_ts_state(TS.SVC_POLICE, 3.0, min_seq=g0.seq + 1)
    new1 = TS.pol_items(sa1[1]) if sa1 else None
    ck("PC2 after claim 1 BOTH clients get police seq + 1, identical, compacted, SEEDS[0] gone",
       sa1 is not None and sb1 is not None and sa1[1] == sb1[1] and sa1[0].seq == g0.seq + 1
       and new1 == TS.compact([x for i, x in enumerate(old) if i != old.index(SEEDS[0])]))
    if new1 is None:
        return
    slot2 = first_free(a.txn_pre_image()[0])
    _rid, _s, r2 = TS.claim(run, a, slot2, SEEDS[1], new1.index(SEEDS[1]))
    run.tx_ok("PC1 SECOND claim in the same session (SEEDS[1] at its new index): never STALE_IMAGE / CONFLICT", r2, APPLIED, R["NONE"])
    ck("PC1 rev advanced once per claim (base of claim 2 = result of claim 1)", r1 is not None and r2 is not None and r1.rev == rev0 + 1 and r2.rev == rev0 + 2)
    ck("PC1 each claimed item entered A's pockets exactly once more (claim 2's post-image, in the chosen slots)", r2 is not None
       and list(r2.post_pockets).count(SEEDS[0]) == before.count(SEEDS[0]) + 1 and list(r2.post_pockets).count(SEEDS[1]) == before.count(SEEDS[1]) + 1
       and r2.post_pockets[slot1] == SEEDS[0] and r2.post_pockets[slot2] == SEEDS[1])
    sa2 = a.wait_ts_state(TS.SVC_POLICE, 3.0, min_seq=sa1[0].seq + 1)
    sb2 = b.wait_ts_state(TS.SVC_POLICE, 3.0, min_seq=sa1[0].seq + 1)
    new2 = TS.pol_items(sa2[1]) if sa2 else None
    ck("PC2 after claim 2 BOTH clients get seq + 1 again (B, in the same building, sees the list shrink twice)",
       sa2 is not None and sb2 is not None and sa2[1] == sb2[1] and sa2[0].seq == sa1[0].seq + 1 and new2 == TS.compact([x for x in new1 if x != SEEDS[1]]))
    if new2 is None:
        return
    # PC3: stale index vs value-relocated index
    stale = new1.index(SEEDS[2])
    reloc = new2.index(SEEDS[2])
    ck("PC3 setup: the second compaction shifted SEEDS[2] (stale index != current index)", stale != reloc)
    slot3 = first_free(a.txn_pre_image()[0])
    _rid, _s, r3 = TS.claim(run, a, slot3, SEEDS[2], stale) if stale != reloc else (None, None, None)
    run.tx_ok("PC3 the stale pre-compaction index -> NOT_AVAILABLE (never the wrong item)", r3, REJECTED, R["NOT_AVAILABLE"])
    # PC4: race for SEEDS[2] at the relocated index (what the fixed client sends): exactly one winner
    pa, pb = a.txn_pre_image(), b.txn_pre_image()
    sa_ = first_free(pa[0])
    sb_ = first_free(pb[0])
    rid_a, rid_b = run.fresh_rid(), run.fresh_rid()
    ta = a.send_txn_commit(TS.K_CLAIM, rid_a, D_POCKET, sa_, SEEDS[2], pre=pa, aux_cond=reloc)
    tb = b.send_txn_commit(TS.K_CLAIM, rid_b, D_POCKET, sb_, SEEDS[2], pre=pb, aux_cond=reloc)
    ra, rb = a.wait_txn_result(ta.seq, 3.0), b.wait_txn_result(tb.seq, 3.0)
    ck("PC4 race: exactly one APPLIED (the value-relocated index is accepted), the other REJECTED(NOT_AVAILABLE)",
       ra is not None and rb is not None and sorted([ra.outcome, rb.outcome]) == [APPLIED, REJECTED]
       and [x.reason for x in (ra, rb) if x.outcome == REJECTED] == [R["NOT_AVAILABLE"]])
    L.pump_sleep(0.6)
    if ra is not None and rb is not None:
        cnt = list(ra.post_pockets).count(SEEDS[2]) + list(rb.post_pockets).count(SEEDS[2])
        ck("PC4 no duplicate: SEEDS[2] is in exactly ONE post-image, gone from both mirrors, one committed line",
           cnt == 1 and SEEDS[2] not in TS.pol_items(a.ts_latest(TS.SVC_POLICE)[1]) and a.ts_latest(TS.SVC_POLICE)[1] == b.ts_latest(TS.SVC_POLICE)[1]
           and run.n_log(r"POLICE_CLAIM item=0x%04X lost-and-found slot=\d+ -> pocket slot \d+ committed" % SEEDS[2]) == 1)


def phase_main(run):
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    run.check("setup: A and B READY and SYNCED", a.rec_synced and b.rec_synced and a.rec_last is not None and b.rec_last is not None)
    fp_full_pockets(run, a, b)
    pc_police(run, a, b)
    run.check("the host survived", run.host.alive())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=29600)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a pc\\build64\\bin_fixture4* copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    if not 29500 <= args.port <= 29999:
        print("REFUSING: worker PK ports are 29500-29999", file=sys.stderr)
        return 2
    TP.HOST_EXTRA[:] = TP.HOST_EXTRA + ["--no-house-sync", "--ts-test-seed-police=" + ",".join("0x%04X" % s for s in SEEDS)]
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "pk_protocol_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        TP.run_phase(results, "127.0.0.1", args.port, "PK", [], phase_main, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
