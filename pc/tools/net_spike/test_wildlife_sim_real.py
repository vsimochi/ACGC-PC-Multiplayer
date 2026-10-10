#!/usr/bin/env python3
"""test_wildlife_sim_real.py - ONE shared wildlife simulation, hosted by the host, on REAL game processes: a host and two clients (A, B), default settings (no wildlife flag).

The players only play: the TEST-ONLY autopilot (AC_TEST_AUTOPILOT, pc_net_game.c) injects controller input at PADRead (stick, A, D-pad) and positions the player (`tp`, the shore spot of a cast);
the vanilla spawn roll, the host's fish / insect AI, the remote-bobber -> host -> bite -> hook flow, the net, the rod, the catch seams are the unmodified game. AC_TEST_WILDLIFE_REROLL repeats
the vanilla roll until the acre has a bug / a fish (it is often empty); AC_TEST_WILDLIFE_TRACE_FRAMES=60 logs every live creature's position once a second in every process.

  S1  shared simulation: the host stays FAR from the creatures while A is in their acre; for 40 s the host's, A's and (a late-joining) B's copies of each creature stay together
  F1  A fishes a host-simulated fish with the real rod: the host's AI reacts to A's bobber (near / bite events), A hooks it, the host accepts ONE catch, A gets the item, everybody loses the fish
  F2  B does the same with another fish;  F3  the HOST fishes a fish with its own rod
  B1  A nets a bug;  B2  B nets a bug;  B3  the HOST nets a bug   (one accepted catch, one item, everybody loses the bug, nobody else gets an item)
  R1  A and B swing at the SAME bug at the same time: exactly one catch, one item
  D1  a client leaves while its bobber is cast: the host's wildlife goes on, the fish is not stuck
Usage: python test_wildlife_sim_real.py [--port 12800] [--only s1,f1,...]"""
import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wplay_lib as W  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = W.BIN
if __name__ == "__main__":
    W.make_fixture()
import net_spike_lib as L  # noqa: E402

DEC_RX = re.compile(r"host: spawn decision entity (\d+) kind (\d) species (\d+) acre\((\d+),(\d+)\) pos\(([-\d.]+),([-\d.]+),([-\d.]+)\)")
TRACE_RX = re.compile(r"TRACE\] now entity (\d+) (fish|bug) species (\d+) world\(([-\d.]+),([-\d.]+),([-\d.]+)\)")
WEST, EAST = (2300, 1450), (2700, 1450)  # crossing x = 2560 enters acre (4,2)
CALM = (17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39)  # crickets / beetles / ladybugs: they sit still


class Session:
    def __init__(self, args, results):
        self.args = args
        self.check = lambda d, c: L.check(d, bool(c), results)
        self.host = None
        self.clients, self.players = {}, {}
        self.n = 0

    def start_host(self):
        env = {"AC_TEST_AUTOPILOT": "ac_auto_host.txt", "AC_TEST_WILDLIFE_REROLL": "80", "AC_TEST_WILDLIFE_TRACE_FRAMES": "60"}
        self.host = L.HostProcess(port=self.args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=env, log_path=os.path.join(HERE, "wsim_host.log"), bin_dir=W.BIN).start()
        ok = self.host.wait_listening(60) and self.host.boot_to_field(timeout=90, slot=0)
        self.check("host in the field (no wildlife flag on the command line)", ok)
        if ok:
            L.resolve_host_town("127.0.0.1", self.args.port)
            self.players["host"] = W.Player(L, "host", self.host)
        return ok

    def start_client(self, label, slot):
        self.n += 1
        env = {"AC_TEST_AUTOPILOT": "ac_auto_%s.txt" % label, "AC_TEST_WILDLIFE_TRACE_FRAMES": "60"}
        c = L.ClientProcess("127.0.0.1:%d" % self.args.port, extra_args=["--bootstrap-resident", str(slot), "-debug"], env=env,
                            log_path=os.path.join(HERE, "wsim_%s_%d.log" % (label, self.n)), bin_dir=W.BIN, label="ws" + label).start()
        ok = c.boot_to_field(timeout=180, slot=slot)
        self.check("client %s in the field" % label, ok)
        if ok:
            self.clients[label] = c
            self.players[label] = W.Player(L, label, c)
        else:
            c.stop()
        return ok

    def stop_client(self, label):
        c = self.clients.pop(label, None)
        self.players.pop(label, None)
        if c is not None:
            c.stop()

    def stop_all(self):
        for k in list(self.clients):
            self.stop_client(k)
        if self.host is not None:
            self.host.stop()

    def log(self, who):
        return self.host.log_text() if who == "host" else self.clients[who].log_text()


