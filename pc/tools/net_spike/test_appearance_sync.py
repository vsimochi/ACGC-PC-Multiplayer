#!/usr/bin/env python3
"""test_appearance_sync.py - Stage 4C-1 (backfill/ordering/resend fix) synthetic appearance test.

Hand-crafts wire bytes (like test_version_mismatch.py/test_move_sync.py) to drive a REAL,
currently-running `AnimalCrossing.exe --host <port>` process through a 3-peer sequential-join
scenario, verifying:
  - each fake client's own appearance is sent only after it processes IDENTITY_ACK (mirrors the
    real client's new post-ACK send site -- this script never sends APPEARANCE before its own ACK
    either, so it exercises exactly the same causal ordering the fix relies on),
  - the host relays each newly-arrived appearance to already-READY peers (pre-existing behavior,
    unaffected by this fix),
  - a peer that joins LATER (client C) receives the appearance of every peer that joined BEFORE it
    (client A, client B) via the host's newcomer backfill -- this is the regression test for the
    original Stage 4C-1 3+ player backfill bug.

This script only sends/receives UDP packets and reports what it observed; it never touches
rendering, a save file, or any window -- not a manual gameplay test.

Usage: python3 test_appearance_sync.py <host_ip> <port>
"""
import socket
import struct
import sys
import time

PCNET_MAGIC = 0x41434E50

PCNET_WIRE_HELLO = 0
PCNET_WIRE_HELLO_ACK = 1
PCNET_WIRE_HEARTBEAT = 2
PCNET_WIRE_DISCONNECT = 3
PCNET_WIRE_DATA = 4

PC_NET_RELIABLE = 1

PC_NETGAME_MSG_IDENTITY = 1
PC_NETGAME_MSG_IDENTITY_ACK = 2
PC_NETGAME_MSG_APPEARANCE = 5

PC_NETGAME_PROTOCOL_VERSION = 1
PC_NETGAME_HOST_PLAYER_ID = 8  # PC_NET_MAX_PEERS

WIRE_HDR_FMT = "<IBBH"
IDENTITY_FMT = "<B3xI8s8sHHB3x"
ACK_FMT = "<BBHI8s8sHHB3x"
# PCNetGameAppearanceMsg header only (msg_type, net_player_id, gender, face, cloth_item,
# sunburn_rank, is_custom_design); the rest (_reserved0[24] + the 544-byte design) is sent/ignored
# as raw zero padding -- this test only exercises catalog clothing (is_custom_design=0), so the
# design payload is never meaningful here.
APPEARANCE_HDR_FMT = "<BBBBHBB"
APPEARANCE_MSG_SIZE = 576
APPEARANCE_PAD_SIZE = APPEARANCE_MSG_SIZE - struct.calcsize(APPEARANCE_HDR_FMT)


def send_hdr(sock, addr, wtype, kind, payload):
    pkt = struct.pack(WIRE_HDR_FMT, PCNET_MAGIC, wtype, kind, len(payload)) + payload
    sock.sendto(pkt, addr)


def build_appearance(gender, face, cloth_item, sunburn_rank=0):
    header = struct.pack(APPEARANCE_HDR_FMT, PC_NETGAME_MSG_APPEARANCE, 0, gender, face, cloth_item, sunburn_rank, 0)
    return header + b"\x00" * APPEARANCE_PAD_SIZE


