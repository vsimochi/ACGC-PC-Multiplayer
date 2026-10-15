#!/usr/bin/env python3
"""test_guest_arrival_hold_src.py - SOURCE pins of hotfix 0.2.1 (guest arrival hold: remote guests parked at the train start after joining a 0.2.0 host).

TIER: SOURCE. Pins (1) the bounded arrival hold (thresholds in pc_remote_arrival_logic.h, the wiring in pc_remote_player.c), (2) the join-time bulk pushes gated on the snapshot + half the reliable
window (the snapshot-restart throttle is NOT in 0.2.1: the 0.2.0 logic is pinned), (3) the bounded 'unknown' window, (4) the diagnostics switch (settings.ini [Debug]
client_log, env AC_CLIENT_LOG, default OFF) and the [JOIN] stage trace. The pure logic is unit-tested natively by test_guest_arrival.py; the behaviour over latency by
test_guest_arrival_latency_real.py. Usage: python test_guest_arrival_hold_src.py"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))


def rd(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8", errors="replace") as f:
        return f.read()


def func(src, name):
    m = re.search(r"\n[a-zA-Z_][^\n;]*\b" + re.escape(name) + r"\([^;{]*\)\s*\{", src)
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
                return src[m.start():j + 1]
    return ""


def main():
    results = []

    def ck(d, c):
        results.append((d, bool(c)))
        print(("PASS - " if c else "FAIL - ") + d)

    hdr = rd("pc", "include", "pc_remote_arrival_logic.h")
    rp = rd("pc", "src", "pc_remote_player.c")
    ng = rd("pc", "src", "pc_net_game.c")
    ngh = rd("pc", "include", "pc_net_game.h")
    rod = rd("src", "actor", "ac_ride_off_demo_move.c_inc")
    st_c = rd("pc", "src", "pc_settings.c")
    st_h = rd("pc", "include", "pc_settings.h")
    main_c = rd("pc", "src", "pc_main.c")

    # ---- (1) bounded hold: thresholds + wiring ----
    ck("header: unknown window 3000 ms, hold 4500 ms after READY, 15000 ms in total, trace every 2000 ms",
       "#define PCARR_JOIN_UNKNOWN_WINDOW_MS 3000u" in hdr and "#define PCARR_JOIN_HOLD_READY_MS 4500u" in hdr and "#define PCARR_JOIN_HOLD_TOTAL_MS 15000u" in hdr
       and "#define PCARR_JOIN_HOLD_TRACE_MS 2000u" in hdr)
    q = func(rp, "pc_remote_arrival_join_query")
    ck("query: the hold clock is the monotonic net ms clock (pc_net_game_now_ms), a hold not polled for > 1 s starts over",
       "pc_net_game_now_ms()" in q and "(uint32_t)(now_ms - s_hold_last_ms) > 1000u" in q)
    ck("query: READY is latched once per hold (s_hold_ready_ms) and feeds both the unknown window and the timeout",
       "s_hold_ready_ms = now_ms" in q and "pcarr_join_unknown_counts(s_hold_ready_seen, since_ready_ms)" in q
       and "pcarr_join_hold_timeout(held_ms, s_hold_ready_seen, since_ready_ms)" in q)
    ck("query: a timed-out hold is decided by pcarr_join_decide_bounded and logged once ('[TRAIN] arrival join: hold timed out after')",
       "pcarr_join_decide_bounded(decided, timeout, n_stopped, n_riding)" in q and q.count("hold timed out after") == 1 and "-> starting the arrival" in q)
    ck("query: while held, the [JOIN] hold trace repeats every PCARR_JOIN_HOLD_TRACE_MS with link / roster_pending / unknown / others",
       "PCARR_JOIN_HOLD_TRACE_MS" in q and "roster_pending=%d, unknown=%d, others=%d" in q and "pc_net_game_join_trace(\"arrival hold:" in q)
    ck("query: still answers 1 at once for a non-client (single-player / host: the vanilla arrival untouched)",
       re.search(r"if \(pc_net_game_role\(\) != PC_NETGAME_ROLE_CLIENT\) \{\s*return 1;", q) is not None)
    fs = func(rod, "aROD_first_set")
    ck("ride-off: first_set still parks only while the query answers 0 (unchanged caller; the bound lives in the query)",
       "if (!pc_remote_arrival_join_query(&s_aROD_join_class, &s_aROD_join_x))" in fs and "mPlib_request_main_demo_wait_type1" in fs)

    # ---- (2) host join-burst throttle ----
    # The snapshot-restart throttle (deferral to SNAPSHOT_END, owed acres) was NOT shipped in 0.2.1 (lead decision: unexercised at runtime): the 0.2.0 logic must stand.
    import subprocess
    head = subprocess.run(["git", "show", "HEAD:pc/src/pc_net_game.c"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    ck("0.2.0 kept: pcnetgame_host_send_out_list is byte-identical to HEAD (window full / failed send -> snapshot restart)",
       head != "" and func(ng, "pcnetgame_host_send_out_list") == func(head, "pcnetgame_host_send_out_list"))
    strip = lambda t: "\n".join(l for l in t.split("\n") if "pc_net_game_join_trace" not in l)  # noqa: E731
    ck("0.2.0 kept: the snapshot pump equals HEAD apart from the [JOIN] END trace line",
       strip(func(ng, "pcnetgame_host_pump_snapshots")) == func(head, "pcnetgame_host_pump_snapshots"))
    ss = func(ng, "pcnetgame_host_start_snapshot")
    ck("0.2.0 kept: start_snapshot restarts mid-snapshot as before (no deferral / owed state anywhere)",
       "snap_restart_owed" not in ng and "snap_owed_acres" not in ng and "start_snapshot_ex" not in ng and "st->snap_active = 1;" in ss)
    jp = func(ng, "pcnetgame_host_join_push_ok")
    ck("join push gate: READY, no snapshot under way, backlog < PC_NET_RELIABLE_WINDOW / 2",
       "#define PC_NETGAME_JOIN_PUSH_BACKLOG_LIMIT (PC_NET_RELIABLE_WINDOW / 2)" in ng and "s_host_peer[p].snap_active" in jp
       and "backlog < PC_NETGAME_JOIN_PUSH_BACKLOG_LIMIT" in jp)
    pg = func(ng, "pcnetgame_page_host_tick")
    ck("pages: gated per message, NOT marked sent when gated (resumed later: no page is lost)",
       "pcnetgame_host_join_push_ok(p)" in pg and pg.index("pcnetgame_host_join_push_ok(p)") < pg.index("s_pg_sent[p][i] = s_pg_rev[i]"))
    ck("outfits (msg 90): gated, s_outfit_sent only set after a successful send", "s_outfit_seq && pcnetgame_host_join_push_ok(p)" in func(ng, "pcnetgame_outfit_host_tick"))
    ck("mailbox letters (guest + resident): gated before the send, sent seq only recorded after it",
       "!pcnetgame_host_join_push_ok(p) || !pcnetgame_guest_mbox_send(" in func(ng, "pcnetgame_host_guest_mbox_push")
       and "!pcnetgame_host_join_push_ok(p) || !pcnetgame_mbox_send(" in func(ng, "pcnetgame_host_mbox_tick"))
    ck("wire: no protocol change (PC_NETGAME_PROTOCOL_VERSION still 8)", "#define PC_NETGAME_PROTOCOL_VERSION 8u" in ngh)

    # ---- (4) diagnostics ----
    ck("settings: client_log exists, default 0, accepted only as 0/1, written under [Debug]",
       "int client_log;" in st_h and ".client_log = 0," in st_c and "strcmp(key, \"client_log\") == 0" in st_c and "\"client_log = 0\\n\"" in st_c
       and "\"[Debug]\\n\"" in st_c and "client_log = %d\\n" in st_c)
    ck("client_log: env AC_CLIENT_LOG overrides, never for --dedicated, file next to the exe, rotated at 4 MB",
       "getenv(\"AC_CLIENT_LOG\")" in main_c and "if (g_pc_dedicated || !pc_client_log_wanted())" in main_c and "GetModuleFileNameA" in main_c
       and "#define PC_CLIENT_LOG_MAX_BYTES (4L * 1024L * 1024L)" in main_c and "client_log.old.txt" in main_c)
    ck("client_log: started right after the settings load; rotation checked from the event poll",
       re.search(r"pc_settings_load\(\);\s*pc_client_log_start\(\);", main_c) is not None and "pc_client_log_tick();" in func(main_c, "pc_platform_poll_events"))
    stages = ["client: transport connected", "client: identity sent", "client: READY", "client: world snapshot epoch", "client: first world snapshot applied",
              "client: record push adopted", "host: peer %d snapshot epoch %u begin", "host: peer %d snapshot epoch %u END sent", "snapshot epoch %u RESTARTED"]
    ck("[JOIN] trace: every stage is traced (" + ", ".join(stages) + ")", all(s in ng for s in stages))
    ck("[JOIN] trace: arrival train actions and 'control released' in the ride-off demo (TARGET_PC)",
       "pc_net_game_join_trace(\"arrival: train action %d (%s)\"" in rod and "pc_net_game_join_trace(\"arrival: control released" in rod)

    bad = [d for d, ok in results if not ok]
    print("-" * 60)
    print("%d/%d checks passed" % (len(results) - len(bad), len(results)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
