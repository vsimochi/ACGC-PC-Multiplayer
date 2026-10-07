#!/usr/bin/env python3
"""test_work_mode_protocol.py - Nook Work Mode: TXN_COMMIT kind 16 WORK (ops 1..7) + WORK_STATE (id 68, 36 B) against a REAL host process.

TIER: PROTOCOL TESTED (real host binary, `--host --bootstrap-resident`, scripted resident / guest FakeClients -- the harness of test_shop_catalog_protocol.py). The Nook dialogue seam
(ac_npc_shop_common.c), the villager talk (ac_quest_manager.c) and the client apply are covered by the build, the menu-patch harness (test_work_menu_patch.py) and the source audit
(section S) -- NOT by a GUI run; no visual verification is claimed.
Run on a DISPOSABLE copy: NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>> (the fixture save is snapshotted and restored; the host's save/mp/work_jobs.dat is removed).
The host gets AC_TEST_HOOKS=1 and AC_TEST_WORK_TYPE=1,2,3 (the job types of the first three created jobs: fruit, fetch from a villager, deliver to a villager; then 1,2,3 again).
  W0 connect: the job state reaches a READY + SYNCED peer with NO op (request 0): the menu knows 'no job' before anything was asked
  W1 job 1 = FRUIT: the objective is the TOWN's native fruit (Save_Get(fruit) of the host save, logged), a Nook reward; ENTER again resumes the SAME job
  W2 refusals (nothing consumed / paid): a character without a job, a wrong job id, a wrong item, ops of another job type
  W3 DELIVER of the fruit: the HOST pays its reward, the slot is emptied, the job is retired, ONE commit line; the retransmit is answered from the journal (REPLAYED); a new-seq
     completion of the paid job is refused
  W4 job 2 = FETCH_VILLAGER: Nook names villager + item; Nook cannot be paid before the item is fetched; the WRONG villager hands nothing; the right villager hands the item ONCE
     (a second request is refused), then DELIVER to Nook pays the Nook reward
  W5 job 3 = DELIVER_VILLAGER: TAKE_PARCEL grants the parcel once; REPORT before delivery is refused; the wrong villager takes nothing; the right villager takes the parcel + pays the optional
     tip (Bells / an item / none) ONCE; REPORT pays the Nook reward (the tip never replaces it)
  W4q/W5q QUEST delivery items: the fetched item / the parcel are granted with the vanilla QUEST pocket condition (post image), a NORMAL copy of the same item NEVER completes the hand-in
     (refused PRECOND, nothing consumed, nothing paid), the QUEST copy does -- also with a NORMAL decoy copy in another slot. FRUIT jobs are unchanged (an ordinary fruit)
  W6 LEAVE quits the job (state NONE, never paid, menu = 'I'd like to work' again); a later ENTER creates a NEW job
  W7 disconnect + reconnect as the SAME character: the job state is pushed again at connect (same job id / step), unchanged
  W8 a GUEST character works independently
  W9 host restart: the persisted job (type, step, villager) survives, a paid job can never be paid again, new job ids never repeat
  S  source audit
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
os.environ["AC_TEST_WORK_TYPE"] = "1,2,3"
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_ts_protocol as TS  # noqa: E402
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE  # noqa: E402

K_WORK = 16
MSG_WORK_STATE = 68
WS_FMT = "<BBBBIIIIHHHBBII"
WS_SIZE = struct.calcsize(WS_FMT)
assert WS_SIZE == 36
ENTER, DELIVER, LEAVE, TAKE, GIVE, RECEIVE, REPORT = 1, 2, 3, 4, 5, 6, 7
REASON_PRECOND, REASON_NO_JOB, REASON_STALE_JOB, REASON_WALLET_FULL = 9, 33, 34, 35
T_FRUIT, T_FETCH, T_DELIVER = 1, 2, 3
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def decode_state(payload):
    (t, flags, state, jtype, job_id, reward, done, last, item, carried, villager, ostate, tkind, tvalue, rid) = struct.unpack(WS_FMT, payload)
    return dict(flags=flags, mode=flags & 1, state=state, type=jtype, job_id=job_id, reward=reward, done=done, last=last, item=item, carried=carried, villager=villager,
                step=ostate, tip_kind=tkind, tip_value=tvalue, request_id=rid)


def is_state(m):
    return m.channel == L.CH_RELIABLE and len(m.payload) == WS_SIZE and m.payload[0] == MSG_WORK_STATE


def states(c, since=None):
    return [decode_state(m.payload) for m in c.inbox.peek_all(is_state, since=since)]


def last_state(c, since=None):
    s = states(c, since)
    return s[-1] if s else None


def food_start():
    src = open(os.path.join(ROOT, "include", "m_name_table.h"), encoding="utf-8", errors="replace").read()
    m = re.search(r"#define ITM_FOOD_START\s+0x([0-9A-Fa-f]+)", src)
    return int(m.group(1), 16) if m else 0x2800


def paper_start():
    src = open(os.path.join(ROOT, "include", "m_name_table.h"), encoding="utf-8", errors="replace").read()
    m = re.search(r"#define ITM_PAPER_START\s+0x([0-9A-Fa-f]+)", src)
    return int(m.group(1), 16) if m else 0x2000


def op(run, c, code, slot=0, item=0, aux=0, pre=None, wait=4.0):
    mark = c.inbox.mark()
    rid = run.fresh_rid()
    sent = c.send_txn_commit(K_WORK, rid, D_NONE, slot, item, pre=pre, aux_cond=code, aux_item=aux)
    return mark, sent, c.wait_txn_result(sent.seq, wait)


def image(c, slot=None, item=None):
    pockets, conds, wallet = c.txn_pre_image()
    pk = tuple(item if (slot is not None and i == slot) else p for i, p in enumerate(pockets))
    return (pk, conds, wallet)


QUEST_COND, NORMAL_COND = 2, 0


def cond_at(conds, slot):
    return (conds >> (2 * slot)) & 3


def image_c(c, slot, item, cond, decoy_slot=None):
    """the local pre-image with `item` at `slot` carrying pocket condition `cond`; optionally an ordinary (NORMAL) copy of the same item at `decoy_slot`"""
    pockets, conds, wallet = c.txn_pre_image()
    pk = list(pockets)
    pk[slot] = item
    conds = (conds & ~(3 << (2 * slot))) | (cond << (2 * slot))
    if decoy_slot is not None:
        pk[decoy_slot] = item
        conds = conds & ~(3 << (2 * decoy_slot))
    return (tuple(pk), conds, wallet)


def free_slot(c):
    return TP.first_free_pocket(c)


def audit_source(check):
    net = open(os.path.join(ROOT, "pc", "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read()
    shop = open(os.path.join(ROOT, "src", "actor", "npc", "ac_npc_shop_common.c"), encoding="utf-8", errors="replace").read()
    nook = open(os.path.join(ROOT, "pc", "src", "pc_nook_house.c"), encoding="utf-8", errors="replace").read()
    qm = open(os.path.join(ROOT, "src", "actor", "ac_quest_manager.c"), encoding="utf-8", errors="replace").read()
    tw = open(os.path.join(ROOT, "src", "actor", "ac_quest_talk_work.c_inc"), encoding="utf-8", errors="replace").read()
    msgc = open(os.path.join(ROOT, "src", "game", "m_msg_main.c_inc"), encoding="utf-8", errors="replace").read()
    check("S1 the job table is keyed by the bound character PID (never the peer / connection) and persisted before the result is sent",
          "pcnetgame_work_find(&st->bound_pid, 1)" in net and "pcnetgame_work_save(); /* durable BEFORE the result" in net)
    check("S2 the host decides: ONE pure plan validates job id + state-machine step + item + villager + wallet cap against the HOST record; the reward is the record's",
          "pcnetgame_work_plan(work, t, post, &post_conds, &post_wallet, &work_reward, &fail)" in net and "add = w->reward;" in net and "static uint8_t pcnetgame_work_plan(" in net)
    check("S3 a delivery retires + tombstones the job in the SAME step the reward is decided; a duplicate is answered from the journal",
          "w->last_rewarded = w->job_id;" in net and "answered from the journal, nothing re-executed" in net)
    check("S4 a guest buying a house keeps its job: the record is re-keyed to the new resident PID in the shared promotion core",
          "pcnetgame_work_rekey(&old_g.key, &Save_Get(private_data)[s].player_ID)" in net)
    check("S5 the FIRST page of Nook's talk gets the work / job choice when the vanilla message is loaded (patched from the authoritative state), not under Other things",
          "pc_nook_msg_patch(index, msg_data->text_buf.data" in msgc and "pc_nook_first_page_work() && mChoice_Get_ChoseNum(mChoice_Get_base_window_p()) == mChoice_CHOICE3" in shop
          and "PC_NOOK_SEL_WORK_LEAVE" in nook and "NK_SEL5" in nook and "PC_NOOK_SEL_JOB" in nook)
    check("S6 the job ids are host-generated, never reused and never 0 mod 65536 (the wire carries the low 16 bits)", "} while ((s_work_next_job_id & 0xFFFFu) == 0u);" in net)
    check("S7 the native fruit comes from the HOST save (Save_Get(fruit)), the job types are a registry (fruit / fetch / deliver) and the villager errands need a villager",
          "pcnetgame_work_town_fruit" in net and "mActor_name_t f = Save_Get(fruit);" in net and "PC_WORK_JOB_FETCH_VILLAGER" in net and "PC_WORK_JOB_DELIVER_VILLAGER" in net)
    check("S8 the villager side runs in the villager's own talk (quest manager talk kind WORK): the hello plays, the host transaction is sent meanwhile, the generated row ends the talk",
          "aQMgr_TALK_KIND_WORK" in qm and "pc_net_game_work_villager_pending" in qm and "pc_net_game_work_begin_op(l_work_op, l_work_npc)" in tw)
    check("S9 the connect-time push: the job state is sent once the record is SYNCED (connect / reconnect), with request 0",
          "wst->work_sent = 1;" in net and "pcnetgame_work_send_state((PCNetPeerId)p, &wst->bound_pid, 0);" in net)
    tag = open(os.path.join(ROOT, "src", "game", "m_tag_ovl.c"), encoding="utf-8", errors="replace").read()
    mq = open(os.path.join(ROOT, "src", "game", "m_quest.c"), encoding="utf-8", errors="replace").read()
    check("S10 the parcel and the fetched item are granted as vanilla QUEST items (host plan + the client delta fallback); nothing else in the plan is marked",
          net.count("mPr_ITEM_COND_QUEST); /* the parcel is a vanilla QUEST item") == 1 and net.count("mPr_ITEM_COND_QUEST); /* the fetched item is a vanilla QUEST item") == 1
          and "np->inventory.item_conditions = mPr_SET_ITEM_COND(np->inventory.item_conditions, s, mPr_ITEM_COND_QUEST);" in net)
    check("S11 the hand-in needs the QUEST copy: the host plan (fetch DELIVER + parcel RECEIVE) and the client lookup (pcnetgame_work_find_pocket(item, cond)) -- a FRUIT job still takes an ordinary fruit",
          "mPr_GET_ITEM_COND(*post_conds, t->slot) != mPr_ITEM_COND_QUEST" in net and "static int pcnetgame_work_find_pocket(uint16_t item, int cond)" in net
          and "pcnetgame_work_find_pocket((uint16_t)v.carried_item, mPr_ITEM_COND_QUEST)" in net and "v.job_type == PC_WORK_JOB_FETCH_VILLAGER ? mPr_ITEM_COND_QUEST : -1" in net)
    check("S12 the label: the vanilla 'Delivery for X from Y' branch comes first and is untouched; 'Delivery Item' only for a QUEST item without recipient entry (Nook's debt money excluded)",
          "static u8 delivery_item_str[13] = \"Delivery Item\";" in tag and tag.index("mQst_GetToFromName(str0, str1, idx) == TRUE") < tag.index("mem_copy(tag->str0, delivery_item_str")
          and "itemCond == mPr_ITEM_COND_QUEST && tag->table == mTG_TABLE_ITEM" in tag and "ITEM1_CAT_MONEY" in tag[tag.index("delivery_item_str, sizeof") - 700:tag.index("delivery_item_str, sizeof")])
    check("S13 selling / use / mail / drop need a NORMAL condition: the vanilla checks are unchanged (m_submenu.c sell filter, Nook refusal, the network sale gate)",
          "mPr_GET_ITEM_COND(priv->inventory.item_conditions, slot_no) == mPr_ITEM_COND_NORMAL" in open(os.path.join(ROOT, "src", "game", "m_submenu.c"), encoding="utf-8", errors="replace").read()
          and "aNSC_CHECK_BUY_REFUSE_QUEST_COND" in shop and "a quest item is never bought" in net)
    check("S14 an abandoned job's delivery item becomes ordinary again, ONLY that item: a QUEST slot of that id that no vanilla quest entry tracks (LEAVE, client APPLIED and host-local)",
          "static void pcnetgame_work_release_leftover(uint16_t item)" in net and "!mQst_PC_SlotHasQuestEntry(i)" in net and net.count("pcnetgame_work_release_leftover(") >= 4
          and "extern int mQst_PC_SlotHasQuestEntry(int idx)" in mq)


def phase_work(run):
    ck = run.check
    fs, ps = food_start(), paper_start()
    fruits = {fs + i for i in range(5)}
    papers = {ps + i for i in range(8)}
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    ck("W0 setup: A and B READY and SYNCED", a.rec_synced and b.rec_synced)
    L.pump_sleep(1.0)
    s0 = states(a)
    ck("W0 the job state reached A at connect with NO op (request 0): no job, Work Mode off -- Nook's first page would offer 'I'd like to work'",
       len(s0) >= 1 and s0[0]["request_id"] == 0 and s0[0]["state"] == 0 and s0[0]["mode"] == 0)
    slot = free_slot(a)

    # ---- W1: job 1 = FRUIT (the town's native fruit)
    off = len(run.log())
    mark, sent, r = op(run, a, ENTER)
    run.tx_ok("W1 ENTER", r, APPLIED, R["NONE"])
    st1 = last_state(a, mark)
    ck("W1 an ACTIVE FRUIT job: Work Mode on, unique non-zero job id, a fruit objective, a Nook reward",
       st1 is not None and st1["mode"] == 1 and st1["state"] == 1 and st1["type"] == T_FRUIT and st1["job_id"] != 0 and (st1["job_id"] & 0xFFFF) != 0 and st1["item"] in fruits and st1["reward"] >= 300)
    if st1 is None:
        return
    m = re.search(r"new job %d type 1: item 0x([0-9A-F]+) .*town fruit in the save: 0x([0-9A-F]+)" % st1["job_id"], run.log(off))
    ck("W1 the objective is the TOWN's native fruit from the host save (the save said 0x%s)" % (m.group(2) if m else "?"), m is not None and int(m.group(1), 16) == int(m.group(2), 16) == st1["item"])
    job1, item1, reward1 = st1["job_id"], st1["item"], st1["reward"]
    mark, sent, r = op(run, a, ENTER)
    st = last_state(a, mark)
    ck("W1 ENTER again resumes the SAME job", r is not None and r.outcome == APPLIED and st is not None and st["job_id"] == job1)

    # ---- W2: refusals
    mark, sent, rb = op(run, b, DELIVER, slot, item1, job1 & 0xFFFF, pre=image(b, slot, item1))
    ck("W2 B never entered: DELIVER refused WORK_NO_JOB (33)", rb is not None and rb.outcome == REJECTED and rb.reason == REASON_NO_JOB)
    mark, sent, r = op(run, a, DELIVER, slot, item1, ((job1 + 1) & 0xFFFF) or 7, pre=image(a, slot, item1))
    ck("W2 a completion naming ANOTHER job id is refused WORK_STALE_JOB (34)", r is not None and r.outcome == REJECTED and r.reason == REASON_STALE_JOB)
    wrong = next(f for f in sorted(fruits) if f != item1)
    mark, sent, r = op(run, a, DELIVER, slot, wrong, job1 & 0xFFFF, pre=image(a, slot, wrong))
    ck("W2 handing in a fruit that is NOT the objective is refused PRECOND (9)", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    mark, sent, r1 = op(run, a, GIVE, slot, ps, 0x1234, pre=image(a))
    mark, sent, r2 = op(run, a, TAKE, slot, ps, job1 & 0xFFFF, pre=image(a))
    mark, sent, r3 = op(run, a, REPORT, 0, 0, job1 & 0xFFFF)
    ck("W2 ops of another job type (villager give / take parcel / report) are refused PRECOND on a fruit job",
       all(x is not None and x.outcome == REJECTED and x.reason == REASON_PRECOND for x in (r1, r2, r3)))

    # ---- W3: DELIVER pays; duplicate; stale
    off = len(run.log())
    wallet0 = a.txn_pre_image()[2]
    mark, sent3, r3 = op(run, a, DELIVER, slot, item1, job1 & 0xFFFF, pre=image(a, slot, item1))
    run.tx_ok("W3 DELIVER of the native fruit", r3, APPLIED, R["NONE"])
    ck("W3 the HOST paid its own Nook reward: post wallet = pre wallet + reward, the slot is emptied", r3 is not None and r3.post_wallet == wallet0 + reward1 and r3.post_pockets[slot] == 0)
    s3 = last_state(a, mark)
    ck("W3 WORK_STATE: job retired (state NONE), tombstone = the job id, jobs_done 1 -- the menu returns to 'I'd like to work'", s3 is not None and s3["state"] == 0 and s3["last"] == job1 and s3["done"] == 1)
    ck("W3 exactly ONE host commit line for the delivery", run.n_log(r"WORK op 2 committed: job_id=%d " % job1, off) == 1)
    a.resend_txn(sent3)
    r3b = a.wait_txn_result(sent3.seq, 4.0, nth=2)
    ck("W3 the retransmit is answered from the journal: APPLIED, REPLAYED, the same post wallet", r3b is not None and r3b.outcome == APPLIED and r3b.reason == R["REPLAYED"] and r3b.post_wallet == r3.post_wallet)
    L.pump_sleep(0.4)
    ck("W3 still ONE commit line (nothing paid twice)", run.n_log(r"WORK op 2 committed: job_id=%d " % job1, off) == 1)
    mark, sent, r = op(run, a, DELIVER, slot, item1, job1 & 0xFFFF, pre=image(a, slot, item1))
    ck("W3 a NEW-seq completion of the PAID job is refused WORK_NO_JOB (33): no second reward", r is not None and r.outcome == REJECTED and r.reason == REASON_NO_JOB)

    # ---- W4: job 2 = FETCH_VILLAGER
    mark, sent, r = op(run, a, ENTER)
    run.tx_ok("W4 ENTER (second job)", r, APPLIED, R["NONE"])
    st4 = last_state(a, mark)
    ck("W4 a FETCH job: a villager (npc id), a stationery item, a Nook reward, step 0 (the item is still with the villager)",
       st4 is not None and st4["type"] == T_FETCH and st4["state"] == 1 and st4["villager"] != 0 and st4["item"] in papers and st4["step"] == 0 and st4["job_id"] > job1)
    if st4 is None or st4["type"] != T_FETCH:
        return
    job2, villager2, fitem, reward2 = st4["job_id"], st4["villager"], st4["item"], st4["reward"]
    mark, sent, r = op(run, a, DELIVER, slot, fitem, job2 & 0xFFFF, pre=image(a, slot, fitem))
    ck("W4 Nook cannot be paid before the item was fetched (a claimed item, step 0): PRECOND", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    mark, sent, r = op(run, a, GIVE, slot, fitem, (villager2 + 1) & 0xFFFF or 5, pre=image(a))
    ck("W4 the WRONG villager hands nothing: PRECOND", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    pk_before = a.txn_pre_image()[0]
    mark, sent, r = op(run, a, GIVE, slot, fitem, villager2, pre=image(a))
    run.tx_ok("W4 the RIGHT villager hands over the item", r, APPLIED, R["NONE"])
    ck("W4 the item is in the pocket slot (the host post image) and the step is 1", r is not None and r.post_pockets[slot] == fitem and pk_before[slot] == 0 and (last_state(a, mark) or {}).get("step") == 1)
    ck("W4q the fetched item is granted as a QUEST item (pocket condition %s, post image)" % (cond_at(r.post_conds, slot) if r is not None else None), r is not None and cond_at(r.post_conds, slot) == QUEST_COND)
    mark, sent, r = op(run, a, GIVE, free_slot(a), fitem, villager2, pre=image(a))
    ck("W4 a SECOND request to the villager is refused (no duplicate item): PRECOND", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    wallet0 = a.txn_pre_image()[2]
    mark, sent, r = op(run, a, DELIVER, slot, fitem, job2 & 0xFFFF, pre=image_c(a, slot, fitem, NORMAL_COND))
    ck("W4q an ORDINARY (NORMAL) copy of the fetched item does NOT complete the job: PRECOND, nothing paid", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    dslot = next(i for i in range(15) if i != slot and a.txn_pre_image()[0][i] == 0)
    mark, sent, r = op(run, a, DELIVER, dslot, fitem, job2 & 0xFFFF, pre=image_c(a, slot, fitem, QUEST_COND, decoy_slot=dslot))
    ck("W4q handing in the NORMAL decoy slot (the QUEST copy sits in another slot) is refused: PRECOND", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    mark, sent, r = op(run, a, DELIVER, slot, fitem, job2 & 0xFFFF, pre=image_c(a, slot, fitem, QUEST_COND, decoy_slot=dslot))
    run.tx_ok("W4 DELIVER of the fetched item to Nook (the QUEST copy, with an ordinary decoy copy elsewhere)", r, APPLIED, R["NONE"])
    ck("W4q the QUEST slot was emptied and its condition cleared, the decoy copy stays untouched (it is the client's own ordinary item)",
       r is not None and r.post_pockets[slot] == 0 and cond_at(r.post_conds, slot) == NORMAL_COND and r.post_pockets[dslot] == fitem and cond_at(r.post_conds, dslot) == NORMAL_COND)
    ck("W4 the Nook reward is paid by the HOST and the job is retired", r is not None and r.post_wallet == wallet0 + reward2 and (last_state(a, mark) or {}).get("state") == 0)

    # ---- W5: job 3 = DELIVER_VILLAGER
    mark, sent, r = op(run, a, ENTER)
    st5 = last_state(a, mark)
    ck("W5 a DELIVER job: a recipient villager, a parcel, a Nook reward (+ an optional tip decided by the host)",
       st5 is not None and st5["type"] == T_DELIVER and st5["villager"] != 0 and st5["carried"] in papers and st5["step"] == 0 and st5["tip_kind"] in (0, 1, 2))
    if st5 is None or st5["type"] != T_DELIVER:
        return
    job3, villager3, parcel, reward3, tkind, tvalue = st5["job_id"], st5["villager"], st5["carried"], st5["reward"], st5["tip_kind"], st5["tip_value"]
    mark, sent, r = op(run, a, REPORT, 0, 0, job3 & 0xFFFF)
    ck("W5 REPORT before anything was delivered is refused PRECOND", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    mark, sent, r = op(run, a, TAKE, slot, parcel, job3 & 0xFFFF, pre=image(a))
    run.tx_ok("W5 TAKE_PARCEL (Nook hands the parcel over)", r, APPLIED, R["NONE"])
    ck("W5 the parcel is in the pocket slot and the step is 1", r is not None and r.post_pockets[slot] == parcel and (last_state(a, mark) or {}).get("step") == 1)
    ck("W5q the parcel is granted as a QUEST item (pocket condition %s, post image)" % (cond_at(r.post_conds, slot) if r is not None else None), r is not None and cond_at(r.post_conds, slot) == QUEST_COND)
    mark, sent, r = op(run, a, TAKE, free_slot(a), parcel, job3 & 0xFFFF, pre=image(a))
    ck("W5 a second parcel is refused (no duplicate item): PRECOND", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    mark, sent, r = op(run, a, RECEIVE, slot, parcel, (villager3 + 1) & 0xFFFF or 5, pre=image(a))
    ck("W5 the WRONG villager takes nothing: PRECOND", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    wallet0 = a.txn_pre_image()[2]
    mark, sent, r = op(run, a, RECEIVE, slot, parcel, villager3, pre=image_c(a, slot, parcel, NORMAL_COND))
    ck("W5q an ORDINARY (NORMAL) copy of the parcel item is NOT taken by the villager: PRECOND, the job is unchanged", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    pslot = next(i for i in range(15) if i != slot and a.txn_pre_image()[0][i] == 0)
    mark, sent, r = op(run, a, RECEIVE, pslot, parcel, villager3, pre=image_c(a, slot, parcel, QUEST_COND, decoy_slot=pslot))
    ck("W5q handing over the NORMAL decoy slot (the QUEST parcel sits in another slot) is refused: PRECOND", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    mark, sent, r = op(run, a, RECEIVE, slot, parcel, villager3, pre=image_c(a, slot, parcel, QUEST_COND, decoy_slot=pslot))
    run.tx_ok("W5 the RIGHT villager takes the parcel (the QUEST copy, with an ordinary decoy copy elsewhere)", r, APPLIED, R["NONE"])
    if r is not None and r.outcome == APPLIED:
        if tkind == 1:
            ck("W5 the villager tip is Bells (%d): the wallet grows by exactly the tip, the slot is empty" % tvalue, r.post_wallet == wallet0 + tvalue and r.post_pockets[slot] == 0)
        elif tkind == 2:
            ck("W5 the villager tip is an item (0x%04X): it takes the parcel's place, the wallet is unchanged" % tvalue, r.post_pockets[slot] == tvalue and r.post_wallet == wallet0)
        else:
            ck("W5 no villager tip: the slot is empty, the wallet is unchanged", r.post_pockets[slot] == 0 and r.post_wallet == wallet0)
        ck("W5 the step is 2 (delivered)", (last_state(a, mark) or {}).get("step") == 2)
    mark, sent, r2 = op(run, a, RECEIVE, slot, parcel, villager3, pre=image_c(a, slot, parcel, QUEST_COND))
    ck("W5 the villager cannot be paid twice (a second RECEIVE is refused): PRECOND", r2 is not None and r2.outcome == REJECTED and r2.reason == REASON_PRECOND)
    wallet0 = a.txn_pre_image()[2]
    mark, sent, r = op(run, a, REPORT, 0, 0, job3 & 0xFFFF)
    run.tx_ok("W5 REPORT to Nook", r, APPLIED, R["NONE"])
    ck("W5 the NOOK reward is paid on top of the tip (the tip never replaces it) and the job is retired", r is not None and r.post_wallet == wallet0 + reward3 and (last_state(a, mark) or {}).get("state") == 0)
    mark, sent, r = op(run, a, REPORT, 0, 0, job3 & 0xFFFF)
    ck("W5 a second REPORT is refused WORK_NO_JOB (33): no second reward", r is not None and r.outcome == REJECTED and r.reason == REASON_NO_JOB)

    # ---- W6: LEAVE quits the job
    mark, sent, r = op(run, a, ENTER)
    st6 = last_state(a, mark)
    ck("W6 a new job (the registry cycles on)", st6 is not None and st6["state"] == 1 and st6["job_id"] > job3)
    job6 = st6["job_id"]
    mark, sent, r = op(run, a, LEAVE)
    run.tx_ok("W6 LEAVE (quit the job)", r, APPLIED, R["NONE"])
    s6 = last_state(a, mark)
    ck("W6 the job is abandoned: state NONE, Work Mode off -- the menu returns to 'I'd like to work'; nothing was paid (jobs_done unchanged)", s6 is not None and s6["state"] == 0 and s6["mode"] == 0 and s6["done"] == 3)
    mark, sent, r = op(run, a, DELIVER, slot, st6["item"], job6 & 0xFFFF, pre=image(a, slot, st6["item"]))
    ck("W6 a completion of the abandoned job is refused WORK_NO_JOB (33)", r is not None and r.outcome == REJECTED and r.reason == REASON_NO_JOB)
    mark, sent, r = op(run, a, ENTER)
    st6b = last_state(a, mark)
    ck("W6 ENTER after a quit creates a NEW job (never the abandoned one)", st6b is not None and st6b["state"] == 1 and st6b["job_id"] > job6)
    jobA = st6b

    # ---- W7: reconnect
    run.release(a)
    a2 = run.ready("A2", run.r1)
    L.pump_sleep(1.0)
    s7 = states(a2)
    ck("W7 the SAME job state (id, type, step, villager, reward) is pushed at connect with NO op after a reconnect",
       len(s7) >= 1 and s7[0]["request_id"] == 0 and s7[0]["job_id"] == jobA["job_id"] and s7[0]["state"] == 1 and s7[0]["type"] == jobA["type"] and s7[0]["step"] == jobA["step"]
       and s7[0]["villager"] == jobA["villager"] and s7[0]["reward"] == jobA["reward"])

    # ---- W8: guest
    g = L.FakeClient("G", run.ip, run.port, guest=L.guest_identity("WORKGUEST", 0x4B01))
    run.clients.append(g)
    gjob = None
    try:
        g.connect_and_ready(quiet=True)
    except Exception as exc:  # noqa: BLE001
        ck("W8 the guest connected (%r)" % (exc,), False)
        g = None
    if g is not None:
        L.pump_sleep(0.8)
        ck("W8 the guest got the connect-time state too (no job)", len(states(g)) >= 1 and states(g)[0]["state"] == 0)
        mark, sent, r = op(run, g, ENTER)
        run.tx_ok("W8 guest ENTER", r, APPLIED, R["NONE"])
        gjob = last_state(g, mark)
        ck("W8 the guest got its OWN job", gjob is not None and gjob["state"] == 1 and gjob["job_id"] != jobA["job_id"])

    # ---- W9: host restart
    all_ids = [job1, job2, job3, jobA["job_id"]] + ([gjob["job_id"]] if gjob else [])
    for cl in list(run.clients):
        run.release(cl)
    ck("W9 the first host did not crash before the restart", run.host.stop() is None)
    ip, port = run.ip, run.port
    host2 = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT)], log_path=os.path.join(HERE, "work_mode_proto_host2.log")).start()
    ok = host2.wait_listening(60.0) and host2.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT)
    run.host = host2
    ck("W9 the host restarted", ok)
    if not ok:
        return
    L.resolve_host_town(ip, port)
    a3 = run.ready("A3", run.r1)
    L.pump_sleep(1.0)
    s9 = states(a3)
    ck("W9 the persisted job survived the restart and is pushed at connect (same id / type / step / villager / reward)",
       len(s9) >= 1 and s9[0]["job_id"] == jobA["job_id"] and s9[0]["type"] == jobA["type"] and s9[0]["villager"] == jobA["villager"] and s9[0]["reward"] == jobA["reward"] and s9[0]["state"] == 1)
    ck("W9 the host loaded work_jobs.dat", run.n_log(r"\[NET\]\[WORK\] host: loaded .*work_jobs\.dat \(next job id \d+(?:, \d+ character\(s\), file version \d)?\)") == 1)
    mark, sent, r = op(run, a3, DELIVER, slot, fitem, job2 & 0xFFFF, pre=image(a3, slot, fitem))
    ck("W9 a completion of a job paid BEFORE the restart is still refused (no duplicate reward across a restart)", r is not None and r.outcome == REJECTED and r.reason in (REASON_STALE_JOB, REASON_NO_JOB))
    mark, sent, r = op(run, a3, LEAVE)
    mark, sent, r = op(run, a3, ENTER)
    s9b = last_state(a3, mark)
    ck("W9 a job id issued after the restart never repeats an earlier one", s9b is not None and s9b["job_id"] > max(all_ids))


def run_phase_two_hosts(results, ip, port, tag, body, snap_gci, restore):
    """TS.run_phase for a body that stops the first host and starts a second one itself (host restart): the LAST host is stopped and checked here."""
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    if len(residents) < 3:
        L.check("[%s] fixture has >= 3 non-host residents (%s)" % (tag, residents), False, results)
        return
    host, ok = TS.start_host(ip, port, tag, [], results)
    run = TP.Run(ip, port, host, results, snap_gci, host_slot, residents[:3])
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
        rc = run.host.stop()
        L.check("[%s] the last host did not crash (exit code before stop: %s)" % (tag, rc), rc is None, results)
        restore()


def run_all(port, results):
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: this test mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_<name> copy only (NET_SPIKE_GAME_BIN=<abs path>)", file=sys.stderr)
        return 2
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    audit_source(ck)
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    work_file = os.path.join(L.GAME_BIN_DIR, "save", "mp", "work_jobs.dat")
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "work_mode_save_snapshot_%d" % os.getpid())
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
        run_phase_two_hosts(results, ip, port, "WORK", phase_work, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11150)
    args = ap.parse_args()
    results = []
    rc = run_all(args.port, results)
    if rc is not None:
        return rc
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
