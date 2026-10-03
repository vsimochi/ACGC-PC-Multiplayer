#!/usr/bin/env python3
"""test_mail_protocol.py - Mail milestone 1 (HOST half): a client's LETTER TO A PLAYER through the host-authoritative MAIL_SEND transaction
(TXN_COMMIT kind 12, no new message id, reasons 23 NO_SUCH_ADDRESS / 24 MAILBOX_FULL / 25 PO_FULL), plus the museum_record HOST-ownership (R).

PROTOCOL test against REAL host processes (`AnimalCrossing.exe --host --bootstrap-resident 0 [hooks]`, started and stopped by this script from the
TEST COPY named by NET_SPIKE_GAME_BIN = pc\\build64\\bin_fixture4; host = resident 0, clients = residents 1..3, at most 3 simultaneous clients, ports
11200+). `require_test_bin_dir()` refuses the live dirs; the fixture save dir is SNAPSHOTTED at start and RESTORED after every host phase. Clients are
scripted FakeClients (net_spike_lib: write_local_mail / upload_local_record / txn_mail_send, TXN_RESULT parsing, canonical-BE letter bytes and the
24-bit letter hash); the host's C code runs for real. What the host did is proven from its own loud log lines (the exact letter hash handed to the
post office, the PO slot it landed in) and from the host's TEST-ONLY --mail-test-force-delivery hook, which runs the REAL vanilla
mPO_delivery_one_address() and logs every letter of every house mailbox (BE hash, font, gift, sender / recipient type), so the test sees WHAT arrived in
WHICH house. The real client half is covered by test_mail_real_client.py (one letter through the real request path) and the source audit test_mail_src.py.

Phases (one host process each):
  M       success (spoofed sender overwritten with the bound identity, receive font, gift kept in the letter, sender mirror slot cleared + the pockets
          untouched, the letter lands in the recipient house's host mailbox after delivery), replay x1 / CONFLICT, a NEW session's push shows the cleared
          slot, forged hash / letter edited after its upload / letter never uploaded -> STALE_IMAGE, RSV_NO gift / receive-font letter / gift echo mismatch
          -> BAD_IMAGE, NPC and MUSEUM recipients -> PRECOND, unknown address -> NO_SUCH_ADDRESS, send to self, STALE_BASE for an upload built before the
          commit, BAD_SHAPE probes, the upload validator for mail[].present, HANDSHAKE / parked / host-own peers ignored, MAILBOX_FULL
  P       museum_record is HOST-owned (a host consumption -> rev + 1 -> PUSH_HOSTFIELDS carrying only the museum bytes), PO_FULL (the 5-slot desk)
  F-drop  drop_result:1:1   the APPLIED RESULT is lost, the commit happened once; the byte-identical resend replays
  F-kill  kill_peer_after_commit:1:1   the peer is dropped after the commit; same-nonce reconnect: pushed mirror has the slot cleared, replay, one PO copy;
          NEW-process rejoin sees the same

Tier: PROTOCOL TESTED (real host binary, scripted clients). Usage: python test_mail_protocol.py [--port 11200] [--only M,P,F-drop,F-kill]
"""
import argparse
import os
import re
import shutil
import struct
import sys

import net_spike_lib as L
import test_txn_protocol as TP
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE, D_POCKET

K_MAIL = L.PC_NETGAME_TXN_KIND_MAIL_SEND
APPLE = 0x2800
RSV_NO = 0xFFFF
MAIL_RE = re.compile(r"\[NET\]\[MAIL\] host: peer (\d+) resident (\d+) MAIL_SEND slot (\d+) -> house (-?\d+) present 0x([0-9A-F]{4}) "
                     r"letter_hash=0x([0-9A-F]{8}) po_slot=(-?\d+) po_sum=(-?\d+) mirror_slot_cleared=1 committed \[TXN\]")
DELIV_RE = re.compile(r"\[NET\]\[MAIL\]\[TEST-ONLY\] force-delivery: house (\d+) mailbox\[(\d+)\] hash=0x([0-9A-F]{8}) font=(\d+) present=0x([0-9A-F]{4}) "
                      r"recipient_type=(\d+) sender_type=(\d+) sender_pid_id=0x([0-9A-F]{4})")


def pid_of(idx):
    """The 20 BE bytes of resident `idx`'s player_ID as the fixture GCI holds it (what a letter's PersonalID carries)."""
    return bytes(L.record_from_gci(L.GAME_BIN_DIR, idx)[:0x14])


