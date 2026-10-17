#!/usr/bin/env python3
"""test_npc_fallback_real.py - villager authority range and stale fallback in the REAL game: a dedicated server (hidden observer at the station acre) and one REAL
client (autopilot), plus a scripted FakeClient for the stream hand-over.

The user's bug: villagers far from the server's observer never get a host actor, so no NPC_MOVE stream: the client held them at their spawn pose forever and could not push
them. Fixture: pc\\build64\\bin_fixture4_npc_real (fresh copy of bin_fixture4 + the build64_w3 exe). Samsung display, volume 1.
  P1 fallback ON (default): the client is parked next to a villager the host does not simulate (>= 2 acres from the observer); over 20 s the villager MOVES on the client
     (local AI), the client logs '-> local AI' for it, the host relays it ('relaying peer'); the client walks into it a few times (reported)
  P2 hand-over (client with AC_MP_NPC_RELAY=0 so a FakeClient can own the relay): the FakeClient streams a fixed pose for that villager -> the real client switches to the
     host stream ('-> host') and its villager stands at the streamed pose; the stream stops -> within ~3 s the client logs 'host silent ... -> local AI' and the villager
     moves again (the "villager left the host's range" case)
  P3 control AC_MP_NPC_FALLBACK=0 (the old behaviour): the same villager stays frozen for 20 s and walking into it does not move it (the reported bug)
Usage: python test_npc_fallback_real.py [--port 28420] [--phases 123]
"""
import argparse
import math
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

FIXTURE = "bin_fixture4_npc_real"
EXE = os.path.abspath(os.path.join(HERE, "..", "..", "build64_w3", "bin", "AnimalCrossing.exe"))
BIN = os.path.join(T.BUILD64, FIXTURE)
os.environ["NET_SPIKE_GAME_BIN"] = BIN

import net_spike_lib as L  # noqa: E402
import test_npc_fallback_protocol as P  # noqa: E402  (shared wire helpers)
os.environ["NET_SPIKE_GAME_BIN"] = BIN  # (the helper module names its own fixture)


class Auto:
    """the TEST-ONLY autopilot of one real client (AC_TEST_AUTOPILOT=<file>, answers as [AUTO] log lines)"""

    def __init__(self, proc, label):
        self.proc, self.file, self.next_id = proc, os.path.join(BIN, "ac_auto_%s.txt" % label), 1

    def cmd(self, line, rx, timeout=30.0):
        cid = self.next_id
        self.next_id += 1
        off = len(self.proc.log_text())
        with open(self.file, "w") as f:
            f.write("%d %s\n" % (cid, line))
        m = self.proc.wait_for_log(rx % cid, timeout, since_offset=off)
        return m.group(0) if m else None

    def npcpos(self):
        a = self.cmd("npcpos", r"\[AUTO\] npcpos %d[^\r\n]*", 15.0) or ""
        return {int(m.group(1)): (float(m.group(2)), float(m.group(4))) for m in re.finditer(r" (\d+):(-?[\d.]+):(-?[\d.]+):(-?[\d.]+)", a)}

    def npcy(self, slot):
        a = self.cmd("npcpos", r"\[AUTO\] npcpos %d[^\r\n]*", 15.0) or ""
        m = re.search(r" %d:(-?[\d.]+):(-?[\d.]+):(-?[\d.]+)" % slot, a)
        return float(m.group(2)) if m else 0.0

    def pos(self):
        a = self.cmd("pos", r"\[AUTO\] pos %d [^\r\n]*", 15.0) or ""
        m = re.search(r"x=([-\d.]+) z=([-\d.]+)", a)
        return (float(m.group(1)), float(m.group(2))) if m else None

    def tp(self, x, z):
        return self.cmd("tp %.1f %.1f" % (x, z), r"\[AUTO\] tp %d [^\r\n]*", 15.0)

    def walk(self, x, z, timeout=45.0):
        a = self.cmd("walk %.1f %.1f 20" % (x, z), r"\[AUTO\] done %d walk \S+[^\r\n]*", timeout)
        return a is not None and " ok" in a


def start_client(port, label, env_extra):
    env = {"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_%s.txt" % label}
    env.update(env_extra)
    c = L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--bootstrap-resident", "1", "-debug"], env=env, log_path=os.path.join(HERE, "npcfb_real_%s.log" % label),
                        bin_dir=BIN, label=label).start()
    orig = c.log_text
    c.log_text = lambda: orig().replace("\r\n", "\n")
    return c


