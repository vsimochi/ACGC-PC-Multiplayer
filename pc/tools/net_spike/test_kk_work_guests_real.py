#!/usr/bin/env python3
"""test_kk_work_guests_real.py - K.K. song claims and Nook Work Mode with SEVERAL REAL guests (and a resident client), on REAL game processes (one REAL host, REAL guest / resident clients,
no GUI automation), on a DISPOSABLE fixture (pc\\build64\\bin_fixture4_kkwork, recreated here with the freshly built exe; its save and save/mp are snapshotted and restored).

Why it exists: capacity phase 7 moved K.K.'s once-per-concert claim and Nook's job table from slots / a shared bit / a 64-row array to the guest's PersonalID. The native tests prove the
tables; this proves the HOST of a real game uses the right identity for REAL guests whose wire ids are reused and whose processes reconnect.

Test hooks used (all TEST-ONLY, AC_TEST_HOOKS=1): host AC_TEST_KK_CONCERT=1 (K.K.'s concert is on, any weekday) and AC_TEST_KK_DAY_FILE=<file> (an integer added to the host's K.K. day stamp, so
a test can move "today"); client AC_TEST_EVNPC_CLAIM=2,<song> (the real claim TXN, once the record is synced) and AC_TEST_WORK_ENTER / AC_TEST_WORK_TYPE (Work Mode).
NOT driven: the K.K. dialogue / Nook menus on screen (GUI); the claim / work TXN is the exact path the dialogue seams call (pc_net_game_evnpc_claim_begin / the work ops).
Usage: python test_kk_work_guests_real.py [--port 12300] [--phase kk|work|all]"""
import argparse
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

NAME = "bin_fixture4_kkwork"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, NAME)
if __name__ == "__main__":
    T.make_fixture(NAME)
import net_spike_lib as L  # noqa: E402

PROFILES = {  # key: (name, PersonalID, land id): five guests with DIFFERENT PersonalIDs
    "alpha": ("Alpha", 0x4D31, 24130), "beta": ("Beta", 0x4D32, 24131), "gamma": ("Gamma", 0x4D33, 24132),
    "delta": ("Delta", 0x4D34, 24133), "eps": ("Epsln", 0x4D35, 24134),
}
CLAIM_RX = r"\[NET\]\[EVNPC\]\[TEST-ONLY\] client: claim op 2 (APPLIED|REJECTED) \(reason (\d+), granted item 0x([0-9A-F]{4})\)"
EVNPC_DONE = 36


def write_profiles(save_dir):
    os.makedirs(os.path.join(save_dir, "mp"), exist_ok=True)
    for key, (nm, pid, lid) in PROFILES.items():
        with open(os.path.join(save_dir, "mp", "guest_%s.ini" % key), "wb") as f:
            f.write(("# test profile\nname = %s\ngender = 1\nface = 5\nhome_town = Hometwn\nplayer_id = 0x%04X\nland_id = %d\n" % (nm, pid, lid)).encode("ascii"))


class Rigs:
    def __init__(self, args, results):
        self.args, self.results = args, results
        self.check = lambda d, c: L.check(d, bool(c), results)
        self.procs = []
        self.n = 0
        self.day_file = os.path.join(os.environ.get("TEMP", "."), "kk_day_%d.txt" % os.getpid())
        self.set_day(0)

    def set_day(self, n):
        with open(self.day_file, "w") as f:
            f.write(str(n))

    def host(self, env=None):
        e = {"AC_TEST_KK_CONCERT": "1", "AC_TEST_KK_DAY_FILE": self.day_file}
        e.update(env or {})
        h = L.HostProcess(port=self.args.port, extra_args=["--bootstrap-resident", str(L.TEST_HOST_RESIDENT), "--max-guests", "12", "--max-peers", "16", "-debug"], env=e,
                          log_path=os.path.join(HERE, "kkwork_host.log"), bin_dir=L.GAME_BIN_DIR).start()
        self.procs.append(h)
        ok = h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=L.TEST_HOST_RESIDENT)
        self.check("host listening and in the field", ok)
        if ok:
            L.resolve_host_town("127.0.0.1", self.args.port)
        return h if ok else None

    def guest(self, key, env=None, tag=""):
        self.n += 1
        c = L.ClientProcess("127.0.0.1:%d" % self.args.port, extra_args=["--guest", "--guest-profile", key, "-debug"], env=env or {},
                            log_path=os.path.join(HERE, "kkwork_guest_%s%s_%d.log" % (key, tag, self.n)), bin_dir=L.GAME_BIN_DIR, label="kkw" + key).start()
        self.procs.append(c)
        return c

    def resident(self, slot, env=None, tag=""):
        self.n += 1
        c = L.ClientProcess("127.0.0.1:%d" % self.args.port, extra_args=["--bootstrap-resident", str(slot), "-debug"], env=env or {},
                            log_path=os.path.join(HERE, "kkwork_res%d%s_%d.log" % (slot, tag, self.n)), bin_dir=L.GAME_BIN_DIR, label="kkwr").start()
        self.procs.append(c)
        return c

    def stop(self, p):
        try:
            p.stop()
        except Exception:  # noqa: BLE001
            pass
        if p in self.procs:
            self.procs.remove(p)

    def stop_all(self):
        for p in reversed(list(self.procs)):
            self.stop(p)


