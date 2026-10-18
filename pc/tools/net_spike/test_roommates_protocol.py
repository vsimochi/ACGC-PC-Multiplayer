#!/usr/bin/env python3
"""test_roommates_protocol.py - ROOMMATES / MOVE-IN (message 104 ROOMMATE, pc/include/pc_roommates.h) against a REAL listen host driven by scripted FakeClients.

TIER: PROTOCOL TESTED (a real `--host --bootstrap-resident 0 --house-sync` process of the pc\\build64_w3 build; FakeClient residents A, B and guests G1, G2, G3).
Disposable fixture pc\\build64\\bin_fixture4_room_proto (a fresh copy of bin_fixture4 + the exe of pc\\build64_w3). Ports 41700-41799 only.
The host seeds ONE placed gyroid (AC_TEST_OFURN_SEED=0x15B0,0: placer = the host seed, no resident key = an UNHELD gyroid).
  MAIN  C1 the 102 CAPS carries total bit 1 (PC_OFURN_C4_ROOMMATES) at a resident and a guest
        Q1 a QUERY from far away -> RESULT NOT_GYROID; Q2 a guest at the unheld gyroid -> INFO role NONE (a guest never claims)
        Q3 resident A at the gyroid -> INFO role OWNER (A CLAIMED it: host log, the sidecar rewritten), no entries; Q4 resident B -> role RESIDENT
        R1 G1 -> CAN_REQUEST, REQUEST -> OK, roommates.ini holds a pending line; R2 the same again -> ALREADY; R3 G2 asks too -> OK; R4 resident B asks -> NOT_GUEST
        R5 A (online) gets the holder NOTICE (0x81, count 2) after the arrival delay
        D1 G2 tries to ACCEPT G1 -> NOT_OWNER; D2 A QUERY lists G1 + G2 pending (keys = the guest keys); D3 A ACCEPTs G1 -> OK: G1 gets STATUS roommate of A's house,
           the NOTICE "accepted", the outdoor-furniture PERMS of a resident (0x0B) and CANON_PUSHes of THAT house only; D4 A DENIES G2 -> G2's NOTICE "denied",
           G2 asks again -> COOLDOWN; D5 G1 QUERY -> role ROOMMATE
        M1 malformed 104s (bad op, wrong count, short / long, a client sending a host op) are dropped (logged), no violation, G1 still connected and answered
        M2 rate: 30 QUERYs back to back -> fewer answered (8 per 2 s), the rate logged
        X1 A REMOVEs G1 -> G1 STATUS "not a roommate", NOTICE "removed", PERMS back to a guest's (0x08), no more pushes; X2 G1 asks again at once -> COOLDOWN
        P1 offline: G3 asks, disconnects; A accepts G3 while G3 is offline (INFO shows it offline); G3 reconnects -> STATUS roommate + NOTICE "accepted" (delivered once)
        L1 G3 moves out (LEAVE) -> OK, A gets NOTICE "moved out"; roommates.ini rewritten, roommates.ini.bak present
  RS    a host restart: roommates.ini is loaded (relations kept); the claimed gyroid is still A's (A QUERY = OWNER)
  OFF   AC_MP_ROOMMATES=0: no CAPS bit, a QUERY -> RESULT OFF
Usage: python test_roommates_protocol.py [--port 41710] [--only MAIN,RS,OFF]
"""
import argparse
import os
import re
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

FIXTURE = "bin_fixture4_room_proto"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)

GYROID = 0x15B0
MSG = 104
HDR = "<BBBBBBHIBBBB8s20s"
ENT = "<BB8s20s2s"
assert struct.calcsize(HDR) + 8 * struct.calcsize(ENT) == 300
QUERY, REQUEST, CANCEL, ACCEPT, DENY, REMOVE, LEAVE = 1, 2, 3, 4, 5, 6, 7
INFO, RESULT, STATUS, NOTICE = 16, 17, 18, 19
R_OK, R_NOT_GYROID, R_NOT_OWNER, R_ALREADY, R_COOLDOWN, R_OFF, R_NOT_GUEST = 0, 2, 3, 7, 8, 13, 14
ROLE_NONE, ROLE_OWNER, ROLE_CAN, ROLE_PENDING, ROLE_ROOMMATE, ROLE_BLOCKED, ROLE_RESIDENT = 0, 1, 2, 3, 4, 5, 6
ST_PENDING, ST_ROOMMATE = 1, 2
GN_ACCEPTED, GN_DENIED, GN_REMOVED = 1, 2, 3
ON_REQUEST, ON_LEFT = 0x81, 0x82
P_PLACE, P_PICKUP, P_LEAF = 1, 2, 8


