#!/usr/bin/env python3
"""test_npc_push_nonowner_real.py - villager push by NON-OWNER clients in the REAL game (user report: "the guest can push the stationary villager, the resident cannot").

Cause: a villager the host does not simulate (far from the dedicated server's observer) is run by ONE client (stale fallback LOCAL mode, the relay owner = the first
NPC_PROXY_MOVE sender); every other client mirrors the relayed stream and its own collision push was never integrated anywhere. Fix (AC_MP_NPC_PUSH_FIX, default on):
the owner's client integrates the remote players' puppet push into the villager it runs (the same pc_remote_player_host_npc_push the host uses for its own villagers),
and the result reaches every other client through the relay.

Setup: dedicated server (hidden observer at the station acre) + a REAL resident client R (--bootstrap-resident 1) + a REAL guest client G (--guest), both with the
TEST-ONLY autopilot, parked next to a villager >= 2 acres from the observer (host: "no actor"). Fixture pc\\build64\\bin_fixture4_vp_npcpush (fresh copy of bin_fixture4 +
the build64_w3 exe). Samsung display, volume 1. Ports 34000-34499 only.
  S1 the host relays exactly one client's villager ("relay peer N"), the other client mirrors it (HOST mode)
  S2 the NON-OWNER chases the villager (it may be walking): the OWNER's client integrates the remote push (PC_NPC_PUSH_DIAG contact lines, pushed away from the pusher,
     '(local AI, relayed) pushed by' line), both clients show the villager at the same spot; the non-owner never integrates a push itself
  S3 the OWNER chases it: the non-owner sees the same spot
  S4 no relay ownership change caused by the pushes (no flapping) and no '-> local AI' / '-> host' transition on the non-owner during the pushes
Usage: python test_npc_push_nonowner_real.py [--port 34210]
"""
import argparse
import math
import os
import re
import shutil
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_vp_npcpush"
EXE = os.path.abspath(os.path.join(HERE, "..", "..", "build64_w3", "bin", "AnimalCrossing.exe"))
BIN = os.path.join(T.BUILD64, FIXTURE)
os.environ["NET_SPIKE_GAME_BIN"] = BIN

import net_spike_lib as L  # noqa: E402
import test_npc_fallback_protocol as P  # noqa: E402  (shared wire / console helpers)
os.environ["NET_SPIKE_GAME_BIN"] = BIN


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

    def npc(self, slot):
        a = self.cmd("npcpos", r"\[AUTO\] npcpos %d[^\r\n]*", 15.0) or ""
        m = re.search(r" %d:(-?[\d.]+):(-?[\d.]+):(-?[\d.]+)" % slot, a)
        return (float(m.group(1)), float(m.group(3))) if m else None

    def pos(self):
        a = self.cmd("pos", r"\[AUTO\] pos %d [^\r\n]*", 10.0) or ""
        m = re.search(r"x=([-\d.]+) z=([-\d.]+)", a)
        return (float(m.group(1)), float(m.group(2))) if m else None

    def tp(self, x, z):
        return self.cmd("tp %.1f %.1f" % (x, z), r"\[AUTO\] tp %d [^\r\n]*", 15.0)

    def walk(self, x, z, timeout=25.0):
        a = self.cmd("walk %.1f %.1f 20" % (x, z), r"\[AUTO\] done %d walk \S+[^\r\n]*", timeout)
        return a


def start(port, label, args, env_extra=None):
    env = {"AC_TEST_HOOKS": "1", "AC_TEST_AUTOPILOT": "ac_auto_%s.txt" % label, "PC_NPC_PUSH_DIAG": "1"}
    env.update(env_extra or {})
    c = L.ClientProcess("127.0.0.1:%d" % port, extra_args=args + ["-debug"], env=env, log_path=os.path.join(HERE, "npcpush_real_%s.log" % label), bin_dir=BIN,
                        label=label).start()
    orig = c.log_text
    c.log_text = lambda: orig().replace("\r\n", "\n")
    return c


PUSH_RX = r"\[NPC\]\[PUPPET_PUSH\] slot=%d contacts=(\d+) push=\((-?[\d.]+),(-?[\d.]+)\) pos=\((-?[\d.]+),(-?[\d.]+)\)"


