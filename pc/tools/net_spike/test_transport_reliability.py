#!/usr/bin/env python3
"""test_transport_reliability.py - Foundation-phase transport contract test against a REAL,
currently-running host (`AnimalCrossing.exe --host <port> --verbose`), driven entirely from the
fake-client side with net_spike_lib's deterministic fault injection -- no C-side seam needed.

Game-level observable used throughout: a PICKUP_REQUEST for OUT_OF_RANGE_TILE (255,255). The host
always answers it with a rejected PICKUP_RESULT echoing the request_id and never mutates the field,
so the number and ORDER of PICKUP_RESULTs tells us exactly how many times, and in which order, the
host's game layer received each request. (The host's own per-peer dedup cache replays a cached
answer for a repeated request_id at the GAME level -- T4's control case proves that observable is
live, so "exactly one result" under a transport-level duplicate is a real proof of transport dedup.)

Checks (contract section 1):
  T1  HELLO nonce echoed; first reliable seq each direction is 0; IDENTITY/IDENTITY_ACK over RDATA
  T2  host never sends legacy DATA kind=RELIABLE (checked across every client at the end)
  T3  cumulative ACK covers a burst
  T4  same seq sent twice -> delivered once (control: same request_id, new seq -> replayed twice)
  T5  seqs transmitted out of order -> delivered in order
  T6  gap -> nothing past it delivered; host ACK = (gap, SACK bits); release -> in-order delivery
  T7  ACK withheld for one host message -> host retransmits it; test sees it once
  T8  first copy of a host message lost -> host retransmit recovers it; SACK'd successor delivered after it
  T9  duplicate ACKs and a bogus ACK (acks never-sent seqs) are tolerated
  T10 legacy DATA kind=1 IDENTITY is dropped (no IDENTITY_ACK); a proper RDATA IDENTITY works
  T11 same-address HELLO with a NEW nonce resets the peer (fresh seq 0 both ways)
  T12 duplicate HELLO with the SAME nonce does not reset an established peer
  T13 clean DISCONNECT frees the slot immediately (next client reuses it without the 5 s timeout)
  T14 going silent -> host frees the peer after PCNET_TIMEOUT (5 s)
  T15 ACKs withheld entirely -> host retransmits, then disconnects after its bounded budget
  T16 every host->client reliable stream was delivered contiguously from seq 0

Run it ALONE against the host (other peers connecting concurrently would make T13's slot-reuse
check ambiguous). Takes ~25 s (T14 and T15 wait out real timers).

Since Stage 1A the host admits a client only as a DISTINCT non-host resident of its save (bin_fixture4: 3 non-host
residents) => at most THREE clients may be connected at once. Clients are therefore retired as soon as their
checks are done (graceful DISCONNECT + a short settle so the host tears the peer down): T1-a leaves after T9, T10-b after
T10, T11-c after T12, so T14-d and T15-e run with at most one other client. The T14/T15 timer waits are unchanged.
Every client object is still included in the final T16/T2 contiguity / legacy-DATA checks (their receive state is kept).

Usage: python test_transport_reliability.py <host_ip> <port>
"""
import sys
import time

import net_spike_lib as L

fresh_rid = L.make_request_id_counter(7000)
OOR = L.OUT_OF_RANGE_TILE
ALL_CLIENTS = []


def client(label):
    c = L.FakeClient(label, HOST, PORT)
    ALL_CLIENTS.append(c)
    return c


def retire(c, settle=0.4):
    """Graceful departure (DISCONNECT) + settle so the host frees the peer and its resident before the next client
    connects (at most 3 simultaneous clients with the 4-resident fixture)."""
    c.disconnect()
    L.pump_sleep(settle)


def oor_request(rid):
    return L.build_pickup_request(OOR[0], OOR[1], rid)


def is_result_payload(rid):
    def pred(payload):
        g = L.decode_game(payload)
        return g is not None and g.msg_type == L.PC_NETGAME_MSG_PICKUP_RESULT and g.request_id == rid
    return pred


def result_order(c, rids):
    """request_ids of PICKUP_RESULTs (for rids in `rids`) in inbox arrival order."""
    return [m.game.request_id for m in c.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_PICKUP_RESULT))
            if m.game is not None and m.game.request_id in rids]


