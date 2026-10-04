#!/usr/bin/env python3
"""test_guest_arrival_sync_src.py - SOURCE AUDIT of the guest-arrival presentation steps T3 (puppet rows) and T4 (local arrival train on the other
processes). TIER: SOURCE AUDIT only (text of the working tree vs the blobs at the pinned baseline commit). Behaviour is covered by test_guest_arrival.py
(native guard unit), test_guest_arrival_real.py (real host + resident + guest processes) and test_move_action_state_wire.py (puppet row table).

BASELINE is pinned (f660cc7, the HEAD this work started from). After the orchestrator commits, set FIX_COMMIT to that hash to compare
BASELINE..FIX_COMMIT instead of the working tree.
Usage: python test_guest_arrival_sync_src.py"""
import os
import re
import subprocess
import sys

import test_guest_train_src as T

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
BASELINE = "f660cc7"
FIX_COMMIT = None  # (filled by orchestrator after commit) None = the working tree


def blob(commit, path):
    return T.norm(subprocess.run(["git", "-C", ROOT, "show", commit + ":" + path], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace"))


def cur(path):
    if FIX_COMMIT:
        return blob(FIX_COMMIT, path)
    return T.norm(open(os.path.join(ROOT, path), encoding="utf-8", errors="replace").read())


def unchanged(paths):
    args = ["git", "-C", ROOT, "diff", "--quiet", "--ignore-cr-at-eol", BASELINE] + ([FIX_COMMIT] if FIX_COMMIT else []) + ["--"] + list(paths)
    return subprocess.run(args, timeout=60).returncode == 0


def diff_lines(path):
    """(added, removed) lines of path vs the baseline (-U0, CR at EOL ignored)."""
    args = ["git", "-C", ROOT, "diff", "-U0", "--ignore-cr-at-eol", BASELINE] + ([FIX_COMMIT] if FIX_COMMIT else []) + ["--", path]
    out = subprocess.run(args, capture_output=True, text=True, timeout=60).stdout.split("\n")
    add = [l[1:] for l in out if l.startswith("+") and not l.startswith("+++")]
    rem = [l[1:] for l in out if l.startswith("-") and not l.startswith("---")]
    return add, rem


def main():
    results = []

    def ck(d, c):
        results.append((d, bool(c)))
        print(("PASS - " if c else "FAIL - ") + d)

    # ---------------- vanilla decomp untouched ----------------
    tc_path = "src/game/m_train_control.c"
    tc, tcb = cur(tc_path), blob(BASELINE, tc_path)
    stripped, regions, unb = T.strip_target_pc(tc)
    bstripped, bregions, bunb = T.strip_target_pc(tcb)
    ck("diff: m_train_control.c is BYTE-IDENTICAL to the baseline %s outside the TARGET_PC regions" % BASELINE, not unb and not bunb and T.nonblank(stripped) == T.nonblank(bstripped))
    cl = "\n".join(regions).split("\n")
    bl = "\n".join(bregions).split("\n")
    it = iter(cl)
    ck("diff: inside the TARGET_PC regions only lines were ADDED (every baseline region line is still there, in order)", all(any(b == c for c in it) for b in bl))
    for fn in ("mTRC_schedule", "mTRC_trainControl", "mTRC_trainSet", "mTRC_init", "mTRC_go_process", "mTRC_mati_init", "mTRC_demo_init", "mTRC_call_init",
               "mTRC_norm_init", "mTRC_move", "mTRC_pc_diag", "mTRC_pc_title_edge"):
        ck("diff: %s is byte-identical to the baseline" % fn, T.func(tc, fn) != "" and T.func(tc, fn) == T.func(tcb, fn))
    ck("vanilla player / train / ride-off / station master decomp files are untouched (git diff vs %s)" % BASELINE,
       unchanged(["src/game/m_player.c", "src/game/m_player_lib.c", "src/game/m_player_common.c_inc", "src/game/m_player_main_demo_standing_train.c_inc",
                  "src/game/m_player_main_demo_getoff_train.c_inc", "src/game/m_player_main_demo_walk.c_inc", "src/actor/ac_ride_off_demo_move.c_inc",
                  "src/actor/ac_intro_demo_move.c_inc", "src/actor/ac_train0_move.c_inc", "src/actor/ac_train1_move.c_inc",
                  "src/actor/npc/ac_npc_station_master_schedule.c_inc", "src/actor/npc/ac_npc_station_master_talk.c_inc", "src/game/m_event.c"]))
    hb = blob(BASELINE, "include/m_train_control.h")
    hc = cur("include/m_train_control.h")
    hs, hr, hu = T.strip_target_pc(hc)
    ck("diff: include/m_train_control.h differs from the baseline only by a TARGET_PC declaration of mTRC_pc_remote_arrival",
       not hu and T.nonblank(hs) == T.nonblank(hb) and "extern int mTRC_pc_remote_arrival(GAME* game, int peer, int puppet_in_local_town);" in "\n".join(hr))

    # ---------------- T4: the train side ----------------
    f = T.func(tc, "mTRC_pc_remote_arrival")
    ck("T4: mTRC_pc_remote_arrival exists inside a TARGET_PC region", f != "" and any("mTRC_pc_remote_arrival" in r for r in regions) and "mTRC_pc_remote_arrival" not in stripped)
    ck("T4: the decision is the pure pcarr_remote_arrival_train_decide() (header-only, natively tested) over the real game state",
       "pcarr_remote_arrival_train_decide(0, puppet_in_local_town" in f and "#include \"pc_remote_arrival_logic.h\"" in tc)
    ck("T4: inputs: town scene + FG field, local player present, title demo, pre-game draw types, coming_flag, train_action",
       all(x in f for x in ("play->scene_id == SCENE_FG", "mFI_FIELDTYPE2_FG", "get_player_actor_withoutCheck", "mEv_CheckTitleDemo() != mEv_TITLEDEMO_NONE",
                            "FIELD_DRAW_TYPE_TRAIN", "FIELD_DRAW_TYPE_PLAYER_SELECT", "Common_Get(train_coming_flag)", "Common_Get(train_action)")))
    ck("T4: local train demo = all four train main indexes + the ride-off / intro demo actors",
       all(x in f for x in ("mPlayer_INDEX_DEMO_STANDING_TRAIN", "mPlayer_INDEX_DEMO_GETOFF_TRAIN", "mPlayer_INDEX_DEMO_GETON_TRAIN", "mPlayer_INDEX_DEMO_GETON_TRAIN_WAIT",
                            "mAc_PROFILE_RIDE_OFF_DEMO", "mAc_PROFILE_INTRO_DEMO")))
    ck("T4: the ONLY state write is Common_Set(train_coming_flag, 3) (the arriving client's own request), after the refusal return",
       len(re.findall(r"Common_Set\(", f)) == 1 and "Common_Set(train_coming_flag, 3);" in f and f.index("return 0;") < f.index("Common_Set(train_coming_flag, 3);"))
    ck("T4: no direct train state machine call / no scene / net call in the hook (mTRC_schedule, mTRC_demo_init, mTRC_init, goto_other_scene, pc_net)",
       not re.search(r"mTRC_schedule|mTRC_demo_init|mTRC_init|mTRC_call_init|goto_other_scene|pc_net|mDemo_|Actor_info_delete|setup_actor", f))
    dm = re.search(r"static void aROD_first_set\(ACTOR\* actor, GAME\* game\) \{\n    Common_Set\(train_coming_flag, 3\);", T.norm(open(os.path.join(ROOT, "src/actor/ac_ride_off_demo_move.c_inc"), encoding="utf-8", errors="replace").read()))
    ck("T4: the value matches the arriving client's own request (ride-off demo aROD_first_set sets train_coming_flag = 3; mTRC_schedule case 3 -> mTRC_demo_init)", dm is not None
       and re.search(r"case 3: \{\n\s+Common_Set\(train_coming_flag, 0\);\n\s+mTRC_demo_init\(\);", tc) is not None)

    # ---------------- T3 / T4: the puppet side ----------------
    rp = cur("pc/src/pc_remote_player.c")
    add, rem = diff_lines("pc/src/pc_remote_player.c")
    ck("diff: pc_remote_player.c: exactly THREE baseline lines were replaced (the DEMO_WALK, DEMO_GETOFF_TRAIN and DEMO_STANDING_TRAIN PCFB rows), nothing else removed (%d removed)" % len(rem),
       len(rem) == 3 and sum(1 for l in rem if "[mPlayer_INDEX_DEMO_WALK] = PCFB" in l) == 1 and sum(1 for l in rem if "[mPlayer_INDEX_DEMO_GETOFF_TRAIN] = PCFB" in l) == 1
       and sum(1 for l in rem if "[mPlayer_INDEX_DEMO_STANDING_TRAIN] = PCFB" in l) == 1)
    rows = {}
    for m in re.finditer(r"\[mPlayer_INDEX_(\w+)\]\s*=\s*(PCROW_BODY|PCROW_ITEM|PCROW|PCFB)\(\s*\"([^\"]*)\"", rp):
        rows[m.group(1)] = (m.group(2), m.group(3))
    bro = {}
    for m in re.finditer(r"\[mPlayer_INDEX_(\w+)\]\s*=\s*(PCROW_BODY|PCROW_ITEM|PCROW|PCFB)\(\s*\"([^\"]*)\"", blob(BASELINE, "pc/src/pc_remote_player.c")):
        bro[m.group(1)] = (m.group(2), m.group(3))
    changed_rows = sorted(k for k in rows if rows[k] != bro.get(k))
    ck("T3: the state-row table differs from the baseline in exactly DEMO_STANDING_TRAIN, DEMO_WALK and DEMO_GETOFF_TRAIN (%s) and has the same 121 indexes" % changed_rows,
       changed_rows == ["DEMO_GETOFF_TRAIN", "DEMO_STANDING_TRAIN", "DEMO_WALK"] and sorted(rows) == sorted(bro) and len(rows) == 121)
    ck("T3: DEMO_STANDING_TRAIN is a filled row 'standing_train' with PC_ROWF_HIDE_BODY (REPEAT, the HIDE row mechanism)",
       rows.get("DEMO_STANDING_TRAIN") == ("PCROW", "standing_train")
       and re.search(r"\[mPlayer_INDEX_DEMO_STANDING_TRAIN\] = PCROW\(\"standing_train\", mPlayer_ANIM_WAIT1, PC_ROW_A1_SAME, PC_NORMAL, PC_REPEAT, 0\.5f, 1\.0f, PC_ROWF_HIDE_BODY,", rp))
    ck("T3: DEMO_GETOFF_TRAIN stays a fallback (puppet rows never replay AnimationMove root motion: an OUTTRAIN1 row would double-apply the clip's translation) and says so; DEMO_GETON_TRAIN* unchanged",
       rows.get("DEMO_GETOFF_TRAIN", ("", ""))[0] == "PCFB" and "deliberately NOT an OUTTRAIN1 row" in rp
       and rows["DEMO_GETON_TRAIN"] == bro["DEMO_GETON_TRAIN"] and rows["DEMO_GETON_TRAIN_WAIT"] == bro["DEMO_GETON_TRAIN_WAIT"])
    ck("T3: DEMO_WALK stays a PCFB row; the fallback block maps it to WALK1 through pcarr_demo_walk_plays_walk (TARGET_PC), so the walk tempo formula applies",
       rows.get("DEMO_WALK", ("", ""))[0] == "PCFB"
       and re.search(r"#ifdef TARGET_PC\n[^\n]*\n[^\n]*\n[^\n]*\n\s+if \(pcarr_demo_walk_plays_walk\(self->cosmetic_action_valid, \(int\)self->cosmetic_action_index, \(int\)mPlayer_INDEX_DEMO_WALK,\n\s+self->cosmetic_move_state == PC_MOVE_STATE_OTHER\)\) \{\n\s+desired_anim_idx = mPlayer_ANIM_WALK1;\n\s+\}\n#endif", rp))
    ck("T3: the sender classifier is untouched (DEMO_WALK stays OTHER on the wire: pc_net_game.c byte-identical)", unchanged(["pc/src/pc_net_game.c"]))
    # hide / clear paths of the HIDE_BODY flag
    rp_pre = T.func(rp, "pc_remote_player_row_pre")
    ck("T3: a HIDE_BODY row is released on a teleport snap and on a silent peer (gap) in row_pre, and on a scene change (latch_clear_req)",
       "(active->flags & PC_ROWF_HIDE_BODY)" in rp_pre and 'pc_remote_player_row_release(self, snapped ? "snap" : "gap");' in rp_pre
       and "slot->latch_clear_req" in rp_pre and 'pc_remote_player_row_release(self, "scene");' in rp_pre)
    ck("T3: any other state releases the row (fallback / another row start) and the draw skips only while the HIDE_BODY row is ACTIVE",
       'pc_remote_player_row_release(self, "finished");' in rp_pre and rp.count("(hrow->flags & PC_ROWF_HIDE_BODY)") >= 3
       and "pc_remote_player_row_release(" in rp)
    ck("T3: the hide flag is only ever set by table rows (no runtime writer): exactly two rows carry PC_ROWF_HIDE_BODY (HIDE, DEMO_STANDING_TRAIN)",
       len(re.findall(r"PC_ROWF_HIDE_BODY, -1, -1,", rp)) == 2)
    # the T4 hook
    poll = T.func(rp, "pc_remote_player_arrival_train_poll")
    ck("T4: pc_remote_player_arrival_train_poll is TARGET_PC guarded, defined once, called once (from row_pre), also TARGET_PC guarded",
       poll != "" and rp.count("pc_remote_player_arrival_train_poll(") == 2  # 1 definition + 1 call
       and re.search(r"#ifdef TARGET_PC\n    pc_remote_player_arrival_train_poll\(self, slot, valid && idx == \(int\)mPlayer_INDEX_DEMO_STANDING_TRAIN\);\n#endif", rp) is not None
       and re.search(r"#ifdef TARGET_PC\nstatic int pc_remote_player_scene_is_local_field\(const PCRemotePlayerSlot\* slot, const GAME_PLAY\* play\);", rp) is not None)
    ck("T4: the poll goes through the pure latch (once per standing period, scene must be the local town) and calls ONLY mTRC_pc_remote_arrival",
       "pcarr_remote_standing_should_evaluate(&self->visual.train_standing_latched, standing, in_town)" in poll and "pc_remote_player_scene_is_local_field(slot, play)" in poll
       and re.findall(r"\bmTRC_\w+\(", poll) == ["mTRC_pc_remote_arrival("] and rp.count("(void)mTRC_pc_remote_arrival(") == 1)
    ck("T4: pc_remote_player.c never writes train state itself (no Common_Set(train_*), no train_coming_flag)",
       not re.search(r"Common_Set\(train_|train_coming_flag|train_action", rp.replace("mTRC_pc_remote_arrival", "")))
    ck("T4: the per-puppet latch lives in the per-actor visual struct (zero at creation: a re-created puppet is a new arrival)",
       "int                   train_standing_latched;" in rp and "int                   rebind_from_row;" in rp)
    ck("T4: the includes of the train API / pure header are TARGET_PC guarded", re.search(r"#ifdef TARGET_PC\n#include \"m_train_control.h\"[^\n]*\n#include \"pc_remote_arrival_logic.h\"[^\n]*\n#endif", rp) is not None)

    # ---------------- vanilla index set / header ----------------
    mp = cur("include/m_player.h")
    ck("T3: the train-demo main indexes are the contiguous DEMO_WALK, GETON_TRAIN, GETON_TRAIN_WAIT, GETOFF_TRAIN, STANDING_TRAIN of m_player.h",
       re.search(r"mPlayer_INDEX_DEMO_WALK,\s*mPlayer_INDEX_DEMO_GETON_TRAIN,\s*mPlayer_INDEX_DEMO_GETON_TRAIN_WAIT,\s*mPlayer_INDEX_DEMO_GETOFF_TRAIN,\s*mPlayer_INDEX_DEMO_STANDING_TRAIN,", mp) is not None)
    hdr = open(os.path.join(PC, "include", "pc_remote_arrival_logic.h"), encoding="utf-8").read()
    ck("header: pure and libc-free (no #include; three static inline functions)", "#include" not in hdr and hdr.count("static inline") == 3)

    # ---------------- no wire change ----------------
    ck("wire: pc_net_game.h / pc_net.h / pc_net.c / pc_net_game.c are byte-identical to the baseline", unchanged(["pc/include/pc_net_game.h", "pc/include/pc_net.h", "pc/src/pc_net.c", "pc/src/pc_net_game.c"]))
    ngc = cur("pc/include/pc_net_game.h")
    ck("wire: PC_NETGAME_PROTOCOL_VERSION is still 8", "#define PC_NETGAME_PROTOCOL_VERSION 8u" in ngc)
    ck("wire: the wire baseline file (if any) is unchanged", unchanged(["pc/tools/net_spike/wire_baseline"]) if os.path.exists(os.path.join(HERE, "wire_baseline")) else True)
    changed = subprocess.run(["git", "-C", ROOT, "diff", "--name-only", "--ignore-cr-at-eol", BASELINE] + ([FIX_COMMIT] if FIX_COMMIT else []), capture_output=True, text=True, timeout=60).stdout.split()
    ck("wire: no pc_net* / protocol source file is in the change set (%s)" % ",".join(changed), not any(re.search(r"pc_net|protocol", c) and not c.endswith((".py", ".md")) for c in changed))

    bad = [d for d, ok in results if not ok]
    print("-" * 60)
    print("%d/%d checks passed" % (len(results) - len(bad), len(results)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
