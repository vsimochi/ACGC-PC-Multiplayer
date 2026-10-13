#!/usr/bin/env python3
"""test_guest_museum_real.py - GUEST MUSEUM DONATION with a REAL `--guest` game client and a REAL host (hook-driven, no GUI automation).

TIER: REAL HOST + REAL GUEST CLIENT game processes:
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --guest --ts-test-donate` (guest profile save/mp/guest.ini written by this test)
  plus a scripted RESIDENT observer (FakeClient, resident slot 1) connected the whole time: its museum mirror must show the guest's donation.
The client's real request path runs: pc_net_game_ts_begin_museum_donate (the entry point Blathers calls; the G5.1 guest guard is gone) -> TXN_COMMIT ->
the host handler -> TXN_RESULT -> the pocket apply step, then the TOWN_SVC_STATE mirror. The ONLY synthetic input is the default-off --ts-test-donate hook
(it puts one fish the LOCAL mirror lacks into a free pocket slot = "the guest caught it", waits 5 s for the normal D3 upload, then begins the donation).
NOT covered: the Blathers dialogue itself (no UI automation; its guest branches are SOURCE AUDITED in test_guest_museum_src.py), the visual museum rooms.

Run ONLY on a disposable pc\\build64\\bin_fixture4* copy (NET_SPIKE_GAME_BIN=<absolute path>, ports 12700+). The fixture save dir is snapshotted at start and
restored at the end (guest.ini and the guest store disappear with it).
  G1  the guest client arrives (player_no 4, adopted record) and its hook donation is sent as MUSEUM_DONATE, answered APPLIED, applied by the host
      post-image (the fish leaves the pocket ONLY then); no local refusal; its local museum says 'already donated'
  G2  host: one APPLIED line for the GUEST record (4 + slot) and one commit line with donor slot 5; no CONFLICT / INTERNAL
  G3  the resident observer's museum mirror carries the fish with nibble 5 and the guest's mirror equals it
  G4  after the guest left: the host's guest store record and a new scripted session of a resident agree (pockets without the fish, museum nibble 5);
      the host's GCI save holds nibble 5
  B   the REAL Blathers dialogue (regression of the manual report): the guest has 3 fish + 3 insects (AC_TEST_MUSEUM_POCKETS), is warped into the museum entrance and
      parked in front of Blathers (AC_TEST_MUSEUM_ROOM=entrance, AC_TEST_GOTO 'c 40'); the autopilot 'mashf' talks and answers every choice with its first option.
      Every offer must take an ACCEPT row and be APPLIED (never the 'a former resident donated it' row / a give-back of an item the host already took), the
      observer's museum holds all six with value 5 and the six pocket slots are emptied once.
Usage: python test_guest_museum_real.py [--port 12700] [--only G,B] [--dist 40]
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
PID, LID = 0x4D29, 0x5E31
GCI_MUSEUM = 0x40 + 0x26000 + 0x213A8
FISH0 = 0x2300


def nib_fish(blob, idx):
    return (blob[0x15 + (idx >> 1)] >> ((idx & 1) * 4)) & 0xF


def gci_museum():
    with open(os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL), "rb") as f:
        d = f.read()
    return d[GCI_MUSEUM:GCI_MUSEUM + 63]


def wait_until(fn, timeout):
    t0 = time.monotonic()
    v = fn()
    while not v and time.monotonic() - t0 < timeout:
        L.pump_sleep(0.7)
        v = fn()
    return v


def wait_log_pumping(proc, pattern, timeout):
    """ClientProcess.wait_for_log, but pumping the scripted clients' hub meanwhile (the resident observer must keep its link alive)."""
    rx = re.compile(pattern)
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        mo = rx.search(proc.log_text())
        if mo:
            return mo
        if not proc.alive():
            return None
        L.pump_sleep(0.5)
    return None


