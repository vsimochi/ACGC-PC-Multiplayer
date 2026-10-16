#!/usr/bin/env python3
"""test_fishing_view_protocol.py - worker P3: BOBBER_VIEW (id 97) and PLAYER_ACTION kind 2 CATCH against a REAL `AnimalCrossing.exe` host with FakeClient peers.

TIER: PROTOCOL + HOST LOGIC (real host process; the peers are FakeClients that speak the wire). Run through p3_run.py (fresh disposable fixture
pc\\build64\\bin_fixture4_p3_<name> with the frozen pc\\build64_w3\\p3_AnimalCrossing.exe; ports 24000-24999; Samsung, volume 1).

  V1  A (town field) sends BOBBER_STATE at ~30 Hz for 3 s: B (same scene) receives BOBBER_VIEW (24 B, UNRELIABLE) with origin = A's id, A's position,
      at <= ~10 Hz; A itself never receives its own view
  V2  C announces a shop scene: C receives NO active BOBBER_VIEW while A fishes in the town
  V3  an implausible BOBBER_STATE (2000 units from A's MOVE position) is not relayed (host log names it); the fish-AI path of BOBBER_STATE is unchanged
  V4  reel-in: A sends active=0 -> B receives one RELIABLE 'gone' (active 0) with a newer seq; no active view follows
  V5  a client-originated BOBBER_VIEW (id 97 client -> host) is ignored: not relayed, the host keeps running
  V6  A casts again and DISCONNECTS without a final state: B receives a 'gone' for A (disconnect)
  C1  PLAYER_ACTION kind 2 CATCH: the host spawns a bug (test-only hspawnbug), D catches it through the X3 catch transaction: B receives
      PLAYER_ACTION (10 B, RELIABLE) kind 2 with origin = D's id and the host-derived item (ITM_INSECT00 + species); D does not
  C2  a REJECTED catch (the same entity again) produces no kind 2
Exit code: 0 all checks passed, 1 otherwise."""
import argparse
import math
import os
import re
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402
from net_spike_lib import FakeClient, check, summary_and_exit_code  # noqa: E402
import wplay_lib as W  # noqa: E402
from test_wildlife_bug_catch import get_current_generation  # noqa: E402

MSG_BOBBER_STATE, MSG_BOBBER_VIEW, MSG_PLAYER_ACTION = 74, 97, 46
BS_FMT = "<BBBbbBBB3f3fhh"   # PCNetGameBobberStateMsg, 36 B
BV_FMT = "<BBBBBbBB3fhH"     # PCBobberViewMsg, 24 B
PA_FMT = "<BBBBBBHH"         # PCNetGamePlayerActionMsg, 10 B
assert struct.calcsize(BS_FMT) == 36 and struct.calcsize(BV_FMT) == 24 and struct.calcsize(PA_FMT) == 10
SCENE_FG, SCENE_SHOP = 7, 9  # m_scene_table.h: SCENE_FG, SCENE_SHOP0 (Nook's Cranny)
KIND_CATCH = 2
SP_BEETLE = 20
ITM_INSECT00 = 0x2D00


def scene(c, sid, seq, in_town=True):
    c.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, sid, L.PC_NETGAME_SCENE_FLAG_IN_TOWN if in_town else 0, 0, 0, seq))
    L.pump_sleep(0.5)


class Bobber:
    def __init__(self, c):
        self.c, self.seq = c, 0

    def send(self, active, x, y, z, uki=4, gyo=1, reliable=False):
        self.seq += 1
        raw = struct.pack(BS_FMT, MSG_BOBBER_STATE, 1 if active else 0, uki if active else 0, gyo if active else 0, 0, 1 if active else 0, 0, 0,
                          x, y, z, x, y, z, 0x1000, self.seq)
        return self.c.send_reliable(raw) if reliable else self.c.send_unreliable(raw)


def is_view(m):
    return m.payload and m.payload[0] == MSG_BOBBER_VIEW and len(m.payload) == 24


def views(c, since=None):
    out = []
    for m in c.inbox.peek_all(is_view, since=since):
        v = struct.unpack(BV_FMT, m.payload)
        out.append(dict(t=m.t, ch=m.channel, origin=v[1], active=v[2], scene=v[3], uki=v[4], gyo=v[5], rod=v[6], rsv=v[7], pos=v[8:11], angle=v[11], seq=v[12]))
    return out


def actions(c, since=None):
    out = []
    for m in c.inbox.peek_all(lambda m: m.payload and m.payload[0] == MSG_PLAYER_ACTION and len(m.payload) == 10, since=since):
        v = struct.unpack(PA_FMT, m.payload)
        out.append(dict(ch=m.channel, origin=v[1], kind=v[2], flags=v[3], ut_x=v[4], ut_z=v[5], item=v[6], seq=v[7]))
    return out


