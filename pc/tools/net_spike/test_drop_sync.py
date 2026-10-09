#!/usr/bin/env python3
"""test_drop_sync.py - Stage 5B-1 (authoritative plain outdoor item drop) synthetic network test.

Hand-crafts wire bytes (like test_pickup_sync.py) to drive a REAL, currently-running
`AnimalCrossing.exe --host <port>` process through the new DROP_REQUEST / DROP_RESULT protocol
(see pc/src/pc_net_game.c), and confirms the existing PC_NETGAME_MSG_FIELD_UPDATE broadcast (reused
unchanged from Stage 5A) carries the dropped item to every other connected client.

This is a deliberately SEPARATE, self-contained script rather than one merged into
test_pickup_sync.py (per the Stage 5B-1 implementation instructions: keep the two test scripts
independent) -- each still has its own main() and test sequence, and its own drop-specific fixture
prep (find_and_empty_tile()/find_occupied_tile() below). Only the wire-format/handshake/fixture-tile
plumbing the two scripts already duplicated verbatim (HELLO/IDENTITY handshake, claim_position(),
the PICKUP_REQUEST/PICKUP_RESULT protocol this script reuses to prepare its own fixtures, the
FIXTURE_CANDIDATE_TILES literal, check()/the summary footer) now lives in the shared
net_spike_lib.py -- see that module's own doc comment for why that's a safe, behavior-preserving
extraction, not a merge of the two scripts.

Fixture strategy (see the Stage 5B audit's "Deterministic Fixture Strategy" and this stage's own
implementation report for why no NEW runtime fixture was needed): this script reuses the EXISTING
--pickup-test-seed fixture (pc_net_game.c's pcnetgame_run_pickup_test_seed(), 30 tiles, one per
acre, each holding ITM_FOOD_APPLE until picked up) two ways:
  1. An UNTOUCHED fixture tile is a ready-made "known occupied, legal, reachable" target -- used
     non-destructively here (attempting to drop onto it and observing the rejection never mutates
     anything; a rejected drop is always a complete no-op -- see pcnetgame_handle_host_drop_request()).
  2. A fixture tile that this script itself first PICKS UP (via the existing, unmodified Stage 5A
     PICKUP_REQUEST/PICKUP_RESULT protocol -- this script speaks that too) becomes a ready-made
     "known empty, legal, reachable" target for the drop accept-path tests.
Because the host has no way to verify a claimed pocket slot/item (an explicit, documented trust
boundary -- see pc_net_game.c's pcnetgame_validate_and_resolve_drop()), this script never needs a
real Now_Private/pocket inventory of its own to exercise the host's request-handling logic: a
FakeClient can claim any plausible slot/item and the host will process it exactly as it would a
real client's claim, subject only to the field-side checks it can actually perform.

Run the host with --pickup-test-seed for deterministic fixture tiles (see run_net_spike.py or
AnimalCrossing.exe --host <port> --pickup-test-seed). Without that flag, every check that needs a
known field-tile state is skipped with an explicit message rather than reporting a false pass/fail.

Foundation-phase migration: transport plumbing (HELLO+nonce, RDATA/ACK, dedup, ordering) comes from
net_spike_lib.FakeClient, which now also owns send_drop_request()/recv_drop_result() (identical
signatures and return tuples to this script's former subclass). Results are matched strictly by
request_id without discarding other traffic. Every original check keeps its original condition;
the explicit send_heartbeat() calls are kept (the library's Hub also keeps idle clients alive).
Test J is kept exactly (abandon WITHOUT a DISCONNECT) and Test J2 is new: same slot reused after a
proper DISCONNECT + the SAME request_id for a different tile (see test_reconnect_state.py).

Usage: python3 test_drop_sync.py <host_ip> <port>
"""
import sys

from net_spike_lib import (
    CandidateQueue,
    DROP_RESULT_SPEC,
    FakeClient,
    OUT_OF_RANGE_TILE,
    check,
    make_request_id_counter,
    summary_and_exit_code,
    tile_center,
)

