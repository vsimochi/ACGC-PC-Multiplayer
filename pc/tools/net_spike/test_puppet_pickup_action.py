#!/usr/bin/env python3
"""test_puppet_pickup_action.py - M9-C Phase 5 (remote pickup presentation + PLAYER_ACTION id 46, protocol stays v7).

Real host process (`--bootstrap-resident 0 --pickup-test-seed`, PC_PUPPET_DIAG=1 in its environment) plus scripted
FakeClients.  Client A performs REAL two-phase pickups (PICKUP_REQUEST -> provisional RESULT -> INTERACT_CONFIRM) of the
seeded fixture tiles, standing on the tile for the host's reach check and then moving next to the host player so its puppet
is inside the presentation range.  Observable decisions (env-gated, quiet by default):
    [NET][ACTION][DIAG] host: PLAYER_ACTION kind=1 origin=O seq=S tile=(ux,uz) item=0xIIII relayed_to=N
    [NET][PUPPET][DIAG] player O pickup event seq=S tile=(ux,uz) item=0xIIII state=<pending|started|late|dropped|bell|ignored> [budget=B]
    [NET][PUPPET][DIAG] player O pickup flying item start frame=F
    [NET][PUPPET][DIAG] player O <pickup_item_get|pickup_gasagoso> sound label=L frame=F budget=B

Tier labels (honest): PROTOCOL TESTED (wire layout, host-only origination, relay/exclusion), HOOK-DRIVEN (automated, no human
input) for the puppet pairing/gating decisions, SOURCE AUDITED for the code shape.  NO visual or audible verification: these
checks prove the event was emitted/relayed/validated, the puppet paired it with its own pickup row and ran the draw path
without crashing; NOT that the flying item looks right.

  A0  source audit (no world/inventory/RNG call in the presentation, host-only origination, id 46 unique + maximum)
  P1  legit pickup: B gets exactly one PLAYER_ACTION (10 bytes, A's id, tile, item == the snapshot item), A gets none, host
      logs the emission + the puppet's `pending`; then A's MOVE pickup row (30) pairs it -> `started` + `flying item start`
      + ITEM_GET (0x40) and GASAGOSO (0x69) sounds
  P2  aborted pickup (INTERACT_CONFIRM ABORT) and a rejected request (already empty tile): no event, tile untouched
  P3  client-originated PLAYER_ACTION (valid, unknown kind, wrong sizes): the host drops it, B receives nothing, no puppet
      event, host alive
  P4  pairing: event arrives DURING the clip -> `late`/`started`; after the flight (clip still running) -> `dropped late`;
      after the clip finished -> held, then `dropped expired`
  P5  a peer in another scene (SHOP): event paired with its pickup row -> `ignored budget=scene`, sounds budget=scene
  P6  disconnect clears the queued event: nothing pairs after the reconnect
  P7  no world mutation: exactly one FIELD_UPDATE per committed pickup tile (none for the aborted one), a fresh authoritative
      snapshot agrees, no crash text; the disconnect summary reports the pickup counters
  (Capacity: the host admits at most 3 clients on the 4-resident fixture since Stage 1A.  OBS and A stay connected; the
   SHOP / G1 / G2 peers each leave gracefully (host teardown line awaited, explicit check) before the next joins, and G2
   leaves before the P7 snapshot probe, so never more than 3 connections are open.)
  S2  (--only s2) real client process: it receives the relayed PLAYER_ACTION about A (`state=pending` in the client log)

Usage: python test_puppet_pickup_action.py [--port 8300] [--only a0|p|s2]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import struct
import sys
import time

import net_spike_lib as L
import wire_baseline
from test_player_scene_real import boot_host, boot_client
from test_puppet_cosmetics import scene_msg, SCENE_FG, SCENE_SHOP0, strip_comments, leave_peer
from test_puppet_held_item import clean, make_b, wait_visual

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "..", "src", "pc_remote_player.c")
NETSRC = os.path.join(HERE, "..", "..", "src", "pc_net_game.c")
IDLE = 0
IDX_PICKUP = 30
KIND_PICKUP = 1

ACT_RX = re.compile(r"\[NET\]\[ACTION\]\[DIAG\] host: PLAYER_ACTION kind=(\d+) origin=(\d+) seq=(\d+) tile=\((\d+),(\d+)\) "
                    r"item=0x([0-9A-Fa-f]+) relayed_to=(\d+)")
EV_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) pickup event seq=(\d+) tile=\((-?\d+),(-?\d+)\) "
                   r"item=0x([0-9A-Fa-f]+) state=(\w+)(?: budget=(\w+))?")
START_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) pickup flying item start frame=([0-9.]+)")
SND_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) (pickup_item_get|pickup_gasagoso) sound label=(-?\d+) "
                    r"frame=(\S+) budget=(\w+)")
DROPPED_RX = re.compile(r"client-originated PLAYER_ACTION")
SUM_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) cosmetics summary: .*pickup_events=(\d+) pickup_started=(\d+) "
                    r"pickup_late=(\d+) pickup_dropped=(\d+) pickup_bell=(\d+) pickup_ignored=(\d+) pickup_sounds=(\d+)")
NO_START_RX = re.compile(r"state=(started|late)")


def events(text, pid, seq=None):
    out = []
    for m in EV_RX.finditer(text):
        if int(m.group(1)) == pid and (seq is None or int(m.group(2)) == seq):
            out.append(m.groups())
    return out


def states(text, pid, seq):
    return [e[5] for e in events(text, pid, seq)]


class Peer:
    def __init__(self, port, label, scene=SCENE_FG):
        self.c = make_b(port, label)
        self.pid = self.c.assigned_peer_id
        self.scene_seq = 0
        self.set_scene(scene)
        L.pump_sleep(1.5, self.c.hub)
        msgs = [m for m in self.c.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_MOVE, (L.CH_UNRELIABLE,)))
                if m.game is not None and m.game.net_player_id == L.PC_NETGAME_HOST_PLAYER_ID]
        g = msgs[-1].game if msgs else None
        self.host_xyz = (g.pos_x, g.pos_y, g.pos_z) if g else (1000.0, 0.0, 1000.0)
        self.x, self.y, self.z = self.host_xyz[0] + 80.0, self.host_xyz[1], self.host_xyz[2]
        self.angle = 0
        self.frame = 1000
        self.rid = 100

    def set_scene(self, sid):
        self.scene_seq += 1
        self.c.send_reliable(scene_msg(self.scene_seq, sid))

    def move_once(self, main_index=None, counter=0):
        self.frame += 1
        self.c.send_move(self.frame, self.x, self.y, self.z, angle=self.angle, speed=0.0, move_state=IDLE,
                         main_index=main_index, entry_counter=counter)

    def stream(self, seconds, main_index=None, counter=0, at=None, action=None):
        """MOVE stream at ~20 Hz; `at` = seconds after which `action()` runs once (e.g. the COMMIT)."""
        t0 = time.monotonic()
        end = t0 + seconds
        done = action is None
        while time.monotonic() < end:
            self.move_once(main_index, counter)
            if not done and time.monotonic() - t0 >= at:
                action()
                done = True
            L.pump_sleep(0.05, self.c.hub)

    def stand_on(self, tile):
        """Claim the tile centre with a FRESH frame number of this peer's own MOVE stream (reliable, ordered before the request)."""
        self.frame += 1
        x, y, z = L.tile_center(*tile)
        self.c.send_move(self.frame, x, self.y, z, angle=0, speed=0.0, move_state=IDLE, reliable=True)
        L.pump_sleep(0.15, self.c.hub)

    def hold_pickup(self, tile):
        """Stand on the tile (the host's reach check uses the last MOVE position), request, return (rid, result)."""
        self.rid += 1
        rid = self.rid
        self.stand_on(tile)
        self.c.send_pickup_request(tile[0], tile[1], rid, auto_confirm=False)
        res = self.c.recv_pickup_result(timeout=1.5, expect_request_id=rid)
        # back next to the host player (inside the puppet presentation range) before anything is committed
        self.stream(1.2)
        return rid, res

    def commit(self, rid):
        self.c.commit_pending(L.CONFIRM_KIND_PICKUP, rid)  # X1b: a TXN_COMMIT (the legacy CONFIRM(COMMIT) is retired)

    def abort(self, rid):
        self.c.confirm(L.CONFIRM_KIND_PICKUP, rid, L.CONFIRM_OUTCOME_ABORT, L.CONFIRM_REASON_CANCELLED)

    def close(self):
        try:
            self.c.close()
        except Exception:
            pass


