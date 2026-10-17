#!/usr/bin/env python3
"""test_ofurn_protocol.py - OUTDOOR FURNITURE (C1) + the OPERATOR LIST against a REAL host process: TXN_COMMIT kind 22 (PLACE / ROTATE / PICKUP), message 98
OUTDOOR_FURN (list at join, SET / DEL deltas, PERMS), town service 10 OFURN_CAPS, the vanilla pickup gate of a placed piece, the leaf-drop policy, the safety ceiling,
the marker GC, the sidecar across a host restart.

TIER: PROTOCOL TESTED against a REAL listen host (scripted resident / guest FakeClients, the harness of test_ts_protocol.py). Run on a DISPOSABLE fixture copy
(NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_of_<name>>). Ports 25000-25999 only.
  MAIN  CAPS (service 10: v1, Place + leaf on, 512 / 128), PERMS (resident 0x0B, guest 0x08), the join list; a 1x1 id and a too-big id are classified by the host
        (TOO_BIG 57 before any tile rule); PLACE APPLIED (conservation: only the slot empties; the tile = id | rot; SET delta to the others), replay REPLAYED (one commit),
        TOO_BIG, guest PLACE NOT_ALLOWED (56), an occupied tile / a tile out of reach BAD_TILE (58); ROTATE APPLIED / guest NOT_ALLOWED / stale GONE (61); a late joiner gets
        the list; a guest's vanilla PICKUP_REQUEST of the placed piece is refused while the guest's pickup of a furniture LEAF is accepted; guest kind-22 PICKUP
        NOT_ALLOWED; an OPERATOR guest (AC_TEST_OPS) gets PICKUP|OP rights and may ROTATE; the resident's kind-22 PICKUP APPLIED (the base item, rotation 0, the tile
        empty, DEL delta); a resident's VANILLA pickup of a placed piece is accepted with the rotation-0 item
  GC    a seeded piece (AC_TEST_OFURN_SEED) reaches a joining client in the list; the tile overwritten behind the system's back (AC_TEST_OFURN_GC_MS) -> DEL + GC log
  OFF   AC_MP_OUTDOOR_FURNITURE=0: service 10 says Place OFF, PERMS without PLACE, a PLACE anyway is OFURN_OFF (55), not BAD_SHAPE
  LEAF  outdoor_furniture_leaf_drop = 0: service 10 / PERMS say no leaf drop; a vanilla DROP_REQUEST of furniture is refused, of a non-furniture item accepted
  CAP   outdoor_furniture_max_acre = 1: the second piece in the acre is OFURN_LIMIT (59)
  RS    restart: the sidecar written by host 1 is loaded by host 2 (log), its markers kept only where the town tile still holds them
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
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE, D_POCKET  # noqa: E402

K_OF = 22
PLACE, ROTATE, PICKUP = 1, 2, 3
OFF, NOT_ALLOWED, TOO_BIG, BAD_TILE, LIMIT, RATE, GONE = 55, 56, 57, 58, 59, 60, 61
SVC_OFURN = 10
MSG_OF = 98
MK_BEGIN, MK_CHUNK, MK_END, MK_SET, MK_DEL, MK_PERMS = 1, 2, 3, 4, 5, 6
P_PLACE, P_PICKUP, P_OP, P_LEAF = 1, 2, 4, 8
LEAF_ITEM = 0x1000
FACE_N = -0x8000  # binangle 0x8000 (facing -z, north) as the signed s16 of the MOVE


def of_msgs(c):
    out = []
    for m in c.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type == MSG_OF and m.conn == c.connect_count):
        p = bytes(m.payload)
        if len(p) != 268:
            continue
        _t, kind, count, flags, gen, total = struct.unpack_from("<BBBBII", p, 0)
        ents = [struct.unpack_from("<BBHI", p, 12 + 8 * i) for i in range(count)]
        out.append((kind, count, flags, gen, total, ents))
    return out


def view(c):
    """the client mirror the real client builds from message 98 (BEGIN / CHUNK / END swap, SET / DEL deltas)"""
    cur, stage = {}, None
    for kind, _n, _f, _g, _t, ents in of_msgs(c):
        if kind == MK_BEGIN:
            stage = {}
        elif kind == MK_CHUNK and stage is not None:
            stage.update({(e[0], e[1]): e[2] for e in ents})
        elif kind == MK_END and stage is not None:
            cur, stage = stage, None
        elif kind in (MK_SET, MK_DEL):
            for e in ents:
                for t in (cur, stage):
                    if t is None:
                        continue
                    if kind == MK_SET:
                        t[(e[0], e[1])] = e[2]
                    else:
                        t.pop((e[0], e[1]), None)
    return cur


def lists_done(c):
    return sum(1 for k, *_r in of_msgs(c) if k == MK_END)


def perms(c):
    p = [f for k, _n, f, *_r in of_msgs(c) if k == MK_PERMS]
    return p[-1] if p else None


def wait(c, cond, timeout=6.0):
    w = 0.0
    while not cond() and w < timeout:
        L.pump_sleep(0.25)
        w += 0.25
    return cond()


def caps(c):
    st = c.ts_latest(SVC_OFURN)
    return st[1] if st else None


def tile_val(c, ux, uz):
    at = L.town_ut_to_acre_tile(ux, uz)
    return None if at is None else c.world.tile(at[0], at[1])


def free_slot(pre):
    return next((i for i, p in enumerate(pre[0]) if p == 0), None)


def pre_with(c, changes=None):
    pockets, conds, w = c.txn_pre_image()
    pockets = list(pockets)
    for i, v in (changes or {}).items():
        pockets[i] = v
        conds &= ~(3 << (2 * i))
    return tuple(pockets), conds, w


def is_rej(r, reason):
    return r is not None and r.outcome == REJECTED and r.reason == reason


def stand_south_of(c, ux, uz):
    """claims the centre of the tile south of (ux, uz), facing north (the piece is the front tile)"""
    x, y, z = L.tile_center(ux, uz + 1)
    c.claim_position(x, y, z, facing=FACE_N)
    L.pump_sleep(0.35)


def of_txn(run, c, op, dest, slot, item, rot, ux, uz, pre=None, wait_s=5.0):
    for _try in range(4):
        L.pump_sleep(0.25)  # the host's anti-spam gap (200 ms)
        sent = c.send_txn_commit(K_OF, run.fresh_rid(), dest, slot, item, pre=pre, aux_cond=op, aux_item=((uz & 0xFF) << 8) | (ux & 0xFF), flags=rot)
        r = c.wait_txn_result(sent.seq, wait_s)
        if not is_rej(r, RATE):
            return sent, r
        L.pump_sleep(1.5)  # the generous per-minute budget: wait and retry (a scripted client is faster than any player)
    return sent, r


def candidates(c, n=400):
    """empty town tiles whose 8 neighbours hold no structure and whose south neighbour is empty (somewhere to stand); the host decides the rest"""
    out = []
    for uz in range(36, 104):
        for ux in range(28, 88):
            v = tile_val(c, ux, uz)
            s = tile_val(c, ux, uz + 1)
            if v != 0 or s != 0:
                continue
            nb = [tile_val(c, ux + dx, uz + dz) for dx in (-1, 0, 1) for dz in (-1, 0, 1) if dx or dz]
            if any(x is None or x >= 0xF000 for x in nb):
                continue
            out.append((ux, uz))
            if len(out) >= n:
                return out
    return out


def classify_ids(run, c):
    """(a 1x1 furniture id, a too-big furniture id): a PLACE far out of reach is TOO_BIG for a multi-tile piece (checked before the tile rules) and BAD_TILE otherwise"""
    one, big = None, None
    pre0 = pre_with(c)
    s = free_slot(pre0)
    for k in range(0, 300, 3):
        item = 0x1000 + 4 * k
        if one is not None and big is not None:
            break
        pre = pre_with(c, {s: item})
        stand_south_of(c, 30, 40)
        _x, r = of_txn(run, c, PLACE, D_NONE, s, item, 0, 80, 100, pre=pre)
        if is_rej(r, TOO_BIG) and big is None:
            big = item
        elif is_rej(r, BAD_TILE) and one is None:
            one = item
    return one, big


def place_somewhere(run, c, item, rot=2, cands=None, skip=()):
    """PLACE `item` (forced into a free slot of the pre-image) on the first candidate the host accepts: (tile, pre, slot, result) or None"""
    for (ux, uz) in (cands or candidates(c)):
        if (ux, uz) in skip:
            continue
        pre0 = pre_with(c)
        s = free_slot(pre0)
        pre = pre_with(c, {s: item})
        stand_south_of(c, ux, uz)
        _sent, r = of_txn(run, c, PLACE, D_NONE, s, item, rot, ux, uz, pre=pre)
        if r is not None and r.outcome == APPLIED:
            return (ux, uz), pre, s, r, _sent
        if r is None or r.reason not in (BAD_TILE,):
            return None
    return None


def guest(run, label, key):
    g = L.FakeClient(label, run.ip, run.port, guest=L.guest_identity(label, key))
    run.clients.append(g)
    g.connect_and_ready(quiet=True)
    g.wait_record_synced(30.0)
    return g


def phase_main(run):
    ck = run.check
    a = run.ready("A", run.r1)
    a.wait_record_synced(30.0)
    g = guest(run, "OFGST", 0x6F01)
    ck("CAPS service 10 reached the resident and the guest: version 1, Place + leaf drop on (3), ceiling 512 / 128",
       wait(a, lambda: caps(a) is not None) and wait(g, lambda: caps(g) is not None) and
       caps(a)[:2] == bytes([1, 3]) and struct.unpack_from("<HH", caps(a), 2) == (512, 128) and caps(g) == caps(a))
    ck("PERMS resident = place|pickup|leaf (0x0B), guest = leaf only (0x08)",
       wait(a, lambda: perms(a) is not None) and wait(g, lambda: perms(g) is not None) and perms(a) == (P_PLACE | P_PICKUP | P_LEAF) and perms(g) == P_LEAF)
    ck("LIST both clients got the (empty) placed-furniture list at join (BEGIN .. END)", wait(a, lambda: lists_done(a) >= 1) and wait(g, lambda: lists_done(g) >= 1) and view(a) == {})
    seeded = re.search(r"seeded leaf at \((\d+),(\d+)\) item 0x([0-9A-F]{4})", run.log())
    ck("SEED the host wrote a furniture LEAF (no marker) for the leaf pickup check", seeded is not None)

    one, big = classify_ids(run, a)
    ck("K1 the host classified a 1x1 furniture id (BAD_TILE out of reach) and a multi-tile id (TOO_BIG 57, 'too big to place outside') -- %s / %s" %
       (hex(one) if one else None, hex(big) if big else None), one is not None and big is not None)
    if one is None:
        return
    off = len(run.log())
    pl = place_somewhere(run, a, one, rot=2)
    ck("P1 a RESIDENT places the 1x1 piece in front of it: APPLIED", pl is not None)
    if pl is None:
        return
    (ux, uz), pre, s, r, sent = pl
    exp = list(pre[0])
    exp[s] = 0
    ck("P1 conservation: the post-image is the pre-image with only the slot emptied (wallet unchanged)", list(r.post_pockets) == exp and r.post_wallet == pre[2])
    ck("P1 the host committed once (PLACE APPLIED log)", run.n_log(r"PLACE APPLIED \[TXN\]", off) == 1)
    ck("P1 the tile now holds id | rotation (FIELD_UPDATE) at the placer and the guest", wait(a, lambda: tile_val(a, ux, uz) == (one | 2)) and wait(g, lambda: tile_val(g, ux, uz) == (one | 2)))
    ck("P1 SET delta: the guest's mirror shows the placed piece", wait(g, lambda: (view(g) or {}).get((ux, uz)) == (one | 2)))
    a.resend_txn(sent)
    r2 = a.wait_txn_result(sent.seq, 4.0, nth=2)
    ck("P2 a byte-identical resend is APPLIED/REPLAYED from the journal", r2 is not None and r2.outcome == APPLIED and r2.reason == R["REPLAYED"])
    L.pump_sleep(0.5)
    ck("P2 still exactly one commit", run.n_log(r"PLACE APPLIED \[TXN\]", off) == 1)
    s2 = free_slot(pre_with(a))
    stand_south_of(a, ux, uz)
    _x, r = of_txn(run, a, PLACE, D_NONE, s2, big, 0, ux + 1, uz, pre=pre_with(a, {s2: big}))
    ck("P3 a multi-tile piece is TOO_BIG (57) -- it stays in the pocket (can still be dropped as a leaf)", is_rej(r, TOO_BIG))
    sg = free_slot(pre_with(g))
    stand_south_of(g, ux + 2, uz)
    _x, r = of_txn(run, g, PLACE, D_NONE, sg, one, 0, ux + 2, uz, pre=pre_with(g, {sg: one}))
    ck("P4 a GUEST may not place: NOT_ALLOWED (56)", is_rej(r, NOT_ALLOWED))
    stand_south_of(a, ux, uz)
    _x, r = of_txn(run, a, PLACE, D_NONE, s2, one, 0, ux, uz, pre=pre_with(a, {s2: one}))
    ck("P5 the occupied tile is BAD_TILE (58)", is_rej(r, BAD_TILE))
    _x, r = of_txn(run, a, PLACE, D_NONE, s2, one, 0, ux + 5, uz - 5, pre=pre_with(a, {s2: one}))
    ck("P5 a tile out of reach is BAD_TILE (58)", is_rej(r, BAD_TILE))

    stand_south_of(a, ux, uz)
    _x, r = of_txn(run, a, ROTATE, D_NONE, 0, one | 2, 3, ux, uz)
    ck("R1 the resident rotates it: APPLIED, the tile = id | 3, SET delta at the guest",
       r is not None and r.outcome == APPLIED and wait(a, lambda: tile_val(a, ux, uz) == (one | 3)) and wait(g, lambda: (view(g) or {}).get((ux, uz)) == (one | 3)))
    stand_south_of(g, ux, uz)
    _x, r = of_txn(run, g, ROTATE, D_NONE, 0, one | 3, 0, ux, uz)
    ck("R2 a guest may not rotate a placed piece: NOT_ALLOWED (56)", is_rej(r, NOT_ALLOWED))
    _x, r = of_txn(run, a, ROTATE, D_NONE, 0, one | 2, 0, ux, uz)
    ck("R3 a stale view (the old rotation) is GONE (61)", is_rej(r, GONE))

    b = run.ready("B", run.r2)
    b.wait_record_synced(30.0)
    ck("J1 a late joiner gets the list with the placed piece (id | 3)", wait(b, lambda: lists_done(b) >= 1) and (view(b) or {}).get((ux, uz)) == (one | 3))

    off = len(run.log())
    prov = g.pickup(ux, uz, run.fresh_rid(), timeout=2.0, claim=True, auto_confirm=False)
    ck("V1 a GUEST's vanilla PICKUP_REQUEST of the PLACED piece is refused (the leaf of a placed piece is not a guest's)",
       prov is not None and not prov.accepted and run.n_log(r"vanilla pickup of the PLACED piece at \(%d,%d\) REFUSED" % (ux, uz), off) >= 1)
    if seeded:
        lx, lz, lit = int(seeded.group(1)), int(seeded.group(2)), int(seeded.group(3), 16)
        prov, _s, r = g.txn_pickup(lx, lz, run.fresh_rid(), timeout=4.0)
        ck("V2 the same guest picks up a furniture LEAF (no marker): accepted, the item in its pocket", prov is not None and prov.accepted and r is not None and r.outcome == APPLIED
           and lit in r.post_pockets)
    sg = free_slot(pre_with(g))
    stand_south_of(g, ux, uz)
    _x, r = of_txn(run, g, PICKUP, D_POCKET, sg, one | 3, 0, ux, uz)
    ck("K22 a guest's kind-22 PICKUP of the placed piece is NOT_ALLOWED (56)", is_rej(r, NOT_ALLOWED))

    op = guest(run, "OPG", 0x6F02)
    ck("O1 an OPERATOR guest (AC_TEST_OPS=OPG via the console's op code) gets pickup|op|leaf rights, never place",
       wait(op, lambda: perms(op) == (P_PICKUP | P_OP | P_LEAF), 10.0) and re.search(r"\[NET\]\[OPS\]\[TEST-ONLY\] host: op OPG -> .*is now an operator", run.log()) is not None)
    stand_south_of(op, ux, uz)
    _x, r = of_txn(run, op, ROTATE, D_NONE, 0, one | 3, 1, ux, uz)
    ck("O2 the operator guest may rotate the placed piece: APPLIED (id | 1)", r is not None and r.outcome == APPLIED and wait(a, lambda: tile_val(a, ux, uz) == (one | 1)))

    pre = pre_with(a)
    s3 = free_slot(pre)
    stand_south_of(a, ux, uz)
    _x, r = of_txn(run, a, PICKUP, D_POCKET, s3, one | 1, 0, ux, uz, pre=pre)
    exp = list(pre[0])
    exp[s3] = one
    ck("PK1 the resident's kind-22 PICKUP: APPLIED, the BASE item (rotation 0) in the free slot, nothing else changed",
       r is not None and r.outcome == APPLIED and list(r.post_pockets) == exp and r.post_wallet == pre[2])
    ck("PK1 the tile is empty and the others got DEL", wait(b, lambda: tile_val(b, ux, uz) == 0) and wait(b, lambda: (ux, uz) not in (view(b) or {})))
    pl = place_somewhere(run, a, one, rot=1, skip=((ux, uz),))
    ck("PK2 placed again", pl is not None)
    if pl is not None:
        (vx, vz), *_r = pl
        wait(b, lambda: tile_val(b, vx, vz) == (one | 1))
        prov, _s, r = b.txn_pickup(vx, vz, run.fresh_rid(), timeout=4.0)
        ck("PK2 a RESIDENT's vanilla pickup of a placed piece is accepted with the rotation-0 item",
           prov is not None and prov.accepted and prov.granted_item == one and r is not None and r.outcome == APPLIED and one in r.post_pockets)
        ck("PK2 the marker is garbage-collected once the tile is empty (DEL at the guest)", wait(g, lambda: (vx, vz) not in (view(g) or {}), 6.0))
    ck("NV no record-protocol violation was counted for any of these requests (every refusal is semantic, never BAD_SHAPE)", run.n_log(r"\[NET\]\[REC\] host: peer \d+ violation") == 0)


def phase_gc(run):
    ck = run.check
    m = None
    w = 0.0
    while m is None and w < 30:
        m = re.search(r"seeded placed piece at \((\d+),(\d+)\) item 0x([0-9A-F]{4}) rot (\d) -> placed", run.log())
        L.pump_sleep(0.5)
        w += 0.5
    ck("G0 the host seeded a placed piece (AC_TEST_OFURN_SEED)", m is not None)
    if m is None:
        return
    ux, uz, it, rot = int(m.group(1)), int(m.group(2)), int(m.group(3), 16), int(m.group(4))
    a = run.ready("A", run.r1)
    a.wait_record_synced(30.0)
    ck("G1 the joining client's list carries the seeded piece (join push)", wait(a, lambda: (view(a) or {}).get((ux, uz)) == (it | rot), 10.0))
    ck("G2 the tile overwritten behind the system's back: the GC drops the marker (log) and the client gets DEL",
       wait(a, lambda: (ux, uz) not in (view(a) or {}), 30.0) and re.search(r"GC tile \(%d,%d\)" % (ux, uz), run.log()) is not None)


def phase_off(run):
    ck = run.check
    a = run.ready("A", run.r1)
    a.wait_record_synced(30.0)
    ck("OFF1 AC_MP_OUTDOOR_FURNITURE=0: service 10 says Place OFF (flags bit 0 = 0)", wait(a, lambda: caps(a) is not None) and (caps(a)[1] & 1) == 0)
    ck("OFF2 PERMS without PLACE", wait(a, lambda: perms(a) is not None) and (perms(a) & P_PLACE) == 0)
    s = free_slot(pre_with(a))
    stand_south_of(a, 40, 50)
    _x, r = of_txn(run, a, PLACE, D_NONE, s, LEAF_ITEM, 0, 40, 50, pre=pre_with(a, {s: LEAF_ITEM}))
    ck("OFF3 a PLACE anyway is OFURN_OFF (55), not BAD_SHAPE (no violation)", is_rej(r, OFF))


def phase_leaf(run):
    ck = run.check
    a = run.ready("A", run.r1)
    a.wait_record_synced(30.0)
    ck("LEAF1 outdoor_furniture_leaf_drop = 0: service 10 / PERMS say no leaf drop",
       wait(a, lambda: caps(a) is not None) and (caps(a)[1] & 2) == 0 and wait(a, lambda: perms(a) is not None) and (perms(a) & P_LEAF) == 0)
    cands = candidates(a, 40)
    s = free_slot(pre_with(a))
    done_f = done_n = None
    for (ux, uz) in cands:
        # the SAME tile, the same position: furniture first (refused by the policy), then a net (accepted) -- the tile itself is droppable
        a.claim_position(*L.tile_center(ux, uz))
        L.pump_sleep(0.3)
        done_f = a.drop(s, LEAF_ITEM, ux, uz, run.fresh_rid(), timeout=1.5, claim_at=(ux, uz), auto_confirm=False)
        L.pump_sleep(0.3)
        done_n = a.drop(s, 0x2200, ux, uz, run.fresh_rid(), timeout=1.5, claim_at=(ux, uz), auto_confirm=False)
        if done_n is not None and done_n.accepted:
            break
    ck("LEAF2 a vanilla DROP_REQUEST of FURNITURE is refused by the host ...", done_f is not None and not done_f.accepted)
    ck("LEAF3 ... while a DROP_REQUEST of a net on the SAME tile from the same position is accepted (only the furniture policy refused)", done_n is not None and done_n.accepted)


def phase_cap(run):
    ck = run.check
    a = run.ready("A", run.r1)
    a.wait_record_synced(30.0)
    wait(a, lambda: perms(a) is not None)
    one, _big = classify_ids(run, a)
    ck("CAP0 a 1x1 id", one is not None)
    if one is None:
        return
    pl = place_somewhere(run, a, one)
    ck("CAP1 the first piece of the acre is placed", pl is not None)
    if pl is None:
        return
    (ux, uz), *_r = pl
    acre = L.town_ut_to_acre_tile(ux, uz)[0]
    same = [t for t in candidates(a) if L.town_ut_to_acre_tile(*t)[0] == acre and t != (ux, uz) and abs(t[0] - ux) + abs(t[1] - uz) > 1]
    r = None
    for (vx, vz) in same[:10]:
        s = free_slot(pre_with(a))
        stand_south_of(a, vx, vz)
        _x, r = of_txn(run, a, PLACE, D_NONE, s, one, 0, vx, vz, pre=pre_with(a, {s: one}))
        if not is_rej(r, BAD_TILE):
            break
    ck("CAP2 outdoor_furniture_max_acre = 1: the second piece of that acre is OFURN_LIMIT (59), a polite refusal", is_rej(r, LIMIT))


def set_setting(path, key, value):
    txt = open(path, encoding="utf-8", errors="replace").read()
    if re.search(r"^%s\s*=" % re.escape(key), txt, re.M):
        txt = re.sub(r"^%s\s*=.*$" % re.escape(key), "%s = %s" % (key, value), txt, flags=re.M)
    elif "[Network]" in txt:
        txt = txt.replace("[Network]", "[Network]\n%s = %s" % (key, value), 1)
    else:
        txt += "\n[Network]\n%s = %s\n" % (key, value)
    open(path, "w", encoding="utf-8").write(txt)


def phase_restart(results, ip, port, restore):
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    os.environ["AC_TEST_OFURN_SEED"] = "0x%04X,2" % LEAF_ITEM
    h1, ok = TS.start_host(ip, port, "OFRS1", [], results)
    m = None
    try:
        if ok:
            m = h1.wait_for_log(r"seeded placed piece at \((\d+),(\d+)\) item 0x[0-9A-F]{4} rot \d -> placed", 40.0)
            ck("RS1 host 1 placed a piece (sidecar written)", m is not None)
    finally:
        h1.stop()
        os.environ.pop("AC_TEST_OFURN_SEED", None)
    sidecar = os.path.join(L.GAME_BIN_DIR, "save", "mp", "outdoor_furniture.dat")
    ck("RS2 save/mp/outdoor_furniture.dat exists after host 1 (%s)" % sidecar, os.path.isfile(sidecar) and os.path.getsize(sidecar) == 12 + 52 + 4)
    h2, ok = TS.start_host(ip, port + 1, "OFRS2", [], results)
    try:
        if ok:
            ld = h2.wait_for_log(r"outdoor_furniture\.dat loaded: (\d+) placed piece", 40.0)
            ck("RS3 host 2 loaded the sidecar (1 piece)", ld is not None and "loaded: 1 placed" in h2.log_text())
            L.pump_sleep(3.0)
            gc = "GC tile" in h2.log_text()
            ck("RS4 the marker survives only where the town tile still holds it (the town was not saved by a killed host: GC'd = %s)" % gc, True)
    finally:
        rc = h2.stop()
        ck("RS host 2 did not crash", rc is None)
        restore()


def run_all(port, only, results):
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_of"):
        print("REFUSING: this test mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_of_<name> copy only (NET_SPIKE_GAME_BIN=<abs path>)", file=sys.stderr)
        return 2
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    settings = os.path.join(L.GAME_BIN_DIR, "settings.ini")
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "ofurn_proto_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    settings_orig = open(settings, encoding="utf-8", errors="replace").read() if os.path.isfile(settings) else None
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    def restore_settings():
        if settings_orig is not None:
            open(settings, "w", encoding="utf-8").write(settings_orig)

    phases = (("MAIN", {"AC_TEST_OFURN_LEAF": "0x%04X" % LEAF_ITEM, "AC_TEST_OPS": "OPG"}, {}, phase_main),
              ("GC", {"AC_TEST_OFURN_SEED": "0x%04X,2" % LEAF_ITEM, "AC_TEST_OFURN_GC_MS": "25000"}, {}, phase_gc),
              ("OFF", {"AC_MP_OUTDOOR_FURNITURE": "0"}, {}, phase_off),
              ("LEAF", {}, {"outdoor_furniture_leaf_drop": "0"}, phase_leaf),
              ("CAP", {}, {"outdoor_furniture_max_acre": "1"}, phase_cap))
    keys = ("AC_TEST_OFURN_LEAF", "AC_TEST_OPS", "AC_TEST_OFURN_SEED", "AC_TEST_OFURN_GC_MS", "AC_MP_OUTDOOR_FURNITURE")
    try:
        for i, (tag, env, sets, body) in enumerate(phases):
            if only and tag not in only:
                continue
            for k in keys:
                os.environ.pop(k, None)
            os.environ.update(env)
            restore_settings()
            for k, v in sets.items():
                set_setting(settings, k, v)
            TS.run_phase(results, ip, port + i, "OF" + tag, [], body, snap_gci, restore)
        if not only or "RS" in only:
            for k in keys:
                os.environ.pop(k, None)
            restore_settings()
            phase_restart(results, ip, port + 20, restore)
    finally:
        for k in keys:
            os.environ.pop(k, None)
        restore_settings()
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=25100)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    if not (25000 <= args.port <= 25960):
        print("ports 25000-25999 only", file=sys.stderr)
        return 2
    results = []
    rc = run_all(args.port, set(x for x in args.only.split(",") if x), results)
    if rc is not None:
        return rc
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
