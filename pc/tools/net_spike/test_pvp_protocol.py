"""test_pvp_protocol.py - player-vs-player tool hits (91 PVP_HIT_REQ client -> host, 92 PVP_HIT host -> the victim only) against a REAL host
(protocol FakeClients A, B, C; the host's own player idles in its town field).

  V1 a request right after entering the town field -> rejected PROTECTED (12) (arrival protection, 10 s)
  V2 a forged request (no swing state in the attacker's MOVE stream) -> NO_SWING (6)
  V3 target C is READY but not in the town field -> SCENE (4); target = the attacker itself / an unknown id -> BAD_TARGET (3)
  V4 target B 100 units away -> RANGE (8); claimed position far from A's MOVE position -> CLAIM_FAR (7); facing away -> CONE (9)
  V5 a valid axe hit A -> B: B receives exactly ONE PVP_HIT (12 B: attacker = A's wire id, tool 1, reaction TUMBLE, speed 7.5, angle away
     from A); A and C receive none
  V6 the same seq replayed -> OLD_SEQ (13); a new seq at once -> RATE (10); a new seq after the rate window but within B's immunity -> IMMUNE (11)
  V7 victim-is-host: A hits the host's player -> accepted, the HOST's own player enters TUMBLE; no PVP_HIT reaches any FakeClient
  V8 malformed: a 15-byte request is ignored, tool 9 -> MALFORMED (1); a client-sent PVP_HIT (92) is ignored; the host keeps running
Run on a disposable fixture: NET_SPIKE_GAME_BIN=<abs pc\\build64\\bin_fixture4_pvp_proto> python test_pvp_protocol.py --port 17320
"""
import argparse
import math
import os
import re
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_ts_protocol as TS  # noqa: E402

MSG_REQ, MSG_HIT = 91, 92
REQ_FMT = "<BBBBHhff"
HIT_FMT = "<BBBBhHf"
assert struct.calcsize(REQ_FMT) == 16 and struct.calcsize(HIT_FMT) == 12
SCENE_FG = 7
AIR_AXE, WAIT = 38, 7
HOST_ID = 8


def hits(c, since=None):
    return [struct.unpack(HIT_FMT, m.payload) for m in c.inbox.peek_all(lambda m: len(m.payload) == 12 and m.payload[0] == MSG_HIT, since=since)]


def moves_from(c, pid, since=None):
    return [struct.unpack(L.MOVE_FMT, m.payload) for m in c.inbox.peek_all(lambda m: len(m.payload) == 28 and m.payload[0] == L.PC_NETGAME_MSG_MOVE and m.payload[1] == pid,
                                                                         since=since)]


def ids_seen(c):
    return sorted(set(m.payload[1] for m in c.inbox.peek_all(lambda m: len(m.payload) == 28 and m.payload[0] == L.PC_NETGAME_MSG_MOVE)))


def enter_town(c, seq=1):
    c.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 0, 0, seq))


class Mover:
    def __init__(self, c):
        self.c, self.f = c, 100

    def at(self, x, y, z, angle=0, main=WAIT, n=2):
        for _ in range(n):
            self.f += 1
            self.c.send_move(self.f, x, y, z, angle=angle, speed=0.0, move_state=0, main_index=main, entry_counter=self.f & 0xF)
            L.pump_sleep(0.06)


def req(c, target, tool, seq, x, z, facing):
    c.send_reliable(struct.pack(REQ_FMT, MSG_REQ, target, tool, 0, seq, facing, x, z))


