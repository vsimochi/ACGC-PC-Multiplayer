#!/usr/bin/env python3
"""test_guest_train_real.py - T1 / T2 REAL host + REAL client process run (the title-demo train leak).

TIER: REAL PROCESSES (hook-driven, no GUI automation): `AnimalCrossing.exe --host <port> --bootstrap-resident 0 -debug` and
`AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot> -debug`, exactly the bootstrap path that used to leave the title
demo's parked train in the town. Everything asserted comes from the T1 `[TRAIN]` lines (PC_LOG GENERAL, change-only) in the two logs.

  A  T2 edge: each bootstrapped process logs exactly one 'title demo 1 -> 0: re-init' (the leak fix fired once per title exit)
  B  after the re-init the train state is the clean one (action 0, control 0, last_control 0, coming_flag 0) and the title's
     parked state (action 5, control 1, last_control 1, title_demo 0) is NEVER logged after the edge
  C  T1 lifecycle: with the train due soon (the hourly train spawns at hh:14:50 RTC) the client log shows the actions 1/2 (arriving) -> 3/4
     (stopped) -> 6.. (departing) -> back to 0 (departed). If it is not due within --max-wait seconds only B is asserted and the
     lifecycle is reported as NOT OBSERVED (never silently passed).
NOT covered: visuals, the guest ride-off arrival train (needs a guest join, see test_guest_g3_real.py), the T5 edge feel.
Run ONLY on a disposable bin_fixture4* dir (NET_SPIKE_GAME_BIN=<absolute path>, the new exe copied into it). The fixture save is snapshotted
at start and restored at the end.
Usage: python test_guest_train_real.py [--port 9830] [--max-wait 120]
"""
import argparse
import datetime
import os
import re
import shutil
import sys
import time

import net_spike_lib as L

HERE = os.path.dirname(os.path.abspath(__file__))
LINE = re.compile(r"\[TRAIN\] (\w+): action=(\d+) control=(\d+) last_control=(\d+) coming_flag=(\d+) signal=(\d+) title_demo=(-?\d+) start_timer=(\d+) now=(\d+)")
EDGE = re.compile(r"\[TRAIN\] title demo 1 -> (-?\d+): re-init \(was action=(\d+) control=(\d+) last=(\d+) coming_flag=(\d+), keep coming_flag=(\d+)\)")


def parse(text):
    """-> list of (offset, kind, fields) for every [TRAIN] line."""
    out = []
    for m in re.finditer(r"\[TRAIN\][^\n]*", text):
        ln = m.group(0)
        e = EDGE.search(ln)
        if e:
            out.append((m.start(), "edge", tuple(int(x) for x in e.groups())))
            continue
        s = LINE.search(ln)
        if s:
            out.append((m.start(), "state", (s.group(1),) + tuple(int(x) for x in s.groups()[1:])))
    return out


