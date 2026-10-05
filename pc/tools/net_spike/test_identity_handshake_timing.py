#!/usr/bin/env python3
"""test_identity_handshake_timing.py - regression test: a normal client must NOT send its identity (and a host must not latch its own player) while the
title -> Start Game -> player-select flow is still running.

ROOT CAUSE (see the comment above pcnetgame_scene_is_pregame in pc_net_game.c): the title's Start handler (title_action_data_init_start_select,
src/actor/ac_animal_logo_misc.c) binds a PLACEHOLDER player (Now_Private = private_data[0], player_no 0) BEFORE the human has chosen a character, and the
save re-read leaves Save scene_no at the saved scene (the town), so pcfa_save_ready() was already true while the title-demo GAME_PLAY was still running.
pcnetgame_update_local_world_ready() latched there and the client sent Resident 0 as its IDENTITY; a second client therefore collided with a connected
Resident 0 ("claimed resident is already connected on another peer") before it could choose Resident 1.

TIERS: (1) NATIVE: the EXACT body of pcnetgame_scene_is_pregame is extracted from pc_net_game.c and compiled against the real scene enum
(include/m_scene_table.h): its truth table must be true for exactly the 8 pre-game scenes and false for every other scene (incl. the town, shops, rooms,
museum);
(2) SOURCE AUDIT of the wiring: the latch acquisition (pcnetgame_update_local_world_ready) requires a running GAME_PLAY whose scene is NOT pre-game; the
client identity send is still gated by that latch; the HOST duplicate-resident / identity validation (pcnetgame_host_process_identity and the helpers
that refuse "claimed resident is already connected") and the guest admission check are byte-identical to the commit before this fix; the wire headers are
untouched.
NOT covered (needs UI automation): the real title-menu Start Game click; the real-client regression runs (resident bootstrap, guest, dedicated) are
separate tests.
Usage: python test_identity_handshake_timing.py"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"
BASELINE = "f3d69ef"  # HEAD when this fix started (the adoption-clock fix)


def main():
    results = []

    def ck(d, c):
        results.append((d, bool(c)))
        print(("PASS - " if c else "FAIL - ") + d)

    src = open(os.path.join(PC, "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    base = subprocess.run(["git", "-C", ROOT, "show", BASELINE + ":pc/src/pc_net_game.c"], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")

    def func(s, name):
        m = re.search(r"^[^\n;]*\b%s\([^;{]*\)\s*\{\n" % re.escape(name), s, re.M)
        if not m:
            return ""
        j = s.find("\n}\n", m.end())
        return s[m.start():j + 3]

    # ---- native: the exact function against the real scene enum
    pre = func(src, "pcnetgame_scene_is_pregame")
    ck("the pre-game predicate exists", pre != "")
    hdr = open(os.path.join(ROOT, "include", "m_scene_table.h"), encoding="utf-8", errors="replace").read()
    e0 = hdr.index("enum scene_table {")
    body = hdr[e0:hdr.index("};", e0)]
    names = re.findall(r"^\s*(SCENE_[A-Z0-9_]+)\s*,", body, re.M)
    enum_txt = "enum scene_table_copy {" + "".join(" %s," % n for n in names) + " SCENE_NUM };"
    ck("the scene enum parsed from the real header has the expected shape (%d scenes, FG = 7, PLAYERSELECT_2 = 27, TITLE_DEMO = 33)" % len(names),
       len(names) > 40 and names[7] == "SCENE_FG" and names[27] == "SCENE_PLAYERSELECT_2" and names[33] == "SCENE_TITLE_DEMO")
    harness = """#include <stdio.h>
