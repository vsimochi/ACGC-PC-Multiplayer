#!/usr/bin/env python3
"""test_ofurn_c2_protocol.py - OUTDOOR FURNITURE C2 (multi-tile footprints, kind 22 op 4 MOVE, message 102 OUTDOOR_FURN2, sidecar v2) against a REAL host process.

TIER: PROTOCOL TESTED against a REAL listen host (scripted resident / guest FakeClients, the harness of test_ofurn_protocol.py / test_ts_protocol.py). Run on a
DISPOSABLE fixture copy (NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_of2_<name>>). Ports 38000-38999 only.
  MAIN  102 CAPS (multi + move) + the 102 list at join; a 2x1 DRESSER (0x1064, a storage piece) placed: origin = id | rot, second cell = RSV_FE1F (FIELD_UPDATE at the
        other client), 102 SET at the guest, NOT in the 98 list (a 0.3.0 client sees a leaf + invisible filler); ROTATE swings the footprint (old cell EMPTY, new FE1F);
        a blocked swing / a blocked push are BAD_TILE and change nothing; PUSH one tile (102 MOVE delta, every cell moved), the byte-identical resend REPLAYED (one
        commit), a push from the wrong side BAD_TILE, a guest MOVE NOT_ALLOWED, PULL one tile back; PICKUP clears every cell (the base item in the pocket);
        a 2x2 (0x11B4) covers 4 cells; a resident's VANILLA pickup of the 2x2's origin leaf (what a 0.3.0 client does) is granted and the GC clears the 3 fillers;
        no record-protocol violation
  GC    a seeded 2x2 whose FILLER cell is overwritten behind the system's back: the marker is dropped (102 DEL), the other filler cells cleared, the origin stays a leaf
  V1    a version 1 (0.3.0) sidecar is converted to version 2 at load (log), the original kept as outdoor_furniture.dat.v1.bak, the file rewritten as v2
  RS    restart: a multi-tile marker written by host 1 is loaded by host 2 (v2, 1 piece, 12 + 52 + 4 bytes)
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
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_ts_protocol as TS  # noqa: E402
import test_ofurn_protocol as OF  # noqa: E402
from test_txn_protocol import APPLIED, R, D_NONE, D_POCKET  # noqa: E402

K_OF = 22
PLACE, ROTATE, PICKUP, MOVE = 1, 2, 3, 4
PULL = 4
NOT_ALLOWED, BAD_TILE = 56, 58
MSG_OF2 = 102
MK_BEGIN, MK_CHUNK, MK_END, MK_SET, MK_DEL, MK_PERMS, MK_CAPS, MK_MOVE = 1, 2, 3, 4, 5, 6, 7, 8
FILLER = 0xFE1F
DRESSER_2X1 = 0x1064  # iam_sum_blue_lowchest01: a 2x1 storage piece (aFTR_SHAPE_TYPEB_0, interaction STORAGE)
TABLE_2X2 = 0x11B4    # iam_sum_cont_table02: aFTR_SHAPE_TYPEC, no vtable
ONE_1X1 = 0x1320      # the C1 real-test 1x1 piece
S, E, N, W = 0, 1, 2, 3


def of2_msgs(c):
    out = []
    for m in c.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type == MSG_OF2 and m.conn == c.connect_count):
        p = bytes(m.payload)
        if len(p) != 268:
            continue
        _t, kind, count, flags, gen, total = struct.unpack_from("<BBBBII", p, 0)
        ents = [struct.unpack_from("<BBHI", p, 12 + 8 * i) for i in range(count)]
        out.append((kind, count, flags, gen, total, ents))
    return out


def view2(c):
    cur, stage = {}, None
    for kind, _n, _f, _g, _t, ents in of2_msgs(c):
        if kind == MK_BEGIN:
            stage = {}
        elif kind == MK_CHUNK and stage is not None:
            stage.update({(e[0], e[1]): e[2] for e in ents})
        elif kind == MK_END and stage is not None:
            cur, stage = stage, None
        elif kind in (MK_SET, MK_DEL, MK_MOVE):
            for t in (cur, stage):
                if t is None:
                    continue
                if kind == MK_SET:
                    t[(ents[0][0], ents[0][1])] = ents[0][2]
                elif kind == MK_DEL:
                    t.pop((ents[0][0], ents[0][1]), None)
                else:
                    t.pop((ents[0][0], ents[0][1]), None)
                    t[(ents[1][0], ents[1][1])] = ents[1][2]
    return cur


def caps2(c):
    f = [fl for k, _n, fl, *_r in of2_msgs(c) if k == MK_CAPS]
    return f[-1] if f else None


def moves2(c):
    return [ents for k, _n, _f, _g, _t, ents in of2_msgs(c) if k == MK_MOVE]


def stand(c, ux, uz, facing):
    x, y, z = L.tile_center(ux, uz)
    c.claim_position(x, y, z, facing={S: 0, E: 0x4000, N: -0x8000, W: -0x4000}[facing])
    L.pump_sleep(0.35)


def free_box(c, x0, z0, x1, z1):
    """every tile of the box empty, and no structure (>= 0xF000 except the filler) on the ring around it"""
    for z in range(z0 - 1, z1 + 2):
        for x in range(x0 - 1, x1 + 2):
            v = OF.tile_val(c, x, z)
            if v is None:
                return False
            if x0 <= x <= x1 and z0 <= z <= z1:
                if v != 0:
                    return False
            elif v >= 0xF000 and v != FILLER:
                return False
    return True


def box_cands(c, w, h, n=200, skip=()):
    out = []
    for uz in range(38, 100):
        for ux in range(30, 84):
            if any(abs(ux - a) < 6 and abs(uz - b) < 6 for a, b in skip):
                continue
            if not (2 <= ux % 16 <= 11 and 2 <= uz % 16 <= 10):
                continue  # away from the acre edges (cliffs / rivers often run there; the host refuses that ground)
            if free_box(c, ux, uz, ux + w - 1, uz + h - 1):
                out.append((ux, uz))
                if len(out) >= n:
                    return out
    return out


def txn(run, c, op, dest, slot, item, flags, ux, uz, pre=None):
    return OF.of_txn(run, c, op, dest, slot, item, flags, ux, uz, pre=pre)


def place_at(run, c, item, rot, cands, stand_rel, probe=False):
    """PLACE `item` rot on the first candidate the host accepts (the client stands at origin + stand_rel facing north): (origin, result) or (None, last result)"""
    r = None
    for (bx, bz) in cands:
        ux, uz = bx, bz + 1  # the candidate box starts one row above the origin (room to push north)
        s = OF.free_slot(OF.pre_with(c))
        pre = OF.pre_with(c, {s: item})
        stand(c, ux + stand_rel[0], uz + stand_rel[1], N)
        _sent, r = txn(run, c, PLACE, D_NONE, s, item, rot, ux, uz, pre=pre)
        if r is not None and r.outcome == APPLIED:
            if not probe:
                return (ux, uz), r
            # probe the row north of the origin (the push / swing target) with a 1x1 piece: the host's ground rules decide; then take both back if it is unusable
            s1 = OF.free_slot(OF.pre_with(c))
            stand(c, ux - 1, uz, N)
            _x, rp = txn(run, c, PLACE, D_NONE, s1, ONE_1X1, 0, ux, uz - 1, pre=OF.pre_with(c, {s1: ONE_1X1}))
            if rp is not None and rp.outcome == APPLIED:
                s2 = OF.free_slot(OF.pre_with(c))
                _x, rk = txn(run, c, PICKUP, D_POCKET, s2, ONE_1X1, 0, ux, uz - 1)
                if rk is not None and rk.outcome == APPLIED:
                    return (ux, uz), r
            s3 = OF.free_slot(OF.pre_with(c))
            stand(c, ux + stand_rel[0], uz + stand_rel[1], N)
            _x, _rk = txn(run, c, PICKUP, D_POCKET, s3, item | rot, 0, ux, uz)
            continue
        if r is None or r.reason != BAD_TILE:
            return None, r
    return None, r


def phase_main(run):
    ck = run.check
    a = run.ready("A", run.r1)
    a.wait_record_synced(30.0)
    g = OF.guest(run, "OFGST2", 0x6F11)
    ck("CAPS2 message 102 CAPS reached the resident and the guest: multi-tile + move (3)", OF.wait(a, lambda: caps2(a) == 3) and OF.wait(g, lambda: caps2(g) == 3))
    ck("LIST2 both got the (empty) 102 list at join and the 98 list as before", OF.wait(a, lambda: view2(a) == {}) and OF.wait(g, lambda: view2(g) == {})
       and OF.wait(a, lambda: OF.lists_done(a) >= 1))
    off0 = len(run.log())
    # ---- 2x1 dresser: room for the piece (2 wide) + a row north (push) + the stand row south: a 2 x 3 box (origin = box row 1)
    cands = box_cands(a, 2, 3)
    ck("BOX found free 2x3 boxes for the 2x1 dresser (%d)" % len(cands), len(cands) > 0)
    (o, r) = place_at(run, a, DRESSER_2X1, 0, cands, (0, 1), probe=True)
    ck("P1 a RESIDENT places the 2x1 DRESSER (0x%04X, rot 0 = +x): APPLIED" % DRESSER_2X1, o is not None)
    if o is None:
        print("   last result: %s" % (r,))
        return
    ux, uz = o
    ck("P1 the footprint: origin = id | 0, (+1,0) = RSV_FE1F, at the placer AND the guest (FIELD_UPDATE)",
       OF.wait(a, lambda: OF.tile_val(a, ux, uz) == DRESSER_2X1 and OF.tile_val(a, ux + 1, uz) == FILLER) and
       OF.wait(g, lambda: OF.tile_val(g, ux, uz) == DRESSER_2X1 and OF.tile_val(g, ux + 1, uz) == FILLER))
    ck("P1 102 SET at the guest; the 98 list (the 0.3.0 client's view) does NOT carry the multi-tile piece",
       OF.wait(g, lambda: (view2(g) or {}).get((ux, uz)) == DRESSER_2X1) and (ux, uz) not in (OF.view(g) or {}))
    # ---- rotation swings the footprint
    stand(a, ux, uz + 1, N)
    _x, r = txn(run, a, ROTATE, D_NONE, 0, DRESSER_2X1, 1, ux, uz)
    ck("R1 ROTATE to 1 (TYPEB_90): APPLIED, (+1,0) EMPTY again, (0,-1) = FE1F, 102 MOVE (same origin) at the guest",
       r is not None and r.outcome == APPLIED and OF.wait(g, lambda: OF.tile_val(g, ux + 1, uz) == 0 and OF.tile_val(g, ux, uz - 1) == FILLER and
                                                         OF.tile_val(g, ux, uz) == (DRESSER_2X1 | 1)) and OF.wait(g, lambda: len(moves2(g)) >= 1))
    _x, r = txn(run, a, ROTATE, D_NONE, 0, DRESSER_2X1 | 1, 0, ux, uz)
    ck("R2 ROTATE back to 0: APPLIED", r is not None and r.outcome == APPLIED and OF.wait(a, lambda: OF.tile_val(a, ux + 1, uz) == FILLER and OF.tile_val(a, ux, uz - 1) == 0))
    # ---- block the swing / the push with a 1x1 piece north of the origin
    s1 = OF.free_slot(OF.pre_with(a))
    stand(a, ux - 1, uz, N)
    _x, r = txn(run, a, PLACE, D_NONE, s1, ONE_1X1, 0, ux, uz - 1, pre=OF.pre_with(a, {s1: ONE_1X1}))
    blocked = r is not None and r.outcome == APPLIED
    ck("B0 a 1x1 piece placed north of the dresser (the obstacle)", blocked)
    if blocked:
        stand(a, ux, uz + 1, N)
        off = len(run.log())
        _x, r = txn(run, a, ROTATE, D_NONE, 0, DRESSER_2X1, 1, ux, uz)
        ck("B1 a rotation whose swing cell is occupied is BAD_TILE (58), nothing changed", OF.is_rej(r, BAD_TILE) and OF.tile_val(a, ux + 1, uz) == FILLER)
        _x, r = txn(run, a, MOVE, D_NONE, 0, DRESSER_2X1, N, ux, uz)
        ck("B2 a push into the occupied tile is BAD_TILE (58), nothing changed", OF.is_rej(r, BAD_TILE) and OF.tile_val(a, ux, uz) == DRESSER_2X1 and
           run.n_log(r"MOVE APPLIED \[TXN\]", off) == 0)
        s2 = OF.free_slot(OF.pre_with(a))
        stand(a, ux - 1, uz, N)
        _x, r = txn(run, a, PICKUP, D_POCKET, s2, ONE_1X1, 0, ux, uz - 1)
        ck("B3 the obstacle picked up again", r is not None and r.outcome == APPLIED and OF.wait(a, lambda: OF.tile_val(a, ux, uz - 1) == 0))
    # ---- PUSH north
    stand(a, ux, uz + 1, N)
    off = len(run.log())
    sent, r = txn(run, a, MOVE, D_NONE, 0, DRESSER_2X1, N, ux, uz)
    ck("M1 PUSH north (the player south of the piece): APPLIED", r is not None and r.outcome == APPLIED)
    ck("M1 every cell moved one tile north (new origin id, new FE1F, the two old cells EMPTY) at the guest",
       OF.wait(g, lambda: OF.tile_val(g, ux, uz - 1) == DRESSER_2X1 and OF.tile_val(g, ux + 1, uz - 1) == FILLER and OF.tile_val(g, ux, uz) == 0 and
               OF.tile_val(g, ux + 1, uz) == 0))
    ck("M1 102 MOVE delta (old origin -> new origin) at the guest, its 102 view re-keyed",
       OF.wait(g, lambda: any(e[0][:2] == (ux, uz) and e[1][:2] == (ux, uz - 1) for e in moves2(g))) and (view2(g) or {}).get((ux, uz - 1)) == DRESSER_2X1
       and (ux, uz) not in (view2(g) or {}))
    a.resend_txn(sent)
    r2 = a.wait_txn_result(sent.seq, 4.0, nth=2)
    ck("M2 a byte-identical resend of the MOVE is APPLIED/REPLAYED from the journal, one commit", r2 is not None and r2.outcome == APPLIED and r2.reason == R["REPLAYED"]
       and run.n_log(r"MOVE APPLIED \[TXN\]", off) == 1)
    uz -= 1
    stand(a, ux - 1, uz, E)
    _x, r = txn(run, a, MOVE, D_NONE, 0, DRESSER_2X1, N, ux, uz)
    ck("M3 a push north from the WEST side (wrong side) is BAD_TILE (58)", OF.is_rej(r, BAD_TILE))
    stand(g, ux, uz + 1, N)
    _x, r = txn(run, g, MOVE, D_NONE, 0, DRESSER_2X1, N, ux, uz)
    ck("M4 a GUEST may not move a placed piece: NOT_ALLOWED (56)", OF.is_rej(r, NOT_ALLOWED))
    # ---- PULL south (the player south of the piece steps back one more tile)
    stand(a, ux, uz + 1, N)
    _x, r = txn(run, a, MOVE, D_NONE, 0, DRESSER_2X1, S | PULL, ux, uz)
    ck("M5 PULL south: APPLIED, the piece back on its first cells", r is not None and r.outcome == APPLIED and
       OF.wait(a, lambda: OF.tile_val(a, ux, uz + 1) == DRESSER_2X1 and OF.tile_val(a, ux + 1, uz + 1) == FILLER and OF.tile_val(a, ux, uz) == 0))
    uz += 1
    # ---- PICKUP clears every cell
    pre = OF.pre_with(a)
    s3 = OF.free_slot(pre)
    stand(a, ux, uz + 1, N)
    _x, r = txn(run, a, PICKUP, D_POCKET, s3, DRESSER_2X1, 0, ux, uz, pre=pre)
    exp = list(pre[0])
    exp[s3] = DRESSER_2X1
    ck("K1 PICKUP of the 2x1: APPLIED, the base item in the free slot, nothing else changed", r is not None and r.outcome == APPLIED and list(r.post_pockets) == exp)
    ck("K1 both cells EMPTY at the guest, 102 DEL", OF.wait(g, lambda: OF.tile_val(g, ux, uz) == 0 and OF.tile_val(g, ux + 1, uz) == 0) and
       OF.wait(g, lambda: (ux, uz) not in (view2(g) or {})))
    # ---- 2x2
    cands = box_cands(a, 2, 4, skip=((ux, uz),))
    (o2, r) = place_at(run, a, TABLE_2X2, 0, cands, (0, 2))
    ck("Q1 a 2x2 table (0x%04X) placed: APPLIED" % TABLE_2X2, o2 is not None)
    if o2 is not None:
        qx, qz = o2
        ck("Q1 4 cells: origin id, (+1,0) (0,+1) (+1,+1) RSV_FE1F (at the guest)",
           OF.wait(g, lambda: OF.tile_val(g, qx, qz) == TABLE_2X2 and all(OF.tile_val(g, qx + dx, qz + dz) == FILLER for dx, dz in ((1, 0), (0, 1), (1, 1)))))
        b = run.ready("B", run.r2)
        b.wait_record_synced(30.0)
        ck("J1 a late joiner gets the 2x2 in its 102 list", OF.wait(b, lambda: (view2(b) or {}).get((qx, qz)) == TABLE_2X2, 10.0))
        stand(b, qx, qz + 2, N)
        prov, _s, r = b.txn_pickup(qx, qz, run.fresh_rid(), timeout=4.0)
        ck("V1 a resident's VANILLA pickup of the origin leaf (what a 0.3.0 client does) is granted with the base item",
           prov is not None and prov.accepted and r is not None and r.outcome == APPLIED and TABLE_2X2 in r.post_pockets)
        ck("V1 the GC then clears the 3 filler cells and the marker (102 DEL)",
           OF.wait(g, lambda: all(OF.tile_val(g, qx + dx, qz + dz) == 0 for dx, dz in ((0, 0), (1, 0), (0, 1), (1, 1))), 8.0)
           and OF.wait(g, lambda: (qx, qz) not in (view2(g) or {}), 8.0) and re.search(r"GC tile \(%d,%d\) shape 2: .* 3 filler cell\(s\) cleared" % (qx, qz), run.log()) is not None)
    ck("NV no record-protocol violation was counted (every refusal is semantic, never BAD_SHAPE)", run.n_log(r"\[NET\]\[REC\] host: peer \d+ violation", off0) == 0)
    ck("98 every message 98 stayed the C1 layout (268 B, kinds 1..6 only)", all(k in (1, 2, 3, 4, 5, 6) for k, *_r in OF.of_msgs(g)))


def phase_gc(run):
    ck = run.check
    m = None
    w = 0.0
    while m is None and w < 40:
        m = re.search(r"seeded placed piece at \((\d+),(\d+)\) item 0x([0-9A-F]{4}) rot (\d) -> placed", run.log())
        L.pump_sleep(0.5)
        w += 0.5
    ck("G0 the host seeded a 2x2 placed piece (AC_TEST_OFURN_SEED)", m is not None)
    if m is None:
        return
    ux, uz = int(m.group(1)), int(m.group(2))
    a = run.ready("A", run.r1)
    a.wait_record_synced(30.0)
    ck("G1 the joining client's 102 list carries the seeded 2x2", OF.wait(a, lambda: (view2(a) or {}).get((ux, uz)) == TABLE_2X2, 10.0))
    ck("G2 a FILLER cell overwritten behind the system's back: the GC drops the marker (102 DEL), clears the 2 other filler cells, the origin stays a leaf",
       OF.wait(a, lambda: (ux, uz) not in (view2(a) or {}), 40.0) and re.search(r"GC tile \(%d,%d\) shape 2: cell \(%d,%d\) holds 0x0000 now.* 2 filler cell\(s\) cleared" % (ux, uz, ux + 1, uz), run.log())
       is not None and OF.wait(a, lambda: OF.tile_val(a, ux, uz) == TABLE_2X2 and OF.tile_val(a, ux, uz + 1) == 0 and OF.tile_val(a, ux + 1, uz + 1) == 0))


def write_v1_sidecar(path, markers):
    body = b"".join(struct.pack("<BBBBHHI20s20s", ux, uz, rot, 1, base, 0, 1700000000, b"\x00" * 20, b"OldTown".ljust(20, b"\x00")) for ux, uz, rot, base in markers)
    hdr = struct.pack("<III", 0x5255464F, 1, len(markers))
    h = 2166136261
    for byte in hdr + body:
        h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
    with open(path, "wb") as f:
        f.write(hdr + body + struct.pack("<I", h))


def phase_v1(results, ip, port, restore):
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    mp = os.path.join(L.GAME_BIN_DIR, "save", "mp")
    os.makedirs(mp, exist_ok=True)
    path = os.path.join(mp, "outdoor_furniture.dat")
    for p in (path, path + ".v1.bak"):
        if os.path.exists(p):
            os.remove(p)
    write_v1_sidecar(path, [(40, 50, 2, ONE_1X1), (41, 52, 0, 0x1000)])
    h, ok = TS.start_host(ip, port, "OFV1", [], results)
    try:
        if ok:
            m = h.wait_for_log(r"is a version 1 \(0\.3\.0\) sidecar: (\d+) piece\(s\) converted to version 2", 40.0)
            ck("C1 host: the v1 sidecar was converted at load (2 pieces)", m is not None and m.group(1) == "2")
            L.pump_sleep(1.0)
    finally:
        rc = h.stop()
        ck("C1 host did not crash", rc is None)
    ck("C2 the original is kept as outdoor_furniture.dat.v1.bak (still version 1)", os.path.isfile(path + ".v1.bak") and
       struct.unpack_from("<I", open(path + ".v1.bak", "rb").read(), 4)[0] == 1)
    ck("C3 the sidecar is version 2 now", os.path.isfile(path) and struct.unpack_from("<I", open(path, "rb").read(), 4)[0] == 2)
    restore()


def phase_restart(results, ip, port, restore):
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    os.environ["AC_TEST_OFURN_SEED"] = "0x%04X,0" % DRESSER_2X1
    h1, ok = TS.start_host(ip, port, "OFRS21", [], results)
    try:
        if ok:
            m = h1.wait_for_log(r"seeded placed piece at \((\d+),(\d+)\) item 0x%04X rot 0 -> placed" % DRESSER_2X1, 40.0)
            ck("RS1 host 1 placed a 2x1 piece (sidecar written)", m is not None)
    finally:
        h1.stop()
        os.environ.pop("AC_TEST_OFURN_SEED", None)
    sidecar = os.path.join(L.GAME_BIN_DIR, "save", "mp", "outdoor_furniture.dat")
    data = open(sidecar, "rb").read() if os.path.isfile(sidecar) else b""
    ck("RS2 the sidecar is v2 with one 52-byte marker whose shape byte is 2x1 (1)", len(data) == 12 + 52 + 4 and struct.unpack_from("<I", data, 4)[0] == 2 and data[12 + 6] == 1)
    h2, ok = TS.start_host(ip, port + 1, "OFRS22", [], results)
    try:
        if ok:
            ld = h2.wait_for_log(r"outdoor_furniture\.dat loaded: (\d+) placed piece", 40.0)
            ck("RS3 host 2 loaded the multi-tile marker", ld is not None and "loaded: 1 placed" in h2.log_text())
    finally:
        rc = h2.stop()
        ck("RS host 2 did not crash", rc is None)
        restore()


def run_all(port, only, results):
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_of2"):
        print("REFUSING: this test mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_of2_<name> copy only (NET_SPIKE_GAME_BIN=<abs path>)", file=sys.stderr)
        return 2
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "ofurn_c2_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    keys = ("AC_TEST_OFURN_LEAF", "AC_TEST_OPS", "AC_TEST_OFURN_SEED", "AC_TEST_OFURN_GC_MS", "AC_TEST_OFURN_GC_CELL", "AC_MP_OUTDOOR_FURNITURE")
    phases = (("MAIN", {}, phase_main),
              ("GC", {"AC_TEST_OFURN_SEED": "0x%04X,0" % TABLE_2X2, "AC_TEST_OFURN_GC_MS": "25000", "AC_TEST_OFURN_GC_CELL": "1"}, phase_gc))
    try:
        for i, (tag, env, body) in enumerate(phases):
            if only and tag not in only:
                continue
            for k in keys:
                os.environ.pop(k, None)
            os.environ.update(env)
            TS.run_phase(results, ip, port + i, "OF2" + tag, [], body, snap_gci, restore)
        for k in keys:
            os.environ.pop(k, None)
        if not only or "V1" in only:
            phase_v1(results, ip, port + 10, restore)
        if not only or "RS" in only:
            phase_restart(results, ip, port + 20, restore)
    finally:
        for k in keys:
            os.environ.pop(k, None)
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=38100)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    if not (38000 <= args.port <= 38960):
        print("ports 38000-38999 only", file=sys.stderr)
        return 2
    results = []
    rc = run_all(args.port, set(x for x in args.only.split(",") if x), results)
    if rc is not None:
        return rc
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
