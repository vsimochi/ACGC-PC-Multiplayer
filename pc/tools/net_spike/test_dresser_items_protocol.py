"""test_dresser_items_protocol.py - the item-class matrix of the dresser EXTRA storage on a REAL host (FakeClient, no GUI). WRITTEN, NOT RUN (needs the 6f build).
Disposable fixture copy only (NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>>).

For every class the client puts the item into pocket 0 (a record UPLOAD: pockets are client-owned), PUTs it into the chest's extra storage, TAKEs it back:
  accepted (APPLIED, the same u16 comes back, conservation pocket+pool): stationery, money bag, tool, clothing, misc, carpet, wallpaper, fruit, plant, music,
            diary, ticket, lucky bag, fish, insect, furniture FTR0 / FTR1 in pocket form (rotation 0)
  rejected (BAD_SHAPE, nothing changes): turnips, knife and fork, exercise card, Wisp spirit, unknown index, rotated furniture id, scenery id
  rejected (PRECOND): a NORMAL item with a non-NORMAL pocket condition (a wrapped present)
Then the host restarts and the pool still holds the furniture item put last (persistence). Usage: python test_dresser_items_protocol.py [--port 16490]"""
import argparse
import os
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if os.environ.get("AC_MP_DRESSER_STORAGE") is None:
    os.environ["AC_MP_DRESSER_STORAGE"] = "1"
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402
import test_dresser_storage_protocol as DP  # noqa: E402
from test_txn_protocol import APPLIED, REJECTED, R  # noqa: E402

CHEST = 0x1068
ACCEPT = [("stationery", 0x2000), ("money bag", 0x2103), ("tool", 0x2200), ("clothing", 0x2409), ("misc", 0x2500), ("carpet", 0x2603),
          ("wallpaper", 0x2703), ("fruit", 0x2800), ("plant", 0x2900), ("music", 0x2A00), ("diary", 0x2B00), ("ticket", 0x2C00), ("lucky bag", 0x2E00),
          ("fish", 0x2300), ("insect", 0x2D00), ("furniture FTR0", 0x1070), ("furniture FTR1", 0x3000)]
REJECT = [("turnips", 0x2F00), ("knife and fork", 0x2530), ("exercise card", 0x2523), ("Wisp spirit", 0x2D28), ("unknown tool index", 0x225C),
          ("rotated furniture", 0x1069), ("scenery", 0x0800)]
REASON_BAD_SHAPE, REASON_PRECOND = 11, 9


def set_pocket0(run, a, item, cond=0):
    pk, conds, wallet = L.record_inventory(a.rec_local)
    pk = list(pk)
    pk[0] = item
    conds = (conds & ~3) | (cond & 3)
    a.rec_local = L.record_set_inventory(a.rec_local, pk, conds, wallet)  # the local image the TXN pre-image is built from
    for _try in range(3):  # the host's record rate limit: >= 1.5 s between accepted uploads (5 RATE_LIMITED in a row close the connection)
        ack = a.upload_local_record(timeout=5.0, wait_gap=2.1)
        if ack is not None and ack.status == 0:
            return True
    return False


def phase(run):
    ck = run.check
    a = run.ready("A", run.r1)
    DP.enter_room(a, run.r1)
    ck("probe answered", DP.query(a) is not None)
    cell = DP.SEED[0][0]
    a.hub.wait_until(lambda: a.house_canon(run.r1) is not None and L.house_cell(a.house_canon(run.r1), 0, 0, cell) == CHEST, 10.0)
    st = DP.query(a, 0, 0, cell, 1, CHEST)
    ck("SELECT the seeded chest (cell %d)" % cell, st is not None)
    gen = 1
    for name, it in ACCEPT:
        ok = set_pocket0(run, a, it)
        pre = a.txn_pre_image()
        mark, _s, r = DP.txn(run, a, DP.OP_PUT, 0, it, 0xFF, pre, gen)
        put_ok = r is not None and r.outcome == APPLIED
        s = (DP.states(a) or [None])[-1]
        slot = s["items"].index(it) if (s and it in s["items"]) else -1
        mark, _s, r2 = DP.txn(run, a, DP.OP_TAKE, 0, it, slot, None, gen) if slot >= 0 else (None, None, None)
        back = r2 is not None and r2.outcome == APPLIED and a.txn_pre_image()[0][0] == it
        ck("ACCEPT %s 0x%04X: pocket set %s, PUT APPLIED %s, TAKE returns the same u16 %s" % (name, it, ok, put_ok, back), ok and put_ok and back)
    for k, (name, it) in enumerate(REJECT):
        # every refused shape counts as a record-protocol violation (3 close the connection): a FRESH connection per rejected class
        run.release(a)
        a = run.ready("R%d" % k, run.r1)
        DP.enter_room(a, run.r1)
        DP.query(a)
        DP.query(a, 0, 0, cell, gen, CHEST)
        set_pocket0(run, a, it)
        pre = a.txn_pre_image()
        mark, _s, r = DP.txn(run, a, DP.OP_PUT, 0, it, 0xFF, pre, gen)
        ck("REJECT %s 0x%04X: PUT REJECTED BAD_SHAPE, nothing stored" % (name, it), r is not None and r.outcome == REJECTED and r.reason == REASON_BAD_SHAPE)
    run.release(a)
    a = run.ready("P", run.r1)
    DP.enter_room(a, run.r1)
    DP.query(a)
    DP.query(a, 0, 0, cell, gen, CHEST)
    set_pocket0(run, a, 0x2409, cond=1)  # a wrapped present (PRESENT condition)
    mark, _s, r = DP.txn(run, a, DP.OP_PUT, 0, 0x2409, 0xFF, a.txn_pre_image(), gen)
    ck("REJECT a wrapped present (condition PRESENT): PRECOND", r is not None and r.outcome == REJECTED and r.reason == REASON_PRECOND)
    # persistence: furniture stays in the pool across a host restart
    set_pocket0(run, a, 0x3000)
    mark, _s, r = DP.txn(run, a, DP.OP_PUT, 0, 0x3000, 0xFF, a.txn_pre_image(), gen)
    ck("PUT furniture 0x3000 for the restart check", r is not None and r.outcome == APPLIED)
    run.release(a)
    ck("the first host did not crash", run.host.stop() is None)
    host2 = L.HostProcess(port=run.port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT), "--house-sync"],
                          log_path=os.path.join(HERE, "dresser_items_host2.log")).start()
    ok = host2.wait_listening(60.0) and host2.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT)
    run.host = host2
    ck("the host restarted", ok)
    if not ok:
        return
    L.resolve_host_town(run.ip, run.port)
    b = run.ready("A2", run.r1)
    DP.enter_room(b, run.r1)
    DP.query(b)
    s = DP.query(b, 0, 0, cell, 1, CHEST)
    ck("after the restart the pool still holds the furniture item 0x3000 (persisted as the same u16)", s is not None and 0x3000 in s["items"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16490)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_<name> copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    side = os.path.join(L.GAME_BIN_DIR, "save", "mp", "dresser_store.dat")
    for p0 in (side, side + ".v1.bak"):
        if os.path.isfile(p0):
            os.remove(p0)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "dresser_items_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)
        for p0 in (side, side + ".v1.bak"):
            if os.path.isfile(p0):
                os.remove(p0)

    try:
        DP.run_two_hosts_house_sync(results, "127.0.0.1", args.port, phase, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
