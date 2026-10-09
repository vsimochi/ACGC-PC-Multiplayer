#!/usr/bin/env python3
"""test_world_snapshot.py - WORLD SNAPSHOT tests (foundation contract section 4, protocol v2)
against a REAL host that is in gameplay and was launched with --pickup-test-seed.

v2 wire (pc_net_game.c): after IDENTITY_ACK (host seq 0) and the APPEARANCE roster, the host sends
SNAPSHOT_BEGIN{epoch, world_seq}, 30 x FIELD_BLOCK{acre, flags IN_SNAPSHOT, epoch, world_seq built at
send time, items[256], deposit[16], valid[16]}, SNAPSHOT_END{epoch, acre_count, world_seq,
renew_time}. Client rules mirrored by net_spike_lib.WorldView: block applies iff world_seq >=
applied_seq[acre] (valid=0 tiles untouched), FIELD_UPDATE iff world_seq > applied_seq[acre], an
in-snapshot block whose epoch is not the latest BEGIN's is dropped.

Ground truth: the host is authoritative -- a pickup the host ACCEPTS proves the tile held the item
the snapshot said; a fresh client's completed snapshot is the host's current truth.

  S1 fresh client: ACK seq 0 -> roster -> BEGIN -> 30 blocks (acres 0..29, once each, same epoch,
     BEGIN.seq <= block.seq <= END.seq) -> END(acre_count 30); no transient value in any valid tile;
     anchor: a tile the snapshot shows holding an apple is granted as an apple by a real pickup, and
     a tile the snapshot shows EMPTY is rejected.
  S2 late join after host mutations: A empties seeded tile Z and drops a cherry on it; B joins: B's
     snapshot shows the cherry, and B's block seq for that acre >= the delta seq A received.
  S3 reconnect: B leaves, A empties seeded tile U, B rejoins (same slot asserted): a complete NEW
     snapshot (new epoch, 30 blocks from acre 0) shows U empty.
  S4 snapshot then delta: after B's snapshot, A empties tile V: B gets a FIELD_UPDATE for V with
     world_seq > B's snapshot block seq for V's acre, and B's view shows V empty.
  S5 delta during snapshot: C connects with its first FIELD_BLOCK dropped (snapshot stalls until
     the host retransmits, >= 150 ms); meanwhile A empties a tile in the stalled acre 0 and in a
     later acre. C's final view must equal a fresh client D's snapshot (truth) on every tile.
  S6 ordering: across every client, FIELD_UPDATE world_seq strictly increases, no world message
     precedes IDENTITY_ACK, no violations.
  S7 mismatched town: REJECT(LAND_MISMATCH, 24 B, host town), no world data, disconnect; then
     probe-then-connect succeeds.
  S8 RESYNC_REQUEST: a READY client asking for a resync gets a complete snapshot with a NEW epoch.
  S9 superseded snapshot: RESYNC sent while the initial snapshot is still streaming -> the client
     ends with a completed snapshot of the latest epoch; blocks of the old epoch after the new BEGIN
     are ignored (never applied).

Usage: python test_world_snapshot.py <host_ip> <port>
"""
import sys

import net_spike_lib as L

ITM_FOOD_APPLE = 0x2800
ITM_FOOD_CHERRY = 0x2801
fresh_rid = L.make_request_id_counter(9500)
ALL = []


def client(label, **kw):
    c = L.FakeClient(label, HOST, PORT, **kw)
    ALL.append(c)
    return c


def live_tiles(view):
    return [t for t in L.TOWN_FIXTURE_TILES if view.tile_ut(*t) == ITM_FOOD_APPLE]


