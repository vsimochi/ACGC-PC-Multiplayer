#!/usr/bin/env python3
"""test_mail2_protocol.py - Mail milestone 2 (HOST half): the HOST-HELD house mailbox reaches its OWNING client (MAILBOX_LETTER, id 56), the client takes
a letter through the host-authoritative MAIL_TAKE transaction (TXN_COMMIT kind 13, reasons 26 NO_SUCH_LETTER / 27 MAIL_CHANGED, 24 reused for "no free
mail slot") and the HOST generates villager replies for every connected resident (M2r).

PROTOCOL test against REAL host processes (`AnimalCrossing.exe --host --bootstrap-resident 0 [hooks]`, started and stopped by this script from the TEST
COPY named by NET_SPIKE_GAME_BIN = pc\\build64\\bin_fixture4; host = resident 0, clients = residents 1..3, at most 3 simultaneous clients, ports 11300+).
`require_test_bin_dir()` refuses the live dirs; the fixture save dir is SNAPSHOTTED at start and RESTORED after every host phase. Clients are scripted
FakeClients (net_spike_lib: MAILBOX_LETTER collector / mbox_state, txn_mail_take, canonical-BE letter bytes and the 24-bit letter hash); the host's C
code runs for real. Letters get into the host mailboxes through the host's TEST-ONLY hooks: --mail-test-seed-mailbox (a simulated postman delivery
into a resident's HOUSE mailbox at world-ready) and the REAL vanilla delivery of --mail-test-force-delivery for letters sent with MAIL_SEND. What the
host did is proven from the received messages and the host's own loud log lines. The real client half is covered by test_mail2_real_client.py (one
real client takes a host letter) and the source audit test_mail2_src.py (the overlay / actor seams are source audited only).

Phases (one host process each):
  T       mailbox sync at READY for the OWNER only (10 slots, digest / seq / flags, no foreign mailbox), no resend of unchanged slots, a delta when the
          host mailbox changes (MAIL_SEND + forced delivery), MAIL_TAKE success (mirror mail[dst] == the letter, cleared indication, new session consistent,
          no duplicate), replay / CONFLICT, NO_SUCH_LETTER, MAIL_CHANGED (+ re-push), BAD_IMAGE, MAILBOX_FULL (dst occupied / no free slot), BAD_SHAPE,
          STALE_BASE for a pre-take upload, owner isolation (a take of another resident's letter), HANDSHAKE / parked / host-own peers ignored,
          concurrent takes of the same slot (exactly one), a delivery into the freed slot (old hash -> MAIL_CHANGED, new hash -> APPLIED)
  G       a letter whose gift is not an item that can exist in a pocket (BAD_IMAGE, stays in the mailbox)
  R       M2r: the host generates the owed villager reply for the connected resident only, once, and it reaches the owner's mailbox
  F-drop  drop_result:1:1   the APPLIED RESULT is lost, the take happened once; the byte-identical resend replays; the cleared slot reached the client
  F-kill  kill_peer_after_commit:1:1   the peer is dropped after the commit; same-nonce reconnect: pushed mirror holds the letter, the mailbox shows it
          gone; replay; NEW-process rejoin the same, exactly one copy

Tier: PROTOCOL TESTED (real host binary, scripted clients). Usage: python test_mail2_protocol.py [--port 11300] [--only T,G,R,F-drop,F-kill]
"""
import argparse
import os
import re
import shutil
import struct
import sys

import net_spike_lib as L
import test_txn_protocol as TP
import test_mail_protocol as MP
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE, D_POCKET

K_TAKE = L.PC_NETGAME_TXN_KIND_MAIL_TAKE
APPLE = 0x2800
BAD_GIFT = 0x0123
SEED_RE = re.compile(r"--mail-test-seed-mailbox: resident (\d+) house (\d+) mailbox\[(\d+)\] letter hash=0x([0-9A-F]{8}) present=0x([0-9A-F]{4})")
PUSH_RE = re.compile(r"\[NET\]\[MAIL\] host: pushed mailbox\[(\d+)\] seq (\d+) \((EMPTY|LETTER)\) of resident (\d+) to peer (\d+)")
TAKE_RE = re.compile(r"\[NET\]\[MAIL\] host: peer (\d+) resident (\d+) MAIL_TAKE mailbox\[(\d+)\] of house (-?\d+) -> mirror mail\[(\d+)\] present 0x([0-9A-F]{4}) "
                     r"letter_hash=0x([0-9A-F]{8}) mailbox_slot_cleared=1 committed \[TXN\]")
REMAIL_RE = re.compile(r"\[NET\]\[MAIL\] host: remail pass for resident (\d+) done: post office holds (\d+) letter\(s\) \(players (\d+)\)")
REMAIL_BEFORE_RE = re.compile(r"\[NET\]\[MAIL\] host: remail pass for resident (\d+) \(peer (\d+)\): post office holds (\d+) letter\(s\) before")
EMPTY = L.MAIL_FONT_UNUSED


def takes(run, off=0):
    """[(peer, resident, mbox idx, house, dst, present, hash)] of the host's MAIL_TAKE commit lines."""
    return [(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(6), 16), int(m.group(7), 16))
            for m in TAKE_RE.finditer(run.log(off))]


def pushes(run, resident=None, off=0):
    """[(idx, seq, kind, resident, peer)] of the host's 'pushed mailbox' lines."""
    out = [(int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4)), int(m.group(5))) for m in PUSH_RE.finditer(run.log(off))]
    return [p for p in out if resident is None or p[3] == resident]


def seeds(run):
    return [(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4), 16), int(m.group(5), 16)) for m in SEED_RE.finditer(run.log())]


def is_empty(g):
    return bool(g.flags & L.PC_NETGAME_MBOX_FLAG_EMPTY)


def empty_be(a):
    """The canonical bytes of an unused slot (what the host sends for an empty mailbox slot): taken from the pushed mirror of A (font 0xFF)."""
    d = a.rec_pushes[-1]["data"] if a.rec_pushes else a.rec_local
    return MP.empty_slot_bytes(d)


def state_ok(a, n=10):
    """The shadow of client `a`: {idx: msg}; True iff all n slots were received exactly once each at the first sync."""
    return sorted(a.mbox_state().keys()) == list(range(n))