def a0(check):
    print("=" * 72 + "\n[A0] SOURCE AUDITED: PLAYER_ACTION + pickup presentation")
    src = open(SRC, encoding="utf-8").read()
    code = strip_comments(src)
    start = code.index("static void pc_pk_diag_event")
    end = code.index("static void pc_puppet_pending_run", start)
    blk = code[start:end]
    forbidden = ["pcfa_set_tile", "mFI_SetFG", "Player_actor_", "putin_item", "pc_net_game_request_", "GET_PLAYER_ACTOR_NOW",
                 "Now_Private", "RANDOM(", "mISL_", "mPr_SetPossessionItem", "mPr_GivePossession", "mPr_SetFreePossessionItem",
                 "mPr_ClearPlayerAction", "fade_entry", "sAdo_PlyWalkSe"]
    for f in forbidden:
        check("A0 pickup presentation block does not contain `%s`" % f, f not in blk)
    check("A0 draw is bg_item_clip->single_draw_proc with NULL checks and the scene gate",
          "Common_Get(clip).bg_item_clip->single_draw_proc(game" in blk and "bg_item_clip == NULL" in blk and
          "pc_pk_gate(self, slot, game) != NULL" in blk)
    check("A0 vanilla timeline constants: item flies 20..40, sounds at clip frames 10 / 20, morph equivalent 12",
          "PC_PUPPET_PK_FLY_START 20.0f" in src and "PC_PUPPET_PK_TIMER_END 40.0f" in src and
          "PC_PUPPET_PK_SOUND_GET_FRAME 10.0f" in src and "PC_PUPPET_PK_SOUND_RUSTLE_FRAME 20.0f" in src and
          "PC_PUPPET_PK_TIMER_BASE 12.0f" in src)
    check("A0 sounds are NA_SE_ITEM_GET / NA_SE_GASAGOSO through the budgeted pc_puppet_fx_reason(sound) path",
          "NA_SE_ITEM_GET" in blk and "NA_SE_GASAGOSO" in blk and "pc_puppet_fx_reason(self, slot, game, 1)" in blk)
    check("A0 the net handler only QUEUES (pc_remote_player_on_action does not call the tick/draw/gate)",
          re.search(r"int pc_remote_player_on_action\(.*?\n\}\n", code, re.S) is not None and
          all(x not in re.search(r"int pc_remote_player_on_action\(.*?\n\}\n", code, re.S).group(0)
              for x in ("pc_pk_gate", "pc_pk_snd", "single_draw_proc", "pc_puppet_pickup_tick", "sAdo_")))
    check("A0 bells (money bags) have no flying item", "pc_pk_item_is_bell(ev.item)" in blk and "mPr_GetAmountForMoneyItem" in blk)
    check("A0 hold timeout 90 frames and one-event-per-run pairing", "PC_PUPPET_PK_HOLD_FRAMES 90.0f" in src and "c->pk_bound" in blk)
    net = open(NETSRC, encoding="utf-8").read()
    ncode = strip_comments(net)
    # Ids 47+ exist now (D3 47..50, X1 51/52), so 46 is no longer the maximum. Kept strict: id 46 is present and unique, and ALL
    # ids are contiguous 1..N with N == the current maximum parsed from the header == wire_baseline's source of truth.
    msg_ids = [int(x) for x in re.findall(r"PC_NETGAME_MSG_[A-Z_0-9]+\s*=\s*(\d+)", net[:net.index("} PCNetGameMsgType;")])]
    check("A0 PLAYER_ACTION = 46 and unique; ids are unique and contiguous 1..N with N == max parsed from the enum == "
          "wire_baseline.EXPECTED_MAX_MSG_ID (%d)" % wire_baseline.EXPECTED_MAX_MSG_ID,
          re.search(r"PC_NETGAME_MSG_PLAYER_ACTION\s*=\s*46", net) is not None and msg_ids.count(46) == 1 and
          len(msg_ids) == len(set(msg_ids)) and sorted(msg_ids) == list(range(1, max(msg_ids) + 1)) and
          max(msg_ids) == wire_baseline.EXPECTED_MAX_MSG_ID)
    check("A0 wire struct is 10 bytes (static asserts for size and offsets)",
          "sizeof(PCNetGamePlayerActionMsg) == 10" in net and "offsetof(PCNetGamePlayerActionMsg, item) == 6" in net)
    check("A0 net_spike_lib PLAYER_ACTION constants/spec match (46, 10 bytes)",
          L.PC_NETGAME_MSG_PLAYER_ACTION == 46 and L.PLAYER_ACTION_SPEC.size == 10 and L.PC_NETGAME_PROTOCOL_VERSION == wire_baseline.EXPECTED_PROTOCOL_VERSION)
    check("A0 emission only from the host commit and the host-local notify (2 call sites + definition)",
          ncode.count("pcnetgame_host_emit_player_action(") == 3)
    i = ncode.index("static void pcnetgame_host_emit_player_action")
    j = ncode.index("static void pcnetgame_handle_client_player_action", i)
    emit = ncode[i:j]
    check("A0 emit is host-only, skips the origin peer, never writes the field",
          "s_role != PC_NETGAME_ROLE_HOST" in emit and "i != origin" in emit and "pcfa_set_tile" not in emit)
    k = ncode.index("static void pcnetgame_handle_client_player_action")
    handler = ncode[k:ncode.index("\n}\n", k)]
    check("A0 client handler is wrap-safe, READY-gated, ignores 'about me', and has no field/inventory call",
          "(int16_t)(uint16_t)(in->seq - s_action_last_seq[origin]) <= 0" in handler and "s_client_link != PC_NETGAME_LINK_READY" in handler
          and "s_client_assigned_peer_id" in handler and all(x not in handler for x in ("pcfa_set_tile", "mFI_", "mPr_", "pc_net_game_request_")))
    check("A0 host drops client-originated PLAYER_ACTION (explicit dispatch case, never relayed)",
          "client-originated PLAYER_ACTION" in net)
    hdr = open(os.path.join(HERE, "..", "..", "include", "pc_net_game.h"), encoding="utf-8").read()
    check("A0 header: PC_NETGAME_PROTOCOL_VERSION is the expected version and the v7 paragraph mentions PLAYER_ACTION (id 46)",
          wire_baseline.header_protocol_ok(hdr) and "PC_NETGAME_MSG_PLAYER_ACTION (id 46" in hdr)


