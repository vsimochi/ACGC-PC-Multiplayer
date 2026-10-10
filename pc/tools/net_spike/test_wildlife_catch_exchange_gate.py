#!/usr/bin/env python3
"""test_wildlife_catch_exchange_gate.py - World Ecology Wildlife Sync T-catch: RESIDUAL REVIEW FIX
verification for the putaway-rod full-pockets exchange-screen grant gate
(Player_actor_request_proc_index_fromPutaway_rod(), m_player_main_putaway_rod.c_inc) and the new
pc_net_game_query_catch_outcome() API it reads (pc_net_game.h/.c).

BACKGROUND: a prior fix made a full-pockets catch correctly send CATCH_REQUEST to the host instead of
bypassing it. An independent review then found a narrower residual bug: the putaway-rod exchange screen
granted the fish LOCALLY to whatever pocket slot the player picked, regardless of what the host actually
decided about the CATCH_REQUEST -- so a player who LOST a race for a fish (host REJECTED their request,
since a peer's request was processed first) could still open the full-pockets "keep or release" dialogue
and choose "exchange," granting themselves an item the host never actually awarded. The fix adds
pc_net_game_query_catch_outcome(entity_id) (pc_net_game.h/.c) -- a tri-state NONE/PENDING/ACCEPTED/
REJECTED query the exchange-screen call site now gates its grant on -- and populates it from BOTH the
client's own CATCH_RESULT handling and the host's own synchronous local-catch resolution.

HONEST SCOPE NOTE (mirrors this suite's own sibling test_wildlife_catch.py's Bug B honesty precedent
exactly): reaching the ACTUAL exchange-screen UI end-to-end requires driving a real player through
genuine cast-rod/wade/wait-for-a-bite/reel-in input with a GENUINELY FULL inventory, then choosing
"exchange" from a real in-game dialogue -- this project's own established precedent (test_wildlife_catch.
py's Bug B note, test_wildlife_catch_realgrant.py's TEST A/B notes) already documents this exact class of
real-player-input flow as impractical to automate with a simple force-flag. This suite does NOT attempt
it. What it DOES verify, end-to-end, against REAL production code (no mocking):

  TEST H (client-side race): a FakeClient racer and a real --force-fish-catch client both target the
          SAME real entity_id; the racer's CATCH_REQUEST is sent first (winning the race), so the host
          REJECTS the real client's own request via the REAL pcnetgame_handle_client_catch_result()
          seam -- then confirms pc_net_game_query_catch_outcome() for that SAME entity_id reports
          REJECTED (3), the exact status value the putaway-rod exchange-screen gate would have read and
          denied a grant for.
  TEST I (host-local self-race): a real host process (--force-fish-catch) accepts its own local catch
          for a live entity (removing it from the authoritative table), then immediately attempts a
          SECOND pc_net_game_host_local_wildlife_catch() call on the exact SAME entity_id -- the
          identical rejection code path a genuine two-peer race produces, since
          pcnetgame_validate_and_commit_catch() only ever checks "is this entity_id still present," never
          "which peer." Confirms that second call is REJECTED and that pc_net_game_query_catch_outcome()
          then reports REJECTED (3) for it -- the exact status the putaway-rod gate would read for the
          host's own local full-pockets catch when raced and lost.

Together, TEST H and TEST I exercise the SAME state-tracking (s_catch_last_outcome, pc_net_game.c) and
the SAME query function (pc_net_game_query_catch_outcome()) the putaway-rod exchange-screen call site
gates its grant on, end-to-end through real production accept/reject logic -- the exchange screen's own
consumption of that query result (the `if (status == PC_NETGAME_CATCH_STATUS_REJECTED || ...)` branch in
m_player_main_putaway_rod.c_inc) is verified by source review and successful compilation only, per the
scope note above.

Usage: python3 test_wildlife_catch_exchange_gate.py [--port 7810]
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
CATCH_REQUEST_TYPE = 41
CATCH_RESULT_TYPE = 42

TRIGGER_FMT = "<BBBB"
SPAWN_FMT = "<BBBBIifff"
SPAWN_SIZE = struct.calcsize(SPAWN_FMT)
SNAPSHOT_BEGIN_FMT = "<B3xIII"
SNAPSHOT_BEGIN_SIZE = struct.calcsize(SNAPSHOT_BEGIN_FMT)
CATCH_REQUEST_FMT = "<B3xIIIi"
CATCH_REQUEST_SIZE = struct.calcsize(CATCH_REQUEST_FMT)
CATCH_RESULT_FMT = "<BBHII"
CATCH_RESULT_SIZE = struct.calcsize(CATCH_RESULT_FMT)

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


def force_spawn_one_fish(client, tag):
    """Stops at the FIRST real FISH spawn observed, instead of bursting every acre -- TEST H needs a
    SINGLE, unambiguous target entity_id so the racer can be pre-positioned on it (by the fish's own
    already-known spawn position) before the real client even boots, eliminating any timing ambiguity
    about which of several candidate fish the client's own late-join snapshot will latch onto."""
    for _pass in range(3):  # the roll is random (a pass may produce no fish at all): an acre without a fish record rolls again on the next trigger
        for bx, bz in ACRES:
            client.send_reliable(build_trigger(bx, bz))
            spawns = collect_spawns(client, 0.15)
            fish = [s for s in spawns if s["kind"] == KIND_FISH]
            if fish:
                print(f"[exchange-gate] {tag}: single-fish seed found entity {fish[0]['entity_id']} at acre "
                      f"({bx},{bz})")
                return fish[0]
    spawns = collect_spawns(client, 0.5)
    fish = [s for s in spawns if s["kind"] == KIND_FISH]
    return fish[0] if fish else None


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
    print(f"[exchange-gate] {tag}: forced spawn burst produced {len(spawns)} total spawn(s), {len(fish)} FISH")
    return fish


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