def pid_id16(pid):
    return struct.unpack_from(">H", pid, 0x10)[0]


def commits_text(txt):
    """[(peer, resident, slot, house, present, hash, po_slot, po_sum)] of the host's MAIL_SEND commit lines in a log text."""
    return [(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5), 16), int(m.group(6), 16), int(m.group(7)), int(m.group(8)))
            for m in MAIL_RE.finditer(txt)]


def delivered_text(txt):
    """[(house, idx, hash, font, present, recipient_type, sender_type, sender_pid_id)] of every force-delivery mailbox line in a log text."""
    return [(int(m.group(1)), int(m.group(2)), int(m.group(3), 16), int(m.group(4)), int(m.group(5), 16), int(m.group(6)), int(m.group(7)), int(m.group(8), 16))
            for m in DELIV_RE.finditer(txt)]


def commits(run, off=0):
    return commits_text(run.log(off))


def delivered(run, off=0):
    return delivered_text(run.log(off))


def last_batch(run):
    """The mailbox lines of the LATEST force-delivery run (each run logs every used letter of every mailbox)."""
    txt = run.log()
    i = txt.rfind("force-delivery: post office holds")
    return delivered(run, 0) if i < 0 else [(int(m.group(1)), int(m.group(2)), int(m.group(3), 16), int(m.group(4)), int(m.group(5), 16), int(m.group(6)),
                                              int(m.group(7)), int(m.group(8), 16)) for m in DELIV_RE.finditer(txt[i:])]


def _wait(pred, timeout):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        L.pump_sleep(0.25)
    return pred()


def wait_delivered(run, hsh, timeout=9.0):
    """Waits until a force-delivery mailbox line with the BE letter hash `hsh` exists; returns it or None."""
    def find():
        return next((d for d in delivered(run) if d[2] == hsh), None)
    _wait(lambda: find() is not None, timeout)
    return find()


def letter(recipient_pid, sender_pid, present=0, **kw):
    return L.build_mail_be(recipient_pid, sender_pid, present=present, **kw)


def mk(run, c, slot, lt, wait=3.0, **kw):
    """write the letter into the client's local record, upload it, send MAIL_SEND: (rid, TxnSent, result, upload ack)."""
    rid = run.fresh_rid()
    sent, r, ack = c.txn_mail_send(slot, lt, rid=rid, timeout=wait, **kw)
    return rid, sent, r, ack


def tx(run, desc, r, outcome, reason):
    run.tx_ok(desc, r, outcome, reason)


def empty_slot_bytes(rec):
    """The bytes of an unused mail slot of a record image (font 0xFF), or None."""
    for i in range(L.REC_MAIL_COUNT):
        m = L.record_mail(rec, i)
        if L.mail_font(m) == L.MAIL_FONT_UNUSED:
            return m
    return None


def start_host(ip, port, tag, extra, results):
    host_slot = L.TEST_HOST_RESIDENT
    log_dir = os.path.dirname(os.path.abspath(__file__))
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot)] + list(extra),
                         log_path=os.path.join(log_dir, f"mail_protocol_{tag}_host.log")).start()
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


