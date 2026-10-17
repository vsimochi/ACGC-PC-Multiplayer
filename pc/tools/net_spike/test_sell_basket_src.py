#!/usr/bin/env python3
"""test_sell_basket_src.py - Nook bulk sell basket (docs/multiplayer-guest-roadmap.md "Nook bulk sell basket"): SOURCE audit.

  P  one shared predicate: pc_sell_refusal() (pc_sell_logic.c) is THE rule of the host's SHOP_SELL validation AND of the basket screen (offer / greyed / put); it
     encodes the vanilla sell filter (m_submenu.c mSM_check_item_for_sell) + the dialogue's quest / Sunday-turnip refusals + the network stationery rule
  W  NO wire change: no new TXN kind / town service / message id / reason; the basket's sale is the EXISTING SHOP_SELL (kind 11) slot mask the host already validated
     slot by slot (all or nothing); the client reaches it only through pc_net_game_ts_begin_shop_sell (old hosts of protocol 8 answer it the same way)
  C  conservation: the basket is a LOCAL staging view: pc_sell_ui.c / the basket branches of pc_dresser_ui.c never write a pocket / the wallet / send anything (outside
     the TEST-ONLY setup write); the pockets change only by the host's APPLIED post-image (client) or the vanilla local sale (listen host)
  N  the Nook seam: the sell menu opens the basket (HANIWA_ENTRUST + basket mode) only when pc_sell_ui_enabled(); its close fills the vanilla multi-item selection; the
     quote row (count + total, Yes / No) is read by aNSC_buy_check like the vanilla offer; "No" keeps the basket; the vanilla picker stays for single player / off
  U  the screen: 5 x 3 grid in the dresser window, "Add to Basket" / "Take Back" tags, no Grab, staged pockets hidden + nameless, greyed pockets refused with a notice,
     the inventory's selectable mask = the predicate, the summary lines, the stale-basket guard at the overlay construct
  K  switches: [Network] bulk_sell (default 1, parsed, written), env AC_MP_BULK_SELL=0, session roles only; the TEST-ONLY hook is default-off and compiled only with
     PC_NET_TEST_HOOKS
TIER: SOURCE (no game process). Usage: python test_sell_basket_src.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
results = []


def rd(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8", errors="replace") as f:
        return f.read()


def ck(desc, cond):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def func_body(src, sig):
    i = src.find(sig)
    if i < 0:
        return ""
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    return ""


def main():
    ng = rd("pc", "src", "pc_net_game.c")
    logic = rd("pc", "src", "pc_sell_logic.c")
    logic_h = rd("pc", "include", "pc_sell_logic.h")
    ui = rd("pc", "src", "pc_sell_ui.c")
    ui_h = rd("pc", "include", "pc_sell_ui.h")
    dui = rd("pc", "src", "pc_dresser_ui.c")
    sm = rd("src", "game", "m_submenu.c")
    nsc = rd("src", "actor", "npc", "ac_npc_shop_common.c")
    tg = rd("src", "game", "m_tag_ovl.c")
    hw = rd("src", "game", "m_haniwa_ovl.c")
    iv = rd("src", "game", "m_inventory_ovl.c")
    nkh = rd("pc", "include", "pc_nook_house.h")
    nk = rd("pc", "src", "pc_nook_house.c")
    st = rd("pc", "src", "pc_settings.c")
    sth = rd("pc", "include", "pc_settings.h")
    pm = rd("pc", "src", "pc_pause_menu.c")
    cm = rd("pc", "CMakeLists.txt")

    # ---------------- P: one predicate
    unit = func_body(ng, "static int pcnetgame_shop_sell_unit(")
    ck("P1 the host's SHOP_SELL value / refusal is pc_sell_refusal() + pc_sell_unit_value() with the HOST's prices (mSP_ItemNo2ItemPrice, Kabu_get_price, the host's weekday)",
       "pc_sell_refusal((unsigned)item, (int)cond, Common_Get(time).rtc_time.weekday == lbRTC_SUNDAY)" in unit
       and "pc_sell_unit_value((unsigned)item, (unsigned)mSP_ItemNo2ItemPrice((mActor_name_t)item), (unsigned)Kabu_get_price())" in unit
       and "fail_reason = pcnetgame_shop_sell_plan(t->pre_pockets" in ng and "pcnetgame_shop_sell_unit(pre[i]" in ng)
    ck("P2 the basket screen uses the SAME predicate for the offer, the greyed mask, the put and the notice (pc_sell_ui_refusal_of -> pc_sell_refusal)",
       "return pc_sell_refusal((unsigned)Now_Private->inventory.pockets[slot]" in ui and ui.count("pc_sell_ui_refusal_of(") >= 5
       and "pc_sell_ui_refusal_of(slot) == PC_SELL_OK" in func_body(ui, "int pc_sell_ui_pocket_offer(") and "pc_sell_ui_refusal_of(i) == PC_SELL_OK" in func_body(ui, "unsigned pc_sell_ui_selectable_mask(")
       and "ref = pc_sell_ui_refusal_of(slot);" in func_body(ui, "int pc_sell_ui_put("))
    sub = rd("src", "game", "m_submenu.c")
    van = func_body(sub, "static int mSM_check_item_for_sell(")
    ref = strip_comments(func_body(logic, "int pc_sell_refusal("))
    ck("P3 the predicate encodes the vanilla sell filter of m_submenu.c (NORMAL condition, no money / exercise cards / knife and fork / Wisp spirits) + quest + Sunday turnips + stationery + unknown ids",
       all(x in van for x in ("mPr_ITEM_COND_NORMAL", "ITEM1_CAT_MONEY", "ITM_EXCERCISE_CARD00", "ITM_EXCERCISE_CARD12", "ITM_KNIFE_AND_FORK", "ITM_SPIRIT0", "ITM_SPIRIT4"))
       and all(x in ref for x in ("cond == PC_SELL_COND_QUEST", "cond != PC_SELL_COND_NORMAL", "ITEM1_CAT_MONEY", "ITM_EXCERCISE_CARD00", "ITM_EXCERCISE_CARD12",
                                  "ITM_KNIFE_AND_FORK", "ITEM_IS_WISP(it)", "it != ITM_KABU_SPOILED && is_sunday", "ITEM_IS_PAPER(it)", "!pc_sell_known(item)")))
    ck("P4 the sale value = vanilla aNSC_buy_check_init (price / 4, turnips kabu x 10 / 50 / 100, spoiled 0) and the constants are pinned to the game's (static asserts)",
       "static const unsigned kabu_sum[4] = { 10u, 50u, 100u, 0u };" in logic and "return item_price / PC_SELL_RATIO;" in logic and "#define PC_SELL_RATIO 4" in logic_h
       and "PC_SELL_RATIO == PC_NETGAME_SHOP_SELL_RATIO" in ng and "PC_SELL_COND_QUEST == mPr_ITEM_COND_QUEST" in ui and "PC_SELL_BASKET_CELLS == mPr_POCKETS_SLOT_COUNT" in ui
       and "value += (mSP_ItemNo2ItemPrice(item) / SELL_BUY_RATIO) * counter;" in nsc and "static int kabu_sum[4] = { 10, 50, 100, 0 };" in nsc)
    ck("P5 pc_sell_logic.c calls no game function (item-id macros only: the native selftest links it alone)",
       not re.search(r"\b(mSP_|Kabu_|Common_Get|Save_Get|Now_Private|mPr_Set|printf\s*\(\s*\"\[NET)", strip_comments(logic).split("int pc_sell_logic_selftest")[0]))

    # ---------------- W: no wire change
    ck("W1 no new transaction kind / reason / town service: the last kind is still 22 (OUTDOOR_FURN), the last reason 61, PC_NETGAME_TS_NUM 11; no SELL_MULTI",
       "#define PC_NETGAME_TXN_KIND_OUTDOOR_FURN 22u" in ng and not re.search(r"#define PC_NETGAME_TXN_KIND_\w+\s+23u", ng) and "SELL_MULTI" not in ng
       and not re.search(r"#define PC_NETGAME_TXN_REASON_\w+\s+6[2-9]u", ng) and re.search(r"#define PC_NETGAME_TS_NUM\s+11u", ng))
    shape = ng[ng.index("} else if (is_sell) {\n            shape_ok ="):][:400]
    ck("W2 the host's SHOP_SELL shape already accepts any slot mask of mixed items (only the primary slot's bit and bits 0..14 are required) and values EVERY masked slot (all or nothing)",
       "(t->aux_item & 0x8000u) == 0" in shape and "(t->aux_item & (1u << t->slot)) != 0" in shape
       and "if (mask & (1u << i)) {" in func_body(ng, "static uint8_t pcnetgame_shop_sell_plan(") and "return (uint8_t)PC_NETGAME_TXN_REASON_NOT_SELLABLE;" in func_body(ng, "static uint8_t pcnetgame_shop_sell_plan("))
    ck("W3 the client's sale is the existing seam: aNSC_pc_sell_prepare turns a multi-item selection into the slot mask -> pc_net_game_ts_begin_shop_sell (kind 11); the basket code never begins a transaction itself",
       "if (play->submenu.selected_item_num > 1) {" in func_body(nsc, "static int aNSC_pc_sell_prepare(") and "pc_net_game_ts_begin_shop_sell((int)aNSC_pc_sell_mask" in nsc
       and "pc_net_game_ts_begin" not in strip_comments(ui).split("void pc_sell_ui_test_tick(void)")[0] and "pc_net_game_dresser_begin" not in strip_comments(func_body(dui, "int pc_dresser_ui_put(")).split("if (!s_active")[0])

    # ---------------- C: conservation
    ui_prod = re.sub(r"#ifdef PC_NET_TEST_HOOKS.*?#endif", "", strip_comments(ui), flags=re.S)
    ck("C1 pc_sell_ui.c (outside the TEST-ONLY hook) never writes a pocket, a condition or the wallet",
       not re.search(r"inventory\.(pockets\[[^\]]*\]|wallet|item_conditions)\s*=[^=]", ui_prod) and "mPr_SetPossessionItem" not in ui_prod and "mPr_SetFreePossessionItem" not in ui_prod)
    ck("C2 the basket branches of the dresser screen only stage locally (take / put / slot item / hidden pockets delegate to pc_sell_ui; no Grab in the basket)",
       "return s_active ? pc_sell_ui_take(col) : PC_DRESSER_UI_ACT_REFUSED;" in dui and "return s_active ? pc_sell_ui_put(pocket_slot) : PC_DRESSER_UI_ACT_REFUSED;" in dui
       and "return s_active ? pc_sell_ui_cell_item(col) : 0;" in dui and "return from_pocket && pc_sell_ui_pocket_hidden(index);" in dui and "s_pending || item == 0 || s_basket)" in dui)
    ck("C3 the close writes ONLY the vanilla selection (Submenu item_p / selected_item_num) and leaves basket mode",
       all(x in func_body(ui, "int pc_sell_ui_close(") for x in ("it[k].item = (mActor_name_t)items[k];", "it[k].slot_no = slots[k];", "submenu->selected_item_num = n;", "pc_dresser_ui_end();")))

    # ---------------- N: the Nook seam
    op = func_body(nsc, "static void aNSC_buy_menu_close_wait_init(")
    ck("N1 the sell menu opens the basket (pc_sell_ui_open + the inventory in HANIWA_ENTRUST mode) only when pc_sell_ui_enabled(); otherwise the vanilla sell picker (mode 0x5)",
       "if (pc_sell_ui_enabled()) {" in op and "pc_sell_ui_open();" in op and "mSM_open_submenu(&play->submenu, mSM_OVL_INVENTORY, mSM_IV_OPEN_HANIWA_ENTRUST, 0x0);" in op
       and "mSM_open_submenu(&play->submenu, mSM_OVL_INVENTORY, 0x5, 0x0);" in op)
    ck("N2 the menu close collects the basket into the vanilla selection before the next action",
       "(void)pc_sell_ui_close(&play->submenu);" in func_body(nsc, "static void aNSC_buy_menu_close_wait("))
    mw = func_body(nsc, "static void aNSC_msg_win_open_wait(")
    ck("N3 a basket selection takes the quote row PC_NOOK_MSG_SELL_BASKET (count + total), counter = the count (plural path, also for one item), next = BUY_CHECK",
       "if (pc_sell_ui_take_from_basket() && item != EMPTY_NO" in mw and "(void)aNSC_check_buy_item_plural(shop_common, submenu);" in mw
       and "pc_nook_house_set_sell(pc_sell_ui_last_count(), pc_sell_ui_last_total());" in mw and "msg_no = pc_nook_msg_id(PC_NOOK_MSG_SELL_BASKET);" in mw
       and "aNSC_setupAction(shop_common, play, aNSC_ACTION_BUY_CHECK);" in mw)
    bc = func_body(nsc, "static void aNSC_buy_check(NPC_SHOP_COMMON_ACTOR* shop_common, GAME_PLAY* play) {")
    ck("N4 aNSC_buy_check reads the quote's Yes / No like the vanilla offer (the sale / the network seam unchanged); No keeps the basket for the next open",
       "if (num == pc_nook_msg_id(PC_NOOK_MSG_SELL_BASKET)) {\n        num = n2;" in bc and "pc_sell_ui_keep_next();" in bc and "mChoice_CHOICE1" in bc
       and "next = aNSC_pc_sell_prepare(shop_common, play) ? aNSC_pc_sell_step() : aNSC_BUY_OUTCOME_CANCEL;" in bc)
    ck("N5 the quote row: appended LAST (index 52, count 53), Yes. / No., built without the 'choice made' demo order (aNSC_buy_after_service waits for that order after a sale)",
       "#define PC_NOOK_MSG_COUNT 53" in nkh and re.search(r"PC_NOOK_MSG_SELL_BASKET\s+/\*", nkh) and "case PC_NOOK_MSG_SELL_BASKET:" in nk
       and "nk_choice_plain(&b, ids, 2);" in nk and "NK_DEMO_NPC0" not in func_body(nk, "static void nk_choice_plain(")
       and "That's %d item%s in all." in nk and "pc_sell_format_bells(bells, sizeof(bells), s_sell_total)" in nk)

    # ---------------- U: the screen
    ck("U1 the basket grid: a 5 x 3 table in the dresser window (the left five dresser columns), cursor up from the pockets limited to its columns",
       "static mTG_tag_data_table_c mTG_pc_basket_table = { 5, 3, mTG_pc_dresser_col_pos, mTG_pc_dresser_line_pos };" in tg
       and "if (table == mTG_TABLE_HANIWA && pc_dresser_ui_basket()) {" in tg and "i < mTG_pc_table(mTG_TABLE_HANIWA)->col_num; i++" in tg)
    ck("U2 the tags: a sellable pocket -> 'Add to Basket' / Quit (mTG_putin_proc), a cell -> 'Take Back' / Quit (mTG_get_proc); a staged pocket offers nothing and has no name tag",
       '"Add to Basket   "' in tg and '"Take Back       "' in tg and "pc_sell_ui_pocket_offer(idx)) ? mTG_TYPE_PUTIN_ITEM : mTG_TYPE_NONE;" in tg
       and "mTG_label_table[mTG_TYPE_HANIWA_GET_ITEM].words = mTG_pc_basket_get_item;" in tg and "pc_dresser_ui_basket() && pc_sell_ui_pocket_hidden(idx)" in tg)
    ck("U3 A on a greyed pocket says why (pc_sell_ui_refuse_pocket -> the notice, drawn by pc_net_notice_draw), nothing changes",
       "pc_sell_ui_refuse_pocket(mTG_get_table_idx(tag));" in tg and "const char* sell_line = pc_sell_ui_notice();" in pm and "pc_sell_refusal_text(ref)" in func_body(ui, "void pc_sell_ui_refuse_pocket("))
    ck("U4 the pocket grid: greyed = not sellable (selectable mask = the predicate), staged pockets hidden (carry_hides in basket mode)",
       "inv_ovl->selectable_item_bitfield = pc_sell_ui_selectable_mask();" in iv and "pc_dresser_ui_carry_hides(1, i)" in iv)
    ck("U5 the window: the dresser's flat solid panel and cells (5 cells per row in basket mode) + the summary lines ('N items' / total / 'Bells') right of the grid",
       "const int ncols = pc_dresser_ui_basket() ? PC_SELL_BASKET_COLS : PC_DRESSER_UI_COLS;" in hw and "mHW_pc_basket_summary(submenu, game, menu_info);" in hw
       and "pc_sell_ui_header(line[0]" in hw)
    ck("U6 a basket never collected (its conversation ended) is dropped at the next gyroid overlay construct (never shown on a real gyroid / dresser)",
       "pc_sell_ui_overlay_constructed();" in hw and "pc_sell_ui_overlay_destroyed();" in hw and "if (s_open && s_destroyed) {" in ui and "s_basket = 0;" in func_body(dui, "void pc_dresser_ui_begin("))

    # ---------------- K: switches
    en = func_body(ui, "int pc_sell_ui_enabled(")
    ck("K1 enabled only in a session (role HOST / CLIENT: single player keeps the vanilla picker), bulk_sell setting and env AC_MP_BULK_SELL=0",
       "role == PC_NETGAME_ROLE_HOST || role == PC_NETGAME_ROLE_CLIENT" in en and "g_pc_settings.bulk_sell != 0" in en and 'getenv("AC_MP_BULK_SELL")' in en)
    ck("K2 [Network] bulk_sell: default 1, parsed (0 / 1), written with a comment",
       ".bulk_sell = 1," in st and 'strcmp(key, "bulk_sell") == 0' in st and 'fprintf(f, "bulk_sell = %d\\n", g_pc_settings.bulk_sell);' in st and "int bulk_sell;" in sth)
    ck("K3 the real-game test hook is TEST-ONLY: compiled only with PC_NET_TEST_HOOKS (pc_sell_ui.c is in that CMake list), runtime-gated by AC_TEST_SELL_BASKET, client role",
       '#ifdef PC_NET_TEST_HOOKS' in func_body(ui, "void pc_sell_ui_test_tick(") and 'pc_test_hook_getenv("AC_TEST_SELL_BASKET")' in ui
       and re.search(r"src/pc_sell_ui\.c # Nook bulk sell basket: AC_TEST_SELL_BASKET[^\n]*\n(?:\s*\$\{[^\n]*\n)*\s*PROPERTIES COMPILE_DEFINITIONS PC_NET_TEST_HOOKS", cm)
       and "int g_pc_th_submenu_data0 = 0;" in sm and "pc_sell_ui_test_tick();" in ng)
    ck("K4 both new sources are built (CMake PC_SOURCES)", "src/pc_sell_logic.c" in cm and "src/pc_sell_ui.c      # Nook bulk sell basket" in cm and "#ifndef PC_SELL_UI_H" in ui_h)

    print("-" * 60)
    print("%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