def stream(bob, x, y, z, seconds, hz=30.0):
    end = time.time() + seconds
    while time.time() < end:
        bob.send(True, x, y, z)
        L.pump_sleep(1.0 / hz)


def run(port, log_dir, results):
    W.make_fixture()
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"], env={"AC_TEST_AUTOPILOT": "ac_auto_host.txt"},
                         bin_dir=W.BIN, log_path=os.path.join(log_dir, "p3_fv_protocol_host.log")).start()
    try:
        check("host reached the field", host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=0), results)
        hp = W.Player(L, "host", host)
        a = FakeClient("A", "127.0.0.1", port)
        b = FakeClient("B", "127.0.0.1", port)
        c = FakeClient("C", "127.0.0.1", port)
        for fc in (a, b, c):
            fc.connect_and_ready()
            fc.drain_field_updates(timeout=0.3)
        ax, az = 4 * 640 + 320.0, 2 * 640 + 320.0
        scene(a, SCENE_FG, 1)
        scene(b, SCENE_FG, 1)
        scene(c, SCENE_SHOP, 1, in_town=False)
        a.send_move_any(ax, 0.0, az, reliable=True)
        b.send_move_any(ax + 80.0, 0.0, az, reliable=True)
        L.pump_sleep(0.6)
        aid = a.assigned_peer_id
        bob = Bobber(a)

        # ---- V1 / V2
        print("=" * 72 + "\nV1 / V2 relay to the same scene, <= 10 Hz")
        mb, mc, ma = b.inbox.mark(), c.inbox.mark(), a.inbox.mark()
        stream(bob, ax + 100.0, 20.0, az + 60.0, 3.0)
        L.pump_sleep(0.4)
        vb = [v for v in views(b, mb) if v["origin"] == aid]
        act = [v for v in vb if v["active"]]
        span = (act[-1]["t"] - act[0]["t"]) if len(act) > 1 else 0.0
        rate = (len(act) - 1) / span if span > 0 else 0.0
        check("V1 B received active BOBBER_VIEWs for A (origin %s): %d in ~3 s" % (aid, len(act)), len(act) >= 15, results)
        check("V1 the rate is <= ~10 Hz (%.1f Hz over %.2f s; the host input was ~30 Hz)" % (rate, span), 0 < rate <= 11.0, results)
        check("V1 the views are UNRELIABLE, carry A's position / scene / uki 4 / gyo 1 / rsv 0",
              act and all(v["ch"] == L.CH_UNRELIABLE and abs(v["pos"][0] - (ax + 100.0)) < 0.01 and abs(v["pos"][2] - (az + 60.0)) < 0.01 and v["scene"] == SCENE_FG
                          and v["uki"] == 4 and v["gyo"] == 1 and v["rsv"] == 0 for v in act), results)
        check("V1 seq strictly increases", all((act[i + 1]["seq"] - act[i]["seq"]) & 0xFFFF in range(1, 0x8000) for i in range(len(act) - 1)), results)
        check("V1 A never receives its own bobber view", not [v for v in views(a, ma) if v["origin"] == aid], results)
        check("V2 C (in a shop) received no active view of A", not [v for v in views(c, mc) if v["origin"] == aid and v["active"]], results)
        check("V1 host log: BOBBER_VIEW relay lines for origin %s" % aid, re.search(r"\[NET\]\[FISHVIEW\] host: BOBBER_VIEW origin=%d active=1" % aid, host.log_text()) is not None,
              results)

        # ---- V3 implausible
        print("=" * 72 + "\nV3 implausible bobber")
        L.pump_sleep(1.3)  # the host turns the silent bobber into a 'gone' after 1 s
        mb = b.inbox.mark()
        stream(bob, ax + 2000.0, 20.0, az, 1.0)
        L.pump_sleep(0.4)
        check("V3 a bobber 2000 units from A's MOVE position is not relayed (no active view)",
              not [v for v in views(b, mb) if v["origin"] == aid and v["active"]], results)
        check("V3 host log names it", "bobber not relayed (implausible or reordered" in host.log_text(), results)

        # ---- V4 reel-in
        print("=" * 72 + "\nV4 reel-in")
        stream(bob, ax + 100.0, 20.0, az + 60.0, 1.0)
        mb = b.inbox.mark()
        bob.send(False, 0.0, 0.0, 0.0, reliable=True)
        L.pump_sleep(0.8)
        after = [v for v in views(b, mb) if v["origin"] == aid]
        gone = [v for v in after if not v["active"]]
        check("V4 B received exactly one 'gone' for A, RELIABLE (%s)" % [(v["active"], v["ch"]) for v in after],
              len(gone) == 1 and gone[0]["ch"] == L.CH_RELIABLE, results)
        check("V4 no active view after the gone", not [v for v in after if v["active"] and gone and v["seq"] > gone[0]["seq"]], results)

        # ---- V5 client-originated id 97
        print("=" * 72 + "\nV5 client-originated BOBBER_VIEW")
        mb = b.inbox.mark()
        a.send_reliable(struct.pack(BV_FMT, MSG_BOBBER_VIEW, 0, 1, SCENE_FG, 4, 1, 0, 0, ax, 20.0, az, 0, 99))
        L.pump_sleep(0.8)
        check("V5 a client-originated BOBBER_VIEW is not relayed", not views(b, mb), results)
        check("V5 the host keeps running", host.proc.poll() is None, results)

        # ---- V6 disconnect
        print("=" * 72 + "\nV6 disconnect while cast")
        stream(bob, ax + 100.0, 20.0, az + 60.0, 1.0)
        mb = b.inbox.mark()
        a.close()
        t0 = time.time()
        got = []
        while time.time() - t0 < 6.0 and not got:
            L.pump_sleep(0.3)
            got = [v for v in views(b, mb) if v["origin"] == aid and not v["active"]]
        check("V6 B received a 'gone' for A after A disconnected (%.1f s)" % (time.time() - t0), bool(got), results)

        # ---- C1 / C2 PLAYER_ACTION kind 2
        print("=" * 72 + "\nC1 / C2 PLAYER_ACTION kind 2 CATCH")
        d = FakeClient("D", "127.0.0.1", port)
        d.connect_and_ready()
        d.drain_field_updates(timeout=0.3)
        scene(d, SCENE_FG, 1)
        bx, bz = 3 * 640 + 320.0, 3 * 640 + 320.0
        d.send_move_any(bx, 0.0, bz, reliable=True)
        L.pump_sleep(0.5)
        gen = get_current_generation(d)
        check("C0 the wildlife generation was read", gen is not None, results)
        ans = hp.cmd("hspawnbug %d %.0f %.0f" % (SP_BEETLE, bx + 20.0, bz), 20) or ""
        m = re.search(r"entity (\d+)", ans)
        ent = int(m.group(1)) if m and " ok " in ans else 0
        check("C0 the host spawned a beetle record (%s)" % ans.strip(), ent != 0, results)
        L.pump_sleep(0.8)
        mb, md = b.inbox.mark(), d.inbox.mark()
        sent = d.send_catch_txn(ent, gen, 9101, SP_BEETLE, kind="bug")
        r = d.wait_txn_result(sent.seq, 3.0)
        check("C1 D's catch was APPLIED by the host (%s)" % ((r and (r.outcome, r.reason, hex(r.item))),), r is not None and r.outcome == 0, results)
        L.pump_sleep(0.8)
        ka = [x for x in actions(b, mb) if x["kind"] == KIND_CATCH]
        check("C1 B received PLAYER_ACTION kind 2 for D (origin %s) with the host-derived item 0x%04X, RELIABLE, flags 0 (%s)" % (d.assigned_peer_id, ITM_INSECT00 + SP_BEETLE, ka),
              len(ka) == 1 and ka[0]["origin"] == d.assigned_peer_id and ka[0]["item"] == ITM_INSECT00 + SP_BEETLE and ka[0]["ch"] == L.CH_RELIABLE and ka[0]["flags"] == 0
              and ka[0]["ut_x"] == 0 and ka[0]["ut_z"] == 0, results)
        check("C1 D (the catcher) did not receive its own CATCH", not [x for x in actions(d, md) if x["kind"] == KIND_CATCH], results)
        check("C1 host log: PLAYER_ACTION CATCH origin=%s" % d.assigned_peer_id,
              re.search(r"\[NET\]\[ACTION\] host: PLAYER_ACTION CATCH origin=%d item=0x%04X" % (d.assigned_peer_id, ITM_INSECT00 + SP_BEETLE), host.log_text()) is not None, results)
        mb = b.inbox.mark()
        sent2 = d.send_catch_txn(ent, gen, 9102, SP_BEETLE, kind="bug")
        r2 = d.wait_txn_result(sent2.seq, 3.0)
        L.pump_sleep(0.8)
        check("C2 the repeated catch of the same entity is REJECTED (%s)" % ((r2 and (r2.outcome, r2.reason)),), r2 is not None and r2.outcome == 1, results)
        check("C2 ... and produces no kind 2", not [x for x in actions(b, mb) if x["kind"] == KIND_CATCH], results)
        for fc in (b, c, d):
            fc.close()
    finally:
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=24110)
    args = ap.parse_args()
    results = []
    log_dir = os.path.join(HERE, "p3_logs")
    os.makedirs(log_dir, exist_ok=True)
    run(args.port, log_dir, results)
    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
