#!/usr/bin/env python3
"""test_building_interactions_src.py - SOURCE AUDIT of the puppet building interactions (knock / door / walk-in / exit).
TIER: SOURCE AUDIT only (text of pc_remote_player.c / pc_net_game.c). Nothing here runs the game or proves that a pose LOOKS right;
the row table itself is exercised by test_move_action_state_wire.py (--only s3: all 121 indexes, parsed table, real host process).

Checks: new row flags exist and are used by the right rows (KNOCK_DOOR/DOOR ROOT_PIN, DOOR HIDE_AT_END, OUTDOOR SCENE_ENTRY without pin),
row_start applies the root pin (and bind/release clear it), dw/mv scene gate + snap_req wait, snapshot trim, hidden_door timeout and
clears, the scene-entry start path, the collision hold, the sender facing for KNOCK_DOOR/DOOR, and that no wire constant changed.
Usage: python test_building_interactions_src.py"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "..", "src"))
FAILS = []


def check(name, ok):
    print(("PASS - " if ok else "FAIL - ") + name)
    if not ok:
        FAILS.append(name)


def func(text, sig):
    """Body text of the C function whose definition line contains `sig` (brace matched)."""
    i = text.index(sig)
    while True:  # skip forward declarations (';' before '{')
        nxt = re.search(r"[{;]", text[i:])
        if nxt.group(0) == "{":
            break
        i = text.index(sig, i + 1)
    j = i + nxt.start()
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[i:k + 1]
    raise ValueError(sig)


def main():
    rp = open(os.path.join(SRC, "pc_remote_player.c"), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    ng = open(os.path.join(SRC, "pc_net_game.c"), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")

    for fl, bit in (("ROOT_PIN", 8), ("HIDE_AT_END", 9), ("SCENE_ENTRY", 10)):
        check("flag PC_ROWF_%s = 1 << %d" % (fl, bit), re.search(r"PC_ROWF_%s\s*=\s*1\s*<<\s*%d\b" % (fl, bit), rp) is not None)
    check("row flags field is wide enough (u16 flags)", re.search(r"u16\s+flags;\s*/\* PC_ROWF_\*", rp) is not None)

    def row(idx):
        m = re.search(r"\[mPlayer_INDEX_%s\]\s*=\s*(PCROW\w*\(.*?\)),\s*\n" % idx, rp)
        return m.group(1) if m else ""

    knock, door, outd = row("KNOCK_DOOR"), row("DOOR"), row("OUTDOOR")
    check("KNOCK_DOOR is a KNOCK1 STOP row with ROOT_PIN", "mPlayer_ANIM_KNOCK1" in knock and "PC_STOP" in knock and "PC_ROWF_ROOT_PIN" in knock)
    check("DOOR is an OPEN1 STOP row with ROOT_PIN | HIDE_AT_END", "mPlayer_ANIM_OPEN1" in door and "PC_STOP" in door and
          "PC_ROWF_ROOT_PIN" in door and "PC_ROWF_HIDE_AT_END" in door)
    check("OUTDOOR is a GO_OUT_O1 STOP row with SCENE_ENTRY and NO root pin (clip root motion moves the body)",
          "mPlayer_ANIM_GO_OUT_O1" in outd and "PC_STOP" in outd and "PC_ROWF_SCENE_ENTRY" in outd and "ROOT_PIN" not in outd)
    check("the wrong 'AnimationMove double-apply' PCFB texts for these rows are gone",
          "would double-apply root translation" not in rp.split("[mPlayer_INDEX_DOOR]")[0].split("[mPlayer_INDEX_INVADE]")[0][-400:] and
          'PCFB("scene transition' not in rp)

    start = func(rp, "static int pc_remote_player_row_start(")
    check("row_start pins the root for ROOT_PIN rows (Set_base_shape_trs + TRANS_XZ|ROT_Y) and clears it otherwise",
          "PC_ROWF_ROOT_PIN" in start and "cKF_SkeletonInfo_R_Animation_Set_base_shape_trs(&v->keyframe0, 0.0f, 1000.0f, 0.0f, 0, 0, 0x4000)" in start and
          "cKF_ANIMATION_TRANS_XZ | cKF_ANIMATION_ROT_Y" in start and "animation_enabled = 0" in start)
    check("row_release and bind_body clear the root pin",
          "animation_enabled = 0" in func(rp, "static void pc_remote_player_row_release(") and
          "animation_enabled = 0" in func(rp, "static void pc_remote_player_bind_body("))
    check("the puppet never runs AnimationMove_base (position is synced, never integrated)", "AnimationMove_base" not in
          re.sub(r'PCFB\(".*"\)', "", re.sub(r"/\*.*?\*/", "", rp, flags=re.S)))

    post = func(rp, "static void pc_remote_player_row_post(")
    check("row_post sets hidden_door when a HIDE_AT_END row finishes", "PC_ROWF_HIDE_AT_END" in post and "hidden_door = 1" in post)
    pre = func(rp, "static void pc_remote_player_row_pre(")
    check("row_pre: hidden_door 600 frame timeout, cleared by the scene clear; NOT by the 90 frame gap",
          "PC_REMOTE_PLAYER_HIDDEN_DOOR_TIMEOUT_FRAMES" in pre and "v->hidden_door = 0" in pre and
          re.search(r"ACTION_GAP_FRAMES\)\s*\{[^}]*hidden_door", pre) is None)
    check("row_pre: waits (does nothing) while snap_req is pending", re.search(r"if \(slot->snap_req\) \{\s*return;", pre) is not None)
    check("row_pre: an active SCENE_ENTRY row is not released by the scene clear; OUTDOOR starts from the clear and from first sight",
          "PC_ROWF_SCENE_ENTRY" in pre and pre.count("PC_ROWF_SCENE_ENTRY") >= 3 and "mPlayer_INDEX_OUTDOOR" in pre)
    check("hidden_door timeout constant is 600 frames", re.search(r"HIDDEN_DOOR_TIMEOUT_FRAMES\s+600\.0", rp) is not None)

    dw = func(rp, "static void pc_remote_player_dw(")
    check("dw: hides on hidden body, pending snap_req and outside the local field scene",
          "pc_remote_player_body_hidden(self)" in dw and "slot->snap_req" in dw and "pc_remote_player_scene_is_local_field(slot" in dw)
    fxg = rp.count("pc_remote_player_body_hidden(self)")
    check("fx gates (2) + dw + collision use the shared hidden test", fxg >= 4)
    check("collision holds for KNOCK_DOOR / DOOR / OUTDOOR rows, hidden bodies and a pending scene event",
          all(s in func(rp, "static const char* pc_remote_player_collide_eval(") for s in
              ("mPlayer_INDEX_KNOCK_DOOR", "mPlayer_INDEX_DOOR", "mPlayer_INDEX_OUTDOOR", "slot->snap_req")))

    on_scene = func(rp, "int pc_remote_player_on_scene(")
    check("on_scene records scene_accept_frame and arms snap_req", "slot->snap_req = 1" in on_scene and "scene_accept_frame" in on_scene)
    res = func(rp, "static int pc_remote_player_scene_snap_resolve(")
    check("snap resolve keeps only samples received at/after the scene event, with timeout and clock-restart escapes",
          "recv_local_frame >= slot->scene_accept_frame" in res and "SCENE_FRESH_TIMEOUT_FRAMES" in res and
          "now < slot->scene_accept_frame" in res)
    mv = func(rp, "static void pc_remote_player_mv(")
    check("mv: resolves the pending scene event, does not interpolate while waiting, and the placement frame never sets pos_snapped",
          "pc_remote_player_scene_snap_resolve" in mv and "!slot->snap_req && pc_remote_player_interpolate" in mv and
          re.search(r"if \(just_placed\) \{[^}]*pos_snapped = 0;", mv, re.S) is not None)
    check("snap_req is reset when the slot/actor is recreated", rp.count("snap_req = 0") >= 4)

    check("sender sends shape_info.rotation.y for TURN_DASH, KNOCK_DOOR and DOOR (value change only)",
          re.search(r"msg->facing_angle\s*=\s*\(local->now_main_index == mPlayer_INDEX_TURN_DASH\s*\|\|\s*local->now_main_index == mPlayer_INDEX_KNOCK_DOOR\s*\|\|\s*"
                    r"local->now_main_index == mPlayer_INDEX_DOOR\)\s*\?\s*actor->shape_info\.rotation\.y\s*:\s*actor->world\.angle\.y;", ng) is not None)

    print("-" * 60)
    print("%s (%d failed)" % ("ALL PASSED" if not FAILS else "FAILED", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
