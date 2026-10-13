#!/usr/bin/env python3
"""test_guest_letters_protocol.py - LETTERS TO GUESTS (guest directory, player -> guest letters, villager event letters, the museum letter), SCRIPTED clients vs a REAL host.

TIER: scripted FakeClient guests / residents against REAL host processes started by this script from a DISPOSABLE fixture pc\\build64\\bin_fixture4_gletters (made fresh from
the read-only bin_fixture4 with the pc\\build64_w3 exe; ports 12870+; host windows on the Samsung monitor, volume 1). The fixture save is snapshotted / restored between phases.

  D  GUEST DIRECTORY (TOWN_SVC_STATE service 7, 324 B): a resident and the guests receive it; it lists every guest of the town that receives town letters (raw LE
     PersonalID = the guest KEY, newest first), the reserved / unused bytes are zero, the digest is valid; a NEW guest appears with a higher seq
  A  RESIDENT -> GUEST (MAIL_SEND kind 12, host env AC_TEST_GUEST_MAIL_SEED=9): APPLIED, the letter (with an apple) goes straight into the guest's host-held mailbox (sender
     forced to the resident, receive font), the CONNECTED guest gets it as a house-0xFF MAILBOX_LETTER at once; the guest file is NOT written by the commit (a resident
     sender: it rides the next host save) and the next periodic host save writes it (v4, 10 letters); the mailbox is now FULL -> the next letter MAILBOX_FULL (stays in the
     sender's mirror); a byte-identical resend replays (no second delivery); after the guest takes one, the retry is APPLIED; a never-seen guest key -> NO_SUCH_ADDRESS
  G  GUEST -> GUEST: APPLIED, written DURABLY at once (both guest files: the sender's mail[] slot empty, the recipient's mailbox holds it); a guest's letter to itself ->
     NO_SUCH_ADDRESS
  E  VILLAGER EVENT LETTERS + MUSEUM (host env AC_TEST_GUEST_EVENT_MAIL=birthday,xmas,vtday,goodbye,museum, TEST-ONLY: one villager becomes the guest's best friend, the REAL
     passes run with a forced window / a noted departure / the museum counted complete): the guest (birthday Jun 15 in its record) receives the birthday card, the
     Christmas card, the Valentine present letter, the goodbye letter and the museum completion letter (sender MUSEUM, its two host-owned state_flags bits set in the
     .gst record, written at once)
     E2 RESTART (same save, the hook again with museum only): the museum letter is NOT sent twice (the persisted bits are the dedupe)
  K  KILL SWITCH (AC_MP_GUEST_MAIL=0): no service 7 at all, a resident's letter to a guest -> NO_SUCH_ADDRESS (as before), no event / museum letter for guests
Usage: python test_guest_letters_protocol.py [--port 12870] [--only D,A,G,E,K]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as TT  # noqa: E402

FIXTURE = "bin_fixture4_gletters"
W3_EXE = os.path.join(TT.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(TT.BUILD64, FIXTURE)
DISPLAY_ENV = {"AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1"}

import net_spike_lib as L  # noqa: E402
import test_guest_protocol as TG  # noqa: E402
import test_guest_mailbox_protocol as GM  # noqa: E402  (the independent .gst parser + helpers; its own fixture constant is not used)
from test_txn_protocol import APPLIED, REJECTED, R  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(TT.BUILD64, FIXTURE)  # GM's import set its own fixture: this test's stays authoritative
assert os.path.normcase(L.GAME_BIN_DIR) == os.path.normcase(os.path.join(TT.BUILD64, FIXTURE)), L.GAME_BIN_DIR

IP = "127.0.0.1"
APPLE = 0x2800
HOUSE_GUEST = 0xFF
SVC = L.PC_NETGAME_TS_GUESTDIR
DIRLEN = L.PC_NETGAME_TS_GUESTDIR_LEN
MUSEUM_BITS = 0xC0  # mPr_FLAG_MUSEUM_COMP_HANDBILL_SCHEDULED | _RECEIVED (bits 6, 7)


def gi(i):
    """ASCII-only test guests (TG.gid runs out of letters past 'Z')."""
    return L.guest_identity("LETTR%c" % (ord("A") + i), 0x4C01 + i, "PENPALS", 0x5C01)


def le_key(pid_be):
    """The raw little-endian PC PersonalID (as the directory blob carries it) of a 20-byte BE PersonalID."""
    return bytes(pid_be[:16]) + struct.pack("<HH", *struct.unpack(">HH", pid_be[16:20]))


def dir_keys(blob):
    n = blob[0]
    return [bytes(blob[4 + 20 * i:4 + 20 * (i + 1)]) for i in range(n)]


def dir_valid(blob):
    if len(blob) != DIRLEN or blob[0] > 16 or blob[1:4] != b"\x00\x00\x00":
        return False
    return all(not any(blob[4 + 20 * i:4 + 20 * (i + 1)]) for i in range(blob[0], 16))


def wait_dir(c, pred, timeout=10.0):
    return c.wait_ts_state(SVC, timeout=timeout, pred=pred)


def join(run, i, token=None, rec=None):
    c = run.guest("G%d" % i, gi(i), token=token, rec=rec)
    c.connect_and_ready(quiet=True)
    return c


# ================================================================================================================ D + A
def phase_a(run):
    ck = run.check
    A = join(run, 0)
    R1 = run.ready("R1", run.r1)
    sA = GM.gslot(A)
    pidA = L.guest_pid_be(gi(0))
    pid_r1 = L.record_from_gci(L.GAME_BIN_DIR, run.r1)[:20]
    # ---- D
    st = wait_dir(R1, lambda b: le_key(pidA) in dir_keys(b))
    ck("D1 the RESIDENT received the guest directory (service 7, %d B) listing guest A's key (raw LE PersonalID)" % DIRLEN, st is not None and len(st[1]) == DIRLEN)
    if st is not None:
        f, b = st
        ck("D1 the blob is well formed (count <= 16, reserved and unused bytes zero) and its digest is the FNV-1a32 of the blob", dir_valid(b) and f.digest == L.fnv1a32(b))
    sa = wait_dir(A, lambda b: le_key(pidA) in dir_keys(b))
    ck("D1 the guest itself receives the same directory (its client hides its own entry from the address book)", sa is not None)
    seq0 = st[0].seq if st is not None else 0
    B = join(run, 1)
    pidB = L.guest_pid_be(gi(1))
    st2 = wait_dir(R1, lambda b: le_key(pidB) in dir_keys(b) and le_key(pidA) in dir_keys(b))
    ck("D2 a NEW guest appears in the directory with a higher seq, newest first (B before A)", st2 is not None and st2[0].seq > seq0
       and dir_keys(st2[1]).index(le_key(pidB)) < dir_keys(st2[1]).index(le_key(pidA)))
    # ---- A
    GM.wait_letters(A, 9)
    p0 = GM.store_of(pidA)
    ck("A0 setup: guest A's mailbox holds the 9 seeded letters (file v4, mask 0x1FF)", p0 is not None and p0["mask"] == 0x1FF)
    L.pump_sleep(1.0)
    letter = L.build_mail_be(pidA, pid_r1, present=APPLE, body=b"RESIDENT-TO-GUEST-1")
    off = len(run.log())
    n0 = len(A.mbox_msgs())
    sent, r, _ack = R1.txn_mail_send(0, letter, rid=run.fresh_rid(), timeout=5.0)
    run.tx_ok("A1 a RESIDENT writes to a GUEST (with an apple)", r, APPLIED, R["NONE"])
    ck("A1 host log: committed straight into GUEST slot %d mailbox[9], the guest store is left to the next host save" % sA,
       re.search(r"\[NET\]\[GUEST\]\[MAIL\] host: peer \d+ resident \d+ MAIL_SEND slot 0 -> GUEST slot %d mailbox\[9\] present 0x2800 [^\n]*\(the guest store is written with the next host save\)" % sA,
                 run.log(off)) is not None)
    exp = L.mail_delivered_expected(letter, pid_r1)
    got = A.hub.wait_until(lambda: any(g.house == HOUSE_GUEST and g.mbox_idx == 9 and not GM.is_empty(g) and bytes(g.letter) == exp for g, _i in A.mbox_msgs()[n0:]), 6.0)
    ck("A1 the connected guest got it at once as a house-0xFF MAILBOX_LETTER (slot 9): sender = the resident (PLAYER), receive font, the apple inside", got)
    p1 = GM.store_of(pidA)
    ck("A1 the commit did NOT write the guest file (a resident sender: the letter rides the next host save, never ahead of the sender's own record)", p1 is not None and p1["mask"] == 0x1FF)
    ok = GM.wait_until(lambda: (lambda p: p is not None and p["mask"] == 0x3FF and p["mbox"][9] == exp)(GM.store_of(pidA)), 100.0)
    ck("A1 the next periodic host save wrote it (guest file v4, 10 letters, slot 9 = the letter)", ok and "periodic save OK" in run.log(off))
    l2 = L.build_mail_be(pidA, pid_r1, present=0, body=b"RESIDENT-TO-GUEST-2")
    _s, r2, _a = R1.txn_mail_send(1, l2, rid=run.fresh_rid(), timeout=5.0)
    run.tx_ok("A2 the guest's mailbox is FULL", r2, REJECTED, L.PC_NETGAME_TXN_REASON_MAILBOX_FULL)
    off3 = len(run.log())
    R1.resend_txn(sent)
    rr = R1.wait_txn_result(sent.seq, 3.0, nth=2)
    ck("A3 a byte-identical resend of the first letter replays APPLIED/REPLAYED; no second delivery", rr is not None and rr.outcome == APPLIED and rr.reason == R["REPLAYED"]
       and "-> GUEST slot" not in run.log(off3))
    _rid, _s, rt, _d = GM.take(run, A, 0)
    run.tx_ok("A4 the guest takes one letter (slot 0 frees up)", rt, APPLIED, R["NONE"])
    n1 = len(A.mbox_msgs())
    _s, r3, _a = R1.txn_mail_send(1, l2, rid=run.fresh_rid(), timeout=5.0, upload=False)
    run.tx_ok("A4 the refused letter (still in the resident's mirror) is sent again: APPLIED into the freed slot 0", r3, APPLIED, R["NONE"])
    got = A.hub.wait_until(lambda: any(g.house == HOUSE_GUEST and g.mbox_idx == 0 and bytes(g.letter) == L.mail_delivered_expected(l2, pid_r1) for g, _i in A.mbox_msgs()[n1:]), 6.0)
    ck("A4 ... and reached the guest (slot 0)", got)
    stranger = L.guest_pid_be(gi(9))
    l4 = L.build_mail_be(stranger, pid_r1, present=0, body=b"NOBODY")
    _s, r4, _a = R1.txn_mail_send(2, l4, rid=run.fresh_rid(), timeout=5.0)
    run.tx_ok("A5 a letter to a never-seen guest key", r4, REJECTED, L.PC_NETGAME_TXN_REASON_NO_SUCH_ADDRESS)
    # ---- G
    GM.wait_letters(B, 9)
    lg = L.build_mail_be(pidB, pidA, present=0, body=b"GUEST-TO-GUEST")
    offg = len(run.log())
    nb = len(B.mbox_msgs())
    _s, rg, _a = A.txn_mail_send(1, lg, rid=run.fresh_rid(), timeout=5.0)
    run.tx_ok("G1 a GUEST writes to another GUEST", rg, APPLIED, R["NONE"])
    sB = GM.gslot(B)
    ck("G1 host log: into guest slot %d's mailbox, both guest files written before the RESULT" % sB,
       re.search(r"peer \d+ guest \d+ MAIL_SEND slot 1 -> GUEST slot %d mailbox\[9\] [^\n]*\(guest store written\)" % sB, run.log(offg)) is not None)
    pa, pb = GM.store_of(pidA), GM.store_of(pidB)
    expg = L.mail_delivered_expected(lg, pidA)
    ck("G1 at once on disk: the recipient's file holds the letter (slot 9), the sender's file no longer has it in mail[1]",
       pb is not None and pb["mbox"][9] == expg and pa is not None and L.mail_font(L.record_mail(pa["record"], 1)) == L.MAIL_FONT_UNUSED)
    got = B.hub.wait_until(lambda: any(g.house == HOUSE_GUEST and g.mbox_idx == 9 and bytes(g.letter) == expg for g, _i in B.mbox_msgs()[nb:]), 6.0)
    ck("G1 the recipient guest got it at once (MAILBOX_LETTER house 0xFF)", got)
    ls = L.build_mail_be(pidA, pidA, present=0, body=b"MYSELF")
    _s, rs, _a = A.txn_mail_send(2, ls, rid=run.fresh_rid(), timeout=5.0)
    run.tx_ok("G2 a guest's letter to itself", rs, REJECTED, L.PC_NETGAME_TXN_REASON_NO_SUCH_ADDRESS)
    ck("A/G host alive, no INTERNAL error / CONFLICT", run.host.alive() and "*** INTERNAL" not in run.log() and "CONFLICT" not in run.log())


# ================================================================================================================ E
EV_TOKEN = {}


def phase_e(run, second=False):
    ck = run.check
    g = gi(5)
    pid = L.guest_pid_be(g)
    rec = L.record_set_bytes(L.fresh_guest_record_for(g), L.REC_OFF_BIRTHDAY, struct.pack(">HBB", 2001, 6, 15))
    A = join(run, 5, token=EV_TOKEN.get("tok"), rec=None if second else rec)
    s = GM.gslot(A)
    if not second:
        EV_TOKEN["tok"] = bytes(A.guest_token)
        ok = GM.wait_until(lambda: "museum completion letter delivered to guest slot %d" % s in run.log(), 20.0)
        box = GM.wait_letters(A, 5, timeout=15.0)
        lt = GM.letters(box)
        t = run.log()
        print("   INFO letters: %s" % {k: (L.mail_sender(v)[1], hex(L.mail_present(v)), v[0x30]) for k, v in lt.items()})
        ck("E1 the hook made a villager the guest's best friend (TEST-ONLY)", "is now guest slot %d's best friend" % s in t)
        ck("E1 BIRTHDAY: the guest's birthday (6/15) was in the window -> 1 card", re.search(r"guest slot %d's birthday \(6/15\) is in the window: 1 birthday card" % s, t) is not None)
        ck("E1 CHRISTMAS: Dec 25 was in the window -> the card", re.search(r"Christmas is in the window: 1 card for guest slot %d" % s, t) is not None)
        ck("E1 VALENTINE: 1 present letter for the guest", re.search(r"Valentine's Day: 1 present letter\(s\) for \d+ guest", t) is not None)
        ck("E1 GOODBYE: the departing villager's letter to the guest it knows", re.search(r"goodbye letter of villager 0x[0-9A-F]{4} delivered to guest slot %d" % s, t) is not None)
        ck("E1 MUSEUM: the completion letter", ok)
        ck("E1 the guest's mailbox received the 5 letters (house 0xFF), all addressed to the guest key", len(lt) == 5 and all(L.mail_recipient(x) == (pid, L.MAIL_NAME_PLAYER) for x in lt.values()))
        ck("E1 senders: 3 villager letters (NPC), the museum letter (MUSEUM), the Christmas card", sum(1 for x in lt.values() if L.mail_sender(x)[1] == L.MAIL_NAME_NPC) >= 3
           and sum(1 for x in lt.values() if L.mail_sender(x)[1] == L.MAIL_NAME_MUSEUM) == 1)
        ck("E1 the birthday card / Valentine letter / museum letter carry presents", sum(1 for x in lt.values() if L.mail_present(x) not in (0, 0xFFFF)) >= 3)
        p = GM.store_of(pid)
        ck("E1 the guest file was written at once by the museum letter: its record's state_flags carry the two host-owned museum bits",
           p is not None and (struct.unpack_from(">I", p["record"], L.REC_OFF_STATE_FLAGS)[0] & MUSEUM_BITS) == MUSEUM_BITS
           and any(L.mail_sender(x)[1] == L.MAIL_NAME_MUSEUM for x in p["mbox"] if any(x)))
        L.pump_sleep(2.0)
        ck("E1 host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())
    else:
        box = GM.wait_box(A)
        L.pump_sleep(5.0)
        lt = GM.letters(GM.guest_box(A))
        t = run.log()
        ck("E2 after a RESTART the hook ran again (museum only) but the museum letter was NOT sent a second time (the persisted state_flags bits)",
           "AC_TEST_GUEST_EVENT_MAIL=museum" in t and "museum completion letter delivered" not in t)
        ck("E2 the guest's mailbox holds exactly ONE museum letter", sum(1 for x in lt.values() if L.mail_sender(x)[1] == L.MAIL_NAME_MUSEUM) == 1)


# ================================================================================================================ K
def phase_k(run):
    ck = run.check
    A = join(run, 7)
    R1 = run.ready("R1", run.r1)
    pidA = L.guest_pid_be(gi(7))
    pid_r1 = L.record_from_gci(L.GAME_BIN_DIR, run.r1)[:20]
    L.pump_sleep(4.0)
    ck("K1 AC_MP_GUEST_MAIL=0: no client receives the guest directory (service 7 never exists), the other services do",
       R1.ts_latest(SVC) is None and A.ts_latest(SVC) is None and R1.ts_latest(L.PC_NETGAME_TS_POLICE) is not None)
    letter = L.build_mail_be(pidA, pid_r1, present=0, body=b"SWITCHED-OFF")
    _s, r, _a = R1.txn_mail_send(0, letter, rid=run.fresh_rid(), timeout=5.0)
    run.tx_ok("K2 a resident's letter to a guest is refused as before (no house)", r, REJECTED, L.PC_NETGAME_TXN_REASON_NO_SUCH_ADDRESS)
    L.pump_sleep(3.0)
    t = run.log()
    ck("K3 no event / museum letter for guests (the hook is armed, the pass never runs)", "museum completion letter" not in t and "best friend (NOT active" not in t
       and "guest mailbox is OFF" in t)


# ================================================================================================================ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12870)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    results = []
    if not os.path.isfile(W3_EXE):
        print("missing %s" % W3_EXE, file=sys.stderr)
        return 2
    dst = TT.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(dst, "AnimalCrossing.exe"))
    L.require_test_bin_dir()
    only = [x for x in args.only.split(",") if x]
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_letters_snapshot_%d" % os.getpid())
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
        e = {"AC_TEST_HOOKS": "1"}
        e.update(env)
        host, ok = GM.start_host(port, "letters_" + tag, extra, e, results)
        run = TG.GRun(IP, port, host, results, snap_gci, host_slot, residents[:3])
        try:
            if ok:
                body(run)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            L.check("[%s] phase raised %r" % (tag, exc), False, results)
        finally:
            GM.close(run, host, results, tag)
            if not keep:
                restore()

    try:
        if not only or "A" in only or "D" in only or "G" in only:
            phase("DAG", args.port, [], {"AC_TEST_GUEST_MAIL_SEED": "9"}, phase_a)
        if not only or "E" in only:
            phase("E", args.port + 1, [], {"AC_TEST_GUEST_EVENT_MAIL": "birthday,xmas,vtday,goodbye,museum"}, phase_e, keep=True)
            phase("E2", args.port + 2, [], {"AC_TEST_GUEST_EVENT_MAIL": "museum"}, lambda run: phase_e(run, second=True))
        if not only or "K" in only:
            phase("K", args.port + 3, [], {"AC_MP_GUEST_MAIL": "0", "AC_TEST_GUEST_EVENT_MAIL": "museum,xmas"}, phase_k)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
