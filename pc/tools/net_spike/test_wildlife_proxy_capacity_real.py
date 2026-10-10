#!/usr/bin/env python3
"""test_wildlife_proxy_capacity_real.py - REAL game processes: does every angler get fish bites, however many different peers have fished before it?

Why: the host keeps one bobber proxy per remote angler (pc_wildlife_authority.c, s_proxy[PCWLD_PROXY_MAX]). An audit suspected the table never releases a slot, so the 9th peer that ever cast
could not get a bite. This drives REAL processes (one host, N REAL guests with the real rod, no packet faking): each guest in turn walks to a fish, casts, waits for the host's bite, hooks it and
gets the host's catch verdict. Guests stay connected, so peer ids 0..N are all in use (id 8 is the host's own). The autopilot only presses buttons / places the player; the cast, the bobber, the
bobber stream, the host fish AI, the bite, the hook and the catch transaction are the game's.

Scenarios (--scenario): seq (default: N guests fish one after another, --casts casts each), churn (guests join, fish and DISCONNECT; the next guest reuses/steps ids), all.
Usage: python test_wildlife_proxy_capacity_real.py [--guests 10] [--casts 1] [--port 12950] [--scenario seq]"""
import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wplay_lib as W  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = W.BIN
W.make_fixture()
import net_spike_lib as L  # noqa: E402
import test_wildlife_sim_real as T  # noqa: E402

ITM_ROD_HEX = "2203"


def write_profiles(n):
    d = os.path.join(W.BIN, "save", "mp")
    os.makedirs(d, exist_ok=True)
    for i in range(1, n + 1):
        with open(os.path.join(d, "guest_g%02d.ini" % i), "wb") as f:
            f.write(("# test profile\nname = Guest%02d\ngender = %d\nface = %d\nhome_town = Hometwn\nplayer_id = 0x%04X\nland_id = %d\n" % (i, i % 2, i % 8, 0x5A00 + i, 25000 + i)).encode("ascii"))


class Rig(T.Session):
    def start_host(self):
        env = {"AC_TEST_AUTOPILOT": "ac_auto_host.txt", "AC_TEST_WILDLIFE_REROLL": "80", "AC_TEST_WILDLIFE_TRACE_FRAMES": "0", "AC_TEST_PROXY_DIAG": "1"}
        self.host = L.HostProcess(port=self.args.port, extra_args=["--bootstrap-resident", "0", "--max-guests", "24", "--max-peers", "24", "-debug"], env=env,
                                  log_path=os.path.join(HERE, "proxycap_host.log"), bin_dir=W.BIN).start()
        ok = self.host.wait_listening(60) and self.host.boot_to_field(timeout=90, slot=0)
        self.check("host in the field", ok)
        if ok:
            L.resolve_host_town("127.0.0.1", self.args.port)
            self.players["host"] = W.Player(L, "host", self.host)
        return ok

    def start_guest(self, i):
        label = "g%02d" % i
        self.n += 1
        env = {"AC_TEST_AUTOPILOT": "ac_auto_%s.txt" % label, "AC_TEST_WILDLIFE_TRACE_FRAMES": "0"}
        c = L.ClientProcess("127.0.0.1:%d" % self.args.port, extra_args=["--guest", "--guest-profile", label, "-debug"], env=env,
                            log_path=os.path.join(HERE, "proxycap_%s_%d.log" % (label, self.n)), bin_dir=W.BIN, label="pc" + label).start()
        self.clients[label] = c
        p = W.Player(L, label, c)
        self.players[label] = p
        deadline = time.time() + 240
        pos = None
        while time.time() < deadline and pos is None:
            if c.proc.poll() is not None:
                break
            ans = p.cmd("pos", 8.0)
            pos = p.pos() if ans else None
        self.check("%s in the field (autopilot answers pos)" % label, pos is not None)
        if pos is None:
            return None
        # a guest arrives by train: the stationmaster's dialogue holds the player (main_index TALK) until A is pressed through it, and the host's first record adoption ("host wins")
        # discards any item given before it -- so dismiss the dialogue first, wait for the adoption, and only then put the rod in the bag
        c.wait_for_log(r"\[NET\]\[REC\] client: adopted rev", 90)
        p.cmd("mash 40", 90)
        return label


def peer_of(host_log, since, label_rx=None):
    """the transport peer id that the most recent READY line of the host log (after offset `since`) names"""
    ids = re.findall(r"host: peer (\d+) (?:is )?READY", host_log[since:])
    return int(ids[-1]) if ids else None


def fish_once(R, label, tag):
    """one cast-bite-hook-catch by `label`; returns (ok, peer_with_bobber_events)"""
    check = R.check
    P = R.players[label]
    off = len(R.log("host"))
    ent = T.ensure_creature(R, label, 0)
    check("%s a live fish exists" % tag, ent is not None)
    if ent is None:
        return False, None
    d = T.decisions(R.log("host")).get(ent)
    T.enter_acre(P, d[2] if d else 4, d[3] if d else 2)
    time.sleep(2)
    ans = P.cmd("catch fish %d" % ent, 270)
    if ans is not None and "no local fish actor" in ans:
        T.enter_acre(P, d[2] if d else 4, d[3] if d else 2)
        time.sleep(3)
        ans = P.cmd("catch fish %d" % ent, 270)
    if ans is not None and "NOT free" in ans:
        P.check_stuck("after the catch of %s" % tag)  # recovers with A presses or raises PlayerStuck with the evidence (instead of stalling the next steps)
    time.sleep(2)
    hl = R.log("host")[off:]
    peers = sorted({int(x) for x in re.findall(r"\[BOBBER\] host: peer (\d+) bobber event \d entity %d " % ent, hl)})
    acc = re.findall(r"peer (\d+) CATCH entity %d .*accepted -- removed" % ent, hl)
    ok = ans is not None and " ok" in ans and len(acc) == 1
    check("%s fish %d: bobber events reached the host AI (peers %s), catch verdict %s, accepted by peer %s (autopilot: %s)" % (tag, ent, peers, "accepted" if acc else "NONE", acc, ans), ok)
    return ok, (peers[0] if peers else None)


