#!/usr/bin/env python3
"""test_move_delay.py - Stage 3 validation: deliberately delayed/paused movement packets.

Same approach as test_move_sync.py, against a REAL, currently-running
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
  4. Go silent on MOVEMENT for 3 seconds -- the actor must HOLD at the last accepted position
     (checked via the host's own periodic [NET][REMOTE][DIAG] dump), never extrapolate
     indefinitely or drift.
  5. Resume sending a normal burst (frames 206..210) -- movement must recover normally.

Foundation-phase migration: handshake via net_spike_lib.FakeClient; MOVE bytes unchanged
(unreliable DATA). During the movement silence the library still ACKs the host's reliable traffic
and sends transport heartbeats -- that is transport-level liveness, invisible to the game layer's
movement logic, and is required under the new transport (unACKed reliable traffic is retransmitted
and eventually disconnects the peer). No MOVE is sent during the silence, exactly as before.

Usage: python3 test_move_delay.py <host_ip> <port>
"""
import sys
import time

import net_spike_lib as L


def build_move(frame, x, y, z, angle=0, speed=1.5, move_state=1, item_kind=-1):
    return L.build_move(frame, x, y, z, angle=angle, speed=speed, move_state=move_state, item_kind=item_kind)


def main():
    hp = L.parse_host_port(sys.argv, "usage: test_move_delay.py <host_ip> <port>")
    if hp is None:
        return 1
    host_ip, port = hp

    c = L.FakeClient("move-delay", host_ip, port)
    try:
        c.connect_and_ready(timeout=3.0, quiet=True)
    except L.ConnectError:
        print("FAIL: expected HELLO_ACK, got nothing")
        return 1
    except (RuntimeError, L.HandshakeRejected) as e:
        print(f"FAIL: no IDENTITY_ACK -- not READY ({e})")
        return 1
    print(f"[{time.time():.3f}] READY")

    def send_move(**kw):
        c.send_unreliable(build_move(**kw))

    for i in range(5):
        send_move(frame=200 + i, x=3000.0 + i * 10.0, y=40.0, z=4000.0)
        L.pump_sleep(0.05)
    print(f"[{time.time():.3f}] sent normal burst frames 200..204")

    print(f"[{time.time():.3f}] sleeping 1.2s before sending a late-but-newer frame 205 ...")
    L.pump_sleep(1.2)
    send_move(frame=205, x=3050.0, y=40.0, z=4000.0)
    print(f"[{time.time():.3f}] sent late frame=205 (must be accepted; newest becomes 205)")
    L.pump_sleep(0.1)

    send_move(frame=204, x=9999.0, y=40.0, z=9999.0)
    print(f"[{time.time():.3f}] sent stale frame=204 with garbage position (must be rejected)")
    L.pump_sleep(0.1)

    # Kept at 3 s as before (originally chosen to stay under PCNET_TIMEOUT_MS when NOTHING was
    # sent; transport heartbeats now keep the link alive regardless, but the scenario is unchanged).
    print(f"[{time.time():.3f}] going silent for 3s -- actor must HOLD, not extrapolate/drift ...")
    L.pump_sleep(3.0)
    print(f"[{time.time():.3f}] silence over")

    for i in range(5):
        send_move(frame=206 + i, x=3050.0 + i * 10.0, y=40.0, z=4010.0)
        L.pump_sleep(0.05)
    print(f"[{time.time():.3f}] sent recovery burst frames 206..210")

    # Keep the connection alive so at least one more diagnostic dump on the host's side reflects
    # the recovered position before this script exits.
    L.pump_sleep(2.0)

    print("DONE -- cross-reference timestamps above against the host's own "
          "[NET][REMOTE][DIAG] log lines to confirm: no backward jump at frame 205, "
          "stale frame 204 rejected, position held steady during the silence, "
          "and movement resumed normally afterward.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