class FakeClient:
    """One hand-crafted client connection: HELLO -> IDENTITY -> wait ACK -> send APPEARANCE.

    seen_appearances accumulates for the client's whole lifetime (keyed by net_player_id), fed by
    both the ACK-wait loop and drain_appearances() -- an APPEARANCE datagram can legitimately arrive
    interleaved with (even before) the ACK itself, since the host sends the roster immediately after
    the ACK in the same synchronous call; this must never be silently discarded by whichever loop
    happens to read it off the socket first.
    """

    def __init__(self, label, host_ip, port, gender, face, cloth_item):
        self.label = label
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(3.0)
        self.addr = (host_ip, port)
        self.gender, self.face, self.cloth_item = gender, face, cloth_item
        self.assigned_peer_id = None
        self.seen_appearances = {}

    def _record_if_appearance(self, payload):
        if len(payload) == APPEARANCE_MSG_SIZE and payload[0] == PC_NETGAME_MSG_APPEARANCE:
            fields = struct.unpack(APPEARANCE_HDR_FMT, payload[: struct.calcsize(APPEARANCE_HDR_FMT)])
            _msg_type, net_player_id, gender, face, cloth_item, sunburn_rank, is_custom = fields
            self.seen_appearances[net_player_id] = (gender, face, cloth_item)
            return True
        return False

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
            if self._record_if_appearance(payload):
                continue  # stashed; keep waiting for the ACK specifically
            if len(payload) >= 1 and payload[0] == PC_NETGAME_MSG_IDENTITY_ACK:
                ack = struct.unpack(ACK_FMT, payload[: struct.calcsize(ACK_FMT)])
                self.assigned_peer_id = ack[2]
                break
        else:
            raise RuntimeError(f"{self.label}: no IDENTITY_ACK received -- not READY")

        print(f"[{self.label}] READY (assigned_peer_id={self.assigned_peer_id})")

        # Ordering-race fix under test: appearance is sent ONLY here, after the ACK -- never
        # earlier (mirrors the real client's PC_NETGAME_MSG_IDENTITY_ACK-handler send site).
        amsg = build_appearance(self.gender, self.face, self.cloth_item)
        send_hdr(self.sock, self.addr, PCNET_WIRE_DATA, PC_NET_RELIABLE, amsg)
        print(f"[{self.label}] sent own appearance (gender={self.gender} face={self.face} cloth_item={self.cloth_item:#x})")

    def ping(self):
        """Bare keepalive with no wait -- see drain_appearances()'s doc comment for why this is
        needed at all; used here where we need to stay alive without also blocking on a read."""
        send_hdr(self.sock, self.addr, PCNET_WIRE_HEARTBEAT, 0, b"")

    def disconnect(self):
        """Sends the fast-path PCNET_WIRE_DISCONNECT (pc_net.c frees the slot immediately and
        fires PC_NET_EVENT_PEER_DISCONNECTED -- no need to wait out the ~5s idle timeout)."""
        send_hdr(self.sock, self.addr, PCNET_WIRE_DISCONNECT, 0, b"")

    def drain_appearances(self, timeout=1.0):
        # PCNET_TIMEOUT_MS is 5000ms of total silence from this peer (pc_net.c) -- this script's
        # own multi-second sleeps between steps would otherwise time this fake client out exactly
        # like a real dead connection, which is correct host behavior but not what this test is
        # measuring. A real client's own 20Hz movement stream naturally prevents this; a HEARTBEAT
        # is the equivalent minimal "still here" for a client that sends nothing else.
        send_hdr(self.sock, self.addr, PCNET_WIRE_HEARTBEAT, 0, b"")
        self.sock.settimeout(timeout)
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                break
            magic, wtype, _wkind, wsize = struct.unpack(WIRE_HDR_FMT, data[:8])
            if magic != PCNET_MAGIC or wtype != PCNET_WIRE_DATA:
                continue
            self._record_if_appearance(data[8 : 8 + wsize])
        self.sock.settimeout(3.0)
        return dict(self.seen_appearances)


def check(label, condition, results):
    print(f"{'PASS' if condition else 'FAIL'} - {label}")
    results.append(condition)


