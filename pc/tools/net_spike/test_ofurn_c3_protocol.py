#!/usr/bin/env python3
"""test_ofurn_c3_protocol.py - OUTDOOR FURNITURE C3 (outdoor STORAGE of a placed storage piece: DRESSER_SELECT floor 0xFC, kind 18 with the outdoor key, the
re-key on MOVE, the pickup refusal NOT_EMPTY 62, the orphan / re-attach, restart persistence) against a REAL listen host.

TIER: PROTOCOL TESTED against a REAL listen host (scripted resident / guest FakeClients, the harness of test_ofurn_c2_protocol.py / test_dresser_storage_protocol.py).
Run on a DISPOSABLE fixture copy (NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_of3_<name>>). Ports 39000-39499 only.
  MAIN  102 CAPS total bit 0 (outdoor storage) at join; a resident places the 2x1 DRESSER (0x1064); SELECT floor 0xFC of its origin -> STATE (floor 0xFC, origin, gen,
        empty); C3b: a GUEST's SELECT from the front -> answered; a SELECT of a wrong tile, of a non-storage piece, from far away, from BEHIND / either SIDE
        (resident and guest), in the front row facing away -> no answer, the other front tile / two tiles out -> answered; PUT x2 with the outdoor selection -> APPLIED (pocket emptied,
        STATE lists them), the identical resend REPLAYED; a SECOND resident selects the same piece, sees both items, PUTs one; the first resident PUTs again (shared
        record: nobody FENCED); C3b: the GUEST sees the 4 items, PUTs its own (REPLAYED on resend), a resident PUT + TAKE after it (nobody FENCED), the guest TAKEs
        its item back, a stale generation is PRECOND, the guest's ROTATE / PICKUP stay NOT_ALLOWED 56; ROTATE -> the same key (the items stay); MOVE (push north) -> the host re-keys the storage, the old origin has no storage, the new one
        has all 4 items; kind 22 PICKUP -> REJECTED 62 OFURN_NOT_EMPTY (cells unchanged); a resident's VANILLA pickup of the origin leaf (what a 0.3.0 client does)
        -> refused, nothing changes; TAKE everything back -> conservation (pockets + storage); PICKUP -> APPLIED; no record-protocol violation
  GC    a seeded dresser with 1 stored item whose origin is overwritten behind the system's back: the GC drops the marker, the storage is kept as an ORPHAN (log); a
        new dresser of the same id placed elsewhere RE-ATTACHES it (log) and its SELECT shows the item; TAKE + PICKUP clean up
  OFF   AC_MP_DRESSER_STORAGE=0: CAPS total 0, an outdoor SELECT is never answered, a placed dresser picks up normally
  RS    restart: an item stored in a seeded outdoor dresser by host 1 is in host 2's STATE of the same piece
"""
import argparse
import os
import re
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
# pc/build64_w3's CMake cache still holds PC_DRESSER_STORAGE_DEFAULT_ON=OFF (configured before the default became ON): the feature is switched on explicitly
STORAGE_ENV = {"AC_MP_DRESSER_STORAGE": "1"}
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_ts_protocol as TS  # noqa: E402
import test_ofurn_protocol as OF  # noqa: E402
import test_ofurn_c2_protocol as C2  # noqa: E402
from test_txn_protocol import APPLIED, R, D_NONE, D_POCKET  # noqa: E402

K_DRESSER = 18
OP_PUT, OP_TAKE = 1, 2
PLACE, ROTATE, PICKUP, MOVE = 1, 2, 3, 4
NOT_EMPTY, BAD_TILE = 62, 58
MSG_STATE, MSG_QUERY = 83, 84
STATE_FMT = "<BBBBBBBBIIHH27HH"
QUERY_FMT = "<BBBBBBH"
OUTDOOR = 0xFC
DRESSER = C2.DRESSER_2X1  # 0x1064, a 2x1 storage piece
ONE_1X1 = C2.ONE_1X1      # 0x1320, not a storage piece
ITEMS = [0x2401, 0x2402, 0x2403, 0x2404]
GUEST_ITEM, RES_ITEM = 0x2405, 0x2406  # C3b: the guest's own item / a resident's item stored and taken after the guest
S, E, N, W = 0, 1, 2, 3


