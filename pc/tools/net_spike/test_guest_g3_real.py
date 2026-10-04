#!/usr/bin/env python3
"""test_guest_g3_real.py - GUESTS G3.1 / G3.3 / G3.4: a REAL game client started with `--guest`, REAL host, killed abruptly and restarted.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation, no manual play, NO visual verification):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --guest` (a NEW process every run; cwd = the disposable fixture dir)
Run ONLY on the disposable pc\\build64\\bin_fixture4 (all FOUR residents present; NET_SPIKE_GAME_BIN=<absolute path>, ports 11800+). The fixture save dir is snapshotted at start
and restored at the end. What this proves is STATE / BINDING / NETWORK behaviour only -- never how the arrival cutscene or the player model looks.

  A  (G3.1) arrival: the arrival path ran (log: save re-read from disk, FRESH record, "guest init ran WITHOUT the gateway" with the gateway flag 0 and player_no 4, then the
     station arrival); no mEv_SetGateway evidence; the host bound GUEST slot 0 and applied the first MIGRATE as resident 4; the host's resident records private_data[0..3] and
     homes[0..3] in the GCI are byte-identical to the baseline after the guest session and after the disconnect for the residents / houses the host does not play (slots 1..3,
     strict); the host's OWN slot 0 / house 0 may only show the small envelope a host-only control run (no client) shows by itself (the host re-saves about every 60 s)
  B  (G3.4) abrupt disconnect: the client process is killed (TerminateProcess); the host logs the transport disconnect AND the destruction of the remote-player slot of the old
     peer: no stale remote player entry survives (READY / created vs destroyed balance per peer)
  C  (G3.4 + G3.3 gender) a NEW process restarts with the same guest.ini key but gender / face EDITED: the host recognises the SAME guest (KNOWN, slot 0, token verified), no
     second guests.dat entry, no MIGRATE, the record bytes are identical; the adopted record's gender differs from the placeholder -> the client logs the look change and
     the ONE same-position scene reload is requested and accepted (goto_other_scene res=1) and the session stays connected
  D  (G3.4 + G3.3 face) another restart with only the FACE edited: the face is rebuilt in place (no scene reload); same guest, no duplicate
Usage: python test_guest_g3_real.py [--port 11800]
"""
import argparse
import os
import re
import shutil
import sys
import time

import net_spike_lib as L
import test_guest_protocol as TG

HERE = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR_REL = "save"
ADOPT_RX = r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=(\d+) kind=FULL"
PID, LID = 0x4D13, 0x5E24
GCI_REL = os.path.join("card_a", "DobutsunomoriP_MURA.gci")
SAVE_T = 0x40 + 0x26000            # Save_t inside the GCI
PRIV_OFF, PRIV_STRIDE, PRIV_N = SAVE_T + 0x20, 0x2440, 4          # private_data[4]
HOME_OFF, HOME_STRIDE, HOME_N = SAVE_T + 0x9CE8, 0x26B0, 4        # homes[4]  (0x137A8 - 0x9CE8 = 4 * 0x26B0)
BAD_RX = r"violation \d/\d|BAD[_ ]DIGEST|ADOPT_FAILED|\*\*\* host REFUSED|host rejected the connection"


def ini_text(name, gender, face, home, pid, lid):
    return "# test profile\nname = %s\ngender = %d\nface = %d\nhome_town = %s\nplayer_id = 0x%04X\nland_id = %d\n" % (name, gender, face, home, pid, lid)


def gci_regions(save_dir):
    """([private_data[i] bytes], [homes[i] bytes]) of the fixture's GCI (read only)."""
    with open(os.path.join(save_dir, GCI_REL), "rb") as f:
        data = f.read()
    priv = [data[PRIV_OFF + i * PRIV_STRIDE:PRIV_OFF + (i + 1) * PRIV_STRIDE] for i in range(PRIV_N)]
    home = [data[HOME_OFF + i * HOME_STRIDE:HOME_OFF + (i + 1) * HOME_STRIDE] for i in range(HOME_N)]
    return priv, home


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


