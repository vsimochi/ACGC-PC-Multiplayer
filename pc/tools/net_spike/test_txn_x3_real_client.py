#!/usr/bin/env python3
"""test_txn_x3_real_client.py - X3: the REAL game client's half of the host-transactional GRANTS (DIG_BURIED / DIG_HOLE bonus / DIG_SHINE bell
and the fish CATCH), REAL processes.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0 --field-action-test-seed [--txn-fault=<mode>] [--authoritative-wildlife]`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot> --txn-test-dig-grant`   (dig scenarios)
           `... --authoritative-wildlife --force-fish-catch`                                                   (catch scenario)
The client's tag build / send / byte-identical resend / apply C code (pcnetgame_txn_begin_grant, pcnetgame_txn_try_send, pcnetgame_txn_tick,
pcnetgame_handle_client_txn_result, pcnetgame_txn_apply_applied) and the host's journal + execution run for real. The assertions come from the
processes' own log lines (order: request sent < TXN_RESULT < pocket write, and the hook's own before / after pocket dumps) plus byte-exact
checks of the host record and the host world through a fresh scripted FakeClient session (PUSH_FULL of the mirror + the join snapshot).
The only synthetic inputs are TEST-ONLY, default-off hooks: client --txn-test-dig-grant (moves the actor onto the --field-action-test-seed
fixtures and calls the REAL request functions pc_net_game_request_dig_buried / _dig_hole_with_grant (fixed ITM_MONEY_100) /
_dig_shine_with_grant (fixed ITM_MONEY_1000) -- it bypasses the shovel animation, the input and the random roll, never the request /
TXN_RESULT chain); client --force-fish-catch (existing: the real pc_net_game_request_catch_fish() on a latched entity); host --txn-fault.
NOT covered (no UI automation): the full-pockets exchange-menu flow with a REAL client (the host side is tested in test_txn_x3_protocol.py with
scripted clients; the client side of the exchange drop is source-audited), the real shovel / net / rod animations, a real BUG catch (the
existing --force-bug-catch hook was deliberately not used: bug-catch tests are excluded from this campaign).

Run ONLY on the disposable pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN=<absolute path>, ports 10850+). The fixture save is snapshotted at
start and restored before every host launch and at the end. At most 1 real client (+ a short FakeClient session) at a time.

  D1  the three dig grants with the real client, no fault: each request leaves ONLY after the record is SYNCED, carries dest POCKET + a free
      slot, is answered by TXN_RESULT(APPLIED) and the pocket changes ONLY then (hook BEFORE / RESOLVED dumps: exactly one slot 0 -> item);
      the host world + record change exactly once each and equal the client's final inventory.
  D2  host --txn-fault=drop_result: the first APPLIED TXN_RESULT is lost, the client resends the IDENTICAL request after ~2 s, the host replays
      the journalled APPLIED, the client applies exactly once (no duplicate grant, no CONFLICT).
  D3  host --txn-fault=kill_peer_after_commit on the first grant + the client process killed: the dug item is NOT in the dead client's
      pockets (it never saw APPLIED), is in the host record exactly once, the tile was dug once; a NEW client process adopts that record.
  C1  fish catch: a FakeClient seeds ONE fish (host --authoritative-wildlife), the real client (--force-fish-catch) claims it: the item enters
      the pocket only on APPLIED, the outcome query reads ACCEPTED, the entity is removed once, the host record holds the fish exactly once.
Usage: python test_txn_x3_real_client.py [--port 10850] [--only d1,d2,d3,c1]
"""
import argparse
import os
import re
import shutil
import sys
import time

import net_spike_lib as L
import test_txn_real_client as RC
import test_wildlife_catch_exchange_gate as WG

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = {"DIG_BURIED": ((40, 104), 0x2800), "DIG_HOLE": ((56, 105), 0x2103), "DIG_SHINE": ((88, 105), 0x2100)}
STAGES = ["DIG_BURIED", "DIG_HOLE", "DIG_SHINE"]
HOLE_START, HOLE_SHINE = 0x0011, 0x005D


def count(rx, text):
    return len(re.findall(rx, text))