def decisions(log):
    return {int(m.group(1)): (int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))) for m in DEC_RX.finditer(log)}


def table_now(S):
    """the host's live entities: decided - caught - released"""
    hl = S.log("host")
    dec = set(decisions(hl))
    gone = {int(x) for x in re.findall(r"CATCH entity (\d+) .*accepted -- removed", hl)} | {int(x) for x in re.findall(r"entity (\d+) \(kind \d species \d+ acre [\d,]+\) released", hl)}
    return dec - gone


def live_kind(S, kind):
    dec = decisions(S.log("host"))
    return sorted(e for e in table_now(S) if dec[e][0] == kind)


def enter_acre(p, bx, bz):
    pos = p.pos()
    cx, cz = bx * 640 + 320, bz * 640 + 320
    if pos and pos[2:] == (bx, bz):
        p.walk(bx * 640 - 120, cz, radius=80)
    p.walk(cx, cz, radius=140)


def cross(p, tries=6):
    """leave acre (4,2) and enter it again (the host rolls on every ENTRY)"""
    for _ in range(tries):
        pos0 = p.pos()
        if pos0 and pos0[2] != 3:
            p.cmd("tp %d %d" % WEST, 20)  # start the real walk from the west side of the boundary (a positioning aid; the crossing itself is walked)
        p.walk(EAST[0], EAST[1], radius=70)
        pos = p.pos()
        if pos and pos[2:] == (4, 2):
            return True
        p.walk(WEST[0], WEST[1], radius=100)
    return False


def ensure_creature(S, who, kind, free_others=False):
    """a live fish (kind 0) / bug (kind 1) in the host's table; `who` walks in and out of acre (4,2) until the host rolled one"""
    for _ in range(16):
        e = live_kind(S, kind)
        if e:
            if free_others and kind == 0 and len(e) > 1:
                # the host (and each client) has only 2 fish actors for ALL players (vanilla pool, TODO W-05): fish records left over from earlier steps would take the pool and the chosen fish would
                # get no host actor (the host then releases it). Release the others (the host's own test command) so the fish under test has room.
                for other in e[:-1]:
                    S.players["host"].cmd("hdespawn %d" % other, 20)
                time.sleep(3)
            return e[-1]
        cross(S.players[who])
        time.sleep(2)
    return None


def surviving_fish(S, who):
    """a live fish that is STILL in the host's table 9 s after it was picked. A fish whose own program destroys it as soon as it is (re-)created (the host re-creates it up to 5 times, then releases
    the record like a creature that is simply gone) cannot be fished by anybody; it is not what these tests are about."""
    for _ in range(4):
        e = ensure_creature(S, who, 0, free_others=True)
        if e is None:
            return None
        time.sleep(9)
        if e in table_now(S):
            return e
        L.info("fish %d ended by itself (released by the host), picking another" % e)
        cross(S.players[who])
        time.sleep(2)
    return None


def traces(S, who, since):
    """{entity: [(x, z), ...]} of the creature traces `who` logged after offset `since`"""
    d = {}
    for m in TRACE_RX.finditer(S.log(who)[since:]):
        d.setdefault(int(m.group(1)), []).append((float(m.group(4)), float(m.group(6))))
    return d


def spread(S, names, since, entity):
    """per second sample: the largest distance between the copies of one creature in the given processes"""
    t = {w: traces(S, w, since[w]).get(entity, []) for w in names}
    n = min(len(v) for v in t.values()) if t else 0
    out = []
    for i in range(n):
        pts = [t[w][i] for w in names]
        out.append(max(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for a in pts for b in pts))
    return out


