#!/usr/bin/env python3
"""test_day_roll_src.py - SOURCE pins + the native selftest of the host day rollover (docs/multiplayer-guest-roadmap.md "Day rollover on a long-running host", TODO WS-01).
Static checks (labelled: they assert C text) of the wiring and of the safety properties the design relies on, then builds and runs tools/net_spike/day_roll_selftest.c.
Usage: python test_day_roll_src.py"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, HERE)
import wire_baseline  # noqa: E402


def rd(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8", errors="replace") as f:
        return f.read()


def main():
    results = []

    def check(desc, cond):
        ok = bool(cond)
        results.append(ok)
        print(("PASS" if ok else "FAIL") + " - [static] " + desc)

    pure = rd("pc", "src", "pc_day_roll.c")
    hdr = rd("pc", "include", "pc_day_roll.h")
    host = rd("pc", "src", "pc_day_roll_host.c")
    play = rd("src", "game", "m_play.c")
    sdi = rd("src", "game", "m_start_data_init.c")
    sdih = rd("include", "m_start_data_init.h")
    vi = rd("pc", "src", "pc_vi.c")
    ded = rd("pc", "src", "pc_dedicated.c")
    dedh = rd("pc", "include", "pc_dedicated.h")
    gc = rd("pc", "src", "pc_net_game.c")
    cm = rd("pc", "CMakeLists.txt")

    # ---- P: the pure module
    incs = re.findall(r'#include\s+[<"]([^>"]+)[>"]', pure)
    check("P pc_day_roll.c is pure (only its own header + libc): %s" % incs, set(incs) <= {"pc_day_roll.h", "stdlib.h", "string.h"})
    check("P DAY rule = vanilla mTM_renewal_renew_time (y/m/d differ; an unread clock never renews)",
          re.search(r"now->year == 0\)\s*\{\s*return 0;", pure) and "renew->year != now->year || renew->month != now->month || renew->day != now->day" in pure)
    check("P GROW rule = vanilla mAGrw_RenewalFgItem (clear code, backwards clock, next 6 AM)",
          "if (last_is_clear)" in pure and "pc_dayroll_seconds(now) < pc_dayroll_seconds(last)" in pure and "out->hour = 6;" in pure and "if (last->hour >= 6)" in pure)
    check("P a shop / villager house is a HARD blocker that never times out; the soft wait is bounded",
          re.search(r"case 2:.*?case 7:.*?return PC_DAYROLL_BLOCK_HARD", pure, re.S) and
          re.search(r"if \(in->hard_blockers > 0\) \{\s*return PC_DAYROLL_WAIT_PEER_HARD;", pure) and
          "in->soft_blockers > 0 && !in->ignore_soft && in->deferred_ms < in->soft_max_ms" in pure)
    check("P limits: soft 30 min, retry 10 min, 3 tries", "(30u * 60u * 1000u)" in hdr and "(10u * 60u * 1000u)" in hdr and "#define PC_DAYROLL_MAX_TRIES     3" in hdr)

    # ---- H: the host integration
    check("H HOST role only (single player / clients never roll)", re.search(r"void pc_day_roll_poll\(void\) \{.*?if \(pc_net_game_role\(\) != PC_NETGAME_ROLE_HOST\) \{\s*return;", host, re.S))
    check("H ON by default, kill switch AC_MP_DAY_ROLL=0", 'getenv("AC_MP_DAY_ROLL")' in host and "s_dr.enabled = !(e != NULL && e[0] == '0' && e[1] == '\\0');" in host)
    check("H DAY detection compares the game date with the date of the last new-day init (boot, then each roll), NOT Save renew_time (mTM_time renews that every frame)",
          "renew_time)" not in host.replace("all_grow_renew_time)", "") and "pc_dr_note_init_date();" in host and
          re.search(r"if \(!s_dr\.init_date_valid\) \{\s*pc_dr_note_init_date\(\);", host) and
          re.search(r"s_dr\.init_ran = 1;\s*pc_dr_note_init_date\(\);", host))
    check("H GROW detection reads the vanilla growth bookkeeping (Save all_grow_renew_time, the clear code, Common time.rtc_time)",
          "Save_GetPointer(all_grow_renew_time)" in host and "Common_GetPointer(time.rtc_time)" in host and "mTM_rtcTime_clear_code" in host)
    check("H a new-day init skipped inside the reload (a player stepped into a shop during the fade) is not counted as a try and is retried after 1 min",
          re.search(r"if \(s_dr\.init_skipped && \(s_dr\.armed & PC_DAYROLL_KIND_DAY\)\) \{.*?s_dr\.att_day\.tries--;.*?PC_DAYROLL_RETRY_GAP_MS \+ 60000u;", host, re.S))
    m_time = rd("src", "game", "m_time.c")
    check("H evidence pin: vanilla mTM_time() itself calls mTM_renewal_renew_time() every frame (why Save renew_time cannot be the DAY key)",
          re.search(r"extern void mTM_time\(\) \{.*?mTM_renewal_renew_time\(\);\s*\}", m_time, re.S))
    check("H the mechanism is the shop-renewal same-position reload", 'pc_town_same_position_reload("day roll")' in host)
    check("H the play_init hook runs only for an armed DAY roll, re-checks the peers and runs mSDI_DayRollInit then mEv_init",
          re.search(r"void pc_day_roll_play_init\(void\* p\) \{.*?if \(!s_dr\.armed \|\| pc_net_game_role\(\) != PC_NETGAME_ROLE_HOST\).*?pc_dr_peer_gate\(&in.*?mSDI_DayRollInit\(\(GAME\*\)play\).*?mEv_init\(&play->event\);", host, re.S))
    check("H the roll writes no player record and sends nothing new on the wire",
          "private_data" not in host and "pc_net_send" not in host and "homes[" not in host)
    check("H test hooks are compiled only in test builds and need AC_TEST_HOOKS=1",
          re.search(r"#ifdef PC_NET_TEST_HOOKS\s*/\* TEST-ONLY: move the HOST game clock", host) and 'pc_test_hook_getenv("AC_TEST_DAY_ROLL_AT")' in host and "pc_test_hooks_enabled()" in host)
    check("H log lines carry the [SERVER] day roll: prefix", 'printf("[SERVER] day roll: %s\\n", buf);' in host)
    check("H a finished roll asks for one save; pc_vi.c writes it with the periodic writer's gates",
          "s_dr.save_due = 1;" in host and re.search(r"pc_day_roll_save_due\(\) && pc_net_game_role\(\) == PC_NETGAME_ROLE_HOST && pc_net_game_dedicated_world_ready\(\) && pcfa_save_ready\(\)\)\s*\{\s*int roll_save_ok = pc_save_write_authoritative\(\);", vi))
    check("H pc_vi.c polls the day roll every frame", "pc_day_roll_poll();" in vi)

    # ---- G: the game seams
    m = re.search(r"#ifdef TARGET_PC\s*\{[^{}]*?extern void pc_day_roll_play_init\(void\* play\);\s*pc_day_roll_play_init\(play\);\s*\}\s*#endif\s*mFM_FieldInit\(play\);", play)
    check("G m_play.c play_init calls the hook right before mFM_FieldInit (after the arena, before any actor)", m and play.find("zelda_InitArena") < m.start())
    body = re.search(r"extern int mSDI_DayRollInit\(GAME\* game\) \{(.*?)\n\}", sdi, re.S)
    b = body.group(1) if body else ""
    seq = ["mEv_UnSetGateway()", "mFM_SetBlockKindLoadCombi(NULL)", "mEv_init_force(&play->event)", "mHsRm_GetHuusuiRoom(NULL, player_no)", "mCkRh_DecideNowGokiFamilyCount(player_no)",
           "mSP_ExchangeLineUp_InGame(NULL)", "mNpc_SetRemoveAnimalNo(", "mMkRm_MarkRoom(NULL)", "mRmTp_SetDefaultLightSwitchData(2)", "mSDI_StartInitAfter(game, FALSE, mSDI_MALLOC_FLAG_ZELDA)"]
    pos = [b.find(s) for s in seq]
    check("G mSDI_DayRollInit = the continue init in boot order, then mSDI_StartInitAfter(FALSE, ZELDA)", body and all(p >= 0 for p in pos) and pos == sorted(pos))
    check("G mSDI_DayRollInit never runs a new-town / new-player init, the decoy punishment or the gateway set",
          body and not re.search(r"mPr_InitPrivateInfo|mEv_SetGateway|bzero|inventory|mSDI_StartInitNew|mFM_RenewalReserve", b))
    check("G mSDI_DayRollInit is TARGET_PC only and declared in the header", re.search(r"#ifdef TARGET_PC[\s\S]*extern int mSDI_DayRollInit\(GAME\* game\);\s*#endif", sdih))

    # ---- C: console + accessors + wire
    check("C dedicated console `day` command + help line", 'strcmp(cmd, "day") == 0' in ded and "pc_day_roll_console(args, pc_ded_out_line);" in ded and "  day [status] | day roll now" in ded)
    check("C the busy accessor lives in pc_dedicated.h (wire header untouched) and is read-only",
          "void pc_net_game_host_day_roll_busy(int* joining, int* pending);" in dedh and
          re.search(r"void pc_net_game_host_day_roll_busy\(int\* joining, int\* pending\) \{(?:(?!pc_net_send|s_host_peer\[i\]\.\w+ =).)*?\n\}", gc, re.S))
    ids = [i for _n, i in wire_baseline.c_message_ids_raw(gc)]
    check("C no new message id (93 stays free): %s" % sorted(set(i for i in ids if i >= 90)), 93 not in ids)
    check("C both files are in the game build", "src/pc_day_roll.c" in cm and "src/pc_day_roll_host.c" in cm)

    # ---- N: native selftest
    st = os.path.join(HERE, "day_roll_selftest.c")
    check("N native selftest present and includes the real module", os.path.isfile(st) and '#include "../../src/pc_day_roll.c"' in open(st).read())
    gcc = None
    for cand in ("gcc", r"C:\msys64\ucrt64\bin\gcc.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True, timeout=30)
            gcc = cand
            break
        except Exception:  # noqa: BLE001
            continue
    if gcc is not None:
        out = os.path.join(os.environ.get("TEMP", HERE), "day_roll_selftest_src_audit.exe")
        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + os.pathsep + r"C:\msys64\ucrt64\bin"
        cp = subprocess.run([gcc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Iinclude", "tools/net_spike/day_roll_selftest.c", "-o", out], cwd=os.path.join(ROOT, "pc"),
                            capture_output=True, text=True, timeout=120, env=env)
        ok = cp.returncode == 0
        if not ok:
            print(cp.stderr[-2000:])
        else:
            r = subprocess.run([out], capture_output=True, text=True, timeout=60, env=env)
            ok = r.returncode == 0
            print("INFO - " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "(no output)"))
        check("N the native selftest builds (-Wall -Wextra -Werror) and passes", ok)
    else:
        print("INFO - gcc not found: native selftest not run here")
    print("\n%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
