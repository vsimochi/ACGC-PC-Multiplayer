#!/usr/bin/env python3
"""test_guest_g4_real.py - GUESTS G4: TWO REAL guest clients (+ ONE REAL resident client) connected to ONE REAL host at the same time.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation, no visual verification):
  host     = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`                   cwd = the disposable pc\\build64\\bin_fixture4
  resident = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident 1`      cwd = bin_fixture4 (the established practice of the real resident tests)
  guest A  = `AnimalCrossing.exe --connect 127.0.0.1:<port> --guest`                     cwd = bin_fixture4_g4a (own guest.ini + guest_token.dat)
  guest B  = `AnimalCrossing.exe --connect 127.0.0.1:<port> --guest`                     cwd = bin_fixture4_g4b (own guest.ini + guest_token.dat)
bin_fixture4_g4a / _g4b are DISPOSABLE per-run copies of the fixture dir (exe, dll, shaders, ini files, a COPY of the host-town save; the rom dir is a junction to the
fixture's rom link); the test removes them at the end. Ports 12000+. At most 3 client processes at any time (+ the host).
What this proves is STATE / BINDING / NETWORK behaviour only.

  A  both guests + the resident connect: two DIFFERENT guest slots (0 and 1) on two different transport peers, each with its OWN token (the two guest_token.dat files and
     the two guests.dat entries differ), independent stored records (own PersonalID, own gender / face), the resident bound to resident 1; three open puppet slots on the
     host (one per peer); the host GCI: private_data[2..3] and homes[1..3] byte-identical to the baseline (a guest allocates no resident / house; resident 1's own record
     is reported only)
  B  guest A's process is killed (TerminateProcess): the host sees ITS peer disconnect and destroys ITS puppet slot ONLY; guest B's session, puppet slot and the
     resident's stay (B / resident processes alive, no disconnect of their peers, no violation); B's stored record is not touched
  C  guest A restarts (same cwd / guest.ini / token): the SAME guest slot 0, KNOWN, token verified, no MIGRATE, ONE guests.dat entry per guest (2 total), A's record
     byte-identical to before; B still connected throughout
  D  HOST restart: every client is stopped, a NEW host process on the same save dir; both guests restart and reconnect: each to ITS OWN slot / record (A slot 0, B slot 1,
     KNOWN, tokens verified, no first contact, records byte-identical to the pre-restart ones, guests.dat still two entries)
Usage: python test_guest_g4_real.py [--port 12000]
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

import net_spike_lib as L
import test_guest_protocol as TG
import test_guest_g3_real as G3
import test_guest_real_client as TGR

HERE = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR_REL = "save"
ADOPT_RX = G3.ADOPT_RX
BAD_RX = G3.BAD_RX
A_ID = ("Quill", 1, 5, "Hometwn", 0x4D21, 0x5E31)
B_ID = ("Fern", 0, 2, "Otherhm", 0x4D22, 0x5E32)


def ident_of(t):
    return L.GuestIdentity(t[0].encode().ljust(8), t[4], t[3].encode().ljust(8), t[5])


def make_client_dir(src, dst):
    """A disposable copy of the fixture dir to act as a client cwd. The rom entry becomes a JUNCTION to whatever the fixture's rom resolves to."""
    if os.path.isdir(dst):
        remove_client_dir(dst)
    os.makedirs(dst)
    for f in ("AnimalCrossing.exe", "SDL2.dll", "keybindings.ini", "settings.ini"):
        shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    shutil.copytree(os.path.join(src, "shaders"), os.path.join(dst, "shaders"))
    shutil.copytree(os.path.join(src, SAVE_DIR_REL), os.path.join(dst, SAVE_DIR_REL))
    shutil.rmtree(os.path.join(dst, SAVE_DIR_REL, "mp"), ignore_errors=True)
    rom_target = os.path.realpath(os.path.join(src, "rom"))
    subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(dst, "rom"), rom_target], check=True, capture_output=True)


