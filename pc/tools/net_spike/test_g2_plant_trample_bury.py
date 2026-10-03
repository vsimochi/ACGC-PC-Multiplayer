#!/usr/bin/env python3
"""test_g2_plant_trample_bury.py - M9-D Group 2 source-audit checks (NO game process is launched).

  G2-1  mTG_plant_proc (src/game/m_tag_ovl.c): the no-shovel Plant else-branch is blocked for a network
        client (warning mWR_WARNING_PUT_PLANT, return before mTG_common_throw_put_field / pocket clear).
  G2-2  Player_actor_SetEffectRemoveFlower_Dash (m_player_main_dash.c_inc): on a host-authoritative client
        the trample (fade_entry_proc) is skipped after the RANDOM(4) draw.
  G2-3  (X1b: SUPERSEDED, deliberately converted) the M9-D mechanism "retain the committed bury claim (s_bury_committed) and restore the item
        on a later host reject" is DELETED: the client no longer clears the pocket before the host's TXN_RESULT(APPLIED), so a failed bury can
        never cost the item and there is nothing to restore. The audit now asserts exactly that: the mechanism is gone, the bury result
        handler only validates and begins a TXN_COMMIT (no pocket write), the slot is cleared only in pcnetgame_txn_apply_applied, and the
        host still sends the tile-reconcile reject after a failed commit (REJECTED(WORLD_CHANGED) + BURY_RESULT accepted=0).

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
    g = d.find("pc_net_game_role() == PC_NETGAME_ROLE_CLIENT")
    f = d.find("fade_entry_proc(name, actor_pos)")
    e = d.find("eEC_EFFECT_HANATIRI")
    check("G2-2: RANDOM draw precedes the client gate; gate precedes petal effect and tile write",
          0 <= r < g < e and g < f, results)
    # G7 intentionally switched this gate from pc_net_game_world_is_host_authoritative() to the plain role test
    check("G2-2: gate returns FALSE (dust effect, no trample) and is the role-based CLIENT gate",
          re.search(r"PC_NETGAME_ROLE_CLIENT\) \{\s*return FALSE;", d) is not None, results)
    check("G2-2: the trample gate is NOT the READY-aware host-authoritative predicate any more (G7: role-based only)",
          "world_is_host_authoritative" not in d, results)

    # ---- G2-3
    net = read("pc/src/pc_net_game.c")
    c_noc = re.sub(r"/\*.*?\*/", "", net, flags=re.S)
    check("G2-3 (X1b): the M9-D committed-bury claim mechanism is DELETED: no s_bury_committed / PCNetGameBuryCommitted / keep timeout anywhere",
          "s_bury_committed" not in c_noc and "PCNetGameBuryCommitted" not in c_noc and "BURY_COMMITTED_KEEP" not in c_noc, results)
    h = func_body(net, r"\nstatic void pcnetgame_handle_client_bury_result\(const PCNetGameBuryResultMsg\* in\) \{")
    h_noc = re.sub(r"/\*.*?\*/", "", h, flags=re.S)
    check("G2-3 (X1b): the bury result handler never writes the pocket (no mPr_*, no inventory write) and never queues CONFIRM(COMMIT): it begins a "
          "TXN_COMMIT with the pending claim", "mPr_Set" not in h_noc and "mPr_Give" not in h_noc and not re.search(r"inventory\.\w+(?:\[[^\]]*\])?\s*=[^=]", h_noc)
          and "PC_NETGAME_CONFIRM_COMMIT" not in h_noc and "pcnetgame_txn_begin(kind, in->request_id, (uint8_t)slot, (uint16_t)s_bury_pending.claimed_item, NULL, &s_bury_pending.owner)" in h_noc,
          results)
    check("G2-3 (X1b): the reject branch still reconciles the tile (pcnetgame_client_apply_tile) and restores nothing (nothing was cleared)",
          "pcnetgame_client_apply_tile(" in h_noc and "RESTORED" not in h_noc and "mPr_SetFreePossessionItem" not in h_noc, results)
    ap = func_body(net, r"\nstatic void pcnetgame_txn_apply_applied\(const PCNetGameClientTxn\* T, const PCNetGameTxnResultMsg\* in\) \{")
    check("G2-3 (X1b): the bury/drop slot is cleared ONLY in pcnetgame_txn_apply_applied (host post-image, or the delta clearing the claimed slot)",
          "np->inventory.pockets[s] = (mActor_name_t)EMPTY_NO;" in ap and "in->post_pockets[i]" in ap, results)
    rst = func_body(net, r"\nstatic void pcnetgame_reset_client_session_state\(void\) \{")
    check("G2-3 (X1b): the session reset clears the transaction in flight (s_ctxn) and the pending bury request",
          "memset(&s_ctxn, 0, sizeof(s_ctxn));" in rst and "memset(&s_bury_pending, 0, sizeof(s_bury_pending));" in rst, results)
    req = func_body(net, r"\nint pc_net_game_request_bury\(")
    check("G2-3 (X1b): a new bury request is refused while a pocket transaction is unresolved (return 0 = the seam shows the 'cannot' warning)",
          "pcnetgame_txn_busy()" in req and "s_bury_committed" not in req, results)
    check("G2-3: the host still sends the tile-reconcile reject with the request id after a failed commit (legacy path AND the TXN WORLD_CHANGED step)",
          "pcnetgame_host_send_bury_reject(peer, rec->request_id, rec->ut_x, rec->ut_z);" in net, results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
