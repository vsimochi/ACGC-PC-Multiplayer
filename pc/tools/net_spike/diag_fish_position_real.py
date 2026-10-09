#!/usr/bin/env python3
"""diag_fish_position_real.py - INVESTIGATION (not a regression test): where is the same fish, in the same second, in the host / the angler A / an observer B?

Real processes: host (resident 0) + resident clients A (angler) and B (observer), all on the Samsung display at volume 1, 1-second creature traces in every process
(AC_TEST_WILDLIFE_TRACE_FRAMES=60 -> '[NET][WILDLIFE][TRACE] now entity N fish ...' with world position, home, block, action). The traces carry no clock, so the k-th sample of each process is
compared (a ~1 s skew at fish speed is the noise floor, reported).
Phases: PRE (nobody fishing), CATCH (A catches the fish; what do host / A / B show before, during, after), TWO (A and B both fish the same fish), KILL (A's process dies with the fish tied).
Output: a text report on stdout."""
import os
import re
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_wildlife_proxy_capacity_real as C  # noqa: E402
import net_spike_lib as L  # noqa: E402
import test_wildlife_sim_real as T  # noqa: E402

FISH_RX = re.compile(r"\[TRACE\] now entity (\d+) fish species (\d+) world\(([-\d.]+),([-\d.]+),([-\d.]+)\) home\(([-\d.]+),([-\d.]+),([-\d.]+)\) block\((\d+),(\d+)\) action=(-?\d+)")


def samples(R, who, since, ent):
    out = []
    for m in FISH_RX.finditer(R.log(who)[since:]):
        if int(m.group(1)) == ent:
            out.append(dict(x=float(m.group(3)), y=float(m.group(4)), z=float(m.group(5)), hx=float(m.group(6)), hz=float(m.group(8)), bx=int(m.group(9)), bz=int(m.group(10)), act=int(m.group(11))))
    return out


def dist(a, b):
    return ((a["x"] - b["x"]) ** 2 + (a["z"] - b["z"]) ** 2) ** 0.5


def compare(R, tag, offs, ent, show=6):
    s = {w: samples(R, w, offs[w], ent) for w in ("host", "A", "B")}
    print("  %s: sample counts host=%d A=%d B=%d" % (tag, len(s["host"]), len(s["A"]), len(s["B"])))
    n = min(len(v) for v in s.values())
    if n == 0:
        return s
    dA = [dist(s["host"][i], s["A"][i]) for i in range(n)]
    dB = [dist(s["host"][i], s["B"][i]) for i in range(n)]
    print("  %s: |host-A| max %.1f mean %.1f ; |host-B| max %.1f mean %.1f  (world units, k-th sample pairs, %d pairs)" % (tag, max(dA), sum(dA) / n, max(dB), sum(dB) / n, n))
    for i in list(range(min(show, n))):
        print("    #%d host(%.0f,%.0f,y%.0f a%d blk%d,%d) A(%.0f,%.0f,y%.0f a%d blk%d,%d) B(%.0f,%.0f,y%.0f a%d blk%d,%d)" % (
            i, s["host"][i]["x"], s["host"][i]["z"], s["host"][i]["y"], s["host"][i]["act"], s["host"][i]["bx"], s["host"][i]["bz"],
            s["A"][i]["x"], s["A"][i]["z"], s["A"][i]["y"], s["A"][i]["act"], s["A"][i]["bx"], s["A"][i]["bz"],
            s["B"][i]["x"], s["B"][i]["z"], s["B"][i]["y"], s["B"][i]["act"], s["B"][i]["bx"], s["B"][i]["bz"]))
    return s


def marks(R):
    return {w: len(R.log(w)) for w in ("host", "A", "B")}


class Rig(C.Rig):
    def start_host(self):
        env = {"AC_TEST_AUTOPILOT": "ac_auto_host.txt", "AC_TEST_WILDLIFE_REROLL": "80", "AC_TEST_WILDLIFE_TRACE_FRAMES": "60", "AC_TEST_PROXY_DIAG": "1"}
        self.host = L.HostProcess(port=self.args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=env, log_path=os.path.join(HERE, "fishpos_host.log"), bin_dir=C.W.BIN).start()
        ok = self.host.wait_listening(60) and self.host.boot_to_field(timeout=90, slot=0)
        if ok:
            L.resolve_host_town("127.0.0.1", self.args.port)
            self.players["host"] = C.W.Player(L, "host", self.host)
        return ok


