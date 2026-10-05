#!/usr/bin/env python3
"""test_house_sync_protocol.py - FURNITURE SYNC Stage 1 (host-authoritative player-house furniture, wire ids 59..61), HOST half.

TIER: PROTOCOL TESTED -- REAL host processes (`AnimalCrossing.exe --host <port> --bootstrap-resident 0 --house-sync [test hooks]`, started / stopped by this script) driven by
scripted FakeClients (net_spike_lib: HOUSE_BEGIN / HOUSE_CHUNK / HOUSE_ACK codec, commit_house(), the canonical house image helpers and a python double of the host's item-class
multiset). The host's C code runs for real; the REAL CLIENT half (the dirty test, the unsafe-scene gates, the stash writer, the commit trigger) is NOT exercised here: it is
covered by the source audit test_house_sync_src.py only. No real player walks into a room in this test (no UI automation), so `pcnetgame_local_house_unsafe` is exercised through
the TEST-ONLY host hook --house-test-host-in-house, not through a live room scene.

DISPOSABLE FIXTURE: the test builds its own copy of pc\\build64\\bin_fixture4 (NEVER touched, only read) in $HOUSE_TEST_FIXTURE (default pc\\build64\\bin_fixture4_house; the basename
must start with bin_fixture4 so the harness' automatic whole-save snapshot / restore applies) and copies the freshly built pc\\build64\\bin\\AnimalCrossing.exe into it (a read of the
build output only; the live save dir is never read or written). `rom` is a junction to the same read-only ISO dir the fixtures use. bin_talkfix and the live bin are never used.

Phases (one host process each; --only P1,P2):
  P1  host WITH --house-sync: HOST_CONFIG byte 1 bit 0 reaches every client; every client gets one 7-chunk CANON_PUSH per owned house whose owner-writable bytes equal the GCI;
      pickup pair (furniture out of the room, into a pocket) APPLIED: ACK seq + 1, record rev + 1, the OTHER client receives the new canonical push (and the owner does not);
      placement (pocket -> room) APPLIED; rotation (multiset-preserving) APPLIED; duplication attempt (an extra furniture, pockets unchanged) -> CONSERVATION: both rejected, the
      owner is rolled back by a FULL record push + a CANON_PUSH of the unchanged house, the other client sees nothing; host log names the differing ids
  P2  host WITH --house-sync --house-test-host-in-house 2 --house-test-host-edit 3,0,<cell>,<item>: STALE record base, BAD_DIGEST, wrong house index (BAD_SHAPE detail 5), invalid cell
      (INVALID_CELL, detail = floor*1024+layer*256+cell), a guest's commit (NOT_OWNER, no push to the guest), BUSY while the host's player is 'in' the house, a host-originated change
      (seq + 1, pushed) that makes an older commit STALE (so it is never undone) and is accepted when built on the new seq, a partial 16 / 17 transfer followed by a disconnect
      (nothing applied, the reconnect is pushed the unchanged seq), a valid commit followed by a reconnect (pushed exactly the committed seq + image), and a graceful host stop whose GCI
      holds the committed house floors AND the committed pockets together (save -> GCI consistency)
  P3  own-room ground pickup pairs (layer-0 ITEM1 cell cleared + same id in a pocket -> APPLIED; cleared cell without a pocket gain -> CONSERVATION; wallet credit -> CONSERVATION)
Usage: python test_house_sync_protocol.py [--port 11800] [--only P1,P2,P3]
"""
import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
SRC_BIN = os.path.join(BUILD64, "bin_fixture4")                       # read-only source of the copy
NEW_EXE = os.path.join(BUILD64, "bin", "AnimalCrossing.exe")          # the freshly built exe (read-only input)
DST_BIN = os.path.abspath(os.environ.get("HOUSE_TEST_FIXTURE", os.path.join(BUILD64, "bin_fixture4_house")))
MARKER = ".house_sync_fixture"


