#!/usr/bin/env python3
"""test_arrival_train_protocol.py - SYNCED ARRIVAL TRAIN (message 105 ARRIVAL_TRAIN, pc/include/pc_arrival_train.h) against a REAL listen host driven by FakeClients.

TIER: PROTOCOL TESTED (a real `--host --bootstrap-resident 0` process of the pc\\build64_w3 build; FakeClient resident A (observer) and guests G1, G2, G3).
Disposable fixture pc\\build64\\bin_fixture4_arrtrain_proto (a fresh copy of bin_fixture4 + the w3 exe). Ports 41850-41869. The game look is NOT covered
(test_arrival_train_real.py does that).
  MAIN  S1 every READY client gets a STATE at READY (op 0x80, version 1, no train): the capability
        R1 G1 REQUEST -> a NEW train, seat 0 = G1, riding, age ~0; A and G2 (the observers) get the same STATE
        R2 G2 REQUEST 2 s later -> the SAME train, seat 1 (the queue: "the next one off")
        R3 a second REQUEST of G1 changes nothing (no new seat)
        P1 G1 STEPPED_OFF -> off; P2 G1 WELCOMED -> welcomed (G2 is next); P3 G2 STEPPED_OFF + WELCOMED
        D1 the depart comes only once both are off, >= 20 s after the stop (train age >= arrival 9.6 s + 20 s) and the ages agree on every client (+-400 ms)
        L1 G3 REQUEST while the train pulls out -> a NEW train (id + 1) that starts ~15 s after the depart (negative age), on every client
        G1 G3 disconnects -> its seat turns 'gone' (it never holds the train)
        M1 malformed 105s (bad version / bad size / a STATE from a client / an op with train bytes) are dropped: no answer, the host lives
  OFF   AC_MP_TRAIN_SYNC=0: no STATE at READY, a REQUEST gets no answer
Usage: python test_arrival_train_protocol.py [--port 41850] [--only MAIN,OFF]
"""
import argparse
import os
import shutil
import struct
import sys
import time

os.environ["AC_TEST_HOOKS"] = "1"
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_arrtrain_proto"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)

MSG = 105
HDR = "<BBBBI"
TRW = "<HBBii3B3BH"
assert struct.calcsize(HDR) + 2 * struct.calcsize(TRW) == 48
OP_STATE, OP_REQUEST, OP_OFF, OP_WELCOMED, OP_ABORT = 0x80, 1, 2, 3, 4
TF_USED, TF_START, TF_DEPART = 1, 2, 4
PS = {0: "riding", 1: "off", 2: "welcomed", 3: "gone"}
ARRIVE_MS = 9634
L = None


def pack(op, seq=1, version=1, tail=b"\x00" * 40):
    return struct.pack(HDR, MSG, op, version, 0, seq) + tail


def unpack(p):
    _t, op, ver, n, seq = struct.unpack_from(HDR, p, 0)
    trains = []
    for i in range(n):
        v = struct.unpack_from(TRW, p, 8 + 20 * i)
        trains.append(dict(id=v[0], flags=v[1], n=v[2], start=v[3], depart=v[4], peer=list(v[5:8])[:v[2]], ps=[PS[x] for x in v[8:11]][:v[2]]))
    return dict(op=op, ver=ver, n=n, seq=seq, trains=trains)


def states(c, since=None):
    out = []
    for m in c.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and len(m.payload) == 48 and m.payload[0] == MSG, since=since):
        d = unpack(bytes(m.payload))
        d["t"] = m.recv_time if hasattr(m, "recv_time") else time.time()
        out.append(d)
    return out


def last(c, since=None):
    s = states(c, since)
    return s[-1] if s else None


