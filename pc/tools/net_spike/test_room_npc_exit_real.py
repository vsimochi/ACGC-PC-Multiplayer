#!/usr/bin/env python3
"""test_room_npc_exit_real.py - villager HOUSE EXIT synchronization (PC_NETGAME_MSG_NPC_HOME_EXIT, id 72), REAL host + REAL client processes (no GUI automation).

TIER: REAL HOST + REAL CLIENT on a DISPOSABLE copy (pc\\build64\\bin_fixture4_homeexit, recreated here). Ports 11830+. Both processes are driven into the SAME villager's house through the game's own scene change
by the TEST-ONLY hook AC_TEST_ROOM_ENTER (AC_TEST_HOOKS=1); AC_TEST_GOTO "v 1" (ac_goto.txt) moves the player next to the villager in the room so it does not stand on the exit route.

The bug (client side): the host's authoritative NPC_STATE of a villager at home is is_home=1 hide=1. A CLIENT inside that villager's house sees it walk out (aNPC_set_be_out_home: is_home 1 -> 0),
leaves the house, and its new outdoor actor was immediately overwritten with the host's stale is_home=1 hide=1: the villager was gone outside. The client now reports the exit to the host, which
validates it, sets the villager outside and broadcasts is_home=0 hide=0.

  S1 (the client leaves the house with the villager gone out; the host entered and left the house first so its state is at home):
     S1a  the host's authoritative state of that villager is at home (is_home=1; hide=1 when its outdoor actor is back at home) before the client's exit
     S1b  the client REPORTED the exit ("[NPC][HOMEEXIT] client: SEND") and the host ACCEPTED it
     S1c  the host broadcast NPC_STATE is_home=0 hide=0 after accepting it, and never is_home=1 afterwards
     S1d  the client applied is_home=0 hide=0 and never the stale is_home=1 afterwards
     S1e  the client's outdoor villager actor was created after it left the house (the villager is present outside)
  S2 (regression, the HOST is the one inside): the host's own exit path is unchanged: the host broadcasts is_home=0 hide=0 after it left, the client applies it, and NO NPC_HOME_EXIT message is
     involved at all.
NOT asserted: the visuals, and a hand-made re-entry (AC_TEST_ROOM_ENTER forces is_home=1 on every entry, which would hide a restored "at home" state).
Usage: python test_room_npc_exit_real.py [--port 11830]
"""
import argparse
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

NAME = "bin_fixture4_homeexit"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, NAME)
if __name__ == "__main__":
    T.make_fixture(NAME)
os.environ["AC_TEST_HOOKS"] = "1"
import net_spike_lib as L  # noqa: E402
import test_txn_real_client as RC  # noqa: E402

SLOT = 1
GOTO_FILE = os.path.join(L.GAME_BIN_DIR, "ac_goto.txt")
ST_HOST = re.compile(r"\[NET\]\[NPC\] host: NPC_STATE change slot %d npc_id 0x[0-9A-F]{4} is_home=(\d) hide=(\d)" % SLOT)
ST_CLIENT = re.compile(r"\[NET\]\[NPC\] client: NPC_STATE applied slot %d npc_id 0x[0-9A-F]{4} is_home=(\d) hide=(\d)" % SLOT)


def states(rx, text, start=0):
    return [(m.start(), int(m.group(1)), int(m.group(2))) for m in rx.finditer(text, start)]


def env(**kw):
    for k in ("AC_TEST_ROOM_ENTER", "AC_TEST_GOTO"):
        os.environ.pop(k, None)
    for k, v in kw.items():
        os.environ[k] = v


def park_next_to_villager(proc, rx=r"inside the villager's house \(entry 1\)"):
    """In the room: park the local player next to the villager (AC_TEST_GOTO 'v'), off the exit route."""
    proc.wait_for_log(rx, 120.0)
    time.sleep(1.5)
    with open(GOTO_FILE, "w") as f:
        f.write("v %d" % SLOT)


