#!/usr/bin/env python3
"""test_guest_museum_protocol.py - GUEST MUSEUM DONATION, SCRIPTED clients vs a REAL host.

TIER: scripted FakeClient guests / residents against REAL host processes (`AnimalCrossing.exe --host --bootstrap-resident 0 ...` from the TEST COPY named by
NET_SPIKE_GAME_BIN, which must be a pc\\build64\\bin_fixture4* directory; ports 12600+). The fixture save dir is SNAPSHOTTED at start and RESTORED after
every phase (guest state in save/mp disappears with the restore).

A guest's fish is put into its record the way the real client does it (an ordinary D3 upload of the local record: the guest "caught" it), then it
offers the fish with the normal MUSEUM_DONATE (TXN_COMMIT kind 8) built on its own local pre-image.

  M   one host; guests A, B, C and a resident observer R1 connected
      M1  A donates an undonated fish: APPLIED, post-image = the slot emptied (the guest RECORD pocket is cleared), rev + 1; the museum mirror (service 2)
          reaches A AND the resident observer with the fish nibble == 5 (DELETED_PLAYER, the guest donor value) and nothing else changed; host log names
          the guest record and donor slot 5
      M2  byte-identical resends -> APPLIED/REPLAYED, executed once (no second applied line, no new museum state); same (nonce, seq) other bytes -> CONFLICT
      M3  guest C (rejections only): the fish A already donated -> ALREADY_DONATED, an apple -> NOT_DONATABLE; C's record is BYTE-IDENTICAL afterwards
          (a new session of C is pushed exactly the record it had before, same rev) and C keeps both items
      M4  two guests racing for one species (A and B send at once): exactly one APPLIED, the other ALREADY_DONATED; the loser keeps the item
      M5  guest vs resident racing for one species (A and R1): exactly one APPLIED; nibble 5 if the guest won, R1's slot + 1 if the resident won
      M6  a resident donation still writes slot + 1 (unchanged resident behaviour)
      M7  persistence: after everybody left, the host's GCI holds the final museum bytes (nibble 5 included) and the guest store holds A's / B's records
          (the donated slots empty, the loser's fish still there)
      R   RESTART (second host process on the SAME save, no restore in between): the museum mirror at READY == the persisted blob, A's new session is
          pushed the record with the donated slots empty, the loser still has its fish (GCI and guest store agree)
  K   --txn-fault=kill_peer_after_commit:1:1: guest A's donation is committed, the connection dropped before the RESULT; the resident observer receives the
      new museum state (nibble 5); the same-nonce reconnect with the token is pushed the record WITHOUT the fish (rev + 1); the old COMMIT resent replays
      APPLIED/REPLAYED with the same post-image (one execution, no new museum state); a new process with the token gets the same record
Not reachable by a scripted client (covered by test_guest_museum_src.py): NO_DONOR_SLOT for a guest slot without a record (a bound guest always has one).
Tier: PROTOCOL TESTED (real host binary, scripted clients). Usage: python test_guest_museum_protocol.py [--port 12600] [--only M,K]
"""
import argparse
import os
import shutil
import struct
import sys
import time

import net_spike_lib as L
import test_guest_protocol as TG
import test_txn_protocol as TP
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE

SAVE_DIR_REL = "save"
K_DONATE = L.PC_NETGAME_TXN_KIND_MUSEUM_DONATE
SVC_MUSEUM = L.PC_NETGAME_TS_MUSEUM
FISH0, INSECT0 = 0x2300, 0x2D00
ITM_APPLE = 0x2800
MUS_OFF = {"fossil": 0x00, "art": 0x0D, "fish": 0x15, "insect": 0x2A}
GCI_SAVE_BASE = 0x40 + 0x26000
GCI_MUSEUM = GCI_SAVE_BASE + 0x213A8
GUEST_DONOR = 5


def nib(blob, cat, idx):
    return (blob[MUS_OFF[cat] + (idx >> 1)] >> ((idx & 1) * 4)) & 0xF