def start_host(ip, port, tag, extra, results):
    host_slot = L.TEST_HOST_RESIDENT
    log_dir = os.path.dirname(os.path.abspath(__file__))
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot)] + list(extra),
                         log_path=os.path.join(log_dir, f"mail2_protocol_{tag}_host.log")).start()
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


def take(run, c, idx, dst=None, wait=3.0, **kw):
    """MAIL_TAKE of mailbox slot `idx` of client `c` into mail slot `dst` (default: its first free local slot): (rid, TxnSent, result, dst)."""
    if dst is None:
        dst = c.free_mail_slot()
    rid = run.fresh_rid()
    sent, r = c.txn_mail_take(idx, dst, rid=rid, timeout=wait, **kw)
    return rid, sent, r, dst


def mail_slots(rec):
    return [L.record_mail(rec, i) for i in range(L.REC_MAIL_COUNT)]


def used_letters(rec):
    return [m for m in mail_slots(rec) if L.mail_font(m) != EMPTY]


# ----------------------------------------------------------------------------------------------------------------------
# Phase T
# ----------------------------------------------------------------------------------------------------------------------
def t_sync(run, a, b):
    ck = run.check
    h0, r1, r2 = run.host_slot, run.r1, run.r2
    ma = a.wait_mbox_msgs(10, 8.0)
    ck("T1 the owner A received exactly 10 MAILBOX_LETTERs at READY (one per mailbox slot 0..9, nothing else)",
       len(ma) == 10 and state_ok(a) and len({g.mbox_idx for g, _i in ma}) == 10)
    gs = {g.mbox_idx: g for g, _i in ma}
    sd = [s for s in seeds(run) if s[0] == r1]
    house = sd[0][1] if sd else -1
    ck("T1 every message is well formed: 316 B spec, flags only bit 0, reserved bytes 0, seq > 0, digest == FNV-1a32 of the 298 letter bytes, one house for all "
       "(the house the host logged for resident %d: %d)" % (r1, house),
       all(g.flags in (0, 1) and g.rsv0 == 0 and g.rsv1 == 0 and g.seq > 0 and g.digest == L.fnv1a32(bytes(g.letter)) and g.house == house for g in gs.values())
       and house >= 0)
    used = sorted(i for i, g in gs.items() if not is_empty(g))
    ck("T1 exactly the 3 seeded letters are used (slots 0, 1, 2); the other 7 slots are 'empty' indications; used_count == 3 everywhere",
       used == [0, 1, 2] and all(g.used_count == 3 for g in gs.values()))
    ck("T1 the 3 seeded letters: font RECV, recipient = resident %d (type PLAYER), sender type NPC; slot 0 carries the gift APPLE, the others none" % r1,
       all(L.mail_font(bytes(gs[i].letter)) == L.MAIL_FONT_RECV and L.mail_recipient(bytes(gs[i].letter)) == (MP.pid_of(r1), L.MAIL_NAME_PLAYER)
           and L.mail_sender(bytes(gs[i].letter))[1] == L.MAIL_NAME_NPC for i in used)
       and L.mail_present(bytes(gs[0].letter)) == APPLE and L.mail_present(bytes(gs[1].letter)) == 0 and L.mail_present(bytes(gs[2].letter)) == 0)
    ck("T1 the wire letters ARE the letters the host seeded (their FNV hashes equal the hashes the host logged for the 3 seeded slots)",
       sorted((s[2], s[3]) for s in sd) == sorted((i, L.mail_hash32(bytes(gs[i].letter))) for i in used) and len(sd) == 3)
    eb = empty_be(a)
    ck("T1 an empty slot is the canonical cleared letter (byte-identical to an unused mail slot, font 0xFF) and all 7 empties share one digest",
       eb is not None and all(bytes(gs[i].letter) == eb for i in range(10) if is_empty(gs[i])) and len({gs[i].digest for i in range(10) if is_empty(gs[i])}) == 1)
    mb = b.wait_mbox_msgs(10, 8.0)
    gb = {g.mbox_idx: g for g, _i in mb}
    ck("T2 the OTHER resident B received its own 10 slots: all 'empty', used_count 0, none of A's digests (no foreign mailbox is ever sent)",
       len(mb) == 10 and state_ok(b) and all(is_empty(g) and g.used_count == 0 for g in gb.values())
       and not ({g.digest for g in gb.values()} & {gs[i].digest for i in used}))
    ck("T2 the host log: 10 pushes for resident %d (to ONE peer), 10 for resident %d (to another peer), none for any other resident" % (r1, r2),
       len(pushes(run, r1)) == 10 and len({p[4] for p in pushes(run, r1)}) == 1 and len(pushes(run, r2)) == 10 and len({p[4] for p in pushes(run, r2)}) == 1
       and {p[3] for p in pushes(run)} == {r1, r2} and {p[4] for p in pushes(run, r1)} != {p[4] for p in pushes(run, r2)})
    L.pump_sleep(2.6)
    ck("T3 an UNCHANGED mailbox is not resent: after 2.6 s A still holds exactly 10 messages and B exactly 10", len(a.mbox_msgs()) == 10 and len(b.mbox_msgs()) == 10)
    return gs


def t_delta(run, a, b, gs):
    ck = run.check
    h0, r1, r2 = run.host_slot, run.r1, run.r2
    n_a, n_b = len(a.mbox_msgs()), len(b.mbox_msgs())
    old0 = b.mbox_state()[0]
    lt = MP.letter(MP.pid_of(r2), MP.pid_of(r1), present=APPLE, body=b"delta letter for B")
    rid, sent, r, ack = MP.mk(run, a, 5, lt)
    MP.tx(run, "T4 A sends a letter to B's house (MAIL_SEND, M1)", r, APPLIED, R["NONE"])
    exp = L.mail_delivered_expected(lt, MP.pid_of(r1))
    ok = b.hub.wait_until(lambda: len(b.mbox_msgs()) > n_b, 12.0)
    L.pump_sleep(0.5)
    mb = b.mbox_msgs()[n_b:]
    ck("T4 after the (forced, vanilla) delivery B received exactly ONE new MAILBOX_LETTER and A received none", ok and len(mb) == 1 and len(a.mbox_msgs()) == n_a)
    g = mb[0][0] if mb else None
    ck("T4 it is B's slot 0 (the first free slot of B's house), a LETTER (not empty), used_count 1, seq > the earlier seq of that slot, digest valid",
       g is not None and g.mbox_idx == 0 and not is_empty(g) and g.used_count == 1 and g.seq > old0.seq and g.digest == L.fnv1a32(bytes(g.letter)))
    ck("T4 its bytes are exactly what the host's post office delivered: the sender replaced by the BOUND identity (resident %d), type PLAYER, RECEIVE font, gift kept" % r1,
       g is not None and bytes(g.letter) == exp and L.mail_present(bytes(g.letter)) == APPLE)
    return lt, exp


