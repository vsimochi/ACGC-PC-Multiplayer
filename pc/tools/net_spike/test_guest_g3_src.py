#!/usr/bin/env python3
"""test_guest_g3_src.py - GUESTS G3 (arrival without the gateway, title-menu entry, look refresh) SOURCE AUDIT (no game process).

  G3.1  src/game/m_start_data_init.c: mSDI_StartDataInitGuest is TARGET_PC only, additive (no pre-existing function changed vs the baseline blob), its calls are EXACTLY the visitor
        (PAK) init's calls minus mEv_SetGateway / mNpc_SetReturnAnimal / mNpc_SendRegisteredGoodbyMail, mEv_UnSetGateway first and mSDI_StartInitAfter last; no new-town / new-player
        init, no private_data / homes / house call; the guest arrival uses it and no longer the PAK init
  G3.2  title menu (src/actor/ac_animal_logo.c, PC_ENHANCEMENTS): the "Join as Guest" item exists ONLY for a network client (pc_guest_title_item_visible == role CLIENT), the same
        three readiness conditions as Start Game, the vanilla Start path (aAL_title_game_data_init_start_select, aAL_fade_out_start_wait, ...) is byte-identical to the baseline blob;
        pc_guest_arrive (the ONE arrival shared by --bootstrap-guest / --guest and the title item) re-reads the save with pc_save_reload() BEFORE the fresh record is built / bound; every
        failure of the title path shows a message and returns to the title (no exit); the CLI path keeps exit(2)
  G3.3  look refresh after adopt (pc_net_game.c): guests only, face-only -> rebuilt in place, gender -> ONE same-position reload; additive (no pre-existing line of pc_net_game.c removed)
  G3.4  the real-client test exists and relies on log lines that exist in the host sources
  W     no wire change (message ids 1..58, wire_baseline green), pc_main.c / pc_vi.c untouched
Tier: SOURCE AUDITED. The title-menu CLICK itself is NOT exercised by any process test (no UI automation). Exit code 0 when all checks pass."""
import os
import re
import subprocess
import sys

import net_spike_lib as L
import test_guest_src as S
import wire_baseline

ROOT = S.ROOT
BASELINE = "e056f56"   # G2 commit (HEAD when G3 started)
END_REF = "e68c90c"    # G4 commit + docs: the diff audits compare BASELINE..END_REF (G3 + the G4 additions it pins), not the working tree (later milestones touch the same files)


