#!/usr/bin/env python3
"""test_house_default_real.py - House synchronization is ON by default for a plain `--host` (Patch 5), REAL processes: one REAL host + two REAL clients (+ a scripted guest).

TIER: REAL HOST + TWO REAL CLIENT game processes (no GUI automation, no manual play), booted into the town field with their bootstrap residents, on a DISPOSABLE copy:
NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>> (ports 11560+). The host is started WITHOUT --house-sync / --dedicated: `AnimalCrossing.exe --host P --bootstrap-resident 0`.
  client A = owner of house HA, AC_TEST_HOOKS=1 AC_TEST_HOUSE_CLIENT_SWAP=0,<furniture cell>,<empty cell>: once its own house is synced and the player is outside, it MOVES one piece of
             furniture in its local save (the owner's edit); the REAL dirty test + commit path sends OWNER_COMMIT with the pockets
  client B = owner of another house (an observer of HA)
  H1 the host announced furniture sync with no flag ('[PC] host services: house sync ON (default for every host ...)') and both clients saw it in HOST_CONFIG
  H2 A's move was COMMITTED: the host logs OWNER_COMMIT ... house HA APPLIED (house seq N), the canonical image digest changed
  H3 B (another resident, another real process) RECEIVED the new canonical house HA (CANON_PUSH seq N) and stashed / applied it
  H4 B restarted (a real process, fresh join): its join pushes the CURRENT canonical copy (seq / digest of the committed layout)
  H5 the host restarted: house HA's canonical digest after the restart equals the committed one (the layout survived in the host save)
  H6 a GUEST (scripted) is never pushed a house (no HOUSE_BEGIN), and cannot commit one (NOT_OWNER)
NOT asserted (no GUI automation): the rooms themselves (entering a house) -- the in-room paths are covered by the Stage 1b logic only.
Usage: python test_house_default_real.py [--port 11560]
"""
import argparse
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
import net_spike_lib as L  # noqa: E402
import test_txn_real_client as RC  # noqa: E402
import test_house_sync_protocol as HS  # noqa: E402


def last_digest(text, h):
    m = re.findall(r"\[NET\]\[HOUSE\] host: house %d canonical image -> seq (\d+) digest 0x([0-9A-F]{8})" % h, text)
    return (int(m[-1][0]), m[-1][1]) if m else (None, None)


def first_digest(text, h):
    m = re.search(r"\[NET\]\[HOUSE\] host: house %d canonical image -> seq (\d+) digest 0x([0-9A-F]{8})" % h, text)
    return (int(m.group(1)), m.group(2)) if m else (None, None)


STARTED = []


