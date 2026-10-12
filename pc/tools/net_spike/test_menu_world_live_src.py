#!/usr/bin/env python3
"""test_menu_world_live_src.py - the PC pause menu does not freeze a hosted multiplayer world (SOURCE AUDIT, no game process).
Submenu (inventory etc.): actors stay frozen (vanilla; an AI pass is unsafe, evidence pinned below); the host only keeps the NPC_MOVE
stream alive with the TRUE held poses (Game_play_pc_submenu_world_hold, read-only). Also pins the Esc-pause talk guard gating."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MP = (ROOT / "src/game/m_play.c").read_text(encoding="utf-8", errors="replace")
PAD = (ROOT / "src/padmgr.c").read_text(encoding="utf-8", errors="replace")
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


mv = MP[MP.index("static void Game_play_move(GAME* game)"):]
mv = mv[:mv.index("title_demo_move(play);")]
check("early return only when paused AND no READY client", re.search(r"#ifdef TARGET_PC.*?if \(g_pc_paused && pc_net_game_host_ready_peer_count\(\) == 0\) return;.*?#endif", mv, re.S) is not None)
check("old unconditional return gone", "if (g_pc_paused) return;" not in mv)
check("input still blocked while paused (padmgr zeroes pads)", "int suppress = g_pc_paused;" in PAD)
fb = MP[MP.index("static void Game_play_move_fbdemo_not_move"):MP.index("static void Game_play_move(GAME* game)")]
check("submenu branch: only the Esc-pause demo guard + the read-only hold hooks", "pc_net" not in fb and fb.count("#ifdef TARGET_PC") == 3)
# Submenu world hold (stage F): read-only heartbeat in the non-WAIT branch, END in the WAIT branch, gated on READY peers.
check("hold called only in the non-WAIT else branch", re.search(
    r"\} else \{\s*mVibctl_set_force_stop\(2\);\s*#ifdef TARGET_PC\s*Game_play_pc_submenu_world_hold\(play\);\s*#endif\s*\}", fb, re.S) is not None)
check("hold END called first in the WAIT branch (before Actor_info_call_actor processes deferred deletes)", re.search(
    r"if \(play->submenu\.process_status == mSM_PROCESS_WAIT\) \{\s*#ifdef TARGET_PC\s*Game_play_pc_submenu_world_hold_end\(play\);[^\n]*\s*#endif\s*play->game_frame\+\+;", fb, re.S) is not None)
hb = MP[MP.index("static void Game_play_pc_submenu_world_hold(GAME_PLAY* play) {"):MP.index("static void Game_play_pc_submenu_world_hold_end")]
check("hold: returns first when no READY peer (single player / client / lone host byte-identical)",
      re.search(r"\{\s*Actor_list\* list;\s*ACTOR\* a;\s*int k, slot;\s*int tick_ok;\s*if \(pc_net_game_host_ready_peer_count\(\) <= 0\) \{\s*return;", hb) is not None)
check("hold: never creates/deletes actors or touches banks/OC pair directly",
      not re.search(r"Actor_info_\w+\(|Actor_delete\(|mSc_\w+\(|CollisionCheck_(OC|clear|setOC)\(|condition_info\.action =", hb))
# F2 real tick: gated (PLAY, no fade/wipe, no talk actor, message hidden, field), per-villager guards, flag set only around mv_proc.
check("F2 tick gate", all(s in hb for s in ("play->submenu.process_status == mSM_PROCESS_PLAY", "play->fb_fade_type == FADE_TYPE_NONE",
      "play->fb_wipe_mode == WIPE_MODE_NONE", "mDemo_Get_talk_actor() == NULL", "mMsg_Check_main_hide(mMsg_Get_base_window_p()) == TRUE",
      "mFI_GET_TYPE(mFI_GetFieldId()) == mFI_FIELD_FG")))
check("F2 per-villager guards (not talking, no pending tool / hand-over birth, constructed)", all(s in hb for s in (
      "!mDemo_IS_ACTOR_TALKING(a)", "talk_condition == aNPC_TALK_TYPE_NONE", "right_hand.requested_item_type == aNPC_ITEM_TYPE_NONE",
      "left_hand.requested_item_type == aNPC_ITEM_TYPE_NONE", "a->ct_proc == NULL")))
check("F2 flag set only around the move proc, OC registrations dropped", re.search(
    r"g_pc_submenu_world_tick = 1;\s*\(\*a->mv_proc\)\(a, \(GAME\*\)play\);\s*g_pc_submenu_world_tick = 0;\s*if \(play->collision_check\.collider_num > oc_num\) \{\s*play->collision_check\.collider_num = oc_num;", hb) is not None)
ACT = (ROOT / "src/game/m_actor.c").read_text(encoding="utf-8", errors="replace")
SCN = (ROOT / "src/game/m_scene.c").read_text(encoding="utf-8", errors="replace")
mk = ACT[ACT.index("extern ACTOR* Actor_info_make_actor("):ACT.index("profile = dlftbl->profile;")]
check("F2 Actor_info_make_actor refuses while the tick flag is set", "if (g_pc_submenu_world_tick) {" in mk and "return NULL;" in mk)
kb = SCN[SCN.index("extern char* mSc_secure_exchange_keep_bank("):SCN.index("extern void mSc_background_dmacopy_controller")]
check("F2 mSc_secure_exchange_keep_bank refuses while the tick flag is set", "if (g_pc_submenu_world_tick) {" in kb)
check("F2 Actor_delete stays mark-only (deferred unlink)", re.search(r"extern void Actor_delete\(ACTOR\* actor\) \{.*?actor->mv_proc = NULL;\s*actor->dw_proc = NULL;\s*\}", ACT, re.S) is not None)
check("F2 flag defaults to 0", "int g_pc_submenu_world_tick = 0;" in ACT)
check("hold: only resident villagers with identity guard, hidden skipped",
      "Save_Get(animals[slot]).id.npc_id" in hb and "n->npc_info.animal != Save_GetPointer(animals[slot])" in hb and "hide_flg" in hb)
check("hold: walk/run mapped to the matching wait action", "aNPC_ACTION_TYPE_WAIT + (act - aNPC_ACTION_TYPE_WALK)" in hb and "aNPC_ACTION_TYPE_WAIT + (act - aNPC_ACTION_TYPE_RUN)" in hb)
# Vanilla submenu freeze unchanged: actors/collision/msg still only run while process_status == WAIT, no npc-only pass.
check("actors/collision/msg still gated by second WAIT block", re.search(
    r"mSM_submenu_move\(&play->submenu\);\s*if \(play->submenu\.process_status == mSM_PROCESS_WAIT\) \{.*?CollisionCheck_OC\(game, &play->collision_check\);.*?"
    r"CollisionCheck_clear\(game, &play->collision_check\);.*?Actor_info_call_actor\(play, &play->actor_info\);.*?mMsg_Main\(game\);\s*(?:#ifdef TARGET_PC\s*Game_play_pc_submenu_world_hold_end\(play\);\s*#endif\s*)?\} else \{\s*mVibctl_set_force_stop\(2\);",
    fb, re.S) is not None)
check("no npc-only actor pass", "Actor_info_call_actor_npc_only" not in MP)
# Talk guard: mDemo_Main (the only acceptor of mDemo_Request) is skipped only while the host is Esc-paused with READY clients.
check("talk guard: mDemo_Main inside WAIT block, skipped only by Game_play_pc_host_paused_live()", re.search(
    r"if \(play->submenu\.process_status == mSM_PROCESS_WAIT\) \{\s*game->doing_point = 2;\s*#ifdef TARGET_PC\s*"
    r"if \(!Game_play_pc_host_paused_live\(\)\)\s*#endif\s*\{\s*mDemo_Main\(play\);\s*\}\s*game->doing_point = 3;\s*mEv_run\(&play->event\);", fb, re.S) is not None)
hp = re.search(r"#ifdef TARGET_PC\s*/\*.*?\*/\s*static int Game_play_pc_host_paused_live\(void\) \{(.*?)\n\}\s*#endif", MP, re.S)
check("talk guard helper is TARGET_PC-only and requires paused AND ready peers > 0",
      hp is not None and "return g_pc_paused && pc_net_game_host_ready_peer_count() > 0;" in hp.group(1))
check("mDemo_Main called exactly once in m_play.c", len(re.findall(r"\bmDemo_Main\(play\);", MP)) == 1)
check("mDemo_stock_clear still unconditional (drops unaccepted requests)", re.search(r"game->doing_point = 4;\s*mDemo_stock_clear\(\);", fb) is not None)
check("guard helper defined before its use and before Game_play_move", MP.index("static int Game_play_pc_host_paused_live") < MP.index("static void Game_play_move_fbdemo_not_move"))
check("pause early-return precedes fbdemo call in Game_play_move", mv.index("if (g_pc_paused && pc_net_game_host_ready_peer_count() == 0) return;") < mv.index("Game_play_move_fbdemo_not_move(game);"))
check("host_ready_peer_count is 0 outside HOST role", True)  # asserted by reading the C body below
NG = (ROOT / "pc/src/pc_net_game.c").read_text(encoding="utf-8", errors="replace")
check("pc_net_game_host_ready_peer_count returns 0 unless HOST", re.search(r"int pc_net_game_host_ready_peer_count\(void\) \{[^}]*?if \(s_role != PC_NETGAME_ROLE_HOST\) return 0;", NG, re.S) is not None)

# Client side: a client's own submenu / Esc freeze only defers LOCAL application. NPC_MOVE is received by pc_net_game_poll() (pc_vi.c,
# unconditional every frame, outside Game_play_move) and buffered in the per-slot snapshot ring; the actors consume it when they run again.
VI = (ROOT / "pc/src/pc_vi.c").read_text(encoding="utf-8", errors="replace")
check("client: pc_net_game_poll() called from pc_vi.c, not from m_play.c", "pc_net_game_poll();" in VI and "pc_net_game_poll" not in MP)
cm = NG[NG.index("static void pcnetgame_handle_client_npc_move(const PCNetGameNpcMoveMsg* in) {"):]
cm = cm[:cm.index("\n}\n")]
check("client: NPC_MOVE handler only buffers into the snapshot ring (no actor access)", "ns->snapshots[ns->snapshot_head]" in cm and "ACTOR" not in cm)

# Evidence that keeps the submenu NPC-only pass unimplemented (pin it so a future pass re-examines these facts):
SM = (ROOT / "src/game/m_submenu.c").read_text(encoding="utf-8", errors="replace")
HAND = (ROOT / "src/actor/npc/ac_npc_hand.c_inc").read_text(encoding="utf-8", errors="replace")
NCTRL = (ROOT / "src/actor/npc/ac_npc_ctrl.c_inc").read_text(encoding="utf-8", errors="replace")
end = SM[SM.index("static void mSM_move_End(Submenu* submenu)"):SM.index("extern void mSM_submenu_move(Submenu* submenu)")]
check("evidence: submenu end repacks/re-DMAs the shared object exchange banks", "mSc_dmacopy_all_exchange_bank(&play->object_exchange);" in end)
check("evidence: NPC move code births bank-backed tool actors", "aTOL_birth_proc(" in HAND)
check("evidence: NPC control code creates actors / registers cloth DMA", "Actor_info_make_actor(" in NCTRL and "aNPC_dma_regist_cloth_data(" in NCTRL)

# Esc-pause hold invariant: while mDemo_Main is skipped a demo stopped via mDemo_End keeps demo->current.actor until unpause. Only
# persistent actors (player, buildings, intro guide) call mDemo_End, so that pointer cannot dangle; a new caller must re-check this.
enders = set()
for p in list((ROOT / "src").rglob("*.c")) + list((ROOT / "src").rglob("*.c_inc")):
    t = p.read_text(encoding="utf-8", errors="replace")
    if "mDemo_End(" in t:
        enders.add(p.name)
allowed = {"m_demo.c", "m_player_main_demo_geton_boat_wade.c_inc", "m_player_main_demo_wade.c_inc", "m_player_main_outdoor.c_inc",
           "m_player_other_func.c_inc", "m_player_main_wade_snowball.c_inc", "m_player_main_wade.c_inc", "ac_cottage.c",
           "ac_post_office_move.c_inc", "ac_needlework_shop_move.c_inc", "ac_house_move.c_inc", "ac_my_house_move.c_inc",
           "ac_npc_rcn_guide2_move.c_inc"}
check("mDemo_End callers are only persistent actors (held STOP cannot dangle): extra=" + str(sorted(enders - allowed)), enders <= allowed)
sys.exit(1 if fails else 0)