def check_snapshot(c, r, name, snap=None):
    snap = snap or (c.completed_snapshots()[-1] if c.completed_snapshots() else None)
    if snap is None:
        L.check(f"{name} {c.label}: snapshot completed", False, r)
        return None
    begin, end = snap["begin"], snap["end"]
    seqs = snap["block_seqs"]
    L.check(f"{name} {c.label}: 30 blocks, acres 0..29 once each, in order (epoch {snap['epoch']})",
            snap["blocks"] == list(range(L.ACRE_NUM)) and begin["acre_count"] == L.ACRE_NUM, r)
    L.check(f"{name} {c.label}: END epoch/acre_count match (acre_count {end['acre_count']})",
            end["epoch"] == snap["epoch"] and end["acre_count"] == L.ACRE_NUM, r)
    L.check(f"{name} {c.label}: BEGIN.seq <= every block seq <= END.seq",
            all(L.seq_diff(s, begin["world_seq"]) >= 0 and L.seq_diff(end["world_seq"], s) >= 0 for s in seqs.values()), r)
    L.check(f"{name} {c.label}: no world violations {c.world.violations[:3]}", not c.world.violations, r)
    L.check(f"{name} {c.label}: no AMBIGUOUS value (RSV_NO/RSV_SIGNBOARD) in any host-supplied tile while the host "
            f"stands in town (saw {c.world.ambiguous_seen[:3]})", not c.world.ambiguous_seen, r)
    return snap


