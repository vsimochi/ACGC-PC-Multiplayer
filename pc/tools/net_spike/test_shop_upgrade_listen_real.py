#!/usr/bin/env python3
"""test_shop_upgrade_listen_real.py - the announced shop upgrade on a LISTEN host (a real `--host --bootstrap-resident 0` process whose own player plays) + a scripted resident FakeClient.

The host player's announcement is replaced by the TEST-ONLY AC_TEST_SHOP_UPGRADE_HOST_START=<s> (the host starts the closure itself <s> s after PENDING, which is what
pc_shop_upgrade_begin() does for the host's own player after Nook's announcement); AC_TEST_SHOP_UPGRADE_QUALIFY=1 adds the sales of the next tier; the closure is 15 s.
  L1 host player idle in the TOWN: closure -> RENEWING -> the vanilla due state is armed -> pc_host_renewshop_poll reloads the town -> the vanilla Shop_Level ct renews -> published,
     the client sees IDLE with the new level.
  L2 host player INDOORS (a villager's house, AC_TEST_ROOM_ENTER) at the deadline: the renewal runs DIRECTLY on the save (no reload), published, IDLE.
  L3 host player INSIDE THE SHOP (--scene-test-enter-shop) when the closure starts: the host's own player is shown out to the town, then the in-town renewal.
Usage: NET_SPIKE_GAME_BIN=<pc\\build64\\bin_fixture4_*> python test_shop_upgrade_listen_real.py [--port 16170]   (a copy made by this test when the variable is not set)
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
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_shupL"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
if "NET_SPIKE_GAME_BIN" not in os.environ:
    os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)
    if __name__ == "__main__":
        T.make_fixture(FIXTURE)
        shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")

import net_spike_lib as L  # noqa: E402
import test_ts_protocol as TS  # noqa: E402

SVC_UPGRADE = 6
HOOK_ENV = {"AC_TEST_HOOKS": "1", "AC_TEST_SHOP_UPGRADE_SECONDS": "15", "AC_TEST_SHOP_UPGRADE_QUALIFY": "1"}


def wait_log(run, pat, timeout):
    end = time.time() + timeout
    while time.time() < end:
        m = re.search(pat, run.log())
        if m:
            return m
        L.pump_sleep(0.25)
    return None


def up_state(c):
    s = c.ts_latest(SVC_UPGRADE)
    return (s[1][0], s[1][3]) if s and len(s[1]) == 16 else None


def common_tail(run, a, tag, how_rx):
    m = wait_log(run, r"\] host: CLOSED: the upgrade \d+ .* was announced by the TEST-ONLY host start hook", 40.0)
    run.check("%s the host started the closure (host start hook = the host player's acknowledgement)" % tag, m is not None)
    m = wait_log(run, r"host: RENEWING \(the closure time elapsed\)", 30.0)
    run.check("%s RENEWING after the 15 s closure" % tag, m is not None)
    m = wait_log(run, r"the shop was upgraded \((%s)\): level (\d+)" % how_rx, 60.0)
    run.check("%s renewed (%s)" % (tag, m and m.group(1)), m is not None)
    run.check("%s the shop acre was published" % tag, "published the shop acre" in run.log())
    end = time.time() + 10.0
    while time.time() < end and not (up_state(a) and up_state(a)[0] == 0 and m and up_state(a)[1] == int(m.group(2))):
        L.pump_sleep(0.25)
    run.check("%s the client sees IDLE at the new level (%s)" % (tag, up_state(a)), m is not None and up_state(a) == (0, int(m.group(2))))
    run.check("%s the client stayed connected and the host is alive" % tag, a.state == a.STATE_CONNECTED and run.host.alive())
    run.check("%s the listen host wrote its restart file (save/mp/shop_upgrade.ini) and removed it after the durable save" % tag,
              "could not write" not in run.log() and "the renewed shop was saved durably" in run.log()
              and not os.path.isfile(os.path.join(L.GAME_BIN_DIR, "save", "mp", "shop_upgrade.ini")))


def phase_town(run):
    a = run.ready("A", run.r1)
    common_tail(run, a, "L1", r"the vanilla shop actor renewed it at the town reload")
    run.check("L1 the in-town path armed the vanilla state and the reload ran", "the vanilla renewal is armed" in run.log() and "shop renewal due" in run.log())


def phase_indoors(run):
    a = run.ready("A", run.r1)
    m = wait_log(run, r"\[NET\]\[SCENE\] local scene live: scene=6 ", 60.0)
    run.check("L2 the host player entered a villager's house (AC_TEST_ROOM_ENTER)", m is not None)
    common_tail(run, a, "L2", r"renewed directly on the save while the host player is not in the town")
    run.check("L2 no town reload was needed", "shop renewal due" not in run.log())


def phase_in_shop(run):
    a = run.ready("A", run.r1)
    m = wait_log(run, r"\[NET\]\[SCENE\] local scene live: scene=9 ", 60.0)
    run.check("L3 the host player stands in the shop (--scene-test-enter-shop)", m is not None)
    # nobody presses A on a headless listen host: Nook's entry greeting stays open, so the exit is HELD (a conversation is never cut) until the existing hard cap forces it
    m = wait_log(run, r"\[NET\]\[RESTOCK\] exit held: demo=1 type=\d+ able=\d msg_hidden=0 restocking=1", 60.0)
    run.check("L3 while Nook's greeting is open the host player's exit is HELD (never cut mid-talk)", m is not None)
    m = wait_log(run, r"\[NET\]\[RESTOCK\] shop closed for the upgrade: the player was shown out", 420.0)
    run.check("L3 the HOST's own player was shown out of the shop (%s)" % (m and m.group(0)), m is not None)
    common_tail(run, a, "L3", r"the vanilla shop actor renewed it at the town reload")
    lg = run.log()
    run.check("L3 the renewal waited for the host player to leave the shop (RENEWING before the exit, renewed after it)",
              m is not None and 0 <= lg.find("RENEWING (the closure time elapsed)") < lg.find("shop closed for the upgrade: the player was shown out") < lg.find("the shop was upgraded"))


PHASES = [("L1", phase_town, {"AC_TEST_SHOP_UPGRADE_HOST_START": "3"}, []),
          ("L2", phase_indoors, {"AC_TEST_SHOP_UPGRADE_HOST_START": "3", "AC_TEST_ROOM_ENTER": "0,1500,0,0,600000"}, []),
          ("L3", phase_in_shop, {"AC_TEST_SHOP_UPGRADE_HOST_START": "10"}, ["--scene-test-enter-shop"])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16170)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    if "AnimalCrossing.exe" in subprocess.run(["tasklist"], capture_output=True, text=True).stdout:
        print("REFUSING: an AnimalCrossing.exe process is running", file=sys.stderr)
        return 2
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a pc\\build64\\bin_fixture4* copy only", file=sys.stderr)
        return 2
    only = [x for x in args.only.split(",") if x]
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "shop_upgrade_listen_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    saved = {k: os.environ.get(k) for k in ("AC_TEST_HOOKS", "AC_TEST_SHOP_UPGRADE_SECONDS", "AC_TEST_SHOP_UPGRADE_QUALIFY", "AC_TEST_SHOP_UPGRADE_HOST_START", "AC_TEST_ROOM_ENTER")}
    try:
        for i, (tag, body, env, extra) in enumerate(PHASES):
            if only and tag not in only:
                continue
            for k in saved:
                os.environ.pop(k, None)
            os.environ.update(HOOK_ENV)
            os.environ.update(env)
            TS.run_phase(results, "127.0.0.1", args.port + i, tag, extra, body, snap_gci, restore)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
