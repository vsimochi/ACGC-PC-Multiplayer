#!/usr/bin/env python3
"""test_shop_nook_sync_src.py - SHOP KEEPER TARGET SYNC (pc/include/pc_shop_keeper_target.h, pc_net_game_shop_keeper_target): source audit + the native selftest.
No game process.

  W  no wire: no new message id (wire_baseline max id unchanged), no pc_net_send in the feature block
  C  candidates: this process' own player (never the hidden --host-observer) + the puppets of the SAME scene (scene id + owner) with their newest MOVE
     position and main index; the local wire id (host 0 / client assigned id); a talking state = mPlayer_INDEX_TALK .. GIVE_WAIT
  L  a talk demo with the LOCAL player forces the local player (vanilla); the result is used only in the idle actions (WAIT / 0x3e / WALK / 0x40 / 0x41 / TURN)
  K  keeper seams: aNSC_set_zone_data retargets (both variants), aNSC_set_player_angl aims at the target, aNSC_decide_next_move_act measures the target's distance
     and safe zone (both variants), all under TARGET_PC; the five keeper actors (Cranny, Nook 'n' Go, Nookway, Nookington's, Timmy / Tommy) use the common file
  H  head look: a synced keeper looks at the remote target (POS request) or keeps vanilla's local look; the generic nearest-puppet look-at is skipped for it
  S  kill switch: AC_MP_SHOP_NOOK_SYNC=0 / settings.ini [Gameplay] shop_nook_sync (default 1, parsed, written, in the template)
  T  the TEST-ONLY placement / log hook is behind PC_NET_TEST_HOOKS + pc_test_hook_getenv
  N  native selftest (pc/tools/net_spike/shop_keeper_target_selftest.c)
Usage: python test_shop_nook_sync_src.py
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


def body(src, head):
    i = src.index(head)
    return src[i:src.index("\n}\n", i)]


ng = rd(PC, "src", "pc_net_game.c")
gh = rd(PC, "include", "pc_net_game.h")
kh = rd(PC, "include", "pc_shop_keeper_target.h")
sc = rd(ROOT, "src", "actor", "npc", "ac_npc_shop_common.c")
hd = rd(ROOT, "src", "actor", "npc", "ac_npc_head.c_inc")
st = rd(PC, "src", "pc_settings.c")
sth = rd(PC, "include", "pc_settings.h")
blk = ng[ng.index("===== SHOP KEEPER TARGET SYNC BEGIN"):ng.index("===== SHOP KEEPER TARGET SYNC END")]
tgt = body(ng, "int pc_net_game_shop_keeper_target(")

# ---- W
ids = [int(x) for x in re.findall(r"PC_NETGAME_MSG_\w+\s*=\s*(\d+)", ng)]
hi = max([wire_baseline.EXPECTED_MAX_MSG_ID] + list(wire_baseline.SPRINT_EXTRA_MSG_IDS))
ck("W highest message id still %d = the documented baseline + sprint extras (no new id; found %s)" % (hi, ids and max(ids)), ids and max(ids) == hi == 104)
ck("W the feature block sends nothing", "pc_net_send" not in blk)

# ---- C
ck("C candidates: own player unless the hidden observer, real player actor", "!pc_host_observer_active() && pcnetgame_is_real_player_actor(pl)" in tgt)
ck("C candidates: puppets of the same scene id AND owner, with their newest MOVE position and motion",
   "ps.scene_id != ls.scene_id || ps.owner != ls.owner" in tgt and "pc_remote_player_get_last_position" in tgt and "pc_remote_player_get_last_motion" in tgt)
ck("C the local wire id: host PC_NETGAME_HOST_PLAYER_ID / client s_client_assigned_peer_id",
   "local_id = (s_role == PC_NETGAME_ROLE_HOST) ? (int)PC_NETGAME_HOST_PLAYER_ID : (int)s_client_assigned_peer_id;" in tgt)
ck("C talking = mPlayer_INDEX_TALK .. mPlayer_INDEX_GIVE_WAIT", "mi >= mPlayer_INDEX_TALK && mi <= mPlayer_INDEX_GIVE_WAIT" in blk)
ck("C only a host or a READY client, only with a local scene",
   "s_role != PC_NETGAME_ROLE_HOST && !(s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY)" in tgt and "pc_net_game_get_local_scene(&ls)" in tgt)
ck("C the shared picker decides (pc_skt_pick); the local player / nobody returns 0 (vanilla)",
   "pc_skt_pick(&s_skt[slot].st, kx, kz, cand, n, dt)" in tgt and "if (pick == PC_SKT_NONE || pick == local_id)" in tgt)
ck("C header: margin one tile, dwell, talk range, ties by the lower id",
   "#define PC_SKT_MARGIN 40.0f" in kh and "#define PC_SKT_DWELL 20.0f" in kh and "PC_SKT_TALK_RANGE" in kh and "(da == db && ida < idb)" in kh)

# ---- L
ck("L a talk demo with the local player forces the local player", "if (local_talk) {" in tgt and "pick = local_id;" in tgt)
rt = body(sc, "static void aNSC_pc_retarget(")
ck("L local talk = the keeper is the talk actor / in a SPEAK or TALK demo",
   "mDemo_Get_talk_actor() == actorx || mDemo_Check(mDemo_TYPE_SPEAK, actorx) == TRUE || mDemo_Check(mDemo_TYPE_TALK, actorx) == TRUE" in rt)
ck("L applied only in the idle actions and never in a local talk", "int apply = !local_talk && aNSC_pc_idle_action(shop_common->action);" in rt and "&& apply)" in rt)
ia = body(sc, "static int aNSC_pc_idle_action(")
ck("L idle actions = WAIT, 0x3e, WALK_PL_OTHER_ZONE, 0x40, 0x41, TURN",
   all(x in ia for x in ("aNSC_ACTION_WAIT", "aNSC_ACTION_0x3e_WAIT", "aNSC_ACTION_WALK_PL_OTHER_ZONE", "aNSC_ACTION_0x40_WAIT", "aNSC_ACTION_0x41_WALK_PL_OTHER_ZONE", "aNSC_ACTION_TURN"))
   and "SELL" not in ia and "BUY" not in ia)

# ---- K
ck("K both aNSC_set_zone_data variants retarget and take the target's zone", sc.count("aNSC_pc_retarget(shop_common); /* SHOP KEEPER TARGET SYNC") == 2
   and "shop_common->player_zone = aNSC_get_zone(aNSC_pc_tgt_of(shop_common)->pos);" in sc and "shop_common->player_zone = aNSC_get_zone(shop_common, aNSC_pc_tgt_of(shop_common)->pos);" in sc)
pa = body(sc, "static void aNSC_set_player_angl(")
ck("K aNSC_set_player_angl aims at the target (TARGET_PC)", "#ifdef TARGET_PC" in pa and "atans_table(t->pos.z - p->z, t->pos.x - p->x)" in pa)
ck("K the idle decision measures the target's distance and safe zone (both variants)",
   sc.count("aNSC_PC_PLAYER_DIST(shop_common)") == 3 and "aNSC_check_safe_zone(shop_common, aNSC_PC_ZONE_PLAYER(shop_common, player))" in sc
   and "aNSC_PC_ZONE_PLAYER(shop_common, player)->world.position.z < 120.0f" in sc)
ck("K non-PC builds: the macros fall back to vanilla", "#define aNSC_PC_PLAYER_DIST(sc) ((sc)->npc_class.actor_class.player_distance_xz)" in sc and "#define aNSC_PC_ZONE_PLAYER(sc, pl) (pl)" in sc)
for f in ("ac_npc_shop_master_move.c_inc", "ac_npc_conv_master_move.c_inc", "ac_npc_super_master_move.c_inc", "ac_npc_depart_master_move.c_inc", "ac_npc_mamedanuki_move.c_inc"):
    t = rd(ROOT, "src", "actor", "npc", f)
    ck("K %s includes the common file and calls set_zone_data then set_player_angl" % f,
       '#include "../src/actor/npc/ac_npc_shop_common.c"' in t and re.search(r"aNSC_set_zone_data\(\w+, [^;]*\);\s*\n\s*aNSC_set_player_angl\(", t))

# ---- H
ck("H head: a synced keeper looks at the remote target or keeps the local look; the generic look-at is skipped",
   "pc_sk = pc_net_game_shop_keeper_head(nactorx, &sx, &sy, &sz);" in hd and "aNPC_HEAD_TARGET_POS, NULL, &at" in hd and "if (pc_sk == 0 && nactorx->palActor == NULL && prio == 1) { /* Patch 8:" in hd)
ck("H the head answer is fresh only (250 ms) and set only when applied", "(uint32_t)(now - s_skt[i].ms) < 250u" in blk and "if (apply) {" in tgt)

# ---- S
en = body(ng, "int pc_net_game_shop_nook_sync_enabled(")
ck("S AC_MP_SHOP_NOOK_SYNC (0 = off, ON by default) && g_pc_settings.shop_nook_sync", 'getenv("AC_MP_SHOP_NOOK_SYNC")' in en and "e[0] == '0') ? 0 : 1" in en
   and "g_pc_settings.shop_nook_sync" in en and "!pc_net_game_shop_nook_sync_enabled()" in tgt)
ck("S settings.ini [Gameplay] shop_nook_sync: default 1, parsed, written, in the template",
   ".shop_nook_sync = 1," in st and 'strcmp(key, "shop_nook_sync") == 0' in st and 'fprintf(f, "shop_nook_sync = %d\\n", g_pc_settings.shop_nook_sync);' in st
   and '"shop_nook_sync = 1\\n"' in st and "int shop_nook_sync;" in sth)
ck("S public API declared", all(x in gh for x in ("int pc_net_game_shop_keeper_target(", "int pc_net_game_shop_keeper_head(", "int pc_net_game_shop_nook_sync_enabled(void);")))

# ---- T
th = body(ng, "static void pcnetgame_skt_test_hook(")
ck("T the TEST-ONLY hook is compiled only with PC_NET_TEST_HOOKS and reads AC_TEST_NOOK_SYNC through pc_test_hook_getenv",
   'pc_test_hook_getenv("AC_TEST_NOOK_SYNC")' in th and "#ifdef PC_NET_TEST_HOOKS\n    pcnetgame_skt_test_hook(" in tgt
   and blk.index("#ifdef PC_NET_TEST_HOOKS\n/* TEST-ONLY") < blk.index("static void pcnetgame_skt_test_hook("))

# ---- N
src = os.path.join(HERE, "shop_keeper_target_selftest.c")
gcc = None
for cand in ("gcc", r"C:\msys64\ucrt64\bin\gcc.exe"):
    try:
        subprocess.run([cand, "--version"], capture_output=True, check=True, timeout=30)
        gcc = cand
        break
    except (OSError, subprocess.SubprocessError):
        continue
ck("N native selftest present and includes the real header", os.path.isfile(src) and '#include "pc_shop_keeper_target.h"' in open(src).read())
if gcc is None:
    print("SKIPPED - gcc not found: native selftest not run")
else:
    out = os.path.join(os.environ.get("TEMP", HERE), "skt_selftest_src_audit.exe")
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + r"C:\msys64\ucrt64\bin"
    cp = subprocess.run([gcc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Iinclude", "tools/net_spike/shop_keeper_target_selftest.c", "-o", out], cwd=PC,
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