def states(c, since=None):
    out = []
    for m in c.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and len(m.payload) == 76 and m.payload[0] == MSG_STATE, since=since):
        v = struct.unpack(STATE_FMT, m.payload)
        out.append(dict(floor=v[2], layer=v[3], ut_x=v[4], ut_z=v[5], gen=v[6], enabled=v[7], rev=v[8], ftr=v[10], items=list(v[12:39])))
    return out


def select(c, ux, uz, gen, ftr, wait=3.0):
    """DRESSER_SELECT of the outdoor piece whose ORIGIN is (ux, uz): its STATE, or None (no answer)"""
    mark = c.inbox.mark()
    c.send_reliable(struct.pack(QUERY_FMT, MSG_QUERY, OUTDOOR, 0, ux, uz, gen, ftr))
    c.hub.wait_until(lambda: any(s["gen"] == gen and s["floor"] == OUTDOOR for s in states(c, mark)), wait)
    s = [s for s in states(c, mark) if s["gen"] == gen and s["floor"] == OUTDOOR]
    return s[-1] if s else None


def probe(c):
    mark = c.inbox.mark()
    c.send_reliable(struct.pack(QUERY_FMT, MSG_QUERY, 0xFF, 0, 0, 0, 0, 0))
    c.hub.wait_until(lambda: len(states(c, mark)) > 0, 3.0)


def dtxn(run, c, op, pocket, item, pool_slot, pre, gen):
    sent = c.send_txn_commit(K_DRESSER, run.fresh_rid(), D_NONE if op == OP_PUT else D_POCKET, pocket, item, pre=pre, aux_cond=op, aux_item=pool_slot, flags=gen)
    return sent, c.wait_txn_result(sent.seq, 4.0)


def put(run, c, item, gen):
    s = OF.free_slot(OF.pre_with(c))
    pre = OF.pre_with(c, {s: item})
    return dtxn(run, c, OP_PUT, s, item, 0xFF, pre, gen)


def take(run, c, item, pool_slot, gen):
    s = OF.free_slot(OF.pre_with(c))
    return dtxn(run, c, OP_TAKE, s, item, pool_slot, None, gen)


def caps_total(c):
    t = [tot for k, _n, _f, _g, tot, _e in C2.of2_msgs(c) if k == C2.MK_CAPS]
    return (t[-1] & 1) if t else None  # bit 0 = C3 outdoor storage (bit 1 = roommates, test_roommates_protocol.py)


