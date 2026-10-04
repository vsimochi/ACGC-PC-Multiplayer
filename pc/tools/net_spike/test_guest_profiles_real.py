#!/usr/bin/env python3
"""test_guest_profiles_real.py - GUEST PROFILES: THREE REAL guest clients run from ONE shared PC directory (ONE cwd, ONE save/mp folder) against ONE REAL host.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation, no visual verification):
  host        = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`                      cwd = the disposable pc\\build64\\bin_fixture4
  alice       = `AnimalCrossing.exe --connect 127.0.0.1:<port> --guest-profile alice`          cwd = bin_fixture4_gprof  (SHARED by all three clients)
  bob         = `AnimalCrossing.exe --connect 127.0.0.1:<port> --guest-profile Bob`            cwd = bin_fixture4_gprof  ('Bob' = case folded to guest_bob.ini)
  default     = `AnimalCrossing.exe --connect 127.0.0.1:<port> --guest`                        cwd = bin_fixture4_gprof  (save/mp/guest.ini + guest_token.dat, as before)
bin_fixture4_gprof is a DISPOSABLE per-run copy of the fixture dir (exe, dll, shaders, ini files, a COPY of the host-town save, rom junction); NO profile / token file exists
in it at the start: every profile is CREATED by the client on first use. The test removes it at the end. Ports 12100+. At most 3 client processes at any time (+ the host).
What this proves is STATE / BINDING / NETWORK behaviour only.

  A  alice and bob start from the SAME cwd: the profile files save/mp/guest_alice.ini and guest_bob.ini are created (the default guest.ini is NOT), with different ids and
     different display names; both join as DIFFERENT guests (host guest slots 0 and 1, two transport peers, first contact each), each with its OWN token file
     (guest_token_alice.dat / guest_token_bob.dat; no guest_token.dat), the tokens equal the two host guests.dat entries, which hold different keys / names / records
  B  alice's process is killed and restarted from the same cwd: the SAME guest (slot 0, KNOWN, token verified, no first contact, no new entry: still two guests.dat entries),
     alice's profile file byte-identical; bob stayed connected and unaffected the whole time
  C  a plain `--guest` client (default profile, the same cwd) is the THIRD guest: slot 2, guest.ini created with the default name 'Guest', guest_token.dat holds its own token;
     the files of alice and bob are untouched; three live puppet slots
  D  HOST restart: all clients stopped, a new host on the same save dir; all three reconnect to their OWN records: slots 0 / 1 / 2, KNOWN, tokens verified, no first contact,
     three guests.dat entries, every profile / token file byte-identical, records never mixed
Usage: python test_guest_profiles_real.py [--port 12100]
"""
import argparse
import os
import re
import shutil
import sys
import time

import net_spike_lib as L
import test_guest_protocol as TG
import test_guest_g3_real as G3
import test_guest_g4_real as G4
import test_guest_real_client as TGR

HERE = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR_REL = "save"
ADOPT_RX = G3.ADOPT_RX
BAD_RX = G3.BAD_RX


def parse_ini(path):
    d = {}
    with open(path, "r", encoding="ascii") as f:
        for ln in f.read().splitlines():
            ln = ln.strip()
            if not ln or ln[0] in "#;[" or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def pid_be_of_ini(path):
    d = parse_ini(path)
    ident = L.GuestIdentity(d["name"].encode().ljust(8), int(d["player_id"], 0), d["home_town"].encode().ljust(8), int(d["land_id"], 0))
    return L.guest_pid_be(ident), d


