#!/usr/bin/env python3
r"""test_fishing_late_release_real.py - REAL host + REAL resident client (Samsung display, volume 1): a host fish RELEASE that reaches the client AFTER it pressed A on the bite.

The client fishes with the real rod (`catch fish ENT`: real cast, real bobber stream, host fish AI bite, real hook). The moment the client's autopilot logs "BITE seen -> hooking" (A pressed:
the bobber goes to gyo_status 4 on the next tick and stays there until its bite timer runs out, 0.9-2.6 s), the test makes the HOST lose its fish actor so the host's proxy lets go and the host
sends the client a BOBBER_EVENT RELEASE (msg 75, ev 4) -- the late release the softlock guards are about:
  K  `killfish ENT` on the host (vanilla deferred destroy; the record stays, the host re-creates the actor): the client must IGNORE the release (applied=0 at gyo_status >= 4), finish the hook,
     the host must accept exactly ONE catch and the client get exactly ONE item; no FLY_ROD watchdog; the player ends free.
  D  `hdespawn ENT` on the host (the record is released: WILDLIFE_DESPAWN + the release): the client must ignore the release and keep the committed fish (despawn Case 2 `< 4`), the host
     must NOT accept the catch, the client gets NO item, and the player ends free (no permanent block).
Evidence: the client's "[BOBBER] client: event 4 entity E applied=A (bobber gyo_status S, fish action F)" line (gyo_status 4 + fish action 5 = BITE is the discriminating window: before the
fix that release reset the bite), the host's CATCH lines, the client's grant lines, "[NET][FISH] fly_rod watchdog" lines, the autopilot's pos / main_index.
Fixture: pc\build64\bin_fixture_rv_b (a disposable copy with the current exe).
Usage: python test_fishing_late_release_real.py [--port 13200] [--only k,d] [--tries 3]"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as TS  # noqa: E402
import wplay_lib as W  # noqa: E402

FIXTURE = os.environ.get("LREL_FIXTURE", "bin_fixture4_s1")  # sprint rule: a bin_fixture4_s<N> disposable copy
W.NAME = FIXTURE
W.BIN = os.path.join(TS.BUILD64, FIXTURE)


def _make_fixture_rv(name, empty_save=False):
    """town_test_support.make_fixture without its bin_fixture4 name assertion (the shared rules name this agent's fixture bin_fixture_rv_b)"""
    assert name == FIXTURE
    dst = os.path.join(TS.BUILD64, name)
    if os.path.isdir(dst):
        if not os.path.isfile(os.path.join(dst, TS.MARKER)):
            raise SystemExit("REFUSING: %s exists and is not a town-test fixture" % dst)
        rom = os.path.join(dst, "rom")
        if os.path.isdir(rom):
            os.rmdir(rom)
        shutil.rmtree(dst)
    os.makedirs(dst)
    for n in ("SDL2.dll", "keybindings.ini", "settings.ini", "shader_cache.bin", ".four_resident_fixture"):
        p = os.path.join(TS.SRC_BIN, n)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dst, n))
    shutil.copy2(TS.NEW_EXE, os.path.join(dst, "AnimalCrossing.exe"))
    shutil.copytree(os.path.join(TS.SRC_BIN, "shaders"), os.path.join(dst, "shaders"))
    shutil.copytree(os.path.join(TS.SRC_BIN, "save"), os.path.join(dst, "save"))
    target = os.path.realpath(os.path.join(TS.SRC_BIN, "rom"))
    subprocess.check_call(["cmd", "/c", "mklink", "/J", os.path.join(dst, "rom"), target], stdout=subprocess.DEVNULL)
    open(os.path.join(dst, TS.MARKER), "w").write("disposable fixture (agent B runtime verification)\n")
    return dst


TS.make_fixture = _make_fixture_rv
os.environ["NET_SPIKE_GAME_BIN"] = W.BIN
import net_spike_lib as L  # noqa: E402
import test_wildlife_sim_real as T  # noqa: E402

EV_RX = r"\[BOBBER\] client: event 4 entity %d applied=(\d) \(bobber gyo_status (-?\d+), fish action (-?\d+)\)"


class Rig(T.Session):
    def start_host(self):
        env = {"AC_TEST_AUTOPILOT": "ac_auto_host.txt", "AC_TEST_WILDLIFE_REROLL": "80", "AC_TEST_WILDLIFE_TRACE_FRAMES": "0", "AC_TEST_PROXY_DIAG": "1"}
        self.host = L.HostProcess(port=self.args.port, extra_args=["--bootstrap-resident", "0", "-debug"], env=env, log_path=os.path.join(HERE, "lrel_host.log"), bin_dir=W.BIN).start()
        ok = self.host.wait_listening(60) and self.host.boot_to_field(timeout=90, slot=0)
        self.check("host in the field", ok)
        if ok:
            L.resolve_host_town("127.0.0.1", self.args.port)
            self.players["host"] = W.Player(L, "host", self.host)
        return ok

    def start_client(self, label, slot):
        self.n += 1
        env = {"AC_TEST_AUTOPILOT": "ac_auto_%s.txt" % label, "AC_TEST_WILDLIFE_TRACE_FRAMES": "0"}
        c = L.ClientProcess("127.0.0.1:%d" % self.args.port, extra_args=["--bootstrap-resident", str(slot), "-debug"], env=env,
                            log_path=os.path.join(HERE, "lrel_%s_%d.log" % (label, self.n)), bin_dir=W.BIN, label="lr" + label).start()
        ok = c.boot_to_field(timeout=180, slot=slot)
        self.check("client %s in the field" % label, ok)
        if ok:
            self.clients[label] = c
            self.players[label] = W.Player(L, label, c)
        else:
            c.stop()
        return ok