def pack(op, ux=0, uz=0, item=GYROID, seq=1, key=None, count=None, result=0, raw_tail=b""):
    n = (1 if key is not None else 0) if count is None else count
    ents = b""
    for i in range(8):
        k = key if (i == 0 and key is not None) else b"\x00" * 20
        ents += struct.pack(ENT, 0, 0, b"\x00" * 8, k if i < n else b"\x00" * 20, b"\x00\x00")
    return struct.pack(HDR, MSG, op, result, n, ux, uz, item, seq, 0, 0, 0, 0, b"\x00" * 8, b"\x00" * 20) + ents + raw_tail


def unpack(p):
    v = struct.unpack_from(HDR, p, 0)
    d = dict(op=v[1], result=v[2], count=v[3], ux=v[4], uz=v[5], item=v[6], seq=v[7], role=v[8], house=v[9], aux=v[10], owner_name=v[12], owner_key=v[13])
    d["e"] = [struct.unpack_from(ENT, p, struct.calcsize(HDR) + 32 * i) for i in range(v[3])]
    return d


def rm_msgs(c, since=None, op=None):
    out = []
    for m in c.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and len(m.payload) == 300 and m.payload[0] == MSG, since=since):
        d = unpack(bytes(m.payload))
        if op is None or d["op"] == op:
            out.append(d)
    return out


_SEQ = [100]


def ask(c, op, ux, uz, key=None, wait=4.0, item=GYROID):
    _SEQ[0] += 1
    s = _SEQ[0]
    mk = c.inbox.mark()
    c.send_reliable(pack(op, ux, uz, item, s, key))
    end = time.time() + wait
    while time.time() < end:
        r = [d for d in rm_msgs(c, mk) if d["seq"] == s and d["op"] in (INFO, RESULT)]
        if r:
            return r[0]
        L.pump_sleep(0.1)
    return None


def caps_total(c):
    for m in reversed(c.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type == 102 and len(m.payload) == 268)):
        _t, kind, _n, _f, _g, total = struct.unpack_from("<BBBBII", bytes(m.payload), 0)
        if kind == 7:
            return total
    return None


def perms(c):
    p = None
    for m in c.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type == 98 and len(m.payload) == 268):
        _t, kind, _n, f = struct.unpack_from("<BBBB", bytes(m.payload), 0)
        if kind == 6:
            p = f
    return p


def pushes(c, since=None):
    out = []
    for m in c.inbox.peek_all(lambda m: m.msg_type == L.PC_NETGAME_MSG_HOUSE_BEGIN and len(m.payload) == 36, since=since):
        v = struct.unpack(L.HOUSE_BEGIN_FMT, bytes(m.payload))
        if v[1] == L.PC_NETGAME_HOUSE_KIND_CANON_PUSH:
            out.append(v[3])
    return out


def wait(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        L.pump_sleep(0.2)
    return bool(cond())


def start_host(ip, port, tag, results, env=None):
    for k, v in (env or {}).items():
        os.environ[k] = v
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT), "--house-sync"],
                         log_path=T.log_path("room_proto_%s_host.log" % tag)).start()
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=120.0, slot=L.TEST_HOST_RESIDENT)
    L.check("[%s] host reached the field" % tag, ok, results)
    if ok:
        L.resolve_host_town(ip, port)
    return host, ok


def seeded_tile(host, timeout=40.0):
    m = host.wait_for_log(r"seeded placed piece at \((\d+),(\d+)\) item 0x%04X rot \d -> placed" % GYROID, timeout)
    return (int(m.group(1)), int(m.group(2))) if m else None


