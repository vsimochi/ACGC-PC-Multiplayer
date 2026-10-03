#!/usr/bin/env python3
"""test_weeds_real_client.py - WEEDS: the REAL game client's half of WEED_PULL (FIELD_ACTION kind 10), REAL processes.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0 --field-action-test-seed`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot> --force-weed-pull`
The client hook (default off, client only) moves the actor onto the weed fixture (24,108) and forces the REAL, unmodified
Player_actor_request_main_remove_grass(): the real pull animation runs and its frame 17 reaches the real seam
(Player_actor_ChangeFGNumber_Remove_grass -> pc_net_game_request_remove_grass). It bypasses only the controller / target resolution.

  P   POSITIVE: the real pull sends exactly one WEED_PULL request, the host accepts and commits it (host log), the client's own tile becomes EMPTY_NO
      (FIELD_UPDATE), and a fresh scripted session sees the host world cleared at (24,108)
  N   NEGATIVE CONTROL: a second real pull of the weed at (40,108) from 16 tiles away is REJECTED by the host's reach check; the client's OWN
      weed at (40,108) is still there afterwards (vanilla would have cleared it locally: the client never writes the tile itself) and the host
      world still holds it

NOT covered: the real flower trample (a real dash over a flower cannot be driven headlessly; host side tested in test_weeds_protocol.py, client
seam source-audited in test_weeds_src.py), the real controller input.
Run ONLY on the disposable pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN=<absolute path>, ports 11650+). The fixture save is snapshotted at start
and restored before every host launch and at the end. One real client at a time (+ a short FakeClient session).
Usage: python test_weeds_real_client.py [--port 11650]
"""
import argparse
import os
import re
import shutil
import sys

import net_spike_lib as L
import test_txn_real_client as RC

W_NEAR, W_FAR = (24, 108), (40, 108)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11650)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "weeds_real_client_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    check = rig.check
    try:
        host_slot = L.TEST_HOST_RESIDENT
        residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
        check("fixture has non-host residents (slots %s)" % residents, len(residents) >= 1)
        if len(residents) < 1:
            return L.summary_and_exit_code(results)
        r1 = residents[0]
        host = rig.start_host("weeds", args.port, ["--field-action-test-seed"])
        check("host reached genuine field-ready state", host is not None)
        if host is None:
            return L.summary_and_exit_code(results)
        L.resolve_host_town(ip, args.port)
        host.wait_for_log(r"--field-action-test-seed: flower 0x083C fixture placed at tile \(88,108\)", 30.0)
        cl = rig.start_client("weeds", args.port, r1, ["--force-weed-pull"])
        check("real client booted into the field and connected", cl is not None)
        if cl is None:
            host.stop()
            return L.summary_and_exit_code(results)
        m = cl.wait_for_log(r"NEGATIVE CONTROL result: local tile \(40,108\) = 0x[0-9A-F]{4}", 150.0)
        check("the real client completed the positive pull and the negative control", m is not None)
        L.pump_sleep(2.0)
        ct = cl.log_text()
        ht = host.log_text()
        off = len(ht)
        cl.stop()
        rig.wait_peer_gone(off - 1)
        ht = host.log_text()

        # ---- P
        i_force = ct.find("--force-weed-pull: local tile (24,108) holds weed 0x0008; forcing a real Player_actor_request_main_remove_grass()")
        m_req = re.search(r"\[NET\]\[WEEDS\] client: WEED_PULL request at tile \(24,108\)", ct)
        m_acc = re.search(r"\[NET\]\[WEEDS\] client: request (\d+) WEED_PULL accepted at tile \(24,108\)", ct)
        m_now = re.search(r"--force-weed-pull: local tile \(24,108\) is now 0x0000 after (\d+) frames", ct)
        check("P the hook forced the real pull on the weed it found in the CLIENT's own grid (the host's seeded weed replicated)", i_force >= 0)
        check("P exactly ONE WEED_PULL request for (24,108) left the real seam (frame 17 of the real pull animation)",
              len(re.findall(r"client: WEED_PULL request at tile \(24,108\)", ct)) == 1 and m_req is not None)
        check("P the host's RESULT arrived: accepted", m_acc is not None)
        check("P the client's own tile became EMPTY_NO", m_now is not None)
        check("P order: forced < request sent < accepted", i_force >= 0 and m_req is not None and m_acc is not None
              and i_force < m_req.start() < m_acc.start())
        check("P the host logged exactly one WEED_PULL commit at (24,108) (0x0008 -> EMPTY_NO)",
              len(re.findall(r"\[NET\]\[WEEDS\] host: peer \d+ WEED_PULL at tile \(24,108\) 0x0008 -> EMPTY_NO", ht)) == 1)
        check("P the client applied the host's FIELD_UPDATE for that acre / tile (the only writer of its tile)",
              re.search(r"\[NET\]\[WORLD\] client: FIELD_UPDATE acre \d+ tile \d+ = 0x0000", ct) is not None)

        # ---- N
        m_neg = re.search(r"NEGATIVE CONTROL result: local tile \(40,108\) = 0x([0-9A-F]{4})", ct)
        check("N the negative-control pull request was sent (real seam) and the host did not execute it",
              len(re.findall(r"client: WEED_PULL request at tile \(40,108\)", ct)) == 1
              and not re.search(r"WEED_PULL at tile \(40,108\)", ht))
        check("N the host REJECTED it (client log: kind 10 rejected by host at (40,108))",
              re.search(r"client: request \d+ \(kind 10\) rejected by host \(tile 40,108\)", ct) is not None)
        check("N the client's OWN weed at (40,108) is still a weed (0x000A) -- the client never wrote its tile itself",
              m_neg is not None and int(m_neg.group(1), 16) == 0x000A)
        f = RC.fake_session(args.port, ip, r1, "Wsnap")
        t_near, t_far = f.world.tile_ut(*W_NEAR), f.world.tile_ut(*W_FAR)
        RC.release(f)
        check("host world (fresh join snapshot): (24,108) is EMPTY_NO, (40,108) is still the weed (observed %s / %s)" % (t_near, t_far),
              t_near == 0 and t_far == 0x000A)
        check("no protocol violation / delta impossible in the client log", "protocol violation" not in ct and "REJECTED(" not in ct)
        host.stop()
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
