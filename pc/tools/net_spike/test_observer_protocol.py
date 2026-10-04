#!/usr/bin/env python3
"""test_observer_protocol.py - the opt-in HIDDEN SERVER OBSERVER (`--host --host-observer`), REAL HOST + FakeClients (protocol level).

TIER: PROTOCOL TESTED (a real host game process `AnimalCrossing.exe --host <port> --host-observer --verbose` from the DISPOSABLE fixture
pc\\build64\\bin_fixture4, four residents; <= 3 scripted FakeClients at a time). No visual / gameplay verification. The fixture's whole save dir is
snapshotted by net_spike_lib's guard on the first launch and restored at exit (and CloneSaveGuard here), so the fixture stays pristine.
Ports 12100+.

What it proves (see pc_host_observer.h / pc_m_card.c pc_host_observer_poll):
  B   boot: the host reaches "world ready" WITHOUT a resident (strictly after the "[NET][OBSERVER] host: observer active" line, no keystrokes, no
      --bootstrap-resident); the observer is bound as player_no 4 with the reserved PersonalID 'SERVER' (never a resident's); no failure line
  A   IDENTITY_ACK: accepted == 1|0x02 (nonzero -> every client accepts; bit 0x02 = "host has no avatar"), host name / id ZERO; the host logs it per peer
  R   ALL FOUR residents are claimable: 0 (the old host slot), 1, 2 (round 1) and 3 + 0 again (round 2) reach READY; a duplicate claim of a connected
      resident is parked and refused SERVER_FULL exactly as before; the first stays alive
  N   NO net-id-8 message ever arrives to any client: no APPEARANCE / MOVE / PLAYER_SCENE / PLAYER_ACTION with net_player_id == 8 over >= 7.5 s
      (two periodic roster resends + the join backfill); positive controls: another client's APPEARANCE (resend / backfill) and MOVE (relay) DO arrive
  C   the host logs "DISARMED reason=observer" (never "ARMED") for puppet collisions
  S   save isolation: the periodic save runs with the observer ("[PC] periodic save OK"), a graceful stop does the final shutdown save; the GCI that
      results holds residents 0..3 BYTE-IDENTICAL to the fixture's (no-op sessions), the four homes[] ownerIDs unchanged, NO occurrence of the observer's
      reserved PersonalID in the GCI / records.dat / guests.dat, and house_arrangement / private_data are untouched (the observer record is never serialised)
Usage: python test_observer_protocol.py [--port 12100]
"""
import argparse
import os
import re
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402

ACK_FLAG_HOST_NO_AVATAR = 0x02
OBSERVER_NAME = b"SERVER  "
HOST_ID = L.PC_NETGAME_HOST_PLAYER_ID
SAVE_BASE = 0x40 + 0x26000            # Save_t inside the GCI
HOMES_OFF = SAVE_BASE + 0x9CE8        # homes[4], stride 0x26B0, ownerID (PersonalID_c, 20 bytes) first
HOME_STRIDE = 0x26B0
HOUSE_ARR_OFF = SAVE_BASE + 0x2068A   # house_arrangement (u8)
APPEARANCE_HDR_FMT = "<BBBBHBB"


def appearance_payload(gender=0, face=1, cloth=0x2500):
    return struct.pack(APPEARANCE_HDR_FMT, L.PC_NETGAME_MSG_APPEARANCE, 0, gender, face, cloth, 0, 0) + b"\x00" * (L.APPEARANCE_MSG_SIZE - struct.calcsize(APPEARANCE_HDR_FMT))


