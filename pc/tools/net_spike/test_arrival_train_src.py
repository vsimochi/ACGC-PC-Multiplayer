#!/usr/bin/env python3
"""test_arrival_train_src.py - SOURCE PINS of the synced arrival train (message 105, pc/include/pc_arrival_train.h, pc/src/pc_net_game_arrtrain.c_inc).

TIER: SOURCE AUDIT (no build, no process). What it pins:
  W  the wire: id 105 ARRIVAL_TRAIN appended (static assert against PCAT_MSG_ID), the 20 / 48-byte layouts, the shape check, both dispatchers, no protocol bump
  H  the host: the STATE at READY BEFORE the snapshot, the tick in the town-services tick, the READY gate of the handler, the kill switch AC_MP_TRAIN_SYNC (default ON)
  C  the client: ops only after the host's STATE arrived (s_at_cap) and READY, the per-session reset, a stale STATE ignored
  G  the game seams: mTRC_move runs the sync tick before the vanilla schedule; T4 stays out for a synced passenger; the ride-off demo asks first, skips its own
     train request when synced, gets off only on its turn and reports off / welcomed; Porter's presence only for an idle Porter (think 8 / 9) without a local ride-off
  V  every vanilla line of the touched decomp files is unchanged (TARGET_PC blocks stripped, compared with HEAD)
Usage: python test_arrival_train_src.py"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
results = []


def ck(d, c):
    results.append((d, bool(c)))
    print(("PASS - " if c else "FAIL - ") + d)


def rd(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read().decode("utf-8", "replace").replace("\r\n", "\n")


def head(rel):
    try:
        return subprocess.run(["git", "-C", ROOT, "show", "HEAD:" + rel], capture_output=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    except (OSError, subprocess.SubprocessError):
        return ""


def strip_pc(text):
    """drops the TARGET_PC branches (keeps an #else branch of #ifdef TARGET_PC); other #if blocks are kept verbatim"""
    out, stack = [], []
    for ln in text.split("\n"):
        s = ln.strip()
        if s.startswith("#ifdef TARGET_PC") or s.startswith("#if defined(TARGET_PC)"):
            stack.append("pc")
            continue
        if s.startswith("#ifndef TARGET_PC"):
            stack.append("nopc")
            continue
        if s.startswith("#if"):
            stack.append("other")
        elif s.startswith("#else") and stack and stack[-1] in ("pc", "nopc"):
            stack[-1] = "nopc" if stack[-1] == "pc" else "pc"
            continue
        elif s.startswith("#endif") and stack:
            k = stack.pop()
            if k in ("pc", "nopc"):
                continue
        if "pc" in stack:
            continue
        out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))  # a TARGET_PC block between two functions leaves one extra blank line


