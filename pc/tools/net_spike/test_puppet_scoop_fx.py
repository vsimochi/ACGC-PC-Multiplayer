#!/usr/bin/env python3
"""test_puppet_scoop_fx.py - M9-C Phase 6b (remote shovel / pitfall / common-action presentation, cosmetic only,
protocol stays v7, no new message).

Real host process (`--host --bootstrap-resident 0 --field-action-test-seed`, PC_PUPPET_DIAG=1 in its environment) plus
scripted FakeClients.  A peer streams MOVE packets with a shovel / common-action main index (+ entry counter + held item)
standing on a tile of the host's own snapshot and facing the tile to dig.  The observable decisions are the env-gated lines
of pc_remote_player.c (formats of Phase 6a, plus):
    [NET][PUPPET][DIAG] player N scoop target event=<name> frame=F found=0|1 tile=(ux,uz) item=0xIIII stump=0|1
    [NET][PUPPET][DIAG] player N feel pos source=<captured|fallback> event=<name>
and the budget token `notarget` (no tile / no grass at the reconstructed target).

Tier labels (honest): HOOK-DRIVEN (automated, no human input) / SOURCE AUDITED, real process, NO visual or audible
verification: these checks prove the trigger frame, the reconstructed target tile, the gating/budget logic, the
one-shot-per-entry bookkeeping, the TARGET_PC override discipline, that nothing in the world changed and that nothing
crashed; NOT that the dig particles or sounds look/sound right.

  A0  source audit: effect override only consulted when set and cleared after the call, forbidden callees absent, the
      frames/args of the table equal the vanilla source numbers
  T1  every implemented state: expected sounds/effects (name, label, budget=ok), after the entry edge, at/after the vanilla
      frame; the target tile line is the tile east of the puppet (found=1, snapshot item)
  T2  repeated entries re-fire; T3 interrupted before the trigger frame does not fire
  T4  another scene -> budget=scene and NO target read; T5 out of range -> budget=range and NO target read
  T6  8 peers digging at once: per-frame effect peak <= cap, events still presented, host alive
  T7  no world mutation: no FIELD_UPDATE to an observer, target tiles unchanged, no crash text
  T8  reflect_scoop against a tree tile: target = the tree tile, TREE_HIT sound, attr 7 effect
  T9  remove_grass: grass tile -> mud effect + sound; facing a non-grass tile -> notarget
  T10 stump target (only if the snapshot has one near the host)
  T11 review H1: the continuous ASE2 pitfall sweat is killed when the puppet leaves STRUGGLE_PITFALL (and on destroy), a
      fresh pitfall spawns again, never two sweats for one run

Usage: python test_puppet_scoop_fx.py [--port 8400] [--only t]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import subprocess
import sys
import time

import net_spike_lib as L
from test_player_scene_real import boot_host
from test_puppet_cosmetics import SCENE_FG, SCENE_SHOP0, strip_comments
from test_puppet_held_item import clean, wait_visual
from test_puppet_tool_fx import (Peer, IDX, NA, EFF_RX, SND_RX, STATE_RX, PEAK_RX, SUM_RX, act, FRAME_CAP, HERE, SRC, FX,
                                 SN)
from test_puppet_tree_shake import parse_tree_ids, EAST, WEST

EFF_DIR = os.path.join(HERE, "..", "..", "..", "src", "effect")
NAME_TABLE_H = os.path.join(HERE, "..", "..", "..", "include", "m_name_table.h")
GAME_DIR = os.path.join(HERE, "..", "..", "..", "src", "game")
TARGET_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) scoop target event=(\w+) frame=(\S+) found=(\d) "
                       r"tile=\((-?\d+),(-?\d+)\) item=0x([0-9A-Fa-f]+) stump=(\d)")
FEEL_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) feel pos source=(\w+) event=(\w+)")
NEW_SOUNDS = {"scoop1", "kirikabu_scoop", "kirikabu_out", "scoop_umeru", "reflect_scoop_snd", "karaburi", "horidashi",
              "scoop_putaway_gasagoso", "araiiki", "hachi_sasareru", "zassou_nuku"}
SHOVEL = 53
GRASS_IDS = {0x08, 0x09, 0x0A}  # GRASS_A..GRASS_C (IS_ITEM_GRASS)


def sounds(text, pid):
    return [m.groups() for m in SND_RX.finditer(text) if int(m.group(1)) == pid and m.group(2) in NEW_SOUNDS]


def effects(text, pid):
    return [m.groups() for m in EFF_RX.finditer(text) if int(m.group(1)) == pid]


def targets(text, pid):
    return [m.groups() for m in TARGET_RX.finditer(text) if int(m.group(1)) == pid]


NEW_EFFECTS = {"dig_hole0", "dig_hole1", "dig_hole2", "dig_scoop", "dig_scoop_stump", "fill_hole3", "fill_hole4",
               "fill_hole5", "fill_scoop", "reflect_scoop", "dig_scoop_get", "tired_dust", "pitfall_sweat", "grass_mud"}


def new_effects(text, pid):
    return [g for g in effects(text, pid) if g[1] in NEW_EFFECTS]


SCOOP_REFLECT_OK = {NA["NA_SE_SCOOP_HIT"], NA["NA_SE_SCOOP_TREE_HIT"], NA["NA_SE_SCOOP_SHIGEMI"]}
ARAIIKI_OK = {NA["NA_SE_ARAIIKI_BOY"], NA["NA_SE_ARAIIKI_GIRL"]}
# (state, index name, held kind, uses_target, [(kind, name, frame, label|set|None)])
CASES = [
    ("dig_scoop", "DIG_SCOOP", SHOVEL, True,
     [(SN, "scoop1", 15.0, NA["NA_SE_SCOOP1"]), (FX, "dig_hole0", 14.0, None), (FX, "dig_hole1", 15.0, None),
      (FX, "dig_hole2", 16.0, None), (FX, "dig_scoop", 22.0, None)]),
    ("fill_scoop", "FILL_SCOOP", SHOVEL, True,
     [(SN, "scoop_umeru", 11.0, NA["NA_SE_SCOOP_UMERU"]), (FX, "fill_hole3", 13.0, None), (FX, "fill_hole4", 19.0, None),
      (FX, "fill_hole5", 25.0, None), (FX, "fill_scoop", 40.0, None)]),
    ("putin_scoop", "PUTIN_SCOOP", SHOVEL, True,
     [(SN, "scoop_umeru", 18.0, NA["NA_SE_SCOOP_UMERU"]), (FX, "fill_hole3", 20.0, None), (FX, "fill_hole4", 26.0, None),
      (FX, "fill_hole5", 32.0, None), (FX, "fill_scoop", 47.0, None)]),
    ("get_scoop", "GET_SCOOP", SHOVEL, True,
     [(SN, "scoop1", 15.0, NA["NA_SE_SCOOP1"]), (SN, "horidashi", 21.0, NA["NA_SE_ITEM_HORIDASHI"]),
      (FX, "dig_hole0", 14.0, None), (FX, "dig_hole1", 15.0, None), (FX, "dig_hole2", 16.0, None),
      (FX, "dig_scoop_get", 22.0, None)]),
    ("reflect_scoop", "REFLECT_SCOOP", SHOVEL, True,
     [(SN, "reflect_scoop_snd", 13.0, SCOOP_REFLECT_OK), (FX, "reflect_scoop", 13.0, None)]),
    ("air_scoop", "AIR_SCOOP", SHOVEL, False, [(SN, "karaburi", 13.0, NA["NA_SE_KARABURI"])]),
    ("putaway_scoop", "PUTAWAY_SCOOP", SHOVEL, False, [(SN, "scoop_putaway_gasagoso", 0.0, NA["NA_SE_GASAGOSO"])]),
    ("tired", "TIRED", -1, False, [(SN, "araiiki", 10.0, ARAIIKI_OK), (FX, "tired_dust", 10.0, None)]),
    ("struggle_pitfall", "STRUGGLE_PITFALL", -1, False, [(FX, "pitfall_sweat", 0.0, None)]),
    ("stung_bee", "STUNG_BEE", -1, False, [(SN, "hachi_sasareru", 0.0, NA["NA_SE_HACHI_SASARERU"])]),
]
SLOW = {"fill_scoop", "putin_scoop"}


def find_pairs(peer, want, radius=330.0, limit=12):
    """(ux, uz) whose own tile and east neighbour satisfy want(v_here, v_east); nearest to the host player first."""
    hx, hy, hz = peer.host_xyz
    out = []
    for ux in range(int(hx // 40) - 9, int(hx // 40) + 10):
        for uz in range(int(hz // 40) - 9, int(hz // 40) + 10):
            a, b = peer.c.world.tile_ut(ux, uz), peer.c.world.tile_ut(ux + 1, uz)
            if a is None or b is None or not want(a, b):
                continue
            d = max(abs(ux * 40.0 + 20.0 - hx), abs(uz * 40.0 + 20.0 - hz))
            if d <= radius:
                out.append((d, (ux, uz)))
    out.sort()
    return [t for _, t in out][:limit]


def place(peer, ux, uz, facing=EAST, back=0.0, secs=1.0):
    """Puppet at the tile centre (shifted `back` units against the facing, east/west only), settle with idle MOVEs."""
    peer.x = ux * 40.0 + 20.0 - (back if facing == EAST else -back)
    peer.z = uz * 40.0 + 20.0
    peer.y = peer.host_xyz[1]
    peer.angle = facing
    peer.stream(secs)


def go(host, peer, idx, counter, kind, secs, tail=0.5):
    peer.item_kind = kind
    off = len(host.log_text())
    peer.stream(secs, idx, counter)
    if tail > 0:
        peer.stream(tail)
    return host.log_text()[off:]


def code_of(path):
    return strip_comments(open(path, encoding="utf-8", errors="replace").read())


def a0(check):
    print("=" * 72 + "\n[A0] SOURCE AUDITED: Phase 6b code")
    src = open(SRC, encoding="utf-8").read()
    code = strip_comments(src)
    hole = code_of(os.path.join(EFF_DIR, "ef_dig_hole.c"))
    scoop = code_of(os.path.join(EFF_DIR, "ef_dig_scoop.c"))
    ctl = code_of(os.path.join(EFF_DIR, "ef_effect_control.c"))
    hdr = code_of(os.path.join(HERE, "..", "..", "..", "include", "ef_effect_control.h"))
    # --- override discipline (review R4: the non-TARGET_PC side is HEAD's code verbatim) ---
    def side(path, keep_pc):
        out, stack = [], []  # stack of "is this line inside a kept branch"
        for ln in open(path, encoding="utf-8", errors="replace").read().replace("\r\n", "\n").split("\n"):
            s = ln.strip()
            if s == "#ifdef TARGET_PC":
                stack.append(keep_pc)
            elif s == "#ifndef TARGET_PC":
                stack.append(not keep_pc)
            elif s == "#else" and stack:
                stack[-1] = not stack[-1]
            elif s == "#endif" and stack:
                stack.pop()
            elif all(stack):
                out.append(ln)
        return "\n".join(out)

    def norm(t):
        return re.sub(r"\s+", " ", strip_comments(t)).strip()

    for nm, txt in (("ef_dig_hole.c", hole), ("ef_dig_scoop.c", scoop)):
        path = os.path.join(EFF_DIR, nm)
        pc_side, non_pc = side(path, True), side(path, False)
        i = txt.index("eEC_pc_source_pos_override != NULL")
        pre = txt[txt.rindex("#ifdef", 0, i):i]
        check("A0 %s: override consulted only inside #ifdef TARGET_PC and only when != NULL" % nm,
              pre.startswith("#ifdef TARGET_PC") and txt.count("eEC_pc_source_pos_override") == 2 and
              "eEC_pc_source_pos_override" not in non_pc and "src_pos" not in non_pc)
        check("A0 %s: on the PC side GET_PLAYER_ACTOR is read only in the else branch of the override test" % nm,
              norm(pc_side).count("GET_PLAYER_ACTOR") == 1 and
              norm(pc_side).index("GET_PLAYER_ACTOR") > norm(pc_side).index("eEC_pc_source_pos_override != NULL"))
        try:
            head = subprocess.run(["git", "show", "HEAD:src/effect/" + nm], cwd=os.path.join(HERE, "..", "..", ".."),
                                  capture_output=True, text=True, timeout=30)
            head_txt = head.stdout if head.returncode == 0 else None
        except Exception:
            head_txt = None
        if head_txt is None:
            print("INFO - A0 %s: git unavailable, HEAD comparison skipped" % nm)
        else:
            check("A0 %s: the non-TARGET_PC code is identical to HEAD's (comments/whitespace aside)" % nm,
                  norm(non_pc) == norm(head_txt.replace("\r\n", "\n")))
    check("A0 the override is defined once (init NULL) in ef_effect_control.c and declared extern in the header",
          "const xyz_t* eEC_pc_source_pos_override = NULL;" in ctl and "extern const xyz_t* eEC_pc_source_pos_override;" in hdr)
    ebh = code_of(os.path.join(HERE, "..", "..", "..", "include", "ac_effectbg.h"))
    check("A0 review R4: the pool accessors are declared in the headers (not extern'd inside pc_remote_player.c)",
          "extern int eEC_pc_count_active(void);" in hdr and "extern int Effectbg_pc_count_active(void);" in ebh and
          "extern int Effectbg_pc_count_near(const xyz_t* pos, f32 radius);" in ebh and
          "extern int eEC_pc_count_active" not in code and "extern int Effectbg_pc_count" not in code)
    ksrc = code[code.index("static void pc_puppet_sweat_kill(PCRemotePlayerActor* self, const char* reason) {"):]
    ksrc = ksrc[:ksrc.index("static void pc_remote_player_dt")]
    check("A0 review H1: sweat kill = NULL-checked clip, ASE2 id + the puppet's own item_name (never RSV_NO), flag-guarded",
          "eEC_CLIP != NULL" in ksrc and "effect_kill_proc != NULL" in ksrc and "self->cos.sweat_active" in ksrc and
          "effect_kill_proc(eEC_EFFECT_ASE2, (u16)(PC_PUPPET_FX_ITEM_NAME_BASE + (u32)self->peer))" in ksrc and
          "RSV_NO" not in ksrc and "0xFFFF" not in ksrc)
    check("A0 review H1: kill reached from the row release, the tool tick (row left / new run), destroy_slot and the actor dt",
          code.count("pc_puppet_sweat_kill(") >= 6 and "s_remote_player_profile.dt_proc = pc_remote_player_dt;" in code and
          'pc_puppet_sweat_kill((PCRemotePlayerActor*)slot->actor, "destroy")' in code and
          'pc_puppet_sweat_kill(self, "left-row")' in code and "pc_puppet_sweat_kill(self, why)" in code)
    check("A0 review H1: sweat_active is set only after an ASE2 tool spawn (one place)",
          code.count("self->cos.sweat_active = 1;") == 1 and "id == (u16)eEC_EFFECT_ASE2" in code)
    pi = code.index("void pc_remote_player_poll(void) {")
    psrc = code[pi:code.index("void pc_remote_player_shutdown(void) {")]
    check("A0 review round 2 R5-LOW-1: pc_remote_player_poll kills a CULLED puppet's sweat when the newest snapshot is not "
          "STRUGGLE_PITFALL (flag-guarded, NO_CULL bit, low-address check; NOT TESTED at runtime: a test cannot force culling)",
          'pc_puppet_sweat_kill((PCRemotePlayerActor*)slot->actor, "culled")' in psrc and
          "cos.sweat_active" in psrc and "ACTOR_STATE_NO_CULL" in psrc and "mPlayer_INDEX_STRUGGLE_PITFALL" in psrc and
          "PC_LOWADDR_LIMIT" in psrc)
    check("A0 review round 2 N1: DIG_SCOOP fan-out is the real worst case 10 (winter bush: 5 HAPPA + 5 YUKI)",
          re.search(r"case eEC_EFFECT_DIG_SCOOP:\s*return 10;", code) is not None)
    # review round 2 (R6-M): the guard is exercised IN-PROCESS (no child process, no game process can ever be launched)
    import ast
    saved_dir, saved_env = L.GAME_BIN_DIR, os.environ.get("NET_SPIKE_ALLOW_LIVE_BIN")

    def guard_exit(bin_dir, allow):
        L.GAME_BIN_DIR = bin_dir
        if allow:
            os.environ["NET_SPIKE_ALLOW_LIVE_BIN"] = "1"
        else:
            os.environ.pop("NET_SPIKE_ALLOW_LIVE_BIN", None)
        try:
            L.require_test_bin_dir()
            return None
        except SystemExit as e:
            return e.code

    try:
        live_exit = guard_exit(L.LIVE_GAME_BIN_DIR, False)
        test_exit = guard_exit(os.path.join(L.LIVE_GAME_BIN_DIR, "..", "bin_talkfix_clone"), False)
        allow_exit = guard_exit(L.LIVE_GAME_BIN_DIR, True)
    finally:
        L.GAME_BIN_DIR = saved_dir
        if saved_env is None:
            os.environ.pop("NET_SPIKE_ALLOW_LIVE_BIN", None)
        else:
            os.environ["NET_SPIKE_ALLOW_LIVE_BIN"] = saved_env
    check("A0 review M1: require_test_bin_dir() refuses the LIVE bin dir (exit 2), accepts a test copy, honours "
          "NET_SPIKE_ALLOW_LIVE_BIN=1 (in-process, no process launched; got %r/%r/%r)" % (live_exit, test_exit, allow_exit),
          live_exit == 2 and test_exit is None and allow_exit is None)
    bad = []
    for nm in ("test_move_action_state_wire.py", "test_puppet_cosmetics.py", "test_puppet_held_item.py",
               "test_puppet_pickup_action.py", "test_puppet_scoop_fx.py", "test_puppet_tool_fx.py",
               "test_puppet_tree_shake.py"):
        with open(os.path.join(HERE, nm), "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
        ok = False
        if len(mains) == 1:
            body = list(mains[0].body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and                     isinstance(body[0].value.value, str):
                body = body[1:]  # docstring
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Call):
                fn = body[0].value.func
                ok = isinstance(fn, ast.Attribute) and fn.attr == "require_test_bin_dir" and                     isinstance(fn.value, ast.Name) and fn.value.id == "L"
        if not ok:
            bad.append(nm)
    check("A0 review M1: main() of all 7 new tests starts with L.require_test_bin_dir() (bad: %s)" % bad, not bad)
    sets = re.findall(r"eEC_pc_source_pos_override\s*=\s*[^;]+;", code)
    check("A0 the puppet sets the override once (&own world position) and clears it once, around ONE effect_make_proc (%s)"
          % sets,
          len(sets) == 2 and sets[0].endswith("&self->actor_class.world.position;") and sets[1].endswith("= NULL;"))
    i = code.index("eEC_pc_source_pos_override = &self->actor_class.world.position;")
    j = code.index("eEC_pc_source_pos_override = NULL;", i)
    seg = code[i:j]
    check("A0 between set and clear exactly one statement: the effect_make_proc call (no return/branch)",
          seg.count("effect_make_proc(") == 1 and seg.count(";") == 2 and "return" not in seg and "if" not in seg)
    others = []
    for root, _d, files in os.walk(os.path.join(HERE, "..", "..", "..", "src")):
        for f in files:
            if f.endswith((".c", ".c_inc", ".h")) and f not in ("ef_effect_control.c", "ef_dig_hole.c", "ef_dig_scoop.c"):
                t = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
                if "eEC_pc_source_pos_override" in t:
                    others.append(f)
    check("A0 no other game/effect source touches the override (%s)" % others, not others)
    # --- forbidden callees in the new block ---
    start = code.index("static int pc_puppet_scoop_target")
    end = code.index("static void pc_puppet_tool_tick", start)
    blk = code[start:end]
    forbidden = ["Player_actor_", "mPlib_Check_scoop_after", "GET_PLAYER_ACTOR_NOW", "Now_Private", "now_private",
                 "pc_net_game_request_", "pcfa_set_tile", "mFI_SetFG_common", "dig_hole_effect_entry_proc",
                 "fly_entry_proc", "bury_hole_effect_entry_proc", "mCoBG_RegistDecalCircle", "drop_entry",
                 "ten_coin_entry", "player_drop_entry_proc", "RANDOM(", "RANDOM_F", "mISL_", "set_pl_act_tim_proc",
                 "mPlib_get_player_actor_main_index", "item_tree_fruit_drop_proc", "ef_dig_hole(", "ef_dig_scoop("]
    for f in forbidden:
        check("A0 scoop/target block does not contain `%s`" % f, f not in blk)
    check("A0 the target reconstruction writes nothing in the field (no `*fg_p =` / mFI_Set / mCoBG_Set / Regist)",
          re.search(r"\*fg_p\s*=[^=]|mFI_Set|mCoBG_Set|mCoBG_Regist|mCoBG_Change", blk) is None)
    check("A0 effects only through pc_puppet_fx_emit_core (one effect_make_proc in the emitter, none in the tool block)",
          "eEC_CLIP->effect_make_proc" not in blk and blk.count("pc_puppet_fx_emit_core(") == 1 and
          code.count("eEC_CLIP->effect_make_proc(") == 1)
    check("A0 the target is read only after the gate precheck (pc_pk_gate before pc_puppet_scoop_target in tool_fire)",
          blk.index("pc_pk_gate(self, slot, game)") < blk.index("have_tgt = pc_puppet_scoop_target("))
    check("A0 mode switch happens only from the cosmetics tick (no network-handler reference)",
          code.count("pc_puppet_tool_tick(") == 2 and
          all("pc_puppet_tool" not in m for m in re.findall(r"void pc_remote_player_on_\w+\(.*?\n\}\n", code, re.S)))
    check("A0 FEEL capture writes only the puppet's own storage (cos.feel_pos), vanilla draw_effect_idx body not run",
          "Matrix_Position_Zero(&self->cos.feel_pos)" in code and "draw_effect_idx" not in code)
    # --- numbers vs the vanilla source ---
    def v(name):
        return code_of(os.path.join(GAME_DIR, name))
    dig, fill, refl, air, get, put, tired = (v("m_player_main_dig_scoop.c_inc"), v("m_player_main_fill_scoop.c_inc"),
                                              v("m_player_main_reflect_scoop.c_inc"), v("m_player_main_air_scoop.c_inc"),
                                              v("m_player_main_get_scoop.c_inc"), v("m_player_main_putin_scoop.c_inc"),
                                              v("m_player_main_tired.c_inc"))
    check("A0 vanilla dig frames 14/15/16/22/42, scoop1 at 20-5, kirikabu frames",
          all(("Check_AnimationFrame(fc, %s)" % f) in dig for f in ("14.0f", "15.0f", "16.0f", "22.0f", "42.0f")) and
          "(mod + 20.0f) - 5.0f" in dig and "Player_actor_Check_AnimationFrame(fc, 21.0f)" in dig)
    check("A0 vanilla fill frames 13/19/25/40 (+7 for FILL_UP_I1), sound at 18-7, putin reuses the helpers",
          all(x in fill for x in ("c1 = 13.0f + mod", "c2 = 19.0f + mod", "c3 = 25.0f + mod", "c4 = 40.0f + mod",
                                  "(18.0f + mod) - 7.0f", "mod = 7.0f")) and
          "SetEffectHit_Fill_scoop" in put and "SetSound_Fill_scoop" in put and "mPlayer_ANIM_FILL_UP_I1" in put)
    check("A0 vanilla reflect frame 13 (effect, sound), air karaburi 13, get effect via Dig helper",
          refl.count("Check_AnimationFrame(&player->keyframe0.frame_control, 13.0f)") >= 3 and
          "Check_AnimationFrame(&player->keyframe0.frame_control, 13.0f)" in air and
          "SetEffectHit_Dig_scoop" in get and "Player_actor_SetSound_Dig_scoop" in get)
    check("A0 vanilla tired: sound + DUST at frame 10 at feel_pos with RSV_NO, 0, 0",
          "Check_AnimationFrame(fc0_p, 10.0f)" in tired and "eEC_EFFECT_DUST, player->feel_pos" in tired)
    check("A0 table rows: 11 new rows (7 shovel states + tired + struggle_pitfall + stung_bee + remove_grass) + the 15 of 6a",
          len(re.findall(r"\{ mPlayer_INDEX_\w+, \d+, \{", code[code.index("static const PCToolRow s_tool_rows[]"):end]))
          == 15 + 11)
    # numeric ids in the table equal repository enums
    check("A0 repository ids: SCOOP1 0x11E, SCOOP_UMERU 0x120, KARABURI 0x43A, ITEM_HORIDASHI 0x57, ZASSOU_NUKU 0x15F",
          (NA["NA_SE_SCOOP1"], NA["NA_SE_SCOOP_UMERU"], NA["NA_SE_KARABURI"], NA["NA_SE_ITEM_HORIDASHI"],
           NA["NA_SE_ZASSOU_NUKU"]) == (0x11E, 0x120, 0x43A, 0x57, 0x15F))


def t_all(port, log_dir, check):
    print("=" * 72 + "\n[T1..T10] HOOK-DRIVEN: remote shovel / pitfall / common-action presentation")
    tree_ids, ev = parse_tree_ids()
    host = boot_host(port, ["--authoritative-wildlife", "--field-action-test-seed"], "scoop_all", log_dir)
    if host is None:
        check("T host reached field", False)
        return
    peers = []

    def new_peer(label, scene=SCENE_FG, far=False):
        p = Peer(port, label, scene, far)
        peers.append(p)
        return p

    try:
        obs = new_peer("OBS")
        obs.stream(2.0)
        check("T puppet visual initialised", wait_visual(host, 0))
        pairs = find_pairs(obs, lambda a, b: a == 0 and b == 0)
        print("INFO - %d empty/empty tile pairs near the host player %s: %s" %
              (len(pairs), tuple(round(x) for x in obs.host_xyz), pairs[:6]))
        check("T1 the snapshot has empty tile pairs near the host (%d)" % len(pairs), len(pairs) >= 3)
        if len(pairs) < 3:
            return
        mark = obs.c.inbox.mark()
        before = {(ux + 1, uz): obs.c.world.tile_ut(ux + 1, uz) for ux, uz in pairs}

        # ---- T1 ----
        probe = None
        n_probe = 0
        counter = 0
        pi = 0
        for name, ixname, kind, uses_t, evs in CASES:
            idx = IDX["mPlayer_INDEX_" + ixname]
            if probe is None or n_probe >= 3:
                if probe is not None:
                    probe.close()
                    peers.remove(probe)
                probe = new_peer("P%d" % len(peers))
                L.pump_sleep(1.0, probe.c.hub)
                n_probe = 0
            n_probe += 1
            ux, uz = pairs[pi % len(pairs)]
            pi += 1
            counter = counter % 15 + 1
            place(probe, ux, uz)
            secs = 3.4 if name in SLOW else 2.4
            t = go(host, probe, idx, counter, kind, secs)
            st = re.search(STATE_RX % (probe.pid, idx, counter), t)
            fx, sn, tg = new_effects(t, probe.pid), sounds(t, probe.pid), targets(t, probe.pid)
            check("T1 %s(%d): entry edge logged with row=%s" % (name, idx, name), st is not None and st.group(1) == name)
            pos_state = st.start() if st else 0
            for kind_e, ename, frame_t, label in evs:
                if kind_e == SN:
                    got = [g for g in sn if g[1] == ename]
                    ok_lab = (lambda lab: (lab in label) if isinstance(label, set) else lab == label)
                    check("T1 %s: sound %s fired once, label ok, budget ok (got %s)" %
                          (name, ename, [(g[2], g[3], g[4]) for g in got]),
                          len(got) == 1 and ok_lab(int(got[0][2])) and got[0][4] == "ok")
                    if got:
                        f = float(got[0][3])
                        check("T1 %s: %s at/after the vanilla frame %.1f (frame=%.1f)" % (name, ename, frame_t, f),
                              (f >= frame_t - 0.01 and f < frame_t + 4.0) if frame_t > 0 else f < 12.0)
                else:
                    got = [g for g in fx if g[1] == ename]
                    check("T1 %s: effect %s fired once, budget ok (got %s)" % (name, ename, [g[6] for g in got]),
                          len(got) == 1 and got[0][6] == "ok")
                    if got:
                        f = float(got[0][2])
                        check("T1 %s: %s at/after the vanilla frame %.1f (frame=%.1f), attr >= 0 (%s)" %
                              (name, ename, frame_t, f, got[0][5]),
                              ((f >= frame_t - 0.01 and f < frame_t + 4.0) if frame_t > 0 else f < 12.0) and
                              int(got[0][5]) >= 0)
            first_ev = min([m.start() for m in (EFF_RX.search(t), SND_RX.search(t)) if m is not None] or [10 ** 9])
            check("T1 %s: the entry edge is logged BEFORE the first event" % name, st is not None and pos_state < first_ev)
            extra = ({g[1] for g in fx} | {g[1] for g in sn}) - {e[1] for e in evs}
            check("T1 %s: no unexpected event names (%s)" % (name, sorted(extra)), not extra)
            bad = [g for g in fx + sn if g[-1] != "ok"]
            check("T1 %s: no suppressed/capped event for a lone puppet in range (%s)" % (name, [g[-1] for g in bad]), not bad)
            if uses_t:
                good_t = [g for g in tg if g[3] == "1" and (int(g[4]), int(g[5])) == (ux + 1, uz) and g[7] == "0" and
                          int(g[6], 16) == 0]
                check("T1 %s: target tile is the tile east of the puppet (%d,%d), snapshot item 0x0000, stump=0 "
                      "(%d target lines, %d match)" % (name, ux + 1, uz, len(tg), len(good_t)),
                      len(tg) >= 1 and len(good_t) == len(tg))
            else:
                check("T1 %s: no target read for this state" % name, not tg)
            if name in ("tired", "struggle_pitfall"):
                fl = [m.groups() for m in FEEL_RX.finditer(t) if int(m.group(1)) == probe.pid]
                print("INFO - %s feel source: %s" % (name, [(g[1], g[2]) for g in fl]))
                check("T1 %s: FEEL position line present (captured or fallback)" % name, len(fl) >= 1)
                check("T1 %s: FEEL joint captured by the puppet's own draw (source=captured)" % name,
                      any(g[1] == "captured" for g in fl))

        # ---- T2: repeated entries ----
        rp = new_peer("RP")
        L.pump_sleep(1.0, rp.c.hub)
        ux, uz = pairs[0]
        place(rp, ux, uz)
        off = len(host.log_text())
        idx_dig = IDX["mPlayer_INDEX_DIG_SCOOP"]
        rp.item_kind = SHOVEL
        for ctr in (5, 6, 7):
            rp.stream(0.8)
            rp.stream(2.0, idx_dig, ctr)
        rp.stream(0.6)
        t = host.log_text()[off:]
        n_s1 = len([g for g in sounds(t, rp.pid) if g[1] == "scoop1"])
        n_h1 = len([g for g in new_effects(t, rp.pid) if g[1] == "dig_hole1"])
        n_ds = len([g for g in new_effects(t, rp.pid) if g[1] == "dig_scoop"])
        check("T2 three dig entries (counters 5,6,7) -> 3 scoop1 sounds, 3 dig_hole1, 3 dig_scoop (%d/%d/%d)" %
              (n_s1, n_h1, n_ds), (n_s1, n_h1, n_ds) == (3, 3, 3))

        # ---- T3: interrupted before the trigger frame ----
        ip = new_peer("IN")
        L.pump_sleep(1.0, ip.c.hub)
        place(ip, ux, uz)
        ip.item_kind = SHOVEL
        off = len(host.log_text())
        for _ in range(3):
            ip.move_once(idx_dig, 9)
            L.pump_sleep(0.05, ip.c.hub)
        ip.stream(1.8, IDX["mPlayer_INDEX_PUTAWAY_SCOOP"], 3)
        ip.stream(0.8)
        t = host.log_text()[off:]
        entered = re.search(STATE_RX % (ip.pid, idx_dig, 9), t) is not None
        n_ev = len([g for g in sounds(t, ip.pid) if g[1] == "scoop1"]) + \
            len([g for g in new_effects(t, ip.pid) if g[1].startswith("dig_")])
        print("INFO - T3 dig row entered=%s dig events=%d" % (entered, n_ev))
        if entered:
            check("T3 a dig_scoop replaced by another row before frame 14 fires none of its frame events (%d)" % n_ev,
                  n_ev == 0)
        else:
            print("INFO - T3 the interpolator never adopted the dig snapshot (too brief): weak check only")

        # ---- T4: another scene; T5: out of range (no field read at all) ----
        sp = new_peer("SH", scene=SCENE_SHOP0)
        L.pump_sleep(2.0, sp.c.hub)
        place(sp, ux, uz)
        t = go(host, sp, idx_dig, 1, SHOVEL, 2.4)
        evs = new_effects(t, sp.pid) + sounds(t, sp.pid)
        check("T4 puppet in the SHOP scene: events present, all budget=scene, never ok (%s)" %
              sorted({g[-1] for g in evs}), len(evs) >= 3 and all(g[-1] == "scene" for g in evs))
        check("T4 no scoop target was read for a puppet in another scene (%d lines)" % len(targets(t, sp.pid)),
              not targets(t, sp.pid))
        fp = new_peer("FAR", far=True)
        L.pump_sleep(1.5, fp.c.hub)
        fp.stream(1.0)  # review R1-L1: the first MOVE of a fresh puppet is adopted silently, so settle with idle MOVEs first
        t = go(host, fp, idx_dig, 1, SHOVEL, 2.4)
        evs = new_effects(t, fp.pid) + sounds(t, fp.pid)
        check("T5 puppet 2500 units away: events present, all budget=range, never ok (%s)" %
              sorted({g[-1] for g in evs}), len(evs) >= 3 and all(g[-1] == "range" for g in evs))
        check("T5 no scoop target was read for an out-of-range puppet (%d lines)" % len(targets(t, fp.pid)),
              not targets(t, fp.pid))

        # (the peers of T1..T5 are no longer needed: the host seats a limited number of peers)
        for p in list(peers):
            if p is not obs:
                p.close()
                peers.remove(p)
        L.pump_sleep(0.5, obs.c.hub)

        # ---- T8: reflect against a tree ----
        trees = find_pairs(obs, lambda a, b: a == 0 and b in tree_ids)
        print("INFO - T8 tree targets (puppet tile, tree east of it): %s" % trees[:4])
        if trees:
            tp = new_peer("TR")
            L.pump_sleep(1.0, tp.c.hub)
            tx, tz = trees[0]
            tv = obs.c.world.tile_ut(tx + 1, tz)
            place(tp, tx, tz)
            t = go(host, tp, IDX["mPlayer_INDEX_REFLECT_SCOOP"], 2, SHOVEL, 1.8)
            tg = [g for g in targets(t, tp.pid) if g[1] == "reflect_scoop_snd"]
            sn = [g for g in sounds(t, tp.pid) if g[1] == "reflect_scoop_snd"]
            fx = [g for g in new_effects(t, tp.pid) if g[1] == "reflect_scoop"]
            check("T8 reflect target line = the tree tile (%d,%d) item 0x%04x (got %s)" %
                  (tx + 1, tz, tv, [(g[3], g[4], g[5], g[6]) for g in tg]),
                  len(tg) == 1 and tg[0][3] == "1" and (int(tg[0][4]), int(tg[0][5])) == (tx + 1, tz) and
                  int(tg[0][6], 16) == tv)
            check("T8 reflect sound is NA_SE_SCOOP_TREE_HIT 0x%x (got %s)" % (NA["NA_SE_SCOOP_TREE_HIT"],
                                                                              [(g[2], g[4]) for g in sn]),
                  len(sn) == 1 and int(sn[0][2]) == NA["NA_SE_SCOOP_TREE_HIT"] and sn[0][4] == "ok")
            check("T8 reflect effect spawned with attribute 7 (STONE) for a tree target (got %s)" %
                  [(g[5], g[6]) for g in fx], len(fx) == 1 and int(fx[0][5]) == 7 and fx[0][6] == "ok")
        else:
            print("INFO - T8 no tree east of an empty tile near the host: SKIPPED (target-dependent branch not exercised)")

        # ---- T9: remove grass ----
        def west_free(a, b):
            return a == 0 and b in GRASS_IDS
        gp = find_pairs(obs, west_free)
        print("INFO - T9 grass tiles (puppet tile, grass east of it): %s" % gp[:4])
        if gp:
            grp = new_peer("GR")
            L.pump_sleep(1.0, grp.c.hub)
            gx, gz = gp[0]
            # stand 20 units west of the grass tile centre: the vanilla 20-ahead probe lands on the weed tile centre
            grp.x, grp.z, grp.y, grp.angle = (gx + 1) * 40.0, gz * 40.0 + 20.0, grp.host_xyz[1], EAST
            grp.stream(1.0)
            t = go(host, grp, IDX["mPlayer_INDEX_REMOVE_GRASS"], 3, -1, 1.8)
            fx = [g for g in new_effects(t, grp.pid) if g[1] == "grass_mud"]
            sn = [g for g in sounds(t, grp.pid) if g[1] == "zassou_nuku"]
            print("INFO - T9 grass_mud=%s zassou_nuku=%s" % ([(g[2], g[6]) for g in fx], [(g[3], g[4]) for g in sn]))
            check("T9 remove_grass on a grass tile: grass_mud effect at frame >= 17, budget ok (got %s)" %
                  [(g[2], g[6]) for g in fx], len(fx) == 1 and float(fx[0][2]) >= 16.99 and fx[0][6] == "ok")
            check("T9 zassou_nuku sound 0x%x at frame >= 17 (got %s)" % (NA["NA_SE_ZASSOU_NUKU"], [(g[2], g[3]) for g in sn]),
                  len(sn) == 1 and int(sn[0][2]) == NA["NA_SE_ZASSOU_NUKU"] and float(sn[0][3]) >= 16.99)
            # facing away: probes land on the west (non-grass) tiles
            grp.angle = WEST
            grp.stream(1.0)
            t = go(host, grp, IDX["mPlayer_INDEX_REMOVE_GRASS"], 4, -1, 1.8)
            fx = [g for g in new_effects(t, grp.pid) if g[1] == "grass_mud"]
            sn = [g for g in sounds(t, grp.pid) if g[1] == "zassou_nuku"]
            check("T9 facing away from the grass: effect budget=notarget and no sound (%s / %d)" %
                  ([g[6] for g in fx], len(sn)), len(fx) == 1 and fx[0][6] == "notarget" and not sn)
        else:
            print("INFO - T9 no grass tile east of an empty tile near the host: SKIPPED")

        # ---- T10: stump ----
        stump_ids = set()
        for nm in ("TREE_STUMP001", "TREE_STUMP002", "TREE_STUMP003", "TREE_STUMP004", "CEDAR_TREE_STUMP001",
                   "TREE_PALM_STUMP001", "GOLD_TREE_STUMP001"):
            try:
                stump_ids.add(ev(nm))
            except Exception:
                pass
        sp2 = find_pairs(obs, lambda a, b: a == 0 and b in stump_ids)
        if sp2:
            stp = new_peer("ST")
            L.pump_sleep(1.0, stp.c.hub)
            sx, sz = sp2[0]
            place(stp, sx, sz)
            t = go(host, stp, idx_dig, 2, SHOVEL, 3.0)
            sn = [g[1] for g in sounds(t, stp.pid)]
            fx = [g[1] for g in new_effects(t, stp.pid)]
            check("T10 dig against a stump tile: kirikabu_scoop, no scoop1, no ground hole particles (%s / %s)" % (sn, fx),
                  "kirikabu_scoop" in sn and "scoop1" not in sn and not [f for f in fx if f.startswith("dig_hole")])
        else:
            print("INFO - T10 no stump tile near the host: SKIPPED (stump conditions SOURCE AUDITED only)")

        # ---- T11 (review H1): pitfall sweat lifetime ----
        for p in list(peers):
            if p is not obs:
                p.close()
                peers.remove(p)
        L.pump_sleep(0.5, obs.c.hub)
        sw = new_peer("SW")
        L.pump_sleep(1.0, sw.c.hub)
        swx, swz = pairs[0]
        place(sw, swx, swz)
        idx_pit = IDX["mPlayer_INDEX_STRUGGLE_PITFALL"]
        kill_rx = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) pitfall sweat kill reason=([\w-]+)")

        def sweat(t):
            return [g for g in new_effects(t, sw.pid) if g[1] == "pitfall_sweat" and g[6] == "ok"]

        def kills(t):
            return [m.group(2) for m in kill_rx.finditer(t) if int(m.group(1)) == sw.pid]

        t = go(host, sw, idx_pit, 3, -1, 2.4, tail=0)
        check("T11 entering STRUGGLE_PITFALL spawns the sweat exactly once for the run (%d) and nothing is killed while "
              "the puppet is still in the row (%s)" % (len(sweat(t)), kills(t)), len(sweat(t)) == 1 and not kills(t))
        off = len(host.log_text())
        sw.stream(1.0)  # idle/other MOVE: the puppet leaves the pitfall row
        t = host.log_text()[off:]
        check("T11 the sweat is killed once after the puppet leaves row 94 (reasons %s)" % kills(t), len(kills(t)) == 1)
        check("T11 no second sweat appears after the kill (%d)" % len(sweat(t)), not sweat(t))
        t = go(host, sw, idx_pit, 4, -1, 2.4, tail=0)
        check("T11 a second pitfall run after the kill spawns a fresh sweat (%d) with no kill during the run (%s)" %
              (len(sweat(t)), kills(t)), len(sweat(t)) == 1 and not kills(t))
        off = len(host.log_text())
        spid = sw.pid
        sw.c.disconnect()
        L.pump_sleep(1.0, obs.c.hub)
        sw.close()
        peers.remove(sw)
        host.wait_for_log(r"player %d pitfall sweat kill reason=destroy" % spid, 15.0, since_offset=off)
        ks = [m.group(2) for m in kill_rx.finditer(host.log_text()[off:]) if int(m.group(1)) == spid]
        check("T11 destroying the puppet while the sweat is active logs the kill (reason=destroy, got %s)" % ks,
              ks[:1] == ["destroy"])
        check("T11 host alive after the sweat kill paths", host.alive())

        # ---- summary counters + T7 ----
        sm_p = new_peer("SM")
        L.pump_sleep(1.0, sm_p.c.hub)
        place(sm_p, ux, uz)
        go(host, sm_p, idx_dig, 1, SHOVEL, 2.4)
        off = len(host.log_text())
        spid = sm_p.pid
        sm_p.c.disconnect()
        L.pump_sleep(1.0, obs.c.hub)
        sm_p.close()
        peers.remove(sm_p)
        host.wait_for_log(r"player %d cosmetics summary: .*tool_effects=" % spid, 15.0, since_offset=off)
        sms = [x for x in SUM_RX.finditer(host.log_text()[off:]) if int(x.group(1)) == spid]
        sm = sms[-1] if sms else None
        check("T7 disconnect summary: one dig -> tool_sounds=1, tool_effects=4, dropped=0 (%s)" %
              (None if sm is None else sm.groups(),),
              sm is not None and int(sm.group(3)) == 1 and int(sm.group(2)) == 4 and int(sm.group(5)) == 0)
        L.pump_sleep(1.0, obs.c.hub)
        ups = [L.field_update_tuple(g) for g in obs.c.field_updates_since(mark)]
        after = {k: obs.c.world.tile_ut(*k) for k in before}
        check("T7 (T1..T10) no FIELD_UPDATE reached the observer (%d)" % len(ups), not ups)
        check("T7 the observer's view of every candidate target tile is unchanged", after == before)

        # ---- T6: 8 peers digging ----
        for p in list(peers):
            p.close()
            peers.remove(p)
        eight = [new_peer("E%d" % i) for i in range(8)]
        mark = eight[0].c.inbox.mark()
        L.pump_sleep(2.0, eight[0].c.hub)
        for i, p in enumerate(eight):
            ux, uz = pairs[i % len(pairs)]
            p.item_kind = SHOVEL
            p.x, p.z, p.y, p.angle = ux * 40.0 + 20.0, uz * 40.0 + 20.0, p.host_xyz[1], EAST
        for _ in range(20):
            for p in eight:
                p.move_once()
            L.pump_sleep(0.05, eight[0].c.hub)
        off = len(host.log_text())
        end = time.monotonic() + 2.6
        while time.monotonic() < end:
            for i, p in enumerate(eight):
                p.move_once(IDX["mPlayer_INDEX_DIG_SCOOP"] if i % 2 == 0 else IDX["mPlayer_INDEX_GET_SCOOP"], 2)
            L.pump_sleep(0.05, eight[0].c.hub)
        for _ in range(10):
            for p in eight:
                p.move_once()
            L.pump_sleep(0.05, eight[0].c.hub)
        t = host.log_text()[off:]
        allev = []
        for p in eight:
            allev += new_effects(t, p.pid) + sounds(t, p.pid)
        budgets = sorted({g[-1] for g in allev})
        n_ok = len([g for g in allev if g[-1] == "ok"])
        peaks = [int(m.group(1)) for m in PEAK_RX.finditer(host.log_text())]
        print("INFO - T6 budgets %s ok=%d peak=%s" % (budgets, n_ok, max(peaks) if peaks else None))
        check("T6 8 simultaneous diggers: events still presented (ok >= 16, got %d)" % n_ok, n_ok >= 16)
        check("T6 only ok/capped/pool budgets occur (%s)" % budgets, set(budgets) <= {"ok", "capped", "pool"})
        check("T6 per-frame effect peak never exceeds the cap %d (peaks %s)" % (FRAME_CAP, peaks[-3:]),
              all(p <= FRAME_CAP for p in peaks))
        check("T6 host alive during/after the burst", host.alive())
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
    ap.add_argument("--port", type=int, default=8400)
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
