#!/usr/bin/env python3
"""test_money_rock_late_join.py - FOCUSED FAKE-CLIENT check closing the Part 4 "persistence" test
matrix item for the World Ecology Stage 1 money-rock fixes: is a money-rock-dropped bag genuinely
visible to a LATE-JOINING client via the existing field snapshot mechanism, with no special-case
plumbing of its own? Per Bug 4's own design doc (pc_net_game.c), a money-rock-dropped bag is "an
entirely ordinary field item" (mFI_SetFG_common()/pcfa_set_tile() writes only), so late-join coverage
should be automatic (inherited from the ordinary FIELD_UPDATE/snapshot machinery) -- this confirms
that rather than assuming it.

Test L1: client A hits the money rock, producing a dropped bag on a known tile. Client B then
connects FOR THE FIRST TIME (a genuine late join) and its own initial snapshot must already show that
tile holding the reward item -- read from B's own locally-applied field copy (post-snapshot), not via
any protocol message specific to money rocks.

Usage: python3 test_money_rock_late_join.py <host_ip> <port>
"""
import struct
import sys

from net_spike_lib import CH_RELIABLE, FakeClient, check, make_request_id_counter, summary_and_exit_code, tile_center

FIELD_ACTION_REQUEST_TYPE = 29
FIELD_ACTION_RESULT_TYPE = 30
KIND_MONEY_ROCK_HIT = 2
#   FIELD_ACTION_REQUEST v3 (12 bytes): msg_type, kind, ut_x, ut_z, uint32 request_id, uint8
#   hole_variant, uint8 _reserved0, uint16 _reserved1 -- see PCNetGameFieldActionRequestMsg's own doc
#   (pc_net_game.c) / test_field_action_wire.py. hole_variant is pinned to 0 for MONEY_ROCK_HIT.
REQ_FMT = "<BBBBIBBH"
RES_FMT = "<BBBBIHBB"
RES_SIZE = struct.calcsize(RES_FMT)

ROCK_TILE = (24, 104)
DROP_TILE = (ROCK_TILE[0] - 1, ROCK_TILE[1] - 1)  # dxs/dzs[0] -- see pcnetgame_find_money_rock_drop_tile()
ITM_MONEY_100 = 0x2103

fresh_request_id = make_request_id_counter(11000)


def wait_field_action_result(client, request_id, timeout=1.0):
    conn = client.connect_count

    def pred(m):
        if m.conn != conn or m.channel != CH_RELIABLE or not m.payload:
            return False
        if m.payload[0] != FIELD_ACTION_RESULT_TYPE or len(m.payload) != RES_SIZE:
            return False
        msg_type, kind, accepted, ut_x, rid, granted_item, ut_z, _r = struct.unpack(RES_FMT, m.payload)
        return rid == request_id

    m = client.inbox.wait_for(pred, timeout)
    if m is None:
        return None
    msg_type, kind, accepted, ut_x, rid, granted_item, ut_z, _r = struct.unpack(RES_FMT, m.payload)
    return dict(accepted=bool(accepted), ut_x=ut_x, ut_z=ut_z, request_id=rid, granted_item=granted_item)


def main():
    if len(sys.argv) != 3:
        print("usage: test_money_rock_late_join.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    results = []

    a = FakeClient("A", host_ip, port)
    a.connect_and_ready()
    a.drain_field_updates(timeout=0.3)

    x, y, z = tile_center(*ROCK_TILE)
    a.claim_position(x, y, z)
    rid = fresh_request_id()
    a.send_reliable(struct.pack(REQ_FMT, FIELD_ACTION_REQUEST_TYPE, KIND_MONEY_ROCK_HIT & 0xFF,
                                 ROCK_TILE[0] & 0xFF, ROCK_TILE[1] & 0xFF, rid & 0xFFFFFFFF, 0, 0, 0))
    hit_res = wait_field_action_result(a, rid, timeout=1.0)
    check("Test L1 setup: MONEY_ROCK_HIT accepted", hit_res is not None and hit_res["accepted"], results)

    # --- Test L1: a genuinely NEW client connects AFTER the hit -- its own initial snapshot must
    # already show the dropped bag, with no money-rock-specific protocol involved. Confirmed the same
    # way test_field_action_sync.py confirms any drop-tile's contents: an ordinary PICKUP_REQUEST,
    # which the host answers strictly from its OWN authoritative field (never from B's snapshot copy --
    # see pcnetgame_validate_and_resolve_pickup()'s own doc), so an accepted pickup with the right
    # granted_item is real evidence the SNAPSHOT delivered to B (and the host's own field) genuinely
    # carried the bag through, not just that A's own hit succeeded. ------------------------------------
    b = FakeClient("B", host_ip, port)
    b.connect_and_ready()
    b.drain_field_updates(timeout=0.3)
    bx, by, bz = tile_center(*DROP_TILE)
    b.claim_position(bx, by, bz)
    prid = fresh_request_id()
    b.send_pickup_request(DROP_TILE[0], DROP_TILE[1], prid)
    pres = b.recv_pickup_result(timeout=1.0, expect_request_id=prid)
    check("Test L1: late-joining client B can pick up the pre-existing bag at the drop tile "
          f"(result={pres!r})", pres is not None and pres[0] and pres[4] == ITM_MONEY_100, results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
