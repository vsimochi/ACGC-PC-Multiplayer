#!/usr/bin/env python3
"""test_g4_station_exchange.py - M9-D Group 4 source-audit checks (NO game process is launched).

  G4-1  mCD_SaveStation_NextLand_bg (pc/src/pc_m_card.c): a network CLIENT returns an error (no write, no
        keep load, no state change) before any save write; Passport_bg writes nothing.
  G4-2  mTG_exchange_proc (src/game/m_tag_ovl.c): a client never reaches mTG_common_throw_put_field for the
        swapped-out hand item; it goes through pc_net_game_exchange_request_drop (pc_net_game.c), whose
        deferred record is consumed by the matching accepted DROP_RESULT. X1b: that handler now begins a
        host-transactional TXN_COMMIT carrying the record, and the replacement is written (after the normal slot
        clear) only by pcnetgame_txn_apply_applied when the host's TXN_RESULT(APPLIED) arrives; the record is
        cleared on session reset.

Runtime behavior of these paths is NOT TESTED (no hook reaches the train station save talk or the catch
exchange menu on a real client).
Usage: python3 test_g4_station_exchange.py
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


def main():
    results = []

    # ---- G4-1
    card = read("pc/src/pc_m_card.c")
    nl = func_body(card, r"\nint mCD_SaveStation_NextLand_bg\(s32\* chan\) \{")
    check("G4-1: mCD_SaveStation_NextLand_bg found", bool(nl), results)
    g = nl.find("if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {")
    check("G4-1: client role guard present", g >= 0, results)
    writes = [nl.find(s) for s in ("pc_save_write_gci_to(", "pc_save_write_gci()", "pc_save_read_gci_to_keep(",
                                    "l_mcd_keep_startCond =", "l_mcd_foreigner_file", "Now_Private->exists",
                                    "travel_persistent_data")]
    check("G4-1: no write / keep load / state mutation before the guard",
          g >= 0 and all(w > g for w in writes if w >= 0) and all(w >= 0 for w in writes), results)
    blk = nl[g:nl.find("\n    }\n", g)] if g >= 0 else ""
    check("G4-1: client branch returns an error code (not NONE) and sets chan",
          "return mCD_TRANS_ERR_NO_TOWN_DATA;" in blk and "mCD_TRANS_ERR_NONE;" not in blk and "*chan" in blk, results)
    check("G4-1: limited-count log", "s_client_station_skip_logged < 3" in blk, results)
    check("G4-1: guard is the first statement after the local declaration",
          nl.find("int is_foreigner = mLd_PlayerManKindCheck();") < g < nl.find("is_foreigner)"), results)
    pp = func_body(card, r"\nint mCD_SaveStation_Passport_bg\(s32\* chan\) \{")
    check("G4-1: Passport_bg writes no file",
          "pc_save_write" not in pp and "fopen" not in pp, results)
    stn = read("src/actor/ac_station_clip.c_inc")
    check("G4-1: aSTM_save_talk default branch handles NO_TOWN_DATA as a talk-ending save error",
          "case mCD_TRANS_ERR_NO_TOWN_DATA:" in stn and "mMsg_Set_ForceNext(msg_p);" in stn, results)
    save_hm = func_body(card, r"\nint mCD_SaveHome_bg\(int param_1, int\* chan\) \{")
    check("G4-1: F1 SaveHome guard still present before pre-write side effects",
          save_hm.find("PC_NETGAME_ROLE_CLIENT") < save_hm.find("pc_save_pre_write_side_effects"), results)

    # ---- G4-2
    tag = read("src/game/m_tag_ovl.c")
    ex = func_body(tag, r"\nstatic void mTG_exchange_proc\(Submenu\* submenu, mSM_MenuInfo_c\* menu_info\) \{")
    check("G4-2: mTG_exchange_proc found", bool(ex), results)
    bal = ex.find("else if (ITEM_IS_BALLOON(item)) {")
    cg = ex.find("else if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {")
    thr = ex.find("mTG_common_throw_put_field(")
    req = ex.find("pc_net_game_exchange_request_drop(")
    check("G4-2: creature branches precede the client branch, which precedes the local field write",
          0 <= bal < cg < thr and cg < req < thr, results)
    cend = ex.find("\n#endif", cg)
    cblk = re.sub(r"/\*.*?\*/", "", ex[cg:cend], flags=re.S) if 0 <= cg < cend < thr else ""
    check("G4-2: client branch has no field write / local put / warning window",
          "mTG_common_throw_put_field" not in cblk and "mTG_host_put_tile_free" not in cblk
          and "mTG_open_warning_window" not in cblk and "return;" not in cblk, results)
    check("G4-2: client branch uses the same tile search arguments as the field-put seam",
          "mTG_search_put_pos(player, &pos, FALSE, FALSE, FALSE, FALSE, FALSE)" in cblk, results)
    check("G4-2: client branch still ends in the vanilla success tail (wait requests / golden demo)",
          "mPlib_request_main_wait_from_submenu();" in cblk
          and "mPlib_request_main_demo_get_golden_item_from_submenu();" in cblk, results)
    check("G4-2: creature release branches unchanged",
          "mPlib_request_main_release_creature_gyoei_from_submenu(" in ex[:bal] and "mTG_insect_release(item);" in ex[:bal],
          results)
    hand = read("src/game/m_hand_ovl.c")
    d2 = func_body(hand, r"\nstatic void mHD_drop_item2\(Submenu\* submenu, mTG_tag_c\* tag, mActor_name_t\* item, int idx\) \{")
    check("G4-2: mHD_drop_item2 notes the swap slot after mHD_drop_item (TARGET_PC)",
          "#ifdef TARGET_PC" in d2 and d2.find("mHD_drop_item(submenu, tag, item, NULL);") <
          d2.find("pc_net_game_exchange_note_swap("), results)

    net = read("pc/src/pc_net_game.c")
    fn = func_body(net, r"\nint pc_net_game_exchange_request_drop\(int hand_item, int hand_cond, int ut_x, int ut_z\) \{")
    check("G4-2: pc_net_game_exchange_request_drop found and client-only",
          bool(fn) and "s_role != PC_NETGAME_ROLE_CLIENT" in fn, results)
    setp = fn.find("mPr_SetPossessionItem(Now_Private, slot, (mActor_name_t)hand_item, hand_cond);")
    reqd = fn.find("pc_net_game_request_drop(slot, hand_item, ut_x, ut_z)")
    rec = fn.find("s_exchange_deferred.valid = 1;")
    check("G4-2: hand item restored to the slot before the claim; record set only after a request is in flight",
          0 <= setp < reqd < rec, results)
    check("G4-2: one-in-flight is a failure, never reuses another request's id",
          fn.find("s_drop_pending.valid)") < reqd and "another drop request is in flight" in fn, results)
    check("G4-2: record keyed by the new request id with owner stamp",
          "s_exchange_deferred.request_id = s_drop_pending.request_id;" in fn
          and "s_exchange_deferred.owner = s_drop_pending.owner;" in fn, results)
    check("G4-2: no local field write in the exchange API", "throw_put_field" not in fn, results)
    h = func_body(net, r"\nstatic void pcnetgame_handle_client_drop_result\(const PCNetGameDropResultMsg\* in\) \{")
    take = h.find("deferred = s_exchange_deferred;")
    consume = h.find("s_exchange_deferred.valid = 0;")
    rej = h.find("if (!in->accepted) {")
    check("G4-2: record taken and consumed at handler entry (before any reject/abort path)",
          0 <= take < consume < rej, results)
    check("G4-2 (X1b): the drop result handler writes NO pocket itself: the consumed record is handed to the transaction (have_deferred ? &deferred : NULL), "
          "once, after every validation, and there is no mPr_Set* / CONFIRM(COMMIT) in the handler",
          h.count("pcnetgame_txn_begin(") == 1 and "have_deferred ? &deferred : NULL" in h and "mPr_Set" not in h
          and "PC_NETGAME_CONFIRM_COMMIT" not in h and h.find("pcnetgame_txn_begin(") > h.find("SLOT_CHANGED"), results)
    ap = func_body(net, r"\nstatic void pcnetgame_txn_apply_applied\(const PCNetGameClientTxn\* T, const PCNetGameTxnResultMsg\* in\) \{")
    clr = ap.find("np->inventory.pockets[s] = (mActor_name_t)EMPTY_NO;")
    app = ap.find("np->inventory.pockets[s] = (mActor_name_t)t->aux_item;")
    ts = func_body(net, r"\nstatic int pcnetgame_txn_try_send\(void\) \{")
    check("G4-2 (X3, replaces the X1b check): the replacement travels IN the TXN_COMMIT tag (flags EXCHANGE + aux_item / aux_cond, set by try_send only when the "
          "exchange slot and owner stamp still match) and is written only in pcnetgame_txn_apply_applied (i.e. after the host APPLIED the drop): from the host "
          "post-image, or in the delta path as a RAW write after the slot clear -- mPr_SetPossessionItem is gone from the apply step",
          0 <= clr < app and "mPr_SetPossessionItem(" not in ap and "flags = (uint8_t)PC_NETGAME_TXN_FLAG_EXCHANGE;" in ts
          and "T->exch_slot == slot && pcnetgame_owner_stamp_matches(&T->exch_owner)" in ts and "aux_item = T->exch_item;" in ts and "aux_cond = T->exch_cond;" in ts
          and "in->post_pockets[t->slot] != (is_exch ? t->aux_item : (uint16_t)EMPTY_NO)" in ap and ap.rfind("if (is_exch) {") > ap.find("delta impossible"), results)
    check("G4-2 (X1b): a REJECTED result loses the replacement (logged), never applies it",
          "the exchange replacement is lost" in func_body(net, r"\nstatic void pcnetgame_handle_client_txn_result\(const PCNetGameTxnResultMsg\* in\) \{"), results)
    rst = func_body(net, r"\nstatic void pcnetgame_reset_client_session_state\(void\) \{")
    check("G4-2: session reset clears record and swap note",
          "memset(&s_exchange_deferred, 0, sizeof(s_exchange_deferred));" in rst and "s_exchange_swap_slot = -1;" in rst,
          results)
    ns = func_body(net, r"\nvoid pc_net_game_exchange_note_swap\(int slot, int swapped, int new_item\) \{")
    check("G4-2: swap note is client-only", "s_role != PC_NETGAME_ROLE_CLIENT" in ns, results)
    hdr = read("pc/include/pc_net_game.h")
    check("G4-2: header declares both functions",
          "int pc_net_game_exchange_request_drop(int hand_item, int hand_cond, int ut_x, int ut_z);" in hdr
          and "void pc_net_game_exchange_note_swap(int slot, int swapped, int new_item);" in hdr, results)
    check("G4-2: Group 1/2 field-put seam still routes single drops",
          "pc_net_game_request_drop(idx, (int)put_item, found_ux, found_uz)" in tag, results)

    # ---- protocol / lib unchanged
    check("protocol is v%d (wire_baseline.EXPECTED_PROTOCOL_VERSION)" % wire_baseline.EXPECTED_PROTOCOL_VERSION,
          wire_baseline.header_protocol_ok(hdr), results)
    # wire contract vs HEAD (content-based, see wire_baseline.py): the files may legitimately change, the WIRE may not
    wire_baseline.run(lambda d, c: check(d, c, results), ROOT)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
