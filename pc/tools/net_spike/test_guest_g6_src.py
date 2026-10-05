#!/usr/bin/env python3
"""test_guest_g6_src.py - GUESTS G6 (reliability / release readiness: readable join failures, operator tools, corrupt storage, shutdown) SOURCE AUDIT (no game process).

  A   G6.1 readable join failures (NO wire change): every reject reason of the frozen PCNetGameRejectReason enum has a `case` in pcnetgame_reject_text() and a readable
      sentence (SERVER_FULL names "server full / guest limit reached / all resident slots taken"; PROTOCOL_MISMATCH names BOTH versions; the town reasons name both
      towns / the host town); the REJECT handler builds the text BEFORE pc_net_game_shutdown() and keeps the numeric reason in its log line; the other silent exits
      (different token, IDENTITY_ACK mismatch, closed during the handshake, token not saved) and the unreachable-host warning set a message too; the message buffer
      is printed to stdout AND stderr, survives pc_net_game_shutdown(), is cleared by a new connection attempt, and is drawn by pc_net_notice_draw() (graph_main,
      every frame: title screen AND in game) before the unchanged not-connected notice.
  B   G6.2 operator tools: `guests` / `guest-remove` / `guest-reset-token` exist in the console + help; the admin function refuses (in this order) a non-host,
      UNTRUSTED storage, an unknown / ambiguous selector, a BOUND guest, then needs `confirm`, then makes the BACKUP of guests.dat (and refuses without it) BEFORE it
      touches the table; removal is rolled back when guests.dat cannot be written; the listing never prints a token; the recovery is host-local (in memory only:
      not in the file builder, not on the wire), one use, time limited, keeps the record, never bypasses the key / validity / UNTRUSTED checks and the per-address
      mint limit.
  C   G6.3: guests.dat is never written while UNTRUSTED (existing pin); the client refuses to arrive when its guest home land equals the loaded town (before anything
      is bound); the host keeps its own check; the guests.dat write after the GCI save hook is unchanged.
  D   no wire change / no protocol bump / no message 59 / no new reject reason; pc_net_game.h gained exactly one function declaration; the diff of pc_net_game.c vs
      HEAD removes no line; resident / single-player code paths are not touched (the touched source files are exactly the expected set).
  E   G6.0 test debris: the cleanup helper exists, only acts on disposable fixture dirs (functional check on throw-away dirs), and every guest real-client test calls it
      after its own restore.
  T   the new tests exist.
Tier: SOURCE AUDITED (+ a functional check of the python cleanup helper on temp dirs). Exit code 0 when all checks pass."""
import os
import re
import shutil
import subprocess
import sys
import tempfile

import net_spike_lib as L
import test_guest_src as S
import wire_baseline

ROOT, HERE = S.ROOT, S.HERE


# The G6 diff audits compare the G6 feature commit against its parent (HEAD when G6 started), NOT against HEAD: HEAD contains G6 since 91de602 and may carry later work.
BASELINE = "cb45bb2"
G6_COMMIT = "91de602"


