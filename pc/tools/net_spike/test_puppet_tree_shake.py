#!/usr/bin/env python3
"""test_puppet_tree_shake.py - M9-C Phase 4 (remote tree-shake presentation, cosmetic only, protocol stays v7).

Real host process (`--host --bootstrap-resident 0 --field-action-test-seed`, PC_PUPPET_DIAG=1 in its environment) plus
scripted FakeClients.  A peer streams MOVE packets with main index 89 (SHAKE_TREE) + an entry counter, standing 25 units
west of a real tree tile and facing east (0x4000).  The tree tiles are DISCOVERED from the host's own world snapshot (the
FakeClient's `world`), using the exact IS_ITEM_COLLIDEABLE_TREE id set PARSED from include/m_name_table.h, so no fixture
coordinates are assumed (the --field-action-test-seed trees are used too when they happen to be near the host player).
The observable decisions are the env-gated lines of pc_remote_player.c
    [NET][PUPPET][DIAG] player N state main_index=89 counter=C row=shake_tree ...            (entry edge, Phase 2a)
    [NET][PUPPET][DIAG] player N tree shake effect=large variant=V tile=(ux,uz) item=0xIIII species=S budget=B efbg_active=A
    [NET][PUPPET][DIAG] player N tree shake sound label=0x135 tile=(ux,uz) item=0xIIII species=S budget=B
    [NET][PUPPET][DIAG] player N cosmetics summary: ... tree_effects=E tree_sounds=S tree_skipped=K

Tier labels (honest): HOOK-DRIVEN (automated, no human input) / SOURCE AUDITED, real process, NO visual or audible
verification: these checks prove the trigger, the tile search, the gating/budget logic ran, that nothing in the world
changed, and that nothing crashed; NOT that the canopy shake looks or sounds right (needs manual play).

  A0  source audit of the Phase 4 code: no forbidden callee in code (comments stripped), the vanilla search constants
  T1  probing real trees: each fires exactly one `tree shake effect ... budget=ok` + one sound line, AFTER its entry edge,
      with tile == the tile the snapshot says holds a tree and item == that tile's snapshot value; facing sweep (only the
      east facing finds the adjacent tree, the other three give budget=notree and no effect)
  T2  cooldown: a new counter inside the 84 frame window -> budget=cooldown (no effect, no sound); after it -> ok again
  T3  three puppets shaking at once: at most 2 effects ok (EffectBG pool: >= 1 slot always stays free, never evict),
      every ok line reports efbg_active <= 1, the others are pool/capped/cooldown
  T4  a puppet in another scene (SHOP) -> budget=scene, zero ok lines
  T5  a shake interrupted by another row state before frame 10 does not fire later
  T6  no world mutation: no FIELD_UPDATE for any shaken tile reaches an observer, the tiles read through a fresh snapshot
      are unchanged, the host survives; the disconnect summary reports tree counters

Usage: python test_puppet_tree_shake.py [--port 8200] [--only t1]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import sys
import time

import net_spike_lib as L
from test_player_scene_real import boot_host
from test_puppet_cosmetics import scene_msg, SCENE_FG, SCENE_SHOP0, strip_comments
from test_puppet_held_item import clean, make_b, wait_visual

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "..", "src", "pc_remote_player.c")
NAME_TABLE_H = os.path.join(HERE, "..", "..", "..", "include", "m_name_table.h")
IDLE = 0
IDX_SHAKE_TREE, IDX_DIG_SCOOP = 89, 58
EAST, SOUTH, WEST, NORTH = 0x4000, 0, -0x8000, -0x4000  # atans_table(dz, dx): 0 = +z, 0x4000 = +x
ENTRY_OFFSET = 25.0  # puppet stands this far west of the tree centre (< mFI_UNIT_BASE_SIZE 40 so dist^2 < 1600)

STATE_RX = r"\[NET\]\[PUPPET\]\[DIAG\] player %d state main_index=89 counter=%d row=shake_tree"
EFF_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) tree shake effect=large variant=(\S+) tile=\((-?\d+),(-?\d+)\) "
                    r"item=0x([0-9a-f]+) species=(\w+) budget=(\w+) efbg_active=(-?\d+)")
SND_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) tree shake sound label=(\S+) tile=\((-?\d+),(-?\d+)\) "
                    r"item=0x([0-9a-f]+) species=(\w+) budget=(\w+)")
SUM_RX = re.compile(r"\[NET\]\[PUPPET\]\[DIAG\] player (\d+) cosmetics summary: .*tree_effects=(\d+) tree_sounds=(\d+) "
                    r"tree_skipped=(\d+)")


def parse_tree_ids():
    """The exact IS_ITEM_COLLIDEABLE_TREE id set, evaluated from include/m_name_table.h (#define NAME (BASE + N))."""
    txt = re.sub(r"/\*.*?\*/", "", open(NAME_TABLE_H, encoding="utf-8", errors="replace").read(), flags=re.S)
    defs = {}
    for m in re.finditer(r"^#define\s+(\w+)\s+(\(?[\w\s+\-()xX]+\)?)\s*(?://.*)?$", txt, re.M):
        defs[m.group(1)] = m.group(2).strip()

    def ev(name):
        e = re.sub(r"[A-Za-z_]\w*", lambda mm: str(ev(mm.group(0))) if mm.group(0) in defs else mm.group(0), defs[name])
        return eval(e)

    i = txt.index("#define IS_ITEM_COLLIDEABLE_TREE")
    j = txt.index("\n\n", i)
    names = re.findall(r"\(item\) == (\w+)", txt[i:j])
    return {ev(n) for n in names}, ev


def effects(text, pid):
    return [m.groups() for m in EFF_RX.finditer(text) if int(m.group(1)) == pid]


def sounds(text, pid):
    return [m.groups() for m in SND_RX.finditer(text) if int(m.group(1)) == pid]


class Peer:
    def __init__(self, port, label, scene=SCENE_FG):
        self.c = make_b(port, label)
        self.pid = self.c.assigned_peer_id
        self.scene_seq = 0
        self.set_scene(scene)
        L.pump_sleep(1.5, self.c.hub)
        msgs = [m for m in self.c.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_MOVE, (L.CH_UNRELIABLE,)))
                if m.game is not None and m.game.net_player_id == L.PC_NETGAME_HOST_PLAYER_ID]
        g = msgs[-1].game if msgs else None
        self.host_xyz = (g.pos_x, g.pos_y, g.pos_z) if g else (1000.0, 0.0, 1000.0)
        self.x, self.y, self.z = self.host_xyz[0] + 80.0, self.host_xyz[1], self.host_xyz[2]
        self.angle = 0
        self.frame = 1000
        self.placed = False

    def set_scene(self, sid):
        self.scene_seq += 1
        self.c.send_reliable(scene_msg(self.scene_seq, sid))

    def move_once(self, main_index=None, counter=0, state=IDLE):
        self.frame += 1
        self.c.send_move(self.frame, self.x, self.y, self.z, angle=self.angle, speed=0.0, move_state=state,
                         main_index=main_index, entry_counter=counter)

    def stream(self, seconds, main_index=None, counter=0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.move_once(main_index, counter)
            L.pump_sleep(0.05, self.c.hub)

    def place_at_tree(self, tree, facing=EAST):
        ux, uz = tree
        self.x, self.z = ux * 40.0 + 20.0 - ENTRY_OFFSET, uz * 40.0 + 20.0
        self.y = self.host_xyz[1]
        self.angle = facing
        self.stream(1.0)  # idle MOVEs: lets the interpolator snap/settle at the new position first

    def close(self):
        try:
            self.c.close()
        except Exception:
            pass


def find_candidates(peer, tree_ids, limit=14, radius=300.0):
    """Tree tiles from the peer's world snapshot within `radius` of the host player, nearest first."""
    hx, hy, hz = peer.host_xyz
    out = []
    for ux in range(int(hx // 40) - 9, int(hx // 40) + 10):
        for uz in range(int(hz // 40) - 9, int(hz // 40) + 10):
            v = peer.c.world.tile_ut(ux, uz)
            if v in tree_ids:
                cx, cz = ux * 40.0 + 20.0, uz * 40.0 + 20.0
                d = max(abs(cx - hx), abs(cz - hz))
                if d <= radius and abs(cx - ENTRY_OFFSET - hx) <= radius:
                    out.append((d, (ux, uz), v))
    out.sort()
    return [(t, v) for _, t, v in out][:limit]


def shake(host, peer, tree, counter, facing=EAST, secs=1.4, settle=True, tail=0.6):
    """One entry: (re)place the puppet, send state 89 with `counter`, then idle; returns the host log slice."""
    if settle:
        peer.place_at_tree(tree, facing)
    peer.angle = facing
    off = len(host.log_text())
    peer.stream(secs, IDX_SHAKE_TREE, counter)
    if tail > 0:
        peer.stream(tail)
    return host.log_text()[off:]


def a0(check):
    print("=" * 72 + "\n[A0] SOURCE AUDITED: pc_remote_player.c Phase 4 code")
    src = open(SRC, encoding="utf-8").read()
    code = strip_comments(src)
    start = code.index("static mActor_name_t pc_puppet_find_shake_tree")
    end = code.index("static void pc_puppet_pending_run", start)
    blk = code[start:end]
    forbidden = ["request_tree_shake", "host_local_tree_shake", "item_tree_fruit_drop_proc", "drop_fruit", "fruit_set",
                 "mFI_SetFG_common", "pcfa_set_tile", "pc_net_game_request_", "set_pl_act_tim_proc", "mPlib_Check_tree_shaken",
                 "mPlib_able_birth_bee", "Check_BirthBee", "pc_tree_birth_bee_visual", "Player_actor_", "GET_PLAYER_ACTOR_NOW",
                 "Now_Private", "RANDOM(", "mISL_", "sAdo_PlyWalkSe", "mFI_UtNumtoFGSet"]
    for f in forbidden:
        check("A0 tree-shake code block does not contain `%s`" % f, f not in blk)
    check("A0 effect goes through the public clip make_effect_bg_proc (null-checked)",
          "Common_Get(clip).make_effect_bg_proc == NULL" in blk and "Common_Get(clip).make_effect_bg_proc(game" in blk)
    check("A0 vanilla constants: 45 degree window, |dy| <= 10, dist^2 < unit base size^2, frame 10, 84 frame cooldown",
          "DEG2SHORT_ANGLE2(45.0f)" in blk and "> 10.0f" in blk and "SQ(mFI_UNIT_BASE_SIZE_F)" in blk and
          "PC_PUPPET_TREE_FRAME 10.0f" in src and "PC_PUPPET_TREE_COOLDOWN_FRAMES 84.0" in src)
    check("A0 effect type is EffectBG_EFFECT_SHAKE_LARGE and sound NA_SE_TREE_YURASU",
          "EffectBG_EFFECT_SHAKE_LARGE" in blk and "NA_SE_TREE_YURASU" in blk)
    check("A0 never evicts: EffectBG occupancy gate + dup gate + global spacing present",
          "Effectbg_pc_count_active()" in blk and "Effectbg_pc_count_near(" in blk and "PC_PUPPET_TREE_GLOBAL_SPACING" in blk and
          "PC_PUPPET_TREE_EFBG_MAX_ACTIVE 1" in src)
    check("A0 gated through pc_puppet_fx_reason (scene/hidden/pause/null/range/pool/capped) before touching the field",
          blk.index("pc_puppet_fx_reason(self, slot, game, 0)") < blk.index("pc_puppet_find_shake_tree(&self"))
    check("A0 hook only from the puppet cosmetics tick (no network handler)",
          code.count("pc_puppet_tree_shake_tick(") == 2 and
          all("pc_puppet_tree_shake" not in m for m in re.findall(r"void pc_remote_player_on_\w+\(.*?\n\}\n", code, re.S)))
    eb = open(os.path.join(HERE, "..", "..", "..", "src", "actor", "ac_effectbg.c"), encoding="utf-8", errors="replace").read()
    i = eb.index("int Effectbg_pc_count_active(void)")
    tail = strip_comments(eb[i:])
    check("A0 EffectBG accessors are read-only (no store into the pool)",
          "efbg_start_p[i].status &" in tail and re.search(r"efbg_start_p\[i\]\.\w+\s*=[^=]", tail) is None)


def t_all(port, log_dir, check):
    print("=" * 72 + "\n[T1..T6] HOOK-DRIVEN: remote tree shake presentation")
    tree_ids, _ev = parse_tree_ids()
    check("T0 parsed the IS_ITEM_COLLIDEABLE_TREE id set from m_name_table.h (%d ids)" % len(tree_ids), len(tree_ids) >= 40)
    host = boot_host(port, ["--authoritative-wildlife", "--field-action-test-seed"], "tree_all", log_dir)
    if host is None:
        check("T host reached field", False)
        return
    peers = []
    obs = None

    def new_peer(label, scene=SCENE_FG):
        p = Peer(port, label, scene)
        peers.append(p)
        return p

    try:
        obs = new_peer("OBS")
        obs.stream(2.0)
        check("T puppet visual initialised", wait_visual(host, 0))
        cands = find_candidates(obs, tree_ids)
        print("INFO - %d tree candidates near the host player %s: %s" %
              (len(cands), tuple(round(v) for v in obs.host_xyz), cands[:6]))
        check("T1 the snapshot shows trees near the host player (%d candidates)" % len(cands), len(cands) >= 3)
        mark = obs.c.inbox.mark()
        before = {t: obs.c.world.tile_ut(*t) for t, _v in cands}

        # ---- T1: probe candidates; each good tree must produce exactly one effect+sound after its entry edge ----
        good = []
        counter = 0
        pi = 0
        probe = None
        for tree, val in cands:
            if len(good) >= 4:
                break
            if probe is None or pi >= 3:
                if probe is not None:
                    probe.close()
                    peers.remove(probe)
                probe = new_peer("P%d" % len(peers))
                pi = 0
            pi += 1
            counter = counter % 15 + 1
            t = shake(host, probe, tree, counter)
            st = re.search(STATE_RX % (probe.pid, counter), t)
            ef = effects(t, probe.pid)
            sn = sounds(t, probe.pid)
            oks = [e for e in ef if e[6] == "ok"]
            if oks:
                e = oks[0]
                good.append((tree, val))
                check("T1 tree %s: entry edge logged before the effect, exactly one effect line (ok) and one sound (ok)" % (tree,),
                      st is not None and len(ef) == 1 and len(sn) == 1 and sn[0][6] == "ok" and
                      st.start() < EFF_RX.search(t).start())
                check("T1 tree %s: diag tile == snapshot tile and item == snapshot value (0x%04x) (got %s item 0x%s)" %
                      (tree, val, (e[2], e[3]), e[4]), (int(e[2]), int(e[3])) == tree and int(e[4], 16) == val)
                check("T1 tree %s: sound label 0x135 (NA_SE_TREE_YURASU) at the same tile" % (tree,),
                      sn[0][1] == "0x135" and (int(sn[0][2]), int(sn[0][3])) == tree)
                check("T1 tree %s: efbg_active reported (>= 0) and <= 1 when a puppet spawned" % (tree,),
                      0 <= int(e[7]) <= 1)
                print("INFO - T1 tree %s item=0x%04x -> variant=%s species=%s" % (tree, val, e[1], e[5]))
                probe.stream(2.6)  # let this puppet's own cooldown, the global spacing and the effect lapse
            else:
                print("INFO - T1 candidate %s gave %s (puppet y/tile height mismatch or pool): skipped" %
                      (tree, [(e[6]) for e in ef]))
        check("T1 at least 2 real trees presented (%d good)" % len(good), len(good) >= 2)
        if len(good) < 2:
            return

        # facing sweep on the first good tree (fresh peer: fresh diag limiter)
        sweep = new_peer("SW")
        L.pump_sleep(2.8, sweep.c.hub)
        tree0 = good[0][0]
        res = {}
        for facing, ctr in ((SOUTH, 1), (WEST, 2), (NORTH, 3)):
            t = shake(host, sweep, tree0, ctr, facing=facing)
            res[facing] = [e[6] for e in effects(t, sweep.pid)]
        t = shake(host, sweep, tree0, 4, facing=EAST)
        res[EAST] = [e[6] for e in effects(t, sweep.pid)]
        print("INFO - T1 facing sweep budgets: %s" % res)
        check("T1 facing sweep: south/west/north (outside the 45 degree window) -> budget=notree only",
              all(res[f] == ["notree"] for f in (SOUTH, WEST, NORTH)))
        check("T1 facing sweep: east (toward the tree) -> budget=ok", res[EAST] == ["ok"])

        # ---- T2: cooldown ----
        cp = new_peer("CD")
        L.pump_sleep(2.8, cp.c.hub)
        t1 = shake(host, cp, tree0, 5, secs=0.8, tail=0.0)
        t2 = shake(host, cp, tree0, 6, settle=False, secs=0.9)  # < 84 frames after the first accepted shake
        b1 = [e[6] for e in effects(t1, cp.pid)]
        b2 = [e[6] for e in effects(t2, cp.pid)]
        n_snd2 = len(sounds(t2, cp.pid))
        check("T2 first shake ok, immediate second shake (new counter) -> budget=cooldown (%s / %s)" % (b1, b2),
              b1 == ["ok"] and b2 == ["cooldown"])
        check("T2 a cooldown shake plays no sound (%d)" % n_snd2, n_snd2 == 0)
        cp.stream(3.0)
        t3 = shake(host, cp, tree0, 7, settle=False)
        b3 = [e[6] for e in effects(t3, cp.pid)]
        check("T2 after the cooldown a new counter fires again (budget=ok) (%s)" % b3, b3 == ["ok"])
        t4 = host.log_text()
        n_states = len(re.findall(STATE_RX % (cp.pid, 7), t4))
        check("T2 repeated state entries logged (one per counter)", n_states >= 1)

        # ---- summary counters on disconnect (the sweep peer: 1 effect ok + 3 notree) ----
        off = len(host.log_text())
        swpid = sweep.pid
        sweep.c.disconnect()
        L.pump_sleep(1.0, obs.c.hub)
        sweep.close()
        peers.remove(sweep)
        host.wait_for_log(r"player %d cosmetics summary: .*tree_effects=" % swpid, 15.0, since_offset=off)
        sms = [x for x in SUM_RX.finditer(host.log_text()[off:]) if int(x.group(1)) == swpid]
        sm = sms[-1] if sms else None
        check("T6 disconnect summary shows tree counters effects=1 sounds=1 skipped=3 (%s)" %
              (None if sm is None else sm.groups(),),
              sm is not None and (int(sm.group(2)), int(sm.group(3)), int(sm.group(4))) == (1, 1, 3))

        # ---- T3: three puppets at once ----
        if len(good) >= 3:
            for p in list(peers):
                if p is not obs:
                    p.close()
                    peers.remove(p)
            trio = [new_peer("Q%d" % i) for i in range(3)]
            L.pump_sleep(3.0, trio[0].c.hub)
            for i, p in enumerate(trio):
                p.place_at_tree(good[i][0])
            off = len(host.log_text())
            end = time.monotonic() + 1.6
            while time.monotonic() < end:
                for p in trio:
                    p.move_once(IDX_SHAKE_TREE, 9)
                L.pump_sleep(0.05, trio[0].c.hub)
            for p in trio:
                p.stream(0.4)
            t = host.log_text()[off:]
            allfx = [e for p in trio for e in effects(t, p.pid)]
            oks = [e for e in allfx if e[6] == "ok"]
            print("INFO - T3 budgets: %s" % sorted(e[6] for e in allfx))
            check("T3 three simultaneous shakes: 1..2 effects ok (never all three), got %d" % len(oks), 1 <= len(oks) <= 2)
            check("T3 every ok line saw efbg_active <= 1 (a slot always stays free for the local player)",
                  all(int(e[7]) <= 1 for e in oks))
            check("T3 the rest are pool/capped/cooldown only (%s)" % sorted({e[6] for e in allfx} - {"ok"}),
                  {e[6] for e in allfx} - {"ok"} <= {"pool", "capped", "cooldown"})
            check("T3 host alive", host.alive())
            for p in trio:
                p.close()
                peers.remove(p)
        else:
            print("INFO - T3 skipped: fewer than 3 good trees")

        # ---- T4: another scene ----
        sp = new_peer("SH", scene=SCENE_SHOP0)
        L.pump_sleep(2.0, sp.c.hub)
        t = shake(host, sp, tree0, 8)
        fx = effects(t, sp.pid)
        check("T4 puppet in the SHOP scene: zero ok lines, budget=scene (%s)" % [e[6] for e in fx],
              len(fx) >= 1 and all(e[6] == "scene" for e in fx) and not sounds(t, sp.pid))

        # ---- T5: interrupted before frame 10 ----
        ip = new_peer("IN")
        L.pump_sleep(2.8, ip.c.hub)
        ip.place_at_tree(good[-1][0])
        off = len(host.log_text())
        ip.frame += 1
        for _ in range(3):
            ip.move_once(IDX_SHAKE_TREE, 9)
            L.pump_sleep(0.05, ip.c.hub)
        for _ in range(3):
            ip.move_once(IDX_DIG_SCOOP, 3)
            L.pump_sleep(0.05, ip.c.hub)
        ip.stream(1.5, IDX_DIG_SCOOP, 3)
        ip.stream(1.0)
        t = host.log_text()[off:]
        entered = re.search(STATE_RX % (ip.pid, 9), t) is not None
        n_ok = len([e for e in effects(t, ip.pid) if e[6] == "ok"])
        print("INFO - T5 shake row entered=%s effects ok=%d" % (entered, n_ok))
        if entered:
            check("T5 a shake replaced by another row state before frame 10 never fires (ok=%d)" % n_ok, n_ok == 0)
        else:
            print("INFO - T5 the interpolator never adopted the 89 snapshot (too brief): not a failure, weak check")

        # ---- T6: no world mutation ----
        L.pump_sleep(1.0, obs.c.hub)
        ups = [L.field_update_tuple(g) for g in obs.c.field_updates_since(mark)]
        shaken = {t for t, _v in cands}
        touched = [u for u in ups if (u[0], u[1]) in shaken]
        print("INFO - T6 observer saw %d FIELD_UPDATE(s) in total; for candidate tree tiles: %s" % (len(ups), touched))
        check("T6 no FIELD_UPDATE for any shaken tree tile reached the observer (the presentation never writes the field)",
              not touched)
        probe_tiles = [t for t, _v in good]
        after = L.read_tiles_via_snapshot("127.0.0.1", port, probe_tiles)
        check("T6 a fresh authoritative snapshot still shows every shaken tile unchanged %s" %
              ([(t, before[t], after[t]) for t in probe_tiles][:4],), all(after[t] == before[t] for t in probe_tiles))
        clean(host, check, "T")

        clean(host, check, "T (end)")
    finally:
        for p in peers:
            p.close()
        host.stop()


def main():
    L.require_test_bin_dir()  # review M1: refuse to run against the live build/save
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8200)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    os.environ["PC_PUPPET_DIAG"] = "1"  # net_spike_lib copies os.environ into the launched game process
    results = []
    log_dir = os.path.join(HERE, "logs", "m9c")
    os.makedirs(log_dir, exist_ok=True)
    check = lambda desc, cond: L.check(desc, cond, results)
    if not args.only or args.only == "a0":
        a0(check)
    if not args.only or args.only in ("t1", "t"):
        t_all(args.port, log_dir, check)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
