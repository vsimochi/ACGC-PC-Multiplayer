"""Source + pure-python model test: player<->puppet passability and HOST puppet->villager push.

Model mirrors src/game/m_collision_obj.c CollisionCheck_setOC_HitInfo / CollisionCheck_Check2ClObjNoOC (re-read from the
current source below so a change there fails this test).
"""
import math, re, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
def rd(p): return (ROOT / p).read_text(encoding="utf-8", errors="replace")
fails = []
def check(c, m):
    if not c: fails.append(m)

HEAVY, IMMOVABLE = 0xFE, 0xFF
def get_type(w): return "IMM" if w == IMMOVABLE else ("HEAVY" if w == HEAVY else "NORMAL")

def oc_split(w1, w2):
    """(coldis1, coldis2) exactly as CollisionCheck_setOC_HitInfo (no priority flags)."""
    t1, t2 = get_type(w1), get_type(w2)
    a, b = float(w1), float(w2)
    if a + b == 0: a = b = 1.0
    m = 1.0 / (a + b)
    if t1 == "IMM":
        return None if t2 == "IMM" else (0.0, 1.0)
    if t1 == "HEAVY":
        return {"IMM": (1.0, 0.0), "HEAVY": (0.5, 0.5)}.get(t2, (0.0, 1.0))
    return (b * m, a * m) if t2 == "NORMAL" else (1.0, 0.0)

def pairs(f0a, f1a, f0b, f1b):
    """Check2ClObjNoOC == 0 (i.e. the pair IS processed)."""
    G = 0x38
    return bool(f0a & f1b & G) and bool(f1a & f0b & G)

# ---- current source facts ----------------------------------------------------------------------------------------
oc = rd("src/game/m_collision_obj.c")
check("coldis1 = weight2 * minweight;" in oc and "coldis2 = weight1 * minweight;" in oc, "OC normal/normal split unchanged")
check("col1->collision_flags0 & col2->collision_flags1 & ClObj_GROUP_ALL" in oc, "OC group pairing unchanged")
rp = rd("pc/src/pc_remote_player.c")
m = re.search(r"s_remote_player_pipe_data = \{\s*\{ (0x\w+), (0x\w+),", rp)
pf0, pf1 = int(m.group(1), 16), int(m.group(2), 16)
pw = int(re.search(r"#define PC_REMOTE_PLAYER_PUSH_WEIGHT (\d+)", rp).group(1))
pr = int(re.search(r"s_remote_player_pipe_data = \{.*?\{ \{ (\d+), (\d+),", rp, re.S).group(1))
pl = rd("src/game/m_player_common.c_inc")
m = re.search(r"Player_actor_OcInfoData_forStand = \{\s*\{ (0x\w+), (0x\w+),.*?\{ \{ (\d+),", pl, re.S)
lf0, lf1, lr = int(m.group(1), 16), int(m.group(2), 16), int(m.group(3))
nd = rd("src/actor/npc/ac_npc_data.c_inc")
m = re.search(r"aNPC_CoInfoData = \{\s*\{ (0x\w+), (0x\w+),", nd)
nf0, nf1 = int(m.group(1), 16), int(m.group(2), 16)
nw = int(re.search(r"aNPC_StatusData = \{\s*\d+,\s*\d+,\s*\d+,\s*\d+,\s*(\d+)", nd).group(1))

# ---- (1) passability ------------------------------------------------------------------------------------------------
check(pairs(lf0, lf1, pf0, pf1), "local player still pairs with puppet (collision not disabled)")
check(not pairs(pf0, pf1, pf0, pf1), "puppet<->puppet never pairs")
check(not pairs(pf0, pf1, nf0, nf1), "puppet OC pipe does NOT pair with NPCs (push is the separate host path)")
frac_player = oc_split(50, pw)[0]           # moving local player (weight 50) vs puppet
check(abs(frac_player - pw / (50.0 + pw)) < 1e-9 and frac_player < 0.08, "moving player takes < 8%% of overlap (%.3f)" % frac_player)
check(oc_split(HEAVY, pw)[0] == 0.0 and oc_split(IMMOVABLE, pw)[0] == 0.0, "idle/immovable player not pushed by puppet")
# walk straight into a standing puppet: player passes if the equilibrium overlap v/frac exceeds the radius sum
rsum = lr + pr
def walk_through(v, frac, frames=400):
    x = -rsum - 5.0
    for _ in range(frames):
        x += v
        d = abs(x)
        if d < rsum:
            x += math.copysign(frac * (rsum - d), x if x != 0 else 1.0)
        if x > rsum: return True
    return False
