#!/usr/bin/env python3
"""test_shop_renewal_reload_src.py - SOURCE test: a host that stays outdoors (dedicated observer / idle listen host) finishes a due shop renewal.

Vanilla renews the shop only in Shop_Level_Actor_ct -> aSL_RenewShop (an outdoor scene LOAD) while mSP_InRenewal() closes the shop from renewal_time - 1 day.
pc_m_card.c pc_host_renewshop_poll() does ONE same-position SCENE_FG reload per renewal_time when the exact aSL_RenewShop condition holds.
Tier: SOURCE-MODEL TESTED. Usage: python test_shop_renewal_reload_src.py
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def rd(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8", errors="replace") as f:
        return f.read()


def main():
    res = []

    def ck(desc, cond):
        res.append(bool(cond))
        print(("PASS " if cond else "FAIL ") + desc)

    lvl = rd("src", "actor", "ac_shop_level.c")
    shop = rd("src", "game", "m_shop.c")
    mc = rd("pc", "src", "pc_m_card.c")
    ck("R0 premise: aSL_RenewShop( is called exactly once, from Shop_Level_Actor_ct (an actor ct = a scene load)",
       len(re.findall(r"\baSL_RenewShop\(actorx, game\)", lvl)) == 1
       and "aSL_RenewShop(actorx, game);" in re.search(r"static void Shop_Level_Actor_ct\([^;{]*\) \{.*?\n\}\n", lvl, re.S).group(0))
    ck("R0 premise: mSP_ShopOpen returns RENEW while mSP_InRenewal (from renewal_time - 1 day, no end until the event is cleared)",
       "if (mSP_InRenewal() != FALSE) {\n        return mSP_SHOP_STATUS_RENEW;" in shop and "lbRTC_Sub_DD(&renew_time, 1);" in shop)
    ck("R0 premise: Shop_Level is a control actor of the town scene", "mAc_PROFILE_SHOP_LEVEL" in rd("src", "data", "scene", "test_fd_npc_land.c"))
    m = re.search(r"static void pc_host_renewshop_poll\(void\) \{(.*?)\n\}\n", mc, re.S)
    ck("R1 pc_host_renewshop_poll exists", m is not None)
    if m is None:
        return 1
    b = m.group(1)
    prod = re.sub(r"#ifdef PC_NET_TEST_HOOKS.*?#endif", "", b, flags=re.S)
    ck("R1 HOST role only (single-player / clients untouched)", "pc_net_game_role() != PC_NETGAME_ROLE_HOST" in prod.split("play = ")[0])
    ck("R2 the exact aSL_RenewShop condition: FG field type, SCENE_FG, mEv_SAVED_RENEWSHOP, now >= renewal_time or the same Y/M/D",
       "mFI_GET_TYPE(mFI_GetFieldId()) != mFI_FIELD_FG" in prod and "Save_Get(scene_no) != SCENE_FG" in prod and "mEv_CheckEvent(mEv_SAVED_RENEWSHOP) != TRUE" in prod
       and "lbRTC_IsOverTime(&renew_time, &now_time) == lbRTC_OVER" in prod and "lbRTC_CHECK_YEARS | lbRTC_CHECK_MONTHS | lbRTC_CHECK_DAYS" in prod)
    ck("R3 idempotent: one reload per renewal_time value (key recorded only on a started reload)",
       "lbRTC_IsEqualTime(&l_key, &renew_time, lbRTC_CHECK_ALL)" in prod and re.search(r"if \(res == 1\) \{\n\s*l_key = renew_time;\n\s*l_have_key = 1;", prod))
    ck("R4 no reload when it cannot help (same level / no shop block) or a player is inside a shop building / the host player is busy",
       "mSP_GetShopLevel() == mSP_GetRealShopLevel()" in prod and "mRF_BLOCKKIND_SHOP" in prod and "pc_host_renewshop_scene_is_shop(" in prod
       and "mPlib_able_submenu_type1" in prod and "fb_wipe_mode != WIPE_MODE_NONE" in prod)
    ck("R5 production path never writes shop state / the event / calls the FG rewrite: only goto_other_scene",
       not re.search(r"Save_Get\(shop\)\.[a-z_.]+\s*=|mEv_EventON|mEv_EventOFF|mSP_PlusSales|aSL_RewriteShopFg|mSP_RenewShopLevel", prod)
       and prod.count("goto_other_scene(") == 1)
    ck("R6 the test hook (renewal due at start) is compiled only with PC_NET_TEST_HOOKS",
       "AC_TEST_RENEWSHOP_DUE" in b and "AC_TEST_RENEWSHOP_DUE" not in prod)
    po = re.search(r"void pc_host_observer_poll\(void\) \{(.*?)\n\}\n", mc, re.S).group(1)
    ck("R7 polled every frame from pc_host_observer_poll BEFORE its observer-flag early return (also covers a listen host)",
       "pc_host_renewshop_poll();" in po and po.index("pc_host_renewshop_poll();") < po.index("if (!g_pc_host_observer"))
    print("\n%d/%d passed" % (sum(res), len(res)))
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