class Env:
    def __init__(self, port, results):
        self.port, self.results = port, results
        self.check = lambda d, c: L.check(d, bool(c), results)
        self.host = None
        self.hn = 0
        self.save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
        self.cdir = None

    def start_host(self):
        self.hn += 1
        for i in range(3):
            h = L.HostProcess(port=self.port, extra_args=["--bootstrap-resident", "0"], log_path=os.path.join(HERE, "guest_profiles_real_host%d_try%d.log" % (self.hn, i)),
                              bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
                self.host = h
                return True
            h.stop()
            time.sleep(2.0)
        return False

    def client(self, args, tag):
        return L.ClientProcess("127.0.0.1:%d" % self.port, extra_args=list(args), log_path=os.path.join(HERE, "guest_profiles_real_%s.log" % tag), bin_dir=self.cdir, label=tag).start()


def read(path):
    with open(path, "rb") as f:
        return f.read()


def run(args, results, env):
    check = env.check
    res_names = [bytes(L.resident_player(i).player_name).rstrip(b"\x00 ").lower() for i in range(4)]
    check("fixture: all FOUR residents exist; the test guest names (alice, bob, Guest) are not resident names", len(L.read_test_save_residents()) == 4
          and all(n not in res_names for n in (b"alice", b"bob", b"guest")))
    base_priv, base_home = G3.gci_regions(env.save_dir)
    cdir = os.path.join(os.path.dirname(os.path.normpath(L.GAME_BIN_DIR)), "bin_fixture4_gprof")
    G4.make_client_dir(L.GAME_BIN_DIR, cdir)
    env.cdir = cdir
    mp = os.path.join(cdir, SAVE_DIR_REL, "mp")
    files = {k: os.path.join(mp, v) for k, v in (("ini_a", "guest_alice.ini"), ("ini_b", "guest_bob.ini"), ("ini_d", "guest.ini"), ("tok_a", "guest_token_alice.dat"),
                                                  ("tok_b", "guest_token_bob.dat"), ("tok_d", "guest_token.dat"))}
    check("start: the shared client dir has NO profile and NO token file (all are created by the clients)", not os.path.exists(mp) and not any(os.path.exists(p) for p in files.values()))
    if not env.start_host():
        check("host reached genuine field-ready state", False)
        return
    check("host reached genuine field-ready state", True)
    host = env.host
    town = L.resolve_host_town("127.0.0.1", args.port)

    def regions_ok(tag):
        p, h = G3.gci_regions(env.save_dir)
        check("%s host GCI: private_data[2..3] and homes[1..3] byte-identical to the baseline (guests allocate no resident / house)" % tag, p[2:] == base_priv[2:] and h[1:] == base_home[1:])

    # ---------------- A: alice + bob from the same cwd ----------------
    off0 = len(host.log_text())
    cA = env.client(["--guest-profile", "alice"], "a1")
    mA = cA.wait_for_log(ADOPT_RX, 150.0)
    cB = env.client(["--guest-profile", "Bob"], "b1")
    mB = cB.wait_for_log(ADOPT_RX, 150.0)
    L.pump_sleep(5.0)
    tA, tB, ht = cA.log_text(), cB.log_text(), host.log_text()[off0:]
    print("--- alice log tail ---\n" + "\n".join(tA.splitlines()[-6:]))
    print("--- bob log tail ---\n" + "\n".join(tB.splitlines()[-6:]))
    check("A both REAL guests (profiles alice and Bob, ONE cwd) arrived and ADOPTED their record", mA is not None and mB is not None)
    check("A no violation / refusal in either log", not re.search(BAD_RX, tA) and not re.search(BAD_RX, tB))
    check("A the client logs name the SELECTED profile files (guest_alice.ini / guest_bob.ini: CREATED the new guest profile) and never guest.ini",
          "CREATED the new guest profile save/mp/guest_alice.ini" in tA and "CREATED the new guest profile save/mp/guest_bob.ini" in tB and "guest.ini" not in tA.replace("guest_alice.ini", "")
          and "guest.ini" not in tB.replace("guest_bob.ini", ""))
    check("A the profile files exist (guest_alice.ini, guest_bob.ini, lower case), the DEFAULT guest.ini does not (no side effect), no tmp left",
          os.path.isfile(files["ini_a"]) and os.path.isfile(files["ini_b"]) and not os.path.exists(files["ini_d"]) and not [f for f in os.listdir(mp) if f.endswith(".tmp")])
    pidA, da = pid_be_of_ini(files["ini_a"])
    pidB, db = pid_be_of_ini(files["ini_b"])
    ini_a0, ini_b0 = read(files["ini_a"]), read(files["ini_b"])
    check("A the two profiles have DIFFERENT display names (alice / Bob) and different ids; the keys differ", da["name"] == "alice" and db["name"] == "Bob" and da["home_town"] == db["home_town"] == "GuestVil"
          and (da["player_id"], da["land_id"]) != (db["player_id"], db["land_id"]) and pidA != pidB)
    ma = re.search(r"peer (\d+) bound to GUEST slot 0 \(first contact: token minted;", ht)
    mb = re.search(r"peer (\d+) bound to GUEST slot 1 \(first contact: token minted;", ht)
    check("A host log: alice -> GUEST slot 0 and Bob -> GUEST slot 1 (first contact each), two different transport peers", ma is not None and mb is not None and ma.group(1) != mb.group(1))
    pA, pB = (int(m.group(1)) if m else -1 for m in (ma, mb))
    eA, pfA = G3.wait_guest_entry(pidA, lambda e: True)
    eB, pfB = G3.wait_guest_entry(pidB, lambda e: True)
    check("A host guests.dat holds exactly the two guests under their OWN keys with different tokens", eA is not None and eB is not None and sum(1 for x in pfB["e"] if x["present"]) == 2
          and eA["token"] != eB["token"] and pidA != pidB)
    try:
        tfA, tfB = TGR.parse_token_file(files["tok_a"]), TGR.parse_token_file(files["tok_b"])
    except (AssertionError, OSError) as exc:
        check("A both token files parse (%s)" % exc, False)
        tfA = tfB = []
    check("A each profile has ITS OWN token file in the shared folder: guest_token_alice.dat holds alice's token, guest_token_bob.dat Bob's (one entry each, for the host town); NO guest_token.dat",
          len(tfA) == 1 and len(tfB) == 1 and eA is not None and eB is not None and tfA[0]["token"] == eA["token"] and tfB[0]["token"] == eB["token"] and tfA[0]["pid"] == pidA
          and tfB[0]["pid"] == pidB and tfA[0]["token"] != tfB[0]["token"] and tfA[0]["land_name"] == bytes(town.land_name) and not os.path.exists(files["tok_d"]))
    tok_a0, tok_b0 = read(files["tok_a"]), read(files["tok_b"])
    bal = G3.live_remote_slots(host.log_text())
    check("A host: TWO open puppet slots, one per peer: %s" % bal, sorted(p for p, v in bal.items() if v > 0) == sorted([pA, pB]) and all(v == 1 for v in bal.values() if v > 0))
    regions_ok("A")
    L.pump_sleep(6.0)
    check("A both processes still running after the idle period", cA.alive() and cB.alive())

    # ---------------- B: kill + restart alice, bob unaffected ----------------
    off_b = len(host.log_text())
    rcode = cA.stop()
    check("B alice's process was killed while running (rc %s)" % rcode, rcode is None)
    mdis = host.wait_for_log(r"\[NET\] host: peer %d disconnected" % pA, 60.0, since_offset=off_b)
    check("B host: alice's peer is gone", mdis is not None)
    L.pump_sleep(3.0)
    eA1, _ = G3.wait_guest_entry(pidA, lambda e: e["rev"] >= 1, 25.0)
    recA = eA1["record"] if eA1 else b""
    off_c = len(host.log_text())
    cA2 = env.client(["--guest-profile", "alice"], "a2")
    mA2 = cA2.wait_for_log(ADOPT_RX, 150.0)
    L.pump_sleep(3.0)
    tA2, hc = cA2.log_text(), host.log_text()[off_c:]
    mk = re.search(r"peer (\d+) bound to GUEST slot 0 \(known guest: token verified;", hc)
    check("B host log: alice returns as the SAME guest (KNOWN, slot 0, token verified); no first contact, no new slot, nothing refused", mk is not None and "first contact" not in hc
          and "bound to GUEST slot 2" not in hc and "bound to GUEST slot 1" not in hc)
    check("B alice's log: LOADED (not created) guest_alice.ini, token held and verified (slot 0), the record adopted with no MIGRATE, nothing refused",
          mA2 is not None and "loaded guest profile save/mp/guest_alice.ini" in tA2 and "CREATED the" not in tA2 and "sent IDENTITY_EXT (token held)" in tA2
          and "guest token verified by the host (guest slot 0)" in tA2 and "MIGRATE upload" not in tA2 and not re.search(BAD_RX, tA2))
    eA2, pf2 = G3.wait_guest_entry(pidA, lambda e: True)
    check("B still exactly TWO guests.dat entries (no new guest), alice's token unchanged, bob's entry untouched", eA2 is not None and sum(1 for x in pf2["e"] if x["present"]) == 2
          and eA2["token"] == eA["token"] and G3.wait_guest_entry(pidB, lambda e: True)[0]["token"] == eB["token"])
    check("B alice's profile and token files are byte-identical to before (a returning guest rewrites nothing); bob's files too", read(files["ini_a"]) == ini_a0 and read(files["tok_a"]) == tok_a0
          and read(files["ini_b"]) == ini_b0 and read(files["tok_b"]) == tok_b0)
    pA2 = int(mk.group(1)) if mk else -1
    bal = G3.live_remote_slots(host.log_text())
    check("B bob was connected the whole time: process alive, no disconnect of his peer, his puppet slot open; alice's new peer has one: %s" % bal,
          cB.alive() and not re.search(r"\[NET\] host: peer %d disconnected" % pB, host.log_text()[off0:]) and bal.get(pB, 0) == 1 and bal.get(pA2, 0) == 1 and not re.search(BAD_RX, cB.log_text()))

    # ---------------- C: a plain --guest client is the third guest ----------------
    off_d = len(host.log_text())
    cD = env.client(["--guest"], "d1")
    mD = cD.wait_for_log(ADOPT_RX, 150.0)
    L.pump_sleep(4.0)
    tD, hd = cD.log_text(), host.log_text()[off_d:]
    check("C the plain --guest client (default profile, same cwd) ADOPTED its record; no violation", mD is not None and not re.search(BAD_RX, tD))
    check("C its log: CREATED the default guest profile save/mp/guest.ini (not a named one)", "CREATED the default guest profile save/mp/guest.ini" in tD)
    md = re.search(r"peer (\d+) bound to GUEST slot 2 \(first contact: token minted;", hd)
    check("C host log: the plain --guest client is guest slot 2 (first contact), a third transport peer; nobody else was re-bound", md is not None and int(md.group(1)) not in (pA2, pB)
          and "bound to GUEST slot 0" not in hd and "bound to GUEST slot 1" not in hd)
    pidD, dd = pid_be_of_ini(files["ini_d"]) if os.path.isfile(files["ini_d"]) else (b"", {})
    check("C guest.ini exists with the legacy default name 'Guest' / home 'GuestVil'; its identity differs from alice's and Bob's", dd.get("name") == "Guest" and dd.get("home_town") == "GuestVil"
          and pidD not in (pidA, pidB) and pidD != b"")
    eD, pf3 = G3.wait_guest_entry(pidD, lambda e: True)
    try:
        tfD = TGR.parse_token_file(files["tok_d"])
    except (AssertionError, OSError) as exc:
        check("C guest_token.dat parses (%s)" % exc, False)
        tfD = []
    check("C THREE guests.dat entries; the default client's token is in guest_token.dat (one entry) and differs from alice's / bob's", eD is not None and sum(1 for x in pf3["e"] if x["present"]) == 3
          and len(tfD) == 1 and tfD[0]["token"] == eD["token"] and eD["token"] not in (eA["token"], eB["token"]) and tfD[0]["pid"] == pidD)
    check("C the profile / token files of alice and bob are untouched by the third client", read(files["ini_a"]) == ini_a0 and read(files["tok_a"]) == tok_a0 and read(files["ini_b"]) == ini_b0
          and read(files["tok_b"]) == tok_b0)
    bal = G3.live_remote_slots(host.log_text())
    pD = int(md.group(1)) if md else -1
    check("C host: THREE live puppet slots (alice's, bob's, default's): %s" % bal, sorted(p for p, v in bal.items() if v > 0) == sorted([pA2, pB, pD]) and all(v == 1 for v in bal.values() if v > 0))
    ini_d0, tok_d0 = read(files["ini_d"]), read(files["tok_d"])
    check("C all three client processes alive", cA2.alive() and cB.alive() and cD.alive())
    cA2.stop()
    cB.stop()
    cD.stop()
    L.pump_sleep(8.0)
    eA3, _ = G3.wait_guest_entry(pidA, lambda e: True)
    eB3, _ = G3.wait_guest_entry(pidB, lambda e: True)
    eD3, _ = G3.wait_guest_entry(pidD, lambda e: True)
    check("C the three stored records are three DIFFERENT characters (own PersonalID each, empty pockets)", eA3 and eB3 and eD3 and eA3["record"][:20] == pidA and eB3["record"][:20] == pidB
          and eD3["record"][:20] == pidD and len({bytes(eA3["record"]), bytes(eB3["record"]), bytes(eD3["record"])}) == 3
          and all(L.record_inventory(x["record"]) == ((0,) * 15, 0, 0) for x in (eA3, eB3, eD3)))
    recs = {k: bytes(x["record"]) for k, x in (("a", eA3), ("b", eB3), ("d", eD3))}
    regions_ok("C")

    # ---------------- D: host restart, everybody reconnects to their own record ----------------
    rcode = host.stop()
    check("D the first host process stopped (rc before stop: %s)" % rcode, rcode is None)
    time.sleep(2.0)
    if not env.start_host():
        check("D the second host process reached field-ready state", False)
        return
    host = env.host
    check("D the second host process reached field-ready state", True)
    off_e = len(host.log_text())
    c1 = env.client(["--guest-profile", "alice"], "a3")
    m1 = c1.wait_for_log(ADOPT_RX, 150.0)
    c2 = env.client(["--guest-profile", "bob"], "b3")     # 'bob' (lower case): the SAME profile as 'Bob'
    m2 = c2.wait_for_log(ADOPT_RX, 150.0)
    c3 = env.client(["--guest"], "d3")
    m3 = c3.wait_for_log(ADOPT_RX, 150.0)
    L.pump_sleep(4.0)
    he = host.log_text()[off_e:]
    t1, t2, t3 = c1.log_text(), c2.log_text(), c3.log_text()
    check("D after the host restart: alice -> slot 0, bob (typed lower case) -> slot 1, default -> slot 2: all KNOWN, token verified, no first contact, nothing refused",
          all(re.search(r"bound to GUEST slot %d \(known guest: token verified;" % s, he) for s in (0, 1, 2)) and "first contact" not in he and "WRONG token" not in he and "WITHOUT a token" not in he)
    check("D each client verified the token with ITS OWN slot number; all adopted their stored record, no MIGRATE, no violation; no profile was re-created",
          m1 and m2 and m3 and "guest token verified by the host (guest slot 0)" in t1 and "guest token verified by the host (guest slot 1)" in t2
          and "guest token verified by the host (guest slot 2)" in t3 and "MIGRATE upload" not in t1 + t2 + t3 and not re.search(BAD_RX, t1 + t2 + t3) and "CREATED the" not in t1 + t2 + t3
          and "loaded guest profile save/mp/guest_bob.ini" in t2)
    c1.stop()
    c2.stop()
    c3.stop()
    L.pump_sleep(8.0)
    eA4, pf4 = G3.wait_guest_entry(pidA, lambda e: True)
    eB4, _ = G3.wait_guest_entry(pidB, lambda e: True)
    eD4, _ = G3.wait_guest_entry(pidD, lambda e: True)
    check("D after the restart still exactly THREE entries, every record byte-identical to the pre-restart one (never mixed), tokens unchanged",
          eA4 and eB4 and eD4 and sum(1 for x in pf4["e"] if x["present"]) == 3 and bytes(eA4["record"]) == recs["a"] and bytes(eB4["record"]) == recs["b"] and bytes(eD4["record"]) == recs["d"]
          and eA4["token"] == eA["token"] and eB4["token"] == eB["token"] and eD4["token"] == eD["token"])
    check("D every profile and token file of the shared folder is byte-identical to its first value, and the folder holds exactly these 6 files",
          read(files["ini_a"]) == ini_a0 and read(files["ini_b"]) == ini_b0 and read(files["ini_d"]) == ini_d0 and read(files["tok_a"]) == tok_a0 and read(files["tok_b"]) == tok_b0
          and read(files["tok_d"]) == tok_d0 and sorted(f for f in os.listdir(mp) if not f.endswith(".bak1")) == sorted(os.path.basename(p) for p in files.values()))
    regions_ok("D")
    bal_end = G3.live_remote_slots(host.log_text())
    check("end: no open remote-player slot after every client is gone: %s" % bal_end, all(v == 0 for v in bal_end.values()))
    check("the host stayed alive and logged no INTERNAL error", host.alive() and "*** INTERNAL" not in host.log_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12100)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)) == "bin_fixture4":
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_profiles_real_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    env = Env(args.port, results)
    import atexit
    atexit.register(lambda: env.cdir and G4.remove_client_dir(env.cdir))
    try:
        run(args, results, env)
    finally:
        if env.host is not None:
            env.host.stop()
        time.sleep(2.0)
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)
        shutil.rmtree(snap_dir, ignore_errors=True)
        L.discard_fixture_guest_mp(L.GAME_BIN_DIR)
        if env.cdir:
            G4.remove_client_dir(env.cdir)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
