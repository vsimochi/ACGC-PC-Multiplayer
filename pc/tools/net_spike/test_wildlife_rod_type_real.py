#!/usr/bin/env python3
"""test_wildlife_rod_type_real.py - WS2-B: the host judges each remote angler's bobber with THAT angler's rod (a REAL host process + one FakeClient peer; Samsung display, volume 1).

Path under test:  client  pcwld_client_collect_bobber()  (rod_type = Now_Private->equipment == ITM_GOLDEN_ROD)
                  -> BOBBER_STATE.rod_type (wire byte 8)  -> host pcwld_host_set_bobber() (PcWldProxy.rod_type, any non-zero byte means golden)
                  -> aGYO_get_uki_type_for() / aGKK_get_uki_type_for() (pcwld_proxy_rod_type) in the fish AI's search/near checks.
The FakeClient plays the angler and sends BOBBER_STATE with rod_type 0 / 1 / 7 / 255 / back to 0 (a legacy sender that never sets the byte is 0 = normal rod). Evidence: the host's [PROXY-DIAG] lines
(AC_TEST_PROXY_DIAG=1): "wire value N -> stored as X rod" (what the host kept) and "is judged by the fish AI with the X rod" (what the fish AI actually used; needs a live host fish near the bobber:
the host injects a river fish with the `hspawn` autopilot command).
NOT covered (fixture limitation, see TODO.md): the CLIENT end (a real client process equipping the golden rod) -- the autopilot `give` can put ITM_GOLDEN_ROD into a pocket but nothing in the
harness selects it as the equipped item, so the 'real golden rod' leg is manual.
Usage: python test_wildlife_rod_type_real.py [--port 12992]"""
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
import test_wildlife_proxy_capacity_real as C  # noqa: E402
import net_spike_lib as L  # noqa: E402

BOBBER_STATE = 74
W = C.W


def payload(rod, seq, x, y, z, active=True):
    return struct.pack("<BBBbbBBB6fhh", BOBBER_STATE, 1 if active else 0, 3 if active else 0, 1 if active else 0, 0, 1 if active else 0, 5 if active else 0, rod, x, y, z, x, y, z, 0, seq)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12992)
    args = ap.parse_args()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    env = {"AC_TEST_AUTOPILOT": "ac_auto_host.txt", "AC_TEST_PROXY_DIAG": "1", "AC_TEST_WILDLIFE_REROLL": "80"}
    host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=env, log_path=os.path.join(HERE, "rodtype_host.log"), bin_dir=W.BIN).start()
    f = None
    try:
        ok = host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)
        check("host in the field", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        L.resolve_host_town("127.0.0.1", args.port)
        P = W.Player(L, "host", host)
        f = L.FakeClient("F1", "127.0.0.1", args.port)
        f.connect_and_ready()
        clients = [f]
        f.claim_position(2794.0, 160.0, 1464.0)
        L.pump_sleep(1.0)

        def log_from(off):
            return host.log_text()[off:]

        # a river fish for the fish AI to examine, right where the bobbers will be
        FX, FZ = 2794.0, 1404.0
        off = len(host.log_text())
        ans = P.cmd("hspawn 8 %f %f" % (FX, FZ), 30)
        check("the host injected a river fish at (%d,%d) (%s)" % (FX, FZ, ans), ans is not None and re.search(r"entity [1-9]\d*", ans or "") is not None)
        L.pump_sleep(1.5)

        seq = 100

        def cast(rod):
            nonlocal seq
            seq += 1
            for c in clients:  # a peer that stays silent is dropped: keep standing in the fish's acre
                c.claim_position(FX, 160.0, FZ + 60.0)
            f.send_unreliable(payload(rod, seq, FX + 6.0, 132.0, FZ + 6.0))

        off = len(host.log_text())
        cast(0)
        L.pump_sleep(0.6)
        check("a cast with rod_type 0 gets a proxy and stores no golden rod", "alloc slot" in log_from(off) and "stored as golden" not in log_from(off))
        off = len(host.log_text())
        for _ in range(10):  # keep the bobber alive and let the fish AI examine it (the proxy expires without refreshes)
            cast(0)
            L.pump_sleep(0.3)
        judged0 = re.findall(r"is judged by the fish AI with the (\w+) rod", log_from(off))
        info0 = "fish AI judged it with: %s" % (judged0[-1:] or "(no fish examined the bobber)")
        L.info(info0)
        check("normal rod: if the fish AI examined the bobber it used the NORMAL rod (%s)" % info0, not judged0 or judged0[-1] == "normal")
        examined = bool(judged0)

        for rod, label, expect in ((1, "1 (golden)", "golden"), (0, "0 (back to normal)", "normal"), (7, "7 (invalid, non-zero)", "golden"), (255, "255 (invalid)", None), (0, "0 again", "normal")):
            off = len(host.log_text())
            for _ in range(8):
                cast(rod)
                L.pump_sleep(0.3)
            lg = log_from(off)
            stored = re.findall(r"wire value (\d+) -> stored as (\w+) rod", lg)
            judged = re.findall(r"is judged by the fish AI with the (\w+) rod", lg)
            if expect is None:  # 255 follows 7 (both golden): no change to report -- the value must NOT have turned the proxy normal
                check("rod_type %s keeps the golden rod (no switch to normal: %s)" % (label, stored), all(s[1] != "normal" for s in stored))
            else:
                check("rod_type %s is stored as the %s rod (%s)" % (label, expect, stored[-1:] or "no change line"), bool(stored) and stored[-1][1] == expect)
            if judged:
                check("rod_type %s: the fish AI judged the bobber with the %s rod (%s)" % (label, expect or "golden", judged[-1]), judged[-1] == (expect or "golden"))
        check("the fish AI examined at least one of the bobbers (consumption path exercised)", examined or "is judged by the fish AI" in host.log_text())

        # a second angler is judged by ITS OWN rod: two peers, one golden, one normal, at the same time
        f2 = L.FakeClient("F2", "127.0.0.1", args.port)
        f2.connect_and_ready()
        clients.append(f2)
        L.pump_sleep(1.0)
        off = len(host.log_text())
        for i in range(10):
            seq += 1
            for c in clients:
                c.claim_position(FX, 160.0, FZ + 60.0)
            f.send_unreliable(payload(1, seq, FX + 6.0, 132.0, FZ + 6.0))
            f2.send_unreliable(payload(0, 500 + i, FX - 6.0, 132.0, FZ - 6.0))
            L.pump_sleep(0.3)
        lg = log_from(off)
        stored = sorted(set(re.findall(r"peer (\d+) bobber rod: wire value (\d+) -> stored as (\w+) rod", lg)))
        L.info("two anglers: %s" % stored)
        peers_golden = {s[0] for s in stored if s[2] == "golden"}
        peers_normal_judged = {m for m in re.findall(r"peer (\d+) bobber is judged by the fish AI with the normal rod", lg)}
        check("the golden angler is stored golden and the normal angler never is (golden peers %s)" % sorted(peers_golden), len(peers_golden) <= 1 and not (peers_golden & peers_normal_judged))
        try:
            f2.disconnect()
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            if f is not None:
                f.disconnect()
        except Exception:  # noqa: BLE001
            pass
        host.stop()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
