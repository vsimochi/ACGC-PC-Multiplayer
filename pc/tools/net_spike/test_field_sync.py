#!/usr/bin/env python3
"""test_field_sync.py - host-authoritative field MUTATION propagation (foundation contract section 4,
protocol v2) against a REAL host in gameplay, launched with --pickup-test-seed.

Mutations are driven by fake client A through the pickup/drop protocol (the host performs, commits
and broadcasts them -- host-originated changes); B and C observe. FIELD_UPDATE v2 is
{grid, acre, tile, flags(DEPOSIT_VALID|DEPOSIT_ON), value, world_seq}; net_spike_lib maps
(acre, tile) back to town ut coords so content assertions compare the tiles the requests named.

  F1  one pickup -> B and C each get exactly ONE FIELD_UPDATE (tile, EMPTY)
  F2  3 pipelined pickups -> observers get all 3, once each, in request order; each update is
      grid 0, DEPOSIT_VALID set, (acre, tile) == the requested tile, world_seq strictly increasing
  F3  same tile repeatedly (drop cherry, pick up, drop apple) -> observers see (cherry, EMPTY, apple)
      in order with increasing world_seq; B's WorldView ends at apple
  F4  requester ordering: A's own PICKUP_RESULT precedes the FIELD_UPDATE it causes
  F5  reconnect: B leaves, A mutates, B rejoins -> B's fresh snapshot contains the change
  F6  no stale peer state: after rejoin (same slot), the next delta B receives has world_seq newer
      than B's snapshot block for that acre and is applied; B has no world violations
  F7  no TRANSIENT value (pcfa_transient_kind) in any FIELD_UPDATE seen by any observer

Usage: python test_field_sync.py <host_ip> <port>    (host must be launched with --pickup-test-seed)
"""
import sys

import net_spike_lib as L

PICKUP = L.PC_NETGAME_MSG_PICKUP_RESULT
FU = L.PC_NETGAME_MSG_FIELD_UPDATE
ITM_FOOD_APPLE = 0x2800
ITM_FOOD_CHERRY = 0x2801
fresh_rid = L.make_request_id_counter(9000)


def next_live(queue, view):
    """Next queued in-town fixture tile the (snapshot-derived) view shows holding an apple."""
    while True:
        t = queue.next_tile()
        if t is None or view.tile_ut(*t) == ITM_FOOD_APPLE:
            return t


def ut_of(g):
    return L.field_update_tuple(g)[:2]


