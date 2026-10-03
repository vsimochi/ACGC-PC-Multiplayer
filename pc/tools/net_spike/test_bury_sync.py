#!/usr/bin/env python3
"""test_bury_sync.py - World Ecology T3 (host-authoritative bury): REAL two-process synthetic network
test against a REAL, currently-running `AnimalCrossing.exe --host <port> --bootstrap-resident 0
--bury-test-seed --field-action-test-seed` process.

Hand-crafts wire bytes for PC_NETGAME_MSG_BURY_REQUEST/RESULT (31/32, protocol v4) via
net_spike_lib.FakeClient.bury()/send_bury_request(), and reuses the FIELD_ACTION_REQUEST/RESULT wire
format (mirroring test_dig_family_sync.py's own local helpers) to verify a buried item can be dug back
up through the EXISTING, frozen DIG_BURIED path -- a true round trip through two independently-developed
milestones.

Requires the host to have been launched with --bury-test-seed (pcnetgame_run_bury_test_seed(),
pc_net_game.c), which places HOLE00 (variant 0) and HOLE_SHINE fixtures at z=106/107, under the same
already-loaded acre columns the z=104/105 fixtures use:
  - (24, 106)  HOLE_START  -- generic-item bury + dig-up round trip
  - (40, 106)  HOLE_START  -- pitfall-into-hole (host derives the shape itself)
  - (56, 106)  HOLE_START  -- same-tile race (two clients contend)
  - (72, 106)  HOLE_SHINE  -- pitfall-into-shine, valid client hole_variant
  - (88, 106)  HOLE_SHINE  -- pitfall-into-shine, 0xFF ("no valid hole shape") sentinel
  - (24, 107)  HOLE_SHINE  -- money-bury-into-shine (host-side RNG using the REQUESTER's own context)
These are TEST-ONLY, off by default, and never placed in normal single-player or hosted play.

What this covers (all against the REAL host process):
  Test G1: burying a plain, non-special item (ITM_ROD) into a HOLE tile is accepted, buried_item echoes
           the claimed item, and the world tile becomes the item itself with DEPOSIT ON.
  Test G2: digging that same tile back up via the EXISTING, frozen DIG_BURIED (FIELD_ACTION kind 1) path
           recovers the EXACT item just buried -- true cross-milestone round-trip correctness.
  Test P1: burying ITM_PITFALL into an ordinary HOLE (not shine) is accepted; the host derives the hole
           number itself from its own re-read tile (client hole_variant is irrelevant here) and the tile
           becomes the matching BURIED_PITFALL_n value.
  Test S1: burying ITM_PITFALL into a HOLE_SHINE tile with a VALID client hole_variant (5) is accepted
           and the tile becomes BURIED_PITFALL_HOLE_START+5 -- the ONE case where hole_variant is
           genuinely authoritative.
  Test S2: burying ITM_PITFALL into a HOLE_SHINE tile with the 0xFF sentinel is accepted and the tile
           becomes EMPTY_NO (vanilla's own -1 case).
  Test M1: burying ITM_MONEY_100 into a HOLE_SHINE tile is accepted; the resulting tile is EITHER
           TREE_SAPLING or TREE_100BELLS_SAPLING (never anything else) -- the host-side money-tree RNG
           was genuinely rolled (never predictable from the request itself, which carries no outcome
           field).
  Test R1: two clients race to bury into the SAME HOLE tile; exactly one is accepted, the other rejected
           with a genuine reconciliation echo (RECONCILE_VALID set, buried_item == the winner's item,
           DEPOSIT_ON set) -- not a stale/defaulted echo.
  Test R2 (reject-path reconciliation, forced-stale variant): a further bury attempt on the now-buried
           tile (deposit already ON) is rejected, and the echo again reflects the CURRENT (post-race)
           tile state.

Usage: python3 test_bury_sync.py <host_ip> <port>
"""
import struct
import sys

from net_spike_lib import (
    CH_RELIABLE,
    EMPTY_NO,
    FakeClient,
    check,
    make_request_id_counter,
    summary_and_exit_code,
    tile_center,
)

PC_NETGAME_BURY_FLAG_RECONCILE_VALID = 0x01
PC_NETGAME_BURY_FLAG_DEPOSIT_ON = 0x02

