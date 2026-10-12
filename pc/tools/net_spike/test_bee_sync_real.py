#!/usr/bin/env python3
r"""test_bee_sync_real.py - worker 8 / A3: BEE_SWARM (msg 81) with a REAL host + REAL client observer + FakeClient shaker (A) and FakeClient observer (B).
Needs a PC_NET_TEST_HOOKS exe (logs '[NET][BEE] present ... proxy count N', 'proxy spawn', 'aBEE proxy ct ran', 'proxy gone').
Env: W8_FIXTURE (default bin_fixture4_w8), AC_DISPLAY_NAME=samsung, AC_MASTER_VOLUME=1."""
import argparse
import os
import re
import struct
import sys

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as TS  # noqa: E402

FIXTURE = os.environ.get("W8_FIXTURE", "bin_fixture4_w8")
BIN = os.path.join(TS.BUILD64, FIXTURE)
os.environ["NET_SPIKE_GAME_BIN"] = BIN
import net_spike_lib as L  # noqa: E402
import wplay_lib as W  # noqa: E402

W.NAME = FIXTURE
W.BIN = BIN

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=16800)
args = ap.parse_args()

BEE = "<BBBBHHiii"
assert struct.calcsize(BEE) == 20
SCN_FMT = "<BBBBHHI"
SCENE_FG, SCENE_SHOP0 = 7, 9
results = []
seqs = {}


def check(d, c):
    L.check(d, bool(c), results)


def scene(c, sid, flags=1):
    seqs[c.label] = seqs.get(c.label, 0) + 1
    c.send_reliable(struct.pack(SCN_FMT, 44, 0, sid, flags, 0, 0, seqs[c.label]))


def bee(ev, seq, x, z, y=100, scene_id=SCENE_FG, origin=0, msg=81):
    return struct.pack(BEE, msg, ev, origin, scene_id, seq, 0, int(x), int(y), int(z))


def is81(m):
    return m.channel in (L.CH_RELIABLE, L.CH_UNRELIABLE) and m.payload and m.payload[0] == 81


def counts(log, off):
    """(present lines, spawn lines, ct lines, gone lines) since offset"""
    t = log[off:]
    return (re.findall(r"\[NET\]\[BEE\] present origin (\d+) event (\d+) seq (\d+) scene \d+ -> proxy count (\d+)", t),
            re.findall(r"\[NET\]\[BEE\] proxy spawn origin (\d+) seq (\d+) actor (\w+)", t),
            t.count("aBEE proxy ct ran"),
            re.findall(r"\[NET\]\[BEE\] proxy gone origin (\d+) -> proxy count (\d+)", t))


SH = [None, None]


def nap(sec):
    """pump the FakeClients and keep the current shaker streaming its position (a real owner moves; a bee proxy follows the owner's last snapshot)"""
    import time as _t
    end = _t.time() + sec
    while True:
        if SH[0] is not None:
            try:
                SH[0].send_move_any(*SH[1], reliable=True)
            except Exception:  # noqa: BLE001
                pass
        L.pump_sleep(min(0.25, max(0.0, end - _t.time())))
        if _t.time() >= end:
            return


def waitlog(proc, rx, timeout, since_offset=0):
    """poll the process log while PUMPING the FakeClients (a plain wait_for_log lets their transport time out)"""
    end = L.time.time() + timeout if hasattr(L, "time") else None
    import time as _t
    end = _t.time() + timeout
    while _t.time() < end:
        m = re.search(rx, proc.log_text()[since_offset:])
        if m:
            return m
        nap(0.3)
    return None


