#!/usr/bin/env python3
"""test_wildlife_special_real.py - REAL game processes (one host, one resident client; Samsung display, volume 1): worker A's wildlife special cases
end to end through the real client code (presentation, the species programs, the real net swing of the autopilot `catch bug`, the client transaction).

  R1 LATENT  a pill bug the HOST reveals (its own tool queue, `pact` = what a remote rock hit feeds) comes out on the CLIENT too: the client's copy
             leaves HIDE (action 3 -> not 3, drawn) and the client logs the follow
  R2 LATENT  the CLIENT's own tool use (`pact` on the client = the vanilla aINS set_pl_act_tim path) reveals its copy at once AND reaches the host
             (WILDLIFE_TOOL_ACTION 86): the host's mole cricket leaves HIDE as well
  R3 ANTS    an ant on candy is shown on the client with the real ant actor (listed by `bugs`), the client catches it with the real net swing
             (autopilot `catch bug`), the host ACCEPTS species 38 and the client's pockets hold ITM_INSECT38 (0x2D26); a 2nd ant fades when its candy goes
  R4 SPIRITS two spirits caught by the client end up STACKED in ONE pocket (SPIRIT1 0x2D29) -- the real notice seam + client slot choice + host check
  R5 ROACH   a cockroach on a tree tile starts in the tree pose on host AND client (goki action WAIT_ON_TREE 4 / MOVE_ON_TREE 5), not the flower pose
Usage: python wlA_run.py real test_wildlife_special_real.py [--port 14300] [--only r1,r2,r3,r4,r5]"""
import argparse
import os
import re
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wplay_lib as W  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = W.BIN
import net_spike_lib as L  # noqa: E402
import test_wildlife_sim_real as T  # noqa: E402

PRES_BUG_RX = re.compile(r"\[AUTO\] pres entity (\d+) bug species (\d+) actor \S+ exist=(\d) type=(\d+) action=(\d+) drawn=(\d)")
PRES_ANT_RX = re.compile(r"\[AUTO\] pres entity (\d+) ant actor \S+ alive=(\d) action=(-?\d+)")


class Rig(T.Session):
    def start_host(self):
        env = {"AC_TEST_AUTOPILOT": "ac_auto_host.txt", "AC_TEST_WILDLIFE_TRACE_FRAMES": "0"}
        self.host = L.HostProcess(port=self.args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=env, log_path=os.path.join(HERE, "wlA_logs", "wlA_real_host.log"),
                                  bin_dir=W.BIN).start()
        ok = self.host.wait_listening(60) and self.host.boot_to_field(timeout=90, slot=0)
        self.check("host in the field", ok)
        if ok:
            L.resolve_host_town("127.0.0.1", self.args.port)
            self.players["host"] = W.Player(L, "host", self.host)
        return ok

    def start_client(self, label, slot):
        self.n += 1
        env = {"AC_TEST_AUTOPILOT": "ac_auto_%s.txt" % label, "AC_TEST_WILDLIFE_TRACE_FRAMES": "0"}
        c = L.ClientProcess("127.0.0.1:%d" % self.args.port, extra_args=["--bootstrap-resident", str(slot), "-debug"], env=env,
                            log_path=os.path.join(HERE, "wlA_logs", "wlA_real_%s_%d.log" % (label, self.n)), bin_dir=W.BIN, label="wa" + label).start()
        ok = c.boot_to_field(timeout=180, slot=slot)
        self.check("client %s in the field" % label, ok)
        if ok:
            self.clients[label] = c
            self.players[label] = W.Player(L, label, c)
        else:
            c.stop()
        return ok


def done(ans):
    return ans is not None and " ok" in ans


def hspawnbug(R, species, x, z):
    a = R.players["host"].cmd("hspawnbug %d %.0f %.0f" % (species, x, z), 20) or ""
    m = re.search(r"entity (\d+)", a)
    return int(m.group(1)) if m and done(a) else 0


def pres(R, who):
    P = R.players[who]
    off = len(R.log(who))
    P.wait(P.send("pres"), 20)
    txt = R.log(who)[off:]
    bugs = {int(m.group(1)): dict(species=int(m.group(2)), exist=int(m.group(3)), action=int(m.group(5)), drawn=int(m.group(6))) for m in PRES_BUG_RX.finditer(txt)}
    ants = {int(m.group(1)): dict(alive=int(m.group(2)), action=int(m.group(3))) for m in PRES_ANT_RX.finditer(txt)}
    return bugs, ants


