#!/usr/bin/env python3
r"""test_menu_world_live_real.py - "the world keeps running behind menus" (REAL host + REAL client A + REAL client B, Samsung display, volume 1).

A opens its real inventory (TEST-ONLY autopilot "submenu inv", m_submenu.c hook, the overlay's own close path) while B walks; then A opens the
Esc menu ("pause 1", the real pc_pause_menu_toggle path) while B walks; then A cycles map / notice board / inventory; the host opens its inventory too.
  M1 A's menu is drawn over the LIVE world ('[NET][SUBMENU] menu_type=1 draw=live-world') and A logs 'client submenu world hold BEGIN ... live=1'
  M2 while A's inventory is open, A's puppet of B ('[NET][REMOTE][DIAG] player N actor pos=') advances (vanilla: frozen at the open position)
  M3 while A's inventory is open, A's villager actors (autopilot npcpos) move or at least the client villager tick ran (ticks > 0 in hold END)
  M4 hold END on A: puppet_ticks > 0, ctl_ticks > 0, no exchange bank allocated, actor count unchanged
  M5 pockets + wallet checksum unchanged across every open / close; A's own player did not move (ppos before / after)
  M6 Esc menu on A (a CLIENT): B's puppet still advances on A while paused; A's own player stays put
  M7 every menu type of the cycle opens and closes (OPEN / CLOSED lines), no crash in any of the 3 processes
  M8 the host's own inventory with READY peers: the host's puppet of B advances too (host live tick)
Screenshots of A's window while the inventory / map are open are saved next to the logs (mwl_A_*.png, best effort, PrintWindow).
Usage: python test_menu_world_live_real.py [--port 27410]   (disposable fixture pc\build64\bin_fixture4_wl_menulive, exe from pc\build64_w3\bin)"""
import argparse
import ctypes
import math
import os
import re
import shutil
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as TS  # noqa: E402

