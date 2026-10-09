#!/usr/bin/env python3
"""run_two_process_smoke.py - REAL two-process smoke test: two AnimalCrossing.exe processes (host +
client, no fake clients), each walked from the title screen into town with game_input.py (each window
targeted by PID only), then verified from their own logs and screenshots.

  host   ./AnimalCrossing.exe --host 7788 --verbose --pickup-test-seed
  client ./AnimalCrossing.exe --connect "127.0.0.1:7788" --verbose
  both with CWD pc/build64/bin (game resources are CWD-relative), each with its own log file under
  pc/tools/net_spike/logs/smoke/. Screenshots are saved there too.

Phases
  (a) client IDENTITY accepted; both sides READY (pc_net_game.c log lines)
  (b) host "snapshot epoch N sent (30 acres ...) known_tiles=K unknown_tiles=U" and client "snapshot
      epoch N applied (...) changed_tiles=C ..." with the per-tile "snapshot tile acre A tile T: 0x.. ->
      0x.." lines checked against the tiles --pickup-test-seed writes (ITM_FOOD_APPLE 0x2800 at the 20
      in-town seed tiles; every one is tile 136 of acres 0-3,5-8,10-13,15-18,20-23)
  (c) each side creates the other's remote-player actor; movement/appearance flow; no GUARD / errors
  (d) kill ONLY the client, relaunch a fresh client (after the host's 5 s timeout, so the slot is
      reused), walk it to town: new snapshot epoch, per-peer reset, READY again
  (e) clean shutdown of both (WM_CLOSE), exit codes; no crash/stall; no unexpected log lines
  (f) reverse order: client started BEFORE the host

NOTE: the script focuses each game window and injects keystrokes; do not type into other windows.
Usage: python run_two_process_smoke.py [--port 7788] [--skip-reverse]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
from game_input import GameWindow  # noqa: E402

L.require_launchable_bin_dir(L.GAME_BIN_DIR)
BIN = L.GAME_BIN_DIR
EXE = os.path.join(BIN, "AnimalCrossing.exe")
OUT = os.path.join(HERE, "logs", "smoke")
BENIGN = re.compile(r"l_keepSave not set, aborting")
SUSPECT = re.compile(r"\b(error|fail(ed|ure)?|assert\w*|fatal|exception|abort\w*|crash\w*)\b", re.I)
results = []
exit_table = []   # (label, pid, how it ended, exit code, clean?)
procs = []


def check(desc, cond):
    L.check(desc, cond, results)


class Game:
    def __init__(self, label, args):
        self.label, self.args = label, list(args)
        self.log_path = os.path.join(OUT, f"{label}.log")
        self.proc = None
        self.win = None
        self._fp = None

    def start(self):
        self._fp = open(self.log_path, "wb")
        self.proc = subprocess.Popen([EXE] + self.args, cwd=BIN, stdout=self._fp, stderr=subprocess.STDOUT)
        procs.append(self)
        print(f"[smoke] launched {self.label}: pid {self.proc.pid} args {self.args}")
        return self

    def text(self):
        try:
            with open(self.log_path, "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def find(self, rx, flags=0):
        return re.findall(rx, self.text(), flags)

    def wait(self, rx, timeout, count=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(re.findall(rx, self.text())) >= count:
                return True
            if not self.alive():
                return len(re.findall(rx, self.text())) >= count
            time.sleep(0.25)
        return len(re.findall(rx, self.text())) >= count

    def window(self):
        if self.win is None:
            self.win = GameWindow(self.proc.pid)
        deadline = time.monotonic() + 20
        while not self.win.ready() and time.monotonic() < deadline:
            time.sleep(0.2)
        return self.win

    def boot_to_town(self, timeout=150.0):
        """title -> START -> A ... until "[SCENE_MODE] 3 -> 1" (gameplay) is logged."""
        if not self.wait(r"press_start_opacity=255", timeout):
            return False
        w = self.window()
        w.press("START", after=3.0)
        n = 0
        deadline = time.monotonic() + timeout
        while not re.search(r"\[SCENE_MODE\] 3 -> 1", self.text()) and self.alive() and time.monotonic() < deadline:
            w.press("A", after=1.5)
            n += 1
            if n > 40:
                break
        ok = bool(re.search(r"\[SCENE_MODE\] 3 -> 1", self.text()))
        print(f"[smoke] {self.label}: boot_to_town {'OK' if ok else 'FAILED'} (START + {n} x A)")
        time.sleep(3.0)
        return ok

    def shot(self, name):
        path = os.path.join(OUT, f"{name}.png")
        try:
            self.window().screenshot(path)
            print(f"[smoke] screenshot {os.path.basename(path)} ({self.label})")
        except Exception as e:  # diagnostics only
            print(f"[smoke] screenshot {name} failed: {e}")
        return path

    def hold(self, button, seconds):
        self.window().hold(button, seconds)

    def close_clean(self, timeout=25.0):
        """WM_CLOSE -> normal shutdown; records how it ended."""
        if not self.alive():
            self._record("already exited before close", self.proc.poll(), False)
            return
        self.window().close()
        try:
            self.proc.wait(timeout)
            self._record("WM_CLOSE (clean shutdown)", self.proc.returncode, self.proc.returncode == 0)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(10)
            self._record("did NOT exit within %.0fs of WM_CLOSE; terminated" % timeout, self.proc.returncode, False)

    def kill(self):
        if self.alive():
            self.proc.terminate()
            self.proc.wait(10)
        self._record("killed by the harness (TerminateProcess)", self.proc.returncode, None)

    def _record(self, how, rc, clean):
        exit_table.append((self.label, self.proc.pid, how, rc, clean))
        if self._fp is not None:
            self._fp.close()
            self._fp = None


boot_log = []  # (label, attempt, boot_ok, exit code if the process died by itself)


def launch_and_boot(label, args):
    """Launch + walk to town. If the process dies on its own during boot (the known intermittent
    0xC0000005 after "GCI save loaded successfully") it is COUNTED, its log kept, and the SAME launch is
    repeated once on a fresh process (label + '_retry'); both outcomes are recorded. Returns (game, ok)."""
    g = Game(label, args).start()
    ok = g.boot_to_town()
    boot_log.append((label, 1, ok, None if g.alive() else g.proc.poll()))
    if ok or g.alive():
        return g, ok
    rc = g.proc.poll()
    print(f"[smoke] BOOT FAILURE: {label} exited by itself with {rc} ({rc & 0xFFFFFFFF:#010x}); "
          f"retrying once on a fresh process (log kept: {os.path.basename(g.log_path)})")
    exit_table.append((label, g.proc.pid, "DIED DURING BOOT (before any network activity)", rc, False))
    g2 = Game(label + "_retry", args).start()
    ok2 = g2.boot_to_town()
    boot_log.append((label + "_retry", 2, ok2, None if g2.alive() else g2.proc.poll()))
    return g2, ok2


def suspects(g):
    out = []
    for ln in g.text().splitlines():
        if SUSPECT.search(ln) and not BENIGN.search(ln) and "[NET][FIELD][GUARD]" not in ln:
            out.append(ln.strip()[:200])
    return out


def guard_counts(g):
    """(total GUARD lines, GUARD lines after the process's own READY line)."""
    t = g.text()
    rdy = t.find("handshake complete, town verified")
    pos = [m.start() for m in re.finditer(r"\[NET\]\[FIELD\]\[GUARD\]", t)]
    return len(pos), len([x for x in pos if rdy >= 0 and x > rdy])


def positions(g, player):
    return sorted(set(re.findall(r"player %d actor pos=\(([-\d.]+),([-\d.]+),([-\d.]+)\)" % player, g.text())))


def parse_tiles(g, epoch):
    """{(acre, tile): (old, new)} for the client's snapshot-tile lines of `epoch` (lines between
    'snapshot epoch E begin' and 'snapshot epoch E applied'), plus the '(N more)' overflow count."""
    t = g.text()
    b = re.search(r"client: snapshot epoch %d begin" % epoch, t)
    e = re.search(r"client: snapshot epoch %d applied" % epoch, t)
    if not b or not e:
        return {}, 0
    seg = t[b.start():e.end()]
    tiles = {(int(a), int(x)): (int(o, 16), int(n, 16))
             for a, x, o, n in re.findall(r"snapshot tile acre (\d+) tile (\d+): 0x([0-9A-Fa-f]{4}) -> 0x([0-9A-Fa-f]{4})", seg)}
    more = re.search(r"snapshot tile \.\.\. \((\d+) more\)", seg)
    return tiles, int(more.group(1)) if more else 0


def expected_seed_tiles():
    out = {}
    for ut in L.TOWN_FIXTURE_TILES:
        out[L.town_ut_to_acre_tile(*ut)] = ut
    return out


SEED_RX = re.compile(r"--pickup-test-seed: fixture item placed at tile \((\d+),(\d+)\)(?: \(OVERWROTE non-empty 0x([0-9A-Fa-f]{4})\))?")
PARK_RX = re.compile(r"snapshot tile (parked|skipped) acre (\d+) tile (\d+): local 0x([0-9A-Fa-f]{4}) host 0x([0-9A-Fa-f]{4}) \(([^)]*)\)")
WEEDS = range(0x0008, 0x000B)


def epoch_segment(g, epoch):
    t = g.text()
    b = re.search(r"client: snapshot epoch %d begin" % epoch, t)
    e = re.search(r"client: snapshot epoch %d applied" % epoch, t)
    if not (b and e):
        return ""
    nl = t.find("\n", e.end())
    return t[b.start():(nl if nl >= 0 else len(t))]


def seed_and_park_report(name, host, client, epoch, changed, known, unknown):
    """(b)/(d): classify the client's snapshot changes / parked / skipped tiles against the host's seed log."""
    seg = epoch_segment(client, epoch)
    seeds = {}  # (acre, tile) -> (ut, prior value or None)
    for x, z, prev in SEED_RX.findall(host.text()):
        at = L.town_ut_to_acre_tile(int(x), int(z))
        if at is not None and at not in seeds:
            seeds[at] = ((int(x), int(z)), int(prev, 16) if prev else None)
    check(f"{name} host seed log lists the 20 in-town fixture tiles ({len(seeds)})", len(seeds) == 20)
    changes, more_changes = parse_tiles(client, epoch)
    parked = {(int(a), int(t)): (int(lo, 16), int(ho, 16), why)
              for k, a, t, lo, ho, why in PARK_RX.findall(seg) if k == "parked"}
    skipped = [(int(a), int(t), int(lo, 16), int(ho, 16), why) for k, a, t, lo, ho, why in PARK_RX.findall(seg)
               if k == "skipped"]
    m = re.search(r"changed_tiles=(\d+) changed_acres=(\d+) parked=(\d+) skipped_transient=(\d+)", seg)
    n_changed, _acres, n_parked, n_skipped = (int(v) for v in m.groups()) if m else (0, 0, 0, 0)
    mp = re.search(r"snapshot tile parked \.\.\. \((\d+) more\)", seg)
    ms = re.search(r"snapshot tile skipped \.\.\. \((\d+) more\)", seg)
    more_p, more_s = (int(mp.group(1)) if mp else 0), (int(ms.group(1)) if ms else 0)

    overwrote = {k: v[1] for k, v in seeds.items() if v[1] is not None}
    empty = [k for k, v in seeds.items() if v[1] is None]
    print(f"    {name} seed tiles: {len(empty)} were EMPTY on the host; {len(overwrote)} were non-empty and OVERWRITTEN: "
          + ", ".join(f"acre {a} tile {t} (was 0x{v:04X})" for (a, t), v in sorted(overwrote.items())))
    # (i) the OVERWROTE note is correct: for every FLAGGED tile the client's own local value (its snapshot
    # change "old" value, or its parked "local" value) equals the prior value the host reported overwriting;
    # for every UN-flagged (host-empty) tile any non-zero client-local value is a cross-process field-init
    # divergence (e.g. a weed grown locally), reported as INFO -- the host tile was empty either way.
    mism = []
    for k, prev in sorted(overwrote.items()):
        local = changes[k][0] if k in changes else (parked[k][0] if k in parked else None)
        if local != prev:
            mism.append((k, prev, local))
    diverge = {k: changes[k][0] for k in empty if k in changes and changes[k][0] != 0}
    for k, v in sorted(diverge.items()):
        print(f"    INFO {name} un-flagged (host-empty) seed tile acre {k[0]} tile {k[1]}: client-local was 0x{v:04X} "
              "(cross-process field-init divergence), still overwritten by the host's 0x2800")
    check(f"{name} (i) OVERWROTE is flagged on exactly {len(overwrote)} tiles {sorted(overwrote)} and the client's own "
          f"local value on each equals the flagged prior value (mismatches: {mism})", not mism)
    # (ii) every seed tile that was EMPTY on the host appears as a client change to the apple
    miss = [k for k in empty if k not in changes or changes[k][1] != 0x2800]
    check(f"{name} (ii) every host-EMPTY seed tile ({len(empty)}) shows on the client as a snapshot change to 0x2800"
          f"{'; NOT seen: ' + str(miss) if miss else ''}", not miss)
    # (iii) overwritten footprints: parked when the prior value was AMBIGUOUS/TRANSIENT (the client owns the
    # tile), applied as a change when the prior value was STABLE
    bad = []
    for k, prev in sorted(overwrote.items()):
        if prev in L.AMBIGUOUS_VALUES or prev in L.TRANSIENT_VALUES:
            ok = k in parked and parked[k][1] == 0x2800 and (
                ("ambiguous" in parked[k][2] and parked[k][0] in L.AMBIGUOUS_VALUES)
                or ("placeholder" in parked[k][2] and parked[k][0] in L.TRANSIENT_VALUES))
            kind = "parked (local ambiguous/placeholder)"
        else:
            ok = k in changes and changes[k][1] == 0x2800
            kind = "applied as a change"
        print(f"      overwritten seed tile acre {k[0]} tile {k[1]} (host was 0x{prev:04X}): expected {kind}: "
              f"{'OK' if ok else 'MISSING'}" + (f" -> {parked[k]}" if k in parked else f" -> {changes.get(k)}"))
        if not ok:
            bad.append(k)
    check(f"{name} (iii) every seed tile the host overwrote is parked (ambiguous/placeholder prior value) or applied "
          f"(stable prior value) on the client, as its prior value dictates{'; wrong: ' + str(bad) if bad else ''}",
          not bad)
    # (iv) all parked tiles, with reasons
    print(f"    {name} (iv) parked={n_parked} (logged {len(parked)}, +{more_p} beyond the log cap):")
    unexplained = []
    for (a, t), (lo, ho, why) in sorted(parked.items()):
        explained = (lo in L.AMBIGUOUS_VALUES and "ambiguous" in why) or (lo in L.TRANSIENT_VALUES and "placeholder" in why)
        note = "seed footprint overwrite" if (a, t) in seeds else (
            "local structure/footprint value owns the tile" if explained else "UNEXPLAINED")
        if not explained:
            unexplained.append((a, t))
        print(f"      parked acre {a} tile {t}: local 0x{lo:04X} host 0x{ho:04X} ({why}) -> {note}")
    check(f"{name} (iv) every logged parked tile has a local AMBIGUOUS/TRANSIENT value matching its reason "
          f"(unexplained: {unexplained})", not unexplained)
    check(f"{name} (iv) parked count accounted: logged {len(parked)} + not-logged {more_p} == parked={n_parked}",
          len(parked) + more_p == n_parked)
    by_reason = {}
    for _a, _t, _lo, _ho, why in skipped:
        by_reason[why] = by_reason.get(why, 0) + 1
    print(f"    {name} (iv) skipped_transient={n_skipped} (logged {len(skipped)} {by_reason}, +{more_s} beyond the cap); "
          f"host unknown_tiles={unknown}")
    check(f"{name} (iv) skipped tiles are exactly the host's valid=0 tiles (skipped={n_skipped} == host unknown_tiles="
          f"{unknown}; every logged skip is 'host valid=0'/'host transient')",
          n_skipped == unknown and all(w in ("host valid=0", "host transient") for w in by_reason))
    others = {k: v for k, v in changes.items() if k not in seeds}
    weeds = {k: v for k, v in others.items() if v[0] in WEEDS or v[1] in WEEDS}
    rest = {k: v for k, v in others.items() if k not in weeds}
    print(f"    {name} non-seed snapshot changes: {len(others)} = {len(weeds)} weed-value changes (0x0008-0x000A: each "
          f"process runs its own field-init growth) + {len(rest)} other:")
    for k, v in sorted(rest.items()):
        print(f"      acre {k[0]} tile {k[1]}: 0x{v[0]:04X} -> 0x{v[1]:04X}  (UNCLASSIFIED: not a seed tile, not a weed)")
    check(f"{name} every changed tile is logged (changed_tiles={n_changed}, logged {len(changes)}, +{more_changes} lost "
          f"to the log cap)", more_changes == 0 and len(changes) == n_changed)


def snapshot_checks(name, host, client, peer, first_only=True):
    """Match the host's 'snapshot ... sent' line for `peer` with the client's 'applied' line; returns epoch."""
    sent = re.findall(r"\[NET\]\[WORLD\] host: peer %d snapshot epoch (\d+) sent \((\d+) acres, world_seq (\d+)\) "
                      r"known_tiles=(\d+) unknown_tiles=(\d+)" % peer, host.text())
    applied = re.findall(r"\[NET\]\[WORLD\] client: snapshot epoch (\d+) applied \((\d+) acres, world_seq (\d+), renew "
                         r"[\d-]+\) changed_tiles=(\d+) changed_acres=(\d+) parked=(\d+) skipped_transient=(\d+)",
                         client.text())
    check(f"{name} host logged 'snapshot epoch N sent (30 acres ...) known_tiles=K unknown_tiles=U' for peer {peer}: "
          f"{sent[-1] if sent else None}", bool(sent) and int(sent[-1][1]) == 30
          and int(sent[-1][3]) + int(sent[-1][4]) == 30 * 256)
    check(f"{name} client logged 'snapshot epoch N applied (30 acres ...) changed_tiles=C ...': "
          f"{applied[-1] if applied else None}", bool(applied) and int(applied[-1][1]) == 30)
    if not sent or not applied:
        return None
    epoch = int(applied[-1][0])
    check(f"{name} the host's sent epoch and the client's applied epoch match ({sent[-1][0]} / {epoch})",
          int(sent[-1][0]) == epoch)
    return epoch, int(applied[-1][3]), int(sent[-1][3]), int(sent[-1][4])


READY_RX_H = r"\[NET\] host: peer (\d+) identity OK \(player_id=\d+, .*\) -> READY"
READY_RX_C = r"\[NET\] client: handshake complete, town verified \(assigned peer id (\d+)\) -> READY"

def phase_main(port):
    """(a)-(e): host + client, late join/reconnect, clean shutdown."""
    # ------------------------------------------------------------------------------------------ host
    host, ok = launch_and_boot("host", ["--host", str(port), "--verbose", "--pickup-test-seed"])
    check("host listening", host.wait(r"\[NET\] hosting on UDP port %d \(protocol 2\)" % port, 60))
    check("host reached gameplay (title -> town)", ok)
    check("host world ready", host.wait(r"\[NET\]\[WORLD\] host: world ready", 30))
    check("host seeded 20 in-town fixture tiles", host.wait(r"--pickup-test-seed: fixture item placed at tile", 30, 20))
    host.shot("01_host_in_town")

    # ---------------------------------------------------------------------------------------- client
    client, ok = launch_and_boot("client", ["--connect", f"127.0.0.1:{port}", "--verbose"])
    check("client reached gameplay (title -> town)", ok)
    check("(a) client IDENTITY accepted: host logged 'peer N identity OK ... -> READY'", host.wait(READY_RX_H, 30))
    check("(a) client logged 'handshake complete, town verified (assigned peer id N) -> READY'",
          client.wait(READY_RX_C, 30))
    hp = host.find(READY_RX_H)
    cp = client.find(READY_RX_C)
    check(f"(a) host peer id == client's assigned peer id ({hp[-1] if hp else None} / {cp[-1] if cp else None})",
          bool(hp) and bool(cp) and hp[-1] == cp[-1])
    peer = int(hp[-1]) if hp else 0
    for ln in host.find(r"(\[NET\] host: peer \d+ (?:transport-connected|identity received|identity OK)[^\n]*)"):
        print("    host   |", ln)
    for ln in client.find(r"(\[NET\] client: (?:transport-connected|save loaded|context sent|handshake complete)[^\n]*)"):
        print("    client |", ln)
    client.wait(r"client: snapshot epoch \d+ applied", 30)
    time.sleep(3)
    host.shot("02_host_with_client")
    client.shot("03_client_in_town")

    # -------------------------------------------------------------------------------------------- (b)
    res = snapshot_checks("(b)", host, client, peer)
    if res:
        epoch, changed, known, unknown = res
        check(f"(b) client changed_tiles C={changed} > 0", changed > 0)
        seed_and_park_report("(b)", host, client, epoch, changed, known, unknown)
        check(f"(b) known_tiles+unknown_tiles == 7680 (known {known}, unknown {unknown})", known + unknown == 7680)

    # -------------------------------------------------------------------------------------------- (c)
    check("(c) host created the client's remote-player actor",
          host.wait(r"\[NET\]\[REMOTE\] created remote-player actor for player %d at" % peer, 30))
    check("(c) client created the host's remote-player actor (player 8)",
          client.wait(r"\[NET\]\[REMOTE\] created remote-player actor for player 8 at", 30))
    check("(c) host received the client's appearance", host.wait(r"player %d: appearance received" % peer, 15))
    check("(c) client received the host's appearance", client.wait(r"player 8: appearance received", 15))
    p_before = positions(host, peer)
    client.hold("UP", 2.0)
    time.sleep(2.5)
    p_after = positions(host, peer)
    print(f"    host's view of the client's position samples: {len(p_before)} before, {len(p_after)} after walking up")
    check("(c) walking the client changed its position as seen by the host ([NET][REMOTE][DIAG] pos samples)",
          len(p_after) > len(p_before))
    q_before = positions(client, 8)
    host.hold("DOWN", 2.0)
    time.sleep(2.5)
    q_after = positions(client, 8)
    check("(c) walking the host changed its position as seen by the client",
          len(q_after) > len(q_before))
    host.shot("04_host_after_walk")
    client.shot("05_client_after_walk")
    ct = client.text()
    rdy = ct.find("handshake complete, town verified")
    g_all = [m.start() for m in re.finditer(r"\[NET\]\[FIELD\]\[GUARD\]", ct)]
    g_after = [x for x in g_all if x > rdy]
    print(f"    client GUARD lines: {len(g_all)} total, {len(g_after)} after its READY (all before READY = "
          "field-init writes by vanilla local actors, e.g. villager-house RSV_NO footprints)")
    check("(c) host log has no [NET][FIELD][GUARD] line (host-side writes are not client-local writes)",
          not host.find(r"\[NET\]\[FIELD\]\[GUARD\]"))
    check(f"(c) no [NET][FIELD][GUARD] spam on the client after READY ({len(g_after)} after READY; {len(g_all)} "
          "before READY at field init, log-rate-limited by the game)", not g_after)
    for g in (host, client):
        print(f"    {g.label} suspect lines (first 3): {suspects(g)[:3]}")

    # -------------------------------------------------------------------------------------------- (d)
    n_ready_before = len(host.find(READY_RX_H))
    client.kill()
    check("(d) host noticed the killed client (peer N disconnected, transport timeout)",
          host.wait(r"\[NET\] host: peer %d disconnected" % peer, 20))
    time.sleep(1.0)
    client2, ok = launch_and_boot("client2", ["--connect", f"127.0.0.1:{port}", "--verbose"])
    check("(d) fresh client reached gameplay", ok)
    check("(d) host: peer READY again (identity OK ... -> READY count grew)",
          host.wait(READY_RX_H, 30, count=n_ready_before + 1))
    check("(d) client2: handshake complete -> READY", client2.wait(READY_RX_C, 30))
    hp2 = host.find(READY_RX_H)
    check(f"(d) the freed slot was reused (peer {hp2[-1] if hp2 else None}, was {peer})", bool(hp2) and int(hp2[-1]) == peer)
    client2.wait(r"client: snapshot epoch \d+ applied", 30)
    res2 = snapshot_checks("(d)", host, client2, peer)
    if res2 and res:
        check(f"(d) NEW snapshot epoch for the rejoined client ({res2[0]} != {res[0]})", res2[0] != res[0])
        check(f"(d) rejoined client changed_tiles={res2[1]} > 0", res2[1] > 0)
        seed_and_park_report("(d)", host, client2, res2[0], res2[1], res2[2], res2[3])
    check("(d) host logged a per-peer reset path: 'transport-connected' again for that slot",
          len(host.find(r"\[NET\] host: peer %d transport-connected" % peer)) >= 2)
    check("(d) host re-created the remote-player actor for the new client",
          host.wait(r"\[NET\]\[REMOTE\] created remote-player actor for player %d at" % peer, 30, count=2))
    host.shot("06_host_after_rejoin")
    client2.shot("07_client2_in_town")
    for ln in host.find(r"(\[NET\] host: peer \d+ (?:disconnected|transport-connected|identity OK)[^\n]*)"):
        print("    host   |", ln)

    # -------------------------------------------------------------------------------------------- (e)
    c2t = client2.text()
    c2_after = [m.start() for m in re.finditer(r"\[NET\]\[FIELD\]\[GUARD\]", c2t)
                if m.start() > c2t.find("handshake complete, town verified")]
    check(f"(e) no [NET][FIELD][GUARD] line in the host log; none after READY in client2 ({len(c2_after)})",
          not host.find(r"\[NET\]\[FIELD\]\[GUARD\]") and not c2_after)
    client2.close_clean()
    time.sleep(1.0)
    host.close_clean()
    for g in (host, client, client2):
        tot, aft = guard_counts(g)
        print(f"    {g.label}: GUARD lines {tot} total, {aft} after its READY")
        sus = suspects(g)
        print(f"    {g.label}: suspect log lines = {sus[:8]}")
        check(f"(e) {g.label}: no unexpected error/assert/fail lines in the log", not sus)
        check(f"(e) {g.label}: no [NET][FIELD][GUARD] " + ("line at all (host-side writes)" if g.label == "host" else
              "line after its own READY"), (tot == 0) if g.label == "host" else (aft == 0))
    check("(e) host logged a normal networking shutdown", "shutting down networking (was host)" in host.text())
    check("(e) client2 logged a normal networking shutdown", "shutting down networking (was client)" in client2.text())
    ends = {(lbl): (how, rc, clean) for lbl, _pid, how, rc, clean in exit_table}
    check(f"(e) host and client2 exited cleanly with code 0 (host {ends.get('host')}, client2 {ends.get('client2')})",
          ends.get("host", (0, 1, False))[2] is True and ends.get("client2", (0, 1, False))[2] is True)



def phase_reverse(port):
    """(f): client started BEFORE the host."""
    time.sleep(2.0)
    rc_, ok = launch_and_boot("rev_client", ["--connect", f"127.0.0.1:{port}", "--verbose"])
    check("(f) client (started BEFORE any host) reached gameplay while connecting to nothing", ok)
    time.sleep(3.0)
    check("(f) client did not become READY without a host and kept running", rc_.alive()
          and not rc_.find(r"handshake complete"))
    rc_.shot("08_reverse_client_alone")
    rh, ok = launch_and_boot("rev_host", ["--host", str(port), "--verbose", "--pickup-test-seed"])
    check("(f) host listening", rh.wait(r"\[NET\] hosting on UDP port %d" % port, 60))
    check("(f) host reached gameplay", ok)
    check("(f) host: the already-waiting client became READY (identity OK ... -> READY)", rh.wait(READY_RX_H, 40))
    check("(f) client: handshake complete -> READY", rc_.wait(READY_RX_C, 40))
    rc_.wait(r"client: snapshot epoch \d+ applied", 30)
    snapshot_checks("(f)", rh, rc_, 0)
    time.sleep(2.0)
    rh.shot("09_reverse_host")
    rc_.shot("10_reverse_client")
    for g in (rh, rc_):
        g.close_clean() if g.alive() else None
    ends = {lbl: (how, rc, clean) for lbl, _pid, how, rc, clean in exit_table}
    check(f"(f) rev_host and rev_client exited cleanly with code 0 (rev_host {ends.get('rev_host')}, "
          f"rev_client {ends.get('rev_client')})", ends.get("rev_host", (0, 1, False))[2] is True
          and ends.get("rev_client", (0, 1, False))[2] is True)
    tot, aft = guard_counts(rc_)
    check(f"(f) rev_client: no [NET][FIELD][GUARD] after READY ({aft} after; {tot} total before READY)", aft == 0)
    check("(f) rev_host: no [NET][FIELD][GUARD] line", guard_counts(rh)[0] == 0)
    for g in (rh, rc_):
        sus = suspects(g)
        print(f"    {g.label}: suspect log lines = {sus[:8]}")
        check(f"(f) {g.label}: no unexpected error/assert/fail lines", not sus)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7788)
    ap.add_argument("--skip-reverse", action="store_true")
    ap.add_argument("--only-reverse", action="store_true")
    a = ap.parse_args()
    port = a.port
    os.makedirs(OUT, exist_ok=True)
    running = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AnimalCrossing.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
    if "AnimalCrossing.exe" in running:
        print("[smoke] ABORT: an AnimalCrossing.exe is already running; stop it first")
        return 1

    try:
        if not a.only_reverse:
            phase_main(port)
        if not a.skip_reverse:
            if not a.only_reverse:
                time.sleep(2.0)
            phase_reverse(port)
    finally:
        for g in procs:
            if g.alive():
                print(f"[smoke] cleanup: terminating still-running {g.label} (pid {g.proc.pid})")
                g.kill()
    print("=" * 72)
    print("PROCESS EXIT TABLE")
    for lbl, pid, how, rc, clean in exit_table:
        print(f"  {lbl:<11} pid {pid:<7} {how:<52} exit code {rc}  clean={clean}")
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
