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

    def __init__(self, label, host_ip, port, hub=None, rto=0.25, bind_port=0, recv_ahead=1024, verbose=False):
        self.label = label
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
        s.bind(("0.0.0.0", bind_port))
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

PC_NETGAME_PROTOCOL_VERSION = 2

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
MOVE_FMT = "<BBBbIfffhhf"               # PCNetMoveMsg, 28 bytes
APPEARANCE_HDR_FMT = "<BBBBHBB"         # PCNetGameAppearanceMsg header
APPEARANCE_MSG_SIZE = 576
PICKUP_REQUEST_FMT = "<BBBBI"           # 8 bytes
PICKUP_RESULT_FMT = "<BBBBIHH"          # 12 bytes
FIELD_UPDATE_FMT = "<BBBBBBHI"          # PCNetGameFieldUpdateMsg v2, 12 bytes
DROP_REQUEST_FMT = "<BBBBHHI"           # 12 bytes
DROP_RESULT_FMT = "<BBBBIHH"            # 12 bytes
PLAYER_CONTEXT_FMT = "<BBBBhh"          # 8 bytes
RTC_FMT = "BBBBBBH"                     # PCNetGameRtcWire (sec,min,hour,day,weekday,month,year), 8 bytes
SNAPSHOT_BEGIN_FMT = "<BBBBII"          # 12 bytes
FIELD_BLOCK_HDR_FMT = "<BBBBII"         # 12-byte header of PCNetGameFieldBlockMsg
FIELD_BLOCK_SIZE = 588                  # header + u16 items[256] + u16 deposit[16] + u16 valid[16]
# Weather + Stalk Market milestone: PCNetGameWorldStateWire, 28 bytes -- 7 daily prices (Sunday..
# Saturday), trade_market, the Kabu update_time (an RTC_FMT), weather, weather_intensity, and 2
# reserved pad bytes (see pc_net_game.c's own PCNetGameWorldStateWire doc comment).
WORLD_STATE_FMT = "<7HH" + RTC_FMT + "BB2x"  # 28 bytes
_WORLD_STATE_FIELDS = ["kabu_daily_price_sun", "kabu_daily_price_mon", "kabu_daily_price_tue",
                       "kabu_daily_price_wed", "kabu_daily_price_thu", "kabu_daily_price_fri",
                       "kabu_daily_price_sat", "kabu_trade_market", "kabu_update_sec", "kabu_update_min",
                       "kabu_update_hour", "kabu_update_day", "kabu_update_weekday", "kabu_update_month",
                       "kabu_update_year", "weather", "weather_intensity"]
SNAPSHOT_END_FMT = "<BBBBII" + RTC_FMT + WORLD_STATE_FMT[1:]  # 48 bytes
WORLD_META_FMT = "<BBHI" + RTC_FMT + WORLD_STATE_FMT[1:]      # 44 bytes
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
     "facing_angle", "reserved0", "speed"],
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