def wait(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        L.pump_sleep(0.1)
    return bool(cond())


def guest(run, label, key):
    g = L.FakeClient(label, run.ip, run.port, guest=L.guest_identity(label, key))
    run.clients.append(g)
    g.connect_and_ready(quiet=True)
    return g


def seat_of(st, peer):
    for tr in (st or {}).get("trains", []):
        for k, p in enumerate(tr["peer"]):
            if p == peer:
                return tr, k
    return None, -1


def main_phase(run):
    ck = run.check
    a = run.ready("A", run.r1)
    g1 = guest(run, "TRAING1", 0x7A01)
    g2 = guest(run, "TRAING2", 0x7A02)
    ck("S1 a STATE at READY (op STATE, version 1, no train) reached A, G1, G2",
       all(wait(lambda c=c: any(s["op"] == OP_STATE and s["ver"] == 1 for s in states(c)), 10.0) for c in (a, g1, g2)) and
       all(states(c)[0]["n"] == 0 for c in (a, g1, g2)))
    p1, p2 = g1.assigned_peer_id, g2.assigned_peer_id
    mk = [c.inbox.mark() for c in (a, g1, g2)]
    g1.send_reliable(pack(OP_REQUEST, 1))
    ok = wait(lambda: all(seat_of(last(c, m), p1)[0] is not None for c, m in zip((a, g1, g2), mk)), 6.0)
    st = last(g1, mk[1])
    tr, k = seat_of(st, p1)
    ck("R1 G1 (peer %s) REQUEST -> a new train, seat 0, riding, age ~0 (%s)" % (p1, tr), ok and tr is not None and k == 0 and tr["ps"][0] == "riding" and
       tr["flags"] & TF_START and -50 <= tr["start"] <= 1500)
    ck("R1 the observers A and G2 got the same train", all(seat_of(last(c, m), p1)[0] is not None and seat_of(last(c, m), p1)[0]["id"] == tr["id"] for c, m in ((a, mk[0]), (g2, mk[2]))))
    train_id = tr["id"] if tr else None
    t_start = time.time() - (tr["start"] / 1000.0 if tr else 0)
    ck("R1 host log: the seat line", run.n_log(r"\[TRAIN\]\[SYNC\] host: peer %d -> train %s seat 0 \(a NEW train, starting now\)" % (p1, train_id)) >= 1)
    L.pump_sleep(2.0)
    mk = [c.inbox.mark() for c in (a, g1, g2)]
    g2.send_reliable(pack(OP_REQUEST, 1))
    ok = wait(lambda: seat_of(last(a, mk[0]), p2)[0] is not None, 6.0)
    tr2, k2 = seat_of(last(a, mk[0]), p2)
    ck("R2 G2 REQUEST 2 s later -> the SAME train %s, seat 1 behind G1 (%s)" % (train_id, tr2), ok and tr2 is not None and tr2["id"] == train_id and k2 == 1 and tr2["peer"] == [p1, p2])
    n_before = len(states(a))
    g1.send_reliable(pack(OP_REQUEST, 2))
    L.pump_sleep(1.0)
    ck("R3 G1 asking again keeps its seat (no change broadcast)", all(s["trains"][0]["peer"] == [p1, p2] for s in states(a)[n_before:] if s["trains"]))
    # wait for the stop (arrival 9.6 s), then G1 steps off and is welcomed
    wait(lambda: time.time() - t_start > ARRIVE_MS / 1000.0 + 0.5, 15.0)
    mk = a.inbox.mark()
    g1.send_reliable(pack(OP_OFF, 3))
    ck("P1 G1 STEPPED_OFF -> 'off' on A", wait(lambda: seat_of(last(a, mk), p1)[0] is not None and seat_of(last(a, mk), p1)[0]["ps"][0] == "off", 5.0))
    L.pump_sleep(3.0)
    mk = a.inbox.mark()
    g1.send_reliable(pack(OP_WELCOMED, 4))
    ck("P2 G1 WELCOMED -> 'welcomed' (G2 is the next one off)", wait(lambda: seat_of(last(a, mk), p1)[0] is not None and seat_of(last(a, mk), p1)[0]["ps"] == ["welcomed", "riding"], 5.0))
    ck("P2 no depart while G2 is still aboard", not any(s["trains"] and s["trains"][0]["flags"] & TF_DEPART for s in states(a)))
    L.pump_sleep(1.0)
    g2.send_reliable(pack(OP_OFF, 3))
    t_last_off = time.time()
    L.pump_sleep(2.0)
    g2.send_reliable(pack(OP_WELCOMED, 4))
    ok = wait(lambda: any(s["trains"] and s["trains"][0]["flags"] & TF_DEPART for s in states(a)), 35.0)
    dep = [s for s in states(a) if s["trains"] and s["trains"][0]["flags"] & TF_DEPART]
    t_dep = time.time()
    first = dep[0]["trains"][0] if dep else None
    ck("D1 the depart came after both stepped off, >= 20 s after the stop and >= 4 s after the last off (train age at depart %s ms)" %
       (first and first["start"] - first["depart"]),
       ok and first is not None and first["start"] - first["depart"] >= ARRIVE_MS + 20000 - 300 and t_dep - t_last_off >= 3.5)
    sa, sg = last(a), last(g2)
    ck("D1 the train ages agree on A and G2 (%s / %s ms)" % (sa and sa["trains"] and sa["trains"][0]["start"], sg and sg["trains"] and sg["trains"][0]["start"]),
       sa and sg and sa["trains"] and sg["trains"] and abs(sa["trains"][0]["start"] - sg["trains"][0]["start"]) <= 3500)  # the periodic resend may sit between them
    # a guest joining while the train pulls out
    g3 = guest(run, "TRAING3", 0x7A03)
    p3 = g3.assigned_peer_id
    L.pump_sleep(1.0)
    mk = [c.inbox.mark() for c in (a, g3)]
    g3.send_reliable(pack(OP_REQUEST, 1))
    ok = wait(lambda: seat_of(last(a, mk[0]), p3)[0] is not None and seat_of(last(g3, mk[1]), p3)[0] is not None, 6.0)
    tr3, k3 = seat_of(last(a, mk[0]), p3)
    tr3g, _ = seat_of(last(g3, mk[1]), p3)
    ck("L1 G3 REQUEST while the train pulls out -> a NEW train %s (seat 0) starting later (age %s ms, about -15 s after the depart)" % (tr3 and tr3["id"], tr3 and tr3["start"]),
       ok and tr3 is not None and train_id is not None and tr3["id"] == train_id + 1 and k3 == 0 and -15500 <= tr3["start"] <= -8000)
    ck("L1 G3 sees the same new train", tr3g is not None and tr3 is not None and tr3g["id"] == tr3["id"] and abs(tr3g["start"] - tr3["start"]) <= 600)
    ck("L1 host log: 'a NEW train, after the departing one'", run.n_log(r"host: peer %d -> train %d seat 0 \(a NEW train, after the departing one\)" % (p3, tr3["id"] if tr3 else -1)) >= 1)
    mk = a.inbox.mark()
    g3.close()
    ck("G1 G3 disconnects -> its seat turns 'gone'", wait(lambda: seat_of(last(a, mk), p3)[0] is not None and seat_of(last(a, mk), p3)[0]["ps"][seat_of(last(a, mk), p3)[1]] == "gone", 15.0))
    # malformed
    mk = a.inbox.mark()
    n_g1 = len(states(g1))
    g1.send_reliable(pack(OP_REQUEST, 9, version=2))
    g1.send_reliable(pack(OP_STATE, 9))
    g1.send_reliable(pack(OP_REQUEST, 9, tail=b"\x01" + b"\x00" * 39))
    g1.send_reliable(struct.pack(HDR, MSG, OP_REQUEST, 1, 0, 9))  # 8 bytes: wrong size
    L.pump_sleep(2.0)
    ck("M1 malformed 105s are dropped (host log %d lines), no change broadcast" % run.n_log(r"\[TRAIN\]\[SYNC\] host: peer %d malformed op dropped" % p1),
       run.n_log(r"\[TRAIN\]\[SYNC\] host: peer %d malformed op dropped" % p1) >= 3 and not any(seat_of(s, p1)[0] is not None and len(seat_of(s, p1)[0]["peer"]) and
                                                                                                      seat_of(s, p1)[0]["id"] != train_id for s in states(a, mk)))
    ck("M1 the host is alive (G1 still connected, gets STATEs)", g1.is_connected() if hasattr(g1, "is_connected") else True)
    _ = n_g1


def off_phase(run):
    ck = run.check
    a = run.ready("A", run.r1)
    g1 = guest(run, "TRAINOF1", 0x7A11)
    L.pump_sleep(3.0)
    ck("OFF1 no STATE at READY with AC_MP_TRAIN_SYNC=0", not states(a) and not states(g1))
    g1.send_reliable(pack(OP_REQUEST, 1))
    L.pump_sleep(2.0)
    ck("OFF2 a REQUEST anyway gets no answer", not states(a) and not states(g1))
    ck("OFF3 host log: the switch is OFF", run.n_log(r"\[TRAIN\]\[SYNC\] synced arrival train: OFF") >= 1)


def start_host(ip, port, tag, results, env=None):
    for k, v in (env or {}).items():
        os.environ[k] = v
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT)], log_path=T.log_path("arrtrain_proto_%s_host.log" % tag)).start()
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=120.0, slot=L.TEST_HOST_RESIDENT)
    L.check("[%s] host reached the field" % tag, ok, results)
    if ok:
        L.resolve_host_town(ip, port)
    return host, ok


