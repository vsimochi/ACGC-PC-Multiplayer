#!/usr/bin/env python3
"""net_spike_lib.py - Shared plumbing for every synthetic network test in pc/tools/net_spike/.

NOT a standalone script -- import it from a `test_*.py` harness. It is the single place that knows
the wire format, so a wire change in pc/src/pc_net.c (transport) or pc/src/pc_net_game.c (game
protocol) is mirrored here once.

Layers (top of file to bottom):

  1. Transport wire codec (foundation contract section 1): HELLO/HELLO_ACK with optional u32 nonce,
     HEARTBEAT, DISCONNECT, unreliable DATA(type 4, kind 0), reliable RDATA(type 5, kind 1,
     u32 seq), ACK(type 6, u32 next_expected + u32 sack_bits).
  2. Pure (socket-free) reliable-channel state: ReliableSender / ReliableReceiver. Unit-testable
     and reused by the Python model host (net_transport_model.py).
  3. Hub: single-threaded pump registry. Every wait in every test pumps EVERY registered
     endpoint, so idle bystander clients keep ACKing / heartbeating / receiving while another
     client is being driven (no more hand-placed send_heartbeat() calls needed for liveness).
  4. Inbox: buffers every decoded message; wait_for()/take()/collect() never discard unrelated
     messages; mark()/since() count only what arrived in a window.
  5. TransportClient: one fake-client connection speaking the new transport, with deterministic
     client-side FAULT INJECTION (see the "fault injection" method group).
  6. Game protocol v2 (pc_net_game.c / pc_net_game.h), verified against the C source: IDENTITY with
     town identity (land_name, land_id, terrain_hash), 8/24-byte REJECT, PLAYER_CONTEXT, SNAPSHOT_BEGIN /
     FIELD_BLOCK / SNAPSHOT_END, FIELD_UPDATE v2 (world_seq), WORLD_META, RESYNC_REQUEST.
  7. FakeClient(TransportClient): handshake (IDENTITY -> IDENTITY_ACK / REJECT), movement,
     pickup/drop request/result matched strictly by request_id, FIELD_UPDATE, appearance.
     Pickup/drop are TWO-PHASE (reserve -> confirm -> commit): an accepted RESULT is provisional and the client
     answers it with INTERACT_CONFIRM (type 17). By default (auto_confirm=True) every accepted RESULT is answered
     with CONFIRM(COMMIT) the moment it is delivered, so single-phase-era tests work unchanged; auto_confirm=False
     (per call or per client) leaves the reservation pending, and confirm(kind, rid, outcome, reason) sends any
     CONFIRM explicitly (ABORT / stale / wrong-kind / colliding-id experiments).
  8. Town probe (probe-then-connect as one helper), field addressing, WorldView (snapshot/delta
     world_seq semantics, contract section 4).
  9. Fixtures, request-id counter, check()/summary helpers.
 10. HostProcess: optional launcher for the real game host (cwd = pc/build64/bin because game
     resources are CWD-relative).

Determinism: nothing in this module uses randomness. Nonces are derived from the client label and
its connect count; faults are keyed by sequence number or payload predicate, never by chance.
"""
from collections import Counter, namedtuple
import os
import re
import select
import socket
import struct
import subprocess
import time
import weakref
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))

# =================================================================================================
# 1. Transport wire codec (contract section 1 / pc_net.c header comment)
# =================================================================================================

PCNET_MAGIC = 0x41434E50  # 'ACNP'

PCNET_WIRE_HELLO = 0
PCNET_WIRE_HELLO_ACK = 1
PCNET_WIRE_HEARTBEAT = 2
PCNET_WIRE_DISCONNECT = 3
PCNET_WIRE_DATA = 4
PCNET_WIRE_RDATA = 5
PCNET_WIRE_ACK = 6

WIRE_TYPE_NAMES = {
    PCNET_WIRE_HELLO: "HELLO",
    PCNET_WIRE_HELLO_ACK: "HELLO_ACK",
    PCNET_WIRE_HEARTBEAT: "HEARTBEAT",
    PCNET_WIRE_DISCONNECT: "DISCONNECT",
    PCNET_WIRE_DATA: "DATA",
    PCNET_WIRE_RDATA: "RDATA",
    PCNET_WIRE_ACK: "ACK",
}

PC_NET_UNRELIABLE = 0
PC_NET_RELIABLE = 1

PC_NET_MAX_PEERS = 8
PC_NET_MAX_PAYLOAD = 1024
PC_NET_RELIABLE_WINDOW = 64  # pc_net.h; the fake client's own receive buffer is more generous

PCNET_HEARTBEAT_INTERVAL_S = 0.5
PCNET_TIMEOUT_S = 5.0
PCNET_SACK_BITS = 32

# Host retransmit policy as implemented by Workstream A's pc_net.c draft (RTO floor 150 ms,
# doubling, capped at 1000 ms, 10 retransmits then DISCONNECT => roughly 8-9 s). Tests use these
# only to size their waits; confirmed by A (final): 10 retransmits then DISCONNECT, ~9.05 s.
HOST_RTO_MIN_S = 0.150
HOST_RTO_MAX_S = 1.000
HOST_MAX_RETRANSMITS = 10
HOST_RETRY_BUDGET_WAIT_S = 15.0  # generous upper bound for "host gives up and disconnects"

WIRE_HDR_FMT = "<IBBH"
WIRE_HDR_SIZE = 8
SEQ_FMT = "<I"
ACK_BODY_FMT = "<II"
NONCE_FMT = "<I"

U32_MASK = 0xFFFFFFFF

Packet = namedtuple("Packet", "wtype kind size seq payload nonce ack_next ack_sack")


def encode_packet(wtype, kind=0, body=b"", size=None):
    """Raw header + body. `size` defaults to len(body); RDATA callers pass the payload-only size."""
    if size is None:
        size = len(body)
    return struct.pack(WIRE_HDR_FMT, PCNET_MAGIC, wtype, kind, size) + body


def encode_hello(nonce, wtype=PCNET_WIRE_HELLO):
    """nonce=None -> legacy size-0 HELLO (== nonce 0 on the host)."""
    if nonce is None:
        return encode_packet(wtype, 0, b"")
    return encode_packet(wtype, 0, struct.pack(NONCE_FMT, nonce & U32_MASK))


def encode_data(payload, kind=PC_NET_UNRELIABLE):
    return encode_packet(PCNET_WIRE_DATA, kind, payload)


def encode_rdata(seq, payload):
    """header(type 5, kind 1, size = payload bytes ONLY) + u32 seq + payload."""
    return encode_packet(PCNET_WIRE_RDATA, PC_NET_RELIABLE, struct.pack(SEQ_FMT, seq & U32_MASK) + payload,
                         size=len(payload))


def encode_ack(next_expected, sack_bits):
    return encode_packet(PCNET_WIRE_ACK, 0, struct.pack(ACK_BODY_FMT, next_expected & U32_MASK, sack_bits & U32_MASK))


def encode_ctrl(wtype):
    return encode_packet(wtype, 0, b"")


def decode_packet(data):
    """Returns a Packet or None for anything malformed / not ours (mirrors pc_net.c's checks)."""
    if len(data) < WIRE_HDR_SIZE:
        return None
    magic, wtype, kind, size = struct.unpack_from(WIRE_HDR_FMT, data, 0)
    if magic != PCNET_MAGIC:
        return None
    body = data[WIRE_HDR_SIZE:]
    if wtype == PCNET_WIRE_RDATA:
        if size > PC_NET_MAX_PAYLOAD or len(body) < 4 + size:
            return None
        (seq,) = struct.unpack_from(SEQ_FMT, body, 0)
        return Packet(wtype, kind, size, seq, bytes(body[4:4 + size]), None, None, None)
    if wtype == PCNET_WIRE_ACK:
        if size < 8 or len(body) < 8:
            return None
        ne, sack = struct.unpack_from(ACK_BODY_FMT, body, 0)
        return Packet(wtype, kind, size, None, b"", None, ne, sack)
    if wtype in (PCNET_WIRE_HELLO, PCNET_WIRE_HELLO_ACK):
        nonce = None
        if size >= 4 and len(body) >= 4:
            (nonce,) = struct.unpack_from(NONCE_FMT, body, 0)
        return Packet(wtype, kind, size, None, b"", nonce, None, None)
    if wtype == PCNET_WIRE_DATA:
        if size > PC_NET_MAX_PAYLOAD or len(body) < size:
            return None
        return Packet(wtype, kind, size, None, bytes(body[:size]), None, None, None)
    return Packet(wtype, kind, size, None, bytes(body[:size]), None, None, None)


def send_hdr(sock, addr, wtype, kind, payload):
    """Legacy raw helper (kept for ad-hoc fault experiments). Prefer TransportClient methods."""
    sock.sendto(encode_packet(wtype, kind, payload), addr)


# --- u32 serial-number arithmetic ---------------------------------------------------------------

def seq_add(a, n):
    return (a + n) & U32_MASK


def seq_diff(a, b):
    """Signed distance a - b in u32 serial arithmetic."""
    d = (a - b) & U32_MASK
    return d - 0x100000000 if d >= 0x80000000 else d


def seq_lt(a, b):
    return seq_diff(a, b) < 0


# =================================================================================================
# 2. Pure reliable-channel state (no sockets)
# =================================================================================================

class _TxEntry:
    __slots__ = ("payload", "last_tx", "tx_count", "withheld", "rto")

    def __init__(self, payload, withheld, rto):
        self.payload = payload
        self.last_tx = None
        self.tx_count = 0
        self.withheld = withheld
        self.rto = rto


class ReliableSender:
    """Send half of one connection's reliable stream. seq starts at 0 per connection."""

    def __init__(self, rto=0.25, rto_max=None, backoff=1.0):
        self.next_seq = 0
        self.unacked = {}  # seq -> _TxEntry
        self.rto = rto
        self.rto_max = rto_max if rto_max is not None else rto
        self.backoff = backoff
        self.acked_total = 0
        self.invalid_acks = 0

    def alloc(self, payload, withheld=False):
        seq = self.next_seq
        self.next_seq = seq_add(seq, 1)
        self.unacked[seq] = _TxEntry(bytes(payload), withheld, self.rto)
        return seq

    def mark_tx(self, seq, now):
        e = self.unacked.get(seq)
        if e is None:
            return
        if e.tx_count > 0:
            e.rto = min(e.rto * self.backoff, self.rto_max)
        e.tx_count += 1
        e.last_tx = now
        e.withheld = False

    def on_ack(self, next_expected, sack_bits):
        """Returns the list of seqs newly acknowledged. An ACK that acknowledges seqs never sent is
        ignored (counted), mirroring pc_net.c's acks_invalid rule."""
        if seq_diff(next_expected, self.next_seq) > 0:
            self.invalid_acks += 1
            return []
        newly = []
        for seq in list(self.unacked):
            if seq_lt(seq, next_expected):
                newly.append(seq)
            else:
                bit = seq_diff(seq, next_expected) - 1
                if 0 <= bit < PCNET_SACK_BITS and (sack_bits >> bit) & 1:
                    newly.append(seq)
        for seq in newly:
            del self.unacked[seq]
        self.acked_total += len(newly)
        return sorted(newly, key=lambda s: seq_diff(s, next_expected))

    def due(self, now):
        """Transmitted-but-unacked, non-withheld seqs whose RTO has elapsed, oldest first."""
        out = []
        for seq, e in self.unacked.items():
            if e.withheld or e.tx_count == 0:
                continue
            if now - e.last_tx >= e.rto:
                out.append(seq)
        return sorted(out, key=lambda s: seq_diff(s, self.next_seq))

    def backlog(self):
        return len(self.unacked)

    def withheld(self):
        return sorted((s for s, e in self.unacked.items() if e.withheld), key=lambda s: seq_diff(s, self.next_seq))


class ReliableReceiver:
    """Receive half: in-order delivery, duplicate suppression, out-of-order buffering, ACK/SACK."""

    STATUS_NEW = "new"
    STATUS_DUP = "dup"
    STATUS_BUFFERED = "buffered"
    STATUS_TOO_FAR = "too_far"

    def __init__(self, max_ahead=1024):
        self.next_expected = 0
        self.buffer = {}
        self.max_ahead = max_ahead
        self.ack_pending = False
        self.delivered_seqs = []
        self.counts = Counter()

    def received(self, seq):
        return seq_lt(seq, self.next_expected) or seq in self.buffer

    def on_rdata(self, seq, payload):
        """Returns (status, [(seq, payload), ...] delivered in order by this arrival)."""
        self.ack_pending = True  # every RDATA, including duplicates, earns a (coalesced) ACK
        d = seq_diff(seq, self.next_expected)
        if d < 0:
            self.counts[self.STATUS_DUP] += 1
            return self.STATUS_DUP, []
        if d >= self.max_ahead:
            self.counts[self.STATUS_TOO_FAR] += 1
            return self.STATUS_TOO_FAR, []
        if d > 0:
            if seq in self.buffer:
                self.counts[self.STATUS_DUP] += 1
                return self.STATUS_DUP, []
            self.buffer[seq] = bytes(payload)
            self.counts[self.STATUS_BUFFERED] += 1
            return self.STATUS_BUFFERED, []
        out = [(seq, bytes(payload))]
        ne = seq_add(seq, 1)
        while ne in self.buffer:
            out.append((ne, self.buffer.pop(ne)))
            ne = seq_add(ne, 1)
        self.next_expected = ne
        self.delivered_seqs.extend(s for s, _ in out)
        self.counts[self.STATUS_NEW] += 1
        return self.STATUS_NEW, out

    def build_ack(self, pretend_missing=()):
        """(next_expected, sack_bits). `pretend_missing` (fault injection) makes the ACK report those
        received seqs as NOT received, so the sender must retransmit them."""
        pm = set(pretend_missing)
        rep = self.next_expected
        for p in pm:
            if seq_lt(p, rep) and self.received(p):
                rep = p
        sack = 0
        for i in range(PCNET_SACK_BITS):
            s = seq_add(rep, 1 + i)
            if s not in pm and self.received(s):
                sack |= 1 << i
        return rep, sack


# =================================================================================================
# 3. Hub: single-threaded pump registry
# =================================================================================================

class Hub:
    """Every member has .pump(now) and optionally .sock. All waits go through the hub so every
    registered endpoint keeps receiving/ACKing/heartbeating/retransmitting while any test code
    waits -- deterministic, single-threaded, no background threads."""

    def __init__(self):
        self._members = []

    def register(self, m):
        if m not in self._members:
            self._members.append(m)

    def unregister(self, m):
        if m in self._members:
            self._members.remove(m)

    def members(self):
        return list(self._members)

    def pump_all(self):
        now = time.monotonic()
        for m in list(self._members):
            m.pump(now)

    def pump_once(self, max_wait=0.005):
        socks = []
        for m in self._members:
            s = getattr(m, "sock", None)
            if s is not None:
                try:
                    if s.fileno() >= 0:
                        socks.append(s)
                except OSError:
                    pass
        if socks and max_wait > 0:
            try:
                select.select(socks, [], [], max_wait)
            except (OSError, ValueError):
                time.sleep(max_wait)
        elif max_wait > 0:
            time.sleep(max_wait)
        self.pump_all()

    def sleep(self, seconds):
        deadline = time.monotonic() + seconds
        self.pump_all()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.pump_once(min(0.01, remaining))

    def wait_until(self, cond, timeout, step=0.005):
        deadline = time.monotonic() + timeout
        self.pump_all()
        if cond():
            return True
        while time.monotonic() < deadline:
            self.pump_once(min(step, max(0.0, deadline - time.monotonic())))
            if cond():
                return True
        return bool(cond())


DEFAULT_HUB = Hub()


def pump_sleep(seconds, hub=None):
    """Drop-in replacement for time.sleep() in tests: keeps every endpoint's transport alive."""
    (hub or DEFAULT_HUB).sleep(seconds)


# =================================================================================================
# 4. Inbox
# =================================================================================================

CH_RELIABLE = "R"      # RDATA payload, delivered once, in order
CH_UNRELIABLE = "U"    # DATA kind 0
CH_LEGACY = "L"        # DATA kind 1 from the host -- must never happen any more (counted)
CH_TRANSPORT = "T"     # transport event (HELLO_ACK, DISCONNECT, ...), payload empty

_MsgBase = namedtuple("_MsgBase", "index t conn channel seq payload event")


class Msg(_MsgBase):
    __slots__ = ()

    @property
    def msg_type(self):
        return self.payload[0] if self.payload else None

    @property
    def game(self):
        if self.channel == CH_TRANSPORT or not self.payload:
            return None
        return decode_game(self.payload)

    @property
    def is_game(self):
        return self.channel in (CH_RELIABLE, CH_UNRELIABLE) and bool(self.payload)


class Inbox:
    """Buffers every decoded inbound message for one client. Nothing is ever dropped implicitly:
    wait_for()/take() remove only the message they return; everything else stays."""

    def __init__(self, hub):
        self.hub = hub
        self._items = []
        self._next_index = 0

    def push(self, t, conn, channel, seq, payload, event=None):
        m = Msg(self._next_index, t, conn, channel, seq, payload, event)
        self._next_index += 1
        self._items.append(m)
        return m

    def mark(self):
        """Index boundary: messages pushed after this call have index >= the returned value."""
        return self._next_index

    @staticmethod
    def _ok(m, pred, since):
        return (since is None or m.index >= since) and (pred is None or pred(m))

    def peek_all(self, pred=None, since=None):
        return [m for m in self._items if self._ok(m, pred, since)]

    def count(self, pred=None, since=None):
        return sum(1 for m in self._items if self._ok(m, pred, since))

    def take(self, pred=None, since=None):
        for i, m in enumerate(self._items):
            if self._ok(m, pred, since):
                del self._items[i]
                return m
        return None

    def take_all(self, pred=None, since=None):
        keep, out = [], []
        for m in self._items:
            (out if self._ok(m, pred, since) else keep).append(m)
        self._items = keep
        return out

    def wait_for(self, pred, timeout, consume=True, since=None):
        found = []

        def cond():
            for m in self._items:
                if self._ok(m, pred, since):
                    found.append(m)
                    return True
            return False

        if not self.hub.wait_until(cond, timeout):
            return None
        m = found[-1]
        if consume:
            self._items.remove(m)
        return m

    def collect(self, pred, duration, consume=True, since=None):
        """Pump for `duration`, then return every match (consumed by default)."""
        self.hub.sleep(duration)
        return self.take_all(pred, since) if consume else self.peek_all(pred, since)

    def clear(self):
        self._items = []

    def __len__(self):
        return len(self._items)


# --- predicate builders ---------------------------------------------------------------------------

def p_msg_type(msg_type, channels=(CH_RELIABLE, CH_UNRELIABLE)):
    return lambda m: m.channel in channels and m.msg_type == msg_type


def p_game(msg_type, channels=(CH_RELIABLE, CH_UNRELIABLE), **fields):
    """Matches a decoded game message of `msg_type` whose named fields equal `fields`."""

    def pred(m):
        if m.channel not in channels or m.msg_type != msg_type:
            return False
        g = m.game
        if g is None:
            return False
        return all(getattr(g, k, None) == v for k, v in fields.items())

    return pred


def p_event(name):
    return lambda m: m.channel == CH_TRANSPORT and m.event == name


# =================================================================================================
# 5. TransportClient (one fake-client connection) + deterministic fault injection
# =================================================================================================

class ConnectError(RuntimeError):
    pass


def _deterministic_nonce(label, connect_count):
    n = (zlib.crc32(label.encode("utf-8")) + 0x9E3779B1 * connect_count) & U32_MASK
    return n if n != 0 else 1


class TransportClient:
    STATE_IDLE = "IDLE"
    STATE_PENDING = "PENDING"
    STATE_CONNECTED = "CONNECTED"
    STATE_CLOSED = "CLOSED"

    def __init__(self, label, host_ip, port, hub=None, rto=0.25, bind_port=0, recv_ahead=1024, verbose=False, bind_ip="0.0.0.0"):
        self.label = label
        self.bind_ip = bind_ip  # local source address (a loopback 127.x.y.z gives the host a distinct peer IP: per-address limits of the guest table)
        self.addr = (socket.gethostbyname(host_ip), int(port))
        self.hub = hub or DEFAULT_HUB
        self.rto = rto
        self.recv_ahead = recv_ahead
        self.verbose = verbose
        self.sock = None
        self._open_socket(bind_port)
        self.inbox = Inbox(self.hub)
        self.stats = Counter()
        self.connect_count = 0
        self.nonce = None
        self._reset_connection_state()
        self.hub.register(self)

    # --- socket ---------------------------------------------------------------------------------

    def _open_socket(self, bind_port=0):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            try:  # Windows: don't let ICMP port-unreachable surface as recvfrom errors
                s.ioctl(socket.SIO_UDP_CONNRESET, False)
            except (OSError, ValueError):
                pass
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        s.bind((self.bind_ip, bind_port))
        self.sock = s

    @property
    def local_port(self):
        return self.sock.getsockname()[1] if self.sock else None

    def new_socket(self, bind_port=0):
        """Switch to a fresh UDP socket (new source port). Connection state is NOT reset here."""
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self._open_socket(bind_port)

    # --- connection state -------------------------------------------------------------------------

    def _reset_connection_state(self):
        self.state = self.STATE_IDLE
        self.sender = ReliableSender(self.rto)
        self.receiver = ReliableReceiver(self.recv_ahead)
        self.raw_arrivals = Counter()  # seq -> datagrams seen (incl. dropped/duplicate) this connection
        self.acks_rx = []              # [(t, next_expected, sack_bits)] from the host
        self.dropped_inbound = []      # seqs dropped by fault injection
        self.disconnected_by_host = False
        self.hello_ack_nonce = None
        self.last_send = 0.0
        self.last_recv = 0.0
        # fault-injection state (always cleared on a new connection)
        self._pretend_missing = set()
        self._skip_ack_seqs = set()
        self._skip_ack_preds = []
        self._drop_seqs = set()
        self._drop_preds = []
        self.withholding_acks = False
        self.silent = False
        self.auto_ack = True
        self.auto_retransmit = True
        self.auto_heartbeat = True

    def is_connected(self):
        return self.state == self.STATE_CONNECTED

    def _log(self, text):
        if self.verbose:
            print(f"[{self.label}] {text}")

    def next_nonce(self):
        return _deterministic_nonce(self.label, self.connect_count + 1)

    def connect(self, timeout=3.0, nonce="auto", keep_inbox=False):
        """Fresh transport connection: resets every seq/ack/fault state, sends HELLO (+nonce),
        resends every 500 ms, returns once HELLO_ACK arrives. nonce=None sends a legacy size-0
        HELLO. Raises ConnectError on timeout."""
        self._reset_connection_state()
        if not keep_inbox:
            self.inbox.clear()
        if nonce == "auto":
            nonce = self.next_nonce()
        self.connect_count += 1
        self.nonce = nonce
        self.state = self.STATE_PENDING
        self._tx(encode_hello(nonce), force=True)
        deadline = time.monotonic() + timeout
        last_hello = time.monotonic()
        while time.monotonic() < deadline:
            self.hub.pump_once(0.01)
            if self.state == self.STATE_CONNECTED:
                return True
            if self.state == self.STATE_CLOSED:
                break
            if time.monotonic() - last_hello >= PCNET_HEARTBEAT_INTERVAL_S:
                self._tx(encode_hello(nonce), force=True)
                last_hello = time.monotonic()
        self.state = self.STATE_CLOSED
        raise ConnectError(f"{self.label}: no HELLO_ACK from {self.addr} within {timeout}s")

    def disconnect(self, repeat=1):
        """Clean goodbye: DISCONNECT wire message so the host frees the slot immediately (no 5 s
        timeout). Local state becomes CLOSED; the socket stays open (reuse it or reconnect)."""
        if self.state in (self.STATE_CONNECTED, self.STATE_PENDING):
            for _ in range(max(1, repeat)):
                self._tx(encode_ctrl(PCNET_WIRE_DISCONNECT), force=True)
        self.state = self.STATE_CLOSED

    def reconnect(self, new_socket=False, timeout=3.0, settle=0.05, keep_inbox=False):
        """disconnect() + (optionally a new source port) + connect() with a NEW nonce and fully reset
        seq/ack state. `settle` pumps briefly so the host processes DISCONNECT first."""
        if self.state in (self.STATE_CONNECTED, self.STATE_PENDING):
            self.disconnect()
            if settle:
                self.hub.sleep(settle)
        if new_socket:
            self.new_socket()
        return self.connect(timeout=timeout, keep_inbox=keep_inbox)

    def restart_same_address(self, timeout=3.0, keep_inbox=False):
        """Simulates a crashed-and-restarted remote on the SAME address: no DISCONNECT, just a
        HELLO with a new nonce. Contract: host emits DISCONNECTED for the old logical peer and
        CONNECTED for a fresh one with fully reset reliable state."""
        return self.connect(timeout=timeout, keep_inbox=keep_inbox)

    def abandon(self):
        """Vanish WITHOUT a DISCONNECT (like a crashed process): stop pumping and close the socket.
        The host keeps this peer's slot until its PCNET_TIMEOUT (5 s)."""
        self.state = self.STATE_CLOSED
        self._abandoned_at = time.monotonic()  # next_default_player(): its resident stays bound host-side until the timeout
        self.hub.unregister(self)
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def close(self):
        if self.state in (self.STATE_CONNECTED, self.STATE_PENDING):
            self.disconnect()
        self.hub.unregister(self)
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # --- transmit ---------------------------------------------------------------------------------

    def _tx(self, pkt, force=False):
        if self.sock is None:
            return False
        if self.silent and not force:
            self.stats["tx_suppressed_silent"] += 1
            return False
        try:
            self.sock.sendto(pkt, self.addr)
        except OSError:
            self.stats["tx_error"] += 1
            return False
        self.last_send = time.monotonic()
        self.stats["tx_datagrams"] += 1
        return True

    def send_reliable(self, payload):
        """Reliable send (RDATA) with the next per-connection seq; transmitted immediately and
        retransmitted by pump() until ACKed. Returns the seq."""
        seq = self.sender.alloc(payload)
        self._transmit_seq(seq)
        return seq

    def _transmit_seq(self, seq, copies=1):
        e = self.sender.unacked.get(seq)
        if e is None:
            return
        for _ in range(copies):
            if self._tx(encode_rdata(seq, e.payload)):
                self.stats["rdata_tx"] += 1
        self.sender.mark_tx(seq, time.monotonic())

    def send_unreliable(self, payload):
        self._tx(encode_data(payload, PC_NET_UNRELIABLE))

    def send_heartbeat(self):
        """Explicit HEARTBEAT (pump() already sends one whenever this client idles >= 500 ms)."""
        self._tx(encode_ctrl(PCNET_WIRE_HEARTBEAT))

    ping = send_heartbeat

    def send_ack_now(self, repeat=1):
        ne, sack = self.receiver.build_ack(self._pretend_missing)
        for _ in range(max(1, repeat)):
            if self._tx(encode_ack(ne, sack)):
                self.stats["acks_tx"] += 1
        self.receiver.ack_pending = False
        return ne, sack

    def send_raw(self, pkt, force=False):
        return self._tx(pkt, force=force)

    # --- pump / receive ---------------------------------------------------------------------------

    def pump(self, now=None):
        if now is None:
            now = time.monotonic()
        self._drain_socket(now)
        if self.state != self.STATE_CONNECTED or self.silent:
            return
        if self.receiver.ack_pending and self.auto_ack and not self.withholding_acks:
            self.send_ack_now()
        if self.auto_retransmit:
            for seq in self.sender.due(now):
                self._transmit_seq(seq)
                self.stats["rdata_retransmit_tx"] += 1
        if self.auto_heartbeat and now - self.last_send >= PCNET_HEARTBEAT_INTERVAL_S:
            if self._tx(encode_ctrl(PCNET_WIRE_HEARTBEAT)):
                self.stats["heartbeat_tx"] += 1

    def _drain_socket(self, now):
        if self.sock is None:
            return
        while True:
            try:
                data, frm = self.sock.recvfrom(65536)
            except BlockingIOError:
                break
            except ConnectionResetError:
                self.stats["icmp_reset"] += 1
                continue
            except OSError as e:
                if getattr(e, "winerror", None) in (10035,):
                    break
                if getattr(e, "winerror", None) in (10054, 10040):
                    self.stats["icmp_reset"] += 1
                    continue
                raise
            if frm[0] != self.addr[0] or frm[1] != self.addr[1]:
                self.stats["rx_foreign"] += 1
                continue
            self._handle_datagram(data, now)

    def _handle_datagram(self, data, now):
        pkt = decode_packet(data)
        if pkt is None:
            self.stats["rx_malformed"] += 1
            return
        self.last_recv = now
        self.stats["rx_total"] += 1
        self.stats["rx_" + WIRE_TYPE_NAMES.get(pkt.wtype, "unknown").lower()] += 1
        t = pkt.wtype
        if t == PCNET_WIRE_HELLO_ACK:
            if self.state != self.STATE_PENDING:
                return
            if pkt.nonce is not None and self.nonce is not None and pkt.nonce != self.nonce:
                self.stats["hello_ack_wrong_nonce"] += 1  # answers some other HELLO; not ours
                return
            if pkt.nonce is None and self.nonce is not None:
                # A size-0 HELLO_ACK answering a nonce HELLO = a legacy (pre-v2) host: refused,
                # exactly like the real v2 client (pc_net.c).
                self.stats["hello_ack_legacy_refused"] += 1
                return
            self.hello_ack_nonce = pkt.nonce
            self.state = self.STATE_CONNECTED
            self.inbox.push(now, self.connect_count, CH_TRANSPORT, None, b"", "HELLO_ACK")
        elif t == PCNET_WIRE_HEARTBEAT:
            pass
        elif t == PCNET_WIRE_DISCONNECT:
            if self.state in (self.STATE_CONNECTED, self.STATE_PENDING):
                self.state = self.STATE_CLOSED
                self.disconnected_by_host = True
                self.inbox.push(now, self.connect_count, CH_TRANSPORT, None, b"", "DISCONNECT")
                self._log("host sent DISCONNECT")
        elif t == PCNET_WIRE_ACK:
            if self.state != self.STATE_CONNECTED:
                return
            self.acks_rx.append((now, pkt.ack_next, pkt.ack_sack))
            self.sender.on_ack(pkt.ack_next, pkt.ack_sack)
        elif t == PCNET_WIRE_RDATA:
            self._on_rdata(pkt, now)
        elif t == PCNET_WIRE_DATA:
            if self.state != self.STATE_CONNECTED:
                return
            if pkt.kind == PC_NET_UNRELIABLE:
                self._deliver(now, CH_UNRELIABLE, None, pkt.payload)
            else:
                self.stats["legacy_reliable_rx"] += 1  # contract: never sent any more
                self._deliver(now, CH_LEGACY, None, pkt.payload)

    def _on_rdata(self, pkt, now):
        if self.state != self.STATE_CONNECTED:
            self.stats["rdata_while_not_connected"] += 1
            return
        if pkt.kind != PC_NET_RELIABLE:
            self.stats["rdata_bad_kind"] += 1
        seq, payload = pkt.seq, pkt.payload
        self.raw_arrivals[seq] += 1
        if self.raw_arrivals[seq] == 1 and not self.receiver.received(seq):
            if seq in self._drop_seqs:
                self._drop_seqs.discard(seq)
                self._fault_drop(seq)
                return
            for pred in list(self._drop_preds):
                if pred(payload):
                    self._drop_preds.remove(pred)
                    self._fault_drop(seq)
                    return
            if seq in self._skip_ack_seqs:
                self._skip_ack_seqs.discard(seq)
                self._pretend_missing.add(seq)
                self.stats["fault_skip_ack"] += 1
            else:
                for pred in list(self._skip_ack_preds):
                    if pred(payload):
                        self._skip_ack_preds.remove(pred)
                        self._pretend_missing.add(seq)
                        self.stats["fault_skip_ack"] += 1
                        break
        elif seq in self._pretend_missing and self.raw_arrivals[seq] > 1:
            self._pretend_missing.discard(seq)  # the host retransmitted it: stop pretending
            self.stats["fault_skip_ack_released"] += 1
        status, delivered = self.receiver.on_rdata(seq, payload)
        self.stats["rdata_" + status] += 1
        for s, p in delivered:
            self._deliver(now, CH_RELIABLE, s, p)

    def _fault_drop(self, seq):
        self.dropped_inbound.append(seq)
        self.stats["fault_drop_inbound"] += 1
        self._log(f"fault: dropped first arrival of inbound seq {seq}")

    def _deliver(self, now, channel, seq, payload):
        m = self.inbox.push(now, self.connect_count, channel, seq, payload)
        self.on_message(m)

    def on_message(self, m):
        """Hook for subclasses (called once per delivered message, in delivery order)."""

    # --- observers --------------------------------------------------------------------------------

    def latest_ack(self):
        return self.acks_rx[-1] if self.acks_rx else None

    def host_retransmits_of(self, seq):
        """How many EXTRA copies of inbound `seq` arrived (>= 1 proves the host retransmitted)."""
        return max(0, self.raw_arrivals.get(seq, 0) - 1)

    def seq_of(self, pred):
        """Inbound seq of the first reliable message (already delivered, still in inbox or not)
        matching `pred(payload)`; None if not seen. Searches the delivered log."""
        for m in self.inbox.peek_all(lambda m: m.channel == CH_RELIABLE):
            if pred(m.payload):
                return m.seq
        return None

    def wait_acked(self, seq, timeout=1.0):
        """True once our reliable `seq` has been ACKed by the host."""
        return self.hub.wait_until(lambda: seq not in self.sender.unacked, timeout)

    def wait_disconnected(self, timeout):
        return self.hub.wait_until(lambda: self.state == self.STATE_CLOSED, timeout)

    def delivered_in_order(self):
        """True iff the host->client reliable stream on this connection was delivered as exactly
        0, 1, 2, ... with no gap or repeat."""
        d = self.receiver.delivered_seqs
        return d == list(range(len(d)))

    # --- fault injection (deterministic, client side) ----------------------------------------------
    #
    # Outbound:
    #   send_reliable_duplicate(payload, copies)   same seq sent `copies` times back-to-back
    #   send_reliable_out_of_order(payloads, order) consecutive seqs, transmitted in `order`
    #   send_reliable_withheld(payload)            seq allocated but NOT transmitted (a gap)
    #   release_withheld(seqs=None)                transmit withheld seq(s) now
    #   send_ack_now(repeat=n) / send_raw_ack()    duplicate / crafted ACKs
    #   send_legacy_reliable_data(payload)         DATA(type 4) kind=1 (must be dropped by host)
    # Inbound:
    #   skip_ack_once(seq) / skip_ack_next(pred)   ACKs report that seq missing until the host
    #                                              retransmits it once (proves host retransmit)
    #   drop_inbound_once(seq) / drop_inbound_next(pred)  ignore the first arrival (as if lost)
    #   withhold_acks(True/False)                  send no ACKs at all (retry-budget test)
    # Whole link:
    #   go_silent() / resume()                     stop ALL outbound traffic (timeout test)
    #   set_auto(ack=, retransmit=, heartbeat=)    disable individual automatic behaviours

    def send_reliable_duplicate(self, payload, copies=2):
        seq = self.sender.alloc(payload)
        self._transmit_seq(seq, copies=copies)
        self.stats["fault_dup_tx"] += copies - 1
        return seq

    def send_reliable_out_of_order(self, payloads, order=None):
        """Allocates consecutive seqs for `payloads` (in list order) and transmits them in `order`
        (indices into payloads; default reversed). Returns the seqs in allocation order."""
        seqs = [self.sender.alloc(p, withheld=True) for p in payloads]
        if order is None:
            order = list(reversed(range(len(payloads))))
        for i in order:
            self._transmit_seq(seqs[i])
        self.stats["fault_reorder_tx"] += 1
        return seqs

    def send_reliable_withheld(self, payload):
        seq = self.sender.alloc(payload, withheld=True)
        self.stats["fault_withheld_tx"] += 1
        return seq

    def release_withheld(self, seqs=None):
        todo = self.sender.withheld() if seqs is None else list(seqs)
        for seq in todo:
            self._transmit_seq(seq)
        return todo

    def send_raw_ack(self, next_expected, sack_bits, repeat=1):
        for _ in range(max(1, repeat)):
            self._tx(encode_ack(next_expected, sack_bits))

    def send_legacy_reliable_data(self, payload):
        self._tx(encode_data(payload, PC_NET_RELIABLE))

    def skip_ack_once(self, seq):
        self._skip_ack_seqs.add(seq)

    def skip_ack_next(self, pred):
        """pred(payload) -> bool, evaluated on the first arrival of each new inbound seq."""
        self._skip_ack_preds.append(pred)

    def drop_inbound_once(self, seq):
        self._drop_seqs.add(seq)

    def drop_inbound_next(self, pred):
        self._drop_preds.append(pred)

    def withhold_acks(self, on=True):
        self.withholding_acks = bool(on)

    def go_silent(self):
        self.silent = True

    def resume(self):
        self.silent = False

    def set_auto(self, ack=None, retransmit=None, heartbeat=None):
        if ack is not None:
            self.auto_ack = bool(ack)
        if retransmit is not None:
            self.auto_retransmit = bool(retransmit)
        if heartbeat is not None:
            self.auto_heartbeat = bool(heartbeat)