GAME_SPECS = {
    s.msg_type: s
    for s in (IDENTITY_SPEC, IDENTITY_ACK_SPEC, REJECT_SPEC, MOVE_SPEC, APPEARANCE_SPEC, PICKUP_REQUEST_SPEC,
              PICKUP_RESULT_SPEC, FIELD_UPDATE_SPEC, DROP_REQUEST_SPEC, DROP_RESULT_SPEC, PLAYER_CONTEXT_SPEC,
              SNAPSHOT_BEGIN_SPEC, FIELD_BLOCK_SPEC, SNAPSHOT_END_SPEC, WORLD_META_SPEC, RESYNC_REQUEST_SPEC,
              INTERACT_CONFIRM_SPEC, VILLAGER_ARRIVAL_SPEC, VILLAGER_DEPARTURE_SPEC, VILLAGER_SNAPSHOT_SPEC)
}
assert IDENTITY_SPEC.size == 32 and IDENTITY_ACK_SPEC.size == 32 and REJECT_TOWN_SPEC.size == 24
assert FIELD_UPDATE_SPEC.size == 12 and PLAYER_CONTEXT_SPEC.size == 8 and SNAPSHOT_BEGIN_SPEC.size == 12
assert SNAPSHOT_END_SPEC.size == 48 and WORLD_META_SPEC.size == 44 and RESYNC_REQUEST_SPEC.size == 4
assert INTERACT_CONFIRM_SPEC.size == 8 and PICKUP_REQUEST_SPEC.size == 8
assert FIELD_BLOCK_SPEC.size + 2 * (256 + 16 + 16) == FIELD_BLOCK_SIZE
assert VILLAGER_ARRIVAL_SPEC.size == 16 and VILLAGER_DEPARTURE_SPEC.size == 8
assert VILLAGER_SNAPSHOT_SPEC.size + ANIMAL_NUM_MAX * struct.calcsize(VILLAGER_SLOT_FMT) == VILLAGER_SNAPSHOT_SIZE
assert VILLAGER_SNAPSHOT_SIZE == 132

# Messages that answer a request, keyed by msg_type -> result spec (used for rid matching).
RESULT_SPECS = {PC_NETGAME_MSG_PICKUP_RESULT: PICKUP_RESULT_SPEC, PC_NETGAME_MSG_DROP_RESULT: DROP_RESULT_SPEC}
WORLD_MSG_TYPES = (PC_NETGAME_MSG_FIELD_UPDATE, PC_NETGAME_MSG_SNAPSHOT_BEGIN, PC_NETGAME_MSG_FIELD_BLOCK,
                   PC_NETGAME_MSG_SNAPSHOT_END, PC_NETGAME_MSG_WORLD_META, PC_NETGAME_MSG_VILLAGER_ARRIVAL,
                   PC_NETGAME_MSG_VILLAGER_DEPARTURE, PC_NETGAME_MSG_VILLAGER_SNAPSHOT)


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


def build_identity(town=ZERO_TOWN, player=DEFAULT_PLAYER, protocol_version=None):
    """v2 IDENTITY: u8 type; u8 has_save; u16 res; u32 protocol_version; name[8]; land[8]; u16
    player_id; u16 land_id; u32 terrain_hash."""
    if protocol_version is None:
        protocol_version = PC_NETGAME_PROTOCOL_VERSION
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

def build_move(frame, x, y, z, angle=0, speed=0.0, move_state=1, item_kind=-1, net_player_id=0):
    return struct.pack(MOVE_FMT, PC_NETGAME_MSG_MOVE, net_player_id, move_state, item_kind, frame & U32_MASK,
                       x, y, z, angle, 0, speed)


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


def build_move_any(frame, x, y, z, angle=0, speed=0.0, move_state=1, item_kind=-1, net_player_id=0):
    """build_move() whose x/y/z/speed may be python floats (incl. nan/inf/1e30) or F32Bits raw bit patterns.
    Byte-identical to build_move() for ordinary floats."""
    head = struct.pack("<BBBbI", PC_NETGAME_MSG_MOVE, net_player_id, move_state, item_kind, frame & U32_MASK)
    tail = struct.pack("<hh", angle, 0)
    return head + _f32_bytes(x) + _f32_bytes(y) + _f32_bytes(z) + tail + _f32_bytes(speed)


def build_interact_confirm(kind, request_id, outcome=CONFIRM_OUTCOME_COMMIT, reason=CONFIRM_REASON_NONE):
    """INTERACT_CONFIRM (type 17, 8 bytes): u8 type, u8 kind, u8 outcome, u8 reason, u32 request_id."""
    return struct.pack(INTERACT_CONFIRM_FMT, PC_NETGAME_MSG_INTERACT_CONFIRM, kind & 0xFF, outcome & 0xFF,
                       reason & 0xFF, request_id & U32_MASK)


