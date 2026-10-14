#!/usr/bin/env python3
"""test_old_host_compat_src.py - a 0.2.0 CLIENT on a 0.1.1 HOST must degrade gracefully: every client -> host request added (or given new semantics) since
v0.1.1 is either gated by a capability the 0.1.1 host never announces, or answered by the 0.1.1 host without a record-protocol violation (3 violations close
the peer, pcnetgame_rec_violation). No game process.

TIER: SOURCE. Reads this tree's pc_net_game.c and the RELEASED v0.1.1 source (`git show v0.1.1:pc/src/pc_net_game.c`; the old-host checks are reported
SKIPPED when git / the tag is not available). Runtime counterpart: test_museum_mail_oldhost_real.py (a real 0.2.0 client on the real 0.1.1 program).

The table (request -> 0.1.1 host behaviour -> how the new client stays safe):
  msg 77/78/79/80/81/84/85/86/87/91 (TREE_DROP, SHOP_PREVIEW, BALL_*, BEE_SWARM, DRESSER_QUERY, SHOP_UPGRADE_REQ, WILDLIFE_TOOL_ACTION, GHOST_HUNT, PVP_HIT_REQ)
      -> no branch in the 0.1.1 dispatcher: "malformed / short / unrecognized: ignore" (no violation)              -> nothing needed
  TXN kind 18 DRESSER   -> 0.1.1: unknown kind = BAD_SHAPE + violation  -> sent only after the host's DRESSER_STATE (83) enabled it (s_dresser_cli_enabled)
  TXN kind 19 MUSEUM_MAIL -> 0.1.1: unknown kind = BAD_SHAPE + violation -> sent only when the host announced town service 6, latched off after one BAD_SHAPE
  TXN kind 20 HOUSE_UPGRADE -> 0.1.1: unknown kind = BAD_SHAPE + violation -> same gate + latch (worker C)
  TXN kind 12 MAIL_SEND by a GUEST -> 0.1.1: refused "guest has no house" BEFORE the shape check (no violation); resident shape unchanged
  TXN kind 13 MAIL_TAKE of the guest mailbox -> only offered when the host fed a guest mailbox (a 0.1.1 host never does)
  TXN kind 8 MUSEUM_DONATE by a guest -> same tag shape as 0.1.1 (aux 0, dest NONE): a semantic refusal (NO_DONOR_SLOT), never BAD_SHAPE
  CATCH_REQUEST of a stacked spirit (pocket holding SPIRIT(k-1)) -> passes the 0.1.1 grant shape (dest POCKET, item != 0): a semantic refusal at worst
Usage: python test_old_host_compat_src.py
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


ng = rd(PC, "src", "pc_net_game.c")
pg = rd(ROOT, "src", "actor", "npc", "ac_npc_post_girl.c_inc")
hdr = rd(PC, "include", "pc_net_game.h")

# ---- the kind 19 gate + latch (release 0.2.0 fix)
av = body(ng, "int pc_net_game_museum_mail_available(void) {")
ck("K19a pc_net_game_museum_mail_available(): a READY client of a host that announced town service 6 (s_up.c_have) and did not refuse kind 19 in this session",
   "s_role == PC_NETGAME_ROLE_CLIENT" in av and "s_client_link == PC_NETGAME_LINK_READY" in av and "s_up.c_have" in av and "!s_mm_unsupported" in av
   and "int pc_net_game_museum_mail_available(void);" in hdr)
bs = body(ng, "int pc_net_game_mail_begin_send(int slot) {")
ck("K19b pc_net_game_mail_begin_send() accepts a museum gift letter only when pc_net_game_museum_mail_available() (else 0 = the caller's refusal path, nothing sent)",
   "ml->present != (mActor_name_t)RSV_NO &&\n           pc_net_game_museum_mail_available()))) {" in bs)
ro = body(ng, "static void pcnetgame_ts_op_resolve(uint8_t kind, uint32_t request_id, int applied) {")
ck("K19c a REJECTED kind 19 with BAD_SHAPE latches s_mm_unsupported (at most ONE violation per session on a host with service 6 but without kind 19)",
   "kind == (uint8_t)PC_NETGAME_TXN_KIND_MUSEUM_MAIL && !applied && s_ts_last_reason == (uint8_t)PC_NETGAME_TXN_REASON_BAD_SHAPE && !s_mm_unsupported" in ro
   and "s_mm_unsupported = 1;" in ro)
ck("K19d the latch is per session: reset next to the house-upgrade latch", "pcnetgame_hu_session_reset();" in ng and re.search(r"pcnetgame_hu_session_reset\(\);[^\n]*\n\s+s_mm_unsupported = 0;", ng) is not None)
ck("K19e the post girl: on a client a museum gift letter is an address only when the host knows kind 19, else res 1 = the existing refusal row (letter and gift handed back)",
   "return pc_net_game_museum_mail_available() ? 0 : 1;" in pg)
ck("K19f s_ctxn.kind 19 is only ever chosen inside pc_net_game_mail_begin_send (no other sender of MUSEUM_MAIL)",
   ng.count("(uint8_t)PC_NETGAME_TXN_KIND_MUSEUM_MAIL : (uint8_t)PC_NETGAME_TXN_KIND_MAIL_SEND") == 1 and
   "(uint8_t)PC_NETGAME_TXN_KIND_MUSEUM_MAIL : (uint8_t)PC_NETGAME_TXN_KIND_MAIL_SEND" in bs.split("int pc_net_game_mail_begin_send")[-1] + ng[ng.index("int pc_net_game_mail_begin_send(int slot) {"):ng.index("int pc_net_game_mail_begin_send(int slot) {") + 9000])

# ---- the other new kinds
ck("K20 HOUSE_UPGRADE: pc_house_upgrade_available() needs s_up.c_have and !s_hu_unsupported (latched on BAD_SHAPE)",
   "s_up.c_have && !s_hu_unsupported" in body(ng, "int pc_house_upgrade_available(void) {") and "s_hu_unsupported = 1;" in ng)
da = body(ng, "int pc_net_game_dresser_available(void) {")
ck("K18 DRESSER: pc_net_game_dresser_available() needs s_dresser_cli_enabled, which only the host's DRESSER_STATE answer sets (a 0.1.1 host ignores the 84 probe)",
   "s_dresser_cli_enabled" in da and len(re.findall(r"s_dresser_cli_enabled = m->enabled != 0;", ng)) >= 1 and
   not re.search(r"s_dresser_cli_enabled = 1;", ng) and "if (!pc_net_game_dresser_available() ||" in body(ng, "int pc_net_game_dresser_begin(int op, int pool_slot, int pocket_slot, int item) {"))
gu = body(ng, "static int pcnetgame_client_guest_mailbox_usable(void) {")
ck("K13 guest MAIL_TAKE / 'Check my mail.': only when the host fed a guest mailbox (s_mbox_host_fed && s_mbox_guest)", "s_mbox_host_fed && s_mbox_guest" in gu)

# ---- the released 0.1.1 host
try:
    old = subprocess.run(["git", "-C", ROOT, "show", "v0.1.1:pc/src/pc_net_game.c"], capture_output=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")
except (OSError, subprocess.SubprocessError):
    old = ""
if "pcnetgame_handle_host_data" not in old:
    print("SKIPPED - the v0.1.1 source is not available (git / tag missing): old-host checks not run")
else:
    hd = body(old, "static void pcnetgame_handle_host_data(PCNetPeerId peer, const uint8_t* data, uint16_t size) {")
    ck("O1 the 0.1.1 host dispatcher ends with the silent 'unrecognized: ignore' fallthrough (no violation for an unknown message id)",
       hd.rstrip().endswith("/* malformed / short / unrecognized: ignore rather than misinterpret */") and "violation" not in hd.split("PC_NETGAME_MSG_TXN_COMMIT")[-1].split("pcnetgame_handle_host_txn_commit")[-1])
    new_ids = {"TREE_DROP": 77, "SHOP_PREVIEW": 78, "BALL_STATE": 79, "BALL_KICK_REQUEST": 80, "BEE_SWARM": 81, "DRESSER_STATE": 83, "DRESSER_QUERY": 84,
               "SHOP_UPGRADE_REQ": 85, "WILDLIFE_TOOL_ACTION": 86, "GHOST_HUNT": 87, "VILLAGER_OUTFIT": 90, "PVP_HIT_REQ": 91, "PVP_HIT": 92}
    enum_re = r"^\s*PC_NETGAME_MSG_(\w+)\s*=\s*(\d+)\s*,"
    old_ids = {k: int(v) for k, v in re.findall(enum_re, old, re.M)}
    cur_ids = {k: int(v) for k, v in re.findall(enum_re, ng, re.M)}
    rng = re.findall(r"data\[0\] >= \(uint8_t\)PC_NETGAME_MSG_(\w+) && data\[0\] <= \(uint8_t\)PC_NETGAME_MSG_(\w+)", hd)
    ck("O2 the new ids 77-81, 83-87, 90-92 are unknown to 0.1.1 (its ids end at %s) and fall in none of its range branches %s" % (max(old_ids.values() or [0]), rng),
       all(cur_ids.get(k) == v for k, v in new_ids.items()) and old_ids and max(old_ids.values()) == 76 and not (set(new_ids.values()) & set(old_ids.values()))
       and all(old_ids[b] < 77 for _a, b in rng))
    tc = hd[hd.index("if (size == sizeof(PCNetGameTxnCommitMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_TXN_COMMIT) {"):]
    tc = tc[:tc.index("return;\n    }\n") + 14]
    gen = body(old, "static void pcnetgame_handle_host_txn_commit(PCNetPeerId peer, const PCNetGameTxnCommitMsg* in) {")
    ck("O3 (the hazard) 0.1.1 routes an unknown TXN kind (18/19/20) to pcnetgame_handle_host_txn_commit, which answers BAD_SHAPE AND counts a violation: hence the gates above",
       "} else {\n            pcnetgame_handle_host_txn_commit(peer, &tc);" in tc and "KIND_MUSEUM_MAIL" not in tc and "KIND_DRESSER" not in tc
       and 'pcnetgame_rec_violation(peer, "malformed TXN_COMMIT (BAD_SHAPE)");' in gen and "in->kind >= (uint8_t)PC_NETGAME_INTERACT_KIND_PICKUP && in->kind <= (uint8_t)PC_NETGAME_INTERACT_KIND_BURY" in gen)
    om = body(old, "static void pcnetgame_handle_host_mail_txn(PCNetPeerId peer, const PCNetGameTxnCommitMsg* in) {")
    ck("O4 0.1.1 refuses a GUEST's MAIL_SEND ('no house') BEFORE its shape check: no violation for the 0.2.0 guest letter",
       om.index("if (idx >= PLAYER_NUM) {") < om.index("/* 3. shape */") and "pcnetgame_txn_reject_guest_no_house(" in om)
    ck("O5 the resident MAIL_SEND tag still matches the 0.1.1 shape (flags 0, dest NONE, slot < mail count)",
       "t->flags == 0 && t->dest == (uint8_t)PC_NETGAME_TXN_DEST_NONE && t->slot < (uint8_t)mPr_INVENTORY_MAIL_COUNT" in om)
    ots = body(old, "static void pcnetgame_handle_host_ts_txn(PCNetPeerId peer, const PCNetGameTxnCommitMsg* in) {")
    ck("O6 MUSEUM_DONATE (also by a guest): the 0.1.1 shape is aux_item 0, dest NONE, aux_cond 0 -- the 0.2.0 client builds the unchanged tag (a refusal is semantic)",
       "shape_ok = t->aux_item == 0 && t->dest == (uint8_t)PC_NETGAME_TXN_DEST_NONE && t->aux_cond == 0;" in ots)
    ck("O7 a stacked-spirit CATCH (dest POCKET, a slot, item != 0) passes the 0.1.1 grant shape (a refusal there is semantic, no violation)",
       "(t->dest == (uint8_t)PC_NETGAME_TXN_DEST_POCKET && t->slot < (uint8_t)mPr_POCKETS_SLOT_COUNT && t->item != 0)" in old)

print("-" * 60)
n = sum(1 for _d, ok in results if ok)
print("%d/%d checks passed" % (n, len(results)))
sys.exit(0 if n == len(results) else 1)