def scenario_seq(R, n, casts):
    check = R.check
    labels = []
    for i in range(1, n + 1):
        label = R.start_guest(i)
        if label is None:
            break
        labels.append(label)
        P = R.players[label]
        P.cmd("give %s" % ITM_ROD_HEX, 20)
        for c in range(casts):
            ok, peer = fish_once(R, label, "S%d %s cast %d" % (i, label, c + 1))
    # a fresh overview: which peers ever produced bobber events
    hl = R.log("host")
    peers = sorted({int(x) for x in re.findall(r"\[BOBBER\] host: peer (\d+) bobber event", hl)})
    ready = sorted({int(x) for x in re.findall(r"host: peer (\d+) (?:is )?READY", hl)})
    L.info("peers that ever reached READY: %s ; peers whose bobber produced host fish-AI events: %s" % (ready, peers))
    proxy_report(R, "SEQ")
    check("every connected guest peer produced host fish-AI events (%d guests, READY peers %s, bobber peers %s)" % (len(labels), ready, peers), len(peers) >= len(labels) and len(labels) == n)


def scenario_churn(R, n):
    check = R.check
    peers = []
    for i in range(1, n + 1):
        label = R.start_guest(i)
        if label is None:
            break
        R.players[label].cmd("give %s" % ITM_ROD_HEX, 20)
        ok, peer = fish_once(R, label, "C%d %s" % (i, label))
        peers.append(peer)
        R.stop_client(label)  # the guest leaves: its peer slot is free again
        time.sleep(3)
    L.info("churn: bobber peer ids per guest: %s" % peers)
    proxy_report(R, "CHURN")
    check("every churned guest got bites (%s)" % peers, all(p is not None for p in peers) and len(peers) == n)


def proxy_report(R, tag):
    hl = R.log("host")
    allocs = re.findall(r"PROXY-DIAG\] alloc slot (\d+) for peer (\d+) \(slots in use now: (\d+)/", hl)
    rels = re.findall(r"PROXY-DIAG\] release slot (\d+) of peer (\d+)", hl)
    fails = re.findall(r"PROXY-DIAG\] ALLOC FAILED for peer (\d+)", hl)
    L.info("%s: proxy allocs %s ; releases %s ; alloc failures %s" % (tag, [(int(a), int(b)) for a, b, _ in allocs], [(int(a), int(b)) for a, b in rels], fails))
    R.check("%s: no proxy allocation failure" % tag, not fails)
    return allocs, rels


def scenario_conc(R):
    """two guests cast at the same fish at the same time: two proxies live at once"""
    check = R.check
    labels = []
    for i in (1, 2):
        lab = R.start_guest(i)
        if lab is None:
            return
        R.players[lab].cmd("give %s" % ITM_ROD_HEX, 20)
        labels.append(lab)
    ent = T.ensure_creature(R, labels[0], 0)
    check("CONC a live fish exists", ent is not None)
    if ent is None:
        return
    d = T.decisions(R.log("host")).get(ent)
    for lab in labels:
        T.enter_acre(R.players[lab], d[2] if d else 4, d[3] if d else 2)
    off = len(R.log("host"))
    cids = [R.players[lab].send("catch fish %d" % ent) for lab in labels]
    res = [R.players[lab].wait(c, 300) for lab, c in zip(labels, cids)]
    L.info("CONC: %s" % res)
    time.sleep(3)
    hl = R.log("host")[off:]
    peers = sorted({int(x) for x in re.findall(r"\[BOBBER\] host: peer (\d+) bobber event", hl)})
    allocs = {int(b) for a, b, _ in re.findall(r"PROXY-DIAG\] alloc slot (\d+) for peer (\d+) \(slots in use now: (\d+)/", hl)}
    check("CONC both guests' bobbers got a proxy at the same time (alloc peers %s)" % sorted(allocs), len(allocs) == 2)
    acc = re.findall(r"peer (\d+) CATCH entity %d .*accepted -- removed" % ent, hl)
    check("CONC exactly one of them caught the fish (accepted by %s)" % acc, len(acc) == 1)
    proxy_report(R, "CONC")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12950)
    ap.add_argument("--guests", type=int, default=10)
    ap.add_argument("--casts", type=int, default=1)
    ap.add_argument("--scenario", default="seq")
    args = ap.parse_args()
    write_profiles(max(args.guests, 12))
    results = []
    R = Rig(args, results)
    try:
        if not R.start_host():
            return L.summary_and_exit_code(results)
        if args.scenario in ("seq", "all"):
            scenario_seq(R, args.guests, args.casts)
        if args.scenario in ("conc", "all"):
            scenario_conc(R)
        if args.scenario in ("churn", "all"):
            scenario_churn(R, args.guests)
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
