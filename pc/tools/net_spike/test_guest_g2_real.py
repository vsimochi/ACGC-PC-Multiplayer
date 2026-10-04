#!/usr/bin/env python3
"""test_guest_g2_real.py - GUESTS G2: a REAL game client started with `--guest` (the persistent profile save/mp/guest.ini), REAL host: create -> disconnect -> reconnect ->
same character, and HOST RESTART -> same character.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --guest` (a NEW process every run; its cwd = the disposable fixture dir, so the profile is
           <fixture>/save/mp/guest.ini and the token <fixture>/save/mp/guest_token.dat)
Run ONLY on the disposable pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN=<absolute path>, ports 11800+). The fixture save dir is snapshotted at start and restored at the end
(guest.ini / guest_token.dat / guests.dat live in save/mp and vanish with the restore). The scripted FakeClient is used ONLY to read back the host-held record.

  Q1  a pre-written guest.ini (name / gender 1 / face 5 / home_town / fixed ids): the client logs the loaded profile, becomes a guest with EXACTLY those values, the host stores
      its record (MIGRATE APPLIED rev 1 -- i.e. the host's empty-economy predicate ACCEPTED the real client's arrival record, no 'not a fresh character' refusal), the guests.dat key
      == the profile's home PersonalID, the guest_token.dat token == guests.dat's; guest.ini is byte-for-byte unchanged (a load never rewrites it)
  Q2  the client restarts with the same guest.ini + token file: the host recognises the SAME returning guest (KNOWN, same slot 0, same token, token verified), NO MIGRATE, the
      client adopts the stored record, no second guests.dat entry, the record is byte-identical
  Q3  HOST RESTART (same fixture dir, guests.dat persisted): the client reconnects and keeps the same character (KNOWN, token verified by the NEW host process, adopts the stored
      record, same guests.dat key / token / record)
  Q4  no guest.ini yet: the client CREATES it (default name 'Guest', home 'GuestVil', fresh CSPRNG ids, gender / face derived from the identity), joins as GUEST slot 1 with exactly
      those ids (host key == the file's ids), and the file parses with an independent implementation
Usage: python test_guest_g2_real.py [--port 11800]
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
import test_guest_real_client as TGR

HERE = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR_REL = "save"
ADOPT_RX = r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=(\d+) kind=FULL"
PID, LID = 0x4C11, 0x5D22


def ini_text(name, gender, face, home, pid, lid):
    return ("# test profile\nname = %s\ngender = %d\nface = %d\nhome_town = %s\nplayer_id = 0x%04X\nland_id = %d\n" % (name, gender, face, home, pid, lid))


def parse_ini(path):
    d = {}
    with open(path, encoding="ascii") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln[0] in "#;[":
                continue
            k, _s, v = ln.partition("=")
            d[k.strip()] = v.strip()
    return d


class Env:
    def __init__(self, port, results):
        self.port, self.results = port, results
        self.check = lambda d, c: L.check(d, bool(c), results)
        self.host = None
        self.hn = 0
        self.cn = 0
        self.save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
        self.ini = os.path.join(self.save_dir, "mp", "guest.ini")
        self.tok = os.path.join(self.save_dir, "mp", "guest_token.dat")

    def start_host(self):
        self.hn += 1
        for i in range(3):
            h = L.HostProcess(port=self.port, extra_args=["--bootstrap-resident", "0"], log_path=os.path.join(HERE, "guest_g2_real_host%d_try%d.log" % (self.hn, i)),
                              bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
                self.host = h
                return True
            h.stop()
            time.sleep(2.0)
        return False

    def client(self, tag):
        self.cn += 1
        return L.ClientProcess("127.0.0.1:%d" % self.port, extra_args=["--guest"], log_path=os.path.join(HERE, "guest_g2_real_%s.log" % tag), bin_dir=L.GAME_BIN_DIR, label=tag).start()


def wait_guest_entry(pid_be, pred, timeout=15.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        try:
            pf = TG.parse_guests(TG.guests_path())
            e = [x for x in pf["e"] if x["present"] and x["pid"] == pid_be]
            if e and pred(e[0]):
                return e[0], pf
        except (AssertionError, OSError):
            pass
        time.sleep(0.4)
    return None, None


def run(args, results, env):
    check = env.check
    ip = "127.0.0.1"
    name, home = "Quill", "Hometown"
    ident = L.GuestIdentity(name.encode().ljust(8), PID, home.encode().ljust(8), LID)
    pid_be = L.guest_pid_be(ident)
    res_names = [bytes(L.resident_player(i).player_name) for i in range(4)]
    check("fixture: the test guest name '%s' is not a resident's name" % name, all(n.rstrip(b"\x00 ") != name.encode() for n in res_names))
    oracle, oracle_ok = TGR.design_oracle()
    check("fixture: identical default designs in the 4 residents (oracle for the ROM / ARAM design names)", oracle_ok)
    os.makedirs(os.path.dirname(env.ini), exist_ok=True)
    body = ini_text(name, 1, 5, home, PID, LID)
    with open(env.ini, "wb") as f:
        f.write(body.encode("ascii"))
    if not env.start_host():
        check("host reached genuine field-ready state", False)
        return
    check("host reached genuine field-ready state", True)
    host = env.host
    town = L.resolve_host_town(ip, args.port)

    # ---------------- Q1: first contact with a pre-written profile ----------------
    c1 = env.client("q1")
    m1 = c1.wait_for_log(ADOPT_RX, 150.0)
    t1 = c1.log_text()
    print("--- client Q1 log tail ---")
    print("\n".join(t1.splitlines()[-18:]))
    check("Q1 client log: the profile was LOADED (not created) from save/mp/guest.ini with the file's values",
          "[PC] --guest: loaded guest profile save/mp/guest.ini: name 'Quill', home town 'Hometown', gender 1, face 5, player id 0x4C11, land id 0x5D22" in t1)
    check("Q1 client log: the SAME arrival path ran: a FRESH record with the GIVEN gender 1 / face 5, the foreigner bound, arriving at the station",
          re.search(r"FRESH guest record \(not a copy of any resident\): gender=1 face=5 shirt=0x24(0[89A-Fa-f])", t1) is not None and "bound as a foreigner" in t1
          and "guest 'Quill   '" in t1)
    check("Q1 client log: IDENTITY_EXT sent (token not held: first contact); the token was saved; MIGRATE applied rev 1; PUSH_FULL adopted",
          "playing a guest -- sent IDENTITY_EXT (token not held (first contact))" in t1 and "guest token (slot 0) saved to save/mp/guest_token.dat" in t1
          and re.search(r"MIGRATE upload xfer 1 APPLIED by the host \(rev 1\)", t1) is not None and m1 is not None)
    ht = host.log_text()
    check("Q1 host log: bound to GUEST slot 0 (first contact: token minted) and the real client's first MIGRATE passed the empty-economy rule (no 'not a fresh character' refusal)",
          re.search(r"peer \d+ bound to GUEST slot 0 \(first contact: token minted;", ht) is not None and "first MIGRATE REFUSED" not in ht
          and re.search(r"MIGRATE xfer 1 APPLIED \(resident 4 ", ht) is not None)
    check("Q1 no violation / refusal in the client log", not re.search(r"violation \d/\d|BAD[_ ]DIGEST|ADOPT_FAILED|\*\*\* host REFUSED|host rejected the connection", t1))
    with open(env.ini, "rb") as f:
        check("Q1 guest.ini is byte-for-byte unchanged by the run (a load never rewrites; the ids are permanent)", f.read() == body.encode("ascii"))
    e0, _pf = wait_guest_entry(pid_be, lambda e: True, 10.0)
    try:
        tf = TGR.parse_token_file(env.tok)
    except (AssertionError, OSError) as exc:
        check("Q1 the token file parses (%s)" % exc, False)
        c1.stop()
        return
    check("Q1 guests.dat key == the profile's home PersonalID (name / home / ids); the client's guest_token.dat holds the SAME token for the host town",
          e0 is not None and e0["pid"] == pid_be and len(tf) == 1 and tf[0]["token"] == e0["token"] and tf[0]["pid"] == pid_be
          and tf[0]["land_name"] == bytes(town.land_name) and tf[0]["land_id"] == town.land_id)
    if e0 is None:
        c1.stop()
        return
    token = e0["token"]
    c1.stop()
    L.pump_sleep(7.0)
    rec1_entry, _ = wait_guest_entry(pid_be, lambda e: e["rev"] >= 1, 20.0)
    check("Q1 guests.dat holds the guest's record at rev >= 1 (stored on disconnect)", rec1_entry is not None)
    if rec1_entry is None:
        return
    rec1 = rec1_entry["record"]
    TGR.fresh_record_checks(check, "Q1", rec1, ident, oracle, gender=1, face=5, resident_names=res_names)
    check("Q1 the stored record is the guest's: name 'Quill', gender 1, face 5, empty pockets / wallet / bank",
          rec1[:20] == pid_be and rec1[0x14] == 1 and rec1[0x15] == 5 and L.record_inventory(rec1) == ((0,) * 15, 0, 0))

    # ---------------- Q2: restart the client with the same profile + token ----------------
    off = len(host.log_text())
    c2 = env.client("q2")
    m2 = c2.wait_for_log(ADOPT_RX, 150.0)
    t2 = c2.log_text()
    check("Q2 client log: the same profile loaded, IDENTITY_EXT carries the stored token, the host verified it (KNOWN, guest slot 0)",
          "loaded guest profile save/mp/guest.ini: name 'Quill'" in t2 and "sent IDENTITY_EXT (token held)" in t2 and "guest token verified by the host (guest slot 0)" in t2)
    check("Q2 host log: bound to GUEST slot 0 (known guest: token verified) -- the same returning guest, not a new one",
          re.search(r"bound to GUEST slot 0 \(known guest: token verified;", host.log_text()[off:]) is not None and "first contact" not in host.log_text()[off:])
    check("Q2 NO re-migration: the client adopts the stored record (no MIGRATE request / upload), nothing refused", m2 is not None and "host asks for a MIGRATE upload" not in t2
          and "MIGRATE upload" not in t2 and not re.search(r"violation \d/\d|BAD[_ ]DIGEST|ADOPT_FAILED|\*\*\* host REFUSED|host rejected the connection", t2))
    c2.stop()
    L.pump_sleep(7.0)
    e2, pf2 = wait_guest_entry(pid_be, lambda e: True, 10.0)
    check("Q2 still exactly ONE guests.dat entry, same token, same record byte for byte (a returning guest creates nothing)",
          e2 is not None and sum(1 for x in pf2["e"] if x["present"]) == 1 and e2["token"] == token and e2["record"] == rec1)
    with open(env.ini, "rb") as f:
        check("Q2 guest.ini unchanged", f.read() == body.encode("ascii"))

    # ---------------- Q3: HOST restart, same fixture dir ----------------
    rc = host.stop()
    check("Q3 the first host process stopped (exit code before stop: %s)" % rc, rc is None)
    time.sleep(2.0)
    if not env.start_host():
        check("Q3 the second host process reached field-ready state", False)
        return
    host = env.host
    check("Q3 the second host process reached field-ready state", True)
    off = len(host.log_text())
    c3 = env.client("q3")
    m3 = c3.wait_for_log(ADOPT_RX, 150.0)
    t3 = c3.log_text()
    ht3 = host.log_text()[off:]
    check("Q3 the restarted host recognises the guest by its persisted token: KNOWN, guest slot 0, token verified (log of the NEW host process)",
          re.search(r"bound to GUEST slot 0 \(known guest: token verified;", ht3) is not None and "first contact" not in ht3 and "WRONG token" not in ht3 and "WITHOUT a token" not in ht3)
    check("Q3 client log: same profile, token held and verified, record ADOPTED from the restarted host, no MIGRATE, no refusal",
          "sent IDENTITY_EXT (token held)" in t3 and "guest token verified by the host (guest slot 0)" in t3 and m3 is not None and "MIGRATE upload" not in t3
          and not re.search(r"violation \d/\d|BAD[_ ]DIGEST|ADOPT_FAILED|\*\*\* host REFUSED|host rejected the connection", t3))
    c3.stop()
    L.pump_sleep(7.0)
    e3, pf3 = wait_guest_entry(pid_be, lambda e: True, 10.0)
    check("Q3 after the host restart the SAME character: one entry, same key / token, record byte-identical to before the restart",
          e3 is not None and sum(1 for x in pf3["e"] if x["present"]) == 1 and e3["pid"] == pid_be and e3["token"] == token and e3["record"] == rec1)
    # read-back through a scripted guest session: the NEW host process pushes exactly that record
    f3 = TGR.fake_guest_as(type("R", (), {"port": args.port})(), "F3", ident, token)
    check("Q3 the restarted host pushes the stored record to a scripted guest session (rev >= 1, class GUEST, byte-identical)",
          f3.rec_pushes and bytes(f3.rec_pushes[-1]["data"]) == rec1 and f3.rec_pushes[-1]["rsv"] == 1 and f3.rec_pushes[-1]["rev"] >= 1)
    TGR.release(f3)

    # ---------------- Q4: no guest.ini: the client creates it ----------------
    os.remove(env.ini)
    off = len(host.log_text())
    c4 = env.client("q4")
    t_end = time.monotonic() + 120.0
    while not os.path.isfile(env.ini) and time.monotonic() < t_end:
        time.sleep(0.5)
    check("Q4 the client CREATED save/mp/guest.ini on first use", os.path.isfile(env.ini))
    m4 = c4.wait_for_log(ADOPT_RX, 150.0)
    t4 = c4.log_text()
    d = {}
    try:
        d = parse_ini(env.ini)
    except OSError:
        pass
    ok_keys = sorted(d) == sorted(["name", "gender", "face", "home_town", "player_id", "land_id"])
    ok_vals = False
    new_pid = None
    if ok_keys:
        try:
            pid2 = int(d["player_id"], 0)
            lid2 = int(d["land_id"], 0)
            g2 = L.GuestIdentity(d["name"].encode().ljust(8), pid2, d["home_town"].encode().ljust(8), lid2)
            new_pid = L.guest_pid_be(g2)
            dg, df, _it, _ix = L.guest_fresh_derive(new_pid)
            ok_vals = (d["name"] == "Guest" and d["home_town"] == "GuestVil" and 1 <= pid2 <= 0xFFFE and 1 <= lid2 <= 0xFFFE and (pid2, lid2) != (PID, LID)
                       and int(d["gender"]) == dg and int(d["face"]) == df)
        except (ValueError, KeyError):
            pass
    check("Q4 the created file parses with an independent implementation: name 'Guest', home 'GuestVil', fresh ids in 1..0xFFFE (different from the old ones), gender / face = the identity-derived values", ok_keys and ok_vals)
    check("Q4 client log: 'CREATED the default guest profile', then the SAME arrival path and a MIGRATE applied (rev 1)", "[PC] --guest: CREATED the default guest profile save/mp/guest.ini: name 'Guest'" in t4
          and "bound as a foreigner" in t4 and m4 is not None)
    e4, pf4 = wait_guest_entry(new_pid, lambda e: True, 15.0) if new_pid else (None, None)
    check("Q4 the host created a SECOND guest (slot 1) keyed by exactly the created file's ids; the first guest's entry is untouched",
          e4 is not None and re.search(r"bound to GUEST slot 1 \(first contact: token minted;", host.log_text()[off:]) is not None
          and any(x["present"] and x["pid"] == pid_be and x["token"] == token for x in pf4["e"]) and "first MIGRATE REFUSED" not in host.log_text()[off:])
    c4.stop()
    check("the host stayed alive and logged no INTERNAL error", host.alive() and "*** INTERNAL" not in host.log_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11800)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_g2_real_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    env = Env(args.port, results)
    try:
        run(args, results, env)
    finally:
        if env.host is not None:
            env.host.stop()
        time.sleep(2.0)   # let every stopped client / host release its files before the save dir is restored
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)
        shutil.rmtree(snap_dir, ignore_errors=True)
        L.discard_fixture_guest_mp(L.GAME_BIN_DIR)   # G6.0: the auto at-exit snapshot holds the pre-launch guest.ini: drop save/mp there too
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
