#!/usr/bin/env python3
"""test_lib_loopback.py - Game-free unit + loopback tests of net_spike_lib.py itself.

Three layers, all deterministic (no randomness):
  U*  pure unit tests: golden wire bytes straight from the foundation contract, u32 serial
      arithmetic, ReliableReceiver/ReliableSender logic with hand-computed ACK/SACK values, Inbox
      semantics, request-id matching, WorldView world_seq rules, town addressing round trip, and a
      scripted lossy/duplicating/reordering in-memory link.
  E*  end-to-end over real UDP on 127.0.0.1: FakeClient/TransportClient against the Python model
      host (net_transport_model.ModelHost, same thread via the Hub), exercising every fault-injection
      API and reconnect/restart semantics.
  S*  script validation: runs the real-host test scripts (test_transport_reliability.py,
      test_reconnect_state.py, test_inventory_correctness.py, ...) as subprocesses against a model host
      subprocess -- including deliberately BUGGY models (`--buggy-stale-cache`: per-peer caches survive a
      reconnect; `--buggy-single-phase`: the OLD pickup/drop behaviour -- field mutated at REQUEST time,
      INTERACT_CONFIRM ignored, NaN positions bypass the reach checks) -- to prove those scripts pass on a
      correct host and FAIL on the buggy ones (i.e. they are not vacuous).

Usage: python test_lib_loopback.py [--skip-scripts]
"""
import math
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import net_spike_lib as L  # noqa: E402
from net_transport_model import EchoApp, ModelGameApp, ModelHost, MODEL_HOST_TOWN  # noqa: E402

results = []


def check(desc, cond):
    L.check(desc, cond, results)


# ================================================================================================
# U: pure unit tests
# ================================================================================================

def u_codec():
    # RDATA: header(type 5, kind 1, size = payload only) + u32 seq + payload
    pkt = L.encode_rdata(7, b"\x06\x01")
    check("U1 RDATA golden bytes", pkt == bytes.fromhex("504e4341" "05" "01" "0200" "07000000" "0601"))
    # ACK: header(type 6, size 8) + next_expected + sack
    check("U1 ACK golden bytes", L.encode_ack(3, 0b1011) == bytes.fromhex("504e4341" "06" "00" "0800" "03000000" "0b000000"))
    check("U1 HELLO with nonce golden bytes", L.encode_hello(0xDEADBEEF) == bytes.fromhex("504e4341" "00" "00" "0400" "efbeadde"))
    check("U1 legacy size-0 HELLO", L.encode_hello(None) == bytes.fromhex("504e4341" "00" "00" "0000"))
    check("U1 unreliable DATA unchanged (type 4 kind 0)", L.encode_data(b"\x04") == bytes.fromhex("504e4341" "04" "00" "0100" "04"))
    check("U1 DISCONNECT", L.encode_ctrl(L.PCNET_WIRE_DISCONNECT) == bytes.fromhex("504e4341" "03" "00" "0000"))
    p = L.decode_packet(pkt)
    check("U1 RDATA decode round trip", p.wtype == 5 and p.kind == 1 and p.size == 2 and p.seq == 7 and p.payload == b"\x06\x01")
    a = L.decode_packet(L.encode_ack(0xFFFFFFFF, 0x80000001))
    check("U1 ACK decode round trip", a.ack_next == 0xFFFFFFFF and a.ack_sack == 0x80000001)
    check("U1 HELLO_ACK nonce decode", L.decode_packet(L.encode_hello(5, L.PCNET_WIRE_HELLO_ACK)).nonce == 5)
    check("U1 malformed: short / bad magic / truncated RDATA / short ACK rejected",
          L.decode_packet(b"\x50\x4e\x43") is None
          and L.decode_packet(b"\x00" * 8) is None
          and L.decode_packet(pkt[:-1]) is None
          and L.decode_packet(L.encode_packet(L.PCNET_WIRE_ACK, 0, b"\x00" * 4)) is None)


def u_confirm_codec():
    b = L.build_interact_confirm(L.CONFIRM_KIND_DROP, 0x01020304, L.CONFIRM_OUTCOME_ABORT, L.CONFIRM_REASON_SLOT_CHANGED)
    check("U11 INTERACT_CONFIRM golden bytes (type 17, kind 2, ABORT, SLOT_CHANGED, request_id LE)",
          b == bytes.fromhex("11" "02" "00" "02" "04030201") and len(b) == 8)
    g = L.decode_game(b)
    check("U11 decode_game(INTERACT_CONFIRM) round trip",
          g is not None and (g.kind, g.outcome, g.reason, g.request_id) == (2, 0, 2, 0x01020304))
    check("U11 constants match the contract (type 17; kinds 1/2; outcome 1=COMMIT 0=ABORT; reasons 0..5; 20000 ms)",
          L.PC_NETGAME_MSG_INTERACT_CONFIRM == 17 and (L.CONFIRM_KIND_PICKUP, L.CONFIRM_KIND_DROP) == (1, 2)
          and (L.CONFIRM_OUTCOME_COMMIT, L.CONFIRM_OUTCOME_ABORT) == (1, 0)
          and [L.CONFIRM_REASON_NONE, L.CONFIRM_REASON_POCKETS_FULL, L.CONFIRM_REASON_SLOT_CHANGED,
               L.CONFIRM_REASON_STATE_CHANGED, L.CONFIRM_REASON_STALE, L.CONFIRM_REASON_CANCELLED] == [0, 1, 2, 3, 4, 5]
          and L.PC_NETGAME_CONFIRM_TIMEOUT_MS == 20000)
    check("U11 a 7-byte / 9-byte confirm-shaped payload is not decoded (exact-size dispatch)",
          L.decode_game(b[:7]) is None and L.decode_game(b + b"\x00") is None)
    nan_move = L.build_move_any(7, float("nan"), 1e30, L.F32_NEG_INF, angle=-5, speed=L.F32_SNAN)
    gm = L.decode_game(nan_move)
    check("U11 build_move_any: 28 bytes, NaN / 1e30 / -Inf / raw signalling-NaN speed survive the wire",
          len(nan_move) == 28 and gm is not None and math.isnan(gm.pos_x)
          and gm.pos_y == struct.unpack("<f", struct.pack("<f", 1e30))[0]
          and gm.pos_z == -math.inf and gm.frame == 7 and gm.facing_angle == -5 and math.isnan(gm.speed)
          and nan_move[24:28] == bytes.fromhex("0000a07f"))
    check("U11 build_move_any is byte-identical to build_move for ordinary floats",
          L.build_move_any(9, 1.5, 2.5, 3.5, angle=100, speed=2.0) == L.build_move(9, 1.5, 2.5, 3.5, angle=100, speed=2.0))


