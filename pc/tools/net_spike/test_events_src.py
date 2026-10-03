#!/usr/bin/env python3
"""test_events_src.py - SOURCE AUDIT of the host-authoritative town EVENTS / special VISITORS (no game process; tier: SOURCE AUDITED):
TOWN_SVC_STATE service 5 = EVENT_STATE, the host's event DECISION state mirrored to clients, plus the client gates of the local event roll.

  E1  wire: service 5, array bound 6, 214-byte blob (84 special + 100 weekly + 30 core) fits one message; python lib == C; wire_baseline pins
  E2  host: the blob is built from the host's own Save_t, EXCLUDING every client-local field; digest-polled and pushed like the other services
  E3  client: strict validation order, content validator, apply writes ONLY the mirrored fields (never the excluded ones), never persisted
  E4  gates: init_special_event / init_weekly_event (the only RANDOM / local-player-id seeded decisions of m_event.c) and the event manager's special-event
      contents generator return/skip on a CLIENT before any roll; the daily table re-derivation after a mirror; the G7 gates in m_event_map_npc.c remain
  E5  inventory: every source file that touches the mirrored save regions is documented (a new writer must be reviewed)
  E6  TEST-ONLY world-state hook (--world-test-force) is default off, host-only, loud, range-checked
"""
import re
import sys

import net_spike_lib as L
from test_ts_src import read, strip_comments, in_order


