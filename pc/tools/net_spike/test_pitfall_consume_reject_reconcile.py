#!/usr/bin/env python3
"""test_pitfall_consume_reject_reconcile.py - FOCUSED FAKE-CLIENT regression test closing the Opus T2
review's required PITFALL_CONSUME reject-reconciliation fix (pcnetgame_fa_validate_adapter_pitfall_
consume(), pc_net_game.c): on ANY reject, not only "the tile is no longer a pitfall" (the ONE case
test_dig_family_sync.py's own Test P1 already exercises), the RESULT's granted_item must echo the
host's own ACTUAL CURRENT tile value so the client's optimistic local pitfall-fall animation can
reconcile against reality. Before the fix, every other reject branch (world not ready, ctx not
IN_TOWN, tile reserved, deposit ON, tile-read failure) left granted_item at its hardcoded EMPTY_NO
initializer regardless of what the tile actually held -- a real desync risk (e.g. a still-buried
pitfall, or a buried ITEM under a deposit-ON tile, wrongly reported to the client as EMPTY_NO).

Test T1 (tile reserved -- the one FOCUSED FAKE-CLIENT-reachable scenario): the "reserved" check runs
BEFORE the "is this actually a buried pitfall" check in the validator (see its own source), so any
reserved tile exercises this reject branch regardless of what that tile holds. Client A hits the money
rock (24,104) (reusing the exact same fixture/mechanic test_money_rock_review_gaps.py's own Test R1
does), producing a real ITM_MONEY_100 reward bag on the first drop candidate (23,103). A then opens a
PENDING, unconfirmed PICKUP reservation on that exact tile (never sending CONFIRM), holding the
reservation on itself as review_gaps' own R1 already establishes is equivalent to a second peer's
in-flight pickup. A then sends a PITFALL_CONSUME at that SAME reserved tile: it must be REJECTED (the
tile is reserved, quite apart from not being a pitfall at all), and the RESULT's granted_item must echo
the host's true current tile value (ITM_MONEY_100, the still-undisturbed reward bag) -- NOT the
hard-coded EMPTY_NO a client would have wrongly reconciled against before this fix.

The other four reject branches (world not ready, ctx not IN_TOWN, deposit ON, tile-read failure) are
not independently reachable through a legitimate client action within a single running host process
(world-not-ready/ctx-invalid preconditions can't be forced from a connected, ready FakeClient; a
deposit-ON tile has no dedicated field-action-test-seed fixture; a tile-read failure only occurs for an
already acre/tile-out-of-range coordinate, which itself fails earlier for an entirely different reason)
-- see this task's own completion report for the SOURCE-ONLY trace confirming the fix's helper
(pcnetgame_fa_pitfall_consume_echo_current()) is called from every one of those branches too, not only
the "tile reserved" one this test exercises live.

Requires a freshly-launched host: `AnimalCrossing.exe --host <port> --bootstrap-resident 0
--field-action-test-seed --verbose`.

Usage: python3 test_pitfall_consume_reject_reconcile.py <host_ip> <port>
"""
import struct
import sys

from net_spike_lib import CH_RELIABLE, FakeClient, check, make_request_id_counter, summary_and_exit_code, tile_center

FIELD_ACTION_REQUEST_TYPE = 29  # PC_NETGAME_MSG_FIELD_ACTION_REQUEST
FIELD_ACTION_RESULT_TYPE = 30   # PC_NETGAME_MSG_FIELD_ACTION_RESULT
KIND_MONEY_ROCK_HIT = 2
KIND_PITFALL_CONSUME = 7

# FIELD_ACTION_REQUEST v3 (12 bytes): msg_type, kind, ut_x, ut_z, uint32 request_id, uint8
# hole_variant, uint8 _reserved0, uint16 _reserved1 -- see PCNetGameFieldActionRequestMsg's own doc
# (pc_net_game.c) / test_field_action_wire.py. hole_variant is pinned to 0 for both kinds used here.
REQ_FMT = "<BBBBIBBH"
RES_FMT = "<BBBBIHBB"
RES_SIZE = struct.calcsize(RES_FMT)

ROCK_TILE = (24, 104)
# pcnetgame_find_money_rock_drop_tile()'s own dxs/dzs[0] -- the first (and, on a freshly-launched
# host, unreserved) candidate a money-rock hit lands its reward on.
DROP_TILE = (ROCK_TILE[0] - 1, ROCK_TILE[1] - 1)  # (23,103)
ITM_MONEY_100 = 0x2103

