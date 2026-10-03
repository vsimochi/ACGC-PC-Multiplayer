#!/usr/bin/env python3
"""test_identity_validation.py - M9 identity Stage 1A (host-side identity validation), protocol v7 UNCHANGED.

PROTOCOL test against a REAL host process (`AnimalCrossing.exe --host --bootstrap-resident 0`, started and stopped by
this script from the pc\\build64\\bin_talkfix_clone TEST COPY (bin_talkfix itself is PROTECTED, never launched); `net_spike_lib.require_test_bin_dir()` refuses the live bin
dir), driven by scripted FakeClients. The host's own C code paths run for real; only the clients are scripted.

The fake clients claim REAL residents of the host's save: net_spike_lib parses the TEST COPY's save read-only
(read_test_save_residents). The test save has exactly TWO residents (slot 0 = the host's own, slot 1), so:
  - "two different residents connect at the same time" (V2) needs a save with >= 3 residents: it runs only when the
    save has them (bin_fixture4, see below) and is skipped (INFO) on bin_talkfix_clone.
  - "ambiguous identity" (A1) needs two records with the same name+player_id: it is a SEPARATE run against the
    --ambiguous fixture (bin_fixture4_ambig) and is skipped on any other save (then covered by the source audit only).

Fixtures (test-only, disposable, gitignored; built by make_four_resident_fixture.py from bin_talkfix, never the live bin):
  python make_four_resident_fixture.py              -> pc\\build64\\bin_fixture4        residents Yubel(host) Angelica Bella Cleo
  python make_four_resident_fixture.py --ambiguous  -> pc\\build64\\bin_fixture4_ambig  slot 2 duplicates slot 1's PersonalID
  set NET_SPIKE_GAME_BIN=<absolute fixture dir> before running (default test run: bin_talkfix_clone; make it with make_four_resident_fixture.py --clone-talkfix).

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
  D1/V2 (Stage 1B) a duplicate claim of a LIVE peer's resident is PARKED (no authority) for the 6 s park window and
      then REJECTed exactly as in Stage 1A; the live peer is never evicted
  S1  (replaces R3) the old peer goes silent (no DISCONNECT); the same resident from a NEW address is accepted once the
      old peer idled >= 2.5 s: park line, eviction line, old peer's DISCONNECTED teardown, only then N bound
  S3  the evicted peer's PENDING pickup reservation is released: the replacement picks up that tile at once
  S4  packets from the evicted address get no reply and have no effect (nothing relayed to the new peer)
  S5  the evicted peer's NPC talk hold is released by the teardown
  S6  eviction mid-snapshot (if the snapshot was incomplete at silence, INFO): the newcomer gets one fresh snapshot
  S2  a live heartbeating peer K is never evicted; the claimant J is parked (no ACK/REJECT/reply for 4 s, K still
      answered) and REJECTed SERVER_FULL (8 B) only after the park window; K stays alive
  (D2/U1/A1 unchanged: host-own / unknown / ambiguous are refused immediately)

Tier: PROTOCOL TESTED (real host binary, scripted clients). No visual / gameplay verification.
Usage: python test_identity_validation.py [--port 8900]
Exit code: 0 all checks passed, 1 otherwise.
"""
import argparse
import os
import re
import struct
import sys
import time

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
    return struct.pack("<BBBBIBBH64x", FIELD_ACTION_REQUEST_TYPE, kind, ut_x, ut_z, rid, 0, 0, 0)  # X3: 76 B (all-zero tag)


def mail_bytes(sender_player):
    """298-byte Mail_c: recipient zeroed (unresolvable -> nothing is delivered), sender PersonalID at +0x16
    (name[8], land[8], player_id u16 LE, land_id u16 LE)."""
    buf = bytearray(298)
    land_name, land_id = L.resident_land()
    buf[0x16:0x1E] = bytes(sender_player.player_name)[:8].ljust(8, b"\x00")
    buf[0x1E:0x26] = land_name
    struct.pack_into("<HH", buf, 0x26, sender_player.player_id & 0xFFFF, land_id)
    return bytes(buf)


DUP_WAIT_S = 10.0  # > the 6 s Stage 1B park window


def raw_connect(label, host_ip, port, player, town):
    """Transport connect + IDENTITY only (no waiting for the answer)."""
    c = L.FakeClient(label, host_ip, port, town=town, player=player, context_flags=None, wait_snapshot=False)
    c.town_claimed = town
    c.connect(timeout=3.0)
    c.assigned_peer_id = None
    c.send_identity(town=town, player=player)
    return c


