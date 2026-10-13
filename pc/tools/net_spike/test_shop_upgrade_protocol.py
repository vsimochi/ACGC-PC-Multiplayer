#!/usr/bin/env python3
"""test_shop_upgrade_protocol.py - the announced 5-minute shop upgrade (town service 6 SHOP_UPGRADE + message 85 SHOP_UPGRADE_REQ): REAL DEDICATED HOSTS + scripted FakeClients (guests)
+ the stdin console.

TIER: protocol / harness -- scripted FakeClients against REAL `AnimalCrossing.exe --host <port> --dedicated` processes started from the DISPOSABLE fixture copy
pc\\build64\\bin_fixture4_shopup (a fresh copy of the read-only bin_fixture4 with the exe of pc\\build64_w3 copied in; NET_SPIKE_GAME_BIN points at it). The closure is shortened with the
TEST-ONLY AC_TEST_SHOP_UPGRADE_SECONDS (PC_TEST_HOOKS build + AC_TEST_HOOKS=1). The Nook dialogue itself is NOT driven (no GUI): a FakeClient sends exactly what the dialogue sends after the
acknowledgement (SHOP_UPGRADE_REQ); its "shown out" step is a PLAYER_SCENE back to the town.
PHASES
  U1  host (feature ON, closure 20 s): service 6 arrives at READY (IDLE, feature bit, closure 20 s); `shop-level-boost confirm` -> PENDING (from L, target L+1) pushed to every peer;
      a request from a player who is NOT in the shop is ignored; two players in the shop request at the same moment -> exactly ONE start (one 'CLOSED' line) and both see CLOSED with the
      same generation; a stale-generation request changes nothing; shop_upgrade.ini holds the deadline; a SHOP_BUY and a SHOP_RESTOCK during the closure are REJECTED NOT_AVAILABLE
      (nothing charged); a late joiner is told CLOSED; at the deadline the state is RENEWING and the renewal WAITS while a player still stands in the shop; once everybody is in the
      town the host renews (level L+1, log 'the shop was upgraded'), publishes the shop acre (a FIELD_BLOCK with the new structure tile SHOP0+L+1 reaches the clients), pushes the
      SHOP mirror and then IDLE, and removes shop_upgrade.ini; `shop-upgrade` reports IDLE.
  U2  the operator starts the closure (`shop-upgrade start confirm`); host restart DURING the closure (closure 60 s): the restart resumes the remaining time from shop_upgrade.ini, a client connecting after the restart is told CLOSED, the shop reopens
      at the ORIGINAL deadline (+ the renewal), shop_upgrade.ini is gone.
  U3  A/B switch AC_MP_SHOP_UPGRADE=0: service 6 says 'feature off' (flags 0), a boost never makes the upgrade PENDING (the vanilla 2-day schedule applies), `shop-upgrade` says OFF.
Usage: python test_shop_upgrade_protocol.py [--port 13311]
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

FIXTURE = "bin_fixture4_shopup"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)

if __name__ == "__main__":
    T.make_fixture(FIXTURE)
    if os.path.isfile(W3_EXE):
        shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))

import net_spike_lib as L  # noqa: E402

IP = "127.0.0.1"
SVC_SHOP, SVC_UPGRADE = 3, 6
MSG_SHOP_UPGRADE_REQ = 85
SCENE_FG, SCENE_SHOP0 = 7, 9
SHOP0 = 0x5804  # STRUCTURE_START (0x5800) + 4
IDLE, PENDING, CLOSED, RENEWING = 0, 1, 2, 3
R_NOT_AVAILABLE = 16


class UClient(L.FakeClient):
    """FakeClient that keeps every TOWN_SVC_STATE and FIELD_BLOCK it receives."""

    def __init__(self, *a, **kw):
        self.svc_log = []    # (monotonic, service, seq, blob)
        self.fblocks = []    # (monotonic, acre, items[256])
        self.scene_seq = 0
        super().__init__(*a, **kw)

    def on_message(self, m):
        try:
            p = m.payload
            if m.channel == L.CH_RELIABLE and p and p[0] == L.PC_NETGAME_MSG_TOWN_SVC_STATE and len(p) >= 12:
                _t, svc, ln, seq, _dig = struct.unpack_from("<BBHII", p, 0)
                self.svc_log.append((time.monotonic(), svc, seq, bytes(p[12:12 + ln])))
            elif m.channel == L.CH_RELIABLE and p and p[0] == L.PC_NETGAME_MSG_FIELD_BLOCK and len(p) >= 12 + 512:
                self.fblocks.append((time.monotonic(), p[2], struct.unpack_from("<256H", p, 12)))
        except Exception:  # noqa: BLE001
            pass
        return super().on_message(m)

    def up_events(self):
        """[(t, state, from, target, flags, gen, min_left, total_s)] of every service 6 received."""
        out = []
        for (t, svc, _seq, b) in self.svc_log:
            if svc == SVC_UPGRADE and len(b) == 16:
                out.append((t, b[0], b[1], b[2], b[3], struct.unpack_from("<I", b, 4)[0], struct.unpack_from("<H", b, 8)[0], struct.unpack_from("<H", b, 10)[0]))
        return out

    def last_up(self):
        ev = self.up_events()
        return ev[-1] if ev else None

    def scene(self, scene_id):
        self.scene_seq += 1
        self.send_reliable(struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, scene_id, L.PC_NETGAME_SCENE_FLAG_IN_TOWN if scene_id == SCENE_FG else 0, 0, 0,
                                       self.scene_seq))

    def request_start(self, gen, target, op=1, rsv=0):
        self.send_reliable(struct.pack("<BBBBI", MSG_SHOP_UPGRADE_REQ, op, target, rsv, gen))


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
        kw = dict(port=port, extra_args=["--dedicated"], log_path=T.log_path("shopup_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR, stdin_pipe=True, env=env)
        h = L.HostProcess(**kw).start()
        orig = h.log_text
        h.log_text = lambda: orig().replace("\r\n", "\n")
        self.port = port
        if not os.path.isfile(L.server_gci()) and h.wait_for_log(r"\[1\] Use this town", 60.0) is not None:
            h.send_line("1")  # first launch on the fixture's legacy town: adopt it (copy), as an operator would answer the console question
        ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
        self.ck("[%s] dedicated host booted (%s)" % (tag, " ".join("%s=%s" % kv for kv in env_extra.items())), ok)
        if ok:
            L.resolve_host_town(IP, port)
        return h, ok

    def stop(self, h):
        h.send_line("stop")
        return h.wait_exit(60.0)

    def join(self, label, g, rec, token=None):
        Rig._n[0] += 1
        bind = "127.0.%d.%d" % (90 + Rig._n[0] // 200, 2 + Rig._n[0] % 200)
        c = UClient(label, IP, self.port, guest=g, guest_token=token, guest_record_img=rec, bind_ip=bind, record_wait=True)
        self.clients.append(c)
        c.connect_and_ready(quiet=True)
        return c

    def closeall(self):
        for c in list(self.clients):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients = []


def console(h, line, pat, timeout=6.0):
    n0 = len(h.log_text())
    h.send_line(line)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        m = re.search(pat, h.log_text()[n0:])
        if m:
            return m
        L.pump_sleep(0.2)
    return None


def host_level(h):
    m = re.findall(r"\[NET\]\[TS\] host: shop level (\d+) sales_sum (\d+)", h.log_text())
    return int(m[-1][0]) if m else None


def source_audit(ck):
    ng = open(os.path.join(T.PC, "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    ck("S wire: service 6 / 16 bytes / array bound 8 (7 = the guest directory) / message 85 (8 bytes) in C",
       "#define PC_NETGAME_TS_UPGRADE  6u" in ng and "#define PC_NETGAME_TS_UPGRADE_LEN 16u" in ng and "#define PC_NETGAME_TS_NUM      8u" in ng  # letters to guests (deliberate): service 7 = GUEST_DIR, array bound 8
       and "PC_NETGAME_MSG_SHOP_UPGRADE_REQ      = 85," in ng and '_Static_assert(sizeof(PCNetGameShopUpgradeReqMsg) == 8,' in ng)
    blk = ng[ng.index("===== SHOP UPGRADE BEGIN"):ng.index("===== SHOP UPGRADE END")]
    st = blk[blk.index("static int pcnetgame_shopup_start("):]
    st = st[:st.index("\n}\n")]
    ck("S the start writes the restart file BEFORE it publishes the closure", st.index("pcnetgame_shopup_file_write();") < st.index("pcnetgame_shopup_publish_state();"))
    fin = blk[blk.index("static void pcnetgame_shopup_finish("):]
    fin = fin[:fin.index("\n}\n")]
    ck("S the reopening publishes the shop acre, then SHOP + POLICE, then service 6, and removes the file only after a durable save",
       fin.index("pcnetgame_shopup_publish_acre(level);") < fin.index("pcnetgame_ts_refresh((int)PC_NETGAME_TS_SHOP)") < fin.index("pcnetgame_shopup_publish_state();")
       < fin.index("pc_save_write_authoritative_durable()") < fin.index("pcnetgame_shopup_file_remove();"))
    ck("S the timer is the HOST's monotonic clock (end_tick); the wall-clock deadline is only for the restart file; a resumed closure is never longer than one closure",
       "s_up.end_tick = now + s_up.total_ms;" in blk and "(int32_t)(now - s_up.end_tick) >= 0" in blk and "left = s_up.total_ms; /* the wall clock went backwards" in blk)
    ck("S the client request handler checks READY, the feature, the shop scene of the requester, then the generation / target (pcnetgame_shopup_start)",
       all(x in blk for x in ("s_host_peer_link[peer] != PC_NETGAME_LINK_READY", "the requester is not inside the shop", "gen != s_up.gen || target != s_up.target_level")))


def run_u1(rig, args, ctx):
    ck = rig.ck
    h, ok = rig.start("u1", args.port, {"AC_TEST_SHOP_UPGRADE_SECONDS": "20"})
    if not ok:
        return
    try:
        a = rig.join("A", ctx["g"]["A"], ctx["rec"]["A"])
        b = rig.join("B", ctx["g"]["B"], ctx["rec"]["B"])
        ctx["tok"]["A"], ctx["tok"]["B"] = bytes(a.guest_token), bytes(b.guest_token)
        ok = wait_for(lambda: a.last_up() is not None and b.last_up() is not None, 10.0)
        ua = a.last_up()
        ck("U1 service 6 at READY: IDLE, feature bit set, closure 20 s (the test hook), generation present", ok and ua[1] == IDLE and ua[4] == 1 and ua[7] == 20)
        lvl = host_level(h)
        ck("U1 (setup) the host's shop level is known (%s) and below Nookway" % lvl, lvl is not None and lvl < 2)
        if lvl is None or lvl >= 2:
            return
        m = console(h, "shop-upgrade", r"SHOP-UPGRADE: (\w+);")
        ck("U1 `shop-upgrade` reports IDLE", m is not None and m.group(1) == "IDLE")
        m = console(h, "shop-level-boost confirm", r"SHOP-LEVEL-BOOST: sales \d+ -> \d+")
        ck("U1 `shop-level-boost confirm` added the missing sales", m is not None)
        ok = wait_for(lambda: all(x.last_up() and x.last_up()[1] == PENDING for x in (a, b)), 6.0)
        ua = a.last_up()
        ck("U1 PENDING pushed to every peer (from %d to %d)" % (lvl, lvl + 1), ok and ua[2] == lvl and ua[3] == lvl + 1)
        if not ok:
            return
        gen = ua[5]
        ck("U1 the host log says PENDING and that Nook announces at the next talk", "PENDING: the town qualifies" in h.log_text())
        # a request from a player who is NOT in the shop
        a.scene(SCENE_FG)
        L.pump_sleep(0.6)
        a.request_start(gen, lvl + 1)
        L.pump_sleep(1.5)
        ck("U1 a request from a player standing in the town (not in the shop) is IGNORED, the upgrade stays PENDING",
           "the requester is not inside the shop" in h.log_text() and a.last_up()[1] == PENDING and "CLOSED:" not in h.log_text())
        # a stale generation
        a.scene(SCENE_SHOP0)
        b.scene(SCENE_SHOP0)
        L.pump_sleep(0.8)
        a.request_start(gen + 1, lvl + 1)
        L.pump_sleep(1.0)
        ck("U1 a request naming another upgrade generation is IGNORED", "names another upgrade" in h.log_text() and a.last_up()[1] == PENDING)
        # the race
        a.request_start(gen, lvl + 1)
        b.request_start(gen, lvl + 1)
        ok = wait_for(lambda: all(x.last_up() and x.last_up()[1] == CLOSED for x in (a, b)), 5.0)
        t_closed = time.monotonic()
        ck("U1 two simultaneous requests: both players see CLOSED with the SAME generation", ok and a.last_up()[5] == gen and b.last_up()[5] == gen)
        L.pump_sleep(1.0)
        ck("U1 exactly ONE start on the host ('CLOSED' once) and the second request was ignored as 'already started'",
           len(re.findall(r"\] host: CLOSED: the upgrade", h.log_text())) == 1 and "the closure was already started" in h.log_text())
        ini = L.server_file("shop_upgrade.ini")
        txt = open(ini).read() if os.path.isfile(ini) else ""
        ck("U1 shop_upgrade.ini holds this town's closure (state closed, target, end_wall_ms)", "state = closed" in txt and "target_level = %d" % (lvl + 1) in txt and "end_wall_ms" in txt)
        ck("U1 CLOSED carries the minutes left (1) and the closure length (20 s)", a.last_up()[6] == 1 and a.last_up()[7] == 20)
        m = console(h, "shop-upgrade", r"SHOP-UPGRADE: (\w+);")
        ck("U1 `shop-upgrade` reports CLOSED", m is not None and m.group(1) == "CLOSED")
        # purchases / restock during the closure
        s = b.send_txn_commit(L.PC_NETGAME_TXN_KIND_SHOP_RESTOCK, 50, L.PC_NETGAME_TXN_DEST_NONE, 0, 0, aux_cond=0, aux_item=500)
        r = b.wait_txn_result(s.seq, 5.0)
        ck("U1 a SHOP_RESTOCK during the closure is REJECTED NOT_AVAILABLE (16), nothing charged",
           r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and r.reason == R_NOT_AVAILABLE)
        free = [i for i, v in enumerate(L.record_inventory(b.rec_local)[0]) if v == 0]
        if free:
            s = b.send_txn_commit(L.PC_NETGAME_TXN_KIND_SHOP_BUY, 51, L.PC_NETGAME_TXN_DEST_POCKET, free[0], 0x2100, aux_cond=0, aux_item=100)
            r = b.wait_txn_result(s.seq, 5.0)
            ck("U1 a SHOP_BUY during the closure is REJECTED NOT_AVAILABLE (16) with the log reason 'closed for its upgrade'",
               r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and r.reason == R_NOT_AVAILABLE and "closed for its upgrade" in h.log_text())
        c = rig.join("C", ctx["g"]["C"], ctx["rec"]["C"])
        ok = wait_for(lambda: c.last_up() is not None, 6.0)
        ck("U1 a player who joins DURING the closure is told CLOSED (generation %d)" % gen, ok and c.last_up()[1] == CLOSED and c.last_up()[5] == gen)
        # B goes back to the town (the shop showed it out); A stays in the shop past the deadline
        b.scene(SCENE_FG)
        ok = wait_for(lambda: a.last_up()[1] == RENEWING, 25.0)
        t_ren = time.monotonic()
        ck("U1 RENEWING after the 20 s closure (%.1f s)" % (t_ren - t_closed), ok and 18.0 <= t_ren - t_closed <= 24.0)
        L.pump_sleep(4.0)
        ck("U1 the renewal WAITS while a player still stands in the shop (no 'was upgraded' yet, state RENEWING)",
           "the shop was upgraded" not in h.log_text() and a.last_up()[1] == RENEWING)
        n_fb = len(a.fblocks)
        a.scene(SCENE_FG)
        ok = wait_for(lambda: all(x.last_up() and x.last_up()[1] == IDLE for x in (a, b, c)), 20.0)
        ck("U1 once everybody is in the town the host renews: IDLE pushed to every peer, the new level %d" % (lvl + 1),
           ok and a.last_up()[2] == lvl + 1 and a.last_up()[3] == lvl + 1)
        log = h.log_text()
        ck("U1 host log: 'the shop was upgraded ... level %d' and the vanilla actor renewed it at the town reload" % (lvl + 1),
           re.search(r"the shop was upgraded \(.*\): level %d" % (lvl + 1), log) is not None and "shop renewal due" in log)
        m = re.search(r"published the shop acre (\d+): structure tile (\d+) = 0x([0-9A-F]{4})", log)
        ck("U1 the shop acre was published with the new structure tile 0x%04X" % (SHOP0 + lvl + 1), m is not None and int(m.group(3), 16) == SHOP0 + lvl + 1)
        if m is not None:
            acre, tile = int(m.group(1)), int(m.group(2))
            got = [items for (_t, ac, items) in a.fblocks[n_fb:] if ac == acre]
            ck("U1 a FIELD_BLOCK of acre %d with tile %d = 0x%04X reached the client" % (acre, tile, SHOP0 + lvl + 1), got and got[-1][tile] == SHOP0 + lvl + 1)
        shop_t = [t for (t, svc, _s, _b) in a.svc_log if svc == SVC_SHOP]
        idle_t = [t for (t, st, *_r) in a.up_events() if st == IDLE]
        ck("U1 the new SHOP mirror arrived no later than the IDLE service 6", shop_t and idle_t and max(shop_t) <= idle_t[-1] + 0.001)
        ck("U1 shop_upgrade.ini is gone after the durable save", not os.path.isfile(ini) and "the renewed shop was saved durably" in log)
        m = console(h, "shop-upgrade", r"SHOP-UPGRADE: (\w+); upgrade \d+; level (\d+)")
        ck("U1 `shop-upgrade` reports IDLE at level %d" % (lvl + 1), m is not None and m.group(1) == "IDLE" and int(m.group(2)) == lvl + 1)
        ctx["level"] = lvl + 1
    finally:
        rig.closeall()
        ck("U1 graceful stop (exit 0)", rig.stop(h) == 0)


def run_u2(rig, args, ctx):
    ck = rig.ck
    lvl = ctx.get("level")
    if lvl is None or lvl >= 2:
        ck("U2 skipped: needs U1 to have reached a level below Nookway (level %s)" % lvl, False)
        return
    env = {"AC_TEST_SHOP_UPGRADE_SECONDS": "60"}
    h, ok = rig.start("u2a", args.port, env)
    if not ok:
        return
    t_start_wall = None
    try:
        a = rig.join("A", ctx["g"]["A"], ctx["rec"]["A"], token=ctx["tok"]["A"])
        wait_for(lambda: a.last_up() is not None, 8.0)
        console(h, "shop-level-boost confirm", r"SHOP-LEVEL-BOOST: sales")
        ok = wait_for(lambda: a.last_up() and a.last_up()[1] == PENDING, 6.0)
        ck("U2 PENDING (from %d to %d)" % (lvl, lvl + 1), ok)
        if not ok:
            return
        # this time the OPERATOR starts the closure (`shop-upgrade start confirm`, e.g. when every player runs an older client that cannot hear the announcement)
        m = console(h, "shop-upgrade start confirm", r"SHOP-UPGRADE START: ([^\n]+)")
        ok = wait_for(lambda: a.last_up()[1] == CLOSED, 5.0)
        t_start_wall = time.time()
        ck("U2 `shop-upgrade start confirm` started the closure (%s) and the client sees CLOSED (60 s closure)" % (m and m.group(1)),
           m is not None and m.group(1).startswith("the closure started") and ok and a.last_up()[7] == 60 and "announced by the dedicated console" in h.log_text())
        a.scene(SCENE_FG)
        L.pump_sleep(8.0)
    finally:
        rig.closeall()
        ck("U2 the host stops DURING the closure (exit 0)", rig.stop(h) == 0)
    ini = L.server_file("shop_upgrade.ini")
    ck("U2 shop_upgrade.ini survived the stop", os.path.isfile(ini) and "state = closed" in open(ini).read())
    h, ok = rig.start("u2b", args.port, env)
    if not ok:
        return
    try:
        mo = wait_for(lambda: re.search(r"resuming the upgrade \d+ .* after a host restart: closed, (\d+) ms of the closure remaining", h.log_text()) is not None, 10.0)
        m = re.search(r"resuming the upgrade \d+ .* after a host restart: closed, (\d+) ms of the closure remaining", h.log_text())
        ck("U2 the restarted host RESUMED the closure ('resuming the upgrade ... closed, M ms remaining'), 0 < M < 60000", mo and 0 < int(m.group(1)) < 60000)
        b = rig.join("B", ctx["g"]["B"], ctx["rec"]["B"], token=ctx["tok"]["B"])
        ok = wait_for(lambda: b.last_up() is not None, 6.0)
        ck("U2 a client connecting after the restart is told CLOSED", ok and b.last_up()[1] == CLOSED)
        ok = wait_for(lambda: b.last_up()[1] == IDLE, 90.0)
        t_open_wall = time.time()
        ck("U2 the shop reopened after the restart (IDLE, level %d)" % (lvl + 1), ok and b.last_up()[3] == lvl + 1)
        if t_start_wall is not None:
            dt = t_open_wall - t_start_wall
            ck("U2 it reopened at the ORIGINAL deadline: %.1f s after the start (60 s closure + the renewal, window 58 .. 75)" % dt, 58.0 <= dt <= 75.0)
        ck("U2 shop_upgrade.ini is gone", not os.path.isfile(ini))
    finally:
        rig.closeall()
        ck("U2 graceful stop (exit 0)", rig.stop(h) == 0)


def run_u3(rig, args, ctx):
    ck = rig.ck
    h, ok = rig.start("u3", args.port, {"AC_MP_SHOP_UPGRADE": "0", "AC_TEST_SHOP_UPGRADE_SECONDS": "20"})
    if not ok:
        return
    try:
        a = rig.join("A", ctx["g"]["A"], ctx["rec"]["A"], token=ctx["tok"]["A"])
        ok = wait_for(lambda: a.last_up() is not None, 8.0)
        ua = a.last_up()
        ck("U3 AC_MP_SHOP_UPGRADE=0: service 6 says the feature is off (flags 0, state 0)", ok and ua[4] == 0 and ua[1] == IDLE)
        ck("U3 the host log says OFF (the vanilla 2-day renewal schedule)", "announced 5-minute shop upgrade: OFF" in h.log_text())
        m = console(h, "shop-upgrade", r"SHOP-UPGRADE: (OFF)")
        ck("U3 `shop-upgrade` reports OFF", m is not None)
        console(h, "shop-level-boost confirm", r"SHOP-LEVEL-BOOST")
        L.pump_sleep(3.0)
        ck("U3 no PENDING with the switch off (no 'PENDING:' line, service 6 unchanged)", "PENDING: the town qualifies" not in h.log_text() and a.last_up()[1] == IDLE)
    finally:
        rig.closeall()
        ck("U3 graceful stop (exit 0)", rig.stop(h) == 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=13311)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: this test mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_<name> copy only", file=sys.stderr)
        return 2
    results = []
    rig = Rig(results)
    ctx = {"g": {}, "rec": {}, "tok": {}}
    for k, (n, pid) in {"A": ("SHOPUPA", 0x4D11), "B": ("SHOPUPB", 0x4D12), "C": ("SHOPUPC", 0x4D13)}.items():
        ctx["g"][k] = L.guest_identity(n, pid, "HOMETWN", 0x5B31)
        ctx["rec"][k] = bytes(L.fresh_guest_record_for(ctx["g"][k]))
    L.server_wipe_sidecars()
    try:
        os.remove(L.server_file("shop_upgrade.ini"))
    except OSError:
        pass
    source_audit(rig.ck)
    for name, fn in (("U1", run_u1), ("U2", run_u2), ("U3", run_u3)):
        if args.only and name not in args.only.split(","):
            continue
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
