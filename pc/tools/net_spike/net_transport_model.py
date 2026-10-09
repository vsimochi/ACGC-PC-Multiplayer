#!/usr/bin/env python3
"""STALE NOTE (X1b): this model host pre-dates D3 (RECORD_HELLO) and X1 (TXN_COMMIT 51 / TXN_RESULT 52): it does not implement them, and
it has been unrunnable since before the campaign (test_lib_loopback.py crashes in tick() with struct.error packing SNAPSHOT_END_FMT: the
model never followed the v2 snapshot format). Its CONFIRM(COMMIT) semantics are therefore also outdated: the real host RETIRES a legacy
COMMIT and commits only on TXN_COMMIT (see test_txn_protocol.py, which runs against the REAL host). It was deliberately NOT patched blind
(a model change that cannot be run proves nothing); fixing it is an open item.
net_transport_model.py - Python reference model of the HOST side of the new transport
(foundation contract section 1) plus a tiny v1 game layer, for testing the test harness itself
without the game.

Why this exists: the real host (AnimalCrossing.exe --host) cannot be built/launched in phase 1, but
the fake-client library (net_spike_lib.py), its fault injection, and the real-host test scripts
(test_transport_reliability.py, test_reconnect_state.py) all need to be proven non-vacuous before
they are pointed at the real thing. This model implements the contract's host rules
(HELLO nonce / restart semantics, DISCONNECT frees the slot, per-peer seq from 0, cumulative ACK +
32-bit SACK coalesced once per pump, in-order dedup delivery with a 64-entry window, RTO with
doubling backoff and a retransmit budget ending in DISCONNECT, 5 s timeout, 500 ms heartbeat,
legacy reliable DATA dropped), mirroring Workstream A's pc_net.c draft parameters.

The game layer is a protocol-v2 subset of pc_net_game.c: IDENTITY (version at offset 4 checked
first -> 8-byte REJECT(PROTOCOL_MISMATCH); has_save=0 -> 24-byte REJECT(NO_SAVE); town (land_name,
land_id, terrain_hash) != MODEL_HOST_TOWN -> 24-byte REJECT(LAND_MISMATCH); each followed by a
DISCONNECT) -> IDENTITY_ACK + roster (host APPEARANCE plus newcomer backfill) -> SNAPSHOT_BEGIN /
30 FIELD_BLOCK / SNAPSHOT_END (4 per tick while backlog < 32); PLAYER_CONTEXT gating pickup/drop on
IN_TOWN; RESYNC_REQUEST -> new epoch; APPEARANCE relay; MOVE ingestion (finite/limit validation, stale-frame
rejection, unreliable relay to the other READY peers); PICKUP/DROP as the TWO-PHASE reserve -> confirm -> commit
protocol of the hardening contract (INTERACT_CONFIRM type 17: per-peer single-slot cache with phases PENDING /
DONE / ABORTED / EXPIRED, a cross-peer reservation table, new-request-replaces-pending, release on disconnect,
expiry after --confirm-timeout seconds, fail-closed reach, |speed| <= 1000); FIELD_UPDATE v2 broadcast with a global world_seq
(sent only on COMMIT); only in-town ut tiles are addressable. It does NOT model facing / item-class validation,
transients, the dirty flush / live blocks, WORLD_META or the ~3 s appearance resend. A nonce-less HELLO is
refused with DISCONNECT.

Deliberately BUGGY modes (each proves a test is not vacuous):
  --buggy-stale-cache   keeps the per-peer caches (incl. a PENDING reservation) across disconnect/READY
                        (the bug test_reconnect_state.py / INV-C(v) must catch).
  --buggy-single-phase  the OLD behaviour: mutates the field at REQUEST time, ignores INTERACT_CONFIRM, stores any
                        MOVE position (NaN/Inf/absurd) and checks reach in the fail-open `d2 > MAX or |dy| > MAX_Y`
                        form (test_inventory_correctness.py must FAIL against it).

Standalone:  python net_transport_model.py --port 7799 [--duration 120] [--buggy-stale-cache]
             [--buggy-single-phase] [--seed-fixture] [--timeout 5.0] [--confirm-timeout 20.0]
"""
import argparse
import math
import socket
import struct
import sys
import time
from collections import Counter

import net_spike_lib as L


class _Slot:
    __slots__ = ("addr", "nonce", "sender", "receiver", "last_recv", "last_send", "conn_id")

    def __init__(self, addr, nonce, now, rto_min, rto_max, window, conn_id):
        self.addr = addr
        self.nonce = nonce
        self.sender = L.ReliableSender(rto_min, rto_max, backoff=2.0)
        self.receiver = L.ReliableReceiver(window)
        self.last_recv = now
        self.last_send = 0.0
        self.conn_id = conn_id