def fish_acre(R, ent):
    d = T.decisions(R.log("host")).get(ent)
    return (d[2], d[3]) if d else (4, 2)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12970)
    args = ap.parse_args()
    R = Rig(args, [])
    try:
        assert R.start_host()
        assert R.start_client("A", 1) and R.start_client("B", 2)
        ent = T.ensure_creature(R, "A", 0)
        print("fish entity:", ent)
        bx, bz = fish_acre(R, ent)
        for w in ("A", "B"):
            T.enter_acre(R.players[w], bx, bz)
        spawn = re.findall(r"spawn decision entity %d kind 0 species (\d+) acre\((\d+),(\d+)\) pos\(([-\d.]+),([-\d.]+),([-\d.]+)\)" % ent, R.log("host"))
        print("host spawn decision:", spawn[-1] if spawn else None)
        for w in ("A", "B"):
            sp = re.findall(r"client: SPAWN entity %d kind 0 species (\d+) acre\((\d+),(\d+)\) pos\(([-\d.]+),([-\d.]+),([-\d.]+)\)" % ent, R.log(w))
            print("client %s received SPAWN:" % w, sp[-1] if sp else None)
        print("== PRE: nobody fishing, 8 s")
        o = marks(R)
        time.sleep(9)
        compare(R, "PRE", o, ent)

        print("== CATCH: A fishes entity %d, B watches" % ent)
        o = marks(R)
        ans = R.players["A"].cmd("catch fish %d" % ent, 270)
        print("  A autopilot:", ans)
        time.sleep(6)
        s = compare(R, "CATCH", o, ent, show=12)
        for w in ("host", "A", "B"):
            hl = R.log(w)[o[w]:]
            idx = [m.start() for m in re.finditer(r"CATCH (?:entity|request)", hl)]
            last_tr = [m.start() for m in FISH_RX.finditer(hl) if int(m.group(1)) == ent]
            after = [p for p in last_tr if idx and p > idx[0]]
            print("  %s: fish samples after the first CATCH log line: %d ; WILDLIFE_DESPAWN lines: %d" % (w, len(after), len(re.findall(r"WILDLIFE_DESPAWN|DESPAWN entity %d" % ent, hl))))
        hl = R.log("host")[o["host"]:]
        for m in re.finditer(r"\[BOBBER\] host: peer \d+ bobber event \d entity %d[^\n]*" % ent, hl):
            print("  host:", m.group(0)[:160])

        print("== TWO anglers: A and B both fish a new fish")
        ent2 = T.ensure_creature(R, "A", 0)
        bx, bz = fish_acre(R, ent2)
        for w in ("A", "B"):
            T.enter_acre(R.players[w], bx, bz)
        print("  fish entity:", ent2)
        o = marks(R)
        ca, cb = R.players["A"].send("catch fish %d" % ent2), R.players["B"].send("catch fish %d" % ent2)
        ra, rb = R.players["A"].wait(ca, 300), R.players["B"].wait(cb, 300)
        print("  A:", ra, "| B:", rb)
        time.sleep(5)
        compare(R, "TWO", o, ent2, show=12)
        hl = R.log("host")[o["host"]:]
        for m in re.finditer(r"\[BOBBER\] host: peer (\d+) bobber event (\d) entity %d" % ent2, hl):
            print("  host bobber event: peer %s ev %s" % (m.group(1), m.group(2)))
        print("  host PROXY-DIAG alloc/release during TWO:", re.findall(r"PROXY-DIAG\] (?:alloc|release) slot \d+ (?:for|of) peer \d+", hl))

        print("== KILL: A hooks a fish, then its process dies")
        ent3 = T.ensure_creature(R, "B", 0)
        bx, bz = fish_acre(R, ent3)
        for w in ("A", "B"):
            T.enter_acre(R.players[w], bx, bz)
        print("  fish entity:", ent3)
        o = marks(R)
        R.players["A"].send("catch fish %d" % ent3)
        m = R.host.wait_for_log(r"\[BOBBER\] host: peer \d+ bobber event 1 entity %d" % ent3, 150, since_offset=o["host"])
        print("  fish tied to A's bobber on the host:", bool(m))
        t0 = len(R.log("host")), len(R.log("B"))
        R.clients["A"].proc.kill()
        time.sleep(16)
        hs = [(s_["x"], s_["z"], s_["act"]) for s_ in samples(R, "host", t0[0], ent3)]
        bs = [(s_["x"], s_["z"], s_["act"]) for s_ in samples(R, "B", t0[1], ent3)]
        print("  after A died: host samples (x,z,action):", [(round(a), round(b), c) for a, b, c in hs[:16]])
        print("  after A died: B samples:", [(round(a), round(b), c) for a, b, c in bs[:16]])
        print("  host PROXY-DIAG after kill:", re.findall(r"PROXY-DIAG\] (?:release slot|fish of slot)[^\n]*", R.log("host")[t0[0]:])[:3])
    finally:
        R.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
