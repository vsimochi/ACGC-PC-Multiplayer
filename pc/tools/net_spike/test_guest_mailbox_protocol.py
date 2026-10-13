#!/usr/bin/env python3
"""test_guest_mailbox_protocol.py - the GUEST MAILBOX ("Guest mailbox via the post office"), SCRIPTED clients vs a REAL host.

TIER: scripted FakeClient guests / residents against REAL host processes started by this script from a DISPOSABLE fixture
pc\\build64\\bin_fixture4_gmail (made fresh from the read-only bin_fixture4 with the pc\\build64_w3 exe; ports 12800+; host windows on the Samsung monitor,
volume 1). The fixture save is snapshotted and restored between phases; the live dirs are never used.

  S  (MAIL_SEND, kind 12)  a guest writes to a resident: APPLIED, the letter leaves the guest MIRROR (read through the guest record, no sender house), the host
     forces the vanilla delivery (--mail-test-force-delivery) and the resident receives it in its HOUSE mailbox with the sender forced to the guest key; the daily
     limit (host env AC_MP_GUEST_MAIL_PER_DAY=2): the 3rd letter is REJECTED MAIL_LIMIT (38) and stays in the guest's record; NO_SUCH_ADDRESS for a stranger
  T  (guest mailbox + MAIL_TAKE, kind 13; host env AC_TEST_GUEST_MAIL_SEED=3,2800)  the guest receives its 10 mailbox slots with house 0xFF (3 letters addressed
     to it, the first with an apple), a resident never receives a house-0xFF slot; take -> APPLIED (mirror mail[dst] == the letter, the slot pushed EMPTY, the
     .gst file is v4 and holds exactly the 2 remaining letters, CRC-valid); byte-identical resend -> REPLAYED once; other bytes -> CONFLICT; the emptied slot
     -> NO_SUCH_LETTER (+ re-push); a wrong hash -> MAIL_CHANGED (+ re-push); an occupied destination -> MAILBOX_FULL
     R  RESTART on the same save: the guest's new session shows exactly the 2 remaining mailbox letters and its pushed record holds the taken letter: every
        letter exists exactly once (mailbox XOR mail[])
  K  --txn-fault=kill_peer_after_commit:1:1: the take commits, the peer is dropped before the RESULT; the reconnect (same nonce) is pushed the record WITH the
     letter and the mailbox slot EMPTY; the resent COMMIT replays; the .gst file holds the letter once (mail[] only)
  V  villager replies (host env AC_TEST_GUEST_MAIL_SEED=10, AC_TEST_GUEST_MAIL_SEED_REPLY=1, AC_TEST_GUEST_MAIL_REPLY_EVERY_PASS=1): a FULL guest mailbox keeps the
     reply owed (send_reply stays set, nothing in the post office for the guest); after one take the next pass delivers the reply INTO THE GUEST MAILBOX
     (sender type NPC), never into the post office
  P  promotion (--promotion: a DEDICATED host on bin_fixture4_gmailp, resident slot 3 / house 3 free): the guest's mailbox letters (5) move into the new
     resident's house mailbox, re-keyed to the new PID; a forced save failure (--promote-fault fail_save) rolls everything back (the house mailbox, the guest file
     with its 5 letters intact) and the retry applies exactly once; the guest file is retired; the promoted resident receives them as MAILBOX_LETTERs (house 3).
     The overflow branch (post office, then carried letters) is NOT reachable here: the assigned house's mailbox is always empty (vanilla re-init) -> source audited
Usage: python test_guest_mailbox_protocol.py [--port 12800] [--only S,T,K,V]   |   python test_guest_mailbox_protocol.py --promotion [--port 12850]
"""
import argparse
import glob
import os
import re
import shutil
import struct
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as TT  # noqa: E402

