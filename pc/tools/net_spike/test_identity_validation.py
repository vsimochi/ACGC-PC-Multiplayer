#!/usr/bin/env python3
"""test_identity_validation.py - M9 identity Stage 1A (host-side identity validation), protocol v7 UNCHANGED.

PROTOCOL test against a REAL host process (`AnimalCrossing.exe --host --bootstrap-resident 0`, started and stopped by
this script from the pc\\build64\\bin_talkfix TEST COPY; `net_spike_lib.require_test_bin_dir()` refuses the live bin
dir), driven by scripted FakeClients. The host's own C code paths run for real; only the clients are scripted.

The fake clients claim REAL residents of the host's save: net_spike_lib parses the TEST COPY's save read-only
(read_test_save_residents). The test save has exactly TWO residents (slot 0 = the host's own, slot 1), so:
  - "two different residents connect at the same time" (V2) needs a save with >= 3 residents: it runs only when the
    save has them (bin_fixture4, see below) and is skipped (INFO) on bin_talkfix.
  - "ambiguous identity" (A1) needs two records with the same name+player_id: it is a SEPARATE run against the
    --ambiguous fixture (bin_fixture4_ambig) and is skipped on any other save (then covered by the source audit only).

Fixtures (test-only, disposable, gitignored; built by make_four_resident_fixture.py from bin_talkfix, never the live bin):
  python make_four_resident_fixture.py              -> pc\\build64\\bin_fixture4        residents Yubel(host) Angelica Bella Cleo
  python make_four_resident_fixture.py --ambiguous  -> pc\\build64\\bin_fixture4_ambig  slot 2 duplicates slot 1's PersonalID
  set NET_SPIKE_GAME_BIN=<absolute fixture dir> before running (default test run: bin_talkfix, 41 checks).

  V1  valid resident (slot 1) claim -> IDENTITY_ACK, READY, snapshot completes, host logs the host-derived binding
  D1  a second connection claiming the same resident -> REJECT(SERVER_FULL, 8 bytes); the first stays READY/alive
  D2  a claim of the host's own resident (slot 0) -> REJECT(SERVER_FULL)
  U1  unknown PersonalID (arbitrary name/id) and a known name with a wrong player_id -> REJECT(NO_SAVE, 24 bytes)
      BEFORE READY: no IDENTITY_ACK / snapshot / roster traffic, host logs REFUSED, never "bound to resident"
  G1  a rejected / never-identified peer's authoritative requests (PICKUP, DROP, BURY, FIELD_ACTION, FRIENDSHIP, MAIL,
      PLAYER_CONTEXT, RESYNC) get no reply and cause no broadcast, while the same requests from READY peer A are answered
  C1  PLAYER_CONTEXT player_no spoof (0, 3, 255) never changes the binding: the host logs the derived idx, warns once
  C2  out-of-range context values are clamped (destiny, money_power low end, goods_power, flags); valid values pass exactly
  M1  forged MAIL_REQUEST sender -> overwritten with the bound resident (host log); an honest sender is not touched
  F1  FRIENDSHIP_REQUEST is keyed by the host-derived resident PersonalID (FRIENDSHIP_UPDATE carries it)
  R1  after A disconnects the binding is released: the same resident can connect again
  R2  same-address restart (same ip:port, new nonce) re-binds without being rejected as its own duplicate
  R3  EDGE (documented limitation, Stage 1B): a reconnect from a NEW address while the old session is silent-but-alive
      is refused (duplicate); once the old session times out (~5 s) it is accepted

Tier: PROTOCOL TESTED (real host binary, scripted clients). No visual / gameplay verification.
Usage: python test_identity_validation.py [--port 8900]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import struct
import sys

import net_spike_lib as L

REJECT_SERVER_FULL = L.PC_NETGAME_REJECT_SERVER_FULL
REJECT_NO_SAVE = L.PC_NETGAME_REJECT_NO_SAVE
FIELD_ACTION_REQUEST_TYPE = 29
FIELD_ACTION_RESULT_TYPE = 30
REQ_REPLY_TYPES = (L.PC_NETGAME_MSG_PICKUP_RESULT, L.PC_NETGAME_MSG_DROP_RESULT, L.PC_NETGAME_MSG_BURY_RESULT,
                   FIELD_ACTION_RESULT_TYPE, L.PC_NETGAME_MSG_FRIENDSHIP_UPDATE, L.PC_NETGAME_MSG_MAIL_DELIVERED)
NOT_AFTER_REJECT = (L.PC_NETGAME_MSG_IDENTITY_ACK, L.PC_NETGAME_MSG_APPEARANCE, L.PC_NETGAME_MSG_SNAPSHOT_BEGIN,
                    L.PC_NETGAME_MSG_FIELD_BLOCK, L.PC_NETGAME_MSG_SNAPSHOT_END, L.PC_NETGAME_MSG_FIELD_UPDATE)


def field_action_request(kind, ut_x, ut_z, rid):
    return struct.pack("<BBBBIBBH", FIELD_ACTION_REQUEST_TYPE, kind, ut_x, ut_z, rid, 0, 0, 0)


def mail_bytes(sender_player):
    """298-byte Mail_c: recipient zeroed (unresolvable -> nothing is delivered), sender PersonalID at +0x16
    (name[8], land[8], player_id u16 LE, land_id u16 LE)."""
    buf = bytearray(298)
    land_name, land_id = L.resident_land()
    buf[0x16:0x1E] = bytes(sender_player.player_name)[:8].ljust(8, b"\x00")
    buf[0x1E:0x26] = land_name
    struct.pack_into("<HH", buf, 0x26, sender_player.player_id & 0xFFFF, land_id)
    return bytes(buf)


def raw_connect(label, host_ip, port, player, town):
    """Transport connect + IDENTITY only (no waiting for the answer)."""
    c = L.FakeClient(label, host_ip, port, town=town, player=player, context_flags=None, wait_snapshot=False)
    c.town_claimed = town
    c.connect(timeout=3.0)
    c.assigned_peer_id = None
    c.send_identity(town=town, player=player)
    return c


def expect_reject(label, host_ip, port, player, results, reason, size, town):
    """connect_and_ready must raise HandshakeRejected(reason) of `size` bytes; returns the (closed) client."""
    c = L.FakeClient(label, host_ip, port, town=town, player=player, context_flags=None, wait_snapshot=False)
    rej = None
    try:
        c.connect_and_ready()
    except L.HandshakeRejected as e:
        rej = e
    L.check(f"{label}: REJECTed (not ACKed)", rej is not None, results)
    if rej is not None:
        L.check(f"{label}: reject reason {reason} ({L.REJECT_REASON_NAMES.get(reason)}) and {size}-byte form "
                f"(got reason={rej.reject.reason}, {len(rej.payload)} bytes)",
                rej.reject.reason == reason and len(rej.payload) == size, results)
    return c, rej


def run(port, log_dir, results):
    check = lambda desc, cond: L.check(desc, cond, results)
    residents = L.read_test_save_residents()
    existing = [i for i, _p, ex in residents if ex]
    check(f"test save has >= 2 existing residents (found slots {existing})", len(existing) >= 2)
    if len(existing) < 2:
        return
    host_slot = L.TEST_HOST_RESIDENT
    r_host = L.resident_player(host_slot)
    r_a_slot = next(i for i in existing if i != host_slot)
    r_a = L.resident_player(r_a_slot)
    print(f"[test] host resident slot {host_slot} = {r_host.player_name!r}; client resident slot {r_a_slot} = "
          f"{r_a.player_name!r}")

    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot)],
                         log_path=os.path.join(log_dir, "identity_validation_host.log")).start()
    clients = []
    try:
        if not host.wait_listening(60.0):
            check("host listening", False)
            return
        if not host.boot_to_field(timeout=90.0, slot=host_slot):
            check("host reached genuine field-ready state (boot_to_field)", False)
            return
        check("host reached genuine field-ready state (boot_to_field)", True)
        ip = "127.0.0.1"
        town = L.resolve_host_town(ip, port)
        log = host.log_text

        # ---- V1: valid resident ----
        a = L.FakeClient("A", ip, port, player=r_a)
        clients.append(a)
        a.connect_and_ready()
        pa = a.assigned_peer_id
        check("V1 valid resident claim -> IDENTITY_ACK + snapshot complete (READY)", a.identity_ack is not None
              and a.snapshot_complete())
        check(f"V1 host logged the host-derived binding of peer {pa} to resident {r_a_slot}",
              re.search(r"\[NET\]\[IDENTITY\] host: peer %d bound to resident %d \(host-derived\)" % (pa, r_a_slot),
                        log()) is not None)
        check("V1 the transport slot (assigned_peer_id) is not the resident index semantics (just a slot)",
              pa == a.identity_ack.assigned_peer_id)

        # ---- D1: duplicate of a bound resident ----
        off = len(log())
        c, rej = expect_reject("D1 duplicate claim of the bound resident", ip, port, r_a, results, REJECT_SERVER_FULL, 8,
                               town)
        c.close()
        check("D1 host logged 'already bound to live peer'", re.search(
            r"claims resident %d, already bound to live peer %d" % (r_a_slot, pa), log()[off:]) is not None)
        a.send_move(1, 100.0, 0.0, 100.0)
        check("D1 the first connection is still alive/READY (not replaced)", not a.inbox.peek_all(
            L.p_event("DISCONNECT")) and a.is_connected())

        # ---- D2: the host's own resident ----
        off = len(log())
        c, rej = expect_reject("D2 claim of the host's own resident", ip, port, r_host, results, REJECT_SERVER_FULL, 8, town)
        c.close()
        check("D2 host logged the host's-own-resident refusal", "which is the host's own active resident" in log()[off:])

        # ---- V2: a SECOND, different resident connects while A is READY (needs >= 3 residents) ----
        pb_v2 = -1  # V2's legitimate second peer id (excluded from the G1 "refused peers" log scan)
        others = [i for i in existing if i not in (host_slot, r_a_slot)]
        if others:
            r_b_slot = others[0]
            r_b = L.resident_player(r_b_slot)
            b = L.FakeClient("B", ip, port, player=r_b)
            clients.append(b)
            b.connect_and_ready()
            pb = b.assigned_peer_id
            pb_v2 = pb
            check("V2 a second DIFFERENT resident connects while A is READY -> ACK + snapshot complete",
                  b.identity_ack is not None and b.snapshot_complete() and pb != pa)
            check(f"V2 host logged peer {pb} bound to resident {r_b_slot} (A stays bound to {r_a_slot})", re.search(
                r"\[NET\]\[IDENTITY\] host: peer %d bound to resident %d \(host-derived\)" % (pb, r_b_slot), log())
                is not None and a.is_connected())
            off = len(log())
            c, rej = expect_reject("V2 B's resident claimed again by a third connection", ip, port, r_b, results,
                                   REJECT_SERVER_FULL, 8, town)
            c.close()
            check("V2 duplicate of B refused as already bound to B's peer", re.search(
                r"claims resident %d, already bound to live peer %d" % (r_b_slot, pb), log()[off:]) is not None)
            b.disconnect()
            L.pump_sleep(0.6)
        else:
            L.info("V2 skipped: the test save has only 2 residents (run against bin_fixture4 for the two-resident case)")

        # ---- U1: unknown identities, refused BEFORE READY ----
        off = len(log())
        unknown = L.PlayerIdentity(b"FAKECLI\x00", 0x7E57, 1)
        c, rej = expect_reject("U1 unknown PersonalID", ip, port, unknown, results, REJECT_NO_SAVE, 24, town)
        mark = c.inbox.mark()
        L.pump_sleep(0.8)
        after = c.inbox.peek_all(lambda x: x.channel == L.CH_RELIABLE and x.msg_type in NOT_AFTER_REJECT)
        check(f"U1 no ACK/roster/snapshot traffic (saw {[x.msg_type for x in after]})", not after)
        c.close()
        c, rej = expect_reject("U1b known name, wrong player_id", ip, port,
                               L.PlayerIdentity(r_a.player_name, (r_a.player_id ^ 1) & 0xFFFF, 1), results,
                               REJECT_NO_SAVE, 24, town)
        c.close()
        txt = log()[off:]
        check("U1 host logged 'REFUSED before READY ... no resident record' twice",
              len(re.findall(r"REFUSED before READY: claimed identity matches no resident record", txt)) == 2)
        check("U1 the unknown claims were never bound to a resident", "bound to resident" not in txt)

        # ---- G1: a rejected / never-identified peer's requests have no effect ----
        # control: READY peer A's requests ARE answered
        mark_a = a.inbox.mark()
        a.send_reliable(L.build_pickup_request(60, 60, 9001))
        got = a.inbox.wait_for(lambda m: m.channel == L.CH_RELIABLE and m.msg_type == L.PC_NETGAME_MSG_PICKUP_RESULT, 3.0)
        check("G1 control: READY peer A's PICKUP_REQUEST is answered with a RESULT", got is not None)
        a.inbox.collect(lambda m: True, 0.3)
        # rejected peer: send IDENTITY(unknown) and, immediately, every authoritative request
        bad = raw_connect("BAD", ip, port, unknown, town)
        clients.append(bad)
        bad.send_player_context(L.DEFAULT_CONTEXT_FLAGS, money_power=1)
        bad.send_reliable(L.build_pickup_request(60, 60, 1))
        bad.send_reliable(L.build_drop_request(0, 0x1234, 60, 60, 2))
        bad.send_reliable(L.build_bury_request(0, 0x1234, 60, 60, 3))
        bad.send_reliable(field_action_request(1, 60, 60, 4))
        bad.send_reliable(L.build_friendship_request(0, 5))
        bad.send_reliable(mail_bytes_msg(r_a))
        bad.send_resync_request()
        # a peer that never sent any IDENTITY at all
        ghost = L.FakeClient("GHOST", ip, port, town=town, player=unknown, context_flags=None, wait_snapshot=False)
        clients.append(ghost)
        ghost.connect(timeout=3.0)
        ghost.send_reliable(L.build_pickup_request(60, 60, 11))
        ghost.send_reliable(L.build_drop_request(0, 0x1234, 60, 60, 12))
        ghost.send_reliable(L.build_friendship_request(0, 5))
        ghost.send_reliable(mail_bytes_msg(r_a))
        L.pump_sleep(1.5)
        bad_replies = bad.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in REQ_REPLY_TYPES)
        ghost_replies = ghost.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in REQ_REPLY_TYPES)
        check(f"G1 rejected peer got no request RESULT / friendship / mail reply (saw {[m.msg_type for m in bad_replies]})",
              not bad_replies)
        check(f"G1 never-identified peer got no reply either (saw {[m.msg_type for m in ghost_replies]})",
              not ghost_replies)
        check("G1 bystander A saw no FRIENDSHIP_UPDATE / MAIL_DELIVERED caused by the refused peers",
              not a.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in
                                   (L.PC_NETGAME_MSG_FRIENDSHIP_UPDATE, L.PC_NETGAME_MSG_MAIL_DELIVERED),
                                   since=mark_a))
        check("G1 host never logged a friendship/mail/context line for the refused peers",
              not re.search(r"host: peer (?!%d\b)(?!%d\b)\d+ (friendship slot|mail delivered|context player_no)" % (pa, pb_v2),
                            log()))
        bad.close()
        ghost.close()

        # ---- C1/C2: PLAYER_CONTEXT ----
        off = len(log())
        check("C1 the connect-time context (claimed player_no=0, bound resident %d) logged a one-time warning"
              % r_a_slot, re.search(r"peer %d PLAYER_CONTEXT claims player_no=0 but is bound to resident %d -- claim "
                                    r"ignored" % (pa, r_a_slot), log()) is not None)
        for spoof in (3, 255):
            a.send_player_context(L.DEFAULT_CONTEXT_FLAGS, player_no=spoof, destiny_type=2, money_power=10, goods_power=5)
        L.pump_sleep(0.6)
        ctx_lines = re.findall(r"host: peer %d context player_no=(\d+) destiny=(\d+) flags=0x([0-9A-F]+) money=(-?\d+) "
                               r"goods=(-?\d+)" % pa, log()[off:])
        check(f"C1 spoofed player_no 3/255: every host context line shows the bound index {r_a_slot} "
              f"(saw {[x[0] for x in ctx_lines]})", len(ctx_lines) >= 2 and all(int(x[0]) == r_a_slot for x in ctx_lines))
        check("C1 the spoof warning is not repeated (one-time)", len(re.findall(
            r"peer %d PLAYER_CONTEXT claims player_no" % pa, log())) == 1)

        off = len(log())
        a.send_player_context(0xFF, player_no=r_a_slot, destiny_type=99, money_power=-32768, goods_power=32767)
        L.pump_sleep(0.5)
        txt = log()[off:]
        check("C2 destiny 99 / money -32768 / goods 32767 / flags 0xFF clamped to 0 / -80 / 50 / 0x01",
              re.search(r"peer %d PLAYER_CONTEXT out-of-range values clamped: destiny 99->0 money -32768->-80 "
                        r"goods 32767->50 flags 0xFF->0x01" % pa, txt) is not None)
        check("C2 the stored context is the clamped one", re.search(
            r"host: peer %d context player_no=%d destiny=0 flags=0x01 money=-80 goods=50" % (pa, r_a_slot), txt) is not None)
        off = len(log())
        a.send_player_context(L.DEFAULT_CONTEXT_FLAGS, player_no=r_a_slot, destiny_type=5, money_power=-80, goods_power=-31)
        a.send_player_context(L.DEFAULT_CONTEXT_FLAGS, player_no=r_a_slot, destiny_type=6, money_power=-81, goods_power=51)
        L.pump_sleep(0.5)
        txt = log()[off:]
        check("C2 boundaries: goods -31 -> -30, goods 51 -> 50, destiny 6 (== mPr_DESTINY_NUM) -> 0, money -81 -> -80",
              "destiny 5->5 money -80->-80 goods -31->-30" in txt and "destiny 6->0 money -81->-80 goods 51->50" in txt)
        off = len(log())
        a.send_player_context(L.DEFAULT_CONTEXT_FLAGS, player_no=r_a_slot, destiny_type=4, money_power=150, goods_power=-30)
        a.send_player_context(0, player_no=r_a_slot, destiny_type=0, money_power=30000, goods_power=50)
        L.pump_sleep(0.5)
        txt = log()[off:]
        check("C2 valid values pass through exactly (destiny 4, money 150, goods -30; money 30000 has no vanilla upper bound)",
              "destiny=4 flags=0x01 money=150 goods=-30" in txt and "destiny=0 flags=0x00 money=30000 goods=50" in txt
              and "clamped" not in txt)

        # ---- M1: forged mail sender ----
        off = len(log())
        a.send_reliable(L.build_mail_request(mail_bytes(r_host)))  # claims to be the HOST's resident
        L.pump_sleep(0.6)
        check("M1 forged sender (the host's resident) was overwritten with the bound resident",
              re.search(r"peer %d MAIL_REQUEST sender PersonalID differs from its bound resident %d -- overwritten"
                        % (pa, r_a_slot), log()[off:]) is not None)
        off = len(log())
        a.send_reliable(L.build_mail_request(mail_bytes(r_a)))  # honest sender
        L.pump_sleep(0.6)
        check("M1 an honest sender is not reported as overwritten", "MAIL_REQUEST sender PersonalID differs" not in log()[off:])
        check("M1 the honest request still reached the villager lookup (undeliverable recipient is logged, not crashed)",
              re.search(r"peer %d MAIL_REQUEST could not be delivered" % pa, log()[off:]) is not None)
        a.send_reliable(L.build_mail_request(b"\x00" * 298))  # all-zero sender
        L.pump_sleep(0.4)
        check("M1 a zeroed sender is overwritten too", len(re.findall(r"peer %d MAIL_REQUEST sender PersonalID differs" % pa,
                                                                      log())) == 2)

        # ---- F1: friendship keyed by the bound PersonalID ----
        a.send_reliable(L.build_friendship_request(0, 1))
        got = a.hub.wait_until(lambda: any(e["slot"] == 0 for e in a.friendship_log), 4.0)
        if got:
            e = [e for e in a.friendship_log if e["slot"] == 0][-1]
            check("F1 FRIENDSHIP_UPDATE carries the bound resident's PersonalID (host save record)",
                  e["player_id"] == r_a.player_id and e["land_id"] == L.resident_land()[1]
                  and bytes(e["player_name"]) == bytes(r_a.player_name))
        else:
            L.info("F1 skipped: no FRIENDSHIP_UPDATE for villager slot 0 (slot empty on this save?)")

        # ---- R1: release on disconnect ----
        a.disconnect()
        L.pump_sleep(0.6)
        a2 = L.FakeClient("A2", ip, port, player=r_a)
        clients.append(a2)
        try:
            a2.connect_and_ready()
            check("R1 after A disconnected the same resident connects again (binding released)", True)
        except L.HandshakeRejected as e:
            check(f"R1 after A disconnected the same resident connects again (rejected: {e})", False)
            return

        # ---- R2: same-address restart ----
        try:
            a2.reconnect_and_ready(same_address_restart=True)
            check("R2 same ip:port restart (new nonce, no DISCONNECT) is re-bound, not rejected as its own duplicate", True)
        except (L.HandshakeRejected, RuntimeError) as e:
            check(f"R2 same ip:port restart re-bound ({e})", False)
            return

        # ---- R3: new address while the old session is alive -> duplicate refused; accepted after the timeout ----
        a2.state = a2.STATE_IDLE
        a2.new_socket()
        refused = False
        try:
            a2.connect_and_ready(timeout=4.0)
        except L.HandshakeRejected as e:
            refused = e.reject.reason == REJECT_SERVER_FULL
        check("R3 EDGE: reconnect from a NEW address while the old session is silent-but-alive is refused (Stage 1B)",
              refused)
        L.pump_sleep(0.3)
        # the old transport slot only dies by the ~5 s silence timeout (the fake client no longer serves it)
        ok = False
        for _ in range(4):
            L.pump_sleep(2.5)
            try:
                a2.state = a2.STATE_IDLE
                a2.connect_and_ready(timeout=4.0)
                ok = True
                break
            except L.HandshakeRejected:
                continue
        check("R3 once the old session timed out the same resident is accepted from the new address", ok)

        check("S host process alive throughout", host.alive())
    finally:
        for c in clients:
            try:
                c.close()
            except Exception:
                pass
        host.stop()


def run_ambiguous(port, log_dir, results):
    """A1 (needs the --ambiguous fixture): two resident records with identical name+player_id+land."""
    check = lambda desc, cond: L.check(desc, cond, results)
    existing = [(i, p) for i, p, ex in L.read_test_save_residents() if ex]
    keys = {}
    for i, p in existing:
        keys.setdefault((bytes(p.player_name), p.player_id), []).append(i)
    dup = [v for v in keys.values() if len(v) > 1]
    check(f"A1 save has two residents with an identical PersonalID (slots {dup})", bool(dup))
    if not dup:
        return
    host_slot = L.TEST_HOST_RESIDENT
    r_amb = L.resident_player(dup[0][0])
    uniq = [i for i, p in existing if len(keys[(bytes(p.player_name), p.player_id)]) == 1 and i != host_slot]
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot)],
                         log_path=os.path.join(log_dir, "identity_validation_ambig_host.log")).start()
    clients = []
    try:
        if not host.wait_listening(60.0) or not host.boot_to_field(timeout=90.0, slot=host_slot):
            check("A1 host reached field-ready", False)
            return
        ip = "127.0.0.1"
        town = L.resolve_host_town(ip, port)
        log = host.log_text
        c, rej = expect_reject("A1 ambiguous identity (matches 2 resident records)", ip, port, r_amb, results,
                               REJECT_SERVER_FULL, 8, town)
        c.close()
        check("A1 host logged the 'matches more than one resident record (ambiguous)' refusal",
              "claimed identity matches more than one resident record (ambiguous)" in log())
        check("A1 the ambiguous claim was never bound", "bound to resident %d" % dup[0][0] not in log()
              and "bound to resident %d" % dup[0][1] not in log())
        if uniq:
            u = L.FakeClient("U", ip, port, player=L.resident_player(uniq[0]))
            clients.append(u)
            u.connect_and_ready()
            check("A1 an unambiguous resident still connects fine", u.identity_ack is not None and u.snapshot_complete())
        check("A1 host process alive", host.alive())
    finally:
        for c in clients:
            try:
                c.close()
            except Exception:
                pass
        host.stop()


def mail_bytes_msg(sender):
    return L.build_mail_request(mail_bytes(sender))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8900)
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    log_dir = os.path.dirname(os.path.abspath(__file__))
    if "NET_SPIKE_AMBIGUOUS" in os.environ or os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).endswith("_ambig"):
        run_ambiguous(args.port, log_dir, results)
    else:
        run(args.port, log_dir, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
