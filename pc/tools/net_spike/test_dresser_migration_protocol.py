"""test_dresser_migration_protocol.py - the one-time v1 -> v2 migration of the unreleased per-resident dresser pool (60 slots, dresser_store.dat v1) on a
REAL host process (FakeClient, no GUI). Disposable fixture copy only (NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>>).

  G1  a v1 file (resident r1: a FULL 60-item pool; another, unknown identity: 3 items) is renamed to .v1.bak at host start (never deleted)
  G2  at r1's first probe (its house known) the host migrates r1 ONCE: 27 + 27 (6r: 27 slots) into its two storage dressers in (floor, layer, cell) order, 6 to the
      legacy overflow, marker saved in the same atomic v2 save; the main file is v2 now
  G3  SELECT shows the deterministic assignment (dresser 1 = v1 slots 0..23, dresser 2 = 24..47)
  G4  claim path: after 2 TAKEs from dresser 1 a re-SELECT claims 2 overflow items into it (10 left)
  G5  host RESTART: no second migration (marker), the same contents, nothing duplicated
  G6  conservation: dressers + overflow + the 2 taken items == the 60 v1 items, each exactly once; the unknown identity's pool stays in the .v1.bak
"""
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
import test_work_mode_protocol as W  # noqa: E402
import test_dresser_storage_protocol as DP  # noqa: E402
from test_txn_protocol import APPLIED, R  # noqa: E402

V1_ITEMS = [0x2480 + i for i in range(60)]
MAGIC = 0x52534344


def fnv(b, h=2166136261):
    for x in b:
        h = ((h ^ x) * 16777619) & 0xFFFFFFFF
    return h


def v1_key(slot):
    with open(os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL), "rb") as f:
        f.seek(L._GCI_PRIVATE_BASE + slot * L._GCI_PRIVATE_STRIDE)
        raw = f.read(20)
    name, land = raw[0:8], raw[8:16]
    pid, land_id = struct.unpack(">HH", raw[16:20])
    return name + struct.pack("<HHI", pid, land_id, fnv(land))


def v1_record(key, items):
    rec = bytearray(680)
    rec[0:16] = key
    for i, it in enumerate(items):
        struct.pack_into("<H", rec, 46 + 2 * i, it)
    return bytes(rec)


def write_v1(path, recs):
    hdr = struct.pack("<IIII", MAGIC, 1, 0, len(recs))
    crc = fnv(hdr)
    for i, r in enumerate(recs):
        crc ^= (fnv(r) + i * 0x9E3779B1) & 0xFFFFFFFF
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(hdr + b"".join(recs) + struct.pack("<I", crc))