PROMO = "--promotion" in sys.argv
FIXTURE = "bin_fixture4_gmailp" if PROMO else "bin_fixture4_gmail"
W3_EXE = os.path.join(TT.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(TT.BUILD64, FIXTURE)
DISPLAY_ENV = {"AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1"}

import net_spike_lib as L  # noqa: E402
import test_guest_protocol as TG  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
from test_txn_protocol import APPLIED, REJECTED, R  # noqa: E402

IP = "127.0.0.1"
K_TAKE = L.PC_NETGAME_TXN_KIND_MAIL_TAKE
APPLE = 0x2800
HOUSE_GUEST = 0xFF
GST_ENTRY = 72 + 0x2440
GST_V3 = 32 + 16 + GST_ENTRY + 4
EXT_LEN = 4 + 4 + 10 * 298
GST_V4 = GST_V3 + 4 + EXT_LEN
LIMIT = 2
TAKE_RE = r"\[NET\]\[MAIL\] host: peer \d+ guest %d MAIL_TAKE mailbox\[%d\] of house 255 -> mirror mail\[(\d+)\] present 0x([0-9A-F]{4}) letter_hash=0x([0-9A-F]{8}) mailbox_slot_cleared=1 committed \[TXN\] \(guest file written\)"


def fresh_fixture(promo=False):
    dst = TT.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(dst, "AnimalCrossing.exe"))  # this worker's build (pc\build64_w3), never the lead's
    if promo:
        import test_guest_lifecycle_protocol as LC  # noqa: E402  (make_free_slot: resident slot 3 / house 3 freed, checksum recomputed)
        LC.make_free_slot(dst)
    return dst


# ---------------------------------------------------------------------------------------------------------------- the per-guest store (independent parser)
def store_dir():
    if PROMO:
        return L.server_file("guests.dat")[:-len(".dat")]
    return os.path.join(L.GAME_BIN_DIR, "save", "mp", "guests")


def parse_gst(b):
    """Independent parser of a .gst file v3 / v4: {'ver', 'gen', 'id', 'pid', 'token', 'record', 'mask', 'mbox': [10 x 298 bytes]}; AssertionError on any violation."""
    assert b[:8] == b"ACMPGS1\x00"
    ver, flags, esz, gen, r1, r2 = struct.unpack("<6I", b[8:32])
    assert flags == 0 and esz == GST_ENTRY and r1 == 0 and r2 == 0 and ver in (3, 4), (ver, flags, esz)
    assert len(b) == (GST_V3 if ver == 3 else GST_V4), "size %d for version %d" % (len(b), ver)
    assert struct.unpack("<I", b[-4:])[0] == (zlib.crc32(b[:-4]) & 0xFFFFFFFF), "file crc32"
    e = b[48:48 + GST_ENTRY]
    rec = bytes(e[72:])
    assert struct.unpack("<I", e[52:56])[0] == (zlib.crc32(rec) & 0xFFFFFFFF)
    out = {"ver": ver, "gen": gen, "id": bytes(b[32:48]), "pid": bytes(e[4:24]), "token": bytes(e[24:40]), "record": rec, "mask": 0, "mbox": [bytes(298)] * 10}
    if ver == 4:
        x = 48 + GST_ENTRY
        ext_len, ext_ver, mask = struct.unpack("<3I", b[x:x + 12])
        assert ext_len == EXT_LEN and ext_ver == 1 and mask & ~0x3FF == 0
        boxes = [bytes(b[x + 12 + i * 298:x + 12 + (i + 1) * 298]) for i in range(10)]
        for i, lt in enumerate(boxes):
            assert bool(mask >> i & 1) == any(lt), "slot %d used flag vs bytes" % i
        out["mask"], out["mbox"] = mask, boxes
    return out


def store_of(pid):
    """The parsed primary .gst of the guest whose key is `pid` (20 BE bytes), or None."""
    for fn in glob.glob(os.path.join(store_dir(), "*.gst")):
        with open(fn, "rb") as f:
            p = parse_gst(f.read())
        if p["pid"] == pid:
            p["path"] = fn
            return p
    return None