def func_body(src, name):
    m = re.search(r"^(?:static |extern )?[A-Za-z_][\w \*]*?[ \*]+%s\([^{;]*\)\s*\{\n" % re.escape(name), src, re.M)
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
    ev = read("src/game/m_event.c")
    evh = read("include/m_event.h")
    em = read("src/actor/ac_event_manager.c")
    emn = read("src/game/m_event_map_npc.c")
    pm = read("pc/src/pc_main.c")
    pp = read("pc/include/pc_platform.h")
    lib = read("pc/tools/net_spike/net_spike_lib.py")
    wb = read("pc/tools/net_spike/wire_baseline.py")
    cs = strip_comments(c)

    # ------------------------------------------------------------------ E1 wire
    check("E1 wire: service 5 EVENT, array bound 6, 214-byte blob (84 + 100 + 30) with the layout assert and the fit-in-one-message assert exist in C",
          "#define PC_NETGAME_TS_EVENT    5u" in c and "#define PC_NETGAME_TS_NUM      6u" in c and "#define PC_NETGAME_TS_EVENT_LEN 214u" in c
          and "sizeof(mEv_special_c) == 84u && sizeof(mEv_weekly_u) == 100u && PC_NETGAME_TS_EVENT_LEN == 84u + 100u + 30u" in c
          and "PC_NETGAME_TS_EVENT_LEN <= PC_NETGAME_TS_BLOB_MAX" in c and "_Static_assert(sizeof(PCNetGameTownSvcStateMsg) == 352," in c)
    check("E1 python lib == C (service 5, len 214, still <= the 340-byte blob maximum), wire_baseline pins the constants and the array bound 6 deliberately",
          L.PC_NETGAME_TS_EVENT == 5 and L.PC_NETGAME_TS_EVENT_LEN == 214 and L.PC_NETGAME_TS_EVENT_LEN <= L.PC_NETGAME_TS_BLOB_MAX
          and "PC_NETGAME_TS_EVENT = 5 " in lib and "PC_NETGAME_TS_EVENT_LEN = 214" in lib
          and '"#define PC_NETGAME_TS_EVENT    5u"' in wb and '"#define PC_NETGAME_TS_NUM      6u"' in wb and '"PC_NETGAME_TS_EVENT": \'5\'' in wb
          and '"PC_NETGAME_TS_EVENT_LEN": \'214\'' in wb and '"event service id"' in wb and '"event blob len"' in wb)
    check("E1 offsets: special at 0, weekly at 84, core at 184 (the three region constants)",
          "#define PCNG_EV_OFF_SPECIAL 0u" in c and "#define PCNG_EV_OFF_WEEKLY  84u" in c and "#define PCNG_EV_OFF_CORE    184u" in c)

    # ------------------------------------------------------------------ E2 host
    b = func_body(c, "pcnetgame_ts_build_event")
    bs = strip_comments(b)
    check("E2 host build: reads the host's own Save_t (event_save_data.special / .weekly + event_save_common core + event_year) into the blob, zeroed first",
          "memset(blob, 0, PC_NETGAME_TS_EVENT_LEN);" in b and "&Save_Get(event_save_data).special, sizeof(mEv_special_c)" in b
          and "&Save_Get(event_save_data).weekly, sizeof(mEv_weekly_u)" in b and "Save_Get(event_year)" in b and "c->ghost_event_type" in b and "c->soncho_event_type" in b
          and "c->dozaemon_completed" in b and "c->bridge_flags.raw" in b)
    check("E2 host build EXCLUDES every client-local field: the transient special_event.flags is sent as 0, dates[TODAY] / dates[BIRTHDAY] (the LOCAL player's) as 0; "
          "no per-player story flags (event_save_data.flags), no area[] / area_use_bitfield, no last_date / valentines_day_date / white_day_date, no delete_event_id",
          "*p++ = 0u;" in b and "i == mEv_SAVE_DATE_TODAY || i == mEv_SAVE_DATE_BIRTHDAY) ? (uint16_t)0u" in b
          and not re.search(r"event_save_data\)?\.flags|area_use_bitfield|->area\b|last_date|valentines_day_date|white_day_date|delete_event_id", bs))
    check("E2 the host refreshes service 5 with the others (digest poll, seq bumps only on a change), pushes it per peer on a seq change (late join / reconnect = READY), "
          "logs a decoded line at every change and runs the client validator on its own blob (loud warning)",
          "pcnetgame_ts_refresh((int)PC_NETGAME_TS_EVENT);" in func_body(c, "pcnetgame_ts_refresh_all")
          and "svc <= (int)PC_NETGAME_TS_EVENT; svc++" in func_body(c, "pcnetgame_host_ts_push_peer")
          and "pcnetgame_ts_build_event(blob);" in func_body(c, "pcnetgame_ts_build")
          and 'pcnetgame_ts_event_log("host", blob);' in func_body(c, "pcnetgame_ts_refresh")
          and "pcnetgame_ts_valid_event_blob(blob)" in func_body(c, "pcnetgame_ts_refresh") and "event state would be REFUSED by clients" in c
          and "static PCNetGameTsHost s_ts_host[PC_NETGAME_TS_NUM];" in c)
    hd = func_body(c, "pcnetgame_handle_host_data")
    check("E2 the host still DROPS a client-originated TOWN_SVC_STATE (an event blob from a peer is never applied)",
          "pcnetgame_handle_client_town_svc" not in hd and "pcnetgame_ts_apply_event" not in hd and "pcnetgame_ts_client_apply" not in hd)

    # ------------------------------------------------------------------ E3 client
    cl = func_body(c, "pcnetgame_handle_client_town_svc")
    cl_regions = cl.replace("pcnetgame_ts_client_apply(&m); /* a session setting, not a save region: no usable save needed */", "")
    ok, miss = in_order(cl_regions, ["s_client_link != PC_NETGAME_LINK_READY", "sizeof(m)", "svc != (int)PC_NETGAME_TS_POLICE && svc != (int)PC_NETGAME_TS_MUSEUM",
                             "m.len != expect", "pcnetgame_fnv1a32(m.blob, m.len) != m.digest", "m.seq <= s_ts_client_seq[svc]", "pcnetgame_ts_valid_event_blob(m.blob)",
                             "pcfa_save_ready()", "pcnetgame_ts_client_apply(&m)"])
    check("E3 client validates in order (READY, size, service, exact len, digest, seq above the session's last, CONTENT, usable save) then applies; service 5 is accepted with its exact len (missing %s)" % miss,
          ok and "svc != (int)PC_NETGAME_TS_EVENT" in cl and "(uint16_t)PC_NETGAME_TS_EVENT_LEN" in cl)
    check("E3 the stash / retry path covers service 5 (applied by the tick once the local save is usable) and the per-session seq memory is reset with the session",
          "svc <= (int)PC_NETGAME_TS_EVENT; svc++" in func_body(c, "pcnetgame_ts_client_tick")
          and "memset(s_ts_client_have, 0, sizeof(s_ts_client_have));" in func_body(c, "pcnetgame_reset_client_session_state"))
    v = func_body(c, "pcnetgame_ts_valid_event_blob")
    check("E3 validator: special kind (-1 or <= mEv_SPNPC_END), scheduled date ranges, the transient flag byte must be 0, event types <= mEv_EVENT_NUM, the local player's two "
          "dates must be 0, SPECIAL3 is an hour, every month/day pair plausible (also ghost / bridge day), dozaemon 0/1, union items legal pocket items by kind, used counters 0..4",
          "sp.kind != 0xFFFFFFFFu && sp.kind > (uint32_t)mEv_SPNPC_END" in v and "sp.scheduled.month > 12 || sp.scheduled.day > 31 || sp.scheduled.hour > 23" in v
          and "p[1] != 0u" in v and "dates[mEv_SAVE_DATE_TODAY] != 0u || dates[mEv_SAVE_DATE_BIRTHDAY] != 0u || dates[mEv_SAVE_DATE_SPECIAL3] > 23u" in v
          and "pcnetgame_ev_md_ok(ghost_day)" in v and "p[27] > 1u" in v and "pcnetgame_is_pocket_legal_item(items[i])" in v and "used < 0 || used > 4" in v
          and all(k in v for k in ("mEv_SPNPC_SHOP", "mEv_SPNPC_DESIGNER", "mEv_SPNPC_BROKER", "mEv_SPNPC_ARTIST", "mEv_SPNPC_ARABIAN")))
    ap = func_body(c, "pcnetgame_ts_apply_event")
    aps = strip_comments(ap)
    check("E3 apply writes ONLY the mirrored fields into the local Save_t: special, weekly, special_event.type, weekly_event.*, dates (never TODAY / BIRTHDAY), ghost_day, bridge_*, "
          "ghost / soncho / dozaemon, event_year; forces the transient special_event.flags to 0 (a client never generates special-event contents); then notifies m_event.c",
          "&Save_Get(event_save_data).special" in ap and "&Save_Get(event_save_data).weekly" in ap and "c->special_event.flags = 0;" in ap
          and "i != (int)mEv_SAVE_DATE_TODAY && i != (int)mEv_SAVE_DATE_BIRTHDAY" in ap and "mEv_PcNotifyMirrorApplied();" in ap
          and not re.search(r"event_save_data\)?\.flags|area_use_bitfield|->area\b|last_date|valentines_day_date|white_day_date|delete_event_id|event_flags", aps))
    check("E3 never persisted: the apply is plain Save_t memory writes (no save-file / sidecar call) and pc_save_write_authoritative still refuses a network CLIENT",
          not re.search(r"pc_save_write|fopen|pc_mp_records|pc_mp_guests", aps)
          and re.search(r"pc_save_write_authoritative[\s\S]{0,1500}?PC_NETGAME_ROLE_CLIENT", read("pc/src/pc_m_card.c")) is not None)
    check("E3 client self-check: the client logs (on change) the digest of its own event state next to the last applied host digest (MATCH / DIFFERS); apply logs a decoded line",
          "local event state digest" in func_body(c, "pcnetgame_event_client_selfcheck") and "pcnetgame_event_client_selfcheck();" in func_body(c, "pcnetgame_ts_client_tick")
          and 'pcnetgame_ts_event_log("client", m->blob);' in func_body(c, "pcnetgame_ts_client_apply"))

    # ------------------------------------------------------------------ E4 gates
    gate = func_body(c, "pc_net_game_event_client_gate")
    check("E4 gate predicate: role CLIENT only (linked or not: a disconnected client keeps the last mirrored state, it never invents events), host / solo return 0; loud once-per-site log",
          "s_role != PC_NETGAME_ROLE_CLIENT" in gate and "s_client_link" not in gate and "return 1;" in gate and "[NET][EVENT] client: local %s suppressed" in gate
          and "int pc_net_game_event_client_gate(int site);" in h and "void pc_net_game_event_note_rederive(void);" in h)
    ise = func_body(ev, "init_special_event")
    iwe = func_body(ev, "init_weekly_event")
    check("E4 init_special_event (the roll seeded by the LOCAL player id / RANDOM(8)) returns FALSE on a client BEFORE the first read of the scene / date / roll and before mEv_ClearSpecialEvent",
          "pc_net_game_event_client_gate(0)" in ise and ise.index("pc_net_game_event_client_gate(0)") < ise.index("switch (Common_Get(last_scene_no))")
          and ise.index("pc_net_game_event_client_gate(0)") < ise.index("RANDOM(8)") and ise.index("pc_net_game_event_client_gate(0)") < ise.index("mEv_ClearSpecialEvent(special_ev)")
          and "return FALSE;" in ise[ise.index("pc_net_game_event_client_gate(0)"):ise.index("pc_net_game_event_client_gate(0)") + 200])
    check("E4 init_weekly_event (Gulliver's local-hour reschedule, the Wisp's RANDOM(3) date, the bridge seed) returns on a client BEFORE any write / roll",
          "pc_net_game_event_client_gate(1)" in iwe and iwe.index("pc_net_game_event_client_gate(1)") < iwe.index("RANDOM(3)")
          and iwe.index("pc_net_game_event_client_gate(1)") < iwe.index("event_dates[mEv_SAVE_DATE_WEEKLY] = sched_date")
          and iwe.index("pc_net_game_event_client_gate(1)") < iwe.index("mEv_ClearEventKabuPeddler(kabu_peddler_data)"))
    check("E4 these are the ONLY RANDOM draws of m_event.c (exactly two RANDOM( sites, both inside the gated functions): no other local roll decides an event",
          len(re.findall(r"\bRANDOM(?:_F)?\(", strip_comments(ev))) == 2 and "RANDOM(8)" in ise and "RANDOM(3)" in iwe)
    check("E4 init_special_event is also reached by mEv_make_new_special_event(); its gate makes that a no-op on a client (the roll result decides everything there)",
          "if (init_special_event(TRUE)) {" in func_body(ev, "mEv_make_new_special_event"))
    run = func_body(ev, "mEv_run")
    check("E4 mEv_run: a raised mirror flag makes the vanilla new-day branch run ONCE (event->day = 99) so today's event table follows the mirrored save fields; "
          "the flag is cleared before; the day branch itself is untouched",
          "if (s_pc_event_mirror_dirty) {" in run and "s_pc_event_mirror_dirty = 0;" in run and "event->day = 99;" in run and "pc_net_game_event_note_rederive();" in run
          and run.index("event->day = 99;") < run.index("if (event->day != day) {")
          and "extern void mEv_PcNotifyMirrorApplied(void) {\n    s_pc_event_mirror_dirty = 1;\n}" in ev and "extern void mEv_PcNotifyMirrorApplied(void);" in evh)
    mgr = em[em.index("if (Save_Get(event_save_common).special_event.flags == 1) {"):][:520]
    check("E4 event manager: the special-event CONTENTS generator (set_special_event_save: RANDOM items) is skipped on a client; the transient flag is still cleared",
          "pc_net_game_event_client_gate(2)" in mgr and mgr.index("pc_net_game_event_client_gate(2)") < mgr.index("set_special_event_save();")
          and "Save_Get(event_save_common).special_event.flags = 0;" in mgr)
    check("E4 the G7 gates of the event joint-NPC picks (m_event_map_npc.c, two sites) are still there",
          emn.count("if (pc_net_game_world_is_host_authoritative()) {") == 2)

    # ------------------------------------------------------------------ E5 inventory
    import os
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
    found = set()
    for base in ("src", "include"):
        for dp, _dn, fns in os.walk(os.path.join(root, base)):
            for fn in fns:
                if fn.endswith((".c", ".c_inc", ".h")):
                    t = open(os.path.join(dp, fn), "rb").read().decode("utf-8", "replace")
                    if re.search(r"event_save_data\b|event_save_common\b", t):
                        found.add(fn)
    documented = {"m_common_data.h", "ac_boat_demo_move.c_inc", "ac_br_shop.c", "ac_br_shop_move.c_inc", "ac_broker_design.c", "ac_event_manager.c",
                  "ac_quest_talk_normal_init.c", "ac_s_car.c", "ac_shop_design.c", "ac_ev_artist.c", "ac_ev_artist_move.c_inc", "ac_ev_broker2_move.c_inc",
                  "ac_ev_broker_move.c_inc", "ac_ev_carpetPeddler.c_inc", "ac_ev_designer_talk.c_inc", "ac_ev_dozaemon.c", "ac_ev_dozaemon_move.c_inc",
                  "ac_ev_ghost_talk.c_inc", "ac_ev_kabuPeddler_move.c_inc", "ac_ev_soncho_talk.c_inc", "m_event.c", "m_shop.c", "m_start_data_init.c"}
    check("E5 inventory: the set of game source files that touch event_save_data / event_save_common equals the DOCUMENTED set (client-side writers = the visitor NPC "
          "interaction files: broker/broker2/designer/artist/carpet/kabu peddler/dozaemon/ghost talk; they write the client's LOCAL copy, never the host's, and a later host "
          "change overwrites them: see impl_notes_events.md). New or removed: %s" % sorted(found ^ documented), found == documented)

    # ------------------------------------------------------------------ E6 test hook
    wt = func_body(c, "pcnetgame_world_test_force")
    check("E6 --world-test-force: default NULL (off), parsed + documented in --help and pc_platform.h, runs once, HOST only (refused otherwise), range checked, loud [TEST-ONLY] logs, "
          "called from the host tick after the world is ready",
          "const char* g_pc_world_test_force = NULL;" in pm and '"--world-test-force="' in pm and "--world-test-force=W,I,T,P[,M[,E]]" in pm and "g_pc_world_test_force" in pp
          and "static int done = 0;" in wt and "s_role != PC_NETGAME_ROLE_HOST" in wt and "REFUSED" in wt and "m < -180 || m > 180" in wt and "[NET][WORLD][TEST-ONLY]" in wt
          and "pcnetgame_world_test_force();" in func_body(c, "pcnetgame_host_ts_tick") and "pcnetgame_world_test_poke_event();" in func_body(c, "pcnetgame_host_ts_tick")
          and "static int poked = 0;" in func_body(c, "pcnetgame_world_test_poke_event") and "(uint32_t)(now - first_ready_ms) < 4000u" in func_body(c, "pcnetgame_world_test_poke_event")
          and "[NET][EVENT][TEST-ONLY]" in func_body(c, "pcnetgame_world_test_poke_event") and "e / 100 > 12" in wt
          and func_body(c, "pcnetgame_host_ts_tick").index("s_host_world_ready") < func_body(c, "pcnetgame_host_ts_tick").index("pcnetgame_world_test_force();"))
    check("E6 the client logs the world state it applied on the snapshot-end path too (weather, market prices) so a real-client test can compare it with the host",
          "client: snapshot-end host weather -> type" in c and "client: snapshot-end host Stalk Market schedule -> trend" in c)

    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