def p_all(port, log_dir, check):
    print("=" * 72 + "\n[P1..P7] HOOK-DRIVEN: PLAYER_ACTION pickup relay + puppet pairing")
    host = boot_host(port, ["--authoritative-wildlife", "--pickup-test-seed"], "pickup_all", log_dir)
    if host is None:
        check("P host reached field", False)
        return
    peers = []

    def new_peer(label, scene=SCENE_FG):
        p = Peer(port, label, scene)
        peers.append(p)
        return p

    try:
        obs = new_peer("OBS")
        a = new_peer("A")
        obs.stream(1.0)
        a.stream(2.0)
        check("P puppet visual initialised", wait_visual(host, 0))
        tiles = [t for t in L.TOWN_FIXTURE_TILES]
        before = {t: obs.c.world.tile_ut(*t) for t in tiles}
        occupied = [t for t in tiles if before[t] not in (None, L.EMPTY_NO)]
        print("INFO - %d/%d fixture tiles occupied in the observer snapshot" % (len(occupied), len(tiles)))
        check("P the seeded fixture tiles are visible in the observer snapshot (>= 8)", len(occupied) >= 8)
        if len(occupied) < 8:
            return
        mark_b = obs.c.inbox.mark()
        committed = []

        # ---- P1: a legit pickup ----
        t1 = occupied[0]
        mark_a = a.c.inbox.mark()
        mark_b1 = obs.c.inbox.mark()
        rid, res = a.hold_pickup(t1)
        check("P1 the request was provisionally accepted (%s)" % (res,), res is not None and res[0] == 1)
        off = len(host.log_text())
        a.stream(0.3)  # idle, still not committed
        check("P1 no PLAYER_ACTION before the COMMIT (a provisional accept is not a success)",
              obs.c.player_actions_since(mark_b1) == [])
        a.stream(0.8, at=0.0, action=lambda: a.commit(rid))
        obs.stream(0.4)
        pa = obs.c.player_actions_since(mark_b1)
        raw = [m.payload for m in obs.c.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_PLAYER_ACTION, (L.CH_RELIABLE,)),
                                                        since=mark_b1)]
        committed.append(t1)
        check("P1 observer B received exactly one PLAYER_ACTION (10 bytes)", len(pa) == 1 and len(raw) == 1 and len(raw[0]) == 10)
        if pa:
            g = pa[0]
            check("P1 fields: origin = A's id, kind PICKUP, flags 0, tile = the picked tile, item = the snapshot item, seq 1 "
                  "(got %s, expected tile %s item 0x%04X)" % (tuple(g), t1, before[t1]),
                  (g.net_player_id, g.kind, g.flags, (g.ut_x, g.ut_z), g.item, g.seq) ==
                  (a.pid, KIND_PICKUP, 0, t1, before[t1], 1))
        check("P1 the originating client A received NO PLAYER_ACTION", a.c.player_actions_since(mark_a) == [])
        t = host.log_text()[off:]
        am = [m for m in ACT_RX.finditer(t)]
        check("P1 host logged one emission (origin A, relayed to exactly 1 = the observer, not A) (%s)" %
              [m.groups() for m in am], len(am) == 1 and int(am[0].group(2)) == a.pid and int(am[0].group(7)) == 1 and
              (int(am[0].group(4)), int(am[0].group(5))) == t1 and int(am[0].group(6), 16) == before[t1])
        evs = events(t, a.pid, 1)
        check("P1 the host's own puppet of A queued it (state=pending) (%s)" % [e[5] for e in evs],
              len(evs) >= 1 and evs[0][5] == "pending" and (int(evs[0][2]), int(evs[0][3])) == t1)
        # the puppet shows its pickup row now: the held event pairs with it
        off = len(host.log_text())
        a.stream(1.8, IDX_PICKUP, 1)
        t = host.log_text()[off:]
        st = states(t, a.pid, 1)
        check("P1 event BEFORE the pickup row: held, then paired at the row's start (state=started) (%s)" % st,
              st[:1] == ["started"])
        starts = [m for m in START_RX.finditer(t) if int(m.group(1)) == a.pid]
        check("P1 `pickup flying item start` logged once", len(starts) == 1)
        snd = [m.groups() for m in SND_RX.finditer(t) if int(m.group(1)) == a.pid]
        got = {s[1]: (s[2], s[4]) for s in snd}
        check("P1 ITEM_GET (label 64) at clip frame 10 and GASAGOSO (label 105) at frame 20, budget=ok (%s)" % got,
              got.get("pickup_item_get") == ("64", "ok") and got.get("pickup_gasagoso") == ("105", "ok"))
        check("P1 sounds fire at/after their clip frames (frame field >= 10 / >= 20)",
              all(float(s[3]) >= (10.0 if s[1] == "pickup_item_get" else 20.0) - 1e-3 for s in snd))
        a.stream(1.5)  # leave the latched row

        # ---- P2: abort + rejected ----
        t2 = occupied[1]
        mark_b2 = obs.c.inbox.mark()
        off = len(host.log_text())
        rid2, res2 = a.hold_pickup(t2)
        a.abort(rid2)
        a.stream(0.8)
        # tile t1 is already empty: a fresh request for it is rejected
        a.rid += 1
        a.stand_on(t1)
        a.c.send_pickup_request(t1[0], t1[1], a.rid, auto_confirm=False)
        rej = a.c.recv_pickup_result(timeout=1.5, expect_request_id=a.rid)
        a.stream(0.8)
        obs.stream(0.5)
        check("P2 the aborted request was provisionally accepted, the repeat request for the empty tile was rejected (%s / %s)" %
              (res2, rej), res2 is not None and res2[0] == 1 and rej is not None and rej[0] == 0)
        check("P2 aborted + rejected pickups: no PLAYER_ACTION to the observer", obs.c.player_actions_since(mark_b2) == [])
        check("P2 no host emission line for them", ACT_RX.search(host.log_text()[off:]) is None)
        check("P2 no puppet event queued for them", events(host.log_text()[off:], a.pid) == [])

        # ---- P3: client-originated PLAYER_ACTION ----
        mark_b3 = obs.c.inbox.mark()
        off = len(host.log_text())
        a.c.send_player_action(net_player_id=obs.pid, seq=7)                   # valid shape, claims to be about B
        a.c.send_player_action(kind=9, seq=8)                                  # unknown kind
        a.c.send_player_action(raw=struct.pack("<BBBBBBHH", 46, 0, 1, 0, 40, 40, 0x2800, 9)[:9])   # truncated
        a.c.send_player_action(raw=struct.pack("<BBBBBBHH", 46, 0, 1, 0, 40, 40, 0x2800, 10) + b"\x00")  # oversized
        a.stream(1.0)
        obs.stream(0.5)
        t = host.log_text()[off:]
        check("P3 the host logged 4 dropped client-originated PLAYER_ACTION messages (%d)" % len(DROPPED_RX.findall(t)),
              len(DROPPED_RX.findall(t)) == 4)
        check("P3 nothing relayed to the observer and no puppet event for them",
              obs.c.player_actions_since(mark_b3) == [] and events(t, a.pid) == [] and events(t, obs.pid) == [])
        check("P3 host alive", host.alive())

        # ---- P4: pairing timelines ----
        # (a) event during the clip (commit ~0.35 s after the first pickup MOVE)
        t3 = occupied[2]
        rid3, _ = a.hold_pickup(t3)
        off = len(host.log_text())
        a.stream(1.8, IDX_PICKUP, 2, at=0.35, action=lambda: a.commit(rid3))
        committed.append(t3)
        t = host.log_text()[off:]
        seq3 = int(ACT_RX.findall(t)[0][2]) if ACT_RX.findall(t) else -1
        st = states(t, a.pid, seq3)
        print("INFO - P4a states for seq %d: %s" % (seq3, st))
        check("P4a event arriving DURING the pickup clip: paired from the current clip frame (late or started) (%s)" % st,
              len(st) >= 2 and st[0] == "pending" and st[1] in ("late", "started"))
        check("P4a the flying item started (log line present)",
              len([m for m in START_RX.finditer(t) if int(m.group(1)) == a.pid]) == 1)
        a.stream(1.5)
        # (b) event after the flight is over, clip still running
        t4 = occupied[3]
        rid4, _ = a.hold_pickup(t4)
        off = len(host.log_text())
        a.stream(2.6, IDX_PICKUP, 3, at=0.95, action=lambda: a.commit(rid4))
        committed.append(t4)
        t = host.log_text()[off:]
        seq4 = int(ACT_RX.findall(t)[0][2]) if ACT_RX.findall(t) else -1
        st = states(t, a.pid, seq4)
        print("INFO - P4b states for seq %d: %s" % (seq4, st))
        check("P4b event after the flight (clip still running or finished): never started, finally dropped (%s)" % st,
              len(st) >= 1 and st[0] == "pending" and "started" not in st and "late" not in st and "dropped" in st)
        a.stream(1.5)
        # (c) event after the clip finished: held, then expires
        t5 = occupied[4]
        rid5, _ = a.hold_pickup(t5)
        off = len(host.log_text())
        a.stream(1.9, IDX_PICKUP, 4)           # the one-shot clip (29 frames at 0.5 = ~1 s) has finished
        a.commit(rid5)
        committed.append(t5)
        a.stream(2.6, IDX_PICKUP, 4)           # still the same (finished) row: the event waits and expires
        t = host.log_text()[off:]
        seq5 = int(ACT_RX.findall(t)[0][2]) if ACT_RX.findall(t) else -1
        evs = events(t, a.pid, seq5)
        print("INFO - P4c events for seq %d: %s" % (seq5, [(e[5], e[6]) for e in evs]))
        check("P4c event after the clip finished: pending then dropped (budget=expired), never started (%s)" %
              [(e[5], e[6]) for e in evs],
              [e[5] for e in evs] == ["pending", "dropped"] and evs[1][6] == "expired")
        a.stream(1.0)

        # ---- P5: another scene ----
        shop = new_peer("SHOP", scene=SCENE_SHOP0)
        shop.stream(1.5)
        t6 = occupied[5]
        ridS, _ = shop.hold_pickup(t6)
        committed.append(t6)
        shop.commit(ridS)
        shop.stream(0.4)
        off = len(host.log_text())
        shop.stream(1.8, IDX_PICKUP, 1)
        t = host.log_text()[off:]
        evs = events(host.log_text()[off - 0:], shop.pid)
        allev = events(host.log_text(), shop.pid)
        sn = [m.groups() for m in SND_RX.finditer(t) if int(m.group(1)) == shop.pid]
        print("INFO - P5 events %s sounds %s" % ([(e[5], e[6]) for e in allev], [(s[1], s[4]) for s in sn]))
        check("P5 SHOP peer: event paired with its pickup row -> state=ignored budget=scene (%s)" %
              [(e[5], e[6]) for e in allev], len(allev) >= 2 and allev[-1][5] == "ignored" and allev[-1][6] == "scene")
        check("P5 no flying item start and no ok sound for the SHOP peer",
              not [m for m in START_RX.finditer(t) if int(m.group(1)) == shop.pid] and
              all(s[4] == "scene" for s in sn) and len(sn) >= 1)
        check("P5 SHOP peer left gracefully (host teardown line seen)", leave_peer(host, shop))
        peers.remove(shop)

        # ---- P6: disconnect clears the queue ----
        g1 = new_peer("G1")
        g1.stream(1.5)
        t7 = occupied[6]
        ridG, _ = g1.hold_pickup(t7)
        g1.commit(ridG)
        committed.append(t7)
        g1.stream(0.6)
        gseq = int(ACT_RX.findall(host.log_text())[-1][2])
        gpid = g1.pid
        pend = events(host.log_text(), gpid, gseq)
        check("P6 the event was queued (pending) before the disconnect (%s)" % [e[5] for e in pend],
              [e[5] for e in pend] == ["pending"])
        off = len(host.log_text())
        check("P6 G1 left gracefully (host teardown line seen)", leave_peer(host, g1))
        peers.remove(g1)
        host.wait_for_log(r"player %d cosmetics summary: .*pickup_events=" % gpid, 15.0, since_offset=off)
        sms = [x for x in SUM_RX.finditer(host.log_text()[off:]) if int(x.group(1)) == gpid]
        check("P6/P7 destroy summary reports pickup_events=1 and nothing started (%s)" %
              (None if not sms else sms[-1].groups(),),
              bool(sms) and int(sms[-1].group(2)) == 1 and int(sms[-1].group(3)) == 0 and int(sms[-1].group(4)) == 0)
        g2 = new_peer("G2")
        off = len(host.log_text())
        g2.stream(3.0, IDX_PICKUP, 1)
        t = host.log_text()[off:]
        check("P6 after the reconnect (pid %d -> %d) the old event can never pair: no pickup lines for it (%s)" %
              (gpid, g2.pid, events(t, g2.pid)), events(t, g2.pid) == [] and events(t, gpid, gseq) == [])
        g2.stream(1.0)
        check("P6 G2 left gracefully before the P7 snapshot probe (host teardown line seen)", leave_peer(host, g2))
        peers.remove(g2)

        # ---- P7: no world mutation ----
        L.pump_sleep(0.8, obs.c.hub)
        ups = [L.field_update_tuple(g) for g in obs.c.field_updates_since(mark_b)]
        by_tile = {}
        for u in ups:
            by_tile.setdefault((u[0], u[1]), []).append(u[2])
        print("INFO - P7 observer saw %d FIELD_UPDATE(s) on %d tile(s); committed %s" % (len(ups), len(by_tile), committed))
        check("P7 exactly one FIELD_UPDATE (value EMPTY_NO) per committed pickup tile (%d tiles)" % len(committed),
              all(by_tile.get(t) == [L.EMPTY_NO] for t in committed))
        check("P7 no FIELD_UPDATE for the aborted tile and for any other fixture tile",
              all(t in committed for t in by_tile if t in tiles) and t2 not in by_tile)
        probe_tiles = committed + [t2]
        after = L.read_tiles_via_snapshot("127.0.0.1", port, probe_tiles)
        check("P7 a fresh authoritative snapshot: committed tiles EMPTY, the aborted tile still holds its item",
              all(after[t] == L.EMPTY_NO for t in committed) and after[t2] == before[t2])
        clean(host, check, "P")
        check("P host still alive at the end", host.alive())
    finally:
        for p in peers:
            p.close()
        host.stop()