# ----------------------------------------------------------------------------------------------------------------------
# Phase M
# ----------------------------------------------------------------------------------------------------------------------
def m_success(run, a):
    ck = run.check
    h0, r1, r2 = run.host_slot, run.r1, run.r2
    pre = a.txn_pre_image()
    lt = letter(pid_of(h0), pid_of(r2), present=APPLE)  # the sender field is SPOOFED (resident 2's identity): the host must overwrite it
    off = len(run.log())
    rid, sent, r, ack = mk(run, a, 2, lt)
    ck("M1 the letter's record upload was APPLIED by the host (AWAIT_CLEAN: the host's mirror holds the letter)", ack is not None and ack.status == 0)
    tx(run, "M1 MAIL_SEND of a letter with a gift to the HOST's resident", r, APPLIED, R["NONE"])
    ck("M1 rev == the upload's rev + 1, dest NONE, slot 2, the gift echoed, post-image == pre-image (pockets / conds / wallet untouched)",
       r is not None and ack is not None and r.rev == ack.rev + 1 and r.dest == D_NONE and r.slot == 2 and r.item == APPLE
       and tuple(r.post_pockets) == tuple(pre[0]) and r.post_conds == pre[1] and r.post_wallet == pre[2])
    ck("M1 cdig == FNV of the client+shared ranges of the mirror AFTER the commit (the host cleared mail[2] with the same bytes the client clears with)",
       r is not None and r.cdig == TP.cown_digest(a.rec_local) and r.cdig != 0)
    cm = commits(run, off)
    exp_hash = L.mail_hash32(L.mail_delivered_expected(lt, pid_of(r1)))
    ck("M1 the host logged exactly one commit, resident %d, slot 2, present APPLE, post office slot >= 0, po_sum 1 (before: nothing queued)" % r1,
       len(cm) == 1 and cm[0][1] == r1 and cm[0][2] == 2 and cm[0][4] == APPLE and cm[0][6] >= 0 and cm[0][7] >= 1)
    ck("M1 the letter handed to the post office == the letter the host READ FROM ITS MIRROR with the sender replaced by the BOUND identity (resident %d, not the spoofed %d), "
       "type PLAYER and the RECEIVE font (hash 0x%08X)" % (r1, r2, exp_hash), len(cm) == 1 and cm[0][5] == exp_hash)
    ck("M1 and that hash differs from the letter AS SENT (the spoofed sender / send font did not survive)", exp_hash != L.mail_hash32(lt))
    ck("M1 one TXN APPLIED line for this request", run.applied_lines(rid) == 1)
    d = wait_delivered(run, exp_hash)
    ck("M1b after the (forced) vanilla delivery the letter IS in the recipient's host mailbox: house %s, font RECV, gift APPLE, sender / recipient type PLAYER, sender PersonalID = resident %d"
       % (cm[0][3] if cm else "?", r1),
       d is not None and cm and d[0] == cm[0][3] and d[3] == L.MAIL_FONT_RECV and d[4] == APPLE and d[5] == 0 and d[6] == 0 and d[7] == pid_id16(pid_of(r1)))
    ck("M1b exactly ONE copy of the gift letter exists in all mailboxes (the gift was not duplicated)", len([x for x in delivered(run) if x[2] == exp_hash]) >= 1
       and len({(x[0], x[1]) for x in delivered(run) if x[2] == exp_hash}) == 1)
    return lt, sent, r, ack, pre, rid, exp_hash, (cm[0][3] if cm else -1)


def m_replay(run, a, lt, sent, r, pre, rid):
    ck = run.check
    n0 = len(commits(run))
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 3.0, nth=2)
    L.pump_sleep(0.3)
    ck("M2 the byte-identical resend replays APPLIED / REPLAYED with the SAME rev, executed once (one commit line, one replay line)",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and r is not None and rep.rev == r.rev
       and len(commits(run)) == n0 and run.replay_lines(rid) == 1)
    raw = a.build_txn_commit_bytes(K_MAIL, rid, D_NONE, 2, APPLE, pre=pre, seq=sent.seq, nonce=sent.nonce, aux_cond=0x12, aux_item=0x3456)
    a.send_txn_commit(K_MAIL, rid, D_NONE, 2, APPLE, raw=raw)
    cf = a.wait_txn_result(sent.seq, 3.0, nth=3)
    ck("M2 the SAME (nonce, seq) with DIFFERENT bytes is a CONFLICT and never executes", cf is not None and cf.outcome == REJECTED and cf.reason == R["CONFLICT"]
       and len(commits(run)) == n0 and "CONFLICT" in run.log())


def m_mirror_after(run, a, r, pre):
    ck = run.check
    run.release(a)
    a2 = run.ready("A2", run.r1)
    p = a2.rec_pushes[-1] if a2.rec_pushes else None
    data = p["data"] if p else b""
    ck("M3 a NEW session of the sender is pushed the mirror at rev == the RESULT's rev", p is not None and p["digest_ok"] and r is not None and p["rev"] == r.rev)
    e = empty_slot_bytes(data) if p else None
    ck("M3 mail[2] of the mirror is EMPTY and byte-identical to an unused slot (the letter and its gift are gone from the sender's record)",
       p is not None and e is not None and L.record_mail(data, 2) == e and L.mail_font(L.record_mail(data, 2)) == L.MAIL_FONT_UNUSED)
    ck("M3 the pockets / conds / wallet of the mirror are unchanged", p is not None and L.record_inventory(data) == pre)
    ck("M3 no letter in the mirror still carries the gift", p is not None and all(not (L.mail_font(L.record_mail(data, i)) != L.MAIL_FONT_UNUSED and L.mail_present(L.record_mail(data, i)) == APPLE)
                                                                       for i in range(L.REC_MAIL_COUNT)))
    return a2


