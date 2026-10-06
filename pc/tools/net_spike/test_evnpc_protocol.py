#!/usr/bin/env python3
"""test_evnpc_protocol.py - Event NPC authority (Patch 4): the host decides the OUTCOME of an event NPC's reward (TXN_COMMIT kind 17 EVNPC) -- races, duplicates, reconnect.

TIER: PROTOCOL TESTED against a REAL host process (scripted resident / guest FakeClients, the harness of test_shop_catalog_protocol.py). The host's event state is put into "Gulliver is visiting" /
"K.K. is playing" by the TEST-ONLY AC_TEST_EVNPC_SPAWN hook (AC_TEST_HOOKS=1); the real client half is covered by test_evnpc_real.py. Run on a DISPOSABLE copy
(NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>>).
  Phase GU (Gulliver):
    G1 two clients race for the gift: exactly ONE APPLIED (an item the HOST rolled lands in the free slot), the other EVNPC_DONE (36); the host committed ONCE
    G2 the winner's retry (new seq) and a reconnect of the winner are refused EVNPC_DONE: nobody gets a second gift of this visit
    G3 a duplicate packet of the winning request is answered from the journal (REPLAYED), not re-executed
    G4 a claim with an item in the tag is BAD_SHAPE
  Phase KK (K.K.):
    K1 a player asks for a song: APPLIED (MINIDISK + song), asking again is EVNPC_DONE (36); another player is independent
    K2 the bit is HOST-held: after a disconnect + reconnect the same player is still refused
    K3 an invalid song is BAD_SHAPE; a guest gets its song once (the guests share ONE bit)
  Phase NO (no visit): both claims are NOT_AVAILABLE (16)
  S  source audit
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_ts_protocol as TS  # noqa: E402
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE  # noqa: E402

K_EVNPC = 17
GIFT, SONG = 1, 2
REASON_PRECOND, REASON_NOT_AVAILABLE, REASON_BAD_SHAPE, REASON_EVNPC_DONE = 9, 16, 11, 36
MD = 0x2A00
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def claim(run, c, code, slot, aux=0, item=0, pre=None, wait=4.0):
    rid = run.fresh_rid()
    sent = c.send_txn_commit(K_EVNPC, rid, D_NONE, slot, item, pre=pre, aux_cond=code, aux_item=aux)
    return sent, c.wait_txn_result(sent.seq, wait)


def free_slot(c):
    return TP.first_free_pocket(c)


def audit(check):
    net = open(os.path.join(ROOT, "pc", "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read()
    dz = open(os.path.join(ROOT, "src", "actor", "npc", "event", "ac_ev_dozaemon_move.c_inc"), encoding="utf-8", errors="replace").read()
    kk = open(os.path.join(ROOT, "src", "actor", "npc", "ac_npc_totakeke_talk.c_inc"), encoding="utf-8", errors="replace").read()
    ctrl = open(os.path.join(ROOT, "src", "actor", "npc", "ac_npc_ctrl.c_inc"), encoding="utf-8", errors="replace").read()
    em = open(os.path.join(ROOT, "src", "actor", "ac_event_manager.c"), encoding="utf-8", errors="replace").read()
    check("S1 the allowlist, the host scan (only while the host's scene is the town field), the client create / remove and the local-spawn suppression exist",
          "int pc_net_game_evnpc_synced(int npc_id)" in net and "pcnetgame_evnpc_field_scene()" in net and "pcnetgame_evnpc_client_tick" in net and "pc_net_game_evnpc_suppress_spawn((int)name)" in ctrl)
    check("S2 the host creates event NPCs without the 'a player is nearby' gate (every one of the three show_actor_at_wade variants)", em.count("aEvMgr_HOST_UNBOUNDED(") >= 5)
    check("S3 the outcome is decided by ONE plan on the host (Gulliver: the gift check rolls with the host RNG; K.K.: the per-player bit is host-held) and the flag is set in the SAME step the item is granted",
          "static uint8_t pcnetgame_evnpc_plan(" in net and "pcnetgame_evnpc_commit(idx < PLAYER_NUM ? idx : -1, t->aux_cond)" in net and "mSP_SelectRandomItem_New" in dz
          and "aNTT_pc_host_song_commit" in kk)
    check("S4 a READY client never rolls / grants itself: Gulliver and K.K. take the claim path (pc_net_game_evnpc_claim_active)", "pc_net_game_evnpc_claim_active()" in dz and "pc_net_game_evnpc_claim_active()" in kk)


def phase_gulliver(run):
    ck = run.check
    run.host.wait_for_log(r"\[NET\]\[EVNPC\]\[TEST-ONLY\] host: creating event NPC 0xD064", 30.0)
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    ck("GU0 setup: A and B READY and SYNCED", a.rec_synced and b.rec_synced)
    sa, sb = free_slot(a), free_slot(b)
    off = len(run.log())
    sent_a = a.send_txn_commit(K_EVNPC, run.fresh_rid(), D_NONE, sa, 0, aux_cond=GIFT, aux_item=0)
    sent_b = b.send_txn_commit(K_EVNPC, run.fresh_rid(), D_NONE, sb, 0, aux_cond=GIFT, aux_item=0)
    ra, rb = a.wait_txn_result(sent_a.seq, 5.0), b.wait_txn_result(sent_b.seq, 5.0)
    wins = [(c, s, r, snt) for c, s, r, snt in ((a, sa, ra, sent_a), (b, sb, rb, sent_b)) if r is not None and r.outcome == APPLIED]
    lose = [r for r in (ra, rb) if r is not None and r.outcome == REJECTED and r.reason == REASON_EVNPC_DONE]
    ck("G1 the race: exactly ONE claim APPLIED and the other EVNPC_DONE (36)", len(wins) == 1 and len(lose) == 1)
    ck("G1 the winner's free slot holds a non-empty item the HOST rolled", len(wins) == 1 and wins[0][2].post_pockets[wins[0][1]] != 0)
    ck("G1 the host committed exactly once", run.n_log(r"EVNPC claim op 1 committed", off) == 1)
    if len(wins) != 1:
        return
    wc, ws, wr, winner_sent = wins[0]
    sent, r = claim(run, wc, GIFT, free_slot(wc))
    ck("G2 the winner's retry (new seq) is refused EVNPC_DONE (36)", r is not None and r.outcome == REJECTED and r.reason == REASON_EVNPC_DONE)
    wc.resend_txn(winner_sent)
    r3 = wc.wait_txn_result(winner_sent.seq, 4.0, nth=2)
    ck("G3 a duplicate packet of the winning request is answered from the journal (APPLIED, REPLAYED)", r3 is not None and r3.outcome == APPLIED and r3.reason == R["REPLAYED"])
    L.pump_sleep(0.4)
    ck("G3 still ONE commit", run.n_log(r"EVNPC claim op 1 committed", off) == 1)
    other = b if wc is a else a
    sent, r = claim(run, other, GIFT, free_slot(other), item=0x2000)
    ck("G4 a claim with an item in the tag is BAD_SHAPE (11)", r is not None and r.outcome == REJECTED and r.reason == REASON_BAD_SHAPE)
    idx = run.r1 if wc is a else run.r2
    run.release(wc)
    wc2 = run.ready("W2", idx)
    sent, r = claim(run, wc2, GIFT, free_slot(wc2))
    ck("G2 after a disconnect + reconnect the winner is still refused EVNPC_DONE: the 'given' flag is the host's", r is not None and r.outcome == REJECTED and r.reason == REASON_EVNPC_DONE)


def phase_kk(run):
    ck = run.check
    run.host.wait_for_log(r"\[NET\]\[EVNPC\]\[TEST-ONLY\] host: creating event NPC 0xD05D", 30.0)
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    ck("KK0 setup: A and B READY and SYNCED", a.rec_synced and b.rec_synced)
    sa = free_slot(a)
    sent, r = claim(run, a, SONG, sa, aux=5)
    run.tx_ok("K1 A asks for song 5", r, APPLIED, R["NONE"])
    ck("K1 the item is MINIDISK + 5 in the free slot", r is not None and r.post_pockets[sa] == MD + 5)
    sent, r = claim(run, a, SONG, free_slot(a), aux=6)
    ck("K1 asking again is refused EVNPC_DONE (36): one song per player per concert", r is not None and r.outcome == REJECTED and r.reason == REASON_EVNPC_DONE)
    sb = free_slot(b)
    sent, r = claim(run, b, SONG, sb, aux=7)
    ck("K1 B is independent: song 7 is APPLIED too (MINIDISK + 7)", r is not None and r.outcome == APPLIED and r.post_pockets[sb] == MD + 7)
    sent, r = claim(run, b, SONG, free_slot(b), aux=1)
    ck("K1 B asking again is EVNPC_DONE (36)", r is not None and r.outcome == REJECTED and r.reason == REASON_EVNPC_DONE)
    run.release(a)
    a2 = run.ready("A2", run.r1)
    sent, r = claim(run, a2, SONG, free_slot(a2), aux=2)
    ck("K2 after a disconnect + reconnect A is still refused EVNPC_DONE: the bit is held by the HOST (a client restart cannot ask twice)", r is not None and r.outcome == REJECTED and r.reason == REASON_EVNPC_DONE)
    sent, r = claim(run, b, SONG, free_slot(b), aux=0x37)
    ck("K3 K.K.'s random song (0x37, no item) / any invalid song is BAD_SHAPE (11)", r is not None and r.outcome == REJECTED and r.reason == REASON_BAD_SHAPE)
    g = L.FakeClient("G", run.ip, run.port, guest=L.guest_identity("KKGUEST", 0x4C01))
    run.clients.append(g)
    try:
        g.connect_and_ready(quiet=True)
        sg = free_slot(g)
        sent, r = claim(run, g, SONG, sg, aux=3)
        ck("K3 a guest gets its song (the guests' shared bit)", r is not None and r.outcome == APPLIED and r.post_pockets[sg] == MD + 3)
        sent, r = claim(run, g, SONG, free_slot(g), aux=4)
        ck("K3 the guest asking again is EVNPC_DONE", r is not None and r.outcome == REJECTED and r.reason == REASON_EVNPC_DONE)
    except Exception as exc:  # noqa: BLE001
        ck("K3 the guest connected (%r)" % (exc,), False)


def phase_none(run):
    ck = run.check
    a = run.ready("A", run.r1)
    ck("NO0 setup: A READY and SYNCED", a.rec_synced)
    sent, r = claim(run, a, GIFT, free_slot(a))
    ck("N1 no Gulliver visit: NOT_AVAILABLE (16)", r is not None and r.outcome == REJECTED and r.reason == REASON_NOT_AVAILABLE)
    sent, r = claim(run, a, SONG, free_slot(a), aux=1)
    ck("N1 no K.K. concert: NOT_AVAILABLE (16)", r is not None and r.outcome == REJECTED and r.reason == REASON_NOT_AVAILABLE)


def run_all(port, results):
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: this test mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_<name> copy only (NET_SPIKE_GAME_BIN=<abs path>)", file=sys.stderr)
        return 2
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    audit(ck)
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "evnpc_proto_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        for i, (tag, spawn, body) in enumerate((("GU", "D064,3,3,8,8", phase_gulliver), ("KK", "D05D,1,3,7,7", phase_kk), ("NO", "0,0,0,0,0", phase_none))):
            if spawn:
                os.environ["AC_TEST_EVNPC_SPAWN"] = spawn
            else:
                os.environ.pop("AC_TEST_EVNPC_SPAWN", None)
            TS.run_phase(results, ip, port + i, tag, [], body, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11540)
    args = ap.parse_args()
    results = []
    rc = run_all(args.port, results)
    if rc is not None:
        return rc
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
