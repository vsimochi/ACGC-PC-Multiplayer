#!/usr/bin/env python3
"""test_ts_protocol.py - Town Services milestone 1, HOST half: the generic host -> client service MIRROR (TOWN_SVC_STATE, id 55) and the
host-authoritative MUSEUM_DONATE (TXN_COMMIT kind 8) / POLICE_CLAIM (kind 9) transactions.

PROTOCOL test against REAL host processes (`AnimalCrossing.exe --host --bootstrap-resident 0 --ts-test-seed-police=0x2800,0x2801,0x2802
[--txn-fault=...]`, started and stopped by this script from the TEST COPY named by NET_SPIKE_GAME_BIN = pc\\build64\\bin_fixture4; host = resident
0, clients = residents 1..3, at most 3 simultaneous clients, ports 11000+). `require_test_bin_dir()` refuses the live dirs; the fixture save dir
is SNAPSHOTTED at start and RESTORED after every host phase. Clients are scripted FakeClients (net_spike_lib: TXN_COMMIT builders, TXN_RESULT /
TOWN_SVC_STATE parsing); the host's C code runs for real. The real game CLIENT half is covered by test_ts_real_client.py (one donation + one
claim) and the source audit test_ts_src.py (stale / duplicate seq handling and the other client-side rules cannot be driven by a scripted client:
the scripted client is the sender, the real client's receive path is the code under test there).

Phases (one host process each):
  M        mirror delivery at READY (police + museum blobs == the host's logged digests, identical for two clients, exactly one push per service,
           no spam), museum donation (fish + insect: record, donor nibble = BOUND slot + 1, pocket removed in the post-image, mirror to a second
           client, svc_seq16 echo, RESULT before the mirror), duplicate / replay / CONFLICT, already-donated rejection that keeps the item,
           concurrent donation of the same species (exactly one winner), invalid / malformed / stale / bad-image requests, police claim
           (success, replay, compaction, mismatch, stale index after compaction, concurrent claim), HANDSHAKE / parked / host-own peers,
           persistence in the host GCI (early save after the clients leave)
  F-drop   drop_result:1:1   the APPLIED RESULT of a donation is lost but the mirror still arrives; the resend replays, executed once
  F-kill   kill_peer_after_commit:1:1   a claim commits, the peer is dropped without a RESULT; the other client sees the new police state, a
           same-nonce reconnect replays, a NEW-PROCESS session sees the post-state (pushed record + mirror)

Tier: PROTOCOL TESTED (real host binary, scripted clients). Usage: python test_ts_protocol.py [--port 11000] [--only M,F-drop,F-kill]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

import net_spike_lib as L
import test_txn_protocol as TP
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE, D_POCKET, first_free

K_DONATE, K_CLAIM = L.PC_NETGAME_TXN_KIND_MUSEUM_DONATE, L.PC_NETGAME_TXN_KIND_POLICE_CLAIM
SVC_POLICE, SVC_MUSEUM = L.PC_NETGAME_TS_POLICE, L.PC_NETGAME_TS_MUSEUM
SEEDS = [0x2800, 0x2801, 0x2802]          # --ts-test-seed-police (ITEM1 items; mPB_keep_item appends them)
FISH0, INSECT0 = 0x2300, 0x2D00
MUS_OFF = {"fossil": 0x00, "art": 0x0D, "fish": 0x15, "insect": 0x2A}
GCI_SAVE_BASE = 0x40 + 0x26000            # Save_t within the GCI file (private_data is at +0x20)
GCI_POLICE, GCI_MUSEUM = GCI_SAVE_BASE + 0x20ED0, GCI_SAVE_BASE + 0x213A8


# ----------------------------------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------------------------------
def pol_items(blob):
    return list(struct.unpack("<20H", blob))


def compact(items):
    nz = [x for x in items if x]
    return nz + [0] * (20 - len(nz))


def nib(blob, cat, idx):
    return (blob[MUS_OFF[cat] + (idx >> 1)] >> ((idx & 1) * 4)) & 0xF


def free_idx(blob, cat, n):
    out = [i for i in range(40 if cat == "fish" else 40) if nib(blob, cat, i) == 0]
    return out[:n]


def host_digests(run, svc, name):
    """{seq: digest} of every '[NET][TS] host: service N (NAME) state -> seq S digest 0xD len L' log line."""
    return {int(m.group(1)): int(m.group(2), 16) for m in re.finditer(r"service %d \(%s\) state -> seq (\d+) digest 0x([0-9A-F]{8}) len \d+" % (svc, name), run.log())}


def ts_count(c, svc):
    return len(c.ts_states_of(svc))


def ts_indices(c, svc, since=None):
    return [m.index for m in c.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_TOWN_SVC_STATE, (L.CH_RELIABLE,)), since=since)
            if m.game is not None and m.game.service == svc]


def txn_idx(c, seq, since=None):
    return [m.index for m in c.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_TXN_RESULT, (L.CH_RELIABLE,)), since=since)
            if m.game is not None and m.game.txn_seq == seq]


def gci_state(bin_dir):
    with open(os.path.join(bin_dir, L.SAVE_GCI_REL), "rb") as f:
        data = f.read()
    mus = data[GCI_MUSEUM:GCI_MUSEUM + 63]
    pol = list(struct.unpack(">20H", data[GCI_POLICE:GCI_POLICE + 40]))
    return mus, pol


def start_host(ip, port, tag, extra, results):
    host_slot = L.TEST_HOST_RESIDENT
    log_dir = os.path.dirname(os.path.abspath(__file__))
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot), "--ts-test-seed-police=" + ",".join("0x%04X" % s for s in SEEDS)] + list(extra),
                         log_path=os.path.join(log_dir, f"ts_protocol_{tag}_host.log")).start()
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=host_slot)
    L.check(f"[{tag}] host reached genuine field-ready state (extra args {list(extra)})", ok, results)
    if ok:
        L.resolve_host_town(ip, port)
    return host, ok


def run_phase(results, ip, port, tag, extra, body, snap_gci, restore):
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    if len(residents) < 3:
        L.check(f"[{tag}] fixture has >= 3 non-host residents ({residents})", False, results)
        return
    print("=" * 72 + f"\n[{tag}] host extra args: {extra}")
    host, ok = start_host(ip, port, tag, extra, results)
    run = TP.Run(ip, port, host, results, snap_gci, host_slot, residents[:3])
    try:
        if ok:
            body(run)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check(f"[{tag}] phase raised {exc!r}", False, results)
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        L.check(f"[{tag}] host did not crash (exit code before stop: {rc})", rc is None, results)
        restore()


def donate(run, c, slot, item, wait=3.0, **kw):
    rid = run.fresh_rid()
    sent = c.send_txn_commit(K_DONATE, rid, D_NONE, slot, item, **kw)
    return rid, sent, c.wait_txn_result(sent.seq, wait)


def claim(run, c, slot, item, pidx, wait=3.0, **kw):
    rid = run.fresh_rid()
    pre = kw.pop("pre", None)
    if pre is None:
        pockets, conds, wallet = c.txn_pre_image()
        pre = (tuple(0 if i == slot else p for i, p in enumerate(pockets)), conds, wallet)
    sent = c.send_txn_commit(K_CLAIM, rid, D_POCKET, slot, item, pre=pre, aux_cond=pidx, **kw)
    return rid, sent, c.wait_txn_result(sent.seq, wait)


def latest(c, svc, min_seq=1, timeout=3.0):
    st = c.wait_ts_state(svc, timeout, min_seq=min_seq)
    return st


# ----------------------------------------------------------------------------------------------------------------------
# Phase M
# ----------------------------------------------------------------------------------------------------------------------
def m_mirror_at_ready(run, a, b):
    ck = run.check
    sa = {s: latest(a, s, 1, 5.0) for s in (SVC_POLICE, SVC_MUSEUM)}
    sb = {s: latest(b, s, 1, 5.0) for s in (SVC_POLICE, SVC_MUSEUM)}
    ck("TS1 both clients received BOTH services right after READY (police + museum)", all(v is not None for v in list(sa.values()) + list(sb.values())))
    if not all(v is not None for v in list(sa.values()) + list(sb.values())):
        return None
    for s, nm, ln in ((SVC_POLICE, "POLICE", L.PC_NETGAME_TS_POLICE_LEN), (SVC_MUSEUM, "MUSEUM", L.PC_NETGAME_TS_MUSEUM_LEN)):
        g, blob = sa[s]
        ck(f"TS1 {nm}: len {ln}, seq >= 1, digest == FNV-1a32 of the blob, and == the digest the HOST logged for that seq (blob == host state)",
           g.len == ln and len(blob) == ln and g.seq >= 1 and g.digest == L.fnv1a32(blob) and host_digests(run, s, nm).get(g.seq) == g.digest)
        ck(f"TS1 {nm}: both clients hold the IDENTICAL blob and seq", sa[s][1] == sb[s][1] and sa[s][0].seq == sb[s][0].seq)
    pol = pol_items(sa[SVC_POLICE][1])
    nz = [x for x in pol if x]
    ck("TS1 POLICE blob: the three --ts-test-seed-police items are the last non-empty entries, in order, via the vanilla mPB_keep_item",
       nz[-3:] == SEEDS and all((x >> 12) in (1, 2, 3) or x == 0 for x in pol))  # FTR0 / ITEM1 / FTR1 name types: the only things mPB_keep_item stores
    L.pump_sleep(2.5)
    ck("TS2 no spam: exactly ONE TOWN_SVC_STATE per service per client after 2.5 s idle (the digest poll sends only on a change)",
       ts_count(a, SVC_POLICE) == 1 and ts_count(a, SVC_MUSEUM) == 1 and ts_count(b, SVC_POLICE) == 1 and ts_count(b, SVC_MUSEUM) == 1)
    ck("TS2 the shop (3) is mirrored since the shop milestone (exactly ONE push per client, 320 B, digest == FNV-1a32), HOST_CONFIG (4, batch A) is pushed exactly "
       "once per client (len 8, host WITHOUT --authoritative-wildlife -> blob[0] == 0, reserved bytes zero, digest == FNV-1a32) and no reserved service (5+) is ever sent",
       all(ts_count(c, L.PC_NETGAME_TS_SHOP) == 1 and c.ts_latest(L.PC_NETGAME_TS_SHOP)[0].len == L.PC_NETGAME_TS_SHOP_LEN for c in (a, b))
       and all(ts_count(c, L.PC_NETGAME_TS_HOSTCFG) == 1 and c.ts_latest(L.PC_NETGAME_TS_HOSTCFG)[0].len == L.PC_NETGAME_TS_HOSTCFG_LEN
               and c.ts_latest(L.PC_NETGAME_TS_HOSTCFG)[1] == bytes(8) and c.ts_latest(L.PC_NETGAME_TS_HOSTCFG)[0].digest == L.fnv1a32(bytes(8)) for c in (a, b))
       and not [1 for c in (a, b) for conn, g, bl in c.ts_states if g.service not in (SVC_POLICE, SVC_MUSEUM, L.PC_NETGAME_TS_SHOP, L.PC_NETGAME_TS_HOSTCFG)])
    ck("TS3 batch A: HOST_CONFIG reaches each client BEFORE the first world SNAPSHOT_BEGIN (the wildlife mode is known before any wildlife snapshot message)",
       all(ts_indices(c, L.PC_NETGAME_TS_HOSTCFG) and c.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_SNAPSHOT_BEGIN, (L.CH_RELIABLE,)))
           and ts_indices(c, L.PC_NETGAME_TS_HOSTCFG)[0] < c.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_SNAPSHOT_BEGIN, (L.CH_RELIABLE,)))[0].index for c in (a, b)))
    return sa[SVC_POLICE], sa[SVC_MUSEUM]


def m_museum(run, a, b, mus0):
    ck = run.check
    g0, blob0 = mus0
    f1, f2, f3 = free_idx(blob0, "fish", 3)
    ins = free_idx(blob0, "insect", 1)[0]
    item1, item2, item3, bug = FISH0 + f1, FISH0 + f2, FISH0 + f3, INSECT0 + ins
    donor_a, donor_b = run.r1 + 1, run.r2 + 1
    base = a.rec_last
    mark_a, mark_b = a.inbox.mark(), b.inbox.mark()
    pre = a.txn_pre_image()
    off = len(run.log())
    rid, sent, r = donate(run, a, 3, item1)
    run.tx_ok("D1 MUSEUM_DONATE of an undonated fish", r, APPLIED, R["NONE"])
    if r is None:
        return None
    ck("D1 RESULT echoes kind 8 / request / nonce / seq, dest NONE, the slot and the item; lineage rev == base + 1",
       (r.kind, r.request_id, r.txn_nonce, r.txn_seq, r.dest, r.slot, r.item) == (K_DONATE, rid, sent.nonce, sent.seq, D_NONE, 3, item1)
       and (r.host_session, r.epoch, r.rev) == (base[0], base[1], base[2] + 1))
    exp_p = tuple(0 if i == 3 else (item1 if i == 3 else p) for i, p in enumerate(pre[0]))
    ck("D1 post-image: the donated slot is EMPTY (item removed from the mirror), every other pocket / conds / wallet unchanged",
       tuple(r.post_pockets) == exp_p and r.post_conds == pre[1] and r.post_wallet == pre[2] and r.cdig == TP.cown_digest(a.rec_local))
    stA = a.wait_ts_state(SVC_MUSEUM, 3.0, min_seq=g0.seq + 1)
    stB = b.wait_ts_state(SVC_MUSEUM, 3.0, min_seq=g0.seq + 1)
    ck("D1 mirror: BOTH clients receive museum seq + 1 after the commit", stA is not None and stB is not None and stA[0].seq == g0.seq + 1 and stA[1] == stB[1])
    if stA is None:
        return None
    blob1 = stA[1]
    ck("D1 the donor nibble of the fish is the BOUND resident's slot + 1 (%d); every other nibble of the museum is unchanged" % donor_a,
       nib(blob1, "fish", f1) == donor_a and all(blob1[i] == blob0[i] or (i == MUS_OFF["fish"] + (f1 >> 1)) for i in range(63)))
    ck("D1 svc_seq16 of the RESULT == the low 16 bits of the museum seq the commit produced, and the digest matches the host log",
       r.svc_seq16 == (stA[0].seq & 0xFFFF) and host_digests(run, SVC_MUSEUM, "MUSEUM").get(stA[0].seq) == stA[0].digest)
    ti = txn_idx(a, sent.seq, mark_a)
    ts = ts_indices(a, SVC_MUSEUM, mark_a)
    ck("D1 ordering: the TXN_RESULT reaches the requester BEFORE its TOWN_SVC_STATE", len(ti) == 1 and len(ts) == 1 and ti[0] < ts[0])
    ck("D1 host log: exactly one 'TXN APPLIED kind=MUSEUM_DONATE' and one committed line with donor slot %d" % donor_a,
       run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE request=%d " % rid, off) == 1
       and run.n_log(r"MUSEUM_DONATE item=0x%04X slot=3 committed \(donor slot %d\) \[TXN\]" % (item1, donor_a), off) == 1)

    # ---- duplicates / replay / CONFLICT ----
    n_ts, off2 = ts_count(a, SVC_MUSEUM), len(run.log())
    a.resend_txn(sent, copies=3)
    a.resend_txn(sent)
    got = a.wait_txn_result(sent.seq, 3.0, nth=3)
    allr = a.txn_results_for(sent.seq)
    ck("D2 three identical requests get three answers: APPLIED/NONE then two APPLIED/REPLAYED, all carrying the same svc_seq16 and post-image",
       got is not None and len(allr) == 3 and allr[0].reason == R["NONE"] and [g.reason for g in allr[1:]] == [R["REPLAYED"]] * 2
       and all(g.outcome == APPLIED and g.svc_seq16 == r.svc_seq16 and tuple(g.post_pockets) == tuple(r.post_pockets) for g in allr))
    L.pump_sleep(0.8)
    ck("D2 NO second execution: no new applied line, no new museum state (seq unchanged), the nibble is still A's",
       run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE request=%d " % rid, off2) == 0 and ts_count(a, SVC_MUSEUM) == n_ts and ts_count(b, SVC_MUSEUM) == n_ts
       and run.n_log(r"service 2 \(MUSEUM\) state ->", off2) == 0)
    bad = bytearray(sent.raw)
    bad[18] ^= 1  # tag.item low byte: same (nonce, seq), different bytes
    a.send_txn_commit(K_DONATE, rid, D_NONE, 3, item1, raw=bytes(bad))
    rc = a.wait_txn_result(sent.seq, 2.5, nth=4)
    ck("D2 same (nonce, seq) with DIFFERENT bytes -> REJECTED(CONFLICT), never executed", rc is not None and rc.outcome == REJECTED and rc.reason == R["CONFLICT"])

    # ---- already donated: B offers the same fish: rejected, B keeps the item ----
    n_ts, off3 = ts_count(b, SVC_MUSEUM), len(run.log())
    b_rev = b.rec_last[2]
    rid_b, sent_b, rb = donate(run, b, 3, item1)
    run.tx_ok("D3 B offers the fish A already donated", rb, REJECTED, R["ALREADY_DONATED"])
    L.pump_sleep(0.6)
    ck("D3 nothing changed: no applied line, no museum state, B's rev unchanged, the RESULT carries no post-image (B keeps the item)",
       run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE request=%d " % rid_b, off3) == 0 and ts_count(b, SVC_MUSEUM) == n_ts and b.rec_last[2] == b_rev
       and rb is not None and all(p == 0 for p in rb.post_pockets) and rb.rev == b_rev
       and nib(latest(b, SVC_MUSEUM)[1], "fish", f1) == donor_a)

    # ---- concurrent donation of the same species: exactly one winner ----
    mark_ts = a.inbox.mark()
    rid_a2, rid_b2 = run.fresh_rid(), run.fresh_rid()
    sa2 = a.send_txn_commit(K_DONATE, rid_a2, D_NONE, 4, item2)
    sb2 = b.send_txn_commit(K_DONATE, rid_b2, D_NONE, 4, item2)
    ra2, rb2 = a.wait_txn_result(sa2.seq, 3.0), b.wait_txn_result(sb2.seq, 3.0)
    ck("D4 concurrent donation of the same fish: exactly ONE APPLIED and the other REJECTED(ALREADY_DONATED)",
       ra2 is not None and rb2 is not None and sorted([ra2.outcome, rb2.outcome]) == [APPLIED, REJECTED]
       and [x.reason for x in (ra2, rb2) if x.outcome == REJECTED] == [R["ALREADY_DONATED"]], )
    win_donor = donor_a if ra2 is not None and ra2.outcome == APPLIED else donor_b
    L.pump_sleep(0.8)
    cur = a.ts_latest(SVC_MUSEUM)[1]
    ck("D4 the museum credits the WINNER (slot + 1 = %d), once" % win_donor, nib(cur, "fish", f2) == win_donor
       and run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE request=(%d|%d) " % (rid_a2, rid_b2)) == 1)

    # ---- a second category (insect) by B, and rejections of every other kind ----
    rid_i, sent_i, ri = donate(run, b, 5, bug)
    run.tx_ok("D5 B donates an undonated insect", ri, APPLIED, R["NONE"])
    L.pump_sleep(0.5)
    ck("D5 the insect nibble is B's donor slot + 1 (%d) in the next mirror" % donor_b, nib(a.ts_latest(SVC_MUSEUM)[1], "insect", ins) == donor_b)
    mus_now = a.ts_latest(SVC_MUSEUM)
    n_ts, off4 = ts_count(a, SVC_MUSEUM), len(run.log())
    _r, _s, rn = donate(run, a, 6, 0x2800)
    run.tx_ok("D6 an apple is not a museum donation", rn, REJECTED, R["NOT_DONATABLE"])
    pre_a = a.txn_pre_image()
    _r, _s, rp = donate(run, a, 6, item3, pre=((0,) * 15, pre_a[1], pre_a[2]), base=(a.rec_last[1], a.rec_last[2]))
    run.tx_ok("D6 the pre-image does not hold the donated item -> PRECOND", rp, REJECTED, R["PRECOND"])
    _r, _s, rbi = donate(run, a, 6, item3, pre=((0,) * 14 + (item3,), pre_a[1], 0xFFFFFFFF), base=(a.rec_last[1], a.rec_last[2]))
    run.tx_ok("D6 an impossible wallet in the pre-image -> BAD_IMAGE", rbi, REJECTED, R["BAD_IMAGE"])
    _r, _s, rs1 = donate(run, a, 14, item3, base=(a.rec_last[1], max(0, a.rec_last[2] - 3)), pre=(tuple(item3 if i == 14 else 0 for i in range(15)), pre_a[1], pre_a[2]))
    run.tx_ok("D6 a base rev older than this resident's last pocket transaction -> STALE_IMAGE", rs1, REJECTED, R["STALE_IMAGE"])
    _r, _s, rs2 = donate(run, a, 14, item3, base=(a.rec_last[1] ^ 0x55, a.rec_last[2]), pre=(tuple(item3 if i == 14 else 0 for i in range(15)), pre_a[1], pre_a[2]))
    run.tx_ok("D6 a base from another lineage (epoch) -> STALE_IMAGE", rs2, REJECTED, R["STALE_IMAGE"])
    L.pump_sleep(0.8)
    ck("D6 none of the rejections changed the museum (no new state, no applied line)", ts_count(a, SVC_MUSEUM) == n_ts
       and run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE", off4) == 0 and a.ts_latest(SVC_MUSEUM)[1] == mus_now[1])
    return dict(item1=item1, f1=f1, donor_a=donor_a)


def m_malformed(run):
    """BAD_SHAPE probes on throw-away sessions (2 per connection: three structural violations close a peer)."""
    ck = run.check
    for n, (label, kind, dest, slot, item, kw) in enumerate([
            ("donate dest POCKET", K_DONATE, D_POCKET, 2, 0x2305, {}), ("donate aux_cond != 0", K_DONATE, D_NONE, 2, 0x2305, dict(aux_cond=1)),
            ("donate slot 15", K_DONATE, D_NONE, 15, 0x2305, {}), ("donate item 0", K_DONATE, D_NONE, 2, 0, {}),
            ("claim dest NONE", K_CLAIM, D_NONE, 2, 0x2800, dict(aux_cond=1)), ("claim lost-and-found index 20", K_CLAIM, D_POCKET, 2, 0x2800, dict(aux_cond=20)),
            ("donate flags EXCHANGE", K_DONATE, D_NONE, 2, 0x2305, dict(flags=1)), ("claim aux_item != 0", K_CLAIM, D_POCKET, 2, 0x2800, dict(aux_cond=1, aux_item=7)),
    ]):
        if n % 2 == 0:
            t = run.ready("S%d" % n, run.r3)
        rev = t.rec_last[2]
        sent = t.send_txn_commit(kind, run.fresh_rid(), dest, slot, item, **kw)
        r = t.wait_txn_result(sent.seq, 2.5)
        run.tx_ok(f"S{n} malformed town-service COMMIT ({label})", r, REJECTED, R["BAD_SHAPE"])
        ck(f"S{n} ... the peer is still connected after the violation and the mirror did not move", t.is_connected() and t.rec_last[2] == rev)
        if n % 2 == 1:
            run.release(t)


def m_police(run, a, b, pol0):
    ck = run.check
    g0, blob0 = pol0
    old = pol_items(blob0)
    idx = {v: old.index(v) for v in SEEDS}
    donor = run.r1
    pre = a.txn_pre_image()
    base = a.rec_last
    off = len(run.log())
    mark_a = a.inbox.mark()
    rid, sent, r = claim(run, a, 7, SEEDS[0], idx[SEEDS[0]])
    run.tx_ok("P1 POLICE_CLAIM of the first seeded item at (index, expected item)", r, APPLIED, R["NONE"])
    if r is None:
        return None
    ck("P1 RESULT echoes kind 9 / request / nonce / seq, dest POCKET, the free slot and the item; rev == base + 1",
       (r.kind, r.request_id, r.txn_nonce, r.txn_seq, r.dest, r.slot, r.item) == (K_CLAIM, rid, sent.nonce, sent.seq, D_POCKET, 7, SEEDS[0])
       and (r.host_session, r.epoch, r.rev) == (base[0], base[1], base[2] + 1))
    ck("P1 post-image: the claimed item is in the chosen slot, every other pocket / conds / wallet unchanged (the item entered the MIRROR pocket)",
       tuple(r.post_pockets) == tuple(SEEDS[0] if i == 7 else (0 if i == 7 else p) for i, p in enumerate(pre[0])) and r.post_wallet == pre[2]
       and r.cdig == TP.cown_digest(a.rec_local))
    stA = a.wait_ts_state(SVC_POLICE, 3.0, min_seq=g0.seq + 1)
    stB = b.wait_ts_state(SVC_POLICE, 3.0, min_seq=g0.seq + 1)
    ck("P1 mirror: BOTH clients receive police seq + 1", stA is not None and stB is not None and stA[1] == stB[1] and stA[0].seq == g0.seq + 1)
    if stA is None:
        return None
    new1 = pol_items(stA[1])
    exp1 = compact([x for i, x in enumerate(old) if i != idx[SEEDS[0]]])
    ck("P1 the host removed exactly that item and COMPACTED the array like the vanilla exit compaction (no hole before a later item)", new1 == exp1)
    ck("P1 svc_seq16 == low 16 bits of the police seq; the RESULT precedes the mirror; one applied line",
       r.svc_seq16 == (stA[0].seq & 0xFFFF) and len(txn_idx(a, sent.seq, mark_a)) == 1 and txn_idx(a, sent.seq, mark_a)[0] < ts_indices(a, SVC_POLICE, mark_a)[0]
       and run.n_log(r"TXN APPLIED kind=POLICE_CLAIM request=%d " % rid, off) == 1)
    n_ts, off2 = ts_count(a, SVC_POLICE), len(run.log())
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 3.0, nth=2)
    L.pump_sleep(0.6)
    ck("P2 the identical resend replays APPLIED/REPLAYED with the same svc_seq16, NOT executed again (no applied line, no new police state)",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and rep.svc_seq16 == r.svc_seq16
       and run.n_log(r"TXN APPLIED kind=POLICE_CLAIM request=%d " % rid, off2) == 0 and ts_count(a, SVC_POLICE) == n_ts and a.ts_latest(SVC_POLICE)[1] == stA[1])

    # ---- mismatches ----
    n_ts, off3 = ts_count(b, SVC_POLICE), len(run.log())
    old_idx = idx[SEEDS[0]]
    _r, _s, r_old = claim(run, b, 8, SEEDS[0], old_idx)
    run.tx_ok("P3 the claimed item is gone (already taken): its old index + expected item -> NOT_AVAILABLE", r_old, REJECTED, R["NOT_AVAILABLE"])
    _r, _s, r_wrong = claim(run, b, 8, 0x2FFF, new1.index(SEEDS[1]))
    run.tx_ok("P3 right index, WRONG expected item -> NOT_AVAILABLE", r_wrong, REJECTED, R["NOT_AVAILABLE"])
    empty_idx = new1.index(0)
    _r, _s, r_empty = claim(run, b, 8, SEEDS[1], empty_idx)
    run.tx_ok("P3 an empty lost-and-found slot -> NOT_AVAILABLE", r_empty, REJECTED, R["NOT_AVAILABLE"])
    last_old = idx[SEEDS[2]]
    last_new = new1.index(SEEDS[2])
    ck("P3 setup: the compaction really shifted the last seeded item (stale index != current index)", last_old != last_new)
    _r, _s, r_stale = claim(run, b, 8, SEEDS[2], last_old)
    run.tx_ok("P3 RACE: B uses the index it saw BEFORE the compaction (the array shifted) -> NOT_AVAILABLE (never the wrong item)", r_stale, REJECTED, R["NOT_AVAILABLE"])
    _r, _s, r_good = claim(run, b, 8, SEEDS[2], last_new)
    run.tx_ok("P3 ... and the retry with the current index wins", r_good, APPLIED, R["NONE"])
    stB2 = b.wait_ts_state(SVC_POLICE, 3.0, min_seq=stA[0].seq + 1)
    new2 = pol_items(stB2[1]) if stB2 else None
    ck("P3 the rejected claims changed nothing; only B's winning claim removed an item (compacted)",
       new2 == compact([x for x in new1 if x != SEEDS[2]]) and run.n_log(r"TXN APPLIED kind=POLICE_CLAIM", off3) == 1)
    L.pump_sleep(0.4)

    # ---- concurrent claim of the last remaining seed (SEEDS[1]) ----
    cur = pol_items(a.ts_latest(SVC_POLICE)[1])
    j = cur.index(SEEDS[1])
    rid_a, rid_b = run.fresh_rid(), run.fresh_rid()
    pre_a = a.txn_pre_image()
    pre_b = b.txn_pre_image()
    sa = a.send_txn_commit(K_CLAIM, rid_a, D_POCKET, 9, SEEDS[1], pre=(tuple(0 if i == 9 else p for i, p in enumerate(pre_a[0])), pre_a[1], pre_a[2]), aux_cond=j)
    sb = b.send_txn_commit(K_CLAIM, rid_b, D_POCKET, 9, SEEDS[1], pre=(tuple(0 if i == 9 else p for i, p in enumerate(pre_b[0])), pre_b[1], pre_b[2]), aux_cond=j)
    ra, rb = a.wait_txn_result(sa.seq, 3.0), b.wait_txn_result(sb.seq, 3.0)
    ck("P4 two clients claim the SAME item at the same moment: exactly one APPLIED, the other REJECTED(NOT_AVAILABLE)",
       ra is not None and rb is not None and sorted([ra.outcome, rb.outcome]) == [APPLIED, REJECTED]
       and [x.reason for x in (ra, rb) if x.outcome == REJECTED] == [R["NOT_AVAILABLE"]])
    L.pump_sleep(0.5)
    ck("P4 the item is gone from the lost and found exactly once; the loser's pocket did not gain it",
       SEEDS[1] not in pol_items(a.ts_latest(SVC_POLICE)[1]) and run.n_log(r"POLICE_CLAIM item=0x%04X lost-and-found slot=\d+ -> pocket slot 9 committed" % SEEDS[1]) == 1)
    return dict(final_pol=pol_items(a.ts_latest(SVC_POLICE)[1]))


def m_peers(run, a):
    ck = run.check
    host = run.host
    L.pump_sleep(0.3)
    rev_chk = a.rec_last[2]
    pol_n = ts_count(a, SVC_POLICE)
    probe = a.build_txn_commit_bytes(K_DONATE, 1, D_NONE, 0, 0x2305, pre=((0x2305,) + (0,) * 14, 0, 0), base=(a.rec_last[1], a.rec_last[2]))
    hs = L.FakeClient("HS", run.ip, run.port, context_flags=None, wait_snapshot=False, record_hello=False)
    hs.connect(timeout=3.0)
    off = len(host.log_text())
    hs.send_reliable(probe)
    L.pump_sleep(1.0)
    ck("X1 a HANDSHAKE peer (no IDENTITY) gets no answer to a MUSEUM_DONATE, no TOWN_SVC_STATE, and the host logs no TXN / MSM activity",
       not hs.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_TXN_RESULT, L.PC_NETGAME_MSG_TOWN_SVC_STATE))
       and "[NET][TXN]" not in run.log(off) and "[NET][MSM]" not in run.log(off) and hs.is_connected())
    hs.close()
    holder = run.ready("H", run.r3)
    park = L.FakeClient("PK", run.ip, run.port, player=L.resident_player(run.r3), context_flags=None, wait_snapshot=False, record_hello=False)
    park.connect(timeout=3.0)
    park.send_identity(town=L.resolve_host_town(run.ip, run.port), player=L.resident_player(run.r3))
    L.pump_sleep(0.5)
    off = len(host.log_text())
    hrev = holder.rec_last[2]
    park.send_reliable(probe)
    L.pump_sleep(1.0)
    ck("X2 a PARKED peer (claim on a live resident) gets no TXN_RESULT / TOWN_SVC_STATE and causes no TXN activity",
       not park.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_TXN_RESULT, L.PC_NETGAME_MSG_IDENTITY_ACK, L.PC_NETGAME_MSG_TOWN_SVC_STATE))
       and "[NET][TXN]" not in run.log(off) and holder.rec_last[2] == hrev)
    park.close()
    own = L.FakeClient("OWN", run.ip, run.port, player=L.resident_player(run.host_slot), context_flags=None, wait_snapshot=False, record_hello=False)
    own.connect(timeout=3.0)
    off = len(host.log_text())
    try:
        own.send_identity(town=L.resolve_host_town(run.ip, run.port), player=L.resident_player(run.host_slot))
        L.pump_sleep(0.5)
        own.send_reliable(probe)
        L.pump_sleep(0.8)
    except Exception:  # noqa: BLE001
        pass
    ck("X3 a peer claiming the HOST's own resident (refused at identity) gets no TXN_RESULT / TOWN_SVC_STATE and nothing is mutated",
       not own.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_TXN_RESULT, L.PC_NETGAME_MSG_TOWN_SVC_STATE))
       and "TXN APPLIED" not in run.log(off) and "[NET][MSM]" not in run.log(off))
    own.close()
    run.release(holder)
    ck("X4 nothing above changed A's mirror or sent A another police state", a.rec_last[2] == rev_chk and ts_count(a, SVC_POLICE) == pol_n)


def m_new_session_and_persistence(run, a, b, exp_mus_blob, exp_pol):
    ck = run.check
    ck("N1 (setup) the final blobs are known", exp_mus_blob is not None and exp_pol is not None)
    run.release(a)
    run.release(b)
    c = run.ready("C", run.r1)
    sm, sp = latest(c, SVC_MUSEUM, 1, 4.0), latest(c, SVC_POLICE, 1, 4.0)
    ck("N1 a NEW session of A gets the final museum and police state at READY (late join covers every earlier change)",
       sm is not None and sp is not None and sm[1] == exp_mus_blob and pol_items(sp[1]) == exp_pol)
    run.release(c)
    deadline = time.time() + 25.0
    ok, last = False, None
    while time.time() < deadline:
        try:
            mus, pol = gci_state(L.GAME_BIN_DIR)
            last = (mus == exp_mus_blob, pol == exp_pol)
            if all(last):
                ok = True
                break
        except OSError:
            pass
        L.pump_sleep(1.0)
    ck("N2 the host's normal GCI save (the early save after the clients left) persists the museum bits and the lost-and-found array (museum==%s police==%s)" % (last and last[0], last and last[1]), ok)


def phase_main(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    ck("M setup: A and B READY and SYNCED", a.rec_synced and b.rec_synced and a.rec_last is not None and b.rec_last is not None)
    st = m_mirror_at_ready(run, a, b)
    if st is None:
        return
    pol0, mus0 = st
    info = m_museum(run, a, b, mus0)
    if info is None:
        return
    m_malformed(run)
    pol_res = m_police(run, a, b, pol0)
    m_peers(run, a)
    m_new_session_and_persistence(run, a, b, a.ts_latest(SVC_MUSEUM)[1], pol_res["final_pol"] if pol_res else None)


def phase_drop(run):
    ck = run.check
    a = run.ready("A", run.r1)
    g0, blob0 = latest(a, SVC_MUSEUM, 1, 5.0)
    f1 = free_idx(blob0, "fish", 1)[0]
    item = FISH0 + f1
    base = a.rec_last
    rid, sent, first = donate(run, a, 2, item, wait=1.6)
    L.pump_sleep(0.3)
    ck("F1 the APPLIED RESULT is lost (drop_result) but the host DID commit: no result at A, the museum mirror still reaches A with A's donor nibble",
       first is None and run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE request=%d " % rid) == 1 and "FAULT INJECTION FIRED mode=drop_result" in run.log())
    st = a.wait_ts_state(SVC_MUSEUM, 3.0, min_seq=g0.seq + 1)
    ck("F1 ... the mirror carries the donation (nibble = slot + 1)", st is not None and nib(st[1], "fish", f1) == run.r1 + 1)
    a.resend_txn(sent)
    res = a.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.4)
    ck("F1 the resend gets APPLIED/REPLAYED with the SAME svc_seq16 as the mirror seq, rev == base + 1",
       res is not None and res.outcome == APPLIED and res.reason == R["REPLAYED"] and st is not None and res.svc_seq16 == (st[0].seq & 0xFFFF) and res.rev == base[2] + 1)
    ck("F1 executed exactly once: one APPLIED line, one replay line, one museum state change",
       run.n_log(r"TXN APPLIED kind=MUSEUM_DONATE request=%d " % rid) == 1 and run.replay_lines(rid) == 1 and ts_count(a, SVC_MUSEUM) == 2)


def phase_kill(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    g0, blob0 = latest(b, SVC_POLICE, 1, 5.0)
    old = pol_items(blob0)
    item = SEEDS[0]
    pidx = old.index(item)
    pre = a.txn_pre_image()
    base = a.rec_last
    rid, sent, r = claim(run, a, 4, item, pidx, wait=1.0)
    closed = TP.wait_closed(a, 4.0)
    L.pump_sleep(0.4)
    ck("K1 kill_peer_after_commit: the host dropped A after committing and sent NO result",
       closed and r is None and not a.txn_results_for(sent.seq) and "kill_peer_after_commit: peer" in run.log() and run.n_log(r"TXN APPLIED kind=POLICE_CLAIM") == 0
       and run.n_log(r"POLICE_CLAIM item=0x%04X .* committed \[TXN\]" % item) == 1)
    stB = b.wait_ts_state(SVC_POLICE, 3.0, min_seq=g0.seq + 1)
    ck("K1 the OTHER client still gets the new police state (the item is gone, compacted)", stB is not None and pol_items(stB[1]) == compact([x for i, x in enumerate(old) if i != pidx]))
    exp = tuple(item if i == 4 else (0 if i == 4 else p) for i, p in enumerate(pre[0]))
    a.reconnect_and_ready(timeout=8.0)
    pushed = a.rec_pushes[-1] if a.rec_pushes else None
    ck("K2 the same-nonce reconnect is pushed the mirror: it already HAS the effect (item in the slot), rev == base + 1",
       pushed is not None and pushed["rev"] == base[2] + 1 and L.record_inventory(pushed["data"])[0] == exp)
    sa = a.wait_ts_state(SVC_POLICE, 4.0, min_seq=1)
    ck("K2 ... and its new session receives the post-claim police state at READY", sa is not None and sa[1] == stB[1])
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.3)
    ck("K3 the OLD COMMIT resent after a same-nonce reconnect replays APPLIED/REPLAYED (journal survives the peer reset), NO second execution",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and tuple(rep.post_pockets) == exp
       and run.n_log(r"POLICE_CLAIM item=0x%04X .* committed \[TXN\]" % item) == 1 and b.ts_latest(SVC_POLICE)[1] == stB[1])
    run.release(a)
    a2 = run.client("A2", run.r1)
    a2.connect_and_ready(quiet=True)
    p2 = a2.rec_pushes[-1] if a2.rec_pushes else None
    s2 = latest(a2, SVC_POLICE, 1, 4.0)
    ck("K4 a NEW-PROCESS rejoin: the pushed record has the item AND the mirror shows it gone from the lost and found",
       p2 is not None and L.record_inventory(p2["data"])[0] == exp and s2 is not None and s2[1] == stB[1])
    ck("K4 the host survived the dropped peer", run.host.alive())


def phase_hostcfg_on(run):
    """Batch A (A1): a host started WITH --authoritative-wildlife announces authoritative_wildlife=1 at READY (service 4, before the snapshot), once per
    session, to every client incl. a late joiner; a same-nonce reconnect gets it again (new session)."""
    ck = run.check
    a = run.ready("A", run.r1)
    ck("W setup: A READY and SYNCED", a.rec_synced and a.rec_last is not None)
    st = latest(a, L.PC_NETGAME_TS_HOSTCFG, 1, 5.0)
    ck("W1 client A received HOST_CONFIG (service 4): len 8, blob[0] == 1 (authoritative wildlife ON), 7 reserved bytes zero, digest == FNV-1a32 of the blob, seq >= 1",
       st is not None and st[0].len == L.PC_NETGAME_TS_HOSTCFG_LEN and len(st[1]) == 8 and st[1][0] == 1 and st[1][1:] == bytes(7)
       and st[0].digest == L.fnv1a32(st[1]) and st[0].seq >= 1)
    idx = ts_indices(a, L.PC_NETGAME_TS_HOSTCFG)
    snap = a.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_SNAPSHOT_BEGIN, (L.CH_RELIABLE,)))
    ck("W2 HOST_CONFIG precedes the first SNAPSHOT_BEGIN (and the wildlife snapshot that follows it)", len(idx) == 1 and len(snap) >= 1 and idx[0] < snap[0].index)
    b = run.ready("B", run.r2)
    sb = latest(b, L.PC_NETGAME_TS_HOSTCFG, 1, 5.0)
    ck("W3 a late-joining client B also receives authoritative_wildlife=1 (same seq, same blob)", sb is not None and sb[1][0] == 1 and st is not None and sb[0].seq == st[0].seq)
    L.pump_sleep(2.5)
    ck("W4 no spam: exactly ONE HOST_CONFIG per client session after 2.5 s idle (the digest poll sends only on a change)",
       ts_count(a, L.PC_NETGAME_TS_HOSTCFG) == 1 and ts_count(b, L.PC_NETGAME_TS_HOSTCFG) == 1)
    ck("W5 the host log shows the READY push with value 1, once per peer", run.n_log(r"\[NET\]\[HOSTCFG\] host: pushed HOST_CONFIG authoritative_wildlife=1 seq \d+ to peer \d+ \(at READY") == 2)
    a.reconnect_and_ready(timeout=8.0)
    sa2 = a.wait_ts_state(L.PC_NETGAME_TS_HOSTCFG, 5.0, min_seq=1)
    ck("W6 a same-nonce reconnect is announced the mode again at its new READY (value 1)", sa2 is not None and sa2[1][0] == 1)
    ck("W7 the host survived", run.host.alive())


PHASES = [
    ("M", [], phase_main),
    ("W", ["--authoritative-wildlife"], phase_hostcfg_on),
    ("F-drop", ["--txn-fault=drop_result:1:1"], phase_drop),
    ("F-kill", ["--txn-fault=kill_peer_after_commit:1:1"], phase_kill),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11000)
    ap.add_argument("--only", default="", help="comma list of phase names (M, W, F-drop, F-kill)")
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
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "ts_protocol_save_snapshot_%d" % os.getpid())
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
            run_phase(results, ip, args.port + i, tag, extra, body, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
