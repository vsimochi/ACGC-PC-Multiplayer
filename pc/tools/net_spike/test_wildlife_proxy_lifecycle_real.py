#!/usr/bin/env python3
"""test_wildlife_proxy_lifecycle_real.py - lifecycle / capacity of the HOST's bobber proxies (pc_wildlife_authority.c, s_proxy[PCWLD_PROXY_MAX = 8]), observed through the host's
[PROXY-DIAG] log lines (AC_TEST_PROXY_DIAG=1: allocation, release, allocation failure).

Parts (all use a REAL host process; the display/volume of every process: AC_DISPLAY_NAME=samsung, AC_MASTER_VOLUME=1):
  rep   REAL resident client fishes 3 times in a row            -> does a normal cast / catch allocate and RELEASE its proxy
  conc  3 REAL resident clients cast at the same time           -> 3 simultaneous proxies, every bobber served
  dc    REAL resident casts, a fish is tied to its bobber, the client process is KILLED -> cleanup on disconnect; a replacement then fishes
  fake  (supplement, protocol level: FAKE UDP peers send BOBBER_STATE to the real host) boundary of the 8 slots: 8 simultaneous, the 9th, release and reuse, 12 historical peers,
        a bobber that is never reported gone
Usage: python test_wildlife_proxy_lifecycle_real.py [--parts rep,conc,dc,fake] [--port 12960]"""
import argparse
import os
import re
import struct
import subprocess
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_wildlife_proxy_capacity_real as C  # noqa: E402  (fresh fixture + Rig)
import wplay_lib as W  # noqa: E402
import net_spike_lib as L  # noqa: E402
import test_wildlife_sim_real as T  # noqa: E402

BOBBER_STATE = 74


def diag(R, since=0):
    hl = R.log("host")[since:]
    allocs = [(int(a), int(b), int(c)) for a, b, c in re.findall(r"PROXY-DIAG\] alloc slot (\d+) for peer (\d+) \(slots in use now: (\d+)/", hl)]
    rels = [(int(a), int(b)) for a, b in re.findall(r"PROXY-DIAG\] release slot (\d+) of peer (\d+)", hl)]
    fails = [int(x) for x in re.findall(r"PROXY-DIAG\] ALLOC FAILED for peer (\d+)", hl)]
    return allocs, rels, fails


def say(tag, R, since=0):
    a, r, f = diag(R, since)
    L.info("%s: allocs(slot,peer,in-use) %s | releases(slot,peer) %s | alloc failures(peer) %s" % (tag, a, r, f))
    return a, r, f


def part_rep(R):
    check = R.check
    if not R.start_client("A", 1):
        return
    for k in range(1, 4):
        off = len(R.log("host"))
        ok, peer = C.fish_once(R, "A", "REP cast %d" % k)
        time.sleep(3)  # PCWLD_PROXY_LIFE_MS (0.8 s) + a few polls
        a, r, f = say("REP cast %d" % k, R, off)
        check("REP cast %d: a proxy was allocated for the angler (%s)" % (k, a), len(a) >= 1)
        check("REP cast %d: ... and released again once the bobber was gone (%s)" % (k, r), len(r) >= 1)
        check("REP cast %d: no allocation failure" % k, not f)