def t_take_success(run, a, gs):
    ck = run.check
    r1 = run.r1
    rev0 = a.rec_last[2]
    base0 = (a.rec_last[1], a.rec_last[2])
    pre = a.txn_pre_image()
    n0 = len(a.mbox_msgs())
    letter0 = bytes(gs[0].letter)
    dst = a.free_mail_slot()
    off = len(run.log())
    rid, sent, r, _d = take(run, a, 0, dst)
    MP.tx(run, "K1 MAIL_TAKE of mailbox slot 0 (the APPLE gift letter) into mail slot %d" % dst, r, APPLIED, R["NONE"])
    ck("K1 rev == the pre-take rev + 1, dest NONE, slot == the MAILBOX slot 0, the gift echoed, post-image == pre-image (pockets / conds / wallet untouched)",
       r is not None and r.rev == rev0 + 1 and r.dest == D_NONE and r.slot == 0 and r.item == APPLE and tuple(r.post_pockets) == tuple(pre[0])
       and r.post_conds == pre[1] and r.post_wallet == pre[2])
    ck("K1 cdig == FNV of the client+shared ranges of the mirror AFTER the commit == the digest of A's local record with the letter written into mail[%d]" % dst,
       r is not None and r.cdig == TP.cown_digest(a.rec_local) and r.cdig != 0 and L.record_mail(a.rec_local, dst) == letter0)
    tk = takes(run, off)
    ck("K1 the host logged exactly one take: resident %d, mailbox 0, mirror mail[%d], present APPLE, letter hash == the hash of the wire letter" % (r1, dst),
       len(tk) == 1 and tk[0][1] == r1 and tk[0][2] == 0 and tk[0][4] == dst and tk[0][5] == APPLE and tk[0][6] == L.mail_hash32(letter0))
    ck("K1 one TXN APPLIED line for this request", run.applied_lines(rid) == 1)
    ok = a.hub.wait_until(lambda: len(a.mbox_msgs()) > n0, 5.0)
    nm = a.mbox_msgs()[n0:]
    ck("K1b the cleared slot reached the owner as ONE new MAILBOX_LETTER: slot 0, EMPTY, seq > the letter's, used_count 2 (the mailbox lost exactly that letter)",
       ok and len(nm) >= 1 and nm[0][0].mbox_idx == 0 and is_empty(nm[0][0]) and nm[0][0].seq > gs[0].seq and nm[0][0].used_count == 2
       and bytes(nm[0][0].letter) == empty_be(a))
    return rid, sent, r, pre, base0, dst, letter0, off


def t_replay(run, a, rid, sent, r, pre, dst, off):
    ck = run.check
    n0 = len(takes(run))
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 3.0, nth=2)
    L.pump_sleep(0.3)
    ck("K2 the byte-identical resend replays APPLIED / REPLAYED with the SAME rev, executed once (one take line, one replay line)",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and r is not None and rep.rev == r.rev and len(takes(run)) == n0
       and run.replay_lines(rid) == 1)
    raw = a.build_txn_commit_bytes(K_TAKE, rid, D_NONE, 0, APPLE, pre=pre, seq=sent.seq, nonce=sent.nonce, aux_cond=0x12, aux_item=0x3456, flags=dst)
    a.send_txn_commit(K_TAKE, rid, D_NONE, 0, APPLE, raw=raw)
    cf = a.wait_txn_result(sent.seq, 3.0, nth=3)
    ck("K2 the SAME (nonce, seq) with DIFFERENT bytes is a CONFLICT and never executes",
       cf is not None and cf.outcome == REJECTED and cf.reason == R["CONFLICT"] and len(takes(run)) == n0 and "CONFLICT" in run.log())


def t_stale_upload(run, a, base0):
    ck = run.check
    L.pump_sleep(1.8)
    x = a.upload_record(a.rec_local, base=base0)  # built on the lineage point BEFORE the take
    sa = a.wait_record_ack(x, timeout=3.0)
    ck("K3 an upload built on the pre-take base is STALE_BASE (it can never resurrect the letter in the mailbox or lose the taken one)",
       sa is not None and sa.status == L.PC_NETGAME_REC_ACK_STALE_BASE)


