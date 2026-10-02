#!/usr/bin/env python3
"""test_puppet_held_item.py - M9-C Phase 1 (remote held-item presentation, no protocol change).

ONE real game process (the host, `--host --bootstrap-resident 0`, PC_PUPPET_DIAG=1 in its environment) plus a
scripted FakeClient "B" that connects, sends an APPEARANCE and streams MOVE packets with different `item_kind`
values. The host renders B's puppet; the env-gated `[NET][PUPPET][DIAG] player N held item kind=K model=<name|deferred>
layer1_anim=A part_table=T` line (pc_remote_player.c, logged on item change only) is the observable decision.

Tier labels (honest): HOOK-DRIVEN / PROTOCOL-TESTED, real process, NO visual verification. These checks prove the
decision logic ran for every kind, that the draw path (hand-matrix callback + item draw, executed every frame while
the puppet is visible) did not crash/assert the process, and that the puppet survives item changes, a back-to-back
change, a disconnect/reconnect and a scene-generation recreate (host enters the shop and returns, via the existing
--scene-test-* hook). Whether the item looks right in the hand is NOT tested here (needs manual gameplay).

  H1  every kind in the matrix produces its decision line with the expected model/anim/part-table values
  H2  back-to-back change (axe -> net -> shovel in consecutive packets) ends on the last kind; no crash
  H3  disconnect + reconnect of B: the new puppet re-logs its decision (per-actor state, nothing stale)
  H4  scene-generation recreate (host goes into SHOP0 and back while B keeps sending kind 9): puppet re-logs
  (all: host process alive, no Segmentation/assert/Unhandled/access-violation text in its log)

Usage: python test_puppet_held_item.py [--port 7850]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import sys
import time

import net_spike_lib as L
from test_player_scene_real import boot_host, wait_log
from test_appearance_sync import build_appearance

HERE = os.path.dirname(os.path.abspath(__file__))
IDLE, WALK, RUN, DASH, TURN_DASH = 0, 1, 2, 3, 8
BAD_RX = re.compile(r"Segmentation|[Aa]ssert|Unhandled|ACCESS_VIOLATION|access violation", re.I)

# (kind, expected model name, expected layer1 anim index or None for -1, expected part table or None for 0)
# mPlayer_ANIM_AXE1 == 2 (mPlib_Get_Pointer_Animation table order: wait1, walk1, axe1, ...); PART_TABLE_AXE == 1.
MATRIX = [
    (0, "axe", 2, 1),
    (8, "gold_axe", 2, 1),
    (9, "net", "any", "any"),
    (10, "gold_net", "any", "any"),
    (11, "umbrella", "any", "any"),
    (42, "umbrella", "any", "any"),
    (43, "deferred", None, 0),   # ORG (custom design) umbrella: not transmitted
    (51, "rod", "any", "any"),
    (52, "gold_rod", "any", "any"),
    (53, "shovel", "any", "any"),
    (54, "gold_shovel", "any", "any"),
    (55, "deferred", None, 0),   # balloon
    (63, "deferred", None, 0),   # pinwheel
    (71, "fan", "any", "any"),
    (78, "fan", "any", "any"),
    (-1, "none", None, 0),
]

DIAG_RX = r"\[NET\]\[PUPPET\]\[DIAG\] player %d held item kind=%d model=(\S+) layer1_anim=(-?\d+) part_table=(-?\d+)"


def host_position(client):
    """Stand ~80 units next to the host's player (read from the host's relayed MOVE) so the puppet is on screen and
    its draw path runs; falls back to a fixed point if no host MOVE was seen."""
    L.pump_sleep(1.5, client.hub)
    msgs = [m for m in client.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_MOVE, (L.CH_UNRELIABLE,)))
            if m.game is not None and m.game.net_player_id == L.PC_NETGAME_HOST_PLAYER_ID]
    if msgs:
        g = msgs[-1].game
        return g.pos_x + 80.0, g.pos_y, g.pos_z
    print("INFO - no host MOVE seen; using a fixed puppet position")
    return 100.0, 0.0, 100.0


class Streamer:
    """Streams MOVE packets for one FakeClient (20 Hz-ish), pumping the transport so B stays connected."""

    def __init__(self, client):
        self.c = client
        self.x, self.y, self.z = host_position(client)
        self.frame = 1000

    def send(self, kind, move_state=IDLE, speed=0.0, seconds=0.0, n=1):
        end = time.monotonic() + seconds
        sent = 0
        while sent < n or time.monotonic() < end:
            self.frame += 1
            self.c.send_move(self.frame, self.x, self.y, self.z, angle=0, speed=speed, move_state=move_state,
                             item_kind=kind)
            sent += 1
            L.pump_sleep(0.05, self.c.hub)
            if sent >= n and seconds <= 0.0:
                break


def diag_lines(text, pid, kind):
    return re.findall(DIAG_RX % (pid, kind), text)


def clean(host, check, tag):
    txt = host.log_text()
    m = BAD_RX.search(txt)
    check(f"{tag} host process alive", host.alive())
    check(f"{tag} no Segmentation/assert/Unhandled/access-violation text in the host log"
          + ("" if m is None else f" (saw: {txt[max(0, m.start() - 80):m.end() + 80]!r})"), m is None)


def make_b(port, label="B"):
    b = L.FakeClient(label, "127.0.0.1", port)
    b.connect_and_ready()
    b.send_reliable(build_appearance(0, 1, 0x2401))
    return b


def wait_visual(host, off, timeout=40.0):
    return host.wait_for_log(r"\[NET\]\[REMOTE\]\[DIAG\] visual initialized", timeout, since_offset=off) is not None


def s1(port, log_dir, check):
    print("=" * 72 + "\n[S1] HOOK-DRIVEN: item-kind matrix, back-to-back change, disconnect/reconnect")
    host = boot_host(port, ["--authoritative-wildlife"], "held_s1", log_dir)
    if host is None:
        check("S1 host reached field", False)
        return
    b = b2 = None
    try:
        off = len(host.log_text())
        b = make_b(port)
        pid = b.assigned_peer_id
        st = Streamer(b)
        st.send(-1, n=10)  # puppet appears with no item
        check("S1 puppet visual initialised for B (appearance resolved)", wait_visual(host, off))
        check("S1 initial decision line: kind=-1 model=none layer1_anim=-1 part_table=0",
              host.wait_for_log(DIAG_RX % (pid, -1), 20.0, since_offset=off) is not None)
        off = len(host.log_text())
        for kind, model, a1, pt in MATRIX:
            if kind == -1:
                continue
            ms, sp = (IDLE, 0.0)
            st.send(kind, ms, sp, seconds=0.9)
            mo = host.wait_for_log(DIAG_RX % (pid, kind), 15.0, since_offset=off)
            ok = mo is not None and mo.group(1) == model
            if ok and a1 is not None and a1 != "any":
                ok = int(mo.group(2)) == a1
            if ok and a1 is None:
                ok = int(mo.group(2)) == -1
            if ok and pt is not None and pt != "any":
                ok = int(mo.group(3)) == pt
            if ok and a1 == "any":
                ok = int(mo.group(2)) >= 0  # an item pose clip was resolved for an implemented kind
            check("S1 kind %d -> model=%s%s (got %s)" % (kind, model, "" if a1 in (None, "any") else " anim=%d" % a1,
                                                       None if mo is None else mo.groups()), ok)
        # moving states with a skeleton item (net angle / rod move clip path) and with an axe, then the skid state
        for kind, ms, sp in ((9, WALK, 2.0), (9, TURN_DASH, 6.0), (51, RUN, 4.0), (0, DASH, 7.0), (11, WALK, 2.0)):
            st.send(kind, ms, sp, seconds=0.7)
        check("S1 moving/skid states with items did not take the host down", host.alive())
        off = len(host.log_text())
        st.send(-1, seconds=0.9)
        mo = host.wait_for_log(DIAG_RX % (pid, -1), 10.0, since_offset=off)
        check("S1 item removed again (kind=-1 model=none layer1_anim=-1 part_table=0)",
              mo is not None and mo.group(1) == "none" and int(mo.group(2)) == -1 and int(mo.group(3)) == 0)

        # H2 back-to-back change: consecutive packets, the interpolator may collapse the middle one
        off = len(host.log_text())
        for k in (0, 9, 53):
            st.send(k, n=1)
        st.send(53, seconds=1.0)
        check("S1 back-to-back change ends on the last kind (shovel)",
              host.wait_for_log(DIAG_RX % (pid, 53), 10.0, since_offset=off) is not None)

        # H3 disconnect/reconnect
        before = len(diag_lines(host.log_text(), pid, 71))
        st.send(71, seconds=0.9)
        check("S1 fan decision logged before the disconnect", len(diag_lines(host.log_text(), pid, 71)) >= before + 1)
        b.disconnect()
        L.pump_sleep(1.0, b.hub)
        b.close()
        b = None
        off = len(host.log_text())
        b2 = make_b(port, "B2")
        pid2 = b2.assigned_peer_id
        st2 = Streamer(b2)
        st2.send(71, seconds=1.5)
        check("S1 reconnect: the NEW puppet logs its own decision (kind=71 model=fan), nothing stale",
              host.wait_for_log(DIAG_RX % (pid2, 71), 25.0, since_offset=off) is not None)
        check("S1 reconnect: puppet visual re-initialised", wait_visual(host, off, 10.0))
        clean(host, check, "S1")
    finally:
        for c in (b, b2):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
        host.stop()


def s2(port, log_dir, check):
    print("=" * 72 + "\n[S2] HOOK-DRIVEN: scene-generation recreate (host enters SHOP0 and returns) while B carries a net")
    host = boot_host(port, ["--scene-test-enter-shop", "--scene-test-leave-after", "900"], "held_s2", log_dir)
    if host is None:
        check("S2 host reached field", False)
        return
    b = None
    try:
        b = make_b(port)
        pid = b.assigned_peer_id
        st = Streamer(b)
        t0 = time.monotonic()
        entered = left = False
        first_diag_before_enter = False
        while time.monotonic() - t0 < 150.0:
            st.send(9, WALK, 2.0, n=1)
            txt = host.log_text()
            if not entered and "hook: goto_other_scene(SCENE_SHOP0) res=1" in txt:
                entered = True
                first_diag_before_enter = len(diag_lines(txt, pid, 9)) >= 1
            if entered and "hook: goto_other_scene(exit) res=1" in txt:
                left = True
                break
            if not host.alive():
                break
        check("S2 host entered the shop and requested the real exit (hook-driven)", entered and left)
        t1 = time.monotonic()
        while time.monotonic() - t1 < 10.0:  # keep streaming through the transition back to the field
            st.send(9, WALK, 2.0, n=1)
        txt = host.log_text()
        n9 = len(diag_lines(txt, pid, 9))
        print("INFO - S2 puppet decision lines for kind 9: %d (diag line existed before the shop entry: %s)" %
              (n9, first_diag_before_enter))
        check("S2 the net decision was logged at least once", n9 >= 1)
        if first_diag_before_enter:
            check("S2 puppet recreated after the scene change re-logged its decision (>= 2 lines, per-actor state)",
                  n9 >= 2)
        else:
            print("INFO - S2 recreate re-log check inconclusive (puppet was not yet visible when the shop hook fired)")
        clean(host, check, "S2")
    finally:
        if b is not None:
            try:
                b.close()
            except Exception:
                pass
        host.stop()


def main():
    L.require_test_bin_dir()  # review M1: refuse to run against the live build/save
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7850)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    os.environ["PC_PUPPET_DIAG"] = "1"  # net_spike_lib copies os.environ into the launched game process
    results = []
    log_dir = os.path.join(HERE, "logs", "m9c")
    os.makedirs(log_dir, exist_ok=True)
    check = lambda d, c: L.check(d, c, results)
    for i, fn in enumerate((s1, s2)):
        if args.only and args.only != fn.__name__:
            continue
        fn(args.port + i, log_dir, check)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
