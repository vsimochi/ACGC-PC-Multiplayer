#!/usr/bin/env python3
"""test_roommates_real.py - ROOMMATES / MOVE-IN with a REAL guest client: REAL listen host + REAL `--guest` client game process + a scripted resident (FakeClient A).

TIER: REAL HOST + REAL GUEST CLIENT (Samsung display, volume 1). The gyroid is seeded next to the guest by the host (AC_TEST_OFURN_SEED=0x15B0 + _SEED_NEAR); the guest
presses A at it through the REAL wait-state seam (TEST-ONLY AC_TEST_ROOM_GYROID: the player is put south of it facing it) and the REAL gyroid menu is driven by
AC_TEST_ROOM_UI (row picks like a player: "Ask to move in", then "Yes"). Resident A (scripted) holds the gyroid (it claims it at its first QUERY) and decides.
Disposable fixture pc\\build64\\bin_fixture4_room_real (a fresh copy of bin_fixture4 + the pc\\build64_w3 exe). Ports 41800-41899.
  U1 the host seeded the gyroid next to the guest; A claimed it (QUERY = OWNER)
  U2 the guest's A opened the move-in menu (the host answered its QUERY: role CAN_REQUEST), the scripted picks asked, the host stored the REQUEST
  U3 A accepts: the real client logged STATUS roommate of A's house, the "accepted" notice and received house pushes of that house
  U4 the guest walked into the shared house (TEST-ONLY warp through goto_other_scene; the door rule says "lives here" = 1) and got "Welcome home"
  U5 A removes the guest: the real client got STATUS "not a roommate" and was SHOWN OUT of the house (back in the town)
  U6 host and guest alive
NOT asserted: what the menu / notices look like on screen (no image comparison), the real door walk (the warp replaces it), the knock skip (source audited).
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

os.environ["AC_TEST_HOOKS"] = "1"
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_room_real"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)
GYROID = 0x15B0
ENV_KEYS = ("AC_TEST_OFURN_SEED", "AC_TEST_OFURN_SEED_NEAR", "AC_TEST_ROOM_GYROID", "AC_TEST_ROOM_GYROID_REPEAT", "AC_TEST_ROOM_UI", "AC_TEST_ROOM_ENTER")
STARTED = []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=41810)
    args = ap.parse_args()
    if not (41800 <= args.port <= 41899):
        print("REFUSING: ports 41800-41899 only", file=sys.stderr)
        return 2
    T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
    import net_spike_lib as L
    import test_txn_real_client as RC
    import test_ofurn_protocol as OF
    import test_roommates_protocol as RP
    RP.L = L
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "room_real_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    a = None
    try:
        residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
        ini = os.path.join(save_dir, "mp", "guest.ini")
        os.makedirs(os.path.dirname(ini), exist_ok=True)
        os.environ["AC_TEST_OFURN_SEED"] = "0x%04X,0" % GYROID
        os.environ["AC_TEST_OFURN_SEED_NEAR"] = "1"
        host = rig.start_host("room_real", args.port, [])
        check("host reached genuine field-ready state", host is not None)
        if host is None:
            return L.summary_and_exit_code(results)
        STARTED.append(host)
        with open(ini, "wb") as f:  # (after start_host: it restores the save snapshot)
            f.write(b"# test profile\nname = Mover\ngender = 1\nface = 3\nhome_town = Farvale\nplayer_id = 0x4D31\nland_id = 24117\n")
        L.resolve_host_town("127.0.0.1", args.port)
        os.environ["AC_TEST_ROOM_GYROID"] = "1"
        os.environ["AC_TEST_ROOM_GYROID_REPEAT"] = "1"
        os.environ["AC_TEST_ROOM_UI"] = "ask,yes"
        os.environ["AC_TEST_ROOM_ENTER"] = "1"
        g = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--guest"], log_path=os.path.join(HERE, "room_real_guest.log"), bin_dir=L.GAME_BIN_DIR,
                            label="roomG").start()
        STARTED.append(g)

        def pump_until(fn, timeout):
            end = time.time() + timeout
            while time.time() < end:
                v = fn()
                if v:
                    return v
                L.pump_sleep(0.3) if a is not None else time.sleep(0.3)
            return fn()

        m = pump_until(lambda: re.search(r"seeded placed piece at \((\d+),(\d+)\) item 0x%04X rot 0 -> placed \(next to peer (\d+)" % GYROID, host.log_text()), 300.0)
        check("U1 the host seeded the gyroid next to the real guest", m is not None)
        if m is None:
            return L.summary_and_exit_code(results)
        ux, uz = int(m.group(1)), int(m.group(2))
        a = L.FakeClient("A", "127.0.0.1", args.port, player=L.resident_player(residents[0]))
        a.rec_resident_idx = residents[0]
        a.connect_and_ready(quiet=True)
        OF.stand_south_of(a, ux - 1, uz)
        r = RP.ask(a, RP.QUERY, ux, uz)
        check("U1 resident A claimed the gyroid (QUERY -> OWNER, house %s)" % (r and r["house"]), r is not None and r["op"] == RP.INFO and r["role"] == RP.ROLE_OWNER)
        house = r["house"] if r else None
        req = pump_until(lambda: re.search(r"\[NET\]\[ROOM\] host: peer \d+ \"[^\"]*\" REQUEST: guest \"Mover", host.log_text()), 240.0)
        gt = g.log_text()
        check("U2 the guest's A at the gyroid opened the move-in menu (role CAN_REQUEST) and the scripted picks asked: the host stored the REQUEST",
              req is not None and re.search(r"\[ROOM\]\[UI\] INFO role 2 ", gt) is not None and "[ROOM][UI][TEST-ONLY] pick 2 'yes'" in gt)
        k = None
        r = RP.ask(a, RP.QUERY, ux, uz)
        for e in (r["e"] if r else []):
            if e[0] == RP.ST_PENDING and e[2].rstrip(b" \x00") == b"Mover":
                k = e[3]
        check("U3 A's list shows the real guest's request", k is not None)
        if k is None:
            return L.summary_and_exit_code(results)
        r = RP.ask(a, RP.ACCEPT, ux, uz, key=k)
        check("U3 A accepts -> OK", r is not None and r["result"] == RP.R_OK)
        st = pump_until(lambda: re.search(r"\[NET\]\[ROOM\] client: STATUS: roommate of house (\d)", g.log_text()), 30.0)
        check("U3 the real guest got STATUS roommate of A's house (%s)" % (st and st.group(1)), st is not None and int(st.group(1)) == house)
        nt = pump_until(lambda: "let you move in. Welcome home!" in g.log_text(), 30.0)
        check("U3 ... and the 'accepted' notice line", nt)
        en = pump_until(lambda: re.search(r"\[NET\]\[ROOM\]\[TEST-ONLY\] enter: walking into the shared house %d .*door entry 1\)" % house, g.log_text()), 90.0)
        wh = pump_until(lambda: "[NET][ROOM] client: entered the shared house %d" % house in g.log_text(), 60.0)
        check("U4 the guest walked into the shared house (the door rule: lives here = 1) and got 'Welcome home'", en is not None and wh and "notice: Welcome home" in g.log_text())
        hp = re.findall(r"\[NET\]\[HOUSE\][^\n]*client[^\n]*house (\d)", g.log_text())
        print("INFO - client house-sync lines naming a house: %s" % sorted(set(hp)))
        L.pump_sleep(4.0)
        OF.stand_south_of(a, ux - 1, uz)
        r = RP.ask(a, RP.REMOVE, ux, uz, key=k)
        check("U5 A removes the guest -> OK", r is not None and r["result"] == RP.R_OK)
        sn = pump_until(lambda: "[NET][ROOM] client: STATUS: not a roommate" in g.log_text(), 30.0)
        so = pump_until(lambda: re.search(r"\[NET\]\[ROOM\] client: shown out of house %d" % house, g.log_text()), 60.0)
        check("U5 the real guest got STATUS 'not a roommate' and was SHOWN OUT of the house", sn and so is not None)
        check("U6 host and guest alive", host.alive() and g.alive())
    finally:
        if a is not None:
            try:
                a.close()
            except Exception:  # noqa: BLE001
                pass
        for p in STARTED:
            try:
                if p.alive():
                    p.stop()
            except Exception:  # noqa: BLE001
                pass
        for k2 in ENV_KEYS:
            os.environ.pop(k2, None)
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
