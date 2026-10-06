#!/usr/bin/env python3
"""test_client_villager_talk.py - regression test for the "ready client locks up talking to a regular
villager" bug (client stuck forever in mPlayer_INDEX_TALK, main=65, dialogue never opens).

Root cause (see ac_npc_move.c_inc, aNPC_pc_talk_lease_active()): on a host-authoritative client the N2
villager sync skips aNPC_schedule_proc()/aNPC_action_proc(), so the villager's local talk action never ran,
mDemo_Set_ListenAble() never came and m_demo.c wait_talk_start() waited forever. The fix is a stateless
"talk lease": a villager in a local talk interaction is not a client consumer until its talk action is over.

TIER LABELS (honest): TWO REAL game processes (`--bootstrap-resident 0` host / `1` client), no GUI
automation. The talk itself is HOOK-DRIVEN REAL GAMEPLAY: the test-only, off-by-default environment variable
PC_FORCE_TALK_VILLAGER=1 (ac_npc_move.c_inc, aNPC_pc_talk_hook()) only teleports the local player in front of
a villager and injects the A / START button triggers and a stick push that the real controller reads
(m_controller.c); everything else (m_demo.c choose/wait_talk_start, aNPC_setup_talk_start, the villager talk
action, the message window, wait_talk_end, the player TALK state, the inventory submenu) is the unmodified
production code. It is NOT manual play: real keyboard/controller handling, dialogue content/variety and
speaking to special NPCs are NOT covered.

  C1  CLIENT (fixed build): host + client READY in the field; hook selects a villager, presses A; the client
      enters TALK, the villager reaches ListenAble, the dialogue runs to its end, the client returns to
      normal control, can then walk (stick) and open the inventory (START). During the talk a different
      villager is still reported host-mirrored (client consumer) and the talk lease begin/end are logged.
  C2  HOST: the same hook on the host process -- a normal host villager interaction still works.
  C3  NEGATIVE CONTROL: client with PC_NPC_TALKLEASE_DISABLE=1 (restores the pre-fix gate) must FAIL the
      same scenario: stuck in TALK, ListenAble never reached.

  M9-C scenarios H1/H1C/H6/H7/H7E (host-authoritative talk hold, NPC_TALK id 45) are documented above their
  functions below; they need a build with PC_NETGAME_PROTOCOL_VERSION >= 6 (7 since M9-C Phase 2a).

Usage: python test_client_villager_talk.py [--port 7811] [--only C1|C2|C3|H1|H1C|H6|H7|H7E|H8|H8C] [--bin-dir DIR]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import sys
import time

import net_spike_lib as L

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BIN = os.path.normpath(os.path.join(HERE, "..", "..", "build64", "bin_fixture4"))  # 4 residents: H7 needs a 2nd scripted peer
TERMINAL_RX = r"\[NPC\]\[TALKHOOK\] (?:client|host) (?:DONE|STUCK|TRIGGER_TIMEOUT)"


def boot_pair(port, name, log_dir, bin_dir, host_env, client_env, attempts=3):
    """Host then client, via direct bootstrap; retries the known intermittent host boot crash."""
    for i in range(attempts):
        host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"],
                             log_path=os.path.join(log_dir, f"{name}_host_try{i}.log"),
                             bin_dir=bin_dir, env=host_env).start()
        if host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=0):
            break
        host.stop()
        host = None
        time.sleep(2.0)
    if host is None:
        return None, None
    for i in range(attempts):
        cl = L.ClientProcess(f"127.0.0.1:{port}", extra_args=["--bootstrap-resident", "1"],
                             log_path=os.path.join(log_dir, f"{name}_client_try{i}.log"),
                             bin_dir=bin_dir, env=client_env, label=name).start()
        if cl.boot_to_field(timeout=90.0, slot=1):
            return host, cl
        cl.stop()
        time.sleep(2.0)
    host.stop()
    return None, None


def hook_lines(text):
    return [ln for ln in text.splitlines() if "[NPC][TALKHOOK]" in ln or "[NPC][TALKLEASE]" in ln]


def run_scenario(port, name, log_dir, bin_dir, host_env, client_env, watch, timeout):
    host, cl = boot_pair(port, name, log_dir, bin_dir, host_env, client_env)
    if host is None:
        return None, None, None
    try:
        proc = cl if watch == "client" else host
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.wait_for_log(TERMINAL_RX, 5.0) is not None:
                break
            if not proc.alive():
                break
        time.sleep(1.5)
        return host.log_text(), cl.log_text(), (host.alive(), cl.alive())
    finally:
        cl.stop()
        host.stop()


def c1(port, log_dir, bin_dir, check):
    print("=" * 72 + "\n[C1] client talks to a regular villager (fixed build)")
    h, c, alive = run_scenario(port, "c1", log_dir, bin_dir, None, {"PC_FORCE_TALK_VILLAGER": "1"}, "client", 240.0)
    if c is None:
        check("C1 host+client reached the field (retried boot)", False)
        return
    check("C1 (1) host and client both reached the field; client is a host-authoritative consumer",
          "[NET][WORLD] client: snapshot epoch" in c and "[NPC][TALKHOOK] client selected villager" in c)
    check("C1 (2) the hook placed the player and pressed A at a regular villager (talk requested)",
          re.search(r"\[NPC\]\[TALKHOOK\] client talk requested", c) is not None)
    check("C1 (2b) talk lease began for the talked-to villager only",
          re.search(r"\[NPC\]\[TALKLEASE\] begin slot=\d+ talk_cond=", c) is not None)
    check("C1 (3) client entered TALK (main=65/mPlayer_INDEX_TALK) while talking",
          re.search(r"talking: frames=\d+ main=65 ", c) is not None or "ListenAble reached" in c)
    check("C1 (4) the villager reached ListenAble (talk action ran locally)",
          re.search(r"\[NPC\]\[TALKHOOK\] client ListenAble reached after \d+ frames", c) is not None)
    check("C1 (4b) the message window was open during the dialogue",
          re.search(r"talking: frames=\d+ main=65 listen=1 msg_visible=1", c) is not None or
          "[NPC][TALKHOOK] client TALK_END" in c)
    m = re.search(r"\[NPC\]\[TALKHOOK\] client TALK_END after (\d+) frames", c)
    check("C1 (5,6) dialogue ended (not stuck) and the client left TALK -> normal control (TALK_END)", m is not None)
    check("C1 (5b) no STUCK / TRIGGER_TIMEOUT line", "TALKHOOK] client STUCK" not in c and
          "TALKHOOK] client TRIGGER_TIMEOUT" not in c)
    check("C1 (6b) the lease ended and host mirroring resumed ('[NPC][TALKLEASE] end')",
          re.search(r"\[NPC\]\[TALKLEASE\] end slot=\d+", c) is not None)
    mv = re.search(r"MOVE_AFTER moved=([0-9.]+) main=(\d+)", c)
    check("C1 (7) the client can move afterwards (stick push moved the player > 5 units)",
          mv is not None and float(mv.group(1)) > 5.0)
    inv = re.search(r"INVENTORY_AFTER opened=(\d)", c)
    check("C1 (8) the client can open the inventory afterwards (START opened the submenu)",
          inv is not None and inv.group(1) == "1")
    others = re.findall(r"other villager slot=(\d+) consumer=(\d)", c)
    others = [(sl, cn) for sl, cn in others if int(sl) >= 0]  # slot -1 = special NPC (never a consumer)
    check("C1 (10) an unrelated villager stayed a host-mirrored client consumer during the talk "
          "(%d samples)" % len(others), len(others) > 0 and all(cn == "1" for _, cn in others))
    check("C1 both processes alive at the end (no crash)", alive is not None and alive[0] and alive[1])


def c2(port, log_dir, bin_dir, check):
    print("=" * 72 + "\n[C2] host talks to a villager (host behavior unchanged)")
    h, c, alive = run_scenario(port, "c2", log_dir, bin_dir, {"PC_FORCE_TALK_VILLAGER": "1"}, None, "host", 240.0)
    if h is None:
        check("C2 host+client reached the field (retried boot)", False)
        return
    check("C2 (9) host talk: ListenAble reached", re.search(r"TALKHOOK\] host ListenAble reached", h) is not None)
    check("C2 (9) host talk: dialogue ended and the host walked and opened the inventory afterwards",
          "TALKHOOK] host TALK_END" in h and re.search(r"host MOVE_AFTER moved=([0-9.]+)", h) is not None and
          re.search(r"host INVENTORY_AFTER opened=1", h) is not None)
    check("C2 (9) no lease logging on the host (host is never a client consumer)", "[NPC][TALKLEASE]" not in h)
    check("C2 both processes alive", alive is not None and alive[0] and alive[1])


def c3(port, log_dir, bin_dir, check):
    print("=" * 72 + "\n[C3] NEGATIVE CONTROL: pre-fix gate (PC_NPC_TALKLEASE_DISABLE=1) must lock up")
    h, c, alive = run_scenario(port, "c3", log_dir, bin_dir, None,
                               {"PC_FORCE_TALK_VILLAGER": "1", "PC_NPC_TALKLEASE_DISABLE": "1"}, "client", 300.0)
    if c is None:
        check("C3 host+client reached the field (retried boot)", False)
        return
    check("C3 (negative control) client talk requested", "[NPC][TALKHOOK] client talk requested" in c)
    check("C3 (negative control) client is STUCK in TALK (main=65) and never reached ListenAble",
          re.search(r"TALKHOOK\] client STUCK: .*main=65", c) is not None and "ListenAble reached" not in c)
    check("C3 (negative control) no lease was ever taken", "[NPC][TALKLEASE] begin" not in c)


# --------------------------------------------------------------------------------------------------------------
# M9-C: host-authoritative client talk hold (NPC_TALK id 45). Scenarios H1/H2/H3 (+control), H6, H7.
#   H1  client talks (hook) -> client SEND begin, host BEGIN/HOLD, host villager stays stationary while held
#       (host DIAG position samples + begin/end pose) [REAL two-process, hook-driven]
#   H1C NEGATIVE CONTROL: client PC_NPC_TALKHOLD_DISABLE=1 (never sends BEGIN): no hold, the same villager MOVES
#       on the host during the client's lease window (window = host-log byte offsets captured when the client
#       log shows the lease begin/end, so +-0.2 s)
#   H2  dialogue completes -> client SEND end, host END + RELEASE, host villager no longer held afterwards
#   H3  client "pose pop" at lease end: the client's local copy stays at the pose it had at talk start (the talk
#       action stops it), then snaps to the host pose, so pop == host displacement over the lease. Derived from
#       the host samples (hold: ~0, control: >0); NOT measured directly on the client [DERIVED]
#   H4  B-cancel: NOT RUN (the hook only alternates A/B pulses and has no early-cancel option; END is the same
#       lease-falling-edge code path as H2)
#   H5  leave-range: NOT APPLICABLE -- mPlayer_INDEX_TALK locks the player (the stick has no effect until
#       TALK_END; C1 (7) only moves after the dialogue), so 'walking away mid-talk' cannot happen in vanilla
#   H6  client killed mid-talk -> host peer reset releases the hold, villager no longer held [REAL two-process]
#   H7  PROTOCOL TESTED (real host binary, scripted FakeClients): two peers on one villager (peer mask, release
#       only after both END), stale seq, END of the wrong peer, bad npc_id, bad slot, non-town scene, peer
#       disconnect release + seq restart on reconnect; H7E: 30 s expiry shortened with PC_NPC_TALKHOLD_TIMEOUT_MS
# --------------------------------------------------------------------------------------------------------------
import struct

NPC_TALK_FMT = "<BBBBHH"
FAKE_SCENE_FMT = "<BBBBHHI"
SCENE_FG, SCENE_SHOP0 = 7, 9


def npc_talk_msg(slot, npc_id, begin, seq):
    return struct.pack(NPC_TALK_FMT, L.PC_NETGAME_MSG_NPC_TALK, slot & 0xFF, 1 if begin else 0, 0,
                       npc_id & 0xFFFF, seq & 0xFFFF)


def scene_msg(scene_id, seq):
    return struct.pack(FAKE_SCENE_FMT, L.PC_NETGAME_MSG_PLAYER_SCENE, 0, scene_id, L.PC_NETGAME_SCENE_FLAG_IN_TOWN,
                       0, 0, seq)


def diag_samples(text, slot):
    out = []
    for m in re.finditer(r"\[NPC\]\[TALKHOLD\]\[DIAG\] slot=%d held=(\d) pos=\(([-0-9.]+),([-0-9.]+)\)" % slot, text):
        out.append((int(m.group(1)), float(m.group(2)), float(m.group(3))))
    return out


def dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def run_hold_pair(port, name, log_dir, bin_dir, client_env, kill_when=None, post_s=12.0, timeout=240.0):
    """Real host + hook-driven client. Records the host-log byte offset when the client's lease begins/ends.
    kill_when: regex on the HOST log; when it appears the client process is killed (H6)."""
    host_env = {"PC_NPC_TALKHOLD_DIAG": "1"}
    host, cl = boot_pair(port, name, log_dir, bin_dir, host_env, client_env)
    if host is None:
        return None
    info = {"off_begin": None, "off_end": None, "killed": False}
    try:
        deadline = time.monotonic() + timeout
        end_seen_at = None
        while time.monotonic() < deadline:
            ctext = cl.log_text()
            if info["off_begin"] is None and "[NPC][TALKLEASE] begin" in ctext:
                info["off_begin"] = len(host.log_text())
            if info["off_end"] is None and "[NPC][TALKLEASE] end" in ctext:
                info["off_end"] = len(host.log_text())
                end_seen_at = time.monotonic()
            mk = re.search(kill_when, host.log_text()) if (kill_when and not info["killed"]) else None
            if mk:
                # H6: prove the hold is ACTIVE at the moment of the kill (not just that it began): host gate begun
                # (TALKHOLD begin), no END/RELEASE/EXPIRE for the slot since the HOLD line, client lease still open.
                slot = mk.groupdict().get("slot")
                off_hold = mk.start()
                info["off_hold"] = off_hold
                info["slot"] = slot

                def hold_active():
                    ht, ct = host.log_text(), cl.log_text()
                    region = ht[off_hold:]  # only this attempt's hold: from its HOLD line onward
                    begins = [m_.start() for m_ in re.finditer(r"\[NPC\]\[TALKHOLD\] begin slot=%s " % slot, region)]
                    ends = [m_.start() for m_ in re.finditer(r"\[NPC\]\[TALKHOLD\] end slot=%s " % slot, region)]
                    gate = bool(begins) and (not ends or begins[-1] > ends[-1])  # last begin after last end
                    ended = re.search(r"\[NPC\]\[TALKNET\] (?:END peer=\d+ slot=%s |RELEASE slot=%s |EXPIRE slot=%s )" %
                                      (slot, slot, slot), region) is not None
                    lease = ct.rfind("[NPC][TALKLEASE] begin") > ct.rfind("[NPC][TALKLEASE] end")
                    return gate and lease and not ended, len(ht)
                t_gate = time.monotonic() + 6.0
                active, off = hold_active()
                while not active and time.monotonic() < t_gate:
                    time.sleep(0.1)
                    active, off = hold_active()
                active, off = hold_active()  # final check immediately before the kill
                info["active_at_kill"] = active
                info["off_kill"] = off
                cl.stop()
                info["killed"] = True
                end_seen_at = time.monotonic()
            if end_seen_at is not None and time.monotonic() - end_seen_at > post_s:
                break
            if re.search(TERMINAL_RX, ctext) and end_seen_at is None and not kill_when:
                break  # talk ended without a lease end line (should not happen)
            if not cl.alive() and not info["killed"]:
                break
            time.sleep(0.2)
        if kill_when and info["killed"]:
            # condition-based wait (transport timeout is 5 s): the peer-reset RELEASE must appear AFTER the kill offset
            t_rel = time.monotonic() + 15.0
            while time.monotonic() < t_rel:
                if re.search(r"\[NPC\]\[TALKNET\] RELEASE slot=\d+ npc=0x[0-9A-F]+ \(peer \d+ reset\)",
                             host.log_text()[info["off_kill"]:]):
                    break
                time.sleep(0.2)
            time.sleep(post_s)
        info["host"] = host.log_text()
        info["client"] = cl.log_text()
        info["alive"] = (host.alive(), cl.alive())
        return info
    finally:
        cl.stop()
        host.stop()


def host_villagers(host):
    """(slot, npc_id) of every regular villager actor the host spawned, from its own log."""
    seen = {}
    for m in re.finditer(r"\[NPC\] regular villager actor spawned slot (\d+) npc_id 0x([0-9A-Fa-f]+)", host.log_text()):
        seen[int(m.group(1))] = int(m.group(2), 16)
    return sorted(seen.items())


def hold_began_walking(h, c):
    """True iff the host's '[NPC][TALKHOLD] begin' line for the client's talked-to slot shows a walking villager.
    Enums (include/ac_npc.h): act_idx aNPC_ACT_WALK == 1; action aNPC_ACTION_TYPE_WALK..WALK_AI == 5..8."""
    ms = re.search(r"\[NPC\]\[TALKHOOK\] client selected villager slot=(\d+)", c)
    if not ms:
        return False
    mw = re.search(r"\[NPC\]\[TALKHOLD\] begin slot=%s pos=\([-0-9.]+,[-0-9.]+\) action=(\d+) act_idx=(\d+)" %
                   ms.group(1), h)
    return mw is not None and int(mw.group(2)) == 1 and 5 <= int(mw.group(1)) <= 8


def h1_h2_h3(port, log_dir, bin_dir, check):
    print("=" * 72 + "\n[H1/H2/H3] client talk -> host holds the villager, then releases")
    info = None
    walking = False
    for attempt in range(3):  # the hook's villager/time is random: need one run where X was mid-walk at hold begin
        info = run_hold_pair(port, "h1" if attempt == 0 else "h1_r%d" % attempt, log_dir, bin_dir,
                             {"PC_FORCE_TALK_VILLAGER": "1"})
        if info is None:
            check("H1 host+client reached the field (retried boot)", False)
            return
        walking = hold_began_walking(info["host"], info["client"])
        print("    H1 attempt %d: villager walking at hold begin = %s" % (attempt, walking))
        if walking:
            break
    h, c = info["host"], info["client"]
    check("H1 (0) PRECONDITION: the villager was walking (act_idx=aNPC_ACT_WALK=1, action in WALK..WALK_AI=5..8) "
          "when the hold began -- an idle villager would make the stationary check vacuous (<= 3 attempts)", walking)
    ms = re.search(r"\[NPC\]\[TALKHOOK\] client selected villager slot=(\d+)", c)
    slot = int(ms.group(1)) if ms else -1
    check("H1 hook selected a regular villager on the client (slot=%d)" % slot, slot >= 0)
    check("H1 (1) client logged SEND begin for that slot",
          re.search(r"\[NPC\]\[TALKNET\] SEND begin slot=%d npc=0x[0-9A-F]+ seq=\d+" % slot, c) is not None)
    check("H1 (2) host logged BEGIN peer=.. slot=%d" % slot,
          re.search(r"\[NPC\]\[TALKNET\] BEGIN peer=\d+ slot=%d npc=0x[0-9A-F]+" % slot, h) is not None)
    check("H1 (2b) host logged HOLD slot=%d with a non-zero peer mask" % slot,
          re.search(r"\[NPC\]\[TALKNET\] HOLD slot=%d npc=0x[0-9A-F]+ peers=0x(?!0000)[0-9A-F]+" % slot, h) is not None)
    mb = re.search(r"\[NPC\]\[TALKHOLD\] begin slot=%d pos=\(([-0-9.]+),([-0-9.]+)\)" % slot, h)
    me = re.search(r"\[NPC\]\[TALKHOLD\] end slot=%d pos=\(([-0-9.]+),([-0-9.]+)\)" % slot, h)
    check("H1 (3) host gate engaged for the slot (TALKHOLD begin line)", mb is not None)
    mw = re.search(r"\[NPC\]\[TALKHOLD\] begin slot=%d pos=\([-0-9.]+,[-0-9.]+\) action=(\d+) act_idx=(\d+)" % slot, h)
    print("    H1 INFO villager state when the hold began: action=%s act_idx=%s (act_idx 1 = aNPC_ACT_WALK: mid-walk "
          "villagers are frozen)" % (mw.group(1) if mw else "?", mw.group(2) if mw else "?"))
    held = [s for s in diag_samples(h, slot) if s[0] == 1]
    if mb and me:
        p0 = (float(mb.group(1)), float(mb.group(2)))
        p1 = (float(me.group(1)), float(me.group(2)))
        worst = max([dist(p0, (x, z)) for _, x, z in held] + [dist(p0, p1)])
        print("    H1 held samples=%d begin=%s end=%s max displacement while held=%.3f" % (len(held), p0, p1, worst))
        check("H1 (4) host villager stayed stationary while held (>= 2 samples, max displacement <= 0.5)",
              len(held) >= 2 and worst <= 0.5)
        check("H3 (derived) pose pop at lease end ~ host displacement over the lease: begin->end <= 0.5",
              dist(p0, p1) <= 0.5)
    else:
        check("H1 (4) host villager stayed stationary while held", False)
        check("H3 (derived) pose pop at lease end ~ host displacement over the lease", False)
    check("H2 (1) client logged SEND end", re.search(r"\[NPC\]\[TALKNET\] SEND end slot=%d" % slot, c) is not None)
    check("H2 (2) host logged END then RELEASE for the slot",
          re.search(r"\[NPC\]\[TALKNET\] END peer=\d+ slot=%d.*\n(?:.*\n)*?\[NPC\]\[TALKNET\] RELEASE slot=%d" %
                    (slot, slot), h) is not None)
    check("H2 (3) host gate disengaged (TALKHOLD end line)", me is not None)
    if me:
        tail = h[h.index(me.group(0)):]
        after = diag_samples(tail, slot)
        pe = (float(me.group(1)), float(me.group(2)))
        moved = max([dist(pe, (x, z)) for _, x, z in after] + [0.0])
        print("    H2 samples after release=%d max displacement from the release pose=%.2f" % (len(after), moved))
        check("H2 (4) after release the host samples show held=0 for the slot only (never held again)",
              len(after) >= 1 and all(s[0] == 0 for s in after))
        print("    H2 INFO villager moved again after release: %s (not asserted: an idle villager may legitimately "
              "stand)" % ("yes" if moved > 1.0 else "not within the %.0f s window" % 12.0))
    check("H1 client did not crash / both alive", info["alive"][0] and info["alive"][1])
    check("H1 client never logged a talk-hold send failure", "SEND begin FAILED" not in c and "SEND end FAILED" not in c)
    check("H1 host did not log a REJECT", "[NPC][TALKNET] REJECT" not in h)


def h1c_control(port, log_dir, bin_dir, check):
    print("=" * 72 + "\n[H1C] NEGATIVE CONTROL: PC_NPC_TALKHOLD_DISABLE=1 -- no BEGIN, the villager keeps moving")
    print("    (villager wandering is random: up to 3 runs, the control needs one where X is walking in the window)")
    moved_ok = False
    for attempt in range(3):
        info = run_hold_pair(port, "h1c" if attempt == 0 else "h1c_r%d" % attempt, log_dir, bin_dir,
                             {"PC_FORCE_TALK_VILLAGER": "1", "PC_NPC_TALKHOLD_DISABLE": "1"}, post_s=3.0)
        if info is None:
            check("H1C host+client reached the field (retried boot)", False)
            return
        h, c = info["host"], info["client"]
        ms = re.search(r"\[NPC\]\[TALKHOOK\] client selected villager slot=(\d+)", c)
        slot = int(ms.group(1)) if ms else -1
        if attempt == 0:
            check("H1C (1) client talked (lease began) but sent nothing",
                  "[NPC][TALKLEASE] begin" in c and "[NPC][TALKNET] SEND" not in c)
            check("H1C (2) host never held or even saw an NPC_TALK", "[NPC][TALKNET]" not in h and
                  "[NPC][TALKHOLD] begin" not in h)
        worst = -1.0
        if info["off_begin"] is not None and info["off_end"] is not None and info["off_end"] > info["off_begin"]:
            smp = diag_samples(h[info["off_begin"]:info["off_end"]], slot)
            if len(smp) >= 2:
                p0 = (smp[0][1], smp[0][2])
                worst = max(dist(p0, (x, z)) for _, x, z in smp)
        print("    H1C attempt %d: slot=%d max host displacement over the client's lease window=%.2f" %
              (attempt, slot, worst))
        if worst > 3.0:
            moved_ok = True
            break
    check("H1C (3) without the hold the host villager MOVED during the client's lease window (> 3 units) -- i.e. "
          "the H1 stationary result is the hold, not an idle villager", moved_ok)


def h6_killed_client(port, log_dir, bin_dir, check):
    print("=" * 72 + "\n[H6] client killed mid-talk -> the host's peer reset releases the hold")
    info = None
    for attempt in range(3):  # a precondition miss (hold not provably active at the kill) is not a product failure
        # PC_FORCE_TALK_HOLD_SECONDS keeps the client's dialogue open 25 s (< the 30 s host expiry; keepalive is 10 s),
        # so the dialogue cannot finish by itself before the kill.
        info = run_hold_pair(port, "h6" if attempt == 0 else "h6_r%d" % attempt, log_dir, bin_dir,
                             {"PC_FORCE_TALK_VILLAGER": "1", "PC_FORCE_TALK_HOLD_SECONDS": "25"},
                             kill_when=r"\[NPC\]\[TALKNET\] HOLD slot=(?P<slot>\d+) npc=0x[0-9A-F]+ peers=0x(?!0000)[0-9A-F]+",
                             post_s=8.0)
        if info is None:
            check("H6 host+client reached the field (retried boot)", False)
            return
        print("    H6 attempt %d: hold active at kill = %s" % (attempt, info.get("active_at_kill")))
        if info.get("active_at_kill"):
            break
    h = info["host"]
    check("H6 (0) PRECONDITION: hold provably active at the kill (TALKHOLD begin seen, no END/RELEASE/EXPIRE for the "
          "slot since HOLD, client lease still open; <= 3 attempts)", bool(info.get("active_at_kill")))
    check("H6 (1) hold was established before the kill", info["killed"] and "[NPC][TALKNET] BEGIN" in h)
    off_hold, off_kill = info.get("off_hold", 0), info.get("off_kill", 0)
    between = h[off_hold:off_kill]
    after_kill = h[off_kill:]
    mr = re.search(r"\[NPC\]\[TALKNET\] RELEASE slot=(\d+) npc=0x[0-9A-F]+ \(peer \d+ reset\)", after_kill)
    check("H6 (2) host released the hold from the peer reset (RELEASE ... (peer N reset)) after the kill offset",
          mr is not None)
    pre_release = after_kill[:after_kill.index(mr.group(0))] if mr else after_kill
    check("H6 (2b) the release was not a dialogue end or expiry (no TALKNET END peer= / EXPIRE between HOLD and the "
          "peer-reset RELEASE)",
          mr is not None and re.search(r"\[NPC\]\[TALKNET\] (?:END peer=\d+ slot=%s |EXPIRE slot=%s )" %
                                       (info.get("slot"), info.get("slot")), between + pre_release) is None)
    if mr:
        slot = int(mr.group(1))
        tail = h[off_kill + after_kill.index(mr.group(0)):]
        after = diag_samples(tail, slot)
        check("H6 (3) host villager no longer held afterwards (held=0 samples, no held=1)",
              len(after) >= 1 and all(s[0] == 0 for s in after))
    check("H6 (4) host process survived the client kill", info["alive"][0])


def h7_protocol(port, log_dir, bin_dir, check):
    print("=" * 72 + "\n[H7] PROTOCOL TESTED: two scripted peers on one villager, stale/invalid/other-peer cases")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"],
                         log_path=os.path.join(log_dir, "h7_host.log"), bin_dir=bin_dir).start()
    clients = []
    try:
        if not host.wait_listening(60.0) or not host.boot_to_field(timeout=90.0, slot=0):
            check("H7 host reached the field", False)
            return
        a = L.FakeClient("A", "127.0.0.1", port)
        a.connect_and_ready()
        clients.append(a)
        b = L.FakeClient("B", "127.0.0.1", port)
        b.connect_and_ready()
        clients.append(b)
        slots = host_villagers(host)
        check("H7 host spawned regular villager actors (%d slots; from the host spawn log)" % len(slots), len(slots) > 0)
        if not slots:
            return
        slot, npc = slots[0]
        ma, mb_ = 1 << a.assigned_peer_id, 1 << b.assigned_peer_id
        time.sleep(0.5)

        def mark():
            return len(host.log_text())

        def seen(rx, since, wait=2.0):
            return host.wait_for_log(rx, wait, since_offset=since) is not None

        def absent(rx, since, wait=0.8):
            time.sleep(wait)
            return re.search(rx, host.log_text()[since:]) is None

        a.send_reliable(scene_msg(SCENE_FG, 1))
        b.send_reliable(scene_msg(SCENE_FG, 1))
        time.sleep(0.5)
        m = mark()
        a.send_reliable(npc_talk_msg(slot, npc, True, 1))
        check("H7 (1) A begin accepted: BEGIN + HOLD peers=A only",
              seen(r"BEGIN peer=%d slot=%d npc=0x%04X" % (a.assigned_peer_id, slot, npc), m) and
              seen(r"HOLD slot=%d npc=0x%04X peers=0x%04X" % (slot, npc, ma), m))
        m = mark()
        b.send_reliable(npc_talk_msg(slot, npc, True, 1))
        check("H7 (2) B begin on the same villager: DENIED (Patch 8: the lease is exclusive, A keeps it)", seen(
            r"DENIED peer=%d slot=%d npc=0x%04X" % (b.assigned_peer_id, slot, npc), m))
        m = mark()
        a.send_reliable(npc_talk_msg(slot, npc, False, 2))
        check("H7 (3) A end: the lease is RELEASED (exclusive owner ended)",
              seen(r"RELEASE slot=%d npc=0x%04X" % (slot, npc), m))
        m = mark()
        a.send_reliable(npc_talk_msg(slot, npc, True, 1))  # stale (<= last seq 2)
        check("H7 (4) stale seq begin rejected and does NOT resurrect A's hold",
              seen(r"REJECT peer=%d slot=%d .*stale" % (a.assigned_peer_id, slot), m) and
              absent(r"HOLD slot=%d npc=0x%04X peers=0x%04X" % (slot, npc, ma), m, 0.3))
        m = mark()
        a.send_reliable(npc_talk_msg(slot, npc, False, 3))  # A is not holding: must not clear B
        time.sleep(0.8)
        check("H7 (5) END from a peer that holds nothing is ignored (no RELEASE)",
              absent(r"RELEASE slot=%d" % slot, m, 0.1))
        m = mark()
        b.send_reliable(npc_talk_msg(slot, npc, True, 2))
        check("H7 (5b) B can take the villager now that A released it", seen(r"HOLD slot=%d npc=0x%04X peers=0x%04X" % (slot, npc, mb_), m))
        m = mark()
        a.send_reliable(npc_talk_msg(slot, (npc ^ 0x0001) & 0xFFFF, True, 4))
        check("H7 (6) wrong npc_id rejected", seen(r"REJECT peer=%d slot=%d .*npc_id" % (a.assigned_peer_id, slot), m))
        m = mark()
        a.send_reliable(npc_talk_msg(ANIMAL_NUM_MAX_PY, npc, True, 5))
        check("H7 (7) slot out of range rejected", seen(r"REJECT peer=%d slot=%d .*slot" % (
            a.assigned_peer_id, ANIMAL_NUM_MAX_PY), m))
        m = mark()
        a.send_reliable(scene_msg(SCENE_SHOP0, 2))
        time.sleep(0.4)
        a.send_reliable(npc_talk_msg(slot, npc, True, 6))
        check("H7 (8) begin from a peer not in the town field scene rejected",
              seen(r"REJECT peer=%d slot=%d .*town" % (a.assigned_peer_id, slot), m))
        a.send_reliable(scene_msg(SCENE_FG, 3))
        time.sleep(0.4)
        m = mark()
        b.send_reliable(npc_talk_msg(slot, npc, False, 3))
        check("H7 (9) B end: END + RELEASE",
              seen(r"END peer=%d slot=%d" % (b.assigned_peer_id, slot), m) and
              seen(r"RELEASE slot=%d npc=0x%04X" % (slot, npc), m))
        # leaving the town field releases the peer's hold; disconnect releases too; reconnect restarts the seq
        m = mark()
        a.send_reliable(npc_talk_msg(slot, npc, True, 7))
        time.sleep(0.5)
        a.send_reliable(scene_msg(SCENE_SHOP0, 4))
        check("H7 (10) peer announcing a non-town scene releases its hold (left the town field)",
              seen(r"RELEASE slot=%d npc=0x%04X \(peer %d left the villager's surroundings\)" % (slot, npc, a.assigned_peer_id), m))
        a.send_reliable(scene_msg(SCENE_FG, 5))
        time.sleep(0.4)
        m = mark()
        a.send_reliable(npc_talk_msg(slot, npc, True, 8))
        seen(r"HOLD slot=%d" % slot, m)
        a.disconnect()
        check("H7 (11) peer disconnect releases its hold (RELEASE ... reset)",
              seen(r"RELEASE slot=%d npc=0x%04X \(peer %d reset\)" % (slot, npc, a.assigned_peer_id), m, 15.0))
        a2 = L.FakeClient("A2", "127.0.0.1", port)
        a2.connect_and_ready()
        clients.append(a2)
        a2.send_reliable(scene_msg(SCENE_FG, 1))
        time.sleep(0.4)
        m = mark()
        a2.send_reliable(npc_talk_msg(slot, npc, True, 1))
        check("H7 (12) a reconnected client restarting at seq 1 is accepted (per-peer seq record was reset)",
              seen(r"BEGIN peer=%d slot=%d" % (a2.assigned_peer_id, slot), m))
        check("H7 host alive throughout", host.alive())
    finally:
        for cl_ in clients:
            try:
                cl_.close()
            except Exception:
                pass
        host.stop()
    h7_keepalive(port, log_dir, bin_dir, check)


def h7_keepalive(port, log_dir, bin_dir, check):
    print("-" * 72 + "\n[H7 keepalive] PROTOCOL TESTED: newer-seq BEGIN repeats every 1 s keep a 3 s hold alive")
    host = L.HostProcess(port=port + 20, extra_args=["--bootstrap-resident", "0"],
                         log_path=os.path.join(log_dir, "h7k_host.log"), bin_dir=bin_dir,
                         env={"PC_NPC_TALKHOLD_TIMEOUT_MS": "3000"}).start()
    a = None
    try:
        if not host.wait_listening(60.0) or not host.boot_to_field(timeout=90.0, slot=0):
            check("H7 keepalive host reached the field", False)
            return
        a = L.FakeClient("A", "127.0.0.1", port + 20)
        a.connect_and_ready()
        slots = host_villagers(host)
        if not slots:
            check("H7 keepalive villager table received", False)
            return
        slot, npc = slots[0]
        a.send_reliable(scene_msg(SCENE_FG, 1))
        time.sleep(0.5)
        m = len(host.log_text())
        seq = 1
        a.send_reliable(npc_talk_msg(slot, npc, True, seq))
        got_hold = host.wait_for_log(r"HOLD slot=%d npc=0x%04X" % (slot, npc), 3.0, since_offset=m) is not None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 7.0:  # > 2x the 3 s timeout
            time.sleep(1.0)
            seq += 1
            a.send_reliable(npc_talk_msg(slot, npc, True, seq))
        txt = host.log_text()[m:]
        check("H7 keepalive: 1 s repeats kept the hold past 3 s (7 s): HOLD once, no EXPIRE, no REJECT, one BEGIN line",
              got_hold and len(re.findall(r"HOLD slot=%d" % slot, txt)) == 1 and "EXPIRE" not in txt and
              "REJECT" not in txt and len(re.findall(r"BEGIN peer=\d+ slot=%d" % slot, txt)) == 1)
        m2 = len(host.log_text())
        t1 = time.monotonic()
        got_exp = host.wait_for_log(r"EXPIRE slot=%d npc=0x%04X" % (slot, npc), 10.0, since_offset=m2) is not None
        dt = time.monotonic() - t1
        check("H7 keepalive: stopping the repeats -> EXPIRE ~3 s later (%.1f s)" % dt, got_exp and 1.0 <= dt <= 6.0)
        check("H7 keepalive host alive", host.alive())
    finally:
        if a is not None:
            try:
                a.close()
            except Exception:
                pass
        host.stop()


ANIMAL_NUM_MAX_PY = 15


def h7e_expiry(port, log_dir, bin_dir, check):
    print("=" * 72 + "\n[H7E] PROTOCOL TESTED: safety expiry (PC_NPC_TALKHOLD_TIMEOUT_MS=3000 on the host)")
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"],
                         log_path=os.path.join(log_dir, "h7e_host.log"), bin_dir=bin_dir,
                         env={"PC_NPC_TALKHOLD_TIMEOUT_MS": "3000"}).start()
    a = None
    try:
        if not host.wait_listening(60.0) or not host.boot_to_field(timeout=90.0, slot=0):
            check("H7E host reached the field", False)
            return
        a = L.FakeClient("A", "127.0.0.1", port)
        a.connect_and_ready()
        slots = host_villagers(host)
        if not slots:
            check("H7E villager table received", False)
            return
        slot, npc = slots[0]
        a.send_reliable(scene_msg(SCENE_FG, 1))
        time.sleep(0.5)
        m = len(host.log_text())
        t0 = time.monotonic()
        a.send_reliable(npc_talk_msg(slot, npc, True, 1))
        got_hold = host.wait_for_log(r"HOLD slot=%d npc=0x%04X" % (slot, npc), 3.0, since_offset=m) is not None
        got_exp = host.wait_for_log(r"EXPIRE slot=%d npc=0x%04X" % (slot, npc), 15.0, since_offset=m) is not None
        dt = time.monotonic() - t0
        check("H7E hold set, then EXPIRE logged once after ~3 s with no END (%.1f s)" % dt,
              got_hold and got_exp and 2.5 <= dt <= 10.0)
        time.sleep(1.0)
        check("H7E EXPIRE logged exactly once", len(re.findall(r"EXPIRE slot=%d" % slot, host.log_text()[m:])) == 1)
    finally:
        if a is not None:
            try:
                a.close()
            except Exception:
                pass
        host.stop()


def h8_keepalive(port, log_dir, bin_dir, check, refresh_ms, control):
    tag = "H8C" if control else "H8"
    print("=" * 72 + "\n[%s] %s: host expiry 3000 ms, client dialogue held open 8 s, client refresh=%s ms" %
          (tag, "NEGATIVE CONTROL (no keepalive)" if control else "keepalive keeps the hold", refresh_ms))
    host_env = {"PC_NPC_TALKHOLD_DIAG": "1", "PC_NPC_TALKHOLD_TIMEOUT_MS": "3000"}
    cl_env = {"PC_FORCE_TALK_VILLAGER": "1", "PC_FORCE_TALK_HOLD_SECONDS": "8", "PC_NPC_TALKHOLD_DIAG": "1", "PC_NPC_TALKHOLD_REFRESH_MS": refresh_ms}
    host, cl = boot_pair(port, tag.lower(), log_dir, bin_dir, host_env, cl_env)
    if host is None:
        check("%s host+client reached the field (retried boot)" % tag, False)
        return
    try:
        deadline = time.monotonic() + 120.0
        end_seen = None
        while time.monotonic() < deadline:
            if end_seen is None and "[NPC][TALKLEASE] end" in cl.log_text():
                end_seen = time.monotonic()
            if end_seen is not None and time.monotonic() - end_seen > 3.0:
                break
            if not cl.alive():
                break
            time.sleep(0.3)
        h, c = host.log_text(), cl.log_text()
        alive = (host.alive(), cl.alive())
    finally:
        cl.stop()
        host.stop()
    ms = re.search(r"\[NPC\]\[TALKHOOK\] client selected villager slot=(\d+)", c)
    slot = int(ms.group(1)) if ms else -1
    check("%s hook selected a villager and the dialogue was held open (slot=%d)" % (tag, slot),
          slot >= 0 and "holding the dialogue open" in c)
    check("%s host logged HOLD for the slot exactly once" % tag,
          len(re.findall(r"\[NPC\]\[TALKNET\] HOLD slot=%d " % slot, h)) == 1)
    exp = re.findall(r"\[NPC\]\[TALKNET\] EXPIRE slot=%d " % slot, h)
    end_pos = h.find("[NPC][TALKNET] END peer")
    exp_pos = h.find("[NPC][TALKNET] EXPIRE slot=%d " % slot)
    if not control:
        check("%s no EXPIRE during the dialogue (keepalive refreshed the 3 s hold) and END + RELEASE at dialogue end" % tag,
              not exp and end_pos > 0 and re.search(r"\[NPC\]\[TALKNET\] RELEASE slot=%d npc=0x[0-9A-F]+\s*\n" % slot, h)
              is not None)
        check("%s client sent refreshes (diag) and host saw repeats without a REJECT" % tag,
              len(re.findall(r"SEND refresh slot=%d" % slot, c)) >= 3 and "[NPC][TALKNET] REJECT" not in h)
        mb = re.search(r"\[NPC\]\[TALKHOLD\] begin slot=%d pos=\(([-0-9.]+),([-0-9.]+)\)" % slot, h)
        held = [s for s in diag_samples(h, slot) if s[0] == 1]
        if mb:
            p0 = (float(mb.group(1)), float(mb.group(2)))
            worst = max([dist(p0, (x, z)) for _, x, z in held] + [0.0])
            check("%s host villager stationary across the >3 s hold (%d held samples, max displacement %.3f <= 0.5)" %
                  (tag, len(held), worst), len(held) >= 5 and worst <= 0.5)
        else:
            check("%s host gate engaged (TALKHOLD begin)" % tag, False)
    else:
        check("%s without refresh the host logged EXPIRE (before END) -- the keepalive is what prevents it" % tag,
              len(exp) == 1 and (end_pos < 0 or exp_pos < end_pos) and "SEND refresh" not in c)
    check("%s both processes alive" % tag, alive[0] and alive[1])


def h8(port, log_dir, bin_dir, check):
    h8_keepalive(port, log_dir, bin_dir, check, "1000", False)


def h8c(port, log_dir, bin_dir, check):
    h8_keepalive(port, log_dir, bin_dir, check, "0", True)


def is_fixture_dir(bin_dir):
    return bool(bin_dir) and os.path.basename(os.path.normpath(bin_dir)).startswith("bin_fixture4")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7811)
    ap.add_argument("--only", default="")
    ap.add_argument("--bin-dir", default=DEFAULT_BIN if os.path.isdir(DEFAULT_BIN) else None)
    args = ap.parse_args()
    results = []
    log_dir = os.path.join(HERE, "logs", "client_villager_talk")
    os.makedirs(log_dir, exist_ok=True)
    check = lambda d, c: L.check(d, c, results)
    with L.CloneSaveGuard(args.bin_dir):  # disposable saves restored afterwards
        for i, (name, fn) in enumerate((("C1", c1), ("C2", c2), ("C3", c3), ("H1", h1_h2_h3), ("H1C", h1c_control),
                                        ("H6", h6_killed_client), ("H7", h7_protocol), ("H7E", h7e_expiry),
                                        ("H8", h8), ("H8C", h8c))):
            if args.only and args.only != name:
                continue
            fn(args.port + i, log_dir, args.bin_dir, check)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