def pockets(R, who):
    a = R.players[who].cmd("pockets", 20) or ""
    return [int(v, 16) for v in re.findall(r"\b([0-9A-F]{4})\b", a.split(" ok ", 1)[-1])] if done(a) else []


def wait_until(fn, timeout, step=0.7):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def spot(R, dx=80.0, dz=0.0):
    """a point next to the client player (x, z, ut_x, ut_z) -- the unit centre"""
    p = R.players["A"].pos()
    x, z = p[0] + dx, p[1] + dz
    ux, uz = int(x // 40), int(z // 40)
    return ux * 40 + 20.0, uz * 40 + 20.0, ux, uz


def r1(R):
    check = R.check
    x, z, ux, uz = spot(R, 80.0, 0.0)
    ent = hspawnbug(R, 36, x, z)
    check("R1 a pill bug record %d next to the client" % ent, ent != 0)
    shown = wait_until(lambda: pres(R, "A")[0].get(ent), 15)
    check("R1 the client shows it hidden under its rock (%s)" % shown, shown and shown["action"] == 3 and shown["drawn"] == 0)
    hb = pres(R, "host")[0].get(ent)
    check("R1 the host's copy is hidden too (%s)" % hb, hb and hb["action"] == 3)
    off = len(R.log("A"))
    check("R1 the host's tool queue takes a rock hit at ut(%d,%d)" % (ux, uz), done(R.players["host"].cmd("pact 1 %d %d" % (ux, uz), 20)))
    out = wait_until(lambda: (lambda b: b if b and b["action"] != 3 else None)(pres(R, "A")[0].get(ent)), 10)
    check("R1 the client's copy came out after the HOST revealed it (%s)" % out, out is not None)
    check("R1 the client logged the follow", "latent bug species 36 revealed" in R.log("A")[off:])


def r2(R):
    check = R.check
    x, z, ux, uz = spot(R, -80.0, 40.0)
    ent = hspawnbug(R, 33, x, z)
    shown = wait_until(lambda: pres(R, "A")[0].get(ent), 15)
    check("R2 a hidden mole cricket %d on the client (%s)" % (ent, shown), ent != 0 and shown and shown["action"] == 2)
    hoff = len(R.log("host"))
    check("R2 the CLIENT digs at ut(%d,%d) (vanilla tool path)" % (ux, uz), done(R.players["A"].cmd("pact 3 %d %d" % (ux, uz), 20)))
    out = wait_until(lambda: (lambda b: b if b and b["action"] != 2 else None)(pres(R, "A")[0].get(ent)), 6)
    check("R2 the client's copy came out at once (%s)" % out, out is not None)
    hl = lambda: R.log("host")[hoff:]  # noqa: E731
    check("R2 the host received the client's tool action and fed it to its insects",
          wait_until(lambda: "tool action 3 at ut(%d,%d) queued" % (ux, uz) in hl() and "handed to the insects" in hl(), 6))
    hout = wait_until(lambda: (lambda b: b if b and b["action"] != 2 else None)(pres(R, "host")[0].get(ent)), 8)
    check("R2 the host's mole cricket came out too (%s)" % hout, hout is not None)


def r3(R):
    check = R.check
    x, z, ux, uz = spot(R, 0.0, 80.0)
    H = R.players["host"]
    pl = H.cmd("place 2806 %d %d" % (ux, uz), 20) or ""
    check("R3 candy placed next to the client (%s)" % pl, " placed" in pl and "NOT placed" not in pl)
    time.sleep(1.5)
    ant = hspawnbug(R, 38, x, z)
    ok = wait_until(lambda: (lambda a: a if a and a["alive"] == 1 else None)(pres(R, "A")[1].get(ant)), 12)
    check("R3 the client shows ant %d with the real ant actor (%s)" % (ant, ok), ant != 0 and ok is not None)
    bl = R.players["A"].bugs()
    check("R3 the client's `bugs` lists the ant (%s)" % bl, any(e == ant and s == 38 for e, s, _x, _z in bl))
    p0 = pockets(R, "A")
    hoff = len(R.log("host"))
    ans = R.players["A"].cmd("catch bug %d" % ant, 130)
    check("R3 the client's real net catch of the ant ended ok (%s)" % ans, done(ans))
    check("R3 the host ACCEPTED the ant catch (species 38)", re.search(r"CATCH entity %d \(species claim 38\) accepted" % ant, R.log("host")[hoff:]) is not None)
    time.sleep(2)
    R.players["A"].cmd("mash 4", 30)
    p1 = pockets(R, "A")
    check("R3 the client's pockets gained ITM_INSECT38 0x2D26 (%d -> %d)" % (p0.count(0x2D26), p1.count(0x2D26)), p1.count(0x2D26) == p0.count(0x2D26) + 1)
    # a 2nd ant fades when its candy goes
    ant2 = hspawnbug(R, 38, x, z)
    wait_until(lambda: (lambda a: a if a and a["alive"] == 1 else None)(pres(R, "A")[1].get(ant2)), 10)
    H.cmd("place 0 %d %d" % (ux, uz), 20)
    gone = wait_until(lambda: (lambda a: a is None or a["alive"] == 0)(pres(R, "A")[1].get(ant2)), 10)
    check("R3 ant %d faded on the client once its candy was gone" % ant2, gone)


def r4(R):
    check = R.check
    H = R.players["host"]
    pos = R.players["A"].pos()
    bx, bz = pos[2], pos[3]
    gt = H.cmd("ghost on %d,%d" % (bx, bz), 20)
    check("R4 the host's ghost hunt is on (%s)" % gt, done(gt))
    p0 = pockets(R, "A")
    got = 0
    for i in range(2):
        x, z, _ux, _uz = spot(R, 60.0 + 30.0 * i, -60.0)
        ent = hspawnbug(R, 40, x, z)
        wait_until(lambda: pres(R, "A")[0].get(ent), 10)
        hoff = len(R.log("host"))
        ans = R.players["A"].cmd("catch bug %d" % ent, 130)
        acc = re.search(r"CATCH entity %d \(species claim 40\) accepted" % ent, R.log("host")[hoff:]) is not None
        check("R4 spirit %d (#%d) caught by the client and accepted by the host (%s)" % (ent, i + 1, ans), done(ans) and acc)
        got += 1 if acc else 0
        time.sleep(2)
        R.players["A"].cmd("mash 4", 30)
    p1 = pockets(R, "A")
    spirits = [p for p in p1 if 0x2D28 <= p <= 0x2D2C]
    before = [p for p in p0 if 0x2D28 <= p <= 0x2D2C]
    check("R4 (G1) the two spirits are STACKED in one pocket (%s -> %s)" % ([hex(p) for p in before], [hex(p) for p in spirits]),
          got == 2 and not before and spirits == [0x2D29])


def r5(R):
    check = R.check
    H = R.players["host"]
    x, z, ux, uz = spot(R, 120.0, 120.0)
    H.cmd("place 804 %d %d" % (ux, uz), 20)  # TREE
    time.sleep(1.5)
    ent = hspawnbug(R, 28, x, z)
    hb = wait_until(lambda: pres(R, "host")[0].get(ent), 10)
    cb = wait_until(lambda: pres(R, "A")[0].get(ent), 12)
    check("R5 the cockroach %d on a tree starts in the tree pose on the HOST (%s)" % (ent, hb), hb and hb["action"] in (4, 5))
    check("R5 ... and on the CLIENT (%s)" % cb, cb and cb["action"] in (4, 5))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=14300)
    ap.add_argument("--only", default="r1,r2,r3,r4,r5")
    args = ap.parse_args()
    os.makedirs(os.path.join(HERE, "wlA_logs"), exist_ok=True)
    results = []
    W.make_fixture()
    R = Rig(args, results)
    try:
        if R.start_host() and R.start_client("A", 1):
            R.players["A"].cmd("mash 6", 40)
            steps = {"r1": r1, "r2": r2, "r3": r3, "r4": r4, "r5": r5}
            for name in args.only.split(","):
                print("=" * 72 + "\n" + name.upper())
                try:
                    steps[name](R)
                except W.PlayerStuck as e:
                    R.check("%s: %s" % (name, e), False)
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