def collect_snapshot_begins(client, duration):
    def pred(m):
        return (m.channel == CH_RELIABLE and m.payload and m.payload[0] == WILDLIFE_SNAPSHOT_BEGIN_TYPE and
                len(m.payload) == SNAPSHOT_BEGIN_SIZE)
    out = []
    for m in client.inbox.collect(pred, duration):
        _, epoch, generation, count = struct.unpack(SNAPSHOT_BEGIN_FMT, m.payload)
        out.append(dict(epoch=epoch, generation=generation, count=count))
    return out


def boot_to_field_keeping_alive(client_proc, keepalive_fake_client, timeout=90.0, slot=1, settle=2.5):
    """Runs ClientProcess.boot_to_field() (a long, plain log-polling wait with no Hub involvement at all)
    on a background thread, while this thread keeps pumping keepalive_fake_client's Hub every ~0.5s in
    the meantime -- otherwise a FakeClient left completely unpumped for the tens of seconds a real
    process needs to boot never sends its own automatic heartbeats/ACKs (see FakeClient's own
    auto_heartbeat doc, net_spike_lib.py), and the HOST drops it for inactivity after PCNET_TIMEOUT_S
    (5s), long before TEST H ever gets to use it to race the real client. `settle` is passed straight
    through to boot_to_field() -- TEST H passes 0 to skip its default 2.5s post-readiness settle sleep
    entirely, since that sleep alone was empirically observed to give the real client's own
    --force-fish-catch trigger enough real wall-clock time to reach and pass its own internal move-sync
    wait and send its CATCH_REQUEST before this function even returns, making it impossible for a racer
    to win by sending only after boot_to_field() itself returns. Returns whatever boot_to_field()
    returned."""
    result = {}

    def run():
        result["ok"] = client_proc.boot_to_field(timeout=timeout, slot=slot, settle=settle)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    while t.is_alive():
        keepalive_fake_client.inbox.collect(lambda m: False, 0.5)
    t.join()
    return result.get("ok", False)


def get_current_generation(client):
    client.send_reliable(L.build_resync_request())
    begins = collect_snapshot_begins(client, 2.0)
    if not begins:
        return None
    return begins[-1]["generation"]


TELEPORT_RE = re.compile(
    r"teleported the local player to entity (\d+)'s own recorded position \(([-\d.]+),([-\d.]+)\)")
QUERY_OUTCOME_RE = re.compile(
    r"--force-fish-catch \(client\): query_catch_outcome\(entity (\d+)\) after CATCH_RESULT wait = (-?\d+)")
HOST_SECOND_ATTEMPT_RE = re.compile(
    r"--force-fish-catch \(host\): SECOND local catch attempt on the SAME entity (\d+) (was ACCEPTED|"
    r"was REJECTED) \(as expected\) -- query_catch_outcome=(-?\d+)")

PC_NETGAME_CATCH_STATUS_REJECTED = 3


