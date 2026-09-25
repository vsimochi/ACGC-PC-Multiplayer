#!/usr/bin/env python3
"""net_spike_lib.py - Shared plumbing for the Stage 5A/5B synthetic network tests.

NOT a standalone script (no __main__/argv handling) -- import it from a `test_*.py` harness like
test_pickup_sync.py or test_drop_sync.py. Those two files independently hand-crafted the same
HELLO->IDENTITY->IDENTITY_ACK handshake, the same PICKUP_REQUEST/PICKUP_RESULT/FIELD_UPDATE wire
plumbing, the same --pickup-test-seed fixture-tile literal, and the same check()/summary-footer
pattern; a read-only audit found roughly 20-25% verbatim duplication between them. This module is
the single place that plumbing now lives, so a future change to any of it (e.g. a wire format
change in pc/src/pc_net_game.c) only needs to be mirrored here once.

This module intentionally does NOT own anything feature-specific: pickup's own probing/discovery
logic and drop's own fixture-prep (find_and_empty_tile()/find_occupied_tile()) stay in their own
scripts, per this project's "one self-contained script per feature" convention (see
test_drop_sync.py's own header comment) -- only the underlying wire-format and harness plumbing is
shared here.

Nothing in this file changes any wire-protocol behavior, message format, or timing -- it is a
verbatim move of what test_pickup_sync.py and test_drop_sync.py already did, unified in one place.
"""
from collections import namedtuple
import socket
import struct
import time

PCNET_MAGIC = 0x41434E50

PCNET_WIRE_HELLO = 0
PCNET_WIRE_HELLO_ACK = 1
PCNET_WIRE_HEARTBEAT = 2
PCNET_WIRE_DATA = 4

PC_NET_RELIABLE = 1

PC_NETGAME_MSG_IDENTITY = 1
PC_NETGAME_MSG_IDENTITY_ACK = 2
PC_NETGAME_MSG_MOVE = 4
PC_NETGAME_MSG_PICKUP_REQUEST = 6
PC_NETGAME_MSG_PICKUP_RESULT = 7
PC_NETGAME_MSG_FIELD_UPDATE = 8
PC_NETGAME_MSG_DROP_REQUEST = 9
PC_NETGAME_MSG_DROP_RESULT = 10

PC_NETGAME_PROTOCOL_VERSION = 1

WIRE_HDR_FMT = "<IBBH"
IDENTITY_FMT = "<B3xI8s8sHHB3x"
ACK_FMT = "<BBHI8s8sHHB3x"
MOVE_FMT = "<BBBbIfffhhf"  # matches PCNetMoveMsg (pc_net_game.c), 28 bytes
PICKUP_REQUEST_FMT = "<BBBBI"  # matches PCNetGamePickupRequestMsg, 8 bytes
PICKUP_RESULT_FMT = "<BBBBIHH"  # matches PCNetGamePickupResultMsg, 12 bytes
FIELD_UPDATE_FMT = "<BBBBHH"  # matches PCNetGameFieldUpdateMsg, 8 bytes
DROP_REQUEST_FMT = "<BBBBHHI"  # matches PCNetGameDropRequestMsg, 12 bytes
DROP_RESULT_FMT = "<BBBBIHH"  # matches PCNetGameDropResultMsg, 12 bytes

EMPTY_NO = 0x0000

# mFI_UNIT_BASE_SIZE (m_field_info.h) -- one field tile is 40x40 world units.
UNIT_SIZE = 40.0


def tile_center(ut_x, ut_z):
    """Approximates mFI_UtNum2CenterWpos() (m_field_info.c) closely enough for these scripts'
    purposes: the host's own PC_NETGAME_PICKUP_MAX_REACH_SQ/_Y tolerances (pc_net_game.c) are
    deliberately generous (50-unit 2D radius, 40-unit vertical) specifically to absorb slack like a
    test client's approximate tile-center guess, not just real position-sync staleness."""
    return ut_x * UNIT_SIZE + UNIT_SIZE / 2.0, 0.0, ut_z * UNIT_SIZE + UNIT_SIZE / 2.0


def send_hdr(sock, addr, wtype, kind, payload):
    pkt = struct.pack(WIRE_HDR_FMT, PCNET_MAGIC, wtype, kind, len(payload)) + payload
    sock.sendto(pkt, addr)


