#!/usr/bin/env python3
"""test_sell_basket_real.py - Nook bulk sell basket with REAL game processes: a listen host + a REAL resident client, Samsung display, volume 1.

TIER: REAL HOST + REAL CLIENT. The client's TEST-ONLY AC_TEST_SELL_BASKET hook (pc_sell_ui.c) puts a fish, an insect and a peach into pockets 0..2 and a 1,000 Bells
bag into pocket 3 (its ONE local test write, carried to the host by the D3 upload). The dresser screen's scripted pad (AC_TEST_DRESSER_GYROID + _STEPS, pc_pad.c: the real
cursor / A / B input) plays the player in the basket: down x3 to the pockets, "Add to Basket" on pockets 0, 1, 2, A on the bag (refused: greyed + notice), a pause (the
screenshot), B closes.
  R1..R3 (AC_TEST_SELL_BASKET=1): the hook opens the REAL basket screen itself (the inventory in HANIWA_ENTRUST mode + basket mode, m_submenu.c's test open); the closed
      basket's selection (the vanilla multi-item Submenu list the Nook seam reads) is sold exactly like aNSC_pc_sell_prepare / aNSC_pc_sell_step: ONE SHOP_SELL (kind 11).
      R1 staging / refusal / close (n=3, mask 0x0007) and no pocket / wallet change before APPLIED; R2 APPLIED, host value == advisory total, final pockets;
      R3 screenshot sell_basket_open.png
  R4 (AC_TEST_SELL_BASKET=2, --nook-test warp,resident, the autopilot): the REAL Nook dialogue: the hook places the player in front of Nook, "mashf" talks and takes his
      first choice ("I want to sell") -> Nook's sell menu opens the basket (the seam), the pad stages / refuses / closes, Nook quotes the count and the total
      (PC_NOOK_MSG_SELL_BASKET; screenshot sell_basket_quote.png), "mashf" answers Yes -> the Nook seam sends ONE SHOP_SELL -> APPLIED; "mashc" ends the talk.
Fixture: the disposable pc\\build64\\bin_fixture4_sell_real (fresh copy of bin_fixture4 + the exe of pc\\build64_w3). Ports 31000-31999.
Usage: python test_sell_basket_real.py [--port 31200] [--only R1,R4]
"""
import argparse
import ctypes
import os
import re
import shutil
import sys
import time