def u_log_evidence():
    """The host-log regexes of test_inventory_correctness.py, run against the formats the real host prints."""
    import re
    import test_inventory_correctness as T
    log = "\n".join([
        "[NET][DROP] host: peer 0 request 9012 reserved tile (24,24) item=0x2801 (field unchanged until CONFIRM)",
        "[NET][DROP] host: peer 0 request 9012 reservation released: aborted by client (reason=2) (tile 24,24)",
        "[NET] host: peer 0 ignored stale/invalid INTERACT_CONFIRM (kind=2 outcome=1 reason=0 request 9012)",
        "[NET][PICKUP] host: peer 1 request 9020 reserved tile (40,24) item=0x2800 (field unchanged until CONFIRM)",
        "[NET][PICKUP] host: peer 1 request 9020 committed tile (40,24) 0x2800 -> 0x0000",
        "[NET][PICKUP] host: peer 2 request 90200 reserved tile (56,24) item=0x2800 (field unchanged until CONFIRM)",
        "[NET][PICKUP] host: peer 2 request 90200 reservation released: expired (no CONFIRM within the reservation "
        "timeout) (tile 56,24)",
        "[NET] host: peer 0: dropped invalid MOVE sample (frame 5 pos=(-nan(ind),0,300) speed=0) [1 dropped so far]",
        "[NET] host: peer 0: dropped invalid MOVE sample (frame 6 pos=(240,1e+30,300) speed=0) [2 dropped so far]"])
    ev = [("A", [T.rx_reserved(2, 9012), T.rx_released(2, 9012, "abort:2")], [T.rx_committed(2, 9012)]),
          ("commit present", [T.rx_reserved(1, 9020), T.rx_committed(1, 9020)], []),
          ("must_not violated", [T.rx_reserved(1, 9020)], [T.rx_committed(1, 9020)]),
          ("wrong reason", [T.rx_released(2, 9012, "abort:1")], []),
          ("rid 9020 must not match 90200's expiry", [T.rx_released(1, 9020, "expired")], []),
          ("rid 90200 expired", [T.rx_released(1, 90200, "expired")], [T.rx_committed(1, 90200)])]
    got = [ok for _d, ok in T.evaluate_evidence(log, ev)]
    check("U12 host-log evidence regexes: match the exact log formats, honour reason/kind/rid (no rid-prefix matches)",
          got == [True, True, False, False, False, True])
    mo = list(re.finditer(T.RX_DROPPED_MOVE, log))
    check("U12 dropped-MOVE-sample regex parses peer, frame and the 'dropped so far' counter (incl. -nan(ind))",
          [(m.group(1), m.group(2), m.group(4)) for m in mo] == [("0", "5", "1"), ("0", "6", "2")])


def u_serial():
    check("U2 seq_diff wraps", L.seq_diff(0, 0xFFFFFFFF) == 1 and L.seq_diff(0xFFFFFFFF, 0) == -1)
    check("U2 seq_lt across wrap", L.seq_lt(0xFFFFFFFE, 1) and not L.seq_lt(1, 0xFFFFFFFE))
    check("U2 seq_add wraps", L.seq_add(0xFFFFFFFF, 2) == 1)


def u_receiver():
    r = L.ReliableReceiver()
    st, out = r.on_rdata(0, b"a")
    check("U3 in-order seq 0 delivered", st == "new" and out == [(0, b"a")] and r.next_expected == 1)
    r.ack_pending = False
    st, out = r.on_rdata(0, b"a")
    check("U3 duplicate (< next_expected) suppressed but re-triggers ACK", st == "dup" and out == [] and r.ack_pending)
    r.on_rdata(1, b"b")
    r.on_rdata(2, b"c")
    st4, _ = r.on_rdata(4, b"e")
    st5, _ = r.on_rdata(5, b"f")
    st7, _ = r.on_rdata(7, b"h")
    check("U3 out-of-order seqs buffered", (st4, st5, st7) == ("buffered",) * 3 and r.next_expected == 3)
    check("U3 duplicate of a buffered seq suppressed", r.on_rdata(5, b"f")[0] == "dup")
    # received: 0,1,2 | 4,5,7  -> next_expected 3, SACK bit0=seq4, bit1=seq5, bit3=seq7 = 0b1011
    check("U3 ACK = (3, 0b1011) hand-computed", r.build_ack() == (3, 0b1011))
    st, out = r.on_rdata(3, b"d")
    check("U3 gap fill delivers 3,4,5 in order", st == "new" and [s for s, _ in out] == [3, 4, 5] and r.next_expected == 6)
    check("U3 ACK after gap fill = (6, 0b1) (seq 7 still SACKed)", r.build_ack() == (6, 0b1))
    r.on_rdata(6, b"g")
    check("U3 delivered_seqs contiguous 0..7", r.delivered_seqs == list(range(8)))
    far = L.ReliableReceiver(max_ahead=64)
    check("U3 seq beyond window ignored (too_far) but ACK pending", far.on_rdata(64, b"x")[0] == "too_far" and far.ack_pending)


def u_pretend_missing():
    r = L.ReliableReceiver()
    for s in range(6):
        r.on_rdata(s, b"%d" % s)
    check("U4 pretend-missing seq 3 of 0..5 -> ACK (3, 0b11)", r.build_ack({3}) == (3, 0b11))
    check("U4 without pretend -> (6, 0)", r.build_ack() == (6, 0))
    r.on_rdata(8, b"8")
    check("U4 pretend-missing a buffered seq hides only its SACK bit", r.build_ack({8}) == (6, 0) and r.build_ack() == (6, 0b10))


def u_sender():
    s = L.ReliableSender(rto=0.2)
    seqs = [s.alloc(b"p%d" % i) for i in range(6)]
    check("U5 sender seqs start at 0", seqs == [0, 1, 2, 3, 4, 5])
    for q in seqs:
        s.mark_tx(q, 100.0)
    newly = s.on_ack(2, 0b101)  # cumulative 0,1 ; SACK bit0 -> seq3, bit2 -> seq5
    check("U5 on_ack cumulative + SACK", newly == [0, 1, 3, 5] and sorted(s.unacked) == [2, 4])
    check("U5 nothing due before RTO", s.due(100.1) == [])
    check("U5 due after RTO, oldest first", s.due(100.25) == [2, 4])
    check("U5 ACK for never-sent seqs ignored", s.on_ack(99, 0) == [] and s.invalid_acks == 1 and sorted(s.unacked) == [2, 4])
    w = s.alloc(b"withheld", withheld=True)
    check("U5 withheld seq never auto-retransmitted", w not in s.due(1e9) and s.withheld() == [w])
    b = L.ReliableSender(rto=0.15, rto_max=1.0, backoff=2.0)
    q = b.alloc(b"x")
    rtos = []
    for k in range(6):
        b.mark_tx(q, float(k))
        rtos.append(round(b.unacked[q].rto, 3))
    check("U5 backoff doubles to cap (model host policy)", rtos == [0.15, 0.3, 0.6, 1.0, 1.0, 1.0])