# =================================================================================================
# 6. Game protocol (pc_net_game.c / pc_net_game.h), protocol v2 -- verified against the C source
# =================================================================================================

PC_NETGAME_MSG_IDENTITY = 1
PC_NETGAME_MSG_IDENTITY_ACK = 2
PC_NETGAME_MSG_REJECT = 3
PC_NETGAME_MSG_MOVE = 4
PC_NETGAME_MSG_APPEARANCE = 5
PC_NETGAME_MSG_PICKUP_REQUEST = 6
PC_NETGAME_MSG_PICKUP_RESULT = 7
PC_NETGAME_MSG_FIELD_UPDATE = 8
PC_NETGAME_MSG_DROP_REQUEST = 9
PC_NETGAME_MSG_DROP_RESULT = 10
PC_NETGAME_MSG_PLAYER_CONTEXT = 11
PC_NETGAME_MSG_SNAPSHOT_BEGIN = 12
PC_NETGAME_MSG_FIELD_BLOCK = 13
PC_NETGAME_MSG_SNAPSHOT_END = 14
PC_NETGAME_MSG_RESYNC_REQUEST = 15
PC_NETGAME_MSG_WORLD_META = 16
PC_NETGAME_MSG_INTERACT_CONFIRM = 17  # client -> host, 8 bytes: the second phase of pickup/drop (reserve -> confirm -> commit)
PC_NETGAME_MSG_WORLD_SNAPSHOT = PC_NETGAME_MSG_SNAPSHOT_BEGIN  # phase-1 name, kept as an alias
# Villager population/is_home milestone (host -> READY client(s), reliable, world_seq-stamped):
PC_NETGAME_MSG_VILLAGER_ARRIVAL = 18    # a new villager grew into a slot (mNpc_Grow()+mNpc_SetNpcHome())
PC_NETGAME_MSG_VILLAGER_DEPARTURE = 19  # a villager was force-removed (mNpc_ForceRemove()); slot + house footprint gone
PC_NETGAME_MSG_VILLAGER_SNAPSHOT = 20   # one-shot full population, sent once per snapshot (join/resync/reconnect)
# Friendship/mail sync milestone:
PC_NETGAME_MSG_FRIENDSHIP_REQUEST = 21         # client -> host only: local delta for THIS connection's identity
PC_NETGAME_MSG_FRIENDSHIP_UPDATE = 22          # host -> every READY client, world_seq-stamped: resulting value
PC_NETGAME_MSG_FRIENDSHIP_SNAPSHOT_ENTRY = 23  # host -> one client: one occupied Anmmem_c entry (late-join/reconnect)
PC_NETGAME_MSG_MAIL_REQUEST = 24               # client -> host only: raw Mail_c the local player sent
PC_NETGAME_MSG_MAIL_DELIVERED = 25             # host -> every READY client, world_seq-stamped: delivery outcome
# World Ecology T3 (host-authoritative bury): client -> host two-phase reserve/INTERACT_CONFIRM, mirroring
# PICKUP_REQUEST/DROP_REQUEST -- see PCNetGameBuryRequestMsg/PCNetGameBuryResultMsg (pc_net_game.c).
PC_NETGAME_MSG_BURY_REQUEST = 31
PC_NETGAME_MSG_BURY_RESULT = 32
PC_NETGAME_BURY_FLAG_RECONCILE_VALID = 0x01
PC_NETGAME_BURY_FLAG_DEPOSIT_ON = 0x02

# M9-A: PLAYER_SCENE (id 44, 12 bytes, reliable) -- see PCNetGamePlayerSceneMsg (pc_net_game.c).
PC_NETGAME_MSG_PLAYER_SCENE = 44
PC_NETGAME_SCENE_FLAG_IN_TOWN = 0x01
PC_NETGAME_SCENE_FLAG_CLEARED = 0x80

# M9-C: NPC_TALK (id 45, 8 bytes, reliable, client -> host) -- see PCNetGameNpcTalkMsg (pc_net_game.c).
PC_NETGAME_MSG_NPC_TALK = 45
PC_NETGAME_NPC_TALK_FLAG_BEGIN = 0x01

# M9-C Phase 5: PLAYER_ACTION (id 46, 10 bytes, reliable, HOST -> client only; presentation hint, kind 1 = PICKUP) --
# see PCNetGamePlayerActionMsg (pc_net_game.c). A client never originates it; the host drops any that arrives.
PC_NETGAME_MSG_PLAYER_ACTION = 46
PC_NETGAME_PLAYER_ACTION_KIND_PICKUP = 1
PLAYER_ACTION_FMT = "<BBBBBBHH"  # msg_type, net_player_id, kind, flags, ut_x, ut_z, item, seq

PC_NETGAME_PROTOCOL_VERSION = 8  # M9-C: v6 NPC_TALK (id 45); v7 MOVE action_state (main index + entry counter), same 28-byte layout, + PLAYER_ACTION (id 46); v8 (D3): RECORD_HELLO/BEGIN/CHUNK/ACK (ids 47-50) + (X1, unreleased: extended in place) TXN_COMMIT/TXN_RESULT (ids 51-52)
PROTOCOL_VERSION = PC_NETGAME_PROTOCOL_VERSION  # the ONE source of truth for tests (wire_baseline.EXPECTED_PROTOCOL_VERSION audits it)

# --- D3 (protocol v8): host-mirrored resident record, messages 47..50 (all RELIABLE) ---
PC_NETGAME_MSG_RECORD_HELLO = 47   # C->H 24 B
PC_NETGAME_MSG_RECORD_BEGIN = 48   # both 28 B
PC_NETGAME_MSG_RECORD_CHUNK = 49   # both 1012 B
PC_NETGAME_MSG_RECORD_ACK = 50     # both 20 B
PC_NETGAME_REC_SIZE = 0x2440
PC_NETGAME_REC_CHUNK_DATA = 1000
PC_NETGAME_REC_CHUNK_COUNT = 10
PC_NETGAME_REC_HELLO_FLAG_HAVE_LAST = 0x01
PC_NETGAME_REC_KIND_PUSH_FULL = 1
PC_NETGAME_REC_KIND_PUSH_HOSTFIELDS = 2
PC_NETGAME_REC_KIND_UPLOAD = 3
PC_NETGAME_REC_KIND_MIGRATE_UPLOAD = 4
PC_NETGAME_REC_ACK_APPLIED = 0
PC_NETGAME_REC_ACK_STALE_BASE = 1
PC_NETGAME_REC_ACK_BAD_DIGEST = 2
PC_NETGAME_REC_ACK_BAD_SHAPE = 3
PC_NETGAME_REC_ACK_INVALID_FIELD = 4
PC_NETGAME_REC_ACK_RATE_LIMITED = 5
PC_NETGAME_REC_ACK_NOT_BOUND = 6
PC_NETGAME_REC_ACK_BUSY = 7
PC_NETGAME_REC_ACK_MIGRATE_REQUEST = 8
PC_NETGAME_REC_ACK_ADOPT_DEFERRED = 9
PC_NETGAME_REC_ACK_ADOPT_FAILED = 10
PC_NETGAME_REC_FIELD_PLAYER_ID = 1
PC_NETGAME_REC_FIELD_EXISTS = 2
PC_NETGAME_REC_FIELD_WALLET = 3
PC_NETGAME_REC_FIELD_BANK = 4
PC_NETGAME_REC_FIELD_LOAN = 5
PC_NETGAME_REC_FIELD_POCKET = 6
PC_NETGAME_REC_FIELD_ITEM_COND = 7
PC_NETGAME_REC_FIELD_EQUIPMENT = 8
PC_NETGAME_REC_FIELD_ORG_TABLE = 9
PC_NETGAME_REC_FIELD_CATALOG = 10   # detail high byte = catalog order index
PC_NETGAME_REC_FIELD_LOTTO = 11
PC_NETGAME_REC_FIELD_MAIL_PRESENT = 12   # mail milestone 1: a USED mail[] letter whose gift is not EMPTY_NO / RSV_NO / pocket-legal; detail high byte = mail slot
# BAD_SHAPE detail codes: 1 total_size, 2 chunk_count, 3 host-only/unknown BEGIN kind, 4 HELLO record_size, 5 BEGIN.rsv != 0,
# 6 HELLO _reserved0 / undefined flag bits. (A host counts a client RECORD_ACK status other than 0/9/10 as a violation.)
RECORD_HELLO_FMT = "<BBHIIIII"      # PCNetGameRecordHelloMsg, 24 bytes
RECORD_BEGIN_FMT = "<BBBBIIIIII"    # PCNetGameRecordBeginMsg, 28 bytes
RECORD_CHUNK_FMT = "<BBHIHH1000s"   # PCNetGameRecordChunkMsg, 1012 bytes
RECORD_ACK_FMT = "<BBHIIII"         # PCNetGameRecordAckMsg, 20 bytes

# --- X1 (protocol v8, UNRELEASED: ids 51/52 extend v8 in place, no version bump): host-transactional PICKUP/DROP/BURY commit ---
# TXN_COMMIT (C->H, RELIABLE, 72 B = 8 B header + the reusable 64 B PCNetGameTxnTag) answers an accepted provisional
# PICKUP/DROP/BURY RESULT; TXN_RESULT (H->C, RELIABLE, 76 B) is the host's answer to EVERY COMMIT (APPLIED + the post-image, or
# REJECTED + a reason). 53/54 are reserved for X2 (TXN_QUERY / TXN_STATUS). The three pocket arrays (15 x u16) are native LE.
PC_NETGAME_MSG_TXN_COMMIT = 51   # C->H 72 B
PC_NETGAME_MSG_TXN_RESULT = 52   # H->C 76 B
PC_NETGAME_TXN_DEST_NONE = 0
PC_NETGAME_TXN_DEST_POCKET = 1
PC_NETGAME_TXN_DEST_WALLET = 2
PC_NETGAME_TXN_SLOT_WALLET = 0xFF
PC_NETGAME_TXN_FLAG_EXCHANGE = 0x01   # X3: TXN_COMMIT kind DROP only (the dropped slot takes aux_item / aux_cond, a catch replacement)
PC_NETGAME_TXN_OUTCOME_APPLIED = 0
PC_NETGAME_TXN_OUTCOME_REJECTED = 1
PC_NETGAME_TXN_REASON_NONE = 0
PC_NETGAME_TXN_REASON_EXPIRED = 1
PC_NETGAME_TXN_REASON_NOT_PENDING = 2
PC_NETGAME_TXN_REASON_WORLD_CHANGED = 3
PC_NETGAME_TXN_REASON_NOT_SYNCED = 4
PC_NETGAME_TXN_REASON_NOT_BOUND = 5
PC_NETGAME_TXN_REASON_FENCED = 6
PC_NETGAME_TXN_REASON_CONFLICT = 7
PC_NETGAME_TXN_REASON_BAD_IMAGE = 8
PC_NETGAME_TXN_REASON_PRECOND = 9
PC_NETGAME_TXN_REASON_STALE_IMAGE = 10
PC_NETGAME_TXN_REASON_BAD_SHAPE = 11
PC_NETGAME_TXN_REASON_BUSY = 12
PC_NETGAME_TXN_REASON_FAULT = 13
PC_NETGAME_TXN_REASON_REPLAYED = 14   # informational, on APPLIED replays
PC_NETGAME_TXN_REASON_ALREADY_DONATED = 15   # town services: the museum already holds the exhibit (the client keeps the item)
PC_NETGAME_TXN_REASON_NOT_AVAILABLE = 16     # town services: the lost-and-found (slot, expected item) no longer matches
PC_NETGAME_TXN_REASON_NOT_DONATABLE = 17     # town services: not a museum donation (vanilla predicate)
PC_NETGAME_TXN_REASON_NO_DONOR_SLOT = 18     # town services: the bound resident has no museum donor slot (guest / extra player)
PC_NETGAME_TXN_REASON_NO_FUNDS = 19          # shop: SHOP_BUY the pre-image wallet (plus money bags) cannot pay the host price
PC_NETGAME_TXN_REASON_NOT_SELLABLE = 20      # shop: SHOP_SELL of stationery / a quest item / a Sunday turnip bundle
PC_NETGAME_TXN_REASON_PRICE_MISMATCH = 21    # shop: SHOP_BUY whose expected price differs from the host's price
PC_NETGAME_TXN_REASON_NO_ROOM = 22           # shop: SHOP_SELL whose money-bag overflow needs more free pockets than there are
PC_NETGAME_TXN_REASON_NO_SUCH_ADDRESS = 23   # mail: MAIL_SEND to a player who owns no house on the HOST
PC_NETGAME_TXN_REASON_MAILBOX_FULL = 24      # mail: MAIL_SEND whose recipient's house mailbox has no free slot
PC_NETGAME_TXN_REASON_PO_FULL = 25           # mail: MAIL_SEND the post office cannot take (5 desk slots / 10 letters per house)
PC_NETGAME_TXN_REASON_NO_SUCH_LETTER = 26    # mail (milestone 2): MAIL_TAKE of a mailbox slot that is empty on the host
PC_NETGAME_TXN_REASON_MAIL_CHANGED = 27      # mail (milestone 2): MAIL_TAKE of a mailbox letter whose hash differs from the claimed one
PC_NETGAME_TXN_RING = 16              # host journal entries per resident
PC_NETGAME_TXN_FENCED_NUM = 4         # fenced nonces remembered per resident
TXN_TAG_FMT = "<IIBBHBBHII15HHII"       # PCNetGameTxnTag, 64 bytes
TXN_COMMIT_FMT = "<BBHIIIBBHBBHII15HHII"  # PCNetGameTxnCommitMsg, 72 bytes
TXN_RESULT_FMT = "<BBBBIIIIIIIBBH15HHII"  # PCNetGameTxnResultMsg, 76 bytes
TXN_REASON_NAMES = {0: "NONE", 1: "EXPIRED", 2: "NOT_PENDING", 3: "WORLD_CHANGED", 4: "NOT_SYNCED", 5: "NOT_BOUND", 6: "FENCED",
                    7: "CONFLICT", 8: "BAD_IMAGE", 9: "PRECOND", 10: "STALE_IMAGE", 11: "BAD_SHAPE", 12: "BUSY", 13: "FAULT",
                    14: "REPLAYED", 15: "ALREADY_DONATED", 16: "NOT_AVAILABLE", 17: "NOT_DONATABLE", 18: "NO_DONOR_SLOT", 19: "NO_FUNDS",
                    20: "NOT_SELLABLE", 21: "PRICE_MISMATCH", 22: "NO_ROOM", 23: "NO_SUCH_ADDRESS", 24: "MAILBOX_FULL", 25: "PO_FULL",
                    26: "NO_SUCH_LETTER", 27: "MAIL_CHANGED"}

# --- X3 (same v8, extended IN PLACE): the one-phase GRANTS ride the SAME 64-byte tag. FIELD_ACTION_REQUEST (29) grows 12 -> 76 B and
# CATCH_REQUEST (41) grows 20 -> 84 B with the trailing PCNetGameTxnTag; TXN_RESULT.kind 4..7 names the grant. An all-zero tag on a
# FIELD_ACTION_REQUEST means "no grant" (every non-granting kind; DIG_HOLE / DIG_SHINE without a bonus); DIG_BURIED and CATCH need a tag.
PC_NETGAME_MSG_FIELD_ACTION_REQUEST = 29  # C->H 76 B (X3)
PC_NETGAME_MSG_FIELD_ACTION_RESULT = 30   # H->C 12 B
PC_NETGAME_MSG_CATCH_REQUEST = 41         # C->H 84 B (X3)
PC_NETGAME_MSG_CATCH_RESULT = 42          # H->C 12 B
PC_NETGAME_TXN_KIND_DIG_BURIED = 4
PC_NETGAME_TXN_KIND_DIG_HOLE = 5
PC_NETGAME_TXN_KIND_DIG_SHINE = 6
PC_NETGAME_TXN_KIND_CATCH = 7
PC_NETGAME_TXN_KIND_MUSEUM_DONATE = 8   # town services: TXN_COMMIT kind 8 (dest NONE, slot = pocket slot, item); host-authoritative museum donation
PC_NETGAME_TXN_KIND_POLICE_CLAIM = 9    # town services: TXN_COMMIT kind 9 (dest POCKET, slot = free slot, item = expected item, aux_cond = lost-and-found index)
PC_NETGAME_TXN_KIND_SHOP_BUY = 10       # shop (milestone 2): dest POCKET, slot = free slot, item, aux_cond = stock code, aux_item = the expected price
PC_NETGAME_TXN_KIND_SHOP_SELL = 11      # shop (milestone 2): dest NONE, slot = primary slot, item = its item, aux_item = the bit mask of every slot sold
PC_NETGAME_TXN_KIND_MAIL_SEND = 12      # mail (milestone 1): dest NONE, slot = mail slot 0..9, item = the letter's present echo, aux_item / aux_cond = low 16 / bits 16..23 of the letter's canonical-BE FNV-1a32
PC_NETGAME_TXN_KIND_MAIL_TAKE = 13      # mail (milestone 2): dest NONE, slot = MAILBOX slot 0..9, flags = the destination mail[] slot, item = the letter's present echo, aux_item / aux_cond = low 16 / bits 16..23 of its canonical-BE FNV-1a32
PC_NETGAME_SHOP_STOCK_COUNTED = 0xFD    # SHOP_BUY aux_cond: a counted candy / grab bag (Shop_c.flowers_candy_grab_bag_count)
PC_NETGAME_SHOP_STOCK_RARE = 0xFE       # SHOP_BUY aux_cond: Shop_c.rare_item
PC_NETGAME_SHOP_STOCK_UNLIMITED = 0xFF  # SHOP_BUY aux_cond: unlimited stationery
PC_NETGAME_SHOP_GOODS_COUNT = 39        # Shop_c.items[] (aux_cond 0..38 = the index)
PC_NETGAME_SHOP_SELL_RATIO = 4          # SELL_BUY_RATIO: a normal item sells for price / 4

# --- Town services milestone 1 (same v8, extended IN PLACE): the generic host -> client service mirror TOWN_SVC_STATE (id 55, RELIABLE,
# 12 + len bytes, variable). Ids 53 / 54 stay reserved for X2 (the C enum names them PC_NETGAME_MSG_TXN_RESERVED_53 / _54). ---
PC_NETGAME_MSG_TOWN_SVC_STATE = 55
PC_NETGAME_TS_POLICE = 1       # Save_t.police_box.keep_items[20] as 20 little-endian u16 (40 B)
PC_NETGAME_TS_MUSEUM = 2       # Save_t.museum_display, 63 raw bytes (4-bit donor nibbles)
# NOTE (shop milestone): service 3 is SENT since milestone 2 (the 320 B Shop_c mirror); the trailing text of the next line is the pinned HEAD text.
PC_NETGAME_TS_SHOP = 3         # RESERVED for the next milestone: never sent, never accepted
PC_NETGAME_TS_BLOB_MAX = 340
PC_NETGAME_TS_POLICE_LEN = 40
PC_NETGAME_TS_MUSEUM_LEN = 63
PC_NETGAME_TS_SHOP_LEN = 320
TOWN_SVC_STATE_FMT = "<BBHII"  # 12-byte header (msg_type, service, len, seq, digest); the blob (len bytes) follows
# --- Mail milestone 2 (same v8, extended IN PLACE): MAILBOX_LETTER (id 56, host -> the OWNING client only, RELIABLE, 316 B): ONE slot of the host-held house
# mailbox as its 298 canonical BE bytes (or an "empty" indication, flags bit 0), per-(resident, slot) seq, FNV-1a32 of the 298 bytes. ---
PC_NETGAME_MSG_MAILBOX_LETTER = 56
PC_NETGAME_MBOX_SLOTS = 10
PC_NETGAME_MBOX_FLAG_EMPTY = 0x01
PC_NETGAME_MAIL_WIRE_SIZE = 298
MAILBOX_LETTER_FMT = "<BBBBIIHH298sH"  # msg_type, house, mbox_idx, flags, seq, digest, used_count, rsv0, letter[298], rsv1 = 316 bytes
# --- Guests G1 (same v8, extended IN PLACE): IDENTITY_EXT (id 57, client -> host, RELIABLE, 42 B, sent BEFORE the frozen IDENTITY: the guest flag, the guest's HOME
# PersonalID and the token it holds) and IDENTITY_TOKEN (id 58, host -> the admitted guest only, RELIABLE, 20 B, right after the frozen IDENTITY_ACK: the token + the
# guest table slot). RECORD_BEGIN.rsv is the record_class (0 resident, 1 guest) in BOTH directions. ---
PC_NETGAME_MSG_IDENTITY_EXT = 57
PC_NETGAME_MSG_IDENTITY_TOKEN = 58
PC_NETGAME_IDEXT_FLAG_GUEST = 0x01
PC_NETGAME_IDTOKEN_FLAG_NEW = 0x01
PC_NETGAME_IDTOKEN_FLAG_KNOWN = 0x02
PC_NETGAME_GUEST_TOKEN_LEN = 16
PC_NETGAME_GUEST_MAX = 8
PC_NETGAME_REC_CLASS_RESIDENT = 0
PC_NETGAME_REC_CLASS_GUEST = 1
IDENTITY_EXT_FMT = "<BBH8s8sHHB16sB"   # msg_type, flags, rsv0, home_player_name[8], home_land_name[8], home_player_id, home_land_id, token_present, token[16], rsv1 = 42 B
IDENTITY_TOKEN_FMT = "<BBBB16s"        # msg_type, flags, guest_slot, table_size, token[16] = 20 B
FIELD_ACTION_REQUEST_FMT = "<BBBBIBBHIIBBHBBHII15HHII"  # PCNetGameFieldActionRequestMsg, 76 bytes (12 B header + the 64 B tag)
FIELD_ACTION_RESULT_FMT = "<BBBBIHBB"                   # PCNetGameFieldActionResultMsg, 12 bytes
CATCH_REQUEST_FMT = "<B3xIIIiIIBBHBBHII15HHII"          # PCNetGameCatchRequestMsg, 84 bytes (20 B header + the 64 B tag)
CATCH_RESULT_FMT = "<BBHII"                             # PCNetGameCatchResultMsg, 12 bytes
FIELD_ACTION_KIND_DIG_BURIED = 1
FIELD_ACTION_KIND_MONEY_ROCK_HIT = 2
FIELD_ACTION_KIND_DIG_HOLE = 5
FIELD_ACTION_KIND_DIG_SHINE = 8
TXN_KIND_NAMES = {1: "PICKUP", 2: "DROP", 3: "BURY", 4: "DIG_BURIED", 5: "DIG_HOLE", 6: "DIG_SHINE", 7: "CATCH", 8: "MUSEUM_DONATE", 9: "POLICE_CLAIM"}
assert struct.calcsize(FIELD_ACTION_REQUEST_FMT) == 76 and struct.calcsize(CATCH_REQUEST_FMT) == 84
assert struct.calcsize(FIELD_ACTION_RESULT_FMT) == 12 and struct.calcsize(CATCH_RESULT_FMT) == 12

