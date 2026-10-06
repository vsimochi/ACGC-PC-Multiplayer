#!/usr/bin/env python3
"""test_evnpc_real.py - Event NPC authority (Patch 4), REAL processes: one REAL host + one REAL client game process (no GUI automation).

TIER: REAL HOST + REAL CLIENT, booted into the town field with the bootstrap resident, on a DISPOSABLE copy: NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>> (ports 11520+).
  host   = `AnimalCrossing.exe --host P --bootstrap-resident 0` with AC_TEST_HOOKS=1, AC_TEST_EVNPC_SPAWN=D064,3,3,8,8 (TEST-ONLY: the host creates Gulliver (SP_NPC_EV_DOZAEMON) at block (3,3)
           unit (8,8) once its town field is up -- the event manager would need the right date / hour / a nearby player) and AC_TEST_EVNPC_DESPAWN_MS=22000 (the visitor leaves 22 s later)
  client = `AnimalCrossing.exe --connect 127.0.0.1:P --bootstrap-resident <slot>`
Asserted from the process logs:
  E1 the host created the event NPC and published the table (EVNPC_STATE seq 1: 1 present) -- without a "player nearby" gate (the host's own player is parked elsewhere)
  E2 the client got the table and CREATED the host's visitor at the host's place ('created the host's event NPC 0xD064 at block (3,3) unit (8,8)'); the client's own event code created none
  E3 the host's visitor left: the table changed (0 present) and the client REMOVED its local actor
  E4 nothing crashed; both processes alive
NOT asserted (no GUI automation): the visual result / Gulliver's dialogue.
Usage: python test_evnpc_real.py [--port 11520]
"""
import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ["AC_TEST_EVNPC_SPAWN"] = "D064,3,3,8,8"
os.environ["AC_TEST_EVNPC_DESPAWN_MS"] = "22000"
os.environ["AC_TEST_EVNPC_CLAIM"] = "1"  # the REAL client claims Gulliver's gift once its record is synced
import net_spike_lib as L  # noqa: E402
import test_txn_real_client as RC  # noqa: E402


def count(rx, text):
    return len(re.findall(rx, text))


def run(args, results, ip, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    host = rig.start_host("evnpc", args.port, [])
    check("host reached genuine field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    cl = rig.start_client("evnpc", args.port, residents[0], [])
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    m = cl.wait_for_log(r"\[NET\]\[EVNPC\] client: created the host's event NPC 0xD064 at block \((\d+),(\d+)\) unit \((\d+),(\d+)\)", 60.0)
    ht = host.log_text()
    check("E1 the host created the event NPC (test hook) and published a table with ONE visitor", "[NET][EVNPC][TEST-ONLY] host: creating event NPC 0xD064" in ht
          and re.search(r"\[NET\]\[EVNPC\] host: event NPC table seq \d+: 1 present", ht) is not None)
    check("E2 the client created the host's visitor (Gulliver, 0xD064) from the table", m is not None)
    ct = cl.log_text()
    if m is not None:
        # the block / unit the client computed from the HOST's world position of the actor
        hm = re.search(r"\[NET\]\[EVNPC\] host: first entry npc 0xD064 at \(([-\d.]+),([-\d.]+)\)", ht)
        check("E2 the client created it at the host's place (the host's actor position is in the table: %s)" % (hm.group(0) if hm else "?"), hm is not None)
    am = re.search(r"created the host's event NPC 0xD064 at block \(\d+,\d+\) unit \(\d+,\d+\): actor exists, actor at \(([-\d.]+),([-\d.]+)\), host at \(([-\d.]+),([-\d.]+)\)", ct)
    check("E2 the client's ACTOR exists and stands within one unit of the host's actor (%s)" % (am.group(0)[-70:] if am else "no actor line"),
          am is not None and abs(float(am.group(1)) - float(am.group(3))) <= 16.0 and abs(float(am.group(2)) - float(am.group(4))) <= 16.0)
    check("E2 the client's own event code never created a second one: exactly ONE create line for the visitor", count(r"created the host's event NPC 0xD064", ct) == 1)
    cm = cl.wait_for_log(r"\[NET\]\[EVNPC\]\[TEST-ONLY\] client: claim op 1 (APPLIED|REJECTED) \(reason (\d+), granted item 0x([0-9A-F]{4})\)", 60.0)
    ht = host.log_text()
    check("C1 the client's claim of Gulliver's gift was APPLIED by the HOST: the item is the one the host rolled and committed into the free slot",
          cm is not None and cm.group(1) == "APPLIED" and re.search(r"\[NET\]\[EVNPC\] host: peer \d+ resident \d+ EVNPC claim op 1 committed: item 0x%s into pocket slot \d+ \[TXN\]" % cm.group(3), ht) is not None)
    check("C1 the client applied the host's post-image (no local roll): 'claim APPLIED ... the host granted item'", re.search(r"\[NET\]\[EVNPC\] client: claim APPLIED \(request \d+\): the host granted item 0x%s" % (cm.group(3) if cm else "0000"), cl.log_text()) is not None)
    gone = cl.wait_for_log(r"\[NET\]\[EVNPC\] client: the host has no event NPC 0xD064 any more -- removing the local actor", 60.0)
    check("E3 the host's visitor left (test hook), the table went to 0 present and the client REMOVED its local actor",
          "[NET][EVNPC][TEST-ONLY] host: the event NPC 0xD064 leaves" in host.log_text() and gone is not None
          and re.search(r"\[NET\]\[EVNPC\] host: event NPC table seq \d+: 0 present", host.log_text()) is not None)
    check("E4 host and client alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11520)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_<name> copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "evnpc_real_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        run(args, results, ip, rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
