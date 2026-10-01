#!/usr/bin/env python3
"""test_player_scene_fixes.py - M9-A review fixes, REAL host + REAL client process + scripted FakeClient B.

  F1  RELAY-TIMEOUT PRESERVES SCENE: B announces SHOP0 and sends a few MOVEs through the host, then stays
      connected (heartbeats only, no MOVE) for > 8 s. The real client C times B's puppet slot out; its
      log line must say the scene identity was PRESERVED and C must NOT log a 'player N scene cleared'.
      B then genuinely disconnects -> host CLEARED -> C logs the scene cleared (real departure still clears).
  F2  HOST-LINK LOSS CLEARS RELAYED SCENES: with C holding the host's scene and B's scene, the host process
      is killed; C logs 'host link lost -- clearing N stored peer scene(s)' with N >= 2.

Tier: PROTOCOL TESTED + real-process (no gameplay hook). Not manual play.
Usage: python test_player_scene_fixes.py [--port 7830]
"""
import argparse
import os
import re
import struct
import sys
import time

import net_spike_lib as L
from test_player_scene_real import boot_host, boot_client

HERE = os.path.dirname(os.path.abspath(__file__))
FMT = "<BBBBHHI"
SCENE_SHOP0 = 9


def scene_msg(seq, sid=SCENE_SHOP0):
    return struct.pack(FMT, L.PC_NETGAME_MSG_PLAYER_SCENE, 0, sid, 0, 0, 0, seq)


def setup(port, log_dir, name, check):
    host = boot_host(port, [], name, log_dir)
    if host is None:
        check(name + " host reached field", False)
        return None
    cl = boot_client(port, [], name, log_dir)
    if cl is None:
        check(name + " client reached field", False)
        host.stop()
        return None
    b = L.FakeClient("B", "127.0.0.1", port)
    b.connect_and_ready()
    return host, cl, b


def f1(port, log_dir, check):
    print("=" * 72 + "\n[F1] relay timeout preserves the relayed scene; real departure still clears it")
    s = setup(port, log_dir, "f1", check)
    if s is None:
        return
    host, cl, b = s
    try:
        pid = b.assigned_peer_id
        b.send_reliable(scene_msg(1))
        for i in range(10):  # a few MOVEs relayed by the host -> C creates a lazily-discovered slot
            b.send_move_any(100.0 + i, 0.0, 100.0, speed=1.0)
            L.pump_sleep(0.05, b.hub)
        ok = cl.wait_for_log(r"\[NET\]\[SCENE\] client: player %d now in scene 9 " % pid, 15.0) is not None
        check("F1 client learned B's scene (SHOP0) via the host relay", ok)
        check("F1 client discovered B via the movement relay",
              cl.wait_for_log(r"player %d discovered via movement relay" % pid, 10.0) is not None)
        off = len(cl.log_text())
        t_end = time.monotonic() + 14.0  # B keeps the transport alive but sends NO MOVE
        while time.monotonic() < t_end:
            L.pump_sleep(0.1, b.hub)
        mo = cl.wait_for_log(r"player %d timed out \(no movement data\).*\(scene identity (\w+)\)" % pid, 5.0,
                             since_offset=off)
        check("F1 client timed B's puppet slot out", mo is not None)
        check("F1 the timeout kept the scene identity ('preserved')", mo is not None and mo.group(1) == "preserved")
        check("F1 no 'scene cleared' for B was logged by the timeout",
              re.search(r"client: player %d scene cleared" % pid, cl.log_text()[off:]) is None)
        off = len(cl.log_text())
        b.disconnect()
        ok = cl.wait_for_log(r"client: player %d scene cleared" % pid, 15.0, since_offset=off) is not None
        check("F1 a real departure (host CLEARED notice) still clears the scene", ok)
        check("F1 processes alive", host.alive() and cl.alive())
    finally:
        b.close()
        cl.stop()
        host.stop()


def f2(port, log_dir, check):
    print("=" * 72 + "\n[F2] client clears relayed scenes when the host link drops")
    s = setup(port, log_dir, "f2", check)
    if s is None:
        return
    host, cl, b = s
    try:
        pid = b.assigned_peer_id
        b.send_reliable(scene_msg(1))
        ok = cl.wait_for_log(r"\[NET\]\[SCENE\] client: player %d now in scene 9 " % pid, 15.0) is not None
        check("F2 client holds B's scene (stored, no MOVE ever sent: in_use==0 case)", ok)
        check("F2 client also holds the host's scene",
              cl.wait_for_log(r"\[NET\]\[SCENE\] client: player %d now in scene 7 " % L.PC_NETGAME_HOST_PLAYER_ID, 5.0)
              is not None)
        off = len(cl.log_text())
        host.stop()
        mo = cl.wait_for_log(r"host link lost -- clearing (\d+) stored peer scene", 20.0, since_offset=off)
        check("F2 client logged host-link loss and cleared >= 2 held scenes (host + B)",
              mo is not None and int(mo.group(1)) >= 2)
        check("F2 client still alive after host loss", cl.alive())
    finally:
        b.close()
        cl.stop()
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7830)
    args = ap.parse_args()
    results = []
    log_dir = os.path.join(HERE, "logs", "m9a")
    os.makedirs(log_dir, exist_ok=True)
    check = lambda d, c: L.check(d, c, results)
    f1(args.port, log_dir, check)
    f2(args.port + 1, log_dir, check)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
