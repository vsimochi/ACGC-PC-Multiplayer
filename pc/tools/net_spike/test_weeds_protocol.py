#!/usr/bin/env python3
"""test_weeds_protocol.py - WEEDS (v8 unreleased): host-authoritative WEED_PULL (FIELD_ACTION kind 10) and FLOWER_TRAMPLE (kind 11).

PROTOCOL test against a REAL host process (`AnimalCrossing.exe --host --bootstrap-resident 0 --pickup-test-seed --bury-test-seed
--field-action-test-seed`, started / stopped by this script from the TEST COPY named by NET_SPIKE_GAME_BIN = pc\\build64\\bin_fixture4*;
host = resident 0, clients = residents 1..3, at most 3 simultaneous clients). The fixture save dir is snapshotted at start and RESTORED
after the host phase. The clients are scripted FakeClients: the host's C code runs for real; the REAL client half (the vanilla pull
animation reaching the seam, the client NOT writing its own tile) is covered by test_weeds_real_client.py.

--field-action-test-seed now also places, on row z=108 of the already loaded acre columns:
  (24,108) GRASS_A 0x0008   (40,108) GRASS_C 0x000A   (56,108) GRASS_B 0x0009      weeds (the two ends and the middle of IS_ITEM_GRASS)
  (72,108) FLOWER_PANSIES0 0x0845   (88,108) FLOWER_LEAVES_PANSIES0 0x083C         flowers (a grown flower and the lowest flower value)

Cases (one host phase):
  W1   WEED_PULL on a weed: accepted (granted_item 0, kind echoed), NO TXN_RESULT (nothing is granted), the tile becomes EMPTY_NO on the host,
       FIELD_UPDATE reaches the OTHER client (and the requester), one host log line
  W2   the same request_id resent: replayed (accepted, no second effect); after an intervening request the old id re-validates and is
       REJECTED (the tile is empty): never a second effect
  W3   WEED_PULL on the now empty tile / on a flower tile / on a never seeded empty tile: rejected, no mutation
  W4   out of reach (200 units away): rejected, the weed survives; in reach: accepted (GRASS_C, the other end of the range)
  W5   two clients race for the same weed (back-to-back): exactly ONE accepted, ONE FIELD_UPDATE tile change
  W6   a non-zero tag on kind 10 / 11 is BAD_SHAPE (TXN_RESULT REJECTED), no mutation
  T1   FLOWER_TRAMPLE on a grown flower: accepted, the tile becomes EXACTLY EMPTY_NO (vanilla's bIT_actor_fade_entry mapping), FIELD_UPDATE
  T2   FLOWER_TRAMPLE on a weed tile / WEED_PULL on a flower tile (kind mismatch): rejected
  T3   scene gate: a client whose PLAYER_CONTEXT says it is NOT in the town is rejected; back in town it is accepted (lowest flower value)
  T4   an unbound peer (transport connection, no IDENTITY) gets no answer and mutates nothing
  T5   a late joiner's snapshot (fresh session) shows every cleared tile as EMPTY_NO and nothing else of the row changed
Usage: python test_weeds_protocol.py [--port 11600]
"""
import argparse
import os
import shutil
import struct
import sys

import net_spike_lib as L
import test_txn_protocol as TP

KW, KT = L.FIELD_ACTION_KIND_WEED_PULL, L.FIELD_ACTION_KIND_FLOWER_TRAMPLE
EMPTY = 0
W_A, W_C, W_B = (24, 108), (40, 108), (56, 108)       # GRASS_A, GRASS_C, GRASS_B
F_P, F_L = (72, 108), (88, 108)                       # FLOWER_PANSIES0, FLOWER_LEAVES_PANSIES0
VAL = {W_A: 0x0008, W_C: 0x000A, W_B: 0x0009, F_P: 0x0845, F_L: 0x083C}
EMPTY_TILE = (70, 90)                                 # never seeded, empty in the fixture town (matches test_pickup_sync's own empty tile)
FA_RES_FMT = L.FIELD_ACTION_RESULT_FMT


def fa_results(c, rid):
    conn = c.connect_count
    out = []
    for m in c.inbox.peek_all(lambda m: m.conn == conn and m.channel == L.CH_RELIABLE and m.payload and m.payload[0] == 30
                              and len(m.payload) == 12):
        _t, kind, acc, ux, r, item, uz, _x = struct.unpack(FA_RES_FMT, m.payload)
        if r == rid:
            out.append(dict(kind=kind, accepted=acc, item=item, ut=(ux, uz)))
    return out