ITM_FOOD_APPLE = 0x2800  # ITM_FOOD_START(0x2800)+0 -- matches the seed item
                         # pcnetgame_run_pickup_test_seed() places
ITM_FOOD_CHERRY = 0x2801  # ITM_FOOD_START(0x2800)+1
ITM_SIGNBOARD = 0x251E  # ITM_ETC_START(0x2500) + 30 -- excluded by pcnetgame_is_droppable_item()

fresh_request_id = make_request_id_counter(2000)  # a disjoint range from test_pickup_sync.py's own
                                                   # counter, in case both scripts are ever
                                                   # (accidentally) pointed at the same host

assert DROP_RESULT_SPEC.tuple_cls._fields == (
    "msg_type", "accepted", "ut_x", "ut_z", "request_id", "placed_item", "reserved"
), "net_spike_lib.DROP_RESULT_SPEC drifted from PCNetGameDropResultMsg"
# FakeClient (net_spike_lib) provides: connect_and_ready(), claim_position(), send_pickup_request()/
# recv_pickup_result() (used below to prepare fixtures), send_drop_request()/recv_drop_result()
# -> (accepted, ut_x, ut_z, request_id, placed_item), drain_field_updates(), send_heartbeat().


def pickup_one(client, ut_x, ut_z, timeout=0.6):
    """Non-destructive-on-failure, destructive-on-success: claims to stand on (ut_x, ut_z) and
    sends a real PICKUP_REQUEST. Returns True if accepted (meaning the tile is now EMPTY_NO --
    used here purely as a fixture-preparation step, not as a Stage 5B-1 check in its own right;
    Stage 5A's own pickup protocol is unmodified and already covered by test_pickup_sync.py)."""
    x, y, z = tile_center(ut_x, ut_z)
    client.claim_position(x, y, z)
    rid = fresh_request_id()
    client.send_pickup_request(ut_x, ut_z, rid)
    result = client.recv_pickup_result(timeout=timeout, expect_request_id=rid)
    return result is not None and bool(result[0])


def find_and_empty_tile(client, queue, max_probes=60):
    """Consumes tiles from `queue` via real pickups until one succeeds (leaving that tile
    EMPTY_NO), or `max_probes` are exhausted. Returns (ut_x, ut_z) or None."""
    tried = 0
    while tried < max_probes:
        tile = queue.next_tile()
        if tile is None:
            return None
        tried += 1
        if pickup_one(client, *tile):
            print(f"    prepared known-empty tile {tile} after {tried} probe(s)")
            return tile
    return None


NEIGHBOR_OFFSETS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


def find_accepted_neighbor(client, cur_ut_x, cur_ut_z, facing, item):
    """Stage 5B-2: tries a drop request against each of the 8 immediate neighbor tiles of
    (cur_ut_x, cur_ut_z) in turn, STOPPING at the first accept. Returns (target_x, target_z, result)
    for the winner, or None if all 8 were rejected.

    Deliberately does NOT try to predict, from Python, which single neighbor
    pcnetgame_resolve_vanilla_drop_tile()'s facing-dependent zigzag search would pick -- this host
    process loads a REAL town save with real, otherwise-unknown-to-this-script terrain/collision
    (trees, buildings, paths) that can legitimately block any given direction, so a hardcoded
    "the +X neighbor must win" assumption is fragile against whatever this specific save's terrain
    actually looks like near a given fixture tile.

    MUST stop at the first accept rather than trying all 8 unconditionally: an accepted drop is NOT
    a no-op like a rejected one (see pcnetgame_handle_host_drop_request()'s own doc) -- it fills
    that neighbor, which changes which candidate the search would resolve to on any FURTHER call
    with the same position/facing (whichever candidate was highest-priority-and-legal before is now
    occupied, so the search correctly moves on to the next one). Continuing to probe after a first
    accept was observed, during this function's own development, to legitimately accept a SECOND,
    different neighbor on a later iteration -- correct behavior from the host's point of view (each
    call re-validates fresh live state), but not "there is exactly one legal neighbor" the way a
    naive read of trying all 8 unconditionally would suggest."""
    for dx, dz in NEIGHBOR_OFFSETS:
        target_x, target_z = cur_ut_x + dx, cur_ut_z + dz
        client.claim_position(*tile_center(cur_ut_x, cur_ut_z), facing=facing)
        rid = fresh_request_id()
        client.send_drop_request(0, item, target_x, target_z, rid)
        result = client.recv_drop_result(timeout=0.6, expect_request_id=rid)
        if result is not None and result[0]:
            return (target_x, target_z, result)
    return None


