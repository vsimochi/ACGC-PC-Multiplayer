#!/usr/bin/env python3
"""test_guest_real_client.py - GUESTS G2: a REAL game client playing a GUEST (a foreigner) in the host's town, REAL host.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation of the game, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-guest GUESTR,HOMETWN,0x4A07,0x5B01 [--d3-test-wallet-add N]` (a NEW process every run)
`--bootstrap-guest` is the TEST-ONLY, default-off, CLIENT-only hook that stands in for the (unreachable in netplay) train arrival: the process binds a
foreigner (Common player_no = mPr_FOREIGNER, Now_Private = a passport copy of a resident record of the loaded town re-keyed to the guest's HOME
PersonalID) and spawns at the station. Everything asserted comes from the two processes' own '[NET]' log lines, the files they wrote
(guests.dat parsed by the protocol test's independent parser, guest_token.dat parsed here by an independent parser) and byte-exact checks of the host
record through a scripted FakeClient guest session.

  R1  first contact: the client reaches READY as a foreigner, sends IDENTITY_EXT, the host MINTS the token (slot 0), the client PERSISTS it
      (save/mp/guest_token.dat == the token in guests.dat), the D3 record flow runs with record_class GUEST (MIGRATE -> rev 1 -> PUSH_FULL -> adopt)
  R2  a NEW client process presents the stored token: KNOWN, verified by the host, NO migration, adopts the host record; --d3-test-wallet-add 100 makes
      it upload a change (class GUEST) which the host applies; a FakeClient guest session then sees the wallet + 100
  R3  a client whose token file was tampered (wrong token) is REFUSED by the host; a client without a token file is REFUSED too
NOT covered (documented): the vanilla train arrival (RIDE_OFF_DEMO is replayed by the hook's door data but not visually verified), any inventory UI,
a guest's shop / pickup through the REAL client request path (the host transactions for guests are protocol-tested in test_guest_protocol.py).
Run ONLY on the disposable pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN=<absolute path>, ports 11600+). The fixture save dir is snapshotted at start and
restored at the end (and before R1). At most 1 real client + 1 FakeClient at a time.
Usage: python test_guest_real_client.py [--port 11600]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time
import zlib

import net_spike_lib as L
import test_guest_protocol as TG

SAVE_DIR_REL = "save"
HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = "GUESTR,HOMETWN,0x4A07,0x5B01"
GUEST = L.GuestIdentity(b"GUESTR  ", 0x4A07, b"HOMETWN ", 0x5B01)   # names are SPACE padded like every vanilla name


def parse_token_file(path):
    """Independent parser of the guest_token.dat v1 layout (pc_mp_guests.h): returns the list of present entries."""
    with open(path, "rb") as f:
        b = f.read()
    assert len(b) == 32 + 4 * 64 + 4 and b[:8] == b"ACMPGTK\x00", "size/magic"
    ver, flags, n, esz, gen, rsv = struct.unpack("<6I", b[8:32])
    assert (ver, flags, n, esz, rsv) == (1, 0, 4, 64, 0)
    assert struct.unpack("<I", b[-4:])[0] == (zlib.crc32(b[:-4]) & 0xFFFFFFFF), "crc32"
    out = []
    for i in range(4):
        e = b[32 + i * 64:32 + (i + 1) * 64]
        if e[0]:
            out.append({"land_name": bytes(e[4:12]), "land_id": struct.unpack("<H", e[12:14])[0], "hash": struct.unpack("<I", e[16:20])[0],
                        "pid": bytes(e[28:48]), "token": bytes(e[48:64])})
    return out


class Rig:
    def __init__(self, port, results, save_dir, snap_dir):
        self.port, self.results, self.save_dir, self.snap_dir = port, results, save_dir, snap_dir
        self.check = lambda d, c: L.check(d, c, results)
        self.host = None
        self.n = 0

    def restore_save(self):
        shutil.rmtree(self.save_dir, ignore_errors=True)
        shutil.copytree(self.snap_dir, self.save_dir)

    def start_host(self):
        for i in range(3):
            h = L.HostProcess(port=self.port, extra_args=["--bootstrap-resident", "0"],
                              log_path=os.path.join(HERE, "guest_real_host_try%d.log" % i), bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
                self.host = h
                return True
            h.stop()
            time.sleep(2.0)
        return False

    def start_client(self, name, extra=()):
        self.n += 1
        return L.ClientProcess("127.0.0.1:%d" % self.port, extra_args=["--bootstrap-guest", SPEC] + list(extra),
                               log_path=os.path.join(HERE, "guest_real_%s.log" % name), bin_dir=L.GAME_BIN_DIR, label=name).start()


def fake_guest(rig, label, token):
    c = L.FakeClient(label, "127.0.0.1", rig.port, guest=GUEST, guest_token=token)
    c.connect_and_ready(quiet=True)
    return c


def release(c):
    try:
        if c.state in (c.STATE_CONNECTED, c.STATE_PENDING):
            c.disconnect()
    except Exception:  # noqa: BLE001
        pass
    c.close()
    L.pump_sleep(0.8)


def run(args, results, rig):
    check = rig.check
    ip = "127.0.0.1"
    tokfile = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL, "mp", "guest_token.dat")
    guests = TG.guests_path()
    rig.restore_save()
    if not rig.start_host():
        check("host reached genuine field-ready state", False)
        return
    check("host reached genuine field-ready state", True)
    host = rig.host
    town = L.resolve_host_town(ip, args.port)

    # ---------------- R1: first contact ----------------
    cl = rig.start_client("r1")
    m = cl.wait_for_log(r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=(\d+) kind=FULL", 120.0)
    ct = cl.log_text()
    print("--- client R1 log tail ---")
    print("\n".join(ct.splitlines()[-25:]))
    check("R1 the guest bootstrap ran: foreigner bound, arriving at the station (SCENE_FG)", "--bootstrap-guest: guest 'GUESTR  '" in ct and "bound as a foreigner" in ct)
    check("R1 the real client reached READY as a foreigner (pcfa_save_ready accepted it): handshake complete", "handshake complete, town verified" in ct)
    check("R1 client log: IDENTITY_EXT sent before the IDENTITY (token not held: first contact)", "playing a guest -- sent IDENTITY_EXT (token not held (first contact))" in ct)
    check("R1 host log: bound to GUEST slot 0 (first contact: token minted); the IDENTITY_EXT was cached as a guest claim",
          re.search(r"peer \d+ bound to GUEST slot 0 \(first contact: token minted;", host.log_text()) is not None
          and "IDENTITY_EXT cached (guest=1, token_present=0)" in host.log_text())
    check("R1 client log: the token was saved to save/mp/guest_token.dat", "guest token (slot 0) saved to save/mp/guest_token.dat" in ct)
    check("R1 client log: MIGRATE upload applied (rev 1), PUSH_FULL adopted (class GUEST on both directions)",
          "host asks for a MIGRATE upload" in ct and re.search(r"MIGRATE upload xfer 1 APPLIED by the host \(rev 1\)", ct) is not None and m is not None)
    check("R1 no violation / BAD DIGEST / ADOPT_FAILED / refusal in the client log", not re.search(r"violation \d/\d|BAD[_ ]DIGEST|ADOPT_FAILED|\*\*\* host REFUSED|host rejected the connection", ct))
    try:
        gf = TG.parse_guests(guests)
        tf = parse_token_file(tokfile)
        e0 = gf["e"][0]
        check("R1 guests.dat (host) and guest_token.dat (client) agree: the SAME 16-byte token; the key == the guest's HOME PersonalID",
              e0["present"] and e0["pid"] == L.guest_pid_be(GUEST) and len(tf) == 1 and tf[0]["token"] == e0["token"] and tf[0]["pid"] == e0["pid"])
        check("R1 guest_token.dat is keyed by the HOST TOWN (land name / id) the client matched, and is client metadata only (no .gci, in save/mp)",
              tf[0]["land_name"] == bytes(town.land_name) and tf[0]["land_id"] == town.land_id and tf[0]["hash"] == town.terrain_hash
              and not any(n.lower().endswith(".gci") for n in os.listdir(os.path.dirname(tokfile))))
        tok = e0["token"]
    except (AssertionError, OSError) as exc:
        check("R1 the token files parse (%s)" % exc, False)
        cl.stop()
        return
    off_log = len(host.log_text())
    cl.stop()
    L.pump_sleep(7.0)  # the host notices the dead client (transport timeout) before the same key reconnects
    f1 = fake_guest(rig, "F1", tok)
    w1 = L.record_get_u32(f1.rec_pushes[-1]["data"], L.REC_OFF_WALLET)
    rev1 = f1.rec_pushes[-1]["rev"]
    check("R1 a FakeClient guest session with the token (KNOWN, same slot) is pushed the host record (rev %d)" % rev1,
          f1.token_msgs[0][1].flags == 2 and f1.token_msgs[0][1].guest_slot == 0 and rev1 >= 1)
    release(f1)

    # ---------------- R2: a new process presents the stored token ----------------
    cl2 = rig.start_client("r2", ["--d3-test-wallet-add", "100"])
    m2 = cl2.wait_for_log(r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=(\d+) kind=FULL", 120.0)
    ct2 = cl2.log_text()
    check("R2 client log: IDENTITY_EXT carries the stored token", "sent IDENTITY_EXT (token held)" in ct2)
    check("R2 client log: the host verified the token (KNOWN)", "guest token verified by the host (guest slot 0)" in ct2)
    check("R2 host log: bound to GUEST slot 0 (known guest: token verified)", re.search(r"bound to GUEST slot 0 \(known guest: token verified;", host.log_text()[off_log:]) is not None)
    check("R2 NO re-migration: the client never asked for / sent a MIGRATE; it adopted the host record", m2 is not None
          and "host asks for a MIGRATE upload" not in ct2 and "MIGRATE upload" not in ct2)
    mw = cl2.wait_for_log(r"--d3-test-wallet-add 100: local wallet (\d+) -> (\d+)", 60.0)
    check("R2 the test hook raised the guest's wallet by 100 locally", mw is not None and int(mw.group(2)) == int(mw.group(1)) + 100)
    ma = cl2.wait_for_log(r"client: upload xfer \d+ APPLIED \(epoch \d+ rev (\d+)\)", 60.0)
    check("R2 the wallet change was UPLOADED (class GUEST) and APPLIED by the host (rev bumped)", ma is not None and int(ma.group(1)) > rev1)
    cl2.stop()
    L.pump_sleep(7.0)
    f2 = fake_guest(rig, "F2", tok)
    w2 = L.record_get_u32(f2.rec_pushes[-1]["data"], L.REC_OFF_WALLET)
    check("R2 the host's guest record carries the uploaded wallet: %d -> %d (+100)" % (w1, w2), w2 == w1 + 100 and f2.rec_pushes[-1]["rsv"] == 1)
    release(f2)

    # ---------------- R3: wrong / missing token ----------------
    with open(tokfile, "rb") as f:
        good = f.read()
    off_log = len(host.log_text())
    bad = bytearray(good)
    bad[32 + 48] ^= 0x5A                                       # flip a token byte ...
    struct.pack_into("<I", bad, len(bad) - 4, zlib.crc32(bytes(bad[:-4])) & 0xFFFFFFFF)  # ... and re-sign the file CRC (a valid file with the WRONG token)
    with open(tokfile, "wb") as f:
        f.write(bytes(bad))
    cl3 = rig.start_client("r3")
    m3 = cl3.wait_for_log(r"host rejected the connection", 90.0)
    check("R3 a client holding a WRONG token is REFUSED by the host (log) and the client reports the rejection", m3 is not None
          and "presented a WRONG token" in host.log_text()[off_log:])
    cl3.stop()
    L.pump_sleep(2.0)
    os.remove(tokfile)
    off_log = len(host.log_text())
    cl4 = rig.start_client("r4")
    m4 = cl4.wait_for_log(r"host rejected the connection", 90.0)
    check("R3 a client WITHOUT a token file (a squatter / a lost token) is REFUSED: 'known guest key presented WITHOUT a token'", m4 is not None
          and "presented WITHOUT a token" in host.log_text()[off_log:])
    cl4.stop()
    check("the host stayed alive and logged no INTERNAL error", host.alive() and "*** INTERNAL" not in host.log_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11600)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_real_client_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    rig = Rig(args.port, results, save_dir, snap_dir)
    try:
        run(args, results, rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