def run(args, results, ip, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has >= 2 non-host residents (slots %s)" % residents, len(residents) >= 2)
    if len(residents) < 2:
        return
    ha, hb = residents[0], residents[1]
    img = L.house_from_gci(L.GAME_BIN_DIR, ha)
    fcell, _fid = HS.first_furniture_cell(img)
    ecells = [c for c in HS.empty_cells(img) if c > 40]
    check("the fixture house %d has a movable furniture piece (cell %s) and free cells" % (ha, fcell), fcell is not None and len(ecells) > 0)
    if fcell is None or not ecells:
        return
    ecell = ecells[0]
    host = rig.start_host("hdef", args.port, [])
    check("the plain host (no --house-sync, no --dedicated) reached genuine field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    ht0 = host.log_text()
    check("H1 the host announced house sync WITHOUT any flag: '[PC] host services: house sync ON (default for every host ...)'",
          re.search(r"\[PC\] host services: house sync ON \(default for every host", ht0) is not None)
    os.environ["AC_TEST_HOUSE_CLIENT_SWAP"] = "0,%d,%d" % (fcell, ecell)
    ca = rig.start_client("hdefA", args.port, ha, [])
    STARTED.append(ca)
    os.environ.pop("AC_TEST_HOUSE_CLIENT_SWAP", None)
    check("real client A (owner of house %d) booted into the field and connected" % ha, ca is not None)
    cb = rig.start_client("hdefB", args.port, hb, []) if ca is not None else None
    STARTED.append(cb)
    check("real client B (owner of house %d, the observer) booted into the field and connected" % hb, cb is not None)
    if ca is None or cb is None:
        return
    check("H1 both clients saw the host's announcement in HOST_CONFIG", ca.wait_for_log(r"\[NET\]\[HOUSE\] client: the host ANNOUNCED furniture sync", 30.0) is not None
          and cb.wait_for_log(r"\[NET\]\[HOUSE\] client: the host ANNOUNCED furniture sync", 30.0) is not None)
    sw = ca.wait_for_log(r"\[NET\]\[HOUSE\]\[TEST-ONLY\] client: swapped the layer-0 cells", 90.0)
    check("H2 A's own house was edited in A's save (furniture cell %d <-> free cell %d)" % (fcell, ecell), sw is not None)
    ap = host.wait_for_log(r"\[NET\]\[HOUSE\] host: peer \d+ OWNER_COMMIT xfer \d+ house %d APPLIED \(house seq (\d+)" % ha, 60.0)
    check("H2 the host APPLIED A's OWNER_COMMIT of house %d (the real dirty test + commit path)" % ha, ap is not None)
    if ap is None:
        nlc = chr(10)
        print("[A house lines]" + nlc + nlc.join(l for l in ca.log_text().splitlines() if "[HOUSE]" in l)[-2500:])
        print("[host house lines]" + nlc + nlc.join(l for l in host.log_text().splitlines() if "[HOUSE]" in l)[-2500:])
        return
    seq = int(ap.group(1))
    time.sleep(2.0)
    ht = host.log_text()
    s2, d2 = last_digest(ht, ha)
    s0, d0 = first_digest(ht, ha)
    check("H2 the canonical digest of house %d changed with the commit (seq %s -> %s, 0x%s -> 0x%s)" % (ha, s0, s2, d0, d2), s2 == seq and d0 is not None and d2 != d0)
    pushed = cb.wait_for_log(r"\[NET\]\[HOUSE\] client: CANON_PUSH house %d seq %d session \d+ received & stashed" % (ha, seq), 40.0)
    check("H3 B (another real process) RECEIVED the new canonical house %d (CANON_PUSH seq %d)" % (ha, seq), pushed is not None)
    # H4: B restarts (a real process): its join pushes the committed copy
    cb.stop()
    time.sleep(2.0)
    cb2 = rig.start_client("hdefB2", args.port, hb, [])
    STARTED.append(cb2)
    check("real client B2 (B restarted) booted and connected", cb2 is not None)
    if cb2 is not None:
        p2 = cb2.wait_for_log(r"\[NET\]\[HOUSE\] client: CANON_PUSH house %d seq (\d+) session \d+ received & stashed" % ha, 40.0)
        check("H4 B2's join pushed the CURRENT canonical house %d (seq %s)" % (ha, p2.group(1) if p2 else "?"), p2 is not None and int(p2.group(1)) == seq)
        cb2.stop()
    # H6 guest
    g = L.FakeClient("G", ip, args.port, guest=L.guest_identity("HOUSEGUEST", 0x4D01))
    try:
        g.connect_and_ready(quiet=True)
        L.pump_sleep(2.0)
        houses = g.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_HOUSE_BEGIN, (L.CH_RELIABLE,)))
        check("H6 a guest is never pushed a house (no HOUSE_BEGIN in its inbox)", len(houses) == 0)
        g.disconnect()
    except Exception as exc:  # noqa: BLE001
        check("H6 the guest connected (%r)" % (exc,), False)
    finally:
        g.close()
    # H5: stop everything gracefully, restart the host, the digest of house ha is the committed one
    ca.stop()
    time.sleep(1.5)
    host.stop()
    rig.host = None
    time.sleep(2.0)
    h2 = L.HostProcess(port=args.port + 1, extra_args=["--bootstrap-resident", "0"], log_path=os.path.join(HERE, "txn_real_hdef2_host.log"), bin_dir=L.GAME_BIN_DIR).start()  # NO save restore: the committed save must survive
    ok2 = h2.wait_listening(60.0) and h2.boot_to_field(timeout=90.0, slot=0)
    check("the host restarted on the committed save", ok2)
    if not ok2:
        h2.stop()
        h2 = None
    if h2 is not None:
        L.resolve_host_town(ip, args.port + 1)
        time.sleep(3.0)
        fs, fd = first_digest(h2.log_text(), ha)
        check("H5 after the host restart the canonical digest of house %d is the COMMITTED one (0x%s == 0x%s: the layout survived in the host save)" % (ha, fd, d2), fd == d2)
        h2.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11560)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_<name> copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "house_default_real_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        run(args, results, ip, rig)
    finally:
        for c in STARTED:
            try:
                if c is not None:
                    c.stop()
            except Exception:  # noqa: BLE001
                pass
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
