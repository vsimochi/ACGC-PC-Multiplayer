#!/usr/bin/env python3
"""test_txn_x3_protocol.py - X3 (HOST half) of the host-transactional GRANTS: DIG_BURIED / DIG_HOLE(+golden-shovel bonus) / DIG_SHINE(+bell
roll) carried by FIELD_ACTION_REQUEST (29, now 76 B) and CATCH (fish and bug) carried by CATCH_REQUEST (41, now 84 B), plus the
exchange-menu DROP that folds the full-pockets catch replacement into the transaction (TXN_COMMIT kind DROP with flags EXCHANGE).

PROTOCOL test against REAL host processes (`AnimalCrossing.exe --host --bootstrap-resident 0 --pickup-test-seed --bury-test-seed
--field-action-test-seed [--authoritative-wildlife] [--txn-fault=...]`, started and stopped by this script from the TEST COPY named by
NET_SPIKE_GAME_BIN = pc\\build64\\bin_fixture4; host = resident 0, clients = residents 1..3, at most 3 simultaneous clients). The fixture
save dir is SNAPSHOTTED at start and RESTORED after every host phase. The clients are scripted FakeClients (net_spike_lib: the X3 grant
builders, TXN_RESULT parsing); the host's C code runs for real. The real game CLIENT half is covered by test_txn_x3_real_client.py.

Phases (one host process each):
  G        grants without wildlife: DIG_BURIED / DIG_HOLE bonus / DIG_SHINE bonus succeed (world tile + mirror + rev + cdig + ordering: TXN_RESULT
           before the legacy FIELD_ACTION_RESULT), byte-identical resends replay (never a second execution), CONFLICT, STALE_IMAGE x3, BAD_IMAGE,
           PRECOND, WORLD_CHANGED, BAD_SHAPE matrix (incl. a malformed CATCH) + the 3-violation close, NOT_SYNCED, FENCED after a new process,
           the same-tile race of two clients (exactly one grant), the mirror seen by a new session
  F-ignore ignore_commit:1:1   the resend after the ignored first COMMIT executes exactly once
  F-fail   fail_world:1:1      WORLD_CHANGED, same-seq replay REJECTED, the tile is free for another client
  F-drop   drop_result:1:2     the TXN_RESULT is lost twice, the third resend replays APPLIED, executed once
  F-kill   kill_peer_after_commit:1:1   disconnect after commit: a new session sees the effect, same-nonce resend replays
  W        --authoritative-wildlife: catch success / replay / race / item mismatch / species mismatch / stale generation / full pockets
           (dest NONE: accepted, exchange CREDIT) and the exchange-menu drop (credit mismatch, success, credit consumed once)
  W-fail   fail_world:1:1 + wildlife   the failed catch leaves the entity live; the next claimant wins
  W-kill   kill_peer_after_commit:1:1 + wildlife   the entity is removed once, a new session has the item, the same-nonce resend replays
  CLI      (no game boot)

Wildlife spawns are RNG-dependent (the fixture date / the spawn decision): a phase that cannot get enough entities FAILS loudly instead of
skipping. Tier: PROTOCOL TESTED (real host binary, scripted clients). Usage: python test_txn_x3_protocol.py [--port 10800] [--only G,W,...]
"""
import argparse
import os
import re
import shutil
import struct
import sys

import net_spike_lib as L
import test_txn_protocol as TP
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE, D_POCKET, S_WALLET, first_free
import test_wildlife_catch_exchange_gate as WG

K_BURIED, K_HOLE, K_SHINE, K_CATCH = (L.PC_NETGAME_TXN_KIND_DIG_BURIED, L.PC_NETGAME_TXN_KIND_DIG_HOLE, L.PC_NETGAME_TXN_KIND_DIG_SHINE,
                                      L.PC_NETGAME_TXN_KIND_CATCH)
FA_BURIED, FA_HOLE, FA_SHINE = L.FIELD_ACTION_KIND_DIG_BURIED, L.FIELD_ACTION_KIND_DIG_HOLE, L.FIELD_ACTION_KIND_DIG_SHINE
ITM_APPLE, ITM_PITFALL = 0x2800, 0x2500 + 18
ITM_M100, ITM_M1000, ITM_M10000, ITM_M30000 = 0x2103, 0x2100, 0x2101, 0x2102
BURIED_TILE = (40, 104)      # --field-action-test-seed: a buried ITM_FOOD_APPLE (deposit ON)
PITFALL_TILE = (24, 105)     # BURIED_PITFALL_HOLE00 (DIG_BURIED grants ITM_PITFALL)
PITFALL_TILE2 = (40, 105)    # a second one
HOLE_TILE = (56, 105)        # EMPTY_NO (DIG_HOLE)
SHINE_TILE = (88, 105)       # SHINE_SPOT
HOLE_START, HOLE_SHINE = 0x0011, 0x005D
KIND_NAMES = L.TXN_KIND_NAMES
FA_RES_FMT = L.FIELD_ACTION_RESULT_FMT
DESPAWN_TYPE = 43


# ----------------------------------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------------------------------
def fa_results(c, rid, since=None):
    """The legacy FIELD_ACTION_RESULT(s) of this request on the current connection: [dict(index, accepted, item)], non consuming."""
    conn = c.connect_count
    out = []
    for m in c.inbox.peek_all(lambda m: m.conn == conn and m.channel == L.CH_RELIABLE and m.payload and m.payload[0] == 30
                              and len(m.payload) == 12, since=since):
        _t, kind, acc, _ux, r, item, _uz, _x = struct.unpack(FA_RES_FMT, m.payload)
        if r == rid:
            out.append(dict(index=m.index, kind=kind, accepted=acc, item=item))
    return out


def catch_results(c, rid, since=None):
    conn = c.connect_count
    out = []
    for m in c.inbox.peek_all(lambda m: m.conn == conn and m.channel == L.CH_RELIABLE and m.payload and m.payload[0] == 42
                              and len(m.payload) == 12, since=since):
        _t, acc, item, ent, r = struct.unpack(L.CATCH_RESULT_FMT, m.payload)
        if r == rid:
            out.append(dict(index=m.index, accepted=acc, item=item, entity=ent))
    return out


def txn_by_rid(c, rid):
    return [g for conn, g in c.txn_results if g.request_id == rid and conn == c.connect_count]


def txn_index(c, seq, since=None):
    return [m.index for m in c.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_TXN_RESULT, (L.CH_RELIABLE,)), since=since)
            if m.game is not None and m.game.txn_seq == seq]


def despawn_ids(c, since=None):
    return [struct.unpack_from("<I", m.payload, 4)[0]
            for m in c.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.payload and m.payload[0] == DESPAWN_TYPE and len(m.payload) == 8,
                                      since=since)]


def wait_rid(c, rid, timeout=3.0):
    c.hub.wait_until(lambda: len(txn_by_rid(c, rid)) >= 1, timeout)
    r = txn_by_rid(c, rid)
    return r[0] if r else None


