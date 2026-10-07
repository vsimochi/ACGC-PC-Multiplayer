#!/usr/bin/env python3
"""test_hostcfg_src.py - SOURCE AUDIT of batch A (no game process; tier: SOURCE AUDITED):
  A1  the HOST decides authoritative wildlife: TOWN_SVC_STATE service 4 = HOST_CONFIG (python spec == C, wire_baseline pins), the host default stays OFF,
      the HOST_CONFIG push at READY precedes the snapshot, the client validates strictly and applies it without a usable save, EVERY wildlife gate of
      pc_net_game.c goes through one predicate that follows the host for a CLIENT, the client's own flag is ignored (logged), local wildlife spawning
      is suppressed while the mode is unknown, the mode-switch transition resets the bookkeeping
  A2  shells / mushrooms: the refill skips the acres READY remote players stand in (host only, read-only, no RNG, solo / client unchanged)
  A3  the CLIENT 'not connected' notice: role/link/latch/3 s condition, the loud rate-limited log, the draw path and its call site (CLIENT only)
"""
import os
import re
import sys

import net_spike_lib as L
import wire_baseline
from test_ts_src import read, strip_comments, in_order


def func_body(src, name):
    """Body of the top-level function `name` (header starts in column 0, so a call statement is never mistaken for it)."""
    m = re.search(r"^(?:static )?[A-Za-z_][\w ]*?[ \*]+%s\([^{;]*\)\s*\{\n" % re.escape(name), src, re.M)
    if not m:
        return ""
    i = m.end()
    return src[i:src.index("\n}\n", i)]


