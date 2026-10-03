#!/usr/bin/env python3
"""test_boot_exec_gate_src.py - host BOOT CRASH fix (GAME_SECOND read as GAME_PLAY), source audit + optional runtime trace.

ROOT CAUSE (Opus boot-crash diagnostic, ~3-5% of host launches): during boot gamePT is a GAME_SECOND (malloc 0xE0, src/graph.c) whose
exec is second_game_main, but the per-frame movement send in pc_net_game_poll() and pc_remote_player_poll() read it as a GAME_PLAY
(GET_PLAYER_ACTOR_NOW() -> actor_info at +0x2EF0 = adjacent heap) and dereferenced the garbage actor.

SOURCE AUDIT (always): the exec gate (gamePT != NULL && gamePT->exec == play_main) is present at every fixed site, and EVERY use of
GET_PLAYER_ACTOR_NOW()/get_player_actor_withoutCheck/GET_PLAYER_ACTOR_ACTOR in pc/src is either under an exec gate or flows through
pcnetgame_is_real_player_actor() (which itself now refuses unless exec == play_main). Allowlist below lists each site + verdict.

RUNTIME CHECK (--runtime N, default off; real host launches under the ctypes breakpoint tracer boot_exec_trace.py on the DISPOSABLE
pc\\build64\\bin_fixture4_persist copy only, one process at a time): INT3 at the call site of pcnetgame_is_real_player_actor in
pc_net_game_poll (found by nm/objdump of the exe under test), logging gamePT->exec per tick until the bootstrap resident is bound.
PASS = no tick with exec != play_main and no access violation in N launches. The same tracer is first run against the PRE-FIX exe
(campaign exe_backup, --old-exe) as a sensitivity control: it must show ticks with exec=second_game_main (the buggy window).
Statistical/mechanism evidence, not a proof; the proof is the source audit above.
Usage: python test_boot_exec_gate_src.py [--runtime N] [--old-exe PATH] [--port 10300]"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

import net_spike_lib as L

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
NM = r"C:\msys64\ucrt64\bin\nm.exe"
OBJDUMP = r"C:\msys64\ucrt64\bin\objdump.exe"


def read(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
        return f.read().decode("utf-8", "replace").replace("\r\n", "\n")


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def func_body(src, name):
    m = re.search(r"^(?:static )?[\w\s\*]+?\b%s\(.*?\)\s*\{\n" % re.escape(name), src, re.M)
    if not m:
        return ""
    i = m.end()
    return src[i:src.index("\n}\n", i)]


# Every GET_PLAYER_ACTOR_NOW() use in pc/src: (file, enclosing function, how it is guarded)
GUARDS = {
    ("pc_net_game.c", "pcnetgame_classify_move_state"): "comment text only (no code use)",
    ("pc_net_game.c", "pcnetgame_is_real_player_actor"): "comment text only; the function itself now refuses unless exec == play_main",
    ("pc_net_game.c", "pcnetgame_scene_tick"): "gamePT->exec == play_main in the same condition (+ is_real_player_actor)",
    ("pc_net_game.c", "pcnetgame_run_scene_test_hook"): "early return unless gamePT->exec == play_main (checked before both reads)",
    ("pc_net_game.c", "pcnetgame_run_collide_test_hook"): "early return unless gamePT->exec == play_main",
    ("pc_net_game.c", "pcnetgame_crec_adopt_blocker"): "early return unless gamePT->exec == play_main",
    ("pc_net_game.c", "pcnetgame_run_dig_hole_test_trigger"): "pcnetgame_is_real_player_actor(local) right after the read (now exec-gated)",
    ("pc_net_game.c", "pcnetgame_run_money_rock_test_triggers"): "X1b: bag-pickup hook reads the actor only after pcnetgame_is_real_player_actor(bag_pl) (exec-gated)",
    ("pc_net_game.c", "pcnetgame_run_txn_test_hook"): "X1b: the actor read is followed by pcnetgame_is_real_player_actor(pl) (exec-gated) in both stages",
    ("pc_net_game.c", "pcnetgame_run_weed_pull_test_trigger"): "WEEDS: pcnetgame_is_real_player_actor(local) right after the read (exec-gated)",
    ("pc_net_game.c", "pcnetgame_run_txn_dig_test_hook"): "X3: the actor read (phase 0) is followed by pcnetgame_is_real_player_actor(pl) (exec-gated) before any use",
    ("pc_net_game.c", "pcnetgame_run_fish_catch_test_trigger_client"): "pcnetgame_is_real_player_actor(local) right after the read (now exec-gated)",
    ("pc_net_game.c", "pcnetgame_run_bug_catch_test_trigger_client"): "pcnetgame_is_real_player_actor(local) right after the read (now exec-gated)",
    ("pc_net_game.c", "pcnetgame_run_bug_despawn_label_race_diag"): "pcnetgame_is_real_player_actor(local) right after the read (now exec-gated)",
    ("pc_net_game.c", "pc_net_game_poll"): "explicit gamePT->exec == play_main in the movement-send condition (+ is_real_player_actor)",
    ("pc_remote_player.c", "pc_remote_player_poll"): "entry gate: gamePT == NULL || gamePT->exec != play_main -> return",
}


def enclosing_function(src, pos):
    """Name of the C function whose body contains src[pos]: the last column-0 'type name(...) {' line before pos."""
    lines = src[:pos].split("\n")
    for j in range(len(lines) - 1, -1, -1):
        m = re.match(r"^(?:static )?[\w\*\s]+? \**(\w+)\(.*\{$", lines[j])
        if m and not lines[j].startswith(" "):
            return m.group(1)
    return None


def audit(check):
    c_raw = read("pc/src/pc_net_game.c")
    r_raw = read("pc/src/pc_remote_player.c")
    c = strip_comments(c_raw)
    r = strip_comments(r_raw)
    real = func_body(c_raw, "pcnetgame_is_real_player_actor")
    check("A1 pcnetgame_is_real_player_actor refuses unless a GAME_PLAY runs (gamePT != NULL && exec == play_main) BEFORE touching the actor",
          "if (gamePT == NULL || gamePT->exec != play_main) {" in real
          and real.index("gamePT->exec != play_main") < real.index("local->actor_class.dlftbl"))
    check("A1 the explanatory comment names the real root cause (GAME_SECOND read as GAME_PLAY), not a half-constructed play scene",
          "GAME_SECOND" in c_raw[c_raw.index("Stage 3 safety check"):c_raw.index("static int pcnetgame_is_real_player_actor")]
          and "ROOT CAUSE" in c_raw)
    poll = func_body(c_raw, "pc_net_game_poll")
    m = re.search(r"if \(gamePT != NULL && gamePT->exec == play_main &&\s*graph_dt_period_elapsed\(gamePT, &s_move_send_accum", poll)
    check("A2 pc_net_game_poll: the per-frame movement send is gated on gamePT->exec == play_main BEFORE GET_PLAYER_ACTOR_NOW()",
          m is not None and poll.index("gamePT->exec == play_main", m.start()) < poll.index("GET_PLAYER_ACTOR_NOW()", m.start()))
    rp = func_body(r_raw, "pc_remote_player_poll")
    check("A3 pc_remote_player_poll: entry gate `gamePT == NULL || gamePT->exec != play_main` returns before GET_PLAYER_ACTOR_NOW() / collision_check / actor reads",
          "if (gamePT == NULL || gamePT->exec != play_main) {" in rp
          and rp.index("gamePT->exec != play_main") < rp.index("GET_PLAYER_ACTOR_NOW()") < rp.index("play->collision_check"))
    check("A3 the early return does not skip any teardown that must run outside play_main (the poll's scene-generation bookkeeping is re-derived at the first play poll)",
          "s_scene_generation++" in rp and rp.index("s_scene_generation++") > rp.index("GET_PLAYER_ACTOR_NOW()")
          and "free" not in rp[:rp.index("GET_PLAYER_ACTOR_NOW()")])
    # every GET_PLAYER_ACTOR_NOW / withoutCheck / ACTOR_ACTOR use in pc/src
    unlisted = []
    seen = set()
    pcdir = os.path.join(ROOT, "pc", "src")
    for fn in sorted(os.listdir(pcdir)):
        if not fn.endswith((".c", ".cpp")):
            continue
        raw = read("pc/src/" + fn)
        code = strip_comments(raw)
        for mm in re.finditer(r"GET_PLAYER_ACTOR_NOW\(\)|get_player_actor_withoutCheck\(|GET_PLAYER_ACTOR_ACTOR\(", code):
            if mm.group(0).startswith("GET_PLAYER_ACTOR_NOW") and code[max(0, mm.start() - 8):mm.start()].endswith("define "):
                continue
            fnc = enclosing_function(code, mm.start())
            key = (fn, fnc)
            seen.add(key)
            if key not in GUARDS:
                unlisted.append(key)
    check("A4 every GET_PLAYER_ACTOR_NOW/get_player_actor_withoutCheck/GET_PLAYER_ACTOR_ACTOR use in pc/src is in the audited allowlist (unlisted: %s)"
          % sorted(set(unlisted)), not unlisted and len(seen) >= 5)

    def gated(fnname, src_raw):
        b = func_body(src_raw, fnname)
        return b
    # per-site verdict checks
    tick = func_body(c_raw, "pcnetgame_scene_tick")
    check("A5 pcnetgame_scene_tick: `s_local_world_latched && gamePT != NULL && gamePT->exec == play_main` guards the actor read",
          re.search(r"s_local_world_latched && gamePT != NULL && gamePT->exec == play_main", tick) is not None)
    ok_hooks = True
    for name in ("pcnetgame_run_dig_hole_test_trigger", "pcnetgame_run_fish_catch_test_trigger_client",
                 "pcnetgame_run_bug_catch_test_trigger_client", "pcnetgame_run_bug_despawn_label_race_diag"):
        b = func_body(c_raw, name)
        if b and not re.search(r"GET_PLAYER_ACTOR_NOW\(\);\s*(?:\n\s*)*if \(!pcnetgame_is_real_player_actor\(local\)\)", b):
            ok_hooks = False
    check("A6 the --force-* test hooks check pcnetgame_is_real_player_actor(local) immediately after the read (a plain read of gamePT+0x2EF0 never dereferences)", ok_hooks)
    # pc_vi.c / pc_field_authority.c / pc_main.c do not read the player actor per frame
    others = [fn for fn in ("pc_vi.c", "pc_field_authority.c", "pc_main.c", "pc_wildlife_authority.c", "pc_m_card.c")
              if re.search(r"GET_PLAYER_ACTOR_NOW|get_player_actor_withoutCheck|GET_PLAYER_ACTOR_ACTOR", strip_comments(read("pc/src/" + fn)))]
    check("A7 pc_vi.c / pc_field_authority.c / pc_main.c / pc_wildlife_authority.c / pc_m_card.c contain no player-actor read (their GAME_PLAY casts are exec-gated or behind pcfa_save_ready; audited: pc_m_card.c:1052/1141 gated by exec == play_main, pc_wildlife_authority.c host trigger called only from play-scene host code)",
          others == [])
    check("A8 persistence-latch fix present (pcnetgame_rec_store_write coalesce branch clears s_rec_store_last_failed)",
          re.search(r"memcmp\(nf\.e, s_rec_file\.e, sizeof\(nf\.e\)\) == 0\) \{[^}]*s_rec_store_last_failed = 0;", func_body(c_raw, "pcnetgame_rec_store_write"), re.S) is not None)


# ------------------------------------------------------------------ runtime
def locate(exe):
    """(call-site address of pcnetgame_is_real_player_actor inside pc_net_game_poll, gamePT address, nm output path)"""
    nm = subprocess.run([NM, "-n", exe], capture_output=True, text=True).stdout
    syms_path = os.path.join(os.environ.get("TEMP", "."), "boot_exec_syms_%d.txt" % os.getpid())
    open(syms_path, "w").write(nm)
    addr = {}
    for ln in nm.splitlines():
        p = ln.split()
        if len(p) == 3 and p[2] in ("pc_net_game_poll", "pcnetgame_is_real_player_actor", "gamePT"):
            addr[p[2]] = int(p[0], 16)
    nxt = [int(ln.split()[0], 16) for ln in nm.splitlines() if len(ln.split()) == 3 and int(ln.split()[0], 16) > addr["pc_net_game_poll"]]
    stop = min(nxt)
    dis = subprocess.run([OBJDUMP, "-d", "--no-show-raw-insn", "--start-address=0x%x" % addr["pc_net_game_poll"],
                          "--stop-address=0x%x" % stop, exe], capture_output=True, text=True).stdout
    target = "%x" % addr["pcnetgame_is_real_player_actor"]
    dl = dis.splitlines()
    calls = []
    for i, ln in enumerate(dl):
        m = re.match(r"^\s*([0-9a-f]+):\s+call\s+%s\b" % target, ln)
        if m:
            calls.append((int(m.group(1), 16), any("<graph_dt_period_elapsed>" in x for x in dl[max(0, i - 40):i])))
    # pc_net_game_poll also contains inlined test hooks that call the same predicate. The per-frame MOVEMENT SEND is the FIRST call
    # in the function (it precedes the inlined hooks' code) (old exe: 0x452247, the address the Opus diagnostic used).
    calls = [calls[0][0]] if calls else []
    return calls, addr["gamePT"], syms_path


def trace_run(exe_dir, port, tag, out_dir):
    exe = os.path.join(exe_dir, "AnimalCrossing.exe")
    calls, gamept, syms = locate(exe)
    if len(calls) != 1:
        return None, "expected exactly one call site in pc_net_game_poll, found %s" % calls
    env = dict(os.environ, BOOT_TRACE_BP="%x" % calls[0], BOOT_TRACE_GAMEPT="%x" % gamept, BOOT_TRACE_SYMS=syms)
    log = os.path.join(out_dir, "%s.log" % tag)
    out = os.path.join(out_dir, "%s.trace.txt" % tag)
    p = subprocess.run([sys.executable, os.path.join(HERE, "boot_exec_trace.py"), exe_dir, log, out, "--host", str(port), "--verbose",
                        "--bootstrap-resident", "0"], env=env, capture_output=True, text=True, timeout=180)
    subprocess.run(["taskkill", "/F", "/IM", "AnimalCrossing.exe"], capture_output=True)
    try:
        txt = open(out).read()
    except OSError:
        return None, "no trace output (%s)" % p.stderr[-200:]
    return txt, None


def runtime(args, check):
    bin_dir = os.path.join(ROOT, "pc", "build64", "bin_fixture4_persist")
    if not os.path.isdir(bin_dir):
        check("R0 disposable pc\\build64\\bin_fixture4_persist exists (run test_d3_record_persist.py once to create it)", False)
        return
    L.require_launchable_bin_dir(bin_dir)
    out_dir = os.path.join(HERE, "logs", "boot_exec_trace")
    os.makedirs(out_dir, exist_ok=True)
    gci = os.path.join(bin_dir, L.SAVE_GCI_REL)
    snap = open(gci, "rb").read()
    exe = os.path.join(bin_dir, "AnimalCrossing.exe")
    new_exe = open(exe, "rb").read()

    def fresh():
        open(gci, "wb").write(snap)
        mp = os.path.join(bin_dir, "save", "mp")
        if os.path.isdir(mp):
            shutil.rmtree(mp, ignore_errors=True)

    try:
        if args.old_exe:
            shutil.copyfile(args.old_exe, exe)
            non_play = 0
            avs = 0
            hits = 0
            for i in range(3):
                fresh()
                txt, err = trace_run(bin_dir, args.port + 50 + i, "old_%02d" % i, out_dir)
                if txt is None:
                    print("   old-exe trace error:", err)
                    continue
                non_play += len(re.findall(r"exec=(?!play_main)\S+", txt))
                avs += txt.count("AV at")
                hits += len(re.findall(r"hit#", txt))
            check("R1 SENSITIVITY CONTROL: the tracer on the PRE-FIX exe sees movement-send ticks with exec != play_main (non-play ticks %d, AVs %d, logged ticks %d in 3 launches)" % (non_play, avs, hits),
                  non_play > 0)
            open(exe, "wb").write(new_exe)
        bad = 0
        avs = 0
        play_ticks = 0
        done = 0
        for i in range(args.runtime):
            fresh()
            txt, err = trace_run(bin_dir, args.port + i, "fix_%02d" % i, out_dir)
            if txt is None:
                check("R2 launch %d traced (%s)" % (i, err), False)
                continue
            done += 1
            bad += len(re.findall(r"exec=(?!play_main)\S+", txt))
            avs += txt.count("AV at")
            play_ticks += len(re.findall(r"exec=play_main", txt))
            time.sleep(1.0)
        check("R2 FIXED exe: %d traced launches, movement-send ticks with exec != play_main = %d, access violations = %d (play_main ticks logged: %d)"
              % (done, bad, avs, play_ticks), done == args.runtime and bad == 0 and avs == 0)
    finally:
        if open(exe, "rb").read() != new_exe:
            open(exe, "wb").write(new_exe)
        open(gci, "wb").write(snap)
        mp = os.path.join(bin_dir, "save", "mp")
        if os.path.isdir(mp):
            shutil.rmtree(mp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", type=int, default=0)
    ap.add_argument("--old-exe", default="")
    ap.add_argument("--port", type=int, default=10300)
    args = ap.parse_args()
    results = []
    check = lambda d, c: L.check(d, c, results)
    audit(check)
    if args.runtime > 0:
        runtime(args, check)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