def main():
    global HOST, PORT
    hp = L.parse_host_port(sys.argv, "usage: test_transport_reliability.py <host_ip> <port>")
    if hp is None:
        return 1
    HOST, PORT = hp
    r = []

    # --- T13 first, while this script's clients are the only peers ---------------------------------
    x = client("T13-x")
    x.connect_and_ready()
    slot = x.assigned_peer_id
    t0 = time.monotonic()
    x.disconnect()
    L.pump_sleep(0.05)  # one host frame to process the goodbye; far below the 5 s timeout
    y = client("T13-y")  # fresh socket = different source port = a genuinely new peer address
    y.connect_and_ready()
    L.check(f"T13 clean DISCONNECT frees slot {slot} immediately (new client got slot {y.assigned_peer_id} "
            f"after {time.monotonic() - t0:.2f}s)", y.assigned_peer_id == slot, r)
    y.disconnect()
    L.pump_sleep(0.05)

    # --- T17: nonce-less (size-0, legacy) HELLO is refused: DISCONNECT, no HELLO_ACK, no slot --------
    # (Workstream A legacy-peer fix.) Verified via slot allocation: a proper client connecting right
    # after must still get the lowest free slot the legacy HELLO would otherwise have taken.
    x = client("T17-x")
    x.connect_and_ready(quiet=True)
    slot = x.assigned_peer_id
    x.disconnect()
    L.pump_sleep(0.05)
    leg = L.TransportClient("T17-legacy", HOST, PORT)
    leg.state, leg.nonce = leg.STATE_PENDING, None
    leg.send_raw(L.encode_hello(None), force=True)
    got_dis = L.DEFAULT_HUB.wait_until(lambda: leg.stats["rx_disconnect"] > 0 or leg.stats["rx_hello_ack"] > 0, 1.5)
    L.pump_sleep(0.2)
    y = client("T17-y")
    y.connect_and_ready(quiet=True)
    L.check(f"T17 nonce-less HELLO -> DISCONNECT and no HELLO_ACK (got disconnect={leg.stats['rx_disconnect']}, "
            f"hello_ack={leg.stats['rx_hello_ack']})", got_dis and leg.stats["rx_disconnect"] > 0
            and leg.stats["rx_hello_ack"] == 0, r)
    L.check(f"T17 ...and no slot: the next proper client got slot {slot} (got {y.assigned_peer_id})",
            y.assigned_peer_id == slot, r)
    y.disconnect()
    leg.close()
    L.pump_sleep(0.05)

    # --- T1 --------------------------------------------------------------------------------------
    a = client("T1-a")
    a.connect()
    L.check("T1 HELLO_ACK echoes our nonce", a.hello_ack_nonce == a.nonce, r)
    id_seq = a.send_identity()
    reply = a.wait_handshake_reply(3.0)
    L.check("T1 IDENTITY sent as RDATA seq 0 and answered with IDENTITY_ACK",
            id_seq == 0 and reply is not None and reply.msg_type == L.PC_NETGAME_MSG_IDENTITY_ACK, r)
    L.check("T1 first host reliable message on the connection has seq 0", reply is not None and reply.seq == 0, r)
    L.check("T1 host ACKed our seq 0", a.wait_acked(id_seq, 1.0), r)

    # --- T3 --------------------------------------------------------------------------------------
    rids = [fresh_rid() for _ in range(5)]
    last = None
    for rid in rids:
        last = a.send_reliable(oor_request(rid))
    ok = L.DEFAULT_HUB.wait_until(lambda: all(a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, q) for q in rids), 2.0)
    L.check("T3 burst of 5 answered, in order", ok and result_order(a, set(rids)) == rids, r)
    L.check("T3 host's cumulative ACK covers the whole burst",
            a.wait_acked(last, 1.0) and a.latest_ack() is not None and a.latest_ack()[1] == a.sender.next_seq, r)

    # --- T4 --------------------------------------------------------------------------------------
    rid = fresh_rid()
    a.send_reliable_duplicate(oor_request(rid), copies=2)
    L.pump_sleep(0.8)
    L.check("T4 same transport seq sent twice -> host game layer saw it once (1 result)",
            a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, rid) == 1, r)
    a.send_reliable(oor_request(rid))  # control: same request_id, NEW seq -> game-level duplicate
    L.pump_sleep(0.8)
    L.check("T4 control: same request_id on a new seq IS a second delivery (cached answer replayed)",
            a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, rid) == 2, r)

    # --- T5 --------------------------------------------------------------------------------------
    r1, r2, r3 = fresh_rid(), fresh_rid(), fresh_rid()
    a.send_reliable_out_of_order([oor_request(r1), oor_request(r2), oor_request(r3)])  # sent r3, r2, r1
    L.DEFAULT_HUB.wait_until(lambda: all(a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, q) for q in (r1, r2, r3)), 2.0)
    L.check("T5 seqs transmitted in reverse -> host delivered them in seq order",
            result_order(a, {r1, r2, r3}) == [r1, r2, r3], r)

    # --- T6 --------------------------------------------------------------------------------------
    g1, g2, g3 = fresh_rid(), fresh_rid(), fresh_rid()
    gap = a.send_reliable_withheld(oor_request(g1))
    a.send_reliable(oor_request(g2))
    a.send_reliable(oor_request(g3))
    L.pump_sleep(0.6)
    L.check("T6 nothing past the gap is delivered while it is open",
            a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, g2) == 0 and a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, g3) == 0, r)
    ack = a.latest_ack()
    L.check(f"T6 host ACK reports (next_expected={gap}, sack=0b11) (got {ack[1:] if ack else None})",
            ack is not None and ack[1:] == (gap, 0b11), r)
    a.release_withheld()
    L.DEFAULT_HUB.wait_until(lambda: all(a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, q) for q in (g1, g2, g3)), 2.0)
    L.check("T6 gap filled -> all three delivered, in order", result_order(a, {g1, g2, g3}) == [g1, g2, g3], r)

    # --- T7 --------------------------------------------------------------------------------------
    rid = fresh_rid()
    a.skip_ack_next(is_result_payload(rid))
    a.send_reliable(oor_request(rid))
    L.DEFAULT_HUB.wait_until(lambda: (a.seq_of(is_result_payload(rid)) is not None
                                      and a.host_retransmits_of(a.seq_of(is_result_payload(rid))) >= 1), 3.0)
    seq = a.seq_of(is_result_payload(rid))
    L.check("T7 ACK withheld once -> host retransmitted the result",
            seq is not None and a.host_retransmits_of(seq) >= 1, r)
    L.check("T7 ...and the test saw it exactly once (client-side dedup)",
            a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, rid) == 1, r)

    # --- T8 --------------------------------------------------------------------------------------
    d1, d2 = fresh_rid(), fresh_rid()
    a.drop_inbound_next(is_result_payload(d1))
    a.send_reliable(oor_request(d1))
    a.send_reliable(oor_request(d2))
    ok = L.DEFAULT_HUB.wait_until(lambda: a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, d1) == 1
                                  and a.count_results(L.PC_NETGAME_MSG_PICKUP_RESULT, d2) == 1, 3.0)
    L.check("T8 lost first copy recovered by host retransmit; delivered d1 then d2",
            ok and len(a.dropped_inbound) == 1 and result_order(a, {d1, d2}) == [d1, d2], r)
    s2 = a.seq_of(is_result_payload(d2))
    if s2 is not None:
        L.info(f"T8 host retransmitted the already-SACKed successor {a.host_retransmits_of(s2)} time(s) "
               "(0 = host honours SACK)")

    # --- T9 --------------------------------------------------------------------------------------
    a.send_ack_now(repeat=10)
    a.send_raw_ack(a.receiver.next_expected + 100000, 0xFFFFFFFF)
    rid = fresh_rid()
    a.send_reliable(oor_request(rid))
    L.check("T9 duplicate + bogus ACKs tolerated (link still works)",
            a.wait_result(L.PC_NETGAME_MSG_PICKUP_RESULT, rid, 1.5, consume=False) is not None and a.is_connected(), r)

    retire(a)  # T1-T9 done: free A's resident (peak concurrent clients <= 3 from here on)

    # --- T10 -------------------------------------------------------------------------------------
    b = client("T10-b")
    b.connect()
    b.send_legacy_reliable_data(L.build_identity(town=b.claimed_town()))  # a VALID identity, legacy framing
    L.check("T10 legacy DATA kind=RELIABLE IDENTITY is dropped (no IDENTITY_ACK)",
            b.wait_handshake_reply(1.0) is None, r)
    b.send_identity()
    L.check("T10 the same IDENTITY over RDATA seq 0 is answered", b.wait_handshake_reply(2.0) is not None, r)

    retire(b)  # T10 done

    # --- T11 -------------------------------------------------------------------------------------
    c = client("T11-c")
    c.connect_and_ready()
    for _ in range(3):
        c.send_reliable(oor_request(fresh_rid()))
    L.pump_sleep(0.3)
    old_nonce = c.nonce
    c.restart_same_address()
    id_seq = c.send_identity()
    reply = c.wait_handshake_reply(3.0)
    L.check("T11 new nonce from the same address: host accepted our fresh seq-0 IDENTITY",
            c.nonce != old_nonce and id_seq == 0 and reply is not None
            and reply.msg_type == L.PC_NETGAME_MSG_IDENTITY_ACK, r)
    L.check("T11 host->client stream restarted at seq 0", reply is not None and reply.seq == 0, r)

    # --- T12 -------------------------------------------------------------------------------------
    c.send_raw(L.encode_hello(c.nonce))  # same nonce again
    L.pump_sleep(0.2)
    rid = fresh_rid()
    s = c.send_reliable(oor_request(rid))
    L.check(f"T12 duplicate same-nonce HELLO did not reset the peer (seq {s} still answered)",
            s > 0 and c.wait_result(L.PC_NETGAME_MSG_PICKUP_RESULT, rid, 1.5, consume=False) is not None, r)

    retire(c)  # T11/T12 done: no other client is connected during T14/T15

    # --- T14 -------------------------------------------------------------------------------------
    d = client("T14-d")
    d.connect_and_ready()
    d.go_silent()
    rx0 = d.stats["rx_total"]
    L.pump_sleep(3.0)
    rx_early = d.stats["rx_total"]
    L.pump_sleep(3.0)  # now > 5 s since our last datagram
    rx_mid = d.stats["rx_total"]
    L.pump_sleep(1.5)
    rx_late = d.stats["rx_total"]
    L.check("T14 host kept sending to us during the first seconds of silence", rx_early > rx0, r)
    L.check(f"T14 host stopped all traffic to us after its timeout (no datagrams in the last 1.5 s; "
            f"{rx_late - rx_mid} seen)", rx_late == rx_mid, r)
    d.resume()
    rid = fresh_rid()
    d.send_reliable(oor_request(rid))
    L.check("T14 host no longer answers the timed-out peer",
            d.wait_result(L.PC_NETGAME_MSG_PICKUP_RESULT, rid, 1.0, consume=False) is None, r)
    d.state = d.STATE_CLOSED

    # --- T15 -------------------------------------------------------------------------------------
    e = client("T15-e")
    e.connect_and_ready()
    rid = fresh_rid()
    e.withhold_acks(True)
    t0 = time.monotonic()
    e.send_reliable(oor_request(rid))
    dis = e.wait_disconnected(L.HOST_RETRY_BUDGET_WAIT_S)
    seq = e.seq_of(is_result_payload(rid))
    L.check(f"T15 ACKs withheld: host retransmitted then disconnected us "
            f"(after {time.monotonic() - t0:.1f}s, {e.host_retransmits_of(seq) if seq is not None else '?'} retransmits)",
            dis and e.disconnected_by_host and seq is not None and e.host_retransmits_of(seq) >= 3, r)
    L.info(f"T15 expected {L.HOST_MAX_RETRANSMITS} retransmits per A's draft")

    # --- T16 / T2 ----------------------------------------------------------------------------------
    L.check("T16 every host->client reliable stream delivered contiguously from seq 0",
            all(cl.delivered_in_order() for cl in ALL_CLIENTS), r)
    L.check("T2 host never sent legacy DATA kind=RELIABLE",
            sum(cl.stats["legacy_reliable_rx"] for cl in ALL_CLIENTS) == 0, r)

    for cl in ALL_CLIENTS:
        cl.close()
    return L.summary_and_exit_code(r)


if __name__ == "__main__":
    sys.exit(main())
