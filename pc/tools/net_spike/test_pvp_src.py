"""test_pvp_src.py - SOURCE AUDIT of the player-vs-player tool hits (ids 91 PVP_HIT_REQ / 92 PVP_HIT, pc_pvp_authority.h).

  W  wire: protocol version still 8; ids 91 / 92 are the ONLY additions (wire_baseline audit passes, the enum lines are pinned, 88 / 89 stay
     unused); the two wire structs are pinned field by field with their 16 / 12 byte _Static_asserts; no new TXN kind / reason, no new town
     service (TS_NUM 9) and no HOST_CONFIG flag for this feature
  A  the axe: Player_actor_Check_axe_after filters puppets (pc_pvp_is_puppet_actor) under TARGET_PC, so a puppet contact never reflects
     (no +3 wear); the puppet test compares the puppet's own mv / dw procs
  S  the shovel: mPlib_Check_scoop_after returns AIR_SCOOP within 39 units of a puppet (like a villager), right after the villager check
  N  the net: HitBGCheck_Swing_net asks pc_net_game_pvp_net_hit (STOP_NET + sound through the vanilla path)
  V  the victim: the request comes from the player's own move (Player_actor_move_other_func1 pre / post hooks), never from a network
     handler; the allowlist is exactly WAIT / WALK / RUN / DASH / TURN_DASH / READY_NET / READY_WALK_NET; the balloon guard turns TUMBLE into
     SHOCK; the client handler only queues
  C  conservation: no item / pocket / wallet / axe-wear / field write in the PvP code (pc_pvp_authority.c, the pc_net_game.c block, the
     m_player.c hooks)
  R  the puppet SHOCK row exists (GAAAN1 -> chain GAAAN2)
  T  the native selftest is present and compiles the real module; the feature has no player / host switch (only the TEST-ONLY env)
Run: python test_pvp_src.py
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, HERE)
import wire_baseline  # noqa: E402

results = []


def check(desc, cond):
    results.append(bool(cond))
    print(("PASS - " if cond else "FAIL - ") + desc)


def rd(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def block(text, start, end):
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j]


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def main():
    gc = rd("pc/src/pc_net_game.c")
    gh = rd("pc/include/pc_net_game.h")
    ph = rd("pc/include/pc_pvp_authority.h")
    pcs = rd("pc/src/pc_pvp_authority.c")
    rp = rd("pc/src/pc_remote_player.c")
    mp = rd("src/game/m_player.c")
    mpc = rd("src/game/m_player_common.c_inc")
    mpl = rd("src/game/m_player_lib.c")
    msn = rd("src/game/m_player_main_swing_net.c_inc")
    mof = rd("src/game/m_player_other_func.c_inc")
    cm = rd("pc/CMakeLists.txt")

    # ---- W wire
    wire_baseline.run(check, ROOT)
    check("W protocol version is still 8", wire_baseline.header_protocol_ok(gh) and wire_baseline.EXPECTED_PROTOCOL_VERSION == 8)
    ids = dict((n, v) for n, v in wire_baseline.c_message_ids_raw(gc))
    check("W PC_NETGAME_MSG_PVP_HIT_REQ = 91 and PC_NETGAME_MSG_PVP_HIT = 92", ids.get("PC_NETGAME_MSG_PVP_HIT_REQ") == 91 and ids.get("PC_NETGAME_MSG_PVP_HIT") == 92)
    vals = sorted(ids.values())
    check("W ids 88 / 89 / 93-95 stay unused, ids above 92 only the puppet sprint 96 / 97 / 98 / 99 and the text chat 100 CHAT_SEND / 101 CHAT_MSG (pc_chat.h), all unique (%s)" % vals[-6:], 88 not in vals and 89 not in vals and 92 in vals and all(v in (96, 97, 98, 99, 100, 101, 102, 103, 104) for v in vals if v > 92) and len(vals) == len(set(vals)))
    req = re.search(r"typedef struct PCPvpHitReqMsg \{(.*?)\} PCPvpHitReqMsg;", ph, re.S)
    hit = re.search(r"typedef struct PCPvpHitMsg \{(.*?)\} PCPvpHitMsg;", ph, re.S)
    norm = lambda m: " ".join(strip_comments(m.group(1)).split()) if m else None  # noqa: E731
    check("W PCPvpHitReqMsg pinned: %s" % norm(req),
          norm(req) == "uint8_t msg_type; uint8_t target; uint8_t tool; uint8_t rsv; uint16_t seq; int16_t facing; float pos_x, pos_z;")
    check("W PCPvpHitMsg pinned: %s" % norm(hit),
          norm(hit) == "uint8_t msg_type; uint8_t attacker; uint8_t tool; uint8_t reaction; int16_t knock_angle; uint16_t seq; float knock_speed;")
    check("W size asserts 16 / 12", "_Static_assert(sizeof(PCPvpHitReqMsg) == 16," in ph and "_Static_assert(sizeof(PCPvpHitMsg) == 12," in ph)
    check("W tool / reaction values pinned (axe 1, shovel 2, net 3; tumble 1, shock 2, flinch 3)",
          "PC_PVP_TOOL_AXE = 1, PC_PVP_TOOL_SHOVEL = 2, PC_PVP_TOOL_NET = 3" in ph and "PC_PVP_REACT_TUMBLE = 1, PC_PVP_REACT_SHOCK = 2, PC_PVP_REACT_FLINCH = 3" in ph)
    kinds = [int(v) for v in re.findall(r"#define PC_NETGAME_TXN_KIND_\w+\s+(\d+)u", gc)]
    reasons = [int(v) for v in re.findall(r"#define PC_NETGAME_TXN_REASON_\w+\s+(\d+)u", gc)]
    check("W no new TXN kind for PvP (max %s <= 22: kind 21 is worker E's EVNPC_CLAIM, 22 outdoor furniture C1) / reason (max %s <= 62: 50..54 are worker E's, 55..62 outdoor furniture)" % (max(kinds) if kinds else None, max(reasons) if reasons else None),
          kinds and reasons and max(kinds) <= 22 and max(reasons) <= 62 and "PVP" not in " ".join(re.findall(r"#define (PC_NETGAME_TXN_(?:KIND|REASON)_\w+)", gc)))
    check("W town services unchanged (TS_NUM 9) and no PVP host-config flag", ("#define PC_NETGAME_TS_NUM      10u" in gc or "#define PC_NETGAME_TS_NUM      12u" in gc) and not re.search(r"HOSTCFG\w*PVP|PVP\w*HOSTCFG", gc))
    check("W the PvP msg ids are dispatched host-side (91) and client-side (92) with exact sizes",
          "size == sizeof(PCPvpHitReqMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_PVP_HIT_REQ" in gc and "size == sizeof(PCPvpHitMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_PVP_HIT" in gc)

    # ---- A axe
    ca = block(mpc, "static int Player_actor_Check_axe_after(", "static int Player_actor_")
    check("A Check_axe_after filters puppets under TARGET_PC before the reflect decision",
          re.search(r"Player_actor_Check_OBJtoLine_forItem_axe\(actorx\)\s*#ifdef TARGET_PC.*?!pc_pvp_is_puppet_actor\(player->item_axe_tris\.collision_obj\.collided_actor\)\s*#endif\s*\)", ca, re.S))
    check("A pc_pvp_is_puppet_actor compares the puppet's own mv / dw procs",
          re.search(r"int pc_pvp_is_puppet_actor\(const void\* actor\) \{.*?a->mv_proc == pc_remote_player_mv && a->dw_proc == pc_remote_player_dw", rp, re.S))
    check("A the reflect wear (+3) path itself is untouched (vanilla)", "axe_damage = prev_damage + 3;" in mpc)

    # ---- S shovel
    sc = block(mpl, "extern int mPlib_Check_scoop_after(", "\n}\n")
    check("S AIR_SCOOP within 39 units of a puppet, right after the villager check",
          re.search(r"mPlib_Search_exist_npc_inCircle_forScoop\(game, pos_p, SQ\(39\.0f\)\);.*?#ifdef TARGET_PC.*?if \(pc_pvp_puppet_near\(pos_p->x, pos_p->z, 39\.0f\)\) \{\s*return mPlayer_INDEX_AIR_SCOOP;\s*\}\s*#endif", sc, re.S))

    # ---- N net
    hb = block(msn, "static int Player_actor_HitBGCheck_Swing_net(", "\n}\n")
    check("N the net head asks pc_net_game_pvp_net_hit after the vanilla OBJ check and returns TRUE (STOP_NET + AMI_HIT)",
          re.search(r"#ifdef TARGET_PC.*?if \(pc_net_game_pvp_net_hit\(ns, ne, pp, actor->shape_info\.rotation\.y\)\) \{\s*return TRUE;", hb, re.S))

    # ---- V victim
    check("V the pre / post hooks run from the player's own move (Player_actor_move_other_func1)",
          re.search(r"static void Player_actor_move_other_func1\(ACTOR\* actor, GAME\* game\) \{\s*#ifdef TARGET_PC\s*Player_actor_pc_pvp_pre\(actor, game\);.*?Player_actor_change_proc_index\(actor, game\);\s*#ifdef TARGET_PC\s*Player_actor_pc_pvp_post\(actor, game\);", mof, re.S))
    pre = block(mp, "static void Player_actor_pc_pvp_pre(ACTOR* actorx, GAME* game) {", "\n}\n")
    check("V the victim requests the REAL tumble / shock (start time 0) / flinch (STUNG_MOSQUITO + the PvP label) and passes the balloon state",
          "Player_actor_request_main_tumble(game, mPlayer_REQUEST_PRIORITY_14)" in pre
          and "Player_actor_request_main_shock_all(game, 0.0f, actorx->shape_info.rotation.y, FALSE, FALSE" in pre
          and "requested_main_index_data.stung_mosquito.label = PC_PVP_FLINCH_LABEL" in pre and "Player_actor_request_main_index(game, mPlayer_INDEX_STUNG_MOSQUITO" in pre
          and "mPlayer_ITEM_IS_BALLOON(player->item_kind)" in pre)
    check("V SHOCK / FLINCH never turn the victim: the shock target angle is its OWN facing; the host angle (face) is unused",
          "(void)face;" in pre and not re.search(r"shock_all\(game, 0\.0f, face", pre))
    check("V the shovel SHOCK is held to PC_PVP_SHOCK_FRAMES (stronger than the flinch)", "player->main_data.shock.end_time = PC_PVP_SHOCK_FRAMES;" in mp
          and "#define PC_PVP_SHOCK_FRAMES 84.0f" in ph)
    smq = rd("src/game/m_player_main_stung_mosquito.c_inc")
    check("V the PvP flinch ends in WAIT, never in the NOTICE_MOSQUITO dialogue",
          re.search(r"#ifdef TARGET_PC\s*if \(stung_mosquito_p->label == PC_PVP_FLINCH_LABEL\) \{.*?Player_actor_request_main_wait_all\(game.*?return;\s*\}\s*#endif\s*Player_actor_request_main_notice_mosquito", smq, re.S))
    check("V reactions per tool: axe TUMBLE, shovel SHOCK, net FLINCH (distinct states 11 / 99 / 107)",
          "(m->tool == PC_PVP_TOOL_AXE) ? PC_PVP_REACT_TUMBLE : (m->tool == PC_PVP_TOOL_SHOVEL ? PC_PVP_REACT_SHOCK : PC_PVP_REACT_FLINCH)" in pcs
          and "PC_PVP_IDX_STUNG_MOSQUITO = 107" in ph and "(int)PC_PVP_IDX_STUNG_MOSQUITO == (int)mPlayer_INDEX_STUNG_MOSQUITO" in gc)
    ok_fn = block(pcs, "int pc_pvp_victim_state_ok(int i) {", "\n}\n")
    cases = re.findall(r"case (PC_PVP_IDX_\w+):", ok_fn)
    check("V the allowlist is exactly WAIT / WALK / RUN / DASH / TURN_DASH / READY_NET / READY_WALK_NET (%s)" % cases,
          sorted(cases) == sorted(["PC_PVP_IDX_WAIT", "PC_PVP_IDX_WALK", "PC_PVP_IDX_RUN", "PC_PVP_IDX_DASH", "PC_PVP_IDX_TURN_DASH", "PC_PVP_IDX_READY_NET", "PC_PVP_IDX_READY_WALK_NET"]))
    check("V the balloon guard: TUMBLE becomes SHOCK while a balloon is held",
          re.search(r"if \(reaction == PC_PVP_REACT_TUMBLE && holding_balloon\) \{\s*return PC_PVP_REACT_SHOCK;", pcs))
    hc = block(gc, "static void pcnetgame_handle_client_pvp_hit(", "\n}\n")
    check("V the client handler only queues (no player request from the network handler)", "pc_pvp_victim_offer" in hc and "request_main" not in hc and "mPlib_" not in hc)
    vt = block(gc, "int pc_net_game_pvp_victim_take(", "\n}\n")
    check("V the victim take is blocked by a demo / submenu / pause", "mDemo_CheckDemo()" in vt and "submenu.open_flag" in vt and "_Game_play_isPause" in vt)
    check("V the index constants are _Static_asserted against m_player.h", "(int)PC_PVP_IDX_SHOCK == (int)mPlayer_INDEX_SHOCK" in gc and "(int)PC_PVP_IDX_WAIT == (int)mPlayer_INDEX_WAIT" in gc)

    # ---- C conservation
    pvp_gc = block(gc, "/* ---- Player-vs-player tool hits (PC_NETGAME_MSG_PVP_HIT_REQ = 91", "/* ---- Tree-drop falling-item animation")
    hooks = block(mp, "/* Player-vs-player tool hits (pc_pvp_authority.h): REACTIONS ONLY.", "#endif")
    forbidden = re.compile(r"Common_Set\(|axe_damage|inventory\.|\.wallet|pockets\[|equipment\s*=|mPr_Set|mPr_Clear|pcfa_set_tile|mFI_SetFG|mFI_UtNumtoFGSet|Set_ItemNoSubmenu")
    for name, txt in (("pc_pvp_authority.c", pcs), ("the pc_net_game.c PvP block", pvp_gc), ("the m_player.c hooks", hooks)):
        bad = forbidden.findall(strip_comments(txt))
        check("C no item / pocket / wallet / axe-wear / field write in %s (%s)" % (name, bad), not bad)

    # ---- R row
    check("R the puppet FLINCH row (STUNG_MOSQUITO: MOSQUITO1 STOP) exists",
          '[mPlayer_INDEX_STUNG_MOSQUITO] = PCROW_ITEM("stung_mosquito", mPlayer_ANIM_MOSQUITO1' in rp)
    check("R the puppet SHOCK row: GAAAN1 STOP -> chain GAAAN2",
          '[mPlayer_INDEX_SHOCK] = PCROW_ITEM("shock", mPlayer_ANIM_GAAAN1, PC_NORMAL, PC_STOP, PC_ROWF_PT_FROM_ANIM1, mPlayer_ANIM_GAAAN2, PC_NETANG_RESET)' in rp)

    # ---- F face / sound of the reactions on the other players' puppets (no wire change)
    check("F the puppet face is derived from its own clip + frame (the vanilla eye / mouth tex-anim tables), not fixed to pattern 0",
          "static void pc_remote_player_face_pattern(" in rp and "mPlib_Get_PlayerEyeTexAnimation_p(a0)" in rp and "mPlib_Get_PlayerMouthTexAnimation_p(a0)" in rp
          and "pc_remote_player_face_pattern(self, &eye, &mouth);" in rp and "eye_tex_p = (u8*)appearance->face_tex + 0 * 0x100;" not in rp)
    check("F the SHOCK GAAAN2 constant face (eye 6 / mouth 5) is reproduced",
          re.search(r"case mPlayer_INDEX_SHOCK:\s*if \(a0 == mPlayer_ANIM_GAAAN2\) \{\s*ce = 6;\s*cm = 5;", rp))
    check("F the puppet SHOCK row spawns the vanilla shock effect at frame 5 (its own SE 0x2D at the puppet)",
          '{ PC_TOOL_FX, PC_TPOS_FEEL, 0, 0, eEC_EFFECT_SHOCK, 5.0f, "shock", 0, PC_TFL_ATTR0 }' in rp)
    check("F the puppet STUNG_MOSQUITO row plays the sting SE NA_SE_6A on entry, and the PvP flinch victim plays it locally",
          '{ PC_TOOL_SND, PC_TPOS_WORLD, 0, 0, NA_SE_6A, 0.0f, "flinch_6a", 0, 0 }' in rp and "sAdo_OngenTrgStart(NA_SE_6A, &actorx->world.position);" in mp)
    check("F no new message id for the face / sound sync (max id 92, or only the puppet sprint 96 / 97 / 98 / 99 and the text chat 100 / 101 above it)", max(wire_baseline.c_message_ids_raw(gc), key=lambda x: x[1])[1] in (92, 96, 97, 98, 99, 100, 101, 102, 103, 104))

    # ---- T misc
    check("T pc_pvp_authority.c is in the game build", "src/pc_pvp_authority.c" in cm)
    check("T no player / host switch: the only off switch is the TEST-ONLY env inside PC_NET_TEST_HOOKS",
          re.search(r"int pc_net_game_pvp_test_off\(void\) \{\s*#ifdef PC_NET_TEST_HOOKS.*?AC_TEST_PVP_OFF.*?#else\s*return 0;\s*#endif", gc, re.S)
          and not re.search(r"g_pc_settings\.\w*pvp|--\w*pvp", gc, re.I))
    st = os.path.join(HERE, "pvp_selftest.c")
    check("T native selftest present and includes the real module", os.path.isfile(st) and '#include "../../src/pc_pvp_authority.c"' in open(st).read())
    gcc = None
    for cand in ("gcc", r"C:\msys64\ucrt64\bin\gcc.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True, timeout=30)
            gcc = cand
            break
        except Exception:  # noqa: BLE001
            continue
    if gcc is not None:
        out = os.path.join(os.environ.get("TEMP", HERE), "pvp_selftest_src_audit.exe")
        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + os.pathsep + r"C:\msys64\ucrt64\bin"
        cp = subprocess.run([gcc, "-std=gnu11", "-Wall", "-Iinclude", "tools/net_spike/pvp_selftest.c", "-lm", "-o", out], cwd=os.path.join(ROOT, "pc"),
                            capture_output=True, text=True, timeout=120, env=env)
        ok = cp.returncode == 0
        if ok:
            rp2 = subprocess.run([out], capture_output=True, text=True, timeout=60, env=env)
            ok = rp2.returncode == 0
            print("INFO - " + rp2.stdout.strip().splitlines()[-1] if rp2.stdout.strip() else "")
        check("T the native selftest builds and passes", ok)
    else:
        print("INFO - gcc not found: native selftest not run here")
    print("\n%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
