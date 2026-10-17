import re, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[3]
def rd(p): return (ROOT / p).read_text(encoding="utf-8", errors="replace")
fails = []
def check(c, m):
    if not c: fails.append(m)
n = rd("pc/src/pc_net_game.c"); b = rd("src/bg_item/bg_item.c"); c = rd("src/bg_item/bg_item_common.c_inc")
check(re.search(r"PC_NETGAME_MSG_TREE_DROP\s+= 77,", n), "id 77")
ids = re.findall(r"^\s+PC_NETGAME_MSG_\w+\s+= (\d+),", n, re.M)  # enum lines only (comments quote ids too)
check(len(ids) == len(set(ids)), "enum ids unique")
check("_Static_assert(sizeof(PCNetGameTreeDropMsg) == 12" in n, "static assert")
check("pcnetgame_handle_client_tree_drop(&td)" in n, "client dispatch")
i = n.index("static void pcnetgame_host_place_tree_drops(")
f = n[i:i+2500]
check(f.index("pcfa_set_tile") < f.index("pcnetgame_host_tree_drop_broadcast"), "broadcast after authoritative tile write")
h = n[n.index("static void pcnetgame_tree_drop_present"):n.index("static void pcnetgame_host_tree_drop_broadcast")]
check("pcfa_set_tile" not in h and "mFI_SetFG" not in h, "presentation path never writes tiles")
p = b[b.index("int pc_bg_item_present_tree_drop("):]
p = p[:p.index(chr(10) + "}")]
check("0x4" in p and "mFI_SetFG_common" not in p, "drop actor flagged no-landing-write; no SetFG")
check("pc_bg_item_present_tree_drop" not in re.sub(r"/\*.*?\*/|//[^\n]*", "", c, flags=re.S), "not in the 4x-included common file (code; a doc comment mention is fine)")
h2 = n[n.index("static void pcnetgame_handle_client_tree_drop"):n.index("static void pcnetgame_host_tree_drop_broadcast")]
check("HONEYCOMB" in h2 and "mNT_check_unknown" in h2, "client rejects honeycomb/unknown item")
check(h2.count("pcfa_town_ut_to_acre_tile") == 2 and "> 2" in h2, "client bounds both tiles + tree/drop proximity")
check("s_recent_seq" in h2, "client duplicate-seq filter")
check("pcnetgame_tree_drop_present" in h2 and "pcfa_scene_is_town()" in n[n.index("static void pcnetgame_tree_drop_present"):], "town-scene gate")
if fails:
    print("FAIL: " + " | ".join(fails)); sys.exit(1)
print("PASS")