def numstat(rel):
    out = subprocess.run(["git", "-C", ROOT, "diff", "--numstat", BASELINE, G6_COMMIT, "--", rel], capture_output=True, check=True, timeout=60).stdout.decode().split()
    return (int(out[0]), int(out[1])) if out else (0, 0)


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    ng_raw = S.read("pc/src/pc_net_game.c")
    ng = S.mask(ng_raw)
    nf = S.functions(ng)

    def fb(name):
        return S.body(ng, nf, name)

    def fr(name):
        return S.body(ng_raw, S.functions(ng_raw), name)

    # ------------------------------------------------------------------ A
    enum = re.search(r"typedef enum PCNetGameRejectReason \{(.*?)\} PCNetGameRejectReason;", ng, re.S).group(1)
    reasons = re.findall(r"(PC_NETGAME_REJECT_\w+)\s*=\s*(\d+)", enum)
    rt = fr("pcnetgame_reject_text")
    ck("A the reject enum keeps the frozen reasons 1 PROTOCOL_MISMATCH, 2 SERVER_FULL, 3 LAND_MISMATCH, 4 NO_SAVE and has exactly TWO deliberate additions: 5 RESIDENT_CREDENTIAL (M-E) and 6 PROMOTED (M-F)", [(n, int(v)) for n, v in reasons] == [
        ("PC_NETGAME_REJECT_PROTOCOL_MISMATCH", 1), ("PC_NETGAME_REJECT_SERVER_FULL", 2), ("PC_NETGAME_REJECT_LAND_MISMATCH", 3), ("PC_NETGAME_REJECT_NO_SAVE", 4),
        ("PC_NETGAME_REJECT_RESIDENT_CREDENTIAL", 5), ("PC_NETGAME_REJECT_PROMOTED", 6)])
    ck("A pcnetgame_reject_text has a `case` for EVERY reject reason of the enum and a `default` for unknown ones", all("case %s:" % n in rt for n, _v in reasons) and "default:" in rt)
    ck("A SERVER_FULL says 'server full / guest limit reached / all resident slots taken' (the code cannot tell them apart) for a guest, and names the other causes (guest table full, "
       "in use, missing / wrong token) plus the guest-reset-token recovery; a resident gets the resident wording",
       "server full / guest limit reached / all resident slots taken" in rt and "guest table is" in rt and "guest-reset-token" in rt and "server full / all resident slots taken" in rt
       and "s_client_ext_sent.token_present" in rt)
    ck("A PROTOCOL_MISMATCH prints BOTH versions (host + this game); LAND_MISMATCH prints both towns and tells the user to copy the host's town save; NO_SAVE names the host town and "
       "offers the guest route",
       re.search(r"case PC_NETGAME_REJECT_PROTOCOL_MISMATCH:.*?host %u, this game %u.*?host_protocol,\s*\(unsigned\)PC_NETGAME_PROTOCOL_VERSION", rt, re.S) is not None
       and re.search(r"case PC_NETGAME_REJECT_LAND_MISMATCH:.*?Your town \(%s\).*?host's town \(%s\).*?save/card_a.*?, b, a\)", rt, re.S) is not None
       and re.search(r"case PC_NETGAME_REJECT_NO_SAVE:.*?host \(town %s\).*?join as a guest.*?, a\)", rt, re.S) is not None)
    h = fr("pcnetgame_handle_client_data")
    i_rej = h.index("host rejected the connection (reason=")
    i_txt = h.index("pcnetgame_reject_text(", i_rej)
    i_msg = h.index("pcnetgame_join_message_set(0, \"%s (reason %u)\"", i_txt)
    i_down = h.index("pc_net_game_shutdown();", i_msg)
    ck("A the REJECT handler: numeric reason log line first, the text is built and the message set BEFORE pc_net_game_shutdown(), and the message carries '(reason N)'",
       i_rej < i_txt < i_msg < i_down and "(unsigned)in.reason, (unsigned)in.expected_protocol_version" in h[i_rej:i_txt])
    tk = fr("pcnetgame_handle_client_identity_token")
    i_diff = tk.index("DIFFERENT guest token")
    ck("A the 'received a new token while holding an old one' path sets a readable message (naming guest_token.dat and the operator recovery) BEFORE it shuts the client down",
       "pcnetgame_join_message_set(0, \"The host gave you a DIFFERENT guest token" in tk and tk.index("pcnetgame_join_message_set(0", i_diff) < tk.index("pc_net_game_shutdown();", i_diff)
       and "guest-reset-token" in tk and "pc_guest_token_path());\n            pc_net_game_shutdown();" in tk.replace("\r", ""))
    ck("A a token that could not be saved also tells the user (not only the log)", "Your guest token could not be saved to %s" in tk or "Your guest token could not be saved to %s" in ng)
    ia = ng[ng.index("IDENTITY_ACK does not match"):]
    ia = ia[:ia.index("pc_net_game_shutdown();")]
    ck("A an IDENTITY_ACK mismatch sets a message first: protocol versions (both numbers) or the two towns", "host runs another network version (host %u, this game %u)" in ia and "does not match your town" in ia)
    ev = ng[ng.index("case PC_NET_EVENT_PEER_DISCONNECTED:\n                    if (s_client_link == PC_NETGAME_LINK_CONNECTING"):] if "case PC_NET_EVENT_PEER_DISCONNECTED:\n                    if (s_client_link == PC_NETGAME_LINK_CONNECTING" in ng else ""
    ck("A a transport close during CONNECTING / HANDSHAKE (older host, stopped host, network error) sets a message naming the host address", "closed the connection before you could join" in ev
       and ev.index("pcnetgame_join_message_set(0") < ev.index("s_client_link = PC_NETGAME_LINK_DISCONNECTED;"))
    nu = fr("pcnetgame_client_notice_update")
    ck("A host unreachable: after PC_NETGAME_CONNECT_WARN_MS in CONNECTING a 'No answer from the host at <ip:port> after N s ... Still trying' warning (non final) is set once and withdrawn as "
       "soon as the transport answers", "PC_NETGAME_CONNECT_WARN_MS" in nu and "No answer from the host at %s after %u s" in nu and "pcnetgame_join_message_set(1," in nu
       and "pcnetgame_join_message_clear();" in nu and "s_client_connect_warned" in nu)
    setter = fr("pcnetgame_join_message_set")
    ck("A the message is printed to stdout AND stderr ([NET][JOIN]) and flushed", "printf(\"[NET][JOIN] %s: %s\\n\"" in setter and "fprintf(stderr, \"[NET][JOIN] %s: %s\\n\"" in setter and "fflush(stderr)" in setter)
    sd = fr("pc_net_game_shutdown")
    sc = fr("pc_net_game_start_client")
    ck("A the message outlives pc_net_game_shutdown() (the client plays on alone) and a new connection attempt clears it", "s_join_msg" not in sd and "pcnetgame_join_message_clear" not in sd
       and "pcnetgame_join_message_clear();" in sc)
    acc = fr("pc_net_game_join_message")
    ck("A the accessor is role independent and a FINAL message expires after PC_NETGAME_JOIN_MSG_SHOW_MS (30 s) while a warning stays", "s_role" not in acc and "PC_NETGAME_JOIN_MSG_SHOW_MS" in acc
       and "#define PC_NETGAME_JOIN_MSG_SHOW_MS    30000u" in ng)
    pm = S.read("pc/src/pc_pause_menu.c")
    nd = S.body(S.mask(pm), S.functions(S.mask(pm)), "pc_net_notice_draw")
    gr = S.read("src/graph.c")
    ck("A pc_net_notice_draw draws the join message first (same PC font overlay path, word wrapped, header 'Cannot join the host:' / 'Waiting for the host:'), returns, and leaves the "
       "not-connected notice unchanged; graph_main still calls it every frame (title + in game) under TARGET_PC",
       nd.index("pc_net_game_join_message(&join_warning)") < nd.index("pc_net_game_client_notice_visible()") and "pc_net_join_message_draw(game, join_msg, join_warning);" in nd
       and "Cannot join the host:" in pm and "Waiting for the host:" in pm and "Not connected to the host" in pm
       and re.search(r"#ifdef TARGET_PC\n    pc_pause_menu_draw\(game\);\n    pc_net_notice_draw\(game\);[^\n]*\n#endif", gr) is not None and numstat("src/graph.c") == (0, 0))
    ck("A the declaration is in pc_net_game.h next to the existing notice accessor", S.read("pc/include/pc_net_game.h").count("const char* pc_net_game_join_message(int* is_warning);") == 1)

    # ------------------------------------------------------------------ B
    ded = S.read("pc/src/pc_dedicated.c")
    ex = ded[ded.index("static void pc_ded_execute"):]
    ck("B the console knows guests / guest-remove / guest-reset-token and `help` lists them; the arguments keep their case (guest names), only the command word is lower-cased",
       all(x in ex for x in ('strcmp(cmd, "guests") == 0', 'strcmp(cmd, "guest-remove") == 0', 'strcmp(cmd, "guest-reset-token") == 0'))
       and "args = cmd + i + 1;" in ex and "guest-remove <slot|name> confirm" in ded and "guest-reset-token <slot|name> confirm" in ded)
    ga = ded[ded.index("static void pc_ded_cmd_guest_admin"):]
    ga = ga[:ga.index("static void pc_ded_cmd_stop")]
    ck("B the command line needs EXACTLY `<slot|name> confirm`: another word / extra arguments change nothing; `confirm` is the explicit consent passed to the admin function",
       "n >= 3 || (n == 2 && pc_ded_stricmp(tok2, \"confirm\") != 0)" in ga and "pc_net_game_dedicated_guest_admin(op, sel, n == 2," in ga)
    gl = ded[ded.index("static void pc_ded_cmd_guests"):ded.index("static void pc_ded_cmd_guest_admin")]
    ck("B the listing prints slot / name / home town / host town / confirmed / rev / bound / recovery -- never a token (no token argument, no hex formatting)", "gi.slot, gi.name, gi.home_town, gi.town" in gl
       and not re.search(r"token|%02x|%02X|%x", re.sub(r'"(?:[^"\\]|\\.)*"', '""', S.mask(gl))))
    st = S.read("pc/include/pc_dedicated.h")
    ck("B the info struct has no token member", "PCNetGameDedicatedGuestInfo" in st and not re.search(r"\btoken\b\s*[\[;]", S.mask(re.search(r"typedef struct PCNetGameDedicatedGuestInfo \{.*?\} PCNetGameDedicatedGuestInfo;", st, re.S).group(0))))
    ad = fr("pc_net_game_dedicated_guest_admin")
    order = [ad.index(x) for x in ("s_role != PC_NETGAME_ROLE_HOST || !s_host_world_ready", "if (s_guest_untrusted) {", "pcnetgame_dedicated_guest_resolve(sel, msg, cap)", "if (peer >= 0) {",
                                   "if (!confirm) {", "pc_mp_guests_backup_file(PC_MP_GUESTS_PATH, bak, sizeof(bak))", "s_guest[g].recovery = 1;", "memset(&s_guest[g], 0, sizeof(s_guest[g]));")]
    ck("B the admin function's order: host+world -> UNTRUSTED refusal -> selector (unknown / ambiguous) -> BOUND refusal -> `confirm` -> BACKUP -> only then the table is touched", order == sorted(order))
    ck("B a failed backup REFUSES the command (nothing is changed) and the removal is ROLLED BACK when guests.dat cannot be written",
       re.search(r"if \(!pc_mp_guests_backup_file\(PC_MP_GUESTS_PATH, bak, sizeof\(bak\)\)\) \{[^}]*return 0;", ad, re.S) is not None
       and re.search(r'if \(!pcnetgame_guest_store_write\("operator removed a guest"\)\) \{\s*s_guest\[g\] = old_g;\s*s_guest_rec\[g\] = old_rec;\s*s_rec_slot\[PLAYER_NUM \+ g\] = old_rs;', ad) is not None)
    ck("B the bound check uses the host's own binding table (READY peers bound to the guest slot), the selector is a single digit 0..7 or a unique case-insensitive name", "pcnetgame_host_peer_bound_to_guest(g, (PCNetPeerId)-1)" in ad
       and "matches %d guest entries" in fr("pcnetgame_dedicated_guest_resolve") and "sel[0] >= '0' && sel[0] <= '7' && sel[1] == '\\0'" in fr("pcnetgame_dedicated_guest_resolve"))
    inf = fr("pc_net_game_dedicated_guest_info")
    ck("B guest_info copies ASCII names / flags only (no token member is read)", ".token" not in inf)
    bld = fr("pcnetgame_guest_store_build")
    ck("B the recovery state is host-local and in memory only: not written by the file builder, not part of any wire struct, not in the file format header",
       "recovery" not in bld and "recovery" not in S.read("pc/include/pc_mp_guests.h") and "recovery" not in S.read("pc/src/pc_mp_guests.c")
       and not re.search(r"typedef struct PCNet\w*(?:Msg|Spec)[^{]*\{[^}]*recovery", ng, re.S))
    gc = fr("pcnetgame_host_guest_check")
    i_rec = gc.index("pcnetgame_guest_recovery_active(g)")
    ck("B the recovery branch sits AFTER the validity / home-land / key-conflict / UNTRUSTED checks and the authenticated-token branch, and BEFORE the unconfirmed-entry re-mint; it re-mints the "
       "SAME entry (out_slot = g, mode 2: the stored record is kept)",
       gc.index("pcnetgame_guest_key_conflict(key)") < gc.index("if (s_guest_untrusted) {") < gc.index("const int tok_ok") < i_rec < gc.index("pcnetgame_guest_entry_disposable(g)")
       and "*out_slot = g;" in gc[i_rec:i_rec + 900] and "*out_mode = 2;" in gc[i_rec:i_rec + 900])
    pi = fr("pcnetgame_host_process_identity")
    ck("B a recovery claim passes the same admission gates as any re-mint: the duplicate-binding check (a bound guest is never replaced by a token-less claim), the guest cap and the per-address "
       "mint limit all come BEFORE pcnetgame_guest_remint()", pi.index("other >= 0") < pi.index("pcnetgame_host_guest_cap_refusal") < pi.index("pcnetgame_guest_mint_allowed(mint_ip, mint_now)")
       < pi.index("pcnetgame_guest_remint(guest_slot, guest_token, guest_old_token)"))
    rm = fr("pcnetgame_guest_remint")
    ck("B the re-mint USES UP the recovery (one use) and marks the entry unconfirmed (the new token is unproven until presented); a rollback restores both; an expired recovery is dropped",
       "s_guest[g].recovery = 0;" in rm and "s_guest[g].confirmed = 0;" in rm and "s_guest[g].recovery = s_guest_remint_prev_recovery;" in rm
       and "s_guest[g].confirmed = s_guest_remint_prev_confirmed;" in fr("pcnetgame_guest_remint_rollback")
       and "PC_NETGAME_GUEST_RECOVERY_MS" in fr("pcnetgame_guest_recovery_active") and "#define PC_NETGAME_GUEST_RECOVERY_MS 600000u" in ng)
    bk = S.read("pc/src/pc_mp_guests.c")
    bkf = bk[bk.index("int pc_mp_guests_backup_file("):]
    ck("B the backup is a byte copy to '<path>.bak-<timestamp>' that never overwrites an existing backup and never touches the source (source opened 'rb'; only OUR partial copy is removed)",
       '"%s.bak-%s"' in bkf and "!file_exists(out_path)" in bkf and 'fopen(path, "rb")' in bkf and "remove(out_path)" in bkf and "remove(path)" not in bkf and "written != total" in bkf)

    # ------------------------------------------------------------------ C
    ck("C guests.dat is never written while UNTRUSTED and admission is refused then (existing pins still in the source)", "if (!s_guest_store_loaded || s_guest_untrusted) {" in fb("pcnetgame_guest_store_write")
       and "guest admission disabled: guests.dat is UNTRUSTED" in ng)
    mc = S.read("pc/src/pc_m_card.c")
    arr = S.body(S.mask(mc), S.functions(S.mask(mc)), "pc_guest_arrive")
    i_land = arr.index("home.land_id == (u16)Save_Get(land_info.id)")
    ck("C the client refuses to arrive when its guest home land (id AND name) equals the loaded town, BEFORE anything is bound (before the passport is built / Common_Set), with a message naming "
       "guest.ini and the new-guest consequence", i_land < arr.index("memset(&l_mcd_foreigner_file") < arr.index("Common_Set(now_private, pass)")
       and "memcmp(home.land_name, Save_Get(land_info.name), sizeof(home.land_name)) == 0" in arr and "Edit land_id in" in arr and "NEW guest" in mc[mc.index("Edit land_id in"):mc.index("Edit land_id in") + 200])
    ck("C the host keeps its own check of a guest home land equal to the town land", "the guest's home land is this town's land (a guest comes from another town)" in ng)
    ck("C the guests.dat write after the successful town GCI write hook is unchanged (pc_net_game_record_after_gci_save still calls pcnetgame_guest_store_write after the records write)",
       'pcnetgame_guest_store_write("after the GCI save")' in fr("pc_net_game_record_after_gci_save"))

    # ------------------------------------------------------------------ D
    ck("D no wire change: message ids 1..66 (66 = M-F promotion handoff) (59..61 = the furniture sync, 62..65 = the town transfer, none is a REJECT_INFO), the protocol version unchanged, wire_baseline green",
       sorted(dict(wire_baseline.c_message_ids(ng_raw)).values()) == list(range(1, 67)) and "PC_NETGAME_MSG_REJECT_INFO" not in ng_raw)
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    ck("D wire_baseline: %d checks, all green" % len(wb), wb and all(c for _d, c in wb))
    hdr = subprocess.run(["git", "-C", ROOT, "diff", "-U0", BASELINE, G6_COMMIT, "--", "pc/include/pc_net_game.h"], capture_output=True, check=True, timeout=60).stdout.decode().replace("\r\n", "\n")
    added = [l[1:] for l in hdr.split("\n") if l.startswith("+") and not l.startswith("+++")]
    ck("D pc_net_game.h gained exactly the one pinned function declaration (5 added lines, 0 removed, no typedef / define)", numstat("pc/include/pc_net_game.h") == (5, 0)
       and sum(1 for l in added if l.startswith("const char* pc_net_game_join_message(")) == 1 and not any(re.match(r"\s*(typedef|#define|enum)\b", l) for l in added))
    ck("D pc_net.c / pc_net.h / net_spike_lib wire constants untouched by G6 (net_spike_lib only gained the cleanup helper)", numstat("pc/src/pc_net.c") == (0, 0) and numstat("pc/include/pc_net.h") == (0, 0)
       and numstat("pc/tools/net_spike/net_spike_lib.py")[1] == 0)
    ck("D pc_net_game.c is purely additive vs HEAD (0 removed lines)", numstat("pc/src/pc_net_game.c")[1] == 0 and numstat("pc/src/pc_net_game.c")[0] > 0)
    changed = sorted(subprocess.run(["git", "-C", ROOT, "diff", "--name-only", BASELINE, G6_COMMIT, "--", "src", "pc/src", "pc/include", "include"], capture_output=True, check=True, timeout=60).stdout.decode().split())
    ck("D resident / single-player byte identity: the touched source files are exactly the expected set (%s)" % changed,
       changed == ["pc/include/pc_dedicated.h", "pc/include/pc_mp_guests.h", "pc/include/pc_net_game.h", "pc/src/pc_dedicated.c", "pc/src/pc_m_card.c", "pc/src/pc_mp_guests.c",
                   "pc/src/pc_net_game.c", "pc/src/pc_pause_menu.c"])
    ck("D pc_m_card.c: only the one pinned guest-arrival refusal was added (8 lines, nothing removed)", numstat("pc/src/pc_m_card.c") == (8, 0))
    ck("D pc_pause_menu.c: only the join-message drawing was added (the existing not-connected draw lines are unchanged: 1 line replaced by the split guard)", numstat("pc/src/pc_pause_menu.c") == (49, 1))

    # ------------------------------------------------------------------ E
    lib = S.read("pc/tools/net_spike/net_spike_lib.py")
    ck("E net_spike_lib.discard_fixture_guest_mp exists and acts only on bin_fixture4* / the disposable clone (and never on a dir bin_dir_refusal() refuses)",
       "def discard_fixture_guest_mp(" in lib and "is_fixture_bin_dir(bin_dir) or _norm_dir(bin_dir) == _norm_dir(CLONE_GAME_BIN_DIR)" in lib and "bin_dir_refusal(bin_dir) is not None" in lib)
    tmp = tempfile.mkdtemp(prefix="g6_cleanup_")
    try:
        fx = os.path.join(tmp, "bin_fixture4_probe")
        other = os.path.join(tmp, "bin_other_probe")
        snap = os.path.join(tmp, "snap")
        for d in (os.path.join(fx, "save", "mp"), os.path.join(other, "save", "mp"), os.path.join(snap, "mp")):
            os.makedirs(d)
            open(os.path.join(d, "guest.ini"), "w").write("x")
        L._FIXTURE_AUTO_GUARDS[L._norm_dir(fx)] = snap
        rem = L.discard_fixture_guest_mp(fx)
        rem2 = L.discard_fixture_guest_mp(other)
        ck("E functional: a fixture-named dir loses save/mp AND the pending auto-snapshot's mp; a non-fixture dir is left alone (%s / %s)" % (rem, rem2),
           len(rem) == 2 and not os.path.exists(os.path.join(fx, "save", "mp")) and not os.path.exists(os.path.join(snap, "mp")) and rem2 == []
           and os.path.exists(os.path.join(other, "save", "mp", "guest.ini")) and os.path.isdir(os.path.join(fx, "save")))
        prot = L.discard_fixture_guest_mp(L.PROTECTED_GAME_BIN_DIR)
        live = L.discard_fixture_guest_mp(L.LIVE_GAME_BIN_DIR)
        ck("E functional: the PROTECTED bin_talkfix dir and the LIVE bin dir are refused (nothing removed: %s / %s)" % (prot, live), prot == [] and live == [])
    finally:
        L._FIXTURE_AUTO_GUARDS.pop(L._norm_dir(os.path.join(tmp, "bin_fixture4_probe")), None)
        shutil.rmtree(tmp, ignore_errors=True)
    for t in ("test_guest_g2_real.py", "test_guest_g3_real.py", "test_guest_g4_real.py", "test_guest_real_client.py"):
        src = S.read("pc/tools/net_spike/" + t)
        ck("E %s calls L.discard_fixture_guest_mp(L.GAME_BIN_DIR) after its own save restore" % t, src.count("L.discard_fixture_guest_mp(L.GAME_BIN_DIR)") == 1
           and src.index("shutil.copytree(snap_dir, save_dir)" if "shutil.copytree(snap_dir, save_dir)" in src else "rig.restore_save()") < src.index("L.discard_fixture_guest_mp(L.GAME_BIN_DIR)"))

    # ------------------------------------------------------------------ T
    for t in ("test_guest_g6_protocol.py", "test_guest_g6_real.py", "test_guest_g6_src.py"):
        ck("T %s exists" % t, os.path.isfile(os.path.join(HERE, t)))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
