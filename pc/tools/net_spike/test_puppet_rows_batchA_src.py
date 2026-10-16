#!/usr/bin/env python3
"""test_puppet_rows_batchA_src.py - SOURCE AUDIT of the P1 puppet animation coverage (batch A rows + row infrastructure,
batch F polish, batch B item-in-hand). TIER: SOURCE AUDIT only (text of pc_remote_player.c / pc_net_game.c compared with the
vanilla src/game/m_player_main_*.c_inc). Nothing here runs the game or proves that a pose LOOKS right; the rows are exercised by
test_move_action_state_wire.py --only s3 (real host process, every index) and the real 3-process test_puppet_rows_real.py.

Checks
  P1  s_row_pin == the vanilla cKF_SkeletonInfo_R_AnimationMove_ct_base mask of EVERY state that calls it (parsed from the .c_inc),
      base z == the state's Set_base_shape_trs z, and no extra entries
  P2  every filled row of a ct_base state carries PC_ROWF_ROOT_PIN, and every ROOT_PIN row has a pin entry
  P3  sender: pcnetgame_facing_rot_y_state lists exactly the states whose mask has ROT_Y; the facing line uses it
  M1  s_row_morph == the vanilla setup morph_counter for every filled row whose setup passes a numeric literal
  R1  batch A / B rows: index -> row name / clip / mode / speed / flags as vanilla
  R2  infra: reverse bind (Base3), FREEZE (stop_net), item_scale curve + draw scale, kind-dependent clips, dig stump, chain morph
  F1  tool-table sounds/effects of the new rows; blinking with a private RNG (never the gameplay RNG); face diag excludes blinks
  D1  DOOR=16 / OUTDOOR=17 labels in the row comments and the roadmap
Usage: python test_puppet_rows_batchA_src.py"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SRC = os.path.join(ROOT, "pc", "src")
GAME = os.path.join(ROOT, "src", "game")
FAILS = []


def check(name, ok):
    print(("PASS - " if ok else "FAIL - ") + name)
    if not ok:
        FAILS.append(name)


def read(p):
    return open(p, encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def func(text, sig):
    i = text.index(sig)
    while True:
        nxt = re.search(r"[{;]", text[i:])
        if nxt.group(0) == "{":
            break
        i = text.index(sig, i + 1)
    j = i + nxt.start()
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[i:k + 1]
    raise ValueError(sig)


def split_args(s):
    out, d, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
        if ch == "," and d == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


MASK_BITS = {"cKF_ANIMATION_TRANS_XZ": 1, "cKF_ANIMATION_TRANS_Y": 2, "cKF_ANIMATION_ROT_Y": 4}
PIN_BITS = {"PC_PIN_XZ": 1, "PC_PIN_Y": 2, "PC_PIN_RY": 4}


def eval_mask(expr, bits):
    expr = " ".join(expr.split())
    if re.fullmatch(r"\d+", expr):
        return int(expr)
    v = 0
    for t in expr.split("|"):
        v |= bits[t.strip()]
    return v


def enum_indexes():
    h = read(os.path.join(ROOT, "include", "m_player.h"))
    block = h[h.index("mPlayer_INDEX_DMA"):h.index("mPlayer_INDEX_NUM")]
    names = re.findall(r"mPlayer_INDEX_(\w+),", block)
    return {n: i for i, n in enumerate(names)}


def vanilla_states():
    """{STATE_NAME: (mask, base_z)} for every m_player_main_<x>.c_inc that calls AnimationMove_ct_base; {STATE: morph literal or None}"""
    pins, morphs = {}, {}
    for f in sorted(glob.glob(os.path.join(GAME, "m_player_main_*.c_inc"))):
        name = os.path.basename(f)[len("m_player_main_"):-len(".c_inc")].upper()
        s = read(f)
        m = re.search(r"AnimationMove_ct_base\((.*?)\);", s, re.S)
        if m:
            mask = eval_mask(split_args(m.group(1))[-1], MASK_BITS)
            t = re.search(r"Set_base_shape_trs\((.*?)\);", s, re.S)
            z = float(split_args(t.group(1))[3].rstrip("f")) if t else 0.0
            pins[name] = (mask, int(z))
        sm = re.search(r"static void Player_actor_setup_main_\w+\(.*?\n\}\n", s, re.S)
        if sm:
            mm = re.search(r"InitAnimation_Base([123])\((.*?)\);", sm.group(0), re.S)
            if mm:
                a = split_args(" ".join(mm.group(2).split()))
                lit = a[7] if mm.group(1) in "12" else a[5]
                morphs[name] = float(lit.rstrip("f")) if re.fullmatch(r"-?\d+(\.\d+)?f?", lit) else None
    return pins, morphs


def main():
    rp = read(os.path.join(SRC, "pc_remote_player.c"))
    ng = read(os.path.join(SRC, "pc_net_game.c"))
    idx = enum_indexes()
    check("enum parsed: 121 main indexes (%d)" % len(idx), len(idx) == 121 and idx["DOOR"] == 16 and idx["OUTDOOR"] == 17)
    vpins, vmorph = vanilla_states()

    # ---- P1 pin table
    tbl = rp[rp.index("static const PCRowPin s_row_pin["):]
    tbl = tbl[:tbl.index("};")]
    pins = {}
    for m in re.finditer(r"\[mPlayer_INDEX_(\w+)\] = \{ ([^,]+), (-?\d+) \}", tbl):
        pins[m.group(1)] = (eval_mask(m.group(2), PIN_BITS), int(m.group(3)))
    check("P1 vanilla ct_base states parsed (%d: %s)" % (len(vpins), sorted(vpins)), len(vpins) >= 25)
    bad = sorted(k for k in set(vpins) | set(pins) if vpins.get(k) != pins.get(k))
    check("P1 s_row_pin == vanilla mask + base z for every ct_base state, no extras (mismatches: %s)" %
          [(k, vpins.get(k), pins.get(k)) for k in bad], not bad)

    # ---- rows
    rows = {}
    tbody = rp[rp.index("static const PCStateRow s_state_rows[mPlayer_INDEX_NUM] = {"):]
    tbody = tbody[:tbody.index("\n};")]
    for m in re.finditer(r"\[mPlayer_INDEX_(\w+)\]\s*=\s*(PCROW_BODY|PCROW_ITEM|PCROW|PCFB)\(", tbody):
        i, d, instr = m.end(), 1, False
        while d:
            ch = tbody[i]
            if ch == '"':
                instr = not instr
            elif not instr and ch == "(":
                d += 1
            elif not instr and ch == ")":
                d -= 1
            i += 1
        rows[m.group(1)] = (m.group(2), " ".join(tbody[m.end():i - 1].split()))
    filled = {k: v for k, v in rows.items() if v[0] != "PCFB"}
    check("rows parsed (%d, %d filled)" % (len(rows), len(filled)), len(rows) == 121)
    nopin = sorted(k for k in filled if k in pins and "PC_ROWF_ROOT_PIN" not in filled[k][1])
    pin_no_tbl = sorted(k for k in filled if "PC_ROWF_ROOT_PIN" in filled[k][1] and k not in pins)
    check("P2 every filled row of a ct_base state has PC_ROWF_ROOT_PIN (%s) and every ROOT_PIN row has a pin entry (%s)" %
          (nopin, pin_no_tbl), not nopin and not pin_no_tbl)

    # ---- P3 sender
    rot = func(ng, "static int pcnetgame_facing_rot_y_state(")
    sent = set(re.findall(r"case mPlayer_INDEX_(\w+):", rot))
    want = set(k for k, (mask, _) in vpins.items() if mask & 4)
    check("P3 sender facing list == the ROT_Y ct_base states (sent %s, vanilla %s)" % (sorted(sent), sorted(want)), sent == want)
    check("P3 the MOVE facing uses shape_info.rotation.y for TURN_DASH or a ROT_Y state, else world.angle.y",
          re.search(r"msg->facing_angle = \(local->now_main_index == mPlayer_INDEX_TURN_DASH \|\| pcnetgame_facing_rot_y_state\(local->now_main_index\)\)\s*"
                    r"\? actor->shape_info\.rotation\.y\s*: actor->world\.angle\.y;", ng) is not None)
    check("P3 the ROT_Y list contains every state the prompt named (34 76 78 86 87 88 90 97 110) + DOOR/KNOCK_DOOR",
          {"SITDOWN", "DEMO_GETON_TRAIN", "DEMO_GETOFF_TRAIN", "ROTATE_OCTAGON", "THROW_MONEY", "PRAY", "MAIL_JUMP", "NOTICE_BEE",
           "SWITCH_ON_LIGHTHOUSE", "DOOR", "KNOCK_DOOR"} <= sent)

    # ---- M1 morph table
    mt = rp[rp.index("static const s8 s_row_morph["):]
    mt = mt[:mt.index("};")]
    morph = {m.group(1): int(m.group(2)) for m in re.finditer(r"\[mPlayer_INDEX_(\w+)\] = (-?\d+)", mt)}
    special = {"TAKEOUT_ITEM", "PUTIN_ITEM", "SWING_FAN", "STOP_NET", "HIDE", "DEMO_STANDING_TRAIN", "WADE_SNOWBALL", "CAST_ROD"}
    mism = []
    for k in sorted(filled):
        if k in special or k not in vmorph or vmorph[k] is None:
            continue
        if morph.get(k, 0) != int(vmorph[k]):
            mism.append((k, vmorph[k], morph.get(k, 0)))
    check("M1 s_row_morph == the vanilla setup morph of every filled row with a literal (mismatches %s)" % mism, not mism)
    check("M1 swing_fan / shock / chain clips / fallback rebinds blend with -5; positive morphs (pull_net 10, lighthouse 9) kept",
          morph.get("SWING_FAN") == -5 and morph.get("SHOCK") == -5 and morph.get("PULL_NET") == 10 and
          morph.get("SWITCH_ON_LIGHTHOUSE") == 9 and "#define PC_ROW_CHAIN_MORPH (-5.0f)" in rp and "#define PC_FALLBACK_MORPH (-5.0f)" in rp
          and rp.count("PC_FALLBACK_MORPH);") >= 2)

    # ---- R1 batch rows
    expect = {
        "PUSH": ("push", "mPlayer_ANIM_PUSH1", "PC_STOP"), "PULL": ("pull", "mPlayer_ANIM_PULL1", "PC_STOP"),
        "PICKUP_FURNITURE": ("pickup_furniture", "mPlayer_ANIM_PICKUP1", "PC_STOP"),
        "SITDOWN": ("sitdown", "mPlayer_ANIM_SITDOWN1", "PC_STOP"), "STANDUP": ("standup", "mPlayer_ANIM_STANDUP1", "PC_STOP"),
        "STOP_NET": ("stop_net", "PC_ROWF_FREEZE", "PC_REPEAT"),
        "DEMO_GETON_TRAIN": ("geton_train", "mPlayer_ANIM_INTRAIN1", "PC_STOP"),
        "DEMO_GETON_TRAIN_WAIT": ("geton_train_wait", "mPlayer_ANIM_INTRAIN_WAIT1", "PC_REPEAT"),
        "DEMO_GETOFF_TRAIN": ("getoff_train", "mPlayer_ANIM_OUTTRAIN1", "PC_STOP"),
        "ROTATE_OCTAGON": ("rotate_octagon", "mPlayer_ANIM_FUKUBIKI1", "PC_STOP"),
        "THROW_MONEY": ("throw_money", "mPlayer_ANIM_SAISEN1", "PC_STOP"), "PRAY": ("pray", "mPlayer_ANIM_OMAIRI_US1", "PC_STOP"),
        "MAIL_JUMP": ("mail_jump", "mPlayer_ANIM_CONFIRM1", "PC_STOP"),
        "MAIL_LAND": ("mail_land", "PC_ROWF_REVERSE", "PC_STOP"),
        "READY_PITFALL": ("ready_pitfall", "mPlayer_ANIM_GURATUKU1", "PC_STOP"),
        "FALL_PITFALL": ("fall_pitfall", "PC_ROW_ANIM_FALL_PITFALL", "PC_STOP"),
        "CLIMBUP_PITFALL": ("climbup_pitfall", "PC_ROW_ANIM_CLIMBUP_PITFALL", "PC_STOP"),
        "NOTICE_BEE": ("notice_bee", "mPlayer_ANIM_HATI3", "PC_STOP"),
        "WADE_SNOWBALL": ("wade_snowball", "PC_ROWF_BODY_CONTINUE", "PC_REPEAT"),
        "SWITCH_ON_LIGHTHOUSE": ("switch_on_lighthouse", "mPlayer_ANIM_LIGHT_ON1", "PC_STOP"),
        "TAKEOUT_ITEM": ("takeout_item", "PC_ROW_ANIM_TAKEOUT", "PC_STOP"),
        "PUTIN_ITEM": ("putin_item", "PC_ROW_ANIM_PUTIN", "PC_STOP"),
    }
    bad = []
    for k, (nm, tok, mode) in expect.items():
        r = filled.get(k)
        if r is None or not r[1].startswith('"%s"' % nm) or tok not in r[1] or mode not in r[1]:
            bad.append((k, r))
    check("R1 the batch A/B rows exist with the vanilla clip / mode (%s)" % bad[:4], not bad)
    check("R1 CLIMBUP_PITFALL plays at speed 1.0 (climbup_pitfall.c_inc:45), the others at 0.5",
          ", 1.0f, 1.0f, PC_ROWF_ROOT_PIN" in filled["CLIMBUP_PITFALL"][1] and ", 0.5f, 1.0f, PC_ROWF_ROOT_PIN" in filled["FALL_PITFALL"][1])
    a0 = func(rp, "static int pc_remote_player_row_anim0(")
    check("R1 kind-dependent clips: OTIRU2/DERU2 with an umbrella, UMB_OPEN1/UMB_CLOSE1 vs PUTAWAY1 (mPlayer_ITEM_IS_UMBRELLA)",
          all(t in a0 for t in ("mPlayer_ANIM_OTIRU2 : mPlayer_ANIM_OTIRU1", "mPlayer_ANIM_DERU2 : mPlayer_ANIM_DERU1",
                                "mPlayer_ANIM_UMB_OPEN1 : mPlayer_ANIM_PUTAWAY1", "mPlayer_ANIM_UMB_CLOSE1 : mPlayer_ANIM_PUTAWAY1",
                                "mPlayer_ITEM_IS_UMBRELLA(raw_kind)")))
    check("R1 net angle of stop_net is ZERO (item_net.c_inc table entry 46), the item clip SWING_WAIT kept from its current frame",
          "PC_ROWF_FREEZE | PC_ROWF_ITEM_CARRY, -1, mPlayer_ITEM_DATA_SWING_WAIT, PC_STOP, 1.0f, PC_NETANG_ZERO" in filled["STOP_NET"][1])

    # ---- R2 infra
    bind = func(rp, "static void pc_remote_player_bind_body(")
    start = func(rp, "static int pc_remote_player_row_start(")
    tick = func(rp, "static void pc_remote_player_row_tick(")
    draw = func(rp, "static void pc_remote_player_draw_item(")
    check("R2 bind_body: reverse -> cKF_SkeletonInfo_R_init_reverse_setspeedandmorphandmode on both layers, else the morph is passed",
          bind.count("cKF_SkeletonInfo_R_init_reverse_setspeedandmorphandmode(") == 2 and bind.count("frame, speed, morph, mode)") == 2)
    check("R2 row_start: FREEZE keeps the bound clips and zeroes both speeds; morph + reverse from the row helpers",
          "PC_ROWF_FREEZE" in start and "v->keyframe0.frame_control.speed = 0.0f;" in start and
          "v->keyframe1.frame_control.speed = 0.0f;" in start and "pc_remote_player_row_morph(idx, row, raw_kind)" in start and
          "pc_remote_player_row_reverse(row, raw_kind)" in start)
    check("R2 row_start: the pin mask comes from s_row_pin (mask + base z), ROOT_PIN rows only",
          "s_row_pin[idx].mask" in start and "(f32)s_row_pin[idx].base_z" in start and "PC_ROWF_ROOT_PIN" in start)
    check("R2 row_start: dig_scoop swaps to DIG_KABU1 when the reconstructed target is a stump (local field only)",
          "mPlayer_ANIM_DIG_KABU1" in start and "pc_puppet_scoop_target(self, &dtgt, &dfg)" in start and "IS_ITEM_TREE_STUMP(dfg)" in start and
          "pc_remote_player_scene_is_local_field(dslot" in start)
    check("R2 take-out: reverse PUTAWAY1 unless umbrella; morph -5 umbrella / +9 otherwise",
          "row->anim0 == PC_ROW_ANIM_TAKEOUT && !mPlayer_ITEM_IS_UMBRELLA(raw_kind)" in func(rp, "static int pc_remote_player_row_reverse(") and
          "mPlayer_ITEM_IS_UMBRELLA(raw_kind) ? -5.0f : 9.0f" in func(rp, "static f32 pc_remote_player_row_morph("))
    check("R2 item_scale: take-out 0 until 36 then get_percent_forAccelBrake(t, 36, 54) + one pose switch (Base1 REPEAT morph 9); put-in 1 - (t/18)",
          "get_percent_forAccelBrake(t, 36.0f, 54.0f, 0.0f, 0.0f)" in tick and "cKF_FRAMECONTROL_REPEAT, 9.0f, 0)" in tick and
          "1.0f - get_percent_forAccelBrake(t, 0.0f, 18.0f, 0.0f, 0.0f)" in tick and "v->row_chained = 1;" in tick)
    check("R2 draw: the carried item is scaled by item_scale (Matrix_scale after Matrix_put), not drawn at 0, umbrellas unscaled",
          "Matrix_scale(v->item_scale, v->item_scale, v->item_scale, MTX_MULT);" in draw and "v->item_scale <= 0.0f" in draw)
    check("R2 put-in remembers the kind being put away when the wire kind clears (last_held_kind / scale_kind)",
          "raw_kind = v->last_held_kind;" in start and "raw_kind = self->visual.scale_kind;" in rp and "visual->last_held_kind = mPlayer_ITEM_KIND_NONE;" in rp)
    check("R2 release ends the scale curve; the chain clip binds with morph -5",
          "v->item_scale_on = 0;" in func(rp, "static void pc_remote_player_row_release(") and "PC_ROW_CHAIN_MORPH, 0);" in rp)
    check("R2 the puppet still never runs AnimationMove_base", "AnimationMove_base" not in re.sub(r'PCFB\(".*"\)', "", re.sub(r"/\*.*?\*/", "", rp, flags=re.S)))

    # ---- F1 cosmetics
    tools = rp[rp.index("static const PCToolRow s_tool_rows[] = {"):]
    tools = tools[:tools.index("\n};")]
    for st, toks in (("PICKUP_FURNITURE", ("NA_SE_ITEM_GET, 10.0f", "NA_SE_GASAGOSO, 20.0f")),
                     ("SITDOWN", ("NA_SE_JUMP, 0.0f", "NA_SE_HARD_CHAIR_SIT, 18.0f")),
                     ("STANDUP", ("NA_SE_HARD_CHAIR_STANDUP, 0.0f", "NA_SE_LANDING, 17.0f")),
                     ("TAKEOUT_ITEM", ("NA_SE_GASAGOSO, 0.0f",)), ("PUTIN_ITEM", ("NA_SE_GASAGOSO, 0.0f",)),
                     ("THROW_MONEY", ("NA_SE_COIN_GASAGOSO, 9.0f", "eEC_EFFECT_COIN, 33.0f")),
                     ("READY_PITFALL", ("eEC_EFFECT_ASE2, 5.0f",)),
                     ("KNOCK_DOOR", ("NA_SE_SOUND_KNOCK, 13.0f", "NA_SE_SOUND_KNOCK, 20.0f"))):
        m = re.search(r"\{ mPlayer_INDEX_%s, (\d+), \{(.*?)\} \} \}," % st, tools, re.S)
        check("F1 tool row %s: %s" % (st, toks), m is not None and all(t in m.group(2) for t in toks) and int(m.group(1)) == m.group(2).count("{ PC_TOOL_"))
    face = func(rp, "static void pc_remote_player_face_pattern(")
    blink = re.sub(r"/\*.*?\*/", "", func(rp, "static int pc_remote_player_blink_eye("), flags=re.S)
    check("F1 blinking: vanilla pattern table {0,0,0,0,1,1,2,2,2,2,2,2,1,1,0,0}, 60..120 frame waits, 0..3 extra blinks, private LCG",
          "{ 0, 0, 0, 0, 1, 1, 2, 2, 2, 2, 2, 2, 1, 1, 0, 0 }" in blink and "blink_rng" in blink and "fqrand" not in blink and
          "RANDOM" not in blink and "get_random_timer" not in blink)
    check("F1 face: blink only when no clip table and no constant state face; the diag line keeps the derived (non-blink) face",
          "pc_remote_player_blink_eye(" in face and face.index("diag_face_eye = e") < face.index("pc_remote_player_blink_eye("))

    # ---- D1
    check("D1 row comments label DOOR 16 / OUTDOOR 17", "/* DOOR 16 (m_player_main_door.c_inc" in rp and "/* OUTDOOR 17 (m_player_main_outdoor.c_inc" in rp)
    road = read(os.path.join(ROOT, "docs", "multiplayer-guest-roadmap.md"))
    check("D1 roadmap labels DOOR (16) / OUTDOOR (17)", "- DOOR (16): row `door`" in road and "- OUTDOOR (17): row `outdoor`" in road)

    print("-" * 60)
    print("%s (%d failed)" % ("ALL PASSED" if not FAILS else "FAILED", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