def attempt(R, mode, ent):
    """one real catch of `ent` by A with the host-side loss injected at A's hook; returns a dict of evidence"""
    A, H = R.players["A"], R.players["host"]
    offa, offh = len(R.log("A")), len(R.log("host"))
    cid = A.send("catch fish %d" % ent)
    bite_rx = re.compile(r"\[AUTO\] %d fish: BITE seen[^\r\n]*" % cid)
    done_rx = re.compile(r"\[AUTO\] done %d \S+ [^\r\n]*" % cid)
    t_end = time.time() + 270
    bite = None
    injected = None
    while time.time() < t_end:
        la = R.log("A")[offa:]
        if bite is None:
            m = bite_rx.search(la)
            if m:
                bite = m.group(0)
                hc = H.send(("killfish %d" if mode == "K" else "hdespawn %d") % ent)
                injected = H.wait(hc, 10)
        if done_rx.search(la):
            break
        time.sleep(0.03)
    ans = A.wait(cid, 5)
    A.cmd("mash 20", 60)
    time.sleep(3)
    st = A.state()
    if st is not None and not st[1]:
        A.cmd("mash 10", 30)
        time.sleep(2)
        st = A.state()
    la, hl = R.log("A")[offa:], R.log("host")[offh:]
    return dict(ans=ans, bite=bite, injected=injected, ev=re.findall(EV_RX % ent, la),
                acc=re.findall(r"peer \d+ CATCH entity %d .*accepted -- removed" % ent, hl),
                rej=re.findall(r"CATCH entity %d [^\n]*(?:reject|refus|not found|unknown)[^\n]*" % ent, hl, re.I),
                grants=re.findall(r"CATCH request \d+ \(entity %d\) accepted -- item 0x[0-9A-F]+ granted" % ent, la),
                wd=re.findall(r"\[NET\]\[FISH\] fly_rod watchdog[^\r\n]*", la), state=st,
                desp=re.findall(r"[^\n]*despawn[^\n]*entity %d\b[^\n]*" % ent, la, re.I)[:3])


def run_mode(R, mode, tries):
    check = R.check
    A = R.players["A"]
    last = None
    for k in range(tries):
        ent = T.surviving_fish(R, "A")
        if ent is None:
            continue
        d = T.decisions(R.log("host")).get(ent)
        T.enter_acre(A, d[2] if d else 4, d[3] if d else 2)
        time.sleep(2)
        ev = attempt(R, mode, ent)
        last = (ent, ev)
        L.info("%s try %d entity %d: %s" % (mode, k + 1, ent, ev))
        hit = [e for e in ev["ev"] if int(e[1]) >= 4]
        if ev["bite"] and hit:
            break
        L.info("%s try %d: the late release did not land in the committed window (bite=%s, release lines %s) -- another fish" % (mode, k + 1, bool(ev["bite"]), ev["ev"]))
    if last is None:
        check("%s a fish was available" % mode, False)
        return
    ent, ev = last
    hit = [e for e in ev["ev"] if int(e[1]) >= 4]
    check("%s A hooked (BITE seen -> A pressed) and the host-side loss was injected (%s | %s)" % (mode, ev["bite"], ev["injected"]), ev["bite"] and ev["injected"])
    check("%s the host's RELEASE reached A after the commit and was IGNORED (event 4 lines: %s)" % (mode, ev["ev"]), hit and all(e[0] == "0" for e in hit))
    L.info("%s discriminating window (gyo_status 4 + fish action 5 BITE) hit: %s" % (mode, any(e[1] == "4" and e[2] == "5" for e in hit)))
    check("%s no release was applied to A's committed bobber (no applied=1 at gyo_status >= 4)" % mode, not any(e[0] == "1" and int(e[1]) >= 4 for e in ev["ev"]))
    if mode == "K":
        check("%s the catch finished (%s)" % (mode, ev["ans"]), ev["ans"] is not None and " ok" in ev["ans"])
        check("%s the host accepted exactly ONE catch of entity %d (%d)" % (mode, ent, len(ev["acc"])), len(ev["acc"]) == 1)
        check("%s A got exactly ONE item (%d)" % (mode, len(ev["grants"])), len(ev["grants"]) == 1)
    else:
        check("%s the host did NOT accept the catch of the released entity %d (%s; reject lines %s)" % (mode, ent, ev["acc"], ev["rej"][:2]), not ev["acc"])
        check("%s A got NO item (%s)" % (mode, ev["grants"]), not ev["grants"])
        L.info("%s A's despawn lines: %s ; autopilot: %s" % (mode, ev["desp"], ev["ans"]))
    L.info("%s FLY_ROD watchdog lines on A: %s" % (mode, ev["wd"]))
    if mode == "K":
        check("%s the normal hook path did not need the FLY_ROD watchdog (%s)" % (mode, ev["wd"]), not ev["wd"])
    check("%s A is free afterwards (main_index, free) = %s" % (mode, ev["state"]), ev["state"] is not None and ev["state"][1] == 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=13200)
    ap.add_argument("--only", default="k,d")
    ap.add_argument("--tries", type=int, default=3)
    args = ap.parse_args()
    W.make_fixture()
    results = []
    R = Rig(args, results)
    try:
        if not (R.start_host() and R.start_client("A", 1)):
            return L.summary_and_exit_code(results)
        for mode in args.only.split(","):
            run_mode(R, mode.strip().upper(), args.tries)
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