def grant(run, c, fa_kind, tile, item=0, hv=0, wait=3.0, claim=True, **kw):
    """send_fa_grant + wait for its TXN_RESULT: (rid, TxnSent, result or None)."""
    if claim:
        c.claim_position(*L.tile_center(*tile))
    rid = run.fresh_rid()
    sent = c.send_fa_grant(fa_kind, tile[0], tile[1], rid, item=item, hole_variant=hv, **kw)
    return rid, sent, c.wait_txn_result(sent.seq, wait)


def applied_lines(run, kind, rid, off=0):
    return run.n_log(r"TXN APPLIED kind=%s request=%d " % (kind, rid), off)


def tile_val(run, c, tile):
    return run.tile_of(c, tile)


def is_hole(v):
    return v is not None and HOLE_START <= v <= HOLE_START + 24


# ----------------------------------------------------------------------------------------------------------------------
# Phase G
# ----------------------------------------------------------------------------------------------------------------------
def g_buried_success(run, a, b):
    ck = run.check
    tile = BURIED_TILE
    mark_b = b.inbox.mark()
    mark_a = a.inbox.mark()
    pre = a.txn_pre_image()
    slot = first_free(pre[0])
    base = a.rec_last
    off = len(run.log())
    rid, sent, r = grant(run, a, FA_BURIED, tile)
    run.tx_ok("G1 DIG_BURIED grant (the host resolves the item)", r, APPLIED, R["NONE"])
    if r is None:
        return None
    ck("G1 RESULT echoes kind 4 / request / nonce / seq / dest POCKET / slot, and carries the HOST-resolved item (the request carried 0)",
       (r.kind, r.request_id, r.txn_nonce, r.txn_seq, r.dest, r.slot, r.item) == (K_BURIED, rid, sent.nonce, sent.seq, D_POCKET, slot, ITM_APPLE))
    ck("G1 lineage: same host_session / epoch, rev == base rev + 1", (r.host_session, r.epoch, r.rev) == (base[0], base[1], base[2] + 1))
    exp_p = tuple(ITM_APPLE if i == slot else p for i, p in enumerate(pre[0]))
    ck("G1 post-image: the item in the chosen slot, every other pocket / conds / wallet unchanged",
       tuple(r.post_pockets) == exp_p and r.post_conds == pre[1] and r.post_wallet == pre[2])
    ck("G1 cdig == FNV of the client+shared ranges of the mirror (patched local image)", r.cdig == TP.cown_digest(a.rec_local) and r.cdig != 0)
    L.pump_sleep(0.5)
    ck("G1 world: the observer sees the tile become a HOLE via exactly ONE FIELD_UPDATE",
       is_hole(tile_val(run, b, tile)) and len(run.field_updates(b, tile, mark_b)) == 1)
    fr = fa_results(a, rid, mark_a)
    ck("G1 the legacy FIELD_ACTION_RESULT follows: accepted with the SAME item", len(fr) == 1 and fr[0]["accepted"] == 1 and fr[0]["item"] == ITM_APPLE)
    ti = txn_index(a, sent.seq, mark_a)
    ck("G1 the TXN_RESULT is delivered BEFORE the legacy FIELD_ACTION_RESULT", len(ti) == 1 and fr and ti[0] < fr[0]["index"])
    ck("G1 host log: exactly one 'TXN APPLIED kind=DIG_BURIED' and one grant-committed line for this request",
       applied_lines(run, "DIG_BURIED", rid, off) == 1 and run.n_log(r"\[DIG_BURIED\] host: peer \d+ request %d grant committed" % rid, off) == 1)
    return dict(rid=rid, sent=sent, r=r, tile=tile, slot=slot, pre=pre)


def g_dups(run, a, b, s):
    ck = run.check
    off = len(run.log())
    mark_b = b.inbox.mark()
    a.resend_txn(s["sent"], copies=3)    # a second reliable message, its datagram transmitted 3x
    a.resend_txn(s["sent"])              # and a third (what the real client does after 2 s without a result)
    got = a.wait_txn_result(s["sent"].seq, 3.0, nth=3)
    allr = a.txn_results_for(s["sent"].seq)
    ck("G2 three identical requests get three answers: APPLIED/NONE then two APPLIED/REPLAYED",
       got is not None and len(allr) == 3 and allr[0].reason == R["NONE"] and [g.reason for g in allr[1:]] == [R["REPLAYED"]] * 2
       and all(g.outcome == APPLIED for g in allr))
    ck("G2 the replays echo the journalled host-resolved item / dest / slot and the CURRENT mirror (same rev, same post-image)",
       all(g.item == ITM_APPLE and g.dest == D_POCKET and g.slot == s["slot"] and g.rev == allr[0].rev
           and tuple(g.post_pockets) == tuple(allr[0].post_pockets) for g in allr))
    L.pump_sleep(0.4)
    ck("G2 NO double execution: no new applied line, two replay lines, no FIELD_UPDATE, the item exists once",
       applied_lines(run, "DIG_BURIED", s["rid"], off) == 0 and run.n_log(r"TXN replay APPLIED\S* kind=DIG_BURIED request=%d " % s["rid"], off) == 2
       and len(run.field_updates(b, s["tile"], mark_b)) == 0 and list(allr[0].post_pockets).count(ITM_APPLE) == list(s["pre"][0]).count(ITM_APPLE) + 1)
    # CONFLICT: the same (nonce, seq) with DIFFERENT bytes (another hole_variant) is never executed
    tag = s["sent"].raw[12:76]
    bad = L.build_field_action_request(FA_BURIED, s["tile"][0], s["tile"][1], s["rid"], 9, tag=tag)
    a.send_fa_grant(0, 0, 0, 0, raw=bad)
    rc = a.wait_txn_result(s["sent"].seq, 2.5, nth=4)
    ck("G2 same (nonce, seq) with DIFFERENT bytes -> REJECTED(CONFLICT), loudly logged, never executed",
       rc is not None and rc.outcome == REJECTED and rc.reason == R["CONFLICT"]
       and ("CONFLICT: seq %d was seen with DIFFERENT bytes" % s["sent"].seq) in run.log(off))
    fr = fa_results(a, s["rid"])
    ck("G2 ... and the legacy RESULT of the conflicting copy says accepted=0", any(x["accepted"] == 0 for x in fr))


