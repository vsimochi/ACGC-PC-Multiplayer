#!/usr/bin/env python3
"""test_npc_lease_protocol.py - Patch 8: EXCLUSIVE villager interaction lease (outdoor AND indoor villagers, one host-owned lease table, NPC_LEASE id 70).

TIER: PROTOCOL TESTED against a REAL host process (`--host --bootstrap-resident 0`) driven by scripted FakeClients. Run on a DISPOSABLE fixture copy
(NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4*>). The real-game dialogue path is covered by test_npc_lease_real.py.
  L1 the table (NPC_LEASE) reaches every ready client at join: nobody owns anything
  L2 A begins a conversation: the host grants it, the table names A for BOTH clients
  L3 B begins on the SAME villager: DENIED (the host logs it), the table still names A
  L4 a near-simultaneous race on another villager: exactly ONE owner, the other DENIED
  L5 A ends: the table frees the villager, B can now take it (owner B)
  L6 the owner disconnects: the lease is released and the table says so
  L7 the owner leaves the town field: the lease is released
  L8 a late joiner is told the CURRENT owners at join
  L9 INDOOR: a client in that villager's house gets the lease; a second client in the same house is DENIED; a client in a DIFFERENT house cannot begin (scene check)
  L10 a client in the field cannot begin on a villager whose lease an indoor client owns (one table for both)
Usage: python test_npc_lease_protocol.py [--port 11780]
"""
import argparse
import os
import re
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
import test_client_villager_talk as VT  # noqa: E402
import test_player_scene_protocol as PS  # noqa: E402

MSG_NPC_LEASE = 70
FMT = "<BBHI16B"
SIZE = struct.calcsize(FMT)
NONE = 0xFF
SCENE_FG = 7
SCENE_NPC_HOUSE = PS.SCENE_NPC_HOUSE


def is_lease(m):
    return m.channel == L.CH_RELIABLE and len(m.payload) == SIZE and m.payload[0] == MSG_NPC_LEASE


def table(c, timeout=3.0, newer_than=None):
    """The newest NPC_LEASE table c has received (owner list) after waiting up to `timeout` for one newer than `newer_than` (seq)."""
    end = time.time() + timeout
    best = None
    while True:
        for m in c.inbox.peek_all(is_lease):
            t = struct.unpack(FMT, m.payload)
            if best is None or t[3] > best[0]:
                best = (t[3], list(t[4:]))
        if best is not None and (newer_than is None or best[0] > newer_than):
            return best
        if time.time() > end:
            return best
        L.pump_sleep(0.1)


def owner(c, slot, newer_than=None, timeout=3.0):
    t = table(c, timeout, newer_than)
    return (t[1][slot] if t else None), (t[0] if t else None)


def scene(c, scene_id, owner_id, seq):
    c.send_reliable(PS.build(0, scene_id, L.PC_NETGAME_SCENE_FLAG_IN_TOWN if scene_id == SCENE_FG else 0, owner_id, seq))


