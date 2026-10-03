#!/usr/bin/env python3
"""test_move_action_state_wire.py - M9-C Phase 2a: protocol v7 MOVE `action_state` (vanilla main index + 4-bit entry counter)
and the puppet state-row machinery that consumes it.

ONE real game process (the host, `--host --bootstrap-resident 0`, PC_PUPPET_DIAG=1 in its environment) plus scripted
FakeClients: "B" streams MOVE packets with chosen (main_index, entry_counter) pairs and is rendered as a puppet by the host
(the env-gated `[NET][PUPPET][DIAG] player N state main_index=I counter=C row=<name|fallback> ... latch=<on|off>` and
`... latch released (finished|timeout|interrupted|scene|snap|gap)` lines are the observable decisions); "C" only observes
what the host relays. A second boot (host hook `--scene-test-enter-shop`) covers the scene-generation recreate.

Tier labels (honest): PROTOCOL TESTED (wire, real host binary, scripted clients) for W1, W2 and V1; HOOK-DRIVEN (automated,
real process, NO visual verification) for P1..P3 and S2. Nothing here proves that a pose LOOKS right; that needs manual play.

  W1  the host accepts a MOVE with a valid action_state and relays it UNCHANGED (incl. all other fields) to another peer;
      the host's own MOVE carries a plausible action_state (main index 1..120) from the real sampler
  W2  malformed action_state (index 200 / 121, reserved bits 12..15 set): the MOVE is NOT rejected (position relayed
      intact) but the field is zeroed before relaying; index 120 (the maximum) passes; the puppet ignores it
  V1  a protocol-version-6 client is rejected with PROTOCOL_MISMATCH reporting the expected version (wire_baseline); test_version_mismatch.py passes
  P1  idle -> dig_scoop edge starts the row (latch=on); re-entry of the SAME index with a bumped counter restarts (a second
      edge line); a duplicate / stale-frame MOVE never produces an edge
  P2  one-shot latch: an idle snapshot arriving right after the swing does not cut the clip (fallback edge shows latch=on);
      the latch is released by 'finished' only after the clip had time to play
  P3  disconnect + reconnect: the new puppet has no latch
  S2  scene-generation recreate: a latched one-shot is NOT re-latched on the new actor; a steady (REPEAT) row starts again
  A1  (Phase 2b, S3) ALL 121 main indexes: every MOVE index 0..120 (incrementing counter) makes the puppet log row=<name> for a
      filled row or row=fallback for an explicit fallback row, matching the table the test PARSES from the C source
      (pc_remote_player.c s_state_rows: PCROW*/PCFB entries + the m_player.h enum); the table must cover 121 indexes
  A2  rapid back-to-back different states and a sequence of every latching one-shot do not wedge the puppet: after idle
      MOVEs the latch is released within a bounded time and a fresh one-shot still starts (HOOK-DRIVEN, no visual check)
  (all: host process alive, no Segmentation/assert/Unhandled text in its log)
Not covered here (SOURCE AUDITED only): the 120-frame safety timeout (no row's clip is long enough to need it), the snapshot
gap and teleport-snap releases, the client receive path (the host zeroes malformed fields before relaying, so a real client
never sees one; both paths share pcnetgame_sanitize_move_action()).

Usage: python test_move_action_state_wire.py [--port 8100] [--only s1|s2|s3]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import subprocess
import sys
import time

import net_spike_lib as L
import wire_baseline
from test_player_scene_real import boot_host
from test_puppet_held_item import make_b, wait_visual, host_position, clean

HERE = os.path.dirname(os.path.abspath(__file__))
IDLE, WALK = 0, 1
# vanilla mPlayer_INDEX_* used here (include/m_player.h, NUM = 121)
IDX_WAIT, IDX_SWING_AXE, IDX_SWING_FAN, IDX_DIG_SCOOP = 7, 37, 109, 58

STATE_RX = (r"\[NET\]\[PUPPET\]\[DIAG\] player %d state main_index=%s counter=%s row=(\S+) anim0=(-?\d+) anim1=(-?\d+) "
            r"part_table=(-?\d+) mode=(\w+) latch=(\w+)")
REL_RX = r"\[NET\]\[PUPPET\]\[DIAG\] player %d latch released \(%s\)"


class Streamer:
    """Streams MOVE packets for one FakeClient with explicit (main_index, counter) pairs."""

    def __init__(self, client, base=None):
        self.c = client
        self.x, self.y, self.z = base if base is not None else host_position(client)
        self.frame = 5000

    def send(self, main_index, counter, move_state=IDLE, kind=-1, n=1, seconds=0.0, action_state=None, speed=0.0,
             angle=0):
        end = time.monotonic() + seconds
        sent = 0
        while sent < n or time.monotonic() < end:
            self.frame += 1
            if action_state is None:
                self.c.send_move(self.frame, self.x, self.y, self.z, angle=angle, speed=speed, move_state=move_state,
                                 item_kind=kind, main_index=main_index, entry_counter=counter)
            else:
                self.c.send_move(self.frame, self.x, self.y, self.z, angle=angle, speed=speed, move_state=move_state,
                                 item_kind=kind, action_state=action_state)
            sent += 1
            L.pump_sleep(0.05, self.c.hub)
            if sent >= n and seconds <= 0.0:
                break


def relayed(observer, from_id):
    return [m.game for m in observer.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_MOVE, (L.CH_UNRELIABLE,)))
            if m.game is not None and m.game.net_player_id == from_id]


def state_rx(pid, idx=None, ctr=None):
    return STATE_RX % (pid, str(idx) if idx is not None else r"-?\d+", str(ctr) if ctr is not None else r"-?\d+")


def lines(text, pid, idx=None, ctr=None):
    return re.findall(state_rx(pid, idx, ctr), text)


def s1(port, log_dir, check):
    print("=" * 72 + "\n[S1] PROTOCOL/HOOK-DRIVEN: relay, malformed field, version, re-entry, latch, reconnect")
    host = boot_host(port, ["--authoritative-wildlife"], "action_s1", log_dir)
    if host is None:
        check("S1 host reached field", False)
        return
    b = c = b2 = None
    try:
        off = len(host.log_text())
        b = make_b(port)
        pid = b.assigned_peer_id
        c = L.FakeClient("C", "127.0.0.1", port)
        c.connect_and_ready()
        st = Streamer(b)
        st.send(IDX_WAIT, 1, n=10)  # puppet appears; the first pair on a fresh actor is adopted silently
        check("S1 puppet visual initialised for B", wait_visual(host, off))

        # ---- W1: valid action_state relayed unchanged ------------------------------------------------------------
        # review round 2 (R6-L): NON-default angle / speed / item kind so a host that zeroed them would fail W1
        W1_ANGLE, W1_SPEED, W1_KIND = 12345, 3.5, 9
        st.send(IDX_SWING_AXE, 5, move_state=6, kind=W1_KIND, speed=W1_SPEED, angle=W1_ANGLE, n=6)
        L.pump_sleep(0.5, c.hub)
        rel = [g for g in relayed(c, pid) if g.action_state == L.move_action_state(IDX_SWING_AXE, 5)]
        check("W1 host relays B's MOVE with action_state (main index 37, counter 5) unchanged to C (%d seen)" % len(rel),
              len(rel) >= 1)
        if rel:
            g = rel[-1]
            # review R2-L3: every field the MOVE carries is checked (the claim is "UNCHANGED"): sender id, move_state, item
            # kind, position (x/y/z), facing angle, speed and the action_state itself
            check("W1 relayed MOVE keeps every field (id=%d x=%.1f y=%.1f z=%.1f ms=%d kind=%d ang=%d speed=%.2f as=0x%04x)" %
                  (g.net_player_id, g.pos_x, g.pos_y, g.pos_z, g.move_state, g.item_kind, g.facing_angle, g.speed,
                   g.action_state),
                  g.net_player_id == pid and abs(g.pos_x - st.x) < 0.01 and abs(g.pos_y - st.y) < 0.01 and
                  abs(g.pos_z - st.z) < 0.01 and g.move_state == 6 and g.item_kind == W1_KIND and g.facing_angle == W1_ANGLE and
                  abs(g.speed - W1_SPEED) < 1e-3 and g.action_state == L.move_action_state(IDX_SWING_AXE, 5))
        hostmoves = relayed(c, L.PC_NETGAME_HOST_PLAYER_ID)
        # a valid sample may carry index 0 (a boot-time sample with a counter): the index is simply 0..120
        idxs = [g.action_state & 0xFF for g in hostmoves]
        print("INFO - host's own MOVE action_state samples:", sorted(set(hex(g.action_state) for g in hostmoves))[:6])
        check("W1 the host's own MOVE carries a plausible action_state (main index 0..120, reserved bits 0)",
              len(idxs) >= 1 and all(0 <= i <= 120 for i in idxs) and
              all((g.action_state & 0xF000) == 0 for g in hostmoves))

        # ---- W2: malformed -------------------------------------------------------------------------------------------
        st2_off = len(host.log_text())
        c_before = len(relayed(c, pid))
        for bad in (200, 121, 0x1025, 0x8000 | 37, 0xFFFF):
            st.send(0, 0, n=3, action_state=bad)
        st.send(IDX_WAIT, 6, n=2)
        L.pump_sleep(0.5, c.hub)
        newer = relayed(c, pid)[c_before:]
        bad_relayed = [g for g in newer if g.action_state not in (0, L.move_action_state(IDX_WAIT, 6))]
        check("W2 no malformed action_state is relayed (all zeroed; %d MOVEs relayed after the bad batch)" % len(newer),
              len(newer) >= 10 and not bad_relayed)
        check("W2 the malformed MOVEs were NOT dropped: position relayed intact",
              len(newer) >= 10 and all(abs(g.pos_x - st.x) < 0.01 for g in newer))
        check("W2 the host logged the zeroing (verbose)",
              host.wait_for_log(r"MOVE action_state 0x00c8 malformed", 5.0, since_offset=st2_off) is not None)
        st.send(120, 7, n=3)  # the maximum legal index passes untouched
        L.pump_sleep(0.4, c.hub)
        check("W2 index 120 (mPlayer_INDEX_NUM-1) is a legal value and is relayed",
              any(g.action_state == L.move_action_state(120, 7) for g in relayed(c, pid)))
        txt = host.log_text()
        check("W2 the puppet never acted on index 200/121 (no state line with those indexes)",
              not lines(txt, pid, 200) and not lines(txt, pid, 121))
        st.send(IDX_WAIT, 8, n=4)

        # ---- V1: protocol version 6 rejected ---------------------------------------------------------------------
        old = L.FakeClient("V6", "127.0.0.1", port)
        rejected, ver = False, None
        try:
            old.connect_and_ready(protocol_version=6, quiet=True)
        except L.HandshakeRejected as e:
            rejected = e.reject.reason == L.PC_NETGAME_REJECT_PROTOCOL_MISMATCH
            ver = e.reject.expected_protocol_version
        check("V1 a protocol-version-6 client is rejected with PROTOCOL_MISMATCH", rejected)
        check("V1 the host reports the required protocol version %d (got %s)" % (wire_baseline.EXPECTED_PROTOCOL_VERSION, ver),
              ver == wire_baseline.EXPECTED_PROTOCOL_VERSION)
        # the subprocess has its own default-identity allocator: steer it to a resident that neither b nor c holds
        used = {bytes(x.player.player_name) for x in (b, c)}
        spare = next((i for i, pl, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT
                      and bytes(pl.player_name) not in used), None)
        sub_env = dict(os.environ)
        if spare is not None:
            sub_env["NET_SPIKE_DEFAULT_RESIDENT"] = str(spare)
        r = subprocess.run([sys.executable, os.path.join(HERE, "test_version_mismatch.py"), "127.0.0.1", str(port)],
                           capture_output=True, text=True, timeout=120, env=sub_env)
        check("V1 test_version_mismatch.py passes against the booted v7 host (exit %d)" % r.returncode, r.returncode == 0)

        # ---- P1: restart edge on re-entry, duplicates/stale never edge --------------------------------------------
        st.send(IDX_WAIT, 9, n=8)
        off = len(host.log_text())
        st.send(IDX_DIG_SCOOP, 2, n=2)
        L.pump_sleep(0.3, b.hub)
        mo = host.wait_for_log(state_rx(pid, IDX_DIG_SCOOP, 2), 8.0, since_offset=off)
        check("P1 idle -> dig_scoop: entry edge starts row dig_scoop (anim0=78 DIG1, mode=stop, latch=on) (got %s)" %
              str(None if mo is None else mo.groups()),
              mo is not None and mo.group(1) == "dig_scoop" and mo.group(2) == "78" and mo.group(5) == "stop" and
              mo.group(6) == "on")
        st.send(IDX_DIG_SCOOP, 2, n=20)  # same pair, 20 more packets: no extra edge
        check("P1 the same (index, counter) pair repeated does not produce another edge line",
              len(lines(host.log_text()[off:], pid, IDX_DIG_SCOOP, 2)) == 1)
        st.send(IDX_DIG_SCOOP, 3, n=2)  # re-entry of the SAME index (next counter) -> restart edge
        check("P1 re-entry of the same index with counter 3 produces a restart edge line",
              host.wait_for_log(state_rx(pid, IDX_DIG_SCOOP, 3), 8.0, since_offset=off) is not None)
        n_before = len(lines(host.log_text(), pid))
        # stale: an old frame number with a different counter -> rejected by the sender-frame dedupe
        stale = L.build_move(st.frame - 3, st.x, st.y, st.z, angle=0, speed=0.0, move_state=6, item_kind=-1,
                             main_index=IDX_DIG_SCOOP, entry_counter=9)
        for _ in range(4):
            b.send_unreliable(stale)
            L.pump_sleep(0.05, b.hub)
        dup = L.build_move(st.frame, st.x, st.y, st.z, angle=0, speed=0.0, move_state=6, item_kind=-1,
                           main_index=IDX_DIG_SCOOP, entry_counter=11)  # duplicate frame number, different payload
        b.send_unreliable(dup)
        L.pump_sleep(0.5, b.hub)
        txt = host.log_text()
        check("P1 stale-frame and duplicate-frame MOVEs (counter 9 / 11) do not produce an edge",
              not lines(txt, pid, IDX_DIG_SCOOP, 9) and not lines(txt, pid, IDX_DIG_SCOOP, 11) and
              len(lines(txt, pid)) == n_before)
        check("P1 the host log shows the stale/duplicate rejection", "rejected stale/duplicate/reordered sample" in txt)

        # ---- P2: one-shot latch ------------------------------------------------------------------------------------
        st.send(IDX_WAIT, 12, seconds=3.0)  # let the previous clip end and settle
        off = len(host.log_text())
        st.send(IDX_SWING_AXE, 13, move_state=6, n=2)       # swing starts ...
        st.send(IDX_WAIT, 14, move_state=IDLE, n=1)         # ... and an idle snapshot follows ~0.1 s later
        t_idle = time.monotonic()
        mo = host.wait_for_log(state_rx(pid, IDX_WAIT, 14), 8.0, since_offset=off)
        check("P2 the idle edge after the swing is a fallback state held by the latch (latch=on) (got %s)" %
              str(None if mo is None else mo.groups()),
              mo is not None and mo.group(1) == "fallback" and mo.group(6) == "on")
        t_rel = None
        for _ in range(100):  # keep streaming idle so the puppet is driven and snapshots stay fresh
            st.send(IDX_WAIT, 14, n=1)
            if re.search(REL_RX % (pid, r"\w+"), host.log_text()[off:]):
                t_rel = time.monotonic()
                break
        rel = re.search(REL_RX % (pid, r"(\w+)"), host.log_text()[off:])
        check("P2 the latch was released with reason 'finished' (got %s)" % str(None if rel is None else rel.group(1)),
              rel is not None and rel.group(1) == "finished")
        dt = (t_rel - t_idle) if t_rel is not None else -1.0
        print("INFO - P2 latch held %.2f s after the idle edge (AXE_SWING1 = 30 frames at 0.5x = ~1 s of game time)" % dt)
        check("P2 the clip was allowed to play (latch not released within 0.25 s of the idle edge)", dt >= 0.25)

        # ---- P3: disconnect/reconnect ----------------------------------------------------------------------------
        st.send(IDX_SWING_AXE, 3, move_state=6, n=2)
        st.send(IDX_WAIT, 4, n=1)  # latched fallback pending
        b.disconnect()
        L.pump_sleep(1.0, b.hub)
        b.close()
        b = None
        off = len(host.log_text())
        b2 = make_b(port, "B2")
        pid2 = b2.assigned_peer_id
        st2 = Streamer(b2)
        st2.send(IDX_WAIT, 1, n=10)
        wait_visual(host, off, 20.0)
        st2.send(IDX_WAIT, 2, n=4)
        mo = host.wait_for_log(state_rx(pid2, IDX_WAIT, 2), 10.0, since_offset=off)
        check("P3 reconnect: the NEW puppet's first edge is a plain fallback with latch=off (got %s)" %
              str(None if mo is None else mo.groups()), mo is not None and mo.group(1) == "fallback" and mo.group(6) == "off")
        check("P3 reconnect: no stale latch release was attributed to the new puppet",
              not re.search(REL_RX % (pid2, r"\w+"), host.log_text()[off:]))
        clean(host, check, "S1")
    finally:
        for x in (b, c, b2):
            if x is not None:
                try:
                    x.close()
                except Exception:
                    pass
        host.stop()


def s2(port, log_dir, check):
    print("=" * 72 + "\n[S2] HOOK-DRIVEN: scene-generation recreate (host enters SHOP0 and returns) with a swing latch / steady row")
    host = boot_host(port, ["--scene-test-enter-shop", "--scene-test-leave-after", "900"], "action_s2", log_dir)
    if host is None:
        check("S2 host reached field", False)
        return
    d = f = None
    try:
        off = len(host.log_text())
        d = make_b(port, "D")
        f = make_b(port, "F")
        pd, pf = d.assigned_peer_id, f.assigned_peer_id
        sd, sf = Streamer(d), Streamer(f)
        sd.send(IDX_WAIT, 1, n=10)
        sf.send(IDX_SWING_FAN, 1, n=10)  # first pair on a fresh actor with a REPEAT row: started immediately
        t_vis = time.monotonic()
        while time.monotonic() - t_vis < 40.0:  # BOTH puppets need their visuals (state rows only run once initialised)
            if len(re.findall(r"\[NET\]\[REMOTE\]\[DIAG\] visual initialized", host.log_text()[off:])) >= 2:
                break
            sd.send(IDX_WAIT, 1, n=1)
            sf.send(IDX_SWING_FAN, 1, n=1)
        sd.send(IDX_SWING_AXE, 2, move_state=6, n=2)  # one-shot edge -> logged once
        t0 = time.monotonic()
        entered = left = False
        while time.monotonic() - t0 < 150.0:
            sd.send(IDX_SWING_AXE, 2, move_state=6, n=1)
            sf.send(IDX_SWING_FAN, 1, move_state=6, n=1)
            txt = host.log_text()
            if not entered and "hook: goto_other_scene(SCENE_SHOP0) res=1" in txt:
                entered = True
            if entered and "hook: goto_other_scene(exit) res=1" in txt:
                left = True
                break
            if not host.alive():
                break
        check("S2 host entered the shop and requested the real exit (hook-driven)", entered and left)
        t1 = time.monotonic()
        while time.monotonic() - t1 < 10.0:
            sd.send(IDX_SWING_AXE, 2, move_state=6, n=1)
            sf.send(IDX_SWING_FAN, 1, move_state=6, n=1)
        txt = host.log_text()
        n_axe = len(lines(txt, pd, IDX_SWING_AXE, 2))
        n_fan = len(lines(txt, pf, IDX_SWING_FAN, 1))
        print("INFO - S2 state lines: swing_axe edge lines=%d, swing_fan (steady row) start lines=%d" % (n_axe, n_fan))
        check("S2 the one-shot swing edge was logged exactly once (not re-latched by the recreated puppet) (%d)" % n_axe,
              n_axe == 1)
        check("S2 the steady (REPEAT) row started again on the recreated puppet (>= 2 start lines, per-actor state) (%d)" %
              n_fan, n_fan >= 2)
        clean(host, check, "S2")
    finally:
        for x in (d, f):
            if x is not None:
                try:
                    x.close()
                except Exception:
                    pass
        host.stop()


def parse_row_table():
    """{main index: name or None (fallback)} parsed from the SAME source the game is built from: the enum in
    include/m_player.h and the PCROW/PCROW_BODY/PCROW_ITEM("name", ...) / PCFB("why") initialisers of s_state_rows."""
    root = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
    hdr = open(os.path.join(root, "include", "m_player.h"), encoding="utf-8", errors="replace").read()
    enum = re.search(r"enum\s*\{\s*(mPlayer_INDEX_DMA,.*?mPlayer_INDEX_NUM)\s*,?\s*\}", hdr, re.S).group(1)
    names = [n.strip() for n in enum.split(",") if n.strip()]
    value = {n[len("mPlayer_INDEX_"):]: i for i, n in enumerate(names)}
    assert value.get("NUM") == 121 and value.get("DMA") == 0, value.get("NUM")
    src = open(os.path.join(root, "pc", "src", "pc_remote_player.c"), encoding="utf-8", errors="replace").read()
    table = {}
    for m in re.finditer(r"\[mPlayer_INDEX_(\w+)\]\s*=\s*(PCROW_BODY|PCROW_ITEM|PCROW|PCFB)\(\s*\"([^\"]*)\"", src):
        idx = value[m.group(1)]
        assert idx not in table, "duplicate row for " + m.group(1)
        table[idx] = m.group(3) if m.group(2) != "PCFB" else None
    return table


def s3(port, log_dir, check):
    print("=" * 72 + "\n[S3] HOOK-DRIVEN: all 121 main indexes (row name / explicit fallback), back-to-back states, one-shot sequence")
    table = parse_row_table()
    filled = sorted(i for i, n in table.items() if n is not None)
    check("A1 the parsed table covers every main index 0..120 exactly once (%d entries, %d filled rows, %d explicit fallbacks)" %
          (len(table), len(filled), len(table) - len(filled)), sorted(table) == list(range(121)))
    print("INFO - filled rows:", ", ".join("%d=%s" % (i, table[i]) for i in filled))
    host = boot_host(port, ["--authoritative-wildlife"], "action_s3", log_dir)
    if host is None:
        check("S3 host reached field", False)
        return
    b = None
    try:
        off = len(host.log_text())
        b = make_b(port)
        pid = b.assigned_peer_id
        st = Streamer(b)
        st.send(IDX_WAIT, 1, n=10)
        check("S3 puppet visual initialised for B", wait_visual(host, off))
        scan_off = len(host.log_text())
        ctr = 1
        for idx in range(121):
            ctr = (ctr % 15) + 1
            st.send(idx, ctr, n=6)  # ~0.3 s per state: every pair outlasts the puppet's interpolation delay
        L.pump_sleep(0.5, b.hub)
        txt = host.log_text()[scan_off:]
        bad, missing, stop_rows = [], [], []
        for idx in range(121):
            seen = lines(txt, pid, -1 if idx == 0 else idx)  # wire index 0 = "no info": logged as main_index=-1
            if not seen:
                missing.append(idx)
                continue
            want = table[idx] if table[idx] is not None else "fallback"
            if any(g[0] != want for g in seen):
                bad.append((idx, want, sorted(set(g[0] for g in seen))))
            if table[idx] is not None and any(g[4] == "stop" for g in seen):
                stop_rows.append(idx)
        print("INFO - S3 state lines logged: %d; STOP (latching) rows seen: %s" % (len(lines(txt, pid)), stop_rows))
        check("A1 every index 0..120 produced a state line (missing: %s)" % missing, not missing)
        check("A1 every state line names the expected row or 'fallback' (mismatches: %s)" % bad[:6], not bad)
        check("A1 at least one latching (STOP) filled row was exercised (%d)" % len(stop_rows), len(stop_rows) >= 10)

        # ---- A2: rapid back-to-back different states + every one-shot in a row, then idle ---------------------------
        rapid = [37, 58, 44, 49, 89, 96, 98, 105, 107, 30, 11, 12, 103, 109, 85, 102, 81, 94, 7]
        for rnd in range(3):
            for idx in rapid:
                ctr = (ctr % 15) + 1
                st.send(idx, ctr, n=1)
        seq = stop_rows if stop_rows else [37]
        off = len(host.log_text())
        for rnd in range(2):
            for idx in seq:
                ctr = (ctr % 15) + 1
                st.send(idx, ctr, n=1)
        st.send(seq[-1], (ctr % 15) + 1, n=1)
        ctr = (ctr % 15) + 1
        off = len(host.log_text())  # only releases caused by the idle MOVEs count from here
        t0 = time.monotonic()
        released = None
        while time.monotonic() - t0 < 10.0:  # idle MOVEs only; the latch must let go (clip end or the 120-frame timeout)
            st.send(IDX_WAIT, ctr, n=1)
            rel = re.findall(REL_RX % (pid, r"(finished|timeout)"), host.log_text()[off:])  # tail edges may still log "interrupted"
            if rel:
                released = rel[-1]
                break
        check("A2 after the one-shot sequence + idle MOVEs the latch was released by the clip end or the safety timeout within 10 s (reason: %s)" % released,
              released in ("finished", "timeout"))
        L.pump_sleep(1.0, b.hub)
        off = len(host.log_text())
        ctr = (ctr % 15) + 1
        st.send(IDX_DIG_SCOOP, ctr, n=3)
        mo = host.wait_for_log(state_rx(pid, IDX_DIG_SCOOP, ctr), 8.0, since_offset=off)
        check("A2 the puppet is not wedged: a fresh dig_scoop edge still starts its row afterwards (got %s)" %
              str(None if mo is None else mo.groups()), mo is not None and mo.group(1) == "dig_scoop")
        clean(host, check, "S3")
    finally:
        if b is not None:
            try:
                b.close()
            except Exception:
                pass
        host.stop()


def main():
    L.require_test_bin_dir()  # review M1: refuse to run against the live build/save
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    os.environ["PC_PUPPET_DIAG"] = "1"  # net_spike_lib copies os.environ into the launched game process
    results = []
    log_dir = os.path.join(HERE, "logs", "m9c")
    os.makedirs(log_dir, exist_ok=True)
    check = lambda desc, cond: L.check(desc, cond, results)
    for i, fn in enumerate((s1, s2, s3)):
        if args.only and args.only != fn.__name__:
            continue
        fn(args.port + i, log_dir, check)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