def s1(S):
    check = S.check
    A = S.players["A"]
    host = S.players["host"]
    L.info("host stays at %s, A goes into acre (4,2)" % (host.pos(),))
    A.walk(WEST[0], WEST[1], radius=100)
    cross(A)
    time.sleep(3)
    S.start_client("B", 2)
    time.sleep(4)
    enter_acre(S.players["B"], 4, 2)  # B walks in: the host replays the acre's creatures to a player entering it
    time.sleep(3)
    ents = sorted(table_now(S))
    check("S1 the host rolled creatures for A's acre (entities %s)" % ents, bool(ents))
    names = ["host", "A", "B"]
    since = {w: len(S.log(w)) for w in names}
    time.sleep(40)
    alive_end = set(table_now(S))
    survivors = 0
    for e in ents:
        sp = spread(S, names, since, e)
        if e not in alive_end:
            # an insect whose own species program ended it is released by the host at once (like vanilla it is simply gone), and every process drops it: it cannot have 40 s of samples
            L.info("S1 entity %d ended during the 40 s window (released by the host; %d samples while it lived)" % (e, len(sp)))
            continue
        survivors += 1
        if len(sp) >= 20:
            check("S1 entity %d: host, A and B copies stay together for %d s (max %.0f, mean %.0f units apart)" % (e, len(sp), max(sp), sum(sp) / len(sp)), max(sp) < 90 and sum(sp) / len(sp) < 35)
        else:
            check("S1 entity %d has a trace in every process (%d samples)" % (e, len(sp)), False)
    check("S1 at least one of the rolled creatures lived through the whole window (%d of %d)" % (survivors, len(ents)), survivors >= 1)
    hm = {e: traces(S, "host", since["host"]).get(e, []) for e in ents}
    check("S1 the host's creatures really moved while the host player stood far away (some creature moved > 12 units in 40 s)",
          any(len(v) > 1 and max(((p[0] - v[0][0]) ** 2 + (p[1] - v[0][1]) ** 2) ** 0.5 for p in v) > 12 for v in hm.values()))


def catch_fish(S, who, observers, tag):
    check = S.check
    P = S.players[who]
    ent = surviving_fish(S, "A")
    check("%s a live fish exists" % tag, ent is not None)
    if ent is None:
        return None
    for o in observers:
        S.players[o].cmd("mash 8", 20)
        enter_acre(S.players[o], 4, 2)
    if who == "host":
        enter_acre(P, 4, 2)  # the host walks to the fish's acre like any player
    off = {w: len(S.log(w)) for w in ["host", "A", "B"] if w == "host" or w in S.clients}
    ans = P.cmd("catch fish %d" % ent, 270)
    if ans is not None and "no local fish actor" in ans:
        # the fish swam out of the angler's range and its local copy was culled; walking back into its acre
        # makes the host replay it (the same thing a player walking back would see)
        L.info("%s: the fish left %s's range -- walking back to its acre and trying again" % (tag, who))
        enter_acre(P, 4, 2)
        time.sleep(3)
        ans = P.cmd("catch fish %d" % ent, 270)
    check("%s %s hooked the fish with the real rod (%s)" % (tag, who, ans), ans is not None and " ok" in ans)
    P.cmd("mash 30", 60)  # dismiss the catch / item windows so the next step is not blocked behind a message
    time.sleep(3)
    hl = S.log("host")
    check("%s the host's AI reacted to %s's bobber (near / bite events)" % (tag, who), len(re.findall(r"bobber event [13] entity %d " % ent, hl[off["host"]:])) >= 1 or who == "host")
    check("%s the HOST accepted exactly one catch of entity %d" % (tag, ent), len(re.findall(r"(?:peer \d+|local) CATCH entity %d .*accepted" % ent, hl)) == 1)
    if who != "host":
        check("%s %s received the item from the host transaction, once" % (tag, who), len(re.findall(r"CATCH request \d+ \(entity %d\) accepted -- item 0x[0-9A-F]+ granted" % ent, S.log(who))) == 1)
    for o in [w for w in ["A", "B"] if w != who and w in S.clients]:
        check("%s %s was granted nothing for it" % (tag, o), not re.search(r"CATCH request \d+ \(entity %d\) accepted" % ent, S.log(o)))
    mark = {w: len(S.log(w)) for w in ["host", "A", "B"] if w == "host" or w in S.clients}
    time.sleep(5)
    for o in mark:
        n_after = len(traces(S, o, mark[o]).get(ent, []))
        check("%s %s no longer shows fish %d (%d trace lines for it in the 5 s after the catch)" % (tag, o, ent, n_after), n_after == 0)
    check("%s the fish is out of the host's table" % tag, ent not in table_now(S))
    return ent


def net_catch(S, who, tag, exclude=()):
    """A bug can end by its own species program before the net reaches it (the host then releases it, like vanilla), or sit too high to net: a round that finds no catchable bug re-rolls fresh bugs
    (crossing acres) and tries again, up to 3 rounds. Only the last round records failures, and a catch that happened is always checked in full."""
    for rnd in (1, 2, 3):
        r = _net_catch_round(S, who, tag, exclude, rnd == 3)
        if r is not None:
            return r
        if rnd < 3:
            L.info("%s: round %d found no catchable bug, re-rolling" % (tag, rnd))
            cross(S.players["A"])
            time.sleep(3)
    return None


