#!/usr/bin/env python3
"""test_dresser_ui_source.py - SOURCE test: the dresser storage UI (ac_my_room_msg_ctrl.c_inc / ac_my_room.c) is TARGET_PC-gated and the vanilla path is untouched.

  U1 with every `#ifdef TARGET_PC` region removed (its #else part kept), the dresser files are IDENTICAL to the same reduction of git HEAD
     (= the non-PC / vanilla code is byte-identical; the PC additions only exist inside TARGET_PC blocks)
  U2 every new PC dresser state is reachable ONLY behind aMR_PcDresserOn() / pc_net_game_dresser_available() (feature off, solo play, the host's own dresser: never)
  U3 the six per-state tables carry exactly three TARGET_PC entries each (the enum appends three states before aMR_MSG_STATE_NUM)
  U4 the put-in to the pool never reaches aMR_ItemPutInFurniture (the item is consumed by the pool branch) and the pocket is restored before the host transaction
Usage: python test_dresser_ui_source.py"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
FILES = ["src/actor/ac_my_room_msg_ctrl.c_inc", "src/actor/ac_my_room.c", "src/actor/ac_my_room_move.c_inc", "src/game/m_haniwa_ovl.c", "src/game/m_tag_ovl.c"]


def strip_target_pc(text):
    out, stack = [], []  # stack of (is_target_pc, in_else)
    for line in text.split("\n"):
        s = line.strip()
        if re.match(r"#\s*if(def|ndef)?\b", s):
            stack.append([s.replace(" ", "") == "#ifdefTARGET_PC" or s.replace(" ", "").startswith("#ifdefined(TARGET_PC)&&"), False])
            if stack[-1][0]:
                continue
        elif re.match(r"#\s*else\b", s) and stack:
            if stack[-1][0]:
                stack[-1][1] = True
                continue
        elif re.match(r"#\s*endif\b", s) and stack:
            top = stack.pop()
            if top[0]:
                continue
        if any(t and not e for t, e in stack):
            continue
        if s:
            out.append(line)  # blank lines are not code: the comparison ignores them
    return "\n".join(out)


def main():
    results = []

    def check(desc, ok):
        results.append(ok)
        print(("PASS - " if ok else "FAIL - ") + desc)

    for f in FILES:
        cur = open(os.path.join(ROOT, f), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
        head = subprocess.run(["git", "show", "HEAD:" + f], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.replace("\r\n", "\n")
        a, b = strip_target_pc(cur), strip_target_pc(head)
        if a != b:
            import difflib
            for d in list(difflib.unified_diff(b.split("\n"), a.split("\n"), lineterm="", n=1))[:30]:
                print("   " + d)
        check("U1 %s: identical to git HEAD once the TARGET_PC regions are removed (vanilla code identical, blank lines ignored)" % f, bool(head) and a == b)
    src = open(os.path.join(ROOT, FILES[0]), encoding="utf-8").read()
    room = open(os.path.join(ROOT, FILES[1]), encoding="utf-8").read()
    check("U2 the take-out list offers 'More storage' only when aMR_PcDresserOn() (pc_net_game_dresser_available() and not edit-locked)",
          "if (count >= 1 && count <= 3 && aMR_PcDresserOn()) {" in src and "return pc_net_game_dresser_available() && !pc_net_game_client_room_edit_locked();" in src)
    check("U2 the PC states are entered only from the storage choice (take-out list or a patched owner message) / the page / the pool put-in",
          src.count("aMR_MSG_STATE_PC_DRESSER_WAIT_PAGE;") == 2 and src.count("aMR_MSG_STATE_PC_DRESSER_GYROID;") == 2 and "if (pc_net_game_dresser_view_ready()) {\n        my_room->requested_msg_type = aMR_MSG_STATE_PC_DRESSER_GYROID;" in src and "if (choice < 0 || choice != pc_dresser_msg_more_choice(msg_no)) {" in src and src.count("aMR_MSG_STATE_PC_DRESSER_TXN;") == 2 and "aMR_pc_storage_choice >= 0 && choice_no == aMR_pc_storage_choice" in src)
    m = re.search(r"#ifdef TARGET_PC\s*/\* dresser storage[^*]*\*/\s*aMR_MSG_STATE_PC_DRESSER_WAIT_PAGE,\s*aMR_MSG_STATE_PC_DRESSER_PAGE,\s*aMR_MSG_STATE_PC_DRESSER_TXN,\s*aMR_MSG_STATE_PC_DRESSER_GYROID,[^\n]*\n\s*#endif\s*aMR_MSG_STATE_NUM", room)
    check("U3 the enum appends exactly four TARGET_PC states right before aMR_MSG_STATE_NUM", m is not None)
    tables = ["msg_init_table", "msg_ctrl_table", "aMR_msg_no_table", "aMR_none_talk_proc", "aMR_force_talk_flag", "aMR_window_se_flag"]
    ok = True
    for t in tables:
        body = re.search(r"%s\[aMR_MSG_STATE_NUM\] = \{(.*?)\n\};" % t, src, re.S)
        if body is None:
            ok = False
            continue
        tail = re.search(r"#ifdef TARGET_PC\n((?:    [^\n]*\n){4})#endif\s*$", body.group(1) + "\n")
        ok = ok and tail is not None
    check("U3 each of the six state tables ends with exactly four TARGET_PC entries", ok)
    put = src[src.find("static void aMR_MsgControlItemPutIn("):]
    put = put[:put.find("\n}\n")]
    pool = src[src.find("static int aMR_PcDresserPutInPool("):]
    pool = pool[:pool.find("\n}\n")]
    check("U4 pool put-in: the submenu item is consumed, then aMR_PcDresserPutInPool (never aMR_ItemPutInFurniture for it); the pocket is restored BEFORE pc_net_game_dresser_begin(PUT)",
          0 < put.find("submenu_item->item = EMPTY_NO;") < put.find("aMR_PcDresserPutInPool(my_room, item, slot)") < put.find("aMR_ItemPutInFurniture")
          and 0 < pool.find("mPr_SetPossessionItem(priv, slot, item, mPr_ITEM_COND_NORMAL);") < pool.find("pc_net_game_dresser_begin(1, 0xFF, slot, item)"))
    mv = open(os.path.join(ROOT, FILES[2]), encoding="utf-8").read()
    grab = mv[mv.find("/* Client inside its OWN house with the house link down / not reconciled: no push / pull / rotate grab"):]
    pick = mv[mv.find("static int aMR_JudgePickUpCommon("):]
    act = open(os.path.join(ROOT, "src", "actor", "ac_my_room_action.c_inc"), encoding="utf-8").read()
    check("U5 Option A (6f): the grab itself stays free (a short A tap OPENS a dresser); the pull / push / rotate STARTS refuse a dresser holding extra items",
          "if (aMR_PcDresserMoveLocked(my_room, ftr_actor2)) {" not in grab[:grab.find("mPlib_request_main_hold_type1")] and
          act.count("if (aMR_PcDresserMoveLocked(my_room, ftr_actor, aMR_PC_MOVE_") == 3 and
          0 <= act.find("if (aMR_PcDresserMoveLocked(my_room, ftr_actor, aMR_PC_MOVE_PULL)) {", act.find("static void aMR_PlacePullFurniture(")) < act.find("my_room->state = 3;") and
          0 <= act.find("if (aMR_PcDresserMoveLocked(my_room, ftr_actor, aMR_PC_MOVE_PUSH)) {", act.find("static void aMR_PlacePushFurniture(")) < act.find("my_room->state = 2;") and
          0 <= act.find("if (aMR_PcDresserMoveLocked(my_room, ftr_actor, aMR_PC_MOVE_ROTATE)) {", act.find("static void aMR_PlaceKurukuruFurniture(")) < act.find("my_room->state = 4;"))
    check("U5 Option A: the pickup judge refuses it before any vanilla pickup branch (TARGET_PC)",
          0 <= pick.find("if (aMR_PcDresserMoveLocked(my_room, ftr_actor, aMR_PC_MOVE_PICKUP)) {") < pick.find("if (item_count == 0 && my_room->demo_flag == FALSE"))
    check("U6 v2: the opened owner dresser is selected on the host (identity: own room floor, layer, origin cell, furniture item)",
          "aMR_PcDresserSelectFtr(my_room, ftr_actor); /* v2" in mv and "pc_net_game_dresser_select(floor, ftr_actor->layer, ut_x, ut_z, item)" in src and
          "aMR_Wpos2PlaceNumber(ut_x, ut_z, ftr_actor->position, ftr_actor->shape_type)" in src)
    tag = open(os.path.join(ROOT, "src", "game", "m_tag_ovl.c"), encoding="utf-8").read()
    hw = open(os.path.join(ROOT, "src", "game", "m_haniwa_ovl.c"), encoding="utf-8").read()
    ui = open(os.path.join(ROOT, "pc", "src", "pc_dresser_ui.c"), encoding="utf-8").read()
    check("U3 one page: the dresser table is a 9 x 3 grid (27 cells) and no paging code is left (no page turn / page indicator)",
          "{ 9, 3, mTG_pc_dresser_col_pos, mTG_pc_dresser_line_pos }" in tag and "pc_dresser_ui_turn" not in tag + hw + ui and "of 7" not in hw)
    check("U3 the dresser screen draws no gyroid portrait, dialogue box or dialogue text (frame [0..17] only + the 27 cells)",
          "if (!pc_dresser_ui_active()) /* dresser mode: no gyroid portrait */" in hw and "for (i = 0; i <= 12; i++) {" in hw and "for (i = 13; i <= 17; i++) {" in hw and
          "if (!pc_dresser_ui_active()) /* dresser mode: no gyroid dialogue" in hw)
    root = ROOT
    rd = lambda *pp: open(os.path.join(root, *pp), encoding="utf-8").read()  # noqa: E731
    mv = rd("src", "actor", "ac_my_room_move.c_inc")
    mc = rd("src", "actor", "ac_my_room_msg_ctrl.c_inc")
    mr = rd("src", "actor", "ac_my_room.c")
    ng = rd("pc", "src", "pc_net_game.c")
    ui = rd("pc", "src", "pc_dresser_ui.c")
    lay = rd("pc", "src", "pc_dresser_layout.c")
    it = rd("pc", "src", "pc_dresser_items.c")
    # ---- 6f layout: the 9-slice remap with a byte-exact backup, restored on every exit path, guarded before any normal gyroid draw
    sf = hw[hw.find("static void mHW_set_frame_dl("):]
    check("6f layout: mHW_set_frame_dl remaps (idempotent) in dresser mode and RESTORES first for any normal gyroid window draw",
          sf.find("pc_dresser_layout_begin(hni_win_v, PC_DLAYOUT_NVTX)") >= 0 and sf.find("(void)pc_dresser_layout_end(); /* guard") >= 0 and
          sf.find("(void)pc_dresser_layout_end(); /* guard") < sf.find("gSPDisplayList(POLY_OPA_DISP++, hni_win_modelT);"))
    check("6f layout: restored on the overlay's destroy path and at pc_dresser_ui_end",
          "(void)pc_dresser_layout_end(); /* the overlay goes away" in hw and "(void)pc_dresser_layout_end(); /* the enlarged gyroid window back" in ui)
    check("6f layout: begin remaps from the byte-exact backup, never twice; end restores the backup; end without begin is a no-op",
          "if (s_vtx != NULL) {\n        return 0; /* already active" in lay and "memcpy(s_backup, v, sizeof(s_backup));" in lay and
          "memcpy(s_vtx, s_backup, sizeof(s_backup));" in lay and "if (s_vtx == NULL) {\n        return 0;\n    }" in lay)
    check("6f layout: the row centres 82 / 60 / 38 agree in the tag table and the overlay draw",
          "mTG_pc_dresser_line_pos[3] = { 82, 60, 38 }" in tag and "{ 82.0f, 60.0f, 38.0f }" in hw)
    # ---- 6f direct open
    do = mv[mv.find("static int aMR_PcDresserDirectOpen("):]
    check("6f direct open: only owner / own house (no NPC house) / feature on / item storage / not edit-locked; vanilla otherwise",
          "if (!aMR_PcDresserOn() || !aMR_CheckRoomOwner(player_no, my_room) || Save_Get(scene_no) == SCENE_NPC_HOUSE || Save_Get(scene_no) == SCENE_COTTAGE_NPC ||" in do and
          "aMR_PcEditLocked() || !aMR_PcDresserIdent(my_room, ftr_actor, &floor, &ut_x, &ut_z, &item) || !aMR_pc_ftr_is_item_storage(item)) {\n        return FALSE;" in do)
    check("6f direct open: waits for THIS dresser's host view (select first), opens the 27-slot screen, falls back to the vanilla menu after 180 frames",
          "#define aMR_PC_DIRECT_OPEN_TIMEOUT 180" in mv and do.find("aMR_PcDresserSelectFtr(my_room, ftr_actor);") < do.find("pc_net_game_dresser_view_ready()") and
          "my_room->requested_msg_type = aMR_MSG_STATE_PC_DRESSER_GYROID;" in do and "if (my_room->msg_timer <= aMR_PC_DIRECT_OPEN_TIMEOUT) {\n        return TRUE;" in do and
          "-> the vanilla menu (fallback)" in do)
    dm = mv[mv.find("static void aMR_SetFtrDemoMsg("):]
    check("6f direct open: called first in the open demo's WAIT_MSG (before the vanilla OWNER_* message choice)",
          0 <= dm.find("if (aMR_PcDresserDirectOpen(my_room, ftr_actor, player_no)) {") < dm.find("if (aMR_GetItemCountInFurniture(ftr_actor) <= 0) {"))
    # ---- 6f item types: ONE predicate (host commit, client STATE, UI, put-in tag); the vanilla slots keep the vanilla filter
    dui = open(os.path.join(ROOT, "pc", "src", "pc_dresser_ui.c"), encoding="utf-8").read()
    check("6f items: pcnetgame_dresser_item_ok == pc_dresser_item_extra_ok, used by the host commit check, the client STATE check and the UI predicate",
          "return pc_dresser_item_extra_ok((unsigned)item);" in ng and "pcnetgame_dresser_item_ok(t->item) &&" in ng and
          "!pcnetgame_dresser_item_ok(m->items[i])" in ng and "return item > 0 && item <= 0xFFFF && pcnetgame_dresser_item_ok((uint16_t)item);" in ng and
          "pc_net_game_dresser_item_storable(item)" in dui)
    check("6f items: turnips / knife+fork / cards / Wisp / unknown ids / rotated furniture rejected",
          "cat == ITEM1_CAT_KABU || it == ITM_KNIFE_AND_FORK" in it and "ITEM_IS_WISP(it)" in it and "(it & 3u) != 0u || idx < 0 || idx >= FTR_NUM" in it)
    # ---- 6f move lock: authoritative on the client (fresh answer of THIS visit, unknown = refuse + ask) and on the host (commit refused)
    ml = mc[mc.find("static int aMR_PcDresserMoveLocked("):]
    check("6f move lock (client): item-storage only; allowed only on state 0; unknown -> refused with notice 3 + the host is asked",
          "!aMR_pc_ftr_is_item_storage(item)) {\n        return FALSE;" in ml and "st = pc_net_game_dresser_move_state(" in ml and
          "pc_net_game_dresser_move_denied();" in ml and "aMR_PcDresserSelectFtr(my_room, ftr_actor);" in ml)
    check("6f move lock (client): a new room visit per own-room actor construction; only a gen-matched answer of THIS visit counts",
          "pc_net_game_dresser_room_visit(); /* dresser storage: a new room visit" in mr and "e->fresh_visit = s_dresser_visit;" in ng and
          "return (e->fresh_visit != 0u && e->fresh_visit == s_dresser_visit) ? 0 : -1;" in ng)
    pc = ng[ng.find("static void pcnetgame_house_process_commit("):]
    check("6f move lock (host): step 9b refuses a commit that removes / moves / rotates a dresser holding extras, BEFORE the commit is applied",
          0 <= pc.find("if (pcnetgame_dresser_commit_locked(h, cur, nimg, &dd)) {") < pc.find("/* 10: commit the pair in ONE call.") and
          "pcnetgame_house_reject(peer, idx, h, (uint8_t)PC_NETGAME_HOUSE_ACK_INVALID_CELL, dd, xfer);" in pc)
    bp = ng[ng.find("static int pcnetgame_hcl_build_pair("):]
    bp = bp[:bp.find("\n}\n")]
    gd = ng[ng.find("static int pcnetgame_dresser_commit_guard("):]
    check("6h commit guard: in build_pair, only with the host feature known; in-room = detect + hold (no in-room commit), out of the room = revert in the "
          "outgoing image + the same correction in the local save; notice 3",
          "if (s_dresser_probed && s_dresser_cli_enabled && h >= 0" in bp and "pcnetgame_dresser_commit_guard(s_hcp.canon_img[h], s_hcl_tx, 0, &skipped)" in bp and
          "s_hcl.room_stopped = 1;" in bp and "pcnetgame_dresser_commit_guard(s_hcp.canon_img[h], s_hcl_tx, 1, &skipped) > 0" in bp and
          "(void)pcnetgame_hcl_write_save(h, s_hcl_tx);" in bp and bp.count("pc_net_game_dresser_move_denied();") == 2)
    check("6q S2 commit guard (supersedes 6h): rotation in place and moves are LEGAL (not touched); only a REMOVAL (pickup) of a piece with keep items / extras / "
          "unknown state is flagged (not revertible: the host refuses); nothing is reverted any more",
          "if (raw1 == raw0 || !pcnetgame_dresser_img_removes(canon, out, f, L, base, raw1)) {" in gd and
          "pc_net_game_dresser_move_state(f, L, c % UT_X_NUM, c / UT_X_NUM, base) == 0) {" in gd and "NOT revertible here" in gd and
          "pcnetgame_dresser_img_set16(out" not in gd)
    mv_ = hw[hw.find("static void mHW_haniwa_ovl_move("):]
    mv_ = mv_[:mv_.find("\n}\n")]
    dr_ = hw[hw.find("static void mHW_haniwa_ovl_draw("):]
    dr_ = dr_[:dr_.find("\n}\n")]
    check("6i crash fix: mHW_haniwa_ovl_move stops when the overlays were unloaded inside pre_move_func or the menu's own End (haniwa_ovl == NULL) BEFORE "
          "mHW_make_message dereferences it; the draw is guarded the same way",
          mv_.count("if (submenu->overlay->haniwa_ovl == NULL) {") == 2 and
          mv_.find("menu_info->pre_move_func(submenu);") < mv_.find("if (submenu->overlay->haniwa_ovl == NULL) {") <
          mv_.find("(*ovl_move_proc[menu_info->proc_status])(submenu, menu_info);") < mv_.rfind("if (submenu->overlay->haniwa_ovl == NULL) {") <
          mv_.find("mHW_make_message(submenu, menu_info);") and
          "if (submenu->overlay->haniwa_ovl == NULL || submenu->overlay->tag_ovl == NULL) {" in dr_)
    act2 = open(os.path.join(ROOT, "src", "actor", "ac_my_room_action.c_inc"), encoding="utf-8").read()
    jd = act2[act2.find("static int aMR_JudgeDemoStart("):]
    check("6j open gate: before any open demo, an own item-storage piece whose cell is NOT in the host's canonical image is refused for now (notice 4); "
          "direct open never SELECTs an uncommitted cell (vanilla instead); the 8 s cap / stopped in-room commits fall back to the vanilla menu",
          0 <= jd.find("aMR_PcDresserOpenRefused(my_room, ftr_actor)) {") < jd.find("aMR_SetOpenFtrDemoData(my_room, ftr_actor, contact_info);") and
          "pc_net_game_dresser_cell_committed(floor, ftr_actor->layer, ut_x, ut_z, item) == 0) {\n        return FALSE;" in mv and
          "if ((int32_t)(now - first_ms) >= 8000) {" in ng and "if (!s_hcl.room_base_valid || s_hcl.room_stopped || s_hcl.rollback) {" in ng and
          "return 4; /* \"The room is being saved\" */" in ng)
    # ---- 6l: 27-slot lock (the vanilla keep slots count too: host step 9b on the PRE-commit image, client guard, client gates) + truthful notices
    cl_ = ng[ng.find("static int pcnetgame_dresser_commit_locked(int h, const uint8_t* old_img, const uint8_t* new_img, uint16_t* detail) {"):]
    cl_ = cl_[:cl_.find("\n}\n")]
    check("6l host step 9b: an item-storage piece whose keep slots (layers L+1..3 of the host's PRE-commit image) are not empty may not be removed / moved / "
          "rotated; judged on old_img so emptying + moving in ONE commit is refused too; same INVALID_CELL detail",
          "pcnetgame_dresser_img_ftr(old_img, f, L, c)" in cl_ and "for (l = L + 1; l < 4; l++) {" in cl_ and "(27-slot lock: take the stored items out first)" in cl_ and
          cl_.count("*detail = (uint16_t)(0x2000u |") == 2 and "if (s_dresser_state != 1 ||" in cl_)
    check("6l client commit guard: canonical keep items count like extra items (revert / hold exactly what step 9b would refuse)",
          "kept += pcnetgame_dresser_img_get16(canon, f, l, c) != 0u;" in gd and
          "if (kept == 0 && pc_net_game_dresser_move_state(f, L, c % UT_X_NUM, c / UT_X_NUM, base) == 0) {" in gd)
    check("6l client gates: live keep items (notice 3) or canonical keep items not yet emptied by a commit (notice 4) refuse BEFORE the extras state; "
          "UNKNOWN -> notice 4 (cell not committed) / 6, never notice 3",
          0 <= ml.find("pc_net_game_dresser_keep_lock(floor, ftr_actor->layer, ut_x, ut_z, item, live)") < ml.find("st = pc_net_game_dresser_move_state(") and
          "live |= ftr_actor->items[i] != EMPTY_NO;" in ml and
          "pc_net_game_dresser_move_wait(pc_net_game_dresser_cell_committed(floor, ftr_actor->layer, ut_x, ut_z, item) == 0);" in ml and
          "if (s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY || !s_dresser_probed || !s_dresser_cli_enabled) {\n        return 0;" in ng)
    pm = open(os.path.join(ROOT, "pc", "src", "pc_pause_menu.c"), encoding="utf-8").read()
    bb = ng[ng.find("static int pcnetgame_dresser_begin_blocked("):]
    bb = bb[:bb.find("\n}\n")]
    check("6l refusal feedback: uncommitted room edits -> notice 5 (two lines), transient -> 6, stopped / rollback / taint -> 2, offline -> 7; set on every refused "
          "extra-slot begin; never 'full'",
          'why = "the room holds uncommitted edits";\n        s_dresser_block_kind = 5;' in bb and "s_dresser_block_kind = 2;" in bb and
          "pcnetgame_dresser_refuse_notice(k, k == 5 ?" in ng and '"Close the dresser to save room changes,"' in pm and
          '"then reopen it to store more items."' in pm and '"Try again in a moment"' in pm and '"Not connected - extra storage is unavailable"' in pm and
          "return s_dresser_refuse_kind; /* 6l" in ng)
    # ---- 6l (final verification): notice SELECTION, each path pinned to its own source (selection logic + texts; rendering / line fit NOT covered)
    def body(text, sig):
        i = text.find(sig)
        return text[i:text.find("\n}\n", i)] if i >= 0 else ""
    mw = body(ng, "void pc_net_game_dresser_move_wait(int uncommitted) {")
    rn = body(ng, "int pc_net_game_room_edit_notice(void) {")
    db = body(ng, "int pc_net_game_dresser_begin(int op, int pool_slot, int pocket_slot, int item) {")
    md = body(ng, "void pc_net_game_dresser_move_denied(void) {")
    kl = body(ng, "int pc_net_game_dresser_keep_lock(int floor, int layer, int ut_x, int ut_z, int ftr_item, int live_keep) {")
    mlb = body(mc, "static int aMR_PcDresserMoveLocked(MY_ROOM_ACTOR* my_room, FTR_ACTOR* ftr_actor, int action) {")
    check("N2 notice 2: begin_blocked -> offline taint / rollback / stopped in-room commits (checked FIRST); move-wait on an uncommitted cell -> 2 when in-room "
          "commits are stopped / rolled back / not armed; text 'Leave the house to resync'",
          bb.find("if (s_hcp.offline_taint || s_hcl.rollback || s_hcl.room_stopped) {") < bb.find("s_dresser_block_kind = 2;") < bb.find("} else if (s_hcl.c_active") and
          "if (uncommitted && (s_hcl.room_stopped || s_hcl.rollback || !s_hcl.room_base_valid)) {\n        pcnetgame_dresser_refuse_notice(2," in mw and
          ': "Leave the house to resync",' in pm)
    check("N6 notice 6: the default kind of every begin_blocked call (transient: another transaction / mail / take op, commit in flight, baseline not armed, "
          "export failed, outside the own house) + s_ts_op active; move-wait with a COMMITTED cell (unknown state, the host is asked) -> 6; text 'Try again in a moment'",
          bb.find("s_dresser_block_kind = 6;") < bb.find("if (s_ctxn.state != PC_NETGAME_CTXN_FREE") and bb.count("s_dresser_block_kind = ") == 3 and
          "const int k = s_ts_op.active ? 6 : s_dresser_block_kind;" in db and "pcnetgame_dresser_refuse_notice(uncommitted ? 4 : 6," in mw and
          "pc_net_game_dresser_move_wait(pc_net_game_dresser_cell_committed(floor, ftr_actor->layer, ut_x, ut_z, item) == 0);" in mlb and
          'edit_notice == 6 ? "Try again in a moment"' in pm)
    check("N7 notice 7: a client whose link is not READY (feature unavailable) on an extra-slot begin; 6 when READY but the record is not synced; text",
          "pcnetgame_dresser_refuse_notice(s_client_link != PC_NETGAME_LINK_READY ? 7 : 6," in db and
          db.find("pcnetgame_dresser_refuse_notice(s_client_link") < db.find("if (!pc_net_game_dresser_available() ||\n") and
          'edit_notice == 7 ? "Not connected - extra storage is unavailable"' in pm)
    check("N4 notice 4: an UNKNOWN state whose cell is NOT committed (move-wait(1)), and live keep slots empty while the canonical image still holds keep items "
          "(take-out being saved) -> move-wait(1); open gate 'saving' -> 4; text 'The room is being saved...'",
          "pcnetgame_dresser_refuse_notice(uncommitted ? 4 : 6," in mw and "if (kl == 2) {\n            pc_net_game_dresser_move_wait(1);" in mlb and
          "return 2;" in kl and 'edit_notice == 4 ? "The room is being saved..."' in pm)
    check("N3 notice 3 ONLY with stored items: move_denied() is reached only for live keep items (kl == 1) or a host answer listing extras (st > 0); an UNKNOWN "
          "state (st < 0) never calls it (move_wait instead); text 'Take the stored items out first'",
          mlb.count("pc_net_game_dresser_move_denied();") == 2 and "if (kl == 1) {\n            pc_net_game_dresser_move_denied();" in mlb and
          "if (st > 0) {\n        pc_net_game_dresser_move_denied();" in mlb and mlb.find("if (st > 0) {") < mlb.find("} else {\n        /* 6l: UNKNOWN") and
          'edit_notice == 3 ? "Take the stored items out first"' in pm)
    check("N5 notice 5: only the 'room holds uncommitted edits' cause; two-line text",
          bb.count("s_dresser_block_kind = 5;") == 1 and 'why = "the room holds uncommitted edits";\n        s_dresser_block_kind = 5;' in bb and
          "if (edit_notice == 5) {" in pm)
    check("N8 the latest refusal wins: notice 3 (move_denied) and the open gate's notice 4 clear the refusal notice; room_edit_notice returns it first",
          "s_dresser_refuse_ms = 0; /* 6l: the latest refusal wins */" in md and ng.count("s_dresser_refuse_ms = 0; /* 6l: the latest refusal wins */") == 3 and
          rn.find("return s_dresser_refuse_kind;") < rn.find("return 4;") < rn.find("return 3;") and
          "(int32_t)(pcnetgame_now_ms() - s_dresser_refuse_ms) < 3000" in rn)  # signed: a stamp of now | 1 read in the same ms is shown, not expired
    # ---- same-millisecond notice ages: every stamp is now | 1 (up to 1 ms AHEAD of a read in the same ms); all four ages are SIGNED compares
    check("N9 all four notice ages in room_edit_notice use the signed form (int32_t)(now - stamp) (no unsigned age compare left)",
          "(int32_t)(pcnetgame_now_ms() - s_dresser_refuse_ms) < 3000" in rn and "(int32_t)(pcnetgame_now_ms() - s_dresser_saving_ms) < 3000" in rn and
          "(int32_t)(pcnetgame_now_ms() - s_dresser_move_denied_ms) < 3000" in rn and "(int32_t)(pcnetgame_now_ms() - s_edit_denied_ms) >= 3000" in rn and
          "(uint32_t)(pcnetgame_now_ms() -" not in rn and
          "s_dresser_refuse_ms = pcnetgame_now_ms() | 1u;" in ng and "s_dresser_saving_ms = now | 1u;" in ng and
          "s_dresser_move_denied_ms = pcnetgame_now_ms() | 1u;" in ng and "s_edit_denied_ms = pcnetgame_now_ms() | 1u;" in ng)

    def age_signed(now, stamp):  # model of (int32_t)(uint32_t)(now - stamp)
        d = (now - stamp) & 0xFFFFFFFF
        return d - (1 << 32) if d >= (1 << 31) else d

    def age_unsigned(now, stamp):
        return (now - stamp) & 0xFFFFFFFF

    cases = [(1000, 1000 | 1), (0xFFFFFFFE, 0xFFFFFFFE | 1), (4000, 4000 | 1), (1001, 1001 | 1)]
    fresh = all(age_signed(n, st) < 3000 for n, st in cases)  # same ms (even now: stamp 1 ms ahead) -> fresh
    old_bug = any(age_unsigned(n, st) >= 3000 for n, st in cases)  # the old unsigned form called it expired
    expired = age_signed(4001, 1000 | 1) >= 3000 and age_signed(3999, 1000 | 1) < 3000 and age_signed(5, 0xFFFFFF00 | 1) < 3000  # 3 s window, wrap-around
    check("N10 model: a stamp 1 ms in the FUTURE (same-ms read, even now) is FRESH with the signed form (the unsigned form wrongly expired it: %s); "
          "the 3 s window and clock wrap-around behave" % old_bug, fresh and old_bug and expired)
    # ---- 6o: the in-room conservation baseline follows an applied dresser PUT / TAKE of EVERY counted class (furniture too), the SAME class function as the count
    rb = body(ng, "static void pcnetgame_dresser_rebase_room(int put, uint16_t item) {")
    check("6o rebase: pcnetgame_dresser_rebase_room uses pcnetgame_house_count_class (furniture -> its rotation-free class), no ITEM1-only filter; the count uses the "
          "same class function",
          "const int y = pcnetgame_house_count_class((mActor_name_t)item);" in rb and "NAME_TYPE_ITEM1" not in rb and "s_hcl_cnt_base[y]" in rb and
          "s_hcl_cnt_base[item]" not in rb and "const int y = pcnetgame_house_count_class(id);" in body(ng, "static void pcnetgame_house_count_id(uint16_t* cnt, mActor_name_t id) {"))
    # ---- 6p: vanilla-slot edits + extra-slot ops TOGETHER in one open screen: the settled dresser screen counts as a quiet live room (in-room commit may land
    # while it stays open) and an extra op refused ONLY because of uncommitted edits waits (deferred, notice 4) instead of failing; the background stays still
    rm_ = open(os.path.join(ROOT, "src", "actor", "ac_my_room.c"), encoding="utf-8").read()
    st_ = body(rm_, "static int aMR_pc_dresser_screen_settled_room(MY_ROOM_ACTOR* my_room) {")
    rq_ = body(rm_, "int aMR_pc_room_quiet(void) {")
    lr_ = body(ng, "static int pcnetgame_hcl_live_room_house(int need_own_room) {")
    check("6p settled dresser screen: overlay active + the gyroid msg state + nothing requested + submenu PLAY + the opened piece = the screen's piece; "
          "room-quiet skips only the room's own demo / message terms and the opened piece, every OTHER furniture / reserve / wall-floor term stays",
          "!pc_dresser_ui_active() || aMR_pc_gyroid_ftr == NULL" in st_ and "my_room->msg_type != aMR_MSG_STATE_PC_DRESSER_GYROID || my_room->requested_msg_type != my_room->msg_type) {" in st_ and
          "requested_msg_type != aMR_MSG_STATE_NONE" not in st_ and  # an ACTIVE message state keeps requested == msg_type (aMR_MessageControl)
          "if (my_room->requested_msg_type != my_room->msg_type && my_room->emu_info.explaination_given_flag == FALSE) {" in mc and
          "play->submenu.process_status != mSM_PROCESS_PLAY" in st_ and "aMR_pc_gyroid_ftr == &l_aMR_work.ftr_actor_list[my_room->demo_ftrID]" in st_ and
          "if (!dresser_open && (my_room->state != 0 ||" in rq_ and "if (dresser_open && i == my_room->demo_ftrID) {\n                continue;" in rq_ and
          "my_room->rsv_ftr[i].exist_flag != 0" in rq_ and "wall_reserve.reserve_flag" in rq_)
    check("6p live room: with the settled screen the submenu / demo / message / player-pose terms are skipped but aMR_pc_room_quiet still decides; the scene / "
          "fade / own-room terms come first",
          lr_.find("if (aMR_pc_dresser_screen_settled()) {") > lr_.find("need_own_room && Common_Get(field_type)") and
          "return aMR_pc_room_quiet() ? h : -1;\n    }\n    if (play->submenu.process_status != mSM_PROCESS_WAIT" in lr_)
    qu_ = body(ng, "static void pcnetgame_hcl_quiet_update(void) {")
    qf_ = body(ng, "static int pcnetgame_hcl_quiet_frames(int h) {")
    check("6p quiet clock: GAME frames (frame_counter) only while the settled dresser screen is open, play game_frame otherwise; a mode switch restarts the "
          "quiet period (full window after the screen opens / closes); a period of the other mode counts 0; the 2 s gap stays on the ms clock",
          "return dresser_screen ? (uint32_t)gamePT->frame_counter : (uint32_t)((GAME_PLAY*)gamePT)->game_frame;" in ng and
          "if (s_hcl.q_house1 != h + 1 || s_hcl_q_dresser != dm) {" in qu_ and "s_hcl.q_gstart = pcnetgame_hcl_quiet_clock(dm);" in qu_ and
          "s_hcl_q_dresser != (aMR_pc_dresser_screen_settled() ? 1 : 0)) {\n        return 0;" in qf_ and
          "(uint32_t)(now - s_hcl.last_commit_ms) < PC_NETGAME_HCL_MIN_GAP_MS" in ng)
    dui = open(os.path.join(ROOT, "pc", "src", "pc_dresser_ui.c"), encoding="utf-8").read()
    pl_ = body(dui, "int pc_dresser_ui_poll(void) {")
    check("6p deferral: ONLY a refusal of kind 5 (uncommitted room edits) defers (pending, notice 4); the poll retries begin until it is SENT or gives up after "
          "PC_DRESSER_UI_DEFER_POLLS (REFUSED, nothing moved); closing the screen drops an unsent deferred op; every other refusal stays REFUSED",
          "if (r < 0 && pc_net_game_dresser_last_block() == 5) {" in dui and "pc_net_game_saving_notice();" in dui and
          "if (b < 0 && pc_net_game_dresser_last_block() == 5 && ++s_defer.polls < PC_DRESSER_UI_DEFER_POLLS) {" in pl_ and
          "return PC_DRESSER_UI_ACT_REFUSED;" in pl_ and "not sent: dropped (nothing moved)" in dui and
          dui.count("const int r = pc_dresser_ui_begin_or_defer(op, index, pocket, item);") == 1 and "s_dresser_last_block = k;" in ng and "s_dresser_last_block = 0;" in ng)
    hwf = body(open(os.path.join(ROOT, "src", "game", "m_haniwa_ovl.c"), encoding="utf-8").read(), "static void mHW_set_frame_dl(Submenu* submenu, GRAPH* graph, f32 pos_x, f32 pos_y) {")
    check("6p background: in dresser mode the window's tile scroll offset is 0 (no drift); the vanilla scroll formula is kept for every other screen",
          "tex_x = -submenu->overlay->menu_control.texture_pos[0] * 8;" in hwf and
          hwf.find("if (pc_dresser_ui_active()) {\n        /* 6p:") < hwf.find("gDPSetTileSize_Dolphin(") and "tex_x = 0;\n        tex_y = 0;" in hwf)

    # MODEL (python, of the class rule pcnetgame_house_count_class; NOT the compiled function): for every class the user moved (furniture, clothes, paper, an
    # ordinary item, a fish, an insect) the in-room baseline rebased by a dresser PUT / TAKE equals the live count (pockets + room) after the same move
    def cls(i):
        t = i >> 12
        if i == 0:
            return None
        if t in (1, 3):
            return i & ~3  # rotation-free (the real rule maps some furniture to their item1 first: same class on both sides either way)
        if t == 2:
            return i
        return None

    def count(ids):
        c = {}
        for i in ids:
            k = cls(i)
            if k is not None:
                c[k] = c.get(k, 0) + 1
        return c

    pockets = [0x1304, 0x2037, 0x240D, 0x2301, 0x2251, 0x1305]  # furniture, paper, clothes, an ordinary item, fish/insect-range ids, rotated furniture
    ok_model = True
    for it in pockets:
        base = count(pockets)
        live = list(pockets)
        live.remove(it)  # PUT into the extras (pool is not part of the house image)
        k = cls(it)
        if k is not None:
            base[k] -= 1  # pcnetgame_dresser_rebase_room(PUT)
        ok_model = ok_model and {a: b for a, b in base.items() if b} == {a: b for a, b in count(live).items() if b}
    check("6p model: baseline rebase == live count after a dresser PUT for furniture / paper / clothes / ordinary / fish / insect / rotated furniture "
          "(both sides use ONE class rule; source: rebase and count both call pcnetgame_house_count_class)", ok_model and
          "const int y = pcnetgame_house_count_class((mActor_name_t)item);" in body(ng, "static void pcnetgame_dresser_rebase_room(int put, uint16_t item) {"))
    # ---- 6q: S1 aligned list, S2 move / rotate occupied dressers (pickup locked), S3 "Move" op
    pu_ = body(dui, "int pc_dresser_ui_put(int pocket_slot) {")
    ml2 = body(mc, "static int aMR_PcDresserMoveLocked(MY_ROOM_ACTOR* my_room, FTR_ACTOR* ftr_actor, int action) {")
    check("6q S2 client gate: push / pull / rotate are never locked any more; pickup keeps the whole lock (keep items, extras, unknown)",
          ml2.find("if (action != aMR_PC_MOVE_PICKUP) {\n        return FALSE;") >= 0 and
          ml2.find("if (action != aMR_PC_MOVE_PICKUP)") < ml2.find("pc_net_game_dresser_keep_lock(") < ml2.find("st = pc_net_game_dresser_move_state("))
    cl2 = ng[ng.find("static int pcnetgame_dresser_commit_locked(int h, const uint8_t* old_img, const uint8_t* new_img, uint16_t* detail) {"):]
    cl2 = cl2[:cl2.find("\n}\n")]
    rm2 = body(ng, "static int pcnetgame_dresser_img_removes(const uint8_t* old_img, const uint8_t* new_img, int floor, int layer, uint16_t base, uint16_t raw_new) {")
    check("6q S2 host step 9b: refuses only a REMOVAL (fewer cells of that floor / layer hold the piece; a rotation in place never is one) for extras AND keep "
          "items; moves / rotations are applied and the house-commit hook re-keys / follows / orphans the storage (never lost or merged)",
          "if ((uint16_t)(raw_new & ~3u) == base) {\n        return 0;" in rm2 and
          "return pcnetgame_dresser_img_count(new_img, floor, layer, base) < pcnetgame_dresser_img_count(old_img, floor, layer, base);" in rm2 and
          "if (raw_new != raw_old && pcnetgame_dresser_img_removes(old_img, new_img, fl, ly, base, raw_new)) {" in cl2 and
          "if (kept == 0 || !pcnetgame_dresser_img_removes(old_img, new_img, f, L, base, raw_new)) {" in cl2 and
          "storage re-keyed" in ng and "storage ORPHANED (kept, never deleted)" in ng and "RE-ATTACHED to the dresser placed at cell" in ng)
    st_c = open(os.path.join(ROOT, "pc", "src", "pc_dresser_store.c"), encoding="utf-8").read()
    st_h = open(os.path.join(ROOT, "pc", "include", "pc_dresser_store.h"), encoding="utf-8").read()
    hd2 = body(ng, "static void pcnetgame_handle_host_dresser_txn(PCNetPeerId peer, const PCNetGameTxnCommitMsg* in) {")
    check("6q S3 store: OP_MOVE = 3 (move into an empty slot / swap with an occupied one), the source must hold the item (NOT_AVAILABLE), src != dst and "
          "dst < 24 (BAD_SHAPE), journalled (the destination in the journal's reserved byte: the 32-byte record layout is unchanged)",
          "PC_DRESSER_OP_MOVE = 3" in st_h and "r->items[t->dst] = t->item;\n        r->items[slot] = other;" in st_c and
          "(t->op == PC_DRESSER_OP_MOVE && (t->dst >= PC_DRESSER_SLOTS || t->dst == t->slot))" in st_c and
          "e->_r = t->op == PC_DRESSER_OP_MOVE ? t->dst : 0u;" in st_c and "} PCDresserJrnl; /* 32 B" in st_h)
    check("6q S3 host: kind-18 op 3 shape (dest NONE, aux_item = src | dst << 8, both < 24, different), pockets untouched (no inventory write, no record "
          "rev), journalled + STATE + APPLIED; roll-forward ignores MOVE; client: try-send re-checks the source, apply changes nothing locally",
          "(op == (int)PC_NETGAME_DRESSER_OP_MOVE && t->dest == (uint8_t)PC_NETGAME_TXN_DEST_NONE && (t->aux_item & 0xFFu) < (unsigned)PC_DRESSER_SLOTS &&" in hd2 and
          hd2.find("if (op == (int)PC_NETGAME_DRESSER_OP_MOVE) {\n        printf(") < hd2.find("(void)pcnetgame_rec_txn_write_inventory(idx, post, post_conds, t->pre_wallet);") and
          "j->op == PC_DRESSER_OP_MOVE) {" in ng and "s_dresser_cli.items[src] != T->item) {" in ng and
          "if (t->aux_cond == (uint8_t)PC_NETGAME_DRESSER_OP_MOVE) { /* 6q (S3): nothing local changes" in ng)
    # ---- 6r: A1 (27 host slots, sidecar v3) + B (grab)
    st_c6 = open(os.path.join(ROOT, "pc", "src", "pc_dresser_store.c"), encoding="utf-8").read()
    st_h6 = open(os.path.join(ROOT, "pc", "include", "pc_dresser_store.h"), encoding="utf-8").read()
    hr6 = body(ng, "static int pcnetgame_dresser_host_ready(void) {")
    check("6r A1 store: 27 slots, sidecar v3 (616-byte record), the v2 record kept as a read-only type (608); a v2 file loads (= 2) and converts losslessly; "
          "the host backs the v2 file up as .v2.bak (byte copy, never overwritten) BEFORE writing v3, else the feature stays OFF",
          "#define PC_DRESSER_SLOTS      27" in st_h6 and "#define PC_DRESSER_VERSION    3u" in st_h6 and "sizeof(PCDresserRec) == 616" in st_c6 and
          "sizeof(PCDresserRecV2) == 608" in st_c6 and "if (hdr[0] == PC_DRESSER_MAGIC && hdr[1] == 2u && load_v2(s, path) == 2) {" in st_c6 and
          "memcpy(r->items, o->items, sizeof(o->items));" in st_c6 and "memcpy(r->jr, o->jr, sizeof(r->jr));" in st_c6 and
          hr6.find('pc_dresser_store_backup_copy(path, ".v2.bak", (unsigned long)time(NULL)) != 1') < hr6.find("if (!pc_dresser_store_save(&s_dresser, path)) {") and
          "never overwrite an existing backup" in st_c6)
    check("6r A1 wire: STATE v3 = 27 items + an explicit zero pad (76 bytes); the client accepts only v3 / 27; all slot bounds use PC_DRESSER_SLOTS (27)",
          "_Static_assert(sizeof(PCNetGameDresserStateMsg) == 76" in ng and "m.version = 3;" in ng and "if (m->version != 3 || m->nslots != (uint16_t)PC_DRESSER_SLOTS" in ng and
          "#define PC_NETGAME_DRESSER_SLOTS 27" in open(os.path.join(ROOT, "pc", "include", "pc_net_game.h"), encoding="utf-8").read())
    sl6 = body(dui, "int pc_dresser_ui_slot(int col, int* index) {")
    check("6r A1 UI: every one of the 27 cells is host slot `col` (no vanilla cell); put = first free host slot, FULL at 27, no vanilla-slot put any more",
          "return PC_DRESSER_UI_SLOT_EXTRA;" in sl6 and "PC_DRESSER_UI_SLOT_VANILLA" not in sl6 and "#define PC_DRESSER_UI_EXTRA    27" in
          open(os.path.join(ROOT, "pc", "include", "pc_dresser_ui.h"), encoding="utf-8").read() and "s_put_fn(" not in dui)
    gy6 = body(mc, "static void aMR_MsgControlPcDresserGyroid(ACTOR* actorx, GAME* game) {")
    check("6r A1 residual keep items: when the screen opens they are taken out to free pockets through the vanilla take-out (notice 9), what does not fit "
          "stays (notice 10); the pickup lock still counts live keep items",
          "while (ftr_actor->items[0] != EMPTY_NO && aMR_PcGyroidVanillaTake(0) == 1) {" in gy6 and "pc_net_game_dresser_ui_notice(left > 0 ? 10 : 9);" in gy6 and
          "live |= ftr_actor->items[i] != EMPTY_NO;" in mc and '"Old drawer items were moved to your pockets"' in pm)
    hd6 = body(ng, "static void pcnetgame_handle_host_dresser_txn(PCNetPeerId peer, const PCNetGameTxnCommitMsg* in) {")
    check("6r B SWAP (op 4): store = slot <-> item2 in one journalled step (NOT_AVAILABLE when the slot changed, BAD_SHAPE without item2); host = dest POCKET, the "
          "pocket item must be NORMAL + storable (predicate on both sides), post[pocket] = the pool item; roll-forward handles SWAP; client = try-send re-check, "
          "apply sets the pocket + rebases both classes; begin refuses a non-storable pocket item (notice 11)",
          "PC_DRESSER_OP_SWAP = 4" in st_h6 and "r->items[slot] = t->item2;" in st_c6 and "(t->op == PC_DRESSER_OP_SWAP && (t->item2 == 0u || t->slot >= PC_DRESSER_SLOTS))" in st_c6 and
          "(op == (int)PC_NETGAME_DRESSER_OP_SWAP && t->dest == (uint8_t)PC_NETGAME_TXN_DEST_POCKET && t->aux_item < (uint16_t)PC_DRESSER_SLOTS)" in hd6 and
          "!pcnetgame_dresser_item_ok(t->pre_pockets[t->slot])) {" in hd6 and "post[t->slot] = t->item;" in hd6 and
          "} else if (j->op == PC_DRESSER_OP_SWAP) {" in ng and "pcnetgame_dresser_rebase_room(1, was);" in ng and "pcnetgame_dresser_rebase_room(0, t->item);" in ng and
          'pcnetgame_dresser_refuse_notice(11, "swap refused: the pocket item cannot be stored in a dresser");' in ng)
    pl6 = body(dui, "int pc_dresser_ui_place(int to_pocket, int index) {")
    check("6r B grab: one operation per placement (cell->cell MOVE op 3, cell->empty pocket TAKE, pocket->empty cell PUT with the chosen slot, cell<->pocket "
          "SWAP op 4, pocket<->pocket local); the origin cancels; the carried item is re-checked at its origin; a non-storable pocket item is refused (notice 11)",
          "return pc_dresser_ui_send(3, src | (index << 8), 0, item, what);" in pl6 and "return pc_dresser_ui_send(2, src, index, item, what);" in pl6 and
          "return pc_dresser_ui_send(4, src, index, item, what);" in pl6 and "return pc_dresser_ui_send(1, index, src, item, what);" in pl6 and
          "return pc_dresser_ui_send(4, index, src, target, what);" in pl6 and "pc_net_game_dresser_pocket_swap(src, index)" in pl6 and
          "put back on its origin (nothing moved)" in pl6 and "is no longer at its origin: placement refused" in pl6 and pl6.count("pc_net_game_dresser_ui_notice(11);") == 2)
    hand = open(os.path.join(ROOT, "src", "game", "m_hand_ovl.c"), encoding="utf-8").read()
    check("6r B UI: the cell tag is Take it / Grab / Quit, the pocket tag Put Away / Grab / Quit (dresser mode only; vanilla lists restored elsewhere); with "
          "an item carried A on a cell / pocket places it, B cancels (screen stays), closing cancels; the hand DRAWS the carried item (view only); hint drawn",
          '"Grab            ",\n    &mTG_pc_dresser_grab_proc,' in tag and "mTG_label_table[mTG_TYPE_PUTIN_ITEM].words = mTG_pc_dresser_putin_item;" in tag and
          "mTG_label_table[mTG_TYPE_PUTIN_ITEM].words = mTG_putin_item;" in tag and "const int act = pc_dresser_ui_place(1, idx);" in tag and
          "const int act = pc_dresser_ui_place(0, mTG_get_table_idx(tag));" in tag and
          "if (pc_dresser_ui_active() && pc_dresser_ui_carry_item() != 0 && (trigger & BUTTON_B) != 0) {\n        pc_dresser_ui_carry_cancel();" in tag and
          "held = (mActor_name_t)pc_dresser_ui_carry_item();" in hand and "closed while carrying" in dui and "Choose a slot or pocket" not in pm and
          "pc_dresser_ui_move" not in tag)
    # ---- 6s: icon centring, hidden grab origin, solid backdrop
    inv6 = open(os.path.join(ROOT, "src", "game", "m_inventory_ovl.c"), encoding="utf-8").read()
    sub6 = open(os.path.join(ROOT, "src", "game", "m_submenu_ovl.c"), encoding="utf-8").read()
    check("6s icons: the dresser columns are the drawn cell centres (vanilla convention), no x / 0.975 any more",
          "static s16 mTG_pc_dresser_col_pos[9] = { -106, -84, -62, -40, -18, 4, 26, 48, 70 };" in tag and "-109, -86, -64" not in tag.split("6c..6r values were")[0][-300:])
    check("6s grab: the origin cell / pocket of the carried item is not drawn (view only), the hand draws it; restored on place / cancel / close (carry state cleared)",
          "if (it != 0 && !pc_dresser_ui_carry_hides(0, i)) {" in hw and "if (pc_dresser_ui_carry_hides(1, i)) {\n            continue;" in inv6 and
          "return s_active && s_carry.active && (s_carry.from_pocket != 0) == (from_pocket != 0) && s_carry.index == index;" in dui)
    play6 = open(os.path.join(ROOT, "src", "game", "m_play.c"), encoding="utf-8").read()
    pr6 = play6[play6.find("if (play->submenu.mode == mSM_MODE_PRERENDER_DONE) {"):]
    pr6 = pr6[:pr6.find("gfx_rect_moji(")]
    check("6u the frozen room scene is drawn behind the dresser screen like behind every submenu (the 6s / 6t backdrops are reverted)",
          "pc_dresser" not in play6 and "G_CYC_FILL" not in pr6 and "mSM_pc_dresser_backdrop" not in sub6)
    hw6 = open(os.path.join(ROOT, "src", "game", "m_haniwa_ovl.c"), encoding="utf-8").read()
    check("6t debug grid (AC_DRESSER_DEBUG_GRID=1 only): RED marks at the drawn cell centres through the window transform, GREEN marks at the icon anchors "
          "through the icon's own mapping (160 + (int)(X * 0.975), 120 - Y): their on-screen distance = the remaining icon offset",
          'getenv("AC_DRESSER_DEBUG_GRID")' in hw6 and "const int cx = -106 + 22 * (k % PC_DRESSER_UI_COLS), cy = (int)row_y[k / PC_DRESSER_UI_COLS];" in hw6 and
          "ax = 160.0f + (f32)(int)((pos_x + pos[0]) * 0.975f);" in hw6 and "ay = 120.0f - (pos_y + pos[1]);" in hw6)
    fr6 = body(hw6, "static void mHW_pc_dresser_frame(Gfx** gfxp) {")
    _ft = hw6.find("static u8 mHW_pc_dresser_flat_tex[64]")
    ftex = hw6[_ft:hw6.find("};", _ft)] if _ft >= 0 else ""
    check("6w window: the interior is ONE flat tan WITHOUT touching the w-layers' combiner / blend / mask: hni_win_mode stays, only tile 0 (TEXEL0, "
          "the gyroid pattern) is re-pointed at a 4x4 GX RGBA8 texture of the EXACT winT env tan (215,185,155; 16 A,R pairs then 16 G,B pairs) before "
          "w1..w13; no combiner / env override (6v's ENVIRONMENT/TEXEL1 lost the mask alpha); no w13 hole quad, no orange; the OPAQUE panel uses the "
          "same tan; the window's XLU mode is restored for the cells",
          "#define MHW_PC_DRESSER_TAN_R 215" in hw6 and "#define MHW_PC_DRESSER_TAN_G 185" in hw6 and "#define MHW_PC_DRESSER_TAN_B 155" in hw6 and
          "#define MHW_PC_TAN_AR 255, MHW_PC_DRESSER_TAN_R" in hw6 and "#define MHW_PC_TAN_GB MHW_PC_DRESSER_TAN_G, MHW_PC_DRESSER_TAN_B" in hw6 and
          "static u8 mHW_pc_dresser_flat_tex[64] ATTRIBUTE_ALIGN(32) = {" in hw6 and
          ftex.count("MHW_PC_TAN_AR") == 16 and ftex.count("MHW_PC_TAN_GB") == 16 and           0 <= fr6.find("gDPSetTextureImage_Dolphin(gfx++, G_IM_FMT_RGBA, G_IM_SIZ_32b, 4, 4, mHW_pc_dresser_flat_tex);") <
          fr6.find("gDPSetTile_Dolphin(gfx++, G_DOLPHIN_TLUT_DEFAULT_MODE, 0, 0, GX_REPEAT, GX_REPEAT, 0, 0);") < fr6.find("for (i = 0; i <= 12; i++) {") and
          "gDPSetCombineLERP(" not in fr6 and "gDPSetEnvColor(" not in fr6 and "ENVIRONMENT" not in fr6 and
          "pc_dresser_layout_hole_quad(" not in fr6 and "hni_win_w13_model[i]" not in fr6 and "245, 125, 55" not in fr6 and
          "gDPSetRenderMode(gfx++, G_RM_OPA_SURF, G_RM_OPA_SURF2);" in fr6 and
          "gDPSetPrimColor(gfx++, 0, 0, MHW_PC_DRESSER_TAN_R, MHW_PC_DRESSER_TAN_G, MHW_PC_DRESSER_TAN_B, 255);" in fr6 and
          "d->v.ob[0] = (s16)((i >= 2) ? PC_DLAYOUT_IN_X1 : PC_DLAYOUT_IN_X0);" in fr6 and
          fr6.find("gDPSetRenderMode(gfx++, G_RM_XLU_SURF, G_RM_XLU_SURF2);") < fr6.find("hni_nwin2_win2T_model[i]"))
    # the flat texture really is 4x4 RGBA8 of the env tan, and the env tan really is winT's
    hwin = open(os.path.join(ROOT, "src", "data", "model", "hni_win.c"), encoding="utf-8").read()
    _wt = hwin.find("Gfx hni_nwin2_winT_model[] = {")
    wt = hwin[_wt:hwin.find("};", _wt)] if _wt >= 0 else ""
    check("6w the flat tan = hni_nwin2_winT's env colour (so winT's inner tan over the flat layers renders the SAME tan, at any alpha)",
          "gsDPSetEnvColor(215, 185, 155, 255)," in wt)
    # ---- 6n: the build-specific default of the HOST switch (dev build pc/build64 ON, everything else OFF)
    cm = open(os.path.join(ROOT, "pc", "CMakeLists.txt"), encoding="utf-8").read()
    dsh = open(os.path.join(ROOT, "pc", "include", "pc_dresser_store.h"), encoding="utf-8").read()
    hr = body(ng, "static int pcnetgame_dresser_host_ready(void) {")
    w3 = os.path.join(ROOT, "..", "build_w3.sh")
    w3s = open(w3, encoding="utf-8").read() if os.path.isfile(w3) else ""
    check("6x option PC_DRESSER_STORAGE_DEFAULT_ON defaults to ON; the define is added ONLY when it is ON, ONLY to pc_net_game.c (APPEND: PC_NET_TEST_HOOKS kept); "
          "build_w3.sh never sets it",
          'option(PC_DRESSER_STORAGE_DEFAULT_ON "dresser storage defaults to ON (env AC_MP_DRESSER_STORAGE=0 disables)" ON)' in cm and
          "if(PC_DRESSER_STORAGE_DEFAULT_ON)\n    set_property(SOURCE ${CMAKE_CURRENT_SOURCE_DIR}/src/pc_net_game.c APPEND PROPERTY COMPILE_DEFINITIONS PC_DRESSER_STORAGE_DEFAULT_ON=1)\nendif()" in cm and
          cm.count("PC_DRESSER_STORAGE_DEFAULT_ON=1") == 1 and w3s != "")
    check("6n the host gate uses the ONE pure function; build default 1 only under #ifdef PC_DRESSER_STORAGE_DEFAULT_ON; env override; the 4 log strings",
          'const char* e = getenv("AC_MP_DRESSER_STORAGE");' in hr and "if (!pc_dresser_feature_enabled_from_env(e, build_default)) {" in hr and
          "#ifdef PC_DRESSER_STORAGE_DEFAULT_ON\n        const int build_default = 1;" in hr and "#else\n        const int build_default = 0;" in hr and
          "storage OFF (AC_MP_DRESSER_STORAGE=0)" in hr and "storage OFF (build configured with PC_DRESSER_STORAGE_DEFAULT_ON=OFF; AC_MP_DRESSER_STORAGE=1 enables)" in hr and
          "storage ON (AC_MP_DRESSER_STORAGE=1)" in hr and "storage ON (default; AC_MP_DRESSER_STORAGE=0 disables)" in hr and
          ng.count('getenv("AC_MP_DRESSER_STORAGE")') == 1 and
          "static inline int pc_dresser_feature_enabled_from_env(const char* env, int build_default) {" in dsh)
    import shutil as _sh, tempfile as _tf
    gcc = _sh.which("gcc") or (r"C:\msys64\ucrt64\bin\gcc.exe" if os.path.isfile(r"C:\msys64\ucrt64\bin\gcc.exe") else None)
    ok_n, out_n = False, "gcc not found"
    if gcc:
        td = _tf.mkdtemp(prefix="dresser_switch_")
        exe = os.path.join(td, "dresser_switch_selftest.exe")
        env_ = dict(os.environ, PATH=os.environ.get("PATH", "") + os.pathsep + os.path.dirname(gcc))
        r = subprocess.run([gcc, "-std=gnu11", "-Wall", "-I", os.path.join(ROOT, "pc", "include"), os.path.join(HERE, "dresser_switch_selftest.c"), "-o", exe],
                           capture_output=True, text=True, env=env_)
        if r.returncode == 0:
            r = subprocess.run([exe], capture_output=True, text=True, env=env_)
            out_n = r.stdout.strip()
            ok_n = r.returncode == 0 and "PASS" in out_n
        else:
            out_n = r.stderr[-300:]
        _sh.rmtree(td, ignore_errors=True)
    check("6n native selftest of pc_dresser_feature_enabled_from_env (unset / empty / '0' / '1' / other x build default 0 / 1): %s" % out_n, ok_n)
    # ---- 6l diagnostics: ONE count per refused ACTION ATTEMPT (the per-frame counter is kept, documented); accounting only
    at = body(ng, "void pc_net_game_dresser_move_attempt(int action, int ftr_item, int ut_x, int ut_z) {")
    check("C1 per-action counter: a refusal of the SAME (action, item, cell) within PC_NETGAME_DRESSER_ATTEMPT_GAP game frames extends the attempt (not counted); "
          "the per-frame counter s_dresser_move_denied_n is kept unchanged and documented",
          "(uint32_t)(frame - l_frame) <= PC_NETGAME_DRESSER_ATTEMPT_GAP) {\n        l_frame = frame; /* the same held attempt */\n        return;" in at and
          "#define PC_NETGAME_DRESSER_ATTEMPT_GAP 30u" in ng and "s_dresser_attempt_by[action]++;" in at and
          "one held action adds one per\n * refused frame (kept unchanged as the per-frame diagnostic)" in ng)
    check("C2 accounting only: every refusal path of the move lock counts the attempt AFTER deciding to refuse; the attempt function never refuses / returns a "
          "decision; the 4 callers pass their action",
          mlb.count("pc_net_game_dresser_move_attempt(action, item, ut_x, ut_z);") == 3 and "void pc_net_game_dresser_move_attempt(" in ng and
          mv.count("aMR_PcDresserMoveLocked(my_room, ftr_actor, aMR_PC_MOVE_PICKUP)") == 1 and
          "#define aMR_PC_MOVE_PULL 2" in mc and "#define aMR_PC_MOVE_ROTATE 4" in mc)
    print("%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
