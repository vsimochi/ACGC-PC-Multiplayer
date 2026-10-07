#!/usr/bin/env python3
"""test_work_items_real.py - Work Mode DELIVERY ITEMS on REAL processes: one REAL host + one REAL client game process (no GUI automation), disposable fixture copy
(NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>>, ports 11960+).

The REAL client runs the real work ops through the TEST-ONLY hook AC_TEST_WORK_ITEMS=1 (m_msg_main.c_inc; the host is forced to job type 3 = deliver to a villager with AC_TEST_WORK_TYPE=3):
ENTER -> TAKE_PARCEL (Nook hands the parcel over) -> LEAVE (quit the job). The client logs its pockets (item, pocket condition) before / after the parcel and after the quit.
  Q1 the parcel lands in a pocket as a vanilla QUEST item (pocket condition 2): the real client APPLY path of the host's post-image; every other pocket (ordinary items, ordinary copies of the
     same item, vanilla quest items) is exactly what it was
  Q2 the parcel slot is NOT sellable / usable by the existing QUEST rules (condition 2): asserted through the pocket condition itself (the vanilla checks read nothing else) + the source audit of
     test_work_mode_protocol.py (S10..S14)
  Q3 LEAVE (the job is abandoned with the parcel still in the pockets): the Work-created parcel becomes an ordinary item again (condition 0, same slot, same item), the cleanup is logged, and NO other pocket
     changes -- the item is not stuck as an unsellable / unusable QUEST item
NOT asserted: the label on screen ("Delivery Item": GUI) and the hand-in with a decoy copy (test_work_mode_protocol.py W4q / W5q, host side).
Usage: python test_work_items_real.py [--port 11960]"""
import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
import net_spike_lib as L  # noqa: E402
import test_txn_real_client as RC  # noqa: E402


def pockets(text, tag):
    """{slot: (item, cond)} of the dump `tag`, or None when the dump never completed."""
    if re.search(r"\[WORKITEM\]\[TEST-ONLY\] %s: parcel item 0x[0-9A-F]{4}, pockets listed" % tag, text) is None:
        return None
    out = {}
    for m in re.finditer(r"\[WORKITEM\]\[TEST-ONLY\] %s: pocket (\d+) item 0x([0-9A-F]{4}) cond (\d)" % tag, text):
        out[int(m.group(1))] = (int(m.group(2), 16), int(m.group(3)))
    return out


def run(args, results, ip, rig):
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    os.environ["AC_TEST_WORK_TYPE"] = "3"
    host = rig.start_host("workitems", args.port, [])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    os.environ["AC_TEST_WORK_ITEMS"] = "1"
    cl = rig.start_client("workitems", args.port, residents[0], [])
    os.environ.pop("AC_TEST_WORK_ITEMS", None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"\[WORKITEM\]\[TEST-ONLY\] after-LEAVE: parcel item", 120.0)
    text = cl.log_text()
    before, take, leave = pockets(text, "before-TAKE"), pockets(text, "after-TAKE"), pockets(text, "after-LEAVE")
    check("the client ran ENTER, TAKE_PARCEL and LEAVE through the real ops (three pocket dumps)", before is not None and take is not None and leave is not None)
    if before is None or take is None or leave is None:
        return
    new = {s: v for s, v in take.items() if before.get(s) != v}
    check("Q1 the parcel appeared in exactly ONE pocket (%s)" % new, len(new) == 1)
    if len(new) != 1:
        return
    slot, (item, cond) = next(iter(new.items()))
    check("Q1 it is a vanilla QUEST item: pocket condition 2 (got %d)" % cond, cond == 2 and slot not in before)
    check("Q1 every other pocket is untouched (ordinary items, same-item copies, vanilla quest items keep item and condition)", all(take.get(s) == v for s, v in before.items()))
    ch = [s for s in leave if leave.get(s) != take.get(s)]
    check("Q3 after the LEAVE only the parcel slot changed (%s)" % ch, ch == [slot])
    check("Q3 the abandoned job's parcel is an ordinary item again: same slot, same item, condition 0 (got %s)" % (leave.get(slot),), leave.get(slot) == (item, 0))
    check("Q3 the cleanup is logged for that item", ("the abandoned job's delivery item 0x%04X (pocket %d) is an ordinary item again" % (item, slot)) in text)
    ht = host.log_text()
    check("the HOST committed TAKE_PARCEL (op 4) and LEAVE (op 3) of the job", re.search(r"WORK op 4 committed: job_id=\d+ type=3 ", ht) is not None and re.search(r"WORK op 3 committed", ht) is not None)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11960)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4_* copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "work_items_snap_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    wj = os.path.join(save_dir, "mp", "work_jobs.dat")
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        if os.path.exists(wj):
            os.remove(wj)
        run(args, results, "127.0.0.1", rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
