#!/usr/bin/env python3
"""test_g2_plant_trample_bury.py - M9-D Group 2 source-audit checks (NO game process is launched).

  G2-1  mTG_plant_proc (src/game/m_tag_ovl.c): the no-shovel Plant else-branch is blocked for a network
        client (warning mWR_WARNING_PUT_PLANT, return before mTG_common_throw_put_field / pocket clear).
  G2-2  Player_actor_SetEffectRemoveFlower_Dash (m_player_main_dash.c_inc): on a host-authoritative client
        the trample (fade_entry_proc) is skipped after the RANDOM(4) draw.
  G2-3  pc_net_game.c: a committed bury is retained (s_bury_committed); a matching later reject restores
        the item (same slot / free slot / LOST log); reset + next request + timeout discard it.

Runtime behavior of these paths is NOT TESTED (no hook reaches the plant menu / dash tile / a bury
COMMIT failure on a real client).
Usage: python3 test_g2_plant_trample_bury.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from net_spike_lib import check, summary_and_exit_code  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read().replace("\r\n", "\n")


def func_body(src, header_re):
    m = re.search(header_re, src)
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
                return src[i:j + 1]
    return ""


def main():
    results = []

    # ---- G2-1
    tag = read("src/game/m_tag_ovl.c")
    body = func_body(tag, r"\nstatic void mTG_plant_proc\(Submenu\* submenu, mSM_MenuInfo_c\* menu_info\) \{")
    check("G2-1: mTG_plant_proc found", bool(body), results)
    ei = body.find("    } else {\n#ifdef TARGET_PC")
    gate = body.find("pc_net_game_role() == PC_NETGAME_ROLE_CLIENT", ei if ei >= 0 else 0)
    throw = body.find("mTG_common_throw_put_field(", ei if ei >= 0 else 0)
    check("G2-1: client gate sits in the no-shovel else-branch before the field write",
          ei >= 0 and gate > ei and throw > gate, results)
    blk = body[gate:throw] if gate >= 0 and throw > gate else ""
    check("G2-1: gate shows mWR_WARNING_PUT_PLANT and returns without touching the pocket",
          "mTG_open_warning_window(submenu, menu_info, mWR_WARNING_PUT_PLANT);" in blk and "return;" in blk
          and "mPr_SetPossessionItem" not in blk, results)
    check("G2-1: shovel (BURY) branch still routed to the host",
          "pc_net_game_request_bury(" in body, results)
    fp = func_body(tag, r"\nstatic void mTG_field_put_proc\(Submenu\* submenu, mSM_MenuInfo_c\* menu_info\) \{")
    check("G2-1: Drop-All client gate and single-drop routing still present",
          "pc_net_game_role() != PC_NETGAME_ROLE_CLIENT" in fp and "pc_net_game_request_drop(" in fp, results)

    # ---- G2-2
    dash = read("src/game/m_player_main_dash.c_inc")
    d = func_body(dash, r"\nstatic int Player_actor_SetEffectRemoveFlower_Dash\(ACTOR\* actor, GAME\* game, s16 angle\) \{")
    r = d.find("RANDOM(4)")
    g = d.find("pc_net_game_world_is_host_authoritative()")
    f = d.find("fade_entry_proc(name, actor_pos)")
    e = d.find("eEC_EFFECT_HANATIRI")
    check("G2-2: RANDOM draw precedes the client gate; gate precedes petal effect and tile write",
          0 <= r < g < e and g < f, results)
    check("G2-2: gate returns FALSE (dust effect, no trample)",
          re.search(r"world_is_host_authoritative\(\)\) \{\s*return FALSE;", d) is not None, results)

    # ---- G2-3
    net = read("pc/src/pc_net_game.c")
    check("G2-3: s_bury_committed record defined", "static PCNetGameBuryCommitted s_bury_committed;" in net, results)
    h = func_body(net, r"\nstatic void pcnetgame_handle_client_bury_result\(const PCNetGameBuryResultMsg\* in\) \{")
    ri = h.find("s_bury_committed.valid && s_bury_committed.request_id == in->request_id")
    nm = h.find("if (!matches) {")
    check("G2-3: reject path restores for a matching retained request (in the !matches branch)",
          0 <= nm < ri, results)
    rb = h[ri:h.find("return; /* not our current pending request")] if ri >= 0 else ""
    check("G2-3: restore: same-slot-if-empty, else free slot, else LOST; one restore only",
          "== (mActor_name_t)EMPTY_NO" in rb and "mPr_SetFreePossessionItem(" in rb and "LOST" in rb
          and "s_bury_committed.valid = 0;" in rb and "pcnetgame_owner_stamp_matches(&s_bury_committed.owner)" in rb,
          results)
    clr = h.find("mPr_SetPossessionItem(Now_Private, slot, (mActor_name_t)EMPTY_NO")
    setr = h.find("s_bury_committed.valid = 1;")
    cmt = h.find("PC_NETGAME_CONFIRM_COMMIT")
    check("G2-3: retained after COMMIT is queued and pocket cleared (record set after the clear)",
          clr >= 0 and setr >= 0 and clr < setr and "s_bury_committed.request_id = in->request_id;" in h, results)
    check("G2-3: source order COMMIT queued < pocket cleared < record set",
          cmt >= 0 and clr >= 0 and setr >= 0 and cmt < clr < setr, results)
    check("G2-3: accepted (provisional) path does not restore anything", "RESTORED" not in h[h.find("if (!matches) {", nm + 10):], results)
    rst = func_body(net, r"\nstatic void pcnetgame_reset_client_session_state\(void\) \{")
    check("G2-3: session reset clears the retained record",
          "memset(&s_bury_committed, 0, sizeof(s_bury_committed));" in rst, results)
    req = func_body(net, r"\nint pc_net_game_request_bury\(")
    check("G2-3: new bury request supersedes the retained record",
          "s_bury_committed.valid = 0;" in req, results)
    check("G2-3: safety timeout discards the retained record",
          "graph_dt_period_elapsed(gamePT, &s_bury_committed.keep_accum" in net, results)
    check("G2-3: host still sends a reject on commit failure with the request id",
          "pcnetgame_host_send_bury_reject(peer, rec->request_id, rec->ut_x, rec->ut_z);" in net, results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