def find_occupied_tile(client, queue, ordinary_item, max_probes=30):
    """Non-destructive: scans tiles from `queue`, attempting a (well-formed, otherwise-legal)
    DROP_REQUEST against each. The FIRST rejection is reported as a known-occupied tile -- a
    rejected drop never mutates anything (see pcnetgame_handle_host_drop_request()'s doc), so this
    never consumes/destroys the tile it finds, unlike find_and_empty_tile() above. Returns
    (ut_x, ut_z) or None. Every fixture tile is freshly seeded and unpicked at this point in the
    script (this is called before any pickup-based fixture preparation runs), so a rejection here
    is overwhelmingly likely to mean "occupied" rather than "acre not resident" -- see the module
    doc comment's "Fixture strategy" section."""
    tried = 0
    while tried < max_probes:
        tile = queue.next_tile()
        if tile is None:
            return None
        tried += 1
        ut_x, ut_z = tile
        x, y, z = tile_center(ut_x, ut_z)
        client.claim_position(x, y, z)
        rid = fresh_request_id()
        client.send_drop_request(0, ordinary_item, ut_x, ut_z, rid)
        result = client.recv_drop_result(timeout=0.6, expect_request_id=rid)
        if result is not None and not result[0]:
            print(f"    found known-occupied tile {tile} after {tried} probe(s) (left untouched)")
            return tile
    return None


