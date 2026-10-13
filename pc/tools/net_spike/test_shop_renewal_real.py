#!/usr/bin/env python3
"""test_shop_renewal_real.py - PROTOCOL test (real host process + FakeClient): a due shop renewal on a host that stays outdoors.

Phases (one host each, --host --bootstrap-resident 0, the host's own player parked outdoors in SCENE_FG):
  RN-base  no hook: a plain SHOP_BUY is APPLIED (the normal purchase before any renewal still works).
  RN-hold  AC_TEST_RENEWSHOP_DUE=hold (TEST-ONLY): a renewal due NOW is armed and the fix's reload is suppressed -> the pre-fix state: SHOP_BUY REJECTED NOT_AVAILABLE.
  RN-fix   AC_TEST_RENEWSHOP_DUE=1: the same due renewal; the host does ONE same-position SCENE_FG reload, the vanilla Shop_Level ct renews (event cleared,
           level 0 -> 1, the mirror shows the new level) and a SHOP_BUY from the NEW stock is APPLIED; no second reload.
  RN-takeover  (the announced shop upgrade, feature ON) AC_TEST_RENEWSHOP_DUE=1 arms the same vanilla due renewal, but the host REPLACES the vanilla schedule: the event is
           cleared ('a vanilla shop renewal schedule was found ... replaced by the announced upgrade'), the upgrade becomes PENDING (Nook announces it), no vanilla reload happens.
The RN-base / RN-hold / RN-fix / RN-twice phases pin the LEGACY vanilla 2-day renewal path and therefore run with the A/B switch AC_MP_SHOP_UPGRADE=0 (deliberate change: with
the announced shop upgrade ON, the default, a host never keeps a vanilla renewal schedule; that path is covered by test_shop_upgrade_protocol.py).
Usage: NET_SPIKE_GAME_BIN=<pc\\build64\\bin_fixture4_wN> python test_shop_renewal_real.py [--port 16100]
"""
import argparse
import os
import re
import shutil
import sys
import time

import net_spike_lib as L
import test_txn_protocol as TP
import test_ts_protocol as TS
import test_shop_protocol as SP
from test_txn_protocol import APPLIED, REJECTED, R

ENV = "AC_TEST_RENEWSHOP_DUE"


def wait_log(run, pat, timeout):
    end = time.time() + timeout
    while time.time() < end:
        m = re.search(pat, run.log())
        if m:
            return m
        L.pump_sleep(0.25)
    return None


def try_buy(run, c, blob):
    cand = SP.listed(run, blob, 1)
    if not cand:
        run.check("setup: a buyable LISTED item exists in the current stock", False)
        return None
    i, it, p = cand[0]
    s = SP.free_slots(c, 1)[0]
    _rid, _sent, r = SP.buy(run, c, s, it, i, p, p + 100)
    return r


def phase_base(run):
    a = run.ready("A", run.r1)
    sa = SP.wait_shop(a, 1, 6.0)
    r = try_buy(run, a, sa[1]) if sa else None
    run.tx_ok("RN-base a normal SHOP_BUY (no renewal pending) is APPLIED", r, APPLIED, R["NONE"])


def phase_hold(run):
    a = run.ready("A", run.r1)  # the hook arms only once a READY peer is connected
    m = wait_log(run, r"AC_TEST_RENEWSHOP_DUE=hold: renewal armed: level (\d+) qualifying (\d+) .* event 1 shop_open (\d+)", 20.0)
    run.check("RN-hold the TEST hook armed a due renewal (level < qualifying, event set)", m is not None and int(m.group(1)) < int(m.group(2)))
    if m is not None and int(m.group(3)) == 2:
        # mSP_ShopOpen() == OPEN despite the due renewal: an earlier vanilla branch wins (mEv_CheckFirstJob() of the host's OWN resident -- still doing
        # Nook's chores -- or a bargain day). Not reproducible with this host; a dedicated observer (player_no 4) has no first job. SKIP, not a pass.
        print("SKIP - RN-hold pre-fix NOT_AVAILABLE not reproducible: the host's mSP_ShopOpen() is OPEN (first-job / bargain branch masks the renewal)")
        return
    run.check("RN-hold the host's own mSP_ShopOpen() is RENEW (3) after arming", m is not None and int(m.group(3)) == 3)
    L.pump_sleep(2.0)
    sa = a.ts_latest(SP.SVC_SHOP)
    r = try_buy(run, a, sa[1]) if sa else None
    run.tx_ok("RN-hold PRE-FIX behaviour reproduced: the host refuses SHOP_BUY NOT_AVAILABLE ('the shop is closed') while the renewal stays pending", r, REJECTED, R["NOT_AVAILABLE"])
    run.check("RN-hold no reload was issued (suppressed)", "same-position SCENE_FG reload so the vanilla shop actor renews" not in run.log())


RELOAD_RX = r"same-position SCENE_FG reload so the vanilla shop actor renews: res=1"
DONE_RX = r"shop renewal reload done: event (\S+) .*?shop level (\d+) \(qualifying (\d+)\)"


