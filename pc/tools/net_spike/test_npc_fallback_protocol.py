#!/usr/bin/env python3
"""test_npc_fallback_protocol.py - villager authority range and stale fallback: the HOST side (relay lease, talk hold cap, console `npc`, server.log).

TIER: PROTOCOL TESTED against a REAL `AnimalCrossing.exe --host <port> --dedicated` (hidden observer, stdin console) driven by scripted FakeClients,
on a DISPOSABLE fixture pc\\build64\\bin_fixture4_npc_proto (a fresh copy of bin_fixture4 with the build64_w3 exe). The client side (local AI, hysteresis)
is covered natively by npc_authority_selftest.c and in the real game by test_npc_fallback_real.py.
  N1 console `npc` lists every villager: host actor (near the observer) / no actor (elsewhere), position + acre
  R1 a client's NPC_PROXY_MOVE for a villager the host does NOT simulate is relayed to the OTHER client as NPC_MOVE (same pose), never echoed to the sender
  R2 one relay owner: a second client's proxy for the same villager is dropped while the owner is active
  R3 the owner goes silent: the host releases the relay (logged) and the second client becomes the owner
  R4 a villager the host DOES simulate: a client's proxy is never relayed (the host stream wins)
  R5 validation: a wrong npc_id / a sender more than two acres away is rejected (logged)
  C1 talk hold cap (AC_MP_NPC_TALK_CAP_MS=6000): BEGIN keepalives cannot hold a villager past the cap; the lease table frees it; further keepalives are refused
     until END; after END a new conversation holds again
  C2 console `npc free <slot>` releases a hold now (and the keepalive cannot re-take it); `npc` shows the hold while it is active
  L1 the relay / hold diagnostics reach servers/<id>/logs/server.log
Usage: python test_npc_fallback_protocol.py [--port 28410]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_npc_proto"
EXE = os.path.abspath(os.path.join(HERE, "..", "..", "build64_w3", "bin", "AnimalCrossing.exe"))
BIN = os.path.join(T.BUILD64, FIXTURE)
os.environ["NET_SPIKE_GAME_BIN"] = BIN

import net_spike_lib as L  # noqa: E402

MSG_NPC_MOVE = 26
MSG_NPC_PROXY = 99
MSG_NPC_TALK = L.PC_NETGAME_MSG_NPC_TALK
MSG_NPC_LEASE = 70
NPC_MOVE_FMT = "<BBBBHHIfffhh"
assert struct.calcsize(NPC_MOVE_FMT) == 28
LEASE_FMT = "<BBHI16B"
NPC_TALK_FMT = "<BBBBHH"
SCENE_FMT = "<BBBBHHI"
SCENE_FG = 7
NONE = 0xFF
NPC_RX = re.compile(r"slot\s+(\d+) npc 0x([0-9A-F]{4}) (host actor|relay peer \d+|no actor(?: \(never\))?)\s+[^;]*; [^;]*; [^;]*; pos \((-?\d+), (-?\d+)\) acre \((-?\d+),(-?\d+)\)")


def proxy(slot, npc_id, frame, x, y, z, angle=0, action=1, mtype=MSG_NPC_PROXY):
    return struct.pack(NPC_MOVE_FMT, mtype, slot, action, 0, npc_id, 0, frame, x, y, z, angle, 0)


def talk(slot, npc_id, begin, seq):
    return struct.pack(NPC_TALK_FMT, MSG_NPC_TALK, slot & 0xFF, 1 if begin else 0, 0, npc_id & 0xFFFF, seq & 0xFFFF)


def scene_fg(c, seq):
    c.send_reliable(struct.pack(SCENE_FMT, L.PC_NETGAME_MSG_PLAYER_SCENE, 0, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 0, 0, seq))


def npc_moves(c, slot, since=0.0):
    out = []
    for m in c.inbox.peek_all(lambda m: len(m.payload) == 28 and m.payload[0] == MSG_NPC_MOVE):
        t = struct.unpack(NPC_MOVE_FMT, m.payload)
        if t[1] == slot and getattr(m, "t", time.time()) >= since:
            out.append((t[6], t[7], t[8], t[9]))
    return out


def lease_owner(c, slot, timeout=3.0):
    L.pump_sleep(0.8)  # the table is sent on change: let the newest one arrive, then read the highest seq
    end = time.time() + timeout
    best = None
    while True:
        for m in c.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and len(m.payload) == 24 and m.payload[0] == MSG_NPC_LEASE):
            t = struct.unpack(LEASE_FMT, m.payload)
            if best is None or t[3] > best[0]:
                best = (t[3], list(t[4:]))
        if best is not None or time.time() > end:
            return best[1][slot] if best else None
        L.pump_sleep(0.1)


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


def villagers(h):
    out = say(h, "npc", settle=1.2)
    return out, [(int(m.group(1)), int(m.group(2), 16), m.group(3), int(m.group(4)), int(m.group(5)), int(m.group(6)), int(m.group(7))) for m in NPC_RX.finditer(out)]


def run(port, results):
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    h = L.HostProcess(port=port, extra_args=["--dedicated"], log_path=os.path.join(HERE, "npc_fallback_proto_host.log"), bin_dir=BIN, stdin_pipe=True,
                      verbose=True, new_group=True, env={"AC_MP_NPC_TALK_CAP_MS": "6000"}).start()
    orig = h.log_text
    h.log_text = lambda: orig().replace("\r\n", "\n")
    clients = []
    try:
        if h.wait_for_log(r"Use this town", 12.0) is not None:
            h.send_line("1")
        ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=150.0)
        ck("dedicated host ready", ok)
        if not ok:
            return
        L.resolve_host_town("127.0.0.1", port)
        a = L.FakeClient("A", "127.0.0.1", port, player=L.resident_player(1))
        a.connect_and_ready(quiet=True)
        b = L.FakeClient("B", "127.0.0.1", port, player=L.resident_player(2))
        b.connect_and_ready(quiet=True)
        clients += [a, b]
        scene_fg(a, 1)
        scene_fg(b, 1)
        L.pump_sleep(3.0)
        out, vs = villagers(h)
        L.info("npc console:\n" + out.strip())
        far = [v for v in vs if v[2].startswith("no actor")]
        near = [v for v in vs if v[2] == "host actor"]
        ck("N1 `npc` lists the villagers (%d), some simulated by the host near the observer (%d) and some not (%d)" % (len(vs), len(near), len(far)),
           len(vs) >= 3 and far and "NPC: villager authority" in out)
        if not far:
            return
        s, nid, _k, x0, z0, _bx, _bz = far[0]
        y0 = 0.0
        # R1 -- A is near the villager, sends its pose; B must receive it as NPC_MOVE
        for i in range(5):
            a.send_move(100 + i, float(x0) + 30.0, y0, float(z0))
            L.pump_sleep(0.05)
        m0 = len(h.log_text())
        t0 = time.time()
        for i in range(25):
            a.send_unreliable(proxy(s, nid, 1000 + i, float(x0) + i, y0, float(z0) + 0.5 * i))
            L.pump_sleep(0.1)
        L.pump_sleep(0.5)
        mine = lambda p: any(abs(p[1] - (x0 + i)) < 0.01 and abs(p[3] - (z0 + 0.5 * i)) < 0.01 for i in range(25))  # noqa: E731
        gb = [p for p in npc_moves(b, s) if mine(p)]
        ga = [p for p in npc_moves(a, s) if mine(p)]
        ck("R1 B received A's villager pose as NPC_MOVE (%d samples)" % len(gb), len(gb) >= 10)
        ck("R1 A never got its own relay back (%d)" % len(ga), len(ga) == 0)
        ck("R1 the host logged the relay start", h.wait_for_log(r"\[NET\]\[NPC\] host: slot %d npc 0x%04X not simulated by the host -> relaying peer %d" % (s, nid, a.assigned_peer_id), 3.0, since_offset=m0))
        # R2 -- B tries to relay the same villager with other coordinates while A is active
        for i in range(5):
            b.send_move(100 + i, float(x0) - 30.0, y0, float(z0))
        for i in range(15):
            a.send_unreliable(proxy(s, nid, 2000 + i, float(x0) + 1.0, y0, float(z0)))
            b.send_unreliable(proxy(s, nid, 2000 + i, float(x0) - 77.0, y0, float(z0) - 77.0))
            L.pump_sleep(0.1)
        L.pump_sleep(0.4)
        ab = [p for p in npc_moves(a, s) if abs(p[1] - (x0 - 77.0)) < 0.01]
        ck("R2 exclusive: B's proxy is dropped while A owns the relay (A got %d of B's samples)" % len(ab), len(ab) == 0)
        # R3 -- A goes silent: released, then B is the owner
        m1 = len(h.log_text())
        rel = h.wait_for_log(r"\[NET\]\[NPC\] host: slot %d relay from peer %d ended \(silent\)" % (s, a.assigned_peer_id), 4.0, since_offset=m1)
        ck("R3 the silent owner's relay was released by the host", rel is not None)
        for i in range(15):
            b.send_unreliable(proxy(s, nid, 3000 + i, float(x0) - 55.0, y0, float(z0) - 55.0))
            L.pump_sleep(0.1)
        L.pump_sleep(0.4)
        ab = [p for p in npc_moves(a, s) if abs(p[1] - (x0 - 55.0)) < 0.01]
        ck("R3 B became the relay owner: A receives B's villager (%d samples)" % len(ab), len(ab) >= 8)
        # R4 -- a villager the host simulates is never relayed
        if near:
            hs, hn, _k, hx, hz, _hbx, _hbz = near[0]
            for i in range(5):
                a.send_move(200 + i, float(hx) + 20.0, y0, float(hz))
            for i in range(15):
                a.send_unreliable(proxy(hs, hn, 4000 + i, float(hx) + 333.0, y0, float(hz) + 333.0))
                L.pump_sleep(0.1)
            L.pump_sleep(0.4)
            bh = npc_moves(b, hs)
            fake = [p for p in bh if abs(p[1] - (hx + 333.0)) < 0.01]
            ck("R4 a host-simulated villager: B gets the host stream (%d samples) and never A's proxy (%d)" % (len(bh), len(fake)), len(bh) >= 5 and len(fake) == 0)
        else:
            L.info("R4 skipped: no villager near the observer")
        # R5 -- validation
        m2 = len(h.log_text())
        a.send_unreliable(proxy(s, (nid + 1) & 0xFFFF, 5000, float(x0), y0, float(z0)))
        r1 = h.wait_for_log(r"relay REJECT peer=%d slot=%d npc=0x%04X: npc_id does not match" % (a.assigned_peer_id, s, (nid + 1) & 0xFFFF), 3.0, since_offset=m2)
        a.send_move(300, float(x0) + 2000.0, y0, float(z0) + 1500.0)
        L.pump_sleep(0.4)
        a.send_unreliable(proxy(s, nid, 5001, float(x0), y0, float(z0)))
        r2 = h.wait_for_log(r"relay REJECT peer=%d slot=%d npc=0x%04X: the villager is more than two acres away" % (a.assigned_peer_id, s, nid), 3.0, since_offset=m2)
        ck("R5 a wrong npc_id is rejected", r1 is not None)
        ck("R5 a sender more than two acres away is rejected", r2 is not None)
        # C1 -- talk hold cap (6 s here)
        ts, tn = (near[0][0], near[0][1]) if near else (s, nid)
        a.send_move(400, float(near[0][3] if near else x0), y0, float(near[0][4] if near else z0))
        m3 = len(h.log_text())
        seq = 10
        a.send_reliable(talk(ts, tn, True, seq))
        held = h.wait_for_log(r"\[NPC\]\[TALKNET\] HOLD slot=%d npc=0x%04X" % (ts, tn), 4.0, since_offset=m3)
        ck("C1 a client BEGIN holds the villager", held is not None)
        ck("C1 the lease table names A", lease_owner(b, ts) == a.assigned_peer_id)
        out = say(h, "npc")
        ck("C2 `npc` shows the hold (HELD by peers)", re.search(r"slot\s+%d npc 0x%04X .*HELD by peers" % (ts, tn), out) is not None)
        capped = None
        t_end = time.time() + 12.0
        while time.time() < t_end and capped is None:
            seq += 1
            a.send_reliable(talk(ts, tn, True, seq))  # the keepalive of a stuck talk state
            capped = h.wait_for_log(r"\[NPC\]\[TALKNET\] RELEASE slot=%d npc=0x%04X peers=0x[0-9A-F]+: hold cap 6000 ms reached" % (ts, tn), 2.0, since_offset=m3)
        ck("C1 the hold was released by the cap although keepalives kept coming", capped is not None)
        L.pump_sleep(1.0)
        ck("C1 the lease table frees the villager after the cap", lease_owner(b, ts) == NONE)
        m4 = len(h.log_text())
        seq += 1
        a.send_reliable(talk(ts, tn, True, seq))
        rj = h.wait_for_log(r"REJECT peer=%d slot=%d npc=0x%04X begin: this peer's hold was capped / freed: refused until its END" % (a.assigned_peer_id, ts, tn), 3.0, since_offset=m4)
        ck("C1 a keepalive after the cap is refused", rj is not None)
        seq += 1
        a.send_reliable(talk(ts, tn, False, seq))
        en = h.wait_for_log(r"\[NPC\]\[TALKNET\] END peer=%d slot=%d npc=0x%04X after a capped / freed hold" % (a.assigned_peer_id, ts, tn), 3.0, since_offset=m4)
        seq += 1
        a.send_reliable(talk(ts, tn, True, seq))
        again = h.wait_for_log(r"\[NPC\]\[TALKNET\] HOLD slot=%d npc=0x%04X" % (ts, tn), 3.0, since_offset=m4)
        ck("C1 after END a new conversation holds again", en is not None and again is not None)
        # C2 -- operator release
        out = say(h, "npc free %d" % ts)
        ck("C2 `npc free` releases the hold", "NPC FREE: slot %d npc 0x%04X: conversation hold released" % (ts, tn) in out)
        L.pump_sleep(0.8)
        ck("C2 the lease table frees the villager", lease_owner(b, ts) == NONE)
        m5 = len(h.log_text())
        seq += 1
        a.send_reliable(talk(ts, tn, True, seq))
        ck("C2 the freed holder's keepalive cannot re-take it",
           h.wait_for_log(r"REJECT peer=%d slot=%d .*refused until its END" % (a.assigned_peer_id, ts), 3.0, since_offset=m5) is not None)
        out = say(h, "npc free")
        ck("C2 `npc free` without a slot prints the usage", "usage: npc free <slot>" in out)
        out = say(h, "help")
        ck("C2 help documents `npc`", "  npc | npc free <slot>" in out)
        # L1 -- server.log
        sl = ""
        for root, _d, files in os.walk(os.path.join(BIN, "servers")):
            for f in files:
                if f == "server.log":
                    sl += open(os.path.join(root, f), "rb").read().decode("utf-8", "replace")
        ck("L1 server.log carries the relay and hold lines", "relaying peer" in sl and "hold cap 6000 ms reached" in sl and "freed by the operator" in sl)
        ck("host alive throughout", h.alive())
    finally:
        for c in clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            h.send_line("stop")
            if h.wait_exit(40.0) is None:
                h.stop()
        except Exception:  # noqa: BLE001
            h.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=28410)
    args = ap.parse_args()
    if not (28000 <= args.port <= 28999):
        print("REFUSING: ports 28000-28999 only", file=sys.stderr)
        return 2
    T.make_fixture(FIXTURE)
    shutil.copy2(EXE, os.path.join(BIN, "AnimalCrossing.exe"))
    results = []
    with L.CloneSaveGuard(BIN):
        run(args.port, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