class MsgSpec:
    """Pairs a wire message's struct format with a namedtuple of field names, so a typed receive
    can filter/return fields by NAME (e.g. `.request_id`) instead of a bare positional index into
    struct.unpack()'s result tuple. See build_msg_spec() for the safety check this buys."""

    __slots__ = ("msg_type", "fmt", "size", "tuple_cls")

    def __init__(self, msg_type, fmt, size, tuple_cls):
        self.msg_type = msg_type
        self.fmt = fmt
        self.size = size
        self.tuple_cls = tuple_cls


def build_msg_spec(msg_type, fmt, field_names, type_name=None):
    """Builds a MsgSpec for use with FakeClient._recv_typed().

    A prior audit of test_drop_sync.py's original _recv_typed(msg_type, fmt, timeout,
    expect_request_id, request_id_index) flagged its `request_id_index` parameter as a
    silent-misfiltering risk: an int index into the unpacked tuple that has no enforced
    relationship to `fmt`'s actual field order, so a future edit to one without the other would
    fail silently (filtering on the wrong field) rather than loudly. This builds a namedtuple from
    `field_names` instead, and asserts right here -- at spec-construction time, not first-use --
    that `fmt` unpacks to exactly as many fields as `field_names` names, so any drift between the
    two is caught immediately rather than discovered as a mysteriously-always-filtered-out result
    later.
    """
    size = struct.calcsize(fmt)
    tuple_cls = namedtuple(type_name or f"Msg{msg_type}Fields", field_names)
    dummy = struct.unpack(fmt, b"\x00" * size)
    if len(dummy) != len(tuple_cls._fields):
        raise AssertionError(
            f"build_msg_spec(msg_type={msg_type}): fmt {fmt!r} unpacks to {len(dummy)} field(s) "
            f"but field_names {field_names!r} names {len(tuple_cls._fields)} -- these must match "
            "1:1, in order."
        )
    return MsgSpec(msg_type, fmt, size, tuple_cls)


PICKUP_RESULT_SPEC = build_msg_spec(
    PC_NETGAME_MSG_PICKUP_RESULT,
    PICKUP_RESULT_FMT,
    ["msg_type", "accepted", "ut_x", "ut_z", "request_id", "granted_item", "reserved"],
    "PickupResultFields",
)


