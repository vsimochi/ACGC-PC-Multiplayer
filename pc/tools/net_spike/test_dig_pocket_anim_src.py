import re, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[3]
def rd(p): return (ROOT / p).read_text(encoding="utf-8", errors="replace")
fails = []
def check(c, m):
    if not c: fails.append(m)
g = rd("src/game/m_player_common.c_inc"); sc = rd("src/game/m_player_main_get_scoop.c_inc"); n = rd("pc/src/pc_net_game.c")
check("int pc_player_present_dig_pocket(" in g, "wrapper exists")
check("s_pc_get_scoop_present_only = TRUE;" in g and "s_pc_get_scoop_present_only = FALSE;" in g, "one-shot flag set/cleared on failure")
i = sc.index("if (s_pc_get_scoop_present_only)"); j = sc.index("mPlib_Get_space_putin_item_forTICKET")
check(i < j and "Player_actor_putin_item" not in sc[i:j], "presentation branch precedes and never calls putin_item")
check("success_flag = TRUE" in sc[i:j], "no pockets-full prompt / drop in presentation mode")
k = n.index("pc_player_present_dig_pocket((int)T.ut_x")
blk = n[k-900:k]
check("applied_ok" in blk and "PC_NETGAME_TXN_DEST_POCKET" in blk, "gated on applied_ok + pocket dest")
rej = n.index("matches no transaction in flight"); 
check(k > n.index("pcnetgame_ts_op_resolve(T.kind, T.request_id, applied_ok);"), "call is after APPLIED apply, not in reject path")
w = g[g.index("int pc_player_present_dig_pocket("):]
w = w[:w.index("s_pc_get_scoop_present_only = TRUE;")]
check("mPlayer_INDEX_WAIT" in w and "80.0f * 80.0f" in w, "dig anim guarded: player still waiting near the tile")
nn = n[n.index("pc_player_present_dig_pocket((int)T.ut_x")-700:]
check("PC_NETGAME_TXN_DEST_POCKET" in nn and "applied_ok" in nn, "only on APPLIED + pocket dest")
if fails:
    print("FAIL: " + " | ".join(fails)); sys.exit(1)
print("PASS")