PC_NETGAME_REJECT_PROTOCOL_MISMATCH = 1  # 8-byte REJECT
PC_NETGAME_REJECT_SERVER_FULL = 2        # reserved, never sent
PC_NETGAME_REJECT_LAND_MISMATCH = 3      # 24-byte REJECT carrying the host town
PC_NETGAME_REJECT_NO_SAVE = 4            # 24-byte REJECT carrying the host town

PC_NETGAME_HOST_PLAYER_ID = 8  # == PC_NET_MAX_PEERS
PC_NETGAME_GRID_TOWN = 0
PC_NETGAME_CTX_FLAG_IN_TOWN = 0x01
PC_NETGAME_FU_FLAG_DEPOSIT_VALID = 0x01
PC_NETGAME_FU_FLAG_DEPOSIT_ON = 0x02
PC_NETGAME_FB_FLAG_IN_SNAPSHOT = 0x01
PC_NETGAME_META_FLAG_RENEW_TIME_VALID = 0x01
PC_NETGAME_META_FLAG_WEATHER_VALID = 0x02      # weather + Stalk Market milestone
PC_NETGAME_META_FLAG_MARKET_VALID = 0x04       # weather + Stalk Market milestone
PC_NETGAME_RESYNC_REASON_SAVE_RELOADED = 1

# INTERACT_CONFIRM (hardening contract): PICKUP_RESULT/DROP_RESULT accepted=1 is now a PROVISIONAL accept (the
# tile is RESERVED, the field has NOT changed); the requester must answer with INTERACT_CONFIRM. The host
# mutates the field ONLY on COMMIT. accepted=0 stays a final rejection (no reservation).
CONFIRM_KIND_PICKUP = 1
CONFIRM_KIND_DROP = 2
CONFIRM_KIND_BURY = 3   # World Ecology T3 -- see PC_NETGAME_MSG_BURY_REQUEST/RESULT
CONFIRM_OUTCOME_ABORT = 0   # the client did NOT do its inventory step and will not
CONFIRM_OUTCOME_COMMIT = 1  # the client did its inventory step
CONFIRM_REASON_NONE = 0
CONFIRM_REASON_POCKETS_FULL = 1
CONFIRM_REASON_SLOT_CHANGED = 2
CONFIRM_REASON_STATE_CHANGED = 3  # player/save changed
CONFIRM_REASON_STALE = 4          # no matching pending request
CONFIRM_REASON_CANCELLED = 5
PC_NETGAME_CONFIRM_TIMEOUT_MS = 20000  # host reservation lifetime (> the client's ~15 s retry budget)
PC_NETGAME_POS_ABS_LIMIT = 100000.0    # |coord| limit of a valid MOVE position (pcnetgame_pos_valid)

# v2 hosts verify the claimed town (land_name, land_id, terrain_hash): FakeClient probes the host's
# town once per (ip, port) via a deliberate LAND_MISMATCH and then claims it.
TOWN_CHECK_ENFORCED = True

# MOVE claims are sent as RDATA so they stay ordered before the pickup/drop request that follows;
# confirmed by B: host dispatch is size+type only.
CLAIM_POSITION_RELIABLE = True

# v2: the host rejects pickup/drop unless the requester's PLAYER_CONTEXT has IN_TOWN, so every
# FakeClient sends PLAYER_CONTEXT(flags=IN_TOWN) right after READY by default (like the real client).
DEFAULT_CONTEXT_FLAGS = PC_NETGAME_CTX_FLAG_IN_TOWN

IDENTITY_FMT = "<BBHI8s8sHHI"           # PCNetGameIdentityMsg v2, 32 bytes (version at offset 4 frozen)
ACK_FMT = "<BBHI8s8sHHI"                # PCNetGameIdentityAckMsg v2, 32 bytes
REJECT_FMT = "<BBHI"                    # PCNetGameRejectMsg, 8 bytes (also the prefix of the 24-byte form)
REJECT_TOWN_FMT = "<BBHI8sHHI"          # PCNetGameRejectTownMsg, 24 bytes
MOVE_FMT = "<BBBbIfffhHf"               # PCNetMoveMsg, 28 bytes (v7: the u16 at offset 22 is action_state)
# v7 MOVE action_state: low byte = vanilla now_main_index (0..120, NUM = 121), bits 8..11 = 4-bit state-entry counter,
# bits 12..15 reserved (must be 0). A malformed value is zeroed by the host (never rejects the MOVE).
MOVE_ACTION_INDEX_NUM = 121
MOVE_ACTION_COUNTER_SHIFT = 8
MOVE_ACTION_COUNTER_MASK = 0x0F


def move_action_state(main_index, entry_counter=0):
    """Encodes the v7 MOVE action_state: (counter & 0xF) << 8 | main_index (no range check on purpose, so tests can
    send out-of-range indices; use action_state=<raw> in build_move for arbitrary reserved bits)."""
    return ((entry_counter & MOVE_ACTION_COUNTER_MASK) << MOVE_ACTION_COUNTER_SHIFT) | (main_index & 0xFF)
APPEARANCE_HDR_FMT = "<BBBBHBB"         # PCNetGameAppearanceMsg header
APPEARANCE_MSG_SIZE = 576
PICKUP_REQUEST_FMT = "<BBBBI"           # 8 bytes
PICKUP_RESULT_FMT = "<BBBBIHH"          # 12 bytes
FIELD_UPDATE_FMT = "<BBBBBBHI"          # PCNetGameFieldUpdateMsg v2, 12 bytes
DROP_REQUEST_FMT = "<BBBBHHI"           # 12 bytes
DROP_RESULT_FMT = "<BBBBIHH"            # 12 bytes
# World Ecology T3: PCNetGameBuryRequestMsg/PCNetGameBuryResultMsg (pc_net_game.c), both 12 bytes.
# Request: msg_type,pocket_slot_idx,ut_x,ut_z,claimed_item,hole_variant,_reserved0,request_id.
# Result:  msg_type,accepted,ut_x,ut_z,request_id,buried_item,flags,reason (flags/reason replaced the
# old 16-bit _reserved0 -- see PCNetGameBuryResultMsg's own doc comment).
BURY_REQUEST_FMT = "<BBBBHBBI"          # 12 bytes
BURY_RESULT_FMT = "<BBBBIHBB"           # 12 bytes
PLAYER_CONTEXT_FMT = "<BBBBhh"          # 8 bytes
RTC_FMT = "BBBBBBH"                     # PCNetGameRtcWire (sec,min,hour,day,weekday,month,year), 8 bytes
SNAPSHOT_BEGIN_FMT = "<BBBBII"          # 12 bytes
FIELD_BLOCK_HDR_FMT = "<BBBBII"         # 12-byte header of PCNetGameFieldBlockMsg
FIELD_BLOCK_SIZE = 588                  # header + u16 items[256] + u16 deposit[16] + u16 valid[16]
# Weather + Stalk Market + World Ecology (fish/bug term) milestone: PCNetGameWorldStateWire, 30 bytes
# -- 7 daily prices (Sunday..Saturday), trade_market, the Kabu update_time (an RTC_FMT), weather,
# weather_intensity, gyoei_term, gyoei_term_transition_offset, insect_term,
# insect_term_transition_offset (see pc_net_game.c's own PCNetGameWorldStateWire doc comment). Grew
# from 28 to 30 bytes in the World Ecology Stage 1 milestone (Item 4): the struct's own 2 reserved pad
# bytes became 2 of the 4 new term bytes, so the struct's LOGICAL size only grew by 2 -- but embedding
# it pushes both composite messages below off 4-byte alignment, so the C compiler now adds 2 bytes of
# IMPLICIT trailing padding to each (52/48 instead of 50/46) that this format must reproduce
# explicitly via a trailing "2x" (see each FMT's own comment below).
WORLD_STATE_FMT = "<7HH" + RTC_FMT + "BBBBBB"  # 30 bytes
_WORLD_STATE_FIELDS = ["kabu_daily_price_sun", "kabu_daily_price_mon", "kabu_daily_price_tue",
                       "kabu_daily_price_wed", "kabu_daily_price_thu", "kabu_daily_price_fri",
                       "kabu_daily_price_sat", "kabu_trade_market", "kabu_update_sec", "kabu_update_min",
                       "kabu_update_hour", "kabu_update_day", "kabu_update_weekday", "kabu_update_month",
                       "kabu_update_year", "weather", "weather_intensity", "gyoei_term",
                       "gyoei_term_transition_offset", "insect_term", "insect_term_transition_offset"]
SNAPSHOT_END_FMT = "<BBBBII" + RTC_FMT + WORLD_STATE_FMT[1:] + "2x"  # 52 bytes (50 logical + 2 implicit pad)
WORLD_META_FMT = "<BBHI" + RTC_FMT + WORLD_STATE_FMT[1:] + "2x"      # 48 bytes (46 logical + 2 implicit pad)
RESYNC_REQUEST_FMT = "<BBBB"            # 4 bytes
INTERACT_CONFIRM_FMT = "<BBBBI"         # 8 bytes: type, kind, outcome, reason, request_id

# Villager population/is_home milestone: PCNetGameVillagerArrivalMsg/PCNetGameVillagerDepartureMsg/
# PCNetGameVillagerSnapshotMsg (pc_net_game.c). Mirrors the exact field layout/padding of the C
# structs -- see that file's own doc comments for the wire contract.
VILLAGER_ARRIVAL_FMT = "<BBBBBBBBHHI"   # 16 bytes
VILLAGER_DEPARTURE_FMT = "<BBBBI"       # 8 bytes
VILLAGER_SLOT_FMT = "<HBBBBBB"          # PCNetGameVillagerSlotWire, 8 bytes
VILLAGER_SNAPSHOT_HDR_FMT = "<BBBBII"   # 12-byte header of PCNetGameVillagerSnapshotMsg
ANIMAL_NUM_MAX = 15                     # mirrors include/m_npc.h; pc_net_game.c _Static_assert's this
VILLAGER_SNAPSHOT_SIZE = struct.calcsize(VILLAGER_SNAPSHOT_HDR_FMT) + ANIMAL_NUM_MAX * struct.calcsize(
    VILLAGER_SLOT_FMT)  # 12 + 15*8 = 132 bytes

# Friendship/mail sync milestone: PCNetGameFriendshipRequestMsg/PCNetGameFriendshipUpdateMsg/
# PCNetGameFriendshipSnapshotEntryMsg/PCNetGameMailRequestMsg/PCNetGameMailDeliveredMsg
# (pc_net_game.c). ANIMAL_MEMORY_NUM=7 (m_npc.h); Anmplmail_c is 258 bytes as actually compiled
# (the decomp's own doc comment says 0x104=260, but the real compiled layout -- verified against a
# live build, see the milestone's build notes -- is 258; PCNetGame*Msg.letter[] mirrors that real
# value, not the doc comment).
ANMPLMAIL_SIZE = 258
FRIENDSHIP_REQUEST_FMT = "<BBbB"                             # 4 bytes
FRIENDSHIP_UPDATE_FMT = "<BBbB8s8sHHI"                       # 28 bytes
# 292 bytes: C's natural struct alignment adds 2 trailing pad bytes after letter[258] (290 -> next
# multiple of 4); Python's "<" format never auto-pads, so that padding is spelled out explicitly
# with a trailing "2x" -- verified against the real compiled sizeof() (see PCNetGameFriendshipSnapshotEntryMsg's
# _Static_assert in pc_net_game.c), not just hand-computed.
FRIENDSHIP_SNAPSHOT_ENTRY_FMT = f"<BBbB8s8sHHIB3x{ANMPLMAIL_SIZE}s2x"  # 292 bytes
MAIL_REQUEST_FMT = "<B3x298s"                                # 302 bytes; 298 == sizeof(Mail_c)
# 288 bytes: same trailing-pad reasoning as FRIENDSHIP_SNAPSHOT_ENTRY_FMT above (286 -> 288).
MAIL_DELIVERED_FMT = f"<BBbB8s8sHHI{ANMPLMAIL_SIZE}s2x"      # 288 bytes

EMPTY_NO = 0x0000
RSV_NO = 0xFFFF
RSV_SIGNBOARD = 0xFE30

# mFI_UNIT_BASE_SIZE (m_field_info.h) -- one field tile is 40x40 world units.
UNIT_SIZE = 40.0


def tile_center(ut_x, ut_z):
    """Approximates mFI_UtNum2CenterWpos() closely enough: the host's pickup reach tolerance is
    deliberately generous (50-unit radius, 40 vertical)."""
    return ut_x * UNIT_SIZE + UNIT_SIZE / 2.0, 0.0, ut_z * UNIT_SIZE + UNIT_SIZE / 2.0


class MsgSpec:
    """A wire message's struct format + namedtuple of field names. `exact` = payload length must
    equal `size`; otherwise the payload must be at least `size` bytes and only the prefix is
    decoded. `total_size` = exact total length when `fmt` only covers a header."""

    __slots__ = ("msg_type", "fmt", "size", "tuple_cls", "exact", "total_size")

    def __init__(self, msg_type, fmt, size, tuple_cls, exact=True, total_size=None):
        self.msg_type = msg_type
        self.fmt = fmt
        self.size = size
        self.tuple_cls = tuple_cls
        self.exact = exact
        self.total_size = total_size

    def matches(self, payload):
        if not payload or payload[0] != self.msg_type:
            return False
        if self.total_size is not None:
            return len(payload) == self.total_size
        return len(payload) == self.size if self.exact else len(payload) >= self.size

    def decode(self, payload):
        return self.tuple_cls._make(struct.unpack_from(self.fmt, payload, 0))


def build_msg_spec(msg_type, fmt, field_names, type_name=None, exact=True, total_size=None):
    """Builds a MsgSpec, asserting at construction time that `fmt` unpacks to exactly as many
    fields as `field_names` names (catches fmt/field drift loudly)."""
    size = struct.calcsize(fmt)
    tuple_cls = namedtuple(type_name or f"Msg{msg_type}Fields", field_names)
    dummy = struct.unpack(fmt, b"\x00" * size)
    if len(dummy) != len(tuple_cls._fields):
        raise AssertionError(
            f"build_msg_spec(msg_type={msg_type}): fmt {fmt!r} unpacks to {len(dummy)} field(s) "
            f"but field_names {field_names!r} names {len(tuple_cls._fields)} -- these must match "
            "1:1, in order."
        )
    return MsgSpec(msg_type, fmt, size, tuple_cls, exact, total_size)


_RTC_FIELDS = ["rtc_sec", "rtc_min", "rtc_hour", "rtc_day", "rtc_weekday", "rtc_month", "rtc_year"]

IDENTITY_SPEC = build_msg_spec(
    PC_NETGAME_MSG_IDENTITY, IDENTITY_FMT,
    ["msg_type", "has_save", "reserved0", "protocol_version", "player_name", "land_name", "player_id", "land_id",
     "terrain_hash"],
    "IdentityFields")
IDENTITY_ACK_SPEC = build_msg_spec(
    PC_NETGAME_MSG_IDENTITY_ACK, ACK_FMT,
    ["msg_type", "accepted", "assigned_peer_id", "protocol_version", "player_name", "land_name",
     "player_id", "land_id", "terrain_hash"],
    "IdentityAckFields")
REJECT_SPEC = build_msg_spec(  # decodes the common 8-byte prefix of both REJECT forms
    PC_NETGAME_MSG_REJECT, REJECT_FMT, ["msg_type", "reason", "reserved", "expected_protocol_version"],
    "RejectFields", exact=False)
REJECT_TOWN_SPEC = build_msg_spec(
    PC_NETGAME_MSG_REJECT, REJECT_TOWN_FMT,
    ["msg_type", "reason", "reserved0", "expected_protocol_version", "land_name", "land_id", "reserved1",
     "terrain_hash"],
    "RejectTownFields")
MOVE_SPEC = build_msg_spec(
    PC_NETGAME_MSG_MOVE, MOVE_FMT,
    ["msg_type", "net_player_id", "move_state", "item_kind", "frame", "pos_x", "pos_y", "pos_z",
     "facing_angle", "action_state", "speed"],
    "MoveFields")
APPEARANCE_SPEC = build_msg_spec(
    PC_NETGAME_MSG_APPEARANCE, APPEARANCE_HDR_FMT,
    ["msg_type", "net_player_id", "gender", "face", "cloth_item", "sunburn_rank", "is_custom_design"],
    "AppearanceHdrFields", total_size=APPEARANCE_MSG_SIZE)
PICKUP_REQUEST_SPEC = build_msg_spec(
    PC_NETGAME_MSG_PICKUP_REQUEST, PICKUP_REQUEST_FMT, ["msg_type", "ut_x", "ut_z", "reserved", "request_id"],
    "PickupRequestFields")
PICKUP_RESULT_SPEC = build_msg_spec(
    PC_NETGAME_MSG_PICKUP_RESULT, PICKUP_RESULT_FMT,
    ["msg_type", "accepted", "ut_x", "ut_z", "request_id", "granted_item", "reserved"],
    "PickupResultFields")
FIELD_UPDATE_SPEC = build_msg_spec(
    PC_NETGAME_MSG_FIELD_UPDATE, FIELD_UPDATE_FMT,
    ["msg_type", "grid", "acre", "tile", "flags", "reserved0", "value", "world_seq"],
    "FieldUpdateFields")
DROP_REQUEST_SPEC = build_msg_spec(
    PC_NETGAME_MSG_DROP_REQUEST, DROP_REQUEST_FMT,
    ["msg_type", "pocket_slot_idx", "ut_x", "ut_z", "claimed_item", "reserved", "request_id"],
    "DropRequestFields")
DROP_RESULT_SPEC = build_msg_spec(
    PC_NETGAME_MSG_DROP_RESULT, DROP_RESULT_FMT,
    ["msg_type", "accepted", "ut_x", "ut_z", "request_id", "placed_item", "reserved"],
    "DropResultFields")
BURY_REQUEST_SPEC = build_msg_spec(
    PC_NETGAME_MSG_BURY_REQUEST, BURY_REQUEST_FMT,
    ["msg_type", "pocket_slot_idx", "ut_x", "ut_z", "claimed_item", "hole_variant", "reserved", "request_id"],
    "BuryRequestFields")
BURY_RESULT_SPEC = build_msg_spec(
    PC_NETGAME_MSG_BURY_RESULT, BURY_RESULT_FMT,
    ["msg_type", "accepted", "ut_x", "ut_z", "request_id", "buried_item", "flags", "reason"],
    "BuryResultFields")
PLAYER_CONTEXT_SPEC = build_msg_spec(
    PC_NETGAME_MSG_PLAYER_CONTEXT, PLAYER_CONTEXT_FMT,
    ["msg_type", "player_no", "destiny_type", "flags", "money_power", "goods_power"], "PlayerContextFields")
SNAPSHOT_BEGIN_SPEC = build_msg_spec(
    PC_NETGAME_MSG_SNAPSHOT_BEGIN, SNAPSHOT_BEGIN_FMT,
    ["msg_type", "grid", "acre_count", "reserved0", "epoch", "world_seq"], "SnapshotBeginFields")
FIELD_BLOCK_SPEC = build_msg_spec(  # header only; decode_world_msg() unpacks the arrays
    PC_NETGAME_MSG_FIELD_BLOCK, FIELD_BLOCK_HDR_FMT, ["msg_type", "grid", "acre", "flags", "epoch", "world_seq"],
    "FieldBlockHdrFields", total_size=FIELD_BLOCK_SIZE)
SNAPSHOT_END_SPEC = build_msg_spec(
    PC_NETGAME_MSG_SNAPSHOT_END, SNAPSHOT_END_FMT,
    ["msg_type", "grid", "acre_count", "flags", "epoch", "world_seq"] + _RTC_FIELDS + _WORLD_STATE_FIELDS,
    "SnapshotEndFields")
WORLD_META_SPEC = build_msg_spec(
    PC_NETGAME_MSG_WORLD_META, WORLD_META_FMT,
    ["msg_type", "flags", "reserved0", "world_seq"] + _RTC_FIELDS + _WORLD_STATE_FIELDS,
    "WorldMetaFields")
RESYNC_REQUEST_SPEC = build_msg_spec(
    PC_NETGAME_MSG_RESYNC_REQUEST, RESYNC_REQUEST_FMT, ["msg_type", "grid", "reason", "reserved0"],
    "ResyncRequestFields")
INTERACT_CONFIRM_SPEC = build_msg_spec(
    PC_NETGAME_MSG_INTERACT_CONFIRM, INTERACT_CONFIRM_FMT, ["msg_type", "kind", "outcome", "reason", "request_id"],
    "InteractConfirmFields")
VILLAGER_ARRIVAL_SPEC = build_msg_spec(
    PC_NETGAME_MSG_VILLAGER_ARRIVAL, VILLAGER_ARRIVAL_FMT,
    ["msg_type", "slot", "reserved_block_x", "reserved_block_z", "reserved_ut_x", "reserved_ut_z", "now_npc_max",
     "reserved0", "npc_id", "reserved1", "world_seq"],
    "VillagerArrivalFields")
VILLAGER_DEPARTURE_SPEC = build_msg_spec(
    PC_NETGAME_MSG_VILLAGER_DEPARTURE, VILLAGER_DEPARTURE_FMT,
    ["msg_type", "slot", "now_npc_max", "reserved0", "world_seq"], "VillagerDepartureFields")
VILLAGER_SNAPSHOT_SPEC = build_msg_spec(  # header only; decode_world_msg() unpacks the slots[] array
    PC_NETGAME_MSG_VILLAGER_SNAPSHOT, VILLAGER_SNAPSHOT_HDR_FMT,
    ["msg_type", "grid", "now_npc_max", "reserved0", "epoch", "world_seq"], "VillagerSnapshotHdrFields",
    total_size=VILLAGER_SNAPSHOT_SIZE)
VillagerSlotFields = namedtuple("VillagerSlotFields",
                                 ["npc_id", "occupied", "is_home", "home_block_x", "home_block_z", "home_ut_x",
                                  "home_ut_z"])

# Friendship/mail sync milestone specs.
FRIENDSHIP_REQUEST_SPEC = build_msg_spec(
    PC_NETGAME_MSG_FRIENDSHIP_REQUEST, FRIENDSHIP_REQUEST_FMT, ["msg_type", "slot", "delta", "reserved0"],
    "FriendshipRequestFields")
FRIENDSHIP_UPDATE_SPEC = build_msg_spec(
    PC_NETGAME_MSG_FRIENDSHIP_UPDATE, FRIENDSHIP_UPDATE_FMT,
    ["msg_type", "slot", "friendship", "reserved0", "player_name", "land_name", "player_id", "land_id", "world_seq"],
    "FriendshipUpdateFields")
FRIENDSHIP_SNAPSHOT_ENTRY_SPEC = build_msg_spec(
    PC_NETGAME_MSG_FRIENDSHIP_SNAPSHOT_ENTRY, FRIENDSHIP_SNAPSHOT_ENTRY_FMT,
    ["msg_type", "slot", "friendship", "letter_info", "player_name", "land_name", "player_id", "land_id",
     "world_seq", "has_letter", "letter"],
    "FriendshipSnapshotEntryFields")
MAIL_REQUEST_SPEC = build_msg_spec(
    PC_NETGAME_MSG_MAIL_REQUEST, MAIL_REQUEST_FMT, ["msg_type", "mail"], "MailRequestFields")
MAIL_DELIVERED_SPEC = build_msg_spec(
    PC_NETGAME_MSG_MAIL_DELIVERED, MAIL_DELIVERED_FMT,
    ["msg_type", "slot", "friendship", "letter_info", "player_name", "land_name", "player_id", "land_id",
     "world_seq", "letter"],
    "MailDeliveredFields")

PLAYER_ACTION_SPEC = build_msg_spec(
    PC_NETGAME_MSG_PLAYER_ACTION, PLAYER_ACTION_FMT,
    ["msg_type", "net_player_id", "kind", "flags", "ut_x", "ut_z", "item", "seq"], "PlayerActionFields")
assert PLAYER_ACTION_SPEC.size == 10
RECORD_HELLO_SPEC = build_msg_spec(
    PC_NETGAME_MSG_RECORD_HELLO, RECORD_HELLO_FMT,
    ["msg_type", "flags", "reserved0", "record_size", "local_digest", "last_host_session", "last_epoch", "last_rev"],
    "RecordHelloFields")
RECORD_BEGIN_SPEC = build_msg_spec(
    PC_NETGAME_MSG_RECORD_BEGIN, RECORD_BEGIN_FMT,
    ["msg_type", "kind", "chunk_count", "rsv", "xfer_id", "epoch", "rev", "total_size", "digest", "host_session"],
    "RecordBeginFields")
RECORD_CHUNK_SPEC = build_msg_spec(
    PC_NETGAME_MSG_RECORD_CHUNK, RECORD_CHUNK_FMT,
    ["msg_type", "chunk_idx", "length", "xfer_id", "offset", "rsv", "data"], "RecordChunkFields")
RECORD_ACK_SPEC = build_msg_spec(
    PC_NETGAME_MSG_RECORD_ACK, RECORD_ACK_FMT,
    ["msg_type", "status", "detail", "xfer_id", "epoch", "rev", "host_session"], "RecordAckFields")
assert RECORD_HELLO_SPEC.size == 24 and RECORD_BEGIN_SPEC.size == 28
assert RECORD_CHUNK_SPEC.size == 1012 and RECORD_ACK_SPEC.size == 20
assert max(RECORD_HELLO_SPEC.size, RECORD_BEGIN_SPEC.size, RECORD_CHUNK_SPEC.size, RECORD_ACK_SPEC.size) <= PC_NET_MAX_PAYLOAD
assert PC_NETGAME_REC_CHUNK_COUNT * PC_NETGAME_REC_CHUNK_DATA >= PC_NETGAME_REC_SIZE > (PC_NETGAME_REC_CHUNK_COUNT - 1) * PC_NETGAME_REC_CHUNK_DATA

class TxnSpec(MsgSpec):
    """MsgSpec of an X1 message whose 15-entry u16 pocket array (struct items 14..28) is collapsed into ONE tuple field."""

    __slots__ = ()
    POCKET_AT = 14

    def decode(self, payload):
        raw = struct.unpack_from(self.fmt, payload, 0)
        a = self.POCKET_AT
        return self.tuple_cls._make(raw[:a] + (tuple(raw[a:a + 15]),) + raw[a + 15:])


def build_txn_spec(msg_type, fmt, field_names, type_name):
    size = struct.calcsize(fmt)
    tuple_cls = namedtuple(type_name, field_names)
    raw = struct.unpack(fmt, b"\x00" * size)
    if len(raw) - 14 != len(field_names):
        raise AssertionError(f"build_txn_spec({msg_type}): fmt unpacks to {len(raw)} items, 15 collapse into 1 -> expected "
                             f"{len(raw) - 14} names, got {len(field_names)}")
    return TxnSpec(msg_type, fmt, size, tuple_cls, True, None)


TXN_COMMIT_SPEC = build_txn_spec(
    PC_NETGAME_MSG_TXN_COMMIT, TXN_COMMIT_FMT,
    ["msg_type", "kind", "rsv0", "request_id", "txn_nonce", "txn_seq", "dest", "slot", "item", "flags", "aux_cond", "aux_item",
     "base_epoch", "base_rev", "pre_pockets", "tag_rsv0", "pre_conds", "pre_wallet"], "TxnCommitFields")
TXN_RESULT_SPEC = build_txn_spec(
    PC_NETGAME_MSG_TXN_RESULT, TXN_RESULT_FMT,
    ["msg_type", "kind", "outcome", "reason", "request_id", "txn_nonce", "txn_seq", "host_session", "epoch", "rev", "cdig",
     "dest", "slot", "item", "post_pockets", "svc_seq16", "post_conds", "post_wallet"], "TxnResultFields")
TOWN_SVC_STATE_SPEC = build_msg_spec(
    PC_NETGAME_MSG_TOWN_SVC_STATE, TOWN_SVC_STATE_FMT, ["msg_type", "service", "len", "seq", "digest"], "TownSvcStateFields", exact=False)
assert TOWN_SVC_STATE_SPEC.size == 12 and 12 + PC_NETGAME_TS_BLOB_MAX <= PC_NET_MAX_PAYLOAD
MAILBOX_LETTER_SPEC = build_msg_spec(
    PC_NETGAME_MSG_MAILBOX_LETTER, MAILBOX_LETTER_FMT,
    ["msg_type", "house", "mbox_idx", "flags", "seq", "digest", "used_count", "rsv0", "letter", "rsv1"], "MailboxLetterFields")
assert MAILBOX_LETTER_SPEC.size == 316 and MAILBOX_LETTER_SPEC.size <= PC_NET_MAX_PAYLOAD
IDENTITY_EXT_SPEC = build_msg_spec(
    PC_NETGAME_MSG_IDENTITY_EXT, IDENTITY_EXT_FMT,
    ["msg_type", "flags", "reserved0", "home_player_name", "home_land_name", "home_player_id", "home_land_id", "token_present", "token",
     "reserved1"], "IdentityExtFields")
IDENTITY_TOKEN_SPEC = build_msg_spec(
    PC_NETGAME_MSG_IDENTITY_TOKEN, IDENTITY_TOKEN_FMT, ["msg_type", "flags", "guest_slot", "table_size", "token"], "IdentityTokenFields")