class FakeClient:
    """One hand-crafted client connection: HELLO -> IDENTITY -> wait ACK -> READY.

    Shared base for test_pickup_sync.py's and test_drop_sync.py's own FakeClient subclasses, which
    add their own feature-specific request/result methods (test_pickup_sync.py's ping(),
    test_drop_sync.py's send_drop_request()/recv_drop_result()). Neither script's client sends its
    own APPEARANCE (not needed for exercising the pickup/drop protocols) unlike
    test_appearance_sync.py's own, separate FakeClient.
    """

    def __init__(self, label, host_ip, port):
        self.label = label
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(2.0)
        self.addr = (host_ip, port)
        self.assigned_peer_id = None
        self.move_frame = 0

    def connect_and_ready(self):
        send_hdr(self.sock, self.addr, PCNET_WIRE_HELLO, 0, b"")
        data, _ = self.sock.recvfrom(2048)
        magic, wtype, _wkind, _wsize = struct.unpack(WIRE_HDR_FMT, data[:8])
        if magic != PCNET_MAGIC or wtype != PCNET_WIRE_HELLO_ACK:
            raise RuntimeError(f"{self.label}: expected HELLO_ACK, got type={wtype}")

        identity = struct.pack(
            IDENTITY_FMT, PC_NETGAME_MSG_IDENTITY, PC_NETGAME_PROTOCOL_VERSION, b"\x00" * 8, b"\x00" * 8, 0, 0, 0
        )
        send_hdr(self.sock, self.addr, PCNET_WIRE_DATA, PC_NET_RELIABLE, identity)

        for _ in range(20):
            data, _ = self.sock.recvfrom(2048)
            magic, wtype, _wkind, wsize = struct.unpack(WIRE_HDR_FMT, data[:8])
            if magic != PCNET_MAGIC or wtype != PCNET_WIRE_DATA:
                continue
            payload = data[8 : 8 + wsize]
            if len(payload) >= 1 and payload[0] == PC_NETGAME_MSG_IDENTITY_ACK:
                ack = struct.unpack(ACK_FMT, payload[: struct.calcsize(ACK_FMT)])
                self.assigned_peer_id = ack[2]
                break
        else:
            raise RuntimeError(f"{self.label}: no IDENTITY_ACK received -- not READY")

        print(f"[{self.label}] READY (assigned_peer_id={self.assigned_peer_id})")

    def send_heartbeat(self):
        """Stage 5B-2: sends a bare PCNET_WIRE_HEARTBEAT packet (pc_net.c) -- "still here", refreshing
        this peer's last_recv_tick on the host so it doesn't hit PCNET_TIMEOUT_MS (5000ms, pc_net.c)
        of apparent silence. A real game client's own transport polls every frame and sends this
        automatically once a connection goes idle (pc_net.c:315-332); FakeClient has no such per-frame
        loop -- it only ever sends when a test script explicitly tells it to -- so a client that sits
        out a long stretch of another peer's own back-and-forth (e.g. a slow fixture scan, or several
        rejected probes each waited out to their own timeout) can otherwise genuinely time out and be
        disconnected by the host mid-script, breaking any LATER check that assumed it was still
        connected. Added when exactly this was observed happening to idle bystander clients during a
        multi-probe Stage 5B-2 neighbor-search test in test_drop_sync.py. Callers with a long-running
        client of their own (e.g. a fixture-search loop) do not need this -- every send already
        refreshes last_recv_tick on its own; this is only for a peer that is otherwise doing nothing
        for a while."""
        send_hdr(self.sock, self.addr, PCNET_WIRE_HEARTBEAT, 0, b"")

    def claim_position(self, x, y, z, facing=0):
        """Sends a movement sample claiming this exact position (and, since Stage 5B-2, this exact
        facing angle -- native engine units, matching PCNetMoveMsg.facing_angle; 0 by default,
        preserving every existing caller's behavior exactly). This is a legitimate (not a
        cheat/bug-exploiting) way to satisfy the host's pickup/drop distance/search checks in an
        automated test: Stage 3 movement sync performs no anti-cheat/physics validation, by
        explicit, documented design (pc_net_game.c) -- a fake client may freely claim to be
        standing at any position/facing, including exactly at the center of whichever tile it is
        about to probe, facing whichever direction picks a specific neighbor in
        pcnetgame_resolve_vanilla_drop_tile()'s octant search."""
        self.move_frame += 1
        msg = struct.pack(MOVE_FMT, PC_NETGAME_MSG_MOVE, 0, 0, -1, self.move_frame, x, y, z, facing, 0, 0.0)
        send_hdr(self.sock, self.addr, PCNET_WIRE_DATA, PC_NET_RELIABLE, msg)

    def send_pickup_request(self, ut_x, ut_z, request_id):
        msg = struct.pack(PICKUP_REQUEST_FMT, PC_NETGAME_MSG_PICKUP_REQUEST, ut_x, ut_z, 0, request_id)
        send_hdr(self.sock, self.addr, PCNET_WIRE_DATA, PC_NET_RELIABLE, msg)

    def _recv_typed(self, spec, timeout, expect_request_id=None):
        """Generic typed-receive helper behind recv_pickup_result() (below) and
        test_drop_sync.py's recv_drop_result(): waits up to `timeout` for a datagram matching
        `spec`'s message type/size, skipping (without early-returning on) any other traffic that
        arrives first -- important against a busy host with other peers' traffic in flight (move,
        appearance, another peer's pickup/drop activity, ...). If `expect_request_id` is given, a
        match for a DIFFERENT request_id is also skipped rather than returned -- this matters when
        this same client's own earlier request's result arrives late/out of order: returning a
        stale, mismatched result as if it answered the CURRENT request would be a false negative
        (this exact failure mode was observed and root-caused during Stage 5A.1 hardening against a
        busy long-lived host).

        Returns `spec.tuple_cls` instance (fields accessible by name, e.g. `.request_id`) or None.
        """
        deadline = time.time() + timeout
        self.sock.settimeout(max(0.05, deadline - time.time()))
        while time.time() < deadline:
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                break
            magic, wtype, _wkind, wsize = struct.unpack(WIRE_HDR_FMT, data[:8])
            if magic != PCNET_MAGIC or wtype != PCNET_WIRE_DATA:
                self.sock.settimeout(max(0.05, deadline - time.time()))
                continue
            payload = data[8 : 8 + wsize]
            if len(payload) == spec.size and payload[0] == spec.msg_type:
                fields = spec.tuple_cls._make(struct.unpack(spec.fmt, payload))
                if expect_request_id is not None and fields.request_id != expect_request_id:
                    self.sock.settimeout(max(0.05, deadline - time.time()))
                    continue
                return fields
            self.sock.settimeout(max(0.05, deadline - time.time()))
        return None

    def recv_pickup_result(self, timeout=1.0, expect_request_id=None):
        """Waits up to `timeout` for a PICKUP_RESULT, returning
        (accepted, ut_x, ut_z, request_id, granted_item) or None. See _recv_typed()'s doc for why
        unrelated traffic and mismatched request ids are skipped rather than misreported."""
        fields = self._recv_typed(PICKUP_RESULT_SPEC, timeout, expect_request_id)
        if fields is None:
            return None
        return (fields.accepted, fields.ut_x, fields.ut_z, fields.request_id, fields.granted_item)

    def drain_field_updates(self, timeout=0.5):
        """Collects every FIELD_UPDATE seen in the window, as a list of (ut_x, ut_z, new_fg_value)."""
        out = []
        deadline = time.time() + timeout
        self.sock.settimeout(max(0.05, deadline - time.time()))
        while time.time() < deadline:
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                break
            magic, wtype, _wkind, wsize = struct.unpack(WIRE_HDR_FMT, data[:8])
            if magic != PCNET_MAGIC or wtype != PCNET_WIRE_DATA:
                continue
            payload = data[8 : 8 + wsize]
            if len(payload) == struct.calcsize(FIELD_UPDATE_FMT) and payload[0] == PC_NETGAME_MSG_FIELD_UPDATE:
                fields = struct.unpack(FIELD_UPDATE_FMT, payload)
                _msg_type, ut_x, ut_z, _reserved0, new_fg_value, _reserved1 = fields
                out.append((ut_x, ut_z, new_fg_value))
            self.sock.settimeout(max(0.05, deadline - time.time()))
        return out


