#!/usr/bin/env python3
"""test_weeds_src.py - WEEDS (v8 unreleased) source audit (NO game process is launched): host-authoritative WEED_PULL (FIELD_ACTION kind 10)
and FLOWER_TRAMPLE (kind 11), the client seams that used to mutate the town locally, and the Wisp clear_grass client gate.

What is asserted (each check names the property):
  * the wire: kinds 10 / 11 are the pinned C defines and lib constants, the 76-byte request / 12-byte result structs are untouched, the dispatch
    table routes them to the WEEDS adapters (the reserved stub is gone), and a non-zero tag on them is BAD_SHAPE (no tag case in the X3 router);
  * the host: validators read-only and gated (host world ready, requester context IN_TOWN, tile not reserved, tile holds a weed / flower using
    the SAME IS_ITEM_GRASS / IS_ITEM_FLOWER ranges as the vanilla seams, synced position valid + the shared reach check, host-local never routed);
    the commit writes ONLY pcfa_set_tile(EMPTY_NO) (vanilla's mapping) and nothing is granted (no mPr_* / inventory / Save write);
  * the client: the request function is role + scene + READY gated (0 vanilla outside the town, 1 not READY = no-op, 2 sent), dedupes a queued
    same-tile request, writes no tile; the RESULT branch only logs; the pull seam skips ONLY fly_entry_proc when handled (mud / sound /
    vibration unconditional); the trample seam keeps the RANDOM(4) draw first, skips ONLY fade_entry_proc + mISL counters when handled;
  * the host's own pull / trample (role != CLIENT) is the unmodified vanilla path;
  * Wisp: Save clear_grass is not set on a READY client;
  * test hooks: --force-weed-pull is default off, client only, documented; the seed fixtures are the documented tiles.
Usage: python3 test_weeds_src.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from net_spike_lib import check, summary_and_exit_code  # noqa: E402

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


def strip_comments(s):
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", s, flags=re.S))


def main():
    R = []
    net = read("pc/src/pc_net_game.c")
    hdr = read("pc/include/pc_net_game.h")
    lib = read("pc/tools/net_spike/net_spike_lib.py")
    grass = read("src/game/m_player_main_remove_grass.c_inc")
    dash = read("src/game/m_player_main_dash.c_inc")
    ghost = read("src/actor/npc/event/ac_ev_ghost_talk.c_inc")
    ghostc = read("src/actor/npc/event/ac_ev_ghost.c")
    main_c = read("pc/src/pc_main.c")
    plat = read("pc/include/pc_platform.h")
    pl = read("src/game/m_player.c")

    # ---- wire
    check("wire: kinds are the pinned C defines (9 SNOWMAN_BREAK, 10 WEED_PULL, 11 FLOWER_TRAMPLE) and the lib constants match",
          "#define PC_NETGAME_FIELD_ACTION_KIND_WEED_PULL      10u" in net and "#define PC_NETGAME_FIELD_ACTION_KIND_FLOWER_TRAMPLE 11u" in net
          and "#define PC_NETGAME_FIELD_ACTION_KIND_SNOWMAN_BREAK 9u" in net
          and re.search(r"^FIELD_ACTION_KIND_WEED_PULL = 10\b", lib, re.M) and re.search(r"^FIELD_ACTION_KIND_FLOWER_TRAMPLE = 11\b", lib, re.M), R)
    check("wire: the request (76 B) / result (12 B) structs they ride are unchanged (size asserts present, no new message id)",
          "_Static_assert(sizeof(PCNetGameFieldActionRequestMsg) == 76," in net and "_Static_assert(sizeof(PCNetGameFieldActionResultMsg) == 12," in net
          and "PC_NETGAME_MSG_WEED" not in net, R)
    tbl = net[net.index("static const PCNetGameFAHandler s_field_action_handlers[] = {"):]
    tbl = tbl[:tbl.index("};")]
    check("wire: the dispatch table routes 10 / 11 to the WEEDS adapters, the RESERVED_10 stub entry and the stub validator are gone",
          "pcnetgame_fa_validate_adapter_weed_pull" in tbl and "pcnetgame_fa_commit_adapter_weed_pull" in tbl
          and "pcnetgame_fa_validate_adapter_flower_trample" in tbl and "pcnetgame_fa_commit_adapter_flower_trample" in tbl
          and "RESERVED_10" not in net and "static int pcnetgame_fa_validate_stub(" not in net, R)
    x3 = func_body(net, r"\nstatic int pcnetgame_x3_field_action_request\(PCNetPeerId peer, const PCNetGameFieldActionRequestMsg\* far\) \{")
    sw = x3[x3.index("switch (far->kind)"):x3.index("}", x3.index("switch (far->kind)"))] if "switch (far->kind)" in x3 else ""
    check("wire: the X3 router has NO tag case for kinds 10 / 11 (a non-zero tag on them is BAD_SHAPE; an all-zero tag falls through to the table)",
          bool(sw) and "WEED_PULL" not in sw and "FLOWER_TRAMPLE" not in sw and "DIG_BURIED" in sw, R)

    # ---- host
    gc = func_body(net, r"\nstatic int pcnetgame_fa_validate_ground_cover\(")
    gcn = strip_comments(gc)
    idx = [gcn.find(s) for s in ("req->is_host_local", "s_host_world_ready", "PC_NETGAME_CTX_FLAG_IN_TOWN", "pcfa_town_ut_to_acre_tile",
                                  "pcnetgame_host_tile_reserved_by", "pcfa_get_tile", "IS_ITEM_FLOWER(value)", "pc_remote_player_get_last_position",
                                  "pcnetgame_pos_valid", "pcnetgame_field_action_reach_check")]
    check("host validator: gates in order (host-local -> not routed, world ready, requester IN_TOWN, tile resolves, not reserved, tile read, "
          "weed/flower range, synced position, valid position, shared reach envelope)", bool(gc) and all(i >= 0 for i in idx) and idx == sorted(idx), R)
    check("host validator: uses the vanilla ranges (IS_ITEM_GRASS for a weed, IS_ITEM_FLOWER for a flower) and is READ-ONLY (no pcfa_set_*, no Save / mPr_ write)",
          "IS_ITEM_GRASS(value)" in gcn and "pcfa_set_" not in gcn and "mPr_" not in gcn and "Save_Set" not in gcn and "RANDOM" not in gcn, R)
    check("host validator: a host-local request is refused (the host's own pull / trample stays vanilla and reaches clients via the dirty-acre flush)",
          re.search(r"if \(req->is_host_local\) \{\s*return 0;", gcn) is not None, R)
    cm = strip_comments(func_body(net, r"\nstatic void pcnetgame_fa_commit_ground_cover\("))
    check("host commit: the ONLY world write is pcfa_set_tile(.., EMPTY_NO) (vanilla bIT_actor_fly_entry / fade_entry mFI_SetFG_common(EMPTY_NO)); nothing granted",
          cm.count("pcfa_set_tile(acre, tile, (uint16_t)EMPTY_NO)") == 1 and cm.count("pcfa_set_") == 1 and "mPr_" not in cm and "inventory" not in cm
          and "Save_Set" not in cm and "*inout_item = (mActor_name_t)EMPTY_NO" in cm, R)
    check("host adapters: kind 10 -> want_flower 0, kind 11 -> want_flower 1, names WEED_PULL / FLOWER_TRAMPLE",
          re.search(r"pcnetgame_fa_validate_ground_cover\(req, ut_x, ut_z, inout_item, out_acre, out_tile, 0\)", net) is not None
          and re.search(r"pcnetgame_fa_validate_ground_cover\(req, ut_x, ut_z, inout_item, out_acre, out_tile, 1\)", net) is not None
          and '"WEED_PULL");' in net and '"FLOWER_TRAMPLE");' in net, R)
    check("host: no other host-side writer of kinds 10 / 11 (pcfa_set_tile appears once in the whole WEEDS block)",
          net[net.index("/* WEEDS: WEED_PULL (kind 10)"):net.index("/* T0-A: the FIELD_ACTION_REQUEST dispatch table")].count("pcfa_set_tile(") == 1, R)

    # ---- client request function / RESULT
    rq = strip_comments(func_body(net, r"\nstatic int pcnetgame_request_ground_cover\("))
    i_role, i_scene, i_ready, i_range, i_dup, i_full, i_send = (rq.find(s) for s in (
        "s_role != PC_NETGAME_ROLE_CLIENT", "!pcfa_scene_is_town()", "s_client_link != PC_NETGAME_LINK_READY", "ut_x < 0",
        "q->kind == kind", "s_field_action_queue_len >= PC_NETGAME_FIELD_ACTION_QUEUE_DEPTH", "pcnetgame_send_field_action_request(kind, ut_x, ut_z)"))
    check("client request: role CLIENT -> else 0; not the town scene -> 0 (vanilla); not READY -> 1 (G7 no-op); range -> 1; queued duplicate -> 2; queue full -> 1; send -> 2",
          0 <= i_role < i_scene < i_ready < i_range < i_dup < i_full < i_send
          and re.search(r"s_role != PC_NETGAME_ROLE_CLIENT\) \{\s*return 0;", rq) and re.search(r"!pcfa_scene_is_town\(\)\) \{\s*return 0;", rq)
          and re.search(r"PC_NETGAME_LINK_READY\) \{\s*return 1;", rq) and rq.rstrip().endswith("return 2;\n}") , R)
    check("client request: writes no tile / pocket (no pcfa_set_*, mFI_*Set*, Save_Set, mPr_*)",
          not re.search(r"pcfa_set_|mFI_\w*Set|Save_Set|mPr_|inventory", rq), R)
    check("client request: the two public seams forward kinds 10 / 11 and are declared in pc_net_game.h",
          "return pcnetgame_request_ground_cover((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_WEED_PULL" in net
          and "return pcnetgame_request_ground_cover((uint8_t)PC_NETGAME_FIELD_ACTION_KIND_FLOWER_TRAMPLE" in net
          and "int pc_net_game_request_remove_grass(int ut_x, int ut_z);" in hdr and "int pc_net_game_request_trample_flower(int ut_x, int ut_z);" in hdr, R)
    res = func_body(net, r"\nstatic void pcnetgame_handle_client_field_action_result\(")
    k = res.index("PC_NETGAME_FIELD_ACTION_KIND_WEED_PULL ||")
    blk = strip_comments(res[k:res.index("return;", k)])
    check("client RESULT: the WEED_PULL / FLOWER_TRAMPLE branch only logs and returns (no tile / pocket write)",
          "printf(" in blk and not re.search(r"pcfa_set_|apply_tile|mFI_|mPr_|Save_Set", blk), R)

    # ---- pull seam
    fn = func_body(grass, r"\nstatic void Player_actor_ChangeFGNumber_Remove_grass\(ACTOR\* actorx, GAME\* game\) \{")
    g_role = fn.find("pc_net_game_role() == PC_NETGAME_ROLE_CLIENT")
    g_req = fn.find("pc_net_game_request_remove_grass(pc_ut_x, pc_ut_z)")
    g_if = fn.find("if (!pc_net_weed_handled)")
    g_fly = fn.find("fly_entry_proc(*fg_p, grass_pos_p, angle_y)")
    g_mud = fn.find("eEC_EFFECT_DIG_MUD")
    check("pull seam: client gate -> request -> `if (!pc_net_weed_handled)` guards the (single) fly_entry_proc tile write; role CLIENT only",
          0 < g_role < g_req < g_if < g_fly and fn.count("fly_entry_proc(*fg_p") == 1 and "#ifdef TARGET_PC" in fn, R)
    check("pull seam: the mud effect, sound and vibration come AFTER and are NOT guarded (cosmetics stay on every role)",
          g_mud > g_fly and fn.find("Player_actor_sound_zassou_nuku") > g_mud and fn.find("Player_actor_set_viblation_Remove_grass") > g_mud
          and "pc_net_weed_handled" not in fn[g_mud:], R)
    check("pull seam: a coordinate that does not resolve to a unit counts as handled (no local write); the request tile is the verified target tile",
          re.search(r"mFI_Wpos2UtNum\(&pc_ut_x, &pc_ut_z, \*target_pos_p\)\) \{\s*pc_net_weed_handled = pc_net_game_request_remove_grass", fn)
          and "} else {\n                            pc_net_weed_handled = 1;" in fn, R)

    # ---- trample seam
    d = func_body(dash, r"\nstatic int Player_actor_SetEffectRemoveFlower_Dash\(ACTOR\* actor, GAME\* game, s16 angle\) \{")
    r_i, ch_i, req_i = d.find("RANDOM(4)"), d.find("IS_ITEM_FLOWER(name)"), d.find("pc_net_game_request_trample_flower(")
    check("trample seam: RNG order preserved: RANDOM(4) is drawn first, then the flower test, then (client only) the request",
          0 <= r_i < ch_i < req_i and strip_comments(d).count("RANDOM(") == 1 and "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT" in d, R)
    check("trample seam: result 1 (not sent) returns FALSE before any effect; the petal burst is the only thing kept for result 2; the tile write and the "
          "mISL counters run only for result 0 (vanilla: the host / single player / a client outside the town)",
          re.search(r"if \(pc_net_trample == 1\) \{\s*return FALSE;", d) is not None and d.find("eEC_EFFECT_HANATIRI") > d.find("if (pc_net_trample == 1)")
          and d.find("if (pc_net_trample == 0)") > d.find("eEC_EFFECT_HANATIRI") and d.find("fade_entry_proc(name, actor_pos)") > d.find("if (pc_net_trample == 0)"), R)

    # ---- the host's own path stays vanilla: both seams are no-ops unless the role is CLIENT
    check("host / single player: the weed-handled flag and the trample result are 0 unless pc_net_game_role() == CLIENT (vanilla path untouched)",
          "int pc_net_weed_handled = 0;" in fn and "int pc_net_trample = 0;" in d, R)

    # ---- Wisp
    gh = ghost[ghost.index("if (ghost->talk_act == aEGH_TALK_CLEAR_GRASS) {"):]
    gh = gh[:gh.index("aEGH_setup_think_proc")]
    check("Wisp: Save clear_grass is set only when the world is NOT host-authoritative (a READY client's flag would be a stray local one: its growth is skipped)",
          "if (!pc_net_game_world_is_host_authoritative())" in gh and gh.index("world_is_host_authoritative") < gh.index("Save_Set(clear_grass, TRUE)")
          and '#include "pc_net_game.h"' in ghostc, R)
    check("Wisp: the host-side consumption is unchanged (mAGrw_ClearGrass still runs from host growth; the client growth is skipped in mFM_PcFieldInitGrowth)",
          "mAGrw_ClearGrass" in read("src/game/m_all_grow_ovl.c") and "Deliberately NOT consuming Save clear_grass" in read("src/game/m_field_make.c"), R)

    # ---- test hooks
    check("hook: --force-weed-pull is a default-off client-only hook: global = 0, parsed, documented in --help, extern in pc_platform.h",
          "int g_pc_force_weed_pull = 0;" in main_c and 'strcmp(argv[i], "--force-weed-pull") == 0' in main_c and "--force-weed-pull   Client-only test hook" in main_c
          and "extern int           g_pc_force_weed_pull;" in plat, R)
    hk = func_body(net, r"\nstatic void pcnetgame_run_weed_pull_test_trigger\(void\) \{")
    check("hook: returns unless g_pc_force_weed_pull, role CLIENT, READY, world latched, town scene; reads the actor only after pcnetgame_is_real_player_actor",
          hk.index("!g_pc_force_weed_pull") < hk.index("s_role != PC_NETGAME_ROLE_CLIENT") < hk.index("s_client_link != PC_NETGAME_LINK_READY")
          and hk.index("GET_PLAYER_ACTOR_NOW()") < hk.index("pcnetgame_is_real_player_actor(local)")
          and "pcnetgame_run_weed_pull_test_trigger();" in net and "PC_Test_ForceRequestRemoveGrass(gamePT" in hk, R)
    check("hook: PC_Test_ForceRequestRemoveGrass is a thin pass-through to the real Player_actor_request_main_remove_grass (TARGET_PC only)",
          re.search(r"int PC_Test_ForceRequestRemoveGrass\(GAME\* game, const xyz_t\* target_pos, const xyz_t\* grass_pos\) \{\s*return Player_actor_request_main_remove_grass\(game, target_pos, grass_pos\);\s*\}", pl) is not None, R)
    seed = func_body(net, r"\nstatic void pcnetgame_run_field_action_test_seed\(void\) \{")
    check("seed: --field-action-test-seed places weeds GRASS_A/C/B at (24,108)/(40,108)/(56,108) and flowers PANSIES0/LEAVES_PANSIES0 at (72,108)/(88,108) only",
          "{ 24, 40, 56 }" in seed and "{ GRASS_A, GRASS_C, GRASS_B }" in seed and "{ 72, 88 }" in seed and "{ FLOWER_PANSIES0, FLOWER_LEAVES_PANSIES0 }" in seed
          and "g_pc_field_action_test_seed" in seed, R)
    return summary_and_exit_code(R)


if __name__ == "__main__":
    sys.exit(main())