def phase_main(run):
    ck = run.check
    a = run.ready("A", run.r1)
    a.wait_record_synced(30.0)
    g = OF.guest(run, "OFGST3", 0x6F31)
    off0 = len(run.log())
    ck("CAPS3 the 102 CAPS carries total bit 0 = outdoor storage (resident and guest)", OF.wait(a, lambda: caps_total(a) == 1) and OF.wait(g, lambda: caps_total(g) == 1))
    probe(a)
    cands = C2.box_cands(a, 2, 3)
    (o, r) = C2.place_at(run, a, DRESSER, 0, cands, (0, 1), probe=True)
    ck("P1 a resident places the 2x1 DRESSER 0x%04X (room to push north)" % DRESSER, o is not None)
    if o is None:
        print("   last result: %s" % (r,))
        return
    ux, uz = o
    C2.stand(a, ux, uz + 1, N)
    st = select(a, ux, uz, 1, DRESSER)
    ck("S1 SELECT floor 0xFC of the origin -> STATE (floor 0xFC, the origin, gen 1, the base id, empty)",
       st is not None and st["ut_x"] == ux and st["ut_z"] == uz and st["ftr"] == DRESSER and st["enabled"] == 1 and not any(st["items"]))
    C2.stand(g, ux, uz + 1, N)
    stg = select(g, ux, uz, 7, DRESSER)
    ck("S2 C3b: a GUEST's outdoor SELECT from the front is answered (the same shared, empty storage)", stg is not None and stg["ut_x"] == ux and not any(stg["items"]))
    ck("S3 a SELECT of the second (filler) cell / a wrong id is not answered", select(a, ux + 1, uz, 2, DRESSER, 2.0) is None and select(a, ux, uz, 3, 0x1068, 2.0) is None)
    C2.stand(a, ux, uz + 8, N)
    ck("S4 a SELECT from 7 tiles away is not answered (next to the piece only)", select(a, ux, uz, 4, DRESSER, 2.0) is None and
       re.search(r"OUTDOOR DRESSER_SELECT refused .*not next to the piece", run.log()) is not None)
    # ---- C3b front rule: the 2x1 rot 0 = cells (ux, uz) + (ux + 1, uz), its front faces +z (south): only the row (ux..ux+1, uz+1) facing north opens
    off_f = len(run.log())
    C2.stand(a, ux + 1, uz - 1, S)
    back_a = select(a, ux, uz, 20, DRESSER, 2.0)
    C2.stand(g, ux, uz - 1, S)
    back_g = select(g, ux, uz, 21, DRESSER, 2.0)
    C2.stand(a, ux - 1, uz, E)
    side_w = select(a, ux, uz, 22, DRESSER, 2.0)
    C2.stand(a, ux + 2, uz, W)
    side_e = select(a, ux, uz, 23, DRESSER, 2.0)
    ck("F1 from BEHIND (resident and guest) and from either SIDE, facing the piece: never answered (the host's front rule, logged)",
       back_a is None and back_g is None and side_w is None and side_e is None and
       run.n_log(r"OUTDOOR DRESSER_SELECT refused .*not in front of the piece", off_f) >= 4)
    C2.stand(a, ux + 1, uz + 1, S)
    ck("F2 in the front row but facing AWAY from the piece: not answered", select(a, ux, uz, 24, DRESSER, 2.0) is None)
    C2.stand(a, ux + 1, uz + 1, N)
    f3 = select(a, ux, uz, 25, DRESSER)
    C2.stand(a, ux, uz + 2, N)
    f4 = select(a, ux, uz, 26, DRESSER)
    ck("F3 the second front tile (ux + 1, uz + 1) facing north is answered (the whole width), and so is two tiles out (the host's lag tolerance)", f3 is not None and f4 is not None)
    C2.stand(a, ux, uz + 1, N)
    st = select(a, ux, uz, 5, DRESSER)
    pre0 = OF.pre_with(a)
    s1, r1 = put(run, a, ITEMS[0], 5)
    s2, r2 = put(run, a, ITEMS[1], 5)
    ck("T1 two PUTs with the outdoor selection: APPLIED", r1 is not None and r1.outcome == APPLIED and r2 is not None and r2.outcome == APPLIED)
    a.resend_txn(s2)
    rr = a.wait_txn_result(s2.seq, 4.0, nth=2)
    ck("T2 the identical resend is APPLIED/REPLAYED (never executed twice)", rr is not None and rr.outcome == APPLIED and rr.reason == R["REPLAYED"])
    st = select(a, ux, uz, 6, DRESSER)
    ck("T3 the STATE lists both items", st is not None and sorted(x for x in st["items"] if x) == sorted(ITEMS[:2]))
    b = run.ready("B", run.r2)
    b.wait_record_synced(30.0)
    probe(b)
    C2.stand(b, ux + 1, uz + 1, N)
    stb = select(b, ux, uz, 1, DRESSER)
    ck("SH1 a SECOND resident selects the same piece (from the other front tile): the same shared storage (both items)",
       stb is not None and sorted(x for x in stb["items"] if x) == sorted(ITEMS[:2]))
    _s, rb = put(run, b, ITEMS[2], 1)
    ck("SH2 ... and PUTs a third item: APPLIED", rb is not None and rb.outcome == APPLIED)
    st = select(a, ux, uz, 7, DRESSER)
    _s, ra = put(run, a, ITEMS[3], 7)
    ck("SH3 the first resident PUTs again after the second one: APPLIED (shared record: not FENCED)", ra is not None and ra.outcome == APPLIED)
    # ---- C3b: a GUEST uses the shared storage like a resident (PUT / replay / TAKE), but still never moves / rotates / picks up the piece
    off_g = len(run.log())
    C2.stand(g, ux, uz + 1, N)
    stg = select(g, ux, uz, 8, DRESSER)
    ck("GU1 the guest's SELECT shows the residents' 4 items", stg is not None and sorted(x for x in stg["items"] if x) == sorted(ITEMS))
    gs, rg = put(run, g, GUEST_ITEM, 8)
    ck("GU2 the guest PUTs an item: APPLIED (its pocket emptied in the post-image)", rg is not None and rg.outcome == APPLIED and GUEST_ITEM not in list(rg.post_pockets))
    g.resend_txn(gs)
    rr = g.wait_txn_result(gs.seq, 4.0, nth=2)
    ck("GU3 the guest's identical resend is APPLIED/REPLAYED (journalled per identity)", rr is not None and rr.outcome == APPLIED and rr.reason == R["REPLAYED"])
    stg = select(g, ux, uz, 9, DRESSER)
    ck("GU4 the shared STATE now holds 5 items", stg is not None and len([x for x in stg["items"] if x]) == 5 and GUEST_ITEM in stg["items"])
    select(a, ux, uz, 10, DRESSER)
    _s, ra = put(run, a, RES_ITEM, 10)
    st = select(a, ux, uz, 12, DRESSER)
    rt = None
    if ra is not None and ra.outcome == APPLIED and st is not None and RES_ITEM in st["items"]:
        _s, rt = take(run, a, RES_ITEM, st["items"].index(RES_ITEM), 12)
    ck("GU5 a resident PUT + TAKE after the guest: APPLIED (nobody FENCED by the other identity)", ra is not None and ra.outcome == APPLIED and rt is not None and rt.outcome == APPLIED)
    stg = select(g, ux, uz, 11, DRESSER)
    _s, rgt = take(run, g, GUEST_ITEM, stg["items"].index(GUEST_ITEM) if stg and GUEST_ITEM in stg["items"] else 0, 11)
    ck("GU6 the guest TAKEs its item back: APPLIED (the item in its post-image), the storage holds the residents' 4 again",
       rgt is not None and rgt.outcome == APPLIED and GUEST_ITEM in list(rgt.post_pockets))
    _s, rs = dtxn(run, g, OP_TAKE, OF.free_slot(OF.pre_with(g)), ITEMS[0], 0, None, 99)
    ck("GU7 a guest commit with a stale selection generation: REJECTED PRECOND (journalled, nothing changes)", rs is not None and rs.outcome != APPLIED and rs.reason == R["PRECOND"])
    _x, rgr = C2.txn(run, g, ROTATE, D_NONE, 0, DRESSER, 1, ux, uz)
    sg3 = OF.free_slot(OF.pre_with(g))
    _x, rgp = C2.txn(run, g, PICKUP, D_POCKET, sg3, DRESSER, 0, ux, uz)
    ck("GU8 the guest still may not ROTATE / PICK UP the placed piece: NOT_ALLOWED 56 (C1), the cells unchanged",
       OF.is_rej(rgr, 56) and OF.is_rej(rgp, 56) and OF.tile_val(a, ux, uz) == DRESSER)
    ck("GU9 no guest refusal 'a guest has no house' for its outdoor kind 18", run.n_log(r"a guest has no house", off_g) == 0)
    # ---- ROTATE keeps the key
    _x, r = C2.txn(run, a, ROTATE, D_NONE, 0, DRESSER, 1, ux, uz)
    _x, r2 = C2.txn(run, a, ROTATE, D_NONE, 0, DRESSER | 1, 0, ux, uz)
    st = select(a, ux, uz, 8, DRESSER)
    ck("R1 ROTATE there and back: APPLIED, the storage stays with the piece (rotation masked: 4 items)",
       r is not None and r.outcome == APPLIED and r2 is not None and r2.outcome == APPLIED and st is not None and len([x for x in st["items"] if x]) == 4)
    # ---- MOVE re-keys
    C2.stand(b, ux + 3, uz + 3, N)
    C2.stand(a, ux, uz + 1, N)
    _x, r = C2.txn(run, a, MOVE, D_NONE, 0, DRESSER, N, ux, uz)
    ck("M1 PUSH north: APPLIED and the host RE-KEYED the storage to the new origin (log)", r is not None and r.outcome == APPLIED and
       re.search(r"outdoor storage of ftr 0x%04X re-keyed \(%d,%d\) -> \(%d,%d\) with the piece \(4 item" % (DRESSER, ux, uz, ux, uz - 1), run.log()) is not None)
    uz -= 1
    C2.stand(a, ux, uz + 1, N)
    ck("M2 the OLD origin has no storage piece any more (SELECT not answered)", select(a, ux, uz + 1, 9, DRESSER, 2.0) is None)
    st = select(a, ux, uz, 10, DRESSER)
    ck("M3 the NEW origin's STATE has all 4 items (the contents followed the piece)", st is not None and sorted(x for x in st["items"] if x) == sorted(ITEMS))
    # ---- PICKUP refused while non-empty
    s3 = OF.free_slot(OF.pre_with(a))
    _x, r = C2.txn(run, a, PICKUP, D_POCKET, s3, DRESSER, 0, ux, uz)
    ck("K1 kind 22 PICKUP while the storage holds items: REJECTED 62 OFURN_NOT_EMPTY, the cells unchanged",
       OF.is_rej(r, NOT_EMPTY) and OF.tile_val(a, ux, uz) == DRESSER and OF.tile_val(a, ux + 1, uz) == C2.FILLER)
    C2.stand(b, ux, uz + 1, N)
    prov, _s, rv = b.txn_pickup(ux, uz, run.fresh_rid(), timeout=4.0)
    L.pump_sleep(1.0)
    ck("K2 a resident's VANILLA pickup of the origin leaf (a 0.3.0 client) is refused; the piece and its storage stay",
       not (prov is not None and prov.accepted and rv is not None and rv.outcome == APPLIED) and OF.tile_val(b, ux, uz) == DRESSER and
       re.search(r"vanilla pickup of the PLACED storage piece at \(%d,%d\) REFUSED: its outdoor storage holds 4 item" % (ux, uz), run.log()) is not None)
    # ---- take everything back (conservation), then the pickup works
    C2.stand(a, ux, uz + 1, N)
    st = select(a, ux, uz, 11, DRESSER)
    ok = st is not None
    for i, it in enumerate(st["items"] if st else []):
        if it:
            _s, rt = take(run, a, it, i, 11)
            ok = ok and rt is not None and rt.outcome == APPLIED
    st = select(a, ux, uz, 12, DRESSER)
    pk = OF.pre_with(a)[0]
    ck("T4 every item TAKEN back by A: APPLIED, the storage empty, A's pockets hold all 4 (conservation over the two residents' puts)",
       ok and st is not None and not any(st["items"]) and all(it in pk for it in ITEMS))
    s3 = OF.free_slot(OF.pre_with(a))
    _x, r = C2.txn(run, a, PICKUP, D_POCKET, s3, DRESSER, 0, ux, uz)
    ck("K3 the EMPTY dresser's PICKUP: APPLIED (the base item in the pocket), both cells EMPTY",
       r is not None and r.outcome == APPLIED and OF.wait(a, lambda: OF.tile_val(a, ux, uz) == 0 and OF.tile_val(a, ux + 1, uz) == 0))
    ck("NV no record-protocol violation was counted", run.n_log(r"\[NET\]\[REC\] host: peer \d+ violation", off0) == 0)
    _ = pre0


