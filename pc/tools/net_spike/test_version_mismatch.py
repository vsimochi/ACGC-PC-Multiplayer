#!/usr/bin/env python3
"""test_version_mismatch.py - Stage 1 Test F (+ foundation-phase v2 handshake rejections): send
incompatible IDENTITY messages to a REAL, currently-running `AnimalCrossing.exe --host <port>`
process and verify each is rejected cleanly.

This does not use net_spike.exe or the real game's client code (both always speak the correct
protocol) -- it hand-crafts IDENTITY with net_spike_lib.py, whose wire formats match pc_net.c
(transport: HELLO+nonce, RDATA/ACK) and pc_net_game.c (PCNetGameIdentityMsg v2 / PCNetGameRejectMsg /
PCNetGameRejectTownMsg) byte-for-byte.

  F  (original Stage 1 test) protocol_version 999 -> 8-byte REJECT(PROTOCOL_MISMATCH) carrying the
     host's protocol version (PC_NETGAME_PROTOCOL_VERSION), then a transport DISCONNECT.
  F2 version is checked FIRST: version 999 AND a wrong town still gets the 8-byte PROTOCOL_MISMATCH.
  F3 (v8) protocol_version 7 (the previous protocol) -> the same 8-byte PROTOCOL_MISMATCH reporting version 8.
  L  correct version, WRONG town -> 24-byte REJECT(LAND_MISMATCH) whose tail carries the HOST's town
     (land_name, land_id, terrain_hash) -- identical to what a matching client's IDENTITY_ACK reports
     -- and no IDENTITY_ACK / APPEARANCE / SNAPSHOT_BEGIN / FIELD_BLOCK follows; transport DISCONNECT.
  N  correct version and town but has_save=0 -> 24-byte REJECT(NO_SAVE) with the host town; DISCONNECT.
  P  probe-then-connect: the town learned from L connects successfully (IDENTITY_ACK).

Usage: python3 test_version_mismatch.py <host_ip> <port>
Run this while a real `AnimalCrossing.exe --host <port> --verbose` process is listening and in
gameplay (a v2 host defers every same-version IDENTITY until its own world is ready).
Exit code 0 when every rejection is clean and correct; 1 otherwise.
"""
import sys

import net_spike_lib as L

WRONG_PROTOCOL_VERSION = 999  # the real game requires PC_NETGAME_PROTOCOL_VERSION
NOT_AFTER_REJECT = (L.PC_NETGAME_MSG_IDENTITY_ACK, L.PC_NETGAME_MSG_APPEARANCE, L.PC_NETGAME_MSG_SNAPSHOT_BEGIN,
                    L.PC_NETGAME_MSG_FIELD_BLOCK, L.PC_NETGAME_MSG_SNAPSHOT_END, L.PC_NETGAME_MSG_FIELD_UPDATE)


def reject_roundtrip(host_ip, port, label, identity_payload, timeout=L.PROBE_TIMEOUT_S):
    """Connects, sends `identity_payload` over RDATA, returns (client, reject Msg or None)."""
    c = L.FakeClient(label, host_ip, port, town=L.ZERO_TOWN, context_flags=None, wait_snapshot=False)
    c.connect(timeout=3.0)
    c.send_reliable(identity_payload)
    m = c.inbox.wait_for(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_REJECT,
                                                                                   L.PC_NETGAME_MSG_IDENTITY_ACK),
                         timeout)
    return c, m


def reject_then_disconnect(c, m, limit=2.0):
    """(ok, detail): the transport DISCONNECT arrived AFTER the REJECT (order preserved, by inbox index) and
    within `limit` s of it. The host lingers up to ~500 ms (PC_NETGAME_REJECT_LINGER_MS) or until its REJECT
    is ACKed -- our client ACKs automatically -- before closing."""
    c.wait_disconnected(limit)
    d = c.inbox.peek_all(L.p_event("DISCONNECT"))
    if m is None or not d:
        return False, "no DISCONNECT"
    dt = d[0].t - m.t
    return d[0].index > m.index and 0 <= dt <= limit, f"DISCONNECT {dt * 1000:.0f} ms after the REJECT"