def t_refusals(run, a, b, gs):
    ck = run.check
    # --- the slot is empty now / the letter never was there ---
    n0 = len(a.mbox_msgs())
    seq0 = a.mbox_state()[0].seq
    rid, sent, r, _d = take(run, a, 0, a.free_mail_slot(), letter_be=bytes(gs[0].letter))
    MP.tx(run, "K4 a SECOND take of the now empty mailbox slot 0 (stale shadow)", r, REJECTED, R["NO_SUCH_LETTER"])
    ok = a.hub.wait_until(lambda: len(a.mbox_msgs()) > n0, 4.0)
    ck("K4 the host re-pushed slot 0 so the stale shadow is corrected: the same (empty) state again, a HIGHER seq (the client ignores an applied seq)",
       ok and a.mbox_msgs()[n0][0].mbox_idx == 0 and is_empty(a.mbox_msgs()[n0][0]) and a.mbox_msgs()[n0][0].seq > seq0)
    rid, sent, r, _d = take(run, a, 7, a.free_mail_slot(), letter_be=bytes(gs[1].letter))
    MP.tx(run, "K4 mailbox slot 7 never held a letter", r, REJECTED, R["NO_SUCH_LETTER"])
    # --- changed letter / forged hash ---
    n1 = len(a.mbox_msgs())
    seq1 = a.mbox_state()[1].seq
    rid, sent, r, _d = take(run, a, 1, a.free_mail_slot(), hash24=(L.mail_hash24(bytes(gs[1].letter)) ^ 1) & 0xFFFFFF)
    MP.tx(run, "K5 a FORGED letter hash for mailbox slot 1", r, REJECTED, R["MAIL_CHANGED"])
    ok = a.hub.wait_until(lambda: len(a.mbox_msgs()) > n1, 4.0)
    ck("K5 the host re-pushed slot 1 (the real letter, a HIGHER seq)", ok and a.mbox_msgs()[n1][0].mbox_idx == 1 and not is_empty(a.mbox_msgs()[n1][0])
       and a.mbox_msgs()[n1][0].seq > seq1 and bytes(a.mbox_msgs()[n1][0].letter) == bytes(gs[1].letter))
    # --- BAD_IMAGE: the gift echo ---
    rid, sent, r, _d = take(run, a, 1, a.free_mail_slot(), item=APPLE + 1)
    MP.tx(run, "K6 the gift echo (tag.item) does not match the mailbox letter", r, REJECTED, R["BAD_IMAGE"])
    ck("K5-K6 no refusal removed the letter: still exactly the one take (slot 0) in the host log", len(takes(run)) == 1)
    # --- the mirror's mail[dst] is occupied ---
    occ = a.free_mail_slot()
    a.write_local_mail(occ, MP.letter(MP.pid_of(run.r1), MP.pid_of(run.r1), body=b"occupies the destination"))
    up = a.upload_local_record()
    ck("K7 setup: the occupying letter was uploaded (APPLIED)", up is not None and up.status == 0)
    rid, sent, r, _d = take(run, a, 1, occ, letter_be=bytes(gs[1].letter))
    MP.tx(run, "K7 MAIL_TAKE into a mail slot that is OCCUPIED in the host mirror (no free mail slot for the letter)", r, REJECTED, R["MAILBOX_FULL"])
    ck("K7 nothing moved: still one take; then the take into a FREE slot is accepted", len(takes(run)) == 1)
    free = a.free_mail_slot(skip=(occ,))
    rid, sent, r, _d = take(run, a, 1, free)
    MP.tx(run, "K7b the same letter into a free mail slot", r, APPLIED, R["NONE"])
    ck("K7b exactly two takes now, the second is mailbox 1", len(takes(run)) == 2 and takes(run)[-1][2] == 1 and takes(run)[-1][4] == free)
    return occ


def t_full(run, a, gs, occ):
    """Fills EVERY mail slot of A's mirror, then takes mailbox slot 2: MAILBOX_FULL; frees one slot: APPLIED."""
    ck = run.check
    fillers = []
    for i in range(L.REC_MAIL_COUNT):
        if a.free_mail_slot() is None:
            break
        s = a.free_mail_slot()
        a.write_local_mail(s, MP.letter(MP.pid_of(run.r1), MP.pid_of(run.r1), body=b"filler %d" % s))
        fillers.append(s)
    up = a.upload_local_record()
    ck("K8 setup: all 10 mail slots are in use in the mirror (upload APPLIED, no free local slot)", up is not None and up.status == 0 and a.free_mail_slot() is None)
    n = len(takes(run))
    rid, sent, r, _d = take(run, a, 2, 0, letter_be=bytes(gs[2].letter))
    MP.tx(run, "K8 MAIL_TAKE while the mirror has NO free mail slot at all", r, REJECTED, R["MAILBOX_FULL"])
    ck("K8 nothing moved, the letter is still in the mailbox (no take line)", len(takes(run)) == n)
    a.write_local_mail(4, bytes(empty_be(a)))
    up = a.upload_local_record()
    ck("K8 setup: mail slot 4 freed and uploaded", up is not None and up.status == 0)
    rid, sent, r, _d = take(run, a, 2, 4, letter_be=bytes(gs[2].letter))
    MP.tx(run, "K8b after a slot was freed the take is APPLIED", r, APPLIED, R["NONE"])
    ck("K8b the letter is in the mirror mail[4] and the mailbox is EMPTY: exactly 3 takes in the host log (slots 0, 1, 2)",
       len(takes(run)) == n + 1 and a.mbox_state().get(2) is not None and L.record_mail(a.rec_local, 4) == bytes(gs[2].letter))
    a.hub.wait_until(lambda: is_empty(a.mbox_state()[2]), 4.0)
    ck("K8b the shadow shows ALL three letters gone (the mailbox is empty again): used_count 0", all(is_empty(g) for g in a.mbox_state().values())
       and a.mbox_state()[2].used_count == 0)


def t_owner_isolation(run, a, b, lt_b, exp_b, gs):
    ck = run.check
    n = len(takes(run))
    nb = len(b.mbox_msgs())
    a_msgs = len(a.mbox_msgs())
    # B claims A's letter (the letter hash of A's slot 0) for ITS OWN slot 0 (which holds the letter A sent it): MAIL_CHANGED, nothing moves
    rid, sent, r, _d = take(run, b, 0, b.free_mail_slot(), letter_be=bytes(gs[0].letter))
    MP.tx(run, "O1 B takes mailbox slot 0 claiming the hash of A's letter (B's slot 0 holds a different letter)", r, REJECTED, R["MAIL_CHANGED"])
    rid, sent, r, _d = take(run, b, 1, b.free_mail_slot(), letter_be=bytes(gs[1].letter))
    MP.tx(run, "O2 B takes ITS mailbox slot 1 (empty on the host) with A's letter hash", r, REJECTED, R["NO_SUCH_LETTER"])
    ck("O1-O2 A's mailbox is untouched and nothing was taken: no new take line, A received no message", len(takes(run)) == n and len(a.mbox_msgs()) == a_msgs)
    rid, sent, r, d = take(run, b, 0, b.free_mail_slot())
    MP.tx(run, "O3 B takes its OWN slot 0 (the letter A mailed it) with its own shadow hash", r, APPLIED, R["NONE"])
    tk = takes(run)
    ck("O3 the host took it for resident %d (B), mailbox 0 of ITS house, into B's mirror mail[%d]; A's record / mailbox are not involved" % (run.r2, d),
       len(tk) == n + 1 and tk[-1][1] == run.r2 and tk[-1][2] == 0 and tk[-1][4] == d and tk[-1][6] == L.mail_hash32(exp_b))
    got = b.hub.wait_until(lambda: any(g.mbox_idx == 0 and is_empty(g) for g, _i in b.mbox_msgs()[nb:]), 5.0)
    ck("O3 only B received the cleared slot 0 (an EMPTY MAILBOX_LETTER); A received nothing", got and len(a.mbox_msgs()) == a_msgs)


