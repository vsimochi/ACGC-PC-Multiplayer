#!/usr/bin/env python3
"""test_menu_world_live_src.py - "the world keeps running behind menus" in a multiplayer session (SOURCE AUDIT, no game process).
Esc menu: a host with a READY peer and (new) a READY client keep the whole world running, input stays suppressed. Submenu (inventory
etc.): the full actor pass stays frozen (vanilla; a full AI pass is unsafe, evidence pinned below); the host keeps the NPC_MOVE stream
alive (F) and ticks idle villagers (F2); in a live session (pc_net_game_menu_world_live: host with READY peers or READY client, setting
menu_world_live / env AC_MP_MENU_WORLD, default on) a WHITELIST ticks too (puppets, effect control, weather, insects, fish) under the
same no-create / no-bank / deferred-delete / OC-drop flag, and the menu is drawn over the LIVE world instead of the prbuf snapshot.
Single player / unconnected: every new path returns before doing anything (vanilla byte-identical)."""
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
ESC_RET = "if (g_pc_paused && pc_net_game_host_ready_peer_count() == 0 && !pc_net_game_menu_world_live()) return;"
check("Esc early return only when paused AND no READY client AND not a live session (client)", re.search(r"#ifdef TARGET_PC.*?" + re.escape(ESC_RET) + r".*?#endif", mv, re.S) is not None)
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
check("hold: returns first unless host-with-peers or live session (single player / unconnected byte-identical)",
      re.search(r"\{\s*Actor_list\* list;\s*ACTOR\* a;\s*int k, slot;\s*int tick_ok;\s*int host = pc_net_game_host_ready_peer_count\(\) > 0;\s*int live = pc_net_game_menu_world_live\(\);\s*if \(!host && !live\) \{\s*return;", hb) is not None)
check("hold: NPC_MOVE heartbeat only on the host; a client's hidden villager may tick (consumer only)",
      "if (!host) {" in hb and "(host && n->condition_info.hide_flg)" in hb)
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
      hp is not None and "return g_pc_paused && (pc_net_game_host_ready_peer_count() > 0 || pc_net_game_menu_world_live());" in hp.group(1))
check("mDemo_Main called exactly once in m_play.c", len(re.findall(r"\bmDemo_Main\(play\);", MP)) == 1)
check("mDemo_stock_clear still unconditional (drops unaccepted requests)", re.search(r"game->doing_point = 4;\s*mDemo_stock_clear\(\);", fb) is not None)
check("guard helper defined before its use and before Game_play_move", MP.index("static int Game_play_pc_host_paused_live") < MP.index("static void Game_play_move_fbdemo_not_move"))
check("pause early-return precedes fbdemo call in Game_play_move", mv.index(ESC_RET) < mv.index("Game_play_move_fbdemo_not_move(game);"))
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
# ---- "world keeps running behind menus" (live multiplayer session) ----
lv = NG[NG.index("int pc_net_game_menu_world_live(void) {"):]
lv = lv[:lv.index("\n}\n")]
check("live predicate: env AC_MP_MENU_WORLD kill switch + settings menu_world_live", 'getenv("AC_MP_MENU_WORLD")' in lv and "!g_pc_settings.menu_world_live" in lv)
check("live predicate: HOST needs >= 1 READY peer, otherwise only a READY client (single player / unconnected = 0)",
      "if (s_role == PC_NETGAME_ROLE_HOST) return pc_net_game_host_ready_peer_count() > 0;" in lv and lv.rstrip().endswith("return pc_net_game_world_is_host_authoritative();"))
ST = (ROOT / "pc/src/pc_settings.c").read_text(encoding="utf-8", errors="replace")
check("setting menu_world_live defaults ON and is parsed / saved", ".menu_world_live = 1," in ST and '"menu_world_live = 1\\n"' in ST and 'strcmp(key, "menu_world_live")' in ST and "menu_world_live = %d" in ST)
lt = MP[MP.index("static void Game_play_pc_submenu_live_tick(GAME_PLAY* play) {"):MP.index("static int Game_play_pc_submenu_free_play")]
check("live tick: puppets (no pvp gate) in ACTOR_PART_UNUSED, constructed and not being deleted",
      "play->actor_info.list[ACTOR_PART_UNUSED]" in lt and "a->ct_proc == NULL && a->mv_proc != NULL && pc_remote_player_is_puppet_actor(a)" in lt)
ids = set(re.findall(r"a->id == (mAc_PROFILE_\w+)", lt))
check("live tick: control-actor WHITELIST is exactly effect control / weather / insect / fish (%s)" % sorted(ids),
      ids == {"mAc_PROFILE_EFFECT_CONTROL", "mAc_PROFILE_WEATHER", "mAc_PROFILE_INSECT", "mAc_PROFILE_GYOEI"} and "ACTOR_PART_CONTROL" in lt)
check("live tick: only parts UNUSED + CONTROL (never PLAYER / NPC / BG / ITEM / ...)", set(re.findall(r"ACTOR_PART_\w+", lt)) == {"ACTOR_PART_UNUSED", "ACTOR_PART_CONTROL"})
check("live tick: respects the vanilla move-while-culled rule", "(ACTOR_STATE_NO_MOVE_WHILE_CULLED | ACTOR_STATE_NO_CULL)) == 0" in lt)
ta = MP[MP.index("static void Game_play_pc_submenu_tick_actor(GAME_PLAY* play, ACTOR* a) {"):MP.index("static void Game_play_pc_submenu_live_tick")]
check("tick helper: flag set only around the move proc, OC registrations dropped", re.search(
    r"g_pc_submenu_world_tick = 1;\s*\(\*a->mv_proc\)\(a, \(GAME\*\)play\);\s*g_pc_submenu_world_tick = 0;\s*if \(play->collision_check\.collider_num > oc_num\) \{\s*play->collision_check\.collider_num = oc_num;", ta) is not None)