def after_reloads(run, a, n, tag):
    end = time.time() + 40.0
    while time.time() < end and len(re.findall(DONE_RX, run.log())) < n:
        L.pump_sleep(0.25)
    done = re.findall(DONE_RX, run.log())
    run.check("%s %d reload(s) done, each cleared the event with level == qualifying (%s)" % (tag, n, done),
              len(done) == n and all(e == "cleared" and lv == q for e, lv, q in done))
    run.check("%s levels advance one tier per renewal (%s)" % (tag, [d[1] for d in done]), [int(d[1]) for d in done] == list(range(int(done[0][1]), int(done[0][1]) + n)) if done else False)
    L.pump_sleep(4.0)
    run.check("%s the connected peer stayed connected across the host's scene reload(s)" % tag, a.state == a.STATE_CONNECTED and run.host.alive())
    sa = a.ts_latest(SP.SVC_SHOP)
    blob = sa[1] if sa else None
    run.check("%s the client's shop mirror shows the new level (%s)" % (tag, blob and SP.shop_level(blob)), blob is not None and done and SP.shop_level(blob) == int(done[-1][1]))
    r = try_buy(run, a, blob) if blob else None
    run.tx_ok("%s SHOP_BUY from the renewed stock (same session, no reconnect) is APPLIED" % tag, r, APPLIED, R["NONE"])
    L.pump_sleep(3.0)
    run.check("%s exactly %d renewal reload(s) issued (one per renewal_time, no loop)" % (tag, n), len(re.findall(RELOAD_RX, run.log())) == n)


def phase_fix(run):
    a = run.ready("A", run.r1)
    after_reloads(run, a, 1, "RN-fix")


def phase_twice(run):
    a = run.ready("A", run.r1)
    after_reloads(run, a, 2, "RN-twice")


def phase_takeover(run):
    run.ready("A", run.r1)
    end = time.time() + 20.0
    while time.time() < end and "PENDING: the town qualifies" not in run.log():
        L.pump_sleep(0.25)
    log = run.log()
    run.check("RN-takeover the armed vanilla renewal was REPLACED (event cleared) by the announced upgrade",
              "a vanilla shop renewal schedule was found (mEv_SAVED_RENEWSHOP): replaced by the announced upgrade" in log)
    run.check("RN-takeover the upgrade is PENDING (Nook announces it at the next talk)", "PENDING: the town qualifies" in log)
    L.pump_sleep(3.0)
    run.check("RN-takeover no vanilla renewal reload was issued", "same-position SCENE_FG reload so the vanilla shop actor renews" not in run.log())


# (tag, AC_TEST_RENEWSHOP_DUE, body, AC_MP_SHOP_UPGRADE): the legacy phases pin the vanilla 2-day path (A/B switch off); RN-takeover runs with the default (ON)
PHASES = [("RN-base", None, phase_base, "0"), ("RN-hold", "hold", phase_hold, "0"), ("RN-fix", "1", phase_fix, "0"), ("RN-twice", "twice", phase_twice, "0"),
          ("RN-takeover", "1", phase_takeover, None)]


def observer_phase(results, port, tag, body, restore):
    print("=" * 72)
    print("[%s] --host-observer host" % tag)
    host = L.HostProcess(port=port, extra_args=["--host-observer"], log_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "renew_obs_%s_host.log" % tag),
                         bin_dir=L.GAME_BIN_DIR).start()
    ok = host.wait_listening(60.0) and host.boot_to_observer(timeout=90.0)
    L.check("[%s] observer host reached observer active + world ready" % tag, ok, results)
    if ok:
        L.resolve_host_town("127.0.0.1", port)
    run = TP.Run("127.0.0.1", port, host, results, None, -1, [1, 2, 3])
    try:
        if ok:
            body(run)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("[%s] phase raised %r" % (tag, exc), False, results)
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        L.check("[%s] host did not crash (exit code before stop: %s)" % (tag, rc), rc is None, results)
        restore()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16100)
    ap.add_argument("--only", default="")
    ap.add_argument("--observer", action="store_true", help="host = --host-observer (the dedicated server's observer identity, player_no 4)")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a pc\\build64\\bin_fixture4* copy only", file=sys.stderr)
        return 2
    only = [x for x in args.only.split(",") if x]
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "shop_renewal_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        for i, (tag, val, body, upg) in enumerate(PHASES):
            if only and tag not in only:
                continue
            if val is None:
                os.environ.pop(ENV, None)
            else:
                os.environ[ENV] = val
            if upg is None:
                os.environ.pop("AC_MP_SHOP_UPGRADE", None)
            else:
                os.environ["AC_MP_SHOP_UPGRADE"] = upg
            if args.observer:
                observer_phase(results, args.port + 10 + i, tag + "-obs", body, restore)
            else:
                TS.run_phase(results, "127.0.0.1", args.port + i, tag, [], body, snap_gci, restore)
    finally:
        os.environ.pop(ENV, None)
        os.environ.pop("AC_MP_SHOP_UPGRADE", None)
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