def g_validation(run, a, b):
    """Rejections that never touch the world: the DIG_HOLE tile (56,105) must stay EMPTY_NO and the mirror unchanged."""
    ck = run.check
    tile = HOLE_TILE
    cur = a.rec_last
    pre = a.txn_pre_image()
    sl = first_free(pre[0])
    mark_b = b.inbox.mark()
    rev_now = cur[2]

    def neg(desc, reason, fa_kind=FA_HOLE, item=ITM_M100, **kw):
        rid, sent, r = grant(run, a, fa_kind, tile, item=item, hv=2, **kw)
        run.tx_ok(desc, r, REJECTED, reason)
        ck(f"{desc}: nothing mutated (the reject reports the current rev, the tile is untouched)",
           r is not None and r.rev == rev_now and tile_val(run, b, tile) == L.EMPTY_NO)
        fr = fa_results(a, rid)
        ck(f"{desc}: the legacy RESULT says accepted=0", len(fr) == 1 and fr[0]["accepted"] == 0)
        return r

    for label, mk in (("base_rev older than the last pocket transaction", lambda c: (c[1], c[2] - 1)),
                      ("base_epoch of another lineage", lambda c: (c[1] ^ 0x1234, c[2])),
                      ("base_rev from the future", lambda c: (c[1], c[2] + 7))):
        np0 = len(a.rec_pushes)
        neg(f"G3 STALE_IMAGE {label}", R["STALE_IMAGE"], base=mk(cur))
        if label.startswith("base_rev older"):
            ck("G3 the paced full push follows a STALE_IMAGE", a.wait_record_push(after=np0, timeout=4.0) is not None)
        L.pump_sleep(2.2)
        cur = a.rec_last
        rev_now = cur[2]
    pre = a.txn_pre_image()
    p_illegal = tuple(0x4000 if i == 1 else p for i, p in enumerate(pre[0]))
    neg("G3 BAD_IMAGE illegal item id (0x4000) in a pocket", R["BAD_IMAGE"], pre=(p_illegal, pre[1], pre[2]))
    neg("G3 BAD_IMAGE wallet above the maximum", R["BAD_IMAGE"], pre=(pre[0], pre[1], 100000))
    neg("G3 BAD_IMAGE item_conditions bit 31", R["BAD_IMAGE"], pre=(pre[0], 0x80000000, pre[2]))
    sl = first_free(pre[0])
    occupied = tuple(0x2801 if i == sl else p for i, p in enumerate(pre[0]))
    neg("G3 PRECOND the chosen slot is occupied in the pre-image", R["PRECOND"], pre=(occupied, pre[1], pre[2]), slot=sl)
    neg("G3 WORLD_CHANGED DIG_BURIED at a tile with no buried item", R["WORLD_CHANGED"], fa_kind=FA_BURIED, item=0)
    ck("G3 none of the rejects produced a FIELD_UPDATE for the tile", len(run.field_updates(b, tile, mark_b)) == 0)


def g_hole_shine(run, a, b):
    ck = run.check
    for name, fa_kind, kind, tile, item, hv, want_tile in (("DIG_HOLE bonus", FA_HOLE, K_HOLE, HOLE_TILE, ITM_M100, 7, None),
                                                          ("DIG_SHINE bell", FA_SHINE, K_SHINE, SHINE_TILE, ITM_M10000, 0, HOLE_SHINE)):
        mark_b = b.inbox.mark()
        mark_a = a.inbox.mark()
        pre = a.txn_pre_image()
        slot = first_free(pre[0])
        base = a.rec_last
        off = len(run.log())
        rid, sent, r = grant(run, a, fa_kind, tile, item=item, hv=hv)
        run.tx_ok(f"G4 {name} grant", r, APPLIED, R["NONE"])
        if r is None:
            continue
        ck(f"G4 {name}: kind {kind}, dest POCKET / slot {slot} / item 0x{item:04X}, rev + 1, post-image has the bonus in the slot only",
           (r.kind, r.dest, r.slot, r.item) == (kind, D_POCKET, slot, item) and r.rev == base[2] + 1
           and tuple(r.post_pockets) == tuple(item if i == slot else p for i, p in enumerate(pre[0])))
        L.pump_sleep(0.5)
        v = tile_val(run, b, tile)
        ck(f"G4 {name} world: tile -> 0x{(HOLE_START + hv) if want_tile is None else want_tile:04X} via ONE FIELD_UPDATE (got {v})",
           v == ((HOLE_START + hv) if want_tile is None else want_tile) and len(run.field_updates(b, tile, mark_b)) == 1)
        fr = fa_results(a, rid, mark_a)
        ti = txn_index(a, sent.seq, mark_a)
        ck(f"G4 {name}: TXN_RESULT before the legacy RESULT, which says accepted", len(fr) == 1 and fr[0]["accepted"] == 1 and ti and ti[0] < fr[0]["index"])
        ck(f"G4 {name} host log: one applied line", applied_lines(run, KIND_NAMES[kind], rid, off) == 1)


def g_race(run, a, b):
    ck = run.check
    tile = PITFALL_TILE
    pre_a, pre_b = a.txn_pre_image(), b.txn_pre_image()
    mark_a, mark_b = a.inbox.mark(), b.inbox.mark()
    off = len(run.log())
    a.claim_position(*L.tile_center(*tile))
    b.claim_position(*L.tile_center(*tile))
    rid_a, rid_b = run.fresh_rid(), run.fresh_rid()
    sa = a.send_fa_grant(FA_BURIED, tile[0], tile[1], rid_a)
    sb = b.send_fa_grant(FA_BURIED, tile[0], tile[1], rid_b)
    ra, rb = a.wait_txn_result(sa.seq, 3.0), b.wait_txn_result(sb.seq, 3.0)
    ck("G5 race: both clients got a TXN_RESULT", ra is not None and rb is not None)
    if ra is None or rb is None:
        return
    ck("G5 race: EXACTLY ONE of the two grants was APPLIED (the other WORLD_CHANGED)",
       [ra.outcome, rb.outcome].count(APPLIED) == 1 and {ra.reason, rb.reason} == {R["NONE"], R["WORLD_CHANGED"]})
    win, lose, win_c, lose_c, pre_l = (ra, rb, a, b, pre_b) if ra.outcome == APPLIED else (rb, ra, b, a, pre_a)
    ck("G5 race: the winner got ITM_PITFALL (the host-resolved item); the loser's reject carries no post-image", win.item == ITM_PITFALL
       and tuple(lose.post_pockets) == (0,) * 15)
    L.pump_sleep(0.5)
    ck("G5 race: the tile changed exactly once (ONE FIELD_UPDATE reached each client)",
       len(run.field_updates(b, tile, mark_b)) == 1 and len(run.field_updates(a, tile, mark_a)) == 1 and is_hole(tile_val(run, b, tile)))
    ck("G5 race: one applied line, one WORLD_CHANGED refusal for the loser",
       applied_lines(run, "DIG_BURIED", rid_a if win is ra else rid_b, off) == 1)
    # the loser's mirror is unchanged: a NEW process of that resident is pushed its OLD pockets
    lbl, slotno = ("A2", run.r1) if lose_c is a else ("B2", run.r2)
    run.release(lose_c)
    n = run.client(lbl, slotno)
    n.connect_and_ready(quiet=True)
    p = n.rec_pushes[-1] if n.rec_pushes else None
    ck("G5 race: the LOSER's mirror was not touched (a new session of it is pushed its pre-race pockets)",
       p is not None and L.record_inventory(p["data"])[0] == tuple(pre_l[0]))
    return n, lose_c is a


