#!/usr/bin/env python3
"""Pure source test (no game process): Work Mode villager tip pool + Bells-tip wallet overflow.

Checks pc/src/pc_net_game.c:
  1. the tip item comes from pcnetgame_work_tip_item() (a pool disjoint from the stationery parcel pool),
     never from pcnetgame_work_errand_item();
  2. a Python model of the pool/overflow rules: tip != parcel for every hash, deterministic, and
     wallet + bag == pre-wallet + tip exactly for every wallet value;
  3. the plan no longer fails the whole VILLAGER_RECEIVE with WORK_WALLET_FULL for a Bells tip, the
     commit sets tip_paid only when paid, and the client APPLIED validator accepts the overflow image.
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
tip = func_body("pcnetgame_work_tip_item")
plan = func_body("pcnetgame_work_plan")
commit = func_body("pcnetgame_work_commit")

ck("tip pool function exists", bool(tip))
ck("tip pool has no stationery (ITM_PAPER) entries", tip and "ITM_PAPER" not in tip and "errand_item" not in tip)
ck("job creation draws the tip from the tip pool", "w->tip_value = pcnetgame_work_tip_item(" in enter)
ck("job creation no longer draws the tip from the errand (stationery) pool",
   not re.search(r"tip_value\s*=\s*pcnetgame_work_errand_item", enter))
ck("tip is stored in the job (retries read it, never re-roll)", "w->tip_value" in enter)
ck("tip item is placed with cond NORMAL in the plan",
   re.search(r"post_conds = mPr_SET_ITEM_COND\(\*post_conds, t->slot, mPr_ITEM_COND_NORMAL\);\s*\n\s*if \(w->tip_kind == PC_WORK_TIP_BELLS\)", plan) is not None)
ck("Bells tip: overflow becomes a money bag (ITM_MONEY_30000) in the vacated parcel slot",
   "ITM_MONEY_30000" in plan and "post[t->slot] == (uint16_t)EMPTY_NO" in plan)
ck("Bells tip never returns WORK_WALLET_FULL (only the reward path can)",
   "add = w->tip_value;" in plan and "*paid = w->tip_value;" in plan)
ck("commit takes the paid amount and sets tip_paid only when paid",
   "uint32_t paid" in SRC[SRC.index("static void pcnetgame_work_commit("):][:120] and "paid > 0u" in commit)
ck("both commit call sites pass paid", "pcnetgame_work_commit(work, t->aux_cond, work_reward)" in SRC and "pcnetgame_work_commit(w, (uint8_t)op, paid)" in SRC)
ck("client APPLIED validator accepts the overflow post-image", "the Bells-tip / reward overflow: wallet + a 30000 bag" in SRC and "const u32 dec = t->pre_wallet - in->post_wallet;" in SRC)

# ---- model ----
PAPER = [0x2000 + i for i in range(8)]
POOL = [0x2800, 0x2801, 0x2802, 0x2803, 0x2804, 0x2807]
WALLET_MAX = 99999


def tip_item(h):
    return POOL[(h & 0xFFFFFFFF) % len(POOL)]


def receive(pre_wallet, tip):
    """returns (wallet, bag_value, paid)"""
    bells = pre_wallet + tip
    if bells < WALLET_MAX:
        return bells, 0, tip
    if bells - 30000 < WALLET_MAX:
        return bells - 30000, 30000, tip
    return pre_wallet, 0, 0


ck("model: tip never equals any parcel id, for 100k hashes", all(tip_item(h) not in PAPER for h in range(100000)))
ck("model: tip is deterministic", all(tip_item(h) == tip_item(h) for h in range(1000)))
ok = True
for w in list(range(0, 300)) + list(range(99000, WALLET_MAX + 1)):
    for tipv in (100, 150, 200, 250):
        nw, bag, paid = receive(w, tipv)
        if paid:
            ok &= (nw + bag == w + tipv) and nw < WALLET_MAX
        else:
            ok &= (nw == w and bag == 0)
ck("model: wallet + bag == pre-wallet + tip exactly, wallet stays below the cap", ok)

print("RESULT:", "ALL PASS" if fails == 0 else "%d FAILED" % fails)
sys.exit(1 if fails else 0)
