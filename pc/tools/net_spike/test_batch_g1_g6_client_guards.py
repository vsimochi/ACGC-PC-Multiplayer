#!/usr/bin/env python3
"""test_batch_g1_g6_client_guards.py - Batch G1-G6 (v7 client-only safe degrade) source-audit checks.
NO game process is launched; runtime behavior is NOT TESTED.

  G1  pc_m_card.c: Resetti reset-code arming + boot persist skipped for a CLIENT (cond-1 write).
  G2  ac_npc_shop_common.c: catalog order refused for a client before any bell/order write.
  G3  ac_npc_shop_common.c: house loan/upgrade/statue offer and paint purchase refused for a client.
  G4  m_post_office.c: mPO_first_work returns early for a client.
  G5  m_field_info.c mFI_SetShell, m_mushroom.c mMsr_SetMushroom/mMsr_FirstClearMushroom skipped for a client.
  G6  m_fuusen.c: Balloon_move returns early for a client.
Host/solo fall through unchanged; protocol is v8 (wire_baseline.EXPECTED_PROTOCOL_VERSION); no wire file touched by this batch.
Usage: python3 test_batch_g1_g6_client_guards.py
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


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read().replace("\r\n", "\n")


def func_body(src, header_re):
    m = re.search(header_re, src)
    if not m:
        return ""
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


def gated_first(body, mutators, results, tag, token=CLIENT, need_ifdef=True):
    g = body.find(token)
    check("%s: guard present" % tag, g >= 0, results)
    if need_ifdef:
        check("%s: guard is under #ifdef TARGET_PC" % tag, g >= 0 and "#ifdef TARGET_PC" in body[:g], results)
    for m in mutators:
        pos = body.find(m)
        check("%s: guard precedes %s" % (tag, m), g >= 0 and pos >= 0 and g < pos, results)


def main():
    results = []

    # ---- G1 Resetti
    card = read("pc/src/pc_m_card.c")
    b = func_body(card, r"\nint mCD_InitGameStart_bg\(int player_no, int card_private_idx, int start_cond, s32\* mounted_chan\) \{")
    check("G1: function found", b != "", results)
    g = b.find(NOT_CLIENT)
    arm = b.find("pc_set_reset_code(Now_Private);")
    wr = b.find("if (!pc_save_write_gci()) {\n                        OSReport(\"[PC] InitGameStart: reset-code persist failed")
    detect = b.find("Now_Private->reset_count++;")
    check("G1: role != CLIENT guard exists", g >= 0, results)
    check("G1: guard precedes the arming call and the boot persist write", 0 <= g < arm < wr, results)
    check("G1: reset detection (reset_count++/reset_flag) stays BEFORE the guard (in-memory accounting kept)",
          0 <= detect < g, results)
    check("G1: guard closes after the persist write, before the foreigner branch",
          0 <= wr < b.find("Batch G1: role != CLIENT") < b.find("Handle foreigner start conditions"), results)
    check("G1: the OUTGOING_FOREIGNER (train) return-home write is untouched",
          "return-home persist failed" in b, results)

    # ---- G2 / G3 shop common
    sc = read("src/actor/npc/ac_npc_shop_common.c")
    check("G2/G3: pc_net_game.h included under TARGET_PC and helper macro defined",
          "#ifdef TARGET_PC\n#include \"pc_net_game.h\"" in sc and
          "#define aNSC_PC_IS_CLIENT() (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT)" in sc and
          "#define aNSC_PC_IS_CLIENT() (0)" in sc, results)
    mw = func_body(sc, r"\nstatic void aNSC_msg_win_open_wait2\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{")
    gi = mw.find("if (aNSC_PC_IS_CLIENT()) {")
    check("G2: offer stage forces action 1 (ORDER_UNAVAILABLE -> REQUEST_Q_ANSWER_WAIT) for a client",
          gi >= 0 and "action = 1;" in mw[gi:gi + 400] and
          "aNSC_MSG_ORDER_UNAVAILABLE" in mw and mw.find("aNSC_ChangeMsgData") > gi, results)
    check("G2: refusal row never routes to ORDER_CHECK (action 1 -> REQUEST_Q_ANSWER_WAIT)",
          "aNSC_ACTION_REQUEST_Q_ANSWER_WAIT, aNSC_ACTION_REQUEST_Q_ANSWER_WAIT," in mw.replace("\n", " ").replace("  ", " ")
          or "aNSC_ACTION_REQUEST_Q_ANSWER_WAIT, aNSC_ACTION_REQUEST_Q_ANSWER_WAIT" in re.sub(r"\s+", " ", mw), results)
    oc = func_body(sc, r"\nstatic void aNSC_order_check\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{")
    go = oc.find("if (aNSC_PC_IS_CLIENT()) {")
    check("G2: order_check defense guard precedes money check, set_ftr_order and PlusSales",
          0 <= go < oc.find("aNSC_money_check(price)") < oc.find("aNSC_set_ftr_order(shop_common);")
          < oc.find("mSP_PlusSales(price);") and "aNSC_MSG_ORDER_CANCEL" in oc[go:go + 400], results)
    check("G2: host order path intact (money check/order/sales still present)",
          "msg_no = aNSC_MSG_ORDER_CONFIRM;" in oc and "aNSC_get_sell_price(price);" in oc, results)
    # two definitions exist (MAMEDANUKI variant first, Nook variant under #else): audit the last one
    sw = func_body(sc[sc.rfind("\nstatic void aNSC_start_wait("):], r"\nstatic void aNSC_start_wait\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{")
    gs = sw.find("if (aNSC_PC_IS_CLIENT() &&")
    check("G3: start_wait diverts REHOUSE/DONE_REHOUSE to WAIT_TYPE_3 for a client",
          gs >= 0 and "wait_type = aNSC_WAIT_TYPE_3;" in sw[gs:gs + 900], results)
    check("G3: divert precedes the action switch, the proc selection and the basement/order writes",
          0 <= gs < sw.find("switch (wait_type)") and gs < sw.find("proc = aNSC_set_talk_info_start_wait;")
          and gs < sw.find("aNSC_set_make_basement_info();"), results)
    check("G3: TYPE_3 proc sets next_action SAY_HELLO_APPROACH like REHOUSE's else-branch (next_action = 1)",
          "shop_common->next_action = aNSC_ACTION_SAY_HELLO_APPROACH;" in sc and "shop_common->next_action = 1;" in sc, results)
    check("G3: host path intact (REHOUSE proc and loan write still present)",
          "proc = aNSC_set_talk_info_start_wait;" in sw and "Now_Private->inventory.loan = next_loan;" in sc, results)
    sa = func_body(sc, r"\nstatic void aNSC_sell_answer0\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{")
    gp = sa.find("aNSC_PC_IS_CLIENT() && shop_common->sell_item >= ITM_RED_PAINT")
    check("G3: paint defense guard precedes the homes[] next_outlook_pal write and the pending flag",
          0 <= gp < sa.find("home->next_outlook_pal =") and gp < sa.find("mPr_FLAG_UPDATE_OUTLOOK_PENDING"), results)
    check("G3: paint refusal reuses next = 0x3 (INSUFFICIENT_FUNDS reply, no debit)", "next = 0x3;" in sa[gp:gp + 500], results)
    check("G3: client paint talks use the foreigner no-sale route (3 sites)",
          "if (ITEM_IS_PAINT(sell_item) && (mLd_PlayerManKindCheck() || aNSC_PC_IS_CLIENT())) {" in sc and
          "if (mLd_PlayerManKindCheck() != FALSE || aNSC_PC_IS_CLIENT()) {" in sc and
          "if (mLd_PlayerManKindCheck() == FALSE && !aNSC_PC_IS_CLIENT()) {" in sc, results)

    # ---- G4 post office
    po = read("src/game/m_post_office.c")
    fw = func_body(po, r"\nextern void mPO_first_work\(\) \{")
    gated_first(fw, ["mTM_AreTimesEqual(", "mPO_first_delivery_proc();", "mPO_adjust_keep_mail();",
                     "Common_Set(force_mail_delivery_flag, TRUE);"], results, "G4")
    check("G4: guard is an early return", "if (%s) {\n        return;\n    }" % CLIENT in fw, results)
    check("G4: mPO_business_proc Stage 0 gate untouched",
          CLIENT in func_body(po, r"\nextern void mPO_business_proc\(GAME_PLAY\* play\) \{"), results)

    # ---- G5 shells / mushrooms
    fi = read("src/game/m_field_info.c")
    ss = func_body(fi, r"\nstatic void mFI_SetShell\(xyz_t player_pos\) \{")
    gated_first(ss, ["mFI_CheckSetShell()", "l_reserve_set_shell++;", "mFI_SetShellWave(l_reserve_set_shell"],
                results, "G5 shell")
    check("G5 shell: early return", "if (%s) {\n        return;\n    }" % CLIENT in ss, results)
    check("G5 shell: mFI_FieldMove still calls mFI_SetShell and mMsr_SetMushroom unconditionally",
          "mFI_SetShell(player_pos);\n    mMsr_SetMushroom(player_pos);" in fi, results)
    mu = read("src/game/m_mushroom.c")
    check("G5 mushroom: pc_net_game.h included under TARGET_PC",
          "#ifdef TARGET_PC\n#include \"pc_net_game.h\"" in mu, results)
    sm = func_body(mu, r"\nextern void mMsr_SetMushroom\(xyz_t player_pos\) \{")
    gated_first(sm, ["mMsr_Mushtime2Rtc(", "mMsr_SetMushroomNum(", "mMsr_ClearMushrooms(", "mMsr_Rtc2MushTime("],
                results, "G5 SetMushroom")
    fm = func_body(mu, r"\nextern void mMsr_FirstClearMushroom\(\) \{")
    gated_first(fm, ["mMsr_Mushtime2Rtc(", "mMsr_ClearMushrooms(", "mMsr_Rtc2MushTime("], results, "G5 FirstClear")

    # ---- G6 balloons
    fu = read("src/game/m_fuusen.c")
    check("G6: pc_net_game.h included under TARGET_PC", "#ifdef TARGET_PC\n#include \"pc_net_game.h\"" in fu, results)
    bm = func_body(fu, r"\nextern void Balloon_move\(GAME_PLAY\* play\) \{")
    gated_first(bm, ["mFI_GET_TYPE(mFI_GetFieldId())", "Balloon_make_fuusen(play);", "Balloon_chk_make_fuusen(play);",
                     "Common_Set(balloon_last_spawn_min, min);"], results, "G6")
    check("G6: early return", "if (%s) {\n    return;\n  }" % CLIENT in bm, results)

    # ---- protocol / wire unchanged
    hdr = read("pc/include/pc_net_game.h")
    check("protocol is v%d (wire_baseline.EXPECTED_PROTOCOL_VERSION)" % wire_baseline.EXPECTED_PROTOCOL_VERSION,
          wire_baseline.header_protocol_ok(hdr), results)
    # wire contract vs HEAD (content-based, see wire_baseline.py): the files may legitimately change, the WIRE may not
    wire_baseline.run(lambda d, c: check(d, c, results), ROOT)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