def t_malformed(run):
    ck = run.check
    cases = [("mailbox slot 10", dict(), 10, D_NONE), ("destination mail slot 10 (flags)", dict(flags=10), 0, D_NONE),
             ("dest POCKET", dict(), 0, D_POCKET), ("rsv0 != 0", dict(rsv0=1), 0, D_NONE)]
    t = None
    for n, (label, kw, slot, dest) in enumerate(cases):
        if n % 2 == 0:
            t = run.ready("S%d" % n, run.r3)
        rev = t.rec_last[2]
        kw = dict(kw)
        kw.setdefault("flags", 0)
        sent = t.send_txn_commit(K_TAKE, run.fresh_rid(), dest, slot, 0, aux_cond=0, aux_item=0, **kw)
        r = t.wait_txn_result(sent.seq, 2.5)
        MP.tx(run, f"B{n} malformed MAIL_TAKE COMMIT ({label})", r, REJECTED, R["BAD_SHAPE"])
        ck(f"B{n} ... the peer is still connected after the violation and the mirror did not move", t.is_connected() and t.rec_last[2] == rev)
        if n % 2 == 1:
            run.release(t)


def t_peers(run, a, gs):
    ck = run.check
    host = run.host
    L.pump_sleep(0.3)
    rev_chk = a.rec_last[2]
    probe = a.build_txn_commit_bytes(K_TAKE, 1, D_NONE, 0, 0, base=(a.rec_last[1], a.rec_last[2]), aux_cond=1, aux_item=2, flags=0)
    hs = L.FakeClient("HS", run.ip, run.port, context_flags=None, wait_snapshot=False, record_hello=False)
    hs.connect(timeout=3.0)
    off = len(host.log_text())
    hs.send_reliable(probe)
    L.pump_sleep(1.2)
    ck("X1 a HANDSHAKE peer (no IDENTITY) gets no answer to a MAIL_TAKE and NO MAILBOX_LETTER; the host logs no TXN / MAIL activity",
       not hs.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_TXN_RESULT, L.PC_NETGAME_MSG_MAILBOX_LETTER))
       and "[NET][TXN]" not in run.log(off) and "MAIL_TAKE" not in run.log(off) and hs.is_connected())
    hs.close()
    holder = run.ready("H", run.r3)
    holder.wait_mbox_msgs(10, 6.0)
    park = L.FakeClient("PK", run.ip, run.port, player=L.resident_player(run.r3), context_flags=None, wait_snapshot=False, record_hello=False)
    park.connect(timeout=3.0)
    park.send_identity(town=L.resolve_host_town(run.ip, run.port), player=L.resident_player(run.r3))
    L.pump_sleep(0.5)
    off = len(host.log_text())
    park.send_reliable(probe)
    L.pump_sleep(1.2)
    ck("X2 a PARKED peer (claim on a live resident) gets no TXN_RESULT and no MAILBOX_LETTER and causes no TXN / MAIL_TAKE activity",
       not park.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_TXN_RESULT, L.PC_NETGAME_MSG_IDENTITY_ACK,
                                                                                  L.PC_NETGAME_MSG_MAILBOX_LETTER))
       and "[NET][TXN]" not in run.log(off) and "MAIL_TAKE" not in run.log(off))
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
    ck("X3 a peer claiming the HOST's own resident (refused at identity) gets no TXN_RESULT and no MAILBOX_LETTER; nothing is mutated",
       not own.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_TXN_RESULT, L.PC_NETGAME_MSG_MAILBOX_LETTER))
       and "TXN APPLIED" not in run.log(off) and "MAIL_TAKE" not in run.log(off))
    own.close()
    run.release(holder)
    ck("X4 nothing above changed A's mirror", a.rec_last[2] == rev_chk)


def t_client_originated(run, a):
    """A client must never be able to SEND a MAILBOX_LETTER: the host drops it (logged under -v only) and nothing about the mailbox changes."""
    ck = run.check
    n = len(pushes(run))
    raw = struct.pack(L.MAILBOX_LETTER_FMT, L.PC_NETGAME_MSG_MAILBOX_LETTER, 1, 0, 0, 999, L.fnv1a32(bytes(298)), 0, 0, bytes(298), 0)
    a.send_reliable(raw)
    L.pump_sleep(1.0)
    ck("X5 a client-originated MAILBOX_LETTER is ignored by the host: the peer stays connected and the host mailbox / pushes are unchanged",
       a.is_connected() and len(pushes(run)) == n and run.host.alive())


