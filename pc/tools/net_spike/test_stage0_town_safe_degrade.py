#!/usr/bin/env python3
"""test_stage0_town_safe_degrade.py - Stage 0 (safe degrade) source-audit checks (NO game process is launched).

A network CLIENT must not independently mutate host-owned shared town state that protocol v7 does not
replicate: shop lineup/renewal, post office delivery, player mail, police box adders and claims, museum
donations. Host and single-player fall through unchanged. Shop purchases/sells stay local (documented).

  S0-1  shop: mSP_ExchangeLineUp_InGame, aSL_RenewShop, aSL_JudgeRenewShop, aSL_ExchangeShopGoodsInGame,
        mSP_InRenewal gated on the CLIENT role before their first mutation.
  S0-2  postman: mPO_business_proc gated; villager MAIL_REQUEST markers still exist.
  S0-3  copper + lost & found adders: ac_npc_police.c spawn, mPB_keep_item, mPB_keep_all_item_in_block.
  S0-4  museum: (town services) a client's donation runs the host transaction before the put-away demo (REJECTED -> return row); putaway
        init never calls the commit/clear for a client; gift-carrying museum mail refused at the counter.
  S0-5  police claim: (town services) a client claims through the host transaction; REJECTED / pockets full take the existing idx == -1 refusal; no local pocket/slot write.
  S0-6  player mail: aPG_check_destination refuses PLAYER (and gift MUSEUM) mail for a client with the
        existing "no such address" code; the letter is removed only after that refusal path restores it.

Runtime behavior is NOT TESTED. Usage: python3 test_stage0_town_safe_degrade.py
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


def gated_first(body, mutators, results, tag, token=CLIENT):
    """Guard exists, sits inside #ifdef TARGET_PC, and precedes every listed mutating call."""
    g = body.find(token)
    check("%s: guard present" % tag, g >= 0, results)
    check("%s: guard is under #ifdef TARGET_PC" % tag, g >= 0 and "#ifdef TARGET_PC" in body[:g], results)
    for m in mutators:
        pos = body.find(m)
        check("%s: guard precedes %s" % (tag, m), g >= 0 and pos >= 0 and g < pos, results)


