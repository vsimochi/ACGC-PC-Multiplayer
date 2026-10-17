#!/usr/bin/env python3
"""test_pk_src.py - worker PK SOURCE AUDIT: full-pocket pickup refusal + police lost-and-found multi-claim (no game process).

  FP  full-pocket pickup
      FP1 pc_net_game_client_pickup_no_room(): client-only, a plain EMPTY_NO pocket test, a wallet-fitting money bag is still allowed, read-only
      FP2 the outdoor branch of Player_actor_CheckAndRequest_main_pickup_all asks it BEFORE requesting the pickup and plays the vanilla refusal
          (Player_actor_Request_main_refuse_pickup_all) instead; own-room-local clients keep the vanilla flow
      FP3 the late refusal: the Bug 3 abort, the queued-TXN cancel (PICKUP + POCKETS_FULL) and request_pickup's pockets-full branch raise the
          one-shot flag; a new request clears it; the pickup animation's end consumes it and plays the refusal
      FP4 header declarations
  PC  police multi-claim
      PC1 the claim locks the message at its continue point while the host answers (mMsg_Set_LockContinue) and unlocks before the vanilla
          continuation (both outcomes) -- the root cause: without it the window ran on and the officer stayed in CHECK_ANSWER
      PC2 the fallback: a pending claim seen outside the continue point is still polled and the officer returns to TALK_END_WAIT
      PC3 the claimed index is relocated by item value in the current mirror before the begin (compaction-safe), never a pocket write before APPLIED
      PC4 the client never compacts its mirror on exit; the host compacts only when its own player is not in the police box
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
results = []


def read(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
        return f.read().decode("utf-8", "replace").replace("\r\n", "\n")


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def func_body(src, name):
    m = re.search(r"^(?:static )?[\w ]+?[ \*]+%s\([^{;]*\)\s*\{\n" % re.escape(name), src, re.M)
    if not m:
        return ""
    i = m.end()
    return src[i:src.index("\n}\n", i)]


def check(desc, cond):
    results.append(bool(cond))
    print(("PASS - " if cond else "FAIL - ") + desc)


def in_order(body, items):
    """every item found, each one AFTER the previous one (sequential search)"""
    at = 0
    for x in items:
        j = body.find(x, at)
        if j < 0:
            return False
        at = j + len(x)
    return True


def main():
    net = strip_comments(read("pc/src/pc_net_game.c"))
    hdr = read("pc/include/pc_net_game.h")
    common = strip_comments(read("src/game/m_player_common.c_inc"))
    pick = strip_comments(read("src/game/m_player_main_pickup.c_inc"))
    pol = strip_comments(read("src/actor/npc/ac_npc_police2_move.c_inc"))

    # ---- FP ----
    nr = func_body(net, "pc_net_game_client_pickup_no_room")
    check("FP1 pc_net_game_client_pickup_no_room exists, is client-only and tests for an EMPTY_NO pocket",
          nr and "s_role != PC_NETGAME_ROLE_CLIENT" in nr and "mPr_GetPossessionItemIdx(Now_Private, (mActor_name_t)EMPTY_NO) >= 0" in nr)
    check("FP1 ... a money bag the wallet takes whole is NOT refused (mPr_GetAmountForMoneyItem + mPr_WALLET_MAX)",
          "mPr_GetAmountForMoneyItem" in nr and "mPr_WALLET_MAX - Now_Private->inventory.wallet" in nr)
    check("FP1 ... read-only (no pocket / wallet / field write, nothing sent)",
          not re.search(r"mPr_Set|inventory\.wallet\s*[+\-]?=[^=]|pc_net_send|pcfa_set_tile|s_pickup_pending", nr))
    m = re.search(r"if \(Player_actor_check_putin_item\(actorx, &item, &target_pos, &item_pos\)\) \{(.*?)\n            \}\n        \}\n    \}", common, re.S)
    outdoor = m.group(1) if m else ""
    check("FP2 the outdoor pickup branch asks pc_net_game_client_pickup_no_room(changed_item) for a non-own-room CLIENT before requesting the pickup",
          in_order(outdoor, ["pc_net_game_role() == PC_NETGAME_ROLE_CLIENT", "!pc_net_game_client_own_room_local()", "pc_net_game_client_pickup_no_room(changed_item)",
                             "Player_actor_Request_main_refuse_pickup_all(game, FALSE)", "if (slot_idx >= 0 || can_exchange)", "Player_actor_request_main_pickup("]))
    check("FP2 ... the refusal replaces the pickup request (else-chained), the title demo path is untouched",
          re.search(r"pc_net_game_client_pickup_no_room\(changed_item\)\) \{\s*Player_actor_Request_main_refuse_pickup_all\(game, FALSE\);\s*\} else\s*(?:#endif\s*)?if \(slot_idx >= 0 \|\| can_exchange\)", outdoor)
          is not None and "mEv_IsTitleDemo()" in outdoor)
    bug3 = func_body(net, "pcnetgame_handle_client_pickup_result")
    check("FP3 the Bug 3 abort (ABORT POCKETS_FULL) raises the one-shot refusal flag",
          in_order(bug3, ["PC_NETGAME_CONFIRM_REASON_POCKETS_FULL, in->request_id);", "s_pickup_full_refusal_ms = pcnetgame_now_ms() | 1u;"]))
    cq = func_body(net, "pcnetgame_txn_cancel_queued")
    check("FP3 the queued-TXN cancel raises it for a PICKUP with POCKETS_FULL only",
          re.search(r"s_ctxn\.kind == \(uint8_t\)PC_NETGAME_INTERACT_KIND_PICKUP && reason == \(uint8_t\)PC_NETGAME_CONFIRM_REASON_POCKETS_FULL\) \{\s*s_pickup_full_refusal_ms", cq) is not None)
    rp = func_body(net, "pc_net_game_request_pickup")
    check("FP3 request_pickup: its pockets-full 'not sent' branch raises it; a request that IS sent clears it first",
          in_order(rp, ["pickup at (%d,%d) not sent (the item stays in the world)", "s_pickup_full_refusal_ms = pcnetgame_now_ms() | 1u;", "s_pickup_full_refusal_ms = 0;",
                        "s_pickup_pending.valid = 1;"]))
    tk = func_body(net, "pc_net_game_take_pickup_full_refusal")
    check("FP3 the take is one-shot (cleared on read) and bounded to 3 s", "s_pickup_full_refusal_ms = 0;" in tk and "< 3000u" in tk)
    pe = func_body(pick, "Player_actor_request_proc_index_fromPickup")
    check("FP3 the pickup animation's end (no exchange) consumes it for a CLIENT and plays the vanilla refusal after the wait request",
          in_order(pe, ["Player_actor_SettleRequestMainIndexPriority(actorx);", "pc_net_game_take_pickup_full_refusal()", "Player_actor_request_main_wait_all(",
                        "Player_actor_Request_main_refuse_pickup_all(game, FALSE);", "return;", "ITM_GOLDEN_SHOVEL"]))
    check("FP4 header declares both functions", "int pc_net_game_client_pickup_no_room(int item);" in hdr and "int pc_net_game_take_pickup_full_refusal(void);" in hdr)
    sp = func_body(pick, "Player_actor_setup_main_Pickup")
    check("FP4 the client pickup seam still forces exchange_flag off (no vanilla swap menu against network-pending state)",
          re.search(r"pc_net_game_role\(\) == PC_NETGAME_ROLE_CLIENT && !own_room_local\) \{\s*main_pickup_p->exchange_flag = FALSE;", sp) is not None)

    # ---- PC ----
    ca = func_body(pol, "aPOL2_check_answer")
    check("PC1 the claim begin locks the message while the host answers, and the busy retry keeps it locked",
          in_order(ca, ["pc_net_game_ts_begin_police_claim(free_slot, aPOL2_ts_idx, (int)aPOL2_ts_item)", "if (ts_begin < 0) {", "mMsg_Set_LockContinue(msg_p);", "return;",
                        "aPOL2_ts_waiting = (ts_begin > 0) ? TRUE : FALSE;", "if (aPOL2_ts_waiting) {", "mMsg_Set_LockContinue(msg_p);"]))
    main_blk = ca[ca.find("if (mMsg_Check_MainNormalContinue(msg_p) == TRUE) {"):]
    check("PC1 the pending poll returns (locked) and the lock is released before the outcome is applied (APPLIED and REJECTED alike)",
          in_order(main_blk, ["ts_res = pc_net_game_ts_poll();", "if (ts_res == PC_NETGAME_TS_OP_PENDING) {", "return;", "aPOL2_ts_waiting = FALSE;",
                              "mMsg_Unset_LockContinue(msg_p);", "if (ts_res == PC_NETGAME_TS_OP_APPLIED) {"]))
    pre = ca[:ca.find("if (mMsg_Check_MainNormalContinue(msg_p) == TRUE) {")]
    check("PC2 fallback before the vanilla check: a pending client claim outside the continue point is polled, unlocked, and the officer goes to TALK_END_WAIT",
          in_order(pre, ["pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && aPOL2_ts_waiting && mMsg_Check_MainNormalContinue(msg_p) != TRUE",
                         "pc_net_game_ts_poll() == PC_NETGAME_TS_OP_PENDING", "aPOL2_ts_waiting = FALSE;", "mMsg_Unset_LockContinue(msg_p);",
                         "aPOL2_setupAction(actor, aPOL2_ACT_TALK_END_WAIT);"]))
    lc = func_body(pol, "aPOL2_pc_locate_claim")
    check("PC3 aPOL2_pc_locate_claim keeps a matching index, else finds the SAME item value in the current mirror, else keeps the pointed index (host refuses)",
          "keep_items[idx] == item" in lc and "keep_items[i] == item" in lc and lc.rstrip().endswith("return idx;") and "keep_items[i] =" not in lc.replace("==", ""))
    check("PC3 the locate runs right before the begin", in_order(ca, ["aPOL2_ts_idx = aPOL2_pc_locate_claim(aPOL2_ts_idx, aPOL2_ts_item);",
                                                                      "pc_net_game_ts_begin_police_claim(free_slot, aPOL2_ts_idx"]))
    cl_blk = ca[ca.find("if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {"):ca.find("} else\n")]
    check("PC3 the client claim block never writes a pocket or keep_items (only APPLIED / the mirror do)",
          cl_blk and "mPr_SetPossessionItem" not in cl_blk and not re.search(r"keep_items\[[^\]]+\]\s*=[^=]", cl_blk))
    check("PC3 the vanilla local write stays guarded by !claimed_remote", re.search(r"if \(!claimed_remote\)\s*(?:#endif\s*)?\{\s*mPr_SetPossessionItem", ca) is not None)
    go = func_body(pol, "aPOL2_player_getout_check")
    check("PC4 the client never compacts its mirror at the exit", in_order(go, ["pc_net_game_role() != PC_NETGAME_ROLE_CLIENT", "mPB_copy_itemBuf("]))
    check("PC4 host: claim removes the item, compacts only when its own player is NOT in the police box",
          re.search(r"keep_items\[pidx\] = \(mActor_name_t\)EMPTY_NO;\s*if \(s_local_scene\.valid && s_local_scene\.scene_id == \(uint8_t\)SCENE_POLICE_BOX\) \{\s*mFI_SetFGUpData\(\);\s*\} else \{\s*mPB_copy_itemBuf", net) is not None)
    check("PC4 client mirror apply redraws the lost-and-found when the client stands in the police box",
          re.search(r"svc == \(int\)PC_NETGAME_TS_POLICE && s_local_scene\.valid && s_local_scene\.scene_id == \(uint8_t\)SCENE_POLICE_BOX\) \{\s*mFI_SetFGUpData\(\);", net) is not None)

    n_ok = sum(results)
    print("%d/%d checks passed" % (n_ok, len(results)))
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
