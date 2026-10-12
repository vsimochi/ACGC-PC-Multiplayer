#!/usr/bin/env python3
"""test_dresser_storage_protocol.py - per-DRESSER storage v2 (TXN kind 18, msgs 83 DRESSER_STATE / 84 DRESSER_SELECT) against a REAL host (protocol FakeClients).

The host default is OFF during the redesign: this test sets AC_MP_DRESSER_STORAGE=1 for the host (run with AC_MP_DRESSER_STORAGE=0 for the OFF checks).
Each dresser = (owner house, floor, layer, cell, furniture id): two furniture cells of the resident's OWN house (read from the fixture save) are used as dressers A / B.
  D1 capability probe (84 floor 0xFF) -> STATE enabled
  D2 SELECT A (gen 1) -> STATE of A (empty); PUT 5 items with tag.flags = 1 -> APPLIED, pocket emptied, A lists them
  D3 the identical PUT retransmitted -> REPLAYED, nothing executed twice
  D4 SELECT B (gen 2) -> STATE of B is EMPTY (independent); TAKE of an A item through B -> NOT_AVAILABLE; PUT 1 item into B -> APPLIED; A still holds 5
  D5 a COMMIT with the OLD generation (1) while B is selected -> REJECTED PRECOND, nothing moves
  D6 SELECT of a cell that holds no furniture -> no answer; a COMMIT then -> REJECTED PRECOND
  D7 capacity: A filled to 24 extra (27 with the vanilla 3) -> the 25th PUT is REJECTED NO_ROOM (22), the item stays in the pocket
  D8 host restart: A still 24, B still 1; a pre-restart TAKE retransmitted WITHOUT a selection -> REPLAYED from the PERSISTED journal, never re-executed
  D9 conservation over pockets + A + B
Run on a disposable fixture: NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_w3> python test_dresser_storage_protocol.py --port 16450
"""
import argparse
import os
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if os.environ.get("AC_MP_DRESSER_STORAGE") is None:
    os.environ["AC_MP_DRESSER_STORAGE"] = "1"  # the host default is OFF during the redesign
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_work_mode_protocol as W  # noqa: E402
from test_txn_protocol import APPLIED, REJECTED, R  # noqa: E402

K_DRESSER = 18
D_NONE, D_POCKET = 0, 1
OP_PUT, OP_TAKE, OP_MOVE, OP_SWAP = 1, 2, 3, 4
MSG_STATE, MSG_QUERY = 83, 84
STATE_FMT = "<BBBBBBBBIIHH27HH"  # 6r: v3, 27 slots + explicit pad
STATE_SIZE = struct.calcsize(STATE_FMT)
assert STATE_SIZE == 76
QUERY_FMT = "<BBBBBBH"
assert struct.calcsize(QUERY_FMT) == 8
SCENE_MY_ROOM_S = 20
REASON_NOT_AVAILABLE, REASON_PRECOND, REASON_NO_ROOM = 16, 9, 22
ITEMS = [0x2401, 0x2402, 0x2403, 0x2404, 0x2405]
SEED = []  # [(cell, item)] A, C (identical twin of A), B: set by the runner


def is_state(m):
    return m.channel == L.CH_RELIABLE and len(m.payload) == STATE_SIZE and m.payload[0] == MSG_STATE


def states(c, since=None):
    out = []
    for m in c.inbox.peek_all(is_state, since=since):
        v = struct.unpack(STATE_FMT, m.payload)
        out.append(dict(version=v[1], floor=v[2], layer=v[3], ut_x=v[4], ut_z=v[5], gen=v[6], enabled=v[7], rev=v[8], digest=v[9], ftr=v[10], nslots=v[11],
                        items=list(v[12:39])))
    return out


def enter_room(c, owner):
    c._dresser_scene_seq = getattr(c, "_dresser_scene_seq", 0) + 1
    c.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, SCENE_MY_ROOM_S, 0, owner, 0, c._dresser_scene_seq))
    L.pump_sleep(0.6)


