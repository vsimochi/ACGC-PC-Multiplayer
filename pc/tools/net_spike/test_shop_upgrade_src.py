#!/usr/bin/env python3
"""test_shop_upgrade_src.py - SOURCE test of the announced 5-minute shop upgrade (multiplayer; pc_shop_upgrade.h).

Pins the seams in the decomp code and the guarantees that cannot be seen from the wire: single player / the switch off keep the vanilla code paths; the closure closes the
shop for everybody (status RENEW before the first-job and sale-event overrides, mSP_InRenewal, the doors, the shop interior shows everybody out); the vanilla 2-day scheduling is
skipped only when a host replaces it; the renewal is the vanilla body; Nook's announcement is a PC wait type of Tom Nook only (never Timmy / Tommy), never during the local
player's first-job chores, never instead of the HRA talk; the TEST-ONLY knobs exist only behind the test-hook gate.
Tier: SOURCE. Usage: python test_shop_upgrade_src.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402


def rd(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def body(text, head_rx):
    m = re.search(head_rx, text)
    if not m:
        return ""
    i = text.index("{", m.start())
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return ""


def main():
    res = []
    ck = lambda d, c: L.check(d, bool(c), res)  # noqa: E731
    shop = rd("src", "game", "m_shop.c")
    lvl = rd("src", "actor", "ac_shop_level.c")
    si = rd("src", "actor", "ac_shop_indoor.c")
    nsc = rd("src", "actor", "npc", "ac_npc_shop_common.c")
    ng = rd("pc", "src", "pc_net_game.c")
    mc = rd("pc", "src", "pc_m_card.c")
    hdr = rd("pc", "include", "pc_shop_upgrade.h")
    nh = rd("pc", "src", "pc_nook_house.c")
    ded = rd("pc", "src", "pc_dedicated.c")
    blk = ng[ng.index("===== SHOP UPGRADE BEGIN"):ng.index("===== SHOP UPGRADE END")]

    so = body(shop, r"extern int mSP_ShopOpen\(\) \{")
    ck("A1 mSP_ShopOpen: RENEW while the upgrade closure runs, under TARGET_PC, BEFORE the first-job override and the sale-event branch",
       "#ifdef TARGET_PC\n    /* Multiplayer shop upgrade" in so and so.index("if (pc_shop_upgrade_closed()) {\n        return mSP_SHOP_STATUS_RENEW;") < so.index("if (mEv_CheckFirstJob())")
       < so.index("mEv_CheckEvent(mEv_SPNPC_SHOP)"))
    ir = body(shop, r"extern int mSP_InRenewal\(\) \{")
    ck("A2 mSP_InRenewal: TRUE while closed (host state / client mirror), then the unchanged client FALSE and the vanilla event test",
       ir.index("if (pc_shop_upgrade_closed()) {\n        return TRUE;") < ir.index("pc_net_game_role() == PC_NETGAME_ROLE_CLIENT") < ir.index("mEv_CheckEvent(mEv_SAVED_RENEWSHOP)"))
    ck("A3 single player / a client of an old host is inert: pc_shop_upgrade_closed() is 0 outside HOST / CLIENT, and for a client without the host's service 6 (c_have)",
       re.search(r"int pc_shop_upgrade_closed\(void\) \{\n    if \(s_role == PC_NETGAME_ROLE_HOST\) \{\n        return pcnetgame_shopup_enabled\(\) && .*?\n    \}\n    if \(s_role == PC_NETGAME_ROLE_CLIENT\) \{\n        return s_up.c_have && s_up.c_enabled && .*?\n    \}\n    return 0;\n\}", blk, re.S) is not None)
    jr = body(lvl, r"static void aSL_JudgeRenewShop\(ACTOR\* actorx, GAME\* game\) \{")
    ck("B1 aSL_JudgeRenewShop: the vanilla 2-day scheduling is skipped only when a host replaces it; the flyer calls at the end still run",
       "&& !pc_shop_upgrade_host_replaces_vanilla()" in jr and jr.rstrip("}").rstrip().endswith("aSL_SetRenewalChiraswhi_Notice();\n    mSP_SetRenewalChiraswhi_AppoDay();"))
    ck("B2 a host replaces the vanilla schedule only with role HOST + the switch (AC_MP_SHOP_UPGRADE, default ON, '0' / 'off' / 'false' = vanilla)",
       "return s_role == PC_NETGAME_ROLE_HOST && pcnetgame_shopup_enabled();" in blk and 'getenv("AC_MP_SHOP_UPGRADE")' in blk
       and 'strcmp(e, "0") == 0 || pcnetgame_shopup_ieq(e, "off") || pcnetgame_shopup_ieq(e, "false")' in blk)
    rd_ = body(lvl, r"int aSL_PC_RenewShopDirect\(void\) \{")
    ck("B3 the direct renewal is the vanilla body (structure tile SHOP0+level, aSL_GroundClean, mSP_RenewShopLevel, event off, new exchange day + lineup) and never touches a live DUMMY tile",
       all(x in rd_ for x in ("fg_p[i] = SHOP0 + next_level;", "aSL_GroundClean((s16)level, (s16)next_level, fg_p, bx, bz,", "mSP_RenewShopLevel()",
                              "mEv_EventOFF(mEv_SAVED_RENEWSHOP);", "mSP_NewExchangeDay();", "mSP_ExchangeLineUp_ZeldaMalloc();"))
       and "if (fg_p[i] >= SHOP0 && fg_p[i] <= SHOP3) {" in rd_)
    rt = body(blk, r"static void pcnetgame_shopup_renew_tick\(uint32_t now\) \{")
    ck("B4 the renewal path: town scene -> the vanilla due state for pc_host_renewshop_poll (the actor ct renews); shop scene -> wait; elsewhere -> the direct body",
       rt.index("scene == SCENE_FG && pcfa_scene_is_town()") < rt.index("mEv_EventON(mEv_SAVED_RENEWSHOP);") < rt.index("pcnetgame_shopup_scene_is_shop(scene)")
       < rt.index("aSL_PC_RenewShopDirect()"))
    po = body(mc, r"static void pc_host_renewshop_poll\(void\) \{")
    ck("B5 pc_host_renewshop_poll performs a due vanilla renewal only when the announced upgrade allows it (switch off, or its own RENEWING)",
       po.index("if (!pc_shop_upgrade_vanilla_renewal_allowed()) {") < po.index("renew_time = Save_Get(shop).renewal_time;")
       and "return !pc_shop_upgrade_host_replaces_vanilla() || s_up.state == PC_SHOP_UPGRADE_RENEWING;" in blk)
    ck("B6 a vanilla schedule found while the switch is on is taken over (event + flags cleared, then PENDING), never silently performed",
       "replaced by the announced upgrade" in blk and "mEv_EventOFF(mEv_SAVED_RENEWSHOP);\n        Save_Get(shop).shop_info.upgrading_today = FALSE;" in blk)
    ej = body(si, r"static void aSI_pc_restock_eject\(GAME\* game\) \{")
    ck("C1 the shop interior shows everybody out for the upgrade too (same latch, same busy rules: never mid-talk)",
       "const int restocking = pc_net_game_shop_restocking() || pc_shop_upgrade_closed();" in ej and "closed for the upgrade" in ej)
    doors = [rd("src", "actor", "ac_%s_move.c_inc" % n) for n in ("shop", "conveni", "super", "depart")]
    ck("C2 the four shop doors explain the closure (the upgrade door row) only when the multiplayer closure runs (single player keeps MSG_SHOP_RENEW)",
       all("if (pc_shop_upgrade_closed()) {\n            msg_no = pc_nook_upgrade_door_msg();" in d and "case mSP_SHOP_STATUS_RENEW: // 3\n                msg_no = MSG_SHOP_RENEW;" in d for d in doors))
    st = nsc[nsc.index("static void aNSC_start_wait(NPC_SHOP_COMMON_ACTOR* shop_common, GAME_PLAY* play) {\n    ACTOR* actorx = (ACTOR*)shop_common;\n    int wait_type"):]
    st = st[:st.index("\n}\n")]
    ck("D1 Nook's start: the upgrade wait type replaces the greeting (not the HRA talk) only when the announcement is due and the player is not in its first-job chores, inside the "
       "Tom Nook-only block (aNSC_PC_HOUSE: never Timmy / Tommy)",
       "if (wait_type != aNSC_WAIT_TYPE_HRATALK && aNSC_pc_up_due()) {" in st and "return pc_shop_upgrade_announce_due() && !mEv_CheckFirstJob();" in nsc
       and st.index("#ifdef aNSC_PC_HOUSE") < st.index("aNSC_pc_up_due()") and "aNSC_pc_hs_enter(shop_common, aNSC_PC_UP_ASK);" in st
       and "proc = aNSC_pc_set_talk_info_upgrade;" in st)
    hp = body(nsc, r"static void aNSC_pc_house_proc\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{")
    ck("D2 the acknowledgement starts the closure (host: at once = 3, client: request = 1 then poll); the closing row on success, the fallback row otherwise",
       "case aNSC_PC_UP_BEGIN:" in hp and "r = pc_shop_upgrade_begin();" in hp and "r = pc_shop_upgrade_poll();" in hp
       and "PC_NOOK_MSG_UPGRADE_CLOSING" in nsc and "PC_NOOK_MSG_UPGRADE_LATER" in nsc)
    ck("D3 the generated rows exist (announcement with 2 acknowledgement choices, closing, fallback, door) and use the closure length / minutes left",
       all("case PC_NOOK_MSG_UPGRADE_%s:" % x in nh for x in ("ANNOUNCE", "CLOSING", "LATER", "DOOR")) and "pc_shop_upgrade_minutes_total()" in nh
       and "pc_shop_upgrade_minutes_left()" in nh and "I've been thinking about" in nh and "help everyone out here" in nh)
    ck("E1 TEST-ONLY knobs only through pc_test_hook_getenv / PC_NET_TEST_HOOKS (AC_TEST_SHOP_UPGRADE_SECONDS / _QUALIFY / _HOST_START / _CLIENT_START)",
       'pc_test_hook_getenv("AC_TEST_SHOP_UPGRADE_SECONDS")' in blk and 'pc_test_hook_getenv("AC_TEST_SHOP_UPGRADE_QUALIFY")' in blk
       and 'pc_test_hook_getenv("AC_TEST_SHOP_UPGRADE_HOST_START")' in blk and 'pc_test_hook_getenv("AC_TEST_SHOP_UPGRADE_CLIENT_START")' in blk
       and blk.count("#ifdef PC_NET_TEST_HOOKS") == 2 and "#define PC_SHOP_UPGRADE_SECONDS 300" in hdr)
    ck("E2 the dedicated console: `shop-upgrade` reports the state, `shop-level-boost` explains the announced upgrade",
       'strcmp(cmd, "shop-upgrade") == 0' in ded and "pc_shop_upgrade_describe(" in ded and "Nook then announces the upgrade (5-minute closure)" in ded)
    fw = body(blk, r"static void pcnetgame_shopup_file_write\(void\) \{")
    ck("F1 the restart file: parent dirs created, written to a .tmp, flushed + committed, then atomically renamed over the old one",
       fw.index("_mkdir(tmp);") < fw.index('fopen(tmp, "wb")') < fw.index("fflush(f);") < fw.index("_commit(_fileno(f))") < fw.index("rename(tmp, path)"))
    print("\n%d/%d passed" % (sum(res), len(res)))
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
