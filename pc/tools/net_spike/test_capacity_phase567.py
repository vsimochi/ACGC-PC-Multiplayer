#!/usr/bin/env python3
"""test_capacity_phase567.py - player-capacity project, PHASES 5 + 6 + 7 (interest management / roster deltas, dynamic puppets, shared guest state).

TIER: native MODEL tests (Linux gcc, also ASan/UBSan) + SOURCE AUDITS. No game process: the game layer (pc_net_game.c, pc_remote_player.c) is only syntax-checked against HEAD here.
 PHASE 5  interest_roster_selftest.c   pc_interest.c (MOVE relay tiers) and pc_roster.c (roster deltas with retry / ordering), ids 7/8/9/10 and 254 peers
 PHASE 6  scale_regression_selftest.c  pc_puppet_pool.c (dynamic puppet slots) + the roster + interest: THE REAL BUG (8 clients + a 9th guest), 254 clients, leave / rejoin cleanup
 PHASE 7  guest_state_selftest.c       pc_keytab.c, pc_tabfile.c (work_jobs.dat v3 + the v2 reader), pc_dayclaims.c (K.K. per guest): the 65th work character, a second guest's song
 plus the Phase 3 guest store, Phase 4 admission (with the real transport over loopback) and the Phase 2 peer table again, and source audits of the game files.
NOT covered (needs the Windows / MSYS2 game build): the real pump / handlers in pc_net_game.c and pc_remote_player.c running against real clients, real puppets, the real actor pool.
Exit code 0 when all checks pass."""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
CC = os.environ.get("CC", "gcc")
PARENT = "ee140fe"  # capacity phase 4 (the last commit before this work)
results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("PASS - " if ok else "FAIL - ") + name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def parent(rel):
    return subprocess.run(["git", "-C", ROOT, "show", "%s:%s" % (PARENT, rel)], capture_output=True, text=True).stdout.replace("\r\n", "\n")


def strip_comments(t):
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", t, flags=re.S))


def run_c(scratch, name, srcs, args=(), sanitize=False, shim_tu=False, timeout=900):
    tag = name + (" [ASan+UBSan]" if sanitize else "")
    san = ["-fsanitize=address,undefined", "-fno-sanitize-recover=undefined"] if sanitize else []
    base = [CC, "-std=gnu11", "-Wall", "-Wextra", "-O1"] + san + ["-I", os.path.join(PC, "include")]
    exe = os.path.join(scratch, name + ("_asan" if sanitize else ""))
    objs, ok = [], True
    units = [(os.path.join(HERE, name + ".c"), shim_tu)] + [(os.path.join(PC, "src", s), False) for s in srcs]
    for i, (src, shim) in enumerate(units):
        obj = os.path.join(scratch, "%s_%d%s.o" % (name, i, "_a" if sanitize else ""))
        cmd = base + (["-D_WIN32", "-I", os.path.join(HERE, "winshim")] if shim else []) + ["-c", src, "-o", obj]
        cp = subprocess.run(cmd, capture_output=True, text=True)
        if cp.returncode != 0 or cp.stderr.strip():
            print(cp.stderr[:1500])
            ok = False
        objs.append(obj)
    check("compile %s (-Wall -Wextra, warning-free)" % tag, ok)
    if not ok:
        return None
    lp = subprocess.run(base + objs + ["-o", exe], capture_output=True, text=True)
    if lp.returncode != 0:
        print(lp.stderr[:1500])
        check("link %s" % tag, False)
        return None
    rargs = list(args)
    for a in rargs:
        if a.startswith(scratch):
            os.makedirs(a, exist_ok=True)
    rp = subprocess.run([exe] + rargs, capture_output=True, text=True, timeout=timeout)
    m = re.search(r"RESULT passed=(\d+) failed=(\d+)", rp.stdout)
    for ln in rp.stdout.splitlines():
        if ln.startswith("FAIL: "):
            check("%s: %s" % (tag, ln[6:]), False)
    check("%s: all checks pass (exit 0, %s)" % (tag, m.group(0) if m else "no RESULT; stderr: " + rp.stderr[-300:]), rp.returncode == 0 and m is not None and m.group(2) == "0")
    return rp.stdout


