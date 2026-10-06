#!/usr/bin/env python3
"""test_room_npc_protocol.py - Patch 2 (indoor villagers): PC_NETGAME_MSG_ROOM_NPC (id 67, unreliable, 28 bytes) -- the pose of the villager standing in its own house
(SCENE_NPC_HOUSE, owner = the villager's npc_id) is sampled by ONE in-room process (the room's pose holder), validated and relayed by the host to the OTHER occupants of that exact room.

TIER: PROTOCOL TESTED against a REAL host process (`--host --bootstrap-resident 0`), driven by scripted FakeClients exactly like test_player_scene_protocol.py. The client-side actor
hooks (ac_npc2_move.c_inc: follow / sample) are covered by the source audit below, NOT by this test; no visual / two-process gameplay claim.
Run on a DISPOSABLE fixture copy: NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_roomnpc>.
  R1 a pose from A (in house 0xD000) is relayed to B (same house) intact, tagged with A's true player id; A gets no echo
  R2 a client standing in a DIFFERENT house (0xD001) receives nothing
  R3 B (same house) posts while A holds the lease: dropped (A receives nothing)
  R4 a client that never announced the room cannot post a pose into it (nobody receives)
  R5 stale / duplicate frame, non-finite position, a non-house scene id and a zero frame are dropped
  R6 hand-over: A leaves the room, B's pose is now accepted and relayed to the new occupant C
  R7 lease expiry: the holder stays in the room but goes silent > 1.5 s -> another occupant's pose is accepted
  R8 the host is still alive and every client READY
  S  source audit: the actor hooks keep schedule / action / angle code for the simulating process, never override a local talk, and key the room on SCENE_NPC_HOUSE + house_owner_name
"""
import argparse
import math
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
import test_player_scene_protocol as PS  # noqa: E402

MSG_ROOM_NPC = 67
FMT = "<BBBBHHIfffhh"
SIZE = struct.calcsize(FMT)
SCENE_NPC_HOUSE, SCENE_FG, SCENE_SHOP0 = PS.SCENE_NPC_HOUSE, PS.SCENE_FG, PS.SCENE_SHOP0
OWNER = 0xD000
OTHER = 0xD001
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def pose(frame, x=100.0, y=0.0, z=50.0, ang=0x4000, action=2, owner=OWNER, npc=None, scene=SCENE_NPC_HOUSE):
    return struct.pack(FMT, MSG_ROOM_NPC, scene & 0xFF, action & 0xFF, 0, owner & 0xFFFF, (owner if npc is None else npc) & 0xFFFF, frame & 0xFFFFFFFF, x, y, z, ang, 0)


def decode(payload):
    t, scene, action, sender, owner, npc, frame, x, y, z, ang, _r = struct.unpack(FMT, payload)
    return dict(scene=scene, action=action, sender=sender, owner=owner, npc=npc, frame=frame, x=x, y=y, z=z, ang=ang)


def is_pose(m):
    return m.channel == L.CH_UNRELIABLE and len(m.payload) == SIZE and m.payload[0] == MSG_ROOM_NPC


def poses(client, duration, since=None):
    return [decode(m.payload) for m in client.inbox.collect(is_pose, duration, since=since)]


def enter(client, scene, owner, seq):
    client.send_reliable(PS.build(0, scene, 0, owner, seq))


