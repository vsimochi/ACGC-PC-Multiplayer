#!/usr/bin/env python3
"""test_guest_museum_src.py - GUEST MUSEUM DONATION, SOURCE AUDIT (no game process).

  G   client: the G5.1 local refusal is gone (pc_net_game_ts_begin_museum_donate is the single host-transaction call); the new read-only predicate
      pc_net_game_museum_guest_donor() is CLIENT + READY + guest only and is declared in pc_net_game.h
  H   host (pcnetgame_handle_host_ts_txn): NO_DONOR_SLOT only for idx < 0 or a guest slot WITHOUT a record (pcnetgame_rec_txn_idx_ok); the donor nibble comes
      from pcnetgame_museum_donor_nibble(idx) (resident idx + 1, guest 5 = mMmd_DONATOR_DELETED_PLAYER); the commit is the TARGET_PC helper
      mMmd_PC_RequestMuseumDisplayDonor (no Common_Get(player_no) swap, no private_data[idx] / Save_Get(private_data) subscript in the handler); the journal /
      replay / conflict / fence / post-image gate order is unchanged; no new reason / message / service / HOST_CONFIG bit (no wire change)
  M   m_museum_display.c: the helper is TARGET_PC only, accepts donor 1..5 only, uses the vanilla category ranges, the "not yet donated" test and the
      vanilla CLR + SET macros, and never calls mLd_PlayerManKindCheck / Common_Get(player_no); the vanilla functions are unchanged
  B   Blathers: a READY guest client is "not foreigner" (TARGET_PC, after the vanilla test); the four "donated by you" tests use aCR_IS_OWN_DONATION, which on
      TARGET_PC never matches a guest (player_no >= PLAYER_NUM) -> nibble 5 takes the DELETED_PLAYER row; the vanilla expression is kept under #else;
      aCR_set_other_player_name_str is still reached only for 1..4 (5 is handled before it)
  P   plaques: every museum plaque / name lookup indexes private_data only for donor 1..4 (5 shows the 'former resident' text)
  T   the protocol / real tests exist
Tier: SOURCE AUDITED. Exit code 0 when all checks pass."""
import os
import re
import sys

import net_spike_lib as L
import test_guest_src as S
import wire_baseline

