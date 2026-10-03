#!/usr/bin/env python3
"""test_d3_record_persist.py - D3-4 persistence of the host-mirrored resident record (save/mp/records.dat), REAL HOST + FakeClient.

TIER: a REAL host game process (`AnimalCrossing.exe --host <port> --bootstrap-resident 0`, started/stopped 5 times) driven by
FakeClient protocol sessions (the real CLIENT game binary is covered separately by test_d3_real_client.py). Every persistence
claim is checked on the files the real host wrote: the GCI bytes (net_spike_lib.record_from_gci) and records.dat, parsed here by an
INDEPENDENT Python implementation of the format (magic/version/CRC32 via zlib, entry layout from pc_mp_records.h).

DISPOSABLE FIXTURE: the test builds pc\\build64\\bin_fixture4_persist itself from pc\\build64\\bin_fixture4 (exe, SDL2.dll, shaders,
ini files, the four-resident save; `rom` is a junction to the same read-only ISO dir the fixtures use) and points
NET_SPIKE_GAME_BIN at it BEFORE net_spike_lib is imported. It never touches bin_fixture4's save, bin_talkfix (protected) or the live bin.
Ports 9900+. One FakeClient at a time (+1 transient).

PHASES (one real host process each; the GCI keeps accumulating the host's own saves between phases):
  A  records.dat missing -> first join MIGRATEs, upload of changed pockets+wallet, graceful host stop (shutdown save):
     the GCI holds the uploaded pockets/wallet, records.dat (save/mp, sibling of card_a) has rev 2 / PersonalID / digest and the
     pre-migration BACKUP of the replaced host record; nothing sidecar-like in card_a/card_b
  B  restart: lineage RESTORED, first join gets PUSH_FULL == the stored record (NOT migrate), new host_session; an upload followed
     by an ABRUPT client drop (no DISCONNECT) -> an early host save within the budget, GCI + records.dat updated; a clean
     connect/disconnect afterwards does NOT trigger another early save
  C1 flip a byte of records.dat -> the .bak1 generation is used (log), the bad file is preserved, no migrate; digest mismatch ->
     host record wins (push == GCI)
  C2 corrupt EVERY generation -> UNTRUSTED mode (log), no migrate, push rev 1 == GCI, all bad files preserved (the C1 one too),
     a fresh valid records.dat is written afterwards
  F/G hosting a save with OTHER client PersonalIDs must not wipe the stored lineage/backup; the original save then restores it
  E  UNTRUSTED is written to records.dat immediately, survives a hard kill before any save, and (all generations deleted but
     *.corrupt-* kept) is still UNTRUSTED, never silently 'missing'
  D  delete records.dat (+ .bak*) AND no *.corrupt-* present... (C2's preserved files are removed first) -> documented missing-file semantics: migration is allowed again (a STALE client
     record then replaces the host record; the replaced host record is persisted as the pre-migration backup)
Usage: python test_d3_record_persist.py [--port 9900]
"""
import argparse
import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
SRC_BIN = os.path.join(BUILD64, "bin_fixture4")
DST_BIN = os.path.join(BUILD64, "bin_fixture4_persist")
MARKER = ".persist_fixture"


def make_fixture():
    """Fresh disposable copy of bin_fixture4 -> bin_fixture4_persist (refuses to replace a dir lacking our marker)."""
    assert os.path.isdir(SRC_BIN), SRC_BIN
    if os.path.isdir(DST_BIN):
        if not os.path.isfile(os.path.join(DST_BIN, MARKER)):
            raise SystemExit("REFUSING: %s exists and is not this test's fixture" % DST_BIN)
        rom = os.path.join(DST_BIN, "rom")
        if os.path.isdir(rom):
            os.rmdir(rom)  # remove the junction only, never the target
        shutil.rmtree(DST_BIN)
    os.makedirs(DST_BIN)
    for name in ("AnimalCrossing.exe", "SDL2.dll", "keybindings.ini", "settings.ini", "shader_cache.bin", ".four_resident_fixture"):
        p = os.path.join(SRC_BIN, name)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(DST_BIN, name))
    shutil.copytree(os.path.join(SRC_BIN, "shaders"), os.path.join(DST_BIN, "shaders"))
    shutil.copytree(os.path.join(SRC_BIN, "save"), os.path.join(DST_BIN, "save"))
    target = os.path.realpath(os.path.join(SRC_BIN, "rom"))
    subprocess.check_call(["cmd", "/c", "mklink", "/J", os.path.join(DST_BIN, "rom").replace("/", "\\"), target.replace("/", "\\")], stdout=subprocess.DEVNULL)
    open(os.path.join(DST_BIN, MARKER), "w").write("disposable fixture of test_d3_record_persist.py\n")


