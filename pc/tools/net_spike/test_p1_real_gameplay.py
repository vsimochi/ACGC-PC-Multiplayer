#!/usr/bin/env python3
"""test_p1_real_gameplay.py - P1 (World Ecology T-dig) real-gameplay verification harness.

Scope: proves that TWO REAL `AnimalCrossing.exe` processes (a host and a second, independently
bootstrapped client -- not a synthetic FakeClient sending raw wire messages) can reach genuine
playable-field state non-interactively via `--bootstrap-resident N`, with the
`--field-action-test-seed` fixtures for DIG_HOLE / FILL_HOLE / PITFALL_CONSUME / DIG_SHINE seeded
and visible to both. This is the harness-level fix for the false-positive boot_to_town() bug
(see net_spike_lib.HostProcess.boot_to_field()'s docstring): it uses NO game_input keystrokes for
boot/menu navigation, and its readiness check is anchored strictly AFTER the bootstrap's own
"resident bound" log line, not a whole-log search.

What this script does NOT do, and why: drive a real character via keyboard input across the town to
each fixture tile and press the dig/shovel button. That was attempted manually (see the P1
verification report) and found impractical within a reasonable budget in this environment:
  - No debugger (gdb/cdb/windbg) or symbol/PDB access was available to read live player position or
    breakpoint the real dig-scoop gameplay function, so there is no ground truth for in-field
    navigation.
  - The bootstrap resident's spawn tile (home door, ~ut (53,37) for slot 0, from pc_m_card.c's
    homeX/homeZ tables / mFI_UNIT_BASE_SIZE=40) is roughly 65-70 tiles away from the
    --field-action-test-seed fixture row (ut z=104/105), across unknown intervening terrain, with no
    on-screen coordinate readout to correct dead-reckoning drift.
  - The D-pad quick-item-select scancodes (I/J/K/L per keybindings.ini) produced no observable change
    in the player's held item across repeated presses in a manual probe, so equipping the shovel
    non-interactively could not be confirmed either.
Per the mission brief's own guidance for exactly this situation (see its DIG_SHINE budget note,
generalized here to all four actions): rather than build large new position-telemetry or
navigation-automation infrastructure, this is reported honestly as SOURCE VERIFIED (code inspected)
+ PROTOCOL VERIFIED (via the existing test_dig_family_sync.py / test_field_action_sync.py /
test_pitfall_consume_reject_reconcile.py suite, already run and passing against these same fixtures
in earlier sessions -- not re-run here per the P1 verification task's own testing-budget rule of not
re-running existing protocol suites), NOT REAL GAMEPLAY VERIFIED.

What this script DOES prove (and is the actual regression check it performs): that the harness fix
itself is real and reaches the field, end to end, for both a host and a second real client process,
which is the precondition every one of the four P1 actions needs before any gameplay-level test
(scripted or manual) can even begin.

Usage: python test_p1_real_gameplay.py [--port 7788]
Exit code: 0 if both processes reach genuine field-ready state with the fixtures seeded, 1 otherwise.
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

# Fixture tiles seeded by --field-action-test-seed (pc_net_game.c,
# pcnetgame_run_field_action_test_seed()), one per P1 action under test:
FIXTURES = {
    "DIG_HOLE": (56, 105),         # EMPTY_NO
    "FILL_HOLE": (72, 105),        # HOLE_START (deposit OFF)
    "PITFALL_CONSUME": (40, 105),  # BURIED_PITFALL_HOLE00 (deposit OFF, consume-race fixture)
    "DIG_SHINE": (88, 105),        # SHINE_SPOT
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7788)
    ap.add_argument("--log-dir", default=os.path.join(HERE, "logs"))
    ap.add_argument("--boot-timeout", type=float, default=90.0)
    a = ap.parse_args()
    os.makedirs(a.log_dir, exist_ok=True)

    ok = True
    host = L.HostProcess(port=a.port, extra_args=["--bootstrap-resident", "0", "--field-action-test-seed"],
                         log_path=os.path.join(a.log_dir, "p1_real_gameplay_host.log")).start()
    client = None
    try:
        print("[p1] waiting for host to reach genuine field-ready state (boot_to_field, no keystrokes)...")
        if not host.wait_listening(60.0):
            print("[p1] FAIL: host never reported listening on the UDP port")
            return 1
        host_ok = host.boot_to_field(timeout=a.boot_timeout, slot=0)
        print(f"[p1] host boot_to_field: {'OK' if host_ok else 'FAIL'}")
        ok = ok and host_ok

        if not host.wait_seeded(20, 30.0):
            print(f"[p1] WARNING: only {len(host.seeded_tiles())} generic seed tile(s) placed "
                  "(field-action fixtures are checked separately below; continuing)")

        client = L.ClientProcess(f"127.0.0.1:{a.port}",
                                 extra_args=["--bootstrap-resident", "1"],
                                 log_path=os.path.join(a.log_dir, "p1_real_gameplay_client.log")).start()
        print("[p1] waiting for client to reach genuine field-ready state (boot_to_field, no keystrokes)...")
        client_ok = client.boot_to_field(timeout=a.boot_timeout, slot=1)
        print(f"[p1] client boot_to_field: {'OK' if client_ok else 'FAIL'}")
        ok = ok and client_ok

        host_text = host.log_text()
        print("[p1] checking the 4 P1 dig-family fixtures were seeded by the REAL host process:")
        for name, (x, z) in FIXTURES.items():
            seeded = f"tile ({x},{z})" in host_text
            print(f"    {name:<16} tile ({x:3d},{z:3d}) seeded={'YES' if seeded else 'NO'}")
            ok = ok and seeded

        print(f"[p1] host still alive: {host.alive()}   client still alive: {client.alive() if client else 'n/a'}")
        ok = ok and host.alive() and (client.alive() if client else False)
    finally:
        if client is not None:
            client.stop()
        host.stop()

    print("=" * 72)
    print("RESULT:", "PASS (two real processes reached genuine field-ready state; fixtures present)"
          if ok else "FAIL")
    print("NOTE: this proves the harness fix (boot_to_field) and fixture seeding only. See this file's")
    print("module docstring for why in-field navigation to actually trigger a dig/fill/pitfall/shine via")
    print("real character input was not attempted programmatically in this environment.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
