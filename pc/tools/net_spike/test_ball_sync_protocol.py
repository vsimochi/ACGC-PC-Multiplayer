"""test_ball_sync_protocol.py - host-authoritative soccer ball / basketball (msgs 79 BALL_STATE / 80 BALL_KICK_REQUEST) against a REAL host (protocol FakeClients).

The host boots into its town field (it has the ball actor). FakeClient A announces the town field (PLAYER_SCENE 7 + IN_TOWN).
  B1 A receives BALL_STATE (32 bytes, UNRELIABLE) with a valid ball position (resting cadence ~1 Hz)
  B2 A sends MOVE next to the ball, then a KICK_REQUEST from there: the host logs 'kick from peer N accepted', and the following
     BALL_STATEs show the host-simulated ball moving (speed != 0 / position changed): kick simulated on the host AND replicated
  B3 a second kick within 0.5 s -> rejected (reason 7 RATE); a replayed old seq -> rejected (8 OLD_SEQ)
  B4 a kick claiming a position far from A's MOVE position -> rejected (5 FAR_SENDER); a malformed 23-byte kick -> ignored (no accept line)
  B5 FakeClient B READY but WITHOUT a town scene announcement: receives no BALL_STATE, its kick is rejected (1 BAD_PEER)
  B6 a BALL_STATE sent by a client to the host is ignored (no crash, host keeps running)
Run on a disposable fixture: NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_w2> python test_ball_sync_protocol.py --port 16210
"""
import argparse
import math
import os
import re
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_ts_protocol as TS  # noqa: E402

MSG_STATE, MSG_KICK = 79, 80
STATE_FMT = "<BBBBHh3f3f"
KICK_FMT = "<BBH3f2f"
assert struct.calcsize(STATE_FMT) == 32 and struct.calcsize(KICK_FMT) == 24
SCENE_FG = 7


def is_state(m):
    return m.channel == L.CH_UNRELIABLE and len(m.payload) == 32 and m.payload[0] == MSG_STATE


def states(c, since=None):
    out = []
    for m in c.inbox.peek_all(is_state, since=since):
        v = struct.unpack(STATE_FMT, m.payload)
        out.append(dict(flags=v[1], type=v[2], seq=v[4], pos=v[6:9], spd=v[9:12]))
    return out


def enter_town(c, seq=1):
    c.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 0, 0, seq))
    L.pump_sleep(0.6)


def kick(c, seq, x, y, z, sx, sz):
    c.send_reliable(struct.pack(KICK_FMT, MSG_KICK, 0, seq, x, y, z, sx, sz))