def expect_reject(label, host_ip, port, player, results, reason, size, town, timeout=L.HANDSHAKE_TIMEOUT_S):
    """connect_and_ready must raise HandshakeRejected(reason) of `size` bytes; returns the (closed) client.
    c.elapsed = seconds from the call until the REJECT arrived. A duplicate-of-a-LIVE-peer claim is PARKED by the host
    for PC_NETGAME_DUP_PARK_MAX_MS (6 s) before the (unchanged) REJECT, so those callers pass timeout=DUP_WAIT_S."""
    c = L.FakeClient(label, host_ip, port, town=town, player=player, context_flags=None, wait_snapshot=False)
    rej = None
    t0 = time.monotonic()
    try:
        c.connect_and_ready(timeout=timeout)
    except L.HandshakeRejected as e:
        rej = e
    c.elapsed = time.monotonic() - t0
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

    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot), "--pickup-test-seed"],
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
                               town, timeout=DUP_WAIT_S)
        c.close()
        check(f"D1 Stage 1B: the duplicate was PARKED for the park window before the REJECT (took {c.elapsed:.1f}s, "
              f"expected 5.5..9.5)", 5.5 <= c.elapsed <= 9.5)
        check("D1 host logged 'parked (resident .. live on peer ..)'", re.search(
            r"\[NET\]\[IDENTITY\] host: peer \d+ parked \(resident %d live on peer %d, idle \d+ ms\)" % (r_a_slot, pa),
            log()[off:]) is not None)
        check("D1 host logged 'still bound to live peer' (unchanged refusal after the park window)", re.search(
            r"claims resident %d, still bound to live peer %d after 6000 ms" % (r_a_slot, pa), log()[off:]) is not None)
        check("D1 the live peer was NOT evicted (no 'evicted stale peer' line)",
              "evicted stale peer" not in log()[off:])
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
                                   REJECT_SERVER_FULL, 8, town, timeout=DUP_WAIT_S)
            c.close()
            check("V2 duplicate of B refused (after the park window) as bound to B's live peer", re.search(
                r"claims resident %d, still bound to live peer %d after 6000 ms" % (r_b_slot, pb), log()[off:]) is not None
                and "evicted stale peer" not in log()[off:] and b.is_connected())
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

        # ---- Stage 1B: stale-session handling (replaces the Stage 1A R3 "a new address is always refused") ----
        # Rule under test: a claim on a resident bound to another READY peer never evicts a LIVE (heartbeating) peer;
        # the newcomer is parked, then refused after the 6 s park window. An old peer silent >= 2.5 s is evicted through
        # the normal DISCONNECTED teardown, and only afterwards is the newcomer admitted.
        from test_client_villager_talk import host_villagers, npc_talk_msg, scene_msg, SCENE_FG

        def mark_log():
            return len(log())

        def seen_since(rx, off, wait=0.6):
            L.pump_sleep(wait)  # pump (keeps every live FakeClient heartbeating) instead of time.sleep
            return re.search(rx, log()[off:]) is not None

        # --- E1 setup: old peer X (= a2, READY, bound to r_a) holds a talk hold (S5) and a PENDING pickup reservation (S3)
        x = a2
        xp = x.assigned_peer_id
        vill = host_villagers(host)
        tslot = tnpc = None
        talk_held = False
        if vill:
            tslot, tnpc = vill[0]
            x.send_reliable(scene_msg(SCENE_FG, 1))
            L.pump_sleep(0.4)
            off = mark_log()
            x.send_reliable(npc_talk_msg(tslot, tnpc, True, 1))
            talk_held = seen_since(r"HOLD slot=%d npc=0x%04X peers=0x%04X" % (tslot, tnpc, 1 << xp), off, 0.8)
            check("S5 setup: old peer X holds villager slot %s (host HOLD line)" % tslot, talk_held)
        else:
            L.info("S5 skipped: the host log shows no spawned regular villager actors")
        seeded = len(re.findall(host.SEED_RX, log()))
        rsv = None
        rid = 7100
        for t in [t for t in L.TOWN_FIXTURE_TILES if x.world.tile_ut(*t) not in (None, L.EMPTY_NO)]:
            rid += 1
            res = x.pickup(t[0], t[1], rid, auto_confirm=False)  # provisional RESULT only: the reservation stays PENDING
            if res is not None and res.accepted:
                rsv = (t, rid)
                break
        check(f"S3 setup: old peer X holds a pending pickup reservation (tile {rsv[0] if rsv else None}; "
              f"{seeded} fixture items seeded)", rsv is not None)

        # --- S1: X goes silent (no DISCONNECT, like a crashed process); the same resident connects from a NEW address
        x.go_silent()
        off = mark_log()
        t0 = time.monotonic()
        n = L.FakeClient("N", ip, port, town=town, player=r_a)
        clients.append(n)
        n_ok = True
        try:
            n.connect_and_ready(timeout=10.0)
        except (L.HandshakeRejected, RuntimeError) as e:
            n_ok = False
            L.info(f"S1 newcomer N failed: {e}")
        el = time.monotonic() - t0
        check("S1 new-address reconnect after the old peer went silent: newcomer ACCEPTED (no REJECT) with a snapshot",
              n_ok and n.identity_ack is not None and n.snapshot_complete())
        check(f"S1 admitted only after the old peer was demonstrably idle >= 2.5 s (took {el:.1f}s, expected 2.0..7.0)",
              2.0 <= el <= 7.0)
        np_ = n.assigned_peer_id
        txt = log()[off:]
        check("S1 newcomer N got a different transport slot than the silent X", np_ is not None and np_ != xp)
        mp_ = re.search(r"\[NET\]\[IDENTITY\] host: peer %d parked \(resident %d live on peer %d, idle (\d+) ms\)" % (
            np_, r_a_slot, xp), txt)
        check("S1 host logged the park line for N (resident live on X)", mp_ is not None)
        me_ = re.search(r"\[NET\]\[IDENTITY\] host: evicted stale peer %d \(resident %d, idle (\d+) ms\) for peer %d" % (
            xp, r_a_slot, np_), txt)
        idle = int(me_.group(1)) if me_ else -1
        check(f"S1 host logged the eviction of X (idle {idle} ms, expected 2500..4600)", me_ is not None and 2500 <= idle <= 4600)
        md_ = re.search(r"\[NET\] host: peer %d disconnected" % xp, txt)
        mb_ = re.search(r"\[NET\]\[IDENTITY\] host: peer %d bound to resident %d \(host-derived\)" % (np_, r_a_slot), txt)
        check("S1 ordering: eviction line < X teardown (DISCONNECTED handled) < N bound (never admitted before the "
              "old peer's teardown)", bool(me_ and md_ and mb_) and me_.start() < md_.start() < mb_.start())
        check("S1 N was never refused", "peer %d REFUSED" % np_ not in txt)
        if talk_held:
            check("S5 the evicted peer's talk hold was released by the normal teardown (RELEASE ... peer reset)",
                  re.search(r"RELEASE slot=%d npc=0x%04X \(peer %d reset\)" % (tslot, tnpc, xp), txt) is not None)

        # --- S4: the evicted address keeps sending (RDATA / unreliable): no reply, no effect, nothing relayed to N
        x.resume()
        mk_x = x.inbox.mark()
        mk_n = n.inbox.mark()
        off = mark_log()
        x.send_reliable(L.build_pickup_request(60, 60, 7201))
        x.send_reliable(L.build_drop_request(0, 0x1234, 60, 60, 7202))
        x.send_reliable(L.build_friendship_request(0, 3))
        x.send_resync_request()
        if talk_held:
            x.send_reliable(npc_talk_msg(tslot, tnpc, True, 2))
        x.send_move(5, 100.0, 0.0, 100.0)
        L.pump_sleep(1.5)
        check("S4 evicted address got NO reply of any game message (no RESULT, no snapshot, no ACK)",
              not x.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.game is not None, since=mk_x))
        def _caused_by_x(m):
            # effects X's requests would have had on N: a RESULT/FIELD_UPDATE/FRIENDSHIP_UPDATE, a new snapshot, or
            # X's slot re-announced (APPEARANCE with net_player_id == X's slot; the host's own appearance is not X's)
            if m.channel != L.CH_RELIABLE:
                return False
            if m.msg_type in (L.PC_NETGAME_MSG_PICKUP_RESULT, L.PC_NETGAME_MSG_FIELD_UPDATE,
                              L.PC_NETGAME_MSG_FRIENDSHIP_UPDATE, L.PC_NETGAME_MSG_SNAPSHOT_BEGIN):
                return True
            return m.msg_type == L.PC_NETGAME_MSG_APPEARANCE and m.game is not None and m.game.net_player_id == xp
        n_extra = n.inbox.peek_all(_caused_by_x, since=mk_n)
        n_all = [(m.msg_type, getattr(m.game, "net_player_id", None)) for m in
                 n.inbox.peek_all(lambda m: m.channel in (L.CH_RELIABLE, L.CH_UNRELIABLE) and m.game is not None,
                                  since=mk_n) if m.msg_type not in (L.PC_NETGAME_MSG_MOVE,)]
        check(f"S4 nothing was relayed/broadcast to the new peer N because of the evicted address (offending "
              f"{[m.msg_type for m in n_extra]}; all non-MOVE msgs N saw meanwhile: {n_all})", not n_extra)
        check("S4 host logged no talk BEGIN/HOLD or new bind for the evicted slot after eviction",
              re.search(r"(BEGIN peer=%d |HOLD slot=\d+ npc=0x[0-9A-Fa-f]+ peers=0x%04X)" % (xp, 1 << xp), log()[off:])
              is None and ("peer %d bound" % xp) not in log()[off:])

        # --- S3: X's pending pickup reservation was released at once (not after the 20 s confirm timeout)
        if rsv is not None:
            res = n.pickup(rsv[0][0], rsv[0][1], 7301)
            check(f"S3 the replacement client can pick up the tile {rsv[0]} the evicted peer had reserved (< 20 s after)",
                  res is not None and res.accepted)

        # --- S6: eviction while the old peer is mid-snapshot; the newcomer gets its own complete snapshot
        n.disconnect()
        L.pump_sleep(0.8)
        m_ = L.FakeClient("M", ip, port, town=town, player=r_a, wait_snapshot=False)
        clients.append(m_)
        m_.connect_and_ready(timeout=10.0, wait_snapshot=False)
        mpid = m_.assigned_peer_id
        m_.go_silent()  # no more ACKs / heartbeats from here on
        L.pump_sleep(0.3)
        mid = not m_.snapshot_complete()
        L.info(f"S6 precondition: evicted peer M's snapshot was still incomplete when it went silent: {mid}")
        off = mark_log()
        k = L.FakeClient("K", ip, port, town=town, player=r_a)
        clients.append(k)
        k_ok = True
        try:
            k.connect_and_ready(timeout=10.0)
        except (L.HandshakeRejected, RuntimeError) as e:
            k_ok = False
            L.info(f"S6 newcomer K failed: {e}")
        txt = log()[off:]
        check("S6 newcomer K after evicting M: ACCEPTED with exactly one complete fresh snapshot",
              k_ok and len(k.completed_snapshots()) == 1)
        check("S6 host logged the eviction of M and a new snapshot epoch queued for K", re.search(
            r"evicted stale peer %d \(resident %d, idle \d+ ms\) for peer %d" % (mpid, r_a_slot, k.assigned_peer_id or -1),
            txt) is not None and re.search(r"peer %d snapshot epoch \d+ queued \(joined\)" % (k.assigned_peer_id or -1),
                                           txt) is not None)
        m_.close()

        # --- S2: a LIVE (heartbeating) peer is never evicted by a claim on its resident
        kp = k.assigned_peer_id
        off = mark_log()
        ev_before = log().count("evicted stale peer")
        j = L.FakeClient("J", ip, port, town=town, player=r_a, context_flags=None, wait_snapshot=False)
        clients.append(j)
        j.connect(timeout=3.0)
        t0 = time.monotonic()
        j.send_identity(town=town, player=r_a)
        j.send_reliable(L.build_pickup_request(60, 60, 7401))  # a parked peer holds no authority
        first = j.wait_handshake_reply(4.0)
        check("S2 while the old peer is live the newcomer is PARKED: neither ACK nor REJECT within 4 s", first is None)
        mk_k = k.inbox.mark()
        k.send_reliable(L.build_pickup_request(60, 60, 7402))
        got = k.inbox.wait_for(lambda m: m.channel == L.CH_RELIABLE and m.msg_type == L.PC_NETGAME_MSG_PICKUP_RESULT, 3.0)
        check("S2 the live old peer K is still READY and its requests are answered during the park", got is not None)
        check("S2 the parked peer got no reply / snapshot / ACK / roster (no authority)",
              not j.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in
                                   NOT_AFTER_REJECT + REQ_REPLY_TYPES))
        rej_msg = j.wait_handshake_reply(8.0)
        el = time.monotonic() - t0
        check("S2 after the park window the newcomer is REJECTed SERVER_FULL (8 bytes) exactly as in Stage 1A",
              rej_msg is not None and rej_msg.msg_type == L.PC_NETGAME_MSG_REJECT and
              rej_msg.game.reason == REJECT_SERVER_FULL and len(rej_msg.payload) == 8)
        check(f"S2 refusal came after the full park window (took {el:.1f}s, expected 5.5..9.5)", 5.5 <= el <= 9.5)
        txt = log()[off:]
        check("S2 host logged park + 'still bound to live peer' and NO eviction of K",
              re.search(r"parked \(resident %d live on peer %d" % (r_a_slot, kp), txt) is not None and
              re.search(r"still bound to live peer %d after 6000 ms" % kp, txt) is not None and
              log().count("evicted stale peer") == ev_before)
        k.send_reliable(L.build_pickup_request(60, 60, 7403))
        got = k.inbox.wait_for(lambda m: m.channel == L.CH_RELIABLE and m.msg_type == L.PC_NETGAME_MSG_PICKUP_RESULT, 3.0)
        check("S2 K is still alive and answered after J was refused", k.is_connected() and got is not None)
        j.close()

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
    with L.CloneSaveGuard():  # disposable clone save restored afterwards (no-op for other dirs)
        if "NET_SPIKE_AMBIGUOUS" in os.environ or os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).endswith("_ambig"):
            run_ambiguous(args.port, log_dir, results)
        else:
            run(args.port, log_dir, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