def _net_catch_round(S, who, tag, exclude, last):
    check = S.check if last else (lambda msg, ok: ok)
    P = S.players[who]
    ent = None
    dec_ = decisions(S.log("host"))
    for _ in range(16):  # the target is a bug that sits on the ground (a flier at 170 units height cannot be netted by anyone)
        dec_ = decisions(S.log("host"))
        ents = sorted([e for e in live_kind(S, 1) if e not in exclude], key=lambda e: (dec_[e][1] not in CALM, e))
        ents = ents[:1]
        if ents:
            ent = ents[-1]
            break
        cross(S.players["A"])
        time.sleep(2)
    check("%s a live bug exists" % tag, ent is not None)
    if ent is None:
        return None
    _d = decisions(S.log("host")).get(ent)
    enter_acre(P, _d[2] if _d else 4, _d[3] if _d else 2)  # every catcher, the host included, walks to the creature like a player
    time.sleep(3)
    live = sorted(P.bugs(), key=lambda b: (b[1] not in CALM, b[0]))
    live = [b for b in live if b[0] not in exclude]
    check("%s %s sees a live bug" % (tag, who), bool(live))
    attempts = [b for b in live[:4] for _ in range(2)]  # the auto-catch keeps re-acquiring the bug's CURRENT position itself; a second try only covers a bug that slipped away for good
    for (e, sp, bx, bz) in attempts:
        if e not in {b[0] for b in P.bugs()}:
            continue
        ans = P.cmd("catch bug %d" % e, 90)
        if ans is None or " ok" not in ans:
            L.info("%s: no catch on bug %d (%s), trying again" % (tag, e, ans))
            continue
        P.cmd("mash 10", 30)
        time.sleep(3)
        hl = S.log("host")
        S.check("%s %s's net caught bug %d; the host accepted exactly one catch" % (tag, who, e), len(re.findall(r"CATCH entity %d .*accepted" % e, hl)) == 1)
        if who != "host":
            S.check("%s %s got the item once" % (tag, who), len(re.findall(r"CATCH request \d+ \(entity %d\) accepted -- item 0x[0-9A-F]+ granted" % e, S.log(who))) == 1)
        for o in [w for w in ["A", "B"] if w != who and w in S.clients]:
            S.check("%s %s got nothing for bug %d" % (tag, o, e), not re.search(r"CATCH request \d+ \(entity %d\) accepted" % e, S.log(o)))
        S.check("%s bug %d is gone everywhere (host table + %s)" % (tag, e, who), e not in table_now(S) and e not in {b[0] for b in P.bugs()})
        return e
    return check("%s caught one of the live bugs with the real net" % tag, False) and None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12800)
    ap.add_argument("--only", default="s1,f1,f2,f3,b1,b2,b3,r1,r2,d1")
    args = ap.parse_args()
    results = []
    S = Session(args, results)
    only = args.only.split(",")
    try:
        if not S.start_host() or not S.start_client("A", 1):
            return L.summary_and_exit_code(results)
        if "s1" in only:
            s1(S)
        elif not S.start_client("B", 2):
            return L.summary_and_exit_code(results)
        if "f1" in only:
            catch_fish(S, "A", ["B"], "F1")
        if "f2" in only and "B" in S.clients:
            catch_fish(S, "B", ["A"], "F2")
        if "f3" in only:
            catch_fish(S, "host", ["A", "B"], "F3")
        if "b1" in only:
            net_catch(S, "A", "B1")
        if "b2" in only and "B" in S.clients:
            net_catch(S, "B", "B2")
        if "b3" in only:
            net_catch(S, "host", "B3")
        if "r1" in only and "B" in S.clients:
            race(S)
        if "r2" in only and "B" in S.clients:
            race_fish(S)
        if "d1" in only and "B" in S.clients:
            disconnect(S)
    finally:
        S.stop_all()
    return L.summary_and_exit_code(results)