def txn_by_rid(c, rid):
    return [g for conn, g in c.txn_results if g.request_id == rid and conn == c.connect_count]


def request(c, kind, tile, rid=None, at=None, wait=1.5, raw=None):
    """claim a position (default the tile centre), send the 76-byte tag-less request, wait for the legacy RESULT: (result dict | None, rid)."""
    x, y, z = L.tile_center(*(at or tile))
    c.claim_position(x, y, z)
    rid = rid or request.fresh()
    c.send_reliable(raw if raw is not None else L.build_field_action_request(kind, tile[0], tile[1], rid))
    c.hub.wait_until(lambda: bool(fa_results(c, rid)), wait)
    r = fa_results(c, rid)
    return (r[0] if r else None), rid


request.fresh = L.make_request_id_counter(7000)


def host_count(run, rx, off=0):
    return run.n_log(rx, off)


def wait_tile(c, tile, value, timeout=2.5):
    return c.hub.wait_until(lambda: c.world.tile_ut(*tile) == value, timeout)


def phase_weeds(run):
    ck = run.check
    # the seed is applied a few polls after the host world is ready
    run.host.wait_for_log(r"--field-action-test-seed: flower 0x083C fixture placed at tile \(88,108\)", 30.0)
    L.pump_sleep(0.5)
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    a.drain_field_updates(timeout=0.3)
    b.drain_field_updates(timeout=0.3)
    ck("setup: the join snapshot holds the five seeded fixture tiles (3 weeds, 2 flowers)",
       all(a.world.tile_ut(*t) == v and b.world.tile_ut(*t) == v for t, v in VAL.items()))
    if not all(a.world.tile_ut(*t) == v for t, v in VAL.items()):
        print("observed:", {t: a.world.tile_ut(*t) for t in VAL})
        return
    log0 = len(run.log())

    # ---- W1
    mark_a, mark_b = a.inbox.mark(), b.inbox.mark()
    r1, rid1 = request(a, KW, W_A)
    ck("W1 WEED_PULL on a seeded weed answered", r1 is not None)
    if r1 is None:
        return
    ck("W1 accepted, kind 10 echoed, granted_item 0, the requested tile echoed",
       r1["accepted"] == 1 and r1["kind"] == KW and r1["item"] == 0 and r1["ut"] == W_A)
    ck("W1 nothing is granted: NO TXN_RESULT for it", not txn_by_rid(a, rid1))
    ck("W1 FIELD_UPDATE: the OTHER client sees the tile become EMPTY_NO", wait_tile(b, W_A, EMPTY))
    ck("W1 and the requester sees it too (it never wrote the tile itself: a scripted client has no vanilla seam)", wait_tile(a, W_A, EMPTY))
    ups_b = [L.field_update_tuple(f) for f in b.field_updates_since(mark_b)]
    ck("W1 exactly one FIELD_UPDATE for that tile reached B, with value 0", [u for u in ups_b if u[:2] == W_A] == [(W_A[0], W_A[1], 0)])
    ck("W1 the host logged exactly one WEED_PULL commit with the vanilla weed value -> EMPTY_NO",
       host_count(run, r"\[NET\]\[WEEDS\] host: peer \d+ WEED_PULL at tile \(24,108\) 0x0008 -> EMPTY_NO", log0) == 1)

    # ---- W2 (replay)
    n_ups = len(b.field_updates_since(mark_b))
    a.send_reliable(L.build_field_action_request(KW, W_A[0], W_A[1], rid1))
    a.hub.wait_until(lambda: len(fa_results(a, rid1)) >= 2, 1.5)
    rs = fa_results(a, rid1)
    ck("W2 the same request_id resent is REPLAYED: a second identical accepted RESULT", len(rs) == 2 and rs[1] == rs[0] and rs[1]["accepted"] == 1)
    ck("W2 and it did not run a second commit (still ONE host line) or produce a new FIELD_UPDATE",
       host_count(run, r"WEED_PULL at tile \(24,108\)", log0) == 1 and len(b.field_updates_since(mark_b)) == n_ups)
    other, _ = request(a, KW, EMPTY_TILE)  # an intervening request replaces the per-peer replay record
    a.send_reliable(L.build_field_action_request(KW, W_A[0], W_A[1], rid1))
    a.hub.wait_until(lambda: len(fa_results(a, rid1)) >= 3, 1.5)
    rs = fa_results(a, rid1)
    ck("W2 after an intervening request the old id is re-VALIDATED, finds EMPTY_NO and is REJECTED (never a second effect)",
       len(rs) == 3 and rs[2]["accepted"] == 0 and host_count(run, r"WEED_PULL at tile \(24,108\)", log0) == 1)

    # ---- W3 (rejections)
    r, _ = request(a, KW, W_A)
    ck("W3 WEED_PULL on the now empty tile is rejected", r is not None and r["accepted"] == 0)
    ck("W3 WEED_PULL on a never seeded empty tile is rejected", other is not None and other["accepted"] == 0)
    r, _ = request(a, KW, F_P)
    ck("W3 WEED_PULL on a flower tile is rejected (the weed range is GRASS_A..GRASS_C only) and the flower survives",
       r is not None and r["accepted"] == 0 and b.world.tile_ut(*F_P) == VAL[F_P])
    r, _ = request(a, KT, W_C)
    ck("T2 FLOWER_TRAMPLE on a weed tile is rejected and the weed survives", r is not None and r["accepted"] == 0 and b.world.tile_ut(*W_C) == VAL[W_C])

    # ---- W4 (reach)
    cx, cy, cz = L.tile_center(*W_C)
    a.claim_position(cx + 200.0, cy, cz)
    rid = request.fresh()
    a.send_reliable(L.build_field_action_request(KW, W_C[0], W_C[1], rid))
    a.hub.wait_until(lambda: bool(fa_results(a, rid)), 1.5)
    r = fa_results(a, rid)
    ck("W4 WEED_PULL from 200 units away is rejected, the weed survives", bool(r) and r[0]["accepted"] == 0 and b.world.tile_ut(*W_C) == VAL[W_C])
    r, _ = request(a, KW, W_C)
    ck("W4 in reach the same weed (GRASS_C, the top of the range) is pulled", r is not None and r["accepted"] == 1 and wait_tile(b, W_C, EMPTY))

    # ---- W6 (BAD_SHAPE) -- two probes on a throw-away session (a third violation would close the peer), before the weed W_B is raced
    g = run.client("G", run.r3)
    g.connect_and_ready(quiet=True)
    tag = g.build_grant_tag(TP.D_POCKET, None, 0)[0]
    gx, gy, gz = L.tile_center(*W_B)
    g.claim_position(gx, gy, gz)
    rids = []
    for kind, tile in ((KW, W_B), (KT, F_P)):
        rid = request.fresh()
        rids.append(rid)
        g.send_reliable(L.build_field_action_request(kind, tile[0], tile[1], rid, tag=tag))
    g.hub.wait_until(lambda: all(txn_by_rid(g, x) for x in rids), 3.0)
    res = [txn_by_rid(g, x) for x in rids]
    ck("W6 a non-zero tag on WEED_PULL / FLOWER_TRAMPLE is REJECTED(BAD_SHAPE) (TXN_RESULT), never executed",
       all(len(x) == 1 and x[0].outcome == TP.REJECTED and x[0].reason == TP.R["BAD_SHAPE"] for x in res))
    ck("W6 and the weed / flower are untouched", b.world.tile_ut(*W_B) == VAL[W_B] and b.world.tile_ut(*F_P) == VAL[F_P])
    run.release(g)

    # ---- W5 (race): both stand on the weed and fire back-to-back
    bx, by, bz = L.tile_center(*W_B)
    a.claim_position(bx, by, bz)
    b.claim_position(bx, by, bz)
    ra, rb = request.fresh(), request.fresh()
    mark_a, mark_b = a.inbox.mark(), b.inbox.mark()
    a.send_reliable(L.build_field_action_request(KW, W_B[0], W_B[1], ra))
    b.send_reliable(L.build_field_action_request(KW, W_B[0], W_B[1], rb))
    a.hub.wait_until(lambda: bool(fa_results(a, ra)) and bool(fa_results(b, rb)), 2.5)
    fa, fb = fa_results(a, ra), fa_results(b, rb)
    ck("W5 both racers got an answer", bool(fa) and bool(fb))
    ck("W5 EXACTLY ONE racer was accepted", bool(fa) and bool(fb) and fa[0]["accepted"] + fb[0]["accepted"] == 1)
    wait_tile(a, W_B, EMPTY)
    wait_tile(b, W_B, EMPTY)
    ups = [L.field_update_tuple(f) for f in a.field_updates_since(mark_a) if L.field_update_tuple(f)[:2] == W_B]
    ck("W5 the tile changed once (one FIELD_UPDATE, value 0) and both mirrors agree", ups == [(W_B[0], W_B[1], 0)]
       and a.world.tile_ut(*W_B) == EMPTY and b.world.tile_ut(*W_B) == EMPTY)
    ck("W5 one host WEED_PULL line for that tile", host_count(run, r"WEED_PULL at tile \(56,108\)", log0) == 1)

    # ---- T1 (trample, vanilla mapping)
    mark_b = b.inbox.mark()
    r, rid = request(a, KT, F_P)
    ck("T1 FLOWER_TRAMPLE on a grown flower accepted (kind 11 echoed, granted_item 0)",
       r is not None and r["accepted"] == 1 and r["kind"] == KT and r["item"] == 0 and r["ut"] == F_P)
    ck("T1 the tile becomes EXACTLY EMPTY_NO (vanilla: bIT_actor_fade_entry -> mFI_SetFG_common(EMPTY_NO)) on the host, seen by the other client",
       wait_tile(b, F_P, EMPTY))
    ck("T1 one FIELD_UPDATE (value 0), no TXN_RESULT, one host FLOWER_TRAMPLE line with the flower value",
       [L.field_update_tuple(f) for f in b.field_updates_since(mark_b) if L.field_update_tuple(f)[:2] == F_P] == [(F_P[0], F_P[1], 0)]
       and not txn_by_rid(a, rid)
       and host_count(run, r"FLOWER_TRAMPLE at tile \(72,108\) 0x0845 -> EMPTY_NO", log0) == 1)
    r2, _ = request(a, KT, F_P)
    ck("T1 a second trample of the same tile is rejected", r2 is not None and r2["accepted"] == 0)

    # ---- T2 / T4 / T3 on the lowest flower value
    r, _ = request(a, KW, F_L)
    ck("T2 WEED_PULL on the lowest flower value is rejected, the flower survives", r is not None and r["accepted"] == 0 and b.world.tile_ut(*F_L) == VAL[F_L])
    u = run.client("U", run.r3)
    u.connect(timeout=3.0)  # transport only: no IDENTITY, never READY
    ux, uy, uz = L.tile_center(*F_L)
    u.claim_position(ux, uy, uz)
    rid = request.fresh()
    u.send_reliable(L.build_field_action_request(KT, F_L[0], F_L[1], rid))
    L.pump_sleep(1.0)
    ck("T4 an unbound peer's FLOWER_TRAMPLE gets no RESULT and mutates nothing", not fa_results(u, rid) and b.world.tile_ut(*F_L) == VAL[F_L])
    run.release(u)
    b.send_player_context(flags=0)  # not in the town scene
    L.pump_sleep(0.4)
    r, _ = request(b, KT, F_L)
    ck("T3 a client whose context says it is NOT in town is rejected, the flower survives", r is not None and r["accepted"] == 0
       and a.world.tile_ut(*F_L) == VAL[F_L])
    b.send_player_context(flags=L.PC_NETGAME_CTX_FLAG_IN_TOWN)
    L.pump_sleep(0.4)
    r, _ = request(b, KT, F_L)
    ck("T3 back in town the same request is accepted (FLOWER_LEAVES_PANSIES0, the lowest value of IS_ITEM_FLOWER) and the tile is EMPTY_NO",
       r is not None and r["accepted"] == 1 and wait_tile(a, F_L, EMPTY))

    # ---- T5 late joiner
    run.release(b)
    c = run.ready("C", run.r2)
    ck("T5 a late joiner's snapshot shows every touched tile as EMPTY_NO", all(c.world.tile_ut(*t) == EMPTY for t in VAL))
    ck("T5 no WEEDS accepted line beyond the five commits", host_count(run, r"\[NET\]\[WEEDS\] host: peer", log0) == 5)


def cli_checks(results):
    import subprocess
    exe = os.path.join(L.GAME_BIN_DIR, L.GAME_EXE_NAME)
    L.require_launchable_bin_dir(L.GAME_BIN_DIR)
    p = subprocess.run([exe, "--help"], cwd=L.GAME_BIN_DIR, capture_output=True, timeout=30)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    L.check("CLI --help documents --force-weed-pull (a Client-only test hook, default off)", p.returncode == 0 and "--force-weed-pull" in out
            and "Client-only test hook" in out, results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11600)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "weeds_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        TP.run_phase(results, ip, args.port, "weeds", [], phase_weeds, snap_gci, restore)
        cli_checks(results)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
