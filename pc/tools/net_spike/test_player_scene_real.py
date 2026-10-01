#!/usr/bin/env python3
"""test_player_scene_real.py - M9-A (scene identity / player presence): TWO REAL game processes
(`--host --bootstrap-resident 0` and `--connect --bootstrap-resident 1`), no keystrokes, no GUI automation.

Tier labels (honest): R1 = real-process BOOTSTRAP run (both reach the field by direct bootstrap, nothing else).
R2/R3 = HOOK-DRIVEN REAL GAMEPLAY: the off-by-default test flag `--scene-test-enter-shop` /
`--scene-test-leave-after N` (pc_net_game.c pcnetgame_run_scene_test_hook()) requests a REAL goto_other_scene()
transition into SCENE_SHOP0 and back on the already-running field; everything after that (play_cleanup /
play_init, the new GAME_PLAY, the scene-live detection, the PLAYER_SCENE announcement/relay) is the real game
code. It is NOT manual play and does NOT prove that interiors work for multiplayer.

  R1  both processes reach the field and each logs exactly ONE local-scene announcement (FIELD = scene 7,
      IN_TOWN) with no title/demo/player-select scene ever announced; each learns the other's FIELD scene.
  R2  client-driven hook: client enters SCENE_SHOP0 (announced once), host sees peer scene 9; the client
      process is then killed while inside the shop -> host logs the peer's scene cleared; a NEW client process
      reconnects and re-announces FIELD (seq restarts, accepted).
  R3  host-driven hook: host enters SCENE_SHOP0 and returns to FIELD; announced FIELD -> SHOP0 -> FIELD exactly
      once each; the client receives the host's scene changes (replay and/or live).

Usage: python test_player_scene_real.py [--port 7810]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import sys
import time

import net_spike_lib as L

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_RX = re.compile(r"\[NET\]\[SCENE\] local scene live: scene=(\d+) kind=(\d+) owner=0x([0-9A-F]+) "
                     r"flags=0x([0-9A-F]+) seq=(\d+)")
SCENE_FG, SCENE_SHOP0 = 7, 9
HOST_ID = L.PC_NETGAME_HOST_PLAYER_ID


def live_scenes(text):
    return [int(m.group(1)) for m in LIVE_RX.finditer(text)]


def boot_host(port, extra, name, log_dir, attempts=3):
    for i in range(attempts):
        host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"] + extra,
                             log_path=os.path.join(log_dir, f"{name}_host_try{i}.log")).start()
        if host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=0):
            return host
        host.stop()  # known intermittent boot crash (diag_boot_crash.py): retry
        time.sleep(2.0)
    return None


def boot_client(port, extra, name, log_dir, attempts=3):
    for i in range(attempts):
        cl = L.ClientProcess(f"127.0.0.1:{port}", extra_args=["--bootstrap-resident", "1"] + extra,
                             log_path=os.path.join(log_dir, f"{name}_client_try{i}.log"), label=name).start()
        if cl.boot_to_field(timeout=90.0, slot=1):
            return cl
        cl.stop()
        time.sleep(2.0)
    return None


def wait_log(proc, pattern, timeout):
    return proc.wait_for_log(pattern, timeout) is not None


def r1(port, log_dir, check):
    print("=" * 72 + "\n[R1] two real processes, bootstrap only")
    host = boot_host(port, ["--authoritative-wildlife"], "r1", log_dir)
    if host is None:
        check("R1 host reached field", False)
        return
    client = boot_client(port, ["--authoritative-wildlife"], "r1", log_dir)
    try:
        if client is None:
            check("R1 client reached field", False)
            return
        check("R1 host and client both reached the field via direct bootstrap", True)
        time.sleep(3.0)
        h, c = host.log_text(), client.log_text()
        check("R1 host announced exactly one local scene and it is FIELD (7)", live_scenes(h) == [SCENE_FG])
        check("R1 client announced exactly one local scene and it is FIELD (7)", live_scenes(c) == [SCENE_FG])
        check("R1 host announced it with the IN_TOWN flag (0x01), kind FIELD (1), owner 0",
              re.search(r"scene=7 kind=1 owner=0x0000 flags=0x01", h) is not None)
        check("R1 client's announcement was SENT to the host (log says 'announcing to host')",
              "(announcing to host)" in c)
        check("R1 host accepted the client's scene (peer 0 now in scene 7, IN_TOWN)",
              re.search(r"\[NET\]\[SCENE\] host: peer \d+ now in scene 7 \(kind 1, owner 0x0000, flags 0x01", h) is not None)
        check("R1 client learned the host's scene (late-join replay or live): player %d in scene 7" % HOST_ID,
              re.search(r"\[NET\]\[SCENE\] client: player %d now in scene 7 " % HOST_ID, c) is not None)
        for name, txt in (("host", h), ("client", c)):
            bad = [s for s in live_scenes(txt) if s != SCENE_FG]
            check(f"R1 {name}: no title/demo/player-select/other scene was ever announced (saw {bad})", not bad)
        check("R1 both processes alive", host.alive() and client.alive())
    finally:
        if client:
            client.stop()
        host.stop()


def r2(port, log_dir, check):
    print("=" * 72 + "\n[R2] HOOK-DRIVEN: client enters SCENE_SHOP0, is killed inside, a new client reconnects")
    host = boot_host(port, [], "r2", log_dir)
    if host is None:
        check("R2 host reached field", False)
        return
    c1 = boot_client(port, ["--scene-test-enter-shop"], "r2a", log_dir)
    c2 = None
    try:
        if c1 is None:
            check("R2 client reached field", False)
            return
        ok = wait_log(c1, r"\[NET\]\[SCENE\]\[TEST\] hook: goto_other_scene\(SCENE_SHOP0\) res=1", 60.0)
        check("R2 hook requested the real goto_other_scene (res=1)", ok)
        ok = wait_log(c1, r"local scene live: scene=9 ", 60.0)
        check("R2 client's new scene (SHOP0=9) became live and was announced", ok)
        time.sleep(2.0)
        ct = c1.log_text()
        check("R2 client announced exactly FIELD then SHOP0, once each", live_scenes(ct) == [SCENE_FG, SCENE_SHOP0])
        ht = host.log_text()
        check("R2 host recorded the client's scene 9 with a seq newer than its FIELD seq",
              re.search(r"host: peer \d+ now in scene 7 .*seq 1\)", ht) is not None and
              re.search(r"host: peer \d+ now in scene 9 .*seq 2\)", ht) is not None)
        check("R2 host process alive while the client is inside the shop", host.alive() and c1.alive())
        off = len(host.log_text())
        c1.stop()  # killed while inside the shop
        ok = host.wait_for_log(r"\[NET\]\[SCENE\] host: peer \d+ left -- its scene was cleared", 30.0,
                               since_offset=off) is not None
        check("R2 killing the client inside the shop -> host cleared that peer's scene (no stale interior)", ok)
        off = len(host.log_text())
        c2 = boot_client(port, [], "r2b", log_dir)
        check("R2 a NEW client process reconnected and reached the field", c2 is not None)
        if c2 is not None:
            time.sleep(3.0)
            ht2 = host.log_text()[off:]
            check("R2 reconnect: host accepted the new client's FIELD scene again (seq restarts at 1)",
                  re.search(r"host: peer \d+ now in scene 7 .*seq 1\)", ht2) is not None)
            check("R2 reconnect: new client announced FIELD exactly once", live_scenes(c2.log_text()) == [SCENE_FG])
    finally:
        for p in (c2, c1):
            if p:
                p.stop()
        host.stop()


def r3(port, log_dir, check):
    print("=" * 72 + "\n[R3] HOOK-DRIVEN: host enters SCENE_SHOP0 and returns to FIELD")
    host = boot_host(port, ["--scene-test-enter-shop", "--scene-test-leave-after", "1800"], "r3", log_dir)
    if host is None:
        check("R3 host reached field", False)
        return
    client = boot_client(port, [], "r3", log_dir)
    try:
        if client is None:
            check("R3 client reached field", False)
            return
        ok = wait_log(host, r"\[NET\]\[SCENE\]\[TEST\] hook: goto_other_scene\(exit\) res=1", 150.0)
        check("R3 hook entered the shop and requested the real exit transition", ok)
        time.sleep(6.0)
        ht = host.log_text()
        check("R3 host announced FIELD -> SHOP0 -> FIELD, once each (no transient scene)",
              live_scenes(ht) == [SCENE_FG, SCENE_SHOP0, SCENE_FG])
        ct = client.log_text()
        seen = [int(m.group(1)) for m in re.finditer(r"\[NET\]\[SCENE\] client: player %d now in scene (\d+)" % HOST_ID, ct)]
        check("R3 client's view of the host: told SHOP0 (9) via the join replay (host was inside the shop when the "
              "client became READY), then FIELD (7) live when the host walked out -- sequence seen: %s" % seen,
              seen == [SCENE_SHOP0, SCENE_FG])
        check("R3 both processes alive after the round trip", host.alive() and client.alive())
    finally:
        client.stop()
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7810)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    results = []
    log_dir = os.path.join(HERE, "logs", "m9a")
    os.makedirs(log_dir, exist_ok=True)
    check = lambda d, c: L.check(d, c, results)
    for i, fn in enumerate((r1, r2, r3)):
        if args.only and args.only != fn.__name__:
            continue
        fn(args.port + i, log_dir, check)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
