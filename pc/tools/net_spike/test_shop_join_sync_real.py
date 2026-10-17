#!/usr/bin/env python3
"""test_shop_join_sync_real.py - "Shop building on join": a client whose LOCAL save has the OLD shop joins a host whose shop was upgraded.

User report: the guest's and the resident's Nook's shop are not synced (different buildings) on a dedicated server that went through the announced shop upgrade.
Root cause (docs/multiplayer-guest-roadmap.md "Shop building on join"): a client joins with its OWN save copy (structure tile SHOP0 = Nook's Cranny, Shop_c level 0);
its town scene and the shop structure actor are built from that stale tile before the snapshot lands. (a) the dedicated host's observer stands next to the shop at the
shadow init, its live shop actor holds DUMMY_SHOPn / RSV_NO on the structure tile and the footprint -> those tiles were "unknown" (valid=0) in every join snapshot;
(b) when the host value does arrive (a later FIELD_UPDATE / a host that knows it) it is PARKED behind the client's live structure actor placeholder, so the old
building stays until the shop acre unloads. Which client shows which building depended on whether its shop acre was loaded at that moment.

TIER: REAL PROCESSES (one disposable fixture pc\\build64\\bin_fixture4_shopb_join, exe of pc\\build64_w3), Samsung display + volume 1:
  P1 dedicated host (hidden observer) on the fixture town (Nook's Cranny, level 0): `shop-level-boost confirm` -> PENDING, `shop-upgrade start confirm` -> CLOSED
     (AC_TEST_SHOP_UPGRADE_SECONDS=20) -> renewed to Nook 'n' Go (level 1); `save`; the host town GCI has 0x5805 / level 1, the fixture's own save/card_a (the guest's
     LOCAL town copy) keeps 0x5804 / level 0 = the stale client save of the report.
  P2 the host is restarted (a fresh shadow init with the observer in town = the user's case): host log 'shadow: the shop structure tile ...' (INFO when the shop acre
     was not loaded around the observer); PROTOCOL: a throwaway fake client's join snapshot holds the structure tile = 0x5805 (pre-fix: unknown).
  P3 a REAL guest client (--guest, its local save = the stale copy) joins: its log has '[NET][SHOP] client: shop building ...' once; either it matches the host
     (0x5805, the snapshot wrote the tile before the shop acre was built) or '!= host 0x5805: refreshing' followed by the re-tag + ONE same-position reload and the
     verify line 'after the town reload the local structure tile ... = 0x5805|0xF0F8 (shop level 1)'.
NOT covered: what the windows LOOK like (no screenshot), a resident client (same client code path), AC_MP_SHOP_JOIN_SYNC=0 (pinned by test_shop_join_sync_src.py).
Ports 32000-32499 only. Usage: python test_shop_join_sync_real.py [--port 32140]
"""
import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_DISPLAY_NAME"] = "samsung"
os.environ["AC_MASTER_VOLUME"] = "1"
os.environ["AC_TOWN_NO_MSGBOX"] = "1"
import town_test_support as T  # noqa: E402
import net_spike_lib as L  # noqa: E402

FIX = "bin_fixture4_shopb_join"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
GCI_SAVE = 0x40 + 0x26000
FG_OFF, SHOP_OFF = 0x137A8, 0x20340
results = []
ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731


def shop_state(gci_path):
    """(acre, tile, structure value, Shop_c level) of the first SHOP0..SHOP3 tile in the town field of a GCI (big-endian Save_t)."""
    d = open(gci_path, "rb").read()
    lvl = struct.unpack_from(">H", d, GCI_SAVE + SHOP_OFF + 0x126)[0] >> 14
    for i in range(0, 30 * 512, 2):
        v = struct.unpack_from(">H", d, GCI_SAVE + FG_OFF + i)[0]
        if 0x5804 <= v <= 0x5807:
            return i // 512, (i % 512) // 2, v, lvl
    return None, None, None, lvl


