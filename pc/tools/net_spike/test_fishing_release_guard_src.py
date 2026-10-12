#!/usr/bin/env python3
r"""test_fishing_release_guard_src.py - fishing softlock guards (SOURCE tier, no game process).

Checks: (F1) PCWLD_BEV_RELEASE ignores a committed bobber (gyo_status >= 4) before touching gyo_command, TARGET_PC only; (F2) wildlife despawn Case 2 uses gyo_status < 4 (< 5 gone),
the Case 1/3 + untouched branches remain; (F3) FLY_ROD has a bounded multiplayer-only watchdog that leaves via the vanilla collect_rod exit (SettleRequestMainIndexPriority +
request_main_wait_all) without granting anything, single-player (ROLE_NONE) untouched, vanilla request_proc branch intact; (F4) vanilla aUKI_bite wait/fallback is untouched.
Usage: python test_fishing_release_guard_src.py"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def rd(*p):
    return open(os.path.join(REPO, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    gt = rd("src", "actor", "ac_gyo_test.c")
    gy = rd("src", "actor", "ac_gyoei.c")
    fly = rd("src", "game", "m_player_main_fly_rod.c_inc")
    uki = rd("src", "actor", "ac_uki_move.c_inc")
    col = rd("src", "game", "m_player_main_collect_rod.c_inc")

    m = re.search(r"case PCWLD_BEV_RELEASE:(.*?)return 0;\n    \}", gt, re.S)
    body = m.group(1) if m else ""
    ck("F1a RELEASE returns 0 when gyo_status >= 4", re.search(r"if \(uki->gyo_status >= 4\) \{\s*return 0;\s*\}", body))
    ck("F1b the guard comes BEFORE gyo_command = 0", m and body.find("gyo_status >= 4") < body.find("gyo_command = 0") and "gyo_command = 0" in body)
    ck("F1c the whole function is TARGET_PC code (apply_bobber_event inside #ifdef)", "#ifdef TARGET_PC" in gt[: gt.find("aGTT_pc_apply_bobber_event")])
    ck("F1d vanilla RELEASE escape actions kept", all(x in body for x in ("aGTT_ACTION_TOUCH", "aGTT_ACTION_BITE", "aGTT_ACTION_NEAR", "aGTT_ACTION_ESCAPE")))

    ck("F2a despawn Case 2 uses gyo_status < 4", "engaged_by_this_process && uki->gyo_status < 4)" in gy)
    ck("F2b the old < 5 test is gone", "uki->gyo_status < 5" not in gy)
    ck("F2c Case 1 and Case 3 still defer-destroy via 0x20", gy.count("gyo_flags |= 0x20") >= 3)

    ck("F3a watchdog is TARGET_PC + role != NONE only", re.search(r"#ifdef TARGET_PC\s*/\*[^#]*?else if \(pc_net_game_role\(\) != PC_NETGAME_ROLE_NONE\)", fly, re.S))
    ck("F3b bounded: 20 s of 60fps frames", "PC_FLY_ROD_WATCHDOG_FRAMES (20 * 60)" in fly)
    ck("F3c triggers on NULL uki, gyo_status == 1, or timeout", all(x in fly for x in ("uki == NULL", "uki->gyo_status == 1", "s_fly_rod_wait_frames > (f32)PC_FLY_ROD_WATCHDOG_FRAMES")))
    ck("F3g timeout accumulates fractional dt (no int truncation above 60 fps)", "static f32 s_fly_rod_wait_frames;" in fly and "s_fly_rod_wait_frames += (f32)gamePT->graph->dt_num_60fps_frames;" in fly
       and "(int)gamePT->graph->dt_num_60fps_frames" not in fly)
    pre = gt[gt.find("int aGTT_pc_apply_bobber_event("):]
    ck("F1e the committed-catch RELEASE guard also covers the kaseki dispatch (checked before aGKK_pc_apply_bobber_event)",
       0 <= pre.find("ev == PCWLD_BEV_RELEASE && uki->gyo_status >= 4") < pre.find("return aGKK_pc_apply_bobber_event("))
    gk = rd("src", "actor", "ac_gyo_kaseki.c")
    mk = re.search(r"int aGKK_pc_apply_bobber_event\(.*?case PCWLD_BEV_RELEASE:(.*?)return 0;\n    \}", gk, re.S)
    kbody = mk.group(1) if mk else ""
    ck("F1f the kaseki (coelacanth) RELEASE handler carries its own gyo_status >= 4 guard before gyo_command = 0",
       re.search(r"if \(uki->gyo_status >= 4\) \{\s*return 0;", kbody) and kbody.find("gyo_status >= 4") < kbody.find("gyo_command = 0"))
    ck("F3d exits like collect_rod (Settle + wait_all) and grants nothing", "Player_actor_SettleRequestMainIndexPriority(actor)" in fly and "Player_actor_request_main_wait_all(game, -5.0f, 0.0f, 0, mPlayer_REQUEST_PRIORITY_1)" in fly
       and "Player_actor_SettleRequestMainIndexPriority(actor)" in col and "request_main_wait_all" in col and "mPlib_Set" not in fly and "pickup" not in fly.lower())
    ck("F3e counter reset in setup", re.search(r"Player_actor_setup_main_Fly_rod.*?s_fly_rod_wait_frames = 0\.0f;", fly, re.S))
    ck("F3f vanilla notice_rod request branch intact", "if (ready && status != 0) {\n        Player_actor_request_main_notice_rod(game, actor->shape_info.rotation.y, mPlayer_REQUEST_PRIORITY_28);\n    }" in fly)

    ck("F4 vanilla aUKI_bite fallback to WAIT and set_proc_wait unchanged", "aUKI_set_proc(actorx, game, aUKI_PROC_WAIT, 0);" in uki and "uki->gyo_status = 1;\n    uki->gyo_type = -1;\n    uki->child_actor = NULL;" in uki)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