class _FakeHub:
    def __init__(self):
        self.pumps = 0

    def wait_until(self, cond, timeout):
        self.pumps += 1
        return cond()

    def sleep(self, s):
        pass


def u_inbox():
    ib = L.Inbox(_FakeHub())
    for i, mt in enumerate([5, 7, 8, 7, 5]):
        ib.push(0.0, 1, L.CH_RELIABLE, i, bytes([mt]) + b"\x00" * 3)
    m = ib.wait_for(L.p_msg_type(8), 0.1)
    check("U6 wait_for returns the match", m is not None and m.seq == 2)
    check("U6 wait_for never discards unrelated messages", [x.seq for x in ib.peek_all()] == [0, 1, 3, 4])
    mk = ib.mark()
    ib.push(0.0, 1, L.CH_RELIABLE, 5, b"\x07")
    check("U6 mark()/since() counts only new arrivals", ib.count(L.p_msg_type(7), since=mk) == 1 and ib.count(L.p_msg_type(7)) == 3)
    taken = ib.take_all(L.p_msg_type(5))
    check("U6 take_all consumes only matches", [x.seq for x in taken] == [0, 4] and len(ib) == 3)
    check("U6 wait_for miss returns None and leaves inbox", ib.wait_for(L.p_msg_type(99), 0.01) is None and len(ib) == 3)


def u_request_matching():
    c = L.FakeClient.__new__(L.FakeClient)  # no socket needed for this unit test
    c.inbox = L.Inbox(_FakeHub())
    c.connect_count = 1
    c.result_log = []

    def res(rid, acc, x, z, conn=1):
        payload = struct.pack(L.PICKUP_RESULT_FMT, L.PC_NETGAME_MSG_PICKUP_RESULT, acc, x, z, rid, 0x2800 if acc else 0, 0)
        m = c.inbox.push(0.0, conn, L.CH_RELIABLE, None, payload)
        c.on_message(m)

    res(11, 0, 1, 1)
    res(12, 1, 2, 2)
    res(11, 0, 1, 1)
    res(12, 1, 9, 9, conn=0)  # stale result from a previous connection
    g = c.wait_result(L.PC_NETGAME_MSG_PICKUP_RESULT, 12, 0.01)
    check("U7 wait_result matches exact request_id on the current connection", g is not None and g.ut_x == 2)
    check("U7 other request ids left untouched", c.inbox.count(L.p_msg_type(L.PC_NETGAME_MSG_PICKUP_RESULT)) == 3)
    check("U7 count_results sees duplicates per rid", c.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, 11) == 2
          and c.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, 12) == 1
          and c.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, 12, current_conn_only=False) == 2)
    check("U7 compat recv_pickup_result tuple", c.recv_pickup_result(0.01, expect_request_id=11) == (0, 1, 1, 11, 0))


def u_worldview():
    w = L.WorldView()
    items = [0] * 256
    items[5] = 0x2800
    check("U8 snapshot block applies", w.apply_block(L.FieldBlock(0, 3, 10, items, [0] * 16)) and w.tile(3, 5) == 0x2800)
    check("U8 newer delta applies", w.apply_delta(L.FieldDelta(0, 3, 5, 0, 12, None)) and w.tile(3, 5) == 0)
    check("U8 OLDER snapshot block does not overwrite newer delta", not w.apply_block(L.FieldBlock(0, 3, 11, items, None))
          and w.tile(3, 5) == 0)
    check("U8 delta older than/equal to applied seq ignored", not w.apply_delta(L.FieldDelta(0, 3, 6, 0x2801, 12, None))
          and w.tile(3, 6) == 0)
    check("U8 newer snapshot block overwrites", w.apply_block(L.FieldBlock(0, 3, 13, items, None)) and w.tile(3, 5) == 0x2800)
    bad = list(items)
    bad[9] = 0xF001  # DUMMY_MAILBOX0: TRANSIENT (pcfa_transient_kind)
    w.apply_block(L.FieldBlock(0, 4, 14, bad, None))
    w.apply_delta(L.FieldDelta(0, 4, 1, 5, 9, None))
    check("U8 transient value + non-increasing delta seq recorded as violations",
          any("transient" in v for v in w.violations) and any("not increasing" in v for v in w.violations))
    n_before = len(w.violations)
    ok_amb = list(items)
    ok_amb[3] = L.RSV_NO  # AMBIGUOUS: may legitimately be sent once settled -> not a violation
    w.apply_block(L.FieldBlock(0, 5, 30, ok_amb, None))
    check("U8 settled AMBIGUOUS value (RSV_NO) is NOT flagged", len(w.violations) == n_before)
    w.apply_delta(L.FieldDelta(0, 7, 16 * 2 + 3, 0x2800, 40, 1))
    check("U8 delta deposit bit set at (ux=3, uz=2)", w.deposit[(0, 7)][2] == 1 << 3)
    # valid[] mask: tile 5 (row 0 bit 5) marked unknown -> the view keeps its own value; its garbage
    # transient content is not a violation either
    v2 = L.WorldView()
    v2.apply_block(L.FieldBlock(0, 9, 1, [0x2800] * 256, [0] * 16, [0xFFFF] * 16))
    masked = [0x1111] * 256
    masked[5] = 0xF001
    valid = [0xFFFF] * 16
    valid[0] &= ~(1 << 5) & 0xFFFF
    v2.apply_block(L.FieldBlock(0, 9, 2, masked, [0] * 16, valid))
    check("U8 FIELD_BLOCK valid=0 tile keeps the local value; valid tiles overwritten; no false violation",
          v2.tile(9, 5) == 0x2800 and v2.tile(9, 6) == 0x1111 and not v2.violations)
    # decode_world_msg on real v2 bytes
    fu = struct.pack(L.FIELD_UPDATE_FMT, L.PC_NETGAME_MSG_FIELD_UPDATE, 0, 23, 136, 3, 0, 0x2801, 77)
    kind, d = L.decode_world_msg(fu)
    check("U8 decode FIELD_UPDATE v2 (12 B): acre/tile/value/seq/deposit-on",
          kind == L.WORLD_DELTA and (d.acre, d.tile, d.value, d.world_seq, d.deposit) == (23, 136, 0x2801, 77, 1)
          and L.field_update_tuple(L.decode_game(fu)) == (72, 88, 0x2801))
    fb = struct.pack(L.FIELD_BLOCK_HDR_FMT, L.PC_NETGAME_MSG_FIELD_BLOCK, 0, 4, 1, 9, 50) + \
        struct.pack("<256H16H16H", *([7] * 256 + [1] * 16 + [0xFFFF] * 16))
    kind, b = L.decode_world_msg(fb)
    check("U8 decode FIELD_BLOCK (588 B)", len(fb) == 588 and kind == L.WORLD_BLOCK and b.acre == 4 and b.epoch == 9
          and b.world_seq == 50 and b.flags == 1 and b.items[255] == 7 and b.deposit[0] == 1)


