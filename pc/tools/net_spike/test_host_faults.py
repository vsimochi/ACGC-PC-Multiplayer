#!/usr/bin/env python3
"""test_host_faults.py - world snapshot + field deltas delivered intact while the HOST's transport
injects faults through Workstream A's deterministic env seam (read at pc_net_init()):
  PC_NET_FAULT_DROP_RDATA_EVERY=N     every Nth outgoing RDATA transmission not sent
  PC_NET_FAULT_DUP_RDATA_EVERY=N      every Nth outgoing RDATA transmission sent twice
  PC_NET_FAULT_REORDER_RDATA_EVERY=N  every Nth outgoing RDATA transmission held back one slot
  PC_NET_FAULT_DROP_ACK_EVERY=N       every Nth outgoing ACK not sent
The host must be launched with ONE of them set (run_game_tests.py does this) and --pickup-test-seed.

Checks (all faults):
  H1 A and B each complete a well-formed 30-acre snapshot; their views are identical, no violations
  H2 4 pipelined pickups by A: every PICKUP_RESULT arrives exactly once
  H3 B receives each resulting FIELD_UPDATE exactly once, in request order, world_seq increasing,
     and its view shows every tile EMPTY
  H4 late joiner C's snapshot equals B's view (diff empty)
  H5 every client's reliable stream contiguous from seq 0 (no gap/duplicate reached the game layer)
  H6 fault-specific evidence that the fault actually fired and was repaired:
       drop_rdata -> gaps: later seqs arrived first and were buffered until the host's retransmit filled them
       dup_rdata  -> duplicate RDATA arrivals were suppressed
       reorder_rdata -> out-of-order RDATA was buffered and delivered in order
       drop_ack   -> the fake clients had to retransmit (host ACKs lost), yet each request was
                     processed exactly once (H2)

Usage: python test_host_faults.py <host_ip> <port> [drop_rdata|dup_rdata|reorder_rdata|drop_ack]
"""
import sys

import net_spike_lib as L

ITM_FOOD_APPLE = 0x2800
PICKUP = L.PC_NETGAME_MSG_PICKUP_RESULT
fresh_rid = L.make_request_id_counter(9800)


def snap_ok(c, r, name):
    s = c.completed_snapshots()[-1] if c.completed_snapshots() else None
    L.check(f"{name} {c.label}: snapshot complete with acres 0..29 in order, END acre_count 30, no violations",
            s is not None and s["blocks"] == list(range(L.ACRE_NUM)) and s["end"]["acre_count"] == L.ACRE_NUM
            and not c.world.violations, r)


