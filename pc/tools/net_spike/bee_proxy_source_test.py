"""Source test (agent 11, bees): the observer proxy path in ac_bee.c never reaches mPlib_* (in particular
mPlib_request_main_stung_bee_type1), never requests a net catch and never sets Common state; pc_bee_sync.c
never mutates game state. Run: python pc/tools/net_spike/bee_proxy_source_test.py"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BEE = open(os.path.join(ROOT, "src", "actor", "ac_bee.c"), encoding="utf-8").read()
SYNC = open(os.path.join(ROOT, "pc", "src", "pc_bee_sync.c"), encoding="utf-8").read()
FORBIDDEN = ("mPlib_", "GET_PLAYER_ACTOR_NOW", "Set_Item_net_catch", "Common_Set", "mFI_Set", "mPr_", "Save_Set")


def body(src, name):
    m = re.search(r"^static (?:void|f32|int) " + re.escape(name) + r"\([^)]*\) \{\n", src, re.M)
    assert m, "function not found: " + name
    i, depth = m.end(), 1
    while depth:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[m.end():i]


fails = []
proxy = body(BEE, "aBEE_proxy_move")
# Every helper the proxy can reach (directly, or through aBEE_setupAction's DISAPPEAR / FLY init procs).
reach = {"aBEE_proxy_move": proxy}
for helper in ("aBEE_appear", "aBEE_fly_move_common", "aBEE_disappear", "aBEE_calc_fly_angle", "aBEE_anime_proc",
               "aBEE_calc_scale", "aBEE_fly_init", "aBEE_disappear_init"):
    reach[helper] = body(BEE, helper)
for name, text in reach.items():
    for bad in FORBIDDEN:
        if bad in text:
            fails.append("%s reaches %s" % (name, bad))
for proc in ("aBEE_fly(", "aBEE_attack(", "aBEE_attack_wait(", "aBEE_caught(", "action_proc"):
    if proc in proxy:
        fails.append("aBEE_proxy_move calls a vanilla player-coupled proc: " + proc)
for act in re.findall(r"aBEE_setupAction\(bee, (aBEE_ACT_\w+)", proxy):
    if act != "aBEE_ACT_DISAPPEAR":
        fails.append("proxy enters a non-cosmetic action " + act)
mv = body(BEE, "aBEE_actor_move")
if mv.find("pc_bee_sync_is_proxy") < 0 or mv.find("pc_bee_sync_is_proxy") > mv.find("action_proc"):
    fails.append("aBEE_actor_move does not branch to the proxy before the vanilla procs")
ai = body(BEE, "aBEE_appear_init")
if not (0 <= ai.find("pc_bee_sync_is_proxy") < ai.find("Common_Set(player_bee_chase_flag")):
    fails.append("aBEE_appear_init sets player_bee_chase_flag for a proxy")
for bad in FORBIDDEN:
    if bad in SYNC:
        fails.append("pc_bee_sync.c references " + bad)

print("bee_proxy_source_test: " + ("ALL PASSED" if not fails else "FAILED\n  " + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
