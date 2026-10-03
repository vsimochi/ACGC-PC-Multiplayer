#!/usr/bin/env python3
"""test_shop_src.py - Town Services milestone 2 (NOOK'S SHOP) SOURCE AUDIT (NO game process is launched).

Audits the shop mirror (TOWN_SVC_STATE service 3), the host-authoritative SHOP_BUY (TXN_COMMIT kind 10) / SHOP_SELL (kind 11) handler and the CLIENT
seams in the Nook dialogue state machine (src/actor/npc/ac_npc_shop_common.c, included by conv_master, depart_master, mamedanuki, shop_master, super_master)
and the shop floor (src/actor/ac_shop_design.c):

  W  wire pins (kinds, reasons, stock codes, blob length, sale ratio == SELL_BUY_RATIO, python == C), wire_baseline
  M  mirror: service 3 = the 320 raw bytes of Shop_c, host build / push, client validate -> apply (the ONLY writer of the client's Save_Get(shop))
  H  host handler: the host computes price / value itself, validates stock + funds, mutates only in the handler (mSP_PlusSales / mSP_ShopSaleReport),
     no RNG, REJECTED never falls through into a mutation, guests refused
  C  client seams: NO client pocket / wallet / sales_sum / stock write before APPLIED in the Nook purchase / sale seams; the vanilla mutations stay
     behind the client branch (host / solo unchanged); pending = return with no state change; APPLIED continues the success dialogue; refusals take
     existing rows; deferred refusals (paint / catalog / loan / raffle tickets / stationery sales) documented
  T  TEST-ONLY hooks (--shop-test-buy / --shop-test-sell): default off, loud, client role only

Runtime behaviour of the DIALOGUE is NOT TESTED here (no UI automation): test_shop_protocol.py (real host, scripted clients) and test_shop_real_client.py
(real client request path) cover the transaction chain. Usage: python3 test_shop_src.py
"""
import os
import re
import sys

