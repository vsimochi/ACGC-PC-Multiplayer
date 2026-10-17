#!/usr/bin/env python3
"""test_ofurn_console_protocol.py - the OPERATOR LIST and the `furniture` console commands against a REAL `AnimalCrossing.exe --host <port> --dedicated` process with a
stdin pipe (the real console), plus scripted FakeClients. Run on a DISPOSABLE fixture copy of bin_fixture4 (NET_SPIKE_GAME_BIN=<abs path of
pc\\build64\\bin_fixture4_of_<name>>; its save dir AND its servers dir are snapshotted and restored). Ports 25000-25999 only.

  OP   `ops` empty; `op Nobody` refused; `op <offline resident>` -> servers/default/ops.ini holds `op = resident <40 hex> <name>` (identity key, not the name alone);
       a second `op` = already; `ops` lists it; a resident ONLINE gets op rights pushed at once (PERMS 0x0F) and loses them with `deop` (0x0B); a guest with the SAME
       name as a resident makes `op <name>` AMBIGUOUS (refused, both listed) and `op peer N` resolves it
  FU   a resident places a piece on the dedicated host; `furniture list` shows tile, item, rotation, the placer's name and the time; `furniture remove <x> <z>` moves it
       to the lost and found (police service blob holds the base item, the tile is empty); `furniture remove #0 delete` deletes; `furniture clear player <name>` removes
       every piece of that placer; a bad sub-command prints the usage; the server still answers `status`
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402
import test_ofurn_protocol as OP  # noqa: E402

IP = "127.0.0.1"


def say(h, cmd, settle=1.0, timeout=8.0):
    off = len(h.log_text())
    assert h.send_line(cmd)
    end = time.monotonic() + timeout
    last, last_t = off, time.monotonic()
    while time.monotonic() < end:
        L.pump_sleep(0.2)
        n = len(h.log_text())
        if n != last:
            last, last_t = n, time.monotonic()
        elif time.monotonic() - last_t >= settle and n > off:
            break
    return h.log_text()[off:].replace("\r\n", "\n")


class MiniRun:
    def __init__(self, port):
        self.ip, self.port = IP, port
        self.fresh_rid = L.make_request_id_counter(5000)
        self.clients = []


def resident(run, label, slot):
    c = L.FakeClient(label, IP, run.port, player=L.resident_player(slot))
    c.rec_resident_idx = slot
    run.clients.append(c)
    c.connect_and_ready(quiet=True)
    c.wait_record_synced(30.0)
    return c


def phase_op(h, ck, run, names, ops_path):
    out = say(h, "ops")
    ck("OP1 `ops` on a fresh server: 0 operators", "0 operator(s)" in out)
    out = say(h, "op Nobodyzz")
    ck("OP2 `op <unknown>` is refused (no player of that name)", "REFUSED" in out and "no player named" in out)
    n1 = names[1]
    out = say(h, "op %s" % n1)
    ck("OP3 `op <offline resident %s>`: now an operator" % n1, "is now an operator" in out and "resident" in out)
    txt = open(ops_path, encoding="utf-8", errors="replace").read() if os.path.isfile(ops_path) else ""
    ck("OP3 %s holds the identity KEY line `op = resident <40 hex> <name>` (versioned)" % ops_path,
       re.search(r"^version = 1$", txt, re.M) is not None and re.search(r"^op = resident [0-9a-f]{40} %s$" % re.escape(n1), txt, re.M) is not None)
    out = say(h, "op %s" % n1.lower())
    ck("OP4 a second `op` (case-insensitive name) = already an operator", "already an operator" in out)
    out = say(h, "ops")
    ck("OP5 `ops` lists it", "1 operator(s)" in out and n1 in out)
    c2 = resident(run, "R2", 2)
    ck("OP6 a resident connects with rights place|pickup|leaf (0x0B)", OP.wait(c2, lambda: OP.perms(c2) == 0x0B, 10.0))
    out = say(h, "op %s" % names[2])
    ck("OP7 `op` of the ONLINE resident: the rights are pushed at once (0x0F = +op)", "is now an operator" in out and OP.wait(c2, lambda: OP.perms(c2) == 0x0F, 6.0))
    out = say(h, "deop %s" % names[2])
    ck("OP8 `deop`: the rights drop back to 0x0B", "is no longer an operator" in out and OP.wait(c2, lambda: OP.perms(c2) == 0x0B, 6.0))
    out = say(h, "deop %s" % names[2])
    ck("OP9 `deop` again: was not an operator", "was not an operator" in out)
    try:
        # two GUESTS with the same name from two different home towns (two identities: the guest key is name + home town + ids)
        g1 = L.FakeClient("TWIN1", IP, run.port, guest=L.guest_identity("TWIN", 0x6F09, "HOMETWN", 0x5B01))
        run.clients.append(g1)
        g1.connect_and_ready(quiet=True)
        g1.wait_record_synced(30.0)
        g1.disconnect()
        L.pump_sleep(1.5)
        # (the host refuses a second guest with a name already used in this town, so two same-name identities cannot be online; `op` of an OFFLINE guest resolves
        # through the guest table)
        out = say(h, "op TWIN")
        ck("OP10 `op <offline guest>` resolves the guest-table identity: a GUEST operator", "is now an operator" in out and "guest slot" in out)
        txt = open(ops_path, encoding="utf-8", errors="replace").read()
        ck("OP10 ops.ini now holds the guest key line too", re.search(r"^op = guest [0-9a-f]{40} TWIN$", txt, re.M) is not None)
        g1.reconnect_and_ready()
        ck("OP11 the guest operator reconnects with pickup|op|leaf rights (0x0E: an operator guest still cannot place)", OP.wait(g1, lambda: OP.perms(g1) == 0x0E, 10.0))
        pi = [m for m in re.finditer(r"peer (\d+): link=READY class=RESIDENT idx 2", say(h, "players"))]
        if pi:
            out = say(h, "op peer %s" % pi[0].group(1))
            ck("OP12 `op peer N` (the online resident 2) works", "is now an operator" in out and OP.wait(c2, lambda: OP.perms(c2) == 0x0F, 6.0))
            out = say(h, "deop %s" % names[2])
            ck("OP12 and `deop` again", "is no longer an operator" in out and OP.wait(c2, lambda: OP.perms(c2) == 0x0B, 6.0))
        else:
            ck("OP12 resident 2 is listed by `players`", False)
        out = say(h, "ops")
        ck("OP13 `ops` lists the resident and the guest operator (online marked)", "2 operator(s)" in out and "guest \"TWIN\" (online)" in out)
    except Exception as exc:  # noqa: BLE001
        ck("OP10 the guest connected (%r)" % (exc,), False)
    return c2


def phase_fu(h, ck, run, c2, names):
    one, _big = OP.classify_ids(run, c2)
    ck("FU0 a 1x1 furniture id classified by the dedicated host", one is not None)
    if one is None:
        return
    pl = OP.place_somewhere(run, c2, one, rot=1)
    ck("FU1 the resident places a piece on the dedicated server", pl is not None)
    if pl is None:
        return
    (ux, uz), *_r = pl
    out = say(h, "furniture list")
    ck("FU2 `furniture list`: 1 piece, its tile, item, rotation E, placer name and time",
       "placed furniture: 1 piece" in out and re.search(r"#0: tile \(%d,%d\) acre \d+: item 0x%04X rot E, placed by resident \"%s\" at \d{4}-\d\d-\d\d \d\d:\d\d" %
                                                       (ux, uz, one, re.escape(names[2])), out) is not None)
    acre = L.town_ut_to_acre_tile(ux, uz)[0]
    out = say(h, "furniture list %d" % acre)
    ck("FU3 `furniture list <acre>` filters by acre", "(1 in acre %d)" % acre in out)
    out = say(h, "furniture remove %d %d" % (ux, uz))
    ck("FU4 `furniture remove x z`: moved to the lost and found", "moved to the lost and found" in out)

    def police_has(item):
        st = c2.ts_latest(1)
        return st is not None and item in struct.unpack_from("<20H", st[1], 0)
    ck("FU4 the police service (lost and found) now holds the base item and the tile is empty",
       OP.wait(c2, lambda: police_has(one), 6.0) and OP.wait(c2, lambda: OP.tile_val(c2, ux, uz) == 0, 6.0))
    pl = OP.place_somewhere(run, c2, one, rot=0, skip=((ux, uz),))
    if pl is not None:
        (vx, vz), *_r = pl
        out = say(h, "furniture remove #0 delete")
        ck("FU5 `furniture remove #0 delete`: DELETED (not to the lost and found), the tile empty",
           "DELETED" in out and OP.wait(c2, lambda: OP.tile_val(c2, vx, vz) == 0, 6.0))
    n = 0
    taken = [(ux, uz)]
    for _i in range(2):
        pl = OP.place_somewhere(run, c2, one, rot=0, skip=tuple(taken))
        if pl is not None:
            taken.append(pl[0])
            n += 1
    out = say(h, "furniture clear player %s" % names[2])
    ck("FU6 `furniture clear player <name>`: every piece of that placer removed (%d)" % n, n == 2 and "2 piece(s) removed" in out)
    out = say(h, "furniture bogus")
    ck("FU7 a bad sub-command prints the usage", "FURNITURE usage" in out)
    out = say(h, "help")
    ck("FU8 `help` lists ops / op / deop and furniture", "ops | op <player> | deop <player>" in out and "furniture list [acre]" in out)
    out = say(h, "status")
    ck("FU9 the server still answers `status`", "[DEDICATED] status" in out and h.proc.poll() is None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=25500)
    args = ap.parse_args()
    if not (25000 <= args.port <= 25990):
        print("ports 25000-25999 only", file=sys.stderr)
        return 2
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_of"):
        print("REFUSING: run on a pc\\build64\\bin_fixture4_of_<name> copy only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    servers = os.path.join(L.GAME_BIN_DIR, "servers")
    snap = os.path.join(os.environ.get("TEMP", "."), "ofurn_console_servers_%d" % os.getpid())
    if os.path.isdir(servers):
        shutil.copytree(servers, snap)
    names = {i: p.player_name.rstrip(b"\x00 ").decode("latin-1") for i, p, ex in L.read_test_save_residents() if ex}
    run = MiniRun(args.port)
    with L.CloneSaveGuard():
        h = None
        try:
            h = L.HostProcess(port=args.port, extra_args=["--dedicated"], log_path=os.path.join(HERE, "ofurn_console_host.log"), bin_dir=L.GAME_BIN_DIR, stdin_pipe=True,
                              verbose=False, new_group=True).start()
            ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=150.0)
            ck("dedicated host booted: observer + world + save ready", ok)
            ck("the fixture has residents 1..3 (%s)" % names, all(k in names for k in (1, 2, 3)))
            if ok and all(k in names for k in (1, 2, 3)):
                L.resolve_host_town(IP, args.port)
                c2 = phase_op(h, ck, run, names, os.path.join(L.GAME_BIN_DIR, "servers", "default", "ops.ini"))
                phase_fu(h, ck, run, c2, names)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ck("test aborted by an exception: %s" % e, False)
        finally:
            for c in run.clients:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            if h is not None:
                try:
                    h.send_line("stop")
                    h.wait_exit(45.0)
                except Exception:  # noqa: BLE001
                    pass
                if h.proc is not None and h.proc.poll() is None:
                    h.stop()
            if os.path.isdir(snap):
                shutil.rmtree(servers, ignore_errors=True)
                shutil.copytree(snap, servers)
                shutil.rmtree(snap, ignore_errors=True)
    L.discard_fixture_guest_mp(L.GAME_BIN_DIR)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
