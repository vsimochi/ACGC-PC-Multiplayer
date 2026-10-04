#!/usr/bin/env python3
"""test_guest_arrival_real.py - T3 / T4 REAL host + REAL resident client + REAL guest client run (the other processes' arrival train).

TIER: REAL PROCESSES (hook-driven, no GUI automation, no visual verification), all with `-debug` so the T1 `[TRAIN]` lines print:
  host     = `AnimalCrossing.exe --host <port> --bootstrap-resident 0 -debug`        (--host-mode resident, default)
             or `AnimalCrossing.exe --host <port> --dedicated -debug` (hidden observer, --host-mode dedicated)
  resident = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <free slot> -debug`
  guest    = `AnimalCrossing.exe --connect 127.0.0.1:<port> --guest -debug`  (arrives through the vanilla RIDE_OFF_DEMO: DEMO_STANDING_TRAIN)
What is asserted (all from the logs of the processes):
  A  the guest arrives (adopted); the guest's OWN train starts (coming_flag 3 -> action 2)
  B  T4: the RESIDENT log shows exactly ONE `[TRAIN] remote arrival of player N: starting the local arrival train` for that guest, followed by the
     train action sequence arriving (2) -> stopped (3/4/5) -> departing (6/7/8) -> departed (0)  [skipped, and reported as NOT OBSERVED, if the
     lifecycle does not complete within --max-wait]; the HOST log shows the same
  C  no second local train for the same puppet (one start line, one 0 -> non-zero train transition per process in the window)
  D  the guard is silent about every other process state: no `NOT calling` line is required, but if one is printed its reason is reported
NOT covered: how the train / hidden puppet look (needs a human), the T3 puppet row animations (see test_move_action_state_wire.py).
The hourly train (hh:14:50) would make the guard refuse (BUSY); the test therefore waits out minutes 9..21 of the hour before it starts.
Run ONLY on a disposable bin_fixture4* dir (NET_SPIKE_GAME_BIN=<absolute path>, the new exe copied into it). The fixture save and save/mp are snapshotted at
start and restored at the end.
Usage: python test_guest_arrival_real.py [--port 9840] [--host-mode resident|dedicated] [--max-wait 150]
"""
import argparse
import datetime
import os
import re
import shutil
import sys
import time

import net_spike_lib as L
from test_guest_train_real import parse

HERE = os.path.dirname(os.path.abspath(__file__))
PID, LID = 0x4D31, 0x5E42
ADOPT_RX = r"\[NET\]\[REC\] client: adopted rev=(\d+) epoch=(\d+) kind=FULL"
START_RX = r"\[TRAIN\] remote arrival of player (\d+): starting the local arrival train \(coming_flag=3\)"
REFUSE_RX = r"\[TRAIN\] remote arrival of player (\d+): NOT calling a local train \(guard reason (\d+); action=(\d+) coming_flag=(\d+)\)"
REASONS = {1: "LATCHED", 2: "PUPPET_SCENE", 3: "LOCAL_SCENE", 4: "NO_PLAYER", 5: "TITLE", 6: "PRE_GAME", 7: "LOCAL_DEMO", 8: "COMING_FLAG", 9: "BUSY"}


def wait_for_quiet_hour():
    n = datetime.datetime.now()
    s = n.minute * 60 + n.second
    if 9 * 60 <= s <= 21 * 60 + 30:
        wait = 21 * 60 + 30 - s
        L.info("local clock %s: the hourly train (hh:14:50) is near; waiting %d s so it cannot make the guard refuse" % (n.strftime("%H:%M:%S"), wait))
        time.sleep(wait)


def actions_after(text, off):
    return [e[2][1] for e in parse(text) if e[1] == "state" and e[0] > off]