def scenario_client_exit(args, rig, check):
    env(AC_TEST_ROOM_ENTER="%d,8000,-1,-1,4000,-1" % SLOT)
    host = rig.start_host("homeexit_s1", args.port, [])
    check("S1 host reached genuine field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town("127.0.0.1", args.port)
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    env(AC_TEST_ROOM_ENTER="%d,20000,-1,-1,75000,-1" % SLOT, AC_TEST_GOTO="1")
    cl = rig.start_client("homeexit_s1", args.port, residents[0], [])
    env()
    check("S1 real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    host.wait_for_log(r"\[NET\]\[ROOMNPC\]\[TEST-ONLY\] back in the field", 120.0)
    park_next_to_villager(cl)
    sent = cl.wait_for_log(r"\[NPC\]\[HOMEEXIT\] client: SEND slot=%d npc=0x[0-9A-F]{4}" % SLOT, 110.0)
    check("S1b the client reported the villager's house exit (the villager really walked out in its room)", sent is not None)
    acc = host.wait_for_log(r"\[NPC\]\[HOMEEXIT\] host: ACCEPTED peer=\d+ slot=%d " % SLOT, 20.0) if sent is not None else None
    check("S1b the host validated and ACCEPTED it", acc is not None)
    back = cl.wait_for_log(r"\[NET\]\[ROOMNPC\]\[TEST-ONLY\] back in the field", 90.0)
    check("S1 the client left the house and is back in the field", back is not None)
    time.sleep(12.0)
    ht, ct = host.log_text(), cl.log_text()
    a_off = ht.find("[NPC][HOMEEXIT] host: ACCEPTED")
    hs = states(ST_HOST, ht)
    before = [s for s in hs if a_off < 0 or s[0] < a_off]
    after = [s for s in hs if a_off >= 0 and s[0] > a_off]
    check("S1a the host's authoritative state before the exit was at home: is_home=1 (hide=1 once its outdoor actor is back at home; %s)" % [s[1:] for s in before], any(s[1] == 1 for s in before))
    print("info: the host's own hidden outdoor actor came out through the new path: %s" % ("[NPC][HOMEEXIT] host: outdoor villager slot %d came out" % SLOT in ht))
    check("S1c the host broadcast NPC_STATE is_home=0 hide=0 after accepting the exit (%s)" % [s[1:] for s in after], any(s[1] == 0 and s[2] == 0 for s in after))
    check("S1c the host never went back to is_home=1 afterwards (%s)" % [s[1:] for s in after], not any(s[1] == 1 for s in after))
    s_off = ct.find("[NPC][HOMEEXIT] client: SEND")
    cs = [s for s in states(ST_CLIENT, ct) if s[0] > s_off >= 0]
    check("S1d the client applied is_home=0 hide=0 after reporting (%s)" % [s[1:] for s in cs], any(s[1] == 0 and s[2] == 0 for s in cs))
    check("S1d the client never applied the stale is_home=1 afterwards (%s)" % [s[1:] for s in cs], not any(s[1] == 1 for s in cs))
    b_off = ct.rfind("[NET][ROOMNPC][TEST-ONLY] leaving the villager's house")
    check("S1e the client's outdoor villager actor was created after it left the house (the villager is present outside)",
          b_off >= 0 and re.search(r"\[NPC\] regular villager actor spawned slot %d " % SLOT, ct[b_off:]) is not None)
    check("S1 both processes alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def scenario_host_inside(args, rig, check):
    env(AC_TEST_ROOM_ENTER="%d,8000,-1,-1,75000,-1" % SLOT, AC_TEST_GOTO="1")
    host = rig.start_host("homeexit_s2", args.port + 1, [])
    check("S2 host reached genuine field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town("127.0.0.1", args.port + 1)
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    env()
    cl = rig.start_client("homeexit_s2", args.port + 1, residents[0], [])
    check("S2 real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    park_next_to_villager(host)
    host.wait_for_log(r"\[NET\]\[ROOMNPC\]\[TEST-ONLY\] leaving the villager's house", 120.0)
    back = host.wait_for_log(r"\[NET\]\[ROOMNPC\]\[TEST-ONLY\] back in the field", 60.0)
    check("S2 the host left the house and is back in the field", back is not None)
    time.sleep(12.0)
    ht, ct = host.log_text(), cl.log_text()
    l_off = ht.find("[NET][ROOMNPC][TEST-ONLY] leaving the villager's house")
    hs = [s for s in states(ST_HOST, ht) if s[0] > l_off >= 0]
    cs = [s for s in states(ST_CLIENT, ct)]
    check("S2 the host broadcast NPC_STATE is_home=0 hide=0 after it left the house (%s)" % [s[1:] for s in hs], any(s[1] == 0 and s[2] == 0 for s in hs))
    check("S2 the client applied is_home=0 hide=0 (last applied: %s)" % (str(cs[-1][1:]) if cs else "none",), bool(cs) and cs[-1][1] == 0 and cs[-1][2] == 0)
    check("S2 no NPC_HOME_EXIT message was involved (the host path is unchanged)", "[NPC][HOMEEXIT]" not in ht and "[NPC][HOMEEXIT]" not in ct)
    check("S2 both processes alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11830)
    ap.add_argument("--only", choices=("s1", "s2"))
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_<name> copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "room_npc_exit_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        if args.only != "s2":
            scenario_client_exit(args, rig, rig.check)
            if rig.host is not None:
                rig.host.stop()
            time.sleep(3.0)
        if args.only != "s1":
            scenario_host_inside(args, rig, rig.check)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
        if os.path.exists(GOTO_FILE):
            os.remove(GOTO_FILE)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
