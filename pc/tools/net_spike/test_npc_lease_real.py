#!/usr/bin/env python3
"""test_npc_lease_real.py - Patch 8: the EXCLUSIVE villager conversation lease on the REAL game dialogue path, TWO REAL game processes (host + client).

TIER: REAL HOST + REAL CLIENT in the town field on a DISPOSABLE fixture copy (NET_SPIKE_GAME_BIN=<abs path of pc\build64\bin_fixture4*>), no GUI automation. The conversation itself is driven
by the existing TEST-ONLY hook PC_FORCE_TALK_VILLAGER=1 (ac_npc_move.c_inc): it parks the local player in front of the villager and injects the A / B button triggers the real controller reads; the
talk request, the host lease, the message window, the dialogue and the talk end are the unmodified production code. PC_FORCE_TALK_SLOT=1 makes BOTH processes pick the SAME villager;
PC_FORCE_TALK_HOLD_SECONDS keeps the host's dialogue open; PC_FORCE_TALK_RETRY=1 makes the client press A again after a refused attempt.
  X1 the HOST's player talks to villager 1 (real dialogue) and OWNS the lease ('HOST-LEASE')
  X2 the CLIENT, arriving later, cannot start a conversation with that villager (its A presses are refused by the talk-request gate): TRIGGER_TIMEOUT, no 'talk requested'
  X3 the host's conversation ends -> lease released ('RELEASE ... host player ended') -> the client's next attempt succeeds: real dialogue, the host log shows the CLIENT's BEGIN / END for that slot
  X4 both processes survive; the client's conversation ended normally (TALK_END)
NOT asserted: indoor villagers (no equivalent hook; covered by the protocol test), visuals / dialogue text.
Usage: python test_npc_lease_real.py [--port 11870]
"""
import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
import test_client_villager_talk as VT  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11870)
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    base = {"PC_FORCE_TALK_VILLAGER": "1", "PC_FORCE_TALK_SLOT": "1"}
    host_env = dict(base, PC_FORCE_TALK_HOLD_SECONDS="60", PC_FORCE_TALK_MAX_FRAMES="200000")
    client_env = dict(base, PC_FORCE_TALK_RETRY="1")
    host, cl = VT.boot_pair(args.port, "lease_real", HERE, L.GAME_BIN_DIR, host_env, client_env)
    check("host and client reached the field", host is not None)
    if host is None:
        return L.summary_and_exit_code(results)
    try:
        hl = host.wait_for_log(r"\[NPC\]\[TALKNET\] HOST-LEASE slot=1 npc=0x[0-9A-F]{4}", 120.0)
        check("X1 the host player's real conversation owns the lease of villager 1 (%s)" % (hl.group(0)[-30:] if hl else "none"), hl is not None)
        to = cl.wait_for_log(r"\[NPC\]\[TALKHOOK\] client TRIGGER_TIMEOUT", 120.0)
        ct = cl.log_text()
        check("X2 the client could NOT start a conversation while the host owns the villager (TRIGGER_TIMEOUT, no 'talk requested' before it)",
              to is not None and ct.find("[NPC][TALKHOOK] client talk requested") == -1 or (to is not None and ct.find("client TRIGGER_TIMEOUT") < ct.find("[NPC][TALKHOOK] client talk requested")))
        rel = host.wait_for_log(r"\[NPC\]\[TALKNET\] RELEASE slot=1 npc=0x[0-9A-F]{4} \(the host player ended the conversation\)", 150.0)
        check("X3 the host's conversation ended and the lease was released", rel is not None)
        tr = cl.wait_for_log(r"\[NPC\]\[TALKHOOK\] client talk requested", 120.0)
        check("X3 AFTER the release the client's real talk request is accepted", tr is not None)
        bg = host.wait_for_log(r"\[NPC\]\[TALKNET\] BEGIN peer=\d+ slot=1 ", 30.0)
        check("X3 the host granted the CLIENT's conversation (BEGIN peer ... slot=1)", bg is not None)
        te = cl.wait_for_log(r"\[NPC\]\[TALKHOOK\] client TALK_END", 120.0)
        check("X4 the client's dialogue ran to its end (TALK_END)", te is not None)
        en = host.wait_for_log(r"\[NPC\]\[TALKNET\] END peer=\d+ slot=1 ", 30.0)
        check("X4 the host saw the client's END and released", en is not None)
        check("host and client alive", host.alive() and cl.alive())
    finally:
        cl.stop()
        host.stop()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
