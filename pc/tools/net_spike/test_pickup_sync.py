#!/usr/bin/env python3
"""test_pickup_sync.py - Stage 5A (authoritative field-item pickup) synthetic network test.

Hand-crafts wire bytes (like test_appearance_sync.py/test_move_sync.py) to drive a REAL,
currently-running `AnimalCrossing.exe --host <port>` process through the new PICKUP_REQUEST /
PICKUP_RESULT / FIELD_UPDATE protocol (see pc/src/pc_net_game.c).

Wire-format/handshake/fixture-tile plumbing shared with test_drop_sync.py (which speaks the same
PICKUP_REQUEST/PICKUP_RESULT protocol to prepare its own fixtures) now lives in net_spike_lib.py --
see that module's own doc comment. Everything below is pickup-specific: probing/discovery of live
field items, and this script's own test sequence.

Fixture note (see the task's own instructions on this): this script does NOT hardcode a specific
world coordinate or assume a specific item exists anywhere. It cannot -- the host boots from a
real GCI save (save/card_a/...) whose field contents this script has no way to inspect directly,
and Stage 5A's own protocol deliberately gives a client no "peek at the raw tile" message (see
pc_net_game.c's own doc on never trusting a client's claimed item id -- the reverse holds too: a
client is never handed raw field truth to read, only PICKUP_RESULT/FIELD_UPDATE facts about
requests it made). Instead, this script establishes its own fixture LIVE, at run time, by scanning
field tiles with real PICKUP_REQUESTs and using whichever ones the host actually reports as
occupied. This works because Stage 3's movement sync performs no anti-cheat/physics validation
(see pc_net_game.c's own doc on pcnetgame_handle_host_move()) -- a fake client may freely claim to
be standing at any position, including exactly at the center of whichever tile it is about to
probe, so the discovery scan never needs a real character near a real item.

Two discovery tiers, tried in order, neither one an arbitrary hardcoded coordinate:
  1. FIXTURE_CANDIDATE_TILES (net_spike_lib.py) -- the exact same 30 tiles (one per acre)
     pc_net_game.c's pcnetgame_run_pickup_test_seed() seeds when the host is launched with
     --pickup-test-seed (Stage 5A.1). This is the deterministic path: run the host with that flag
     and this script reliably finds a live item within the first ~30 probes instead of scanning
     hundreds of empty tiles. The set is documented and shared between production and test code,
     not invented here -- see pc_net_game.c for the authoritative copy.
  2. A broader live scan (unchanged from the original Stage 5A pass) -- the fallback for a host
     run WITHOUT --pickup-test-seed, e.g. a real, naturally-played save that already has loose
     items lying around somewhere. Any AnimalCrossing.exe boot save with at least two ordinary
     loose field items (fruit, shells, rocks, etc. -- not weeds, a different interaction, and not
     money bags, which Stage 5A excludes) anywhere in the scanned region is a valid fixture here;
     no manual save preparation is required beyond "a real, played-in town most likely already has
     some."
If neither tier finds enough live items, the accept-path checks (Test A/C/E) are skipped with an
explicit message rather than silently reporting a false pass or fail -- see main().

Foundation-phase migration: all transport plumbing (HELLO+nonce, RDATA/ACK, dedup, ordering,
heartbeats while idle) comes from net_spike_lib.FakeClient; results are matched strictly by
request_id without discarding any other traffic; Test H counts only traffic that ARRIVES during its
idle window. Every original check is kept with its original condition. Test I is kept exactly
(abandon the connection WITHOUT a DISCONNECT, so the host keeps the old slot) plus one added
condition (the answer must echo the new request's own tile), and Test I2 is new: it forces the
case Test I could never reach -- same slot reused after a proper DISCONNECT, SAME request_id,
different tile (see test_reconnect_state.py for the full matrix).

Usage: python3 test_pickup_sync.py <host_ip> <port>
"""
import sys

from net_spike_lib import (
    CandidateQueue,
    CH_RELIABLE,
    EMPTY_NO,
    FakeClient,
    OUT_OF_RANGE_TILE,
    PC_NETGAME_MSG_FIELD_UPDATE,
    PC_NETGAME_MSG_PICKUP_RESULT,
    check,
    make_request_id_counter,
    pump_sleep,
    summary_and_exit_code,
    tile_center,
)