%s
%s
int main(void) {
    int sc, bad = 0, n_true = 0;
    for (sc = 0; sc < SCENE_NUM; sc++) {
        int want = (sc == SCENE_TITLE_DEMO || sc == SCENE_PLAYERSELECT || sc == SCENE_PLAYERSELECT_2 || sc == SCENE_PLAYERSELECT_3 ||
                    sc == SCENE_PLAYERSELECT_SAVE || sc == SCENE_START_DEMO || sc == SCENE_START_DEMO2 || sc == SCENE_START_DEMO3);
        int got = pcnetgame_scene_is_pregame(sc) != 0;
        n_true += got;
        if (got != want) { printf("MISMATCH scene %%d got %%d want %%d" "\\n", sc, got, want); bad++; }
    }
    printf("TOWN=%%d SHOP0=%%d MY_ROOM_S=%%d MUSEUM=%%d" "\\n", pcnetgame_scene_is_pregame(SCENE_FG), pcnetgame_scene_is_pregame(SCENE_SHOP0),
           pcnetgame_scene_is_pregame(SCENE_MY_ROOM_S), pcnetgame_scene_is_pregame(SCENE_MUSEUM_ENTRANCE));
    printf("RESULT scenes=%%d pregame=%%d bad=%%d" "\\n", SCENE_NUM, n_true, bad);
    return bad ? 1 : 0;
}
""" % (enum_txt, pre)
    scratch = tempfile.mkdtemp(prefix="acmp_idtiming_")
    c_path = os.path.join(scratch, "pregame.c")
    open(c_path, "w").write(harness)
    exe = os.path.join(scratch, "pregame.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", c_path, "-o", exe], capture_output=True, text=True, env=env)
    ck("native: the extracted predicate compiles warning-free against the real scene enum", cp.returncode == 0 and "warning" not in cp.stderr)
    if cp.returncode != 0:
        print(cp.stderr[:2000])
    else:
        rp = subprocess.run([exe], capture_output=True, text=True, env=env, timeout=30)
        out = rp.stdout
        ck("native: exactly the 8 pre-game scenes are true, every other scene is false (no mismatch)", rp.returncode == 0 and "RESULT" in out and "pregame=8 bad=0" in out)
        ck("native: the town, a shop, a player room and the museum are NOT pre-game", "TOWN=0 SHOP0=0 MY_ROOM_S=0 MUSEUM=0" in out)

    # ---- source audit
    latch = func(src, "pcnetgame_update_local_world_ready")
    ck("src: the world latch needs a running GAME_PLAY whose scene is not pre-game",
       "gamePT != NULL && gamePT->exec == play_main && !pcnetgame_scene_is_pregame((int)((GAME_PLAY*)gamePT)->scene_id)" in latch
       and "if (!pcfa_save_ready()) {" in latch and "s_local_world_latched = 0;" in latch)
    ck("src: the lifecycle predicate of the adoption watchdog shares the same helper", "return pcnetgame_scene_is_pregame(sc) || pcnetgame_house_client_adopt_wait();" in func(src, "pcnetgame_crec_adopt_lifecycle_now"))
    i0 = src.index("static void pcnetgame_client_tick(void) {" + chr(10))
    tick = src[i0:src.index(chr(10) + "}" + chr(10), i0) + 3]
    ck("src: the client still sends IDENTITY only when the (now stricter) latch is set: `int ready = s_local_world_latched;` + `if (ready)` before the send",
       "int ready = s_local_world_latched;" in tick and tick.index("if (ready) {") < tick.index("pc_net_send(0, PC_NET_RELIABLE, &msg, (uint16_t)sizeof(msg))"))
    users = [m.start() for m in re.finditer(r"s_local_world_latched\s*=\s*1", src)]
    ck("src: the latch is set in exactly one place (the gated one)", len(users) == 1)
    same = {}
    for fn in ("pcnetgame_host_process_identity", "pcnetgame_host_refuse_identity", "pcnetgame_host_guest_check", "pcnetgame_guest_key_conflict",
               "pcnetgame_host_revalidate_bound_peers", "pcnetgame_build_identity_msg", "pcnetgame_capture_local_identity"):
        a, b = func(src, fn), func(base, fn)
        same[fn] = a != "" and a == b
    ck("src: the HOST identity / duplicate-resident validation, guest admission and the client identity builder are byte-identical to the baseline: %s" %
       {k: v for k, v in same.items() if not v}, all(same.values()))
    ck("src: the 'claimed resident is already connected on another peer' refusal is still present (duplicate-resident validation not bypassed)",
       "claimed resident is already connected on another peer" in src)
    ck("wire: no protocol change (wire headers / transport untouched vs the baseline)",
       subprocess.run(["git", "-C", ROOT, "diff", "--quiet", BASELINE, "--", "pc/include/pc_net_game.h", "pc/include/pc_net.h", "pc/src/pc_net.c"]).returncode == 0)

    failed = [d for d, ok in results if not ok]
    print("-" * 60)
    print("%d/%d checks passed" % (len(results) - len(failed), len(results)))
    for d in failed:
        print("FAILED: " + d)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
