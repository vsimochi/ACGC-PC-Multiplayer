#!/usr/bin/env python3
r"""test_dedicated_lifecycle_real.py - the dedicated server's player-facing lifecycle lines:
   "A client is connecting to the server..."  /  "<username> arrived into town"  /  "<username> left town".

LIFECYCLE SIGNALS USED (pc_net_game.c, pcnetgame_ded_life_*):
  connecting = the FIRST IDENTITY / IDENTITY_EXT / HANDSHAKE_BUSY message of a transport session that is still in the handshake (a town-fetch connection sends TOWN_FETCH_REQ instead, so it never counts);
  arrived    = the first accepted PLAYER_SCENE of a READY peer whose scene is the home town field (kind FIELD + IN_TOWN flag): the client's own game says it is standing in the town. READY (identity
               accepted) is NOT enough: a first-time character is READY only after Rover's scene, and walks through the station before the town;
  left town  = the peer slot's state is wiped (disconnect, drop, reject, slot reuse) while `arrived` is set; the name was captured at arrival.

TIER: REAL `AnimalCrossing.exe --host <port> --dedicated` process (disposable fixture pc\build64\bin_fixture4), redirected stdout (the [DEDICATED] lines the console prints) + FakeClients / TransportClients speaking
the wire protocol. Not a real client through the real ROM; the Rover wait is simulated with the same HANDSHAKE_BUSY keepalive the real client sends. The interactive-console presentation (friendly lines replace the
low-level ones, typed text preserved) is covered by test_dedicated_console_real.py (check L*).
Usage (isolated hidden desktop, AC_DISPLAY_NAME=samsung AC_MASTER_VOLUME=0, NET_SPIKE_GAME_BIN=<...>\bin_fixture4): python test_dedicated_lifecycle_real.py [--port 12990]"""
import argparse
import os
import re
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

BUSY = bytes([76, 0, 0, 0])
SCENE_FG, SCENE_SHOP0 = 7, 9


def scene_msg(scene_id, seq, in_town=True):
    flags = L.PC_NETGAME_SCENE_FLAG_IN_TOWN if in_town and scene_id == SCENE_FG else 0
    return struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, scene_id, flags, 0, 0, seq)


def name_of(slot):
    raw = L.resident_player(slot).player_name
    return "".join(chr(b) if 0x20 <= b <= 0x7E else "?" for b in raw).rstrip(" \x00")