def main_phase(run, OF, sidecar):
    ck = run.check
    tile = seeded_tile(run.host)
    ck("SEED the host seeded a placed gyroid (AC_TEST_OFURN_SEED) %s" % (tile,), tile is not None)
    if tile is None:
        return
    ux, uz = tile
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    g1 = OF.guest(run, "ROOMG1", 0x7101)
    g2 = OF.guest(run, "ROOMG2", 0x7102)
    ck("C1 the 102 CAPS total carries bit 1 (roommates) at a resident and a guest (A %s, G1 %s)" % (caps_total(a), caps_total(g1)),
       wait(lambda: caps_total(a) is not None and caps_total(g1) is not None) and caps_total(a) & 2 and caps_total(g1) & 2)
    k1, k2 = L.guest_pid_be(g1.guest), L.guest_pid_be(g2.guest)

    OF.stand_south_of(g1, ux + 6, uz + 6)
    r = ask(g1, QUERY, ux, uz)
    ck("Q1 a QUERY from 6 tiles away -> RESULT NOT_GYROID (%s)" % (repr(r and (r["op"], r["result"])),), r is not None and r["op"] == RESULT and r["result"] == R_NOT_GYROID)
    OF.stand_south_of(g1, ux, uz)
    r = ask(g1, QUERY, ux, uz)
    ck("Q2 a guest at the UNHELD gyroid -> INFO role NONE (a guest never claims) (%s)" % (repr(r and (r["op"], r["role"])),), r is not None and r["op"] == INFO and r["role"] == ROLE_NONE)
    off = len(run.log())
    OF.stand_south_of(a, ux, uz)
    r = ask(a, QUERY, ux, uz)
    ck("Q3 resident A at the unheld gyroid -> INFO role OWNER with no entries (%s)" % (repr(r and (r["op"], r["role"], r["count"])),),
       r is not None and r["op"] == INFO and r["role"] == ROLE_OWNER and r["count"] == 0)
    ck("Q3 the host logged the CLAIM (the gyroid is now held by resident %d)" % run.r1, run.n_log(r"the gyroid at \(%d,%d\) is now held by resident %d" % (ux, uz, run.r1), off) == 1)
    house_a = r["house"] if r else None
    OF.stand_south_of(b, ux, uz)
    r = ask(b, QUERY, ux, uz)
    ck("Q4 resident B -> role RESIDENT (holder name = A's) (%s)" % (r and r["role"]), r is not None and r["op"] == INFO and r["role"] == ROLE_RESIDENT)

    # requests
    r = ask(g1, QUERY, ux, uz)
    ck("R1 G1 QUERY -> CAN_REQUEST (%s)" % (r and r["role"]), r is not None and r["role"] == ROLE_CAN)
    r = ask(g1, REQUEST, ux, uz)
    ck("R1 G1 REQUEST -> RESULT OK (%s)" % (r and r["result"]), r is not None and r["op"] == RESULT and r["result"] == R_OK)
    txt = open(sidecar, encoding="ascii", errors="replace").read() if os.path.isfile(sidecar) else ""
    ck("R1 roommates.ini holds the pending line (state 1, guest key)", re.search(r"^room = 1 [0-9a-f]{40} %s " % k1.hex(), txt, re.M) is not None)
    r = ask(g1, REQUEST, ux, uz)
    ck("R2 the same request again -> ALREADY (%s)" % (r and r["result"]), r is not None and r["result"] == R_ALREADY)
    OF.stand_south_of(g2, ux, uz)
    r = ask(g2, REQUEST, ux, uz)
    ck("R3 G2 asks too -> OK (%s)" % (r and r["result"]), r is not None and r["result"] == R_OK)
    r = ask(b, REQUEST, ux, uz)
    ck("R4 resident B asks -> NOT_GUEST (%s)" % (r and r["result"]), r is not None and r["result"] == R_NOT_GUEST)
    ok = wait(lambda: any(d["aux"] == ON_REQUEST for d in rm_msgs(a, op=NOTICE)), 14.0)
    n = [d for d in rm_msgs(a, op=NOTICE) if d["aux"] == ON_REQUEST]
    ck("R5 the online holder A got ONE notice of the waiting requests (count %s)" % (n and n[0]["seq"]), ok and len(n) == 1 and n[0]["seq"] == 2)

    # decisions
    r = ask(g2, ACCEPT, ux, uz, key=k1)
    ck("D1 G2 tries to ACCEPT G1 -> NOT_OWNER (%s)" % (r and r["result"]), r is not None and r["result"] == R_NOT_OWNER)
    r = ask(a, QUERY, ux, uz)
    ents = r["e"] if r else []
    ck("D2 A's list: G1 and G2 pending, keyed by the guest keys, both online (%s)" % [(e[0], e[1], e[3].hex()[:12]) for e in ents],
       r is not None and sorted(e[3] for e in ents if e[0] == ST_PENDING) == sorted([k1, k2]) and all(e[1] == 1 for e in ents))
    mk1 = g1.inbox.mark()
    r = ask(a, ACCEPT, ux, uz, key=k1)
    ck("D3 A ACCEPTs G1 -> OK (%s)" % (r and r["result"]), r is not None and r["result"] == R_OK)
    ok = wait(lambda: any(d["role"] == ROLE_ROOMMATE for d in rm_msgs(g1, mk1, STATUS)), 6.0)
    st = [d for d in rm_msgs(g1, mk1, STATUS)]
    ck("D3 G1 got STATUS roommate of A's house %s (%s)" % (house_a, [(d["role"], d["house"]) for d in st]), ok and st[-1]["house"] == house_a)
    ok = wait(lambda: any(d["aux"] == GN_ACCEPTED for d in rm_msgs(g1, mk1, NOTICE)), 12.0)
    ck("D3 G1 got the NOTICE 'accepted'", ok)
    ck("D3 G1's outdoor-furniture rights are a resident's now (PERMS %s)" % perms(g1), wait(lambda: perms(g1) is not None and perms(g1) & (P_PLACE | P_PICKUP) == (P_PLACE | P_PICKUP), 6.0))
    ok = wait(lambda: len(pushes(g1, mk1)) > 0, 10.0)
    ph = sorted(set(pushes(g1, mk1)))
    ck("D3 G1 receives CANON_PUSHes of A's house ONLY (%s)" % ph, ok and ph == [house_a])
    ck("D3 G2 (not a roommate) received no house push at all", pushes(g2) == [])
    mk2 = g2.inbox.mark()
    r = ask(a, DENY, ux, uz, key=k2)
    ck("D4 A DENIES G2 -> OK (%s)" % (r and r["result"]), r is not None and r["result"] == R_OK)
    ck("D4 G2 got the NOTICE 'denied'", wait(lambda: any(d["aux"] == GN_DENIED for d in rm_msgs(g2, mk2, NOTICE)), 12.0))
    r = ask(g2, REQUEST, ux, uz)
    ck("D4 G2 asks again -> COOLDOWN (%s)" % (r and r["result"]), r is not None and r["result"] == R_COOLDOWN)
    r = ask(g1, QUERY, ux, uz)
    ck("D5 G1 QUERY -> role ROOMMATE (%s)" % (r and r["role"]), r is not None and r["role"] == ROLE_ROOMMATE)

    # malformed / rate
    off = len(run.log())
    for bad in (pack(9, ux, uz), pack(QUERY, ux, uz, count=1, key=k2), pack(QUERY, ux, uz)[:299], pack(QUERY, ux, uz) + b"\x00", pack(INFO, ux, uz),
                pack(QUERY, 3, uz)):
        g1.send_reliable(bad)
    L.pump_sleep(1.0)
    ck("M1 6 malformed 104s were dropped and logged (%d lines), no record violation" % run.n_log(r"\[NET\]\[ROOM\] host: peer \d+ (malformed ROOMMATE|ROOMMATE of \d+ bytes)", off),
       run.n_log(r"\[NET\]\[ROOM\] host: peer \d+ (malformed ROOMMATE|ROOMMATE of \d+ bytes)", off) == 6 and run.n_log(r"violation", off) == 0)
    L.pump_sleep(2.5)
    mk = g1.inbox.mark()
    base = _SEQ[0]
    for i in range(30):
        _SEQ[0] += 1
        g1.send_reliable(pack(QUERY, ux, uz, GYROID, _SEQ[0]))
    L.pump_sleep(4.0)
    got = len([d for d in rm_msgs(g1, mk) if d["op"] in (INFO, RESULT) and d["seq"] > base])
    ck("M2 30 QUERYs back to back -> %d answered (< 30: the rate limit of 8 per 2 s dropped some)" % got, 1 <= got < 30 and run.n_log(r"ROOMMATE rate exceeded") >= 1)
    L.pump_sleep(2.5)
    r = ask(g1, QUERY, ux, uz)
    ck("M1/M2 G1 is still connected and answered", r is not None)

    # removal
    mk1 = g1.inbox.mark()
    r = ask(a, REMOVE, ux, uz, key=k1)
    ck("X1 A REMOVEs G1 -> OK (%s)" % (r and r["result"]), r is not None and r["result"] == R_OK)
    ck("X1 G1 got STATUS 'not a roommate'", wait(lambda: any(d["role"] == ROLE_NONE and d["house"] == 0xFF for d in rm_msgs(g1, mk1, STATUS)), 6.0))
    ck("X1 G1 got the NOTICE 'removed'", wait(lambda: any(d["aux"] == GN_REMOVED for d in rm_msgs(g1, mk1, NOTICE)), 12.0))
    ck("X1 G1's rights are a guest's again (PERMS %s)" % perms(g1), wait(lambda: perms(g1) is not None and perms(g1) & (P_PLACE | P_PICKUP) == 0, 6.0))
    L.pump_sleep(2.0)
    mkp = g1.inbox.mark()
    run.host.send_console("save") if hasattr(run.host, "send_console") else None
    L.pump_sleep(3.0)
    ck("X1 no more house pushes to G1 after the removal", pushes(g1, mkp) == [])
    r = ask(g1, REQUEST, ux, uz)
    ck("X2 G1 asks again at once -> COOLDOWN (%s)" % (r and r["result"]), r is not None and r["result"] == R_COOLDOWN)

    # offline acceptance
    g3 = OF.guest(run, "ROOMG3", 0x7103)
    k3 = L.guest_pid_be(g3.guest)
    OF.stand_south_of(g3, ux, uz)
    r = ask(g3, REQUEST, ux, uz)
    ck("P1 G3 asks -> OK (%s)" % (r and r["result"]), r is not None and r["result"] == R_OK)
    g3.disconnect()
    L.pump_sleep(2.0)
    r = ask(a, QUERY, ux, uz)
    e3 = [e for e in (r["e"] if r else []) if e[3] == k3]
    ck("P1 A's list shows G3 pending and OFFLINE (%s)" % [(e[0], e[1]) for e in e3], len(e3) == 1 and e3[0][0] == ST_PENDING and e3[0][1] == 0)
    r = ask(a, ACCEPT, ux, uz, key=k3)
    ck("P1 A accepts the OFFLINE G3 -> OK (%s)" % (r and r["result"]), r is not None and r["result"] == R_OK)
    mk3 = g3.inbox.mark()
    g3.reconnect_and_ready()
    ok = wait(lambda: any(d["role"] == ROLE_ROOMMATE for d in rm_msgs(g3, mk3, STATUS)), 10.0)
    ck("P1 G3 reconnects -> STATUS roommate", ok)
    ok = wait(lambda: any(d["aux"] == GN_ACCEPTED for d in rm_msgs(g3, mk3, NOTICE)), 15.0)
    L.pump_sleep(3.0)
    ck("P1 ... and the waiting NOTICE 'accepted' exactly once", ok and len([d for d in rm_msgs(g3, mk3, NOTICE) if d["aux"] == GN_ACCEPTED]) == 1)
    mka = a.inbox.mark()
    OF.stand_south_of(g3, ux, uz)
    r = ask(g3, LEAVE, ux, uz)
    ck("L1 G3 moves out (LEAVE) -> OK (%s)" % (r and r["result"]), r is not None and r["result"] == R_OK)
    ck("L1 A got the NOTICE 'moved out'", wait(lambda: any(d["aux"] == ON_LEFT for d in rm_msgs(a, mka, NOTICE)), 12.0))
    ck("L1 roommates.ini and roommates.ini.bak exist", os.path.isfile(sidecar) and os.path.isfile(sidecar + ".bak"))
    # leave G1 as a pending request + G3 removed for the restart check
    r = ask(a, QUERY, ux, uz)
    ck("L1 A's list after everything: no roommate, no request (%s)" % (repr(r and r["e"]),), r is not None and r["count"] == 0)
    return tile


