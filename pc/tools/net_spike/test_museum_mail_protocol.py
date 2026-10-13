#!/usr/bin/env python3
"""test_museum_mail_protocol.py - worker B: MUSEUM LETTERS WITH A GIFT (TXN_COMMIT kind 19 MUSEUM_MAIL) + GUEST DONOR NAMES (town service 8 MUSEUM_DONORS,
host sidecar museum_donors.dat), SCRIPTED FakeClients vs a REAL host on a DISPOSABLE fixture (NET_SPIKE_GAME_BIN=<pc\\build64\\bin_fixture4_wlB_<name>>, ports 15xxx).
The letter is put into the client's record the way the real client does it (an ordinary D3 upload: the player wrote it, the gift inside), then MUSEUM_MAIL names it.
Host env AC_TEST_MUSEUM_DAY=file: the museum's day pass (residents: vanilla; guests: the new pass) runs when the test drops ac_museum_day.txt (not only at the day change).

  G1 a GUEST mails an undonated fish to the museum: APPLIED, the letter left the guest mirror, the museum mirror (service 2) shows the fish with nibble 5, MUSEUM_DONORS
     (service 8) names THIS guest for that exhibit at the guest AND at a resident; museum_donors.dat exists next to the other host sidecars
  G2 byte-identical resend -> APPLIED/REPLAYED (one execution); kind 19 for a PLAYER letter / a museum letter without a gift -> PRECOND; nothing changed
  G3 the guests' museum day pass: the information letter (the Museum entry of the address book: contacted) and then the result letter of the donation arrive in the GUEST
     mailbox (house 0xFF, sender type MUSEUM, no gift for a donation)
  G4 a FOSSIL letter -> APPLIED (stored for appraisal); the next pass sends the appraised fossil back as the letter's gift (a dinosaur FTR item, nibble rules untouched)
  G5 an APPLE letter -> APPLIED (CANNOT_DONATE): the pass returns the apple as the letter's gift (nothing lost)
  G6 a guest's in-person MUSEUM_DONATE (kind 8) of another fish is attributed too (service 8 names the guest for it)
  R1 a RESIDENT mails an undonated insect: APPLIED, the nibble is that resident's slot + 1 (not the host's player), no guest attribution for it
  P  RESTART: after a graceful stop (GCI saved) a second host process on the same save reloads museum_donors.dat: a new session gets the same names
Usage: python test_museum_mail_protocol.py [--port 15440]
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
os.environ["AC_TEST_HOOKS"] = "1"
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402
import test_guest_protocol as TG  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE  # noqa: E402

K_MUSEUM_MAIL = 19
K_DONATE = L.PC_NETGAME_TXN_KIND_MUSEUM_DONATE
SVC_MUSEUM, SVC_MDONORS = 2, 8
FISH0, INSECT0, APPLE, FOSSIL = 0x2300, 0x2D00, 0x2800, 0x2511
MUS_OFF = {"fossil": 0x00, "art": 0x0D, "fish": 0x15, "insect": 0x2A}
EXH_BASE = {"fossil": 0, "art": 25, "insect": 40, "fish": 80}
HOUSE_GUEST = 0xFF
DINO_LO, DINO_HI = 0x3000, 0x3200  # generous range check for an appraised fossil FTR item (FTR_DIN_*)


def nib(blob, cat, idx):
    return (blob[MUS_OFF[cat] + (idx >> 1)] >> ((idx & 1) * 4)) & 0xF


def free_idx(blob, cat, n):
    return [i for i in range(40 if cat in ("fish", "insect") else 15) if nib(blob, cat, i) == 0][:n]


def gi(i):
    return TG.gid(60 + i)


def donors_name_for(c, exhibit):
    st = c.ts_latest(SVC_MDONORS)
    if st is None:
        return None
    b = st[1]
    ix = b[4 + exhibit]
    if ix == 0xFF or ix >= b[0]:
        return None
    k = bytes(b[124 + 20 * ix:124 + 20 * ix + 20])  # the native (little-endian PC) PersonalID -> the canonical BE form of L.guest_pid_be
    return k[:16] + struct.pack(">HH", *struct.unpack("<HH", k[16:20]))


def day_pass(run, n_guest_lines_before=None, timeout=15.0):
    """Drops ac_museum_day.txt (AC_TEST_MUSEUM_DAY=file) and waits until the host ran the pass; returns True."""
    n0 = run.n_log(r"AC_TEST_MUSEUM_DAY: running the museum day pass now")
    with open(os.path.join(L.GAME_BIN_DIR, "ac_museum_day.txt"), "w") as f:
        f.write("1")
    ok = wait_until(lambda: run.n_log(r"AC_TEST_MUSEUM_DAY: running the museum day pass now") > n0, timeout)
    L.pump_sleep(2.5)  # the guest pass + the record push to the guest
    return ok


def museum_letter(c, slot, gift, sender_pid, recipient_type=L.MAIL_NAME_MUSEUM):
    museum_pid = b"Museum  " + bytes(12)
    letter = L.build_mail_be(museum_pid, sender_pid, present=gift, recipient_type=recipient_type, body=b"FOR THE MUSEUM")
    L.pump_sleep(2.5)  # a guest gets a FULL record push after each museum commit (museum_record is host-owned): the scripted client adopts it first, then writes
    c.write_local_mail(slot, letter)
    ack = c.upload_local_record()
    return letter, ack


def commit19(run, c, slot, letter, kind=K_MUSEUM_MAIL, wait=4.0):
    it, ac, ai = c.mail_tag_fields(letter)
    rid = run.fresh_rid()
    sent = c.send_txn_commit(kind, rid, D_NONE, slot, it, pre=c.txn_pre_image(), aux_cond=ac, aux_item=ai)
    return sent, c.wait_txn_result(sent.seq, wait)


def guest_box(c):
    return {k: g for k, g in c.mbox_state().items() if g.house == HOUSE_GUEST}


def box_letters(c):
    return [bytes(g.letter) for g in guest_box(c).values() if not (g.flags & L.PC_NETGAME_MBOX_FLAG_EMPTY)]


def wait_until(fn, timeout):
    t0 = time.monotonic()
    v = fn()
    while not v and time.monotonic() - t0 < timeout:
        L.pump_sleep(0.5)
        v = fn()
    return v


def start_host(ip, port, tag, results, env):
    host_slot = L.TEST_HOST_RESIDENT
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot), "--no-house-sync"], env=env,
                         log_path=os.path.join(HERE, "museum_mail_%s_host.log" % tag)).start()
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=host_slot)
    L.check("[%s] host reached field-ready state" % tag, ok, results)
    if ok:
        L.resolve_host_town(ip, port)
    return host, ok


def phase_main(run):
    ck = run.check
    A = run.guest("GA", gi(0))
    A.connect_and_ready(quiet=True)
    R1 = run.ready("R1", run.r1)
    pidA = L.guest_pid_be(gi(0))
    st = A.wait_ts_state(SVC_MUSEUM, 6.0)
    ck("the guest got the museum mirror", st is not None)
    if st is None:
        return None
    mus0 = st[1]
    fish = free_idx(mus0, "fish", 2)
    ins = free_idx(mus0, "insect", 1)
    ck("fixture precondition: 2 undonated fish + 1 undonated insect (%s %s)" % (fish, ins), len(fish) == 2 and len(ins) == 1)
    L.pump_sleep(2.0)
    ck("G3 first museum day pass ran", day_pass(run))
    ck("G3 ... the guest got the information letter and is 'contacted' (the Museum address entry)",
       run.n_log(r"\[NET\]\[MUSEUM\]\[GUEST\] host: guest slot \d+ museum pass: 1 letter\(s\), contacted=1") >= 1)
    # ---- G1
    letter, ack = museum_letter(A, 0, FISH0 + fish[0], pidA)
    ck("G1 the guest's record upload (the letter with the fish inside) was APPLIED", ack is not None)
    off = len(run.log())
    sent, r = commit19(run, A, 0, letter)
    run.tx_ok("G1 MUSEUM_MAIL (kind 19) of a guest with an undonated fish", r, APPLIED, R["NONE"])
    ck("G1 host log: DONATED with donor nibble 5, the mirror slot cleared",
       run.n_log(r"\[NET\]\[MUSEUM\]\[MAIL\] host: peer \d+ guest record \d+ MUSEUM_MAIL slot 0 gift 0x%04X -> DONATED \(donor nibble 5\).*mirror_slot_cleared=1 committed" % (FISH0 + fish[0]), off) == 1)
    mus = A.wait_ts_state(SVC_MUSEUM, 6.0, pred=lambda b: nib(b, "fish", fish[0]) == 5)
    ck("G1 the museum mirror shows the fish with nibble 5 (guest donor)", mus is not None)
    ex = EXH_BASE["fish"] + fish[0]
    ok_a = wait_until(lambda: donors_name_for(A, ex) == pidA, 8.0)
    ok_r = wait_until(lambda: donors_name_for(R1, ex) == pidA, 8.0)
    ck("G1 MUSEUM_DONORS (service 8) names THIS guest for exhibit %d at the guest and at a resident" % ex, ok_a and ok_r)
    ck("G1 host log: the attribution was recorded in museum_donors.dat", run.n_log(r"\[NET\]\[MUSEUM\]\[DONOR\] host: exhibit %d \(item 0x%04X\) attributed to guest slot" % (ex, FISH0 + fish[0]), off) == 1)
    side = os.path.join(L.GAME_BIN_DIR, "save", "mp", "museum_donors.dat")
    ck("G1 museum_donors.dat exists next to the other host sidecars (%s)" % side, os.path.isfile(side))
    # ---- G2
    off2 = len(run.log())
    A.resend_txn(sent)
    rr = A.wait_txn_result(sent.seq, 3.0, nth=2)
    ck("G2 a byte-identical resend replays APPLIED/REPLAYED, executed once", rr is not None and rr.outcome == APPLIED and rr.reason == R["REPLAYED"]
       and run.n_log(r"MUSEUM_MAIL slot 0 gift", off2) == 0)
    pl, _a = museum_letter(A, 1, APPLE, pidA, recipient_type=L.MAIL_NAME_PLAYER)
    _s, rp = commit19(run, A, 1, pl)
    run.tx_ok("G2 kind 19 for a letter addressed to a PLAYER", rp, REJECTED, R["PRECOND"])
    nl, _a = museum_letter(A, 2, 0, pidA)
    _s, rn = commit19(run, A, 2, nl)
    run.tx_ok("G2 kind 19 for a museum letter WITHOUT a gift (it stays local)", rn, REJECTED, R["PRECOND"])
    # ---- G4 / G5 (sent before the passes so one pass returns them)
    fl, _a = museum_letter(A, 3, FOSSIL, pidA)
    _s, rf = commit19(run, A, 3, fl)
    run.tx_ok("G4 MUSEUM_MAIL of an unidentified FOSSIL", rf, APPLIED, R["NONE"])
    ck("G4 host log: stored for appraisal", "FOSSIL stored for appraisal" in run.log(off2))
    al, _a = museum_letter(A, 4, APPLE, pidA)
    _s, ra = commit19(run, A, 4, al)
    run.tx_ok("G5 MUSEUM_MAIL of an APPLE (not an exhibit)", ra, APPLIED, R["NONE"])
    ck("G5 host log: CANNOT_DONATE (returned by letter)", "CANNOT_DONATE (returned by letter)" in run.log(off2))
    # ---- G3 / G4 / G5: the second pass returns the results (max 3 per pass: the thank-you letter, the apple, the appraised fossil)
    ck("G3 second museum day pass ran", day_pass(run))
    got = wait_until(lambda: len(box_letters(A)) >= 4, 20.0)
    lt = box_letters(A)
    presents = sorted(L.mail_present(x) for x in lt)
    ck("G3 the guest mailbox (house 0xFF) received the museum's letters (%d): info + results" % len(lt), got)
    ck("G3 every one is addressed to the guest and comes from the MUSEUM (sender type 2)", lt and all(L.mail_recipient(x)[0] == pidA and L.mail_sender(x)[1] == L.MAIL_NAME_MUSEUM for x in lt))
    ck("G3 the information letter + the donation's thank-you letter carry no gift (%s)" % ["0x%04X" % p for p in presents], presents.count(0) >= 2)
    ck("G4 the appraised fossil came back as a letter's gift (an identified fossil furniture item, %s)" % ["0x%04X" % p for p in presents],
       any(p not in (0, APPLE, FOSSIL) and p >= 0x1000 for p in presents))
    ck("G5 the apple came back as a letter's gift", APPLE in presents)
    ck("G3 host log: the guest's museum pass ran and marked it contacted (the Museum address entry)", run.n_log(r"\[NET\]\[MUSEUM\]\[GUEST\] host: guest slot \d+ museum pass: \d+ letter\(s\), contacted=1") >= 1)
    # ---- G6 in-person donation attributed
    give = dict(enumerate(A.txn_pre_image()[0]))
    slot = [i for i, p in give.items() if p == 0][0]
    pockets = tuple(FISH0 + fish[1] if i == slot else p for i, p in give.items())
    _p, conds, wallet = A.txn_pre_image()
    A.rec_local = L.record_set_inventory(A.rec_local if A.rec_local is not None else A.own_record(), pockets, conds, wallet)
    A.upload_local_record()
    rid = run.fresh_rid()
    s6 = A.send_txn_commit(K_DONATE, rid, D_NONE, slot, FISH0 + fish[1])
    r6 = A.wait_txn_result(s6.seq, 4.0)
    run.tx_ok("G6 the guest's in-person MUSEUM_DONATE of another fish", r6, APPLIED, R["NONE"])
    ex6 = EXH_BASE["fish"] + fish[1]
    ck("G6 ... is attributed to the guest too (service 8)", wait_until(lambda: donors_name_for(R1, ex6) == pidA, 8.0))
    # ---- R1 resident
    pid_r1 = L.record_from_gci(L.GAME_BIN_DIR, run.r1)[:20]
    rl, _a = museum_letter(R1, 0, INSECT0 + ins[0], pid_r1)
    off3 = len(run.log())
    _s, rr1 = commit19(run, R1, 0, rl)
    run.tx_ok("R1 a RESIDENT's MUSEUM_MAIL of an undonated insect", rr1, APPLIED, R["NONE"])
    ck("R1 host log: donor nibble = that resident's slot + 1 (%d)" % (run.r1 + 1), run.n_log(r"resident %d MUSEUM_MAIL slot 0 gift 0x%04X -> DONATED \(donor nibble %d\)" % (run.r1, INSECT0 + ins[0], run.r1 + 1), off3) == 1)
    ck("R1 the museum mirror shows nibble %d for the insect, no guest attribution for it" % (run.r1 + 1),
       R1.wait_ts_state(SVC_MUSEUM, 6.0, pred=lambda b: nib(b, "insect", ins[0]) == run.r1 + 1) is not None and donors_name_for(R1, EXH_BASE["insect"] + ins[0]) is None)
    ck("host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())
    return {"ex": ex, "pidA": pidA}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=15440)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4_* copy only", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap = os.path.join(os.environ.get("TEMP", "."), "museum_mail_snap_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    env = {"AC_TEST_MUSEUM_DAY": "file"}
    try:
        host, ok = start_host(ip, args.port, "M", results, env)
        run = TG.GRun(ip, args.port, host, results, snap_gci, host_slot, residents[:3])
        info = None
        try:
            if ok:
                info = phase_main(run)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            L.check("[M] phase raised %r" % exc, False, results)
        for cl in run.clients:
            try:
                cl.disconnect()
            except Exception:  # noqa: BLE001
                pass
            cl.close()
        if ok:
            host.wait_for_log(r"\[PC\] GCI save: written successfully", 30.0)
            L.pump_sleep(1.0)
        host.stop()
        # ---- P restart
        if info is not None:
            host2, ok2 = start_host(ip, args.port + 1, "P", results, {})
            try:
                if ok2:
                    run2 = TG.GRun(ip, args.port + 1, host2, results, snap_gci, host_slot, residents[:3])
                    R2 = run2.ready("R2", residents[1])
                    ok_n = wait_until(lambda: donors_name_for(R2, info["ex"]) == info["pidA"], 10.0)
                    L.check("P after a host RESTART museum_donors.dat is reloaded: a new session gets the guest's name for exhibit %d" % info["ex"], ok_n, results)
                    L.check("P host log: museum_donors.dat loaded", "museum_donors.dat loaded" in host2.log_text(), results)
                    for cl in run2.clients:
                        cl.close()
            finally:
                host2.stop()
    finally:
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
