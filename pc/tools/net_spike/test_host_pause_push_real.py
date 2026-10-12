#!/usr/bin/env python3
r"""test_host_pause_push_real.py - sprint 2 worker 7: RUNTIME checks on ONE REAL host process (fixture bin_fixture4_w7, ports 16700+) driven by FakeClients A and B.

TIER: Runtime verified for what the host process does (host = real game, clients = protocol FakeClients; no GUI, no keystrokes).
Test hooks used (all TEST-ONLY, AC_TEST_HOOKS=1): the autopilot commands `pause 0|1` (the real pc_pause_menu_toggle), `ppos`, `walk` (pc_net_game.c) and the host villager-talk driver
PC_FORCE_TALK_VILLAGER (+ PC_FORCE_TALK_DELAY_FRAMES) in ac_npc_move.c_inc; diagnostics PC_NPC_PUSH_DIAG=1 / PC_NPC_TALKHOLD_DIAG=1.
  P1 puppet push: puppet A parked 10 units from a regular villager -> '[NPC][PUPPET_PUSH]' for that slot AND the villager's NPC_MOVE pose (seen by client B) moves away from the puppet
  P2 held villager: A holds a talk lease on that villager, puppet 10 units from it -> NO PUPPET_PUSH line for that slot and the NPC_MOVE pose does not move
  P3 passability: the HOST local player walks straight into the puppet's position (arrival radius 20) -> reaches it (a solid puppet would stop it ~38 away); control walk without puppet first
  Z1 host Esc pause with a READY peer: the real toggle runs, the world keeps ticking (NPC_MOVE frames keep advancing to B, villager poses change)
  Z2 while paused the host's own villager conversation (PC_FORCE_TALK_VILLAGER) does NOT start ('talk requested' absent for 6 s)
  Z3 after the unpause the same hook's conversation starts and ends normally (host dialogue), and a client talk lease taken during the pause is released on END ('[NPC][TALKHOLD] end')
Usage: python test_host_pause_push_real.py [--port 16700] [--no-fixture]   (take /tmp/ac_game_lock first)"""
import argparse
import math
import os
import re
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FX = "bin_fixture4_w7"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FX)
os.environ["AC_DISPLAY_NAME"] = "samsung"
os.environ["AC_MASTER_VOLUME"] = "1"
if __name__ == "__main__" and "--no-fixture" not in sys.argv:
    T.make_fixture(FX)
import net_spike_lib as L  # noqa: E402
import test_client_villager_talk as VT  # noqa: E402
import test_observer_protocol as OB  # noqa: E402

NPC_MOVE_FMT = "<BBBBHHIfffhh"
NPC_MOVE_SIZE = struct.calcsize(NPC_MOVE_FMT)
MSG_NPC_MOVE = 26
HOOK_SLOT = 1
AUTO_DONE = r"\[AUTO\] done %d walk (\w+)[^\r\n]*"


def is_npc_move(m):
    return m.channel == L.CH_UNRELIABLE and len(m.payload) == NPC_MOVE_SIZE and m.payload[0] == MSG_NPC_MOVE


def decode(m):
    t = struct.unpack(NPC_MOVE_FMT, m.payload)
    return {"slot": t[1], "npc": t[4], "frame": t[6], "x": t[7], "y": t[8], "z": t[9]}


def samples(c, since=None):
    return [decode(m) for m in c.inbox.peek_all(is_npc_move, since)]


def latest_by_slot(c, since=None):
    out = {}
    for s in samples(c, since):
        if s["slot"] not in out or s["frame"] >= out[s["slot"]]["frame"]:
            out[s["slot"]] = s
    return out


def stream(c, x, y, z, seconds):
    """the puppet stands at x,y,z: one MOVE claim every ~33 ms for `seconds`"""
    end = time.time() + seconds
    while time.time() < end:
        c.claim_position(x, y, z, 0)
        L.pump_sleep(0.033)