def m_refusals(run, a):
    ck = run.check
    h0, r1 = run.host_slot, run.r1
    n0 = len(commits(run))
    # --- forged hash / edited after the upload / never uploaded ---
    lt2 = letter(pid_of(h0), pid_of(r1), body=b"second letter")
    rid, sent, r, ack = mk(run, a, 3, lt2, hash24=(L.mail_hash24(lt2) ^ 1) & 0xFFFFFF)
    tx(run, "M4 a FORGED letter hash", r, REJECTED, R["STALE_IMAGE"])
    ck("M4 nothing was delivered", len(commits(run)) == n0)
    lt3 = letter(pid_of(h0), pid_of(r1), body=b"v1 uploaded")
    a.write_local_mail(4, lt3)
    ack3 = a.upload_local_record()
    lt3b = letter(pid_of(h0), pid_of(r1), body=b"v2 edited locally after the upload")
    rid, sent, r, _ack = mk(run, a, 4, lt3b, upload=False)
    ck("M5 the upload of v1 was APPLIED", ack3 is not None and ack3.status == 0)
    tx(run, "M5 the letter was EDITED after its last upload (hash of v2, mirror holds v1)", r, REJECTED, R["STALE_IMAGE"])
    rid, sent, r, _ack = mk(run, a, 5, letter(pid_of(h0), pid_of(r1), body=b"never uploaded"), upload=False)
    tx(run, "M5 a letter that was NEVER uploaded (the mirror slot is empty): 'commit before upload'", r, REJECTED, R["STALE_IMAGE"])
    ck("M5 still nothing delivered", len(commits(run)) == n0)
    # --- after a STALE_IMAGE the same (now uploaded) letter is accepted ---
    rid, sent, r, ack = mk(run, a, 3, None, upload=False)
    tx(run, "M4b the genuinely uploaded letter of slot 3 (same bytes, correct hash) is then APPLIED", r, APPLIED, R["NONE"])
    ck("M4b exactly one more commit, no gift", len(commits(run)) == n0 + 1 and commits(run)[-1][4] == 0)
    n1 = len(commits(run))
    # --- BAD_IMAGE ---
    rid, sent, r, ack = mk(run, a, 1, letter(pid_of(h0), pid_of(r1), present=RSV_NO, body=b"rsv gift"))
    ck("M6 the upload validator tolerates the vanilla RSV_NO 'no gift' marker in a letter (APPLIED)", ack is not None and ack.status == 0)
    tx(run, "M6 MAIL_SEND of a letter whose gift is RSV_NO (not EMPTY_NO, not pocket-legal)", r, REJECTED, R["BAD_IMAGE"])
    rid, sent, r, ack = mk(run, a, 6, letter(pid_of(h0), pid_of(r1), font=L.MAIL_FONT_RECV, body=b"recv font"))
    tx(run, "M6 MAIL_SEND of a letter in the RECEIVE font (not a send-font letter; hash matches the mirror)", r, REJECTED, R["BAD_IMAGE"])
    rid, sent, r, ack = mk(run, a, 7, letter(pid_of(h0), pid_of(r1), present=APPLE, body=b"echo"), item=APPLE + 1)
    tx(run, "M6 the gift echo (tag.item) does not match the mirror letter", r, REJECTED, R["BAD_IMAGE"])
    # --- PRECOND: villager / museum recipients ---
    rid, sent, r, ack = mk(run, a, 8, letter(pid_of(h0), pid_of(r1), recipient_type=L.MAIL_NAME_NPC, body=b"to a villager"))
    tx(run, "M7 an NPC recipient stays on MAIL_REQUEST (not this transaction)", r, REJECTED, R["PRECOND"])
    rid, sent, r, ack = mk(run, a, 9, letter(pid_of(h0), pid_of(r1), recipient_type=L.MAIL_NAME_MUSEUM, body=b"to the museum"))
    tx(run, "M7 a MUSEUM recipient waits for the museum milestone", r, REJECTED, R["PRECOND"])
    # --- NO_SUCH_ADDRESS ---
    bad = bytearray(pid_of(h0))
    bad[0x10] ^= 0xFF
    rid, sent, r, ack = mk(run, a, 0, letter(bytes(bad), pid_of(r1), body=b"nobody lives here"))
    tx(run, "M8 a PLAYER recipient whose PersonalID matches no house on the host", r, REJECTED, R["NO_SUCH_ADDRESS"])
    ck("M1-M8 none of the refusals delivered anything", len(commits(run)) == n1)
    return n1