def secs_to_next_train():
    n = datetime.datetime.now()
    nxt = n.replace(minute=14, second=50, microsecond=0)
    if nxt <= n:
        nxt += datetime.timedelta(hours=1)
    return (nxt - n).total_seconds()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9830)
    ap.add_argument("--slot", type=int, default=None, help="client resident (default: the first one not in the first-job events)")
    ap.add_argument("--max-wait", type=float, default=120.0, help="seconds to wait for the hourly train lifecycle after the client is in the town")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable pc\\build64\\bin_fixture4* only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    check = lambda d, c: L.check(d, c, results)  # noqa: E731
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    snap = os.path.join(os.environ.get("TEMP", "."), "guest_train_real_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    host = cl = None
    try:
        residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
        check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
        if not residents:
            return L.summary_and_exit_code(results)
        # The hourly train never comes while the resident is in the first-job / HRA events (mEv_CheckArbeit). The fixture residents 0 and 1 are
        # (Save_t.event_save_data.flags, BE u32 at GCI 0x26040 + 0x20498 + 0xB8: bit 2 + player_no = FIRSTJOB_PLR<n>), so prefer a free resident.
        arbeit = free = None
        try:
            with open(os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL), "rb") as f:
                f.seek(0x26040 + 0x20498 + 0xB8)
                flags = int.from_bytes(f.read(4), "big")
            arbeit = [n for n in range(4) if (flags >> (2 + n)) & 1 or (flags >> (10 + n)) & 1 or (flags >> (14 + n)) & 1]
            free = [r for r in residents if r not in arbeit]
        except OSError:
            pass
        slot = args.slot if args.slot is not None else (free[0] if free else residents[0])
        L.info("residents in the first-job/HRA events (no hourly train): %s; client resident = %d" % (arbeit, slot))
        due = secs_to_next_train()
        L.info("next hourly train spawn in %.0f s (hh:14:50 local RTC)" % due)
        host = L.HostProcess(port=args.port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT), "-debug"],
                             log_path=os.path.join(HERE, "guest_train_real_host.log"), bin_dir=L.GAME_BIN_DIR).start()
        check("host listening and in the field", host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT))
        cl = L.ClientProcess("127.0.0.1:%d" % args.port, extra_args=["--bootstrap-resident", str(slot), "-debug"],
                             log_path=os.path.join(HERE, "guest_train_real_client.log"), bin_dir=L.GAME_BIN_DIR, label="gt").start()
        check("client (bootstrap resident %d) in the field with the snapshot applied" % slot, cl.boot_to_field(timeout=90.0, slot=slot))
        time.sleep(4.0)
        for who, proc in (("host", host), ("client", cl)):
            ev = parse(proc.log_text())
            edges = [e for e in ev if e[1] == "edge"]
            check("A %s: exactly one 'title demo 1 -> 0: re-init' line (%d)" % (who, len(edges)), len(edges) == 1 and edges[0][2][0] == 0)
            if not edges:
                continue
            eoff = edges[0][0]
            check("A %s: the edge fired with coming_flag 0 (nothing to keep) and kept coming_flag %d" % (who, edges[0][2][5]), edges[0][2][4] == 0 and edges[0][2][5] == 0)
            before = [e for e in ev if e[1] == "state" and e[0] < eoff]
            L.info("%s: state lines before the edge: %s" % (who, [(e[2][1], e[2][2], e[2][3], e[2][6]) for e in before]))
            after = [e for e in ev if e[1] == "state" and e[0] > eoff]
            parked = [e for e in after if e[2][1] == 5 and e[2][2] == 1 and e[2][3] == 1 and e[2][6] == 0]
            check("B %s: the title's parked state (action 5, control 1, last 1, title_demo 0) is NEVER logged after the edge" % who, not parked)
            check("B %s: the state right after the re-init is the clean one (action 0, control 0, last_control 0, coming 0, title_demo 0)" % who,
                  bool(after)
                  and after[0][2][1] == 0 and after[0][2][2] == 0 and after[0][2][3] == 0 and after[0][2][4] == 0 and after[0][2][6] == 0)
        # ---- C: the lifecycle ----
        ct = cl.log_text()
        eoff = next((e[0] for e in parse(ct) if e[1] == "edge"), 0)
        deadline = time.monotonic() + args.max_wait
        done = False
        while time.monotonic() < deadline:
            seq = [e[2][1] for e in parse(cl.log_text()) if e[1] == "state" and e[0] > eoff]
            # departed: after a non-zero action, the state returned to 0
            k = next((i for i, a in enumerate(seq) if a != 0), None)
            if k is not None and 0 in seq[k:] and any(a >= 6 for a in seq[k:]):
                done = True
                break
            time.sleep(2.0)
        seq = [e[2][1] for e in parse(cl.log_text()) if e[1] == "state" and e[0] > eoff]
        L.info("client action sequence after the edge: %s" % seq)
        if done:
            acts = set(seq)
            check("C client train lifecycle observed: arriving (1/2) -> stopped (3/4/5) -> departing (6/7/8) -> departed (0)",
                  bool(acts & {1, 2}) and bool(acts & {3, 4, 5}) and bool(acts & {6, 7, 8}) and seq[-1] == 0)
        else:
            L.info("C NOT OBSERVED: the full train lifecycle did not complete within %.0f s (train due in %.0f s at start); only A / B were asserted"
                   % (args.max_wait, due))
        check("no crash: both processes alive at the end", host.exit_code() is None and cl.exit_code() is None)
    finally:
        for p in (cl, host):
            if p is not None:
                p.stop()
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
