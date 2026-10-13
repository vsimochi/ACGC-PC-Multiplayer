#!/usr/bin/env python3
"""test_shop_upgrade_door_real.py - shop upgrade follow-up (b) (worker C): a REAL client that stands IN FRONT OF THE SHOP in the town while the host renews it.

A REAL dedicated host (bin_fixture4_wlC_shdH, a generated town) + a REAL guest client N (bin_fixture4_wlC_shdN, a store character) placed in the shop by the TEST-ONLY
`--nook-test warp,shopdoor` hook (`shopdoor` = the exit a real shop door writes, in front of the shop, so the shown-out player stands next to the shop and its live shop structure
actor exists). Host env: AC_TEST_SHOP_UPGRADE_QUALIFY=1, AC_TEST_SHOP_UPGRADE_SECONDS=20. The client's A button is the autopilot (mashc / walk). Samsung display, volume 1.
  D1 the announcement is acknowledged -> the shop closes -> N is shown out IN FRONT OF THE SHOP (the exit line of the hook);
  D2 the host renews; N patches its LIVE shop structure actor (its teardown restores the NEW tile, never the old one) and reloads the town at its position; after the reload the
     local structure tile is the new shop (SHOP0+L+1 or its live DUMMY_SHOPn) -- no revert;
  D3 N walks into the shop door: the scene it enters is the NEW shop interior (SCENE_CONVENI for Nook 'n' Go).
Usage: python test_shop_upgrade_door_real.py [--port 16360]
"""
import argparse
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_w9_shop_real as W  # noqa: E402  (sets the display / volume / AC_TEST_HOOKS env; helpers)
from test_w9_shop_real import T, L  # noqa: E402

HOST_NAME, N_NAME = "bin_fixture4_wlC_shdH", "bin_fixture4_wlC_shdN"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
L.GAME_BIN_DIR = os.path.join(T.BUILD64, HOST_NAME)
os.environ["NET_SPIKE_GAME_BIN"] = L.GAME_BIN_DIR
results = []
ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
SHOWN = r"\[NET\]\[RESTOCK\] shop closed for the upgrade: the player was shown out \(waited (\d+) frames([^)]*)\)"
SCENE_CONVENI = 23