os.environ["NET_SPIKE_GAME_BIN"] = DST_BIN
if __name__ == "__main__":
    make_fixture()

sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

SAVE = os.path.join(DST_BIN, "save")
MPDIR = os.path.join(SAVE, "mp")
REC = os.path.join(MPDIR, "records.dat")
GCI = os.path.join(DST_BIN, L.SAVE_GCI_REL)
ENTRY_SIZE = 4 + 20 + 16 + 0x2440
FILE_SIZE = 32 + 4 * ENTRY_SIZE + 4


def parse_records(path):
    """Independent parser of the records.dat v1 layout. Returns dict or raises AssertionError."""
    with open(path, "rb") as f:
        b = f.read()
    assert len(b) == FILE_SIZE, "size %d != %d" % (len(b), FILE_SIZE)
    assert b[:8] == b"ACMPREC\x00"
    ver, flags, nslots, esz, gen, rsv = struct.unpack("<6I", b[8:32])
    assert (ver, flags, nslots, esz, rsv) == (1, 0, 4, ENTRY_SIZE, 0), (ver, flags, nslots, esz, rsv)
    assert struct.unpack("<I", b[-4:])[0] == (zlib.crc32(b[:-4]) & 0xFFFFFFFF), "file crc32"
    ents = []
    for i in range(4):
        e = b[32 + i * ENTRY_SIZE:32 + (i + 1) * ENTRY_SIZE]
        present, bpres = e[0], e[1]
        d = {"present": present, "backup_present": bpres}
        if present:
            d["pid"] = bytes(e[4:24])
            d["epoch"], d["rev"], d["digest"], d["bcrc"] = struct.unpack("<4I", e[24:40])
            d["backup"] = bytes(e[40:])
            if bpres:
                assert d["bcrc"] == (zlib.crc32(d["backup"]) & 0xFFFFFFFF), "backup crc32"
        ents.append(d)
    return {"gen": gen, "e": ents, "raw": b}


