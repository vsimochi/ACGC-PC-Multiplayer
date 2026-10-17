#!/usr/bin/env python3
"""test_ofurn_real.py - OUTDOOR FURNITURE (C2: multi-tile + grab / push / pull / rotate), REAL processes: one REAL listen host + one REAL client game process.

TIER: REAL HOST + REAL CLIENT (no GUI automation of the menus: the client drives the REAL wait-state seam pc_ofurn_front_press and the REAL vanilla player states
HOLD / PULL / PUSH / ROTATE_FURNITURE through the TEST-ONLY AC_TEST_OFURN=grab hook, whose synthetic A button / stick replaces the pad only inside the outdoor grab
driver; the host places a 2x1 DRESSER next to the client through AC_TEST_OFURN_SEED + _SEED_NEAR), on a DISPOSABLE copy
NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_of2_<name>> (ports 38000-38999). The host runs PC_PUPPET_DIAG=1: it is the OBSERVER of the client's
player (its puppet rows). Optional --drawcycle: both processes also build / draw / tear down the model of EVERY furniture id (the C1 crash check).
  G1 the host placed a 2x1 dresser (0x1064, a storage piece) next to the real client; the client adopted the C2 CAPS (message 102) and the piece
  G2 the client built the 2x1 MODEL (fp 1) and registered its COLLISION box
  G3 A in front of the piece GRABS it: the real HOLD state (main index 19)
  G4 stick away = PULL: the request went out, the host APPLIED the MOVE, the client saw APPLIED; stick towards = PUSH: the same
  G5 stick sideways = ROTATE (LTURN / RTURN): at least one rotation APPLIED (or refused locally with the "won't budge" puff when the swing cell is taken)
  G6 the OBSERVER (host) saw the client's puppet in HOLD 19, PULL 21 and PUSH 20 and slid its model of the moved piece
  G7 the grab released (A up), then B picks the piece up: the host APPLIED the PICKUP, the client released the model
  G8 host and client alive
NOT asserted: what the models / animations look like on screen (no image comparison).
"""
import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402
import test_txn_real_client as RC  # noqa: E402

ITEM = 0x1064  # iam_sum_blue_lowchest01: a 2x1 DRESSER (aFTR_SHAPE_TYPEB_0, storage interaction)
SCRIPT = "grab,pull,push,left,right,release"
STARTED = []
ENV_KEYS = ("AC_TEST_OFURN_SEED", "AC_TEST_OFURN_SEED_NEAR", "AC_TEST_OFURN_DRAWCYCLE", "AC_TEST_OFURN", "AC_TEST_OFURN_GRAB", "PC_PUPPET_DIAG", "AC_MP_DRESSER_STORAGE",
            "AC_TEST_MUSEUM_POCKETS")