def phase_gc(run):
    ck = run.check
    m = None
    w = 0.0
    while m is None and w < 40:
        m = re.search(r"seeded placed piece at \((\d+),(\d+)\) item 0x%04X rot 0 -> placed" % DRESSER, run.log())
        L.pump_sleep(0.5)
        w += 0.5
    ck("G0 the host seeded a placed dresser (AC_TEST_OFURN_SEED)", m is not None)
    if m is None:
        return
    ux, uz = int(m.group(1)), int(m.group(2))
    a = run.ready("A", run.r1)
    a.wait_record_synced(30.0)
    probe(a)
    C2.stand(a, ux, uz + 1, N)
    st = select(a, ux, uz, 1, DRESSER)
    _s, r = put(run, a, ITEMS[0], 1)
    ck("G1 SELECT + PUT of one item into the seeded dresser: APPLIED", st is not None and r is not None and r.outcome == APPLIED)
    ok = TS.wait_log(run, r"outdoor storage at \(%d,%d\) ftr 0x%04X lost its piece with 1 item\(s\): kept as an ORPHAN" % (ux, uz, DRESSER), 60.0)
    ck("G2 the origin overwritten behind the system's back: the GC drops the marker, the storage is kept as an ORPHAN (log)", ok)
    cands = C2.box_cands(a, 2, 3, skip=((ux, uz),))
    (o, r) = C2.place_at(run, a, DRESSER, 0, cands, (0, 1))
    ck("G3 a new dresser of the same id placed elsewhere: APPLIED", o is not None)
    if o is None:
        return
    nx, nz = o
    ck("G4 ... the orphaned storage is RE-ATTACHED to it (log)", TS.wait_log(run, r"outdoor storage RE-ATTACHED: the orphaned storage of ftr 0x%04X from \(%d,%d\) \(1 item\(s\)\) is now in the piece at \(%d,%d\)"
                                                                % (DRESSER, ux, uz, nx, nz), 10.0))
    C2.stand(a, nx, nz + 1, N)
    st = select(a, nx, nz, 2, DRESSER)
    ck("G5 its SELECT shows the item", st is not None and ITEMS[0] in st["items"])
    if st is not None and ITEMS[0] in st["items"]:
        _s, rt = take(run, a, ITEMS[0], st["items"].index(ITEMS[0]), 2)
        s3 = OF.free_slot(OF.pre_with(a))
        _x, rk = C2.txn(run, a, PICKUP, D_POCKET, s3, DRESSER, 0, nx, nz)
        ck("G6 TAKE + PICKUP clean up: APPLIED", rt is not None and rt.outcome == APPLIED and rk is not None and rk.outcome == APPLIED)