def write_gst(path, p, record=None):
    """Re-signs a .gst file with a patched record (entry record CRC + file CRC), keeping the version / extension block."""
    with open(path, "rb") as f:
        b = bytearray(f.read())
    if record is not None:
        b[48 + 72:48 + 72 + 0x2440] = record
        struct.pack_into("<I", b, 48 + 52, zlib.crc32(bytes(record)) & 0xFFFFFFFF)
    struct.pack_into("<I", b, len(b) - 4, zlib.crc32(bytes(b[:-4])) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(bytes(b))


# ---------------------------------------------------------------------------------------------------------------- helpers
def gi(i):
    return TG.gid(40 + i)


def gslot(c):
    return c.token_msgs[-1][1].guest_slot


def join(run, i, token=None):
    c = run.guest("G%d" % i, gi(i), token=token)
    c.connect_and_ready(quiet=True)
    return c


def is_empty(g):
    return bool(g.flags & L.PC_NETGAME_MBOX_FLAG_EMPTY)


def guest_box(c):
    """{idx: latest MAILBOX_LETTER} of the GUEST mailbox (house 0xFF) of this connection."""
    return {k: g for k, g in c.mbox_state().items() if g.house == HOUSE_GUEST}


def wait_box(c, n=10, timeout=12.0):
    c.hub.wait_until(lambda: len(guest_box(c)) >= n, timeout)
    return guest_box(c)


def letters(box):
    return {k: bytes(g.letter) for k, g in box.items() if not is_empty(g)}


def wait_letters(c, n, timeout=15.0):
    """Waits until the guest mailbox shadow of `c` holds all 10 slots and >= n letters (the seed hook runs once the guest record is SYNCED)."""
    c.hub.wait_until(lambda: len(guest_box(c)) >= 10 and len(letters(guest_box(c))) >= n, timeout)
    return guest_box(c)


def wait_until(fn, timeout):
    t0 = time.monotonic()
    v = fn()
    while not v and time.monotonic() - t0 < timeout:
        L.pump_sleep(0.5)
        v = fn()
    return v


def start_host(port, tag, extra, env, results, dedicated=False):
    log = os.path.join(TT.LOG_DIR, "guest_mailbox_%s_host.log" % tag)
    e = dict(DISPLAY_ENV)
    e.update(env or {})
    if dedicated:
        first = not os.path.isfile(L.server_gci())
        h = L.HostProcess(port=port, extra_args=["--dedicated"] + list(extra), log_path=log, bin_dir=L.GAME_BIN_DIR, env=e, stdin_pipe=True, verbose=False, new_group=True).start()
        if first:
            h.send_line("1")  # first launch of the disposable fixture: "[1] Use this town" (adopt the fixture's legacy town by copy)
        ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
    else:
        slot = L.TEST_HOST_RESIDENT
        h = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(slot)] + list(extra), log_path=log, env=e).start()
        ok = h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=slot)
    L.check("[%s] host up (%s) %s" % (tag, "dedicated" if dedicated else "listen", " ".join(extra)), ok, results)
    if ok:
        L.resolve_host_town(IP, port)
    return h, ok


def close(run, host, results, tag):
    for c in run.clients:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    rc = host.stop()
    L.check("[%s] host did not crash (exit code before stop: %s)" % (tag, rc), rc is None, results)


def take(run, c, idx, dst=None, wait=4.0, **kw):
    if dst is None:
        dst = c.free_mail_slot()
    rid = run.fresh_rid()
    sent, r = c.txn_mail_take(idx, dst, rid=rid, timeout=wait, **kw)
    return rid, sent, r, dst