def rs_phase(run, OF, tile):
    """host 2 on the same save dir: roommates.ini is loaded; the gyroid of host 1 survives only if host 1's town save kept its tile, so host 2 seeds one too:
    resident A claims / holds it (OWNER either way, the log says which)"""
    ck = run.check
    ck("RS1 host 2 loaded roommates.ini (log: %s)" % (re.findall(r"roommates\.ini (?:loaded|none yet)[^\n]*", run.log())[:1],),
       wait(lambda: run.n_log(r"roommates\.ini loaded: [1-9]\d* relation") >= 1, 20.0))
    t2 = seeded_tile(run.host) or tile
    a = run.ready("A", run.r1)
    OF.stand_south_of(a, t2[0], t2[1])
    r = None
    end = time.time() + 20.0
    while time.time() < end:
        r = ask(a, QUERY, t2[0], t2[1])
        if r is not None and r["op"] == INFO:
            break
        L.pump_sleep(1.0)
    ck("RS2 resident A holds a gyroid on host 2 (role %s)" % (r and r["role"]), r is not None and r["role"] == ROLE_OWNER)


def off_phase(run, OF, tile):
    ck = run.check
    a = run.ready("A", run.r1)
    ck("OFF1 no roommates bit in the 102 CAPS (%s)" % caps_total(a), wait(lambda: caps_total(a) is not None, 10.0) and not (caps_total(a) & 2))
    if tile:
        OF.stand_south_of(a, tile[0], tile[1])
        r = ask(a, QUERY, tile[0], tile[1])
        ck("OFF2 a QUERY anyway -> RESULT OFF (%s)" % (r and r["result"]), r is not None and r["op"] == RESULT and r["result"] == R_OFF)