def phase(run):
    ck = run.check
    a = run.ready("A", run.r1)
    enter_town(a)
    mark = a.inbox.mark()
    a.hub.wait_until(lambda: len(states(a, mark)) >= 2, 4.0)
    st = states(a, mark)
    ck("B1 A receives BALL_STATE in the town field (%d states, first %s)" % (len(st), st[0] if st else None), len(st) >= 2)
    if not st:
        return
    s0 = st[-1]
    ck("B1 the state is a live host ball (flags %d, type %d)" % (s0["flags"], s0["type"]), (s0["flags"] & 1) == 1 and s0["type"] < 3)
    bx, by, bz = s0["pos"]
    # stand 15 units west of the ball, moving east into it
    px, pz = bx - 15.0, bz
    for f in range(4):
        a.send_move(100 + f, px, by, pz, speed=3.0)
        L.pump_sleep(0.1)
    off = len(run.log())
    mark = a.inbox.mark()
    kick(a, 1, px, by, pz, 4.0, 0.0)
    a.hub.wait_until(lambda: len(states(a, mark)) >= 4, 3.0)
    L.pump_sleep(0.5)
    acc = re.findall(r"\[NET\]\[BALL\] host: kick from peer (\d+) accepted", run.log(off))
    ck("B2 the host accepted A's kick (%s)" % acc, len(acc) == 1)
    st2 = states(a, mark)
    moved = [s for s in st2 if math.hypot(s["spd"][0], s["spd"][2]) > 0.05 or math.hypot(s["pos"][0] - bx, s["pos"][2] - bz) > 2.0]
    ck("B2 the host simulated the kick and replicated it (%d/%d states moving; last pos %s from %s)" % (len(moved), len(st2), st2[-1]["pos"] if st2 else None, (bx, bz)),
       len(moved) >= 1)
    ck("B2 the ball moved east (away from the kicker): dx %.1f" % ((st2[-1]["pos"][0] - bx) if st2 else 0), st2 and st2[-1]["pos"][0] > bx + 1.0)
    ck("B2 moving cadence ~10 Hz (%d states in ~1 s)" % len(st2), len(st2) >= 4)

    # B3 rate limit + replay
    s1 = (states(a) or [s0])[-1]
    bx, by, bz = s1["pos"]
    px, pz = bx - 15.0, bz
    a.send_move(200, px, by, pz, speed=3.0)
    L.pump_sleep(0.6)
    off = len(run.log())
    kick(a, 2, px, by, pz, 4.0, 0.0)
    kick(a, 3, px, by, pz, 4.0, 0.0)
    L.pump_sleep(0.4)
    lg = run.log(off)
    ck("B3 first of two quick kicks accepted, second rejected RATE (7): %s" % re.findall(r"kick from peer \d+ \w+(?: \(reason \d+\))?", lg),
       len(re.findall(r"kick from peer \d+ accepted", lg)) == 1 and len(re.findall(r"kick from peer \d+ rejected \(reason 7\)", lg)) == 1)
    L.pump_sleep(0.6)
    off = len(run.log())
    kick(a, 2, px, by, pz, 4.0, 0.0)
    L.pump_sleep(0.4)
    # distance checks run before the seq check, and by now the kicked ball has rolled > 40 units away: 6 FAR_BALL or 8 OLD_SEQ
    ck("B3 replayed old seq rejected (8, or 6 when the ball already rolled away)",
       len(re.findall(r"kick from peer \d+ rejected \(reason [68]\)", run.log(off))) == 1 and "accepted" not in run.log(off))

    # B4 far claim + malformed
    off = len(run.log())
    kick(a, 10, px + 300.0, by, pz, 4.0, 0.0)
    L.pump_sleep(0.4)
    a.send_reliable(struct.pack(KICK_FMT, MSG_KICK, 0, 11, px, by, pz, 4.0, 0.0)[:23])
    a.send_reliable(struct.pack(KICK_FMT, MSG_KICK, 0, 12, px, by, pz, 99.0, 0.0))  # speed out of bounds
    L.pump_sleep(0.6)
    lg = run.log(off)
    ck("B4 far-from-sender claim rejected (5)", len(re.findall(r"rejected \(reason 5\)", lg)) == 1)
    ck("B4 out-of-bounds speed rejected (2), 23-byte kick ignored, nothing accepted", len(re.findall(r"rejected \(reason 2\)", lg)) == 1 and "accepted" not in lg)

    # B5 READY but not in the town field
    b = run.ready("B", run.r2)
    mark_b = b.inbox.mark()
    L.pump_sleep(2.5)
    ck("B5 B (no town scene announced) gets no BALL_STATE (%d)" % len(states(b, mark_b)), len(states(b, mark_b)) == 0)
    off = len(run.log())
    kick(b, 1, px, by, pz, 4.0, 0.0)
    L.pump_sleep(0.5)
    ck("B5 B's kick rejected (1)", len(re.findall(r"rejected \(reason 1\)", run.log(off))) == 1)

    # B6 a client-sent BALL_STATE is ignored by the host
    a.send_unreliable(struct.pack(STATE_FMT, MSG_STATE, 1, 0, 0, 999, 0, bx, by, bz, 9.0, 0.0, 0.0))
    L.pump_sleep(1.0)
    ck("B6 host still running after a client-sent BALL_STATE", run.host.proc.poll() is None if hasattr(run.host, "proc") else True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16210)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_<name> copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "ball_sync_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        TS.run_phase(results, "127.0.0.1", args.port, "BALL", [], phase, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