# The exact same 30 tiles pc_net_game.c's pcnetgame_run_pickup_test_seed() seeds when the host is
# launched with --pickup-test-seed -- kept as a literal, documented copy (not re-derived/computed)
# so a diff to the production list is easy to spot and mirror here. One tile per acre.
FIXTURE_CANDIDATE_TILES = [
    (8, 8), (24, 8), (40, 8), (56, 8), (72, 8),
    (8, 24), (24, 24), (40, 24), (56, 24), (72, 24),
    (8, 40), (24, 40), (40, 40), (56, 40), (72, 40),
    (8, 56), (24, 56), (40, 56), (56, 56), (72, 56),
    (8, 72), (24, 72), (40, 72), (56, 72), (72, 72),
    (8, 88), (24, 88), (40, 88), (56, 88), (72, 88),
]


class CandidateQueue:
    """Shared, stateful source of "which tile to try next", consumed across every discovery call
    in one test run so nothing is ever probed twice. Yields FIXTURE_CANDIDATE_TILES first (fast,
    deterministic when the host was launched with --pickup-test-seed).

    `fallback=True` (test_pickup_sync.py's original CandidateQueue behavior, and this class'
    default) additionally falls back to a broad row-major scan of the rest of the addressable town
    once the fixture tiles are exhausted, for a host that wasn't launched with --pickup-test-seed.

    `fallback=False` (test_drop_sync.py's original, simpler TileQueue behavior) stops once the
    fixture tiles are exhausted instead of scanning further -- drop's fixture strategy only ever
    needs fixture tiles (an untouched one, or one it empties itself via a real pickup), so a broad
    scan would only slow down (and never help) its SKIP-on-exhaustion checks.
    """

    def __init__(self, fallback=True):
        self._fixture = list(FIXTURE_CANDIDATE_TILES)
        self._fallback = (
            ((x, z) for z in range(0, 96) for x in range(0, 80) if (x, z) not in FIXTURE_CANDIDATE_TILES)
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

    # test_drop_sync.py's original TileQueue used this name; kept as an alias so either reads
    # naturally at a call site.
    pop_next = next_tile


def make_request_id_counter(start):
    """Returns a fresh_request_id() closure seeded at `start`, so the first id it returns is
    `start + 1`. Callers pass a disjoint `start` per script (test_pickup_sync.py's 1000,
    test_drop_sync.py's 2000) so the two scripts' request ids never collide if both are ever
    (accidentally) pointed at the same host."""
    counter = [start]

    def fresh_request_id():
        counter[0] += 1
        return counter[0]

    return fresh_request_id


def check(desc, cond, results):
    print(("PASS" if cond else "FAIL") + " - " + desc)
    results.append(bool(cond))


def summary_and_exit_code(results):
    """Prints the standard pass-count footer and returns the process exit code (0 if every check
    in `results` passed, 1 otherwise)."""
    print("-" * 60)
    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1