fresh_request_id = make_request_id_counter(61000)


def build_field_action_request(kind, ut_x, ut_z, request_id):
    return struct.pack(REQ_FMT, FIELD_ACTION_REQUEST_TYPE, kind & 0xFF, ut_x & 0xFF, ut_z & 0xFF,
                        request_id & 0xFFFFFFFF, 0, 0, 0)


def decode_field_action_result(payload):
    msg_type, kind, accepted, ut_x, request_id, granted_item, ut_z, _reserved0 = struct.unpack(RES_FMT, payload)
    return dict(msg_type=msg_type, kind=kind, accepted=bool(accepted), ut_x=ut_x, ut_z=ut_z,
                request_id=request_id, granted_item=granted_item)


def wait_field_action_result(client, request_id, timeout=1.0):
    conn = client.connect_count

    def pred(m):
        if m.conn != conn or m.channel != CH_RELIABLE or not m.payload:
            return False
        if m.payload[0] != FIELD_ACTION_RESULT_TYPE or len(m.payload) != RES_SIZE:
            return False
        return decode_field_action_result(m.payload)["request_id"] == request_id

    m = client.inbox.wait_for(pred, timeout)
    return None if m is None else decode_field_action_result(m.payload)


def hit_rock(client, timeout=1.0):
    x, y, z = tile_center(*ROCK_TILE)
    client.claim_position(x, y, z)
    rid = fresh_request_id()
    client.send_reliable(build_field_action_request(KIND_MONEY_ROCK_HIT, ROCK_TILE[0], ROCK_TILE[1], rid))
    return wait_field_action_result(client, rid, timeout=timeout)


def pitfall_consume(client, ut_x, ut_z, timeout=1.0):
    x, y, z = tile_center(ut_x, ut_z)
    client.claim_position(x, y, z)
    rid = fresh_request_id()
    client.send_reliable(build_field_action_request(KIND_PITFALL_CONSUME, ut_x, ut_z, rid))
    return wait_field_action_result(client, rid, timeout=timeout), rid


def main():
    if len(sys.argv) != 3:
        print("usage: test_pitfall_consume_reject_reconcile.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    results = []

    a = FakeClient("A", host_ip, port)
    a.connect_and_ready()
    a.drain_field_updates(timeout=0.3)

    # --- setup: hit the money rock, then open a PENDING (unconfirmed) pickup on the reward tile,
    # exactly like test_money_rock_review_gaps.py's own Test R1 setup -- this holds a real reservation
    # on DROP_TILE without needing a second client. ----------------------------------------------------
    hit = hit_rock(a)
    check("Test T1 setup: MONEY_ROCK_HIT accepted", hit is not None and hit["accepted"], results)

    a.claim_position(*tile_center(*DROP_TILE))
    rid = fresh_request_id()
    a.send_pickup_request(*DROP_TILE, rid, auto_confirm=False)
    pickup_res = a.recv_pickup_result(timeout=1.0, expect_request_id=rid)
    check("Test T1 setup: PICKUP_REQUEST on the reward tile accepted (now PENDING/reserved)",
          pickup_res is not None and pickup_res[0], results)

    if pickup_res is not None and pickup_res[0]:
        # --- Test T1: PITFALL_CONSUME at the SAME (now reserved) tile must be rejected -- the
        # "reserved" check runs before the "is this a buried pitfall" check, so this tile need not
        # actually be a pitfall to exercise the reject-reconciliation bug. -------------------------------
        r, pc_rid = pitfall_consume(a, *DROP_TILE)
        check("Test T1: PITFALL_CONSUME on a reserved tile received an answer", r is not None, results)
        if r is not None:
            check("Test T1: rejected (tile is reserved by A's own pending pickup)", not r["accepted"], results)
            check("Test T1: request_id echoed", r["request_id"] == pc_rid, results)
            check(f"Test T1 (THE FIX): granted_item echoes the host's true CURRENT tile value "
                  f"(ITM_MONEY_100, 0x{ITM_MONEY_100:04X} -- the undisturbed reward bag), "
                  f"not a hard-coded EMPTY_NO; got 0x{r['granted_item']:04X}",
                  r["granted_item"] == ITM_MONEY_100, results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
