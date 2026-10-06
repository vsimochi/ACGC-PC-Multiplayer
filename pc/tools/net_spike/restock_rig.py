#!/usr/bin/env python3
"""restock_rig.py - Nook's shop 24/7 + manual restock: a MANUAL / SCREENSHOT-DRIVEN rig with a REAL dedicated host and TWO REAL guest clients A and B (NOT an automated test: a person or an
agent drives the two game windows with nook_visual_ctl.py (NK_TAG=A|B) and looks at screenshots; test_restock_protocol.py is the automated tier).

TIER: disposable bin_fixture4_rs (host, `--dedicated --time 2`: the in-game clock is 02:00, the shop would have been closed) + bin_fixture4_rsa / bin_fixture4_rsb (the two clients, EMPTY save
dirs; AC_DISPLAY_NAME=samsung, AC_MASTER_VOLUME=1). Client A = payer (`--nook-test wallet=3000,warp`), client B = bystander (`--nook-test wallet=0,warp`): both are warped into the shop.
The rig stays alive until <log dir>/rs_stop appears, then writes <log dir>/rs_report.txt and stops everything. A host console line placed in <log dir>/rs_cmd.txt is sent and consumed.
Usage: python restock_rig.py [--port 11972]
"""
import argparse
import json
import os
import re
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=11972)
ap.add_argument("--time", default="2")
args = ap.parse_args()
HOST_NAME = "bin_fixture4_rs"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, HOST_NAME)
T.make_fixture(HOST_NAME)
CL = {"A": "bin_fixture4_rsa", "B": "bin_fixture4_rsb"}
for n in CL.values():
    T.make_fixture(n, empty_save=True)

import net_spike_lib as L  # noqa: E402

ENV = {"AC_TEST_HOOKS": "1", "AC_TOWN_NO_MSGBOX": "1", "AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1"}
STOP = T.log_path("rs_stop")
CMD = T.log_path("rs_cmd.txt")
REPORT = T.log_path("rs_report.txt")
for p in (STOP, CMD, REPORT, T.log_path("A_nk_state.json"), T.log_path("B_nk_state.json")):
    if os.path.exists(p):
        os.remove(p)

host = None
clients = {}
try:
    L.require_test_bin_dir()
    uuids = {}
    for tag, name in CL.items():
        d = os.path.join(T.BUILD64, name)
        mp = os.path.join(d, "save", "mp")
        os.makedirs(mp, exist_ok=True)
        open(os.path.join(mp, "guest_p%s.ini" % tag.lower()), "w", newline="").write(
            "name = Pl%s\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x12%s5\nland_id = 0x4321\n" % (tag, "3" if tag == "A" else "4"))
        r = subprocess.run([os.path.join(d, "AnimalCrossing.exe"), "--character-import-profile", "p%s" % tag.lower()], cwd=d, capture_output=True, text=True, timeout=60)
        m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
        assert r.returncode == 0 and m, "profile import failed: " + r.stdout + r.stderr
        uuids[tag] = m.group(1)
    host = L.HostProcess(port=args.port, extra_args=["--dedicated", "--town-serve", "on", "--resident-tokens", "tofu", "--time", args.time], log_path=T.log_path("rs_host.log"),
                         bin_dir=L.GAME_BIN_DIR, stdin_pipe=True, verbose=False, new_group=True).start()
    assert host.wait_listening(60.0) and host.boot_to_dedicated(timeout=120.0), "host did not boot"
    for tag, name in CL.items():
        d = os.path.join(T.BUILD64, name)
        spec = "wallet=3000,warp" if tag == "A" else "wallet=0,warp"
        c = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--character", uuids[tag], "--town-fetch", "--online-ui", "--nook-test", spec], log_path=T.log_path("rs_%s.log" % tag),
                            bin_dir=d, env=ENV, label="rs" + tag).start()
        mo = c.wait_for_log(r"-> READY", 240.0)
        ok = mo is not None and c.proc.poll() is None
        clients[tag] = c
        json.dump({"pid": c.proc.pid, "ready": ok, "exit": c.proc.poll()}, open(T.log_path("%s_nk_state.json" % tag), "w"))
        print("client %s pid %d ready=%s exit=%s" % (tag, c.proc.pid, ok, c.proc.poll()), flush=True)
    print("rig up; waiting for %s" % STOP, flush=True)
    while not os.path.exists(STOP):
        if os.path.exists(CMD):
            line = open(CMD).read().strip()
            os.remove(CMD)
            if line:
                host.send_line(line)
        time.sleep(0.5)
    out = []
    rx = re.compile(r"RESTOCK|nook dialogue|shown out|TS\] client: applied service 4|TS\] client: applied service 3|Unhandled")
    for tag, c in clients.items():
        out.append("--- client %s alive=%s\n%s" % (tag, c.proc.poll() is None, "\n".join(l for l in c.log_text().replace("\r\n", "\n").split("\n") if rx.search(l))))
    out.append("--- host\n" + "\n".join(l for l in host.log_text().replace("\r\n", "\n").split("\n") if re.search(r"RESTOCK|SHOP_RESTOCK|service 3|service 4", l)))
    open(REPORT, "w", encoding="utf-8").write("\n".join(out))
finally:
    for c in clients.values():
        c.stop()
    if host is not None:
        try:
            host.send_line("stop")
            host.wait_exit(60.0)
        except Exception:
            pass
        host.stop()
    print("rig down", flush=True)
