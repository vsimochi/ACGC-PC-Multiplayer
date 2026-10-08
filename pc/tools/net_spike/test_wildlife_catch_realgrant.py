#!/usr/bin/env python3
"""test_wildlife_catch_realgrant.py - World Ecology Wildlife Sync T-catch: REAL end-to-end grant
verification for a host's own local catch (TEST A) and a real client's catch (TEST B), using the new
--force-fish-catch test-only hook (pc_platform.h/pc_net_game.c) to bypass fishing's own impractical-to-
automate cast/float/bite/hook input sequence -- see that flag's own doc for the exact, narrow bypass
boundary (every line of host validation/removal/broadcast/grant logic exercised is the REAL, unmodified
code; only "a real UKI actor must be hooked first" is skipped).

TEST A: a real host process (--force-wildlife-trigger to seed a fish, --force-fish-catch to catch it)
        must log a real "item 0x.... granted" line for its own local catch.

        HONEST SCOPE NOTE (post-T3 review correction -- this test's own doc previously overclaimed what
        it verifies): the REAL production seam here is
        pcnetgame_run_fish_catch_test_trigger_host() -> pc_net_game_host_local_wildlife_catch(), the
        exact same authoritative-table validate/remove/despawn-broadcast function
        Player_actor_setup_main_Notice_rod() (m_player_main_notice_rod.c_inc) calls for a real host's
        own local catch -- that half IS genuinely exercised end-to-end. The ITEM GRANT itself, however,
        is applied by pcnetgame_run_fish_catch_test_trigger_host()'s own TEST-ONLY call to
        mPr_SetFreePossessionItem() (pc_net_game.c) -- a duplicate of, not the same call site as, the
        real gameplay seam's Player_actor_putin_item() (m_player_main_notice_rod.c_inc). This test
        therefore verifies the real authoritative accept/remove/despawn path plus a test-hook's own
        grant call, NOT the real Player_actor_putin_item() grant call site itself -- driving real
        cast/float/bite/hook input to reach that exact call site was judged impractical to automate
        (see this module's own top-of-file rationale), so this gap is documented here rather than
        silently overclaimed.
TEST B: a real host (seeds fish) + a real second client (--force-fish-catch) -- the CLIENT must log a
        real "granted to a free pocket slot" line (via the REAL client-side seam,
        pcnetgame_handle_client_catch_result(), pc_net_game.c -- unlike TEST A's host-local grant, this
        IS the actual production grant call site, not a test-hook duplicate).

        TEST FIX (post-T3 review): a previous revision of this test asserted "the HOST must never log
        its own self-grant for this catch" by checking for a host log line that could only ever be
        produced if the host itself were launched with --force-fish-catch -- which it was NOT, making
        that assertion vacuously true regardless of correctness (the log line the check searched for
        could never have appeared either way). This revision launches the HOST with
        --force-fish-catch too, seeds at least two fish, and waits for the host to genuinely self-catch
        one of them (a real, observable self-grant with its own entity_id) BEFORE ever starting the
        client -- then confirms the client's own separately-caught entity_id is DIFFERENT from the
        host's self-caught one, i.e. the host's authoritative table correctly kept the two claims
        distinct rather than the host's own force-catch loop and the client's CATCH_REQUEST ever
        colliding on, or double-granting, the SAME entity.

Usage: python3 test_wildlife_catch_realgrant.py [--port 7801]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
from net_spike_lib import CH_RELIABLE, FakeClient, check, summary_and_exit_code  # noqa: E402

# --force-wildlife-trigger is CLIENT-only (pc_platform.h) -- it has no effect if passed to a HOST
# process. To seed a fish on the HOST for TEST A/B, use a FakeClient to send real
# WILDLIFE_SPAWN_TRIGGER_REQUEST bytes instead, exactly like test_wildlife_spawn_wire.py /
# test_wildlife_catch.py already do -- the host-side handler does not (and structurally cannot) tell a
# scripted peer from a real flagged client sending the identical bytes.
WILDLIFE_SPAWN_TRIGGER_REQUEST_TYPE = 36
WILDLIFE_SPAWN_TYPE = 37
TRIGGER_FMT = "<BBBB"
SPAWN_FMT = "<BBBBIifff"
SPAWN_SIZE = struct.calcsize(SPAWN_FMT)
ACRES = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (2, 4), (3, 5), (1, 6), (5, 6), (4, 2)]


def build_trigger(bx, bz):
    return struct.pack(TRIGGER_FMT, WILDLIFE_SPAWN_TRIGGER_REQUEST_TYPE, bx & 0xFF, bz & 0xFF, 0)


def collect_spawns(client, duration):
    def pred(m):
        return (m.channel == CH_RELIABLE and m.payload and m.payload[0] == WILDLIFE_SPAWN_TYPE and
                len(m.payload) == SPAWN_SIZE)
    out = []
    for m in client.inbox.collect(pred, duration):
        out.append(struct.unpack(SPAWN_FMT, m.payload))
    return out


def seed_fish_via_fake_client(port):
    """Connects a throwaway FakeClient, bursts the acre-trigger list, and returns True if at least one
    real FISH spawn was observed (RNG-dependent, same accepted-gap shape as every other wildlife test in
    this suite)."""
    fish = seed_fish_via_fake_client_list(port)
    return len(fish) > 0


def seed_fish_via_fake_client_list(port):
    """Same as seed_fish_via_fake_client(), but returns the actual list of FISH spawn tuples (SPAWN_FMT-
    unpacked) instead of a bool -- used by TEST B (below) to confirm at least two distinct real fish
    entities exist before letting the host and client each independently claim one."""
    fc = FakeClient("seed", "127.0.0.1", port)
    fc.connect_and_ready()
    fc.drain_field_updates(timeout=0.3)
    spawns = []
    for bx, bz in ACRES:
        fc.send_reliable(build_trigger(bx, bz))
        spawns.extend(collect_spawns(fc, 0.15))
    spawns.extend(collect_spawns(fc, 0.5))
    fish = [s for s in spawns if s[1] == 0]  # kind field, index 1; KIND_FISH == 0
    fc.close()
    return fish


def test_a(port, log_dir, results):
    print("=" * 72)
    print("[realgrant] TEST A: real host-local catch (--force-fish-catch, host role)")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife",
                                               "--force-fish-catch"],
                         log_path=os.path.join(log_dir, "realgrant_testA_host.log")).start()
    try:
        if not host.wait_listening(60.0):
            check("TEST A: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("TEST A: host reached genuine field-ready state", False, results)
            return
        check("TEST A: host reached genuine field-ready state", True, results)

        if not seed_fish_via_fake_client(port):
            check("TEST A: at least one real FISH spawn seeded on the host (RNG-dependent)", False, results)
            return
        check("TEST A: at least one real FISH spawn seeded on the host", True, results)

        # --force-fish-catch polls every frame once a live FISH record exists -- give it a window.
        deadline = time.time() + 10.0
        log = ""
        while time.time() < deadline:
            log = host.log_text()
            if "--force-fish-catch (host):" in log:
                break
            time.sleep(0.5)

        check("TEST A: host attempted a real pc_net_game_host_local_wildlife_catch() call (the real "
              "production authority seam) via the --force-fish-catch test hook",
              "--force-fish-catch (host):" in log, results)
        check("TEST A: that call was ACCEPTED and the test hook's own duplicate grant call "
              "(mPr_SetFreePossessionItem(), NOT the real Player_actor_putin_item() seam -- see this "
              "test's own top-of-file HONEST SCOPE NOTE) applied a real item",
              "accepted -- item" in log and "granted" in log, results)
        check("TEST A: host log shows the REAL authoritative removal for the caught entity "
              "(pc_net_game_host_local_wildlife_catch() -> pcnetgame_commit_catch_despawn())",
              "local CATCH entity" in log and "accepted -- removed from authoritative table" in log, results)
    finally:
        host.stop()


HOST_SELF_GRANT_RE = re.compile(r"--force-fish-catch \(host\): entity (\d+) accepted -- item")
HOST_PEER_CATCH_RE = re.compile(r"peer \d+ CATCH entity (\d+) .*accepted -- removed from authoritative table")


def test_b(port, log_dir, results):
    print("=" * 72)
    print("[realgrant] TEST B: real client catch (--force-fish-catch, client role)")
    # TEST FIX (post-T3 review): the host is now ALSO launched with --force-fish-catch, so the "host
    # inventory untouched" check below has something real to verify against instead of checking for a
    # log line that could never appear either way (see this module's own top-of-file TEST FIX doc for
    # the full rationale). At least two distinct fish are seeded up front so the host's own force-catch
    # loop (which fires once, for the first live FISH record it finds in its authoritative table) and
    # the client's own force-catch (which targets whatever entity its own late-join snapshot/spawn
    # bookkeeping most recently saw) have two genuinely different entities to land on.
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife",
                                               "--force-fish-catch"],
                         log_path=os.path.join(log_dir, "realgrant_testB_host.log")).start()
    client = None
    try:
        if not host.wait_listening(60.0):
            check("TEST B: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("TEST B: host reached genuine field-ready state", False, results)
            return
        check("TEST B: host reached genuine field-ready state", True, results)

        fish = seed_fish_via_fake_client_list(port)
        check("TEST B: at least two real FISH spawns seeded on the host (RNG-dependent -- needed so the "
              "host's own self-catch and the client's own catch can land on two DIFFERENT entities)",
              len(fish) >= 2, results)
        if len(fish) < 2:
            return

        # Let the host's own --force-fish-catch self-catch fire BEFORE the client even exists -- it
        # polls every frame once world-ready/town-scene/a live FISH record all hold, and fires exactly
        # once, so this always resolves against a single, real, host-local entity.
        deadline = time.time() + 10.0
        host_log = ""
        while time.time() < deadline:
            host_log = host.log_text()
            if "--force-fish-catch (host):" in host_log:
                break
            time.sleep(0.5)
        check("TEST B: host genuinely self-caught one fish via its own --force-fish-catch (real "
              "self-grant activity to compare the client's later catch against)",
              "accepted -- item" in host_log and "granted" in host_log, results)
        host_self_match = HOST_SELF_GRANT_RE.search(host_log)
        check("TEST B: host log's own self-grant names a specific entity_id", host_self_match is not None,
              results)
        host_self_entity = int(host_self_match.group(1)) if host_self_match else None

        client = L.ClientProcess(f"127.0.0.1:{port}",
                                 extra_args=["--bootstrap-resident", "1", "--authoritative-wildlife",
                                            "--force-fish-catch"],
                                 log_path=os.path.join(log_dir, "realgrant_testB_client.log")).start()
        client_ok = client.boot_to_field(timeout=90.0, slot=1)
        check("TEST B: real client reached genuine field-ready state", client_ok, results)
        if not client_ok:
            return

        deadline = time.time() + 25.0
        client_log = ""
        while time.time() < deadline:
            client_log = client.log_text()
            if "--force-fish-catch active:" in client_log and "CATCH request" in client_log:
                break
            time.sleep(0.5)
        time.sleep(1.0)
        client_log = client.log_text()
        host_log = host.log_text()

        check("TEST B: client sent a real CATCH_REQUEST via --force-fish-catch",
              "--force-fish-catch active:" in client_log, results)
        check("TEST B: client's catch was accepted and a real item was granted to its own pockets "
              "(pcnetgame_handle_client_catch_result(), the REAL production client-side grant seam)",
              "accepted -- item" in client_log and ("granted from the host transaction" in client_log or "granted to a free pocket slot" in client_log), results)

        host_peer_match = HOST_PEER_CATCH_RE.search(host_log)
        check("TEST B: host log shows a peer CATCH acceptance (removal) naming a specific entity_id",
              host_peer_match is not None, results)
        client_entity = int(host_peer_match.group(1)) if host_peer_match else None

        # The meaningful, previously-vacuous check: the host's OWN self-caught entity (genuinely live
        # activity, confirmed above) must be a DIFFERENT entity from the one the client legitimately
        # caught via CATCH_REQUEST -- i.e. the host never also granted itself the SAME entity a peer's
        # catch already claimed, and the host's own --force-fish-catch self-catch fired exactly once
        # (not a second time for the client's entity). NOTE: this test never fills either side's
        # pockets, so it does NOT exercise the full-pockets exchange-screen double-award scenario (the
        # putaway-rod exchange-screen grant gate, m_player_main_putaway_rod.c_inc) -- it only confirms
        # ordinary two-peer entity-level exclusivity at the network/protocol level.
        check("TEST B: host's own self-caught entity_id and the client's own caught entity_id are "
              "DIFFERENT (host never also self-granted the SAME entity the client legitimately caught)",
              host_self_entity is not None and client_entity is not None and
              host_self_entity != client_entity, results)
        check("TEST B: host's --force-fish-catch self-catch fired exactly ONCE total (never a second "
              "time for the client's own entity)",
              host_log.count("--force-fish-catch (host):") >= 1 and
              len(re.findall(r"--force-fish-catch \(host\): entity \d+ accepted -- item", host_log)) == 1,
              results)
    finally:
        if client is not None:
            client.stop()
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7801)
    args = ap.parse_args()
    log_dir = HERE
    results = []

    test_a(args.port, log_dir, results)
    test_b(args.port + 1, log_dir, results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