def blob(rel, ref=BASELINE):
    return subprocess.run(["git", "-C", ROOT, "show", ref + ":" + rel], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def raw_bytes(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
        return f.read()


def numstat(rel):
    out = subprocess.run(["git", "-C", ROOT, "diff", "--numstat", BASELINE, END_REF, "--", rel], capture_output=True, check=True, timeout=60).stdout.decode().split()
    return (int(out[0]), int(out[1])) if out else (0, 0)


def fns(src):
    m = S.mask(src)
    return m, S.functions(m)


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)

    # ------------------------------------------------------------------ G3.1
    sdi_raw = S.read("src/game/m_start_data_init.c")
    sdi_base = blob("src/game/m_start_data_init.c")
    sm, sf = fns(sdi_raw)
    bm, bf = fns(sdi_base)
    sb = lambda n: S.body(sm, sf, n)
    guest = sb("mSDI_StartDataInitGuest")
    obs = sb("mSDI_StartDataInitObserver")
    pak = sb("mSDI_StartInitPak")
    call_rx = r"\b(m[A-Z][A-Za-z0-9_]*)\("
    skip = ("mFRm_CheckSaveData", "mSDI_StartInitPak", "mSDI_StartDataInitObserver", "mSDI_StartDataInitGuest")
    pak_calls = [c for c in re.findall(call_rx, pak) if c not in skip]
    gst_calls = [c for c in re.findall(call_rx, guest) if c not in skip]
    obs_calls = [c for c in re.findall(call_rx, obs) if c not in skip]
    excluded = {"mNpc_SetReturnAnimal", "mNpc_GetInAnimalP", "mNpc_SendRegisteredGoodbyMail", "mEv_SetGateway"}
    expected = [c for c in pak_calls if c not in excluded]
    ck("G3.1 mSDI_StartDataInitGuest exists; its calls are EXACTLY mSDI_StartInitPak's minus SetReturnAnimal / GetInAnimalP / SendRegisteredGoodbyMail / SetGateway, mEv_UnSetGateway first, "
       "mSDI_StartInitAfter last (Pak: %s | guest: %s)" % (pak_calls, gst_calls),
       guest != "" and gst_calls == ["mEv_UnSetGateway"] + expected + ["mSDI_StartInitAfter"] and expected)
    ck("G3.1 it calls the same player-neutral calls as the host observer's init (same mEv_UnSetGateway ... mSDI_StartInitAfter sequence)", gst_calls == obs_calls)
    ck("G3.1 NO mEv_SetGateway (the visitor gateway flag), no goodbye mail, no return animal in the guest init", "mEv_SetGateway" not in guest.replace("mEv_UnSetGateway", "")
       and "mNpc_SendRegisteredGoodbyMail" not in guest and "mNpc_SetReturnAnimal" not in guest)
    ck("G3.1 no new-town / new-player init, no house / home allocation, no private_data / homes / passport access in the guest init",
       not re.search(r"mSDI_StartInitNew|NEW_PLAYER|private_data|homes|mHS_|mHm_|l_mcd_foreigner|mPr_LoadPak|mPr_InitPrivateInfo|mNW_InitOneMyOriginal|Save_Set", guest))
    ck("G3.1 it keeps the stale-flag clear (mEv_UnSetGateway with player_no == 4), the binding guard (now_private != NULL, player_no == mPr_FOREIGNER), the valid-save guard and the vanilla "
       "malloc flag: mSDI_StartInitAfter(game, FALSE, mSDI_MALLOC_FLAG_ZELDA)",
       "Common_Get(now_private) == NULL || Common_Get(player_no) != mPr_FOREIGNER" in guest and "mFRm_CheckSaveData() == TRUE" in guest
       and "mSDI_StartInitAfter(game, FALSE, mSDI_MALLOC_FLAG_ZELDA);" in guest)
    i_def = sdi_raw.index("extern int mSDI_StartDataInitGuest(GAME* game) {")
    ck("G3.1 TARGET_PC only: the definition sits between #ifdef TARGET_PC and its #endif, the declaration is in its own #ifdef TARGET_PC block of m_start_data_init.h",
       sdi_raw.rfind("#ifdef TARGET_PC", 0, i_def) > sdi_raw.rfind("#endif", 0, i_def) and sdi_raw.index("#endif", i_def) > sdi_raw.index("\n}\n", i_def)
       and re.search(r"#ifdef TARGET_PC\n[^#]*extern int mSDI_StartDataInitGuest\(GAME\* game\);\n#endif", S.read("include/m_start_data_init.h")) is not None)
    same = [n for n in ("mSDI_StartInitNew", "mSDI_StartInitNewPlayer", "mSDI_StartInitFrom", "mSDI_StartInitPak", "mSDI_StartInitErr", "mSDI_StartInitAfter", "mSDI_StartInitBefore",
                        "mSDI_StartDataInit", "mSDI_StartDataInitObserver") if S.body(sm, sf, n) == S.body(bm, bf, n) and S.body(sm, sf, n) != ""]
    ck("G3.1 vanilla single-player / resident init untouched: every pre-existing mSDI_* function body is IDENTICAL to the baseline blob %s (%d of 9)" % (BASELINE, len(same)), len(same) == 9)
    ck("G3.1 numstat vs baseline: m_start_data_init.c / .h remove no line", numstat("src/game/m_start_data_init.c")[1] == 0 and numstat("include/m_start_data_init.h")[1] == 0)

    card = S.read("pc/src/pc_m_card.c")
    cm, cf = fns(card)
    cb = lambda n: S.body(cm, cf, n)
    arr = cb("pc_guest_arrive")
    poll = cb("pc_bootstrap_guest_poll")
    join = cb("pc_guest_title_join")
    ck("G3.1 the guest arrival (pc_guest_arrive) uses mSDI_StartDataInitGuest and neither the visitor PAK init nor the observer / new-town inits; the poll only calls pc_guest_arrive",
       "mSDI_StartDataInitGuest(gamePT)" in arr and not re.search(r"mSDI_StartDataInit\(|MODE_PAK|mSDI_StartDataInitObserver|mSDI_INIT_MODE|mSDI_StartInitNew", arr)
       and 'pc_guest_arrive("--bootstrap-guest", g_pc_bootstrap_guest, err, sizeof(err))' in poll and "mSDI_" not in poll)
    ck("G3.1 pc_guest_arrive never arms pc_save_ready, never writes a save, never writes private_data / homes (only the read-only name check function reads private_data)",
       "pc_save_ready" not in arr and "pc_save_write" not in arr and "private_data" not in arr and "homes" not in arr)
    ck("G3.1 resident path untouched: pc_bootstrap_resident_poll, the observer poll and the resident-name conflict function are byte-identical to the baseline blob",
       all(S.body(cm, cf, n) == S.body(*fns(blob("pc/src/pc_m_card.c")), n) != "" for n in ("pc_bootstrap_resident_poll", "pc_host_observer_poll", "pc_guest_resident_name_conflict")))
    # pc_guest_build_fresh_record differs from the baseline ONLY by the shirt-helper extraction (the G6 first-run creation shares the shirt): substituting the helper call with the
    # old inline expression (and re-declaring the old local) must give the baseline body byte for byte; the helper itself is pinned separately.
    _bld_new = cb("pc_guest_build_fresh_record")
    _bld_old = S.body(*fns(blob("pc/src/pc_m_card.c")), "pc_guest_build_fresh_record")
    _call = "pc_guest_starter_shirt(home, gender)"
    _inline = "(ITM_CLOTH000 + (gender == mPr_SEX_FEMALE ? 8 : 0) + shirt_idx)"
    _h_line = "const u32 h = pc_guest_identity_hash(home);\n"
    _bld_sub = _bld_new.replace(_call, _inline).replace(_h_line, _h_line + "    int shirt_idx = (int)((h >> 8) & 7u);\n")
    _shirt = cb("pc_guest_starter_shirt")
    ck("G3.1 pc_guest_build_fresh_record == the baseline body once the one pc_guest_starter_shirt() call is replaced by the old inline shirt expression (+ the old shirt_idx local); the helper "
       "derives the same shirt: const u32 h = pc_guest_identity_hash(id); shirt_idx = (h >> 8) & 7; ITM_CLOTH000 + (female ? 8 : 0) + shirt_idx",
       _bld_old != "" and _bld_new.count(_call) == 1 and _bld_sub == _bld_old
       and "const u32 h = pc_guest_identity_hash(id);" in _shirt and "const int shirt_idx = (int)((h >> 8) & 7u);" in _shirt
       and "(u16)(ITM_CLOTH000 + (gender == mPr_SEX_FEMALE ? 8 : 0) + shirt_idx)" in _shirt)

    # ------------------------------------------------------------------ G3.2
    logo = S.read("src/actor/ac_animal_logo.c")
    logo_base = blob("src/actor/ac_animal_logo.c")
    lm, lf = fns(logo)
    lbm, lbf = fns(logo_base)
    lb = lambda n: S.body(lm, lf, n)
    changed_ok = {"aAL_actor_ct", "aAL_pc_game_start_wait", "aAL_pc_menu_draw"}
    unchanged = [n for _a, _b, n in lbf if S.body(lbm, lbf, n) == lb(n) and lb(n) != ""]
    changed = [n for _a, _b, n in lbf if S.body(lbm, lbf, n) != lb(n)]
    ck("G3.2 vanilla Start path untouched: aAL_title_game_data_init_start_select, aAL_fade_out_start_wait, aAL_game_start_wait, aAL_setupAction, aAL_wipe_end_check, aAL_chk_start_key* are "
       "byte-identical to the baseline blob; the only changed functions are %s (changed: %s)" % (sorted(changed_ok), changed),
       set(changed) <= changed_ok and all(n in unchanged for n in ("aAL_title_game_data_init_start_select", "aAL_fade_out_start_wait", "aAL_game_start_wait", "aAL_setupAction", "aAL_wipe_end_check",
                                                                    "aAL_chk_start_key", "aAL_chk_start_key2", "aAL_actor_dt")))
    start_blk = re.search(r"case aAL_PC_ITEM_START:[^\n]*\n(.*?)break;", lb("aAL_pc_game_start_wait"), re.S).group(1)
    base_start = re.search(r"case 0:[^\n]*\n(.*?)break;", S.body(lbm, lbf, "aAL_pc_game_start_wait"), re.S).group(1)
    ck("G3.2 the Start Game case body (conditions + aAL_setupAction(.., aAL_ACTION_FADE_OUT_START)) is identical to the baseline's `case 0` body", start_blk == base_start and "aAL_ACTION_FADE_OUT_START" in start_blk)
    gcase = re.search(r"case aAL_PC_ITEM_GUEST:[^\n]*\n(.*?)break;", lb("aAL_pc_game_start_wait"), re.S).group(1)
    ck("G3.2 the Join as Guest case has the SAME three readiness conditions as Start Game (mLd_CheckStartFlag, aAL_wipe_end_check, mTD_tdemo_button_ok_check) and calls pc_guest_title_join",
       all(x in gcase for x in ("mLd_CheckStartFlag() == TRUE", "aAL_wipe_end_check(game) == TRUE", "mTD_tdemo_button_ok_check()", "pc_guest_title_join()")))
    mb = lb("aAL_pc_menu_build")
    ck("G3.2 the item exists ONLY for a network client: the menu (aAL_pc_menu_build) adds aAL_PC_ITEM_GUEST only under `if (pc_guest_title_item_visible())` (the else branch is the Play Online "
       "item, no role); Start, Options and Quit are UNCONDITIONAL, in that order (without a client the id list is Start / [Play Online] / Options / Quit); the count / item accessors use the builder",
       mb != "" and re.search(r"if \(pc_guest_title_item_visible\(\)\) \{\s*items\[n\+\+\] = aAL_PC_ITEM_GUEST;\s*\} else if \(pc_play_online_menu_available\(\)\)", mb) is not None
       and mb.count("aAL_PC_ITEM_GUEST") == 1
       and re.search(r"items\[n\+\+\] = aAL_PC_ITEM_START;\s*if \(pc_guest_title_item_visible\(\)\)", mb) is not None
       and re.search(r"\}\s*items\[n\+\+\] = aAL_PC_ITEM_OPTIONS;\s*items\[n\+\+\] = aAL_PC_ITEM_QUIT;\s*return n;", mb) is not None
       and "aAL_pc_menu_build(items)" in lb("aAL_pc_menu_count") and "aAL_pc_menu_build(items)" in lb("aAL_pc_menu_item"))
    ck("G3.2 pc_guest_title_item_visible() == (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT); the join refuses any other role",
       re.sub(r"\s+", "", cb("pc_guest_title_item_visible")).endswith("returnpc_net_game_role()==PC_NETGAME_ROLE_CLIENT;") and "pc_net_game_role() != PC_NETGAME_ROLE_CLIENT" in join)
    def in_pc_enh(text, pos):
        stack = []
        for ln in text[:pos].split("\n"):
            t = ln.strip()
            if t.startswith("#if"):
                stack.append(t)
            elif t.startswith("#endif") and stack:
                stack.pop()
            elif t.startswith("#else") and stack:
                stack[-1] = "#else-of " + stack[-1]
        return any(x.startswith("#ifdef PC_ENHANCEMENTS") for x in stack)
    ck("G3.2 the title code of the item is all inside #ifdef PC_ENHANCEMENTS (no vanilla build path changed): every aAL_pc_menu_count / aAL_pc_menu_item / pc_guest_title_ / "
       "s_aAL_pc_guest_joining / aAL_PC_ITEM reference sits inside a PC_ENHANCEMENTS block",
       all(in_pc_enh(logo, m.start()) for m in re.finditer(r"aAL_pc_menu_count|aAL_pc_menu_item|pc_guest_title_|s_aAL_pc_guest_joining|aAL_PC_ITEM", logo)))
    ck("G3.2 the label shows the guest NAME from the profile module (Join as Guest (NAME)); the title actor never touches the profile module itself",
       '"Join as Guest (%s)"' in card and "pc_guest_profile" not in logo and "pc_guest_title_label()" in logo)
    i_reload, i_ok, i_bld, i_bind, i_init, i_scene = (arr.find(x) for x in ("pc_save_reload()", "mFRm_CheckSaveData() == FALSE", "pc_guest_build_fresh_record(pass", "Common_Set(now_private, pass);",
                                                                            "mSDI_StartDataInitGuest(gamePT)", "goto_other_scene(play, &door_data, TRUE)"))
    ck("G3.2 the save is RE-READ with pc_save_reload() (the call of the vanilla Start path) BEFORE the validity check, the fresh record, the binding, the guest init and the station scene change",
       0 < i_reload < i_ok < i_bld < i_bind < i_init < i_scene and "if (pc_save_loaded) {" in arr and "pc_save_reload();" in lb("aAL_title_game_data_init_start_select"))
    ck("G3.2 ONE arrival function: the title join and the CLI poll both call pc_guest_arrive (tags join-as-guest / --bootstrap-guest)",
       'pc_guest_arrive("join-as-guest", spec, err, sizeof(err))' in join and 'pc_guest_arrive("--bootstrap-guest"' in poll and cb("pc_guest_arrive") != "")
    ck("G3.2 title failure path: every failure shows a message and returns 0 (pc_guest_title_fail), nothing exits: no exit / abort / SDL_Quit / g_pc_running in the join, the fail helper or the arrival",
       "exit(" not in join + cb("pc_guest_title_fail") + arr and "abort(" not in join + arr and "g_pc_running" not in join + arr and "SDL_Quit" not in join + cb("pc_guest_title_fail") + arr
       and join.count("pc_guest_title_fail(") == join.count("return 0;") == 6 and join.count("return 1;") == 1
       and len(re.findall(r'pc_guest_title_fail\((?:[^;"]|"(?:[^"\\]|\\.)*")*\);\s*return 0;', join)) == 6)  # every `return 0;` immediately follows a pc_guest_title_fail(...) call
    # G4 pin update: G4 added the host-side `--max-guests N` option to pc_main.c (11 purely ADDED lines: the option parse + two help lines, no line removed or changed); the early
    # --bootstrap-guest / --guest validation is still untouched (deletions == 0), pc_vi.c is still untouched. Asserted again in test_guest_g4_src.py.
    ck("G3.2 CLI path keeps exit(2): the poll exits 2 on any arrival failure, the early validation in pc_main.c is untouched (--bootstrap-guest / --guest argument validation exits 2; "
       "G4: the only change to pc_main.c is the 11 added --max-guests lines, nothing removed)",
       poll.count("exit(2);") == 1 and numstat("pc/src/pc_main.c") == (11, 0) and numstat("pc/src/pc_vi.c") == (0, 0))
    ck("G3.2 the arrival restores now_private / player_no / rtc_enabled when the init or the scene change fails (so a failed title join leaves the title usable), and checks the scene is "
       "ready (play_main, no wipe, a player actor) BEFORE anything is bound",
       re.search(r"mSDI_StartDataInitGuest\(gamePT\) != TRUE\) \{\s*Common_Set\(now_private, prev_private\);\s*Common_Set\(player_no, prev_player_no\);\s*Common_Set\(time\.rtc_enabled, prev_rtc\);", arr)
       and re.search(r"scene_res != TRUE\) \{\s*Common_Set\(demo_profiles\[0\], mAc_PROFILE_NUM\);\s*Common_Set\(now_private, prev_private\);", arr)
       and arr.index("get_player_actor_withoutCheck(play) == NULL") < i_reload)
    ck("G3.2 the message is drawn by the title menu with the existing PC font helper pc_menu_draw_centered (word wrapped), read through pc_guest_title_message(), shown for 8 s",
       "pc_guest_title_message()" in lb("aAL_pc_menu_draw") and "pc_menu_draw_centered(game, row" in lb("aAL_pc_menu_draw")
       and "time(NULL) + 8" in cb("pc_guest_title_fail")
       # the heading moved out of the title actor: it is set by pc_guest_title_fail() and drawn through pc_guest_title_message_head()
       and 's_pc_guest_title_msg_head = "Could not join as a guest:";' in cb("pc_guest_title_fail") and "Could not join as a guest:" in card
       and "pc_menu_draw_centered(game, pc_guest_title_message_head()" in lb("aAL_pc_menu_draw") and "pc_guest_title_message_head" in logo)

    # ------------------------------------------------------------------ G3.3
    ng_raw = S.read("pc/src/pc_net_game.c")
    nm, nf = fns(ng_raw)
    nb = lambda n: S.body(nm, nf, n)
    la = nb("pcnetgame_look_refresh_after_adopt")
    lp = nb("pcnetgame_look_reload_poll")
    ap = nb("pcnetgame_crec_apply_staged")
    ck("G3.3 pc_net_game.c: the change is purely additive (no line removed vs the baseline blob)", numstat("pc/src/pc_net_game.c")[1] == 0 and numstat("pc/src/pc_net_game.c")[0] > 20)
    ck("G3.3 the look refresh is GUESTS ONLY: it returns first unless player_no >= mPr_FOREIGNER, so a resident's adopt is untouched",
       la != "" and la.index("(int)Common_Get(player_no) < (int)mPr_FOREIGNER") < la.index("s_look_reload_pending = 1;") and "return; /* residents: untouched */" in ng_raw)
    ck("G3.3 face-only difference -> mPlib_change_player_face(gamePT) in place (needs a live face bank); gender difference -> ONE deferred reload request (the skeleton bank is bound at scene load)",
       "mPlib_get_player_face_p(gamePT) != NULL" in la and "mPlib_change_player_face(gamePT);" in la and "(int)np->gender != old_gender" in la and la.index("s_look_reload_pending = 1;") < la.index("mPlib_change_player_face"))
    ck("G3.3 called from the adopt memcpy path right after the single write into the live record (after `memcpy(np, &s_crec_merged`), before the cloth refresh",
       ap.index("memcpy(np, &s_crec_merged, sizeof(Private_c));") < ap.index("pcnetgame_look_refresh_after_adopt(old_gender, old_face, np);") < ap.index("*cloth_refreshed = 0;")
       and "const int old_gender = (int)np->gender;" in ap and "const int old_face = (int)np->face;" in ap)
    ck("G3.3 the reload: one-shot, only in an idle SCENE_FG (no fade / wipe, real player actor, mPlib_able_submenu_type1, submenu WAIT), same position, demo profiles cleared (the train arrival is "
       "not replayed), goto_other_scene to the CURRENT scene; elsewhere it is dropped (the next scene load rebuilds the banks)",
       lp != "" and "play->scene_id != SCENE_FG || Save_Get(scene_no) != SCENE_FG" in lp and "door.next_scene_id = SCENE_FG;" in lp and "Common_Set(demo_profiles[0], mAc_PROFILE_NUM);" in lp
       and "mPlib_able_submenu_type1((GAME*)play)" in lp and "goto_other_scene(play, &door, TRUE)" in lp and lp.count("goto_other_scene(") == 1 and "s_look_reload_pending = 0;" in lp
       and "pos = pl->actor_class.world.position;" in lp)
    ct = nb("pcnetgame_crec_tick")
    ck("G3.3 the reload poll runs from the client record tick only after the record is SYNCED (after the adopt), and only reads state otherwise",
       ct.index("if (s_crec.state != PC_NETGAME_CRS_SYNCED) {") < ct.index("pcnetgame_look_reload_poll();"))
    ck("G3.3 no wire change by G3: message ids 1..66 (66 = M-F promotion handoff) (59..61 came with the furniture sync, 62..65 with the town transfer), wire_baseline green, protocol version untouched",
       sorted(dict(wire_baseline.c_message_ids(ng_raw)).values()) == list(range(1, wire_baseline.EXPECTED_MAX_MSG_ID + 1)) and wire_baseline.EXPECTED_MAX_MSG_ID == 66)
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    ck("G3.3 wire_baseline: %d checks, all green" % len(wb), wb and all(c for _d, c in wb))

    # ------------------------------------------------------------------ G3.4
    real = S.read("pc/tools/net_spike/test_guest_g3_real.py")
    ck("G3.4 the real-client test exists, drives --guest against --host --bootstrap-resident 0 on bin_fixture4 only, kills the client abruptly and restarts it",
       "extra_args=[\"--guest\"]" in real and "startswith(\"bin_fixture4\")" in real and "c1.stop()" in real and "env.client(\"c2\")" in real and "private_data[0..3]" in real)
    ck("G3.4 it relies on host log lines that exist: peer disconnect, remote-player READY / destroy, guest slot binding, MIGRATE as resident 4",
       "[NET] host: peer %d disconnected" in ng_raw and "[NET][REMOTE] player %d disconnected -- destroying remote-player actor" in S.read("pc/src/pc_remote_player.c")
       and "[NET][REMOTE] player %d READY -- remote-player actor creation pending" in S.read("pc/src/pc_remote_player.c") and "bound to GUEST slot %d" in ng_raw)

    # ------------------------------------------------------------------ EOL
    eol_ok = True
    for rel in ("pc/src/pc_m_card.c", "pc/src/pc_net_game.c", "src/game/m_start_data_init.c", "include/m_start_data_init.h", "src/actor/ac_animal_logo.c",
                "pc/tools/net_spike/test_guest_g3_src.py", "pc/tools/net_spike/test_guest_g3_real.py", "pc/tools/net_spike/test_guest_g1_src.py",
                "pc/tools/net_spike/test_guest_src.py", "pc/tools/net_spike/test_guest_g2_src.py"):
        # the INDEX holds LF (i/lf); the worktree file is uniform (all LF, or all CRLF under autocrlf), never mixed
        eol = subprocess.run(["git", "-C", ROOT, "ls-files", "--eol", "--", rel], capture_output=True, check=True, timeout=60).stdout.decode()
        b = raw_bytes(rel)
        n_crlf, n_lf = b.count(b"\r\n"), b.count(b"\n")
        good = eol.startswith("i/lf") and n_crlf in (0, n_lf)
        if not good:
            print("   EOL mismatch:", rel, eol.strip(), n_crlf, n_lf)
        eol_ok = eol_ok and good
    ck("every edited file keeps its EOL (index LF; worktree files uniformly LF or uniformly CRLF, never mixed)", eol_ok)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
