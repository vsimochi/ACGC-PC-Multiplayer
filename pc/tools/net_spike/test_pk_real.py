#!/usr/bin/env python3
r"""test_pk_real.py - worker PK REAL-GAME run (real host + real client, Samsung display, volume 1, TEST-ONLY autopilot / positioning aids).

ONE host process + ONE client process (default --only P,K):
  P  full-pocket pickup: the real client fills its pockets (autopilot 'give'), is parked 20 units north of a --pickup-test-seed tile facing it and
     presses B (the pickup button) once. Expected: the vanilla 'pockets full' refusal (main_index REFUSE_PICKUP = 3, never PICKUP = 30), the client's
     log line, NO pickup request / commit, the host's tile untouched. Control: one rod taken out (autopilot 'take') -> the same B press runs the real
     pickup (PICKUP 30, TXN APPLIED).
  K  police lost-and-found multi-claim: host --ts-test-seed-police; the pockets are freed again, the real client warps into the police box
     (AC_TEST_MUSEUM_ROOM=police), is parked in front of lost-and-found entry 0 (ac_goto 'k 0') and mashes A answering every choice with 'yes'
     (autopilot 'mashf'). Expected: at least TWO POLICE_CLAIM transactions APPLIED in the SAME visit (the bug: only one per visit), none rejected, each
     item granted once, a resident observer (FakeClient) receives a police mirror per claim.
Fixture: env PK_FIXTURE (default bin_fixture4_pk_proto, a pc\build64\bin_fixture4* copy with the w3 exe). Ports 29500-29999.
Usage: python test_pk_real.py [--only P,K] [--port 29700]
"""
import argparse
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
import town_test_support as TS  # noqa: E402

FIXTURE = os.environ.get("PK_FIXTURE", "bin_fixture4_pk_proto")
BIN = os.path.join(TS.BUILD64, FIXTURE)
os.environ["NET_SPIKE_GAME_BIN"] = BIN
import net_spike_lib as L  # noqa: E402
import wplay_lib as W  # noqa: E402

W.NAME = FIXTURE
W.BIN = BIN
results = []
SEEDS = [0x2800, 0x2801, 0x2802]


def check(d, c):
    L.check(d, bool(c), results)