def audit_source(check):
    mv = open(os.path.join(ROOT, "src", "actor", "npc", "ac_npc2_move.c_inc"), encoding="utf-8", errors="replace").read()
    net = open(os.path.join(ROOT, "pc", "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read()
    check("S1 the room villager is keyed on SCENE_NPC_HOUSE and house_owner_name == the actor's npc_id",
          "play->scene_id != SCENE_NPC_HOUSE" in mv and "Common_Get(house_owner_name)" in mv and "animal->id.npc_id != owner" in mv)
    check("S2 a local talk / interaction state is never overridden (follow and sample both bail)", mv.count("aNPC_pc_room_talking(nactorx)") >= 2)
    check("S3 the follower skips schedule / action / angle code; the simulating process keeps them and samples",
          "if (!pc_room_follow) {" in mv and "aNPC_pc_room_sample(nactorx, play);" in mv and "aNPC_schedule_proc(nactorx, play);" in mv)
    check("S4 the host validates the sender announced that room, holds a lease and relays only to in-room peers",
          "pcnetgame_room_player_in_room(from, m->scene_id, m->owner)" in net and "PCNG_ROOM_NPC_LEASE_MS" in net and "(PCNetPlayerId)i != from" in net)
    check("S5 the message is dispatched on both the host and the client paths", net.count("PC_NETGAME_MSG_ROOM_NPC") >= 4)


def run(port, log_dir, results):
    check = lambda desc, cond: L.check(desc, cond, results)  # noqa: E731
    audit_source(check)
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"], log_path=os.path.join(log_dir, "room_npc_proto_host.log")).start()
    try:
        if not host.wait_listening(60.0) or not host.boot_to_field(timeout=90.0, slot=0):
            check("host reached the field", False)
            return
        check("host reached the field", True)
        a = L.FakeClient("A", "127.0.0.1", port)
        a.connect_and_ready()
        b = L.FakeClient("B", "127.0.0.1", port)
        b.connect_and_ready()
        c = L.FakeClient("C", "127.0.0.1", port)
        c.connect_and_ready()
        for cl in (a, b, c):
            PS.scenes(cl, 0.3)
        enter(a, SCENE_NPC_HOUSE, OWNER, 1)
        enter(b, SCENE_NPC_HOUSE, OWNER, 1)
        enter(c, SCENE_NPC_HOUSE, OTHER, 1)
        L.pump_sleep(0.6)
        for cl in (a, b, c):
            poses(cl, 0.1)

        # ---- R1 / R2
        sb, sc, sa = b.inbox.mark(), c.inbox.mark(), a.inbox.mark()
        a.send_unreliable(pose(1, x=111.5, z=77.25, ang=0x2000, action=5))
        rb = poses(b, 0.7, since=sb)
        check("R1 B receives exactly one relayed pose", len(rb) == 1)
        check("R1 position / facing / action intact", len(rb) == 1 and abs(rb[0]["x"] - 111.5) < 1e-3 and abs(rb[0]["z"] - 77.25) < 1e-3 and rb[0]["ang"] == 0x2000 and rb[0]["action"] == 5)
        check("R1 tagged with A's TRUE player id, owner / npc identity kept", len(rb) == 1 and rb[0]["sender"] == a.assigned_peer_id and rb[0]["owner"] == OWNER and rb[0]["npc"] == OWNER)
        check("R1 A gets no echo of its own pose", len(poses(a, 0.2, since=sa)) == 0)
        check("R2 a client in a DIFFERENT house receives nothing", len(poses(c, 0.2, since=sc)) == 0)

        # ---- R3
        sa = a.inbox.mark()
        b.send_unreliable(pose(1, x=5.0))
        check("R3 the non-holder's pose is dropped while A holds the lease", len(poses(a, 0.7, since=sa)) == 0)

        # ---- R4
        sa, sb = a.inbox.mark(), b.inbox.mark()
        c.send_unreliable(pose(50, owner=OWNER, x=9.0))  # C stands in house 0xD001, claims 0xD000
        check("R4 a client that is not in the room cannot post into it", len(poses(a, 0.5, since=sa)) == 0 and len(poses(b, 0.1, since=sb)) == 0)

        # ---- R5
        sb = b.inbox.mark()
        a.send_unreliable(pose(1, x=1.0))                       # duplicate frame
        a.send_unreliable(pose(0, x=2.0))                       # zero frame
        a.send_unreliable(pose(9, x=float("nan")))              # non-finite
        a.send_unreliable(pose(10, scene=SCENE_FG))             # not a villager house
        check("R5 stale / zero / non-finite / wrong-scene poses are dropped", len(poses(b, 0.7, since=sb)) == 0)
        sb = b.inbox.mark()
        a.send_unreliable(pose(20, x=33.0))
        rb = poses(b, 0.6, since=sb)
        check("R5 a newer frame from the holder is still relayed afterwards", len(rb) == 1 and abs(rb[0]["x"] - 33.0) < 1e-3)

        # ---- R6: hand-over: A leaves the room, C joins it, B becomes the holder
        enter(a, SCENE_FG, 0, 2)
        enter(c, SCENE_NPC_HOUSE, OWNER, 2)
        L.pump_sleep(0.6)
        for cl in (a, b, c):
            poses(cl, 0.1)
        sc = c.inbox.mark()
        b.send_unreliable(pose(100, x=44.0))
        rc = poses(c, 0.7, since=sc)
        check("R6 after the holder left the room, B's pose is accepted and relayed to the new occupant C",
              len(rc) == 1 and abs(rc[0]["x"] - 44.0) < 1e-3 and rc[0]["sender"] == b.assigned_peer_id)

        # ---- R7: lease expiry (B stays in the room, silent > 1.5 s)
        L.pump_sleep(1.8)
        sb = b.inbox.mark()
        c.send_unreliable(pose(7, x=55.0))
        rb = poses(b, 0.7, since=sb)
        check("R7 the silent holder's lease expired: C's pose is accepted and relayed to B", len(rb) == 1 and abs(rb[0]["x"] - 55.0) < 1e-3 and rb[0]["sender"] == c.assigned_peer_id)

        # ---- R8
        for cl in (a, b, c):
            cl.ping()
        check("R8 the host is alive and every client is still READY", host.alive() and a.is_connected() and b.is_connected() and c.is_connected())
        check("R8 the host logged the lease holders", len(re.findall(r"\[NET\]\[ROOMNPC\] host: room scene \d+ owner 0x[0-9A-F]+ pose holder is now player \d+", host.log_text())) >= 3)
    finally:
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7795)
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    run(args.port, HERE, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