def u_addressing():
    ok = True
    for acre in range(L.ACRE_NUM):
        for tile in range(L.TILE_NUM):
            ut = L.acre_tile_to_town_ut(acre, tile)
            if L.town_ut_to_acre_tile(*ut) != (acre, tile):
                ok = False
    check("U9 acre/tile <-> town ut round trip for all 30x256", ok)
    check("U9 contract mapping ax = ut_x/16 - 1 (offset constant)", L.town_ut_to_acre_tile(16, 16) == (0, 0)
          and L.town_ut_to_acre_tile(8, 8) is None and L.town_ut_to_acre_tile(16 * 5 + 15, 16 * 6 + 15) == (29, 255))


def u_lossy_link():
    """Two sender/receiver pairs over a scripted link: every 3rd datagram dropped, every 5th
    duplicated, every 4th held back one slot (reorder). 200 messages must arrive exactly once, in
    order, using only cumulative+SACK ACKs and RTO retransmits."""
    tx = L.ReliableSender(rto=0.05)
    rx = L.ReliableReceiver()
    wire, held, delivered = [], [], []
    n_dgram = 0
    now = 0.0
    msgs = [b"m%03d" % i for i in range(200)]
    queue = list(msgs)
    for _step in range(20000):
        now += 0.01
        while queue and tx.backlog() < 32:
            seq = tx.alloc(queue.pop(0))
            tx.mark_tx(seq, now)
            wire.append((seq, tx.unacked[seq].payload))
        for seq in tx.due(now):
            tx.mark_tx(seq, now)
            wire.append((seq, tx.unacked[seq].payload))
        batch, wire = wire, []
        out_batch = []
        for d in batch:
            n_dgram += 1
            if n_dgram % 3 == 0:
                continue
            if n_dgram % 4 == 0:
                held.append(d)
                continue
            out_batch.append(d)
            if n_dgram % 5 == 0:
                out_batch.append(d)
            if held:
                out_batch.append(held.pop(0))
        for seq, p in out_batch:
            _st, out = rx.on_rdata(seq, p)
            delivered.extend(pp for _s, pp in out)
        if rx.ack_pending:
            n_dgram += 1
            ne, sack = rx.build_ack()
            rx.ack_pending = False
            if n_dgram % 7 != 0:  # lose some ACKs too
                tx.on_ack(ne, sack)
        if len(delivered) == len(msgs) and tx.backlog() == 0:
            break
    check("U10 scripted lossy/dup/reorder link: 200 msgs exactly once, in order", delivered == msgs)
    check("U10 duplicates were actually exercised", rx.counts["dup"] > 0 and rx.counts["buffered"] > 0)


# ================================================================================================
# E: end-to-end over UDP loopback against the in-process model host
# ================================================================================================