def main():
    if len(sys.argv) not in (3, 4):
        print("usage: test_host_faults.py <host_ip> <port> [drop_rdata|dup_rdata|reorder_rdata|drop_ack]")
        return 1
    host, port = sys.argv[1], int(sys.argv[2])
    fault = sys.argv[3] if len(sys.argv) == 4 else None
    r = []
    print(f"fault under test: {fault or 'none'}")

    a = L.FakeClient("A", host, port)
    a.connect_and_ready()
    b = L.FakeClient("B", host, port)
    b.connect_and_ready()
    snap_ok(a, r, "H1")
    snap_ok(b, r, "H1")
    L.check(f"H1 A and B hold identical world views (diff {a.world.diff(b.world)[:3]})", a.world.diff(b.world) == [], r)

    clients = [a, b]
    live = [t for t in L.TOWN_FIXTURE_TILES if a.world.tile_ut(*t) == ITM_FOOD_APPLE][:4]
    if len(live) < 2:
        print("SKIP - H2-H4: fewer than 2 live fixture tiles (launch host with --pickup-test-seed)")
    else:
        bmark = b.inbox.mark()
        rids = []
        res = []
        # Two-phase protocol adaptation: a new request aborts the peer's pending one, so the requests can no longer
        # be pipelined back-to-back; each waits for its provisional RESULT (auto-CONFIRMed) before the next.
        for t in live:
            a.claim_position(*L.tile_center(*t))
            q = fresh_rid()
            rids.append(q)
            a.send_pickup_request(t[0], t[1], q)
            res.append(a.wait_result(PICKUP, q, 3.0, consume=False))
        L.pump_sleep(1.5)
        L.check(f"H2 every PICKUP_RESULT arrived exactly once (counts {[a.count_results(PICKUP, q) for q in rids]})",
                all(x is not None for x in res) and all(a.count_results(PICKUP, q) == 1 for q in rids), r)
        accepted = [t for t, x in zip(live, res) if x is not None and x.accepted]
        gs = [g for g in b.field_updates_since(bmark) if L.field_update_tuple(g)[:2] in accepted]
        order = [L.field_update_tuple(g)[:2] for g in gs]
        seqs = [g.world_seq for g in gs]
        L.check(f"H3 B got each FIELD_UPDATE exactly once, in request order, world_seq increasing ({order}, {seqs})",
                len(accepted) == len(live) and order == accepted
                and all(L.seq_diff(y, x) > 0 for x, y in zip(seqs, seqs[1:])), r)
        L.check("H3 B's view shows every mutated tile EMPTY", all(b.world.tile_ut(*t) == L.EMPTY_NO for t in accepted), r)
        c = L.FakeClient("C", host, port)
        c.connect_and_ready()
        snap_ok(c, r, "H4")
        L.check(f"H4 late joiner C's snapshot equals B's view (diff {c.world.diff(b.world)[:3]})",
                c.world.diff(b.world) == [], r)
        clients.append(c)

    # H7 (two-phase protocol): a pickup whose provisional RESULT is delayed (first delivery of the RESULT lost on our
    # side, so it only arrives with the host's retransmit >= ~150 ms later) must NOT mutate the world while the RESULT
    # -- and therefore the requester's CONFIRM -- is still in flight (the old single-phase host mutated at REQUEST
    # time, which is exactly the bug); once the retransmitted RESULT arrives exactly once and is confirmed, the
    # host commits and B sees the change. (dup_rdata: the host's duplicate copy is delivered at once, so 'still in
    # flight' cannot be required there.)
    live2 = [t for t in L.TOWN_FIXTURE_TILES if a.world.tile_ut(*t) == ITM_FOOD_APPLE and b.world.tile_ut(*t) == ITM_FOOD_APPLE]
    if live2:
        t = live2[0]
        q = fresh_rid()
        bmark = b.inbox.mark()
        a.drop_inbound_next(lambda p: p[:1] == bytes([PICKUP]) and len(p) == 12 and int.from_bytes(p[4:8], "little") == q)
        a.claim_position(*L.tile_center(*t))
        a.send_pickup_request(t[0], t[1], q)
        t_sent = a.last_send
        saw_t = lambda: any(L.field_update_tuple(g)[:2] == t for g in b.field_updates_since(bmark))
        b_saw_early = L.DEFAULT_HUB.wait_until(saw_t, 0.1)  # < the host's 150 ms RTO: the RESULT is still in flight
        res_early = a.count_results(PICKUP, q)
        res = a.wait_result(PICKUP, q, 5.0, consume=False)
        b_saw = L.DEFAULT_HUB.wait_until(saw_t, 2.0)
        L.check(f"H7 delayed-RESULT pickup: the host did NOT mutate while A's provisional result was still in flight "
                f"(B saw it early={b_saw_early}, results delivered to A at that moment={res_early}); the result then "
                f"arrived once via retransmit, accepted={res.accepted if res else None}, and after A's CONFIRM B saw "
                f"the change (B saw it={b_saw})",
                (not b_saw_early or fault == "dup_rdata") and (res_early == 0 or fault == "dup_rdata") and b_saw
                and res is not None and res.accepted and a.count_results(PICKUP, q) == 1
                and len(a.dropped_inbound) >= 1, r)
    else:
        L.info("H7 skipped: no live tile left")

    L.check("H5 every client's reliable stream contiguous from seq 0",
            all(x.delivered_in_order() for x in clients), r)

    retx = sum(sum(max(0, n - 1) for n in x.raw_arrivals.values()) for x in clients)
    dups = sum(x.stats["rdata_dup"] for x in clients)
    buffered = sum(x.stats["rdata_buffered"] for x in clients)
    our_retx = sum(x.stats["rdata_retransmit_tx"] for x in clients)
    L.info(f"transport evidence: extra copies received={retx} dup-suppressed={dups} buffered-out-of-order={buffered} "
           f"our retransmits={our_retx}")
    if fault == "drop_rdata":
        # A host-dropped first transmission arrives only once (as the retransmit), so "extra copies" cannot
        # show it. The observable is the GAP: later seqs arrive first and are buffered until the host's
        # retransmit fills the hole (H5 then proves in-order, exactly-once delivery).
        L.check(f"H6 drop_rdata: gaps opened by dropped host transmissions were observed ({buffered} "
                "out-of-order arrivals buffered) and every one repaired by retransmit", buffered > 0, r)
    elif fault == "dup_rdata":
        L.check("H6 dup_rdata: duplicate RDATA arrivals observed and suppressed", dups > 0, r)
    elif fault == "reorder_rdata":
        L.check("H6 reorder_rdata: out-of-order RDATA buffered and delivered in order", buffered > 0, r)
    elif fault == "drop_ack":
        L.check("H6 drop_ack: lost host ACKs forced client retransmits; each request still processed once",
                our_retx > 0, r)

    for x in clients:
        x.close()
    return L.summary_and_exit_code(r)


if __name__ == "__main__":
    sys.exit(main())
