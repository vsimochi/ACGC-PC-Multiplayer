#!/usr/bin/env python3
"""test_work_v2_protocol.py - Nook Work Mode v2 (pc/include/pc_work_logic.h) against a REAL host process: ops 8 URGE / 9 QUIT / 10 VILLAGER_COMPLETE, the v2 tag flag,
town service 11 WORK_CAPS, the WORK_STATE flag bits (latched per peer), the varied items, the friendship tip, the switch.

TIER: PROTOCOL TESTED (real host binary, `--host --bootstrap-resident`, scripted resident FakeClients -- the harness of test_work_mode_protocol.py). The dialogue seams
(ac_npc_shop_common.c / ac_quest_talk_work.c_inc) are covered by test_work_v2_src.py and the real run test_work_v2_real.py.
Fixture: pc\\build64\\bin_fixture4_wv2_proto (a fresh copy of bin_fixture4 + the exe of pc\\build64_w3; the save is snapshotted and restored). Host env: AC_TEST_HOOKS=1,
AC_TEST_WORK_TYPE=3,2,3,1,3,3 (the job types of the created jobs in order), AC_TEST_WORK_URGENT=1 for the first host (every job urgent when asked; the unforced roll is the
pure pc_work_v2_urgent, covered by the native selftest).
  V0  service 11 reaches every READY peer with bit 0 (Work v2 on); an old-style client (never sends the flag) gets WORK_STATE with NO bit beyond bit 0
  V1  v2 ENTER (flag): a v2 job (state bit JOB_V2 once the peer is latched), the item from the varied parcel pool (logged with its game name), no tip decided at creation
  V2  URGE (op 8): the bonus is added ONCE (reward + 100..300), bit URGED; a second URGE adds nothing; a stale job id is refused
  V3  VILLAGER_COMPLETE (op 10): wrong villager / an ordinary copy / another item refused (nothing moves); the right one = ONE step: the parcel slot emptied, the wallet
      grows by EXACTLY reward + tip, the job retired (state NONE, AUTO bit, jobs_done + 1), tip = pc_work_v2_tip(job id, the logged friendship); replay = REPLAYED, nothing
      re-paid; a new-seq COMPLETE / a REPORT afterwards = WORK_NO_JOB
  V4  QUIT (op 9) of a FETCH job carrying its QUEST item: an ordinary copy / a stale id refused; the right one removes the item, pays nothing, ends the job and Work Mode
  V5  COMPLETE with a wallet near the cap: 30000 of (wallet + reward + tip) becomes a money bag in the vacated slot, nothing created or lost
  V6  a v2 FRUIT job delivered to Nook pays the reward and NO tip
  V7  an old-style client's ENTER is a v1 job (stationery, the old tip rule) and its WORK_STATE never carries the v2 bits; a reconnect of the v2 client starts unlatched
  V8  host restart with AC_MP_WORK_V2=0: service 11 says off; op 8 / the v2 flag = BAD_SHAPE; the persisted v2 job (with its urged reward) still resolves with the old ops:
      RECEIVE pays the v2 friendship tip, REPORT pays the reward
Usage: python test_work_v2_protocol.py [--port 11180]
"""
import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_wv2_proto"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)
os.environ["AC_TEST_HOOKS"] = "1"
import test_work_mode_protocol as WM  # noqa: E402  (imports net_spike_lib with the fixture above)

os.environ["AC_TEST_WORK_TYPE"] = "3,2,3,1,3,3"
L = WM.L
TP = WM.TP
TS = WM.TS
APPLIED, REJECTED, R = WM.APPLIED, WM.REJECTED, WM.R
ENTER, DELIVER, LEAVE, TAKE, GIVE, RECEIVE, REPORT = WM.ENTER, WM.DELIVER, WM.LEAVE, WM.TAKE, WM.GIVE, WM.RECEIVE, WM.REPORT
URGE, QUIT, COMPLETE = 8, 9, 10
FLAG_V2 = 1
SF_JOB_V2, SF_URGED, SF_AUTO = 0x02, 0x04, 0x08
SVC_WORK = 11
BAD_SHAPE = R["BAD_SHAPE"]
REASON_PRECOND, REASON_NO_JOB, REASON_STALE_JOB = WM.REASON_PRECOND, WM.REASON_NO_JOB, WM.REASON_STALE_JOB
QUEST_COND, NORMAL_COND = WM.QUEST_COND, WM.NORMAL_COND
M32 = 0xFFFFFFFF