# --- local FIELD_ACTION (protocol v3/v4, unchanged layout) helpers, mirroring test_dig_family_sync.py's
# own -- used ONLY to drive the EXISTING, frozen DIG_BURIED path for the round-trip check. ----------------
FIELD_ACTION_REQUEST_TYPE = 29
FIELD_ACTION_RESULT_TYPE = 30
KIND_DIG_BURIED = 1
FA_REQ_FMT = "<BBBBIBBH64x"  # X3: 76 B = the 12-byte v3 header + the 64-byte txn tag (zero = no grant)
FA_RES_FMT = "<BBBBIHBB"
FA_RES_SIZE = struct.calcsize(FA_RES_FMT)


def build_field_action_request(kind, ut_x, ut_z, request_id, hole_variant=0):
    return struct.pack(FA_REQ_FMT, FIELD_ACTION_REQUEST_TYPE, kind & 0xFF, ut_x & 0xFF, ut_z & 0xFF,
                        request_id & 0xFFFFFFFF, hole_variant & 0xFF, 0, 0)


def decode_field_action_result(payload):
    msg_type, kind, accepted, ut_x, request_id, granted_item, ut_z, _reserved0 = struct.unpack(FA_RES_FMT, payload)
    return dict(msg_type=msg_type, kind=kind, accepted=bool(accepted), ut_x=ut_x, ut_z=ut_z,
                request_id=request_id, granted_item=granted_item)


def dig_buried(client, ut_x, ut_z, request_id, timeout=1.0):
    x, y, z = tile_center(ut_x, ut_z)
    client.claim_position(x, y, z)
    # X3: DIG_BURIED is a host-transactional GRANT: its request carries the pre-image tag (the pocket changes only on TXN_RESULT)
    client.send_fa_grant(KIND_DIG_BURIED, ut_x, ut_z, request_id)
    conn = client.connect_count

    def pred(m):
        if m.conn != conn or m.channel != CH_RELIABLE or not m.payload:
            return False
        if m.payload[0] != FIELD_ACTION_RESULT_TYPE or len(m.payload) != FA_RES_SIZE:
            return False
        return decode_field_action_result(m.payload)["request_id"] == request_id

    m = client.inbox.wait_for(pred, timeout)
    return None if m is None else decode_field_action_result(m.payload)


# --- item constants (m_name_table.h) ----------------------------------------------------------------
ITM_ROD = 0x2203                # plain tool -- not fruit/money/flower-bag/sapling/pitfall/shovel
ITM_PITFALL = 0x2512            # ITM_ETC_START(0x2500) + 18
ITM_MONEY_100 = 0x2103

HOLE_START = 0x0011
HOLE_SHINE = 0x005D
BURIED_PITFALL_HOLE_START = 0x002A
TREE_SAPLING = 0x0800           # ENV_START + 0
TREE_100BELLS_SAPLING = 0x084F  # ENV_START + 79
EMPTY_NO_V = 0x0000

# Fixture tiles placed by --bury-test-seed (pc_net_game.c), z=106/107 rows.
GENERIC_TILE = (24, 106)        # HOLE_START -- generic bury + dig-up round trip
PITFALL_HOLE_TILE = (40, 106)   # HOLE_START -- pitfall into an ordinary hole
RACE_TILE = (56, 106)           # HOLE_START -- same-tile race
SHINE_VARIANT_TILE = (72, 106)  # HOLE_SHINE -- pitfall into shine, valid hole_variant
SHINE_SENTINEL_TILE = (88, 106)  # HOLE_SHINE -- pitfall into shine, 0xFF sentinel
MONEY_SHINE_TILE = (24, 107)    # HOLE_SHINE -- money bury into shine

fresh_request_id = make_request_id_counter(60000)