def main():
    hp = L.parse_host_port(sys.argv, "usage: test_version_mismatch.py <host_ip> <port>")
    if hp is None:
        return 1
    host_ip, port = hp
    results = []

    # --- F: the original Stage 1 test -------------------------------------------------------------------
    identity_payload = L.build_identity(town=L.ZERO_TOWN, protocol_version=WRONG_PROTOCOL_VERSION)
    assert len(identity_payload) == 32, f"identity payload is {len(identity_payload)} bytes, expected 32"
    try:
        c, m = reject_roundtrip(host_ip, port, "version-mismatch", identity_payload, timeout=3.0)
    except L.ConnectError:
        print("FAIL: no HELLO_ACK received (host not reachable / not listening?)")
        return 1
    print("received HELLO_ACK -- transport-connected")
    print(f"sent IDENTITY with protocol_version={WRONG_PROTOCOL_VERSION} (real game requires {L.PC_NETGAME_PROTOCOL_VERSION})")
    if m is None or m.msg_type != L.PC_NETGAME_MSG_REJECT:
        print("FAIL: no REJECT received in response to the bad-version IDENTITY")
        return 1
    rej = m.game
    print(f"received REJECT: reason={rej.reason} expected_protocol_version={rej.expected_protocol_version} ({len(m.payload)} bytes)")
    L.check("F connection rejected cleanly for protocol mismatch (reason and host version correct)",
            rej.reason == L.PC_NETGAME_REJECT_PROTOCOL_MISMATCH
            and rej.expected_protocol_version == L.PC_NETGAME_PROTOCOL_VERSION, results)
    L.check("F PROTOCOL_MISMATCH uses the version-stable 8-byte REJECT form", len(m.payload) == 8, results)
    L.check("F host followed the REJECT with a transport DISCONNECT", c.wait_disconnected(2.0) and c.disconnected_by_host, results)
    ok, detail = reject_then_disconnect(c, m)
    L.check(f"F REJECT is delivered BEFORE the DISCONNECT and the DISCONNECT follows within 2 s ({detail})", ok, results)
    L.check("F REJECT arrived over the reliable channel (RDATA), never legacy reliable DATA",
            m.channel == L.CH_RELIABLE and c.stats["legacy_reliable_rx"] == 0, results)
    c.close()

    # --- F3 (v8): a client speaking the PREVIOUS protocol (7) is refused with PROTOCOL_MISMATCH reporting the current one ---
    prev = L.PC_NETGAME_PROTOCOL_VERSION - 1
    c, m = reject_roundtrip(host_ip, port, "v%d-client" % prev, L.build_identity(town=L.ZERO_TOWN, protocol_version=prev),
                            timeout=3.0)
    L.check("F3 a v%d client is rejected with the 8-byte PROTOCOL_MISMATCH reporting v%d" % (prev, L.PC_NETGAME_PROTOCOL_VERSION),
            m is not None and m.msg_type == L.PC_NETGAME_MSG_REJECT and len(m.payload) == 8
            and m.game.reason == L.PC_NETGAME_REJECT_PROTOCOL_MISMATCH
            and m.game.expected_protocol_version == L.PC_NETGAME_PROTOCOL_VERSION == L.PROTOCOL_VERSION, results)
    L.check("F3 the v%d client got no IDENTITY_ACK / record traffic and the host closed the link" % prev,
            c.inbox.count(lambda x: x.channel == L.CH_RELIABLE and x.msg_type in (
                L.PC_NETGAME_MSG_IDENTITY_ACK, L.PC_NETGAME_MSG_RECORD_BEGIN, L.PC_NETGAME_MSG_RECORD_ACK)) == 0
            and c.wait_disconnected(2.0), results)
    c.close()

    # --- F2: version checked before town ---------------------------------------------------------------
    c, m = reject_roundtrip(host_ip, port, "version+town-mismatch",
                            L.build_identity(town=L.PROBE_TOWN, protocol_version=WRONG_PROTOCOL_VERSION), timeout=3.0)
    L.check("F2 wrong version AND wrong town -> PROTOCOL_MISMATCH (8 bytes), version is checked first",
            m is not None and m.msg_type == L.PC_NETGAME_MSG_REJECT and len(m.payload) == 8
            and m.game.reason == L.PC_NETGAME_REJECT_PROTOCOL_MISMATCH, results)
    c.close()

    # --- L: LAND_MISMATCH -------------------------------------------------------------------------------
    c, m = reject_roundtrip(host_ip, port, "wrong-town", L.build_identity(town=L.PROBE_TOWN))
    host_town = None
    ok_reject = m is not None and m.msg_type == L.PC_NETGAME_MSG_REJECT
    L.check("L wrong town is REJECTed (not ACKed)", ok_reject, results)
    if ok_reject:
        rt = L.decode_reject_town(m.payload)
        host_town = L.town_from_reject(m.payload)
        print(f"received REJECT: reason={m.game.reason} ({len(m.payload)} bytes) host town={host_town}")
        L.check("L reason is LAND_MISMATCH and the REJECT is the 24-byte town form",
                m.game.reason == L.PC_NETGAME_REJECT_LAND_MISMATCH and len(m.payload) == 24 and rt is not None, results)
        L.check("L REJECT carries the host protocol version", m.game.expected_protocol_version == L.PC_NETGAME_PROTOCOL_VERSION,
                results)
    mark = m.index + 1 if m is not None else 0
    L.pump_sleep(1.0)
    after = c.inbox.peek_all(lambda x: x.channel == L.CH_RELIABLE and x.msg_type in NOT_AFTER_REJECT, since=mark)
    L.check(f"L no READY/roster/snapshot traffic follows the REJECT (saw {[x.msg_type for x in after]})", not after, results)
    L.check("L host disconnected the rejected client", c.wait_disconnected(2.0) and c.disconnected_by_host, results)
    ok, detail = reject_then_disconnect(c, m)
    L.check(f"L REJECT before DISCONNECT, DISCONNECT within 2 s ({detail})", ok, results)
    c.close()

    # --- P: probe-then-connect, and the reject's town equals the ACK's town ------------------------------
    if host_town is not None:
        good = L.FakeClient("probe-then-connect", host_ip, port, town=host_town)
        try:
            good.connect_and_ready()
            L.check("P connecting with the town learned from the REJECT succeeds", True, results)
            L.check("P REJECT's host town == IDENTITY_ACK's host town (land_name, land_id, terrain_hash)",
                    L.town_from_identity_ack(good.identity_ack) == host_town, results)
        except (L.HandshakeRejected, RuntimeError) as e:
            L.check(f"P connecting with the town learned from the REJECT succeeds ({e})", False, results)
        good.close()

    # --- N: NO_SAVE --------------------------------------------------------------------------------------
    if host_town is not None:
        nosave = L.PlayerIdentity(b"NOSAVE\x00\x00", 1, 0)
        c, m = reject_roundtrip(host_ip, port, "no-save", L.build_identity(town=host_town, player=nosave))
        L.check("N has_save=0 -> 24-byte REJECT(NO_SAVE) carrying the host town",
                m is not None and m.msg_type == L.PC_NETGAME_MSG_REJECT and m.game.reason == L.PC_NETGAME_REJECT_NO_SAVE
                and len(m.payload) == 24 and L.town_from_reject(m.payload) == host_town, results)
        L.check("N host disconnected the no-save client", c.wait_disconnected(2.0) and c.disconnected_by_host, results)
        ok, detail = reject_then_disconnect(c, m)
        L.check(f"N REJECT before DISCONNECT, DISCONNECT within 2 s ({detail})", ok, results)
        c.close()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
