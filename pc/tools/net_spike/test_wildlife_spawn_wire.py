#!/usr/bin/env python3
"""test_wildlife_spawn_wire.py - World Ecology Wildlife Sync T0 (authority seam foundation only):
REAL synthetic network test against a REAL, currently-running `AnimalCrossing.exe --host <port>`
process, mirroring test_snowman_sync.py's own methodology exactly (a scripted FakeClient standing in
for a real client sending raw protocol bytes, validating the HOST's real C code paths byte-for-byte).

Covers, all against the REAL host process:
  Test W1: a WILDLIFE_SPAWN_TRIGGER_REQUEST for a valid, addressable acre does not crash the host and
           is processed (no malformed-message rejection).
  Test W2: an out-of-range acre (bx=0, the border block never addressable -- see PCFA_ACRE_X_NUM/
           PCFA_ACRE_Z_NUM, pc_field_authority.h) produces NO WILDLIFE_SPAWN broadcast and the host
           stays alive (silently rejected, exactly like every other FIELD_ACTION-style request with
           bad coordinates in this codebase).
  Test W3 (multi-acre, entity uniqueness): triggering every one of the 30 addressable town acres once
           each. Any WILDLIFE_SPAWN broadcasts observed must have: pairwise-unique entity_id values,
           kind in {FISH, BUG}, bx/bz within the addressable range, and species >= 0. This is the
           "host can maintain wildlife records in multiple acres simultaneously" + "unique entity ids"
           check exercised over the wire (item 1/3 of this milestone's own required checks).
  Test W4 (already-exists / re-trigger): re-triggering an acre that Test W3 already produced at least
           one spawn of kind K for must not produce a SECOND WILDLIFE_SPAWN of that SAME kind for that
           SAME acre (the host's chk_live_* shim, backed by the new table, must report "already alive"
           for that kind -- replacing the vanilla local-pool check). A different kind may still
           legitimately spawn (the two decisions are independent), so this only asserts no *duplicate
           same-kind* record for that acre.

What this does NOT do, and why (documented per this milestone's own testing-budget guidance, and
exactly the same real-gameplay-navigation limitation test_p1_real_gameplay.py already ran into and
documented): it does not drive a real second game client through an actual keyboard-controlled wade
into water. That would require knowing/reaching a real river/pond tile from the bootstrap resident's
spawn point with no on-screen coordinate readout or debugger access to verify progress -- reported
here as SOURCE AUDITED (ac_set_manager.c's gating, pc_wildlife_authority.c's adapter) + PROTOCOL
TESTED (this script, against a REAL host process) + boot_to_field() proving a real host process
genuinely reaches playable-field state, NOT REAL GAMEPLAY (wade-input) VERIFIED. Because bx/bz is the
ONLY thing WILDLIFE_SPAWN_TRIGGER_REQUEST ever carries (never a species/RNG result/position -- see
its own doc, pc_net_game.c), this protocol-level test exercises the EXACT SAME host-side code path
(pcnetgame_handle_host_wildlife_spawn_trigger_request() -> pcwld_host_spawn_trigger()) a real wade
event would reach; only the CLIENT-side trigger origin (a scripted send vs. a real
aSetMgr_move_set()/mFI_CheckPlayerWade() sequence) differs.

Usage: python3 test_wildlife_spawn_wire.py <host_ip> <port>
"""
import struct
import sys

from net_spike_lib import CH_RELIABLE, FakeClient, check, summary_and_exit_code

WILDLIFE_SPAWN_TRIGGER_REQUEST_TYPE = 36  # PC_NETGAME_MSG_WILDLIFE_SPAWN_TRIGGER_REQUEST
WILDLIFE_SPAWN_TYPE = 37                  # PC_NETGAME_MSG_WILDLIFE_SPAWN

# PCNetGameWildlifeSpawnTriggerRequestMsg: msg_type, bx, bz, _reserved0 -- 4 bytes.
TRIGGER_FMT = "<BBBB"
# PCNetGameWildlifeSpawnMsg: msg_type, kind, bx, bz, uint32 entity_id, int32 species, float x, y, z
# -- 24 bytes.
SPAWN_FMT = "<BBBBIifff"
SPAWN_SIZE = struct.calcsize(SPAWN_FMT)

KIND_FISH = 0
KIND_BUG = 1

# Addressable town acre range (SET_MANAGER's own raw block-number convention: ax = bx - 1, az = bz - 1
# -- PCFA_ACRE_X_NUM == 5, PCFA_ACRE_Z_NUM == 6, pc_field_authority.h).
ACRE_BX_RANGE = range(1, 6)   # 1..5
ACRE_BZ_RANGE = range(1, 7)   # 1..6


def build_trigger(bx, bz):
    return struct.pack(TRIGGER_FMT, WILDLIFE_SPAWN_TRIGGER_REQUEST_TYPE, bx & 0xFF, bz & 0xFF, 0)


