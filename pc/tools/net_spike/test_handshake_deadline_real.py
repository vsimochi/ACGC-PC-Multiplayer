#!/usr/bin/env python3
"""test_handshake_deadline_real.py - N-05: a peer that connects the TRANSPORT (and keeps it alive with heartbeats) but never sends its IDENTITY must not hold a host slot for ever.
A REAL host process (Samsung display; volume from the environment, 0 for the mission) and a bare TransportClient (connect + automatic heartbeats/ACKs, no IDENTITY). The host drops it after
PC_NETGAME_HANDSHAKE_DEADLINE_MS (60 s, counted only while the host world is ready); a normal FakeClient that joins afterwards is admitted (the slot is not leaked), and a FakeClient that joined
BEFORE and completed the handshake is NOT dropped by the deadline.
Usage: python test_handshake_deadline_real.py [--port 12970]   (takes ~80 s)"""
import argparse
import os
import re
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "0")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wplay_lib as W  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = W.BIN
W.make_fixture()  # before net_spike_lib is imported (its default FakeClient identities come from this copy's save)
import net_spike_lib as L  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12970)
    args = ap.parse_args()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"], bin_dir=W.BIN, log_path=os.path.join(HERE, "hsdeadline_host.log")).start()
    silent = ready = late = None
    try:
        ok = host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        ready = L.FakeClient("R", "127.0.0.1", args.port)
        ready.connect_and_ready()
        check("a normal client completes the handshake", ready.is_connected())
        silent = L.TransportClient("silent", "127.0.0.1", args.port)
        silent.connect()
        t0 = time.time()
        check("the silent peer is transport-connected (it never sends IDENTITY)", silent.state == silent.STATE_CONNECTED)
        L.pump_sleep(30)
        check("after 30 s the silent peer is still connected (the deadline is 60 s)", silent.state == silent.STATE_CONNECTED and "never completed the identity handshake" not in host.log_text())
        end = t0 + 90
        while time.time() < end and "never completed the identity handshake" not in host.log_text():
            L.pump_sleep(2)
        dropped = "never completed the identity handshake" in host.log_text()
        check("the host dropped the silent peer after its handshake deadline (%.0f s)" % (time.time() - t0), dropped and 55 <= time.time() - t0 <= 85)
        L.pump_sleep(3)
        check("the already-READY client was not touched by the deadline", ready.is_connected())
        late = L.FakeClient("L", "127.0.0.1", args.port)
        late.connect_and_ready()
        check("a client that joins afterwards is admitted (the slot is free again)", late.is_connected())
    finally:
        for c in (silent, ready, late):
            try:
                if c is not None:
                    c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        host.stop()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