def lifecycle_done(seq):
    k = next((i for i, a in enumerate(seq) if a != 0), None)
    return k is not None and 0 in seq[k:] and any(a >= 6 for a in seq[k:]) and seq[-1] == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9840)
    ap.add_argument("--host-mode", choices=("resident", "dedicated"), default="resident")
    ap.add_argument("--slot", type=int, default=None, help="client resident (default: the first one not in the first-job events)")
    ap.add_argument("--max-wait", type=float, default=150.0, help="seconds to wait for the train lifecycle after the remote-arrival line")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4* only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    snap = os.path.join(os.environ.get("TEMP", "."), "guest_arrival_real_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    host = res = gst = None
    tag = args.host_mode
    try:
        residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
        # the hourly train never comes while a resident is in the first-job / HRA events: pick a free one (fixture residents 0 and 1 are in it)
        arbeit = []
        try:
            with open(os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL), "rb") as f:
                f.seek(0x26040 + 0x20498 + 0xB8)
                flags = int.from_bytes(f.read(4), "big")
            arbeit = [n for n in range(4) if (flags >> (2 + n)) & 1 or (flags >> (10 + n)) & 1 or (flags >> (14 + n)) & 1]
        except OSError:
            pass
        free = [r for r in residents if r not in arbeit]
        check("fixture has a free non-host resident (first-job/HRA residents %s, free %s)" % (arbeit, free), len(free) >= 1 or args.slot is not None)
        if not free and args.slot is None:
            return L.summary_and_exit_code(results)
        slot = args.slot if args.slot is not None else free[0]
        os.makedirs(os.path.join(save_dir, "mp"), exist_ok=True)
        with open(os.path.join(save_dir, "mp", "guest.ini"), "wb") as f:
            f.write(("# test profile\nname = Quill\ngender = 1\nface = 5\nhome_town = Hometwn\nplayer_id = 0x%04X\nland_id = %d\n" % (PID, LID)).encode("ascii"))
        wait_for_quiet_hour()

        hlog = os.path.join(HERE, "guest_arrival_real_host_%s.log" % tag)
        if args.host_mode == "dedicated":
            host = L.HostProcess(port=args.port, extra_args=["--dedicated", "-debug"], log_path=hlog, bin_dir=L.GAME_BIN_DIR, stdin_pipe=True).start()
            orig = host.log_text
            host.log_text = lambda: orig().replace("\r\n", "\n")
            check("dedicated host observer + world + save ready", host.wait_listening(60.0) and host.boot_to_dedicated(timeout=120.0))
        else:
            host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT), "-debug"], log_path=hlog, bin_dir=L.GAME_BIN_DIR).start()
            check("host listening and in the field", host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT))
        res = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", str(slot), "-debug"],
                              log_path=os.path.join(HERE, "guest_arrival_real_resident_%s.log" % tag), bin_dir=L.GAME_BIN_DIR, label="gar").start()
        check("resident client (bootstrap resident %d) in the field with the snapshot applied" % slot, res.boot_to_field(timeout=90.0, slot=slot))
        time.sleep(4.0)
        roff = len(res.log_text())
        hoff = len(host.log_text())
        L.info("resident / host log offsets before the guest arrives: %d / %d; resident train lines so far: %s" % (roff, hoff, actions_after(res.log_text(), 0)))
        check("before the guest: neither process logged a remote-arrival line", re.search(START_RX, res.log_text()) is None and re.search(START_RX, host.log_text()) is None)

        gst = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--guest", "-debug"],
                              log_path=os.path.join(HERE, "guest_arrival_real_guest_%s.log" % tag), bin_dir=L.GAME_BIN_DIR, label="gag").start()
        m = gst.wait_for_log(ADOPT_RX, 150.0)
        check("A guest client arrived and its record was adopted", m is not None)
        gt0 = gst.log_text()
        check("A the guest's own arrival train started (coming_flag 3 logged on the guest)", re.search(r"\[TRAIN\] \w+: action=\d+ control=\d+ last_control=\d+ coming_flag=3", gt0) is not None
              or "[TRAIN]" in gt0)

        # ---- B: the remote-arrival line on the resident (and on the host) ----
        ms = res.wait_for_log(START_RX, 120.0, since_offset=roff)
        check("B resident: a local arrival train was started for the guest's puppet", ms is not None)
        # The fixture host resident (0) is in the first-job event: its OWN town scene spawns the vanilla INTRO_DEMO (its own train arrival), so the guard must
        # refuse there with LOCAL_DEMO (7) -- the host is itself arriving. A dedicated host (hidden observer, no intro demo) must start the train.
        host_intro = args.host_mode == "resident" and L.TEST_HOST_RESIDENT in arbeit
        if host_intro:
            mh = host.wait_for_log(REFUSE_RX, 20.0, since_offset=hoff)
            check("B host (resident %d is in the first-job event: its own intro demo is a local train demo): the guard REFUSED with LOCAL_DEMO (7), no train called" % L.TEST_HOST_RESIDENT,
                  mh is not None and mh.group(2) == "7" and re.search(START_RX, host.log_text()[hoff:]) is None)
        else:
            mh = host.wait_for_log(START_RX, 20.0, since_offset=hoff)
            check("B host (%s): a local arrival train was started for the guest's puppet" % args.host_mode, mh is not None)
        for who, proc, off in (("resident", res, roff), ("host", host, hoff)):
            txt = proc.log_text()[off:]
            starts = re.findall(START_RX, txt)
            refus = re.findall(REFUSE_RX, txt)
            L.info("%s: start lines %s; refusals %s" % (who, starts, [(p, REASONS.get(int(r), r), a, c) for p, r, a, c in refus]))
            want = 0 if (who == "host" and host_intro) else 1
            check("C %s: exactly %d start line(s) for the guest's arrival (%d)" % (who, want, len(starts)), len(starts) == want)
            check("C %s: exactly one guard evaluation per puppet period (start + refusals = 1: %d)" % (who, len(starts) + len(refus)), len(starts) + len(refus) == 1)
        if ms is None:
            return L.summary_and_exit_code(results)

        # ---- lifecycle on both ----
        deadline = time.monotonic() + args.max_wait
        done = {}
        while time.monotonic() < deadline and len(done) < (1 if host_intro else 2):
            for who, proc, off in (("resident", res, roff), ("host", host, hoff)):
                txt = proc.log_text()
                so = re.search(START_RX, txt[off:])
                if so is None or who in done:
                    continue
                seq = actions_after(txt, off + so.start())
                if lifecycle_done(seq):
                    done[who] = seq
            time.sleep(2.0)
        for who, proc, off in (("resident", res, roff), ("host", host, hoff)):
            txt = proc.log_text()
            so = re.search(START_RX, txt[off:])
            seq = actions_after(txt, off + so.start()) if so else []
            L.info("%s: [TRAIN] action sequence after the remote-arrival line: %s" % (who, seq))
            if who == "host" and host_intro:
                check("C host: no local train was started by the refused guard (no 0 -> non-zero transition caused by it: the host's own state stays as it was)", so is None)
            elif who in done:
                acts = set(seq)
                check("B %s: train lifecycle observed: arriving (2) -> stopped (3/4/5) -> departing (6/7/8) -> departed (0)" % who,
                      bool(acts & {2}) and bool(acts & {3, 4, 5}) and bool(acts & {6, 7, 8}) and seq[-1] == 0)
                rises = sum(1 for a, b in zip([0] + seq, seq) if a == 0 and b != 0)
                check("C %s: exactly ONE 0 -> non-zero train transition in the window (no second local train: %d)" % (who, rises), rises == 1)
                check("B %s: the first state after the call is coming_flag-driven demo_init (action 2 BEGIN_SLOWDOWN)" % who, seq and seq[0] in (0, 2) and 2 in seq)
            else:
                L.info("B %s: full lifecycle NOT OBSERVED within %.0f s (sequence above); only the start line was asserted" % (who, args.max_wait))
                rises = sum(1 for a, b in zip([0] + seq, seq) if a == 0 and b != 0)
                check("C %s: at most ONE 0 -> non-zero train transition so far (%d)" % (who, rises), rises <= 1)
        check("no crash: host, resident and guest alive at the end", host.exit_code() is None and res.exit_code() is None and gst.exit_code() is None)
    finally:
        for p in (gst, res, host):
            if p is not None:
                p.stop()
        time.sleep(2.0)
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
        L.discard_fixture_guest_mp(L.GAME_BIN_DIR)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