def _run(args, rig):
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    os.environ["AC_TEST_OFURN_SEED"] = "0x%04X,0" % ITEM
    os.environ["AC_TEST_OFURN_SEED_NEAR"] = "1"
    os.environ["PC_PUPPET_DIAG"] = "1"
    if args.drawcycle:
        os.environ["AC_TEST_OFURN_DRAWCYCLE"] = "1"
    os.environ.pop("AC_TEST_OFURN", None)
    host = rig.start_host("of2real", args.port, [])
    check("host reached genuine field-ready state", host is not None)
    if host is None:
        return
    STARTED.append(host)
    L.resolve_host_town("127.0.0.1", args.port)
    os.environ.pop("PC_PUPPET_DIAG", None)
    os.environ["AC_TEST_OFURN"] = "grab"
    os.environ["AC_TEST_OFURN_GRAB"] = SCRIPT
    a = rig.start_client("of2realA", args.port, residents[0], [])
    check("real client booted into the field and connected", a is not None)
    if a is None:
        return
    STARTED.append(a)
    m = host.wait_for_log(r"seeded placed piece at \((\d+),(\d+)\) item 0x%04X rot 0 -> placed \(next to peer (\d+)" % ITEM, 120.0)
    check("G1 the host placed a 2x1 dresser next to the real client", m is not None)
    if m is None:
        return
    ux, uz, peer = int(m.group(1)), int(m.group(2)), int(m.group(3))
    lst = a.wait_for_log(r"\[NET\]\[OFURN\] client: (?:C2 list of every placed piece: \d+ piece|C2 SET tile \(%d,%d\))" % (ux, uz), 60.0)
    at = a.log_text()
    check("G1 the client adopted the C2 CAPS (message 102: multi-tile + move) and got the piece in its 102 list / a SET",
          "[NET][OFURN] client: the host supports outdoor furniture C2: multi-tile ON, move (grab / push / pull) ON" in at and lst is not None)
    built = a.wait_for_log(r"\[OFURN\]\[DRAW\] model built: tile \(%d,%d\) item 0x%04X rot 0 shape 3 fp 1" % (ux, uz, ITEM), 60.0)
    check("G2 the client's renderer built the 2x1 MODEL of the dresser (shape TYPEB_0, footprint 2x1)", built is not None)
    col = a.wait_for_log(r"\[OFURN\]\[COL\] collision registered: tile \(%d,%d\) item 0x%04X type \d+ fp 1" % (ux, uz, ITEM), 30.0)
    check("G2 ... and registered its COLLISION box (the 2x1 move-bg type)", col is not None)
    gr = a.wait_for_log(r"\[OFURN\]\[GRAB\] grabbed the piece at \(%d,%d\) item 0x%04X fp 1" % (ux, uz, ITEM), 120.0)
    check("G3 A in front of the piece grabbed it (the real wait-state seam)", gr is not None)
    hs = a.wait_for_log(r"grab: step 1 of .*\(player state 19, grab active 1", 30.0)
    check("G3 the player is in the vanilla HOLD state (main index 19) while A is held", hs is not None)
    pl = a.wait_for_log(r"\[OFURN\]\[GRAB\] PULL dir 0: \(%d,%d\) -> \(%d,%d\) \(request sent" % (ux, uz, ux, uz + 1), 40.0)
    hm1 = host.wait_for_log(r"MOVE APPLIED \[TXN\]: tile \(%d,%d\) item 0x%04X rot 4" % (ux, uz, ITEM), 30.0)
    cm1 = a.wait_for_log(r"\[OFURN\]\[GRAB\] MOVE APPLIED: \(%d,%d\) rot 0 -> \(%d,%d\)" % (ux, uz, ux, uz + 1), 30.0)
    check("G4 stick AWAY = PULL: request sent, the host APPLIED the MOVE (dir S | PULL), the client saw APPLIED", pl is not None and hm1 is not None and cm1 is not None)
    ps = a.wait_for_log(r"\[OFURN\]\[GRAB\] PUSH dir 2: \(%d,%d\) -> \(%d,%d\) \(request sent" % (ux, uz + 1, ux, uz), 40.0)
    hm2 = host.wait_for_log(r"MOVE APPLIED \[TXN\]: tile \(%d,%d\) item 0x%04X rot 2" % (ux, uz + 1, ITEM), 30.0)
    check("G4 stick TOWARDS = PUSH: request sent, the host APPLIED the MOVE (dir N)", ps is not None and hm2 is not None)
    done = a.wait_for_log(r"grab: script done .*; B -> (\d)", 60.0)
    at = a.log_text()
    ht = host.log_text()
    rot_ok = len(re.findall(r"ROTATE APPLIED \[TXN\]: tile \(%d,%d\)" % (ux, uz), ht))
    rot_blocked = len(re.findall(r"\[OFURN\]\[GRAB\] ROTATE: blocked locally", at))
    check("G5 stick SIDEWAYS = ROTATE: %d rotation(s) APPLIED by the host, %d refused locally (swing cell taken / the player's own cell)" % (rot_ok, rot_blocked),
          rot_ok >= 1 or rot_blocked >= 1)
    pd = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player %d state main_index=(\d+)" % peer, ht)
    check("G6 the OBSERVER (host) saw the client's puppet in HOLD 19, PULL 21 and PUSH 20 (main indexes %s)" % sorted(set(pd)),
          "19" in pd and "20" in pd and "21" in pd)
    if re.search(r"\[OFURN\]\[DRAW\] model built: tile \(%d,%d\)" % (ux, uz), ht):
        check("G6 the observer (whose renderer had the piece in range) slid its own model of the moved piece (MOVE -> slide)", "(slides from its old pose)" in ht)
    else:
        print("NOTE - G6b not asserted: the host player stood too far away to draw the piece (renderer range 24 tiles)")
    check("G7 the grab was released (A up)", "[OFURN][GRAB] released the piece" in at)
    pk = host.wait_for_log(r"PICKUP APPLIED \[TXN\]: tile \(%d,%d\) item 0x%04X" % (ux, uz, ITEM), 30.0)
    rel = a.wait_for_log(r"\[OFURN\]\[DRAW\] model released: tile \(%d,%d\)" % (ux, uz), 30.0)
    check("G7 B in front of it (not grabbing): the host APPLIED the PICKUP of the 2x1 and the client released the model", done is not None and pk is not None and rel is not None)
    if args.drawcycle:
        dc = a.wait_for_log(r"\[OFURN\]\[DRAWTEST\]\[TEST-ONLY\] done: (\d+) furniture model", 180.0)
        check("DC the model draw cycle finished on the client", dc is not None)
    check("G8 host and client alive", host.alive() and a.alive())