def main():
    results = []

    def check(d, c):
        L.check(d, bool(c), results)

    c = read("pc/src/pc_net_game.c")
    h = read("pc/include/pc_net_game.h")
    main_c = read("pc/src/pc_main.c")
    sm = read("src/actor/ac_set_manager.c")
    fi = read("src/game/m_field_info.c")
    ms = read("src/game/m_mushroom.c")
    gr = read("src/graph.c")
    pm = read("pc/src/pc_pause_menu.c")
    pmh = read("pc/include/pc_pause_menu.h")
    lib = read("pc/tools/net_spike/net_spike_lib.py")
    cs = strip_comments(c)

    # ------------------------------------------------------------------ A1 wire
    check("A1 wire: service 4 HOST_CONFIG, array bound 5, 8-byte blob and the fit assert exist in C; python lib == C; the message struct / size asserts are untouched (352 B)",
          "#define PC_NETGAME_TS_HOSTCFG  4u" in c and "#define PC_NETGAME_TS_NUM      6u" in c and "#define PC_NETGAME_TS_HOSTCFG_LEN 8u" in c
          and "PC_NETGAME_TS_HOSTCFG_LEN <= PC_NETGAME_TS_BLOB_MAX" in c and "_Static_assert(sizeof(PCNetGameTownSvcStateMsg) == 352," in c
          and "PC_NETGAME_TS_HOSTCFG = 4 " in lib and "PC_NETGAME_TS_HOSTCFG_LEN = 8" in lib and L.PC_NETGAME_TS_HOSTCFG == 4 and L.PC_NETGAME_TS_HOSTCFG_LEN == 8)
    check("A1 wire: the wire_baseline pins cover the new constants (TS_C_PINS + lib pins) and its self-test mutations for them exist",
          '"#define PC_NETGAME_TS_HOSTCFG  4u"' in read("pc/tools/net_spike/wire_baseline.py") and '"PC_NETGAME_TS_HOSTCFG_LEN": \'8\'' in read("pc/tools/net_spike/wire_baseline.py")
          and '"hostcfg service id"' in read("pc/tools/net_spike/wire_baseline.py"))
    check("A1 the host flag's DEFAULT stays OFF (int g_pc_authoritative_wildlife = 0;) and the CLI parse only ever sets it to 1",
          "int g_pc_authoritative_wildlife = 0;" in main_c and len(re.findall(r"g_pc_authoritative_wildlife = ", main_c)) == 2)
    # ------------------------------------------------------------------ A1 host
    build = func_body(c, "pcnetgame_ts_build")
    check("A1 host: service 4 blob = 8 bytes, byte 0 = the host's own flag (0/1), the rest zero (memset), len 8; refreshed with the other services (digest / seq like them)",
          "svc == (int)PC_NETGAME_TS_HOSTCFG" in build and "memset(blob, 0, PC_NETGAME_TS_HOSTCFG_LEN);" in build and "blob[0] = g_pc_authoritative_wildlife ? 1u : 0u;" in build
          and "pcnetgame_ts_refresh((int)PC_NETGAME_TS_HOSTCFG);" in func_body(c, "pcnetgame_ts_refresh_all"))
    check("A1 host: the tick push loop covers service 4 (retry on a failed send / change), and HOST_CONFIG is pushed at READY BEFORE pcnetgame_host_start_snapshot",
          "svc <= (int)PC_NETGAME_TS_EVENT; svc++" in func_body(c, "pcnetgame_host_ts_push_peer")  # events: the loop bound is now service 5
          and c.count("pcnetgame_host_ts_push_hostcfg(peer);") == 1
          and c.index("pcnetgame_host_ts_push_hostcfg(peer);") < c.index('pcnetgame_host_start_snapshot(peer, "joined");')
          and c.index("pcnetgame_host_send_scene_roster(peer);") < c.index("pcnetgame_host_ts_push_hostcfg(peer);")
          and "s_host_peer_link[peer] != PC_NETGAME_LINK_READY" in func_body(c, "pcnetgame_host_ts_push_hostcfg"))
    # ------------------------------------------------------------------ A1 client
    hd = func_body(c, "pcnetgame_handle_client_town_svc")
    check("A1 client: service 4 is accepted with the exact length 8, digest + seq checks run BEFORE it, the blob is validated strictly (byte 0 in {0,1}, reserved zero) and applied "
          "WITHOUT a usable save (before the pcfa_save_ready stash)",
          "svc != (int)PC_NETGAME_TS_HOSTCFG" in hd and "(uint16_t)PC_NETGAME_TS_HOSTCFG_LEN" in hd and "pcnetgame_ts_valid_hostcfg_blob(m.blob)" in hd
          and in_order(hd, ["pcnetgame_fnv1a32(m.blob, m.len) != m.digest", "m.seq == 0u", "pcnetgame_ts_valid_hostcfg_blob", "if (svc == (int)PC_NETGAME_TS_HOSTCFG) {", "if (!pcfa_save_ready())"])[0]
          and "blob[0] > 1u" in func_body(c, "pcnetgame_ts_valid_hostcfg_blob") and "blob[i] != 0u" in func_body(c, "pcnetgame_ts_valid_hostcfg_blob"))
    check("A1 client: a client-originated TOWN_SVC_STATE is still dropped by the host (unchanged), the READY gate is the first check of the client handler",
          "client-originated TOWN_SVC_STATE" in c and hd.lstrip().startswith("PCNetGameTownSvcStateMsg m;") and "s_client_link != PC_NETGAME_LINK_READY" in hd[:300])
    gate = func_body(c, "pcnetgame_wildlife_auth_on")
    check("A1 the ONE predicate: a CLIENT follows s_client_wildlife_mode == 1 (own flag ignored), host / solo use g_pc_authoritative_wildlife; mode starts at -1 (unknown) and is reset to -1 at start_client",
          "s_role == PC_NETGAME_ROLE_CLIENT" in gate and "return s_client_wildlife_mode == 1;" in gate and "return g_pc_authoritative_wildlife ? 1 : 0;" in gate
          and "static int8_t                   s_client_wildlife_mode = -1;" in c and "s_client_wildlife_mode = -1; /* batch A (A1)" in func_body(c, "pc_net_game_start_client"))
    code_refs = [m.start() for m in re.finditer(r"g_pc_authoritative_wildlife", cs)]
    allowed = ["pcnetgame_wildlife_auth_on", "pcnetgame_ts_build", "pcnetgame_client_wildlife_mode_apply", "pc_net_game_start_client"]
    bad = []
    for pos in code_refs:
        # find the enclosing function name (the last function header before pos)
        heads = list(re.finditer(r"^(?:static )?[\w \*]+?[ \*]+(\w+)\([^{;]*\)\s*\{\n", cs[:pos], re.M))
        name = heads[-1].group(1) if heads else "?"
        if name not in allowed:
            bad.append(name)
    check("A1 NO wildlife gate of pc_net_game.c reads g_pc_authoritative_wildlife directly any more (only the predicate, the host blob builder, the adopt log and the start_client log; bad: %s)" % bad,
          not bad and len(code_refs) == 4 and cs.count("pcnetgame_wildlife_auth_on()") >= 14)
    check("A1 pc_net_game_authoritative_wildlife_enabled() returns the predicate; pc_net_game_wildlife_mode_pending() == CLIENT && mode < 0; both declared",
          "return pcnetgame_wildlife_auth_on();" in func_body(c, "pc_net_game_authoritative_wildlife_enabled")
          and "return s_role == PC_NETGAME_ROLE_CLIENT && s_client_wildlife_mode < 0;" in func_body(c, "pc_net_game_wildlife_mode_pending")
          and "int pc_net_game_wildlife_mode_pending(void);" in h)
    check("A1 the client's own --authoritative-wildlife is IGNORED and that is logged loudly at start_client; --help says so",
          "--authoritative-wildlife is IGNORED on a client" in func_body(c, "pc_net_game_start_client") and "on a CLIENT the flag is ignored" in main_c)
    check("A1 spawn seam: ac_set_manager skips the local spawn while the mode is pending, BEFORE the plain vanilla call; the other branches are unchanged",
          sm.index("pc_net_game_wildlife_mode_pending()") < sm.index("if (!pc_net_game_authoritative_wildlife_enabled())")
          and "else if (!pc_net_game_authoritative_wildlife_enabled()) {\n          set_manager->set_overlay.ovl_proc(set_manager, play);" in sm
          and "pc_net_game_request_wildlife_spawn_trigger(" in sm and "pc_net_game_host_local_wildlife_spawn_trigger(" in sm)
    ap = func_body(c, "pcnetgame_client_wildlife_mode_apply")
    check("A1 mode switch contract: -1 -> x needs nothing; a REAL change resets the presentation bookkeeping, the stamps, the known generation and the snapshot state; nothing local is destroyed",
          "if (prev >= 0 && prev != (int)s_client_wildlife_mode) {" in ap and all(x in ap for x in ("pcwld_presentation_reset();", "pcwld_clear_local_actor_stamps();",
                                                                                               "s_client_wildlife_known_generation = 0;", "s_client_wildlife_snap_active = 0;"))
          and "Actor_delete" not in ap and "destruct" not in ap)
    check("A1 the host's own gates are unchanged semantically: the host (and solo) take the flag branch of the predicate",
          "return g_pc_authoritative_wildlife ? 1 : 0;" in gate)
    # ------------------------------------------------------------------ A2
    hook = func_body(c, "pc_net_game_host_remote_player_in_acre")
    hs = strip_comments(hook)
    check("A2 hook is HOST only (0 for client / solo), READY peers only, field-scene peers only, finite in-range last MOVE position, exact-acre compare; read-only: no RNG, no Save_ / fg write, bounded by the peer span",
          "s_role != PC_NETGAME_ROLE_HOST" in hook and "s_host_peer_link[p] != PC_NETGAME_LINK_READY" in hook and "PC_NETSCENE_KIND_FIELD" in hook
          and "pcnetgame_pos_valid(px, py, pz)" in hook and "mFI_Wpos2BlockNum(&pbx, &pbz, pos) == TRUE && pbx == bx && pbz == bz" in hook
          and "p < pcnetgame_peer_span()" in hook and not re.search(r"RANDOM|fqrand|Save_|Common_Get|mFI_Set|\bFG\b|pcfa_note", hs)
          and "int pc_net_game_host_remote_player_in_acre(int bx, int bz);" in h)
    check("A2 shells: mFI_ResearchShell skips an acre a remote player is in exactly like the player's own acre (host-only hook, TARGET_PC); the vanilla condition and RNG order are untouched",
          "if ((bx != player_bx || bz != player_bz)\n#ifdef TARGET_PC" in fi and "&& !pc_net_game_host_remote_player_in_acre(bx, bz)" in fi
          and fi.count("pc_net_game_host_remote_player_in_acre(") == 1)
    check("A2 mushrooms: placement skips remote acres (bx+1, bz+1, vanilla row/col rule kept) and the clear pass never selects a mushroom in one (candidate zeroed, no RNG consumed there)",
          "&& !pc_net_game_host_remote_player_in_acre(bx + 1, bz + 1)" in ms and "pc_net_game_host_remote_player_in_acre(i % FG_BLOCK_X_NUM + 1, i / FG_BLOCK_X_NUM + 1)" in ms
          and ms.count("pc_net_game_host_remote_player_in_acre(") == 2 and "if (bx != player_bx - 1 && bz != player_bz - 1" in ms)
    check("A2 the client gates of batch G5 are still there (mFI_SetShell / mMsr_SetMushroom return for role CLIENT)",
          "if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {\n        return;" in func_body(fi, "mFI_SetShell") and "PC_NETGAME_ROLE_CLIENT" in func_body(ms, "mMsr_SetMushroom"))
    # ------------------------------------------------------------------ A3
    nu = func_body(c, "pcnetgame_client_notice_update")
    check("A3 notice condition: role CLIENT && local world loaded && link != READY, shown only after 3000 ms continuous, hidden + timer reset otherwise, loud log rate-limited to 10 s",
          "s_role == PC_NETGAME_ROLE_CLIENT && s_local_world_latched && s_client_link != PC_NETGAME_LINK_READY" in nu and "#define PC_NETGAME_NOTICE_DELAY_MS 3000u" in c
          and "#define PC_NETGAME_NOTICE_LOG_MS   10000u" in c and "[NET][NOTICE] client: NOT CONNECTED to the host" in nu and "s_notice_visible = 0;" in nu
          and "(uint32_t)(now - s_notice_since_ms) >= PC_NETGAME_NOTICE_DELAY_MS" in nu)
    po = func_body(c, "pc_net_game_poll")
    check("A3 the update runs only for the CLIENT role (host / solo: poll resets the flags / returns), and pc_net_game_client_notice_visible() only reads the flag",
          "if (s_role == PC_NETGAME_ROLE_CLIENT) {\n        pcnetgame_client_notice_update();" in po and "s_notice_visible = 0;" in po[:300]
          and "return s_notice_visible;" in func_body(c, "pc_net_game_client_notice_visible") and "int pc_net_game_client_notice_visible(void);" in h)
    dr = func_body(pm, "pc_net_notice_draw")
    check("A3 draw: the existing PC font overlay path only (pc_menu_draw_centered inside mFont_SetMatrix / UnSetMatrix, like the pause overlay), no-op while paused / no graph (G6.1: the join-failure message is drawn first, in the same path, and returns; the not-connected notice is unchanged); "
          "called from graph_main right after pc_pause_menu_draw, under TARGET_PC",
          "if (g_pc_paused || game == NULL || game->graph == NULL) return;" in dr and "if (!pc_net_game_client_notice_visible()) return;" in dr
          and dr.index("pc_net_game_join_message(&join_warning)") < dr.index("if (!pc_net_game_client_notice_visible()) return;") and dr.count("pc_menu_draw_centered(") == 2
          and "mFont_SetMatrix(game->graph, mFont_MODE_FONT);" in dr and "mFont_UnSetMatrix(game->graph, mFont_MODE_FONT);" in dr
          and "void pc_net_notice_draw(struct game_s* game);" in pmh
          and gr.index("pc_pause_menu_draw(game);") < gr.index("pc_net_notice_draw(game);") and gr.count("pc_net_notice_draw(game);") == 1
          and re.search(r"#ifdef TARGET_PC\n    pc_pause_menu_draw\(game\);\n    pc_net_notice_draw\(game\);[^\n]*\n#endif", gr) is not None)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
