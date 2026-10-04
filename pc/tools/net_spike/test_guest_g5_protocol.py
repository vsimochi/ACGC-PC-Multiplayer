#!/usr/bin/env python3
"""test_guest_g5_protocol.py - GUESTS G5.2 (gameplay on a guest record: the activities NOT yet covered for guest slots + disconnect safety), SCRIPTED clients vs a REAL host.

TIER: scripted (FakeClient guests / one resident) against REAL host processes (`AnimalCrossing.exe --host --bootstrap-resident 0 ...`, started / stopped by this script from the
TEST COPY named by NET_SPIKE_GAME_BIN = pc\\build64\\bin_fixture4; host = resident 0, ports 12000+). The fixture save dir is SNAPSHOTTED at start and RESTORED after every phase
(guests.dat lives in save/mp and disappears with the restore). The REAL game client is not exercised here: there is no UI automation, see the roadmap (G5 evidence tiers).

ALREADY COVERED for guest slots by test_guest_protocol.py section X (NOT repeated here): pickup (pocket + money bag), drop, bury (TP T1 matrix with guests as actor and observer),
DIG_BURIED grant, SHOP_BUY, SHOP_SELL, POLICE_CLAIM, and the NO_DONOR_SLOT refusals of MUSEUM_DONATE / MAIL_SEND / MAIL_TAKE (no rev change, no MAILBOX_LETTER ever pushed).

NEW here (every txn kind that a guest slot had not exercised, plus the replay / disconnect machinery on guest journals):
  M  one host (--authoritative-wildlife + the field-action / pickup test seeds); guest A, guest B, resident control R1 connected the whole time
     M1  DIG_HOLE bonus + DIG_SHINE bell grants by guest A: applied to A's record ONLY (log: `resident 4+slot`), rev + 1, world tile changed once, guest B / resident R1 get no push / result
     M2  byte-identical resends of that grant replay (REPLAYED, same post-image and rev, one execution, no second FIELD_UPDATE)
     M3  SHOP_BUY by A, then resends: replayed, the wallet is charged ONCE; A's record == the host mirror (a new session of A is pushed exactly what the client applied)
     M4  WEED_PULL and FLOWER_TRAMPLE by guest A (field actions, no grant): accepted, tile EMPTY_NO via one FIELD_UPDATE seen by B and R1, NO TXN_RESULT, A's record rev unchanged,
         same request_id replay is not a second effect
     M5  fish / bug CATCH by guest A (host-derived item, dest POCKET): applied to A only, one WILDLIFE_DESPAWN, resends replay, a race of A and B on one entity has exactly one winner
         (whose record changes), the loser is REJECTED and its record is untouched
     M6  after everything: guest B's record is still byte-for-byte its first-contact record, the resident R1 saw nothing; the residents / homes of the GCI are unchanged
  K  --txn-fault=kill_peer_after_commit:1:1  guest A's PICKUP is committed then its connection is dropped before the RESULT: reconnect (same nonce + token), the pushed record already
     HAS the effect, the byte-identical old COMMIT replays (no second item), a NEW-PROCESS rejoin with the token is pushed the same record, the host's guests.dat record equals it;
     guest B and the resident unaffected
  D  --txn-fault=drop_result:1:2  guest A's SHOP_BUY: the RESULT is lost twice, the third resend replays APPLIED/REPLAYED with the same post-image, the wallet / item exactly once
  CLI (none: no new flag)
Tier: PROTOCOL TESTED (real host binary, scripted clients). Usage: python test_guest_g5_protocol.py [--port 12000] [--only M,K,D]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

import net_spike_lib as L
import test_guest_protocol as TG
import test_shop_protocol as SH
import test_txn_protocol as TP
import test_txn_x3_protocol as X3
import test_weeds_protocol as WD
from test_txn_protocol import APPLIED, REJECTED, R, D_POCKET, first_free

SAVE_DIR_REL = "save"
KNOWN = L.PC_NETGAME_IDTOKEN_FLAG_KNOWN
KP = L.CONFIRM_KIND_PICKUP
SVC_SHOP = L.PC_NETGAME_TS_SHOP


def gi(i):
    return TG.gid(52 + i)


def join(run, i, token=None):
    c = run.guest("G%d" % i, gi(i), token=token)
    c.connect_and_ready(quiet=True)
    return c


def gslot(c):
    return c.token_msgs[-1][1].guest_slot


def applied_for(run, idx, kind, rid, off=0):
    """TXN APPLIED lines of request `rid` charged to record index `idx` (the host logs `resident <idx>`; guests are PLAYER_NUM + guest slot)."""
    return run.n_log(r"resident %d TXN APPLIED kind=%s request=%d " % (idx, kind, rid), off)


def replayed_for(run, idx, kind, rid, off=0):
    return run.n_log(r"resident %d TXN replay APPLIED\S* kind=%s request=%d " % (idx, kind, rid), off)


def read_guest_record(c_slot):
    try:
        pf = TG.parse_guests(TG.guests_path())
    except Exception:  # noqa: BLE001
        return None
    e = pf["e"][c_slot]
    return e.get("record") if e.get("present") else None


def wait_guests_dat_inventory(slot, want_inv, timeout=12.0):
    t0 = time.monotonic()
    rec = None
    while time.monotonic() - t0 < timeout:
        rec = read_guest_record(slot)
        if rec is not None and L.record_inventory(rec) == want_inv:
            return rec
        L.pump_sleep(0.5)
    return rec


# ======================================================================================================================
# phase M
# ======================================================================================================================
def phase_m(run):
    ck = run.check
    run.host.wait_for_log(r"--field-action-test-seed: flower 0x083C fixture placed at tile \(88,108\)", 30.0)
    L.pump_sleep(0.5)
    A, B = join(run, 0), join(run, 1)
    Rc = run.ready("R1", run.r1)
    for c in (A, B, Rc):
        c.drain_field_updates(timeout=0.3)
    sA, sB = gslot(A), gslot(B)
    iA, iB = 4 + sA, 4 + sB
    ck("M setup: two guests in guest slots %d / %d (record indices %d / %d) and one resident client in slot %d" % (sA, sB, iA, iB, run.r1), sA != sB and iA >= 4 and iB >= 4)
    b_first = B.rec_pushes[0]["data"] if B.rec_pushes else B.rec_local
    b_rev0, r_pushes0, b_pushes0, r_last0 = B.rec_last[2], len(Rc.rec_pushes), len(B.rec_pushes), Rc.rec_last
    r_results0 = len(Rc.txn_results)
    b_results0 = len(B.txn_results)

    # ---------------------------------------------------------------- M1 DIG_HOLE bonus + DIG_SHINE bell
    holes = []
    for name, fa_kind, kind, tile, item, hv, want_tile in (("DIG_HOLE bonus", X3.FA_HOLE, X3.K_HOLE, X3.HOLE_TILE, X3.ITM_M100, 7, None),
                                                          ("DIG_SHINE bell", X3.FA_SHINE, X3.K_SHINE, X3.SHINE_TILE, X3.ITM_M10000, 0, X3.HOLE_SHINE)):
        mark_b, mark_r = B.inbox.mark(), Rc.inbox.mark()
        pre = A.txn_pre_image()
        slot = first_free(pre[0])
        base = A.rec_last
        off = len(run.log())
        rid, sent, r = X3.grant(run, A, fa_kind, tile, item=item, hv=hv)
        run.tx_ok("M1 %s grant by a GUEST" % name, r, APPLIED, R["NONE"])
        if r is None:
            continue
        ck("M1 %s: kind %d, dest POCKET / slot %d / item 0x%04X, rev + 1, post-image == pre + the grant only" % (name, kind, slot, item),
           (r.kind, r.dest, r.slot, r.item) == (kind, D_POCKET, slot, item) and r.rev == base[2] + 1 and (r.host_session, r.epoch) == (base[0], base[1])
           and tuple(r.post_pockets) == tuple(item if i == slot else p for i, p in enumerate(pre[0])) and r.post_wallet == pre[2])
        L.pump_sleep(0.5)
        want = ((X3.HOLE_START + hv) if want_tile is None else want_tile)
        ck("M1 %s world: tile -> 0x%04X via exactly one FIELD_UPDATE at the observers (guest B and resident R1)" % (name, want),
           X3.tile_val(run, B, tile) == want and X3.tile_val(run, Rc, tile) == want and len(run.field_updates(B, tile, mark_b)) == 1 and len(run.field_updates(Rc, tile, mark_r)) == 1)
        ck("M1 %s host log: ONE applied line, charged to record index %d (the guest), none to any resident (0..3)" % (name, iA),
           applied_for(run, iA, X3.KIND_NAMES[kind], rid, off) == 1 and run.n_log(r"resident [0-3] TXN APPLIED kind=%s request=%d " % (X3.KIND_NAMES[kind], rid), off) == 0)
        holes.append((name, kind, rid, sent, r, tile, mark_b, mark_r))
    ck("M1 neither guest B nor resident R1 received a record push or a TXN_RESULT for A's grants", len(B.rec_pushes) == b_pushes0 and len(Rc.rec_pushes) == r_pushes0
       and len(B.txn_results) == b_results0 and len(Rc.txn_results) == r_results0)

    # ---------------------------------------------------------------- M2 resends replay
    for name, kind, rid, sent, r, tile, mark_b, mark_r in holes:
        off = len(run.log())
        n_ups = len(run.field_updates(B, tile, mark_b))
        cur_inv, cur_rev = A.txn_pre_image(), A.rec_last[2]   # a replay reports the host's CURRENT mirror (later grants included), with the journalled item / kind
        A.resend_txn(sent, copies=2)
        A.resend_txn(sent)
        got = A.wait_txn_result(sent.seq, 3.0, nth=3)
        allr = A.txn_results_for(sent.seq)
        L.pump_sleep(0.4)
        ck("M2 %s: two byte-identical resends (one also duplicated at the transport) replay APPLIED/REPLAYED: journalled kind / item, the host's current mirror and rev" % name,
           got is not None and len(allr) == 3 and all(g.reason == R["REPLAYED"] and g.outcome == APPLIED and g.kind == kind and g.item == r.item and tuple(g.post_pockets) == cur_inv[0]
                                                      and g.post_wallet == cur_inv[2] and g.rev == cur_rev for g in allr[1:]))
        ck("M2 %s: still ONE execution (applied count 1, replay lines >= 1, no second FIELD_UPDATE)" % name,
           applied_for(run, iA, X3.KIND_NAMES[kind], rid) == 1 and replayed_for(run, iA, X3.KIND_NAMES[kind], rid, off) >= 1
           and len(run.field_updates(B, tile, mark_b)) == n_ups)

    # ---------------------------------------------------------------- M3 SHOP_BUY + resend, record mirror
    sa = A.wait_ts_state(SVC_SHOP, 6.0, min_seq=1)
    ck("M3 the shop mirror reaches the guest", sa is not None)
    if sa is not None:
        cand = SH.listed(run, sa[1])
        ck("M3 the stock offers a buyable item", len(cand) >= 1)
        if cand:
            i1, it1, p1 = cand[0]
            s1 = SH.free_slots(A, 1)[0]
            pre = SH.pre_free(A, s1, p1 + 777)
            base = A.rec_last
            off = len(run.log())
            rid, sent, r = SH.buy(run, A, s1, it1, i1, p1, p1 + 777, pre=pre)
            run.tx_ok("M3 SHOP_BUY by a guest", r, APPLIED, R["NONE"])
            if r is not None:
                ck("M3 charged once: item in the slot, wallet == 777 (price taken from the pre-image wallet), rev + 1", r.post_pockets[s1] == it1 and r.post_wallet == 777 and r.rev == base[2] + 1)
                A.resend_txn(sent)
                A.resend_txn(sent)
                got = A.wait_txn_result(sent.seq, 3.0, nth=3)
                allr = A.txn_results_for(sent.seq)
                L.pump_sleep(0.3)
                ck("M3 two resends replay the SAME post-image (wallet 777, item once): the guest is charged ONE time",
                   got is not None and len(allr) == 3 and all(g.reason == R["REPLAYED"] and g.post_wallet == 777 and g.rev == r.rev and list(g.post_pockets).count(it1) >= 1
                                                              and tuple(g.post_pockets) == tuple(r.post_pockets) for g in allr[1:])
                   and applied_for(run, iA, "SHOP_BUY", rid, off) == 1)

    # ---------------------------------------------------------------- M4 weeds / flowers
    ck("M4 setup: the join snapshot holds the seeded weed / flower tiles", all(A.world.tile_ut(*t) == v for t, v in WD.VAL.items()))
    rev_a = A.rec_last[2]
    log0 = len(run.log())
    mark_b, mark_r = B.inbox.mark(), Rc.inbox.mark()
    r1, rid1 = WD.request(A, WD.KW, WD.W_A)
    ck("M4 WEED_PULL by a guest: accepted, kind echoed, granted_item 0", r1 is not None and r1["accepted"] == 1 and r1["kind"] == WD.KW and r1["item"] == 0)
    ck("M4 nothing is granted: NO TXN_RESULT, the guest record rev is unchanged", not WD.txn_by_rid(A, rid1) and A.rec_last[2] == rev_a)
    ck("M4 the tile becomes EMPTY_NO at guest B and resident R1 (one FIELD_UPDATE each)", WD.wait_tile(B, WD.W_A, WD.EMPTY) and WD.wait_tile(Rc, WD.W_A, WD.EMPTY)
       and len([u for u in (L.field_update_tuple(f) for f in B.field_updates_since(mark_b)) if u[:2] == WD.W_A]) == 1
       and len([u for u in (L.field_update_tuple(f) for f in Rc.field_updates_since(mark_r)) if u[:2] == WD.W_A]) == 1)
    A.send_reliable(L.build_field_action_request(WD.KW, WD.W_A[0], WD.W_A[1], rid1))
    A.hub.wait_until(lambda: len(WD.fa_results(A, rid1)) >= 2, 1.5)
    rs = WD.fa_results(A, rid1)
    ck("M4 the same request_id resent is replayed (identical accepted RESULT), still ONE host commit line", len(rs) == 2 and rs[1] == rs[0]
       and run.n_log(r"WEED_PULL at tile \(24,108\)", log0) == 1)
    r2, rid2 = WD.request(A, WD.KT, WD.F_P)
    ck("M4 FLOWER_TRAMPLE by a guest: accepted, tile EMPTY_NO at the observers, no TXN_RESULT, rev unchanged",
       r2 is not None and r2["accepted"] == 1 and r2["kind"] == WD.KT and not WD.txn_by_rid(A, rid2) and WD.wait_tile(B, WD.F_P, WD.EMPTY) and WD.wait_tile(Rc, WD.F_P, WD.EMPTY)
       and run.n_log(r"FLOWER_TRAMPLE at tile \(72,108\)", log0) == 1)
    r3, _ = WD.request(A, WD.KW, WD.W_A)
    ck("M4 a second pull of the now-empty tile by the guest is rejected (no second effect)", r3 is not None and r3["accepted"] == 0)
    ck("M4 guest B / resident R1 got no record push from A's field actions", len(B.rec_pushes) == b_pushes0 and len(Rc.rec_pushes) == r_pushes0)

    # ---------------------------------------------------------------- M5 catch
    seed, ents, gen = X3.wildlife_setup(run, 3, want=5)
    if len(ents) >= 3 and gen is not None:
        seed.inbox.clear()
        e1, e2 = ents[0], ents[1]
        mark_s = seed.inbox.mark()
        pre = A.txn_pre_image()
        slot = first_free(pre[0])
        base = A.rec_last
        off = len(run.log())
        item1 = X3.ent_item(e1)
        rid, sent, r = X3.catch(run, A, e1, gen)
        run.tx_ok("M5 catch of a %s (species %d) by a GUEST, host-derived item 0x%04X" % (X3.ent_kind(e1), e1["species"], item1), r, APPLIED, R["NONE"])
        if r is not None:
            ck("M5 RESULT: kind CATCH, dest POCKET / slot %d / the derived item, rev + 1, post-image = pre + item only" % slot,
               (r.kind, r.dest, r.slot, r.item, r.rev) == (X3.K_CATCH, D_POCKET, slot, item1, base[2] + 1)
               and tuple(r.post_pockets) == tuple(item1 if i == slot else p for i, p in enumerate(pre[0])))
            L.pump_sleep(0.5)
            ck("M5 one WILDLIFE_DESPAWN reached the observer; host log: ONE catch-accepted line and ONE applied line charged to the guest record (%d)" % iA,
               X3.despawn_ids(seed, mark_s).count(e1["entity_id"]) == 1 and run.n_log(r"CATCH entity %d \(species claim \d+\) accepted" % e1["entity_id"], off) == 1
               and applied_for(run, iA, "CATCH", rid, off) == 1)
            A.resend_txn(sent, copies=2)
            got = A.wait_txn_result(sent.seq, 3.0, nth=2)
            L.pump_sleep(0.4)
            ck("M5 the resend replays APPLIED/REPLAYED (same item), the entity is NOT removed twice", got is not None and got.reason == R["REPLAYED"] and got.item == item1
               and X3.despawn_ids(seed, mark_s).count(e1["entity_id"]) == 1 and run.n_log(r"CATCH entity %d \(species claim \d+\) accepted" % e1["entity_id"], off) == 1)
        # race of two GUESTS on one entity: exactly one winner, the loser's record is untouched
        pre_b = B.txn_pre_image()
        base_b = B.rec_last
        base_a = A.rec_last
        pre_a = A.txn_pre_image()
        A.claim_position(e2["x"], e2["y"], e2["z"])
        B.claim_position(e2["x"], e2["y"], e2["z"])
        L.pump_sleep(0.3)
        rid_a, rid_b = run.fresh_rid(), run.fresh_rid()
        sa_ = A.send_catch_txn(e2["entity_id"], gen, rid_a, e2["species"], kind=X3.ent_kind(e2))
        sb_ = B.send_catch_txn(e2["entity_id"], gen, rid_b, e2["species"], kind=X3.ent_kind(e2))
        ra, rb = A.wait_txn_result(sa_.seq, 3.0), B.wait_txn_result(sb_.seq, 3.0)
        ok = ra is not None and rb is not None and sorted([ra.outcome, rb.outcome]) == sorted([APPLIED, REJECTED])
        ck("M5 race: two guests claim ONE entity: exactly one APPLIED and one REJECTED (A: %s, B: %s)" % (None if ra is None else ra.outcome, None if rb is None else rb.outcome), ok)
        if ok:
            win, lose, wbase, lbase = (ra, rb, base_a, base_b) if ra.outcome == APPLIED else (rb, ra, base_b, base_a)
            ck("M5 race: the winner's record advanced by exactly one rev, the loser's reported rev is its OWN unchanged rev (the loser's record untouched)",
               win.rev == wbase[2] + 1 and lose.rev == lbase[2])
            ck("M5 race: one host catch-accepted line for the entity", run.n_log(r"CATCH entity %d \(species claim \d+\) accepted" % e2["entity_id"]) == 1)

    # ---------------------------------------------------------------- M6 isolation + the mirror equals what the client applied
    L.pump_sleep(0.5)
    ck("M6 resident R1 received no record push, no TXN_RESULT and its lineage point is unchanged during the whole guest activity",
       len(Rc.rec_pushes) == r_pushes0 and len(Rc.txn_results) == r_results0 and Rc.rec_last == r_last0)
    final_local, final_last = A.rec_local, A.rec_last
    tokA = bytes(A.guest_token)
    run.release(A)
    A2 = run.guest("A2", gi(0), token=tokA)
    A2.connect_and_ready(quiet=True)
    push = A2.rec_pushes[-1] if A2.rec_pushes else None
    ck("M6 a new session of guest A is pushed the host mirror: rev == the last RESULT rev, the record == what the client applied, KNOWN, same slot",
       push is not None and push["rev"] == final_last[2] and push["data"] == final_local and push["rsv"] == 1 and A2.token_msgs[-1][1].flags == KNOWN and gslot(A2) == sA)
    rec = wait_guests_dat_inventory(sA, L.record_inventory(final_local))
    ck("M6 guests.dat holds the same inventory / wallet as the pushed record (persisted mirror)", rec is not None and L.record_inventory(rec) == L.record_inventory(final_local))
    tokB = bytes(B.guest_token)
    run.release(B)
    B2 = run.guest("B2", gi(1), token=tokB)
    B2.connect_and_ready(quiet=True)
    pb = B2.rec_pushes[-1] if B2.rec_pushes else None
    ck("M6 guest B's record is byte-for-byte its first-contact record at its own unchanged rev (no activity of A touched it)",
       pb is not None and pb["data"] == b_first and pb["rev"] == b_rev0 and gslot(B2) == sB)
    ck("M6 host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())


# ======================================================================================================================
# phase K / D
# ======================================================================================================================
def phase_k(run):
    ck = run.check
    A, B = join(run, 2), join(run, 3)
    Rc = run.ready("R1", run.r1)
    sA = gslot(A)
    iA = 4 + sA
    tokA = bytes(A.guest_token)
    b_pushes0, r_pushes0 = len(B.rec_pushes), len(Rc.rec_pushes)
    base = A.rec_last
    tile, rid, prov = run.reserve(A)
    ck("K a guest reserves a ground item (PICKUP accepted)", prov is not None)
    if prov is None:
        return
    mark_b = B.inbox.mark()
    pre = A.txn_pre_image()
    slot = first_free(pre[0])
    sent = A.send_txn_commit(KP, rid, D_POCKET, slot, prov.granted_item)
    closed = TP.wait_closed(A, 4.0)
    L.pump_sleep(0.4)
    ck("K kill_peer_after_commit: the host dropped guest A after committing and sent NO result",
       closed and not A.txn_results_for(sent.seq) and "kill_peer_after_commit: peer" in run.log())
    ck("K the world changed exactly once (observer B sees the tile cleared via one FIELD_UPDATE)", run.tile_of(B, tile) == L.EMPTY_NO and len(run.field_updates(B, tile, mark_b)) == 1
       and run.n_log(r"request %d committed tile .* \[TXN\]" % rid) == 1)
    exp = tuple(prov.granted_item if i == slot else p for i, p in enumerate(pre[0]))
    L.pump_sleep(0.8)
    A.reconnect_and_ready(timeout=8.0)
    pushed = A.rec_pushes[-1] if A.rec_pushes else None
    ck("K the same-nonce reconnect (token) is the SAME guest slot %d, KNOWN, and is pushed the mirror: it already HAS the effect, rev == base + 1" % sA,
       gslot(A) == sA and A.token_msgs[-1][1].flags == KNOWN and pushed is not None and pushed["rev"] == base[2] + 1 and L.record_inventory(pushed["data"])[0] == exp)
    A.resend_txn(sent)
    rep = A.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.3)
    ck("K the OLD COMMIT resent after the reconnect replays APPLIED/REPLAYED (the guest journal survives the peer reset): same post-image, NO second item / execution",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and tuple(rep.post_pockets) == exp and list(rep.post_pockets).count(prov.granted_item) == 1
       and run.n_log(r"request %d committed tile" % rid) == 1 and len(run.field_updates(B, tile, mark_b)) == 1)
    ck("K the host log charges the replay to the guest record %d and nothing to any resident (0..3)" % iA, replayed_for(run, iA, "PICKUP", rid) >= 1
       and run.n_log(r"resident [0-3] TXN (replay )?APPLIED\S* kind=PICKUP request=%d " % rid) == 0)
    run.release(A)
    A2 = run.guest("A2", gi(2), token=tokA)
    A2.connect_and_ready(quiet=True)
    p2 = A2.rec_pushes[-1] if A2.rec_pushes else None
    ck("K a NEW-PROCESS rejoin with the token is pushed the same record (the effect, once), slot unchanged", p2 is not None and L.record_inventory(p2["data"])[0] == exp
       and p2["rev"] == base[2] + 1 and gslot(A2) == sA and run.tile_of(B, tile) == L.EMPTY_NO and len(run.field_updates(B, tile, mark_b)) == 1)
    rec = wait_guests_dat_inventory(sA, L.record_inventory(p2["data"]) if p2 else None)
    ck("K guests.dat holds exactly that inventory (the host-side record == what the client is pushed / applies)", rec is not None and p2 is not None
       and L.record_inventory(rec) == L.record_inventory(p2["data"]))
    ck("K guest B and resident R1 were not pushed anything meanwhile", len(B.rec_pushes) == b_pushes0 and len(Rc.rec_pushes) == r_pushes0)
    ck("K host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())


def phase_d(run):
    ck = run.check
    run.host.wait_for_log(r"--field-action-test-seed: flower 0x083C fixture placed at tile \(88,108\)", 30.0)
    A, B = join(run, 4), join(run, 5)
    sA = gslot(A)
    iA = 4 + sA
    sa = A.wait_ts_state(SVC_SHOP, 6.0, min_seq=1)
    ck("D the shop mirror reaches the guest", sa is not None)
    if sa is None:
        return
    cand = SH.listed(run, sa[1])
    ck("D the stock offers a buyable item", len(cand) >= 1)
    if not cand:
        return
    i1, it1, p1 = cand[0]
    s1 = SH.free_slots(A, 1)[0]
    pre = SH.pre_free(A, s1, p1 + 500)
    base = A.rec_last
    off = len(run.log())
    rid = run.fresh_rid()
    sent = A.send_txn_commit(L.PC_NETGAME_TXN_KIND_SHOP_BUY, rid, D_POCKET, s1, it1, pre=pre, aux_cond=i1, aux_item=p1)
    first = A.wait_txn_result(sent.seq, 1.6)
    L.pump_sleep(0.3)
    ck("D the APPLIED RESULT is lost (drop_result) but the host DID commit: no result at the guest, ONE applied line charged to record %d" % iA,
       first is None and applied_for(run, iA, "SHOP_BUY", rid, off) == 1)
    A.resend_txn(sent)
    ck("D resend 1: the replay is dropped too (fault count 2)", A.wait_txn_result(sent.seq, 1.2) is None)
    A.resend_txn(sent)
    res = A.wait_txn_result(sent.seq, 2.5)
    exp = tuple(it1 if i == s1 else p for i, p in enumerate(pre[0]))
    ck("D resend 2 gets APPLIED/REPLAYED with the same post-image: item once, wallet charged once (== 500), rev == base + 1",
       res is not None and res.outcome == APPLIED and res.reason == R["REPLAYED"] and tuple(res.post_pockets) == exp and res.post_wallet == 500 and res.rev == base[2] + 1)
    L.pump_sleep(0.3)
    ck("D the host APPLIED count is exactly 1 (two replays, two drop_result firings): no duplicate item / bell",
       applied_for(run, iA, "SHOP_BUY", rid) == 1 and replayed_for(run, iA, "SHOP_BUY", rid) == 2 and run.n_log(r"FAULT INJECTION FIRED mode=drop_result") == 2)
    tokA = bytes(A.guest_token)
    local = A.rec_local
    run.release(A)
    A2 = run.guest("A2", gi(4), token=tokA)
    A2.connect_and_ready(quiet=True)
    p2 = A2.rec_pushes[-1] if A2.rec_pushes else None
    ck("D after a new session the guest record on the host == what the client applied (inventory, wallet 500, rev)", p2 is not None
       and L.record_inventory(p2["data"]) == L.record_inventory(local) and p2["rev"] == res.rev and L.record_inventory(p2["data"])[2] == 500)
    ck("D host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())


PHASES = [
    ("M", ["--authoritative-wildlife"], phase_m),
    ("K", ["--txn-fault=kill_peer_after_commit:1:1"], phase_k),
    ("D", ["--txn-fault=drop_result:1:2"], phase_d),
]


def run_phase(results, ip, port, tag, extra, body, snap_gci, restore):
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    if len(residents) < 3:
        L.check("[%s] fixture has >= 3 non-host residents (%s)" % (tag, residents), False, results)
        return None
    print("=" * 72 + "\n[%s] host extra args: %s" % (tag, extra))
    host, ok = TP.start_host(ip, port, "g5" + tag, TG.HOST_EXTRA + list(extra), results)
    run = TG.GRun(ip, port, host, results, snap_gci, host_slot, residents[:3])
    try:
        if ok:
            body(run)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("[%s] phase raised %r" % (tag, exc), False, results)
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        L.check("[%s] host did not crash (exit code before stop: %s)" % (tag, rc), rc is None, results)
    log = run.log()
    TG.check_residents_unchanged(results, snap_gci, os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL), host_slot, log, require_write=False)
    restore()
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12000)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    only = [x for x in args.only.split(",") if x]
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_g5_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    L.check("the fixture starts WITHOUT a save/mp directory (no leftover guest state)", not os.path.exists(os.path.join(save_dir, "mp")), results)

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        for i, (tag, extra, body) in enumerate(PHASES):
            if only and tag not in only:
                continue
            run_phase(results, ip, args.port + i, tag, extra, body, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    L.check("the fixture ends WITHOUT a save/mp directory", not os.path.exists(os.path.join(save_dir, "mp")), results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