def m_self_and_stale(run, a):
    ck = run.check
    r1 = run.r1
    lt = letter(pid_of(r1), pid_of(r1), body=b"note to self")
    off = len(run.log())
    rid, sent, r, ack = mk(run, a, 0, lt)
    tx(run, "M9 a letter to SELF (the sender's own house)", r, APPLIED, R["NONE"])
    cm = commits(run, off)
    ck("M9 delivered by the host to the sender's OWN house (a house different from the host resident's)", len(cm) == 1 and cm[0][1] == r1)
    exp = L.mail_hash32(L.mail_delivered_expected(lt, pid_of(r1)))
    d = wait_delivered(run, exp)
    ck("M9b the self letter arrived in that house's mailbox with the RECEIVE font", d is not None and cm and d[0] == cm[0][3] and d[3] == L.MAIL_FONT_RECV)
    L.pump_sleep(1.8)
    x = a.upload_record(a.rec_local, base=(ack.epoch, ack.rev))  # built on the lineage point BEFORE the MAIL_SEND commit
    sa = a.wait_record_ack(x, timeout=3.0)
    ck("M10 an upload built on the pre-commit base is STALE_BASE (it can never resurrect the sent letter)", sa is not None and sa.status == L.PC_NETGAME_REC_ACK_STALE_BASE)


def m_malformed(run):
    ck = run.check
    h0 = run.host_slot
    cases = [
        ("slot 10", dict(), 10, D_NONE),
        ("dest POCKET", dict(), 0, D_POCKET),
        ("flags 1", dict(flags=1), 0, D_NONE),
        ("rsv0 != 0", dict(rsv0=1), 0, D_NONE),
    ]
    t = None
    for n, (label, kw, slot, dest) in enumerate(cases):
        if n % 2 == 0:
            t = run.ready("S%d" % n, run.r3)
        rev = t.rec_last[2]
        sent = t.send_txn_commit(K_MAIL, run.fresh_rid(), dest, slot, 0, aux_cond=0, aux_item=0, **kw)
        r = t.wait_txn_result(sent.seq, 2.5)
        tx(run, f"B{n} malformed MAIL_SEND COMMIT ({label})", r, REJECTED, R["BAD_SHAPE"])
        ck(f"B{n} ... the peer is still connected after the violation and the mirror did not move", t.is_connected() and t.rec_last[2] == rev)
        if n % 2 == 1:
            run.release(t)


def m_validator(run, c):
    ck = run.check
    h0, r3 = run.host_slot, run.r3
    # an illegal gift in a USED letter -> INVALID_FIELD (detail 12, slot in the high byte), rev unchanged
    c.write_local_mail(1, letter(pid_of(h0), pid_of(r3), present=0x0123, body=b"junk gift"))
    rev = c.rec_last[2]
    ack = c.upload_local_record()
    ck("V1 an upload with a USED letter whose gift is not pocket-legal (0x0123) -> INVALID_FIELD detail MAIL_PRESENT | slot 1 << 8, rev unchanged",
       ack is not None and ack.status == L.PC_NETGAME_REC_ACK_INVALID_FIELD and ack.detail == (L.PC_NETGAME_REC_FIELD_MAIL_PRESENT | (1 << 8)) and ack.rev == rev)
    c.write_local_mail(1, letter(pid_of(h0), pid_of(r3), present=RSV_NO, body=b"rsv"))
    ack = c.upload_local_record()
    ck("V2 the vanilla RSV_NO marker is accepted (APPLIED)", ack is not None and ack.status == 0)
    c.write_local_mail(1, letter(pid_of(h0), pid_of(r3), present=APPLE, body=b"apple"))
    ack = c.upload_local_record()
    ck("V3 a pocket-legal gift is accepted", ack is not None and ack.status == 0)
    junk_unused = letter(pid_of(h0), pid_of(r3), present=0x0123, font=L.MAIL_FONT_UNUSED)
    c.write_local_mail(2, junk_unused)
    ack = c.upload_local_record()
    ck("V4 a garbage gift inside an UNUSED slot (font 0xFF) is never read: accepted", ack is not None and ack.status == 0)