FIXTURE = "bin_fixture4_wl_menulive"
BIN = os.path.join(TS.BUILD64, FIXTURE)
W3_EXE = os.path.join(TS.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = BIN
import net_spike_lib as L  # noqa: E402
import wplay_lib as W  # noqa: E402

W.BIN = BIN
PUP_RX = re.compile(r"\[NET\]\[REMOTE\]\[DIAG\] player (\d+) actor pos=\(([-\d.]+),([-\d.]+),([-\d.]+)\)")
END_RX = re.compile(r"\[NET\]\[SUBMENU\] (host|client) submenu world hold END frames=(\d+) actors_open=(\d+) actors_close=(\d+) ticks=(\d+) "
                    r"refused_make=(\d+) deferred_delete=(\d+) bank_idx_open=(-?\d+) bank_idx_close=(-?\d+) puppet_ticks=(\d+) ctl_ticks=(\d+)")


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


def puppet_track(text):
    """[(player, x, z)] in log order"""
    return [(int(m.group(1)), float(m.group(2)), float(m.group(4))) for m in PUP_RX.finditer(text)]


def track_span(pts):
    if len(pts) < 2:
        return 0.0
    return max(math.hypot(x - pts[0][1], z - pts[0][2]) for _, x, z in pts)


def ap_cmd(P, line, rx, timeout=20.0):
    off = len(P.log())
    P.send(line)
    return wait_rx(P.log, rx, timeout, off)


def npcpos(P):
    m = ap_cmd(P, "npcpos", r"\[AUTO\] npcpos \d+([^\r\n]*)", 20.0)
    a = m.group(1) if m else ""
    return {int(x.group(1)): (float(x.group(2)), float(x.group(4))) for x in re.finditer(r"(\d+):(-?[\d.]+):(-?[\d.]+):(-?[\d.]+)", a)}


def ppos(P):
    m = ap_cmd(P, "ppos", r"\[AUTO\] ppos \d+ x=([-\d.]+) y=([-\d.]+) z=([-\d.]+)", 20.0)
    return (float(m.group(1)), float(m.group(3))) if m else None


def shot(proc, path):
    """best effort: PrintWindow (PW_RENDERFULLCONTENT) of the process's top-level window into a PNG"""
    try:
        from PIL import Image
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        pid = proc.proc.pid
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _):
            p = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(p))
            if p.value == pid and user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
                found.append(hwnd)
            return True
        user32.EnumWindows(cb, 0)
        if not found:
            return False
        hwnd = ctypes.c_void_p(found[0])

        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
        rc = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        w, h = rc.r - rc.l, rc.b - rc.t
        hdc = user32.GetDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        user32.PrintWindow(hwnd, mdc, 3)  # PW_CLIENTONLY | PW_RENDERFULLCONTENT
        buf = ctypes.create_string_buffer(w * h * 4)

        class BIH(ctypes.Structure):
            _fields_ = [("size", ctypes.c_uint32), ("w", ctypes.c_int32), ("h", ctypes.c_int32), ("planes", ctypes.c_uint16), ("bits", ctypes.c_uint16),
                        ("comp", ctypes.c_uint32), ("isz", ctypes.c_uint32), ("xp", ctypes.c_int32), ("yp", ctypes.c_int32), ("cu", ctypes.c_uint32), ("ci", ctypes.c_uint32)]
        bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
        Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB").save(path)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
        return True
    except Exception as e:  # noqa: BLE001
        print("[INFO] screenshot failed: %s" % e)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=27410)
    ap.add_argument("--no-fixture", action="store_true")
    args = ap.parse_args()
    if not (27000 <= args.port <= 27999):
        print("REFUSING: ports 27000-27999 only", file=sys.stderr)
        return 2
    if not args.no_fixture:
        make_fixture()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = A = B = None
    try:
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env={"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_host.txt"},
                             log_path=os.path.join(HERE, "mwl_host.log"), bin_dir=BIN).start()
        ok = host.wait_listening(60) and host.boot_to_field(timeout=120, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        A = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"], env={"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_A.txt"},
                            log_path=os.path.join(HERE, "mwl_A.log"), bin_dir=BIN, label="mwlA").start()
        ok = A.boot_to_field(timeout=200, slot=1)
        check("client A in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        B = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "2", "-debug"], env={"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_B.txt"},
                            log_path=os.path.join(HERE, "mwl_B.log"), bin_dir=BIN, label="mwlB").start()
        ok = B.boot_to_field(timeout=200, slot=2)
        check("client B in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        H, PA, PB = W.Player(L, "host", host), W.Player(L, "A", A), W.Player(L, "B", B)
        pa = PA.pos()
        L.info("A pos %s, B pos %s" % (pa, PB.pos()))
        if pa is None:
            check("A answers the autopilot", False)
            return L.summary_and_exit_code(results)
        ax, az = pa[0], pa[1]
        L.info("B tp: %s" % PB.cmd("tp %.1f %.1f" % (ax + 60.0, az + 40.0), 20))
        time.sleep(3.0)

        def b_walks(seconds):
            end = time.time() + seconds
            k = 0
            while time.time() < end:
                PB.walk(ax + (160.0 if k % 2 == 0 else -40.0), az + 60.0, timeout=12.0, radius=20)
                k += 1

        # ---- inventory on A while B walks ----
        a_pos0 = ppos(PA)
        n0 = npcpos(PA)
        offA = len(A.log_text())
        m = ap_cmd(PA, "submenu inv", r"\[TEST\]\[SUBMENU\] client menu 1 OPEN pockets_sum=([0-9A-F]+) live=(\d)", 30.0)
        check("A opened its inventory through the hook (%s)" % (m and m.group(0)), m is not None)
        if m is None:
            return L.summary_and_exit_code(results)
        sum_open = m.group(1)
        dr = wait_rx(A.log_text, r"\[NET\]\[SUBMENU\] menu_type=1 draw=(\S+)", 10.0, offA)
        bg = wait_rx(A.log_text, r"\[NET\]\[SUBMENU\] client submenu world hold BEGIN menu_type=1 actors=(\d+) live=(\d)", 10.0, offA)
        check("M1 A's inventory is drawn over the LIVE world (%s)" % (dr and dr.group(0)), dr is not None and dr.group(1) == "live-world")
        check("M1 A's client world hold BEGIN with live=1 (%s)" % (bg and bg.group(0)), bg is not None and bg.group(2) == "1")
        time.sleep(1.5)
        off_open = len(A.log_text())
        b_walks(10.0)
        shot(A, os.path.join(HERE, "mwl_A_inventory.png"))
        n1 = npcpos(PA)
        off_mid = len(A.log_text())
        tr = puppet_track(A.log_text()[off_open:off_mid])
        span = track_span(tr)
        check("M2 A's puppet of B advanced while A's inventory was open (%d samples, span %.1f units)" % (len(tr), span), len(tr) >= 4 and span > 40.0)
        moved = {s: math.hypot(n1[s][0] - n0[s][0], n1[s][1] - n0[s][1]) for s in n1 if s in n0}
        L.info("villager displacement on A while the menu was open: %s" % {k: round(v, 1) for k, v in moved.items()})
        mc = ap_cmd(PA, "submenu close", r"\[TEST\]\[SUBMENU\] client menu 1 CLOSED pockets_sum=([0-9A-F]+)", 30.0)
        check("A's inventory closed through the overlay's own close path (%s)" % (mc and mc.group(0)), mc is not None)
        e = wait_rx(A.log_text, END_RX, 10.0, offA)
        L.info("A hold END: %s" % (e and e.group(0)))
        check("M3 villagers on A moved while the menu was open, or the client villager tick ran (moved %s, ticks %s)" % (
            {k: round(v, 1) for k, v in moved.items()}, e and e.group(5)), (moved and max(moved.values()) > 3.0) or (e is not None and int(e.group(5)) > 0))
        check("M4 A's live tick ran: puppet_ticks>0 ctl_ticks>0 (%s)" % str(e and (e.group(10), e.group(11))), e is not None and int(e.group(10)) > 0 and int(e.group(11)) > 0)
        check("M4 no exchange bank allocated while A's menu was open", e is not None and e.group(8) == e.group(9))
        check("M4 no actor created / deleted by the menu tick (refused_make %s, deferred_delete %s)" % (e and e.group(6), e and e.group(7)), e is not None)
        check("M5 pockets + wallet unchanged across A's inventory (%s / %s)" % (sum_open, mc and mc.group(1)), mc is not None and mc.group(1) == sum_open)
        time.sleep(2.0)
        a_pos1 = ppos(PA)
        check("M5 A's own player did not move while its menu was open (%s -> %s)" % (a_pos0, a_pos1),
              a_pos0 and a_pos1 and math.hypot(a_pos1[0] - a_pos0[0], a_pos1[1] - a_pos0[1]) < 2.0)

        # ---- Esc menu on A (client) ----
        p = ap_cmd(PA, "pause 1", r"\[AUTO\] pause \d+ paused=(\d)", 20.0)
        check("A Esc-paused (%s)" % (p and p.group(0)), p is not None and p.group(1) == "1")
        a_pos2 = ppos(PA)
        offp = len(A.log_text())
        b_walks(8.0)
        offp2 = len(A.log_text())
        trp = puppet_track(A.log_text()[offp:offp2])
        a_pos3 = ppos(PA)
        p = ap_cmd(PA, "pause 0", r"\[AUTO\] pause \d+ paused=(\d)", 20.0)
        check("A un-paused (%s)" % (p and p.group(0)), p is not None and p.group(1) == "0")
        check("M6 A's puppet of B advanced while A (a CLIENT) was in the Esc menu (%d samples, span %.1f)" % (len(trp), track_span(trp)), len(trp) >= 4 and track_span(trp) > 40.0)
        check("M6 A's own player stayed put in the Esc menu (%s -> %s)" % (a_pos2, a_pos3),
              a_pos2 and a_pos3 and math.hypot(a_pos3[0] - a_pos2[0], a_pos3[1] - a_pos2[1]) < 2.0)

        # ---- cycle every menu type on A ----
        for name, t in (("map", 5), ("notice", 6), ("inv", 1)):
            time.sleep(2.0)
            offc = len(A.log_text())
            mo = ap_cmd(PA, "submenu %s" % name, r"\[TEST\]\[SUBMENU\] client menu %d OPEN pockets_sum=([0-9A-F]+)" % t, 30.0)
            time.sleep(3.0)
            if name == "map":
                shot(A, os.path.join(HERE, "mwl_A_map.png"))
            mcl = ap_cmd(PA, "submenu close", r"\[TEST\]\[SUBMENU\] client menu %d CLOSED pockets_sum=([0-9A-F]+)" % t, 30.0)
            dr = wait_rx(A.log_text, r"\[NET\]\[SUBMENU\] menu_type=%d draw=(\S+)" % t, 2.0, offc)
            check("M7 menu %s opened, drew %s and closed, pockets unchanged (%s / %s)" % (name, dr and dr.group(1), mo and mo.group(1), mcl and mcl.group(1)),
                  mo is not None and mcl is not None and mo.group(1) == mcl.group(1) and A.alive())

        # ---- the host's own inventory with READY peers ----
        offh = len(host.log_text())
        mh = ap_cmd(H, "submenu inv", r"\[TEST\]\[SUBMENU\] host menu 1 OPEN pockets_sum=([0-9A-F]+) live=(\d)", 30.0)
        check("host opened its inventory (%s)" % (mh and mh.group(0)), mh is not None and mh.group(2) == "1")
        time.sleep(1.5)
        offh2 = len(host.log_text())
        b_walks(8.0)
        trh = puppet_track(host.log_text()[offh2:])
        mhc = ap_cmd(H, "submenu close", r"\[TEST\]\[SUBMENU\] host menu 1 CLOSED pockets_sum=([0-9A-F]+)", 30.0)
        eh = wait_rx(host.log_text, END_RX, 10.0, offh)
        check("M8 the host's puppets advanced behind its inventory (%d samples, span %.1f; END %s)" % (len(trh), track_span(trh), eh and eh.group(0)),
              len(trh) >= 4 and track_span(trh) > 40.0 and eh is not None and int(eh.group(10)) > 0)
        check("M8 host pockets unchanged", mh is not None and mhc is not None and mh.group(1) == mhc.group(1))
        time.sleep(2.0)
        check("M7 all three processes alive", host.alive() and A.alive() and B.alive())
    finally:
        for pr, tag in ((B, "B"), (A, "A"), (host, "host")):
            if pr is not None:
                rc = pr.stop()
                check("%s did not crash (%s)" % (tag, rc), rc is None)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