def wait_pump(proc, rx, timeout, since=0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        m = re.search(rx, proc.log_text()[since:])
        if m:
            return m
        if not proc.alive():
            return None
        L.pump_sleep(0.5)
    return re.search(rx, proc.log_text()[since:])


def wait_upload(cli, since, timeout=50.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if re.search(r"upload xfer \d+ APPLIED", cli.log_text()[since:]):
            return True
        time.sleep(0.5)
    return False


def press_b_and_sample(pc, cli, host):
    co, ho = len(cli.log_text()), len(host.log_text())
    pc.cmd("mashb 0.45", 10.0)  # ONE B press (the vanilla pickup button); the autopilot runs one command at a time
    states = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.5:
        st = pc.state()
        if st is not None:
            states.append(st[0])
    L.pump_sleep(1.5)
    return states, cli.log_text()[co:], host.log_text()[ho:]


def phase_p(host, cli, pc):
    seeds = [(int(a), int(b)) for a, b in re.findall(r"--pickup-test-seed: fixture item placed at tile \((\d+),(\d+)\)", host.log_text())]
    L.pump_sleep(3.0)
    off_give = len(cli.log_text())
    gave, full = 0, False
    for _ in range(16):
        a = pc.cmd("give 2203", 20.0) or ""
        if "no free pocket" in a:
            full = True
            break
        gave += 1
    check("P the client's pockets are full (autopilot give until 'no free pocket', %d given)" % gave, full)
    check("P the full pockets reached the host (D3 upload APPLIED)", wait_upload(cli, off_give))
    L.pump_sleep(1.0)
    pref = [t for t in seeds if t in ((40, 56), (40, 40), (24, 56), (56, 56), (40, 72))]
    tile = pref[0] if pref else (seeds[len(seeds) // 2] if seeds else (40, 56))
    tx, tz = tile[0] * 40 + 20, tile[1] * 40 + 20
    check("P a seeded tile to target (%s of %d seeds)" % (tile, len(seeds)), bool(seeds))
    pc.cmd("tp %.1f %.1f" % (tx, tz - 20.0), 20.0)  # the pickup hand reaches 20 units ahead (+15 radius)
    pc.cmd("face %.1f %.1f" % (tx, tz), 20.0)
    L.pump_sleep(1.5)
    states, ct, ht = press_b_and_sample(pc, cli, host)
    print("   INFO main_index samples after the B press (full pockets): %s" % states)
    check("P the client logged the full-pocket refusal (no pickup tried)", re.search(r"\[NET\]\[PICKUP\] pockets full: item 0x[0-9A-F]{4} is not picked up", ct) is not None)
    check("P the vanilla refusal state REFUSE_PICKUP (3) was seen and the pickup animation PICKUP (30) never ran", 3 in states and 30 not in states)
    check("P nothing was sent / committed: no provisional accept, no TXN pickup, no host commit for the tile",
          "accepted (item=" not in ct and "kind=PICKUP" not in ct and not re.search(r"committed tile \(%d,%d\)" % tile, ht))
    pc.cmd("mash 3", 20.0)
    L.pump_sleep(1.5)
    st = pc.state()
    check("P the player ends free after the message (%s)" % (st,), st is not None and st[1] == 1)
    # control: one free pocket -> the ordinary pickup of the same tile runs (geometry / button are right; the refusal above was the full pockets)
    off = len(cli.log_text())
    pc.cmd("take 2203", 20.0)
    gave -= 1
    wait_upload(cli, off)
    L.pump_sleep(1.0)
    pc.cmd("tp %.1f %.1f" % (tx, tz - 20.0), 20.0)
    pc.cmd("face %.1f %.1f" % (tx, tz), 20.0)
    L.pump_sleep(1.0)
    states2, _ct2, _ht2 = press_b_and_sample(pc, cli, host)
    L.pump_sleep(2.0)
    ct2 = cli.log_text()[off:]
    print("   INFO main_index samples after the B press (one free pocket): %s" % states2)
    check("P control: with one free pocket the same B press runs the real pickup (PICKUP 30 seen, TXN APPLIED, no refusal line)",
          30 in states2 and re.search(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=PICKUP", ct2) is not None and "is not picked up" not in ct2)
    return gave


def phase_k(host, cli, obs):
    auto = os.path.join(BIN, "ac_auto_C.txt")
    w = wait_pump(cli, r"warping into museum room 8 \(scene (\d+)\)", 240.0)
    check("K the client warped into the police box", w is not None)
    if w is None:
        return
    live = wait_pump(cli, r"local scene live: scene=%s " % w.group(1), 60.0)
    check("K the police box scene is live on the client", live is not None)
    L.pump_sleep(3.0)
    with open(os.path.join(BIN, "ac_goto.txt"), "w") as f:
        f.write("k 0\n")
    g = wait_pump(cli, r"goto k 0: (lost-and-found unit \((\d+),(\d+)\) holds 0x([0-9A-F]{4})|no lost-and-found unit)", 20.0)
    check("K the client was parked in front of lost-and-found entry 0 (%s)" % (g.group(1) if g else None), g is not None and "holds" in g.group(1))
    if g is None or "holds" not in g.group(1):
        return
    L.pump_sleep(1.5)
    co = len(cli.log_text())
    with open(auto, "w") as f:
        f.write("901 mashf 40\n")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 50.0:
        if len(re.findall(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=POLICE_CLAIM", cli.log_text()[co:])) >= 2 or not cli.alive():
            break
        L.pump_sleep(1.0)
    L.pump_sleep(4.0)
    ct, ht = cli.log_text()[co:], host.log_text()
    applied = re.findall(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=POLICE_CLAIM", ct)
    begins = re.findall(r"client: begin POLICE_CLAIM request=\d+ pocket slot=\d+ item=0x([0-9A-F]{4})", ct)
    commits = re.findall(r"POLICE_CLAIM item=0x([0-9A-F]{4}) lost-and-found slot=\d+ -> pocket slot \d+ committed", ht)
    print("   INFO client begins %s, host commits %s, applied %d" % (begins, commits, len(applied)))
    check("K at least TWO lost-and-found claims APPLIED in the SAME visit (no re-entry)", len(applied) >= 2)
    check("K no claim was rejected and each committed claim was answered APPLIED (host commits %s)" % commits,
          not re.search(r"TXN_RESULT REJECTED[^\n]*kind=POLICE_CLAIM", ct) and len(commits) == len(applied))
    L.pump_sleep(1.0)
    if obs is not None:
        n_obs = len([1 for conn, gg, bl in obs.ts_states if gg.service == L.PC_NETGAME_TS_POLICE])
        check("K the resident observer received one police mirror per claim (+ the READY one): %d" % n_obs, n_obs >= 1 + len(commits))
    check("K no dialogue hang / crash on the client", cli.alive())


def run(port, do_p, do_k):
    extra = ["--bootstrap-resident", "0", "-debug"]
    if do_p:
        extra.append("--pickup-test-seed")
    if do_k:
        extra.append("--ts-test-seed-police=" + ",".join("0x%04X" % s for s in SEEDS))
    host = L.HostProcess(port=port, extra_args=extra, bin_dir=BIN, log_path=os.path.join(HERE, "pk_real_host.log")).start()
    ok = host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0)
    check("host in the field", ok)
    cli = obs = None
    try:
        if not ok:
            return
        L.resolve_host_town("127.0.0.1", port)
        if do_k:
            res = [i for i, _p, ex in L.read_test_save_residents(BIN) if ex and i not in (0, 1)]
            obs = L.FakeClient("OBS", "127.0.0.1", port, player=L.resident_player(res[0]))
            obs.rec_resident_idx = res[0]
            obs.connect_and_ready(quiet=True)
            st0 = obs.wait_ts_state(L.PC_NETGAME_TS_POLICE, 6.0, min_seq=1)
            check("K the observer holds the police mirror with the seeds", st0 is not None and all(s in struct.unpack("<20H", st0[1]) for s in SEEDS))
        for p in (os.path.join(BIN, "ac_auto_C.txt"), os.path.join(BIN, "ac_goto.txt")):
            if os.path.exists(p):
                os.remove(p)
        env = {"AC_TEST_AUTOPILOT": "ac_auto_C.txt"}
        if do_k:
            env.update({"AC_TEST_MUSEUM_ROOM": "police,%d" % (110000 if do_p else 3000), "AC_TEST_GOTO": "1"})
        cli = L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--bootstrap-resident", "1", "-debug"], bin_dir=BIN, log_path=os.path.join(HERE, "pk_real_client.log"),
                              label="pk", env=env).start()
        check("real client in the field", cli.boot_to_field(timeout=180, slot=1))
        pc = W.Player(L, "C", cli)
        if do_p:
            gave = phase_p(host, cli, pc)
            if do_k:  # free the pockets again for the claims (the rods given above)
                off = len(cli.log_text())
                for _ in range(max(gave, 0)):
                    pc.cmd("take 2203", 20.0)
                check("K setup: the pockets were freed again (upload APPLIED)", wait_upload(cli, off))
        if do_k:
            phase_k(host, cli, obs)
    finally:
        if cli is not None:
            cli.stop()
        if obs is not None:
            try:
                obs.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        check("host did not crash (%s)" % rc, rc is None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=29700)
    ap.add_argument("--only", default="P,K")
    args = ap.parse_args()
    if not 29500 <= args.port <= 29999:
        print("REFUSING: worker PK ports are 29500-29999", file=sys.stderr)
        return 2
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(BIN)).startswith("bin_fixture4"):
        print("REFUSING: fixture copies only", file=sys.stderr)
        return 2
    save_dir = os.path.join(BIN, "save")
    snap = os.path.join(os.environ.get("TEMP", "."), "pk_real_save_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    try:
        tags = args.only.split(",")
        run(args.port, "P" in tags, "K" in tags)
    finally:
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