assert IDENTITY_EXT_SPEC.size == 42 and IDENTITY_TOKEN_SPEC.size == 20 and max(IDENTITY_EXT_SPEC.size, IDENTITY_TOKEN_SPEC.size) <= PC_NET_MAX_PAYLOAD
assert PC_NETGAME_TS_BLOB_MAX >= max(PC_NETGAME_TS_POLICE_LEN, PC_NETGAME_TS_MUSEUM_LEN, PC_NETGAME_TS_SHOP_LEN)
assert struct.calcsize(TXN_TAG_FMT) == 64 and TXN_COMMIT_SPEC.size == 72 and TXN_RESULT_SPEC.size == 76
assert max(TXN_COMMIT_SPEC.size, TXN_RESULT_SPEC.size) <= PC_NET_MAX_PAYLOAD
assert struct.calcsize(TXN_COMMIT_FMT) == 8 + struct.calcsize(TXN_TAG_FMT)

GAME_SPECS = {
    s.msg_type: s
    for s in (IDENTITY_SPEC, IDENTITY_ACK_SPEC, REJECT_SPEC, MOVE_SPEC, APPEARANCE_SPEC, PICKUP_REQUEST_SPEC,
              PICKUP_RESULT_SPEC, FIELD_UPDATE_SPEC, DROP_REQUEST_SPEC, DROP_RESULT_SPEC, PLAYER_CONTEXT_SPEC,
              SNAPSHOT_BEGIN_SPEC, FIELD_BLOCK_SPEC, SNAPSHOT_END_SPEC, WORLD_META_SPEC, RESYNC_REQUEST_SPEC,
              INTERACT_CONFIRM_SPEC, VILLAGER_ARRIVAL_SPEC, VILLAGER_DEPARTURE_SPEC, VILLAGER_SNAPSHOT_SPEC,
              FRIENDSHIP_REQUEST_SPEC, FRIENDSHIP_UPDATE_SPEC, FRIENDSHIP_SNAPSHOT_ENTRY_SPEC, MAIL_REQUEST_SPEC,
              MAIL_DELIVERED_SPEC, BURY_REQUEST_SPEC, BURY_RESULT_SPEC, PLAYER_ACTION_SPEC, RECORD_HELLO_SPEC,
              RECORD_BEGIN_SPEC, RECORD_CHUNK_SPEC, RECORD_ACK_SPEC, TXN_COMMIT_SPEC, TXN_RESULT_SPEC, TOWN_SVC_STATE_SPEC,
              MAILBOX_LETTER_SPEC, IDENTITY_EXT_SPEC, IDENTITY_TOKEN_SPEC)
}
assert IDENTITY_SPEC.size == 32 and IDENTITY_ACK_SPEC.size == 32 and REJECT_TOWN_SPEC.size == 24
assert FIELD_UPDATE_SPEC.size == 12 and PLAYER_CONTEXT_SPEC.size == 8 and SNAPSHOT_BEGIN_SPEC.size == 12
assert SNAPSHOT_END_SPEC.size == 52 and WORLD_META_SPEC.size == 48 and RESYNC_REQUEST_SPEC.size == 4
assert INTERACT_CONFIRM_SPEC.size == 8 and PICKUP_REQUEST_SPEC.size == 8
assert FIELD_BLOCK_SPEC.size + 2 * (256 + 16 + 16) == FIELD_BLOCK_SIZE
assert VILLAGER_ARRIVAL_SPEC.size == 16 and VILLAGER_DEPARTURE_SPEC.size == 8
assert VILLAGER_SNAPSHOT_SPEC.size + ANIMAL_NUM_MAX * struct.calcsize(VILLAGER_SLOT_FMT) == VILLAGER_SNAPSHOT_SIZE
assert VILLAGER_SNAPSHOT_SIZE == 132
assert FRIENDSHIP_REQUEST_SPEC.size == 4 and FRIENDSHIP_UPDATE_SPEC.size == 28
assert FRIENDSHIP_SNAPSHOT_ENTRY_SPEC.size == 292 and MAIL_REQUEST_SPEC.size == 302
assert MAIL_DELIVERED_SPEC.size == 288
assert BURY_REQUEST_SPEC.size == 12 and BURY_RESULT_SPEC.size == 12

# Messages that answer a request, keyed by msg_type -> result spec (used for rid matching).
RESULT_SPECS = {PC_NETGAME_MSG_PICKUP_RESULT: PICKUP_RESULT_SPEC, PC_NETGAME_MSG_DROP_RESULT: DROP_RESULT_SPEC,
                PC_NETGAME_MSG_BURY_RESULT: BURY_RESULT_SPEC}
WORLD_MSG_TYPES = (PC_NETGAME_MSG_FIELD_UPDATE, PC_NETGAME_MSG_SNAPSHOT_BEGIN, PC_NETGAME_MSG_FIELD_BLOCK,
                   PC_NETGAME_MSG_SNAPSHOT_END, PC_NETGAME_MSG_WORLD_META, PC_NETGAME_MSG_VILLAGER_ARRIVAL,
                   PC_NETGAME_MSG_VILLAGER_DEPARTURE, PC_NETGAME_MSG_VILLAGER_SNAPSHOT,
                   PC_NETGAME_MSG_FRIENDSHIP_UPDATE, PC_NETGAME_MSG_FRIENDSHIP_SNAPSHOT_ENTRY,
                   PC_NETGAME_MSG_MAIL_DELIVERED)


def decode_game(payload):
    """Decoded namedtuple for a known game message (by msg_type + size rule), else None."""
    if not payload:
        return None
    spec = GAME_SPECS.get(payload[0])
    if spec is None or not spec.matches(payload):
        return None
    return spec.decode(payload)


# --- identity ------------------------------------------------------------------------------------

TownIdentity = namedtuple("TownIdentity", "land_name land_id terrain_hash")
PlayerIdentity = namedtuple("PlayerIdentity", "player_name player_id has_save")

ZERO_TOWN = TownIdentity(b"\x00" * 8, 0, 0)
DEFAULT_PLAYER = PlayerIdentity(b"FAKECLI\x00", 0x7E57, 1)  # has_save must be 1 (0 -> REJECT NO_SAVE)
ZERO_PLAYER = DEFAULT_PLAYER  # phase-1 name kept for compatibility
PROBE_TOWN = TownIdentity(b"NOTOWN\x00\x00", 0xFFFF, 0xDEADBEEF)  # deliberately wrong: the host must REJECT it


def build_identity(town=ZERO_TOWN, player=None, protocol_version=None):
    """v2 IDENTITY: u8 type; u8 has_save; u16 res; u32 protocol_version; name[8]; land[8]; u16
    player_id; u16 land_id; u32 terrain_hash."""
    if protocol_version is None:
        protocol_version = PC_NETGAME_PROTOCOL_VERSION
    if player is None:
        player = DEFAULT_PLAYER  # looked up at CALL time: _init_default_player() below may replace it
    return struct.pack(IDENTITY_FMT, PC_NETGAME_MSG_IDENTITY, player.has_save & 0xFF, 0, protocol_version & U32_MASK,
                       bytes(player.player_name)[:8].ljust(8, b"\x00"),
                       bytes(town.land_name)[:8].ljust(8, b"\x00"),
                       player.player_id & 0xFFFF, town.land_id & 0xFFFF, town.terrain_hash & U32_MASK)


def town_from_identity_ack(ack):
    return TownIdentity(bytes(ack.land_name), ack.land_id, ack.terrain_hash)


def decode_reject_town(payload):
    """The 24-byte LAND_MISMATCH / NO_SAVE REJECT (decoded) or None."""
    if not REJECT_TOWN_SPEC.matches(payload):
        return None
    return REJECT_TOWN_SPEC.decode(payload)


def town_from_reject(payload):
    """The HOST's town identity carried by a 24-byte REJECT, or None."""
    r = decode_reject_town(payload)
    return None if r is None else TownIdentity(bytes(r.land_name), r.land_id, r.terrain_hash)


def is_land_mismatch_reject(rej):
    return rej.reason == PC_NETGAME_REJECT_LAND_MISMATCH


def field_update_tuple(fields):
    """(ut_x, ut_z, value) from a decoded v2 FIELD_UPDATE (grid, acre, tile), so assertions keep
    comparing town-scene coordinates exactly as the pickup/drop requests use them."""
    ut = acre_tile_to_town_ut(fields.acre, fields.tile)
    if ut is None:
        return (None, None, fields.value)
    return (ut[0], ut[1], fields.value)


# --- builders --------------------------------------------------------------------------------------

def build_move(frame, x, y, z, angle=0, speed=0.0, move_state=1, item_kind=-1, net_player_id=0, action_state=0,
               main_index=None, entry_counter=0):
    """PCNetMoveMsg. `action_state` is the raw u16 (v7); or pass `main_index` (+ `entry_counter`) to encode it."""
    if main_index is not None:
        action_state = move_action_state(main_index, entry_counter)
    return struct.pack(MOVE_FMT, PC_NETGAME_MSG_MOVE, net_player_id, move_state, item_kind, frame & U32_MASK,
                       x, y, z, angle, action_state & 0xFFFF, speed)


class F32Bits(int):
    """A float32 given as its raw IEEE-754 bit pattern (for NaN payloads / signalling NaN / -NaN that a
    python float cannot express reliably). Accepted wherever build_move_any() takes a float."""


F32_QNAN = F32Bits(0x7FC00000)
F32_NEG_QNAN = F32Bits(0xFFC00000)
F32_SNAN = F32Bits(0x7FA00000)
F32_POS_INF = F32Bits(0x7F800000)
F32_NEG_INF = F32Bits(0xFF800000)


def _f32_bytes(v):
    if isinstance(v, F32Bits):
        return struct.pack("<I", int(v) & U32_MASK)
    return struct.pack("<f", v)


def build_move_any(frame, x, y, z, angle=0, speed=0.0, move_state=1, item_kind=-1, net_player_id=0, action_state=0):
    """build_move() whose x/y/z/speed may be python floats (incl. nan/inf/1e30) or F32Bits raw bit patterns.
    Byte-identical to build_move() for ordinary floats."""
    head = struct.pack("<BBBbI", PC_NETGAME_MSG_MOVE, net_player_id, move_state, item_kind, frame & U32_MASK)
    tail = struct.pack("<hH", angle, action_state & 0xFFFF)
    return head + _f32_bytes(x) + _f32_bytes(y) + _f32_bytes(z) + tail + _f32_bytes(speed)


def build_interact_confirm(kind, request_id, outcome=CONFIRM_OUTCOME_COMMIT, reason=CONFIRM_REASON_NONE):
    """INTERACT_CONFIRM (type 17, 8 bytes): u8 type, u8 kind, u8 outcome, u8 reason, u32 request_id."""
    return struct.pack(INTERACT_CONFIRM_FMT, PC_NETGAME_MSG_INTERACT_CONFIRM, kind & 0xFF, outcome & 0xFF,
                       reason & 0xFF, request_id & U32_MASK)


def build_txn_commit(kind, request_id, nonce, seq, dest, slot, item, base_epoch, base_rev, pre_pockets, pre_conds, pre_wallet,
                     flags=0, aux_cond=0, aux_item=0, rsv0=0, tag_rsv0=0):
    """The 72-byte TXN_COMMIT (X1). pre_pockets = 15 NATIVE u16 values. Every field can be set arbitrarily (malformed tests)."""
    assert len(pre_pockets) == 15
    return struct.pack(TXN_COMMIT_FMT, PC_NETGAME_MSG_TXN_COMMIT, kind & 0xFF, rsv0 & 0xFFFF, request_id & U32_MASK,
                       nonce & U32_MASK, seq & U32_MASK, dest & 0xFF, slot & 0xFF, item & 0xFFFF, flags & 0xFF, aux_cond & 0xFF,
                       aux_item & 0xFFFF, base_epoch & U32_MASK, base_rev & U32_MASK, *[p & 0xFFFF for p in pre_pockets],
                       tag_rsv0 & 0xFFFF, pre_conds & U32_MASK, pre_wallet & U32_MASK)


def fish_item_for_species(species):
    """The item a caught fish of bobber species `species` becomes (aUKI_get_fish_type()'s fish_data[], duplicated host-side as
    pcnetgame_x3_fish_item()): FISH00 + n for n < 40, whale -> FISH39, the three trash kinds, SALMON2 -> FISH22. 0 if out of range."""
    if 0 <= species < 40:
        return 0x2300 + species
    return {40: 0x2300 + 39, 41: 0x2500 + 14, 42: 0x2500 + 15, 43: 0x2500 + 16, 44: 0x2300 + 22}.get(species, 0)


def bug_item_for_species(species):
    """The item of a caught bug: ITM_INSECT00 + species (0x2D00 + n), the spirit is ITM_SPIRIT0 (0x2D28)."""
    return 0x2D00 + species if 0 <= species <= 40 else 0


def build_txn_tag(nonce, seq, dest, slot, item, base_epoch, base_rev, pre_pockets, pre_conds, pre_wallet,
                  flags=0, aux_cond=0, aux_item=0, tag_rsv0=0):
    """The 64-byte reusable PCNetGameTxnTag (X1/X3). pre_pockets = 15 NATIVE u16 values."""
    assert len(pre_pockets) == 15
    return struct.pack(TXN_TAG_FMT, nonce & U32_MASK, seq & U32_MASK, dest & 0xFF, slot & 0xFF, item & 0xFFFF, flags & 0xFF,
                       aux_cond & 0xFF, aux_item & 0xFFFF, base_epoch & U32_MASK, base_rev & U32_MASK,
                       *[p & 0xFFFF for p in pre_pockets], tag_rsv0 & 0xFFFF, pre_conds & U32_MASK, pre_wallet & U32_MASK)


ZERO_TXN_TAG = bytes(64)
assert struct.calcsize(TXN_TAG_FMT) == 64


def build_field_action_request(kind, ut_x, ut_z, request_id, hole_variant=0, tag=None, rsv0=0, rsv1=0):
    """The 76-byte FIELD_ACTION_REQUEST (X3). tag=None -> the all-zero tag (a request that grants nothing)."""
    return struct.pack("<BBBBIBBH", PC_NETGAME_MSG_FIELD_ACTION_REQUEST, kind & 0xFF, ut_x & 0xFF, ut_z & 0xFF,
                       request_id & U32_MASK, hole_variant & 0xFF, rsv0 & 0xFF, rsv1 & 0xFFFF) + (ZERO_TXN_TAG if tag is None else tag)


def build_catch_request(entity_id, generation, request_id, claimed_species, tag=None):
    """The 84-byte CATCH_REQUEST (X3); the tag is mandatory on the host (tag=None sends an all-zero one: a BAD_SHAPE probe)."""
    return struct.pack("<B3xIIIi", PC_NETGAME_MSG_CATCH_REQUEST, entity_id & U32_MASK, generation & U32_MASK,
                       request_id & U32_MASK, claimed_species) + (ZERO_TXN_TAG if tag is None else tag)


def build_pickup_request(ut_x, ut_z, request_id):
    return struct.pack(PICKUP_REQUEST_FMT, PC_NETGAME_MSG_PICKUP_REQUEST, ut_x & 0xFF, ut_z & 0xFF, 0,
                       request_id & U32_MASK)


def build_drop_request(pocket_slot_idx, claimed_item, ut_x, ut_z, request_id):
    return struct.pack(DROP_REQUEST_FMT, PC_NETGAME_MSG_DROP_REQUEST, pocket_slot_idx, ut_x & 0xFF, ut_z & 0xFF,
                       claimed_item & 0xFFFF, 0, request_id & U32_MASK)


def build_bury_request(pocket_slot_idx, claimed_item, ut_x, ut_z, request_id, hole_variant=0xFF):
    """World Ecology T3: PC_NETGAME_MSG_BURY_REQUEST (31, 12 bytes). hole_variant defaults to the 0xFF
    ("no valid hole shape") sentinel -- pass 0..24 for the one sub-case where it is authoritative
    (a pitfall buried into a HOLE_SHINE tile -- see PCNetGameBuryRequestMsg's own doc)."""
    return struct.pack(BURY_REQUEST_FMT, PC_NETGAME_MSG_BURY_REQUEST, pocket_slot_idx & 0xFF, ut_x & 0xFF,
                       ut_z & 0xFF, claimed_item & 0xFFFF, hole_variant & 0xFF, 0, request_id & U32_MASK)


def build_friendship_request(slot, delta):
    """PC_NETGAME_MSG_FRIENDSHIP_REQUEST (21, 4 bytes): client -> host only. `delta` is a signed
    byte (matches Anmmem_c.friendship's own s8 storage)."""
    return struct.pack(FRIENDSHIP_REQUEST_FMT, PC_NETGAME_MSG_FRIENDSHIP_REQUEST, slot & 0xFF, delta & 0xFF, 0)


def build_mail_request(mail_bytes):
    """PC_NETGAME_MSG_MAIL_REQUEST (24, 302 bytes): client -> host only. `mail_bytes` must be
    exactly 298 bytes (sizeof(Mail_c)); this is a raw/opaque copy, never decoded here -- a test that
    needs a specific recipient/sender should build those 298 bytes itself (see
    test_friendship_mail_sync.py for a minimal fixture) or just exercise malformed-size rejection."""
    if len(mail_bytes) != 298:
        raise ValueError(f"build_mail_request: mail_bytes must be exactly 298 bytes, got {len(mail_bytes)}")
    return struct.pack(MAIL_REQUEST_FMT, PC_NETGAME_MSG_MAIL_REQUEST, mail_bytes)


def build_player_context(flags=DEFAULT_CONTEXT_FLAGS, player_no=0, destiny_type=0, money_power=0, goods_power=0):
    return struct.pack(PLAYER_CONTEXT_FMT, PC_NETGAME_MSG_PLAYER_CONTEXT, player_no & 0xFF, destiny_type & 0xFF,
                       flags & 0xFF, money_power, goods_power)


def build_resync_request(reason=PC_NETGAME_RESYNC_REASON_SAVE_RELOADED, grid=PC_NETGAME_GRID_TOWN):
    return struct.pack(RESYNC_REQUEST_FMT, PC_NETGAME_MSG_RESYNC_REQUEST, grid, reason, 0)


# =================================================================================================
# 7. FakeClient
# =================================================================================================

REJECT_REASON_NAMES = {1: "PROTOCOL_MISMATCH", 2: "SERVER_FULL", 3: "LAND_MISMATCH", 4: "NO_SAVE"}


class HandshakeRejected(RuntimeError):
    def __init__(self, label, reject, payload):
        super().__init__(f"{label}: host REJECTed the handshake (reason={reject.reason} "
                         f"{REJECT_REASON_NAMES.get(reject.reason, '?')}, host protocol={reject.expected_protocol_version})")
        self.reject = reject
        self.payload = payload


HANDSHAKE_TIMEOUT_S = 5.0   # generous: a v2 host defers IDENTITY until its own save is loaded
SNAPSHOT_TIMEOUT_S = 10.0
RECORD_SYNC_TIMEOUT_S = 10.0


