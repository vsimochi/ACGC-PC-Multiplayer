#!/usr/bin/env python3
"""test_day_roll_real.py - REAL host process + FakeClient(s): the day rollover on a long-running host (docs/multiplayer-guest-roadmap.md
"Day rollover on a long-running host", TODO WS-01). The TEST-ONLY hook AC_TEST_DAY_ROLL_AT=<sec>,<hhmm> (test build + AC_TEST_HOOKS=1) moves the HOST game clock
to tomorrow hh:mm:05 <sec> seconds after a READY peer is connected (lbRTC_SetTime: a real clock change the clients follow), so the real detection path fires.

Phases (each on a fresh copy of the fixture save; observer = --host-observer, the dedicated server's identity):
  DR-obs     observer host, clock -> tomorrow 06:30: ONE roll (new day + growth), the new-day init ran, nothing left due, saved; client A sees the new renew time
             (WORLD_META) and the host's post-roll weather; a client B that joins afterwards gets a snapshot identical to A's live-updated world (tiles, villagers);
             A's pockets / wallet and every resident record in the saved GCI are unchanged; no second roll. Then the SAME save restarted without the hook:
             no roll is due (idempotent: the roll's own bookkeeping was saved).
  DR-shop    observer host, A announces Nook's shop (SCENE_SHOP0) before the clock moves: the roll is DEFERRED (shop / villager house) and does not start
             while A stays; A walks out (SCENE_FG) -> the roll starts and completes.
  DR-off     AC_MP_DAY_ROLL=0: the clock moves, nothing is due / started.
  DR-mid     the host is killed right after the new-day init ran (before the save); the restart boots normally (vanilla boot) with no roll due and
             unchanged resident records.
  DR-listen  listen host (--host --bootstrap-resident N): one roll, done, saved.
  DR-ded     --dedicated server + stdin console: `day` status / `day roll now` / `day advance 0010` (TEST-ONLY) -> a roll with no client connected, server.log.
Usage: NET_SPIKE_GAME_BIN=<pc\\build64\\bin_fixture4_dr_*> python test_day_roll_real.py [--port 21100] [--only DR-obs,...]"""
import argparse
import hashlib
import os
import re
import shutil
import struct
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
import test_txn_protocol as TP  # noqa: E402

SCENE_FG, SCENE_SHOP0 = 7, 9
CLOCK_RX = r"\[SERVER\] day roll: \[TEST-ONLY\] AC_TEST_DAY_ROLL_AT: the host game clock was moved to (\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d)"
DONE_RX = r"\[SERVER\] day roll: done \(([^)]*)\): new-day init date (\d{4})-(\d\d)-(\d\d), .*?still due: (\S+(?: \S+)*?); weather (-?\d+) -> (-?\d+), villagers (\d+) -> (\d+)"


def scene_msg(scene_id, seq):
    return struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, scene_id, L.PC_NETGAME_SCENE_FLAG_IN_TOWN if scene_id == SCENE_FG else 0, 0, 0, seq)


def wait_log(run_or_host, pat, timeout, off=0):
    host = run_or_host.host if hasattr(run_or_host, "host") else run_or_host
    end = time.time() + timeout
    while time.time() < end:
        m = re.search(pat, host.log_text()[off:])
        if m:
            return m
        if not host.alive():
            return None
        L.pump_sleep(0.25)
    return None


def files_digest(d):
    out = {}
    if os.path.isdir(d):
        for root, _ds, fs in os.walk(d):
            for f in fs:
                p = os.path.join(root, f)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, d)] = hashlib.sha1(fh.read()).hexdigest()
    return out


