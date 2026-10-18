#!/usr/bin/env python3
"""test_house_upgrade_src.py - source audit of the house upgrades via Nook (worker C) and of the shop-upgrade follow-ups. No game process.

TIER: SOURCE. The runtime behaviour is covered by test_house_upgrade_protocol.py (FakeClients vs a real dedicated host) and test_house_upgrade_real.py (real host + real client,
incl. the real Nook dialogue driven by the autopilot); the generated rows by the native selftest (test_work_menu_patch.py -> pc_nook_house_selftest).
Usage: python test_house_upgrade_src.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
results = []


def ck(d, c):
    results.append((d, bool(c)))
    print(("PASS - " if c else "FAIL - ") + d)


def rd(*p):
    return open(os.path.join(*p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def body(src, head):
    i = src.index(head)
    return src[i:src.index("\n}\n", i)]


ng = rd(PC, "src", "pc_net_game.c")
sc = rd(ROOT, "src", "actor", "npc", "ac_npc_shop_common.c")
hm = rd(ROOT, "src", "game", "m_home.c")
nh = rd(PC, "src", "pc_nook_house.c")
nhh = rd(PC, "include", "pc_nook_house.h")
mc = rd(PC, "src", "pc_m_card.c")
blk = ng[ng.index("===== HOUSE UPGRADE BEGIN"):ng.index("===== HOUSE UPGRADE END")]

# ---- m_home.c: the per-house construction
one = body(hm, "extern int mHm_PC_RehouseOne(int home_no, int dry_run) {")
ck("m_home: mHm_PC_RehouseOne is TARGET_PC only, keeps vanilla's branches / date test / writes (size + wall-door markers, basement, statue) and has NO all-houses outlook copy and NO state_flags loop",
   "#ifdef TARGET_PC\n/**\n * @brief Multiplayer house upgrades" in hm and "CHECK_ORDER_DATE(home, rtc_time)" in one and "mHm_RehouseWallDoor(home, home->size_info.size);" in one
   and "mHm_SetBasement((u8)home_no);" in one and "home->size_info.next_size = mHm_HOMESIZE_STATUE;" in one and "state_flags" not in one and "for (" not in one)
ck("m_home: a built-and-unannounced basement / a standing statue is not built again (the poll is idempotent)",
   "if (home->flags.has_basement && home->size_info.renew) {\n            return 0;" in one and "home->size_info.next_size != mHm_HOMESIZE_STATUE" in one)

cr = body(hm, "extern void mHm_CheckRehouseOrder() {")
ck("m_home: a network CLIENT's boot never builds an order in its copy (homes[] is the host's canon); the state_flags loop and host / solo stay vanilla",
   "skip_houses = pc_net_game_role_is_client();" in cr and "for (i = 0; i < PLAYER_NUM && !skip_houses; i++) {" in cr and "&= (~0x200);" in cr)

# ---- host handler
hd = body(blk, "static void pcnetgame_handle_host_house_upgrade_txn(")
ck("host: guests are refused PRECOND before anything else is derived; the index comes from the binding gate only",
   hd.index("idx = pcnetgame_rec_gate(peer, 0, 0);") < hd.index("if (idx >= PLAYER_NUM) {") < hd.index("PC_NETGAME_TXN_REASON_PRECOND") < hd.index("pcnetgame_rec_priv_ptr(idx)"))
ck("host: shape (op 1..4, roof palette only for ORDER_SIZE, dest NONE / slot 0 / item 0) -> BAD_SHAPE + a record violation",
   "op >= PC_HU_OP_ORDER_SIZE && op <= PC_HU_OP_ACK_BUILT" in hd and "t->aux_item < (uint16_t)mHm_OUTLOOK_PAL_NUM" in hd and 'pcnetgame_rec_violation(peer, "malformed HOUSE_UPGRADE' in hd)
ck("host: journal (fence / replay / CONFLICT) before the base / pre-image checks, which precede every precondition",
   hd.index("pcnetgame_txn_journal_find(R,") < hd.index("PC_NETGAME_TXN_REASON_STALE_IMAGE") < hd.index("pcnetgame_rec_validate_inventory(") < hd.index("pcnetgame_hu_precond(op, hs,"))
ck("host: order of the refusals: idempotent state answer -> HOUSE_BUSY (a transfer in flight) -> HOUSE_CHANGED (guard) -> NOT_ELIGIBLE (vanilla preconditions on the HOST's data)",
   hd.index("pcnetgame_hu_already(op, t, hs, mir)") < hd.index("PC_NETGAME_TXN_REASON_HOUSE_BUSY") < hd.index("PC_NETGAME_TXN_REASON_HOUSE_CHANGED") < hd.index("PC_NETGAME_TXN_REASON_HOUSE_NOT_ELIGIBLE, 1, NULL, why"))
ck("host: the statue rank is the town count, the count is capped at 3 (vanilla), written on the host thread only",
   "hs->size_info.statue_rank = n;" in hd and "if (n >= 4) {\n                n = 3;" in hd and "Save_Get(num_statues) = n;" in hd)
ck("host: ACK_BUILT sets the vanilla next loan into the MIRROR (rev + 1, last_pocket_rev) and clears renew / the basement order (pad_1 = basement)",
   "mir->inventory.loan = new_loan;" in hd and "hs->size_info.renew = FALSE;" in hd and "hs->size_info.pad_1 = 1;" in hd and "slot->rev++;" in hd and "R->last_pocket_rev = slot->rev;" in hd)
ck("host: durable save BEFORE APPLIED, rolled back + FAULT when it fails; the canon refresh after APPLIED",
   hd.index("pc_save_write_authoritative_durable()") < hd.index('pcnetgame_txn_send_applied(peer, idx, in, slot, (uint8_t)PC_NETGAME_TXN_REASON_NONE, "")') < hd.rindex("pcnetgame_house_host_refresh(h, 1);")
   and "memcpy(hs, snap, sizeof(snap));" in hd and "Save_Get(num_statues) = snap_statues;" in hd and "mir->inventory.loan = snap_loan;" in hd)
ck("loan table: Medium 148,000 / Large 398,000 / Upper 798,000 / basement 49,800 (mPlayer_DEBT1/2/4/3 == aNSC_LOAN_*)",
   all(x in blk for x in ("case mHm_HOMESIZE_MEDIUM: return (unsigned)mPlayer_DEBT1;", "case mHm_HOMESIZE_LARGE:  return (unsigned)mPlayer_DEBT2;",
                          "case mHm_HOMESIZE_UPPER:  return (unsigned)mPlayer_DEBT4;", "return (unsigned)mPlayer_DEBT3;")))

# ---- construction poll
pt = body(blk, "static void pcnetgame_hu_host_tick(uint32_t now) {")
ck("poll: 1 Hz, host only; a due house is built only when nobody uses it (the host's player, a READY player in a room of it, an owner commit) else DEFERRED (logged once a minute)",
   "(uint32_t)(now - s_next_ms) < 1000u" in pt and "pcnetgame_hu_house_busy(h)" in pt and "but DEFERRED" in pt and "60000u" in pt)
bz = body(blk, "static const char* pcnetgame_hu_house_busy(int h) {")
ck("poll: the busy test covers the host's own player (pcnetgame_local_house_unsafe), every READY peer's announced room of house h and an open owner commit of house h",
   "pcnetgame_local_house_unsafe(h)" in bz and "PC_NETSCENE_KIND_PLAYER_HOUSE" in bz and "(int)sc.owner == h" in bz and "hup_open && !s_host_peer[p].hup_pd" in bz)
ck("poll: dry run first, then the build, a durable save and the canon push; TEST-ONLY AC_TEST_REHOUSE_DUE through pc_test_hook_getenv",
   pt.index("mHm_PC_RehouseOne(h, 1)") < pt.index("pcnetgame_hu_house_busy(h)") < pt.index("mHm_PC_RehouseOne(h, 0)") < pt.index("pc_save_write_authoritative_durable()")
   < pt.index("pcnetgame_house_host_refresh(h, 1)") and 'pc_test_hook_getenv("AC_TEST_REHOUSE_DUE")' in pt)

# ---- client
ck("client: the op waits for a CLEAN record (AWAIT_CLEAN, bounded 5 s) before the kind-20 transaction; the new txn's guard travels as tag.flags",
   "#define PCNG_HU_CLEAN_TIMEOUT_MS 5000u" in blk and "pcnetgame_mail_state_clean()" in body(blk, "static void pcnetgame_hu_client_tick(void) {")
   and "flags = T->take_dst; /* the guard captured when the op began */" in ng)
ap = body(blk, "static int pcnetgame_txn_apply_house_upgrade(")
ck("client: the APPLIED apply writes only the loan of a renew ACK (deterministic from the guard) and moves the record base; homes[] is never written locally",
   "np->inventory.loan = loan;" in ap and "Save_Get(homes" not in ap and "pcnetgame_crec_set_base(in->epoch, in->rev, in->host_session, in->cdig);" in ap)
ck("client: availability = READY resident client with its own house, house sync on, service 6 seen (capability proxy), not latched unsupported",
   all(x in body(blk, "int pc_house_upgrade_available(void) {") for x in ("pcnetgame_hcl_active()", "s_up.c_have", "!s_hu_unsupported", "!pcnetgame_client_is_guest_player()")))
ck("client: an old host's BAD_SHAPE answer latches 'unsupported' for the session (at most one record violation per session)",
   "reason == (uint8_t)PC_NETGAME_TXN_REASON_BAD_SHAPE && !s_hu_unsupported" in blk and "s_hu_unsupported = 0;" in body(blk, "static void pcnetgame_hu_session_reset(void) {"))
ck("client: a session reset resolves an unresolved op REJECTED with reason 0 (LINKLOST when it was sent)", "pcnetgame_hu_session_reset();" in ng and "s_hu_op.reason = 0; /* a lost link" in blk)

# ---- dialogue seams
sw = sc[sc.rfind("\nstatic void aNSC_start_wait("):]
sw = sw[:sw.index("\n}\n")]
ck("dialogue: with the capability a client's write-needing talk start becomes PC_HOUSEUP, else REHOUSE -> greeting / no capability -> greeting (Batch G3 kept)",
   "pc_house_upgrade_available() && (aNSC_pc_hu_op = aNSC_pc_hu_talk_op(&pc_after)) != 0" in sw and "wait_type = aNSC_WAIT_TYPE_PC_HOUSEUP;" in sw
   and "} else if (!pc_house_upgrade_available() || wait_type == aNSC_WAIT_TYPE_REHOUSE) {\n            wait_type = aNSC_WAIT_TYPE_3;" in sw)
ck("dialogue: PC_HOUSEUP shows the wait row, then the op (BEGIN / PENDING), APPLIED -> the vanilla row, else a PC refusal row",
   "proc = aNSC_pc_set_talk_info_houseup;" in sw and "aNSC_pc_hs_enter(shop_common, aNSC_PC_HU_BEGIN);" in sw and "case aNSC_PC_HU_BEGIN:" in sc and "case aNSC_PC_HU_PENDING:" in sc
   and "aNSC_Set_continue_msg_num(mMsg_Get_base_window_p(), shop_common, aNSC_get_msg_no(aNSC_pc_hu_after));" in sc)
tk = body(sc, "static int aNSC_pc_hu_talk_op(int* after) {")
ck("dialogue: the talk-start op mirrors vanilla: renew -> ACK (BASEMENT_BUILT / size row), statue built -> ACK (STATUE_BUILT), loan 0 Upper -> ORDER_STATUE (REHOUSE_STATUE), Large without basement -> ORDER_BASEMENT",
   all(x in tk for x in ("aNSC_MSG_BASEMENT_BUILT", "aNSC_MSG_STATUE_BUILT", "aNSC_MSG_REHOUSE_STATUE", "aNSC_MSG_REHOUSE_BASEMENT", "PC_HU_OP_ORDER_STATUE", "PC_HU_OP_ORDER_BASEMENT")))
cs = body(sc, "static int aNSC_pc_hu_choice_step(")
ck("dialogue: the choice seam locks the vanilla message at its continue point while the op runs; APPLIED unlocks (vanilla follow-up), a refusal replaces the follow-up with a PC row",
   "mMsg_Set_LockContinue(msg_p);" in cs and cs.count("mMsg_Unset_LockContinue(msg_p);") == 2 and "aNSC_Set_continue_msg_num(msg_p, shop_common, pc_nook_msg_id(which));" in cs)
for fn, op, wr in (("static void aNSC_check_roof_col_order(", "PC_HU_OP_ORDER_SIZE, choice)", "home->size_info.next_size += 1;"),
                   ("static void aNSC_check_roof_col_order2(", "PC_HU_OP_ORDER_SIZE, choice + unk * 3)", "home->size_info.next_size += 1;"),
                   ("static void aNSC_check_col_chg_or_make_basement(", "PC_HU_OP_ORDER_BASEMENT, 0)", "aNSC_set_make_basement_info();")):
    b = body(sc, fn)
    ck("dialogue: %s -- a client takes the host order (%s) and never reaches the local write" % (fn.split("(")[0].split()[-1], op.split(",")[0]),
       "aNSC_pc_hu_choice_step(shop_common, " + op in b and b.index("aNSC_pc_hu_choice_step(shop_common, " + op) < b.index(wr) and "if (aNSC_PC_IS_CLIENT()) {" in b)
ck("dialogue: paint stays refused on a client (deferred: a bells purchase + next_outlook_pal + the pending flag)", "aNSC_PC_IS_CLIENT() && shop_common->sell_item >= ITM_RED_PAINT" in sc)

# ---- rows
ck("rows: PC_NOOK_MSG_UPG_WAIT / _FAILED / _LINKLOST / _BUSY appended (count 45), the wait row ends with BTN FORCENEXT CONTINUE, the others with MSGEND",
   "#define PC_NOOK_MSG_COUNT 55" in nhh and "PC_NOOK_MSG_UPG_BUSY,         /*" in nhh  # worker E appended 7 rows after it; the sell basket quote row (52) after them
   and "nk_code(&b, NK_BTN);\n            nk_code(&b, NK_FORCENEXT);\n            nk_code(&b, NK_CONTINUE);" in nh[nh.index("case PC_NOOK_MSG_UPG_WAIT:"):nh.index("case PC_NOOK_MSG_UPG_FAILED:")])

# ---- shop follow-ups
rt = body(ng, "static void pcnetgame_shopup_renew_tick(uint32_t now) {")
ck("shop (a): a town-reload attempt only counts once a reload was really issued (a busy listen-host player / a player in a shop is waiting, never a lost upgrade)",
   "pc_host_renewshop_reload_count() == s_shopup_armed_reloads" in rt and "s_shopup_armed_reloads = pc_host_renewshop_reload_count();" in rt
   and "s_pc_renewshop_reloads++;" in mc and "int pc_host_renewshop_reload_count(void) {" in mc)
ck("shop (c): the reopening leaflet goes to the guests' host-held mailboxes too (same handbill as mSP_SetRenewalChiraswhi_AppoDay), the guest store is written",
   "pcnetgame_shopup_guest_leaflets(); /* worker C" in body(ng, "static void pcnetgame_shopup_finish(const char* how) {")
   and "{ 0x1B, 0x1B, 0x1C, 0x1D }" in ng and "pcnetgame_guest_store_write(\"shop upgrade leaflet\")" in ng)
ck("shop (d): PENDING raises an on-screen notice on the listen host and on every client ('Tom Nook has news about his shop')",
   ng.count("pcnetgame_shopup_notice_set(3, 0);") == 2 and "Tom Nook has news about his shop: go talk to him!" in ng)

print("-" * 60)
n = sum(1 for _d, ok in results if ok)
print("%d/%d checks passed" % (n, len(results)))
sys.exit(0 if n == len(results) else 1)