def main():
    results = []

    # ---- S0-1 shop
    shop = read("src/game/m_shop.c")
    check("S0-1: m_shop.c includes pc_net_game.h under TARGET_PC",
          re.search(r'#ifdef TARGET_PC\n#include "pc_settings.h"\n#include "pc_net_game.h"[^\n]*\n#endif', shop) is not None,
          results)
    ex = func_body(shop, r"\nextern void mSP_ExchangeLineUp_InGame\(GAME\* game\) \{")
    check("S0-1: mSP_ExchangeLineUp_InGame found", bool(ex), results)
    gated_first(ex, ["mSP_CheckExchangeDay2()", "mSP_NewExchangeDay()", "mSP_ExchangeLineUp_GameAlloc(game)",
                     "mSP_LotteryLineUp_GameAlloc(game)"], results, "S0-1 lineup")
    check("S0-1 lineup: guard is a bare return (no mutation inside)",
          re.search(r"if \(pc_net_game_role\(\) == PC_NETGAME_ROLE_CLIENT && !s_shop_first_lineup\) \{\n\s+return;\n\s+\}", ex) is not None, results)
    gs = func_body(shop, r"\nextern void mSP_ShopGameStartCt\(GAME\* game\) \{")
    check("S0-1 new-town: mSP_ShopGameStartCt seeds the first lineup for a client (flag set after init, before lineup, cleared after)",
          bool(gs) and gs.find("mSP_InitShopSaveData();") < gs.find("s_shop_first_lineup = 1;")
          < gs.find("mSP_ExchangeLineUp_InGame(game);") < gs.find("s_shop_first_lineup = 0;"), results)
    check("S0-1 lineup: host/offline fall-through intact",
          "if (mSP_CheckExchangeDay2()) {" in ex and "Save_Get(shop).shop_info.not_loaded_before = TRUE;" in ex, results)
    inr = func_body(shop, r"\nextern int mSP_InRenewal\(\) \{")
    gated_first(inr, ["mEv_CheckEvent(mEv_SAVED_RENEWSHOP)"], results, "S0-1 InRenewal")
    check("S0-1 InRenewal: client gets FALSE", "return FALSE;" in inr[:inr.find("mEv_CheckEvent")], results)
    # Batch G2/G3 added client refusals to ac_npc_shop_common.c (catalog order, house loan/rehouse, paint), so the old
    # "file has no pc_net_game" check is stale. Current truth: purchases and sells stay UNGATED -- the sell/buy bodies and
    # ac_shop_design.c carry no role gate -- and the only role-gated spots in ac_npc_shop_common.c are the G2/G3 refusal sites.
    nsc = read("src/actor/npc/ac_npc_shop_common.c")
    check("S0-1: shop purchase/sell paths still ungated (ac_shop_design.c has no role gate)",
          "pc_net_game" not in read("src/actor/ac_shop_design.c"), results)
    spans = []
    for fname in ("aNSC_sell_item_init", "aNSC_sell_item_with_ticket_init", "aNSC_buy_check_init", "aNSC_buy_check"):
        b = func_body(nsc, r"\nstatic void %s\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{" % fname)
        check("S0-1: %s found and contains no pc_net_game / role gate (purchase/sell stays ungated)" % fname,
              bool(b) and "pc_net_game" not in b and "aNSC_PC_IS_CLIENT" not in b, results)
        if b:
            spans.append((nsc.index(b), nsc.index(b) + len(b)))
    sites = [m.start() for m in re.finditer(r"aNSC_PC_IS_CLIENT\(\)", nsc)
             if not nsc[nsc.rfind("\n", 0, m.start()) + 1:m.start()].lstrip().startswith("#define")]
    check("S0-1: ac_npc_shop_common.c role gates are exactly the 7 G2/G3 refusal sites (paint x3, rehouse, order x2, "
          "paint-sell) and none lies inside a purchase/sell body (found %d)" % len(sites),
          len(sites) == 7 and not [x for x in sites if any(a <= x < b for a, b in spans)], results)
    sl = read("src/actor/ac_shop_level.c")
    check("S0-1: ac_shop_level.c includes pc_net_game.h under TARGET_PC",
          '#ifdef TARGET_PC\n#include "pc_net_game.h"' in sl, results)
    rn = func_body(sl, r"\nstatic void aSL_RenewShop\(ACTOR\* actorx, GAME\* game\) \{")
    gated_first(rn, ["aSL_RewriteShopFg(", "mSP_RenewShopLevel()", "mEv_EventOFF(mEv_SAVED_RENEWSHOP)",
                     "mSP_ExchangeLineUp_ZeldaMalloc()"], results, "S0-1 aSL_RenewShop")
    check("S0-1 aSL_RenewShop: host path intact",
          "aSL_RewriteShopFg(actorx, game, mSP_GetShopLevel(), mSP_GetRealShopLevel()) && mSP_RenewShopLevel()" in rn,
          results)
    jr = func_body(sl, r"\nstatic void aSL_JudgeRenewShop\(ACTOR\* actorx, GAME\* game\) \{")
    gated_first(jr, ["mEv_EventON(mEv_SAVED_RENEWSHOP)", "Save_Get(shop).renewal_time =",
                     "aSL_SetRenewalChiraswhi_Notice()"], results, "S0-1 aSL_JudgeRenewShop")
    xs = func_body(sl, r"\nstatic void aSL_ExchangeShopGoodsInGame\(ACTOR\* actorx, GAME\* game, lbRTC_hour_t hour\) \{")
    gated_first(xs, ["mSP_NewExchangeDay()", "mSP_ExchangeLineUp_ZeldaMalloc()", "mSP_LotteryLineUp_ZeldaMalloc()"],
                results, "S0-1 aSL_ExchangeShopGoodsInGame")

    # ---- S0-2 postman
    po = read("src/game/m_post_office.c")
    check("S0-2: m_post_office.c includes pc_net_game.h under TARGET_PC",
          '#ifdef TARGET_PC\n#include "pc_net_game.h"' in po, results)
    bp = func_body(po, r"\nextern void mPO_business_proc\(GAME_PLAY\* play\) \{")
    gated_first(bp, ["mPO_delivery_proc(play)"], results, "S0-2 mPO_business_proc")
    check("S0-2: host/offline fall-through intact",
          "Common_Get(clip).demo_clip == NULL" in bp and "case SCENE_PLAYERSELECT:" in bp, results)
    npc = read("src/game/m_npc.c")
    mr = func_body(npc, r"\nextern int mNpc_SendMailtoNpc\(Mail_c\* mail\) \{") or npc
    check("S0-2: villager MAIL_REQUEST client intercept marker intact in m_npc.c",
          "pc_net_game_request_mail_delivery(client_anm_idx, mail, sizeof(*mail));" in npc
          and "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT" in npc, results)
    net = read("pc/src/pc_net_game.c")
    check("S0-2: host MAIL_REQUEST handler / MAIL_DELIVERED broadcast still exist",
          "pcnetgame_handle_host_mail_request" in net and "PC_NETGAME_MSG_MAIL_DELIVERED" in net, results)
    check("S0-2: mPO_delivery_one_address (postman actor path) not edited",
          "pc_net_game" not in func_body(po, r"\nextern int mPO_delivery_one_address\(int house_no\) \{"), results)

    # ---- S0-3 copper + adders
    pol = read("src/actor/npc/ac_npc_police.c")
    ct = func_body(pol, r"\nstatic void aPOL_actor_ct\(ACTOR\* actorx, GAME\* game\) \{")
    check("S0-3: ac_npc_police.c includes pc_net_game.h under TARGET_PC",
          '#ifdef TARGET_PC\n#include "pc_net_game.h"' in pol, results)
    gp = ct.find(NOT_CLIENT)
    kp = ct.find("mPB_keep_item(*mFI_GetUnitFG(actorx->home.position));")
    sp = ct.find("mFI_SetFG_common(RSV_NO, actorx->home.position, TRUE);")
    check("S0-3: copper spawn keep + RSV_NO write both inside the not-client guard (TARGET_PC)",
          0 <= ct.find("#ifdef TARGET_PC") < gp < kp < sp < ct.find("#else"), results)
    check("S0-3: non-PC build keeps the original two unguarded calls (#else branch)",
          ct.find("#else") < ct.rfind("mPB_keep_item(*mFI_GetUnitFG(actorx->home.position));") < ct.find("#endif"),
          results)
    pb = read("src/game/m_police_box.c")
    check("S0-3: m_police_box.c includes pc_net_game.h under TARGET_PC",
          '#ifdef TARGET_PC\n#include "pc_net_game.h"' in pb, results)
    ki = func_body(pb, r"\nextern void mPB_keep_item\(mActor_name_t item_no\) \{")
    gated_first(ki, ["mPB_get_keep_item_sum()", "Save_Set(police_box.keep_items"], results, "S0-3 mPB_keep_item")
    check("S0-3 mPB_keep_item: guard is a bare return",
          re.search(r"if \(pc_net_game_role\(\) == PC_NETGAME_ROLE_CLIENT\) \{\n\s+return;\n\s+\}", ki) is not None, results)
    ka = func_body(pb, r"\nextern void mPB_keep_all_item_in_block\(int blk_x, int blk_z\) \{")
    g = ka.find(CLIENT)
    check("S0-3 keep_all: tile clearing and deposit clear still run for a client (guard only skips the array merge)",
          0 <= ka.find("*block_items = EMPTY_NO;") < g < ka.find("if (count > 0) {")
          and ka.find("mFI_ClearDeposit(blk_x, blk_z);") > g and "count = 0;" in ka[g:g + 120], results)
    check("S0-3: mPB_force_set_keep_item routes through the gated mPB_keep_item",
          "mPB_keep_item(mPB_get_force_set_item());" in func_body(pb, r"\nextern void mPB_force_set_keep_item\(\) \{"),
          results)
    check("S0-3: host snowman path (pc_net_game.c) still calls mPB_keep_item",
          "mPB_keep_item((mActor_name_t)prior);" in net, results)

    # ---- S0-4 museum
    cur = read("src/actor/npc/ac_npc_curator.c")
    check("S0-4: ac_npc_curator.c includes pc_net_game.h under TARGET_PC",
          '#ifdef TARGET_PC\n#include "pc_net_game.h"' in cur, results)
    cm = read("src/actor/npc/ac_npc_curator_move.c_inc")
    ow = func_body(cm, r"\nstatic void aCR_msg_win_open_wait\(NPC_CURATOR_ACTOR\* curator, GAME_PLAY\* play\) \{")
    check("S0-4: aCR_msg_win_open_wait found", bool(ow), results)
    g = ow.find("pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && act_idx >= 16")
    fish = ow.find("act_idx = aCR_get_idx_to_donate_fish(item);")
    use = ow.find("donate_act_p = &donate_act[act_idx];")
    setmsg = ow.find("mMsg_Set_continue_msg_num(msg_p, donate_act_p->msg_no);")
    beg = ow.find("pc_net_game_ts_begin_museum_donate(play->submenu.item_p->slot_no, (int)item)", g)
    poll = ow.find("pc_net_game_ts_poll()", beg)
    check("S0-4 (town services, replaces the give-back): a client's put-away rows (act_idx >= 16) run the host transaction after classification and before "
          "the row is used: begin (busy -> return, refused -> row 2) -> wait (PENDING returns, no state change) -> APPLIED proceeds, anything else -> row 2",
          0 <= fish < g < beg < poll < use < setmsg and "#ifdef TARGET_PC" in ow[fish:g]
          and "ts_begin < 0" in ow[beg - 80:poll] and ow[g:poll].count("act_idx = 2;") == 1
          and "PC_NETGAME_TS_OP_PENDING" in ow[poll - 80:use] and "return;" in ow[poll:use]
          and "ts_res != PC_NETGAME_TS_OP_APPLIED" in ow[poll:use] and ow[poll:use].count("act_idx = 2;") == 1, results)
    check("S0-4: the client branch itself never commits the museum bit or touches the pocket (the pocket changes only in the host-APPLIED apply step; the bit arrives by mirror)",
          "mMmd_RequestMuseumDisplay" not in ow and "mPr_SetPossessionItem" not in ow and "inventory" not in ow, results)
    check("S0-4: row 2 is the existing return-demo refusal row; rows >= 16 are the put-away rows",
          "{ 0x2F64, aCR_TALK_RETURN_DEMO_START_WAIT }," in cm
          and cm.index("{ 0x2F8F, aCR_TALK_PUTAWAY_DEMO_START_WAIT2 },") > cm.index("{ 0x2F6A, aCR_TALK_RETURN_DEMO_START_WAIT2 },"),
          results)
    rows = re.search(r"donate_act\[\] = \{\n(.*?)\n    \};", cm, re.S).group(1).strip().split("\n")
    check("S0-4: donate_act row 16 is the first PUTAWAY row (index math behind 'act_idx >= 16')",
          "PUTAWAY" in rows[16] and all("PUTAWAY" not in r for r in rows[:16]) and all("PUTAWAY" in r for r in rows[16:]),
          results)
    pi = func_body(cm, r"\nstatic void aCR_putaway_demo_end_wait_init\(NPC_CURATOR_ACTOR\* curator, GAME_PLAY\* play\) \{")
    gc = pi.find(NOT_CLIENT)
    check("S0-4: putaway init: commit + pocket clear only reachable when not client (defense in depth)",
          0 <= gc < pi.find("mMmd_RequestMuseumDisplay(sm_item_p->item)") < pi.find("mPr_SetPossessionItem(")
          and pi.find("mMsg_Set_LockContinue") > pi.find("mPr_SetPossessionItem("), results)
    check("S0-4: return path for refusal rows does not call the commit/clear (RETURN_DEMO functions)",
          "mMmd_RequestMuseumDisplay" not in func_body(cm, r"\nstatic void aCR_return_demo_start_wait\(NPC_CURATOR_ACTOR\* curator, GAME_PLAY\* play\) \{")
          and "mPr_SetPossessionItem" not in func_body(cm, r"\nstatic void aCR_return_demo_end_wait\(NPC_CURATOR_ACTOR\* curator, GAME_PLAY\* play\) \{"),
          results)
    check("S0-4: mMmd_RequestMuseumDisplay still the commit on the host path",
          "if (mMmd_RequestMuseumDisplay(sm_item_p->item) == TRUE) {" in pi, results)

    # ---- S0-5 police claim
    p2 = read("src/actor/npc/ac_npc_police2_move.c_inc")
    check("S0-5: police2 includes pc_net_game.h under TARGET_PC",
          '#ifdef TARGET_PC\n#include "pc_net_game.h"' in read("src/actor/npc/ac_npc_police2.c"), results)
    ca = func_body(p2, r"\nstatic void aPOL2_check_answer\(NPC_POLICE2_ACTOR\* actor, GAME_PLAY\* play\) \{")
    check("S0-5: aPOL2_check_answer found", bool(ca), results)
    g = ca.find(CLIENT)
    gi = ca.find("idx = -1;", g)
    ticket = ca.find("mPlib_Get_space_putin_item_forTICKET(item_p)")
    normal = ca.find("idx = mPlib_Get_space_putin_item();")
    refuse = ca.find("if (idx == -1) {")
    sfg = ca.find("mFI_SetFG_common(RSV_NO, dummy_pos, TRUE);")
    spi = ca.find("mPr_SetPossessionItem(Now_Private, idx, *item_p")
    clr = ca.find("Save_Get(police_box).keep_items[actor->item_idx] = EMPTY_NO;")
    beg = ca.find("pc_net_game_ts_begin_police_claim(free_slot, aPOL2_ts_idx, (int)aPOL2_ts_item)")
    poll = ca.find("pc_net_game_ts_poll()", beg)
    check("S0-5 (town services, replaces the refusal): a client defaults to the existing refusal (idx = -1), picks its own free slot, begins the host claim with "
          "(lost-and-found slot, EXPECTED item, pocket slot), waits while PENDING and only an APPLIED answer sets claimed_remote -- all before the vanilla host path",
          0 <= ca.find("#ifdef TARGET_PC") < g < gi < beg < poll < ticket < normal < refuse < sfg < spi < clr
          and "int free_slot = mPlib_Get_space_putin_item();" in ca[g:beg] and "return;" in ca[poll:poll + 160] and "PC_NETGAME_TS_OP_PENDING" in ca[poll:poll + 160]
          and "claimed_remote = TRUE;" in ca[poll:ticket] and ca.count("claimed_remote = TRUE;") == 1, results)
    check("S0-5: the vanilla pocket write and the local lost-and-found slot clear are skipped for a remote claim (if (!claimed_remote)); the 'here you go' branch is kept",
          "if (!claimed_remote)" in ca[sfg:spi] and ca.index("mPr_SetPossessionItem(Now_Private, idx, *item_p") < ca.index("sAdo_OngenTrgStart(NA_SE_ITEM_GET"), results)
    check("S0-5: refusal branch uses msg 0x0781 and holds no item removal / slot clear",
          "mMsg_Set_continue_msg_num(msg_p, 0x0781);" in ca[refuse:sfg], results)
    check("S0-5: fall-through to talk-end-wait intact (item_idx = -1; aPOL2_ACT_TALK_END_WAIT)",
          "actor->item_idx = -1;" in ca and "aPOL2_setupAction(actor, aPOL2_ACT_TALK_END_WAIT);" in ca, results)
    check("S0-5: host/offline claim path intact",
          "mPr_SetPossessionItem(Now_Private, idx, *item_p, mPr_ITEM_COND_NORMAL);" in ca
          and "mPlib_Get_space_putin_item_forTICKET(item_p)" in ca, results)
    go = func_body(p2, r"\nstatic void aPOL2_player_getout_check\(GAME_PLAY\* play, ACTOR\* playerx\) \{")
    check("S0-5 (review M2): leave-building compaction is the vanilla local mPB_copy_itemBuf for host / solo and is SKIPPED for a client (its copy is the host's mirror)",
          "mPB_copy_itemBuf(Save_Get(police_box).keep_items);" in p2 and "pc_net_game_role() != PC_NETGAME_ROLE_CLIENT" in go
          and go.index("pc_net_game_role() != PC_NETGAME_ROLE_CLIENT") < go.index("mPB_copy_itemBuf("), results)

    # ---- S0-6 player mail
    pg = read("src/actor/npc/ac_npc_post_girl.c_inc")
    check("S0-6: post girl includes pc_net_game.h under TARGET_PC",
          '#ifdef TARGET_PC\n#include "pc_net_game.h"' in read("src/actor/npc/ac_npc_post_girl.c"), results)
    cd = func_body(pg, r"\nint aPG_check_destination\(Mail_c \*mail\)\{")
    check("S0-6: aPG_check_destination found", bool(cd), results)
    g = cd.find(CLIENT)
    check("S0-6: client refusal under TARGET_PC before the recipient switch and any lookup",
          0 <= cd.find("#ifdef TARGET_PC") < g < cd.find("switch (mail->header.recipient.type)")
          and g < cd.find("mMl_hunt_for_send_address(mail)"), results)
    blk = cd[g:cd.find("#endif", g)]
    check("S0-6: refuses PLAYER type and MUSEUM-with-gift only, returns 1 (existing 'no such address')",
          "mMl_NAME_TYPE_PLAYER" in blk and "mMl_NAME_TYPE_MUSEUM" in blk and "mail->present != EMPTY_NO" in blk
          and "return 1;" in blk and "mMl_NAME_TYPE_NPC" not in blk, results)
    check("S0-6: villager (case 1) validation untouched",
          "mNpc_SearchAnimalPersonalID(&anm_pid) == -1" in cd, results)
    rc = func_body(pg, r"\nvoid aPG_receive_menu_close_wait\(NPC_POSTGIRL_ACTOR \*postgirl, GAME_PLAY \*play\) \{")
    ok0 = rc.find("mPO_receipt_proc(&play->submenu.mail, mPO_SENDTYPE_MAIL);")
    rest = rc.find("mMl_copy_mail(&Now_Private->mail[submenu_item->slot_no], &play->submenu.mail);")
    chk = rc.find("aPG_check_destination(&play->submenu.mail)")
    check("S0-6: receipt (letter removal) only in case 0 after the destination check; refusal cases restore the letter",
          0 <= chk < ok0 < rest and "case 1:\n            case 2:\n            case 3:" in rc, results)
    check("S0-6: no other player-mail send UI (only the post girl calls aPG_check_destination)",
          sum(read(p).count("aPG_check_destination(") for p in ("src/actor/npc/ac_npc_post_girl.c_inc",)) == 2, results)

    # ---- protocol / wire unchanged
    hdr = read("pc/include/pc_net_game.h")
    check("protocol is v%d (wire_baseline.EXPECTED_PROTOCOL_VERSION)" % wire_baseline.EXPECTED_PROTOCOL_VERSION,
          wire_baseline.header_protocol_ok(hdr), results)
    # wire contract vs HEAD (content-based, see wire_baseline.py): the files may legitimately change, the WIRE may not
    wire_baseline.run(lambda d, c: check(d, c, results), ROOT)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