fresh_request_id = make_request_id_counter(1000)  # matches this script's original starting id


def probe(client, ut_x, ut_z, timeout=0.6):
    """One full probe of a single tile: claim to be standing on it, request it, return the
    (accepted, granted_item) outcome (or (False, None) on no response within `timeout`). A probe
    that returns accepted=True has just CONSUMED that tile -- see the module doc comment for why
    this script has no non-destructive way to check a tile's contents. Filters strictly by this
    probe's own request_id (see net_spike_lib.FakeClient._recv_typed()'s doc) so unrelated traffic
    -- another peer's activity, a stale result from an earlier probe -- can never be misread as
    this one's answer."""
    x, y, z = tile_center(ut_x, ut_z)
    client.claim_position(x, y, z)
    rid = fresh_request_id()
    client.send_pickup_request(ut_x, ut_z, rid)
    result = client.recv_pickup_result(timeout=timeout, expect_request_id=rid)
    if result is None:
        return False, None
    accepted, _r_ut_x, _r_ut_z, _r_rid, granted_item = result
    return bool(accepted), (granted_item if accepted else None)


def discover_items(client, count, queue, max_probes=600):
    """Pulls candidate tiles from `queue` (a CandidateQueue), stopping once `count` real,
    currently-occupied tiles have been found (and consumed) or `max_probes` tiles have been tried
    in THIS call, whichever comes first. Returns a list of (ut_x, ut_z, granted_item)."""
    found = []
    tried = 0
    while len(found) < count and tried < max_probes:
        tile = queue.next_tile()
        if tile is None:
            break  # queue exhausted -- nothing left anywhere to try
        tried += 1
        queue.probes_used += 1
        accepted, item = probe(client, *tile)
        if accepted:
            print(f"    discovered live item {item:#x} at tile {tile} after {tried} probe(s) this call")
            found.append((tile[0], tile[1], item))
    return found