def phase_off(run):
    ck = run.check
    a = run.ready("A", run.r1)
    a.wait_record_synced(30.0)
    ck("O1 storage OFF: the 102 CAPS total is 0", OF.wait(a, lambda: caps_total(a) == 0))
    cands = C2.box_cands(a, 2, 3)
    (o, r) = C2.place_at(run, a, DRESSER, 0, cands, (0, 1))
    ck("O2 a dresser still places", o is not None)
    if o is None:
        return
    ux, uz = o
    C2.stand(a, ux, uz + 1, N)
    ck("O3 an outdoor SELECT is never answered", select(a, ux, uz, 1, DRESSER, 2.0) is None)
    s3 = OF.free_slot(OF.pre_with(a))
    _x, r = C2.txn(run, a, PICKUP, D_POCKET, s3, DRESSER, 0, ux, uz)
    ck("O4 the dresser picks up normally", r is not None and r.outcome == APPLIED)


def phase_restart(results, ip, port, restore, snap_gci):
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    os.environ["AC_TEST_OFURN_SEED"] = "0x%04X,0" % DRESSER
    h1, ok = TS.start_host(ip, port, "OF3RS1", [], results)
    ux = uz = None
    try:
        if ok:
            m = h1.wait_for_log(r"seeded placed piece at \((\d+),(\d+)\) item 0x%04X rot 0 -> placed" % DRESSER, 40.0)
            ck("RS1 host 1 seeded a placed dresser", m is not None)
            if m is not None:
                ux, uz = int(m.group(1)), int(m.group(2))
                run = TP.Run(ip, port, h1, results, snap_gci, host_slot, residents[:3])
                try:
                    a = run.ready("A", run.r1)
                    a.wait_record_synced(30.0)
                    probe(a)
                    C2.stand(a, ux, uz + 1, N)
                    st = select(a, ux, uz, 1, DRESSER)
                    _s, r = put(run, a, ITEMS[1], 1)
                    ck("RS2 host 1: SELECT + PUT into the outdoor dresser: APPLIED", st is not None and r is not None and r.outcome == APPLIED)
                    L.pump_sleep(2.0)
                finally:
                    for cl in run.clients:
                        try:
                            cl.close()
                        except Exception:  # noqa: BLE001
                            pass
    finally:
        h1.stop()
        os.environ.pop("AC_TEST_OFURN_SEED", None)
    if ux is None:
        restore()
        return
    h2, ok = TS.start_host(ip, port + 1, "OF3RS2", [], results)
    try:
        if ok:
            ld = h2.wait_for_log(r"outdoor_furniture\.dat loaded: (\d+) placed piece", 40.0)
            run = TP.Run(ip, port + 1, h2, results, snap_gci, host_slot, residents[:3])
            try:
                a = run.ready("A", run.r1)
                a.wait_record_synced(30.0)
                probe(a)
                C2.stand(a, ux, uz + 1, N)
                st = select(a, ux, uz, 2, DRESSER)
                if st is not None:
                    ck("RS3 host 2 (restart): the marker loaded and the SAME piece's STATE still holds the item", ld is not None and ITEMS[1] in st["items"])
                else:
                    # host 1 stopped before its town save: the seeded tile is not in host 2's town, so the GC drops the marker -- the storage must survive as an ORPHAN
                    orph = TS.wait_log(run, r"outdoor storage at \(%d,%d\) ftr 0x%04X lost its piece with 1 item\(s\): kept as an ORPHAN" % (ux, uz, DRESSER), 10.0)
                    (o, _r) = C2.place_at(run, a, DRESSER, 0, C2.box_cands(a, 2, 3), (0, 1))
                    st2 = None
                    if o is not None:
                        C2.stand(a, o[0], o[1] + 1, N)
                        st2 = select(a, o[0], o[1], 3, DRESSER)
                    ck("RS3 host 2 (restart; host 1's town save did not keep the tile): the persisted storage is an ORPHAN with its item and RE-ATTACHES to a new dresser",
                       ld is not None and orph and st2 is not None and ITEMS[1] in st2["items"])
            finally:
                for cl in run.clients:
                    try:
                        cl.close()
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        rc = h2.stop()
        ck("RS host 2 did not crash", rc is None)
        restore()


