#!/usr/bin/env python3
"""test_g7_disconnected_client_gates.py - G7 (v7, no wire/save change) source-audit checks.
NO game process is launched; runtime behavior is NOT TESTED by this script.

A CLIENT process (role CLIENT, only ever via --connect) whose link is not READY (still connecting, or the host is
gone) used to fall back to vanilla LOCAL mutations at the class-A world-mutation sites, because they were gated on
pc_net_game_world_is_host_authoritative() (role CLIENT && link READY). They now use the role test
pc_net_game_role() == PC_NETGAME_ROLE_CLIENT; the request functions no-op when not READY, so such a client can
neither mutate its local world nor grant itself a reward. Host/solo (role HOST/NONE) fall through unchanged.
Usage: python3 test_g7_disconnected_client_gates.py
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from net_spike_lib import check, summary_and_exit_code  # noqa: E402
import wire_baseline  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
CLIENT = "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT"
NOT_CLIENT = "pc_net_game_role() != PC_NETGAME_ROLE_CLIENT"
READYAWARE = "pc_net_game_world_is_host_authoritative()"


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read().replace("\r\n", "\n")


def strip_comments(s):
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", s, flags=re.S))


def func_body(src, header_re, which=0):
    ms = list(re.finditer(header_re, src))
    if len(ms) <= which:
        return ""
    m = ms[which]
    i = src.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    return ""


def site(results, tag, body, mutators=(), token=CLIENT, host_tail=()):
    check("%s: function found" % tag, body != "", results)
    body = strip_comments(body)
    check("%s: role test present" % tag, token in body, results)
    check("%s: READY-aware call gone from this function" % tag, READYAWARE not in body, results)
    g = body.find(token)
    for m in mutators:
        pos = body.find(m)
        check("%s: guard precedes %s" % (tag, m), g >= 0 and pos >= 0 and g < pos, results)
    for h in host_tail:
        check("%s: host/solo fall-through intact (%s)" % (tag, h), h in body, results)


def main():
    results = []

    # ---- S1 dig buried / shine / gold-shovel money (+ explicit swallow)
    pc = read("src/game/m_player_common.c_inc")
    b = func_body(pc, r"\nstatic int Player_actor_CheckAndRequest_main_scoop_all\(GAME\* game, int priority\) \{")
    site(results, "S1 dig", b, ["pc_net_game_request_dig_shine_with_grant(", "pc_net_game_request_dig_hole_with_grant(",
                                "pc_net_game_request_dig_buried(", "Player_actor_request_main_get_scoop_all("],
         host_tail=["} else\n#endif\n            if (Player_actor_request_main_get_scoop_all(game, &target_pos, item, priority)) {"])
    sw = b.find("pc_net_game_client_link_state() != PC_NETGAME_LINK_READY")
    check("S1 dig: explicit not-READY swallow (return TRUE) after the buried request, before the vanilla get_scoop",
          0 <= b.find("pc_net_game_request_dig_buried(") < sw < b.find("Player_actor_request_main_get_scoop_all(")
          and "return TRUE;" in b[sw:sw + 120], results)

    # ---- S2 money rock / bee-birth
    mr = func_body(pc, r"\nstatic int Player_actor_Search_STONE_TC\(ACTOR\* actorx, xyz_t\* target_pos_p\) \{")
    site(results, "S2 money rock", mr, ["pc_net_game_request_money_rock_hit(", "ten_coin_entry_ex_proc(target_pos_p"],
         host_tail=["pc_net_game_role() == PC_NETGAME_ROLE_HOST", "pc_net_game_host_local_money_rock_hit("])
    check("S2 money rock: not-READY client still returns TRUE (no vanilla ten_coin_entry)",
          "pc_net_game_request_money_rock_hit(ut_x, ut_z);\n            }\n            return TRUE;" in mr, results)
    bb = func_body(pc, r"\nstatic int Player_actor_Check_BirthBee_common\(ACTOR\* actorx, mActor_name_t item, int tree_ut_x, "
                       r"int tree_ut_z,\n\s+s16\* angle_y_p\) \{")
    site(results, "S2 bee-birth", bb, ["pc_net_game_request_tree_shake(", "item_tree_fruit_drop_proc(item"],
         host_tail=["pc_net_game_host_local_tree_shake(", "pc_tree_birth_bee_visual("])

    # ---- S1 bury / plant / exchange (m_tag_ovl.c)
    tag = read("src/game/m_tag_ovl.c")
    pl = func_body(tag, r"\nstatic void mTG_plant_proc\(Submenu\* submenu, mSM_MenuInfo_c\* menu_info\) \{")
    site(results, "S1 bury (mTG_plant_proc shovel branch)", pl,
         ["pc_net_game_request_bury(", "mTG_open_warning_window(submenu, menu_info, mWR_WARNING_PUT_PLANT);"],
         host_tail=["pc_net_game_role() == PC_NETGAME_ROLE_HOST", "pc_net_game_host_local_bury("])
    check("S1 bury (plant): M9-D G2-1 no-shovel plant block still uses the role test",
          "G2-1" in pl and CLIENT in pl[pl.find("G2-1"):], results)
    bu = func_body(tag, r"\nstatic void mTG_bury_proc\(Submenu\* submenu, mSM_MenuInfo_c\* menu_info\) \{")
    site(results, "S1 bury (mTG_bury_proc)", bu,
         ["pc_net_game_request_bury(", "mTG_open_warning_window(submenu, menu_info, mWR_WARNING_PUT_ITEM);"],
         host_tail=["pc_net_game_role() == PC_NETGAME_ROLE_HOST", "pc_net_game_host_local_bury("])
    for nm, body in (("plant", pl), ("bury", bu)):
        check("S1 bury (%s): request failure shows the warning and returns before any pocket write" % nm,
              re.search(r"\} else \{\n[^}]*mTG_open_warning_window\(submenu, menu_info, mWR_WARNING_PUT_(PLANT|ITEM)\);\n"
                        r"\s+\}\n\s+return;", body) is not None, results)
    ex = func_body(tag, r"\nstatic void mTG_exchange_proc\(Submenu\* submenu, mSM_MenuInfo_c\* menu_info\) \{")
    site(results, "S1 exchange hole branch", ex,
         ["mPlib_request_main_putin_scoop_from_submenu((xyz_t*)menu_info->data2, EMPTY_NO"],
         host_tail=["mPlib_request_main_putin_scoop_from_submenu((xyz_t*)menu_info->data2, item, demo_gold_scoop);"])
    check("S1 exchange: M9-D G4-2 client drop-request branch (role test) still precedes the local field write",
          0 <= ex.find(CLIENT) < ex.find("pc_net_game_exchange_request_drop(") < ex.find("mTG_common_throw_put_field("),
          results)

    # ---- S1 putin_scoop defensive choke
    ps = func_body(read("src/game/m_player_main_putin_scoop.c_inc"),
                   r"\nstatic void Player_actor_setup_main_Putin_scoop\(ACTOR\* actor, GAME\* game\) \{")
    site(results, "S1 putin_scoop", ps, ["player_drop_entry_proc(game, item, ut_x, ut_z, 0);"], token=NOT_CLIENT,
         host_tail=["#else\n            Common_Get(clip).bg_item_clip->player_drop_entry_proc(game, item, ut_x, ut_z, 0);"])

    # ---- S1 treasure bury (feeds dig buried); both region variants
    nt = read("src/game/m_notice.c")
    for i in (0, 1):
        t = func_body(nt, r"\nstatic void mNtc_check_treasure\(\) \{", i)
        site(results, "S1 treasure variant %d" % i, t, ["Save_GetPointer(treasure_buried_time)", "mFI_SetTreasure("],
             host_tail=["Save_GetPointer(treasure_buried_time)"])
        check("S1 treasure variant %d: early return" % i,
              "if (%s) {\n        return;\n    }" % CLIENT in t, results)

    # ---- S2 mechanical swaps
    ds = func_body(read("src/game/m_player_main_dig_scoop.c_inc"),
                   r"\nstatic void Player_actor_Put_Hole_Dig_scoop\(ACTOR\* actor\) \{")
    site(results, "S2 dig hole", ds, ["pc_net_game_request_dig_hole(", "dig_hole_effect_entry_proc(item, pos, 12, 0)"],
         host_tail=["dig_hole_effect_entry_proc(item, pos, 12, 0)", "fly_entry_proc("])
    check("S2 dig hole: not-READY client returns right after the request block (no local hole)",
          "pc_net_game_request_dig_hole(ux, uz, (uint8_t)hn);\n                }\n                return;" in ds, results)
    fs = func_body(read("src/game/m_player_main_fill_scoop.c_inc"),
                   r"\nstatic void Player_actor_Reset_Hole_Fill_scoop\(ACTOR\* actor\) \{")
    site(results, "S2 fill", fs, ["pc_net_game_request_fill_hole(", "bury_hole_effect_entry_proc(EMPTY_NO, pos, 46)"],
         host_tail=["bury_hole_effect_entry_proc(EMPTY_NO, pos, 46)"])
    check("S2 fill: not-READY client returns right after the request block (no local fill)",
          "pc_net_game_request_fill_hole(ux, uz);\n            }\n            return;" in fs, results)
    pf = func_body(read("src/game/m_player_main_ready_pitfall.c_inc"),
                   r"\nstatic void Player_actor_setup_main_Ready_pitfall\(ACTOR\* actorx, GAME\* game\) \{")
    site(results, "S2 pitfall", pf, ["pc_net_game_request_pitfall_consume(", "Player_actor_SetupItem_Base1("])
    da = func_body(read("src/game/m_player_main_dash.c_inc"),
                   r"\nstatic int Player_actor_SetEffectRemoveFlower_Dash\(ACTOR\* actor, GAME\* game, s16 angle\) \{")
    # WEEDS (v8 unreleased): the G2-2 `return FALSE` guard became the host-authoritative FLOWER_TRAMPLE request (result 1 = not sent: FALSE)
    site(results, "S2 trample", da, ["pc_net_game_request_trample_flower(", "fade_entry_proc(name, actor_pos)",
                                     "mISL_SetNowPlayerAction(mISL_PLAYER_ACTION_TRAMPLE_FLOWER)"],
         host_tail=["fade_entry_proc(name, actor_pos)"])
    check("S2 trample: RANDOM(4) draw precedes the guard (RNG stream unchanged)",
          0 <= da.find("RANDOM(4)") < da.find(CLIENT), results)
    st = func_body(read("src/game/m_player_main_shake_tree.c_inc"),
                   r"\nstatic void Player_actor_SetEffect_Shake_tree\(ACTOR\* actorx, GAME\* game\) \{")
    site(results, "S2 shake", st,
         ["pc_net_game_request_tree_shake(", "item_tree_fruit_drop_proc(item, ut_x, ut_z, &drop_pos)"],
         host_tail=["pc_net_game_host_local_tree_shake(", "item_tree_fruit_drop_proc(item, ut_x, ut_z, &drop_pos)"])
    ax = func_body(read("src/game/m_player_main_swing_axe.c_inc"),
                   r"\nstatic void Player_actor_CutTree_Swing_axe\(ACTOR\* actor, GAME\* game, "
                   r"mPlayer_main_swing_axe_c\* main_axe, int flag\) \{")
    site(results, "S2 chop", ax, ["pc_net_game_request_tree_chop(", "Player_actor_Get_TreeNoToStumpNo(actor, game, item"],
         host_tail=["pc_net_game_host_local_tree_chop(", "pc_net_game_role() == PC_NETGAME_ROLE_HOST",
                    "Player_actor_Get_TreeNoToStumpNo(actor, game, item"])
    check("S2 chop: outer test is CLIENT || HOST and the inner request test is CLIENT",
          "if (%s || pc_net_game_role() == PC_NETGAME_ROLE_HOST) {" % CLIENT in ax
          and "if (%s) {\n                    pc_net_game_request_tree_chop" % CLIENT in ax, results)
    sn = func_body(read("src/actor/ac_snowman.c"),
                   r"\nstatic void aSNOWMAN_Set_PSnowman_info\(SNOWMAN_ACTOR\* actor\) \{")
    site(results, "S2 snowman build", sn, ["pc_net_game_request_snowman_build(", "mSN_regist_snowman_society(&sman_info)"],
         host_tail=["mSN_regist_snowman_society(&sman_info)", "pc_net_game_field_tile_reserved("])
    pm = func_body(read("src/actor/ac_psnowman.c"), r"\nstatic void aPSM_actor_move\(ACTOR\* actor, GAME\* game\) \{")
    site(results, "S2 snowman break", pm, ["pc_net_game_request_snowman_break(", "mSN_ClearSnowman(&actor->npc_id)"],
         host_tail=["mSN_ClearSnowman(&actor->npc_id)"])

    # ---- remaining READY-aware call sites == documented class B / S3 (deferred) set
    allowed = {
        "pc/src/pc_net_game.c": 1,                       # --diag-role-link-state print (definition excluded)
        "src/actor/ac_set_manager.c": 2,                 # wildlife (already also role-gated)
        "src/actor/ac_set_npc_manager.c": 1,             # class B
        "src/actor/ac_weather.c": 1,                     # S3
        "src/actor/npc/ac_npc_move.c_inc": 4,            # class B (talk lease / movement consumer / log)
        "src/actor/npc/ac_npc_think.c_inc": 1,           # S3 (NPC pitfall)
        "src/actor/npc/event/ac_ev_ghost_talk.c_inc": 1, # WEEDS: Wisp clear_grass is not set on a READY client (host growth consumes it)
        "src/game/m_event_map_npc.c": 2,                 # S3
        "src/game/m_field_info.c": 1,                    # S3 (daily hole cleanup)
        "src/game/m_field_make.c": 1,                    # S3
        "src/game/m_kabu_manager.c": 1,                  # S3
        "src/game/m_kankyo_weather.c_inc": 1,            # S3
        "src/game/m_npc.c": 5,                           # S3
        "src/game/m_npc_walk.c": 2,                      # S3
        "src/game/m_player_main_notice_net.c_inc": 1,    # wildlife notice (already role-denied elsewhere)
        "src/game/m_player_main_notice_rod.c_inc": 1,    # wildlife notice (already role-denied elsewhere)
    }
    found = {}
    for top in ("src", "pc/src", "pc/include", "include"):
        for dp, _dn, fns in os.walk(os.path.join(ROOT, top)):
            for fn in fns:
                if not fn.endswith((".c", ".h", ".c_inc", ".cpp", ".inc")):
                    continue
                rel = os.path.relpath(os.path.join(dp, fn), ROOT).replace("\\", "/")
                n = 0
                for line in strip_comments(read(rel)).split("\n"):
                    s = line.strip()
                    if READYAWARE not in s or s.startswith("int pc_net_game_world"):
                        continue
                    n += 1
                if n:
                    found[rel] = n
    check("remaining world_is_host_authoritative() code sites are exactly the class B / S3 set (%s)" %
          ", ".join("%s=%d" % kv for kv in sorted(found.items())), found == allowed, results)

    # ---- protocol / wire unchanged
    hdr = read("pc/include/pc_net_game.h")
    check("protocol is v%d (wire_baseline.EXPECTED_PROTOCOL_VERSION)" % wire_baseline.EXPECTED_PROTOCOL_VERSION,
          wire_baseline.header_protocol_ok(hdr), results)
    # wire contract vs HEAD (content-based, see wire_baseline.py): the files may legitimately change, the WIRE may not
    wire_baseline.run(lambda d, c: check(d, c, results), ROOT)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