def track(au, slot, seconds):
    """npcpos samples of `slot` for `seconds`; returns (samples, max displacement from the first sample)"""
    pts = []
    end = time.time() + seconds
    while time.time() < end:
        p = au.npcpos().get(slot)
        if p is not None:
            pts.append(p)
        time.sleep(1.0)
    if not pts:
        return pts, 0.0
    return pts, max(math.hypot(x - pts[0][0], z - pts[0][1]) for x, z in pts)


def park_near(au, ck, tag, vx, vz):
    """park the client next to the villager's spot and return the slot -> pose map of the client's live villager actors"""
    au.tp(vx + 70.0, vz)
    time.sleep(8.0)  # the client's NPC manager spawns the villagers of the new acres
    return au.npcpos()


def nearest_slot(live, vx, vz, want=None):
    best = None
    for s, (x, z) in live.items():
        if want is not None and s != want:
            continue
        d = math.hypot(x - vx, z - vz)
        if best is None or d < best[1]:
            best = (s, d)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=28420)
    ap.add_argument("--phases", default="123")
    args = ap.parse_args()
    if not (28000 <= args.port <= 28999):
        print("REFUSING: ports 28000-28999 only", file=sys.stderr)
        return 2
    T.make_fixture(FIXTURE)
    shutil.copy2(EXE, os.path.join(BIN, "AnimalCrossing.exe"))
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    t0 = time.time()
    with L.CloneSaveGuard(BIN):
        h = L.HostProcess(port=args.port, extra_args=["--dedicated"], log_path=os.path.join(HERE, "npcfb_real_host.log"), bin_dir=BIN, stdin_pipe=True, verbose=True,
                          new_group=True).start()
        orig = h.log_text
        h.log_text = lambda: orig().replace("\r\n", "\n")
        procs = []
        target = None  # (slot, npc_id, x, z) of the villager the phases use
        try:
            if h.wait_for_log(r"Use this town", 12.0) is not None:
                h.send_line("1")
            ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=150.0)
            ck("dedicated host ready", ok)
            if not ok:
                return L.summary_and_exit_code(results)
            L.resolve_host_town("127.0.0.1", args.port)
            out, vs = P.villagers(h)
            L.info("npc console:\n" + out.strip())
            obs = re.search(r"around acre \((-?\d+),(-?\d+)\)", out)
            ox, oz = (int(obs.group(1)), int(obs.group(2))) if obs else (3, 1)
            far = [v for v in vs if v[2].startswith("no actor") and max(abs(v[5] - ox), abs(v[6] - oz)) >= 2]
            ck("there is a villager >= 2 acres from the observer acre (%d,%d) without a host actor (%d)" % (ox, oz, len(far)), far)
            if not far:
                return L.summary_and_exit_code(results)
            s, nid, _k, vx, vz, bx, bz = far[0]
            L.info("target villager: slot %d npc 0x%04X list pos (%d,%d) acre (%d,%d)" % (s, nid, vx, vz, bx, bz))

            if "1" in args.phases:
                c = start_client(args.port, "C1", {})
                procs.append(c)
                ck("P1 client in the field", c.boot_to_field(timeout=200, slot=1))
                au = Auto(c, "C1")
                live = park_near(au, ck, "P1", vx, vz)
                ns = nearest_slot(live, vx, vz, want=s)
                ck("P1 the client has a live actor of the target villager (slot %d; live %s)" % (s, sorted(live)), ns is not None)
                if ns is not None:
                    m_local = c.wait_for_log(r"\[NET\]\[NPC\] client: slot %d npc 0x%04X (?:no host sample in|host silent for) \d+ ms -> local AI" % (s, nid), 10.0)
                    ck("P1 the client switched the villager to local AI (no host stream)", m_local is not None)
                    pts, disp = track(au, s, 20.0)
                    ck("P1 over 20 s the villager MOVES on the client (max displacement %.1f, %d samples)" % (disp, len(pts)), disp > 15.0)
                    ck("P1 the host relays the client's villager", h.wait_for_log(r"slot %d npc 0x%04X not simulated by the host -> relaying peer" % (s, nid), 5.0) is not None)
                    out = P.say(h, "npc")
                    ck("P1 `npc` names the relay owner for the villager", re.search(r"slot\s+%d npc 0x%04X relay peer \d+" % (s, nid), out) is not None)
                    before = au.npcpos().get(s)
                    for _ in range(3):  # walk into it a few times (pushing works again: it is integrated locally)
                        p = au.npcpos().get(s)
                        if p is None:
                            break
                        au.walk(p[0], p[1], timeout=20.0)
                    after = au.npcpos().get(s)
                    L.info("P1 walk-into: villager %s -> %s" % (before, after))
                c.stop()
                procs.remove(c)
                time.sleep(3.0)

            if "2" in args.phases:
                c = start_client(args.port, "C2", {"AC_MP_NPC_RELAY": "0"})
                procs.append(c)
                ck("P2 client (relay off) in the field", c.boot_to_field(timeout=200, slot=1))
                au = Auto(c, "C2")
                live = park_near(au, ck, "P2", vx, vz)
                ok_local = c.wait_for_log(r"\[NET\]\[NPC\] client: slot %d npc 0x%04X (?:no host sample in|host silent for) \d+ ms -> local AI" % (s, nid), 12.0)
                ck("P2 the client runs the villager locally first", s in live and ok_local is not None)
                f = L.FakeClient("F", "127.0.0.1", args.port, player=L.resident_player(2))
                f.connect_and_ready(quiet=True)
                P.scene_fg(f, 1)
                cp = au.pos() or (vx + 70.0, vz)
                fx, fz = cp[0] - 60.0, cp[1] + 10.0  # a fixed spot next to the client (walkable: the client stands 60 units away)
                for i in range(5):
                    f.send_move(100 + i, fx + 25.0, 0.0, fz)
                    L.pump_sleep(0.05)
                off = len(c.log_text())
                yv = au.npcy(s)  # the villager's real ground height (a y of 0 would put it under the ground and the BG check moves it)
                for i in range(60):  # 6 s of a steady 10 Hz stream
                    f.send_unreliable(P.proxy(s, nid, 1000 + i, fx, yv, fz))
                    L.pump_sleep(0.1)
                to_host = c.wait_for_log(r"\[NET\]\[NPC\] client: slot %d npc 0x%04X host stream back \(\d+ fresh samples\) -> host" % (s, nid), 1.0, since_offset=off)
                ck("P2 a fresh stream switches the client to the host stream ('-> host')", to_host is not None)
                pp = au.npcpos().get(s)
                ck("P2 the client's villager stands at the streamed pose (%s vs %.0f,%.0f)" % (pp, fx, fz), pp is not None and math.hypot(pp[0] - fx, pp[1] - fz) < 15.0)  # the BG check after the pose may push it off an obstacle
                t_stop = time.time()
                silent = c.wait_for_log(r"\[NET\]\[NPC\] client: slot %d npc 0x%04X host silent for (\d+) ms -> local AI" % (s, nid), 8.0, since_offset=off)
                dt = time.time() - t_stop
                ck("P2 the stream stopped: the client switched back to local AI within ~3.5 s (%.1f s, %s)" % (dt, silent.group(1) if silent else None),
                   silent is not None and dt < 4.5)
                pts, disp = track(au, s, 40.0)
                ck("P2 ... and the villager moves again (max displacement %.1f)" % disp, disp > 10.0)
                ck("P2 no flapping: exactly one '-> host' and one '-> local AI' for the hand-over",
                   len(re.findall(r"client: slot %d npc 0x%04X host stream back" % (s, nid), c.log_text()[off:])) == 1
                   and len(re.findall(r"client: slot %d npc 0x%04X host silent for" % (s, nid), c.log_text()[off:])) == 1)
                f.close()
                c.stop()
                procs.remove(c)
                time.sleep(3.0)

            if "3" in args.phases:
                c = start_client(args.port, "C3", {"AC_MP_NPC_FALLBACK": "0"})
                procs.append(c)
                ck("P3 control client (fallback OFF) in the field", c.boot_to_field(timeout=200, slot=1))
                au = Auto(c, "C3")
                live = park_near(au, ck, "P3", vx, vz)
                ck("P3 the client has a live actor of the target villager", s in live)
                if s in live:
                    pts, disp = track(au, s, 20.0)
                    ck("P3 control: without the fallback the villager is FROZEN for 20 s (max displacement %.2f) -- the reported bug" % disp, disp < 1.0 and len(pts) >= 10)
                    p = au.npcpos().get(s)
                    if p is not None:
                        au.walk(p[0], p[1], timeout=20.0)
                        au.walk(p[0] - 60.0, p[1], timeout=20.0)
                        q = au.npcpos().get(s)
                        ck("P3 control: walking into it does not move it (%s -> %s)" % (p, q), q is not None and math.hypot(q[0] - p[0], q[1] - p[1]) < 1.0)
                    ck("P3 control: no fallback transition logged", "-> local AI" not in c.log_text())
                c.stop()
                procs.remove(c)
            ck("host alive throughout", h.alive())
        finally:
            for p in procs:
                try:
                    p.stop()
                except Exception:  # noqa: BLE001
                    pass
            try:
                h.send_line("stop")
                if h.wait_exit(40.0) is None:
                    h.stop()
            except Exception:  # noqa: BLE001
                h.stop()
            print("elapsed %.0f s" % (time.time() - t0))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