def make_fixture():
    """Fresh disposable copy of bin_fixture4 (+ the new exe) -> DST_BIN (refuses to replace a dir lacking our marker)."""
    assert os.path.isdir(SRC_BIN), SRC_BIN
    assert os.path.basename(DST_BIN).startswith("bin_fixture4"), "the fixture dir name must start with bin_fixture4"
    if os.path.isdir(DST_BIN):
        if not os.path.isfile(os.path.join(DST_BIN, MARKER)):
            raise SystemExit("REFUSING: %s exists and is not this test's fixture" % DST_BIN)
        rom = os.path.join(DST_BIN, "rom")
        if os.path.isdir(rom):
            os.rmdir(rom)  # remove the junction only, never the target
        shutil.rmtree(DST_BIN)
    os.makedirs(DST_BIN)
    for name in ("SDL2.dll", "keybindings.ini", "settings.ini", "shader_cache.bin", ".four_resident_fixture"):
        p = os.path.join(SRC_BIN, name)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(DST_BIN, name))
    shutil.copy2(NEW_EXE if os.path.isfile(NEW_EXE) else os.path.join(SRC_BIN, "AnimalCrossing.exe"), os.path.join(DST_BIN, "AnimalCrossing.exe"))
    shutil.copytree(os.path.join(SRC_BIN, "shaders"), os.path.join(DST_BIN, "shaders"))
    shutil.copytree(os.path.join(SRC_BIN, "save"), os.path.join(DST_BIN, "save"))
    target = os.path.realpath(os.path.join(SRC_BIN, "rom"))
    subprocess.check_call(["cmd", "/c", "mklink", "/J", os.path.join(DST_BIN, "rom").replace("/", "\\"), target.replace("/", "\\")], stdout=subprocess.DEVNULL)
    open(os.path.join(DST_BIN, MARKER), "w").write("disposable fixture of test_house_sync_protocol.py\n")


os.environ["NET_SPIKE_GAME_BIN"] = DST_BIN
if __name__ == "__main__":
    make_fixture()

sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

GCI = os.path.join(DST_BIN, L.SAVE_GCI_REL)


class Rig:
    def __init__(self, port, results):
        self.port0, self.results, self.n = port, results, 0
        self.check = lambda d, c: L.check(d, c, results)
        self.host = None
        self.clients = []

    def start_host(self, tag, extra=()):
        self.n += 1
        port = self.port0 + self.n
        for i in range(3):
            h = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--house-sync"] + list(extra),
                              log_path=os.path.join(HERE, "house_sync_%s_try%d.log" % (tag, i)), bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
                self.host, self.port, self.tag = h, port, tag
                L.resolve_host_town("127.0.0.1", port)
                return h
            h.stop()
            time.sleep(2.0)
        return None

    def join(self, slot, label, wait_pushes=True):
        c = L.FakeClient(label, "127.0.0.1", self.port, player=L.resident_player(slot))
        c.rec_resident_idx = slot
        c.connect_and_ready(quiet=True)
        self.clients.append(c)
        if wait_pushes:
            c.hub.wait_until(lambda: len({p["house"] for p in c.house_pushes_of()}) >= 4, 8.0)
        return c

    def stop_graceful(self):
        """Polite window close (WM_CLOSE -> the game's normal shutdown path incl. the final authoritative save)."""
        from game_input import GameWindow
        h = self.host
        win = GameWindow(h.proc.pid)
        t0 = time.monotonic()
        while not win.ready() and time.monotonic() - t0 < 5.0:
            time.sleep(0.1)
        posted = win.close()
        deadline = time.monotonic() + 40.0
        while h.alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        code = h.proc.returncode if not h.alive() else None
        h.stop()
        self.host = None
        return posted, code

    def release(self):
        for c in self.clients:
            try:
                if c.state in (c.STATE_CONNECTED, c.STATE_PENDING):
                    c.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients = []
        L.pump_sleep(0.5)


def owner_equal(a, b):
    """True iff the OWNER-WRITABLE bytes of two house images are identical."""
    return L.house_owner_digest(a) == L.house_owner_digest(b)


def pushes_by_house(c):
    out = {}
    for p in c.house_pushes_of():
        if p["digest_ok"]:
            out[p["house"]] = p
    return out


def empty_cells(img, f=0, l=0):
    return [c for c in range(L.HOUSE_CELLS) if L.house_cell(img, f, l, c) == 0]


def pockets_of(rec):
    return list(L.record_inventory(rec)[0])


def with_pockets(rec, pockets):
    _p, conds, wallet = L.record_inventory(rec)
    return L.record_set_inventory(rec, pockets, conds, wallet)


def log_has(host, rx):
    return re.search(rx, host.log_text()) is not None


