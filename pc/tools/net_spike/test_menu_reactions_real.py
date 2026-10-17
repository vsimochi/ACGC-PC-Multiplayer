#!/usr/bin/env python3
r"""test_menu_reactions_real.py - "cosmetic world reactions behind menus" (REAL host + REAL client A + REAL client B, Samsung display, volume 1).

Host = REAL with --field-action-test-seed (fruit tree at tile (56,104)). A stands south of the tree and opens its REAL inventory (TEST-ONLY
autopilot "submenu inv"); B is a real observer client. The SHAKER is a FakeClient S (resident 3) standing 25 units west of the tree, facing
east: it streams MOVE with main index 89 (SHAKE_TREE) + an entry counter (the remote-player tree-shake presentation of every other process
fires the EffectBG canopy sway at keyframe 10, exactly as for a real shaker) and sends the host its TREE_SHAKE field action (the host commits
the fruit and broadcasts TREE_DROP 77). (A real client's A press next to the tree did not shake it in the first run: no TREE_SHAKE reached the host.)
  R1 while A's inventory is open (between A's OPEN and CLOSED lines) A logs '[NET][MENU] tree reaction during menu: EffectBG animating'
     (the puppet's tree sway animates behind the menu)
  R2 while A's inventory is open A presents the host's TREE_DROP ('[NET][TREE] tree drop present ... spawned=1') AND logs
     '[NET][MENU] tree reaction during menu: item drop presentation animating' (the falling fruit moves behind the menu)
  R3 A's cosmetic END line: efbg_ticks > 0, bgitem_ticks > 0, drop_ticks > 0 and field_changed_cos=0 (the town map hash - Save fg +
     deposit - never changed inside the cosmetic tick); the ground refresh ran (redraws > 0) or nothing was pending
  R4 A's pockets + wallet unchanged across the menu, no exchange bank allocated, A's player did not move
  R5 the LISTEN HOST with its own inventory open: B shakes again -> the host logs the EffectBG tree reaction during its menu and
     field_changed_cos=0 in its END line
  R6 no crash in any of the 3 processes
Usage: python test_menu_reactions_real.py [--port 32610]   (disposable fixture pc\build64\bin_fixture4_tm_reactions, exe from pc\build64_w3\bin)"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as TS  # noqa: E402

FIXTURE = "bin_fixture4_tm_reactions"
BIN = os.path.join(TS.BUILD64, FIXTURE)
W3_EXE = os.path.join(TS.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = BIN
import net_spike_lib as L  # noqa: E402
import wplay_lib as W  # noqa: E402
from test_appearance_sync import build_appearance  # noqa: E402  (a puppet only plays state rows once its visual is initialized)

W.BIN = BIN
W.NAME = FIXTURE
TREE = (56, 104)
IDX_SHAKE_TREE = 89
EAST = 0x4000  # atans_table(dz, dx): 0x4000 = +x
SCENE_FG = 7
END_RX = re.compile(r"\[NET\]\[SUBMENU\] (host|client) submenu world hold END frames=(\d+) actors_open=(\d+) actors_close=(\d+) ticks=(\d+) "
                    r"refused_make=(\d+) deferred_delete=(\d+) bank_idx_open=(-?\d+) bank_idx_close=(-?\d+)")
COS_RX = re.compile(r"\[NET\]\[MENU\] cosmetic reactions END efbg_ticks=(\d+) efbg_active_frames=(\d+) bgitem_ticks=(\d+) drop_ticks=(\d+) "
                    r"drop_held=(\d+) redraws=(\d+) redraw_held=(\d+) field_changed_cos=(\d+)")
EFBG_RX = r"\[NET\]\[MENU\] tree reaction during menu: EffectBG animating slots=(\d+)"
DROPANIM_RX = r"\[NET\]\[MENU\] tree reaction during menu: item drop presentation animating"
TDROP_RX = re.compile(r"\[NET\]\[TREE\] tree drop present item 0x([0-9A-F]+) tree\((\d+),(\d+)\) drop\((\d+),(\d+)\) idx (\d+) spawned=(\d)")


def make_fixture():
    TS.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(BIN, "AnimalCrossing.exe"))


def wait_rx(get_text, rx, timeout, since=0):
    rx = re.compile(rx) if isinstance(rx, str) else rx
    end = time.time() + timeout
    while time.time() < end:
        m = list(rx.finditer(get_text()[since:]))
        if m:
            return m[-1]
        time.sleep(0.25)
    return None


def ap_cmd(P, line, rx, timeout=20.0):
    off = len(P.log())
    P.send(line)
    return wait_rx(P.log, rx, timeout, off)


def ppos(P):
    m = ap_cmd(P, "ppos", r"\[AUTO\] ppos \d+ x=([-\d.]+) y=([-\d.]+) z=([-\d.]+)", 20.0)
    return (float(m.group(1)), float(m.group(3))) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=32610)
    ap.add_argument("--no-fixture", action="store_true")
    args = ap.parse_args()
    if not (32500 <= args.port <= 32999):
        print("REFUSING: ports 32500-32999 only", file=sys.stderr)
        return 2
    if not args.no_fixture:
        make_fixture()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = A = B = fake = None
    try:
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "--field-action-test-seed", "-debug"],
                             env={"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_host.txt", "PC_PUPPET_DIAG": "1"}, log_path=os.path.join(HERE, "mre_host.log"), bin_dir=BIN).start()
        ok = host.wait_listening(60) and host.boot_to_field(timeout=120, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        A = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"], env={"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_A.txt", "PC_PUPPET_DIAG": "1"},
                            log_path=os.path.join(HERE, "mre_A.log"), bin_dir=BIN, label="mreA").start()
        ok = A.boot_to_field(timeout=200, slot=1)
        check("client A in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        B = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "2", "-debug"], env={"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_B.txt"},
                            log_path=os.path.join(HERE, "mre_B.log"), bin_dir=BIN, label="mreB").start()
        ok = B.boot_to_field(timeout=200, slot=2)
        check("client B in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        H, PA, PB = W.Player(L, "host", host), W.Player(L, "A", A), W.Player(L, "B", B)
        tx, _, tz = L.tile_center(*TREE)
        # the four shaker spots (25 units from the tree centre, facing it); the y of each is measured with B standing there
        # (run 3: the west spot gave budget=notree even with no menu open, and B's own real A press there shook nothing either)
        sides = []
        for ox, oz, ang in ((-25.0, 0.0, EAST), (25.0, 0.0, -0x4000), (0.0, -25.0, 0), (0.0, 25.0, -0x8000)):
            PB.cmd("tp %.1f %.1f" % (tx + ox, tz + oz), 20)
            time.sleep(1.2)
            bp = ap_cmd(PB, "ppos", r"\[AUTO\] ppos \d+ x=([-\d.]+) y=([-\d.]+) z=([-\d.]+)", 20.0)
            sides.append((tx + ox, float(bp.group(2)) if bp else 40.0, tz + oz, ang))
        L.info("shaker spots (x, ground y, z, facing): %s; B tp away: %s" % (sides, PB.cmd("tp %.1f %.1f" % (tx - 40.0, tz + 80.0), 20)))
        sx, sy, sz, sang = sides[0]
        L.info("A tp: %s" % PA.cmd("tp %.1f %.1f" % (tx, tz + 120.0), 20))
        res = {i: ident for i, ident, ex in L.read_test_save_residents(BIN) if ex}
        fake = L.FakeClient("S", "127.0.0.1", args.port, player=res[3])
        fake.connect_and_ready()
        fake.send_reliable(build_appearance(0, 1, 0x2401))
        fake.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 0, 0, 1))
        fs = {"frame": 1000, "spot": sides[0]}

        def stream(seconds, main_index=None, counter=0):
            end = time.monotonic() + seconds
            x, y, z, ang = fs["spot"]
            while time.monotonic() < end:
                fs["frame"] += 1
                fake.send_move(fs["frame"], x, y, z, angle=ang, speed=0.0, move_state=0, main_index=main_index, entry_counter=counter)
                L.pump_sleep(0.05, fake.hub)

        def fake_shake(counter, seq, drops=True):
            stream(1.4, IDX_SHAKE_TREE, counter)
            stream(0.6)
            if drops:
                fake.send_reliable(struct.pack("<BBBBIBBH64x", 29, 3, TREE[0], TREE[1], seq, 0, 0, 0))
            stream(4.0)

        stream(4.0)  # idle MOVEs: the puppets on A / the host appear and settle at the shaker spot
        # control (menu CLOSED): a shake outside any menu proves the shaker path itself (sway only, no field action); the first
        # spot whose shake A's puppet accepts (budget=ok) is used for the menu phases
        ctl_ok, ctl_all = False, []
        for k, spot in enumerate(sides):
            fs["spot"] = spot
            stream(1.5)  # settle at the new spot
            offc = len(A.log_text())
            fake_shake(7 + k, 0, drops=False)
            ctl = re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player \d+ tree shake [^\r\n]*", A.log_text()[offc:])
            ctl_all.append((spot, ctl))
            if any("effect=large" in x and "budget=ok" in x for x in ctl):
                ctl_ok = True
                break
            stream(1.5)  # past the 84-frame per-puppet cooldown
        L.info("control shakes (no menu) on A: %s" % ctl_all)
        check("R0 control: with no menu open A's puppet of S fires the canopy shake from spot %s" % (fs["spot"],), ctl_ok)
        stream(3.0)  # past the 84-frame per-puppet / per-tile cooldown

        # ---- A's inventory open while S shakes the fruit tree ----
        a_pos0 = ppos(PA)
        offA, offH = len(A.log_text()), len(host.log_text())
        m = ap_cmd(PA, "submenu inv", r"\[TEST\]\[SUBMENU\] client menu 1 OPEN pockets_sum=([0-9A-F]+) live=(\d)", 30.0)
        check("A opened its inventory through the hook (%s)" % (m and m.group(0)), m is not None and m.group(2) == "1")
        if m is None:
            return L.summary_and_exit_code(results)
        sum_open = m.group(1)
        stream(1.5)
        fake_shake(1, 32611)
        mc = ap_cmd(PA, "submenu close", r"\[TEST\]\[SUBMENU\] client menu 1 CLOSED pockets_sum=([0-9A-F]+)", 30.0)
        check("A's inventory closed through the overlay's own close path (%s)" % (mc and mc.group(0)), mc is not None)
        full = A.log_text()[offA:]
        close_at = full.find("[TEST][SUBMENU] client menu 1 CLOSED")
        during = full[:close_at] if close_at >= 0 else full
        drops = TDROP_RX.findall(during)
        efbg = re.search(EFBG_RX, during)
        L.info("A during the menu: tree drops %s, efbg %s, puppet tree lines %s, host TREE lines %s" % (
            drops, efbg and efbg.group(0), re.findall(r"\[NET\]\[PUPPET\]\[DIAG\] player \d+ tree shake [^\r\n]*", during), TDROP_RX.findall(host.log_text()[offH:])))
        check("R1 A's tree sway (EffectBG) animated WHILE its inventory was open (%s)" % (efbg and efbg.group(0)), efbg is not None)
        check("R2 A presented the host's TREE_DROP while its menu was open (%d drops, all spawned=1)" % len(drops), len(drops) >= 1 and all(d[6] == "1" for d in drops))
        check("R2 A's falling drops animated WHILE its menu was open", re.search(DROPANIM_RX, during) is not None)
        e = wait_rx(A.log_text, END_RX, 10.0, offA)
        c = wait_rx(A.log_text, COS_RX, 10.0, offA)
        L.info("A END: %s | %s" % (e and e.group(0), c and c.group(0)))
        check("R3 A cosmetic tick ran: efbg_ticks>0 efbg_active_frames>0 bgitem_ticks>0 drop_ticks>0 (%s)" % (c and c.group(0)),
              c is not None and int(c.group(1)) > 0 and int(c.group(2)) > 0 and int(c.group(3)) > 0 and int(c.group(4)) > 0)
        check("R3 the town map never changed inside A's cosmetic tick (field_changed_cos=%s)" % (c and c.group(8)), c is not None and c.group(8) == "0")
        check("R3 A's ground view refreshed from the host's tile writes during the menu (redraws=%s held=%s)" % (c and c.group(6), c and c.group(7)),
              c is not None and int(c.group(6)) > 0)
        check("R4 A's pockets + wallet unchanged (%s / %s)" % (sum_open, mc and mc.group(1)), mc is not None and mc.group(1) == sum_open)
        check("R4 no exchange bank allocated while A's menu was open", e is not None and e.group(8) == e.group(9))
        stream(2.0)
        a_pos1 = ppos(PA)
        check("R4 A's own player did not move (%s -> %s)" % (a_pos0, a_pos1),
              a_pos0 and a_pos1 and abs(a_pos1[0] - a_pos0[0]) < 2.0 and abs(a_pos1[1] - a_pos0[1]) < 2.0)

        # ---- the listen host's own inventory while S shakes again (sway only: the fruit is gone) ----
        stream(2.0)
        L.info("host tp: %s" % H.cmd("tp %.1f %.1f" % (tx + 40.0, tz + 120.0), 20))
        stream(3.0)
        offh = len(host.log_text())
        mh = ap_cmd(H, "submenu inv", r"\[TEST\]\[SUBMENU\] host menu 1 OPEN pockets_sum=([0-9A-F]+) live=(\d)", 30.0)
        check("host opened its inventory (%s)" % (mh and mh.group(0)), mh is not None and mh.group(2) == "1")
        stream(1.5)
        fake_shake(2, 32612, drops=False)
        mhc = ap_cmd(H, "submenu close", r"\[TEST\]\[SUBMENU\] host menu 1 CLOSED pockets_sum=([0-9A-F]+)", 30.0)
        ht = host.log_text()[offh:]
        hclose = ht.find("[TEST][SUBMENU] host menu 1 CLOSED")
        hefbg = re.search(EFBG_RX, ht[:hclose] if hclose >= 0 else ht)
        hc = wait_rx(host.log_text, COS_RX, 10.0, offh)
        L.info("host END: %s" % (hc and hc.group(0)))
        check("R5 the host's tree sway animated WHILE its own inventory was open (%s)" % (hefbg and hefbg.group(0)), hefbg is not None)
        check("R5 the host's cosmetic tick never changed the town map (%s)" % (hc and hc.group(0)), hc is not None and hc.group(8) == "0" and int(hc.group(3)) > 0)
        check("R5 host pockets unchanged", mh is not None and mhc is not None and mh.group(1) == mhc.group(1))
        stream(2.0)
        check("R6 all three processes alive", host.alive() and A.alive() and B.alive())
    finally:
        try:
            fake and fake.is_connected() and fake.disconnect()
        except Exception:  # noqa: BLE001
            pass
        for pr, tag in ((B, "B"), (A, "A"), (host, "host")):
            if pr is not None:
                rc = pr.stop()
                check("%s did not crash (%s)" % (tag, rc), rc is None)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
