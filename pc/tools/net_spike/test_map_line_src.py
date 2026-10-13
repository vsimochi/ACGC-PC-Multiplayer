#!/usr/bin/env python3
"""test_map_line_src.py - Nook's first-purchase "here is a map" line: the one-shot flag + chain wiring. SOURCE-MODEL only, no game process.
The extra page is appended to the vanilla thanks message by pc_nook_msg_patch (the existing loader hook), armed only by a purchase that granted the map."""
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
    nsc = rd("src", "actor", "npc", "ac_npc_shop_common.c")
    nk = rd("pc", "src", "pc_nook_house.c")
    ld = rd("src", "game", "m_msg_main.c_inc")

    ck("L1 the loader already calls pc_nook_msg_patch for every vanilla message (the only hook used; no state-machine change)", "(void)pc_nook_msg_patch(index, msg_data->text_buf.data" in ld)
    p = re.search(r"int pc_nook_msg_patch\(.*?\) \{(.*?)\n    if \(index != s_first_page_id\)", nk, re.S).group(1)
    ck("L2 patch: armed index only, consumed on first load, appends only to a message that ends with MSGEND", "index == s_map_arm_idx" in p and "s_map_arm_idx = -1" in p and "nk_map_line_patch(" in p)
    f = re.search(r"static int nk_map_line_patch\(.*?\n\}\n", nk, re.S).group(0)
    ck("L3 appender requires 7F 00 at the end, bounds-checks cap, writes only after building", "data[len - 2] != NK_CC || data[len - 1] != NK_END" in f and "len - 2 + b.n > cap" in f and f.index("memcpy") > f.index("len - 2 + b.n > cap"))
    ck("L4 selftest covers END / CONTINUE / foreign index / no-room cases", "pc_nook_map_line_arm(0x2347)" in nk and "pc_nook_map_line_arm(0x2346)" in nk)
    ck("L5 flag: client sets it only from pc_net_game_take_map_granted() on APPLIED, host only inside the grant impl",
       nsc.count("aNSC_pc_map_just_granted = 1") == 2 and "if (pc_net_game_take_map_granted())" in nsc)
    si = re.search(r"static void aNSC_sell_item_init\(.*?\n\}\n", nsc, re.S).group(0)
    ck("L6 host chain: arms in aNSC_sell_item_init's non-client branch AFTER the grant, with the message the answer0 just selected (shop_common->msg_no)",
       si.index("_impl()") < si.index("aNSC_pc_map_chain_msg(shop_common->msg_no)") < si.index("} /* else:"))
    ck("L7 client chain: after the success Set_continue in the in-flight branch; stale arm cleared before it",
       re.search(r"pc_nook_map_line_clear\(\);[^\n]*\n\s+aNSC_Set_continue_msg_num\(mMsg_Get_base_window_p\(\), shop_common, aNSC_get_msg_no\(msg_no\[pc_next\]\)\);\n\s+aNSC_pc_map_chain_msg\(shop_common->msg_no\);", nsc) is not None)
    ck("L8 the host/answer0 tail clears a stale arm (a non-grant purchase can never get the line)", re.search(r"pc_nook_map_line_clear\(\);[^\n]*\n#endif\n\s+aNSC_Set_continue_msg_num\(msg_p", nsc) is not None)
    chain = re.search(r"static void aNSC_pc_map_chain_msg\(int msg_index\) \{(.*?)\n\}\n", nsc, re.S).group(1)
    ck("L9 chain is a no-op unless the flag is set, and consumes it (exactly once)", chain.strip().startswith("if (aNSC_pc_map_just_granted) {") and "aNSC_pc_map_just_granted = 0;" in chain)
    # the map line itself adds no generated row; the count grew to 35 later for dresser storage (PC_NOOK_MSG_DRESSER_REFUSED), not for the map line
    ck("L10 the map line adds no new action / message id / enum entry (PC_NOOK_MSG_COUNT 45 = 34 + the dresser refusal row + the 4 shop upgrade rows + the 2 guest post rows + the 4 house upgrade rows, all appended after it; the map line itself added none)",
       "#define PC_NOOK_MSG_COUNT 45" in rd("pc", "include", "pc_nook_house.h") and "PC_NOOK_MSG_DRESSER_REFUSED, /*" in rd("pc", "include", "pc_nook_house.h")
       and "PC_NOOK_MSG_UPGRADE_DOOR,     /*" in rd("pc", "include", "pc_nook_house.h") and "PC_NOOK_MSG_UPG_BUSY          /*" in rd("pc", "include", "pc_nook_house.h"))
    ck("L11 client flag describes the latest applied shop txn only (reset at the start of apply, set only inside the grant branch)",
       "s_map_granted_oneshot = 0; /* the map-line flag always describes" in ng and ng.count("s_map_granted_oneshot = 1;") == 1)
    ck("L12 exported test-hook state lives only in the single export includer", nsc.count("int aNSC_pc_map_oneshot_state(int* total)") == 1 and nsc.index("int aNSC_pc_map_oneshot_state") > nsc.index("#ifdef aNSC_PC_EXPORT_MAP_GRANT"))
    ck("L13 host hook reports the flag after the first and the second grant call", "host map-line hook: flag_after_first=%d" in ng)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
