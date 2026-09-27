#!/usr/bin/env python3
"""test_move_rotation.py - Stage 3 validation: shortest-path rotation across the +32767/-32768
signed-angle wraparound boundary, with no translation (pure rotation-in-place).

Sends two snapshots at the SAME position but with facing_angle on opposite sides of the wrap
boundary (32000, then -32000 -- only ~1536 units apart the short way, but 64000 apart the naive
long way). Correct shortest-path interpolation must swing straight through the +32767/-32768
seam; a naive linear lerp of the raw values would instead crawl the long way through 0.

Cross-reference the host's own [NET][REMOTE][DIAG] position-dump angle= values against the
printed timestamps below.

Foundation-phase migration: handshake via net_spike_lib.FakeClient; MOVE bytes unchanged
(unreliable DATA, move_state 0, item_kind -1, speed 0); sleeps pump the transport.

Usage: python3 test_move_rotation.py <host_ip> <port>
"""
import sys
import time

import net_spike_lib as L


def main():
    hp = L.parse_host_port(sys.argv, "usage: test_move_rotation.py <host_ip> <port>")
    if hp is None:
        return 1
    host_ip, port = hp
    c = L.FakeClient("move-rotation", host_ip, port)
    try:
        c.connect_and_ready(timeout=3.0, quiet=True)
    except L.ConnectError:
        print("FAIL: no HELLO_ACK")
        return 1
    except (RuntimeError, L.HandshakeRejected):
        print("FAIL: not READY")
        return 1
    print(f"[{time.time():.3f}] READY")

    def send_move(frame, angle):
        c.send_unreliable(L.build_move(frame, 5000.0, 40.0, 5000.0, angle=angle, speed=0.0, move_state=0, item_kind=-1))

    # Settle at angle=32000 for several snapshots (past the interp delay) before flipping.
    for i in range(6):
        send_move(frame=300 + i, angle=32000)
        L.pump_sleep(0.15)
    print(f"[{time.time():.3f}] settled at angle=32000, same position (5000,40,5000)")

    # Now flip to -32000: the short way is straight through the +32767/-32768 seam (~1536
    # units); the long way (a naive linear lerp of the raw values) would be 64000 units through 0.
    for i in range(6):
        send_move(frame=306 + i, angle=-32000)
        L.pump_sleep(0.15)
    print(f"[{time.time():.3f}] flipped to angle=-32000, same position -- watch the host's "
          f"[NET][REMOTE][DIAG] angle= values swing through +/-32768, not crawl through 0")
    L.pump_sleep(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