def g_misc(run, a, b, s):
    """FENCED after a new process, NOT_SYNCED, the BAD_SHAPE matrix and the 3-violation close."""
    ck = run.check
    host = run.host
    # --- FENCED / new nonce: a request of a NEW process (new nonce) journals and fences the old one
    prior, old_nonce, old_seq = s["sent"].raw, s["sent"].nonce, s["sent"].seq   # G1's DIG_BURIED request (process A of the first connection)
    if a.txn_nonce == old_nonce:
        a.new_process(keep_record=True)   # A is still that process: become a NEW process (new nonce, seq restarts at 1)
    rid, sent, r = grant(run, a, FA_BURIED, (56, 105), item=0)   # not a buried tile: WORLD_CHANGED, but it journals the NEW nonce
    run.tx_ok("G6 a NEW process (new nonce, seq 1) is served: REJECTED(WORLD_CHANGED) but journalled", r, REJECTED, R["WORLD_CHANGED"])
    ck("G6 the host logged the new client nonce and fenced the old one", ("new client nonce %d" % a.txn_nonce) in run.log())
    n_old = len(a.txn_results_for(old_seq, nonce=old_nonce))
    a.send_reliable(prior)
    rf = a.wait_txn_result(old_seq, 2.5, nth=n_old + 1, nonce=old_nonce)
    ck("G6 the OLD nonce's request resent after the new process is REJECTED(FENCED)", rf is not None and rf.outcome == REJECTED and rf.reason == R["FENCED"])
    # --- NOT_SYNCED
    ns = run.client("NS", run.r3, record_hello=False, record_wait=False)
    ns.connect_and_ready(quiet=True)
    rid, sent, r = grant(run, ns, FA_BURIED, PITFALL_TILE2, item=0, pre=((0,) * 15, 0, 0), base=(0, 0))
    run.tx_ok("G7 a READY-but-not-SYNCED peer's grant", r, REJECTED, R["NOT_SYNCED"])
    ck("G7 the legacy RESULT says accepted=0 and the tile was not dug", fa_results(ns, rid) and fa_results(ns, rid)[0]["accepted"] == 0
       and not is_hole(tile_val(run, b, PITFALL_TILE2)))
    run.release(ns)
    # --- BAD_SHAPE matrix from a throw-away session, two probes per connection; a third closes the peer
    g = run.client("G", run.r3)
    g.connect_and_ready(quiet=True)
    probes = []

    def probe(desc, send):
        probes.append((desc, send))

    probe("DIG_HOLE grant with the wrong bonus item (ITM_MONEY_1000)", lambda c, rid: c.send_fa_grant(FA_HOLE, 56, 105, rid, item=ITM_M1000))
    probe("DIG_HOLE grant with dest NONE", lambda c, rid: c.send_fa_grant(FA_HOLE, 56, 105, rid, item=ITM_M100, dest=D_NONE, slot=S_WALLET))
    probe("DIG_SHINE grant with an item outside the shine set", lambda c, rid: c.send_fa_grant(FA_SHINE, 88, 105, rid, item=ITM_PITFALL))
    probe("DIG_SHINE grant with slot 15", lambda c, rid: c.send_fa_grant(FA_SHINE, 88, 105, rid, item=ITM_M1000, slot=15))
    probe("DIG_BURIED grant with a non-zero item (the host resolves it)", lambda c, rid: c.send_fa_grant(FA_BURIED, 40, 105, rid, item=ITM_APPLE))
    probe("DIG_BURIED with an all-zero tag", lambda c, rid: c.send_fa_grant(0, 0, 0, 0, raw=L.build_field_action_request(FA_BURIED, 40, 105, rid)))
    probe("TREE_SHAKE (never grants) with a non-zero tag", lambda c, rid: c.send_fa_grant(
        0, 0, 0, 0, raw=L.build_field_action_request(3, 56, 104, rid, tag=c.build_grant_tag(D_POCKET, None, 0)[0])))
    probe("DIG_HOLE grant with the EXCHANGE flag", lambda c, rid: c.send_fa_grant(FA_HOLE, 56, 105, rid, item=ITM_M100, flags=1))
    probe("DIG_HOLE grant with nonce 0", lambda c, rid: c.send_fa_grant(FA_HOLE, 56, 105, rid, item=ITM_M100, nonce=0))
    probe("CATCH_REQUEST with an all-zero tag", lambda c, rid: c.send_catch_txn(0, 0, 0, 0, raw=L.build_catch_request(1, 0, rid, 0)))
    probe("CATCH with dest POCKET and item 0", lambda c, rid: c.send_catch_txn(0, 0, 0, 0, raw=c.catch_request_bytes(1, 0, rid, 0, item=0, kind="fish")))
    probe("CATCH with dest NONE and an item", lambda c, rid: c.send_catch_txn(
        0, 0, 0, 0, raw=L.build_catch_request(1, 0, rid, 0, tag=c.build_grant_tag(D_NONE, S_WALLET, 0x2300)[0])))
    off = len(run.log())
    good_probes = 0
    for i in range(0, len(probes) - 1, 2):
        pair = probes[i:i + 2]
        rids = []
        for desc, send in pair:
            rid = run.fresh_rid()
            rids.append(rid)
            send(g, rid)
        g.hub.wait_until(lambda: all(txn_by_rid(g, r_) for r_ in rids), 3.0)
        for (desc, _s), rid in zip(pair, rids):
            res = txn_by_rid(g, rid)
            ck(f"G8 BAD_SHAPE: {desc} -> REJECTED(BAD_SHAPE)", len(res) == 1 and res[0].outcome == REJECTED and res[0].reason == R["BAD_SHAPE"])
            good_probes += 1
        if i + 2 < len(probes) - 1:
            g.disconnect()
            L.pump_sleep(0.5)
            g.reconnect_and_ready()
    ck("G8 the malformed requests changed nothing (no tile of the fixtures was touched, the host is alive)",
       host.alive() and not is_hole(tile_val(run, b, PITFALL_TILE2)) and tile_val(run, b, SHINE_TILE) == HOLE_SHINE)
    # the third malformed message of ONE connection closes the peer (the 3-violation policy shared with the record protocol)
    g.disconnect()
    L.pump_sleep(0.5)
    g.reconnect_and_ready()
    for desc, send in probes[:3]:
        send(g, run.fresh_rid())
        L.pump_sleep(0.3)
    closed = TP.wait_closed(g, 4.0)
    ck("G8 three malformed grants on one connection CLOSE the peer (violation policy), the host survives", closed and host.alive()
       and run.n_log(r"violation", off) >= 3)


def g_mirror(run, a, b, n_after_race):
    ck = run.check
    # A's whole sequence of grants is in the mirror: a NEW process of A is pushed exactly the local image
    local = tuple(a.txn_pre_image()[0])
    run.release(a)
    a2 = run.client("A3", run.r1)
    a2.connect_and_ready(quiet=True)
    p = a2.rec_pushes[-1] if a2.rec_pushes else None
    ck("G9 a NEW session of A is pushed the mirror: it holds every APPLIED grant (apple, bonus, bell [, pitfall]) and nothing else",
       p is not None and L.record_inventory(p["data"])[0] == local and local.count(ITM_APPLE) >= 1 and local.count(ITM_M100) >= 1
       and local.count(ITM_M10000) >= 1)