def main():
    ng = rd("pc/src/pc_net_game.c")
    inc = rd("pc/src/pc_net_game_arrtrain.c_inc")
    hdr = rd("pc/include/pc_arrival_train.h")
    trc = rd("src/game/m_train_control.c")
    rod = rd("src/actor/ac_ride_off_demo_move.c_inc")
    stm = rd("src/actor/npc/ac_npc_station_master_schedule.c_inc")
    stmc = rd("src/actor/npc/ac_npc_station_master.c")
    trh = rd("include/m_train_control.h")
    pv = rd("pc/include/pc_net_game.h")

    # ---- W
    ck("W id 105 ARRIVAL_TRAIN is the last enum entry, statically tied to PCAT_MSG_ID",
       re.search(r"PC_NETGAME_MSG_ARRIVAL_TRAIN\s*=\s*105,", ng) is not None and "} PCNetGameMsgType;\n_Static_assert((int)PC_NETGAME_MSG_ARRIVAL_TRAIN == (int)PCAT_MSG_ID" in ng
       and "#define PCAT_MSG_ID 105u" in hdr)
    ck("W the layouts are pinned: wire entry 20 bytes, message 48 bytes", "pcat_wire_size_check[(sizeof(PCArrTrainWire) == 20) ? 1 : -1]" in hdr and
       "pcat_msg_size_check[(sizeof(PCArrTrainMsg) == 48) ? 1 : -1]" in hdr)
    ck("W the protocol version is untouched (no bump)", "#define PC_NETGAME_PROTOCOL_VERSION 8" in pv or re.search(r"PC_NETGAME_PROTOCOL_VERSION\s+8\b", pv + ng) is not None)
    ck("W host dispatcher: 105 goes to pcnetgame_handle_host_arrtrain (size checked inside)", "if (data[0] == (uint8_t)PC_NETGAME_MSG_ARRIVAL_TRAIN) { /* SYNCED ARRIVAL TRAIN" in ng and
       "pcnetgame_handle_host_arrtrain(peer, data, size);" in ng)
    ck("W client dispatcher: a 48-byte 105 goes to pcnetgame_handle_client_arrtrain", "if (size == sizeof(PCArrTrainMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_ARRIVAL_TRAIN) {" in ng)
    ck("W every receiver runs the shape check (pcat_msg_ok) and the op direction check", inc.count("pcat_msg_ok(&m)") == 2 and "m.op == PCAT_OP_STATE) {\n        printf(\"[TRAIN][SYNC] host: peer %d malformed" in inc
       and "m.op != PCAT_OP_STATE) {\n        printf(\"[TRAIN][SYNC] client: malformed STATE dropped" in inc)
    ck("W the c_inc is included once, after the last function of pc_net_game.c (next to the roommates block, which stays the last line)",
       ng.count('#include "pc_net_game_arrtrain.c_inc"') == 1 and ng.rfind('#include "pc_net_game_arrtrain.c_inc"') > ng.rfind("\n}\n"))

    # ---- H
    i_cfg = ng.find("pcnetgame_host_ts_push_hostcfg(peer); /* batch A (A1)")
    i_at = ng.find("pcnetgame_arrtrain_host_on_ready(peer);", i_cfg)
    i_snap = ng.find('pcnetgame_host_start_snapshot(peer, "joined");', i_cfg)
    ck("H the STATE goes out at READY, after HOST_CONFIG and BEFORE the snapshot", 0 < i_cfg < i_at < i_snap)
    tick = ng[ng.find("static void pcnetgame_host_ts_tick(void) {"):]
    tick = tick[:tick.find("\n}\n")]
    ck("H the host tick runs in the town-services tick", "pcnetgame_arrtrain_host_tick(); /* synced arrival train" in tick)
    ck("H the host handler drops anything from a peer that is not READY (never a violation)", "s_host_peer_link[peer] != PC_NETGAME_LINK_READY) {\n        return; /* dropped (never a violation) */" in inc
       and "violation" not in inc.split("static void pcnetgame_handle_host_arrtrain")[1].split("\n}\n")[0].replace("never a violation", ""))
    ck("H kill switch AC_MP_TRAIN_SYNC: default ON, '0' = OFF", 'getenv("AC_MP_TRAIN_SYNC")' in inc and 's_on = !(e != NULL && strcmp(e, "0") == 0);' in inc)
    ck("H the switch gates the host side (READY push, tick, handler)", inc.count("!pcnetgame_arrtrain_enabled()") >= 5)
    ck("H a passenger whose connection is gone never holds a train", "pcat_host_peer_gone(&s_at_q, (uint8_t)p)" in inc)

    # ---- C
    snd = inc.split("static void pcnetgame_arrtrain_client_send(unsigned op) {")[1].split("\n}\n")[0]
    ck("C client ops only while READY and after the host's STATE (an old host never sees 105)", "s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY && s_at_cap" in snd)
    ck("C the capability / timeline are reset per client session", "pcnetgame_arrtrain_client_reset(); /* synced arrival train" in ng)
    ck("C a stale / duplicate STATE is ignored (seq)", "if (s_at_cap && (int32_t)(m.seq - s_at_cli_seq) <= 0) {" in inc)
    ck("C a fallback (no seat in time) is sticky for that arrival: no second request while the previous path still holds it; ABORT frees the seat",
       "if (s_at_fallback) {\n        return 0;" in inc and "s_at_fallback = 1; /* sticky" in inc and "pc_arrtrain_local_progress(PCAT_OP_ABORT);" in inc)
    ck("C pc_arrtrain_active: host = switch on, client = READY + STATE seen", "return s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY && s_at_cap;" in inc)

    # ---- G
    mv = trc[trc.find("extern void mTRC_move(GAME* game) {"):]
    ck("G mTRC_move: the sync tick runs right before the vanilla schedule (TARGET_PC)", "#ifdef TARGET_PC\n    mTRC_pc_sync_tick(play); /* synced arrival train: inert without the host's timeline */\n#endif\n    state = mTRC_schedule(play);" in mv)
    ck("G the train replica gets the game's own mTRC_* constants", "out->slow_speed = mTRC_SLOW_SPEED;" in trc and "out->speedup_rate = mTRC_SPEEDUP_RATE;" in trc)
    ck("G the sync tick never runs on a title demo and never takes a boarding / other local arrival train",
       "mEv_CheckTitleDemo() == mEv_TITLEDEMO_NONE && pc_arrtrain_view(&v)" in trc and "mTRC_pc_local_boarding(play) || mTRC_pc_local_other_arrival(play, &v)" in trc)
    ck("G the hold and the release go through the vanilla WAIT_STOPPED rule (start_timer only)", "Common_Set(train_start_timer, now_sec + 60); /* held at the station" in trc and
       "Common_Set(train_start_timer, now_sec); /* the vanilla departure" in trc)
    ra = trc[trc.find("extern int mTRC_pc_remote_arrival(GAME* game, int peer, int puppet_in_local_town) {"):]
    ck("G T4: a synced passenger never gets a separate local train (checked first)", ra.find("if (pc_arrtrain_peer_is_passenger(peer)) {") < ra.find("why = pcarr_remote_arrival_train_decide("))
    ck("G m_train_control.h declares the two seams", "extern int mTRC_pc_local_may_get_off(void);" in trh and "extern int mTRC_pc_synced_driving(void);" in trh)
    fs = rod[rod.find("static void aROD_first_set(ACTOR* actor, GAME* game) {"):]
    fs = fs[:fs.find("\n}\n")]
    ck("G ride-off first step: asks the timeline FIRST (hold = parked in DEMO_WAIT), the legacy shared arrival only when not synced",
       fs.find("s_aROD_synced = pc_arrtrain_arrival_query();") < fs.find("pc_remote_arrival_join_query(") and "} else if (Common_Get(player_no) == mPr_FOREIGNER) {" in fs
       and "mPlib_request_main_demo_wait_type1(game, FALSE, NULL); /* parked at the station start, no control, until the host's seat" in fs)
    ck("G ride-off: no own train request when synced", "    if (!s_aROD_synced) /* synced: the timeline already started the local train (mTRC_pc_sync_tick) */\n#endif\n    Common_Set(train_coming_flag, 3);" in fs)
    ck("G ride-off: gets off only on its turn (TARGET_PC), vanilla condition kept in the #else", "if (train->action == 5 && mTRC_pc_local_may_get_off()) {" in rod and "#else\n    if (train->action == 5) {\n#endif" in rod)
    ck("G ride-off: reports STEPPED_OFF at the get-off and WELCOMED when Porter's welcome ends", "pc_arrtrain_local_progress(PCAT_OP_STEPPED_OFF);" in rod and "pc_arrtrain_local_progress(PCAT_OP_WELCOMED);" in rod)
    pr = stm[stm.find("static int aSTM_pc_arrival_presence("):]
    ck("G Porter: only an idle Porter (think 8 / 9), never with a local ride-off demo or while talking", "(actor->think_idx != aSTM_THINK_8 && actor->think_idx != aSTM_THINK_9) ||" in pr
       and "mAc_PROFILE_RIDE_OFF_DEMO, ACTOR_PART_CONTROL) != NULL || mDemo_Check(mDemo_TYPE_TALK, ax) || mDemo_Check(mDemo_TYPE_SPEAK, ax)" in pr)
    ck("G Porter: the hook replaces only the vanilla idle request of talk_wait", "    if (!aSTM_pc_arrival_presence(actor, play)) /* synced arrival train" in stm and '#include "pc_arrival_train.h"' in stmc)
    ck("G pc_town_local_arrival_active still counts the ride-off demo (the hold keeps every same-position reload away)",
       "Actor_info_name_search(&play->actor_info, mAc_PROFILE_RIDE_OFF_DEMO, ACTOR_PART_CONTROL) != NULL) {\n        return 1;" in rd("pc/src/pc_m_card.c"))

    # ---- V
    for rel in ("src/game/m_train_control.c", "src/actor/ac_ride_off_demo_move.c_inc", "src/actor/npc/ac_npc_station_master_schedule.c_inc", "src/actor/npc/ac_npc_station_master.c",
                "src/actor/ac_train1_move.c_inc"):
        h = head(rel)
        ck("V %s: the vanilla (non-TARGET_PC) code is identical to HEAD" % rel, h != "" and strip_pc(rd(rel)) == strip_pc(h))

    bad = [d for d, ok in results if not ok]
    print("-" * 60)
    print("%d/%d checks passed" % (len(results) - len(bad), len(results)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
