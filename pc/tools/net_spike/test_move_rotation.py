#!/usr/bin/env python3
"""test_move_rotation.py - Stage 3 validation: shortest-path rotation across the +32767/-32768
signed-angle wraparound boundary, with no translation (pure rotation-in-place).

Sends two snapshots at the SAME position but with facing_angle on opposite sides of the wrap
boundary (32000, then -32000 -- only ~1536 units apart the short way, but 64000 apart the naive
long way). Correct shortest-path interpolation must swing straight through the +32767/-32768
seam; a naive linear lerp of the raw values would instead crawl the long way through 0.

Cross-reference the host's own [NET][REMOTE][DIAG] position-dump angle= values against the
printed timestamps below.

Usage: python3 test_move_rotation.py <host_ip> <port>
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
MOVE_FMT = "<BBBbIfffhhf"


def send_hdr(sock, addr, wtype, kind, payload):
    sock.sendto(struct.pack(WIRE_HDR_FMT, PCNET_MAGIC, wtype, kind, len(payload)) + payload, addr)


def send_move(sock, addr, frame, angle):
    msg = struct.pack(MOVE_FMT, PC_NETGAME_MSG_MOVE, 0, 0, -1, frame, 5000.0, 40.0, 5000.0, angle, 0, 0.0)
    send_hdr(sock, addr, PCNET_WIRE_DATA, PC_NET_UNRELIABLE, msg)


def main():
    if len(sys.argv) != 3:
        print("usage: test_move_rotation.py <host_ip> <port>")
        return 1
    addr = (sys.argv[1], int(sys.argv[2]))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)

    send_hdr(sock, addr, PCNET_WIRE_HELLO, 0, b"")
    data, _ = sock.recvfrom(2048)
    magic, wtype, _, _ = struct.unpack(WIRE_HDR_FMT, data[:8])
    if magic != PCNET_MAGIC or wtype != PCNET_WIRE_HELLO_ACK:
        print("FAIL: no HELLO_ACK")
        return 1

    identity = struct.pack(IDENTITY_FMT, PC_NETGAME_MSG_IDENTITY, PC_NETGAME_PROTOCOL_VERSION,
                            b"\x00" * 8, b"\x00" * 8, 0, 0, 0)
    send_hdr(sock, addr, PCNET_WIRE_DATA, PC_NET_RELIABLE, identity)
    ready = False
    for _ in range(10):
        try:
            data, _ = sock.recvfrom(2048)
        except socket.timeout:
            break
        magic, wtype, _, wsize = struct.unpack(WIRE_HDR_FMT, data[:8])
        if magic == PCNET_MAGIC and wtype == PCNET_WIRE_DATA and data[8] == PC_NETGAME_MSG_IDENTITY_ACK:
            ready = True
            break
    if not ready:
        print("FAIL: not READY")
        return 1
    print(f"[{time.time():.3f}] READY")

    # Settle at angle=32000 for several snapshots (past the interp delay) before flipping.
    for i in range(6):
        send_move(sock, addr, frame=300 + i, angle=32000)
        time.sleep(0.15)
    print(f"[{time.time():.3f}] settled at angle=32000, same position (5000,40,5000)")

    # Now flip to -32000: the short way is straight through the +32767/-32768 seam (~1536
    # units); the long way (a naive linear lerp of the raw values) would be 64000 units through 0.
    for i in range(6):
        send_move(sock, addr, frame=306 + i, angle=-32000)
        time.sleep(0.15)
    print(f"[{time.time():.3f}] flipped to angle=-32000, same position -- watch the host's "
          f"[NET][REMOTE][DIAG] angle= values swing through +/-32768, not crawl through 0")
    time.sleep(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
