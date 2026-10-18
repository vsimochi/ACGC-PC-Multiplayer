#!/usr/bin/env python3
"""test_work_v2_src.py - Nook Work Mode v2 (pc/include/pc_work_logic.h): source audit + the native selftest (work_v2_selftest.c). No game process.

TIER: SOURCE + NATIVE. The host / wire behaviour is protocol-tested by test_work_v2_protocol.py (real host, FakeClients), the dialogue by test_work_v2_real.py (real game).
  N   the native selftest (tip formula / range / friendship, urgent roll + bonus, the item pick, the service 11 blob, the Check job choices, the v2 hand-over plans)
  H   host: the switch, ops 8..10 and the v2 tag flag only with it on; the v2 latch per peer (WORK_STATE bits only for a latched peer); the plan / commit of the new ops
  W   wire: service 11 WORK_CAPS (built / refreshed / validated / applied, per session), no new message id / TXN kind / reason
  C   client: every v2 op / the flag only after the host's service 11 bit (pc_net_game_work_v2), the BAD_SHAPE latch, the APPLIED apply of QUIT / COMPLETE
  D   dialogue: Nook (Give me a job / Check job / No. / Quit job, hand-overs WHILE the row is read) and the villager (row 1 on screen -> the hand-over -> row 2)
  P   the varied item pools: ordinary pocket items only (furniture, clothes, umbrellas, rugs, wallpaper, food, stationery), never tools / fish / bugs / money
  K   the kill switch AC_MP_WORK_V2 / settings.ini [Gameplay] work_v2
Usage: python test_work_v2_src.py
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
results = []


def ck(d, c):
    results.append((d, bool(c)))
    print(("PASS - " if c else "FAIL - ") + d)


def rd(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def body(src, head):
    i = src.find(head)
    if i < 0:
        return ""
    j = src.index("{", i + len(head) - 1)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    return ""


ng = rd("pc", "src", "pc_net_game.c")
nk = rd("pc", "src", "pc_nook_house.c")
nkh = rd("pc", "include", "pc_nook_house.h")
wl = rd("pc", "include", "pc_work_logic.h")
sc = rd("src", "actor", "npc", "ac_npc_shop_common.c")
tw = rd("src", "actor", "ac_quest_talk_work.c_inc")
st = rd("pc", "src", "pc_settings.c")

# ---- N
gcc = shutil.which("gcc") or r"C:\msys64\ucrt64\bin\gcc.exe"
out = os.path.join(os.environ.get("TEMP", HERE), "work_v2_selftest_src_audit.exe")
env = dict(os.environ, PATH=os.path.dirname(gcc) + os.pathsep + os.environ.get("PATH", ""))
cp = subprocess.run([gcc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Iinclude", "tools/net_spike/work_v2_selftest.c", "-o", out], cwd=PC, capture_output=True, text=True, env=env)
ck("N the native selftest compiles with -Wall -Wextra -Werror (%s)" % (cp.stderr.strip()[-200:] or "ok"), cp.returncode == 0)
if cp.returncode == 0:
    rr = subprocess.run([out], capture_output=True, text=True, env=env)
    m = re.search(r"(\d+)/(\d+) checks passed", rr.stdout)
    ck("N the native selftest passes (%s)" % (m.group(0) if m else rr.stdout[-200:]), rr.returncode == 0 and m is not None and m.group(1) == m.group(2) and int(m.group(2)) >= 25)

# ---- H
on = body(ng, "static int pcnetgame_work_v2_on(void) {")
ck("H1 the host switch: AC_MP_WORK_V2 (ON unless '0') AND settings.ini work_v2", 'getenv("AC_MP_WORK_V2")' in on and "e[0] == '0'" in on and "g_pc_settings.work_v2" in on)
ts = body(ng, "static void pcnetgame_handle_host_ts_txn(PCNetPeerId peer, const PCNetGameTxnCommitMsg* in) {")
ck("H2 shape: the v2 tag flag only for WORK and only with the switch on; ops 8..10 only with the switch on; URGE needs the job id, QUIT / COMPLETE a pocket slot + item",
   "(is_work && t->flags == (uint8_t)PC_WORK_TXN_FLAG_V2 && pcnetgame_work_v2_on())" in ts
   and "t->aux_cond <= (uint8_t)(pcnetgame_work_v2_on() ? PC_WORK_OP_MAX_V2 : PC_WORK_OP_MAX_V1)" in ts
   and "t->aux_cond == (uint8_t)PC_WORK_OP_URGE) && t->aux_item != 0)" in ts
   and "op == PC_WORK_OP_QUIT ||" in body(ng, "static int pcnetgame_work_op_has_slot(uint8_t op) {") and "op == PC_WORK_OP_VILLAGER_COMPLETE;" in body(ng, "static int pcnetgame_work_op_has_slot(uint8_t op) {"))
ck("H3 the latch: a valid v2-flagged WORK op latches the peer (logged); it is cleared when the link is not READY",
   "st->work_v2 = 1;" in ts and ts.index("st->work_v2 = 1;") > ts.index("t->aux_cond <= (uint8_t)(pcnetgame_work_v2_on()") and "wst->work_v2 = 0; /* the v2 latch is per session */" in ng)
pk = body(ng, "static void pcnetgame_work_pack_state(PCNetGameWorkStateMsg* m, const PCNetWorkChar* w, uint32_t request_id, int ext) {")
ss = body(ng, "static int pcnetgame_work_send_state(PCNetPeerId peer, const PersonalID_c* key, uint32_t request_id) {")
ck("H4 WORK_STATE carries the bits 1..3 only for a latched peer (the old client refuses unknown bits)", "if (ext) {" in pk and "PC_WORK_SF_JOB_V2" in pk and "PC_WORK_SF_URGED" in pk and "PC_WORK_SF_AUTO" in pk
   and "s_host_peer[peer].work_v2" in ss)
ck("H5 the v2 request flag reaches the plan / commit from BOTH callers (a peer's TXN and the host's own player)",
   "s_work_req_v2 = (t->flags == (uint8_t)PC_WORK_TXN_FLAG_V2);" in ts and ts.index("s_work_req_v2 = ") < ts.index("pcnetgame_work_plan(work, t,")
   and "s_work_req_v2 = pc_net_game_work_v2();" in body(ng, "static int pcnetgame_work_host_local(int op, int npc) {"))
en = body(ng, "static void pcnetgame_work_enter(PCNetWorkChar* w) {")
ck("H6 a v2 ENTER makes a v2 job (wflags, cleared for every new job): the varied pool item, no tip decided at creation; a v1 ENTER keeps stationery + the old tip rule",
   "w->wflags = (uint8_t)(s_work_req_v2 ? PC_WORK_WF_V2 : 0u);" in en and "pcnetgame_work_v2_item(type, w->job_id) : pcnetgame_work_errand_item(h >> 12)" in en
   and "switch ((w->wflags & PC_WORK_WF_V2) ? 0u : (h >> 9) % 3u)" in en and "w->tip_value = pcnetgame_work_tip_item(" in en)
pl = body(ng, "static uint8_t pcnetgame_work_plan(")
ck("H7 plan URGE / QUIT check the job id; QUIT needs the job's QUEST item and empties its slot; COMPLETE = RECEIVE's checks + add = reward + the v2 tip",
   "op == (uint8_t)PC_WORK_OP_URGE || op == (uint8_t)PC_WORK_OP_QUIT) &&" in pl and "case PC_WORK_OP_QUIT:" in pl and "case PC_WORK_OP_VILLAGER_COMPLETE:" in pl
   and "add = w->reward + pcnetgame_work_v2_tip_of(w);" in pl and pl.count("!= mPr_ITEM_COND_QUEST") >= 4)
ck("H8 the COMPLETE overflow: the money bag goes into the vacated slot like DELIVER; *paid = the whole payment",
   "(op == (uint8_t)PC_WORK_OP_DELIVER || op == (uint8_t)PC_WORK_OP_REPORT || op == (uint8_t)PC_WORK_OP_VILLAGER_COMPLETE) && add < 30000u" in pl
   and "if (op == (uint8_t)PC_WORK_OP_DELIVER || op == (uint8_t)PC_WORK_OP_REPORT || op == (uint8_t)PC_WORK_OP_VILLAGER_COMPLETE) {\n        *paid = add;" in pl)
cm = body(ng, "static void pcnetgame_work_commit(PCNetWorkChar* w, uint8_t op, uint32_t paid) {")
ck("H9 commit: URGE adds the bonus ONCE (URGED flag); QUIT abandons; COMPLETE records the tip, sets AUTO and retires + tombstones in the same step",
   "(w->wflags & PC_WORK_WF_URGED) == 0 && pcnetgame_work_urgent(w)" in cm and "w->wflags |= (uint8_t)PC_WORK_WF_URGED;" in cm and "w->reward += bonus;" in cm
   and "case PC_WORK_OP_QUIT:" in cm and "w->wflags |= (uint8_t)PC_WORK_WF_AUTO;" in cm and cm.index("w->tip_value = tip;") < cm.index("pcnetgame_work_commit_deliver(w); /* retired")
   and "PCNetWorkChar persisted layout drifted" in ng and "uint8_t      wflags;" in ng)
ck("H10 the tip: friendship of that villager with that CHARACTER in the host's save (Anmmem_c of its PersonalID), the pure pc_work_v2_tip",
   "mNpc_GetAnimalMemoryIdx((PersonalID_c*)pid, a->memories, ANIMAL_MEMORY_NUM)" in body(ng, "static int pcnetgame_work_friendship(const PersonalID_c* pid, uint16_t npc) {")
   and "pc_work_v2_tip(w->job_id, pcnetgame_work_friendship(&w->key, w->target_villager))" in ng)
ck("H11 a v2 job handed over with the OLD step (op 6) still gets the v2 Bells tip (decided then), REPORT pays only the reward",
   "} else if ((w->wflags & PC_WORK_WF_V2) != 0) {" in pl and "const uint32_t v2tip = pcnetgame_work_v2_tip_of(w);" in pl and "w->tip_value = pcnetgame_work_v2_tip_of(w);" in cm)

# ---- W
ck("W1 service 11 WORK_CAPS: 8 bytes, built from the switch, refreshed with the others; the array bound is 12",
   "#define PC_NETGAME_TS_WORK    11u" in ng and "#define PC_NETGAME_TS_NUM      12u" in ng and "#define PC_NETGAME_TS_WORK_LEN 8u" in ng
   and "pc_work_caps_build(blob, pcnetgame_work_v2_on());" in ng and "(void)pcnetgame_ts_refresh((int)PC_NETGAME_TS_WORK);" in ng)
ck("W2 the client accepts service 11 only with its length + pc_work_caps_valid, applies it as a session state, resets it per session",
   "svc != (int)PC_NETGAME_TS_WORK)" in ng and "(svc == (int)PC_NETGAME_TS_WORK) ? (uint16_t)PC_NETGAME_TS_WORK_LEN" in ng and "svc == (int)PC_NETGAME_TS_WORK ? pc_work_caps_valid(m.blob)" in ng
   and "s_work_caps_on = (int)(m.blob[1] & 1u);" in ng and "s_work_caps_on = 0;    /* Work v2: service 11 comes again at READY */" in ng)
ids = [int(x) for x in re.findall(r"^\s*PC_NETGAME_MSG_\w+\s*=\s*(\d+),", ng[ng.index("typedef enum PCNetGameMsgType {"):ng.index("} PCNetGameMsgType;")], re.M)]
kinds = [int(x) for x in re.findall(r"#define PC_NETGAME_TXN_KIND_\w+\s+(\d+)u", ng)]
reasons = [int(x) for x in re.findall(r"#define PC_NETGAME_TXN_REASON_\w+\s+(\d+)u", ng)]
ck("W3 no new message id (max %d), TXN kind (max %d) or reason (max %d): WORK kind 16 extended in place (ops 8..10)" % (max(ids), max(kinds), max(reasons)),
   max(ids) == 104 and max(kinds) == 22 and max(reasons) == 62 and "PC_WORK_OP_URGE = 8, PC_WORK_OP_QUIT = 9, PC_WORK_OP_VILLAGER_COMPLETE = 10" in nkh)
ck("W4 the WORK_STATE size is unchanged (36 B), the v2 bits ride in its flags byte", '_Static_assert(sizeof(PCNetGameWorkStateMsg) == 36, "PCNetGameWorkStateMsg wire size drifted");' in ng
   and "#define PC_WORK_SF_ALL     0x0Fu" in wl)

# ---- C
v2 = body(ng, "int pc_net_game_work_v2(void) {")
ck("C1 pc_net_game_work_v2: the local switch; a client also needs READY + service 11 received with bit 0 + no BAD_SHAPE latch",
   "pcnetgame_work_v2_on()" in v2 and "s_ts_client_have[PC_NETGAME_TS_WORK] && s_work_caps_on && !s_work_v2_refused" in v2)
bo = body(ng, "int pc_net_game_work_begin_op(int op, int npc) {")
ck("C2 begin_op: ops 8..10 only when v2; the flag travels as tag.flags (take_dst) only when v2",
   "op > (pc_net_game_work_v2() ? PC_WORK_OP_MAX_V2 : PC_WORK_OP_MAX_V1)" in bo and "s_ctxn.take_dst = (uint8_t)(pc_net_game_work_v2() ? PC_WORK_TXN_FLAG_V2 : 0u);" in bo
   and "flags = T->take_dst; /* Work v2: PC_WORK_TXN_FLAG_V2" in ng)
ck("C3 the villager auto-completes (op 10) only with v2, else the old RECEIVE",
   "*op = pc_net_game_work_v2() ? PC_WORK_OP_VILLAGER_COMPLETE : PC_WORK_OP_VILLAGER_RECEIVE;" in body(ng, "int pc_net_game_work_villager_pending(int npc, int* op) {"))
ck("C4 a BAD_SHAPE answer to a v2 WORK op latches the old dialogue for the session",
   "s_ts_last_reason == (uint8_t)PC_NETGAME_TXN_REASON_BAD_SHAPE && s_ctxn_work_v2_sent && !s_work_v2_refused" in ng and "s_work_v2_refused = 1;" in ng)
ap = body(ng, "static int pcnetgame_txn_apply_work(const PCNetGameClientTxn* T, const PCNetGameTxnResultMsg* in) {")
ck("C5 APPLIED: QUIT / COMPLETE remove the item; QUIT must leave the slot empty and the wallet unchanged; COMPLETE may put the overflow bag in t->slot",
   "op == (uint8_t)PC_WORK_OP_QUIT || op == (uint8_t)PC_WORK_OP_VILLAGER_COMPLETE);" in ap
   and "(op == (uint8_t)PC_WORK_OP_QUIT && (in->post_pockets[t->slot] != (uint16_t)EMPTY_NO || in->post_wallet != t->pre_wallet))" in ap
   and "op == (uint8_t)PC_WORK_OP_VILLAGER_COMPLETE) && t->slot < (uint8_t)mPr_POCKETS_SLOT_COUNT &&" in ap)
ck("C6 the client mirror takes the flag bits (validated against PC_WORK_SF_ALL) and shows them in the view",
   "(in->flags & ~(uint8_t)PC_WORK_SF_ALL) != 0" in ng and "s_work_c.flags = in->flags;" in ng and "v->urged = (s_work_c.flags & PC_WORK_SF_URGED) ? 1 : 0;" in ng)

# ---- D
sf = body(sc, "static void aNSC_pc_wk_start_flow(NPC_SHOP_COMMON_ACTOR* shop_common) {")
ck("D1 Nook: the v2 flow only with pc_net_game_work_v2(); an active job -> the Check job row, no job -> ENTER as before",
   "aNSC_pc_wk2 = pc_net_game_work_v2();" in sf and sf.index("aNSC_pc_wk2_check(shop_common, &v);") < sf.index("aNSC_pc_wk_op = PC_WORK_OP_ENTER;"))
ck("D2 the first page says 'Give me a job' / 'Check job' only for a v2 host (else the old labels)",
   "if (pc_net_game_work_v2()) {" in nk and "PC_NOOK_SEL_CHECK_JOB : PC_NOOK_SEL_GIVE_JOB" in nk and 'snprintf(tmp, sizeof(tmp), "Give me a job");' in nk and 'snprintf(tmp, sizeof(tmp), "Check job");' in nk
   and 'snprintf(tmp, sizeof(tmp), "Quit job");' in nk)
ask = sc[sc.index("if (aNSC_pc_hs_state == aNSC_PC_WK2_ASK) {"):sc.index("} else if (aNSC_pc_hs_state == aNSC_PC_UP_ASK) {")]
ck("D3 the Check job choices: the builder and the actor read the SAME pc_work_v2_row (with the same has-item bit): Here it is / I'll take it / I delivered it / No. -> URGE / Quit job",
   "pc_work_v2_row(s_wk_view.job_type, s_wk_view.obj_state, s_wk2_has, acts, 4)" in nk and "pc_work_v2_row(aNSC_pc_wk_view.job_type, aNSC_pc_wk_view.obj_state, aNSC_pc_wk2_has, acts, 4)" in ask
   and "act == PC_WORK_ACT_NO ? PC_WORK_OP_URGE" in ask and "PC_WORK_OP_QUIT : PC_WORK_OP_LEAVE" in ask and "ids[i] = acts[i] == PC_WORK_ACT_HAND ? PC_NOOK_SEL_WORK_HERE" in nk)
dn = body(sc, "static void aNSC_pc_wk2_done(NPC_SHOP_COMMON_ACTOR* shop_common) {")
ck("D4 Nook's answers: a delivery job hands the item over right away (TAKE_PARCEL after ENTER, or 'make room'); URGE -> urgent (+bonus) / hurry / take your time from the HOST's state",
   "aNSC_pc_wk_op = PC_WORK_OP_TAKE_PARCEL; /* the delivery item is handed over right with the job */" in dn and "PC_WK2_V_NOROOM" in dn
   and "if (have && v.urged && v.reward > before.reward) {" in dn and "PC_WK2_V_HURRY : PC_WK2_V_RELAX" in dn)
rw = body(sc, "static void aNSC_pc_wk2_rowwait(NPC_SHOP_COMMON_ACTOR* shop_common) {")
ck("D5 Nook: the hand-over starts only once its row is ON SCREEN (the build serial changed), the row is locked at its end, and the closing row follows the hand-over",
   "pc_nook_wk2_serial() == aNSC_pc_wk2_serial0" in rw and rw.index("pc_nook_wk2_serial() == aNSC_pc_wk2_serial0") < rw.index("pc_ho_work_plan(") and "mMsg_Set_LockContinue" in rw
   and "aNSC_pc_wk2_after = PC_NOOK_MSG_WK2_NEXT;" in rw and re.search(r"aNSC_pc_hs_state = aNSC_PC_HS_END;[^\n]*\n\s*if \(aNSC_pc_wk2_after >= 0\) \{\s*aNSC_pc_hs_set_msg\(shop_common, aNSC_pc_wk2_after\);", sc) is not None
   and "aNSC_pc_wk2_row_ho(shop_common, PC_WK2_V_TAKEBACK, PC_WK2_V_QUIT, PC_WORK_OP_QUIT);" in dn and "aNSC_pc_wk2_row_ho(shop_common, PC_WK2_V_THANKS, PC_WK2_V_PAID, PC_WORK_OP_DELIVER);" in dn
   and "aNSC_pc_wk2_row_ho(shop_common, PC_WK2_V_PARCEL, PC_WK2_V_PARCEL_PAY, PC_WORK_OP_TAKE_PARCEL);" in dn)
ck("D6 Quit job without the item in the pockets (lost / never taken) falls back to the plain LEAVE: a job can never be stuck",
   "if (r == 0 && aNSC_pc_wk2 && aNSC_pc_wk_op == PC_WORK_OP_QUIT) {" in sc and "aNSC_pc_wk_op = PC_WORK_OP_LEAVE;" in sc)
sh = body(tw, "static void aQMgr_work_show(QUEST_MANAGER_ACTOR* manager, int ok) {")
ck("D7 villager: the APPLIED selects row 1 (WK2) WITHOUT locking the hello; the hand-over waits for row 1 to be on screen (ROWWAIT), then HAND locks row 1, then row 2 (WK2_NEXT)",
   "mMsg_Set_continue_msg_num(mMsg_Get_base_window_p(), pc_nook_msg_id(PC_NOOK_MSG_WK2));" in sh and "mMsg_Set_LockContinue" not in sh.replace("mMsg_Set_LockContinue */", "")
   and "manager->talk_step = aQMgr_WORK_STEP_ROWWAIT;" in sh and "pc_ho_work_plan(l_work_op, ok," in sh
   and re.search(r"case aQMgr_WORK_STEP_ROWWAIT:\s*if \(pc_nook_wk2_serial\(\) == l_work_serial0\) \{", tw) is not None
   and "mMsg_Set_continue_msg_num(mMsg_Get_base_window_p(), pc_nook_msg_id(PC_NOOK_MSG_WK2_NEXT));" in body(tw, "static void aQMgr_work_next_row(QUEST_MANAGER_ACTOR* manager) {"))
ck("D8 villager rows: FETCH = 'Tom Nook sent you? Here you go: <item>!' + goodbye; DELIVER = '<item> from Tom Nook!' + (v2) the tip and Nook's pay, plus a chat notice of the payout",
   "PC_WK2_V_VGIVE : PC_WK2_V_VRECV" in sh and "PC_WK2_V_VDONE" in sh and "pc_net_game_work_notice(note);" in sh and "nk_text(b, \"Oh, Tom Nook sent you?\");" in nk
   and "nk_text(b, \"Tom Nook asked me to pay\");" in nk)
ck("D9 the item names are the game's own (mIN_copy_name_str + its article), set by the actor for every v2 row",
   "mIN_copy_name_str(nm, (mActor_name_t)item);" in body(ng, "void pc_net_game_work_item_name(int item, unsigned char* out16, int* article) {")
   and "pc_net_game_work_item_name(" in sc and "pc_net_game_work_item_name(" in tw and "static void nk_wk2_item(NkBuf* b) {" in nk)
ck("D10 two appended rows only (WK2 / WK2_NEXT: their text is a variant), count 55 < the roommate offset 64; the old Work rows stay for an old host",
   "#define PC_NOOK_MSG_COUNT 55" in nkh and "PC_NOOK_MSG_WK2,\n    PC_NOOK_MSG_WK2_NEXT\n};" in nkh and "case PC_NOOK_MSG_WORK_JOB: {" in nk)

# ---- P
fetch = body(ng, "static const uint16_t s_work_v2_fetch_pool[] = {")
parcel = body(ng, "static const uint16_t s_work_v2_parcel_pool[] = {")
ent = re.findall(r"\(uint16_t\)\(?([A-Z_0-9]+(?:\([A-Z_0-9]+\))?(?: \+ \d+)?)\)?", fetch + parcel)
allowed = re.compile(r"^(?:ITM_FOOD_(?:APPLE|CHERRY|PEAR|PEACH|ORANGE|COCONUT|CANDY)|FTR_START\(FTR_\w+\)|ITM_(?:CLOTH|UMBRELLA|CARPET|WALL|PAPER)_START \+ \d+)$")
bad = [e for e in ent if not allowed.match(e)]
ck("P1 the pools hold only fruit / candy / furniture / clothes / umbrellas / rugs / wallpaper / stationery (%d entries, odd ones: %s)" % (len(ent), bad), len(ent) >= 30 and not bad)
ck("P2 never a tool, fish, bug, money, ticket or gyroid", not re.search(r"ITM_(?:TOOL|NET|AXE|SHOVEL|ROD|FISH|INSECT|MONEY|TICKET|HANIWA|SPIRIT)", fetch + parcel))
ck("P3 the item is the host's choice from the job id (pc_work_v2_pick), so a retry / replay / restart never changes it", "pc_work_v2_pick(s_work_v2_fetch_pool," in ng and "pc_work_v2_pick(s_work_v2_parcel_pool," in ng)

# ---- K
ck("K1 settings.ini [Gameplay] work_v2 (default 1): parsed, written and documented; env AC_MP_WORK_V2=0 also switches it off",
   ".work_v2 = 1," in st and 'strcmp(key, "work_v2") == 0' in st and 'fprintf(f, "work_v2 = %d\\n", g_pc_settings.work_v2);' in st and '"work_v2 = 1\\n"' in st)

n_ok = sum(1 for _d, c in results if c)
print("-" * 60)
print("%d/%d checks passed" % (n_ok, len(results)))
sys.exit(0 if n_ok == len(results) else 1)