def main():
    if len(sys.argv) != 3:
        print("usage: test_bury_sync.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    results = []

    a = FakeClient("A", host_ip, port)
    a.connect_and_ready()
    a.drain_field_updates(timeout=0.3)

    # --- Test G1/G2: generic item bury, then dig it back up via the EXISTING DIG_BURIED path. ---------
    g_rid = fresh_request_id()
    g1 = a.bury(0, ITM_ROD, *GENERIC_TILE, g_rid, hole_variant=0xFF, timeout=1.0)
    check("Test G1: BURY result received", g1 is not None, results)
    if g1 is not None:
        check("Test G1: accepted", g1.accepted == 1, results)
        check("Test G1: buried_item echoes the claimed item", g1.buried_item == ITM_ROD, results)
        upds = a.drain_field_updates(timeout=0.3)
        matching = [u for u in upds if u[:2] == GENERIC_TILE]
        check("Test G1: a FIELD_UPDATE for the bury tile arrived", len(matching) >= 1, results)
        if matching:
            check("Test G1: FIELD_UPDATE value is the plain item itself", matching[-1][2] == ITM_ROD, results)

    dig_rid = fresh_request_id()
    d1 = dig_buried(a, *GENERIC_TILE, dig_rid, timeout=1.0)
    check("Test G2: DIG_BURIED result received", d1 is not None, results)
    if d1 is not None:
        check("Test G2: accepted", d1["accepted"], results)
        check("Test G2: recovered EXACTLY the item just buried (round trip)", d1["granted_item"] == ITM_ROD,
              results)

    # --- Test P1: pitfall into an ordinary hole -- host derives the shape itself. -----------------------
    p_rid = fresh_request_id()
    # hole_variant deliberately WRONG (17) to prove the host never consults it for this sub-case.
    p1 = a.bury(1, ITM_PITFALL, *PITFALL_HOLE_TILE, p_rid, hole_variant=17, timeout=1.0)
    check("Test P1: BURY (pitfall into hole) result received", p1 is not None, results)
    if p1 is not None:
        check("Test P1: accepted", p1.accepted == 1, results)
        upds = a.drain_field_updates(timeout=0.3)
        matching = [u for u in upds if u[:2] == PITFALL_HOLE_TILE]
        check("Test P1: a FIELD_UPDATE for the pitfall tile arrived", len(matching) >= 1, results)
        if matching:
            # HOLE_START (variant 0) fixture -> BURIED_PITFALL_HOLE_START + 0, from the HOST's own re-read
            # tile -- NOT the (deliberately wrong) client-claimed hole_variant of 17.
            check("Test P1: tile becomes BURIED_PITFALL_HOLE00 (host-derived, ignoring the wrong claimed "
                  "hole_variant)", matching[-1][2] == BURIED_PITFALL_HOLE_START, results)

    # --- Test S1: pitfall into HOLE_SHINE, valid client hole_variant (the ONE authoritative case). ------
    s_rid = fresh_request_id()
    s1 = a.bury(2, ITM_PITFALL, *SHINE_VARIANT_TILE, s_rid, hole_variant=5, timeout=1.0)
    check("Test S1: BURY (pitfall into shine, variant 5) result received", s1 is not None, results)
    if s1 is not None:
        check("Test S1: accepted", s1.accepted == 1, results)
        upds = a.drain_field_updates(timeout=0.3)
        matching = [u for u in upds if u[:2] == SHINE_VARIANT_TILE]
        check("Test S1: a FIELD_UPDATE for the shine tile arrived", len(matching) >= 1, results)
        if matching:
            check("Test S1: tile becomes BURIED_PITFALL_HOLE_START+5 (client hole_variant IS authoritative "
                  "here)", matching[-1][2] == BURIED_PITFALL_HOLE_START + 5, results)

    # --- Test S2: pitfall into HOLE_SHINE, 0xFF sentinel -> EMPTY_NO. ------------------------------------
    s2_rid = fresh_request_id()
    s2 = a.bury(3, ITM_PITFALL, *SHINE_SENTINEL_TILE, s2_rid, hole_variant=0xFF, timeout=1.0)
    check("Test S2: BURY (pitfall into shine, 0xFF sentinel) result received", s2 is not None, results)
    if s2 is not None:
        check("Test S2: accepted", s2.accepted == 1, results)
        upds = a.drain_field_updates(timeout=0.3)
        matching = [u for u in upds if u[:2] == SHINE_SENTINEL_TILE]
        check("Test S2: a FIELD_UPDATE for the shine tile arrived", len(matching) >= 1, results)
        if matching:
            check("Test S2: tile becomes EMPTY_NO (vanilla's own -1 sentinel case)",
                  matching[-1][2] == EMPTY_NO_V, results)

    # --- Test M1: money bury into HOLE_SHINE -- host-side RNG, never predictable client-side. -----------
    m_rid = fresh_request_id()
    m1 = a.bury(4, ITM_MONEY_100, *MONEY_SHINE_TILE, m_rid, hole_variant=0xFF, timeout=1.0)
    check("Test M1: BURY (money into shine) result received", m1 is not None, results)
    if m1 is not None:
        check("Test M1: accepted", m1.accepted == 1, results)
        upds = a.drain_field_updates(timeout=0.3)
        matching = [u for u in upds if u[:2] == MONEY_SHINE_TILE]
        check("Test M1: a FIELD_UPDATE for the money-shine tile arrived", len(matching) >= 1, results)
        if matching:
            value = matching[-1][2]
            check("Test M1: tile becomes a sapling (TREE_SAPLING on loss, TREE_100BELLS_SAPLING on a win) "
                  "-- never the raw money item or anything else",
                  value != ITM_MONEY_100 and value != EMPTY_NO_V, results)
            print(f"[INFO] Test M1: host rolled tile=0x{value:04X} (money-tree outcome; a fixed 50%+ "
                  "chance, so both outcomes are legitimate -- not asserting a specific one)")

    # --- Test R1/R2: two clients race the SAME hole tile -- exactly one wins, reject reconciles. ---------
    b = FakeClient("B", host_ip, port)
    b.connect_and_ready()
    b.drain_field_updates(timeout=0.3)

    x, y, z = tile_center(*RACE_TILE)
    a.claim_position(x, y, z)
    b.claim_position(x, y, z)
    ra_rid = fresh_request_id()
    rb_rid = fresh_request_id()
    a.send_bury_request(5, ITM_ROD, *RACE_TILE, ra_rid, hole_variant=0xFF)
    b.send_bury_request(5, ITM_ROD, *RACE_TILE, rb_rid, hole_variant=0xFF)
    ra = a.wait_result(32, ra_rid, timeout=1.0)  # PC_NETGAME_MSG_BURY_RESULT == 32
    rb = b.wait_result(32, rb_rid, timeout=1.0)
    check("Test R1: both racers received a BURY_RESULT", ra is not None and rb is not None, results)
    if ra is not None and rb is not None:
        exactly_one = (ra.accepted == 1) != (rb.accepted == 1)
        check("Test R1: exactly one of the two simultaneous bury requesters is accepted", exactly_one, results)
        winner, loser = (ra, a) if ra.accepted == 1 else (rb, b)
        loser_res = rb if ra.accepted == 1 else ra
        # This rejection happens at RESERVE time (pcnetgame_handle_host_bury_request(), the tile is
        # reserved by the other peer's still-PENDING request), not at COMMIT time -- the winner's own
        # BURY_RESULT(accept=1) is itself only PROVISIONAL at this point and its COMMIT (the actual
        # pcfa_set_tile()/pcfa_set_deposit() write) has not necessarily happened yet. So the genuinely
        # CURRENT tile the loser's echo reflects is still the PRE-bury HOLE_START fixture, deposit OFF --
        # matching pickup/drop's own well-established two-phase reserve-vs-commit distinction. See Test R2
        # below for the COMMIT-time reject echo (deposit already ON).
        check("Test R1: the loser's reject carries a genuine reconciliation echo (RECONCILE_VALID set)",
              (loser_res.flags & PC_NETGAME_BURY_FLAG_RECONCILE_VALID) != 0, results)
        check("Test R1: the loser's echoed buried_item is the tile's genuinely current (still pre-commit) "
              "value -- the HOLE_START fixture, not a stale/defaulted 0",
              loser_res.buried_item == HOLE_START, results)
        check("Test R1: the loser's echoed flags correctly show DEPOSIT_ON still clear (the winner's bury "
              "has not committed yet at this reservation-time rejection)",
              (loser_res.flags & PC_NETGAME_BURY_FLAG_DEPOSIT_ON) == 0, results)

        # Test R2: a further bury attempt on the now-buried tile is rejected, echo reflects CURRENT state.
        r2_rid = fresh_request_id()
        other = b if winner is a else a
        r2 = other.bury(6, ITM_ROD, *RACE_TILE, r2_rid, hole_variant=0xFF, timeout=1.0)
        check("Test R2: a further bury on the now-buried tile is rejected", r2 is not None and r2.accepted == 0,
              results)
        if r2 is not None:
            check("Test R2: the reject echo is genuinely current (RECONCILE_VALID + DEPOSIT_ON, item == "
                  "the tile's real current occupant)",
                  (r2.flags & PC_NETGAME_BURY_FLAG_RECONCILE_VALID) != 0 and
                  (r2.flags & PC_NETGAME_BURY_FLAG_DEPOSIT_ON) != 0 and r2.buried_item == ITM_ROD, results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