def main():
    res = {i: ident for i, ident, ex in L.read_test_save_residents(BIN) if ex}
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"], bin_dir=BIN, log_path=os.path.join(HERE, "w8_bee_host.log")).start()
    ok = host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)
    check("host in the field", ok)
    if not ok:
        host.stop()
        return
    cli = a = b = None
    try:
        cli = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"], bin_dir=BIN, log_path=os.path.join(HERE, "w8_bee_client.log"),
                              label="w8c", env={"AC_TEST_AUTOPILOT": "ac_auto_C.txt"}).start()
        check("real client observer in the field", cli.boot_to_field(timeout=180, slot=1))
        pc = W.Player(L, "C", cli)
        a = L.FakeClient("A", "127.0.0.1", args.port, player=res[2])
        b = L.FakeClient("B", "127.0.0.1", args.port, player=res[3])
        a.connect_and_ready()
        b.connect_and_ready()
        check("FakeClients A (shaker) and B (observer) READY", a.is_connected() and b.is_connected())
        aid = a.assigned_peer_id
        x0, y0, z0 = L.tile_center(56, 104)
        x0, z0 = 2400.0, 1488.0  # next to the observer (a bee far outside its block is culled and never moves)
        a.claim_position(x0, y0, z0)
        scene(a, SCENE_FG)
        scene(b, SCENE_FG)
        SH[0], SH[1] = a, (x0, y0, z0)
        nap(2.0)
        st0 = pc.state()
        pos0 = pc.pos()
        check("observer start state read (%s %s)" % (st0, pos0), st0 is not None)
        ho, co = len(host.log_text()), len(cli.log_text())
        mb = b.inbox.mark()

        # S1 spawn
        a.send_reliable(bee(1, 100, x0, z0))
        nap(2.5)
        hp, hs, hct, hg = counts(host.log_text(), ho)
        cp, cs, cct, cg = counts(cli.log_text(), co)
        print("[INFO] host: present %s spawn %s ct %d | client: present %s spawn %s ct %d" % (hp, hs, hct, cp, cs, cct))
        check("S1 real CLIENT observer: one proxy for (origin A, seq 100): present count 1, one spawn, actor created", len(cs) == 1 and cs[0][2] == "created" and cp and cp[-1][3] == "1")
        check("S1 real HOST also shows exactly one proxy (relay present on the host)", len(hs) == 1 and hs[0][2] == "created" and hp and hp[-1][3] == "1")
        check("S1 the bee object bank loads on the observer: aBEE proxy ct ran (client %d, host %d)" % (cct, hct), cct >= 1 and hct >= 1)
        check("S1 origin id of the relay is A's wire id %s (client present origin %s)" % (aid, cp[-1][0] if cp else None), cp and int(cp[-1][0]) == aid)
        check("S1 B (FakeClient observer) got exactly one relayed 81", len(b.inbox.peek_all(is81, since=mb)) == 1)

        # S2 duplicate spawn of the same seq
        c0, h0 = len(cli.log_text()), len(host.log_text())
        a.send_reliable(bee(1, 100, x0, z0))
        a.send_reliable(bee(1, 100, x0 + 10, z0))
        nap(1.5)
        cp, cs, cct, cg = counts(cli.log_text(), c0)
        hp, hs, hct, hg = counts(host.log_text(), h0)
        check("S2 duplicate SPAWN seq 100 ignored on client (spawn lines %d, count %s)" % (len(cs), [p[3] for p in cp]), len(cs) == 0 and all(p[3] == "1" for p in cp))
        check("S2 duplicate SPAWN seq 100 ignored on host (spawn lines %d)" % len(hs), len(hs) == 0 and all(p[3] == "1" for p in hp))

        # S3 sting then caught -> proxy goes away
        c0 = len(cli.log_text())
        a.send_reliable(bee(2, 100, x0, z0))
        nap(0.5)
        a.send_reliable(bee(3, 100, x0, z0))
        gone = waitlog(cli, r"\[NET\]\[BEE\] proxy gone origin %d -> proxy count 0" % aid, 15.0, c0)
        check("S3 STING then CAUGHT: client proxy gone, count 0", gone)
        # a late/duplicate spawn of the ended seq must stay ignored
        c0 = len(cli.log_text())
        a.send_reliable(bee(1, 100, x0, z0))
        nap(1.5)
        cp, cs, cct, cg = counts(cli.log_text(), c0)
        check("S3b late SPAWN of the already-ended seq 100 ignored (spawn lines %d)" % len(cs), len(cs) == 0 and all(p[3] == "0" for p in cp))

        # S3c ordering: a late SPAWN of an OLDER seq must not replace a live swarm
        c0 = len(cli.log_text())
        a.send_reliable(bee(1, 200, x0, z0))
        nap(1.5)
        a.send_reliable(bee(1, 199, x0, z0))
        a.send_reliable(bee(1, 65500, x0, z0))  # wraparound: 65500 is OLDER than 200 (diff -(236))
        nap(1.5)
        cp, cs, cct, cg = counts(cli.log_text(), c0)
        check("S3c older-seq SPAWNs (199, 65500) did not replace the live seq-200 proxy (spawn lines %s)" % (cs,), len(cs) == 1 and cs[0][1] == "200")
        a.send_reliable(bee(4, 200, x0, z0))
        waitlog(cli, r"\[NET\]\[BEE\] proxy gone origin %d -> proxy count 0" % aid, 15.0, c0)
        # S4 new swarm + DISAPPEAR
        c0 = len(cli.log_text())
        a.send_reliable(bee(1, 101, x0, z0))
        nap(1.5)
        cp, cs, cct, cg = counts(cli.log_text(), c0)
        check("S4 new seq 101 spawns one proxy", len(cs) == 1 and cp and cp[-1][3] == "1")
        a.send_reliable(bee(4, 101, x0, z0))
        gone = waitlog(cli, r"\[NET\]\[BEE\] proxy gone origin %d -> proxy count 0" % aid, 15.0, c0)
        check("S4 DISAPPEAR: proxy gone", gone)

        # S5 owner leaves the scene (the observers' proxy must fade)
        c0 = len(cli.log_text())
        a.send_reliable(bee(1, 102, x0, z0))
        nap(1.5)
        scene(a, SCENE_SHOP0)
        gone = waitlog(cli, r"\[NET\]\[BEE\] proxy gone origin %d -> proxy count 0" % aid, 15.0, c0)
        check("S5 owner changed scene: client proxy gone", gone)
        scene(a, SCENE_FG)
        nap(1.0)

        # S6 owner disconnect
        c0 = len(cli.log_text())
        a.send_reliable(bee(1, 103, x0, z0))
        nap(1.5)
        cp, cs, cct, cg = counts(cli.log_text(), c0)
        check("S6 seq 103 spawned a proxy before the disconnect", len(cs) == 1)
        a.disconnect()
        gone = waitlog(cli, r"\[NET\]\[BEE\] proxy gone origin %d -> proxy count 0" % aid, 20.0, since_offset=c0)
        check("S6 owner disconnected: client proxy gone", gone)

        # S7 malformed relays (a fresh shaker A2 + a not-READY transport peer)
        a2 = L.FakeClient("A2", "127.0.0.1", args.port, player=res[2])
        a2.connect_and_ready()
        scene(a2, SCENE_FG)
        a2.claim_position(x0, y0, z0)
        SH[0], SH[1] = a2, (x0, y0, z0)
        nap(1.5)
        c0, h0 = len(cli.log_text()), len(host.log_text())
        mb = b.inbox.mark()
        good = bee(1, 500, x0, z0)
        bad = [
            ("size 19", good[:19]), ("size 21", good + b"\x00"), ("event 0", bee(0, 501, x0, z0)), ("event 5", bee(5, 502, x0, z0)),
            ("event 255", bee(255, 503, x0, z0)), ("x negative", bee(1, 504, -5, z0)), ("x 32768", bee(1, 505, 32768, z0)), ("z 32768", bee(1, 506, x0, 32768)),
            ("y 8192", bee(1, 507, x0, z0, y=8192)), ("y -4096", bee(1, 508, x0, z0, y=-4096)), ("scene mismatch (9, sender is in 7)", bee(1, 509, x0, z0, scene_id=SCENE_SHOP0)),
            ("scene 255", bee(1, 510, x0, z0, scene_id=255)),
        ]
        for _n, p in bad:
            a2.send_reliable(p)
        x = L.TransportClient("X", "127.0.0.1", args.port)
        x.connect()
        x.send_reliable(good)  # a not-READY peer
        nap(2.5)
        cp, cs, cct, cg = counts(cli.log_text(), c0)
        hp, hs, hct, hg = counts(host.log_text(), h0)
        got = b.inbox.peek_all(is81, since=mb)
        check("S7 none of %d malformed (+1 not-READY) relays reached the client (present lines %d, spawn lines %d)" % (len(bad), len(cp), len(cs)), not cp and not cs)
        check("S7 none reached the host present path (present lines %d) nor B (%d x 81)" % (len(hp), len(got)), not hp and not hs and not got)
        # a client->host message tagged with a forged origin byte must be re-stamped by the host (origin = real sender)
        c0 = len(cli.log_text())
        a2.send_reliable(bee(1, 600, x0, z0, origin=1))  # forged origin 1 (= the real client's own wire id)
        nap(2.0)
        cp, cs, cct, cg = counts(cli.log_text(), c0)
        a2id = a2.assigned_peer_id
        check("S7b forged origin byte is re-stamped to the sender's id %s (client present origins %s)" % (a2id, [p[0] for p in cp]), cp and all(int(p[0]) == a2id for p in cp))
        a2.send_reliable(bee(4, 600, x0, z0))
        waitlog(cli, r"\[NET\]\[BEE\] proxy gone origin %d" % a2id, 15.0, since_offset=c0)

        # S8 the observer's own player state is unchanged
        st1 = pc.state()
        pos1 = pc.pos()
        check("S8 observer's own player state unchanged: main_index/free %s -> %s, position %s -> %s" % (st0, st1, pos0, pos1),
              st1 == st0 and pos0 and pos1 and abs(pos0[0] - pos1[0]) < 60 and abs(pos0[1] - pos1[1]) < 60)
        check("S9 host/client/FakeClients alive", host.alive() and cli.alive() and a2.is_connected() and b.is_connected())
        for c in (a2, x):
            try:
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass
    finally:
        for c in (a, b):
            try:
                c and c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if cli:
            cli.stop()
        host.stop()


if __name__ == "__main__":
    main()
    sys.exit(L.summary_and_exit_code(results))
