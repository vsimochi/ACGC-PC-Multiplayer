#!/usr/bin/env python3
"""test_wildlife_golden_rod_real.py - the REAL equipment path of the rod type (REAL host + REAL client process; Samsung display, volume 1).

Where test_wildlife_rod_type_real.py sends hand-made BOBBER_STATE bytes from a fake peer, this test lets a real client EQUIP a rod through the game's own player state and cast it:
  * the golden rod (ITM_GOLDEN_ROD 0x223C) is put in the client's bag with the test-only `give` (a pocket item, exactly what a player would have found / bought);
  * the autopilot's `catch fish ENT golden` / `equip HEX` cycle the equipped tool with the game's own D-pad tool cycle until Now_Private->equipment is the wanted item (nothing sets `equipment`
    directly), then walk, aim, cast, wait for the bite and hook it with the real player code;
  * client side: pcwld_client_collect_bobber() reads Now_Private->equipment == ITM_GOLDEN_ROD into BOBBER_STATE.rod_type; host side: the [PROXY-DIAG] lines show what the host stored for that
    angler's proxy and which rod its fish AI actually judged the bobber with.
Cases (the host's OWN equipped rod is varied to show it is not consulted for a remote angler's bobber):
  G1  client golden rod, host has the normal rod equipped       -> host stores / judges GOLDEN
  G2  client normal rod, host has the GOLDEN rod equipped       -> host stores / judges NORMAL
What this does NOT show: bite timing / search angle differences (the two rods differ in aGYO_search_angle and aGYO_bite_time) -- the evidence is which rod table the host's AI used.
Usage: python test_wildlife_golden_rod_real.py [--port 12984]"""
import argparse
import os
import re
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_wildlife_lifecycle_real as LC  # noqa: E402
import net_spike_lib as L  # noqa: E402

T = LC.T
GOLDEN, ROD = "223C", "2203"


def fish_with(R, who, rod_word, tag):
    """one real cast-bite-hook of a fresh fish by `who` with the wanted rod; returns (ans, host-log slice)"""
    check = R.check
    P = R.players[who]
    ent = T.surviving_fish(R, who)
    check("%s a live fish exists" % tag, ent is not None)
    if ent is None:
        return None, ""
    d = T.decisions(R.log("host")).get(ent)
    T.enter_acre(P, d[2] if d else 4, d[3] if d else 2)
    time.sleep(2)
    off = len(R.log("host"))
    ans = P.cmd("catch fish %d %s" % (ent, rod_word), 280)
    if ans is not None and "no local fish actor" in ans:
        T.enter_acre(P, d[2] if d else 4, d[3] if d else 2)
        time.sleep(3)
        ans = P.cmd("catch fish %d %s" % (ent, rod_word), 280)
    P.cmd("mash 20", 60)
    time.sleep(3)
    return ans, R.log("host")[off:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12984)
    args = ap.parse_args()
    results = []
    R = LC.Rig(args, results)
    check = R.check
    try:
        if not (R.start_host() and R.start_client("A", 1)):
            check("host and client in the field", False)
            return L.summary_and_exit_code(results)
        H, A = R.players["host"], R.players["A"]
        for P, who in ((H, "host"), (A, "A")):
            r = P.cmd("give %s" % GOLDEN, 20)
            check("%s: the golden rod is in a pocket (%s)" % (who, r), r is not None and "put in a pocket" in r)

        # G1: the host holds the NORMAL rod, the client casts the GOLDEN one
        r = H.cmd("equip %s" % ROD, 60)
        check("G1 the host has the normal rod equipped by the D-pad cycle (%s)" % r, r is not None and " ok" in r)
        ans, hl = fish_with(R, "A", "golden", "G1")
        alog = R.log("A")
        check("G1 the client's equipped item became the golden rod (autopilot log shows equipment 0x223C)", "equipment 0x223C" in alog)
        stored = re.findall(r"peer (\d+) bobber rod: wire value (\d+) -> stored as (\w+) rod", hl)
        judged = re.findall(r"peer (\d+) bobber is judged by the fish AI with the (\w+) rod", hl)
        L.info("G1 autopilot answer %r; host stored %s; host judged %s" % (ans, stored, judged))
        check("G1 the host stored the client's bobber as GOLDEN (wire value 1)", any(s[1] == "1" and s[2] == "golden" for s in stored))
        check("G1 the host's fish AI judged it with the GOLDEN rod although the host holds the normal one", bool(judged) and judged[-1][1] == "golden")
        check("G1 the cast was a real catch attempt that reached the host fish AI (bobber events or a verdict)", "bobber event" in hl or "CATCH entity" in hl)

        # G2: the host holds the GOLDEN rod, the client casts the NORMAL one
        r = H.cmd("equip %s" % GOLDEN, 60)
        check("G2 the host has the golden rod equipped by the D-pad cycle (%s)" % r, r is not None and " ok" in r)
        ans, hl = fish_with(R, "A", "", "G2")
        stored = re.findall(r"peer (\d+) bobber rod: wire value (\d+) -> stored as (\w+) rod", hl)
        judged = re.findall(r"peer (\d+) bobber is judged by the fish AI with the (\w+) rod", hl)
        L.info("G2 autopilot answer %r; host stored %s; host judged %s" % (ans, stored, judged))
        check("G2 the host did NOT store a golden rod for the client's bobber", not any(s[2] == "golden" for s in stored))
        check("G2 the host's fish AI judged it with the NORMAL rod although the host holds the golden one", bool(judged) and all(j[1] == "normal" for j in judged))
        check("G2 the cast was a real catch attempt that reached the host fish AI", "bobber event" in hl or "CATCH entity" in hl)
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