def run(port, log_dir, results):
    check = lambda desc, cond: L.check(desc, cond, results)  # noqa: E731
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"], log_path=os.path.join(log_dir, "npc_lease_proto_host.log")).start()
    clients = []
    try:
        if not host.wait_listening(60.0) or not host.boot_to_field(timeout=90.0, slot=0):
            check("host reached the field", False)
            return
        a = L.FakeClient("A", "127.0.0.1", port)
        a.connect_and_ready()
        b = L.FakeClient("B", "127.0.0.1", port)
        b.connect_and_ready()
        clients += [a, b]
        v = VT.host_villagers(host)
        check("the host spawned >= 2 regular villager actors (%d)" % len(v), len(v) >= 2)
        if len(v) < 2:
            return
        (s1, n1), (s2, n2) = v[0], v[1]
        scene(a, SCENE_FG, 0, 1)
        scene(b, SCENE_FG, 0, 1)
        L.pump_sleep(0.6)
        ta, tb = table(a), table(b)
        check("L1 both clients got the NPC_LEASE table at join and nobody owns anything", ta is not None and tb is not None and all(x == NONE for x in ta[1]) and all(x == NONE for x in tb[1]))
        base = ta[0]
        # L2
        a.send_reliable(VT.npc_talk_msg(s1, n1, True, 1))
        oa, seq = owner(a, s1, base)
        ob, _ = owner(b, s1, base)
        check("L2 A's conversation is granted: the table names A (%s) for both clients (B sees %s)" % (oa, ob), oa == a.assigned_peer_id and ob == a.assigned_peer_id)
        # L3
        m = len(host.log_text())
        b.send_reliable(VT.npc_talk_msg(s1, n1, True, 1))
        den = host.wait_for_log(r"\[NPC\]\[TALKNET\] DENIED peer=%d slot=%d npc=0x%04X" % (b.assigned_peer_id, s1, n1), 4.0, since_offset=m)
        L.pump_sleep(0.5)
        ob2, _ = owner(b, s1)
        check("L3 B's begin on the SAME villager is DENIED by the host and the table still names A (%s)" % ob2, den is not None and ob2 == a.assigned_peer_id)
        # L4 race on villager 2: both begin back to back; exactly one wins
        base = table(a)[0]
        a.send_reliable(VT.npc_talk_msg(s2, n2, True, 2))
        b.send_reliable(VT.npc_talk_msg(s2, n2, True, 2))
        L.pump_sleep(1.0)
        w, _ = owner(a, s2, base)
        w2, _ = owner(b, s2)
        check("L4 the race on villager 2 has exactly one owner (%s) and both clients agree (%s)" % (w, w2), w in (a.assigned_peer_id, b.assigned_peer_id) and w == w2)
        loser = b if w == a.assigned_peer_id else a
        check("L4 the other one was DENIED", re.search(r"\[NPC\]\[TALKNET\] DENIED peer=%d slot=%d" % (loser.assigned_peer_id, s2), host.log_text()) is not None)
        winner = a if loser is b else b
        # release villager 2 to keep the rest simple
        winner.send_reliable(VT.npc_talk_msg(s2, n2, False, 3))
        L.pump_sleep(0.5)
        # L5
        base = table(a)[0]
        a.send_reliable(VT.npc_talk_msg(s1, n1, False, 4))
        fo, _ = owner(b, s1, base)
        check("L5 A ends the conversation: the table frees the villager (%s)" % fo, fo == NONE)
        base = table(a)[0]
        b.send_reliable(VT.npc_talk_msg(s1, n1, True, 3))
        bo, _ = owner(a, s1, base)
        check("L5 B can now take the same villager (owner %s)" % bo, bo == b.assigned_peer_id)
        # L8 late joiner sees current owners
        c = L.FakeClient("C", "127.0.0.1", port)
        c.connect_and_ready()
        clients.append(c)
        tc = table(c)
        check("L8 a late joiner is told the CURRENT owner at join (B owns villager 1)", tc is not None and tc[1][s1] == b.assigned_peer_id)
        scene(c, SCENE_FG, 0, 1)
        # L6 owner disconnect
        base = table(a)[0]
        bid = b.assigned_peer_id
        b.disconnect()
        clients.remove(b)
        fo, _ = owner(a, s1, base, 20.0)
        check("L6 the owner (B) disconnected: its lease was released (table %s)" % fo, fo == NONE)
        # L7 owner leaves the field
        base = table(a)[0]
        a.send_reliable(VT.npc_talk_msg(s1, n1, True, 5))
        o, _ = owner(c, s1, base)
        check("L7 A holds villager 1 again (%s)" % o, o == a.assigned_peer_id)
        base = table(c)[0]
        scene(a, PS.SCENE_SHOP0, 0, 2)
        fo, _ = owner(c, s1, base)
        check("L7 A left the town field: the lease is released (%s)" % fo, fo == NONE)
        scene(a, SCENE_FG, 0, 3)
        L.pump_sleep(0.4)
        # L9 / L10 indoor
        base = table(c)[0]
        scene(a, SCENE_NPC_HOUSE, n1, 4)
        L.pump_sleep(0.4)
        a.send_reliable(VT.npc_talk_msg(s1, n1, True, 6))
        o, _ = owner(c, s1, base)
        check("L9 A INSIDE villager 1's own house gets the lease (%s)" % o, o == a.assigned_peer_id)
        scene(c, SCENE_NPC_HOUSE, n1, 2)
        L.pump_sleep(0.4)
        m = len(host.log_text())
        c.send_reliable(VT.npc_talk_msg(s1, n1, True, 2))
        den = host.wait_for_log(r"\[NPC\]\[TALKNET\] DENIED peer=%d slot=%d" % (c.assigned_peer_id, s1), 4.0, since_offset=m)
        check("L9 a second client in the SAME house is DENIED (the same lease table as outdoors)", den is not None)
        scene(c, SCENE_NPC_HOUSE, n2, 3)  # a DIFFERENT villager's house
        L.pump_sleep(0.4)
        m = len(host.log_text())
        c.send_reliable(VT.npc_talk_msg(s1, n1, True, 3))
        rj = host.wait_for_log(r"\[NPC\]\[TALKNET\] REJECT peer=%d slot=%d npc=0x%04X begin: peer is neither in the town field nor in that villager's house" % (c.assigned_peer_id, s1, n1), 4.0, since_offset=m)
        check("L9 a client inside a DIFFERENT villager's house cannot begin on villager 1 (scene check)", rj is not None)
        scene(c, SCENE_FG, 0, 4)
        L.pump_sleep(0.4)
        m = len(host.log_text())
        c.send_reliable(VT.npc_talk_msg(s1, n1, True, 4))
        den = host.wait_for_log(r"\[NPC\]\[TALKNET\] DENIED peer=%d slot=%d" % (c.assigned_peer_id, s1), 4.0, since_offset=m)
        check("L10 a client OUTDOORS cannot take the villager whose conversation an indoor client owns", den is not None)
        # leaving the house releases
        base = table(c)[0]
        scene(a, SCENE_FG, 0, 5)
        fo, _ = owner(c, s1, base)
        check("L9 the indoor owner left the house: the lease is released (%s)" % fo, fo == NONE)
        check("host alive throughout", host.alive())
    finally:
        for cl in clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11780)
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    run(args.port, HERE, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
