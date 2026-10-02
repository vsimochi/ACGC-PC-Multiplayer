#!/usr/bin/env python3
"""test_puppet_tool_fx.py - M9-C Phase 6a (remote held-tool sounds/effects, cosmetic only, protocol stays v7).

Real host process (`--host --bootstrap-resident 0`, PC_PUPPET_DIAG=1 in its environment) plus scripted FakeClients.  A peer
streams MOVE packets with a tool main index (+ entry counter, + the matching held item kind) near the host player.  The
observable decisions are the env-gated lines of pc_remote_player.c
    [NET][PUPPET][DIAG] player N state main_index=I counter=C row=<name> ...                    (entry edge, Phase 2a)
    [NET][PUPPET][DIAG] player N cosmetic effect=<name> frame=F pos=(x,z) attr=A budget=B
    [NET][PUPPET][DIAG] player N <sound name> sound label=L frame=F budget=B
    [NET][PUPPET][DIAG] cosmetic per-frame spawn peak=P cap=3
    [NET][PUPPET][DIAG] player N cosmetics summary: ... tool_effects=E tool_sounds=S tool_noop=O tool_dropped=D

Tier labels (honest): HOOK-DRIVEN (automated, no human input) / SOURCE AUDITED, real process, NO visual or audible
verification: these checks prove the trigger frame, the gating/budget logic, the one-shot-per-entry bookkeeping, that nothing
in the world changed and that nothing crashed; NOT that the swoosh/impact effects or sounds look/sound right.

  A0  source audit of the Phase 6a block (no forbidden callee, ids come from the repo enums, no src/effect edit for the net)
  T1  every implemented tool state: one entry, expected sound/effect lines (name, label, budget=ok|noop), after the entry
      edge, at/after the vanilla frame (frame field >= trigger, < trigger + 4)
  T2  repeated entries (new counter) re-fire, also for a steady REPEAT row
  T3  a state interrupted before its trigger frame does not fire it
  T4  another scene (SHOP) -> budget=scene, never ok; T5 out of range -> budget=range, never ok
  T6  8 peers swinging at once: per-frame effect peak <= cap 3, host alive, events still presented (taking turns)
  T7  no world mutation: no FIELD_UPDATE reaches an observer; no crash text; summary counters on disconnect

Usage: python test_puppet_tool_fx.py [--port 8300] [--only t]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import sys
import time

import net_spike_lib as L
from test_player_scene_real import boot_host
from test_puppet_cosmetics import scene_msg, SCENE_FG, SCENE_SHOP0, strip_comments
from test_puppet_held_item import clean, make_b, wait_visual

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "..", "src", "pc_remote_player.c")
AUDIO_H = os.path.join(HERE, "..", "..", "..", "include", "audio_defs.h")
EFF_H = os.path.join(HERE, "..", "..", "..", "include", "ef_effect_control.h")
NET_EFFECT_C = os.path.join(HERE, "..", "..", "..", "src", "effect", "ef_swing_net.c")
IDLE = 0
FRAME_CAP = 3

EFF_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) cosmetic effect=(\w+) frame=(\S+) pos=\((-?\d+),(-?\d+)\) "
                    r"attr=(-?\d+) budget=(\w+)")
SND_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) (\w+) sound label=(-?\d+) frame=(\S+) budget=(\w+)")
STATE_RX = r"\[NET\]\[PUPPET\]\[DIAG\] player %d state main_index=%d counter=%d row=(\w+)"
PEAK_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] cosmetic per-frame spawn peak=(\d+) cap=(\d+)")
SUM_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) cosmetics summary: .*tool_effects=(\d+) tool_sounds=(\d+) "
                    r"tool_noop=(\d+) tool_dropped=(\d+)")
TOOL_SOUND_NAMES = {"axe_furi", "axe_cut", "axe_hit", "axe_broken", "net_slip", "ami_furi", "ami_get",
                    "net_putaway_gasagoso", "rod_stroke", "rod_stroke_small", "rod_back", "rod_putaway_gasagoso",
                    "umbrella_rotate", "uchiwa"}


def main_index_enum():
    """mPlayer_INDEX_* values from include/m_player.h (the wire carries the vanilla index)."""
    txt = open(os.path.join(HERE, "..", "..", "..", "include", "m_player.h"), encoding="utf-8", errors="replace").read()
    i = txt.index("mPlayer_INDEX_DMA")
    j = txt.index("mPlayer_INDEX_NUM", i)
    out, v = {}, 0
    for line in re.sub(r"//[^\n]*", "", txt[i:j]).split("\n"):
        m = re.match(r"\s*(mPlayer_INDEX_\w+)\s*(?:=\s*(\w+))?\s*,", line)
        if m:
            if m.group(2):
                v = int(m.group(2), 0)
            out[m.group(1)] = v
            v += 1
    return out


def audio_enum():
    """NA_SE_* values of the single AudioSE enum in include/audio_defs.h (explicit `= 0x..` or implicit +1)."""
    lines = open(AUDIO_H, encoding="utf-8", errors="replace").read().split("\n")
    out, v = {}, -1
    for line in lines:
        line = re.sub(r"//.*", "", line)
        m = re.match(r"\s*(NA_SE_\w+)\s*(?:=\s*(\w+))?\s*,", line)
        if m:
            if m.group(2):
                try:
                    v = int(m.group(2), 0)
                except ValueError:
                    v = out[m.group(2)]
            else:
                v += 1
            out[m.group(1)] = v
    return out


IDX = main_index_enum()
NA = audio_enum()

# (state name, mPlayer_INDEX_*, held item kind, [(kind, name, trigger frame (0 = entry), label|None, may_be_noop)])
FX, SN = "fx", "snd"
CASES = [
    ("swing_axe", "SWING_AXE", 0, [(SN, "axe_furi", 10.0, NA["NA_SE_TOOL_FURI"], False),
                                   (FX, "swing_axe_start", 12.0, None, True),
                                   (FX, "swing_axe_hit", 15.0, None, False),
                                   (SN, "axe_cut", 15.0, NA["NA_SE_AXE_CUT"], False)]),
    ("air_axe", "AIR_AXE", 0, [(SN, "axe_furi", 10.0, NA["NA_SE_TOOL_FURI"], False),
                               (FX, "swing_axe_start", 12.0, None, True)]),
    ("reflect_axe", "REFLECT_AXE", 0, [(SN, "axe_furi", 10.0, NA["NA_SE_TOOL_FURI"], False),
                                       (FX, "swing_axe_start", 12.0, None, True),
                                       (FX, "reflect_axe_hit", 15.0, None, False),
                                       (SN, "axe_hit", 15.0, NA["NA_SE_AXE_HIT"], False)]),
    ("broken_axe", "BROKEN_AXE", 0, [(SN, "axe_furi", 10.0, NA["NA_SE_TOOL_FURI"], False),
                                     (FX, "swing_axe_start", 12.0, None, True),
                                     (FX, "break_axe", 15.0, None, False),
                                     (SN, "axe_broken", 15.0, NA["NA_SE_TOOL_BROKEN3"], False)]),
    ("slip_net", "SLIP_NET", 9, [(SN, "net_slip", 0.0, 0x4129, False), (FX, "net_slip_asimoto", 0.0, None, False)]),
    ("swing_net", "SWING_NET", 9, [(SN, "ami_furi", 0.0, NA["NA_SE_TOOL_FURI"], False)]),
    ("pull_net", "PULL_NET", 9, [(SN, "ami_get", 0.0, NA["NA_SE_TOOL_GET"], False)]),
    ("putaway_net", "PUTAWAY_NET", 9, [(SN, "net_putaway_gasagoso", 0.0, NA["NA_SE_GASAGOSO"], False)]),
    ("cast_rod", "CAST_ROD", 51, [(FX, "swing_rod", 0.0, None, True),
                                  (SN, "rod_stroke", 20.0, NA["NA_SE_ROD_STROKE"], False)]),
    ("air_rod", "AIR_ROD", 51, [(FX, "swing_rod", 0.0, None, True),
                                (SN, "rod_stroke_small", 20.0, NA["NA_SE_ROD_STROKE_SMALL"], False)]),
    ("collect_rod", "COLLECT_ROD", 51, [(SN, "rod_back", 0.0, NA["NA_SE_ROD_BACK"], False),
                                        (FX, "swing_rod", 0.0, None, True)]),
    ("fly_rod", "FLY_ROD", 51, [(SN, "rod_back", 0.0, NA["NA_SE_ROD_BACK"], False)]),
    ("putaway_rod", "PUTAWAY_ROD", 51, [(SN, "rod_putaway_gasagoso", 0.0, NA["NA_SE_GASAGOSO"], False)]),
    ("rotate_umbrella", "ROTATE_UMBRELLA", 11, [(FX, "kasamizu", 0.0, None, False),
                                                (SN, "umbrella_rotate", 0.0, NA["NA_SE_UMBRELLA_ROTATE"], False)]),
    ("swing_fan", "SWING_FAN", 71, [(SN, "uchiwa", 1.5, NA["NA_SE_UCHIWA"], False)]),
]
# states with the vanilla trigger being a frame of a non-trivial clip: allow a longer stream
SLOW = {"swing_axe", "reflect_axe", "broken_axe", "cast_rod", "air_rod"}


def effects(text, pid):
    return [m.groups() for m in EFF_RX.finditer(text) if int(m.group(1)) == pid]


def tool_sounds(text, pid):
    return [m.groups() for m in SND_RX.finditer(text) if int(m.group(1)) == pid and m.group(2) in TOOL_SOUND_NAMES]


class Peer:
    def __init__(self, port, label, scene=SCENE_FG, far=False):
        self.c = make_b(port, label)
        self.pid = self.c.assigned_peer_id
        self.scene_seq = 0
        self.set_scene(scene)
        L.pump_sleep(1.5, self.c.hub)
        msgs = [m for m in self.c.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_MOVE, (L.CH_UNRELIABLE,)))
                if m.game is not None and m.game.net_player_id == L.PC_NETGAME_HOST_PLAYER_ID]
        g = msgs[-1].game if msgs else None
        hx, hy, hz = (g.pos_x, g.pos_y, g.pos_z) if g else (1000.0, 0.0, 1000.0)
        self.host_xyz = (hx, hy, hz)
        self.x, self.y, self.z = (hx + (2500.0 if far else 80.0)), hy, hz
        self.angle = 0
        self.frame = 1000
        self.item_kind = -1

    def set_scene(self, sid):
        self.scene_seq += 1
        self.c.send_reliable(scene_msg(self.scene_seq, sid))

    def move_once(self, main_index=None, counter=0):
        self.frame += 1
        self.c.send_move(self.frame, self.x, self.y, self.z, angle=self.angle, speed=0.0, move_state=IDLE,
                         item_kind=self.item_kind, main_index=main_index, entry_counter=counter)

    def stream(self, seconds, main_index=None, counter=0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.move_once(main_index, counter)
            L.pump_sleep(0.05, self.c.hub)

    def close(self):
        try:
            self.c.close()
        except Exception:
            pass


def act(host, peer, idx, counter, kind, secs=1.7, tail=0.5, pre=1.0):
    """One entry: idle first (so the interpolator settles), then state `idx` with `counter`, then idle."""
    peer.item_kind = kind
    if pre > 0:
        peer.stream(pre)
    off = len(host.log_text())
    peer.stream(secs, idx, counter)
    if tail > 0:
        peer.stream(tail)
    return host.log_text()[off:]


def a0(check):
    print("=" * 72 + "\n[A0] SOURCE AUDITED: pc_remote_player.c Phase 6a code")
    src = open(SRC, encoding="utf-8").read()
    code = strip_comments(src)
    start = code.index("typedef struct PCToolEvent")
    end = code.index("static void pc_puppet_pending_run", start)
    blk = code[start:end]
    forbidden = ["Player_actor_", "sAdo_PlayerStatusLevel", "sAdo_PlyWalkSe", "eEC_EFFECT_SWING_NET",
                 "eEC_EFFECT_KAGU_HAPPA", "eEC_EFFECT_HANABI", "eEC_EFFECT_SHOOTING",
                 "eEC_EFFECT_YAJIRUSHI", "eEC_EFFECT_TURI", "GET_PLAYER_ACTOR_NOW", "Now_Private", "now_private",
                 "pc_net_game_request_", "pcfa_set_tile", "mFI_SetFG_common", "RANDOM(", "RANDOM_F", "mISL_",
                 "set_pl_act_tim_proc", "fade_entry", "item_tree_fruit_drop_proc", "mPlib_get_player_actor_main_index",
                 "UKI_ACTOR", "fishing_rod_actor"]
    for f in forbidden:
        check("A0 tool block does not contain `%s`" % f, f not in blk)
    check("A0 sounds only via sAdo_OngenTrgStart(label, pos); effects only via the shared emitter (gated clip; Phase 6b: "
          "pc_puppet_fx_emit_core)",
          blk.count("sAdo_OngenTrgStart(") == 1 and blk.count("pc_puppet_fx_emit_core(") == 1 and
          "eEC_CLIP->effect_make_proc" not in blk)
    check("A0 every sound/effect id in the table is a repository enum (no numeric literal except the slip label)",
          all(re.search(r"\b(NA_SE_\w+|eEC_EFFECT_\w+|PC_TOOL_NA_SLIP|PC_TID_\w+)\b,\s*[\d.]+f", ln) for ln in
              re.findall(r"\{ PC_TOOL_(?:SND|FX),[^\n]*\}", blk)))
    check("A0 the slip label constant is the vanilla one (Player_actor_sound_slip 0x4129)", "PC_TOOL_NA_SLIP 0x4129" in src)
    check("A0 gated: sound goes through pc_puppet_fx_reason(.., 1), effects through the shared emitter",
          "pc_puppet_fx_reason(self, slot, game, 1)" in blk)
    check("A0 the effect no-op prefilter reads collision/field only after the local-scene check",
          blk.index("pc_remote_player_scene_is_local_field(slot, play)") < blk.index("mCoBG_Wpos2Attribute("))
    check("A0 hook only from the cosmetics tick (one call) and never from a network handler",
          code.count("pc_puppet_tool_tick(") == 2 and
          all("pc_puppet_tool" not in m for m in re.findall(r"void pc_remote_player_on_\w+\(.*?\n\}\n", code, re.S)))
    check("A0 arm on a new row start, entry events only on that tick, frame events through pc_puppet_frame_crossed",
          "c->tool_row_start != row_start_now" in blk and "pc_puppet_frame_crossed(last, cur, max_frames, e->frame)" in blk)
    check("A0 first-sight REPEAT rows adopt silently (entry events marked done)", "if (first_sight)" in blk)
    check("A0 net position comes from this puppet's own draw capture (Matrix_Position_VecZ 4000, RotY 3000)",
          "Matrix_Position_VecZ(4000.0f, &self->cos.net_pos)" in code and "Matrix_rotateXYZ(0, 3000, 0, MTX_MULT)" in code)
    net_c = open(NET_EFFECT_C, encoding="utf-8", errors="replace").read()
    check("A0 ef_swing_net.c is NOT edited / NOT spawned by the puppet (its local-player read stays unreachable)",
          "TARGET_PC" not in net_c and "GET_PLAYER_ACTOR_GAME_ACTOR" in net_c)
    check("A0 repository ids match the vanilla values the audit cites (TOOL_FURI 0x5A, GASAGOSO 0x69, ROD_STROKE 0x109, "
          "UCHIWA 0x167, UMBRELLA_ROTATE 0x432, ROD_STROKE_SMALL 0x445)",
          (NA["NA_SE_TOOL_FURI"], NA["NA_SE_GASAGOSO"], NA["NA_SE_ROD_STROKE"], NA["NA_SE_UCHIWA"],
           NA["NA_SE_UMBRELLA_ROTATE"], NA["NA_SE_ROD_STROKE_SMALL"]) == (0x5A, 0x69, 0x109, 0x167, 0x432, 0x445))
    check("A0 table rows: the 15 states this test exercises + the 11 Phase 6b rows (test_puppet_scoop_fx.py)",
          blk.count("{ mPlayer_INDEX_") == len(CASES) + 11)


def t_all(port, log_dir, check):
    print("=" * 72 + "\n[T1..T7] HOOK-DRIVEN: remote held-tool presentation")
    host = boot_host(port, ["--authoritative-wildlife"], "tool_all", log_dir)
    if host is None:
        check("T host reached field", False)
        return
    peers = []
    obs = None

    def new_peer(label, scene=SCENE_FG, far=False):
        p = Peer(port, label, scene, far)
        peers.append(p)
        return p

    try:
        obs = new_peer("OBS")
        obs.stream(2.0)
        check("T puppet visual initialised", wait_visual(host, 0))
        mark = obs.c.inbox.mark()
        probe = None
        n_probe = 0
        counter = 0
        # ---- T1: every implemented state ----
        for name, ixname, kind, evs in CASES:
            idx = IDX["mPlayer_INDEX_" + ixname]
            if probe is None or n_probe >= 4:  # fresh actor now and then: fresh diag limiter, fresh bucket
                if probe is not None:
                    probe.close()
                    peers.remove(probe)
                probe = new_peer("P%d" % len(peers))
                L.pump_sleep(1.0, probe.c.hub)
                n_probe = 0
            n_probe += 1
            counter = counter % 15 + 1
            secs = 2.4 if name in SLOW else 1.7
            if name == "swing_fan":
                secs = 2.2
            t = act(host, probe, idx, counter, kind, secs=secs)
            st = re.search(STATE_RX % (probe.pid, idx, counter), t)
            fx = effects(t, probe.pid)
            sn = tool_sounds(t, probe.pid)
            check("T1 %s(%d): entry edge logged with row=%s" % (name, idx, name), st is not None and st.group(1) == name)
            pos_state = st.start() if st else 0
            for kind_e, ename, frame_t, label, noop_ok in evs:
                if kind_e == SN:
                    got = [g for g in sn if g[1] == ename]
                    if name == "swing_fan":
                        ok = len(got) >= 1
                    else:
                        ok = len(got) == 1
                    check("T1 %s: sound %s fired %s, label %s (got %s)" %
                          (name, ename, "once" if name != "swing_fan" else ">= once", hex(label),
                           [(g[2], g[3], g[4]) for g in got]),
                          ok and all(int(g[2]) == label and g[4] == "ok" for g in got))
                    if got:
                        f = float(got[0][3])
                        check("T1 %s: %s at/after the vanilla frame %.1f (frame=%.1f)" % (name, ename, frame_t, f),
                              (f >= frame_t - 0.01 and f < frame_t + 4.0) if frame_t > 0 else f < 12.0)
                else:
                    got = [g for g in fx if g[1] == ename]
                    budgets = [g[6] for g in got]
                    good = {"ok", "noop"} if noop_ok else {"ok"}
                    check("T1 %s: effect %s fired once, budget in %s (got %s)" % (name, ename, sorted(good), budgets),
                          len(got) == 1 and budgets[0] in good)
                    if got:
                        f = float(got[0][2])
                        check("T1 %s: %s at/after the vanilla frame %.1f (frame=%.1f)" %
                              (name, ename, frame_t, f),
                              (f >= frame_t - 0.01 and f < frame_t + 4.0) if frame_t > 0 else f < 12.0)
            first_ev = min([m.start() for m in (EFF_RX.search(t), SND_RX.search(t)) if m is not None] or [10 ** 9])
            check("T1 %s: the entry edge is logged BEFORE the first tool event" % name, st is not None and pos_state < first_ev)
            extra = ({g[1] for g in fx} | {g[1] for g in sn}) - {e[1] for e in evs}
            check("T1 %s: no unexpected tool event names (%s)" % (name, sorted(extra)), not extra)
            n_budget_bad = [g for g in fx + sn if g[-1] not in ("ok", "noop")]
            check("T1 %s: no suppressed/capped event for a lone puppet in range (%s)" %
                  (name, [g[-1] for g in n_budget_bad]), not n_budget_bad)

        # ---- T2: repeated entries re-fire (also for a steady REPEAT row) ----
        rp = new_peer("RP")
        L.pump_sleep(1.0, rp.c.hub)
        off = len(host.log_text())
        rp.item_kind = 9
        idx_slip = IDX["mPlayer_INDEX_SLIP_NET"]
        rp.stream(1.6, idx_slip, 3)  # the FIRST MOVE of a fresh actor already carries the state
        t0 = host.log_text()[off:]
        # review R1-L1: nothing is adopted before the first MOVE exists, and that first pair is adopted SILENTLY: a steady REPEAT
        # row starts (row logged) but it is not an entry edge, so no entry sound/effect fires for an action already in progress
        check("T2 first MOVE of a fresh actor (steady REPEAT row) starts the row silently: row logged, NO entry sound/effect "
              "(%s / %s)" % ([g[1] for g in tool_sounds(t0, rp.pid)], [g[1] for g in effects(t0, rp.pid)]),
              re.search(STATE_RX % (rp.pid, idx_slip, 3), t0) is not None and
              not [g for g in tool_sounds(t0, rp.pid) if g[1] == "net_slip"] and
              not [g for g in effects(t0, rp.pid) if g[1] == "net_slip_asimoto"])
        off = len(host.log_text())
        rp.stream(0.6)
        rp.stream(1.4, idx_slip, 4)  # a real new entry (counter changed)
        t1 = host.log_text()[off:]
        check("T2 slip_net with a new entry counter fires its entry sound + effect once more (%s / %s)" %
              ([g[1] for g in tool_sounds(t1, rp.pid)], [g[1] for g in effects(t1, rp.pid)]),
              len([g for g in tool_sounds(t1, rp.pid) if g[1] == "net_slip"]) == 1 and
              len([g for g in effects(t1, rp.pid) if g[1] == "net_slip_asimoto"]) == 1)
        rp.stream(0.6)
        off = len(host.log_text())
        n_axe = 0
        idx_axe = IDX["mPlayer_INDEX_SWING_AXE"]
        rp.item_kind = 0
        for ctr in (5, 6, 7):
            rp.stream(0.8)
            rp.stream(2.4, idx_axe, ctr)
        rp.stream(0.6)
        t = host.log_text()[off:]
        n_furi = len([g for g in tool_sounds(t, rp.pid) if g[1] == "axe_furi"])
        n_cut = len([g for g in tool_sounds(t, rp.pid) if g[1] == "axe_cut"])
        n_hit = len([g for g in effects(t, rp.pid) if g[1] == "swing_axe_hit"])
        check("T2 three swing_axe entries (counters 5,6,7) -> 3 furi sounds, 3 cut sounds, 3 hit effects (%d/%d/%d)" %
              (n_furi, n_cut, n_hit), (n_furi, n_cut, n_hit) == (3, 3, 3))

        # ---- T3: interrupted before the trigger frame ----
        ip = new_peer("IN")
        L.pump_sleep(1.0, ip.c.hub)
        ip.item_kind = 0
        ip.stream(1.0)
        off = len(host.log_text())
        for _ in range(3):
            ip.move_once(idx_axe, 9)
            L.pump_sleep(0.05, ip.c.hub)
        ip.stream(1.8, IDX["mPlayer_INDEX_DIG_SCOOP"], 3)
        ip.stream(1.0)
        t = host.log_text()[off:]
        entered = re.search(STATE_RX % (ip.pid, idx_axe, 9), t) is not None
        n_ev = len([g for g in tool_sounds(t, ip.pid) if g[1].startswith("axe")]) + \
            len([g for g in effects(t, ip.pid) if g[1].startswith(("swing_axe", "reflect_axe", "break_axe"))])
        print("INFO - T3 axe row entered=%s tool events=%d" % (entered, n_ev))
        if entered:
            check("T3 a swing_axe replaced by another row before frame 10 fires none of its frame events (%d)" % n_ev,
                  n_ev == 0)
        else:
            print("INFO - T3 the interpolator never adopted the 37 snapshot (too brief): weak check only")
        ip.item_kind = 51
        ip.stream(1.0)
        off = len(host.log_text())
        for _ in range(3):
            ip.move_once(IDX["mPlayer_INDEX_CAST_ROD"], 10)
            L.pump_sleep(0.05, ip.c.hub)
        ip.stream(1.8, IDX["mPlayer_INDEX_DIG_SCOOP"], 4)
        t = host.log_text()[off:]
        check("T3 cast_rod interrupted before frame 20 never plays rod_stroke (entry swing_rod effect allowed)",
              not [g for g in tool_sounds(t, ip.pid) if g[1] == "rod_stroke"])

        # ---- T4: another scene; T5: out of range ----
        sp = new_peer("SH", scene=SCENE_SHOP0)
        L.pump_sleep(2.0, sp.c.hub)
        t = act(host, sp, IDX["mPlayer_INDEX_SWING_AXE"], 1, 0, secs=2.2)
        evs = effects(t, sp.pid) + tool_sounds(t, sp.pid)
        check("T4 puppet in the SHOP scene: events present but all budget=scene, never ok (%s)" %
              sorted({g[-1] for g in evs}), len(evs) >= 3 and all(g[-1] == "scene" for g in evs))
        fp = new_peer("FAR", far=True)
        L.pump_sleep(1.5, fp.c.hub)
        t = act(host, fp, IDX["mPlayer_INDEX_SWING_AXE"], 1, 0, secs=2.2)
        evs = effects(t, fp.pid) + tool_sounds(t, fp.pid)
        check("T5 puppet 2500 units away: events present, all budget=range, never ok (%s)" %
              sorted({g[-1] for g in evs}), len(evs) >= 3 and all(g[-1] == "range" for g in evs))

        # ---- summary counters on disconnect (a fresh peer: one swing_axe) ----
        sm_p = new_peer("SM")
        L.pump_sleep(1.0, sm_p.c.hub)
        act(host, sm_p, IDX["mPlayer_INDEX_SWING_AXE"], 1, 0, secs=2.2)
        off = len(host.log_text())
        spid = sm_p.pid
        sm_p.c.disconnect()
        L.pump_sleep(1.0, obs.c.hub)
        sm_p.close()
        peers.remove(sm_p)
        host.wait_for_log(r"player %d cosmetics summary: .*tool_effects=" % spid, 15.0, since_offset=off)
        sms = [x for x in SUM_RX.finditer(host.log_text()[off:]) if int(x.group(1)) == spid]
        sm = sms[-1] if sms else None
        check("T7 disconnect summary: one swing_axe -> tool_sounds=2, tool_effects+tool_noop=2, dropped=0 (%s)" %
              (None if sm is None else sm.groups(),),
              sm is not None and int(sm.group(3)) == 2 and int(sm.group(2)) + int(sm.group(4)) == 2 and
              int(sm.group(5)) == 0)

        # ---- T6: 8 peers swinging at once ----
        # the host seats 8 peers: the observer leaves first, eight[0] observes FIELD_UPDATEs from here on
        L.pump_sleep(1.0, obs.c.hub)
        ups = [L.field_update_tuple(g) for g in obs.c.field_updates_since(mark)]
        check("T7 (T1..T5) no FIELD_UPDATE reached the observer (tool presentation never writes the field) (%d)" % len(ups),
              not ups)
        for p in list(peers):
            p.close()
            peers.remove(p)
        eight = [new_peer("E%d" % i) for i in range(8)]
        mark = eight[0].c.inbox.mark()
        L.pump_sleep(2.0, eight[0].c.hub)
        for i, p in enumerate(eight):
            p.item_kind = 0
            p.x += 30.0 * (i % 4)
            p.z += 30.0 * (i // 4)
        for _ in range(20):
            for p in eight:
                p.move_once()
            L.pump_sleep(0.05, eight[0].c.hub)
        off = len(host.log_text())
        end = time.monotonic() + 2.8
        while time.monotonic() < end:
            for i, p in enumerate(eight):
                p.move_once(IDX["mPlayer_INDEX_SWING_AXE"] if i % 2 == 0 else IDX["mPlayer_INDEX_BROKEN_AXE"], 2)
            L.pump_sleep(0.05, eight[0].c.hub)
        for _ in range(10):
            for p in eight:
                p.move_once()
            L.pump_sleep(0.05, eight[0].c.hub)
        t = host.log_text()[off:]
        allev = []
        for p in eight:
            allev += effects(t, p.pid) + tool_sounds(t, p.pid)
        budgets = sorted({g[-1] for g in allev})
        n_ok = len([g for g in allev if g[-1] == "ok"])
        peaks = [int(m.group(1)) for m in PEAK_RX.finditer(host.log_text())]
        print("INFO - T6 budgets %s ok=%d peak=%s" % (budgets, n_ok, max(peaks) if peaks else None))
        check("T6 8 simultaneous swings: events still presented (ok >= 8, got %d)" % n_ok, n_ok >= 8)
        check("T6 only ok/noop/capped/pool budgets occur (%s)" % budgets, set(budgets) <= {"ok", "noop", "capped", "pool"})
        check("T6 per-frame effect peak never exceeds the cap %d (peaks %s)" % (FRAME_CAP, peaks[-3:]),
              all(p <= FRAME_CAP for p in peaks))
        check("T6 host alive during/after the burst", host.alive())

        # ---- T7: no world mutation ----
        L.pump_sleep(1.0, eight[0].c.hub)
        ups = [L.field_update_tuple(g) for g in eight[0].c.field_updates_since(mark)]
        check("T7 (T6) no FIELD_UPDATE reached an observer during the 8-peer burst (%d)" % len(ups), not ups)
        clean(host, check, "T7")
    finally:
        for p in peers:
            p.close()
        host.stop()


def main():
    L.require_test_bin_dir()  # review M1: refuse to run against the live build/save
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8300)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    os.environ["PC_PUPPET_DIAG"] = "1"  # net_spike_lib copies os.environ into the launched game process
    results = []
    log_dir = os.path.join(HERE, "logs", "m9c")
    os.makedirs(log_dir, exist_ok=True)
    check = lambda desc, cond: L.check(desc, cond, results)
    if not args.only or args.only == "a0":
        a0(check)
    if not args.only or args.only in ("t", "t1"):
        t_all(args.port, log_dir, check)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