# ================================================================================================================ S
def phase_s(run):
    ck = run.check
    A = join(run, 0)
    R1 = run.ready("R1", run.r1)
    sA = gslot(A)
    pidA = L.guest_pid_be(gi(0))
    pid_r1 = L.record_from_gci(L.GAME_BIN_DIR, run.r1)[:20]
    L.pump_sleep(2.0)
    letter = L.build_mail_be(pid_r1, pidA, present=0, body=b"GUEST-TO-RESIDENT-1")
    off = len(run.log())
    sent, r, ack = A.txn_mail_send(0, letter, rid=run.fresh_rid(), timeout=5.0)
    run.tx_ok("S1 a GUEST writes to a resident (letter read from the guest MIRROR, no sender house)", r, APPLIED, R["NONE"])
    ck("S1 host log: the guest's MAIL_SEND committed into the post office and counted (1 of %d today)" % LIMIT,
       run.n_log(r"\[NET\]\[MAIL\] host: peer \d+ guest %d MAIL_SEND slot 0 -> house \d+ " % (4 + sA), off) == 1
       and run.n_log(r"guest slot %d sent a letter to house \d+ \(1 of %d today\)" % (sA, LIMIT), off) == 1)
    exp = L.mail_delivered_expected(letter, pidA)
    got = R1.hub.wait_until(lambda: any(not is_empty(g) and bytes(g.letter) == exp for g in R1.mbox_state().values() if g.house != HOUSE_GUEST), 20.0)
    ck("S1 the vanilla delivery (forced) put it into the RESIDENT's house mailbox: sender = the GUEST key (type PLAYER), receive font, text unchanged", got)
    ck("S1 the resident never receives a guest-mailbox (house 0xFF) slot", all(g.house != HOUSE_GUEST for g, _i in R1.mbox_msgs()))
    l2 = L.build_mail_be(pid_r1, pidA, present=0, body=b"GUEST-TO-RESIDENT-2")
    _s, r2, _a = A.txn_mail_send(1, l2, rid=run.fresh_rid(), timeout=5.0)
    run.tx_ok("S2 the 2nd letter of the day (limit %d)" % LIMIT, r2, APPLIED, R["NONE"])
    l3 = L.build_mail_be(pid_r1, pidA, present=0, body=b"GUEST-TO-RESIDENT-3")
    off3 = len(run.log())
    _s, r3, _a = A.txn_mail_send(2, l3, rid=run.fresh_rid(), timeout=5.0)
    run.tx_ok("S2 the 3rd letter of the day is over the host limit", r3, REJECTED, L.PC_NETGAME_TXN_REASON_MAIL_LIMIT)
    ck("S2 ... nothing reached the post office for it (no commit line), the host says why", run.n_log(r"MAIL_SEND slot 2 ->", off3) == 0 and "daily limit" in run.log(off3))
    stranger = L.guest_pid_be(gi(9))
    l4 = L.build_mail_be(stranger, pidA, present=0, body=b"NOBODY")
    _s, r4, _a = A.txn_mail_send(3, l4, rid=run.fresh_rid(), timeout=5.0)
    run.tx_ok("S3 a letter to somebody without a house on the host", r4, REJECTED, L.PC_NETGAME_TXN_REASON_NO_SUCH_ADDRESS)
    tok = bytes(A.guest_token)
    run.release(A)
    A2 = join(run, 0, token=tok)
    pr = A2.rec_pushes[-1]["data"] if A2.rec_pushes else None
    ck("S2/S3 a new session is pushed the record: the two SENT letters are gone, the refused ones (limit / no address) are still in mail[2] / mail[3]",
       pr is not None and L.mail_font(L.record_mail(pr, 0)) == L.MAIL_FONT_UNUSED and L.mail_font(L.record_mail(pr, 1)) == L.MAIL_FONT_UNUSED
       and L.record_mail(pr, 2) == l3 and L.record_mail(pr, 3) == l4)
    ck("S host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())


# ================================================================================================================ T (+ R)
def phase_t(run):
    ck = run.check
    A = join(run, 0)
    R1 = run.ready("R1", run.r1)
    sA, iA = gslot(A), 4 + gslot(A)
    pidA = L.guest_pid_be(gi(0))
    box = wait_letters(A, 3)
    lt = letters(box)
    ck("T1 the guest received all 10 slots of its GUEST mailbox (house 0xFF), 3 letters (the seed), digests valid", len(box) == 10 and sorted(lt) == [0, 1, 2]
       and all(g.digest == L.fnv1a32(bytes(g.letter)) for g in box.values()))
    ck("T1 every letter is addressed to the guest key (recipient type PLAYER), the first carries the apple", all(L.mail_recipient(x) == (pidA, L.MAIL_NAME_PLAYER) for x in lt.values())
       and L.mail_present(lt.get(0, bytes(298))) == APPLE)
    ck("T1 the host wrote the guest file as v4 with the 3 letters (mask 0x007)", (lambda p: p is not None and p["ver"] == 4 and p["mask"] == 0x7)(store_of(pidA)))
    L.pump_sleep(1.5)
    ck("T1 a resident never receives a house-0xFF slot", all(g.house != HOUSE_GUEST for g, _i in R1.mbox_msgs()))
    # ---- T2 take
    off = len(run.log())
    n0 = len(A.mbox_msgs())
    rid, sent, r, dst = take(run, A, 0)
    run.tx_ok("T2 MAIL_TAKE of guest mailbox slot 0", r, APPLIED, R["NONE"])
    m = re.search(TAKE_RE % (iA, 0), run.log(off))
    ck("T2 host log: the take committed into the guest MIRROR mail[%s] and the guest file was written before the RESULT" % dst, m is not None and int(m.group(1)) == dst)
    ok = A.hub.wait_until(lambda: any(g.mbox_idx == 0 and is_empty(g) and g.house == HOUSE_GUEST for g, _i in A.mbox_msgs()[n0:]), 5.0)
    ck("T2 the emptied slot reached the guest as one EMPTY house-0xFF MAILBOX_LETTER", ok)
    p = store_of(pidA)
    ck("T2 the .gst file (v4, CRC valid): the mailbox holds exactly slots 1 and 2, the record's mail[dst] holds the taken letter (one file, one write)",
       p is not None and p["ver"] == 4 and p["mask"] == 0x6 and L.record_mail(p["record"], dst) == lt[0])
    # ---- T3 replay / conflict
    off2 = len(run.log())
    A.resend_txn(sent)
    rr = A.wait_txn_result(sent.seq, 3.0, nth=2)
    ck("T3 a byte-identical resend replays APPLIED/REPLAYED; no second commit", rr is not None and rr.outcome == APPLIED and rr.reason == R["REPLAYED"]
       and len(re.findall(TAKE_RE % (iA, 0), run.log(off2))) == 0)
    bad = bytearray(sent.raw)
    bad[18] ^= 1
    A.send_txn_commit(K_TAKE, rid, L.PC_NETGAME_TXN_DEST_NONE, 0, 0, raw=bytes(bad))
    rc = A.wait_txn_result(sent.seq, 3.0, nth=3)
    ck("T3 same (nonce, seq) other bytes -> CONFLICT", rc is not None and rc.outcome == REJECTED and rc.reason == R["CONFLICT"])
    # ---- T4 refusals
    n1 = len(A.mbox_msgs())
    _rid, _s, rn, _d = take(run, A, 0, letter_be=lt[0])
    run.tx_ok("T4 taking the (now empty) slot 0 again with the old letter", rn, REJECTED, R["NO_SUCH_LETTER"])
    ok = A.hub.wait_until(lambda: any(g.mbox_idx == 0 and g.house == HOUSE_GUEST for g, _i in A.mbox_msgs()[n1:]), 4.0)
    ck("T4 ... the host re-pushes slot 0 (a higher seq, still EMPTY)", ok)
    n2 = len(A.mbox_msgs())
    _rid, _s, rm, _d = take(run, A, 1, hash24=(L.mail_hash24(lt[1]) ^ 0x5A5A5A))
    run.tx_ok("T4 a take of slot 1 claiming another letter (wrong hash)", rm, REJECTED, R["MAIL_CHANGED"])
    ok = A.hub.wait_until(lambda: any(g.mbox_idx == 1 and g.house == HOUSE_GUEST and bytes(g.letter) == lt[1] for g, _i in A.mbox_msgs()[n2:]), 4.0)
    ck("T4 ... the host re-pushes slot 1 (the real letter)", ok)
    _rid, _s, rf, _d = take(run, A, 1, dst=dst)
    run.tx_ok("T4 a take into the occupied mail[%d]" % dst, rf, REJECTED, L.PC_NETGAME_TXN_REASON_MAILBOX_FULL)
    p2 = store_of(pidA)
    ck("T4 after the refusals the guest file is unchanged (mailbox 1 + 2, the letter in mail[dst])", p2 is not None and p2["mask"] == 0x6 and L.record_mail(p2["record"], dst) == lt[0])
    tok = bytes(A.guest_token)
    for c in (A, R1):
        run.release(c)
    ck("T host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())
    return dict(tok=tok, lt=lt, dst=dst, sA=sA)


def phase_r(run, info):
    ck = run.check
    pidA = L.guest_pid_be(gi(0))
    A = join(run, 0, token=info["tok"])
    box = wait_letters(A, 2)
    pr = A.rec_pushes[-1]["data"] if A.rec_pushes else None
    lt = info["lt"]
    ck("R after a host RESTART the guest mailbox holds exactly the 2 remaining letters (slots 1, 2)", letters(box) == {1: lt[1], 2: lt[2]})
    in_mail = [i for i in range(L.REC_MAIL_COUNT) if pr is not None and L.record_mail(pr, i) == lt[0]]
    ck("R the pushed record holds the taken letter exactly once (mail[%d]) and it is NOT in the mailbox: mailbox XOR mail[]" % info["dst"],
       in_mail == [info["dst"]] and lt[0] not in letters(box).values())
    ck("R the guest file agrees (v4, mask 0x006)", (lambda p: p is not None and p["mask"] == 0x6)(store_of(pidA)))


# ================================================================================================================ K
def phase_k(run):
    ck = run.check
    A = join(run, 1)
    sA, iA = gslot(A), 4 + gslot(A)
    pidA = L.guest_pid_be(gi(1))
    box = wait_letters(A, 3)
    lt = letters(box)
    ck("K setup: the seeded guest mailbox reached the guest (3 letters)", sorted(lt) == [0, 1, 2])
    off = len(run.log())
    rid, sent, first, dst = take(run, A, 0, wait=1.5)
    closed = TP.wait_closed(A, 4.0)
    ck("K kill_peer_after_commit: the take was committed (guest file written) and the peer dropped WITHOUT a result", first is None and closed
       and "kill_peer_after_commit: peer" in run.log(off) and re.search(TAKE_RE % (iA, 0), run.log(off)) is not None)
    L.pump_sleep(0.8)
    A.reconnect_and_ready(timeout=8.0)
    box2 = wait_box(A)
    pr = A.rec_pushes[-1]["data"] if A.rec_pushes else None
    ck("K the same-nonce reconnect is pushed the record WITH the letter (mail[%d]) and the mailbox shows slot 0 EMPTY" % dst,
       pr is not None and L.record_mail(pr, dst) == lt[0] and 0 not in letters(box2) and sorted(letters(box2)) == [1, 2])
    off2 = len(run.log())
    A.resend_txn(sent)
    rep = A.wait_txn_result(sent.seq, 3.0)
    ck("K the old COMMIT resent replays APPLIED/REPLAYED, no second commit", rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"]
       and re.search(TAKE_RE % (iA, 0), run.log(off2)) is None)
    p = store_of(pidA)
    ck("K the guest file holds the letter exactly once: in mail[dst], not in the mailbox (mask 0x006)", p is not None and p["mask"] == 0x6 and L.record_mail(p["record"], dst) == lt[0]
       and lt[0] not in p["mbox"])


# ================================================================================================================ V
def phase_v(run):
    ck = run.check
    A = join(run, 2)
    sA = gslot(A)
    box = wait_letters(A, 10)
    ck("V setup: the guest mailbox is FULL (10 seeded letters)", len(letters(box)) == 10)
    ok = wait_until(lambda: "a reply is still owed" in run.log() and ("guest slot %d mailbox is FULL" % sA) in run.log(), 15.0)
    ck("V1 the villager owes a reply but the mailbox is FULL: the reply is NOT delivered, send_reply stays set (host: 'mailbox is FULL', 'a reply is still owed')", ok)
    ck("V1 nothing was generated into the post office for the guest (no resident remail line names it, no reply delivered)",
       "villager-reply pass for guest slot %d (peer" % sA not in run.log())
    n0 = len(A.mbox_msgs())
    _rid, _s, r, _d = take(run, A, 0)
    run.tx_ok("V2 the guest takes one letter (slot 0 frees up)", r, APPLIED, R["NONE"])
    ok = A.hub.wait_until(lambda: any(g.mbox_idx == 0 and g.house == HOUSE_GUEST and not is_empty(g) for g, _i in A.mbox_msgs()[n0:]), 12.0)
    g0 = guest_box(A).get(0)
    ck("V2 the next pass delivered the villager's reply INTO THE GUEST MAILBOX slot 0 (sender type NPC, recipient = the guest key, receive font)",
       ok and g0 is not None and not is_empty(g0) and L.mail_sender(bytes(g0.letter))[1] == L.MAIL_NAME_NPC
       and L.mail_recipient(bytes(g0.letter)) == (L.guest_pid_be(gi(2)), L.MAIL_NAME_PLAYER))
    ck("V2 host log: 'villager-reply pass for guest slot %d ... 1 letter(s) delivered into its mailbox'" % sA,
       re.search(r"villager-reply pass for guest slot %d \(peer \d+\): 1 letter\(s\) delivered into its mailbox" % sA, run.log()) is not None)
    p = store_of(L.guest_pid_be(gi(2)))
    ck("V2 the guest file holds the reply (10 letters again, v4)", p is not None and p["mask"] == 0x3FF)


# ================================================================================================================ P (promotion, dedicated)
GCI_MAIN = None


def phase_p(results, port):
    import make_four_resident_fixture as MF  # noqa: E402
    import test_guest_lifecycle_protocol as LC  # noqa: E402
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    fresh_fixture(promo=True)
    L.server_wipe_sidecars()
    rig = LC.Rig(results)
    g = L.guest_identity("MAILGST", 0x4D11, "HOMETWN", 0x5E21)
    pid = L.guest_pid_be(g)
    # 1. the guest joins once (fresh record) and gets 5 seeded letters; the host stops gracefully
    h, ok = start_host(port, "p0", [], {"AC_TEST_GUEST_MAIL_SEED": "5,2800"}, results, dedicated=True)
    if not ok:
        return
    rig.port = port
    try:
        c = rig.join("MG", g, bytes(L.fresh_guest_record_for(g)))
        tok = bytes(c.guest_token)
        wait_letters(c, 5)
        L.pump_sleep(2.0)
        rig.release(c)
        L.pump_sleep(5.0)
    finally:
        rig.closeall()
        ck("P0 graceful stop", rig.stop(h) == 0)
    st = store_of(pid)
    ck("P0 the guest file is v4 with 5 letters (mask 0x01F)", st is not None and st["ver"] == 4 and st["mask"] == 0x1F)
    if st is None:
        return
    mbox5 = [st["mbox"][i] for i in range(5)]
    rec = bytearray(st["record"])
    struct.pack_into(">I", rec, L.REC_OFF_WALLET, 25000)
    write_gst(st["path"], st, record=bytes(rec))
    ck("P0 the guest's stored wallet patched to 25000 (record + file CRC re-signed; the mailbox block kept)", (lambda p: p is not None and p["mask"] == 0x1F
                                                                                                            and struct.unpack_from(">I", p["record"], L.REC_OFF_WALLET)[0] == 25000)(store_of(pid)))
    # 2. (the free house's mailbox is EMPTY when it is assigned: the vanilla house (re)initialisation clears it -- a GCI pre-fill does not survive, observed in the first run --
    #    so all 5 letters fit the house mailbox and the post office / carried-letter overflow of pc_mp_promote_migrate_mailbox is unreachable with <= 10 guest letters: source audited)
    hm = MF.MAIN_OFF + MF.HOME_OFF + 3 * MF.HOME_STRIDE + 0x1A30
    # 3. a forced save failure: everything rolls back; the retry applies once
    h, ok = start_host(port + 1, "p1", ["--resident-tokens", "tofu", "--promote-fault", "fail_save"], {}, results, dedicated=True)
    if not ok:
        return
    rig.port = port + 1
    try:
        a = rig.join("MG", g, bytes(L.fresh_guest_record_for(g)), token=tok)
        L.pump_sleep(8.0)
        ck("P1 the guest holds the stored wallet 25000 and its 5 mailbox letters", L.record_inventory(a.rec_local)[2] == 25000 and len(letters(wait_letters(a, 5))) == 5)
        sent = LC.buy(a, rid=1)
        r = a.wait_txn_result(sent.seq, 10.0)
        ck("P1 `fail_save`: the purchase is REJECTED and rolled back", r is not None and r.outcome == REJECTED)
        L.pump_sleep(1.5)
        LC.say(h, "save", settle=3.0, timeout=12.0)
        L.pump_sleep(3.0)
        gci = LC.raw(LC.gci_path())
        hm_now = [bytes(gci[hm + k * 298:hm + (k + 1) * 298]) for k in range(10)]
        ck("P1 after the rollback (+ a console save) house 3 is unowned again and its mailbox holds NONE of the guest's letters", bytes(gci[MF.MAIN_OFF + MF.HOME_OFF + 3 * MF.HOME_STRIDE:
           MF.MAIN_OFF + MF.HOME_OFF + 3 * MF.HOME_STRIDE + 20]) != pid and not any(x in hm_now for x in mbox5) and not any(pid in x[0:20] for x in hm_now))
        st1 = store_of(pid)
        ck("P1 the guest file still holds the 5 letters (nothing lost, nothing moved)", st1 is not None and st1["mask"] == 0x1F and [st1["mbox"][i] for i in range(5)] == mbox5)
        sent = LC.buy(a, rid=2)
        r = a.wait_txn_result(sent.seq, 15.0)
        ck("P2 the retry APPLIES", r is not None and r.outcome == APPLIED)
        L.pump_sleep(2.0)
        t = h.log_text()
        ck("P2 host log: '[PC] migrate: guest mailbox letters=5 -> house mailbox 5, post office 0, carried letters 0' (once for the rolled back try, once for the retry)",
           t.count("[PC] migrate: guest mailbox letters=5 -> house mailbox 5, post office 0, carried letters 0") == 2 and t.count("the in-memory change was ROLLED BACK") == 1)
        mem = LC.parse_members(LC.mpath("members.dat"))
        k1 = [e for e in mem if e["kind"] == 1]
        npid = bytes(k1[0]["pid"]) if len(k1) == 1 else None
    finally:
        rig.closeall()
        ck("P2 graceful stop", rig.stop(h) == 0)
    if npid is None:
        ck("P2 the new resident PID is known (members.dat)", False)
        return
    gci = LC.raw(LC.gci_path())

    def rekey(lt):
        b = bytearray(lt)
        if b[0:20] == pid:
            b[0:20] = npid
        if b[0x16:0x2A] == pid:
            b[0x16:0x2A] = npid
        return bytes(b)
    exp = [rekey(x) for x in mbox5]
    house = [bytes(gci[hm + k * 298:hm + (k + 1) * 298]) for k in range(10)]
    po_off = MF.MAIN_OFF + 0x20694 + 8  # Save_t.post_office.mail[5] (PostOffice_c: 3 x s16 + u16, then the letters)
    po = [bytes(gci[po_off + k * 298:po_off + (k + 1) * 298]) for k in range(5)]
    ck("P3 the GCI: house 3's mailbox slots 0..4 = the 5 guest letters RE-KEYED to the new PID (recipient; the villager sender stays), in order", house[:5] == exp)
    ck("P3 the GCI: house 3 is owned by the new PID", bytes(gci[MF.MAIN_OFF + MF.HOME_OFF + 3 * MF.HOME_STRIDE:MF.MAIN_OFF + MF.HOME_OFF + 3 * MF.HOME_STRIDE + 20]) == npid)
    ck("P3 every letter exists exactly once in the town (house + post office) and nowhere under the guest key", sum(house.count(x) + po.count(x) for x in exp) == 5
       and not any(pid in x[0:20] for x in house + po))
    ck("P3 the guest entry is retired: no live .gst for the guest key (its file was moved aside, kept)", store_of(pid) is None
       and any(".removed-" in n for n in os.listdir(store_dir())))
    # 4. the NEW resident (claim with the handed-over resident token) gets its house mailbox through the normal MAILBOX_LETTER path
    rtok = bytes(k1[0]["token"]) if len(k1) == 1 else None
    h, ok = start_host(port + 2, "p2", ["--resident-tokens", "tofu"], {}, results, dedicated=True)
    if not ok or rtok is None:
        return
    rig.port = port + 2
    try:
        ident = L.PlayerIdentity(npid[:8], struct.unpack(">H", npid[16:18])[0], 1)
        rc = rig.resident("RES3", ident, 3, rtok)
        got = rc.hub.wait_until(lambda: sum(1 for g_ in rc.mbox_state().values() if g_.house == 3 and not is_empty(g_)) >= 5, 15.0)
        box = {k: bytes(g_.letter) for k, g_ in rc.mbox_state().items() if g_.house == 3 and not is_empty(g_)}
        ck("P4 the promoted RESIDENT receives the 5 letters through the normal MAILBOX_LETTER path (house 3, the re-keyed bytes)", got and sorted(box) == [0, 1, 2, 3, 4]
           and [box[i] for i in range(5)] == exp)
    finally:
        rig.closeall()
        ck("P4 graceful stop", rig.stop(h) == 0)


# ================================================================================================================ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12850 if PROMO else 12800)
    ap.add_argument("--only", default="")
    ap.add_argument("--promotion", action="store_true")
    args = ap.parse_args()
    results = []
    if not os.path.isfile(W3_EXE):
        print("missing %s" % W3_EXE, file=sys.stderr)
        return 2
    if PROMO:
        try:
            phase_p(results, args.port)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            L.check("[P] phase raised %r" % exc, False, results)
        return L.summary_and_exit_code(results)
    fresh_fixture()
    L.require_test_bin_dir()
    only = [x for x in args.only.split(",") if x]
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_mailbox_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    def phase(tag, port, extra, env, body, keep=False):
        print("=" * 72 + "\n[%s]" % tag)
        host, ok = start_host(port, tag, extra, env, results)
        run = TG.GRun(IP, port, host, results, snap_gci, host_slot, residents[:3])
        out = None
        try:
            if ok:
                out = body(run)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            L.check("[%s] phase raised %r" % (tag, exc), False, results)
        finally:
            close(run, host, results, tag)
            if not keep:
                restore()
        return out

    try:
        if (not only or "S" in only):
            phase("S", args.port, ["--mail-test-force-delivery"], {"AC_MP_GUEST_MAIL_PER_DAY": str(LIMIT)}, phase_s)
        if (not only or "T" in only):
            info = phase("T", args.port + 1, [], {"AC_TEST_GUEST_MAIL_SEED": "3,2800"}, phase_t, keep=True)
            if info is not None:
                phase("R", args.port + 2, [], {}, lambda run: phase_r(run, info))  # same save, no seed: the restart
            else:
                restore()
        if (not only or "K" in only):
            phase("K", args.port + 3, ["--txn-fault=kill_peer_after_commit:1:1"], {"AC_TEST_GUEST_MAIL_SEED": "3"}, phase_k)
        if (not only or "V" in only):
            phase("V", args.port + 4, [], {"AC_TEST_GUEST_MAIL_SEED": "10", "AC_TEST_GUEST_MAIL_SEED_REPLY": "1", "AC_TEST_GUEST_MAIL_REPLY_EVERY_PASS": "1"}, phase_v)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
