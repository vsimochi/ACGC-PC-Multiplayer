"""test_action_ext_protocol.py - PUPPET ACTION_EXT (96, worker P2) against a REAL host (protocol FakeClients A, B, C; the host's own player idles
in its town field). The host is started with PC_PUPPET_DIAG=1 so its relay / drop / puppet-store lines are visible.

  P1 A (town field) sends a valid ACTION_EXT (LIE_BED, pin 3): B (town field) receives exactly ONE 16-byte copy stamped with A's wire id and
     otherwise byte-identical; A (the sender) and C (announced in the post office) receive none; the host stores it for its own puppet of A
  P2 a forged net_player_id (8 = the host's id) is re-stamped with A's real id
  P3 C enters the town field: A's next message reaches B AND C
  P4 malformed (anim0 157, part table 5, a reserved flag bit, index 7 WAIT, counter 16, a bad face nibble, rsv != 0, 15 / 17 bytes) are dropped:
     nobody receives anything and the host logs the drops
  P5 the host keeps running and A stays connected (B still receives A's MOVE stream afterwards)
  --old: the same traffic against a RELEASED 0.2.1 host (fixture bin_fixture4_p2_old, copied from bin_fixture4_ev_old): the old host drops id 96
     silently (no relay to anyone, no violation / disconnect: A stays connected, the host keeps running)
Run on a disposable fixture: NET_SPIKE_GAME_BIN=<abs pc\\build64\\bin_fixture4_p2_proto> python test_action_ext_protocol.py --port 23110
"""
import argparse
import os
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
os.environ["PC_PUPPET_DIAG"] = "1"  # the host's relay / drop / store diag lines
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_ts_protocol as TS  # noqa: E402

MSG = 96
FMT = "<BBBBBBBBBBBBHBB"
assert struct.calcsize(FMT) == 16
SCENE_FG, SCENE_POST_OFFICE = 7, 14
WAIT, LIE_BED = 7, 26
HOST_ID = 8


def ext(c, since=None):
    return [struct.unpack(FMT, m.payload) for m in c.inbox.peek_all(lambda m: len(m.payload) == 16 and m.payload[0] == MSG, since=since)]


def moves_from(c, pid, since=None):
    return [struct.unpack(L.MOVE_FMT, m.payload) for m in c.inbox.peek_all(lambda m: len(m.payload) == 28 and m.payload[0] == L.PC_NETGAME_MSG_MOVE and m.payload[1] == pid,
                                                                         since=since)]


def ids_seen(c):
    return sorted(set(m.payload[1] for m in c.inbox.peek_all(lambda m: len(m.payload) == 28 and m.payload[0] == L.PC_NETGAME_MSG_MOVE)))


def announce(c, scene, flags, seq):
    c.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, scene, flags, 0, 0, seq))


def pack(idx=LIE_BED, ctr=3, a0=41, a1=41, pt=0, flags=(3 << 1), speed_q=16, frame=1, face=0xFF, variant=0, aux=0, seq=1, rsv=0, pid=0):
    return struct.pack(FMT, MSG, pid, idx, ctr, a0, a1, pt, flags, speed_q, frame, face, variant, aux, seq, rsv)


class Mover:
    def __init__(self, c):
        self.c, self.f = c, 100

    def at(self, x, y, z, main=WAIT, n=2):
        for _ in range(n):
            self.f += 1
            self.c.send_move(self.f, x, y, z, angle=0, speed=0.0, move_state=0, main_index=main, entry_counter=self.f & 0xF)
            L.pump_sleep(0.06)


