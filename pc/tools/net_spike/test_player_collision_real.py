#!/usr/bin/env python3
"""test_player_collision_real.py - M9-B (player<->player collision via a proxy ClObjPipe on remote puppets):
TWO REAL game processes (`--host --bootstrap-resident 0` and `--connect --bootstrap-resident 1`, both with
`--authoritative-wildlife`), no keystrokes, no GUI automation.

Tier labels (honest): every check below is HOOK-DRIVEN REAL GAMEPLAY: the off-by-default test flags
`--collide-test-overlap` / `--collide-test-approach N` (pc_net_game.c pcnetgame_run_collide_test_hook()) only write
the LOCAL player's world.position (the same field/timing the --force-dig-hole teleport uses); everything after that
(the real OC pass, collision_vec, the real player movement/BG check, scene transitions, the network) is the real
game code. It is NOT manual play: feel, walls/fences/water, net/axe swings and 3+ players are NOT covered here.

  C1  overlap teleport: the local player is pushed out of the puppet within a few frames; bounded per-frame push,
      no NaN; the OTHER process's own player never teleports or gets a network-driven reverse push.
  C2  stepped approach (2 units/frame): after first contact the distance never collapses below the contact band and
      per-frame local movement stays <= step + 2 (no jitter/explosion), no teleport.
  C3  client enters a shop -> host logs DISARMED reason=scene and no [COLLIDE] activity until it is re-ARMED after
      the client returns to the FIELD.
  C4  client killed -> host logs DISARMED reason=gone; a NEW client re-arms only after the puppet exists, has a
      snapshot and a visual (appearance resolved).

Usage: python test_player_collision_real.py [--port 7830] [--only c1|c2|c3|c4]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import math
import os
import re
import sys
import time

import net_spike_lib as L
from test_player_scene_real import boot_host, boot_client, wait_log

HERE = os.path.dirname(os.path.abspath(__file__))
DIAG_RX = re.compile(
    r"\[NET\]\[COLLIDE\]\[DIAG\] player (\d+) frame=(\d+) xz_dist=(\S+) push=\((\S+),(\S+)\) "
    r"local=\((\S+),(\S+)\) puppet=\((\S+),(\S+)\) hit=(\d) wt=(\d+) main=(\d+) peak_oc=(\d+) failed_setoc=(\d+)")
TELEPORT_RX = re.compile(r"\[NET\]\[COLLIDE\]\[TEST\] --collide-test-overlap: frame=(\d+) teleporting")
APPROACH_START_RX = re.compile(r"\[NET\]\[COLLIDE\]\[TEST\] --collide-test-approach: frame=(\d+) starting")
APPROACH_DONE_RX = re.compile(r"\[NET\]\[COLLIDE\]\[TEST\] --collide-test-approach: frame=(\d+) done")
ARM_RX = re.compile(r"\[NET\]\[COLLIDE\] player (\d+): collider ARMED")
DISARM_RX = re.compile(r"\[NET\]\[COLLIDE\] player (\d+): collider DISARMED reason=(\S+)")

TELEPORT_DIST_BAND = 38.0  # pipe radii 20+20, minus 16-bit centre truncation slack (task spec: ~38)


def diags(text, offset=0):
    out = []
    for m in DIAG_RX.finditer(text[offset:]):
        v = m.groups()
        try:
            d = dict(player=int(v[0]), frame=int(v[1]), dist=float(v[2]), push=(float(v[3]), float(v[4])),
                     local=(float(v[5]), float(v[6])), puppet=(float(v[7]), float(v[8])), hit=int(v[9]),
                     wt=int(v[10]), main=int(v[11]), peak=int(v[12]), failed=int(v[13]), pos=m.start() + offset)
        except ValueError:
            d = dict(player=int(v[0]), frame=int(v[1]), dist=float("nan"), push=(float("nan"),) * 2,
                     local=(float("nan"),) * 2, puppet=(float("nan"),) * 2, hit=0, wt=0, main=0, peak=0, failed=0,
                     pos=m.start() + offset)
        out.append(d)
    return out


def finite(d):
    return all(math.isfinite(x) for x in (d["dist"],) + d["push"] + d["local"] + d["puppet"])


def peaks(text):
    """(peak colliders seen, failed setOC count) from every DIAG/peak line of one log."""
    pk = max([d["peak"] for d in diags(text)] + [int(x) for x in re.findall(r"collider_num new peak=(\d+)", text)] or [0])
    fl = max([d["failed"] for d in diags(text)] or [0])
    return pk, fl


def c1(port, log_dir, check, stats):
    print("=" * 72 + "\n[C1] HOOK-DRIVEN: client --collide-test-overlap teleports onto the host's puppet")
    host = boot_host(port, ["--authoritative-wildlife"], "c1", log_dir)
    if host is None:
        check("C1 host reached field", False)
        return
    client = boot_client(port, ["--authoritative-wildlife", "--collide-test-overlap"], "c1", log_dir)
    try:
        if client is None:
            check("C1 client reached field", False)
            return
        ok = wait_log(client, r"--collide-test-overlap: frame=\d+ teleporting", 90.0)
        check("C1 overlap hook fired on the client (puppet present, FIELD/IN_TOWN, ~2 s stable)", ok)
        time.sleep(4.0)
        ct, ht = client.log_text(), host.log_text()
        m = TELEPORT_RX.search(ct)
        t0 = int(m.group(1)) if m else 0
        cd = [d for d in diags(ct) if d["frame"] >= t0]
        check("C1 client logged [COLLIDE] ARMED for the host's puppet (in range once teleported onto it)",
              ARM_RX.search(ct) is not None)
        check("C1 client produced per-frame [COLLIDE][DIAG] contact lines after the teleport (%d)" % len(cd), len(cd) >= 3)
        if cd:
            first_clear = next((d for d in cd if d["dist"] >= TELEPORT_DIST_BAND), None)
            frames_to_clear = None if first_clear is None else first_clear["frame"] - t0
            print("INFO - C1 teleport at game frame %d; first frame with xz_dist >= %.0f: %s (%s frames after); "
                  "first lines: %s" % (t0, TELEPORT_DIST_BAND, None if first_clear is None else first_clear["frame"],
                                      frames_to_clear, [(d["frame"] - t0, round(d["dist"], 1), d["wt"]) for d in cd[:12]]))
            check("C1 pushed out to xz_dist >= %.0f within 5 game frames of the teleport (took %s)" %
                  (TELEPORT_DIST_BAND, frames_to_clear), frames_to_clear is not None and frames_to_clear <= 5)
            check("C1 final settled distance is >= %.0f and < 60" % TELEPORT_DIST_BAND,
                  TELEPORT_DIST_BAND <= cd[-1]["dist"] < 60.0)
            check("C1 per-frame push magnitude <= 40 for every frame, no NaN/inf",
                  all(finite(d) and math.hypot(*d["push"]) <= 40.0 for d in cd))
            seq = [d["local"] for d in cd]
            jumps = [math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(seq, seq[1:])]
            check("C1 local player moved <= 40 units between consecutive logged frames (no explosion; max %.1f)" %
                  (max(jumps) if jumps else 0.0), all(j <= 40.0 for j in jumps))
        # The other process: its own player is only ever moved by ITS OWN local solver against ITS OWN puppet of the
        # sender (symmetric design; there is no push message). Bound: no teleport/explosion, no NaN.
        hd = [d for d in diags(ht) if d["pos"] > 0]
        hl = [d["local"] for d in hd]
        if hl:
            hj = [math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(hl, hl[1:])]
            span = math.hypot(max(x for x, _ in hl) - min(x for x, _ in hl), max(z for _, z in hl) - min(z for _, z in hl))
            print("INFO - C1 host own-player logged positions: %d samples, max per-step %.1f, bounding span %.1f" %
                  (len(hl), max(hj) if hj else 0.0, span))
            check("C1 other process (host) own player: no NaN, no jump > 40 between logged frames, total span <= 45 "
                  "(only its own local solver may move it)",
                  all(finite(d) for d in hd) and all(j <= 40.0 for j in hj) and span <= 45.0)
        else:
            check("C1 other process (host) own player: no collision-driven movement logged at all", True)
        check("C1 both processes alive", host.alive() and client.alive())
        for n, t in (("host", ht), ("client", ct)):
            stats.append((n + " c1",) + peaks(t))
    finally:
        client.stop()
        host.stop()


def c2(port, log_dir, check, stats):
    print("=" * 72 + "\n[C2] HOOK-DRIVEN: client --collide-test-approach 150 (2 units/frame toward the host's puppet)")
    host = boot_host(port, ["--authoritative-wildlife"], "c2", log_dir)
    if host is None:
        check("C2 host reached field", False)
        return
    client = boot_client(port, ["--authoritative-wildlife", "--collide-test-approach", "150"], "c2", log_dir)
    try:
        if client is None:
            check("C2 client reached field", False)
            return
        ok = wait_log(client, r"--collide-test-approach: frame=\d+ done", 120.0)
        check("C2 approach hook ran all its steps", ok)
        time.sleep(1.0)
        ct = client.log_text()
        ms, md = APPROACH_START_RX.search(ct), APPROACH_DONE_RX.search(ct)
        f0, f1 = (int(ms.group(1)), int(md.group(1))) if ms and md else (0, 0)
        cd = [d for d in diags(ct) if f0 <= d["frame"] <= f1 + 30 and not (d["wt"] == 0)]
        contact = [d for d in cd if d["hit"] or math.hypot(*d["push"]) > 0.0]
        check("C2 contact was reached (push seen, %d contact lines)" % len(contact), len(contact) >= 1)
        if contact:
            fc = contact[0]["frame"]
            post = [d for d in cd if d["frame"] >= fc]
            print("INFO - C2 first contact frame %d; post-contact dist: min %.1f max %.1f; wt seen %s" %
                  (fc, min(d["dist"] for d in post), max(d["dist"] for d in post), sorted({d["wt"] for d in post})))
            check("C2 after first contact xz_dist never drops below 36 (min %.1f)" % min(d["dist"] for d in post),
                  min(d["dist"] for d in post) >= 36.0)
            seq = [(d["frame"], d["local"]) for d in post]
            bad = [(a[0], b[0], math.hypot(a[1][0] - b[1][0], a[1][1] - b[1][1])) for a, b in zip(seq, seq[1:])
                   if b[0] - a[0] >= 1 and math.hypot(a[1][0] - b[1][0], a[1][1] - b[1][1]) > 2.0 * (b[0] - a[0]) + 2.0]
            check("C2 per-frame local movement <= step(2) + 2 after contact (violations: %s)" % bad[:4], not bad)
            check("C2 no NaN/inf and per-frame push <= 40", all(finite(d) and math.hypot(*d["push"]) <= 40.0 for d in post))
        check("C2 both processes alive", host.alive() and client.alive())
        for n, t in (("host", host.log_text()), ("client", ct)):
            stats.append((n + " c2",) + peaks(t))
    finally:
        client.stop()
        host.stop()


def events(text):
    ev = []
    for m in ARM_RX.finditer(text):
        ev.append((m.start(), "ARMED", int(m.group(1)), ""))
    for m in DISARM_RX.finditer(text):
        ev.append((m.start(), "DISARMED", int(m.group(1)), m.group(2)))
    return sorted(ev)


def c3(port, log_dir, check, stats):
    print("=" * 72 + "\n[C3] HOOK-DRIVEN: host overlaps the client (armed), then the client enters a shop and leaves again")
    # The overlap hook needs ~120 polls of stability, the shop hook fires ~180 polls after the field announcement:
    # putting the overlap on the HOST (long since standing) makes the armed -> shop order deterministic enough.
    host = boot_host(port, ["--authoritative-wildlife", "--collide-test-overlap"], "c3", log_dir)
    if host is None:
        check("C3 host reached field", False)
        return
    client = boot_client(port, ["--authoritative-wildlife", "--scene-test-enter-shop",
                                "--scene-test-leave-after", "600"], "c3", log_dir)
    try:
        if client is None:
            check("C3 client reached field", False)
            return
        ok = wait_log(client, r"hook: goto_other_scene\(exit\) res=1", 150.0)
        check("C3 client entered the shop and requested the real exit (hook-driven)", ok)
        time.sleep(8.0)
        ht = host.log_text()
        ev = events(ht)
        print("INFO - C3 host collider events: %s" % [(k, p, r) for _, k, p, r in ev])
        kinds = [(k, r) for _, k, _, r in ev]
        check("C3 host armed the client's puppet while both were in the FIELD", kinds[:1] == [("ARMED", "")])
        dis = [i for i, (k, r) in enumerate(kinds) if k == "DISARMED" and r == "scene"]
        check("C3 host logged DISARMED reason=scene when the client entered the shop", len(dis) >= 1)
        if dis:
            i0 = dis[0]
            re_arm = [i for i in range(i0 + 1, len(kinds)) if kinds[i][0] == "ARMED"]
            check("C3 host re-ARMED after the client returned to the FIELD", len(re_arm) >= 1)
            lo = ev[i0][0]
            hi = ev[re_arm[0]][0] if re_arm else len(ht)
            between = ht[lo:hi]
            check("C3 no [COLLIDE][DIAG] lines (= no registration, hence no pushes) while DISARMED",
                  DIAG_RX.search(between) is None)
            stray = [k for k in kinds[i0 + 1:(re_arm[0] if re_arm else len(kinds))]]
            check("C3 no extra ARMED/DISARMED flapping while inside the shop", stray == [])
        check("C3 both processes alive", host.alive() and client.alive())
        for n, t in (("host", ht), ("client", client.log_text())):
            stats.append((n + " c3",) + peaks(t))
    finally:
        client.stop()
        host.stop()


def c4(port, log_dir, check, stats):
    print("=" * 72 + "\n[C4] HOOK-DRIVEN: client killed, then a NEW client reconnects")
    host = boot_host(port, ["--authoritative-wildlife"], "c4", log_dir)
    if host is None:
        check("C4 host reached field", False)
        return
    c1p = boot_client(port, ["--authoritative-wildlife", "--collide-test-overlap"], "c4a", log_dir)
    c2p = None
    try:
        if c1p is None:
            check("C4 first client reached field", False)
            return
        ok = host.wait_for_log(r"\[NET\]\[COLLIDE\] player \d+: collider ARMED", 90.0) is not None
        check("C4 host armed the first client's puppet", ok)
        time.sleep(3.0)
        off = len(host.log_text())
        c1p.stop()
        ok = host.wait_for_log(r"\[NET\]\[COLLIDE\] player \d+: collider DISARMED reason=gone", 30.0, since_offset=off) is not None
        check("C4 killing the client -> host logged DISARMED reason=gone", ok)
        time.sleep(2.0)
        ht = host.log_text()
        after = ht[off:]
        check("C4 no [COLLIDE][DIAG] (= no registration/pushes) after the client is gone, before it returns",
              DIAG_RX.search(after[after.find("reason=gone"):]) is None if "reason=gone" in after else False)
        off2 = len(host.log_text())
        c2p = boot_client(port, ["--authoritative-wildlife", "--collide-test-overlap"], "c4b", log_dir)
        check("C4 a NEW client process reconnected and reached the field", c2p is not None)
        if c2p is not None:
            ok = host.wait_for_log(r"\[NET\]\[COLLIDE\] player \d+: collider ARMED", 60.0, since_offset=off2) is not None
            ht2 = host.log_text()[off2:]
            print("INFO - C4 host events after reconnect: %s" % [(k, r) for _, k, _, r in events(ht2)])
            if ok:
                a = ARM_RX.search(ht2).start()
                created = re.search(r"\[NET\]\[REMOTE\] created remote-player actor", ht2)
                snap = re.search(r"\[NET\]\[REMOTE\]\[DIAG\] player \d+ actor pos=.*snapshots=[1-9]", ht2)
                vis = re.search(r"\[NET\]\[REMOTE\]\[DIAG\] visual initialized", ht2)
                check("C4 new client's collider ARMED only after the puppet was created (%s), received >=1 snapshot "
                      "(%s) and its visual initialised (%s)" %
                      (bool(created), bool(snap), bool(vis)),
                      created is not None and snap is not None and vis is not None and
                      created.start() < a and snap.start() < a and vis.start() < a)
            else:
                # Not within range of the host (spawn offsets differ): still verify nothing armed prematurely.
                print("INFO - C4 new client never came within the 150-unit arming range of the host (no ARMED); "
                      "re-arm ordering could not be observed")
                check("C4 new client re-armed after reconnect (in range)", False)
        check("C4 host process alive", host.alive())
        stats.append(("host c4",) + peaks(host.log_text()))
    finally:
        for p in (c2p, c1p):
            if p:
                p.stop()
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7830)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    results, stats = [], []
    log_dir = os.path.join(HERE, "logs", "m9b")
    os.makedirs(log_dir, exist_ok=True)
    check = lambda d, c: L.check(d, c, results)
    for i, fn in enumerate((c1, c2, c3, c4)):
        if args.only and args.only != fn.__name__:
            continue
        fn(args.port + i, log_dir, check, stats)
    print("INFO - collider table stats (name, peak OC collider_num seen, failed setOC count): %s" % stats)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