def query(c, floor=0xFF, layer=0, cell=0, gen=0, ftr=0, wait=3.0):
    mark = c.inbox.mark()
    c.send_reliable(struct.pack(QUERY_FMT, MSG_QUERY, floor, layer, cell % 16, cell // 16, gen, ftr))
    c.hub.wait_until(lambda: len(states(c, mark)) > 0, wait)
    s = states(c, mark)
    return s[-1] if s else None


def txn(run, c, op, pocket, item, pool_slot, pre, gen):
    mark = c.inbox.mark()
    sent = c.send_txn_commit(K_DRESSER, run.fresh_rid(), D_NONE if op == OP_PUT else D_POCKET, pocket, item, pre=pre, aux_cond=op, aux_item=pool_slot, flags=gen)
    return mark, sent, c.wait_txn_result(sent.seq, 4.0)


def put(run, a, it, gen):
    pk, conds, wallet = a.txn_pre_image()
    slot = W.free_slot(a)
    pre = (tuple(it if i == slot else p for i, p in enumerate(pk)), conds & ~(3 << (2 * slot)), wallet)
    return txn(run, a, OP_PUT, slot, it, 0xFF, pre, gen) + (slot,)


def furniture_cells(h):
    img = L.house_from_gci(L.GAME_BIN_DIR, h)
    out = []
    for c in range(L.HOUSE_CELLS):
        v = L.house_cell(img, 0, 0, c)
        if (v >> 12) in (1, 3) and (v & 3) == 0:  # FTR0 / FTR1 furniture ids, rotation 0 (a pocket holds the rotation-free id)
            out.append((c, v))
    return out


def phase(run):
    ck = run.check
    off_mode = os.environ.get("AC_MP_DRESSER_STORAGE") == "0"
    a = run.ready("A", run.r1)
    ck("setup: A READY and SYNCED", a.rec_synced)
    enter_room(a, run.r1)
    st = query(a)
    if off_mode:
        ck("OFF: the host does not answer the capability probe (old-host behaviour: no extra storage offered)", st is None)
        mark, _s, r, _sl = put(run, a, ITEMS[0], 1)
        ck("OFF: a kind-18 PUT is REJECTED DRESSER_DISABLED (37), nothing changes", r is not None and r.outcome == REJECTED and r.reason == 37)
        # 6l OFF: the 27-slot (keep-item) rule never applies: put a vanilla keep item into a seeded chest, then move it -> both APPLIED
        h = run.r1
        (cb0, fb0) = SEED[2]
        a.hub.wait_until(lambda: a.house_canon(h) is not None and L.house_cell(a.house_canon(h), 0, 0, cb0) == fb0, 10.0)
        img = a.house_canon(h)
        if img is None or L.house_cell(img, 0, 0, cb0) != fb0:
            ck("OFF M8 skipped: the seeded chest is not in the canonical image (no seeding with the feature off)", True)
            return
        pk, conds, wallet = L.record_inventory(a.rec_local)
        ks = next((i for i, x in enumerate(pk) if x != 0 and (x >> 12) == 2), None)
        free = [c for c in range(L.HOUSE_CELLS) if all(L.house_cell(img, 0, l, c) == 0 for l in (0, 1, 2, 3)) and c > 40 and c != cb0]
        if ks is None or not free:
            ck("OFF M8 skipped: no ordinary pocket item / free cell", True)
            return
        kit = pk[ks]
        pk2 = list(pk)
        pk2[ks] = 0
        img1 = L.house_set_cell(img, 0, 1, cb0, kit)
        x = a.commit_house(h, img1, L.record_set_inventory(a.rec_local, pk2, conds & ~(3 << (2 * ks)), wallet))
        ack = a.wait_house_ack(x, timeout=5.0)
        ck("OFF M8 setup: a vanilla keep item 0x%04X put into the chest at cell %d (APPLIED)" % (kit, cb0), ack is not None and ack.status == 0)
        img2 = L.house_set_cell(L.house_set_cell(L.house_set_cell(L.house_set_cell(img1, 0, 0, cb0, 0), 0, 1, cb0, 0), 0, 0, free[0], fb0), 0, 1, free[0], kit)
        off = len(run.log())
        x = a.commit_house(h, img2)
        ack = a.wait_house_ack(x, timeout=5.0)
        ck("OFF M8 a MOVE of the chest holding a keep item is APPLIED (feature off: no 27-slot lock, vanilla behaviour)",
           ack is not None and ack.status == 0 and run.n_log(r"REFUSED \(27-slot lock", off) == 0)
        return
    ck("D1 probe answered: STATE v3, 27 host slots, enabled", st is not None and st["version"] == 3 and st["nslots"] == 27 and st["enabled"] == 1 and st["floor"] == 0xFF)
    # v2: only vanilla ITEM-STORAGE furniture (DRAWERS / WARDROBE / CLOSET profiles) is a dresser; the fixture has none, so the host seeds three
    # (AC_TEST_DRESSER_SEED, TEST-ONLY) at the first probe: A and C = two IDENTICAL chests 0x1068, B = another chest 0x1070
    (ca, fa), (cc, fc), (cb, fb) = SEED[0], SEED[1], SEED[2]
    a.hub.wait_until(lambda: a.house_canon(run.r1) is not None and L.house_cell(a.house_canon(run.r1), 0, 0, cb) == fb, 10.0)
    canon = a.house_canon(run.r1)
    ck("setup: the host seeded the three storage pieces into house %d (pushed canonical copy: A cell %d, C cell %d = 0x%04X, B cell %d = 0x%04X)" % (run.r1, ca, cc, fa, cb, fb),
       canon is not None and L.house_cell(canon, 0, 0, ca) == fa and L.house_cell(canon, 0, 0, cc) == fc and L.house_cell(canon, 0, 0, cb) == fb)
    ck("setup: the host log confirms they ARE item storage (the vanilla profile predicate)", run.n_log(r"seeded furniture 0x%04X \(item storage 1\)" % fa) == 2 and
       run.n_log(r"seeded furniture 0x%04X \(item storage 1\)" % fb) == 1)
    plain = [(c, v) for c, v in furniture_cells(run.r1) if v not in (fa, fb)]
    if plain:
        ck("D0 SELECT of a furniture WITHOUT item storage (0x%04X at cell %d) -> no answer" % (plain[0][1], plain[0][0]), query(a, 0, 0, plain[0][0], 9, plain[0][1], wait=2.0) is None)
    sa = query(a, 0, 0, ca, 1, fa)
    ck("D2 SELECT A (cell %d item 0x%04X gen 1) -> STATE of A, empty" % (ca, fa), sa is not None and sa["gen"] == 1 and sa["ut_x"] == ca % 16 and sa["ut_z"] == ca // 16 and not any(sa["items"]))
    off = len(run.log())
    sent_put = None
    for it in ITEMS:
        mark, sent, r, slot = put(run, a, it, 1)
        run.tx_ok("D2 PUT 0x%04X into A" % it, r, APPLIED, R["NONE"])
        sent_put = sent_put or sent
    sa = (states(a) or [None])[-1]
    ck("D2 A lists the 5 items", sa is not None and sorted(x for x in sa["items"] if x) == sorted(ITEMS))
    a.resend_txn(sent_put)
    rr = a.wait_txn_result(sent_put.seq, 4.0, nth=2)
    ck("D3 the identical PUT retransmitted is REPLAYED", rr is not None and rr.outcome == APPLIED and rr.reason == R["REPLAYED"])
    L.pump_sleep(0.4)
    ck("D3 exactly 5 PUT commit lines", run.n_log(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ PUT ", off) == 5)
    sb = query(a, 0, 0, cb, 2, fb)
    ck("D4 SELECT B (cell %d gen 2) -> B is EMPTY (independent of A)" % cb, sb is not None and sb["gen"] == 2 and not any(sb["items"]))
    free = W.free_slot(a)
    mark, _s, r = txn(run, a, OP_TAKE, free, ITEMS[0], 0, None, 2)
    ck("D4 TAKE of an A item through B -> NOT_AVAILABLE (16)", r is not None and r.outcome == REJECTED and r.reason == REASON_NOT_AVAILABLE)
    mark, _s, r, _sl = put(run, a, 0x2406, 2)
    run.tx_ok("D4 PUT 0x2406 into B", r, APPLIED, R["NONE"])
    mark, _s, r, _sl = put(run, a, 0x2407, 1)
    ck("D5 a COMMIT with the OLD generation 1 while B is selected -> REJECTED PRECOND (9)", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    empty = [c for c in range(L.HOUSE_CELLS) if L.house_cell(L.house_from_gci(L.GAME_BIN_DIR, run.r1), 0, 0, c) == 0 and c > 40]
    sx = query(a, 0, 0, empty[0], 3, fa, wait=2.0)
    ck("D6 SELECT of a cell without that furniture -> no answer", sx is None)
    mark, _s, r, _sl = put(run, a, 0x2407, 3)
    ck("D6 ... and a COMMIT with that generation -> REJECTED PRECOND", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    sa = query(a, 0, 0, ca, 4, fa)
    ck("D7 re-select A (gen 4): still the 5 items", sa is not None and sorted(x for x in sa["items"] if x) == sorted(ITEMS))
    fill = [0x2410 + i for i in range(22)]  # 6r: 5 + 22 = 27
    ok_all = True
    for it in fill:
        mark, _s, r, _sl = put(run, a, it, 4)
        ok_all = ok_all and r is not None and r.outcome == APPLIED
    ck("D7 A filled to 27 host slots (6r)", ok_all and sum(1 for x in (states(a) or [{"items": []}])[-1]["items"] if x) == 27)
    mark, _s, r, slot = put(run, a, 0x2430, 4)
    ck("D7 the 28th PUT is REJECTED NO_ROOM (22): full storage refuses", r is not None and r.outcome == REJECTED and r.reason == REASON_NO_ROOM)
    # take one back so a pre-restart TAKE can be replayed later
    sa = (states(a) or [None])[-1]
    ps = sa["items"].index(ITEMS[1])
    free = W.free_slot(a)
    mark, sent_take, r = txn(run, a, OP_TAKE, free, ITEMS[1], ps, None, 4)
    run.tx_ok("D7 TAKE 0x%04X back from A" % ITEMS[1], r, APPLIED, R["NONE"])
    rev_a = (states(a) or [None])[-1]["rev"]
    # ---- Q (6q, S2 + S3; WRITTEN, NOT RUN when it was written: needs a game process, the user's session was active): the host lets an OCCUPIED dresser be
    # MOVED and ROTATED (the house-commit hook carries its storage: re-key / fingerprint follow / orphan + re-attach), refuses only its REMOVAL (pickup,
    # extras or keep items), and executes the kind-18 op 3 MOVE (move / swap of two pool slots, pockets unchanged, journalled + replayed).
    # This FakeClient has no client gate (like an old client).
    cur_a, cur_b, cur_c = ca, cb, cc
    h = run.r1
    a.hub.wait_until(lambda: a.house_canon(h) is not None, 8.0)
    img0 = a.house_canon(h)
    ck("Q setup: A holds the canonical image of its house %d" % h, img0 is not None and L.house_cell(img0, 0, 0, ca) == fa)
    if img0 is not None:
        emp = [c for c in range(L.HOUSE_CELLS) if all(L.house_cell(img0, 0, l, c) == 0 for l in (0, 1, 2, 3)) and c > 40 and c not in (ca, cb, cc)]
        e1, e2, e3, e4 = emp[0], emp[1], emp[2], emp[3]

        def commit(img, rec=None):
            x = a.commit_house(h, img, rec) if rec is not None else a.commit_house(h, img)
            ack = a.wait_house_ack(x, timeout=5.0)
            L.pump_sleep(0.5)
            return ack

        def applied(ack):
            return ack is not None and ack.status == 0

        def refused_rm(ack, off, cell, what="extra"):
            return (ack is not None and ack.status == 4 and ack.detail == (0x2000 | (cell & 0xFF)) and
                    run.n_log(r"holds \d+ %s item\(s\) and the commit REMOVES it: REFUSED" % ("vanilla keep" if what == "keep" else "extra"), off) == 1)

        def move_op(src, dst, item, gen):
            sent = a.send_txn_commit(K_DRESSER, run.fresh_rid(), D_NONE, 0, item, pre=None, aux_cond=OP_MOVE, aux_item=src | (dst << 8), flags=gen)
            return sent, a.wait_txn_result(sent.seq, 4.0)

        def n_items(st):
            return 0 if st is None else sum(1 for x in st["items"] if x)

        # Q1 MOVE of A (26 extras) ca -> e1: APPLIED, the storage follows (re-keyed)
        off3 = len(run.log())
        img1 = L.house_set_cell(L.house_set_cell(img0, 0, 0, ca, 0), 0, 0, e1, fa)
        ack = commit(img1)
        ck("Q1 MOVE of dresser A holding 26 extras (cell %d -> %d) is APPLIED and its storage FOLLOWS (re-keyed / followed, nothing orphaned)" % (ca, e1),
           applied(ack) and run.n_log(r"0x%04X with extra storage MOVED cell %d -> %d: storage re-keyed|0x%04X with \d+ extra item\(s\) FOLLOWED its piece cell %d -> %d" %
                                     (fa, ca, e1, fa, ca, e1), off3) == 1 and run.n_log(r"storage ORPHANED", off3) == 0)
        s1 = query(a, 0, 0, e1, 5, fa)
        ck("Q1 a SELECT of A at its NEW cell %d answers its 26 items; the old cell %d has no dresser any more" % (e1, ca),
           n_items(s1) == 26 and query(a, 0, 0, ca, 6, fa, wait=2.0) is None)
        cur_a = e1
        # Q2 ROTATE of A in place: APPLIED, same identity (the key uses the rotation-free id)
        off3 = len(run.log())
        img2 = L.house_set_cell(img1, 0, 0, e1, fa | 1)
        ack = commit(img2)
        s2 = query(a, 0, 0, e1, 7, fa)
        ck("Q2 ROTATE of A (26 extras) in place is APPLIED; the storage stays (26 items, no re-key / orphan)", applied(ack) and n_items(s2) == 26 and
           run.n_log(r"re-keyed|ORPHANED", off3) == 0)
        # Q3 PICKUP of A into a pocket: REFUSED (removal), host image unchanged, pockets unchanged
        pk, conds, wallet = L.record_inventory(a.rec_local)
        pk3 = L.record_inventory(a.rec_local)
        fs = list(pk).index(0)
        pkp = list(pk)
        pkp[fs] = fa
        off3 = len(run.log())
        ack = commit(L.house_set_cell(img2, 0, 0, e1, 0), L.record_set_inventory(a.rec_local, pkp, conds, wallet))
        ck("Q3 PICKUP of A (26 extras) into pocket %d is REFUSED (INVALID_CELL 0x%04X)" % (fs, 0x2000 | e1), refused_rm(ack, off3, e1))
        a.hub.wait_until(lambda: a.house_canon(h) is not None and a.house_canon(h) == img2, 6.0)
        L.pump_sleep(0.8)
        ck("Q3 host image unchanged (re-pushed = A at %d, rotated) and the pockets unchanged" % e1,
           a.house_canon(h) == img2 and L.record_inventory(a.rec_local) == pk3)
        # Q4 kind-18 op 3 MOVE inside A: into an empty slot, then a swap; replay; NOT_AVAILABLE
        s4 = query(a, 0, 0, e1, 8, fa)
        items = s4["items"] if s4 else [0] * 27
        src0 = next(i for i, x in enumerate(items) if x)
        dst0 = next(i for i, x in enumerate(items) if not x)
        it0 = items[src0]
        pk4 = L.record_inventory(a.rec_local)
        sent, r = move_op(src0, dst0, it0, 8)
        run.tx_ok("Q4 MOVE op: A slot %d (0x%04X) -> empty slot %d" % (src0, it0, dst0), r, APPLIED, R["NONE"])
        L.pump_sleep(0.4)
        st4 = (states(a) or [None])[-1]
        ck("Q4 the item moved (slot %d now 0x%04X, slot %d empty), still 26 items, pockets unchanged" % (dst0, it0, src0),
           st4 is not None and st4["items"][dst0] == it0 and st4["items"][src0] == 0 and n_items(st4) == 26 and L.record_inventory(a.rec_local) == pk4)
        a.resend_txn(sent)
        rr = a.wait_txn_result(sent.seq, 4.0, nth=2)
        ck("Q4 the identical MOVE retransmitted is REPLAYED (never executed twice)", rr is not None and rr.outcome == APPLIED and rr.reason == R["REPLAYED"])
        src1 = next(i for i, x in enumerate(st4["items"]) if x and i != dst0)
        it1 = st4["items"][src1]
        sent, r = move_op(src1, dst0, it1, 8)
        run.tx_ok("Q4 MOVE op onto an OCCUPIED slot = SWAP (slot %d 0x%04X <-> slot %d 0x%04X)" % (src1, it1, dst0, it0), r, APPLIED, R["NONE"])
        L.pump_sleep(0.4)
        st5 = (states(a) or [None])[-1]
        ck("Q4 swapped: slot %d = 0x%04X, slot %d = 0x%04X, still 26 items" % (dst0, it1, src1, it0),
           st5 is not None and st5["items"][dst0] == it1 and st5["items"][src1] == it0 and n_items(st5) == 26)
        empty_slot = next(i for i, x in enumerate(st5["items"]) if not x)
        sent, r = move_op(empty_slot, dst0, it0, 8)
        ck("Q4 a MOVE whose source does not hold the item is REJECTED NOT_AVAILABLE (16), nothing moves",
           r is not None and r.outcome == REJECTED and r.reason == REASON_NOT_AVAILABLE and n_items((states(a) or [None])[-1]) == 26)
        ck("Q4 the host logged exactly 2 MOVE commits", run.n_log(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ MOVE item=", off3) == 2)
        # Q4b (6r) SWAP (op 4): pool slot <-> pocket in ONE transaction; then the pool is restored (PUT back) so the later phases still see 26 items
        st6 = query(a, 0, 0, e1, 13, fa)
        filled = [i for i, x in enumerate(st6["items"]) if x] if st6 else []
        if len(filled) >= 2:
            s_a, s_b = filled[0], filled[1]
            it_a, it_b = st6["items"][s_a], st6["items"][s_b]
            free = W.free_slot(a)
            mark, _s, r = txn(run, a, OP_TAKE, free, it_a, s_a, None, 13)
            run.tx_ok("Q4b setup: TAKE 0x%04X (slot %d) into pocket %d" % (it_a, s_a, free), r, APPLIED, R["NONE"])
            L.pump_sleep(0.4)
            sent = a.send_txn_commit(K_DRESSER, run.fresh_rid(), D_POCKET, free, it_b, pre=None, aux_cond=OP_SWAP, aux_item=s_b, flags=13)
            r = a.wait_txn_result(sent.seq, 4.0)
            run.tx_ok("Q4b SWAP: pool slot %d (0x%04X) <-> pocket %d (0x%04X)" % (s_b, it_b, free, it_a), r, APPLIED, R["NONE"])
            L.pump_sleep(0.4)
            st7 = (states(a) or [None])[-1]
            pk7 = L.record_inventory(a.rec_local)[0]
            ck("Q4b swapped: slot %d = 0x%04X, pocket %d = 0x%04X, 25 items in the pool (nothing created / lost)" % (s_b, it_a, free, it_b),
               st7 is not None and st7["items"][s_b] == it_a and pk7[free] == it_b and n_items(st7) == 25)
            a.resend_txn(sent)
            rr = a.wait_txn_result(sent.seq, 4.0, nth=2)
            ck("Q4b the identical SWAP retransmitted is REPLAYED (never executed twice)", rr is not None and rr.outcome == APPLIED and rr.reason == R["REPLAYED"])
            mark, _s, r = txn(run, a, OP_PUT, free, it_b, 0xFF, None, 13)  # from the pocket it really sits in
            run.tx_ok("Q4b restore: PUT 0x%04X back from pocket %d (26 items again)" % (it_b, free), r, APPLIED, R["NONE"])
        # Q5 C (1 extra item) moved together with an unrelated plain piece: APPLIED, C's item follows
        last = img2
        plain = [(c, v) for c in range(L.HOUSE_CELLS) for v in [L.house_cell(last, 0, 0, c)] if (v >> 12) in (1, 3) and (v & ~3) not in (fa, fb, fc)]
        sc0 = query(a, 0, 0, cc, 9, fc)
        mark, _s, r, _sl = put(run, a, 0x2440, 9)
        run.tx_ok("Q5 setup: PUT 0x2440 into C (the identical twin of A): 1 extra item", r, APPLIED, R["NONE"])
        img5 = L.house_set_cell(L.house_set_cell(last, 0, 0, cc, 0), 0, 0, e3, fc)
        if plain:
            img5 = L.house_set_cell(L.house_set_cell(img5, 0, 0, plain[0][0], 0), 0, 0, e4, plain[0][1])
        off3 = len(run.log())
        ack = commit(img5)
        sc5 = query(a, 0, 0, e3, 10, fc)
        ck("Q5 MOVE of C (1 extra) cell %d -> %d (+ an unrelated plain move) is APPLIED; C's item follows to cell %d" % (cc, e3, e3),
           applied(ack) and sc5 is not None and [x for x in sc5["items"] if x] == [0x2440])
        sa5 = query(a, 0, 0, e1, 11, fa)
        ck("Q5 A (the identical twin) is unaffected: 26 items at cell %d" % e1, n_items(sa5) == 26)
        cur_c = e3
        last = img5
        # Q6 keep items: B's extra item out (TAKE), then one commit = keep item into B + B moved (pre-image keep empty: a move) -> APPLIED; then the
        # PICKUP of B with the keep item -> REFUSED (keep rule); a second move with the keep item travelling -> APPLIED
        sb = query(a, 0, 0, cb, 12, fb)
        free = W.free_slot(a)
        mark, _s, r = txn(run, a, OP_TAKE, free, 0x2406, sb["items"].index(0x2406) if sb else 0, None, 12)
        run.tx_ok("Q6 setup: TAKE 0x2406 out of B (its extras are empty now)", r, APPLIED, R["NONE"])
        L.pump_sleep(0.5)
        pk, conds, wallet = L.record_inventory(a.rec_local)
        kv = next(i for i, x in enumerate(pk) if x == 0x2406)
        pk6 = list(pk)
        pk6[kv] = 0
        img6 = L.house_set_cell(L.house_set_cell(L.house_set_cell(last, 0, 0, cb, 0), 0, 0, e2, fb), 0, 1, e2, 0x2406)
        ack = commit(img6, L.record_set_inventory(a.rec_local, pk6, conds, wallet))
        ck("Q6 keep item 0x2406 into B + B moved cell %d -> %d in ONE commit is APPLIED" % (cb, e2), applied(ack))
        cur_b = e2
        last = img6
        pk, conds, wallet = L.record_inventory(a.rec_local)
        fs = list(pk).index(0)
        pkp = list(pk)
        pkp[fs] = fb
        off3 = len(run.log())
        ack = commit(L.house_set_cell(L.house_set_cell(last, 0, 0, e2, 0), 0, 1, e2, 0), L.record_set_inventory(a.rec_local, pkp, conds, wallet))
        ck("Q6 PICKUP of B holding a vanilla keep item is REFUSED (27-slot keep rule, INVALID_CELL 0x%04X)" % (0x2000 | e2), refused_rm(ack, off3, e2, "keep"))
        a.hub.wait_until(lambda: a.house_canon(h) is not None and a.house_canon(h) == last, 6.0)
        L.pump_sleep(0.8)
        img7 = L.house_set_cell(L.house_set_cell(L.house_set_cell(L.house_set_cell(last, 0, 0, e2, 0), 0, 1, e2, 0), 0, 0, e4 if not plain else emp[4], fb), 0, 1,
                                e4 if not plain else emp[4], 0x2406)
        ack = commit(img7)
        ck("Q6 a MOVE of B with its keep item travelling is APPLIED", applied(ack))
        cur_b = e4 if not plain else emp[4]
    # ---- D8 host restart
    for cl in list(run.clients):
        run.release(cl)
    ck("D8 the first host did not crash before the restart", run.host.stop() is None)
    host2 = L.HostProcess(port=run.port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT)], log_path=os.path.join(HERE, "dresser_proto_host2.log")).start()
    ok = host2.wait_listening(60.0) and host2.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT)
    run.host = host2
    ck("D8 the host restarted", ok)
    if not ok:
        return
    L.resolve_host_town(run.ip, run.port)
    b = run.ready("A2", run.r1)
    enter_room(b, run.r1)
    off2 = len(run.log())
    b.resend_txn(sent_take)
    rr = b.wait_txn_result(sent_take.seq, 4.0, nonce=sent_take.nonce)
    ck("D8 the pre-restart TAKE retransmitted WITHOUT a selection is answered from the PERSISTED journal (APPLIED/REPLAYED)",
       rr is not None and rr.outcome == APPLIED and rr.reason == R["REPLAYED"])
    L.pump_sleep(0.4)
    ck("D8 nothing re-executed after the restart", run.n_log(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ (PUT|TAKE) ", off2) == 0)
    query(b)  # probe first (the client UI order)
    sa = query(b, 0, 0, cur_a, 1, fa)
    sb = query(b, 0, 0, cur_b, 2, fb)
    sc = query(b, 0, 0, cur_c, 3, fc) if img0 is not None else None
    ck("D8 after the restart the twin C still holds its 1 item (0x2440) at its NEW cell %d" % cur_c, sc is not None and [x for x in sc["items"] if x] == [0x2440])
    ck("D8 after the restart A still holds 26 (rev %s) and B (moved after emptying) is empty at its new cell" % rev_a,
       sa is not None and sum(1 for x in sa["items"] if x) == 26 and sa["rev"] == rev_a and sb is not None and not any(sb["items"]))
    tracked = set(ITEMS + [0x2406] + fill + [0x2430])
    pool = [x for x in (sa["items"] if sa else []) + (sb["items"] if sb else []) if x in tracked]
    pockets = [x for x in b.txn_pre_image()[0] if x in tracked]
    ck("D9 conservation: pool %d + the refused full PUT never left the pocket (pocket copy of 0x2430 or not created) -- every applied PUT is in A/B exactly once"
       % len(pool), len(pool) == len(set(pool)) and len(pool) == 26)  # 0x2406 left B (TAKE) and now sits in B's vanilla slot (house image)


def run_two_hosts_house_sync(results, ip, port, body, snap_gci, restore):
    """W.run_phase_two_hosts, but the first host runs WITH furniture sync (the M phase commits house images through the real OWNER_COMMIT path)."""
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    img = L.house_from_gci(L.GAME_BIN_DIR, residents[0])
    emp = [c for c in range(L.HOUSE_CELLS) if L.house_cell(img, 0, 0, c) == 0 and c > 40]
    SEED[:] = [(emp[8], 0x1068), (emp[14], 0x1068), (emp[20], 0x1070)]  # A, C (identical twin), B
    os.environ["AC_TEST_HOOKS"] = "1"
    os.environ["AC_TEST_DRESSER_SEED"] = "%d:%s" % (residents[0], ",".join("%d=%04X" % cv for cv in SEED))
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot), "--house-sync"], log_path=os.path.join(HERE, "dresser_proto_host1.log")).start()
    os.environ.pop("AC_TEST_DRESSER_SEED", None)  # the restarted host keeps the seeded pieces in its save
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=host_slot)
    L.check("[DRESSER] host (furniture sync ON) reached field-ready state", ok, results)
    if ok:
        L.resolve_host_town(ip, port)
    run = TP.Run(ip, port, host, results, snap_gci, host_slot, residents[:3])
    try:
        if ok:
            body(run)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("[DRESSER] phase raised %r" % (exc,), False, results)
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        rc = run.host.stop()
        L.check("[DRESSER] the last host did not crash (exit code before stop: %s)" % (rc,), rc is None, results)
        restore()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16450)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_<name> copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    side = os.path.join(L.GAME_BIN_DIR, "save", "mp", "dresser_store.dat")
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "dresser_proto_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    if os.path.isfile(side):
        os.remove(side)

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)
        if os.path.isfile(side):
            os.remove(side)

    try:
        run_two_hosts_house_sync(results, "127.0.0.1", args.port, phase, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
