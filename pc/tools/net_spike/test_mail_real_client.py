#!/usr/bin/env python3
"""test_mail_real_client.py - Mail milestone 1, the REAL game client's half, ONE small smoke test: one letter WITH A GIFT from a real client to the host's
resident through the real MAIL_SEND code path (AWAIT_CLEAN -> TXN_COMMIT kind 12 -> the host's mirror read + commit -> TXN_RESULT -> the slot cleared by the
apply step). REAL processes.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0 --mail-test-force-delivery`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot> --mail-test-send=0,gift`
The client's request seam entry points (pc_net_game_mail_begin_send / _poll that the post girl dialogue calls), the AWAIT_CLEAN wait for the D3 upload, the
TXN_COMMIT build / send, TXN_RESULT handling and the apply step (pcnetgame_txn_apply_mail), and the host's handler + the vanilla post office all run for
real. The ONLY synthetic input is the TEST-ONLY, default-off hook: its one local write puts a send-font letter (and the gift moved out of a pocket, like the
letter board's hand overlay) into a free mail slot; it bypasses only the letter board and the post girl dialogue. The host hook --mail-test-force-delivery runs
the REAL vanilla delivery so the letter can be seen in the recipient's host mailbox. Assertions come from both processes' own log lines (order: letter
written < begin < record clean < request sent < TXN_RESULT < the slot cleared) and from a byte-exact check of the host mirror through a fresh scripted
FakeClient session after the client left.
NOT covered (no UI automation): the post girl dialogue state machine itself and the mTG_send_proc seam (SOURCE AUDITED in test_mail_src.py), the REJECTED rows
with a real client (host side tested with scripted clients in test_mail_protocol.py), a second real client, the recipient READING the letter (M2).

Run ONLY on the disposable pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN=<absolute path>, ports 11210+). The fixture save is snapshotted at start and restored
before every host launch and at the end. One real client at a time.
Usage: python test_mail_real_client.py [--port 11210]
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
    host = rig.start_host("mail_r1", port, ["--mail-test-force-delivery"])
    check("R1 host reached genuine field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, port)
    cl = rig.start_client("mail_r1", port, r1, ["--mail-test-send=0,gift"])
    check("R1 real client booted into the field and connected", cl is not None)
    if cl is None:
        host.stop()
        return
    m = cl.wait_for_log(r"--mail-test-send: final mail slot (\d+) ", 150.0)
    check("R1 the hook ran to its final report", m is not None)
    ct, ht = cl.log_text(), host.log_text()
    off = len(ht)
    cl.stop()
    rig.wait_peer_gone(off - 1)
    if m is None:
        host.stop()
        return
    m_wr = re.search(r"--mail-test-send=0,gift: wrote a letter into mail slot (\d+) for house (\d+) present 0x([0-9A-F]{4}) hash=0x([0-9A-F]{8})", ct)
    m_gift = re.search(r"--mail-test-send: gift item 0x([0-9A-F]{4}) moved from pocket slot (\d+) into the letter", ct)
    m_begin = re.search(r"\[NET\]\[MAIL\] client: begin MAIL_SEND request=(\d+) mail slot=(\d+) present=0x([0-9A-F]{4}) letter_hash24=0x([0-9A-F]{6}) -- awaiting a clean record", ct)
    m_clean = re.search(r"\[NET\]\[MAIL\] client: record is clean after (\d+) ms -- beginning the MAIL_SEND transaction", ct)
    m_sent = re.search(r"TXN_COMMIT sent kind=MAIL_SEND request=(\d+) nonce=(\d+) seq=(\d+) dest=0 slot=(\d+) item=0x([0-9A-F]{4}) base=\(epoch (\d+), rev (\d+)\)", ct)
    m_res = re.search(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=MAIL_SEND request=(\d+) seq=(\d+) rev=(\d+)", ct)
    m_app = re.search(r"client: APPLIED request (\d+) kind=MAIL_SEND -- the letter in mail slot (\d+) was handed to the post office and removed locally", ct)
    m_hook = re.search(r"--mail-test-send: result APPLIED reason 0 after (\d+) ms", ct)
    m_fin = re.search(r"--mail-test-send: final mail slot (\d+) EMPTY \(font 255\) pockets=([0-9A-F,]+)", ct)
    cm = MP.commits_text(ht)
    check("R1 the hook wrote ONE send-font letter into a free mail slot for house 0 and moved a gift out of a pocket (or, with empty pockets, attached a test apple)",
          m_wr is not None and m_gift is not None or (m_wr is not None and int(m_wr.group(3), 16) != 0))
    gift = int(m_wr.group(3), 16) if m_wr else 0
    slot = int(m_wr.group(1)) if m_wr else -1
    check("R1 begin -> the client WAITED for a clean record (AWAIT_CLEAN: the D3 upload carried the letter) -> the kind-12 request (dest NONE, the letter's slot, the gift echo) was sent "
          "-> TXN_RESULT(APPLIED, same seq) -> the slot was cleared by the apply step; each in this order in the client log",
          all(x is not None for x in (m_wr, m_begin, m_clean, m_sent, m_res, m_app, m_hook)) and int(m_begin.group(2)) == slot == int(m_sent.group(4)) == int(m_app.group(2))
          and int(m_begin.group(3), 16) == gift == int(m_sent.group(5), 16) and m_sent.group(3) == m_res.group(2) and int(m_res.group(3)) > int(m_sent.group(7))
          and m_wr.start() < m_begin.start() < m_clean.start() < m_sent.start() < m_res.start() < m_app.start())
    check("R1 the AWAIT_CLEAN wait was bounded (%s ms < 5000) and the request carried the 24-bit hash the hook logged (letter_hash24 == the low 24 bits of the written hash)"
          % (m_clean and m_clean.group(1)),
          m_clean is not None and int(m_clean.group(1)) < 5000 and m_begin is not None and m_wr is not None and int(m_begin.group(4), 16) == int(m_wr.group(4), 16) & 0xFFFFFF)
    check("R1 the letter and the pockets changed locally ONLY on APPLIED (the hook's invariant check never fired); nothing rejected / resent / diverged / inconsistent",
          "CHANGED BEFORE APPLIED" not in ct and "REJECTED(" not in ct and "resending the identical" not in ct and "possible local divergence" not in ct and "inconsistent" not in ct
          and "refused locally" not in ct and "protocol violation" not in ct)
    check("R1 the final client state: the mail slot is EMPTY (font 255) and the gift is in NO pocket", m_fin is not None and int(m_fin.group(1)) == slot
          and gift not in [int(x, 16) for x in m_fin.group(2).strip(",").split(",")] or (m_fin is not None and gift == 0))
    model = None
    try:
        model = MP.letter(MP.pid_of(host_slot), MP.pid_of(r1), present=gift)
    except Exception:  # noqa: BLE001
        pass
    check("R1 the letter the real client wrote is byte-identical to the python model of it (recipient = resident %d, sender = the client's own identity, send font, the hook's body, gift 0x%04X): BE hash 0x%s"
          % (host_slot, gift, m_wr and m_wr.group(4)), model is not None and m_wr is not None and L.mail_hash32(model) == int(m_wr.group(4), 16))
    exp = L.mail_hash32(L.mail_delivered_expected(model, MP.pid_of(r1))) if model is not None else None
    check("R1 host: exactly one commit line (resident %d, slot %d, house 0, the gift, po_slot >= 0), and the letter handed to the post office is the model with the receive font (hash 0x%08X)"
          % (r1, slot, exp or 0),
          len(cm) == 1 and cm[0][1] == r1 and cm[0][2] == slot and cm[0][3] == 0 and cm[0][4] == gift and cm[0][6] >= 0 and exp is not None and cm[0][5] == exp
          and count(r"TXN APPLIED kind=MAIL_SEND ", ht) == 1 and "CONFLICT" not in ht and "INTERNAL" not in ht)
    d = [x for x in MP.delivered_text(ht) if exp is not None and x[2] == exp]
    check("R1 after the forced vanilla delivery the letter IS in house 0's host mailbox: receive font, the gift, sender / recipient type PLAYER, sender PersonalID = the client",
          len(d) >= 1 and d[0][0] == 0 and d[0][3] == L.MAIL_FONT_RECV and d[0][4] == gift and d[0][5] == 0 and d[0][6] == 0 and d[0][7] == MP.pid_id16(MP.pid_of(r1)))
    f = RC.fake_session(port, ip, r1, "MailR1rec")
    push = f.rec_pushes[-1]
    data = push["data"]
    e = MP.empty_slot_bytes(data)
    pk = tuple(int(x, 16) for x in m_fin.group(2).strip(",").split(",")) if m_fin else ()
    check("R1 host mirror (new FakeClient session): mail[%d] is cleared (byte-identical to an unused slot), the pockets equal the client's final pockets, and no letter / pocket still carries the gift"
          % slot,
          e is not None and L.record_mail(data, slot) == e and tuple(L.record_inventory(data)[0]) == pk
          and all(L.mail_font(L.record_mail(data, i)) == L.MAIL_FONT_UNUSED or L.mail_present(L.record_mail(data, i)) != gift for i in range(L.REC_MAIL_COUNT) if gift))
    RC.release(f)
    host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11210)
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
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "mail_real_client_save_snapshot_%d" % os.getpid())
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