os.environ["AC_DISPLAY_NAME"] = "samsung"
os.environ["AC_MASTER_VOLUME"] = "1"
os.environ["AC_TEST_HOOKS"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_sell_real"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)
# the scripted pad (bits: 1 A, 2 right, 4 left, 8 B, 64 down; 0 = a ~150-frame pause): down x3 (basket rows 2, 3, then the pockets' top row = pocket 0),
# A A (Add to Basket) on pockets 0, 1, 2 (right between them), right + A on the money bag (refused), three pauses, B x3 (close)
STEPS = "64,64,64,1,1,2,1,1,2,1,1,2,1,0,0,0,8,8,8"


def shot_window(proc, path):
    """(= test_menu_world_live_real.shot) best effort: PrintWindow (PW_RENDERFULLCONTENT) of the process's top-level window into a PNG"""
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


def mash(c, auto, n, kind, secs):
    """one autopilot mash command (A presses; mashf = the FIRST option at every choice, mashc = the LAST), waits until it ended"""
    with open(auto, "w") as f:
        f.write("%d %s %.2f\n" % (n, kind, secs))
    end = time.monotonic() + secs + 15
    while time.monotonic() < end:
        if re.search(r"\[AUTO\] (?:done %d |mash %d end)" % (n, n), c.log_text()):
            return True
        time.sleep(0.2)
    return False


def check_staging(ck, ct, tag):
    staged = re.findall(r"\[NET\]\[SELL\] basket: pocket (\d+) item 0x([0-9A-F]{4}) -> cell (\d+)", ct)
    ck("%s pockets 0, 1, 2 were staged into cells 0, 1, 2 through the real tag 'Add to Basket' (%s)" % (tag, staged), [(int(p), int(cl)) for p, _i, cl in staged] == [(0, 0), (1, 1), (2, 2)])
    ck("%s A on the money bag (pocket 3) was refused with the notice text (greyed: never staged)" % tag,
       re.search(r"\[NET\]\[SELL\] basket: pocket 3 item 0x2100 is not sellable \(4: Bells can't be sold\)", ct) is not None and not re.search(r"basket: pocket 3 item .* -> cell", ct))


def check_final(ck, m_fin, m_host, w0, advisory, tag):
    pockets = [int(x, 16) for x in m_fin.group(1).strip(",").split(",")]
    v = int(m_host.group(1))
    ck("%s the host value %d == the client's advisory total %d; wallet %d -> %d (host line %s -> %s)" % (tag, v, advisory, w0, int(m_fin.group(2)), m_host.group(2), m_host.group(3)),
       v == advisory and int(m_host.group(2)) == w0 and int(m_fin.group(2)) == w0 + v == int(m_host.group(3)))
    ck("%s final pockets: 0..2 EMPTY (sold), 3 still the 1,000 Bells bag (%s)" % (tag, ["%04X" % x for x in pockets[:4]]),
       pockets[0] == 0 and pockets[1] == 0 and pockets[2] == 0 and pockets[3] == 0x2100)


def run_r123(args, L, ck):
    h = c = None
    try:
        h = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0"], log_path=T.log_path("sellb_real_host.log"), bin_dir=L.GAME_BIN_DIR).start()
        ok = h.wait_listening(60.0) and h.boot_to_field(timeout=120.0, slot=0)
        ck("listen host reached the field", ok)
        if not ok:
            return
        env = dict(os.environ, AC_TEST_SELL_BASKET="1", AC_TEST_DRESSER_GYROID="1", AC_TEST_DRESSER_GYROID_STEPS=STEPS)
        c = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1"], log_path=T.log_path("sellb_real_client.log"), bin_dir=L.GAME_BIN_DIR,
                            label="sellb", env=env).start()
        ok = c.boot_to_field(timeout=150.0, slot=1)
        ck("real resident client (slot 1) booted and connected", ok)
        if not ok:
            return
        m_open = c.wait_for_log(r"\[NET\]\[SELL\]\[TEST-ONLY\] basket test: the basket screen is open", 120.0)
        ck("the REAL basket screen opened (the inventory in HANIWA_ENTRUST mode, basket mode)", m_open is not None)
        if c.wait_for_log(r"gyroid pad step 14: ", 120.0) is not None:
            time.sleep(0.8)
            shot = os.path.join(HERE, "sell_basket_open.png")
            ck("R3 screenshot of the basket screen saved (%s)" % shot, shot_window(c, shot))
        m_fin = c.wait_for_log(r"\[NET\]\[SELL\]\[TEST-ONLY\] basket test: final pockets=([0-9A-F,]+) wallet=(\d+)", 120.0)
        ct, ht = c.log_text(), h.log_text()
        ck("the hook ran to its final report", m_fin is not None)
        check_staging(ck, ct, "R1")
        m_close = re.search(r"\[NET\]\[SELL\] basket screen CLOSED: 3 item\(s\) mask=0x0007 advisory total (\d+) Bells", ct)
        m_sel = re.search(r"basket test: selection n=3 mask=0x0007 advisory=(\d+) wallet=(\d+)", ct)
        ck("R1 the basket closed with n=3, mask 0x0007 (the vanilla selection the Nook seam reads)", m_close is not None and m_sel is not None)
        ck("R1 no pocket / wallet change while the basket was open or before APPLIED", "CHANGED WHILE THE BASKET WAS OPEN" not in ct and "CHANGED BEFORE APPLIED" not in ct)
        m_res = re.search(r"basket test: result (APPLIED|REJECTED) mask=0x0007 reason (\d+)", ct)
        m_sent = re.search(r"TXN_COMMIT sent kind=SHOP_SELL request=\d+ nonce=\d+ seq=\d+ dest=0 slot=0 item=0x([0-9A-F]{4})", ct)
        m_host = re.search(r"SHOP_SELL mask=0x0007 value=(\d+) wallet (\d+) -> (\d+) sales_sum now \d+ committed", ht)
        ck("R2 ONE SHOP_SELL (primary slot 0) -> APPLIED; the host committed mask 0x0007 once", m_res is not None and m_res.group(1) == "APPLIED" and m_sent is not None and m_host is not None
           and len(re.findall(r"SHOP_SELL mask=0x0007 value=", ht)) == 1)
        if m_fin and m_host and m_sel and m_close:
            check_final(ck, m_fin, m_host, int(m_sel.group(2)), int(m_close.group(1)), "R2")
        ck("no protocol violation / refusal on either side", "protocol violation" not in ct and "REJECTED(" not in ct and re.search(r"\[NET\]\[REC\] host: peer \d+ violation", ht) is None)
        ck("both processes alive", c.alive() and h.alive())
    finally:
        if c is not None:
            c.stop()
        if h is not None:
            h.stop()