def t_concurrent(run, a, c, gs):
    """Two takes of the SAME slot back to back (exactly one wins), then a delivery into the freed slot (old hash -> MAIL_CHANGED, new hash -> APPLIED)."""
    ck = run.check
    # --- a fresh seeded mailbox is needed: A's three letters are gone. C (idle) mails A two letters (forced delivery puts them into A's house) ---
    n0 = len(a.mbox_msgs())
    lts = []
    for k in range(2):
        lt = MP.letter(MP.pid_of(run.r1), MP.pid_of(run.r3), body=b"to A #%d" % k)
        rid, sent, r, ack = MP.mk(run, c, k, lt)
        MP.tx(run, "C1.%d C sends A a letter (MAIL_SEND)" % k, r, APPLIED, R["NONE"])
        lts.append(L.mail_delivered_expected(lt, MP.pid_of(run.r3)))
        a.hub.wait_until(lambda: len([g for g, _i in a.mbox_msgs()[n0:] if not is_empty(g)]) > k, 12.0)
    new = {g.mbox_idx: g for g, _i in a.mbox_msgs()[n0:] if not is_empty(g)}
    ck("C1 A's shadow got both delivered letters in its two lowest free mailbox slots (0 and 1), byte-identical to what the post office delivered",
       sorted(new) == [0, 1] and {bytes(new[0].letter), bytes(new[1].letter)} == set(lts))
    # --- two takes of slot 0 without waiting ---
    base = len(takes(run))
    d1, d2 = a.free_mail_slot(), None
    d2 = a.free_mail_slot(skip=(d1,))
    s1 = a.send_txn_commit(K_TAKE, run.fresh_rid(), D_NONE, 0, L.mail_present(bytes(new[0].letter)), aux_cond=(L.mail_hash24(bytes(new[0].letter)) >> 16) & 0xFF,
                           aux_item=L.mail_hash24(bytes(new[0].letter)) & 0xFFFF, flags=d1)
    s2 = a.send_txn_commit(K_TAKE, run.fresh_rid(), D_NONE, 0, L.mail_present(bytes(new[0].letter)), aux_cond=(L.mail_hash24(bytes(new[0].letter)) >> 16) & 0xFF,
                           aux_item=L.mail_hash24(bytes(new[0].letter)) & 0xFFFF, flags=d2)
    a.take_ctx[(s1.nonce, s1.seq)] = (d1, bytes(new[0].letter))
    r1_ = a.wait_txn_result(s1.seq, 3.0)
    r2_ = a.wait_txn_result(s2.seq, 3.0)
    outs = sorted([(r1_.outcome, r1_.reason) if r1_ else None, (r2_.outcome, r2_.reason) if r2_ else None], key=lambda x: (x is None, x))
    ck("C2 two takes of the SAME mailbox slot sent back to back (the second built on the pre-first lineage): EXACTLY ONE is APPLIED (the first), the other is "
       "REJECTED(STALE_IMAGE: the lineage moved), exactly one take line",
       r1_ is not None and r2_ is not None and (r1_.outcome, r2_.outcome) == (APPLIED, REJECTED) and r2_.reason == R["STALE_IMAGE"] and len(takes(run)) == base + 1)
    n_k = len(takes(run))
    rid, sent, r, _d = take(run, a, 0, a.free_mail_slot(), letter_be=bytes(new[0].letter))
    MP.tx(run, "C2b the same slot taken AGAIN on a fresh lineage (the sequential form of the race): NO_SUCH_LETTER", r, REJECTED, R["NO_SUCH_LETTER"])
    ck("C2b still exactly one take of that letter", len(takes(run)) == n_k)
    ck("C2 the letter exists exactly once: in the mirror at mail[%d] (and not at mail[%s])" % (d1, d2),
       sum(1 for m in used_letters(a.rec_local) if m == bytes(new[0].letter)) == 1 and L.record_mail(a.rec_local, d1) == bytes(new[0].letter))
    # --- a delivery fills the freed slot 0 while the client still holds the old shadow hash ---
    n1 = len(a.mbox_msgs())
    lt3 = MP.letter(MP.pid_of(run.r1), MP.pid_of(run.r3), body=b"to A #2 after the take")
    rid, sent, r, ack = MP.mk(run, c, 2, lt3)
    MP.tx(run, "C3 C sends A a third letter", r, APPLIED, R["NONE"])
    a.hub.wait_until(lambda: any(g.mbox_idx == 0 and not is_empty(g) for g, _i in a.mbox_msgs()[n1:]), 12.0)
    g0 = next((g for g, _i in a.mbox_msgs()[n1:] if g.mbox_idx == 0 and not is_empty(g)), None)
    ck("C3 the postman filled the FREED slot 0 with the new letter (a different digest than the one just taken)", g0 is not None and g0.digest != new[0].digest)
    rid, sent, r, _d = take(run, a, 0, a.free_mail_slot(), letter_be=bytes(new[0].letter))
    MP.tx(run, "C3 a take of slot 0 with the OLD letter's hash (the slot changed under the client)", r, REJECTED, R["MAIL_CHANGED"])
    rid, sent, r, _d = take(run, a, 0, a.free_mail_slot())
    MP.tx(run, "C3b a take of slot 0 with the NEW letter's hash", r, APPLIED, R["NONE"])


