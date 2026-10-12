#!/usr/bin/env python3
r"""test_dig_tree_real.py - worker 8 / A5: (1) REAL client digs a buried item (APPLIED -> '[NET][DIG] present pocket anim' once), (2) a REJECTED dig (client-local fake deposit the host
does not have) never plays it, (3) TREE_DROP (77): a FakeClient shakes the seeded fruit tree -> the REAL client spawns the falling visual; malformed 77 fed to the real client handler
(autopilot 'treedrop') spawn nothing. Host = REAL, --field-action-test-seed (buried apple at (40,104), fruit tree (56,104)).
Logs come from g_pc_verbose-gated prints ('[NET][DIG]', '[NET][TREE]'). Env W8_FIXTURE (default bin_fixture4_w8)."""
import argparse
import os
import re
import struct
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as TS  # noqa: E402

FIXTURE = os.environ.get("W8_FIXTURE", "bin_fixture4_w8")
BIN = os.path.join(TS.BUILD64, FIXTURE)
os.environ["NET_SPIKE_GAME_BIN"] = BIN
import net_spike_lib as L  # noqa: E402
import wplay_lib as W  # noqa: E402

W.NAME = FIXTURE
W.BIN = BIN
ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=16820)
args = ap.parse_args()
results = []


def check(d, c):
    L.check(d, bool(c), results)


def send_auto(label, line, cid):
    with open(os.path.join(BIN, "ac_auto_%s.txt" % label), "w") as f:
        f.write("%d %s\n" % (cid, line))


def tdrops(log, off=0):
    return re.findall(r"\[NET\]\[TREE\] tree drop present item 0x([0-9A-F]+) tree\((\d+),(\d+)\) drop\((\d+),(\d+)\) idx (\d+) spawned=(\d)", log[off:])