def my_procs_running(dirs):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Process AnimalCrossing -ErrorAction SilentlyContinue | ForEach-Object { $_.Path }"],
                       capture_output=True, text=True)
    paths = [p.strip().lower() for p in r.stdout.splitlines() if p.strip()]
    return [p for p in paths if any(p.startswith(os.path.normcase(os.path.abspath(d)).lower()) for d in dirs)]


def start_host(fx, port, tag):
    env = {"AC_TEST_SHOP_UPGRADE_SECONDS": "20"}
    h = L.HostProcess(port=port, extra_args=["--dedicated", "-debug"], log_path=T.log_path("shopjoin_host_%s.log" % tag), bin_dir=fx, stdin_pipe=True, env=env).start()
    if tag == "p1" and h.wait_for_log(r"\[1\] Use this town", 40.0) is not None:
        h.send_line("1")  # first launch: adopt the fixture town (save/card_a, copied; the legacy file stays = the client's stale local copy)
    ok = h.wait_listening(90.0) and h.boot_to_dedicated(timeout=150.0)
    return h, ok


def stop_host(h):
    try:
        h.send_line("stop")
        h.wait_exit(60.0)
    except Exception:  # noqa: BLE001
        pass
    h.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=32140)
    args = ap.parse_args()
    if not 32000 <= args.port <= 32499:
        print("REFUSING: ports 32000-32499 only", file=sys.stderr)
        return 2
    fx = os.path.join(T.BUILD64, FIX)
    if my_procs_running([fx]):
        print("REFUSING: a game process of the fixture is still running", file=sys.stderr)
        return 2
    T.make_fixture(FIX)
    shutil.copy2(W3_EXE, os.path.join(fx, "AnimalCrossing.exe"))
    L.GAME_BIN_DIR = fx
    os.environ["NET_SPIKE_GAME_BIN"] = fx
    os.makedirs(os.path.join(fx, "save", "mp"), exist_ok=True)
    with open(os.path.join(fx, "save", "mp", "guest.ini"), "wb") as f:
        f.write(b"# test profile\nname = Quill\ngender = 1\nface = 5\nhome_town = Hometwn\nplayer_id = 0x4D37\nland_id = 24151\n")
    local_gci = os.path.join(fx, "save", "card_a", "DobutsunomoriP_MURA.gci")
    a0, t0, v0, l0 = shop_state(local_gci)
    ck("P0 the fixture town has Nook's Cranny (acre %s tile %s = %s, level %d)" % (a0, t0, v0 and hex(v0), l0), v0 == 0x5804 and l0 == 0)
    h = gst = None
    try:
        # ---------------- P1: the host upgrades the shop
        h, ok = start_host(fx, args.port, "p1")
        ck("P1 dedicated host booted (observer in the town)", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        off = len(h.log_text())
        h.send_line("shop-level-boost confirm")
        ck("P1 the boost made the upgrade PENDING", h.wait_for_log(r"PENDING: the town qualifies for", 20.0, since_offset=off) is not None)
        h.send_line("shop-upgrade start confirm")
        ck("P1 the console started the closure (CLOSED)", h.wait_for_log(r"\] host: CLOSED: the upgrade", 20.0, since_offset=off) is not None)
        up = h.wait_for_log(r"the shop was upgraded \(.*\): level (\d+)", 120.0, since_offset=off)
        ck("P1 the host renewed the shop (level %s)" % (up and up.group(1)), up is not None and up.group(1) == "1")
        off = len(h.log_text())
        h.send_line("save")
        ck("P1 host save OK", h.wait_for_log(r"save result: OK", 60.0, since_offset=off) is not None)
        stop_host(h)
        h = None
        hg = [os.path.join(dp, n) for dp, _d, fs in os.walk(os.path.join(fx, "servers")) for n in fs if n == "DobutsunomoriP_MURA.gci" and os.sep + "town" + os.sep in dp]
        hs = shop_state(hg[0]) if hg else (None, None, None, -1)
        ck("P1 the host town GCI has Nook 'n' Go (tile %s, level %d)" % (hs[2] and hex(hs[2]), hs[3]), hs[2] == 0x5805 and hs[3] == 1)
        ls = shop_state(local_gci)
        ck("P1 the client's LOCAL save is the stale copy (tile %s, level %d)" % (ls[2] and hex(ls[2]), ls[3]), ls[2] == 0x5804 and ls[3] == 0)

        # ---------------- P2: fresh host session (shadow init with the observer in the town)
        h, ok = start_host(fx, args.port, "p2")
        ck("P2 dedicated host restarted", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        sh = re.search(r"\[NET\]\[SHOP\] host: shadow: the shop structure tile acre (\d+) tile (\d+) is 0x([0-9A-F]{4}) \(live placeholder 0x([0-9A-F]{4})\)", h.log_text())
        if sh:
            ck("P2 host shadow learned the structure tile behind its live shop actor (%s)" % sh.group(0)[30:], sh.group(3) == "5805")
        else:
            L.info("P2 no 'shadow: the shop structure tile' line: the shop acre was not loaded around the observer at the shadow init (the save tile was stable)")
        ut = L.acre_tile_to_town_ut(a0, t0)
        tiles = L.read_tiles_via_snapshot("127.0.0.1", args.port, [ut])
        ck("P2 PROTOCOL: a joining client's snapshot carries the host's structure tile 0x5805 (got %s)" % (tiles.get(ut) is not None and hex(tiles.get(ut))), tiles.get(ut) == 0x5805)

        # ---------------- P3: a REAL guest with the stale local save joins
        gst = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--guest", "-debug"], log_path=T.log_path("shopjoin_guest.log"), bin_dir=fx, label="sjg").start()
        ck("P3 guest READY", gst.wait_for_log(r"-> READY", 240.0) is not None)
        m = gst.wait_for_log(r"\[NET\]\[SHOP\] client: shop building (0x[0-9A-F]{4}) (matches|!= host (0x[0-9A-F]{4}): refreshing)", 120.0)
        ck("P3 the guest ran the shop building check (%s)" % (m and m.group(0)), m is not None)
        if m is not None and m.group(2) == "matches":
            ck("P3 the guest's building is the host's Nook 'n' Go (snapshot wrote the tile before the shop acre was built)", m.group(1) == "0x5805")
            L.info("P3 the refresh path (re-tag + reload) was NOT exercised: the shop acre was not built from the stale tile in this run")
        elif m is not None:
            ck("P3 the guest saw the stale building and the host's Nook 'n' Go (%s != %s)" % (m.group(1), m.group(3)), m.group(1) == "0x5804" and m.group(3) == "0x5805")
            if "live structure actor" in gst.log_text()[m.start():m.start() + 400]:
                rl = gst.wait_for_log(r"same-position SCENE_FG reload \(the shop building differs from the host's\): res=1", 180.0)
                ck("P3 ONE same-position reload refreshed the town", rl is not None)
                vt = gst.wait_for_log(r"after the town reload the local structure tile acre \d+ tile \d+ = 0x([0-9A-F]{4}) \(shop level (\d+)\)", 30.0)
                ck("P3 after the reload the structure is Nook 'n' Go (%s)" % (vt and vt.group(0)), vt is not None and vt.group(1) in ("5805", "F0F8") and vt.group(2) == "1")
            else:
                L.info("P3 the shop acre was not loaded: the field tile was rewritten (the new shop is built when the acre loads)")
        L.pump_sleep(6.0)
        txt = gst.log_text()
        ck("P3 exactly one shop building check in the session", len(re.findall(r"\[NET\]\[SHOP\] client: shop building 0x", txt)) == 1)
        ck("P3 at most one join refresh reload", len(re.findall(r"reload \(the shop building differs from the host's\): res=1", txt)) <= 1)
        ck("P3 host + guest alive", h.exit_code() is None and gst.exit_code() is None)
    finally:
        if gst is not None:
            gst.stop()
        if h is not None:
            stop_host(h)
        time.sleep(2.0)
        left = my_procs_running([fx])
        if left:
            print("WARNING: fixture processes still running: %s" % left)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