def e_tests():
    hub = L.Hub()
    echo = EchoApp()
    host = ModelHost(port=0, app=echo, hub=hub, timeout=1.5)
    port = host.port

    c = L.TransportClient("E-client", "127.0.0.1", port, hub=hub)
    c.connect()
    check("E1 HELLO carries nonce and HELLO_ACK echoes it", c.hello_ack_nonce == c.nonce and c.nonce != 0)
    check("E1 host slot allocated", host.peer_count() == 1)

    s0 = c.send_reliable(b"one")
    s1 = c.send_reliable(b"two")
    hub.wait_until(lambda: c.inbox.count(lambda m: m.channel == L.CH_RELIABLE) >= 2, 1.0)
    got = [m.payload for m in c.inbox.take_all(lambda m: m.channel == L.CH_RELIABLE)]
    check("E2 reliable round trip; client seqs start at 0", (s0, s1) == (0, 1) and got == [b"ECHO:one", b"ECHO:two"])
    check("E2 host->client stream delivered 0,1 in order", c.receiver.delivered_seqs == [0, 1])
    check("E2 our sends ACKed by host", c.wait_acked(s1, 1.0) and c.sender.backlog() == 0)

    # duplicate seq -> host delivers once
    before = len(echo.got)
    c.send_reliable_duplicate(b"dup", copies=3)
    hub.sleep(0.3)
    check("E3 same seq sent 3x -> delivered once", [p for _, p in echo.got[before:]] == [b"dup"]
          and host.stats["rdata_dup"] >= 2)

    # out of order -> host delivers in order
    before = len(echo.got)
    c.send_reliable_out_of_order([b"A", b"B", b"C"])
    hub.sleep(0.3)
    check("E4 seqs sent C,B,A -> host delivers A,B,C", [p for _, p in echo.got[before:]] == [b"A", b"B", b"C"])

    # withheld gap -> host SACK bits, no delivery past the gap
    before = len(echo.got)
    g = c.send_reliable_withheld(b"G1")
    c.send_reliable(b"G2")
    c.send_reliable(b"G3")
    hub.sleep(0.3)
    ne, sack = c.latest_ack()[1:]
    check("E5 gap: nothing delivered past it", echo.got[before:] == [])
    check("E5 gap: host ACK = (gap seq, 0b11)", (ne, sack) == (g, 0b11))
    c.release_withheld()
    hub.sleep(0.3)
    check("E5 gap released -> G1,G2,G3 in order", [p for _, p in echo.got[before:]] == [b"G1", b"G2", b"G3"])

    # skip ACK once -> host retransmits, client dedups
    c.inbox.clear()
    c.skip_ack_next(lambda p: p == b"ECHO:retx")
    c.send_reliable(b"retx")
    hub.sleep(0.6)
    seq = c.seq_of(lambda p: p == b"ECHO:retx")
    check("E6 skip-ACK: host retransmitted (>=1 extra copy observed)", seq is not None and c.host_retransmits_of(seq) >= 1)
    check("E6 skip-ACK: message seen exactly once by the test", c.inbox.count(lambda m: m.payload == b"ECHO:retx") == 1)
    check("E6 skip-ACK: host eventually got its ACK", hub.wait_until(lambda: host.slots[0].sender.backlog() == 0, 1.0))

    # drop first inbound copy + SACK out-of-order delivery on the client
    c.inbox.clear()
    c.drop_inbound_next(lambda p: p == b"ECHO:d1")
    c.send_reliable(b"d1")
    c.send_reliable(b"d2")
    hub.wait_until(lambda: c.inbox.count(lambda m: m.payload.startswith(b"ECHO:d")) >= 2, 2.0)
    order = [m.payload for m in c.inbox.take_all(lambda m: m.payload.startswith(b"ECHO:d"))]
    check("E7 dropped first copy recovered by host retransmit; client delivers d1,d2 in order",
          order == [b"ECHO:d1", b"ECHO:d2"] and len(c.dropped_inbound) == 1)
    check("E7 host->client stream still contiguous", c.delivered_in_order())

    # duplicate + bogus ACKs tolerated
    c.send_ack_now(repeat=5)
    c.send_raw_ack(0x7FFFFFF0, 0)
    c.send_reliable(b"after-acks")
    got = c.inbox.wait_for(lambda m: m.payload == b"ECHO:after-acks", 1.0)
    check("E8 duplicate/bogus ACKs tolerated (bogus counted invalid)", got is not None and host.stats["acks_invalid"] == 1)

    # legacy reliable DATA dropped
    before = len(echo.got)
    c.send_legacy_reliable_data(b"legacy")
    hub.sleep(0.2)
    check("E9 legacy DATA kind=1 dropped by host", echo.got[before:] == [] and host.stats["legacy_reliable_dropped"] == 1)

    # clean disconnect frees the slot immediately; reconnect gets slot 0 with fresh seqs
    first_conn = host.conn_id(0)
    c.disconnect()
    hub.sleep(0.05)
    check("E10 DISCONNECT frees the host slot promptly", host.peer_count() == 0)
    c.inbox.clear()
    c.connect()
    s = c.send_reliable(b"fresh")
    hub.wait_until(lambda: c.inbox.count(lambda m: m.channel == L.CH_RELIABLE) >= 1, 1.0)
    check("E10 reconnect: new nonce, slot 0 reused by a NEW logical peer",
          host.slots[0] is not None and host.conn_id(0) != first_conn and c.connect_count == 2)
    check("E10 reconnect: both directions restart at seq 0", s == 0 and c.receiver.delivered_seqs == [0])

    # same-address restart (new nonce, no DISCONNECT)
    ev_before = len(host.events)
    conn_before = host.conn_id(0)
    c.restart_same_address()
    s = c.send_reliable(b"restarted")
    hub.wait_until(lambda: c.inbox.count(lambda m: m.payload == b"ECHO:restarted") >= 1, 1.0)
    new_events = [(e[1], e[2]) for e in host.events[ev_before:]]
    check("E11 same-address new nonce -> host DISCONNECTED then CONNECTED same slot",
          new_events == [("DISCONNECTED", 0), ("CONNECTED", 0)] and host.conn_id(0) != conn_before)
    check("E11 restart: seqs reset both ways", s == 0 and c.receiver.delivered_seqs == [0])

    # withhold all ACKs -> host exhausts its retransmit budget and disconnects us
    host.max_retransmits = 3
    c.withhold_acks(True)
    c.send_reliable(b"budget")
    t0 = time.monotonic()
    dis = c.wait_disconnected(5.0)
    seq = c.seq_of(lambda p: p == b"ECHO:budget")
    check("E12 withheld ACKs: host retransmitted then DISCONNECTed us",
          dis and c.disconnected_by_host and seq is not None and c.host_retransmits_of(seq) == 3)
    check("E12 host counted a budget disconnect", host.stats["budget_disconnects"] == 1)
    info_t = time.monotonic() - t0
    L.info(f"E12 budget disconnect after {info_t:.2f}s with max_retransmits=3")
    host.max_retransmits = L.HOST_MAX_RETRANSMITS

    # go silent -> host times out (model timeout 1.5 s here)
    c.connect()
    hub.sleep(0.1)
    c.go_silent()
    rx_before = c.stats["rx_heartbeat"]
    hub.sleep(1.0)
    check("E13 host keeps talking during early silence (heartbeats)", c.stats["rx_heartbeat"] > rx_before)
    hub.sleep(1.0)
    check("E13 silence past timeout frees the host slot", host.peer_count() == 0)
    c.resume()
    before = len(echo.got)
    c.send_reliable(b"ghost")
    hub.sleep(0.3)
    check("E13 after timeout the host ignores the stale peer (no delivery, no ACK)",
          echo.got[before:] == [] and host.stats["unknown_peer_RDATA"] >= 1)

    # new source port reconnect
    c.new_socket()
    c.connect()
    check("E14 reconnect from a new UDP port works", c.is_connected() and host.peer_count() == 1)

    # nonce-less legacy HELLO: refused with DISCONNECT, no slot, no HELLO_ACK
    peers_before = host.peer_count()
    raw = L.TransportClient("E-legacy", "127.0.0.1", port, hub=hub)
    raw.state = raw.STATE_PENDING
    raw.nonce = None
    raw.send_raw(L.encode_hello(None), force=True)
    got = hub.wait_until(lambda: raw.stats["rx_disconnect"] > 0, 1.0)
    check("E16 nonce-less (size-0) HELLO -> DISCONNECT, no HELLO_ACK, no slot",
          got and raw.stats["rx_hello_ack"] == 0 and host.peer_count() == peers_before)
    raw.close()
    # a v2 client refuses a size-0 HELLO_ACK answering its nonce HELLO (legacy host)
    lc = L.TransportClient("E-legacy-ack", "127.0.0.1", port, hub=hub)
    lc.state = lc.STATE_PENDING
    lc.nonce = 1234
    lc._handle_datagram(L.encode_hello(None, L.PCNET_WIRE_HELLO_ACK), time.monotonic())
    check("E17 client refuses a size-0 HELLO_ACK (stays PENDING)",
          lc.state == lc.STATE_PENDING and lc.stats["hello_ack_legacy_refused"] == 1)
    lc.close()

    # bystander liveness: an idle client stays connected while another is driven for > timeout
    idle = L.TransportClient("E-idle", "127.0.0.1", port, hub=hub)
    idle.connect()
    hub.sleep(2.0)  # > model timeout 1.5 s; Hub pumping keeps heartbeats flowing
    check("E15 idle bystander kept alive by Hub pumping (no manual heartbeats)", host.peer_count() == 2)

    c.close()
    idle.close()
    hub.unregister(host)
    host.sock.close()


