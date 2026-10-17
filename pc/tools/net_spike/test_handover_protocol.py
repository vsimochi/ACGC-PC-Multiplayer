#!/usr/bin/env python3
"""test_handover_protocol.py - HAND-OVER ANIMATIONS (message 103 HANDOVER, pc/include/pc_handover.h) against a REAL host process driven by scripted FakeClients.

TIER: PROTOCOL TESTED (a real `--host --bootstrap-resident 0` process of the new build, FakeClients A, B, C). Disposable fixture pc\\build64\\bin_fixture4_ho_proto (a fresh
copy of bin_fixture4 + the exe of pc\\build64_w3). Ports 39500-39999 only.
  H1 VILLAGER: A holds villager V's conversation (NPC_TALK BEGIN) and reports a hand-over (kind NPC_GIVES): the host accepts it (log), its held villager plays the clip
     steps, and B (town field) sees V's NPC_MOVE action_type become aNPC_ACTION_TYPE_TRANSFER, then TRANSFER_WAIT, and come back to the previous action afterwards.
     Nobody receives a 103 for a villager (client -> host only).
  H2 the take side (NPC_TAKES): B sees GET, GET_PULL, GET_PUTAWAY.
  H3 B (who does NOT hold V's conversation) reports a hand-over with V: dropped ("does not hold"), V's action is not touched.
  H4 malformed (kind 3, class 3, a shop message with a slot, a reserved flag, rsv != 0, npc_id 0, item 0, slot 15, a wrong npc_id for the slot) are dropped and logged;
     15 / 17-byte messages are dropped silently; A stays connected.
  H5 rate: 8 back-to-back valid messages -> at most 4 accepted in the 2 s window, the rest "rate".
  H6 SHOP: A and B announce Nook's shop, C the town field: A's shop hand-over reaches B exactly once (origin stamped with A's id, otherwise identical), not A, not C.
  H7 host alive, A / B still connected (MOVE streams).
Usage: python test_handover_protocol.py [--port 39510]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_ho_proto"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)

MSG = 103
FMT = "<BBBBHHBBHI"
assert struct.calcsize(FMT) == 16
GIVES, TAKES = 1, 2
VILLAGER, SHOP = 1, 2
NPC_MOVE_FMT = "<BBBBHHIfffhh"
SCENE_FG, SCENE_SHOP0 = 7, 9
HOST_ID = 8


def action_types():
    """the outdoor aNPC_ACTION_TYPE_* values (include/ac_npc.h, the first enum = !aNPC_NPC2)"""
    src = open(os.path.join(T.PC, "..", "include", "ac_npc.h"), encoding="utf-8", errors="replace").read()
    i = src.index("aNPC_ACTION_TYPE_TRANSFER_MOVE")
    a = src.rindex("enum {", 0, i)
    b = src.index("};", i)
    names = [x.strip().rstrip(",") for x in src[a + 6:b].split("\n") if x.strip().startswith("aNPC_ACTION_TYPE_")]
    return {n: k for k, n in enumerate(names)}


def pack(kind=GIVES, cls=VILLAGER, slot=0, npc=0, item=0x2A00, origin=0, flags=0, seq=1, rsv=0):
    return struct.pack(FMT, MSG, kind, cls, slot, npc, item, origin, flags, seq, rsv)


def ho(c, since=None):
    return [struct.unpack(FMT, m.payload) for m in c.inbox.peek_all(lambda m: len(m.payload) == 16 and m.payload[0] == MSG, since=since)]


def npc_actions(c, slot, since=None):
    out = []
    for m in c.inbox.peek_all(lambda m: len(m.payload) == 28 and m.payload[0] == 26 and m.payload[1] == slot, since=since):
        out.append(struct.unpack(NPC_MOVE_FMT, m.payload)[2])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=39510)
    args = ap.parse_args()
    if not (39500 <= args.port <= 39990):
        print("REFUSING: ports 39500-39999 only", file=sys.stderr)
        return 2
    if not os.path.isfile(W3_EXE):
        print("REFUSING: build pc\\build64_w3 first", file=sys.stderr)
        return 2
    T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
    import net_spike_lib as L
    import test_client_villager_talk as VT
    import test_player_scene_protocol as PS

    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    acts = action_types()
    TR, TRW, GET, GETP, GETW = (acts["aNPC_ACTION_TYPE_TRANSFER"], acts["aNPC_ACTION_TYPE_TRANSFER_WAIT"], acts["aNPC_ACTION_TYPE_GET"],
                                acts["aNPC_ACTION_TYPE_GET_PULL"], acts["aNPC_ACTION_TYPE_GET_PUTAWAY"])
    print("action types: TRANSFER %d TRANSFER_WAIT %d GET %d GET_PULL %d GET_PUTAWAY %d" % (TR, TRW, GET, GETP, GETW))

    def scene(c, sid, seq):
        c.send_reliable(PS.build(0, sid, L.PC_NETGAME_SCENE_FLAG_IN_TOWN if sid == SCENE_FG else 0, 0, seq))

    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0"], log_path=T.log_path("ho_proto_host.log")).start()
    clients = []
    try:
        if not host.wait_listening(60.0) or not host.boot_to_field(timeout=120.0, slot=0):
            ck("host reached the field", False)
            return L.summary_and_exit_code(results)
        a = L.FakeClient("A", "127.0.0.1", args.port)
        a.connect_and_ready()
        b = L.FakeClient("B", "127.0.0.1", args.port)
        b.connect_and_ready()
        c = L.FakeClient("C", "127.0.0.1", args.port)
        c.connect_and_ready()
        clients += [a, b, c]
        for x in (a, b, c):
            scene(x, SCENE_FG, 1)
        L.pump_sleep(1.0)
        v = VT.host_villagers(host)
        ck("the host spawned >= 2 regular villager actors (%d)" % len(v), len(v) >= 2)
        if len(v) < 2:
            return L.summary_and_exit_code(results)
        (s1, n1), (s2, n2) = v[0], v[1]
        b.hub.wait_until(lambda: len(npc_actions(b, s1)) >= 3, 6.0)
        ck("B receives villager %d's NPC_MOVE stream (%d samples)" % (s1, len(npc_actions(b, s1))), len(npc_actions(b, s1)) >= 3)

        # ---- H1
        a.send_reliable(VT.npc_talk_msg(s1, n1, True, 1))
        held = host.wait_for_log(r"\[NPC\]\[TALKNET\] (?:BEGIN|GRANT|HOLD)[^\n]*slot=%d" % s1, 5.0)
        L.pump_sleep(1.0)
        before = npc_actions(b, s1)[-1] if npc_actions(b, s1) else None
        mk = b.inbox.mark()
        ma, mc = a.inbox.mark(), c.inbox.mark()
        off = len(host.log_text())
        a.send_reliable(pack(GIVES, VILLAGER, s1, n1, 0x2A00, seq=1))
        acc = host.wait_for_log(r"\[NET\]\[HANDOVER\] host: peer %d villager slot %d npc 0x%04X NPC_GIVES item 0x2A00 -> the held villager plays it" % (a.assigned_peer_id, s1, n1),
                                5.0, since_offset=off)
        ck("H1 the host accepted A's villager hand-over (A holds the conversation; hold line %s)" % bool(held), acc is not None)
        b.hub.wait_until(lambda: TRW in npc_actions(b, s1, mk), 6.0)
        L.pump_sleep(2.5)
        seq_b = npc_actions(b, s1, mk)
        dedup = [x for i, x in enumerate(seq_b) if i == 0 or seq_b[i - 1] != x]
        ck("H1 B saw V's NPC_MOVE action go TRANSFER (%d) -> TRANSFER_WAIT (%d) -> back to %s (%s)" % (TR, TRW, before, dedup),
           TR in dedup and TRW in dedup and dedup.index(TR) < dedup.index(TRW) and dedup[-1] == before)
        lg = host.log_text()[off:]
        ck("H1 the host logged the clip steps of its held villager (step 1 / 2 / done)",
           re.search(r"host: villager slot %d npc 0x%04X clip step 2" % (s1, n1), lg) and re.search(r"host: villager slot %d npc 0x%04X clip step 6 \(done" % (s1, n1), lg))
        ck("H1 nobody receives a 103 for a villager (A %d / B %d / C %d)" % (len(ho(a, ma)), len(ho(b, mk)), len(ho(c, mc))), not ho(a, ma) and not ho(b, mk) and not ho(c, mc))

        # ---- H2
        mk = b.inbox.mark()
        off = len(host.log_text())
        a.send_reliable(pack(TAKES, VILLAGER, s1, n1, 0x2A00, seq=2))
        b.hub.wait_until(lambda: GETW in npc_actions(b, s1, mk), 8.0)
        L.pump_sleep(1.5)
        seq_b = npc_actions(b, s1, mk)
        dedup = [x for i, x in enumerate(seq_b) if i == 0 or seq_b[i - 1] != x]
        ck("H2 the take side: B saw GET (%d) -> GET_PULL (%d) -> GET_PUTAWAY (%d) -> back (%s)" % (GET, GETP, GETW, dedup),
           all(x in dedup for x in (GET, GETP, GETW)) and dedup.index(GET) < dedup.index(GETP) < dedup.index(GETW) and dedup[-1] == before)

        # ---- H3
        L.pump_sleep(2.2)  # a fresh rate window for B too
        off = len(host.log_text())
        mk = b.inbox.mark()
        b.send_reliable(pack(GIVES, VILLAGER, s1, n1, 0x2A00, seq=1))
        rej = host.wait_for_log(r"\[NET\]\[HANDOVER\] host: peer %d hand-over dropped \(the sender does not hold that villager's conversation\)" % b.assigned_peer_id, 4.0, since_offset=off)
        L.pump_sleep(1.0)
        ck("H3 B (no conversation with V) is refused and V's action is untouched (%s)" % sorted(set(npc_actions(b, s1, mk))),
           rej is not None and TR not in npc_actions(b, s1, mk))
        b.send_reliable(pack(GIVES, VILLAGER, s2, n2, 0x2A00, seq=2))
        rej2 = host.wait_for_log(r"peer %d hand-over dropped \(the sender does not hold that villager's conversation\) class=1 slot=%d" % (b.assigned_peer_id, s2), 4.0, since_offset=off)
        ck("H3 a villager nobody talks to is refused too", rej2 is not None)

        # ---- H4
        L.pump_sleep(2.2)
        off = len(host.log_text())
        bad = [pack(3, VILLAGER, s1, n1), pack(GIVES, 3, s1, n1), pack(GIVES, SHOP, 0, 0xD001), pack(GIVES, VILLAGER, s1, n1, flags=2)]
        for p in bad:
            a.send_reliable(p)
        L.pump_sleep(2.2)
        bad2 = [pack(GIVES, VILLAGER, s1, n1, rsv=1), pack(GIVES, VILLAGER, s1, 0), pack(GIVES, VILLAGER, s1, n1, item=0), pack(GIVES, VILLAGER, 15, n1)]
        for p in bad2:
            a.send_reliable(p)
        L.pump_sleep(2.2)
        a.send_reliable(pack(GIVES, VILLAGER, s1, (n1 + 1) & 0xFFFF))
        a.send_reliable(pack(GIVES, VILLAGER, s1, n1)[:15])
        a.send_reliable(pack(GIVES, VILLAGER, s1, n1) + b"\x00")
        L.pump_sleep(1.5)
        lg = host.log_text()[off:]
        nmal = lg.count("hand-over dropped (malformed)")
        ck("H4 the 8 malformed messages are dropped as malformed (%d)" % nmal, nmal == 8)
        ck("H4 a wrong npc_id for the slot is dropped", "hand-over dropped (no such villager in that slot)" in lg)
        ck("H4 the wrong sizes are dropped silently, nothing accepted", "-> the held villager plays it" not in lg)

        # ---- H5
        L.pump_sleep(2.2)
        off = len(host.log_text())
        for i in range(8):
            a.send_reliable(pack(GIVES, VILLAGER, s1, n1, seq=10 + i))
        L.pump_sleep(1.5)
        lg = host.log_text()[off:]
        nacc, nrate = lg.count("-> the held villager plays it"), lg.count("hand-over dropped (rate)")
        ck("H5 rate: %d accepted, %d refused (rate) of 8 back-to-back" % (nacc, nrate), nacc == 4 and nrate == 4)
        a.send_reliable(VT.npc_talk_msg(s1, n1, False, 2))
        L.pump_sleep(3.0)

        # ---- H6
        scene(a, SCENE_SHOP0, 2)
        scene(b, SCENE_SHOP0, 2)
        L.pump_sleep(1.0)
        ma, mb, mc = a.inbox.mark(), b.inbox.mark(), c.inbox.mark()
        off = len(host.log_text())
        sent = pack(GIVES, SHOP, 0xFF, 0xD001, 0x2A00, seq=5)
        a.send_reliable(sent)
        b.hub.wait_until(lambda: len(ho(b, mb)) >= 1, 4.0)
        L.pump_sleep(1.0)
        hb = ho(b, mb)
        exp = list(struct.unpack(FMT, sent))
        exp[6] = a.assigned_peer_id
        ck("H6 B (same shop) received exactly one copy, stamped with A's id %d and otherwise identical (%s vs %s)" % (a.assigned_peer_id, hb, exp),
           len(hb) == 1 and list(hb[0]) == exp)
        ck("H6 the sender A and C (town field) received none (%d / %d)" % (len(ho(a, ma)), len(ho(c, mc))), not ho(a, ma) and not ho(c, mc))
        ck("H6 the host logged the shop relay to 1 peer", re.search(r"host: shop hand-over from %d \(NPC_GIVES item 0x2A00 keeper 0xD001 scene 9\) -> 1 peer" % a.assigned_peer_id,
                                                            host.log_text()[off:]) is not None)
        a.send_reliable(pack(GIVES, SHOP, 0xFF, 0xD001, 0x2A00, origin=HOST_ID, seq=6))
        b.hub.wait_until(lambda: len(ho(b, mb)) >= 2, 4.0)
        hb = ho(b, mb)
        ck("H6 a forged origin is re-stamped with A's id (%s)" % [x[6] for x in hb], len(hb) == 2 and hb[1][6] == a.assigned_peer_id)

        # ---- H7
        ck("H7 host alive, A / B / C still connected", host.alive() and all(x.is_connected() if hasattr(x, "is_connected") else True for x in (a, b, c)))
        lg = host.log_text()
        ck("H7 no protocol violation for 103", "violation" not in lg.split("[NET][HANDOVER]", 1)[-1] or "protocol violation" not in lg)
    finally:
        for cl in clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        host.stop()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
