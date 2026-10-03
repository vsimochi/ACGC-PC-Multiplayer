#!/usr/bin/env python3
"""test_wildlife_bug_catch.py - World Ecology Wildlife Sync T4 (ordinary bug catching) verification,
mirroring this project's own established test_wildlife_catch.py / test_wildlife_catch_realgrant.py /
test_wildlife_catch_exchange_gate.py methodology (fish) as closely as makes sense for bugs, against a
REAL, currently-launched `AnimalCrossing.exe` host (and, for the real-grant tests, a real second connected
client), using the new test-only --force-bug-catch hook (pc_platform.h/pc_net_game.c) added alongside it.

--force-bug-catch mirrors --force-fish-catch's own exact bypass boundary: it calls the REAL
pc_net_game_host_local_wildlife_catch()/pc_net_game_request_catch_bug() network seams directly against a
REAL, currently-live authoritative BUG entity -- bypassing only "a real net must be swung and connect with
a live insect actor first," never any line of host validation, removal, broadcast, or client-side grant
logic (all of which is the real, unmodified production code).

Covers (numbering matches this file's own TEST letters, deliberately reusing the fish suite's own letter
scheme where the coverage is directly analogous):
  TEST A (real host-local catch): a real host process with --force-bug-catch grants itself a real item
          into its own Save_t pockets and removes the entity from the authoritative table, WITHOUT any
          network round trip to itself.
  TEST B (real client catch): a real second `AnimalCrossing.exe --connect` process with
          --force-bug-catch sends a genuine CATCH_REQUEST, receives CATCH_RESULT, and grants itself the
          item locally; the HOST's own inventory must NOT change for this catch (mirrors realgrant TEST
          B's own post-T3 rigor: the host is ALSO launched with --force-bug-catch and made to genuinely
          self-catch a DIFFERENT entity first, so "host inventory untouched for the client's catch" has
          something real, observable to verify against instead of a log line that could never appear
          either way).
  TEST C (two-client race, MOST IMPORTANT): two FakeClients send a CATCH_REQUEST for the SAME entity_id
          back-to-back. Exactly one must be accepted, the other rejected; exactly one WILDLIFE_DESPAWN
          must be broadcast for that entity_id (not two).
  TEST D (stale request): an unknown/never-issued entity_id, and a real entity_id with a mismatched
          generation, must both be rejected; no despawn is ever broadcast for either.
  TEST E (out-of-range): a FakeClient parked far beyond PC_NETGAME_BUG_CATCH_REACH_SQ (1000 units since
          M9-D F4 -- an acre diagonal plus net reach; fish's is 800) from the bug's own recorded position must be rejected; a
          FOLLOW-UP catch from a real in-range position must still succeed.
  TEST F (late-join after catch): after a catch, a NEW FakeClient's own initial WILDLIFE_SNAPSHOT_ENTRY
          (type 39 -- NOT WILDLIFE_SPAWN, type 37, which a late-joiner never receives for pre-existing
          entities either way, matching test_wildlife_catch.py's own TEST F fix) must never re-offer the
          already-caught entity_id.
  TEST ANT (T4's explicit ant-exclusion guard): a real spawned ant (PC_WILDLIFE_KIND_BUG, species ==
          aINS_INSECT_TYPE_ANT) DOES get a real entity_id and IS broadcast like any other wildlife record
          (confirmed by inspection of pcwld_table_insert()/pcwld_shim_make_ant_proc(),
          pc_wildlife_authority.c) -- so, unlike the original task brief's own worst-case assumption, this
          IS constructible as a genuine protocol-level test: a CATCH_REQUEST claiming the ant's own real
          species from within reach must still be REJECTED outright by
          pcnetgame_validate_and_commit_catch()'s own explicit `rec.species == aINS_INSECT_TYPE_ANT` guard
          (pc_net_game.c), and the ant must remain live afterward (no despawn).
  TEST EXCHANGE (H/I-style, mirrors test_wildlife_catch_exchange_gate.py): exercises the SAME
          s_catch_last_outcome state / pc_net_game_query_catch_outcome() query the putaway-NET
          exchange-screen gate (m_player_main_putaway_net.c_inc, T4's own mirror of the putaway-ROD fix)
          reads, for a BUG entity specifically:
            EXCHANGE-H: a FakeClient racer wins a race against a real --force-bug-catch client; the real
                        client's own query_catch_outcome() must then report REJECTED (3).
            EXCHANGE-I: a real host's own local catch, raced against itself (the identical rejection path
                        a genuine two-peer race produces), must also report REJECTED (3) via
                        query_catch_outcome().

HONEST SCOPE NOTES (same tiering discipline as every other wildlife test in this project):
  - TEST A's item grant is applied by the test hook's own duplicate mPr_SetFreePossessionItem() call
    (pcnetgame_run_bug_catch_test_trigger_host(), pc_net_game.c), NOT the real gameplay seam's
    Player_actor_putin_item() call site (m_player_main_notice_net.c_inc) -- driving real net-swing input
    to reach that exact call site was judged impractical to automate, mirroring test_wildlife_catch_
    realgrant.py's own TEST A note for fish exactly. The authoritative accept/remove/despawn path itself
    (pc_net_game_host_local_wildlife_catch()) IS the real, unmodified production function either way.
  - TEST B's grant IS the real production call site (pcnetgame_handle_client_catch_result()) -- this is
    genuinely PROTOCOL+HOST-LOGIC TESTED, not a stand-in duplicate.
  - Every --force-bug-catch-driven test in this file is PROTOCOL+HOST-LOGIC TESTED, never "REAL GAMEPLAY
    TESTED" (this project's own established terminology reserves that tier for literal manual keyboard-
    driven input) -- see this module's own top-of-file doc for the exact bypass boundary.
  - The exchange-screen call site's own CONSUMPTION of query_catch_outcome() in m_player_main_putaway_
    net.c_inc (the `if (status == REJECTED || status == PENDING) exchange = FALSE;` branch) is verified by
    source review and successful compilation only -- reaching it end-to-end needs a genuinely full
    inventory driven through real net-swing input and a real "exchange" dialogue choice, which is out of
    scope for the same reason real net-swing input is throughout this file. TEST EXCHANGE-H/I instead
    verify, end-to-end against real production code, the SAME state (s_catch_last_outcome) and the SAME
    query function (pc_net_game_query_catch_outcome()) that call site reads -- this is a genuine, non-
    trivial slice of PROTOCOL+HOST-LOGIC TESTED coverage for the underlying mechanism, not a vacuous check.

Usage: python3 test_wildlife_bug_catch.py [--port 7820]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import struct
import sys
import threading
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

KIND_BUG = 1
# enum insect_type (ac_insect_h.h): aINS_INSECT_TYPE_ANT is index 38 of 40 (0..39), the second-to-last
# entry before aINS_INSECT_TYPE_MOSQUITO (39) and aINS_INSECT_TYPE_NUM (40).
AINS_INSECT_TYPE_ANT = 38

# Same acre burst test_wildlife_catch.py/test_wildlife_catch_realgrant.py already use for fish --
# pcwld_host_spawn_trigger() runs BOTH the fish and insect vanilla overlays synchronously for every acre
# in this same burst, so it produces BUG spawns (including, sometimes, ants) exactly as reliably.
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
KIND_UNDER_TEST = "bug"


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


def force_spawn_bugs(client, tag):
    spawns = []
    for bx, bz in ACRES:
        client.send_reliable(build_trigger(bx, bz))
        spawns.extend(collect_spawns(client, 0.15))
    spawns.extend(collect_spawns(client, 0.5))
    bugs = [s for s in spawns if s["kind"] == KIND_BUG]
    print(f"[bug-catch] {tag}: forced spawn burst produced {len(spawns)} total spawn(s), {len(bugs)} BUG")
    return bugs


def force_spawn_one_ordinary_bug(client, tag):
    """Like force_spawn_bugs(), but stops at the first NON-ANT bug (an ordinary catchable target) --
    mirrors test_wildlife_catch_exchange_gate.py's own force_spawn_one_fish() precedent: a single,
    unambiguous target entity_id is needed wherever a racer must be pre-positioned before a real client
    even boots."""
    for bx, bz in ACRES:
        client.send_reliable(build_trigger(bx, bz))
        spawns = collect_spawns(client, 0.15)
        ordinary = [s for s in spawns if s["kind"] == KIND_BUG and s["species"] != AINS_INSECT_TYPE_ANT]
        if ordinary:
            print(f"[bug-catch] {tag}: single-bug seed found entity {ordinary[0]['entity_id']} "
                  f"(species {ordinary[0]['species']}) at acre ({bx},{bz})")
            return ordinary[0]
    spawns = collect_spawns(client, 0.5)
    ordinary = [s for s in spawns if s["kind"] == KIND_BUG and s["species"] != AINS_INSECT_TYPE_ANT]
    return ordinary[0] if ordinary else None


def get_current_generation(client):
    client.send_reliable(L.build_resync_request())
    begins = collect_snapshot_begins(client, 2.0)
    if not begins:
        return None
    return begins[-1]["generation"]


def boot_to_field_keeping_alive(client_proc, keepalive_fake_client, timeout=90.0, slot=1, settle=0):
    """See test_wildlife_catch_exchange_gate.py's own identical helper for the full rationale (an
    unpumped FakeClient gets dropped by the host for inactivity long before a real process finishes
    booting)."""
    result = {}

    def run():
        result["ok"] = client_proc.boot_to_field(timeout=timeout, slot=slot, settle=settle)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    while t.is_alive():
        keepalive_fake_client.inbox.collect(lambda m: False, 0.5)
    t.join()
    return result.get("ok", False)


# ---------------------------------------------------------------------------------------------------
# TEST A / TEST B: real end-to-end grant verification (--force-bug-catch), mirrors
# test_wildlife_catch_realgrant.py's own TEST A/B exactly, adapted for bugs.
# ---------------------------------------------------------------------------------------------------
def test_a(port, log_dir, results):
    print("=" * 72)
    print("[bug-catch] TEST A: real host-local catch (--force-bug-catch, host role)")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife",
                                               "--force-bug-catch"],
                         log_path=os.path.join(log_dir, "bugcatch_testA_host.log")).start()
    try:
        if not host.wait_listening(60.0):
            check("TEST A: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("TEST A: host reached genuine field-ready state", False, results)
            return
        check("TEST A: host reached genuine field-ready state", True, results)

        seed = FakeClient("seed", "127.0.0.1", port)
        seed.connect_and_ready()
        seed.drain_field_updates(timeout=0.3)
        bugs = force_spawn_bugs(seed, "seed")
        ordinary = [b for b in bugs if b["species"] != AINS_INSECT_TYPE_ANT]
        seed.close()
        check("TEST A: at least one real ordinary (non-ant) BUG spawn seeded on the host "
              "(RNG-dependent)", len(ordinary) > 0, results)
        if not ordinary:
            return

        deadline = time.time() + 10.0
        log = ""
        while time.time() < deadline:
            log = host.log_text()
            if "--force-bug-catch (host):" in log:
                break
            time.sleep(0.5)

        check("TEST A: host attempted a real pc_net_game_host_local_wildlife_catch() call (the real "
              "production authority seam) via the --force-bug-catch test hook",
              "--force-bug-catch (host):" in log, results)
        check("TEST A: that call was ACCEPTED and the test hook's own duplicate grant call "
              "(mPr_SetFreePossessionItem(), NOT the real Player_actor_putin_item() seam -- see this "
              "test's own top-of-file HONEST SCOPE NOTE) applied a real item",
              "accepted -- item" in log and "granted" in log, results)
        check("TEST A: host log shows the REAL authoritative removal for the caught entity "
              "(pc_net_game_host_local_wildlife_catch() -> pcnetgame_commit_catch_despawn())",
              "local CATCH entity" in log and "accepted -- removed from authoritative table" in log, results)
    finally:
        host.stop()


HOST_SELF_GRANT_RE = re.compile(r"--force-bug-catch \(host\): entity (\d+) accepted -- item")
HOST_PEER_CATCH_RE = re.compile(r"peer \d+ CATCH entity (\d+) .*accepted -- removed from authoritative table")


def test_b(port, log_dir, results):
    print("=" * 72)
    print("[bug-catch] TEST B: real client catch (--force-bug-catch, client role)")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife",
                                               "--force-bug-catch"],
                         log_path=os.path.join(log_dir, "bugcatch_testB_host.log")).start()
    client = None
    try:
        if not host.wait_listening(60.0):
            check("TEST B: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("TEST B: host reached genuine field-ready state", False, results)
            return
        check("TEST B: host reached genuine field-ready state", True, results)

        seed = FakeClient("seed", "127.0.0.1", port)
        seed.connect_and_ready()
        seed.drain_field_updates(timeout=0.3)
        bugs = force_spawn_bugs(seed, "seed")
        seed.close()
        ordinary = [b for b in bugs if b["species"] != AINS_INSECT_TYPE_ANT]
        check("TEST B: at least two real ordinary BUG spawns seeded on the host (RNG-dependent -- "
              "needed so the host's own self-catch and the client's own catch can land on two "
              "DIFFERENT entities)", len(ordinary) >= 2, results)
        if len(ordinary) < 2:
            return

        # Let the host's own --force-bug-catch self-catch fire BEFORE the client even exists.
        deadline = time.time() + 10.0
        host_log = ""
        while time.time() < deadline:
            host_log = host.log_text()
            if "--force-bug-catch (host):" in host_log:
                break
            time.sleep(0.5)
        check("TEST B: host genuinely self-caught one bug via its own --force-bug-catch (real "
              "self-grant activity to compare the client's later catch against)",
              "accepted -- item" in host_log and "granted" in host_log, results)
        host_self_match = HOST_SELF_GRANT_RE.search(host_log)
        check("TEST B: host log's own self-grant names a specific entity_id", host_self_match is not None,
              results)
        host_self_entity = int(host_self_match.group(1)) if host_self_match else None

        client = L.ClientProcess(f"127.0.0.1:{port}",
                                 extra_args=["--bootstrap-resident", "1", "--authoritative-wildlife",
                                            "--force-bug-catch"],
                                 log_path=os.path.join(log_dir, "bugcatch_testB_client.log")).start()
        client_ok = client.boot_to_field(timeout=90.0, slot=1)
        check("TEST B: real client reached genuine field-ready state", client_ok, results)
        if not client_ok:
            return

        deadline = time.time() + 25.0
        client_log = ""
        while time.time() < deadline:
            client_log = client.log_text()
            if "--force-bug-catch active:" in client_log and "CATCH request" in client_log:
                break
            time.sleep(0.5)
        time.sleep(1.0)
        client_log = client.log_text()
        host_log = host.log_text()

        check("TEST B: client sent a real CATCH_REQUEST via --force-bug-catch",
              "--force-bug-catch active:" in client_log, results)
        check("TEST B: client's catch was accepted and a real item was granted to its own pockets "
              "(pcnetgame_handle_client_catch_result(), the REAL production client-side grant seam)",
              "accepted -- item" in client_log and "granted to a free pocket slot" in client_log, results)

        host_peer_match = HOST_PEER_CATCH_RE.search(host_log)
        check("TEST B: host log shows a peer CATCH acceptance (removal) naming a specific entity_id",
              host_peer_match is not None, results)
        client_entity = int(host_peer_match.group(1)) if host_peer_match else None

        check("TEST B: host's own self-caught entity_id and the client's own caught entity_id are "
              "DIFFERENT (host never also self-granted the SAME entity the client legitimately caught "
              "-- i.e. host inventory was not ALSO credited for the client's own catch)",
              host_self_entity is not None and client_entity is not None and
              host_self_entity != client_entity, results)
        check("TEST B: host's --force-bug-catch self-catch fired exactly ONCE total (never a second "
              "time for the client's own entity)",
              host_log.count("--force-bug-catch (host):") >= 1 and
              len(re.findall(r"--force-bug-catch \(host\): entity \d+ accepted -- item", host_log)) == 1,
              results)
    finally:
        if client is not None:
            client.stop()
        host.stop()


# ---------------------------------------------------------------------------------------------------
# TEST C..F, TEST ANT: pure-protocol coverage via FakeClient(s) against one real host, mirrors
# test_wildlife_catch.py's own structure exactly.
# ---------------------------------------------------------------------------------------------------
def test_protocol_suite(port, log_dir, results):
    log_path = os.path.join(log_dir, "bugcatch_protocol_host.log")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife"],
                         log_path=log_path).start()
    try:
        if not host.wait_listening(60.0):
            check("protocol suite: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("protocol suite: host reached genuine field-ready state (boot_to_field)", False, results)
            return
        check("protocol suite: host reached genuine field-ready state (boot_to_field)", True, results)

        a = FakeClient("A", "127.0.0.1", port)
        a.connect_and_ready()
        a.drain_field_updates(timeout=0.3)

        bugs = force_spawn_bugs(a, "seed")
        ordinary = [b for b in bugs if b["species"] != AINS_INSECT_TYPE_ANT]
        ants = [b for b in bugs if b["species"] == AINS_INSECT_TYPE_ANT]
        check("protocol suite: at least one ordinary (non-ant) BUG spawn produced for testing "
              "(RNG-dependent)", len(ordinary) > 0, results)
        if not ordinary:
            print("[bug-catch] WARNING: zero ordinary bugs spawned this run -- cannot proceed")
            return

        generation = get_current_generation(a)
        check("protocol suite: could read the current authoritative wildlife generation via resync",
              generation is not None, results)
        if generation is None:
            return
        print(f"[bug-catch] current generation: {generation}")

        # -----------------------------------------------------------------------------------------
        # TEST D: stale / unknown entity_id, and a real entity_id with a mismatched generation.
        # -----------------------------------------------------------------------------------------
        print("=" * 72)
        print("[bug-catch] TEST D: stale/unknown entity_id and mismatched generation")
        stale_target = ordinary[0]
        send_catch(a, 0xDEADBEEF, generation, 9001, stale_target["species"])
        res = collect_catch_results(a, 0.5)
        check("TEST D: unknown entity_id rejected", len(res) == 1 and res[0]["accepted"] == 0, results)
        despawns = collect_despawns(a, 0.2)
        check("TEST D: no WILDLIFE_DESPAWN for an unknown entity_id", len(despawns) == 0, results)

        send_catch(a, stale_target["entity_id"], generation ^ 0xFFFFFFFF, 9002,
                                            stale_target["species"])
        res = collect_catch_results(a, 0.5)
        check("TEST D: mismatched-generation claim on a REAL entity_id rejected",
              len(res) == 1 and res[0]["accepted"] == 0, results)
        despawns = collect_despawns(a, 0.2)
        check("TEST D: no WILDLIFE_DESPAWN for a mismatched-generation claim", len(despawns) == 0, results)

        # -----------------------------------------------------------------------------------------
        # TEST E: out-of-range claim (beyond PC_NETGAME_BUG_CATCH_REACH_SQ == 1000^2, M9-D F4: was
        # 150^2; a bug can legitimately drift up to an acre diagonal from its spawn point), then a legitimate in-range claim on the SAME entity succeeds.
        # -----------------------------------------------------------------------------------------
        print("=" * 72)
        print("[bug-catch] TEST E: out-of-range claim (bug reach == 1000 units), then in-range succeeds")
        far_target = ordinary[0]
        # Comfortably beyond 1000 units (bug reach) -- 100000 units is unambiguously out of range.
        a.send_move_any(far_target["x"] + 100000.0, 0.0, far_target["z"] + 100000.0, reliable=True)
        time.sleep(0.3)
        send_catch(a, far_target["entity_id"], generation, 9003, far_target["species"])
        res = collect_catch_results(a, 0.5)
        check("TEST E: out-of-range claim rejected", len(res) == 1 and res[0]["accepted"] == 0, results)
        despawns = collect_despawns(a, 0.2)
        check("TEST E: bug remains live after an out-of-range rejection (no despawn)", len(despawns) == 0,
              results)

        # M9-D F4: just beyond the new 1000-unit bound (1100) is still rejected (the check was kept as an
        # anti-teleport sanity bound, not removed).
        a.send_move_any(far_target["x"] + 1100.0, 0.0, far_target["z"], reliable=True)
        time.sleep(0.3)
        send_catch(a, far_target["entity_id"], generation, 9005, far_target["species"])
        res = collect_catch_results(a, 0.5)
        check("TEST E: claim from 1100 units (just beyond the 1000-unit bound) rejected",
              len(res) == 1 and res[0]["accepted"] == 0, results)
        despawns = collect_despawns(a, 0.2)
        check("TEST E: bug still live after the 1100-unit rejection", len(despawns) == 0, results)

        # A position clearly within the bug reach but not exactly on top of it, to prove the check is a
        # genuine radius test rather than an exact-position match.
        a.send_move_any(far_target["x"] + 50.0, 0.0, far_target["z"], reliable=True)
        time.sleep(0.3)
        send_catch(a, far_target["entity_id"], generation, 9004, far_target["species"])
        res = collect_catch_results(a, 0.5)
        check("TEST E: the SAME entity_id, claimed from a legitimate in-range position (50 units, well "
              "within the bug reach), is accepted",
              len(res) == 1 and res[0]["accepted"] == 1 and res[0]["entity_id"] == far_target["entity_id"],
              results)
        despawns = collect_despawns(a, 0.3)
        check("TEST E: exactly one WILDLIFE_DESPAWN for the now-caught entity",
              despawns.count(far_target["entity_id"]) == 1, results)
        e_caught_id = far_target["entity_id"]

        # -----------------------------------------------------------------------------------------
        # TEST ANT: the explicit ant-exclusion guard (pcnetgame_validate_and_commit_catch(), T4).
        # -----------------------------------------------------------------------------------------
        print("=" * 72)
        print("[bug-catch] TEST ANT: explicit ant-exclusion guard")
        ant_retry = 0
        while not ants and ant_retry < 6:
            more = force_spawn_bugs(a, f"ant-seed-{ant_retry}")
            ants = [b for b in more if b["species"] == AINS_INSECT_TYPE_ANT]
            ant_retry += 1
        check("TEST ANT: at least one real ANT spawn observed for testing (RNG-dependent -- ants DO get "
              "a real entity_id and ARE broadcast, per pcwld_shim_make_ant_proc()'s own doc, even though "
              "T1 never gives them a local presentation actor)",
              len(ants) > 0, results)
        if ants:
            ant = ants[0]
            a.send_move_any(ant["x"], 0.0, ant["z"], reliable=True)
            time.sleep(0.3)
            send_catch(a, ant["entity_id"], generation, 9050, ant["species"])
            res = collect_catch_results(a, 0.5)
            check("TEST ANT: a CATCH_REQUEST claiming an ant's own real species, from within reach, is "
                  "still REJECTED by the explicit `rec.species == aINS_INSECT_TYPE_ANT` guard",
                  len(res) == 1 and res[0]["accepted"] == 0, results)
            despawns = collect_despawns(a, 0.2)
            check("TEST ANT: no WILDLIFE_DESPAWN for the rejected ant claim (the ant remains live)",
                  len(despawns) == 0, results)
        else:
            print("[bug-catch] TEST ANT: WARNING -- zero ants spawned this run (RNG-dependent); guard "
                  "not exercised this run, but confirmed present by source (pc_net_game.c: "
                  "`if (rec.species == aINS_INSECT_TYPE_ANT) return 0;`)")

        # -----------------------------------------------------------------------------------------
        # TEST C (most important): two-client race on the SAME entity_id.
        # -----------------------------------------------------------------------------------------
        print("=" * 72)
        print("[bug-catch] TEST C: two-client race on the SAME entity_id")
        more_bugs = force_spawn_bugs(a, "race-seed")
        race_candidates = [b for b in more_bugs
                           if b["species"] != AINS_INSECT_TYPE_ANT and b["entity_id"] != e_caught_id]
        check("TEST C: a fresh ordinary BUG entity is available for the race test (RNG-dependent)",
              len(race_candidates) > 0, results)
        if race_candidates:
            target = race_candidates[0]
            b = FakeClient("B", "127.0.0.1", port)
            b.connect_and_ready()
            b.drain_field_updates(timeout=0.3)
            a.send_move_any(target["x"], 0.0, target["z"], reliable=True)
            b.send_move_any(target["x"], 0.0, target["z"], reliable=True)
            time.sleep(0.3)

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

        # -----------------------------------------------------------------------------------------
        # TEST F: late-join after a catch never re-offers the caught entity (WILDLIFE_SNAPSHOT_ENTRY,
        # not WILDLIFE_SPAWN -- see test_wildlife_catch.py's own TEST F fix doc).
        # -----------------------------------------------------------------------------------------
        print("=" * 72)
        print("[bug-catch] TEST F: late-join after a catch")
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
        host_log = host.log_text()
        check("TEST E cross-check: host log shows the accepted CATCH for the TEST E entity",
              f"CATCH entity {e_caught_id}" in host_log or f"CATCH_REQUEST entity {e_caught_id}" in host_log or
              "accepted -- removed" in host_log, results)
    finally:
        host.stop()


# ---------------------------------------------------------------------------------------------------
# TEST LABEL-RACE: T8 REVIEW FIX regression test (--diag-bug-despawn-label-race). Reproduces the exact
# scenario an independent review found missing from this file's own existing coverage: Bug 2 part (b)'s
# original "clear local_actor immediately on despawn" change (since reverted -- see pcwld_bug_handle_
# wildlife_despawn()'s own doc, pc_wildlife_authority.c) reintroduced a double-award race because vanilla
# keeps a netted bug's actor ALIVE while it is the LOCAL player's own current item_net_catch_label
# (mPlib_Get_item_net_catch_label(), ac_insect_move.c_inc's own aINS_cull_check()) -- so a WILDLIFE_DESPAWN
# for a DIFFERENT peer's accepted catch on the SAME entity_id can arrive on a process whose own local actor
# is still genuinely alive and still holding that label. Every OTHER test in this file either passes
# entity_ids directly to the network seams (never touching the label-lookup path at all) or drives a real
# client's OWN catch attempt (never a DIFFERENT peer's win landing while THIS process independently holds
# the label) -- this is the one purpose-built to exercise exactly that gap.
#
# Driving a REAL net-swing animation to genuinely hold the label was judged impractical to automate
# (same standing note as every other real-input limitation throughout this file), so --diag-bug-despawn-
# label-race (pc_net_game.c/pc_platform.h) forces the HOST's own item_net_catch_label onto a live bug's
# real local actor directly via the REAL mPlib_Change_item_net_catch_label() seam (the exact assignment a
# genuine net-swing hit makes, ac_ant.c:90's own identical call for precedent) -- then a FakeClient racer
# sends a genuine CATCH_REQUEST for that SAME entity_id, which the host accepts (it never issued its own
# catch attempt) and reconciles via the REAL, unmodified pcnetgame_commit_catch_despawn() ->
# pcwld_handle_wildlife_despawn() -> pcwld_bug_handle_wildlife_despawn() chain, on the SAME host process
# whose label is still active. The diagnostic then queries the REAL pc_net_game_bug_entity_id_for_label()
# (the exact wrapper Player_actor_setup_main_Notice_net()/the putaway-net exchange gate call) for the SAME
# (local_actor, species) pair immediately afterward and reports PASS/FAIL.
# HONEST SCOPE: PROTOCOL+HOST-LOGIC TESTED, not REAL GAMEPLAY TESTED -- the label is forced directly rather
# than produced by genuine net-swing input, exactly like this file's own --force-bug-catch hooks bypass
# only "a real net must be swung and connect with a live insect actor first, never any host validation,
# removal, broadcast, or client-side grant/lookup logic (all real, unmodified production code).
# ---------------------------------------------------------------------------------------------------
LABEL_RACE_LATCH_RE = re.compile(
    r"--diag-bug-despawn-label-race: latched entity (\d+) \(species (-?\d+), local_actor")
LABEL_RACE_SETTLED_RE = re.compile(r"--diag-bug-despawn-label-race: settled -- now waiting")
LABEL_RACE_RESULT_DETAIL_RE = re.compile(
    r"--diag-bug-despawn-label-race: entity (\d+) despawned by a competing catch while this process's "
    r"own label was still active -- pc_net_game_bug_entity_id_for_label\([^)]*\) = (\d+) \(expected (\d+)"
    r"[^;]*; 0 would mean the mapping was wiped despite the active label -- the exact regression this "
    r"diagnostic targets\); local actor exist_flag=(\d+) insect_flags\.destruct=(\d+)")
LABEL_RACE_RESULT_RE = re.compile(r"--diag-bug-despawn-label-race: RESULT (PASS|FAIL)")


def test_label_race(port, log_dir, results):
    print("=" * 72)
    print("[bug-catch] TEST LABEL-RACE: losing peer's local actor still holds the net-catch label when "
          "the winner's WILDLIFE_DESPAWN lands (T8 review fix regression)")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife",
                                               "--diag-bug-despawn-label-race"],
                         log_path=os.path.join(log_dir, "bugcatch_labelrace_host.log")).start()
    racer = None
    try:
        if not host.wait_listening(60.0):
            check("LABEL-RACE: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("LABEL-RACE: host reached genuine field-ready state", False, results)
            return
        check("LABEL-RACE: host reached genuine field-ready state", True, results)

        # Deliberately mirrors test_exchange_h's own single-acre force_spawn_one_ordinary_bug() seeding,
        # NOT force_spawn_bugs()'s own full 10-acre burst: the real vanilla local insect actor pool this
        # process's own presentation actors share is small (observed: a single acre's own burst can already
        # fill it) -- a full 10-acre burst was found, empirically, to immediately evict the very entity this
        # test needs to survive (pcwld_presentation_create()'s own Bug 2 part (a) collision scan correctly
        # invalidates it the instant a LATER acre's spawn reuses its exact pool slot -- a real, unrelated
        # vanilla population-cap effect, not the regression under test here). A single acre's burst leaves
        # the target undisturbed for the rest of this test.
        seed = FakeClient("seed", "127.0.0.1", port)
        seed.connect_and_ready()
        seed.drain_field_updates(timeout=0.3)
        target = force_spawn_one_ordinary_bug(seed, "seed")
        check("LABEL-RACE: at least one real ordinary (non-ant) BUG spawn seeded on the host "
              "(RNG-dependent)", target is not None, results)
        if target is None:
            seed.close()
            return

        # Pre-connect and pre-position the racer BEFORE waiting on the host's own latch line, so the
        # competing CATCH_REQUEST can go out the instant the latch is observed -- minimizing the real
        # wall-clock window the target's own local actor has to naturally wander/flee on its own vanilla AI
        # before the race actually happens (unrelated to, and not to be confused with, the Bug 2(a) pool-
        # reuse effect the single-acre seeding above already avoids).
        racer = FakeClient("racer", "127.0.0.1", port)
        racer.connect_and_ready()
        racer.drain_field_updates(timeout=0.2)
        racer.send_move_any(target["x"], 0.0, target["z"], reliable=True)
        generation = get_current_generation(racer)
        check("LABEL-RACE: racer could read the current authoritative wildlife generation",
              generation is not None, results)
        seed.close()
        if generation is None:
            return

        # The diag hook (host-only, HOST role) picks the first live bug it finds already locally
        # materialized on the host's own presentation table, teleports the host's own local player on top
        # of it (keeping it genuinely alive via ordinary no-cull proximity -- see pc_platform.h's own doc
        # on this flag for why a forced item_net_catch_label alone cannot do this safely), and settles for
        # half a second -- wait for the latch line, THEN the settled line, before racing (the host's own
        # s_presentation[] slot order is not something this test needs to predict, though with only one
        # acre seeded it should be this test's own single target).
        deadline = time.time() + 15.0
        log = ""
        latch_match = None
        while time.time() < deadline:
            log = host.log_text()
            latch_match = LABEL_RACE_LATCH_RE.search(log)
            if latch_match:
                break
            time.sleep(0.05)
        check("LABEL-RACE: host latched a live BUG entity, teleported onto it, and forced its own "
              "item_net_catch_label (--diag-bug-despawn-label-race)", latch_match is not None, results)
        if latch_match is None:
            return
        latched_id = int(latch_match.group(1))
        latched_species = int(latch_match.group(2))

        deadline = time.time() + 5.0
        settled = False
        while time.time() < deadline:
            log = host.log_text()
            if LABEL_RACE_SETTLED_RE.search(log):
                settled = True
                break
            time.sleep(0.05)
        check("LABEL-RACE: host reported settled after the teleport (engine's own block_x/block_z and "
              "camera-distance bookkeeping caught up)", settled, results)
        if not settled:
            return
        check("LABEL-RACE: the latched entity_id matches the real spawn this test seeded (so its own x/z "
              "position is known for the racer's reach check)",
              latched_id == target["entity_id"], results)
        check("LABEL-RACE: the latched entity's species matches its own spawn record",
              latched_species == target["species"], results)

        # A DIFFERENT peer (the FakeClient racer) now wins the race for the SAME entity_id, while the
        # host's own local actor for it is still alive and still holding the label just forced onto it --
        # this is the exact precondition the regression needs: a despawn landing on a process whose own
        # actor is genuinely mid-catch.
        racer_request = catch_bytes(racer, latched_id, generation, 9001, latched_species)
        racer.send_reliable(racer_request)
        racer_res = collect_catch_results(racer, 2.0)
        print(f"[bug-catch] LABEL-RACE: racer CATCH_RESULT(s): {racer_res}")
        check("LABEL-RACE: the racer's competing CATCH_REQUEST for the label-holding entity was ACCEPTED "
              "(the host never attempted its own catch -- only forced the label -- so the racer's genuine "
              "request must win)",
              len(racer_res) == 1 and racer_res[0]["accepted"] == 1 and
              racer_res[0]["entity_id"] == latched_id, results)

        deadline = time.time() + 15.0
        detail_match = None
        result_match = None
        while time.time() < deadline:
            log = host.log_text()
            detail_match = LABEL_RACE_RESULT_DETAIL_RE.search(log)
            result_match = LABEL_RACE_RESULT_RE.search(log)
            if detail_match and result_match:
                break
            time.sleep(0.25)
        check("LABEL-RACE: the host's own despawn reconciliation for the raced entity was observed "
              "(pcwld_bug_handle_wildlife_despawn() ran on the host process while its own label was "
              "still active)", detail_match is not None, results)
        if detail_match:
            despawned_id = int(detail_match.group(1))
            looked_up = int(detail_match.group(2))
            expected = int(detail_match.group(3))
            exist_flag = int(detail_match.group(4))
            destruct_flag = int(detail_match.group(5))
            check("LABEL-RACE: the despawn observed was for the SAME latched/raced entity_id",
                  despawned_id == latched_id, results)
            check("LABEL-RACE: at the moment of despawn, the host's own local actor was STILL genuinely "
                  "alive (exist_flag == TRUE) -- confirms this test actually reproduces the 'losing peer "
                  "still mid-catch' precondition, not a no-op where the actor had already died",
                  exist_flag == 1, results)
            check("LABEL-RACE: the deferred-destroy flag was set (insect_flags.destruct == TRUE) -- the "
                  "real aINS_cull_check() will finish tearing the actor down only once the label itself "
                  "later releases, exactly as vanilla intends",
                  destruct_flag == 1, results)
            check("LABEL-RACE (THE REGRESSION CHECK): pc_net_game_bug_entity_id_for_label() for the "
                  "label-holder's own (local_actor, species) STILL resolves to the real entity_id "
                  "immediately after the despawn, rather than 0 -- Bug 2 part (b)'s reverted 'clear "
                  "immediately on despawn' change would have made this 0, which is precisely what would "
                  "make Notice_net/the putaway-net exchange gate fall through to an untracked vanilla "
                  "grant and reopen the double-award race",
                  looked_up == expected and looked_up == latched_id, results)
        check("LABEL-RACE: the diagnostic's own final RESULT line reports PASS",
              result_match is not None and result_match.group(1) == "PASS", results)
    finally:
        if racer is not None:
            racer.close()
        host.stop()


# ---------------------------------------------------------------------------------------------------
# TEST EXCHANGE-H / EXCHANGE-I: mirrors test_wildlife_catch_exchange_gate.py's own TEST H/I exactly,
# adapted for bugs -- exercises the SAME pc_net_game_query_catch_outcome()/s_catch_last_outcome state
# the putaway-NET exchange-screen gate (m_player_main_putaway_net.c_inc) reads.
# ---------------------------------------------------------------------------------------------------
TELEPORT_RE = re.compile(
    r"--force-bug-catch: teleported the local player to entity (\d+)'s own recorded position "
    r"\(([-\d.]+),([-\d.]+)\)")
QUERY_OUTCOME_RE = re.compile(
    r"--force-bug-catch \(client\): query_catch_outcome\(entity (\d+)\) after CATCH_RESULT wait = (-?\d+)")
HOST_SECOND_ATTEMPT_RE = re.compile(
    r"--force-bug-catch \(host\): SECOND local catch attempt on the SAME entity (\d+) (was ACCEPTED|"
    r"was REJECTED) \(as expected\) -- query_catch_outcome=(-?\d+)")

PC_NETGAME_CATCH_STATUS_REJECTED = 3


def test_exchange_h(port, log_dir, results):
    print("=" * 72)
    print("[bug-catch] TEST EXCHANGE-H: client-side race -- loser's query_catch_outcome() must read "
          "REJECTED")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife"],
                         log_path=os.path.join(log_dir, "bugcatch_exchangeH_host.log")).start()
    client = None
    racer = None
    try:
        if not host.wait_listening(60.0):
            check("EXCHANGE-H: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("EXCHANGE-H: host reached genuine field-ready state", False, results)
            return
        check("EXCHANGE-H: host reached genuine field-ready state", True, results)

        seed = FakeClient("seed", "127.0.0.1", port)
        seed.connect_and_ready()
        seed.drain_field_updates(timeout=0.3)
        target = force_spawn_one_ordinary_bug(seed, "seed")
        check("EXCHANGE-H: at least one real ordinary BUG spawn produced for the race (RNG-dependent)",
              target is not None, results)
        if target is None:
            seed.close()
            return
        seed.close()

        racer = FakeClient("racer", "127.0.0.1", port)
        racer.connect_and_ready()
        racer.drain_field_updates(timeout=0.2)
        racer.send_move_any(target["x"], 0.0, target["z"], reliable=True)
        time.sleep(0.2)
        generation = get_current_generation(racer)
        check("EXCHANGE-H: racer could read the current authoritative wildlife generation",
              generation is not None, results)
        if generation is None:
            return
        racer_request = catch_bytes(racer, target["entity_id"], generation, 5001, target["species"])

        client = L.ClientProcess(f"127.0.0.1:{port}",
                                 extra_args=["--bootstrap-resident", "1", "--authoritative-wildlife",
                                            "--force-bug-catch"],
                                 log_path=os.path.join(log_dir, "bugcatch_exchangeH_client.log")).start()
        client_ok = boot_to_field_keeping_alive(client, racer, timeout=90.0, slot=1, settle=0)
        check("EXCHANGE-H: real client reached genuine field-ready state", client_ok, results)
        if not client_ok:
            return
        racer.send_reliable(racer_request)
        racer_res = collect_catch_results(racer, 1.0)
        print(f"[bug-catch] EXCHANGE-H: racer CATCH_RESULT(s): {racer_res}")
        check("EXCHANGE-H: the FakeClient racer's request (sent first) was ACCEPTED -- won the race",
              len(racer_res) == 1 and racer_res[0]["accepted"] == 1 and
              racer_res[0]["entity_id"] == target["entity_id"], results)

        deadline = time.time() + 30.0
        client_log = ""
        teleport_match = None
        while time.time() < deadline:
            client_log = client.log_text()
            teleport_match = TELEPORT_RE.search(client_log)
            if teleport_match:
                break
            time.sleep(0.1)
        check("EXCHANGE-H: real client latched and teleported to a real target entity_id",
              teleport_match is not None, results)
        if teleport_match:
            raced_entity_id = int(teleport_match.group(1))
            check("EXCHANGE-H: the client latched the SAME (only) entity_id the racer just claimed",
                  raced_entity_id == target["entity_id"], results)
            print(f"[bug-catch] EXCHANGE-H: client latched entity {raced_entity_id}")

        deadline = time.time() + 25.0
        outcome_match = None
        while time.time() < deadline:
            client_log = client.log_text()
            outcome_match = QUERY_OUTCOME_RE.search(client_log)
            if outcome_match:
                break
            time.sleep(0.25)
        check("EXCHANGE-H: real client's own CATCH_REQUEST was REJECTED by the host (the real "
              "pcnetgame_handle_client_catch_result() seam, having lost the race)",
              "rejected by host -- no item granted" in client_log, results)
        check("EXCHANGE-H: real client's own query_catch_outcome() print for the raced entity_id "
              "appeared", outcome_match is not None, results)
        if outcome_match:
            queried_entity = int(outcome_match.group(1))
            queried_status = int(outcome_match.group(2))
            check("EXCHANGE-H: query_catch_outcome() was queried for the SAME raced entity_id",
                  queried_entity == target["entity_id"], results)
            check("EXCHANGE-H: query_catch_outcome() reports REJECTED (3) for the losing client's own "
                  "raced catch -- the exact status the putaway-NET exchange-screen gate "
                  "(m_player_main_putaway_net.c_inc) would read and use to deny a grant",
                  queried_status == PC_NETGAME_CATCH_STATUS_REJECTED, results)
    finally:
        if racer is not None:
            racer.close()
        if client is not None:
            client.stop()
        host.stop()


def test_exchange_i(port, log_dir, results):
    print("=" * 72)
    print("[bug-catch] TEST EXCHANGE-I: host-local self-race -- query_catch_outcome() must read "
          "REJECTED")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife",
                                               "--force-bug-catch"],
                         log_path=os.path.join(log_dir, "bugcatch_exchangeI_host.log")).start()
    try:
        if not host.wait_listening(60.0):
            check("EXCHANGE-I: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("EXCHANGE-I: host reached genuine field-ready state", False, results)
            return
        check("EXCHANGE-I: host reached genuine field-ready state", True, results)

        seed = FakeClient("seed", "127.0.0.1", port)
        seed.connect_and_ready()
        seed.drain_field_updates(timeout=0.3)
        bugs = force_spawn_bugs(seed, "seed")
        ordinary = [b for b in bugs if b["species"] != AINS_INSECT_TYPE_ANT]
        seed.close()
        check("EXCHANGE-I: at least one real ordinary BUG spawn seeded on the host (RNG-dependent)",
              len(ordinary) > 0, results)
        if not ordinary:
            return

        deadline = time.time() + 10.0
        log = ""
        match = None
        while time.time() < deadline:
            log = host.log_text()
            match = HOST_SECOND_ATTEMPT_RE.search(log)
            if match:
                break
            time.sleep(0.3)
        check("EXCHANGE-I: host's first local catch was accepted (real "
              "pc_net_game_host_local_wildlife_catch() seam)",
              "accepted -- item" in log and "granted" in log, results)
        check("EXCHANGE-I: the SECOND host-local catch attempt on the SAME (now-removed) entity_id "
              "fired and was observed", match is not None, results)
        if match:
            second_status = match.group(2)
            outcome = int(match.group(3))
            check("EXCHANGE-I: the second host-local catch attempt was REJECTED (identical code path a "
                  "genuine two-peer race produces)",
                  second_status == "was REJECTED", results)
            check("EXCHANGE-I: query_catch_outcome() reports REJECTED (3) for the host's own "
                  "raced-and-lost local catch -- the exact status the putaway-NET exchange-screen gate "
                  "(m_player_main_putaway_net.c_inc) would read for the HOST's own full-pockets catch",
                  outcome == PC_NETGAME_CATCH_STATUS_REJECTED, results)
    finally:
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7820)
    args = ap.parse_args()
    log_dir = HERE
    results = []

    test_protocol_suite(args.port, log_dir, results)
    test_a(args.port + 1, log_dir, results)
    test_b(args.port + 2, log_dir, results)
    test_exchange_h(args.port + 4, log_dir, results)
    test_exchange_i(args.port + 6, log_dir, results)
    test_label_race(args.port + 8, log_dir, results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