def n_items(s):
    return sum(1 for x in s["items"] if x) if s else -1


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16460)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4_<name> copy only", file=sys.stderr)
        return 2
    results = []
    ck = lambda d, ok: L.check(d, ok, results)  # noqa: E731
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    side = os.path.join(L.GAME_BIN_DIR, "save", "mp", "dresser_store.dat")
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "dresser_mig_snapshot_%d" % os.getpid())
    for p0 in (side, side + ".v1.bak", side + ".tmp"):  # never snapshot a sidecar: the fixture must come back WITHOUT any dresser file
        if os.path.isfile(p0):
            os.remove(p0)
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def clean_side():
        for p in (side, side + ".v1.bak", side + ".tmp"):
            if os.path.isfile(p):
                os.remove(p)

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)
        clean_side()
        # the library's automatic at-exit fixture restore snapshotted save/ at the FIRST launch, i.e. WITH the v1 file this test wrote: drop it there too
        for snap in list(getattr(L, "_FIXTURE_AUTO_GUARDS", {}).values()):
            for p0 in ("dresser_store.dat", "dresser_store.dat.v1.bak", "dresser_store.dat.tmp"):
                q = os.path.join(snap, "mp", p0)
                if os.path.isfile(q):
                    os.remove(q)

    clean_side()
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    r1 = residents[0]
    img = L.house_from_gci(L.GAME_BIN_DIR, r1)
    emp = [c for c in range(L.HOUSE_CELLS) if L.house_cell(img, 0, 0, c) == 0 and c > 40]
    d1, d2 = emp[8], emp[20]  # dresser 1 (lower cell) = chest 0x1068, dresser 2 = chest 0x1070
    write_v1(side, [v1_record(v1_key(r1), V1_ITEMS), v1_record(b"UNKNOWN-IDENTITY", [0x2470, 0x2471, 0x2472])])
    os.environ["AC_TEST_HOOKS"] = "1"
    os.environ["AC_TEST_DRESSER_SEED"] = "%d:%d=1068,%d=1070" % (r1, d1, d2)
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(host_slot), "--house-sync"], log_path=os.path.join(HERE, "dresser_mig_host1.log")).start()
    os.environ.pop("AC_TEST_DRESSER_SEED", None)
    run = None
    try:
        ok = host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=host_slot)
        ck("host (furniture sync ON) reached field-ready state", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        run = TP.Run("127.0.0.1", args.port, host, results, snap_gci, host_slot, residents[:3])
        a = run.ready("A", r1)
        DP.enter_room(a, r1)
        st = DP.query(a)
        ck("setup: probe answered (feature ON)", st is not None and st["enabled"] == 1)
        L.pump_sleep(0.5)
        ck("G1 the v1 file was renamed to .v1.bak (kept, never deleted)", run.n_log(r"v1 \(per-resident\) dresser file .* kept as a backup") == 1 and os.path.isfile(side + ".v1.bak"))
        ck("G1 ... and loaded as the read-only migration source (2 pools)", run.n_log(r"v1 legacy pools .*\.v1\.bak: 2 resident pool\(s\)") == 1)
        ck("G2 resident %d MIGRATED once at its first probe: 54 items -> 2 dressers (27 slots each, 6r), 6 to the legacy overflow" % r1,
           run.n_log(r"v1 MIGRATED resident %d \(house \d+\): 54 item\(s\) -> 2 dresser\(s\) .* 6 to the legacy overflow \(now 6\)" % r1) == 1)
        with open(side, "rb") as f:
            hdr = struct.unpack("<II", f.read(8))
        ck("G2 the main sidecar is a v3 file now (magic DCSR, version 3: 27 slots, 6r)", hdr == (MAGIC, 3))
        s1 = DP.query(a, 0, 0, d1, 1, 0x1068)
        s2 = DP.query(a, 0, 0, d2, 2, 0x1070)
        ck("G3 dresser 1 (cell %d) = v1 slots 0..26 in order" % d1, s1 is not None and s1["items"] == V1_ITEMS[0:27])
        ck("G3 dresser 2 (cell %d) = v1 slots 27..53 in order" % d2, s2 is not None and s2["items"] == V1_ITEMS[27:54])
        # G4: take two out of dresser 1, re-select: the overflow fills the freed slots
        DP.query(a, 0, 0, d1, 3, 0x1068)  # select dresser 1 again (full: nothing to claim)
        for slot_i, it in ((0, V1_ITEMS[0]), (1, V1_ITEMS[1])):
            free = W.free_slot(a)
            mark, _s, r = DP.txn(run, a, DP.OP_TAKE, free, it, slot_i, None, 3)
            run.tx_ok("G4 TAKE 0x%04X out of dresser 1" % it, r, APPLIED, R["NONE"])
        s1 = DP.query(a, 0, 0, d1, 4, 0x1068)
        L.pump_sleep(0.3)
        ck("G4 the re-SELECT CLAIMED 2 legacy overflow items into the freed slots (4 left)", run.n_log(r"resident %d: 2 legacy overflow item\(s\) CLAIMED .*\(4 left" % r1) == 1)
        ck("G4 dresser 1 now holds v1 slots 2..26 + the first two overflow items (54, 55)", s1 is not None and sorted(x for x in s1["items"] if x) == sorted(V1_ITEMS[2:27] + V1_ITEMS[54:56]))
        # G5: restart
        run.release(a)
        ck("G5 the first host did not crash before the restart", run.host.stop() is None)
        host2 = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(host_slot), "--house-sync"], log_path=os.path.join(HERE, "dresser_mig_host2.log")).start()
        ok = host2.wait_listening(60.0) and host2.boot_to_field(timeout=90.0, slot=host_slot)
        run.host = host2
        ck("G5 the host restarted", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        b = run.ready("A2", r1)
        DP.enter_room(b, r1)
        DP.query(b)
        L.pump_sleep(0.5)
        ck("G5 after the restart: NO second migration (the marker)", run.n_log(r"v1 MIGRATED") == 0 and run.n_log(r"v1 legacy pools") == 1)
        t1 = DP.query(b, 0, 0, d1, 1, 0x1068)
        t2 = DP.query(b, 0, 0, d2, 2, 0x1070)
        ck("G5 the same contents after the restart (27 + 27, nothing claimed twice)", t1 is not None and t2 is not None and n_items(t1) == 27 and n_items(t2) == 27 and
           t1["items"] == s1["items"] and t2["items"] == V1_ITEMS[27:54])
        pockets = [x for x in b.txn_pre_image()[0] if x in V1_ITEMS]
        seen = [x for x in t1["items"] + t2["items"] if x in V1_ITEMS] + pockets
        # the 4 still in the overflow are V1_ITEMS[56:60] (not visible on the wire); every other v1 item must be visible exactly once
        ck("G6 conservation: dressers + pockets hold v1 items 0..55 exactly once (%d), the overflow keeps 56..59" % len(seen),
           sorted(seen) == sorted(V1_ITEMS[0:56]) and len(seen) == len(set(seen)))
        ck("G6 the .v1.bak (incl. the unknown identity's pool) is still there, untouched", os.path.isfile(side + ".v1.bak"))
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        ck("phase raised %r" % (exc,), False)
    finally:
        if run is not None:
            for cl in run.clients:
                try:
                    cl.close()
                except Exception:  # noqa: BLE001
                    pass
            rc = run.host.stop()
            ck("the last host did not crash (%s)" % (rc,), rc is None)
        else:
            host.stop()
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
        left = [p0 for p0 in (side, side + ".v1.bak") if os.path.isfile(p0)]
        if left:
            print("WARNING: dresser files left in the fixture after the restore: %s" % left)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