def main():
    if len(sys.argv) != 3:
        print("usage: test_appearance_sync.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    results = []

    # --- Test A: 2 players -------------------------------------------------------------------
    a = FakeClient("A", host_ip, port, gender=0, face=1, cloth_item=0x2401)
    a.connect_and_ready()
    time.sleep(0.3)
    a.drain_appearances(timeout=1.0)
    check("Test A: A received host appearance", PC_NETGAME_HOST_PLAYER_ID in a.seen_appearances, results)

    b = FakeClient("B", host_ip, port, gender=1, face=2, cloth_item=0x2402)
    b.connect_and_ready()
    time.sleep(0.3)
    b.drain_appearances(timeout=1.0)
    check("Test A: B received host appearance", PC_NETGAME_HOST_PLAYER_ID in b.seen_appearances, results)
    check("Test A: B received A's appearance (backfill)", a.assigned_peer_id in b.seen_appearances, results)

    a.drain_appearances(timeout=1.0)
    check("Test A: A received B's appearance (relay)", b.assigned_peer_id in a.seen_appearances, results)

    # --- Test B: 3 players, sequential join (the mandatory regression test) ------------------
    c = FakeClient("C", host_ip, port, gender=0, face=3, cloth_item=0x2403)
    c.connect_and_ready()
    time.sleep(0.3)
    c.drain_appearances(timeout=1.5)
    check("Test B: C received host appearance", PC_NETGAME_HOST_PLAYER_ID in c.seen_appearances, results)
    check("Test B: C received A's appearance (backfill)", a.assigned_peer_id in c.seen_appearances, results)
    check(
        "Test B: C received B's appearance (backfill) [THE REGRESSION TEST]",
        b.assigned_peer_id in c.seen_appearances,
        results,
    )

    a.drain_appearances(timeout=1.5)
    b.drain_appearances(timeout=1.5)
    check("Test B: A received C's appearance (relay)", c.assigned_peer_id in a.seen_appearances, results)
    check("Test B: B received C's appearance (relay)", c.assigned_peer_id in b.seen_appearances, results)

    # --- Test D: reconnect -- old appearance must not leak from the previous connection -------
    # A and C must stay alive (PCNET_TIMEOUT_MS is 5s of total silence, pc_net.c) through this
    # whole sequence purely so THEY can keep observing -- ping them explicitly rather than relying
    # on incidental drain_appearances() calls, since a real client's own 20Hz movement stream would
    # make this a non-issue but these fake clients send nothing else.
    old_b_peer_id = b.assigned_peer_id
    stale_tuple_on_a = a.seen_appearances.get(old_b_peer_id)  # B's OLD data, if any -- must not persist
    print(f"[B] disconnecting (was peer {old_b_peer_id}, appearance gender=1 face=2 cloth_item=0x2402)")
    b.disconnect()
    a.ping()
    c.ping()
    time.sleep(0.5)  # let PC_NET_EVENT_PEER_DISCONNECTED reach the host and clear the slot

    b2 = FakeClient("B2(reconnected)", host_ip, port, gender=0, face=7, cloth_item=0x2410)
    b2.connect_and_ready()
    a.ping()
    c.ping()
    time.sleep(0.3)
    a.drain_appearances(timeout=1.5)
    c.drain_appearances(timeout=1.5)

    reused_slot = b2.assigned_peer_id == old_b_peer_id
    print(f"[B2] assigned_peer_id={b2.assigned_peer_id} (old B was {old_b_peer_id}, slot reused: {reused_slot})")
    check(
        "Test D: A's view of B2's slot shows B2's NEW appearance, not B's old one",
        a.seen_appearances.get(b2.assigned_peer_id) == (b2.gender, b2.face, b2.cloth_item),
        results,
    )
    check(
        "Test D: C's view of B2's slot shows B2's NEW appearance, not B's old one",
        c.seen_appearances.get(b2.assigned_peer_id) == (b2.gender, b2.face, b2.cloth_item),
        results,
    )
    if reused_slot:
        check(
            "Test D: (slot was reused) A's cached tuple for that slot changed from B's old value",
            a.seen_appearances.get(b2.assigned_peer_id) != stale_tuple_on_a,
            results,
        )

    print("-" * 60)
    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