def m_peers(run, a, lt):
    ck = run.check
    host = run.host
    L.pump_sleep(0.3)
    rev_chk = a.rec_last[2]
    probe = a.build_txn_commit_bytes(K_MAIL, 1, D_NONE, 2, 0, base=(a.rec_last[1], a.rec_last[2]), aux_cond=1, aux_item=2)
    hs = L.FakeClient("HS", run.ip, run.port, context_flags=None, wait_snapshot=False, record_hello=False)
    hs.connect(timeout=3.0)
    off = len(host.log_text())
    hs.send_reliable(probe)
    L.pump_sleep(1.0)
    ck("X1 a HANDSHAKE peer (no IDENTITY) gets no answer to a MAIL_SEND; the host logs no TXN / MAIL activity",
       not hs.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type == L.PC_NETGAME_MSG_TXN_RESULT)
       and "[NET][TXN]" not in run.log(off) and "[NET][MAIL]" not in run.log(off) and hs.is_connected())
    hs.close()
    holder = run.ready("H", run.r3)
    park = L.FakeClient("PK", run.ip, run.port, player=L.resident_player(run.r3), context_flags=None, wait_snapshot=False, record_hello=False)
    park.connect(timeout=3.0)
    park.send_identity(town=L.resolve_host_town(run.ip, run.port), player=L.resident_player(run.r3))
    L.pump_sleep(0.5)
    off = len(host.log_text())
    park.send_reliable(probe)
    L.pump_sleep(1.0)
    ck("X2 a PARKED peer (claim on a live resident) gets no TXN_RESULT and causes no TXN / MAIL activity",
       not park.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_TXN_RESULT, L.PC_NETGAME_MSG_IDENTITY_ACK))
       and "[NET][TXN]" not in run.log(off) and "[NET][MAIL]" not in run.log(off))
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
    ck("X3 a peer claiming the HOST's own resident (refused at identity) gets no TXN_RESULT and nothing is mutated",
       not own.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type == L.PC_NETGAME_MSG_TXN_RESULT)
       and "TXN APPLIED" not in run.log(off) and "[NET][MAIL]" not in run.log(off))
    own.close()
    run.release(holder)
    ck("X4 nothing above changed A's mirror", a.rec_last[2] == rev_chk)


def m_mailbox_full(run, a):
    """Sends letters to resident 2's house (nobody connected as it) until the host answers MAILBOX_FULL; waits for the forced delivery after every send."""
    ck = run.check
    h0, r1, r2 = run.host_slot, run.r1, run.r2
    sent_hashes, house = [], None
    refused = None
    for i in range(14):
        lt = letter(pid_of(r2), pid_of(r1), body=bytes([0x41 + i]) * 12)
        off = len(run.log())
        rid, sent, r, ack = mk(run, a, 0, lt)
        if r is not None and r.outcome == APPLIED:
            cm = commits(run, off)
            house = cm[0][3] if cm else house
            sent_hashes.append(L.mail_hash32(L.mail_delivered_expected(lt, pid_of(r1))))
            _wait(lambda: "done, post office keeps 0 player letter(s)" in run.log(off), 6.0)
        else:
            refused = r
            break
    ck("MF1 the host refused with MAILBOX_FULL after %d accepted letters (the house mailbox has no free slot)" % len(sent_hashes),
       refused is not None and refused.outcome == REJECTED and refused.reason == R["MAILBOX_FULL"] and len(sent_hashes) >= 1)
    batch = [d for d in last_batch(run) if d[0] == house]
    ck("MF1 the host's mailbox of that house holds exactly 10 used letters (vanilla HOME_MAILBOX_SIZE) and every accepted letter is one of them",
       len(batch) == 10 and all(h in {d[2] for d in batch} for h in sent_hashes))
    n_po = run.n_log(r"force-delivery: post office holds")
    ck("MF1 the refused letter was NOT queued (the post office is empty: the delivery hook has nothing left to deliver)", "post office keeps 0 player letter(s)" in run.log())
    ck("MF1 the refused letter is STILL in the sender's slot (the client keeps it on a refusal)", L.mail_font(L.record_mail(a.rec_local, 0)) != L.MAIL_FONT_UNUSED)