vmin = rsum * frac_player
check(walk_through(1.6, frac_player) and walk_through(3.0, frac_player), "walking (>=1.6 u/frame) passes through a puppet")
print("model: player push fraction %.4f, head-on pass-through speed threshold %.2f u/frame (tangential always slides)" % (frac_player, vmin))
# max per-frame push is frac*rsum (< BG radius 18), applied BEFORE the player's BG check (Movement -> BGcheck order)
check(frac_player * rsum < 18.0, "max puppet push per frame below the BG check radius")
walk = rd("src/game/m_player_main_walk.c_inc")
check(walk.find("Player_actor_Movement_Walk(actor, game") < walk.find("Player_actor_BGcheck_Walk(actor);", walk.find("Player_actor_Movement_Walk(actor, game")),
      "player BG check runs after the collision_vec is applied")

# ---- (2) host puppet -> villager push ---------------------------------------------------------------------------
fn = re.search(r"int pc_remote_player_host_npc_push\(.*?\n\}\n", rp, re.S)
check(fn is not None, "pc_remote_player_host_npc_push exists")
body = fn.group(0) if fn else ""
check("collider_table" not in body and "CollisionCheck_setOC" not in body, "push never touches the OC collider table")
check("collided_actor" not in body, "push does not set collided_actor")
check("50.0f / ((float)w + 50.0f)" in body and "MASSTYPE_HEAVY" in body, "vanilla NORMAL split, HEAVY/IMMOVABLE NPC not pushed")
check("pc_remote_player_scene_is_local_field" in body and "pc_remote_player_body_hidden" in body, "only visible field puppets")
mv = rd("src/actor/npc/ac_npc_move.c_inc")
hp = re.search(r"static void aNPC_pc_host_puppet_push\(.*?\n\}\n", mv, re.S)
check(hp is not None, "aNPC_pc_host_puppet_push exists")
hb = hp.group(0) if hp else ""
check(hb.find("pc_net_game_role() != PC_NETGAME_ROLE_HOST") >= 0 and hb.find("PC_NETGAME_ROLE_HOST") < hb.find("pc_remote_player_host_npc_push(game"),
      "host-role gate precedes any work (single-player no-op)")
check("talk_condition != aNPC_TALK_TYPE_NONE" in hb and "aNPC_ACT_TALK" in hb, "talking villager not pushed")
sb = re.search(r"static void aNPC_actor_move_show_before\(.*?\n\}\n", mv, re.S).group(0)
i_held = sb.find("aNPC_pc_host_held_position_move(actorx)")
i_push = sb.find("aNPC_pc_host_puppet_push(nactorx, game)")
i_bg = sb.find("aNPC_BGcheck(nactorx, actorx)")
check(0 <= i_held < i_push < sb.find("aNPC_position_move(actorx);", i_push) < i_bg, "push only in the non-consumer non-held branch, before position_move and BG check")
held = re.search(r"static void aNPC_pc_host_held_position_move\(.*?aNPC_position_move\(actorx\);", mv, re.S).group(0)
check("collision_vec.x = 0.0f" in held and "collision_vec.z = 0.0f" in held, "held (client-talk) villager drops all OC push")
# model: client player (puppet on host) walks into a weight-50 villager at v: villager gets 50% of overlap -> pushed ahead
frac_npc = oc_split(50, nw)[1]
check(abs(frac_npc - 0.5) < 1e-9, "villager takes 50%% like vs the local player")
x_npc, x_p, v = 0.0, -60.0, 1.5
nr = 20
for _ in range(200):
    x_p += v
    d = x_npc - x_p
    if d < nr + 20:
        x_npc += frac_npc * (nr + 20 - d)
check(x_npc > 200 and abs((nr + 20) - (x_npc - x_p) - v * (1 - frac_npc) / frac_npc) < 0.5, "model: walking puppet pushes the villager ahead (moved %.1f)" % x_npc)
check(oc_split(50, HEAVY)[1] == 0.0, "HEAVY (e.g. greeting) villager not pushed")

