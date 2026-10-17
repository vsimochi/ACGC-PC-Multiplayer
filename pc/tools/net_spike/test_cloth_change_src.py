#!/usr/bin/env python3
"""test_cloth_change_src.py - worker CL: (1) other players see a clothes change, (2) the F3 10x speed toggle is gone. Source audit, no game.

  V  vanilla anchors: the inventory equip never enters mPlayer_INDEX_CHANGE_CLOTH (only NPC code requests 101); the menu doll plays
     MENU_CHANGE1 @1.0 STOP + NA_SE_WEAR, Now_Private->cloth is written at doll frame 36; 101 = wear + KIGAE at 16, texture at 36 / 18
  H  receiver hold: a clothing change of a resolved outfit is held (raw fields updated, render buffers not), never the first appearance,
     the poll never resolves a held slot early and releases an expired / actor-less hold; ON by default, PC_PUPPET_CLOTH_ANIM=0 = instant
  P  presentation: synthesized MENU_CHANGE1 clip (frame 1, speed 1.0, STOP), cues at 16 (wear + KIGAE), swap at 36, bound to the MOVE
     pair, ends at the clip end / pair change / any row; a 101 ACTION_EXT row swaps at 36 (MENU_CHANGE1) / 18 (ITAZURA1)
  W  no new wire: the message id table is unchanged by this work (ACTION_EXT 96 still carries 101, no id >= 102 added for it)
  F  F3: no SDLK_F3 / SDL_SCANCODE_F3 handler anywhere in pc/src, no speedhack toggle, nothing sets g_pc_speedhack_enabled, docs updated,
     F4 (ping) kept; the logic step is real-time dt (graph.c), so max_fps=0 / vsync=0 only changes the render rate
  T  test hook: AC_TEST_AEXT_SCRIPT=clothes (menu writes, a real 101 change, WAIT, menu restore)
Usage: python test_cloth_change_src.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
results = []


def ck(d, c):
    results.append((d, bool(c)))
    print(("PASS - " if c else "FAIL - ") + d)


def rd(*p):
    return open(os.path.join(*p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def strip_comments(t):
    return re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", t), flags=re.S)


def body(src, head):
    i = src.index(head)
    return src[i:src.index("\n}\n", i)]


rp = rd(PC, "src", "pc_remote_player.c")
ng = rd(PC, "src", "pc_net_game.c")
pm = rd(PC, "src", "pc_main.c")
hd = rd(ROOT, "src", "game", "m_hand_ovl.c")
tg = rd(ROOT, "src", "game", "m_tag_ovl.c")
iv = rd(ROOT, "src", "game", "m_inventory_ovl.c")
cc = rd(ROOT, "src", "game", "m_player_main_change_cloth.c_inc")
gr = rd(ROOT, "src", "graph.c")
doc = rd(PC, "DOCUMENTATION.md")
rpc = strip_comments(rp)

# ---- V vanilla anchors
callers = []
for base, _, files in os.walk(os.path.join(ROOT, "src")):
    for f in files:
        if f.endswith((".c", ".c_inc", ".cpp")):
            t = strip_comments(rd(base, f))
            if "mPlib_request_main_change_cloth_forNPC_type1(" in t and f != "m_player_lib.c":
                callers.append(f)
ck("V only NPC / test code requests CHANGE_CLOTH 101 (%s)" % sorted(callers),
   callers and all((f.startswith("ac_") or f == "m_player.c") for f in callers) and not any(f.startswith(("m_hand", "m_tag", "m_inventory")) for f in callers))
ck("V the menu equip writes Now_Private->cloth at doll frame 36 (m_hand_ovl.c)",
   re.search(r"passCheck_now\([^;]*player_main_keyframe\.frame_control,\s*36\.0f\)\)\s*\{\s*cloth_p = &Now_Private->cloth;", hd))
ck("V the menu doll plays mIV_ANIM_CHANGE + NA_SE_WEAR on a cloth drop (m_tag_ovl.c)",
   re.search(r"case ITEM1_CAT_CLOTH:\s*submenu->overlay->segment\.change_player_main_anime_idx = mIV_ANIM_CHANGE;\s*sAdo_SysTrgStart\(NA_SE_WEAR\);", tg))
ck("V mIV_ANIM_CHANGE = MENU_CHANGE1 at speed 1.0 STOP (m_inventory_ovl.c)",
   "mPlayer_ANIM_MENU_CHANGE1," in iv and re.search(r"if \(main_anim_idx == mIV_ANIM_CHANGE\) \{\s*cKF_SkeletonInfo_R_init_standard_setframeandspeedandmorphandmode\(&inv_ovl->player_main_keyframe, anime, NULL,\s*1\.0f, 1\.0f", iv))
ck("V vanilla 101: wear + KIGAE at 16, texture at 36 (MENU_CHANGE1) / 18 (ITAZURA1)",
   "Player_actor_Check_AnimationFrame(fc0_p, 16.0f)" in cc and "eEC_EFFECT_KIGAE" in cc and "menu_flag ? 36.0f : 18.0f" in cc)

# ---- H hold
oa = body(rp, "void pc_remote_player_on_appearance(")
ck("H on_appearance computes the clothing change BEFORE overwriting the raw fields",
   oa.index("pc_puppet_cloth_differs(&slot->appearance, appearance)") < oa.index("slot->appearance.cloth_item = appearance->cloth_item"))
ck("H only a resolved, already-valid outfit is animated (never the first appearance)",
   "cloth_new = slot->appearance.valid && slot->resolved_appearance.ready && pc_puppet_cloth_differs" in oa and "cc->hold = 0; /* a new occupant" in oa)
ck("H a held change does not resolve the render buffers at arrival; anything else still does",
   re.search(r"if \(cc == NULL \|\| !cc->hold\) \{\s*pc_remote_player_apply_appearance\(slot\);", oa) and "pending_resolve = 1;" in oa)
ck("H the hold needs a live puppet in play_main and is bounded (PC_CLOTH_HOLD_FRAMES)",
   "slot->in_use && slot->actor != NULL" in oa and "cc->deadline = graph_dt_frame_time(gamePT) + PC_CLOTH_HOLD_FRAMES" in oa and
   re.search(r"#define PC_CLOTH_HOLD_FRAMES\s+150\.0", rp))
dif = body(rp, "static int pc_puppet_cloth_differs(")
ck("H differs = catalog item / design flag / custom design pixels (not face / sunburn / swell)",
   "cur->cloth_item != a->cloth_item" in dif and "memcmp(&cur->design, a->design_record" in dif and "sunburn" not in dif and "face" not in dif)
pl = body(rp, "void pc_remote_player_poll(")
ck("H the poll retry never resolves a held slot and releases an expired / actor-less hold",
   "!(i >= 0 && i < PC_PUPPET_ID_LIMIT && s_cloth_chg[i].hold)" in pl and re.search(r"slot->actor == NULL \|\| !slot->in_use \|\| t > s_cloth_chg\[i\]\.deadline", pl))
ck("H ON by default, PC_PUPPET_CLOTH_ANIM=0 = the old instant swap",
   re.search(r'getenv\("PC_PUPPET_CLOTH_ANIM"\);\s*s_on = \(e != NULL && e\[0\] == \'0\'\) \? 0 : 1;', rp))

# ---- P presentation
st = body(rp, "static int pc_puppet_cloth_step(")
ck("P synthesized clip = MENU_CHANGE1 / MENU_CHANGE1, frame 1, speed 1.0, STOP, normal part table",
   "pc_remote_player_bind_body(v, mPlayer_ANIM_MENU_CHANGE1, mPlayer_ANIM_MENU_CHANGE1, 1.0f, 1.0f, cKF_FRAMECONTROL_STOP, PC_FALLBACK_MORPH, 0)" in st
   and "mPlayer_PART_TABLE_NORMAL" in st)
ck("P cues at 16 (wear + KIGAE), swap at 36",
   re.search(r"#define PC_CLOTH_MENU_CUE\s+16\.0f", rp) and re.search(r"#define PC_CLOTH_MENU_SWAP\s+36\.0f", rp) and
   "NA_SE_WEAR, \"menu_cloth_wear\"" in rp and "eEC_EFFECT_KIGAE, \"menu_cloth_kigae\"" in rp and
   st.index("PC_CLOTH_MENU_CUE)") < st.index("PC_CLOTH_MENU_SWAP)"))
ck("P the clip is bound to the MOVE pair and ends at clip end / pair change / any row / snap",
   all(s in st for s in ("idx != cc->idx", "ctr != cc->ctr", "pp->v.on", "v->row_active", "slot->snap_req", "end_frame - 0.01f")))
ck("P its end rebinds like an ACTION_EXT end and resolves a still-held outfit",
   "v->rebind_from_row = 1;" in st and "menu_clip_end" in st)
ck("P a real 101 row swaps at the vanilla texture frame (ITAZURA1 18, else 36); a pending 101 waits for its row",
   "mPlayer_ANIM_ITAZURA1) ? 18.0f : 36.0f" in st and "newest_idx == mPlayer_INDEX_CHANGE_CLOTH" in st)
ck("P unshown -> instant; moving / busy -> instant with the puff + sound (gated)",
   '"not_shown"' in st and 'standing ? "busy" : "moving"' in st)
ap = body(rp, "static int pc_puppet_aext_apply(")
ck("P wired into pc_puppet_aext_apply (owns the body like a row) after the ACTION_EXT decision",
   "pc_puppet_cloth_enabled() && pc_puppet_cloth_step(self, slot, game, pp, valid, idx, ctr, now)" in ap and
   ap.index("pc_aext_decide(") < ap.index("pc_puppet_cloth_step("))
ck("P ACTION_EXT still allowlists 101 with its KIGAE / wear / HALLOWEEN cues",
   "case PC_AEXT_IDX_CHANGE_CLOTH:" in rd(PC, "src", "pc_action_ext.c") and '"cloth_kigae"' in rp and '"cloth_wear"' in rp and '"cloth_halloween"' in rp)

# ---- W no wire
ck("W no new message id for the clothes change (no 102+ define mentions cloth)",
   not re.search(r"PC_NETGAME_MSG_\w*CLOTH\w*\s*=", ng) and "PC_NETGAME_MSG_ACTION_EXT" in ng)

# ---- F F3
srcs = {}
for base, _, files in os.walk(os.path.join(PC, "src")):
    for f in files:
        if f.endswith((".c", ".cpp", ".h")):
            srcs[f] = strip_comments(rd(base, f))
for base, _, files in os.walk(os.path.join(PC, "include")):
    for f in files:
        if f.endswith(".h"):
            srcs["inc/" + f] = strip_comments(rd(base, f))
f3 = [f for f, t in srcs.items() if re.search(r"SDLK_F3\b|SDL_SCANCODE_F3\b", t)]
ck("F no SDLK_F3 / SDL_SCANCODE_F3 handler in pc/src or pc/include (%s)" % f3, not f3)
ck("F the speedhack toggle is gone", "pc_speedhack_toggle" not in strip_comments(pm) and "[5x]" not in strip_comments(pm))
setters = [f for f, t in srcs.items() if re.search(r"g_pc_speedhack_enabled\s*(=[^=]|\+\+|--|\^=|\|=)", t)]
ck("F nothing assigns g_pc_speedhack_enabled except its 0 definition (%s)" % setters,
   setters == ["pc_main.c"] and len(re.findall(r"g_pc_speedhack_enabled\s*=", srcs["pc_main.c"])) == 1 and
   re.search(r"int\s+g_pc_speedhack_enabled = 0;", srcs["pc_main.c"]))
ck("F F4 ping toggle kept", "SDLK_F4" in strip_comments(pm))
ck("F docs no longer advertise F3", "**F3** = toggle" not in doc and "F3 does nothing" in doc)
ck("F the logic step is real-time dt (graph.c: OSGetTime delta, clamp 4 frames), independent of the frame limiter",
   re.search(r"double delta_time = \(\(u32\)\(current_time - time\)\) \* d_multiplier;", gr) and "dt_num_60fps_frames > 4.0" in gr)
kb = []
for base, _, files in os.walk(PC):
    if "build" in base or "lib" in base.split(os.sep):
        continue
    for f in files:
        if f.endswith(".ini"):
            if re.search(r"(?im)^\s*\w+\s*=\s*F3\s*$", rd(base, f)):
                kb.append(f)
ck("F no .ini binds F3 (%s)" % kb, not kb)

# ---- T
ck("T test hook script 'clothes' (menu writes + a real 101 change + restore)",
   'strcmp(s_script, "clothes") == 0' in ng and "mPlib_request_main_change_cloth_forNPC_type1(game, alt2" in ng and
   "mPlib_change_player_cloth(game, Now_Private->cloth.idx);" in ng)

n_ok = sum(1 for _, c in results if c)
print("%d/%d passed" % (n_ok, len(results)))
sys.exit(0 if n_ok == len(results) else 1)
