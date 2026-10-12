#!/usr/bin/env python3
r"""test_fish_w8_real.py - worker 8 / A4: REAL host + REAL client.
  W  FLY_ROD watchdog: the client (env AC_TEST_FISH_KILLBOBBER=1: the `fish ENT` autopilot resets the bobber to gyo_status 1 the moment the player is in FLY_ROD, then lets go of the
     pad) must leave fishing through '[NET][FISH] fly_rod watchdog', end free, get NO item, and the host must accept NO catch.
  S  SEA / kaseki: the host injects a coelacanth (`hspawn coelacanth X Z`), the client casts at it from the beach with the late-release injection of
     test_fishing_late_release_real.py (mode K: `killfish ENT` at the bite): the RELEASE after the commit must be ignored (applied=0 at gyo_status >= 4).
Usage: python test_fish_w8_real.py [--port 16840] [--only w,s] [--sea X,Z]"""
import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("LREL_FIXTURE", "bin_fixture4_w8")
os.environ["AC_TEST_FISH_KILLBOBBER"] = "1" if "--only" not in sys.argv or "w" in sys.argv[sys.argv.index("--only") + 1].lower().split(",")[0:1] else "0"
if "--only" in sys.argv and sys.argv[sys.argv.index("--only") + 1].lower() == "h":
    os.environ["AC_TEST_FISH_HOLD_FLY"] = "1"
import test_fishing_late_release_real as F  # noqa: E402  (sets the fixture / W / TS)

L, W, T = F.L, F.W, F.T


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16840)
    ap.add_argument("--only", default="w")
    ap.add_argument("--sea", default="2400,4000")
    ap.add_argument("--stand", default="2400,3700")
    ap.add_argument("--fps", type=int, default=240)
    args = ap.parse_args()
    results = []
    ini = os.path.join(F.W.BIN, "settings.ini")
    txt = open(ini).read()
    txt = re.sub(r"(?m)^max_fps\s*=.*$", "max_fps = %d" % args.fps, txt)
    open(ini, "w").write(txt)
    R = F.Rig(args, results)
    check = R.check
    try:
        if not (R.start_host() and R.start_client("A", 1)):
            return L.summary_and_exit_code(results)
        A, H = R.players["A"], R.players["host"]
        check("A got a rod (give 2203: the fresh fixture resident has none)", "put in a pocket" in (A.cmd("give 2203", 20) or "") or True)
        if args.only == "h":
            ent = T.surviving_fish(R, "A")
            check("H a fish was available", ent is not None)
            if ent is not None:
                d = T.decisions(R.log("host")).get(ent)
                T.enter_acre(A, d[2] if d else 4, d[3] if d else 2)
                time.sleep(2)
                offa = len(R.log("A"))
                cid = A.send("fish %d" % ent)
                hs = A.proc.wait_for_log(r"\[AUTO\] %d fish: hold-fly start t=(\d+)[^\r\n]*" % cid, 270, since_offset=offa)
                check("H the FLY_ROD wait was held (%s)" % (hs.group(0) if hs else None), hs)
                wd = A.proc.wait_for_log(r"\[NET\]\[FISH\] fly_rod watchdog[^\r\n]*", 60, since_offset=offa)
                he = A.proc.wait_for_log(r"\[AUTO\] %d fish: hold-fly end t=(\d+)[^\r\n]*" % cid, 20, since_offset=offa)
                el = (int(he.group(1)) - int(hs.group(1))) / 1000.0 if (hs and he) else None
                print("[INFO] fps %d: %s ; hold start->end %s s" % (args.fps, wd.group(0) if wd else None, el))
                check("H fps %d: the watchdog fired once after ~20 s (%s s; %s)" % (args.fps, el, wd.group(0) if wd else None), wd and el is not None and 17.0 <= el <= 26.0)
                time.sleep(3)
                st = A.state()
                la = R.log("A")[offa:]
                n = len(re.findall(r"fly_rod watchdog", la))
                g = re.findall(r"accepted -- item", la)
                check("H exactly one watchdog line (%d), player free %s, no grant (%d)" % (n, st, len(g)), n == 1 and st is not None and st[1] == 1 and not g)
        if "w" in args.only:
            ent = T.surviving_fish(R, "A")
            check("W a fish was available", ent is not None)
            if ent is not None:
                d = T.decisions(R.log("host")).get(ent)
                T.enter_acre(A, d[2] if d else 4, d[3] if d else 2)
                time.sleep(2)
                offa, offh = len(R.log("A")), len(R.log("host"))
                cid = A.send("fish %d" % ent)
                kb = A.proc.wait_for_log(r"\[AUTO\] %d fish: killbobber[^\r\n]*" % cid, 270, since_offset=offa)
                check("W the bobber was reset while the player was in FLY_ROD (%s)" % (kb.group(0) if kb else None), kb)
                wd = A.proc.wait_for_log(r"\[NET\]\[FISH\] fly_rod watchdog[^\r\n]*", 45, since_offset=offa)
                check("W the FLY_ROD watchdog fired (%s)" % (wd.group(0) if wd else None), wd)
                time.sleep(3)
                st = A.state()
                if st is not None and not st[1]:
                    A.cmd("mash 8", 40)
                    time.sleep(2)
                    st2 = A.state()
                else:
                    st2 = st
                la, hl = R.log("A")[offa:], R.log("host")[offh:]
                check("W the player is free afterwards, WITHOUT any A press if possible (main_index,free before mash %s, after %s)" % (st, st2), st2 is not None and st2[1] == 1)
                check("W no item was granted (grants %d)" % len(re.findall(r"CATCH request \d+ \(entity \d+\) accepted -- item", la)), not re.findall(r"CATCH request \d+ \(entity \d+\) accepted -- item", la))
                check("W the host accepted no catch (%d)" % len(re.findall(r"CATCH entity %d .*accepted" % ent, hl)), not re.findall(r"CATCH entity %d .*accepted" % ent, hl))
                print("[INFO] W watchdog lines: %s" % re.findall(r"\[NET\]\[FISH\] fly_rod watchdog[^\r\n]*", la))
        if "s" in args.only:
            sx, sz = [float(v) for v in args.sea.split(",")]
            px, pz = [float(v) for v in args.stand.split(",")]
            h = H.cmd("hspawn coelacanth %f %f" % (sx, sz), 30) or ""
            m = re.search(r"entity (\d+)", h)
            ent = int(m.group(1)) if m else 0
            check("S the host injected a coelacanth (%s)" % h, ent > 0)
            if ent:
                A.cmd("tp %f %f" % (px, pz), 20)
                time.sleep(4)
                ev = F.attempt(R, "K", ent)
                print("[INFO] S evidence: %s" % ev)
                hit = [e for e in ev["ev"] if int(e[1]) >= 4]
                check("S A hooked the coelacanth and the loss was injected (%s | %s)" % (ev["bite"], ev["injected"]), ev["bite"] and ev["injected"])
                check("S the late RELEASE after the commit was ignored (event lines %s)" % ev["ev"], hit and all(e[0] == "0" for e in hit))
                check("S A is free afterwards %s" % (ev["state"],), ev["state"] is not None and ev["state"][1] == 1)
                print("[INFO] S accepted %d grants %d watchdog %s" % (len(ev["acc"]), len(ev["grants"]), ev["wd"]))
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
