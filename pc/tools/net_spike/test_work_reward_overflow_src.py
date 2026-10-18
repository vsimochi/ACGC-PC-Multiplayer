#!/usr/bin/env python3
"""Pure source test (no game process): Work Mode DELIVER / REPORT reward wallet overflow.

Checks pc/src/pc_net_game.c:
  1. pcnetgame_work_plan turns a reward that overflows the wallet into wallet + one ITM_MONEY_30000 bag
     (DELIVER: the vacated t->slot; REPORT: the first free pre-image pocket) and refuses WORK_WALLET_FULL
     only when no pocket is free; paid is set only on the paid path;
  2. the client APPLIED validator (pcnetgame_txn_apply_work) accepts the bag post-image for DELIVER / REPORT
     (and still for the VILLAGER_RECEIVE tip), finds the bag slot the same way the host chose it;
  3. a Python model: wallet + bag == pre-wallet + reward exactly for every wallet 0..99999 and
     reward 300..700 (the max the job generator produces), wallet stays <= the cap, and a full pocket
     set is refused with nothing changed.
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SRC = open(os.path.join(ROOT, "pc", "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read()

fails = 0


def ck(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails += 1


def func_body(name):
    m = re.search(r"\n(?:static )?[\w\s\*]+\b" + re.escape(name) + r"\([^;{]*\)\s*\{", SRC)
    if not m:
        return ""
    i = SRC.index("{", m.start())
    depth = 0
    for j in range(i, len(SRC)):
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:j + 1]
    return ""


enter = func_body("pcnetgame_work_enter")
plan = func_body("pcnetgame_work_plan")
commit = func_body("pcnetgame_work_commit")
apply_ = func_body("pcnetgame_txn_apply_work")

# ---- reward range from the generator ----
m = re.search(r"w->reward = (\d+)u \+ \(\(h >> 20\) & 3u\) \* (\d+)u;", enter)
ck("reward base formula found", m is not None)
extra = len(re.findall(r"w->reward \+= (\d+)u;", enter))
REWARD_MIN = int(m.group(1)) if m else 300
REWARD_MAX = (int(m.group(1)) + 3 * int(m.group(2)) + 100 * extra) if m else 700
ck("max generated reward (%d) < 30000: one bag always suffices" % REWARD_MAX, REWARD_MAX < 30000)

# ---- plan ----
ov = plan[plan.find("if (add != 0 && *post_wallet + add > (uint32_t)mPr_WALLET_MAX)"):]
ck("plan: overflow block exists", bool(ov) and "ITM_MONEY_30000" in ov)
ck("plan: only DELIVER / REPORT (and the Work v2 VILLAGER_COMPLETE, whose bag goes into the vacated slot like DELIVER) use the reward-overflow bag",
   "(op == (uint8_t)PC_WORK_OP_DELIVER || op == (uint8_t)PC_WORK_OP_REPORT || op == (uint8_t)PC_WORK_OP_VILLAGER_COMPLETE) && add < 30000u" in ov
   and "if (op == (uint8_t)PC_WORK_OP_DELIVER || op == (uint8_t)PC_WORK_OP_VILLAGER_COMPLETE) { /* the slot the handed-in item vacated */" in ov)
ck("plan: DELIVER bag goes into the vacated t->slot", "bag = (post[t->slot] == (uint16_t)EMPTY_NO) ? (int)t->slot : -1;" in ov)
ck("plan: REPORT bag goes into the first free pocket", re.search(r"for \(k = 0; k < mPr_POCKETS_SLOT_COUNT; k\+\+\) \{\s*if \(post\[k\] == \(uint16_t\)EMPTY_NO\) \{\s*bag = k;\s*break;", ov) is not None)
ck("plan: no free pocket -> still WORK_WALLET_FULL (nothing lost)",
   re.search(r"if \(bag < 0\) \{[^}]*WORK_WALLET_FULL", ov) is not None)
ck("plan: exact conservation wallet + 30000 == pre + reward", "*post_wallet = *post_wallet + add - 30000u;" in ov)
ck("plan: paid set on the overflow path", "*paid = add;" in ov[:ov.find("return 0;") + 10])
ck("plan: bag slot cond NORMAL", "mPr_SET_ITEM_COND(*post_conds, bag, mPr_ITEM_COND_NORMAL)" in ov)
ck("commit: DELIVER / REPORT retire the job only via the accepted plan",
   re.search(r"case PC_WORK_OP_DELIVER:\s*case PC_WORK_OP_REPORT:\s*pcnetgame_work_commit_deliver\(w\);", commit) is not None)
ck("host path: refusal returns before commit (journalled outcome, retries never re-execute)",
   SRC.find("pcnetgame_work_plan(work, t, post, &post_conds, &post_wallet, &work_reward, &fail)") <
   SRC.find("pcnetgame_work_commit(work, t->aux_cond, work_reward)") and "replay of an applied WORK op" in SRC)

# ---- client validator ----
ck("apply: bag slot computed for REPORT from the first free pre-image pocket",
   re.search(r"if \(t->pre_pockets\[i\] == \(uint16_t\)EMPTY_NO\) \{\s*bs = \(in->post_pockets\[i\] == \(uint16_t\)ITM_MONEY_30000\) \? i : -1;", apply_) is not None)
ck("apply: bag slot for VILLAGER_RECEIVE / DELIVER / (Work v2) VILLAGER_COMPLETE is t->slot",
   "(op == (uint8_t)PC_WORK_OP_VILLAGER_RECEIVE || op == (uint8_t)PC_WORK_OP_DELIVER || op == (uint8_t)PC_WORK_OP_VILLAGER_COMPLETE) && t->slot < (uint8_t)mPr_POCKETS_SLOT_COUNT" in apply_)
ck("apply: post < pre accepted only with a bag and dec < 30000",
   "(in->post_wallet < t->pre_wallet && !(bs >= 0 && t->pre_wallet - in->post_wallet < 30000u))" in apply_)
ck("apply: DELIVER non-empty slot only allowed with the overflow bag",
   "in->post_pockets[t->slot] != (uint16_t)EMPTY_NO && bs < 0" in apply_)
ck("apply: s initialised (REPORT delta path)", "s = -1, bs = -1;" in apply_)
ck("apply: REPORT delta path places the bag in a free local pocket",
   "op == (uint8_t)PC_WORK_OP_REPORT && bs >= 0" in apply_)

# ---- model ----
WALLET_MAX = 99999
POCKETS = 15


def plan_model(op, wallet, reward, pockets, slot):
    """returns (status, wallet, pockets, paid)"""
    post = list(pockets)
    if op == "DELIVER":
        post[slot] = None
    if wallet + reward <= WALLET_MAX:
        return 0, wallet + reward, post, reward
    bag = -1
    if reward < 30000 and wallet <= WALLET_MAX:
        if op == "DELIVER":
            bag = slot if post[slot] is None else -1
        else:
            for k in range(POCKETS):
                if post[k] is None:
                    bag = k
                    break
    if bag < 0:
        return 35, wallet, list(pockets), 0
    post[bag] = 30000
    return 0, wallet + reward - 30000, post, reward


def bags(p):
    return sum(x for x in p if x == 30000)


def validator(op, pre_w, post_w, pre_p, post_p, slot):
    if post_w > WALLET_MAX:
        return False
    if post_w < pre_w:
        bs = -1
        if op == "REPORT":
            for i in range(POCKETS):
                if pre_p[i] is None:
                    bs = i if post_p[i] == 30000 else -1
                    break
        elif post_p[slot] == 30000:
            bs = slot
        if not (bs >= 0 and pre_w - post_w < 30000):
            return False
    return True


ok = True
okv = True
refused_ok = True
wallets = list(range(0, 200)) + list(range(98500, WALLET_MAX + 1))
for op in ("DELIVER", "REPORT"):
    for w in wallets:
        for r in range(REWARD_MIN, REWARD_MAX + 1, 50):
            pre = ["x"] * POCKETS
            slot = 3
            if op == "REPORT":
                pre[7] = None  # one free pocket
            st, nw, post, paid = plan_model(op, w, r, pre, slot)
            ok &= st == 0 and paid == r and nw + bags(post) - bags(pre) == w + r and 0 <= nw <= WALLET_MAX
            okv &= validator(op, w, nw, pre, post, slot)
            if op == "REPORT":
                full = ["x"] * POCKETS
                st2, nw2, post2, paid2 = plan_model(op, w, r, full, slot)
                if w + r > WALLET_MAX:
                    refused_ok &= st2 == 35 and nw2 == w and post2 == full and paid2 == 0
                else:
                    refused_ok &= st2 == 0 and nw2 == w + r
ck("model: wallet + bag == pre-wallet + reward exactly, wallet <= cap (wallets 0..199, 98500..99999; rewards %d..%d)" % (REWARD_MIN, REWARD_MAX), ok)
ck("model: the client validator accepts every host post-image", okv)
ck("model: REPORT with no free pocket is refused with nothing changed", refused_ok)
ok2 = True
for w in range(0, WALLET_MAX + 1):
    r = REWARD_MAX
    st, nw, post, paid = plan_model("DELIVER", w, r, ["x"] * POCKETS, 0)
    ok2 &= st == 0 and nw + bags(post) == w + r and nw <= WALLET_MAX
ck("model: DELIVER at max reward conserves for every wallet 0..99999", ok2)

print("RESULT:", "ALL PASS" if fails == 0 else "%d FAILED" % fails)
sys.exit(1 if fails else 0)
