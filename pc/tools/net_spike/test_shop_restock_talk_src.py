#!/usr/bin/env python3
"""test_shop_restock_talk_src.py - restock must not cut a Nook conversation, and the 500 Bell fee counts once towards shop progression. SOURCE only, no game process."""
import os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
rd = lambda *p: open(os.path.join(REPO, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    si = rd("src", "actor", "ac_shop_indoor.c")
    ng = rd("pc", "src", "pc_net_game.c")
    f = re.search(r"static void aSI_pc_restock_eject\(GAME\* game\) \{(.*?)\n\}\n", si, re.S).group(1)
    ck("T1 the eject waits while a message window is visible", "mMsg_Check_MainHide(mMsg_Get_base_window_p()) == FALSE" in f)
    ck("T1 the eject still waits for demo / submenu-able", "mDemo_CheckDemo() != FALSE" in f and "mPlib_able_submenu_type1(game) == FALSE" in f)
    m = re.search(r"#define aSI_PC_EJECT_HARD_CAP (\d+)", si)
    ck("T1 the stuck-state hard cap is far beyond the 60 s restock (>= 300000 ms, WALL-CLOCK)", m and int(m.group(1)) >= 300000)
    ck("T1 frame-rate independence: the grace (500 ms) and the cap are wall-clock (pc_net_game_now_ms), no per-frame counter decides the eject",
       "#define aSI_PC_EJECT_GRACE_MS 500" in si and "aSI_pc_eject_wait++" not in f and "aSI_pc_eject_wait = (int)(s32)(now - aSI_pc_eject_t0);" in f and
       "if (aSI_pc_eject_wait < aSI_PC_EJECT_GRACE_MS) {" in f and "aSI_pc_eject_wait < 30" not in f)
    ck("T1 a busy player is forced ONLY while still restocking AND past the hard cap", "if (busy && (!restocking || aSI_pc_eject_wait < aSI_PC_EJECT_HARD_CAP)) {" in f
       # the held branch may carry a log-only diagnostic (printf, no state write) but must still end in `return;` before any exit / latch clear
       and re.search(r"if \(busy && \(!restocking \|\| aSI_pc_eject_wait < aSI_PC_EJECT_HARD_CAP\)\) \{(?:(?!goto_other_scene|aSI_pc_eject_pending = 0)[^}]|\{[^}]*\})*?\n        return;\n    \}", f) is not None)
    ck("T1 a restock seen inside latches the exit (talk outlasting the restock is shown out after the talk)", "if (restocking) {\n        aSI_pc_eject_pending = 1;" in f and "if (!aSI_pc_eject_pending) {" in f)
    ck("T1 the latch clears only after the scene change was accepted", re.search(r"if \(goto_other_scene\(play, Common_GetPointer\(structure_exit_door_data\), FALSE\)\) \{[^}]*aSI_pc_eject_pending = 0;", f) is not None)
    ct = re.search(r"static void Shop_Indoor_Actor_ct\(ACTOR\* actorx, GAME\* game\) \{(.*?)\n\}\n", si, re.S).group(1)
    ck("T1 a new shop room resets the latch + wait", "aSI_pc_restock_eject_reset();" in ct and "#ifdef TARGET_PC" in ct)
    ck("T1 the whole thing is TARGET_PC only", "#ifdef TARGET_PC\n/* Nook's manual restock" in si)
    ck("T1 single player inert: pc_net_game_shop_restocking() is 0 outside HOST/CLIENT roles",
       re.search(r"int pc_net_game_shop_restocking\(void\) \{\n    if \(s_role == PC_NETGAME_ROLE_HOST\) \{\n        return s_restock.active;\n    \}.*?return 0;\n\}", ng, re.S) is not None)
    ck("T1 m_msg.h is included", '#include "m_msg.h"' in si)
    n = len(re.findall(r"mSP_PlusSales\(PC_NETGAME_RESTOCK_PRICE\)", ng))
    ck("T2 exactly two fee sites (peer commit + host-local)", n == 2)
    peer = re.search(r"\} else if \(is_restock\) \{\n        if \(!pcnetgame_restock_start\(\"a peer's payment\"\)\).*?\n    \} else if \(is_buy\) \{", ng, re.S).group(0)
    ck("T2 peer fee counted after a successful restock start, inside the commit branch (after the replay/journal check)", "mSP_PlusSales(PC_NETGAME_RESTOCK_PRICE)" in peer and peer.index("return;") < peer.index("mSP_PlusSales"))
    loc = re.search(r"int pc_net_game_restock_begin\(void\) \{.*?return 3;", ng, re.S).group(0)
    ck("T2 host-local fee counted after pcnetgame_restock_start succeeded", loc.index("pcnetgame_restock_start") < loc.index("mSP_PlusSales(PC_NETGAME_RESTOCK_PRICE)"))
    ck("T2 work mode never calls mSP_PlusSales", "mSP_PlusSales" not in re.search(r"pcnetgame_work_commit\(work.*?\n    \} else if \(is_restock\)", ng, re.S).group(0))
    # T3: the dedicated-server `shop-level-boost` admin command (progression aid, never an immediate upgrade)
    ded = rd("pc", "src", "pc_dedicated.c")
    b = re.search(r"static void pc_ded_cmd_shop_level_boost\(char\* args\) \{(.*?)\n\}\n", ded, re.S)
    ck("T3 command exists and is dispatched", b is not None and 'strcmp(cmd, "shop-level-boost") == 0' in ded and "shop-level-boost [confirm]" in ded)
    body = b.group(1) if b else ""
    ck("T3 exactly one mSP_PlusSales call, after the confirm gate", body.count("mSP_PlusSales(") == 1 and body.index('"confirm"') < body.index("mSP_PlusSales(target - sum)"))
    ck("T3 never writes shop_level / renewal_time / the renewal event", not re.search(r"shop_level\s*=|renewal_time\s*=|mEv_EventON|mEv_EventOFF|Save_Get\(shop\)", body))
    ck("T3 refuses: not ready / max tier / restocking / renewal already scheduled / threshold reached",
       all(s in body for s in ("pc_net_game_dedicated_world_ready()", "level >= mSP_SHOP_TYPE_DSUPER", "pc_net_game_shop_restocking()", "mEv_CheckEvent(mEv_SAVED_RENEWSHOP)", "<= sum")))
    ck("T3 prints the 2-day caveat and the visitor caveat", "2 days" in body and "visitor_flag" in body)
    lvl = rd("src", "actor", "ac_shop_level.c")
    fg = rd("src", "data", "scene", "test_fd_npc_land.c")
    play = rd("src", "game", "m_play.c")
    ck("T3 the FG scene (scene_word_data[7] = test_fd_npc_land, where the observer stands) spawns the Shop_Level actor",
       "mAc_PROFILE_SHOP_LEVEL" in fg and re.search(r"npc_room01_info,\s*test_fd_npc_land_info,", play) is not None)
    # the announced shop upgrade (deliberate change): a host with AC_MP_SHOP_UPGRADE on (default) never schedules the vanilla 2-day renewal; with the switch off (and in single
    # player) the vanilla scheduling block is unchanged
    ck("T3 the host (not a client) schedules the renewal: aSL_JudgeRenewShop returns only for CLIENT; the vanilla scheduling is skipped only when the host replaces it with the announced upgrade",
       "if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {\n        return;\n    }\n#endif\n    if (Save_Get(scene_no) == SCENE_FG && mEv_CheckEvent(mEv_SAVED_RENEWSHOP) == FALSE\n#ifdef TARGET_PC\n" in lvl
       and "        && !pc_shop_upgrade_host_replaces_vanilla()\n#endif\n    ) {\n        if (mSP_GetShopLevel() < mSP_GetRealShopLevel()) {" in lvl)
    buy = re.search(r"\} else if \(is_buy\) \{\n        mSP_PlusSales\(shop_price\);.*?\n    \} else if \(is_sell\) \{", ng, re.S)
    bb = buy.group(0) if buy else ""
    ck("T4 a guest's APPLIED purchase records the foreign shopper on the host (idempotent, guest records only)",
       "if (idx >= PLAYER_NUM && mSP_PC_SetVisitorRemote()) {" in bb and "pcnetgame_shop_map_grant_idx(idx)" in bb
       and re.search(r"int mSP_PC_SetVisitorRemote\(void\) \{\n    if \(Save_Get\(shop\).visitor_flag != FALSE\) \{\n        return 0;", rd("src", "game", "m_shop.c")) is not None)
    ck("T4 guests are always foreign (identity refuses a guest whose home land is this town)", "the guest's home land is this town's land" in ng)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