def main():
    if len(sys.argv) != 3:
        print("usage: test_pickup_sync.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    results = []

    a = FakeClient("A", host_ip, port)
    a.connect_and_ready()
    b = FakeClient("B", host_ip, port)
    b.connect_and_ready()
    c = FakeClient("C", host_ip, port)
    c.connect_and_ready()
    a.drain_field_updates(timeout=0.3)
    b.drain_field_updates(timeout=0.3)
    c.drain_field_updates(timeout=0.3)

    # --- Test B: empty tile is rejected, no mutation. -------------------------------------------
    # A tile picked far from any plausible discovery-scan range below, to avoid any chance of
    # colliding with a tile a later test's own scan might separately discover and consume.
    empty_ut_x, empty_ut_z = 70, 90
    accepted, _ = probe(a, empty_ut_x, empty_ut_z)
    check("Test B: an already-empty tile is rejected", not accepted, results)

    # --- Test G: an out-of-range coordinate is rejected (different code path than Test B -- this
    # exercises mFI_UtNumCheck()/a NULL mFI_UtNum2UtFG() return, not "classified but empty"). ------
    a.claim_position(*tile_center(0, 0))
    rid = fresh_request_id()
    a.send_pickup_request(255, 255, rid)
    result = a.recv_pickup_result(timeout=0.6, expect_request_id=rid)
    check(
        "Test G: an out-of-range tile coordinate is rejected",
        result is not None and not result[0],
        results,
    )

    # --- Test F: invalid distance is rejected (claim to be on the far side of the map from the
    # target tile). -------------------------------------------------------------------------------
    a.claim_position(0.0, 0.0, 0.0)  # claim origin
    far_ut_x, far_ut_z = 79, 95  # far corner of the addressable town
    rid = fresh_request_id()
    a.send_pickup_request(far_ut_x, far_ut_z, rid)
    result = a.recv_pickup_result(timeout=0.6, expect_request_id=rid)
    check(
        "Test F: a tile far from the claimed position is rejected",
        result is not None and not result[0],
        results,
    )

    # --- Test D: duplicate request (same sender + request_id sent twice) is answered identically,
    # not reprocessed. Uses a guaranteed-empty tile -- dedup applies to REJECTED decisions too (see
    # pc_net_game.c's s_host_pickup_state doc), so no real item is needed for this test. ------------
    dup_ut_x, dup_ut_z = 71, 91
    a.claim_position(*tile_center(dup_ut_x, dup_ut_z))
    dup_rid = fresh_request_id()
    a.send_pickup_request(dup_ut_x, dup_ut_z, dup_rid)
    first = a.recv_pickup_result(timeout=0.6, expect_request_id=dup_rid)
    a.send_pickup_request(dup_ut_x, dup_ut_z, dup_rid)  # exact same request_id again
    second = a.recv_pickup_result(timeout=0.6, expect_request_id=dup_rid)
    check(
        "Test D: a duplicate request_id gets the identical cached answer",
        first is not None and second is not None and first == second,
        results,
    )

    # --- Discovery for Test A (solo valid pickup), Test E (retry-after-accept), and Test C (race).
    # One shared queue (fixture candidates first, then a broad fallback scan) so nothing already
    # tried/consumed by an earlier check is ever probed again -- see CandidateQueue's own doc. -----
    queue = CandidateQueue()
    print("scanning for live field items (Test A)...")
    found_a = discover_items(a, count=1, queue=queue)

    if not found_a:
        print(f"SKIP - Test A/C/E: no live pickupable field item found in {queue.probes_used} probes "
              "(fixture candidates + broad fallback scan) -- this save's town may simply have "
              "nothing loose there right now, and it was not launched with --pickup-test-seed. "
              "Every other check in this script is independent of this and still ran above/below. "
              "Launch the host with --pickup-test-seed for deterministic accept-path coverage.")
    else:
        item_ut_x, item_ut_z, item_a = found_a[0]

        # --- Test A: valid pickup -- request accepted, tile cleared, result has the correct item,
        # and every other connected client converges on the same empty tile via FIELD_UPDATE. ------
        # (The discovery probe above IS this request -- already consumed, already verified accepted
        # by discover_items(); this section verifies the CONVERGENCE side.)
        b_updates = b.drain_field_updates(timeout=1.0)
        c_updates = c.drain_field_updates(timeout=0.5)
        check(
            "Test A: another client (B) sees the tile go empty via FIELD_UPDATE",
            (item_ut_x, item_ut_z, EMPTY_NO) in b_updates,
            results,
        )
        check(
            "Test A: a third client (C) also converges on the same empty tile",
            (item_ut_x, item_ut_z, EMPTY_NO) in c_updates,
            results,
        )

        # --- Test E: retry after an accepted request -- a later, differently-id'd request for the
        # SAME now-empty tile must be rejected, not re-granted (proves the field side of "duplicate
        # requests must never grant an item twice" on an ACCEPTED, not just a rejected, entry --
        # Test D above already covers the rejected case). Reuses Test A's own tile directly; no
        # separate discovery needed since the property under test is "already consumed", which
        # Test A's own probe already established. ----------------------------------------------------
        accepted_replay, _ = probe(a, item_ut_x, item_ut_z)
        check(
            "Test E: a later request for an already-consumed tile is rejected, not re-granted",
            not accepted_replay,
            results,
        )

        # --- Test C: race -- B and C both target the SAME live tile, sent back-to-back before
        # either result is read, so both requests are already queued host-side before either is
        # processed (see Phase 11's exact scenario). Exactly one must be accepted. Pulls candidates
        # from the SAME shared queue (continuing right where Test A's discovery left off), capped
        # so a save/host with nothing further live doesn't turn into an unbounded scan. -------------
        race_result = None
        race_tile = None
        race_probes_budget = 400
        while race_probes_budget > 0:
            tile = queue.next_tile()
            if tile is None:
                break
            race_probes_budget -= 1
            ut_x, ut_z = tile
            x, y, z = tile_center(ut_x, ut_z)
            b.claim_position(x, y, z)
            c.claim_position(x, y, z)
            rid_b = fresh_request_id()
            rid_c = fresh_request_id()
            b.send_pickup_request(ut_x, ut_z, rid_b)
            c.send_pickup_request(ut_x, ut_z, rid_c)  # sent before EITHER result is read
            res_b = b.recv_pickup_result(timeout=0.6, expect_request_id=rid_b)
            res_c = c.recv_pickup_result(timeout=0.6, expect_request_id=rid_c)
            b_ok = res_b is not None and res_b[0]
            c_ok = res_c is not None and res_c[0]
            if b_ok or c_ok:
                race_result = (b_ok, c_ok)
                race_tile = (ut_x, ut_z)
                break

        if race_result is None:
            print("SKIP - Test C (race): no further live item found to race over.")
        else:
            b_ok, c_ok = race_result
            check("Test C: exactly one of the two simultaneous requesters is accepted", b_ok != c_ok, results)
            accepted_again, _ = probe(a, *race_tile)
            check("Test C: the raced tile is empty afterward (not double-granted)", not accepted_again, results)

    # --- Test H: no flooding -- after all of the above, an idle window must not produce repeated
    # unprompted pickup traffic. Counts only messages that ARRIVE during the window (a host-side
    # transport retransmit would be deduplicated by the library and is not game-level traffic). ------
    mark = a.inbox.mark()
    pump_sleep(2.0)
    idle_events = [
        m.msg_type
        for m in a.inbox.peek_all(
            lambda m: m.channel == CH_RELIABLE and m.msg_type in (PC_NETGAME_MSG_PICKUP_RESULT, PC_NETGAME_MSG_FIELD_UPDATE),
            since=mark,
        )
    ]
    check(
        f"Test H: no unprompted pickup traffic during a 2s idle window (saw {len(idle_events)})",
        len(idle_events) == 0,
        results,
    )

    # --- Test I: reconnect safety -- a fresh connection's dedup/pending state must not be
    # poisoned by a previous connection's cached request ids. ---------------------------------------
    a.abandon()  # like the original sock.close(): no DISCONNECT, host keeps a's slot until timeout
    a2 = FakeClient("A2(reconnected)", host_ip, port)
    a2.connect_and_ready()
    a2.drain_field_updates(timeout=0.3)
    # request_id space is intentionally shared/monotonic across this whole script (see
    # fresh_request_id/net_spike_lib.make_request_id_counter), so a2's very first request_id is
    # guaranteed to be one the ORIGINAL connection (as peer slot `a` occupied) may well have
    # already used and cached -- if reconnect-safety were broken, the host might answer from the
    # old peer's stale cache instead of genuinely validating this new connection's request.
    reused_looking_rid = fresh_request_id()
    a2.claim_position(*tile_center(72, 92))
    a2.send_pickup_request(72, 92, reused_looking_rid)
    result = a2.recv_pickup_result(timeout=0.6, expect_request_id=reused_looking_rid)
    check(
        "Test I: a reconnected client's request is genuinely validated, not answered from stale "
        "cached state",
        result is not None and not result[0] and (result[1], result[2]) == (72, 92),
        results,
    )

    # --- Test I2 (foundation phase): the case Test I cannot reach. a2 leaves with a proper
    # DISCONNECT and immediately reconnects: its slot is the lowest free one (a's abandoned slot is
    # still held, B and C are connected), so the new connection REUSES it -- asserted, not assumed.
    # It then sends the SAME request_id as its previous connection's last request, for a different
    # tile. A stale per-slot dedup cache would replay (72,92)'s answer. ---------------------------------
    slot_before = a2.assigned_peer_id
    a2.reconnect_and_ready()
    check(
        f"Test I2 precondition: the reconnected client reused slot {slot_before} (got {a2.assigned_peer_id})",
        a2.assigned_peer_id == slot_before,
        results,
    )
    oor_x, oor_z = OUT_OF_RANGE_TILE
    a2.send_pickup_request(oor_x, oor_z, reused_looking_rid)  # SAME request_id, different tile
    result_i2 = a2.recv_pickup_result(timeout=0.6, expect_request_id=reused_looking_rid)
    check(
        "Test I2: same slot + same request_id after a clean reconnect is validated fresh (answer echoes "
        "(255,255), not the previous connection's cached (72,92))",
        result_i2 is not None and not result_i2[0] and (result_i2[1], result_i2[2]) == (oor_x, oor_z),
        results,
    )

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