def auto(n_dir, line):
    open(os.path.join(n_dir, "ac_auto.txt"), "w").write(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16360)
    args = ap.parse_args()
    T.make_fixture(HOST_NAME, empty_save=True)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, HOST_NAME, "AnimalCrossing.exe"))
    n_dir, n_uuid = W.setup_client_fixture(N_NAME, "n", old_exe=W3_EXE)
    shutil.copy2(W3_EXE, os.path.join(n_dir, "AnimalCrossing.exe"))
    ip = W.IP
    env_h = dict(os.environ, AC_TEST_HOOKS="1", AC_TEST_SHOP_UPGRADE_SECONDS="20", AC_TEST_SHOP_UPGRADE_QUALIFY="1")
    h = L.HostProcess(port=args.port, extra_args=["--dedicated", "--town-serve", "on", "--time", "12"], log_path=T.log_path("shdreal_host.log"), bin_dir=L.GAME_BIN_DIR,
                      stdin_pipe=True, env=env_h).start()
    if h.wait_for_log(r"Enter town name", 60.0) is not None:
        h.send_line("Upgrade")
    ok = h.wait_listening(90.0) and h.boot_to_dedicated(timeout=150.0)
    ck("[host] dedicated host booted (generated town)", ok)
    procs = []
    try:
        if not ok:
            return L.summary_and_exit_code(results)
        envN = dict(W.ENV, AC_TEST_SHOP_DIAG="1", AC_TEST_AUTOPILOT="1")
        cn = L.ClientProcess("%s:%d" % (ip, args.port), extra_args=["--character", n_uuid, "--town-fetch", "--online-ui", "--nook-test", "warp,shopdoor"],
                             log_path=T.log_path("shdreal_N.log"), bin_dir=n_dir, env=envN, label="N").start()
        procs.append(cn)
        ck("D1 N READY", W.wait_pump(cn, r"-> READY", 300.0) is not None)
        ck("D1 the upgrade is PENDING and N was told (on-screen notice armed for every player)", W.wait_pump(cn, r"the host says the shop upgrade is PENDING", 60.0) is not None)
        ex = W.wait_pump(cn, r"--nook-test shopdoor: the exit is in front of the shop \(block (\d+),(\d+) tile (\d+)\) at \((-?\d+),(-?\d+),(-?\d+)\)", 120.0)
        ck("D1 N warped into the shop with its exit in front of the shop", ex is not None)
        ck("D1 Nook's talk is the upgrade ANNOUNCEMENT", W.wait_pump(cn, r"\[NET\]\[SHOPUP\] nook dialogue: the shop upgrade announcement is shown", 60.0) is not None)
        acked = None
        for k in range(1, 7):
            auto(n_dir, "%d mashc 6" % (10 + k))
            W.wait_pump(cn, r"\[AUTO\] done %d mashc" % (10 + k), 20.0)
            acked = re.search(r"nook dialogue: the closure of the upgrade was started -> closing row", cn.log_text())
            if acked:
                break
        ck("D1 the closure started", acked is not None)
        for k in range(20, 24):
            auto(n_dir, "%d mashc 6" % k)
            W.wait_pump(cn, r"\[AUTO\] done %d mashc" % k, 20.0)
            if re.search(SHOWN, cn.log_text()):
                break
        ck("D1 N was shown out of the shop", W.wait_pump(cn, SHOWN, 30.0) is not None)
        up = W.wait_pump(h, r"the shop was upgraded \(.*\): level (\d+)", 90.0)
        ck("D2 the host renewed the shop (level %s)" % (up and up.group(1)), up is not None)
        lvl_new = int(up.group(1)) if up else -1
        pat = W.wait_pump(cn, r"live shop structure actor \(0x(58\w\w)\) will restore 0x(\w{4}) at its teardown", 60.0)
        ck("D2 N (in front of the shop) patched its LIVE structure actor: the teardown restores the NEW tile 0x%04X (old %s)" % (0x5804 + lvl_new, pat and pat.group(1)),
           pat is not None and int(pat.group(2), 16) == 0x5804 + lvl_new)
        rel = W.wait_pump(cn, r"\[NET\]\[SHOPUP\] client: same-position SCENE_FG reload \(the shop was upgraded\): res=1", 60.0)
        ck("D2 N reloaded the town at its position", rel is not None)
        vt = W.wait_pump(cn, r"after the town reload the local structure tile acre \d+ tile \d+ = 0x([0-9A-F]{4}) \(shop level (\d+)\)", 30.0)
        ck("D2 after the reload the local structure tile is the NEW shop (no revert) and the shop mirror says level %d (%s)" % (lvl_new, vt and vt.group(0)),
           vt is not None and int(vt.group(1), 16) in (0x5804 + lvl_new, 0xF0F7 + lvl_new) and int(vt.group(2)) == lvl_new)
        L.pump_sleep(5.0)
        if ex is not None:
            dx, dz = int(ex.group(4)) + 68, int(ex.group(6)) - 68
            n0 = len(cn.log_text())
            for k in range(40, 46):
                auto(n_dir, "%d walk %d %d 10" % (k, dx, dz))
                if W.wait_pump(cn, r"local scene live: scene=(\d+) kind=2", 25.0, since=n0) is not None:
                    break
            sc = re.search(r"local scene live: scene=(\d+) kind=2", cn.log_text()[n0:])
            ck("D3 N walked into the shop door and entered the NEW interior (scene %s, Nook 'n' Go = %d)" % (sc and sc.group(1), SCENE_CONVENI),
               sc is not None and int(sc.group(1)) == SCENE_CONVENI)
        ck("D3 host + N alive", h.alive() and cn.alive())
    finally:
        for c in procs:
            try:
                c.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            h.send_line("stop")
            h.wait_exit(60.0)
        except Exception:  # noqa: BLE001
            pass
        h.stop()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