class FakeClient(TransportClient):
    """One hand-crafted v2 game client: transport connect -> IDENTITY (claiming the host's town) ->
    IDENTITY_ACK -> READY -> PLAYER_CONTEXT(IN_TOWN) -> wait for the initial world snapshot.

    Feature-specific tests subclass this (see test_appearance_sync.py) or use it directly.
      town           claimed TownIdentity (default: probed from the host, cached)
      context_flags  PLAYER_CONTEXT flags sent after READY (None = send no context)
      wait_snapshot  connect_and_ready() waits for SNAPSHOT_END (the world is synced) by default
    """

    # Two-phase pickup/drop (INTERACT_CONFIRM). Default: like a real client with room in its pockets / the item
    # still in its slot, the moment an accepted (provisional) RESULT is delivered the client sends
    # CONFIRM(COMMIT) -- so tests written for the old single-phase protocol keep working unchanged. Tests that
    # need the reservation to stay pending / be aborted / expire pass auto_confirm=False (per call, or set
    # `client.auto_confirm = False` for the whole client). Each (kind, request_id, connection) is auto-confirmed
    # at most once, so a replayed RESULT never produces a second CONFIRM.
    auto_confirm = True

    # D3 (v8) record protocol defaults: a FakeClient behaves like a v8 client that is compatible with the host's HELLO
    # enforcement: it sends RECORD_HELLO right after IDENTITY_ACK (record_hello=False suppresses it), answers a
    # MIGRATE_REQUEST by uploading its resident's GCI record and adopts/ACKs pushes automatically (record_auto=False
    # turns the automatic answers off so a test can drive the exchange by hand), and connect_and_ready() waits until the
    # host reports the record synced (record_wait=False skips that wait).
    record_hello = True
    record_auto = True
    record_wait = True

    # Guests G1: a FakeClient with `guest` set plays a GUEST: it sends IDENTITY_EXT (guest flag, its home PersonalID, the token it holds) BEFORE its IDENTITY, claims
    # the HOST's town + the guest's own name / player id in the IDENTITY, uses `guest_record(...)` as its local record (MIGRATE payload), stamps RECORD_BEGIN.rsv with
    # record_class GUEST and, like the real client, remembers a token the host sends (IDENTITY_TOKEN, flag NEW) in `guest_token` -- the test double of save/mp/guest_token.dat.
    guest = None
    guest_token = None
    guest_send_ext = True
    guest_ext_flags = PC_NETGAME_IDEXT_FLAG_GUEST
    guest_record_img = None

    # X1 commit path of an accepted provisional RESULT: "txn" (the DEFAULT since X1b, like the real client: PC_NETGAME_TXN_RETIRE_LEGACY_COMMIT
    # == 1) = TXN_COMMIT + the TXN_RESULT; "legacy" = INTERACT_CONFIRM(COMMIT), which the host now RETIRES (logged, reservation released,
    # nothing mutated) -- kept only so a test can prove exactly that.
    commit_mode = "txn"
    txn_pickup_dest = "pocket"   # auto-commit of a pickup in "txn" mode: "pocket" (first free slot) or "wallet"

    def __init__(self, label, host_ip, port, town=None, player=None, hub=None, context_flags=DEFAULT_CONTEXT_FLAGS,
                 wait_snapshot=True, record_hello=None, record_auto=None, record_wait=None, commit_mode=None, guest=None,
                 guest_token=None, guest_record_img=None, guest_send_ext=None, **kw):
        if guest is not None:
            self.guest = guest
            player = player or guest_player(guest)
            if guest_record_img is not None:
                self.guest_record_img = guest_record_img
        if guest_token is not None:
            self.guest_token = guest_token
        if guest_send_ext is not None:
            self.guest_send_ext = guest_send_ext
        self.token_msgs = []            # (conn, IdentityTokenFields) for EVERY IDENTITY_TOKEN received
        if record_hello is not None:
            self.record_hello = record_hello
        if record_auto is not None:
            self.record_auto = record_auto
        if record_wait is not None:
            self.record_wait = record_wait
        self.rec_last = None            # (host_session, epoch, rev) this client last synced with; survives reconnects
        self.rec_resident_idx = None    # resident slot of self.player in the test GCI (lazy)
        self.rec_migrate_payload = None  # override of the MIGRATE_UPLOAD bytes (default: own_record())
        self.rec_local = None           # the client's current local record image (BE bytes)
        # X1 transactional commit (test double of the real client's s_txn_nonce / s_txn_next_seq): the nonce is per PROCESS
        # (survives reconnect(); new_process() re-rolls it), the seq is monotone per process and never reset by a session reset.
        if commit_mode is not None:
            self.commit_mode = commit_mode
        self.txn_nonce = new_txn_nonce()
        self.txn_seq = 0
        self.txn_results = []        # (conn, TxnResultFields) for EVERY TXN_RESULT received, arrival order
        self.txn_commits_sent = []   # (raw bytes, conn) for EVERY TXN_COMMIT this client sent
        self.ts_states = []          # (conn, TownSvcStateFields, blob bytes) for EVERY TOWN_SVC_STATE received (town services), arrival order
        self.mbox_log = []           # (conn, MailboxLetterFields, inbox index) for EVERY MAILBOX_LETTER received (mail milestone 2), arrival order
        self.take_ctx = {}           # (nonce, seq) -> (dst mail slot, letter BE bytes) of a MAIL_TAKE this client sent (what its apply step writes on APPLIED)
        self.txn_requests = {}       # (kind, request_id) -> (slot, item): what the auto-commit needs for drop/bury
        super().__init__(label, host_ip, port, hub=hub, **kw)
        self.host_ip = host_ip
        self.port = int(port)
        self.town = town
        self.player = player or next_default_player(self)
        self.context_flags = context_flags
        self.wait_snapshot = wait_snapshot
        self.assigned_peer_id = None
        self.identity_ack = None
        self.reject = None
        self.move_frame = 0
        self.result_log = []  # (msg_type, request_id, fields, conn) for EVERY result ever delivered
        self._reset_record_tracking()
        self.confirm_policy = {}   # (kind, request_id) -> bool, per-request override of auto_confirm
        self.confirms_sent = []    # (kind, request_id, outcome, reason, conn) for EVERY CONFIRM this client sent
        self._auto_confirmed = set()  # (kind, request_id, conn) already auto-confirmed
        self._reset_world_tracking()

    # --- world tracking (mirrors the real client's rules in pcnetgame_handle_client_*) --------------

    def _reset_connection_state(self):
        super()._reset_connection_state()
        self._reset_world_tracking()  # a new connection starts with no world knowledge at all
        self._reset_record_tracking()

    def _reset_record_tracking(self):
        self.rec_xfer_counter = 0   # next client -> host xfer_id is rec_xfer_counter + 1 (per connection)
        self.rec_rx = None          # the host -> client transfer being reassembled
        self.rec_pushes = []        # completed host pushes this connection: dicts (kind, epoch, rev, session, data, digest_ok, ...)
        self.rec_acks = []          # every RECORD_ACK received this connection: (conn, RecordAckFields)
        self.rec_violations = []    # reassembly protocol problems seen on pushes
        self.rec_synced = False     # PUSH_FULL fully received (digest ok) or HELLO answered APPLIED (continuation)
        self.rec_migrate_epoch = None

    def _reset_world_tracking(self):
        self.world = WorldView()  # what a correct client believes, per contract section 4
        self.snapshots = []       # one dict per SNAPSHOT_BEGIN seen on this connection
        self.world_log = []       # [(inbox_index, kind, obj)] every world message, delivery order
        self.superseded_blocks = []  # in-snapshot blocks whose epoch != the latest BEGIN (ignored)
        self.live_blocks = []     # flush FIELD_BLOCKs (flags 0) applied outside a snapshot
        self.meta_log = []        # WORLD_META dicts
        # Villager population/is_home milestone: every ARRIVAL/DEPARTURE dict ever applied, in
        # delivery order, plus a believed per-slot view (slot -> dict, or absent if empty/unknown)
        # built the same way the real client would (VILLAGER_SNAPSHOT replaces the whole view;
        # ARRIVAL/DEPARTURE update one slot; all three obey world_seq staleness exactly like the C
        # client -- see _apply_world() below).
        self.villager_log = []
        self.villager_snapshot_log = []
        self.villager_slots = {}
        self._villager_seq = 0
        # Friendship/mail sync milestone: every FRIENDSHIP_UPDATE/FRIENDSHIP_SNAPSHOT_ENTRY/
        # MAIL_DELIVERED ever applied, in delivery order, plus a believed per-(slot, player_id,
        # land_id) view -- world_seq staleness mirrors the C client exactly (strict `>` for
        # UPDATE/MAIL_DELIVERED, `>=` for SNAPSHOT_ENTRY -- see _apply_world() below).
        self.friendship_log = []
        self.mail_log = []
        self.friendship_memory = {}  # (slot, player_id, land_id) -> {"friendship", "letter_info", "letter"}
        self._friendship_seq = 0

    def _current_snapshot(self):
        return self.snapshots[-1] if self.snapshots and self.snapshots[-1]["end"] is None else None

    def _apply_world(self, m, kind, obj):
        self.world_log.append((m.index, kind, obj))
        cur = self._current_snapshot()
        if kind == WORLD_SNAPSHOT_BEGIN:
            if cur is not None:
                cur["superseded"] = True
            self.snapshots.append({"conn": m.conn, "epoch": obj["epoch"], "begin": obj, "end": None, "blocks": [],
                                   "block_seqs": {}, "deltas_during": 0, "superseded": False,
                                   "begin_index": m.index})
        elif kind == WORLD_SNAPSHOT_END:
            if cur is None or obj["epoch"] != cur["epoch"]:
                self.world.violations.append(f"SNAPSHOT_END epoch {obj['epoch']} without a matching BEGIN")
            else:
                cur["end"] = obj
                cur["end_index"] = m.index
        elif kind == WORLD_BLOCK:
            if obj.flags & PC_NETGAME_FB_FLAG_IN_SNAPSHOT:
                if cur is None or obj.epoch != cur["epoch"]:
                    self.superseded_blocks.append(obj)
                    return
                cur["blocks"].append(obj.acre)
                cur["block_seqs"][obj.acre] = obj.world_seq
            else:
                self.live_blocks.append(obj)
            self.world.apply_block(obj)
        elif kind == WORLD_DELTA:
            if cur is not None:
                cur["deltas_during"] += 1
            self.world.apply_delta(obj)
        elif kind == WORLD_META:
            self.meta_log.append(obj)
        elif kind == WORLD_VILLAGER_ARRIVAL:
            self.villager_log.append(("arrival", obj))
            if obj["world_seq"] > self._villager_seq:
                self._villager_seq = obj["world_seq"]
                self.villager_slots[obj["slot"]] = {"npc_id": obj["npc_id"], "is_home": True,
                                                    "home_block_x": obj["reserved_block_x"],
                                                    "home_block_z": obj["reserved_block_z"],
                                                    "home_ut_x": obj["reserved_ut_x"],
                                                    "home_ut_z": obj["reserved_ut_z"] + 1}
        elif kind == WORLD_VILLAGER_DEPARTURE:
            self.villager_log.append(("departure", obj))
            if obj["world_seq"] > self._villager_seq:
                self._villager_seq = obj["world_seq"]
                self.villager_slots.pop(obj["slot"], None)
        elif kind == WORLD_VILLAGER_SNAPSHOT:
            self.villager_snapshot_log.append(obj)
            if cur is None or obj["epoch"] != cur["epoch"]:
                return  # part of a superseded snapshot -- the real client discards it the same way
            if obj["world_seq"] >= self._villager_seq:
                self._villager_seq = obj["world_seq"]
                self.villager_slots = {}
                for i, s in enumerate(obj["slots"]):
                    if s.occupied:
                        self.villager_slots[i] = {"npc_id": s.npc_id, "is_home": bool(s.is_home),
                                                  "home_block_x": s.home_block_x, "home_block_z": s.home_block_z,
                                                  "home_ut_x": s.home_ut_x, "home_ut_z": s.home_ut_z}
        elif kind == WORLD_FRIENDSHIP_UPDATE:
            self.friendship_log.append(obj)
            if obj["world_seq"] > self._friendship_seq:
                self._friendship_seq = obj["world_seq"]
                key = (obj["slot"], obj["player_id"], obj["land_id"])
                entry = self.friendship_memory.setdefault(key, {})
                entry["friendship"] = obj["friendship"]
        elif kind == WORLD_FRIENDSHIP_SNAPSHOT_ENTRY:
            self.friendship_log.append(obj)
            if obj["world_seq"] >= self._friendship_seq:
                if obj["world_seq"] > self._friendship_seq:
                    self._friendship_seq = obj["world_seq"]
                key = (obj["slot"], obj["player_id"], obj["land_id"])
                entry = self.friendship_memory.setdefault(key, {})
                entry["friendship"] = obj["friendship"]
                entry["letter_info"] = obj["letter_info"]
                if obj["has_letter"]:
                    entry["letter"] = obj["letter"]
        elif kind == WORLD_MAIL_DELIVERED:
            self.mail_log.append(obj)
            if obj["world_seq"] > self._friendship_seq:
                self._friendship_seq = obj["world_seq"]
                key = (obj["slot"], obj["player_id"], obj["land_id"])
                entry = self.friendship_memory.setdefault(key, {})
                entry["friendship"] = obj["friendship"]
                entry["letter_info"] = obj["letter_info"]
                entry["letter"] = obj["letter"]

    def snapshot_complete(self):
        return bool(self.snapshots) and self.snapshots[-1]["conn"] == self.connect_count \
            and self.snapshots[-1]["end"] is not None

    def completed_snapshots(self):
        return [s for s in self.snapshots if s["conn"] == self.connect_count and s["end"] is not None]

    def wait_snapshot_complete(self, timeout=SNAPSHOT_TIMEOUT_S, after_count=0):
        """True once this connection has completed more than `after_count` snapshots."""
        return self.hub.wait_until(lambda: len(self.completed_snapshots()) > after_count, timeout)

    # --- handshake ------------------------------------------------------------------------------

    def claimed_town(self):
        if self.town is not None:
            return self.town
        if TOWN_CHECK_ENFORCED:
            return resolve_host_town(self.host_ip, self.port, hub=self.hub)
        return ZERO_TOWN

    @property
    def record_class(self):
        return PC_NETGAME_REC_CLASS_GUEST if self.guest is not None else PC_NETGAME_REC_CLASS_RESIDENT

    def send_identity_ext(self, token="held", flags=None, raw=None, **kw):
        """IDENTITY_EXT (guests G1). token: "held" = the token this client holds (guest_token), None = none, or 16 bytes."""
        if raw is not None:
            return self.send_reliable(raw)
        tk = self.guest_token if token == "held" else token
        return self.send_reliable(build_identity_ext(self.guest, tk, self.guest_ext_flags if flags is None else flags, **kw))

    def send_identity(self, protocol_version=None, town=None, player=None):
        payload = build_identity(town if town is not None else self.claimed_town(),
                                 player if player is not None else self.player, protocol_version)
        return self.send_reliable(payload)

    def send_player_context(self, flags=DEFAULT_CONTEXT_FLAGS, **kw):
        return self.send_reliable(build_player_context(flags, **kw))

    def send_resync_request(self, reason=PC_NETGAME_RESYNC_REASON_SAVE_RELOADED):
        return self.send_reliable(build_resync_request(reason))

    def wait_handshake_reply(self, timeout=HANDSHAKE_TIMEOUT_S):
        """IDENTITY_ACK or REJECT, or None. Leaves every other message in the inbox."""
        m = self.inbox.wait_for(
            lambda m: m.channel == CH_RELIABLE and m.msg_type in (PC_NETGAME_MSG_IDENTITY_ACK, PC_NETGAME_MSG_REJECT)
            and m.game is not None, timeout)
        return m

    def connect_and_ready(self, timeout=HANDSHAKE_TIMEOUT_S, protocol_version=None, quiet=False, wait_snapshot=None):
        """Transport connect + IDENTITY + wait IDENTITY_ACK (+ PLAYER_CONTEXT + initial snapshot).
        Raises HandshakeRejected on REJECT and RuntimeError on silence / missing snapshot."""
        self.town_claimed = self.claimed_town()  # probe BEFORE connecting (probe uses its own slot)
        self.connect(timeout=min(timeout, 3.0))
        self.assigned_peer_id = None
        self.identity_ack = None
        self.reject = None
        if self.guest is not None and self.guest_send_ext:
            self.send_identity_ext()
        self.send_identity(protocol_version=protocol_version, town=self.town_claimed)
        m = self.wait_handshake_reply(timeout)
        if m is None:
            raise RuntimeError(f"{self.label}: no IDENTITY_ACK received -- not READY")
        g = m.game
        if m.msg_type == PC_NETGAME_MSG_REJECT:
            self.reject = g
            raise HandshakeRejected(self.label, g, m.payload)
        self.identity_ack = g
        self.identity_ack_seq = m.seq
        self.identity_ack_index = m.index
        self.assigned_peer_id = g.assigned_peer_id
        if self.record_hello:
            self.send_record_hello()
        self.post_ready_handshake(self.wait_snapshot if wait_snapshot is None else wait_snapshot)
        if self.record_hello and self.record_auto and self.record_wait and not self.wait_record_synced(RECORD_SYNC_TIMEOUT_S):
            raise RuntimeError(f"{self.label}: READY but the resident record was not synced within {RECORD_SYNC_TIMEOUT_S}s "
                               f"(acks: {[(a.status, a.xfer_id) for _c, a in self.rec_acks]}, pushes: {len(self.rec_pushes)})")
        if not quiet:
            print(f"[{self.label}] READY (assigned_peer_id={self.assigned_peer_id})")
        return self

    def post_ready_handshake(self, wait_snapshot=True):
        """v2: PLAYER_CONTEXT right after READY (as the real client does), then optionally wait for
        the initial snapshot so the test starts from a synced world."""
        if self.context_flags is not None:
            self.send_player_context(self.context_flags)
        if wait_snapshot and not self.wait_snapshot_complete(SNAPSHOT_TIMEOUT_S):
            raise RuntimeError(f"{self.label}: READY but the initial world snapshot did not complete within "
                               f"{SNAPSHOT_TIMEOUT_S}s (snapshots seen: {len(self.snapshots)})")

    def reconnect_and_ready(self, new_socket=False, timeout=HANDSHAKE_TIMEOUT_S, same_address_restart=False,
                            wait_snapshot=None):
        """Full reconnect with fresh transport state + fresh handshake. same_address_restart=True
        skips DISCONNECT (host sees a new nonce from the same address)."""
        if same_address_restart:
            self.state = self.STATE_IDLE
        elif self.state in (self.STATE_CONNECTED, self.STATE_PENDING):
            self.disconnect()
            self.hub.sleep(0.05)
        if new_socket:
            self.new_socket()
        return self.connect_and_ready(timeout=timeout, wait_snapshot=wait_snapshot)

    # --- sends --------------------------------------------------------------------------------------

    def claim_position(self, x, y, z, facing=0):
        """Movement sample claiming this exact position/facing (Stage 3 performs no anti-cheat, so
        a fake client may stand anywhere -- used to satisfy pickup/drop reach checks)."""
        self.move_frame += 1
        msg = build_move(self.move_frame, x, y, z, angle=facing, speed=0.0, move_state=0, item_kind=-1)
        return self.send_reliable(msg) if CLAIM_POSITION_RELIABLE else self.send_unreliable(msg)

    def send_move(self, frame, x, y, z, angle=0, speed=0.0, move_state=1, item_kind=-1, reliable=False,
                  action_state=0, main_index=None, entry_counter=0):
        """v7: `action_state` (raw u16) or `main_index`/`entry_counter` (encoded) fill the MOVE action field."""
        msg = build_move(frame, x, y, z, angle=angle, speed=speed, move_state=move_state, item_kind=item_kind,
                         action_state=action_state, main_index=main_index, entry_counter=entry_counter)
        return self.send_reliable(msg) if reliable else self.send_unreliable(msg)

    def send_move_any(self, x, y, z, speed=0.0, facing=0, reliable=True, frame=None, move_state=0):
        """A MOVE with an arbitrary float32 position/speed (nan/inf/1e30 or F32Bits) and a FRESH frame number
        (a stale frame would be dropped by the host for the wrong reason). Reliable by default so it stays
        ordered before the pickup/drop request that follows."""
        if frame is None:
            self.move_frame += 1
            frame = self.move_frame
        msg = build_move_any(frame, x, y, z, angle=facing, speed=speed, move_state=move_state, item_kind=-1)
        return self.send_reliable(msg) if reliable else self.send_unreliable(msg)

    def send_pickup_request(self, ut_x, ut_z, request_id, auto_confirm=None):
        if auto_confirm is not None:
            self.confirm_policy[(CONFIRM_KIND_PICKUP, request_id & U32_MASK)] = bool(auto_confirm)
        return self.send_reliable(build_pickup_request(ut_x, ut_z, request_id))

    def send_drop_request(self, pocket_slot_idx, claimed_item, ut_x, ut_z, request_id, auto_confirm=None):
        self.txn_requests[(CONFIRM_KIND_DROP, request_id & U32_MASK)] = (pocket_slot_idx, claimed_item)
        if auto_confirm is not None:
            self.confirm_policy[(CONFIRM_KIND_DROP, request_id & U32_MASK)] = bool(auto_confirm)
        return self.send_reliable(build_drop_request(pocket_slot_idx, claimed_item, ut_x, ut_z, request_id))

    def send_bury_request(self, pocket_slot_idx, claimed_item, ut_x, ut_z, request_id, hole_variant=0xFF,
                          auto_confirm=None):
        self.txn_requests[(CONFIRM_KIND_BURY, request_id & U32_MASK)] = (pocket_slot_idx, claimed_item)
        if auto_confirm is not None:
            self.confirm_policy[(CONFIRM_KIND_BURY, request_id & U32_MASK)] = bool(auto_confirm)
        return self.send_reliable(
            build_bury_request(pocket_slot_idx, claimed_item, ut_x, ut_z, request_id, hole_variant))

    def send_friendship_request(self, slot, delta):
        """Raw protocol-level FRIENDSHIP_REQUEST (bypasses the real game's mNpc_AddFriendship() --
        useful for malformed-slot/stale-style protocol tests; see test_friendship_mail_sync.py)."""
        return self.send_reliable(build_friendship_request(slot, delta))

    def send_mail_request(self, mail_bytes):
        """Raw protocol-level MAIL_REQUEST (bypasses the real game's mNpc_SendMailtoNpc())."""
        return self.send_reliable(build_mail_request(mail_bytes))

    def confirm(self, kind, request_id, outcome=CONFIRM_OUTCOME_COMMIT, reason=CONFIRM_REASON_NONE):
        """Explicit INTERACT_CONFIRM (reliable). Returns the transport seq."""
        self.confirms_sent.append((kind, request_id & U32_MASK, outcome, reason, self.connect_count))
        return self.send_reliable(build_interact_confirm(kind, request_id, outcome, reason))

    def send_confirm_raw(self, payload):
        """A hand-built (possibly malformed) confirm-shaped payload, reliable."""
        return self.send_reliable(payload)

    def send_appearance(self, payload):
        return self.send_reliable(payload)

    # --- results, matched strictly by request_id -------------------------------------------------------

    # --- D3 record protocol (client side of the wire, as a test double) --------------------------------------

    def own_record(self):
        """This client's resident record as the test GCI holds it (the 'local GCI' a real client would import on MIGRATE).
        A GUEST client's is its guest_record (the home record a real foreigner client carries)."""
        if self.guest is not None:
            if self.guest_record_img is None:
                self.guest_record_img = guest_record(self.guest)
            return self.guest_record_img
        if self.rec_resident_idx is None:
            for i, ident, _ex in read_test_save_residents():
                if ident.player_id == self.player.player_id and ident.player_name == self.player.player_name:
                    self.rec_resident_idx = i
                    break
        if self.rec_resident_idx is None:
            raise LookupError(f"{self.label}: no resident of the test GCI matches {self.player}")
        return record_from_gci(GAME_BIN_DIR, self.rec_resident_idx)

    def send_record_hello(self, flags=None, last=None, record_size=PC_NETGAME_REC_SIZE, local_digest=None, raw=None):
        """RECORD_HELLO (reliable). Defaults: last = what this client last synced (rec_last), local_digest of rec_local."""
        if raw is not None:
            return self.send_reliable(raw)
        last = last if last is not None else self.rec_last
        have = last is not None
        if flags is None:
            flags = PC_NETGAME_REC_HELLO_FLAG_HAVE_LAST if have else 0
        if local_digest is None:
            try:
                local_digest = fnv1a32(self.rec_local if self.rec_local is not None else self.own_record())
            except (LookupError, OSError):
                local_digest = 0
        sess, ep, rv = last if have else (0, 0, 0)
        return self.send_reliable(struct.pack(RECORD_HELLO_FMT, PC_NETGAME_MSG_RECORD_HELLO, flags, 0, record_size,
                                              local_digest & U32_MASK, sess, ep, rv))

    def send_record_begin(self, kind, xfer_id, epoch, rev, total_size=PC_NETGAME_REC_SIZE, digest=0,
                          chunk_count=PC_NETGAME_REC_CHUNK_COUNT, rsv=None, host_session=0):
        if rsv is None:
            rsv = self.record_class  # RECORD_BEGIN.rsv = record_class (0 resident, 1 guest)
        return self.send_reliable(struct.pack(RECORD_BEGIN_FMT, PC_NETGAME_MSG_RECORD_BEGIN, kind, chunk_count, rsv,
                                              xfer_id & U32_MASK, epoch & U32_MASK, rev & U32_MASK, total_size,
                                              digest & U32_MASK, host_session))

    def send_record_chunk(self, chunk_idx, xfer_id, data, length=None, offset=None, rsv=0):
        """One RECORD_CHUNK; `data` is padded/truncated to the fixed 1000-byte field. Defaults for length/offset are the
        correct values for a well-formed chunk of a full record."""
        total = PC_NETGAME_REC_SIZE
        if offset is None:
            offset = chunk_idx * PC_NETGAME_REC_CHUNK_DATA
        if length is None:
            length = min(PC_NETGAME_REC_CHUNK_DATA, max(0, total - chunk_idx * PC_NETGAME_REC_CHUNK_DATA))
        body = bytes(data[:PC_NETGAME_REC_CHUNK_DATA]).ljust(PC_NETGAME_REC_CHUNK_DATA, b"\x00")
        return self.send_reliable(struct.pack(RECORD_CHUNK_FMT, PC_NETGAME_MSG_RECORD_CHUNK, chunk_idx & 0xFF, length & 0xFFFF,
                                              xfer_id & U32_MASK, offset & 0xFFFF, rsv & 0xFFFF, body))

    def send_record_ack(self, status, xfer_id=0, detail=0, epoch=0, rev=0, host_session=0):
        return self.send_reliable(struct.pack(RECORD_ACK_FMT, PC_NETGAME_MSG_RECORD_ACK, status, detail, xfer_id & U32_MASK,
                                              epoch & U32_MASK, rev & U32_MASK, host_session))

    def next_record_xfer_id(self):
        self.rec_xfer_counter += 1
        return self.rec_xfer_counter

    def upload_record(self, data, base=None, kind=PC_NETGAME_REC_KIND_UPLOAD, xfer_id=None, digest=None, rsv=None):
        """BEGIN + all chunks of `data` (BE bytes) as client -> host transfer `xfer_id` (default: next id). `base` = (epoch,
        rev) the upload builds on (default: this client's last synced point). `digest` defaults to fnv1a32(data). Returns the
        xfer_id used. Needs no ACK wait: use wait_record_ack(xfer_id)."""
        if xfer_id is None:
            xfer_id = self.next_record_xfer_id()
        else:
            self.rec_xfer_counter = max(self.rec_xfer_counter, xfer_id)
        if base is None:
            base = (self.rec_last[1], self.rec_last[2]) if self.rec_last else (0, 0)
        if digest is None:
            digest = fnv1a32(data)
        n = (len(data) + PC_NETGAME_REC_CHUNK_DATA - 1) // PC_NETGAME_REC_CHUNK_DATA
        self.send_record_begin(kind, xfer_id, base[0], base[1], total_size=len(data), digest=digest, chunk_count=n, rsv=rsv)
        for i in range(n):
            self.send_record_chunk(i, xfer_id, data[i * PC_NETGAME_REC_CHUNK_DATA:(i + 1) * PC_NETGAME_REC_CHUNK_DATA],
                                   length=min(PC_NETGAME_REC_CHUNK_DATA, len(data) - i * PC_NETGAME_REC_CHUNK_DATA),
                                   offset=i * PC_NETGAME_REC_CHUNK_DATA)
        return xfer_id

    def wait_record_ack(self, xfer_id=None, status=None, timeout=3.0, consume=True):
        """The next RECORD_ACK on this connection (optionally for `xfer_id` / with `status`), or None."""
        conn = self.connect_count

        def pred(m):
            if m.conn != conn or m.channel != CH_RELIABLE or m.msg_type != PC_NETGAME_MSG_RECORD_ACK:
                return False
            g = m.game
            return g is not None and (xfer_id is None or g.xfer_id == xfer_id) and (status is None or g.status == status)

        m = self.inbox.wait_for(pred, timeout, consume=consume)
        return None if m is None else m.game

    def wait_record_push(self, after=0, timeout=5.0):
        """Block until more than `after` completed pushes exist on this connection; returns the newest (dict) or None."""
        if not self.hub.wait_until(lambda: len(self.rec_pushes) > after, timeout):
            return None
        return self.rec_pushes[-1]

    def recv_record(self, after=0, timeout=5.0):
        """Reassembled host push (dict: kind, epoch, rev, session, data, digest, digest_ok, xfer) or None."""
        return self.wait_record_push(after, timeout)

    def wait_record_synced(self, timeout=RECORD_SYNC_TIMEOUT_S):
        return self.hub.wait_until(lambda: self.rec_synced, timeout)

    def _record_on_message(self, m):
        t = m.msg_type
        g = m.game
        if g is None or m.conn != self.connect_count:
            return
        if t == PC_NETGAME_MSG_RECORD_BEGIN:
            self.rec_rx = {"kind": g.kind, "xfer": g.xfer_id, "epoch": g.epoch, "rev": g.rev, "session": g.host_session, "rsv": g.rsv,
                           "digest": g.digest, "count": g.chunk_count, "total": g.total_size, "chunks": {}}
        elif t == PC_NETGAME_MSG_RECORD_CHUNK:
            rx = self.rec_rx
            if rx is None or rx["xfer"] != g.xfer_id or g.chunk_idx >= rx["count"] or g.chunk_idx in rx["chunks"] \
                    or g.offset != g.chunk_idx * PC_NETGAME_REC_CHUNK_DATA:
                self.rec_violations.append(("chunk", g.chunk_idx, g.xfer_id))
                return
            rx["chunks"][g.chunk_idx] = bytes(g.data[:g.length])
            if len(rx["chunks"]) == rx["count"]:
                data = b"".join(rx["chunks"][i] for i in range(rx["count"]))
                push = dict(rx, data=data, digest_ok=(len(data) == rx["total"] == PC_NETGAME_REC_SIZE
                                                      and fnv1a32(data) == rx["digest"]), conn=m.conn)
                self.rec_rx = None
                self.rec_pushes.append(push)
                if push["digest_ok"]:
                    self.rec_last = (push["session"], push["epoch"], push["rev"])
                    self.rec_local = data
                    if push["kind"] == PC_NETGAME_REC_KIND_PUSH_FULL:
                        self.rec_synced = True
                    if self.record_auto and getattr(self, "state", None) == self.STATE_CONNECTED:
                        self.send_record_ack(PC_NETGAME_REC_ACK_APPLIED, push["xfer"], 0, push["epoch"], push["rev"],
                                             push["session"])
        elif t == PC_NETGAME_MSG_RECORD_ACK:
            self.rec_acks.append((m.conn, g))
            if g.status == PC_NETGAME_REC_ACK_MIGRATE_REQUEST:
                self.rec_migrate_epoch = g.epoch
                if self.record_auto and getattr(self, "state", None) == self.STATE_CONNECTED:
                    payload = self.rec_migrate_payload if self.rec_migrate_payload is not None else self.own_record()
                    self.upload_record(payload, base=(g.epoch, 0), kind=PC_NETGAME_REC_KIND_MIGRATE_UPLOAD)
            elif g.status == PC_NETGAME_REC_ACK_APPLIED:
                self.rec_last = (g.host_session, g.epoch, g.rev)
                if g.xfer_id == 0:
                    self.rec_synced = True  # HELLO continuation answered without a push

    # --- X1 transactional commit (client side of the wire, as a test double) ---------------------------------------

    def new_process(self, keep_record=False):
        """Pretend this client is a NEW game process: a new random txn nonce, seq restarts at 1, and (unless keep_record) nothing
        is remembered about the previous record sync (a real new process never claims a continuation). keep_record=True models
        the real client's nonce RE-ROLL on a seq wrap, which keeps its record state."""
        self.txn_nonce = new_txn_nonce()
        self.txn_seq = 0
        if not keep_record:
            self.rec_last = None
            self.rec_local = None

    def next_txn_seq(self):
        self.txn_seq = (self.txn_seq + 1) & U32_MASK
        return self.txn_seq

    def txn_pre_image(self):
        """(pockets tuple of 15 native u16, item_conditions, wallet) of this client's current local record image."""
        return record_inventory(self.rec_local if self.rec_local is not None else self.own_record())

    def build_txn_commit_bytes(self, kind, request_id, dest, slot, item, pre=None, base=None, seq=None, nonce=None, **kw):
        """The COMMIT bytes. Default pre-image: this client's local image (drop/bury: forced to hold `item` at `slot`, a legal
        client-owned claim); default base: what it last synced (rec_last); seq = next_txn_seq(); nonce = self.txn_nonce."""
        if pre is None:
            pockets, conds, wallet = self.txn_pre_image()
            if kind in (CONFIRM_KIND_DROP, CONFIRM_KIND_BURY, PC_NETGAME_TXN_KIND_MUSEUM_DONATE) and 0 <= slot < 15:
                pockets = tuple(item if i == slot else p for i, p in enumerate(pockets))
            pre = (pockets, conds, wallet)
        if base is None:
            base = (self.rec_last[1], self.rec_last[2]) if self.rec_last else (0, 0)
        if seq is None:
            seq = self.next_txn_seq()
        if nonce is None:
            nonce = self.txn_nonce
        return build_txn_commit(kind, request_id, nonce, seq, dest, slot, item, base[0], base[1], pre[0], pre[1], pre[2], **kw)

    def send_txn_commit(self, kind, request_id, dest, slot, item, pre=None, base=None, seq=None, nonce=None, raw=None, **kw):
        """Builds (unless `raw` is given) and sends a TXN_COMMIT (reliable). Returns TxnSent(raw, seq, nonce)."""
        if raw is None:
            raw = self.build_txn_commit_bytes(kind, request_id, dest, slot, item, pre=pre, base=base, seq=seq, nonce=nonce, **kw)
        self.txn_commits_sent.append((raw, self.connect_count))
        self.send_reliable(raw)
        return TxnSent(raw, struct.unpack_from("<I", raw, 12)[0] if len(raw) >= 16 else 0,
                       struct.unpack_from("<I", raw, 8)[0] if len(raw) >= 12 else 0)

    def resend_txn(self, sent, copies=1):
        """Resends the IDENTICAL bytes of an earlier COMMIT (what the real client does on a timeout) as a NEW reliable message;
        copies > 1 additionally transmits that datagram `copies` times (transport-level duplicates)."""
        if copies <= 1:
            self.send_reliable(sent.raw)
        else:
            self.send_reliable_duplicate(sent.raw, copies)
        self.txn_commits_sent.append((sent.raw, self.connect_count))
        return sent

    def drop_next_txn_result(self):
        """Client-side loss hook: the next inbound TXN_RESULT is dropped before it reaches the inbox (and not acked, so the host's
        transport retransmits it). Complements the host's --txn-fault=drop_result (the host never SENDS it)."""
        self.drop_inbound_next(lambda payload: bool(payload) and payload[0] == PC_NETGAME_MSG_TXN_RESULT)

    def txn_results_for(self, seq, nonce=None, current_conn_only=False):
        n = self.txn_nonce if nonce is None else nonce
        return [g for conn, g in self.txn_results if g.txn_seq == seq and g.txn_nonce == n
                and (not current_conn_only or conn == self.connect_count)]

    def wait_txn_result(self, seq, timeout=3.0, nth=1, nonce=None):
        """The nth TXN_RESULT (default the first) for (nonce, seq), or None after `timeout`. Non-consuming."""
        if not self.hub.wait_until(lambda: len(self.txn_results_for(seq, nonce)) >= nth, timeout):
            return None
        return self.txn_results_for(seq, nonce)[nth - 1]

    def _txn_on_result(self, m, g):
        """Applies an APPLIED result like the real client's apply step: the pocket range / conds / wallet of the local image become
        the host's post-image and the D3 base moves to the result's lineage point."""
        if m.conn != self.connect_count or g.outcome != PC_NETGAME_TXN_OUTCOME_APPLIED or g.txn_nonce != self.txn_nonce:
            return
        if self.rec_local is not None:
            self.rec_local = record_set_inventory(self.rec_local, g.post_pockets, g.post_conds, g.post_wallet)
            if g.kind == PC_NETGAME_TXN_KIND_MAIL_SEND and 0 <= g.slot < REC_MAIL_COUNT:
                # the real client's pcnetgame_txn_apply_mail: the sent letter leaves the slot (mMl_clear_mail bytes == any other unused slot)
                empty = next((record_mail(self.rec_local, i) for i in range(REC_MAIL_COUNT)
                              if i != g.slot and record_mail(self.rec_local, i)[0x2E] == MAIL_FONT_UNUSED), None)
                if empty is not None:
                    self.rec_local = record_set_mail(self.rec_local, g.slot, empty)
            if g.kind == PC_NETGAME_TXN_KIND_MAIL_TAKE and (g.txn_nonce, g.txn_seq) in self.take_ctx:
                # the real client's pcnetgame_txn_apply_take: the verified letter goes into its own mail[dst]
                dst, lt = self.take_ctx[(g.txn_nonce, g.txn_seq)]
                if lt is not None and 0 <= dst < REC_MAIL_COUNT:
                    self.rec_local = record_set_mail(self.rec_local, dst, lt)
        self.rec_last = (g.host_session, g.epoch, g.rev)

    def _auto_txn_commit(self, kind, rid, g):
        """commit_mode == "txn": answer an accepted provisional RESULT with a TXN_COMMIT (once per request, like the auto-confirm)."""
        pockets, _conds, wallet = self.txn_pre_image()
        if kind == CONFIRM_KIND_PICKUP:
            item = g.granted_item
            if self.txn_pickup_dest == "wallet":
                self.send_txn_commit(kind, rid, PC_NETGAME_TXN_DEST_WALLET, PC_NETGAME_TXN_SLOT_WALLET, item)
            else:
                free = next((i for i, p in enumerate(pockets) if p == EMPTY_NO), None)
                pre = None
                if free is None:
                    # Every pocket of the modelled local image is occupied (a long test picks up more items than there are slots). The REAL
                    # client would ABORT(POCKETS_FULL); the scripted client models a player who discarded the item in the last slot instead,
                    # so the exactly-once host behaviour under test stays reachable. (Explicit TXN_COMMITs / txn_pickup() never do this.)
                    free = len(pockets) - 1
                    cleared = tuple(EMPTY_NO if i == free else p for i, p in enumerate(pockets))
                    pre = (cleared, self.txn_pre_image()[1], wallet)
                    if self.rec_local is not None:
                        self.rec_local = record_set_inventory(self.rec_local, cleared, pre[1], wallet)
                self.send_txn_commit(kind, rid, PC_NETGAME_TXN_DEST_POCKET, free, item, pre=pre)
        else:
            slot, item = self.txn_requests.get((kind, rid), (0, 0))
            self.send_txn_commit(kind, rid, PC_NETGAME_TXN_DEST_NONE, slot, item)

    def commit_pending(self, kind, request_id):
        """Explicit commit of an ACCEPTED provisional RESULT of this connection that was left pending (auto_confirm=False): exactly what
        the automatic commit would have sent (TXN_COMMIT in the default 'txn' mode, CONFIRM(COMMIT) in the retired 'legacy' mode)."""
        rid = request_id & U32_MASK
        g = next((g for _t, r, g, conn in reversed(self.result_log)
                  if r == rid and conn == self.connect_count and getattr(g, "accepted", 0)), None)
        if g is None:
            raise RuntimeError(f"{self.label}: no accepted provisional RESULT for request {request_id} on this connection")
        if self.commit_mode == "txn":
            self._auto_txn_commit(kind, rid, g)
        else:
            self.confirm(kind, rid, CONFIRM_OUTCOME_COMMIT, CONFIRM_REASON_NONE)

    def _txn_flow(self, kind, rid, prov, dest, slot, item, timeout, **kw):
        """Shared tail of txn_pickup/txn_drop/txn_bury: TXN_COMMIT for an accepted provisional RESULT, then wait for its RESULT."""
        if prov is None or not prov.accepted:
            return prov, None, None
        sent = self.send_txn_commit(kind, rid, dest, slot, item, **kw)
        return prov, sent, self.wait_txn_result(sent.seq, timeout)

    def txn_pickup(self, ut_x, ut_z, rid, dest="pocket", slot=None, timeout=3.0, claim=True, req_timeout=1.0, **kw):
        """Full pickup transaction through the NEW path: PICKUP_REQUEST -> provisional RESULT -> TXN_COMMIT -> TXN_RESULT.
        Returns (provisional, TxnSent or None, TxnResultFields or None)."""
        prov = self.pickup(ut_x, ut_z, rid, timeout=req_timeout, claim=claim, auto_confirm=False)
        if prov is None or not prov.accepted:
            return prov, None, None
        if dest == "wallet":
            d, s = PC_NETGAME_TXN_DEST_WALLET, PC_NETGAME_TXN_SLOT_WALLET
        else:
            pockets = self.txn_pre_image()[0]
            d, s = PC_NETGAME_TXN_DEST_POCKET, (slot if slot is not None else next((i for i, p in enumerate(pockets) if p == EMPTY_NO), 0))
        return self._txn_flow(CONFIRM_KIND_PICKUP, rid, prov, d, s, prov.granted_item, timeout, **kw)

    def txn_drop(self, pocket_slot_idx, item, ut_x, ut_z, rid, claim_at=None, facing=0, timeout=3.0, req_timeout=1.0, **kw):
        """Full drop transaction: DROP_REQUEST -> provisional RESULT -> TXN_COMMIT -> TXN_RESULT (see txn_pickup)."""
        prov = self.drop(pocket_slot_idx, item, ut_x, ut_z, rid, timeout=req_timeout, claim_at=claim_at, facing=facing,
                         auto_confirm=False)
        return self._txn_flow(CONFIRM_KIND_DROP, rid, prov, PC_NETGAME_TXN_DEST_NONE, pocket_slot_idx, item, timeout, **kw)

    def txn_bury(self, pocket_slot_idx, item, ut_x, ut_z, rid, hole_variant=0xFF, claim=True, timeout=3.0, req_timeout=1.0, **kw):
        """Full bury transaction: BURY_REQUEST -> provisional RESULT -> TXN_COMMIT -> TXN_RESULT (see txn_pickup)."""
        prov = self.bury(pocket_slot_idx, item, ut_x, ut_z, rid, hole_variant=hole_variant, timeout=req_timeout, claim=claim,
                         auto_confirm=False)
        return self._txn_flow(CONFIRM_KIND_BURY, rid, prov, PC_NETGAME_TXN_DEST_NONE, pocket_slot_idx, item, timeout, **kw)

    def build_grant_tag(self, dest, slot, item, pre=None, base=None, seq=None, nonce=None, **kw):
        """(tag bytes, seq, nonce) for an X3 grant request. Default pre-image = this client's local image, default base = rec_last,
        seq = next_txn_seq(), nonce = self.txn_nonce, slot None = the first free slot of the pre-image (0xFF when there is none)."""
        if pre is None:
            pre = self.txn_pre_image()
        if base is None:
            base = (self.rec_last[1], self.rec_last[2]) if self.rec_last else (0, 0)
        if seq is None:
            seq = self.next_txn_seq()
        if nonce is None:
            nonce = self.txn_nonce
        if slot is None:
            slot = next((i for i, p in enumerate(pre[0]) if p == EMPTY_NO), PC_NETGAME_TXN_SLOT_WALLET)
        return build_txn_tag(nonce, seq, dest, slot, item, base[0], base[1], pre[0], pre[1], pre[2], **kw), seq, nonce

    def send_fa_grant(self, kind, ut_x, ut_z, rid, item=0, slot=None, hole_variant=0, dest=PC_NETGAME_TXN_DEST_POCKET, raw=None,
                      **kw):
        """Builds (unless `raw`) and sends an X3 FIELD_ACTION_REQUEST carrying a grant tag (DIG_BURIED item 0 / DIG_HOLE 0x2103 /
        DIG_SHINE 0x2100..0x2102). Returns TxnSent(raw, seq, nonce); wait_txn_result(sent.seq) gives the host's TXN_RESULT."""
        if raw is None:
            tag, seq, nonce = self.build_grant_tag(dest, slot, item, **kw)
            raw = build_field_action_request(kind, ut_x, ut_z, rid, hole_variant, tag)
        self.txn_commits_sent.append((raw, self.connect_count))
        self.send_reliable(raw)
        return TxnSent(raw, struct.unpack_from("<I", raw, 16)[0], struct.unpack_from("<I", raw, 12)[0])

    def catch_request_bytes(self, entity_id, generation, rid, species, item=None, kind="fish", slot=None, dest=PC_NETGAME_TXN_DEST_POCKET,
                            **kw):
        """The 84-byte X3 CATCH_REQUEST (not sent). item None = the host-derived item of `species` for `kind` ("fish" / "bug");
        dest NONE = a full-pockets catch (slot 0xFF, item 0). Default pre-image / base / seq / nonce as build_grant_tag()."""
        if dest == PC_NETGAME_TXN_DEST_NONE:
            slot, item = PC_NETGAME_TXN_SLOT_WALLET, 0
        elif item is None:
            item = fish_item_for_species(species) if kind == "fish" else bug_item_for_species(species)
        tag, _seq, _nonce = self.build_grant_tag(dest, slot, item, **kw)
        return build_catch_request(entity_id, generation, rid, species, tag)

    def send_catch_txn(self, entity_id, generation, rid, species, item=None, slot=None, dest=PC_NETGAME_TXN_DEST_POCKET, raw=None,
                       kind="fish", **kw):
        """Builds (unless `raw`) and sends an X3 CATCH_REQUEST (tag: dest POCKET + the host-derived item, or dest NONE / item 0 for a
        full-pockets catch). Returns TxnSent(raw, seq, nonce)."""
        if raw is None:
            raw = self.catch_request_bytes(entity_id, generation, rid, species, item=item, kind=kind, slot=slot, dest=dest, **kw)
        self.txn_commits_sent.append((raw, self.connect_count))
        self.send_reliable(raw)
        return TxnSent(raw, struct.unpack_from("<I", raw, 24)[0], struct.unpack_from("<I", raw, 20)[0])

    # --- town services (milestone 1): the TOWN_SVC_STATE mirror + the MUSEUM_DONATE / POLICE_CLAIM transactions ---------------------

    def ts_states_of(self, svc, current_conn_only=True):
        """[(TownSvcStateFields, blob)] of every TOWN_SVC_STATE of service `svc` received (arrival order)."""
        return [(g, b) for conn, g, b in self.ts_states if g.service == svc and (not current_conn_only or conn == self.connect_count)]

    def ts_latest(self, svc, current_conn_only=True):
        """(fields, blob) of the newest received TOWN_SVC_STATE of `svc`, or None."""
        a = self.ts_states_of(svc, current_conn_only)
        return a[-1] if a else None

    def wait_ts_state(self, svc, timeout=3.0, min_seq=1, pred=None, current_conn_only=True):
        """Waits for a TOWN_SVC_STATE of `svc` with seq >= min_seq (and pred(blob) if given); returns (fields, blob) or None."""
        def find():
            for g, b in reversed(self.ts_states_of(svc, current_conn_only)):
                if g.seq >= min_seq and (pred is None or pred(b)):
                    return g, b
            return None
        if not self.hub.wait_until(lambda: find() is not None, timeout):
            return None
        return find()

    def txn_donate(self, slot, item, rid=0, timeout=3.0, **kw):
        """MUSEUM_DONATE: TXN_COMMIT kind 8 (dest NONE, slot, item; default pre-image = the local image with `item` forced into `slot`).
        Returns (TxnSent, TxnResultFields or None)."""
        sent = self.send_txn_commit(PC_NETGAME_TXN_KIND_MUSEUM_DONATE, rid, PC_NETGAME_TXN_DEST_NONE, slot, item, **kw)
        return sent, self.wait_txn_result(sent.seq, timeout)

    def txn_claim(self, slot, item, police_idx, rid=0, timeout=3.0, pre=None, **kw):
        """POLICE_CLAIM: TXN_COMMIT kind 9 (dest POCKET, slot = a free pocket slot, item = the expected item, aux_cond = the lost-and-found
        index). Default pre-image = the local image with `slot` forced EMPTY. Returns (TxnSent, TxnResultFields or None)."""
        if pre is None:
            pockets, conds, wallet = self.txn_pre_image()
            pre = (tuple(0 if i == slot else p for i, p in enumerate(pockets)), conds, wallet)
        sent = self.send_txn_commit(PC_NETGAME_TXN_KIND_POLICE_CLAIM, rid, PC_NETGAME_TXN_DEST_POCKET, slot, item, pre=pre,
                                    aux_cond=police_idx, **kw)
        return sent, self.wait_txn_result(sent.seq, timeout)

    def txn_shop_buy(self, slot, item, stock_code, price, rid=0, timeout=3.0, pre=None, **kw):
        """SHOP_BUY: TXN_COMMIT kind 10 (dest POCKET, slot = a FREE pocket slot, item, aux_cond = the stock code, aux_item = the price the client
        expects). Default pre-image = the local image with `slot` forced EMPTY. Returns (TxnSent, TxnResultFields or None)."""
        if pre is None:
            pockets, conds, wallet = self.txn_pre_image()
            pre = (tuple(0 if i == slot else p for i, p in enumerate(pockets)), conds, wallet)
        sent = self.send_txn_commit(PC_NETGAME_TXN_KIND_SHOP_BUY, rid, PC_NETGAME_TXN_DEST_POCKET, slot, item, pre=pre,
                                    aux_cond=stock_code, aux_item=price, **kw)
        return sent, self.wait_txn_result(sent.seq, timeout)

    def txn_shop_sell(self, mask, slot, item, rid=0, timeout=3.0, pre=None, **kw):
        """SHOP_SELL: TXN_COMMIT kind 11 (dest NONE, slot = the primary slot, item = its item, aux_item = the bit mask of every slot sold).
        Default pre-image = the local image with `item` forced into every slot of the mask. Returns (TxnSent, TxnResultFields or None)."""
        if pre is None:
            pockets, conds, wallet = self.txn_pre_image()
            pre = (tuple(item if (mask >> i) & 1 else p for i, p in enumerate(pockets)), conds, wallet)
        sent = self.send_txn_commit(PC_NETGAME_TXN_KIND_SHOP_SELL, rid, PC_NETGAME_TXN_DEST_NONE, slot, item, pre=pre, aux_item=mask, **kw)
        return sent, self.wait_txn_result(sent.seq, timeout)

    # --- mail milestone 1: MAIL_SEND (TXN_COMMIT kind 12) ------------------------------------------------------------------------------

    def mail_tag_fields(self, mail_be, hash24=None):
        """(item, aux_cond, aux_item) of a MAIL_SEND for the letter bytes `mail_be`: the gift echo and the 24-bit hash split low16 / bits 16..23."""
        h = mail_hash24(mail_be) if hash24 is None else hash24
        return mail_present(mail_be), (h >> 16) & 0xFF, h & 0xFFFF

    def write_local_mail(self, slot, mail_be, pockets=None):
        """Models the player writing a letter: the local record image gets `mail_be` in mail[slot] (and optionally new pockets, e.g. the gift taken out
        of a pocket like the letter board's hand overlay does). Returns the new local image (also stored in rec_local)."""
        rec = self.rec_local if self.rec_local is not None else self.own_record()
        rec = record_set_mail(rec, slot, mail_be)
        if pockets is not None:
            _p, conds, wallet = record_inventory(rec)
            rec = record_set_inventory(rec, pockets, conds, wallet)
        self.rec_local = rec
        return rec

    def upload_local_record(self, timeout=4.0, wait_gap=1.7):
        """Uploads rec_local on the current base (an ordinary D3 UPLOAD) and waits for the host's APPLIED ack; on APPLIED rec_last moves to the ack's
        lineage point. Returns the RecordAckFields or None. `wait_gap` seconds are pumped first (the host's 1.5 s gap after an accepted upload)."""
        if wait_gap:
            pump_sleep(wait_gap)
        xid = self.upload_record(self.rec_local)
        return self.wait_record_ack(xid, timeout=timeout)

    def txn_mail_send(self, slot, mail_be=None, rid=0, timeout=3.0, pre=None, upload=True, hash24=None, item=None, wait_gap=1.7, **kw):
        """MAIL_SEND: uploads the local record first (the real client's AWAIT_CLEAN: `upload`, with `mail_be` written into mail[slot] before it),
        then TXN_COMMIT kind 12 (dest NONE, slot, item = the letter's present, aux = the 24-bit letter hash). Default pre-image = the local image's
        pockets. Returns (TxnSent, TxnResultFields or None, upload_ack or None)."""
        ack = None
        if mail_be is not None:
            self.write_local_mail(slot, mail_be)
        if upload:
            ack = self.upload_local_record(wait_gap=wait_gap)
        cur = record_mail(self.rec_local if self.rec_local is not None else self.own_record(), slot) if mail_be is None else mail_be
        it, ac, ai = self.mail_tag_fields(cur, hash24)
        if item is not None:
            it = item
        if pre is None:
            pre = self.txn_pre_image()
        kw.setdefault("aux_cond", ac)
        kw.setdefault("aux_item", ai)
        sent = self.send_txn_commit(PC_NETGAME_TXN_KIND_MAIL_SEND, rid, PC_NETGAME_TXN_DEST_NONE, slot, it, pre=pre, **kw)
        return sent, self.wait_txn_result(sent.seq, timeout), ack

    # --- mail milestone 2: the mailbox shadow (MAILBOX_LETTER, id 56) and MAIL_TAKE (TXN_COMMIT kind 13) ------------------------------------

    def mbox_msgs(self, current_conn_only=True, since=0):
        """[(MailboxLetterFields, inbox index)] of every MAILBOX_LETTER received (this connection only by default), arrival order, index >= since."""
        return [(g, i) for conn, g, i in self.mbox_log if (not current_conn_only or conn == self.connect_count) and i >= since]

    def mbox_state(self, current_conn_only=True):
        """{mailbox idx: the MAILBOX_LETTER with the highest seq} of this connection: the client's shadow of the host mailbox as the real client applies it."""
        st = {}
        for g, _i in self.mbox_msgs(current_conn_only):
            if g.mbox_idx not in st or g.seq > st[g.mbox_idx].seq:
                st[g.mbox_idx] = g
        return st

    def wait_mbox_msgs(self, n, timeout=6.0, since=0):
        """Waits until >= n MAILBOX_LETTERs (index >= since) arrived on this connection; returns the list (possibly shorter on timeout)."""
        self.hub.wait_until(lambda: len(self.mbox_msgs(since=since)) >= n, timeout)
        return self.mbox_msgs(since=since)

    def txn_mail_take(self, mbox_idx, dst, rid=0, timeout=3.0, letter_be=None, hash24=None, item=None, pre=None, **kw):
        """MAIL_TAKE: TXN_COMMIT kind 13 (dest NONE, slot = the mailbox slot, flags = the destination mail[] slot, item = the letter's present, aux = the
        24-bit letter hash). The letter defaults to this client's shadow of that mailbox slot (the latest MAILBOX_LETTER). Default pre-image = the
        local image's pockets. Returns (TxnSent, TxnResultFields or None)."""
        st = self.mbox_state().get(mbox_idx)
        cur = letter_be if letter_be is not None else (bytes(st.letter) if st is not None else bytes(REC_MAIL_SIZE))
        it, ac, ai = self.mail_tag_fields(cur, hash24)
        if item is not None:
            it = item
        if pre is None:
            pre = self.txn_pre_image()
        kw.setdefault("aux_cond", ac)
        kw.setdefault("aux_item", ai)
        kw.setdefault("flags", dst)
        sent = self.send_txn_commit(PC_NETGAME_TXN_KIND_MAIL_TAKE, rid, PC_NETGAME_TXN_DEST_NONE, mbox_idx, it, pre=pre, **kw)
        self.take_ctx[(sent.nonce, sent.seq)] = (dst, cur)
        return sent, self.wait_txn_result(sent.seq, timeout)

    def free_mail_slot(self, skip=()):
        """The first mail[] slot of this client's local record image that is unused (font 0xFF) and not in `skip`, or None."""
        rec = self.rec_local if self.rec_local is not None else self.own_record()
        return next((i for i in range(REC_MAIL_COUNT) if i not in skip and record_mail(rec, i)[0x2E] == MAIL_FONT_UNUSED), None)

    def on_message(self, m):
        if m.channel == CH_RELIABLE and m.msg_type == PC_NETGAME_MSG_IDENTITY_TOKEN:
            g = m.game
            if g is not None:
                self.token_msgs.append((m.conn, g))
                if self.guest is not None and self.guest_token is None and (g.flags & PC_NETGAME_IDTOKEN_FLAG_NEW):
                    self.guest_token = bytes(g.token)  # first contact: remember it (what the real client persists to save/mp/guest_token.dat)
        if m.channel == CH_RELIABLE and m.msg_type == PC_NETGAME_MSG_TOWN_SVC_STATE:
            g = m.game
            if g is not None:
                self.ts_states.append((m.conn, g, bytes(m.payload[12:12 + g.len])))
        if m.channel == CH_RELIABLE and m.msg_type == PC_NETGAME_MSG_MAILBOX_LETTER:
            g = m.game
            if g is not None:
                self.mbox_log.append((m.conn, g, m.index))
        if m.channel == CH_RELIABLE and m.msg_type == PC_NETGAME_MSG_TXN_RESULT:
            g = m.game
            if g is not None:
                self.txn_results.append((m.conn, g))
                self._txn_on_result(m, g)
        if m.channel == CH_RELIABLE and m.msg_type in (PC_NETGAME_MSG_RECORD_BEGIN, PC_NETGAME_MSG_RECORD_CHUNK,
                                                       PC_NETGAME_MSG_RECORD_ACK):
            self._record_on_message(m)
        if m.channel == CH_RELIABLE and m.msg_type in RESULT_SPECS:
            g = m.game
            if g is not None:
                self.result_log.append((m.msg_type, g.request_id, g, m.conn))
                self._maybe_auto_confirm(m, g)
        if WORLD_WIRE_SPEC_AVAILABLE and m.channel == CH_RELIABLE and m.msg_type in WORLD_MSG_TYPES:
            w = decode_world_msg(m.payload)
            if w is not None:
                self._apply_world(m, *w)

    def _maybe_auto_confirm(self, m, g):
        """An accepted RESULT is PROVISIONAL: answer it with CONFIRM(COMMIT) unless this request opted out."""
        if not g.accepted or getattr(self, "state", None) != self.STATE_CONNECTED or m.conn != self.connect_count:
            return
        if m.msg_type == PC_NETGAME_MSG_PICKUP_RESULT:
            kind = CONFIRM_KIND_PICKUP
        elif m.msg_type == PC_NETGAME_MSG_BURY_RESULT:
            kind = CONFIRM_KIND_BURY
        else:
            kind = CONFIRM_KIND_DROP
        rid = g.request_id
        want = self.__dict__.get("confirm_policy", {}).get((kind, rid), self.auto_confirm)
        key = (kind, rid, m.conn)
        if not want or key in self.__dict__.get("_auto_confirmed", ()):
            return
        self._auto_confirmed.add(key)
        if self.commit_mode == "txn":
            self._auto_txn_commit(kind, rid, g)
        else:
            self.confirm(kind, rid, CONFIRM_OUTCOME_COMMIT, CONFIRM_REASON_NONE)

    def count_results(self, msg_type, request_id, current_conn_only=True):
        return sum(1 for t, rid, _g, conn in self.result_log
                   if t == msg_type and rid == request_id and (not current_conn_only or conn == self.connect_count))

    def wait_result(self, msg_type, request_id, timeout=1.0, consume=True):
        """The decoded result for exactly this request_id on the current connection, or None.
        Results for other request ids are left in the inbox untouched."""
        conn = self.connect_count
        m = self.inbox.wait_for(
            lambda m: m.conn == conn and m.channel == CH_RELIABLE and m.msg_type == msg_type
            and m.game is not None and m.game.request_id == request_id,
            timeout, consume=consume)
        return None if m is None else m.game

    def _recv_typed(self, spec, timeout, expect_request_id=None):
        """Compat with the v1 helper: first message matching `spec` (and request id if given)."""
        conn = self.connect_count

        def pred(m):
            if m.conn != conn or m.channel != CH_RELIABLE or not spec.matches(m.payload):
                return False
            return expect_request_id is None or spec.decode(m.payload).request_id == expect_request_id

        m = self.inbox.wait_for(pred, timeout)
        return None if m is None else spec.decode(m.payload)

    def recv_pickup_result(self, timeout=1.0, expect_request_id=None):
        """(accepted, ut_x, ut_z, request_id, granted_item) or None."""
        f = self._recv_typed(PICKUP_RESULT_SPEC, timeout, expect_request_id)
        return None if f is None else (f.accepted, f.ut_x, f.ut_z, f.request_id, f.granted_item)

    def recv_drop_result(self, timeout=1.0, expect_request_id=None):
        """(accepted, ut_x, ut_z, request_id, placed_item) or None."""
        f = self._recv_typed(DROP_RESULT_SPEC, timeout, expect_request_id)
        return None if f is None else (f.accepted, f.ut_x, f.ut_z, f.request_id, f.placed_item)

    def pickup(self, ut_x, ut_z, request_id, timeout=0.6, claim=True, auto_confirm=None):
        """Request + wait for the (provisional) PICKUP_RESULT. auto_confirm=None follows self.auto_confirm (True):
        an accepted result is immediately CONFIRMed(COMMIT). auto_confirm=False leaves the reservation pending so
        the caller can confirm(...)/abort/expire/disconnect it."""
        if claim:
            self.claim_position(*tile_center(ut_x, ut_z))
        self.send_pickup_request(ut_x, ut_z, request_id, auto_confirm=auto_confirm)
        return self.wait_result(PC_NETGAME_MSG_PICKUP_RESULT, request_id, timeout)

    def drop(self, pocket_slot_idx, claimed_item, ut_x, ut_z, request_id, timeout=0.6, claim_at=None, facing=0,
             auto_confirm=None):
        """Request + wait for the (provisional) DROP_RESULT; auto_confirm as in pickup()."""
        if claim_at is not None:
            self.claim_position(*tile_center(*claim_at), facing=facing)
        self.send_drop_request(pocket_slot_idx, claimed_item, ut_x, ut_z, request_id, auto_confirm=auto_confirm)
        return self.wait_result(PC_NETGAME_MSG_DROP_RESULT, request_id, timeout)

    def recv_bury_result(self, timeout=1.0, expect_request_id=None):
        """(accepted, ut_x, ut_z, request_id, buried_item, flags, reason) or None."""
        f = self._recv_typed(BURY_RESULT_SPEC, timeout, expect_request_id)
        return None if f is None else (f.accepted, f.ut_x, f.ut_z, f.request_id, f.buried_item, f.flags, f.reason)

    def bury(self, pocket_slot_idx, claimed_item, ut_x, ut_z, request_id, hole_variant=0xFF, timeout=0.6,
             claim=True, auto_confirm=None):
        """World Ecology T3: request + wait for the (provisional) BURY_RESULT; auto_confirm as in
        pickup()/drop() (an accepted result is immediately CONFIRMed(COMMIT) unless opted out)."""
        if claim:
            self.claim_position(*tile_center(ut_x, ut_z))
        self.send_bury_request(pocket_slot_idx, claimed_item, ut_x, ut_z, request_id, hole_variant,
                               auto_confirm=auto_confirm)
        return self.wait_result(PC_NETGAME_MSG_BURY_RESULT, request_id, timeout)

    def quiet_for(self, pred, seconds, since=None):
        """Pump for `seconds`; True iff NO message matching `pred` arrived (in the window, or since the given
        inbox mark). Non-consuming."""
        mark = self.inbox.mark() if since is None else since
        self.hub.sleep(seconds)
        return self.inbox.count(pred, since=mark) == 0

    # --- world messages -----------------------------------------------------------------------------------

    def drain_field_updates(self, timeout=0.5):
        """Pumps for `timeout`, then consumes every FIELD_UPDATE received so far (and only those),
        returned as [(ut_x, ut_z, value)] in arrival order."""
        msgs = self.inbox.collect(p_msg_type(PC_NETGAME_MSG_FIELD_UPDATE, (CH_RELIABLE,)), timeout)
        out = []
        for m in msgs:
            g = m.game
            if g is not None:
                out.append(field_update_tuple(g))
        return out

    def field_updates_since(self, mark):
        """Decoded FIELD_UPDATE v2 tuples (non-consuming) delivered at inbox index >= mark."""
        return [m.game for m in self.inbox.peek_all(p_msg_type(PC_NETGAME_MSG_FIELD_UPDATE, (CH_RELIABLE,)),
                                                     since=mark) if m.game is not None]

    def player_actions_since(self, mark=None):
        """Decoded PLAYER_ACTION (id 46) tuples (non-consuming) delivered at inbox index >= mark."""
        return [m.game for m in self.inbox.peek_all(p_msg_type(PC_NETGAME_MSG_PLAYER_ACTION, (CH_RELIABLE,)), since=mark)
                if m.game is not None]

    def send_player_action(self, net_player_id=0, kind=PC_NETGAME_PLAYER_ACTION_KIND_PICKUP, flags=0, ut_x=40, ut_z=40,
                           item=0x2800, seq=1, raw=None):
        """A CLIENT-ORIGINATED PLAYER_ACTION (reliable): the host must drop it (clients never originate it)."""
        payload = raw if raw is not None else struct.pack(PLAYER_ACTION_FMT, PC_NETGAME_MSG_PLAYER_ACTION,
                                                          net_player_id & 0xFF, kind & 0xFF, flags & 0xFF, ut_x & 0xFF,
                                                          ut_z & 0xFF, item & 0xFFFF, seq & 0xFFFF)
        return self.send_reliable(payload)

    def take_game(self, msg_type, since=None):
        """Consume every decoded message of msg_type already received."""
        return [m.game for m in self.inbox.take_all(p_msg_type(msg_type), since) if m.game is not None]