def main():
    global L
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=41710)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    if not (41700 <= args.port <= 41780):
        print("REFUSING: ports 41700-41799 only", file=sys.stderr)
        return 2
    if not os.path.isfile(W3_EXE):
        print("REFUSING: build pc\\build64_w3 first", file=sys.stderr)
        return 2
    only = set(x for x in args.only.split(",") if x)
    T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
    import net_spike_lib as L  # noqa: E402
    import test_txn_protocol as TP  # noqa: E402
    import test_ofurn_protocol as OF  # noqa: E402
    results = []
    ip = "127.0.0.1"
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    sidecar = os.path.join(L.GAME_BIN_DIR, "save", "mp", "roommates.ini")
    tile = None
    for tag, env, body in (("MAIN", {"AC_TEST_OFURN_SEED": "0x%04X,0" % GYROID}, "main"), ("RS", {"AC_TEST_OFURN_SEED": "0x%04X,0" % GYROID}, "rs"), ("OFF", {"AC_MP_ROOMMATES": "0"}, "off")):
        if only and tag not in only:
            continue
        for k in ("AC_TEST_OFURN_SEED", "AC_MP_ROOMMATES"):
            os.environ.pop(k, None)
        print("=" * 72 + "\n[%s]" % tag)
        host, ok = start_host(ip, args.port + {"MAIN": 0, "RS": 1, "OFF": 2}[tag], tag, results, env)
        run = TP.Run(ip, args.port + {"MAIN": 0, "RS": 1, "OFF": 2}[tag], host, results, b"", L.TEST_HOST_RESIDENT, residents[:3])
        try:
            if ok:
                if body == "main":
                    tile = main_phase(run, OF, sidecar)
                elif body == "rs":
                    if tile is None:
                        m = re.search(r"\((\d+),(\d+)\) rot 0 .*held|gyroid at \((\d+),(\d+)\) is now held", open(T.log_path("room_proto_MAIN_host.log"), errors="replace").read())
                        tile = (int(m.group(3)), int(m.group(4))) if m and m.group(3) else None
                    if tile:
                        rs_phase(run, OF, tile)
                    else:
                        L.check("RS needs the MAIN phase's gyroid tile", False, results)
                else:
                    off_phase(run, OF, tile)
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
    for k in ("AC_TEST_OFURN_SEED", "AC_MP_ROOMMATES"):
        os.environ.pop(k, None)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
