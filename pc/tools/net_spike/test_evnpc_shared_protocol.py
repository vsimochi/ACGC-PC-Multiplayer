#!/usr/bin/env python3
"""test_evnpc_shared_protocol.py - SHARED EVENT NPCs (worker E): the host decides Redd's / Saharah's / Wendell's / Gracie's / the Wisp's outcome (TXN_COMMIT kind 21
EVNPC_CLAIM, TS service 9 EVNPC_CAPS) -- races, replays, conservation, quotas, the kill switch.

TIER: PROTOCOL TESTED against a REAL host process (scripted resident / guest FakeClients, the harness of test_evnpc_protocol.py). The host's visit is seeded by the TEST-ONLY
AC_TEST_EVNPC_SEED hook (AC_TEST_HOOKS=1). Run on a DISPOSABLE copy (NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_ev_<name>>). Ports 21000+.
  CAPS  every READY client gets service 9: version 1, claims ON, ops 1..5 (0x3E)
  RD    Redd: two residents race for lineup piece 0 -> exactly one APPLIED (the piece in its free slot, the wallet minus the host price, nothing else changed = conservation),
        the other SOLD_OUT (50) with nothing charged; a byte-identical resend of the winner is REPLAYED (one commit); a new seq for the same piece is SOLD_OUT; a wrong price
        is EVC_CHANGED (52); an empty wallet is NO_FUNDS (19); lineup index 3 is BAD_SHAPE; a guest buys piece 2; the loser's next service 5 shows piece 0 sold
  SA    Saharah: a carpet + 3000 -> a host-rolled carpet in the same slot; the second trader at the stale 3000 is EVC_CHANGED, at 6000 APPLIED
  AR    Wendell: a fish -> a wallpaper (once per player: CLAIMED 51), three per visit (the 4th player: SOLD_OUT 50)
  DS    Gracie: result 0 -> a cloth in the free slot; once per player (51); three per visit (50) -- no write past pids[3]
  WP    the Wisp: 4 spirit points -> NO_SPIRITS (54); 5 points -> APPLIED, every spirit slot emptied, the item in the spirit slot, service 9 says "returned";
        another resident -> CLAIMED (51); a guest -> PRECOND (9)
  NO    no visitor: every op NOT_AVAILABLE (16)
  OFF   host AC_MP_EVENT_NPC=0: service 9 says OFF and a claim is EVC_OFF (53)
"""
import argparse
import os
import re
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_ts_protocol as TS  # noqa: E402
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE, D_POCKET  # noqa: E402

K_EVC = 21
REDD, SAHARAH, ARTIST, DESIGNER, WISP = 1, 2, 3, 4, 5
PRECOND, NOT_AVAILABLE, BAD_SHAPE, NO_FUNDS = 9, 16, 11, 19
SOLD_OUT, CLAIMED, CHANGED, OFF, NO_SPIRITS = 50, 51, 52, 53, 54
SVC_EVENT, SVC_EVNPC = 5, 9
SPIRIT0 = 0x2D28
FISH = 0x2300
CARPET = 0x2600
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def cat(item):
    return (item >> 8) & 0xF if (item >> 12) == 2 else -1


def pre_with(c, changes=None, wallet=None):
    pockets, conds, w = c.txn_pre_image()
    pockets = list(pockets)
    for i, v in (changes or {}).items():
        pockets[i] = v
        conds &= ~(3 << (2 * i))
    return tuple(pockets), conds, (w if wallet is None else wallet)


def free_slots(pre, n=1):
    return [i for i, p in enumerate(pre[0]) if p == 0][:n]


def claim(run, c, op, dest, slot, item=0, flags=0, aux=0, pre=None, wait=5.0):
    sent = c.send_txn_commit(K_EVC, run.fresh_rid(), dest, slot, item, pre=pre, aux_cond=op, aux_item=aux, flags=flags)
    return sent, c.wait_txn_result(sent.seq, wait)


def is_rej(r, reason):
    return r is not None and r.outcome == REJECTED and r.reason == reason


def conserved(pre, r, slot, new_item, price):
    """post == pre except: `slot` holds new_item, the wallet paid `price` (money bags are not used: the pre wallet covers it)"""
    if r is None or r.outcome != APPLIED:
        return False
    exp = list(pre[0])
    exp[slot] = new_item
    return list(r.post_pockets) == exp and r.post_wallet == pre[2] - price


def caps(c):
    st = c.ts_states_of(SVC_EVNPC)
    return st[-1][1] if st else None