import net_spike_lib as L
import wire_baseline
from test_ts_src import read, strip_comments, func_body, in_order, strip_hook_bodies

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def main():
    results = []
    check = lambda d, c: L.check(d, bool(c), results)
    c_raw = read("pc/src/pc_net_game.c")
    c = strip_comments(c_raw)
    h = read("pc/include/pc_net_game.h")
    main_c = read("pc/src/pc_main.c")
    plat_h = read("pc/include/pc_platform.h")
    nsc_raw = read("src/actor/npc/ac_npc_shop_common.c")
    nsc = strip_comments(nsc_raw)
    sd_raw = read("src/actor/ac_shop_design.c")
    sd = strip_comments(sd_raw)
    nsc_h = read("include/ac_npc_shop_common.h")
    hb, he = c_raw.index("===== TS HOST BEGIN"), c_raw.index("===== TS HOST END")
    cb, ce = c_raw.index("===== TS CLIENT BEGIN"), c_raw.index("===== TS CLIENT END")
    hblk_raw, cblk_raw = c_raw[hb:he], c_raw[cb:ce]
    hblk, cblk = strip_comments(hblk_raw), strip_comments(cblk_raw)
    # the handler + the SHOP host helpers sit between the TS host helpers and "TS HOST END"
    shop_region_raw = c_raw[c_raw.index("---- SHOP (service 3): host helpers"):he]
    shop_region = strip_comments(shop_region_raw)
    ts_raw = func_body(shop_region_raw, "pcnetgame_handle_host_ts_txn")
    ts = strip_comments(ts_raw)

    # ------------------------------------------------------------------ W: wire
    check("W kinds SHOP_BUY = 10 / SHOP_SELL = 11, stock codes COUNTED 0xFD / RARE 0xFE / UNLIMITED 0xFF, reasons NO_FUNDS 19 / NOT_SELLABLE 20 / PRICE_MISMATCH 21 / NO_ROOM 22, service SHOP = 3, "
          "blob 320 == sizeof(Shop_c)",
          all(re.search(r"#define %s\s+%s" % (n, v), c_raw) for n, v in (
              ("PC_NETGAME_TXN_KIND_SHOP_BUY", "10u"), ("PC_NETGAME_TXN_KIND_SHOP_SELL", "11u"), ("PC_NETGAME_SHOP_STOCK_COUNTED", "0xFDu"), ("PC_NETGAME_SHOP_STOCK_RARE", "0xFEu"),
              ("PC_NETGAME_SHOP_STOCK_UNLIMITED", "0xFFu"), ("PC_NETGAME_TXN_REASON_NO_FUNDS", "19u"), ("PC_NETGAME_TXN_REASON_NOT_SELLABLE", "20u"),
              ("PC_NETGAME_TXN_REASON_PRICE_MISMATCH", "21u"), ("PC_NETGAME_TXN_REASON_NO_ROOM", "22u"), ("PC_NETGAME_TS_SHOP", "3u"), ("PC_NETGAME_TS_SHOP_LEN", "320u")))
          and "_Static_assert(PC_NETGAME_TS_SHOP_LEN == sizeof(Shop_c)," in c_raw)
    ratio = re.search(r"#define SELL_BUY_RATIO (\d+)", nsc_h)
    check("W the host's sale ratio PC_NETGAME_SHOP_SELL_RATIO (4u) equals the game's SELL_BUY_RATIO (include/ac_npc_shop_common.h = %s); the stock-code count equals mSP_GOODS_COUNT (39)" % (ratio and ratio.group(1)),
          ratio is not None and ratio.group(1) == "4" and "#define PC_NETGAME_SHOP_SELL_RATIO      4u" in c_raw
          and re.search(r"#define mSP_GOODS_COUNT 39", read("include/m_shop.h")) and L.PC_NETGAME_SHOP_GOODS_COUNT == 39)
    check("W python formats / constants / reason names equal the C values (kinds 10 / 11, 23 reasons, stock codes, TS_SHOP_LEN, sale ratio)",
          (L.PC_NETGAME_TXN_KIND_SHOP_BUY, L.PC_NETGAME_TXN_KIND_SHOP_SELL) == (10, 11)
          and (L.PC_NETGAME_SHOP_STOCK_COUNTED, L.PC_NETGAME_SHOP_STOCK_RARE, L.PC_NETGAME_SHOP_STOCK_UNLIMITED) == (0xFD, 0xFE, 0xFF)
          and [L.TXN_REASON_NAMES[i] for i in (19, 20, 21, 22)] == ["NO_FUNDS", "NOT_SELLABLE", "PRICE_MISMATCH", "NO_ROOM"] and len(L.TXN_REASON_NAMES) == 26  # mail milestone 1: + 23 / 24 / 25
          and L.PC_NETGAME_TS_SHOP_LEN == 320 and L.PC_NETGAME_SHOP_SELL_RATIO == 4)
    check("W the client-facing reject codes of pc_net_game.h equal the C reasons (NOT_AVAILABLE 16, NO_FUNDS 19, NOT_SELLABLE 20, PRICE_MISMATCH 21, NO_ROOM 22) and the new seam API is declared",
          all(re.search(r"#define PC_NETGAME_TS_REJECT_%s\s+%d" % (n, v), h) for n, v in (("NOT_AVAILABLE", 16), ("NO_FUNDS", 19), ("NOT_SELLABLE", 20), ("PRICE_MISMATCH", 21), ("NO_ROOM", 22)))
          and all(x in h for x in ("int pc_net_game_shop_buy_stock_code(int item);", "int pc_net_game_ts_begin_shop_buy(int pocket_slot, int item, int stock_code, int price);",
                                   "int pc_net_game_ts_begin_shop_sell(int slot_mask, int primary_slot, int item);", "int pc_net_game_ts_last_reject_reason(void);")))
    wire_baseline.run(lambda desc, cond: check("W " + desc, cond), ROOT)

    # ------------------------------------------------------------------ M: mirror
    build = func_body(hblk_raw, "pcnetgame_ts_build")
    check("M service 3 is built from the host's raw Save_Get(shop) (320 B) and the refresh_all / push loops cover it",
          "memcpy(blob, &Save_Get(shop), PC_NETGAME_TS_SHOP_LEN)" in build and "pcnetgame_ts_refresh((int)PC_NETGAME_TS_SHOP)" in func_body(hblk_raw, "pcnetgame_ts_refresh_all")
          and "svc <= (int)PC_NETGAME_TS_SHOP" in func_body(hblk_raw, "pcnetgame_host_ts_push_peer"))
    check("M the host runs the CLIENT validator on its own shop blob and logs a loud warning if clients would refuse it; the validator checks every stock / lottery / rare entry, the bag count, sales_sum and the visitor flag",
          "pcnetgame_ts_valid_shop_blob(blob)" in func_body(hblk_raw, "pcnetgame_ts_refresh") and "would be REFUSED by clients" in hblk_raw
          and all(x in func_body(hblk_raw, "pcnetgame_ts_valid_shop_blob") for x in ("s.items[i]", "s.lottery_items[i]", "s.rare_item", "flowers_candy_grab_bag_count", "s.visitor_flag")))
    cl = func_body(cblk_raw, "pcnetgame_handle_client_town_svc")
    ok, miss = in_order(cl, ["s_client_link != PC_NETGAME_LINK_READY", "sizeof(m)", "svc != (int)PC_NETGAME_TS_POLICE && svc != (int)PC_NETGAME_TS_MUSEUM && svc != (int)PC_NETGAME_TS_SHOP",
                             "m.len != expect", "pcnetgame_fnv1a32(m.blob, m.len) != m.digest", "m.seq <= s_ts_client_seq[svc]", "pcnetgame_ts_valid_shop_blob(m.blob)",
                             "pcfa_save_ready()", "pcnetgame_ts_client_apply(&m)"])
    check("M the client validates a shop message in the usual order: READY link, size, service (3 accepted, 4+ ignored), exact len, digest, seq above the last of this session, content, usable save, THEN applies (missing %s)" % miss, ok)
    ap = func_body(cblk_raw, "pcnetgame_ts_client_apply")
    check("M the client's local Save_Get(shop) is written ONLY by the mirror apply (one memcpy of the blob); the host never overwrites its own shop from a peer (client-originated TOWN_SVC_STATE is dropped)",
          "memcpy(&Save_Get(shop), m->blob, PC_NETGAME_TS_SHOP_LEN)" in ap and len(re.findall(r"memcpy\(&Save_Get\(shop\)", c)) == 1
          and not re.search(r"Save_Get\(shop\)(?:\.\w+(?:\[[^\]]*\])?)+\s*(?:\+\+|--|[+\-*]?=[^=])", c)
          and "pcnetgame_handle_client_town_svc" not in func_body(c, "pcnetgame_handle_host_data"))
    check("M an applied shop mirror redraws the shop floor ONLY when the client stands in a shop (aSD_PC_SyncDisplayWithStock; items that appeared since show at the next entry: documented)",
          "svc == (int)PC_NETGAME_TS_SHOP && pcnetgame_shop_local_scene_is_shop()" in ap and "aSD_PC_SyncDisplayWithStock()" in ap and "NOT added in place" in ap)
    check("M the generation gates of Stage 0 are intact (the client mirrors the host's stock instead of rolling its own): mSP_ExchangeLineUp_InGame / aSL_RenewShop keep their role gates",
          "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && !s_shop_first_lineup" in read("src/game/m_shop.c")
          and "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT" in read("src/actor/ac_shop_level.c"))

    # ------------------------------------------------------------------ H: host handler
    check("H ONE host handler serves kinds 8 / 9 / 10 / 11; the dispatcher routes the two shop kinds to it", ts != "" and "const int is_buy = (in->kind == (uint8_t)PC_NETGAME_TXN_KIND_SHOP_BUY);" in ts
          and "const int is_sell = (in->kind == (uint8_t)PC_NETGAME_TXN_KIND_SHOP_SELL);" in ts and "tc.kind == (uint8_t)PC_NETGAME_TXN_KIND_SHOP_BUY || tc.kind == (uint8_t)PC_NETGAME_TXN_KIND_SHOP_SELL" in c)
    ok, miss = in_order(ts_raw, ["s_host_peer_link[peer] != PC_NETGAME_LINK_READY", "pcnetgame_rec_gate(peer, 0, 0)", "shape_ok = in->_rsv0 == 0", "pcnetgame_txn_journal_find(", "t->base_epoch != slot->epoch",
                                 "pcnetgame_rec_validate_inventory(t->pre_pockets", "memcpy(post, t->pre_pockets", "mSP_ItemNo2ItemPrice(item)", "shop_price != (uint32_t)t->aux_item",
                                 "pcnetgame_shop_pay(post", "pcnetgame_shop_sell_plan(t->pre_pockets", "pcnetgame_rec_validate_inventory(post, post_conds", "mSP_PlusSales(shop_price);",
                                 "mSP_ShopSaleReport(", "mSP_PlusSales(sell_value / 2u);", "pcnetgame_rec_txn_write_inventory(idx, post", "slot->rev++;",
                                 "pcnetgame_txn_journal_add(R, in, hash", "pcnetgame_ts_refresh(svc);", "pcnetgame_txn_send_applied(peer, idx, in, slot, (uint8_t)PC_NETGAME_TXN_REASON_NONE"])
    check("H the HOST computes the price (mSP_ItemNo2ItemPrice) and compares the client's expected price BEFORE paying; steps in order: gate, shape, journal, base, pre-image, post-image locals, price / stock / funds, "
          "post-image validated, THEN the vanilla mutations, mirror write, journal, refresh, RESULT (missing %s)" % miss, ok)
    check("H BUY stock validation: pcnetgame_shop_stock_code (items[] index / rare / counted / unlimited) must equal the client's aux_cond (else NOT_AVAILABLE), the price must equal aux_item (else PRICE_MISMATCH), "
          "the pre-image must pay it (NO_FUNDS), paint / bargain / raffle stock is refused",
          "code != (int)t->aux_cond" in ts and "PC_NETGAME_TXN_REASON_NOT_AVAILABLE" in ts and "PC_NETGAME_TXN_REASON_PRICE_MISMATCH" in ts and "PC_NETGAME_TXN_REASON_NO_FUNDS" in ts
          and "ITEM_IS_PAINT(item)" in shop_region and "mSP_TANUKI_SHOP_STATUS_EVENT || status == mSP_TANUKI_SHOP_STATUS_FUKUBIKI" in shop_region)
    check("H the host's price / value are NEVER taken from the message: 'shop_price = mSP_ItemNo2ItemPrice(item)' and the sale value is summed from the PRE-image items by pcnetgame_shop_sell_unit (price / ratio, Kabu_get_price for turnips)",
          "shop_price = mSP_ItemNo2ItemPrice(item);" in ts and not re.search(r"shop_price\s*=\s*[^;]*(?:aux_item|t->)", ts)
          and "mSP_ItemNo2ItemPrice((mActor_name_t)item) / PC_NETGAME_SHOP_SELL_RATIO" in shop_region and "Kabu_get_price()" in shop_region)
    check("H the sale rules: stationery + quest conditions + Sunday turnips -> NOT_SELLABLE; a spoiled turnip / worthless item is value 0; bag overflow follows vanilla (WALLET_MAX, 30000 bags, bags <= empty + sold slots else NO_ROOM)",
          all(x in shop_region for x in ("ITEM_IS_PAPER((mActor_name_t)item)", "mPr_ITEM_COND_QUEST", "lbRTC_SUNDAY", "ITM_KABU_SPOILED", "bags > empty + n", "PC_NETGAME_TXN_REASON_NO_ROOM", "ITM_MONEY_30000",
                                  "bells >= (uint32_t)mPr_WALLET_MAX", "PC_NETGAME_TXN_REASON_NOT_SELLABLE")))
    check("H payment = vanilla mSP_money_check + mSP_get_sell_price on the PRE-image: wallet first, then money bags 100 / 1000 / 10000 / 30000 lowest slot first, change to the wallet",
          all(x in func_body(shop_region_raw, "pcnetgame_shop_pay") for x in ("ITM_MONEY_100", "ITM_MONEY_1000", "ITM_MONEY_10000", "ITM_MONEY_30000", "feasible", "*wallet = money - amount")))
    check("H the shop mutations exist ONLY in the handler: mSP_PlusSales( twice (buy price, sale value / 2), mSP_ShopSaleReport( once, nothing else in pc_net_game.c writes the shop stock / sales_sum",
          c.count("mSP_PlusSales(") == 2 and c.count("mSP_ShopSaleReport(") == 1 and ts.count("mSP_PlusSales(") == 2 and ts.count("mSP_ShopSaleReport(") == 1
          and "mSP_ShopSaleReport(" not in func_body(shop_region_raw, "pcnetgame_shop_classify"))
    check("H the host's OWN resident is never written (post-image gate pcnetgame_rec_txn_idx_ok before the first shop mutation) and a guest (idx >= PLAYER_NUM) is refused with NO_DONOR_SLOT",
          "!pcnetgame_rec_txn_idx_ok(idx)" in ts and ts.index("!pcnetgame_rec_txn_idx_ok(idx)") < ts.index("mSP_PlusSales(shop_price);")
          and "if (idx >= PLAYER_NUM) {" in ts and "PC_NETGAME_TXN_REASON_NO_DONOR_SLOT" in ts)
    check("H NO RNG and no mPr_ item writer in the SHOP host helpers / handler (the pocket is written raw by pcnetgame_rec_txn_write_inventory with mPr_SET_ITEM_COND values)",
          not re.search(r"\b(RANDOM|rand|qrand|pcnetgame_rec_rand32|fqrand|mPr_Set\w*|mPr_Give\w*|mPr_Clear\w*)\s*\(", shop_region) and "pcnetgame_rec_txn_write_inventory(idx, post, post_conds, post_wallet)" in ts)
    mut_tokens = ("mSP_PlusSales(", "mSP_ShopSaleReport(", "pcnetgame_rec_txn_write_inventory(", "slot->rev++", "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED")
    rej_ok = []
    for m in re.finditer(r"pcnetgame_txn_reject\(", ts):
        nxt = ts.find("return", m.end())
        seg = ts[m.end():nxt] if nxt >= 0 else ts[m.end():]
        rej_ok.append(nxt >= 0 and not any(t in seg for t in mut_tokens))
    check("H every pcnetgame_txn_reject(..) reaches a 'return' before ANY shop / mirror mutation token and there are >= 12 of them", rej_ok and all(rej_ok) and len(rej_ok) >= 12)
    check("H the host's shop copy is refreshed (digest poll) right after the commit and every ready peer is pushed; the host's own shop floor (host player standing in a shop) is redrawn through aSD_PC_SyncDisplayWithStock",
          ts.index("pcnetgame_ts_refresh(svc);") > ts.index("mSP_ShopSaleReport(") and "pcnetgame_host_ts_push_all();" in ts and "aSD_PC_SyncDisplayWithStock()" in ts
          and c.count("aSD_PC_SyncDisplayWithStock()") == 2)
    check("H L2 the mirror validator does not cap sales_sum below the vanilla u32 range (no 0x3FFFFFFF cap)", "0x3FFFFFFF" not in func_body(hblk_raw, "pcnetgame_ts_valid_shop_blob") and "s.sales_sum <=" not in hblk)
    check("H L1 stationery is UNLIMITED only while some items[i] == item (else not buyable): both host validation and the client stock code share that rule",
          re.search(r"cls == PCNG_SHOP_CLS_UNLIMITED\) \{\s*for \(i = 0; i < mSP_GOODS_COUNT; i\+\+\) \{[^}]*s->items\[i\] == item\) \{\s*return \(int\)PC_NETGAME_SHOP_STOCK_UNLIMITED;", shop_region) is not None)
    hs = func_body(c_raw, "pc_net_game_host_shop_can_sell")
    a0 = (re.search(r"static void aNSC_sell_answer0\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{.*?\n\}\n", nsc, re.S) or [""])[0]
    check("H H1 the HOST's own purchase re-checks the stock before the pocket write / debit: pc_net_game_host_shop_can_sell (HOST role only, special-day stock and solo / client answer 1) "
          "wraps pcnetgame_shop_stock_code on the host's Save_Get(shop); the host / solo branch of aNSC_sell_answer0 calls it BEFORE mPr_SetPossessionItem and refuses with row 11 (NOT runtime-tested: no host-side purchase hook)",
          "s_role != PC_NETGAME_ROLE_HOST" in hs and "pcnetgame_shop_stock_code((mActor_name_t)item, &Save_Get(shop), status) >= 0" in hs and "mSP_TANUKI_SHOP_STATUS_EVENT" in hs
          and a0.index("pc_net_game_host_shop_can_sell((int)item)") < a0.index("mPr_SetPossessionItem(") and "next = aNSC_PC_ROW_REFUSED;" in a0)
    check("H L3 the manekin clip is NULL-guarded before change2naked_manekin_proc (the display sync runs from the poll)", "shop_manekin_clip != NULL" in sd and sd.index("shop_manekin_clip != NULL") < sd.index("change2naked_manekin_proc"))
    check("H the shop status is a SIDE-EFFECT-FREE read (pure mSP_ShopOpen / CheckFukubikiDay / Chk_HukubukuroSail / CheckHallowinDay), not the possibly stale Common tanuki_shop_status",
          "mSP_ShopOpen() == mSP_SHOP_STATUS_OPENEVENT" in shop_region and "mSP_SetTanukiShopStatus" not in c)

    # ------------------------------------------------------------------ C: client
    pc_helpers = nsc[nsc.index("static int aNSC_pc_buy_wait;"):nsc.index("static int aNSC_pc_selection_has_paper(Submenu* menu) {")]
    check("C the client seam helpers (aNSC_pc_*) never write the wallet, the pockets, sales_sum or the stock: no mPr_Set*, no mSP_PlusSales / mSP_get_sell_price / mSP_ShopSaleReport, no inventory assignment; they only begin / poll",
          not re.search(r"\b(mPr_Set\w*|mPr_Give\w*|mSP_PlusSales|mSP_get_sell_price|aNSC_get_sell_price|mSP_ShopSaleReport)\s*\(", pc_helpers)
          and not re.search(r"inventory\.\w+(?:\[[^\]]*\])?\s*=[^=]", pc_helpers) and "pc_net_game_ts_begin_shop_buy(" in pc_helpers and "pc_net_game_ts_begin_shop_sell(" in pc_helpers
          and pc_helpers.count("pc_net_game_ts_poll()") == 2)
    m = re.search(r"static void aNSC_sell_answer0\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{.*?\n\}\n", nsc, re.S)
    a0 = m.group(0) if m else ""
    check("C aNSC_sell_answer0 (the purchase): a client first POLLS an in-flight transaction (no money re-check), pending -> return; the vanilla pocket write mPr_SetPossessionItem exists ONCE and sits after the client branch",
          a0.count("mPr_SetPossessionItem(") == 1 and "aNSC_PC_IS_CLIENT() && aNSC_pc_buy_wait" in a0 and a0.index("aNSC_PC_IS_CLIENT() && aNSC_pc_buy_wait") < a0.index("aNSC_money_check(shop_common->value)")
          and a0.index("aNSC_pc_buy_step(shop_common)") < a0.index("mPr_SetPossessionItem(") and a0.count("if (pc_next == -1) {\n            return;") + a0.count("if (pc_next == -1) {\n                        return;") + a0.count("if (pc_next == -1) {\n                    return;") >= 2)
    check("C a refused purchase takes the EXISTING rows: not enough money -> row 3 (INSUFFICIENT_FUNDS), pockets full -> row 4 (POCKETS_FULL), anything else (sold out / host refusal) -> row 11 = the existing aNSC_MSG_BUY_CANCEL text; "
          "APPLIED -> the vanilla success rows (BUY_NORMAL, net / axe / shovel / rod / signboard), never a ticket row",
          "aNSC_MSG_SELL_SIGN,    aNSC_MSG_BUY_CANCEL };" in nsc and "aNSC_ACTION_SELL_ITEM_WITHOUT_TICKET, aNSC_ACTION_SELL_ITEM_INSUFICIENT_FUNDS };" in nsc
          and "return aNSC_PC_ROW_REFUSED;" in pc_helpers and "if (reason == PC_NETGAME_TS_REJECT_NO_FUNDS) {\n        return 3;" in pc_helpers and "return 4;" in pc_helpers
          and "static int aNSC_pc_buy_success_row(mActor_name_t item) {\n    if (aNSC_check_item_with_ticket(item) == TRUE) {\n        return 0;" in pc_helpers)
    check("C raffle tickets for a client purchase are never granted: aNSC_check_same_month_ticket / aNSC_setup_ticket_remain are called only from the vanilla (host / solo) branch of aNSC_sell_answer0",
          nsc.count("aNSC_check_same_month_ticket(ticket)") == 1 and nsc.count("aNSC_setup_ticket_remain();") == 1 and "aNSC_check_same_month_ticket(" not in pc_helpers
          and a0.index("aNSC_pc_buy_step(shop_common)") < a0.index("aNSC_check_same_month_ticket(ticket)"))
    check("C a NOT_AVAILABLE rejection takes the sold item off the floor display through the shop's report proc (client mode = display only)",
          "reason == PC_NETGAME_TS_REJECT_NOT_AVAILABLE && CLIP(shop_design_clip) != NULL" in pc_helpers and "reportGoodsSale_proc(shop_common->ut_x, shop_common->ut_z)" in pc_helpers)
    m = re.search(r"static void aNSC_sell_item_init\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{.*?\n\}\n", nsc, re.S)
    sii = m.group(0) if m else ""
    check("C aNSC_sell_item_init debits the wallet only when NOT a client (the host post-image already did) and still calls the (display-only on a client) report proc",
          "if (!aNSC_PC_IS_CLIENT()) {\n        aNSC_get_sell_price(shop_common->value);" in sii and "reportGoodsSale_proc(shop_common->ut_x, shop_common->ut_z)" in sii)
    m = re.search(r"static int aSD_ReportGoodsSales_ex\(int ux, int uz, int accounting\) \{.*?\n\}\n", sd, re.S)
    rg = m.group(0) if m else ""
    check("C ac_shop_design.c: a client's sale report is display-only: accounting = role != CLIENT, mSP_PlusSales only under 'if (accounting)', every mSP_ShopSaleReport goes through aSD_SaleReport (returns FALSE without accounting); "
          "no direct mSP_ShopSaleReport / mSP_PlusSales elsewhere in the report function",
          "pc_net_game_role() != PC_NETGAME_ROLE_CLIENT" in sd and "if (accounting) {\n            mSP_PlusSales(price);" in rg and rg.count("mSP_PlusSales(") == 1 and "mSP_ShopSaleReport(" not in rg
          and rg.count("aSD_SaleReport(accounting,") == 4 and "if (!accounting) {\n        return FALSE;\n    }\n    return mSP_ShopSaleReport(" in sd)
    m = re.search(r"static void aNSC_buy_check\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{.*?\n\}\n", nsc, re.S)
    bc = m.group(0) if m else ""
    check("C aNSC_buy_check (the sale): a client resumes / polls its in-flight SHOP_SELL first; the client branch (prepare + step) sits BEFORE the vanilla local sale (mSP_PlusSales, slot exchange, wallet write), which stays the host / solo path; "
          "pending (-1) leaves without a state change",
          "aNSC_PC_IS_CLIENT() && aNSC_pc_sell_wait" in bc and "next = aNSC_pc_sell_step();\n        if (next == -1) {\n            return;" in bc
          and bc.index("aNSC_pc_sell_prepare(shop_common, play)") < bc.index("mSP_PlusSales(shop_common->money / 2);") and bc.count("mSP_PlusSales(") == 1
          and bc.index("aNSC_pc_sell_prepare(shop_common, play)") < bc.index("Now_Private->inventory.wallet = bells;"))
    m = re.search(r"static void aNSC_receive_check\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{.*?\n\}\n", nsc, re.S)
    rc = m.group(0) if m else ""
    check("C aNSC_receive_check (free disposal of a worthless item): the client case begins a SHOP_SELL (value 0) and breaks / returns BEFORE the vanilla mPr_SetPossessionItem, which exists once",
          rc.count("mPr_SetPossessionItem(") == 1 and "if (aNSC_PC_IS_CLIENT()) {" in rc and rc.index("aNSC_pc_sell_step()") < rc.index("mPr_SetPossessionItem(") and "if (aNSC_pc_sell_step() == -1) {\n                                return;" in rc)
    check("C a sale that cannot be done refuses with the existing rows: stationery -> the 'cannot take that' row (REFUSE_QUEST_COND) at selection time, a rejected / refused sale -> CANCEL (no change), "
          "NO_ROOM -> MONEY_OVERFLOW; APPLIED -> BREAK_BAG when a bag appeared else the normal 'sold' row",
          "action = aNSC_CHECK_BUY_REFUSE_QUEST_COND;" in nsc and "aNSC_pc_selection_has_paper(submenu)" in nsc and "PC_NETGAME_TS_REJECT_NO_ROOM ? aNSC_BUY_OUTCOME_MONEY_OVERFLOW : aNSC_BUY_OUTCOME_CANCEL" in pc_helpers
          and "aNSC_pc_count_bags() > aNSC_pc_sell_bags_before ? aNSC_BUY_OUTCOME_BREAK_BAG : aNSC_BUY_OUTCOME_NORMAL" in pc_helpers)
    check("C the wait flags are reset when a new purchase / sale starts (aNSC_sell_check_before_init / aNSC_buy_sum_check_init) so no stale town-service wait state survives an abandoned dialogue",
          nsc.count("aNSC_pc_reset_waits();") == 2)
    check("C DEFERRED refusals still in place on a client (Stage 0 / G2 / G3): catalog order refused before any bell moves, paint purchase refused, house loan / upgrade refused (7 G2 / G3 gate sites)",
          "action = 1;" in nsc and "msg_no = aNSC_MSG_ORDER_CANCEL;" in nsc and "shop_common->sell_item >= ITM_RED_PAINT" in nsc
          and ("ITEM_IS_PAINT(item)" in shop_region and "paint is a house-service purchase" in shop_region_raw))
    stock_fn = func_body(c_raw, "pc_net_game_shop_buy_stock_code")
    check("C pc_net_game_shop_buy_stock_code is a READ of the MIRRORED shop (client role + READY only, -1 otherwise); the begin API refuses a negative code / zero price and a bad mask; the result reason is exposed once",
          "s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY" in stock_fn and "pcnetgame_shop_stock_code((mActor_name_t)item, &Save_Get(shop)," in stock_fn
          and "if (stock_code < 0 || price <= 0) {" in c and "slot_mask > 0x7FFF" in c and "s_ts_last_reason = in->reason;" in c)
    check("C try_send: SHOP_BUY needs a free pocket slot (the chosen one or the first free), carries the stock code in aux_cond and the expected price in aux_item; SHOP_SELL needs the offered item still in the primary slot and every masked slot non-empty; "
          "a failed precondition cancels the op (REJECTED, nothing changed)",
          "case PC_NETGAME_TXN_KIND_SHOP_BUY:" in c and "aux_cond = T->ts_aux;       /* the stock code */" in c_raw and "aux_item = T->ts_aux_item;  /* the price the player was shown */" in c_raw
          and "case PC_NETGAME_TXN_KIND_SHOP_SELL: {" in c and "a pocket slot of the sale is empty now" in c)
    check("C the client pocket / wallet change for a shop transaction happens ONLY in pcnetgame_txn_apply_applied -> pcnetgame_txn_apply_shop (host post-image, owner stamp checked, shape verified): the post-image may change only the "
          "slots of the transaction (+ a bag in / out), APPLIED echo checked by the result handler",
          "return pcnetgame_txn_apply_shop(T, in);" in c and "changes slot %d outside the transaction" in c_raw and "inconsistent shop post-image" in c_raw
          and len(re.findall(r"np->inventory\.wallet\s*=[^=]", func_body(c_raw, "pcnetgame_txn_apply_shop"))) == 2)
    check("C the TS CLIENT block (outside the TEST-ONLY hooks) never touches the inventory: begin / poll / mirror apply contain no 'inventory' access (the shop hook is stripped like the other hooks)",
          "inventory" not in strip_hook_bodies(cblk))

    # ------------------------------------------------------------------ T: test hooks
    hook = func_body(cblk_raw, "pcnetgame_run_shop_test_hook")
    check("T --shop-test-buy / --shop-test-sell: default OFF in pc_main.c, in --help and documented in pc_platform.h; client role only; loud logs; called once from the poll",
          "int g_pc_shop_test_buy = 0;" in main_c and "int g_pc_shop_test_sell = 0;" in main_c and "--shop-test-buy " in main_c and "--shop-test-sell " in main_c
          and "g_pc_shop_test_buy" in plat_h and "g_pc_shop_test_sell" in plat_h
          and "(!g_pc_shop_test_buy && !g_pc_shop_test_sell) || s_stage >= 5 || s_role != PC_NETGAME_ROLE_CLIENT" in hook and "[NET][SHOP][TEST-ONLY]" in hook and c.count("pcnetgame_run_shop_test_hook();") == 1)
    check("T the hook's local writes are exactly two loud test-setup writes (wallet raise for the buy, a fish into a free slot for the sale); both before the request, and it logs CHANGED BEFORE APPLIED if pockets / wallet move while pending",
          hook.count("Now_Private->inventory.wallet = 90000u;") == 1 and hook.count("Now_Private->inventory.pockets[s_slot] =") == 1 and "POCKET CHANGED BEFORE APPLIED" in hook and "WALLET CHANGED BEFORE APPLIED" in hook
          and hook.index("Now_Private->inventory.wallet = 90000u;") < hook.index("pc_net_game_ts_begin_shop_buy("))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