def main():
    res = {i: ident for i, ident, ex in L.read_test_save_residents(BIN) if ex}
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "--field-action-test-seed", "-debug"], bin_dir=BIN, log_path=os.path.join(HERE, "w8_dig_host.log")).start()
    ok = host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)
    check("host in the field", ok)
    if not ok:
        host.stop()
        return
    cli = a = None
    try:
        host.wait_for_log(r"buried fixture item placed at tile \(40,104\)", 60)
        cli = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", "1", "-debug"], bin_dir=BIN, log_path=os.path.join(HERE, "w8_dig_client.log"),
                              label="w8d", env={"AC_TEST_AUTOPILOT": "ac_auto_C.txt"}).start()
        check("real client in the field", cli.boot_to_field(timeout=180, slot=1))
        pc = W.Player(L, "C", cli)
        a = L.FakeClient("A", "127.0.0.1", args.port, player=res[2])
        a.connect_and_ready()
        L.pump_sleep(1.5)
        cid = 100

        # ---------- TREE DROP ----------
        x0, y0, z0 = L.tile_center(56, 104)
        a.claim_position(x0 + 30.0, y0, z0)
        L.pump_sleep(0.5)
        co, ho = len(cli.log_text()), len(host.log_text())
        a.send_reliable(struct.pack("<BBBBIBBH64x", 29, 3, 56, 104, 16801, 0, 0, 0))
        r = a.inbox.wait_for(lambda m: m.channel == L.CH_RELIABLE and len(m.payload) == 12 and m.payload[0] == 30, 3.0)
        check("T1 FakeClient TREE_SHAKE accepted by the host", r is not None and struct.unpack("<BBBBIHBB", r.payload)[2])
        L.pump_sleep(3.0)
        cd, hd = tdrops(cli.log_text(), co), tdrops(host.log_text(), ho)
        print("[INFO] client tree-drop lines %s | host %s" % (cd, hd))
        check("T1 REAL client spawned the falling visual for each host drop (client %d, host %d, all spawned=1)" % (len(cd), len(hd)),
              len(cd) >= 1 and len(cd) == len(hd) and all(x[6] == "1" for x in cd))

        def feed(desc, idx, tx, tz, dx, dz, item, seq, expect):
            nonlocal cid
            cid += 1
            o = len(cli.log_text())
            send_auto("C", "treedrop %d %d %d %d %d %X %d" % (idx, tx, tz, dx, dz, item, seq), cid)
            got = cli.wait_for_log(r"\[AUTO\] treedrop %d fed" % cid, 10, since_offset=o)
            L.pump_sleep(0.5)
            n = len(tdrops(cli.log_text(), o))
            check("T2 %s -> %s (spawn lines %d)" % (desc, "spawn" if expect else "NO spawn", n), got and n == (1 if expect else 0))

        feed("valid control (apple, adjacent)", 0, 56, 104, 55, 104, 0x2800, 9001, True)
        feed("repeated seq 9001", 0, 56, 104, 55, 104, 0x2800, 9001, False)
        feed("honeycomb item 0x62", 0, 56, 104, 55, 104, 0x0062, 9002, False)
        feed("EMPTY item", 0, 56, 104, 55, 104, 0x0000, 9003, False)
        feed("unknown item 0xFFFF", 0, 56, 104, 55, 104, 0xFFFF, 9004, False)
        feed("index 3", 3, 56, 104, 55, 104, 0x2800, 9005, False)
        feed("tree tile out of the town acres (2,2)", 0, 2, 2, 2, 3, 0x2800, 9006, False)
        feed("drop tile out of the town acres", 0, 16, 104, 14, 104, 0x2800, 9007, False)
        feed("far from the tree (dx 3)", 0, 56, 104, 59, 104, 0x2800, 9008, False)
        feed("far from the tree (dz 3)", 0, 56, 104, 56, 107, 0x2800, 9009, False)
        feed("second valid (distance 2, seq 9010)", 1, 56, 104, 58, 106, 0x2800, 9010, True)

        # ---------- DIG ----------
        a.disconnect()  # a FakeClient left idle times out mid-dig and the host save/record traffic then delays the dig transaction
        L.pump_sleep(3.0)
        cid += 1
        off_give = len(cli.log_text())
        send_auto("C", "give 2202", cid)  # ITM_SHOVEL
        cli.wait_for_log(r"\[AUTO\] give %d" % cid, 10)
        L.pump_sleep(1.0)
        for _ in range(100):  # the pocket change makes the resident record dirty: the D3 record gate blocks dig requests until the upload is APPLIED
            if re.search(r"upload xfer \d+ APPLIED", cli.log_text()[off_give:]):
                break
            time.sleep(0.5)
        L.pump_sleep(1.0)
        # D1: applied dig of the seeded buried apple (40,104)
        cid += 1
        o = len(cli.log_text())
        send_auto("C", "dig 40 104", cid)
        done = cli.wait_for_log(r"\[AUTO\] done %d \S+ " % cid, 60, since_offset=o)
        for _ in range(80):
            if "present pocket anim started" in cli.log_text()[o:] or "REJECTED" in cli.log_text()[o:]:
                break
            time.sleep(0.5)
        #  # the txn can lag behind the autopilot (a record upload may run first)
        L.pump_sleep(4.0)
        t = cli.log_text()[o:]
        applied = [m.start() for m in re.finditer(r"TXN_RESULT APPLIED kind=\w*DIG_BURIED", t)]
        pres = [m.start() for m in re.finditer(r"\[NET\]\[DIG\] present pocket anim called", t)]
        started = t.count("[NET][DIG] present pocket anim started")
        print("[INFO] D1 applied=%d present called=%d started=%d done=%s" % (len(applied), len(pres), started, done))
        check("D1 the dig was APPLIED by the host (one TXN_RESULT APPLIED DIG_BURIED)", len(applied) == 1)
        check("D1 present-pocket-anim called exactly once, after APPLIED (started=%d)" % started, len(pres) == 1 and applied and pres[0] > applied[0])
        # make sure the player ends free
        pc.cmd("mash 6", 30)
        L.pump_sleep(2.0)
        st = pc.state()
        check("D1 the player ends free (%s)" % (st,), st is not None and st[1] == 1)

        # D2: REJECTED dig -- the real client sends DIG_BURIED for (42,104), where the host has no buried item (pocket tag + request are the real path)
        cid += 1
        o = len(cli.log_text())
        ho2 = len(host.log_text())
        send_auto("C", "digreq 42 104", cid)
        r = cli.wait_for_log(r"\[AUTO\] digreq %d [^\r\n]*" % cid, 10, since_offset=o)
        print("[INFO] D2 %s" % (r.group(0) if r else None))
        rj = cli.wait_for_log(r"TXN_RESULT REJECTED|REJECTED|rejected", 10, since_offset=o)
        L.pump_sleep(2.0)
        t = cli.log_text()[o:]
        print("[INFO] D2 client lines: %s" % [l for l in t.splitlines() if "TXN" in l or "FIELD_ACTION" in l][:6])
        check("D2 the request was sent (digreq returned 1)", r and r.group(0).endswith("returned 1"))
        check("D2 the host rejected it (client log shows a rejection)", rj or "reject" in t.lower())
        check("D2 no APPLIED and NO pocket animation (present lines %d)" % t.count("[NET][DIG] present pocket anim"), "TXN_RESULT APPLIED" not in t and t.count("[NET][DIG] present pocket anim") == 0)
        pc.cmd("mash 6", 30)
        pc.cmd("mash 8", 30)  # the earlier mash A on the dug hole starts a FILL_HOLE (main_index 59): let it finish
        L.pump_sleep(5.0)
        st = pc.state()
        check("D2 the player is free (%s)" % (st,), st is not None and st[1] == 1)
        check("S host and client alive", host.alive() and cli.alive())
    finally:
        try:
            a and a.is_connected() and a.disconnect()
        except Exception:  # noqa: BLE001
            pass
        if cli:
            cli.stop()
        host.stop()


if __name__ == "__main__":
    main()
    sys.exit(L.summary_and_exit_code(results))