def main():
    hp = L.parse_host_port(sys.argv, "usage: test_field_sync.py <host_ip> <port>")
    if hp is None:
        return 1
    host_ip, port = hp
    r = []
    a, b, c = L.connect_ready_clients(host_ip, port, ["A", "B", "C"])
    for x in (a, b, c):
        x.drain_field_updates(0.3)
    queue = L.CandidateQueue(fallback=False)
    all_seen = []

    # --- F1 + F4 ----------------------------------------------------------------------------------------
    tile = next_live(queue, a.world)
    if tile is None:
        print("SKIP - all F checks: no live fixture item (launch the host with --pickup-test-seed)")
        for x in (a, b, c):
            x.close()
        return L.summary_and_exit_code(r, ["host not seeded"])
    rid = fresh_rid()
    a.claim_position(*L.tile_center(*tile))
    a.send_pickup_request(tile[0], tile[1], rid)
    m = a.inbox.wait_for(L.p_game(PICKUP, (L.CH_RELIABLE,), request_id=rid), 0.6)
    L.check(f"F1 A's pickup of live tile {tile} accepted", m is not None and m.game.accepted, r)
    result_index = m.index if m is not None else None
    fb = b.drain_field_updates(1.0)
    fc = c.drain_field_updates(0.3)
    all_seen += fb + fc
    L.check(f"F1 B got exactly one FIELD_UPDATE {tile}->EMPTY (got {fb})", fb.count((tile[0], tile[1], L.EMPTY_NO)) == 1, r)
    L.check("F1 C got exactly one FIELD_UPDATE for the same change", fc.count((tile[0], tile[1], L.EMPTY_NO)) == 1, r)
    fu_msgs = [x for x in a.inbox.peek_all(L.p_msg_type(FU, (L.CH_RELIABLE,)))
               if x.game is not None and ut_of(x.game) == tile]
    L.check("F4 requester got exactly one FIELD_UPDATE for its own change, delivered after its PICKUP_RESULT",
            result_index is not None and len(fu_msgs) == 1 and fu_msgs[0].index > result_index, r)
    a.drain_field_updates(0.0)

    # --- F2 ------------------------------------------------------------------------------------------------
    tiles = []
    while len(tiles) < 3:
        t = next_live(queue, a.world)
        if t is None:
            break
        tiles.append(t)
    rids = []
    bmark = b.inbox.mark()
    results = []
    # Two-phase protocol adaptation: a NEW request from a peer implicitly ABORTS that peer's pending one (a real
    # client only ever has one in flight), so back-to-back "pipelined" requests would abort each other before the
    # first CONFIRM could be sent. Each request therefore waits for its provisional RESULT (auto-CONFIRMed by the
    # library) before the next is sent; every assertion below is unchanged. (INV-C(vi) tests the replacement rule.)
    for t in tiles:
        a.claim_position(*L.tile_center(*t))
        q = fresh_rid()
        rids.append(q)
        a.send_pickup_request(t[0], t[1], q)
        results.append(a.wait_result(PICKUP, q, 1.5))
    accepted = [t for t, res in zip(tiles, results) if res is not None and res.accepted]
    L.pump_sleep(1.0)
    gs = [g for g in b.field_updates_since(bmark) if ut_of(g) in accepted]
    fb = b.drain_field_updates(0.0)
    all_seen += fb + c.drain_field_updates(0.2)
    expected = [(t[0], t[1], L.EMPTY_NO) for t in accepted]
    L.check(f"F2 {len(accepted)} pipelined mutations observed once each, in request order (got {fb})",
            len(accepted) >= 2 and [u for u in fb if u in expected] == expected, r)
    seqs = [g.world_seq for g in gs]
    L.check(f"F2 each update is grid 0 with DEPOSIT_VALID, addresses the requested tile, and world_seq strictly "
            f"increases (seqs {seqs})",
            len(gs) == len(accepted) and all(g.grid == 0 and g.flags & L.PC_NETGAME_FU_FLAG_DEPOSIT_VALID for g in gs)
            and [ut_of(g) for g in gs] == accepted and all(L.seq_diff(y, x) > 0 for x, y in zip(seqs, seqs[1:])), r)
    L.check("F2 B's world view shows every mutated tile EMPTY", all(b.world.tile_ut(*t) == L.EMPTY_NO for t in accepted), r)

    # --- F3 ------------------------------------------------------------------------------------------------
    same = accepted[0] if accepted else tile  # a tile this script emptied itself
    bmark = b.inbox.mark()
    steps = []
    res1 = a.drop(0, ITM_FOOD_CHERRY, same[0], same[1], fresh_rid(), claim_at=same)
    steps.append(res1 is not None and res1.accepted)
    res2 = a.pickup(same[0], same[1], fresh_rid())
    steps.append(res2 is not None and res2.accepted and res2.granted_item == ITM_FOOD_CHERRY)
    res3 = a.drop(0, ITM_FOOD_APPLE, same[0], same[1], fresh_rid(), claim_at=same)
    steps.append(res3 is not None and res3.accepted)
    L.pump_sleep(1.0)
    gs = [g for g in b.field_updates_since(bmark) if ut_of(g) == same]
    fb = b.drain_field_updates(0.0)
    all_seen += fb + c.drain_field_updates(0.2)
    on_tile = [u[2] for u in fb if (u[0], u[1]) == same]
    L.check(f"F3 drop/pickup/drop on {same} all accepted by the host", all(steps), r)
    L.check(f"F3 observer saw (cherry, EMPTY, apple) in order on the same tile (got {[hex(v) for v in on_tile]})",
            on_tile == [ITM_FOOD_CHERRY, L.EMPTY_NO, ITM_FOOD_APPLE], r)
    seqs = [g.world_seq for g in gs]
    L.check(f"F3 world_seq increases on every change to the same tile ({seqs}); B's view ends at apple",
            len(seqs) == 3 and all(L.seq_diff(y, x) > 0 for x, y in zip(seqs, seqs[1:]))
            and b.world.tile_ut(*same) == ITM_FOOD_APPLE, r)

    # --- F5 / F6 -------------------------------------------------------------------------------------------
    slot_b = b.assigned_peer_id
    b.disconnect()
    L.pump_sleep(0.1)
    missed = next_live(queue, a.world)
    res = a.pickup(missed[0], missed[1], fresh_rid()) if missed else None
    L.check(f"F5 A mutated {missed} while B was away", res is not None and res.accepted, r)
    b.connect_and_ready()  # waits for the fresh snapshot
    L.check(f"F5 B rejoined the same slot {slot_b} (got {b.assigned_peer_id})", b.assigned_peer_id == slot_b, r)
    L.check("F5 B's fresh snapshot shows the change made while it was away",
            missed is not None and b.world.tile_ut(*missed) == L.EMPTY_NO, r)
    acre, t6 = L.town_ut_to_acre_tile(*same)
    snap_seq = b.snapshots[-1]["block_seqs"].get(acre)
    bmark = b.inbox.mark()
    res = a.pickup(same[0], same[1], fresh_rid())  # the apple F3 left there
    L.pump_sleep(0.6)
    gs = [g for g in b.field_updates_since(bmark) if g.acre == acre and g.tile == t6]
    all_seen += [L.field_update_tuple(g) for g in gs]
    L.check(f"F6 after rejoin, the next delta for {same} is newer than B's snapshot block (snap seq {snap_seq}, "
            f"deltas {[g.world_seq for g in gs]}) and is applied",
            res is not None and res.accepted and len(gs) == 1 and snap_seq is not None
            and L.seq_diff(gs[0].world_seq, snap_seq) > 0 and b.world.tile_ut(*same) == L.EMPTY_NO, r)
    L.check(f"F6 rejoined B holds no world violations {b.world.violations[:3]}", not b.world.violations, r)

    # --- F7 ------------------------------------------------------------------------------------------------
    L.check(f"F7 no transient value in any of the {len(all_seen)} observed FIELD_UPDATEs",
            all(u[2] not in L.TRANSIENT_VALUES for u in all_seen), r)
    L.check("F7 no AMBIGUOUS value (RSV_NO/RSV_SIGNBOARD) in any snapshot/delta a client applied (host in town)",
            all(not x.world.ambiguous_seen for x in (a, b, c)), r)
    L.check("stream integrity: every client's reliable stream contiguous from seq 0; no violations anywhere",
            all(x.delivered_in_order() and not x.world.violations for x in (a, b, c)), r)

    for x in (a, b, c):
        x.close()
    return L.summary_and_exit_code(r)


if __name__ == "__main__":
    sys.exit(main())
