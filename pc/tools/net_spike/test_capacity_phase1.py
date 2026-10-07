#!/usr/bin/env python3
"""test_capacity_phase1.py - player-capacity project, PHASE 1 (decouple capacity constants, instrumentation). Focused, no game process.

 1. The REAL spawn-offset code is cut out of pc/src/pc_remote_player.c and compiled natively: ids 0..8 are exactly the historical table, ids past it
    (up to 4096) are finite and pairwise at least 30 world units apart (no two puppets spawn on top of each other).
 2. The coupling asserts exist (host wire id 8 independent of the peer table, peer_mask <= 16 peers, puppet fx ids <= 17 slots) and the host id value is
    unchanged vs the parent commit (wire compatible).
 3. The dedicated status line reports peers / capacity / transport counters and the accessor exists.
 4. PC_NET_MAX_PEERS is still 8 and the 4-guest admission default is untouched (Phase 1 changes no admission behaviour).
NOT covered: any runtime behaviour (the game / Winsock transport only build on the Windows toolchain).
Exit code 0 when all checks pass."""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
CC = os.environ.get("CC", "gcc")
PARENT = "61f392f"
results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("PASS - " if ok else "FAIL - ") + name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def parent(rel):
    return subprocess.run(["git", "-C", ROOT, "show", PARENT + ":" + rel], capture_output=True, check=True).stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def main():
    rp = read("pc/src/pc_remote_player.c")
    i = rp.index("static const PCRemotePlayerOffset s_offset_table[] = {")
    j = rp.index("/* One accepted movement sample")
    code = rp[i:j]
    harness = """#include <math.h>
#include <stdio.h>
typedef float f32;
typedef struct PCRemotePlayerOffset { f32 x, z; } PCRemotePlayerOffset;
""" + code + """
int main(void) {
    static const float exp9[9][2] = {{60,0},{-60,0},{0,60},{0,-60},{60,60},{-60,60},{60,-60},{-60,-60},{0,100}};
    int i, j, bad = 0, n = 4096;
    static PCRemotePlayerOffset o[4096];
    for (i = 0; i < n; i++) o[i] = pc_remote_spawn_offset(i);
    for (i = 0; i < 9; i++) if (o[i].x != exp9[i][0] || o[i].z != exp9[i][1]) bad |= 1;
    for (i = 0; i < n; i++) if (!isfinite(o[i].x) || !isfinite(o[i].z)) bad |= 2;
    for (i = 9; i < n; i++) for (j = 0; j < i; j++) { float dx = o[i].x - o[j].x, dz = o[i].z - o[j].z; if (dx*dx + dz*dz < 30.0f*30.0f) { bad |= 4; } }
    printf("table_n=%d bad=%d\\n", PC_REMOTE_OFFSET_TABLE_N, bad);
    return bad;
}
"""
    d = tempfile.mkdtemp(prefix="acmp_cap1_")
    src = os.path.join(d, "off.c")
    open(src, "w").write(harness)
    exe = os.path.join(d, "off")
    cp = subprocess.run([CC, "-std=gnu11", "-Wall", "-Wextra", "-O1", src, "-o", exe, "-lm"], capture_output=True, text=True)
    check("compile the real spawn-offset code natively (warning-free)", cp.returncode == 0 and cp.stderr.strip() == "")
    if cp.returncode == 0:
        r = subprocess.run([exe], capture_output=True, text=True)
        check("spawn offsets: ids 0..8 unchanged, 0..4095 finite and pairwise >= 30 apart (%s)" % r.stdout.strip(), r.returncode == 0)
    old_tbl = re.search(r"s_offset_table\[PC_REMOTE_PLAYER_SLOT_COUNT\] = \{(.*?)\};", parent("pc/src/pc_remote_player.c"), re.S).group(1)
    new_tbl = re.search(r"s_offset_table\[\] = \{(.*?)\};", rp, re.S).group(1)
    check("spawn offsets: the 9 literals are byte-identical to the parent commit", old_tbl == new_tbl)
    check("spawn offsets: the puppet creation uses the function, not a direct table index", "s_offset_table[i]" not in rp and "pc_remote_spawn_offset(i)" in rp)

    h = read("pc/include/pc_net_game.h")
    check("host wire id: own constant == 8, PLAYER_ID defined from it", "#define PC_NETGAME_HOST_WIRE_ID 8" in h and "((PCNetPlayerId)PC_NETGAME_HOST_WIRE_ID)" in h)
    check("host wire id: peer table may not outgrow it (static assert)", "PC_NET_MAX_PEERS <= PC_NETGAME_HOST_WIRE_ID" in h)
    check("host wire id value unchanged vs parent (8 == old PC_NET_MAX_PEERS)", "PC_NETGAME_HOST_PLAYER_ID ((PCNetPlayerId)PC_NET_MAX_PEERS)" in parent("pc/include/pc_net_game.h") and "#define PC_NET_MAX_PEERS   8" in read("pc/include/pc_net.h"))
    ng = read("pc/src/pc_net_game.c")
    check("peer_mask coupling asserted", "PC_NET_MAX_PEERS <= 16, \"PCNetGameNpcTalkHold.peer_mask is 16 bits" in ng)
    check("puppet fx id coupling asserted", "PC_REMOTE_PLAYER_SLOT_COUNT <= 0x11" in rp)

    ded = read("pc/src/pc_dedicated.c")
    check("status: capacity + transport counters printed", "capacity: transport slots" in ded and "pc_net_get_stats(&ns)" in ded and "pc_net_game_dedicated_capacity(" in ded)
    check("status accessor declared and defined (HOST only)", "int  pc_net_game_dedicated_capacity(" in read("pc/include/pc_dedicated.h") and "int pc_net_game_dedicated_capacity(" in ng)
    check("Phase 1 leaves admission untouched: max_guests default 4 / clamp 1..8 / reserve rule", ".max_guests = 4" in read("pc/src/pc_settings.c") and "occupied + reserve > PC_NET_MAX_PEERS" in ng)

    bad = [n for n, ok in results if not ok]
    print("test_capacity_phase1: %d checks, %d failed" % (len(results), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
