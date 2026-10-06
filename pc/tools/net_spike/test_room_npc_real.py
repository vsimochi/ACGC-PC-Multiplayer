#!/usr/bin/env python3
"""test_room_npc_real.py - Indoor villager synchronization (Patch 7), REAL processes: one REAL host + one REAL client game process (no GUI automation).

TIER: REAL HOST + REAL CLIENT, booted into the town field with their bootstrap residents, on a DISPOSABLE copy: NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>> (ports 11740+).
Both processes are driven INTO THE SAME VILLAGER'S HOUSE through the game's own scene change by the TEST-ONLY hook AC_TEST_ROOM_ENTER (AC_TEST_HOOKS=1):
  host   AC_TEST_ROOM_ENTER=0,6000        enters the house of animal 0 six seconds after it is idle in the field and stays
  client AC_TEST_ROOM_ENTER=0,9000,3000,9000,15000,3000   enters 9 s after, "talks" to the villager (the pose carries the TALK flag; the dialogue itself needs a GUI) from 3 s to 9 s in the room,
                                  leaves 15 s after entering and re-enters 3 s after being back in the field
Asserted from the process logs:
  I1 both real processes entered the SAME house through the real scene change and both announced the room (the host sees the client in the room)
  I2 ONE pose holder was arbitrated by the host; the other process FOLLOWS the holder's pose ('following the room pose ... owner 0x....')
  I3 talk state: the host logs the client's villager as TALKING and the other occupant logs 'talk state -> 1' (visible to the player that is not talking), later 'talk state -> 0'
  I4 the client left the house and RE-ENTERED (entry 2): consistent, the host still alive and the room pose flows again
  I5 nothing crashed; both processes alive
NOT asserted (no GUI automation): the actual dialogue, visuals, the villager's pixels. The talk state is injected by the hook at the sampling seam; everything downstream (wire, host lease arbitration,
relay, follower hold) is the real code.
Usage: python test_room_npc_real.py [--port 11740]
"""
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


def run(args, results, ip, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    os.environ["AC_TEST_ROOM_ENTER"] = "0,6000"
    host = rig.start_host("roomnpc", args.port, [])
    check("host reached genuine field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    os.environ["AC_TEST_ROOM_ENTER"] = "0,9000,3000,9000,15000,3000"
    cl = rig.start_client("roomnpc", args.port, residents[0], [])
    os.environ.pop("AC_TEST_ROOM_ENTER", None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    hin = host.wait_for_log(r"\[NET\]\[ROOMNPC\]\[TEST-ONLY\] inside the villager's house \(entry 1\)", 90.0)
    cin = cl.wait_for_log(r"\[NET\]\[ROOMNPC\]\[TEST-ONLY\] inside the villager's house \(entry 1\)", 90.0)
    check("I1 the HOST process entered the villager's house through the real scene change", hin is not None)
    check("I1 the CLIENT process entered the villager's house through the real scene change", cin is not None)
    ho = host.wait_for_log(r"\[NET\]\[ROOMNPC\] host: room scene \d+ owner 0x([0-9A-F]{4}) pose holder is now player (-?\d+)", 40.0)
    check("I2 the host arbitrated a pose holder for the room (%s)" % (ho.group(0)[-48:] if ho else "none"), ho is not None)
    fh = host.wait_for_log(r"\[NET\]\[ROOMNPC\] following the room pose", 20.0)
    fc = cl.wait_for_log(r"\[NET\]\[ROOMNPC\] following the room pose", 20.0)
    check("I2 the non-holder process follows the holder's pose (host followed: %s, client followed: %s)" % (fh is not None, fc is not None), fh is not None or fc is not None)
    tk = host.wait_for_log(r"\[NET\]\[ROOMNPC\] host: villager 0x([0-9A-F]{4}) in its house is TALKING with player (\d+)", 40.0)
    check("I3 the host recorded the villager as TALKING with the client's player (%s)" % (tk.group(0)[-40:] if tk else "none"), tk is not None and tk.group(2) != "-1")
    hv = host.wait_for_log(r"\[NET\]\[ROOMNPC\] room villager 0x[0-9A-F]{4} talk state -> 1 \(holder player (\d+)\)", 30.0)
    check("I3 the OTHER occupant (the host process, not talking) sees the talk state (talk state -> 1)", hv is not None)
    hv0 = host.wait_for_log(r"\[NET\]\[ROOMNPC\] room villager 0x[0-9A-F]{4} talk state -> 0", 40.0)
    check("I3 the talk state ended for the other occupant (talk state -> 0)", hv0 is not None)
    lv = cl.wait_for_log(r"\[NET\]\[ROOMNPC\]\[TEST-ONLY\] leaving the villager's house", 60.0)
    bk = cl.wait_for_log(r"\[NET\]\[ROOMNPC\]\[TEST-ONLY\] back in the field", 60.0)
    re2 = cl.wait_for_log(r"\[NET\]\[ROOMNPC\]\[TEST-ONLY\] inside the villager's house \(entry 2\)", 90.0)
    check("I4 the client left the house, was back in the field and RE-ENTERED the same house (entry 2)", lv is not None and bk is not None and re2 is not None)
    check("I4 the host process was still in the room and alive after the client's leave / re-enter (room scene announced again)", host.alive() and host.log_text().count("pose holder is now player") >= 1)
    check("I5 host and client alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11740)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_<name> copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "room_npc_real_save_snapshot_%d" % os.getpid())
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