class ModelHost:
    def __init__(self, port=0, bind_ip="127.0.0.1", rto_min=L.HOST_RTO_MIN_S, rto_max=L.HOST_RTO_MAX_S,
                 max_retransmits=L.HOST_MAX_RETRANSMITS, timeout=L.PCNET_TIMEOUT_S,
                 heartbeat=L.PCNET_HEARTBEAT_INTERVAL_S, max_peers=L.PC_NET_MAX_PEERS,
                 window=L.PC_NET_RELIABLE_WINDOW, app=None, hub=None, log=None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            try:
                self.sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except (OSError, ValueError):
                pass
        self.sock.bind((bind_ip, port))
        self.port = self.sock.getsockname()[1]
        self.rto_min, self.rto_max = rto_min, rto_max
        self.max_retransmits = max_retransmits
        self.timeout, self.heartbeat = timeout, heartbeat
        self.window = window
        self.slots = [None] * max_peers
        self.events = []          # (t, "CONNECTED"/"DISCONNECTED", slot, reason)
        self.delivered = []       # (slot, conn_id, seq, payload) reliable, in delivery order
        self.stats = Counter()
        self.app = app
        self.log = log
        self._conn_counter = 0
        self.hub = hub
        if hub is not None:
            hub.register(self)

    # --- helpers --------------------------------------------------------------------------------

    def _say(self, text):
        if self.log:
            self.log(text)

    def _tx(self, addr, pkt):
        try:
            self.sock.sendto(pkt, addr)
        except OSError:
            self.stats["tx_error"] += 1
            return False
        return True

    def _find(self, addr):
        for i, s in enumerate(self.slots):
            if s is not None and s.addr == addr:
                return i
        return None

    def _open(self, i, addr, nonce, now):
        self._conn_counter += 1
        self.slots[i] = _Slot(addr, nonce, now, self.rto_min, self.rto_max, self.window, self._conn_counter)
        self.events.append((now, "CONNECTED", i, "hello"))
        self._say(f"[MODEL] peer {i} transport-connected (nonce={nonce:#x})")
        if self.app:
            self.app.on_connected(self, i)

    def _free(self, i, now, reason, notify=True):
        self.slots[i] = None
        if notify:
            self.events.append((now, "DISCONNECTED", i, reason))
            self._say(f"[MODEL] peer {i} disconnected ({reason})")
            if self.app:
                self.app.on_disconnected(self, i)

    def peer_count(self):
        return sum(1 for s in self.slots if s is not None)

    def conn_id(self, i):
        s = self.slots[i]
        return None if s is None else s.conn_id

    # --- API used by the game layer -----------------------------------------------------------------

    def send_reliable(self, i, payload):
        s = self.slots[i] if 0 <= i < len(self.slots) else None
        if s is None or s.sender.backlog() >= self.window:
            return None
        seq = s.sender.alloc(payload)
        self._transmit(s, seq)
        return seq

    def _transmit(self, s, seq):
        e = s.sender.unacked[seq]
        if self._tx(s.addr, L.encode_rdata(seq, e.payload)):
            s.last_send = time.monotonic()
        s.sender.mark_tx(seq, time.monotonic())

    def send_unreliable(self, i, payload):
        s = self.slots[i]
        if s is not None and self._tx(s.addr, L.encode_data(payload)):
            s.last_send = time.monotonic()

    def disconnect(self, i):
        """pc_net_disconnect(): DISCONNECT notice + free; no local event (matches pc_net.h)."""
        s = self.slots[i]
        if s is None:
            return
        self._tx(s.addr, L.encode_ctrl(L.PCNET_WIRE_DISCONNECT))
        self._free(i, time.monotonic(), "local", notify=False)
        if self.app:
            self.app.on_local_disconnect(self, i)

    # --- pump ---------------------------------------------------------------------------------------

    def pump(self, now=None):
        if now is None:
            now = time.monotonic()
        while True:
            try:
                data, frm = self.sock.recvfrom(65536)
            except BlockingIOError:
                break
            except ConnectionResetError:
                continue
            except OSError as e:
                if getattr(e, "winerror", None) == 10035:
                    break
                if getattr(e, "winerror", None) in (10054, 10040):
                    continue
                raise
            self._handle(frm, data, now)
        for i, s in enumerate(self.slots):
            if s is None:
                continue
            if now - s.last_recv > self.timeout:
                self._free(i, now, "timeout")
                continue
            dead = False
            sent = 0
            for seq in s.sender.due(now):
                e = s.sender.unacked[seq]
                if e.tx_count - 1 >= self.max_retransmits:
                    dead = True
                    break
                if sent >= 8:
                    continue
                self._transmit(s, seq)
                self.stats["retransmits"] += 1
                sent += 1
            if dead:
                self.stats["budget_disconnects"] += 1
                self._tx(s.addr, L.encode_ctrl(L.PCNET_WIRE_DISCONNECT))
                self._free(i, now, "retransmit budget")
                continue
            if s.receiver.ack_pending:
                ne, sack = s.receiver.build_ack()
                s.receiver.ack_pending = False
                if self._tx(s.addr, L.encode_ack(ne, sack)):
                    s.last_send = now
                    self.stats["acks_sent"] += 1
            if now - s.last_send > self.heartbeat:
                if self._tx(s.addr, L.encode_ctrl(L.PCNET_WIRE_HEARTBEAT)):
                    s.last_send = now
        if self.app:
            self.app.tick(self, now)

    def _handle(self, frm, data, now):
        pkt = L.decode_packet(data)
        if pkt is None:
            self.stats["malformed"] += 1
            return
        i = self._find(frm)
        t = pkt.wtype
        if t == L.PCNET_WIRE_HELLO:
            if pkt.nonce is None:
                # nonce-less (size < 4) legacy HELLO: refused with a DISCONNECT, no slot, no HELLO_ACK
                # (Workstream A's legacy-peer fix). A size-4 HELLO with nonce 0 is still accepted.
                self.stats["legacy_hello_refused"] += 1
                self._tx(frm, L.encode_ctrl(L.PCNET_WIRE_DISCONNECT))
                return
            nonce = pkt.nonce
            if i is None:
                free = [k for k, s in enumerate(self.slots) if s is None]
                if not free:
                    return
                self._open(free[0], frm, nonce, now)
            elif self.slots[i].nonce != nonce:
                self.stats["nonce_restarts"] += 1
                self._free(i, now, "nonce restart")
                self._open(i, frm, nonce, now)
            else:
                self.slots[i].last_recv = now
            self._tx(frm, L.encode_hello(pkt.nonce, L.PCNET_WIRE_HELLO_ACK))
            return
        if i is None:
            self.stats["unknown_peer_" + L.WIRE_TYPE_NAMES.get(t, "x")] += 1
            return
        s = self.slots[i]
        s.last_recv = now
        if t == L.PCNET_WIRE_DISCONNECT:
            self._free(i, now, "goodbye")
        elif t == L.PCNET_WIRE_ACK:
            if L.seq_diff(pkt.ack_next, s.sender.next_seq) > 0:
                self.stats["acks_invalid"] += 1
                return
            s.sender.on_ack(pkt.ack_next, pkt.ack_sack)
        elif t == L.PCNET_WIRE_RDATA:
            if pkt.kind != L.PC_NET_RELIABLE:
                return
            status, out = s.receiver.on_rdata(pkt.seq, pkt.payload)
            self.stats["rdata_" + status] += 1
            for seq, payload in out:
                self.delivered.append((i, s.conn_id, seq, payload))
                if self.app and self.slots[i] is s:
                    self.app.on_reliable(self, i, payload)
        elif t == L.PCNET_WIRE_DATA:
            if pkt.kind != L.PC_NET_UNRELIABLE:
                self.stats["legacy_reliable_dropped"] += 1
                return
            if self.app:
                self.app.on_unreliable(self, i, pkt.payload)


class NullApp:
    def on_connected(self, host, i): pass
    def on_disconnected(self, host, i): pass
    def on_local_disconnect(self, host, i): pass
    def on_reliable(self, host, i, payload): pass
    def on_unreliable(self, host, i, payload): pass
    def tick(self, host, now): pass


class EchoApp(NullApp):
    """Echoes every reliable payload back reliably; records deliveries per slot."""

    def __init__(self):
        self.got = []

    def on_reliable(self, host, i, payload):
        self.got.append((i, payload))
        host.send_reliable(i, b"ECHO:" + payload)


MODEL_HOST_TOWN = L.TownIdentity(b"MODELTWN", 0x1234, 0x0DDBA11)
MODEL_HOST_PLAYER = L.PlayerIdentity(b"HOSTPLYR", 0x4321, 1)
MODEL_APPLE = 0x2800


PH_PENDING = "PENDING"    # reservation held, field NOT mutated, waiting for the requester's CONFIRM
PH_DONE = "DONE"          # committed (or a final rejection): replay the cached RESULT on a retry
PH_ABORTED = "ABORTED"    # released by ABORT / replacement: a retry of this request_id is rejected
PH_EXPIRED = "EXPIRED"    # released by the confirm timeout: a retry of this request_id is rejected

PICKUP_REACH_SQ = 50.0 * 50.0   # PC_NETGAME_PICKUP_MAX_REACH_SQ
PICKUP_REACH_Y = 40.0           # PC_NETGAME_PICKUP_MAX_REACH_Y


class _Inter:
    """One peer's most recent pickup or drop request (the host's single-slot dedup cache entry, gaining a
    phase in the two-phase protocol)."""
    __slots__ = ("rid", "phase", "tile", "item", "t0", "result")

    def __init__(self, rid, phase, tile, item, t0, result):
        self.rid, self.phase, self.tile, self.item, self.t0, self.result = rid, phase, tile, item, t0, result


def _pos_valid(x, y, z):
    """pcnetgame_pos_valid(): finite and |coord| <= PC_NETGAME_POS_ABS_LIMIT on all three axes."""
    return all(math.isfinite(v) and abs(v) <= L.PC_NETGAME_POS_ABS_LIMIT for v in (x, y, z))


SPEED_ABS_LIMIT = 1000.0  # a MOVE with a non-finite or |speed| > 1000 sample is dropped at ingest


class ModelGameApp(NullApp):
    """v2 subset of pc_net_game.c's host logic (see module doc).

    Pickup/drop are TWO-PHASE (reserve -> confirm -> commit, hardening contract):
      REQUEST  validate (in-town context, town tile, item/empty, NOT reserved by any peer, finite position,
               reach) -> reserve, cache PENDING, RESULT(accepted=1) [provisional]; the world is NOT touched.
               A rejected request answers RESULT(accepted=0) with no reservation.
      retry    (same request_id) PENDING/DONE -> replay the cached RESULT (the timer is NOT extended);
               ABORTED/EXPIRED -> RESULT(accepted=0), never re-granted.
      new rid  from a peer with a pending interaction OF THE SAME KIND implicitly ABORTS the old one first (one
               pending per kind per peer; a pending pickup and a pending drop coexist and are independent); the
               immediate predecessor id is remembered per kind (a retry of exactly that id gets accepted=0).
      CONFIRM  acts only for (READY peer, kind matches, request_id == that peer's PENDING request): COMMIT
               re-checks the tile then mutates + broadcasts FIELD_UPDATE (phase DONE); ABORT releases. Anything
               else is stale and ignored.
      release  on COMMIT/ABORT/replacement, peer disconnect/reset, and expiry (`confirm_timeout` seconds,
               evaluated every tick). Expiry/abort never mutate the world.
    buggy_single_phase reproduces the OLD host: it mutates the world at REQUEST time, ignores CONFIRM, stores any
    MOVE position (NaN/Inf included) and evaluates reach in the old fail-open form
    `if d2 > MAX or |dy| > MAX_Y: reject` (false for NaN)."""

    def __init__(self, buggy_stale_cache=False, seed_fixture=False, buggy_single_phase=False,
                 confirm_timeout=L.PC_NETGAME_CONFIRM_TIMEOUT_MS / 1000.0):
        self.buggy = buggy_stale_cache
        self.single_phase = buggy_single_phase
        self.confirm_timeout = confirm_timeout
        self.ready = set()
        self.ctx = {}           # slot -> PLAYER_CONTEXT flags
        self.snap = {}          # slot -> {"epoch", "stage", "next"}
        self.epoch_counter = 0
        self.world_seq = 0
        self.last_tick = 0.0
        self.pickup_cache = {}  # slot -> _Inter (kind 1)
        self.drop_cache = {}    # slot -> _Inter (kind 2)
        self.pos = {}           # slot -> (x, y, z, frame) last ACCEPTED MOVE sample
        self.replaced = {}      # (slot, kind) -> request_id of the pending request a newer same-kind request replaced
        self.stats = Counter()
        self.appearance = {}    # slot -> last APPEARANCE payload (net_player_id rewritten to slot)
        self.world = {}         # town ut (x, z) -> item
        if seed_fixture:
            for t in L.TOWN_FIXTURE_TILES:  # the real host only places the 20 in-town tiles
                self.world[t] = MODEL_APPLE

    def _reset(self, i):
        self.ctx.pop(i, None)
        self.snap.pop(i, None)
        self.pos.pop(i, None)
        self.replaced.pop((i, L.CONFIRM_KIND_PICKUP), None)
        self.replaced.pop((i, L.CONFIRM_KIND_DROP), None)
        if not self.buggy:
            self.pickup_cache.pop(i, None)  # also releases any reservation the peer held
            self.drop_cache.pop(i, None)

    def on_disconnected(self, host, i):
        self.ready.discard(i)
        self.appearance.pop(i, None)
        self._reset(i)

    on_local_disconnect = on_disconnected

    def _reject_town(self, host, i, reason):
        host.send_reliable(i, struct.pack(L.REJECT_TOWN_FMT, L.PC_NETGAME_MSG_REJECT, reason, 0,
                                          L.PC_NETGAME_PROTOCOL_VERSION, MODEL_HOST_TOWN.land_name,
                                          MODEL_HOST_TOWN.land_id, 0, MODEL_HOST_TOWN.terrain_hash))
        host.disconnect(i)

    def _start_snapshot(self, i):
        self.epoch_counter += 1
        self.snap[i] = {"epoch": self.epoch_counter, "stage": 0, "next": 0}

    def _block(self, acre, flags, epoch):
        items = [0] * L.TILE_NUM
        for (x, z), v in self.world.items():
            at = L.town_ut_to_acre_tile(x, z)
            if at is not None and at[0] == acre:
                items[at[1]] = v
        hdr = struct.pack(L.FIELD_BLOCK_HDR_FMT, L.PC_NETGAME_MSG_FIELD_BLOCK, 0, acre, flags, epoch, self.world_seq)
        return hdr + struct.pack("<256H16H16H", *(items + [0] * 16 + [0xFFFF] * 16))

    def _expire(self, now):
        for cache in (self.pickup_cache, self.drop_cache):
            for it in cache.values():
                if it.phase == PH_PENDING and now - it.t0 > self.confirm_timeout:
                    it.phase = PH_EXPIRED
                    self.stats["expired"] += 1

    def tick(self, host, now):
        self._expire(now)  # every poll, not rate-limited
        if now - self.last_tick < 1.0 / 60.0:  # the real host pumps snapshots once per game frame
            return
        self.last_tick = now
        for i in sorted(self.snap):
            st = self.snap[i]
            sent = 0
            while i in self.snap and sent < 4 and host.slots[i] is not None and host.slots[i].sender.backlog() < 32:
                if st["stage"] == 0:
                    host.send_reliable(i, struct.pack(L.SNAPSHOT_BEGIN_FMT, L.PC_NETGAME_MSG_SNAPSHOT_BEGIN, 0,
                                                      L.ACRE_NUM, 0, st["epoch"], self.world_seq))
                    st["stage"] = 1
                elif st["stage"] == 1:
                    host.send_reliable(i, self._block(st["next"], L.PC_NETGAME_FB_FLAG_IN_SNAPSHOT, st["epoch"]))
                    st["next"] += 1
                    if st["next"] >= L.ACRE_NUM:
                        st["stage"] = 2
                else:
                    host.send_reliable(i, struct.pack(L.SNAPSHOT_END_FMT, L.PC_NETGAME_MSG_SNAPSHOT_END, 0, L.ACRE_NUM,
                                                      L.PC_NETGAME_META_FLAG_RENEW_TIME_VALID, st["epoch"],
                                                      self.world_seq, 0, 0, 0, 1, 0, 1, 2026))
                    del self.snap[i]
                sent += 1

    # --- MOVE ---------------------------------------------------------------------------------------

    def on_unreliable(self, host, i, payload):
        if i in self.ready and L.MOVE_SPEC.matches(payload):
            self._on_move(host, i, payload)

    def _on_move(self, host, i, payload):
        g = L.MOVE_SPEC.decode(payload)
        if not self.single_phase and not (_pos_valid(g.pos_x, g.pos_y, g.pos_z) and math.isfinite(g.speed)
                                          and abs(g.speed) <= SPEED_ABS_LIMIT):
            self.stats["move_dropped_invalid"] += 1  # dropped BEFORE storage and before the relay
            return
        last = self.pos.get(i)
        if last is not None and L.seq_diff(g.frame, last[3]) <= 0:
            self.stats["move_dropped_stale"] += 1    # stale/duplicate/reordered
            return
        self.pos[i] = (g.pos_x, g.pos_y, g.pos_z, g.frame)
        relayed = bytes([payload[0], i]) + payload[2:]  # net_player_id = the true originating slot
        for k in sorted(self.ready):
            if k != i:
                host.send_unreliable(k, relayed)

    # --- reservations ---------------------------------------------------------------------------------

    def _reserved_tiles(self):
        return {it.tile for cache in (self.pickup_cache, self.drop_cache) for it in cache.values()
                if it.phase == PH_PENDING}

    def _abort_pending(self, i, kind):
        """A NEW request from peer i aborts that peer's pending interaction of the SAME kind only (one pending per
        kind per peer; a pending pickup and a pending drop coexist and are independent)."""
        cache = self.pickup_cache if kind == L.CONFIRM_KIND_PICKUP else self.drop_cache
        it = cache.get(i)
        if it is not None and it.phase == PH_PENDING:
            it.phase = PH_ABORTED
            self.replaced[(i, kind)] = it.rid  # the immediate predecessor id (per kind) is remembered
            self.stats["replaced"] += 1

    def _reach_ok(self, i, cx, cz):
        """Pickup reach against the peer's last ACCEPTED position. center.y is seeded from the peer's own y,
        exactly like the real host (so dy is 0 -- or NaN when y is inf)."""
        p = self.pos.get(i)
        if p is None:
            return False  # no movement sample from this peer yet
        px, py, pz = p[0], p[1], p[2]
        dx, dz, dy = cx - px, cz - pz, py - py
        d2 = dx * dx + dz * dz
        if self.single_phase:
            return not (d2 > PICKUP_REACH_SQ or abs(dy) > PICKUP_REACH_Y)  # old fail-open form (NaN passes)
        return d2 <= PICKUP_REACH_SQ and abs(dy) <= PICKUP_REACH_Y         # fail-closed form

    def _drop_reach_ok(self, i, tile):
        """Drop: the target must be the tile the requester stands on (the model's stand-in for the real host's
        vanilla drop-tile search) AND pass the same reach envelope."""
        cx = tile[0] * L.UNIT_SIZE + L.UNIT_SIZE / 2.0
        cz = tile[1] * L.UNIT_SIZE + L.UNIT_SIZE / 2.0
        if not self._reach_ok(i, cx, cz):
            return False
        px, pz = self.pos[i][0], self.pos[i][2]
        if math.isnan(px) or math.isnan(pz):
            return True  # old behaviour: an undefined position makes the tile lookup / comparison fall through
        return (math.floor(px / L.UNIT_SIZE), math.floor(pz / L.UNIT_SIZE)) == tile

    # --- pickup / drop / confirm --------------------------------------------------------------------------

    def _result(self, kind, ok, g, item):
        fmt = L.PICKUP_RESULT_FMT if kind == L.CONFIRM_KIND_PICKUP else L.DROP_RESULT_FMT
        mt = L.PC_NETGAME_MSG_PICKUP_RESULT if kind == L.CONFIRM_KIND_PICKUP else L.PC_NETGAME_MSG_DROP_RESULT
        return struct.pack(fmt, mt, int(ok), g.ut_x, g.ut_z, g.request_id, item if ok else L.EMPTY_NO, 0)

    def _on_pickup_request(self, host, i, g, in_town):
        kind = L.CONFIRM_KIND_PICKUP
        c = self.pickup_cache.get(i)
        if c is not None and c.rid == g.request_id:
            if c.phase in (PH_PENDING, PH_DONE):
                host.send_reliable(i, c.result)          # replay; a PENDING retry does NOT extend the timer
            else:
                host.send_reliable(i, self._result(kind, False, g, L.EMPTY_NO))  # ABORTED/EXPIRED: never re-grant
            return
        if self.replaced.get((i, kind)) == g.request_id:  # retry of exactly the replaced request: rejected
            host.send_reliable(i, self._result(kind, False, g, L.EMPTY_NO))
            return
        self._abort_pending(i, kind)
        tile = (g.ut_x, g.ut_z)
        item = self.world.get(tile, L.EMPTY_NO)
        cx, cz = tile[0] * L.UNIT_SIZE + L.UNIT_SIZE / 2.0, tile[1] * L.UNIT_SIZE + L.UNIT_SIZE / 2.0
        ok = (in_town and L.town_ut_to_acre_tile(*tile) is not None and item != L.EMPTY_NO
              and (self.single_phase or tile not in self._reserved_tiles()) and self._reach_ok(i, cx, cz))
        out = self._result(kind, ok, g, item)
        if not ok:
            self.pickup_cache[i] = _Inter(g.request_id, PH_DONE, tile, L.EMPTY_NO, 0.0, out)
            host.send_reliable(i, out)
        elif self.single_phase:  # OLD behaviour: mutate at REQUEST time
            self.world[tile] = L.EMPTY_NO
            self.pickup_cache[i] = _Inter(g.request_id, PH_DONE, tile, item, 0.0, out)
            host.send_reliable(i, out)
            self._broadcast_field_update(host, g.ut_x, g.ut_z, L.EMPTY_NO)
        else:  # reserve; the field stays untouched until CONFIRM(COMMIT)
            self.pickup_cache[i] = _Inter(g.request_id, PH_PENDING, tile, item, time.monotonic(), out)
            host.send_reliable(i, out)

    def _on_drop_request(self, host, i, g, in_town):
        kind = L.CONFIRM_KIND_DROP
        c = self.drop_cache.get(i)
        if c is not None and c.rid == g.request_id:
            if c.phase in (PH_PENDING, PH_DONE):
                host.send_reliable(i, c.result)
            else:
                host.send_reliable(i, self._result(kind, False, g, L.EMPTY_NO))
            return
        if self.replaced.get((i, kind)) == g.request_id:
            host.send_reliable(i, self._result(kind, False, g, L.EMPTY_NO))
            return
        self._abort_pending(i, kind)
        tile = (g.ut_x, g.ut_z)
        ok = (in_town and L.town_ut_to_acre_tile(*tile) is not None
              and self.world.get(tile, L.EMPTY_NO) == L.EMPTY_NO and g.claimed_item != 0
              and (self.single_phase or tile not in self._reserved_tiles()) and self._drop_reach_ok(i, tile))
        out = self._result(kind, ok, g, g.claimed_item)
        if not ok:
            self.drop_cache[i] = _Inter(g.request_id, PH_DONE, tile, L.EMPTY_NO, 0.0, out)
            host.send_reliable(i, out)
        elif self.single_phase:
            self.world[tile] = g.claimed_item
            self.drop_cache[i] = _Inter(g.request_id, PH_DONE, tile, g.claimed_item, 0.0, out)
            host.send_reliable(i, out)
            self._broadcast_field_update(host, g.ut_x, g.ut_z, g.claimed_item)
        else:
            self.drop_cache[i] = _Inter(g.request_id, PH_PENDING, tile, g.claimed_item, time.monotonic(), out)
            host.send_reliable(i, out)

    def _on_confirm(self, host, i, g):
        if self.single_phase:
            return  # OLD host: no such message
        cache = self.pickup_cache if g.kind == L.CONFIRM_KIND_PICKUP else \
            self.drop_cache if g.kind == L.CONFIRM_KIND_DROP else None
        it = None if cache is None else cache.get(i)
        if it is None or it.phase != PH_PENDING or it.rid != g.request_id:
            self.stats["confirm_stale"] += 1
            return  # stale: ignore, no mutation
        if g.outcome != L.CONFIRM_OUTCOME_COMMIT:
            it.phase = PH_ABORTED
            self.stats["aborted"] += 1
            return
        if g.kind == L.CONFIRM_KIND_PICKUP:
            if self.world.get(it.tile, L.EMPTY_NO) != it.item:  # impossible with reservations; release, no mutation
                it.phase = PH_ABORTED
                self.stats["commit_changed"] += 1
                return
            self.world[it.tile] = L.EMPTY_NO
            self._broadcast_field_update(host, it.tile[0], it.tile[1], L.EMPTY_NO)
        else:
            if self.world.get(it.tile, L.EMPTY_NO) != L.EMPTY_NO:
                it.phase = PH_ABORTED
                self.stats["commit_changed"] += 1
                return
            self.world[it.tile] = it.item
            self._broadcast_field_update(host, it.tile[0], it.tile[1], it.item)
        it.phase = PH_DONE
        self.stats["committed"] += 1

    def on_reliable(self, host, i, payload):
        if payload[:1] == bytes([L.PC_NETGAME_MSG_IDENTITY]) and len(payload) >= 8 and i not in self.ready:
            version = struct.unpack_from("<I", payload, 4)[0]  # offset 4 frozen across versions
            if version != L.PC_NETGAME_PROTOCOL_VERSION:
                host.send_reliable(i, struct.pack(L.REJECT_FMT, L.PC_NETGAME_MSG_REJECT,
                                                  L.PC_NETGAME_REJECT_PROTOCOL_MISMATCH, 0, L.PC_NETGAME_PROTOCOL_VERSION))
                host.disconnect(i)
                return
            g = L.decode_game(payload)
            if g is None:
                return
            if not g.has_save:
                self._reject_town(host, i, L.PC_NETGAME_REJECT_NO_SAVE)
                return
            if L.TownIdentity(bytes(g.land_name), g.land_id, g.terrain_hash) != MODEL_HOST_TOWN:
                self._reject_town(host, i, L.PC_NETGAME_REJECT_LAND_MISMATCH)
                return
            self._reset(i)
            ack = struct.pack(L.ACK_FMT, L.PC_NETGAME_MSG_IDENTITY_ACK, 1, i, L.PC_NETGAME_PROTOCOL_VERSION,
                              MODEL_HOST_PLAYER.player_name, MODEL_HOST_TOWN.land_name, MODEL_HOST_PLAYER.player_id,
                              MODEL_HOST_TOWN.land_id, MODEL_HOST_TOWN.terrain_hash)
            host.send_reliable(i, ack)
            self.ready.add(i)
            self.appearance.pop(i, None)
            appearance = struct.pack(L.APPEARANCE_HDR_FMT, L.PC_NETGAME_MSG_APPEARANCE, L.PC_NETGAME_HOST_PLAYER_ID,
                                     0, 1, 0x2400, 0, 0)
            host.send_reliable(i, appearance.ljust(L.APPEARANCE_MSG_SIZE, b"\x00"))
            for k in sorted(self.appearance):  # newcomer backfill (Stage 4C-1)
                if k != i and k in self.ready:
                    host.send_reliable(i, self.appearance[k])
            self._start_snapshot(i)
            return
        g = L.decode_game(payload)
        if g is None or i not in self.ready:
            return
        if g.msg_type == L.PC_NETGAME_MSG_MOVE:
            self._on_move(host, i, payload)
            return
        if g.msg_type == L.PC_NETGAME_MSG_PLAYER_CONTEXT:
            self.ctx[i] = g.flags
            return
        if g.msg_type == L.PC_NETGAME_MSG_RESYNC_REQUEST:
            self._start_snapshot(i)
            return
        if g.msg_type == L.PC_NETGAME_MSG_APPEARANCE:
            relayed = bytes([payload[0], i]) + payload[2:]  # net_player_id = sender's slot
            self.appearance[i] = relayed
            for k in sorted(self.ready):
                if k != i:
                    host.send_reliable(k, relayed)
            return
        in_town = bool(self.ctx.get(i, 0) & L.PC_NETGAME_CTX_FLAG_IN_TOWN)
        if g.msg_type == L.PC_NETGAME_MSG_PICKUP_REQUEST:
            self._on_pickup_request(host, i, g, in_town)
        elif g.msg_type == L.PC_NETGAME_MSG_DROP_REQUEST:
            self._on_drop_request(host, i, g, in_town)
        elif g.msg_type == L.PC_NETGAME_MSG_INTERACT_CONFIRM:
            self._on_confirm(host, i, g)

    def _broadcast_field_update(self, host, ut_x, ut_z, value):
        acre, tile = L.town_ut_to_acre_tile(ut_x, ut_z)
        self.world_seq += 1
        fu = struct.pack(L.FIELD_UPDATE_FMT, L.PC_NETGAME_MSG_FIELD_UPDATE, 0, acre, tile,
                         L.PC_NETGAME_FU_FLAG_DEPOSIT_VALID, 0, value, self.world_seq)
        for k in sorted(self.ready):
            host.send_reliable(k, fu)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--timeout", type=float, default=L.PCNET_TIMEOUT_S)
    ap.add_argument("--buggy-stale-cache", action="store_true")
    ap.add_argument("--buggy-single-phase", action="store_true")
    ap.add_argument("--confirm-timeout", type=float, default=L.PC_NETGAME_CONFIRM_TIMEOUT_MS / 1000.0,
                    help="reservation lifetime in seconds (loopback tests use a short one)")
    ap.add_argument("--seed-fixture", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    hub = L.Hub()
    app = ModelGameApp(buggy_stale_cache=a.buggy_stale_cache, seed_fixture=a.seed_fixture,
                       buggy_single_phase=a.buggy_single_phase, confirm_timeout=a.confirm_timeout)
    host = ModelHost(port=a.port, timeout=a.timeout, app=app, hub=hub,
                     log=None if a.quiet else (lambda s: print(s, flush=True)))
    print(f"[MODEL] hosting on UDP port {host.port}", flush=True)
    end = time.monotonic() + a.duration
    while time.monotonic() < end:
        hub.pump_once(0.005)
    print(f"[MODEL] exiting; stats={dict(host.stats)} game={dict(app.stats)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