def connect_ready_clients(host_ip, port, labels, cls=FakeClient, **kw):
    out = []
    for label in labels:
        c = cls(label, host_ip, port, **kw)
        c.connect_and_ready()
        out.append(c)
    return out


# =================================================================================================
# 8. Town probe, field addressing, WorldView
# =================================================================================================

_TOWN_CACHE = {}
PROBE_TIMEOUT_S = 30.0  # the host defers IDENTITY until its own world is ready


def probe_host_town(host_ip, port, hub=None, timeout=PROBE_TIMEOUT_S):
    """One throwaway connection that learns the host's town identity, then leaves.

    Sends IDENTITY claiming the deliberately wrong PROBE_TOWN; a v2 host answers with the 24-byte
    REJECT(LAND_MISMATCH) carrying its own town and disconnects us. (If the host ever ACKed the
    probe, its IDENTITY_ACK carries the same identity.) Raises RuntimeError otherwise."""
    c = FakeClient("town-probe", host_ip, port, town=PROBE_TOWN, player=DEFAULT_PLAYER, hub=hub, context_flags=None,
                   wait_snapshot=False)
    try:
        c.connect(timeout=3.0)
        c.send_identity(town=PROBE_TOWN)
        m = c.wait_handshake_reply(timeout)
        if m is None:
            raise RuntimeError(f"town probe: no IDENTITY_ACK/REJECT from host within {timeout}s")
        if m.msg_type == PC_NETGAME_MSG_IDENTITY_ACK:
            return town_from_identity_ack(m.game)
        town = town_from_reject(m.payload)
        if town is None:
            raise RuntimeError(f"town probe: REJECT reason={m.game.reason} ({len(m.payload)} bytes) carries no "
                               "host town identity")
        return town
    finally:
        c.close()
        (hub or DEFAULT_HUB).sleep(0.05)


def resolve_host_town(host_ip, port, hub=None):
    key = (socket.gethostbyname(host_ip), int(port))
    if key not in _TOWN_CACHE:
        _TOWN_CACHE[key] = probe_host_town(host_ip, port, hub=hub)
    return _TOWN_CACHE[key]


def connect_matching_town(label, host_ip, port, cls=FakeClient, **kw):
    """Probe-then-connect in one call: learns the host's town (cached) and connects claiming it."""
    town = resolve_host_town(host_ip, port, hub=kw.get("hub"))
    c = cls(label, host_ip, port, town=town, **kw)
    return c.connect_and_ready()


_SNAPSHOT_PROBES = [0]


def read_tiles_via_snapshot(host_ip, port, uts, hub=None):
    """Authoritative, NON-mutating read of town tiles: a throwaway fake client connects, receives the host's
    complete world snapshot, and reports the value it holds for each ut in `uts` (None = unknown/not town).
    A pending reservation never shows here (the field only changes on COMMIT), so this is how tests assert
    'the field has NOT changed'. Returns {ut: value}."""
    _SNAPSHOT_PROBES[0] += 1
    c = FakeClient(f"tile-probe-{_SNAPSHOT_PROBES[0]}", host_ip, port, hub=hub, context_flags=None)
    try:
        c.connect_and_ready(quiet=True)
        return {ut: c.world.tile_ut(*ut) for ut in uts}
    finally:
        c.close()
        (hub or DEFAULT_HUB).sleep(0.1)


# --- persistent field addressing (contract section 2) ---------------------------------------------------