def push_trial(tag, pusher, other, integrator, slot, rounds=4):
    """`rounds` times: the pusher is parked 48 units from the villager's CURRENT spot (it may be walking: run 1 showed a villager walking away from a straight
    walk-through that started 60 units off) and walks into it, to a point 30 units behind its centre (seen on the pusher's own client). Returns (pushes, mean_dot, gap): the PC_NPC_PUSH_DIAG contact lines that `integrator` logged in the
    window (integrator = the owner for a non-owner push: the remote push integrated there), the mean cosine between each push vector and the pusher -> villager direction
    (> 0: pushed away from the pusher), and the final distance between the two clients' views of the villager (the other client sees the same pose)."""
    dirs, pushes, dots = [], [], []
    for k in range(rounds):
        v = pusher.npc(slot)
        if v is None:
            time.sleep(0.5)
            continue
        ang = k * 2.4  # a new approach direction each round (a fence / tree may block one)
        ux, uz = math.cos(ang), math.sin(ang)
        pusher.tp(v[0] - ux * 48.0, v[1] - uz * 48.0)  # just outside contact (pipe radii ~20 + 20)
        time.sleep(0.6)
        v = pusher.npc(slot) or v
        p = pusher.pos()
        if p is None:
            continue
        dx, dz = v[0] - p[0], v[1] - p[1]
        d = math.hypot(dx, dz) or 1.0
        dirs.append((dx / d, dz / d))
        off_k = len(integrator.proc.log_text())
        pusher.walk(v[0] + dx / d * 30.0, v[1] + dz / d * 30.0, timeout=8.0)  # walk INTO it (the target is behind its centre)
        time.sleep(0.6)
        for m in re.finditer(PUSH_RX % slot, integrator.proc.log_text()[off_k:]):
            pdx, pdz = float(m.group(2)), float(m.group(3))
            n = math.hypot(pdx, pdz)
            pushes.append((pdx, pdz))
            if n > 0:
                dots.append((pdx * dx + pdz * dz) / (n * d))  # this round's pusher -> villager direction
    time.sleep(1.5)  # relay + interpolation delay
    a, b = pusher.npc(slot), other.npc(slot)
    gap = math.hypot(a[0] - b[0], a[1] - b[1]) if a and b else None
    L.info("%s: %d steps, %d push contact lines on the integrating client, mean cos %.2f, views %s / %s (gap %s)" %
           (tag, len(dirs), len(pushes), (sum(dots) / len(dots)) if dots else 0.0, a, b, None if gap is None else "%.1f" % gap))
    return len(pushes), ((sum(dots) / len(dots)) if dots else 0.0), gap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=34210)
    args = ap.parse_args()
    if not (34000 <= args.port <= 34499):
        print("REFUSING: ports 34000-34499 only", file=sys.stderr)
        return 2
    T.make_fixture(FIXTURE)
    shutil.copy2(EXE, os.path.join(BIN, "AnimalCrossing.exe"))
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    t0 = time.time()
    with L.CloneSaveGuard(BIN):
        mp = os.path.join(BIN, "save", "mp")
        os.makedirs(mp, exist_ok=True)
        with open(os.path.join(mp, "guest.ini"), "wb") as f:
            f.write(b"# test profile\nname = Quill\ngender = 1\nface = 5\nhome_town = Hometwn\nplayer_id = 0x4D31\nland_id = 24130\n")
        h = L.HostProcess(port=args.port, extra_args=["--dedicated"], log_path=os.path.join(HERE, "npcpush_real_host.log"), bin_dir=BIN, stdin_pipe=True, verbose=True,
                          new_group=True).start()
        orig = h.log_text
        h.log_text = lambda: orig().replace("\r\n", "\n")
        procs = []
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
            ck("`npc` header names the push rule", "every player pushes a relayed villager" in out)
            obs = re.search(r"around acre \((-?\d+),(-?\d+)\)", out)
            ox, oz = (int(obs.group(1)), int(obs.group(2))) if obs else (3, 1)
            far = [v for v in vs if v[2].startswith("no actor") and max(abs(v[5] - ox), abs(v[6] - oz)) >= 2]
            ck("a villager >= 2 acres from the observer acre (%d,%d) has no host actor (%d)" % (ox, oz, len(far)), far)
            if not far:
                return L.summary_and_exit_code(results)
            s, nid, _k, vx, vz, bx, bz = far[0]
            L.info("target villager: slot %d npc 0x%04X list pos (%d,%d) acre (%d,%d)" % (s, nid, vx, vz, bx, bz))

            r = start(args.port, "R", ["--bootstrap-resident", "1"])
            procs.append(r)
            ck("R resident client in the field", r.boot_to_field(timeout=200, slot=1))
            ar = Auto(r, "R")
            ar.tp(vx + 70.0, vz)
            m = r.wait_for_log(r"\[NET\]\[NPC\] client: slot %d npc 0x%04X (?:no host sample in|host silent for) \d+ ms -> local AI" % (s, nid), 15.0)
            ck("R runs the villager on its own AI", m is not None)

            g = start(args.port, "G", ["--guest"])
            procs.append(g)
            ag = Auto(g, "G")
            deadline = time.time() + 240
            gp = None
            while time.time() < deadline and gp is None and g.alive():
                gp = ag.pos()
            ck("G guest client in the field (autopilot answers pos)", gp is not None)
            if gp is None:
                return L.summary_and_exit_code(results)
            g.wait_for_log(r"\[NET\]\[REC\] client: adopted rev", 90)
            ag.cmd("mash 40", r"\[AUTO\] done %d mash[^\r\n]*", 90.0)  # the stationmaster's arrival dialogue
            vr = ar.npc(s) or (vx, vz)
            ag.tp(vr[0] - 70.0, vr[1])
            time.sleep(8.0)  # G's NPC manager spawns the villager; G's grace window; the relay settles
            out = P.say(h, "npc")
            mo = re.search(r"slot\s+%d npc 0x%04X relay peer (\d+)" % (s, nid), out)
            ck("S1 the host relays one client's villager (%s)" % (mo.group(0) if mo else out.strip()[-300:]), mo is not None)
            if mo is None:
                return L.summary_and_exit_code(results)
            owner_peer = int(mo.group(1))
            hl = h.log_text()
            kinds = dict((int(p), k) for p, k in re.findall(r"peer (\d+) READY: (RESIDENT|GUEST)", hl))
            L.info("peers: %s, relay owner peer %d" % (kinds, owner_peer))
            owner_kind = kinds.get(owner_peer)
            ck("S1 the relay owner is one of the two clients (%s)" % owner_kind, owner_kind in ("RESIDENT", "GUEST"))
            if owner_kind is None:
                return L.summary_and_exit_code(results)
            (own, aown, oname), (non, anon, nname) = ((r, ar, "R"), (g, ag, "G")) if owner_kind == "RESIDENT" else ((g, ag, "G"), (r, ar, "R"))
            ck("S1 the non-owner %s mirrors the relayed villager (HOST mode: it has no live '-> local AI' without a later '-> host')" % nname,
               anon.npc(s) is not None)
            relay_lines0 = len(re.findall(r"slot %d npc 0x%04X not simulated by the host -> relaying peer" % (s, nid), h.log_text()))
            non_off = len(non.log_text())

            n2, cos2, gap2 = push_trial("S2 non-owner %s pushes" % nname, anon, aown, aown, s)
            ck("S2 the NON-OWNER %s's push is integrated on the owner %s (%d contact lines, pushed away from the pusher: mean cos %.2f)" % (nname, oname, n2, cos2), n2 >= 3 and cos2 > 0.3)
            ck("S2 the owner logged the remote push ('(local AI, relayed) pushed by')",
               re.search(r"\[NET\]\[NPC\] client: slot %d \(local AI, relayed\) pushed by \d+ remote player" % s, own.log_text()) is not None)
            ck("S2 both clients show the villager at the same spot after the push (gap %s)" % gap2, gap2 is not None and gap2 < 30.0)
            ck("S2 the non-owner (a consumer) never integrated a push itself", re.search(PUSH_RX % s, non.log_text()[non_off:]) is None)
            n3, _c3, gap3 = push_trial("S3 owner %s pushes" % oname, aown, anon, aown, s)
            ck("S3 the OWNER %s pushes it (vanilla local push: no remote-push line needed, %d) and the non-owner sees the same spot (gap %s)" % (oname, n3, gap3),
               gap3 is not None and gap3 < 30.0)
            out = P.say(h, "npc")
            mo2 = re.search(r"slot\s+%d npc 0x%04X relay peer (\d+)" % (s, nid), out)
            relay_lines1 = len(re.findall(r"slot %d npc 0x%04X not simulated by the host -> relaying peer" % (s, nid), h.log_text()))
            ck("S4 the relay owner did not change during the pushes (peer %s -> %s, %d new relay lines)" % (owner_peer, mo2.group(1) if mo2 else None, relay_lines1 - relay_lines0),
               mo2 is not None and int(mo2.group(1)) == owner_peer and relay_lines1 == relay_lines0)
            nt = non.log_text()[non_off:]
            ck("S4 no mode flapping on the non-owner during the pushes", re.search(r"client: slot %d npc 0x%04X (?:host silent|no host sample|host stream back)" % (s, nid), nt) is None)
            ck("host and both clients alive", h.alive() and r.alive() and g.alive())
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
