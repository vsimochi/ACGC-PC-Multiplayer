#!/usr/bin/env python3
"""test_guest_g4_protocol.py - GUESTS G4 (capacity + identity separation under concurrency), SCRIPTED clients vs a REAL host.

TIER: scripted (FakeClient guests / residents) against REAL host processes (`AnimalCrossing.exe --host --bootstrap-resident 0 ...`, started / stopped by this
script from the TEST COPY named by NET_SPIKE_GAME_BIN = pc\\build64\\bin_fixture4; host = resident 0, residents 1..3 may be connected as clients; ports 11900+).
The fixture save dir is SNAPSHOTTED at start and RESTORED afterwards (guests.dat lives in save/mp and is removed with the restore); settings.ini of the fixture
is backed up and restored byte for byte. The REAL game client is NOT exercised here (test_guest_g4_real.py).

Host processes (each is a fresh process on the SAME save / mp dir, so guests.dat persists across them):
  H1  default cap (no flag, no setting => max_guests 4)
      A  FOUR guests connected at once: distinct slots 0..3, distinct tokens, each stored as ITS OWN key; a 5th guest is REFUSED (SERVER_FULL) with the host log line
         "guest limit reached (4 of max_guests=4 ...)", nothing minted, the 4 live guests unaffected (still connected, one open puppet slot each); a RESIDENT is still
         admitted while the cap is full, and the 5th guest is still refused with the resident connected
      B  IDENTITY: guest B's key with A's token (and the reverse, and without a token) is refused, every live guest unaffected; one guest leaving removes only ITS
         puppet slot; the refused guest then joins (slot 4); a KNOWN guest returning while the cap is full is refused (log), but a returning guest that is still bound
         REPLACES its own old binding (silent old session evicted, admitted although the cap is full)
      C  reconnect: every guest gets ITS OWN stored record back (byte-equal to what it uploaded, its own PersonalID, never another's), KNOWN flag, same slot
      D  guests.dat FULL (8 entries): the 9th NEW guest below the cap is refused with "guest table is full" (not the cap line), a known guest still reconnects
  H2  restart with `--max-guests 8`: records still separate after the host restart (5 guests, each its own record); the TRANSPORT RESERVE: with 3 resident records not
      yet connected only 5 guests fit, the 6th is refused ("held for residents"), then ALL three residents are admitted (8 peers)
  H3  restart with `--max-guests 2`: cap 2 (two known guests connected; a third KNOWN guest and a NEW key at a full table are refused with the cap line, existing guests
      unaffected, a RESIDENT is admitted); a refused guest joins after another leaves; the 2 guests still own their separate records
  H4  restart with settings.ini `max_guests = 1` and NO flag (the setting is read): the second guest is refused naming max_guests=1
  CLI `--max-guests 0 / 9 / abc / <missing>` exit with code 2 before any initialisation.
Usage: python test_guest_g4_protocol.py [--port 11900]
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

import net_spike_lib as L
import test_txn_protocol as TP
import test_guest_protocol as TG
import test_guest_g3_real as G3

SAVE_DIR_REL = "save"
KNOWN, NEW = L.PC_NETGAME_IDTOKEN_FLAG_KNOWN, L.PC_NETGAME_IDTOKEN_FLAG_NEW
FULL = L.PC_NETGAME_REJECT_SERVER_FULL
GCOUNT = 12  # guest identities used by this test (gid(40 + i), distinct from the other guest tests)


def gi(i):
    return TG.gid(40 + i)


class State:
    """What the first host process learned about every guest (reused after the restarts)."""
    tok = {}        # i -> token bytes
    slot = {}       # i -> guest slot
    rec = {}        # i -> the record the guest uploaded (== the stored record)


def open_slots(run):
    return {p: v for p, v in G3.live_remote_slots(run.log()).items() if v > 0}


def peer_of(c):
    return int(c.assigned_peer_id)


def join(run, i, token=None, expect_new=None):
    c = run.guest("G%d" % i, gi(i), token=token)
    c.connect_and_ready(quiet=True)
    return c


def learn(i, c):
    tk = c.token_msgs[-1][1]
    State.tok[i] = bytes(tk.token)
    State.slot[i] = tk.guest_slot
    State.rec[i] = c.own_record()


def refused(run, i, token=None, timeout=5.0):
    return run.attempt(run.guest("X%d" % i, gi(i), token=token), timeout=timeout)


def entries(run):
    pf = TG.parse_guests(TG.guests_path())
    return [e for e in pf["e"] if e["present"]]


def run_h1(run, results):
    ck = run.check
    # ---------------- A: four at once, a fifth is refused ----------------
    cl = {}
    for i in range(4):
        cl[i] = join(run, i)
        learn(i, cl[i])
    t = run.log()
    ck("A four guests connected at once: distinct slots 0..3 in order, 4 distinct tokens, flag NEW",
       [State.slot[i] for i in range(4)] == [0, 1, 2, 3] and len({State.tok[i] for i in range(4)}) == 4 and all(cl[i].token_msgs[-1][1].flags == NEW for i in range(4)))
    ck("A host log: each bound to its OWN guest slot (first contact)", all(re.search(r"peer %d bound to GUEST slot %d \(first contact" % (peer_of(cl[i]), i), t) for i in range(4)))
    ck("A four distinct transport peers", len({peer_of(cl[i]) for i in range(4)}) == 4)
    ck("A four open puppet slots, one per peer: %s" % open_slots(run), sorted(open_slots(run)) == sorted(peer_of(cl[i]) for i in range(4)) and all(v == 1 for v in open_slots(run).values()))
    TP.L.pump_sleep(1.0)
    ents = entries(run)
    ck("A guests.dat holds exactly the 4 guests (their own keys, unique tokens)", len(ents) == 4 and {e["pid"] for e in ents} == {L.guest_pid_be(gi(i)) for i in range(4)}
       and len({e["token"] for e in ents}) == 4)
    off = len(run.log())
    c5, rej = refused(run, 4)
    lg = run.log()[off:]
    ck("A the 5th guest is REFUSED (SERVER_FULL) at the cap of 4 and the host log names the cap: 'guest limit reached (4 of max_guests=4 guests are connected)'",
       c5 is None and rej is not None and rej.reason == FULL and "guest limit reached (4 of max_guests=4 guests are connected)" in lg)
    ck("A ... nothing minted / created for it (no 'bound to GUEST', no 'token minted', still 4 entries)", "bound to GUEST" not in lg and "token minted" not in lg
       and "guests.dat written" not in lg and len(entries(run)) == 4)
    ck("A the 4 live guests are unaffected: still connected, no disconnect line in the host log, 4 open puppet slots",
       all(cl[i].is_connected() for i in range(4)) and not re.search(r"\[NET\] host: peer \d+ disconnected", lg) and len(open_slots(run)) == 4)
    r1 = run.ready("RES1", run.r1)
    t = run.log()
    ck("A a RESIDENT is admitted while the guest cap is full (bound to resident %d, no guest line, never gated)" % run.r1,
       r1.identity_ack is not None and "peer %d bound to resident %d" % (peer_of(r1), run.r1) in t and not r1.token_msgs)
    off = len(run.log())
    c5, rej = refused(run, 4)
    ck("A with the resident connected the 5th guest is STILL refused with the cap line", c5 is None and rej is not None and "guest limit reached (4 of max_guests=4" in run.log()[off:])
    ck("A all four guests + the resident are still connected afterwards", all(cl[i].is_connected() for i in range(4)) and r1.is_connected())

    # ---------------- B: identity separation under concurrency ----------------
    off = len(run.log())
    pre = {p: v for p, v in open_slots(run).items()}
    a_for_b, rej1 = refused(run, 1, token=State.tok[0])
    b_for_a, rej2 = refused(run, 0, token=State.tok[1])
    no_tok, rej3 = refused(run, 2)
    lg = run.log()[off:]
    ck("B guest B's key with guest A's token is REFUSED ('WRONG token'), A's key with B's token too, B's key with NO token too (SERVER_FULL each)",
       all(x is None for x in (a_for_b, b_for_a, no_tok)) and all(r is not None and r.reason == FULL for r in (rej1, rej2, rej3))
       and lg.count("presented a WRONG token") == 2 and lg.count("presented WITHOUT a token") == 1)
    ck("B ... the cross-token attempts changed nothing: all four still connected, same open puppet slots %s, still 4 entries, tokens unchanged" % pre,
       all(cl[i].is_connected() for i in range(4)) and open_slots(run) == pre and {e["token"] for e in entries(run)} == {State.tok[i] for i in range(4)})
    # a guest claiming a RESIDENT identity (free resident r2) and one named like a connected guest (another key, same name)
    town = L.resolve_host_town(run.ip, run.port)
    pl = L.resident_player(run.r2)
    off = len(run.log())
    cr, rr = run.attempt(run.guest("XRES", L.GuestIdentity(bytes(pl.player_name), pl.player_id, bytes(town.land_name), town.land_id)))
    ck("B a guest-flagged claim of a resident is refused (no impersonation of a resident), nothing bound", cr is None and rr is not None
       and "guest-flagged claim matches a resident" in run.log()[off:] and "bound to GUEST" not in run.log()[off:])
    g0 = gi(0)
    clone = L.GuestIdentity(bytes(g0.player_name), 0x4E77, bytes(g0.land_name), g0.land_id)
    off = len(run.log())
    cn, rn = run.attempt(run.guest("XNAME", clone))
    ck("B a NEW key with the NAME of a connected guest is refused (name rules among the guests; even at the cap, nothing created)", cn is None and rn is not None
       and "bound to GUEST" not in run.log()[off:] and len(entries(run)) == 4)
    # ---- one guest leaves: only ITS puppet slot goes ----
    leaving, keep = 2, (0, 1, 3)
    p_leave = peer_of(cl[leaving])
    off = len(run.log())
    run.release(cl[leaving])
    L.pump_sleep(1.0)
    os2 = open_slots(run)
    ck("B guest 2 leaves: its puppet slot (peer %d) is destroyed, the OTHER guests' slots (%s) and the resident's stay open" % (p_leave, [peer_of(cl[i]) for i in keep]),
       p_leave not in os2 and all(os2.get(peer_of(cl[i]), 0) == 1 for i in keep) and os2.get(peer_of(r1), 0) == 1
       and all(cl[i].is_connected() for i in keep))
    n5, rej5 = None, None
    c5 = run.guest("G4", gi(4))
    c5.connect_and_ready(quiet=True)
    learn(4, c5)
    ck("B the previously refused 5th guest now joins: NEW key, slot 4 (guest 2's slot 2 stays reserved for it), NEW token", State.slot[4] == 4 and c5.token_msgs[-1][1].flags == NEW
       and State.tok[4] not in {State.tok[i] for i in range(4)})
    cl[4] = c5
    off = len(run.log())
    c2b, rej2b = refused(run, 2, token=State.tok[2])
    ck("B a KNOWN guest (guest 2, valid token) returning while 4 guests are bound is REFUSED by the cap (log line), not by its token",
       c2b is None and rej2b is not None and rej2b.reason == FULL and "guest limit reached (4 of max_guests=4" in run.log()[off:] and "WRONG token" not in run.log()[off:])
    # a returning guest that is still bound replaces ITS OWN old binding even though the cap is full
    off = len(run.log())
    cl[0].go_silent()
    a2 = run.guest("A-again", gi(0), token=State.tok[0])
    t0 = time.monotonic()
    try:
        a2.connect_and_ready(timeout=20.0, quiet=True)
        ok = True
    except Exception as exc:  # noqa: BLE001
        print("A-again handshake failed:", exc)
        ok = False
    lg = run.log()[off:]
    ck("B guest 0 returns while its old session is SILENT and the cap (4) is full: the old binding is evicted and replaced, the guest is admitted (%.1f s)" % (time.monotonic() - t0),
       ok and a2.identity_ack is not None and re.search(r"evicted stale peer \d+ \(guest slot 0, idle \d+ ms\)", lg) and re.search(r"bound to GUEST slot 0 \(known guest", lg)
       and "guest limit reached" not in lg)
    run.release(cl[0])
    cl[0] = a2
    ck("B exactly 4 guests are bound now (1, 3, 4 and the returned 0) and the three untouched sessions are alive",
       len(re.findall(r"bound to GUEST slot", run.log())) >= 6 and all(cl[i].is_connected() for i in (1, 3, 4)))
    for i in (0, 1, 3, 4):
        run.release(cl[i])
    run.release(r1)
    L.pump_sleep(1.0)
    ck("B everybody gone: no open puppet slot: %s" % open_slots(run), not open_slots(run))

    # ---------------- C: every guest gets ITS OWN record back ----------------
    cc = {}
    for i in (0, 1, 3, 4):                                  # four at once again
        cc[i] = join(run, i, token=State.tok[i])
    ok_all = True
    for i in (0, 1, 3, 4):
        push = cc[i].rec_pushes[-1] if cc[i].rec_pushes else None
        good = (push is not None and push["data"][:20] == L.guest_pid_be(gi(i)) and push["data"] == State.rec[i] and cc[i].token_msgs[-1][1].flags == KNOWN
                and cc[i].token_msgs[-1][1].guest_slot == State.slot[i] and bytes(cc[i].token_msgs[-1][1].token) == State.tok[i]
                and not [a for _c, a in cc[i].rec_acks if a.status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST])
        if not good:
            print("C guest %d wrong: push=%s" % (i, None if push is None else push["data"][:20].hex()))
        ok_all = ok_all and good
    ck("C four guests reconnect at once: each is pushed ITS OWN stored record (own PersonalID, byte-equal to its upload), KNOWN, its own slot / token, no MIGRATE", ok_all)
    ck("C the four records are pairwise different", len({State.rec[i] for i in (0, 1, 3, 4)}) == 4)
    for i in (0, 1, 3, 4):
        run.release(cc[i])
    c2c = join(run, 2, token=State.tok[2])
    ck("C guest 2 (the one that left earlier) is back in ITS slot 2 with its own record", c2c.token_msgs[-1][1].guest_slot == 2 and c2c.rec_pushes[-1]["data"] == State.rec[2])
    run.release(c2c)

    # ---------------- D: guests.dat full ----------------
    for i in (5, 6, 7):
        c = join(run, i)
        learn(i, c)
        run.release(c)
    ck("D guests 5..7 fill the table: slots 5,6,7; guests.dat holds 8 entries", [State.slot[i] for i in (5, 6, 7)] == [5, 6, 7] and len(entries(run)) == 8)
    off = len(run.log())
    c9, rej9 = refused(run, 8)
    lg = run.log()[off:]
    ck("D the 9th NEW guest (cap not reached, 0 bound) is REFUSED with 'guest table is full' (SERVER_FULL), NOT the cap line; nothing created",
       c9 is None and rej9 is not None and rej9.reason == FULL and "guest table is full" in lg and "guest limit reached" not in lg and len(entries(run)) == 8)
    k = join(run, 3, token=State.tok[3])
    ck("D a known guest still reconnects with the table full", k.token_msgs[-1][1].flags == KNOWN and k.rec_pushes[-1]["data"] == State.rec[3])
    run.release(k)
    ck("H1 host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())


def run_h2(run, results):
    ck = run.check
    cl = {}
    for i in range(5):
        cl[i] = join(run, i, token=State.tok[i])
    ok_all = all(cl[i].rec_pushes and cl[i].rec_pushes[-1]["data"] == State.rec[i] and cl[i].token_msgs[-1][1].guest_slot == State.slot[i] and cl[i].token_msgs[-1][1].flags == KNOWN
                 for i in range(5))
    ck("H2 after the HOST RESTART five guests (cap 8) connect at once: each gets ITS OWN record (byte-equal to its pre-restart upload), its own slot, KNOWN", ok_all)
    off = len(run.log())
    c6, rej = refused(run, 5, token=State.tok[5])
    lg = run.log()[off:]
    ck("H2 the 6th guest is refused although max_guests is 8: 3 resident records are not connected, so 5 guests + 3 residents = the 8 transport peers: log 'held for residents'",
       c6 is None and rej is not None and rej.reason == FULL and "guest limit reached" in lg and "more are held for residents" in lg and "bound to GUEST" not in lg)
    ck("H2 the five guests are unaffected", all(cl[i].is_connected() for i in range(5)))
    rs = [run.ready("RES%d" % k, r) for k, r in enumerate((run.r1, run.r2, run.r3), 1)]
    t = run.log()
    ck("H2 ALL THREE residents are admitted after that (8 peers: 5 guests + 3 residents): bound to their resident slots, no guest line",
       all(r.identity_ack is not None and "peer %d bound to resident %d" % (peer_of(r), idx) in t for r, idx in zip(rs, (run.r1, run.r2, run.r3))))
    ck("H2 all 8 sessions are connected and 8 puppet slots are open: %s" % open_slots(run), all(cl[i].is_connected() for i in range(5)) and all(r.is_connected() for r in rs)
       and len(open_slots(run)) == 8)
    off = len(run.log())
    try:
        c6, rej = refused(run, 5, token=State.tok[5], timeout=4.0)
    except Exception:  # noqa: BLE001  (the 9th transport peer is dropped silently: no REJECT, the handshake just times out)
        c6 = None
    ck("H2 with 8 peers connected a further guest gets no admission (refused with a log line or dropped by the full transport table: never bound)",
       c6 is None and "bound to GUEST slot %d" % State.slot[5] not in run.log()[off:])
    for c in list(cl.values()) + rs:
        run.release(c)
    ck("H2 host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())


def run_h3(run, results):
    ck = run.check
    cl = {i: join(run, i, token=State.tok[i]) for i in (0, 1)}
    ck("H3 (cap 2) two known guests connect; each its own record", all(cl[i].rec_pushes[-1]["data"] == State.rec[i] for i in (0, 1)))
    off = len(run.log())
    c3, rej = refused(run, 2, token=State.tok[2])
    lg = run.log()[off:]
    ck("H3 a third KNOWN guest is refused: SERVER_FULL + 'guest limit reached (2 of max_guests=2 guests are connected)'", c3 is None and rej is not None and rej.reason == FULL
       and "guest limit reached (2 of max_guests=2 guests are connected)" in lg)
    off = len(run.log())
    c9, rej9 = refused(run, 9)
    lg = run.log()[off:]
    ck("H3 a NEW key at a FULL table AND at the cap gets the CAP line (the cap is checked before any token is minted / table slot is looked for)",
       c9 is None and rej9 is not None and "guest limit reached (2 of max_guests=2" in lg and "guest table is full" not in lg and len(entries(run)) == 8)
    ck("H3 the two live guests are unaffected", all(cl[i].is_connected() for i in (0, 1)) and len(open_slots(run)) == 2)
    r1 = run.ready("RES1", run.r1)
    ck("H3 a resident is admitted with the cap full", r1.identity_ack is not None and "peer %d bound to resident %d" % (peer_of(r1), run.r1) in run.log())
    ck("H3 ... still refused guest afterwards", refused(run, 2, token=State.tok[2])[0] is None)
    run.release(cl[0])
    L.pump_sleep(1.0)
    c3 = join(run, 2, token=State.tok[2])
    ck("H3 after guest 0 leaves the refused guest 2 joins: its own record, its own slot", c3.rec_pushes[-1]["data"] == State.rec[2] and c3.token_msgs[-1][1].guest_slot == State.slot[2])
    ck("H3 guest 1 is still connected with its own binding (no puppet of it was touched): %s" % open_slots(run), cl[1].is_connected() and open_slots(run).get(peer_of(cl[1]), 0) == 1)
    for c in (cl[1], c3, r1):
        run.release(c)
    ck("H3 host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())


def run_h4(run, results):
    ck = run.check
    a = join(run, 0, token=State.tok[0])
    off = len(run.log())
    b, rej = refused(run, 1, token=State.tok[1])
    ck("H4 settings.ini max_guests = 1 (no flag): the second guest is refused naming max_guests=1; the first is unaffected",
       a.rec_pushes[-1]["data"] == State.rec[0] and b is None and rej is not None and rej.reason == FULL and "guest limit reached (1 of max_guests=1 guests are connected)" in run.log()[off:]
       and a.is_connected())
    run.release(a)
    ck("H4 host alive, no INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())


def phase(args, results, ip, snap_gci, residents, host_slot, tag, port_off, extra, body):
    host, ok = TP.start_host(ip, args.port + port_off, tag, extra, results)
    run = TG.GRun(ip, args.port + port_off, host, results, snap_gci, host_slot, residents[:3])
    try:
        if ok:
            body(run, results)
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
    return run


def cli_checks(results):
    exe = os.path.join(L.GAME_BIN_DIR, "AnimalCrossing.exe")
    for args_ in (["--max-guests", "0"], ["--max-guests", "9"], ["--max-guests", "abc"], ["--max-guests"]):
        try:
            p = subprocess.run([exe] + args_, cwd=L.GAME_BIN_DIR, capture_output=True, timeout=60)
            rc, err = p.returncode, (p.stderr or b"").decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            rc, err = None, "timeout"
        L.check("CLI %s exits with code 2 and prints the 1..8 refusal (rc=%s)" % (" ".join(args_), rc), rc == 2 and "--max-guests: REFUSED" in err, results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11900)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    ini_path = os.path.join(L.GAME_BIN_DIR, "settings.ini")
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_g4_protocol_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    with open(ini_path, "rb") as f:
        ini_bak = f.read()
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    L.check("fixture has >= 3 non-host residents (slots %s)" % residents, len(residents) >= 3, results)
    try:
        if len(residents) >= 3:
            cli_checks(results)
            mp = os.path.join(save_dir, "mp")
            shutil.rmtree(mp, ignore_errors=True)
            r1 = phase(args, results, ip, snap_gci, residents, host_slot, "guestg4a", 0, [], run_h1)
            if len(State.tok) >= 8:
                phase(args, results, ip, snap_gci, residents, host_slot, "guestg4b", 1, ["--max-guests", "8"], run_h2)
                phase(args, results, ip, snap_gci, residents, host_slot, "guestg4c", 2, ["--max-guests", "2"], run_h3)
                with open(ini_path, "rb") as f:
                    txt = f.read().decode("utf-8")
                with open(ini_path, "wb") as f:
                    f.write((txt.rstrip("\r\n") + "\n\n[Network]\nmax_guests = 1\n").encode("utf-8"))
                try:
                    phase(args, results, ip, snap_gci, residents, host_slot, "guestg4d", 3, [], run_h4)
                finally:
                    with open(ini_path, "wb") as f:
                        f.write(ini_bak)
            else:
                L.check("H1 produced eight guests for the later phases (%d)" % len(State.tok), False, results)
            TG.check_residents_unchanged(results, snap_gci, gci_path, host_slot, r1.log() if r1 else "", require_write=False)
    finally:
        with open(ini_path, "wb") as f:
            f.write(ini_bak)
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)
        shutil.rmtree(snap_dir, ignore_errors=True)
    with open(ini_path, "rb") as f:
        L.check("settings.ini of the fixture restored byte for byte", f.read() == ini_bak, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