def free_idx(blob, cat, n):
    return [i for i in range(40) if nib(blob, cat, i) == 0][:n]


def only_nibbles_changed(old, new, cat_idx):
    """True when every byte of `new` equals `old` except the bytes holding the listed (category, index) nibbles."""
    allowed = {MUS_OFF[c] + (i >> 1) for c, i in cat_idx}
    return all(old[k] == new[k] or k in allowed for k in range(63))


def gi(i):
    return TG.gid(33 + i)


def gslot(c):
    return c.token_msgs[-1][1].guest_slot


def join(run, i, token=None):
    c = run.guest("G%d" % i, gi(i), token=token)
    c.connect_and_ready(quiet=True)
    return c


def give(c, items):
    """The guest 'catches' items: {slot: item} written into its local record and uploaded (an ordinary D3 upload). Returns the ack."""
    pockets, _conds, _wallet = c.txn_pre_image()
    pockets = tuple(items.get(i, p) for i, p in enumerate(pockets))
    c.rec_local = L.record_set_inventory(c.rec_local if c.rec_local is not None else c.own_record(), pockets, _conds, _wallet)
    return c.upload_local_record()


def donate(run, c, slot, item, wait=3.0, **kw):
    rid = run.fresh_rid()
    sent = c.send_txn_commit(K_DONATE, rid, D_NONE, slot, item, **kw)
    return rid, sent, c.wait_txn_result(sent.seq, wait)


def gci_museum(bin_dir):
    with open(os.path.join(bin_dir, L.SAVE_GCI_REL), "rb") as f:
        data = f.read()
    return data[GCI_MUSEUM:GCI_MUSEUM + 63]


def store_record(gslot_):
    try:
        e = TG.parse_guests(TG.guests_path())["e"][gslot_]
    except Exception:  # noqa: BLE001
        return None
    return e.get("record") if e.get("present") else None


def wait_until(fn, timeout):
    t0 = time.monotonic()
    v = fn()
    while not v and time.monotonic() - t0 < timeout:
        L.pump_sleep(0.7)
        v = fn()
    return v


def start_host(ip, port, tag, extra, results):
    host_slot = L.TEST_HOST_RESIDENT
    log_dir = os.path.dirname(os.path.abspath(__file__))
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot)] + list(extra),
                         log_path=os.path.join(log_dir, "guest_museum_%s_host.log" % tag)).start()
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=host_slot)
    L.check("[%s] host reached genuine field-ready state (extra args %s)" % (tag, list(extra)), ok, results)
    if ok:
        L.resolve_host_town(ip, port)
    return host, ok


def close_run(run, host, results, tag):
    for cl in run.clients:
        try:
            cl.close()
        except Exception:  # noqa: BLE001
            pass
    rc = host.stop()
    L.check("[%s] host did not crash (exit code before stop: %s)" % (tag, rc), rc is None, results)


def residents_untouched(results, snap_gci, gci_path, host_slot, r1):
    """No guest donation wrote a resident slot / house: the non-host residents OTHER than the donating observer R1 are byte-unchanged in the GCI, the four
    homes[].ownerID are unchanged and no test guest PersonalID appears in private_data[0..3] / homes[0..3]."""
    base = L._GCI_PRIVATE_BASE - 0x20
    homes = base + 0x9CE8
    with open(gci_path, "rb") as f:
        now = f.read()
    same = [L.record_from_gci(snap_gci, i) == L.record_from_gci(now, i) for i in range(4) if i not in (host_slot, r1)]
    L.check("G the %d non-host residents that did not donate are byte-unchanged in the GCI" % len(same), bool(same) and all(same), results)
    L.check("G the four homes[].ownerID are unchanged", all(snap_gci[homes + h * 0x26B0:homes + h * 0x26B0 + 20] == now[homes + h * 0x26B0:homes + h * 0x26B0 + 20]
                                                          for h in range(4)), results)
    region = now[base + 0x20:base + 0x20 + 4 * 0x2440] + now[homes:homes + 4 * 0x26B0]
    L.check("G no test guest PersonalID appears in private_data[0..3] / homes[0..3]", not any(L.guest_pid_be(gi(i)) in region for i in range(6)), results)