class Rig:
    def __init__(self, port0, results, log_dir):
        self.port0, self.results, self.log_dir, self.n = port0, results, log_dir, 0
        self.check = lambda d, c: L.check(d, bool(c), results)
        self.host = None
        self.port = None
        self.clients = []

    def start_host(self, tag):
        self.n += 1
        port = self.port0 + self.n
        for i in range(3):
            h = L.HostProcess(port=port, extra_args=["--host-observer"], log_path=os.path.join(self.log_dir, "observer_%s_try%d.log" % (tag, i)),
                              bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_observer(timeout=90.0):
                self.host, self.port, self.tag = h, port, tag
                L.resolve_host_town("127.0.0.1", port)
                return h
            h.stop()
            time.sleep(2.0)
        return None

    def join(self, label, slot, **kw):
        c = L.FakeClient(label, "127.0.0.1", self.port, player=L.resident_player(slot), **kw)
        c.rec_resident_idx = slot
        c.connect_and_ready(quiet=True)
        self.clients.append(c)
        return c

    def release(self, c):
        try:
            if c.state in (c.STATE_CONNECTED, c.STATE_PENDING):
                c.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
        if c in self.clients:
            self.clients.remove(c)
        L.pump_sleep(0.6)

    def stop_graceful(self):
        from game_input import GameWindow
        h = self.host
        win = GameWindow(h.proc.pid)
        t0 = time.monotonic()
        while not win.ready() and time.monotonic() - t0 < 5.0:
            time.sleep(0.1)
        posted = win.close()
        deadline = time.monotonic() + 40.0
        while h.alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        code = h.proc.returncode if not h.alive() else None
        h.stop()
        self.host = None
        return posted, code


def gci_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def host_presence(c, since=0):
    """Every presence-type message tagged with the HOST's net id (8) that reached client `c`."""
    out = []
    for t in (L.PC_NETGAME_MSG_MOVE, L.PC_NETGAME_MSG_APPEARANCE, L.PC_NETGAME_MSG_PLAYER_SCENE, L.PC_NETGAME_MSG_PLAYER_ACTION):
        for m in c.inbox.peek_all(L.p_msg_type(t), since=since):
            g = m.game
            if g is not None and getattr(g, "net_player_id", None) == HOST_ID:
                out.append((t, m.index))
    return out


def run(args, results):
    rig = Rig(args.port, results, args.log_dir)
    check = rig.check
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    residents = {i: (ident, ex) for i, ident, ex in L.read_test_save_residents()}
    check("fixture has four existing residents 0..3 and no save/mp", sorted(residents) == [0, 1, 2, 3] and all(ex for _i, (_p, ex) in residents.items())
          and not os.path.exists(os.path.join(save_dir, "mp")))
    if sorted(residents) != [0, 1, 2, 3]:
        return
    pre = gci_bytes(gci_path)
    pre_recs = [L.record_from_gci(pre, i) for i in range(4)]
    pre_homes = pre[HOMES_OFF:HOMES_OFF + 4 * HOME_STRIDE]
    pre_owner_ids = [pre[HOMES_OFF + i * HOME_STRIDE:HOMES_OFF + i * HOME_STRIDE + 20] for i in range(4)]
    land_name, land_id = L.resident_land()
    observer_ids = [OBSERVER_NAME + land_name + struct.pack(">HH", 0xF0FE - k, land_id) for k in range(16)]
    check("no resident / house owner of the fixture is named 'SERVER' (the reserved identity is free)", all(OBSERVER_NAME not in r[:8] for r in pre_recs)
          and all(OBSERVER_NAME not in o for o in pre_owner_ids))

    # ================================== B: boot without a resident ==================================
    host = rig.start_host("A")
    check("B host launched with --host-observer reached 'observer active' and then 'world ready' (no keystrokes, no --bootstrap-resident)", host is not None)
    if host is None:
        return
    ht = host.log_text()
    check("B the observer was bound as a non-participant foreigner: player_no=4, PersonalID 'SERVER  '", re.search(
        r"\[PC\] --host-observer: observer bound \(not a network participant: player_no=4, PersonalID 'SERVER  ' id 0x([0-9A-F]{4})\)", ht) is not None)
    mo = re.search(r"observer bound .*PersonalID 'SERVER  ' id 0x([0-9A-F]{4})", ht)
    obs_id = int(mo.group(1), 16) if mo else 0
    check("B the reserved player_id is the first free candidate of the fixed sequence 0xF0FE, 0xF0FD, ... (no resident uses it)", 0xF0FE - 15 <= obs_id <= 0xF0FE
          and all(struct.unpack(">H", r[16:18])[0] != obs_id for r in pre_recs))
    check("B no failure line, no resident bootstrap line, no 'local save not loaded' after readiness",
          "observer init FAILED" not in ht and "--bootstrap-resident" not in ht and "resident bound, transitioning" not in ht)
    check("B no travel / passport machinery ran (no 'toNextLand refused', no 'InitGameStart refused', no 'station travel save refused'); the host is alive",
          host.alive() and "toNextLand refused" not in ht and "mCD_InitGameStart_bg refused" not in ht and "station travel save refused" not in ht
          and "mCD_SaveHome_bg refused" not in ht)
    check("B the observer's avatar is in a wait state, hidden and collider-less (log)", re.search(r"avatar main_index=\d+ \(hidden, no collider, no input\)", ht) is not None)
    check("B host log: world ready (the host serves the town without any resident bound)", L.HostProcess.WORLD_READY_RX and re.search(host.WORLD_READY_RX, ht))

    # ================================== round 1: residents 0, 1, 2 ==================================
    a = rig.join("A0", 0)
    b = rig.join("B1", 1)
    ack = a.identity_ack
    check("A resident 0 (the old HOST slot) reaches READY with the observer host", a.assigned_peer_id is not None and a.rec_synced)
    check("A IDENTITY_ACK.accepted == 1|0x02 (host has no avatar; any nonzero value is 'accepted')", ack is not None and ack.accepted == (1 | ACK_FLAG_HOST_NO_AVATAR))
    check("A IDENTITY_ACK carries NO host identity: player_name all zero and player_id 0 (the observer's identity never leaves the host)",
          bytes(ack.player_name) == b"\x00" * 8 and ack.player_id == 0)
    check("A the second client's ACK is identical in kind", b.identity_ack.accepted == (1 | ACK_FLAG_HOST_NO_AVATAR) and bytes(b.identity_ack.player_name) == b"\x00" * 8
          and b.identity_ack.player_id == 0)
    ht = host.log_text()
    check("A host log: peer %d bound to resident 0 (host-derived) and 'host has no avatar' logged for it" % a.assigned_peer_id,
          re.search(r"peer %d bound to resident 0 \(host-derived\)" % a.assigned_peer_id, ht) is not None
          and re.search(r"\[NET\]\[OBSERVER\] host: peer %d: host has no avatar" % a.assigned_peer_id, ht) is not None)
    check("A the host never refused resident 0 as 'the host's own resident'", "claimed resident is the host's own resident" not in ht)

    # duplicate claim of a connected resident: parked, then refused exactly as before; the first stays alive
    dup = L.FakeClient("DUP0", "127.0.0.1", rig.port, player=L.resident_player(0))
    rig.clients.append(dup)
    t0 = time.monotonic()
    rej = None
    try:
        dup.connect_and_ready(timeout=10.0, quiet=True)
    except L.HandshakeRejected as e:
        rej = e
    el = time.monotonic() - t0
    check("R duplicate claim of connected resident 0: parked, then REJECT(SERVER_FULL) after ~6 s (not immediately)",
          rej is not None and rej.reject.reason == L.PC_NETGAME_REJECT_SERVER_FULL and el >= 5.0)
    check("R the host logged the park for resident 0 and the first client A0 is still READY", re.search(r"peer \d+ parked \(resident 0 live on peer %d" % a.assigned_peer_id,
                                                                                                         host.log_text()) is not None and a.state == a.STATE_CONNECTED)
    rig.release(dup)

    c = rig.join("C2", 2)
    check("R resident 2 reaches READY (three real residents 0, 1, 2 are connected at once)", c.rec_synced and c.identity_ack.accepted == 3)

    # positive controls + the observation window (>= 7.5 s covers two periodic roster resends and the join backfill)
    marks = {x.label: 0 for x in (a, b, c)}
    b.send_appearance(appearance_payload(0, 2, 0x2501))
    L.pump_sleep(0.4)
    for k in range(12):
        b.send_move(k + 1, 2000.0 + k, 0.0, 760.0, move_state=1, speed=0.0)
        L.pump_sleep(0.05)
    a_saw_b_move = a.inbox.wait_for(L.p_game(L.PC_NETGAME_MSG_MOVE, net_player_id=b.assigned_peer_id), 5.0, consume=False)
    check("N positive control: client-to-client MOVE relay still works (A sees B's MOVE tagged with B's peer id %s)" % b.assigned_peer_id, a_saw_b_move is not None)
    L.pump_sleep(7.5)
    a_app_b = a.inbox.peek_all(L.p_game(L.PC_NETGAME_MSG_APPEARANCE, net_player_id=b.assigned_peer_id))
    check("N positive control: the roster path is alive without the host entry (A received B's appearance %d time(s): backfill / resend)" % len(a_app_b), len(a_app_b) >= 1)
    check("N positive control: C (joined before B's appearance) also receives B's appearance through the periodic resend (%d time(s))"
          % len(c.inbox.peek_all(L.p_game(L.PC_NETGAME_MSG_APPEARANCE, net_player_id=b.assigned_peer_id))),
          len(c.inbox.peek_all(L.p_game(L.PC_NETGAME_MSG_APPEARANCE, net_player_id=b.assigned_peer_id))) >= 1)
    for cl in (a, b, c):
        hp = host_presence(cl)
        check("N client %s: NO net-id-8 APPEARANCE / MOVE / PLAYER_SCENE / PLAYER_ACTION ever arrived (%d)" % (cl.label, len(hp)), not hp)
    ht = host.log_text()
    check("C the host never armed a puppet collider (no 'collider ARMED'); the observer disarm reason is logged when a puppet exists",
          "collider ARMED" not in ht)
    check("C host log: collider DISARMED reason=observer", "collider DISARMED reason=observer" in ht)
    check("N host log: the host never announced its own scene to clients ('local scene live ... (announcing to clients)' absent)", "(announcing to clients)" not in ht)

    # periodic save with the observer
    check("S the periodic save runs with the observer ([PC] periodic save OK within 75 s of world ready)",
          host.wait_for_log(r"\[PC\] periodic save OK", 75.0) is not None)

    # ================================== round 2: resident 3 and 0 again ==================================
    for cl in (a, b, c):
        rig.release(cl)
    L.pump_sleep(1.0)
    d = rig.join("D3", 3)
    a2 = rig.join("A0b", 0)
    check("R round 2: resident 3 and resident 0 (again, after its release) reach READY", d.rec_synced and a2.rec_synced and d.identity_ack.accepted == 3)
    check("R all four residents 0, 1, 2, 3 were claimed and bound (host log)", all(re.search(r"bound to resident %d \(host-derived\)" % i, host.log_text()) for i in range(4)))
    L.pump_sleep(3.5)
    for cl in (d, a2):
        check("N client %s (round 2): no net-id-8 presence message" % cl.label, not host_presence(cl))
    rig.release(d)
    rig.release(a2)

    # ================================== S: graceful stop + save isolation ==================================
    posted, code = rig.stop_graceful()
    ht = open(host.log_path, "rb").read().decode("utf-8", "replace")
    check("S graceful stop: window close posted, exit code 0, 'final shutdown save OK' (the observer host saves at shutdown)", posted and code == 0 and "final shutdown save OK" in ht)
    check("S no 'save FAILED' / 'REJECTED' line in the host log", "final shutdown save FAILED" not in ht and "periodic save FAILED" not in ht)
    post = gci_bytes(gci_path)
    check("S the GCI was rewritten (size unchanged: vanilla layout) by the observer host", len(post) == len(pre) and post != pre)
    post_recs = [L.record_from_gci(post, i) for i in range(4)]
    for i in range(4):
        check("S resident %d record is BYTE-IDENTICAL after the sessions (no-op client sessions, observer never merged into it)" % i, post_recs[i] == pre_recs[i])
    post_owner_ids = [post[HOMES_OFF + i * HOME_STRIDE:HOMES_OFF + i * HOME_STRIDE + 20] for i in range(4)]
    check("S the four homes[] ownerIDs are unchanged", post_owner_ids == pre_owner_ids)
    post_homes = post[HOMES_OFF:HOMES_OFF + 4 * HOME_STRIDE]
    MAILBOX = (0x1A30, 0x25D4)   # mHm_hs_c.mailbox: the vanilla system mail the day / start-up logic delivers to a RESIDENT's own house (recipient == ownerID)
    diff_offs = [k for k in range(len(pre_homes)) if pre_homes[k] != post_homes[k]]
    outside = [k % HOME_STRIDE for k in diff_offs if not MAILBOX[0] <= (k % HOME_STRIDE) < MAILBOX[1]]
    print("   INFO homes[] region: %d byte(s) differ (system mail into mailboxes of homes %s)" % (len(diff_offs), sorted({k // HOME_STRIDE for k in diff_offs})))
    check("S every changed byte of homes[] lies inside a mailbox (system letters addressed to the house's own resident); floors / ownerID / flags / "
          "gyroid / cockroach / music box of every house are untouched (%d byte(s) outside a mailbox)" % len(outside), not outside)
    check("S house_arrangement is unchanged", post[HOUSE_ARR_OFF] == pre[HOUSE_ARR_OFF])
    obs_in = [k for k, oid in enumerate(observer_ids) if oid in post]
    check("S NO occurrence of the observer's reserved PersonalID (name 'SERVER', this land, 0xF0FE..) anywhere in the GCI", not obs_in)
    check("S the observer name 'SERVER  ' appears nowhere in the GCI at a PersonalID position", all(post[o:o + 8] != OBSERVER_NAME for o in
                                                                                                    [SAVE_BASE + 0x20 + i * 0x2440 for i in range(4)] +
                                                                                                    [HOMES_OFF + i * HOME_STRIDE for i in range(4)]))
    mp = os.path.join(save_dir, "mp")
    side = {}
    for nm in ("records.dat", "guests.dat"):
        p = os.path.join(mp, nm)
        side[nm] = gci_bytes(p) if os.path.isfile(p) else b""
    check("S the sidecars (records.dat / guests.dat) never carry the observer's PersonalID either", not any(oid in blob for blob in side.values() for oid in observer_ids))
    check("S no guest table file was created (no guest ever connected)", not os.path.isfile(os.path.join(mp, "guests.dat")))
    return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12100)
    ap.add_argument("--log-dir", default=HERE)
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    with L.CloneSaveGuard():
        try:
            run(args, results)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            L.check("test aborted by an exception: %s" % e, False, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