def run(args, results):
    ck = lambda d, c: L.check(d, bool(c), results)
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    ck("fixture: a non-host resident exists for the observer (%s)" % residents, residents)
    if not residents:
        return
    r1 = residents[0]
    ini = os.path.join(save_dir, "mp", "guest.ini")
    os.makedirs(os.path.dirname(ini), exist_ok=True)
    with open(ini, "wb") as f:
        f.write(("# test profile\nname = Wendel\ngender = 0\nface = 2\nhome_town = Farvale\nplayer_id = 0x%04X\nland_id = %d\n" % (PID, LID)).encode("ascii"))
    host = None
    for i in range(3):
        h = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT)], log_path=os.path.join(HERE, "guest_museum_real_host_try%d.log" % i),
                          bin_dir=L.GAME_BIN_DIR).start()
        if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT):
            host = h
            break
        h.stop()
        time.sleep(2.0)
    ck("host reached genuine field-ready state", host is not None)
    if host is None:
        return
    cl = None
    obs = None
    try:
        L.resolve_host_town(ip, args.port)
        obs = L.FakeClient("OBS", ip, args.port, player=L.resident_player(r1))
        obs.rec_resident_idx = r1
        obs.connect_and_ready(quiet=True)
        st0 = obs.wait_ts_state(L.PC_NETGAME_TS_MUSEUM, 6.0, min_seq=1)
        ck("the resident observer is READY and holds the museum mirror", st0 is not None)
        off_h = len(host.log_text())
        cl = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--guest", "--ts-test-donate"], log_path=os.path.join(HERE, "guest_museum_real_client.log"),
                             bin_dir=L.GAME_BIN_DIR, label="guest_museum").start()
        m = wait_log_pumping(cl, r"--ts-test-donate: final item 0x([0-9A-F]{4}) museum_info=(\d+)", 240.0)
        ck("G1 the guest client's hook ran to its final report", m is not None)
        ct = cl.log_text()
        ht = host.log_text()[off_h:]
        ck("G1 the client is a guest (adopted record, player_no 4 / the guest arrival path ran)",
           re.search(r"\[NET\]\[REC\] client: adopted rev=\d+ epoch=\d+ kind=FULL", ct) is not None and re.search(r"guest init ran WITHOUT the gateway[^\n]*player_no = 4", ct) is not None)
        if m is None:
            return
        item = int(m.group(1), 16)
        fi = item - FISH0
        m_place = re.search(r"--ts-test-donate: placed fish item 0x([0-9A-F]{4}) into pocket slot (\d+)", ct)
        m_sent = re.search(r"TXN_COMMIT sent kind=MUSEUM_DONATE request=(\d+) nonce=(\d+) seq=(\d+) dest=0 slot=(\d+) item=0x([0-9A-F]{4})", ct)
        m_res = re.search(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=MUSEUM_DONATE request=(\d+) seq=(\d+) rev=(\d+)", ct)
        m_app = re.search(r"client: APPLIED request (\d+) kind=MUSEUM_DONATE -- (.*?): slot (\d+) item 0x0000", ct)
        ck("G1 no local refusal: the G5.1 'MUSEUM_DONATE refused locally' line never appears and the hook was not refused", "refused locally" not in ct
           and "the request was refused locally" not in ct)
        ck("G1 the placed fish 0x%04X was sent as MUSEUM_DONATE from its slot, answered TXN_RESULT(APPLIED) for that seq and applied by the host post-image (slot emptied)" % item,
           None not in (m_place, m_sent, m_res, m_app) and int(m_place.group(1), 16) == item and int(m_sent.group(5), 16) == item
           and int(m_sent.group(4)) == int(m_place.group(2)) == int(m_app.group(3)) and m_sent.group(3) == m_res.group(2))
        ck("G1 order: placed < sent < RESULT < pocket write; the pocket never changed before APPLIED; nothing rejected / inconsistent",
           None not in (m_place, m_sent, m_res, m_app) and m_place.start() < m_sent.start() < m_res.start() < m_app.start()
           and "POCKET CHANGED BEFORE APPLIED" not in ct and "REJECTED(" not in ct and "inconsistent post-image" not in ct and "protocol violation" not in ct)
        ck("G1 the guest's LOCAL museum (written only by the mirror) says 'already donated' (2) for the fish", int(m.group(2)) == 2)
        fm = re.search(r"--ts-test: final pockets=([0-9A-F,]+) police=", ct)
        final_pockets = tuple(int(x, 16) for x in fm.group(1).strip(",").split(",")) if fm else None
        ck("G1 the guest's final pockets no longer hold the fish", final_pockets is not None and item not in final_pockets)
        mg = re.search(r"peer (\d+) guest record (\d+) MUSEUM_DONATE item=0x%04X slot=\d+ committed \(donor slot 5\) \[TXN\]" % item, ht)
        ck("G2 host: exactly one APPLIED MUSEUM_DONATE, charged to a GUEST record (index >= 4), committed with donor slot 5; no CONFLICT / INTERNAL",
           mg is not None and int(mg.group(2)) >= 4 and len(re.findall(r"TXN APPLIED kind=MUSEUM_DONATE ", ht)) == 1
           and re.search(r"resident %s TXN APPLIED kind=MUSEUM_DONATE " % (mg.group(2) if mg else "x"), ht) is not None
           and "CONFLICT" not in ht and "*** INTERNAL" not in ht)
        so = obs.wait_ts_state(L.PC_NETGAME_TS_MUSEUM, 5.0, min_seq=1, pred=lambda b: nib_fish(b, fi) != 0)
        ck("G3 the resident observer's museum mirror carries the fish with the guest donor value 5", so is not None and nib_fish(so[1], fi) == 5)
        m_gmir = None
        for mm in re.finditer(r"\[NET\]\[TS\] client: applied service 2 \(MUSEUM\) seq (\d+) digest 0x([0-9A-F]{8})", ct):
            if m_res is not None and mm.start() > m_res.start():
                m_gmir = mm
                break
        ck("G3 the guest applied the museum mirror of its donation after the RESULT, with the digest the observer holds",
           m_gmir is not None and so is not None and int(m_gmir.group(2), 16) == so[0].digest)
        gslot = int(mg.group(2)) - 4 if mg else None
        cl.stop()
        cl = None
        wait_until(lambda: re.search(r"\[NET\] host: peer %s disconnected" % (mg.group(1) if mg else "x"), host.log_text()[off_h:]) is not None, 60.0)
        obs.disconnect()
        obs.close()
        obs = None

        def store_ok():
            try:
                e = TG.parse_guests(TG.guests_path())["e"][gslot]
            except Exception:  # noqa: BLE001
                return False
            return e.get("present") and L.record_inventory(e["record"])[0] == final_pockets
        ck("G4 the host's guest store holds the guest's record with exactly the pockets the client ended with (no fish)", gslot is not None and wait_until(store_ok, 40.0))
        ck("G4 the host's GCI save holds the fish with nibble 5", wait_until(lambda: nib_fish(gci_museum(), fi) == 5, 40.0))
        f2 = L.FakeClient("OBS2", ip, args.port, player=L.resident_player(r1))
        f2.rec_resident_idx = r1
        f2.connect_and_ready(quiet=True)
        s2 = f2.wait_ts_state(L.PC_NETGAME_TS_MUSEUM, 5.0, min_seq=1)
        ck("G4 a new resident session receives the museum with nibble 5 (late join)", s2 is not None and nib_fish(s2[1], fi) == 5 and s2[1] == gci_museum())
        f2.disconnect()
        f2.close()
        ck("G4 host alive", host.alive())
    finally:
        if cl is not None:
            cl.stop()
        if obs is not None:
            try:
                obs.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        ck("host did not crash (exit code before stop: %s)" % rc, rc is None)

BLATHERS_ITEMS = [0x2300, 0x2301, 0x2302, 0x2D00, 0x2D01, 0x2D02]   # the user's manual case: 3 fish + 3 insects
ROW_RX = r"\[NET\]\[TS\] client: Blathers row for item 0x([0-9A-F]{4}): (\d+) \(([^;]*); re-derived now (\d+), local museum says (\d+)"


def nib_cat(blob, item):
    if 0x2300 <= item < 0x2328:
        i, off = item - 0x2300, 0x15
    else:
        i, off = item - 0x2D00, 0x2A
    return (blob[off + (i >> 1)] >> ((i & 1) * 4)) & 0xF


def run_blathers(args, results):
    """B: the REAL Blathers dialogue of a real --guest client, driven by the autopilot ('mashf' = A presses, every choice answered with its FIRST option:
    'donate', then the first selectable pocket item, 'hand over', 'yes, more'). Regression of the manual report: every offered exhibit the museum lacks
    must take an ACCEPT row (16..27, a transaction) and be APPLIED -- never the 'a former resident donated it' row of an exhibit that is not donated,
    and never a give-back of an item the host already took (the TXN_RESULT and the museum mirror of the same donation arrive in one network poll)."""
    ck = lambda d, c: L.check(d, bool(c), results)
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    if not residents:
        ck("B fixture has a resident for the observer", False)
        return
    r1 = residents[0]
    ini = os.path.join(save_dir, "mp", "guest.ini")
    os.makedirs(os.path.dirname(ini), exist_ok=True)
    with open(ini, "wb") as f:
        f.write(("# test profile\nname = Wendel\ngender = 0\nface = 2\nhome_town = Farvale\nplayer_id = 0x%04X\nland_id = %d\n" % (PID, LID)).encode("ascii"))
    auto = os.path.join(L.GAME_BIN_DIR, "ac_auto_guest.txt")
    if os.path.exists(auto):
        os.remove(auto)
    host = L.HostProcess(port=args.port + 5, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT)], log_path=os.path.join(HERE, "guest_museum_real_B_host.log"),
                         bin_dir=L.GAME_BIN_DIR).start()
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT)
    ck("B host reached genuine field-ready state", ok)
    cl = obs = None
    try:
        if not ok:
            return
        L.resolve_host_town(ip, args.port + 5)
        obs = L.FakeClient("OBSB", ip, args.port + 5, player=L.resident_player(r1))
        obs.rec_resident_idx = r1
        obs.connect_and_ready(quiet=True)
        st0 = obs.wait_ts_state(L.PC_NETGAME_TS_MUSEUM, 6.0, min_seq=1)
        ck("B the observer holds the museum mirror", st0 is not None)
        if st0 is None:
            return
        free = [it for it in BLATHERS_ITEMS if nib_cat(st0[1], it) == 0]
        ck("B the fixture museum lacks the test exhibits (%s)" % ["0x%04X" % x for x in free], len(free) == len(BLATHERS_ITEMS))
        env = {"AC_TEST_AUTOPILOT": "ac_auto_guest.txt", "AC_TEST_MUSEUM_POCKETS": ",".join("%04X" % x for x in BLATHERS_ITEMS), "AC_TEST_MUSEUM_ROOM": "entrance,3000", "AC_TEST_GOTO": "1"}
        cl = L.ClientProcess("127.0.0.1:%d" % (args.port + 5), extra_args=["--guest"], log_path=os.path.join(HERE, "guest_museum_real_B_client.log"), bin_dir=L.GAME_BIN_DIR,
                             env=env, label="guest_museum_b").start()
        w = wait_log_pumping(cl, r"warping into museum room 6 \(scene (\d+)\)", 240.0)
        ck("B the guest client set up its pockets and warped into the museum entrance", w is not None and cl.log_text().count("AC_TEST_MUSEUM_POCKETS: placed item") == len(BLATHERS_ITEMS))
        if w is None:
            return
        live = wait_log_pumping(cl, r"local scene live: scene=%s " % w.group(1), 60.0)
        ck("B the entrance scene is live on the guest", live is not None)
        L.pump_sleep(3.0)
        with open(os.path.join(L.GAME_BIN_DIR, "ac_goto.txt"), "w") as f:
            f.write("c %d\n" % args.dist)
        g = wait_log_pumping(cl, r"goto c: Blathers at \(([-0-9.]+), ([-0-9.]+)\)", 20.0)
        ck("B the guest was parked in front of Blathers (positioning aid)", g is not None)
        L.pump_sleep(1.5)
        with open(auto, "w") as f:
            f.write("1 mashf 320\n")
        t0 = time.monotonic()
        rows = []
        while time.monotonic() - t0 < 340.0:
            rows = re.findall(ROW_RX, cl.log_text())
            if len(rows) >= len(BLATHERS_ITEMS) or not cl.alive():
                break
            L.pump_sleep(1.0)
        L.pump_sleep(3.0)
        ct = cl.log_text()
        rows = [(int(a, 16), int(b), c, int(d), int(e)) for a, b, c, d, e in re.findall(ROW_RX, ct)]
        print("   INFO Blathers rows: %s" % [("0x%04X" % r[0], r[1], r[3]) for r in rows])
        ck("B Blathers decided a row for each of the %d offered exhibits (got %d)" % (len(BLATHERS_ITEMS), len(rows)), len(rows) == len(BLATHERS_ITEMS)
           and sorted(r[0] for r in rows) == sorted(BLATHERS_ITEMS))
        ck("B every offer took an ACCEPT row (16..27) -- never 'already donated (former resident)' / a give-back for an exhibit the museum lacked",
           rows and all(r[1] >= 16 for r in rows))
        applied = re.findall(r"TXN_RESULT APPLIED kind=MUSEUM_DONATE request=\d+ seq=\d+", ct)
        ck("B one MUSEUM_DONATE transaction per offer, each APPLIED, none rejected", len(applied) == len(BLATHERS_ITEMS) and "REJECTED(" not in ct
           and len(re.findall(r"client: begin MUSEUM_DONATE ", ct)) == len(BLATHERS_ITEMS))
        raced = [r for r in rows if r[3] < 16]
        print("   INFO offers whose row re-derived AFTER the answer would have been an 'already donated' row (the race the freeze closes): %d" % len(raced))
        so = obs.wait_ts_state(L.PC_NETGAME_TS_MUSEUM, 5.0, min_seq=1, pred=lambda b: all(nib_cat(b, it) == 5 for it in BLATHERS_ITEMS))
        ck("B the resident observer's museum holds all six exhibits with the guest donor value 5", so is not None)
        fm = re.findall(r"client: APPLIED request \d+ kind=MUSEUM_DONATE -- [^:]*: slot (\d+) item 0x0000", ct)
        ck("B the guest's pocket slots of the six items were emptied by the host post-images (no item handed back, none duplicated)", len(fm) == len(BLATHERS_ITEMS)
           and len(set(fm)) == len(BLATHERS_ITEMS))
        ck("B no dialogue hang: the client is alive and nothing was rejected / inconsistent", cl.alive() and "inconsistent post-image" not in ct and "protocol violation" not in ct)
    finally:
        for p in (cl,):
            if p is not None:
                p.stop()
        if obs is not None:
            try:
                obs.close()
            except Exception:  # noqa: BLE001
                pass
        if os.path.exists(auto):
            os.remove(auto)
        rc = host.stop()
        ck("B host did not crash (exit code before stop: %s)" % rc, rc is None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12700)
    ap.add_argument("--only", default="G,B")
    ap.add_argument("--dist", type=int, default=40)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on a pc\\build64\\bin_fixture4* copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_museum_real_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    L.check("the fixture starts WITHOUT a save/mp directory", not os.path.exists(os.path.join(save_dir, "mp")), results)
    try:
        if "G" in args.only:
            run(args, results)
        if "B" in args.only:
            shutil.rmtree(save_dir, ignore_errors=True)
            shutil.copytree(snap_dir, save_dir)
            run_blathers(args, results)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("run raised %r" % exc, False, results)
    finally:
        time.sleep(3.0)  # the stopped processes release their files first
        shutil.rmtree(save_dir, ignore_errors=True)
        if os.path.exists(os.path.join(save_dir, "mp")):
            time.sleep(3.0)
            shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir, dirs_exist_ok=True)
        shutil.rmtree(snap_dir, ignore_errors=True)
    L.check("the fixture ends WITHOUT a save/mp directory", not os.path.exists(os.path.join(save_dir, "mp")), results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
