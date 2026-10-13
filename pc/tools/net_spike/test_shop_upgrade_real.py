#!/usr/bin/env python3
"""test_shop_upgrade_real.py - the announced 5-minute shop upgrade with a REAL guest client process in Nook's shop.

A REAL dedicated host (bin_fixture4_shupH, a generated town) + a REAL guest client N (bin_fixture4_shupN, a store character) placed in the shop by the TEST-ONLY `--nook-test warp`
hook. Both fixtures get the exe of pc\\build64_w3. Host env: AC_TEST_SHOP_UPGRADE_QUALIFY=1 (the sales of the next tier once a peer is READY = what `shop-level-boost confirm`
does) and AC_TEST_SHOP_UPGRADE_SECONDS=20. The client's A button is the autopilot `mashc` (answers every choice with the first entry). Display pinned to the Samsung monitor, volume 1.
  R1 the upgrade becomes PENDING; N enters the shop; the talk Nook starts is the ANNOUNCEMENT (client log 'the shop upgrade announcement is shown'); mashc acknowledges it ->
     the client sends SHOP_UPGRADE_REQ, the host CLOSES the shop (exactly once) -> the closing row -> the conversation ends -> N is shown out to the town
     ('shop closed for the upgrade: the player was shown out').
  R2 after the 20 s closure the host renews (log 'the shop was upgraded', the shop acre published); N is told IDLE (level + 1), patches its live shop structure actor and
     reloads the town at its position ('same-position SCENE_FG reload (the shop was upgraded): res=1').
  R3 N is warped into the shop again: the host's shop level is the new one, nobody announces again (no second 'announcement is shown').
NOT covered: what the windows LOOK like (no screenshot assertion), the door text while closed, a listen host (see test_shop_upgrade_listen_real.py).
Usage: python test_shop_upgrade_real.py [--port 16150]   (refuses to run while another AnimalCrossing.exe is running)
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_w9_shop_real as W  # noqa: E402  (sets the display / volume / AC_TEST_HOOKS env; helpers)
from test_w9_shop_real import T, L  # noqa: E402

HOST_NAME, N_NAME = "bin_fixture4_shupH", "bin_fixture4_shupN"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
L.GAME_BIN_DIR = os.path.join(T.BUILD64, HOST_NAME)
os.environ["NET_SPIKE_GAME_BIN"] = L.GAME_BIN_DIR
results = []
ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
SHOWN = r"\[NET\]\[RESTOCK\] shop closed for the upgrade: the player was shown out \(waited (\d+) frames([^)]*)\)"


def game_running():
    r = subprocess.run(["tasklist"], capture_output=True, text=True)
    return "AnimalCrossing.exe" in r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16150)
    args = ap.parse_args()
    if game_running():
        print("REFUSING: an AnimalCrossing.exe process is running (the user's game / server must not be disturbed)", file=sys.stderr)
        return 2
    T.make_fixture(HOST_NAME, empty_save=True)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, HOST_NAME, "AnimalCrossing.exe"))
    n_dir, n_uuid = W.setup_client_fixture(N_NAME, "n", old_exe=W3_EXE)
    shutil.copy2(W3_EXE, os.path.join(n_dir, "AnimalCrossing.exe"))
    ip = W.IP
    env_h = dict(os.environ, AC_TEST_HOOKS="1", AC_TEST_SHOP_UPGRADE_SECONDS="20", AC_TEST_SHOP_UPGRADE_QUALIFY="1")
    h = L.HostProcess(port=args.port, extra_args=["--dedicated", "--town-serve", "on", "--time", "12"], log_path=T.log_path("shupreal_host.log"), bin_dir=L.GAME_BIN_DIR,
                      stdin_pipe=True, env=env_h).start()
    if h.wait_for_log(r"Enter town name", 60.0) is not None:
        h.send_line("Upgrade")  # first launch: the console asks the new town's name
    ok = h.wait_listening(90.0) and h.boot_to_dedicated(timeout=150.0)
    ck("[host] dedicated host booted (generated town)", ok)
    procs = []
    try:
        if not ok:
            return L.summary_and_exit_code(results)
        envN = dict(W.ENV, AC_TEST_SHOP_DIAG="1", AC_TEST_AUTOPILOT="1")
        cn = L.ClientProcess("%s:%d" % (ip, args.port), extra_args=["--character", n_uuid, "--town-fetch", "--online-ui", "--nook-test", "warp"],
                             log_path=T.log_path("shupreal_N.log"), bin_dir=n_dir, env=envN, label="N").start()
        procs.append(cn)
        ck("R1 N READY", W.wait_pump(cn, r"-> READY", 300.0) is not None)
        ck("R1 the host made the upgrade PENDING (test qualify hook)", W.wait_pump(h, r"PENDING: the town qualifies for", 30.0) is not None)
        ck("R1 N was told PENDING", W.wait_pump(cn, r"\[NET\]\[SHOPUP\] client: the host says the shop upgrade is PENDING", 30.0) is not None)
        ck("R1 N warped into the shop", W.wait_pump(cn, r"--nook-test: warping the guest into the shop", 120.0) is not None)
        ann = W.wait_pump(cn, r"\[NET\]\[SHOPUP\] nook dialogue: the shop upgrade announcement is shown", 60.0)
        ck("R1 Nook's talk is the upgrade ANNOUNCEMENT", ann is not None)
        acked = None
        for k in range(1, 7):  # answer the announcement's choice (and any other question) with the first choice until the closing row is up
            open(os.path.join(n_dir, "ac_auto.txt"), "w").write("%d mashc 6\n" % (10 + k))
            W.wait_pump(cn, r"\[AUTO\] done %d mashc" % (10 + k), 20.0)
            acked = re.search(r"nook dialogue: the closure of the upgrade was started -> closing row", cn.log_text())
            if acked:
                break
        ck("R1 the acknowledgement sent the start request and the closure started (closing row)", acked is not None
           and "start request sent" in cn.log_text())
        ck("R1 the host CLOSED the shop exactly once (announced by a peer)", W.wait_pump(h, r"\] host: CLOSED: the upgrade \d+ .* was announced by peer", 10.0) is not None
           and len(re.findall(r"\] host: CLOSED: the upgrade", h.log_text())) == 1)
        for k in range(20, 24):  # close the closing row
            open(os.path.join(n_dir, "ac_auto.txt"), "w").write("%d mashc 6\n" % k)
            W.wait_pump(cn, r"\[AUTO\] done %d mashc" % k, 20.0)
            if re.search(SHOWN, cn.log_text()):
                break
        ex = W.wait_pump(cn, SHOWN, 30.0)
        ck("R1 N was shown out of the shop once the conversation ended (%s)" % (ex and ex.group(0)), ex is not None and "FORCED" not in ex.group(2))
        # ---------------- R2: the renewal
        up = W.wait_pump(h, r"the shop was upgraded \(.*\): level (\d+)", 60.0)
        ck("R2 the host renewed the shop after the closure (level %s)" % (up and up.group(1)), up is not None)
        ck("R2 the host published the shop acre", "published the shop acre" in h.log_text())
        lvl_new = int(up.group(1)) if up else -1
        rel = W.wait_pump(cn, r"\[NET\]\[SHOPUP\] client: (same-position SCENE_FG reload \(the shop was upgraded\): res=1|no live shop structure actor .* local structure tile acre \d+ tile \d+ = 0x([0-9A-F]{4}))", 60.0)
        ck("R2 N refreshed its town view after the reopening (%s)" % (rel and rel.group(1)[:60]), rel is not None)
        if rel is not None and rel.group(2) is not None:
            # the shop acre was not loaded around N (the test warp put N back where it stood, far from the shop): no live structure actor to patch, the field holds the new tile
            ck("R2 N's local structure tile is the NEW one 0x%04X (FIELD_BLOCK applied)" % (0x5804 + lvl_new), int(rel.group(2), 16) == 0x5804 + lvl_new)
            print("NOTE - R2 the live-structure-actor patch + same-position reload path was NOT exercised in this run (the shop acre was not loaded around N)")
        elif rel is not None:
            ck("R2 N patched its live shop structure actor to the new tile before the reload",
               re.search(r"live shop structure actor \(0x58\w\w\) will restore 0x%04X at its teardown" % (0x5804 + lvl_new), cn.log_text()) is not None)
            vt = W.wait_pump(cn, r"after the town reload the local structure tile acre \d+ tile \d+ = 0x([0-9A-F]{4}) \(shop level (\d+)\)", 30.0)
            ck("R2 after the reload N's structure tile is the NEW shop (SHOP0+%d or its live DUMMY_SHOP%d) and its shop mirror says level %d (%s)"
               % (lvl_new, lvl_new, lvl_new, vt and vt.group(0)),
               vt is not None and int(vt.group(1), 16) in (0x5804 + lvl_new, 0xF0F7 + lvl_new) and int(vt.group(2)) == lvl_new)
        ck("R2 N was told IDLE with the new level", re.search(r"client: the shop reopened at level %s" % (up.group(1) if up else "x"), cn.log_text()) is not None)
        # ---------------- R3: the new shop, no second announcement
        L.pump_sleep(8.0)
        ck("R3 host + N alive after the reload", h.alive() and cn.alive())
        ck("R3 exactly one announcement in this run", len(re.findall(r"the shop upgrade announcement is shown", cn.log_text())) == 1)
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