def run_all(port, only, results):
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_of3"):
        print("REFUSING: this test mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_of3_<name> copy only (NET_SPIKE_GAME_BIN=<abs path>)", file=sys.stderr)
        return 2
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "ofurn_c3_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    keys = ("AC_TEST_OFURN_LEAF", "AC_TEST_OPS", "AC_TEST_OFURN_SEED", "AC_TEST_OFURN_GC_MS", "AC_TEST_OFURN_GC_CELL", "AC_MP_OUTDOOR_FURNITURE", "AC_MP_DRESSER_STORAGE")
    phases = (("MAIN", dict(STORAGE_ENV), phase_main),
              ("GC", dict(STORAGE_ENV, AC_TEST_OFURN_SEED="0x%04X,0" % DRESSER, AC_TEST_OFURN_GC_MS="50000"), phase_gc),
              ("OFF", {"AC_MP_DRESSER_STORAGE": "0"}, phase_off))
    try:
        for i, (tag, env, body) in enumerate(phases):
            if only and tag not in only:
                continue
            for k in keys:
                os.environ.pop(k, None)
            os.environ.update(env)
            TS.run_phase(results, ip, port + i, "OF3" + tag, [], body, snap_gci, restore)
        for k in keys:
            os.environ.pop(k, None)
        if not only or "RS" in only:
            os.environ.update(STORAGE_ENV)
            phase_restart(results, ip, port + 20, restore, snap_gci)
    finally:
        for k in keys:
            os.environ.pop(k, None)
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=39100)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    if not (39000 <= args.port <= 39470):
        print("ports 39000-39499 only", file=sys.stderr)
        return 2
    results = []
    rc = run_all(args.port, set(x for x in args.only.split(",") if x), results)
    if rc is not None:
        return rc
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