class Env:
    def __init__(self, port, results):
        self.port, self.results = port, results
        self.check = lambda d, c: L.check(d, bool(c), results)
        self.host = None
        self.save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
        self.ini = os.path.join(self.save_dir, "mp", "guest.ini")

    def start_host(self):
        for i in range(3):
            h = L.HostProcess(port=self.port, extra_args=["--bootstrap-resident", "0"], log_path=os.path.join(HERE, "guest_g3_real_host_try%d.log" % i),
                              bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
                self.host = h
                return True
            h.stop()
            time.sleep(2.0)
        return False

    def client(self, tag):
        return L.ClientProcess("127.0.0.1:%d" % self.port, extra_args=["--guest"], log_path=os.path.join(HERE, "guest_g3_real_%s.log" % tag), bin_dir=L.GAME_BIN_DIR, label=tag).start()


def live_remote_slots(host_log):
    """peer id -> (opened - destroyed) of the host's remote-player slots, from the host log (READY / discovered open a slot, 'disconnected -- destroying' closes it)."""
    bal = {}
    for m in re.finditer(r"\[NET\]\[REMOTE\] player (\d+) (READY|discovered via movement relay|disconnected -- destroying|timed out)", host_log):
        p = int(m.group(1))
        if m.group(2) in ("READY", "discovered via movement relay"):
            bal[p] = bal.get(p, 0) + 1
        else:
            bal[p] = bal.get(p, 0) - 1
    return bal


def run(args, results, env):
    check = env.check
    ip = "127.0.0.1"
    name, home = "Quill", "Hometwn"
    ident = L.GuestIdentity(name.encode().ljust(8), PID, home.encode().ljust(8), LID)
    pid_be = L.guest_pid_be(ident)
    res_names = [bytes(L.resident_player(i).player_name) for i in range(4)]
    check("fixture: all FOUR residents exist in the disposable save", len(L.read_test_save_residents()) == 4 and all(ex for _i, _p, ex in L.read_test_save_residents()))
    check("fixture: the test guest name '%s' is not a resident's name" % name, all(n.rstrip(b"\x00 ") != name.encode() for n in res_names))
    base_priv, base_home = gci_regions(env.save_dir)
    gci_path = os.path.join(env.save_dir, GCI_REL)
    gci_mtime0 = os.path.getmtime(gci_path)
    os.makedirs(os.path.dirname(env.ini), exist_ok=True)
    with open(env.ini, "wb") as f:
        f.write(ini_text(name, 1, 5, home, PID, LID).encode("ascii"))
    if not env.start_host():
        check("host reached genuine field-ready state", False)
        return
    check("host reached genuine field-ready state", True)
    host = env.host

    def ndiff(a, b):
        return sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))

    def same_regions(tag):
        """The host plays resident 0 and re-saves its GCI about every 60 s (a HOST-ONLY control run, no client at all, changed exactly 1 byte of private_data[0] and 214 bytes of
        homes[0]: the host's own player / house, e.g. time-driven state). So: the three residents the host does NOT play and their houses must be byte-identical (strict), and the host's
        own slot / house may only show that small host-save envelope (<= 16 / <= 512 bytes), never a rewrite."""
        p, h = gci_regions(env.save_dir)
        check("%s host GCI: private_data[1..3] (the 3 residents the host does not play) byte-identical to the baseline" % tag, p[1:] == base_priv[1:])
        check("%s host GCI: homes[1..3] byte-identical to the baseline (the guest allocated / aliased none of those houses)" % tag, h[1:] == base_home[1:])
        check("%s host GCI: the host's own resident 0 / house 0 show only the host-only-control envelope (private_data[0]: %d bytes <= 16, homes[0]: %d bytes <= 512)"
              % (tag, ndiff(p[0], base_priv[0]), ndiff(h[0], base_home[0])), ndiff(p[0], base_priv[0]) <= 16 and ndiff(h[0], base_home[0]) <= 512)

    # ---------------- A: arrival ----------------
    off0 = len(host.log_text())
    c1 = env.client("a1")
    m1 = c1.wait_for_log(ADOPT_RX, 150.0)
    t1 = c1.log_text()
    print("--- client A1 log tail ---")
    print("\n".join(t1.splitlines()[-16:]))
    ix = [t1.find(s) for s in ("town save re-read from disk before the arrival", "FRESH guest record (not a copy of any resident)", "guest init ran WITHOUT the gateway", "bound as a foreigner, arriving at the station")]
    check("A client log: the arrival ran in order: save re-read from disk (pc_save_reload) -> FRESH record -> guest init -> station arrival", all(i >= 0 for i in ix) and ix == sorted(ix))
    check("A client log: the guest init marker says mSDI_StartDataInitGuest ran WITHOUT the gateway: gateway flag after init = 0, player_no = 4",
          "mSDI_StartDataInitGuest: no mEv_SetGateway / return animal / goodbye mail): gateway flag after init = 0 (0 = not set), player_no = 4" in t1)
    check("A client log: no arrival failure line and no refusal", "--bootstrap-guest: FAILED" not in t1 and "--bootstrap-guest: REFUSED" not in t1 and not re.search(BAD_RX, t1))
    check("A client log: the record was ADOPTED (the client reached SYNCED after the real arrival)", m1 is not None)
    ht = host.log_text()[off0:]
    mb = re.search(r"peer (\d+) bound to GUEST slot 0 \(first contact: token minted;", ht)
    check("A host log: peer bound to GUEST slot 0 (first contact) and the first MIGRATE was applied as resident 4 (player_no 4 on the wire)",
          mb is not None and re.search(r"MIGRATE xfer 1 APPLIED \(resident 4 ", ht) is not None and "first MIGRATE REFUSED" not in ht)
    p1 = int(mb.group(1)) if mb else -1
    check("A host log: the remote-player slot of that peer is open (READY) while the guest is connected", live_remote_slots(host.log_text()).get(p1, 0) == 1)
    L.pump_sleep(6.0)
    same_regions("A (guest connected)")
    e0, _pf = wait_guest_entry(pid_be, lambda e: True, 10.0)
    check("A guests.dat holds exactly the guest's key", e0 is not None and e0["pid"] == pid_be)
    if e0 is None:
        c1.stop()
        return
    token = e0["token"]

    # ---------------- B: abrupt disconnect ----------------
    off_b = len(host.log_text())
    rc = c1.stop()
    check("B the client process was killed while running (it had not exited on its own: %s)" % rc, rc is None)
    mdis = host.wait_for_log(r"\[NET\] host: peer %d disconnected" % p1, 60.0, since_offset=off_b)
    mdes = host.wait_for_log(r"\[NET\]\[REMOTE\] player %d disconnected -- destroying remote-player actor" % p1, 5.0, since_offset=off_b)
    check("B host log: the old peer is gone (transport disconnect) and its remote-player slot was destroyed (no stale puppet)", mdis is not None and mdes is not None)
    check("B host remote-player balance: no peer keeps an open slot after the guest is gone: %s" % live_remote_slots(host.log_text()), all(v == 0 for v in live_remote_slots(host.log_text()).values()))
    L.pump_sleep(7.0)
    e1, pf1 = wait_guest_entry(pid_be, lambda e: e["rev"] >= 1, 20.0)
    check("B guests.dat holds the guest's record at rev >= 1 after the abrupt disconnect (host stored it)", e1 is not None)
    if e1 is None:
        return
    rec1 = e1["record"]
    check("B the stored record is the guest's: gender 1, face 5", rec1[:20] == pid_be and rec1[0x14] == 1 and rec1[0x15] == 5)
    same_regions("B (after the disconnect)")

    # ---------------- C: restart with an EDITED look (gender differs): same guest + one look reload ----------------
    with open(env.ini, "wb") as f:
        f.write(ini_text(name, 0, 2, home, PID, LID).encode("ascii"))
    off_c = len(host.log_text())
    c2 = env.client("c2")
    m2 = c2.wait_for_log(ADOPT_RX, 150.0)
    mreload = c2.wait_for_log(r"\[NET\]\[LOOK\] same-position scene reload \(SCENE_FG at -?\d+,-?\d+\)[^\n]*goto_other_scene res=1", 90.0)
    t2 = c2.log_text()
    hc = host.log_text()[off_c:]
    mb2 = re.search(r"peer (\d+) bound to GUEST slot 0 \(known guest: token verified;", hc)
    check("C host log: the restarted client is recognised as the SAME guest (KNOWN, slot 0, token verified), no first contact, no new slot", mb2 is not None and "first contact" not in hc
          and "bound to GUEST slot 1" not in hc)
    check("C client log: IDENTITY_EXT carried the token, verified by the host; the record was ADOPTED with no MIGRATE; nothing refused", m2 is not None and "sent IDENTITY_EXT (token held)" in t2
          and "guest token verified by the host (guest slot 0)" in t2 and "MIGRATE upload" not in t2 and not re.search(BAD_RX, t2))
    check("C (G3.1) the restarted client also ran the no-gateway guest init", "gateway flag after init = 0 (0 = not set), player_no = 4" in t2)
    check("C (G3.3) the placeholder (ini gender 0 face 2) differs from the stored record (gender 1 face 5): the client logged the look change and requested the same-position reload",
          "[NET][LOOK] guest look changed on adopt: gender 0 -> 1 face 2 -> 5" in t2)
    check("C (G3.3) the ONE same-position scene reload was performed (goto_other_scene res=1) and happened once", mreload is not None and t2.count("[NET][LOOK] same-position scene reload") == 1)
    L.pump_sleep(8.0)
    t2b = c2.log_text()
    check("C after the reload the session is still alive and connected: client process running, host did not drop the peer, no violation / refusal",
          c2.alive() and not re.search(r"\[NET\] host: peer %d disconnected" % (int(mb2.group(1)) if mb2 else -1), host.log_text()[off_c:]) and not re.search(BAD_RX, t2b))
    p2 = int(mb2.group(1)) if mb2 else -1
    bal = live_remote_slots(host.log_text())
    check("C host remote-player balance: exactly ONE open slot (the live client's), none for the old peer unless the transport reused its id: %s" % bal,
          sum(1 for v in bal.values() if v > 0) == 1 and bal.get(p2, 0) == 1 and all(v in (0, 1) for v in bal.values()))
    e2, pf2 = wait_guest_entry(pid_be, lambda e: True, 10.0)
    check("C exactly ONE guests.dat entry (no duplicate record slot), same token", e2 is not None and sum(1 for x in pf2["e"] if x["present"]) == 1 and e2["token"] == token)
    off_d = len(host.log_text())
    c2.stop()
    mdis2 = host.wait_for_log(r"\[NET\] host: peer %d disconnected" % p2, 60.0, since_offset=off_c)
    check("C the second abrupt disconnect is observed by the host too", mdis2 is not None)
    L.pump_sleep(7.0)
    e2b, _ = wait_guest_entry(pid_be, lambda e: True, 10.0)
    check("C the stored record after the second session is byte-identical (a returning guest with an edited ini does not change the host record)", e2b is not None and e2b["record"] == rec1
          and e2b["rev"] >= e1["rev"])
    same_regions("C (after the second disconnect)")

    # ---------------- D: restart with only the FACE edited: rebuilt in place ----------------
    with open(env.ini, "wb") as f:
        f.write(ini_text(name, 1, 3, home, PID, LID).encode("ascii"))
    c3 = env.client("d3")
    m3 = c3.wait_for_log(ADOPT_RX, 150.0)
    mface = c3.wait_for_log(r"\[NET\]\[LOOK\] guest face changed on adopt: face 3 -> 5 \(gender 1\)", 30.0)
    t3 = c3.log_text()
    hd = host.log_text()[off_d:]
    check("D host log: again the SAME guest (KNOWN, slot 0, token verified), no new slot", re.search(r"bound to GUEST slot 0 \(known guest: token verified;", hd) is not None and "first contact" not in hd
          and "bound to GUEST slot 1" not in hd)
    check("D client log: adopted, no MIGRATE, nothing refused", m3 is not None and "MIGRATE upload" not in t3 and not re.search(BAD_RX, t3))
    check("D (G3.3) face-only difference: the face was rebuilt IN PLACE (no scene reload requested)", mface is not None and "[NET][LOOK] same-position scene reload" not in t3
          and "requesting ONE same-position scene reload" not in t3)
    L.pump_sleep(6.0)
    c3.stop()
    L.pump_sleep(7.0)
    e3, pf3 = wait_guest_entry(pid_be, lambda e: True, 10.0)
    check("D exactly ONE guests.dat entry, same token, record byte-identical", e3 is not None and sum(1 for x in pf3["e"] if x["present"]) == 1 and e3["token"] == token and e3["record"] == rec1)
    same_regions("D (end)")
    bal_end = live_remote_slots(host.log_text())
    check("end: no open remote-player slot after every guest is gone: %s" % bal_end, all(v == 0 for v in bal_end.values()))
    print("info: host GCI mtime changed during the run: %s" % (os.path.getmtime(gci_path) != gci_mtime0))
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
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_g3_real_save_snapshot_%d" % os.getpid())
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