def first_furniture_cell(img, f=0, l=0):
    """(cell, id) of the first FTR1 (0x3xxx) furniture of the layer with rotation 0: the one that is picked up / moved by the tests."""
    for c in range(L.HOUSE_CELLS):
        v = L.house_cell(img, f, l, c)
        if (v >> 12) == 3 and (v & 3) == 0:
            return c, v
    return None, None


def run_p1(rig):
    check = rig.check
    host = rig.start_host("P1")
    check("P1 host reached field-ready state with --house-sync", host is not None)
    if host is None:
        return
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    check("P1 fixture has >= 2 non-host residents %s" % residents, len(residents) >= 2)
    ia, ib = residents[0], residents[1]
    gci_imgs = {h: L.house_from_gci(DST_BIN, h) for h in range(4)}
    a = rig.join(ia, "A")
    b = rig.join(ib, "B")
    # --- HOST_CONFIG + the initial canonical pushes
    hc = a.ts_latest(L.PC_NETGAME_TS_HOSTCFG)
    check("P1 HOST_CONFIG reached client A with byte 1 bit 0 (house sync) set and every other byte zero (byte 0 = wildlife off)",
          hc is not None and len(hc[1]) == 8 and hc[1][0] == 0 and hc[1][1] == L.PC_NETGAME_HOSTCFG_FLAG_HOUSE_SYNC and hc[1][2:] == bytes(6))
    check("P1 HOST_CONFIG reached client B too", b.ts_latest(L.PC_NETGAME_TS_HOSTCFG) is not None and b.ts_latest(L.PC_NETGAME_TS_HOSTCFG)[1][1] == 1)
    pa, pb = pushes_by_house(a), pushes_by_house(b)
    check("P1 client A received a digest-ok CANON_PUSH of every owned house (0..3), 7 chunks each, kind CANON_PUSH, image 0x1A44 B",
          sorted(pa) == [0, 1, 2, 3] and all(p["count"] == 7 and p["kind"] == 2 and len(p["data"]) == 0x1A44 for p in pa.values()))
    check("P1 client B received the same four pushes", sorted(pb) == [0, 1, 2, 3])
    check("P1 the pushed owner-writable bytes of every house equal the GCI's (BE image, floors + wall_floor + music_box)",
          all(owner_equal(pa[h]["data"], gci_imgs[h]) and owner_equal(pb[h]["data"], gci_imgs[h]) for h in range(4)))
    hdr_same = {h: bytes(pa[h]["data"][:0x38]) == bytes(gci_imgs[h][:0x38]) for h in range(4)}
    L.info("header (ownerID .. outlook) of the pushed images equals the GCI's: %s (host-owned bytes may legitimately differ after the host booted)" % hdr_same)
    check("P1 the pushed ownerIDs are the residents' (house i is owned by resident i in this fixture)",
          all(L.house_owner_pid(pa[h]["data"])[1:] == L.house_owner_pid(gci_imgs[h])[1:] for h in range(4))
          and L.house_owner_pid(pa[ia]["data"])[1] == a.player.player_id)
    check("P1 canonical seqs start at 1 and every push carries the host session", all(p["seq"] >= 1 and p["session"] != 0 for p in pa.values()))
    # --- pickup pair
    ha, hb = ia, ib                       # the house index of A / B
    base_img = pa[ha]["data"]
    cell, fid = first_furniture_cell(base_img)
    check("P1 A's house %d has a furniture (cell %s id 0x%04X) to pick up" % (ha, cell, fid or 0), cell is not None)
    if cell is None:
        return
    sess0, seq0 = a.house_base_for(ha)
    rec0 = a.rec_local
    pk0 = pockets_of(rec0)
    free = pk0.index(0)
    rev0 = a.rec_last[2]
    nb_before = len(b.house_pushes_of(ha))
    na_before = len(a.house_pushes_of(ha))
    img1 = L.house_set_cell(base_img, 0, 0, cell, 0)
    pk1 = list(pk0)
    pk1[free] = fid
    rec1 = with_pockets(rec0, pk1)
    x = a.commit_house(ha, img1, rec1)
    ack = a.wait_house_ack(x)
    check("P1 pickup pair (furniture out of the room, into pocket %d) APPLIED: ack seq %s -> %s, record rev %s -> %s" % (free, seq0, ack and ack.house_seq, rev0, ack and ack.rev),
          ack is not None and ack.status == 0 and ack.house_seq == seq0 + 1 and ack.rev == rev0 + 1 and ack.host_session == sess0)
    pb2 = b.wait_house_push(after=nb_before, house=ha, timeout=6.0)
    check("P1 the OTHER client B received the new canonical push of house %d: seq %d, the cell is empty, everything else of the owner bytes unchanged" % (ha, seq0 + 1),
          pb2 is not None and pb2["seq"] == seq0 + 1 and pb2["digest_ok"] and L.house_cell(pb2["data"], 0, 0, cell) == 0 and owner_equal(pb2["data"], img1))
    L.pump_sleep(1.0)
    check("P1 the owner A received NO push of its own house for its own commit", len(a.house_pushes_of(ha)) == na_before)
    check("P1 host log: OWNER_COMMIT APPLIED for house %d seq %d" % (ha, seq0 + 1),
          log_has(host, r"OWNER_COMMIT xfer %d house %d APPLIED \(house seq %d," % (x, ha, seq0 + 1)))
    # --- placement: pocket -> room (cell + 1 of the furniture's former place is empty by construction? use an empty cell)
    emp = [c for c in empty_cells(img1) if L.house_cell(img1, 0, 0, c) == 0 and c != cell]
    pcell = emp[0]
    img2 = L.house_set_cell(img1, 0, 0, pcell, fid)
    pk2 = list(pk1)
    pk2[free] = 0
    rec2 = with_pockets(rec1, pk2)
    x2 = a.commit_house(ha, img2, rec2)
    ack2 = a.wait_house_ack(x2)
    check("P1 placement (pocket %d -> room cell %d) APPLIED: seq %d, rev + 1" % (free, pcell, seq0 + 2), ack2 is not None and ack2.status == 0 and ack2.house_seq == seq0 + 2 and ack2.rev == rev0 + 2)
    pb3 = b.wait_house_push(after=0, house=ha, min_seq=seq0 + 2, timeout=6.0)
    check("P1 B received the placement push (seq %d, furniture at cell %d)" % (seq0 + 2, pcell), pb3 is not None and pb3["seq"] == seq0 + 2 and L.house_cell(pb3["data"], 0, 0, pcell) == fid)
    # --- rotation (multiset preserving)
    img3 = L.house_set_cell(img2, 0, 0, pcell, fid + 1)
    x3 = a.commit_house(ha, img3, rec2)
    ack3 = a.wait_house_ack(x3)
    check("P1 rotation (id | 1, same item class) APPLIED: seq %d" % (seq0 + 3), ack3 is not None and ack3.status == 0 and ack3.house_seq == seq0 + 3)
    pb4 = b.wait_house_push(after=0, house=ha, min_seq=seq0 + 3, timeout=6.0)
    check("P1 B received the rotation push (seq %d)" % (seq0 + 3), pb4 is not None and pb4["seq"] == seq0 + 3 and L.house_cell(pb4["data"], 0, 0, pcell) == fid + 1)
    # --- duplication attempt: an extra furniture appears in the room, pockets unchanged -> CONSERVATION
    emp2 = [c for c in empty_cells(img3) if c not in (cell, pcell)]
    dcell = emp2[0]
    img_dup = L.house_set_cell(img3, 0, 0, dcell, fid)
    nb_b = len(b.house_pushes_of(ha))
    na_p = len(a.house_pushes_of(ha))
    nrec_p = len(a.rec_pushes)
    seq_ok = seq0 + 3
    xd = a.commit_house(ha, img_dup, rec2)
    ackd = a.wait_house_ack(xd)
    check("P1 duplication attempt (extra furniture 0x%04X in the room, pockets unchanged) -> CONSERVATION" % fid, ackd is not None and ackd.status == L.PC_NETGAME_HOUSE_ACK_CONSERVATION)
    pr = a.wait_record_push(after=nrec_p, timeout=5.0)
    check("P1 the owner is rolled back by a FULL record push (the host's pockets, not the attempted ones)",
          pr is not None and pr["kind"] == L.PC_NETGAME_REC_KIND_PUSH_FULL and pockets_of(pr["data"]) == pk2)
    pr2 = a.wait_house_push(after=na_p, house=ha, timeout=5.0)
    check("P1 ... and by a CANON_PUSH of the UNCHANGED house (seq %d, the rotation image, no extra furniture)" % seq_ok,
          pr2 is not None and pr2["seq"] == seq_ok and owner_equal(pr2["data"], img3) and L.house_cell(pr2["data"], 0, 0, dcell) == 0)
    L.pump_sleep(1.5)
    check("P1 client B saw NOTHING of the rejected commit (no new push of house %d)" % ha, len(b.house_pushes_of(ha)) == nb_b)
    ht = host.log_text()
    m = re.search(r"CONSERVATION failed; first differing item ids \(old -> new\): ([^\n]+)", ht)
    check("P1 host log names the differing class(es): %s" % (m.group(1) if m else None), m is not None and "(1 -> 2)" in m.group(1))
    check("P1 the host's house %d / record were NOT changed by the rejected commit (the owner's next base = the rolled-back state)" % ha,
          a.house_base_for(ha)[1] == seq_ok and a.rec_last[2] == rev0 + 3)
    # a duplicate sent by B for A's house is refused too (wrong owner -> house index mismatch)
    xb = b.commit_house(ha, img3, b.rec_local, base=a.house_base_for(ha))
    ackb = b.wait_house_ack(xb)
    check("P1 B committing A's house index -> BAD_SHAPE (the house is derived from the sender, never trusted)",
          ackb is not None and ackb.status == L.PC_NETGAME_HOUSE_ACK_BAD_SHAPE and ackb.detail == 5)
    check("P1 the host did not crash and still runs", host.alive())


