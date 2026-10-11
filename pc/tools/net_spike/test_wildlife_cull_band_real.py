#!/usr/bin/env python3
"""test_wildlife_cull_band_real.py - W-04: the host keeps a record attended up to PC_WILDLIFE_ATTEND_UNITS (700) from a player, but its fish cull (aGYO_cull_check) only exempted creatures within
600 units of a REMOTE player. A fish 600-700 units from the only remote player (and far from the host's own player) was therefore attended, culled, re-created every second and finally released
by B-7 although a player was in range. REAL host process + one scripted FakeClient standing at a chosen distance (volume from the environment).
  W1  control: the peer stands 450 units from the fish: the host actor is never culled (no 're-creating' line for it)
  W2  the peer stands 650 units from the fish (inside the attendance radius, outside the old 600 cull exemption): the host actor is not culled either, and the record stays in the table
The fish are injected with the test-only `hspawn` next to the host's own player's far side (>= 650 units from the host player, so only the REMOTE player can keep them alive).
Usage: python test_wildlife_cull_band_real.py [--port 12940]"""
import argparse
import os
import re
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "0")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_wildlife_lifecycle_real as LC  # noqa: E402
import net_spike_lib as L  # noqa: E402


def run_phase(R, tag, fish_xz, peer_xz, secs=18):
    H = R.players["host"]
    off = len(R.log("host"))
    ans = H.cmd("hspawn 8 %.1f %.1f" % fish_xz, 30)
    m = re.search(r"entity (\d+)", ans or "")
    ent = int(m.group(1)) if m else 0
    R.check("%s the host injected a fish (entity %d) at %s" % (tag, ent, fish_xz), ent > 0)
    f = L.FakeClient("W%s" % tag, "127.0.0.1", R.args.port)
    try:
        f.connect_and_ready()
        t_end = time.time() + secs
        while time.time() < t_end:
            f.claim_position(peer_xz[0], 160.0, peer_xz[1])
            L.pump_sleep(0.25)
        txt = R.log("host")[off:]
        recreated = len(re.findall(r"entity %d \(kind 0\) is attended but has no host actor" % ent, txt))
        released = re.search(r"entity %d \(kind 0 species \d+ acre \d+,\d+\) released" % ent, txt) is not None
        in_table = ent in LC.T.table_now(R)
        return ent, recreated, released, in_table
    finally:
        try:
            f.disconnect()
        except Exception:  # noqa: BLE001
            pass
        H.cmd("hdespawn %d" % ent, 20)
        L.pump_sleep(3.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12940)
    args = ap.parse_args()
    results = []
    R = LC.Rig(args, results)
    try:
        if not R.start_host():
            R.check("host in the field", False)
            return L.summary_and_exit_code(results)
        H = R.players["host"]
        H.cmd("tp 1300 1500", 20)  # the host's own player stands far west: the fish below are >= 1400 units from it
        time.sleep(2)
        fish = (2794.0, 1404.0)
        ent, rec, rel, tab = run_phase(R, "W1", fish, (2794.0 - 450.0, 1404.0))
        R.check("W1 control: peer 450 units away: the host actor of fish %d is not culled for good (re-creations %d; one at start-up is normal: the peer's first position has not arrived yet), not released (%s), still in the table (%s)" % (ent, rec, rel, tab), rec <= 1 and not rel and tab)
        ent, rec, rel, tab = run_phase(R, "W2", fish, (2794.0 - 650.0, 1404.0))
        R.check("W2 peer 650 units away (inside the 700 attendance radius): the host actor of fish %d is not culled (re-creations %d), not released (%s), still in the table (%s)" % (ent, rec, rel, tab),
                rec <= 1 and not rel and tab)
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
