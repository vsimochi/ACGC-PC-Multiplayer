#!/usr/bin/env python3
"""test_move_delay.py - Stage 3 validation: deliberately delayed/paused movement packets.

Same hand-crafted-wire-bytes approach as test_move_sync.py, against a REAL, currently-running
`AnimalCrossing.exe --host <port> --verbose` process. This script sends packets; it cannot see the
host's own log, so run with the host's stdout redirected to a file and grep that file for
"[NET][REMOTE][DIAG]" lines to check the actual outcome (this script only prints what it sent and
when).

Scenario:
  1. Send a normal short burst (frames 200..204) so the peer has real interpolation history.
  2. Sleep ~1.2s (well past the ~150ms interpolation delay) before sending frame 205 -- a
     genuinely LATE-ARRIVING but still-newer packet. Must be accepted (newer frame always wins),
     and the actor must not have jumped backward or produced garbage while waiting.
  3. Send a stale frame (204, already superseded) immediately after -- must be rejected.
  4. Go silent for ~6 seconds (no packets at all) -- the actor must HOLD at the last accepted
     position (checked via the host's own periodic [NET][REMOTE][DIAG] dump), never extrapolate
     indefinitely or drift.
  5. Resume sending a normal burst (frames 206..210) -- movement must recover normally.

Usage: python3 test_move_delay.py <host_ip> <port>
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
    pkt = struct.pack(WIRE_HDR_FMT, PCNET_MAGIC, wtype, kind, len(payload)) + payload
    sock.sendto(pkt, addr)


def build_move(frame, x, y, z, angle=0, speed=1.5, move_state=1, item_kind=-1):
    return struct.pack(MOVE_FMT, PC_NETGAME_MSG_MOVE, 0, move_state, item_kind, frame, x, y, z, angle, 0, speed)


def send_move(sock, addr, **kw):
    msg = build_move(**kw)
    send_hdr(sock, addr, PCNET_WIRE_DATA, PC_NET_UNRELIABLE, msg)


def main():
    if len(sys.argv) != 3:
        print("usage: test_move_delay.py <host_ip> <port>")
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
        if magic == PCNET_MAGIC and wtype == PCNET_WIRE_DATA:
            payload = data[8:8 + wsize]
            if len(payload) >= 1 and payload[0] == PC_NETGAME_MSG_IDENTITY_ACK:
                got_ack = True
                break
    if not got_ack:
        print("FAIL: no IDENTITY_ACK -- not READY")
        return 1
    print(f"[{time.time():.3f}] READY")

    for i in range(5):
        send_move(sock, addr, frame=200 + i, x=3000.0 + i * 10.0, y=40.0, z=4000.0)
        time.sleep(0.05)
    print(f"[{time.time():.3f}] sent normal burst frames 200..204")

    print(f"[{time.time():.3f}] sleeping 1.2s before sending a late-but-newer frame 205 ...")
    time.sleep(1.2)
    send_move(sock, addr, frame=205, x=3050.0, y=40.0, z=4000.0)
    print(f"[{time.time():.3f}] sent late frame=205 (must be accepted; newest becomes 205)")
    time.sleep(0.1)

    send_move(sock, addr, frame=204, x=9999.0, y=40.0, z=9999.0)
    print(f"[{time.time():.3f}] sent stale frame=204 with garbage position (must be rejected)")
    time.sleep(0.1)

    # Kept safely under pc_net.c's own PCNET_TIMEOUT_MS (5000ms) transport heartbeat timeout --
    # a longer silence would trigger a real (and, at the transport layer, correct) disconnect
    # before the recovery burst below ever arrives, which would test pc_net.c's timeout instead
    # of pc_remote_player's "hold, don't extrapolate" behavior.
    print(f"[{time.time():.3f}] going silent for 3s -- actor must HOLD, not extrapolate/drift ...")
    time.sleep(3.0)
    print(f"[{time.time():.3f}] silence over")

    for i in range(5):
        send_move(sock, addr, frame=206 + i, x=3050.0 + i * 10.0, y=40.0, z=4010.0)
        time.sleep(0.05)
    print(f"[{time.time():.3f}] sent recovery burst frames 206..210")

    # Keep the connection alive (below the transport timeout) so at least one more diagnostic
    # dump on the host's side reflects the recovered position before this script exits.
    time.sleep(2.0)

    print("DONE -- cross-reference timestamps above against the host's own "
          "[NET][REMOTE][DIAG] log lines to confirm: no backward jump at frame 205, "
          "stale frame 204 rejected, position held steady during the 6s silence, "
          "and movement resumed normally afterward.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