def main():
    global HOST, PORT
    hp = L.parse_host_port(sys.argv, "usage: test_world_snapshot.py <host_ip> <port>")
    if hp is None:
        return 1
    HOST, PORT = hp
    r = []

    # --- S1 -------------------------------------------------------------------------------------------------
    a = client("S1-A")
    a.connect_and_ready()
    L.check("S1 IDENTITY_ACK is host reliable seq 0", a.identity_ack_seq == 0, r)
    snap = check_snapshot(a, r, "S1")
    rel = [m for m in a.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE)]
    first_world = min((i for i, _k, _o in a.world_log), default=None)
    appearance_idx = [m.index for m in rel if m.msg_type == L.PC_NETGAME_MSG_APPEARANCE]
    L.check("S1 roster APPEARANCE precedes SNAPSHOT_BEGIN",
            bool(appearance_idx) and snap is not None and min(appearance_idx) < snap["begin_index"], r)
    live = live_tiles(a.world)
    L.info(f"S1 snapshot shows {len(live)} apple fixture tiles: {live}")
    if not live:
        print("SKIP - S1 anchor/S2-S5: no apple fixture tile in the snapshot (launch host with --pickup-test-seed)")
        for c in ALL:
            c.close()
        return L.summary_and_exit_code(r, ["host not seeded"])
    t = live.pop(0)
    res = a.pickup(t[0], t[1], fresh_rid())
    L.check(f"S1 anchor: tile {t} the snapshot shows as apple is granted as an apple by the host",
            res is not None and res.accepted and res.granted_item == ITM_FOOD_APPLE, r)
    empty = next((u for u in L.TOWN_FIXTURE_TILES if a.world.tile_ut(*u) == L.EMPTY_NO and u != t), None)
    if empty is not None:
        res = a.pickup(empty[0], empty[1], fresh_rid())
        L.check(f"S1 anchor: tile {empty} the snapshot shows EMPTY is rejected", res is not None and not res.accepted, r)

    # --- S2 -------------------------------------------------------------------------------------------------
    z = live.pop(0)
    mark = a.inbox.mark()
    res = a.pickup(z[0], z[1], fresh_rid())
    L.check(f"S2 A emptied seeded tile {z}", res is not None and res.accepted, r)
    res = a.drop(0, ITM_FOOD_CHERRY, z[0], z[1], fresh_rid(), claim_at=z)
    L.check(f"S2 A dropped a cherry on {z}", res is not None and res.accepted, r)
    L.pump_sleep(0.3)
    zacre, ztile = L.town_ut_to_acre_tile(*z)
    a_deltas = [g for g in a.field_updates_since(mark) if g.acre == zacre and g.tile == ztile]
    b = client("S2-B")
    b.connect_and_ready()
    check_snapshot(b, r, "S2")
    L.check(f"S2 late joiner's snapshot shows the cherry on {z}", b.world.tile_ut(*z) == ITM_FOOD_CHERRY, r)
    L.check("S2 late joiner's block seq for that acre >= the last delta seq A saw for it",
            bool(a_deltas) and L.seq_diff(b.snapshots[-1]["block_seqs"][zacre], a_deltas[-1].world_seq) >= 0, r)

    # --- S3 -------------------------------------------------------------------------------------------------
    slot = b.assigned_peer_id
    old_epoch = b.snapshots[-1]["epoch"]
    b.disconnect()
    L.pump_sleep(0.1)
    u = live.pop(0)
    res = a.pickup(u[0], u[1], fresh_rid())
    L.check(f"S3 A emptied {u} while B was away", res is not None and res.accepted, r)
    b.connect_and_ready()
    L.check(f"S3 B rejoined the same slot {slot}", b.assigned_peer_id == slot, r)
    s3 = check_snapshot(b, r, "S3")
    L.check("S3 rejoin got a NEW epoch", s3 is not None and s3["epoch"] != old_epoch, r)
    L.check(f"S3 B's new snapshot shows {u} empty", b.world.tile_ut(*u) == L.EMPTY_NO, r)

    # --- S4 -------------------------------------------------------------------------------------------------
    v = live.pop(0)
    vacre, vtile = L.town_ut_to_acre_tile(*v)
    snap_seq = b.snapshots[-1]["block_seqs"][vacre]
    bmark = b.inbox.mark()
    res = a.pickup(v[0], v[1], fresh_rid())
    L.pump_sleep(0.5)
    b_deltas = [g for g in b.field_updates_since(bmark) if g.acre == vacre and g.tile == vtile]
    L.check(f"S4 A emptied {v} after B's snapshot", res is not None and res.accepted, r)
    L.check("S4 B got exactly one FIELD_UPDATE for it, world_seq newer than its snapshot block, and applied it",
            len(b_deltas) == 1 and L.seq_diff(b_deltas[0].world_seq, snap_seq) > 0 and b.world.tile_ut(*v) == L.EMPTY_NO, r)

    # --- S5 -------------------------------------------------------------------------------------------------
    c = client("S5-C", context_flags=None)
    c.connect(timeout=3.0)
    c.drop_inbound_next(lambda p: p[:1] == bytes([L.PC_NETGAME_MSG_FIELD_BLOCK]))
    c.send_identity()
    reply = c.wait_handshake_reply()
    stalled = [x for x in live if L.town_ut_to_acre_tile(*x)[0] == 0]
    later = [x for x in live if L.town_ut_to_acre_tile(*x)[0] > 0]
    mutated = {}  # tile -> value it must end up with
    if stalled:
        x = stalled[0]
        res = a.pickup(x[0], x[1], fresh_rid(), timeout=0.4)
        if res is not None and res.accepted:
            mutated[x] = L.EMPTY_NO
            live.remove(x)
    elif a.world.tile_ut(24, 24) == L.EMPTY_NO:  # acre 0's fixture tile, emptied earlier: drop onto it
        res = a.drop(0, ITM_FOOD_CHERRY, 24, 24, fresh_rid(), claim_at=(24, 24), timeout=0.4)
        if res is not None and res.accepted:
            mutated[(24, 24)] = ITM_FOOD_CHERRY
    if later:
        x = later[0]
        res = a.pickup(x[0], x[1], fresh_rid(), timeout=0.4)
        if res is not None and res.accepted:
            mutated[x] = L.EMPTY_NO
            live.remove(x)
    ok = c.wait_snapshot_complete(10.0)
    L.check(f"S5 C (first FIELD_BLOCK dropped) completed its snapshot; mutated during it: {mutated}",
            reply is not None and ok and len(c.dropped_inbound) == 1, r)
    d = client("S5-D-truth")
    d.connect_and_ready()
    diff = c.world.diff(d.world)
    L.check(f"S5 C's view equals truth (fresh client D) on every tile (diff {diff[:3]})", ok and diff == [], r)
    L.check(f"S5 C's view shows every tile mutated during its snapshot with its new value {mutated}",
            len(mutated) >= 1 and all(c.world.tile_ut(*x) == v for x, v in mutated.items()), r)
    L.info(f"S5 mutated {len(mutated)} tile(s) during C's stalled snapshot (acre 0 = the stalled acre)")
    L.info(f"S5 stale block/delta ignored by C's rules: {c.world.ignored_stale[:4]}")

    # --- S8 / S9 ---------------------------------------------------------------------------------------------
    e = client("S8-E")
    e.connect_and_ready()
    n0 = len(e.completed_snapshots())
    old = e.snapshots[-1]["epoch"]
    e.send_resync_request()
    ok = e.wait_snapshot_complete(10.0, after_count=n0)
    s8 = e.completed_snapshots()[-1] if ok else None
    L.check("S8 RESYNC_REQUEST -> a complete snapshot with a NEW epoch", ok and s8["epoch"] != old, r)
    if s8 is not None:
        check_snapshot(e, r, "S8", s8)

    f = client("S9-F", context_flags=None)
    f.connect(timeout=3.0)
    f.send_identity()
    f.wait_handshake_reply()
    L.DEFAULT_HUB.wait_until(lambda: f.snapshots and len(f.snapshots[-1]["blocks"]) >= 2, 5.0)
    f.send_resync_request()
    L.DEFAULT_HUB.wait_until(lambda: len(f.completed_snapshots()) >= 1 and len(f.snapshots) >= 2
                             and f.snapshots[-1]["end"] is not None, 10.0)
    last = f.snapshots[-1]
    first = f.snapshots[0]
    L.check(f"S9 resync mid-snapshot: {len(f.snapshots)} BEGINs, the latest epoch completed with all 30 acres",
            len(f.snapshots) >= 2 and last["end"] is not None and last["blocks"] == list(range(L.ACRE_NUM))
            and last["epoch"] != first["epoch"], r)
    L.check("S9 no block of the superseded epoch was applied after the new BEGIN (ignored, not applied)",
            all(bl.epoch != last["epoch"] for bl in f.superseded_blocks) and not f.world.violations, r)
    L.info(f"S9 first epoch got {len(first['blocks'])} block(s) before being superseded; "
           f"{len(f.superseded_blocks)} late block(s) of it ignored")

    # --- S6 -------------------------------------------------------------------------------------------------
    for cl in ALL:
        ack_idx = getattr(cl, "identity_ack_index", None)
        pre = [i for i, _k, _o in cl.world_log if ack_idx is not None and i < ack_idx]
        L.check(f"S6 {cl.label}: FIELD_UPDATE world_seq strictly increasing, no violations, stream contiguous, "
                f"no world message before IDENTITY_ACK", not cl.world.violations and cl.delivered_in_order() and not pre, r)

    # --- S7 -------------------------------------------------------------------------------------------------
    bad = client("S7-wrong-town", town=L.PROBE_TOWN, context_flags=None, wait_snapshot=False)
    try:
        bad.connect_and_ready()
        L.check("S7 wrong town is rejected", False, r)
    except L.HandshakeRejected as ex:
        L.check("S7 wrong town rejected with LAND_MISMATCH (24-byte form)",
                L.is_land_mismatch_reject(ex.reject) and len(ex.payload) == 24, r)
        L.check("S7 REJECT carries the host town identity (== IDENTITY_ACK's)",
                L.town_from_reject(ex.payload) == L.town_from_identity_ack(a.identity_ack), r)
    L.pump_sleep(1.0)
    L.check("S7 rejected client received no world data", not bad.world_log, r)
    L.check("S7 rejected client was disconnected", bad.wait_disconnected(2.0), r)
    good = client("S7-probe-then-connect")
    L.resolve_host_town(HOST, PORT)
    good.connect_and_ready()
    L.check("S7 probe-then-connect succeeds", good.assigned_peer_id is not None and good.snapshot_complete(), r)

    for cl in ALL:
        cl.close()
    return L.summary_and_exit_code(r)


if __name__ == "__main__":
    sys.exit(main())