def phase_main(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    ck("T setup: A and B READY and SYNCED", a.rec_synced and b.rec_synced and a.rec_last is not None)
    gs = t_sync(run, a, b)
    if len(gs) != 10:
        return
    lt_b, exp_b = t_delta(run, a, b, gs)
    # --- TAKE ---
    rid, sent, r, pre, base0, dst, letter0, off = t_take_success(run, a, gs)
    if r is None or r.outcome != APPLIED:
        return
    t_replay(run, a, rid, sent, r, pre, dst, off)
    t_stale_upload(run, a, base0)
    # a NEW session of the owner: mirror holds the letter, the mailbox is consistent, nothing duplicated
    seq1 = a.mbox_state()[1].seq
    run.release(a)
    a2 = run.ready("A2", run.r1)
    m2 = a2.wait_mbox_msgs(10, 8.0)
    s2 = a2.mbox_state()
    p = a2.rec_pushes[-1] if a2.rec_pushes else None
    ck("K9 a NEW session of A is pushed the mirror at rev == the take's rev with the letter in mail[%d], byte-identical to the mailbox letter" % dst,
       p is not None and p["digest_ok"] and p["rev"] == r.rev and L.record_mail(p["data"], dst) == letter0)
    ck("K9 ... and its mailbox sync: slot 0 EMPTY (the letter left the host mailbox), slots 1 and 2 still the original letters, the SAME seqs as before (the host's seq "
       "survives a reconnect; unchanged slots are the same state)",
       len(m2) == 10 and is_empty(s2[0]) and bytes(s2[1].letter) == bytes(gs[1].letter) and bytes(s2[2].letter) == bytes(gs[2].letter) and s2[1].seq == seq1)
    ck("K9 exactly ONE copy of the APPLE gift exists across the mirror record and the mailbox (no duplicate)",
       sum(1 for m in used_letters(p["data"]) if L.mail_present(m) == APPLE) + sum(1 for g in s2.values() if not is_empty(g) and L.mail_present(bytes(g.letter)) == APPLE) == 1)
    a = a2
    t_refusals(run, a, b, gs)
    occ = None
    t_full(run, a, gs, occ)
    t_owner_isolation(run, a, b, lt_b, exp_b, gs)
    t_malformed(run)
    t_client_originated(run, a)
    run.release(b)
    t_peers(run, a, gs)
    c = run.ready("C", run.r3)
    c.wait_mbox_msgs(10, 6.0)
    ck("T5 the third resident C got 10 'empty' slots (none of A's / B's letters)", len(c.mbox_msgs()) == 10 and all(is_empty(g) for g, _i in c.mbox_msgs()))
    # concurrent section on a fresh mailbox of A: clear the mirror's fillers first so there are free mail slots again
    for s in range(L.REC_MAIL_COUNT):
        if L.mail_font(L.record_mail(a.rec_local, s)) != EMPTY:
            a.write_local_mail(s, bytes(empty_be(a)))
    up = a.upload_local_record()
    ck("C0 setup: A's mirror mail[] emptied again (upload APPLIED)", up is not None and up.status == 0)
    t_concurrent(run, a, c, gs)


# ----------------------------------------------------------------------------------------------------------------------
# Phase G: a gift that cannot exist in a pocket
# ----------------------------------------------------------------------------------------------------------------------
def phase_gift(run):
    ck = run.check
    a = run.ready("A", run.r1)
    ms = a.wait_mbox_msgs(10, 8.0)
    gs = a.mbox_state()
    ck("G1 the owner receives the seeded letters, slot 0 carrying the gift 0x%04X that is not pocket-legal" % BAD_GIFT,
       len(ms) == 10 and not is_empty(gs[0]) and L.mail_present(bytes(gs[0].letter)) == BAD_GIFT and not is_empty(gs[1]))
    n = len(takes(run))
    rev = a.rec_last[2]
    rid, sent, r, _d = take(run, a, 0)
    MP.tx(run, "G2 MAIL_TAKE of the letter whose gift cannot exist in a pocket (it would be refused by the record validator and block the client's uploads)",
          r, REJECTED, R["BAD_IMAGE"])
    ck("G2 nothing moved: no take line, the mirror rev did not change", len(takes(run)) == n and r is not None and r.rev == rev)
    rid, sent, r, _d = take(run, a, 1)
    MP.tx(run, "G3 the other (ordinary) letter can still be taken", r, APPLIED, R["NONE"])
    L.pump_sleep(1.0)
    ck("G3 the refused letter was never touched on the host: no 'cleared' message for slot 0 (only slot 1 was cleared)",
       not any(g.mbox_idx == 0 and is_empty(g) and g.seq > gs[0].seq for g, _i in a.mbox_msgs()))


# ----------------------------------------------------------------------------------------------------------------------
# Phase R: M2r
# ----------------------------------------------------------------------------------------------------------------------
def phase_remail(run):
    ck = run.check
    r1, r2, r3 = run.r1, run.r2, run.r3
    ck("R0 the host test hook made a villager owe resident %d a reply" % r1,
       MP._wait(lambda: "--mail-test-seed-reply: villager slot" in run.log(), 10.0))
    a = run.ready("A", r1)
    a.wait_mbox_msgs(10, 8.0)
    ok = MP._wait(lambda: any(int(m.group(1)) == r1 for m in REMAIL_RE.finditer(run.log())), 12.0)
    ck("R1 once the owner's record is SYNCED the HOST runs the villager-reply pass for resident %d" % r1, ok)
    pr = [m for m in REMAIL_BEFORE_RE.finditer(run.log()) if int(m.group(1)) == r1]
    po = [m for m in REMAIL_RE.finditer(run.log()) if int(m.group(1)) == r1]
    ck("R1 the pass put EXACTLY ONE reply letter into the post office (before n, after n + 1, one player letter)",
       len(pr) == 1 and len(po) == 1 and int(po[0].group(2)) == int(pr[0].group(3)) + 1 and int(po[0].group(3)) >= 1)
    n0 = 10  # the 10 READY-time messages (all empty: the pass needs a SYNCED record, which comes later) are the baseline
    ck("R1b the READY-time sync of the owner was 10 EMPTY slots (the reply did not exist yet)", all(is_empty(g) for g, _i in a.mbox_msgs()[:n0]))
    got = a.hub.wait_until(lambda: any(not is_empty(g) for g, _i in a.mbox_msgs()[n0:]), 14.0)
    L.pump_sleep(0.4)
    new = [g for g, _i in a.mbox_msgs()[n0:] if not is_empty(g)]
    ck("R2 the reply reached the OWNER's mailbox as ONE new MAILBOX_LETTER (the forced vanilla delivery moved it into the house mailbox)", got and len(new) == 1)
    g = new[0] if new else None
    lt = bytes(g.letter) if g is not None else b""
    ck("R2 it is a villager letter: sender type NPC, recipient = resident %d (type PLAYER), RECEIVE font, digest valid, used_count 1" % r1,
       g is not None and L.mail_sender(lt)[1] == L.MAIL_NAME_NPC and L.mail_recipient(lt) == (MP.pid_of(r1), L.MAIL_NAME_PLAYER)
       and L.mail_font(lt) == L.MAIL_FONT_RECV and g.digest == L.fnv1a32(lt) and g.used_count == 1)
    b = run.ready("B", r2)
    b.wait_mbox_msgs(10, 8.0)
    MP._wait(lambda: any(int(m.group(1)) == r2 for m in REMAIL_RE.finditer(run.log())), 8.0)
    pb = [m for m in REMAIL_BEFORE_RE.finditer(run.log()) if int(m.group(1)) == r2]
    qb = [m for m in REMAIL_RE.finditer(run.log()) if int(m.group(1)) == r2]
    ck("R3 resident %d (connected, nothing owed to it): a pass ran and generated NOTHING (the post office count did not change), its mailbox stays empty" % r2,
       len(pb) == 1 and len(qb) == 1 and int(qb[0].group(2)) == int(pb[0].group(3)) and all(is_empty(g) for g, _i in b.mbox_msgs()))
    run.release(a)
    a2 = run.ready("A2", r1)
    a2.wait_mbox_msgs(10, 8.0)
    L.pump_sleep(3.0)
    ck("R4 a reconnect of the owner the same day does NOT generate a second reply: still one pass for resident %d in the host log, and its new session mailbox holds "
       "exactly the one reply" % r1,
       len([m for m in REMAIL_RE.finditer(run.log()) if int(m.group(1)) == r1]) == 1
       and len([g for g in a2.mbox_state().values() if not is_empty(g)]) == 1 and any(bytes(g.letter) == lt for g in a2.mbox_state().values() if not is_empty(g)))
    ck("R5 no pass ever ran for a resident that is NOT connected (%d) nor for the host's own resident (%d)" % (r3, run.host_slot),
       not any(int(m.group(1)) in (r3, run.host_slot) for m in REMAIL_BEFORE_RE.finditer(run.log())))


# ----------------------------------------------------------------------------------------------------------------------
# Fault phases
# ----------------------------------------------------------------------------------------------------------------------
def phase_drop(run):
    ck = run.check
    a = run.ready("A", run.r1)
    ms = a.wait_mbox_msgs(10, 8.0)
    gs = a.mbox_state()
    rev = a.rec_last[2]
    dst = a.free_mail_slot()
    off = len(run.log())
    n0 = len(a.mbox_msgs())
    rid, sent, first, _d = take(run, a, 0, dst, wait=1.6)
    L.pump_sleep(0.4)
    ck("F1 the APPLIED RESULT is lost (drop_result) but the host DID take: no result at A, one take line, fault logged",
       first is None and len(takes(run, off)) == 1 and "FAULT INJECTION FIRED mode=drop_result" in run.log())
    ok = a.hub.wait_until(lambda: len(a.mbox_msgs()) > n0, 4.0)
    ck("F1 ... and the cleared slot 0 still reached the client as a MAILBOX_LETTER (the digest poll does not depend on the RESULT)",
       ok and a.mbox_msgs()[n0][0].mbox_idx == 0 and is_empty(a.mbox_msgs()[n0][0]))
    a.resend_txn(sent)
    res = a.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.4)
    ck("F1 the resend gets APPLIED / REPLAYED, rev == the pre-take rev + 1, executed exactly once (one take line, one replay line)",
       res is not None and res.outcome == APPLIED and res.reason == R["REPLAYED"] and res.rev == rev + 1 and len(takes(run, off)) == 1 and run.replay_lines(rid) == 1)
    ck("F1 the client's apply step used the verified letter although the cleared slot had already arrived (mirror mail[%d] == the letter)" % dst,
       L.record_mail(a.rec_local, dst) == bytes(gs[0].letter))
    run.release(a)
    a2 = run.ready("A2", run.r1)
    a2.wait_mbox_msgs(10, 8.0)
    p = a2.rec_pushes[-1] if a2.rec_pushes else None
    s2 = a2.mbox_state()
    ck("F2 a new session: the pushed mirror holds the letter ONCE and the mailbox slot 0 is empty, slots 1 and 2 intact",
       p is not None and L.record_mail(p["data"], dst) == bytes(gs[0].letter) and len(used_letters(p["data"])) == 1 and is_empty(s2[0])
       and not is_empty(s2[1]) and not is_empty(s2[2]) and len(takes(run, off)) == 1)


