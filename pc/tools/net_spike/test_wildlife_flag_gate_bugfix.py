#!/usr/bin/env python3
"""test_wildlife_flag_gate_bugfix.py - T1 REVIEW BUGFIX verification (Bug 1, HIGH/REQUIRED):
reproduces the EXACT flag-mismatch scenarios the independent review used to find that a flag-OFF
host/client still processed wildlife spawn traffic, and confirms the fix
(pcnetgame_handle_host_wildlife_spawn_trigger_request()/pcnetgame_handle_client_wildlife_spawn(),
pc_net_game.c) now gates BOTH handlers on g_pc_authoritative_wildlife BEFORE any other processing.

Scenario C (T2 REVIEW BUGFIX, HIGH/REQUIRED -- late-join snapshot path, NOT covered by Scenario B):
Scenario B above only exercises the real-time WILDLIFE_SPAWN broadcast gate
(pcnetgame_handle_client_wildlife_spawn()) -- its flag-OFF client connects BEFORE any spawns exist,
so its own late-join snapshot is always empty (0 entries) and never exercises the three T2 snapshot
handlers (pcnetgame_handle_client_wildlife_snapshot_begin/_entry/_end(), pc_net_game.c) at all. An
independent review reproduced LIVE that those three handlers did not check g_pc_authoritative_wildlife,
so a flag-OFF client joining LATE (after wildlife already exists on the host) still received the
snapshot and materialized real actors via pcwld_presentation_create() -- reintroducing the exact
double-spawn bug Scenario B's own fix was supposed to eliminate, just via the snapshot path instead of
the real-time broadcast path. Scenario C: a REAL host WITH --authoritative-wildlife; a FakeClient
forces >=1 real spawn to exist FIRST (same trigger-burst mechanism Scenario B uses); THEN a REAL
second `AnimalCrossing.exe --connect` client WITHOUT --authoritative-wildlife joins late and receives
its initial (non-empty) snapshot. After the fix: the flag-OFF client's log shows ZERO
"presentation: entity ... materialized locally" lines, and shows the new ignore-gate log lines for
WILDLIFE_SNAPSHOT_BEGIN/_ENTRY (and, if reached, _END) instead.

Scenario A (host-side gate, flag OFF host + a flagged/malicious peer sending the trigger):
  A REAL `AnimalCrossing.exe --host <port>` process WITHOUT --authoritative-wildlife, plus a
  FakeClient (scripted synthetic peer -- the host-side handler cannot tell a scripted peer from a
  real flag-ON client sending the identical bytes; this mirrors test_wildlife_spawn_wire.py's own
  established methodology) sending WILDLIFE_SPAWN_TRIGGER_REQUEST for a burst of acres. Before the
  fix, the host would run the full adapter (real decision + real actor creation + broadcast) anyway.
  After the fix: zero "[NET][WILDLIFE] host: spawn decision" lines, zero WILDLIFE_SPAWN broadcasts
  observed by the FakeClient, and the host log contains the new
  "SPAWN_TRIGGER_REQUEST from peer ... ignored -- authoritative wildlife is disabled on this host"
  line at least once (--verbose).

Scenario B (client-side gate, flag ON host + flag OFF real client, host forced to actually spawn):
  A REAL host WITH --authoritative-wildlife, plus a REAL second `AnimalCrossing.exe --connect`
  client WITHOUT --authoritative-wildlife (both non-interactively bootstrapped via
  --bootstrap-resident, mirroring test_p1_real_gameplay.py's own harness-level approach -- no
  keystrokes), plus a FakeClient used ONLY to force the host to actually produce >=1 real spawn
  decision (there is no non-interactive way to drive a real wade event; this is the same accepted
  gap test_wildlife_spawn_wire.py's own docstring already documents). Before the fix, the real
  flag-OFF client would materialize the host's broadcasted spawn(s) via pcwld_presentation_create()
  on top of its own ordinary vanilla local spawning. After the fix: the real client's OWN log
  contains at least one
  "[NET][WILDLIFE] client: SPAWN entity ... ignored -- authoritative wildlife is disabled on this
  client" line, and NEVER a "presentation: entity ... materialized locally" line, for any entity_id
  the host actually broadcast.

Usage: python test_wildlife_flag_gate_bugfix.py [--port 7788]
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

WILDLIFE_SPAWN_TRIGGER_REQUEST_TYPE = 36
WILDLIFE_SPAWN_TYPE = 37
TRIGGER_FMT = "<BBBB"
SPAWN_FMT = "<BBBBIifff"
SPAWN_SIZE = struct.calcsize(SPAWN_FMT)

# Same 10-acre burst pcnetgame_run_wildlife_trigger_test_trigger() (--force-wildlife-trigger,
# pc_net_game.c) uses, for consistency with that established precedent.
ACRES = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (2, 4), (3, 5), (1, 6), (5, 6), (4, 2)]


def build_trigger(bx, bz):
    return struct.pack(TRIGGER_FMT, WILDLIFE_SPAWN_TRIGGER_REQUEST_TYPE, bx & 0xFF, bz & 0xFF, 0)


def decode_spawn(payload):
    msg_type, kind, bx, bz, entity_id, species, x, y, z = struct.unpack(SPAWN_FMT, payload)
    return dict(kind=kind, bx=bx, bz=bz, entity_id=entity_id, species=species)


def collect_spawns(client, duration):
    def pred(m):
        return (m.channel == CH_RELIABLE and m.payload and m.payload[0] == WILDLIFE_SPAWN_TYPE and
                len(m.payload) == SPAWN_SIZE)
    return [decode_spawn(m.payload) for m in client.inbox.collect(pred, duration)]


def scenario_a(port, log_dir, results):
    print("=" * 72)
    print("[bug1] Scenario A: flag-OFF host + a peer sending WILDLIFE_SPAWN_TRIGGER_REQUEST")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--no-authoritative-wildlife"],
                         log_path=os.path.join(log_dir, "bug1_scenA_host.log")).start()
    try:
        if not host.wait_listening(60.0):
            check("Scenario A: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("Scenario A: host reached genuine field-ready state (boot_to_field)", False, results)
            return
        check("Scenario A: host reached genuine field-ready state (boot_to_field)", True, results)

        a = FakeClient("A", "127.0.0.1", port)
        a.connect_and_ready()
        a.drain_field_updates(timeout=0.3)

        all_spawns = []
        for bx, bz in ACRES:
            a.send_reliable(build_trigger(bx, bz))
            all_spawns.extend(collect_spawns(a, 0.15))
        all_spawns.extend(collect_spawns(a, 0.5))

        check("Scenario A: host still connected/READY after 10 triggers", a.is_connected(), results)
        check("Scenario A: ZERO WILDLIFE_SPAWN broadcasts with the flag off",
              len(all_spawns) == 0, results)

        log = host.log_text()
        check("Scenario A: ZERO '[NET][WILDLIFE] host: spawn decision' lines with the flag off",
              "[NET][WILDLIFE] host: spawn decision" not in log, results)
        check("Scenario A: host log shows the new host-side gate-ignore line",
              "authoritative wildlife is disabled on this host" in log, results)
        n_ignored = log.count("authoritative wildlife is disabled on this host")
        print(f"[bug1] Scenario A: host logged the gate-ignore line {n_ignored} time(s) "
              f"(sent {len(ACRES)} triggers)")
    finally:
        host.stop()


def scenario_b(port, log_dir, results):
    print("=" * 72)
    print("[bug1] Scenario B: flag-ON host + flag-OFF real client, host forced to actually spawn")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife"],
                         log_path=os.path.join(log_dir, "bug1_scenB_host.log")).start()
    client = None
    try:
        if not host.wait_listening(60.0):
            check("Scenario B: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("Scenario B: host reached genuine field-ready state (boot_to_field)", False, results)
            return
        check("Scenario B: host reached genuine field-ready state (boot_to_field)", True, results)

        client = L.ClientProcess(f"127.0.0.1:{port}", extra_args=["--bootstrap-resident", "1"],
                                 log_path=os.path.join(log_dir, "bug1_scenB_client.log")).start()
        client_ok = client.boot_to_field(timeout=90.0, slot=1)
        check("Scenario B: real flag-OFF client reached genuine field-ready state", client_ok, results)
        if not client_ok:
            return

        # Force the host to actually decide + broadcast >=1 spawn (no non-interactive real wade
        # exists -- same accepted gap test_wildlife_spawn_wire.py's own docstring documents). A
        # FakeClient standing in as a third peer is enough: the host-side decision/broadcast code
        # path is identical regardless of who sent the valid trigger message.
        a = FakeClient("A", "127.0.0.1", port)
        a.connect_and_ready()
        a.drain_field_updates(timeout=0.3)
        observed = []
        # every addressable acre, to maximize the chance of at least one real RNG spawn
        for bx in range(1, 6):
            for bz in range(1, 7):
                a.send_reliable(build_trigger(bx, bz))
                observed.extend(collect_spawns(a, 0.1))
        observed.extend(collect_spawns(a, 0.5))
        print(f"[bug1] Scenario B: host produced {len(observed)} WILDLIFE_SPAWN broadcast(s) "
              f"across all 30 acres")
        if not observed:
            print("[bug1] Scenario B: WARNING -- zero spawns this run (RNG-dependent town/season/time "
                  "of day); cannot verify the client-side gate against a real broadcast this run")
            check("Scenario B: host produced at least one real spawn to test the client gate against",
                  False, results)
            return
        check("Scenario B: host produced at least one real spawn to test the client gate against",
              True, results)

        time.sleep(1.5)  # let the real client's poll loop receive/process the broadcast(s)
        client_log = client.log_text()
        check("Scenario B: flag-OFF client log shows the new client-side gate-ignore line",
              "authoritative wildlife is disabled on this client" in client_log, results)
        check("Scenario B: flag-OFF client NEVER materialized a local presentation actor",
              "presentation: entity" not in client_log, results)
        n_ignored = client_log.count("authoritative wildlife is disabled on this client")
        print(f"[bug1] Scenario B: flag-OFF client logged the gate-ignore line {n_ignored} time(s) "
              f"for {len(observed)} broadcast spawn(s)")
    finally:
        if client is not None:
            client.stop()
        host.stop()


def scenario_c(port, log_dir, results):
    print("=" * 72)
    print("[bug1] Scenario C: flag-ON host with EXISTING wildlife + flag-OFF real client LATE-JOINING "
          "(T2 snapshot-path gate -- not covered by Scenario B)")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife"],
                         log_path=os.path.join(log_dir, "bug1_scenC_host.log")).start()
    client = None
    try:
        if not host.wait_listening(60.0):
            check("Scenario C: host reached listening state", False, results)
            return
        if not host.boot_to_field(timeout=90.0, slot=0):
            check("Scenario C: host reached genuine field-ready state (boot_to_field)", False, results)
            return
        check("Scenario C: host reached genuine field-ready state (boot_to_field)", True, results)

        # Force >=1 real spawn to exist on the host FIRST -- BEFORE the flag-OFF client ever connects
        # -- so that client's own initial snapshot is guaranteed non-empty. Same FakeClient
        # trigger-burst mechanism Scenario B uses (no non-interactive real wade exists; same accepted
        # gap test_wildlife_spawn_wire.py's own docstring documents).
        a = FakeClient("A", "127.0.0.1", port)
        a.connect_and_ready()
        a.drain_field_updates(timeout=0.3)
        observed = []
        for bx in range(1, 6):
            for bz in range(1, 7):
                a.send_reliable(build_trigger(bx, bz))
                observed.extend(collect_spawns(a, 0.1))
        observed.extend(collect_spawns(a, 0.5))
        print(f"[bug1] Scenario C: host produced {len(observed)} WILDLIFE_SPAWN broadcast(s) "
              f"across all 30 acres BEFORE the flag-OFF client connects")
        if not observed:
            print("[bug1] Scenario C: WARNING -- zero spawns this run (RNG-dependent town/season/time "
                  "of day); cannot verify the late-join snapshot actually carries any entries this run")
            check("Scenario C: host produced at least one real spawn to exist before the late-join",
                  False, results)
            return
        check("Scenario C: host produced at least one real spawn to exist before the late-join",
              True, results)

        # NOW connect the real flag-OFF client -- late, after wildlife already exists on the host. Its
        # own initial SNAPSHOT_BEGIN/.../SNAPSHOT_END pass is exactly where the T2 snapshot handlers
        # (BEGIN/ENTRY/END) run, non-empty this time.
        client = L.ClientProcess(f"127.0.0.1:{port}", extra_args=["--bootstrap-resident", "1"],
                                 log_path=os.path.join(log_dir, "bug1_scenC_client.log")).start()
        client_ok = client.boot_to_field(timeout=90.0, slot=1)
        check("Scenario C: real flag-OFF client reached genuine field-ready state (late-join)",
              client_ok, results)
        if not client_ok:
            return

        time.sleep(1.5)  # let the real client's poll loop receive/process its initial snapshot
        client_log = client.log_text()

        # NOTE: with the fix in place the flag-OFF client's BEGIN handler returns before ever reaching
        # the "wildlife snapshot begin (generation ..., N entries expected)" printf (that line is part
        # of the known-generation/reset logic the gate is specifically meant to skip entirely -- see
        # pcnetgame_handle_client_wildlife_snapshot_begin()'s own doc) -- so that line is NOT expected
        # to appear here, unlike the pre-fix reviewer repro where it did. Non-emptiness of the snapshot
        # is instead confirmed by counting the per-entity ENTRY ignore-gate lines below: the host sent
        # `len(observed)` real entities, so a matching number of ENTRY ignore-gate lines proves this
        # client's snapshot really carried them all (not just BEGIN with 0 entries).
        n_entry = client_log.count("WILDLIFE_SNAPSHOT_ENTRY entity")
        check("Scenario C: flag-OFF late-joiner's snapshot was genuinely non-empty (>= 1 "
              "WILDLIFE_SNAPSHOT_ENTRY ignore-gate line logged, one per known spawn)",
              n_entry > 0, results)
        check("Scenario C: flag-OFF late-joiner log shows the new WILDLIFE_SNAPSHOT_BEGIN "
              "ignore-gate line",
              "WILDLIFE_SNAPSHOT_BEGIN ignored -- authoritative wildlife is disabled on this client"
              in client_log, results)
        check("Scenario C: flag-OFF late-joiner log shows the new WILDLIFE_SNAPSHOT_ENTRY "
              "ignore-gate line",
              "WILDLIFE_SNAPSHOT_ENTRY entity" in client_log and
              "ignored -- authoritative wildlife is disabled on this client" in client_log, results)
        check("Scenario C: flag-OFF late-joiner NEVER materialized a local presentation actor "
              "(the T2 double-spawn bug the review found)",
              "materialized locally" not in client_log, results)
        n_begin = client_log.count(
            "WILDLIFE_SNAPSHOT_BEGIN ignored -- authoritative wildlife is disabled on this client")
        n_entry = client_log.count(
            "WILDLIFE_SNAPSHOT_ENTRY entity")
        print(f"[bug1] Scenario C: flag-OFF late-joiner logged {n_begin} BEGIN ignore-gate line(s) and "
              f"{n_entry} ENTRY ignore-gate line(s) for a snapshot carrying {len(observed)} known "
              f"spawn(s)")
    finally:
        if client is not None:
            client.stop()
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7788)
    ap.add_argument("--log-dir", default=os.path.join(HERE, "logs"))
    a = ap.parse_args()
    os.makedirs(a.log_dir, exist_ok=True)
    results = []
    scenario_a(a.port, a.log_dir, results)
    # Scenarios B and C exercised a client WITHOUT the flag under a flag-ON host. Since the host now
    # publishes its wildlife mode in HOST_CONFIG and the client follows it (a client can no longer opt out
    # on its own), that combination cannot occur; the client-side ignore-gates are covered by
    # test_wildlife_sim_real.py instead.
    print("[bug1] Scenarios B and C retired: the client now follows the host's HOST_CONFIG wildlife mode")
    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