def main():
    if len(sys.argv) != 3:
        print("usage: test_drop_sync.py <host_ip> <port>")
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

    # One shared queue for the whole run so Test B's non-destructive scan and the later
    # pickup-based fixture preparation never target the same tile twice. fallback=False: drop's
    # fixture strategy only ever needs the --pickup-test-seed fixture tiles themselves (see the
    # module doc comment), never a broader scan.
    queue = CandidateQueue(fallback=False)

    # --- Test E: an out-of-range coordinate is rejected. -------------------------------------------
    a.claim_position(*tile_center(0, 0))
    rid = fresh_request_id()
    a.send_drop_request(0, ITM_FOOD_APPLE, 255, 255, rid)
    result = a.recv_drop_result(timeout=0.6, expect_request_id=rid)
    check("Test E: an out-of-range tile coordinate is rejected", result is not None and not result[0], results)

    # --- Test F: an unsupported item classification is rejected, even for an otherwise-legal
    # position (claims to stand exactly at the target tile, so the ONLY thing that can be wrong is
    # the claimed item -- see pcnetgame_is_droppable_item()'s ITM_SIGNBOARD exclusion). -------------
    # Foundation phase: the original target (70,8) lies in the border block, which protocol v2
    # (pcfa_town_ut_to_acre_tile) rejects as "not a town tile" -- the check would have passed for the
    # wrong reason. The target is now a tile this script first empties with a real pickup (a known
    # legal, empty, persistent town tile), and a control drop of an ordinary item on the SAME tile
    # from the same position must then be accepted, proving the signboard was rejected for its class.
    sign_tile = find_and_empty_tile(a, queue)
    if sign_tile is None:
        print("SKIP - Test F: no live fixture item to prepare a legal empty tile (host not seeded?)")
    else:
        sign_ut_x, sign_ut_z = sign_tile
        a.claim_position(*tile_center(sign_ut_x, sign_ut_z))
        rid = fresh_request_id()
        a.send_drop_request(0, ITM_SIGNBOARD, sign_ut_x, sign_ut_z, rid)
        result = a.recv_drop_result(timeout=0.6, expect_request_id=rid)
        check(
            "Test F: an unsupported item classification (signboard) is rejected",
            result is not None and not result[0],
            results,
        )
        rid = fresh_request_id()
        a.send_drop_request(0, ITM_FOOD_CHERRY, sign_ut_x, sign_ut_z, rid)
        control = a.recv_drop_result(timeout=0.6, expect_request_id=rid)
        check(
            "Test F control: an ordinary item on the SAME tile/position is accepted (so the signboard "
            "rejection was the item class, not the tile)",
            control is not None and control[0],
            results,
        )

    # --- Test G: reach validation -- current-tile-only. Two sub-cases. ------------------------------
    # G1: claim to be at the origin, target a tile far across the map (mirrors test_pickup_sync.py's
    # Test F).
    a.claim_position(0.0, 0.0, 0.0)
    far_ut_x, far_ut_z = 79, 95
    rid = fresh_request_id()
    a.send_drop_request(0, ITM_FOOD_APPLE, far_ut_x, far_ut_z, rid)
    result = a.recv_drop_result(timeout=0.6, expect_request_id=rid)
    check("Test G1: a tile far from the claimed position is rejected", result is not None and not result[0], results)

    # G2: claim to stand exactly on one tile, but request its immediate neighbor -- this is the
    # precise "current tile only" check (pcnetgame_validate_and_resolve_drop()'s exact-tile-match),
    # not merely a coarse distance bound.
    base_ut_x, base_ut_z = 60, 60  # off the fixture list; only used for its coordinates, never for
                                   # item state
    a.claim_position(*tile_center(base_ut_x, base_ut_z))
    rid = fresh_request_id()
    a.send_drop_request(0, ITM_FOOD_APPLE, base_ut_x + 1, base_ut_z, rid)
    result = a.recv_drop_result(timeout=0.6, expect_request_id=rid)
    check(
        "Test G2: standing on one tile but targeting its neighbor is rejected (current-tile-only)",
        result is not None and not result[0],
        results,
    )

    # --- Test B: a known-occupied tile is rejected, and is left untouched by the rejection. --------
    occupied_tile = find_occupied_tile(a, queue, ITM_FOOD_APPLE)
    # B and C have sat idle (no packet sent) since their initial handshake -- a slow fixture scan
    # above plus the neighbor-search probing below can approach PCNET_TIMEOUT_MS (5000ms, pc_net.c)
    # of apparent silence from their end, which would disconnect them before Test C needs them. See
    # send_heartbeat()'s own doc (net_spike_lib.py) for why FakeClient needs this explicitly, unlike
    # a real game client's own per-frame transport loop.
    b.send_heartbeat()
    c.send_heartbeat()
    if occupied_tile is None:
        print(
            "SKIP - Test B: no occupied fixture tile found (host likely not launched with "
            "--pickup-test-seed, or every scanned tile's acre had not streamed in yet)."
        )
    else:
        ut_x, ut_z = occupied_tile
        # Re-probe the SAME tile again to confirm the earlier rejection truly left it untouched
        # (still occupied, not e.g. accidentally cleared) -- a second independent observation, not
        # just trusting the first.
        a.claim_position(*tile_center(ut_x, ut_z))
        rid = fresh_request_id()
        a.send_drop_request(0, ITM_FOOD_APPLE, ut_x, ut_z, rid)
        result = a.recv_drop_result(timeout=0.6, expect_request_id=rid)
        check(
            "Test B: a known-occupied tile is rejected, and remains occupied afterward",
            result is not None and not result[0],
            results,
        )

    # --- Test M/N (Stage 5B-2): current tile occupied falls through to vanilla's own neighbor
    # search. Reuses Test B's own occupied fixture tile as the "current tile" to stand on (Test B
    # already confirmed above that a rejected drop leaves it genuinely untouched, so it is still
    # occupied here). See find_accepted_neighbor()'s own doc for why this stops at the first accept
    # instead of trying all 8 unconditionally -- this host's real town save has real terrain this
    # script cannot see in advance, so it cannot predict which neighbor wins, and an accepted
    # (unlike a rejected) probe mutates the field, changing what a FURTHER call would resolve to. ---
    if occupied_tile is None:
        print("SKIP - Test M/N: no occupied fixture tile available (see Test B's own skip above).")
    else:
        cur_ut_x, cur_ut_z = occupied_tile
        FACING_TEST = 16384  # 90 degrees, same 16-bit engine-angle convention test_move_rotation.py
                              # already uses (32768 == 180 degrees) -- the specific value doesn't
                              # matter here since every legal neighbor is tried regardless of order.
        b.drain_field_updates(timeout=0.2)
        c.drain_field_updates(timeout=0.2)
        won = find_accepted_neighbor(a, cur_ut_x, cur_ut_z, FACING_TEST, ITM_FOOD_CHERRY)
        check(
            "Test M: current tile occupied falls through to a legal neighbor (Stage 5B-2)",
            won is not None,
            results,
        )
        if won is not None:
            won_ut_x, won_ut_z, res = won
            check(
                "Test M: the accepted neighbor is placed with the claimed item",
                res[4] == ITM_FOOD_CHERRY,
                results,
            )
            b_updates_m = b.drain_field_updates(timeout=1.0)
            check(
                "Test M: another client (B) sees the accepted neighbor tile become occupied via "
                "FIELD_UPDATE",
                (won_ut_x, won_ut_z, ITM_FOOD_CHERRY) in b_updates_m,
                results,
            )

            # Test N: re-request that SAME now-filled neighbor tile again -- must now be rejected
            # (already occupied), the same "recheck after fill" pattern Test A/C already use for
            # their own tiles, proving this is a genuine, persistent field mutation, not a one-shot
            # fluke, and that the host doesn't just keep granting the same tile on every request.
            rid_n = fresh_request_id()
            a.claim_position(*tile_center(cur_ut_x, cur_ut_z), facing=FACING_TEST)
            a.send_drop_request(0, ITM_FOOD_APPLE, won_ut_x, won_ut_z, rid_n)
            recheck_n = a.recv_drop_result(timeout=0.6, expect_request_id=rid_n)
            check(
                "Test N: the same neighbor tile, once filled, rejects a further drop",
                recheck_n is not None and not recheck_n[0],
                results,
            )

    b.send_heartbeat()  # see the identical call/comment before Test B above
    c.send_heartbeat()

    # --- Test A: basic accept. Prepare a known-empty tile via a real pickup, then drop onto it.
    # Deliberately claims a NON-ZERO facing (-16384, 90 degrees the other way from Test M/N's
    # FACING_PLUS_X above) to also confirm facing is irrelevant whenever the current tile is
    # already legal -- pcnetgame_resolve_vanilla_drop_tile() only ever consults facing after the
    # current-tile check has already failed (see that function's own doc). -----------------------
    empty_tile = find_and_empty_tile(a, queue)
    if empty_tile is None:
        print(
            "SKIP - Test A/C/D: no live pickupable fixture item found to empty a target tile from "
            "-- host likely not launched with --pickup-test-seed. Every other check in this script "
            "is independent of this and still ran above/below."
        )
    else:
        ut_x, ut_z = empty_tile
        b.drain_field_updates(timeout=0.2)  # clear whatever the pickup above already broadcast
        c.drain_field_updates(timeout=0.2)

        drop_item = ITM_FOOD_CHERRY  # deliberately different from the apple used to seed the tile,
                                     # so a correct placed_item unambiguously proves the HOST's
                                     # claimed_item (not some leftover/previous value) was written
        a.claim_position(*tile_center(ut_x, ut_z), facing=-16384)  # see this block's header comment
        rid_a = fresh_request_id()
        a.send_drop_request(3, drop_item, ut_x, ut_z, rid_a)
        result = a.recv_drop_result(timeout=0.6, expect_request_id=rid_a)
        check(
            "Test A: a valid drop onto a known-empty (legal) current tile is accepted regardless of "
            "facing",
            result is not None and result[0] and result[4] == drop_item,
            results,
        )

        b_updates = b.drain_field_updates(timeout=1.0)
        c_updates = c.drain_field_updates(timeout=0.5)
        check(
            "Test A: another client (B) sees the tile become occupied via FIELD_UPDATE",
            (ut_x, ut_z, drop_item) in b_updates,
            results,
        )
        check(
            "Test A: a third client (C) also converges on the same occupied tile",
            (ut_x, ut_z, drop_item) in c_updates,
            results,
        )

        # --- Test D: duplicate accepted request -- the SAME (peer, request_id) as Test A's own
        # successful drop, resent IMMEDIATELY (before any other request from this same peer). Must
        # replay the identical cached decision, not re-validate (which would now incorrectly see
        # "occupied" and reject its own prior success), and must not re-broadcast a second
        # FIELD_UPDATE. This must run before any other request from peer "a", since the host's dedup
        # cache is deliberately a single slot per peer (mirrors Stage 5A pickup's own design: "the
        # most recently PROCESSED request from this peer is all that's ever needed") -- an
        # intervening, different request_id from the same peer legitimately overwrites it, which
        # would make this test exercise a real but different scenario (a late/stale duplicate
        # arriving after newer traffic) rather than the immediate-retry case it's meant to check. ----
        b.drain_field_updates(timeout=0.1)
        c.drain_field_updates(timeout=0.1)
        a.send_drop_request(3, drop_item, ut_x, ut_z, rid_a)  # exact same request_id as Test A
        replay = a.recv_drop_result(timeout=0.6, expect_request_id=rid_a)
        check(
            "Test D: a duplicate request_id gets the identical cached accepted answer",
            replay is not None and replay == result,
            results,
        )
        b_replay_updates = b.drain_field_updates(timeout=0.5)
        check(
            "Test D: the duplicate does not trigger a second FIELD_UPDATE broadcast",
            (ut_x, ut_z, drop_item) not in b_replay_updates,
            results,
        )

        # Confirm "target tile becomes occupied" independently of the FIELD_UPDATE broadcast: a
        # fresh drop attempt against the SAME tile, from a DIFFERENT request_id, must now be
        # rejected (occupied), never double-accepted. Runs AFTER Test D deliberately -- see Test D's
        # own comment for why introducing a different request_id from this same peer any earlier
        # would interfere with the dedup-cache check Test D is specifically targeting.
        rid_check = fresh_request_id()
        a.claim_position(*tile_center(ut_x, ut_z))
        a.send_drop_request(0, ITM_FOOD_APPLE, ut_x, ut_z, rid_check)
        recheck = a.recv_drop_result(timeout=0.6, expect_request_id=rid_check)
        check(
            "Test A: the tile is genuinely occupied afterward (a second, different drop is rejected)",
            recheck is not None and not recheck[0],
            results,
        )

        b.send_heartbeat()  # see the identical call/comment before Test B above -- B/C have been
        c.send_heartbeat()  # idle since Test A/D above, and are about to be needed for the race

        # --- Test C: race -- B and C both target the SAME empty tile, sent back-to-back before
        # either result is read. Exactly one must be accepted. Prepares its OWN separate empty
        # tile (never Test A's, which is now occupied). ------------------------------------------------
        race_tile = find_and_empty_tile(a, queue)
        if race_tile is None:
            print("SKIP - Test C (race): no further live item found to prepare a race tile from.")
        else:
            r_ut_x, r_ut_z = race_tile
            x, y, z = tile_center(r_ut_x, r_ut_z)
            b.claim_position(x, y, z)
            c.claim_position(x, y, z)
            rid_b = fresh_request_id()
            rid_c = fresh_request_id()
            b.send_drop_request(1, ITM_FOOD_APPLE, r_ut_x, r_ut_z, rid_b)
            c.send_drop_request(1, ITM_FOOD_APPLE, r_ut_x, r_ut_z, rid_c)  # sent before EITHER result is read
            res_b = b.recv_drop_result(timeout=0.6, expect_request_id=rid_b)
            res_c = c.recv_drop_result(timeout=0.6, expect_request_id=rid_c)
            b_ok = res_b is not None and res_b[0]
            c_ok = res_c is not None and res_c[0]
            check("Test C: exactly one of the two simultaneous requesters is accepted", b_ok != c_ok, results)

            rid_check2 = fresh_request_id()
            a.claim_position(*tile_center(r_ut_x, r_ut_z))
            a.send_drop_request(0, ITM_FOOD_APPLE, r_ut_x, r_ut_z, rid_check2)
            recheck2 = a.recv_drop_result(timeout=0.6, expect_request_id=rid_check2)
            check(
                "Test C: the raced tile holds exactly one item afterward (not double-placed)",
                recheck2 is not None and not recheck2[0],
                results,
            )

    # --- Test J: reconnect safety -- a fresh connection's dedup state must not be poisoned by a
    # previous connection's cached drop decision. Mirrors test_pickup_sync.py's own Test I, and
    # specifically exercises pcnetgame_reset_all_host_peer_state()'s new single dispatch point
    # (pc_net_game.c): both the disconnect-time reset and the belt-and-suspenders fresh-READY reset
    # now go through that one function for BOTH pickup and drop caches together, so this is also a
    # real check that the drop side of that refactor is wired up correctly, not just pickup's. -------
    a.abandon()  # like the original sock.close(): no DISCONNECT, host keeps a's slot until timeout
    a2 = FakeClient("A2(reconnected)", host_ip, port)
    a2.connect_and_ready()
    a2.drain_field_updates(timeout=0.3)
    # request_id space is shared/monotonic across this whole script, so a2's very first request_id
    # is guaranteed to be one the ORIGINAL connection (as peer slot `a` occupied) may well have
    # already used and cached -- if reconnect-safety were broken, the host might answer from the
    # old peer's stale cache instead of genuinely validating this new connection's request.
    # (72, 92): the exact same tile test_pickup_sync.py's own Test I already establishes is a
    # valid, in-bounds, ordinary tile with nothing on it (its own check there is "a pickup here is
    # rejected" -- i.e. genuinely empty), reused here rather than a freshly-guessed coordinate so
    # this test doesn't carry its own independent assumption about which tiles are in-bounds/empty.
    reused_looking_rid = fresh_request_id()
    a2.claim_position(*tile_center(72, 92))
    a2.send_drop_request(0, ITM_FOOD_APPLE, 72, 92, reused_looking_rid)
    result_j = a2.recv_drop_result(timeout=0.6, expect_request_id=reused_looking_rid)
    check(
        "Test J: a reconnected client's drop request is genuinely validated -- accepted for THIS "
        "tile, not a stale cached decision echoing a different one",
        result_j is not None and result_j[0] and result_j[1] == 72 and result_j[2] == 92,
        results,
    )

    # --- Test J2 (foundation phase): the case Test J cannot reach -- a2 leaves with a proper
    # DISCONNECT and reconnects into the SAME slot (asserted: a's abandoned slot is still held and
    # B/C are connected, so a2's freed slot is the lowest free one), then resends the SAME request_id
    # for a different tile. A stale per-slot drop cache would replay the accepted (72,92) placement. ----
    slot_before = a2.assigned_peer_id
    a2.reconnect_and_ready()
    check(
        f"Test J2 precondition: the reconnected client reused slot {slot_before} (got {a2.assigned_peer_id})",
        a2.assigned_peer_id == slot_before,
        results,
    )
    oor_x, oor_z = OUT_OF_RANGE_TILE
    a2.send_drop_request(0, ITM_FOOD_APPLE, oor_x, oor_z, reused_looking_rid)  # SAME request_id
    result_j2 = a2.recv_drop_result(timeout=0.6, expect_request_id=reused_looking_rid)
    check(
        "Test J2: same slot + same request_id after a clean reconnect is validated fresh (rejected, echoes "
        "(255,255)), not the previous connection's cached (72,92) acceptance",
        result_j2 is not None and not result_j2[0] and (result_j2[1], result_j2[2]) == (oor_x, oor_z),
        results,
    )

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