def build_pickup_request(ut_x, ut_z, request_id):
    return struct.pack(PICKUP_REQUEST_FMT, PC_NETGAME_MSG_PICKUP_REQUEST, ut_x & 0xFF, ut_z & 0xFF, 0,
                       request_id & U32_MASK)


def build_drop_request(pocket_slot_idx, claimed_item, ut_x, ut_z, request_id):
    return struct.pack(DROP_REQUEST_FMT, PC_NETGAME_MSG_DROP_REQUEST, pocket_slot_idx, ut_x & 0xFF, ut_z & 0xFF,
                       claimed_item & 0xFFFF, 0, request_id & U32_MASK)


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

    def __init__(self, label, host_ip, port, town=None, player=None, hub=None, context_flags=DEFAULT_CONTEXT_FLAGS,
                 wait_snapshot=True, **kw):
        super().__init__(label, host_ip, port, hub=hub, **kw)
        self.host_ip = host_ip
        self.port = int(port)
        self.town = town
        self.player = player or DEFAULT_PLAYER
        self.context_flags = context_flags
        self.wait_snapshot = wait_snapshot
        self.assigned_peer_id = None
        self.identity_ack = None
        self.reject = None
        self.move_frame = 0
        self.result_log = []  # (msg_type, request_id, fields, conn) for EVERY result ever delivered
        self.confirm_policy = {}   # (kind, request_id) -> bool, per-request override of auto_confirm
        self.confirms_sent = []    # (kind, request_id, outcome, reason, conn) for EVERY CONFIRM this client sent
        self._auto_confirmed = set()  # (kind, request_id, conn) already auto-confirmed
        self._reset_world_tracking()

    # --- world tracking (mirrors the real client's rules in pcnetgame_handle_client_*) --------------

    def _reset_connection_state(self):
        super()._reset_connection_state()
        self._reset_world_tracking()  # a new connection starts with no world knowledge at all

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
        self.post_ready_handshake(self.wait_snapshot if wait_snapshot is None else wait_snapshot)
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

    def send_move(self, frame, x, y, z, angle=0, speed=0.0, move_state=1, item_kind=-1, reliable=False):
        msg = build_move(frame, x, y, z, angle=angle, speed=speed, move_state=move_state, item_kind=item_kind)
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
        if auto_confirm is not None:
            self.confirm_policy[(CONFIRM_KIND_DROP, request_id & U32_MASK)] = bool(auto_confirm)
        return self.send_reliable(build_drop_request(pocket_slot_idx, claimed_item, ut_x, ut_z, request_id))

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

    def on_message(self, m):
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
        kind = CONFIRM_KIND_PICKUP if m.msg_type == PC_NETGAME_MSG_PICKUP_RESULT else CONFIRM_KIND_DROP
        rid = g.request_id
        want = self.__dict__.get("confirm_policy", {}).get((kind, rid), self.auto_confirm)
        key = (kind, rid, m.conn)
        if not want or key in self.__dict__.get("_auto_confirmed", ()):
            return
        self._auto_confirmed.add(key)
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
    c = FakeClient("town-probe", host_ip, port, town=PROBE_TOWN, hub=hub, context_flags=None, wait_snapshot=False)
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


class HostProcess:
    """Launches `AnimalCrossing.exe --host <port> --verbose [extra...]` with CWD = the game's bin
    directory (resources are CWD-relative) and stdout/stderr redirected to a log file (--verbose
    makes stdout unbuffered). Context manager; stop() terminates the process."""

    def __init__(self, port=7788, extra_args=(), log_path=None, bin_dir=None, env=None):
        self.port = int(port)
        self.extra_args = list(extra_args)
        self.bin_dir = bin_dir or GAME_BIN_DIR
        self.exe = os.path.join(self.bin_dir, GAME_EXE_NAME)
        self.log_path = log_path or os.path.join(self.bin_dir, f"net_spike_host_{self.port}.log")
        self.env = env
        self.proc = None
        self._log_fp = None

    def start(self):
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