# ---- the pure model (pc_work_logic.h), computed independently in Python
def mix(job, salt):
    h = ((job * 0x9E3779B1) & M32) ^ salt
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & M32
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & M32
    h ^= h >> 16
    return h


def tip_model(job, friendship):
    base = 50 + (mix(job, 0x54495031) % 26) * 10
    f = 0 if friendship <= 0 else min(friendship, 127)
    return ((base + 2 * f) // 10) * 10


def bonus_model(job):
    return 100 + (mix(job, 0x424F4E31) % 5) * 50


def op(run, c, code, slot=0, item=0, aux=0, pre=None, flags=FLAG_V2, wait=4.0):
    mark = c.inbox.mark()
    rid = run.fresh_rid()
    sent = c.send_txn_commit(WM.K_WORK, rid, WM.D_NONE, slot, item, pre=pre, aux_cond=code, aux_item=aux, flags=flags)
    return mark, sent, c.wait_txn_result(sent.seq, wait)


def rej(r, reason):
    return r is not None and r.outcome == REJECTED and r.reason == reason


def item_pool_text():
    """the two pools as the C source lists them (names of the constants), for the log"""
    src = open(os.path.join(WM.ROOT, "pc", "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read()
    return "s_work_v2_fetch_pool" in src and "s_work_v2_parcel_pool" in src


def phase(run):
    ck = run.check
    ps = WM.paper_start()
    papers = {ps + i for i in range(64)}
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    ck("V0 setup: A (the v2 client) and B (an old-style client) READY and SYNCED", a.rec_synced and b.rec_synced)
    L.pump_sleep(1.2)
    for nm, c in (("A", a), ("B", b)):
        latest = c.ts_latest(SVC_WORK)
        ck("V0 %s got town service 11 WORK_CAPS: 8 bytes, version 1, bit 0 = Work v2 ON (%s)" % (nm, latest and latest[1].hex()),
           latest is not None and len(latest[1]) == 8 and latest[1][0] == 1 and latest[1][1] == 1 and not any(latest[1][2:]))
    s0 = WM.states(b)
    ck("V0 the old-style client's connect-time WORK_STATE has no bit beyond bit 0", len(s0) >= 1 and all((s["flags"] & ~1) == 0 for s in s0))
    slot = WM.free_slot(a)

    # ---- V1: v2 ENTER -> job 1 = DELIVER (v2)
    off = len(run.log())
    mark, sent, r = op(run, a, ENTER)
    run.tx_ok("V1 v2 ENTER", r, APPLIED, R["NONE"])
    st = WM.last_state(a, mark)
    ck("V1 a v2 DELIVER job: WORK_STATE carries the JOB_V2 bit (the peer is latched), a villager, a parcel, no tip decided yet",
       st is not None and st["type"] == WM.T_DELIVER and (st["flags"] & SF_JOB_V2) and st["villager"] != 0 and st["carried"] != 0 and st["tip_kind"] == 0)
    if st is None or st["type"] != WM.T_DELIVER:
        return
    job1, vill1, parcel1, reward1 = st["job_id"], st["villager"], st["carried"], st["reward"]
    m = re.search(r"\[NET\]\[WORK\] host: v2 job item 0x%04X = \"([^\"]*)\" \(article (\d)\)" % parcel1, run.log(off))
    ck("V1 the item comes from the varied pool, named by the game's own table (0x%04X = %r)" % (parcel1, m and m.group(1)), m is not None and m.group(1).strip() != "" and item_pool_text())
    ck("V1 the host logged that the peer uses Work v2", run.n_log(r"peer \d+ uses Work v2", off) >= 1)

    # ---- V2: URGE
    mark, sent, r = op(run, a, URGE, 0, 0, ((job1 + 1) & 0xFFFF) or 7)
    ck("V2 URGE naming another job id is refused WORK_STALE_JOB", rej(r, REASON_STALE_JOB))
    pre_u = a.txn_pre_image()
    mark, sent, r = op(run, a, URGE, 0, 0, job1 & 0xFFFF)
    run.tx_ok("V2 URGE (Check job -> No.)", r, APPLIED, R["NONE"])
    st = WM.last_state(a, mark)
    ck("V2 the job is urgent (AC_TEST_WORK_URGENT=1): the reward grew by the job's bonus (%s) ONCE, bit URGED set, nothing moved (wallet / pockets)" % bonus_model(job1),
       st is not None and st["reward"] == reward1 + bonus_model(job1) and (st["flags"] & SF_URGED) and r is not None and r.post_wallet == pre_u[2] and tuple(r.post_pockets) == tuple(pre_u[0]))
    reward1u = st["reward"] if st else reward1
    mark, sent, r = op(run, a, URGE, 0, 0, job1 & 0xFFFF)
    st = WM.last_state(a, mark)
    ck("V2 a second URGE adds nothing (still %d, still urged)" % reward1u, r is not None and r.outcome == APPLIED and st is not None and st["reward"] == reward1u and (st["flags"] & SF_URGED))
    ck("V2 host log: one URGENT line with the bonus, then 'already urgent'", run.n_log(r"job %d is URGENT: bonus %d added once" % (job1, bonus_model(job1)), off) == 1
       and run.n_log(r"job %d asked about: already urgent" % job1, off) == 1)

    # ---- V3: TAKE + COMPLETE
    mark, sent, r = op(run, a, TAKE, slot, parcel1, job1 & 0xFFFF, pre=WM.image(a))
    run.tx_ok("V3 TAKE_PARCEL", r, APPLIED, R["NONE"])
    mark, sent, r = op(run, a, COMPLETE, slot, parcel1, (vill1 + 1) & 0xFFFF or 5, pre=WM.image(a))
    ck("V3 COMPLETE at the WRONG villager is refused PRECOND", rej(r, REASON_PRECOND))
    mark, sent, r = op(run, a, COMPLETE, slot, parcel1, vill1, pre=WM.image_c(a, slot, parcel1, NORMAL_COND))
    ck("V3 COMPLETE with an ORDINARY copy of the parcel is refused PRECOND", rej(r, REASON_PRECOND))
    other = parcel1 + 1
    mark, sent, r = op(run, a, COMPLETE, slot, other, vill1, pre=WM.image_c(a, slot, other, QUEST_COND))
    ck("V3 COMPLETE with another (quest) item is refused PRECOND", rej(r, REASON_PRECOND))
    off = len(run.log())
    pre = a.txn_pre_image()
    mark, sent3, r3 = op(run, a, COMPLETE, slot, parcel1, vill1, pre=WM.image(a))
    run.tx_ok("V3 COMPLETE at the right villager", r3, APPLIED, R["NONE"])
    mm = re.search(r"job %d completed at villager 0x%04X: reward (\d+) \+ tip (\d+) \(friendship (-?\d+)\) = (\d+) paid" % (job1, vill1), run.log(off))
    tip = int(mm.group(2)) if mm else -1
    fr = int(mm.group(3)) if mm else 0
    ck("V3 the tip is pc_work_v2_tip(job, friendship %d) = %d (host said %d), 50..550" % (fr, tip_model(job1, fr), tip), mm is not None and tip == tip_model(job1, fr) and 50 <= tip <= 550)
    st3 = WM.last_state(a, mark)
    ck("V3 ONE step: the parcel slot is empty, the wallet grew by exactly reward %d + tip %d" % (reward1u, tip),
       r3 is not None and r3.post_pockets[slot] == 0 and r3.post_wallet == pre[2] + reward1u + tip and int(mm.group(1) if mm else -1) == reward1u)
    ck("V3 the job is retired at the villager: state NONE, AUTO bit, tip kind Bells + the tip in the state, jobs_done %s" % (st3 and st3["done"]),
       st3 is not None and st3["state"] == 0 and (st3["flags"] & SF_AUTO) and st3["tip_kind"] == 1 and st3["tip_value"] == tip and st3["last"] == job1 and st3["done"] == 1)
    ck("V3 exactly ONE commit line", run.n_log(r"WORK op 10 committed: job_id=%d " % job1, off) == 1)
    a.resend_txn(sent3)
    r3b = a.wait_txn_result(sent3.seq, 4.0, nth=2)
    ck("V3 the retransmit is answered from the journal (REPLAYED, the same post wallet), nothing re-paid",
       r3b is not None and r3b.outcome == APPLIED and r3b.reason == R["REPLAYED"] and r3b.post_wallet == r3.post_wallet and run.n_log(r"WORK op 10 committed: job_id=%d " % job1, off) == 1)
    mark, sent, r = op(run, a, COMPLETE, slot, parcel1, vill1, pre=WM.image_c(a, slot, parcel1, QUEST_COND))
    ck("V3 a new-seq COMPLETE of the paid job is refused WORK_NO_JOB", rej(r, REASON_NO_JOB))
    mark, sent, r = op(run, a, REPORT, 0, 0, job1 & 0xFFFF)
    ck("V3 a REPORT to Nook afterwards is refused WORK_NO_JOB (nothing paid twice)", rej(r, REASON_NO_JOB))

    # ---- V4: job 2 = FETCH (v2): GIVE then QUIT
    mark, sent, r = op(run, a, ENTER)
    st = WM.last_state(a, mark)
    ck("V4 a v2 FETCH job", st is not None and st["type"] == WM.T_FETCH and (st["flags"] & SF_JOB_V2) and (st["flags"] & SF_AUTO) == 0 and (st["flags"] & SF_URGED) == 0)
    if st is None or st["type"] != WM.T_FETCH:
        return
    job2, vill2, fitem = st["job_id"], st["villager"], st["item"]
    mark, sent, r = op(run, a, GIVE, slot, fitem, vill2, pre=WM.image(a))
    run.tx_ok("V4 the villager hands the fetched item over", r, APPLIED, R["NONE"])
    ck("V4 the fetched item (0x%04X) is a QUEST item in the slot" % fitem, r is not None and r.post_pockets[slot] == fitem and WM.cond_at(r.post_conds, slot) == QUEST_COND)
    mark, sent, r = op(run, a, QUIT, slot, fitem, job2 & 0xFFFF, pre=WM.image_c(a, slot, fitem, NORMAL_COND))
    ck("V4 QUIT returning an ORDINARY copy is refused PRECOND", rej(r, REASON_PRECOND))
    mark, sent, r = op(run, a, QUIT, slot, fitem, ((job2 + 1) & 0xFFFF) or 9, pre=WM.image(a))
    ck("V4 QUIT naming another job is refused WORK_STALE_JOB", rej(r, REASON_STALE_JOB))
    pre = a.txn_pre_image()
    mark, sent, r = op(run, a, QUIT, slot, fitem, job2 & 0xFFFF, pre=WM.image(a))
    run.tx_ok("V4 QUIT with the item", r, APPLIED, R["NONE"])
    st = WM.last_state(a, mark)
    ck("V4 the item went back (slot empty, condition cleared), nothing paid, the job and Work Mode ended, jobs_done unchanged",
       r is not None and r.post_pockets[slot] == 0 and WM.cond_at(r.post_conds, slot) == NORMAL_COND and r.post_wallet == pre[2] and st is not None and st["state"] == 0
       and st["mode"] == 0 and st["done"] == 1)
    mark, sent, r = op(run, a, DELIVER, slot, fitem, job2 & 0xFFFF, pre=WM.image_c(a, slot, fitem, QUEST_COND))
    ck("V4 the quit job can never be paid (WORK_NO_JOB)", rej(r, REASON_NO_JOB))

    # ---- V5: job 3 = DELIVER, COMPLETE with a wallet near the cap
    mark, sent, r = op(run, a, ENTER)
    st = WM.last_state(a, mark)
    if st is None or st["type"] != WM.T_DELIVER:
        ck("V5 a v2 DELIVER job (got %s)" % st, False)
        return
    job3, vill3, parcel3, reward3 = st["job_id"], st["villager"], st["carried"], st["reward"]
    mark, sent, r = op(run, a, TAKE, slot, parcel3, job3 & 0xFFFF, pre=WM.image(a))
    run.tx_ok("V5 TAKE_PARCEL", r, APPLIED, R["NONE"])
    pk, conds, _w = a.txn_pre_image()
    rich = 99950
    off = len(run.log())
    mark, sent, r = op(run, a, COMPLETE, slot, parcel3, vill3, pre=(pk, conds, rich))
    mm = re.search(r"job %d completed at villager 0x%04X: reward (\d+) \+ tip (\d+) \(friendship (-?\d+)\)" % (job3, vill3), run.log(off))
    tip3 = int(mm.group(2)) if mm else -1
    ck("V5 near the cap: wallet + one 30000 bag in the vacated slot == pre-wallet + reward %d + tip %d exactly (post wallet %s)" % (reward3, tip3, r and r.post_wallet),
       r is not None and r.outcome == APPLIED and r.post_pockets[slot] == 0x2102 and r.post_wallet + 30000 == rich + reward3 + tip3 and r.post_wallet <= 99999)

    # ---- V6: job 4 = FRUIT (v2) -> Nook: no tip
    mark, sent, r = op(run, a, ENTER)
    st = WM.last_state(a, mark)
    if st is None or st["type"] != WM.T_FRUIT:
        ck("V6 a v2 FRUIT job (got %s)" % st, False)
        return
    job4, fruit, reward4 = st["job_id"], st["item"], st["reward"]
    s2 = next(i for i in range(15) if a.txn_pre_image()[0][i] == 0)
    pk, conds, w = a.txn_pre_image()
    mark, sent, r = op(run, a, DELIVER, s2, fruit, job4 & 0xFFFF, pre=WM.image_c(a, s2, fruit, NORMAL_COND))
    ck("V6 a FRUIT job handed to Nook pays exactly its reward %d and NO tip" % reward4, r is not None and r.outcome == APPLIED and r.post_wallet == w + reward4 and r.post_pockets[s2] == 0)

    # ---- V7: the old-style client B
    mark, sent, r = op(run, b, ENTER, flags=0)
    st = WM.last_state(b, mark)
    ck("V7 an old-style ENTER (no flag) gives a v1 job: stationery parcel, the old tip rule, NO v2 bit in its WORK_STATE",
       st is not None and st["type"] == WM.T_DELIVER and st["carried"] in papers and st["tip_kind"] in (0, 1, 2) and (st["flags"] & ~1) == 0)
    mark, sent, r = op(run, b, URGE, 0, 0, (st["job_id"] & 0xFFFF) if st else 1, flags=0)
    st2 = WM.last_state(b, mark)
    ck("V7 (an old client never sends op 8; if a v2 op arrives unflagged it is served, but the state still carries no v2 bit)", r is not None and st2 is not None and (st2["flags"] & ~1) == 0)
    # job 6 for A: a v2 DELIVER carried into the host restart
    mark, sent, r = op(run, a, ENTER)
    st = WM.last_state(a, mark)
    if st is None or st["type"] != WM.T_DELIVER:
        ck("V8 setup: a v2 DELIVER job for the restart (got %s)" % st, False)
        return
    job6, vill6, parcel6 = st["job_id"], st["villager"], st["carried"]
    mark, sent, r = op(run, a, URGE, 0, 0, job6 & 0xFFFF)
    st = WM.last_state(a, mark)
    reward6 = st["reward"] if st else 0
    slot6 = WM.free_slot(a)  # (V5 left its money bag in `slot`)
    mark, sent, r = op(run, a, TAKE, slot6, parcel6, job6 & 0xFFFF, pre=WM.image(a))
    run.tx_ok("V8 setup: TAKE_PARCEL of the urged v2 job", r, APPLIED, R["NONE"])
    run.release(a)
    a2 = run.ready("A2", run.r1)
    L.pump_sleep(1.0)
    s7 = WM.states(a2)
    ck("V7 a reconnect of the v2 client starts UNLATCHED: its connect-time state has no v2 bit (an old program on the same character would accept it)",
       len(s7) >= 1 and s7[0]["job_id"] == job6 and (s7[0]["flags"] & ~1) == 0)

    # ---- V8: host restart with the switch OFF
    for cl in list(run.clients):
        run.release(cl)
    ck("V8 the first host did not crash before the restart", run.host.stop() is None)
    host2 = L.HostProcess(port=run.port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT)], log_path=os.path.join(HERE, "work_v2_proto_host2.log"),
                          env=dict(os.environ, AC_MP_WORK_V2="0", AC_TEST_WORK_URGENT="")).start()
    ok = host2.wait_listening(60.0) and host2.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT)
    run.host = host2
    ck("V8 the host restarted with AC_MP_WORK_V2=0", ok)
    if not ok:
        return
    L.resolve_host_town(run.ip, run.port)
    a3 = run.ready("A3", run.r1)
    L.pump_sleep(1.2)
    latest = a3.ts_latest(SVC_WORK)
    ck("V8 service 11 now says Work v2 OFF", latest is not None and latest[1][1] == 0)
    s9 = WM.states(a3)
    ck("V8 the persisted v2 job survived (id, the urged reward %d, carrying)" % reward6, len(s9) >= 1 and s9[0]["job_id"] == job6 and s9[0]["reward"] == reward6 and s9[0]["step"] == 1)
    mark, sent, r = op(run, a3, URGE, 0, 0, job6 & 0xFFFF, flags=0)
    ck("V8 op 8 is BAD_SHAPE with the switch off (exactly the old host's answer)", rej(r, BAD_SHAPE))
    c3 = run.ready("C", run.r3)
    mark, sent, r = op(run, c3, ENTER, flags=FLAG_V2)
    ck("V8 the v2 flag is BAD_SHAPE with the switch off", rej(r, BAD_SHAPE))
    off = len(run.log())
    pre = a3.txn_pre_image()
    mark, sent, r = op(run, a3, RECEIVE, slot6, parcel6, vill6, pre=WM.image(a3), flags=0)
    run.tx_ok("V8 the old RECEIVE of the v2 parcel", r, APPLIED, R["NONE"])
    st = WM.last_state(a3, mark)
    tip6 = (r.post_wallet - pre[2]) if r is not None else -1
    ck("V8 RECEIVE of a v2 job pays the v2 friendship tip (%d Bells, 50..550) into the wallet" % tip6, r is not None and 50 <= tip6 <= 550 and tip6 % 10 == 0 and st is not None and st["step"] == 2)
    pre = a3.txn_pre_image()
    mark, sent, r = op(run, a3, REPORT, 0, 0, job6 & 0xFFFF, flags=0)
    ck("V8 REPORT then pays the urged reward %d (the job never gets stuck)" % reward6, r is not None and r.outcome == APPLIED and r.post_wallet == pre[2] + reward6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11180)
    args = ap.parse_args()
    T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
    os.environ["AC_TEST_WORK_URGENT"] = "1"
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    ck("the fixture runs the build64_w3 exe", os.path.getsize(os.path.join(L.GAME_BIN_DIR, "AnimalCrossing.exe")) == os.path.getsize(W3_EXE))
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    work_file = os.path.join(L.GAME_BIN_DIR, "save", "mp", "work_jobs.dat")
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "work_v2_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    if os.path.isfile(work_file):
        os.remove(work_file)

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)
        if os.path.isfile(work_file):
            os.remove(work_file)

    try:
        WM.run_phase_two_hosts(results, ip, args.port, "WORKV2", phase, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
