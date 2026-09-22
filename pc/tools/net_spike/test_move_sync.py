#!/usr/bin/env python3
"""test_move_sync.py - Stage 3 synthetic movement-packet test.

Hand-crafts wire bytes (like test_version_mismatch.py) to drive a REAL, currently-running
`AnimalCrossing.exe --host <port>` process through: normal handshake -> a stream of MOVE
snapshots with increasing frame -> a deliberately stale/duplicate frame (should be rejected,
visible as a "[NET][REMOTE][DIAG] rejected stale/duplicate/reordered" line in the host's own
stdout/log) -> a deliberately huge position jump (should be a "[NET][REMOTE][DIAG] teleport snap"
line, not a slow slide).

This script only sends packets and reports what it sent; it cannot see the host's own log (run
with the host's stdout redirected to a file and grep it separately, exactly like
test_version_mismatch.py's caller does).

Usage: python3 test_move_sync.py <host_ip> <port>
"""
import socket
import struct
import sys
import time

PCNET_MAGIC = 0x41434E50

PCNET_WIRE_HELLO = 0
PCNET_WIRE_HELLO_ACK = 1
PCNET_WIRE_DATA = 4

PC_NET_RELIABLE = 1
PC_NET_UNRELIABLE = 0

PC_NETGAME_MSG_IDENTITY = 1
PC_NETGAME_MSG_IDENTITY_ACK = 2
PC_NETGAME_MSG_MOVE = 4

PC_NETGAME_PROTOCOL_VERSION = 1

WIRE_HDR_FMT = "<IBBH"
IDENTITY_FMT = "<B3xI8s8sHHB3x"
ACK_FMT = "<BBHI8s8sHHB3x"
# PCNetMoveMsg: uint8 msg_type, net_player_id, move_state; int8 item_kind; uint32 frame;
# float pos_x, pos_y, pos_z; int16 facing_angle, _reserved0; float speed.  (28 bytes)
MOVE_FMT = "<BBBbIfffhhf"


def send_hdr(sock, addr, wtype, kind, payload):
    pkt = struct.pack(WIRE_HDR_FMT, PCNET_MAGIC, wtype, kind, len(payload)) + payload
    sock.sendto(pkt, addr)


def build_move(frame, x, y, z, angle=0, speed=0.0, move_state=1, item_kind=-1):
    return struct.pack(MOVE_FMT, PC_NETGAME_MSG_MOVE, 0, move_state, item_kind, frame, x, y, z, angle, 0, speed)


def main():
    if len(sys.argv) != 3:
        print("usage: test_move_sync.py <host_ip> <port>")
        return 1
    host_ip, port = sys.argv[1], int(sys.argv[2])
    addr = (host_ip, port)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)

    send_hdr(sock, addr, PCNET_WIRE_HELLO, 0, b"")
    data, _ = sock.recvfrom(2048)
    magic, wtype, wkind, wsize = struct.unpack(WIRE_HDR_FMT, data[:8])
    if magic != PCNET_MAGIC or wtype != PCNET_WIRE_HELLO_ACK:
        print(f"FAIL: expected HELLO_ACK, got type={wtype}")
        return 1
    print("transport-connected")

    identity = struct.pack(IDENTITY_FMT, PC_NETGAME_MSG_IDENTITY, PC_NETGAME_PROTOCOL_VERSION,
                            b"\x00" * 8, b"\x00" * 8, 0, 0, 0)
    send_hdr(sock, addr, PCNET_WIRE_DATA, PC_NET_RELIABLE, identity)

    got_ack = False
    for _ in range(10):
        try:
            data, _ = sock.recvfrom(2048)
        except socket.timeout:
            break
        magic, wtype, wkind, wsize = struct.unpack(WIRE_HDR_FMT, data[:8])
        if magic != PCNET_MAGIC or wtype != PCNET_WIRE_DATA:
            continue
        payload = data[8:8 + wsize]
        if len(payload) >= 1 and payload[0] == PC_NETGAME_MSG_IDENTITY_ACK:
            got_ack = True
            break
    if not got_ack:
        print("FAIL: no IDENTITY_ACK received -- not READY, cannot send MOVE")
        return 1
    print("READY -- sending movement test sequence")

    # 1) Normal increasing-frame stream (should all be accepted).
    for i in range(5):
        msg = build_move(frame=100 + i, x=1000.0 + i * 10.0, y=40.0, z=2000.0, angle=1000 * i, speed=2.0)
        send_hdr(sock, addr, PCNET_WIRE_DATA, PC_NET_UNRELIABLE, msg)
        time.sleep(0.05)
    print("sent 5 increasing-frame snapshots (frame 100..104)")

    # 2) Deliberately stale/duplicate frame (should be rejected -- check the host's own log for
    #    "[NET][REMOTE][DIAG] rejected stale/duplicate/reordered ... incoming.frame=101").
    stale = build_move(frame=101, x=9999.0, y=40.0, z=9999.0, speed=99.0)
    send_hdr(sock, addr, PCNET_WIRE_DATA, PC_NET_UNRELIABLE, stale)
    print("sent stale/duplicate frame=101 (should be rejected; position must NOT jump to 9999,9999)")
    time.sleep(0.2)

    # 3) Exact duplicate of the most recent accepted frame (should also be rejected).
    dup = build_move(frame=104, x=1111.0, y=40.0, z=1111.0)
    send_hdr(sock, addr, PCNET_WIRE_DATA, PC_NET_UNRELIABLE, dup)
    print("sent exact-duplicate frame=104 (should be rejected)")
    time.sleep(0.2)

    # 4) A deliberately huge jump on the next real frame (should snap, not interpolate/slide --
    #    check for "[NET][REMOTE][DIAG] teleport snap" in the host's log).
    teleport = build_move(frame=105, x=50000.0, y=40.0, z=50000.0, speed=0.0)
    send_hdr(sock, addr, PCNET_WIRE_DATA, PC_NET_UNRELIABLE, teleport)
    print("sent teleport jump frame=105 to (50000,40,50000)")
    time.sleep(0.2)

    # 5) Resume normal nearby movement after the teleport.
    for i in range(3):
        msg = build_move(frame=106 + i, x=50000.0 + i * 5.0, y=40.0, z=50000.0, speed=1.0)
        send_hdr(sock, addr, PCNET_WIRE_DATA, PC_NET_UNRELIABLE, msg)
        time.sleep(0.05)
    print("sent 3 more snapshots near the teleported position (frame 106..108)")

    print("DONE -- inspect the host's own log for [NET][REMOTE][DIAG] lines to verify behavior")
    return 0


if __name__ == "__main__":
    sys.exit(main())