def phase_g(run):
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    a.drain_field_updates(timeout=0.3)
    b.drain_field_updates(timeout=0.3)
    s = g_buried_success(run, a, b)
    if s is None:
        return
    g_dups(run, a, b, s)
    g_validation(run, a, b)
    g_hole_shine(run, a, b)
    race = g_race(run, a, b)
    if race is not None:
        n, a_lost = race
        if a_lost:
            a = n   # A's resident was re-served as a new process (nonce / seq restart): keep using the live object
        else:
            b = n
    g_misc(run, a, b, s)
    g_mirror(run, a, b, None)


# ----------------------------------------------------------------------------------------------------------------------
# fault phases (DIG grants)
# ----------------------------------------------------------------------------------------------------------------------
def phase_f_ignore(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    b.drain_field_updates(timeout=0.3)
    mark_b = b.inbox.mark()
    off = len(run.log())
    rid, sent, r = grant(run, a, FA_BURIED, BURIED_TILE, wait=2.6)
    ck("F-ignore the first request is IGNORED by the host (the fault): no TXN_RESULT, no legacy result, nothing journalled, the tile untouched",
       r is None and not fa_results(a, rid) and "ignore_commit: " in run.log(off) and not is_hole(tile_val(run, b, BURIED_TILE)))
    a.resend_txn(sent)
    r = a.wait_txn_result(sent.seq, 3.0)
    run.tx_ok("F-ignore the byte-identical resend executes", r, APPLIED, R["NONE"])
    L.pump_sleep(0.4)
    ck("F-ignore executed exactly once: one applied line, one FIELD_UPDATE, the fault fired once",
       applied_lines(run, "DIG_BURIED", rid, off) == 1 and len(run.field_updates(b, BURIED_TILE, mark_b)) == 1 and run.n_log(r"ignore_commit: ", off) == 1)


def phase_f_fail(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    b.drain_field_updates(timeout=0.3)
    mark_b = b.inbox.mark()
    off = len(run.log())
    rid, sent, r = grant(run, a, FA_BURIED, BURIED_TILE)
    run.tx_ok("F-fail the grant hits the injected fail_world", r, REJECTED, R["WORLD_CHANGED"])
    fr = fa_results(a, rid)
    ck("F-fail nothing mutated: the legacy RESULT says accepted=0, the tile untouched, no FIELD_UPDATE", len(fr) == 1 and fr[0]["accepted"] == 0
       and not is_hole(tile_val(run, b, BURIED_TILE)) and len(run.field_updates(b, BURIED_TILE, mark_b)) == 0)
    a.resend_txn(sent)
    r2 = a.wait_txn_result(sent.seq, 2.5, nth=2)
    ck("F-fail the same-seq resend REPLAYS the journalled REJECTED (never re-evaluated: the fault does not fire again)",
       r2 is not None and r2.outcome == REJECTED and r2.reason == R["WORLD_CHANGED"] and run.n_log(r"FAULT INJECTION FIRED", off) == 1)
    rid_b, sent_b, rb = grant(run, b, FA_BURIED, BURIED_TILE)
    run.tx_ok("F-fail the tile is free for ANOTHER client: its grant is APPLIED", rb, APPLIED, R["NONE"])
    L.pump_sleep(0.4)
    ck("F-fail the tile changed exactly once, by B", is_hole(tile_val(run, b, BURIED_TILE)) and len(run.field_updates(b, BURIED_TILE, mark_b)) == 1)


def phase_f_drop(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    b.drain_field_updates(timeout=0.3)
    mark_b = b.inbox.mark()
    off = len(run.log())
    pre = a.txn_pre_image()
    rid, sent, r = grant(run, a, FA_BURIED, BURIED_TILE, wait=1.8)
    L.pump_sleep(0.3)
    ck("F-drop the host EXECUTED the grant but the TXN_RESULT was lost (no result at the client); the legacy RESULT (informational) still arrived",
       r is None and applied_lines(run, "DIG_BURIED", rid, off) == 1 and "drop_result: TXN_RESULT APPLIED" in run.log(off))
    a.resend_txn(sent)
    r2 = a.wait_txn_result(sent.seq, 1.8)
    ck("F-drop the first resend's replay is lost too (K=2)", r2 is None)
    a.resend_txn(sent)
    r3 = a.wait_txn_result(sent.seq, 3.0)
    ck("F-drop the third resend gets APPLIED/REPLAYED with the full post-image (the item in the slot)",
       r3 is not None and r3.outcome == APPLIED and r3.reason == R["REPLAYED"] and r3.item == ITM_APPLE and r3.post_pockets[r3.slot] == ITM_APPLE)
    L.pump_sleep(0.4)
    ck("F-drop executed exactly once (one applied line, one FIELD_UPDATE), the item exists once more in the local image",
       applied_lines(run, "DIG_BURIED", rid, off) == 1 and len(run.field_updates(b, BURIED_TILE, mark_b)) == 1
       and list(a.txn_pre_image()[0]).count(ITM_APPLE) == list(pre[0]).count(ITM_APPLE) + 1)


def phase_f_kill(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    b.drain_field_updates(timeout=0.3)
    mark_b = b.inbox.mark()
    off = len(run.log())
    pre = a.txn_pre_image()
    slot = first_free(pre[0])
    base = a.rec_last
    rid, sent, r = grant(run, a, FA_SHINE, SHINE_TILE, item=ITM_M30000, wait=0.5)
    closed = TP.wait_closed(a, 4.0)
    L.pump_sleep(0.5)
    ck("F-kill the host dropped A AFTER committing and sent NO result", closed and r is None and not txn_by_rid(a, rid)
       and "kill_peer_after_commit: peer" in run.log(off))
    ck("F-kill the world changed exactly once (observer: HOLE_SHINE via one FIELD_UPDATE)",
       tile_val(run, b, SHINE_TILE) == HOLE_SHINE and len(run.field_updates(b, SHINE_TILE, mark_b)) == 1)
    exp = tuple(ITM_M30000 if i == slot else p for i, p in enumerate(pre[0]))
    a.reconnect_and_ready(timeout=8.0)
    pushed = a.rec_pushes[-1] if a.rec_pushes else None
    ck("F-kill the same-nonce reconnect is pushed the mirror: it already HAS the bell in the slot, rev == base + 1",
       pushed is not None and pushed["rev"] == base[2] + 1 and L.record_inventory(pushed["data"])[0] == exp)
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.3)
    ck("F-kill the OLD request resent after the reconnect replays APPLIED/REPLAYED (journal survives the peer reset), no second execution",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and tuple(rep.post_pockets) == exp
       and run.n_log(r"request %d grant committed" % rid, off) == 1 and len(run.field_updates(b, SHINE_TILE, mark_b)) == 1)
    run.release(a)
    a2 = run.client("A2", run.r1)
    a2.connect_and_ready(quiet=True)
    p2 = a2.rec_pushes[-1] if a2.rec_pushes else None
    ck("F-kill a NEW-PROCESS rejoin: the pushed record has the effect, the tile stays changed exactly once",
       p2 is not None and L.record_inventory(p2["data"])[0] == exp and len(run.field_updates(b, SHINE_TILE, mark_b)) == 1)
    ck("F-kill the host survived the dropped peer", run.host.alive())


# ----------------------------------------------------------------------------------------------------------------------
# wildlife phases (catch)
# ----------------------------------------------------------------------------------------------------------------------
def seed_wildlife(c, need=3, rounds=8):
    """Burst the spawn trigger over every addressable acre, again and again (a single acre may legitimately roll 'nothing'), until
    `need` distinct entities exist or `rounds` bursts are done. Never an ant (no ordinary net-catch path)."""
    seen = {}
    for _ in range(rounds):
        for bx, bz in WG.ACRES:
            c.send_reliable(WG.build_trigger(bx, bz))
            for s in WG.collect_spawns(c, 0.15):
                seen.setdefault(s["entity_id"], s)
        for s in WG.collect_spawns(c, 0.5):
            seen.setdefault(s["entity_id"], s)
        if len(seen) >= need:
            break
    return [s for s in seen.values() if not (s["kind"] == 1 and s["species"] == 38)]


def ent_kind(sp):
    return "fish" if sp["kind"] == 0 else "bug"


def ent_item(sp):
    return L.fish_item_for_species(sp["species"]) if sp["kind"] == 0 else L.bug_item_for_species(sp["species"])


def catch(run, c, sp, gen, species=None, item=None, dest=D_POCKET, pre=None, wait=3.0, move=True, **kw):
    if move:
        c.claim_position(sp["x"], sp["y"], sp["z"])
        L.pump_sleep(0.3)
    rid = run.fresh_rid()
    sent = c.send_catch_txn(sp["entity_id"], gen, rid, sp["species"] if species is None else species,
                            item=item if item is not None else (None if dest == D_POCKET else 0), kind=ent_kind(sp), dest=dest, pre=pre, **kw)
    return rid, sent, c.wait_txn_result(sent.seq, wait)


def wildlife_setup(run, need=3, want=None):
    seed = run.ready("S", run.r3)
    seed.drain_field_updates(timeout=0.3)
    spawns = seed_wildlife(seed, want or need)
    fish = [s for s in spawns if s["kind"] == 0]
    bugs = [s for s in spawns if s["kind"] == 1]
    print(f"[X3-W] spawn burst: {len(spawns)} entities ({len(fish)} fish, {len(bugs)} bugs)")
    ents = fish + bugs
    gen = WG.get_current_generation(seed)
    run.check(f"W setup: >= {need} catchable wildlife entities spawned ({len(fish)} fish, {len(bugs)} bugs; RNG-dependent) and the generation is readable",
              len(ents) >= need and gen is not None)
    return seed, ents, gen


def phase_w(run):
    ck = run.check
    seed, ents, gen = wildlife_setup(run, 3, want=5)   # 3 are required; 5 give a chance of a bug for W7
    if len(ents) < 3 or gen is None:
        return
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    # the seed client S holds resident r3 (observer of every DESPAWN)
    seed.inbox.clear()
    e1, e2, e3 = ents[0], ents[1], ents[2]
    # ---------------- W1: success (POCKET), journal replay, ordering, observers ----------------
    mark_a, mark_s = a.inbox.mark(), seed.inbox.mark()
    pre = a.txn_pre_image()
    slot = first_free(pre[0])
    base = a.rec_last
    off = len(run.log())
    item1 = ent_item(e1)
    rid, sent, r = catch(run, a, e1, gen)
    run.tx_ok(f"W1 catch of a {ent_kind(e1)} (species {e1['species']}): the claimed item is the host-derived 0x{item1:04X}", r, APPLIED, R["NONE"])
    if r is None:
        return
    ck("W1 RESULT: kind CATCH, dest POCKET / free slot / the derived item, rev + 1, post-image has it in the slot only",
       (r.kind, r.dest, r.slot, r.item, r.rev) == (K_CATCH, D_POCKET, slot, item1, base[2] + 1)
       and tuple(r.post_pockets) == tuple(item1 if i == slot else p for i, p in enumerate(pre[0])))
    L.pump_sleep(0.5)
    cr = catch_results(a, rid, mark_a)
    ti = txn_index(a, sent.seq, mark_a)
    dsp = [m.index for m in a.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.payload and m.payload[0] == DESPAWN_TYPE, since=mark_a)]
    ck("W1 the legacy CATCH_RESULT (accepted, same item) follows the TXN_RESULT; the WILDLIFE_DESPAWN goes out BEFORE both",
       len(cr) == 1 and cr[0]["accepted"] == 1 and cr[0]["item"] == item1 and ti and dsp and dsp[0] < ti[0] < cr[0]["index"])
    ck("W1 the observer received exactly ONE WILDLIFE_DESPAWN for the entity", despawn_ids(seed, mark_s).count(e1["entity_id"]) == 1)
    ck("W1 host log: one catch-accepted line (the entity removed once) and one applied line",
       run.n_log(r"CATCH entity %d \(species claim \d+\) accepted" % e1["entity_id"], off) == 1 and applied_lines(run, "CATCH", rid, off) == 1)
    a.resend_txn(sent, copies=3)
    a.resend_txn(sent)
    got = a.wait_txn_result(sent.seq, 3.0, nth=3)
    allr = a.txn_results_for(sent.seq)
    L.pump_sleep(0.4)
    ck("W1 two byte-identical resends replay APPLIED/REPLAYED; the entity was NOT removed twice (still ONE despawn, ONE accepted line)",
       got is not None and len(allr) == 3 and [g.reason for g in allr[1:]] == [R["REPLAYED"]] * 2 and all(g.item == item1 for g in allr)
       and despawn_ids(seed, mark_s).count(e1["entity_id"]) == 1
       and run.n_log(r"CATCH entity %d \(species claim \d+\) accepted" % e1["entity_id"], off) == 1)
    # ---------------- W2: race on one entity ----------------
    mark_s = seed.inbox.mark()
    pre_b = b.txn_pre_image()
    off = len(run.log())
    a.claim_position(e2["x"], e2["y"], e2["z"])
    b.claim_position(e2["x"], e2["y"], e2["z"])
    L.pump_sleep(0.3)
    rid_a, rid_b = run.fresh_rid(), run.fresh_rid()
    sa = a.send_catch_txn(e2["entity_id"], gen, rid_a, e2["species"], kind=ent_kind(e2))
    sb = b.send_catch_txn(e2["entity_id"], gen, rid_b, e2["species"], kind=ent_kind(e2))
    ra, rb = a.wait_txn_result(sa.seq, 3.0), b.wait_txn_result(sb.seq, 3.0)
    ck("W2 race: EXACTLY ONE of the two claimants was APPLIED, the other REJECTED(WORLD_CHANGED); first-catcher-wins",
       ra is not None and rb is not None and [ra.outcome, rb.outcome].count(APPLIED) == 1 and {ra.reason, rb.reason} == {R["NONE"], R["WORLD_CHANGED"]})
    L.pump_sleep(0.4)
    ck("W2 race: one WILDLIFE_DESPAWN, one accepted line for the entity", despawn_ids(seed, mark_s).count(e2["entity_id"]) == 1
       and run.n_log(r"CATCH entity %d \(species claim \d+\) accepted" % e2["entity_id"], off) == 1)
    if ra is not None and rb is not None and rb.outcome == APPLIED:
        ra, rb = rb, ra   # keep `a` as the winner for the following sections only conceptually; nothing below depends on it
    # ---------------- W3: item mismatch / species mismatch / stale generation never remove the entity ----------------
    mark_s = seed.inbox.mark()
    rev_now = a.rec_last[2]
    rid, sent, r = catch(run, a, e3, gen, item=ent_item(e3) + 1)
    run.tx_ok("W3 a claimed item that is not the host-derived item of the species", r, REJECTED, R["PRECOND"])
    ck("W3 nothing mutated (rev unchanged) and the entity stays live (no despawn)", r is not None and r.rev == rev_now and not despawn_ids(seed, mark_s))
    wrong_species = (e3["species"] + 1) % (40 if e3["kind"] == 0 else 38)
    if e3["kind"] == 0 and (wrong_species == e3["species"] or wrong_species >= 40):
        wrong_species = 1 if e3["species"] != 1 else 2
    rid, sent, r = catch(run, a, e3, gen, species=wrong_species)
    run.tx_ok("W3 a claimed species that does not match the authoritative record", r, REJECTED, R["WORLD_CHANGED"])
    ck("W3 the entity stays live (no despawn)", not despawn_ids(seed, mark_s))
    rid, sent, r = catch(run, a, e3, (gen + 1) & 0xFFFFFFFF)
    run.tx_ok("W3 a stale / foreign generation", r, REJECTED, R["WORLD_CHANGED"])
    ck("W3 the entity stays live (no despawn)", not despawn_ids(seed, mark_s))
    ck("W3 each reject was answered by a legacy CATCH_RESULT accepted=0", all(x["accepted"] == 0 for x in catch_results(a, rid)) and catch_results(a, rid))
    # ---------------- W4: full pockets: dest NONE -> accepted, the entity removed once, an exchange CREDIT ----------------
    apples = tuple([ITM_APPLE] * 15)
    full_pre = (apples, 0, a.txn_pre_image()[2])
    off = len(run.log())
    rid, sent, r = catch(run, a, e3, gen, dest=D_NONE, pre=full_pre)
    run.tx_ok("W4 a full-pockets catch (dest NONE / slot 0xFF / item 0) of the SAME entity", r, APPLIED, R["NONE"])
    credit_item = ent_item(e3)
    ck("W4 dest NONE / slot 0xFF / item 0 echoed; the post-image == the pre-image (nothing enters the pockets)",
       r is not None and (r.dest, r.slot, r.item) == (D_NONE, S_WALLET, 0) and tuple(r.post_pockets) == apples)
    L.pump_sleep(0.4)
    ck("W4 the entity was removed once (one despawn) and the host granted the exchange credit", despawn_ids(seed, mark_s).count(e3["entity_id"]) == 1
       and "exchange credit granted" in run.log(off))
    # ---------------- W5: the exchange-menu drop (credit mismatch, success, credit consumed) ----------------
    tiles = []
    for _ in range(2):
        t = run.queue.next_tile()
        rid_p = run.fresh_rid()
        prov, sent_p, rp = b.txn_pickup(t[0], t[1], rid_p)
        if rp is not None and rp.outcome == APPLIED:
            tiles.append(t)
    ck("W5 setup: B emptied two seeded tiles (two apples picked up)", len(tiles) == 2)
    if len(tiles) < 2:
        return
    T1, T2 = tiles
    ex_slot = 4
    mark_b = b.inbox.mark()
    rev_now = a.rec_last[2]
    # wrong replacement -> PRECOND (the credit stays)
    prov, sent_d, rd = a.txn_drop(ex_slot, ITM_APPLE, T1[0], T1[1], run.fresh_rid(), claim_at=T1, flags=L.PC_NETGAME_TXN_FLAG_EXCHANGE,
                                  aux_item=credit_item + 1, aux_cond=0)
    ck("W5 the exchange DROP request got its provisional accept", prov is not None and prov.accepted)
    run.tx_ok("W5 an exchange whose replacement is NOT the credited catch item", rd, REJECTED, R["PRECOND"])
    ck("W5 ... nothing mutated: the reject released the reservation (the tile stays EMPTY, no FIELD_UPDATE)",
       rd is not None and rd.rev == rev_now and run.tile_of(b, T1) == L.EMPTY_NO and len(run.field_updates(b, T1, mark_b)) == 0)
    # right replacement -> APPLIED
    prov, sent_d, rd = a.txn_drop(ex_slot, ITM_APPLE, T1[0], T1[1], run.fresh_rid(), claim_at=T1, flags=L.PC_NETGAME_TXN_FLAG_EXCHANGE,
                                  aux_item=credit_item, aux_cond=0)
    run.tx_ok("W5 the exchange with the CREDITED replacement", rd, APPLIED, R["NONE"])
    exp = tuple(credit_item if i == ex_slot else p for i, p in enumerate(apples))
    ck("W5 post-image: the dropped slot holds the catch replacement (not EMPTY), everything else unchanged; rev + 1",
       rd is not None and tuple(rd.post_pockets) == exp and rd.dest == D_NONE and rd.slot == ex_slot and rd.rev == rev_now + 1)
    L.pump_sleep(0.5)
    ck("W5 world: the dropped apple is on the tile (one FIELD_UPDATE)", run.tile_of(b, T1) == ITM_APPLE and len(run.field_updates(b, T1, mark_b)) == 1)
    # the credit is one-shot
    prov, sent_d, rd2 = a.txn_drop(ex_slot + 1, ITM_APPLE, T2[0], T2[1], run.fresh_rid(), claim_at=T2, flags=L.PC_NETGAME_TXN_FLAG_EXCHANGE,
                                   aux_item=credit_item, aux_cond=0)
    run.tx_ok("W5 a SECOND exchange with the same replacement: the credit was consumed (one shot)", rd2, REJECTED, R["PRECOND"])
    ck("W5 the credit-less exchange mutated nothing (tile still EMPTY)", run.tile_of(b, T2) == L.EMPTY_NO)
    # ---------------- W7: a BUG entity (only when the RNG spawned one beyond the three used above) ----------------
    extra_bugs = [s for s in ents[3:] if s["kind"] == 1]
    if extra_bugs:
        eb = extra_bugs[0]
        mark_s = seed.inbox.mark()
        rev_b = b.rec_last[2]
        bug_item = L.bug_item_for_species(eb["species"])
        rid, sent, r = catch(run, b, eb, gen, item=bug_item + 1)
        run.tx_ok(f"W7 bug (species {eb['species']}): an item that is not ITM_INSECT00 + species", r, REJECTED, R["PRECOND"])
        ck("W7 ... the bug stays live (no despawn), nothing mutated", r is not None and r.rev == rev_b and not despawn_ids(seed, mark_s))
        rid, sent, r = catch(run, b, eb, gen)
        run.tx_ok(f"W7 bug catch with the host-derived item 0x{bug_item:04X}", r, APPLIED, R["NONE"])
        L.pump_sleep(0.4)
        ck("W7 kind CATCH, dest POCKET, the item in the slot, one despawn", r is not None and r.kind == K_CATCH and r.item == bug_item
           and r.post_pockets[r.slot] == bug_item and despawn_ids(seed, mark_s).count(eb["entity_id"]) == 1)
    else:
        print("[X3-W] INFO: no additional BUG entity spawned this run (RNG): the bug item derivation is NOT exercised in this run")
    # ---------------- W6: a new session sees every grant ----------------
    local = tuple(a.txn_pre_image()[0])
    run.release(a)
    a2 = run.client("A2", run.r1)
    a2.connect_and_ready(quiet=True)
    p = a2.rec_pushes[-1] if a2.rec_pushes else None
    ck("W6 a NEW session of A is pushed the mirror: the full-pockets image with the exchange replacement in the dropped slot (14 apples + the caught item)",
       p is not None and L.record_inventory(p["data"])[0] == local and local[ex_slot] == credit_item and local.count(ITM_APPLE) == 14)


def phase_w_fail(run):
    ck = run.check
    seed, ents, gen = wildlife_setup(run, 1)
    if not ents or gen is None:
        return
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    e = ents[0]
    mark_s = seed.inbox.mark()
    off = len(run.log())
    rid, sent, r = catch(run, a, e, gen)
    run.tx_ok("W-fail the first catch hits the injected fail_world", r, REJECTED, R["WORLD_CHANGED"])
    L.pump_sleep(0.3)
    ck("W-fail the entity stays LIVE: no despawn, the legacy CATCH_RESULT says accepted=0",
       not despawn_ids(seed, mark_s) and catch_results(a, rid) and catch_results(a, rid)[0]["accepted"] == 0)
    a.resend_txn(sent)
    r2 = a.wait_txn_result(sent.seq, 2.5, nth=2)
    ck("W-fail the same-seq resend replays the journalled REJECTED (the fault does not fire again)",
       r2 is not None and r2.outcome == REJECTED and run.n_log(r"FAULT INJECTION FIRED", off) == 1)
    rid_b, sent_b, rb = catch(run, b, e, gen)
    run.tx_ok("W-fail the NEXT claimant wins the still-live entity", rb, APPLIED, R["NONE"])
    L.pump_sleep(0.4)
    ck("W-fail the entity was removed exactly once (one despawn)", despawn_ids(seed, mark_s).count(e["entity_id"]) == 1)


def phase_w_kill(run):
    ck = run.check
    seed, ents, gen = wildlife_setup(run, 1)
    if not ents or gen is None:
        return
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    e = ents[0]
    mark_s = seed.inbox.mark()
    off = len(run.log())
    pre = a.txn_pre_image()
    slot = first_free(pre[0])
    base = a.rec_last
    item = ent_item(e)
    rid, sent, r = catch(run, a, e, gen, wait=0.5)
    closed = TP.wait_closed(a, 4.0)
    L.pump_sleep(0.5)
    ck("W-kill the host dropped A AFTER committing and sent NO result", closed and r is None and not txn_by_rid(a, rid)
       and "kill_peer_after_commit: peer" in run.log(off))
    ck("W-kill the entity was removed exactly once (the observer saw one despawn) and a second claimant is REJECTED",
       despawn_ids(seed, mark_s).count(e["entity_id"]) == 1)
    rid_b, sent_b, rb = catch(run, b, e, gen)
    run.tx_ok("W-kill a second claimant of the same entity is REJECTED (the entity is gone)", rb, REJECTED, R["WORLD_CHANGED"])
    exp = tuple(item if i == slot else p for i, p in enumerate(pre[0]))
    a.reconnect_and_ready(timeout=8.0)
    pushed = a.rec_pushes[-1] if a.rec_pushes else None
    ck("W-kill the same-nonce reconnect is pushed the mirror: it already HAS the caught item, rev == base + 1",
       pushed is not None and pushed["rev"] == base[2] + 1 and L.record_inventory(pushed["data"])[0] == exp)
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.3)
    ck("W-kill the OLD catch resent after the reconnect replays APPLIED/REPLAYED, no second removal",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and tuple(rep.post_pockets) == exp
       and run.n_log(r"CATCH entity %d \(species claim \d+\) accepted" % e["entity_id"], off) == 1)
    run.release(a)
    a2 = run.client("A2", run.r1)
    a2.connect_and_ready(quiet=True)
    p2 = a2.rec_pushes[-1] if a2.rec_pushes else None
    ck("W-kill a NEW-PROCESS rejoin is pushed the mirror with the caught item", p2 is not None and L.record_inventory(p2["data"])[0] == exp)


# ----------------------------------------------------------------------------------------------------------------------
PHASES = [
    ("G", [], phase_g),
    ("F-ignore", ["--txn-fault=ignore_commit:1:1"], phase_f_ignore),
    ("F-fail", ["--txn-fault=fail_world:1:1"], phase_f_fail),
    ("F-drop", ["--txn-fault=drop_result:1:2"], phase_f_drop),
    ("F-kill", ["--txn-fault=kill_peer_after_commit:1:1"], phase_f_kill),
    ("W", ["--authoritative-wildlife"], phase_w),
    ("W-fail", ["--authoritative-wildlife", "--txn-fault=fail_world:1:1"], phase_w_fail),
    ("W-kill", ["--authoritative-wildlife", "--txn-fault=kill_peer_after_commit:1:1"], phase_w_kill),
]


def cli_checks(results):
    """The X3 CLI surface: the new TEST-ONLY client hook is documented, the fault modes still are (no game boot)."""
    import subprocess
    exe = os.path.join(L.GAME_BIN_DIR, L.GAME_EXE_NAME)
    L.require_launchable_bin_dir(L.GAME_BIN_DIR)
    p = subprocess.run([exe, "--help"], cwd=L.GAME_BIN_DIR, capture_output=True, timeout=30)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    L.check("CLI --help documents --txn-test-dig-grant (a Client-only TEST hook, default off)", p.returncode == 0 and "--txn-test-dig-grant" in out
            and "Client-only TEST hook" in out, results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10800)
    ap.add_argument("--only", default="", help="comma list of phase names (G, F-ignore, ..., CLI)")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    only = [x for x in args.only.split(",") if x]
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "txn_x3_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        for i, (tag, extra, body) in enumerate(PHASES):
            if only and tag not in only:
                continue
            TP.run_phase(results, ip, args.port + i, "x3_" + tag, extra, body, snap_gci, restore)
        if not only or "CLI" in only:
            cli_checks(results)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