def run_r4(args, L, ck):
    auto = os.path.join(L.GAME_BIN_DIR, "ac_auto.txt")
    if os.path.isfile(auto):
        os.remove(auto)
    h = c = None
    try:
        h = L.HostProcess(port=args.port + 1, extra_args=["--bootstrap-resident", "0"], log_path=T.log_path("sellb_real_r4_host.log"), bin_dir=L.GAME_BIN_DIR,
                          env=dict(os.environ, AC_TEST_CLEAR_ERRANDS="1")).start()
        ok = h.wait_listening(60.0) and h.boot_to_field(timeout=120.0, slot=0)
        ck("R4 listen host reached the field", ok)
        if not ok:
            return
        env = dict(os.environ, AC_TEST_SELL_BASKET="2", AC_TEST_DRESSER_GYROID="1", AC_TEST_DRESSER_GYROID_STEPS=STEPS, AC_TEST_AUTOPILOT="1", AC_TEST_CLEAR_ERRANDS="1")
        c = L.ClientProcess("127.0.0.1:%d" % (args.port + 1), extra_args=["--bootstrap-resident", "1", "--nook-test", "warp,resident"], log_path=T.log_path("sellb_real_r4_client.log"),
                            bin_dir=L.GAME_BIN_DIR, label="sellb4", env=env).start()
        ok = c.boot_to_field(timeout=150.0, slot=1)
        ck("R4 real resident client booted and connected", ok)
        if not ok:
            return
        ck("R4 warped into the shop and placed in front of Nook",
           c.wait_for_log(r"--nook-test: warping the guest into the shop", 120.0) is not None
           and c.wait_for_log(r"basket test \(nook\): Nook 0x[0-9A-F]{4} at .* the player placed at", 120.0) is not None)
        opened = False
        for k in range(1, 16):
            mash(c, auto, 100 + k, "mashf", 1.0)
            if re.search(r"\[NET\]\[SELL\] basket screen OPEN", c.log_text()):
                opened = True
                break
            time.sleep(0.5)
        ck("R4 Nook's first choice ('I want to sell') opened the BASKET screen through the Nook seam",
           opened and c.wait_for_log(r"Nook's sell menu opened the basket screen", 10.0) is not None)
        if not opened:
            return
        if c.wait_for_log(r"gyroid pad step 14: ", 120.0) is not None:
            time.sleep(0.8)
            ck("R4 screenshot of the basket screen opened by Nook saved", shot_window(c, os.path.join(HERE, "sell_basket_nook.png")))
        mq = c.wait_for_log(r"\[NET\]\[SELL\] nook dialogue: basket quote (\d+) item\(s\) (\d+) Bells", 90.0)
        ck("R4 the closed basket became Nook's QUOTE row (3 items, the advisory total) %s" % (mq and mq.group(0)), mq is not None and mq.group(1) == "3")
        if mq is None:
            return
        time.sleep(1.5)
        ck("R4 screenshot of Nook's quote saved", shot_window(c, os.path.join(HERE, "sell_basket_quote.png")))
        sold = False
        for k in range(1, 12):
            mash(c, auto, 200 + k, "mashf", 1.0)
            if re.search(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=SHOP_SELL", c.log_text()):
                sold = True
                break
        ck("R4 'Yes.' -> the Nook seam sent ONE SHOP_SELL and got APPLIED", sold)
        time.sleep(1.0)
        for k in range(1, 4):
            mash(c, auto, 300 + k, "mashc", 3.0)
        m_fin = c.wait_for_log(r"\[NET\]\[SELL\]\[TEST-ONLY\] basket test: final pockets=([0-9A-F,]+) wallet=(\d+)", 90.0)
        ct, ht = c.log_text(), h.log_text()
        ck("R4 the hook reported the final pockets", m_fin is not None)
        check_staging(ck, ct, "R4")
        m_open = re.search(r"basket test \(nook\): Nook's sell menu opened the basket screen \(wallet=(\d+)\)", ct)
        m_close = re.search(r"\[NET\]\[SELL\] basket screen CLOSED: 3 item\(s\) mask=0x0007 advisory total (\d+) Bells", ct)
        m_sent = re.search(r"TXN_COMMIT sent kind=SHOP_SELL request=\d+ nonce=\d+ seq=\d+ dest=0 slot=0 item=0x([0-9A-F]{4})", ct)
        m_host = re.search(r"SHOP_SELL mask=0x0007 value=(\d+) wallet (\d+) -> (\d+) sales_sum now \d+ committed", ht)
        ck("R4 ONE SHOP_SELL mask 0x0007 (primary slot 0) was committed by the host", m_close is not None and m_sent is not None and m_host is not None
           and len(re.findall(r"SHOP_SELL mask=0x[0-9A-F]{4} value=", ht)) == 1)
        ck("R4 no pocket change while the basket was open", "CHANGED WHILE THE BASKET WAS OPEN" not in ct)
        if m_fin and m_host and m_open and m_close:
            check_final(ck, m_fin, m_host, int(m_open.group(1)), int(m_close.group(1)), "R4")
        ck("R4 no protocol violation / refusal; both processes alive", "protocol violation" not in ct and "REJECTED(" not in ct and c.alive() and h.alive())
    finally:
        if os.path.isfile(auto):
            os.remove(auto)
        if c is not None:
            c.stop()
        if h is not None:
            h.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=31200)
    ap.add_argument("--only", default="R1,R4", help="R1 (= R1..R3, the hook-opened basket) and / or R4 (the real Nook dialogue)")
    args = ap.parse_args()
    if not (31000 <= args.port <= 31990):
        print("ports 31000-31999 only")
        return 2
    T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
    import net_spike_lib as L

    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    only = set(x.strip() for x in args.only.split(",") if x.strip())
    if "R1" in only:
        run_r123(args, L, ck)
    if "R4" in only:
        run_r4(args, L, ck)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