def wait_pumped(host, pat, timeout, since_offset=0, tick=None):
    """host.wait_for_log in short slices while the FakeClients keep pumping (an unpumped FakeClient times out and is dropped by the host)"""
    end = time.time() + timeout
    while time.time() < end:
        m = host.wait_for_log(pat, 0.3, since_offset=since_offset)
        if m is not None:
            return m
        L.pump_sleep(0.2)
        if tick is not None:
            tick()
    return None


class Auto:
    def __init__(self, host):
        self.host = host
        self.n = 0
        self.path = os.path.join(L.GAME_BIN_DIR, "ac_auto_host.txt")

    def send(self, line):
        self.n += 1
        with open(self.path, "w") as f:
            f.write("%d %s\n" % (self.n, line))
        return self.n

    def cmd(self, line, pat, timeout=30.0):
        cid = self.send(line)
        m = wait_pumped(self.host, r"\[AUTO\] " + (pat % cid), timeout)
        return None if m is None else m.group(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16700)
    ap.add_argument("--no-fixture", action="store_true")
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    env = {"AC_TEST_AUTOPILOT": "ac_auto_host.txt", "PC_NPC_PUSH_DIAG": "1", "PC_NPC_TALKHOLD_DIAG": "1", "PC_FORCE_TALK_VILLAGER": "1",
           "PC_FORCE_TALK_SLOT": str(HOOK_SLOT), "PC_FORCE_TALK_RETRY": "1", "PC_FORCE_TALK_DELAY_FRAMES": "6000"}
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0"], env=env, log_path=os.path.join(HERE, "w7_host.log"), bin_dir=L.GAME_BIN_DIR).start()
    clients = []
    try:
        if not host.wait_listening(60.0) or not host.boot_to_field(timeout=120.0, slot=0):
            check("host reached the field", False)
            return L.summary_and_exit_code(results)
        a = L.FakeClient("A", "127.0.0.1", args.port)
        a.connect_and_ready()
        b = L.FakeClient("B", "127.0.0.1", args.port)
        b.connect_and_ready()
        clients += [a, b]
        a.send_appearance(OB.appearance_payload(0, 2, 0x2500))  # a puppet only gets its visual (and so push / passability) once its appearance resolved
        b.send_appearance(OB.appearance_payload(0, 3, 0x2501))
        a.send_reliable(VT.scene_msg(VT.SCENE_FG, 1))
        b.send_reliable(VT.scene_msg(VT.SCENE_FG, 1))
        L.pump_sleep(1.5)
        vill = dict(VT.host_villagers(host))
        pose = latest_by_slot(b)
        cand = [s for s in sorted(pose) if s != HOOK_SLOT and s in vill]
        check("B receives NPC_MOVE poses of a regular villager other than the hook slot (%s)" % sorted(pose), len(cand) >= 1)
        if len(cand) < 1:
            return L.summary_and_exit_code(results)
        s_v = cand[0]  # the fixture town spawns only 2 regular villagers: slot 1 belongs to the host talk hook
        auto = Auto(host)

        # ---- P1 puppet push
        stream(a, pose[s_v]["x"] + 10.0, pose[s_v]["y"], pose[s_v]["z"], 0.5)
        p0 = latest_by_slot(b)[s_v]
        px, pz0 = p0["x"] + 10.0, p0["z"]
        mark = b.inbox.mark()
        off = len(host.log_text())
        stream(a, px, p0["y"], pz0, 6.0)
        ht = host.log_text()[off:]
        push = re.findall(r"\[NPC\]\[PUPPET_PUSH\] slot=%d contacts=(\d+) push=\(([-0-9.]+),([-0-9.]+)\) pos=\(([-0-9.]+),([-0-9.]+)\)" % s_v, ht)
        dist = [math.hypot(s["x"] - px, s["z"] - pz0) for s in samples(b, mark) if s["slot"] == s_v]
        check("P1 precondition: the puppet visual is initialised on the host", "[NET][REMOTE][DIAG] visual initialized" in host.log_text())
        check("P1 the host logged PUPPET_PUSH for villager %d (%d lines)" % (s_v, len(push)), len(push) >= 1)
        check("P1 the villager's NPC_MOVE pose seen by client B left the puppet: dist 10 -> max %.1f (>= 25)" % (max(dist) if dist else -1), bool(dist) and max(dist) >= 25.0)

        # ---- P2 held villager is never pushed
        h0 = latest_by_slot(b)[s_v]
        a.send_reliable(VT.npc_talk_msg(s_v, vill[s_v], True, 1))
        got = wait_pumped(host, r"\[NPC\]\[TALKNET\] BEGIN peer=%d slot=%d " % (a.assigned_peer_id, s_v), 5.0, len(host.log_text()) - 1)
        check("P2 the host granted A's conversation lease on villager %d" % s_v, got is not None)
        stream(a, h0["x"], h0["y"], h0["z"], 1.0)  # settle: the held villager stops
        h1 = latest_by_slot(b)[s_v]
        mark = b.inbox.mark()
        off = len(host.log_text())
        stream(a, h1["x"] + 10.0, h1["y"], h1["z"], 6.0)
        ht = host.log_text()[off:]
        hp = [(s["x"], s["z"]) for s in samples(b, mark) if s["slot"] == s_v]
        drift = max([math.hypot(x - h1["x"], z - h1["z"]) for x, z in hp] or [-1])
        check("P2 no PUPPET_PUSH line for the held villager %d" % s_v, re.search(r"\[NPC\]\[PUPPET_PUSH\] slot=%d " % s_v, ht) is None)
        check("P2 the held villager's pose did not move (max drift %.2f <= 3, %d samples)" % (drift, len(hp)), len(hp) >= 10 and 0.0 <= drift <= 3.0)
        a.send_reliable(VT.npc_talk_msg(s_v, vill[s_v], False, 2))
        L.pump_sleep(0.5)

        # ---- P3 host local player walks through the puppet
        def host_xyz():
            pp = auto.cmd("ppos", r"ppos %d [^\r\n]*")
            mm = re.search(r"x=([-\d.]+) y=([-\d.]+) z=([-\d.]+)", pp or "")
            return tuple(float(v) for v in mm.groups()) if mm else None

        def walk_to(tx, tz, ty, puppet_at=None, timeout=25.0):
            cid = auto.send("walk %.1f %.1f 20" % (tx, tz))
            end = time.time() + timeout
            while time.time() < end:
                if puppet_at is not None:
                    a.claim_position(puppet_at[0], ty, puppet_at[1], 0)
                L.pump_sleep(0.033)
                mm = re.search(AUTO_DONE % cid, host.log_text())
                if mm:
                    return mm.group(0)
            return None

        s0 = host_xyz()
        if s0 is None:
            check("P3 host ppos answered", False)
        else:
            hx, hy, hz = s0
            far = (hx + 400.0, hz + 400.0)
            chosen = None
            for dx, dz in ((80.0, 0.0), (-80.0, 0.0), (0.0, 80.0), (0.0, -80.0)):
                stream(a, far[0], hy, far[1], 0.3)  # puppet far away: the control walk has nothing to bump into
                r1 = walk_to(hx + dx, hz + dz, hy, far)
                if r1 is not None and " ok" in r1:
                    chosen = (dx, dz)
                    break
                walk_to(hx, hz, hy, far, 10.0)
            check("P3 control: the host player can walk 80 units %s with no puppet near (path is clear)" % (chosen,), chosen is not None)
            if chosen is not None:
                tx, tz = hx + chosen[0], hz + chosen[1]
                r0 = walk_to(hx, hz, hy, far)  # back to the start
                stream(a, tx, hy, tz, 0.5)
                res = walk_to(tx, tz, hy, (tx, tz), 25.0)
                s2 = host_xyz()
                d2 = math.hypot(s2[0] - tx, s2[2] - tz) if s2 else 999.0
                check("P3 the host player walked INTO the puppet (arrival within 20 of its centre: %r; a solid puppet would stop it ~38 away; it is eased back to %.1f when idle, back-to-start %r)" % (res, d2, r0),
                      res is not None and " ok" in res and d2 < 38.0)
        stream(a, 100.0, 160.0, 100.0, 0.3)

        # ---- Z: host pause with READY peers
        paused = auto.cmd("pause 1", r"pause %d paused=\d+ ready_peers=\d+")
        check("Z1 the real pause toggle ran with >= 1 READY peer (%s)" % paused, paused is not None and "paused=1" in paused and not paused.endswith("ready_peers=0"))
        a.send_reliable(VT.npc_talk_msg(s_v, vill[s_v], True, 3))  # a client lease taken DURING the pause (refreshed every ~8 s: the host expires an idle lease after 30 s)
        mark = b.inbox.mark()
        off = len(host.log_text())
        nxt = [time.time() + 8.0]
        seq = [3]

        def refresh():
            if time.time() >= nxt[0]:
                nxt[0] = time.time() + 8.0
                seq[0] += 1  # a refresh needs a newer seq (the host rejects a stale/duplicate one)
                a.send_reliable(VT.npc_talk_msg(s_v, vill[s_v], True, seq[0]))

        sel = wait_pumped(host, r"\[NPC\]\[TALKHOOK\] host selected villager slot=%d" % HOOK_SLOT, 240.0, off, refresh)
        check("Z2 the host talk hook selected villager %d while the host stayed paused" % HOOK_SLOT, sel is not None)
        end6 = time.time() + 6.0
        while time.time() < end6:
            L.pump_sleep(0.2)
            refresh()
        smp = samples(b, mark)
        frames = sorted({s["frame"] for s in smp})
        moved = {}
        for s in smp:
            moved.setdefault(s["slot"], []).append((s["x"], s["z"]))
        any_moved = any(math.hypot(max(p[0] for p in v) - min(p[0] for p in v), max(p[1] for p in v) - min(p[1] for p in v)) > 1.0 for v in moved.values())
        ht = host.log_text()[off:]
        check("Z1 while paused NPC_MOVE kept arriving at B with advancing frames (%d distinct frames, %d..%d)" % (len(frames), frames[0] if frames else -1, frames[-1] if frames else -1),
              len(frames) >= 60 and frames[-1] > frames[0])
        check("Z1 while paused the world kept ticking: at least one villager pose changed", any_moved)
        i_sel = ht.find("host selected villager")
        check("Z2 while paused NO host conversation started ('talk requested' absent for >= 6 s after the hook selected its villager)",
              i_sel >= 0 and "[NPC][TALKHOOK] host talk requested" not in ht[i_sel:])
        # ---- unpause and recover
        off1 = len(host.log_text())
        up = auto.cmd("pause 0", r"pause %d paused=\d+ ready_peers=\d+")
        check("Z3 unpause toggle ran (%s)" % up, up is not None and "paused=0" in up)
        check("Z2 no host conversation lease (HOST-LEASE) was taken while paused (villager 1 stood %s units from the host player)" % "~40", "HOST-LEASE" not in host.log_text()[off:off1])
        tr = wait_pumped(host, r"\[NPC\]\[TALKHOOK\] host talk requested", 60.0, off1, refresh)
        check("Z3 after the unpause the host conversation starts (hook retry)", tr is not None)
        check("Z3 and the host owns the lease of villager 1 (HOST-LEASE after the unpause)", wait_pumped(host, r"\[NPC\]\[TALKNET\] HOST-LEASE slot=%d " % HOOK_SLOT, 30.0, off1, refresh) is not None)
        te = wait_pumped(host, r"\[NPC\]\[TALKHOOK\] host TALK_END", 120.0, off1, refresh)
        check("Z3 and ends normally (TALK_END)", te is not None)
        seq[0] += 1
        a.send_reliable(VT.npc_talk_msg(s_v, vill[s_v], False, seq[0]))
        end_ = wait_pumped(host, r"\[NPC\]\[TALKNET\] END peer=%d slot=%d " % (a.assigned_peer_id, s_v), 10.0, off)
        check("Z3 the client lease taken during the pause ENDs normally", end_ is not None)
        rel = wait_pumped(host, r"\[NPC\]\[TALKHOLD\] end slot=%d " % s_v, 10.0, off)
        check("Z3 the held villager is released ('[NPC][TALKHOLD] end') - nothing stays held", rel is not None)
        check("host alive", host.alive())
    finally:
        for c in clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        host.stop()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
