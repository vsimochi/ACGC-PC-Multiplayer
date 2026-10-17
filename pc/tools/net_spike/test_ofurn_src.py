#!/usr/bin/env python3
"""test_ofurn_src.py - OUTDOOR FURNITURE (C1 + C2) + the OPERATOR LIST: SOURCE audit of the invariants the protocol / real tests cannot see directly.

TIER: SOURCE (no game process). Reads pc_net_game.c, pc_ofurn_draw.c, ac_my_room.c, bg_item_common.c_inc, m_tag_ovl.c, m_player_main_wait.c_inc, pc_settings.c,
pc_dedicated.c, pc_pause_menu.c and the docs. Usage: python test_ofurn_src.py
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
dr = rd(PC, "src", "pc_ofurn_draw.c")
mr = rd(ROOT, "src", "actor", "ac_my_room.c")
bi = rd(ROOT, "src", "bg_item", "bg_item_common.c_inc")
tg = rd(ROOT, "src", "game", "m_tag_ovl.c")
wt = rd(ROOT, "src", "game", "m_player_main_wait.c_inc")
st = rd(PC, "src", "pc_settings.c")
dd = rd(PC, "src", "pc_dedicated.c")
pm = rd(PC, "src", "pc_pause_menu.c")
hdr = rd(PC, "include", "pc_outdoor_furniture.h")

blk = ng[ng.index("/* ===== OUTDOOR FURNITURE BEGIN"):ng.index("/* ===== OUTDOOR FURNITURE END ===== */")]

# ---- wire / dispatch
ck("W1 kind 22 / message 98 / service 10 / reasons 55..61 defined and asserted against pc_outdoor_furniture.h",
   "#define PC_NETGAME_TXN_KIND_OUTDOOR_FURN 22u" in ng and "PC_NETGAME_MSG_OUTDOOR_FURN         = 98," in ng and "#define PC_NETGAME_TS_OFURN   10u" in ng
   and "#define PC_NETGAME_TS_NUM      11u" in ng and "PC_NETGAME_TXN_REASON_OFURN_GONE == PC_OFURN_REASON_GONE" in ng and "(int)PC_NETGAME_MSG_OUTDOOR_FURN == (int)PC_OFURN_MSG_ID" in ng)
hd = body(ng, "static void pcnetgame_handle_host_data(PCNetPeerId peer, const uint8_t* data, uint16_t size) {") if "static void pcnetgame_handle_host_data(PCNetPeerId peer, const uint8_t* data, uint16_t size) {" in ng else ng
ck("W2 the host routes kind 22 to its own one-phase handler and drops a client-originated message 98",
   "pcnetgame_handle_host_ofurn_txn(peer, &tc);" in ng and "if (data[0] == (uint8_t)PC_NETGAME_MSG_OUTDOOR_FURN || data[0] == (uint8_t)PC_NETGAME_MSG_OUTDOOR_FURN2) {\n        return;" in ng)
ck("W3 the client dispatches message 98 by exact size + type, validates it (pc_ofurn_msg_valid) and only while READY",
   "if (size == sizeof(PCOfurnMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_OUTDOOR_FURN) {" in ng and "if (!pc_ofurn_msg_valid(m)) {" in blk
   and "s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY" in body(ng, "static void pcnetgame_handle_client_ofurn(const PCOfurnMsg* m) {"))
ht = body(ng, "static void pcnetgame_handle_host_ofurn_txn(PCNetPeerId peer, const PCNetGameTxnCommitMsg* in) {")
ck("W4 the kind 22 handler: READY gate, binding gate, SYNCED, shape (pc_ofurn_shape_ok -> BAD_SHAPE + violation), the journal (fence / replay / conflict), base, pre-image, the idx gate",
   all(x in ht for x in ("s_host_peer_link[peer] != PC_NETGAME_LINK_READY", "pcnetgame_rec_gate(peer, 0, 0)", "PC_NETGAME_RECS_SYNCED", "pc_ofurn_shape_ok(",
                         "pcnetgame_rec_violation(peer, \"malformed OUTDOOR_FURNITURE TXN_COMMIT (BAD_SHAPE)\")", "pcnetgame_txn_nonce_fenced(", "pcnetgame_txn_journal_find(",
                         "PC_NETGAME_TXN_REASON_CONFLICT", "PC_NETGAME_TXN_REASON_REPLAYED", "PC_NETGAME_TXN_REASON_STALE_IMAGE", "pcnetgame_rec_validate_inventory(t->pre_pockets",
                         "pcnetgame_rec_txn_idx_ok(idx)", "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED")))
ck("W5 rights: PLACE needs a RESIDENT binding; ROTATE / PICKUP a resident or an operator (the bound identity, never a claimed name); the requester position is the host's own copy",
   "pcnetgame_ofurn_peer_is_resident(peer)" in ht and "pcnetgame_ofurn_peer_may_pickup(peer)" in ht and "pc_remote_player_get_last_position((PCNetPlayerId)peer" in ht
   and "bound_pid" in body(ng, "static int pcnetgame_peer_identity(PCNetPeerId peer, int* kind, uint8_t key[PC_OPS_KEY_LEN], char* name) {")
   and "ready_player_name" not in body(ng, "static int pcnetgame_peer_identity(PCNetPeerId peer, int* kind, uint8_t key[PC_OPS_KEY_LEN], char* name) {"))
ck("W6 the reusable operator check exists and is exported (pcnetgame_player_is_op / pc_net_game_peer_is_op), the listen host's own player is implicitly an operator",
   "int pcnetgame_player_is_op(PCNetPeerId peer) {" in ng and "int pc_net_game_peer_is_op(int peer) {" in ng and "return s_role == PC_NETGAME_ROLE_HOST && !g_pc_dedicated;" in blk)

# ---- crash-safety ordering
cp = body(ng, "static int pcnetgame_ofurn_commit_place(int ux, int uz, uint16_t item, int rot, int placer_kind, const uint8_t* key, const char* name) {")
cr = body(ng, "static int pcnetgame_ofurn_commit_remove(int idx) {")
co = body(ng, "static int pcnetgame_ofurn_commit_reshape(int idx, int rot, int nox, int noz) {")
wc = body(ng, "static int pcnetgame_ofurn_write_cells(const PCOfurnMarker* before, const PCOfurnMarker* after, uint32_t* mask) {")
ck("D1 write-ahead: the sidecar is saved BEFORE the footprint tiles change (place / reshape = rotate + move / remove), a failed save or tile write undoes the marker",
   all(b.index("pcnetgame_ofurn_host_save()") < b.index("pcnetgame_ofurn_write_cells(") for b in (cp, cr, co)) and "pc_ofurn_remove_at(&s_ofh, idx);" in cp
   and "(void)pc_ofurn_add(&s_ofh, &mk);" in cr and "s_ofh.m[idx] = before;" in co
   and "return pcnetgame_ofurn_commit_reshape(idx, rot, (int)s_ofh.m[idx].ut_x, (int)s_ofh.m[idx].ut_z);" in ng)
ck("D1b C2 the footprint write is all-or-nothing: every old value is read first, a failed pcfa_set_tile restores the tiles written so far",
   wc.index("pcfa_get_tile(acre[i], tile[i], &old[i])") < wc.index("pcfa_set_tile(acre[i], tile[i], want[i])") and "(void)pcfa_set_tile(acre[j], tile[j], old[j]);" in wc
   and "PC_OFURN_FILLER" in body(ng, "static uint16_t pcnetgame_ofurn_cell_value(const PCOfurnMarker* mk, int i) {"))
ck("D2 every commit publishes through the single field path (pcnetgame_host_flush_mask -> FIELD_UPDATE) and THEN the marker delta",
   all(b.index("pcnetgame_host_flush_mask(") < b.index("pcnetgame_ofurn_broadcast") for b in (cp, cr, co)))
gc = body(ng, "static void pcnetgame_ofurn_host_gc(void) {")
ck("D3 the GC drops every marker whose footprint (ANY cell) no longer holds its values, clears the filler cells still holding RSV_FE1F and saves the sidecar",
   "!pcnetgame_ofurn_tiles_match(&mk, &bx, &bz, &bv)" in gc and "v == (uint16_t)PC_OFURN_FILLER" in gc and "pcfa_set_tile(acre, tile, (uint16_t)EMPTY_NO)" in gc
   and "pcnetgame_ofurn_host_save()" in gc and "pcnetgame_host_flush_mask(mask)" in gc)
ck("D4 the PICKUP op gives the BASE item (rotation 0), the PLACE op empties exactly the slot", "post[t->slot] = pc_ofurn_base(t->item);" in ht and "post[t->slot] = (uint16_t)EMPTY_NO;" in ht)

# ---- the vanilla gates
vp = body(ng, "static int pcnetgame_validate_and_resolve_pickup(PCNetPeerId peer, uint8_t ut_x, uint8_t ut_z,")
ck("G1 the vanilla pickup gate: a placed piece is refused for a non-resident non-operator and granted with rotation 0", "pcnetgame_ofurn_pickup_gate(peer, (int)ut_x, (int)ut_z, (uint16_t)raw_item, &granted)" in vp
   and "*granted = pc_ofurn_base(raw);" in blk and "pcnetgame_ofurn_peer_may_pickup(peer)" in body(ng, "int pcnetgame_ofurn_pickup_gate(PCNetPeerId peer, int ut_x, int ut_z, uint16_t raw, uint16_t* granted) {"))
dp = body(ng, "static int pcnetgame_is_droppable_item(mActor_name_t item) {")
ck("G2 the leaf-drop policy refuses furniture in pcnetgame_is_droppable_item (host: settings, client: the host's announcement); single player unchanged",
   "ITEM_IS_FTR(item) && !pc_net_game_ofurn_leaf_drop_ok()" in dp and "return 1;\n}" in body(ng, "int pc_net_game_ofurn_leaf_drop_ok(void) {") + "\n}")
ck("G3 the client refuses a guest's local B pickup of a placed piece with a notice (nothing sent)", "pc_net_game_ofurn_placed_at(ut_x, ut_z) && !pc_net_game_ofurn_may_pickup()" in body(ng, "int pc_net_game_request_pickup(int ut_x, int ut_z, int item) {"))

# ---- renderer: presentation only
dr_code = re.sub(r"/\*.*?\*/", "", dr, flags=re.S)
ck("R1 pc_ofurn_draw.c never writes a field tile, a deposit bit, a house, Save or a pocket",
   not re.search(r"pcfa_set_|mFI_SetFG(?!UpData)|mFI_Set\w*Deposit|mFI_LineDeposit|Save_Set|Save_Get\(homes|mPr_Set|pc_net_game_request_", dr_code))
sa = mr[mr.index("/* ===== OUTDOOR FURNITURE (C1, pc/src/pc_ofurn_draw.c)"):mr.index("#endif /* TARGET_PC (outdoor furniture) */")]
sa_code = re.sub(r"/\*.*?\*/", "", sa, flags=re.S)
ck("R2 the standalone furniture code of ac_my_room.c never runs a move_proc, never touches l_aMR_work / the room banks / my_room_clip, and passes NULL as the room actor",
   "move_proc" not in sa_code and "l_aMR_work" not in sa_code and "aMR_FtrNo2BankAddress" not in sa_code and "my_room_clip" not in sa_code
   and "profile->vtable->draw_proc(ftr_actor, NULL, game, bank);" in sa)
ck("R3 (C2 fix) every vtable is used (123 profiles -- every dresser / wardrobe / closet -- have an EMPTY dt_proc and were leaves in C1); the dt_proc runs at teardown so a ct_proc allocation is always freed",
   "return profile->vtable != NULL;" in sa and "aMR_PC_UseVtable(profile) && profile->vtable->ct_proc != NULL" in sa
   and "profile->vtable->dt_proc(ftr_actor, bank);" in body(mr, "void aMR_PC_StandaloneDt(FTR_ACTOR* ftr_actor, u8* bank) {"))
ck("R4 bg_item hooks (TARGET_PC): the leaf of a placed FURNITURE tile is hidden, the models move / draw / are torn down with the field-item actor",
   "ITEM_IS_FTR(*fg_p) && pc_ofurn_hide_leaf(base_ut_x + ut_x, base_ut_z + ut_z, *fg_p)" in bi and "pc_ofurn_bgitem_move(play);" in bi and "pc_ofurn_bgitem_draw(play);" in bi
   and "pc_ofurn_bgitem_destruct();" in bi and bi.count("#ifdef TARGET_PC") >= 5)
ck("R5 the renderer acts in the TOWN field only and registers collision only for the nearest pieces (the 64-entry move-bg registry is shared)",
   "if (!pcfa_scene_is_town()" in dr and "#define PC_OFURN_COLLIDE_MAX 24" in dr and "mCoBG_RegistMoveBg(" in dr and "mCoBG_CrossOffMoveBg(" in dr)
ck("R6 a new play scene forgets the old move-bg indices (mCoBG_InitMoveBgData re-initialised the registry)", "if (play != s_play_seen) {" in dr and "s_slot[j].collide_idx = -1;" in dr)

# ---- UI seams
ck("U1 the tag menu: mTG_TYPE_76 (unused in vanilla) = Grab / Place / Drop / Quit on PC only, offered for a plain furniture item in the town field when pc_net_game_ofurn_place_offer()",
   "{ mTG_pc_field_ftr, ARRAY_COUNT(mTG_pc_field_ftr) },                     /* mTG_TYPE_76" in tg and "#else\n    { NULL, 0 },                                                             /* mTG_TYPE_76 */\n#endif" in tg
   and "pc_net_game_ofurn_place_offer((int)item)) {\n        ret_tag_type = mTG_TYPE_76;" in tg and '"Place           "' in tg)
ck("U2 Place: the front tile + the piece facing the player, the pickup-exchange screen keeps the vanilla drop; the leaf-drop policy refuses with a notice",
   "pc_ofurn_front_tile(player->world.position.x" in tg and "pc_ofurn_rot_for_facing(" in tg and "if (menu_info->data0 == 13 || player == NULL) {" in tg
   and "Dropping furniture outside is off on this server" in tg)
ck("U3 the wait-state A / B seam: vanilla shake + pickup unchanged in the #else branch and whenever no placed piece is in front",
   "const int ofurn_used = pc_ofurn_front_press(game, Player_actor_CheckController_forShake_tree(game), Player_actor_CheckController_forPickup(game));" in wt
   and "#else\n    Player_actor_CheckAndRequest_main_shake_tree_all(game);" in wt and "if (!pc_net_game_ofurn_placed_at(ux, uz)) {\n        return 0;" in dr)
ck("U4 the notice line is drawn by pc_net_notice_draw", "pc_net_game_ofurn_notice()" in pm)
ck("U5 a client offers Place / Rotate / Pick up only with service 10 + PERMS (an old host never gets kind 22)",
   "s_ofcl.have_caps && !s_ofcl.unsupported && s_ofcl.have_perms" in body(ng, "static int pcnetgame_ofurn_client_can(uint8_t perm) {"))

# ---- settings / console / kill switch / test hooks
ck("C1 settings.ini [Network]: outdoor_furniture_place / _leaf_drop / _max_town / _max_acre parsed, saved and in the default text (ON by default, 512 / 128)",
   all(('"%s") == 0' % k) in st for k in ("outdoor_furniture_place", "outdoor_furniture_leaf_drop", "outdoor_furniture_max_town", "outdoor_furniture_max_acre"))
   and ".ofurn_place = 1," in st and ".ofurn_leaf_drop = 1," in st and ".ofurn_max_town = 512," in st and ".ofurn_max_acre = 128," in st and "outdoor_furniture_max_acre = %d" in st)
ck("C2 the console: ops / op / deop / furniture dispatched, in help", 'strcmp(cmd, "ops") == 0 || strcmp(cmd, "op") == 0 || strcmp(cmd, "deop") == 0' in dd
   and "pc_net_game_dedicated_furniture(args, pc_ded_out_line);" in dd and "ops | op <player> | deop <player>" in dd)
ck("C3 op / deop resolve online peers, the save's residents and this town's guests; a name with two identities is REFUSED as ambiguous; `peer N` works",
   "ambiguous" in blk and "pcnetgame_ops_resolve(sel, c, 8)" in blk and 'strncmp(sel, "peer ", 5) == 0' in blk and "Save_Get(private_data)[i].player_ID" in blk
   and "s_guest[i].used && pcnetgame_town_equal(&s_guest[i].town, &s_host_town)" in blk)
ck("C4 ops.ini / outdoor_furniture.dat live next to work_jobs.dat (servers/<id>/ or save/mp/)", 'pcnetgame_ofurn_host_path(path, sizeof(path), "ops.ini");' in blk
   and 'pcnetgame_ofurn_host_path(path, sizeof(path), "outdoor_furniture.dat");' in blk and "pc_server_work_path()" in body(ng, "static void pcnetgame_ofurn_host_path(char* out, size_t cap, const char* name) {"))
ck("C5 the kill switch AC_MP_OUTDOOR_FURNITURE=0 turns Place off (service 10, PERMS, kind 22 -> OFURN_OFF)", 'getenv("AC_MP_OUTDOOR_FURNITURE")' in blk
   and "return !pcnetgame_ofurn_env_off() && g_pc_settings.ofurn_place != 0;" in blk)
hooks = re.findall(r'getenv\("(AC_TEST_\w+)"\)', blk + dr)
ck("C6 every TEST-ONLY hook goes through pc_test_hook_getenv (no plain getenv for AC_TEST_*): %s" % sorted(set(re.findall(r'pc_test_hook_getenv\("(AC_TEST_\w+)"\)', blk + dr))),
   not [h for h in hooks if not re.search(r'pc_test_hook_getenv\("%s"\)' % h, blk + dr)] and not re.search(r'(?<!pc_test_hook_)getenv\("AC_TEST_', blk + dr))
ck("C7 the safety ceiling: the town cap is clamped to the 1024-entry table, refusals are OFURN_LIMIT (59)", "PC_OFURN_TABLE_CAP" in blk and "PC_NETGAME_TXN_REASON_OFURN_LIMIT" in blk)

# ---- C2: multi-tile footprints, message 102, MOVE, the grab driver
hw = rd(ROOT, "src", "game", "m_player_main_hold.c_inc")
hr = rd(ROOT, "src", "game", "m_player_main_rotate_furniture.c_inc")
pof = rd(PC, "src", "pc_outdoor_furniture.c")
ck("C2W1 message 102 OUTDOOR_FURN2 = the 98 layout (268 B) with its own id; the client takes it by exact size + type and validates it (pc_ofurn_msg2_valid)",
   "PC_NETGAME_MSG_OUTDOOR_FURN2        = 102," in ng and "(int)PC_NETGAME_MSG_OUTDOOR_FURN2 == (int)PC_OFURN_MSG2_ID" in ng
   and "if (size == sizeof(PCOfurnMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_OUTDOOR_FURN2) {" in ng and "if (!pc_ofurn_msg2_valid(m)) {" in blk)
pp = body(ng, "static void pcnetgame_ofurn_host_pump_peer(PCNetPeerId peer) {")
bc = body(ng, "static void pcnetgame_ofurn_broadcast(uint8_t kind, const PCOfurnMarker* mk) {")
ck("C2W2 message 98 (the 0.3.0 client's list) carries 1x1 pieces ONLY: the list filters shape 1X1, deltas of a multi-tile piece go to 102 only",
   "s_ofh.m[st->ofurn_next + i].shape == (uint8_t)PC_OFURN_FP_1X1" in pp and "pcnetgame_ofurn_count_1x1()" in pp
   and "if (mk->shape == (uint8_t)PC_OFURN_FP_1X1) {\n        pcnetgame_ofurn_send_delta(0, kind, mk, NULL);" in bc and "pcnetgame_ofurn_send_delta(1, kind, mk, NULL);" in bc)
ck("C2W3 no new PERMS bit / service 10 flag / kind on 98 (a 0.3.0 client refuses those): the C2 capability travels as a 102 CAPS",
   "PC_OFURN_MK_CAPS" not in pp and "m.kind = PC_OFURN_MK_CAPS;" in blk and "#define PC_OFURN_CAPS_VERSION 1u" in hdr and "#define PC_OFURN_PERM_LEAF_DROP 0x08u" in hdr
   and "m->kind > PC_OFURN_MK_PERMS" in pof)
ck("C2W4 a client sends MOVE / a multi-tile PLACE only after the host's 102 CAPS (a 0.3.0 host counts an unknown op as BAD_SHAPE)",
   "if (!pcnetgame_ofurn_client_c2((uint8_t)PC_OFURN_C2_MOVE)) {" in body(ng, "int pc_net_game_ofurn_move(int ut_x, int ut_z, int dir, int pull) {")
   and "!pcnetgame_ofurn_client_c2((uint8_t)PC_OFURN_C2_MULTI)" in body(ng, "int pc_net_game_ofurn_place(int pocket_slot, int item, int ut_x, int ut_z, int rot) {")
   and "s_ofcl.have_c2 && (s_ofcl.c2_flags & feature) != 0 && !s_ofcl.unsupported" in blk)
ck("C2H1 the MOVE op: the same rights as pickup / rotate, the piece checked against its whole footprint, the player's side + every newly covered cell validated",
   "case PC_OFURN_OP_MOVE:" in ht and "pcnetgame_ofurn_check_move(midx," in ht and "pcnetgame_ofurn_commit_reshape(midx, (int)s_ofh.m[midx].rot, move_x, move_z)" in ht
   and "pcnetgame_ofurn_tiles_match(&s_ofh.m[idx], NULL, NULL, NULL)" in body(ng, "static int pcnetgame_ofurn_check_existing(int ux, int uz, uint16_t item, uint8_t* reason, const char** why) {")
   and "pcnetgame_ofurn_cells_rules(xs, zs, n," in body(ng, "static uint8_t pcnetgame_ofurn_check_reshape(int idx, int rot, int nox, int noz, const char** why) {"))
ck("C2H2 multi-tile placement: every cell follows the C1 tile rules + flat ground, the footprint stays in the town, the filler never counts as a building",
   "pcnetgame_ofurn_cells_rules(xs, zs, n, ux, uz, why)" in body(ng, "static uint8_t pcnetgame_ofurn_check_place(int ux, int uz, uint16_t item, int rot_in, int player_ux, int player_uz, const char** why) {")
   and "v != (uint16_t)PC_OFURN_FILLER" in body(ng, "static int pcnetgame_ofurn_is_structure(uint16_t v) {") and "fabsf(c.y - c0.y) > 4.0f" in blk)
ck("C2H3 the sidecar: version 2 written, a version 1 (0.3.0) file converted with a .v1.bak copy kept",
   "#define PC_OFURN_FILE_VERSION 2u" in hdr and '"%s.v1.bak"' in blk and "pc_ofurn_copy_file(path, bak)" in blk and "ver == (int)PC_OFURN_FILE_VERSION_C1" in blk)
ck("C2H4 the filler RSV_FE1F: STABLE for the field diff (never withheld like RSV_NO), refused by the snowman build",
   "#define PC_OFURN_FILLER      0xFE1Fu" in hdr and "value == (uint16_t)PC_OFURN_FILLER" in ng
   and "if (v == RSV_NO || v == RSV_SIGNBOARD) {" in rd(PC, "src", "pc_field_authority.c"))
ck("C2P1 the player's HOLD / ROTATE states ask the PC outdoor judge only when there is no My_Room actor (the vanilla call is unchanged in the #else branch)",
   "Common_Get(clip).my_room_clip != NULL ? Common_Get(clip).my_room_clip->judge_player_action_proc : pc_ofurn_grab_judge;" in hw
   and "#else\n        state = Common_Get(clip).my_room_clip->judge_player_action_proc(&wpos0, &wpos1, ftr_no);" in hw
   and "Common_Get(clip).my_room_clip != NULL ? Common_Get(clip).my_room_clip->judge_player_action_proc : pc_ofurn_grab_judge;" in hr)
gj = body(dr, "int pc_ofurn_grab_judge(xyz_t* wpos0, xyz_t* wpos1, int ftr_no) {")
ck("C2P2 the outdoor judge answers 0 without an outdoor grab / for another ftr_no, releases on A up, 16-frame push / pull timers, rotation re-armed by a neutral stick",
   "if (!s_grab.active || ftr_no != PCOG_FTR_NO" in gj and 'pc_ofurn_grab_end("A released");' in gj and "s_grab.push_t > 16.0f" in gj and "s_grab.pull_t > 16.0f" in gj
   and "s_grab.allow_rot = 1;" in gj)
ck("C2P3 the grab enters the REAL hold state (mPlib_request_main_hold_type1); A in front = grab, B = pick up (vanilla: B picks furniture up)",
   "mPlib_request_main_hold_type1(game, PCOG_FTR_NO, (face + 2) & 3, &pos)" in dr and "a_trigger ? pc_ofurn_grab_try(game, pl, ux, uz) : pc_net_game_ofurn_pickup(ux, uz)" in dr)
ck("C2P4 a refused step snaps the model back (rollback) with the vanilla bubu puff / a notice; no furniture behaviour code outdoors (no move_proc, no my_room_clip)",
   "pc_ofurn_pres_rollback();" in dr and "eEC_EFFECT_BUBU" in dr and "move_proc" not in dr_code and "my_room_clip" not in dr_code)
ck("C2P5 the renderer draws at the footprint's model position (2x2 on the shared corner) and slides remote moves (102 MOVE)",
   "pc_ofurn_fp_model_pos(fp, ux, uz, &x, &z);" in dr and "pc_net_game_ofurn_take_slide(" in dr and "case PC_OFURN_MK_MOVE: {" in blk)

# ---- docs
rm = rd(ROOT, "docs", "multiplayer-guest-roadmap.md")
cc = rd(ROOT, "docs", "console-commands.md")
td = rd(ROOT, "TODO.md")
ck("DOC2 the roadmap has the C2 design section", "Outdoor furniture C2" in rm)
ck("DOC the roadmap has the C1 design section, console-commands.md documents ops / op / deop / furniture, TODO.md (repo root) lists C1 + what C2 / C3 still need",
   "Outdoor furniture (C1) and the operator list" in rm and "`op <player>`" in cc and "`furniture list" in cc and "Outdoor furniture" in td)

print("-" * 60)
n = sum(1 for _d, ok in results if ok)
print("%d/%d checks passed" % (n, len(results)))
sys.exit(0 if n == len(results) else 1)
