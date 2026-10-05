#!/usr/bin/env python3
"""test_pdata_protocol.py - PERSONAL DATA sync (diary only; kinds 3 PDATA_COMMIT / 4 PDATA_PUSH on the HOUSE_BEGIN / HOUSE_CHUNK / HOUSE_ACK ids 59..61), HOST half.

TIER: PROTOCOL TESTED -- REAL host processes (`AnimalCrossing.exe --host <port> --bootstrap-resident 0 [--personal-sync on|off | --town-serve on --house-sync]`, started / stopped
by this script) driven by scripted FakeClients (net_spike_lib: commit_pdata() / wait_pdata_push()). The host's C code runs for real. The CLIENT half (adopt only with no menu open,
the commit trigger after the diary menu closes) is NOT exercised here: no player opens a diary menu in a process test; it is covered by the source audit test_pdata_src.py only.
The host's "own player has the diary open -> BUSY" branch is not reachable either (needs a real menu); source audited only.

DISPOSABLE FIXTURE: a copy of pc\\build64\\bin_fixture4 (NEVER touched, only read) in $PDATA_TEST_FIXTURE (default pc\\build64\\bin_fixture4_pdata; the basename must start with
bin_fixture4) plus the freshly built pc\\build64\\bin\\AnimalCrossing.exe. The live save dir, bin_talkfix and the other fixtures are never read or written.

Phases (one host process each; --only P1,P2,P3):
  P1  host --personal-sync on (NO --house-sync): HOST_CONFIG byte 1 == 0x02 (only the personal bit); each resident client is pushed ITS OWN slot only (12 chunks, digest ok, page = the
      slot, rev 1, bytes == the GCI's slot); a commit built on that base -> APPLIED (rev + 1, host log, the OTHER client gets nothing); a commit whose page field names ANOTHER slot
      -> BAD_SHAPE detail 5 and the bound slot is pushed back; unknown block -> BAD_SHAPE detail 4; a commit on an old base -> STALE (+ canonical push back); a wrong digest ->
      BAD_DIGEST; a 0x7F byte and a 0x80 byte -> INVALID_CELL (detail = offset), nothing applied; a second valid commit APPLIED; a GUEST client gets no push and NOT_OWNER;
      graceful stop: the GCI holds the committed bytes in the committed slot and every other slot is byte-identical to the fixture
  P2  host WITHOUT any flag (AUTO: town_serve is off -> personal sync off): HOST_CONFIG byte 1 == 0, no push, a commit is dropped (no ACK, nothing applied)
  P3  host --town-serve on --house-sync (AUTO on because the town is served): byte 1 == 0x03 (both bits), the house pushes still arrive, a diary commit shares the xfer counter with
      the house path and is APPLIED
Usage: python test_pdata_protocol.py [--port 11900] [--only P1,P2,P3]
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
SRC_BIN = os.path.join(BUILD64, "bin_fixture4")                       # read-only source of the copy
NEW_EXE = os.path.join(BUILD64, "bin", "AnimalCrossing.exe")          # the freshly built exe (read-only input)
DST_BIN = os.path.abspath(os.environ.get("PDATA_TEST_FIXTURE", os.path.join(BUILD64, "bin_fixture4_pdata")))
MARKER = ".pdata_sync_fixture"

DIARY_GCI_OFF = 0x40 + 0x1460 + 0xBAC0 + 0xCCA0   # file offset of the diary ARAM block in the GCI (CARDDir 0x40 + Others offset + mail + original)
SLOT_SIZE = 0x2E80


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
    open(os.path.join(DST_BIN, MARKER), "w").write("disposable fixture of test_pdata_protocol.py\n")


os.environ["NET_SPIKE_GAME_BIN"] = DST_BIN
if __name__ == "__main__":
    make_fixture()

sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402


def gci_diary_slots(path=None):
    d = open(path or os.path.join(DST_BIN, L.SAVE_GCI_REL), "rb").read()
    return [d[DIARY_GCI_OFF + 2 + s * SLOT_SIZE: DIARY_GCI_OFF + 2 + (s + 1) * SLOT_SIZE] for s in range(4)]


def log_has(host, rx):
    return re.search(rx, host.log_text()) is not None


def diary_text(msg, month=0):
    """A diary slot image: `msg` at the start of month `month`, spaces everywhere else (what the vanilla editor leaves behind)."""
    buf = bytearray(b"\x20" * SLOT_SIZE)
    buf[month * 992: month * 992 + len(msg)] = msg
    return bytes(buf)


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
            h = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"] + list(extra), log_path=os.path.join(HERE, "pdata_%s_try%d.log" % (tag, i)),
                              bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
                self.host, self.port, self.tag = h, port, tag
                L.resolve_host_town("127.0.0.1", port)
                return h
            h.stop()
            time.sleep(2.0)
        return None

    def join(self, slot, label):
        c = L.FakeClient(label, "127.0.0.1", self.port, player=L.resident_player(slot))
        c.rec_resident_idx = slot
        c.connect_and_ready(quiet=True)
        self.clients.append(c)
        return c

    def stop_graceful(self):
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


def residents():
    return [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]


def run_p1(rig):
    check = rig.check
    host = rig.start_host("P1", ["--personal-sync", "on"])
    check("P1 host reached the field-ready state with --personal-sync on and WITHOUT --house-sync", host is not None)
    if host is None:
        return
    res = residents()
    check("P1 fixture has >= 2 non-host residents %s" % res, len(res) >= 2)
    ia, ib = res[0], res[1]
    gci0 = gci_diary_slots()
    check("P1 the fixture's GCI diary slots are readable (4 x 0x2E80 B)", all(len(s) == SLOT_SIZE for s in gci0))
    a = rig.join(ia, "A")
    b = rig.join(ib, "B")
    pa = a.wait_pdata_push(timeout=8.0)
    pb = b.wait_pdata_push(timeout=8.0)
    hc = a.ts_latest(L.PC_NETGAME_TS_HOSTCFG)
    check("P1 HOST_CONFIG byte 1 == 0x02 (personal bit only: house sync is off), every other byte zero",
          hc is not None and len(hc[1]) == 8 and hc[1][0] == 0 and hc[1][1] == L.HOSTCFG_FLAG_PERSONAL_SYNC and hc[1][2:] == bytes(6))
    check("P1 client A got ONE PDATA_PUSH: kind 4 shape (12 chunks, digest ok, 0x2E80 B), block 0, page = ITS OWN slot %d, rev 1" % ia,
          pa is not None and pa["digest_ok"] and pa["count"] == 12 and pa["total"] == 0x2E80 and pa["block"] == 0 and pa["slot"] == ia and pa["rev"] == 1 and pa["session"] != 0)
    check("P1 client B got its own slot %d (not A's)" % ib, pb is not None and pb["digest_ok"] and pb["slot"] == ib and pb["rev"] == 1)
    check("P1 the pushed bytes equal the GCI's diary slot of that resident", pa is not None and pa["data"] == gci0[ia] and pb is not None and pb["data"] == gci0[ib])
    L.pump_sleep(1.5)
    check("P1 each client was pushed exactly one slot (never another resident's, no house push without --house-sync)",
          len(a.pdata_pushes_of()) == 1 and len(b.pdata_pushes_of()) == 1 and len(a.house_pushes_of()) == 0)
    sess, rev0 = pa["session"], pa["rev"]
    # --- a valid commit
    d1 = diary_text(b"Dear diary, today I fished.", 0)
    x1 = a.commit_pdata(d1, (sess, rev0))
    ack = a.wait_house_ack(x1)
    check("P1 a valid commit built on (session, rev %d) is APPLIED: page rev %d, ACK epoch = block 0, rev field = slot %d" % (rev0, rev0 + 1, ia),
          ack is not None and ack.status == 0 and ack.house_seq == rev0 + 1 and ack.epoch == 0 and ack.rev == ia and ack.host_session == sess)
    check("P1 host log: PDATA_COMMIT APPLIED for slot %d" % ia, log_has(host, r"PDATA_COMMIT xfer %d diary slot %d APPLIED \(page rev %d\)" % (x1, ia, rev0 + 1)))
    L.pump_sleep(1.5)
    check("P1 the committer is not pushed back and the other client is pushed nothing", len(a.pdata_pushes_of()) == 1 and len(b.pdata_pushes_of()) == 1)
    # --- message slot != bound slot: BAD_SHAPE, nothing applied, the BOUND slot is pushed back
    na = len(a.pdata_pushes_of())
    evil = diary_text(b"overwrite B", 3)
    x = a.commit_pdata(evil, (sess, rev0 + 1), slot=ib)
    ack = a.wait_house_ack(x)
    check("P1 a commit whose page field names ANOTHER resident's slot -> BAD_SHAPE detail 5 (the page field never selects a slot)",
          ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_BAD_SHAPE and ack.detail == 5)
    p2 = a.wait_pdata_push(after=na, timeout=6.0)
    check("P1 ... and the canonical bound slot is pushed back (own slot, rev %d, the committed bytes)" % (rev0 + 1), p2 is not None and p2["slot"] == ia and p2["rev"] == rev0 + 1 and p2["data"] == d1)
    x = a.commit_pdata(evil, (sess, rev0 + 1), block=1)
    ack = a.wait_house_ack(x)
    check("P1 an unknown block id -> BAD_SHAPE detail 4", ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_BAD_SHAPE and ack.detail == 4)
    # --- stale base
    na = len(a.pdata_pushes_of())
    x = a.commit_pdata(diary_text(b"stale edit", 1), (sess, rev0))
    ack = a.wait_house_ack(x)
    check("P1 a commit built on the OLD page rev %d -> STALE (host rev %d), nothing applied" % (rev0, rev0 + 1), ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_STALE and ack.house_seq == rev0 + 1)
    p3 = a.wait_pdata_push(after=na, timeout=6.0)
    check("P1 ... and the canonical slot is pushed back", p3 is not None and p3["rev"] == rev0 + 1 and p3["data"] == d1)
    x = a.commit_pdata(diary_text(b"stale session", 1), (sess + 1, rev0 + 1))
    ack = a.wait_house_ack(x)
    check("P1 a commit built on a foreign host session -> STALE", ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_STALE)
    # --- bad digest / control bytes
    x = a.commit_pdata(diary_text(b"bad digest", 1), (sess, rev0 + 1), digest=0x12345678)
    ack = a.wait_house_ack(x)
    check("P1 a wrong digest -> BAD_DIGEST", ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_BAD_DIGEST)
    bad7f = bytearray(diary_text(b"ok text", 2))
    bad7f[5 * 992 + 17] = 0x7F
    x = a.commit_pdata(bytes(bad7f), (sess, rev0 + 1))
    ack = a.wait_house_ack(x)
    check("P1 a 0x7F (control code) byte anywhere -> INVALID_CELL, detail = its offset %d" % (5 * 992 + 17),
          ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_INVALID_CELL and ack.detail == 5 * 992 + 17)
    bad80 = bytearray(diary_text(b"ok text", 2))
    bad80[11 * 992 + 991] = 0x80
    x = a.commit_pdata(bytes(bad80), (sess, rev0 + 1))
    ack = a.wait_house_ack(x)
    check("P1 a 0x80 (message tag) byte -> INVALID_CELL, detail = its offset", ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_INVALID_CELL and ack.detail == 11 * 992 + 991)
    # --- a second valid commit on the current base (the refusals changed nothing)
    d2 = bytearray(diary_text(b"Second entry", 0))
    d2[11 * 992: 11 * 992 + 9] = b"December!"
    d2 = bytes(d2)
    assert len(d2) == SLOT_SIZE
    x = a.commit_pdata(d2, (sess, rev0 + 1))
    ack = a.wait_house_ack(x)
    check("P1 after all the refusals a valid commit on rev %d is APPLIED (-> rev %d): the refusals left the state untouched" % (rev0 + 1, rev0 + 2),
          ack is not None and ack.status == 0 and ack.house_seq == rev0 + 2)
    # --- a guest: no push, NOT_OWNER
    g = L.FakeClient("G", "127.0.0.1", rig.port, guest=L.guest_identity("GUESTA", 0x4A01, "HOMETWN", 0x5B01), bind_ip="127.0.21.2")
    rig.clients.append(g)
    g.connect_and_ready(quiet=True)
    L.pump_sleep(2.0)
    check("P1 the host pushed NO diary to the guest", len(g.pdata_pushes_of()) == 0)
    x = g.commit_pdata(d2, (sess, rev0 + 2), slot=ia)
    ack = g.wait_house_ack(x)
    check("P1 a GUEST's commit (naming slot %d) -> NOT_OWNER, refused before any slot index exists" % ia, ack is not None and ack.status == L.PC_NETGAME_HOUSE_ACK_NOT_OWNER)
    g.disconnect()
    check("P1 host still runs", host.alive())
    L.pump_sleep(1.0)
    # --- graceful stop -> GCI
    posted, code = rig.stop_graceful()
    ht = open(host.log_path, "rb").read().decode("utf-8", "replace")
    check("P1 graceful host stop: window close posted, exit code 0, final shutdown save OK", posted and code == 0 and "final shutdown save OK" in ht)
    gci1 = gci_diary_slots()
    check("P1 the GCI holds the committed bytes (the SECOND commit) in resident %d's slot" % ia, gci1[ia] == d2)
    check("P1 every OTHER slot of the diary block is byte-identical to the fixture's (including the one the rejected commit named)", all(gci1[s] == gci0[s] for s in range(4) if s != ia))


def run_p2(rig):
    check = rig.check
    host = rig.start_host("P2")
    check("P2 host reached the field-ready state with NO personal-sync flag (town_serve is off -> AUTO = off)", host is not None)
    if host is None:
        return
    ia = residents()[0]
    gci0 = gci_diary_slots()
    a = rig.join(ia, "A")
    L.pump_sleep(3.0)
    hc = a.ts_latest(L.PC_NETGAME_TS_HOSTCFG)
    check("P2 HOST_CONFIG byte 1 == 0 (the personal bit is ABSENT)", hc is not None and len(hc[1]) == 8 and hc[1][1] == 0)
    check("P2 no PDATA_PUSH was sent", len(a.pdata_pushes_of()) == 0)
    x = a.commit_pdata(diary_text(b"must be ignored", 0), (1, 1))
    ack = a.wait_house_ack(x, timeout=2.5)
    check("P2 a PDATA_COMMIT is dropped without an answer while the feature is off", ack is None and not log_has(host, r"PDATA_COMMIT xfer %d" % x))
    L.pump_sleep(1.0)
    posted, code = rig.stop_graceful()
    check("P2 graceful stop OK and the GCI diary is unchanged", posted and code == 0 and gci_diary_slots() == gci0)


def run_p3(rig):
    check = rig.check
    host = rig.start_host("P3", ["--town-serve", "on", "--house-sync"])
    check("P3 host reached the field-ready state with --town-serve on --house-sync (AUTO personal sync = on)", host is not None)
    if host is None:
        return
    ia = residents()[0]
    a = rig.join(ia, "A")
    pa = a.wait_pdata_push(timeout=8.0)
    a.hub.wait_until(lambda: len({p["house"] for p in a.house_pushes_of()}) >= 4, 8.0)
    hc = a.ts_latest(L.PC_NETGAME_TS_HOSTCFG)
    check("P3 HOST_CONFIG byte 1 == 0x03 (house sync + personal sync)", hc is not None and hc[1][1] == (L.PC_NETGAME_HOSTCFG_FLAG_HOUSE_SYNC | L.HOSTCFG_FLAG_PERSONAL_SYNC))
    check("P3 the diary push arrives (own slot) next to the four house pushes",
          pa is not None and pa["digest_ok"] and pa["slot"] == ia and len({p["house"] for p in a.house_pushes_of() if p["digest_ok"]}) == 4 and not a.house_violations)
    d1 = diary_text(b"coexistence", 4)
    x = a.commit_pdata(d1, (pa["session"], pa["rev"]))
    ack = a.wait_house_ack(x)
    check("P3 a diary commit with house sync on is APPLIED (the xfer counter is shared with the house commits)", ack is not None and ack.status == 0 and ack.house_seq == pa["rev"] + 1)
    check("P3 host still runs", host.alive())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11900)
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
