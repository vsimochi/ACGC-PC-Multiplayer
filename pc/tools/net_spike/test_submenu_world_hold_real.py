#!/usr/bin/env python3
r"""test_submenu_world_hold_real.py - worker 5 / workstream F: the HOST's submenu no longer silences the villager stream.
REAL host process (PC_NET_TEST_HOOKS build, fixture W5_FIXTURE default bin_fixture4_w5) + a protocol FakeClient (READY, scene FIELD) that counts NPC_MOVE (26).
The host opens its real inventory through the TEST-ONLY hook AC_TEST_HOST_SUBMENU="open_after_ms,hold_ms" (m_submenu.c) and closes it through the overlay's own close path.
  S1 the host opened the inventory and logged '[NET][SUBMENU] host submenu world hold BEGIN'
  S2 while the submenu is open the FakeClient keeps receiving NPC_MOVE with strictly advancing frame numbers (vanilla: none)
  S3 no walk/run action is broadcast while held (the clients show villagers standing, not walking in place)
  S4 hold END: actor count at open == at close (no actor create/delete while the menu was open)
  S5 pockets+wallet checksum unchanged across open/close; the stream continues after close; host alive
Usage: python test_submenu_world_hold_real.py [--port 16510]   (take /tmp/ac_game_lock first)"""
import argparse
import math
import os
import re
import struct
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as TS  # noqa: E402

FIXTURE = os.environ.get("W5_FIXTURE", "bin_fixture4_w5")
BIN = os.path.join(TS.BUILD64, FIXTURE)
os.environ["NET_SPIKE_GAME_BIN"] = BIN
import net_spike_lib as L  # noqa: E402

WALK_RUN = set(range(5, 13))  # aNPC_ACTION_TYPE_WALK..RUN_AI (WAIT=0..WAIT_NEMU=4)


def is26(m):
    return m.channel in (L.CH_RELIABLE, L.CH_UNRELIABLE) and m.payload and len(m.payload) == 28 and m.payload[0] == 26


def frame_of(m):
    return struct.unpack_from("<I", m.payload, 8)[0]


def pos_of(m):
    return struct.unpack_from("<fff", m.payload, 12)


def wait_host(host, rx, timeout, off=0):
    deadline = time.monotonic() + timeout
    r = re.compile(rx)
    while time.monotonic() < deadline:
        mo = r.search(host.log_text()[off:])
        if mo:
            return mo
        if not host.alive():
            return None
        L.pump_sleep(0.1)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16510)
    args = ap.parse_args()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    res = {i: ident for i, ident, ex in L.read_test_save_residents(BIN) if ex}
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0"], bin_dir=BIN,
                         log_path=os.path.join(HERE, "w5_submenu_host.log"),
                         env={"AC_TEST_HOST_SUBMENU": "6000,15000"}).start()
    ok = host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)
    check("host in the field", ok)
    if not ok:
        host.stop()
        return L.summary_and_exit_code(results)
    fc = None
    try:
        fc = L.FakeClient("F", "127.0.0.1", args.port, player=res[2])
        fc.connect_and_ready()
        fc.send_reliable(struct.pack("<BBBBHHI", 44, 0, 7, 1, 0, 0, 1))  # scene FIELD, IN_TOWN
        check("FakeClient READY", fc.is_connected())
        m0 = fc.inbox.mark()
        b = wait_host(host, r"\[NET\]\[SUBMENU\] host submenu world hold BEGIN menu_type=(\d+) actors=(\d+)", 40.0)
        m1 = fc.inbox.mark()
        pre = fc.inbox.peek_all(is26, since=m0)
        check("S1 host opened its inventory (hold BEGIN %s)" % (b.group(0) if b else None), b is not None)
        e = wait_host(host, r"\[NET\]\[SUBMENU\] host submenu world hold END frames=(\d+) actors_open=(\d+) actors_close=(\d+)", 40.0)
        m2 = fc.inbox.mark()
        held = [m for m in fc.inbox.peek_all(is26, since=m1) if m.index < m2]
        frames = [frame_of(m) for m in held]
        acts = sorted(set(m.payload[2] for m in held))
        slots = sorted(set(m.payload[1] for m in held))
        print("[INFO] NPC_MOVE before open %d, while held %d (slots %s actions %s)" % (len(pre), len(held), slots, acts))
        check("S2 NPC_MOVE kept arriving while the host submenu was open (%d msgs)" % len(held), len(held) >= 20)
        check("S2 frame numbers strictly advance", frames and all(b2 > a2 for a2, b2 in zip(frames, frames[1:])))
        moved = {}
        for s in slots:
            ps = [pos_of(m) for m in held if m.payload[1] == s]
            moved[s] = max(math.hypot(p[0] - ps[0][0], p[2] - ps[0][2]) for p in ps)
        print("[INFO] max displacement per slot while open: %s" % {k: round(v, 1) for k, v in moved.items()})
        check("F2 villagers really moved while the host submenu was open (max %.1f units)" % max(moved.values() or [0]), moved and max(moved.values()) > 5.0)
        check("F2 walk/run actions broadcast while open (%s)" % acts, set(acts) & WALK_RUN)
        check("S4 hold END logged (%s)" % (e.group(0) if e else None), e is not None)
        check("S4 actor count unchanged across the hold", e is not None and e.group(2) == e.group(3))
        ex = re.search(r"world hold END .*?ticks=(\d+) refused_make=(\d+) deferred_delete=(\d+) bank_idx_open=(-?\d+) bank_idx_close=(-?\d+)", host.log_text())
        check("F2 villager move procs ran (%s)" % (ex and ex.group(0)), ex and int(ex.group(1)) > 0)
        check("F2 no exchange bank allocated while open (bank_idx equal)", ex and ex.group(4) == ex.group(5))
        ht = host.log_text()
        po = re.search(r"\[TEST\]\[SUBMENU\] host inventory OPEN pockets_sum=([0-9A-F]+)", ht)
        pc = re.search(r"\[TEST\]\[SUBMENU\] host inventory CLOSED pockets_sum=([0-9A-F]+)", ht)
        check("S5 pockets+wallet unchanged (%s / %s)" % (po and po.group(1), pc and pc.group(1)), po and pc and po.group(1) == pc.group(1))
        L.pump_sleep(3.0)
        after = fc.inbox.peek_all(is26, since=m2)
        check("S5 stream continues after close (%d msgs) and host alive" % len(after), len(after) > 0 and host.alive())
        jumps = {}
        for s in slots:
            a_ = [pos_of(m) for m in after if m.payload[1] == s]
            h_ = [pos_of(m) for m in held if m.payload[1] == s]
            if a_ and h_:
                jumps[s] = math.hypot(a_[0][0] - h_[-1][0], a_[0][2] - h_[-1][2])
        print("[INFO] close jump per slot: %s" % {k: round(v, 1) for k, v in jumps.items()})
        check("F2 no jump after close larger than ~1 s of walking (%s)" % jumps, jumps and max(jumps.values()) < 150.0)
    finally:
        try:
            if fc is not None:
                fc.disconnect()
        finally:
            host.stop()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