def run_p2(rig):
    check = rig.check
    gci_imgs = {h: L.house_from_gci(DST_BIN, h) for h in range(4)}
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    check("P2 fixture has >= 3 non-host residents %s" % residents, len(residents) >= 3)
    ia, ib, ic = residents[0], residents[1], residents[2]
    edit_cell = [c for c in empty_cells(gci_imgs[ic]) if c > 40][0]
    edit_item = 0x2037
    host = rig.start_host("P2", ["--house-test-host-in-house", str(ib), "--house-test-host-edit", "%d,0,%d,0x%04X" % (ic, edit_cell, edit_item)])
    check("P2 host reached field-ready state", host is not None)
    if host is None:
        return
    a = rig.join(ia, "A")
    ha, hb, hc_ = ia, ib, ic
    # --- host-originated change: A holds the initial canonical copy of house hc_; the host then edits its own save -> seq + 1 pushed to every READY client
    pa0 = pushes_by_house(a)
    s_init = pa0[hc_]["seq"]
    got = a.wait_house_push(after=0, house=hc_, min_seq=s_init + 1, timeout=10.0)
    check("P2 host-originated change (--house-test-host-edit: house %d cell %d = 0x%04X): host log + a CANON_PUSH with seq %d reaches the READY client A" % (hc_, edit_cell, edit_item, s_init + 1),
          got is not None and got["seq"] == s_init + 1 and L.house_cell(got["data"], 0, 0, edit_cell) == edit_item
          and log_has(host, r"--house-test-host-edit: house %d floor 0 layer 0 cell %d" % (hc_, edit_cell)))
    if got is None:
        return
    b = rig.join(ib, "B")
    c = rig.join(ic, "C")
    pa = pushes_by_house(a)
    pc_ = pushes_by_house(c)
    s_new = got["seq"]
    check("P2 a client joining AFTER the host change is pushed the new canonical copy (seq %d with the host's cell)" % s_new, pc_[hc_]["seq"] == s_new and L.house_cell(pc_[hc_]["data"], 0, 0, edit_cell) == edit_item)
    # a commit built BEFORE the host change (old seq, the cell still empty): STALE, the host's change is never undone
    new_img = pc_[hc_]["data"]
    cell_c, fid_c = first_furniture_cell(new_img)
    rec_c = c.rec_local
    pk_c = pockets_of(rec_c)
    fr = pk_c.index(0)
    img_old = L.house_set_cell(L.house_set_cell(new_img, 0, 0, cell_c, 0), 0, 0, edit_cell, 0)   # an edit that never saw the host's cell
    pk_new = list(pk_c)
    pk_new[fr] = fid_c
    nrec = len(c.rec_pushes)
    x = c.commit_house(hc_, img_old, with_pockets(rec_c, pk_new), base=(pc_[hc_]["session"], s_new - 1))
    ack = c.wait_house_ack(x)
    check("P2 a commit built on the OLD house seq %d (the host moved to %d) -> STALE (the host's change is not undone)" % (s_new - 1, s_new), ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_STALE and ack.house_seq == s_new)
    pr = c.wait_record_push(after=nrec, timeout=5.0)
    check("P2 the stale owner is rolled back by a FULL record push (its pocket edit is gone)", pr is not None and pr["kind"] == L.PC_NETGAME_REC_KIND_PUSH_FULL and pockets_of(pr["data"]) == pk_c)
    # the same edit rebuilt on the NEW canonical copy (it keeps the host's cell) is accepted
    img_c = L.house_set_cell(new_img, 0, 0, cell_c, 0)
    x = c.commit_house(hc_, img_c, with_pockets(c.rec_local, [fid_c if i == fr else v for i, v in enumerate(pockets_of(c.rec_local))]))
    ack = c.wait_house_ack(x)
    check("P2 the same edit rebuilt on the NEW seq %d is APPLIED (seq %d) and keeps the host's cell" % (s_new, s_new + 1), ack is not None and ack.status == 0 and ack.house_seq == s_new + 1)
    # --- A: bad digest / wrong house / invalid cell / stale rec base
    base_a = pa[ha]["data"]
    seqa = a.house_base_for(ha)[1]
    recA = a.rec_local
    cellA, fidA = first_furniture_cell(base_a)
    pkA = pockets_of(recA)
    frA = pkA.index(0)
    imgA = L.house_set_cell(base_a, 0, 0, cellA, 0)
    pkA2 = list(pkA)
    pkA2[frA] = fidA
    recA2 = with_pockets(recA, pkA2)
    nrecA = len(a.rec_pushes)
    x = a.commit_house(ha, imgA, recA2, digest=0xDEADBEEF)
    ack = a.wait_house_ack(x)
    check("P2 wrong payload digest -> BAD_DIGEST", ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_BAD_DIGEST)
    a.wait_record_push(after=nrecA, timeout=5.0)
    x = a.commit_house(ha, imgA, with_pockets(a.rec_local, pkA2), house_field=hb)
    ack = a.wait_house_ack(x)
    check("P2 BEGIN.house != the sender's house -> BAD_SHAPE detail 5", ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_BAD_SHAPE and ack.detail == 5)
    badc = [cc for cc in empty_cells(base_a) if cc > 40][0]
    img_bad = L.house_set_cell(base_a, 0, 0, badc, L.HOUSE_RSV_WALL_NO)
    nrecA = len(a.rec_pushes)
    x = a.commit_house(ha, img_bad, a.rec_local)
    ack = a.wait_house_ack(x)
    check("P2 a structural id (RSV_WALL_NO) written into an empty cell -> INVALID_CELL, detail = floor*1024 + layer*256 + cell = %d" % badc,
          ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_INVALID_CELL and ack.detail == badc)
    a.wait_record_push(after=nrecA, timeout=5.0)
    nrecA = len(a.rec_pushes)
    rl = a.rec_last
    x = a.commit_house(ha, imgA, with_pockets(a.rec_local, pkA2), rec_base=(rl[1], rl[2] - 1))
    ack = a.wait_house_ack(x)
    check("P2 a commit on a STALE record base (rev - 1) -> STALE", ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_STALE)
    a.wait_record_push(after=nrecA, timeout=5.0)
    # --- BUSY: the host's own player is 'in' house hb (TEST hook)
    recB = b.rec_local
    pkB = pockets_of(recB)
    cellB, fidB = first_furniture_cell(pushes_by_house(b)[hb]["data"])
    imgB = L.house_set_cell(pushes_by_house(b)[hb]["data"], 0, 0, cellB, 0)
    pkB2 = list(pkB)
    pkB2[pkB.index(0)] = fidB
    nrecB = len(b.rec_pushes)
    x = b.commit_house(hb, imgB, with_pockets(recB, pkB2))
    ack = b.wait_house_ack(x)
    check("P2 BUSY while the host's own player is in house %d (--house-test-host-in-house): nothing applied, no rollback push" % hb, ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_BUSY)
    L.pump_sleep(1.0)
    check("P2 ... and BUSY sent no FULL record push", len(b.rec_pushes) == nrecB)
    # --- guest: NOT_OWNER, no push to the guest
    g = L.FakeClient("G", "127.0.0.1", rig.port, guest=L.guest_identity("GUESTA", 0x4A01, "HOMETWN", 0x5B01), bind_ip="127.0.21.2")
    rig.clients.append(g)
    g.connect_and_ready(quiet=True)
    L.pump_sleep(1.5)
    check("P2 the host pushed NO house to the guest", len(g.house_pushes) == 0)
    x = g.commit_house(ha, imgA, g.rec_local if g.rec_local is not None else g.own_record(), base=(a.house_base_for(ha)[0], seqa))
    ack = g.wait_house_ack(x)
    check("P2 a GUEST's commit -> NOT_OWNER (refused before any homes[] index exists)", ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_NOT_OWNER)
    g.disconnect()
    # --- partial 16/17 then disconnect: nothing applied
    seq_before = a.house_base_for(ha)[1]
    rl = a.rec_last
    x = a.commit_house(ha, imgA, with_pockets(a.rec_local, pkA2), only_chunks=set(range(16)))
    L.pump_sleep(0.5)
    a.disconnect()
    L.pump_sleep(1.0)
    a.reconnect_and_ready()
    a.hub.wait_until(lambda: len({p["house"] for p in a.house_pushes_of()}) >= 4, 8.0)
    pa2 = pushes_by_house(a)
    check("P2 partial transfer (16 of 17 chunks) then disconnect: nothing applied -- the reconnect is pushed the UNCHANGED seq %d and image" % seq_before,
          pa2[ha]["seq"] == seq_before and owner_equal(pa2[ha]["data"], base_a) and not log_has(host, r"OWNER_COMMIT xfer %d house %d APPLIED" % (x, ha)))
    # --- a valid commit, then reconnect: pushed exactly the committed seq + image; save -> GCI consistency afterwards
    rl = a.rec_last
    recA = a.rec_local
    pkA = pockets_of(recA)
    frA = pkA.index(0)
    pkA3 = list(pkA)
    pkA3[frA] = fidA
    recA3 = with_pockets(recA, pkA3)
    x = a.commit_house(ha, imgA, recA3)
    ack = a.wait_house_ack(x)
    check("P2 a valid pickup pair APPLIED (seq %d -> %d)" % (seq_before, seq_before + 1), ack is not None and ack.status == 0 and ack.house_seq == seq_before + 1)
    committed_seq = ack.house_seq
    a.disconnect()
    L.pump_sleep(1.0)
    a.reconnect_and_ready()
    a.hub.wait_until(lambda: len({p["house"] for p in a.house_pushes_of()}) >= 4, 8.0)
    pa3 = pushes_by_house(a)
    check("P2 reconnect: the owner is pushed exactly the committed seq %d and image, and its record is the committed pair's (pocket %d = 0x%04X)" % (committed_seq, frA, fidA),
          pa3[ha]["seq"] == committed_seq and owner_equal(pa3[ha]["data"], imgA) and pockets_of(a.rec_local)[frA] == fidA)
    # --- save -> GCI: graceful host stop; the GCI must hold the committed floors AND pockets, and the host's own edit
    L.pump_sleep(1.0)
    posted, code = rig.stop_graceful()
    ht = open(host.log_path, "rb").read().decode("utf-8", "replace")
    check("P2 graceful host stop: window close posted, exit code 0, final shutdown save OK", posted and code == 0 and "final shutdown save OK" in ht)
    g_a = L.house_from_gci(DST_BIN, ha)
    g_c = L.house_from_gci(DST_BIN, hc_)
    rec_g = L.record_from_gci(DST_BIN, ia)
    check("P2 GCI consistency: house %d floors == the committed image AND resident %d pockets == the committed pockets (house and record saved as one pair)" % (ha, ia),
          owner_equal(g_a, imgA) and pockets_of(rec_g) == pkA3)
    check("P2 GCI: house %d keeps the host-originated cell (0x%04X at cell %d) and the owner's later accepted edit" % (hc_, edit_item, edit_cell),
          L.house_cell(g_c, 0, 0, edit_cell) == edit_item and L.house_cell(g_c, 0, 0, cell_c) == 0)
    check("P2 GCI: the houses the tests never edited are unchanged (house %d, host house 0)" % hb, owner_equal(L.house_from_gci(DST_BIN, hb), gci_imgs[hb]) and owner_equal(L.house_from_gci(DST_BIN, 0), gci_imgs[0]))