def test_h(port, log_dir, results):
    """TEST H: real client loses a genuine two-peer race; pc_net_game_query_catch_outcome() must report
    REJECTED for the raced entity_id -- the exact status the putaway-rod exchange-screen gate reads."""
    print("=" * 72)
    print("[exchange-gate] TEST H: client-side race -- loser's query_catch_outcome() must read REJECTED")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife"],
                         log_path=os.path.join(log_dir, "exchange_gate_testH_host.log")).start()
    client = None
    racer = None
    try:
        if not host.wait_listening(60.0):
            check("TEST H: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("TEST H: host reached genuine field-ready state", False, results)
            return
        check("TEST H: host reached genuine field-ready state", True, results)

        seed = FakeClient("seed", "127.0.0.1", port)
        seed.connect_and_ready()
        seed.drain_field_updates(timeout=0.3)
        target = force_spawn_one_fish(seed, "seed")
        check("TEST H: at least one real FISH spawn produced for the race (RNG-dependent)",
              target is not None, results)
        if target is None:
            seed.close()
            return
        seed.close()

        # Pre-position the racer on the fish's own ALREADY-KNOWN spawn position and pre-fetch the
        # generation BEFORE the real client even boots -- so the only thing left to do once the race
        # window opens is a single send_reliable() call, minimizing latency in the critical window
        # against the client's own fixed ~90-frame wait (see pcnetgame_run_fish_catch_test_trigger_
        # client()'s own doc, pc_net_game.c). Since exactly ONE fish was seeded (force_spawn_one_fish()),
        # the client's own late-join snapshot can only ever latch onto THIS SAME entity_id -- no ambiguity
        # to resolve after the fact.
        racer = FakeClient("racer", "127.0.0.1", port)
        racer.connect_and_ready()
        racer.drain_field_updates(timeout=0.2)
        racer.send_move_any(target["x"], 0.0, target["z"], reliable=True)
        time.sleep(0.2)
        generation = get_current_generation(racer)
        check("TEST H: racer could read the current authoritative wildlife generation", generation is not None,
              results)
        if generation is None:
            return
        racer_request = catch_bytes(racer, target["entity_id"], generation, 5001, target["species"])

        # The real client is launched AFTER the fish already exists, so its own late-join snapshot names
        # this SAME entity_id -- --force-fish-catch's own client trigger latches whichever fish it most
        # recently observed (see pcnetgame_run_fish_catch_test_trigger_client()'s own doc).
        client = L.ClientProcess(f"127.0.0.1:{port}",
                                 extra_args=["--bootstrap-resident", "1", "--authoritative-wildlife",
                                            "--force-fish-catch"],
                                 log_path=os.path.join(log_dir, "exchange_gate_testH_client.log")).start()
        # Keep the pre-positioned racer's Hub pumped (heartbeats/ACKs) for the whole, potentially long
        # boot -- see boot_to_field_keeping_alive()'s own doc for why an unpumped FakeClient gets dropped
        # by the host for inactivity well before this completes.
        client_ok = boot_to_field_keeping_alive(client, racer, timeout=90.0, slot=1, settle=0)
        # Fire the racer's PRE-BUILT request THE INSTANT boot_to_field_keeping_alive returns -- do not
        # wait for (or poll for) the client's own "teleported" log line first. Both this racer and the
        # real client's own --force-fish-catch trigger share the exact same precondition (a real local
        # player actor must exist) before either can act on the fish at all, and the client's own trigger
        # then ADDS its own further internal wait on top (s_wait_frames, pcnetgame_run_fish_catch_test_
        # trigger_client(), pc_net_game.c) before it sends anything -- so sending here, as early as
        # topologically possible, needs no calibrated real-time guess about that engine's own frame
        # pacing (which was empirically found to vary enough between runs/environments to make a
        # log-line-polling handoff an unreliable way to win this race deterministically).
        check("TEST H: real client reached genuine field-ready state", client_ok, results)
        if not client_ok:
            return
        racer.send_reliable(racer_request)
        racer_res = collect_catch_results(racer, 1.0)
        print(f"[exchange-gate] TEST H: racer CATCH_RESULT(s): {racer_res}")
        check("TEST H: the FakeClient racer's request (sent first) was ACCEPTED -- won the race",
              len(racer_res) == 1 and racer_res[0]["accepted"] == 1 and
              racer_res[0]["entity_id"] == target["entity_id"], results)

        # Confirm (diagnostic + sanity check, not a timing gate) that the real client did indeed latch
        # the SAME (only) entity_id the racer just claimed.
        deadline = time.time() + 30.0
        client_log = ""
        teleport_match = None
        while time.time() < deadline:
            client_log = client.log_text()
            teleport_match = TELEPORT_RE.search(client_log)
            if teleport_match:
                break
            time.sleep(0.1)
        check("TEST H: real client latched and teleported to a real target entity_id",
              teleport_match is not None, results)
        if teleport_match:
            raced_entity_id = int(teleport_match.group(1))
            check("TEST H: the client latched the SAME (only) entity_id the racer just claimed",
                  raced_entity_id == target["entity_id"], results)
            print(f"[exchange-gate] TEST H: client latched entity {raced_entity_id}")

        # Now let the real client's own --force-fish-catch fire its own (losing) request, and its own
        # stage-3 query_catch_outcome() print resolve. ~4s (240 frames) move-sync wait + ~2s (120 frames)
        # CATCH_RESULT wait, both @ 60fps -- 25s leaves ample margin.
        deadline = time.time() + 25.0
        outcome_match = None
        while time.time() < deadline:
            client_log = client.log_text()
            outcome_match = QUERY_OUTCOME_RE.search(client_log)
            if outcome_match:
                break
            time.sleep(0.25)
        check("TEST H: real client's own CATCH_REQUEST was REJECTED by the host (the real "
              "pcnetgame_handle_client_catch_result() seam, having lost the race)",
              "rejected by host -- no item granted" in client_log, results)
        check("TEST H: real client's own query_catch_outcome() print for the raced entity_id appeared",
              outcome_match is not None, results)
        if outcome_match:
            queried_entity = int(outcome_match.group(1))
            queried_status = int(outcome_match.group(2))
            check("TEST H: query_catch_outcome() was queried for the SAME raced entity_id",
                  queried_entity == raced_entity_id, results)
            check("TEST H: query_catch_outcome() reports REJECTED (3) for the losing client's own "
                  "raced catch -- the exact status the putaway-rod exchange-screen gate "
                  "(m_player_main_putaway_rod.c_inc) would read and use to deny a grant",
                  queried_status == PC_NETGAME_CATCH_STATUS_REJECTED, results)
    finally:
        if racer is not None:
            racer.close()
        if client is not None:
            client.stop()
        host.stop()


def test_i(port, log_dir, results):
    """TEST I: a real host's own synchronous local catch, raced against itself (identical rejection code
    path a genuine two-peer race produces), must report REJECTED via query_catch_outcome()."""
    print("=" * 72)
    print("[exchange-gate] TEST I: host-local self-race -- query_catch_outcome() must read REJECTED")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife",
                                               "--force-fish-catch"],
                         log_path=os.path.join(log_dir, "exchange_gate_testI_host.log")).start()
    try:
        if not host.wait_listening(60.0):
            check("TEST I: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("TEST I: host reached genuine field-ready state", False, results)
            return
        check("TEST I: host reached genuine field-ready state", True, results)

        seed = FakeClient("seed", "127.0.0.1", port)
        seed.connect_and_ready()
        seed.drain_field_updates(timeout=0.3)
        fish = force_spawn_fish(seed, "seed")
        check("TEST I: at least one real FISH spawn seeded on the host (RNG-dependent)", len(fish) > 0,
              results)
        seed.close()
        if not fish:
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
        check("TEST I: host's first local catch was accepted (real pc_net_game_host_local_wildlife_catch() "
              "seam)", "accepted -- item" in log and "granted" in log, results)
        check("TEST I: the SECOND host-local catch attempt on the SAME (now-removed) entity_id fired and "
              "was observed", match is not None, results)
        if match:
            second_status = match.group(2)
            outcome = int(match.group(3))
            check("TEST I: the second host-local catch attempt was REJECTED (identical code path a "
                  "genuine two-peer race produces -- pcnetgame_validate_and_commit_catch() only checks "
                  "entity presence, never which peer)",
                  second_status == "was REJECTED", results)
            check("TEST I: query_catch_outcome() reports REJECTED (3) for the host's own raced-and-lost "
                  "local catch -- the exact status the putaway-rod exchange-screen gate "
                  "(m_player_main_putaway_rod.c_inc) would read for the HOST's own full-pockets catch",
                  outcome == PC_NETGAME_CATCH_STATUS_REJECTED, results)
    finally:
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7810)
    args = ap.parse_args()
    log_dir = HERE
    results = []

    test_h(args.port, log_dir, results)
    test_i(args.port + 1, log_dir, results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
