#!/usr/bin/env python3
"""test_roommates_src.py - ROOMMATES / MOVE-IN: source audit (docs/multiplayer-guest-roadmap.md "Roommates / move-in").

TIER: SOURCE AUDIT (+ the native selftest of pc_roommates.c). Usage: python test_roommates_src.py
  W  wire: id 104 = PC_ROOM_MSG_ID (static assert), 300 bytes, no new TXN kind / reason / town service, the capability is bit 1 of the 102 CAPS `total`,
     a client sends 104 only after it, old peers: the host drops a malformed 104 without a violation, the client ignores it unless READY
  H  host: requests validated (shape, READY + bound, rate 8 / 2 s, the requester next to the gyroid that comes with house h; the holder = homes[h]'s owner),
     roommates.ini written BEFORE the answer and undone on a
     failed write, the guest-table housekeeping only while the guest store is trusted, no private_data[] / homes[] subscript by a guest index
  R  rights: the house push to a guest = its shared house only, the outdoor-furniture resident rights include a roommate
  C  client: no knock for the shared house, every other guest visit unchanged (no door gate); the show-out after a demotion; the move-in dialogue INSIDE the vanilla
     gyroid conversation (no overlay: generated message rows, the gyroid's own menu + one choice, vanilla Yes / No windows, names via free string 10); the queued
     notice line at the top
  E  editing the shared house (host commit authorisation + rollback, grace / freeze, client own house / set_field_type / lock, music box, dressers), the spawn at the door,
     no "Welcome home"
  S  switches + test hooks: settings.ini roommates / AC_MP_ROOMMATES=0, the TEST-ONLY hooks gated by PC_NET_TEST_HOOKS + AC_TEST_HOOKS
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
results = []


def rd(*p):
    with open(os.path.join(*p), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def ck(desc, cond):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def body(src, head):
    i = src.find(head)
    if i < 0:
        return ""
    j = src.find("\n}\n", i)
    return src[i:j + 3]


ng = rd(PC, "src", "pc_net_game.c")
ri = rd(PC, "src", "pc_net_game_roommates.c_inc")
rh = rd(PC, "include", "pc_roommates.h")
rc = rd(PC, "src", "pc_roommates.c")
uh = rd(PC, "include", "pc_room_ui.h")
uc = rd(PC, "src", "pc_room_ui.c")
dlh = rd(PC, "include", "pc_room_dlg.h")
dlc = rd(PC, "src", "pc_room_dlg.c")
nk = rd(PC, "src", "pc_nook_house.c")
hh = rd(ROOT, "include", "ac_haniwa.h")
od = rd(PC, "src", "pc_ofurn_draw.c")
ofh = rd(PC, "include", "pc_outdoor_furniture.h")
pad = rd(PC, "src", "pc_pad.c")
gr = rd(ROOT, "src", "graph.c")
pm = rd(PC, "src", "pc_pause_menu.c")
mh = rd(ROOT, "src", "actor", "ac_my_house_move.c_inc")
pcm = rd(ROOT, "src", "game", "m_player_common.c_inc")
st = rd(PC, "src", "pc_settings.c")
ded = rd(PC, "src", "pc_dedicated.c")
cm = rd(PC, "CMakeLists.txt")

# ---- W
ck("W1 PC_NETGAME_MSG_ROOMMATE = 104 in the enum, asserted equal to PC_ROOM_MSG_ID; PCRoomMsg is 300 bytes (static asserts in both files)",
   re.search(r"PC_NETGAME_MSG_ROOMMATE\s*=\s*104,", ng) and '_Static_assert(PC_ROOM_MSG_ID == (unsigned)PC_NETGAME_MSG_ROOMMATE, "ROOMMATE id");' in ri
   and "#define PC_ROOM_MSG_ID     104" in rh and "#define PC_ROOM_MSG_SIZE   300" in rh and "(sizeof(PCRoomMsg) == PC_ROOM_MSG_SIZE)" in rc
   and "sizeof(PCRoomMsg) == PC_ROOM_MSG_SIZE && sizeof(PCRoomMsg) <= PC_NET_MAX_PAYLOAD" in ri)
kinds = [int(v) for v in re.findall(r"#define PC_NETGAME_TXN_KIND_\w+\s+(\d+)u", ng)]
reasons = [int(v) for v in re.findall(r"#define PC_NETGAME_TXN_REASON_\w+\s+(\d+)u", ng)]
ck("W2 no new TXN kind (max %d = 22), reason (max %d = 62) or town service (TS_NUM 11)" % (max(kinds), max(reasons)),
   max(kinds) == 22 and max(reasons) == 62 and "#define PC_NETGAME_TS_NUM      12u" in ng and "ROOMMATE" not in " ".join(re.findall(r"#define (PC_NETGAME_TXN_\w+)", ng)))
ck("W3 the capability: PC_OFURN_C4_ROOMMATES = bit 1 of the 102 CAPS total (C3 storage keeps bit 0); the host ORs it in, the client reads it with its own mask",
   "#define PC_OFURN_C4_ROOMMATES 0x02u" in ofh and "#define PC_OFURN_C3_STORAGE 0x01u" in ofh
   and "m.total = pcnetgame_ofurn_c3_flags() | pcnetgame_room_caps_bit();" in ng and "pcnetgame_room_client_caps(m->total);" in ng
   and "const int on = (total & PC_OFURN_C4_ROOMMATES) != 0;" in ri and "s_ofcl.c3_flags = m->total & PC_OFURN_C3_STORAGE;" in ng)
on = body(ri, "int pc_net_game_room_on(void) {")
snd = body(ri, "uint32_t pc_net_game_room_send(int op, int house, const uint8_t* guest_key) {")
ck("W4 a client sends 104 only while READY and after the host's capability bit (an old host never sees 104); every message is validated before it is sent",
   "return s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY && s_rmc.caps;" in on and "if (!pc_net_game_room_on()" in snd
   and "if (!pc_room_msg_valid(&m, 1)) {" in snd and snd.index("pc_room_msg_valid(&m, 1)") < snd.index("pc_net_send(0, PC_NET_RELIABLE, &m"))
hd = body(ri, "static void pcnetgame_handle_host_roommate(PCNetPeerId peer, const uint8_t* data, uint16_t size) {")
ck("W5 host: a 104 needs a READY + bound peer and exactly 300 bytes, is validated (pc_room_msg_valid from_client) and rate limited (8 / 2 s); a bad one is dropped and "
   "logged, NEVER a record violation",
   "s_host_peer_link[peer] != PC_NETGAME_LINK_READY || !s_host_peer[peer].bound_valid" in hd and "if (size != sizeof(PCRoomMsg)) {" in hd
   and "if (!pc_room_msg_valid(&in, 1)) {" in hd and "PCNG_ROOM_RATE_N" in hd and "violation" not in ri)
ck("W6 dispatch: host by id (the handler checks the size), client by exact size + id; the client handler ignores anything while not READY",
   "if (data[0] == (uint8_t)PC_NETGAME_MSG_ROOMMATE) { /* ROOMMATES" in ng and "pcnetgame_handle_host_roommate(peer, data, size);" in ng
   and "if (size == sizeof(PCRoomMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_ROOMMATE) {" in ng
   and "s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY || size != sizeof(PCRoomMsg)" in body(ri, "static void pcnetgame_handle_client_roommate("))
ck("W7 the block is included once at the end of pc_net_game.c; the pure module + the UI are built (CMake)",
   ng.count('#include "pc_net_game_roommates.c_inc"') == 1 and ng.rstrip().endswith('#include "pc_net_game_roommates.c_inc" /* ROOMMATES / MOVE-IN (message 104): the host relations, the client seam, the console */')
   and "src/pc_roommates.c" in cm and "src/pc_room_ui.c" in cm and "src/pc_room_dlg.c" in cm)

# ---- H
h = body(ri, "static void pcnetgame_room_handle(int peer, const PCRoomMsg* in) {")
ck("H1 the gyroid is the one that COMES WITH house h (fixed spots of the player-house acre); every request needs the requester next to it in town (the host's copy of "
   "its position); a client names it by its house only (tile / item must be 0)",
   "house < 0 || house >= PLAYER_NUM || !pcnetgame_room_actor_near(peer, (int)pc_room_gyroid_ut[house][0], (int)pc_room_gyroid_ut[house][1])" in h
   and "const uint8_t pc_room_gyroid_ut[4][2] = { { 51, 37 }, { 60, 37 }, { 51, 44 }, { 60, 44 } };" in rc and "PC_NETGAME_CTX_FLAG_IN_TOWN" in body(ri, "static int pcnetgame_room_actor_near(")
   and "if (m->ut_x != 0 || m->ut_z != 0 || m->item != 0) {" in rc and "m->house > 3" in rc
   and "pc_ofurn_find" not in ri and "placer" not in ri)  # no placed-furniture gyroid / placer ownership any more
ck("H2 roommates.ini is written BEFORE the RESULT goes out; a failed write restores the table (FAULT, nothing changed)",
   "if (r == PC_ROOM_R_OK && !pcnetgame_room_host_save()) {" in h and "memcpy(&s_rmh, &undo, sizeof(s_rmh)); /* nothing changed */" in h
   and h.index("pcnetgame_room_host_save()") < h.rindex("pcnetgame_room_reply_send(peer, &out);"))
ck("H3 only the holder decides (ACCEPT / DENY / REMOVE: the requester's key = the gyroid holder's), only a guest asks / withdraws / moves out",
   "if (memcmp(me, hkey, PC_ROOM_KEY_LEN) != 0) {\n                    r = PC_ROOM_R_NOT_OWNER;" in h and h.count("kind != PC_OPS_KIND_GUEST") >= 3)
gc = body(ri, "static void pcnetgame_room_host_gc(void) {")
ck("H4 housekeeping never prunes against an unknown guest table (UNTRUSTED / not loaded) and an unreadable roommates.ini refuses changes (BUSY) without rewriting it",
   "if (s_rmh_state != 1 || !s_guest_store_loaded || s_guest_untrusted) {" in gc and "s_rmh_state = rc == PC_ROOM_FILE_BAD ? -1 : 1;" in ri
   and "pcnetgame_room_host_ensure() < 0 && in->op != PC_ROOM_OP_QUERY" in h)
ck("H5 no guest index ever subscripts private_data[] / homes[] (the block indexes them only with resident loops 0..3 or a homes[] index from the save)",
   not re.search(r"private_data\)\[(?:idx|peer|g|slot)\]", ri) and not re.search(r"homes\[(?:idx|peer|g)\]", ri))
ck("H6 the holder is ALWAYS the resident who owns homes[h] (the buyer): no claim, no console owner",
   "pcnetgame_mbox_house_of(pid) == h" in body(ri, "static int pcnetgame_room_holder(int h,") and "pcnetgame_room_claim" not in ri and 'strcmp(sub, "owner")' not in ri)

# ---- R
ck("R1 the house push: a guest peer only while it is a ROOMMATE (or in its demotion grace), and only its shared house",
   "(st->bound_class == (uint8_t)PC_NETGAME_REC_CLASS_GUEST && pcnetgame_room_peer_commit_house((PCNetPeerId)p) < 0)" in ng
   and "(st->bound_class != (uint8_t)PC_NETGAME_REC_CLASS_GUEST || pcnetgame_room_peer_commit_house(peer) == h)" in ng
   and "NOT_OWNER" in body(ng, "static void pcnetgame_house_handle_begin("))
ck("R2 outdoor furniture: a roommate has a resident's rights (PERMS re-sent when they change); a new roommate gets its shared house pushed at once",
   "(s_host_peer[peer].bound_class == (uint8_t)PC_NETGAME_REC_CLASS_RESIDENT || pcnetgame_room_peer_is_roommate(peer))" in ng
   and "s_host_peer[p].hsent_seq[house] = 0;" in h)

# ---- C
ck("C1 the knock: a TARGET_PC block in the foreigner branch only; a roommate of that house walks in (no knock), every other case unchanged",
   "#ifdef TARGET_PC\n                    /* roommates (pc_room_ui.h): a guest walks into the house it shares like the holder does (no knock) */\n"
   "                    extern int pc_net_game_room_house_entry(int house);\n                    if (pc_net_game_room_house_entry(arrange_idx) > 0) {\n"
   "                        return FALSE;\n                    }\n#endif\n                    return TRUE;" in pcm)
ck("C2 the door: NO roommate gate: every guest enters (and knocks at) other people's houses exactly as before roommates (ac_my_house_move.c_inc is the pre-roommates file)",
   "pc_net_game_room" not in mh and "door_refused" not in ri + uh and subprocess.run(["git", "-C", ROOT, "diff", "--quiet", "30b152f", "--", "src/actor/ac_my_house_move.c_inc"]).returncode == 0)
he = body(ri, "int pc_net_game_room_house_entry(int house) {")
ck("C3 the entry rule is a READY client of a roommates host, a guest player, house 0..3; a roommate only when the local house h is held by the STATUS holder",
   "!s_rmc.caps" in he and "!pcnetgame_client_is_guest_player()" in he and "return pcnetgame_room_client_lives_in(house) ? 1 : 0;" in he
   and "return mPr_CheckCmpPersonalID(&Save_Get(homes[h]).ownerID, &p) == TRUE;" in body(ri, "static int pcnetgame_room_client_lives_in(int h) {"))
ct = body(ri, "static void pcnetgame_room_client_tick(void) {")
ck("C4 the show-out after a demotion: no wipe / fade, no message / submenu (a demo holds it 10 s at most), a valid exit door (never scene 0 / the same scene), then the vanilla walk-out calls",
   "mPlib_able_submenu_type1((GAME*)play) == FALSE || mMsg_Check_MainHide(mMsg_Get_base_window_p()) == FALSE || play->submenu.process_status != mSM_PROCESS_WAIT ||" in ct
   and "(mDemo_CheckDemo() != FALSE && (uint32_t)(pcnetgame_now_ms() - s_rmc.eject_t0) < PCNG_ROOM_EJECT_DEMO_MS)" in ct
   and "Common_Get(structure_exit_door_data).next_scene_id == 0 || Common_Get(structure_exit_door_data).next_scene_id == play->scene_id" in ct
   and "goto_other_scene(play, Common_GetPointer(structure_exit_door_data), TRUE)" in ct and "aMR_SaveWaltzTempo2();" in ct and "mBGMPsComp_make_ps_wipe(0x195);" in ct)
ck("C5 NO overlay: the move-in UI is the vanilla gyroid conversation (no pad hook but the TEST-ONLY one, no draw hook, no PC menu drawing in pc_room_ui.c); the rows are "
   "generated message rows in the idiomatic place (pc_room_dlg.c behind pc_nook_msg_build / pc_nook_sel_build / pc_nook_msg_patch, ids after the Nook rows, MSG_MAX asserted), "
   "the names through free string 10, the Yes / No windows the vanilla choice tail",
   "pc_room_ui_input" not in pad + uc + uh and "pc_room_ui_draw" not in gr + uc + uh and "pc_menu_" not in uc and "pc_text_draw" not in uc
   and "const unsigned rf = pc_room_ui_test_pad();" in pad and "unsigned pc_room_ui_test_pad(void) {\n    return 0;\n}" in uc
   and "if (pc_room_dlg_msg_id_valid(id)) {\n        return pc_room_dlg_build_id(id, dst, cap);" in nk and "if (pc_room_dlg_sel_id_valid(id)) {\n        return pc_room_dlg_sel_build_id(id, dst16);" in nk
   and "const int rn = pc_room_dlg_msg_patch(index, data, len, cap);" in nk and "|| pc_room_dlg_msg_id_valid(id);" in nk and "|| pc_room_dlg_sel_id_valid(id);" in nk
   and "_Static_assert(PC_ROOM_DLG_MSG_MAX == MSG_MAX," in nk and "PC_ROOM_DLG_MSG_OFS >= PC_NOOK_MSG_COUNT && PC_ROOM_DLG_SEL_OFS >= PC_NOOK_SEL_COUNT" in nk
   and "rd_code(b, RD_FREE10);" in dlc and "#define RD_FREE10 0x36" in dlc and "mMsg_Set_free_str(mMsg_Get_base_window_p(), mMsg_FREE_STR10, nm, PC_ROOM_NAME_LEN);" in uc
   and "bad += pc_room_dlg_selftest();" in nk and '#include "m_' not in dlc)
hn = rd(ROOT, "src", "actor", "ac_haniwa_move.c_inc")
dm = rd(ROOT, "src", "game", "m_demo.c")
gi = body(uc, "int pc_room_gyroid_intercept(void* actor, int house) {")
pk = body(uc, "int pc_room_gyroid_menu_pick(void* actor, int chosen) {")
ck("C6 the gyroid seams: A by a guest / the house owner in the vanilla talk position asks the host FIRST (QUERY; no talk while it answers, 1.5 s at most, then the vanilla "
   "talk unarmed); the INFO arms the conversation and the vanilla talk starts by the one-shot forced pick; the gyroid's own menus (owner 0x0925 / 0927 / 0930 / 0933 / 0936, guest "
   "0x0929) get ONE choice before End / Leave only while armed and only when their choice list is exactly the vanilla one; talk_with_master / talk_with_guest renumber "
   "the vanilla choices back (the vanilla switch kept under #else) and the inserted one enters aHNW_ACTION_PC_ROOM (TARGET_PC, appended); any other resident / off = vanilla",
   "if (pc_room_gyroid_intercept(actorx, (int)haniwa->house_idx)) {\n                return;\n            }\n#endif\n            mDemo_Request(mDemo_TYPE_TALK, actorx, &aHNW_set_talk_info_dance);" in hn
   and "if (!guest && !owner) {\n        return 0;" in gi and "mDemo_PC_TalkPositionOk((ACTOR*)actor)" in gi and "!pc_net_game_room_on()" in gi
   and "s_seq = pc_net_game_room_send(PC_ROOM_OP_QUERY, house, NULL);" in gi and "PC_ROOM_QUERY_TIMEOUT_MS" in gi and "return rui_start_talk(actor, NULL);" in gi
   and "g_pc_demo_force_talk_actor = actor; /* m_demo.c: picked without a new A press */" in uc
   and "(void*)request->actor == g_pc_demo_force_talk_actor" in dm and "g_pc_demo_force_talk_actor = NULL; /* one shot */" in dm
   and hn.count("const int pc_chosen = pc_room_gyroid_menu_pick(haniwa, (int)mChoice_GET_CHOSENUM());") == 2 and hn.count("#else\n        switch (mChoice_GET_CHOSENUM()) {\n#endif") == 1
   and hn.count("#else\n            switch (mChoice_GET_CHOSENUM()) {\n#endif") == 1 and hn.count("action = aHNW_ACTION_PC_ROOM;") == 2
   and "#ifdef TARGET_PC\n    aHNW_ACTION_PC_ROOM," in hh and hh.index("aHNW_ACTION_PC_ROOM") > hh.index("aHNW_ACTION_DOOR_OPEN_TIMER")
   and "#ifdef TARGET_PC\n        &aHNW_pc_room,\n#endif" in hn and "pc_room_gyroid_frame(actor, (int)haniwa->house_idx, (int)haniwa->common_actor_class.act);" in hn
   and "return chosen > c ? chosen - 1 : chosen;" in pk and "s_cphase != RC_ARMED || actor == NULL || actor != s_actor || c < 0" in pk
   and "index == PC_ROOM_VMSG_OWNER_FIRST || index == PC_ROOM_VMSG_OWNER_AGAIN || index == 0x0930 || index == 0x0933 || index == 0x0936" in dlc
   and "if (s_arm_mode != PC_ROOM_DLG_ARM_OWNER || rd_find_sel(data, len, ids, &n) < 0 || n != 4) {" in dlc
   and "n != 2 || ids[0] != k_guest_ids[0] || ids[1] != k_guest_ids[1]" in dlc and "pc_room_gyroid" not in od and "Talk to the gyroid" not in uc + hn)
dg = body(uc, "int pc_room_gyroid_dialog(void* actor, int house) {")
ck("C6b the move-in rows: a decision always goes through a vanilla choice window first (guest Yes / No; holder 'Let them in.' / 'Turn them down.' / 'Decide later.', "
   "'Ask to move out' then a Yes / No confirm); the host's answer is waited for behind 'Processing request...' (5 s, then 'could not reach the records'); the talk ending "
   "any way disarms (pc_room_gyroid_frame)",
   "rui_ask(PC_ROOM_DLG_OWNER_RM_CONFIRM);" in dg and "rui_send(PC_ROOM_OP_REMOVE, s_info.e[s_steps[s_step]].key);" in dg[dg.index("case PC_ROOM_DLG_OWNER_RM_CONFIRM:"):]
   and "rui_send(c == 0 ? PC_ROOM_OP_ACCEPT : PC_ROOM_OP_DENY, s_info.e[s_steps[s_step]].key);" in dg and "return rui_next_step(); /* Decide later. */" in dg
   and "(int32_t)(rui_now() - s_t0) >= (int32_t)PC_ROOM_OP_TIMEOUT_MS" in dg and "(uint32_t)(rui_now()" not in uc and "rui_answered(0, PC_ROOM_DLG_REASON_LINK)" in dg
   and "case PC_ROOM_DLG_WAIT:" in dlc and "rd_code(&b, RD_FORCENEXT); /* stops here" in dlc
   and 'rui_reset(s_talk_seen ? "the talk ended" : "the talk did not start");' in uc)
ng_ = body(uc, "const char* pc_room_ui_notice_get(void) {")
ck("C8 the notice line: a FIFO of host NOTICE lines (each ~5 s; a new line never overwrites the one on screen), drawn at the TOP (y 46, 58 under the shop upgrade line): "
   "clear of the player, the message window and the clock",
   "s_nq_head = (s_nq_head + 1) % PC_ROOM_NOTICE_QUEUE;" in ng_ and "return s_nq[s_nq_head];" in ng_ and "#define PC_ROOM_NOTICE_QUEUE     6" in uc
   and "pc_menu_draw_centered(game, rm_line, pc_shop_upgrade_notice(up_probe, (int)sizeof(up_probe)) ? 58.0f : 46.0f," in pm
   and "pc_room_ui_is_open() ? 168.0f" not in pm)
ck("C7 nothing in the UI / client seam writes game state (no pocket / wallet / house / save write)",
   not re.search(r"(inventory|pockets|wallet|homes\[[^\]]*\]\.[a-z_]+\s*=|Save_Set)", uc) and not re.search(r"\binventory\b|\bwallet\b", ri))

# ---- E: editing the shared house
pc_ = body(ng, "static void pcnetgame_house_process_commit(PCNetPeerId peer) {")
hb_ = body(ng, "static void pcnetgame_house_handle_begin(PCNetPeerId peer, const PCNetGameHouseBeginMsg* in) {")
rj_ = body(ng, "static void pcnetgame_house_reject(PCNetPeerId peer, int idx, int h, uint8_t status, uint16_t detail, uint32_t xfer) {")
ck("E1 host: a ROOMMATE's OWNER_COMMIT of its shared house is authorised like the owner's (house from the relation, never from the message), conservation over the house + "
   "the guest MIRROR record; a guest without the right is NOT_OWNER and rolled back (a FULL record push)",
   "if (idx >= PLAYER_NUM && pcnetgame_room_peer_commit_house(peer) < 0) {" in pc_ and "pcnetgame_room_peer_commit_house(peer); /* roommates: the shared house */" in pc_
   and "pcnetgame_house_count_record(s_hs_cnt_old, pcnetgame_rec_priv_ptr(idx));" in pc_ and "Save_Get(private_data)[idx]]" not in pc_
   and "if (idx >= PLAYER_NUM && pcnetgame_room_peer_commit_house(peer) < 0) {" in hb_ and "pcnetgame_house_reject(peer, idx, -1, (uint8_t)PC_NETGAME_HOUSE_ACK_NOT_OWNER" in hb_
   and "(status != (uint8_t)PC_NETGAME_HOUSE_ACK_NOT_OWNER || idx >= PLAYER_NUM)" in rj_)
cg = body(ri, "static int pcnetgame_room_peer_commit_house(PCNetPeerId peer) {")
ck("E2 demotion: the host keeps accepting the demoted roommate's LAST in-room commit (a grace that lasts while it stands in that house, then 2 min) so its pockets and the "
   "house stay one consistent pair; the client locks every NEW edit at once (frozen) and ends the freeze only when the pair settled",
   "PCNG_ROOM_GRACE_MS" in ri and "sc.kind == (uint8_t)PC_NETSCENE_KIND_PLAYER_HOUSE && (int)sc.owner == s_rmp[peer].grace_house" in cg
   and "rp->grace_house = rp->house;" in ri and "s_rmc.frozen_house == sh" in body(ri, "static int pcnetgame_room_client_room_unconfirmed(void) {")
   and "pcnetgame_room_client_pair_settled(s_rmc.frozen_house)" in ri and "!s_hcl.c_active && !s_hcl.rollback && !pcnetgame_hcl_dirty(h) && !s_crec.st_valid" in ri)
ho = body(ng, "static int pcnetgame_hcl_own_house(void) {")
el = body(ng, "int pc_net_game_client_room_edit_locked(void) {")
ak = body(ng, "static void pcnetgame_hcl_handle_ack(")
fm = rd(ROOT, "src", "game", "m_field_make.c")
ck("E3 client: the shared house is the guest's 'own' house for the sync (commit pipeline, upload deferral, quit flush); the room is the guest's own room for the vanilla "
   "editing gates (set_field_type PLAYER_ROOM) only while the host confirmed it AND the sync is usable; an unconfirmed / frozen / sync-less room is locked; a guest's NOT_OWNER "
   "is a rollback, never 'sync disabled'",
   "return pcnetgame_room_client_edit_house();" in ho and "if (pcnetgame_room_client_room_unconfirmed()) {\n        return 1;" in el and "return pcnetgame_client_is_guest_player();" in el
   and "pc_net_game_house_sync_active() &&" in body(ri, "int pc_net_game_room_edit_room(int house) {")
   and "if (mFI_GET_TYPE(field_id) == mFI_FIELD_PLAYER0_ROOM && pc_net_game_room_edit_room((int)mFI_GET_PLAYER_ROOM_NO(field_id))) {\n                house_owner_id = field_id;" in fm
   and "if (pcnetgame_client_is_guest_player()) {" in ak[ak.index("case PC_NETGAME_HOUSE_ACK_NOT_OWNER:"):])
cd = rd(ROOT, "include", "m_common_data.h")
mc = rd(ROOT, "src", "actor", "ac_my_room_msg_ctrl.c_inc")
ck("E4 the music box of the room: the shared house for a guest (GetRoomMusicBox + the three msg_ctrl reads), the vanilla arrangement for everybody else",
   "Save_Get(homes[pc_room_music_box_house()]).music_box)" in cd and mc.count("music_box = Save_Get(homes[pc_room_music_box_house()]).music_box;") == 3
   and "return mHS_get_arrange_idx(Common_Get(player_no));" in body(uc, "int pc_room_music_box_house(void) {"))
dq = body(ng, "static void pcnetgame_handle_host_dresser_query(PCNetPeerId peer, const PCNetGameDresserQueryMsg* q) {")
dtx = body(ng, "static void pcnetgame_handle_host_dresser_txn(PCNetPeerId peer, const PCNetGameTxnCommitMsg* in) {")
ck("E5 dressers: a roommate selects / uses the dressers of the shared house (the store keyed by the HOLDER's house: one store for both), the dresser records of a house with "
   "a roommate are SHARED records (fenced per identity), a guest's pocket digests are salted, the holder's v1 migration / overflow claim never run for a guest",
   "h = pcnetgame_edit_house_of(peer, idx);" in dq and "if (idx < PLAYER_NUM) {\n        pcnetgame_dresser_migrate_resident(idx, h);" in dq
   and "pcnetgame_room_peer_house(peer) != (int)sc.owner" in dtx and "pcnetgame_room_house_shared((int)s_dresser_sel[peer].house)" in dtx
   and "pcnetgame_dresser_owner8(h, owner8);" in dq)
mcard = rd(PC, "src", "pc_m_card.c")
ck("E6 spawn: a roommate's arrival starts at the door of the house it shares (the vanilla 'at home' door: exit_type 1 / extra 1, HOUSE0 + h, no RIDE_OFF_DEMO) when "
   "save/mp/roommate_homes.ini names it and the reloaded town's house h is still held by that resident; else the station; the client writes the file from the host's STATUS",
   "} else if ((roommate_house = pc_guest_roommate_house(&home)) >= 0) {" in mcard and "door_data->door_actor_name = HOUSE0 + (h & 3);" in mcard
   and "mPr_CheckCmpPersonalID(&Save_Get(homes[e->house]).ownerID, &holder) == TRUE" in mcard and "pcnetgame_room_client_remember(h, m.owner_key);" in ri
   and "idx == mPlayer_INDEX_OUTDOOR;" in mcard)
ck("E7 no 'Welcome home' notice anywhere", "Welcome home" not in ri + uc + ng)

# ---- S
ck("S1 switches: settings.ini [Network] roommates (default 1, read + written), env AC_MP_ROOMMATES=0 (host: no bit + RESULT OFF; client: no menu)",
   ".roommates = 1," in st and 'strcmp(key, "roommates") == 0' in st and 'fprintf(f, "roommates = %d\\n", g_pc_settings.roommates);' in st
   and 'getenv("AC_MP_ROOMMATES")' in ri and "g_pc_settings.roommates != 0" in ri and "if (!pcnetgame_room_host_on()) {\n        r = PC_ROOM_R_OFF;" in h)
th = ri[ri.index("#ifdef PC_NET_TEST_HOOKS\n/* TEST-ONLY"):]
ck("S2 the TEST-ONLY hooks (AC_TEST_ROOM_GYROID / _ENTER / AC_TEST_ROOM_UI) live in PC_NET_TEST_HOOKS blocks and need AC_TEST_HOOKS=1",
   "if (!pc_test_hooks_enabled() ||" in th and "#ifdef PC_NET_TEST_HOOKS\n    pcnetgame_room_test_hooks();\n#endif" in ri
   and "#ifdef PC_NET_TEST_HOOKS\n#include \"pc_test_hooks.h\"" in uc and "if (!pc_test_hooks_enabled() || sc == NULL" in uc)
ck("S3 the console: `roommates` list / remove / clear (confirm word) / owner, listed in help",
   'strcmp(cmd, "roommates") == 0' in ded and "pc_net_game_dedicated_roommates(args, pc_ded_out_line);" in ded and "roommates [list] | remove <guest> confirm" in ded
   and 'if (strcmp(a2, "confirm") != 0) {' in ri)

# ---- native selftest
r = subprocess.run([sys.executable, os.path.join(HERE, "test_roommates_native.py")], capture_output=True, text=True)
ck("N the native selftest of pc_roommates.c passes (test_roommates_native.py)", r.returncode == 0 and "ALL PASS" in r.stdout)

print("-" * 60)
print("%d/%d checks passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