# ======================================================================================================================
# phase M (+ R restart)
# ======================================================================================================================
def phase_m(run):
    ck = run.check
    A, B, C = join(run, 0), join(run, 1), join(run, 2)
    Rc = run.ready("R1", run.r1)
    sA, sB, sC = gslot(A), gslot(B), gslot(C)
    iA = 4 + sA
    ck("M setup: three guests (guest slots %d / %d / %d) and one resident observer (slot %d)" % (sA, sB, sC, run.r1), len({sA, sB, sC}) == 3)
    st = A.wait_ts_state(SVC_MUSEUM, 6.0, min_seq=1)
    sr = Rc.wait_ts_state(SVC_MUSEUM, 6.0, min_seq=1)
    ck("M the museum mirror (service 2) reaches the guest at READY, identical to the resident's", st is not None and sr is not None and st[1] == sr[1])
    if st is None or sr is None:
        return None
    g0, blob0 = st
    f = free_idx(blob0, "fish", 5)
    ins = free_idx(blob0, "insect", 1)
    ck("M the fixture museum has >= 5 undonated fish and an undonated insect", len(f) == 5 and len(ins) == 1)
    if len(f) < 5 or not ins:
        return None
    F1, F2, F3, F4 = (FISH0 + x for x in f[:4])
    acks = [give(A, {0: F1, 1: F2, 2: F3}), give(B, {0: F2, 1: F4}), give(C, {0: F1, 1: ITM_APPLE})]
    ck("M setup: the guests' records hold their fish (D3 upload APPLIED for A, B and C)", all(a is not None and a.status == 0 for a in acks))

    # ---------------------------------------------------------------- M1
    base = A.rec_last
    pre = A.txn_pre_image()
    mark_r = Rc.inbox.mark()
    off = len(run.log())
    rid, sent, r = donate(run, A, 0, F1)
    run.tx_ok("M1 a GUEST donates an undonated fish", r, APPLIED, R["NONE"])
    if r is None or r.outcome != APPLIED:
        return None
    ck("M1 post-image: the donated pocket slot is EMPTY (the guest record pocket is cleared), every other pocket / conds / wallet unchanged, rev == base + 1",
       tuple(r.post_pockets) == tuple(0 if i == 0 else p for i, p in enumerate(pre[0])) and r.post_conds == pre[1] and r.post_wallet == pre[2]
       and r.rev == base[2] + 1 and L.record_inventory(A.rec_local)[0][0] == 0)
    sa = A.wait_ts_state(SVC_MUSEUM, 3.0, min_seq=g0.seq + 1)
    sr = Rc.wait_ts_state(SVC_MUSEUM, 3.0, min_seq=g0.seq + 1)
    ck("M1 the museum mirror reaches the guest AND the resident observer (seq + 1, identical blobs)", sa is not None and sr is not None and sa[0].seq == g0.seq + 1 and sa[1] == sr[1])
    if sa is None:
        return None
    blob1 = sa[1]
    ck("M1 the fish nibble is 5 (the guest donor value, DELETED_PLAYER: no new save value) and no other nibble changed",
       nib(blob1, "fish", f[0]) == GUEST_DONOR and only_nibbles_changed(blob0, blob1, [("fish", f[0])]))
    ck("M1 svc_seq16 of the RESULT == the museum seq the commit produced", r.svc_seq16 == (sa[0].seq & 0xFFFF))
    ck("M1 host log: one APPLIED line for the guest record %d and one commit line 'guest record %d ... (donor slot 5)'" % (iA, iA),
       run.n_log(r"resident %d TXN APPLIED kind=MUSEUM_DONATE request=%d " % (iA, rid), off) == 1
       and run.n_log(r"guest record %d MUSEUM_DONATE item=0x%04X slot=0 committed \(donor slot 5\) \[TXN\]" % (iA, F1), off) == 1)
    ck("M1 the resident observer got no TXN_RESULT and no record push from the guest's donation", not Rc.txn_results and len(Rc.rec_pushes) <= 1)

    # ---------------------------------------------------------------- M2 replay / conflict
    n_ts, off2 = len(A.ts_states_of(SVC_MUSEUM)), len(run.log())
    A.resend_txn(sent)
    A.resend_txn(sent)
    got = A.wait_txn_result(sent.seq, 3.0, nth=3)
    allr = A.txn_results_for(sent.seq)
    ck("M2 two byte-identical resends -> APPLIED/REPLAYED twice, same post-image and svc_seq16",
       got is not None and len(allr) == 3 and [g.reason for g in allr[1:]] == [R["REPLAYED"]] * 2
       and all(g.outcome == APPLIED and tuple(g.post_pockets) == tuple(r.post_pockets) and g.svc_seq16 == r.svc_seq16 for g in allr))
    L.pump_sleep(0.8)
    ck("M2 executed ONCE: no new applied line, no new museum state", run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE request=%d " % rid, off2) == 0
       and len(A.ts_states_of(SVC_MUSEUM)) == n_ts and run.n_log(r"service 2 \(MUSEUM\) state ->", off2) == 0)
    bad = bytearray(sent.raw)
    bad[18] ^= 1
    A.send_txn_commit(K_DONATE, rid, D_NONE, 0, F1, raw=bytes(bad))
    rc = A.wait_txn_result(sent.seq, 2.5, nth=4)
    ck("M2 same (nonce, seq) with DIFFERENT bytes -> REJECTED(CONFLICT), never executed", rc is not None and rc.outcome == REJECTED and rc.reason == R["CONFLICT"])

    # ---------------------------------------------------------------- M3 rejections leave C's record byte-identical
    tokC = bytes(C.guest_token)
    run.release(C)
    C1 = join(run, 2, token=tokC)
    c_before = C1.rec_pushes[-1] if C1.rec_pushes else None
    ck("M3 setup: C's new session is pushed its record (the fish and the apple in slots 0 / 1)", c_before is not None
       and L.record_inventory(c_before["data"])[0][:2] == (F1, ITM_APPLE))
    off3, n_ts3 = len(run.log()), len(Rc.ts_states_of(SVC_MUSEUM))
    _r, _s, ra = donate(run, C1, 0, F1)
    run.tx_ok("M3 guest C offers the fish guest A already donated", ra, REJECTED, R["ALREADY_DONATED"])
    _r, _s, rn = donate(run, C1, 1, ITM_APPLE)
    run.tx_ok("M3 guest C offers an apple", rn, REJECTED, R["NOT_DONATABLE"])
    L.pump_sleep(0.8)
    ck("M3 nothing changed: no applied line, no museum state, C's rev unchanged", run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE", off3) == 0
       and len(Rc.ts_states_of(SVC_MUSEUM)) == n_ts3 and c_before is not None and ra is not None and rn is not None
       and ra.rev == c_before["rev"] and rn.rev == c_before["rev"])
    run.release(C1)
    C2 = join(run, 2, token=tokC)
    c_after = C2.rec_pushes[-1] if C2.rec_pushes else None
    ck("M3 C's host record is BYTE-IDENTICAL after both rejections (same bytes, same rev): C keeps the fish and the apple",
       c_after is not None and c_before is not None and c_after["data"] == c_before["data"] and c_after["rev"] == c_before["rev"])
    run.release(C2)

    # ---------------------------------------------------------------- M4 guest vs guest race (F2: A slot 1, B slot 0)
    off4 = len(run.log())
    pa, pb = A.txn_pre_image(), B.txn_pre_image()
    rid_a, rid_b = run.fresh_rid(), run.fresh_rid()
    s_a = A.send_txn_commit(K_DONATE, rid_a, D_NONE, 1, F2)
    s_b = B.send_txn_commit(K_DONATE, rid_b, D_NONE, 0, F2)
    ra4, rb4 = A.wait_txn_result(s_a.seq, 3.0), B.wait_txn_result(s_b.seq, 3.0)
    ck("M4 two guests race for one fish: exactly ONE APPLIED and the other REJECTED(ALREADY_DONATED)", ra4 is not None and rb4 is not None
       and sorted([ra4.outcome, rb4.outcome]) == [APPLIED, REJECTED] and [x.reason for x in (ra4, rb4) if x.outcome == REJECTED] == [R["ALREADY_DONATED"]])
    a_won4 = ra4 is not None and ra4.outcome == APPLIED
    L.pump_sleep(0.8)
    cur = Rc.ts_latest(SVC_MUSEUM)[1]
    ck("M4 the fish is credited once with the guest value 5; one applied line", nib(cur, "fish", f[1]) == GUEST_DONOR
       and run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE request=(%d|%d) " % (rid_a, rid_b), off4) == 1)
    loser = (B, rb4, pb, 0) if a_won4 else (A, ra4, pa, 1)
    ck("M4 the loser keeps the item: no post-image, its local record still holds the fish in slot %d" % loser[3],
       loser[1] is not None and all(p == 0 for p in loser[1].post_pockets) and loser[0].txn_pre_image()[0][loser[3]] == F2)

    # ---------------------------------------------------------------- M5 guest vs resident race (F3: A slot 2, R1 default pre-image slot 3)
    off5 = len(run.log())
    rid_g, rid_r = run.fresh_rid(), run.fresh_rid()
    s_g = A.send_txn_commit(K_DONATE, rid_g, D_NONE, 2, F3)
    s_r = Rc.send_txn_commit(K_DONATE, rid_r, D_NONE, 3, F3)
    rg5, rr5 = A.wait_txn_result(s_g.seq, 3.0), Rc.wait_txn_result(s_r.seq, 3.0)
    ck("M5 a guest and a resident race for one fish: exactly ONE APPLIED and the other REJECTED(ALREADY_DONATED)", rg5 is not None and rr5 is not None
       and sorted([rg5.outcome, rr5.outcome]) == [APPLIED, REJECTED] and [x.reason for x in (rg5, rr5) if x.outcome == REJECTED] == [R["ALREADY_DONATED"]])
    g_won5 = rg5 is not None and rg5.outcome == APPLIED
    L.pump_sleep(0.8)
    cur = Rc.ts_latest(SVC_MUSEUM)[1]
    ck("M5 the winner's donor value is in the museum (%s)" % ("guest -> 5" if g_won5 else "resident -> slot + 1"),
       nib(cur, "fish", f[2]) == (GUEST_DONOR if g_won5 else run.r1 + 1) and run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE request=(%d|%d) " % (rid_g, rid_r), off5) == 1)

    # ---------------------------------------------------------------- M6 resident unchanged
    _r, _s, ri = donate(run, Rc, 4, INSECT0 + ins[0])
    run.tx_ok("M6 the resident donates an insect", ri, APPLIED, R["NONE"])
    L.pump_sleep(0.6)
    final = Rc.ts_latest(SVC_MUSEUM)[1]
    ck("M6 a resident donation still writes the resident's slot + 1 (%d)" % (run.r1 + 1), nib(final, "insect", ins[0]) == run.r1 + 1)
    sa_last = A.ts_latest(SVC_MUSEUM)
    ck("M6 the guest's mirror equals the resident's (every update reached both)", sa_last is not None and sa_last[1] == final)

    # ---------------------------------------------------------------- M7 persistence
    exp_a = L.record_inventory(A.rec_local)
    exp_b = L.record_inventory(B.rec_local)
    tokA, tokB = bytes(A.guest_token), bytes(B.guest_token)
    for c in (A, B, Rc):
        run.release(c)
    ok_gci = wait_until(lambda: gci_museum(L.GAME_BIN_DIR) == final, 30.0)
    ck("M7 the host's GCI save (the early save after the players left) holds the final museum bytes, guest nibbles (5) included", ok_gci)
    ra_ = wait_until(lambda: (lambda x: x is not None and L.record_inventory(x) == exp_a)(store_record(sA)), 15.0)
    rb_ = wait_until(lambda: (lambda x: x is not None and L.record_inventory(x) == exp_b)(store_record(sB)), 15.0)
    ck("M7 the guest store holds A's and B's records exactly as the clients applied them (donated slots empty, the race loser still has its fish)", ra_ and rb_)
    ck("M7 host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())
    return dict(final=final, exp_a=exp_a, exp_b=exp_b, tokA=tokA, tokB=tokB, sA=sA, sB=sB, f=f, a_won4=a_won4, F2=F2)


def phase_r(run, info):
    ck = run.check
    A = join(run, 0, token=info["tokA"])
    B = join(run, 1, token=info["tokB"])
    st = A.wait_ts_state(SVC_MUSEUM, 6.0, min_seq=1)
    ck("R after a host RESTART the museum mirror at READY == the persisted blob (the guest donations survived, nibble 5)", st is not None and st[1] == info["final"])
    pa = A.rec_pushes[-1] if A.rec_pushes else None
    pb = B.rec_pushes[-1] if B.rec_pushes else None
    ck("R guest A's new session (same slot) is pushed the record with the donated slots empty: GCI and guest store agree",
       pa is not None and gslot(A) == info["sA"] and L.record_inventory(pa["data"]) == info["exp_a"])
    ck("R guest B's record is the persisted one too (the M4 loser keeps its fish; the winner's slot is empty)",
       pb is not None and gslot(B) == info["sB"] and L.record_inventory(pb["data"]) == info["exp_b"]
       and (info["F2"] in L.record_inventory(pb["data"])[0]) == bool(info["a_won4"]))
    ck("R no fish was both kept by a guest and credited to the museum by that guest (no duplication)",
       all(not (nib(info["final"], "fish", i) == GUEST_DONOR and (FISH0 + i) in L.record_inventory(pa["data"])[0]) for i in info["f"][:1]) if pa else False)


# ======================================================================================================================
# phase K
# ======================================================================================================================
def phase_k(run):
    ck = run.check
    A = join(run, 5)
    Rc = run.ready("R1", run.r1)
    sA = gslot(A)
    iA = 4 + sA
    tokA = bytes(A.guest_token)
    st = A.wait_ts_state(SVC_MUSEUM, 6.0, min_seq=1)
    sr = Rc.wait_ts_state(SVC_MUSEUM, 6.0, min_seq=1)
    if st is None or sr is None:
        ck("K museum mirror at READY", False)
        return
    g0, blob0 = st
    fi = free_idx(blob0, "fish", 1)[0]
    F = FISH0 + fi
    ack = give(A, {0: F})
    ck("K setup: guest A's record holds the fish (upload APPLIED)", ack is not None and ack.status == 0)
    base = A.rec_last
    pre = A.txn_pre_image()
    off = len(run.log())
    rid, sent, first = donate(run, A, 0, F, wait=1.5)
    closed = TP.wait_closed(A, 4.0)
    ck("K kill_peer_after_commit: the host committed the guest's donation and dropped it WITHOUT a result", first is None and closed
       and "kill_peer_after_commit: peer" in run.log(off) and run.n_log(r"guest record %d MUSEUM_DONATE item=0x%04X slot=0 committed \(donor slot 5\)" % (iA, F), off) == 1
       and run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE request=%d " % rid, off) == 0)
    sr1 = Rc.wait_ts_state(SVC_MUSEUM, 3.0, min_seq=sr[0].seq + 1)
    ck("K the resident observer still receives the new museum state with the guest value 5", sr1 is not None and nib(sr1[1], "fish", fi) == GUEST_DONOR)
    exp = tuple(0 if i == 0 else p for i, p in enumerate(pre[0]))
    L.pump_sleep(0.8)
    A.reconnect_and_ready(timeout=8.0)
    pushed = A.rec_pushes[-1] if A.rec_pushes else None
    ck("K the same-nonce reconnect (token) is the same guest slot and is pushed the record WITHOUT the fish, rev == base + 1",
       gslot(A) == sA and pushed is not None and L.record_inventory(pushed["data"])[0] == exp and pushed["rev"] == base[2] + 1)
    n_ts = len(Rc.ts_states_of(SVC_MUSEUM))
    off2 = len(run.log())
    A.resend_txn(sent)
    rep = A.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.6)
    ck("K the OLD COMMIT resent after the reconnect replays APPLIED/REPLAYED with the same post-image: one execution, no new museum state",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and tuple(rep.post_pockets) == exp
       and run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE request=%d " % rid, off2) == 0 and len(Rc.ts_states_of(SVC_MUSEUM)) == n_ts
       and run.n_log(r"MUSEUM_DONATE item=0x%04X slot=0 committed" % F) == 1)
    run.release(A)
    A2 = join(run, 5, token=tokA)
    p2 = A2.rec_pushes[-1] if A2.rec_pushes else None
    st2 = A2.wait_ts_state(SVC_MUSEUM, 4.0, min_seq=1)
    ck("K a NEW process with the token is pushed the same record (fish gone once) and the museum holds the fish once with value 5",
       p2 is not None and L.record_inventory(p2["data"])[0] == exp and st2 is not None and nib(st2[1], "fish", fi) == GUEST_DONOR)
    ck("K host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())


# ======================================================================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12600)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on a pc\\build64\\bin_fixture4* copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    only = [x for x in args.only.split(",") if x]
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_museum_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    L.check("the fixture starts WITHOUT a save/mp directory (no leftover guest state)", not os.path.exists(os.path.join(save_dir, "mp")), results)
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        if len(residents) < 3:
            L.check("fixture has >= 3 non-host residents (%s)" % residents, False, results)
        if (not only or "M" in only) and len(residents) >= 3:
            print("=" * 72 + "\n[M] main + persistence")
            host, ok = start_host(ip, args.port, "M", [], results)
            run = TG.GRun(ip, args.port, host, results, snap_gci, host_slot, residents[:3])
            info = None
            try:
                if ok:
                    info = phase_m(run)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                L.check("[M] phase raised %r" % exc, False, results)
            finally:
                close_run(run, host, results, "M")
            residents_untouched(results, snap_gci, gci_path, host_slot, residents[0])
            if info is not None:
                print("=" * 72 + "\n[R] restart on the same save (no restore)")
                host, ok = start_host(ip, args.port + 1, "R", [], results)
                run = TG.GRun(ip, args.port + 1, host, results, snap_gci, host_slot, residents[:3])
                try:
                    if ok:
                        phase_r(run, info)
                except Exception as exc:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    L.check("[R] phase raised %r" % exc, False, results)
                finally:
                    close_run(run, host, results, "R")
            restore()
        if (not only or "K" in only) and len(residents) >= 3:
            print("=" * 72 + "\n[K] kill_peer_after_commit")
            host, ok = start_host(ip, args.port + 2, "K", ["--txn-fault=kill_peer_after_commit:1:1"], results)
            run = TG.GRun(ip, args.port + 2, host, results, snap_gci, host_slot, residents[:3])
            try:
                if ok:
                    phase_k(run)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                L.check("[K] phase raised %r" % exc, False, results)
            finally:
                close_run(run, host, results, "K")
            restore()
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    L.check("the fixture ends WITHOUT a save/mp directory", not os.path.exists(os.path.join(save_dir, "mp")), results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
