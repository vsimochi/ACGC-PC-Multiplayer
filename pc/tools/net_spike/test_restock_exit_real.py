#!/usr/bin/env python3
"""test_restock_exit_real.py - Nook's restock EXIT LATCH (ac_shop_indoor.c aSI_pc_restock_eject) with a REAL guest client in the shop.

A REAL dedicated host (bin_fixture4_w9) + a REAL NEW guest client N (bin_fixture4_w9n) placed in the shop by the TEST-ONLY `--nook-test warp` hook, a scripted FakeClient
guest P pays the restock (kind 15; AC_TEST_RESTOCK_WAIT_MS=20000). The autopilot `mash` (A presses, now allowed indoors) plays the player's A button.
  D1 idle player: N dismisses Nook's entry greeting (mash), stands idle; P restocks -> N is shown out within a few seconds.
  D2 open conversation: a fresh N process enters, the greeting is LEFT OPEN; P restocks -> 'exit held: demo=1 type=8 ... msg_hidden=0' and NO exit while it
     is open; then mash -> shown out (restocking / restocked while talking).
Usage: python test_restock_exit_real.py [--port 16130]
"""
import argparse
import os
import re
import shutil
import time

import test_w9_shop_real as W  # sets NET_SPIKE_GAME_BIN = bin_fixture4_w9 and the env; reuses its helpers
from test_w9_shop_real import T, L