def wait_caps(c, timeout=6.0):
    waited = 0.0
    while caps(c) is None and waited < timeout:
        L.pump_sleep(0.25)
        waited += 0.25
    return caps(c)


def guest(run, label, key):
    g = L.FakeClient(label, run.ip, run.port, guest=L.guest_identity(label, key))
    run.clients.append(g)
    g.connect_and_ready(quiet=True)
    return g


def redd_lineup(run):
    m = re.search(r"seeded Redd: lineup 0x([0-9A-F]{4}) 0x([0-9A-F]{4}) 0x([0-9A-F]{4}) prices (\d+) (\d+) (\d+)", run.log())
    if not m:
        return None
    return [int(m.group(i), 16) for i in (1, 2, 3)], [int(m.group(i)) for i in (4, 5, 6)]


def audit(check):
    rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8", errors="replace").read()  # noqa: E731
    net = rd("pc", "src", "pc_net_game.c")
    check("S1 kind 21 is dispatched to its own one-phase handler (journal, base, pre-image, plan, commit, journal, APPLIED, services 5 + 9 pushed)",
          "pcnetgame_handle_host_evc_txn(peer, &tc);" in net and "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED" in net
          and "(void)pcnetgame_ts_refresh((int)PC_NETGAME_TS_EVNPC); /* the Wisp flag */" in net)


def phase_caps_and_redd(run):
    ck = run.check
    run.host.wait_for_log(r"\[NET\]\[EVC\]\[TEST-ONLY\] host: seeded Redd", 40.0)
    lu = redd_lineup(run)
    ck("RD0 the host seeded Redd's lineup (3 pieces with a price)", lu is not None and all(lu[0]) and all(lu[1]))
    if lu is None:
        return
    items, prices = lu
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    ca, cb = wait_caps(a), wait_caps(b)
    ck("CAPS service 9 reached both clients: version 1, claims ON, ops 0x3E, Wisp not returned",
       ca is not None and cb is not None and ca[0] == 1 and ca[1] == 1 and ca[2] == 0 and struct.unpack_from("<I", ca, 4)[0] == 0x3E)
    pa = pre_with(a, wallet=99999)
    pb = pre_with(b, wallet=99999)
    sa, sb = free_slots(pa)[0], free_slots(pb)[0]
    off = len(run.log())
    sent_a = a.send_txn_commit(K_EVC, run.fresh_rid(), D_POCKET, sa, items[0], pre=pa, aux_cond=REDD, aux_item=prices[0] & 0xFFFF, flags=0)
    sent_b = b.send_txn_commit(K_EVC, run.fresh_rid(), D_POCKET, sb, items[0], pre=pb, aux_cond=REDD, aux_item=prices[0] & 0xFFFF, flags=0)
    ra, rb = a.wait_txn_result(sent_a.seq, 6.0), b.wait_txn_result(sent_b.seq, 6.0)
    wins = [(c, s, r, snt, p) for c, s, r, snt, p in ((a, sa, ra, sent_a, pa), (b, sb, rb, sent_b, pb)) if r is not None and r.outcome == APPLIED]
    lose = [r for r in (ra, rb) if is_rej(r, SOLD_OUT)]
    ck("RD1 the race for piece 0: exactly ONE APPLIED and the other SOLD_OUT (50)", len(wins) == 1 and len(lose) == 1)
    ck("RD1 the host committed exactly once", run.n_log(r"REDD_BUY APPLIED \[TXN\]", off) == 1)
    if len(wins) != 1:
        return
    wc, ws, wr, wsent, wpre = wins[0]
    ck("RD1 conservation: the winner's post-image = pre-image + the piece in the free slot - the host price (%d), nothing else" % prices[0],
       conserved(wpre, wr, ws, items[0], prices[0]))
    loser = b if wc is a else a
    lr = rb if wc is a else ra
    ck("RD1 the loser's result carries no post-image (nothing charged, nothing granted)", lr is not None and lr.post_wallet == 0 and all(p == 0 for p in lr.post_pockets))
    wc.resend_txn(wsent)
    r2 = wc.wait_txn_result(wsent.seq, 4.0, nth=2)
    ck("RD2 a byte-identical resend of the winning request is APPLIED/REPLAYED (journal), not re-executed",
       r2 is not None and r2.outcome == APPLIED and r2.reason == R["REPLAYED"])
    L.pump_sleep(0.4)
    ck("RD2 still ONE commit", run.n_log(r"REDD_BUY APPLIED \[TXN\]", off) == 1)
    p2 = pre_with(loser, wallet=99999)
    _s, r = claim(run, loser, REDD, D_POCKET, free_slots(p2)[0], items[0], 0, prices[0] & 0xFFFF, pre=p2)
    ck("RD3 a new request for the sold piece is SOLD_OUT (50)", is_rej(r, SOLD_OUT))
    _s, r = claim(run, loser, REDD, D_POCKET, free_slots(p2)[0], items[1], 1, (prices[1] + 4) & 0xFFFF, pre=p2)
    ck("RD4 a wrong price for piece 1 is EVC_CHANGED (52), nothing charged", is_rej(r, CHANGED))
    p0 = pre_with(loser, wallet=0)
    p0 = (tuple(0 if 0x2100 <= v <= 0x2103 else v for v in p0[0]), p0[1], 0)  # no money bags either (ITM_MONEY_1000..100)
    _s, r = claim(run, loser, REDD, D_POCKET, free_slots(p0)[0], items[1], 1, prices[1] & 0xFFFF, pre=p0)
    ck("RD5 an empty wallet is NO_FUNDS (19)", is_rej(r, NO_FUNDS))
    _s, r = claim(run, loser, REDD, D_POCKET, free_slots(p2)[0], items[1], 3, prices[1] & 0xFFFF, pre=p2)
    ck("RD6 lineup index 3 is BAD_SHAPE (11)", is_rej(r, BAD_SHAPE))
    try:
        g = guest(run, "REDDG", 0x5E01)
        L.pump_sleep(1.0)
        pg = pre_with(g, wallet=99999)
        sg = free_slots(pg)[0]
        _s, r = claim(run, g, REDD, D_POCKET, sg, items[2], 2, prices[2] & 0xFFFF, pre=pg)
        ck("RD7 a GUEST buys piece 2 (guests are full shop participants): APPLIED, conserved", conserved(pg, r, sg, items[2], prices[2]))
    except Exception as exc:  # noqa: BLE001
        ck("RD7 the guest connected (%r)" % (exc,), False)
    L.pump_sleep(1.5)
    ev = loser.ts_states_of(SVC_EVENT)
    blob = ev[-1][1] if ev else b""
    ck("RD8 the loser's newest EVENT service shows piece 0 (and piece 2) sold (items[] at blob offset 68)",
       len(blob) >= 74 and struct.unpack_from("<3H", blob, 68)[0] == 0 and struct.unpack_from("<3H", blob, 68)[2] == 0)


