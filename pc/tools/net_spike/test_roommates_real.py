#!/usr/bin/env python3
"""test_roommates_real.py - ROOMMATES / MOVE-IN with a REAL guest client: REAL listen host + REAL `--guest` client game process + a scripted resident (FakeClient A).

TIER: REAL HOST + REAL GUEST CLIENT (Samsung display, volume 1). The gyroid is the one that COMES WITH resident A's house (house 1: the vanilla save gyroid beside the
door). The guest is put in front of it (TEST-ONLY AC_TEST_ROOM_GYROID=1) and the gyroid's REAL talk seam (ac_haniwa_move.c_inc -> pc_room_gyroid_intercept, the vanilla
talk-position test) asks the host, then the VANILLA gyroid conversation runs with the move-in choice in its own menu; AC_TEST_ROOM_UI answers its choice windows
like a player ("Ask to move in", "Yes"). Resident A (scripted) holds house 1 and decides.
Disposable fixture pc\\build64\\bin_fixture4_room_real (a fresh copy of bin_fixture4 + the pc\\build64_w3 exe). Ports 41800-41899.
  U1 A at its house's gyroid -> OWNER
  U2 the guest's A at the house gyroid: INFO role CAN_REQUEST armed the vanilla conversation, "Ask to move in" + "Yes." chosen: the host stored the REQUEST
  U3 A accepts: the real client's STATUS roommate of house 1, the notice, save/mp/roommate_homes.ini remembers the house
  U4 the ROOMMATE edits the shared house through the REAL client commit pipeline (TEST-ONLY AC_TEST_HOUSE_CLIENT_SWAP: a furniture move in the local save of house 1, committed
     by the client's own house sync as its 'own' house): the host APPLIED an OWNER_COMMIT of house 1 for the GUEST record
  U5 the guest process is restarted: it ARRIVES AT THE DOOR of house 1 (no train / station), STATUS roommate again
  U6 it walks into house 1 (TEST-ONLY warp): its own room (field_type PLAYER_ROOM: editable), in-room commits armed
  U7 A removes it: STATUS 'not a roommate', the room is frozen (locked), it is SHOWN OUT, the freeze ends once its last commit settled
  U8 host and guest alive
NOT asserted: what the dialogue / notices look like on screen (--shots captures them), the real door walk (the warp replaces it), hand edits inside the room (the vanilla editing gates are source audited).
"""
import argparse
import os
import re
import shutil
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
ENV_KEYS = ("AC_TEST_GYROID_DIAG", "AC_TEST_ROOM_GYROID", "AC_TEST_ROOM_GYROID_REPEAT", "AC_TEST_ROOM_UI", "AC_TEST_ROOM_ENTER", "AC_TEST_HOUSE_CLIENT_SWAP")
STARTED = []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=41810)
    ap.add_argument("--shots", action="store_true", help="screenshot mode: a REAL resident client holds the house (its menu is rendered); PNGs into roommate_shots/")
    ap.add_argument("--exe", default=W3_EXE, help="--shots: the AnimalCrossing.exe copied into the fixture (default pc\\build64_w3\\bin)")
    args = ap.parse_args()
    if not (41800 <= args.port <= 41899):
        print("REFUSING: ports 41800-41899 only", file=sys.stderr)
        return 2
    if args.shots:
        return run_shots(args)
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
        house = residents[0]  # house i belongs to resident i in this fixture
        host = rig.start_host("room_real", args.port, ["--house-sync"])
        check("host reached genuine field-ready state", host is not None)
        if host is None:
            return L.summary_and_exit_code(results)
        STARTED.append(host)
        ini = os.path.join(save_dir, "mp", "guest.ini")
        os.makedirs(os.path.dirname(ini), exist_ok=True)
        with open(ini, "wb") as f:  # (after start_host: it restores the save snapshot)
            f.write(b"# test profile\nname = Mover\ngender = 1\nface = 3\nhome_town = Farvale\nplayer_id = 0x4D31\nland_id = 24117\n")
        homes = os.path.join(L.GAME_BIN_DIR, "save", "mp", "roommate_homes.ini")
        L.resolve_host_town("127.0.0.1", args.port)

        def pump_until(fn, timeout):
            end = time.time() + timeout
            while time.time() < end:
                v = fn()
                if v:
                    return v
                L.pump_sleep(0.3) if a is not None else time.sleep(0.3)
            return fn()

        a = L.FakeClient("A", "127.0.0.1", args.port, player=L.resident_player(house))
        a.rec_resident_idx = house
        a.connect_and_ready(quiet=True)
        RP.at_gyroid(OF, a, house)
        r = RP.ask(a, RP.QUERY, house)
        check("U1 resident A at its house's gyroid -> OWNER (house %d)" % house, r is not None and r["op"] == RP.INFO and r["role"] == RP.ROLE_OWNER)

        os.environ["AC_TEST_ROOM_GYROID"] = str(house)
        os.environ["AC_TEST_GYROID_DIAG"] = "1"
        os.environ["AC_TEST_ROOM_GYROID_REPEAT"] = "1"
        os.environ["AC_TEST_ROOM_UI"] = "ask,yes"
        os.environ["AC_TEST_HOUSE_CLIENT_SWAP"] = "0,17,18"
        g = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--guest"], log_path=os.path.join(HERE, "room_real_guest.log"), bin_dir=L.GAME_BIN_DIR,
                            label="roomG").start()
        STARTED.append(g)
        req = pump_until(lambda: re.search(r"\[NET\]\[ROOM\] host: peer \d+ \"Mover\" REQUEST: guest \"Mover", host.log_text()), 300.0)
        gt = g.log_text()
        check("U2 the guest's A at the house gyroid: INFO CAN_REQUEST armed the VANILLA conversation (the move-in choice in its own menu), 'Ask to move in' + 'Yes.': the host stored the REQUEST",
              req is not None and "[ROOM][UI] A at the gyroid of house %d (a guest)" % house in gt and re.search(r"\[ROOM\]\[UI\] INFO role 2 ", gt)
              and "the gyroid of house %d talks: armed" % house in gt and "[ROOM][DLG] gyroid guest menu 0x0929: the move-in choice added" in gt
              and re.search(r"pick 1 'ask' -> choice 1 of 3 \('Ask to move in", gt) and re.search(r"pick 2 'yes' -> choice 0 of 2", gt))
        k = None
        RP.at_gyroid(OF, a, house)
        r = RP.ask(a, RP.QUERY, house)
        for e in (r["e"] if r else []):
            if e[0] == RP.ST_PENDING and e[2].rstrip(b" \x00") == b"Mover":
                k = e[3]
        check("U3 A's list shows the real guest's request", k is not None)
        if k is None:
            return L.summary_and_exit_code(results)
        r = RP.ask(a, RP.ACCEPT, house, key=k)
        check("U3 A accepts -> OK", r is not None and r["result"] == RP.R_OK)
        st = pump_until(lambda: re.search(r"\[NET\]\[ROOM\] client: STATUS: roommate of house (\d) \(the local town's house \d holder matches", g.log_text()), 30.0)
        check("U3 the real guest got STATUS roommate of house %d (holder keys match)" % house, st is not None and int(st.group(1)) == house)
        check("U3 ... the 'accepted' notice and roommate_homes.ini remembers the house",
              pump_until(lambda: "let you move in" in g.log_text() and "remembers the shared house" in g.log_text(), 30.0) and os.path.isfile(homes))
        cm = pump_until(lambda: re.search(r"OWNER_COMMIT xfer \d+ house %d APPLIED \(house seq \d+, resident ([4-9])" % house, host.log_text()), 90.0)
        check("U4 the ROOMMATE's own house sync committed its edit of house %d (TEST-ONLY swap): the host APPLIED it for the GUEST record (slot %s)" % (house, cm and cm.group(1)),
              cm is not None and "is now this guest's to edit (roommate)" in g.log_text())

        # ---- restart: the roommate arrives at the house door
        g.stop()
        L.pump_sleep(3.0)
        for k2 in ("AC_TEST_ROOM_GYROID", "AC_TEST_ROOM_GYROID_REPEAT", "AC_TEST_ROOM_UI", "AC_TEST_HOUSE_CLIENT_SWAP"):
            os.environ.pop(k2, None)
        os.environ["AC_TEST_ROOM_ENTER"] = "1"
        g = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--guest"], log_path=os.path.join(HERE, "room_real_guest2.log"), bin_dir=L.GAME_BIN_DIR,
                            label="roomG2").start()
        STARTED.append(g)
        ar = pump_until(lambda: re.search(r"arriving at the door of the house it shares as a roommate \(SCENE_FG\), house (\d)", g.log_text()), 240.0)
        check("U5 the restarted roommate ARRIVES AT THE DOOR of house %d (no train) (%s)" % (house, ar and ar.group(1)), ar is not None and int(ar.group(1)) == house)
        check("U5 ... and gets STATUS roommate again", pump_until(lambda: "client: STATUS: roommate of house %d" % house in g.log_text(), 120.0))
        en = pump_until(lambda: re.search(r"\[NET\]\[ROOM\] client: entered the shared house %d \(its own room" % house, g.log_text()), 120.0)
        check("U6 it walked into house %d as its OWN room (field_type PLAYER_ROOM: editable like the holder's)" % house, en is not None)
        arm = pump_until(lambda: re.search(r"entered own house %d: in-room commits" % house, g.log_text()), 30.0)
        check("U6 ... the in-room owner pipeline runs for it (%s)" % (arm and arm.group(0)), arm is not None)
        L.pump_sleep(4.0)
        RP.at_gyroid(OF, a, house)
        r = RP.ask(a, RP.REMOVE, house, key=k)
        check("U7 A removes the guest -> OK", r is not None and r["result"] == RP.R_OK)
        fz = pump_until(lambda: "demoted while inside house %d" % house in g.log_text(), 30.0)
        so = pump_until(lambda: re.search(r"\[NET\]\[ROOM\] client: shown out of house %d" % house, g.log_text()), 90.0)
        check("U7 the real guest was frozen (room locked) and SHOWN OUT of house %d" % house, fz and so is not None)
        fe = pump_until(lambda: re.search(r"the freeze of house %d ended \(left the house, ([^)]*)\)" % house, g.log_text()), 120.0)
        check("U7 the freeze ended once the last commit settled (%s)" % (fe and fe.group(1)), fe is not None and "settled" in fe.group(1) and "cap" not in fe.group(1))
        check("U8 host and guest alive", host.alive() and g.alive())
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


