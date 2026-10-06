#!/usr/bin/env python3
"""test_restock_protocol.py - Nook's shop MANUAL RESTOCK (TXN_COMMIT kind 15 SHOP_RESTOCK + the HOST_CONFIG restock state): REAL DEDICATED HOSTS + scripted FakeClients (guests) + the stdin console.

TIER: scripted FakeClients against REAL `AnimalCrossing.exe --host <port> --dedicated` processes started from the DISPOSABLE fixture copy pc\\build64\\bin_fixture4_rsp (a fresh copy of the
read-only bin_fixture4; NET_SPIKE_GAME_BIN points at it). The live save dir, bin_talkfix*, bin_fixture4 and the other disposable copies are never used.
PHASES
  P0  host (defaults): guests A, B (wallet 3000) and C (wallet 100) join, the host stops, the stored records are patched
  P1  host: all three reconnect. R1 C cannot pay -> NO_FUNDS (19); R2 a wrong price -> PRICE_MISMATCH (21), a malformed message -> BAD_SHAPE (11), a stale base -> STALE_IMAGE (10): none starts
      a restock; R3 A and B request at the same time: exactly ONE APPLIED (wallet 3000 -> 2500), the other REJECTED RESTOCKING (31) with its wallet intact; every peer receives HOST_CONFIG with
      the restock state (byte 2 = 1, generation 1); R4 the winner's identical resend is answered from the journal (REPLAYED) and the host log has exactly one 'SHOP_RESTOCK paid'; R5 the winner
      DISCONNECTS right after paying, the loser asks again while restocking (RESTOCKING, nothing charged), the winner RECONNECTS mid-restock (HOST_CONFIG still says restocking, wallet 2500);
      R6 the restock ends 60 s (host time) after the payment: the new catalog (SHOP service, DIFFERENT bytes) reaches every peer BEFORE the OPEN HOST_CONFIG; R7 the loser asks once more and now
      pays (APPLIED, generation 2); the host stops with a restock running
  P2  host restart DURING a restock (AC_TEST_RESTOCK_WAIT_MS=90000 test hook, only for this phase): the restart resumes the remaining time from save/mp/shop_restock.ini ('resuming restock'),
      the restock ends at the ORIGINAL wall-clock deadline, a client that connects during the wait is told 'restocking', the new catalog arrives, shop_restock.ini is gone
Usage: python test_restock_protocol.py [--port 12991]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402
import make_four_resident_fixture as MF  # noqa: E402,F401

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_rsp")

if __name__ == "__main__":
    T.make_fixture("bin_fixture4_rsp")

import net_spike_lib as L  # noqa: E402

IP = "127.0.0.1"
PRICE = 500
GST_ENTRY = 72 + 0x2440
GST_SIZE = 32 + 8 * GST_ENTRY + 4
SVC_SHOP, SVC_HOSTCFG = 3, 4


def mp_dir():
    return os.path.join(L.GAME_BIN_DIR, "save", "mp")


def mpath(n):
    return os.path.join(mp_dir(), n)


def raw(path):
    with open(path, "rb") as f:
        return f.read()


def parse_guests(path):
    b = raw(path)
    assert len(b) == GST_SIZE and b[:8] == b"ACMPGST\x00"
    assert struct.unpack_from("<I", b, len(b) - 4)[0] == zlib.crc32(b[:-4]) & 0xFFFFFFFF
    out = []
    for i in range(8):
        e = b[32 + i * GST_ENTRY:32 + (i + 1) * GST_ENTRY]
        if e[0]:
            rec = e[72:72 + 0x2440]
            out.append(dict(slot=i, pid=bytes(e[4:24]), wallet=struct.unpack_from(">I", rec, L.REC_OFF_WALLET)[0]))
    return out


def patch_guest_record(path, pid, patch):
    b = bytearray(raw(path))
    for i in range(8):
        o = 32 + i * GST_ENTRY
        if b[o] and bytes(b[o + 4:o + 24]) == pid:
            rec = bytearray(b[o + 72:o + 72 + 0x2440])
            patch(rec)
            b[o + 72:o + 72 + 0x2440] = rec
            struct.pack_into("<I", b, o + 52, zlib.crc32(bytes(rec)) & 0xFFFFFFFF)
            struct.pack_into("<I", b, len(b) - 4, zlib.crc32(bytes(b[:-4])) & 0xFFFFFFFF)
            with open(path, "wb") as f:
                f.write(bytes(b))
            return True
    return False


class PClient(L.FakeClient):
    """FakeClient that timestamps every TOWN_SVC_STATE (service, seq, digest, blob)."""

    def __init__(self, *a, **kw):
        self.svc_log = []  # (monotonic, service, seq, digest, blob)
        super().__init__(*a, **kw)

    def on_message(self, m):
        try:
            if m.channel == L.CH_RELIABLE and m.payload and m.payload[0] == L.PC_NETGAME_MSG_TOWN_SVC_STATE and len(m.payload) >= 12:
                _t, svc, ln, seq, dig = struct.unpack_from("<BBHII", m.payload, 0)
                self.svc_log.append((time.monotonic(), svc, seq, dig, bytes(m.payload[12:12 + ln])))
        except Exception:  # noqa: BLE001
            pass
        return super().on_message(m)

    def cfg_events(self):
        """[(t, restocking, gen)] of every HOST_CONFIG received."""
        return [(t, b[2], struct.unpack_from("<I", b, 4)[0]) for (t, svc, seq, dig, b) in self.svc_log if svc == SVC_HOSTCFG and len(b) >= 8]

    def shop_events(self):
        return [(t, dig) for (t, svc, seq, dig, b) in self.svc_log if svc == SVC_SHOP]


class Rig:
    _n = [0]

    def __init__(self, results):
        self.results = results
        self.ck = lambda d, c: L.check(d, bool(c), results)
        self.clients = []
        self.port = None

    def start(self, tag, port, extra=(), env=None):
        kw = dict(port=port, extra_args=["--dedicated"] + list(extra), log_path=T.log_path("rsp_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR, stdin_pipe=True, verbose=False, new_group=True)
        if env:
            kw["env"] = env
        h = L.HostProcess(**kw).start()
        orig = h.log_text
        h.log_text = lambda: orig().replace("\r\n", "\n")
        self.port = port
        ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
        self.ck("[%s] dedicated host booted (%s)" % (tag, " ".join(extra) or "defaults"), ok)
        if ok:
            L.resolve_host_town(IP, port)
        return h, ok

    def stop(self, h):
        h.send_line("stop")
        return h.wait_exit(60.0)

    def _bind(self):
        Rig._n[0] += 1
        return "127.0.%d.%d" % (80 + Rig._n[0] // 200, 2 + Rig._n[0] % 200)

    def join(self, label, g, rec, token=None):
        c = PClient(label, IP, self.port, guest=g, guest_token=token, guest_record_img=rec, bind_ip=self._bind(), record_wait=True)
        self.clients.append(c)
        c.connect_and_ready(quiet=True)
        return c

    def release(self, c):
        try:
            if c.is_connected():
                c.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
        if c in self.clients:
            self.clients.remove(c)
        L.pump_sleep(0.8)

    def closeall(self):
        for c in list(self.clients):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients = []


def restock(c, price=PRICE, rid=1, **kw):
    """Sends ONE SHOP_RESTOCK (kind 15) built from the client's local image."""
    return c.send_txn_commit(L.PC_NETGAME_TXN_KIND_SHOP_RESTOCK, rid, L.PC_NETGAME_TXN_DEST_NONE, 0, 0, aux_cond=0, aux_item=price, **kw)


