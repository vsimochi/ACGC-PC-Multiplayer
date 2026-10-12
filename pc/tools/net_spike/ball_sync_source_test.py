"""ball_sync_source_test.py - source/model test of the host-authoritative ball wiring (worker 2, workstream C).

Checks (source only, no game process):
  1. enum ids 79/80 are BALL_STATE / BALL_KICK_REQUEST, unique in PCNetGameMsgType;
  2. wire structs carry _Static_assert sizes 32 / 24 and dispatch is exact size + id, in the right direction only
     (host dispatches only KICK_REQUEST, client only STATE);
  3. every multiplayer branch in ac_ball.c is inside #ifdef TARGET_PC and gated on pc_net_game_ball_mode()
     (single player: VANILLA => byte-identical behaviour); the kick impulse is lifted into aBALL_apply_kick unchanged;
  4. the host tick / cleanup hooks are present; the client never sends BALL_STATE; the host never sends KICK_REQUEST.
Run: python pc/tools/net_spike/ball_sync_source_test.py
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
NG = open(os.path.join(ROOT, "pc", "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read()
BALL = open(os.path.join(ROOT, "src", "actor", "ac_ball.c"), encoding="utf-8", errors="replace").read()
HDR = open(os.path.join(ROOT, "pc", "include", "pc_ball_authority.h"), encoding="utf-8", errors="replace").read()

fails = []


def check(cond, what):
    if not cond:
        fails.append(what)


# 1. enum ids
enum = NG[NG.index("PC_NETGAME_MSG_HELLO") if "PC_NETGAME_MSG_HELLO" in NG else 0: NG.index("} PCNetGameMsgType;")]
enum = enum[enum.rfind("typedef enum"):] if "typedef enum" in enum else enum
ids = re.findall(r"PC_NETGAME_MSG_(\w+)\s*=\s*(\d+)", enum)
by_id = {}
for name, v in ids:
    by_id.setdefault(int(v), []).append(name)
check(by_id.get(79) == ["BALL_STATE"], "id 79 must be BALL_STATE only: %r" % by_id.get(79))
check(by_id.get(80) == ["BALL_KICK_REQUEST"], "id 80 must be BALL_KICK_REQUEST only: %r" % by_id.get(80))

# 2. wire structs + dispatch
check(re.search(r"_Static_assert\(sizeof\(PCBallStateMsg\) == 32", HDR) is not None, "PCBallStateMsg static assert 32")
check(re.search(r"_Static_assert\(sizeof\(PCBallKickMsg\) == 24", HDR) is not None, "PCBallKickMsg static assert 24")
kick_disp = [m.start() for m in re.finditer(r"size == sizeof\(PCBallKickMsg\) && data\[0\] == \(uint8_t\)PC_NETGAME_MSG_BALL_KICK_REQUEST", NG)]
state_disp = [m.start() for m in re.finditer(r"size == sizeof\(PCBallStateMsg\) && data\[0\] == \(uint8_t\)PC_NETGAME_MSG_BALL_STATE", NG)]
check(len(kick_disp) == 1 and len(state_disp) == 1, "exactly one exact-size dispatch each (kick %d, state %d)" % (len(kick_disp), len(state_disp)))
host_fn = NG.index("static void pcnetgame_handle_host_data(")
client_fn = NG.index("static void pcnetgame_handle_client_data(")


# direction: the kick dispatch belongs to the host handler, the state dispatch to the client handler
def owner(pos):
    starts = sorted([(host_fn, "host"), (client_fn, "client")])
    best = None
    for s, n in starts:
        if s < pos:
            best = (s, n)
    # make sure no other top-level function definition sits between the handler start and pos
    if best is None:
        return None
    between = NG[best[0] + 10:pos]
    if re.search(r"\n(static )?[a-zA-Z_][\w\s\*]*\([^;{]*\)\s*\{\n", between):
        return "other"
    return best[1]


check(kick_disp and owner(kick_disp[0]) == "host", "kick dispatch must be in pcnetgame_handle_host_data (got %r)" % (kick_disp and owner(kick_disp[0])))
check(state_disp and owner(state_disp[0]) == "client", "state dispatch must be in pcnetgame_handle_client_data (got %r)" % (state_disp and owner(state_disp[0])))
blk = NG[NG.index("worker-2 block"):NG.index("/* ---- Tree-drop falling-item animation")]
check("PC_NET_UNRELIABLE, &m, (uint16_t)sizeof(m)" in blk and "pc_net_send(0, PC_NET_RELIABLE, &m" in blk, "state unreliable / kick reliable")
check("s_host_peer_link[peer] != PC_NETGAME_LINK_READY" in blk and "pcnetgame_ball_peer_in_town" in blk, "host kick: READY + town scene gate")
check("s_client_link != PC_NETGAME_LINK_READY" in blk, "client state: READY gate")
check("pc_net_game_ball_client_kick" in blk and "pc_net_game_ball_mode() != PC_BALL_MODE_CLIENT" in blk, "client kick only in client mode")
check(re.search(r"pcnetgame_host_world_poll\(\);\s*\n\s*pcnetgame_ball_host_tick\(\);", NG) is not None, "host tick hook")
check("pc_net_game_ball_peer_gone((int)peer);" in NG, "peer cleanup hook")

# 3. ac_ball.c gating
pc_blocks = re.findall(r"#ifdef TARGET_PC\n(.*?)#endif", BALL, re.S)
outside = BALL
for b in pc_blocks:
    outside = outside.replace(b, "")
for sym in ("pc_net_game_ball_", "s_pc_ball_mode", "PC_BALL_"):
    check(sym not in outside, "%s used outside #ifdef TARGET_PC in ac_ball.c" % sym)
check("s_pc_ball_mode = pc_net_game_ball_mode();" in BALL, "ct decides the mode")
check(re.search(r"if \(actor->collider != collided\) \{\s*actor->collider = collided;\s*aBALL_apply_kick\(actor, &collided->world.position, &collided->position_speed\);", BALL) is not None,
      "vanilla OBJcheck calls the lifted impulse with the collider")
check(BALL.count("fqrand2()") == 1, "impulse not duplicated")
check("pc_net_send" not in BALL and "PCBallStateMsg" not in BALL and "PCBallKickMsg" not in BALL, "ac_ball never builds wire messages itself")

if fails:
    for f in fails:
        print("FAIL:", f)
    print("ball_sync_source_test: %d FAILURES" % len(fails))
    sys.exit(1)
print("ball_sync_source_test: ALL PASS")