def _run_storage(args, rig):
    """C3 (--storage): the outdoor STORAGE of a placed dresser + the place / pickup presentation, REAL host (observer) + REAL client (AC_TEST_OFURN=storage):
      S1 the host seeded the 2x1 dresser next to the client; the client's renderer BIRTHED it (the 102 SET -> '[NET][OFURN] place anim contact')
      S2 the client announced outdoor storage from the 102 CAPS; an A TAP (the real wait-state seam + HOLD) opened the 27-slot screen ('OPENED after N ms')
      S3 a PUT through the screen's own call: the host APPLIED the outdoor kind 18; the screen closed (scripted B)
      S4 a PUSH north: the host re-keyed the storage; the A tap at the new origin reopened it with the item (the contents followed the piece)
      S5 B while it holds the item: REFUSED 62 (OFURN_NOT_EMPTY), the piece came back
      S6 the item taken out; B again: the PICKUP_FURNITURE state (32) on the client, the host APPLIED the pickup, the client's shrink ended ('pickup anim contact'),
         the OBSERVER (host) saw the client's puppet in main index 32
      S7 the dresser placed again from the pocket: the local leaf flight + the birth after the host's SET
      S8 host and client alive"""
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    os.environ["AC_TEST_OFURN_SEED"] = "0x%04X,0" % ITEM
    os.environ["AC_TEST_OFURN_SEED_NEAR"] = "1"
    os.environ["AC_MP_DRESSER_STORAGE"] = "1"  # pc/build64_w3's cache still says PC_DRESSER_STORAGE_DEFAULT_ON=OFF
    os.environ["PC_PUPPET_DIAG"] = "1"
    os.environ.pop("AC_TEST_OFURN", None)
    host = rig.start_host("of3real", args.port, [])
    check("host reached genuine field-ready state", host is not None)
    if host is None:
        return
    STARTED.append(host)
    L.resolve_host_town("127.0.0.1", args.port)
    os.environ.pop("PC_PUPPET_DIAG", None)
    os.environ["AC_TEST_OFURN"] = "storage"
    a = rig.start_client("of3realA", args.port, residents[0], [])
    check("real client booted into the field and connected", a is not None)
    if a is None:
        return
    STARTED.append(a)
    m = host.wait_for_log(r"seeded placed piece at \((\d+),(\d+)\) item 0x%04X rot 0 -> placed \(next to peer (\d+)" % ITEM, 120.0)
    check("S1 the host placed a 2x1 dresser next to the real client", m is not None)
    if m is None:
        return
    ux, uz, peer = int(m.group(1)), int(m.group(2)), int(m.group(3))
    b1 = a.wait_for_log(r"\[NET\]\[OFURN\] place anim contact: tile \(%d,%d\) item 0x%04X" % (ux, uz, ITEM), 90.0)
    check("S1 the client's renderer showed the vanilla BIRTH of the newly placed piece (place anim contact)", b1 is not None)
    c3 = a.wait_for_log(r"the host supports outdoor furniture C3: outdoor storage ON", 30.0)
    bk = a.wait_for_log(r"storage: BACK tap result: the screen is (\w+)", 120.0)
    check("S2b C3b front rule: the A TAP from BEHIND the piece was a short grab and opened nothing (no outdoor SELECT sent)",
          bk is not None and bk.group(1) == "closed" and "is not in front of it -- no open" in a.log_text() and "A tapped (not in front of the piece: no open)" in a.log_text())
    op = a.wait_for_log(r"\[OFURN\]\[STORAGE\] outdoor storage of \(%d,%d\) OPENED after (\d+) ms" % (ux, uz), 120.0)
    check("S2 outdoor storage announced (102 CAPS) and the A TAP from the FRONT opened the 27-slot screen", c3 is not None and op is not None)
    hp = host.wait_for_log(r"\[NET\]\[DRESSER\] host: peer %d resident \d+ PUT item=0x[0-9A-F]{4} pocket=\d+ pool slot=\d+" % peer, 60.0)
    cl = a.wait_for_log(r"\[NET\]\[OFURN\]\[TEST-ONLY\] storage: the screen closed", 60.0)
    check("S3 a PUT through the screen: the host APPLIED the outdoor kind 18; the screen closed (B)", hp is not None and cl is not None)
    rk = host.wait_for_log(r"outdoor storage of ftr 0x%04X re-keyed \(%d,%d\) -> \(%d,%d\) with the piece \(1 item" % (ITEM, ux, uz, ux, uz - 1), 60.0)
    ro = a.wait_for_log(r"storage: screen open at \(%d,%d\) with 1 item\(s\)" % (ux, uz - 1), 90.0)
    check("S4 PUSH north: the host re-keyed the storage; the reopened screen at the new origin holds the item", rk is not None and ro is not None)
    ne = a.wait_for_log(r"storage: the non-empty pickup: outcome 2 reason 62", 90.0)
    check("S5 B while it holds the item: REFUSED 62 OFURN_NOT_EMPTY (and the piece came back)", ne is not None and "[OFURN][PICK] pickup of" in a.log_text())
    pk = host.wait_for_log(r"PICKUP APPLIED \[TXN\]: tile \(%d,%d\) item 0x%04X" % (ux, uz - 1, ITEM), 150.0)
    ct = a.wait_for_log(r"\[NET\]\[OFURN\] pickup anim contact: tile \(%d,%d\)" % (ux, uz - 1), 30.0)
    st32 = "PICKUP_FURNITURE (32) requested" in a.log_text()
    L.pump_sleep(2.0)
    pd = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player %d state main_index=(\d+)" % peer, host.log_text())
    check("S6 the empty pickup: state 32 on the client, the host APPLIED it, the shrink ended (pickup anim contact)", pk is not None and ct is not None and st32)
    check("S6 the OBSERVER (host) saw the client's puppet in PICKUP_FURNITURE 32 (main indexes %s)" % sorted(set(pd)), "32" in pd)
    lf = a.wait_for_log(r"\[NET\]\[OFURN\] place anim: the leaf of 0x%04X flies to \(%d,%d\)" % (ITEM, ux, uz - 1), 60.0)
    b2 = a.wait_for_log(r"\[NET\]\[OFURN\] place anim contact: tile \(%d,%d\) item 0x%04X" % (ux, uz - 1, ITEM), 30.0)
    check("S7 the dresser placed again from the pocket: the leaf flight, then the birth once the host's SET is in", lf is not None and b2 is not None)
    check("S8 host and client alive", host.alive() and a.alive())


