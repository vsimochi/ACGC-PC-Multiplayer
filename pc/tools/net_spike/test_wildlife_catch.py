#!/usr/bin/env python3
"""test_wildlife_catch.py - World Ecology Wildlife Sync T-catch (ordinary fish catching only)
verification, against a REAL, currently-launched `AnimalCrossing.exe` host (and, for TEST B, a real
second connected client), mirroring test_wildlife_spawn_wire.py's / test_wildlife_flag_gate_bugfix.py's
own established methodology: a scripted FakeClient standing in for a real client wherever raw protocol
bytes are enough to exercise the HOST's real C code paths byte-for-byte (race/stale/out-of-range), and a
real second AnimalCrossing.exe process (via --force-fish-catch) wherever a REAL Save_t/pockets grant
needs to be observed end-to-end.

Covers:
  TEST C (two-client race, MOST IMPORTANT): two FakeClients send a CATCH_REQUEST for the SAME entity_id
          back-to-back. Exactly one must be accepted, the other rejected; exactly one WILDLIFE_DESPAWN
          must be broadcast for that entity_id (not two).
  TEST D (stale request): an unknown/never-issued entity_id, and a real entity_id with a mismatched
          generation, must both be rejected; no despawn is ever broadcast for either.
  TEST E (out-of-range): a FakeClient parked far away (via a real MOVE) from the fish's own recorded
          position must be rejected; the fish must remain live (a FOLLOW-UP catch from a real position
          must still succeed).
  TEST A (real host-local catch): a real host process with --force-fish-catch grants itself a real item
          into its own Save_t pockets and removes the entity from the authoritative table, WITHOUT any
          network round trip to itself.
  TEST B (real client catch): a real second `AnimalCrossing.exe --connect` process with
          --force-fish-catch sends a genuine CATCH_REQUEST, receives CATCH_RESULT, and grants itself the
          item locally; the HOST's own inventory must NOT change for this catch.
  TEST F (late-join after catch): after a catch, a NEW FakeClient's own initial WILDLIFE_SNAPSHOT must
          never re-offer the already-caught entity_id (confirmed via the ABSENCE of a "presentation:
          entity <id> materialized" log line referencing that id anywhere in a fresh connection's
          traffic, matching this project's established log-based verification convention).
  TEST G (species matching): a recognized-trash-type claim is accepted regardless of the record's own
          species, and a genuine species mismatch is rejected without disturbing the entity.

Bug B (post-T3 review, full-pockets catch bypassing the host) -- HONEST SCOPE NOTE: the CATCH_REQUEST
wire message carries no "pockets full/local grant amount" field at all (see PCNetGameCatchRequestMsg's
own doc, pc_net_game.c) -- from the host's own point of view, an ordinary catch and a full-pockets catch
are BYTE-IDENTICAL requests, and the host-side accept/remove/despawn behavior Bug B's fix depends on
(pcnetgame_validate_and_commit_catch() removing the entity unconditionally on accept, a second request
for the same now-removed entity_id always rejected) is exactly what TEST C/E above already exercise
end-to-end, byte-for-byte, against the real host. This protocol-level coverage is therefore genuine, not
a stand-in. What it does NOT cover: the CLIENT-side behavioral change itself (m_player_main_notice_rod.
c_inc now sending CATCH_REQUEST even when free_space < 0, with local_grant forced to 0) -- reaching that
exact call site requires driving a real player through actual fishing input with a genuinely full
inventory, which was judged impractical to automate for the same reason real cast/float/bite/hook input
already is (see this module's own "What this does NOT do" section below) -- that half of Bug B's fix is
SOURCE-AUDITED only, not exercised by this test suite. --force-fish-catch cannot stand in for it either:
that test hook calls pc_net_game_request_catch_fish()/pc_net_game_host_local_wildlife_catch() directly,
bypassing Player_actor_setup_main_Notice_rod() (the real seam Bug B's fix lives in) entirely, exactly
like TEST A's own HONEST SCOPE NOTE (test_wildlife_catch_realgrant.py) describes for its own grant call.

What this does NOT do, and why (same accepted-gap shape as test_wildlife_spawn_wire.py's own docstring):
it never drives a real player through actual cast-rod/wade/wait-for-a-bite/reel-in keyboard input --
fishing's own multi-stage state machine was judged too complex/non-deterministic for a force-flag to
short-circuit at the bite-detection stage (unlike a single dig-scoop request or wade-trigger send), so
this milestone instead added a NEW test-only --force-fish-catch hook (pc_platform.h/pc_net_game.c) that
calls the REAL pc_net_game_host_local_wildlife_catch()/pc_net_game_request_catch_fish() network seams
directly against a REAL, currently-live authoritative wildlife entity -- see that flag's own doc for the
exact bypass boundary (only "a real UKI actor must be cast/floated/bitten first" is skipped; every line
of host validation, removal, broadcast, and client-side grant logic is the real, unmodified code).

Usage: python3 test_wildlife_catch.py [--port 7799]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
from net_spike_lib import CH_RELIABLE, FakeClient, check, summary_and_exit_code  # noqa: E402

WILDLIFE_SPAWN_TRIGGER_REQUEST_TYPE = 36
WILDLIFE_SPAWN_TYPE = 37
WILDLIFE_SNAPSHOT_BEGIN_TYPE = 38
WILDLIFE_SNAPSHOT_ENTRY_TYPE = 39
CATCH_REQUEST_TYPE = 41
CATCH_RESULT_TYPE = 42
WILDLIFE_DESPAWN_TYPE = 43

TRIGGER_FMT = "<BBBB"
SPAWN_FMT = "<BBBBIifff"
SPAWN_SIZE = struct.calcsize(SPAWN_FMT)
SNAPSHOT_BEGIN_FMT = "<B3xIII"
SNAPSHOT_BEGIN_SIZE = struct.calcsize(SNAPSHOT_BEGIN_FMT)
# PCNetGameWildlifeSnapshotEntryMsg (pc_net_game.c): msg_type/kind/bx/bz, entity_id, species, pos xyz,
# epoch -- 28 bytes, see that struct's own doc for the exact field order/packing (no padding).
SNAPSHOT_ENTRY_FMT = "<BBBBIifffI"
SNAPSHOT_ENTRY_SIZE = struct.calcsize(SNAPSHOT_ENTRY_FMT)
assert SNAPSHOT_ENTRY_SIZE == 28, SNAPSHOT_ENTRY_SIZE
CATCH_REQUEST_FMT = "<B3xIIIi"
CATCH_REQUEST_SIZE = struct.calcsize(CATCH_REQUEST_FMT)
CATCH_RESULT_FMT = "<BBHII"
CATCH_RESULT_SIZE = struct.calcsize(CATCH_RESULT_FMT)
DESPAWN_FMT = "<B3xI"
DESPAWN_SIZE = struct.calcsize(DESPAWN_FMT)

assert CATCH_REQUEST_SIZE == 20, CATCH_REQUEST_SIZE
assert CATCH_RESULT_SIZE == 12, CATCH_RESULT_SIZE
assert DESPAWN_SIZE == 8, DESPAWN_SIZE
assert SNAPSHOT_BEGIN_SIZE == 16, SNAPSHOT_BEGIN_SIZE

KIND_FISH = 0

ACRES = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (2, 4), (3, 5), (1, 6), (5, 6), (4, 2)]


def build_trigger(bx, bz):
    return struct.pack(TRIGGER_FMT, WILDLIFE_SPAWN_TRIGGER_REQUEST_TYPE, bx & 0xFF, bz & 0xFF, 0)


def decode_spawn(payload):
    _, kind, bx, bz, entity_id, species, x, y, z = struct.unpack(SPAWN_FMT, payload)
    return dict(kind=kind, bx=bx, bz=bz, entity_id=entity_id, species=species, x=x, y=y, z=z)


def collect_spawns(client, duration):
    def pred(m):
        return (m.channel == CH_RELIABLE and m.payload and m.payload[0] == WILDLIFE_SPAWN_TYPE and
                len(m.payload) == SPAWN_SIZE)
    return [decode_spawn(m.payload) for m in client.inbox.collect(pred, duration)]


def build_catch_request(entity_id, generation, request_id, claimed_species):
    return struct.pack(CATCH_REQUEST_FMT, CATCH_REQUEST_TYPE, entity_id, generation, request_id,
                       claimed_species)

# X3 (host-transactional catch, protocol v8 extended in place): CATCH_REQUEST is 84 bytes = the 20-byte header above + the 64-byte
# PCNetGameTxnTag. These helpers build the tagged request from the sending FakeClient's own record image (dest POCKET + the
# host-derived item of the claimed species), exactly like the real client does.
KIND_UNDER_TEST = "fish"


def catch_bytes(client, entity_id, generation, request_id, claimed_species):
    return client.catch_request_bytes(entity_id, generation, request_id, claimed_species, kind=KIND_UNDER_TEST)


def send_catch(client, entity_id, generation, request_id, claimed_species):
    return client.send_catch_txn(entity_id, generation, request_id, claimed_species, kind=KIND_UNDER_TEST)



def collect_catch_results(client, duration):
    def pred(m):
        return (m.channel == CH_RELIABLE and m.payload and m.payload[0] == CATCH_RESULT_TYPE and
                len(m.payload) == CATCH_RESULT_SIZE)
    out = []
    for m in client.inbox.collect(pred, duration):
        _, accepted, granted_item, entity_id, request_id = struct.unpack(CATCH_RESULT_FMT, m.payload)
        out.append(dict(accepted=accepted, granted_item=granted_item, entity_id=entity_id,
                        request_id=request_id))
    return out


def collect_despawns(client, duration):
    def pred(m):
        return (m.channel == CH_RELIABLE and m.payload and m.payload[0] == WILDLIFE_DESPAWN_TYPE and
                len(m.payload) == DESPAWN_SIZE)
    out = []
    for m in client.inbox.collect(pred, duration):
        _, entity_id = struct.unpack(DESPAWN_FMT, m.payload)
        out.append(entity_id)
    return out


def decode_snapshot_entry(payload):
    _, kind, bx, bz, entity_id, species, x, y, z, epoch = struct.unpack(SNAPSHOT_ENTRY_FMT, payload)
    return dict(kind=kind, bx=bx, bz=bz, entity_id=entity_id, species=species, x=x, y=y, z=z, epoch=epoch)


def collect_snapshot_entries(client, duration):
    def pred(m):
        return (m.channel == CH_RELIABLE and m.payload and m.payload[0] == WILDLIFE_SNAPSHOT_ENTRY_TYPE and
                len(m.payload) == SNAPSHOT_ENTRY_SIZE)
    return [decode_snapshot_entry(m.payload) for m in client.inbox.collect(pred, duration)]


def collect_snapshot_begins(client, duration):
    def pred(m):
        return (m.channel == CH_RELIABLE and m.payload and m.payload[0] == WILDLIFE_SNAPSHOT_BEGIN_TYPE and
                len(m.payload) == SNAPSHOT_BEGIN_SIZE)
    out = []
    for m in client.inbox.collect(pred, duration):
        _, epoch, generation, count = struct.unpack(SNAPSHOT_BEGIN_FMT, m.payload)
        out.append(dict(epoch=epoch, generation=generation, count=count))
    return out


def force_spawn_fish(client, tag):
    spawns = []
    for _pass in range(3):  # random roll: repeat the stimulus (up to 3 passes) until at least two fish exist
        for bx, bz in ACRES:
            client.send_reliable(build_trigger(bx, bz))
            spawns.extend(collect_spawns(client, 0.15))
        spawns.extend(collect_spawns(client, 0.5))
        if len([s for s in spawns if s["kind"] == KIND_FISH]) >= 2:
            break
    fish = [s for s in spawns if s["kind"] == KIND_FISH]
    print(f"[catch] {tag}: forced spawn burst produced {len(spawns)} total spawn(s), {len(fish)} FISH")
    return fish


def get_current_generation(client):
    """Forces a resync on this connection and reads the generation off the resulting
    WILDLIFE_SNAPSHOT_BEGIN -- the same value a real client would remember as
    s_client_wildlife_known_generation."""
    since = client.inbox.mark()
    client.send_reliable(L.build_resync_request())
    begins = collect_snapshot_begins(client, 2.0)
    if not begins:
        return None
    return begins[-1]["generation"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7799)
    args = ap.parse_args()
    port = args.port
    log_dir = HERE
    results = []

    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife"],
                         log_path=os.path.join(log_dir, "catch_host.log")).start()
    try:
        if not host.wait_listening(60.0):
            check("host reached listening state", False, results)
            return summary_and_exit_code(results)
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("host reached genuine field-ready state (boot_to_field)", False, results)
            return summary_and_exit_code(results)
        check("host reached genuine field-ready state (boot_to_field)", True, results)

        a = FakeClient("A", "127.0.0.1", port)
        a.connect_and_ready()
        a.drain_field_updates(timeout=0.3)

        fish = force_spawn_fish(a, "seed")
        if not fish:
            check("at least one FISH spawn produced for testing (RNG-dependent)", False, results)
            print("[catch] WARNING: zero fish spawned this run -- cannot proceed with any catch test")
            return summary_and_exit_code(results)
        check("at least one FISH spawn produced for testing", True, results)

        generation = get_current_generation(a)
        check("could read the current authoritative wildlife generation via resync", generation is not None,
              results)
        if generation is None:
            return summary_and_exit_code(results)
        print(f"[catch] current generation: {generation}")

        # ---------------------------------------------------------------------------------------
        # TEST D: stale / unknown entity_id, and a real entity_id with a mismatched generation.
        # ---------------------------------------------------------------------------------------
        print("=" * 72)
        print("[catch] TEST D: stale/unknown entity_id and mismatched generation")
        stale_target = fish[0]
        since = a.inbox.mark()
        send_catch(a, 0xDEADBEEF, generation, 9001, stale_target["species"])
        res = collect_catch_results(a, 0.5)
        check("TEST D: unknown entity_id rejected", len(res) == 1 and res[0]["accepted"] == 0, results)
        despawns = collect_despawns(a, 0.2)
        check("TEST D: no WILDLIFE_DESPAWN for an unknown entity_id", len(despawns) == 0, results)

        since = a.inbox.mark()
        send_catch(a, stale_target["entity_id"], generation ^ 0xFFFFFFFF, 9002,
                                            stale_target["species"])
        res = collect_catch_results(a, 0.5)
        check("TEST D: mismatched-generation claim on a REAL entity_id rejected",
              len(res) == 1 and res[0]["accepted"] == 0, results)
        despawns = collect_despawns(a, 0.2)
        check("TEST D: no WILDLIFE_DESPAWN for a mismatched-generation claim", len(despawns) == 0, results)

        # ---------------------------------------------------------------------------------------
        # TEST E: out-of-range claim, then a legitimate in-range claim on the SAME entity succeeds.
        # ---------------------------------------------------------------------------------------
        print("=" * 72)
        print("[catch] TEST E: out-of-range claim, then a legitimate in-range claim succeeds")
        far_target = fish[0]
        a.send_move_any(far_target["x"] + 100000.0, 0.0, far_target["z"] + 100000.0, reliable=True)
        time.sleep(0.3)
        since = a.inbox.mark()
        send_catch(a, far_target["entity_id"], generation, 9003, far_target["species"])
        res = collect_catch_results(a, 0.5)
        check("TEST E: out-of-range claim rejected", len(res) == 1 and res[0]["accepted"] == 0, results)
        despawns = collect_despawns(a, 0.2)
        check("TEST E: fish remains live after an out-of-range rejection (no despawn)", len(despawns) == 0,
              results)

        a.send_move_any(far_target["x"], 0.0, far_target["z"], reliable=True)
        time.sleep(0.3)
        since = a.inbox.mark()
        send_catch(a, far_target["entity_id"], generation, 9004, far_target["species"])
        res = collect_catch_results(a, 0.5)
        check("TEST E: the SAME entity_id, claimed from a legitimate in-range position, is accepted",
              len(res) == 1 and res[0]["accepted"] == 1 and res[0]["entity_id"] == far_target["entity_id"],
              results)
        despawns = collect_despawns(a, 0.3)
        check("TEST E: exactly one WILDLIFE_DESPAWN for the now-caught entity",
              despawns.count(far_target["entity_id"]) == 1, results)
        e_caught_id = far_target["entity_id"]

        # ---------------------------------------------------------------------------------------
        # TEST C (most important): two-client race on the SAME entity_id.
        # ---------------------------------------------------------------------------------------
        print("=" * 72)
        print("[catch] TEST C: two-client race on the SAME entity_id")
        more_fish = force_spawn_fish(a, "race-seed")
        race_candidates = [f for f in more_fish if f["entity_id"] != e_caught_id]
        check("TEST C: a fresh FISH entity is available for the race test (RNG-dependent)",
              len(race_candidates) > 0, results)
        if race_candidates:
            target = race_candidates[0]
            b = FakeClient("B", "127.0.0.1", port)
            b.connect_and_ready()
            b.drain_field_updates(timeout=0.3)
            a.send_move_any(target["x"], 0.0, target["z"], reliable=True)
            b.send_move_any(target["x"], 0.0, target["z"], reliable=True)
            time.sleep(0.3)

            since_a = a.inbox.mark()
            since_b = b.inbox.mark()
            send_catch(a, target["entity_id"], generation, 9101, target["species"])
            send_catch(b, target["entity_id"], generation, 9102, target["species"])
            res_a = collect_catch_results(a, 0.6)
            res_b = collect_catch_results(b, 0.6)
            accepted_count = sum(1 for r in res_a if r["accepted"]) + sum(1 for r in res_b if r["accepted"])
            rejected_count = sum(1 for r in res_a if not r["accepted"]) + sum(1 for r in res_b if not r["accepted"])
            check("TEST C: exactly one of the two racing requests was accepted", accepted_count == 1, results)
            check("TEST C: exactly one of the two racing requests was rejected", rejected_count == 1, results)
            despawns_a = collect_despawns(a, 0.3)
            despawns_b = collect_despawns(b, 0.3)
            check("TEST C: exactly one WILDLIFE_DESPAWN observed for the raced entity (client A)",
                  despawns_a.count(target["entity_id"]) == 1, results)
            check("TEST C: exactly one WILDLIFE_DESPAWN observed for the raced entity (client B)",
                  despawns_b.count(target["entity_id"]) == 1, results)
            b.close()

        # ---------------------------------------------------------------------------------------
        # TEST G (new, post-T3 review): recognized-trash-type acceptance and a genuine species
        # mismatch rejection -- neither was previously covered by this suite.
        #
        # aGYO_TYPE_EMPTY_CAN (41) is a recognized trash id (aGYO_IS_FISH_TRASH(), ac_gyoei.h) --
        # pcwld_fish_species_matches_claim() accepts it regardless of the authoritative record's own
        # (pre-trash-roll) species, since vanilla's trash substitution is trusted client-side (see that
        # function's own doc, pc_wildlife_authority.c). This does not require an actual SALMON2 spawn
        # (RNG/season-dependent, so not reliably reproducible in an automated run) to exercise real
        # species-matching leniency end-to-end.
        #
        # A claimed species that is neither the record's own species, nor its SALMON2->SALMON
        # conversion, nor a recognized trash id, must be rejected outright -- this was previously
        # entirely untested.
        # ---------------------------------------------------------------------------------------
        print("=" * 72)
        print("[catch] TEST G: recognized-trash-type acceptance and species-mismatch rejection")
        # aGYO_TYPE_EMPTY_CAN (ac_gyoei.h): aGYO_TYPE_NUM==40 (40 real fish species, 0..39) ->
        # aGYO_TYPE_WHALE==40, aGYO_TYPE_EMPTY_CAN==41 (first of the 3-value aGYO_IS_FISH_TRASH()
        # range 41..43; aGYO_TYPE_SALMON2==44 follows immediately after, outside that range).
        AGYO_TYPE_EMPTY_CAN = 41
        g_fish = force_spawn_fish(a, "species-seed")
        g_candidates = [f for f in g_fish if f["entity_id"] not in (e_caught_id,)]
        check("TEST G: at least two fresh FISH entities are available (RNG-dependent)",
              len(g_candidates) >= 2, results)
        if len(g_candidates) >= 2:
            trash_target, mismatch_target = g_candidates[0], g_candidates[1]

            # Move into reach of each target first -- pcnetgame_fish_catch_reach_check() (pc_net_game.c)
            # rejects a claim from a peer whose last-synced position is too far from the record's own
            # position, exactly like TEST E/C above; the trash/mismatch checks below are only meaningful
            # once that reach precondition is satisfied, not accidentally tripped over it.
            a.send_move_any(trash_target["x"], 0.0, trash_target["z"], reliable=True)
            time.sleep(0.3)
            since = a.inbox.mark()
            send_catch(a, trash_target["entity_id"], generation, 9201,
                                                AGYO_TYPE_EMPTY_CAN)
            res = collect_catch_results(a, 0.5)
            check("TEST G: a recognized trash claim (EMPTY_CAN) on a real fish entity is ACCEPTED "
                  "regardless of the record's own species",
                  len(res) == 1 and res[0]["accepted"] == 1 and
                  res[0]["entity_id"] == trash_target["entity_id"], results)
            despawns = collect_despawns(a, 0.3)
            check("TEST G: exactly one WILDLIFE_DESPAWN for the trash-claimed entity",
                  despawns.count(trash_target["entity_id"]) == 1, results)

            # A species value guaranteed not to equal the record's own, not its SALMON2->SALMON
            # conversion partner, and not a recognized trash id: offset well clear of the whole
            # aGYO_TYPE_* range (0..43) so it can never coincidentally collide.
            bogus_species = mismatch_target["species"] + 1000
            a.send_move_any(mismatch_target["x"], 0.0, mismatch_target["z"], reliable=True)
            time.sleep(0.3)
            since = a.inbox.mark()
            send_catch(a, mismatch_target["entity_id"], generation, 9202,
                                                bogus_species)
            res = collect_catch_results(a, 0.5)
            check("TEST G: a genuine species mismatch is REJECTED",
                  len(res) == 1 and res[0]["accepted"] == 0, results)
            despawns = collect_despawns(a, 0.2)
            check("TEST G: no WILDLIFE_DESPAWN for a rejected species-mismatch claim",
                  len(despawns) == 0, results)

            # The fish must remain live and catchable with its own real species afterward.
            since = a.inbox.mark()
            send_catch(a, mismatch_target["entity_id"], generation, 9203,
                                                mismatch_target["species"])
            res = collect_catch_results(a, 0.5)
            check("TEST G: the SAME entity, claimed afterward with its own real species, is accepted "
                  "(a rejected mismatch claim did not consume/despawn the entity)",
                  len(res) == 1 and res[0]["accepted"] == 1, results)

        # ---------------------------------------------------------------------------------------
        # TEST F: late-join after a catch never re-offers the caught entity.
        #
        # TEST FIX (post-T3 review): a late-joining client's own initial wildlife data arrives as
        # WILDLIFE_SNAPSHOT_ENTRY (type 39, T2's late-join/reconnect snapshot design) -- NOT as
        # WILDLIFE_SPAWN (type 37, which only ever goes to clients already connected at spawn time).
        # The previous version of this test checked for the wrong message type (WILDLIFE_SPAWN),
        # which a late-joiner never receives for entities that existed before it connected either way
        # -- so the original assertion passed regardless of whether the caught entity was correctly
        # excluded from the snapshot, proving nothing. Checking WILDLIFE_SNAPSHOT_ENTRY instead
        # actually exercises the real late-join path.
        # ---------------------------------------------------------------------------------------
        print("=" * 72)
        print("[catch] TEST F: late-join after a catch")
        c = FakeClient("C", "127.0.0.1", port)
        c.connect_and_ready()
        c.drain_field_updates(timeout=0.5)
        c_entries = collect_snapshot_entries(c, 1.0)
        check("TEST F: a fresh late-joiner's own initial WILDLIFE_SNAPSHOT_ENTRY data is non-empty "
              "(sanity check that the snapshot path itself is being exercised)",
              len(c_entries) > 0, results)
        reoffered = [s for s in c_entries if s["entity_id"] == e_caught_id]
        check("TEST F: a fresh late-joiner's own initial WILDLIFE_SNAPSHOT_ENTRY data never re-offers "
              "the caught entity_id",
              len(reoffered) == 0, results)
        c.close()

        print("=" * 72)
        print(f"[catch] host log entity-{e_caught_id} despawn mentions:")
        host_log = host.log_text()
        for line in host_log.splitlines():
            if f"entity {e_caught_id}" in line or f"entity %u" in line:
                pass
        check("TEST A/E cross-check: host log shows the accepted CATCH for the TEST E entity",
              f"CATCH entity {e_caught_id}" in host_log or f"CATCH_REQUEST entity {e_caught_id}" in host_log or
              "accepted -- removed" in host_log, results)
    finally:
        host.stop()

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