def wallet(c):
    return L.record_inventory(c.rec_local)[2]


def source_audit(rig):
    ck = rig.ck
    ng = open(os.path.join(T.PC, "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    ms = open(os.path.join(T.PC, "..", "src", "game", "m_shop.c"), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    ck("S wire: kind 15 / reason 31 / price 500 / wait 60000 in C == net_spike_lib",
       "#define PC_NETGAME_TXN_KIND_SHOP_RESTOCK 15u" in ng and "#define PC_NETGAME_TXN_REASON_RESTOCKING 31u" in ng and "#define PC_NETGAME_RESTOCK_PRICE         500u" in ng
       and "#define PC_NETGAME_RESTOCK_WAIT_MS       60000u" in ng and (L.PC_NETGAME_TXN_KIND_SHOP_RESTOCK, L.PC_NETGAME_TXN_REASON_RESTOCKING) == (15, 31))
    ck("S the kind-15 COMMIT goes through the same dispatch as the other town-service kinds (one synchronous host handler)",
       "tc.kind == (uint8_t)PC_NETGAME_TXN_KIND_SHOP_RESTOCK) {\n            pcnetgame_handle_host_ts_txn(peer, &tc);" in ng)
    h = ng[ng.index("static void pcnetgame_handle_host_ts_txn("):ng.index("/* ===== TS HOST END ===== */")]
    order = ["pcnetgame_txn_nonce_fenced(", "pcnetgame_rec_refresh_hostfields(idx, slot)", "STALE_IMAGE", "pcnetgame_rec_validate_inventory(t->pre_pockets", "if (s_restock.active) {", "PC_NETGAME_RESTOCK_PRICE",
             "post_wallet -= PC_NETGAME_RESTOCK_PRICE", "pcnetgame_restock_start(", "pcnetgame_rec_txn_write_inventory(idx, post", "pcnetgame_txn_journal_add(R, in, hash",
             "pcnetgame_ts_refresh(svc)", "pcnetgame_txn_send_applied(peer, idx, in, slot"]
    idx = [h.find(x) for x in order[:-1]] + [h.rfind(order[-1])]
    ck("S handler order: journal fence/replay -> base -> pre-image -> 'already restocking' check -> price -> debit in locals -> start -> mirror write -> journal -> refresh -> RESULT (one debit, one start)",
       all(i >= 0 for i in idx) and idx == sorted(idx))
    cl = ng[ng.index("static void pcnetgame_ts_client_apply("):ng.index("static void pcnetgame_ts_client_apply(") + 6000]
    ck("S the timer is the HOST's monotonic clock (end_tick = pcnetgame_now_ms() + wait), the persisted wall-clock deadline is only for a restart, a client never computes a deadline",
       "s_restock.end_tick = pcnetgame_now_ms() + wait_ms;" in ng and "(int32_t)(now - s_restock.end_tick) >= 0" in ng and "end_tick" not in cl)
    fin = ng[ng.index("static void pcnetgame_restock_finish(const char* why) {"):ng.index("static void pcnetgame_restock_finish(const char* why) {") + 1200]
    ck("S the catalog is generated by the HOST only (mSP_ExchangeLineUp_ZeldaMalloc in pcnetgame_restock_finish) and mirrored SHOP first, HOST_CONFIG second",
       "mSP_ExchangeLineUp_ZeldaMalloc();" in fin and fin.index("pcnetgame_ts_refresh((int)PC_NETGAME_TS_SHOP)") < fin.index("pcnetgame_ts_refresh((int)PC_NETGAME_TS_HOSTCFG)"))
    ck("S the shop is open 24/7 on PC (the clock-based PRE / END / OPEN returns are compiled out, RESTOCK closes it, RENEW / first job / raffle day keep their rules)",
       "#ifndef TARGET_PC /* PC: Nook's shop is open 24/7" in ms and "return mSP_SHOP_STATUS_RESTOCK;" in ms and "if (mSP_CheckFukubikiDay() == FALSE) {\n        return mSP_SHOP_STATUS_OPEN;" in ms)


def phase0(rig, args, ctx):
    ck = rig.ck
    h, ok = rig.start("p0", args.port - 1)
    if not ok:
        return
    try:
        for key in ("A", "B", "C"):
            c = rig.join(key + "0", ctx["g"][key], ctx["rec"][key])
            ctx["tok"][key] = bytes(c.guest_token)
        L.pump_sleep(2.0)
        for c in list(rig.clients):
            rig.release(c)
        L.pump_sleep(7.0)
    finally:
        rig.closeall()
        ck("P0 graceful stop (exit 0)", rig.stop(h) == 0)
    for key, w in (("A", 3000), ("B", 3000), ("C", 100)):
        def goods(rec, w=w):
            struct.pack_into(">I", rec, L.REC_OFF_WALLET, w)
        ck("P0 %s's stored record patched (wallet %d)" % (key, w), patch_guest_record(mpath("guests.dat"), L.guest_pid_be(ctx["g"][key]), goods))


def phase1(rig, args, ctx):
    ck = rig.ck
    h, ok = rig.start("p1", args.port)
    if not ok:
        return
    g, rec, tok = ctx["g"], ctx["rec"], ctx["tok"]
    try:
        a = rig.join("A", g["A"], rec["A"], token=tok["A"])
        b = rig.join("B", g["B"], rec["B"], token=tok["B"])
        c = rig.join("C", g["C"], rec["C"], token=tok["C"])
        L.pump_sleep(9.0)
        ck("P1 the three guests hold their stored wallets 3000 / 3000 / 100", (wallet(a), wallet(b), wallet(c)) == (3000, 3000, 100))
        for x, n in ((a, "A"), (b, "B"), (c, "C")):
            ev = x.cfg_events()
            ck("P1 %s received HOST_CONFIG at READY with byte 2 = 0 (the shop is OPEN), generation 0" % n, ev and ev[-1][1] == 0 and ev[-1][2] == 0)
        shop0 = c.shop_events()[-1][1] if c.shop_events() else None
        ck("P1 the first SHOP service (the day's catalog) reached C", shop0 is not None)
        n_cfg = len(c.cfg_events())
        # ---------------- R1 / R2 refusals
        sent = restock(c, rid=1)
        r = c.wait_txn_result(sent.seq, 5.0)
        ck("R1 insufficient Bells (100): REJECTED NO_FUNDS (19), kind 15 echoed", r is not None and r.kind == 15 and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and r.reason == L.PC_NETGAME_TXN_REASON_NO_FUNDS)
        base = (a.rec_last[1], a.rec_last[2])
        for i, (label, kw, reason) in enumerate([("a wrong price (499)", dict(price=PRICE - 1), L.PC_NETGAME_TXN_REASON_PRICE_MISMATCH),
                                                 ("a stale base (epoch + 1)", dict(base=(base[0] + 1, base[1])), L.PC_NETGAME_TXN_REASON_STALE_IMAGE),
                                                 ("a malformed message (flags 1)", dict(flags=1), L.PC_NETGAME_TXN_REASON_BAD_SHAPE)]):
            s = restock(a, rid=10 + i, **kw)
            r = a.wait_txn_result(s.seq, 5.0)
            ck("R2 %s -> REJECTED %s (%d)" % (label, L.TXN_REASON_NAMES.get(reason), reason), r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and r.reason == reason)
            L.pump_sleep(0.4)
        L.pump_sleep(1.5)
        ck("R2 none of the refusals changed anything: no new HOST_CONFIG, wallets intact, no 'SHOP_RESTOCK paid' on the host", len(c.cfg_events()) == n_cfg and wallet(a) == 3000
           and "SHOP_RESTOCK paid" not in h.log_text())
        # ---------------- R3 two simultaneous requests
        sa = restock(a, rid=20)
        sb = restock(b, rid=20)
        ra = a.wait_txn_result(sa.seq, 6.0)
        rb = b.wait_txn_result(sb.seq, 6.0)
        res = [(a, sa, ra), (b, sb, rb)]
        applied = [x for x in res if x[2] is not None and x[2].outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED]
        refused = [x for x in res if x[2] is not None and x[2].outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and x[2].reason == L.PC_NETGAME_TXN_REASON_RESTOCKING]
        ck("R3 two simultaneous requests: exactly ONE APPLIED and ONE REJECTED RESTOCKING (31)", len(applied) == 1 and len(refused) == 1)
        if len(applied) != 1 or len(refused) != 1:
            return
        win, wsent, wres = applied[0]
        lose = refused[0][0]
        ck("R3 the winner's post wallet is exactly 3000 - 500 = 2500; the loser's wallet is intact (3000)", wres.post_wallet == 2500 and wallet(win) == 2500 and wallet(lose) == 3000)
        L.pump_sleep(1.5)
        for x, n in ((a, "A"), (b, "B"), (c, "C")):
            ev = x.cfg_events()
            ck("R3 %s received HOST_CONFIG: restocking = 1, generation 1" % n, ev and ev[-1][1] == 1 and ev[-1][2] == 1)
        t_cfg1 = [t for (t, rs, gen) in c.cfg_events() if rs == 1][0]
        ck("R3 the host logged exactly one 'SHOP_RESTOCK paid 500: wallet 3000 -> 2500' and one 'RESTOCKING started'", len(re.findall(r"SHOP_RESTOCK paid 500: wallet 3000 -> 2500", h.log_text())) == 1
           and len(re.findall(r"RESTOCKING started", h.log_text())) == 1)
        ck("R3 shop_restock.ini was written (host restart resume)", os.path.isfile(mpath("shop_restock.ini")))
        # ---------------- R4 replay
        win.resend_txn(wsent)
        L.pump_sleep(1.5)
        rr = win.txn_results_for(wsent.seq)
        ck("R4 the identical resend is answered from the journal (a second APPLIED, same post wallet) and does NOT debit again", len(rr) >= 2 and all(x.post_wallet == 2500 for x in rr)
           and len(re.findall(r"SHOP_RESTOCK paid", h.log_text())) == 1)
        # ---------------- R5 disconnect right after paying; the other asks again; reconnect mid-restock
        wname = "A" if win is a else "B"
        wg, wrec, wtok = g[wname], rec[wname], tok[wname]
        rig.release(win)
        s2 = restock(lose, rid=21)
        r2 = lose.wait_txn_result(s2.seq, 6.0)
        ck("R5 the winner disconnected right after paying; the other player asks again while restocking: REJECTED RESTOCKING, nothing charged (wallet 3000)",
           r2 is not None and r2.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and r2.reason == L.PC_NETGAME_TXN_REASON_RESTOCKING and wallet(lose) == 3000)
        L.pump_sleep(12.0)
        win2 = rig.join(wname + "2", wg, wrec, token=wtok)
        L.pump_sleep(3.0)
        ev = win2.cfg_events()
        ck("R5 the winner RECONNECTS mid-restock: HOST_CONFIG at READY still says restocking = 1 (generation 1), and its wallet is 2500 (the host record, not charged twice)",
           ev and ev[-1][1] == 1 and ev[-1][2] == 1 and wallet(win2) == 2500)
        # ---------------- R6 the end of the restock
        shop_before = [d for (_t, d) in c.shop_events()]
        end = time.monotonic() + 75.0
        while time.monotonic() < end and not any(rs == 0 and gen == 1 for (_t, rs, gen) in c.cfg_events()):
            L.pump_sleep(0.25)
        ev = c.cfg_events()
        t_open = [t for (t, rs, gen) in ev if rs == 0 and gen == 1]
        ck("R6 C was told the shop is OPEN again (restocking = 0, generation 1)", bool(t_open))
        if t_open:
            dt = t_open[0] - t_cfg1
            ck("R6 the restock lasted 60 s of HOST time (measured at a bystander between the two HOST_CONFIG pushes: %.2f s, window 59.0 .. 62.0)" % dt, 59.0 <= dt <= 62.0)
            sh = [(t, d) for (t, d) in c.shop_events() if t > t_cfg1]
            ck("R6 a NEW catalog was pushed (SHOP service digest differs from the day's catalog) strictly BEFORE the OPEN HOST_CONFIG", bool(sh) and sh[-1][1] not in shop_before and sh[-1][0] <= t_open[0])
            L.pump_sleep(2.0)
            same = set(x.shop_events()[-1][1] for x in (lose, c, win2) if x.is_connected() and x.shop_events())
            ck("R6 every connected peer holds the SAME new catalog (one digest)", len(same) == 1)
        ck("R6 shop_restock.ini is gone once the shop reopened; the host log says 'a NEW catalog was generated'", not os.path.isfile(mpath("shop_restock.ini")) and "a NEW catalog was generated" in h.log_text())
        # ---------------- R7 a new request after the end
        s3 = restock(lose, rid=30)
        r3 = lose.wait_txn_result(s3.seq, 6.0)
        ck("R7 after the restock ended a new request is accepted (APPLIED, 3000 -> 2500)", r3 is not None and r3.outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED and r3.post_wallet == 2500)
        L.pump_sleep(1.0)
        ev = c.cfg_events()
        ck("R7 HOST_CONFIG: restocking = 1, generation 2", ev[-1][1] == 1 and ev[-1][2] == 2)
        L.pump_sleep(7.0)
    finally:
        rig.closeall()
        ck("P1 graceful stop (exit 0), with a restock running", rig.stop(h) == 0)
    ents = {e["pid"]: e["wallet"] for e in parse_guests(mpath("guests.dat"))}
    ck("P1 guests.dat: A 2500 / B 2500 (each paid exactly once), C 100", sorted((ents[bytes(L.guest_pid_be(g[k]))] for k in ("A", "B"))) == [2500, 2500] and ents[bytes(L.guest_pid_be(g["C"]))] == 100)


def phase2(rig, args, ctx):
    ck = rig.ck
    g, rec, tok = ctx["g"], ctx["rec"], ctx["tok"]
    ini = mpath("shop_restock.ini")
    ck("P2 (setup) shop_restock.ini of the running restock survived the host stop", os.path.isfile(ini))
    try:
        os.remove(ini)  # restart the story: a fresh restock with a LONG wait so the restart lands inside it
    except OSError:
        pass
    env = dict(os.environ, AC_TEST_HOOKS="1", AC_TEST_RESTOCK_WAIT_MS="90000")
    h, ok = rig.start("p2a", args.port, env=env)
    if not ok:
        return
    t_pay_wall = None
    try:
        a = rig.join("A", g["A"], rec["A"], token=tok["A"])
        L.pump_sleep(9.0)
        s = restock(a, rid=40)
        r = a.wait_txn_result(s.seq, 6.0)
        t_pay_wall = time.time()
        ck("P2 the payment is APPLIED (the test hook stretched the wait to 90 s)", r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED)
        L.pump_sleep(3.0)
        ck("P2 shop_restock.ini holds this town's deadline", os.path.isfile(ini) and "end_wall_ms" in open(ini).read())
    finally:
        rig.closeall()
        ck("P2 the host stops DURING the restock (exit 0)", rig.stop(h) == 0)
    h, ok = rig.start("p2b", args.port, env=env)
    if not ok:
        return
    try:
        L.pump_sleep(2.0)
        mo = re.search(r"resuming restock (\d+) after a host restart: (\d+) ms remaining", h.log_text())
        ck("P2 the restarted host RESUMED the restock (log: 'resuming restock N after a host restart: M ms remaining'), M > 0", mo is not None and int(mo.group(2)) > 0)
        b = rig.join("B", g["B"], rec["B"], token=tok["B"])
        L.pump_sleep(2.0)
        ev = b.cfg_events()
        ck("P2 a client connecting during the wait is told: restocking = 1", ev and ev[-1][1] == 1)
        end = time.monotonic() + 100.0
        while time.monotonic() < end and not any(rs == 0 for (_t, rs, _g) in b.cfg_events()):
            L.pump_sleep(0.25)
        t_open_wall = time.time()
        ck("P2 the restock ended after the restart (OPEN pushed)", any(rs == 0 for (_t, rs, _g) in b.cfg_events()))
        if t_pay_wall is not None:
            dt = t_open_wall - t_pay_wall
            ck("P2 it ended at the ORIGINAL deadline: %.1f s after the payment (90 s wait, window 88 .. 94)" % dt, 88.0 <= dt <= 94.0)
        ck("P2 a new catalog was pushed to the reconnected client and shop_restock.ini is gone", bool(b.shop_events()) and not os.path.isfile(ini))
    finally:
        rig.closeall()
        ck("P2 graceful stop (exit 0)", rig.stop(h) == 0)


def run(args, results):
    rig = Rig(results)
    ctx = {"g": {}, "rec": {}, "tok": {}}
    names = {"A": ("RSTOCKA", 0x4C11), "B": ("RSTOCKB", 0x4C12), "C": ("RSTOCKC", 0x4C13)}
    for k, (n, pid) in names.items():
        ctx["g"][k] = L.guest_identity(n, pid, "HOMETWN", 0x5B21)
        ctx["rec"][k] = bytes(L.fresh_guest_record_for(ctx["g"][k]))
    shutil.rmtree(mp_dir(), ignore_errors=True)  # the disposable fixture only
    source_audit(rig)
    for name, fn in (("P0", phase0), ("P1", phase1), ("P2", phase2)):
        try:
            fn(rig, args, ctx)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            rig.ck("%s raised %r" % (name, e), False)
        finally:
            rig.closeall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12991)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: this test mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_<name> copy only", file=sys.stderr)
        return 2
    results = []
    run(args, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