def order_ok(text):
    """Every records.dat write is logged right after a 'GCI save: written successfully' line (the sidecar is never written first)."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if "records.dat written (after the GCI save" in ln:
            if not any("GCI save: written successfully" in x for x in lines[max(0, i - 3):i]):
                return False
    return True


def gci_rec(idx):
    return L.record_from_gci(GCI, idx)


def flip_byte(path, off):
    with open(path, "rb") as f:
        b = bytearray(f.read())
    b[off] ^= 0x10
    with open(path, "wb") as f:
        f.write(b)


def count(rx, text):
    return len(re.findall(rx, text))


def modified(rec):
    """Changed pockets + wallet (all client-owned, shape-legal: pocket ids reuse items that exist in the fixture save)."""
    r = bytes(rec)
    r = L.record_set_bytes(r, L.REC_OFF_POCKETS, struct.pack(">15H", 0x2037, 0x1304, 0x240D, 0x2801, *([0] * 11)))
    return L.record_set_u32(r, L.REC_OFF_WALLET, 4321)


class Rig:
    def __init__(self, port, results):
        self.port0, self.results, self.n = port, results, 0
        self.check = lambda d, c: L.check(d, c, results)
        self.host = None
        self.clients = []

    def start_host(self, tag):
        self.n += 1
        port = self.port0 + self.n
        for i in range(3):
            h = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"],
                              log_path=os.path.join(HERE, "d3_persist_%s_try%d.log" % (tag, i)), bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
                self.host, self.port, self.tag = h, port, tag
                L.resolve_host_town("127.0.0.1", port)
                return h
            h.stop()
            time.sleep(2.0)
        return None

    def join(self, slot, label, migrate_payload=None, wait=True):
        c = L.FakeClient(label, "127.0.0.1", self.port, player=L.resident_player(slot))
        c.rec_resident_idx = slot
        if migrate_payload is not None:
            c.rec_migrate_payload = migrate_payload
        c.connect_and_ready(quiet=True)
        self.clients.append(c)
        return c

    def release(self, c, abandon=False):
        try:
            if abandon:
                c.abandon()
            elif c.state in (c.STATE_CONNECTED, c.STATE_PENDING):
                c.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
        L.pump_sleep(0.5)

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
        self.last_log = open(h.log_path, "rb").read().decode("utf-8", "replace")
        self.check("%s ordering invariant: every 'records.dat written' line directly follows a successful GCI save line (%d writes)"
                   % (self.tag, count(r"records\.dat written \(after the GCI save", self.last_log)),
                   order_ok(self.last_log))
        return posted, code


def listing(d):
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def run(args, results):
    rig = Rig(args.port, results)
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture copy has non-host residents %s" % residents, len(residents) >= 1)
    if not residents:
        return
    r1 = residents[0]
    with open(GCI, "rb") as f:
        pristine_gci = f.read()
    orig = L.record_from_gci(pristine_gci, r1)
    check("fixture starts WITHOUT save/mp", not os.path.exists(MPDIR))
    up = modified(orig)
    exp_a = L.record_merge_expected(orig, up)

    # =============================== PHASE A ===============================
    host = rig.start_host("A")
    check("A host reached field-ready state", host is not None)
    if host is None:
        return
    ht = host.log_text()
    check("A host log: records file MISSING at load (first run), before any client", "load mode=MISSING" in ht)
    f = rig.join(r1, "A1")
    push = f.rec_pushes[-1]
    check("A first join of resident %d: MIGRATE_REQUEST -> auto-migrate -> PUSH_FULL rev 1" % r1,
          any(a.status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST for _c, a in f.rec_acks) and push["rev"] == 1)
    epoch1, sess1 = push["epoch"], push["session"]
    L.pump_sleep(1.8)
    x = f.upload_record(up, base=(epoch1, 1))
    ack = f.wait_record_ack(xfer_id=x)
    check("A upload of changed pockets+wallet APPLIED rev 2", ack is not None and ack.status == 0 and ack.rev == 2)
    off = len(host.log_text())
    posted, code = rig.stop_graceful()
    ht = open(host.log_path, "rb").read().decode("utf-8", "replace")
    check("A graceful stop: window close posted, process exit code 0", posted and code == 0)
    check("A host log: final shutdown save OK", "final shutdown save OK" in ht)
    i_gci = ht.rfind("GCI save: written successfully")
    i_rec = ht.find("records.dat written (after the GCI save", max(i_gci - 1, 0)) if i_gci >= 0 else -1
    check("A host log: records.dat is written AFTER the GCI save line (ordering)", i_gci >= 0 and i_rec > i_gci)
    check("A records.dat exists at save/mp/records.dat, sibling of card_a", os.path.isfile(REC) and os.path.isdir(os.path.join(SAVE, "card_a")))
    check("A nothing sidecar-like in card_a / card_b (card_a only holds the GCI + its .bak*, card_b empty)",
          all(n.startswith("DobutsunomoriP_MURA.gci") for n in listing(os.path.join(SAVE, "card_a")))
          and listing(os.path.join(SAVE, "card_b")) == [] and sorted(listing(SAVE)) == ["card_a", "card_b", "mp"])
    check("A no *.gci / gci* / GAF* named file in save/mp", all(not re.search(r"\.gci|\.GAF|^GAF", n, re.I) for n in listing(MPDIR)))
    check("A GCI size unchanged (vanilla layout, 0x72040 bytes)", os.path.getsize(GCI) == 0x72040)
    g1 = gci_rec(r1)
    check("A GCI holds the uploaded pockets and wallet of resident %d" % r1,
          g1[L.REC_OFF_POCKETS:L.REC_OFF_POCKETS + 30] == up[L.REC_OFF_POCKETS:L.REC_OFF_POCKETS + 30]
          and L.record_get_u32(g1, L.REC_OFF_WALLET) == 4321)
    check("A the GCI record equals the host merge expectation for the whole record (client-owned from the upload)",
          g1 == exp_a or all(g1[o:o + n] == exp_a[o:o + n] for _nm, o, n, ow in L.RECORD_FIELD_RANGES if ow in ("client", "shared")))
    try:
        rd = parse_records(REC)
        ok_parse = True
    except AssertionError as ex:
        print("   parse error:", ex)
        rd, ok_parse = None, False
    check("A records.dat parses (independent parser: magic, version 1, size, CRC32)", ok_parse)
    if not ok_parse:
        return
    e1 = rd["e"][r1]
    check("A entry for resident %d: PersonalID == the GCI record's, epoch %d, rev 2 (migrate 1 + upload 2)" % (r1, epoch1),
          e1["present"] == 1 and e1["pid"] == g1[:20] and e1["epoch"] == epoch1 and e1["rev"] == 2)
    check("A entry digest_at_save == FNV-1a-32 of the resident's record in the GCI as saved", e1["digest"] == L.fnv1a32(g1))
    check("A pre-migration BACKUP persisted: the replaced host record (0x2440 B, own checksum)",
          e1["backup_present"] == 1 and e1["backup"] == orig)
    check("A other residents have no entry (no client, host's own slot never synced)",
          all(not rd["e"][i]["present"] for i in range(4) if i != r1))
    gen_a = rd["gen"]

    # =============================== PHASE B ===============================
    host = rig.start_host("B")
    check("B host restarted", host is not None)
    if host is None:
        return
    ht = host.log_text()
    check("B host log: records file loaded OK (generation used 0), lineage RESTORED for resident %d" % r1,
          "load mode=OK " in ht and re.search(r"resident %d lineage RESTORED \(epoch %d rev 2" % (r1, epoch1), ht) is not None)
    f = rig.join(r1, "B1")
    push = f.rec_pushes[-1]
    check("B first join of the same resident: PUSH_FULL (NO MIGRATE_REQUEST) rev 2, stored epoch",
          not any(a.status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST for _c, a in f.rec_acks)
          and push["kind"] == L.PC_NETGAME_REC_KIND_PUSH_FULL and push["rev"] == 2 and push["epoch"] == epoch1)
    check("B pushed record == the stored (GCI) record, byte for byte", push["data"] == g1 and push["digest_ok"])
    check("B new host_session (random per process)", push["session"] != sess1 and push["session"] != 0)
    check("B host log: no MIGRATE_REQUEST sent after the restart", "MIGRATE_REQUEST sent" not in host.log_text())
    L.pump_sleep(1.8)
    up_b = L.record_set_u32(push["data"], L.REC_OFF_WALLET, 7777)
    x = f.upload_record(up_b, base=(push["epoch"], push["rev"]))
    ack = f.wait_record_ack(xfer_id=x)
    check("B upload (wallet 7777) APPLIED rev 3", ack is not None and ack.status == 0 and ack.rev == 3)
    t_drop = time.monotonic()
    off = len(host.log_text())
    rig.release(f, abandon=True)  # vanish without DISCONNECT (like a crashed client)
    m = host.wait_for_log(r"\[PC\] early save \(dirty client disconnect\) OK", 25.0, since_offset=off)
    dt = time.monotonic() - t_drop
    check("B (early save) ABRUPT drop with an unsaved accepted upload -> an early host save happened (%.1f s after the drop; budget "
          "= 5 s peer timeout + 5 s + margin, far below the 60 s periodic interval)" % dt, m is not None and dt < 20.0)
    tail = host.log_text()[off:]
    check("B early save: no periodic save in between, and records.dat written after that GCI save",
          "periodic save" not in tail.split("early save (dirty client disconnect)")[0]
          and re.search(r"GCI save: written successfully.*\n(?:.*\n)*?.*records\.dat written \(after the GCI save", tail) is not None)
    try:
        rd2 = parse_records(REC)
    except AssertionError as ex:
        print("   parse error:", ex)
        rd2 = None
    g2 = gci_rec(r1)
    check("B after the early save: GCI wallet 7777, records.dat rev 3, newer generation, digest == the new GCI record",
          rd2 is not None and L.record_get_u32(g2, L.REC_OFF_WALLET) == 7777 and rd2["e"][r1]["rev"] == 3
          and rd2["gen"] > gen_a and rd2["e"][r1]["digest"] == L.fnv1a32(g2) and rd2["e"][r1]["epoch"] == epoch1)
    check("B previous generation preserved as records.dat.bak1 (valid, rev 2)",
          os.path.isfile(REC + ".bak1") and parse_records(REC + ".bak1")["e"][r1]["rev"] == 2)
    n_early = count(r"early save \(dirty client disconnect\)", host.log_text())
    f = rig.join(r1, "B2")
    check("B reconnect gets rev 3 (a fresh PUSH_FULL of the saved record)", f.rec_pushes[-1]["rev"] == 3
          and f.rec_pushes[-1]["data"] == g2)
    L.pump_sleep(1.0)
    rig.release(f)
    L.pump_sleep(9.0)
    check("B a clean connect/disconnect with NO unsaved upload does not trigger another early save",
          count(r"early save \(dirty client disconnect\)", host.log_text()) == n_early)
    posted, code = rig.stop_graceful()
    check("B graceful stop ok (exit code 0)", posted and code == 0)
    rd3 = parse_records(REC)
    check("B after the shutdown save records.dat is still valid, rev 3", rd3["e"][r1]["rev"] == 3)

    # =============================== PHASE C1 ===============================
    g_c = gci_rec(r1)
    flip_byte(REC, 5000)
    corrupt_before = glob.glob(REC + ".corrupt-*")
    host = rig.start_host("C1")
    check("C1 host restarted with a corrupt records.dat", host is not None)
    if host is None:
        return
    ht = host.log_text()
    check("C1 host log: records.dat UNREADABLE (checksum), RECOVERED from .bak1, load mode=OK_BACKUP, bad file preserved",
          "file checksum mismatch" in ht and "RECOVERED from generation .bak1" in ht and "load mode=OK_BACKUP" in ht
          and "preserved as" in ht)
    bad = glob.glob(REC + ".corrupt-*")
    check("C1 the corrupt file was moved aside, never deleted (records.dat.corrupt-<timestamp> exists)", len(bad) == len(corrupt_before) + 1)
    check("C1 log: digest MISMATCH -> rev kept, epoch re-rolled (the .bak1 generation predates the last GCI save)",
          re.search(r"resident %d digest MISMATCH.*rev 2 kept" % r1, ht) is not None)
    f = rig.join(r1, "C1a")
    push = f.rec_pushes[-1]
    check("C1 first join: NO migrate, PUSH_FULL == the host GCI record (host wins), rev 2 (the .bak1 lineage), epoch re-rolled",
          not any(a.status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST for _c, a in f.rec_acks) and push["data"] == g_c
          and push["rev"] == 2 and push["epoch"] != epoch1)
    check("C1 host log: no MIGRATE_REQUEST", "MIGRATE_REQUEST sent" not in host.log_text())
    rig.release(f)
    posted, code = rig.stop_graceful()
    check("C1 graceful stop ok", posted and code == 0)
    try:
        rd4 = parse_records(REC)
        check("C1 a fresh valid records.dat was written by the next GCI save (rev 2, digest matches)",
              rd4["e"][r1]["rev"] == 2 and rd4["e"][r1]["digest"] == L.fnv1a32(gci_rec(r1)))
    except AssertionError as ex:
        print("   parse error:", ex)
        check("C1 a fresh valid records.dat was written by the next GCI save", False)

    # =============================== PHASE C2 ===============================
    gens = [p for p in (REC, REC + ".bak1", REC + ".bak2") if os.path.isfile(p)]
    for p in gens:
        flip_byte(p, 6000)
    pre_corrupt = set(glob.glob(os.path.join(MPDIR, "*.corrupt-*")))
    g_c2 = gci_rec(r1)
    host = rig.start_host("C2")
    check("C2 host restarted with ALL generations (%d) corrupt" % len(gens), host is not None)
    if host is None:
        return
    ht = host.log_text()
    check("C2 host log: UNTRUSTED MODE entered loudly, migration disabled, host save wins",
          "load mode=UNTRUSTED" in ht and "UNTRUSTED MODE" in ht and "ALL records generations are unreadable" in ht)
    now_corrupt = set(glob.glob(os.path.join(MPDIR, "*.corrupt-*")))
    check("C2 every bad generation preserved aside (+%d new corrupt-* files; the C1 file still exists; none deleted)" % len(gens),
          len(now_corrupt) == len(pre_corrupt) + len(gens) and pre_corrupt <= now_corrupt)
    f = rig.join(r1, "C2a")
    push = f.rec_pushes[-1]
    check("C2 first join: NO migrate (an old client record must not overwrite the host's progress), PUSH_FULL rev 1 == the GCI record",
          not any(a.status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST for _c, a in f.rec_acks) and push["rev"] == 1
          and push["data"] == g_c2)
    check("C2 host log: UNTRUSTED -> rev 1 for the resident, no MIGRATE_REQUEST",
          re.search(r"resident %d UNTRUSTED -> rev 1" % r1, host.log_text()) is not None and "MIGRATE_REQUEST sent" not in host.log_text())
    rig.release(f)
    posted, code = rig.stop_graceful()
    check("C2 graceful stop ok", posted and code == 0)
    try:
        rd5 = parse_records(REC)
        check("C2 after the next GCI save a fresh valid records.dat exists (UNTRUSTED residents persisted as rev 1)",
              rd5["e"][r1]["rev"] == 1)
    except AssertionError as ex:
        print("   parse error:", ex)
        check("C2 fresh valid records.dat", False)

    # =============================== PHASE D ===============================
    g_d = gci_rec(r1)
    for p in glob.glob(REC + "*"):
        os.remove(p)  # deliberate operator reset: records.dat, .bak* AND the preserved *.corrupt-* files
    check("D records.dat (+ .bak* + *.corrupt-*) deleted by the operator, GCI kept", not glob.glob(REC + "*") and os.path.isfile(GCI))
    host = rig.start_host("D")
    check("D host restarted without records.dat", host is not None)
    if host is None:
        return
    check("D host log: load mode=MISSING (operator reset removed the corrupt-* files too)", "load mode=MISSING" in host.log_text())
    stale = orig  # the original (older) record: a client that played offline
    f = rig.join(r1, "D1", migrate_payload=stale)
    push = f.rec_pushes[-1]
    check("D documented missing-file semantics: MIGRATE_REQUEST sent again, the (stale) client record is imported once (rev 1)",
          any(a.status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST for _c, a in f.rec_acks) and push["rev"] == 1
          and push["data"] == L.record_merge_expected(g_d, stale))
    rig.release(f)
    posted, code = rig.stop_graceful()
    check("D graceful stop ok", posted and code == 0)
    try:
        rd6 = parse_records(REC)
        e6 = rd6["e"][r1]
        check("D records.dat rewritten: rev 1 and the pre-migration BACKUP holds the host record replaced by the import",
              e6["rev"] == 1 and e6["backup_present"] == 1 and e6["backup"] == g_d)
    except AssertionError as ex:
        print("   parse error:", ex)
        check("D records.dat rewritten with backup", False)

    # =============================== PHASE F / G (other town / reassigned residents must not wipe lineage) ===============================
    rd_before = parse_records(REC)
    e_before = dict(rd_before["e"][r1])
    gci_orig = open(GCI, "rb").read()
    swapped = bytearray(gci_orig)
    for idx in (1, 2, 3):  # other resident set: change the first PersonalID name byte of every client resident, main + backup copy
        for base in (0x40 + 0x26000 + 0x20, 0x40 + 0x4C000 + 0x20):
            swapped[base + idx * 0x2440] ^= 0x01
    open(GCI, "wb").write(bytes(swapped))
    host = rig.start_host("F")
    check("F host started on a save whose client residents have OTHER PersonalIDs", host is not None)
    if host is None:
        return
    ht = host.log_text()
    check("F host log: the slot with a stored entry reports 'PersonalID differs from the stored entry' (rev 0, entry kept, backup NOT adopted)",
          count(r"resident %d PersonalID differs from the stored entry" % r1, ht) == 1 and "load mode=OK " in ht)
    posted, code = rig.stop_graceful()
    check("F graceful stop ok", posted and code == 0)
    try:
        rdf = parse_records(REC)
        ef = rdf["e"][r1]
        check("F after hosting the other resident set the ORIGINAL lineage of resident %d is intact (pid, epoch, rev 1, digest, backup)" % r1,
              ef["present"] == 1 and ef["pid"] == e_before["pid"] and ef["epoch"] == e_before["epoch"] and ef["rev"] == e_before["rev"]
              and ef["digest"] == e_before["digest"] and ef["backup_present"] == 1 and ef["backup"] == e_before["backup"])
        check("F the other slots' stored entries are unchanged too", all(rdf["e"][i] == rd_before["e"][i] for i in range(4)))
    except AssertionError as ex:
        print("   parse error:", ex)
        check("F records.dat still valid", False)
    open(GCI, "wb").write(gci_orig)  # swap the original town back
    host = rig.start_host("G")
    check("G host restarted on the ORIGINAL save", host is not None)
    if host is None:
        return
    check("G host log: original lineage RESTORED for resident %d (rev %d)" % (r1, e_before["rev"]),
          re.search(r"resident %d lineage RESTORED \(epoch %d rev %d" % (r1, e_before["epoch"], e_before["rev"]), host.log_text()) is not None)
    f = rig.join(r1, "G1")
    check("G first join: PUSH_FULL rev %d, NO migrate" % e_before["rev"],
          not any(a.status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST for _c, a in f.rec_acks) and f.rec_pushes[-1]["rev"] == e_before["rev"])
    rig.release(f)
    posted, code = rig.stop_graceful()
    check("G graceful stop ok", posted and code == 0)

    # =============================== PHASE E (UNTRUSTED is durable immediately and sticky) ===============================
    for q in [REC, REC + ".bak1", REC + ".bak2"]:
        if os.path.isfile(q):
            flip_byte(q, 7000)
    host = rig.start_host("E1")
    check("E1 host started with every generation corrupt", host is not None)
    if host is None:
        return
    ht = host.log_text()
    check("E1 UNTRUSTED entered and its decision written to records.dat IMMEDIATELY (before any GCI save)",
          "load mode=UNTRUSTED" in ht and "UNTRUSTED decision persisted immediately" in ht and "GCI save: written successfully" not in ht.split("UNTRUSTED decision persisted immediately")[0].split("hosting on UDP")[-1])
    try:
        rde = parse_records(REC)
        check("E1 records.dat exists right after start: residents rev 1 (host wins), valid", rde["e"][r1]["rev"] == 1)
    except (AssertionError, OSError) as ex:
        print("   parse error:", ex)
        check("E1 records.dat exists right after start", False)
    check("E1 no periodic/early save happened yet (kill happens inside the ~60 s window)", "periodic save" not in host.log_text())
    host.stop()  # hard kill (terminate): NO shutdown save
    rig.host = None
    host = rig.start_host("E2")
    check("E2 restart after the kill with NO save in between", host is not None)
    if host is None:
        return
    ht = host.log_text()
    f = rig.join(r1, "E2a")
    check("E2 still not 'missing': load mode=OK from the immediately written file, rev 1, NO migrate",
          "load mode=OK " in ht and not any(a.status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST for _c, a in f.rec_acks)
          and f.rec_pushes[-1]["rev"] == 1)
    rig.release(f)
    host.stop()
    rig.host = None
    for q in glob.glob(REC + "*"):
        if ".corrupt-" not in q:
            os.remove(q)  # belt and braces: valid generations gone, preserved *.corrupt-* files remain
    host = rig.start_host("E3")
    check("E3 host started with no generation but preserved *.corrupt-* files", host is not None)
    if host is None:
        return
    ht = host.log_text()
    f = rig.join(r1, "E3a")
    check("E3 load() stays UNTRUSTED (not MISSING) and the migration stays disabled",
          "load mode=UNTRUSTED" in ht and "preserved *.corrupt-* file(s) exist" in ht
          and not any(a.status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST for _c, a in f.rec_acks) and f.rec_pushes[-1]["rev"] == 1)
    rig.release(f)
    posted, code = rig.stop_graceful()
    check("E3 graceful stop ok", posted and code == 0)

    allcl = rig.clients
    for c in allcl:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9900)
    args = ap.parse_args()
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
