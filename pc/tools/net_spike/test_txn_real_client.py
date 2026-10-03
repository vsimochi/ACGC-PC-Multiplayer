#!/usr/bin/env python3
"""test_txn_real_client.py - X1b: the REAL game client's half of the host-transactional pickup/drop/bury commit, REAL processes.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0 [--txn-fault=<mode>]`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot> --txn-test-pickup-drop` (a NEW process per run)
The client's TXN_COMMIT build / send / resend / apply C code and the host's journal + execution run for real. Every assertion comes from
the processes' own log lines plus byte-exact checks of the host resident record and the host world through scripted FakeClient sessions
(a new FakeClient session for the resident receives the host PUSH_FULL of the mirror and its join snapshot of the world).
The only synthetic inputs are the TEST-ONLY, default-off hooks: client --txn-test-pickup-drop (one real request_drop() of the first
droppable pocket item at the tile vanilla's drop search picks, then, after its transaction resolved, a real request_pickup() of it from
that tile; it bypasses only the inventory UI / animation input), host --txn-fault=kill_peer_after_commit / drop_result.
NOT covered here (no UI automation): the inventory overlay actually staying shut while a transaction is pending (the lock is an AND into
mPlib_able_submenu_type1(), source-audited by test_txn_src.py), the pickup animation, a real bury UI flow.

Run ONLY on the disposable pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN=<absolute path>, ports 10700+). The fixture save is
snapshotted at start and restored before every host launch (and the client's local GCI is then the original one) and at the end.
At most 1 real client (+ a short-lived FakeClient session for the record + world check after it left) at a time.

  R1  the real client's DROP transaction then PICKUP transaction (no fault): the client sends TXN_COMMIT (never CONFIRM(COMMIT)), the
      pocket changes ONLY on the host's APPLIED (log order + 'host post-image' = the local inventory still equalled the pre-image at
      apply time, i.e. nothing mutated it in between), the host record + world change exactly once each, the final host record equals
      the original (item neither duplicated nor lost) and the tile is empty again; D3 base never regresses.
  R2  abrupt kill: host --txn-fault=kill_peer_after_commit drops the peer right after the DROP commit (no RESULT) and the client process
      is killed; a NEW client process for that resident adopts the host record (the item is gone from the pockets and sits on the tile
      exactly once: neither duplicated nor lost); the first client never mutated its pockets (no APPLIED).
  R3  lost RESULT: host --txn-fault=drop_result swallows the first APPLIED: the client resends the IDENTICAL COMMIT after ~2 s, the host
      replays the journalled APPLIED (no second execution, no CONFLICT) and the client applies exactly once; the whole drop+pickup
      pair then completes and the host record equals the original.
  R4  wallet route: host --field-action-test-seed --force-money-rock-hit drops a money bag, the real client (--force-money-bag-pickup, which
      moves it onto the bag and calls the real request_pickup()) commits it as a WALLET transaction (dest 2, slot 0xFF): the wallet is credited
      ONLY on APPLIED (host post-image), exactly once, the host record wallet equals the client's, the pockets are untouched, the tile is empty.
Usage: python test_txn_real_client.py [--port 10700]
"""
import argparse
import os
import re
import shutil
import sys
import time

import net_spike_lib as L

SAVE_DIR_REL = "save"
HERE = os.path.dirname(os.path.abspath(__file__))


def count(rx, text):
    return len(re.findall(rx, text))


class Rig:
    def __init__(self, port, results, save_dir, snap_dir):
        self.port, self.results, self.save_dir, self.snap_dir = port, results, save_dir, snap_dir
        self.check = lambda d, c: L.check(d, c, results)
        self.host = None

    def restore_save(self):
        shutil.rmtree(self.save_dir, ignore_errors=True)
        shutil.copytree(self.snap_dir, self.save_dir)

    def start_host(self, tag, port, extra=()):
        self.restore_save()
        for i in range(3):
            h = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"] + list(extra),
                              log_path=os.path.join(HERE, "txn_real_%s_host_try%d.log" % (tag, i)), bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
                self.host = h
                return h
            h.stop()
            time.sleep(2.0)
        self.host = None
        return None

    def start_client(self, tag, port, slot, extra=()):
        for i in range(3):
            c = L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--bootstrap-resident", str(slot)] + list(extra),
                                log_path=os.path.join(HERE, "txn_real_%s_try%d.log" % (tag, i)), bin_dir=L.GAME_BIN_DIR,
                                label=tag).start()
            if c.boot_to_field(timeout=90.0, slot=slot):
                return c
            c.stop()
            time.sleep(2.0)
        return None

    def wait_peer_gone(self, off, timeout=16.0):
        return self.host.wait_for_log(r"\[NET\] host: peer \d+ disconnected", timeout, since_offset=off) is not None