check("tick helpers never create / delete actors or touch banks / the OC pass", not re.search(r"Actor_info_\w+\(|Actor_delete\(|mSc_\w+\(|CollisionCheck_(OC|clear|setOC)\(", ta + lt))
check("live tick runs only in a live session, with the menu fully open from free play", "if (live && play->submenu.process_status == mSM_PROCESS_PLAY && Game_play_pc_submenu_free_play(play)) {" in hb)
fp = MP[MP.index("static int Game_play_pc_submenu_free_play(GAME_PLAY* play) {"):MP.index("static void Game_play_pc_submenu_world_hold(GAME_PLAY* play) {")]
check("free-play gate: no fade / wipe / talk actor / message window", all(x in fp for x in ("FADE_TYPE_NONE", "WIPE_MODE_NONE", "mDemo_Get_talk_actor() == NULL", "mMsg_Check_main_hide(mMsg_Get_base_window_p()) == TRUE")))
RP = (ROOT / "pc/src/pc_remote_player.c").read_text(encoding="utf-8", errors="replace")
check("puppet collider held (no OC registration) while the submenu tick flag is set", re.search(r"if \(g_pc_submenu_world_tick\) \{\s*return \"hold\";", RP) is not None)
KA = (ROOT / "src/actor/ac_ins_ka.c").read_text(encoding="utf-8", errors="replace")
check("mosquito never requests a sting from inside a menu tick", re.search(r"if \(!g_pc_submenu_world_tick\)\s*#endif\s*\{\s*mPlib_request_main_stung_mosquito_type1\(actorx\);", KA) is not None)
mvf = MP[MP.index("static void Game_play_move(GAME* game)"):MP.index("title_demo_move(play);")]
check("scroll phase keeps flowing only in a live session (else-branch of the WAIT block)", re.search(r"\}\s*#ifdef TARGET_PC\s*else if \(pc_net_game_menu_world_live\(\)\) \{\s*evw_anime_add_scroll_phase\(&play->game\);", mvf, re.S) is not None)
# draw half
du = MP[MP.index("static void Game_play_pc_menu_live_draw_update(GAME_PLAY* play) {"):MP.index("static int Game_play_pc_menu_live_draw(GAME_PLAY* play) {")]
check("live draw decided once per menu (latched on the first PRERENDER_DONE frame) from live session + free play",
      "if (s_pc_menu_live_draw < 0) {" in du and "pc_net_game_menu_world_live() && Game_play_pc_submenu_free_play(play)" in du and "s_pc_menu_live_draw = -1;" in du)
mb = MP[MP.index("static int makeBumpTexture("):MP.index("static void draw_version(")]
check("makeBumpTexture: vanilla prbuf path kept, skipped only for a live-drawn menu",
      re.search(r"Game_play_pc_menu_live_draw_update\(play\);\s*#endif\s*if \(play->submenu\.mode == mSM_MODE_PRERENDER_DONE\s*#ifdef TARGET_PC\s*&& !Game_play_pc_menu_live_draw\(play\)", mb) is not None and "gSPTextureRectangle" in mb)
check("live draw: the world draw is bracketed by the submenu-area save / restore (the player's draw calls load_player)",
      re.search(r"if \(Game_play_pc_menu_live_draw\(play\)\) \{[^}]*mSM_pc_area_save\(&play->submenu\);\s*Actor_info_draw_actor\(play, &play->actor_info\);\s*mSM_pc_area_restore\(&play->submenu\);", mb) is not None
      and "extern void mSM_pc_area_save(Submenu* submenu) {" in SM and "SubmenuArea_visit = s_pc_area_save.visit;" in SM)
gd = MP[MP.index("static void Game_play_draw(GAME_PLAY* play) {"):MP.index("extern void play_main(GAME* game) {")]
check("draw: live menu lists are sub-lists CALLED from LIGHT_DISP, depth cleared + 4:3 UI first, vanilla draw otherwise",
      gd.count("gSPDisplayList(NOW_LIGHT_DISP++, g);") == 2 and "PC_NOOP_CLEAR_DEPTH" in gd and "PC_NOOP_WIDESCREEN_STRETCH_OFF" in gd and gd.count("gfxclose(") == 2
      and gd.count("mSM_submenu_draw(&play->submenu, (GAME*)play);") == 2 and re.search(r"\} else\s*#endif\s*\{\s*mSM_submenu_draw", gd) is not None)
E64 = (ROOT / "src/static/libforest/emu64/emu64.c").read_text(encoding="utf-8", errors="replace")
GX = (ROOT / "pc/src/pc_gx.c").read_text(encoding="utf-8", errors="replace")
gxc = GX[GX.index("void pc_gx_clear_depth_now(void) {"):]
gxc = gxc[:gxc.index("\n}\n")]
check("emu64 handles PC_NOOP_CLEAR_DEPTH -> pc_gx_clear_depth_now (flush first, depth only)", "PC_NOOP_CLEAR_DEPTH" in E64 and "glClear(GL_DEPTH_BUFFER_BIT);" in gxc
      and gxc.index("pc_gx_draw_pending();") < gxc.index("glClear(") and "GL_COLOR_BUFFER_BIT" not in gxc)
check("input still suppressed while paused (client Esc too: padmgr is role-blind)", "int suppress = g_pc_paused;" in PAD)
check("TEST-ONLY autopilot submenu request: one writer in the autopilot, default 0", "int g_pc_th_submenu_req = 0;" in SM and NG.count("g_pc_th_submenu_req = t;") == 1)
sys.exit(1 if fails else 0)
