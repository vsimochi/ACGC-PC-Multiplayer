#!/usr/bin/env python3
"""test_wildlife_bobber_order_real.py - a REAL host process and one FakeClient peer (protocol level; Samsung display, volume 1): B-8, a reordered BOBBER_STATE must not re-activate a bobber proxy.

BOBBER_STATE.seq (the former always-0 reserved field) orders one sender's messages. The peer sends: active (seq 10), the reliable 'gone' (seq 11), then a STALE unreliable 'active' (seq 9, as if
the network had delivered it late). The host must keep the proxy inactive and release it (a proxy re-activated by the stale packet would stay active for good: nothing ever sends another
'gone'). Then: a new cast (seq 12) is applied, and a legacy sender that leaves seq 0 is still served. Evidence: the host's [PROXY-DIAG] lines (AC_TEST_PROXY_DIAG=1).
Usage: python test_wildlife_bobber_order_real.py [--port 12996]"""
import argparse
import os
import re
import struct
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_wildlife_proxy_capacity_real as C  # noqa: E402
import net_spike_lib as L  # noqa: E402

BOBBER_STATE = 74


def payload(active, seq, x=2500.0, z=1500.0):
    return struct.pack("<BBBbbBBB6fhh", BOBBER_STATE, 1 if active else 0, 3 if active else 0, 1 if active else 0, 0, 1 if active else 0, 5 if active else 0, 0,
                       x, 0.0, z, x, 0.0, z, 0, seq)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12996)
    args = ap.parse_args()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env={"AC_TEST_PROXY_DIAG": "1"}, log_path=os.path.join(HERE, "border_host.log"),
                         bin_dir=C.W.BIN).start()
    f = None
    try:
        ok = host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        f = L.FakeClient("F1", "127.0.0.1", args.port)
        f.connect_and_ready()
        time.sleep(1.0)

        def n(rx, since):
            return len(re.findall(rx, host.log_text()[since:]))

        off = len(host.log_text())
        f.send_unreliable(payload(True, 10))
        time.sleep(0.5)
        check("a cast (seq 10) gets a proxy", n(r"PROXY-DIAG\] alloc slot", off) == 1)
        f.send_reliable(payload(False, 11))
        time.sleep(0.3)
        f.send_unreliable(payload(True, 9))  # stale: older than the 'gone' that was already applied
        time.sleep(3.0)
        check("the stale 'active' (seq 9) did not bring the proxy back: it was released", n(r"PROXY-DIAG\] release slot", off) == 1)
        diag = re.findall(r"PROXY-DIAG\] slot \d+ peer \d+ active=(\d)", host.log_text()[off:])
        check("... and the proxy never showed up active after the 'gone' (active flags seen %s)" % diag[-3:], not diag or diag[-1] == "0")

        off = len(host.log_text())
        f.send_unreliable(payload(True, 12))
        time.sleep(0.6)
        check("a new cast (seq 12) is applied", n(r"PROXY-DIAG\] alloc slot", off) == 1)
        f.send_reliable(payload(False, 13))
        time.sleep(3.0)
        check("... and released after its 'gone'", n(r"PROXY-DIAG\] release slot", off) == 1)

        off = len(host.log_text())
        f.send_unreliable(payload(True, 0))
        time.sleep(0.6)
        check("a legacy sender that leaves seq 0 is still served", n(r"PROXY-DIAG\] alloc slot", off) == 1)
        f.send_reliable(payload(False, 0))
        time.sleep(3.0)
        check("... and released", n(r"PROXY-DIAG\] release slot", off) == 1)

        # control: the same reordering from a sender that does NOT number its messages is applied (the proxy comes back and is never released) -- this is the hazard seq closes, and it shows the
        # checks above can fail
        off = len(host.log_text())
        f.send_unreliable(payload(True, 20))
        time.sleep(0.5)
        f.send_reliable(payload(False, 21))
        time.sleep(0.3)
        f.send_unreliable(payload(True, 0))
        time.sleep(3.0)
        check("control: an unnumbered stale 'active' after the 'gone' re-activates the proxy (no release: %d)" % n(r"PROXY-DIAG\] release slot", off), n(r"PROXY-DIAG\] release slot", off) == 0)
    finally:
        try:
            if f is not None:
                f.disconnect()
        except Exception:  # noqa: BLE001
            pass
        host.stop()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