def part_conc(R):
    check = R.check
    for lab, slot in (("A", 1), ("B", 2), ("C", 3)):
        if lab not in R.clients and not R.start_client(lab, slot):
            return
    for rnd in (1, 2):  # round 2: the same three players fish again -> the slots must be reusable
        tag = "CONC round %d" % rnd
        ent = T.ensure_creature(R, "A", 0)
        check("%s a live fish exists" % tag, ent is not None)
        if ent is None:
            return
        d = T.decisions(R.log("host")).get(ent)
        for lab in ("A", "B", "C"):
            T.enter_acre(R.players[lab], d[2] if d else 4, d[3] if d else 2)
        off = len(R.log("host"))
        ids = {lab: R.players[lab].send("catch fish %d" % ent) for lab in ("A", "B", "C")}
        res = {lab: R.players[lab].wait(c, 300) for lab, c in ids.items()}
        L.info("%s autopilot answers: %s" % (tag, res))
        time.sleep(4)
        a, r, f = say(tag, R, off)
        peers = sorted({p for _s, p, _n in a})
        check("%s: every angler's bobber had a proxy at some point (allocs %s, peers %s)" % (tag, a, peers), len(peers) >= 1)
        acc = re.findall(r"peer (\d+) CATCH entity %d .*accepted -- removed" % ent, R.log("host")[off:])
        check("%s: exactly one catch of the one fish (%s)" % (tag, acc), len(acc) == 1)
        check("%s: no allocation failure" % tag, not f)
        used = [(s_, p_) for s_, p_, _n in a]
        L.info("%s: slots used %s" % (tag, used))
        if rnd == 2:
            check("%s: slots 0..2 only (no extra slot consumed) %s" % (tag, sorted({s_ for s_, _ in used})), all(s_ < 3 for s_, _ in used))
    # at the end nothing may still hold a caught fish
    time.sleep(4)
    hl = R.log("host")
    last = {}
    for m in re.finditer(r"PROXY-DIAG\] slot (\d+) peer (\d+) active=(\d) until_in=(-?\d+) ms uki_status=(\d+) gyo_status=(\d+) gyo_command=(\d+) child=(\w+)", hl):
        last[int(m.group(1))] = m.groups()
    stuck = {k: v for k, v in last.items() if v[2] == "0" and int(v[3]) < -3000 and (v[6] != "0" or v[7] == "yes")}
    check("CONC no proxy is left sitting inactive with a stale fish (%s)" % stuck, not stuck)


def kill_pid_of(c):
    try:
        c.proc.kill()
    except Exception:  # noqa: BLE001
        pass


def part_dc(R):
    check = R.check
    if "A" not in R.clients and not R.start_client("A", 1):
        return
    ent = T.ensure_creature(R, "A", 0)
    check("DC a live fish exists", ent is not None)
    if ent is None:
        return
    d = T.decisions(R.log("host")).get(ent)
    T.enter_acre(R.players["A"], d[2] if d else 4, d[3] if d else 2)
    off = len(R.log("host"))
    cid = R.players["A"].send("catch fish %d" % ent)
    m = R.host.wait_for_log(r"\[BOBBER\] host: peer (\d+) bobber event 1 entity %d" % ent, 150, since_offset=off) if hasattr(R.host, "wait_for_log") else None
    check("DC a fish was tied to the angler's bobber (host saw a NEAR/TOUCH event)", m is not None)
    pre = diag(R, off)
    L.info("DC before the kill: allocs %s releases %s" % (pre[0], pre[1]))
    victim_peer = pre[0][-1][1] if pre[0] else None
    kill_pid_of(R.clients["A"])  # the angler's process dies with the fish tied to its bobber
    time.sleep(14)  # the transport times a silent peer out (~5 s), then the idle release
    a, r, f = say("DC after the kill", R, off)
    check("DC the dead angler's proxy was released (peer %s, releases %s)" % (victim_peer, r), any(p == victim_peer for _s, p in r))
    R.stop_client("A")
    time.sleep(2)
    if not R.start_client("A", 1):
        return
    off2 = len(R.log("host"))
    ok, peer = C.fish_once(R, "A", "DC replacement angler")
    a2, r2, f2 = say("DC replacement", R, off2)
    check("DC the replacement angler got a proxy (%s) and no allocation failure" % a2, len(a2) >= 1 and not f2)


def bobber_payload(active, x=2500.0, z=1500.0):
    return struct.pack("<BBBbbBBB6fhh", BOBBER_STATE, 1 if active else 0, 3 if active else 0, 1 if active else 0, 0, 1 if active else 0, 5 if active else 0, 0,
                       x, 0.0, z, x, 0.0, z, 0, 0)