GUEST_FISH = 0x2300  # a fish (storable in a dresser) the TEST-ONLY AC_TEST_MUSEUM_POCKETS hook puts into the guest's pocket


def _run_storage_guest(args, rig):
    """C3b (--storage-guest): a REAL `--guest` client opens the outdoor storage of a placed dresser (AC_TEST_OFURN=storage_guest):
      U1 the host seeded the 2x1 dresser next to the guest
      U2 the A press from BEHIND opened nothing (the front rule; a guest gets the C1 notice), the A press from the FRONT opened the 27-slot screen
      U3 the guest PUT its fish (host: outdoor kind 18 APPLIED for the guest slot), reopened it with the item, TOOK it back (APPLIED)
      U4 B on the piece is refused for the guest (no pickup right), the piece stays; host and client alive"""
    check = rig.check
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    ini = os.path.join(save_dir, "mp", "guest.ini")
    os.makedirs(os.path.dirname(ini), exist_ok=True)
    with open(ini, "wb") as f:
        f.write(b"# test profile\nname = Wendel\ngender = 0\nface = 2\nhome_town = Farvale\nplayer_id = 0x4D29\nland_id = 24113\n")
    os.environ["AC_TEST_OFURN_SEED"] = "0x%04X,0" % ITEM
    os.environ["AC_TEST_OFURN_SEED_NEAR"] = "1"
    os.environ["AC_MP_DRESSER_STORAGE"] = "1"  # pc/build64_w3's cache still says PC_DRESSER_STORAGE_DEFAULT_ON=OFF
    os.environ.pop("AC_TEST_OFURN", None)
    host = rig.start_host("of3guest", args.port, [])
    check("host reached genuine field-ready state", host is not None)
    if host is None:
        return
    STARTED.append(host)
    L.resolve_host_town("127.0.0.1", args.port)
    os.environ["AC_TEST_OFURN"] = "storage_guest"
    os.environ["AC_TEST_MUSEUM_POCKETS"] = "%04X" % GUEST_FISH
    g = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--guest"], log_path=os.path.join(HERE, "txn_real_of3guestG.log"), bin_dir=L.GAME_BIN_DIR,
                        label="of3guestG").start()
    STARTED.append(g)
    m = host.wait_for_log(r"seeded placed piece at \((\d+),(\d+)\) item 0x%04X rot 0 -> placed \(next to peer (\d+)" % ITEM, 300.0)
    check("U1 the host placed a 2x1 dresser next to the real GUEST client", m is not None)
    if m is None:
        return
    ux, uz, peer = int(m.group(1)), int(m.group(2)), int(m.group(3))
    bk = g.wait_for_log(r"storage: BACK tap result: the screen is (\w+)", 180.0)
    gt = g.log_text()
    check("U2 the guest's A from BEHIND opened nothing (front rule) and showed the residents-only notice instead of a grab",
          bk is not None and bk.group(1) == "closed" and "is not in front of it -- no open" in gt and "[OFURN][GRAB] grabbed" not in gt)
    op = g.wait_for_log(r"\[OFURN\]\[STORAGE\] outdoor storage of \(%d,%d\) OPENED after (\d+) ms" % (ux, uz), 120.0)
    hs = host.wait_for_log(r"\[NET\]\[DRESSER\] host: peer %d resident (\d+) selected the OUTDOOR storage at tile %d,%d" % (peer, ux, uz), 10.0)
    check("U2 the guest's A from the FRONT opened the 27-slot screen (host: the guest slot selected the outdoor storage)", op is not None and hs is not None and int(hs.group(1)) >= 4)
    hp = host.wait_for_log(r"\[NET\]\[DRESSER\] host: peer %d resident \d+ PUT item=0x%04X pocket=\d+ pool slot=\d+ .*committed \[TXN\]" % (peer, GUEST_FISH), 90.0)
    ro = g.wait_for_log(r"storage: screen open at \(%d,%d\) with 1 item\(s\)" % (ux, uz), 90.0)
    ht = host.wait_for_log(r"\[NET\]\[DRESSER\] host: peer %d resident \d+ TAKE item=0x%04X pocket=\d+ pool slot=\d+ .*committed \[TXN\]" % (peer, GUEST_FISH), 120.0)
    check("U3 the guest PUT its fish (APPLIED), the reopened screen showed it, and it TOOK it back (APPLIED)", hp is not None and ro is not None and ht is not None)
    dn = g.wait_for_log(r"storage \(guest\): done", 120.0)
    gt = g.log_text()
    check("U4 B on the piece is refused locally for a guest (no pickup right): no PICKUP reached the host; the piece is still placed",
          dn is not None and "PICKUP APPLIED [TXN]: tile (%d,%d)" % (ux, uz) not in host.log_text() and "a guest has no house" not in host.log_text())
    _ = gt
    check("U5 host and guest client alive", host.alive() and g.alive())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=39300)
    ap.add_argument("--drawcycle", action="store_true")
    ap.add_argument("--storage", action="store_true", help="C3: outdoor storage + the place / pickup presentation")
    ap.add_argument("--storage-guest", action="store_true", help="C3b: a real --guest client opens / uses the outdoor storage (front rule too)")
    args = ap.parse_args()
    if not (39000 <= args.port <= 39499):
        print("ports 39000-39499 only", file=sys.stderr)
        return 2
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_of"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_of2_<name> copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "ofurn_real_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        (_run_storage_guest if args.storage_guest else _run_storage if args.storage else _run)(args, rig)
    finally:
        for p in STARTED:  # only the processes THIS test started
            try:
                if p.alive():
                    p.stop()
            except Exception:  # noqa: BLE001
                pass
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
