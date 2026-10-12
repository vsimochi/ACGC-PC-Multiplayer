#!/usr/bin/env python3
"""Pure source test (no game process): a pocket money bag is droppable over the network.

Checks pcnetgame_is_droppable_item() keeps every guard (EMPTY_NO / signboard / honeycomb / mNT_check_unknown /
type switch) but no longer excludes money bags, and that the host re-check + the client UI seam use the SAME predicate.
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SRC = open(os.path.join(ROOT, "pc", "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read()
TAG = open(os.path.join(ROOT, "src", "game", "m_tag_ovl.c"), encoding="utf-8", errors="replace").read()

fails = 0


def ck(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails += 1


i = SRC.index("static int pcnetgame_is_droppable_item(mActor_name_t item) {")
body = SRC[i:SRC.index("\n}\n", i)]
ck("EMPTY_NO / ITM_SIGNBOARD / HONEYCOMB still refused", all(s in body for s in ("EMPTY_NO", "ITM_SIGNBOARD", "HONEYCOMB")))
ck("mNT_check_unknown guard kept", "mNT_check_unknown(item)" in body)
ck("money-bag exclusion removed", "pcnetgame_is_money_bag_item" not in body)
ck("type switch kept (FTR0 / FTR1 / ITEM1 only)", all(s in body for s in ("NAME_TYPE_FTR0", "NAME_TYPE_FTR1", "NAME_TYPE_ITEM1")) and "default:" in body)
ck("host re-check still uses the predicate", "if (!pcnetgame_is_droppable_item((mActor_name_t)in->claimed_item))" in SRC)
ck("client UI seam uses the same predicate (no separate bag exclusion)", "pc_net_game_is_droppable_item((int)put_item)" in TAG and "money" not in TAG[TAG.index("pc_net_game_is_droppable_item((int)put_item)") - 200:TAG.index("pc_net_game_is_droppable_item((int)put_item)") + 200].lower())
ck("pickup of a field bag still uses the wallet/pocket route", "pcnetgame_txn_pickup_route" in SRC)
ck("the drop txn precondition is item-generic (slot must hold the claimed item)", "pocket slot does not hold the claimed item" in SRC)
ck("doc comment updated", "money-bag drop enabled" in SRC)

# ---- model: conservation of one bag through drop + pickup ----
def drop_then_pickup(pocket_bag, wallet, wallet_max=99999):
    field = pocket_bag          # leaves the pocket exactly when written to the field (APPLIED)
    pocket = 0
    # pickup: wallet if it fits, else pocket
    if wallet + field <= wallet_max:
        wallet += field
        field = 0
    else:
        pocket, field = field, 0
    return pocket, wallet, field


ok = True
for bag in (1000, 10000, 30000, 100):
    for w in (0, 1, 69999, 70000, 99000, 99999):
        p, nw, f = drop_then_pickup(bag, w)
        ok &= (p + nw + f == bag + w) and f == 0
ck("model: pocket bag + wallet conserved across drop + pickup", ok)

print("RESULT:", "ALL PASS" if fails == 0 else "%d FAILED" % fails)
sys.exit(1 if fails else 0)