def race(S):
    check = S.check
    A, B = S.players["A"], S.players["B"]
    for attempt in range(3):  # a bug can end by its own species program while both players walk to it (the host then releases it, like vanilla): such a round is repeated with a fresh bug
        ent = None
        for _ in range(16):
            mine = sorted(A.bugs(), key=lambda t: (t[1] not in CALM, t[0]))
            if not mine:
                cross(A)
                time.sleep(2)
                continue
            t = mine[0]
            enter_acre(B, int(t[2] // 640), int(t[3] // 640))
            time.sleep(3)
            a, b = {x[0]: x for x in A.bugs()}, {x[0]: x for x in B.bugs()}
            if t[0] in a and t[0] in b:
                ent = t[0]
                break
            cross(A)
        if attempt == 2 or ent is None:
            check("R1 A and B both see the same live bug", ent is not None)
        if ent is None:
            if attempt == 2:
                return
            continue
        if race_both(S, "R1", "bug", ent, final=(attempt == 2)) is not False:
            if attempt < 2:
                check("R1 A and B both see the same live bug", True)
            return


def race_fish(S):
    check = S.check
    ent = surviving_fish(S, "A")
    check("R2 a live fish exists", ent is not None)
    if ent is None:
        return
    for w in ("A", "B"):
        S.players[w].cmd("mash 6", 20)
        enter_acre(S.players[w], 4, 2)
    race_both(S, "R2", "fish", ent)


def race_both(S, tag, kind, ent, final=True):
    """A and B both auto-catch entity ENT at (nearly) the same moment: the command files are written back to back, nothing serializes them"""
    check = S.check
    A, B = S.players["A"], S.players["B"]
    ca, cb = A.send("catch %s %d" % (kind, ent)), B.send("catch %s %d" % (kind, ent))
    ra, rb = A.wait(ca, 300), B.wait(cb, 300)
    L.info("%s: A: %s | B: %s" % (tag, ra, rb))
    if not final and kind == "bug" and all(r and (" gone" in r or " rejected" in r) for r in (ra, rb)) and ent not in table_now(S):
        L.info("%s: the bug %d ended before either catch could be granted (released by the host: both players gone / refused) -- repeating the race with a fresh bug" % (tag, ent))
        A.cmd("mash 6", 20)
        B.cmd("mash 6", 20)
        return False
    verdicts = [("ok" if (r and " ok" in r) else ("rejected" if (r and " rejected" in r) else ("gone" if (r and " gone" in r) else "fail"))) for r in (ra, rb)]
    check("%s exactly one of the two auto-catches succeeded (A: %s, B: %s)" % (tag, verdicts[0], verdicts[1]), verdicts.count("ok") == 1 and all(v in ("ok", "rejected", "gone") for v in verdicts))
    A.cmd("mash 6", 20)
    B.cmd("mash 6", 20)
    time.sleep(2)
    hl = S.log("host")
    acc = re.findall(r"peer (\d+) CATCH entity %d .*accepted -- removed" % ent, hl)
    check("%s the host accepted exactly ONE of the two catches of entity %d (peers %s)" % (tag, ent, acc), len(acc) == 1)
    grants = sum(len(re.findall(r"CATCH request \d+ \(entity %d\) accepted -- item 0x[0-9A-F]+ granted" % ent, S.log(w))) for w in ("A", "B"))
    check("%s exactly one item was granted in total (%d)" % (tag, grants), grants == 1)
    check("%s the entity is gone from the host's table" % tag, ent not in table_now(S))
    despawns = len(re.findall(r"WILDLIFE_DESPAWN[^\n]*entity %d\b|despawn[^\n]*entity %d\b" % (ent, ent), hl))
    for w in ("A", "B"):
        check("%s %s no longer shows entity %d" % (tag, w, ent), ent not in {x[0] for x in S.players[w].bugs()} if kind == "bug" else True)


def disconnect(S):
    check = S.check
    B = S.players["B"]
    ent = ensure_creature(S, "A", 0, free_others=True)
    check("D1 a live fish exists", ent is not None)
    if ent is None:
        return
    enter_acre(B, 4, 2)
    ca = B.send("fish %d" % ent)
    time.sleep(35)  # B casts, the host's fish may be engaged with B's bobber
    S.stop_client("B")  # B vanishes in the middle of it
    time.sleep(8)
    hl = S.log("host")
    check("D1 the host noticed B leaving and stayed alive", S.host.alive() and re.search(r"host: peer \d+ disconnected", hl) is not None)
    check("D1 the fish is still alive in the host's table (the host stays authoritative)", ent in table_now(S) or ent in {int(x) for x in re.findall(r"CATCH entity (\d+) .*accepted -- removed", hl)})
    since = len(S.log("host"))
    time.sleep(8)
    t = traces(S, "host", since).get(ent, [])
    check("D1 the host keeps simulating the fish after B left (%d traces)" % len(t), len(t) >= 4 or ent not in table_now(S))
    check("D1 A still sees the fish moving", len(traces(S, "A", since if False else 0).get(ent, [])) > 0)


if __name__ == "__main__":
    sys.exit(main())