def s2(port, log_dir, check):
    print("=" * 72 + "\n[S2] HOOK-DRIVEN: real client process receives the relayed PLAYER_ACTION")
    host = boot_host(port, ["--authoritative-wildlife", "--pickup-test-seed"], "pickup_s2", log_dir)
    if host is None:
        check("S2 host reached field", False)
        return
    client = boot_client(port, ["--authoritative-wildlife"], "pickup_s2", log_dir)
    a = None
    try:
        if client is None:
            check("S2 client reached field", False)
            return
        time.sleep(2.0)
        a = Peer(port, "A")
        a.stream(2.0)
        obs = a
        before = {t: a.c.world.tile_ut(*t) for t in L.TOWN_FIXTURE_TILES}
        occupied = [t for t in L.TOWN_FIXTURE_TILES if before[t] not in (None, L.EMPTY_NO)]
        check("S2 fixture tiles visible", len(occupied) >= 2)
        tile = occupied[0]
        rid, res = a.hold_pickup(tile)
        off_c = len(client.log_text())
        off_h = len(host.log_text())
        a.stream(0.6, at=0.0, action=lambda: a.commit(rid))
        a.stream(1.8, IDX_PICKUP, 1)
        ht = host.log_text()[off_h:]
        ct = client.log_text()[off_c:]
        am = ACT_RX.findall(ht)
        check("S2 host emitted once, relayed to the real client (and not to A) (%s)" % (am,),
              len(am) == 1 and int(am[0][6]) == 1)
        seq = int(am[0][2]) if am else -1
        ev = events(ct, a.pid, seq)
        print("INFO - S2 client puppet events for A: %s" % [(e[5], e[6]) for e in ev])
        check("S2 the real client queued the event about A (state=pending, tile/item as committed) (%s)" % ev,
              len(ev) >= 1 and ev[0][5] == "pending" and (int(ev[0][2]), int(ev[0][3])) == tile and
              int(ev[0][4], 16) == before[tile])
        check("S2 the client then paired / gated it with A's pickup row (a state line other than pending)",
              len(ev) >= 2 and ev[1][5] in ("started", "late", "ignored", "dropped"))
        check("S2 A (the originator) received no PLAYER_ACTION", a.c.player_actions_since(0) == [])
        L.pump_sleep(1.0, a.c.hub)
        clean(host, check, "S2 host")
        check("S2 client alive", client.alive())
        m = re.search(r"Segmentation|[Aa]ssert|Unhandled|ACCESS_VIOLATION|access violation", client.log_text())
        check("S2 no crash text in the client log", m is None)
    finally:
        if a is not None:
            a.close()
        if client is not None:
            client.stop()
        host.stop()


def main():
    L.require_test_bin_dir()  # review M1: refuse to run against the live build/save
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8300)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    os.environ["PC_PUPPET_DIAG"] = "1"  # net_spike_lib copies os.environ into the launched game processes
    results = []
    log_dir = os.path.join(HERE, "logs", "m9c")
    os.makedirs(log_dir, exist_ok=True)
    check = lambda desc, cond: L.check(desc, cond, results)
    if not args.only or args.only == "a0":
        a0(check)
    if not args.only or args.only in ("p", "p1"):
        p_all(args.port, log_dir, check)
    if args.only == "s2":
        s2(args.port + 20, log_dir, check)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