def claim_result(proc, timeout=150.0):
    m = proc.wait_for_log(CLAIM_RX, timeout)
    return None if m is None else (m.group(1), int(m.group(2)), int(m.group(3), 16))


def host_wire_id_of(host_text, name_rx_after):
    """wire id the host gave to the peer of the most recent 'READY' line (best effort, for the report only)"""
    ids = re.findall(r"\[NET\] host: peer (\d+) (?:is )?READY", host_text)
    return ids[-1] if ids else "?"


def phase_kk(R):
    check = R.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    host = R.host()
    if host is None:
        return
    song = lambda n: {"AC_TEST_EVNPC_CLAIM": "2,%d" % n}  # noqa: E731

    # K1 a RESIDENT client claims (vanilla per-slot bit), then a GUEST A claims: independent
    r1 = R.resident(residents[0], song(1), "k1")
    res = claim_result(r1)
    check("K1 resident client's claim is APPLIED (vanilla resident bit)", res is not None and res[0] == "APPLIED")
    m = re.search(r"\[NET\]\[KK\] host: song claim recorded for a resident \(record slot (\d+)\).*resident 0x([0-9A-F]+) foreigner 0x([0-9A-F]+)", host.log_text())
    check("K1 host recorded it on the RESIDENT bit of that slot and left the foreigner bit clear (%s)" % (m.group(0)[-60:] if m else "no line"),
          m is not None and bin(int(m.group(2), 16)).count("1") == 1 and int(m.group(3), 16) == 0)
    rbits = int(m.group(2), 16) if m else 0

    a = R.guest("alpha", song(2), "k2")
    res = claim_result(a)
    check("K2 guest A (PersonalID 0x4D31) claim is APPLIED", res is not None and res[0] == "APPLIED")
    ht = host.log_text()
    ms = re.findall(r"\[NET\]\[KK\] host: song claim recorded for a GUEST identity \(record slot (\d+)\); guest claims today (\d+); vanilla bits now: resident 0x([0-9A-F]+) foreigner 0x([0-9A-F]+)", ht)
    check("K2 host recorded a GUEST identity claim; the resident bits are unchanged by it and the shared foreigner bit is still clear (%s)" % (ms[-1:] ,),
          len(ms) == 1 and int(ms[0][1]) == 1 and int(ms[0][3], 16) == 0 and int(ms[0][2], 16) == rbits)

    # K3 guest B (another PersonalID) is NOT treated as 'already got one' because A did
    b = R.guest("beta", song(3), "k3")
    res = claim_result(b)
    check("K3 guest B (0x4D32) claim is APPLIED although guest A already claimed (no shared foreigner bit)", res is not None and res[0] == "APPLIED")

    # K4 wire-id reuse: A leaves, C takes A's freed id; A comes back under ANOTHER id. Claims follow the PersonalID, not the id.
    off = len(host.log_text())
    R.stop(a)
    gone = host.wait_for_log(r"\[NET\] host: peer \d+ disconnected", 30.0, since_offset=off)
    check("K4 host noticed guest A leaving", gone is not None)
    c = R.guest("gamma", song(4), "k4")
    res = claim_result(c)
    check("K4 guest C (0x4D33, joined after a leave: late join, probably reusing A's wire id) claim is APPLIED, not blocked by A's claim", res is not None and res[0] == "APPLIED")
    a2 = R.guest("alpha", song(5), "k4a")
    res = claim_result(a2)
    check("K4 guest A returns (new process, probably another wire id) and asks again: REJECTED with EVNPC_DONE (its claim follows its PersonalID)",
          res is not None and res[0] == "REJECTED" and res[1] == EVNPC_DONE)

    # wire-id evidence from the host's own lines: (peer wire id, record slot) of every K.K. guest claim
    seq = re.findall(r"host: peer (\d+) resident (\d+) (?:EVNPC claim op 2 committed|COMMIT kind=EVNPC)", host.log_text())
    L.info("K.K. claim (peer wire id, record slot) in order: %s" % seq)
    peers = [int(x[0]) for x in seq]
    check("K4 wire-id reuse really happened: guest C's claim came in on the peer id A first used (so a claim keyed by peer id would have blocked C) %s" % peers[:5],
          len(peers) >= 4 and peers[3] == peers[1] and peers[4] != peers[1])
    check("K4 A's returning claim was refused on a DIFFERENT peer id than its first claim, but the same guest record slot (%s)" % (seq[1:5],), len(seq) >= 5 and seq[4][1] == seq[1][1] and seq[4][0] != seq[1][0])

    # K5 a resident asks twice: still vanilla (one bit per slot)
    R.stop(r1)
    r2 = R.resident(residents[0], song(6), "k5")
    res = claim_result(r2)
    check("K5 the resident asks again: REJECTED with EVNPC_DONE (vanilla bit, unchanged)", res is not None and res[0] == "REJECTED" and res[1] == EVNPC_DONE)

    # K6 next day: the host's day stamp moves: A can claim again; B (who did not return) is swept at the next mark
    R.stop(a2)
    R.set_day(1)
    a3 = R.guest("alpha", song(7), "k6")
    res = claim_result(a3)
    check("K6 the next day guest A claims again: APPLIED (the once-per-day claim no longer counts)", res is not None and res[0] == "APPLIED")
    ms = re.findall(r"guest claims today (\d+)", host.log_text())
    check("K6 the day-sweep dropped yesterday's claims (claims today: %s)" % ms, ms[-1:] == ["1"])
    R.set_day(0)

    # K7 two more guests at once (the host limits NEW guest tokens to 3 per address per 60 s: class D abuse limit, kept; wait the window out like a real crowd would)
    L.info("waiting 65 s for the per-address guest-token window")
    time.sleep(65.0)
    # (D, E) after others joined: independent claims
    d = R.guest("delta", song(8), "k7")
    e = R.guest("eps", song(9), "k7")
    rd, re_ = claim_result(d), claim_result(e)
    check("K7 two simultaneous late guests D and E each get APPLIED", rd is not None and re_ is not None and rd[0] == "APPLIED" and re_[0] == "APPLIED")
    ht = host.log_text()
    check("K7 the shared foreigner bit was never touched by any guest claim of the whole run",
          all(int(x, 16) == 0 for x in re.findall(r"song claim recorded for a GUEST identity.*foreigner 0x([0-9A-F]+)", ht)))
    check("all processes alive (host)", host.alive())
    R.stop_all()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12300)
    ap.add_argument("--phase", default="all")
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    snap = os.path.join(os.environ.get("TEMP", "."), "kkwork_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    R = Rigs(args, results)
    try:
        write_profiles(save_dir)
        if args.phase in ("kk", "all"):
            phase_kk(R)
        if args.phase in ("work", "all"):
            if args.phase == "all":  # a clean save / save/mp for the second phase
                shutil.rmtree(save_dir, ignore_errors=True)
                shutil.copytree(snap, save_dir)
                write_profiles(save_dir)
            import test_kk_work_guests_work as W  # noqa: F401  (added with the Work phase)
            W.phase_work(R, save_dir)
    finally:
        R.stop_all()
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