def phase_main(run):
    ck = run.check
    a = run.ready("A", run.r1)
    ck("MAIL setup: A READY and SYNCED", a.rec_synced and a.rec_last is not None)
    lt, sent, r, ack, pre, rid, exp_hash, house0 = m_success(run, a)
    if r is None or r.outcome != APPLIED:
        return
    m_replay(run, a, lt, sent, r, pre, rid)
    a = m_mirror_after(run, a, r, pre)
    m_refusals(run, a)
    m_self_and_stale(run, a)
    m_malformed(run)
    c = run.ready("C", run.r3)
    m_validator(run, c)
    run.release(c)
    m_peers(run, a, lt)
    m_mailbox_full(run, a)


# ----------------------------------------------------------------------------------------------------------------------
# Phase P: museum_record host ownership (PUSH_HOSTFIELDS) + the PO_FULL desk
# ----------------------------------------------------------------------------------------------------------------------
def phase_museum_po(run):
    ck = run.check
    h0 = run.host_slot
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    c = run.ready("C", run.r3)
    ck("P setup: A, B, C READY and SYNCED", a.rec_synced and b.rec_synced and c.rec_synced)
    n0 = len(a.rec_pushes)
    base_rev = a.rec_last[2]
    before = a.rec_pushes[-1]["data"] if a.rec_pushes else a.rec_local
    ck("P1 the host test hook poked resident %d's museum_record (a simulated host consumption)" % run.r1,
       _wait(lambda: "--mail-test-poke-museum=%d: resident %d museum_record.stored_fossil_num" % (run.r1, run.r1) in run.log(), 12.0))
    push = a.wait_record_push(after=n0, timeout=8.0)
    ck("P1 the 1 Hz host-field watcher saw the museum_record digest change: 'host-consumed fields (... museum_record) changed -> rev N'",
       "museum_record) changed -> rev" in run.log())
    ck("P1 ... and pushed PUSH_HOSTFIELDS to the owner at rev + 1", push is not None and push["kind"] == L.PC_NETGAME_REC_KIND_PUSH_HOSTFIELDS and push["rev"] == base_rev + 1
       and push["digest_ok"])
    if push is not None:
        d0, d1 = bytes(before), bytes(push["data"])
        diff = [i for i in range(len(d1)) if d0[i] != d1[i]]
        lo, hi = L.REC_OFF_MUSEUM_RECORD, L.REC_OFF_MUSEUM_RECORD + L.REC_MUSEUM_RECORD_SIZE
        ck("P1 the ONLY bytes that changed between the previous mirror and the push are inside museum_record (0x18..0x65), at least one", len(diff) >= 1 and all(lo <= i < hi for i in diff))
    other = b.rec_pushes[-1]["rev"] if b.rec_pushes else None
    ck("P1 the other residents received no push for it", len(b.rec_pushes) == 1 and len(c.rec_pushes) == 1)
    # --- PO_FULL: the 5-slot desk (no delivery hook in this phase) ---
    senders = [a, b, c, a, b, c, a]
    ress = [run.r1, run.r2, run.r3, run.r1, run.r2, run.r3, run.r1]
    refused, got = None, []
    for k, (s, rs) in enumerate(zip(senders, ress)):
        lt = letter(pid_of(h0), pid_of(rs), body=bytes([0x50 + k]) * 8)
        off = len(run.log())
        rid, sent, r, ack = mk(run, s, 0, lt)
        if r is not None and r.outcome == APPLIED:
            got.append(commits(run, off)[0])
        else:
            refused = r
            break
    ck("PO1 the desk takes letters until it holds 5 (vanilla mPO_MAIL_STORAGE_SIZE) and then answers PO_FULL (%d accepted)" % len(got),
       refused is not None and refused.outcome == REJECTED and refused.reason == R["PO_FULL"] and len(got) >= 1 and got[-1][7] == 5)
    ck("PO1 po_sum rose by exactly one per accepted letter (strictly increasing)", all(got[i][7] == got[i - 1][7] + 1 for i in range(1, len(got))))
    n_c = len(commits(run))
    rid, sent, r2, _ack = mk(run, c, 0, letter(pid_of(h0), pid_of(run.r3), body=b"one more"))
    tx(run, "PO2 a further letter is still PO_FULL (nothing changed meanwhile)", r2, REJECTED, R["PO_FULL"])
    ck("PO2 the refused letters were not queued: commit count unchanged", len(commits(run)) == n_c)