def phase_kill(run):
    ck = run.check
    a = run.ready("A", run.r1)
    a.wait_mbox_msgs(10, 8.0)
    gs = a.mbox_state()
    rev = a.rec_last[2]
    dst = a.free_mail_slot()
    off = len(run.log())
    rid, sent, r, _d = take(run, a, 0, dst, wait=1.0)
    closed = TP.wait_closed(a, 4.0)
    L.pump_sleep(0.4)
    ck("K1 kill_peer_after_commit: the host dropped A after taking and sent NO result",
       closed and r is None and not a.txn_results_for(sent.seq) and "kill_peer_after_commit: peer" in run.log() and len(takes(run, off)) == 1)
    a.reconnect_and_ready(timeout=8.0)
    a.wait_mbox_msgs(10, 8.0)
    pushed = a.rec_pushes[-1] if a.rec_pushes else None
    s = a.mbox_state()
    ck("K2 the same-nonce reconnect is pushed the mirror: rev == the pre-take rev + 1 and mail[%d] ALREADY holds the letter; the new session's mailbox shows slot 0 EMPTY"
       % dst, pushed is not None and pushed["rev"] == rev + 1 and L.record_mail(pushed["data"], dst) == bytes(gs[0].letter) and is_empty(s[0])
       and not is_empty(s[1]))
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.3)
    ck("K3 the OLD COMMIT resent after the reconnect replays APPLIED / REPLAYED (journal survives the peer reset), NO second execution",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and len(takes(run, off)) == 1)
    run.release(a)
    a2 = run.client("A2", run.r1)
    a2.connect_and_ready(quiet=True)
    a2.wait_mbox_msgs(10, 8.0)
    p2 = a2.rec_pushes[-1] if a2.rec_pushes else None
    s2 = a2.mbox_state()
    ck("K4 a NEW-PROCESS rejoin: the pushed record holds the letter in mail[%d] ONCE, the mailbox slot 0 is empty (exactly one copy of the gift overall)" % dst,
       p2 is not None and L.record_mail(p2["data"], dst) == bytes(gs[0].letter) and is_empty(s2[0]) and len(takes(run, off)) == 1
       and sum(1 for m in used_letters(p2["data"]) if L.mail_present(m) == APPLE) + sum(1 for g in s2.values() if not is_empty(g) and L.mail_present(bytes(g.letter)) == APPLE) == 1)
    ck("K4 the host survived the dropped peer", run.host.alive())


PHASES = [
    ("M2-T", None, phase_main),
    ("M2-G", None, phase_gift),
    ("M2-R", None, phase_remail),
    ("M2-drop", None, phase_drop),
    ("M2-kill", None, phase_kill),
]
PHASE_KEYS = {"T": "M2-T", "G": "M2-G", "R": "M2-R", "F-drop": "M2-drop", "F-kill": "M2-kill"}


def phase_args(tag, first_client):
    r = first_client
    if tag == "M2-T":
        return ["--mail-test-force-delivery", "--mail-test-seed-mailbox=%d,3,0,%X" % (r, APPLE)]
    if tag == "M2-G":
        return ["--mail-test-seed-mailbox=%d,2,0,%X" % (r, BAD_GIFT)]
    if tag == "M2-R":
        return ["--mail-test-force-delivery", "--mail-test-seed-reply=%d" % r]
    if tag == "M2-drop":
        return ["--mail-test-seed-mailbox=%d,3,0,%X" % (r, APPLE), "--txn-fault=drop_result:1:1"]
    return ["--mail-test-seed-mailbox=%d,3,0,%X" % (r, APPLE), "--txn-fault=kill_peer_after_commit:1:1"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11300)
    ap.add_argument("--only", default="", help="comma list of phase names (T, G, R, F-drop, F-kill)")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    only = [PHASE_KEYS.get(x, x) for x in args.only.split(",") if x]
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "mail2_protocol_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    first_client = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT][0]

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        for i, (tag, _extra, body) in enumerate(PHASES):
            if only and tag not in only:
                continue
            run_phase(results, ip, args.port + i, tag, phase_args(tag, first_client), body, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