def phase(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    c = run.ready("C", run.r3)
    announce(a, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 1)
    announce(b, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 1)
    announce(c, SCENE_POST_OFFICE, 0, 1)
    L.pump_sleep(0.6)
    ma, mb, mc = Mover(a), Mover(b), Mover(c)
    a.hub.wait_until(lambda: len(moves_from(a, HOST_ID)) >= 1, 6.0)
    hm = moves_from(a, HOST_ID)
    ck("host MOVE stream visible to A (%d samples)" % len(hm), hm)
    if not hm:
        return
    hx, hy, hz = hm[-1][5], hm[-1][6], hm[-1][7]
    for _ in range(3):
        ma.at(hx + 60.0, hy, hz)
        mb.at(hx + 90.0, hy, hz)
        mc.at(hx + 120.0, hy, hz)
    L.pump_sleep(0.8)
    a.hub.wait_until(lambda: len(ids_seen(a)) >= 3 and len(ids_seen(b)) >= 3, 4.0)
    ids_a, ids_b = ids_seen(a), ids_seen(b)
    a_id = [i for i in ids_b if i != HOST_ID and i not in ids_a]
    ck("A's wire id learned from B's relayed MOVEs: %s (A sees %s, B sees %s)" % (a_id, ids_a, ids_b), len(a_id) == 1)
    if len(a_id) != 1:
        return
    A_ID = a_id[0]

    # P1
    marks = (a.inbox.mark(), b.inbox.mark(), c.inbox.mark())
    hoff = len(run.host.log_text())
    sent = pack(seq=7, aux=0x1234, variant=1)
    a.send_reliable(sent)
    b.hub.wait_until(lambda: len(ext(b, marks[1])) >= 1, 3.0)
    L.pump_sleep(0.8)
    eb = ext(b, marks[1])
    ck("P1 B received exactly one ACTION_EXT (%s)" % eb, len(eb) == 1)
    if eb:
        exp = list(struct.unpack(FMT, sent))
        exp[1] = A_ID
        ck("P1 the relay is stamped with A's id %d and otherwise identical (%s vs %s)" % (A_ID, list(eb[0]), exp), list(eb[0]) == exp)
    ck("P1 the sender A and C (post office) received none (%d / %d)" % (len(ext(a, marks[0])), len(ext(c, marks[2]))), not ext(a, marks[0]) and not ext(c, marks[2]))
    lg = run.host.log_text()[hoff:]
    ck("P1 the host relayed it to 1 peer and stored it for its own puppet of A",
       ("[NET][AEXT] host: from %d idx=26" % A_ID) in lg and "-> 1 peer(s)" in lg and ("player %d action_ext rx main_index=26 counter=3 anim0=41" % A_ID) in lg)

    # P2
    marks = (a.inbox.mark(), b.inbox.mark(), c.inbox.mark())
    a.send_reliable(pack(seq=8, pid=HOST_ID))
    b.hub.wait_until(lambda: len(ext(b, marks[1])) >= 1, 3.0)
    eb = ext(b, marks[1])
    ck("P2 a forged net_player_id 8 is re-stamped with A's id (%s)" % [e[1] for e in eb], len(eb) == 1 and eb[0][1] == A_ID)

    # P3
    announce(c, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 2)
    L.pump_sleep(0.8)
    marks = (a.inbox.mark(), b.inbox.mark(), c.inbox.mark())
    a.send_reliable(pack(idx=65, ctr=4, a0=111, a1=111, flags=0x10, face=0x65, seq=9))
    b.hub.wait_until(lambda: len(ext(b, marks[1])) >= 1 and len(ext(c, marks[2])) >= 1, 3.0)
    L.pump_sleep(0.5)
    ck("P3 C entered the town field: A's next message reaches B and C once each (%d / %d), not A (%d)" % (len(ext(b, marks[1])), len(ext(c, marks[2])), len(ext(a, marks[0]))),
       len(ext(b, marks[1])) == 1 and len(ext(c, marks[2])) == 1 and not ext(a, marks[0]))

    # P4
    marks = (a.inbox.mark(), b.inbox.mark(), c.inbox.mark())
    hoff = len(run.host.log_text())
    bad = [pack(a0=157), pack(pt=5), pack(flags=0x20), pack(idx=WAIT), pack(ctr=16), pack(face=0x86), pack(rsv=1)]
    for p in bad:
        a.send_reliable(p)
    a.send_reliable(pack()[:15])
    a.send_reliable(pack() + b"\x00")
    L.pump_sleep(1.5)
    lg = run.host.log_text()[hoff:]
    ndrop = lg.count("ACTION_EXT dropped (malformed)")
    ck("P4 the %d malformed messages are dropped and logged (%d), the wrong sizes silently" % (len(bad), ndrop), ndrop == len(bad))
    ck("P4 nobody received anything (%d / %d / %d)" % (len(ext(a, marks[0])), len(ext(b, marks[1])), len(ext(c, marks[2]))),
       not ext(a, marks[0]) and not ext(b, marks[1]) and not ext(c, marks[2]))

    # P5
    mk = b.inbox.mark()
    ma.at(hx + 61.0, hy, hz, n=4)
    b.hub.wait_until(lambda: len(moves_from(b, A_ID, mk)) >= 1, 3.0)
    ck("P5 A is still connected (B receives A's MOVE stream: %d)" % len(moves_from(b, A_ID, mk)), moves_from(b, A_ID, mk))
    ck("P5 host still running", run.host.exit_code() is None)


def phase_old(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    announce(a, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 1)
    announce(b, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 1)
    L.pump_sleep(0.6)
    ma, mb = Mover(a), Mover(b)
    a.hub.wait_until(lambda: len(moves_from(a, HOST_ID)) >= 1, 6.0)
    hm = moves_from(a, HOST_ID)
    ck("old host: MOVE stream visible to A (%d samples)" % len(hm), hm)
    if not hm:
        return
    hx, hy, hz = hm[-1][5], hm[-1][6], hm[-1][7]
    for _ in range(3):
        ma.at(hx + 60.0, hy, hz)
        mb.at(hx + 90.0, hy, hz)
    L.pump_sleep(0.8)
    ids_a, ids_b = ids_seen(a), ids_seen(b)
    a_id = [i for i in ids_b if i != HOST_ID and i not in ids_a]
    ck("old host: A's wire id learned (%s)" % a_id, len(a_id) == 1)
    marks = (a.inbox.mark(), b.inbox.mark())
    hoff = len(run.host.log_text())
    for i in range(20):  # a burst of valid + malformed ACTION_EXT, as a 0.2.x client playing would send (and worse)
        a.send_reliable(pack(seq=i + 1, ctr=i & 15))
        a.send_reliable(pack(a0=200))
    a.send_reliable(pack()[:15])
    L.pump_sleep(2.0)
    lg = run.host.log_text()[hoff:]
    ck("old host: nothing relayed (A %d / B %d)" % (len(ext(a, marks[0])), len(ext(b, marks[1]))), not ext(a, marks[0]) and not ext(b, marks[1]))
    ck("old host: no violation / close logged for A", "violation" not in lg and "closed after" not in lg)
    if len(a_id) == 1:
        mk = b.inbox.mark()
        ma.at(hx + 61.0, hy, hz, n=4)
        b.hub.wait_until(lambda: len(moves_from(b, a_id[0], mk)) >= 1, 3.0)
        ck("old host: A still connected (B receives A's MOVE stream: %d)" % len(moves_from(b, a_id[0], mk)), moves_from(b, a_id[0], mk))
    ck("old host: still running", run.host.exit_code() is None)


def run_phase_plain(results, ip, port, tag, body, snap_gci, restore):
    """TS.run_phase without the test-hook flags (a RELEASE host refuses --ts-test-seed-police and exits 2)."""
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot), "--no-house-sync"],
                         log_path=os.path.join(HERE, "ts_protocol_%s_host.log" % tag)).start()
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=host_slot)
    L.check("[%s] host reached genuine field-ready state" % tag, ok, results)
    run = None
    try:
        if ok:
            L.resolve_host_town(ip, port)
            run = TP.Run(ip, port, host, results, snap_gci, host_slot, residents[:3])
            body(run)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("[%s] phase raised %r" % (tag, exc), False, results)
    finally:
        for cl in (run.clients if run is not None else []):
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        L.check("[%s] host did not crash (exit code before stop: %s)" % (tag, rc), rc is None, results)
        restore()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=23110)
    ap.add_argument("--old", action="store_true", help="run the old-host phase (a released 0.2.1 host)")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_p2_"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_p2_<name> copy only", file=sys.stderr)
        return 2
    if not (23000 <= args.port <= 23999):
        print("REFUSING: ports 23000-23999 only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "aext_proto_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    t0 = time.time()
    try:
        if args.old:
            run_phase_plain(results, "127.0.0.1", args.port, "AEXT_OLD", phase_old, snap_gci, restore)
        else:
            TS.run_phase(results, "127.0.0.1", args.port, "AEXT", [], phase, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    print("elapsed %.0f s" % (time.time() - t0))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