def judged(run, off, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        m = re.search(r"\[NET\]\[PVP\] host: hit by (\d+) on (\d+) tool (\d+) seq (\d+) (accepted|rejected \(reason (\d+) \w+\))", run.host.log_text()[off:])
        if m:
            return m
        L.pump_sleep(0.1)
    return None


def reason(m):
    return None if m is None else ("OK" if m.group(5) == "accepted" else int(m.group(6)))


def phase(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    c = run.ready("C", run.r3)
    enter_town(a)
    enter_town(b)
    L.pump_sleep(0.6)
    ma, mb, mc = Mover(a), Mover(b), Mover(c)
    # the host's own position from its relayed MOVE stream
    a.hub.wait_until(lambda: len(moves_from(a, HOST_ID)) >= 1, 6.0)
    hm = moves_from(a, HOST_ID)
    ck("host MOVE stream visible to A (%d samples)" % len(hm), hm)
    if not hm:
        return
    hx, hy, hz = hm[-1][5], hm[-1][6], hm[-1][7]
    # A 30 units south of a spot 200 units east of the host, B at that spot, C next to it (C never enters the town field)
    bx, bz = hx + 200.0, hz
    ax, az = bx, bz - 30.0
    mb.at(bx, hy, bz)
    mc.at(bx + 20.0, hy, bz)
    ma.at(ax, hy, az)
    L.pump_sleep(0.8)
    a.hub.wait_until(lambda: len(ids_seen(a)) >= 3, 4.0)
    ids_a, ids_b = ids_seen(a), ids_seen(b)
    a_id = [i for i in ids_b if i != HOST_ID and i not in ids_a]
    b_id = [i for i in ids_a if i != HOST_ID and i not in ids_b]
    c_id = [i for i in ids_a if i in ids_b and i != HOST_ID]
    ck("wire ids learned from the relayed MOVEs: A %s B %s C %s" % (a_id, b_id, c_id), len(a_id) == 1 and len(b_id) == 1 and len(c_id) == 1)
    if not (a_id and b_id and c_id):
        return
    A_ID, B_ID, C_ID = a_id[0], b_id[0], c_id[0]
    seq = [0]

    def attempt(target, tool=1, x=None, z=None, facing=0, main=AIR_AXE, s=None):
        if s is None:
            seq[0] += 1
            s = seq[0]
        ma.at(ax, hy, az, angle=facing, main=main, n=1)
        off = len(run.host.log_text())
        req(a, target, tool, s, ax if x is None else x, az if z is None else z, facing)
        return judged(run, off)

    # V1 arrival protection (A and B entered the town field < 10 s ago)
    r = attempt(B_ID)
    ck("V1 a hit right after the arrival is rejected PROTECTED (12): %s" % (r.group(0) if r else None), reason(r) == 12)
    t_wait = 10.8
    L.info("waiting %.1f s for the arrival protection" % t_wait)
    end = time.time() + t_wait
    while time.time() < end:
        ma.at(ax, hy, az, n=1)
        mb.at(bx, hy, bz, n=1)
        L.pump_sleep(0.4)
    # V2 forged: no swing state
    r = attempt(B_ID, main=WAIT)
    ck("V2 forged request (A's MOVE shows WAIT) -> NO_SWING (6): %s" % (r.group(0) if r else None), reason(r) == 6)
    # V3 scene / bad targets
    r = attempt(C_ID)
    ck("V3 target C not in the town field -> SCENE (4): %s" % (r.group(0) if r else None), reason(r) == 4)
    r = attempt(A_ID)
    ck("V3 target = the attacker itself -> BAD_TARGET (3): %s" % (r.group(0) if r else None), reason(r) == 3)
    r = attempt(200)
    ck("V3 target = unknown id 200 -> BAD_TARGET (3): %s" % (r.group(0) if r else None), reason(r) == 3)
    # V4 range / claim / cone
    mb.at(bx, hy, bz + 100.0)
    L.pump_sleep(0.3)
    r = attempt(B_ID)
    ck("V4 B 100 units away -> RANGE (8): %s" % (r.group(0) if r else None), reason(r) == 8)
    mb.at(bx, hy, bz)
    L.pump_sleep(0.3)
    r = attempt(B_ID, x=ax + 90.0)
    ck("V4 claimed position 90 units off A's MOVE -> CLAIM_FAR (7): %s" % (r.group(0) if r else None), reason(r) == 7)
    r = attempt(B_ID, facing=-32768)
    ck("V4 A facing away from B -> CONE (9): %s" % (r.group(0) if r else None), reason(r) == 9)
    # V5 valid
    ma_mark, mb_mark, mc_mark = a.inbox.mark(), b.inbox.mark(), c.inbox.mark()
    r = attempt(B_ID)
    ck("V5 a valid axe hit A -> B accepted: %s" % (r.group(0) if r else None), reason(r) == "OK")
    good_seq = seq[0]
    t_acc = time.time()
    # V6 (timed from the accept): a new seq at once -> RATE, the accepted seq replayed -> OLD_SEQ, after the 700 ms rate window but inside
    # B's 2 s immunity -> IMMUNE
    r = attempt(B_ID)
    ck("V6 a new seq at once -> RATE (10): %s" % (r.group(0) if r else None), reason(r) == 10)
    r = attempt(B_ID, s=good_seq)
    ck("V6 the same seq replayed -> OLD_SEQ (13): %s" % (r.group(0) if r else None), reason(r) == 13)
    L.pump_sleep(max(0.0, 0.95 - (time.time() - t_acc)))
    r = attempt(B_ID)
    ck("V6 after the rate window (%.2f s), within B's 2 s immunity -> IMMUNE (11): %s" % (time.time() - t_acc, r.group(0) if r else None), reason(r) == 11)
    b.hub.wait_until(lambda: len(hits(b, mb_mark)) >= 1, 3.0)
    L.pump_sleep(0.8)
    hb = hits(b, mb_mark)
    ck("V5 B received exactly one PVP_HIT (%s)" % hb, len(hb) == 1)
    if hb:
        h = hb[0]
        ck("V5 PVP_HIT: attacker %d (A=%d), tool %d, reaction %d (TUMBLE), speed %.2f, angle %d (away from A = 0 +- 2 deg)" % (h[1], A_ID, h[2], h[3], h[6], h[4]),
           h[1] == A_ID and h[2] == 1 and h[3] == 1 and abs(h[6] - 7.5) < 0.01 and abs(h[4]) <= 400)
    ck("V5 A and C received no PVP_HIT (%d / %d)" % (len(hits(a, ma_mark)), len(hits(c, mc_mark))), not hits(a, ma_mark) and not hits(c, mc_mark))
    L.pump_sleep(2.5)
    # V7 victim-is-host
    ax2, az2 = hx, hz - 30.0
    for _ in range(6):
        ma.at(ax2, hy, az2, n=1)
        L.pump_sleep(0.1)
    L.pump_sleep(0.6)
    marks = (a.inbox.mark(), b.inbox.mark(), c.inbox.mark())
    hoff = len(run.host.log_text())
    seq[0] += 1
    ma.at(ax2, hy, az2, angle=0, main=AIR_AXE, n=1)
    req(a, HOST_ID, 1, seq[0], ax2, az2, 0)
    r = judged(run, hoff)
    ck("V7 A -> the host's player accepted: %s" % (r.group(0) if r else run.host.log_text()[hoff:][-300:]), reason(r) == "OK")
    ent = run.host.wait_for_log(r"\[NET\]\[PVP\] victim: (TUMBLE|SHOCK) (entered|NOT entered)[^\r\n]*", 4.0, hoff)
    ck("V7 the host's own player entered TUMBLE (%s)" % (ent.group(0) if ent else None), ent is not None and ent.group(1) == "TUMBLE" and ent.group(2) == "entered")
    L.pump_sleep(0.5)
    ck("V7 no FakeClient received a PVP_HIT", not hits(a, marks[0]) and not hits(b, marks[1]) and not hits(c, marks[2]))
    # V8 malformed
    off = len(run.host.log_text())
    a.send_reliable(struct.pack(REQ_FMT, MSG_REQ, B_ID, 1, 0, 999, 0, ax, az)[:15])
    seq[0] += 1
    req(a, B_ID, 9, seq[0], ax, az, 0)
    a.send_reliable(struct.pack(HIT_FMT, MSG_HIT, A_ID, 1, 1, 0, 1, 7.5))
    L.pump_sleep(1.0)
    lg = run.host.log_text()[off:]
    ck("V8 tool 9 -> MALFORMED (1), the 15-byte request and the client-sent PVP_HIT are ignored (log %s)" % re.findall(r"host: hit by[^\r\n]*", lg),
       len(re.findall(r"rejected \(reason 1 ", lg)) == 1 and "accepted" not in lg)
    ck("V8 nobody received a PVP_HIT from the malformed traffic", not hits(b, mb_mark)[1:])
    ck("V8 host still running", run.host.exit_code() is None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=17320)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_<name> copy only", file=sys.stderr)
        return 2
    if not (17000 <= args.port <= 17999):
        print("REFUSING: ports 17000-17999 only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "pvp_proto_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        TS.run_phase(results, "127.0.0.1", args.port, "PVP", [], phase, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
