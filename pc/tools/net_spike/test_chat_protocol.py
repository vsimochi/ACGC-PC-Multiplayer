#!/usr/bin/env python3
"""test_chat_protocol.py - MULTIPLAYER TEXT CHAT (worker CH, messages 100 CHAT_SEND / 101 CHAT_MSG) against a REAL `AnimalCrossing.exe --host <port> --dedicated`
process with a stdin pipe (the real console) and scripted FakeClients A, B (town field) and C (post office). Run on a DISPOSABLE fixture copy of bin_fixture4
(NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_ch_<name>>; its save AND servers dirs are restored). Ports 30500-30999 only.

  P0 CAPS   every READY client receives one CHAT_MSG CAPS+ON (flags 0x06, len 0, origin 0xFF) from the host
  P1 SCOPE  A (town) says "Hello there!": A gets its echo (origin = A's wire id, A's bound name), B (town) one identical copy, C (post office) nothing;
            the host logs "[CHAT] <A>: Hello there!" and "-> 1 peer(s) + echo"
  P2        C enters the town field: A's next line reaches B and C
  P3 RATE   a burst of 5 lines: 3 relayed, 2 dropped ("rate limit" logged), A gets ONE "too fast" NOTICE (no bubble kind)
  P4 BAD    len 0 / len 61 / a control code / a non-zero tail / 67 and 69 bytes / a client-originated CHAT_MSG: nothing relayed, drops logged, A stays connected
  P5 SANI   "  a    b  " arrives as "a b"; Latin-1 font codes (e acute 124) pass unchanged
  P6 MUTE   `mute <A>` -> servers/default/mutes.ini holds `mute = resident <40 hex> <name>`; A is told; A's line reaches nobody (+ a "muted" NOTICE); `mutes`
            lists it; `mute` again = already; `unmute <A>` -> A's next line reaches B again; `mute Nobodyzz` refused
  P7 SAY    `say Server says hi` reaches A, B and C (flags SERVER, origin 0xFF), logged "[CHAT] [server]: Server says hi"
  P8 SWITCH `chat off` -> every client gets CAPS without ON; A's line is dropped with a NOTICE; `chat on` -> CAPS ON again; `chat` prints the counters;
            `help` lists the chat commands; the server still answers `status`
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402

IP = "127.0.0.1"
SEND, MSG = 100, 101
SEND_FMT, MSG_FMT = "<BBH64s", "<BBBBHBB8s64s"
assert struct.calcsize(SEND_FMT) == 68 and struct.calcsize(MSG_FMT) == 80
F_SERVER, F_CAPS, F_ON, F_NOTICE = 1, 2, 4, 8
SCENE_FG, SCENE_POST_OFFICE = 7, 14


def say(h, cmd, settle=1.0, timeout=8.0):
    off = len(h.log_text())
    assert h.send_line(cmd)
    end = time.monotonic() + timeout
    last, last_t = off, time.monotonic()
    while time.monotonic() < end:
        L.pump_sleep(0.2)
        n = len(h.log_text())
        if n != last:
            last, last_t = n, time.monotonic()
        elif time.monotonic() - last_t >= settle and n > off:
            break
    return h.log_text()[off:].replace("\r\n", "\n")


def chats(c, since=None):
    out = []
    for m in c.inbox.peek_all(lambda m: len(m.payload) == 80 and m.payload[0] == MSG, since=since):
        t, origin, flags, ln, seq, scene, rsv, name, text = struct.unpack(MSG_FMT, m.payload)
        out.append({"origin": origin, "flags": flags, "len": ln, "seq": seq, "scene": scene, "rsv": rsv, "name": name, "text": text[:ln], "tail": text[ln:]})
    return out


def lines(c, since=None):
    return [x for x in chats(c, since) if not (x["flags"] & (F_CAPS | F_NOTICE | F_SERVER))]


def notices(c, since=None):
    return [x for x in chats(c, since) if x["flags"] & F_NOTICE]


def send(c, text, seq=1, length=None, tail=b"", raw=None):
    if raw is not None:
        c.send_reliable(raw)
        return
    body = text + tail
    c.send_reliable(struct.pack(SEND_FMT, SEND, len(text) if length is None else length, seq, body.ljust(64, b"\x00")[:64]))


def announce(c, scene, flags, seq):
    c.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, scene, flags, 0, 0, seq))


def wait(c, pred, t):
    return c.hub.wait_until(pred, t)


class MiniRun:
    def __init__(self, port):
        self.ip, self.port = IP, port
        self.fresh_rid = L.make_request_id_counter(7000)
        self.clients = []


def resident(run, label, slot):
    c = L.FakeClient(label, IP, run.port, player=L.resident_player(slot))
    c.rec_resident_idx = slot
    run.clients.append(c)
    c.connect_and_ready(quiet=True)
    c.wait_record_synced(30.0)
    return c


def phases(h, ck, run, names, mutes_path):
    a = resident(run, "A", 1)
    b = resident(run, "B", 2)
    c = resident(run, "C", 3)
    # P0
    for cl in (a, b, c):
        wait(cl, lambda cl=cl: any(x["flags"] & F_CAPS for x in chats(cl)), 6.0)
    caps = [[x for x in chats(cl) if x["flags"] & F_CAPS] for cl in (a, b, c)]
    ck("P0 every READY client got exactly one CAPS+ON (flags 0x06, len 0, origin 0xFF): %s" % [[(x["flags"], x["len"], x["origin"]) for x in k] for k in caps],
       all(len(k) == 1 and k[0]["flags"] == (F_CAPS | F_ON) and k[0]["len"] == 0 and k[0]["origin"] == 0xFF for k in caps))
    announce(a, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 1)
    announce(b, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 1)
    announce(c, SCENE_POST_OFFICE, 0, 1)
    L.pump_sleep(1.0)

    # P1
    ma, mb, mc = a.inbox.mark(), b.inbox.mark(), c.inbox.mark()
    hoff = len(h.log_text())
    send(a, b"Hello there!", seq=1)
    wait(b, lambda: lines(b, mb) and lines(a, ma), 4.0)
    L.pump_sleep(0.8)
    la, lb, lc = lines(a, ma), lines(b, mb), lines(c, mc)
    a_id = la[0]["origin"] if la else None
    ck("P1 A got its echo (%s)" % la, len(la) == 1 and la[0]["text"] == b"Hello there!" and la[0]["origin"] not in (0xFF, 8))
    nm_a = names[1].encode("latin-1")
    ck("P1 B got exactly one identical copy: origin %s = A's id, A's bound name, scene 7, seq 1, zero tail, rsv 0 (%s)" % (a_id, lb),
       len(lb) == 1 and lb[0]["origin"] == a_id and lb[0]["text"] == b"Hello there!" and lb[0]["name"].rstrip(b" \x00") == nm_a and lb[0]["scene"] == SCENE_FG and
       lb[0]["seq"] == 1 and lb[0]["tail"] == b"\x00" * (64 - 12) and lb[0]["rsv"] == 0)
    ck("P1 C (post office) received nothing (%s)" % lc, not lc)
    lg = h.log_text()[hoff:]
    ck("P1 the host logged \"[CHAT] %s: Hello there!\" and the relay scope (1 peer + echo)" % names[1],
       ("[CHAT] %s: Hello there!" % names[1]) in lg and "-> 1 peer(s) + echo" in lg)
    if a_id is None:
        return

    # P2
    announce(c, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 2)
    L.pump_sleep(1.0)
    ma, mb, mc = a.inbox.mark(), b.inbox.mark(), c.inbox.mark()
    send(a, b"second line", seq=2)
    wait(c, lambda: lines(c, mc) and lines(b, mb), 4.0)
    L.pump_sleep(0.6)
    ck("P2 C entered the town: A's line reaches B and C once each (%d / %d) + A's echo (%d)" % (len(lines(b, mb)), len(lines(c, mc)), len(lines(a, ma))),
       len(lines(b, mb)) == 1 and len(lines(c, mc)) == 1 and len(lines(a, ma)) == 1 and lines(c, mc)[0]["origin"] == a_id)

    # P3
    L.pump_sleep(2.5)  # a full bucket again
    ma, mb = a.inbox.mark(), b.inbox.mark()
    hoff = len(h.log_text())
    for i in range(5):
        send(a, b"burst %d" % i, seq=10 + i)
    L.pump_sleep(2.0)
    lb, na = lines(b, mb), notices(a, ma)
    lg = h.log_text()[hoff:]
    ck("P3 burst of 5: B got the first 3 (%s), the host logged 2 rate drops (%d)" % ([x["text"] for x in lb], lg.count("dropped (rate limit)")),
       [x["text"] for x in lb] == [b"burst 0", b"burst 1", b"burst 2"] and lg.count("dropped (rate limit)") == 2)
    ck("P3 A got ONE 'too fast' NOTICE (origin 0xFF, flags NOTICE) (%s)" % [x["text"] for x in na],
       len(na) == 1 and na[0]["text"] == b"You are sending messages too fast." and na[0]["origin"] == 0xFF)

    # P4
    L.pump_sleep(2.5)
    ma, mb, mc = a.inbox.mark(), b.inbox.mark(), c.inbox.mark()
    hoff = len(h.log_text())
    bad = [struct.pack(SEND_FMT, SEND, 0, 20, b"\x00" * 64), struct.pack(SEND_FMT, SEND, 61, 21, b"x" * 64),
           struct.pack(SEND_FMT, SEND, 3, 22, b"a\x7fb".ljust(64, b"\x00")), struct.pack(SEND_FMT, SEND, 2, 23, b"okX".ljust(64, b"\x00")),
           struct.pack(SEND_FMT, SEND, 2, 24, b"ok".ljust(64, b"\x00"))[:67], struct.pack(SEND_FMT, SEND, 2, 25, b"ok".ljust(64, b"\x00")) + b"\x00",
           struct.pack(MSG_FMT, MSG, 8, 0, 2, 26, 7, 0, b"Forged  ", b"hi".ljust(64, b"\x00"))]
    for p in bad:
        send(a, None, raw=p)
    L.pump_sleep(1.5)
    lg = h.log_text()[hoff:]
    nd = lg.count("CHAT_SEND dropped (malformed") + lg.count("CHAT_SEND dropped (wrong size")
    ck("P4 the 6 malformed CHAT_SENDs are dropped and logged (%d); the forged CHAT_MSG is dropped silently" % nd, nd == 6)
    ck("P4 nobody received anything (%d / %d / %d)" % (len(lines(a, ma)), len(lines(b, mb)), len(lines(c, mc))),
       not lines(a, ma) and not lines(b, mb) and not lines(c, mc))
    mb = b.inbox.mark()
    send(a, b"still here", seq=27)
    ck("P4 A is still connected (its next line reaches B)", wait(b, lambda: lines(b, mb), 4.0))

    # P5
    L.pump_sleep(2.5)
    mb = b.inbox.mark()
    send(a, b"  a    b  ", seq=30)
    send(a, b"caf\x7c", seq=31)  # 0x7C is e acute (CHAR_ACUTE_e 124) in the game font
    wait(b, lambda: len(lines(b, mb)) >= 2, 4.0)
    lb = lines(b, mb)
    ck("P5 sanitized: \"  a    b  \" -> \"a b\"; the font code 124 (e acute) passes (%s)" % [x["text"] for x in lb],
       [x["text"] for x in lb] == [b"a b", b"caf\x7c"])

    # P6
    L.pump_sleep(2.5)
    ma = a.inbox.mark()
    out = say(h, "mute %s" % names[1])
    txt = open(mutes_path, encoding="utf-8", errors="replace").read() if os.path.isfile(mutes_path) else ""
    ck("P6 `mute %s`: muted, and mutes.ini holds the identity KEY line" % names[1], "is now muted" in out and re.search(r"^version = 1$", txt, re.M) is not None and
       re.search(r"^mute = resident [0-9a-f]{40} %s$" % re.escape(names[1]), txt, re.M) is not None)
    ck("P6 A is told it was muted", wait(a, lambda: any(x["text"] == b"You were muted by the server." for x in notices(a, ma)), 4.0))
    L.pump_sleep(2.2)  # the notice limiter (2 s)
    ma, mb, mc = a.inbox.mark(), b.inbox.mark(), c.inbox.mark()
    hoff = len(h.log_text())
    send(a, b"can you hear me", seq=40)
    wait(a, lambda: notices(a, ma), 4.0)
    L.pump_sleep(0.8)
    ck("P6 a muted line reaches nobody (A %d / B %d / C %d) and A gets the 'muted' NOTICE (%s)" % (len(lines(a, ma)), len(lines(b, mb)), len(lines(c, mc)),
                                                                                             [x["text"] for x in notices(a, ma)]),
       not lines(a, ma) and not lines(b, mb) and not lines(c, mc) and [x["text"] for x in notices(a, ma)] == [b"You are muted on this server."] and
       "dropped (muted)" in h.log_text()[hoff:])
    out = say(h, "mutes")
    ck("P6 `mutes` lists it", "1 muted player(s)" in out and names[1] in out)
    out = say(h, "mute %s" % names[1].lower())
    ck("P6 `mute` again (case-insensitive) = already muted", "was already muted" in out)
    out = say(h, "mute Nobodyzz")
    ck("P6 `mute <unknown>` is refused", "REFUSED" in out and "no player named" in out)
    out = say(h, "unmute %s" % names[1])
    ck("P6 `unmute`: no longer muted (and mutes.ini no longer lists it)", "is no longer muted" in out and
       re.search(r"^mute = ", open(mutes_path, encoding="utf-8", errors="replace").read(), re.M) is None)
    L.pump_sleep(1.0)
    mb = b.inbox.mark()
    send(a, b"back again", seq=41)
    ck("P6 after unmute A's line reaches B again", wait(b, lambda: any(x["text"] == b"back again" for x in lines(b, mb)), 4.0))

    # P7
    ma, mb, mc = a.inbox.mark(), b.inbox.mark(), c.inbox.mark()
    hoff = len(h.log_text())
    out = say(h, "say Server says hi")
    for cl, mk in ((a, ma), (b, mb), (c, mc)):
        wait(cl, lambda cl=cl, mk=mk: any(x["flags"] & F_SERVER for x in chats(cl, mk)), 4.0)
    sv = [[x for x in chats(cl, mk) if x["flags"] & F_SERVER] for cl, mk in ((a, ma), (b, mb), (c, mc))]
    ck("P7 `say`: A, B and C each got one SERVER line, origin 0xFF (%s)" % [[(x["origin"], x["text"]) for x in k] for k in sv],
       all(len(k) == 1 and k[0]["origin"] == 0xFF and k[0]["text"] == b"Server says hi" for k in sv) and "sent to 3 player(s)" in out)
    ck("P7 logged \"[CHAT] [server]: Server says hi\"", "[CHAT] [server]: Server says hi" in h.log_text()[hoff:])

    # P8
    ma, mb, mc = a.inbox.mark(), b.inbox.mark(), c.inbox.mark()
    out = say(h, "chat off")
    for cl, mk in ((a, ma), (b, mb), (c, mc)):
        wait(cl, lambda cl=cl, mk=mk: any(x["flags"] & F_CAPS for x in chats(cl, mk)), 4.0)
    co = [[x["flags"] for x in chats(cl, mk) if x["flags"] & F_CAPS] for cl, mk in ((a, ma), (b, mb), (c, mc))]
    ck("P8 `chat off`: every client gets CAPS without ON (%s); the console says OFF" % co, all(k == [F_CAPS] for k in co) and "chat: OFF" in out)
    L.pump_sleep(2.2)
    ma, mb = a.inbox.mark(), b.inbox.mark()
    send(a, b"anyone?", seq=50)
    wait(a, lambda: notices(a, ma), 4.0)
    L.pump_sleep(0.6)
    ck("P8 with chat off A's line is dropped (B %d) with a NOTICE (%s)" % (len(lines(b, mb)), [x["text"] for x in notices(a, ma)]),
       not lines(b, mb) and [x["text"] for x in notices(a, ma)] == [b"Chat is turned off on this server."])
    ma = a.inbox.mark()
    out = say(h, "chat on")
    ck("P8 `chat on`: CAPS+ON again", "chat: ON" in out and wait(a, lambda: any(x["flags"] == (F_CAPS | F_ON) for x in chats(a, ma)), 4.0))
    out = say(h, "chat")
    ck("P8 `chat` prints the state and the counters", re.search(r"chat: ON \(0 muted; messages relayed \d+, dropped: rate 2, muted 1, malformed \d+, off 1\)", out) is not None)
    out = say(h, "say")
    ck("P8 `say` without text prints the usage", "SAY usage" in out)
    out = say(h, "help")
    ck("P8 `help` lists say / mutes / chat", "say <text>" in out and "mutes | mute <player> | unmute <player>" in out and "chat [on|off]" in out)
    out = say(h, "status")
    ck("P8 the server still answers `status` and is running", "[DEDICATED] status" in out and h.proc.poll() is None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=30510)
    args = ap.parse_args()
    if not (30500 <= args.port <= 30999):
        print("ports 30500-30999 only", file=sys.stderr)
        return 2
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_ch_"):
        print("REFUSING: run on a pc\\build64\\bin_fixture4_ch_<name> copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    servers = os.path.join(L.GAME_BIN_DIR, "servers")
    snap = os.path.join(os.environ.get("TEMP", "."), "chat_proto_servers_%d" % os.getpid())
    if os.path.isdir(servers):
        shutil.copytree(servers, snap)
    names = {i: p.player_name.rstrip(b"\x00 ").decode("latin-1") for i, p, ex in L.read_test_save_residents() if ex}
    run = MiniRun(args.port)
    t0 = time.time()
    with L.CloneSaveGuard():
        h = None
        try:
            h = L.HostProcess(port=args.port, extra_args=["--dedicated"], log_path=os.path.join(HERE, "chat_protocol_host.log"), bin_dir=L.GAME_BIN_DIR, stdin_pipe=True,
                              verbose=False, new_group=True).start()
            ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=150.0)
            ck("dedicated host booted: observer + world + save ready", ok)
            ck("the fixture has residents 1..3 (%s)" % names, all(k in names for k in (1, 2, 3)))
            if ok and all(k in names for k in (1, 2, 3)):
                L.resolve_host_town(IP, args.port)
                phases(h, ck, run, names, os.path.join(L.GAME_BIN_DIR, "servers", "default", "mutes.ini"))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ck("test aborted by an exception: %s" % e, False)
        finally:
            for c in run.clients:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            if h is not None:
                try:
                    h.send_line("stop")
                    h.wait_exit(45.0)
                except Exception:  # noqa: BLE001
                    pass
                if h.proc is not None and h.proc.poll() is None:
                    h.stop()
            if os.path.isdir(snap):
                shutil.rmtree(servers, ignore_errors=True)
                shutil.copytree(snap, servers)
                shutil.rmtree(snap, ignore_errors=True)
    L.discard_fixture_guest_mp(L.GAME_BIN_DIR)
    print("elapsed %.0f s" % (time.time() - t0))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
