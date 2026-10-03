#!/usr/bin/env python3
"""test_ts_real_client.py - Town Services milestone 1, the REAL game client's half: the TOWN_SVC_STATE mirror it receives and applies, one
museum DONATION and one lost-and-found CLAIM driven through the real client transaction code. REAL processes.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0 [--ts-test-seed-police=0x2800,0x2801]`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot> --ts-test-donate | --ts-test-claim`
The client's mirror receive / validate / apply code (pcnetgame_handle_client_town_svc / _ts_client_apply), the UI-seam entry points the Blathers /
Booker dialogues call (pc_net_game_ts_begin_museum_donate / _police_claim / _poll), the TXN_COMMIT build / send, TXN_RESULT handling and the pocket
apply step (pcnetgame_txn_apply_applied) and the host's handler + mirror push all run for real. Assertions come from the processes' own log lines
(order: mirror applied at join < request sent < TXN_RESULT < pocket write < the mirror of the change) and a byte-exact check of the host record and
the host's service mirror through a fresh scripted FakeClient session after the client left.
The only synthetic inputs are TEST-ONLY, default-off hooks (they bypass the dialogue and the menu, never the request / TXN_RESULT chain):
host --ts-test-seed-police, client --ts-test-donate (places one fish into a free pocket slot = "the player caught it", the hook's one local pocket
write, then waits 5 s so the normal D3 upload carries it) and --ts-test-claim.
NOT covered (no UI automation): the Blathers / Booker dialogue state machines themselves (their wait / apply / give-back branches are SOURCE
AUDITED in test_ts_src.py and test_stage0_town_safe_degrade.py), the visual refresh of the museum rooms / the lost-and-found items, the
REJECTED branch with a real client (host side tested with scripted clients in test_ts_protocol.py), and a second real client.

Run ONLY on the disposable pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN=<absolute path>, ports 11100+). The fixture save is snapshotted at start
and restored before every host launch and at the end. One real client at a time.
  R1  museum donation: the client receives BOTH services at join, the hook's donation is begun, answered APPLIED, the fish leaves the pocket ONLY
      then, the museum mirror of the change arrives and the client's local museum says 'already donated'; the host record + museum nibble match
  R2  lost-and-found claim: the client's local lost-and-found mirror holds the seeded items, the claim is answered APPLIED, the item enters the
      pocket ONLY then, the next police mirror (compacted) arrives; the host record holds the item once, the host's police list lost it
Usage: python test_ts_real_client.py [--port 11100] [--only r1,r2]
"""
import argparse
import os
import re
import shutil
import struct
import sys

import net_spike_lib as L
import test_txn_real_client as RC

SEEDS = [0x2800, 0x2801]


def count(rx, text):
    return len(re.findall(rx, text))


def final_state(text):
    m = re.search(r"--ts-test: final pockets=([0-9A-F,]+) police=([0-9A-F,]+) wallet=(\d+)", text)
    if m is None:
        return None
    return (tuple(int(x, 16) for x in m.group(1).strip(",").split(",")), tuple(int(x, 16) for x in m.group(2).strip(",").split(",")), int(m.group(3)))


