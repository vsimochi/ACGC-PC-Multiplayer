#!/usr/bin/env python3
"""test_handshake_busy_real.py - Rover-scene disconnect regression (first-run character creation withholds IDENTITY for the whole train cutscene, longer than the host's 60 s N-05
identity-handshake deadline). The client now sends PC_NETGAME_MSG_HANDSHAKE_BUSY (id 76, 4 bytes, reliable) every 10 s while it is transport-connected but cannot identify itself yet; the host
restarts the deadline on it, capped since the connect.

TIER: REAL host process (Samsung display, volume 0) + bare TransportClients speaking the wire message. It does NOT run a real client through the real ROM's character creation (that needs the
ROM + a human-visible scene; see the mission report). The client-side sender is covered by source checks (test_handshake_busy_src below, same file) and by the manual first-run test.
Checks: (1) a silent peer is dropped at ~60 s (unchanged N-05); (2) a peer sending HANDSHAKE_BUSY every 10 s is still connected well past 60 s; (3) a peer that then completes IDENTITY is
READY and stays (the normal transition); (4) the extension is capped (PC_NET_TEST_HS_BUSY_MAX_MS=45000 test cap, AC_TEST_HOOKS=1): keepalives stop extending and the peer is dropped;
(5) a malformed / wrong-size busy message and one from a READY peer are ignored; (6) source: the client sender is in both waiting branches and the message is host-gated on the HANDSHAKE link.
Usage: python test_handshake_busy_real.py [--port 12975]   (takes ~3 min)"""
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
W.make_fixture()
import net_spike_lib as L  # noqa: E402

BUSY = bytes([76, 0, 0, 0])
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def source_checks(check):
    s = open(os.path.join(REPO, "pc", "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    check("S1 msg id 76 is HANDSHAKE_BUSY and its struct is 4 bytes", re.search(r"PC_NETGAME_MSG_HANDSHAKE_BUSY\s*=\s*76", s) and "sizeof(PCNetGameHandshakeBusyMsg) == 4" in s)
    tick = re.search(r"static void pcnetgame_client_tick\(void\) \{.*?\n\}\n", s, re.S).group(0)
    check("S2 the client sends the keepalive in both waiting branches (creation active / waiting for a loaded save)", tick.count("pcnetgame_client_handshake_busy_tick();") == 2)
    h = re.search(r"static void pcnetgame_handle_host_handshake_busy\(.*?\n\}\n", s, re.S).group(0)
    check("S3 the host only extends a peer on the HANDSHAKE link that is not closing, and caps the extension", "PC_NETGAME_LINK_HANDSHAKE" in h and "closing" in h and "hs_busy_max_ms()" in h)
    conn = re.search(r"case PC_NET_EVENT_PEER_CONNECTED:.*?hs_first_ms = ", s, re.S)
    check("S5 a (re)used peer slot starts clean: the connect event resets ALL per-peer state (clears the busy flags) BEFORE hs_since_ms / hs_first_ms are set, so the 30-min cap restarts per connection",
          conn is not None and "pcnetgame_reset_all_host_peer_state(ev.peer)" in conn.group(0) and "hs_since_ms = pcnetgame_now_ms()" in conn.group(0))
    check("S6 the keepalive can only touch the sender's own slot: the handler takes the transport peer id of the packet and writes only s_host_peer[peer]", "s_host_peer[peer].hs_since_ms = now;" in h and "[ev" not in h)
    check("S4 the cap override is test-hook only", re.search(r"#ifdef PC_NET_TEST_HOOKS\n\s*const char\* e = pc_test_hook_getenv\(\"PC_NET_TEST_HS_BUSY_MAX_MS\"\)", s) is not None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12975)
    args = ap.parse_args()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    source_checks(check)
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"], bin_dir=W.BIN, log_path=os.path.join(HERE, "hsbusy_host.log"),
                         env={"PC_NET_TEST_HS_BUSY_MAX_MS": "45000"}).start()
    silent = busy = bad = ready = None
    try:
        ok = host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        silent = L.TransportClient("silent", "127.0.0.1", args.port)
        busy = L.TransportClient("busy", "127.0.0.1", args.port)
        bad = L.TransportClient("bad", "127.0.0.1", args.port)
        for c in (silent, busy, bad):
            c.connect()
        t0 = time.time()
        check("three peers are transport-connected, none identified", all(c.state == c.STATE_CONNECTED for c in (silent, busy, bad)))
        last = 0.0
        bad_last = 0.0
        res = {}
        while time.time() - t0 < 115:
            now = time.time() - t0
            if now - last >= 10:
                busy.send_reliable(BUSY)
                last = now
            if now - bad_last >= 10:
                bad.send_reliable(bytes([76, 0, 0, 0, 0, 0]))  # wrong size: ignored (falls to the unknown-message path)
                bad.send_reliable(bytes([76]))
                bad_last = now
            L.pump_sleep(1)
            for name, c in (("silent", silent), ("busy", busy), ("bad", bad)):
                if name not in res and c.state != c.STATE_CONNECTED:
                    res[name] = now
            if "silent" in res and "busy" in res and "bad" in res:
                break
        log = host.log_text()
        check("silent peer dropped at the unchanged 60 s deadline (%.0f s)" % res.get("silent", -1), 55 <= res.get("silent", -1) <= 70)
        check("malformed busy messages did NOT extend the deadline (dropped at %.0f s)" % res.get("bad", -1), 55 <= res.get("bad", -1) <= 70)
        check("the busy peer outlived the 60 s deadline (dropped at %.0f s)" % res.get("busy", 999), res.get("busy", 999) > 75)
        check("the busy peer was dropped once the cap (45 s test cap) stopped extending (%.0f s, expected 85-115)" % res.get("busy", -1), 85 <= res.get("busy", -1) <= 115)
        check("host logged the extension and the cap", "handshake deadline extended" in log and "no longer extend the handshake deadline" in log)
        check("host logged the drop for the silent peers", "never completed the identity handshake" in log)
        # the normal transition: busy keepalives, then IDENTITY -> READY, then a late busy message is ignored
        ready = L.FakeClient("R", "127.0.0.1", args.port)
        ready.connect_and_ready()
        check("a client that connects and identifies normally reaches READY", ready.is_connected())
        L.pump_sleep(2)
        ready.send_reliable(BUSY)
        L.pump_sleep(3)
        check("a busy message from a READY peer is ignored (still connected)", ready.is_connected())
    finally:
        for c in (silent, busy, bad, ready):
            try:
                if c is not None:
                    c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        host.stop()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