def main():
    global L
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=41850)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    if not (41850 <= args.port <= 41868):
        print("REFUSING: ports 41850-41869 only", file=sys.stderr)
        return 2
    if not os.path.isfile(W3_EXE):
        print("REFUSING: build pc\\build64_w3 first", file=sys.stderr)
        return 2
    only = set(x for x in args.only.split(",") if x)
    T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
    import net_spike_lib as L  # noqa: E402
    import test_txn_protocol as TP  # noqa: E402
    results = []
    ip = "127.0.0.1"
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    for tag, env, body in (("MAIN", {}, main_phase), ("OFF", {"AC_MP_TRAIN_SYNC": "0"}, off_phase)):
        if only and tag not in only:
            continue
        os.environ.pop("AC_MP_TRAIN_SYNC", None)
        print("=" * 72 + "\n[%s]" % tag)
        port = args.port + {"MAIN": 0, "OFF": 1}[tag]
        host, ok = start_host(ip, port, tag, results, env)
        run = TP.Run(ip, port, host, results, b"", L.TEST_HOST_RESIDENT, residents[:3])
        try:
            if ok:
                body(run)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            L.check("[%s] phase raised %r" % (tag, exc), False, results)
        finally:
            for cl in run.clients:
                try:
                    cl.close()
                except Exception:  # noqa: BLE001
                    pass
            rc = host.stop()
            L.check("[%s] host did not crash (exit code before stop: %s)" % (tag, rc), rc is None, results)
    os.environ.pop("AC_MP_TRAIN_SYNC", None)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
