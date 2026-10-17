#!/usr/bin/env python3
"""test_shop_join_sync_src.py - SOURCE pins of "Shop building on join" (docs/multiplayer-guest-roadmap.md; kill switch AC_MP_SHOP_JOIN_SYNC=0).

Bug: a client joining with its OWN (stale) save showed the old shop building (the structure actor built from its local SHOP0 tile) while another client showed the
host's upgraded shop: the host's shadow did not know the structure tile behind its own live shop actor (valid=0 in the join snapshot), and a host value that did arrive
was parked behind the client's live structure placeholder. Pins:
  H1 host: pcnetgame_host_init_shadow() learns the shop (pcnetgame_host_shadow_learn_shop) before the dirty bits are dropped; the structure tile is SHOP0 + the host's
     level ONLY when the field holds a placeholder (a STABLE save tile is the field's own value), the footprint only RSV_NO tiles that are still unknown; kill switch.
  C1 client: once per session after the first snapshot (s_client_world_synced) AND service 3 (Shop_c) are applied, READY, the local world latched, no refresh running,
     not during a scene change; compares the live structure actor npc_id (else the local tile) with SHOP0 + mSP_GetShopLevel(); logs '[NET][SHOP] client: shop building
     0x.. != host 0x..: refreshing'; rewrites only STABLE local tiles under pcfa_set_net_apply_active; a live actor -> the shop upgrade's client refresh (re-tag + ONE
     same-position reload, bounded 10 min) with its own reason; reset per session in pcnetgame_shopup_client_reset().
  C2 the shared client tick: client role only (offline / host never run it), the join check first, the reload reason restored after use.
  M1 pc_m_card.c pc_town_shop_structure_npc(): exec-gated (gamePT->exec == play_main) and read-only.
  A1-A9 the arrival gate (regression: the guest stuck on the train tracks): no same-position reload (shop join / shop refresh / day roll / host renewal / look reload)
     while the local train arrival runs; the client refresh waits for the arrival to finish + 2 s idle in the town
     ('[NET][SHOP] client: shop refresh deferred until the arrival finishes').
Tier: SOURCE. Usage: python test_shop_join_sync_src.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402


def rd(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def body(text, head_rx):
    m = re.search(head_rx, text)
    if not m:
        return ""
    i = text.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return ""


def main():
    res = []
    ck = lambda d, c: L.check(d, bool(c), res)  # noqa: E731
    ng = rd("pc", "src", "pc_net_game.c")
    mc = rd("pc", "src", "pc_m_card.c")
    en = body(ng, r"static int pcnetgame_shop_join_sync_enabled\(void\) \{")
    ck("H0 kill switch: AC_MP_SHOP_JOIN_SYNC, ON unless it is \"0\", logged once", 'getenv("AC_MP_SHOP_JOIN_SYNC")' in en and 's_on = !(e != NULL && strcmp(e, "0") == 0);' in en
       and "static int s_on = -1;" in en)
    ish = body(ng, r"static void pcnetgame_host_init_shadow\(void\) \{")
    ck("H1 init_shadow learns the shop after the field read and before the dirty bits are dropped",
       "pcnetgame_host_shadow_learn_shop();" in ish and ish.index("pcfa_read_acre(acre, s_shadow_items[acre]") < ish.index("pcnetgame_host_shadow_learn_shop();")
       < ish.index("pcfa_take_dirty_acres()"))
    ls = body(ng, r"static void pcnetgame_host_shadow_learn_shop\(void\) \{")
    ck("H2 learn_shop: gated on the switch, the host's level, the structure tile only behind a placeholder (not STABLE) = SHOP0 + level, marked known",
       "!pcnetgame_shop_join_sync_enabled()" in ls and "const int level = mSP_GetShopLevel();" in ls
       and "if (pcfa_transient_kind(items[st]) != PCFA_VALUE_STABLE) {\n        s_shadow_items[acre][st] = (uint16_t)(SHOP0 + level);" in ls
       and "pcnetgame_set_dep_bit(s_shadow_known[acre], st, 1);" in ls)
    ck("H3 learn_shop: the footprint only where the field holds RSV_NO and the tile is still unknown (never overrides a known value)",
       "items[t] == (uint16_t)RSV_NO && !pcnetgame_dep_bit(s_shadow_known[acre], t)" in ls and "s_shadow_items[acre][t] = (uint16_t)RSV_NO;" in ls)
    ck("H4 learn_shop is only called from the host shadow init", ng.count("pcnetgame_host_shadow_learn_shop();") == 1 and ng.count("pcnetgame_host_shadow_learn_shop(") == 2)
    blk = ng[ng.index("===== SHOP JOIN SYNC (docs"):ng.index("===== SHOP JOIN SYNC END")]
    jt = body(blk, r"static void pcnetgame_shopjoin_client_tick\(void\) \{")
    gate = jt[:jt.index("s_shopjoin_done = 1;")]
    ck("C1 gate: once per session, READY, first snapshot applied, service 3 (Shop_c) applied, local world latched, no refresh already running",
       all(s in gate for s in ("s_shopjoin_done ||", "s_client_link != PC_NETGAME_LINK_READY", "!s_client_world_synced", "!s_ts_client_have[PC_NETGAME_TS_SHOP]",
                               "!s_local_world_latched", "s_up.c_refresh_level >= 0")))
    ck("C2 the kill switch ends the check without touching anything", "if (!pcnetgame_shop_join_sync_enabled()) {\n        s_shopjoin_done = 1;\n        return;" in jt
       and jt.index("pcnetgame_shop_join_sync_enabled()") < jt.index("pcfa_set_tile"))
    ck("C3 never decided during a scene change (no live actor and no play scene -> retry next frame, done not set yet)",
       "if (live == 0 && (gamePT == NULL || gamePT->exec != play_main)) {\n        return;" in jt
       and jt.index("gamePT->exec != play_main") < jt.index("s_shopjoin_done = 1;\n    if ((n = aSL_PC_ShopFootprint"))
    ck("C4 the built building = the live structure actor's npc_id, else the local structure tile; the host's = SHOP0 + the mirrored level",
       "want = (uint16_t)(SHOP0 + level);" in jt and "level = mSP_GetShopLevel();" in jt and "built = live != 0 ? (uint16_t)live : v;" in jt and "if (built == want) {" in jt)
    ck("C5 the mismatch log line", '"[NET][SHOP] client: shop building 0x%04X != host 0x%04X: refreshing' in jt and "matches the host's shop level" in jt)
    w = jt[jt.index("pcfa_set_net_apply_active(1);"):jt.index("pcfa_set_net_apply_active(0);")]
    ck("C6 the local field writes are net-apply writes, STABLE tiles only (a live placeholder is re-tagged instead), the footprint = RSV_NO + deposit off",
       "if (pcfa_transient_kind(v) == PCFA_VALUE_STABLE && v != want) {\n        (void)pcfa_set_tile(ac, st, want);" in w
       and "pcfa_transient_kind(f) == PCFA_VALUE_STABLE" in w and "(void)pcfa_set_tile(ac, tl[i], (uint16_t)RSV_NO);" in w and "(void)pcfa_set_deposit(ac, tl[i], 0);" in w)
    ck("C7 a live actor reuses the shop upgrade's client refresh (re-tag + one reload) with its own reason",
       "if (live != 0) {\n        s_shopup_refresh_why = \"the shop building differs from the host's\";\n        s_shopup_refresh_is_join = 1;" in jt
       and "s_up.c_refresh_level = level;" in jt)
    tick = body(ng, r"static void pcnetgame_shopup_client_tick\(void\) \{")
    ck("C8 the shared client tick: CLIENT role only, then the join check before the refresh",
       tick.index("if (s_role != PC_NETGAME_ROLE_CLIENT) {\n        return;\n    }") < tick.index("pcnetgame_shopjoin_client_tick();") < tick.index("if (s_up.c_refresh_level >= 0) {"))
    ck("C9 the reload uses the reason and restores the renewal reason after it started",
       "(r = pc_town_same_position_reload(s_shopup_refresh_why)) != 0) {" in tick and "            s_up.c_refresh_level = -1;\n            s_shopup_refresh_why = \"the shop was upgraded\";" in tick
       and "bounded" in jt and "> 600000u" in tick)
    # A1-A8: the arrival gate (regression "guest stuck on the train tracks": the join refresh reloaded the town at +7.4 s while the local train arrival ran).
    ar = body(mc, r"int pc_town_local_arrival_active\(void\) \{")
    ck("A1 pc_town_local_arrival_active: the pending RIDE_OFF_DEMO profile, the live RIDE_OFF_DEMO actor, or a demo main state (train / boat) = the arrival runs",
       "Common_Get(demo_profiles[0]) == mAc_PROFILE_RIDE_OFF_DEMO" in ar and "Actor_info_name_search(&play->actor_info, mAc_PROFILE_RIDE_OFF_DEMO, ACTOR_PART_CONTROL) != NULL" in ar
       and "idx >= mPlayer_INDEX_DEMO_WAIT && idx <= mPlayer_INDEX_DEMO_WADE" in ar and "gamePT->exec != play_main" in ar)
    mo = body(mc, r"int pc_town_reload_moment\(void\) \{")
    ck("A2 pc_town_reload_moment: the arrival (2) is checked first, then fade/wipe, town scene, menu, demo, able_submenu",
       "if (pc_town_local_arrival_active()) {\n        return 2;" in mo and 0 <= mo.find("pc_town_local_arrival_active()") < mo.find("fb_wipe_mode")
       and "mDemo_CheckDemo() != FALSE" in mo and "mSM_PROCESS_WAIT" in mo and "mPlib_able_submenu_type1" in mo and "return -1;" in mo)
    rl = body(mc, r"int pc_town_same_position_reload\(const char\* why\) \{")
    ck("A3 every pc_town_same_position_reload (shop refresh, shop join, day roll) refuses unless the safe moment: the arrival is never cut",
       "if ((m = pc_town_reload_moment()) != 1) {\n        return m == -1 ? -1 : 0;" in rl and 0 <= rl.find("pc_town_reload_moment()") < rl.find("goto_other_scene"))
    rn = body(mc, r"static void pc_host_renewshop_poll\(void\) \{")
    lk = body(ng, r"static void pcnetgame_look_reload_poll\(void\) \{")
    ck("A4 the other same-position reloads (host shop renewal, G3.3 look reload) wait for the arrival too",
       0 <= rn.find("if (pc_town_local_arrival_active()) {") < rn.find("goto_other_scene") and 0 <= lk.find("if (pc_town_local_arrival_active()) {") < lk.find("goto_other_scene"))
    ck("A5 the client refresh is gated by the arrival-finished moment BEFORE the reload, with the deferral log line",
       0 <= tick.find("(r = pc_town_reload_moment()) == 2 || r == 0") < tick.find("pc_town_same_position_reload(")
       and "[NET][SHOP] client: shop refresh deferred until the arrival finishes" in tick)
    ck("A6 a settle of PC_SHOPUP_REFRESH_SETTLE_MS (2000 ms) continuously idle in the town after the arrival, restarted by any busy frame",
       "#define PC_SHOPUP_REFRESH_SETTLE_MS 2000u" in ng and "s_shopup_safe_since = 0;" in tick
       and 0 <= tick.find("(pcnetgame_now_ms() - s_shopup_safe_since) < PC_SHOPUP_REFRESH_SETTLE_MS") < tick.find("pc_town_same_position_reload("))
    ck("A7 the reload / verify log the player's position (the real test checks it is not the train start)",
       "[NET][SHOP] client: refresh reload at the player's position (%d,%d)" in tick and "(shop level %d), player at (%d,%d), live shop actor 0x%04X" in tick)
    ck("A8 a reload never replays the arrival demo: it clears the demo profiles (no RIDE_OFF_DEMO restart at the tracks)",
       rl.count("Common_Set(demo_profiles[0], mAc_PROFILE_NUM);") == 1)
    ck("A9 the defer / settle state is reset per pending refresh and per session",
       ng.count("s_shopup_defer_logged = 0;") >= 4 and "s_shopup_safe_since = 0;" in body(ng, r"static void pcnetgame_shopup_client_reset\(void\) \{"))
    rs = body(ng, r"static void pcnetgame_shopup_client_reset\(void\) \{")
    ck("C10 per session: reset in pcnetgame_shopup_client_reset(), which the client session reset calls",
       "s_shopjoin_done = 0;" in rs and "pcnetgame_shopup_client_reset(); /* the shop upgrade mirror is host-fed too" in ng)
    npc = body(mc, r"int pc_town_shop_structure_npc\(void\) \{")
    ck("M1 pc_town_shop_structure_npc: exec-gated before the play cast, returns the SHOP0..SHOP3 npc_id, never writes it",
       npc.index("gamePT == NULL || gamePT->exec != play_main") < npc.index("play = (GAME_PLAY*)gamePT;") and "return (int)actor->npc_id;" in npc
       and "npc_id =" not in npc)
    ck("M2 the vanilla renewal stays client-off (aSL_RenewShop returns for a CLIENT: the join sync never renews on the client)",
       "if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {\n        return;\n    }" in rd("src", "actor", "ac_shop_level.c"))
    return L.summary_and_exit_code(res)


if __name__ == "__main__":
    raise SystemExit(main())