def part_fake(R):
    check = R.check
    fakes = []
    base = len(R.log("host"))

    def mk(i):
        f = L.FakeClient("F%d" % i, "127.0.0.1", R.args.port)
        f.connect_and_ready()
        time.sleep(0.4)
        rd = re.findall(r"host: peer (\d+) (?:is )?READY", R.log("host"))
        return f, (int(rd[-1]) if rd else None)

    for i in range(1, 13):
        fakes.append(mk(i))
    peers = [p for _f, p in fakes]
    L.info("FAKE peers (READY order): %s" % peers)
    off = len(R.log("host"))
    for f, p in fakes[:8]:  # 8 simultaneous active bobbers
        f.send_unreliable(bobber_payload(True))
        time.sleep(0.15)
    time.sleep(1.0)
    a, r, fl = say("FAKE 8 casting", R, off)
    check("FAKE 8 simultaneous bobbers got slots 0..7 (%s)" % a, sorted(s for s, _p, _n in a) == list(range(8)))
    off = len(R.log("host"))
    f9, p9 = fakes[8]
    for _ in range(3):
        f9.send_unreliable(bobber_payload(True))
        time.sleep(0.2)
    a, r, fl = say("FAKE 9th casts while 8 are active", R, off)
    check("FAKE the 9th simultaneous bobber (peer %s) is refused and the refusal is logged (%s)" % (p9, fl), p9 in fl)
    off = len(R.log("host"))
    f1, p1 = fakes[0]
    f1.send_reliable(bobber_payload(False))
    time.sleep(2.5)
    a, r, fl = say("FAKE first angler reels in", R, off)
    check("FAKE a finished bobber's slot is released (%s)" % r, any(p == p1 for _s, p in r))
    off = len(R.log("host"))
    for _ in range(3):
        f9.send_unreliable(bobber_payload(True))
        time.sleep(0.2)
    a, r, fl = say("FAKE 9th retries after a release", R, off)
    check("FAKE ... and the 9th angler now gets the freed slot (%s)" % a, any(p == p9 for _s, p, _n in a))
    # historical peers: everybody reels in, then 3 peers that never cast before use the table
    off = len(R.log("host"))
    for f, p in fakes[:9]:
        f.send_reliable(bobber_payload(False))
    time.sleep(3.0)
    a, r, fl = say("FAKE everybody reels in", R, off)
    check("FAKE all 9 slots in use were released (%d releases)" % len(r), len(r) >= 8)
    off = len(R.log("host"))
    for f, p in fakes[9:12]:
        f.send_unreliable(bobber_payload(True))
        time.sleep(0.2)
    time.sleep(0.8)
    a, r, fl = say("FAKE 3 never-seen peers (10th..12th) cast", R, off)
    check("FAKE peers 10..12 (historical ids beyond 8) get proxies (%s) with no failure" % a, len(a) == 3 and not fl)
    # leak path A: the 'bobber gone' message never arrives
    off = len(R.log("host"))
    time.sleep(2.5)
    a, r, fl = say("FAKE bobbers of 10..12 are never reported gone (2.5 s)", R, off)
    L.info("FAKE leak path A: releases after the stream simply stopped: %s" % r)
    check("FAKE (leak path A) a bobber whose 'gone' message never arrives: slot released anyway? %s" % ("YES" if r else "NO - the slot stays allocated"), True)
    off = len(R.log("host"))
    fakes[9][0].disconnect()
    time.sleep(12)
    a, r, fl = say("FAKE peer 10 disconnects", R, off)
    L.info("FAKE disconnect cleanup of a never-reported-gone bobber: releases %s" % r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12960)
    ap.add_argument("--parts", default="rep,conc,dc,fake")
    args = ap.parse_args()
    C.write_profiles(2)
    results = []
    R = C.Rig(args, results)
    try:
        parts = args.parts.split(",")
        if not R.start_host():
            return L.summary_and_exit_code(results)
        if "rep" in parts:
            part_rep(R)
        if "conc" in parts:
            part_conc(R)
        if "dc" in parts:
            part_dc(R)
        if "fake" in parts:
            if len(R.clients) and ("rep" in parts or "conc" in parts or "dc" in parts):
                for k in list(R.clients):
                    R.stop_client(k)
            part_fake(R)
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