def remove_client_dir(dst):
    rom = os.path.join(dst, "rom")
    if os.path.lexists(rom):
        try:
            os.rmdir(rom)            # a junction / symlink to a directory: removes ONLY the link, never the target
        except OSError:
            try:
                os.unlink(rom)
            except OSError:
                pass
    shutil.rmtree(dst, ignore_errors=True)


class Env:
    def __init__(self, port, results):
        self.port, self.results = port, results
        self.check = lambda d, c: L.check(d, bool(c), results)
        self.host = None
        self.hn = 0
        self.save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
        self.dirs = {}

    def start_host(self):
        self.hn += 1
        for i in range(3):
            h = L.HostProcess(port=self.port, extra_args=["--bootstrap-resident", "0"], log_path=os.path.join(HERE, "guest_g4_real_host%d_try%d.log" % (self.hn, i)),
                              bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
                self.host = h
                return True
            h.stop()
            time.sleep(2.0)
        return False

    def guest(self, which, tag):
        return L.ClientProcess("127.0.0.1:%d" % self.port, extra_args=["--guest"], log_path=os.path.join(HERE, "guest_g4_real_%s.log" % tag), bin_dir=self.dirs[which],
                               label=tag).start()

    def resident(self, tag, slot=1):
        return L.ClientProcess("127.0.0.1:%d" % self.port, extra_args=["--bootstrap-resident", str(slot)], log_path=os.path.join(HERE, "guest_g4_real_%s.log" % tag),
                               bin_dir=L.GAME_BIN_DIR, label=tag).start()


def entry_of(pid_be, pred=lambda e: True, timeout=15.0):
    return G3.wait_guest_entry(pid_be, pred, timeout)


def run(args, results, env):
    check = env.check
    idA, idB = ident_of(A_ID), ident_of(B_ID)
    pidA, pidB = L.guest_pid_be(idA), L.guest_pid_be(idB)
    res_names = [bytes(L.resident_player(i).player_name) for i in range(4)]
    check("fixture: all FOUR residents exist; the test guest names are not resident names", len(L.read_test_save_residents()) == 4
          and all(n.rstrip(b"\x00 ") not in (A_ID[0].encode(), B_ID[0].encode()) for n in res_names))
    base_priv, base_home = G3.gci_regions(env.save_dir)
    for which, tup in (("a", A_ID), ("b", B_ID)):
        d = os.path.join(os.path.dirname(os.path.normpath(L.GAME_BIN_DIR)), "bin_fixture4_g4%s" % which)
        make_client_dir(L.GAME_BIN_DIR, d)
        env.dirs[which] = d
        os.makedirs(os.path.join(d, SAVE_DIR_REL, "mp"), exist_ok=True)
        with open(os.path.join(d, SAVE_DIR_REL, "mp", "guest.ini"), "wb") as f:
            f.write(G3.ini_text(tup[0], tup[1], tup[2], tup[3], tup[4], tup[5]).encode("ascii"))
    tokA_file = os.path.join(env.dirs["a"], SAVE_DIR_REL, "mp", "guest_token.dat")
    tokB_file = os.path.join(env.dirs["b"], SAVE_DIR_REL, "mp", "guest_token.dat")
    if not env.start_host():
        check("host reached genuine field-ready state", False)
        return
    check("host reached genuine field-ready state", True)
    host = env.host
    town = L.resolve_host_town("127.0.0.1", args.port)

    def same_regions(tag):
        p, h = G3.gci_regions(env.save_dir)
        ndiff = lambda a, b: sum(1 for x, y in zip(a, b) if x != y)
        check("%s host GCI: private_data[2..3] byte-identical to the baseline (the guests allocated / touched no resident)" % tag, p[2:] == base_priv[2:])
        check("%s host GCI: homes[1..3] byte-identical to the baseline (no house allocated or aliased for a guest)" % tag, h[1:] == base_home[1:])
        print("   info %s: private_data[1] (the resident client's) differs by %d bytes, private_data[0] / homes[0] (the host's own) by %d / %d bytes from the baseline"
              % (tag, ndiff(p[1], base_priv[1]), ndiff(p[0], base_priv[0]), ndiff(h[0], base_home[0])))

    # ---------------- A: resident + two guests at once ----------------
    off0 = len(host.log_text())
    rc_ = env.resident("res1")
    ok_res = rc_.wait_for_log(L.ClientProcess.CLIENT_SNAPSHOT_APPLIED_RX, 150.0) is not None
    check("A the REAL resident client (resident 1) reached the synced world", ok_res)
    cA = env.guest("a", "ga1")
    mA = cA.wait_for_log(ADOPT_RX, 150.0)
    cB = env.guest("b", "gb1")
    mB = cB.wait_for_log(ADOPT_RX, 150.0)
    L.pump_sleep(5.0)
    tA, tB, ht = cA.log_text(), cB.log_text(), host.log_text()[off0:]
    print("--- guest A log tail ---\n" + "\n".join(tA.splitlines()[-8:]))
    print("--- guest B log tail ---\n" + "\n".join(tB.splitlines()[-8:]))
    check("A both REAL guests arrived and ADOPTED their record (reached SYNCED)", mA is not None and mB is not None)
    check("A no violation / refusal in either guest log, nor in the resident's", not re.search(BAD_RX, tA) and not re.search(BAD_RX, tB) and not re.search(BAD_RX, rc_.log_text()))
    ma = re.search(r"peer (\d+) bound to GUEST slot 0 \(first contact: token minted;", ht)
    mb = re.search(r"peer (\d+) bound to GUEST slot 1 \(first contact: token minted;", ht)
    mr = re.search(r"peer (\d+) bound to resident 1 \(host-derived\)", ht)
    check("A host log: guest A -> GUEST slot 0, guest B -> GUEST slot 1 (first contact each), the resident -> resident 1; three different transport peers",
          ma is not None and mb is not None and mr is not None and len({ma.group(1), mb.group(1), mr.group(1)}) == 3)
    pA, pB, pR = (int(m.group(1)) if m else -1 for m in (ma, mb, mr))
    check("A host log: the first MIGRATEs of guest slot 0 / 1 were applied as record slots 4 / 5 (one each) and passed the empty-economy rule; nothing refused",
          len(re.findall(r"MIGRATE xfer 1 APPLIED \(resident 4 ", ht)) == 1 and len(re.findall(r"MIGRATE xfer 1 APPLIED \(resident 5 ", ht)) == 1 and "first MIGRATE REFUSED" not in ht)
    eA, pfA = entry_of(pidA)
    eB, pfB = entry_of(pidB)
    check("A guests.dat holds exactly the two guests, each under ITS OWN key, with different tokens", eA is not None and eB is not None and sum(1 for x in pfB["e"] if x["present"]) == 2
          and eA["token"] != eB["token"] and pidA != pidB)
    try:
        tfA, tfB = TGR.parse_token_file(tokA_file), TGR.parse_token_file(tokB_file)
    except (AssertionError, OSError) as exc:
        check("A both guest_token.dat files parse (%s)" % exc, False)
        tfA = tfB = []
    check("A each client stored ITS OWN token for the host town: guest A's file holds A's token (slot key A), B's file holds B's, and they differ",
          len(tfA) == 1 and len(tfB) == 1 and eA is not None and eB is not None and tfA[0]["token"] == eA["token"] and tfB[0]["token"] == eB["token"] and tfA[0]["pid"] == pidA
          and tfB[0]["pid"] == pidB and tfA[0]["token"] != tfB[0]["token"] and tfA[0]["land_name"] == bytes(town.land_name))
    bal = G3.live_remote_slots(host.log_text())
    check("A host: THREE open puppet slots, one per peer (A %d, B %d, resident %d): %s" % (pA, pB, pR, bal), sorted(p for p, v in bal.items() if v > 0) == sorted([pA, pB, pR])
          and all(v == 1 for v in bal.values() if v > 0))
    same_regions("A (all connected)")
    L.pump_sleep(8.0)           # the host's disconnect-triggered early save writes the guest records; also lets both guests idle in the town together
    check("A all three client processes are still running after the idle period", cA.alive() and cB.alive() and rc_.alive())

    # ---------------- B: kill guest A abruptly ----------------
    off_b = len(host.log_text())
    rcode = cA.stop()
    check("B guest A's process was killed while running (it had not exited on its own: %s)" % rcode, rcode is None)
    mdis = host.wait_for_log(r"\[NET\] host: peer %d disconnected" % pA, 60.0, since_offset=off_b)
    mdes = host.wait_for_log(r"\[NET\]\[REMOTE\] player %d disconnected -- destroying remote-player actor" % pA, 5.0, since_offset=off_b)
    check("B host: guest A's peer is gone and ITS puppet slot was destroyed", mdis is not None and mdes is not None)
    L.pump_sleep(4.0)
    hb = host.log_text()[off_b:]
    bal = G3.live_remote_slots(host.log_text())
    check("B host: guest B's and the resident's puppet slots are STILL open, A's is closed: %s" % bal, bal.get(pB, 0) == 1 and bal.get(pR, 0) == 1 and bal.get(pA, 0) == 0)
    check("B host log: no disconnect of guest B's or the resident's peer, no refusal, B / resident processes alive and not shut down",
          not re.search(r"\[NET\] host: peer (%d|%d) disconnected" % (pB, pR), hb) and cB.alive() and rc_.alive() and not re.search(BAD_RX, cB.log_text()))
    eA1, _ = entry_of(pidA, lambda e: e["rev"] >= 1, 25.0)
    eB1, pf1 = entry_of(pidB, lambda e: True)
    check("B guests.dat: A stored at rev >= 1 (host stored it on the abrupt disconnect); B's entry is unchanged by A's death (same token / key)",
          eA1 is not None and eB1 is not None and eB1["token"] == eB["token"] and eB1["pid"] == pidB and eA1["token"] == eA["token"])
    recA = eA1["record"]
    check("B guest A's stored record is A's OWN (Quill: gender 1, face 5, its PersonalID); B (still connected, nothing stored yet) is not touched", recA[:20] == pidA and recA[0x14] == 1
          and recA[0x15] == 5 and L.record_inventory(recA) == ((0,) * 15, 0, 0) and eB1["rev"] == eB["rev"])

    # ---------------- C: guest A restarts ----------------
    off_c = len(host.log_text())
    cA2 = env.guest("a", "ga2")
    mA2 = cA2.wait_for_log(ADOPT_RX, 150.0)
    L.pump_sleep(3.0)
    tA2, hc = cA2.log_text(), host.log_text()[off_c:]
    mk = re.search(r"peer (\d+) bound to GUEST slot 0 \(known guest: token verified;", hc)
    check("C host log: guest A returns as the SAME guest (KNOWN, slot 0, token verified); no first contact, no new slot, nothing refused", mk is not None and "first contact" not in hc
          and "bound to GUEST slot 2" not in hc and "bound to GUEST slot 1" not in hc)
    check("C client A log: token held and verified (guest slot 0), the record ADOPTED with no MIGRATE; nothing refused", mA2 is not None and "sent IDENTITY_EXT (token held)" in tA2
          and "guest token verified by the host (guest slot 0)" in tA2 and "MIGRATE upload" not in tA2 and not re.search(BAD_RX, tA2))
    bal = G3.live_remote_slots(host.log_text())
    pA2 = int(mk.group(1)) if mk else -1
    check("C host: A's new peer has its puppet slot, B's and the resident's slots are still open (three live): %s" % bal, bal.get(pA2, 0) == 1 and bal.get(pB, 0) == 1 and bal.get(pR, 0) == 1
          and sum(1 for v in bal.values() if v > 0) == 3)
    check("C guest B and the resident were connected the whole time (processes alive, no disconnect line for their peers since the start)",
          cB.alive() and rc_.alive() and not re.search(r"\[NET\] host: peer (%d|%d) disconnected" % (pB, pR), host.log_text()[off0:]))
    cA2.stop()
    cB.stop()
    rc_.stop()
    L.pump_sleep(8.0)
    eA2, pf2 = entry_of(pidA)
    eB2, _ = entry_of(pidB)
    recB = eB2["record"] if eB2 is not None else b""
    check("C exactly TWO guests.dat entries (no duplicate): A's record byte-identical to before its restart (returning A did not change it), tokens unchanged", eA2 is not None
          and eB2 is not None and sum(1 for x in pf2["e"] if x["present"]) == 2 and eA2["record"] == recA and eA2["token"] == eA["token"] and eB2["token"] == eB["token"])
    check("C guest B's record, stored when its session ended, is B's OWN (Fern: gender 0, face 2, its PersonalID, rev >= 1, empty pockets), different from A's", eB2 is not None
          and eB2["rev"] >= 1 and recB[:20] == pidB and recB[0x14] == 0 and recB[0x15] == 2 and L.record_inventory(recB) == ((0,) * 15, 0, 0) and recB != recA)
    same_regions("C (all clients stopped)")

    # ---------------- D: host restart, both guests reconnect to their own records ----------------
    rcode = host.stop()
    check("D the first host process stopped (exit code before stop: %s)" % rcode, rcode is None)
    time.sleep(2.0)
    if not env.start_host():
        check("D the second host process reached field-ready state", False)
        return
    host = env.host
    check("D the second host process reached field-ready state", True)
    off_d = len(host.log_text())
    cA3 = env.guest("a", "ga3")
    mA3 = cA3.wait_for_log(ADOPT_RX, 150.0)
    cB3 = env.guest("b", "gb3")
    mB3 = cB3.wait_for_log(ADOPT_RX, 150.0)
    L.pump_sleep(4.0)
    hd = host.log_text()[off_d:]
    tA3, tB3 = cA3.log_text(), cB3.log_text()
    check("D after the host restart guest A -> its own slot 0 and guest B -> its own slot 1: KNOWN, token verified, no first contact, nothing refused",
          re.search(r"bound to GUEST slot 0 \(known guest: token verified;", hd) is not None and re.search(r"bound to GUEST slot 1 \(known guest: token verified;", hd) is not None
          and "first contact" not in hd and "WRONG token" not in hd and "WITHOUT a token" not in hd)
    check("D both clients adopted their stored record from the restarted host: no MIGRATE, token verified with their own slot numbers, no violation",
          mA3 is not None and mB3 is not None and "guest token verified by the host (guest slot 0)" in tA3 and "guest token verified by the host (guest slot 1)" in tB3
          and "MIGRATE upload" not in tA3 and "MIGRATE upload" not in tB3 and not re.search(BAD_RX, tA3) and not re.search(BAD_RX, tB3))
    cA3.stop()
    cB3.stop()
    L.pump_sleep(8.0)
    eA3, pf3 = entry_of(pidA)
    eB3, _ = entry_of(pidB)
    check("D after the restart both records are byte-identical to the pre-restart ones, still exactly two entries, tokens unchanged (records never mixed)",
          eA3 is not None and eB3 is not None and sum(1 for x in pf3["e"] if x["present"]) == 2 and eA3["record"] == recA and eB3["record"] == recB
          and eA3["token"] == eA["token"] and eB3["token"] == eB["token"])
    same_regions("D (end)")
    bal_end = G3.live_remote_slots(host.log_text())
    check("end: no open remote-player slot after every client is gone: %s" % bal_end, all(v == 0 for v in bal_end.values()))
    check("the host stayed alive and logged no INTERNAL error", host.alive() and "*** INTERNAL" not in host.log_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12000)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)) == "bin_fixture4":
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_g4_real_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    env = Env(args.port, results)
    import atexit
    atexit.register(lambda: [remove_client_dir(d) for d in list(env.dirs.values())])   # registered BEFORE the library's fixture guards: runs AFTER their at-exit restore
    try:
        run(args, results, env)
    finally:
        if env.host is not None:
            env.host.stop()
        time.sleep(2.0)   # let every stopped client / host release its files before the save dir is restored
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)
        shutil.rmtree(snap_dir, ignore_errors=True)
        for d in env.dirs.values():
            remove_client_dir(d)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