SHOT_FIXTURE = "bin_fixture4_room_shots"
SHOT_DIR = os.path.join(HERE, "roommate_shots")


def run_shots(args):
    """--shots: the move-in flow on screen. REAL host + REAL guest client + a REAL resident client holding the house (not scripted: its gyroid dialogue is drawn).
    Both clients are driven by the TEST-ONLY hooks (AC_TEST_ROOM_GYROID / AC_TEST_ROOM_UI / AC_TEST_ROOM_ENTER / autopilot); each step is captured with PrintWindow
    from that process's own window into roommate_shots/ (best effort, logged [SHOT])."""
    bin_dir = os.path.join(T.BUILD64, SHOT_FIXTURE)
    os.environ["NET_SPIKE_GAME_BIN"] = bin_dir
    T.make_fixture(SHOT_FIXTURE)
    shutil.copy2(args.exe, os.path.join(bin_dir, "AnimalCrossing.exe"))
    import net_spike_lib as L
    import test_txn_real_client as RC
    import test_menu_world_live_real as MWL  # (PrintWindow screenshot helper)
    os.environ["NET_SPIKE_GAME_BIN"] = bin_dir  # (that module points the variable at its own fixture on import)
    os.makedirs(SHOT_DIR, exist_ok=True)
    for k2 in ENV_KEYS:
        os.environ.pop(k2, None)
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    save_dir = os.path.join(bin_dir, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "room_shots_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    procs = []
    addr = "127.0.0.1:%d" % args.port

    def waitrx(proc, rx, timeout, since=0):
        rx = re.compile(rx)
        end = time.time() + timeout
        while True:
            m = rx.search(proc.log_text(), since)
            if m or time.time() > end or not proc.alive():
                if m is None:
                    print("[SHOT] timeout waiting for %r" % rx.pattern)
                return m
            time.sleep(0.05)

    def shot(proc, name, delay=0.0):
        if delay > 0:
            time.sleep(delay)
        ok = MWL.shot(proc, os.path.join(SHOT_DIR, name + ".png"))
        print("[SHOT] %s %s" % (name, "saved" if ok else "FAILED"))
        return ok

    def burst(proc, name, stop_proc, stop_rx, since, period=0.7, limit=20.0, after=0):
        """shots every `period` s until stop_rx appears in stop_proc's log (+ `after` more shots) or `limit` s"""
        end, n, extra = time.time() + limit, 0, -1
        while time.time() < end and proc.alive():
            shot(proc, "%s_%02d" % (name, n))
            n += 1
            if extra < 0 and re.search(stop_rx, stop_proc.log_text()[since:]):
                extra = after
            if extra == 0:
                break
            if extra > 0:
                extra -= 1
            time.sleep(period)

    def client(tag, extra, env):
        p = L.ClientProcess(addr, extra_args=extra, env=env, log_path=os.path.join(HERE, "room_shots_%s.log" % tag), bin_dir=bin_dir, label=tag).start()
        procs.append(p)
        return p

    try:
        residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
        house = residents[0]
        host = rig.start_host("room_shots", args.port, ["--house-sync"])
        check("host reached genuine field-ready state", host is not None)
        if host is None:
            return L.summary_and_exit_code(results)
        procs.append(host)
        ini = os.path.join(save_dir, "mp", "guest.ini")
        os.makedirs(os.path.dirname(ini), exist_ok=True)
        with open(ini, "wb") as f:
            f.write(b"# test profile\nname = Mover\ngender = 1\nface = 3\nhome_town = Farvale\nplayer_id = 0x4D31\nland_id = 24117\n")
        L.resolve_host_town("127.0.0.1", args.port)

        # ---- the guest at the house gyroid asks to move in (the vanilla guest conversation + "Ask to move in")
        g = client("guest", ["--guest"], {"AC_TEST_ROOM_GYROID": str(house), "AC_TEST_GYROID_DIAG": "1", "AC_TEST_ROOM_UI": "ask,yes",
                                          "AC_TEST_HOUSE_CLIENT_SWAP": "0,17,18"})
        m = waitrx(g, r"gyroid: warping in front of the gyroid of house", 300)
        check("S1 guest warped in front of the gyroid", m)
        if m is None:
            return L.summary_and_exit_code(results)
        shot(g, "roommate_01_guest_at_gyroid", 4.5)
        m = waitrx(g, r"\[ROOM\]\[UI\] INFO role 2 ", 60, m.end())
        check("S2 guest: the host's INFO (CAN_REQUEST) armed the vanilla gyroid conversation", m and waitrx(g, r"the gyroid of house %d talks: armed" % house, 5, m.start()))
        if m is None:
            return L.summary_and_exit_code(results)
        shot(g, "roommate_02_guest_gyroid_talks", 1.2)
        m = waitrx(g, r"pick 1 'ask' -> choice 1 of 3 \('Ask to move in", 30, m.end())
        check("S2 the gyroid's own guest menu carries 'Ask to move in' (choice 1 of 3, before Leave)", m)
        shot(g, "roommate_03_guest_menu", 0.3)
        m = waitrx(g, r"pick 2 'yes' -> choice 0 of 2", 30, m.end() if m else 0)
        check("S3 the vanilla Yes / No window asks first", m)
        shot(g, "roommate_04_guest_ask_question", 0.3)
        m = waitrx(g, r"\[ROOM\]\[UI\] RESULT op 2 -> 0", 20, m.end() if m else 0)
        check("S3 guest request answered OK (a dialogue row, no overlay)", m)
        shot(g, "roommate_05_guest_request_sent", 3.0)
        check("S3 host stored the REQUEST", waitrx(host, r"\[NET\]\[ROOM\] host: peer \d+ \"Mover\" REQUEST", 20))

        # ---- the REAL resident client that holds the house: its gyroid's menu has "Move-in requests"
        r = client("holder", ["--bootstrap-resident", str(house)], {"AC_TEST_ROOM_GYROID": str(house), "AC_TEST_ROOM_UI": "move-in,let"})
        check("S4 holder client in the field", r.boot_to_field(timeout=180.0, slot=house))
        m = waitrx(r, r"\[ROOM\]\[UI\] INFO role 1 \(1 entry\)", 150)
        check("S4 holder: INFO lists the pending request", m)
        if m is None:
            return L.summary_and_exit_code(results)
        shot(r, "roommate_06_holder_gyroid_talks", 1.2)
        m = waitrx(r, r"pick 1 'move-in' -> choice 3 of 5 \('Move-in requests", 30, m.end())
        check("S4 the gyroid's own menu carries 'Move-in requests' (choice 3 of 5, before End)", m)
        shot(r, "roommate_07_holder_menu", 0.3)
        m = waitrx(r, r"pick 2 'let' -> choice 0 of 3 \('Let them in", 30, m.end() if m else 0)
        check("S5 the request comes one at a time: 'Mover wishes to move in' Let them in. / Turn them down. / Decide later.", m)
        shot(r, "roommate_08_holder_request", 0.3)
        m = waitrx(r, r"\[ROOM\]\[UI\] RESULT op 4 -> 0", 20, m.end() if m else 0)
        check("S5 holder accepted (the host answered OK)", m)
        gm = waitrx(g, r"\[ROOM\]\[UI\] notice: .*let you move in", 10)  # (the host's NOTICE reaches the guest with the RESULT: its line shows ~5 s)
        shot(g, "roommate_11_guest_accepted_notice", 0.5)
        shot(r, "roommate_09_holder_accepted", 2.5)
        m = waitrx(r, r"the script is done: the last choice", 20, m.end() if m else 0)
        check("S5 back in the gyroid's menu afterwards (the next choice window is the menu again)", m)
        shot(r, "roommate_10_holder_menu_again", 0.3)
        check("S5 guest got the accepted notice (queued line at the top)", gm)
        check("S5 the roommate's own house sync committed an edit (TEST-ONLY swap)",
              waitrx(host, r"OWNER_COMMIT xfer \d+ house %d APPLIED" % house, 90))
        waitrx(r, r"gyroid conversation of house %d over" % house, 20)

        # ---- restart: the roommate arrives at the door, walks in
        r.stop()
        g.stop()
        time.sleep(3.0)
        g = client("guest2", ["--guest"], {"AC_TEST_ROOM_ENTER": "1"})
        m = waitrx(g, r"arriving at the door of the house it shares as a roommate", 300)
        check("S6 roommate arrives at the door", m)
        if m is None:
            return L.summary_and_exit_code(results)
        shot(g, "roommate_12_roommate_at_door", 5.0)
        m = waitrx(g, r"\[NET\]\[ROOM\] client: entered the shared house %d" % house, 90)
        check("S6 roommate entered the shared house", m)
        if m is None:
            return L.summary_and_exit_code(results)
        shot(g, "roommate_13_roommate_inside", 3.0)

        # ---- the holder removes the roommate (it is inside): "Roommates" -> "Ask to move out" -> Yes
        r = client("holder2", ["--bootstrap-resident", str(house)], {"AC_TEST_ROOM_GYROID": str(house), "AC_TEST_ROOM_UI": "roommates,ask,yes"})
        check("S7 holder client back in the field", r.boot_to_field(timeout=180.0, slot=house))
        m = waitrx(r, r"\[ROOM\]\[UI\] INFO role 1 \(1 entry\)", 150)
        check("S7 holder: INFO lists the roommate", m)
        if m is None:
            return L.summary_and_exit_code(results)
        goff = len(g.log_text())
        m = waitrx(r, r"pick 1 'roommates' -> choice 3 of 5 \('Roommates", 30, m.end())
        check("S7 the menu says 'Roommates' (no request waiting)", m)
        shot(r, "roommate_14_holder_menu_roommates", 0.3)
        m = waitrx(r, r"pick 2 'ask' -> choice 0 of 2 \('Ask to move out", 30, m.end() if m else 0)
        check("S7 'Mover resides here with you.' Ask to move out / Leave it be.", m)
        shot(r, "roommate_15_holder_roommate", 0.3)
        m = waitrx(r, r"pick 3 'yes' -> choice 0 of 2", 30, m.end() if m else 0)
        check("S7 ... then a vanilla Yes / No confirm", m)
        shot(r, "roommate_16_holder_remove_confirm", 0.3)
        m = waitrx(r, r"\[ROOM\]\[UI\] RESULT op 6 -> 0", 20, m.end() if m else 0)
        check("S7 holder removed the roommate (the host answered OK)", m)
        shot(r, "roommate_17_holder_removed", 3.0)
        m = waitrx(g, r"demoted while inside house %d" % house, 30, goff)
        check("S7 guest frozen in the room", m)
        burst(g, "roommate_18_guest_shown_out", g, r"\[NET\]\[ROOM\] client: shown out of house %d" % house, goff, period=1.0, limit=60.0, after=3)
        m = waitrx(g, r"the freeze of house %d ended" % house, 120, goff)
        check("S7 freeze ended", m)
        shot(g, "roommate_19_guest_back_in_town", 2.0)
        check("S8 host / guest / holder alive", host.alive() and g.alive() and r.alive())
    finally:
        for p in procs:
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
