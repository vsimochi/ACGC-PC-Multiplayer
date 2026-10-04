#!/usr/bin/env python3
"""test_guest_g6_protocol.py - GUESTS G6 (operator tools + corrupt guest storage + shutdown), REAL DEDICATED HOSTS + scripted FakeClients + the stdin console.

TIER: scripted clients (FakeClient guests / one resident) against REAL `AnimalCrossing.exe --host <port> --dedicated` processes with a stdin pipe, started from the
DISPOSABLE fixture pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN; ports 12500+). The fixture's whole save dir is snapshotted and restored (CloneSaveGuard + the
library's automatic guard), so save/mp (guests.dat, backups, *.corrupt-*) never survives. NO real game client here (test_guest_g6_real.py), NO visual verification.

PHASES (each one a real host process on the SAME save/mp dir):
  P1  operator tools: `guests` (empty; listing never prints a token), two guests A / B first contact + token-presenting reconnect (confirmed); while A is BOUND
      `guest-remove` / `guest-reset-token` are REFUSED and change nothing (no backup, guests.dat byte-identical); without `confirm` / with bad arguments / unknown
      or unused selector: nothing changes; token LOSS: A without its token is refused; `guest-reset-token A confirm` backs guests.dat up (byte-identical copy,
      guests.dat.bak-<timestamp>) and ARMS the recovery without writing; A (no token) is then admitted ONCE with a NEW token for the SAME slot and the SAME
      stored record (byte-equal push), the entry stays unconfirmed until the new token is presented, a second token-less claim is refused again, the other guests'
      entries are byte-identical; `guest-remove B confirm` (by name, case-insensitive) backs up first, removes only B, and B can then join as a NEW character;
      the resident client stays connected and untouched throughout; `stop` exits 0.
  P2  shutdown with a guest session IN PROGRESS: guest A connected (KNOWN) when `stop` is typed: exit 0, guests.dat valid and still holding A's record unchanged,
      written at most once during the shutdown; P3 restart: A reconnects with its token -> KNOWN, pushed the byte-identical record.
  P4..P6  corrupt guests.dat (the ONLY generation: empty file / truncated / FUTURE version): the host does not crash, UNTRUSTED mode is logged, a known guest (with
      its token) and a new guest are refused, a resident is still admitted, `guests` shows nothing and `guest-remove` is refused while UNTRUSTED, the bad file is
      PRESERVED (moved aside, byte-identical to what was written), no guests.dat is (re)written, `stop` exits 0.
Usage: python test_guest_g6_protocol.py [--port 12500]
"""
import argparse
import glob
import os
import re
import shutil
import struct
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402
import test_guest_protocol as TG  # noqa: E402

FULL = L.PC_NETGAME_REJECT_SERVER_FULL
NEW, KNOWN = L.PC_NETGAME_IDTOKEN_FLAG_NEW, L.PC_NETGAME_IDTOKEN_FLAG_KNOWN
IP = "127.0.0.1"


def gpath():
    return TG.guests_path()


def mpdir():
    return os.path.dirname(gpath())


def raw_guests():
    with open(gpath(), "rb") as f:
        return f.read()


def baks():
    return sorted(glob.glob(gpath() + ".bak-*"))


