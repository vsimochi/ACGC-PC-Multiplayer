#!/usr/bin/env python3
"""test_evnpc_shared_src.py - SHARED EVENT NPCs (worker E): source audit of the host claim (TXN kind 21 EVNPC_CLAIM, TS service 9 EVNPC_CAPS), the client seams of Redd /
Saharah / Wendell / Gracie / the Wisp, the wire ids, the old-host compatibility (a 0.2.1 client never meets kind 21; a new client never sends it to a 0.2.x / 0.1.1 host)
and the kill switch. No game process.

TIER: SOURCE. Reads this tree and `git show d17cae66:pc/src/pc_net_game.c` (= release 0.2.1, the old host reference) and `git show v0.1.1:...` when available.
Usage: python test_evnpc_shared_src.py
"""
import os
import re
import subprocess
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


def git_show(rev, rel):
    try:
        return subprocess.run(["git", "-C", ROOT, "show", "%s:%s" % (rev, rel)], capture_output=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    except (OSError, subprocess.SubprocessError):
        return ""


ng = rd(PC, "src", "pc_net_game.c")
hdr = rd(PC, "include", "pc_evnpc_shared.h")
pure = rd(PC, "src", "pc_evnpc_shared.c")
nkh = rd(PC, "include", "pc_nook_house.h")
nk = rd(PC, "src", "pc_nook_house.c")
EV = os.path.join(ROOT, "src", "actor", "npc", "event")
redd = rd(EV, "ac_ev_broker2_move.c_inc")
sah = rd(EV, "ac_ev_carpetPeddler.c_inc")
art = rd(EV, "ac_ev_artist_move.c_inc")
dsg = rd(EV, "ac_ev_designer_talk.c_inc")
gst = rd(EV, "ac_ev_ghost_talk.c_inc")
bd = rd(ROOT, "src", "actor", "ac_broker_design.c")
inc = {n: rd(ROOT, "include", n) for n in ("ac_ev_broker2.h", "ac_ev_carpetPeddler.h", "m_event.h")}
cm = rd(PC, "CMakeLists.txt")

# ---- W: wire ids (the allocation of this worker) ----
ck("W1 TXN kind 21 EVNPC_CLAIM and reasons 50..54", "#define PC_NETGAME_TXN_KIND_EVNPC_CLAIM 21u" in ng and all(
    "#define PC_NETGAME_TXN_REASON_EVC_%s" % n in ng for n in ("SOLD_OUT   50u", "CLAIMED    51u", "CHANGED    52u", "OFF        53u", "NO_SPIRITS 54u")))
ck("W2 TS service 9 EVNPC_CAPS (8 bytes) and PC_NETGAME_TS_NUM 10 (11 since outdoor furniture C1 added service 10 OFURN_CAPS)", "#define PC_NETGAME_TS_EVNPC    9u" in ng and ("#define PC_NETGAME_TS_NUM      10u" in ng or "#define PC_NETGAME_TS_NUM      11u" in ng) and
   "#define PC_NETGAME_TS_EVNPC_LEN 8u" in ng)
ck("W3 no new message id (94 / 95 stay unused: the claim rides TXN_COMMIT / TXN_RESULT / TOWN_SVC_STATE)", not re.search(r"PC_NETGAME_MSG_\w+\s*=\s*9[45]\s*,", ng))
ck("W4 the header reasons equal the wire reasons, the op numbers are 1..5", "#define PC_EVC_REASON_SOLD_OUT   50" in hdr and "#define PC_EVC_REASON_NO_SPIRITS 54" in hdr
   and "#define PC_EVC_OP_WISP_RETURN    5" in hdr and "#define PC_EVC_OPS_ALL           0x3Eu" in hdr)
ck("W5 the pure module is built (CMakeLists) and has no game include", "src/pc_evnpc_shared.c" in cm and '#include "pc_evnpc_shared.h"' in pure and "m_common_data" not in pure)

# ---- C: constants pinned to the vanilla actors ----
ck("C1 Redd's price multiplier == aEBR2_PRICE_MULT (4)", "#define aEBR2_PRICE_MULT 4" in inc["ac_ev_broker2.h"] and "#define PC_EVC_REDD_MULT           4u" in hdr)
ck("C2 Saharah's price start / cap == eECPD_PRICE_START (3000) / eECPD_PRICE_MULT_CAP (4)", "#define eECPD_PRICE_START 3000" in inc["ac_ev_carpetPeddler.h"] and
   "#define eECPD_PRICE_MULT_CAP 4" in inc["ac_ev_carpetPeddler.h"] and "#define PC_EVC_SAHARAH_PRICE_START 3000u" in hdr and "#define PC_EVC_SAHARAH_MULT_CAP    4" in hdr)
ck("C3 Gracie's quota == mEv_DESGINER_NUM (3), the spirit points == mEv_GHOST_HITODAMA_NUM (5), Redd's lineup == mEv_BROKER_ITEM_NUM (3)",
   "#define mEv_DESGINER_NUM 3" in inc["m_event.h"] and "#define mEv_GHOST_HITODAMA_NUM 5" in inc["m_event.h"] and "#define mEv_BROKER_ITEM_NUM 3" in inc["m_event.h"]
   and "#define PC_EVC_DESIGNER_MAX        3" in hdr and "#define PC_EVC_SPIRIT_POINTS       5" in hdr and "#define PC_EVC_REDD_ITEMS          3" in hdr)

# ---- H: the host handler ----
h = body(ng, "static void pcnetgame_handle_host_evc_txn(PCNetPeerId peer, const PCNetGameTxnCommitMsg* in) {")
ck("H1 kind 21 is dispatched to its own handler", "if (tc.kind == (uint8_t)PC_NETGAME_TXN_KIND_EVNPC_CLAIM) {\n            pcnetgame_handle_host_evc_txn(peer, &tc);" in ng)
ck("H2 the identity comes from the binding gate only; READY + SYNCED gates first", "idx = pcnetgame_rec_gate(peer, 0, 0);" in h and "PC_NETGAME_RECS_SYNCED" in h)
ck("H3 the shape check uses the pure pc_evc_shape_ok and a malformed claim is BAD_SHAPE + a record violation (never journalled)",
   "pc_evc_shape_ok(op, dest_code, t->slot, t->item, t->flags, t->aux_item" in h and 'pcnetgame_rec_violation(peer, "malformed EVNPC_CLAIM TXN_COMMIT (BAD_SHAPE)");' in h)
order = [h.find(x) for x in ("pcnetgame_txn_nonce_fenced(R, t->txn_nonce)", "pcnetgame_txn_journal_find(R, t->txn_nonce, t->txn_seq)", "R->max_seq = t->txn_seq;",
                             "t->base_epoch != slot->epoch", "pcnetgame_rec_validate_inventory(t->pre_pockets", "pcnetgame_evc_env_off()", "switch (op) {",
                             "pcnetgame_rec_validate_inventory(post, post_conds, post_wallet)", "(void)pcnetgame_rec_txn_write_inventory(idx, post, post_conds, post_wallet);",
                             "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED", "pcnetgame_txn_send_applied(peer, idx, in, slot, (uint8_t)PC_NETGAME_TXN_REASON_NONE")]
ck("H4 order: fence -> journal replay -> journalled from here -> base -> pre-image -> kill switch -> plan (read-only) -> post-image check -> commit -> mirror -> journal -> APPLIED",
   all(x >= 0 for x in order) and order == sorted(order))
ck("H5 every refusal of the plan is journalled and nothing is mutated before it (one reject site after the plan)",
   h.count("pcnetgame_txn_reject(peer, idx, slot, in, hash, reason, 1, NULL, why);") == 1 and h.index("pcnetgame_txn_reject(peer, idx, slot, in, hash, reason, 1, NULL, why);") < h.index("THE commit"))
ck("H6 payment from the PRE-image with the SHOP_BUY payer (wallet, then money bags); the price is the HOST's (client values only compared)",
   h.count("pcnetgame_shop_pay(post, &post_conds, &post_wallet, price)") == 2 and "mSP_ItemNo2ItemPrice((mActor_name_t)t->item) * PC_EVC_REDD_MULT" in h and
   "pc_evc_saharah_price(sp->event.arabian.used)" in h)
ck("H7 Redd: one buyer per piece (items[flags] cleared in the commit, SOLD_OUT when empty), the vanilla record (sold_items / pid / used / end_time)",
   "b->items[t->flags] == (mActor_name_t)EMPTY_NO" in h and "b->items[t->flags] = (mActor_name_t)EMPTY_NO;" in h and "lbRTC_TimeCopy(&b->end_time" in h)
ck("H8 Wendell / Gracie: once per PersonalID (CLAIMED), 3 per visit (SOLD_OUT), the roll excludes what was given; Gracie never writes past pids[3]",
   h.count("pcnetgame_evc_pid_in(") == 2 and "a->walls, n)" in h and "d->gifted_cloths, n)" in h and "d->used >= mEv_DESGINER_NUM" in h)
ck("H9 the Wisp: residents only, 5 spirit points of the PRE-image, every spirit stripped from the post-image, RETURNED + ghost_day once for the town, weeds -> clear_grass",
   "idx >= PLAYER_NUM" in h and "pc_evc_spirit_points(t->pre_pockets" in h and "pc_evc_strip_spirits(post, &post_conds" in h and
   "ghost->flags |= mEv_GHOST_FLAG_RETURNED_SPIRITS;" in h and "Save_Get(event_save_common).ghost_day = 0;" in h and "Save_Set(clear_grass, TRUE);" in h)
ck("H10 rolls use the HOST RNG with the claimant's mirror as Now_Private (restored at once)", "Common_Set(now_private, mir);" in ng and "Common_Set(now_private, keep);" in ng)
ck("H11 after APPLIED the visit record (service 5) and the caps (service 9) are refreshed and pushed to every peer",
   h.index("pcnetgame_txn_send_applied(peer, idx, in, slot, (uint8_t)PC_NETGAME_TXN_REASON_NONE") < h.index("(void)pcnetgame_ts_refresh((int)PC_NETGAME_TS_EVENT); /* the visit record")
   and "pcnetgame_host_ts_push_all();" in h)
ck("H12 the visit must be on TODAY (special kind + special event type + the schedule dates)", "pcnetgame_evc_visit_on(" in h and
   "pc_evc_md_in_range(today, (unsigned)c->dates[mEv_SAVE_DATE_SPECIAL1], (unsigned)c->dates[mEv_SAVE_DATE_SPECIAL2])" in ng)

# ---- K: kill switch / capability / old hosts ----
mode = body(ng, "int pc_evc_mode(int op) {")
ck("K1 pc_evc_mode: host / solo / AC_MP_EVENT_NPC=0 = LOCAL; host announced OFF = LOCAL; announced ON for the op = CLAIM; else REFUSE (Saharah: LOCAL)",
   "s_role != PC_NETGAME_ROLE_CLIENT" in mode and "pcnetgame_evc_env_off()" in mode and "s_evc.have && !s_evc.on" in mode and "!s_evc.unsupported" in mode and
   "(s_evc.ops & (1u << op))" in mode and "op == PC_EVC_OP_SAHARAH_TRADE ? PC_EVC_MODE_LOCAL : PC_EVC_MODE_REFUSE" in mode)
ck("K2 s_evc.have is set ONLY by a validated service 9 blob (pcnetgame_evc_client_apply_caps) and cleared per session",
   len(re.findall(r"s_evc\.have = 1;", ng)) == 1 and "s_evc.have = 1;" in body(ng, "static void pcnetgame_evc_client_apply_caps(const uint8_t* blob) {") and
   "pcnetgame_evc_session_reset(); /* worker E" in ng and "pc_evc_caps_valid(m.blob)" in ng)
ck("K3 a BAD_SHAPE answer to kind 21 latches the claims off for the session (at most one violation on a host that announced 9 but lacks an op)",
   "reason == (uint8_t)PC_NETGAME_TXN_REASON_BAD_SHAPE && !s_evc.unsupported" in body(ng, "static void pcnetgame_evc_op_resolve(int applied, uint8_t reason) {") and
   "pcnetgame_evc_op_resolve(applied, applied ? (uint8_t)0 : s_ts_last_reason);" in ng)
bg = body(ng, "int pc_evc_begin(int op, int slot, int item, int flags, int aux_item) {")
ck("K4 kind 21 is only ever queued by pc_evc_begin, which needs CLAIM mode and refuses a malformed request locally (never a BAD_SHAPE)",
   ng.count("s_ctxn.kind = (uint8_t)PC_NETGAME_TXN_KIND_EVNPC_CLAIM;") == 1 and "s_ctxn.kind = (uint8_t)PC_NETGAME_TXN_KIND_EVNPC_CLAIM;" in bg and
   "pc_evc_mode(op) != PC_EVC_MODE_CLAIM" in bg and "!pc_evc_shape_ok(op," in bg)
ck("K5 the host kill switch: service 9 says OFF and kind 21 is REJECTED EVC_OFF (journalled, no violation)",
   "pc_evc_caps_build(blob, !pcnetgame_evc_env_off()" in ng and "(uint8_t)PC_NETGAME_TXN_REASON_EVC_OFF, 1, NULL" in h and 'getenv("AC_MP_EVENT_NPC")' in ng)
REL_021 = "d17cae66d9a7b4a30b0f3ea17f2a46ece4211d53"  # the 0.2.1 release commit (HEAD already contains the service 9 code)
old = git_show(REL_021, "pc/src/pc_net_game.c")
if "pcnetgame_handle_host_data" not in old:
    print("SKIPPED - the 0.2.1 source (git d17cae6) is not available: old-host checks not run")
else:
    ck("O1 the 0.2.1 host (release d17cae6) never announces service 9 (no PC_NETGAME_TS_EVNPC): a new client never sends kind 21 to it",
       "PC_NETGAME_TS_EVNPC" not in old and "#define PC_NETGAME_TS_NUM      9u" in old)
    ck("O2 a 0.2.1 CLIENT ignores the unknown service 9 ('reserved / unknown -- ignored') and never sends kind 21 itself",
       "reserved / unknown -- ignored" in old and "KIND_EVNPC_CLAIM" not in old)
    ck("O3 (the hazard) the 0.2.1 host answers an unknown TXN kind with BAD_SHAPE + a violation -- hence the gate above",
       'pcnetgame_rec_violation(peer, "malformed TXN_COMMIT (BAD_SHAPE)");' in old)
v011 = git_show("v0.1.1", "pc/src/pc_net_game.c")
if "pcnetgame_handle_host_data" in v011:
    ck("O4 the 0.1.1 host never announces service 9 either", "PC_NETGAME_TS_EVNPC" not in v011)

# ---- S: client seams ----
ck("S1 Redd: the sale waits for the host (pc_evc_begin REDD_BUY + ts_poll), APPLIED only updates the display, refused = a row; a host-side guard for a piece sold meanwhile",
   "pc_evc_begin(PC_EVC_OP_REDD_BUY, slot, item, item_idx, (int)(sell_price & 0xFFFF))" in redd and "pc_net_game_ts_poll()" in redd and
   "if (item == EMPTY_NO) {\n            mMsg_Set_continue_msg_num(msg_p, pc_evc_refusal_msg(PC_EVC_OP_REDD_BUY, PC_EVC_REASON_SOLD_OUT)); /* sold to a client while this talk ran */" in redd)
rs = redd[redd.index("static int aEBR2_pc_sell("):redd.index("static void aEBR2_sell_after(")]
ck("S1 Redd's claim path never writes the pockets / wallet itself", "mPr_SetPossessionItem" not in rs and "mSP_get_sell_price" not in rs)
ck("S2 Saharah: the carpet goes back into its slot before the claim, APPLIED skips the local writes (counter, pocket, Bells), refused keeps the carpet",
   "pc_evc_begin(PC_EVC_OP_SAHARAH_TRADE, slot, item, 0, (int)(peddler->price & 0xFFFF))" in sah and "if (aECPD_pc_applied) {" in sah and
   "mPr_SetPossessionItem(Now_Private, slot, item, mPr_ITEM_COND_NORMAL); /* the inventory took it out" in sah)
ck("S3 Wendell: the fish stays in the pockets until APPLIED, the wallpaper roll / record are skipped then, REFUSE mode refuses politely",
   "pc_evc_begin(PC_EVC_OP_ARTIST_TRADE, slot, item, 0, 0)" in art and "if (aEART_pc_applied) {" in art and "pc_evc_refusal_msg(PC_EVC_OP_ARTIST_TRADE, -1)" in art)
ck("S4 Gracie: the result is claimed, the hand-over neither grants nor records locally after APPLIED, the vanilla record is bounded by mEv_DESGINER_NUM",
   "pc_evc_begin(PC_EVC_OP_DESIGNER_GIFT, slot, 0, 0, result)" in dsg and "if (!aEDSN_pc_applied)" in dsg and "if (aEDSN_pc_applied) {" in dsg and
   "if (idx >= 0 && idx < mEv_DESGINER_NUM)" in dsg)
ck("S5 the Wisp: no local spirit deletion in claim mode, the claim at the reward choice (weeds / item) or the colour pick (roof), the host's item used, a refusal keeps the spirits",
   "if (aEGH_pc_claim_mode() == PC_EVC_MODE_LOCAL) /* a client keeps the spirits" in gst and "pc_evc_begin(PC_EVC_OP_WISP_RETURN, slot, 0, 0, reward)" in gst and
   "(int)PC_EVC_WISP_ROOF | ghost->roof_pal" in gst and "aEGH_pc_applied ? (mActor_name_t)pc_evc_item() : aEGH_not_collect_get()" in gst and
   "uint16_t aEGH_pc_host_roll_item(void) {" in gst)
ck("S6 every seam is TARGET_PC only (the vanilla body stays for host / solo)", all(s.count("#ifdef TARGET_PC") >= 2 for s in (redd, sah, art, dsg, gst)))
ck("S7 Redd's display: a piece another player bought leaves the tent display (aBD_PC_SyncDisplayWithLineup, client tick + host commit)",
   "int aBD_PC_SyncDisplayWithLineup(void) {" in bd and ng.count("aBD_PC_SyncDisplayWithLineup()") >= 2)
ck("S8 the client apply checks the post-image (legal, wallet only down) and applies the host image or the delta", "in->post_wallet > t->pre_wallet" in
   body(ng, "static int pcnetgame_txn_apply_evc(const PCNetGameClientTxn* T, const PCNetGameTxnResultMsg* in) {") and
   "return pcnetgame_txn_apply_evc(T, in);" in ng)

# ---- M: the refusal rows ----
rows = ("EVC_SOLD", "EVC_CLAIMED", "EVC_FUNDS", "EVC_FAILED", "EVC_OLDHOST", "EVC_WISP_DONE", "EVC_NO_SPIRITS")
ck("M1 seven appended refusal rows (PC_NOOK_MSG_COUNT 52), each built in pc_nook_house.c (the charset selftest covers them)",
   "#define PC_NOOK_MSG_COUNT 52" in nkh and all("PC_NOOK_MSG_%s" % r in nkh and "case PC_NOOK_MSG_%s:" % r in nk for r in rows))

print("-" * 60)
n = sum(1 for _d, ok in results if ok)
print("%d/%d checks passed" % (n, len(results)))
sys.exit(0 if n == len(results) else 1)
