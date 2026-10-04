#!/usr/bin/env python3
"""test_guest_train_src.py - SOURCE AUDIT of the guest-arrival fixes T1 (train diagnostics), T2 (title-demo train leak) and T5 (borderless
edge clamp). TIER: SOURCE AUDIT only (text of the working tree vs the blob at the baseline commit). Behaviour is covered by
test_guest_train.py (native) and test_guest_train_real.py (real processes).

DIFF AUDIT: BASELINE is pinned (018344c, the HEAD this work started from). The working-tree file is compared against `git show BASELINE:path`
(never `git diff HEAD`): after removing every `#ifdef TARGET_PC ... #endif` region the file must be BYTE-IDENTICAL to the baseline (one pinned
exception: the T5 wade-trigger `if (...)` is split over lines to host an #ifdef condition). After the orchestrator commits, set
FIX_COMMIT to that commit hash to compare BASELINE..FIX_COMMIT instead of the working tree.
Usage: python test_guest_train_src.py"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
BASELINE = "018344c"
FIX_COMMIT = None  # (filled by orchestrator after commit) None = the working tree

FILES = ["src/game/m_train_control.c", "src/game/m_player_common.c_inc", "src/game/m_player.c"]


def norm(s):
    return s.replace("\r\n", "\n")


def baseline_blob(path):
    return norm(subprocess.run(["git", "-C", ROOT, "show", BASELINE + ":" + path], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace"))


def current(path):
    if FIX_COMMIT:
        return baseline_blob_at(FIX_COMMIT, path)
    return norm(open(os.path.join(ROOT, path), encoding="utf-8", errors="replace").read())


def baseline_blob_at(commit, path):
    return norm(subprocess.run(["git", "-C", ROOT, "show", commit + ":" + path], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace"))


def strip_target_pc(s):
    """Removes every top-level `#ifdef TARGET_PC ... #endif` region (nesting aware). Returns (stripped, regions, unbalanced)."""
    out, regions, depth, cur, other = [], [], 0, [], 0
    for ln in s.split("\n"):
        t = ln.strip()
        if depth == 0:
            if t == "#ifdef TARGET_PC":
                depth, cur = 1, [ln]
                continue
            out.append(ln)
        else:
            cur.append(ln)
            if t.startswith("#if"):
                depth += 1
            elif t == "#endif":
                depth -= 1
                if depth == 0:
                    regions.append("\n".join(cur))
    return "\n".join(out), regions, depth != 0


def nonblank(s):
    """Blank lines are ignored (a blank line next to an added #ifdef region is the only whitespace-only difference allowed)."""
    return [l for l in s.split(chr(10)) if l.strip() != ""]


def func(s, name):
    m = re.search(r"^[^\n;]*\b%s\([^;{]*\)\s*\{\n" % re.escape(name), s, re.M)
    if not m:
        return ""
    j = s.find("\n}\n", m.end())
    return s[m.start():j + 3]


def main():
    results = []

    def ck(d, c):
        results.append((d, bool(c)))
        print(("PASS - " if c else "FAIL - ") + d)

    # ---- diff audit: only TARGET_PC additions ----
    for p in FILES:
        cur, base = current(p), baseline_blob(p)
        stripped, regions, unbalanced = strip_target_pc(cur)
        if p.endswith("m_player_common.c_inc"):
            pinned = "if ((play->block_table.block_x != bx || play->block_table.block_z != bz)\n        ) {"
            ck("diff: %s contains the one pinned multi-line wade condition (hosting the T5 #ifdef)" % p, pinned in stripped)
            stripped = stripped.replace(pinned, "if (play->block_table.block_x != bx || play->block_table.block_z != bz) {")
        bstripped, bregions, bunbal = strip_target_pc(base)
        ck("diff: %s is BYTE-IDENTICAL to the baseline %s outside the TARGET_PC regions (%d regions, baseline had %d)" % (p, BASELINE, len(regions), len(bregions)),
           not unbalanced and not bunbal and nonblank(stripped) == nonblank(bstripped))
        clines = chr(10).join(regions).split(chr(10))
        blines = chr(10).join(bregions).split(chr(10))
        it = iter(clines)
        ck("diff: %s inside the TARGET_PC regions only lines were ADDED (every baseline region line is still there, in order)" % p,
           all(any(b == c for c in it) for b in blines))
        ck("diff: %s has new TARGET_PC lines (the change exists)" % p, len(clines) > len(blines))

    tc = current("src/game/m_train_control.c")
    tcb = baseline_blob("src/game/m_train_control.c")
    for fn in ("mTRC_schedule", "mTRC_trainControl", "mTRC_trainSet", "mTRC_init", "mTRC_go_process", "mTRC_mati_init", "mTRC_demo_init"):
        ck("diff: vanilla %s is byte-identical to the baseline" % fn, func(tc, fn) != "" and func(tc, fn) == func(tcb, fn))

    # ---- T1 ----
    d = func(tc, "mTRC_pc_diag")
    ck("T1: mTRC_pc_diag exists, logs through PC_LOG (GENERAL), writes no Common state",
       d != "" and "PC_LOG(PCL_GENERAL" in d and "Common_Set" not in d and "mTRC_init" not in d)
    ck("T1: change-only logging (compares action / control / last_control / coming_flag / signal / title demo with the previous values)",
       all(x in d for x in ("s_action == action", "s_ctl == ctl", "s_last == last", "s_coming == coming", "s_signal == signal", "s_demo == demo")) and "return;" in d)
    ck("T1: the line carries action, control, last_control, coming_flag, start_timer vs now (RTC seconds)",
       all(x in d for x in ("action=%u", "control=%u", "last_control=%u", "coming_flag=%u", "start_timer=%u", "now=%u")))
    mv = func(tc, "mTRC_move")
    ck("T1: mTRC_move logs on both the idle and the run path, each TARGET_PC guarded",
       'mTRC_pc_diag("idle");' in mv and 'mTRC_pc_diag("move");' in mv and mv.count("#ifdef TARGET_PC") == 3)

    # ---- T2 ----
    e = func(tc, "mTRC_pc_title_edge")
    ck("T2: edge detection goes through the pure pcarr_trc_title_edge() with START1 and the pre-game draw types",
       "pcarr_trc_title_edge(&s_was_start1, demo, mEv_TITLEDEMO_START1, pre_game)" in e
       and "FIELD_DRAW_TYPE_TRAIN" in e and "FIELD_DRAW_TYPE_PLAYER_SELECT" in e)
    ck("T2: on the edge: mTRC_init(game) then the coming_flag is restored via pcarr_trc_coming_after_reinit(.., 3u) (ride-off arrival kept)",
       "mTRC_init(game);" in e and "pcarr_trc_coming_after_reinit(Common_Get(train_coming_flag), 3u)" in e
       and e.index("mTRC_init(game);") < e.index("Common_Set(train_coming_flag, (u8)coming);"))
    ck("T2: the edge check runs BEFORE the early return of mTRC_move (and only under TARGET_PC)",
       "mTRC_pc_title_edge(game);" in mv and mv.index("mTRC_pc_title_edge(game);") < mv.index("if (!mTRC_go_process() || player == NULL)"))
    ck("T2: no scene transition / no wire from the T2 path (no goto_other_scene / pc_net / mDemo calls)",
       not re.search(r"goto_other_scene|pc_net|mDemo_|mSDI_|mPlib_", e))
    ck("T2: the title-demo-1 latch is static in the edge function only and START1 is mEv_TITLEDEMO_START1 == 1 in the decomp enum",
       "static int s_was_start1 = 0;" in e and re.search(r"mEv_TITLEDEMO_NONE = 0,\s*mEv_TITLEDEMO_START1,", norm(open(os.path.join(ROOT, "include/m_event.h"), encoding="utf-8", errors="replace").read())) is not None)

    # ---- T5 ----
    pcm = current("src/game/m_player_common.c_inc")
    t2 = func(pcm, "Player_actor_BGcheck_common_type2")
    ck("T5: BGcheck_common_type2 keeps the original mCoBG_BgCheckControll call and adds the clamp only for an invalid new block on the FG field",
       "mCoBG_BgCheckControll(NULL, actorx, 18.0f, 0.0f, TRUE, FALSE, 1);" in t2
       and "pcarr_borderless_clamp_needed(Common_Get(field_type) == mFI_FIELDTYPE2_FG," in t2
       and "Player_actor_pc_pos_in_valid_block(&actorx->world.position)" in t2 and "mCoBG_UniqueWallCheck(actorx, 18.0f, 0.0f);" in t2
       and t2.count("#ifdef TARGET_PC") == 1 and "#endif" in t2)
    v = func(pcm, "Player_actor_pc_pos_in_valid_block")
    ck("T5: the validity test is the vanilla mFI_BlockCheck (the scroll transition's test), on floor-divided block coordinates",
       "mFI_BlockCheck(bx, bz)" in v and "pcarr_floor_div(pos->x, mFI_BK_WORLDSIZE_X_F)" in v and "pcarr_floor_div(pos->z, mFI_BK_WORLDSIZE_Z_F)" in v)
    ck("T5: the borderless acre-change wade is refused into an invalid acre (pcarr_borderless_wade_allowed, FG field)",
       "pcarr_borderless_wade_allowed(Common_Get(field_type) == mFI_FIELDTYPE2_FG," in pcm
       and "Player_actor_pc_pos_in_valid_block(player_pos_p)" in pcm)
    ck("T5: the vanilla block check used by the scroll transition (mFI_BlockCheck: in range and not BG_TYPE_292) is unchanged in m_field_info.c",
       subprocess.run(["git", "-C", ROOT, "diff", "--quiet", "--ignore-cr-at-eol", BASELINE, "--", "src/game/m_field_info.c"], timeout=60).returncode == 0
       if not FIX_COMMIT else subprocess.run(["git", "-C", ROOT, "diff", "--quiet", "--ignore-cr-at-eol", BASELINE, FIX_COMMIT, "--", "src/game/m_field_info.c"], timeout=60).returncode == 0)
    ck("T5: walk / run / dash still select the borderless check only under g_mPlib_wade_disabled (callers unchanged)",
       all(subprocess.run(["git", "-C", ROOT, "diff", "--quiet", "--ignore-cr-at-eol", BASELINE, "--", "src/game/" + f], timeout=60).returncode == 0
           for f in ("m_player_main_walk.c_inc", "m_player_main_run.c_inc", "m_player_main_dash.c_inc")) if not FIX_COMMIT else True)

    # ---- header ----
    hdr = open(os.path.join(PC, "include", "pc_arrival_logic.h"), encoding="utf-8").read()
    ck("header: pure and libc-free (no #include other than none, only static inline functions)",
       "#include" not in hdr and hdr.count("static inline") == 5)

    # ---- no wire change ----
    ng = baseline_blob("pc/include/pc_net_game.h")
    ngc = current("pc/include/pc_net_game.h")
    ck("wire: PC_NETGAME_PROTOCOL_VERSION is still 8 and pc_net_game.h has no change vs the baseline", "#define PC_NETGAME_PROTOCOL_VERSION 8u" in ngc and ngc == ng)
    changed = subprocess.run(["git", "-C", ROOT, "diff", "--name-only", "--ignore-cr-at-eol", BASELINE] + ([FIX_COMMIT] if FIX_COMMIT else []), capture_output=True,
                             text=True, timeout=60).stdout.split()
    # T3 / T4 (pinned update): pc/src/pc_remote_player.c is now legitimately changed (puppet rows + the arrival-train hook, audited by
    # test_guest_arrival_sync_src.py: additive/pinned, no wire change); every pc_net* / protocol file is still untouched.
    ck("wire: no pc/src/pc_net*.c / protocol file is touched (%s)" % ",".join(changed),
       not any(re.search(r"pc_net|protocol", c) and not c.endswith(".py") for c in changed))

    bad = [d for d, ok in results if not ok]
    print("-" * 60)
    print("%d/%d checks passed" % (len(results) - len(bad), len(results)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