def run_p3(rig):
    """Own-room ground pickup (client inside its own room takes the vanilla local branch: the layer-0 ITEM1 cell is cleared and the same id enters a pocket in one frame;
    the commit pair is what the host sees). Host-side conservation only; the client seam itself is covered by test_house_sync_src.py."""
    check = rig.check
    host = rig.start_host("P3")
    check("P3 host reached field-ready state with --house-sync", host is not None)
    if host is None:
        return
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    check("P3 fixture has >= 1 non-host resident %s" % residents, len(residents) >= 1)
    ha = residents[0]
    a = rig.join(ha, "A")
    pa = pushes_by_house(a)
    check("P3 client A received the canonical push of its house %d" % ha, ha in pa)
    if ha not in pa:
        return
    img0 = pa[ha]["data"]
    rec0 = a.rec_local
    pk0 = pockets_of(rec0)
    jj = [j for j, v in enumerate(pk0) if (v >> 12) == 2 and not 0x2F00 <= v < 0x2F04]
    check("P3 fixture record has a plain ITEM1 pocket item to use as the dropped item %s" % ([hex(pk0[j]) for j in jj],), len(jj) > 0)
    if not jj:
        return
    j = jj[0]
    item = pk0[j]
    cell = empty_cells(img0)[0]
    sess0, seq0 = a.house_base_for(ha)
    wallet0 = L.record_inventory(rec0)[2]
    # 1. drop in the room (pocket -> layer-0 cell)
    img_f = L.house_set_cell(img0, 0, 0, cell, item)
    pk_e = list(pk0)
    pk_e[j] = 0
    rec_e = with_pockets(rec0, pk_e)
    ack = a.wait_house_ack(a.commit_house(ha, img_f, rec_e))
    check("P3 drop pair (ITEM1 0x%04X pocket -> room cell %d) APPLIED" % (item, cell), ack is not None and ack.status == 0 and ack.house_seq == seq0 + 1)
    # 2. own-room ground pickup: the cell is cleared and the SAME id appears in a pocket
    ack = a.wait_house_ack(a.commit_house(ha, L.house_set_cell(img_f, 0, 0, cell, 0), rec0))
    check("P3 own-room ground pickup pair (layer-0 ITEM1 cell cleared + same id in a pocket) APPLIED", ack is not None and ack.status == 0 and ack.house_seq == seq0 + 2)
    # 3. back on the floor, then two illegal pickups
    ack = a.wait_house_ack(a.commit_house(ha, img_f, rec_e))
    check("P3 second drop APPLIED (baseline for the rejected pickups)", ack is not None and ack.status == 0 and ack.house_seq == seq0 + 3)
    img_c = L.house_set_cell(img_f, 0, 0, cell, 0)
    nrec = len(a.rec_pushes)
    ack = a.wait_house_ack(a.commit_house(ha, img_c, rec_e))
    check("P3 cleared cell with NO pocket gain -> CONSERVATION (an item cannot vanish)", ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_CONSERVATION)
    a.wait_record_push(after=nrec, timeout=5.0)
    L.pump_sleep(1.0)
    nrec = len(a.rec_pushes)
    rec_w = L.record_set_inventory(rec_e, pk_e, L.record_inventory(rec_e)[1], wallet0 + 100)
    ack = a.wait_house_ack(a.commit_house(ha, img_c, rec_w))
    check("P3 cleared cell paid into the WALLET instead of a pocket (money-bag shortcut) -> CONSERVATION (the wallet is not counted)",
          ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_CONSERVATION)
    check("P3 the host did not crash and still runs", host.alive())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11800)
    ap.add_argument("--only", default="P1,P2,P3")
    args = ap.parse_args()
    L.require_test_bin_dir()
    only = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    results = []
    rig = Rig(args.port, results)
    for tag, fn in (("P1", run_p1), ("P2", run_p2), ("P3", run_p3)):
        if tag not in only:
            continue
        print("=" * 72 + "\n[%s]" % tag)
        with L.CloneSaveGuard():
            try:
                fn(rig)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                L.check("%s phase raised %r" % (tag, exc), False, results)
            finally:
                rig.release()
                if rig.host is not None:
                    rc = rig.host.stop()
                    L.check("[%s] host did not crash (exit code before stop: %s)" % (tag, rc), rc is None, results)
                    rig.host = None
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
