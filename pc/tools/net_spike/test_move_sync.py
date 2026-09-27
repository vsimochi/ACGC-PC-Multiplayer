#!/usr/bin/env python3
"""test_move_sync.py - Stage 3 synthetic movement-packet test.

Drives a REAL, currently-running `AnimalCrossing.exe --host <port>` process through: normal
handshake -> a stream of MOVE snapshots with increasing frame -> a deliberately stale/duplicate
frame (should be rejected, visible as a "[NET][REMOTE][DIAG] rejected stale/duplicate/reordered"
line in the host's own stdout/log) -> a deliberately huge position jump (should be a
"[NET][REMOTE][DIAG] teleport snap" line, not a slow slide).

This script only sends packets and reports what it sent; it cannot see the host's own log (run
with the host's stdout redirected to a file and grep it separately).

Foundation-phase migration: the handshake (HELLO+nonce, IDENTITY over RDATA, IDENTITY_ACK) comes
from net_spike_lib.FakeClient. MOVE stays an UNRELIABLE DATA(type 4, kind 0) datagram with the
exact same bytes as before. Sleeps go through pump_sleep() so the library keeps ACKing the host's
reliable traffic (otherwise the new transport would retransmit and eventually drop us).

Usage: python3 test_move_sync.py <host_ip> <port>
"""
import sys

import net_spike_lib as L


def build_move(frame, x, y, z, angle=0, speed=0.0, move_state=1, item_kind=-1):
    return L.build_move(frame, x, y, z, angle=angle, speed=speed, move_state=move_state, item_kind=item_kind)


def main():
    hp = L.parse_host_port(sys.argv, "usage: test_move_sync.py <host_ip> <port>")
    if hp is None:
        return 1
    host_ip, port = hp

    c = L.FakeClient("move-sync", host_ip, port)
    try:
        c.connect_and_ready(timeout=3.0)
    except L.ConnectError:
        print("FAIL: expected HELLO_ACK, got nothing")
        return 1
    except (RuntimeError, L.HandshakeRejected) as e:
        print(f"FAIL: no IDENTITY_ACK received -- not READY, cannot send MOVE ({e})")
        return 1
    print("READY -- sending movement test sequence")

    # 1) Normal increasing-frame stream (should all be accepted).
    for i in range(5):
        c.send_unreliable(build_move(frame=100 + i, x=1000.0 + i * 10.0, y=40.0, z=2000.0, angle=1000 * i, speed=2.0))
        L.pump_sleep(0.05)
    print("sent 5 increasing-frame snapshots (frame 100..104)")

    # 2) Deliberately stale/duplicate frame (should be rejected -- check the host's own log for
    #    "[NET][REMOTE][DIAG] rejected stale/duplicate/reordered ... incoming.frame=101").
    c.send_unreliable(build_move(frame=101, x=9999.0, y=40.0, z=9999.0, speed=99.0))
    print("sent stale/duplicate frame=101 (should be rejected; position must NOT jump to 9999,9999)")
    L.pump_sleep(0.2)

    # 3) Exact duplicate of the most recent accepted frame (should also be rejected).
    c.send_unreliable(build_move(frame=104, x=1111.0, y=40.0, z=1111.0))
    print("sent exact-duplicate frame=104 (should be rejected)")
    L.pump_sleep(0.2)

    # 4) A deliberately huge jump on the next real frame (should snap, not interpolate/slide --
    #    check for "[NET][REMOTE][DIAG] teleport snap" in the host's log).
    c.send_unreliable(build_move(frame=105, x=50000.0, y=40.0, z=50000.0, speed=0.0))
    print("sent teleport jump frame=105 to (50000,40,50000)")
    L.pump_sleep(0.2)

    # 5) Resume normal nearby movement after the teleport.
    for i in range(3):
        c.send_unreliable(build_move(frame=106 + i, x=50000.0 + i * 5.0, y=40.0, z=50000.0, speed=1.0))
        L.pump_sleep(0.05)
    print("sent 3 more snapshots near the teleported position (frame 106..108)")

    print("DONE -- inspect the host's own log for [NET][REMOTE][DIAG] lines to verify behavior")
    return 0


if __name__ == "__main__":
    sys.exit(main())
