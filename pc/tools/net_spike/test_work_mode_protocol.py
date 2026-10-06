#!/usr/bin/env python3
"""test_work_mode_protocol.py - Patch 3 (Nook Work Mode foundation): TXN_COMMIT kind 16 WORK + WORK_STATE (id 68) against a REAL host process.

TIER: PROTOCOL TESTED (real host binary, `--host --bootstrap-resident`, scripted resident / guest FakeClients -- the harness of test_shop_catalog_protocol.py). The Nook dialogue seam
(ac_npc_shop_common.c) and the client apply path are covered by the source audit (section S) and the build, NOT by a GUI run here; no visual verification is claimed.
Run on a DISPOSABLE copy: NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>> (the fixture save is snapshotted and restored; the host's save/mp/work_jobs.dat is removed).
  W1 ENTER: APPLIED, WORK_STATE carries a NEW host job (unique non-zero job_id, a fruit objective, a reward), Work Mode on
  W2 ENTER again: APPLIED with the SAME job (resume, no second job)
  W3 DELIVER refused: no job for a character that never entered (WORK_NO_JOB), a wrong job id (WORK_STALE_JOB), a wrong item (PRECOND); nothing credited / consumed
  W4 DELIVER with the right job id + item: APPLIED, the pocket slot is emptied and the wallet = pre + reward (HOST decides the reward), job retired, jobs_done 1, ONE host commit line
  W5 duplicate packet / retransmit of the same (nonce, seq) is answered from the journal (REPLAYED), nothing paid twice (still ONE commit line, same post wallet)
  W6 a stale completion with a NEW sequence number for the retired job is refused (WORK_NO_JOB); after a fresh ENTER the OLD job id is refused (WORK_STALE_JOB) -- no duplicate reward
  W7 LEAVE: Work Mode off, the active job is kept; DELIVER while not in Work Mode is refused (WORK_NO_JOB)
  W8 disconnect + reconnect as the SAME character: ENTER returns the SAME job id (the job belongs to the character, not the connection / peer slot)
  W9 a GUEST character works too (ENTER, DELIVER, reward) and is independent of the residents
  W10 host restart: the persisted job survives (same job id, reward), a paid job can never be paid again (stale completion refused), and new job ids never repeat
  S  source audit: host-owned table keyed by the bound PID, durable save before the result, rekey on promotion, dialogue seam
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
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_ts_protocol as TS  # noqa: E402
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE  # noqa: E402

K_WORK = 16
MSG_WORK_STATE = 68
WS_FMT = "<BBBBIIIIHHI"
WS_SIZE = struct.calcsize(WS_FMT)
OP_ENTER, OP_DELIVER, OP_LEAVE = 1, 2, 3
REASON_PRECOND, REASON_NO_JOB, REASON_STALE_JOB, REASON_WALLET_FULL = 9, 33, 34, 35
FRUITS = {0x2800 + i for i in range(5)}  # ITM_FOOD_START(+0..4); re-read from the source below
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def decode_state(payload):
    t, flags, state, jtype, job_id, reward, done, last, item, cnt, rid = struct.unpack(WS_FMT, payload)
    return dict(flags=flags, state=state, job_type=jtype, job_id=job_id, reward=reward, done=done, last=last, item=item, count=cnt, request_id=rid)


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
    return int(m.group(1), 16) if m else None


def work(run, c, op, slot=0, item=0, job_low16=0, pre=None, wait=4.0, **kw):
    """One WORK op; returns (since-mark, sent, result)."""
    mark = c.inbox.mark()
    rid = run.fresh_rid()
    sent = c.send_txn_commit(K_WORK, rid, D_NONE, slot, item, pre=pre, aux_cond=op, aux_item=job_low16, **kw)
    return mark, sent, c.wait_txn_result(sent.seq, wait)


def with_item(c, slot, item):
    pockets, conds, wallet = c.txn_pre_image()
    pk = tuple(item if i == slot else p for i, p in enumerate(pockets))
    return (pk, conds, wallet)


def free_slot(c):
    return TP.first_free_pocket(c)


def audit_source(check):
    net = open(os.path.join(ROOT, "pc", "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read()
    shop = open(os.path.join(ROOT, "src", "actor", "npc", "ac_npc_shop_common.c"), encoding="utf-8", errors="replace").read()
    nook = open(os.path.join(ROOT, "pc", "src", "pc_nook_house.c"), encoding="utf-8", errors="replace").read()
    check("S1 the job table is keyed by the bound character PID (never the peer / connection) and persisted before the result is sent",
          "pcnetgame_work_find(&st->bound_pid, 1)" in net and "pcnetgame_work_save(); /* durable BEFORE the result" in net)
    check("S2 the host decides: job id match + objective item + wallet cap are checked on the HOST record; the reward is the record's, never the client's",
          "pcnetgame_work_check_deliver(work, t->aux_item, t->item, &fail)" in net and "work_reward = work->reward;" in net and "post_wallet += work_reward;" in net)
    check("S3 the job is retired + tombstoned in the SAME step as the reward (commit_deliver) and a duplicate is answered from the journal",
          "pcnetgame_work_commit_deliver(work); /* the job is retired + tombstoned" in net and "answered from the journal, nothing re-executed" in net)
    check("S4 a guest buying a house keeps its job: the record is re-keyed to the new resident PID in the shared promotion core",
          "pcnetgame_work_rekey(&old_g.key, &Save_Get(private_data)[s].player_ID)" in net)
    check("S5 the dialogue offers 'I'd like to work' under Other things (guest menu + resident menu) and runs ENTER / DELIVER / LEAVE through pc_net_game_work_begin",
          "aNSC_pc_wk_start_flow(shop_common)" in shop and "pc_net_game_work_begin(op)" in shop and "PC_NOOK_SEL_WORK" in nook)
    check("S6 the job ids are host-generated, never reused and never 0 mod 65536 (the wire carries the low 16 bits)",
          "} while ((s_work_next_job_id & 0xFFFFu) == 0u);" in net)


def phase_work(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    ck("W0 setup: A and B READY and SYNCED", a.rec_synced and b.rec_synced)
    fs = food_start()
    fruits = {fs + i for i in range(5)} if fs is not None else FRUITS
    slot = free_slot(a)

    # ---- W1
    off = len(run.log())
    mark, sent, r = work(run, a, OP_ENTER)
    run.tx_ok("W1 ENTER", r, APPLIED, R["NONE"])
    st1 = last_state(a, mark)
    ck("W1 a WORK_STATE answered it: Work Mode on, an ACTIVE job with a unique non-zero job_id, a fruit objective and a reward",
       st1 is not None and (st1["flags"] & 1) == 1 and st1["state"] == 1 and st1["job_id"] != 0 and (st1["job_id"] & 0xFFFF) != 0 and st1["item"] in fruits and 300 <= st1["reward"] <= 600
       and st1["request_id"] != 0)
    if st1 is None:
        return
    job1, item1, reward1 = st1["job_id"], st1["item"], st1["reward"]
    ck("W1 the host logged the job creation", run.n_log(r"\[NET\]\[WORK\] host: peer \d+ resident \d+ WORK op 1 committed: job_id=%d " % job1, off) == 1)

    # ---- W2
    mark, sent, r = work(run, a, OP_ENTER)
    run.tx_ok("W2 ENTER again", r, APPLIED, R["NONE"])
    st2 = last_state(a, mark)
    ck("W2 the SAME job is returned (resumed, not re-created)", st2 is not None and st2["job_id"] == job1 and st2["item"] == item1 and st2["reward"] == reward1)

    # ---- W3
    pre_b = with_item(b, slot, item1)
    mark, sent, rb = work(run, b, OP_DELIVER, slot, item1, job1 & 0xFFFF, pre=pre_b)
    ck("W3 B never entered: DELIVER refused WORK_NO_JOB (33), nothing consumed",
       rb is not None and rb.outcome == REJECTED and rb.reason == REASON_NO_JOB)
    pre_a = with_item(a, slot, item1)
    mark, sent, r = work(run, a, OP_DELIVER, slot, item1, (job1 + 1) & 0xFFFF or 7, pre=pre_a)
    ck("W3 a completion naming ANOTHER job id is refused WORK_STALE_JOB (34)", r is not None and r.outcome == REJECTED and r.reason == REASON_STALE_JOB)
    wrong = next(f for f in sorted(fruits) if f != item1)
    pre_w = with_item(a, slot, wrong)
    mark, sent, r = work(run, a, OP_DELIVER, slot, wrong, job1 & 0xFFFF, pre=pre_w)
    ck("W3 handing in an item that is NOT the objective is refused PRECOND (9)", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)

    # ---- W4
    off = len(run.log())
    pockets, conds, wallet0 = a.txn_pre_image()
    pre_a = with_item(a, slot, item1)
    mark, sent4, r4 = work(run, a, OP_DELIVER, slot, item1, job1 & 0xFFFF, pre=pre_a)
    run.tx_ok("W4 DELIVER of the job item", r4, APPLIED, R["NONE"])
    ck("W4 the HOST paid its own reward: post wallet = pre wallet + reward", r4 is not None and r4.post_wallet == wallet0 + reward1)
    ck("W4 the pocket slot is empty in the post image", r4 is not None and r4.post_pockets[slot] == 0)
    s4 = last_state(a, mark)
    ck("W4 WORK_STATE: job retired (state NONE), tombstone = the job id, jobs_done 1", s4 is not None and s4["state"] == 0 and s4["last"] == job1 and s4["done"] == 1)
    ck("W4 exactly ONE host commit line for the delivery", run.n_log(r"WORK op 2 committed: job_id=%d " % job1, off) == 1)

    # ---- W5: duplicate packet / retransmit of the same bytes
    a.resend_txn(sent4)
    r5 = a.wait_txn_result(sent4.seq, 4.0, nth=2)
    ck("W5 the retransmit is answered from the journal: APPLIED, reason REPLAYED, the same post wallet",
       r5 is not None and r5.outcome == APPLIED and r5.reason == R["REPLAYED"] and r5.post_wallet == r4.post_wallet)
    L.pump_sleep(0.4)
    ck("W5 still ONE commit line (nothing re-executed, nothing paid twice)", run.n_log(r"WORK op 2 committed: job_id=%d " % job1, off) == 1)

    # ---- W6: a stale completion with a NEW seq for the retired job; a new job; the old id refused
    pre_a = with_item(a, slot, item1)
    mark, sent, r = work(run, a, OP_DELIVER, slot, item1, job1 & 0xFFFF, pre=pre_a)
    ck("W6 a new-seq completion of the already PAID job is refused WORK_NO_JOB (33): no second reward", r is not None and r.outcome == REJECTED and r.reason == REASON_NO_JOB)
    mark, sent, r = work(run, a, OP_ENTER)
    run.tx_ok("W6 ENTER after the paid job", r, APPLIED, R["NONE"])
    st6 = last_state(a, mark)
    ck("W6 a NEW job (a larger host job id), the old one is gone", st6 is not None and st6["job_id"] > job1 and st6["state"] == 1)
    job2, item2, reward2 = st6["job_id"], st6["item"], st6["reward"]
    pre_a = with_item(a, slot, item1)
    mark, sent, r = work(run, a, OP_DELIVER, slot, item1, job1 & 0xFFFF, pre=pre_a)
    ck("W6 the OLD job id is refused WORK_STALE_JOB (34) while the new job is active", r is not None and r.outcome == REJECTED and r.reason == REASON_STALE_JOB)

    # ---- W7: LEAVE keeps the job, no work outside Work Mode
    mark, sent, r = work(run, a, OP_LEAVE)
    run.tx_ok("W7 LEAVE", r, APPLIED, R["NONE"])
    s7 = last_state(a, mark)
    ck("W7 Work Mode off, the active job is kept", s7 is not None and (s7["flags"] & 1) == 0 and s7["state"] == 1 and s7["job_id"] == job2)
    pre_a = with_item(a, slot, item2)
    mark, sent, r = work(run, a, OP_DELIVER, slot, item2, job2 & 0xFFFF, pre=pre_a)
    ck("W7 DELIVER while NOT in Work Mode is refused WORK_NO_JOB (33)", r is not None and r.outcome == REJECTED and r.reason == REASON_NO_JOB)

    # ---- W8: reconnect as the same character
    run.release(a)
    a2 = run.ready("A2", run.r1)
    mark, sent, r = work(run, a2, OP_ENTER)
    run.tx_ok("W8 ENTER after a disconnect + reconnect as the same character", r, APPLIED, R["NONE"])
    s8 = last_state(a2, mark)
    ck("W8 the SAME job (id / item / reward) comes back: it belongs to the character, not the connection", s8 is not None and s8["job_id"] == job2 and s8["item"] == item2 and s8["reward"] == reward2)

    # ---- W9: a guest works too
    g = L.FakeClient("G", run.ip, run.port, guest=L.guest_identity("WORKGUEST", 0x4B01))
    run.clients.append(g)
    try:
        g.connect_and_ready(quiet=True)
    except Exception as exc:  # noqa: BLE001
        ck("W9 the guest connected (%r)" % (exc,), False)
        g = None
    gjob = None
    if g is not None:
        gslot = free_slot(g)
        mark, sent, r = work(run, g, OP_ENTER)
        run.tx_ok("W9 guest ENTER", r, APPLIED, R["NONE"])
        sg = last_state(g, mark)
        ck("W9 the guest got its OWN job (different from A's)", sg is not None and sg["state"] == 1 and sg["job_id"] not in (job1, job2))
        if sg is not None:
            gjob = sg
            gp0 = g.txn_pre_image()[2]
            mark, sent, r = work(run, g, OP_DELIVER, gslot, sg["item"], sg["job_id"] & 0xFFFF, pre=with_item(g, gslot, sg["item"]))
            ck("W9 the guest's DELIVER is APPLIED and pays the host reward", r is not None and r.outcome == APPLIED and r.post_wallet == gp0 + sg["reward"])

    # ---- W10: host restart
    all_jobs = [job1, job2] + ([gjob["job_id"]] if gjob else [])
    for cl in list(run.clients):
        run.release(cl)
    ck("W10 the first host did not crash before the restart", run.host.stop() is None)
    ip, port = run.ip, run.port
    host2 = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT)], log_path=os.path.join(HERE, "work_mode_proto_host2.log")).start()
    ok = host2.wait_listening(60.0) and host2.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT)
    run.host = host2
    ck("W10 the host restarted", ok)
    if not ok:
        return
    L.resolve_host_town(ip, port)
    a3 = run.ready("A3", run.r1)
    mark, sent, r = work(run, a3, OP_ENTER)
    run.tx_ok("W10 ENTER after a host restart", r, APPLIED, R["NONE"])
    s10 = last_state(a3, mark)
    ck("W10 the persisted job survived the restart (same id / item / reward)", s10 is not None and s10["job_id"] == job2 and s10["item"] == item2 and s10["reward"] == reward2)
    ck("W10 the host loaded work_jobs.dat", run.n_log(r"\[NET\]\[WORK\] host: loaded .*work_jobs\.dat \(next job id \d+\)") == 1)
    pre = with_item(a3, slot, item1)
    mark, sent, r = work(run, a3, OP_DELIVER, slot, item1, job1 & 0xFFFF, pre=pre)
    ck("W10 a completion of the job paid BEFORE the restart is still refused (no duplicate reward across a restart)", r is not None and r.outcome == REJECTED and r.reason in (REASON_STALE_JOB, REASON_NO_JOB))
    # pay job2 now, then a fresh ENTER must produce a job id beyond everything issued before the restart
    pre = with_item(a3, slot, item2)
    mark, sent, r = work(run, a3, OP_DELIVER, slot, item2, job2 & 0xFFFF, pre=pre)
    ck("W10 the restored job can be completed and paid", r is not None and r.outcome == APPLIED)
    mark, sent, r = work(run, a3, OP_ENTER)
    s10b = last_state(a3, mark)
    ck("W10 a job id issued after the restart never repeats an earlier one", s10b is not None and s10b["job_id"] > max(all_jobs))


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
