#!/usr/bin/env python3
"""test_work_reward_overflow_protocol.py - Work Mode DELIVER / REPORT reward overflow (30000 money bag) against a REAL host process (protocol FakeClient peers).

The pre-image of a TXN_COMMIT carries the claimed wallet / pockets, so a FakeClient can put the wallet next to the cap (the shop SE5 scenario uses the same trick).
Host: AC_TEST_HOOKS=1, AC_TEST_WORK_TYPE=1,1,3 (job 1 fruit, job 2 fruit, job 3 deliver-to-villager, ...).
  O1 DELIVER with wallet 99900: wallet + bag conserved (old + reward == new + 30000), the bag sits in the slot the item vacated; other pockets untouched
  O3 the identical commit retransmitted -> REPLAYED from the journal, one host commit line, never paid twice; a new-seq completion is NO_JOB
  O4 DELIVER with a normal wallet: wallet + reward, no bag
  O2 REPORT with wallet 99900 and every pocket full -> WORK_WALLET_FULL (35), job unchanged (still ACTIVE, step delivered, jobs_done unchanged, nothing logged as committed);
     the same REPORT with one free pocket -> paid, bag in the FIRST free pocket, conservation; retransmit -> REPLAYED, one commit line
Run on a disposable fixture: NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_rv_a> --port 13100
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ["AC_TEST_WORK_TYPE"] = "1,1,3"
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_work_mode_protocol as W  # noqa: E402
from test_txn_protocol import APPLIED, REJECTED, R  # noqa: E402
os.environ["AC_TEST_WORK_TYPE"] = "1,1,3"  # after the imports: test_work_mode_protocol sets its own "1,2,3" at import time

ITM_MONEY_30000 = None


def money30000():
    return 0x2102  # ITM_MONEY_30000 (include/m_name_table.h: ITM_MONEY_10000 + 1)


def phase(run):
    ck = run.check
    bag = money30000()
    fs = W.food_start()
    a = run.ready("A", run.r1)
    ck("setup: A READY and SYNCED", a.rec_synced)
    L.pump_sleep(0.8)
    slot = W.free_slot(a)

    # ---- job 1 (fruit): overflow DELIVER
    mark, sent, r = W.op(run, a, W.ENTER)
    run.tx_ok("ENTER job 1", r, APPLIED, R["NONE"])
    st = W.last_state(a, mark)
    ck("job 1 is a FRUIT job with a 300..700 reward", st is not None and st["type"] == W.T_FRUIT and 300 <= st["reward"] <= 700)
    job1, item1, rew1 = st["job_id"], st["item"], st["reward"]
    off = len(run.log())
    pk0, conds0, _w = a.txn_pre_image()
    wallet0 = 99900
    pre_pk = tuple(item1 if i == slot else p for i, p in enumerate(pk0))
    mark, sent1, r1 = W.op(run, a, W.DELIVER, slot, item1, job1 & 0xFFFF, pre=(pre_pk, conds0, wallet0))
    run.tx_ok("O1 DELIVER with wallet %d + reward %d" % (wallet0, rew1), r1, APPLIED, R["NONE"])
    ck("O1 CONSERVED: old wallet + reward == new wallet + 30000 (%d + %d == %s + 30000)" % (wallet0, rew1, r1.post_wallet if r1 else None),
       r1 is not None and wallet0 + rew1 == r1.post_wallet + 30000 and r1.post_wallet <= 99999)
    ck("O1 the 30000 bag sits in the vacated slot %d, every other pocket unchanged" % slot,
       r1 is not None and r1.post_pockets[slot] == bag and all(r1.post_pockets[i] == pre_pk[i] for i in range(15) if i != slot))
    s1 = W.last_state(a, mark)
    ck("O1 job retired + tombstoned, jobs_done 1", s1 is not None and s1["state"] == 0 and s1["last"] == job1 and s1["done"] == 1)
    ck("O1 ONE host commit line", run.n_log(r"WORK op 2 committed: job_id=%d " % job1, off) == 1)
    # O3 replay
    a.resend_txn(sent1)
    r1b = a.wait_txn_result(sent1.seq, 4.0, nth=2)
    ck("O3 identical TXN_COMMIT replayed: APPLIED/REPLAYED, same post wallet and bag", r1b is not None and r1b.outcome == APPLIED and r1b.reason == R["REPLAYED"]
       and r1b.post_wallet == r1.post_wallet and r1b.post_pockets == r1.post_pockets)
    L.pump_sleep(0.4)
    ck("O3 still ONE commit line (not paid twice)", run.n_log(r"WORK op 2 committed: job_id=%d " % job1, off) == 1)
    mark, _s, rn = W.op(run, a, W.DELIVER, slot, item1, job1 & 0xFFFF, pre=(pre_pk, conds0, wallet0))
    ck("O3 a NEW-seq completion of the paid job is NO_JOB (33)", rn is not None and rn.outcome == REJECTED and rn.reason == W.REASON_NO_JOB)

    # ---- job 2 (fruit): normal wallet
    mark, sent, r = W.op(run, a, W.ENTER)
    st2 = W.last_state(a, mark)
    ck("job 2 is a FRUIT job", st2 is not None and st2["type"] == W.T_FRUIT and st2["job_id"] > job1)
    job2, item2, rew2 = st2["job_id"], st2["item"], st2["reward"]
    pre_pk2 = tuple(item2 if i == slot else p for i, p in enumerate(pk0))
    mark, _s, r2 = W.op(run, a, W.DELIVER, slot, item2, job2 & 0xFFFF, pre=(pre_pk2, conds0, 1000))
    run.tx_ok("O4 DELIVER with a normal wallet (1000)", r2, APPLIED, R["NONE"])
    ck("O4 wallet == 1000 + reward %d, slot emptied, no bag anywhere" % rew2, r2 is not None and r2.post_wallet == 1000 + rew2 and r2.post_pockets[slot] == 0
       and bag not in r2.post_pockets)

    # ---- job 3 (deliver to villager): REPORT overflow
    mark, sent, r = W.op(run, a, W.ENTER)
    st3 = W.last_state(a, mark)
    ck("job 3 is a DELIVER_VILLAGER job", st3 is not None and st3["type"] == W.T_DELIVER)
    if st3 is None or st3["type"] != W.T_DELIVER:
        return
    job3, villager3, parcel, rew3 = st3["job_id"], st3["villager"], st3["carried"], st3["reward"]
    mark, _s, r = W.op(run, a, W.TAKE, slot, parcel, job3 & 0xFFFF, pre=W.image(a))
    run.tx_ok("TAKE_PARCEL", r, APPLIED, R["NONE"])
    mark, _s, r = W.op(run, a, W.RECEIVE, slot, parcel, villager3, pre=W.image_c(a, slot, parcel, W.QUEST_COND))
    run.tx_ok("RECEIVE (parcel to the villager)", r, APPLIED, R["NONE"])
    ck("step delivered (2)", (W.last_state(a, mark) or {}).get("step") == 2)
    off = len(run.log())
    full = tuple(fs for _ in range(15))
    rwallet = 99900
    mark, sent, rf = W.op(run, a, W.REPORT, 0, 0, job3 & 0xFFFF, pre=(full, 0, rwallet))
    ck("O2 REPORT with wallet %d and ALL pockets full -> REJECTED WORK_WALLET_FULL (35)" % rwallet, rf is not None and rf.outcome == REJECTED and rf.reason == W.REASON_WALLET_FULL)
    sf = W.last_state(a, mark)
    ck("O2 job unchanged: still ACTIVE, step 2, same job id, jobs_done 2, last_rewarded not this job",
       sf is not None and sf["state"] == 1 and sf["job_id"] == job3 and sf["step"] == 2 and sf["done"] == 2 and sf["last"] != job3)
    ck("O2 no wallet / pocket change was granted (REJECTED carries no applied post image) and NO commit line", run.n_log(r"WORK op 7 committed: job_id=%d " % job3, off) == 0)
    # same job, now with free pockets 3 and 7
    free_pk = tuple(0 if i in (3, 7) else fs for i in range(15))
    mark, sent7, rp = W.op(run, a, W.REPORT, 0, 0, job3 & 0xFFFF, pre=(free_pk, 0, rwallet))
    run.tx_ok("O2b the same REPORT with free pockets 3 and 7", rp, APPLIED, R["NONE"])
    ck("O2b CONSERVED: %d + %d == %s + 30000" % (rwallet, rew3, rp.post_wallet if rp else None), rp is not None and rwallet + rew3 == rp.post_wallet + 30000 and rp.post_wallet <= 99999)
    ck("O2b the bag is in the FIRST free pocket (3); pocket 7 stays free; the rest unchanged",
       rp is not None and rp.post_pockets[3] == bag and rp.post_pockets[7] == 0 and all(rp.post_pockets[i] == fs for i in range(15) if i not in (3, 7)))
    ck("O2b job retired (state 0, last == job)", (W.last_state(a, mark) or {}).get("last") == job3)
    ck("O2b ONE commit line", run.n_log(r"WORK op 7 committed: job_id=%d " % job3, off) == 1)
    a.resend_txn(sent7)
    rpb = a.wait_txn_result(sent7.seq, 4.0, nth=2)
    ck("O3b the REPORT retransmit is REPLAYED with the same post wallet / pockets", rpb is not None and rpb.outcome == APPLIED and rpb.reason == R["REPLAYED"]
       and rpb.post_wallet == rp.post_wallet and rpb.post_pockets == rp.post_pockets)
    L.pump_sleep(0.4)
    ck("O3b still ONE commit line (never paid twice)", run.n_log(r"WORK op 7 committed: job_id=%d " % job3, off) == 1)
    mark, _s, rn = W.op(run, a, W.REPORT, 0, 0, job3 & 0xFFFF, pre=(free_pk, 0, rwallet))
    ck("O3b a NEW-seq REPORT of the paid job is NO_JOB (33)", rn is not None and rn.outcome == REJECTED and rn.reason == W.REASON_NO_JOB)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=13100)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_<name> copy only", file=sys.stderr)
        return 2
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    work_file = os.path.join(L.GAME_BIN_DIR, "save", "mp", "work_jobs.dat")
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "work_reward_snapshot_%d" % os.getpid())
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
        W.run_phase_two_hosts(results, "127.0.0.1", args.port, "WORKREWARD", phase, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