def phase_saharah(run):
    ck = run.check
    run.host.wait_for_log(r"\[NET\]\[EVC\]\[TEST-ONLY\] host: seeded saharah", 40.0)
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    wait_caps(a)
    pa = pre_with(a, wallet=99999)
    s = free_slots(pa)[0]
    pa = pre_with(a, {s: CARPET + 3}, wallet=99999)
    _sent, r = claim(run, a, SAHARAH, D_POCKET, s, CARPET + 3, 0, 3000, pre=pa)
    ck("SA1 a carpet + 3000 Bells: APPLIED, a host-rolled carpet in the same slot, the wallet minus 3000, nothing else changed",
       r is not None and r.outcome == APPLIED and cat(r.post_pockets[s]) == 6 and conserved(pa, r, s, r.post_pockets[s], 3000))
    pb = pre_with(b, wallet=99999)
    sb = free_slots(pb)[0]
    pb = pre_with(b, {sb: CARPET + 5}, wallet=99999)
    _sent, r = claim(run, b, SAHARAH, D_POCKET, sb, CARPET + 5, 0, 3000, pre=pb)
    ck("SA2 the next trader at the stale 3000 Bells is EVC_CHANGED (52): the town's counter moved", is_rej(r, CHANGED))
    _sent, r = claim(run, b, SAHARAH, D_POCKET, sb, CARPET + 5, 0, 6000, pre=pb)
    ck("SA3 at 6000 Bells: APPLIED", r is not None and r.outcome == APPLIED and cat(r.post_pockets[sb]) == 6 and r.post_wallet == 99999 - 6000)
    _sent, r = claim(run, b, SAHARAH, D_POCKET, sb, FISH, 0, 12000, pre=pre_with(b, {sb: FISH}, wallet=99999))
    ck("SA4 a non-carpet handed over is PRECOND (9)", is_rej(r, PRECOND))


