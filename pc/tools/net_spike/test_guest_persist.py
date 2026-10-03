#!/usr/bin/env python3
"""test_guest_persist.py - GUESTS G1 persistence of the guest table (save/mp/guests.dat), REAL HOST + FakeClient guests.

TIER: a REAL host game process (`AnimalCrossing.exe --host <port> --bootstrap-resident 0`, started / stopped several times) driven by FakeClient guest
sessions. Every persistence claim is checked on the file the real host wrote, parsed by an INDEPENDENT Python implementation of the format
(test_guest_protocol.parse_guests; layout from pc_mp_guests.h). The native unit test test_guest_storage.py covers every corruption / crash window of the
module itself; here the HOST GLUE is exercised end to end.

DISPOSABLE FIXTURE: reuses the fixture builder of test_d3_record_persist.py (pc\\build64\\bin_fixture4_persist, built fresh from bin_fixture4; never touches
bin_fixture4's save, bin_talkfix or the live bin). Ports 11500+. One FakeClient at a time (+1 transient).

PHASES (one real host process each):
  A  no guests.dat -> two guests A, B first contact (tokens minted, guests.dat written at the mint, rev 0), A migrates (rev 1) and uploads (rev 2),
     B migrates (rev 1); graceful stop (shutdown save): guests.dat (independent parser) holds both keys, tokens, A rev 2 + the merged record, B rev 1; the
     write happened AFTER the GCI save line; nothing guest-like in card_a / card_b / the GCI is the vanilla size; no *.gci name in save/mp
  B  restart: both guests restored from the file (log), A reconnects with its token -> SAME slot, KNOWN, no MIGRATE, the pushed record == the stored one, a NEW
     host_session; a squatter without the token / with a wrong token is refused; an upload + ABRUPT drop -> an early host save writes guests.dat (rev 3) and
     keeps the previous generation as .bak1; the clean reconnect afterwards triggers no early save; graceful stop
  C1 flip a byte of guests.dat -> the .bak1 generation is used (log), the bad file is preserved, A connects with its token and gets the OLDER stored record
  C2 corrupt EVERY generation -> UNTRUSTED: log, A (even with its token) and a NEW guest are REFUSED ('guest admission disabled'), a RESIDENT still joins,
     no guests.dat is written, every bad file is preserved
  D  operator reset (remove guests.dat, .bak files AND *.corrupt-*) -> MISSING: the host mints a NEW token for the same key (the old one is useless)
Usage: python test_guest_persist.py [--port 11500]
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_d3_record_persist as P  # noqa: E402  (sets NET_SPIKE_GAME_BIN to the disposable bin_fixture4_persist BEFORE net_spike_lib is used)

L = P.L
import test_guest_protocol as TG  # noqa: E402

SAVE, MPDIR, GCI = P.SAVE, P.MPDIR, P.GCI
GUESTS = os.path.join(MPDIR, "guests.dat")


def gid(i):
    return TG.gid(i)


class GRig(P.Rig):
    def join_guest(self, label, g, token=None, **kw):
        c = L.FakeClient(label, "127.0.0.1", self.port, guest=g, guest_token=token, **kw)
        c.connect_and_ready(quiet=True)
        self.clients.append(c)
        return c

    def try_guest(self, label, g, token=None, timeout=5.0):
        c = L.FakeClient(label, "127.0.0.1", self.port, guest=g, guest_token=token)
        self.clients.append(c)
        try:
            c.connect_and_ready(timeout=timeout, quiet=True)
            return c, None
        except L.HandshakeRejected as e:
            return None, e.reject


def run(args, results):
    rig = GRig(args.port, results)
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    if not residents:
        check("fixture copy has non-host residents", False)
        return
    r1 = residents[0]
    check("fixture starts WITHOUT save/mp", not os.path.exists(MPDIR))
    gA, gB = gid(0), gid(1)

    # =============================== PHASE A ===============================
    host = rig.start_host("A")
    check("A host reached field-ready state", host is not None)
    if host is None:
        return
    check("A host log: guests file MISSING at load (first run), before any client", "guests file 'save/mp/guests.dat' load mode=MISSING" in host.log_text())
    a = rig.join_guest("A1", gA)
    tokA = a.guest_token
    push = a.rec_pushes[-1]
    check("A guest A: first contact, MIGRATE -> PUSH_FULL rev 1, class GUEST, slot 0 token minted", a.token_msgs[0][1].guest_slot == 0 and push["rev"] == 1 and push["rsv"] == 1 and tokA)
    expA1 = L.record_merge_expected(L.guest_blank_record(gA), a.own_record())
    check("A the pushed record is the merge of the upload into the blank guest record", push["data"] == expA1)
    check("A guests.dat exists right after the mint, entry 0 = A with its token and rev 0 (independent parser)", os.path.isfile(GUESTS)
          and TG.parse_guests(GUESTS)["e"][0]["pid"] == L.guest_pid_be(gA) and TG.parse_guests(GUESTS)["e"][0]["token"] == tokA and TG.parse_guests(GUESTS)["e"][0]["rev"] == 0)
    up = L.record_set_u32(expA1, L.REC_OFF_WALLET, 4321)
    up = L.record_set_u16(up, L.REC_OFF_POCKETS + 2 * 3, 0x2037)
    L.pump_sleep(1.7)
    x = a.upload_record(up, base=(push["epoch"], 1))
    ack = a.wait_record_ack(xfer_id=x)
    check("A guest A upload APPLIED rev 2", ack is not None and ack.status == 0 and ack.rev == 2)
    expA2 = L.record_merge_expected(expA1, up)
    sessA = push["session"]
    rig.release(a)
    b = rig.join_guest("B1", gB)
    tokB = b.guest_token
    expB1 = L.record_merge_expected(L.guest_blank_record(gB), b.own_record())
    check("A guest B: slot 1, rev 1, its own token (different from A's)", b.token_msgs[0][1].guest_slot == 1 and b.rec_pushes[-1]["rev"] == 1 and tokB and tokB != tokA
          and b.rec_pushes[-1]["data"] == expB1)
    rig.release(b)
    posted, code = rig.stop_graceful()
    ht = open(host.log_path, "rb").read().decode("utf-8", "replace")
    check("A graceful stop: window close posted, exit code 0, final shutdown save OK", posted and code == 0 and "final shutdown save OK" in ht)
    i_gci = ht.rfind("GCI save: written successfully")
    i_gst = ht.find("guests.dat written (after the GCI save", max(i_gci - 1, 0)) if i_gci >= 0 else -1
    check("A host log: guests.dat is written AFTER the GCI save line (the hook order)", i_gci >= 0 and i_gst > i_gci)
    try:
        pf = TG.parse_guests(GUESTS)
        ok_parse = True
    except AssertionError as ex:
        print("   parse error:", ex)
        pf, ok_parse = None, False
    check("A guests.dat parses (independent parser: magic, version 2, size, CRC32, record CRCs, key <-> record binding)", ok_parse)
    if not ok_parse:
        return
    e0, e1 = pf["e"][0], pf["e"][1]
    check("A entry 0 = guest A: key, token, rev 2, record == the merged upload; entry 1 = guest B: key, token, rev 1, record == its merge; the rest absent",
          e0["present"] and e0["pid"] == L.guest_pid_be(gA) and e0["token"] == tokA and e0["rev"] == 2 and e0["record"] == expA2
          and e1["present"] and e1["pid"] == L.guest_pid_be(gB) and e1["token"] == tokB and e1["rev"] == 1 and e1["record"] == expB1
          and all(not e["present"] for e in pf["e"][2:]) and e0["epoch"] != 0 and e1["epoch"] != 0)
    check("A the records of guests are not in the GCI: GCI keeps its vanilla size and no guest PersonalID is anywhere in it",
          os.path.getsize(GCI) == 0x72040 and L.guest_pid_be(gA) not in open(GCI, "rb").read() and L.guest_pid_be(gB) not in open(GCI, "rb").read())
    check("A nothing guest-like in card_a / card_b; no *.gci / GAF named file in save/mp; save/mp is a sibling of card_a",
          all("guest" not in n.lower() for d in ("card_a", "card_b") for n in P.listing(os.path.join(SAVE, d))) and sorted(P.listing(SAVE)) == ["card_a", "card_b", "mp"]
          and all(not re.search(r"\.gci|\.GAF|^GAF", n, re.I) for n in P.listing(MPDIR)))
    check("A (v2) both entries carry THIS host town's identity (identical, non-empty name / id / terrain hash), are UNCONFIRMED (no token-presenting reconnect "
          "yet) and have increasing mint ages",
          e0["town_land_name"] == e1["town_land_name"] and e0["town_land_name"] != b"\x00" * 8 and e0["town_land_id"] == e1["town_land_id"]
          and e0["town_terrain_hash"] == e1["town_terrain_hash"] and e0["confirmed"] == 0 and e1["confirmed"] == 0 and e0["age"] < e1["age"])
    gen_a = pf["gen"]

    # =============================== PHASE B ===============================
    host = rig.start_host("B")
    check("B host restarted", host is not None)
    if host is None:
        return
    ht = host.log_text()
    check("B host log: guests file loaded OK (generation used 0), 2 guest(s) restored", "load mode=OK " in ht and "2 guest(s) restored" in ht)
    a = rig.join_guest("B-A", gA, token=tokA)
    push = a.rec_pushes[-1]
    check("B guest A reconnects with its token: SAME slot 0, KNOWN, same token, NO MIGRATE_REQUEST", a.token_msgs[0][1].guest_slot == 0 and a.token_msgs[0][1].flags == 2
          and bytes(a.token_msgs[0][1].token) == tokA and not any(x.status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST for _c, x in a.rec_acks))
    check("B the pushed record == the stored one (rev 2) byte for byte, class GUEST, a NEW host_session", push["data"] == expA2 and push["rev"] == 2 and push["rsv"] == 1
          and push["session"] != sessA and push["session"] != 0)
    L.pump_sleep(0.5)
    try:
        pfc = TG.parse_guests(GUESTS)
        check("B (M3) A's token-presenting reconnect + first record step CONFIRMED its entry durably (guests.dat confirmed = 1, written at that moment); B stays unconfirmed",
              pfc["e"][0]["confirmed"] == 1 and pfc["e"][1]["confirmed"] == 0 and "guest slot 0 CONFIRMED" in host.log_text())
    except AssertionError as ex:
        check("B guests.dat parses after the confirmation (%s)" % ex, False)
    c, rej = rig.try_guest("B-squat", gA, token=None)
    check("B a squatter claiming A's PersonalID WITHOUT the token is refused after the restart", c is None and rej is not None and "presented WITHOUT a token" in host.log_text())
    c, rej = rig.try_guest("B-wrong", gA, token=bytes(v ^ 0xFF for v in tokA))
    check("B ... and with a WRONG token", c is None and rej is not None and "presented a WRONG token" in host.log_text())
    rig.release(a)
    # upload + abrupt drop -> early save
    a = rig.join_guest("B-A2", gA, token=tokA)
    push = a.rec_pushes[-1]
    up = L.record_set_u32(expA2, L.REC_OFF_WALLET, 5555)
    L.pump_sleep(1.7)
    x = a.upload_record(up, base=(push["epoch"], 2))
    ack = a.wait_record_ack(xfer_id=x)
    check("B an upload on the restored lineage is APPLIED rev 3", ack is not None and ack.status == 0 and ack.rev == 3)
    expA3 = L.record_merge_expected(expA2, up)
    off = len(host.log_text())
    t0 = time.monotonic()
    rig.release(a, abandon=True)
    m = host.wait_for_log(r"guests\.dat written \(after the GCI save", 30.0, since_offset=off)
    dt = time.monotonic() - t0
    check("B an accepted guest change + an ABRUPT client drop makes the host save EARLY (guests.dat written after the GCI save, %.1f s <= 25 s, no 60 s wait)" % dt,
          m is not None and dt <= 25.0)
    try:
        pf = TG.parse_guests(GUESTS)
        check("B guests.dat now holds A rev 3 with the new record; B unchanged; the generation counter grew", pf["e"][0]["rev"] == 3 and pf["e"][0]["record"] == expA3
              and pf["e"][1]["rev"] == 1 and pf["gen"] > gen_a)
    except AssertionError as ex:
        check("B guests.dat parses after the early save (%s)" % ex, False)
    check("B the previous generation is kept as guests.dat.bak1 (rotation)", os.path.isfile(GUESTS + ".bak1"))
    off = len(host.log_text())
    a = rig.join_guest("B-A3", gA, token=tokA)
    rig.release(a)
    L.pump_sleep(9.0)
    check("B a clean connect / disconnect without any change triggers NO new guests.dat write (coalesced)", "guests.dat written" not in host.log_text()[off:])
    posted, code = rig.stop_graceful()
    check("B graceful stop OK", posted and code == 0)
    pf = TG.parse_guests(GUESTS)
    gen_b = pf["gen"]
    bak1 = TG.parse_guests(GUESTS + ".bak1")

    # =============================== PHASE C1 ===============================
    P.flip_byte(GUESTS, 200)
    host = rig.start_host("C1")
    check("C1 host started over a corrupt guests.dat", host is not None)
    if host is None:
        return
    ht = host.log_text()
    check("C1 host log: guests.dat UNREADABLE (checksum), RECOVERED from generation .bak1, the bad file preserved as guests.dat.corrupt-*",
          "guests.dat' is UNREADABLE: file checksum mismatch" in ht and "RECOVERED from generation .bak1" in ht and glob.glob(GUESTS + ".corrupt-*"))
    a = rig.join_guest("C1-A", gA, token=tokA)
    check("C1 guest A (token) connects: the OLDER stored record of the recovered generation is pushed (rev %d), KNOWN, slot 0" % bak1["e"][0]["rev"],
          a.rec_pushes[-1]["data"] == bak1["e"][0]["record"] and a.rec_pushes[-1]["rev"] == bak1["e"][0]["rev"] and a.token_msgs[0][1].flags == 2)
    rig.release(a)
    posted, code = rig.stop_graceful()
    check("C1 graceful stop OK", posted and code == 0)

    # =============================== PHASE C2 ===============================
    for pth in (GUESTS, GUESTS + ".bak1", GUESTS + ".bak2"):
        if os.path.isfile(pth):
            P.flip_byte(pth, 300)
    host = rig.start_host("C2")
    check("C2 host started with every generation corrupt", host is not None)
    if host is None:
        return
    ht = host.log_text()
    check("C2 host log: UNTRUSTED MODE for guests (loud), every bad file preserved, residents unaffected", "UNTRUSTED mode" in ht and "NO guest is admitted" in ht
          and len(glob.glob(GUESTS + "*.corrupt-*")) >= 3)
    c, rej = rig.try_guest("C2-A", gA, token=tokA)
    check("C2 guest A WITH its token is REFUSED (the table is untrusted: 'guest admission disabled')", c is None and rej is not None and "guest admission disabled" in host.log_text())
    c, rej = rig.try_guest("C2-new", gid(5))
    check("C2 a NEW guest is refused too (no silent re-issue of tokens)", c is None and rej is not None)
    res = rig.join(r1, "C2-R")
    check("C2 a RESIDENT still joins and syncs normally", res.identity_ack is not None and res.rec_synced)
    rig.release(res)
    check("C2 no guests.dat was (re)written while UNTRUSTED", not os.path.isfile(GUESTS))
    posted, code = rig.stop_graceful()
    check("C2 graceful stop OK; still no guests.dat, bad files still preserved", posted and code == 0 and not os.path.isfile(GUESTS) and len(glob.glob(GUESTS + "*.corrupt-*")) >= 3)

    # =============================== PHASE D ===============================
    for pth in glob.glob(GUESTS + "*"):
        os.remove(pth)
    host = rig.start_host("D")
    check("D host started after the documented operator reset", host is not None)
    if host is None:
        return
    check("D host log: guests file MISSING (first run again)", "load mode=MISSING" in host.log_text())
    a = rig.join_guest("D-A", gA)
    check("D guest A is a FIRST CONTACT again: a NEW token (flag NEW, the old one is useless), migrates again", a.token_msgs[0][1].flags == 1 and a.guest_token != tokA
          and any(x.status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST for _c, x in a.rec_acks))
    rig.release(a)
    posted, code = rig.stop_graceful()
    check("D graceful stop OK", posted and code == 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11500)
    args = ap.parse_args()
    P.make_fixture()
    L.require_test_bin_dir()
    if os.path.basename(os.path.normpath(L.GAME_BIN_DIR)) != "bin_fixture4_persist":
        print("REFUSING: runs only on the disposable pc\\build64\\bin_fixture4_persist copy", file=sys.stderr)
        return 2
    results = []
    try:
        run(args, results)
    finally:
        out = subprocess.run(["tasklist"], capture_output=True, text=True).stdout
        left = [ln for ln in out.splitlines() if "AnimalCrossing" in ln]
        L.check("no AnimalCrossing process left running", not left, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
