#!/usr/bin/env python3
"""test_house_upgrade_protocol.py - house upgrades via Nook for resident clients (worker C): TXN_COMMIT kind 20 HOUSE_UPGRADE + the host construction poll, against a REAL
DEDICATED host driven by scripted FakeClients (residents + one guest).

TIER: protocol / harness -- `AnimalCrossing.exe --host <port> --dedicated` (pc\\build64_w3's exe copied into the DISPOSABLE fixture pc\\build64\\bin_fixture4_wlC_hup, a fresh copy
of the read-only bin_fixture4) with AC_TEST_HOOKS=1 + AC_TEST_REHOUSE_DUE=1 (TEST-ONLY: an order placed today is dated one year back, so the host's construction poll builds it
within ~1 s instead of the next real day). The Nook dialogue is NOT driven (no GUI): a FakeClient sends exactly what the dialogue sends (pc_house_upgrade_begin -> kind 20 with the
stale-header guard of its copy of the house, which it takes from the host's CANON_PUSH images) and models the client apply (the loan of an ACK_BUILT) on its local record.
PHASES
  H1  NOT_ELIGIBLE with a loan left (nothing changes); STALE guard -> HOUSE_CHANGED; ORDER_SIZE happy path (canon: next_size + 1, roof palette, order date); an identical resend
      -> APPLIED REPLAYED (no double order); same (nonce, seq) with other bytes -> CONFLICT; a guest -> PRECOND; malformed op -> BAD_SHAPE; the construction is DEFERRED while the
      owner announces a room of its own house and runs once it leaves (one CONSTRUCTED line, canon: size + 1, renew, outlook = ordered); ACK_BUILT -> rev + 1 and the vanilla loan
      (the FakeClient applies it: its next upload is accepted); then every op of the full path up to the statue (pay -> order -> build -> ack ...) for TWO residents, whose
      statue orders are sent at the same moment -> two distinct ranks and num_statues + 2; ACK of the statue; the GCI after a graceful stop holds the houses / loans / num_statues.
  H2  restart: an ORDER sent, the host restarted (the in-memory journal is gone), the IDENTICAL bytes resent -> APPLIED REPLAYED answered from the state (no second order).
Usage: python test_house_upgrade_protocol.py [--port 16310] [--only H1,H2]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_wlC_hup"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")

if __name__ == "__main__":
    T.make_fixture(FIXTURE)
    if os.path.isfile(W3_EXE):
        shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))

import net_spike_lib as L  # noqa: E402

IP = "127.0.0.1"
KIND = 20
OP_SIZE, OP_BASEMENT, OP_STATUE, OP_ACK = 1, 2, 3, 4
R_NOT_ELIGIBLE, R_BUSY, R_CHANGED = 45, 46, 47
SCENE_FG, SCENE_MY_ROOM_S = 7, 20
DEBT = {1: 148000, 2: 398000, 3: 798000}
DEBT_BASEMENT = 49800
NUM_STATUES_GCI = 0x40 + 0x26000 + 0x2068B


def hdr(img):
    """The house header of a BE house image (canon push / GCI)."""
    b4, b5 = img[0x2A], img[0x2B]
    return dict(day=img[0x26], month=img[0x27], year=struct.unpack(">H", bytes(img[0x28:0x2A]))[0], size=(b4 >> 5) & 7, next=(b4 >> 2) & 7, rank=b4 & 3,
                renew=(b5 >> 7) & 1, statue=(b5 >> 6) & 1, bas_ord=(b5 >> 5) & 1, pad1=(b5 >> 4) & 1, has_bas=(img[0x24] >> 4) & 1, outlook=img[0x2C],
                ordered=img[0x2D], next_outlook=img[0x2E])


def guard(h):
    return (h["size"] & 7) | (h["has_bas"] << 3) | (h["renew"] << 4) | (h["bas_ord"] << 5) | (h["statue"] << 6) | ((1 if h["next"] == h["size"] else 0) << 7)


def wait_for(pred, timeout, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        L.pump_sleep(step)
    return pred()


class Rig:
    _n = [0]

    def __init__(self, results):
        self.results = results
        self.ck = lambda d, c: L.check(d, bool(c), results)
        self.clients = []
        self.port = None

    def start(self, tag, port, env_extra):
        env = dict(os.environ, AC_TEST_HOOKS="1", **env_extra)
        h = L.HostProcess(port=port, extra_args=["--dedicated"], log_path=T.log_path("hup_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR, stdin_pipe=True, env=env).start()
        orig = h.log_text
        h.log_text = lambda: orig().replace("\r\n", "\n")
        self.port = port
        if not os.path.isfile(L.server_gci()) and h.wait_for_log(r"\[1\] Use this town", 60.0) is not None:
            h.send_line("1")
        ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
        self.ck("[%s] dedicated host booted" % tag, ok)
        if ok:
            L.resolve_host_town(IP, port)
        return h, ok

    def stop(self, h):
        h.send_line("stop")
        return h.wait_exit(60.0)

    def join_resident(self, label, slot):
        Rig._n[0] += 1
        c = L.FakeClient(label, IP, self.port, player=L.resident_player(slot), bind_ip="127.0.%d.%d" % (110 + Rig._n[0] // 200, 2 + Rig._n[0] % 200), record_wait=True)
        c.rec_resident_idx = slot
        c.connect_and_ready(quiet=True)
        c.hub.wait_until(lambda: len({p["house"] for p in c.house_pushes_of()}) >= 1, 8.0)
        self.clients.append(c)
        return c

    def join_guest(self, label, g, rec):
        Rig._n[0] += 1
        c = L.FakeClient(label, IP, self.port, guest=g, guest_record_img=rec, bind_ip="127.0.%d.%d" % (110 + Rig._n[0] // 200, 2 + Rig._n[0] % 200), record_wait=True)
        c.connect_and_ready(quiet=True)
        self.clients.append(c)
        return c

    def closeall(self):
        for c in list(self.clients):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients = []


class Res:
    """One resident FakeClient + its house."""

    def __init__(self, rig, c, slot):
        self.rig, self.c, self.slot = rig, c, slot
        self.house = None
        for hh in range(4):
            img = c.house_canon(hh)
            if img is not None and L.house_owner_pid(img)[1] == c.player.player_id and L.house_owner_pid(img)[0] == bytes(c.player.player_name):
                self.house = hh
        self.rid = 0x500 + slot * 0x100

    def canon(self):
        img = self.c.house_canon(self.house)
        return hdr(img) if img is not None else None

    def wait_canon(self, pred, timeout=8.0):
        ok = wait_for(lambda: self.canon() is not None and pred(self.canon()), timeout)
        return ok, self.canon()

    def loan(self):
        rec = self.c.rec_local if self.c.rec_local is not None else self.c.own_record()
        return struct.unpack_from(">I", rec, L.REC_OFF_LOAN)[0]

    def set_loan(self, v):
        rec = self.c.rec_local if self.c.rec_local is not None else self.c.own_record()
        self.c.rec_local = L.record_set_u32(rec, L.REC_OFF_LOAN, v)
        ack = self.c.upload_local_record()
        return ack is not None and ack.status == L.PC_NETGAME_REC_ACK_APPLIED

    def send(self, op, arg=0, g=None, **kw):
        self.rid += 1
        h = self.canon()
        sent = self.c.send_txn_commit(KIND, self.rid, L.PC_NETGAME_TXN_DEST_NONE, 0, 0, flags=guard(h) if g is None else g, aux_cond=op, aux_item=arg, **kw)
        return sent

    def txn(self, op, arg=0, g=None, timeout=6.0, **kw):
        sent = self.send(op, arg, g, **kw)
        return sent, self.c.wait_txn_result(sent.seq, timeout=timeout)

    def apply_ack(self, before):
        """The client apply of an APPLIED ACK_BUILT (pcnetgame_txn_apply_house_upgrade): the vanilla next loan into the local record."""
        if before["renew"]:
            v = DEBT_BASEMENT if before["bas_ord"] else DEBT.get(before["size"], 0)
            rec = self.c.rec_local if self.c.rec_local is not None else self.c.own_record()
            self.c.rec_local = L.record_set_u32(rec, L.REC_OFF_LOAN, v)
            return v
        return None


def next_op(h, loan):
    if h["renew"] or (h["statue"] and h["next"] == 4):
        return "ack"
    if loan:
        return "pay"
    if h["size"] < 3 and h["size"] == h["next"] and not h["bas_ord"]:
        return "basement" if (h["size"] == 2 and not h["has_bas"]) else "size"
    if h["size"] == 3 and h["next"] == 3 and not h["statue"]:
        return "statue"
    if h["size"] != h["next"] or h["bas_ord"] or (h["statue"] and h["next"] != 4):
        return "wait"
    return "done"


def advance(rig, r, until, log, max_steps=40):
    """Drives resident r through the vanilla path until next_op() == `until` (or a failure). Returns True when reached."""
    ck = rig.ck
    for _ in range(max_steps):
        h = r.canon()
        op = next_op(h, r.loan())
        if op == until:
            return True
        if op == "ack":
            before = dict(h)
            _s, res = r.txn(OP_ACK)
            if res is None or res.outcome != L.PC_NETGAME_TXN_OUTCOME_APPLIED:
                ck("%s ACK_BUILT applied (house %d, got %r)" % (log, r.house, None if res is None else (res.outcome, res.reason)), False)
                return False
            r.apply_ack(before)
            ok, _h2 = r.wait_canon(lambda x: not x["renew"] and not (x["statue"] and before["statue"] and before["next"] == 4 and x["statue"]))
            if not ok:
                ck("%s canon after ACK (house %d)" % (log, r.house), False)
                return False
        elif op == "pay":
            if not r.set_loan(0):
                ck("%s paying the loan (upload)" % log, False)
                return False
        elif op in ("size", "basement", "statue"):
            o = {"size": OP_SIZE, "basement": OP_BASEMENT, "statue": OP_STATUE}[op]
            _s, res = r.txn(o, 3 if o == OP_SIZE else 0)
            if res is None or res.outcome != L.PC_NETGAME_TXN_OUTCOME_APPLIED:
                ck("%s %s order applied (house %d, got %r)" % (log, op, r.house, None if res is None else (res.outcome, res.reason)), False)
                return False
            if not r.wait_canon(lambda x: next_op(x, 0) != op)[0]:
                ck("%s canon after the %s order (house %d)" % (log, op, r.house), False)
                return False
        elif op == "wait":
            if not r.wait_canon(lambda x: next_op(x, r.loan()) != "wait", 12.0)[0]:
                ck("%s the construction of house %d happened (AC_TEST_REHOUSE_DUE)" % (log, r.house), False)
                return False
        else:
            return op == until
    return False


def run_h1(rig, args, ctx):
    ck = rig.ck
    h, ok = rig.start("h1", args.port, {"AC_TEST_REHOUSE_DUE": "1"})
    if not ok:
        return
    try:
        a = Res(rig, rig.join_resident("A", 1), 1)
        b = Res(rig, rig.join_resident("B", 2), 2)
        ck("H1 (setup) both residents own a house and got its canon (A house %s, B house %s)" % (a.house, b.house), a.house is not None and b.house is not None)
        if a.house is None or b.house is None:
            return
        ha = a.canon()
        print("[H1] A house %d header %r loan %d; B house %d header %r loan %d" % (a.house, ha, a.loan(), b.house, b.canon(), b.loan()))
        # bring A to a state where a size order is the next vanilla step (pay a pending loan / ack / wait), then put a loan back for NOT_ELIGIBLE
        ck("H1 (setup) A reaches 'an order can be placed'", advance(rig, a, "size", "H1 setup A") or next_op(a.canon(), a.loan()) in ("basement", "statue"))
        first = next_op(a.canon(), 0)
        op_first = {"size": OP_SIZE, "basement": OP_BASEMENT, "statue": OP_STATUE}.get(first, OP_SIZE)
        ck("H1 (setup) a loan of 5000 uploaded", a.set_loan(5000))
        n0 = len(h.log_text())
        _s, res = a.txn(op_first, 3 if op_first == OP_SIZE else 0)
        ck("H1 NOT_ELIGIBLE (45) with a loan left; nothing changed", res is not None and res.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and res.reason == R_NOT_ELIGIBLE and
           "NOT ELIGIBLE" in h.log_text()[n0:])
        ck("H1 (setup) loan paid (0 uploaded)", a.set_loan(0))
        g_good = guard(a.canon())
        _s, res = a.txn(op_first, 3 if op_first == OP_SIZE else 0, g=g_good ^ 0x01)
        ck("H1 a stale header guard -> HOUSE_CHANGED (47)", res is not None and res.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and res.reason == R_CHANGED)
        _s, res = a.txn(9)
        ck("H1 an unknown op -> BAD_SHAPE (11)", res is not None and res.reason == L.PC_NETGAME_TXN_REASON_BAD_SHAPE)
        before = a.canon()
        n0 = len(h.log_text())
        sent, res = a.txn(op_first, 3 if op_first == OP_SIZE else 0)
        ck("H1 %s happy path: APPLIED" % first, res is not None and res.outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED and res.reason == 0)
        ck("H1 the host logged APPLIED and a durable save", re.search(r"\[NET\]\[HOUSEUP\] host: peer \d+ resident 1 house %d \w+ APPLIED \[TXN\].*\(saved durably\)" % a.house,
                                                                    h.log_text()[n0:]) is not None)
        ok, after = a.wait_canon(lambda x: x != before)
        if op_first == OP_SIZE:
            ck("H1 the owner's canon shows the order: next_size %d -> %d, ordered roof 3" % (before["next"], before["next"] + 1),
               ok and after["next"] == before["size"] + 1 and after["ordered"] == 3)
        else:
            ck("H1 the owner's canon shows the order", ok)
        a.c.resend_txn(sent)
        res2 = a.c.wait_txn_result(sent.seq, timeout=4.0, nth=2)
        ck("H1 an identical resend -> APPLIED REPLAYED (journal), no second order", res2 is not None and res2.outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED and
           res2.reason == L.PC_NETGAME_TXN_REASON_REPLAYED and h.log_text().count("resident 1 house %d" % a.house) >= 1)
        conf = a.c.send_txn_commit(KIND, a.rid, L.PC_NETGAME_TXN_DEST_NONE, 0, 0, seq=sent.seq, flags=guard(before), aux_cond=op_first, aux_item=2 if op_first == OP_SIZE else 0)
        res3 = a.c.wait_txn_result(conf.seq, timeout=4.0, nth=3)
        ck("H1 the same (nonce, seq) with other bytes -> CONFLICT (7)", res3 is not None and res3.reason == L.PC_NETGAME_TXN_REASON_CONFLICT)
        # the construction is deferred while the owner announces a room of its own house
        a.c.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, SCENE_MY_ROOM_S, 0, a.house, 0, 900))
        ok = wait_for(lambda: re.search(r"house %d construction is due \(\w+\) but DEFERRED: a player is inside the house" % a.house, h.log_text()) is not None, 8.0)
        ck("H1 the construction is DEFERRED while the owner is inside its house", ok and "house %d CONSTRUCTED" % a.house not in h.log_text())
        a.c.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 0, 0, 901))
        ok = wait_for(lambda: "house %d CONSTRUCTED" % a.house in h.log_text(), 8.0)
        ck("H1 ... and runs once the owner left (one CONSTRUCTED line, saved durably)", ok and h.log_text().count("house %d CONSTRUCTED" % a.house) == 1 and
           re.search(r"house %d CONSTRUCTED.*saved durably" % a.house, h.log_text()) is not None)
        ok, built = a.wait_canon(lambda x: x["renew"] == 1)
        if op_first == OP_SIZE:
            ck("H1 the owner's canon after the construction: size %d, renew, outlook = the ordered roof 3" % (before["size"] + 1),
               ok and built["size"] == before["size"] + 1 and built["renew"] == 1 and built["outlook"] == 3)
        L.pump_sleep(2.5)
        ck("H1 exactly one construction (no repeat on later polls)", h.log_text().count("house %d CONSTRUCTED" % a.house) == 1)
        if built is not None and built["renew"]:
            rev0 = a.c.rec_last[2] if a.c.rec_last else 0
            sent, res = a.txn(OP_ACK)
            want = DEBT_BASEMENT if built["bas_ord"] else DEBT.get(built["size"], 0)
            ck("H1 ACK_BUILT APPLIED with rev + 1 (the mirror loan changed)", res is not None and res.outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED and res.rev == rev0 + 1)
            ck("H1 the host set the vanilla next loan %d" % want, re.search(r"ACK_BUILT APPLIED \[TXN\].* loan %d \(rev \+ 1\)" % want, h.log_text()) is not None)
            a.apply_ack(built)
            ok, _x = a.wait_canon(lambda x: x["renew"] == 0)
            ck("H1 the canon after the ACK: renew cleared", ok)
            ck("H1 the client's record with the applied loan is accepted by the host (upload APPLIED: digests agree)", a.set_loan(want))
        # guest: PRECOND
        gc = rig.join_guest("G", ctx["g"], ctx["rec"])
        gs = gc.send_txn_commit(KIND, 77, L.PC_NETGAME_TXN_DEST_NONE, 0, 0, flags=0x80, aux_cond=OP_SIZE, aux_item=1)
        gr = gc.wait_txn_result(gs.seq, timeout=4.0)
        ck("H1 a GUEST -> PRECOND (9)", gr is not None and gr.reason == L.PC_NETGAME_TXN_REASON_PRECOND)
        # the full path for A and B up to the statue order, then both statue orders at the same moment
        ra = advance(rig, a, "statue", "H1 A path")
        rb = advance(rig, b, "statue", "H1 B path")
        ck("H1 A and B both reached 'statue order' through every op (pay / order / build / ack)", ra and rb)
        if ra and rb:
            m = re.findall(r"num_statues (\d+)", h.log_text())
            n_before = int(m[-1]) if m else None
            sa = a.send(OP_STATUE)
            sb = b.send(OP_STATUE)
            res_a = a.c.wait_txn_result(sa.seq, timeout=6.0)
            res_b = b.c.wait_txn_result(sb.seq, timeout=6.0)
            ck("H1 both statue orders APPLIED", res_a is not None and res_b is not None and res_a.outcome == 0 and res_b.outcome == 0)
            ranks = [int(x) for x in re.findall(r"ORDER_STATUE APPLIED \[TXN\].* rank (\d+) num_statues", h.log_text())]
            ck("H1 two DISTINCT statue ranks %r (the host serialises num_statues)" % ranks[-2:], len(ranks) >= 2 and ranks[-1] != ranks[-2])
            okA, ca = a.wait_canon(lambda x: x["statue"] == 1)
            okB, cb = b.wait_canon(lambda x: x["statue"] == 1)
            ck("H1 both owners' canons carry the statue order with their rank", okA and okB and ca["rank"] != cb["rank"])
            ok = wait_for(lambda: a.canon()["next"] == 4 and b.canon()["next"] == 4, 12.0)
            ck("H1 both statues were built (next_size = STATUE)", ok)
            _s, res = a.txn(OP_ACK)
            ck("H1 the statue acknowledgement APPLIED (statue_ordered cleared, no loan)", res is not None and res.outcome == 0 and a.wait_canon(lambda x: x["statue"] == 0)[0])
            ctx["statues"] = (n_before, ranks[-2:])
        ctx["A"], ctx["B"] = (a.house, a.slot, a.loan()), (b.house, b.slot, b.loan())
    finally:
        rig.closeall()
        ck("H1 graceful stop (exit 0)", rig.stop(h) == 0)
    # GCI after the run
    gci = L.server_gci()
    if os.path.isfile(gci) and "A" in ctx:
        data = open(gci, "rb").read()
        for key in ("A", "B"):
            hh, slot, loan = ctx[key]
            g = hdr(L.house_from_gci(data, hh))
            rec_loan = struct.unpack_from(">I", data, 0x40 + 0x26000 + 0x20 + slot * 0x2440 + L.REC_OFF_LOAN)[0]
            ck("H1 GCI: resident %s house %d size %d next %d statue_ordered %d; loan %d (client %d)" % (key, hh, g["size"], g["next"], g["statue"], rec_loan, loan),
               g["size"] == 3 and g["next"] == 4 and rec_loan == loan)
        ck("H1 GCI: num_statues = %d" % data[NUM_STATUES_GCI], data[NUM_STATUES_GCI] >= 2)


def run_h2(rig, args, ctx):
    ck = rig.ck
    h, ok = rig.start("h2a", args.port + 1, {})
    if not ok:
        return
    sent = None
    try:
        c = Res(rig, rig.join_resident("C", 3), 3)
        ck("H2 (setup) resident C owns house %s" % c.house, c.house is not None)
        if c.house is None:
            return
        reached = advance(rig, c, "size", "H2 setup") or next_op(c.canon(), c.loan()) in ("basement", "statue")
        op = {"size": OP_SIZE, "basement": OP_BASEMENT, "statue": OP_STATUE}.get(next_op(c.canon(), 0), OP_SIZE)
        ck("H2 (setup) C can order (%d)" % op, reached)
        sent, res = c.txn(op, 4 if op == OP_SIZE else 0)
        ck("H2 the order APPLIED before the restart", res is not None and res.outcome == 0)
        nonce, raw = c.c.txn_nonce, sent.raw
    finally:
        rig.closeall()
        ck("H2 graceful stop (exit 0)", rig.stop(h) == 0)
    if sent is None:
        return
    h, ok = rig.start("h2b", args.port + 2, {})
    if not ok:
        return
    try:
        c2 = rig.join_resident("C2", 3)
        c2.txn_nonce = nonce  # the SAME client process (its nonce survives a host restart)
        n0 = len(h.log_text())
        c2.send_reliable(raw)
        seq = struct.unpack_from("<I", raw, 12)[0]
        res = c2.wait_txn_result(seq, timeout=5.0, nonce=nonce)
        ck("H2 the identical resend after the restart -> APPLIED REPLAYED from the house state (no second order)",
           res is not None and res.outcome == 0 and res.reason == L.PC_NETGAME_TXN_REASON_REPLAYED and "already reflected by the house state" in h.log_text()[n0:])
    finally:
        rig.closeall()
        ck("H2 graceful stop (exit 0)", rig.stop(h) == 0)


def run_h3(rig, args, ctx):
    """Shop upgrade follow-up (c): the vanilla reopening leaflet reaches the residents' HOUSE mailboxes (MAILBOX_LETTER for the house) and, new, the GUESTS' host-held mailboxes
    (MAILBOX_LETTER house 0xFF); follow-up (d): the PENDING state is announced to everybody (service 6) -- the on-screen notice itself is client-side (source audit)."""
    ck = rig.ck
    h, ok = rig.start("h3", args.port + 3, {"AC_TEST_SHOP_UPGRADE_SECONDS": "20", "AC_TEST_SHOP_UPGRADE_QUALIFY": "1", "AC_TEST_CLEAR_ERRANDS": "1"})  # vanilla: a player still in the first job gets no leaflet
    if not ok:
        return
    try:
        a = rig.join_resident("A3", 1)
        gc = rig.join_guest("G3", ctx["g"], ctx["rec"])

        def up(c):
            ev = [(f, b) for (conn, f, b) in c.ts_states if conn == c.connect_count and f.service == 6 and len(b) == 16]
            return ev[-1][1] if ev else None

        ok = wait_for(lambda: up(gc) is not None and up(gc)[0] == 1, 30.0)
        ck("H3 the upgrade is PENDING and every peer was told (service 6)", ok and up(a) is not None and up(a)[0] == 1)
        if not ok:
            return
        b = up(gc)
        gen, target = struct.unpack_from("<I", b, 4)[0], b[2]
        gc.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, 9, 0, 0, 0, 501))
        L.pump_sleep(1.0)
        gc.send_reliable(struct.pack("<BBBBI", 85, 1, target, 0, gen))
        ck("H3 the guest's start request closed the shop", wait_for(lambda: up(gc)[0] == 2, 10.0))
        gc.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, SCENE_FG, L.PC_NETGAME_SCENE_FLAG_IN_TOWN, 0, 0, 502))
        g0, a0 = len(gc.mbox_msgs()), len(a.mbox_msgs())
        ok = wait_for(lambda: "the shop was upgraded" in h.log_text(), 90.0)
        ck("H3 the host renewed the shop", ok)
        ck("H3 the host put the reopening leaflet into the guest's mailbox", re.search(r"the reopening leaflet went into 1 guest mailbox\(es\)", h.log_text()) is not None)
        gl = gc.wait_mbox_msgs(g0 + 1, timeout=10.0)
        ck("H3 the guest received a MAILBOX_LETTER (house 0xFF) after the renewal", any(m.house == 0xFF and not (m.flags & 1) for m, _i in gl[g0:]))
        al = a.wait_mbox_msgs(a0 + 1, timeout=10.0)
        ck("H3 the resident received the vanilla leaflet in its HOUSE mailbox (MAILBOX_LETTER for its house)", any(m.house != 0xFF and not (m.flags & 1) for m, _i in al[a0:]))
    finally:
        rig.closeall()
        ck("H3 graceful stop (exit 0)", rig.stop(h) == 0)


def run_h4(rig, args, ctx):
    """A failed durable save (TEST-ONLY --promote-fault fail_save: the next durable save fails once) rolls the order back and answers FAULT; the retry is APPLIED."""
    ck = rig.ck
    h, ok = rig.start("h4", args.port + 4, {})
    if not ok:
        return
    h.stop()
    env = dict(os.environ, AC_TEST_HOOKS="1")
    h = L.HostProcess(port=args.port + 5, extra_args=["--dedicated", "--promote-fault", "fail_save"], log_path=T.log_path("hup_h4b_host.log"), bin_dir=L.GAME_BIN_DIR,
                      stdin_pipe=True, env=env).start()
    orig = h.log_text
    h.log_text = lambda: orig().replace("\r\n", "\n")
    rig.port = args.port + 5
    ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
    ck("H4 dedicated host with --promote-fault fail_save booted", ok)
    if not ok:
        h.stop()
        return
    L.resolve_host_town(IP, rig.port)
    try:
        r = Res(rig, rig.join_resident("D4", 1), 1)
        ck("H4 (setup) the resident reaches 'an order can be placed'", advance(rig, r, "size", "H4 setup") or next_op(r.canon(), r.loan()) in ("basement", "statue"))
        op = {"size": OP_SIZE, "basement": OP_BASEMENT, "statue": OP_STATUE}.get(next_op(r.canon(), 0), OP_SIZE)
        before = r.canon()
        _s, res = r.txn(op, 2 if op == OP_SIZE else 0)
        ck("H4 the first order meets the failing durable save: REJECTED FAULT (13), rolled back", res is not None and res.outcome == 1 and res.reason == L.PC_NETGAME_TXN_REASON_FAULT
           and "the durable save failed" in h.log_text())
        L.pump_sleep(2.0)
        ck("H4 the owner's canon did not change", r.canon() == before)
        _s, res = r.txn(op, 2 if op == OP_SIZE else 0)
        ck("H4 the retry is APPLIED", res is not None and res.outcome == 0)
    finally:
        rig.closeall()
        ck("H4 graceful stop (exit 0)", rig.stop(h) == 0)


def source_audit(ck):
    ng = open(os.path.join(T.PC, "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    ck("S wire: kind 20, reasons 45..47 in C", "#define PC_NETGAME_TXN_KIND_HOUSE_UPGRADE 20u" in ng and "#define PC_NETGAME_TXN_REASON_HOUSE_NOT_ELIGIBLE 45u" in ng and
       "#define PC_NETGAME_TXN_REASON_HOUSE_BUSY         46u" in ng and "#define PC_NETGAME_TXN_REASON_HOUSE_CHANGED      47u" in ng)
    blk = ng[ng.index("===== HOUSE UPGRADE BEGIN"):ng.index("===== HOUSE UPGRADE END")]
    hd = blk[blk.index("static void pcnetgame_handle_host_house_upgrade_txn("):]
    hd = hd[:hd.index("\n}\n")]
    ck("S the handler saves DURABLY before it answers APPLIED and rolls back on a failed save",
       hd.index("pc_save_write_authoritative_durable()") < hd.index('pcnetgame_txn_send_applied(peer, idx, in, slot, (uint8_t)PC_NETGAME_TXN_REASON_NONE, "")') and
       "memcpy(hs, snap, sizeof(snap));" in hd)
    ck("S the idempotence check (state answers a resend after a restart) precedes the guard refusal", hd.index("pcnetgame_hu_already(op, t, hs, mir)") < hd.index("PC_NETGAME_TXN_REASON_HOUSE_CHANGED"))
    ck("S the client never writes homes[] (the apply sets only the loan)", "Save_Get(homes" not in blk[blk.index("static int pcnetgame_txn_apply_house_upgrade("):])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16310)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_<name> copy only", file=sys.stderr)
        return 2
    results = []
    rig = Rig(results)
    ctx = {"g": L.guest_identity("HUPGUEST", 0x4E21, "HOMETWN", 0x5B41)}
    ctx["rec"] = bytes(L.fresh_guest_record_for(ctx["g"]))
    L.server_wipe_sidecars()
    source_audit(rig.ck)
    for name, fn in (("H1", run_h1), ("H2", run_h2), ("H3", run_h3), ("H4", run_h4)):
        if args.only and name not in args.only.split(","):
            continue
        if True:  # every phase starts from a fresh fixture copy (phases must not depend on each other)
            T.make_fixture(FIXTURE)
            if os.path.isfile(W3_EXE):
                shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
            L.server_wipe_sidecars()
        try:
            fn(rig, args, ctx)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            rig.ck("%s raised %r" % (name, e), False)
        finally:
            rig.closeall()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