TOWN_GRID = 0
ACRE_X_NUM = 5
ACRE_Z_NUM = 6
ACRE_NUM = 30
TILE_NUM = 256
UT_PER_ACRE = 16
DEPOSIT_ROWS = 16
# ax = ut_x/16 - 1, az = ut_z/16 - 1 (pcfa_town_ut_to_acre_tile(), pc_field_authority.c): valid town
# ut_x 16..95, ut_z 16..111.
TOWN_UT_BLOCK_OFFSET = 1

# pcfa_transient_kind() (pc_field_authority.c): TRANSIENT values must never be sent as world state.
# AMBIGUOUS values (RSV_NO, RSV_SIGNBOARD) are withheld <= 10 s and may then legitimately be sent as a
# settled stable value, so they are not violations on the wire.
TRANSIENT_VALUES = frozenset(list(range(0xF001, 0xF128 + 1)) + list(range(0x43, 0x5B + 1)) + [0x6F])
AMBIGUOUS_VALUES = frozenset({RSV_NO, RSV_SIGNBOARD})


def town_ut_to_acre_tile(ut_x, ut_z):
    ax = ut_x // UT_PER_ACRE - TOWN_UT_BLOCK_OFFSET
    az = ut_z // UT_PER_ACRE - TOWN_UT_BLOCK_OFFSET
    if not (0 <= ax < ACRE_X_NUM and 0 <= az < ACRE_Z_NUM):
        return None
    return az * ACRE_X_NUM + ax, (ut_z % UT_PER_ACRE) * UT_PER_ACRE + (ut_x % UT_PER_ACRE)


def acre_tile_to_town_ut(acre, tile):
    if not (0 <= acre < ACRE_NUM and 0 <= tile < TILE_NUM):
        return None
    ax, az = acre % ACRE_X_NUM, acre // ACRE_X_NUM
    ux, uz = tile % UT_PER_ACRE, tile // UT_PER_ACRE
    return (ax + TOWN_UT_BLOCK_OFFSET) * UT_PER_ACRE + ux, (az + TOWN_UT_BLOCK_OFFSET) * UT_PER_ACRE + uz


# items: 256 u16; deposit/valid: 16 u16 rows (bit ux of row uz); valid=None means "all valid"
FieldBlock = namedtuple("FieldBlock", "grid acre world_seq items deposit valid flags epoch")
FieldBlock.__new__.__defaults__ = (None, 0, 0)
FieldDelta = namedtuple("FieldDelta", "grid acre tile value world_seq deposit flags")  # deposit: 0/1 or None
FieldDelta.__new__.__defaults__ = (0,)

WORLD_WIRE_SPEC_AVAILABLE = True

WORLD_BLOCK = "BLOCK"
WORLD_DELTA = "DELTA"
WORLD_SNAPSHOT_BEGIN = "SNAPSHOT_BEGIN"
WORLD_SNAPSHOT_END = "SNAPSHOT_END"
WORLD_META = "WORLD_META"
# Villager population/is_home milestone:
WORLD_VILLAGER_ARRIVAL = "VILLAGER_ARRIVAL"
WORLD_VILLAGER_DEPARTURE = "VILLAGER_DEPARTURE"
WORLD_VILLAGER_SNAPSHOT = "VILLAGER_SNAPSHOT"
# Friendship/mail sync milestone:
WORLD_FRIENDSHIP_UPDATE = "FRIENDSHIP_UPDATE"
WORLD_FRIENDSHIP_SNAPSHOT_ENTRY = "FRIENDSHIP_SNAPSHOT_ENTRY"
WORLD_MAIL_DELIVERED = "MAIL_DELIVERED"


def _rtc(g):
    return (g.rtc_year, g.rtc_month, g.rtc_day, g.rtc_hour, g.rtc_min, g.rtc_sec)


def _world_state(g):
    """Weather + Stalk Market milestone: PCNetGameWorldStateWire fields already decoded onto g
    (SNAPSHOT_END/WORLD_META both embed it) -> a plain dict, kabu_daily_price in Sunday..Saturday
    order (matching Kabu_price_c.daily_price / lbRTC_SUNDAY..lbRTC_SATURDAY)."""
    return {
        "kabu_daily_price": [g.kabu_daily_price_sun, g.kabu_daily_price_mon, g.kabu_daily_price_tue,
                             g.kabu_daily_price_wed, g.kabu_daily_price_thu, g.kabu_daily_price_fri,
                             g.kabu_daily_price_sat],
        "kabu_trade_market": g.kabu_trade_market,
        "kabu_update_time": (g.kabu_update_year, g.kabu_update_month, g.kabu_update_day, g.kabu_update_hour,
                             g.kabu_update_min, g.kabu_update_sec),
        "weather": g.weather,
        "weather_intensity": g.weather_intensity,
        "gyoei_term": g.gyoei_term,
        "gyoei_term_transition_offset": g.gyoei_term_transition_offset,
        "insect_term": g.insect_term,
        "insect_term_transition_offset": g.insect_term_transition_offset,
    }


def decode_world_msg(payload):
    """B's v2 world messages -> (kind, obj):
         (WORLD_BLOCK, FieldBlock)      FIELD_BLOCK (in-snapshot or live flush block)
         (WORLD_DELTA, FieldDelta)      FIELD_UPDATE v2
         (WORLD_SNAPSHOT_BEGIN, dict)   {"grid", "acre_count", "epoch", "world_seq"}
         (WORLD_SNAPSHOT_END, dict)     {"grid", "acre_count", "flags", "epoch", "world_seq", "renew_time",
                                          + weather/Stalk-Market fields, see _world_state()}
         (WORLD_META, dict)             {"flags", "world_seq", "renew_time",
                                          + weather/Stalk-Market fields, see _world_state()}
       or None."""
    g = decode_game(payload)
    if g is None:
        return None
    t = g.msg_type
    if t == PC_NETGAME_MSG_FIELD_UPDATE:
        dep = None
        if g.flags & PC_NETGAME_FU_FLAG_DEPOSIT_VALID:
            dep = 1 if g.flags & PC_NETGAME_FU_FLAG_DEPOSIT_ON else 0
        return WORLD_DELTA, FieldDelta(g.grid, g.acre, g.tile, g.value, g.world_seq, dep, g.flags)
    if t == PC_NETGAME_MSG_FIELD_BLOCK:
        arr = struct.unpack_from("<256H16H16H", payload, FIELD_BLOCK_SPEC.size)
        return WORLD_BLOCK, FieldBlock(g.grid, g.acre, g.world_seq, list(arr[:256]), list(arr[256:272]),
                                       list(arr[272:288]), g.flags, g.epoch)
    if t == PC_NETGAME_MSG_SNAPSHOT_BEGIN:
        return WORLD_SNAPSHOT_BEGIN, {"grid": g.grid, "acre_count": g.acre_count, "epoch": g.epoch,
                                      "world_seq": g.world_seq}
    if t == PC_NETGAME_MSG_SNAPSHOT_END:
        d = {"grid": g.grid, "acre_count": g.acre_count, "flags": g.flags, "epoch": g.epoch,
             "world_seq": g.world_seq, "renew_time": _rtc(g)}
        d.update(_world_state(g))
        return WORLD_SNAPSHOT_END, d
    if t == PC_NETGAME_MSG_WORLD_META:
        d = {"flags": g.flags, "world_seq": g.world_seq, "renew_time": _rtc(g)}
        d.update(_world_state(g))
        return WORLD_META, d
    if t == PC_NETGAME_MSG_VILLAGER_ARRIVAL:
        return WORLD_VILLAGER_ARRIVAL, {
            "slot": g.slot, "npc_id": g.npc_id, "reserved_block_x": g.reserved_block_x,
            "reserved_block_z": g.reserved_block_z, "reserved_ut_x": g.reserved_ut_x,
            "reserved_ut_z": g.reserved_ut_z, "now_npc_max": g.now_npc_max, "world_seq": g.world_seq,
        }
    if t == PC_NETGAME_MSG_VILLAGER_DEPARTURE:
        return WORLD_VILLAGER_DEPARTURE, {"slot": g.slot, "now_npc_max": g.now_npc_max, "world_seq": g.world_seq}
    if t == PC_NETGAME_MSG_VILLAGER_SNAPSHOT:
        slots = []
        off = VILLAGER_SNAPSHOT_SPEC.size
        slot_size = struct.calcsize(VILLAGER_SLOT_FMT)
        for i in range(ANIMAL_NUM_MAX):
            slots.append(VillagerSlotFields._make(struct.unpack_from(VILLAGER_SLOT_FMT, payload, off + i * slot_size)))
        return WORLD_VILLAGER_SNAPSHOT, {"grid": g.grid, "now_npc_max": g.now_npc_max, "epoch": g.epoch,
                                         "world_seq": g.world_seq, "slots": slots}
    if t == PC_NETGAME_MSG_FRIENDSHIP_UPDATE:
        return WORLD_FRIENDSHIP_UPDATE, {
            "slot": g.slot, "friendship": g.friendship, "player_name": g.player_name, "land_name": g.land_name,
            "player_id": g.player_id, "land_id": g.land_id, "world_seq": g.world_seq,
        }
    if t == PC_NETGAME_MSG_FRIENDSHIP_SNAPSHOT_ENTRY:
        return WORLD_FRIENDSHIP_SNAPSHOT_ENTRY, {
            "slot": g.slot, "friendship": g.friendship, "letter_info": g.letter_info, "player_name": g.player_name,
            "land_name": g.land_name, "player_id": g.player_id, "land_id": g.land_id, "world_seq": g.world_seq,
            "has_letter": bool(g.has_letter), "letter": g.letter,
        }
    if t == PC_NETGAME_MSG_MAIL_DELIVERED:
        return WORLD_MAIL_DELIVERED, {
            "slot": g.slot, "friendship": g.friendship, "letter_info": g.letter_info, "player_name": g.player_name,
            "land_name": g.land_name, "player_id": g.player_id, "land_id": g.land_id, "world_seq": g.world_seq,
            "letter": g.letter,
        }
    return None


def _bit(rows, tile):
    return (rows[tile // UT_PER_ACRE] >> (tile % UT_PER_ACRE)) & 1


class WorldView:
    """What a correct client should believe about the shared town after applying host snapshot
    blocks and deltas under the v2 rules (block applies iff world_seq >= applied_seq[acre], delta iff
    world_seq > applied_seq[acre]; block tiles with valid=0 leave the local value untouched). Also
    records every protocol violation it can detect from the client side."""

    def __init__(self):
        self.items = {}       # (grid, acre) -> list[256] (None = never learned)
        self.deposit = {}     # (grid, acre) -> list[16]
        self.known = {}       # (grid, acre) -> set of tiles with a host-supplied value
        self.acre_seq = {}    # (grid, acre) -> last applied world_seq
        self.max_seq_seen = None
        self.last_delta_seq = None
        self.ignored_stale = []   # ("block"|"delta", key, seq, applied_seq)
        self.violations = []      # human-readable strings
        self.ambiguous_seen = []  # (kind, key, tile, value): RSV_NO / RSV_SIGNBOARD in host-supplied data
        # (protocol: the host must not COMMIT these while it stands in town; not a violation on its own)

    def _see_seq(self, seq):
        if self.max_seq_seen is None or seq_diff(seq, self.max_seq_seen) > 0:
            self.max_seq_seen = seq

    def apply_block(self, b):
        key = (b.grid, b.acre)
        if len(b.items) != TILE_NUM:
            self.violations.append(f"block {key}: {len(b.items)} items, expected {TILE_NUM}")
            return False
        valid = [True] * TILE_NUM if b.valid is None else [bool(_bit(b.valid, t)) for t in range(TILE_NUM)]
        for t, v in enumerate(b.items):
            if valid[t] and v in TRANSIENT_VALUES:
                self.violations.append(f"block {key} tile {t}: transient value {v:#06x} sent as world state")
            if valid[t] and v in AMBIGUOUS_VALUES:
                self.ambiguous_seen.append(("block", key, t, v))
        self._see_seq(b.world_seq)
        cur = self.acre_seq.get(key)
        if cur is not None and seq_diff(b.world_seq, cur) < 0:
            self.ignored_stale.append(("block", key, b.world_seq, cur))
            return False
        row = self.items.setdefault(key, [None] * TILE_NUM)
        dep = self.deposit.setdefault(key, [0] * DEPOSIT_ROWS)
        known = self.known.setdefault(key, set())
        for t in range(TILE_NUM):
            if not valid[t]:
                continue
            row[t] = b.items[t]
            known.add(t)
            if b.deposit is not None:
                ux, uz = t % UT_PER_ACRE, t // UT_PER_ACRE
                if _bit(b.deposit, t):
                    dep[uz] |= 1 << ux
                else:
                    dep[uz] &= ~(1 << ux) & 0xFFFF
        self.acre_seq[key] = b.world_seq
        return True

    def apply_delta(self, d):
        key = (d.grid, d.acre)
        if d.value in TRANSIENT_VALUES:
            self.violations.append(f"delta {key} tile {d.tile}: transient value {d.value:#06x} sent as world state")
        if d.value in AMBIGUOUS_VALUES:
            self.ambiguous_seen.append(("delta", key, d.tile, d.value))
        if self.last_delta_seq is not None and seq_diff(d.world_seq, self.last_delta_seq) <= 0:
            self.violations.append(f"delta world_seq not increasing: {d.world_seq} after {self.last_delta_seq}")
        self.last_delta_seq = d.world_seq
        self._see_seq(d.world_seq)
        cur = self.acre_seq.get(key)
        if cur is not None and seq_diff(d.world_seq, cur) <= 0:
            self.ignored_stale.append(("delta", key, d.world_seq, cur))
            return False
        row = self.items.setdefault(key, [None] * TILE_NUM)
        row[d.tile] = d.value
        self.known.setdefault(key, set()).add(d.tile)
        if d.deposit is not None:
            dep = self.deposit.setdefault(key, [0] * DEPOSIT_ROWS)
            ux, uz = d.tile % UT_PER_ACRE, d.tile // UT_PER_ACRE
            if d.deposit:
                dep[uz] |= 1 << ux
            else:
                dep[uz] &= ~(1 << ux) & 0xFFFF
        self.acre_seq[key] = d.world_seq
        return True

    def tile(self, acre, tile, grid=TOWN_GRID):
        row = self.items.get((grid, acre))
        return None if row is None else row[tile]

    def tile_ut(self, ut_x, ut_z):
        at = town_ut_to_acre_tile(ut_x, ut_z)
        return None if at is None else self.tile(*at)

    def diff(self, other):
        """Tiles where the two views disagree (only tiles both views know)."""
        out = []
        for key in sorted(set(self.items) | set(other.items)):
            a, b = self.items.get(key), other.items.get(key)
            if a is None or b is None:
                out.append((key, "missing"))
                continue
            for t in range(TILE_NUM):
                if a[t] is not None and b[t] is not None and a[t] != b[t]:
                    out.append((key, t, a[t], b[t]))
        return out


# =================================================================================================
# 9. Fixtures and result helpers
# =================================================================================================

# The same 30 tiles pc_net_game.c's pcnetgame_run_pickup_test_seed() seeds when the host is launched
# with --pickup-test-seed -- kept as a literal copy so a diff to the production list is easy to spot.
FIXTURE_CANDIDATE_TILES = [
    (8, 8), (24, 8), (40, 8), (56, 8), (72, 8),
    (8, 24), (24, 24), (40, 24), (56, 24), (72, 24),
    (8, 40), (24, 40), (40, 40), (56, 40), (72, 40),
    (8, 56), (24, 56), (40, 56), (56, 56), (72, 56),
    (8, 72), (24, 72), (40, 72), (56, 72), (72, 72),
    (8, 88), (24, 88), (40, 88), (56, 88), (72, 88),
]

# Only these 20 of the 30 seed tiles are persistent TOWN tiles under pcfa_town_ut_to_acre_tile()
# (ut_x 16..95, ut_z 16..111). The 10 with ut_x == 8 or ut_z == 8 lie in the border blocks: the v2
# host never places them (verified: its log lists exactly the 20 below) and every pickup/drop there is
# rejected as "not a town tile" -- so a test using them would pass/fail for the wrong reason.
TOWN_FIXTURE_TILES = [t for t in FIXTURE_CANDIDATE_TILES if town_ut_to_acre_tile(*t) is not None]
OUT_OF_TOWN_FIXTURE_TILES = [t for t in FIXTURE_CANDIDATE_TILES if town_ut_to_acre_tile(*t) is None]
assert len(TOWN_FIXTURE_TILES) == 20

OUT_OF_RANGE_TILE = (255, 255)  # always rejected by pickup/drop without touching the field


class CandidateQueue:
    """Shared "which tile to try next" source, so nothing is probed twice in one run. In-town
    fixture tiles first; fallback=True then scans the rest of the town (ut 16..95 x 16..111)."""

    def __init__(self, fallback=True):
        self._fixture = list(TOWN_FIXTURE_TILES)
        self._fallback = (
            ((x, z) for z in range(16, 112) for x in range(16, 96) if (x, z) not in FIXTURE_CANDIDATE_TILES)
            if fallback
            else None
        )
        self.probes_used = 0

    def next_tile(self):
        if self._fixture:
            return self._fixture.pop(0)
        if self._fallback is None:
            return None
        try:
            return next(self._fallback)
        except StopIteration:
            return None

    pop_next = next_tile


def make_request_id_counter(start):
    """fresh_request_id() closure; the first id returned is start + 1."""
    counter = [start]

    def fresh_request_id():
        counter[0] += 1
        return counter[0]

    return fresh_request_id


PENDING_EXIT_CODE = 2


def check(desc, cond, results):
    print(("PASS" if cond else "FAIL") + " - " + desc)
    results.append(bool(cond))


def info(desc):
    print("INFO - " + desc)


def pending(desc, pend):
    """Record a check that cannot run yet (e.g. a missing fixture or spec)."""
    print("PENDING - " + desc)
    pend.append(desc)


def summary_and_exit_code(results, pend=None):
    """Standard footer. 0 = all passed, 1 = any failure, PENDING_EXIT_CODE = no failures but some
    checks are pending (so a skeleton can never masquerade as a pass)."""
    print("-" * 60)
    print(f"{sum(results)}/{len(results)} checks passed" + (f", {len(pend)} pending" if pend else ""))
    if not all(results):
        return 1
    if pend:
        return PENDING_EXIT_CODE
    return 0


def parse_host_port(argv, usage):
    """Common `<host_ip> <port>` CLI parsing; returns (host_ip, port) or None after printing usage."""
    if len(argv) != 3:
        print(usage)
        return None
    return argv[1], int(argv[2])


# =================================================================================================
# 10. HostProcess (real game host launcher; run_game_tests.py)
# =================================================================================================

GAME_BIN_DIR = os.environ.get("NET_SPIKE_GAME_BIN",
                              os.path.abspath(os.path.join(HERE, "..", "..", "build64", "bin")))
GAME_EXE_NAME = "AnimalCrossing.exe"
LIVE_GAME_BIN_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "build64", "bin"))  # holds the LIVE save
# PROTECTED test artifact: its save must never be modified (a host that accepts a migrate upload / early-saves on a dirty
# client disconnect REWRITES the GCI and creates save/mp). The harness never launches a game from it; use the disposable
# clone pc/build64/bin_talkfix_clone (make_four_resident_fixture.py --clone-talkfix). Read-only parse helpers may still read it.
PROTECTED_GAME_BIN_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "build64", "bin_talkfix"))
CLONE_GAME_BIN_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "build64", "bin_talkfix_clone"))


def _norm_dir(p):
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def bin_dir_refusal(bin_dir):
    """None when a game process may be launched from bin_dir, else the refusal message. There is deliberately NO override for the
    protected bin_talkfix dir; the live bin dir keeps the pre-existing NET_SPIKE_ALLOW_LIVE_BIN=1 override."""
    nd = _norm_dir(bin_dir)
    if nd == _norm_dir(PROTECTED_GAME_BIN_DIR):
        return ("REFUSING to launch a game process from the PROTECTED test artifact %s (its save must never be modified; a host "
                "can rewrite the GCI and create save/mp). Use the disposable clone %s "
                "(python make_four_resident_fixture.py --clone-talkfix) via NET_SPIKE_GAME_BIN." % (bin_dir, CLONE_GAME_BIN_DIR))
    if nd == _norm_dir(LIVE_GAME_BIN_DIR) and os.environ.get("NET_SPIKE_ALLOW_LIVE_BIN") != "1":
        return ("REFUSING to launch a game process from the LIVE build directory %s, which holds the live save. Set "
                "NET_SPIKE_GAME_BIN to a disposable test copy (e.g. %s), or set NET_SPIKE_ALLOW_LIVE_BIN=1 to override explicitly."
                % (bin_dir, CLONE_GAME_BIN_DIR))
    return None


def is_fixture_bin_dir(bin_dir):
    """The disposable 4-resident fixture dirs (bin_fixture4, bin_fixture4_ambig, bin_fixture4_persist, ...)."""
    return bool(bin_dir) and os.path.basename(os.path.normpath(bin_dir)).startswith("bin_fixture4")


_FIXTURE_AUTO_GUARDS = {}   # normalized fixture bin dir -> temp snapshot of its whole save dir (taken at the first launch from it)
_SAVE_GUARD_DEPTH = [0]     # explicit CloneSaveGuard contexts currently active


def _fixture_snapshot(bin_dir):
    import shutil
    import tempfile
    save = os.path.join(bin_dir, "save")
    snap = os.path.join(tempfile.gettempdir(), "net_spike_fixture_save_snap_%d_%d" % (os.getpid(), len(_FIXTURE_AUTO_GUARDS)))
    shutil.rmtree(snap, ignore_errors=True)
    shutil.copytree(save, snap)
    return snap


def _fixture_restore(bin_dir, snap):
    """The whole save dir back to the snapshot (so save/mp written by a host / client is removed too), then the snapshot is deleted."""
    import shutil
    save = os.path.join(bin_dir, "save")
    shutil.rmtree(save, ignore_errors=True)
    shutil.copytree(snap, save)
    shutil.rmtree(snap, ignore_errors=True)


def _auto_fixture_guard(bin_dir):
    """M4 safety net: the FIRST game launch from a disposable fixture dir snapshots its whole save dir and registers an at-exit restore, so a
    test that forgot its own guard (or crashed) can no longer leave a mutated fixture behind (a host early-saves / writes save/mp). The
    snapshot is of the state BEFORE this python process touched the dir; explicit CloneSaveGuard contexts and the tests' own snapshot/restore
    keep working (they restore the same or an equal state first). Never applies to the live / protected dirs (refused earlier)."""
    import atexit
    if not is_fixture_bin_dir(bin_dir) or not os.path.isdir(os.path.join(bin_dir, "save")):
        return
    key = _norm_dir(bin_dir)
    if key in _FIXTURE_AUTO_GUARDS:
        return
    snap = _fixture_snapshot(bin_dir)
    _FIXTURE_AUTO_GUARDS[key] = snap
    atexit.register(lambda: os.path.isdir(snap) and _fixture_restore(bin_dir, snap))


def require_launchable_bin_dir(bin_dir):
    """Raise RuntimeError (naming the dir) when a host/client game process must not be launched from bin_dir. A launch from a fixture dir also
    arms the automatic whole-save snapshot / at-exit restore (_auto_fixture_guard)."""
    msg = bin_dir_refusal(bin_dir)
    if msg:
        raise RuntimeError(msg)
    _auto_fixture_guard(bin_dir)


def require_test_bin_dir():
    """Safety guard for tests that write the game's save (M9-C review M1). Resolves the bin directory exactly as HostProcess /
    ClientProcess do (GAME_BIN_DIR: NET_SPIKE_GAME_BIN or the pc/build64/bin default) and REFUSES (message + exit code 2) when
    it is the live pc/build64/bin directory, unless NET_SPIKE_ALLOW_LIVE_BIN=1. Call it at the top of a test's main(); it is
    never called at import time and changes nothing for tests that do not call it."""
    import sys

    msg = bin_dir_refusal(GAME_BIN_DIR)
    if msg:
        print(msg, file=sys.stderr)
        sys.exit(2)


class CloneSaveGuard:
    """Context manager for runs on the DISPOSABLE dirs: bin_talkfix_clone (its save .gci is snapshotted at entry and, on exit, restored
    byte-for-byte and save/mp removed: a host may early-save / accept a migrate upload) AND every bin_fixture4* fixture dir (M4: the WHOLE
    save dir is snapshotted at entry and restored on exit, incl. removal of save/mp), so runs stay reproducible. A no-op for any other dir
    (the protected/live dirs can never launch a game anyway). Replaces the former per-test FixtureSaveGuard."""

    def __init__(self, bin_dir=None):
        self.bin_dir = bin_dir or GAME_BIN_DIR
        self.clone = _norm_dir(self.bin_dir) == _norm_dir(CLONE_GAME_BIN_DIR)
        self.fixture = is_fixture_bin_dir(self.bin_dir) and os.path.isdir(os.path.join(self.bin_dir, "save"))
        self.active = self.clone or self.fixture
        self.gci = os.path.join(self.bin_dir, SAVE_GCI_REL)
        self.snap = None
        self.snap_dir = None

    def __enter__(self):
        _SAVE_GUARD_DEPTH[0] += 1
        if self.fixture:
            self.snap_dir = _fixture_snapshot(self.bin_dir)
        elif self.active and os.path.isfile(self.gci):
            with open(self.gci, "rb") as f:
                self.snap = f.read()
        return self

    def __exit__(self, *exc):
        _SAVE_GUARD_DEPTH[0] -= 1
        if self.fixture and self.snap_dir is not None:
            _fixture_restore(self.bin_dir, self.snap_dir)
        elif self.active and self.snap is not None:
            import shutil
            with open(self.gci, "wb") as f:
                f.write(self.snap)
            mp = os.path.join(self.bin_dir, "save", "mp")
            if os.path.isdir(mp):
                shutil.rmtree(mp, ignore_errors=True)
        return False


# --- M9 identity Stage 1A: resident identities of the TEST COPY's save ---------------------------------------------
# Since Stage 1A the host REFUSES an IDENTITY that does not match one of ITS OWN saved residents (and refuses the
# host's own resident and an already-connected resident). The old arbitrary default fake identity ("FAKECLI") would be
# refused, so the default FakeClient identity is now a REAL resident of the host's save. Test-only: the residents are
# parsed READ-ONLY from <bin dir>/save/card_a/DobutsunomoriP_MURA.gci of the TEST COPY (NET_SPIKE_GAME_BIN); the LIVE
# bin dir is never read (it is refused here), and nothing is ever written. Layout: 0x40 CARDDir header, Save_t at
# 0x26000, private_data[4] at +0x20, stride 0x2440 (Private_c), PersonalID_c first (name[8], land[8], BE u16 player_id,
# BE u16 land_id), Private_c.exists at +0x1086. Wire constants are untouched.
SAVE_GCI_REL = os.path.join("save", "card_a", "DobutsunomoriP_MURA.gci")
_GCI_PRIVATE_BASE = 0x40 + 0x26000 + 0x20
_GCI_PRIVATE_STRIDE = 0x2440
_GCI_PRIVATE_EXISTS = 0x1086
PLAYER_NUM = 4
TEST_HOST_RESIDENT = int(os.environ.get("NET_SPIKE_HOST_RESIDENT", "0"))  # the resident the test host bootstraps (--bootstrap-resident N)


def read_test_save_residents(bin_dir=None):
    """[(slot, PlayerIdentity, exists)] for the four resident records of the test copy's save, or [] when the save is
    absent/unreadable or the bin dir is the LIVE one (never read)."""
    bd = bin_dir or GAME_BIN_DIR
    norm = lambda p: os.path.normcase(os.path.realpath(os.path.abspath(p)))
    if norm(bd) == norm(LIVE_GAME_BIN_DIR):
        return []
    try:
        with open(os.path.join(bd, SAVE_GCI_REL), "rb") as f:
            f.seek(_GCI_PRIVATE_BASE)
            raw = f.read(_GCI_PRIVATE_STRIDE * PLAYER_NUM)
    except OSError:
        return []
    if len(raw) < _GCI_PRIVATE_STRIDE * PLAYER_NUM:
        return []
    out = []
    for i in range(PLAYER_NUM):
        o = i * _GCI_PRIVATE_STRIDE
        name = raw[o:o + 8]
        pid, land_id = struct.unpack(">HH", raw[o + 16:o + 20])
        exists = raw[o + _GCI_PRIVATE_EXISTS]
        if land_id == 0xFFFF:
            continue  # empty slot
        out.append((i, PlayerIdentity(name, pid, 1), bool(exists)))
    return out


def resident_player(slot, bin_dir=None):
    """PlayerIdentity of resident `slot` of the test copy's save (raises LookupError when unavailable)."""
    for i, ident, _ex in read_test_save_residents(bin_dir):
        if i == slot:
            return ident
    raise LookupError(f"resident slot {slot} not found in the test save ({SAVE_GCI_REL}); is NET_SPIKE_GAME_BIN a test copy?")


def resident_land(bin_dir=None):
    """(land_name bytes, land_id) of the test copy's town (PersonalID land fields of resident 0)."""
    bd = bin_dir or GAME_BIN_DIR
    norm = lambda p: os.path.normcase(os.path.realpath(os.path.abspath(p)))
    if norm(bd) == norm(LIVE_GAME_BIN_DIR):
        raise LookupError("resident_land: refusing to read the LIVE bin dir's save")
    with open(os.path.join(bd, SAVE_GCI_REL), "rb") as f:
        f.seek(_GCI_PRIVATE_BASE)
        raw = f.read(20)
    return raw[8:16], struct.unpack(">H", raw[18:20])[0]