results = []
ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
SHOWN = r"\[NET\]\[RESTOCK\] shop (restocking|restocked while talking): the player was shown out \(waited (\d+) frames([^)]*)\)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16130)
    args = ap.parse_args()
    if not os.path.isfile(os.path.join(T.BUILD64, W.HOST_NAME, "servers", "default", "server.ini")):
        T.make_fixture(W.HOST_NAME, empty_save=True)
    shutil.copy2(os.path.join(T.BUILD64, "bin", "AnimalCrossing.exe"), os.path.join(T.BUILD64, W.HOST_NAME, "AnimalCrossing.exe"))
    n_dir, n_uuid = W.setup_client_fixture(W.N_NAME, "n")
    shutil.copy2(os.path.join(T.BUILD64, "bin", "AnimalCrossing.exe"), os.path.join(n_dir, "AnimalCrossing.exe"))
    L.require_test_bin_dir()
    L.server_wipe_sidecars()
    gP = L.guest_identity("PREVIEWR", 0x4D11, "HOMETWN", 0x5B21)
    recP = bytes(L.fresh_guest_record_for(gP))
    ip = W.IP

    def start_host(tag, port, env=None):
        h = L.HostProcess(port=port, extra_args=["--dedicated", "--town-serve", "on", "--resident-tokens", "tofu", "--time", "12"], log_path=T.log_path("w1_rx_%s_host.log" % tag),
                          bin_dir=L.GAME_BIN_DIR, stdin_pipe=True, verbose=False, new_group=True, env=env).start()
        ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
        ck("[%s] dedicated host booted" % tag, ok)
        return h, ok

    def stop_host(h):
        try:
            h.send_line("stop")
            h.wait_exit(60.0)
        except Exception:  # noqa: BLE001
            pass
        h.stop()

    h, ok = start_host("p0", args.port - 1)
    if not ok:
        return L.summary_and_exit_code(results)
    p = L.FakeClient("P0", ip, args.port - 1, guest=gP, guest_record_img=recP, bind_ip="127.0.91.3", record_wait=True)
    p.connect_and_ready(quiet=True)
    tokP = bytes(p.guest_token)
    L.pump_sleep(2.0)
    p.disconnect()
    p.close()
    L.pump_sleep(7.0)
    stop_host(h)
    ck("P0 P's stored wallet patched to 3000", W.patch_guest_wallet(os.path.join(L.server_dir(), "citizens", "guests"), L.guest_pid_be(gP), 3000))

    h, ok = start_host("p1", args.port, env={"AC_TEST_RESTOCK_WAIT_MS": "20000"})
    if not ok:
        return L.summary_and_exit_code(results)
    P = None
    procs = []
    try:
        P = L.FakeClient("P", ip, args.port, guest=gP, guest_token=tokP, guest_record_img=recP, bind_ip="127.0.91.4", record_wait=True)
        P.connect_and_ready(quiet=True)

        def enter(tag):
            envN = dict(W.ENV, AC_TEST_SHOP_DIAG="1", AC_TEST_AUTOPILOT="1")
            try:
                os.remove(os.path.join(n_dir, "ac_auto.txt"))
            except OSError:
                pass
            cn = L.ClientProcess("%s:%d" % (ip, args.port), extra_args=["--character", n_uuid, "--town-fetch", "--online-ui", "--nook-test", "warp"],
                                 log_path=T.log_path("w1_rx_%s_N.log" % tag), bin_dir=n_dir, env=envN, label="N" + tag).start()
            procs.append(cn)
            ck("%s N READY" % tag, W.wait_pump(cn, r"-> READY", 240.0) is not None)  # keeps P pumped
            gn = W.wait_pump(cn, r"--nook-test: warping the guest into the shop", 120.0)
            ck("%s N warped into the shop" % tag, gn is not None)
            W.wait_pump(cn, r"diag: restocking=0 able_submenu1=\d demo=1 msg_main_hidden=0 wipe=0", 40.0)
            return cn

        def restock(tag, rid):
            W.scene(P, W.SCENE_SHOP0)  # P stands in the shop (as worker 9's test does) before it pays
            L.pump_sleep(1.5)
            sent = P.send_txn_commit(L.PC_NETGAME_TXN_KIND_SHOP_RESTOCK, rid, L.PC_NETGAME_TXN_DEST_NONE, 0, 0, aux_cond=0, aux_item=500)
            r = P.wait_txn_result(sent.seq, 8.0)
            ck("%s P's SHOP_RESTOCK APPLIED" % tag, r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED)
            return time.monotonic()

        # ---------------- D1: idle player
        cn = enter("D1")
        md, dg = None, []
        for k in range(1, 5):  # the first entry of a new guest is a longer Nook dialogue (several questions): answer until the window is closed
            open(os.path.join(n_dir, "ac_auto.txt"), "w").write("%d mashc 6\n" % (10 + k))
            md = W.wait_pump(cn, r"\[AUTO\] done %d mashc ok" % (10 + k), 20.0)
            L.pump_sleep(4.0)
            dg = re.findall(r"diag: restocking=0 able_submenu1=(\d) demo=(\d) msg_main_hidden=(\d)", cn.log_text())
            if dg and dg[-1][1:] == ("0", "1"):
                break
        ck("D1 the indoor mash ran and dismissed Nook's greeting (last diag before the restock: %s)" % (dg[-1:] if dg else None),
           md is not None and dg and dg[-1][1:] == ("0", "1"))
        off = len(cn.log_text())
        t0 = restock("D1", 1)
        ex = W.wait_pump(cn, SHOWN, 20.0, since=off)
        ck("D1 the IDLE player was shown out %.1f s after the restock (%s)" % (time.monotonic() - t0, ex and ex.group(0)),
           ex is not None and ex.group(1) == "restocking" and "FORCED" not in ex.group(3) and time.monotonic() - t0 < 12.0)
        cn.stop()
        L.pump_sleep(25.0)  # the 20 s restock ends; P keeps pumping
        # ---------------- D2: greeting left open
        cn = enter("D2")
        off = len(cn.log_text())
        restock("D2", 2)
        hd = W.wait_pump(cn, r"\[NET\]\[RESTOCK\] exit held: demo=(\d) type=(\d+) able=(\d) msg_hidden=(\d) restocking=1", 15.0, since=off)
        ck("D2 the exit is HELD while the greeting is open (%s)" % (hd and hd.group(0)), hd is not None and hd.group(1) == "1" and hd.group(2) == "8" and hd.group(4) == "0")
        L.pump_sleep(6.0)
        ck("D2 NOT shown out while the conversation is open", re.search(SHOWN, cn.log_text()[off:]) is None)
        off2 = len(cn.log_text())
        open(os.path.join(n_dir, "ac_auto.txt"), "w").write("2 mashc 10\n")
        t1 = time.monotonic()
        ex = W.wait_pump(cn, SHOWN, 25.0, since=off2)
        ck("D2 after the player dismisses the greeting (mash) it is shown out %.1f s later (%s)" % (time.monotonic() - t1, ex and ex.group(0)),
           ex is not None and "FORCED" not in ex.group(3) and time.monotonic() - t1 < 16.0)
        ck("D4 host alive, N alive", h.alive() and cn.alive())
    finally:
        try:
            P and P.disconnect()
        except Exception:  # noqa: BLE001
            pass
        for c in procs:
            try:
                c.stop()
            except Exception:  # noqa: BLE001
                pass
        stop_host(h)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
