#!/usr/bin/env python3
"""test_mail2_real_client.py - Mail milestone 2, the REAL game client's half, ONE small smoke test: a real client receives the HOST-HELD mailbox of its house
as MAILBOX_LETTER messages (host-fed shadow) and takes two letters out of it through the real MAIL_TAKE code path (TXN_COMMIT kind 13 -> the host verifies the
letter in ITS mailbox and moves it into the mirror mail[dst] -> TXN_RESULT -> the apply step writes the verified letter into the client's own mail[]).

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0 --mail-test-seed-mailbox=<res>,2,0,2800`   (a simulated postman delivery: 2 letters, the first
           with a gift, into that resident's house mailbox at world-ready)
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot> --mail-test-take=2`
The client's MAILBOX_LETTER handler (validation, shadow, host-fed state), pc_net_game_mail_take_step() (the entry point the mailbox overlay's transfer code calls),
the TXN_COMMIT build / send, TXN_RESULT handling and the apply step (pcnetgame_txn_apply_take), and the host's handler run for real. The ONLY synthetic input is the
TEST-ONLY default-off hook that calls the take API (it writes nothing locally). Assertions come from both processes' own log lines and from a byte-exact check
of the host mirror + the host mailbox through a fresh scripted FakeClient session after the client left.
NOT covered (no UI automation): the mailbox overlay / actor seams (mTG_trans_mail*, the open gate, the discard refusal: SOURCE AUDITED in test_mail2_src.py), the
REJECTED rows with a real client (host side tested with scripted clients in test_mail2_protocol.py), a second real client.

Run ONLY on the disposable pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN=<absolute path>, ports 11310+). The fixture save is snapshotted at start and restored
before every host launch and at the end. One real client at a time.
Usage: python test_mail2_real_client.py [--port 11310]
"""
import argparse
import os
import re
import shutil
import sys

import net_spike_lib as L
import test_txn_real_client as RC
import test_mail_protocol as MP

APPLE = 0x2800
SEED_RE = re.compile(r"--mail-test-seed-mailbox: resident (\d+) house (\d+) mailbox\[(\d+)\] letter hash=0x([0-9A-F]{8}) present=0x([0-9A-F]{4})")
TAKE_RE = re.compile(r"\[NET\]\[MAIL\] host: peer (\d+) resident (\d+) MAIL_TAKE mailbox\[(\d+)\] of house (-?\d+) -> mirror mail\[(\d+)\] present 0x([0-9A-F]{4}) "
                     r"letter_hash=0x([0-9A-F]{8}) mailbox_slot_cleared=1 committed \[TXN\]")


def count(rx, text):
    return len(re.findall(rx, text))


