#!/usr/bin/env python3
"""test_txn_protocol.py - X1a (HOST half) of the host-transactional PICKUP / DROP / BURY commit (TXN_COMMIT 51 / TXN_RESULT 52).

PROTOCOL test against REAL host processes (`AnimalCrossing.exe --host --bootstrap-resident 0 --pickup-test-seed --bury-test-seed
--field-action-test-seed [--txn-fault=...]`, started and stopped by this script from the TEST COPY named by NET_SPIKE_GAME_BIN =
pc\\build64\\bin_fixture4: host = resident 0, clients = residents 1..3, at most 3 simultaneous clients). `require_test_bin_dir()`
refuses the live dir; the fixture save dir is SNAPSHOTTED at start and RESTORED after every host phase. Clients are scripted
FakeClients (net_spike_lib: TXN builders, TXN_RESULT parsing); the host's C code runs for real. The real game CLIENT half is X1b
(X1b: the real client now sends TXN_COMMIT and PC_NETGAME_TXN_RETIRE_LEGACY_COMMIT is 1: a legacy CONFIRM(COMMIT) is RETIRED -- tested here; the
old "processed as before" branch only runs with the constant at 0 and is source-audited in test_txn_src.py).

Phases (one host process each; the spec's matrix T1..T11 for the host half):
  M    main (no fault): T1 success pickup POCKET / money-bag WALLET / drop / bury (world + mirror change, rev+1, RESULT before
       FIELD_UPDATE, cdig, D3 push to a second session shows the mirror), T8 duplicates + byte-identical resend replays (never a
       second execution), stale resend after a newer txn, CONFLICT, NOT_PENDING (same request, new seq), STALE_IMAGE x3, BAD_IMAGE x3,
       PRECOND x7, BAD_SHAPE x14, wrong sizes, T9 FENCED (gap / new process / old nonce) + wraparound, T10 D3 interplay (upload then
       COMMIT, upload on the pre-txn base, push restart), T11 HANDSHAKE / parked / host-own-resident peers, legacy-vs-TXN mixed use (retired legacy COMMIT)
  F-ignore1   ignore_commit:1:1   -> the resend after 2 s executes exactly once
  F-ignore12  ignore_commit:1:12  -> EXPIRED, tile and mirror unchanged
  F-failworld fail_world:1:2      -> WORLD_CHANGED (pickup + bury: reject echo after the RESULT), same-seq replay, tile free again
  F-expire    expire:1:1          -> EXPIRED (L-2: the legacy path answered nothing), replay, tile free again
  F-drop      drop_result:1:2     -> RESULT lost twice, the third resend gets APPLIED/REPLAYED, executed once
  F-kill      kill_peer_after_commit:1:1 -> same-nonce reconnect + old COMMIT resent replays; new-process rejoin push has the effect
  CLI         --txn-fault is HOST-only, validated, documented in --help (no game boot)

Tier: PROTOCOL TESTED (real host binary, scripted clients). Usage: python test_txn_protocol.py [--port 10600] [--only M,F-drop,...]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

import net_spike_lib as L

SAVE_DIR_REL = "save"
KP, KD, KB = L.CONFIRM_KIND_PICKUP, L.CONFIRM_KIND_DROP, L.CONFIRM_KIND_BURY
APPLIED, REJECTED = L.PC_NETGAME_TXN_OUTCOME_APPLIED, L.PC_NETGAME_TXN_OUTCOME_REJECTED
R = {n.replace("PC_NETGAME_TXN_REASON_", ""): getattr(L, n) for n in dir(L) if n.startswith("PC_NETGAME_TXN_REASON_")}
D_NONE, D_POCKET, D_WALLET, S_WALLET = (L.PC_NETGAME_TXN_DEST_NONE, L.PC_NETGAME_TXN_DEST_POCKET, L.PC_NETGAME_TXN_DEST_WALLET,
                                        L.PC_NETGAME_TXN_SLOT_WALLET)
ITM_ROD = 0x2203
ITM_MONEY_100 = 0x2103
GENERIC_BURY_TILE = (24, 106)   # --bury-test-seed: HOLE_START
BURY_TILE_2 = (40, 106)         # HOLE_START
ROCK_TILE = (24, 104)           # --field-action-test-seed: money rock
ROCK_DROP_TILE = (23, 103)      # where the first hit drops an ITM_MONEY_100 bag
HOST_EXTRA = ["--pickup-test-seed", "--bury-test-seed", "--field-action-test-seed"]


def cown_digest(rec):
    """FNV-1a32 over the CLIENT+SHARED ranges of a BE record image (what TXN_RESULT.cdig carries)."""
    return L.fnv1a32(b"".join(rec[o:o + n] for _nm, o, n, own in L.RECORD_FIELD_RANGES if own in (L.REC_OWN_CLIENT, L.REC_OWN_SHARED)))


def first_free(pockets):
    return next(i for i, p in enumerate(pockets) if p == 0)


def first_free_pocket(c):
    return first_free(c.txn_pre_image()[0])


class Run:
    """One host phase: the host process, its log, clients, a shared tile queue and the check recorder."""

    def __init__(self, ip, port, host, results, snap_gci, host_slot, residents):
        self.ip, self.port, self.host, self.results = ip, port, host, results
        self.snap_gci, self.host_slot = snap_gci, host_slot
        self.r1, self.r2, self.r3 = residents
        self.fresh_rid = L.make_request_id_counter(1000)
        self.queue = L.CandidateQueue(fallback=False)
        self.clients = []

    def check(self, desc, cond):
        L.check(desc, bool(cond), self.results)

    # --- clients -----------------------------------------------------------------------------------------------
    def client(self, label, slot, **kw):
        c = L.FakeClient(label, self.ip, self.port, player=L.resident_player(slot), **kw)
        c.rec_resident_idx = slot
        self.clients.append(c)
        return c

    def ready(self, label, slot, **kw):
        c = self.client(label, slot, **kw)
        c.connect_and_ready(quiet=True)
        return c

    def release(self, c):
        try:
            if c.state in (c.STATE_CONNECTED, c.STATE_PENDING):
                c.disconnect()
        except Exception:  # noqa: BLE001
            pass
        c.close()
        L.pump_sleep(0.6)

    # --- host log ----------------------------------------------------------------------------------------------
    def log(self, off=0):
        return self.host.log_text()[off:]

    def n_log(self, rx, off=0):
        return len(re.findall(rx, self.log(off)))

    def applied_lines(self, rid, off=0):
        return self.n_log(r"TXN APPLIED kind=\w+ request=%d " % rid, off)

    def replay_lines(self, rid, outcome="APPLIED", off=0):
        return self.n_log(r"TXN replay %s\S* kind=\w+ request=%d " % (outcome, rid), off)

    # --- world / fixtures --------------------------------------------------------------------------------------
    def reserve(self, c, tile=None, attempts=25):
        """PICKUP_REQUEST until accepted: the PENDING reservation is held. Returns (tile, rid, prov) or None."""
        for _ in range(attempts):
            t = tile if tile is not None else self.queue.next_tile()
            if t is None:
                return None
            rid = self.fresh_rid()
            prov = c.pickup(t[0], t[1], rid, timeout=1.0, claim=True, auto_confirm=False)
            if prov is not None and prov.accepted:
                return t, rid, prov
            if tile is not None:
                return None
        return None

    def field_updates(self, c, tile, since):
        return [f for f in c.field_updates_since(since) if L.field_update_tuple(f)[:2] == tuple(tile)]

    def tile_of(self, c, tile):
        return c.world.tile_ut(*tile)

    def commit(self, c, kind, rid, dest, slot, item, wait=2.0, **kw):
        """send_txn_commit + wait for its result: (TxnSent, result or None)."""
        sent = c.send_txn_commit(kind, rid, dest, slot, item, **kw)
        return sent, c.wait_txn_result(sent.seq, wait)

    def tx_ok(self, desc, r, outcome, reason):
        self.check(f"{desc}: outcome {'APPLIED' if outcome == APPLIED else 'REJECTED'}, reason {L.TXN_REASON_NAMES.get(reason)} "
                   f"(got {None if r is None else (r.outcome, L.TXN_REASON_NAMES.get(r.reason))})",
                   r is not None and r.outcome == outcome and r.reason == reason)


def wait_closed(c, timeout=3.0):
    return c.wait_disconnected(timeout) and c.disconnected_by_host


def start_host(ip, port, tag, extra, results):
    host_slot = L.TEST_HOST_RESIDENT
    log_dir = os.path.dirname(os.path.abspath(__file__))
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot)] + HOST_EXTRA + list(extra),
                         log_path=os.path.join(log_dir, f"txn_protocol_{tag}_host.log")).start()
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
    run = Run(ip, port, host, results, snap_gci, host_slot, residents[:3])
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


# ======================================================================================================================
# Phase M
# ======================================================================================================================
def drop_back(run, c, slot, item, tile):
    """A txn DROP of `item` from `slot` onto `tile` (the tile the item was picked from): restores the tile for later sections."""
    prov, _sent, res = c.txn_drop(slot, item, tile[0], tile[1], run.fresh_rid(), claim_at=tile)
    run.tx_ok("setup: drop back onto the tile (a newer transaction)", res, APPLIED, R["NONE"])
    return res


def t1_success_paths(run, a, b):
    ck = run.check
    # ---------------- T1a: pickup into a POCKET ----------------
    t1 = run.reserve(a)
    ck("T1a a seeded tile could be reserved (provisional accept)", t1 is not None)
    if t1 is None:
        return None
    tile1, rid1, prov1 = t1
    mark_a, mark_b = a.inbox.mark(), b.inbox.mark()
    pre = a.txn_pre_image()
    slot1 = first_free(pre[0])
    base1 = a.rec_last
    sent1, r1 = run.commit(a, KP, rid1, D_POCKET, slot1, prov1.granted_item)
    run.tx_ok("T1a pickup POCKET", r1, APPLIED, R["NONE"])
    if r1 is None:
        return None
    ck("T1a RESULT echoes kind/request/nonce/seq/dest/slot/item",
       (r1.kind, r1.request_id, r1.txn_nonce, r1.txn_seq, r1.dest, r1.slot, r1.item) == (KP, rid1, sent1.nonce, sent1.seq, D_POCKET, slot1, prov1.granted_item))
    ck("T1a lineage: host_session/epoch unchanged, rev == base rev + 1", (r1.host_session, r1.epoch, r1.rev) == (base1[0], base1[1], base1[2] + 1))
    exp_p = tuple(prov1.granted_item if i == slot1 else p for i, p in enumerate(pre[0]))
    ck("T1a post-image: the item is in the chosen slot, every other pocket / conds / wallet unchanged",
       tuple(r1.post_pockets) == exp_p and r1.post_conds == pre[1] and r1.post_wallet == pre[2])
    ck("T1a cdig == FNV of the client+shared ranges of the mirror (computed from the patched local image)", r1.cdig == cown_digest(a.rec_local) and r1.cdig != 0)
    L.pump_sleep(0.5)
    ck("T1a world: the observer sees the tile EMPTY, via exactly ONE FIELD_UPDATE for it",
       run.tile_of(b, tile1) == L.EMPTY_NO and len(run.field_updates(b, tile1, mark_b)) == 1)
    res_idx = [m.index for m in a.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_TXN_RESULT, (L.CH_RELIABLE,)), since=mark_a)]
    fu_idx = [m.index for m in a.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_FIELD_UPDATE, (L.CH_RELIABLE,)), since=mark_a)
              if L.field_update_tuple(m.game)[:2] == tuple(tile1)]
    ck("T1a the TXN_RESULT is delivered BEFORE the FIELD_UPDATE broadcast of the same commit", res_idx and fu_idx and res_idx[0] < fu_idx[0])
    ck("T1a host log: exactly one 'TXN APPLIED' and one '[TXN]' committed line for this request",
       run.applied_lines(rid1) == 1 and run.n_log(r"request %d committed tile .* \[TXN\]" % rid1) == 1)

    # ---------------- T1b: drop (the apple goes back on the same tile) ----------------
    mark_b = b.inbox.mark()
    pre = a.txn_pre_image()
    base = a.rec_last
    prov, sentd, rd = a.txn_drop(slot1, prov1.granted_item, tile1[0], tile1[1], run.fresh_rid(), claim_at=tile1)
    ck("T1b drop provisional accepted", prov is not None and prov.accepted)
    run.tx_ok("T1b drop", rd, APPLIED, R["NONE"])
    if rd is not None:
        ck("T1b drop: dest NONE, the slot is EMPTY in the post-image (others unchanged), rev + 1, cdig matches",
           rd.dest == D_NONE and rd.post_pockets[slot1] == 0 and rd.rev == base[2] + 1
           and tuple(rd.post_pockets) == tuple(0 if i == slot1 else p for i, p in enumerate(pre[0])) and rd.cdig == cown_digest(a.rec_local))
    L.pump_sleep(0.5)
    ck("T1b drop world: the tile holds the item again, via exactly one FIELD_UPDATE",
       run.tile_of(b, tile1) == prov1.granted_item and len(run.field_updates(b, tile1, mark_b)) == 1)

    # ---------------- T1c: wallet pickup of a money bag (the money rock drops one) ----------------
    a.claim_position(*L.tile_center(*ROCK_TILE))
    a.send_reliable(L.build_field_action_request(2, ROCK_TILE[0], ROCK_TILE[1], run.fresh_rid()))  # FIELD_ACTION MONEY_ROCK_HIT (X3: 76 B, all-zero tag = no grant)
    L.pump_sleep(0.8)
    pre_b = b.txn_pre_image()
    bag = run.reserve(b, tile=ROCK_DROP_TILE)
    ck("T1c the money rock dropped a bag that B can reserve (provisional accept, ITM_MONEY_100)", bag is not None and bag[2].granted_item == ITM_MONEY_100)
    if bag is not None:
        _t, rid_bag, prov_bag = bag
        base = b.rec_last
        sentw, rw = run.commit(b, KP, rid_bag, D_WALLET, S_WALLET, prov_bag.granted_item)
        run.tx_ok("T1c pickup WALLET", rw, APPLIED, R["NONE"])
        if rw is not None:
            ck("T1c wallet pickup: dest WALLET/slot 0xFF, wallet + 100 bells, pockets and conds unchanged, rev + 1, cdig matches",
               rw.dest == D_WALLET and rw.slot == S_WALLET and rw.post_wallet == pre_b[2] + 100 and tuple(rw.post_pockets) == tuple(pre_b[0])
               and rw.post_conds == pre_b[1] and rw.rev == base[2] + 1 and rw.cdig == cown_digest(b.rec_local))

    # ---------------- T1d: bury ----------------
    mark_b = b.inbox.mark()
    rid = run.fresh_rid()
    base = a.rec_last
    prov, sentb, rb = a.txn_bury(3, ITM_ROD, GENERIC_BURY_TILE[0], GENERIC_BURY_TILE[1], rid)
    ck("T1d bury provisional accepted", prov is not None and prov.accepted)
    run.tx_ok("T1d bury", rb, APPLIED, R["NONE"])
    if rb is not None:
        ck("T1d bury: slot 3 EMPTY in the post-image (the claimed item was forced into the pre-image), rev + 1, dest NONE",
           rb.post_pockets[3] == 0 and rb.rev == base[2] + 1 and rb.dest == D_NONE)
    L.pump_sleep(0.5)
    ck("T1d bury world: the tile became the buried item with ONE FIELD_UPDATE",
       run.tile_of(b, GENERIC_BURY_TILE) == ITM_ROD and len(run.field_updates(b, GENERIC_BURY_TILE, mark_b)) == 1)
    ck("T1d bury: the TXN path sends NO success BURY_RESULT(accepted=1) follow-up (only the provisional one)",
       sum(1 for t, rid_, g, _c in a.result_log if t == L.PC_NETGAME_MSG_BURY_RESULT and rid_ == rid and g.accepted) == 1)
    return tile1, prov1


def t8_duplicates(run, a, b):
    ck = run.check
    epoch = a.rec_last[1]
    tile2, rid2, prov2 = run.reserve(a)
    mark_b = b.inbox.mark()
    pre = a.txn_pre_image()
    slot2 = first_free(pre[0])
    sent2 = a.send_txn_commit(KP, rid2, D_POCKET, slot2, prov2.granted_item)
    a.send_reliable_duplicate(sent2.raw, copies=3)   # a second reliable message with the SAME bytes, its datagram sent 3x
    a.resend_txn(sent2)                               # and a third (what the real client does after 2 s without a RESULT)
    got = a.wait_txn_result(sent2.seq, 3.0, nth=3)
    allr = a.txn_results_for(sent2.seq)
    ck("T8 three identical COMMITs get three answers: the first APPLIED/NONE, the other two APPLIED/REPLAYED",
       got is not None and len(allr) == 3 and allr[0].reason == R["NONE"] and all(g.outcome == APPLIED for g in allr)
       and [g.reason for g in allr[1:]] == [R["REPLAYED"], R["REPLAYED"]])
    ck("T8 the replays carry the same post-image/rev/cdig as the execution (nothing changed in between)",
       all(tuple(g.post_pockets) == tuple(allr[0].post_pockets) and g.rev == allr[0].rev and g.cdig == allr[0].cdig for g in allr))
    L.pump_sleep(0.4)
    ck("T8 NO double execution: one 'TXN APPLIED' line, two replay lines, exactly one FIELD_UPDATE, the item exists once more than before",
       run.applied_lines(rid2) == 1 and run.replay_lines(rid2) == 2 and len(run.field_updates(b, tile2, mark_b)) == 1
       and list(allr[0].post_pockets).count(prov2.granted_item) == list(pre[0]).count(prov2.granted_item) + 1)
    rev_after_t2 = allr[0].rev
    sent_dbl, r_dbl = run.commit(a, KP, rid2, D_POCKET, first_free_pocket(a), prov2.granted_item)
    run.tx_ok("T8 a second COMMIT (NEW seq) for an already committed request", r_dbl, REJECTED, R["NOT_PENDING"])
    ck("T8 ... changed nothing (rev unchanged in the reject, still ONE APPLIED line: the slot was consumed once)",
       r_dbl is not None and r_dbl.rev == rev_after_t2 and run.applied_lines(rid2) == 1)
    bad_raw = a.build_txn_commit_bytes(KP, rid2, D_POCKET, slot2, prov2.granted_item, seq=sent2.seq, nonce=sent2.nonce,
                                       pre=a.txn_pre_image(), base=(epoch, rev_after_t2 + 1))
    a.send_txn_commit(KP, rid2, 0, 0, 0, raw=bad_raw)
    rc = a.wait_txn_result(sent2.seq, 2.0, nth=4)
    ck("T8 same (nonce, seq) with DIFFERENT bytes -> REJECTED(CONFLICT), loudly logged, never executed",
       rc is not None and rc.outcome == REJECTED and rc.reason == R["CONFLICT"]
       and ("CONFLICT: seq %d was seen with DIFFERENT bytes" % sent2.seq) in run.log())
    mark_b = b.inbox.mark()
    prov, sentd2, rd2 = a.txn_drop(slot2, prov2.granted_item, tile2[0], tile2[1], run.fresh_rid(), claim_at=tile2)
    run.tx_ok("T8 setup: a NEWER transaction (drop back) is APPLIED", rd2, APPLIED, R["NONE"])
    L.pump_sleep(0.4)
    a.resend_txn(sent2)
    rs = a.wait_txn_result(sent2.seq, 2.0, nth=5)
    ck("T8 stale resend after a NEWER txn: replays APPLIED/REPLAYED with the CURRENT mirror (the newer txn's rev), never re-executes",
       rs is not None and rs.outcome == APPLIED and rs.reason == R["REPLAYED"] and rd2 is not None and rs.rev == rd2.rev
       and tuple(rs.post_pockets) == tuple(rd2.post_pockets) and run.applied_lines(rid2) == 1)
    L.pump_sleep(0.4)
    ck("T8 ... and the world did not change again (the tile holds the item from the drop, ONE FIELD_UPDATE since)",
       run.tile_of(b, tile2) == prov2.granted_item and len(run.field_updates(b, tile2, mark_b)) == 1)


def t_validation(run, a, b):
    """STALE_IMAGE x3 (+ paced push), BAD_IMAGE x3, PRECOND x4 (pickup) and x3 (drop/bury), BAD_SHAPE x14, wrong sizes."""
    ck = run.check
    host = run.host
    tile_v, rid_v, prov_v = run.reserve(a)
    cur = a.rec_last
    pre = a.txn_pre_image()
    sl = first_free(pre[0])
    for label, mk in (("base_rev older than the last pocket transaction", lambda c: (c[1], c[2] - 1)),
                      ("base_epoch of another lineage", lambda c: (c[1] ^ 0x1234, c[2])),
                      ("base_rev from the future", lambda c: (c[1], c[2] + 7))):
        np0 = len(a.rec_pushes)
        s_, r_ = run.commit(a, KP, rid_v, D_POCKET, sl, prov_v.granted_item, base=mk(cur))
        run.tx_ok(f"STALE {label}", r_, REJECTED, R["STALE_IMAGE"])
        ck(f"STALE {label}: the reject reports the CURRENT rev, the tile is untouched", r_ is not None and r_.rev == cur[2] and run.tile_of(b, tile_v) == prov_v.granted_item)
        if label.startswith("base_rev older"):
            ck("STALE the paced full push follows a STALE_IMAGE (the client re-syncs)", a.wait_record_push(after=np0, timeout=4.0) is not None)
        L.pump_sleep(2.2)  # past the 2 s stale-push pacing
        again = run.reserve(a, tile=tile_v)  # the REJECTED released the reservation: the same tile can be reserved again
        ck(f"STALE {label}: the REJECTED released the reservation (a new request on the same tile is accepted)", again is not None)
        if again is None:
            return False
        rid_v, prov_v = again[1], again[2]
        cur = a.rec_last
        pre = a.txn_pre_image()

    mark_b = b.inbox.mark()
    rev_now = cur[2]
    sl = first_free(pre[0])
    state = {"rid": rid_v, "prov": prov_v}

    def neg(desc, reason, kind=KP, dest=D_POCKET, slot=None, item=None, **kw):
        slot = sl if slot is None else slot
        item = state["prov"].granted_item if item is None else item
        s_, r_ = run.commit(a, kind, state["rid"], dest, slot, item, **kw)
        run.tx_ok(desc, r_, REJECTED, reason)
        ck(f"{desc}: nothing mutated (reject rev == current rev, tile unchanged)", r_ is not None and r_.rev == rev_now
           and run.tile_of(b, tile_v) == state["prov"].granted_item)
        again = run.reserve(a, tile=tile_v)  # the reject released the reservation
        if again is not None:
            state["rid"], state["prov"] = again[1], again[2]
        return r_

    p_illegal = tuple(0x4000 if i == 1 else p for i, p in enumerate(pre[0]))
    neg("BAD_IMAGE illegal item id (0x4000) in a pocket", R["BAD_IMAGE"], pre=(p_illegal, pre[1], pre[2]))
    neg("BAD_IMAGE wallet above the maximum", R["BAD_IMAGE"], pre=(pre[0], pre[1], 100000))
    neg("BAD_IMAGE item_conditions bit 31", R["BAD_IMAGE"], pre=(pre[0], 0x80000000, pre[2]))
    occupied_pre = tuple(0x2801 if i == sl else p for i, p in enumerate(pre[0]))
    neg("PRECOND pickup POCKET into an occupied slot (in the pre-image)", R["PRECOND"], pre=(occupied_pre, pre[1], pre[2]))
    neg("PRECOND pickup WALLET of a non-money item", R["PRECOND"], dest=D_WALLET, slot=S_WALLET)
    neg("PRECOND item does not match the reservation (granted item echo)", R["PRECOND"], item=0x2801)
    ck("BAD_IMAGE / PRECOND: no FIELD_UPDATE for the tile in all of the above", len(run.field_updates(b, tile_v, mark_b)) == 0)

    # --- drop / bury preconditions: need a DROP reservation on an EMPTY tile and a BURY reservation on a hole ---
    item_d = state["prov"].granted_item
    s_, r_pick = run.commit(a, KP, state["rid"], D_POCKET, sl, item_d)
    run.tx_ok("PRECOND setup: a pickup empties the tile", r_pick, APPLIED, R["NONE"])
    sd = sl
    rid_d = run.fresh_rid()
    provd = a.drop(sd, item_d, tile_v[0], tile_v[1], rid_d, timeout=1.0, claim_at=tile_v, auto_confirm=False)
    ck("PRECOND setup: DROP reservation on the now-empty tile", provd is not None and provd.accepted)
    if provd is not None and provd.accepted:
        pre_now = a.txn_pre_image()
        empty_pre = tuple(0 if i == sd else p for i, p in enumerate(pre_now[0]))
        s_, r_ = run.commit(a, KD, rid_d, D_NONE, sd, item_d, pre=(empty_pre, pre_now[1], pre_now[2]))
        run.tx_ok("PRECOND drop with the slot EMPTY in the pre-image (does not hold the claimed item)", r_, REJECTED, R["PRECOND"])
        rid_d = run.fresh_rid()
        provd = a.drop(sd, item_d, tile_v[0], tile_v[1], rid_d, timeout=1.0, claim_at=tile_v, auto_confirm=False)
        ck("PRECOND setup: DROP reservation again after the reject released it", provd is not None and provd.accepted)
        s_, r_ = run.commit(a, KD, rid_d, D_NONE, (sd + 1) % 15, item_d)
        run.tx_ok("PRECOND drop echo: tag.slot differs from the DROP_REQUEST's slot", r_, REJECTED, R["PRECOND"])
        rid_d = run.fresh_rid()
        provd = a.drop(sd, item_d, tile_v[0], tile_v[1], rid_d, timeout=1.0, claim_at=tile_v, auto_confirm=False)
        s_, r_ = run.commit(a, KD, rid_d, D_NONE, sd, item_d)
        run.tx_ok("PRECOND cleanup: the correct drop applies (tile restored)", r_, APPLIED, R["NONE"])
    mark_b = b.inbox.mark()
    rid_b = run.fresh_rid()
    provb = a.bury(5, ITM_ROD, BURY_TILE_2[0], BURY_TILE_2[1], rid_b, timeout=1.0, auto_confirm=False)
    ck("PRECOND setup: BURY reservation on a HOLE tile", provb is not None and provb.accepted)
    if provb is not None and provb.accepted:
        pre_now = a.txn_pre_image()
        s_, r_ = run.commit(a, KB, rid_b, D_NONE, 5, ITM_ROD, pre=(pre_now[0], pre_now[1], pre_now[2]))  # slot 5 is EMPTY in the pre-image
        run.tx_ok("PRECOND bury with the slot empty in the pre-image", r_, REJECTED, R["PRECOND"])
        rid_b = run.fresh_rid()
        provb = a.bury(5, ITM_ROD, BURY_TILE_2[0], BURY_TILE_2[1], rid_b, timeout=1.0, auto_confirm=False)
        s_, r_ = run.commit(a, KB, rid_b, D_NONE, 6, ITM_ROD)  # tag.slot 6 differs from the request's slot 5
        run.tx_ok("PRECOND bury echo: tag.slot differs from the BURY_REQUEST's slot", r_, REJECTED, R["PRECOND"])
        L.pump_sleep(0.3)
        ck("PRECOND bury rejects mutated nothing: the (40,106) hole tile is untouched", run.tile_of(b, BURY_TILE_2) is not None
           and run.tile_of(b, BURY_TILE_2) != ITM_ROD and len(run.field_updates(b, BURY_TILE_2, mark_b)) == 0)

    # --- BAD_SHAPE matrix: answered BEFORE any journal / reservation work, nothing is consumed ---
    tv2 = run.reserve(a, tile=tile_v)
    rid_s, prov_s = tv2[1], tv2[2]
    L.pump_sleep(0.3)
    mark_b = b.inbox.mark()  # the PRECOND setup above legitimately changed this tile twice; count from here
    pre_now = a.txn_pre_image()
    sl = first_free(pre_now[0])
    rev_now = a.rec_last[2]
    item = prov_s.granted_item
    shapes = [("flags (EXCHANGE bit) set", KP, D_POCKET, sl, dict(flags=1)), ("aux_cond != 0", KP, D_POCKET, sl, dict(aux_cond=1)),
              ("aux_item != 0", KP, D_POCKET, sl, dict(aux_item=5)), ("header _rsv0 != 0", KP, D_POCKET, sl, dict(rsv0=1)),
              ("tag _rsv0 != 0", KP, D_POCKET, sl, dict(tag_rsv0=1)), ("nonce 0", KP, D_POCKET, sl, dict(nonce=0)),
              ("seq 0", KP, D_POCKET, sl, dict(seq=0)), ("pickup POCKET slot 15", KP, D_POCKET, 15, {}),
              ("pickup dest NONE", KP, D_NONE, sl, {}), ("pickup WALLET with slot != 0xFF", KP, D_WALLET, 3, {}),
              ("kind 0", 0, D_POCKET, sl, {}), ("kind 4 (reserved for X3)", 4, D_POCKET, sl, {}), ("kind 12 (kinds 8..11 are town-service kinds since the town-services / shop milestones)", 12, D_POCKET, sl, {}),
              ("drop with dest POCKET", KD, D_POCKET, sl, {})]
    # X1b: a structurally malformed COMMIT (BAD_SHAPE, or a wrong-size TXN_COMMIT) now also COUNTS toward the 3-violation close policy
    # of the record protocol. So the honest client `a` (which must keep its reservation) sends only TWO malformed messages in total, the
    # rest of the matrix goes out from a throw-away session `g` of a third resident, TWO per connection (a reconnect between the pairs).
    n0 = len(a.txn_results)
    d0 = shapes[0]
    a.send_txn_commit(0, 0, 0, 0, 0, raw=a.build_txn_commit_bytes(d0[1], rid_s, d0[2], d0[3], item, **d0[4]))
    ok_raw = a.build_txn_commit_bytes(KP, rid_s, D_POCKET, sl, item)
    L.pump_sleep(0.6)
    nres = len(a.txn_results)
    a.send_reliable(ok_raw[:-1])          # a wrong-size COMMIT (71 B): no answer, counts as the 2nd violation of `a`
    L.pump_sleep(0.6)
    bs_a = [g_ for _c, g_ in a.txn_results[n0:nres]]
    ck("BAD_SHAPE: the honest client's own malformed COMMIT (%s) is answered REJECTED(BAD_SHAPE) at the current rev" % d0[0],
       len(bs_a) == 1 and bs_a[0].outcome == REJECTED and bs_a[0].reason == R["BAD_SHAPE"] and bs_a[0].rev == rev_now)
    ck("BAD_SIZE: a 71-byte COMMIT gets NO answer and no mutation; `a` (2 violations so far) is still connected and the host alive",
       len(a.txn_results) == nres and a.is_connected() and host.alive())
    g = run.client("G", run.r3)
    g.connect_and_ready(quiet=True)
    rev_g = g.rec_last[2]
    rest = shapes[1:]
    bs = []
    for i in range(0, len(rest), 2):
        pair = rest[i:i + 2]
        n_g = len(g.txn_results)
        for desc, kind, dest, slot, kw in pair:
            g.send_txn_commit(0, 0, 0, 0, 0, raw=g.build_txn_commit_bytes(kind, rid_s, dest, slot, item, **kw))
        g.hub.wait_until(lambda: len(g.txn_results) >= n_g + len(pair), 2.5)
        bs += [g_ for _c, g_ in g.txn_results[n_g:]]
        if i + 2 < len(rest):
            g.disconnect()
            L.pump_sleep(0.5)
            g.reconnect_and_ready()
    bs_all = bs_a + bs
    ck(f"BAD_SHAPE: all {len(shapes)} malformed COMMITs are answered REJECTED(BAD_SHAPE) ({len(bs_all)} answers; the first from `a`, the rest "
       f"from a throw-away session, two per connection)",
       len(bs_all) == len(shapes) and all(x.outcome == REJECTED and x.reason == R["BAD_SHAPE"] for x in bs_all) and all(x.rev == rev_g for x in bs))
    ck("BAD_SHAPE: none of them closed a connection (each connection stayed at <= 2 violations)", g.is_connected() and a.is_connected())
    ck("BAD_SHAPE / wrong sizes mutated nothing: tile untouched, still no new FIELD_UPDATE for it",
       run.tile_of(b, tile_v) == item and len(run.field_updates(b, tile_v, mark_b)) == 0)
    # --- X1b violation policy (host side): the 3rd structurally malformed message of ONE connection closes it; semantic refusals never count ---
    off_v = len(host.log_text())
    g.disconnect()
    L.pump_sleep(0.5)
    g.reconnect_and_ready()
    for _ in range(3):  # NOT_PENDING x3 (valid shape, no reservation), then a seq jump and FENCED x2 (seq below the fence): honest-client refusals
        run.commit(g, KP, run.fresh_rid(), D_POCKET, first_free_pocket(g), item)
    g.txn_seq = 199
    run.commit(g, KP, run.fresh_rid(), D_POCKET, first_free_pocket(g), item)               # seq 200 -> NOT_PENDING (max_seq 200)
    for fs in (150, 151):
        run.commit(g, KP, run.fresh_rid(), D_POCKET, first_free_pocket(g), item, seq=fs)    # below the fence, not in the ring -> FENCED
    n_np = sum(1 for _c, x in g.txn_results if x.outcome == REJECTED and x.reason == R["NOT_PENDING"])
    n_fe = sum(1 for _c, x in g.txn_results if x.outcome == REJECTED and x.reason == R["FENCED"])
    ck("VIOL honest refusals never count: %d NOT_PENDING + %d FENCED answers on one connection, still connected, no violation line in the host log" % (n_np, n_fe),
       g.is_connected() and n_np >= 4 and n_fe >= 2 and "violation" not in run.log(off_v))
    g.send_reliable(ok_raw[:-1])
    g.send_reliable(ok_raw[:8])
    L.pump_sleep(0.6)
    probe_sent, probe_res = run.commit(g, KP, run.fresh_rid(), D_POCKET, first_free_pocket(g), item)
    ck("VIOL two wrong-size COMMITs (violations 2/3) leave the connection open: a valid-shape probe is still answered",
       g.is_connected() and probe_res is not None and probe_res.reason == R["NOT_PENDING"])
    off_c = len(host.log_text())
    g.send_reliable(ok_raw + b"\x00")   # the 3rd structurally malformed message of this connection
    closed = g.wait_disconnected(3.0)
    L.pump_sleep(0.3)
    ck("VIOL the 3rd malformed message closes the connection (host log: 'closed after 3 record protocol violations'), nothing mutated",
       closed and "closed after 3 record protocol violations" in run.log(off_c) and run.tile_of(b, tile_v) == item
       and len(run.field_updates(b, tile_v, mark_b)) == 0)
    g.reconnect_and_ready()
    off_s = len(host.log_text())
    for k in range(3):  # BAD_SHAPE x3 on one connection: answered each time (the RESULT precedes the violation), the 3rd closes the peer
        g.send_txn_commit(0, 0, 0, 0, 0, raw=g.build_txn_commit_bytes(KP, rid_s, D_POCKET, 15, item))
        L.pump_sleep(0.35)
    closed2 = g.wait_disconnected(3.0)
    ck("VIOL 3 BAD_SHAPE COMMITs on one connection close it too (same counter as the wrong-size ones and the record-chunk violations)",
       closed2 and "closed after 3 record protocol violations" in run.log(off_s))
    g.reconnect_and_ready()
    probe_sent, probe_res = run.commit(g, KP, run.fresh_rid(), D_POCKET, first_free_pocket(g), item)
    ck("VIOL an honest session after the close starts with a clean counter (a valid-shape probe is answered, connection open)",
       g.is_connected() and probe_res is not None and probe_res.reason == R["NOT_PENDING"])
    run.release(g)
    sent_good, r_good = run.commit(a, KP, rid_s, D_POCKET, sl, item)
    run.tx_ok("after the whole malformed matrix the SAME reservation still commits (shape errors consume nothing)", r_good, APPLIED, R["NONE"])
    ck("... and rev advanced by exactly one over everything above", r_good is not None and r_good.rev == rev_now + 1)
    drop_back(run, a, sl, item, tile_v)
    return True


def t9_fenced(run, a, b):
    ck = run.check
    tile_f, rid_f, prov_f = run.reserve(a)
    slf = first_free_pocket(a)
    a.txn_seq = 999
    sent_j, r_j = run.commit(a, KP, rid_f, D_POCKET, slf, prov_f.granted_item)  # seq 1000: executes
    run.tx_ok("T9 a seq jump (1000) executes (max_seq moves)", r_j, APPLIED, R["NONE"])
    drop_back(run, a, slf, prov_f.granted_item, tile_f)
    tg = run.reserve(a, tile=tile_f)
    rid_g, prov_g = tg[1], tg[2]
    raw_old = a.build_txn_commit_bytes(KP, rid_g, D_POCKET, slf, prov_g.granted_item, seq=500)
    s_, r_ = run.commit(a, KP, rid_g, 0, 0, 0, raw=raw_old)
    run.tx_ok("T9 seq 500 <= max_seq and never journaled -> FENCED (never executed)", r_, REJECTED, R["FENCED"])
    s_, r_ = run.commit(a, KP, rid_g, D_POCKET, slf, prov_g.granted_item)
    run.tx_ok("T9 ... and the reservation survived: the next seq commits", r_, APPLIED, R["NONE"])
    drop_back(run, a, slf, prov_g.granted_item, tile_f)
    old_raw = sent_j.raw
    run.release(a)
    a3 = run.client("A3", run.r1)
    a3.connect_and_ready(quiet=True)
    tile_h, rid_h, prov_h = run.reserve(a3)
    slh = first_free_pocket(a3)
    sent_h, r_h = run.commit(a3, KP, rid_h, D_POCKET, slh, prov_h.granted_item)
    run.tx_ok("T9 a NEW client process: its seq 1 executes (nonce adopted, the old one fenced)", r_h, APPLIED, R["NONE"])
    a3.send_txn_commit(KP, 0, 0, 0, 0, raw=old_raw)
    L.pump_sleep(0.6)
    fenced = [g for _c, g in a3.txn_results if g.txn_nonce != a3.txn_nonce]
    ck("T9 the OLD process' byte-identical resend is REJECTED(FENCED), not replayed or executed",
       fenced and fenced[-1].outcome == REJECTED and fenced[-1].reason == R["FENCED"])
    ck("T9 host log: the nonce roll was logged", "new client nonce %d" % a3.txn_nonce in run.log())
    drop_back(run, a3, slh, prov_h.granted_item, tile_h)
    # wraparound at the top of the seq space, then a nonce re-roll
    a3.txn_seq = 0xFFFFFFFD
    tw = run.reserve(a3, tile=tile_h)
    sent_w1, r_w1 = run.commit(a3, KP, tw[1], D_POCKET, slh, tw[2].granted_item)  # 0xFFFFFFFE
    run.tx_ok("T9 seq 0xFFFFFFFE executes", r_w1, APPLIED, R["NONE"])
    prov, sent_w2, r_w2 = a3.txn_drop(slh, tw[2].granted_item, tile_h[0], tile_h[1], run.fresh_rid(), claim_at=tile_h)  # 0xFFFFFFFF
    ck("T9 seq 0xFFFFFFFF executes (the last legal seq)", r_w2 is not None and r_w2.outcome == APPLIED and r_w2.txn_seq == 0xFFFFFFFF)
    tw2 = run.reserve(a3, tile=tile_h)
    s_, r_ = run.commit(a3, KP, tw2[1], D_POCKET, slh, tw2[2].granted_item)  # the counter wraps to seq 0 -> BAD_SHAPE
    run.tx_ok("T9 a wrap to seq 0 -> BAD_SHAPE (the client must RE-ROLL its nonce, never reuse seq 0)", r_, REJECTED, R["BAD_SHAPE"])
    old_nonce = a3.txn_nonce
    a3.new_process(keep_record=True)
    s_, r_ = run.commit(a3, KP, tw2[1], D_POCKET, slh, tw2[2].granted_item)  # new nonce, seq 1, same reservation
    run.tx_ok("T9 after the nonce re-roll seq 1 executes", r_, APPLIED, R["NONE"])
    stale = a3.build_txn_commit_bytes(KP, tw2[1], D_POCKET, slh, tw2[2].granted_item, seq=0xFFFFFFFF, nonce=old_nonce)
    a3.send_txn_commit(KP, 0, 0, 0, 0, raw=stale)
    L.pump_sleep(0.6)
    fz = [g for _c, g in a3.txn_results if g.txn_nonce == old_nonce and g.txn_seq == 0xFFFFFFFF and g.outcome == REJECTED]
    ck("T9 the old (rolled) nonce is FENCED even for its highest seq", fz and fz[-1].reason == R["FENCED"])
    drop_back(run, a3, slh, tw2[2].granted_item, tile_h)
    run.release(a3)
    a4 = run.client("A4", run.r1)
    a4.connect_and_ready(quiet=True)
    return a4


def t10_d3(run, a, b):
    ck = run.check
    host = run.host
    cur = a.rec_last
    L.pump_sleep(1.7)
    img = L.record_set_u32(a.rec_local, L.REC_OFF_BANK, 4242)
    x = a.upload_record(img, base=(cur[1], cur[2]))
    ack = a.wait_record_ack(xfer_id=x)
    ck("T10 an upload on the current base is APPLIED (rev + 1)", ack is not None and ack.status == L.PC_NETGAME_REC_ACK_APPLIED and ack.rev == cur[2] + 1)
    a.rec_local = img
    tile_10, rid_10, prov_10 = run.reserve(a)
    sl10 = first_free_pocket(a)
    s10, r10 = run.commit(a, KP, rid_10, D_POCKET, sl10, prov_10.granted_item)
    run.tx_ok("T10 an upload fully applied, THEN a COMMIT on the new base", r10, APPLIED, R["NONE"])
    ck("T10 rev == the pre-upload rev + 2 (one for the upload, one for the txn)", r10 is not None and r10.rev == cur[2] + 2)
    ck("T10 the txn kept the uploaded bank field (it only changes pockets/conds/wallet): cdig covers the merged image",
       r10 is not None and r10.cdig == cown_digest(a.rec_local))
    L.pump_sleep(1.7)
    stale_img = L.record_set_u32(a.rec_local, L.REC_OFF_BANK, 777)
    x = a.upload_record(stale_img, base=(cur[1], cur[2] + 1))  # the PRE-txn base
    ack = a.wait_record_ack(xfer_id=x)
    ck("T10 an upload built on the PRE-txn base is STALE_BASE (and reports the txn's rev)",
       ack is not None and ack.status == L.PC_NETGAME_REC_ACK_STALE_BASE and r10 is not None and ack.rev == r10.rev)
    drop_back(run, a, sl10, prov_10.granted_item, tile_10)
    # a stale upload queues a push; the COMMIT right behind it restarts that push at rev_after
    restarted = False
    for attempt in range(3):
        L.pump_sleep(2.3)
        tile_p, rid_p, prov_p = run.reserve(a)
        curp = a.rec_last
        slp = first_free_pocket(a)
        off = len(host.log_text())
        np0 = len(a.rec_pushes)
        a.upload_record(L.record_set_u32(a.rec_local, L.REC_OFF_BANK, 999 + attempt), base=(curp[1], curp[2] - 1))
        sp, rp = run.commit(a, KP, rid_p, D_POCKET, slp, prov_p.granted_item)
        if rp is None or rp.outcome != APPLIED:
            ck("T10 push-restart attempt applied", False)
            break
        L.pump_sleep(1.0)
        lg = run.log(off)
        m_app = re.search(r"TXN APPLIED kind=PICKUP request=%d " % rid_p, lg)
        queued = re.findall(r"push FULL queued \(resident \d+ epoch \d+ rev (\d+) xfer (\d+)", lg)
        restarted = bool(m_app) and bool(queued) and int(queued[-1][0]) == rp.rev and len(queued) >= 2
        if restarted:
            last = a.rec_pushes[-1] if len(a.rec_pushes) > np0 else None
            ck("T10 the LAST push the client completed carries rev_after and the post-image (never the stale pre-txn pockets)",
               last is not None and last["rev"] == rp.rev and L.record_inventory(last["data"])[0] == tuple(rp.post_pockets))
        drop_back(run, a, slp, prov_p.granted_item, tile_p)
        if restarted:
            break
    ck("T10 a push in flight (queued by a stale upload) is RESTARTED by the COMMIT at rev_after "
       "(host log: a 'push FULL queued ... rev <rev_after>' after the APPLIED)", restarted)


def t_mixed(run, a, b):
    ck = run.check
    host = run.host
    L.pump_sleep(0.5)
    mark_b = b.inbox.mark()
    tile_m, rid_m, prov_m = run.reserve(a)
    slm = first_free_pocket(a)
    off = len(host.log_text())
    sent_m, r_m = run.commit(a, KP, rid_m, D_POCKET, slm, prov_m.granted_item)
    run.tx_ok("MIX TXN commit first", r_m, APPLIED, R["NONE"])
    a.confirm(KP, rid_m, L.CONFIRM_OUTCOME_COMMIT, L.CONFIRM_REASON_NONE)  # then a LEGACY commit for the same request
    L.pump_sleep(0.6)
    ck("MIX a legacy CONFIRM(COMMIT) after a TXN APPLIED is ignored (logged), one committed line, one FIELD_UPDATE",
       ("legacy CONFIRM(COMMIT) for PICKUP request %d ignored: already committed via TXN_COMMIT" % rid_m) in run.log(off)
       and run.n_log(r"request %d committed tile" % rid_m, off) == 1 and len(run.field_updates(b, tile_m, mark_b)) == 1)
    drop_back(run, a, slm, prov_m.granted_item, tile_m)
    # X1b: PC_NETGAME_TXN_RETIRE_LEGACY_COMMIT == 1: a legacy CONFIRM(COMMIT) is RETIRED -- logged, the reservation released, NOTHING
    # mutated. (The older "legacy commit processed as before" behaviour only exists while the constant is 0: that branch is now
    # source-audited in test_txn_src.py, it cannot run at runtime.) Then a TXN_COMMIT for the released request is NOT_PENDING.
    tile_l, rid_l, prov_l = run.reserve(a)
    off = len(host.log_text())
    mark_b = b.inbox.mark()
    rev_before = a.rec_last[2]
    a.confirm(KP, rid_l, L.CONFIRM_OUTCOME_COMMIT, L.CONFIRM_REASON_NONE)
    L.pump_sleep(0.7)
    lg_l = run.log(off)
    ck("MIX legacy CONFIRM(COMMIT) is RETIRED: logged, the reservation released, the tile keeps its item (no FIELD_UPDATE, no committed line), "
       "the mirror is untouched",
       "CONFIRM(COMMIT) is retired in v8 -- use TXN_COMMIT" in lg_l and ("request %d reservation released" % rid_l) in lg_l
       and "legacy CONFIRM(COMMIT) is retired" in lg_l and run.tile_of(b, tile_l) == prov_l.granted_item
       and len(run.field_updates(b, tile_l, mark_b)) == 0 and run.n_log(r"request %d committed tile" % rid_l, off) == 0)
    sent_l, r_l = run.commit(a, KP, rid_l, D_POCKET, first_free_pocket(a), prov_l.granted_item)
    run.tx_ok("MIX a TXN_COMMIT for a request whose reservation the retired legacy COMMIT released", r_l, REJECTED, R["NOT_PENDING"])
    ck("MIX ... changed nothing: rev unchanged, no TXN APPLIED for it, no committed line, the tile still holds the item",
       r_l is not None and r_l.rev == rev_before and run.applied_lines(rid_l, off) == 0 and run.n_log(r"request %d committed tile" % rid_l, off) == 0
       and run.tile_of(b, tile_l) == prov_l.granted_item)
    L.pump_sleep(0.3)
    # the tile is free for another reservation again (the retired COMMIT released it): reserve + commit it through TXN properly
    tile_l2, rid_l2, prov_l2 = run.reserve(a)
    sl2 = first_free_pocket(a)
    sent_l2, r_l2 = run.commit(a, KP, rid_l2, D_POCKET, sl2, prov_l2.granted_item)
    run.tx_ok("MIX a fresh pickup after the retired legacy COMMIT commits through TXN", r_l2, APPLIED, R["NONE"])
    drop_back(run, a, sl2, prov_l2.granted_item, tile_l2)
    # TXN rejected (PRECOND) releases the reservation, then a legacy COMMIT for it is ignored
    tile_q, rid_q, prov_q = run.reserve(a)
    off = len(host.log_text())
    s_, r_q = run.commit(a, KP, rid_q, D_POCKET, first_free_pocket(a), 0x2801)  # item mismatch -> PRECOND (releases)
    run.tx_ok("MIX TXN REJECTED(PRECOND) releases the reservation", r_q, REJECTED, R["PRECOND"])
    mark_b = b.inbox.mark()
    a.confirm(KP, rid_q, L.CONFIRM_OUTCOME_COMMIT, L.CONFIRM_REASON_NONE)
    L.pump_sleep(0.6)
    ck("MIX a legacy COMMIT after the TXN reject is ignored: the tile keeps its item, no committed line",
       run.tile_of(b, tile_q) == prov_q.granted_item and len(run.field_updates(b, tile_q, mark_b)) == 0 and run.n_log(r"request %d committed tile" % rid_q, off) == 0)


def t_notsynced(run, a, b):
    """X1 review L1: a NOT_SYNCED refusal (answered BEFORE the journal step) must RELEASE the still-PENDING reservation, so a late copy of that COMMIT
    can never execute. A client that sent IDENTITY but never a RECORD_HELLO (inside the 5 s HELLO deadline) is READY and bound but not SYNCED."""
    ck = run.check
    host = run.host
    L.pump_sleep(0.3)
    ns = run.client("NS", run.r3, record_hello=False, record_wait=False)
    ns.connect_and_ready(quiet=True)
    t = run.reserve(ns)
    ck("L1 setup: a not-yet-SYNCED client holds a PENDING reservation (the host's request path does not need SYNCED)", t is not None)
    if t is None:
        run.release(ns)
        return
    tile, rid, prov = t
    off = len(host.log_text())
    mark_b = b.inbox.mark()
    sent, r = run.commit(ns, KP, rid, D_POCKET, 0, prov.granted_item, pre=((0,) * 15, 0, 0), base=(0, 0))
    run.tx_ok("L1 TXN_COMMIT from a READY-but-not-SYNCED peer", r, REJECTED, R["NOT_SYNCED"])
    L.pump_sleep(0.3)
    ck("L1 the refusal RELEASED the reservation (host log 'reservation released' for the request) and mutated nothing",
       ("request %d reservation released" % rid) in run.log(off) and run.tile_of(b, tile) == prov.granted_item
       and len(run.field_updates(b, tile, mark_b)) == 0)
    sent2, r2 = run.commit(ns, KP, rid, D_POCKET, 0, prov.granted_item, pre=((0,) * 15, 0, 0), base=(0, 0))
    run.tx_ok("L1 a second copy of the COMMIT (a late duplicate) is refused again, never executed", r2, REJECTED, R["NOT_SYNCED"])
    ck("L1 ... and still no FIELD_UPDATE / committed line for it", len(run.field_updates(b, tile, mark_b)) == 0
       and run.n_log(r"request %d committed tile" % rid, off) == 0)
    run.release(ns)


def t11_peers(run, a, b):
    ck = run.check
    host = run.host
    L.pump_sleep(0.5)
    rev_chk = a.rec_last[2]
    raw_probe = a.build_txn_commit_bytes(KP, 1, D_POCKET, 0, 0x2800, pre=((0,) * 15, 0, 0), base=(a.rec_last[1], a.rec_last[2]))
    hs = L.FakeClient("HS", run.ip, run.port, context_flags=None, wait_snapshot=False, record_hello=False)
    hs.connect(timeout=3.0)
    off = len(host.log_text())
    hs.send_reliable(raw_probe)
    L.pump_sleep(1.0)
    ck("T11 a HANDSHAKE peer (no IDENTITY) gets NO answer to a TXN_COMMIT and the host logs nothing about it",
       not hs.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type == L.PC_NETGAME_MSG_TXN_RESULT)
       and "[NET][TXN]" not in run.log(off) and hs.is_connected())
    hs.close()
    holder = run.ready("H", run.r3)
    park = L.FakeClient("PK", run.ip, run.port, player=L.resident_player(run.r3), context_flags=None, wait_snapshot=False, record_hello=False)
    park.connect(timeout=3.0)
    park.send_identity(town=L.resolve_host_town(run.ip, run.port), player=L.resident_player(run.r3))
    L.pump_sleep(0.5)
    off = len(host.log_text())
    hrev = holder.rec_last[2]
    park.send_reliable(raw_probe)
    L.pump_sleep(1.0)
    ck("T11 a PARKED peer (claim on a live resident) gets no TXN_RESULT and causes no TXN activity",
       not park.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_TXN_RESULT, L.PC_NETGAME_MSG_IDENTITY_ACK))
       and "[NET][TXN]" not in run.log(off) and holder.rec_last[2] == hrev)
    park.close()
    own = L.FakeClient("OWN", run.ip, run.port, player=L.resident_player(run.host_slot), context_flags=None, wait_snapshot=False, record_hello=False)
    own.connect(timeout=3.0)
    off = len(host.log_text())
    try:
        own.send_identity(town=L.resolve_host_town(run.ip, run.port), player=L.resident_player(run.host_slot))
        L.pump_sleep(0.5)
        own.send_reliable(raw_probe)
        L.pump_sleep(0.8)
    except Exception:  # noqa: BLE001
        pass
    ck("T11 a peer claiming the HOST's own resident gets no TXN_RESULT and no TXN activity at all (the host's record is never written)",
       not own.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type == L.PC_NETGAME_MSG_TXN_RESULT) and "TXN APPLIED" not in run.log(off)
       and "[NET][TXN]" not in run.log(off))
    own.close()
    run.release(holder)
    ck("T11 nothing above changed A's mirror: A's rev still equals what A last saw", a.rec_last[2] == rev_chk)


def phase_main(run):
    ck = run.check
    host = run.host
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    sess, epoch, rev0 = a.rec_last
    ck("M setup: A and B READY and SYNCED, A holds the pushed mirror (rev >= 1)", rev0 >= 1 and b.rec_synced and a.rec_synced)
    if t1_success_paths(run, a, b) is None:
        return
    # ---------------- D3: a second session of A gets the mirror in PUSH_FULL ----------------
    final_local, final_last = a.rec_local, a.rec_last
    run.release(a)
    a2 = run.client("A2", run.r1)
    a2.connect_and_ready(quiet=True)
    push = a2.rec_pushes[-1] if a2.rec_pushes else None
    ck("D3 a new session of A is pushed the mirror: rev == the last RESULT rev, the record == the patched local image byte for byte "
       "(only pockets/conds/wallet differ from the previous push)",
       push is not None and push["rev"] == final_last[2] and push["data"] == final_local and L.record_inventory(push["data"]) == L.record_inventory(final_local))
    ck("D3 the new session is a new client process (new txn nonce)", a2.txn_nonce != a.txn_nonce)
    a = a2
    t8_duplicates(run, a, b)
    if not t_validation(run, a, b):
        return
    a = t9_fenced(run, a, b)
    t10_d3(run, a, b)
    t_mixed(run, a, b)
    t_notsynced(run, a, b)
    t11_peers(run, a, b)
    ck("M the host never logged an INTERNAL error nor crashed", "*** INTERNAL" not in run.log() and host.alive())
    ck("M a TEST-ONLY fault line never appears without --txn-fault", "[TEST-ONLY]" not in run.log())
    ck("M the host's own resident (slot %d) never appears in a TXN log line" % run.host_slot, not re.search(r"resident %d TXN" % run.host_slot, run.log()))
    run.release(a)
    run.release(b)


# ======================================================================================================================
# Fault phases
# ======================================================================================================================
def fault_setup(run):
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    return a, b


def phase_ignore1(run):
    ck = run.check
    ck("F-ignore1 startup log: FAULT INJECTION ENABLED mode=1 (loud, [TEST-ONLY])", "[NET][TXN][TEST-ONLY] FAULT INJECTION ENABLED mode=1 nth=1 count=1" in run.log())
    a, b = fault_setup(run)
    tile, rid, prov = run.reserve(a)
    mark_b = b.inbox.mark()
    slot = first_free_pocket(a)
    sent = a.send_txn_commit(KP, rid, D_POCKET, slot, prov.granted_item)
    first = a.wait_txn_result(sent.seq, 1.6)
    L.pump_sleep(0.3)
    ck("T2 the first COMMIT is lost (ignore_commit): no result, tile unchanged, nothing journaled",
       first is None and run.tile_of(b, tile) == prov.granted_item and ("ignore_commit: COMMIT request=%d" % rid) in run.log() and run.applied_lines(rid) == 0)
    a.resend_txn(sent)  # what the real client does after 2 s
    res = a.wait_txn_result(sent.seq, 2.5)
    run.tx_ok("T2 the byte-identical resend EXECUTES (it was never seen): APPLIED", res, APPLIED, R["NONE"])
    L.pump_sleep(0.4)
    ck("T2 executed exactly once: one TXN APPLIED line, one FIELD_UPDATE, the item once in the post-image",
       run.applied_lines(rid) == 1 and len(run.field_updates(b, tile, mark_b)) == 1 and res is not None
       and list(res.post_pockets).count(prov.granted_item) == 1)
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 2.0, nth=2)
    ck("T2 a further resend replays (REPLAYED), still one execution", rep is not None and rep.reason == R["REPLAYED"] and run.applied_lines(rid) == 1)


def phase_ignore12(run):
    ck = run.check
    a, b = fault_setup(run)
    tile, rid, prov = run.reserve(a)
    base = a.rec_last
    mark_b = b.inbox.mark()
    slot = first_free_pocket(a)
    sent = a.send_txn_commit(KP, rid, D_POCKET, slot, prov.granted_item)
    t0 = time.monotonic()
    n = 0
    res = None
    while time.monotonic() - t0 < 45.0:
        res = a.wait_txn_result(sent.seq, 2.0)
        if res is not None:
            break
        a.resend_txn(sent)
        n += 1
    run.tx_ok("T2b after 12 ignored COMMITs (resends every 2 s, > the 20 s reservation) the answer is REJECTED(EXPIRED)", res, REJECTED, R["EXPIRED"])
    ck(f"T2b it took {n} resends / {time.monotonic() - t0:.0f} s (>= 12 arrivals ignored, >= 20 s)", n >= 11 and time.monotonic() - t0 >= 20.0)
    ck("T2b host log: 12 ignore_commit firings, the reservation expired by the per-poll expiry",
       run.n_log(r"FAULT INJECTION FIRED mode=ignore_commit") == 12 and "reservation released: expired" in run.log())
    L.pump_sleep(0.4)
    ck("T2b tile and mirror unchanged: the tile still holds the item (no FIELD_UPDATE), the reject reports the unchanged rev",
       run.tile_of(b, tile) == prov.granted_item and len(run.field_updates(b, tile, mark_b)) == 0 and res is not None and res.rev == base[2])
    again = run.reserve(a, tile=tile)
    ck("T2b the tile is free for a new request after the expiry", again is not None)
    if again:
        s2, r2 = run.commit(a, KP, again[1], D_POCKET, slot, again[2].granted_item)
        run.tx_ok("T2b the next transaction applies", r2, APPLIED, R["NONE"])
        ck("T2b rev == base + 1 (the expired one never touched the mirror)", r2 is not None and r2.rev == base[2] + 1)


def phase_failworld(run):
    ck = run.check
    a, b = fault_setup(run)
    base = a.rec_last
    tile, rid, prov = run.reserve(a)
    mark_b = b.inbox.mark()
    slot = first_free_pocket(a)
    sent, res = run.commit(a, KP, rid, D_POCKET, slot, prov.granted_item)
    run.tx_ok("T3 fail_world: REJECTED(WORLD_CHANGED)", res, REJECTED, R["WORLD_CHANGED"])
    L.pump_sleep(0.3)
    ck("T3 world and mirror unchanged: tile intact, no FIELD_UPDATE, reject rev == base rev, no post-image",
       run.tile_of(b, tile) == prov.granted_item and len(run.field_updates(b, tile, mark_b)) == 0 and res is not None
       and res.rev == base[2] and all(p == 0 for p in res.post_pockets))
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 2.0, nth=2)
    ck("T3 a same-seq byte-identical resend replays the REJECTED outcome (host log: 'replay REJECTED'), one fault firing so far",
       rep is not None and rep.outcome == REJECTED and rep.reason == R["WORLD_CHANGED"] and run.replay_lines(rid, "REJECTED") == 1
       and run.n_log(r"FAULT INJECTION FIRED mode=fail_world") == 1)
    # the 2nd firing: a bury. RESULT first, then the BURY_RESULT(accepted=0) reconcile echo
    pre_len = len(a.result_log)
    rid_b = run.fresh_rid()
    prov_b, sb, rbb = a.txn_bury(3, ITM_ROD, GENERIC_BURY_TILE[0], GENERIC_BURY_TILE[1], rid_b)
    run.tx_ok("T3 bury under fail_world: REJECTED(WORLD_CHANGED)", rbb, REJECTED, R["WORLD_CHANGED"])
    L.pump_sleep(0.5)
    bury_rejects = [g for t, r_, g, _c in a.result_log[pre_len:] if t == L.PC_NETGAME_MSG_BURY_RESULT and r_ == rid_b and not g.accepted]
    ck("T3 bury: the BURY_RESULT(accepted=0) reconcile echo follows the TXN_RESULT (RECONCILE_VALID)",
       len(bury_rejects) == 1 and bury_rejects[0].flags & L.PC_NETGAME_BURY_FLAG_RECONCILE_VALID)
    ck("T3 bury: the hole tile is untouched", run.tile_of(b, GENERIC_BURY_TILE) is not None and run.tile_of(b, GENERIC_BURY_TILE) != ITM_ROD)
    # fault count exhausted: the tile is FREE for another client and its transaction applies
    again = run.reserve(b, tile=tile)
    ck("T3 the tile is FREE for another client (the failure released the reservation)", again is not None)
    if again:
        sb2, rb_ = run.commit(b, KP, again[1], D_POCKET, first_free_pocket(b), again[2].granted_item)
        run.tx_ok("T3 the other client's transaction on that tile applies (fault count exhausted)", rb_, APPLIED, R["NONE"])
        L.pump_sleep(0.4)
        ck("T3 the tile was changed exactly once overall", run.tile_of(b, tile) == L.EMPTY_NO and len(run.field_updates(b, tile, mark_b)) == 1)


def phase_expire(run):
    ck = run.check
    a, b = fault_setup(run)
    base = a.rec_last
    tile, rid, prov = run.reserve(a)
    mark_b = b.inbox.mark()
    slot = first_free_pocket(a)
    sent, res = run.commit(a, KP, rid, D_POCKET, slot, prov.granted_item)
    run.tx_ok("T4 expire fault: the COMMIT gets an explicit REJECTED(EXPIRED) (the legacy path answered NOTHING: L-2)", res, REJECTED, R["EXPIRED"])
    L.pump_sleep(0.3)
    ck("T4 mirror unchanged (reject rev == base rev), tile intact, host logged the TEST-ONLY expiry",
       res is not None and res.rev == base[2] and run.tile_of(b, tile) == prov.granted_item and len(run.field_updates(b, tile, mark_b)) == 0
       and "expired (TEST-ONLY injected)" in run.log())
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 2.0, nth=2)
    ck("T4 the resend replays REJECTED(EXPIRED)", rep is not None and rep.reason == R["EXPIRED"] and run.replay_lines(rid, "REJECTED") == 1)
    again = run.reserve(a, tile=tile)
    ck("T4 the tile is reservable again", again is not None)
    if again:
        s2, r2 = run.commit(a, KP, again[1], D_POCKET, slot, again[2].granted_item)
        run.tx_ok("T4 a fresh request applies afterwards", r2, APPLIED, R["NONE"])
        ck("T4 rev == base + 1", r2 is not None and r2.rev == base[2] + 1)


def phase_drop_result(run):
    ck = run.check
    a, b = fault_setup(run)
    base = a.rec_last
    tile, rid, prov = run.reserve(a)
    mark_b = b.inbox.mark()
    pre = a.txn_pre_image()
    slot = first_free(pre[0])
    sent = a.send_txn_commit(KP, rid, D_POCKET, slot, prov.granted_item)
    first = a.wait_txn_result(sent.seq, 1.6)
    L.pump_sleep(0.3)
    ck("T6 the APPLIED RESULT is lost (drop_result) but the host DID commit: no result at A, the tile is cleared at B",
       first is None and run.tile_of(b, tile) == L.EMPTY_NO and run.applied_lines(rid) == 1)
    a.resend_txn(sent)
    ck("T6 resend 1: the replay is dropped too (fault count 2)", a.wait_txn_result(sent.seq, 1.2) is None)
    a.resend_txn(sent)
    res = a.wait_txn_result(sent.seq, 2.5)
    exp = tuple(prov.granted_item if i == slot else p for i, p in enumerate(pre[0]))
    ck("T6 resend 2 gets APPLIED/REPLAYED with the same post-image, rev == base + 1",
       res is not None and res.outcome == APPLIED and res.reason == R["REPLAYED"] and tuple(res.post_pockets) == exp and res.rev == base[2] + 1)
    L.pump_sleep(0.3)
    ck("T6 the host APPLIED count for the request is exactly 1 (two replays), one FIELD_UPDATE, two drop_result firings",
       run.applied_lines(rid) == 1 and run.replay_lines(rid) == 2 and len(run.field_updates(b, tile, mark_b)) == 1
       and run.n_log(r"FAULT INJECTION FIRED mode=drop_result") == 2)


def phase_kill(run):
    ck = run.check
    host = run.host
    a, b = fault_setup(run)
    base = a.rec_last
    tile, rid, prov = run.reserve(a)
    mark_b = b.inbox.mark()
    pre = a.txn_pre_image()
    slot = first_free(pre[0])
    sent = a.send_txn_commit(KP, rid, D_POCKET, slot, prov.granted_item)
    closed = wait_closed(a, 4.0)
    L.pump_sleep(0.4)
    ck("T5 kill_peer_after_commit: the host dropped A after committing and sent NO result (A was disconnected)",
       closed and not a.txn_results_for(sent.seq) and "kill_peer_after_commit: peer" in run.log() and run.applied_lines(rid) == 0)
    ck("T5 the world changed exactly once (observer sees the tile EMPTY via one FIELD_UPDATE) and the commit logged '[TXN]'",
       run.tile_of(b, tile) == L.EMPTY_NO and len(run.field_updates(b, tile, mark_b)) == 1 and run.n_log(r"request %d committed tile .* \[TXN\]" % rid) == 1)
    L.pump_sleep(0.8)
    exp = tuple(prov.granted_item if i == slot else p for i, p in enumerate(pre[0]))
    # T7: the same nonce (same object) reconnects and resends the OLD COMMIT bytes
    a.reconnect_and_ready(timeout=8.0)
    pushed = a.rec_pushes[-1] if a.rec_pushes else None
    ck("T5/T7 the same-nonce reconnect is pushed the mirror: it already HAS the effect (item in the slot), rev == base + 1",
       pushed is not None and pushed["rev"] == base[2] + 1 and L.record_inventory(pushed["data"])[0] == exp)
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.3)
    ck("T7 the OLD COMMIT resent after a same-nonce reconnect replays APPLIED/REPLAYED (the journal survives the peer reset), NO second execution",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and tuple(rep.post_pockets) == exp
       and run.n_log(r"request %d committed tile" % rid) == 1 and len(run.field_updates(b, tile, mark_b)) == 1)
    # T5: a NEW process rejoins and also gets the effect
    run.release(a)
    a2 = run.client("A2", run.r1)
    a2.connect_and_ready(quiet=True)
    p2 = a2.rec_pushes[-1] if a2.rec_pushes else None
    ck("T5 a NEW-PROCESS rejoin: the pushed record has the effect, the tile stays changed exactly once",
       p2 is not None and L.record_inventory(p2["data"])[0] == exp and run.tile_of(b, tile) == L.EMPTY_NO and len(run.field_updates(b, tile, mark_b)) == 1)
    ck("T5 the host survived the dropped peer", host.alive())


PHASES = [
    ("M", [], phase_main),
    ("F-ignore1", ["--txn-fault=ignore_commit:1:1"], phase_ignore1),
    ("F-ignore12", ["--txn-fault=ignore_commit:1:12"], phase_ignore12),
    ("F-failworld", ["--txn-fault=fail_world:1:2"], phase_failworld),
    ("F-expire", ["--txn-fault=expire:1:1"], phase_expire),
    ("F-drop", ["--txn-fault=drop_result:1:2"], phase_drop_result),
    ("F-kill", ["--txn-fault=kill_peer_after_commit:1:1"], phase_kill),
]


def cli_checks(results):
    """--txn-fault is HOST-only and validated: a client / single-player launch and a bad spec exit 2 with a message, no game boot."""
    import subprocess
    exe = os.path.join(L.GAME_BIN_DIR, L.GAME_EXE_NAME)
    L.require_launchable_bin_dir(L.GAME_BIN_DIR)

    def run(argv):
        p = subprocess.run([exe] + argv, cwd=L.GAME_BIN_DIR, capture_output=True, timeout=30)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")

    rc, out = run(["--txn-fault=drop_result"])
    L.check("CLI --txn-fault without --host (single-player) is REFUSED: exit 2 and a message, no game start", rc == 2 and "HOST-only" in out, results)
    rc, out = run(["--connect", "127.0.0.1:1", "--txn-fault=expire"])
    L.check("CLI --txn-fault with --connect (client role) is REFUSED: exit 2", rc == 2 and "HOST-only" in out, results)
    rc, out = run(["--host", "10699", "--txn-fault=nonsense"])
    L.check("CLI an unknown --txn-fault mode is REFUSED: exit 2 and names the valid modes", rc == 2 and "bad --txn-fault spec" in out and "ignore_commit" in out, results)
    rc, out = run(["--host", "10699", "--txn-fault=expire:0"])
    L.check("CLI N < 1 is REFUSED: exit 2", rc == 2, results)
    rc, out = run(["--help"])
    L.check("CLI --help documents --txn-fault (HOST-only TEST hook, modes, N/K)", rc == 0 and "--txn-fault=MODE[:N[:K]]" in out and "HOST-only" in out
            and "kill_peer_after_commit" in out, results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10600)
    ap.add_argument("--only", default="", help="comma list of phase names (M, F-ignore1, ..., CLI)")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    only = [x for x in args.only.split(",") if x]
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "txn_protocol_save_snapshot_%d" % os.getpid())
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
        if not only or "CLI" in only:
            cli_checks(results)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
