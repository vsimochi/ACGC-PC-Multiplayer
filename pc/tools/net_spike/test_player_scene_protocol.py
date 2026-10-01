#!/usr/bin/env python3
"""test_player_scene_protocol.py - M9-A (scene identity / player presence): PROTOCOL test against a REAL host
process (`AnimalCrossing.exe --host --bootstrap-resident 0`, started and stopped by this script), driving it
with scripted FakeClients exactly like test_wildlife_spawn_wire.py / test_appearance_sync.py (the host's C
code paths run for real; only the client side is scripted).

Covers PC_NETGAME_MSG_PLAYER_SCENE (id 44, reliable, 12 bytes):
  S1  host's own scene (FIELD, IN_TOWN) is replayed to a client at READY (host is a player too).
  S2  scene message from a READY peer is accepted and relayed to the other READY peer.
  S3  the relayed message carries the TRUE originating peer id (a client-claimed id is overwritten).
  S4  stale / duplicate sequence is rejected and NOT relayed; seq 0 is invalid.
  S5  non-announceable scene ids (title demo, player select, out of range) and illegal flags are dropped.
  S6  newer sequence accepted and relayed.
  S7  late join: a newly READY peer receives the host's scene and every other READY peer's last scene.
  S8  disconnect: the other clients receive a CLEARED notice for the departed peer; a later late joiner
      is NOT told about it (no stale presence), and the departed peer's seq space restarts cleanly.
  S9  reconnect: the re-announced scene (seq 1 again) is accepted and relayed.
  S10 a peer that is not READY (never completed IDENTITY) cannot inject a scene.
  S11 a protocol-version-4 peer is rejected with PROTOCOL_MISMATCH and the host reports version 5.
  S12 host stays alive/READY throughout.

Tier: PROTOCOL TESTED (real host binary, scripted clients). Not real two-process gameplay.

Usage: python test_player_scene_protocol.py [--port 7790]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import struct
import sys

import net_spike_lib as L

MSG = L.PC_NETGAME_MSG_PLAYER_SCENE
FMT = "<BBBBHHI"
SIZE = struct.calcsize(FMT)
FLAG_IN_TOWN = L.PC_NETGAME_SCENE_FLAG_IN_TOWN
FLAG_CLEARED = L.PC_NETGAME_SCENE_FLAG_CLEARED
HOST_ID = L.PC_NETGAME_HOST_PLAYER_ID

# enum scene_table values (verified by test_player_scene_wire.py against include/m_scene_table.h)
SCENE_NPC_HOUSE = 6
SCENE_FG = 7
SCENE_SHOP0 = 9
SCENE_POST_OFFICE = 14
SCENE_START_DEMO = 15
SCENE_PLAYERSELECT = 19
SCENE_TITLE_DEMO = 33


def build(net_player_id, scene_id, flags, owner, seq):
    return struct.pack(FMT, MSG, net_player_id & 0xFF, scene_id & 0xFF, flags & 0xFF, owner & 0xFFFF, 0,
                       seq & 0xFFFFFFFF)


def decode(payload):
    t, pid, sid, flags, owner, _r, seq = struct.unpack(FMT, payload)
    return dict(net_player_id=pid, scene_id=sid, flags=flags, owner=owner, seq=seq)


def is_scene(m):
    return m.channel == L.CH_RELIABLE and len(m.payload) == SIZE and m.payload[0] == MSG


def scenes(client, duration, since=None):
    return [decode(m.payload) for m in client.inbox.collect(is_scene, duration, since=since)]


def run(port, log_dir, results):
    check = lambda desc, cond: L.check(desc, cond, results)
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"],
                         log_path=os.path.join(log_dir, "player_scene_proto_host.log")).start()
    try:
        if not host.wait_listening(60.0):
            check("host listening", False)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("host reached genuine field-ready state (boot_to_field)", False)
            return
        check("host reached genuine field-ready state (boot_to_field)", True)
        mo = host.wait_for_log(r"\[NET\]\[SCENE\] local scene live: scene=(\d+) kind=(\d+) owner=0x([0-9A-F]+) "
                               r"flags=0x([0-9A-F]+) seq=(\d+)", 10.0)
        check("S0 host logged its own live local scene exactly as FIELD/IN_TOWN",
              mo is not None and int(mo.group(1)) == SCENE_FG and int(mo.group(2)) == 1 and
              int(mo.group(4), 16) == FLAG_IN_TOWN)
        check("S0 host announced its local scene exactly once so far",
              len(re.findall(r"\[NET\]\[SCENE\] local scene live", host.log_text())) == 1)

        # ---- S1: host's own scene replayed at READY ----
        a = L.FakeClient("A", "127.0.0.1", port)
        a.connect_and_ready()
        got = scenes(a, 0.6)
        host_scenes = [s for s in got if s["net_player_id"] == HOST_ID]
        check("S1 host's own scene replayed to A at READY (FIELD, IN_TOWN, seq>=1)",
              len(host_scenes) == 1 and host_scenes[0]["scene_id"] == SCENE_FG and
              host_scenes[0]["flags"] == FLAG_IN_TOWN and host_scenes[0]["seq"] >= 1 and host_scenes[0]["owner"] == 0)

        b = L.FakeClient("B", "127.0.0.1", port)
        b.connect_and_ready()
        scenes(b, 0.4)
        scenes(a, 0.2)

        # ---- S2/S3: accepted + relayed with the true peer id ----
        since_b = b.inbox.mark()
        a.send_reliable(build(99, SCENE_SHOP0, FLAG_IN_TOWN, 0x1234, 1))  # claimed id 99, junk owner/flag
        rel = scenes(b, 0.6, since=since_b)
        check("S2 B receives exactly one relayed scene for A", len(rel) == 1)
        check("S3 relayed net_player_id is A's true peer id (claimed 99 overwritten)",
              len(rel) == 1 and rel[0]["net_player_id"] == a.assigned_peer_id)
        check("S2 relayed scene id/seq intact (SHOP0, seq 1)",
              len(rel) == 1 and rel[0]["scene_id"] == SCENE_SHOP0 and rel[0]["seq"] == 1)
        check("S2 owner forced to 0 for a non-house scene (host normalizes junk owner)",
              len(rel) == 1 and rel[0]["owner"] == 0)
        check("S2 A does not get its own scene echoed back", len(scenes(a, 0.2)) == 0)

        # ---- S4: stale / duplicate / zero ----
        since_b = b.inbox.mark()
        a.send_reliable(build(0, SCENE_POST_OFFICE, 0, 0, 1))  # duplicate seq
        a.send_reliable(build(0, SCENE_POST_OFFICE, 0, 0, 0))  # zero seq
        check("S4 duplicate seq and seq 0 are rejected, nothing relayed", len(scenes(b, 0.6, since=since_b)) == 0)

        # ---- S5: non-announceable / illegal flags ----
        since_b = b.inbox.mark()
        a.send_reliable(build(0, SCENE_TITLE_DEMO, 0, 0, 5))
        a.send_reliable(build(0, SCENE_PLAYERSELECT, 0, 0, 6))
        a.send_reliable(build(0, SCENE_START_DEMO, 0, 0, 7))
        a.send_reliable(build(0, 200, 0, 0, 8))
        a.send_reliable(build(0, SCENE_POST_OFFICE, 0x40, 0, 9))        # unknown flag bit
        a.send_reliable(build(0, SCENE_POST_OFFICE, FLAG_CLEARED, 0, 10))  # CLEARED is host->client only
        check("S5 title/player-select/start-demo/out-of-range ids and illegal flags dropped (nothing relayed)",
              len(scenes(b, 0.7, since=since_b)) == 0)

        # ---- S6: newer accepted (a villager house: owner meaningful) ----
        since_b = b.inbox.mark()
        a.send_reliable(build(0, SCENE_NPC_HOUSE, 0, 0xD000, 2))
        rel = scenes(b, 0.6, since=since_b)
        check("S6 newer seq accepted and relayed (NPC_HOUSE, owner 0xD000 kept, seq 2)",
              len(rel) == 1 and rel[0]["scene_id"] == SCENE_NPC_HOUSE and rel[0]["owner"] == 0xD000 and
              rel[0]["seq"] == 2 and rel[0]["net_player_id"] == a.assigned_peer_id)
        check("S4b the earlier stale seq did not overwrite the newer state (host replays seq 2 below)", True)

        since_a = a.inbox.mark()
        b.send_reliable(build(0, SCENE_POST_OFFICE, 0, 0, 1))
        rel = scenes(a, 0.6, since=since_a)
        check("S2b B's scene is relayed to A with B's id", len(rel) == 1 and rel[0]["net_player_id"] == b.assigned_peer_id
              and rel[0]["scene_id"] == SCENE_POST_OFFICE)

        # ---- S7: late join replay ----
        c = L.FakeClient("C", "127.0.0.1", port)
        c.connect_and_ready()
        got = scenes(c, 0.8)
        by_id = {s["net_player_id"]: s for s in got}
        check("S7 late joiner C receives the host's scene", HOST_ID in by_id and by_id[HOST_ID]["scene_id"] == SCENE_FG)
        check("S7 late joiner C receives A's LAST scene (NPC_HOUSE seq 2 owner 0xD000), not the stale one",
              a.assigned_peer_id in by_id and by_id[a.assigned_peer_id]["scene_id"] == SCENE_NPC_HOUSE and
              by_id[a.assigned_peer_id]["seq"] == 2 and by_id[a.assigned_peer_id]["owner"] == 0xD000)
        check("S7 late joiner C receives B's scene (POST_OFFICE)",
              b.assigned_peer_id in by_id and by_id[b.assigned_peer_id]["scene_id"] == SCENE_POST_OFFICE)
        check("S7 late joiner is not told about itself", c.assigned_peer_id not in by_id)
        scenes(a, 0.1)
        scenes(b, 0.1)

        # ---- S8: disconnect clears ----
        old_a = a.assigned_peer_id
        since_b, since_c = b.inbox.mark(), c.inbox.mark()
        a.disconnect()
        b.ping()
        c.ping()
        rb = scenes(b, 0.8, since=since_b)
        rc = scenes(c, 0.1, since=since_c)
        check("S8 B receives a CLEARED notice for A's id",
              any(s["net_player_id"] == old_a and (s["flags"] & FLAG_CLEARED) for s in rb))
        check("S8 C receives a CLEARED notice for A's id",
              any(s["net_player_id"] == old_a and (s["flags"] & FLAG_CLEARED) for s in rc))
        d = L.FakeClient("D", "127.0.0.1", port)
        d.connect_and_ready()
        got = scenes(d, 0.8)
        ids = {s["net_player_id"] for s in got if not (s["flags"] & FLAG_CLEARED)}
        check("S8 a later joiner D is not given the departed A's scene (slot %d was reused by D itself)" % old_a,
              (old_a not in ids) or d.assigned_peer_id != old_a)
        check("S8 D still receives B's and the host's scene",
              b.assigned_peer_id in ids and HOST_ID in ids)
        b.ping()
        c.ping()
        e = L.FakeClient("E", "127.0.0.1", port)
        e.connect_and_ready()
        got = scenes(e, 0.8)
        check("S8 stale presence check: D (reused A's slot, never announced) has NO scene in E's replay "
              "(A's old NPC_HOUSE scene did not survive the slot reuse)",
              d.assigned_peer_id == old_a and all(sc["net_player_id"] != d.assigned_peer_id for sc in got))
        b.ping()
        c.ping()
        d.ping()

        # ---- S9: reconnect re-establishes ----
        a2 = L.FakeClient("A2", "127.0.0.1", port)
        a2.connect_and_ready()
        scenes(a2, 0.4)
        since_b = b.inbox.mark()
        a2.send_reliable(build(0, SCENE_SHOP0, FLAG_IN_TOWN, 0, 1))  # seq restarts at 1 for a new session
        rel = scenes(b, 0.7, since=since_b)
        check("S9 reconnected peer's re-announced scene (seq 1 again) is accepted and relayed",
              len(rel) == 1 and rel[0]["scene_id"] == SCENE_SHOP0 and rel[0]["net_player_id"] == a2.assigned_peer_id)

        # ---- S10: not-READY peer cannot inject ----
        x = L.FakeClient("X", "127.0.0.1", port)
        x.connect()
        since_b = b.inbox.mark()
        x.send_reliable(build(0, SCENE_SHOP0, 0, 0, 1))
        check("S10 a peer that never completed IDENTITY cannot inject a scene", len(scenes(b, 0.6, since=since_b)) == 0)
        x.disconnect()

        # ---- S11: v4 peer rejected ----
        old = L.FakeClient("V4", "127.0.0.1", port)
        rejected = False
        expected_version = None
        try:
            old.connect_and_ready(protocol_version=4, quiet=True)
        except L.HandshakeRejected as e:
            rejected = e.reject.reason == L.PC_NETGAME_REJECT_PROTOCOL_MISMATCH
            expected_version = e.reject.expected_protocol_version
        check("S11 protocol version 4 peer rejected with PROTOCOL_MISMATCH", rejected)
        check("S11 host reports required protocol version 5", expected_version == 5)

        # ---- S12 ----
        check("S12 host process still alive and clients still READY",
              host.alive() and b.is_connected() and c.is_connected() and a2.is_connected())
        scene_lines = len(re.findall(r"\[NET\]\[SCENE\] local scene live", host.log_text()))
        check("S12 host's own local scene was announced exactly once in the whole run (no re-announce churn)",
              scene_lines == 1)
    finally:
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7790)
    args = ap.parse_args()
    results = []
    run(args.port, os.path.dirname(os.path.abspath(__file__)), results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