def fake_session(port, ip, slot, label):
    c = L.FakeClient(label, ip, port, player=L.resident_player(slot))
    c.rec_resident_idx = slot
    c.connect_and_ready(quiet=True)
    return c


def release(c):
    try:
        if c.state in (c.STATE_CONNECTED, c.STATE_PENDING):
            c.disconnect()
    except Exception:  # noqa: BLE001
        pass
    c.close()
    L.pump_sleep(0.8)


def host_view(port, ip, slot, label, tile):
    """A new FakeClient session for the resident: the host's mirror as the PUSH_FULL it receives AND the host world (its join snapshot) at
    `tile` -> (rev, epoch, (pockets, conds, wallet), tile value). (A FakeClient that is not pumped is timed out by the host, so there is
    no long-lived observer: the world is read from a fresh snapshot, the transitions from the host's own commit log lines.)"""
    f = fake_session(port, ip, slot, label)
    push = f.rec_pushes[-1]
    inv = L.record_inventory(push["data"])
    info = (push["rev"], push["epoch"], inv, f.world.tile_ut(*tile))
    release(f)
    return info


def pos(text, rx, start=0):
    m = re.compile(rx).search(text, start)
    return -1 if m is None else m.start()


def run_drop_pickup_flow(rig, tag, port, ip, r1, r2, gci_inv, host_extra=(), client_extra=("--txn-test-pickup-drop",)):
    """Boots a host + a real client with the hook; returns (host, client) or None."""
    check = rig.check
    host = rig.start_host(tag, port, host_extra)
    check("%s host reached genuine field-ready state" % tag, host is not None)
    if host is None:
        return None
    L.resolve_host_town(ip, port)
    cl = rig.start_client(tag, port, r1, list(client_extra))
    check("%s real client booted into the field and connected" % tag, cl is not None)
    if cl is None:
        return None
    return host, cl


def parse_drop(ct):
    m = re.search(r"--txn-test-pickup-drop: DROP of pocket slot (\d+) \(item 0x([0-9A-F]{4})\) at tile \((\d+),(\d+)\)", ct)
    return None if m is None else (int(m.group(1)), int(m.group(2), 16), int(m.group(3)), int(m.group(4)))


