#!/usr/bin/env python3
"""test_guest_g2_protocol.py - Guests G2.1 (HOST half): the host-enforced EMPTY ECONOMY of a NEW guest's FIRST (MIGRATE) upload.

TIER: PROTOCOL TESTED -- a REAL host process (`AnimalCrossing.exe --host --bootstrap-resident 0 ...`, the disposable pc\\build64\\bin_fixture4 named by
NET_SPIKE_GAME_BIN; the live / protected dirs are refused by net_spike_lib) and SCRIPTED FakeClient guests / residents (the test double of the client wire). No real
game client runs here (that is test_guest_real_client.py / test_guest_g2_real.py). The fixture save dir is snapshotted at start and restored at the end.

A NEW guest's first-contact record (pcnetgame_rec_process_upload, kind MIGRATE_UPLOAD, guest slot >= PLAYER_NUM) must be a legal FRESH character
(pc_mp_guest_record_fresh_check: identity / appearance / designs only; economy + progress = the vanilla empty defaults):
  A  each cheating variant (a pocket item, wallet, bank, loan != 100, a letter with a gift, a post office gift flag in state_flags, a 4th catalog bit) is REFUSED:
     RECORD_ACK INVALID_FIELD with detail low byte 13 (GUEST_NOT_FRESH) and the PC_MP_FRESH_BAD_* reason in the high byte, a clear host log line, NO push, the
     guests.dat entry is still the host's blank rev-0 record and UNCONFIRMED (nothing stored); the SAME connection can then send the fresh record, which is
     APPLIED (rev 1, pushed back byte for byte) -- the refusal wedges nothing
  B  a legal fresh record with everything a client may legitimately choose or have changed (the 3 starter catalog bits, a date dependent calendar tail, another
     gender / face / shirt, other designs) is ACCEPTED and stored byte for byte
  C  a RETURNING guest (rev > 0, token) is unaffected: it adopts the stored record (no MIGRATE) and its later upload with money and an item is APPLIED
  D  RESIDENTS are unaffected: a resident (fixture slot with items and a loan of 17400) still imports its own populated record by the same MIGRATE path
  the refusal reasons are parsed from pc_mp_guests.h (not hard coded twice)
Usage: python test_guest_g2_protocol.py [--port 11700]
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
import test_txn_protocol as TP

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
SAVE_DIR_REL = "save"
FIELD_GUEST_NOT_FRESH = 13
ITM_APPLE = 0x2800


def fresh_reasons():
    """PC_MP_FRESH_* enum of pc_mp_guests.h -> {name: value}"""
    with open(os.path.join(PC, "include", "pc_mp_guests.h"), encoding="utf-8") as f:
        t = f.read()
    body = re.search(r"enum \{\s*(PC_MP_FRESH_OK = 0,.*?)\};", t, re.S).group(1)
    names = re.findall(r"(PC_MP_FRESH_[A-Z_]+)", re.sub(r"/\*.*?\*/", "", body, flags=re.S))
    return {n: i for i, n in enumerate(names)}


def set_gift_letter(rec, slot=0, item=ITM_APPLE):
    o = L.REC_OFF_MAIL + slot * L.REC_MAIL_SIZE
    r = bytearray(rec)
    r[o + 0x2E] = 0                                  # content.font = used letter
    struct.pack_into(">H", r, o + 0x2C, item)       # present
    return bytes(r)


def catalog_bits(rec, n):
    r = bytearray(rec)
    for k in range(n):
        r[L.REC_OFF_CATALOG + 4 + 17 * k] |= 1 << (k % 8)
    return bytes(r)


def entry_of(g):
    pf = TG.parse_guests(TG.guests_path())
    return next((e for e in pf["e"] if e["present"] and e["pid"] == L.guest_pid_be(g)), None)


def wait_entry(g, pred, timeout=10.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        try:
            e = entry_of(g)
        except (AssertionError, OSError):
            e = None
        if e is not None and pred(e):
            return e
        time.sleep(0.4)
    return None


def body(run, results):
    ck = run.check
    R = fresh_reasons()
    ck("the PC_MP_FRESH_* reason enum was parsed from the header (OK = 0, POCKET / WALLET / BANK / LOAN / MAIL_GIFT / CATALOG / PROGRESS present)",
       R["PC_MP_FRESH_OK"] == 0 and all(k in R for k in ("PC_MP_FRESH_BAD_POCKET", "PC_MP_FRESH_BAD_WALLET", "PC_MP_FRESH_BAD_BANK", "PC_MP_FRESH_BAD_LOAN",
                                                          "PC_MP_FRESH_BAD_MAIL_GIFT", "PC_MP_FRESH_BAD_CATALOG", "PC_MP_FRESH_BAD_PROGRESS")))
    variants = [
        ("a pocket item", lambda r: L.record_set_u16(r, L.REC_OFF_POCKETS, ITM_APPLE), "PC_MP_FRESH_BAD_POCKET", "a pocket slot is not empty"),
        ("wallet 500", lambda r: L.record_set_u32(r, L.REC_OFF_WALLET, 500), "PC_MP_FRESH_BAD_WALLET", "wallet is not 0"),
        ("bank 1000", lambda r: L.record_set_u32(r, L.REC_OFF_BANK, 1000), "PC_MP_FRESH_BAD_BANK", "bank account is not 0"),
        ("a letter with a gift", set_gift_letter, "PC_MP_FRESH_BAD_MAIL_GIFT", "a letter carries a gift"),
        ("loan 0", lambda r: L.record_set_u32(r, L.REC_OFF_LOAN, 0), "PC_MP_FRESH_BAD_LOAN", "loan is not the vanilla 100"),
        ("a post office gift flag in state_flags", lambda r: L.record_set_u32(r, L.REC_OFF_STATE_FLAGS, 1 | (1 << 5)), "PC_MP_FRESH_BAD_PROGRESS", "game progress"),
        ("4 catalog bits", lambda r: catalog_bits(r, 4), "PC_MP_FRESH_BAD_CATALOG", "catalog progress"),
    ]
    before_lines = len(re.findall(r"first MIGRATE REFUSED \(not a fresh character\)", run.log()))
    for i, (name, mut, rname, rtext) in enumerate(variants):
        g = TG.gid(i)
        fresh = L.fresh_guest_record_for(g)
        bad = mut(fresh)
        ck("A%d sanity: the cheating record differs from the fresh one only by the cheat (len ok, not equal)" % i, len(bad) == len(fresh) == 0x2440 and bad != fresh)
        off = len(run.log())
        c = run.guest("A%d" % i, g, rec=bad, record_wait=False)
        c.connect_and_ready(quiet=True)
        ack = c.wait_record_ack(status=L.PC_NETGAME_REC_ACK_INVALID_FIELD, timeout=8.0)
        lt = run.log()[off:]
        ck("A%d [%s] the first MIGRATE is REFUSED: RECORD_ACK INVALID_FIELD, detail low byte %d (GUEST_NOT_FRESH), high byte = %s (%d)" % (i, name, FIELD_GUEST_NOT_FRESH, rname, R[rname]),
           ack is not None and (ack.detail & 0xFF) == FIELD_GUEST_NOT_FRESH and (ack.detail >> 8) == R[rname])
        ck("A%d host log names the guest slot, 'not a fresh character' and the reason (%s); nothing stored" % (i, rtext),
           re.search(r"guest slot %d first MIGRATE REFUSED \(not a fresh character\): [^\n]*%s" % (i, re.escape(rtext)), lt) is not None
           and "nothing stored, the guests.dat entry is unchanged" in lt)
        ck("A%d no PUSH_FULL was sent and the MIGRATE was not APPLIED (host log has no 'MIGRATE xfer 1 APPLIED' for this peer)" % i,
           not c.rec_pushes and "MIGRATE xfer 1 APPLIED" not in lt)
        e = entry_of(g)
        ck("A%d guests.dat: the entry exists (token minted at first contact) but is UNCONFIRMED, rev 0, and holds the host's BLANK record (the cheating record was not stored)" % i,
           e is not None and e["confirmed"] == 0 and e["rev"] == 0 and e["record"] == L.guest_blank_record(g))
        # the same connection can still deliver the legal record
        ep = c.rec_migrate_epoch
        L.pump_sleep(0.3)
        x = c.upload_record(fresh, base=(ep, 0), kind=L.PC_NETGAME_REC_KIND_MIGRATE_UPLOAD)
        ok_ack = c.wait_record_ack(xfer_id=x, status=L.PC_NETGAME_REC_ACK_APPLIED, timeout=8.0)
        push = c.wait_record_push(after=0, timeout=8.0)
        ck("A%d the refusal wedges nothing: the SAME connection then sends the fresh record -> APPLIED rev 1 and the push carries it byte for byte" % i,
           ok_ack is not None and ok_ack.rev == 1 and push is not None and push["data"] == fresh and push["rsv"] == L.PC_NETGAME_REC_CLASS_GUEST)
        run.release(c)
        e2 = wait_entry(g, lambda d: d["rev"] == 1 and d["record"] == fresh)
        ck("A%d after the legal upload guests.dat holds exactly the fresh record at rev 1 (the early save on disconnect)" % i, e2 is not None)
    after_lines = len(re.findall(r"first MIGRATE REFUSED \(not a fresh character\)", run.log()))
    ck("A exactly %d refusals were logged for %d cheating variants (the legal retries and every other path logged none)" % (len(variants), len(variants)),
       after_lines - before_lines == len(variants))

    # ---- B: every legitimately client-chosen / changed value is accepted ----
    gb = TG.gid(7)
    legal = L.fresh_guest_record_for(gb, gender=1, face=6)
    legal = catalog_bits(legal, 3)
    legal = bytearray(legal)
    for k in range(L.REC_OFF_CALENDAR, L.REC_OFF_CALENDAR + 0x68):
        legal[k] ^= 0x5A if k % 3 else 0x01
    legal[L.REC_OFF_CALENDAR + 0x68:L.REC_OFF_CALENDAR + 0x68 + 4] = b"\x00\x00\x00\x00"   # tortimer field stays 0 (not date dependent)
    legal[L.REC_OFF_MY_ORG + 0x20:L.REC_OFF_MY_ORG + 0x40] = b"\x33" * 0x20                # another design texture
    legal = bytes(legal)
    off = len(run.log())
    b = run.guest("B", gb, rec=legal)
    b.connect_and_ready(quiet=True)
    pb = b.rec_pushes[-1] if b.rec_pushes else None
    ck("B a legal fresh record with 3 starter catalog bits, another gender / face, a changed calendar tail and design is ACCEPTED (MIGRATE APPLIED rev 1, no refusal logged)",
       pb is not None and pb["rev"] == 1 and "first MIGRATE REFUSED" not in run.log()[off:] and re.search(r"MIGRATE xfer 1 APPLIED", run.log()[off:]) is not None)
    ck("B the host stored exactly the merge of the upload into its blank record == the upload (appearance gender 1 face 6 included)",
       pb is not None and pb["data"] == L.record_merge_expected(L.guest_blank_record(gb), legal) and pb["data"][0x14:0x16] == bytes([1, 6]))
    tok = b.guest_token
    ck("B the client double holds the minted token", tok is not None and len(tok) == 16)
    run.release(b)
    L.pump_sleep(1.0)

    # ---- C: a RETURNING guest (rev > 0) is not subject to the first-migrate rule ----
    off = len(run.log())
    c2 = run.guest("C", gb, token=tok)
    c2.connect_and_ready(quiet=True)
    pc_ = c2.rec_pushes[-1] if c2.rec_pushes else None
    ck("C the returning guest (token) gets the stored record with NO MIGRATE (rev 1)", pc_ is not None and pc_["data"] == pb["data"] and pc_["rev"] == 1
       and "MIGRATE_REQUEST" not in run.log()[off:])
    up = L.record_set_u32(pc_["data"], L.REC_OFF_WALLET, 4242)
    up = L.record_set_u16(up, L.REC_OFF_POCKETS + 2 * 3, ITM_APPLE)
    up = L.record_set_u32(up, L.REC_OFF_BANK, 777)
    L.pump_sleep(1.7)
    x = c2.upload_record(up, base=(pc_["epoch"], 1))
    a2 = c2.wait_record_ack(xfer_id=x, timeout=8.0)
    ck("C a LATER upload of the returning guest with wallet / pocket item / bank is APPLIED (rev 2): the empty-economy rule is first-MIGRATE only",
       a2 is not None and a2.status == L.PC_NETGAME_REC_ACK_APPLIED and a2.rev == 2 and "first MIGRATE REFUSED" not in run.log()[off:])
    run.release(c2)

    # ---- D: residents keep importing their own populated record ----
    off = len(run.log())
    r1 = run.ready("R1", run.r1)
    pr = r1.rec_pushes[-1] if r1.rec_pushes else None
    inv = L.record_inventory(pr["data"]) if pr is not None else None
    ck("D a RESIDENT (slot %d: items + loan 17400 in its record) imports its populated record by MIGRATE: APPLIED, pushed rev 1, class RESIDENT, never refused as 'not fresh'" % run.r1,
       pr is not None and pr["rev"] == 1 and pr["rsv"] == L.PC_NETGAME_REC_CLASS_RESIDENT and inv is not None and any(inv[0])
       and re.search(r"MIGRATE xfer 1 APPLIED \(resident %d " % run.r1, run.log()[off:]) is not None and "first MIGRATE REFUSED" not in run.log()[off:])
    run.release(r1)
    ck("the host never crashed or logged an INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11700)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_g2_protocol_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    L.check("fixture has >= 3 non-host residents (slots %s)" % residents, len(residents) >= 3, results)
    host = None
    try:
        if len(residents) >= 3:
            host, ok = TP.start_host(ip, args.port, "g2", TG.HOST_EXTRA, results)
            run = TG.GRun(ip, args.port, host, results, snap_gci, host_slot, residents[:3])
            try:
                if ok:
                    body(run, results)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                L.check("g2 protocol run raised %r" % exc, False, results)
            finally:
                for cl in run.clients:
                    try:
                        cl.close()
                    except Exception:  # noqa: BLE001
                        pass
                rc = host.stop()
                L.check("host did not crash (exit code before stop: %s)" % rc, rc is None, results)
            TG.check_residents_unchanged(results, snap_gci, gci_path, host_slot, run.log(), require_write=False)
    finally:
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
