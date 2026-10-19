#!/usr/bin/env python3
"""test_old_host_compat_src.py - a 0.2.0 CLIENT on a 0.1.1 HOST must degrade gracefully: every client -> host request added (or given new semantics) since
v0.1.1 is either gated by a capability the 0.1.1 host never announces, or answered by the 0.1.1 host without a record-protocol violation (3 violations close
the peer, pcnetgame_rec_violation). No game process.

TIER: SOURCE. Reads this tree's pc_net_game.c and the RELEASED v0.1.1 source (`git show v0.1.1:pc/src/pc_net_game.c`; the old-host checks are reported
SKIPPED when git / the tag is not available). Runtime counterpart: test_museum_mail_oldhost_real.py (a real 0.2.0 client on the real 0.1.1 program).

The table (request -> 0.1.1 host behaviour -> how the new client stays safe):
  msg 77/78/79/80/81/84/85/86/87/91/96 (TREE_DROP, SHOP_PREVIEW, BALL_*, BEE_SWARM, DRESSER_QUERY, SHOP_UPGRADE_REQ, WILDLIFE_TOOL_ACTION, GHOST_HUNT, PVP_HIT_REQ, ACTION_EXT)
      -> no branch in the 0.1.1 dispatcher: "malformed / short / unrecognized: ignore" (no violation)              -> nothing needed
  msg 100 CHAT_SEND (text chat) -> the same silent ignore on an old host; the new client also never sends it there: CHAT_SEND only after the host's CHAT_MSG CAPS ON
      (an old host never announces it: "Chat is not available on this server"); msg 101 CHAT_MSG on an OLD CLIENT -> its client dispatcher's silent ignore
  TXN kind 18 DRESSER   -> 0.1.1: unknown kind = BAD_SHAPE + violation  -> sent only after the host's DRESSER_STATE (83) enabled it (s_dresser_cli_enabled)
  TXN kind 19 MUSEUM_MAIL -> 0.1.1: unknown kind = BAD_SHAPE + violation -> sent only when the host announced town service 6, latched off after one BAD_SHAPE
  TXN kind 20 HOUSE_UPGRADE -> 0.1.1: unknown kind = BAD_SHAPE + violation -> same gate + latch (worker C)
  TXN kind 21 EVNPC_CLAIM -> 0.1.1 / 0.2.x: unknown kind = BAD_SHAPE + violation -> sent only after the host's town service 9 said ON for the op, latched off after one BAD_SHAPE (worker E)
  TXN kind 16 WORK ops 8..10 / tag.flags 1 (Nook Work Mode v2) -> 0.1.1 .. 0.4.0: BAD_SHAPE + violation -> sent only after the host's town service 11 said ON, latched off after one BAD_SHAPE
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
em = body(ng, "int pc_evc_mode(int op) {")
ck("K21 EVNPC_CLAIM: pc_evc_mode() is CLAIM only with s_evc.have (set by a validated service 9 blob alone), the op bit and !s_evc.unsupported (latched on BAD_SHAPE); pc_evc_begin needs CLAIM",
   "s_evc.have && !s_evc.unsupported" in em and "(s_evc.ops & (1u << op))" in em and "s_evc.unsupported = 1;" in ng and
   "pc_evc_mode(op) != PC_EVC_MODE_CLAIM" in body(ng, "int pc_evc_begin(int op, int slot, int item, int flags, int aux_item) {"))
oc = body(ng, "static int pcnetgame_ofurn_client_can(uint8_t perm) {")
ob = body(ng, "static int pcnetgame_ofurn_begin(int op, int slot, uint16_t item, int flags, int ux, int uz) {")
ck("K22 OUTDOOR_FURNITURE: pcnetgame_ofurn_client_can() needs a validated service 10 (s_ofcl.have_caps), the host's PERMS (s_ofcl.have_perms + the right) and !s_ofcl.unsupported "
   "(latched on BAD_SHAPE); every kind-22 begin (Place / Rotate / Pick up) passes it first; s_ctxn.kind 22 is only ever set in pcnetgame_ofurn_begin",
   "s_ofcl.have_caps && !s_ofcl.unsupported && s_ofcl.have_perms" in oc and "(s_ofcl.perms & perm) != 0" in oc and "s_ofcl.unsupported = 1;" in ng and
   ng.count("s_ctxn.kind = (uint8_t)PC_NETGAME_TXN_KIND_OUTDOOR_FURN;") == 1 and "s_ctxn.kind = (uint8_t)PC_NETGAME_TXN_KIND_OUTDOOR_FURN;" in ob and
   all(body(ng, h).index("pcnetgame_ofurn_client_can(") < body(ng, h).index("pcnetgame_ofurn_begin(") for h in (
       "int pc_net_game_ofurn_place(int pocket_slot, int item, int ut_x, int ut_z, int rot) {", "int pc_net_game_ofurn_rotate_to(int ut_x, int ut_z, int rot) {",
       "int pc_net_game_ofurn_pickup(int ut_x, int ut_z) {", "int pc_net_game_ofurn_move(int ut_x, int ut_z, int dir, int pull) {")) and
   ng.count("pcnetgame_ofurn_begin(PC_OFURN_OP_") == 4 and "pc_net_game_ofurn_rotate_to(ut_x, ut_z," in body(ng, "int pc_net_game_ofurn_rotate(int ut_x, int ut_z) {"))
mv = body(ng, "int pc_net_game_ofurn_move(int ut_x, int ut_z, int dir, int pull) {")
pl = body(ng, "int pc_net_game_ofurn_place(int pocket_slot, int item, int ut_x, int ut_z, int rot) {")
ck("K22c C2 (outdoor furniture): op 4 MOVE and a multi-tile PLACE are sent only after the host's message 102 CAPS (pcnetgame_ofurn_client_c2: have_c2 + the flag + "
   "!unsupported); a 0.3.0 host never sends 102, so it never sees op 4 (its pc_ofurn_shape_ok -> BAD_SHAPE + violation) or a multi-tile PLACE",
   mv.index("pcnetgame_ofurn_client_c2((uint8_t)PC_OFURN_C2_MOVE)") < mv.index("pcnetgame_ofurn_begin(PC_OFURN_OP_MOVE") and
   pl.index("pcnetgame_ofurn_client_c2((uint8_t)PC_OFURN_C2_MULTI)") < pl.index("pcnetgame_ofurn_begin(") and
   "s_ofcl.have_c2 = 1;" in body(ng, "static void pcnetgame_handle_client_ofurn2(const PCOfurnMsg* m) {") and
   body(ng, "static void pcnetgame_handle_client_ofurn2(const PCOfurnMsg* m) {").index("pc_ofurn_msg2_valid(m)") <
   body(ng, "static void pcnetgame_handle_client_ofurn2(const PCOfurnMsg* m) {").index("s_ofcl.have_c2 = 1;"))
ck("K22b the client applies service 10 only after the strict pc_ofurn_caps_valid() (an unknown flag / version: ignored, no Place) and message 98 only after pc_ofurn_msg_valid()",
   "svc == (int)PC_NETGAME_TS_OFURN ? pc_ofurn_caps_valid(m.blob)" in ng and "if (!pc_ofurn_msg_valid(m)) {" in body(ng, "static void pcnetgame_handle_client_ofurn(const PCOfurnMsg* m) {"))
ca = body(ng, "int pc_net_game_chat_available(const char** why) {")
cs = body(ng, "int pc_net_game_chat_send(const uint8_t* codes, int len) {")
cc = body(ng, "static void pcnetgame_handle_client_chat(const uint8_t* data, uint16_t size) {")
ck("K100 CHAT_SEND: a client sends it only when pc_net_game_chat_available() (READY + s_chat_cli_caps == 1); s_chat_cli_caps becomes 1 only from a validated CHAT_MSG CAPS+ON "
   "(pc_chat_msg_valid first) and is reset per session; CHAT_SEND is built nowhere else",
   "s_chat_cli_caps == 1" in ca and "s_client_link != PC_NETGAME_LINK_READY" in ca and "!pc_net_game_chat_available(NULL)" in cs and
   cc.index("pc_chat_msg_valid(&m)") < cc.index("s_chat_cli_caps = now_on;") and "(m.flags & PC_CHAT_F_ON) ? 1 : 2" in cc and
   ng.count("m.msg_type = (uint8_t)PC_NETGAME_MSG_CHAT_SEND;") == 1 and "m.msg_type = (uint8_t)PC_NETGAME_MSG_CHAT_SEND;" in cs and
   "s_chat_cli_caps = 0;" in body(ng, "static void pcnetgame_chat_client_tick(void) {"))
gu = body(ng, "static int pcnetgame_client_guest_mailbox_usable(void) {")
ck("K13 guest MAIL_TAKE / 'Check my mail.': only when the host fed a guest mailbox (s_mbox_host_fed && s_mbox_guest)", "s_mbox_host_fed && s_mbox_guest" in gu)
wv = body(ng, "int pc_net_game_work_v2(void) {")
wb = body(ng, "int pc_net_game_work_begin_op(int op, int npc) {")
ck("K16 Work v2 (TXN kind 16 ops 8..10 + tag.flags bit 0): sent only when pc_net_game_work_v2() -- READY + the host's town service 11 WORK_CAPS with bit 0 (an old host "
   "never sends service 11; its WORK shape refuses ops > 7 / flags != 0 as BAD_SHAPE + violation) -- and latched off after one BAD_SHAPE; WORK_STATE flag bits 1..3 go only to a peer "
   "that sent a v2-flagged op (an OLD CLIENT refuses a WORK_STATE with unknown flag bits) and service 11 is unknown to an old client (ignored)",
   "s_ts_client_have[PC_NETGAME_TS_WORK] && s_work_caps_on && !s_work_v2_refused" in wv and "op > (pc_net_game_work_v2() ? PC_WORK_OP_MAX_V2 : PC_WORK_OP_MAX_V1)" in wb
   and "s_ctxn.take_dst = (uint8_t)(pc_net_game_work_v2() ? PC_WORK_TXN_FLAG_V2 : 0u);" in wb and "s_host_peer[peer].work_v2" in ng and "s_work_v2_refused = 1;" in ng)

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
               "SHOP_UPGRADE_REQ": 85, "WILDLIFE_TOOL_ACTION": 86, "GHOST_HUNT": 87, "VILLAGER_OUTFIT": 90, "PVP_HIT_REQ": 91, "PVP_HIT": 92,
               "ACTION_EXT": 96,  # worker P2 puppet ACTION_EXT (presentation only, see test_action_ext_src.py)
               "OUTDOOR_FURN": 98,  # outdoor furniture C1: host -> client only (a 0.1.1 / 0.2.x CLIENT ignores it: the client dispatcher's 'unrecognized: ignore')
               "CHAT_SEND": 100, "CHAT_MSG": 101,  # text chat (worker CH)
               "OUTDOOR_FURN2": 102,  # outdoor furniture C2: host -> client only (an old client ignores it, O8 / O9)  # text chat (worker CH): C -> H (gated by CAPS, see K100) / H -> C (an old client's silent ignore, O8)
               "HANDOVER": 103,  # hand-over animations: see below
               "ARRIVAL_TRAIN": 105,  # synced arrival train: C -> H only after the host's STATE (an old host never sends one, so never sees it) / H -> C (an old client ignores it, O8)
               "ROOMMATE": 104}  # roommates: C -> H only after the 102 CAPS bit C4 (an old host never sees it) / H -> C (an old client ignores it, O8); hand-over animations: C -> H (villager) / H -> C (shop keeper relay): an old host / client ignores it (O1 / O8)
    enum_re = r"^\s*PC_NETGAME_MSG_(\w+)\s*=\s*(\d+)\s*,"
    old_ids = {k: int(v) for k, v in re.findall(enum_re, old, re.M)}
    cur_ids = {k: int(v) for k, v in re.findall(enum_re, ng, re.M)}
    rng = re.findall(r"data\[0\] >= \(uint8_t\)PC_NETGAME_MSG_(\w+) && data\[0\] <= \(uint8_t\)PC_NETGAME_MSG_(\w+)", hd)
    cd = body(old, "static void pcnetgame_handle_client_data(const uint8_t* data, uint16_t size) {")
    ck("O8 the 0.1.1 CLIENT dispatcher ends with the silent 'unrecognized: ignore' (an old client drops CHAT_MSG 101 / the CAPS announcement without a reaction)",
       cd.rstrip().endswith("/* malformed/short/unrecognized: ignore */") and not re.search(r"PC_NETGAME_MSG_\w+\s*=\s*10[01]", old))
    ck("O2 the new ids 77-81, 83-87, 90-92, 96 are unknown to 0.1.1 (its ids end at %s) and fall in none of its range branches %s" % (max(old_ids.values() or [0]), rng),
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

# ---- the released 0.3.0 (outdoor furniture C1) peers
try:
    v030 = subprocess.run(["git", "-C", ROOT, "show", "v0.3.0:pc/src/pc_net_game.c"], capture_output=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    v030o = subprocess.run(["git", "-C", ROOT, "show", "v0.3.0:pc/src/pc_outdoor_furniture.c"], capture_output=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")
except (OSError, subprocess.SubprocessError):
    v030 = v030o = ""
if "pcnetgame_handle_client_ofurn" not in v030:
    print("SKIPPED - the v0.3.0 source is not available: 0.3.0 checks not run")
else:
    cd3 = body(v030, "static void pcnetgame_handle_client_data(const uint8_t* data, uint16_t size) {")
    ck("O9 a 0.3.0 CLIENT takes message 98 only at exactly 268 bytes, refuses an unknown 98 kind / PERMS bit whole (pc_ofurn_msg_valid), has no id 102 and ends with "
       "'unrecognized: ignore' (so 102 is dropped silently and 98 must stay the C1 layout: C2 adds no 98 kind / bit)",
       "if (size == sizeof(PCOfurnMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_OUTDOOR_FURN) {" in cd3 and "m->kind > PC_OFURN_MK_PERMS" in v030o and
       "PC_OFURN_PERM_LEAF_DROP)) != 0u)" in v030o and not re.search(r"PC_NETGAME_MSG_\w+\s*=\s*102", v030) and cd3.rstrip().endswith("/* malformed/short/unrecognized: ignore */"))
    ck("O10 a 0.3.0 HOST refuses kind 22 op 4 as BAD_SHAPE (+ a violation): hence the 102 CAPS gate of K22c",
       "case PC_OFURN_OP_PICKUP:" in v030o and "PC_OFURN_OP_MOVE" not in v030o and "malformed OUTDOOR_FURNITURE TXN_COMMIT (BAD_SHAPE)" in v030)
    ck("O11 a 0.3.0 host loads only sidecar version 1 (a v2 file is refused as corrupt, never misread as 1x1 markers)",
       "hdr[1] != PC_OFURN_FILE_VERSION" in v030o)
    # C3 (outdoor storage): no new id; the outdoor SELECT is message 84 with floor 0xFC, reason 62 a new value of the existing TXN_RESULT reason byte
    cf3 = body(v030, "static uint16_t pcnetgame_dresser_cell_ftr(int h, int floor, int layer, int ut_x, int ut_z) {")
    ck("O12 a 0.3.0 HOST never answers an outdoor SELECT (floor 0xFC is no house floor: its cell lookup returns 0 -> 'refused', no STATE); a C3 client sends one only after "
       "the C3 bit of the 102 CAPS (which a 0.3.0 host never sends)",
       "floor >= mHm_ROOM_NUM" in cf3 and "not a furniture of that id in its own house" in v030 and not re.search(r"PC_NETGAME_MSG_\w+\s*=\s*102", v030))
    ck("O13 a 0.3.0 CLIENT never sends floor 0xFC (its select takes floors 0..2) and shows reason 62 with its generic refusal line (pc_ofurn_reason_text default)",
       "floor < 0 || floor > 2" in body(v030, "int pc_net_game_dresser_select(int floor, int layer, int ut_x, int ut_z, int ftr_item) {")
       and 'default: return "That didn\'t work - try again";' in v030o)
    # C3b (guests open outdoor storage, front rule): no wire change; an older host simply never answers a guest's dresser probe
    oq = body(v030, "static void pcnetgame_handle_host_dresser_query(PCNetPeerId peer, const PCNetGameDresserQueryMsg* q) {")
    sel = body(ng, "int pc_net_game_dresser_select(int floor, int layer, int ut_x, int ut_z, int ftr_item) {")
    beg = body(ng, "int pc_net_game_dresser_begin(int op, int pool_slot, int pocket_slot, int item) {")
    ook = body(ng, "int pc_net_game_ofurn_storage_open_ok(const char** why) {")
    ck("O14 a GUEST client of this build on an older host (0.3.0 / the first C3 build answer the dresser probe of residents only: `idx >= PLAYER_NUM` returns) never sends an "
       "outdoor SELECT or a kind 18 (both need pc_net_game_dresser_available() = the probe answered) and says 'Only residents can use storage outside' once the probe gave up",
       "if (idx < 0 || idx >= PLAYER_NUM) {" in oq and "if (!pc_net_game_dresser_available() ||" in sel and "if (!pc_net_game_dresser_available() ||" in beg
       and '(pcnetgame_client_is_guest_player() && s_dresser_probe_gave_up) ? "Only residents can use storage outside"' in ook)

print("-" * 60)
n = sum(1 for _d, ok in results if ok)
print("%d/%d checks passed" % (n, len(results)))
sys.exit(0 if n == len(results) else 1)
