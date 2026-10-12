#!/usr/bin/env python3
"""test_map_grant_src.py - the town map (Common map_flag) is granted after a player's first applied SHOP_BUY. SOURCE only, no game process."""
import os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
rd = lambda *p: open(os.path.join(REPO, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    ng = rd("pc", "src", "pc_net_game.c")
    h = re.search(r"static int pcnetgame_shop_map_grant_idx\(int idx\) \{(.*?)\n\}\n", ng, re.S).group(1)
    ck("M1 host helper is gated by the sanctioned idx check (host own resident never written) and is idempotent via maps[]",
       "pcnetgame_rec_txn_idx_ok(idx)" in h and "mPr_GetThisLandMapIdx(r->maps, mPr_FOREIGN_MAP_COUNT) != -1" in h and "mPr_SetNewMap(r->maps" in h)
    buy = re.search(r"\} else if \(is_buy\) \{\n        mSP_PlusSales\(shop_price\).*?\} else if \(is_sell\)", ng, re.S).group(0)
    ck("M2 the host grant runs only in the SHOP_BUY commit branch (never sell / restock / replay)", "pcnetgame_shop_map_grant_idx(idx)" in buy and ng.count("pcnetgame_shop_map_grant_idx(idx)") == 1)
    ap = re.search(r"static int pcnetgame_txn_apply_shop\(.*?\n\}\n", ng, re.S).group(0)
    ck("M3 the client does the identical grant on APPLIED buy and sets map_flag", "buy && mPr_GetThisLandMapIdx(np->maps" in ap and "Common_Set(map_flag, TRUE)" in ap)
    ck("M4 client derives map_flag once per synced session from the record's maps[]", "s_crec_map_flag_done" in ng and "Common_Set(map_flag, TRUE)" in ng)
    ck("M5 maps[] is inside the client-owned record range (0x10F8..0x2440), so the cdig stays equal", "{ 0x10F8u, 0x1348u, PC_NETGAME_REC_OWN_CLIENT }" in ng and "offsetof(Private_c, maps) == 0x11DC" in ng)
    ck("M6 no mPr_FLAG_1/8 use and no new persistence file", "mPr_FLAG_8" not in h and "fopen" not in h)
    nsc = rd("src", "actor", "npc", "ac_npc_shop_common.c")
    si = re.search(r"static void aNSC_sell_item_init\(.*?\n\}\n", nsc, re.S).group(0)
    fn = re.search(r"static int aNSC_pc_host_first_purchase_map_grant_impl\(void\) \{(.*?)\n\}\n", nsc, re.S).group(1)
    mm = rd("src", "actor", "npc", "ac_npc_shop_master_move.c_inc")
    ck("M7 the networked HOST's own purchase grants the map via the shared function from aNSC_sell_item_init (non-client branch only; solo untouched)",
       "aNSC_pc_host_first_purchase_map_grant_impl()" in si and si.index("aNSC_pc_host_first_purchase_map_grant_impl") > si.index("if (!aNSC_PC_IS_CLIENT())")
       and "pc_net_game_role() == PC_NETGAME_ROLE_HOST" in fn and "mPr_GetThisLandMapIdx(Now_Private->maps, mPr_FOREIGN_MAP_COUNT) == -1" in fn
       and "mPr_SetNewMap(Now_Private->maps, mPr_FOREIGN_MAP_COUNT)" in fn and "Common_Set(map_flag, TRUE)" in fn and nsc.count("aNSC_pc_host_first_purchase_map_grant_impl()") == 2)
    ck("M7b the file is #included by 5 actors: the impl is static and the exported test-hook wrapper is defined by exactly ONE includer (no duplicate symbols)",
       "int aNSC_pc_host_first_purchase_map_grant(void) {" in nsc and "#ifdef aNSC_PC_EXPORT_MAP_GRANT" in nsc and "#define aNSC_PC_EXPORT_MAP_GRANT" in mm
       and sum(1 for f in ("conv_master", "depart_master", "mamedanuki", "shop_master", "super_master")
               if "#define aNSC_PC_EXPORT_MAP_GRANT" in rd("src", "actor", "npc", "ac_npc_%s_move.c_inc" % f)) == 1)
    hk = re.search(r"static void pcnetgame_host_map_grant_test_hook\(void\) \{(.*?)\n\}\n", ng, re.S).group(1)
    ck("M8 host test hook: env-gated (AC_TEST_HOST_MAP_GRANT via pc_test_hook_getenv), once, calls the SAME shared function twice, called from the host tick",
       "pc_test_hook_getenv(\"AC_TEST_HOST_MAP_GRANT\")" in hk and "done = 1" in hk and hk.count("aNSC_pc_host_first_purchase_map_grant()") == 2
       and re.search(r"pcnetgame_museum_bits_test_hook\(\);\n    pcnetgame_host_map_grant_test_hook\(\);", ng) is not None)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