# --- D3: the canonical BE record image (== the GCI bytes of one Private_c) and its ownership table ---------------------
# One offset-range table over the BE image, mirrored from pc_net_game.c (s_rec_ranges; test_d3_record_src.py asserts they
# are identical and match include/m_private.h): (name, offset, length, owner).
REC_OWN_IMMUTABLE = "immutable"
REC_OWN_HOST = "host"
REC_OWN_SHARED = "shared"   # client-writable AND host-consumed: merged on upload, host value taken on PUSH_HOSTFIELDS
REC_OWN_CLIENT = "client"
RECORD_FIELD_RANGES = [
    ("player_ID", 0x0000, 0x0014, REC_OWN_IMMUTABLE),
    ("client_a", 0x0014, 0x0004, REC_OWN_CLIENT),
    ("museum_record", 0x0018, 0x004E, REC_OWN_HOST),   # mail milestone R (0x18: the 0x17 in m_private.h's comment is wrong, u16 alignment)
    ("client_a2", 0x0066, 0x0020, REC_OWN_CLIENT),
    ("lotto_ticket", 0x0086, 0x0002, REC_OWN_SHARED),
    ("client_b", 0x0088, 0x0FFE, REC_OWN_CLIENT),
    ("exists", 0x1086, 0x0001, REC_OWN_IMMUTABLE),
    ("client_c", 0x1087, 0x0021, REC_OWN_CLIENT),
    ("catalog_orders", 0x10A8, 0x0014, REC_OWN_SHARED),
    ("client_d", 0x10BC, 0x0038, REC_OWN_CLIENT),
    ("reset_code", 0x10F4, 0x0004, REC_OWN_HOST),
    ("client_e", 0x10F8, 0x1348, REC_OWN_CLIENT),
]
assert RECORD_FIELD_RANGES[0][1] == 0 and all(
    RECORD_FIELD_RANGES[i][1] + RECORD_FIELD_RANGES[i][2] == RECORD_FIELD_RANGES[i + 1][1]
    for i in range(len(RECORD_FIELD_RANGES) - 1)) and     RECORD_FIELD_RANGES[-1][1] + RECORD_FIELD_RANGES[-1][2] == PC_NETGAME_REC_SIZE
REC_OFF_PLAYER_ID = 0x0000
REC_OFF_POCKETS = 0x0068            # 15 x BE u16
REC_OFF_LOTTO = 0x0086              # 2 bytes
REC_OFF_ITEM_COND = 0x0088          # BE u32
REC_OFF_WALLET = 0x008C             # BE u32
REC_OFF_LOAN = 0x0090               # BE u32
REC_OFF_EQUIPMENT = 0x04A4          # BE u16
REC_OFF_EXISTS = 0x1086
REC_OFF_CATALOG_ORDERS = 0x10A8     # 5 x {BE u16 item, u8 shop_level, pad}
REC_OFF_RESET_CODE = 0x10F4         # BE u32
REC_OFF_CATALOG_ITEM0 = 0x10A8      # order i: BE u16 item at +4*i, u8 shop_level at +4*i+2, pad
REC_OFF_BANK = 0x122C               # BE u32
REC_OFF_ORG_TABLE = 0x2340          # 8 x u8
REC_OFF_MUSEUM_RECORD = 0x0018      # 0x4E bytes, HOST-owned (mail milestone R)
REC_MUSEUM_RECORD_SIZE = 0x004E
REC_OFF_MAIL = 0x04E0               # 10 x Mail_c (0x12A bytes each), client-owned
REC_MAIL_SIZE = 0x012A
REC_MAIL_COUNT = 10
MAIL_FONT_RECV = 0                  # mMl_FONT_RECV
MAIL_FONT_SEND = 1                  # mMl_FONT_SEND
MAIL_FONT_UNUSED = 0xFF             # mMl_check_not_used_mail
MAIL_NAME_PLAYER = 0
MAIL_NAME_NPC = 1
MAIL_NAME_MUSEUM = 2
MAIL_NAME_CLEAR = 0xFF


def fnv1a32(data):
    """FNV-1a 32 over bytes (the D3 record digest; pc_net_game.c pcnetgame_fnv1a32)."""
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & U32_MASK
    return h


def record_from_gci(bin_or_path, idx):
    """The 0x2440-byte canonical BE image of resident `idx` read from GCI bytes, a GCI path, or a bin dir (-> its
    save/card_a GCI). Read-only; the LIVE bin dir is refused."""
    data = bin_or_path
    if not isinstance(data, (bytes, bytearray)):
        path = str(bin_or_path)
        norm = lambda q: os.path.normcase(os.path.realpath(os.path.abspath(q)))
        if os.path.isdir(path):
            path = os.path.join(path, SAVE_GCI_REL)
        if norm(os.path.dirname(os.path.dirname(os.path.dirname(path)))) == norm(LIVE_GAME_BIN_DIR):
            raise LookupError("record_from_gci: refusing to read the LIVE bin dir's save")
        with open(path, "rb") as f:
            f.seek(_GCI_PRIVATE_BASE + idx * _GCI_PRIVATE_STRIDE)
            return f.read(_GCI_PRIVATE_STRIDE)
    o = _GCI_PRIVATE_BASE + idx * _GCI_PRIVATE_STRIDE
    return bytes(data[o:o + _GCI_PRIVATE_STRIDE])


TxnSent = namedtuple("TxnSent", "raw seq nonce")


def new_txn_nonce():
    """A random non-zero 32-bit client-process nonce (test double of pcnetgame_rec_rand32 for the txn nonce)."""
    import random
    return random.getrandbits(32) or 1


def record_inventory(rec):
    """(pockets tuple of 15 native u16, item_conditions, wallet) of a canonical BE record image (what a TXN pre-image carries)."""
    return (tuple(struct.unpack_from(">15H", rec, REC_OFF_POCKETS)), struct.unpack_from(">I", rec, REC_OFF_ITEM_COND)[0],
            struct.unpack_from(">I", rec, REC_OFF_WALLET)[0])


def record_set_inventory(rec, pockets, conds, wallet):
    """`rec` with its pockets / item_conditions / wallet replaced (native values in, BE bytes out); nothing else changes."""
    b = bytearray(rec)
    struct.pack_into(">15H", b, REC_OFF_POCKETS, *[p & 0xFFFF for p in pockets])
    struct.pack_into(">I", b, REC_OFF_ITEM_COND, conds & U32_MASK)
    struct.pack_into(">I", b, REC_OFF_WALLET, wallet & U32_MASK)
    return bytes(b)


def record_set_u16(rec, off, val):
    b = bytearray(rec)
    b[off:off + 2] = struct.pack(">H", val & 0xFFFF)
    return bytes(b)


def record_set_u32(rec, off, val):
    b = bytearray(rec)
    b[off:off + 4] = struct.pack(">I", val & U32_MASK)
    return bytes(b)


def record_set_bytes(rec, off, data):
    b = bytearray(rec)
    b[off:off + len(data)] = data
    return bytes(b)


def record_get_u16(rec, off):
    return struct.unpack_from(">H", rec, off)[0]


def record_get_u32(rec, off):
    return struct.unpack_from(">I", rec, off)[0]


def record_mail(rec, i):
    """The 0x12A canonical-BE bytes of mail[i] of a record image."""
    o = REC_OFF_MAIL + i * REC_MAIL_SIZE
    return bytes(rec[o:o + REC_MAIL_SIZE])


def record_set_mail(rec, i, mail):
    """`rec` with mail[i] replaced by the 0x12A BE bytes `mail`; nothing else changes."""
    assert len(mail) == REC_MAIL_SIZE
    o = REC_OFF_MAIL + i * REC_MAIL_SIZE
    return record_set_bytes(rec, o, mail)


def mail_hash32(mail_be):
    """The full FNV-1a32 of one letter's canonical-BE bytes (pc_net_game.c pcnetgame_mail_be_hash)."""
    return fnv1a32(bytes(mail_be))


def mail_hash24(mail_be):
    """The low 24 bits of mail_hash32: MAIL_SEND's tag.aux_item (bits 0..15) + tag.aux_cond (bits 16..23)."""
    return mail_hash32(mail_be) & 0xFFFFFF


def mail_present(mail_be):
    return struct.unpack_from(">H", mail_be, 0x2C)[0]


def mail_font(mail_be):
    return mail_be[0x2E]


def mail_recipient(mail_be):
    """(personalID 20 bytes, type) of the recipient of a letter's BE bytes."""
    return bytes(mail_be[0x00:0x14]), mail_be[0x14]


def mail_sender(mail_be):
    return bytes(mail_be[0x16:0x2A]), mail_be[0x2A]


def build_mail_be(recipient_pid, sender_pid, present=0, font=MAIL_FONT_SEND, recipient_type=MAIL_NAME_PLAYER, sender_type=MAIL_NAME_PLAYER,
                  mail_type=0, paper=0, body=None):
    """The 0x12A canonical-BE bytes of a letter (Mail_c: recipient Mail_nm_c at 0x00 = PersonalID 0x14 + type, sender at 0x16, present BE u16 at
    0x2C, content at 0x2E: font, header_back_start, mail_type, paper_type, header[24] at 0x32, body[192] at 0x4A, footer[32] at 0x10A). The PersonalIDs are the 20 BE bytes
    of a resident's player_ID (record bytes 0..0x13). header / footer are spaces (0x20) like a cleared letter; body defaults to a recognisable text."""
    assert len(recipient_pid) == 0x14 and len(sender_pid) == 0x14
    b = bytearray(REC_MAIL_SIZE)
    b[0x00:0x14] = recipient_pid
    b[0x14] = recipient_type & 0xFF
    b[0x16:0x2A] = sender_pid
    b[0x2A] = sender_type & 0xFF
    struct.pack_into(">H", b, 0x2C, present & 0xFFFF)
    b[0x2E] = font & 0xFF
    b[0x2F] = 0
    b[0x30] = mail_type & 0xFF
    b[0x31] = paper & 0xFF
    b[0x32:0x4A] = bytes([0x20]) * 24
    body = bytes(body) if body is not None else bytes((0x30 + (i % 10)) for i in range(32))
    b[0x4A:0x10A] = (body + bytes([0x20]) * 192)[:192]
    b[0x10A:0x12A] = bytes([0x20]) * 32
    return bytes(b)


def mail_delivered_expected(mail_be, sender_pid):
    """What the host must hand to the post office for a client's letter `mail_be`: the sender forced to the bound identity (type PLAYER) and the
    RECEIVE font; everything else (recipient, gift, text) as the client wrote it."""
    b = bytearray(mail_be)
    b[0x16:0x2A] = sender_pid
    b[0x2A] = MAIL_NAME_PLAYER
    b[0x2E] = MAIL_FONT_RECV
    return bytes(b)


def record_merge_expected(host_be, upload_be):
    """What the host must hold after accepting `upload_be` over `host_be`: client-owned AND shared ranges from the upload,
    immutable and host-owned (reset_code) ranges from the host record."""
    out = bytearray(host_be)
    for _n, off, ln, owner in RECORD_FIELD_RANGES:
        if owner in (REC_OWN_CLIENT, REC_OWN_SHARED):
            out[off:off + ln] = upload_be[off:off + ln]
    return bytes(out)


# --- Guests G1: a GUEST's identity (home PersonalID) and its record, as test doubles ---------------------------------------
GuestIdentity = namedtuple("GuestIdentity", "player_name player_id land_name land_id")


def guest_identity(name="GUESTA", player_id=0x4A01, land="HOMETWN", land_id=0x5B01):
    """A guest whose HOME town is not the test town (the default land / ids never equal the fixture's resident or town ids)."""
    return GuestIdentity(bytes(name.encode("ascii") if isinstance(name, str) else name)[:8].ljust(8, b"\x00"), player_id,
                         bytes(land.encode("ascii") if isinstance(land, str) else land)[:8].ljust(8, b"\x00"), land_id)


def guest_pid_be(g):
    """The 20-byte canonical BE PersonalID of a GuestIdentity (the first 0x14 bytes of its record image, the guests.dat pid)."""
    return (bytes(g.player_name)[:8].ljust(8, b"\x00") + bytes(g.land_name)[:8].ljust(8, b"\x00")
            + struct.pack(">HH", g.player_id & 0xFFFF, g.land_id & 0xFFFF))


def guest_player(g):
    return PlayerIdentity(bytes(g.player_name)[:8].ljust(8, b"\x00"), g.player_id & 0xFFFF, 1)


def guest_record(g, base_slot=1, bin_dir=None, base=None):
    """A LEGAL guest record image: a resident record of the test GCI (default slot 1) re-keyed to the guest's home PersonalID
    (player_ID replaced, exists stays 1, every other field a field the validators already accept)."""
    rec = base if base is not None else record_from_gci(bin_dir or GAME_BIN_DIR, base_slot)
    return guest_pid_be(g) + bytes(rec)[20:]


def guest_blank_record(g):
    """The record the HOST creates at a guest's first contact (rev 0): zeros, player_ID = the guest key (BE), exists = 1. A MIGRATE_UPLOAD
    then merges the client-owned / shared ranges into it (record_merge_expected(guest_blank_record(g), upload))."""
    r = bytearray(PC_NETGAME_REC_SIZE)
    r[0:20] = guest_pid_be(g)
    r[0x1086] = 1
    return bytes(r)


def build_identity_ext(g, token=None, flags=PC_NETGAME_IDEXT_FLAG_GUEST, rsv0=0, rsv1=0, token_present=None):
    """IDENTITY_EXT bytes (42 B). token: 16 bytes or None (token_present 0). token_present / rsv overrides let a test send malformed ones."""
    tp = (1 if token is not None else 0) if token_present is None else token_present
    return struct.pack(IDENTITY_EXT_FMT, PC_NETGAME_MSG_IDENTITY_EXT, flags & 0xFF, rsv0 & 0xFFFF, bytes(g.player_name)[:8].ljust(8, b"\x00"),
                       bytes(g.land_name)[:8].ljust(8, b"\x00"), g.player_id & 0xFFFF, g.land_id & 0xFFFF, tp & 0xFF,
                       bytes(token if token is not None else b"")[:16].ljust(16, b"\x00"), rsv1 & 0xFF)


def _init_default_player():
    """Default fake identity := the first existing resident that is not the host's own (TEST_HOST_RESIDENT). Only when
    NET_SPIKE_GAME_BIN names a test copy whose save can be read; otherwise the legacy arbitrary identity stays (a host
    started from the live bin dir is never targeted by tests -- require_test_bin_dir())."""
    global DEFAULT_PLAYER, ZERO_PLAYER
    if "NET_SPIKE_GAME_BIN" not in os.environ:
        return
    for i, ident, exists in read_test_save_residents():
        if exists and i != TEST_HOST_RESIDENT:
            DEFAULT_PLAYER = ident
            ZERO_PLAYER = ident
            return


_init_default_player()

_default_player_alloc = []  # [(resident slot, weakref to the FakeClient it was handed to, allocation sequence number)]
_default_player_seq = [0]
ABANDON_HOLD_S = 6.0  # host transport silence timeout (PCNET_TIMEOUT ~5 s) + margin


def reset_default_player_allocator():
    """Forget every allocation (call at the top of a test's run() if it must start from the first non-host resident)."""
    del _default_player_alloc[:]


def next_default_player(client=None):
    """Per-process allocator for FakeClient's DEFAULT identity (an explicit `player=` argument always wins). Hands out
    DISTINCT existing non-host residents of the TEST COPY's save: a resident is skipped while the FakeClient it was last
    handed to is still open (state != CLOSED, i.e. IDLE/PENDING/CONNECTED; close()/disconnect()/abandon() free it), and
    among the free ones the least recently handed out wins (so a just-disconnected resident is reused last, giving the
    host time to release its binding). A resident whose client was abandon()ed (silent vanish, the host keeps the
    binding until its ~5 s transport timeout) counts as held for ABANDON_HOLD_S; if that is all that stands in the way the
    allocator pumps (pump_sleep) until the host has released it. Env NET_SPIKE_DEFAULT_RESIDENT=<slot> makes that slot
    the first choice (used by a test that starts a subprocess while its own clients hold other residents). If all are held by OPEN clients the least recently used
    one is returned anyway (a genuine "more simultaneous clients than residents" situation -- the host will refuse it,
    which is the correct behaviour).
    With the 2-resident bin_talkfix(_clone) save (one non-host resident) this is exactly the old single DEFAULT_PLAYER; with the
    4-resident fixture (make_four_resident_fixture.py -> pc/build64/bin_fixture4, host = resident 0) up to three clients can
    be connected at the same time (residents 1, 2, 3). Outside a test copy (NET_SPIKE_GAME_BIN unset / unreadable save)
    it returns DEFAULT_PLAYER. Test-only: no wire or production effect; duplicate-claim tests pass an explicit `player=`."""
    if "NET_SPIKE_GAME_BIN" not in os.environ:
        return DEFAULT_PLAYER
    cands = [(i, ident) for i, ident, ex in read_test_save_residents() if ex and i != TEST_HOST_RESIDENT]
    if not cands:
        return DEFAULT_PLAYER
    preferred = int(os.environ.get("NET_SPIKE_DEFAULT_RESIDENT", "-1"))  # a parent test steering a subprocess to a spare resident
    while True:
        now = time.monotonic()
        last = {}  # slot -> (busy, release_time or None, seq) of its most recent allocation
        for slot, ref, seq in _default_player_alloc:
            c = ref()
            ab = getattr(c, "_abandoned_at", None) if c is not None else None
            hold_until = (ab + ABANDON_HOLD_S) if ab is not None and now < ab + ABANDON_HOLD_S else None
            open_ = c is not None and getattr(c, "state", None) != TransportClient.STATE_CLOSED
            last[slot] = (open_ or hold_until is not None, hold_until if not open_ else None, seq)
        free = [(0 if i == preferred else 1, last.get(i, (False, None, -1))[2], i, ident) for i, ident in cands
                if not last.get(i, (False, None, -1))[0]]
        if free:
            k = min(free)
            pick = (k[1], k[2], k[3])
            break
        waits = [last[i][1] for i, _ in cands if last[i][1] is not None]
        if not waits:  # every resident is held by an open client: more simultaneous clients than residents
            pick = min((last[i][2], i, ident) for i, ident in cands)
            break
        # a resident is only held by an abandoned (silent) client: the host frees it at its transport timeout; wait for that
        pump_sleep(max(0.05, min(waits) - now + 0.3))
    _default_player_seq[0] += 1
    if client is not None:
        _default_player_alloc.append((pick[1], weakref.ref(client), _default_player_seq[0]))
    return pick[2]


def rebind_default_player(client):
    """Re-register `client`'s CURRENT identity in next_default_player()'s allocator as held by `client` (test-only
    bookkeeping, no wire/host effect). Needed when a test reconnects a client that it had closed earlier ("parked" it to stay
    within the 3-simultaneous-client limit): meanwhile another client may have been handed the same resident, so the
    allocator's most recent record for that resident no longer names the reconnecting client and would wrongly treat the
    resident as free again."""
    if "NET_SPIKE_GAME_BIN" not in os.environ:
        return
    for i, ident, ex in read_test_save_residents():
        if ex and ident == client.player:
            _default_player_seq[0] += 1
            _default_player_alloc.append((i, weakref.ref(client), _default_player_seq[0]))
            return


class HostProcess:
    """Launches `AnimalCrossing.exe --host <port> --verbose [extra...]` with CWD = the game's bin
    directory (resources are CWD-relative) and stdout/stderr redirected to a log file (--verbose
    makes stdout unbuffered). Context manager; stop() terminates the process."""

    def __init__(self, port=7788, extra_args=(), log_path=None, bin_dir=None, env=None):
        self.port = int(port)
        self.extra_args = list(extra_args)
        self.bin_dir = bin_dir or GAME_BIN_DIR
        self.exe = os.path.join(self.bin_dir, GAME_EXE_NAME)
        require_launchable_bin_dir(self.bin_dir)  # protected bin_talkfix / live bin: refuse at construction
        self.log_path = log_path or os.path.join(self.bin_dir, f"net_spike_host_{self.port}.log")
        self.env = env
        self.proc = None
        self._log_fp = None

    def start(self):
        require_launchable_bin_dir(self.bin_dir)
        if not os.path.isfile(self.exe):
            raise FileNotFoundError(self.exe)
        self._log_fp = open(self.log_path, "wb")
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        self.proc = subprocess.Popen([self.exe, "--host", str(self.port), "--verbose"] + self.extra_args,
                                     cwd=self.bin_dir, stdout=self._log_fp, stderr=subprocess.STDOUT, env=env)
        return self

    def log_text(self):
        try:
            with open(self.log_path, "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def wait_for_log(self, pattern, timeout, since_offset=0):
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.log_text()[since_offset:]
            mo = rx.search(text)
            if mo:
                return mo
            if self.proc is not None and self.proc.poll() is not None:
                return None
            time.sleep(0.1)
        return None

    def wait_listening(self, timeout=60.0):
        return self.wait_for_log(r"\[NET\] hosting on UDP port %d" % self.port, timeout) is not None

    WORLD_READY_RX = r"\[NET\]\[WORLD\] host: world ready \(land_id=0x([0-9A-Fa-f]+) hash=0x([0-9A-Fa-f]+)"
    # Client-side analogue of WORLD_READY_RX, for ClientProcess.boot_to_field()'s default ready_rx:
    # there is no "client: world ready" line, but "snapshot epoch N applied" (pc_net_game.c) is the
    # client's own equivalent readiness signal -- the initial SNAPSHOT_END from the host has been
    # fully applied to the client's local field/world state.
    CLIENT_SNAPSHOT_APPLIED_RX = r"\[NET\]\[WORLD\] client: snapshot epoch \d+ applied"
    SEED_RX = r"--pickup-test-seed: fixture item placed at tile \((\d+),(\d+)\)"

    def world_ready(self):
        return re.search(self.WORLD_READY_RX, self.log_text()) is not None

    def boot_to_town(self, timeout=120.0, verbose=True):
        """Walks the host from the title screen into gameplay (v2 serves no world state before
        that): waits for the title's "press start", presses START, then A (every 1.5 s) through the
        villager dialogue and the player-select menu (first player) until the log reports
        "[NET][WORLD] host: world ready". Uses game_input (focuses the host window). Returns True on
        success."""
        from game_input import GameWindow  # local import: Windows-only helper

        deadline = time.monotonic() + timeout
        if self.wait_for_log(r"press_start_opacity=255", timeout) is None:
            return False
        win = GameWindow(self.proc.pid)
        while not win.ready() and time.monotonic() < deadline:
            time.sleep(0.2)
        win.press("START", after=3.0)
        presses = 0
        while not self.world_ready() and time.monotonic() < deadline and self.alive():
            win.press("A", after=1.5)
            presses += 1
            if presses > 30:
                break
        ok = self.world_ready()
        if verbose:
            print(f"[host] boot_to_town: {'world ready' if ok else 'FAILED'} after START + {presses} x A")
        return ok

    # --- direct-bootstrap readiness (P1 real-gameplay verification) --------------------------------
    #
    # boot_to_town() above is for the interactive title-screen path (protocol v2 tests) and injects
    # keystrokes via game_input.py. boot_to_field() is for `--bootstrap-resident N` launches: it uses
    # NO keystrokes at all and fixes a false-positive that boot_to_town()'s own world_ready() has
    # against this path -- world_ready() greps the ENTIRE log for the "world ready" line, but that
    # exact line is also printed transiently during the title-screen demo reload (pre-existing
    # behavior, unrelated to bootstrap), so a naive whole-log search can report success before the
    # real, bootstrapped world is actually playable. boot_to_field() instead anchors its readiness
    # search to start strictly AFTER the bootstrap's own "resident bound, transitioning to town"
    # line (pc_m_card.c), so only a "world ready" that happens after the real bootstrap counts.
    BOOTSTRAP_BOUND_RX_FMT = r"\[PC\] --bootstrap-resident %d: resident bound, transitioning to town \(SCENE_FG\)"
    BOOTSTRAP_FAIL_RX_FMT = (r"\[PC\] --bootstrap-resident %d: (slot out of range|slot has no resident|"
                             r"no valid town save is loaded|resident is marked away/travelling|"
                             r"mSDI_StartDataInit failed|goto_other_scene to SCENE_FG failed)")
    LOCAL_SAVE_NOT_LOADED_RX = r"\[NET\]\[WORLD\] (?:host|client): local save not loaded"

    def _wait_bootstrap_bound(self, slot, timeout, verbose):
        """Waits for pc_m_card.c's own bootstrap outcome line (success OR a known failure) --
        independent of host/client role, since --bootstrap-resident is handled before net role
        matters. Returns the absolute log-byte-offset right after the SUCCESS line, or None."""
        bound_rx = re.compile(self.BOOTSTRAP_BOUND_RX_FMT % slot)
        fail_rx = re.compile(self.BOOTSTRAP_FAIL_RX_FMT % slot)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.log_text()
            mo = bound_rx.search(text)
            if mo:
                return mo.end()
            fmo = fail_rx.search(text)
            if fmo:
                if verbose:
                    print(f"[boot_to_field] bootstrap failure: {fmo.group(0)!r}")
                return None
            if self.proc is not None and self.proc.poll() is not None:
                if verbose:
                    tail = [ln for ln in text.splitlines() if ln.strip()][-4:]
                    print("[boot_to_field] process exited before any bootstrap outcome line (exit code %s; log %s; "
                          "last lines: %s)" % (self.proc.returncode, self.log_path, tail))
                return None
            time.sleep(0.1)
        if verbose:
            print("[boot_to_field] timed out waiting for the bootstrap resident-bound line")
        return None

    def boot_to_field(self, timeout=60.0, slot=0, ready_rx=None, settle=2.5, verbose=True):
        """Non-interactive readiness check for a process launched with `--bootstrap-resident <slot>`.
        Uses NO game_input/keystrokes. Steps (see module docstring section above for why):
          1. Wait for pc_m_card.c's bootstrap outcome line for `slot` (success or a known failure);
             fail fast on a known failure line or early process exit.
          2. Wait for `ready_rx` (default: HostProcess.WORLD_READY_RX) to match STRICTLY AFTER the
             success line's offset -- not anywhere in the whole log.
          3. Sleep `settle` seconds for the entrance wipe/house-exit animation.
          4. Confirm no "local save not loaded" line appears after the step-2 match, and the process
             is still alive.
        Returns True only if all of the above pass within `timeout`."""
        if ready_rx is None:
            ready_rx = self.WORLD_READY_RX
        deadline = time.monotonic() + timeout
        bound_offset = self._wait_bootstrap_bound(slot, timeout, verbose)
        if bound_offset is None:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if verbose:
                print("[boot_to_field] timed out before the readiness wait could start")
            return False
        mo = self.wait_for_log(ready_rx, remaining, since_offset=bound_offset)
        if mo is None:
            if verbose:
                print("[boot_to_field] timed out waiting for readiness AFTER the bootstrap-bound line "
                      "(this is the false-positive fix: a match earlier in the log, e.g. from the "
                      "title-screen demo reload, does not count)")
            return False
        ready_offset = bound_offset + mo.end()
        if settle > 0:
            time.sleep(settle)
        if self.proc is not None and self.proc.poll() is not None:
            if verbose:
                print("[boot_to_field] process exited during the post-readiness settle period")
            return False
        text_after = self.log_text()[ready_offset:]
        if re.search(self.LOCAL_SAVE_NOT_LOADED_RX, text_after):
            if verbose:
                print("[boot_to_field] 'local save not loaded' seen AFTER readiness -- the readiness "
                      "line was a false positive")
            return False
        if verbose:
            print("[boot_to_field] genuine field-ready (bootstrap-bound -> readiness, no post-readiness "
                  "'local save not loaded')")
        return True

    def host_town_from_log(self):
        mo = re.search(self.WORLD_READY_RX, self.log_text())
        return None if mo is None else (int(mo.group(1), 16), int(mo.group(2), 16))

    def seeded_tiles(self):
        return [(int(a), int(b)) for a, b in re.findall(self.SEED_RX, self.log_text())]

    def wait_seeded(self, count=20, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.seeded_tiles()) >= count:
                return True
            time.sleep(0.2)
        return len(self.seeded_tiles()) >= count

    def exit_code(self):
        return None if self.proc is None else self.proc.poll()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout=5.0):
        """Stops the host. Returns the process exit code it had BEFORE we stopped it (None if it was
        still running -- i.e. it did not crash on its own)."""
        own_exit = self.exit_code()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout)
        if self._log_fp is not None:
            self._log_fp.close()
            self._log_fp = None
        return own_exit

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


class ClientProcess(HostProcess):
    """Launches `AnimalCrossing.exe --connect <ip:port> --verbose [extra...]` -- the second REAL game
    process for two-real-process P1 gameplay verification. Same log-file/boot_to_field/stop plumbing
    as HostProcess (which it subclasses); only the launched command line differs (no --host, no
    wait_listening -- that is a host-only log line)."""

    def __init__(self, connect_to, extra_args=(), log_path=None, bin_dir=None, env=None, label="client"):
        self.connect_to = connect_to
        super().__init__(port=0, extra_args=extra_args, log_path=log_path, bin_dir=bin_dir, env=env)
        self.log_path = log_path or os.path.join(self.bin_dir, f"net_spike_{label}.log")

    def start(self):
        require_launchable_bin_dir(self.bin_dir)
        if not os.path.isfile(self.exe):
            raise FileNotFoundError(self.exe)
        self._log_fp = open(self.log_path, "wb")
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        self.proc = subprocess.Popen(
            [self.exe, "--connect", self.connect_to, "--verbose"] + self.extra_args,
            cwd=self.bin_dir, stdout=self._log_fp, stderr=subprocess.STDOUT, env=env)
        return self

    def boot_to_field(self, timeout=60.0, slot=0, ready_rx=None, settle=2.5, verbose=True):
        """Same contract as HostProcess.boot_to_field(), defaulting `ready_rx` to the CLIENT
        readiness signal (CLIENT_SNAPSHOT_APPLIED_RX) instead of the host's WORLD_READY_RX."""
        if ready_rx is None:
            ready_rx = self.CLIENT_SNAPSHOT_APPLIED_RX
        return super().boot_to_field(timeout=timeout, slot=slot, ready_rx=ready_rx, settle=settle,
                                     verbose=verbose)