HERE = S.HERE


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    ng_raw = S.read("pc/src/pc_net_game.c")
    ng = S.mask(ng_raw)
    nf = S.functions(ng)
    h = S.read("pc/include/pc_net_game.h")

    # ------------------------------------------------------------------ G client
    mus = S.body(ng, nf, "pc_net_game_ts_begin_museum_donate")
    ck("G the G5.1 guest guard is removed: pc_net_game_ts_begin_museum_donate is the single pcnetgame_ts_begin(kind 8) call, no guest predicate, no local 'return 0'",
       mus.split("{", 1)[1].strip() == "return pcnetgame_ts_begin((uint8_t)PC_NETGAME_TXN_KIND_MUSEUM_DONATE, pocket_slot, 0, item, 0);"
       and "MUSEUM_DONATE refused locally" not in ng_raw)
    pred = S.body(ng, nf, "pc_net_game_museum_guest_donor")
    ck("G pc_net_game_museum_guest_donor() = CLIENT role AND link READY AND the existing guest predicate (read-only: no assignment)",
       pred.split("{", 1)[1].strip() == "return s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY && pcnetgame_client_is_guest_player();")
    ck("G it is declared in pc_net_game.h next to the donation begin API", "int pc_net_game_museum_guest_donor(void);" in h
       and h.index("int pc_net_game_ts_begin_museum_donate(") < h.index("int pc_net_game_museum_guest_donor(void);"))
    ck("G the guest predicate itself is unchanged (Now_Private set and player_no >= mPr_FOREIGNER)",
       "(int)Common_Get(player_no) >= (int)mPr_FOREIGNER" in S.body(ng, nf, "pcnetgame_client_is_guest_player"))
    ck("G the client test hook --ts-test-donate drives the same public call (works for a --guest client now that the guard is gone)",
       "pc_net_game_ts_begin_museum_donate(s_slot, (int)s_item)" in S.body(ng, nf, "pcnetgame_run_ts_test_hook"))

    # ------------------------------------------------------------------ H host
    ts = S.body(ng, nf, "pcnetgame_handle_host_ts_txn")
    don = ts[ts.index("} else if (is_donate) {", ts.index("pcnetgame_shop_sell_plan(")):ts.index("} else {\n        pidx = (int)t->aux_cond;")]
    ck("H NO_DONOR_SLOT only for idx < 0 or a guest slot whose record does not exist (pcnetgame_rec_txn_idx_ok); residents keep the old path",
       "if (idx < 0 || (idx >= PLAYER_NUM && !pcnetgame_rec_txn_idx_ok(idx))) {" in don and "PC_NETGAME_TXN_REASON_NO_DONOR_SLOT" in don
       and "idx >= PLAYER_NUM) {" not in don.replace("!pcnetgame_rec_txn_idx_ok(idx))) {", ""))
    ck("H the read-only predicate is unchanged: the two Blathers paintings -> NOT_DONATABLE, mMmd_GetDisplayInfo CANNOT -> NOT_DONATABLE, ALREADY -> ALREADY_DONATED",
       "FTR_SUM_ART02" in don and "FTR_SUM_ART03" in don and "info == mMmd_DISPLAY_CANNOT_DONATE" in don and "info == mMmd_DISPLAY_ALREADY_DONATED" in don
       and "PC_NETGAME_TXN_REASON_ALREADY_DONATED" in don and "PC_NETGAME_TXN_REASON_NOT_DONATABLE" in don)
    nib = S.body(ng, nf, "pcnetgame_museum_donor_nibble")
    ck("H pcnetgame_museum_donor_nibble: resident 0..3 -> idx + 1 (the vanilla value), anything else (a guest record) -> mMmd_DONATOR_DELETED_PLAYER (5)",
       nib.split("{", 1)[1].strip() == "return (idx >= 0 && idx < PLAYER_NUM) ? idx + 1 : (int)mMmd_DONATOR_DELETED_PLAYER;")
    ck("H the commit: donor = pcnetgame_museum_donor_nibble(idx), the ONLY museum writer is mMmd_PC_RequestMuseumDisplayDonor(item, donor); the vanilla "
       "mMmd_RequestMuseumDisplay / the Common_Get(player_no) swap are gone from pc_net_game.c",
       re.search(r"const int donor = pcnetgame_museum_donor_nibble\(idx\);\s*if \(!mMmd_PC_RequestMuseumDisplayDonor\(\(mActor_name_t\)t->item, donor\)\)", ts) is not None
       and ng_raw.count("mMmd_PC_RequestMuseumDisplayDonor(") == 1 and "mMmd_RequestMuseumDisplay(" not in ng_raw
       and not re.search(r"Common_Get\(player_no\)\s*=[^=]", ts) and "saved_player_no" not in ts)
    ck("H no private_data[...] / Save_Get(private_data) subscript and no s_guest_rec access in the handler: every record write goes through "
       "pcnetgame_rec_txn_write_inventory (-> pcnetgame_rec_priv_ptr, guest-aware)",
       "private_data" not in ts and "s_guest_rec" not in ts and "pcnetgame_rec_txn_write_inventory(idx, post, post_conds, post_wallet)" in ts
       and "pcnetgame_rec_priv_ptr(idx)" in S.body(ng, nf, "pcnetgame_rec_txn_write_inventory"))
    ck("H pcnetgame_rec_txn_idx_ok accepts a guest index only when its guest-table record exists (the accessor returns NULL for an unused entry)",
       "return r != NULL && r->exists == TRUE;" in S.body(ng, nf, "pcnetgame_rec_txn_idx_ok") and "s_guest[slot - PLAYER_NUM].used" in S.body(ng, nf, "pcnetgame_rec_priv_ptr"))
    order = ["pcnetgame_rec_gate(peer, 0, 0)", "pcnetgame_txn_nonce_fenced(", "pcnetgame_txn_journal_find(", "PC_NETGAME_TXN_REASON_CONFLICT", "PC_NETGAME_TXN_REASON_REPLAYED",
             "R->max_seq = t->txn_seq;", "t->base_epoch != slot->epoch", "pcnetgame_rec_validate_inventory(t->pre_pockets", "mMmd_GetDisplayInfo(",
             "pcnetgame_rec_validate_inventory(post, post_conds, post_wallet) != 0 || !pcnetgame_rec_txn_idx_ok(idx)", "mMmd_PC_RequestMuseumDisplayDonor(",
             "pcnetgame_rec_txn_write_inventory(idx, post", "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED", "pcnetgame_ts_refresh(svc);",
             "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT", "pcnetgame_txn_send_applied(peer, idx, in, slot, (uint8_t)PC_NETGAME_TXN_REASON_NONE", "pcnetgame_host_ts_push_all();"]
    pos, ok = 0, True
    for o in order:
        k = ts.find(o, pos)
        if k < 0:
            ok = False
            print("   missing / out of order:", o)
            break
        pos = k
    ck("H the journal / replay / CONFLICT / fence / base / pre-image / predicate / post-image gate / commit / journal / refresh / RESULT / mirror order is unchanged", ok)
    ck("H the commit log names the donor nibble actually written (guest record vs resident)",
       "pcnetgame_museum_donor_nibble(idx));" in ts and '"guest record" : "resident"' in ts)
    ck("H no new wire value: reasons 15 / 17 / 18 unchanged, PC_NETGAME_TS_NUM unchanged (no service 7), no new HOST_CONFIG flag",
       re.search(r"#define PC_NETGAME_TXN_REASON_NO_DONOR_SLOT\s+18u", ng_raw) and re.search(r"#define PC_NETGAME_TXN_REASON_ALREADY_DONATED\s+15u", ng_raw)
       and "PC_NETGAME_TS_MUSEUM_ATTR" not in ng_raw and "HOSTCFG_FLAG_GUEST_MUSEUM" not in ng_raw)
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), S.ROOT)
    ck("H wire_baseline: %d checks, all green" % len(wb), wb and all(c for _d, c in wb))

    # ------------------------------------------------------------------ M museum display helper
    md = S.read("src/game/m_museum_display.c").replace("\r\n", "\n")
    mh = S.read("include/m_museum_display.h").replace("\r\n", "\n")
    k = md.index("extern int mMmd_PC_RequestMuseumDisplayDonor(mActor_name_t item, int donor) {")
    helper = md[k:md.index("\n}\n", k) + 3]
    ck("M the helper is TARGET_PC only (in the .c and the prototype in the .h)",
       md.rfind("#ifdef TARGET_PC", 0, k) > md.rfind("#endif", 0, k) and md.find("#endif", k) > k
       and re.search(r"#ifdef TARGET_PC\nextern int mMmd_PC_RequestMuseumDisplayDonor\(mActor_name_t item, int donor\);[^\n]*\n#endif", mh) is not None)
    ck("M donor outside 1..5 is refused before anything is written",
       helper.index("if (donor < mMmd_DONATOR_PLAYER1 || donor > mMmd_DONATOR_DELETED_PLAYER) {\n    return FALSE;") < helper.index("_CLR("))
    ck("M the four vanilla category ranges, the 'not yet donated' test (!mMmd_IS_DONATED) and the vanilla CLR + SET macros with the explicit donor",
       all(x in helper for x in ("item >= FTR_START(FTR_DIN_TRIKERA_HEAD) && item <= FTR_END(FTR_DIN_TRILOBITE)", "item >= FTR_START(FTR_SUM_ART01) && item <= FTR_END(FTR_SUM_ART15)",
                                 "item >= ITM_INSECT00 && item < ITM_SPIRIT0", "item >= ITM_FISH00 && item <= ITM_FISH_END",
                                 "!mMmd_IS_DONATED(mMmd_FossilInfo(idx))", "!mMmd_IS_DONATED(mMmd_ArtInfo(idx))", "!mMmd_IS_DONATED(mMmd_InsectInfo(idx))", "!mMmd_IS_DONATED(mMmd_FishInfo(idx))",
                                 "mMmd_FOSSIL_SET(Save_Get(museum_display), idx, donor);", "mMmd_ART_SET(Save_Get(museum_display), idx, donor);",
                                 "mMmd_INSECT_SET(Save_Get(museum_display), idx, donor);", "mMmd_FISH_SET(Save_Get(museum_display), idx, donor);"))
       and helper.count("_CLR(Save_Get(museum_display), idx);") == 4)
    ck("M the helper never reads the LOCAL player (no mLd_PlayerManKindCheck, no Common_Get(player_no), no private_data)",
       "mLd_PlayerManKindCheck" not in helper and "player_no" not in helper and "private_data" not in helper)
    rmd = md[md.index("extern int mMmd_RequestMuseumDisplay(mActor_name_t item) {"):k]
    ck("M the vanilla mMmd_RequestMuseumDisplay / mMmd_Set* are unchanged (still credit Common_Get(player_no) + 1 behind mLd_PlayerManKindCheck)",
       md.count("Common_Get(player_no) + 1);") == 4 and "if (mLd_PlayerManKindCheck() == FALSE) {" in rmd and "mMmd_SetFish(fish_idx);" in rmd)

    # ------------------------------------------------------------------ B Blathers
    cm = S.read("src/actor/npc/ac_npc_curator_move.c_inc").replace("\r\n", "\n")
    ck("B aCR_IS_OWN_DONATION: TARGET_PC adds 'player_no < PLAYER_NUM' (a guest, player_no 4, never owns nibble 5); #else keeps the vanilla comparison",
       "#ifdef TARGET_PC" in cm and "#define aCR_IS_OWN_DONATION(donator) ((donator) == (Common_Get(player_no) + 1) && Common_Get(player_no) < PLAYER_NUM)\n#else\n"
       "#define aCR_IS_OWN_DONATION(donator) ((donator) == (Common_Get(player_no) + 1))\n#endif" in cm)
    fns = {}
    for fn in ("aCR_get_idx_to_donate_fossil", "aCR_get_idx_to_donate_art", "aCR_get_idx_to_donate_insect", "aCR_get_idx_to_donate_fish"):
        a = cm.index("static int %s(mActor_name_t item) {" % fn)
        fns[fn] = cm[a:cm.index("\n}\n", a)]
    ck("B all four 'donated by you' tests use aCR_IS_OWN_DONATION; no raw 'donator == (Common_Get(player_no) + 1)' remains",
       all("if (aCR_IS_OWN_DONATION(donator)) {" in b for b in fns.values()) and "donator == (Common_Get(player_no) + 1)" not in cm)
    ck("B nibble 5 (DELETED_PLAYER) has its own row in each, BEFORE the other-player name lookup (aCR_set_other_player_name_str only for 1..4)",
       all("case mMmd_DONATOR_DELETED_PLAYER:" in b and b.index("case mMmd_DONATOR_DELETED_PLAYER:") < b.index("aCR_set_other_player_name_str(donator - 1);") for b in fns.values()))
    ck("B DELETED_PLAYER rows (12..15) are < 16: a guest offering an already-held exhibit gets the give-back path, no transaction is begun",
       "act_idx >= 16" in cm and "return 12;" in fns["aCR_get_idx_to_donate_fossil"] and "return 13;" in fns["aCR_get_idx_to_donate_art"]
       and "res = 15;" in fns["aCR_get_idx_to_donate_insect"] and "return 14;" in fns["aCR_get_idx_to_donate_fish"])
    ti = cm[cm.index("static void aCR_set_talk_info(ACTOR* actorx) {"):]
    ti = ti[:ti.index("\n}\n")]
    ck("B aCR_set_talk_info: a READY guest client is 'not foreigner' (TARGET_PC else-branch after the unchanged vanilla mLd_PlayerManKindCheck test)",
       re.search(r"if \(mLd_PlayerManKindCheck\(\) == FALSE\) \{\n\s+not_foreigner = TRUE;\n\s+\}\n#ifdef TARGET_PC\n(\s+/\*[^*]*\*+(?:[^/*][^*]*\*+)*/\n)?\s+else if \(pc_net_game_museum_guest_donor\(\)\) \{\n\s+not_foreigner = TRUE;\n\s+\}\n#endif", ti) is not None)
    ck("B the curator file reaches pc_net_game.h (ac_npc_curator.c includes it under TARGET_PC)", "#include \"pc_net_game.h\"" in S.read("src/actor/npc/ac_npc_curator.c"))
    ck("B the client donation seam is unchanged: ts_begin 0 -> row 2 (give back: non-READY link / old host NO_DONOR_SLOT ends as REJECTED -> row 2), the vanilla commit stays host / single player only",
       re.search(r"aCR_ts_waiting = \(ts_begin > 0\) \? TRUE : FALSE;\s+if \(!aCR_ts_waiting\) \{\s+act_idx = 2;", cm) is not None
       and "if (ts_res != PC_NETGAME_TS_OP_APPLIED) {\n                        act_idx = 2;" in cm
       and "if (pc_net_game_role() != PC_NETGAME_ROLE_CLIENT)\n#endif\n    if (mMmd_RequestMuseumDisplay(sm_item_p->item) == TRUE) {" in cm)

    # ------------------------------------------------------------------ F the manual-test bug: the outcome row is frozen across the transaction
    ow = cm[cm.index("static void aCR_msg_win_open_wait(NPC_CURATOR_ACTOR* curator, GAME_PLAY* play) {"):]
    ow = ow[:ow.index("\n}\n")]
    ck("F the client branch is entered while a donation is awaited too (act_idx >= 16 || aCR_ts_waiting), not only for a freshly derived put-away row",
       "if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && (act_idx >= 16 || aCR_ts_waiting)) {" in ow)
    ck("F the row decided on the PRE-donation museum is frozen at the begin (aCR_ts_act_idx) and restored on every awaited frame BEFORE the PENDING return and the "
       "APPLIED check: the mirror of the same donation (same network poll as the RESULT) can no longer turn it into an 'already donated' row (12..15 guest, 4..7 resident)",
       "static int aCR_ts_act_idx = 0;" in cm and ow.index("aCR_ts_act_idx = act_idx;") < ow.index("int ts_res = pc_net_game_ts_poll();")
       < ow.index("act_idx = aCR_ts_act_idx;") < ow.index("PC_NETGAME_TS_OP_PENDING") < ow.index("if (ts_res != PC_NETGAME_TS_OP_APPLIED) {"))
    ck("F every decided client row is logged (pc_net_game_museum_note_row: final row, the row re-derived now, the local display info)",
       ow.count("pc_net_game_museum_note_row(") == 2 and "void pc_net_game_museum_note_row(int item, int derived_now, int final_row);" in h
       and "Blathers row for item 0x%04X" in ng_raw)
    hk = S.body(ng, nf, "pcnetgame_museum_pockets_test_hook")
    ck("F the real-test hooks are TEST-ONLY: AC_TEST_MUSEUM_POCKETS / the 'entrance' warp live inside #ifdef PC_NET_TEST_HOOKS and read their switch with pc_test_hook_getenv; "
       "the goto aid 'c' only parks the player; 'mashf' only answers choices with option 0",
       hk != "" and 'pc_test_hook_getenv("AC_TEST_MUSEUM_POCKETS")' in hk
       and ng_raw.rfind("#ifdef PC_NET_TEST_HOOKS", 0, ng_raw.index("static int pcnetgame_museum_pockets_test_hook(void) {"))
       > ng_raw.rfind("#endif", 0, ng_raw.index("static int pcnetgame_museum_pockets_test_hook(void) {"))
       and "{ SCENE_MUSEUM_ENTRANCE, mSc_DIRECT_NORTH, 0, 0, { 240, 0, 200 }, EMPTY_NO, 2, { 0, 0, 0 } }," in ng_raw
       and "mAc_PROFILE_NPC_CURATOR, ACTOR_PART_NPC" in S.body(ng, nf, "pcnetgame_goto_test_hook") and 'strcmp(s_ap.cmd, "mashf") == 0' in ng_raw)

    # ------------------------------------------------------------------ P plaques
    plaq = {"src/actor/ac_museum_fossil.c": ["mMmd_DONATOR_EXISTS(donator)"], "src/actor/ac_museum_picture.c": ["mMmd_DONATOR_EXISTS(donator)"],
            "src/actor/ac_museum_fish.c": ["fishInfo >= 1 && fishInfo <= 4"], "src/actor/ac_museum_insect.c": ["insect_caught_by >= 1 && insect_caught_by <= 4"],
            "src/actor/ac_museum_insect_goki.c_inc": ["info >= 1 && info <= 4"]}
    good = True
    for f, guards in plaq.items():
        src = S.read(f).replace("\r\n", "\n")
        for m in re.finditer(r"private_data\[", src):
            pre = src[max(0, m.start() - 400):m.start()]
            if not any(g in pre for g in guards):
                good = False
                print("   unguarded private_data subscript in", f, "at", m.start())
    ck("P every plaque / name lookup subscripts private_data only behind a donor 1..4 guard (5 = the 'former resident' text, MSG_12192 / MSG_12195 / MSG_12197)", good)
    ck("P mMmd_DONATOR_EXISTS is 1..4 and mMmd_IS_DONATED is 1..5 (a guest's 5 counts as donated, never as an existing resident)",
       "#define mMmd_DONATOR_EXISTS(donator) ((donator) >= mMmd_DONATOR_PLAYER1 && (donator) <= mMmd_DONATOR_PLAYER4)" in mh
       and "#define mMmd_IS_DONATED(donator) ((donator) >= mMmd_DONATOR_PLAYER1 && (donator) <= mMmd_DONATOR_DELETED_PLAYER)" in mh)
    cl = S.body(ng, nf, "pcnetgame_ts_valid_museum_blob")
    ck("P the client mirror accepts nibble 5 (<= DELETED_PLAYER) and still rejects 6..15", "> (unsigned)mMmd_DONATOR_DELETED_PLAYER" in cl)

    # ------------------------------------------------------------------ T
    for t in ("test_guest_museum_protocol.py", "test_guest_museum_real.py"):
        ck("T %s exists" % t, os.path.isfile(os.path.join(HERE, t)))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