def pockets_of(text, stage, which):
    m = re.search(r"--txn-test-dig-grant: stage %d %s pockets=([0-9A-F,]+) wallet=(\d+)" % (stage, which), text)
    return None if m is None else tuple(int(x, 16) for x in m.group(1).strip(",").split(",")), (None if m is None else int(m.group(2)))


def host_tiles(port, ip, slot, label, tiles):
    f = RC.fake_session(port, ip, slot, label)
    push = f.rec_pushes[-1]
    out = (push["rev"], L.record_inventory(push["data"]), {t: f.world.tile_ut(*t) for t in tiles})
    RC.release(f)
    return out


def run_dig(rig, tag, port, ip, r1, fault=None):
    extra = ["--field-action-test-seed"] + ([fault] if fault else [])
    host = rig.start_host(tag, port, extra)
    rig.check("%s host reached genuine field-ready state" % tag, host is not None)
    if host is None:
        return None
    L.resolve_host_town(ip, port)
    cl = rig.start_client(tag, port, r1, ["--txn-test-dig-grant"])
    rig.check("%s real client booted into the field and connected" % tag, cl is not None)
    if cl is None:
        host.stop()
        return None
    return host, cl


def check_stage(rig, tag, ct, st, base_inv):
    """The per-stage client-log assertions: returns the slot the grant filled (or None)."""
    ck = rig.check
    name = STAGES[st]
    tile, item = FIX[name]
    before, w0 = pockets_of(ct, st, "BEFORE the request")
    after, w1 = pockets_of(ct, st, "RESOLVED")
    ck("%s stage %d (%s): the hook dumped the pockets before the request and after the transaction resolved" % (tag, st, name),
       before is not None and after is not None)
    if before is None or after is None:
        return None
    diff = [i for i in range(15) if before[i] != after[i]]
    ck("%s stage %d: exactly ONE pocket slot changed (0x0000 -> 0x%04X), the wallet is untouched" % (tag, st, item),
       len(diff) == 1 and before[diff[0]] == 0 and after[diff[0]] == item and w0 == w1)
    slot = diff[0] if diff else None
    m_sent = re.search(r"TXN grant request sent kind=%s request=(\d+) nonce=(\d+) seq=(\d+) dest=1 slot=(\d+) item=0x([0-9A-F]{4}) base=\(epoch (\d+), rev (\d+)\)"
                       % name, ct)
    m_res = re.search(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=%s request=(\d+) seq=(\d+) rev=(\d+) epoch=(\d+)" % name, ct)
    m_app = re.search(r"client: APPLIED request (\d+) kind=%s -- host post-image: slot (\d+) item 0x%04X, wallet now (\d+)" % (name, item), ct)
    ck("%s stage %d: a grant request (dest POCKET, slot %s, item 0x%s: %s) was sent, answered by TXN_RESULT(APPLIED) with the same seq, and applied by the host "
       "post-image into that slot" % (tag, st, slot, "0000" if name == "DIG_BURIED" else "%04X" % item, name),
       m_sent is not None and m_res is not None and m_app is not None and m_sent.group(3) == m_res.group(2) and int(m_sent.group(4)) == slot
       and int(m_app.group(2)) == slot and int(m_sent.group(5), 16) == (0 if name == "DIG_BURIED" else item) and int(m_res.group(3)) > int(m_sent.group(7)))
    if m_sent and m_res and m_app:
        p_before = ct.find("--txn-test-dig-grant: stage %d BEFORE the request" % st)
        ck("%s stage %d order: pockets BEFORE < request sent < TXN_RESULT APPLIED < pocket write (APPLIED line) < RESOLVED dump" % (tag, st),
           0 <= p_before < m_sent.start() < m_res.start() < m_app.start() < ct.find("--txn-test-dig-grant: stage %d RESOLVED" % st))
    ck("%s stage %d: no pocket changed before APPLIED (the hook's invariant check never fired)" % (tag, st),
       "POCKET CHANGED BEFORE APPLIED" not in ct)
    return slot


def run(args, results, ip, snap_gci, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has two non-host residents (slots %s)" % residents, len(residents) >= 2)
    if len(residents) < 2:
        return
    r1, r2 = residents[0], residents[1]
    gci_inv = L.record_inventory(L.record_from_gci(snap_gci, r1))
    tiles = [FIX[n][0] for n in STAGES]

    def finish_dig(tag, port, host, cl):
        L.pump_sleep(3.0)
        ct = cl.log_text()
        off = len(host.log_text())
        cl.stop()
        rig.wait_peer_gone(off - 1)
        return ct

    def _d1():
        port = args.port
        t = run_dig(rig, "d1", port, ip, r1)
        if t is None:
            return
        host, cl = t
        m = cl.wait_for_log(r"--txn-test-dig-grant: stage 2 RESOLVED", 150.0)
        check("D1 the real client completed the three dig-grant stages", m is not None)
        ct = finish_dig("d1", port, host, cl)
        ht = host.log_text()
        slots = [check_stage(rig, "D1", ct, st, gci_inv[0]) for st in range(3)]
        check("D1 the three grants filled three DIFFERENT slots (never the same slot twice)", None not in slots and len(set(slots)) == 3)
        check("D1 exactly 3 grant requests sent, exactly 3 applies, no resend, no rejection / delta / inconsistency / violation in the client log",
              count(r"TXN grant request sent ", ct) == 3 and count(r"client: APPLIED request ", ct) == 3 and "resending the identical" not in ct
              and "REJECTED(" not in ct and "delta impossible" not in ct and "inconsistent post-image" not in ct and "protocol violation" not in ct)
        check("D1 no legacy client grant: the old 'granted to a free pocket slot' / 'LOST -- free slot vanished' lines are absent",
              "granted to a free pocket slot" not in ct and "free slot vanished" not in ct)
        check("D1 host: one TXN APPLIED line per kind (DIG_BURIED, DIG_HOLE, DIG_SHINE), one grant-committed line each, no CONFLICT / INTERNAL",
              all(count(r"TXN APPLIED kind=%s " % n, ht) == 1 for n in STAGES) and all(count(r"\[%s\] host: peer \d+ request \d+ grant committed" % n, ht) == 1 for n in STAGES)
              and "CONFLICT" not in ht and "INTERNAL" not in ht)
        after2, w2 = pockets_of(ct, 2, "RESOLVED")
        rev, inv, tv = host_tiles(port, ip, r1, "D1rec", tiles)
        check("D1 host record (new FakeClient session): pockets == the client's final inventory (apple + 100-bell bonus + 1000-bell roll, once each), wallet unchanged",
              after2 is not None and tuple(inv[0]) == after2 and inv[2] == gci_inv[2] and all(list(inv[0]).count(FIX[n][1]) == list(gci_inv[0]).count(FIX[n][1]) + 1 for n in STAGES))
        check("D1 host world (fresh join snapshot): the buried tile is a hole, the DIG_HOLE tile is HOLE_START+0, the shine tile is HOLE_SHINE (observed %s)"
              % {k: (None if v is None else hex(v)) for k, v in tv.items()},
              tv[FIX["DIG_BURIED"][0]] is not None and HOLE_START <= tv[FIX["DIG_BURIED"][0]] <= HOLE_START + 24 and tv[FIX["DIG_HOLE"][0]] == HOLE_START
              and tv[FIX["DIG_SHINE"][0]] == HOLE_SHINE)
        host.stop()

    def _d2():
        port = args.port + 1
        t = run_dig(rig, "d2", port, ip, r1, "--txn-fault=drop_result:1:1")
        if t is None:
            return
        host, cl = t
        m = cl.wait_for_log(r"--txn-test-dig-grant: stage 2 RESOLVED", 150.0)
        check("D2 the real client completed the three dig-grant stages despite the lost TXN_RESULT", m is not None)
        ct = finish_dig("d2", port, host, cl)
        ht = host.log_text()
        check("D2 host fired drop_result once (the first APPLIED TXN_RESULT was swallowed)", count(r"drop_result: TXN_RESULT APPLIED for seq \d+ NOT sent", ht) == 1)
        check("D2 the client resent the IDENTICAL request after ~2 s (resend 1 logged), the SENT line of that request appears once (one build)",
              re.search(r"resending the identical request \(resend 1\)", ct) is not None and count(r"TXN grant request sent kind=DIG_BURIED", ct) == 1)
        check("D2 the host replayed the journalled APPLIED (no second execution, no CONFLICT) and the client applied from the replay exactly once",
              count(r"TXN replay APPLIED kind=DIG_BURIED", ht) == 1 and count(r"\[DIG_BURIED\] host: peer \d+ request \d+ grant committed", ht) == 1
              and "CONFLICT" not in ht and "TXN_RESULT APPLIED (replayed) kind=DIG_BURIED" in ct and count(r"client: APPLIED request \d+ kind=DIG_BURIED", ct) == 1)
        slots = [check_stage(rig, "D2", ct, st, gci_inv[0]) for st in range(3)]
        check("D2 three DIFFERENT slots, three applies in total (no duplicate grant from the resend)", None not in slots and len(set(slots)) == 3
              and count(r"client: APPLIED request ", ct) == 3)
        after2, _w = pockets_of(ct, 2, "RESOLVED")
        rev, inv, tv = host_tiles(port, ip, r1, "D2rec", tiles)
        check("D2 host record == the client's final inventory (every item once)", after2 is not None and tuple(inv[0]) == after2
              and all(list(inv[0]).count(FIX[n][1]) == list(gci_inv[0]).count(FIX[n][1]) + 1 for n in STAGES))
        host.stop()

    def _d3():
        port = args.port + 2
        t = run_dig(rig, "d3", port, ip, r1, "--txn-fault=kill_peer_after_commit:1:1")
        if t is None:
            return
        host, cl = t
        m = host.wait_for_log(r"\[NET\]\[TXN\]\[TEST-ONLY\] kill_peer_after_commit: peer \d+ dropped AFTER the grant commit", 150.0)
        check("D3 host fired kill_peer_after_commit after executing the real client's DIG_BURIED grant", m is not None)
        cl.proc.kill()
        cl.proc.wait(10)
        ct = cl.log_text()
        check("D3 the client had sent its grant request before it died, and never mutated its pockets (no APPLIED, no RESOLVED dump)",
              "TXN grant request sent kind=DIG_BURIED" in ct and "client: APPLIED request" not in ct and "stage 0 RESOLVED" not in ct)
        before, _w = pockets_of(ct, 0, "BEFORE the request")
        ht = host.log_text()
        check("D3 host: the grant committed exactly once and journalled APPLIED, no RESULT sent",
              count(r"\[DIG_BURIED\] host: peer \d+ request \d+ grant committed", ht) == 1 and count(r"TXN APPLIED kind=DIG_BURIED", ht) == 0)
        L.pump_sleep(2.0)
        rev, inv, tv = host_tiles(port, ip, r1, "D3rec", tiles)
        exp_apple = list(gci_inv[0]).count(0x2800) + 1
        check("D3 host record after the kill: the dug apple is in the pockets exactly once more than before (%d), nothing else changed"
              % exp_apple, list(inv[0]).count(0x2800) == exp_apple and inv[2] == gci_inv[2]
              and sorted(x for x in inv[0] if x) == sorted([x for x in gci_inv[0] if x] + [0x2800]))
        check("D3 the buried tile was dug once (a hole now: %s)" % (None if tv[FIX["DIG_BURIED"][0]] is None else hex(tv[FIX["DIG_BURIED"][0]])),
              tv[FIX["DIG_BURIED"][0]] is not None and HOLE_START <= tv[FIX["DIG_BURIED"][0]] <= HOLE_START + 24)
        cl2 = rig.start_client("d3b", port, r1)
        check("D3 a NEW real client process booted", cl2 is not None)
        if cl2 is not None:
            m = cl2.wait_for_log(r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=\d+ kind=FULL .* pockets=([0-9A-F,]+)", 60.0)
            check("D3 the new client adopted the host record (rev %d) holding the apple" % rev, m is not None and int(m.group(1)) == rev
                  and tuple(int(x, 16) for x in m.group(2).strip(",").split(",")) == tuple(inv[0]))
            cl2.stop()
        host.stop()

    def _c1():
        port = args.port + 3
        host = rig.start_host("c1", port, ["--authoritative-wildlife"])
        check("C1 host reached genuine field-ready state", host is not None)
        if host is None:
            return
        L.resolve_host_town(ip, port)
        seed = RC.fake_session(port, ip, r2, "seedC1")
        seed.drain_field_updates(timeout=0.3)
        fish = None
        for _ in range(8):
            fish = WG.force_spawn_one_fish(seed, "seed")
            if fish is not None:
                break
        RC.release(seed)
        check("C1 a real FISH was spawned for the claim (RNG-dependent; the seed session left, the entity persists)", fish is not None)
        if fish is None:
            host.stop()
            return
        want_item = L.fish_item_for_species(fish["species"])
        cl = rig.start_client("c1", port, r1, ["--authoritative-wildlife", "--force-fish-catch"])
        check("C1 real client booted into the field and connected", cl is not None)
        if cl is None:
            host.stop()
            return
        m = cl.wait_for_log(r"--force-fish-catch \(client\): query_catch_outcome\(entity (\d+)\) after CATCH_RESULT wait = (-?\d+)", 150.0)
        ct = cl.log_text()
        ht = host.log_text()
        check("C1 the hook's outcome query printed", m is not None)
        check("C1 the exchange-gate outcome read ACCEPTED (2) for the claimed entity %d" % fish["entity_id"],
              m is not None and int(m.group(1)) == fish["entity_id"] and int(m.group(2)) == 2)
        m_sent = re.search(r"TXN grant request sent kind=CATCH request=(\d+) nonce=(\d+) seq=(\d+) dest=1 slot=(\d+) item=0x([0-9A-F]{4}) base=\(epoch (\d+), rev (\d+)\)", ct)
        m_res = re.search(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=CATCH request=(\d+) seq=(\d+) rev=(\d+)", ct)
        m_app = re.search(r"client: APPLIED request (\d+) kind=CATCH -- host post-image: slot (\d+) item 0x([0-9A-F]{4}), wallet now (\d+)", ct)
        check("C1 a CATCH request (dest POCKET, the host-derived fish item 0x%04X) was sent, answered by TXN_RESULT(APPLIED) with the same seq, and applied by "
              "the host post-image into that slot" % want_item,
              m_sent is not None and m_res is not None and m_app is not None and m_sent.group(3) == m_res.group(2)
              and int(m_sent.group(5), 16) == want_item and int(m_app.group(3), 16) == want_item and int(m_sent.group(4)) == int(m_app.group(2)))
        check("C1 order in the client log: request sent < TXN_RESULT APPLIED < pocket write < CATCH accepted line (the pocket is written ONLY on APPLIED)",
              m_sent is not None and m_res is not None and m_app is not None
              and m_sent.start() < m_res.start() < m_app.start() < ct.find("client: CATCH request"))
        check("C1 the host removed the entity exactly once (one accepted line, one TXN APPLIED kind=CATCH) and no CONFLICT / INTERNAL",
              count(r"CATCH entity %d \(species claim \d+\) accepted" % fish["entity_id"], ht) == 1 and count(r"TXN APPLIED kind=CATCH", ht) == 1
              and "CONFLICT" not in ht and "INTERNAL" not in ht)
        check("C1 no legacy client grant (the old 'granted to a free pocket slot' line is absent) and no rejection / violation in the client log",
              "granted to a free pocket slot" not in ct and "REJECTED(" not in ct and "protocol violation" not in ct)
        L.pump_sleep(3.0)
        off = len(host.log_text())
        cl.stop()
        rig.wait_peer_gone(off - 1)
        rev, inv, tv = host_tiles(port, ip, r1, "C1rec", [])
        check("C1 host record (new FakeClient session): the fish item is in the pockets exactly once more than before, nothing else changed",
              list(inv[0]).count(want_item) == list(gci_inv[0]).count(want_item) + 1 and inv[2] == gci_inv[2]
              and sorted(x for x in inv[0] if x) == sorted([x for x in gci_inv[0] if x] + [want_item]))
        host.stop()

    for name, fn in (("d1", _d1), ("d2", _d2), ("d3", _d3), ("c1", _c1)):
        if name in args.only:
            fn()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10850)
    ap.add_argument("--only", default="d1,d2,d3,c1")
    args = ap.parse_args()
    args.only = set(x.strip() for x in args.only.split(",") if x.strip())
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "txn_x3_real_client_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        run(args, results, ip, snap_gci, rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