def phase_artist(run):
    ck = run.check
    run.host.wait_for_log(r"\[NET\]\[EVC\]\[TEST-ONLY\] host: seeded artist", 40.0)
    cl = [run.ready("A", run.r1), run.ready("B", run.r2), run.ready("C", run.r3)]
    wait_caps(cl[0])
    walls = []
    for i, c in enumerate(cl):
        p = pre_with(c)
        s = free_slots(p)[0]
        p = pre_with(c, {s: FISH + 7})
        _sent, r = claim(run, c, ARTIST, D_POCKET, s, FISH + 7, pre=p)
        ok = r is not None and r.outcome == APPLIED and cat(r.post_pockets[s]) == 7 and conserved(p, r, s, r.post_pockets[s], 0)
        ck("AR%d resident %s hands a fish: APPLIED, a wallpaper replaces it (conserved)" % (i + 1, "ABC"[i]), ok)
        if ok:
            walls.append(r.post_pockets[s])
        if i == 0:
            p2 = pre_with(c, {free_slots(pre_with(c))[0]: FISH + 8})
            s2 = [k for k, v in enumerate(p2[0]) if v == FISH + 8][0]
            _sent, r2 = claim(run, c, ARTIST, D_POCKET, s2, FISH + 8, pre=p2)
            ck("AR1 the same player again is CLAIMED (51): once per player per visit", is_rej(r2, CLAIMED))
    ck("AR2 the first two wallpapers differ (the host excludes the ones already given)", len(walls) >= 2 and walls[0] != walls[1])
    try:
        g = guest(run, "ARTG", 0x5E02)
        L.pump_sleep(1.0)
        p = pre_with(g)
        s = free_slots(p)[0]
        _sent, r = claim(run, g, ARTIST, D_POCKET, s, FISH + 9, pre=pre_with(g, {s: FISH + 9}))
        ck("AR4 a 4th player after three gifts is SOLD_OUT (50)", is_rej(r, SOLD_OUT))
    except Exception as exc:  # noqa: BLE001
        ck("AR4 the guest connected (%r)" % (exc,), False)


def phase_designer(run):
    ck = run.check
    run.host.wait_for_log(r"\[NET\]\[EVC\]\[TEST-ONLY\] host: seeded designer", 40.0)
    cl = [run.ready("A", run.r1), run.ready("B", run.r2), run.ready("C", run.r3)]
    wait_caps(cl[0])
    for i, c in enumerate(cl):
        p = pre_with(c)
        s = free_slots(p)[0]
        _sent, r = claim(run, c, DESIGNER, D_NONE, s, 0, 0, i & 1, pre=p)
        ck("DS%d resident %s result %d: APPLIED, a cloth in the free slot (conserved)" % (i + 1, "ABC"[i], i & 1),
           r is not None and r.outcome == APPLIED and cat(r.post_pockets[s]) == 4 and conserved(p, r, s, r.post_pockets[s], 0))
        if i == 0:
            p = pre_with(c)
            _sent, r = claim(run, c, DESIGNER, D_NONE, free_slots(p)[0], 0, 0, 0, pre=p)
            ck("DS1 the same player again is CLAIMED (51)", is_rej(r, CLAIMED))
    _sent, r = claim(run, cl[0], DESIGNER, D_NONE, 0, 0, 0, 2)
    ck("DS4 result 2 (the minigame failed) is BAD_SHAPE: never a gift", is_rej(r, BAD_SHAPE))
    try:
        g = guest(run, "DSGG", 0x5E03)
        L.pump_sleep(1.0)
        p = pre_with(g)
        _sent, r = claim(run, g, DESIGNER, D_NONE, free_slots(p)[0], 0, 0, 0, pre=p)
        ck("DS5 a 4th player after three gifts is SOLD_OUT (50): no write past pids[3]", is_rej(r, SOLD_OUT))
    except Exception as exc:  # noqa: BLE001
        ck("DS5 the guest connected (%r)" % (exc,), False)


