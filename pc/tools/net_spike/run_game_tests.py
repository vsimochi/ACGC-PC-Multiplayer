#!/usr/bin/env python3
"""run_game_tests.py - runs the synthetic network tests against the REAL game host, one FRESH host
per test.

For each selected test: launches `AnimalCrossing.exe --host <port> --verbose [flags]` via
net_spike_lib.HostProcess (CWD = pc/build64/bin, because game resources are CWD-relative; optional
fault-injection env vars), waits for "[NET] hosting on UDP port", walks the host from the title
screen into gameplay (HostProcess.boot_to_town(): protocol v2 serves no world state before that),
waits for the --pickup-test-seed fixtures where needed, runs the test script with
`127.0.0.1 <port> [extra args]`, then stops the host -- recording whether the host had already
exited on its own (a crash) and keeping its log next to the test output.

NOTE: boot_to_town() focuses the host window and injects keystrokes (game_input.py); do not type
into other windows while this runs.

Usage:
  python run_game_tests.py [--port 7788] [--log-dir DIR] [--no-launch] [test ...]
  --no-launch   use an already-running, already-in-town host on --port
Test names may omit the "test_" prefix and ".py" suffix; a fault entry is named like
"host_faults:drop_rdata". Default: the whole suite below.
"inventory_correctness" (two-phase pickup/drop, ~1 min) and "inventory_expiry" (the ~25 s reservation-expiry test)
are separate entries so the slow one can be selected/skipped.
Exit code: 0 all passed, 1 any failure / host crash, 2 no failure but some PENDING.
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

SEED = ["--pickup-test-seed"]
BURY_SEED = ["--bury-test-seed", "--field-action-test-seed"]
# (name, script, host flags, host env, script extra args)
SUITE = [
    ("version_mismatch", "test_version_mismatch.py", [], {}, []),
    ("move_sync", "test_move_sync.py", [], {}, []),
    ("move_delay", "test_move_delay.py", [], {}, []),
    ("move_rotation", "test_move_rotation.py", [], {}, []),
    ("appearance_sync", "test_appearance_sync.py", [], {}, []),
    ("pickup_sync", "test_pickup_sync.py", SEED, {}, []),
    ("drop_sync", "test_drop_sync.py", SEED, {}, []),
    ("transport_reliability", "test_transport_reliability.py", [], {}, []),
    ("reconnect_state", "test_reconnect_state.py", SEED, {}, []),
    ("world_snapshot", "test_world_snapshot.py", SEED, {}, []),
    ("field_sync", "test_field_sync.py", SEED, {}, []),
    ("bury_sync", "test_bury_sync.py", BURY_SEED, {}, []),
    ("field_action_sync", "test_field_action_sync.py", ["--field-action-test-seed"], {}, []),
    ("dig_family_sync", "test_dig_family_sync.py", ["--field-action-test-seed"], {}, []),
    ("money_rock_late_join", "test_money_rock_late_join.py", ["--field-action-test-seed"], {}, []),
    ("money_rock_review_gaps", "test_money_rock_review_gaps.py", ["--field-action-test-seed"], {}, []),
    ("tree_review_gaps", "test_tree_review_gaps.py", ["--field-action-test-seed"], {}, []),
    ("snowman_sync", "test_snowman_sync.py", [], {}, []),
    # two-phase pickup/drop hardening (reserve -> confirm -> commit); "{host_log}" is replaced by the host's log path
    ("inventory_correctness", "test_inventory_correctness.py", SEED, {}, ["--skip-expiry", "--host-log", "{host_log}"]),
    # the slow ~22 s reservation-expiry test (PC_NETGAME_CONFIRM_TIMEOUT_MS = 20000), its own fresh host
    ("inventory_expiry", "test_inventory_correctness.py", SEED, {}, ["--only", "expiry", "--host-log", "{host_log}"]),
    ("host_faults:drop_rdata", "test_host_faults.py", SEED, {"PC_NET_FAULT_DROP_RDATA_EVERY": "3"}, ["drop_rdata"]),
    ("host_faults:dup_rdata", "test_host_faults.py", SEED, {"PC_NET_FAULT_DUP_RDATA_EVERY": "3"}, ["dup_rdata"]),
    ("host_faults:reorder_rdata", "test_host_faults.py", SEED, {"PC_NET_FAULT_REORDER_RDATA_EVERY": "3"},
     ["reorder_rdata"]),
    ("host_faults:drop_ack", "test_host_faults.py", SEED, {"PC_NET_FAULT_DROP_ACK_EVERY": "2"}, ["drop_ack"]),
    # Villager population/is_home milestone: TEST-ONLY debug hooks force a grow+remove shortly after
    # boot (see pc_main.c/pc_net_game.c's pcnetgame_run_villager_test_triggers()) so the test doesn't
    # have to wait on the real multi-day trigger condition.
    ("villager_population", "test_villager_population.py", ["--force-villager-grow", "--force-villager-remove"], {},
     ["--expect", "both"]),
    # Friendship/mail sync milestone: TEST-ONLY debug hooks apply a friendship delta and send a test
    # letter shortly after boot (see pc_main.c/pc_net_game.c's
    # pcnetgame_run_friendship_mail_test_triggers()) through the REAL mNpc_AddFriendship()/
    # mNpc_SendMailtoNpc() functions, so the test doesn't depend on real dialogue/post-office UI flow.
    ("friendship_mail_sync", "test_friendship_mail_sync.py", ["--force-friendship-delta", "5", "--force-mail-send"],
     {}, ["--expect", "both"]),
]


def normalize(name):
    for n, *_ in SUITE:
        if name in (n, "test_" + n, "test_" + n + ".py", n + ".py"):
            return n
    raise SystemExit(f"unknown test {name!r}; known: {[n for n, *_ in SUITE]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7788)
    ap.add_argument("--settle", type=float, default=2.0, help="seconds to wait after the host world is ready")
    ap.add_argument("--log-dir", default=os.path.join(HERE, "logs"))
    ap.add_argument("--no-launch", action="store_true")
    ap.add_argument("--script-timeout", type=float, default=300.0)
    ap.add_argument("tests", nargs="*")
    a = ap.parse_args()

    table = {n: (s, f, e, x) for n, s, f, e, x in SUITE}
    selected = [normalize(t) for t in a.tests] or [n for n, *_ in SUITE]
    os.makedirs(a.log_dir, exist_ok=True)
    summary = []
    boots = []  # one record per host process launched: test, attempt, pid, boot_ok, exit before stop, log

    def launch(name, flags, env, attempt):
        stem = name.replace(":", "_") + ("" if attempt == 1 else f".retry{attempt - 1}")
        host = L.HostProcess(port=a.port, extra_args=flags, env=env,
                             log_path=os.path.join(a.log_dir, f"{stem}.host.log")).start()
        ok = host.wait_listening(90.0) and host.boot_to_town(150.0)
        if ok and flags == SEED and not host.wait_seeded(20, 30.0):
            print(f"[run] {name}: only {len(host.seeded_tiles())} seed tile(s) placed (continuing)")
        return host, ok, stem

    def record(name, attempt, host, boot_ok, own_exit, note=""):
        rc = host.proc.returncode if host.proc is not None else None
        boots.append({"test": name, "attempt": attempt, "pid": host.proc.pid if host.proc else None,
                      "boot_ok": boot_ok, "own_exit": own_exit, "final_rc": rc, "log": host.log_path, "note": note})

    for name in selected:
        script, flags, env, extra = table[name]
        host = None
        stem = name.replace(":", "_")
        first_failed_boot = None
        if not a.no_launch:
            attempt = 1
            host, ok, stem = launch(name, flags, env, attempt)
            while not ok and attempt <= 2:  # a failed boot is counted, then the SAME test is re-run once
                own = host.stop()
                print(f"[run] {name}: host did not reach gameplay on attempt {attempt} "
                      f"(exit before stop: {own if own is None else hex(own & 0xFFFFFFFF)}) -- see {host.log_path}")
                record(name, attempt, host, False, own, "BOOT FAILURE")
                if attempt == 2:
                    break
                first_failed_boot = own
                attempt += 1
                host, ok, stem = launch(name, flags, env, attempt)
            if not ok:
                summary.append((name, f"HOST-FAIL (both boot attempts failed)"))
                continue
            time.sleep(a.settle)
        print(f"[run] ===== {name} {' '.join(flags)} {env or ''}")
        t0 = time.monotonic()
        argv_extra = []
        for x in extra:
            if x == "{host_log}":
                if host is not None:
                    argv_extra.append(host.log_path)
                else:
                    argv_extra.pop()  # --no-launch: no host log to hand over; drop the preceding "--host-log"
            else:
                argv_extra.append(x)
        try:
            r = subprocess.run([sys.executable, os.path.join(HERE, script), "127.0.0.1", str(a.port)] + argv_extra,
                               capture_output=True, text=True, timeout=a.script_timeout)
            out, rc = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            out, rc = out + "\n[run] TIMEOUT\n", 1
        with open(os.path.join(a.log_dir, f"{stem}.out.txt"), "w", encoding="utf-8") as f:
            f.write(out)
        print(out)
        crash = None
        if host is not None:
            crash = host.stop()
            record(name, attempt, host, True, crash, "CRASHED DURING TEST" if crash is not None else "")
            if crash is not None:
                print(f"[run] {name}: HOST EXITED ON ITS OWN with code {crash} ({crash & 0xFFFFFFFF:#010x}) -- "
                      f"log kept at {host.log_path}")
        verdict = {0: "PASS", 2: "PENDING"}.get(rc, "FAIL")
        if crash is not None:
            verdict = "HOST-CRASH/" + verdict
        tail = [ln for ln in out.splitlines() if "checks passed" in ln]
        extra_note = ""
        if first_failed_boot is not None or (boots and boots[-1]["attempt"] > 1):
            extra_note = " [after a failed boot attempt]"
        summary.append((name, f"{verdict} ({time.monotonic() - t0:.1f}s) {tail[-1] if tail else ''}{extra_note}"))
    print("=" * 72)
    for name, verdict in summary:
        print(f"{verdict:<50} {name}")
    print("HOST BOOT TABLE (one row per AnimalCrossing.exe launched)")
    abnormal = 0
    lines = []
    for b in boots:
        bad = (not b["boot_ok"]) or b["own_exit"] is not None
        abnormal += 1 if bad else 0
        ex = "still running (terminated by harness)" if b["own_exit"] is None else f"EXITED ON ITS OWN {b['own_exit']} ({b['own_exit'] & 0xFFFFFFFF:#010x})"
        lines.append(f"{b['test']:<28} attempt {b['attempt']} pid {b['pid']:<7} boot={'OK ' if b['boot_ok'] else 'FAIL'} "
                     f"{ex}; final rc {b['final_rc']} {b['note']}  {os.path.basename(b['log'])}")
    print(chr(10).join(lines))
    print(f"total host boots: {len(boots)}, abnormal: {abnormal}")
    with open(os.path.join(a.log_dir, "boot_table.txt"), "w", encoding="utf-8") as f:
        f.write(chr(10).join(lines) + f"\ntotal host boots: {len(boots)}, abnormal: {abnormal}\n")
    if any(v.startswith(("FAIL", "HOST")) for _, v in summary):
        return 1
    if any(v.startswith("PENDING") for _, v in summary):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