def run(args, results, ip, snap_gci, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    r1 = residents[0]
    gci_inv = L.record_inventory(L.record_from_gci(snap_gci, r1))

    def peek_host_state(port, label):
        f = RC.fake_session(port, ip, r1, label)
        push = f.rec_pushes[-1]
        mus = f.wait_ts_state(L.PC_NETGAME_TS_MUSEUM, 4.0)
        pol = f.wait_ts_state(L.PC_NETGAME_TS_POLICE, 4.0)
        out = (L.record_inventory(push["data"]), mus, pol)
        RC.release(f)
        return out

    def _r1():
        port = args.port
        host = rig.start_host("r1", port, [])
        check("R1 host reached genuine field-ready state", host is not None)
        if host is None:
            return
        L.resolve_host_town(ip, port)
        cl = rig.start_client("r1", port, r1, ["--ts-test-donate"])
        check("R1 real client booted into the field and connected", cl is not None)
        if cl is None:
            host.stop()
            return
        m = cl.wait_for_log(r"--ts-test-donate: final item 0x([0-9A-F]{4}) museum_info=(\d+)", 150.0)
        check("R1 the hook ran to its final report", m is not None)
        ct = cl.log_text()
        ht = host.log_text()
        off = len(ht)
        cl.stop()
        rig.wait_peer_gone(off - 1)
        if m is None:
            host.stop()
            return
        item = int(m.group(1), 16)
        check("R1 the client applied BOTH service mirrors at join (police seq >= 1, museum seq >= 1) before the hook placed anything",
              count(r"\[NET\]\[TS\] client: applied service 1 \(POLICE\) seq \d+ digest 0x[0-9A-F]{8} len 40", ct) >= 1
              and count(r"\[NET\]\[TS\] client: applied service 2 \(MUSEUM\) seq \d+ digest 0x[0-9A-F]{8} len 63", ct) >= 1
              and ct.find("applied service 2 (MUSEUM)") < ct.find("--ts-test-donate: placed fish item"))
        m_place = re.search(r"--ts-test-donate: placed fish item 0x([0-9A-F]{4}) into pocket slot (\d+)", ct)
        m_sent = re.search(r"TXN_COMMIT sent kind=MUSEUM_DONATE request=(\d+) nonce=(\d+) seq=(\d+) dest=0 slot=(\d+) item=0x([0-9A-F]{4}) base=\(epoch (\d+), rev (\d+)\)", ct)
        m_res = re.search(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=MUSEUM_DONATE request=(\d+) seq=(\d+) rev=(\d+)", ct)
        m_app = re.search(r"client: APPLIED request (\d+) kind=MUSEUM_DONATE -- (.*?): slot (\d+) item 0x0000, wallet now (\d+)", ct)
        check("R1 the donation of the placed fish (0x%04X) was sent as MUSEUM_DONATE (dest NONE, the slot it sat in), answered by TXN_RESULT(APPLIED) with the same seq and applied "
              "by the host post-image (the slot is EMPTY)" % item,
              m_place is not None and m_sent is not None and m_res is not None and m_app is not None and int(m_place.group(1), 16) == item
              and int(m_sent.group(5), 16) == item and int(m_sent.group(4)) == int(m_place.group(2)) == int(m_app.group(3)) and m_sent.group(3) == m_res.group(2)
              and int(m_res.group(3)) > int(m_sent.group(7)))
        m_mus2 = None
        if m_res is not None:
            for mm in re.finditer(r"\[NET\]\[TS\] client: applied service 2 \(MUSEUM\) seq (\d+)", ct):
                if mm.start() > m_res.start():
                    m_mus2 = mm
                    break
        check("R1 order in the client log: mirrors at join < fish placed < request sent < TXN_RESULT APPLIED < pocket write < the museum mirror of the donation",
              all(x is not None for x in (m_place, m_sent, m_res, m_app, m_mus2)) and m_place.start() < m_sent.start() < m_res.start() < m_app.start() < m_mus2.start())
        check("R1 the pocket changed ONLY on APPLIED (the hook's invariant check never fired) and nothing was rejected / resent / inconsistent",
              "POCKET CHANGED BEFORE APPLIED" not in ct and "REJECTED(" not in ct and "resending the identical" not in ct and "delta impossible" not in ct
              and "inconsistent post-image" not in ct and "protocol violation" not in ct)
        check("R1 the client's LOCAL museum (written by the mirror, never by a local commit) says 'already donated' (2) for the donated fish", int(m.group(2)) == 2)
        fs = final_state(ct)
        check("R1 the client's final pockets no longer hold the fish", fs is not None and item not in fs[0])
        check("R1 host: exactly one 'TXN APPLIED kind=MUSEUM_DONATE' + one committed line (donor slot %d), no CONFLICT / INTERNAL" % (r1 + 1),
              count(r"TXN APPLIED kind=MUSEUM_DONATE ", ht) == 1 and count(r"MUSEUM_DONATE item=0x%04X slot=\d+ committed \(donor slot %d\)" % (item, r1 + 1), ht) == 1
              and "CONFLICT" not in ht and "INTERNAL" not in ht)
        inv, mus, pol = peek_host_state(port, "R1rec")
        idx = item - 0x2300
        nibble = None if mus is None else (mus[1][0x15 + (idx >> 1)] >> ((idx & 1) * 4)) & 0xF
        check("R1 host (new FakeClient session): the museum mirror carries the donor nibble slot + 1 (%d, observed %s) and the host record's pockets do not hold the fish; the fish "
              "left the pockets once" % (r1 + 1, nibble), nibble == r1 + 1 and list(inv[0]).count(item) == 0 and list(gci_inv[0]).count(item) == 0
              and fs is not None and tuple(inv[0]) == fs[0])
        host.stop()

    def _r2():
        port = args.port + 1
        host = rig.start_host("r2", port, ["--ts-test-seed-police=" + ",".join("0x%04X" % s for s in SEEDS)])
        check("R2 host reached genuine field-ready state", host is not None)
        if host is None:
            return
        L.resolve_host_town(ip, port)
        cl = rig.start_client("r2", port, r1, ["--ts-test-claim"])
        check("R2 real client booted into the field and connected", cl is not None)
        if cl is None:
            host.stop()
            return
        m = cl.wait_for_log(r"--ts-test-claim: final item 0x([0-9A-F]{4}) museum_info=(\d+)", 150.0)
        check("R2 the hook ran to its final report", m is not None)
        ct = cl.log_text()
        ht = host.log_text()
        off = len(ht)
        cl.stop()
        rig.wait_peer_gone(off - 1)
        if m is None:
            host.stop()
            return
        item = int(m.group(1), 16)
        m_pre = re.search(r"--ts-test: stage 0 setup done pockets=[0-9A-F,]+ police=([0-9A-F,]+) wallet=", ct)
        pre_pol = [int(x, 16) for x in m_pre.group(1).strip(",").split(",")] if m_pre else []
        first_nz = next((x for x in pre_pol if x), None)
        check("R2 the claimed item is the FIRST non-empty entry (0x%04X) of the client's local lost-and-found mirror, which holds both seeded items (the fixture town already "
              "had items in its lost and found)" % item, item != 0 and item == first_nz and all(s in pre_pol for s in SEEDS))
        m_sent = re.search(r"TXN_COMMIT sent kind=POLICE_CLAIM request=(\d+) nonce=(\d+) seq=(\d+) dest=1 slot=(\d+) item=0x([0-9A-F]{4}) base=\(epoch (\d+), rev (\d+)\)", ct)
        m_res = re.search(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=POLICE_CLAIM request=(\d+) seq=(\d+) rev=(\d+)", ct)
        m_app = re.search(r"client: APPLIED request (\d+) kind=POLICE_CLAIM -- (.*?): slot (\d+) item 0x([0-9A-F]{4}), wallet now (\d+)", ct)
        check("R2 a POLICE_CLAIM (dest POCKET, a free slot, the EXPECTED item 0x%04X) was sent, answered by TXN_RESULT(APPLIED) with the same seq and applied by the host post-image "
              "into that slot" % item,
              m_sent is not None and m_res is not None and m_app is not None and int(m_sent.group(5), 16) == item and int(m_app.group(4), 16) == item
              and int(m_sent.group(4)) == int(m_app.group(3)) and m_sent.group(3) == m_res.group(2) and int(m_res.group(3)) > int(m_sent.group(7)))
        m_first = re.search(r"\[NET\]\[TS\] client: applied service 1 \(POLICE\) seq (\d+)", ct)
        m_pol2 = None
        if m_res is not None:
            for mm in re.finditer(r"\[NET\]\[TS\] client: applied service 1 \(POLICE\) seq (\d+)", ct):
                if mm.start() > m_res.start():
                    m_pol2 = mm
                    break
        check("R2 order in the client log: the police mirror at join < request sent < TXN_RESULT APPLIED < pocket write < the next police mirror (the claim's)",
              all(x is not None for x in (m_first, m_sent, m_res, m_app, m_pol2)) and m_first.start() < m_sent.start() < m_res.start() < m_app.start() < m_pol2.start()
              and m_pol2.group(1) != m_first.group(1))
        check("R2 the pocket changed ONLY on APPLIED and nothing was rejected / resent / inconsistent",
              "POCKET CHANGED BEFORE APPLIED" not in ct and "REJECTED(" not in ct and "resending the identical" not in ct and "delta impossible" not in ct
              and "inconsistent post-image" not in ct and "protocol violation" not in ct)
        fs = final_state(ct)
        check("R2 the client's final pockets hold the item once in the claimed slot, and its LOCAL lost-and-found (written only by the mirror) lost it (police=%s)"
              % (None if fs is None else ",".join("%04X" % x for x in fs[1])),
              fs is not None and list(fs[0]).count(item) == list(gci_inv[0]).count(item) + 1 and item not in fs[1] and SEEDS[1] in fs[1])
        check("R2 host: exactly one 'TXN APPLIED kind=POLICE_CLAIM' + one committed line, no CONFLICT / INTERNAL",
              count(r"TXN APPLIED kind=POLICE_CLAIM ", ht) == 1 and count(r"POLICE_CLAIM item=0x%04X lost-and-found slot=\d+ -> pocket slot \d+ committed" % item, ht) == 1
              and "CONFLICT" not in ht and "INTERNAL" not in ht)
        inv, mus, pol = peek_host_state(port, "R2rec")
        pl = list(struct.unpack("<20H", pol[1])) if pol else []
        check("R2 host (new FakeClient session): the pushed record holds the item exactly once more than before, the police mirror lost it (and kept the second seed, compacted)",
              list(inv[0]).count(item) == list(gci_inv[0]).count(item) + 1 and item not in pl and SEEDS[1] in pl and fs is not None and tuple(inv[0]) == fs[0])
        host.stop()

    for name, fn in (("r1", _r1), ("r2", _r2)):
        if name in args.only:
            fn()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11100)
    ap.add_argument("--only", default="r1,r2")
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
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "ts_real_client_save_snapshot_%d" % os.getpid())
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