def typedef_blocks(text, suffix):
    """name -> text of every `typedef struct|enum ... { ... } Name;` block whose name ends with `suffix` (one nesting level of braces is enough for the wire structs)."""
    blocks = {}
    for b in re.findall(r"typedef (?:struct|enum)[^{;]*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}\s*\w+\s*;", text):
        m = re.search(r"\}\s*(\w+)\s*;\s*$", b)
        if m and m.group(1).endswith(suffix):
            blocks[m.group(1)] = b
    return blocks


def main():
    scratch = os.path.join(tempfile.gettempdir(), "acmp_capacity567")
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)

    # ---------------- PHASE 5 / 6 / 7 native model tests ----------------
    out = run_c(scratch, "interest_roster_selftest", ["pc_interest.c", "pc_roster.c", "pc_peer_table.c"])
    if out is not None:
        need = ["the 9th client (wire id 9): every existing client (0..7) receives its roster entry", "the host id 8 is never a destination", "a 10th client",
                "a departure is owed to every other READY peer", "peer 2's window is full", "254 peers: everyone ends up knowing", "100 peers leave"]
        check("interest/roster test ran the join / leave / blocked-window / 254-peer scenarios", all(any(n in ln for ln in out.splitlines()) for n in need))
    run_c(scratch, "interest_roster_selftest", ["pc_interest.c", "pc_roster.c", "pc_peer_table.c"], sanitize=True)
    out = run_c(scratch, "scale_regression_selftest", ["pc_interest.c", "pc_roster.c", "pc_puppet_pool.c", "pc_peer_table.c"])
    if out is not None:
        need = ["LEGACY 9-slot table (the old code): existing clients 0..7 can NOT see client 9", "DYNAMIC slots: every existing client (0..7) has a puppet slot for client 9",
                "254 clients (ids 0..254 except 8)", "99 peers leave", "id 9 is reused by a NEW peer", "with interest management every player still has a visible puppet"]
        check("regression test reproduced the old bug with the legacy table and showed it fixed with dynamic slots", all(any(n in ln for ln in out.splitlines()) for n in need))
    run_c(scratch, "scale_regression_selftest", ["pc_interest.c", "pc_roster.c", "pc_puppet_pool.c", "pc_peer_table.c"], sanitize=True)
    gs = ["pc_keytab.c", "pc_tabfile.c", "pc_dayclaims.c"]
    out = run_c(scratch, "guest_state_selftest", gs, [os.path.join(scratch, "gstate")])
    if out is not None:
        need = ["REGRESSION (65th character)", "REGRESSION (K.K.)", "the OLD 64-record file is read", "a flipped byte in a record: BAD", "100000 distinct characters fit"]
        check("guest-state test ran the 65th-character, K.K., v2 migration and corruption scenarios", all(any(n in ln for ln in out.splitlines()) for n in need))
    run_c(scratch, "guest_state_selftest", gs, [os.path.join(scratch, "gstate_asan")], sanitize=True)

    # ---------------- the earlier phases still hold ----------------
    run_c(scratch, "peer_table_selftest", ["pc_peer_table.c"], [os.path.join(scratch, "pt")])
    run_c(scratch, "guest_store_selftest", ["pc_mp_guest_store.c", "pc_mp_guests.c", "pc_mp_membership.c", "pc_mp_records.c"], [os.path.join(scratch, "gst")])
    if os.name != "nt":
        run_c(scratch, "guest_admission_selftest", ["pc_peer_table.c", "pc_guest_admit.c", "pc_mp_guest_store.c", "pc_mp_guests.c", "pc_mp_membership.c", "pc_mp_records.c"],
              [os.path.join(scratch, "adm")], shim_tu=True)

    # ---------------- source audits ----------------
    ng, rp, rph = read("pc/src/pc_net_game.c"), read("pc/src/pc_remote_player.c"), read("pc/include/pc_remote_player.h")
    code, rcode = strip_comments(ng), strip_comments(rp)
    allsrc = ""
    for d in ("pc/src", "pc/include"):
        for f in os.listdir(os.path.join(ROOT, d)):
            if f.endswith((".c", ".h")):
                allsrc += read(d + "/" + f)
    check("PHASE 6: PC_REMOTE_PLAYER_SLOT_COUNT and the static s_slots[] table are gone from every source", "PC_REMOTE_PLAYER_SLOT_COUNT" not in strip_comments(allsrc) and not re.search(r"\bs_slots\b", rcode))
    check("PHASE 6: slots come from the pool: readers use find (never allocate), only the writers (READY / MOVE / APPEARANCE / SCENE) acquire",
          rcode.count("pc_remote_player_acquire_slot(player_id)") == 4 and "pc_remote_player_get_slot" not in rcode and "static PCPuppetPool s_pool;" in rcode)
    check("PHASE 6: every id guard of pc_net_game.c is the whole usable wire-id range 0..254 (no 'past the puppet table' rejection)", code.count("PC_REMOTE_PLAYER_ID_LIMIT") >= 5 and "ids past the puppet table" not in ng)
    check("PHASE 6: puppet effect item names no longer overlap real names or the PC_TID ids for any wire id", "PC_PUPPET_FX_ITEM_NAME_BASE 0xFEE0u" in rp and "(PC_PUPPET_ID_LIMIT - 1) < 0xFFF1u" in rp and "0xFEC2u" in rp)
    check("PHASE 6: the actor pool is respected: puppets are created only with headroom above a reserve (pc_puppet_actor_headroom) and a failed creation is counted", "pc_puppet_actor_headroom((int)play->actor_info.total_num, mAc_MAX_ACTORS, PC_PUPPET_ACTOR_RESERVE)" in rp and "s_actor_create_failed++" in rp)
    check("PHASE 6: collision is unchanged and interest-managed: the 150-unit arming range and the 50-entry table, a refused setOC is counted", "PC_REMOTE_PLAYER_COLLIDE_RANGE 150.0f" in rp and "s_collide_failed_setoc" in rp and "Cl_COLLIDER_NUM" in rp)
    check("PHASE 6: disconnect / CLEARED release the slot (no stale puppet): on_disconnect frees it, the client's CLEARED handler forgets the id, shutdown frees all",
          "pc_puppet_pool_release(&s_pool, (int)player_id)" in rp and "pc_remote_player_forget((PCNetPlayerId)in->net_player_id)" in ng and "pc_puppet_pool_release_all(&s_pool)" in rp)
    check("PHASE 5: the host's roster is the delta book: no full-roster resend function remains; READY joins, a departure leaves, an appearance / scene change marks, a pump with a window check delivers",
          "pcnetgame_host_send_full_roster" not in code and "pcnetgame_host_send_scene_roster" not in code and "pc_roster_join(&s_roster_app, (int)peer);" in ng and "pc_roster_leave(&s_roster_scn, (int)peer);" in ng and
          "pc_roster_changed(&s_roster_app, (int)peer);" in ng and "pcnetgame_roster_window_ok(dest)" in ng and "static void pcnetgame_roster_pump(void)" in ng)
    check("PHASE 5: the periodic appearance resend is ONE subject per period (pc_roster_refresh_step), not a roster per peer", "pc_roster_refresh_step(&s_roster_app)" in ng)
    check("PHASE 5: the MOVE relay (peer and host) is thinned by interest tiers with a boost after a scene change, behind settings.ini interest_management (default 1)",
          code.count("pc_interest_relay(tier, count, boost)") == 2 and "s_move_boost[peer] = PC_INTEREST_BOOST_SAMPLES;" in ng and ".interest_management = 1," in read("pc/src/pc_settings.c"))
    check("PHASE 5: the transport is untouched (reliable semantics, sequence / SACK / ACK, broadcast contract): pc_net.c / pc_net.h identical to the parent commit",
          read("pc/src/pc_net.c") == parent("pc/src/pc_net.c") and read("pc/include/pc_net.h") == parent("pc/include/pc_net.h"))
    new_msg, old_msg = typedef_blocks(ng, "Msg"), typedef_blocks(parent("pc/src/pc_net_game.c"), "Msg")
    changed = sorted(n for n in old_msg if n.endswith("Msg") and new_msg.get(n) != old_msg[n])
    added = sorted(n for n in new_msg if n.endswith("Msg") and n not in old_msg)
    wild_sim = ["PCNetGameBobberEventMsg", "PCNetGameBobberStateMsg", "PCNetGameWildlifeStateMsg"]  # the host-authoritative wildlife simulation (ids 73-75) came after this milestone
    check("WIRE: no existing PCNet*Msg struct changed (%d compared); the only additions are the wildlife simulation messages %s" % (len([n for n in old_msg if n.endswith("Msg")]), added),
          len(old_msg) > 20 and not changed and added == wild_sim)
    enum_new = re.search(r"typedef enum PCNetGameMsgType \{.*?\} PCNetGameMsgType;", ng, re.S)
    enum_old = re.search(r"typedef enum PCNetGameMsgType \{.*?\} PCNetGameMsgType;", parent("pc/src/pc_net_game.c"), re.S)
    ids_old = dict(re.findall(r"(PC_NETGAME_MSG_[A-Z_0-9]+)\s*=\s*(\d+)", enum_old.group(0)))
    ids_new = dict(re.findall(r"(PC_NETGAME_MSG_[A-Z_0-9]+)\s*=\s*(\d+)", enum_new.group(0)))
    check("WIRE: every message id of the parent commit is unchanged; the only new ones are WILDLIFE_STATE 73, BOBBER_STATE 74, BOBBER_EVENT 75 (no protocol bump)",
          enum_new is not None and all(ids_new.get(k) == v for k, v in ids_old.items()) and {k: v for k, v in ids_new.items() if k not in ids_old} == {"PC_NETGAME_MSG_WILDLIFE_STATE": "73", "PC_NETGAME_MSG_BOBBER_STATE": "74", "PC_NETGAME_MSG_BOBBER_EVENT": "75"})
    check("WIRE: uint8_t wire ids kept; the host id is still 8 and 0xFF stays 'nobody'", "#define PC_NETGAME_HOST_WIRE_ID 8" in read("pc/include/pc_net_game.h") and "PC_PEER_ID_LAST" in read("pc/include/pc_net_game.h"))
    check("PHASE 7: K.K.'s claim is per guest identity: claimant -2 skips the shared foreigner bit and the host's day-claim table records it", "aNTT_pc_host_song_check(is_guest ? -2" in ng and "pc_dayclaims_mark(&s_kk_claims" in ng and
          "claimant == -2" in read("src/actor/npc/ac_npc_totakeke_talk.c_inc") and "pcnetgame_evnpc_plan(idx, t, post," in ng)
    check("PHASE 7: the work table is dynamic (PCKeyTab keyed by PersonalID, file v3 with a v2 reader); no fixed 64; a rekey removes and re-adds", "PC_WORK_MAX_CHARS" not in code and "PC_WORK_FILE_VERSION 3u" in ng and
          "pc_tabfile_load(path, PC_WORK_FILE_MAGIC, PC_WORK_FILE_VERSION, PC_WORK_FILE_VERSION_V2, 64" in ng and "pc_keytab_remove(&s_work_tab, &from_key)" in ng)
    check("RESIDENTS stay 4: PLAYER_NUM tables and the resident table size are untouched, guests never touch them", "#define PC_NETGAME_RESIDENT_TABLE_SIZE   4" in ng and "static PCNetGameMboxHost s_mbox_host[PLAYER_NUM][PC_NETGAME_MBOX_SLOTS];" in ng and "static uint32_t s_remail_day[PLAYER_NUM];" in ng)
    for rel, why in (("pc/src/pc_mp_guest_store.c", "Phase 3 guest store"), ("pc/include/pc_mp_guest_store.h", "Phase 3 guest store header"), ("pc/src/pc_guest_admit.c", "Phase 4 admission rules"), ("pc/include/pc_guest_admit.h", "Phase 4 admission header"),
                     ("pc/src/pc_peer_table.c", "Phase 2 peer table")):
        check("%s unchanged by this work (%s)" % (rel, why), read(rel) == parent(rel))
    changed_files = subprocess.run(["git", "-C", ROOT, "diff", "--name-only", PARENT], capture_output=True, text=True).stdout.split()
    check("Rover / radial menu / character files not touched", not [f for f in changed_files if re.search(r"rover|radial|character|nook_house|guide2", f, re.I)])
    ded = read("pc/src/pc_dedicated.c")
    check("status prints interest, roster, reliable backlog, puppet slots / actors and collision numbers", all(s in ded for s in ("interest (%s): MOVE relayed near/mid/far/apart", "roster: owed=%d", "puppets: slots %d", "collision: puppet colliders armed %d")))

    bad = [n for n, ok in results if not ok]
    print("\nRESULT passed=%d failed=%d" % (len(results) - len(bad), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