def run(args, results, ip, snap_gci, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    r1 = residents[0]
    port = args.port
    host = rig.start_host("mail2_r1", port, ["--mail-test-seed-mailbox=%d,2,0,%X" % (r1, APPLE)])
    check("R1 host reached genuine field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, port)
    cl = rig.start_client("mail2_r1", port, r1, ["--mail-test-take=2"])
    check("R1 real client booted into the field and connected", cl is not None)
    if cl is None:
        host.stop()
        return
    m = cl.wait_for_log(r"--mail-test-take: taken (\d+); local mailbox still holds (\d+) letter", 150.0)
    check("R1 the hook ran to its final report", m is not None)
    ct, ht = cl.log_text(), host.log_text()
    off = len(ht)
    cl.stop()
    rig.wait_peer_gone(off - 1)
    if m is None:
        host.stop()
        return
    seeds = [(int(x.group(3)), int(x.group(4), 16), int(x.group(5), 16)) for x in SEED_RE.finditer(ht) if int(x.group(1)) == r1]
    applied_h = [(int(x.group(1)), int(x.group(2)), int(x.group(3), 16), int(x.group(4), 16)) for x in re.finditer(
        r"--mail-test-take: APPLIED mailbox\[(\d+)\] -> mail slot (\d+) hash=0x([0-9A-F]{8}) present=0x([0-9A-F]{4}) after", ct)]
    tk = [(int(x.group(2)), int(x.group(3)), int(x.group(4)), int(x.group(5)), int(x.group(6), 16), int(x.group(7), 16)) for x in TAKE_RE.finditer(ht)]
    check("R1 the host seeded exactly 2 letters into resident %d's house mailbox (slots 0 and 1), the first with the gift APPLE" % r1,
          sorted(s[0] for s in seeds) == [0, 1] and [s for s in seeds if s[0] == 0][0][2] == APPLE)
    check("R1 the client's shadow was fed by the host: 10 'applied mailbox[i]' lines (one per slot, 2 LETTER + 8 EMPTY) and the HOST-FED line, in that order",
          count(r"\[NET\]\[MAIL\] client: applied mailbox\[\d\] seq \d+ digest 0x[0-9A-F]{8} \((?:LETTER|EMPTY)", ct) >= 10 and count(r"\(LETTER, present", ct) >= 2
          and "the mailbox is now HOST-FED (all 10 slots received)" in ct
          and ct.index("applied mailbox[") < ct.index("the mailbox is now HOST-FED"))
    check("R1 the client log shows exactly 2 'begin MAIL_TAKE' (mailbox 0 then 1) and 2 'APPLIED ... kind=MAIL_TAKE' and 2 hook APPLIED lines; nothing refused / rejected / inconsistent",
          count(r"\[NET\]\[MAIL\] client: begin MAIL_TAKE request=\d+ mailbox\[\d\]", ct) == 2 and count(r"client: APPLIED request \d+ kind=MAIL_TAKE", ct) == 2
          and len(applied_h) == 2 and sorted(a[0] for a in applied_h) == [0, 1] and "refused locally" not in ct and "REJECTED(" not in ct and "inconsistent" not in ct
          and "no matching verified letter" not in ct and "protocol violation" not in ct and "resending the identical" not in ct)
    check("R1 the pockets and every mail slot changed locally ONLY on APPLIED (the hook's invariant checks never fired)", "CHANGED BEFORE APPLIED" not in ct)
    check("R1 the letters the client wrote into its mail[] are exactly the letters the host seeded (BE hashes equal, gift APPLE on the first)",
          len(applied_h) == 2 and sorted((a[0], a[2]) for a in applied_h) == sorted((s[0], s[1]) for s in seeds) and [a for a in applied_h if a[0] == 0][0][3] == APPLE)
    check("R1 host: exactly 2 MAIL_TAKE commits (resident %d, mailboxes 0 and 1 of one house), each into the mail slot the client reported, hashes == the seeded hashes; "
          "2 TXN APPLIED lines; no CONFLICT / INTERNAL" % r1,
          len(tk) == 2 and all(t[0] == r1 for t in tk) and len({t[2] for t in tk}) == 1 and sorted((t[1], t[5]) for t in tk) == sorted((s[0], s[1]) for s in seeds)
          and sorted((t[1], t[3]) for t in tk) == sorted((a[0], a[1]) for a in applied_h) and count(r"TXN APPLIED kind=MAIL_TAKE ", ht) == 2
          and "CONFLICT" not in ht and "INTERNAL" not in ht)
    check("R1 the client's final report: 2 taken, the local mailbox holds 0 letters", int(m.group(1)) == 2 and int(m.group(2)) == 0)
    pk = re.search(r"--mail-test-take: taken \d+; local mailbox still holds \d+ letter\(s\); pockets=([0-9A-F,]+)", ct)
    f = RC.fake_session(port, ip, r1, "Mail2R1rec")
    push = f.rec_pushes[-1]
    data = push["data"]
    f.wait_mbox_msgs(10, 8.0)
    st = f.mbox_state()
    mirror_hashes = sorted(L.mail_hash32(m_) for m_ in MP_used(data))
    check("R1 host mirror (new FakeClient session): EXACTLY the 2 taken letters are in mail[] (byte-identical to the seeded letters: hashes equal), the pockets equal the "
          "client's final pockets", len(MP_used(data)) == 2 and mirror_hashes == sorted(s[1] for s in seeds)
          and (pk is None or tuple(L.record_inventory(data)[0]) == tuple(int(x, 16) for x in pk.group(1).strip(",").split(","))))
    check("R1 host mailbox (the new session's MAILBOX_LETTER sync): all 10 slots EMPTY now (the letters left the host mailbox), and the gift APPLE exists exactly once overall "
          "(in the mirror, not in the mailbox)", len(st) == 10 and all(g.flags & 1 for g in st.values())
          and sum(1 for m_ in MP_used(data) if L.mail_present(m_) == APPLE) == 1)
    RC.release(f)
    host.stop()


def MP_used(data):
    return [L.record_mail(data, i) for i in range(L.REC_MAIL_COUNT) if L.mail_font(L.record_mail(data, i)) != L.MAIL_FONT_UNUSED]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11310)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "mail2_real_client_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        run(args, results, ip, snap_gci, rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