def phase_wisp(run):
    ck = run.check
    run.host.wait_for_log(r"\[NET\]\[EVC\]\[TEST-ONLY\] host: seeded the Wisp", 40.0)
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    wait_caps(a)
    fs = free_slots(pre_with(a), 2)
    p4 = pre_with(a, {fs[0]: SPIRIT0 + 3})  # 4 points
    _sent, r = claim(run, a, WISP, D_NONE, fs[0], 0, 0, 2, pre=p4)
    ck("WP1 4 spirit points: NO_SPIRITS (54), nothing taken", is_rej(r, NO_SPIRITS))
    p5 = pre_with(a, {fs[0]: SPIRIT0 + 2, fs[1]: SPIRIT0 + 1})  # 3 + 2 = 5 points in two slots
    off = len(run.log())
    _sent, r = claim(run, a, WISP, D_NONE, fs[0], 0, 0, 2, pre=p5)
    ok = r is not None and r.outcome == APPLIED and r.post_pockets[fs[1]] == 0 and r.post_pockets[fs[0]] != 0 and \
        not (SPIRIT0 <= r.post_pockets[fs[0]] <= SPIRIT0 + 4) and r.post_wallet == p5[2]
    ck("WP2 5 points: APPLIED, every spirit slot emptied, the host-rolled item in the claim's spirit slot, the wallet unchanged", ok)
    ck("WP2 the host logged the turn-in once", run.n_log(r"WISP_RETURN APPLIED \[TXN\]", off) == 1)
    L.pump_sleep(2.0)
    cb = caps(b)
    ck("WP3 service 9 now says the host's Wisp has its spirits back (byte 2 = 1) at the OTHER client", cb is not None and cb[2] == 1)
    fb = free_slots(pre_with(b), 1)
    _sent, r = claim(run, b, WISP, D_NONE, fb[0], 0, 0, 0, pre=pre_with(b, {fb[0]: SPIRIT0 + 4}))
    ck("WP4 another resident with 5 points afterwards is CLAIMED (51): once per town", is_rej(r, CLAIMED))
    try:
        g = guest(run, "WSPG", 0x5E04)
        L.pump_sleep(1.0)
        fg = free_slots(pre_with(g), 1)
        _sent, r = claim(run, g, WISP, D_NONE, fg[0], 0, 0, 0, pre=pre_with(g, {fg[0]: SPIRIT0 + 4}))
        ck("WP5 a GUEST is PRECOND (9): the Wisp never deals with visitors (vanilla)", is_rej(r, PRECOND))
    except Exception as exc:  # noqa: BLE001
        ck("WP5 the guest connected (%r)" % (exc,), False)


def phase_none(run):
    ck = run.check
    run.host.wait_for_log(r"\[NET\]\[EVC\]\[TEST-ONLY\] host: seeded NO visitor", 40.0)
    a = run.ready("A", run.r1)
    wait_caps(a)
    p = pre_with(a, wallet=99999)
    s = free_slots(p)[0]
    _x, r1 = claim(run, a, REDD, D_POCKET, s, 0x1000, 0, 100, pre=p)
    _x, r2 = claim(run, a, DESIGNER, D_NONE, s, 0, 0, 0, pre=p)
    _x, r3 = claim(run, a, WISP, D_NONE, s, 0, 0, 0, pre=pre_with(a, {s: SPIRIT0 + 4}))
    ck("NO1 no visitor: Redd / Gracie / the Wisp are NOT_AVAILABLE (16)", is_rej(r1, NOT_AVAILABLE) and is_rej(r2, NOT_AVAILABLE) and is_rej(r3, NOT_AVAILABLE))


def phase_off(run):
    ck = run.check
    a = run.ready("A", run.r1)
    c = wait_caps(a)
    ck("OFF1 AC_MP_EVENT_NPC=0 on the host: service 9 says claims OFF", c is not None and c[1] == 0)
    p = pre_with(a)
    _x, r = claim(run, a, DESIGNER, D_NONE, free_slots(p)[0], 0, 0, 0, pre=p)
    ck("OFF2 a claim anyway is EVC_OFF (53), not BAD_SHAPE (no violation)", is_rej(r, OFF))


def run_all(port, only, results):
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: this test mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_<name> copy only (NET_SPIKE_GAME_BIN=<abs path>)", file=sys.stderr)
        return 2
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    audit(ck)
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "evc_proto_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    phases = (("RD", "redd", None, phase_caps_and_redd), ("SA", "saharah", None, phase_saharah), ("AR", "artist", None, phase_artist),
              ("DS", "designer", None, phase_designer), ("WP", "wisp", None, phase_wisp), ("NO", "none", None, phase_none), ("OFF", "designer", "0", phase_off))
    try:
        for i, (tag, seed, off, body) in enumerate(phases):
            if only and tag not in only:
                continue
            os.environ["AC_TEST_EVNPC_SEED"] = seed
            if off is not None:
                os.environ["AC_MP_EVENT_NPC"] = off
            else:
                os.environ.pop("AC_MP_EVENT_NPC", None)
            TS.run_phase(results, ip, port + i, "EVC" + tag, [], body, snap_gci, restore)
    finally:
        os.environ.pop("AC_MP_EVENT_NPC", None)
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=21100)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    if not (21000 <= args.port <= 21990):
        print("ports 21000-21999 only (worker E)", file=sys.stderr)
        return 2
    results = []
    rc = run_all(args.port, set(x for x in args.only.split(",") if x), results)
    if rc is not None:
        return rc
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
