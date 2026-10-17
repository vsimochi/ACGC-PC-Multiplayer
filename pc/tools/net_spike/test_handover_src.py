#!/usr/bin/env python3
"""test_handover_src.py - HAND-OVER ANIMATIONS (pc/include/pc_handover.h, message 103 HANDOVER): source audit + the native selftest. No game process.

  W  wire: PC_NETGAME_MSG_HANDOVER = 103 (unique, documented in wire_baseline / the old-peer compat test), PCHandoverMsg pinned (16 B), protocol still 8
  D  dispatch: the host branch takes exactly 16 bytes (a wrong size is dropped, never a violation); the client branch takes exactly 16 bytes; the client
     never presents a VILLAGER message or its own hand-over
  R  routing / validation: the host accepts a READY, well-formed, rate-limited sender with an announced scene; a VILLAGER message only from the peer that holds
     that villager's conversation (talk-hold peer set + npc_id) and for the npc_id of that slot; a SHOP message is relayed RELIABLE to every OTHER READY peer of the
     sender's announced scene (origin stamped); the host's held-villager script runs only while the talk hold lasts
  S  seams: the Work Mode villager talk and Nook's Work Mode rows start the VANILLA machinery (the quest manager's npc takeout order, mPlib_request_main_give_type1,
     the handOverItem birth / master change / request modes, Nook's TRANSFER / GET clips) only from the APPLIED path (pc_ho_work_plan(op, ok, ...)), always
     unlock the message, and time out; every town villager's vanilla aNPC_ACT_TRANS / aNPC_ACT_GET start is reported (outdoor actors only); indoor observers let a
     hand-over action through
  P  presentation only: none of the new code writes a pocket, a condition, the wallet or a job
  K  kill switch: AC_MP_HANDOVER_ANIM=0 / settings.ini [Gameplay] handover_anim (default 1, parsed + written) gate the plans, the sender and the host
  N  native selftest (pc/tools/net_spike/handover_selftest.c)
Usage: python test_handover_src.py
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
sys.path.insert(0, HERE)
import wire_baseline  # noqa: E402

results = []


def ck(d, c):
    results.append((d, bool(c)))
    print(("PASS - " if c else "FAIL - ") + d)


def rd(*p):
    return open(os.path.join(*p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def strip_comments(t):
    return re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", t), flags=re.S)


def body(src, head):
    i = src.index(head)
    return src[i:src.index("\n}\n", i)]


def between(src, a, b):
    i = src.index(a)
    return src[i:src.index(b, i)]


ng = rd(PC, "src", "pc_net_game.c")
gh = rd(PC, "include", "pc_net_game.h")
hh = rd(PC, "include", "pc_handover.h")
wk = rd(ROOT, "src", "actor", "ac_quest_talk_work.c_inc")
qm = rd(ROOT, "src", "actor", "ac_quest_manager.c")
sc = rd(ROOT, "src", "actor", "npc", "ac_npc_shop_common.c")
mv = rd(ROOT, "src", "actor", "npc", "ac_npc_move.c_inc")
mv2 = rd(ROOT, "src", "actor", "npc", "ac_npc2_move.c_inc")
tr = rd(ROOT, "src", "actor", "npc", "ac_npc_act_trans.c_inc")
gt = rd(ROOT, "src", "actor", "npc", "ac_npc_act_get.c_inc")
st = rd(PC, "src", "pc_settings.c")
sth = rd(PC, "include", "pc_settings.h")
oc = rd(HERE, "test_old_host_compat_src.py")

# ---- W
ids = dict((n, v) for n, v in wire_baseline.c_message_ids_raw(ng))
vals = sorted(ids.values())
ck("W PC_NETGAME_MSG_HANDOVER = 103, ids unique", ids.get("PC_NETGAME_MSG_HANDOVER") == 103 and len(vals) == len(set(vals)))
ck("W protocol version still 8", wire_baseline.header_protocol_ok(gh) and wire_baseline.EXPECTED_PROTOCOL_VERSION == 8)
m = re.search(r"typedef struct PCHandoverMsg \{(.*?)\} PCHandoverMsg;", hh, re.S)
norm = " ".join(strip_comments(m.group(1)).split()) if m else None
ck("W PCHandoverMsg pinned: %s" % norm, norm == "uint8_t msg_type; uint8_t kind; uint8_t npc_class; uint8_t slot; uint16_t npc_id; uint16_t item; uint8_t origin; "
   "uint8_t flags; uint16_t seq; uint32_t rsv;")
ck("W 16-byte size asserts (header + pc_net_game.c, <= PC_NET_MAX_PAYLOAD) and the id assert",
   "sizeof(PCHandoverMsg) == 16" in hh and "_Static_assert(sizeof(PCHandoverMsg) == 16 && sizeof(PCHandoverMsg) <= PC_NET_MAX_PAYLOAD" in ng
   and '_Static_assert(PC_HO_MSG_ID == (unsigned)PC_NETGAME_MSG_HANDOVER, "HANDOVER id");' in ng and "#define PC_HO_MSG_ID 103u" in hh)
ck("W 103 is a documented sprint id (wire_baseline) and listed for the old-peer compat audit", 103 in wire_baseline.SPRINT_EXTRA_MSG_IDS and '"HANDOVER": 103' in oc)

# ---- D
ck("D host dispatcher: id 103 -> exactly 16 bytes -> pcnetgame_handle_host_handover, a wrong size only counts a drop",
   re.search(r"if \(data\[0\] == \(uint8_t\)PC_NETGAME_MSG_HANDOVER\) \{[^}]*if \(size == sizeof\(PCHandoverMsg\)\) \{[^}]*pcnetgame_handle_host_handover\(peer, &ho\);\s*\} else \{\s*s_ho_stats\.rejected\+\+;", ng) is not None)
ck("D client dispatcher: exactly 16 bytes -> pcnetgame_handle_client_handover",
   re.search(r"if \(size == sizeof\(PCHandoverMsg\) && data\[0\] == \(uint8_t\)PC_NETGAME_MSG_HANDOVER\) \{[^}]*pcnetgame_handle_client_handover\(&ho\);", ng) is not None)
cl = body(ng, "static void pcnetgame_handle_client_handover(const PCHandoverMsg* in) {")
ck("D the client presents only a READY, enabled, well-formed SHOP message from somebody else",
   all(s in cl for s in ("s_client_link != PC_NETGAME_LINK_READY", "!pc_handover_enabled()", "!pc_ho_msg_ok(in, ANIMAL_NUM_MAX)", "in->npc_class != PC_HO_CLASS_SHOP",
                         "(PCNetPlayerId)in->origin == (PCNetPlayerId)s_client_assigned_peer_id", "pcnetgame_ho_shop_start(in, \"client\")")))

# ---- R
hr = body(ng, "static void pcnetgame_handle_host_handover(PCNetPeerId peer, const PCHandoverMsg* in) {")
ck("R host: READY sender, enabled, well-formed, an announced scene, then the per-peer rate",
   all(s in hr for s in ("s_host_peer_link[peer] != PC_NETGAME_LINK_READY", "!pc_handover_enabled()", "!pc_ho_msg_ok(in, ANIMAL_NUM_MAX)",
                         "!pc_remote_player_get_scene((PCNetPlayerId)peer, &sc) || !sc.valid", "!pc_ho_rate_ok(&s_ho_rate[peer], pcnetgame_now_ms())"))
   and hr.index("pc_ho_msg_ok") < hr.index("pc_ho_rate_ok"))
ck("R host VILLAGER: the slot's villager must carry that npc_id and the sender must hold its conversation (talk-hold peer set + npc_id)",
   "(uint16_t)a->id.npc_id != in->npc_id" in hr and "h->npc_id != in->npc_id || !pc_peer_set_has(&h->peer_mask, (int)peer)" in hr
   and "pc_ho_script_start(&s_ho_host_script[in->slot]" in hr)
rl = body(ng, "static void pcnetgame_ho_host_relay_shop(int origin, const PCNetPlayerScene* sc, const PCHandoverMsg* in) {")
ck("R host SHOP relay: origin stamped, RELIABLE, every OTHER READY peer whose announced scene (id + owner) equals the sender's",
   "m.origin = (uint8_t)origin;" in rl and "i != origin && s_host_peer_link[i] == PC_NETGAME_LINK_READY" in rl and "pcnetgame_aext_same_scene(&ps, sc)" in rl
   and "pc_net_send((PCNetPeerId)i, PC_NET_RELIABLE, &m" in rl)
ck("R host SHOP: shown on the host's own keeper only for a client's hand-over in the host player's own scene (not an observer host)",
   "origin != (int)PC_NETGAME_HOST_PLAYER_ID && !pc_host_observer_active() && pcnetgame_aext_same_scene(&s_local_scene, sc)" in rl)
hs = body(ng, "int pc_net_game_host_handover_step(int slot, int npc_id, int clip_stopped, float dt, int* changed, int* fish) {")
ck("R the host's villager script runs only on the host, for that npc_id, while the talk hold lasts (else it ends: the villager's own code takes over)",
   "s_role != PC_NETGAME_ROLE_HOST" in hs and "(int)s->npc_id != npc_id || !pc_net_game_host_npc_talk_held(slot, npc_id)" in hs)
ck("R the host tick is called only for a talk-HELD villager, after its (skipped) decisions; it restores the saved action at the end",
   re.search(r"if \(pc_talk_held\) \{\s*aNPC_pc_host_handover_tick\(nactorx, game\);", mv) is not None
   and "aNPC_setupAction(nactorx, s_saved[slot]);" in body(mv, "static void aNPC_pc_host_handover_tick(NPC_ACTOR* nactorx, GAME* game) {"))
lo = body(ng, "void pc_net_game_handover_local(int npc_class, int slot, int npc_id, int kind, int item, int fish) {")
ck("R the local report: a READY client sends ONE reliable 103 to the host; a host relays only its own SHOP hand-over (its villager is NPC_MOVE's business)",
   "s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY" in lo and "pc_net_send(0, PC_NET_RELIABLE, &m" in lo
   and "s_role == PC_NETGAME_ROLE_HOST && npc_class == PC_HO_CLASS_SHOP" in lo and "if (!pc_handover_enabled() || !s_local_scene.valid)" in lo)

# ---- S
show = body(wk, "static void aQMgr_work_show(QUEST_MANAGER_ACTOR* manager, int ok) {")
hand = body(wk, "static int aQMgr_work_hand(void) {")
ck("S work villager talk: the plan comes from pc_ho_work_plan(op, ok, ...) (a refusal plays nothing), the hello is locked while it runs",
   "pc_ho_work_plan(l_work_op, ok," in show and "pc_handover_enabled() ?" in show and "mMsg_Set_LockContinue" in show and "aQMgr_WORK_STEP_HAND" in show)
ck("S work villager talk: villager -> player = the quest manager's own takeout order; player -> villager = mPlib_request_main_give_type1 + the vanilla putaway order",
   "aQMgr_talk_common_set_npc_takeout(item);" in hand and "mPlib_request_main_give_type1(gamePT, item, aHOI_REQUEST_PUTAWAY, FALSE, FALSE);" in hand
   and "aQMgr_set_npc_putaway();" in hand and "hoi->request_mode == aHOI_REQUEST_TRANS_WAIT" in hand)
ck("S work villager talk: waits on the handOverItem master + the player's receive states, times out, always unlocks the message",
   "pc_ho_wait_tick(&l_work_ho_wait, hoi->master_actor != NULL, mPlib_check_player_actor_main_index_RecieveMove(gamePT)" in hand
   and hand.count("mMsg_Unset_LockContinue(mMsg_Get_base_window_p());") >= 2 and "PC_HO_WAIT_TIMEOUT" in hand)
ck("S the quest manager defines the two vanilla helpers the work talk forward-declares (aQMgr_talk_common_set_npc_takeout / aQMgr_set_npc_putaway)",
   "static int aQMgr_talk_common_set_npc_takeout(mActor_name_t item) {" in qm and "static int aQMgr_set_npc_putaway() {" in qm and '#include "pc_handover.h"' in qm)
done = body(sc, "static void aNSC_pc_wk_done(NPC_SHOP_COMMON_ACTOR* shop_common) {")
pend = body(sc, "static void aNSC_pc_wk_pending(NPC_SHOP_COMMON_ACTOR* shop_common) {")
ck("S Nook: the hand-over is planned only in aNSC_pc_wk_done (APPLIED / the host's own player), for TAKE_PARCEL and DELIVER / REPORT, after the end row",
   done.count("aNSC_pc_ho_plan(shop_common);") == 2 and "aNSC_pc_ho_plan" not in pend.split("aNSC_pc_wk_done(shop_common);")[0]
   and done.index("PC_NOOK_MSG_WORK_PARCEL") < done.index("aNSC_pc_ho_plan"))
tk = body(sc, "static int aNSC_pc_ho_tick(NPC_SHOP_COMMON_ACTOR* shop_common) {")
ck("S Nook gives: the handOverItem birth with Nook as master (PUTAWAY after mode), TRANSFER -> TRANS_WAIT request modes, TRANSFER1 / TRANS_WAIT1 clips",
   "hoi->birth_proc(item, aHOI_REQUEST_PUTAWAY, aHOI_NOT_PRESENT, actorx)" in tk and "chg_request_mode_proc(actorx, aHOI_REQUEST_TRANSFER)" in tk
   and "chg_request_mode_proc(actorx, aHOI_REQUEST_TRANS_WAIT)" in tk and "aNPC_ANIM_TRANSFER1" in tk and "aNPC_ANIM_TRANS_WAIT1" in tk)
ck("S Nook takes: mPlib_request_main_give_type1, then GET1 -> chg_master_proc -> GET_PULL -> the player's after mode with GET_PULL1 / GET_PUTAWAY1",
   "mPlib_request_main_give_type1(gamePT, item, aHOI_REQUEST_PUTAWAY, FALSE, FALSE);" in tk and "hoi->chg_master_proc(actorx)" in tk
   and "chg_request_mode_proc(actorx, aHOI_REQUEST_GET_PULL)" in tk and "chg_request_mode_proc(actorx, hoi->player_after_mode)" in tk
   and all(a in tk for a in ("aNPC_ANIM_GET1", "aNPC_ANIM_GET_PULL1", "aNPC_ANIM_GET_PUTAWAY1")))
ck("S Nook: every step is capped, an abandoned hand-over restores the clip, the row is always unlocked; the state goes HO_RUN -> END",
   'why = "TIMEOUT";' in tk and "mMsg_Unset_LockContinue(mMsg_Get_base_window_p());" in tk
   and re.search(r"case aNSC_PC_HO_RUN:\s*if \(aNSC_pc_ho_tick\(shop_common\)\) \{\s*aNSC_pc_hs_state = aNSC_PC_HS_END;", sc) is not None)
ck("S Nook reports both hand-overs to the other shoppers (class SHOP, the keeper's npc_id)",
   tk.count("pc_net_game_handover_local(PC_HO_CLASS_SHOP, -1, (int)(u16)actorx->npc_id,") == 2)
ob = body(sc, "static int aNSC_pc_ho_observer(NPC_SHOP_COMMON_ACTOR* shop_common, GAME_PLAY* play) {")
ck("S an observer's idle keeper (wait / walk after the player / turn) plays the relayed steps standing still and returns to its action",
   "pc_net_game_shop_handover_step((int)(u16)actorx->npc_id" in ob and "aNSC_setupAction(shop_common, play, shop_common->action);" in ob and "aNSC_set_stop_spd(shop_common);" in ob
   and all(re.search(r"static void %s\(NPC_SHOP_COMMON_ACTOR\* shop_common, GAME_PLAY\* play\) \{\s*(?:ACTOR\* actorx = \(ACTOR\*\)shop_common;\s*)?#ifdef TARGET_PC\s*if \(aNSC_pc_ho_observer\(shop_common, play\)\) \{" % f, sc)
           for f in ("aNSC_wait", "aNSC_walk_pl_other_zone", "aNSC_turn")))
ck("S every town villager's vanilla TRANS / GET start is reported (outdoor actors only: !aNPC_NPC2)",
   re.search(r"#if defined\(TARGET_PC\) && !defined\(aNPC_NPC2\)\s*if \(step == aNPC_ACT_TRANS_STEP_TRANS \|\| step == aNPC_ACT_TRANS_STEP_TRANS_F\) \{\s*aNPC_pc_handover_note\(nactorx, PC_HO_KIND_NPC_GIVES, nactorx->left_hand.item\);", tr)
   is not None and re.search(r"#if defined\(TARGET_PC\) && !defined\(aNPC_NPC2\)\s*if \(step == aNPC_ACT_GET_STEP_GET && nactorx->action.idx == aNPC_ACT_GET\) \{\s*aNPC_pc_handover_note\(nactorx, PC_HO_KIND_NPC_TAKES, CLIP\(handOverItem_clip\)->item\);", gt) is not None)
note = body(mv, "static void aNPC_pc_handover_note(NPC_ACTOR* nactorx, int kind, mActor_name_t item) {")
ck("S the report is for a real town villager slot only", "aNPC_get_animal_idx(nactorx)" in note and "slot < 0" in note and "pc_net_game_handover_local(PC_HO_CLASS_VILLAGER, slot," in note)
ck("S indoors (ROOM_NPC): another player's talk keeps the villager in WAIT except for a hand-over action of that talk",
   "if (other_talking && !(pc_handover_enabled() && desired >= aNPC_ACTION_TYPE_TRANS_MOVE && desired <= aNPC_ACTION_TYPE_ESTIMATE_F)) {" in mv2)

# ---- P
blocks = {
    "pc_net_game.c HANDOVER block": between(ng, "/* ===== HAND-OVER ANIMATIONS BEGIN", "/* ===== HAND-OVER ANIMATIONS END ===== */"),
    "ac_quest_talk_work.c_inc hand-over": show + hand,
    "ac_npc_shop_common.c hand-over": between(sc, "static int aNSC_pc_ho_n,", "/* the op finished (APPLIED") + ob,
    "ac_npc_move.c_inc hand-over": body(mv, "static void aNPC_pc_host_handover_tick(NPC_ACTOR* nactorx, GAME* game) {") + note,
    "pc_handover.h": hh,
}
for name, b in blocks.items():
    code = strip_comments(b)
    bad = [w for w in ("pockets[", "item_conditions", "wallet", "mPr_Set", "mPr_SetFreePossessionItem", "Now_Private", "pcnetgame_work_commit", "inventory.") if w in code]
    ck("P %s writes no pocket / condition / wallet / job (%s)" % (name, bad or "none"), not bad)

# ---- K
en = body(ng, "int pc_handover_enabled(void) {")
ck("K pc_handover_enabled = AC_MP_HANDOVER_ANIM (0 = off, ON by default) && g_pc_settings.handover_anim", 'getenv("AC_MP_HANDOVER_ANIM")' in en and "e[0] == '0') ? 0 : 1" in en
   and "g_pc_settings.handover_anim" in en)
ck("K settings.ini [Gameplay] handover_anim: default 1, parsed (0 / 1), written, documented in the template",
   ".handover_anim = 1," in st and 'strcmp(key, "handover_anim") == 0' in st and 'fprintf(f, "handover_anim = %d\\n", g_pc_settings.handover_anim);' in st
   and '"handover_anim = 1\\n"' in st and "int handover_anim;" in sth)

# ---- N
src = os.path.join(HERE, "handover_selftest.c")
gcc = None
for cand in ("gcc", r"C:\msys64\ucrt64\bin\gcc.exe"):
    try:
        subprocess.run([cand, "--version"], capture_output=True, check=True, timeout=30)
        gcc = cand
        break
    except (OSError, subprocess.SubprocessError):
        continue
ck("N native selftest present and includes the real header", os.path.isfile(src) and '#include "pc_handover.h"' in open(src).read())
if gcc is None:
    print("SKIPPED - gcc not found: native selftest not run")
else:
    out = os.path.join(os.environ.get("TEMP", HERE), "handover_selftest_src_audit.exe")
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + r"C:\msys64\ucrt64\bin"
    cp = subprocess.run([gcc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Iinclude", "tools/net_spike/handover_selftest.c", "-o", out], cwd=PC,
                        capture_output=True, timeout=120, env=env)
    ck("N selftest compiles warning-free (%s)" % cp.stderr.decode(errors="replace")[-300:], cp.returncode == 0)
    if cp.returncode == 0:
        r = subprocess.run([out], capture_output=True, timeout=60, env=env)
        tail = r.stdout.decode(errors="replace").strip().splitlines()[-1:] or [""]
        ck("N selftest passes (%s)" % tail[0], r.returncode == 0)

print("-" * 60)
n = sum(1 for _d, okv in results if okv)
print("%d/%d checks passed" % (n, len(results)))
sys.exit(0 if n == len(results) else 1)