def run(args, results, ip, snap_gci, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has two non-host residents (slots %s)" % residents, len(residents) >= 2)
    if len(residents) < 2:
        return
    r1, r2 = residents[0], residents[1]
    gci_inv = L.record_inventory(L.record_from_gci(snap_gci, r1))
    check("fixture resident %d holds items in its pockets %s" % (r1, [hex(p) for p in gci_inv[0] if p]),
          any(p != 0 for p in gci_inv[0]))

    def _sc_r1():
        # ============================ R1: real drop + pickup transactions ============================
        port = args.port
        t = run_drop_pickup_flow(rig, "r1", port, ip, r1, r2, gci_inv)
        if t is None:
            return
        host, cl = t
        m = cl.wait_for_log(r"\[NET\]\[TXN\] client: APPLIED request \d+ kind=PICKUP", 90.0)
        ct = cl.log_text()
        check("R1 the real client performed the drop and the pickup transactions (both APPLIED)",
              m is not None and re.search(r"client: APPLIED request \d+ kind=DROP", ct) is not None)
        d = parse_drop(ct)
        check("R1 test hook picked a droppable pocket item and a tile", d is not None)
        if d is None:
            cl.stop()
            host.stop()
            return
        slot_d, item_d, ux, uz = d
        check("R1 hook dropped the item the fixture resident holds in that slot (0x%04X)" % item_d, gci_inv[0][slot_d] == item_d)
        p_cd = pos(ct, r"TXN_COMMIT sent kind=DROP request=(\d+) nonce=(\d+) seq=(\d+) dest=0 slot=%d item=0x%04X" % (slot_d, item_d))
        p_rd = pos(ct, r"TXN_RESULT APPLIED kind=DROP")
        p_ad = pos(ct, r"client: APPLIED request \d+ kind=DROP -- host post-image: slot %d item 0x0000" % slot_d)
        check("R1 DROP order in the client log: COMMIT sent < TXN_RESULT APPLIED < pocket write (the slot is cleared ONLY on APPLIED)",
              0 <= p_cd < p_rd < p_ad)
        check("R1 DROP apply used the exact host post-image (the local inventory still equalled the pre-image: nothing mutated it "
              "between the COMMIT and the RESULT)", p_ad >= 0 and "delta (the local inventory changed" not in ct)
        check("R1 no legacy path: the client never queued CONFIRM(COMMIT) / cleared the slot synchronously (old log lines absent)",
              "CONFIRM(COMMIT) sent" not in ct and "cleared pocket slot" not in ct and "CONFIRM could not be queued" not in ct)
        mc = re.search(r"TXN_COMMIT sent kind=DROP request=(\d+) nonce=(\d+) seq=(\d+) .* base=\(epoch (\d+), rev (\d+)\)", ct)
        mr = re.search(r"TXN_RESULT APPLIED kind=DROP request=(\d+) seq=(\d+) rev=(\d+) epoch=(\d+)", ct)
        check("R1 DROP: result echoes the COMMIT's request/seq, the epoch is unchanged and the host rev advanced past the base",
              mc is not None and mr is not None and mc.group(1) == mr.group(1) and mc.group(3) == mr.group(2)
              and mc.group(4) == mr.group(4) and int(mr.group(3)) > int(mc.group(5)))
        mcp = re.search(r"TXN_COMMIT sent kind=PICKUP request=(\d+) nonce=(\d+) seq=(\d+) dest=1 slot=(\d+) item=0x%04X base=\(epoch (\d+), rev (\d+)\)" % item_d, ct)
        mrp = re.search(r"TXN_RESULT APPLIED kind=PICKUP request=(\d+) seq=(\d+) rev=(\d+)", ct)
        check("R1 PICKUP: a pocket-destination COMMIT for the dropped item, answered APPLIED; seq is monotone (drop seq < pickup seq), "
              "the same process nonce; the D3 base never regressed (pickup base rev >= the drop's APPLIED rev)",
              mcp is not None and mrp is not None and mr is not None and int(mcp.group(3)) == int(mc.group(3)) + 1
              and mcp.group(2) == mc.group(2) and int(mcp.group(6)) >= int(mr.group(3)) and int(mrp.group(3)) > int(mr.group(3)))
        check("R1 PICKUP apply used the exact host post-image into the freed slot",
              re.search(r"client: APPLIED request \d+ kind=PICKUP -- host post-image: slot %d item 0x%04X" % (slot_d, item_d), ct) is not None)
        check("R1 exactly 2 TXN_COMMITs sent, exactly 2 applies, no resend, no rejection/delta/inconsistency in the client log",
              count(r"TXN_COMMIT sent ", ct) == 2 and count(r"client: APPLIED request ", ct) == 2 and "resending the identical" not in ct
              and "REJECTED(" not in ct and "delta impossible" not in ct and "inconsistent post-image" not in ct)
        ht = host.log_text()
        check("R1 host: DROP and PICKUP each committed exactly once through the TXN path (world tile written once each: 0x0000 -> item, item -> 0x0000)",
              count(r"\[NET\]\[DROP\] host: peer \d+ request \d+ committed tile \(%d,%d\) 0x0000 -> 0x%04X \[TXN\]" % (ux, uz, item_d), ht) == 1
              and count(r"\[NET\]\[PICKUP\] host: peer \d+ request \d+ committed tile \(%d,%d\) 0x%04X -> 0x0000 \[TXN\]" % (ux, uz, item_d), ht) == 1)
        check("R1 host: two TXN APPLIED lines (DROP, PICKUP) for this resident, rev strictly increasing; legacy CONFIRM(COMMIT) never seen; no CONFLICT/INTERNAL",
              count(r"TXN APPLIED kind=DROP", ht) == 1 and count(r"TXN APPLIED kind=PICKUP", ht) == 1
              and "CONFIRM(COMMIT) is retired" not in ht and "CONFLICT" not in ht and "INTERNAL" not in ht)
        L.pump_sleep(3.5)
        ct = cl.log_text()
        check("R1 no violation / BAD DIGEST / ADOPT_FAILED / refusal in the client log",
              "protocol violation" not in ct and "BAD DIGEST" not in ct and "ADOPT_FAILED" not in ct and "REFUSED record transfer" not in ct)
        off = len(host.log_text())
        cl.stop()
        check("R1 host noticed the client leaving", rig.wait_peer_gone(off - 1))
        rev, epoch, inv, tv = host_view(port, ip, r1, "R1rec", (ux, uz))
        check("R1 host world (fresh join snapshot): tile (%d,%d) is empty again (observed %s)" % (ux, uz, None if tv is None else hex(tv)),
              tv == 0)
        check("R1 host record (new FakeClient session): pockets %s == the original (the item is back in slot %d exactly once), wallet unchanged"
              % ([hex(p) for p in inv[0] if p], slot_d), inv[0] == gci_inv[0] and inv[2] == gci_inv[2])
        check("R1 the item appears exactly once in the host record pockets", list(inv[0]).count(item_d) == 1)
        host.stop()

    def _sc_r2():
        # ============================ R2: abrupt client kill right after the COMMIT ============================
        port = args.port + 1
        t = run_drop_pickup_flow(rig, "r2", port, ip, r1, r2, gci_inv, host_extra=["--txn-fault=kill_peer_after_commit"])
        if t is None:
            return
        host, cl = t
        m = host.wait_for_log(r"\[NET\]\[TXN\]\[TEST-ONLY\] kill_peer_after_commit: peer \d+ dropped AFTER the commit", 90.0)
        check("R2 host fired kill_peer_after_commit after executing the real client's DROP COMMIT", m is not None)
        killed_at = time.monotonic()
        cl.proc.kill()                      # the abrupt kill of the client process
        cl.proc.wait(10)
        ct = cl.log_text()
        d = parse_drop(ct)
        check("R2 the client had sent its TXN_COMMIT (kind=DROP) before it died", d is not None and "TXN_COMMIT sent kind=DROP" in ct)
        check("R2 the first client never mutated its pockets: no APPLIED line (it never got a RESULT) and no legacy clear",
              "client: APPLIED request" not in ct and "cleared pocket slot" not in ct)
        slot_d, item_d, ux, uz = d if d else (0, 0, 0, 0)
        ht = host.log_text()
        check("R2 host: the DROP committed exactly once (tile (%d,%d) 0x0000 -> 0x%04X) and journalled APPLIED, no RESULT sent" % (ux, uz, item_d),
              count(r"\[NET\]\[DROP\] host: peer \d+ request \d+ committed tile \(%d,%d\) 0x0000 -> 0x%04X \[TXN\]" % (ux, uz, item_d), ht) == 1
              and count(r"TXN APPLIED kind=DROP", ht) == 0)  # the APPLIED line is printed by the RESULT sender, which the fault skips
        L.pump_sleep(2.0)
        rev, epoch, inv, tv = host_view(port, ip, r1, "R2rec", (ux, uz))
        exp = tuple(0 if i == slot_d else p for i, p in enumerate(gci_inv[0]))
        check("R2 host record after the kill (new FakeClient session): the item left the pockets (slot %d empty), the rest unchanged" % slot_d,
              inv[0] == exp and inv[2] == gci_inv[2])
        check("R2 host world (fresh join snapshot): the dropped item sits on tile (%d,%d) (observed %s): present on the tile exactly once"
              % (ux, uz, None if tv is None else hex(tv)), tv == item_d)
        cl2 = rig.start_client("r2b", port, r1)         # a NEW real client process for the same resident, no hook
        check("R2 second real client process booted", cl2 is not None)
        if cl2 is not None:
            m = cl2.wait_for_log(r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=\d+ kind=FULL .* pockets=([0-9A-F,]+)", 60.0)
            check("R2 the new client adopted the host record (rev %d)" % rev, m is not None and int(m.group(1)) == rev)
            if m is not None:
                adopted = tuple(int(x, 16) for x in m.group(2).split(","))
                check("R2 adopted pockets == the host record (the item is NOT duplicated: absent from the pockets, present once on the tile; not "
                      "lost: the tile holds it)", adopted == exp and item_d not in adopted)
            ct2 = cl2.log_text()
            check("R2 second client: HELLO have_last=0 (new process), no txn activity, no violation",
                  "HELLO sent (have_last=0" in ct2 and "TXN_COMMIT" not in ct2 and "protocol violation" not in ct2)
            cl2.stop()
        host.stop()

    def _sc_r3():
        # ============================ R3: lost RESULT -> identical resend ============================
        port = args.port + 2
        t = run_drop_pickup_flow(rig, "r3", port, ip, r1, r2, gci_inv, host_extra=["--txn-fault=drop_result"])
        if t is None:
            return
        host, cl = t
        m = cl.wait_for_log(r"\[NET\]\[TXN\] client: APPLIED request \d+ kind=PICKUP", 90.0)
        ct = cl.log_text()
        ht = host.log_text()
        check("R3 both transactions completed on the real client despite the lost RESULT", m is not None
              and re.search(r"client: APPLIED request \d+ kind=DROP", ct) is not None)
        d = parse_drop(ct)
        slot_d, item_d, ux, uz = d if d else (0, 0, 0, 0)
        check("R3 host fired drop_result once (the first APPLIED RESULT was swallowed)",
              count(r"drop_result: TXN_RESULT APPLIED for seq \d+ NOT sent", ht) == 1)
        check("R3 client resent the COMMIT after ~2 s (resend 1 logged for the DROP) and the SENT line appears once (identical bytes: one build)",
              re.search(r"resending the identical COMMIT \(resend 1\)", ct) is not None and count(r"TXN_COMMIT sent kind=DROP", ct) == 1)
        check("R3 host replayed the journalled APPLIED for the resend (no second execution), no CONFLICT (the resend was byte-identical)",
              count(r"TXN replay APPLIED kind=DROP", ht) == 1 and "CONFLICT" not in ht
              and count(r"\[NET\]\[DROP\] host: peer \d+ request \d+ committed tile \(%d,%d\) 0x0000 -> 0x%04X \[TXN\]" % (ux, uz, item_d), ht) == 1)
        check("R3 client applied the DROP exactly once, from the replayed APPLIED (the post-image path), and the pickup completed too",
              count(r"client: APPLIED request \d+ kind=DROP", ct) == 1 and "TXN_RESULT APPLIED (replayed) kind=DROP" in ct
              and count(r"client: APPLIED request \d+ kind=PICKUP", ct) == 1)
        L.pump_sleep(3.0)
        off = len(host.log_text())
        cl.stop()
        rig.wait_peer_gone(off - 1)
        rev, epoch, inv, tv = host_view(port, ip, r1, "R3rec", (ux, uz))
        check("R3 host record == the original (the item exactly once: no duplicate from the resend, none lost)",
              inv[0] == gci_inv[0] and inv[2] == gci_inv[2] and list(inv[0]).count(item_d) == 1)
        check("R3 host world (fresh join snapshot): the tile is empty again (observed %s); the host executed the drop once (single commit line above)"
              % (None if tv is None else hex(tv)), tv == 0)
        host.stop()


    def _sc_r4():
        # ============================ R4: money bag -> WALLET transaction ============================
        port = args.port + 3
        t = run_drop_pickup_flow(rig, "r4", port, ip, r1, r2, gci_inv, host_extra=["--field-action-test-seed", "--force-money-rock-hit"],
                                 client_extra=["--force-money-bag-pickup"])
        if t is None:
            return
        host, cl = t
        m = cl.wait_for_log(r"\[NET\]\[TXN\] client: APPLIED request \d+ kind=PICKUP", 120.0)
        ct = cl.log_text()
        ht = host.log_text()
        check("R4 the real client's bag pickup transaction was APPLIED", m is not None)
        mf = re.search(r"--force-money-bag-pickup active: forcing a real pickup attempt at tile \((\d+),(\d+)\) \(item=0x([0-9A-F]{4})\) wallet=(\d+)", ct)
        check("R4 the hook found a money bag on the field and requested its pickup (wallet %s before)" % (mf.group(4) if mf else None), mf is not None)
        if mf is None:
            cl.stop()
            return
        bx, bz, bag, w0 = int(mf.group(1)), int(mf.group(2)), int(mf.group(3), 16), int(mf.group(4))
        mc = re.search(r"TXN_COMMIT sent kind=PICKUP request=(\d+) nonce=\d+ seq=(\d+) dest=2 slot=255 item=0x%04X base=\(epoch \d+, rev (\d+)\)" % bag, ct)
        mr = re.search(r"TXN_RESULT APPLIED kind=PICKUP request=(\d+) seq=(\d+) rev=(\d+)", ct)
        ma = re.search(r"client: APPLIED request \d+ kind=PICKUP -- host post-image: slot 255 item 0x0000, wallet now (\d+)", ct)
        check("R4 the COMMIT is a WALLET transaction (dest 2, slot 0xFF, item = the bag) answered APPLIED with the same seq; the wallet is written by the host post-image",
              mc is not None and mr is not None and ma is not None and mc.group(2) == mr.group(2) and int(mr.group(3)) > int(mc.group(3)))
        w1 = int(ma.group(1)) if ma else -1
        check("R4 the wallet was credited exactly once with a money-bag amount (%d -> %d)" % (w0, w1),
              w1 - w0 in (100, 1000, 10000, 30000, 50, 20, 200, 500) and count(r"client: APPLIED request \d+ kind=PICKUP", ct) == 1)
        check("R4 order in the client log: the request was sent with the OLD wallet, the credit appears only after TXN_RESULT APPLIED",
              pos(ct, r"forcing a real pickup attempt") < pos(ct, r"TXN_COMMIT sent kind=PICKUP") < pos(ct, r"TXN_RESULT APPLIED kind=PICKUP") < pos(ct, r"host post-image: slot 255"))
        check("R4 host: the bag tile (%d,%d) committed exactly once through the TXN path (0x%04X -> 0x0000), no legacy CONFIRM(COMMIT), no CONFLICT"
              % (bx, bz, bag), count(r"\[NET\]\[PICKUP\] host: peer \d+ request \d+ committed tile \(%d,%d\) 0x%04X -> 0x0000 \[TXN\]" % (bx, bz, bag), ht) == 1
              and "CONFIRM(COMMIT) is retired" not in ht and "CONFLICT" not in ht)
        L.pump_sleep(3.0)
        off = len(host.log_text())
        cl.stop()
        rig.wait_peer_gone(off - 1)
        rev, epoch, inv, tv = host_view(port, ip, r1, "R4rec", (bx, bz))
        check("R4 host record (new FakeClient session): wallet %d == the client's, pockets unchanged (the bag went to the wallet, not a pocket)" % w1,
              inv[2] == w1 and inv[0] == gci_inv[0])
        check("R4 host world: the bag tile is empty (observed %s)" % (None if tv is None else hex(tv)), tv == 0)
        host.stop()

    if "r1" in args.only:
        _sc_r1()
    if "r2" in args.only:
        _sc_r2()
    if "r3" in args.only:
        _sc_r3()
    if "r4" in args.only:
        _sc_r4()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10700)
    ap.add_argument("--only", default="r1,r2,r3,r4", help="comma list of scenarios to run (r1,r2,r3,r4)")
    args = ap.parse_args()
    args.only = set(x.strip() for x in args.only.split(",") if x.strip())
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "txn_real_client_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    rig = Rig(args.port, results, save_dir, snap_dir)
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