class Rig:
    _n = [0]

    def __init__(self, results):
        self.results = results
        self.ck = lambda d, c: L.check(d, bool(c), results)
        self.clients = []

    def guest(self, label, g, token=None):
        Rig._n[0] += 1
        c = L.FakeClient(label, IP, self.port, guest=g, guest_token=token, bind_ip="127.0.%d.%d" % (40 + Rig._n[0] // 200, 2 + Rig._n[0] % 200))
        self.clients.append(c)
        return c

    def join(self, label, g, token=None):
        c = self.guest(label, g, token)
        c.connect_and_ready(quiet=True)
        return c

    def attempt(self, label, g, token=None, timeout=5.0):
        c = self.guest(label, g, token)
        try:
            c.connect_and_ready(timeout=timeout, quiet=True)
            return c, None
        except L.HandshakeRejected as e:
            return None, e.reject

    def resident(self, label, slot):
        c = L.FakeClient(label, IP, self.port, player=L.resident_player(slot))
        c.rec_resident_idx = slot
        c.connect_and_ready(quiet=True)
        self.clients.append(c)
        return c

    def release(self, c):
        try:
            if c.is_connected():
                c.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
        if c in self.clients:
            self.clients.remove(c)
        L.pump_sleep(0.8)

    def closeall(self):
        for c in list(self.clients):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients = []


def start(rig, tag, port, log_dir):
    h = L.HostProcess(port=port, extra_args=["--dedicated"], log_path=os.path.join(log_dir, "guest_g6_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR, stdin_pipe=True, verbose=False,
                      new_group=True).start()
    orig = h.log_text
    h.log_text = lambda: orig().replace("\r\n", "\n")
    rig.port = port
    ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
    L.check("[%s] dedicated host booted: observer + world + save ready" % tag, ok, rig.results)
    if ok:
        L.resolve_host_town(IP, port)
    return h, ok


def say(h, cmd, settle=1.2, timeout=8.0):
    """Sends one console line and returns the host log text printed AFTER it (waits until the command's [DEDICATED]/[NET][GUEST] output stopped growing)."""
    off = len(h.log_text())
    assert h.send_line(cmd)
    end = time.monotonic() + timeout
    last, last_t = off, time.monotonic()
    while time.monotonic() < end:
        L.pump_sleep(0.2)                                   # keeps the FakeClients' heartbeats / acks going (a silent peer is timed out by the host)
        n = len(h.log_text())
        if n != last:
            last, last_t = n, time.monotonic()
        elif time.monotonic() - last_t >= settle and n > off:
            break
    return h.log_text()[off:]


def stop_and_wait(h, timeout=45.0):
    h.send_line("stop")
    return h.wait_exit(timeout)


def parse():
    return TG.parse_guests(gpath())


def entry_of(g):
    pid = L.guest_pid_be(g)
    for e in parse()["e"]:
        if e["present"] and e["pid"] == pid:
            return e
    return None


# --------------------------------------------------------------------------------------------------------------------------------------
def phase1(rig, args, log_dir):
    ck = rig.ck
    gA, gB, gC = TG.gid(0), TG.gid(1), TG.gid(2)
    h, ok = start(rig, "p1", args.port, log_dir)
    if not ok:
        return h
    try:
        out = say(h, "guests")
        ck("P1 `guests` with an empty table: 'guests: none stored'", "[DEDICATED] guests: none stored" in out)
        out = say(h, "help")
        ck("P1 `help` lists guests / guest-remove / guest-reset-token", all(x in out for x in ("  guests ", "  guest-remove <slot|name> confirm", "  guest-reset-token <slot|name> confirm")))
        res = rig.resident("RES1", 1)
        a = rig.join("A", gA)
        tokA, recA = bytes(a.token_msgs[-1][1].token), a.own_record()
        b = rig.join("B", gB)
        tokB, recB = bytes(b.token_msgs[-1][1].token), b.own_record()
        ck("P1 two guests first contact: slots 0 / 1, flag NEW, two distinct tokens", (a.token_msgs[-1][1].guest_slot, b.token_msgs[-1][1].guest_slot) == (0, 1)
           and a.token_msgs[-1][1].flags == NEW and tokA != tokB)
        L.pump_sleep(1.0)
        rig.release(a)
        rig.release(b)
        a = rig.join("A2", gA, token=tokA)       # token-presenting reconnect + record step => CONFIRMED
        b2 = rig.join("B2", gB, token=tokB)
        L.pump_sleep(1.5)
        eA, eB = entry_of(gA), entry_of(gB)
        ck("P1 both entries CONFIRMED after the token-presenting reconnect (so the token is the only way back in)", eA is not None and eB is not None and eA["confirmed"] == 1 and eB["confirmed"] == 1
           and eA["token"] == tokA and eB["token"] == tokB)
        rig.release(b2)
        pre_all = raw_guests()
        # --- listing: bound flag, never a token
        out = say(h, "guests")
        ck("P1 `guests` lists slot 0 (bound=yes) and slot 1 (bound=no) with name / home town / confirmed / rev", re.search(r'slot 0: name="GUESTA" home_town="HOMETWN" host_town="[^"]*" confirmed=yes rev=\d+ bound=yes', out) is not None
           and re.search(r'slot 1: name="GUESTB" home_town="HOMETWN" host_town="[^"]*" confirmed=yes rev=\d+ bound=no', out) is not None)
        ck("P1 `guests` never prints a token (neither full nor the first 4 hex chars)", tokA.hex() not in out and tokB.hex() not in out and tokA.hex()[:8] not in out and tokB.hex()[:8] not in out
           and "token=" not in out)
        # --- bound guest: both commands refused, nothing changes
        o1 = say(h, "guest-remove 0 confirm")
        o2 = say(h, "guest-reset-token guesta confirm")
        ck("P1 `guest-remove` / `guest-reset-token` of a BOUND guest are refused ('is connected on peer')", "guest-remove: refused: guest slot 0" in o1 and "is connected on peer" in o1
           and "guest-reset-token: refused: guest slot 0" in o2 and "is connected on peer" in o2)
        ck("P1 ... nothing changed: guests.dat byte-identical, no backup file, the bound guest still connected", raw_guests() == pre_all and not baks() and a.is_connected())
        rig.release(a)
        # --- no confirm / bad arguments / bad selectors
        o = say(h, "guest-remove guesta")
        o_b = say(h, "guest-remove guesta yes")
        o_c = say(h, "guest-remove guesta confirm extra")
        o_d = say(h, "guest-remove nobody confirm")
        o_e = say(h, "guest-remove 7 confirm")
        o_f = say(h, "guest-reset-token 0")
        o_g = say(h, "guest-remove")
        ck("P1 without `confirm` the command only says what it would do ('re-run with `confirm`')", "REMOVES the guest" in o and "re-run with `confirm`" in o and "re-run with `confirm`" in o_f and "NEW token" in o_f)
        ck("P1 unknown trailing word / extra arguments are refused ('expected exactly'), unknown name / unused slot / no argument are refused",
           "expected exactly" in o_b and "expected exactly" in o_c and "no guest named" in o_d and "guest slot 7 is not in use" in o_e and "usage:" in o_g)
        ck("P1 ... none of them changed anything: guests.dat byte-identical, no backup file", raw_guests() == pre_all and not baks())
        # --- token LOSS: the real guest has no token any more
        off = len(h.log_text())
        c, rej = rig.attempt("A-notoken", gA)
        lg = h.log_text()[off:]
        ck("P1 token loss: A WITHOUT its token is refused (SERVER_FULL) -- 'presented WITHOUT a token'", c is None and rej is not None and rej.reason == FULL and "presented WITHOUT a token" in lg)
        # --- reset-token arms the recovery (backup first, no write, nothing else touched)
        gen_before = parse()["gen"]
        o = say(h, "guest-reset-token guesta confirm")
        bk = baks()
        ck("P1 `guest-reset-token A confirm`: ARMED, a backup guests.dat.bak-<timestamp> exists and is BYTE-IDENTICAL to guests.dat as it was", "token recovery ARMED" in o and len(bk) == 1
           and open(bk[0], "rb").read() == pre_all)
        ck("P1 ... arming wrote nothing: guests.dat byte-identical, generation unchanged", raw_guests() == pre_all and parse()["gen"] == gen_before)
        o = say(h, "guests")
        ck("P1 `guests` shows RECOVERY-ARMED for A only", re.search(r'slot 0: .*RECOVERY-ARMED', o) is not None and not re.search(r'slot 1: .*RECOVERY-ARMED', o))
        # a NEW unrelated guest is unaffected by the armed recovery
        cc = rig.join("C", gC)
        ck("P1 an unrelated NEW guest still gets its own slot / token while the recovery is armed (slot 2, NEW)", cc.token_msgs[-1][1].guest_slot == 2 and cc.token_msgs[-1][1].flags == NEW
           and bytes(cc.token_msgs[-1][1].token) not in (tokA, tokB))
        rig.release(cc)
        L.pump_sleep(1.0)
        pre_C = entry_of(gC)
        # --- the real guest comes back WITHOUT a token: ONE re-mint for the SAME record
        off = len(h.log_text())
        a3 = rig.join("A3", gA)
        tk = a3.token_msgs[-1][1]
        newA = bytes(tk.token)
        lg = h.log_text()[off:]
        ck("P1 recovery: A (no token) is admitted to the SAME slot 0 with flag NEW and a DIFFERENT token (the old one is dead)", tk.guest_slot == 0 and tk.flags == NEW and newA != tokA
           and "operator RECOVERY pending" in lg)
        ck("P1 recovery: the host log says the operator recovery was used and the stored record is kept", "operator recovery of guest slot 0 used" in lg)
        ck("P1 recovery: A is pushed its OWN stored record byte for byte (same character, not a fresh one)", a3.rec_pushes and a3.rec_pushes[-1]["data"] == recA)
        L.pump_sleep(1.2)
        eA2 = entry_of(gA)
        ck("P1 recovery: guests.dat holds the NEW token, same slot / record / rev, entry UNCONFIRMED until the new token is presented", eA2 is not None and eA2["token"] == newA
           and eA2["record"] == recA and eA2["rev"] == eA["rev"] and eA2["confirmed"] == 0 and eA2["pid"] == eA["pid"])
        eB_now, eC_now = entry_of(gB), entry_of(gC)
        ck("P1 the OTHER guests' entries are byte-identical (B unchanged, C unchanged since it joined)", eB_now == eB and eC_now == pre_C)
        rig.release(a3)
        # --- one use only
        off = len(h.log_text())
        c, rej = rig.attempt("A-again", gA)
        ck("P1 the recovery was ONE use: another token-less claim of A is refused again", c is None and rej is not None and rej.reason == FULL and "presented WITHOUT a token" in h.log_text()[off:])
        c, rej = rig.attempt("A-old", gA, token=tokA)
        ck("P1 the OLD token is dead (refused, 'WRONG token')", c is None and rej is not None and "presented a WRONG token" in h.log_text()[off:])
        a4 = rig.join("A4", gA, token=newA)
        L.pump_sleep(1.5)
        ck("P1 the NEW token works (KNOWN, same slot, same record) and CONFIRMS the entry again", a4.token_msgs[-1][1].flags == KNOWN and a4.token_msgs[-1][1].guest_slot == 0
           and a4.rec_pushes[-1]["data"] == recA and entry_of(gA)["confirmed"] == 1)
        rig.release(a4)
        # --- remove B by NAME (case-insensitive)
        before_rm = raw_guests()
        n_bak = len(baks())
        o = say(h, "guest-remove GuestB confirm")
        bk = baks()
        ck("P1 `guest-remove GuestB confirm`: REMOVED, a NEW backup exists and equals guests.dat as it was before the removal", "REMOVED" in o and len(bk) == n_bak + 1
           and any(open(p, "rb").read() == before_rm for p in bk))
        pf = parse()
        names = [e["pid"][:8].rstrip(b" \x00") for e in pf["e"] if e["present"]]
        ck("P1 only B is gone: guests.dat holds A and C (their entries byte-identical), generation advanced", sorted(names) == [b"GUESTA", b"GUESTC"] and entry_of(gA) is not None
           and entry_of(gC) == pre_C)
        ck("P1 ... the removal of B is in the host log (ADMIN ... REMOVED by the operator (backup ...))", re.search(r"\[NET\]\[GUEST\] ADMIN: guest slot 1 \(\"GUESTB\"\) REMOVED by the operator \(backup ", h.log_text()) is not None)
        off = len(h.log_text())
        b3 = rig.join("B3", gB, token=tokB)
        tkb = b3.token_msgs[-1][1]
        lg = h.log_text()[off:]
        ck("P1 B (old token, entry gone) can join again as a NEW character: first contact, NEW token, FRESH record (not the old record)",
           tkb.flags == NEW and bytes(tkb.token) != tokB and "has no entry for (host table reset / other host): treated as a first contact" in lg)
        rig.release(b3)
        ck("P1 the RESIDENT client was never disturbed (still connected) and no INTERNAL error", res.is_connected() and h.alive() and "*** INTERNAL" not in h.log_text())
        code = stop_and_wait(h)
        ck("P1 `stop`: exit code 0 (got %s)" % code, code == 0)
        try:
            parse()
            ck("P1 guests.dat valid after the shutdown", True)
        except AssertionError as ex:
            ck("P1 guests.dat valid after the shutdown (%s)" % ex, False)
    finally:
        rig.closeall()
        h.stop()
    return h


def phase2(rig, args, log_dir):
    ck = rig.ck
    gA = TG.gid(0)
    eA = entry_of(gA)
    if eA is None:
        ck("P2 precondition: guest A is stored", False)
        return
    tok, rec = eA["token"], eA["record"]
    h, ok = start(rig, "p2", args.port + 1, log_dir)
    if not ok:
        return
    try:
        a = rig.join("A", gA, token=tok)
        ck("P2 A reconnects with its token (KNOWN) and is pushed its stored record", a.token_msgs[-1][1].flags == KNOWN and a.rec_pushes[-1]["data"] == rec)
        L.pump_sleep(1.0)
        before = raw_guests()
        off = len(h.log_text())
        h.send_line("stop")                                  # the guest is STILL connected
        code = h.wait_exit(45.0)
        lg = h.log_text()[off:]
        ck("P2 `stop` with a guest session in progress: exit code 0 (got %s), 'shutdown: final save OK' logged once" % code, code == 0 and lg.count("[DEDICATED] shutdown: final save OK") == 1)
        ck("P2 guests.dat was written at most ONCE during the shutdown (%d)" % lg.count("guests.dat written"), lg.count("guests.dat written") <= 1)
        try:
            e = entry_of(gA)
            ck("P2 guests.dat valid; A's entry holds the SAME token and the byte-identical record; no other entry appeared", e is not None and e["token"] == tok and e["record"] == rec
               and sum(1 for x in parse()["e"] if x["present"]) == sum(1 for x in TG.parse_guests(gpath())["e"] if x["present"]))
        except AssertionError as ex:
            ck("P2 guests.dat valid after the shutdown (%s)" % ex, False)
        ck("P2 the file the host left is not a half-written one (size == the format size)", os.path.getsize(gpath()) == TG.GUEST_FILE)
    finally:
        rig.closeall()
        h.stop()
    h, ok = start(rig, "p3", args.port + 2, log_dir)
    if not ok:
        return
    try:
        a = rig.join("A", gA, token=tok)
        ck("P3 restart: A reconnects with its token -> KNOWN, same slot, pushed the byte-identical record", a.token_msgs[-1][1].flags == KNOWN and a.rec_pushes[-1]["data"] == rec)
        rig.release(a)
        code = stop_and_wait(h)
        ck("P3 `stop`: exit 0", code == 0)
    finally:
        rig.closeall()
        h.stop()


def corrupt_phase(rig, args, log_dir, tag, port, kind, valid_bytes):
    ck = rig.ck
    gA, gN = TG.gid(0), TG.gid(5)
    eA = entry_of_bytes(valid_bytes, gA)
    tok = eA["token"] if eA else b"\x11" * 16
    for p in glob.glob(gpath() + "*"):
        os.remove(p)                                           # disposable fixture only: the previous generations / backups / aside copies are ours
    if kind == "empty":
        bad = b""
    elif kind == "truncated":
        bad = valid_bytes[:5000]
    else:                                                       # FUTURE version, otherwise a perfect file (version 3 + a re-computed CRC)
        buf = bytearray(valid_bytes)
        struct.pack_into("<I", buf, 8, 3)
        struct.pack_into("<I", buf, len(buf) - 4, zlib.crc32(bytes(buf[:-4])) & 0xFFFFFFFF)
        bad = bytes(buf)
    with open(gpath(), "wb") as f:
        f.write(bad)
    h, ok = start(rig, tag, port, log_dir)
    if not ok:
        return
    try:
        t = h.log_text()
        ck("%s host started over a %s guests.dat (no crash) and logged UNTRUSTED mode" % (tag, kind), h.alive() and "UNTRUSTED mode" in t and "NO guest is admitted" in t)
        asides = glob.glob(gpath() + ".corrupt-*")
        ck("%s the bad file was PRESERVED (moved aside, byte-identical to what was written) and no guests.dat was created" % tag,
           len(asides) == 1 and open(asides[0], "rb").read() == bad and not os.path.isfile(gpath()))
        off = len(h.log_text())
        c1, r1 = rig.attempt(tag + "-known", gA, token=tok)
        c2, r2 = rig.attempt(tag + "-new", gN)
        lg = h.log_text()[off:]
        ck("%s a KNOWN guest (with its token) and a NEW guest are refused with the existing reason SERVER_FULL; the host logs 'guest admission disabled ... UNTRUSTED'" % tag,
           c1 is None and c2 is None and r1 is not None and r2 is not None and r1.reason == FULL and r2.reason == FULL and lg.count("guest admission disabled: guests.dat is UNTRUSTED") == 2)
        res = rig.resident(tag + "-res", 1)
        ck("%s a RESIDENT is still admitted (residents are unaffected)" % tag, res.is_connected() and "bound to resident 1" in h.log_text())
        o = say(h, "guests")
        o2 = say(h, "guest-remove 0 confirm")
        ck("%s `guests` lists nothing and `guest-remove` is refused ('UNTRUSTED'): nothing can be changed" % tag, "guests: none stored" in o and "refused: guests.dat is UNTRUSTED" in o2 and not os.path.isfile(gpath())
           and not baks())
        ck("%s the host is alive, no INTERNAL error" % tag, h.alive() and "*** INTERNAL" not in h.log_text())
        code = stop_and_wait(h)
        ck("%s `stop`: exit 0, still no guests.dat, the bad file still preserved byte-identically (%s)" % (tag, code), code == 0 and not os.path.isfile(gpath())
           and [open(p, "rb").read() for p in glob.glob(gpath() + ".corrupt-*")] == [bad])
    finally:
        rig.closeall()
        h.stop()


def entry_of_bytes(raw, g):
    pid = L.guest_pid_be(g)
    buf = os.path.join(os.environ.get("TEMP", "."), "g6_valid_%d.dat" % os.getpid())
    with open(buf, "wb") as f:
        f.write(raw)
    try:
        for e in TG.parse_guests(buf)["e"]:
            if e["present"] and e["pid"] == pid:
                return e
    finally:
        os.remove(buf)
    return None


def run(args, results):
    rig = Rig(results)
    log_dir = args.log_dir
    shutil.rmtree(mpdir(), ignore_errors=True)
    L.check("fixture has four residents and no save/mp", len(L.read_test_save_residents()) == 4 and not os.path.exists(mpdir()), results)
    phase1(rig, args, log_dir)
    if os.path.isfile(gpath()):
        valid = raw_guests()
        phase2(rig, args, log_dir)
        valid = raw_guests()
        for i, kind in enumerate(("empty", "truncated", "future")):
            corrupt_phase(rig, args, log_dir, "P%d" % (4 + i), args.port + 3 + i, kind, valid)
    else:
        L.check("P1 left a guests.dat for the later phases", False, results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12500)
    ap.add_argument("--log-dir", default=HERE)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test mutates the game save; run it on pc\\build64\\bin_fixture4 only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    with L.CloneSaveGuard():
        try:
            run(args, results)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            L.check("test aborted by an exception: %s" % e, False, results)
    L.discard_fixture_guest_mp(L.GAME_BIN_DIR)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