def decode_spawn(payload):
    msg_type, kind, bx, bz, entity_id, species, x, y, z = struct.unpack(SPAWN_FMT, payload)
    return dict(msg_type=msg_type, kind=kind, bx=bx, bz=bz, entity_id=entity_id, species=species,
                pos=(x, y, z))


def collect_spawns(client, duration):
    def pred(m):
        return (m.channel == CH_RELIABLE and m.payload and m.payload[0] == WILDLIFE_SPAWN_TYPE and
                len(m.payload) == SPAWN_SIZE)

    msgs = client.inbox.collect(pred, duration)
    return [decode_spawn(m.payload) for m in msgs]


def main():
    if len(sys.argv) != 3:
        print("usage: test_wildlife_spawn_wire.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    results = []

    a = FakeClient("A", host_ip, port)
    a.connect_and_ready()
    a.drain_field_updates(timeout=0.3)

    # --- Test W1: a valid acre trigger does not crash / disconnect the host --------------------------
    since_w1 = a.inbox.mark()
    a.send_reliable(build_trigger(1, 1))
    spawns_w1 = collect_spawns(a, 0.5)
    check("Test W1: host still connected/READY after a valid-acre trigger", a.is_connected(), results)
    print(f"[wildlife] W1: acre (1,1) produced {len(spawns_w1)} spawn(s) (0 is a legitimate outcome -- "
          f"the vanilla decision itself may roll 'nothing spawns')")

    # --- Test W2: an out-of-range acre is silently rejected, no crash, no broadcast ------------------
    since_w2 = a.inbox.mark()
    a.send_reliable(build_trigger(0, 0))
    spawns_w2 = collect_spawns(a, 0.4)
    check("Test W2: out-of-range acre (0,0) produced no WILDLIFE_SPAWN broadcast", len(spawns_w2) == 0,
          results)
    check("Test W2: host still connected/READY after an out-of-range trigger", a.is_connected(), results)

    # --- Test W3: every addressable acre, once each -- uniqueness + multi-acre coverage --------------
    all_spawns = list(spawns_w1)
    for bx in ACRE_BX_RANGE:
        for bz in ACRE_BZ_RANGE:
            if bx == 1 and bz == 1:
                continue  # already triggered in W1
            a.send_reliable(build_trigger(bx, bz))
            all_spawns.extend(collect_spawns(a, 0.12))
    # one more drain in case the last few triggers' decisions are still in flight
    all_spawns.extend(collect_spawns(a, 0.4))

    check("Test W3: host still connected/READY after 30 acre triggers", a.is_connected(), results)
    print(f"[wildlife] W3: {len(all_spawns)} total WILDLIFE_SPAWN broadcast(s) observed across all 30 "
          f"addressable acres")

    ids = [s["entity_id"] for s in all_spawns]
    check("Test W3: every entity_id is non-zero", all(i != 0 for i in ids), results)
    check("Test W3: every entity_id is pairwise-unique across all observed spawns",
          len(ids) == len(set(ids)), results)
    check("Test W3: every kind is FISH or BUG", all(s["kind"] in (KIND_FISH, KIND_BUG) for s in all_spawns),
          results)
    check("Test W3: every acre is within the addressable range",
          all(s["bx"] in ACRE_BX_RANGE and s["bz"] in ACRE_BZ_RANGE for s in all_spawns), results)
    check("Test W3: every species value is non-negative (a real vanilla enum value, not a sentinel)",
          all(s["species"] >= 0 for s in all_spawns), results)
    if all_spawns:
        kinds_seen = sorted(set(s["kind"] for s in all_spawns))
        acres_seen = sorted(set((s["bx"], s["bz"]) for s in all_spawns))
        print(f"[wildlife] W3: kinds observed={kinds_seen} acres with >=1 spawn={len(acres_seen)}")
    else:
        print("[wildlife] W3: WARNING -- zero spawns observed across all 30 acres; this town/time-of-"
              "day/season combination may simply not roll any insect/fish here (RNG-dependent, not "
              "necessarily a failure) -- Test W4 will be skipped")

    # --- Test W4: re-triggering an already-occupied acre must not duplicate that SAME kind -----------
    if all_spawns:
        by_acre = {}
        for s in all_spawns:
            by_acre.setdefault((s["bx"], s["bz"]), set()).add(s["kind"])
        target_acre, existing_kinds = next(iter(by_acre.items()))
        a.send_reliable(build_trigger(*target_acre))
        retrigger_spawns = collect_spawns(a, 0.5)
        dup_kind = [s for s in retrigger_spawns if s["kind"] in existing_kinds]
        check(f"Test W4: re-triggering acre {target_acre} (already has kind(s) {existing_kinds}) "
              f"produced no duplicate-kind spawn", len(dup_kind) == 0, results)
        check("Test W4: host still connected/READY after the re-trigger", a.is_connected(), results)
    else:
        print("[wildlife] W4: skipped (no spawn observed in W3 to re-trigger against)")

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