# ---- (3) client stale fallback: a villager the host does not simulate runs the local AI (pushable) ------------------
cc = re.search(r"static int aNPC_pc_is_client_consumer\(.*?\n\}\n", mv, re.S).group(0)
i_auth = cc.find("pc_net_game_world_is_host_authoritative()")
i_idx = cc.find("aNPC_get_animal_idx(nactorx) < 0")
i_fb = cc.find("aNPC_pc_stale_fallback_local(nactorx)")
i_lease = cc.find("aNPC_pc_talk_lease_active(nactorx)")
check(0 <= i_auth < i_idx < i_fb < i_lease, "fallback only for READY-client regular villagers (event NPCs excluded first), evaluated before the talk lease")
check(re.search(r"aNPC_pc_stale_fallback_local\(nactorx\)\) \{[^}]*return FALSE;", cc, re.S) is not None, "fallback -> not a consumer")
fb = re.search(r"static int aNPC_pc_stale_fallback_local\(.*?\n\}\n", mv, re.S)
check(fb is not None, "aNPC_pc_stale_fallback_local exists")
fbb = fb.group(0) if fb else ""
check("hide_flg != FALSE" in fbb and "pc_net_game_npc_talk_busy(slot, npc_id)" in fbb, "must_hold: hidden villager / another player's conversation")
check("pc_net_game_npc_client_local_ai(slot, npc_id, actor_new, must_hold)" in fbb and "frame_counter" in fbb, "evaluated once per frame, actor (re)creation detected")
# the non-consumer path IS the vanilla path: position integration + host-side push + schedule/action/angle
check(sb.find("if (aNPC_pc_is_client_consumer(nactorx))") < i_held and "aNPC_position_move(actorx);" in sb[i_push:], "fallback (non-consumer) path calls aNPC_position_move")
ms = re.search(r"static void aNPC_actor_move_show\(.*?\n\}\n", mv, re.S).group(0)
check("if (!pc_client_consumer && !pc_talk_held)" in ms and ms.find("aNPC_schedule_proc(nactorx, play);") > 0 and "aNPC_angle_calc(nactorx);" in ms,
      "fallback runs schedule/action/angle")
check(ms.find("if (!pc_client_consumer) {") < ms.find("aNPC_pc_host_sample_move(nactorx);"), "fallback samples its pose (client: relay to the host)")
ng = rd("pc/src/pc_net_game.c")
la = re.search(r"int pc_net_game_npc_client_local_ai\(.*?\n\}\n", ng, re.S)
check(la is not None, "pc_net_game_npc_client_local_ai exists")
lab = la.group(0) if la else ""
check("s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY" in lab and "pcnetgame_npc_fallback_on()" in lab, "client READY only + kill switch")
check("host silent for %u ms -> local AI" in lab and "-> host (after %u ms local)" in lab, "one log line per transition")
check("snapshot_count = 0" in lab, "a stale ring is cleared when the actor is (re)created")
hc = re.search(r"static void pcnetgame_handle_client_npc_move\([^;{]*\{.*?\n\}\n", ng, re.S).group(0)
check("pc_npc_auth_on_sample(&s_npc_auth[in->slot]" in hc, "every accepted NPC_MOVE refreshes the slot (fresh stream keeps consumer mode)")
au = rd("pc/include/pc_npc_authority.h")
for k, v in (("PC_NPC_AUTH_STALE_MS", 2500), ("PC_NPC_AUTH_GRACE_MS", 4000), ("PC_NPC_AUTH_FRESH_SAMPLES", 5), ("PC_NPC_TALK_HOLD_CAP_MS", 120000)):
    check(re.search(r"#define %s %du" % (k, v), au) is not None, "%s == %d" % (k, v))
hp2 = re.search(r"static void pcnetgame_handle_host_npc_proxy_move\([^;{]*\{.*?\n\}\n", ng, re.S)
check(hp2 is not None, "host relay handler exists")
hpb = hp2.group(0) if hp2 else ""
check("PC_NPC_HOST_ACTOR_ALIVE_MS" in hpb and "pc_npc_proxy_offer(" in hpb and "i != (int)peer" in hpb, "relay: only without a host actor, one owner, never echoed to the owner")
check("npc_id does not match the host animal table" in hpb and "not in the town field" in hpb, "relay validated")
sw = re.search(r"static void pcnetgame_host_talk_hold_sweep\([^;{]*\{.*?\n\}\n", ng, re.S).group(0)
check("pc_npc_talk_hold_capped(h->begin_ms, now, pcnetgame_talk_hold_cap_ms())" in sw and "pc_peer_set_add(&h->capped, p)" in sw, "hold cap releases and blocks keepalives")
nt = re.search(r"static void pcnetgame_handle_host_npc_talk\([^;{]*\{.*?\n\}\n", ng, re.S).group(0)
check(nt.find("pc_peer_set_has(&h->capped, peer)") >= 0 and "refused until its END" in nt, "capped peer's keepalive BEGIN refused")
ma = rd("src/game/m_actor.c")
check("actor->part == ACTOR_PART_NPC && pc_host_observer_active()" in ma and "Actor_pc_observer_npc_player_dist(actor);" in ma, "observer: NPC player distance to the nearest real player")
tk = rd("src/actor/npc/ac_npc_talk.c_inc")
check("mDemo_IS_ACTOR_TALKING((ACTOR*)a)" in tk and "return 2;" in tk, "keepalive probe distinguishes a visible conversation from a stuck talk state")

if fails:
    print("FAIL: " + " | ".join(fails)); sys.exit(1)
print("PASS")