# ----------------------------------------------------------------------------------------------------------------------
# Fault phases
# ----------------------------------------------------------------------------------------------------------------------
def phase_drop(run):
    ck = run.check
    a = run.ready("A", run.r1)
    h0, r1 = run.host_slot, run.r1
    lt = letter(pid_of(h0), pid_of(r1), present=APPLE, body=b"drop result")
    off = len(run.log())
    rid, sent, first, ack = mk(run, a, 4, lt, wait=1.6)
    L.pump_sleep(0.3)
    ck("F1 the APPLIED RESULT is lost (drop_result) but the host DID commit: no result at A, one commit line, fault logged",
       first is None and len(commits(run, off)) == 1 and "FAULT INJECTION FIRED mode=drop_result" in run.log())
    a.resend_txn(sent)
    res = a.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.4)
    ck("F1 the resend gets APPLIED / REPLAYED, rev == upload rev + 1, executed exactly once (one commit line, one replay line, one TXN APPLIED + replay)",
       res is not None and res.outcome == APPLIED and res.reason == R["REPLAYED"] and ack is not None and res.rev == ack.rev + 1
       and len(commits(run, off)) == 1 and run.replay_lines(rid) == 1)
    cm = commits(run, off)
    exp = L.mail_hash32(L.mail_delivered_expected(lt, pid_of(r1)))
    ck("F1 the post office holds exactly ONE copy of the letter (po_sum 1, the delivered hash is the expected one)", len(cm) == 1 and cm[0][5] == exp and cm[0][7] == 1)


def phase_kill(run):
    ck = run.check
    a = run.ready("A", run.r1)
    h0, r1 = run.host_slot, run.r1
    lt = letter(pid_of(h0), pid_of(r1), present=APPLE, body=b"kill after commit")
    off = len(run.log())
    rid, sent, r, ack = mk(run, a, 4, lt, wait=1.0)
    closed = TP.wait_closed(a, 4.0)
    L.pump_sleep(0.4)
    ck("K1 kill_peer_after_commit: the host dropped A after committing and sent NO result",
       closed and r is None and not a.txn_results_for(sent.seq) and "kill_peer_after_commit: peer" in run.log() and len(commits(run, off)) == 1)
    a.reconnect_and_ready(timeout=8.0)
    pushed = a.rec_pushes[-1] if a.rec_pushes else None
    e = empty_slot_bytes(pushed["data"]) if pushed else None
    ck("K2 the same-nonce reconnect is pushed the mirror: rev == upload rev + 1 and mail[4] is ALREADY cleared (the letter left the record at the commit)",
       pushed is not None and ack is not None and pushed["rev"] == ack.rev + 1 and e is not None and L.record_mail(pushed["data"], 4) == e)
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.3)
    ck("K3 the OLD COMMIT resent after the reconnect replays APPLIED / REPLAYED (journal survives the peer reset), NO second execution",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and len(commits(run, off)) == 1)
    run.release(a)
    a2 = run.client("A2", run.r1)
    a2.connect_and_ready(quiet=True)
    p2 = a2.rec_pushes[-1] if a2.rec_pushes else None
    e2 = empty_slot_bytes(p2["data"]) if p2 else None
    ck("K4 a NEW-PROCESS rejoin: the pushed record has mail[4] cleared, and the post office has exactly ONE copy (one commit, po_sum 1)",
       p2 is not None and e2 is not None and L.record_mail(p2["data"], 4) == e2 and len(commits(run, off)) == 1 and commits(run, off)[0][7] == 1)
    ck("K4 the host survived the dropped peer", run.host.alive())


PHASES = [
    ("ML-M", ["--mail-test-force-delivery"], phase_main),
    ("ML-P", None, phase_museum_po),   # extra args filled in main(): --mail-test-poke-museum=<first non-host resident>
    ("ML-drop", ["--txn-fault=drop_result:1:1"], phase_drop),
    ("ML-kill", ["--txn-fault=kill_peer_after_commit:1:1"], phase_kill),
]
PHASE_KEYS = {"M": "ML-M", "P": "ML-P", "F-drop": "ML-drop", "F-kill": "ML-kill"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11200)
    ap.add_argument("--only", default="", help="comma list of phase names (M, P, F-drop, F-kill)")
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
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "mail_protocol_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    first_client = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT][0]

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        for i, (tag, extra, body) in enumerate(PHASES):
            if only and tag not in only:
                continue
            if extra is None:
                extra = ["--mail-test-poke-museum=%d" % first_client]
            run_phase(results, ip, args.port + i, tag, extra, body, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
