#!/usr/bin/env python3
"""test_guest_real_client.py - GUESTS G2: a REAL game client playing a GUEST (a foreigner) in the host's town, REAL host.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation of the game, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-guest GUESTR,HOMETWN,0x4A07,0x5B01 [--d3-test-wallet-add N]` (a NEW process every run)
`--bootstrap-guest` is the TEST-ONLY, default-off, CLIENT-only hook that stands in for the (unreachable in netplay) train arrival: the process binds a
foreigner (Common player_no = mPr_FOREIGNER, Now_Private = the passport) and spawns at the station. Guests G1: the passport is a FRESH character
(mPr_ClearPrivateInfo + mPr_InitPrivateInfo + the guest's HOME PersonalID + gender / face / starter shirt given or derived from the identity), NEVER a copy of a
resident: the first-contact MIGRATE uploads that fresh record. Everything asserted comes from the two processes' own '[NET]' log lines, the files they wrote
(guests.dat parsed by the protocol test's independent parser, guest_token.dat parsed here by an independent parser) and byte-exact checks of the host
record through a scripted FakeClient guest session.

  R0  (Guests G1) the client-side early NAME check: a guest named like a resident of the loaded town, and a blank name, make the client exit with code 2
      BEFORE it connects as anything (no guest is created on the host)
  R1  first contact: the client reaches READY as a foreigner, sends IDENTITY_EXT, the host MINTS the token (slot 0), the client PERSISTS it
      (save/mp/guest_token.dat == the token in guests.dat), the D3 record flow runs with record_class GUEST (MIGRATE -> rev 1 -> PUSH_FULL -> adopt)
  R2  a NEW client process presents the stored token: KNOWN, verified by the host, NO migration, adopts the host record; --d3-test-wallet-add 100 makes
      it upload a change (class GUEST) which the host applies; a FakeClient guest session then sees the wallet + 100
  R3  a client whose token file was tampered (wrong token) is REFUSED by the host; a client without a token file is REFUSED too
  R5  (Guests G1) a SECOND real guest with the optional GENDER,FACE arguments (1,5): honoured in the client log and in the record the host holds; the record is
      independent of guest 1's (own name / appearance), no resident name / pocket / wallet in either
  (R1 also asserts the FRESH record: name == NAME, pockets / wallet / bank / letters empty, no resident name, identity-derived appearance, the whole record equal to
   the python mirror fresh_guest_record_for() except the ranges the MODE_PAK arrival init legitimately touches -- catalog bits, maps, calendar / day counters)
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


# Ranges of the guest record that the vanilla MODE_PAK arrival init (mSDI_StartInitAfter on the passport: item-collect bits, map renewal, calendar welcome, complete
# talk flags) or the in-game day logic may touch before the first upload. EVERY OTHER BYTE of the stored record must equal the python mirror exactly.
#   Observed with the real client (R1 / R5): ONLY the catalog bitfields differ (the three item-collect bits mSDI_StartInitAfter sets: mPr_SetItemCollectBit
#   FTR_SUM_CASSE01 / FTR_NOG_COLLEGENOTE / FTR_NOG_MIKANBOX); maps, complete flags and the calendar are byte-identical to the mirror. The calendar / day counter tail
#   stays allowed because it is date dependent (calendar welcome, sunburn day logic), nothing else is.
MAY_DIFFER = [("catalog bitfields (3 item-collect bits)", 0x1108, 0x11DC), ("calendar / day counters / tail (date dependent)", 0x234C, 0x2440)]


def diff_ranges(a, b):
    """[(start, end)] runs of differing bytes of two equal-length images."""
    out, i, n = [], 0, min(len(a), len(b))
    while i < n:
        if a[i] != b[i]:
            j = i
            while j < n and a[j] != b[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def design_oracle():
    """my_org[0..3] (names / textures come from ROM / ARAM): the vanilla defaults every fixture resident carries. Returns (bytes, all residents identical)."""
    recs = [L.record_from_gci(L.GAME_BIN_DIR, i) for i in range(4)]
    d = [bytes(r[L.REC_OFF_MY_ORG:L.REC_OFF_MY_ORG + 4 * L.REC_MY_ORG_SIZE]) for r in recs]
    return d[1], all(x == d[1] for x in d)


def fresh_record_checks(check, tag, rec, g, oracle, gender=None, face=None, resident_names=()):
    """The host-held record `rec` of guest `g` is the FRESH character: python mirror equal outside MAY_DIFFER, empty economy, no resident data."""
    exp = L.fresh_guest_record_for(g, gender=gender, face=face, default_designs=oracle)
    bad = [(a, b) for a, b in diff_ranges(rec, exp) if not any(lo <= a and b <= hi for _n, lo, hi in MAY_DIFFER)]
    allowed = [(a, b) for a, b in diff_ranges(rec, exp) if any(lo <= a and b <= hi for _n, lo, hi in MAY_DIFFER)]
    print("   [%s] record vs python mirror: unexpected diffs %s; allowed (MODE_PAK / day) diffs %s" % (tag, [(hex(a), hex(b)) for a, b in bad], [(hex(a), hex(b)) for a, b in allowed]))
    check("%s the host holds the guest's FRESH record: equal to the python mirror (fresh_guest_record_for) in every byte outside the MODE_PAK-touched ranges" % tag,
          len(rec) == L.PC_NETGAME_REC_SIZE and not bad)
    cat_runs = [(a, b) for a, b in diff_ranges(rec, exp) if 0x1108 <= a and b <= 0x11DC]
    set_bits = sum(bin(rec[i] ^ exp[i]).count("1") for a, b in cat_runs for i in range(a, b))
    check("%s the ONLY change the arrival init made inside the catalog bitfields is item-collect bits being SET (%d bit(s) in %s; vanilla sets exactly 3)" % (tag, set_bits, [(hex(a), hex(b)) for a, b in cat_runs]),
          all((rec[i] & exp[i]) == exp[i] for a, b in cat_runs for i in range(a, b)) and 0 < set_bits <= 3)
    check("%s the record: PersonalID = the guest's HOME PersonalID, exists 1, reset_code 0" % tag,
          rec[:20] == L.guest_pid_be(g) and rec[L.REC_OFF_EXISTS] == 1 and L.record_get_u32(rec, L.REC_OFF_RESET_CODE) == 0)
    check("%s the record: pockets, item conditions, wallet, bank EMPTY (no starter bag, nothing cloned), loan = the vanilla pre-house 100" % tag,
          L.record_inventory(rec) == ((0,) * 15, 0, 0) and L.record_get_u32(rec, L.REC_OFF_BANK) == 0 and L.record_get_u32(rec, L.REC_OFF_LOAN) == 100)
    check("%s the record: all 10 letters EMPTY (font 0xFF), no catalog order, no equipment" % tag,
          all(L.mail_font(rec[L.REC_OFF_MAIL + i * L.REC_MAIL_SIZE:L.REC_OFF_MAIL + (i + 1) * L.REC_MAIL_SIZE]) == L.MAIL_FONT_UNUSED for i in range(10))
          and rec[L.REC_OFF_CATALOG_ORDERS:L.REC_OFF_CATALOG_ORDERS + 20] == b"\x00" * 20 and L.record_get_u16(rec, L.REC_OFF_EQUIPMENT) == 0)
    check("%s the record carries NO resident name (%s) anywhere" % (tag, ", ".join(n.decode("latin-1").strip() for n in resident_names)),
          all(n not in rec for n in resident_names))
    return exp


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

    def start_client(self, name, extra=(), spec=None):
        self.n += 1
        return L.ClientProcess("127.0.0.1:%d" % self.port, extra_args=["--bootstrap-guest", spec or SPEC] + list(extra),
                               log_path=os.path.join(HERE, "guest_real_%s.log" % name), bin_dir=L.GAME_BIN_DIR, label=name).start()


def fake_guest(rig, label, token):
    c = L.FakeClient(label, "127.0.0.1", rig.port, guest=GUEST, guest_token=token)
    c.connect_and_ready(quiet=True)
    return c


def fake_guest_as(rig, label, ident, token):
    c = L.FakeClient(label, "127.0.0.1", rig.port, guest=ident, guest_token=token)
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

    oracle, oracle_ok = design_oracle()
    check("fixture: the 4 residents carry identical my_org[0..3] (the vanilla default designs: the oracle for the ROM / ARAM-derived default design names / textures)", oracle_ok)
    res_names = [bytes(L.resident_player(i).player_name) for i in range(4)]

    # ---------------- R0: the client's early NAME check ----------------
    for tag, spec, rx in (("R0a resident name", "%s,HOMETWN,0x4A07,0x5B01" % res_names[1].decode("latin-1").strip(), r"REFUSED: the guest name '%s' equals the name of resident 1 of this town" % re.escape(res_names[1].decode("latin-1"))),
                          ("R0b blank name", "   ,HOMETWN,0x4A07,0x5B01", r"REFUSED: bad spec '   ,HOMETWN,0x4A07,0x5B01': NAME is not a valid game player name \(blank, or a character the name entry cannot produce\)")):
        # Guests G1.1: a blank name is a SPEC error now caught by the early validator (pc_bootstrap_guest_validate, before the window / save load); the resident-name
        # clash needs the loaded town and is still refused by pc_bootstrap_guest_poll. Both exit with code 2 and a stderr diagnostic.
        c0 = rig.start_client("r0" + tag[2], spec=spec)
        t_end = time.monotonic() + 180.0
        while c0.alive() and time.monotonic() < t_end:
            time.sleep(0.5)
        t0 = c0.log_text()
        check("%s: the client EXITS with code 2 before connecting (alive=%s, exit code %s) and says why" % (tag, c0.alive(), c0.exit_code()),
              not c0.alive() and c0.exit_code() == 2 and re.search(rx, t0) is not None)
        check("%s: no FRESH record was built, no foreigner bound, and the host created no guest (no guests.dat, no 'bound to GUEST')" % tag,
              "FRESH guest record" not in t0 and "bound as a foreigner" not in t0 and not os.path.exists(guests) and "bound to GUEST" not in host.log_text())
        if c0.alive():
            c0.stop()

    # ---------------- R1: first contact ----------------
    cl = rig.start_client("r1")
    m = cl.wait_for_log(r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=(\d+) kind=FULL", 120.0)
    ct = cl.log_text()
    print("--- client R1 log tail ---")
    print("\n".join(ct.splitlines()[-25:]))
    check("R1 the guest bootstrap ran: foreigner bound, arriving at the station (SCENE_FG)", "--bootstrap-guest: guest 'GUESTR  '" in ct and "bound as a foreigner" in ct)
    mf = re.search(r"--bootstrap-guest: FRESH guest record \(not a copy of any resident\): gender=(\d+) face=(\d+) shirt=0x([0-9A-Fa-f]+) \(derived from the identity\)", ct)
    dg, df, ditem, _didx = L.guest_fresh_derive(L.guest_pid_be(GUEST))
    check("R1 (G1) client log: a FRESH guest record was built (not a resident copy); gender / face / shirt = the values DERIVED from the identity (python mirror: %d / %d / 0x%04X)" % (dg, df, ditem),
          mf is not None and (int(mf.group(1)), int(mf.group(2)), int(mf.group(3), 16)) == (dg, df, ditem))
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
    hrec1 = bytes(f1.rec_pushes[-1]["data"])
    fresh_record_checks(check, "R1 (G1) reconnect", hrec1, GUEST, oracle, resident_names=res_names)
    check("R1 (G1) the host's first-contact record is what the REAL client uploaded and is restored to a returning session: the derived appearance bytes (gender %d, face %d, shirt 0x%04X) are in it" % (dg, df, ditem),
          hrec1[0x14] == dg and hrec1[0x15] == df and struct.unpack(">H", hrec1[L.REC_OFF_CLOTH_ITEM:L.REC_OFF_CLOTH_ITEM + 2])[0] == ditem and w1 == 0)
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
    # ---------------- R5: a second real guest, GENDER,FACE given ----------------
    spec2 = "GUESTS,HOMETWN,0x4A08,0x5B01,1,5"
    g2 = L.GuestIdentity(b"GUESTS  ", 0x4A08, b"HOMETWN ", 0x5B01)
    off_log = len(host.log_text())
    cl5 = rig.start_client("r5", spec=spec2)
    m5 = cl5.wait_for_log(r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=(\d+) kind=FULL", 120.0)
    ct5 = cl5.log_text()
    check("R5 (G1) client log: FRESH record with the GIVEN gender 1 / face 5 (not derived)", re.search(r"FRESH guest record \(not a copy of any resident\): gender=1 face=5 shirt=0x24(0[89A-Fa-f])", ct5) is not None)
    check("R5 host log: the second real guest is bound to GUEST slot 1 (first contact: token minted), no violation in the client log",
          re.search(r"peer \d+ bound to GUEST slot 1 \(first contact: token minted;", host.log_text()[off_log:]) is not None and m5 is not None
          and not re.search(r"violation \d/\d|BAD[_ ]DIGEST|ADOPT_FAILED|host rejected the connection", ct5))
    cl5.stop()
    L.pump_sleep(7.0)
    try:
        e5 = [e for e in TG.parse_guests(guests)["e"] if e["present"] and e["pid"] == L.guest_pid_be(g2)]
        tok5 = e5[0]["token"]
    except (AssertionError, OSError, IndexError) as exc:
        check("R5 guests.dat holds the second guest (%s)" % exc, False)
        tok5 = None
    if tok5 is not None:
        f5 = fake_guest_as(rig, "F5", g2, tok5)
        hrec5 = bytes(f5.rec_pushes[-1]["data"])
        fresh_record_checks(check, "R5", hrec5, g2, oracle, gender=1, face=5, resident_names=res_names)
        check("R5 the two real guests are independent characters: different PersonalID / name; guest 2 has the GIVEN gender 1 / face 5 and a girl's shirt, guest 1 has its derived values",
              hrec5[:20] != hrec1[:20] and hrec5[:8] != hrec1[:8] and hrec5[0x14:0x16] == bytes([1, 5]) and 0x2408 <= struct.unpack(">H", hrec5[L.REC_OFF_CLOTH_ITEM:L.REC_OFF_CLOTH_ITEM + 2])[0] < 0x2410
              and hrec1[0x14:0x16] == bytes([dg, df]))
        release(f5)
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
        L.discard_fixture_guest_mp(L.GAME_BIN_DIR)   # G6.0: the auto at-exit snapshot holds the pre-launch guest.ini: drop save/mp there too
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