def start_observer(port, tag, env=None):
    host = L.HostProcess(port=port, extra_args=["--host-observer"], log_path=os.path.join(HERE, "dayroll_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR, env=env).start()
    ok = host.wait_listening(60.0) and host.boot_to_observer(timeout=120.0)
    if ok:
        L.resolve_host_town("127.0.0.1", port)
    return host, ok


def start_listen(port, tag, env=None):
    slot = L.TEST_HOST_RESIDENT
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(slot)], log_path=os.path.join(HERE, "dayroll_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR,
                         env=env).start()
    ok = host.wait_listening(60.0) and host.boot_to_field(timeout=120.0, slot=slot)
    if ok:
        L.resolve_host_town("127.0.0.1", port)
    return host, ok


def residents_other_than_host(host_slot):
    return [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]


def records(gci):
    return [L.record_from_gci(gci, i) for i in range(4)]


def ready_in_town(run, label, slot):
    """a FakeClient that, like a real client, announces its scene (the town field) right after READY"""
    c = run.ready(label, slot)
    c.send_reliable(scene_msg(SCENE_FG, 1))
    L.pump_sleep(0.5)
    return c


def world_tiles(c):
    return {k: list(v) for k, v in c.world.items.items()}


# ---------------------------------------------------------------------------------------------------------------------------------------------------
def phase_obs(results, port, gci_path, mp_dir):
    check = lambda d, c: L.check("[DR-obs] " + d, c, results)  # noqa: E731
    pre_recs = records(gci_path)
    pre_mp = files_digest(mp_dir)
    host, ok = start_observer(port, "obs", env={"AC_TEST_DAY_ROLL_AT": "6,0630"})
    check("observer host ready", ok)
    if not ok:
        host.stop()
        return
    run = TP.Run("127.0.0.1", port, host, results, None, -1, residents_other_than_host(-1)[:3])
    try:
        a = ready_in_town(run, "A", run.r1)
        pre_img = a.txn_pre_image()
        ev0 = a.wait_ts_state(L.PC_NETGAME_TS_EVENT, 5.0)
        mclk = wait_log(run, CLOCK_RX, 40.0)
        world_log_mark = len(a.world_log)
        check("the TEST hook moved the host clock to tomorrow 06:30 (%s)" % (mclk.group(0) if mclk else None), mclk is not None)
        m = wait_log(run, DONE_RX, 90.0)
        log = run.log()
        check("one roll was due: new day + growth", re.search(r"day roll: due: new day \+ growth at", log))
        check("it started with a same-position town reload", "day roll: starting (new day + growth): same-position town reload" in log and "same-position SCENE_FG reload (day roll): res=1" in log)
        check("the new-day boot init ran inside the reload", re.search(r"day roll: new-day init ran \(binding player_no 4\)", log))
        check("done: the new-day init date = the new date and nothing is still due (%s)" % (m.group(0)[:200] if m else None),
              m is not None and mclk is not None and m.group(2, 3, 4) == mclk.group(1, 2, 3) and m.group(5) == "none")
        st = log.find("day roll: starting")
        check("growth ran in the reload's field build (all_grow_renew_time = the new date, after 6 AM)",
              st >= 0 and mclk is not None and re.search(r"day roll: done .*all_grow_renew_time %s-%s-%s 06:3" % mclk.group(1, 2, 3), log[st:]))
        ms = wait_log(run, r"\[SERVER\] day roll: saved", 30.0)
        check("the roll was saved (authoritative save)", ms is not None)
        L.pump_sleep(4.0)
        new_date = tuple(int(x) for x in mclk.group(1, 2, 3)) if mclk else None
        metas = [x for x in a.meta_log if new_date and tuple(x["renew_time"][:3]) == new_date]
        check("client A received a WORLD_META with the new all_grow_renew_time date (%d)" % len(metas), len(metas) >= 1)
        if m is not None and a.meta_log:
            check("client A's weather == the host's post-roll weather (%s vs %s)" % (a.meta_log[-1]["weather"], m.group(7)), a.meta_log[-1]["weather"] == int(m.group(7)))
        check("client A stayed connected across the host's reload", a.state == a.STATE_CONNECTED and host.alive())
        mg = re.search(r"\[NET\]\[GROW\] scene 7: field-init growth changed (\d+) town acre", log[st:]) if st >= 0 else None
        live = [k for _i, k, _o in a.world_log[world_log_mark:] if k in (L.WORLD_DELTA, L.WORLD_BLOCK, L.WORLD_SNAPSHOT_BEGIN)]
        check("the growth changed town acres (%s) and client A received the field changes live (%d field messages)" % (mg.group(1) if mg else None, len(live)),
              mg is not None and int(mg.group(1)) > 0 and len(live) > 0)
        ev1 = a.ts_latest(L.PC_NETGAME_TS_EVENT)
        check("client A received the host's re-initialised event state (TOWN_SVC EVENT seq %s -> %s; the boot init re-rolls special / weekly events)" %
              (ev0[0].seq if ev0 else None, ev1[0].seq if ev1 else None),
              ev0 is not None and ev1 is not None and (ev1[0].seq > ev0[0].seq or "service 5 (EVENT) state" not in log[st:]))
        b = ready_in_town(run, "B", run.r2)
        L.pump_sleep(3.0)
        wa, wb = world_tiles(a), world_tiles(b)
        diff = [k for k in wb if k in wa and wa[k] != wb[k]]
        check("a client joining after the roll gets the same town tiles A has live (%d acres differ)" % len(diff), wb and not diff)
        arr = [k for k, _o in a.villager_log]
        moved = m is not None and int(m.group(8)) != int(m.group(9))
        check("villagers: the host's count %s -> %s and client A got %d arrival / departure message(s) (a move-in / move-out reaches clients through msgs 18 / 19)" %
              (m.group(8) if m else None, m.group(9) if m else None, len(arr)), m is not None and (len(arr) > 0) == moved)
        check("A's pockets / wallet are unchanged by the roll", a.txn_pre_image() == pre_img)
        L.pump_sleep(8.0)
        check("exactly one roll (no loop)", len(re.findall(r"day roll: starting", run.log())) == 1)
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        check("host did not crash (exit code before stop: %s)" % rc, rc is None)
    post_recs = records(gci_path)
    check("every resident record in the saved GCI is byte-identical (pockets, wallet, dresser...): %s" % [i for i in range(4) if pre_recs[i] != post_recs[i]],
          pre_recs == post_recs)
    post_mp = files_digest(mp_dir)
    changed = sorted(k for k in set(pre_mp) | set(post_mp) if pre_mp.get(k) != post_mp.get(k) and "guest" in k.lower())
    check("no guest file changed (%s)" % changed, not changed)
    # restart on the SAME save, no hook: the roll's own bookkeeping was saved -> nothing due
    host, ok = start_observer(port + 1, "obs_restart")
    check("restart on the rolled save: observer ready", ok)
    try:
        if ok:
            L.pump_sleep(12.0)
            check("restart: no roll is due (idempotent: the boot itself is the new-day init for the saved clock)", "day roll: due" not in host.log_text())
    finally:
        rc = host.stop()
        check("restart host did not crash (%s)" % rc, rc is None)


def phase_shop(results, port):
    check = lambda d, c: L.check("[DR-shop] " + d, c, results)  # noqa: E731
    host, ok = start_observer(port, "shop", env={"AC_TEST_DAY_ROLL_AT": "8,0010"})
    check("observer host ready", ok)
    if not ok:
        host.stop()
        return
    run = TP.Run("127.0.0.1", port, host, results, None, -1, residents_other_than_host(-1)[:3])
    try:
        a = run.ready("A", run.r1)
        a.send_reliable(scene_msg(SCENE_SHOP0, 2))  # A's first announced scene is Nook's shop (it is inside when the day changes)
        check("the clock moved", wait_log(run, CLOCK_RX, 40.0) is not None)
        md = wait_log(run, r"day roll: deferred \(new day\): a player is in a shop or a villager house", 15.0)
        check("the due roll is DEFERRED while A is in Nook's shop", md is not None)
        L.pump_sleep(8.0)
        check("no roll started while A stayed in the shop", "day roll: starting" not in run.log())
        off = len(run.log())
        a.send_reliable(scene_msg(SCENE_FG, 3))
        check("A walked out -> the roll starts", wait_log(run, r"day roll: starting \(new day\)", 15.0, off) is not None)
        m = wait_log(run, DONE_RX, 60.0, off)
        check("... and completes with nothing left due", m is not None and m.group(5) == "none")
        check("... and is saved", wait_log(run, r"day roll: saved", 30.0, off) is not None)
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        check("host did not crash (%s)" % rc, rc is None)


def phase_off(results, port):
    check = lambda d, c: L.check("[DR-off] " + d, c, results)  # noqa: E731
    host, ok = start_observer(port, "off", env={"AC_TEST_DAY_ROLL_AT": "4,0010", "AC_MP_DAY_ROLL": "0"})
    check("observer host ready", ok)
    run = TP.Run("127.0.0.1", port, host, results, None, -1, residents_other_than_host(-1)[:3])
    try:
        if ok:
            ready_in_town(run, "A", run.r1)
            check("the clock moved (the hook itself still runs)", wait_log(run, CLOCK_RX, 40.0) is not None)
            L.pump_sleep(10.0)
            log = run.log()
            check("AC_MP_DAY_ROLL=0: nothing due, nothing started, no reload", "day roll: due" not in log and "day roll: starting" not in log and "reload (day roll)" not in log)
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        check("host did not crash (%s)" % rc, rc is None)


def phase_mid(results, port, gci_path):
    check = lambda d, c: L.check("[DR-mid] " + d, c, results)  # noqa: E731
    pre_recs = records(gci_path)
    host, ok = start_observer(port, "mid", env={"AC_TEST_DAY_ROLL_AT": "5,0010"})
    check("observer host ready", ok)
    run = TP.Run("127.0.0.1", port, host, results, None, -1, residents_other_than_host(-1)[:3])
    try:
        if ok:
            ready_in_town(run, "A", run.r1)
            check("the new-day init ran", wait_log(run, r"day roll: new-day init ran", 60.0) is not None)
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        host.proc.kill()  # mid-roll: no final save
        host.stop()
    check("killed before the roll's save", "day roll: saved" not in host.log_text())
    host, ok = start_observer(port + 1, "mid_restart")
    check("restart after a kill mid-roll: observer ready (the vanilla boot ran)", ok)
    try:
        if ok:
            L.pump_sleep(12.0)
            check("restart: no roll due (the boot itself is the new-day init; the moved clock was never saved)", "day roll: due" not in host.log_text())
    finally:
        rc = host.stop()
        check("restart host did not crash (%s)" % rc, rc is None)
    check("resident records unchanged after the kill + restart", records(gci_path) == pre_recs)


def phase_listen(results, port):
    check = lambda d, c: L.check("[DR-listen] " + d, c, results)  # noqa: E731
    host, ok = start_listen(port, "listen", env={"AC_TEST_DAY_ROLL_AT": "6,0010"})
    check("listen host (own resident) ready in the town", ok)
    run = TP.Run("127.0.0.1", port, host, results, None, L.TEST_HOST_RESIDENT, residents_other_than_host(L.TEST_HOST_RESIDENT)[:3])
    try:
        if ok:
            a = ready_in_town(run, "A", run.r1)
            check("the clock moved", wait_log(run, CLOCK_RX, 40.0) is not None)
            m = wait_log(run, DONE_RX, 90.0)
            log = run.log()
            check("the new-day init ran for the host's own resident binding", re.search(r"day roll: new-day init ran \(binding player_no %d\)" % L.TEST_HOST_RESIDENT, log))
            check("done with nothing left due (%s)" % (m.group(0)[:160] if m else None), m is not None and m.group(5) == "none")
            check("saved", wait_log(run, r"day roll: saved", 30.0) is not None)
            check("client A stayed connected", a.state == a.STATE_CONNECTED and host.alive())
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        check("host did not crash (%s)" % rc, rc is None)


def ask(h, cmd, end_rx, timeout=10.0):
    off = len(h.log_text())
    h.send_line(cmd)
    mo = h.wait_for_log(end_rx, timeout, since_offset=off)
    time.sleep(0.3)
    return h.log_text()[off:] if mo is not None else None


def phase_ded(results, port):
    """--dedicated server, stdin console: `day` status, `day roll now` with nothing due, `day advance 0010` (TEST-ONLY) -> a real roll with NO client
    connected (the observer alone), `day` afterwards reports it."""
    import test_dedicated_runtime as DR
    check = lambda d, c: L.check("[DR-ded] " + d, c, results)  # noqa: E731
    servers = os.path.join(L.GAME_BIN_DIR, "servers")
    snap = os.path.join(os.environ.get("TEMP", "."), "day_roll_servers_%d" % os.getpid())
    shutil.rmtree(snap, ignore_errors=True)
    if os.path.isdir(servers):
        shutil.copytree(servers, snap)
    h = DR.start(port, "dayroll_ded", HERE, verbose=False, new_group=True)
    try:
        if h.wait_for_log(r"Use this town", 12.0) is not None:
            h.send_line("1")
        ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=150.0)
        check("dedicated server ready", ok)
        if not ok:
            return
        t = ask(h, "help", r"  stop     graceful shutdown")
        check("help documents `day`", t is not None and "  day [status] | day roll now" in t)
        t = ask(h, "day", r"  rolls this run: .*")
        check("`day` reports ON, the game date, the last new-day init, state idle (%r)" % (t or "")[:300],
              t is not None and "[DEDICATED] day: rollover ON; game date " in t and "last new-day init " in t and "state: idle" in t and "rolls this run: 0; last: none" in t)
        t = ask(h, "day roll now", r"  rolls this run: .*")
        check("`day roll now` with nothing due says so", t is not None and "DAY ROLL: nothing is due" in t)
        t = ask(h, "day bogus", r"usage: day")
        check("a bad sub-command prints the usage", t is not None)
        off = len(h.log_text())
        t = ask(h, "day advance 0010", r"  rolls this run: .*")
        check("`day advance 0010` (TEST-ONLY) moves the game clock to tomorrow 00:10", t is not None and "DAY ADVANCE: the game clock moved to tomorrow" in t)
        mo = h.wait_for_log(r"\[SERVER\] day roll: done \(new day\)", 60.0, since_offset=off)
        check("the roll ran on the dedicated server with no client connected", mo is not None)
        check("... and was saved", h.wait_for_log(r"\[SERVER\] day roll: saved", 30.0, since_offset=off) is not None)
        t = ask(h, "day", r"  rolls this run: .*")
        check("`day` afterwards: idle, 1 roll, the last result (%r)" % (t or "")[-200:], t is not None and "state: idle" in t and "rolls this run: 1; last: " in t and "done" in t)
        srv_log = os.path.join(servers, "default", "logs", "server.log")
        try:
            with open(srv_log, "rb") as f:
                sl = f.read().decode("utf-8", "replace")
        except OSError:
            sl = ""
        check("the roll is in servers/<id>/logs/server.log", "day roll: done (new day)" in sl)
    finally:
        h.send_line("stop")
        rc = h.wait_exit(30.0)
        if rc is None:
            h.stop()
        check("dedicated server stopped cleanly (exit %s)" % rc, rc == 0)
        shutil.rmtree(servers, ignore_errors=True)
        if os.path.isdir(snap):
            shutil.copytree(snap, servers)
            shutil.rmtree(snap, ignore_errors=True)


PHASES = ["DR-obs", "DR-shop", "DR-off", "DR-mid", "DR-listen", "DR-ded"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=21100)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_dr"):
        print("REFUSING: run on a pc\\build64\\bin_fixture4_dr_* copy only", file=sys.stderr)
        return 2
    if not (21000 <= args.port and args.port + 20 <= 21999):
        print("REFUSING: ports 21000-21999 only", file=sys.stderr)
        return 2
    only = [x for x in args.only.split(",") if x]
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    mp_dir = os.path.join(save_dir, "mp")
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "day_roll_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    bodies = {"DR-obs": lambda p: phase_obs(results, p, gci_path, mp_dir), "DR-shop": lambda p: phase_shop(results, p), "DR-off": lambda p: phase_off(results, p),
              "DR-mid": lambda p: phase_mid(results, p, gci_path), "DR-listen": lambda p: phase_listen(results, p), "DR-ded": lambda p: phase_ded(results, p)}
    try:
        for i, tag in enumerate(PHASES):
            if only and tag not in only:
                continue
            print("=" * 72 + "\n[%s]" % tag)
            try:
                bodies[tag](args.port + 4 * i)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                L.check("[%s] phase raised %r" % (tag, exc), False, results)
            restore()
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