def lines(h, rx):
    return [m.group(0) for m in re.finditer(rx, h.log_text().replace("\r\n", "\n"))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12990)
    args = ap.parse_args()
    results = []
    ck = lambda d, c, x="": L.check(d + ((" :: " + str(x)[:300]) if (x and not c) else ""), bool(c), results)  # noqa: E731
    port = args.port
    h = L.HostProcess(port=port, extra_args=["--dedicated"], log_path=os.path.join(HERE, "dedicated_lifecycle_host.log"), bin_dir=L.GAME_BIN_DIR, stdin_pipe=True, verbose=False,
                      new_group=True).start()
    clients = []

    def client(label, slot):
        c = L.FakeClient(label, "127.0.0.1", port, player=L.resident_player(slot))
        c.rec_resident_idx = slot
        clients.append(c)
        return c

    CONN = r"\[DEDICATED\] A client is connecting to the server\.\.\."
    ARR = r"\[DEDICATED\] ([^\n]+) arrived into town"
    LEFT = r"\[DEDICATED\] ([^\n]+) left town"
    try:
        ck("host listening", h.wait_listening(60.0))
        ck("host booted (status: observer + world + save ready)", h.boot_to_dedicated(timeout=120.0))
        names = {i: name_of(i) for i in range(4)}
        L.resolve_host_town("127.0.0.1", port)  # the harness's own town probe is a throw-away session (it sends an IDENTITY): do it once and count from here
        base = len(lines(h, CONN))
        ck("the fixture has four residents with distinct names: %s" % names, len(set(names.values())) == 4 and all(names.values()))

        # ---- 8. an auxiliary TOWN FETCH connection is not a player ----
        t = L.TransportClient("fetch", "127.0.0.1", port)
        clients.append(t)
        t.connect()
        info = L.town_fetch_start(t)
        got, probs = L.town_fetch_collect(t, info) if info is not None else (b"", ["no TOWN_INFO"])
        t.disconnect()
        L.pump_sleep(1.5)
        ck("L8 a town-fetch connection (info %s, %d bytes) printed no connecting / arrived / left line" % ("ok" if info else "NONE", len(got)),
           info is not None and not probs and len(lines(h, CONN)) == base and not lines(h, ARR) and not lines(h, LEFT), (probs, lines(h, CONN)))
        ck("L9a ...and its low-level lines are still in the log (redirected output keeps the diagnostics)", "[DEDICATED] peer " in h.log_text() and "connected (transport; awaiting identity)" in h.log_text())

        # ---- 1. connects, identifies, leaves before reaching the town: connecting only ----
        a = client("A", 0)
        a.connect_and_ready(quiet=True)
        L.pump_sleep(1.0)
        a.disconnect()
        L.pump_sleep(1.5)
        ck("L1 identity accepted but the player left before the town: exactly one 'connecting' line, no arrival, no departure", len(lines(h, CONN)) == base + 1 and not lines(h, ARR) and not lines(h, LEFT),
           (lines(h, CONN), lines(h, ARR), lines(h, LEFT)))

        # ---- 2/3/5. first-time character: long Rover wait (keepalives), identity late, then the town ----
        n_conn = len(lines(h, CONN))
        b = client("B", 1)
        b.connect(timeout=3.0)
        b.send_reliable(BUSY)
        L.pump_sleep(6.0)
        b.send_reliable(BUSY)
        L.pump_sleep(1.0)
        ck("L2a the Rover wait (two keepalives, no identity) produced ONE connecting line and no arrival", len(lines(h, CONN)) == n_conn + 1 and not lines(h, ARR), (lines(h, CONN), lines(h, ARR)))
        b.town_claimed = b.claimed_town()
        if b.resident_claim and b.guest is None:
            b.send_resident_ext(town=b.town_claimed)
        b.send_identity(town=b.town_claimed)
        m = b.wait_handshake_reply(10.0)
        ck("L2b the delayed identity was accepted (READY)", m is not None and m.msg_type == L.PC_NETGAME_MSG_IDENTITY_ACK, None if m is None else (m.msg_type, getattr(m, "game", None)))
        if m is None or m.msg_type != L.PC_NETGAME_MSG_IDENTITY_ACK:
            return L.summary_and_exit_code(results)
        b.post_ready_handshake(True)
        L.pump_sleep(1.0)
        ck("L2c READY alone is not an arrival (no 'arrived' line yet), and no second connecting line", not lines(h, ARR) and len(lines(h, CONN)) == n_conn + 1, lines(h, ARR))
        b.send_reliable(scene_msg(SCENE_SHOP0, 1))  # a non-town place first (the station / a shop is not the town field)
        L.pump_sleep(0.8)
        ck("L2d a non-town scene is no arrival either", not lines(h, ARR))
        b.send_reliable(scene_msg(SCENE_FG, 2))
        L.pump_sleep(1.2)
        arr = lines(h, ARR)
        ck("L3 entering the town field gives exactly one arrival with the player's real name '%s': %s" % (names[1], arr), len(arr) == 1 and re.search(ARR, arr[0]).group(1) == names[1])
        b.send_reliable(scene_msg(SCENE_FG, 3))
        b.send_reliable(scene_msg(SCENE_SHOP0, 4))
        b.send_reliable(scene_msg(SCENE_FG, 5))
        L.pump_sleep(1.2)
        ck("L5a repeated / returning town scenes do not repeat the arrival", len(lines(h, ARR)) == 1, lines(h, ARR))
        b.disconnect()
        L.pump_sleep(2.0)
        left = lines(h, LEFT)
        ck("L4 a town-present client disconnecting gives exactly one departure with the captured name: %s" % left, len(left) == 1 and re.search(LEFT, left[0]).group(1) == names[1])
        b.disconnect()
        L.pump_sleep(1.0)
        ck("L5b a second disconnect of the same session does not repeat the departure", len(lines(h, LEFT)) == 1)

        # ---- 7. two clients joining at the same time ----
        c = client("C", 2)
        d = client("D", 3)
        c.connect(timeout=3.0)
        d.connect(timeout=3.0)
        c.town_claimed = c.claimed_town()
        d.town_claimed = d.claimed_town()
        for x in (c, d):
            if x.resident_claim and x.guest is None:
                x.send_resident_ext(town=x.town_claimed)
        c.send_identity(town=c.town_claimed)
        d.send_identity(town=d.town_claimed)
        mc, md = c.wait_handshake_reply(10.0), d.wait_handshake_reply(10.0)
        ck("L7a both identities accepted", mc is not None and md is not None and mc.msg_type == md.msg_type == L.PC_NETGAME_MSG_IDENTITY_ACK)
        c.post_ready_handshake(True)
        d.post_ready_handshake(True)
        d.send_reliable(scene_msg(SCENE_FG, 1))  # D reaches the town first
        L.pump_sleep(0.6)
        c.send_reliable(scene_msg(SCENE_FG, 1))
        L.pump_sleep(1.2)
        arr = [re.search(ARR, x).group(1) for x in lines(h, ARR)]
        ck("L7b each concurrent player arrives once under their own name, in the order they reached the town: %s" % arr, arr == [names[1], names[3], names[2]], arr)
        c.disconnect()
        L.pump_sleep(1.5)
        left = [re.search(LEFT, x).group(1) for x in lines(h, LEFT)]
        ck("L7c C leaving gives C's name only (D is still present): %s" % left, left == [names[1], names[2]], left)
        d.disconnect()
        L.pump_sleep(1.5)
        left = [re.search(LEFT, x).group(1) for x in lines(h, LEFT)]
        ck("L7d then D leaving gives D's name: %s" % left, left == [names[1], names[2], names[3]], left)

        # ---- 6. a reused peer slot inherits nothing ----
        before_conn = len(lines(h, CONN))
        e = client("E", 0)
        e.connect_and_ready(quiet=True)
        L.pump_sleep(1.0)
        e.disconnect()
        L.pump_sleep(1.5)
        left = [re.search(LEFT, x).group(1) for x in lines(h, LEFT)]
        ck("L6a a client that reuses an earlier peer slot and never reaches the town gets a fresh 'connecting' line and NO departure (no inherited name / state)",
           len(lines(h, CONN)) == before_conn + 1 and left == [names[1], names[2], names[3]], (left, lines(h, CONN)))
        f = client("F", 2)
        f.connect_and_ready(quiet=True)
        f.send_reliable(scene_msg(SCENE_FG, 1))
        L.pump_sleep(1.2)
        f.disconnect()
        L.pump_sleep(1.5)
        arr = [re.search(ARR, x).group(1) for x in lines(h, ARR)]
        left = [re.search(LEFT, x).group(1) for x in lines(h, LEFT)]
        ck("L6b the next session of that slot arrives and leaves under ITS name: arrivals %s, departures %s" % (arr, left), arr[-1] == names[2] and left[-1] == names[2] and len(arr) == 4 and len(left) == 4)

        peers = re.findall(r"peer (\d+) connected \(transport; awaiting identity\)", h.log_text())
        ck("L6c the sessions above really reused peer slots (%d transport sessions on %d distinct slots: %s)" % (len(peers), len(set(peers)), peers), len(set(peers)) < len(peers), peers)

        # ---- 9. diagnostics ----
        txt = h.log_text()
        ck("L9b the low-level diagnostics are still there: peer connect / READY / disconnect detail lines", "connected (transport; awaiting identity)" in txt and re.search(r"peer \d+ READY: RESIDENT idx \d name=", txt) is not None
           and re.search(r"peer \d+ disconnected \(was READY\)", txt) is not None)
        sl = os.path.join(L.server_dir(L.GAME_BIN_DIR), "logs", "server.log")
        stext = open(sl, encoding="utf-8", errors="replace").read() if os.path.exists(sl) else ""
        ck("L9d servers/default/logs/server.log has the connecting / arrived / left lines AND the low-level lines", "A client is connecting to the server..." in stext and ("%s arrived into town" % names[2]) in stext
           and ("%s left town" % names[2]) in stext and "connected (transport; awaiting identity)" in stext)
    finally:
        for cl in clients:
            try:
                if cl.state in (cl.STATE_CONNECTED, cl.STATE_PENDING):
                    cl.disconnect()
            except Exception:  # noqa: BLE001
                pass
        try:
            h.send_line("stop")
            h.wait_exit(30) if hasattr(h, "wait_exit") else L.pump_sleep(5)
        except Exception:  # noqa: BLE001
            pass
        h.stop()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
