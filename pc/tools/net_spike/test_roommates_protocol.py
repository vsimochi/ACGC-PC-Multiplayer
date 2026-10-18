#!/usr/bin/env python3
"""test_roommates_protocol.py - ROOMMATES / MOVE-IN (message 104 ROOMMATE, pc/include/pc_roommates.h) against a REAL listen host driven by scripted FakeClients.

TIER: PROTOCOL TESTED (a real `--host --bootstrap-resident 0 --house-sync` process of the pc\\build64_w3 build; FakeClient residents A, B and guests G1, G2, G3).
Disposable fixture pc\\build64\\bin_fixture4_room_proto (a fresh copy of bin_fixture4 + the exe of pc\\build64_w3; house i belongs to resident i). Ports 41700-41799.
The gyroid is the one that COMES WITH A HOUSE (the vanilla save gyroid beside each house door, fixed spots: town units (51,37) (60,37) (51,44) (60,44)); a client
names it by its house (`house` 0..3, tile / item 0).
  MAIN  C1 the 102 CAPS carries total bit 1 (PC_OFURN_C4_ROOMMATES) at a resident and a guest
        Q1 a QUERY from far away -> RESULT NOT_GYROID; M0 a QUERY that names a tile (the old placed-gyroid form) is malformed: dropped
        Q2 G1 at the gyroid of A's house -> INFO role CAN_REQUEST (holder = A, the owner of that house); Q3 A -> role OWNER (no entries); Q4 B -> RESIDENT
        R1 G1 REQUEST -> OK (roommates.ini); R2 again -> ALREADY; R3 G2 -> OK; R4 B -> NOT_GUEST; R5 A's notice "2 requests"
        D1 G2 ACCEPT -> NOT_OWNER; D2 A's list (keys, online); D3 ACCEPT G1 -> STATUS roommate + notice + PERMS 0x0B + pushes of A's house only
        D4 DENY G2 -> notice + COOLDOWN; D5 G1 -> ROOMMATE
        E1 the ROOMMATE G1 edits A's house: a pickup pair (furniture out of the room into G1's pocket) OWNER_COMMIT -> APPLIED (house seq + 1, G1's record rev + 1)
        E2 A's commit on the OLD base right after -> STALE (two editors: the later one is rolled back, never merged) and A is re-pushed the new house
        E3 G1 places it back -> APPLIED (conservation over house + G1's pockets)
        E4 a non-roommate guest (G2) committing A's house -> NOT_OWNER, rolled back (a FULL record push)
        M1 malformed 104s dropped (no violation); M2 a burst is rate limited
        X1 A REMOVEs G1 -> STATUS none + notice + PERMS 0x08; X2 within the demotion grace G1's last commit is still APPLIED (AC_TEST_ROOM_GRACE_MS 4000);
           X3 after the grace -> NOT_OWNER + the FULL record push (rolled back); X4 G1 asks again -> COOLDOWN
        P1 offline accept of G3, delivered at its reconnect (STATUS + notice once); L1 G3 LEAVE -> A's notice; roommates.ini + .bak
  RS    a host restart: roommates.ini loaded
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

GYROID_UT = [(51, 37), (60, 37), (51, 44), (60, 44)]
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
P_PLACE, P_PICKUP = 1, 2
L = None


def pack(op, house=0, seq=1, key=None, count=None, ux=0, uz=0, item=0, result=0):
    n = (1 if key is not None else 0) if count is None else count
    ents = b""
    for i in range(8):
        k = key if (i == 0 and key is not None) else b"\x00" * 20
        ents += struct.pack(ENT, 0, 0, b"\x00" * 8, k if i < n else b"\x00" * 20, b"\x00\x00")
    return struct.pack(HDR, MSG, op, result, n, ux, uz, item, seq, 0, house, 0, 0, b"\x00" * 8, b"\x00" * 20) + ents


def unpack(p):
    v = struct.unpack_from(HDR, p, 0)
    d = dict(op=v[1], result=v[2], count=v[3], seq=v[7], role=v[8], house=v[9], aux=v[10], owner_name=v[12], owner_key=v[13])
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


def ask(c, op, house, key=None, wait=4.0, **kw):
    _SEQ[0] += 1
    s = _SEQ[0]
    mk = c.inbox.mark()
    c.send_reliable(pack(op, house, s, key, **kw))
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


def at_gyroid(OF, c, h):
    OF.stand_south_of(c, GYROID_UT[h][0], GYROID_UT[h][1])


def first_furniture_cell(img):
    for cell in range(L.HOUSE_CELLS):
        v = L.house_cell(img, 0, 0, cell)
        if (v >> 12) == 3 and (v & 3) == 0:
            return cell, v
    return None, None


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


def main_phase(run, OF, sidecar):
    ck = run.check
    a = run.ready("A", run.r1)
    a.wait_record_synced(30.0)
    b = run.ready("B", run.r2)
    g1 = OF.guest(run, "ROOMG1", 0x7101)
    g2 = OF.guest(run, "ROOMG2", 0x7102)
    ha = run.r1  # house i belongs to resident i in this fixture
    ck("C1 the 102 CAPS total carries bit 1 (roommates) at a resident and a guest (A %s, G1 %s)" % (caps_total(a), caps_total(g1)),
       wait(lambda: caps_total(a) is not None and caps_total(g1) is not None) and caps_total(a) & 2 and caps_total(g1) & 2)
    k1, k2 = L.guest_pid_be(g1.guest), L.guest_pid_be(g2.guest)

    OF.stand_south_of(g1, GYROID_UT[ha][0] + 8, GYROID_UT[ha][1] + 8)
    r = ask(g1, QUERY, ha)
    ck("Q1 a QUERY from 8 tiles away -> RESULT NOT_GYROID (%s)" % (repr(r and (r["op"], r["result"])),), r is not None and r["op"] == RESULT and r["result"] == R_NOT_GYROID)
    at_gyroid(OF, g1, ha)
    off = len(run.log())
    g1.send_reliable(pack(QUERY, ha, 5, ux=GYROID_UT[ha][0], uz=GYROID_UT[ha][1], item=0x15B0))
    L.pump_sleep(1.0)
    ck("M0 a QUERY naming a tile (the old placed-gyroid form) is malformed: dropped and logged", run.n_log(r"malformed ROOMMATE", off) == 1)
    r = ask(g1, QUERY, ha)
    ck("Q2 G1 at the gyroid of house %d -> INFO role CAN_REQUEST, holder = A (the owner of that house) (%s)" % (ha, repr(r and (r["role"], r["owner_name"])),),
       r is not None and r["op"] == INFO and r["role"] == ROLE_CAN and r["owner_name"][:len(a.player.player_name.rstrip(b"\x00 "))] == a.player.player_name.rstrip(b"\x00 "))
    at_gyroid(OF, a, ha)
    r = ask(a, QUERY, ha)
    ck("Q3 A at its own house's gyroid -> OWNER, no entries (%s)" % (repr(r and (r["role"], r["count"])),), r is not None and r["role"] == ROLE_OWNER and r["count"] == 0)
    at_gyroid(OF, b, ha)
    r = ask(b, QUERY, ha)
    ck("Q4 resident B at A's gyroid -> RESIDENT (%s)" % (r and r["role"]), r is not None and r["role"] == ROLE_RESIDENT)

    r = ask(g1, REQUEST, ha)
    ck("R1 G1 REQUEST -> OK (%s)" % (r and r["result"]), r is not None and r["result"] == R_OK)
    txt = open(sidecar, encoding="ascii", errors="replace").read() if os.path.isfile(sidecar) else ""
    ck("R1 roommates.ini holds the pending line", re.search(r"^room = 1 [0-9a-f]{40} %s " % k1.hex(), txt, re.M) is not None)
    r = ask(g1, REQUEST, ha)
    ck("R2 again -> ALREADY (%s)" % (r and r["result"]), r is not None and r["result"] == R_ALREADY)
    at_gyroid(OF, g2, ha)
    r = ask(g2, REQUEST, ha)
    ck("R3 G2 asks too -> OK (%s)" % (r and r["result"]), r is not None and r["result"] == R_OK)
    r = ask(b, REQUEST, ha)
    ck("R4 resident B asks -> NOT_GUEST (%s)" % (r and r["result"]), r is not None and r["result"] == R_NOT_GUEST)
    ok = wait(lambda: any(d["aux"] == ON_REQUEST for d in rm_msgs(a, op=NOTICE)), 14.0)
    n = [d for d in rm_msgs(a, op=NOTICE) if d["aux"] == ON_REQUEST]
    ck("R5 the holder A got ONE notice of the waiting requests (count %s)" % (n and n[0]["seq"]), ok and len(n) == 1 and n[0]["seq"] == 2)

    r = ask(g2, ACCEPT, ha, key=k1)
    ck("D1 G2 tries to ACCEPT G1 -> NOT_OWNER (%s)" % (r and r["result"]), r is not None and r["result"] == R_NOT_OWNER)
    r = ask(a, QUERY, ha)
    ents = r["e"] if r else []
    ck("D2 A's list: G1 and G2 pending, both online (%d)" % len(ents), sorted(e[3] for e in ents if e[0] == ST_PENDING) == sorted([k1, k2]) and all(e[1] == 1 for e in ents))
    mk1 = g1.inbox.mark()
    r = ask(a, ACCEPT, ha, key=k1)
    ck("D3 A ACCEPTs G1 -> OK (%s)" % (r and r["result"]), r is not None and r["result"] == R_OK)
    ok = wait(lambda: any(d["role"] == ROLE_ROOMMATE for d in rm_msgs(g1, mk1, STATUS)), 6.0)
    st = rm_msgs(g1, mk1, STATUS)
    ck("D3 G1 got STATUS roommate of house %d" % ha, ok and st[-1]["house"] == ha)
    ck("D3 G1 got the NOTICE 'accepted'", wait(lambda: any(d["aux"] == GN_ACCEPTED for d in rm_msgs(g1, mk1, NOTICE)), 12.0))
    ck("D3 G1's outdoor-furniture rights are a resident's (PERMS %s)" % perms(g1), wait(lambda: perms(g1) is not None and perms(g1) & (P_PLACE | P_PICKUP) == (P_PLACE | P_PICKUP), 6.0))
    ok = wait(lambda: len(pushes(g1, mk1)) > 0, 10.0)
    ck("D3 G1 receives CANON_PUSHes of house %d ONLY (%s)" % (ha, sorted(set(pushes(g1, mk1)))), ok and sorted(set(pushes(g1, mk1))) == [ha])
    ck("D3 G2 (not a roommate) received no house push", pushes(g2) == [])
    mk2 = g2.inbox.mark()
    r = ask(a, DENY, ha, key=k2)
    ck("D4 A DENIES G2 -> OK", r is not None and r["result"] == R_OK)
    ck("D4 G2 got the NOTICE 'denied'", wait(lambda: any(d["aux"] == GN_DENIED for d in rm_msgs(g2, mk2, NOTICE)), 12.0))
    r = ask(g2, REQUEST, ha)
    ck("D4 G2 asks again -> COOLDOWN (%s)" % (r and r["result"]), r is not None and r["result"] == R_COOLDOWN)
    r = ask(g1, QUERY, ha)
    ck("D5 G1 QUERY -> ROOMMATE (%s)" % (r and r["role"]), r is not None and r["role"] == ROLE_ROOMMATE)

    # ---- editing the shared house
    g1.wait_record_synced(20.0)
    ph = g1.wait_house_push(after=0, house=ha, timeout=8.0)
    img = ph["data"] if ph else None
    cell, fid = first_furniture_cell(img) if img else (None, None)
    ck("E0 G1 holds the pushed image of house %d with a furniture to move (cell %s)" % (ha, cell), cell is not None)
    if cell is None:
        return
    a_base = a.house_base_for(ha)
    pk0 = list(L.record_inventory(g1.rec_local)[0])
    free = pk0.index(0)
    img1 = L.house_set_cell(img, 0, 0, cell, 0)
    pk1 = list(pk0)
    pk1[free] = fid
    _p, conds, wallet = L.record_inventory(g1.rec_local)
    rec1 = L.record_set_inventory(g1.rec_local, pk1, conds, wallet)
    x = g1.commit_house(ha, img1, rec1, base=(ph["session"], ph["seq"]))
    ack = g1.wait_house_ack(x)
    ck("E1 the ROOMMATE's pickup pair (furniture into its pocket %d) -> APPLIED (seq %s)" % (free, ack and ack.house_seq), ack is not None and ack.status == 0)
    ck("E1 host log: OWNER_COMMIT of house %d APPLIED for the guest record" % ha, run.n_log(r"OWNER_COMMIT xfer \d+ house %d APPLIED \(house seq \d+, resident [4-9]" % ha) >= 1)
    xa = a.commit_house(ha, img, a.rec_local, base=a_base)
    acka = a.wait_house_ack(xa)
    ck("E2 the holder's commit on the OLD base right after -> STALE (the later editor is rolled back, never merged) (%s)" % (acka and acka.status),
       acka is not None and acka.status == L.PC_NETGAME_HOUSE_ACK_STALE)
    pa = a.wait_house_push(after=0, house=ha, min_seq=(ack.house_seq if ack else 0), timeout=6.0)
    ck("E2 A is pushed the house WITHOUT the furniture (the roommate's edit)", pa is not None and L.house_cell(pa["data"], 0, 0, cell) == 0)
    L.pump_sleep(1.0)
    img2 = L.house_set_cell(img1, 0, 0, cell, fid)
    pk2 = list(pk1)
    pk2[free] = 0
    x3 = g1.commit_house(ha, img2, L.record_set_inventory(rec1, pk2, conds, wallet))
    ack3 = g1.wait_house_ack(x3)
    ck("E3 G1 places it back -> APPLIED", ack3 is not None and ack3.status == 0)
    nrec = len(g2.rec_pushes)
    x4 = g2.commit_house(ha, img2, g2.rec_local, base=g1.house_base_for(ha))
    ack4 = g2.wait_house_ack(x4)
    ck("E4 a non-roommate guest committing house %d -> NOT_OWNER (%s)" % (ha, ack4 and ack4.status), ack4 is not None and ack4.status == L.PC_NETGAME_HOUSE_ACK_NOT_OWNER)
    ck("E4 ... and is rolled back (a FULL record push)", g2.wait_record_push(after=nrec, timeout=5.0) is not None)

    # ---- malformed / rate
    off = len(run.log())
    for bad in (pack(9, ha), pack(QUERY, ha, count=1, key=k2), pack(QUERY, ha)[:299], pack(QUERY, ha) + b"\x00", pack(INFO, ha), pack(QUERY, 4)):
        g1.send_reliable(bad)
    L.pump_sleep(1.0)
    nbad = run.n_log(r"\[NET\]\[ROOM\] host: peer \d+ (malformed ROOMMATE|ROOMMATE of \d+ bytes)", off)
    ck("M1 6 malformed 104s dropped and logged (%d), no violation" % nbad, nbad == 6 and run.n_log(r"violation", off) == 0)
    L.pump_sleep(2.5)
    mk = g1.inbox.mark()
    base = _SEQ[0]
    for _i in range(30):
        _SEQ[0] += 1
        g1.send_reliable(pack(QUERY, ha, _SEQ[0]))
    L.pump_sleep(4.0)
    got = len([d for d in rm_msgs(g1, mk) if d["op"] in (INFO, RESULT) and d["seq"] > base])
    ck("M2 30 QUERYs back to back -> %d answered (< 30, the rate limit)" % got, 1 <= got < 30 and run.n_log(r"ROOMMATE rate exceeded") >= 1)
    L.pump_sleep(2.5)

    # ---- removal + the commit grace
    mk1 = g1.inbox.mark()
    rb = g1.house_base_for(ha)
    r = ask(a, REMOVE, ha, key=k1)
    ck("X1 A REMOVEs G1 -> OK", r is not None and r["result"] == R_OK)
    ck("X1 G1 got STATUS 'not a roommate'", wait(lambda: any(d["role"] == ROLE_NONE and d["house"] == 0xFF for d in rm_msgs(g1, mk1, STATUS)), 6.0))
    ck("X1 G1 got the NOTICE 'removed'", wait(lambda: any(d["aux"] == GN_REMOVED for d in rm_msgs(g1, mk1, NOTICE)), 12.0))
    ck("X1 G1's rights are a guest's again (PERMS %s)" % perms(g1), wait(lambda: perms(g1) is not None and perms(g1) & (P_PLACE | P_PICKUP) == 0, 6.0))
    ph = g1.wait_house_push(after=0, house=ha, timeout=2.0)
    cur = ph["data"] if ph else img2
    xg = g1.commit_house(ha, cur, g1.rec_local, base=rb)
    ackg = g1.wait_house_ack(xg)
    ck("X2 within the demotion grace G1's last (no-change) commit is still accepted (status %s: APPLIED or STALE, never NOT_OWNER)" % (ackg and ackg.status),
       ackg is not None and ackg.status in (0, L.PC_NETGAME_HOUSE_ACK_STALE))
    L.pump_sleep(5.0)
    nrec = len(g1.rec_pushes)
    xg2 = g1.commit_house(ha, cur, g1.rec_local, base=g1.house_base_for(ha))
    ackg2 = g1.wait_house_ack(xg2)
    ck("X3 after the grace -> NOT_OWNER (%s)" % (ackg2 and ackg2.status), ackg2 is not None and ackg2.status == L.PC_NETGAME_HOUSE_ACK_NOT_OWNER)
    ck("X3 ... rolled back (a FULL record push to the demoted guest)", g1.wait_record_push(after=nrec, timeout=5.0) is not None)
    at_gyroid(OF, g1, ha)
    r = ask(g1, REQUEST, ha)
    ck("X4 G1 asks again -> COOLDOWN (%s)" % (r and r["result"]), r is not None and r["result"] == R_COOLDOWN)

    # ---- offline acceptance + leave
    g3 = OF.guest(run, "ROOMG3", 0x7103)
    k3 = L.guest_pid_be(g3.guest)
    at_gyroid(OF, g3, ha)
    r = ask(g3, REQUEST, ha)
    ck("P1 G3 asks -> OK", r is not None and r["result"] == R_OK)
    g3.disconnect()
    L.pump_sleep(2.0)
    r = ask(a, QUERY, ha)
    e3 = [e for e in (r["e"] if r else []) if e[3] == k3]
    ck("P1 A's list shows G3 pending and OFFLINE", len(e3) == 1 and e3[0][0] == ST_PENDING and e3[0][1] == 0)
    r = ask(a, ACCEPT, ha, key=k3)
    ck("P1 A accepts the OFFLINE G3 -> OK", r is not None and r["result"] == R_OK)
    mk3 = g3.inbox.mark()
    g3.reconnect_and_ready()
    ck("P1 G3 reconnects -> STATUS roommate", wait(lambda: any(d["role"] == ROLE_ROOMMATE for d in rm_msgs(g3, mk3, STATUS)), 10.0))
    ok = wait(lambda: any(d["aux"] == GN_ACCEPTED for d in rm_msgs(g3, mk3, NOTICE)), 15.0)
    L.pump_sleep(3.0)
    ck("P1 ... and the waiting NOTICE 'accepted' exactly once", ok and len([d for d in rm_msgs(g3, mk3, NOTICE) if d["aux"] == GN_ACCEPTED]) == 1)
    mka = a.inbox.mark()
    at_gyroid(OF, g3, ha)
    r = ask(g3, LEAVE, ha)
    ck("L1 G3 moves out -> OK", r is not None and r["result"] == R_OK)
    ck("L1 A got the NOTICE 'moved out'", wait(lambda: any(d["aux"] == ON_LEFT for d in rm_msgs(a, mka, NOTICE)), 12.0))
    ck("L1 roommates.ini and roommates.ini.bak exist", os.path.isfile(sidecar) and os.path.isfile(sidecar + ".bak"))


def rs_phase(run, OF):
    ck = run.check
    ck("RS1 host 2 loaded roommates.ini (%s)" % (re.findall(r"roommates\.ini (?:loaded|none yet)[^\n]*", run.log())[:1],),
       wait(lambda: run.n_log(r"roommates\.ini loaded: [1-9]\d* relation") >= 1, 20.0))
    a = run.ready("A", run.r1)
    at_gyroid(OF, a, run.r1)
    r = ask(a, QUERY, run.r1)
    ck("RS2 A is still the holder of its house's gyroid (role %s)" % (r and r["role"]), r is not None and r["role"] == ROLE_OWNER)


def off_phase(run, OF):
    ck = run.check
    a = run.ready("A", run.r1)
    ck("OFF1 no roommates bit in the 102 CAPS (%s)" % caps_total(a), wait(lambda: caps_total(a) is not None, 10.0) and not (caps_total(a) & 2))
    at_gyroid(OF, a, run.r1)
    r = ask(a, QUERY, run.r1)
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
    for tag, env, body in (("MAIN", {"AC_TEST_ROOM_GRACE_MS": "4000"}, main_phase), ("RS", {}, rs_phase), ("OFF", {"AC_MP_ROOMMATES": "0"}, off_phase)):
        if only and tag not in only:
            continue
        for k in ("AC_TEST_ROOM_GRACE_MS", "AC_MP_ROOMMATES"):
            os.environ.pop(k, None)
        print("=" * 72 + "\n[%s]" % tag)
        port = args.port + {"MAIN": 0, "RS": 1, "OFF": 2}[tag]
        host, ok = start_host(ip, port, tag, results, env)
        run = TP.Run(ip, port, host, results, b"", L.TEST_HOST_RESIDENT, residents[:3])
        try:
            if ok:
                if body is main_phase:
                    body(run, OF, sidecar)
                else:
                    body(run, OF)
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
    for k in ("AC_TEST_ROOM_GRACE_MS", "AC_MP_ROOMMATES"):
        os.environ.pop(k, None)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
