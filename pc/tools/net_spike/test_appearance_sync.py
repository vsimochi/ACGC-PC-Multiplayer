#!/usr/bin/env python3
"""test_appearance_sync.py - Stage 4C-1 (backfill/ordering/resend fix) + Stage 4C-2 (dynamic
appearance change) synthetic appearance test.

Hand-crafts wire bytes (like test_version_mismatch.py/test_move_sync.py) to drive a REAL,
currently-running `AnimalCrossing.exe --host <port>` process through several scenarios:

Stage 4C-1 (Test A/B/D below):
  - each fake client's own appearance is sent only after it processes IDENTITY_ACK (mirrors the
    real client's new post-ACK send site -- this script never sends APPEARANCE before its own ACK
    either, so it exercises exactly the same causal ordering the fix relies on),
  - the host relays each newly-arrived appearance to already-READY peers (pre-existing behavior,
    unaffected by this fix),
  - a peer that joins LATER (client C) receives the appearance of every peer that joined BEFORE it
    (client A, client B) via the host's newcomer backfill -- this is the regression test for the
    original Stage 4C-1 3+ player backfill bug.

Stage 4C-2 (Test client-change/stability/custom-design/reconnect-then-change below): a fake
client's appearance changing mid-session is just a second (or third, ...) PC_NETGAME_MSG_APPEARANCE
datagram sent from the same already-READY connection -- indistinguishable on the wire from what the
real client's new pc_net_game_poll() per-frame change-detection sends. This exercises the REAL,
running host binary's existing, unmodified receive+relay path
(pcnetgame_handle_host_appearance()) under a changed-appearance scenario, proving propagation is
immediate (via the synchronous relay-on-receipt already in place) rather than dependent on the
~3-second periodic resend. It does NOT exercise the new local-change-DETECTION code itself (that
requires a real Now_Private mutation via actual gameplay, which this script deliberately never
performs) -- see the Stage 4C-2 implementation report for what that gap means and why.

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
APPEARANCE_HDR_SIZE = struct.calcsize(APPEARANCE_HDR_FMT)
APPEARANCE_RESERVED0_SIZE = 24
APPEARANCE_DESIGN_SIZE = APPEARANCE_MSG_SIZE - APPEARANCE_HDR_SIZE - APPEARANCE_RESERVED0_SIZE  # 544
APPEARANCE_PAD_SIZE = APPEARANCE_MSG_SIZE - APPEARANCE_HDR_SIZE  # catalog case: reserved0 + zeroed design


def send_hdr(sock, addr, wtype, kind, payload):
    pkt = struct.pack(WIRE_HDR_FMT, PCNET_MAGIC, wtype, kind, len(payload)) + payload
    sock.sendto(pkt, addr)


def build_appearance(gender, face, cloth_item, sunburn_rank=0):
    header = struct.pack(APPEARANCE_HDR_FMT, PC_NETGAME_MSG_APPEARANCE, 0, gender, face, cloth_item, sunburn_rank, 0)
    return header + b"\x00" * APPEARANCE_PAD_SIZE


def build_custom_design_appearance(gender, face, design_fill_byte, sunburn_rank=0, palette_byte=5):
    """A real mNW_original_design_c-shaped design_record (name[16] + palette + flag_design_set +
    14 bytes of alignment padding to reach the 32-byte-aligned `design` field + 512 non-zero pixel
    bytes) -- same layout the Stage 4C-1 investigation's ad-hoc verification used, generalized here
    so Test D can distinguish two different custom designs by their fill byte. cloth_item is
    RSV_CLOTH (0xFE20) -- informational only on the wire; the receiver's classification is driven
    entirely by is_custom_design, matching pc_remote_player_on_appearance()'s own logic."""
    header = struct.pack(APPEARANCE_HDR_FMT, PC_NETGAME_MSG_APPEARANCE, 0, gender, face, 0xFE20, sunburn_rank, 1)
    reserved0 = b"\x00" * APPEARANCE_RESERVED0_SIZE
    name = b"TESTDESIGN\x00\x00\x00\x00\x00\x00"
    flag_design_set = bytes([1])
    pad_to_design_start = b"\x00" * (32 - (len(name) + 1 + len(flag_design_set)))
    design_pixels = bytes([design_fill_byte]) * 512
    design_record = name + bytes([palette_byte]) + flag_design_set + pad_to_design_start + design_pixels
    assert len(design_record) == APPEARANCE_DESIGN_SIZE, len(design_record)
    return header + reserved0 + design_record


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
            fields = struct.unpack(APPEARANCE_HDR_FMT, payload[:APPEARANCE_HDR_SIZE])
            _msg_type, net_player_id, gender, face, cloth_item, sunburn_rank, is_custom = fields
            # First byte of the 512-byte pixel payload -- enough to distinguish two different
            # custom designs (build_custom_design_appearance() fills each with a distinct byte)
            # without comparing all 512 bytes every time.
            design_fill_byte = payload[APPEARANCE_HDR_SIZE + APPEARANCE_RESERVED0_SIZE + 32]
            self.seen_appearances[net_player_id] = (gender, face, cloth_item, is_custom, design_fill_byte)
            return True
        return False

    def send_appearance_update(self, payload):
        """Sends a NEW appearance datagram on an already-connected/READY socket -- simulates the
        real client's pc_net_game_poll() detecting a local Now_Private change mid-session and
        sending immediately, without repeating the HELLO/IDENTITY/ACK handshake. `payload` is
        whatever build_appearance()/build_custom_design_appearance() returned."""
        send_hdr(self.sock, self.addr, PCNET_WIRE_DATA, PC_NET_RELIABLE, payload)

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
    b2_catalog_tuple = (b2.gender, b2.face, b2.cloth_item, 0, 0)  # catalog: is_custom=0, no design payload
    check(
        "Test D: A's view of B2's slot shows B2's NEW appearance, not B's old one",
        a.seen_appearances.get(b2.assigned_peer_id) == b2_catalog_tuple,
        results,
    )
    check(
        "Test D: C's view of B2's slot shows B2's NEW appearance, not B's old one",
        c.seen_appearances.get(b2.assigned_peer_id) == b2_catalog_tuple,
        results,
    )
    if reused_slot:
        check(
            "Test D: (slot was reused) A's cached tuple for that slot changed from B's old value",
            a.seen_appearances.get(b2.assigned_peer_id) != stale_tuple_on_a,
            results,
        )

    # --- Stage 4C-2 Dynamic-A: client appearance change propagates immediately (not via the ~3s
    # periodic resend) -- B2 (already READY from the reconnect test above) sends a SECOND, different
    # appearance on its existing connection, simulating what the real client's new
    # pc_net_game_poll() per-frame change-detection sends the instant it detects a local change. ---
    b2_new_cloth_item = 0x2420
    print(f"[B2] appearance CHANGE: cloth_item 0x{b2.cloth_item:x} -> 0x{b2_new_cloth_item:x}")
    b2.send_appearance_update(build_appearance(b2.gender, b2.face, b2_new_cloth_item))
    changed_tuple = (b2.gender, b2.face, b2_new_cloth_item, 0, 0)
    # Deliberately SHORT timeouts (well under the 3s periodic resend period) -- the assertion is
    # specifically that this does NOT need to wait for the safety-net resend to arrive.
    a.drain_appearances(timeout=1.0)
    c.drain_appearances(timeout=1.0)
    check(
        "Dynamic-A: A receives B2's changed appearance promptly (immediate relay, not the 3s resend)",
        a.seen_appearances.get(b2.assigned_peer_id) == changed_tuple,
        results,
    )
    check(
        "Dynamic-A: C receives B2's changed appearance promptly (immediate relay, not the 3s resend)",
        c.seen_appearances.get(b2.assigned_peer_id) == changed_tuple,
        results,
    )

    # --- Stage 4C-2 Dynamic-B: NOT exercised by this script -- see the note printed below. -------
    print(
        "Dynamic-B (host's own appearance change broadcasting to READY peers) is NOT exercised by "
        "this script: it requires the real host process's own Now_Private to actually change "
        "through real gameplay (equip a different shirt), which this hand-crafted-UDP-packet "
        "harness cannot trigger from outside the process without manual gameplay. See the Stage "
        "4C-2 implementation report for what was verified instead (code review + build only)."
    )

    # --- Stage 4C-2 Dynamic-C: stability -- no unprompted FLOOD of appearance packets when nothing
    # changed. A regression check specifically for the new per-frame comparison in
    # pc_net_game_poll(): if it had a bug that made it always report "changed" (e.g. a broken
    # memcmp, or the "first observation" baseline never actually getting marked valid), the host
    # would re-broadcast on every single frame instead of only the existing ~3s cadence, and C
    # would receive a large burst of appearance datagrams in a short window.
    #
    # NOTE: the count is checked against a small tolerance, not zero -- the pre-existing periodic
    # resend (unchanged by this stage) is still allowed to legitimately fire during this window
    # (its own ~3s timer runs independently of this test and may coincidentally land here); the
    # per-connected-peer message count from ONE resend cycle is a small, bounded number (each READY
    # peer gets the host's own appearance plus every OTHER READY peer's appearance once), whereas a
    # genuine every-frame flood bug would produce dozens of duplicate packets for the SAME peer in
    # this window -- the two are trivially distinguishable by count. ---------------------------------
    idle_events = []
    idle_deadline = time.time() + 2.0  # well under the 3s periodic-resend period, but generous
    c.sock.settimeout(0.5)
    while time.time() < idle_deadline:
        try:
            data, _ = c.sock.recvfrom(2048)
        except socket.timeout:
            continue
        magic, wtype, _wkind, wsize = struct.unpack(WIRE_HDR_FMT, data[:8])
        if magic == PCNET_MAGIC and wtype == PCNET_WIRE_DATA:
            payload = data[8 : 8 + wsize]
            if len(payload) == APPEARANCE_MSG_SIZE and payload[0] == PC_NETGAME_MSG_APPEARANCE:
                idle_events.append(payload)
    c.sock.settimeout(3.0)
    check(
        "Dynamic-C: no unprompted appearance FLOOD while nothing changed (saw "
        f"{len(idle_events)} packet(s) in a 2s idle window; a handful from one legitimate periodic "
        "resend is fine, dozens would indicate a per-frame flood bug)",
        len(idle_events) <= 4,
        results,
    )

    # --- Stage 4C-2 Dynamic-D: catalog -> custom design -> catalog, verifying the full design
    # payload propagates and the receiver correctly switches classification both directions. -------
    print("[C] appearance CHANGE: catalog -> custom design")
    c.send_appearance_update(build_custom_design_appearance(c.gender, c.face, design_fill_byte=0x11))
    a.drain_appearances(timeout=1.0)
    check(
        "Dynamic-D: A receives C's custom design (is_custom_design=1, correct pixel payload)",
        a.seen_appearances.get(c.assigned_peer_id) == (c.gender, c.face, 0xFE20, 1, 0x11),
        results,
    )

    print("[C] appearance CHANGE: custom design -> a DIFFERENT custom design")
    c.send_appearance_update(build_custom_design_appearance(c.gender, c.face, design_fill_byte=0x22))
    a.drain_appearances(timeout=1.0)
    check(
        "Dynamic-D: A receives C's switch to a different custom design (new pixel payload wins)",
        a.seen_appearances.get(c.assigned_peer_id) == (c.gender, c.face, 0xFE20, 1, 0x22),
        results,
    )

    print("[C] appearance CHANGE: custom design -> catalog")
    c.send_appearance_update(build_appearance(c.gender, c.face, 0x2405))
    a.drain_appearances(timeout=1.0)
    check(
        "Dynamic-D: A receives C's switch back to catalog (is_custom_design correctly clears)",
        a.seen_appearances.get(c.assigned_peer_id) == (c.gender, c.face, 0x2405, 0, 0),
        results,
    )

    # --- Stage 4C-2 Dynamic-E: reconnect, then verify a dynamic change AFTER reconnecting still
    # propagates normally (the initial-appearance-after-reconnect part is already Test D above). ---
    print(f"[B2] disconnecting (peer {b2.assigned_peer_id})")
    b2.disconnect()
    a.ping()
    time.sleep(0.5)

    b3 = FakeClient("B3(reconnected again)", host_ip, port, gender=1, face=4, cloth_item=0x2406)
    b3.connect_and_ready()
    a.ping()
    time.sleep(0.3)
    a.drain_appearances(timeout=1.0)
    check(
        "Dynamic-E: A receives B3's fresh initial appearance after a second reconnect",
        a.seen_appearances.get(b3.assigned_peer_id) == (b3.gender, b3.face, b3.cloth_item, 0, 0),
        results,
    )

    b3_new_cloth_item = 0x2407
    print(f"[B3] appearance CHANGE (post-reconnect): cloth_item 0x{b3.cloth_item:x} -> 0x{b3_new_cloth_item:x}")
    b3.send_appearance_update(build_appearance(b3.gender, b3.face, b3_new_cloth_item))
    a.drain_appearances(timeout=1.0)
    check(
        "Dynamic-E: a dynamic change AFTER reconnecting still propagates normally",
        a.seen_appearances.get(b3.assigned_peer_id) == (b3.gender, b3.face, b3_new_cloth_item, 0, 0),
        results,
    )

    print("-" * 60)
    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