def e_game_tests():
    """FakeClient game-layer helpers against the model game app."""
    hub = L.Hub()
    app = ModelGameApp(seed_fixture=True)
    host = ModelHost(port=0, app=app, hub=hub)
    a = L.FakeClient("GA", "127.0.0.1", host.port, hub=hub)
    b = L.FakeClient("GB", "127.0.0.1", host.port, hub=hub)
    a.connect_and_ready(quiet=True)
    b.connect_and_ready(quiet=True)
    check("G1 handshake: IDENTITY_ACK assigned slots 0/1", (a.assigned_peer_id, b.assigned_peer_id) == (0, 1))
    check("G1 host town visible in IDENTITY_ACK", L.town_from_identity_ack(a.identity_ack) == MODEL_HOST_TOWN)
    check("G1 other traffic (host APPEARANCE) kept in inbox, not discarded",
          a.inbox.count(L.p_msg_type(L.PC_NETGAME_MSG_APPEARANCE)) == 1)
    check("G1 v2 handshake waited for a complete 30-acre snapshot (world view has the seeded apple)",
          a.snapshot_complete() and a.snapshots[-1]["blocks"] == list(range(30)) and a.world.tile_ut(24, 24) == 0x2800)
    r = a.pickup(24, 24, 101)
    check("G2 pickup matched by request_id", r is not None and r.accepted == 1 and r.granted_item == 0x2800)
    check("G2 bystander B receives FIELD_UPDATE v2 (mapped back to ut)", (24, 24, 0) in b.drain_field_updates(0.2))
    check("G2 bystander B's world view applied it", b.world.tile_ut(24, 24) == 0 and not b.world.violations)
    a.send_pickup_request(24, 24, 101)  # game-level duplicate (new transport seq, same rid)
    hub.sleep(0.2)
    check("G3 game-level duplicate replays cached answer (2 results for rid 101)",
          a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, 101) == 2)
    a.send_reliable_duplicate(L.build_pickup_request(255, 255, 102))  # transport duplicate
    hub.sleep(0.3)
    check("G4 transport duplicate delivered once (1 result for rid 102)",
          a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, 102) == 1)
    town = L.probe_host_town("127.0.0.1", host.port, hub=hub)
    check("G5 probe_host_town learns host town and frees its slot", town == MODEL_HOST_TOWN and host.peer_count() == 2)
    bad = L.FakeClient("GBAD", "127.0.0.1", host.port, hub=hub)
    try:
        bad.connect_and_ready(protocol_version=999, quiet=True)
        rejected = False
    except L.HandshakeRejected as e:
        rejected = e.reject.reason == L.PC_NETGAME_REJECT_PROTOCOL_MISMATCH
    check("G6 wrong protocol version -> HandshakeRejected(PROTOCOL_MISMATCH)", rejected)
    check("G6 host DISCONNECTs the rejected client", bad.wait_disconnected(1.0) and bad.disconnected_by_host)
    wrong = L.FakeClient("GWRONG", "127.0.0.1", host.port, hub=hub, town=L.PROBE_TOWN)
    try:
        wrong.connect_and_ready(quiet=True)
        ok = False
    except L.HandshakeRejected as e:
        ok = L.is_land_mismatch_reject(e.reject) and L.town_from_reject(e.payload) == MODEL_HOST_TOWN
    check("G7 wrong town -> 24-byte LAND_MISMATCH carrying the host town", ok)
    # --- two-phase pickup/drop through the lib helpers ------------------------------------------------------------
    mark_b = b.inbox.mark()
    r = a.pickup(40, 40, 301, auto_confirm=False)
    check("G20 pickup(auto_confirm=False): provisional accepted RESULT, granted item echoed",
          r is not None and r.accepted == 1 and r.granted_item == 0x2800)
    hub.sleep(0.3)
    check("G20 ...and NO FIELD_UPDATE reaches anyone while unconfirmed (field not mutated at REQUEST time)",
          b.field_updates_since(mark_b) == [] and not any(c[:2] == (1, 301) for c in a.confirms_sent))
    rival = b.pickup(40, 40, 302)
    check("G20 a rival's request for the reserved tile is rejected", rival is not None and not rival.accepted)
    a.confirm(L.CONFIRM_KIND_PICKUP, 301, L.CONFIRM_OUTCOME_COMMIT)
    check("G20 CONFIRM(COMMIT) commits: observers get the FIELD_UPDATE (tile -> empty)",
          hub.wait_until(lambda: (40, 40, 0) in [L.field_update_tuple(g) for g in b.field_updates_since(mark_b)], 1.0))
    mark_b = b.inbox.mark()
    r = a.drop(2, 0x2801, 40, 40, 304, claim_at=(40, 40), auto_confirm=False)
    a.confirm(L.CONFIRM_KIND_DROP, 304, L.CONFIRM_OUTCOME_ABORT, L.CONFIRM_REASON_SLOT_CHANGED)
    hub.sleep(0.3)
    a.send_drop_request(2, 0x2801, 40, 40, 304)
    retry = a.wait_result(L.PC_NETGAME_MSG_DROP_RESULT, 304, 0.5)
    check("G21 drop provisional accept + ABORT(SLOT_CHANGED): no FIELD_UPDATE, a retry of the id is rejected",
          r is not None and r.accepted == 1 and b.field_updates_since(mark_b) == [] and retry is not None
          and not retry.accepted)
    a.pickup(56, 40, 305)
    a.send_pickup_request(56, 40, 305)  # a replayed provisional RESULT must not be confirmed a second time
    hub.sleep(0.3)
    check("G22 default auto_confirm sends exactly one CONFIRM(COMMIT) per accepted result (replays are not re-confirmed)",
          sum(1 for c in a.confirms_sent if c[:2] == (1, 305)) == 1)
    nx, ny, nz = L.tile_center(56, 56)
    a.claim_position(nx, ny, nz)
    a.send_move_any(float("nan"), ny, nz)
    hub.sleep(0.1)
    far = a.pickup(72, 88, 306, claim=False)
    near = a.pickup(56, 56, 307, claim=False)  # inside the 50-unit reach of the last VALID position
    check("G23 the model drops a NaN MOVE (last valid position stays): a far tile is rejected, a near one accepted",
          far is not None and not far.accepted and near is not None and near.accepted == 1)
    a.send_pickup_request(255, 255, 308)
    busy = not a.quiet_for(L.p_msg_type(L.PC_NETGAME_MSG_PICKUP_RESULT), 0.3)
    check("G24 quiet_for: True over an idle window, False when a matching message arrives in it",
          a.quiet_for(L.p_msg_type(L.PC_NETGAME_MSG_FIELD_UPDATE), 0.2) and busy)
    noctx = L.FakeClient("GNOCTX", "127.0.0.1", host.port, hub=hub, context_flags=None)
    noctx.connect_and_ready(quiet=True)
    r = noctx.pickup(40, 24, 201)
    check("G8 pickup without PLAYER_CONTEXT(IN_TOWN) rejected", r is not None and not r.accepted)
    noctx.send_player_context()
    r = noctx.pickup(40, 24, 202)
    check("G8 ...accepted once the context is sent", r is not None and r.accepted)
    n0 = len(noctx.completed_snapshots())
    noctx.send_resync_request()
    check("G9 RESYNC_REQUEST -> a new complete snapshot epoch", noctx.wait_snapshot_complete(3.0, after_count=n0)
          and noctx.completed_snapshots()[-1]["epoch"] != noctx.completed_snapshots()[0]["epoch"])
    for x in (a, b, bad, wrong, noctx):
        x.close()
    hub.unregister(host)
    host.sock.close()


# ================================================================================================
# S: validate the real-host scripts against model host subprocesses
# ================================================================================================

def _run_against_model(script, port, model_args, script_timeout=120, script_args=()):
    model = subprocess.Popen([sys.executable, os.path.join(HERE, "net_transport_model.py"), "--port", str(port),
                              "--duration", str(script_timeout + 10), "--quiet"] + model_args,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = model.stdout.readline()
        if "hosting" not in line:
            return None, "model host failed to start: " + line
        r = subprocess.run([sys.executable, os.path.join(HERE, script), "127.0.0.1", str(port)] + list(script_args),
                           capture_output=True, text=True, timeout=script_timeout, cwd=os.path.dirname(HERE))
        return r.returncode, r.stdout + r.stderr
    finally:
        model.terminate()
        try:
            model.wait(5)
        except subprocess.TimeoutExpired:
            model.kill()


def s_tests():
    rc, out = _run_against_model("test_transport_reliability.py", 47811, ["--seed-fixture"])
    print("\n".join("    | " + ln for ln in (out or "").splitlines()))
    check("S1 test_transport_reliability.py passes against the correct model host", rc == 0)
    rc, out = _run_against_model("test_reconnect_state.py", 47812, ["--seed-fixture"])
    print("\n".join("    | " + ln for ln in (out or "").splitlines()))
    check("S2 test_reconnect_state.py passes against the correct model host", rc == 0)
    rc, out = _run_against_model("test_reconnect_state.py", 47813, ["--seed-fixture", "--buggy-stale-cache"])
    print("\n".join("    | " + ln for ln in (out or "").splitlines()))
    check("S3 test_reconnect_state.py FAILS against the buggy (stale per-peer cache) model host", rc == 1)
    # Migrated / new scripts: plumbing + logic check against the model's v1 game subset. (test_drop_sync.py
    # is excluded: the model deliberately skips the host's item-class and reach validation, which 3 of
    # its checks exercise.)
    port = 47820
    for script, expect_rc, tail in (("test_version_mismatch.py", 0, "17/17 checks passed"),
                                     ("test_appearance_sync.py", 0, "20/20 checks passed"),
                                     ("test_pickup_sync.py", 0, "13/13 checks passed"),
                                     ("test_move_sync.py", 0, "DONE"),
                                     ("test_move_rotation.py", 0, "flipped"),
                                     ("test_field_sync.py", 0, "18/18 checks passed"),
                                     ("test_world_snapshot.py", 0, "checks passed"),
                                     ("test_host_faults.py", 0, "checks passed")):
        port += 1
        rc, out = _run_against_model(script, port, ["--seed-fixture"])
        last = [ln for ln in (out or "").splitlines() if ln.strip()][-1:] or ["<no output>"]
        print(f"    | {script}: exit={rc} last line: {last[0]}")
        if rc != expect_rc:
            print("\n".join("    | " + ln for ln in (out or "").splitlines()))
        ok = rc == expect_rc and tail in out and "FAIL -" not in out
        check(f"S4 {script} against the model: exit {expect_rc} ({tail}, no FAIL lines)", ok)


def _fail_names(out):
    """Names ('INV-A2', 'INV-D [..] pickup rejected ...') of every FAIL line of a script's output."""
    return [ln[len("FAIL - "):] for ln in (out or "").splitlines() if ln.startswith("FAIL - ")]


def _has_fail(fails, prefix):
    return any(f.startswith(prefix) for f in fails)


# Checks that MUST fail against the OLD single-phase behaviour (`--buggy-single-phase`), per group. (The group runs
# that need fresh fixture tiles -- D and expiry -- are run on their own model instance: the buggy model consumes
# tiles at REQUEST time and would otherwise exhaust the 20-tile fixture pool before they run.)
MUST_FAIL_SINGLE_PHASE_MAIN = [
    "INV-A2 ", "INV-A4 ", "INV-A5 ", "INV-A7 ", "INV-A9 ", "INV-B2 ", "INV-B4 ", "INV-B5 ", "INV-B7 ", "INV-Ci1 ",
    "INV-Ci2 ", "INV-Civ4 ", "INV-Civ9 ", "INV-Civ10 ", "INV-Cvi3 ", "INV-Cvi4 ", "INV-Cv2 [clean]",
    "INV-Cv5 [clean]", "INV-Cv2 [restart]", "INV-Cv5 [restart]", "INV-Ciii1 ", "INV-Ciii2 ", "INV-Ciii5 ",
    "INV-Ciii6 ", "INV-Ctimeout1 ", "INV-Ctimeout2 ",
]
# (the OLD host consumes a fixture tile on every accepted request, so only the first patterns are reachable before the
# 20-tile pool is exhausted; those are the ones asserted)
_D_PATS = ["NaN x] pickup", "speed 1e30 (finite but absurd)] pickup", "speed -1e30 (finite but absurd)] drop",
           "speed 5000 (> 1000 limit)] pickup", "NaN y] pickup", "NaN z] drop", "NaN x,y,z] pickup"]
MUST_FAIL_SINGLE_PHASE_D_NOMOVE = ["INV-D [no valid position ever; " + x for x in _D_PATS]
MUST_FAIL_SINGLE_PHASE_D_FAR = ["INV-D [far valid position; " + x for x in _D_PATS] + [
    "INV-D no non-finite / absurd sample was relayed"]
MUST_FAIL_SINGLE_PHASE_CROSS = ["INV-X3 ", "INV-X5 ", "INV-X7 ", "INV-X8 ", "INV-X9 ", "INV-X11 "]
MUST_FAIL_SINGLE_PHASE_EXPIRY = ["INV-C-expiry-3 ", "INV-C-expiry-4 ", "INV-C-expiry-6 ", "INV-C-expiry-7 "]


def s_inventory_tests():
    """test_inventory_correctness.py: passes on the corrected model; FAILS (the listed checks) on the OLD single-phase
    model and on the stale-cache model. Fixture: the 20 seeded town tiles; the model runs with a 1.5 s confirm timeout."""
    fast = ["--seed-fixture", "--confirm-timeout", "1.5"]
    sa = ["--expiry-s", "1.5"]
    rc, out = _run_against_model("test_inventory_correctness.py", 47850, fast, script_args=sa, script_timeout=240)
    print("\n".join("    | " + ln for ln in (out or "").splitlines() if not ln.startswith("PASS")))
    n_ok = (out or "").count("PASS - INV-")
    check(f"S5 test_inventory_correctness.py passes against the correct two-phase model ({n_ok} INV checks, no FAIL)",
          rc == 0 and "FAIL -" not in out and n_ok >= 100)
    rc, out = _run_against_model("test_inventory_correctness.py", 47851, fast + ["--buggy-single-phase"], script_args=sa,
                                 script_timeout=240)
    fails = _fail_names(out)
    print(f"    | buggy single-phase (all groups): exit={rc}, {len(fails)} failing check(s)")
    check("S6 test_inventory_correctness.py FAILS against the OLD single-phase model host (mutates at REQUEST time, "
          "ignores CONFIRM)", rc == 1 and len(fails) >= 30)
    missing = [m for m in MUST_FAIL_SINGLE_PHASE_MAIN if not _has_fail(fails, m)]
    check(f"S6 ...specifically the A / B / Ci / Civ / Cvi / Cv / Ciii / Ctimeout checks fail (missing: {missing})",
          not missing)
    rc, out = _run_against_model("test_inventory_correctness.py", 47852, fast + ["--buggy-single-phase"],
                                 script_args=sa + ["--only", "d-nomove"])
    fails_d = _fail_names(out)
    missing = [m for m in MUST_FAIL_SINGLE_PHASE_D_NOMOVE if not _has_fail(fails_d, m)]
    print(f"    | buggy single-phase (group d-nomove only): exit={rc}, {len(fails_d)} failing check(s)")
    check(f"S6 ...and a connection with only NaN/Inf/absurd MOVEs (D, no valid position) is accepted by the OLD host "
          f"-> the D checks fail (missing: {missing})", rc == 1 and not missing)
    rc, out = _run_against_model("test_inventory_correctness.py", 47856, fast + ["--buggy-single-phase"],
                                 script_args=sa + ["--only", "d-far,d-relay"])
    fails_d = _fail_names(out)
    missing = [m for m in MUST_FAIL_SINGLE_PHASE_D_FAR if not _has_fail(fails_d, m)]
    print(f"    | buggy single-phase (groups d-far,d-relay): exit={rc}, {len(fails_d)} failing check(s)")
    check(f"S6 ...and the far-valid-position + invalid-sample checks and the 'not relayed' check fail on the OLD host "
          f"(missing: {missing})", rc == 1 and not missing)
    rc, out = _run_against_model("test_inventory_correctness.py", 47853, fast + ["--buggy-single-phase"],
                                 script_args=sa + ["--only", "expiry"])
    fails_x = _fail_names(out)
    missing = [m for m in MUST_FAIL_SINGLE_PHASE_EXPIRY if not _has_fail(fails_x, m)]
    print(f"    | buggy single-phase (group expiry only): exit={rc}, {len(fails_x)} failing check(s)")
    check(f"S6 ...and the expiry checks fail on the OLD host (missing: {missing})", rc == 1 and not missing)
    rc, out = _run_against_model("test_inventory_correctness.py", 47857, fast + ["--buggy-single-phase"],
                                 script_args=sa + ["--only", "cross"])
    fails_c = _fail_names(out)
    missing = [m for m in MUST_FAIL_SINGLE_PHASE_CROSS if not _has_fail(fails_c, m)]
    print(f"    | buggy single-phase (group cross only): exit={rc}, {len(fails_c)} failing check(s)")
    check(f"S6 ...and the cross-kind coexistence checks fail on the OLD host (missing: {missing})",
          rc == 1 and not missing)
    rc, out = _run_against_model("test_inventory_correctness.py", 47854, fast + ["--buggy-stale-cache"], script_args=sa)
    fails_s = _fail_names(out)
    print(f"    | buggy stale-cache: exit={rc}, failing: {[f[:14] for f in fails_s]}")
    check("S7 test_inventory_correctness.py FAILS against the stale-cache model host (a PENDING reservation survives a "
          "reconnect / disconnect): INV-Cv2 [clean/restart] and INV-Ciii2 fail",
          rc == 1 and _has_fail(fails_s, "INV-Cv2 [clean]") and _has_fail(fails_s, "INV-Cv2 [restart]")
          and _has_fail(fails_s, "INV-Ciii2 "))
    # the same script must also run a single group on demand (selectors used by run_game_tests.py)
    rc, out = _run_against_model("test_inventory_correctness.py", 47855, fast, script_args=sa + ["--only", "a,b"])
    check("S8 test_inventory_correctness.py --only a,b runs just those groups", rc == 0 and "group a" in out
          and "group b" in out and "group civ" not in out and "INV-D" not in out)


def main():
    t0 = time.monotonic()
    for fn in (u_codec, u_confirm_codec, u_log_evidence, u_serial, u_receiver, u_pretend_missing, u_sender, u_inbox, u_request_matching,
               u_worldview, u_addressing, u_lossy_link):
        fn()
    e_tests()
    e_game_tests()
    if "--skip-scripts" not in sys.argv:
        s_tests()
        s_inventory_tests()
    print(f"(elapsed {time.monotonic() - t0:.1f}s)")
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
