#!/usr/bin/env python3
"""test_d3_record_protocol.py - D3-1 / D3-2 (HOST half) of the host-mirrored resident record, protocol v8.

PROTOCOL test against a REAL host process (`AnimalCrossing.exe --host --bootstrap-resident 0`, started and stopped by this
script from the TEST COPY named by NET_SPIKE_GAME_BIN; use the 4-resident fixture pc\\build64\\bin_fixture4: host = resident 0,
clients = residents 1..3, at most 3 simultaneous clients here). `require_test_bin_dir()` refuses the live bin dir. The fixture's
save is SNAPSHOTTED at start and RESTORED after each host stop (a host autosave would otherwise leave merged test records in
the disposable fixture). Clients are scripted FakeClients speaking the v8 record wire (net_spike_lib); the host's C code runs
for real.

  T1  READY peer -> HELLO -> (rev 0) MIGRATE_REQUEST -> MIGRATE_UPLOAD (the client's GCI record) -> APPLIED rev 1 -> PUSH_FULL;
      the push == record_from_gci(bound resident) (digest ok), host_session/epoch consistent; each client gets only its own
      resident's record (and nobody gets another's push when a peer joins)
  T2  malformed chunk matrix (chunk before BEGIN, idx >= count, offset, len, foreign xfer, duplicate, wrong message size,
      non-increasing BEGIN xfer, host-only BEGIN kind): dropped, counted, 3 violations close the peer, nothing applied
  T3  BAD_SHAPE (total_size, chunk_count; its chunks are absorbed without violations), BAD_DIGEST
  T4  STALE_BASE: old rev, wrong epoch, replay of an old accepted upload (new xfer id, old base); the client then gets a push
  T5  INVALID_FIELD matrix (wallet, bank, loan, illegal pocket ids, cond bits 30/31, equipment, org table, player_ID, exists,
      catalog order item / shop_level, lotto month / storage); legal edge values (incl. a NEW catalog order and lotto tickets,
      which are SHARED = client-writable) accepted and merged; invalid uploads never change rev; the ONLY host-owned range,
      reset_code, is silently kept
      (and the client-owned bytes of that same upload ARE merged)
  T6  RATE_LIMITED (3 uploads within 1 s) and a flood of 5 in a row closes the peer
  T7  HANDSHAKE and parked peers' record messages are ignored; a duplicate HELLO is ignored; a host-only ACK status is a violation
  T8  HELLO enforcement (compile-time constant, ON since the real client half exists; the old env knob is gone): no HELLO ->
      closed after ~5 s; a default FakeClient is unaffected.
      A v7 FakeClient gets PROTOCOL_MISMATCH reporting v8.
  T9  the host's own resident (0) is never written / pushed (log + no push carries its PersonalID)
  M   migration: a second connection gets the HOST record (not the client's local GCI); a MIGRATE_UPLOAD when rev > 0 is refused
      STALE_BASE; a same-session reconnect is a continuation (APPLIED, no push); a different session -> PUSH_FULL; a mid-upload
      disconnect leaves the record unchanged and the next connection's upload merges only its own bytes

NOT covered here (documented): PUSH_HOSTFIELDS (needs a host-side change of catalog_orders/lotto/reset_code that no
protocol-level or hook-free test can cause), NOT_BOUND for a READY unbound peer (unreachable: every READY peer is bound),
persistence (D3-4). Tier: PROTOCOL TESTED (real host binary, scripted clients).
Usage: python test_d3_record_protocol.py [--port 9700]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time

import net_spike_lib as L

ACK = L
SAVE_DIR_REL = "save"


def log_since(host, off):
    return host.log_text()[off:]


def rec_lines(host, off=0):
    return [ln for ln in log_since(host, off).splitlines() if "[NET][REC]" in ln]


def wait_closed(c, timeout=3.0):
    return c.wait_disconnected(timeout) and c.disconnected_by_host


class Env:
    def __init__(self, ip, port, host, results):
        self.ip, self.port, self.host, self.results = ip, port, host, results
        self.check = lambda d, c: L.check(d, c, results)
        self.clients = []

    def client(self, label, slot, **kw):
        c = L.FakeClient(label, self.ip, self.port, player=L.resident_player(slot), **kw)
        c.rec_resident_idx = slot
        self.clients.append(c)
        return c

    def ready(self, label, slot, **kw):
        c = self.client(label, slot, **kw)
        c.connect_and_ready(quiet=True)
        return c

    def release(self, c):
        """Graceful leave + give the host time to tear the binding down."""
        try:
            if c.state in (c.STATE_CONNECTED, c.STATE_PENDING):
                c.disconnect()
        except Exception:  # noqa: BLE001
            pass
        c.close()
        L.pump_sleep(0.6)

    def wait_log(self, rx, off, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            m = re.search(rx, log_since(self.host, off))
            if m:
                return m
            L.pump_sleep(0.1)
        return None


def edit_record(rec, **kw):
    """Convenience record edits on a BE image."""
    r = rec
    if "wallet" in kw:
        r = L.record_set_u32(r, L.REC_OFF_WALLET, kw["wallet"])
    if "bank" in kw:
        r = L.record_set_u32(r, L.REC_OFF_BANK, kw["bank"])
    if "loan" in kw:
        r = L.record_set_u32(r, L.REC_OFF_LOAN, kw["loan"])
    if "pocket" in kw:
        slot, item = kw["pocket"]
        r = L.record_set_u16(r, L.REC_OFF_POCKETS + 2 * slot, item)
    if "cond" in kw:
        r = L.record_set_u32(r, L.REC_OFF_ITEM_COND, kw["cond"])
    if "equipment" in kw:
        r = L.record_set_u16(r, L.REC_OFF_EQUIPMENT, kw["equipment"])
    if "org" in kw:
        r = L.record_set_bytes(r, L.REC_OFF_ORG_TABLE, bytes(kw["org"]))
    return r


def run_main(args, results, ip, snap_gci):
    port = args.port
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check = lambda d, c: L.check(d, c, results)
    check(f"fixture has >= 3 non-host residents (slots {residents})", len(residents) >= 3)
    if len(residents) < 3:
        return
    r1, r2, r3 = residents[:3]
    gci = lambda i: L.record_from_gci(snap_gci, i)
    log_dir = os.path.dirname(os.path.abspath(__file__))
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot)],
                         log_path=os.path.join(log_dir, "d3_record_protocol_host.log")).start()
    env = Env(ip, port, host, results)
    try:
        if not host.wait_listening(60.0) or not host.boot_to_field(timeout=90.0, slot=host_slot):
            check("host reached genuine field-ready state", False)
            return
        check("host reached genuine field-ready state", True)
        L.resolve_host_town(ip, port)

        # ---------------- T1 + M: first join -> migration -> push ----------------
        a = env.client("A", r1)
        a.connect_and_ready(quiet=True)
        acks = [g for _c, g in a.rec_acks]
        check("T1/M ACK sequence on the first join: MIGRATE_REQUEST (rev 0, xfer 0) then APPLIED for the MIGRATE upload (rev 1)",
              len(acks) >= 2 and acks[0].status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST and acks[0].xfer_id == 0
              and acks[0].rev == 0 and acks[1].status == L.PC_NETGAME_REC_ACK_APPLIED and acks[1].xfer_id == 1
              and acks[1].rev == 1 and acks[1].epoch == acks[0].epoch)
        push = a.rec_pushes[0] if a.rec_pushes else None
        check("T1 PUSH_FULL after the migration: kind 1, rev 1, same epoch, digest ok, 0x2440 bytes",
              push is not None and push["kind"] == L.PC_NETGAME_REC_KIND_PUSH_FULL and push["rev"] == 1
              and push["epoch"] == acks[0].epoch and push["digest_ok"] and len(push["data"]) == L.PC_NETGAME_REC_SIZE
              and push["session"] != 0)
        check("T1 the push equals record_from_gci(bound resident) byte for byte",
              push is not None and push["data"] == gci(r1))
        check("T1 push carries the bound resident's own PersonalID and not the host's",
              push is not None and push["data"][:20] == gci(r1)[:20] and push["data"][:20] != gci(host_slot)[:20])
        check("T1/M host log: MIGRATE_REQUEST sent, MIGRATE xfer 1 APPLIED rev 1, push queued with the BE digest",
              "never synced (rev 0): MIGRATE_REQUEST sent" in host.log_text() and re.search(r"MIGRATE xfer 1 APPLIED \(resident %d epoch \d+ rev 1\)" % r1, host.log_text())
              and re.search(r"push FULL queued \(resident %d epoch \d+ rev 1 xfer 1 digest 0x%08X\)" % (r1, L.fnv1a32(gci(r1))), host.log_text()))
        sess, epoch = push["session"], push["epoch"]
        host_view = {r1: gci(r1)}  # the record the host holds for each resident

        b = env.ready("B", r2)
        c = env.ready("C", r3)
        check("T1 B and C each got exactly their own resident's record (equal to its GCI record, PersonalID differs from A)",
              b.rec_pushes and c.rec_pushes and b.rec_pushes[-1]["data"] == gci(r2) and c.rec_pushes[-1]["data"] == gci(r3)
              and b.rec_pushes[-1]["data"][:20] != a.rec_pushes[-1]["data"][:20]
              and b.rec_pushes[-1]["session"] == sess and c.rec_pushes[-1]["session"] == sess)
        check("T1 joining B/C did not cause any push to A (one push, only its own resident)", len(a.rec_pushes) == 1)
        check("T1 B/C epochs are per resident slot (random) and revs are 1", b.rec_pushes[-1]["rev"] == 1 and c.rec_pushes[-1]["rev"] == 1)
        host_view[r2], host_view[r3] = gci(r2), gci(r3)
        env.release(b)
        env.release(c)

        # ---- M: a second connection of A gets the HOST record, not the client's local one ----
        up_wallet = edit_record(host_view[r1], wallet=12345, pocket=(0, 0x2005))
        L.pump_sleep(1.6)
        a.upload_record(up_wallet, base=(epoch, 1))
        ack = a.wait_record_ack(xfer_id=2)
        check("M upload on base (epoch, rev 1) -> APPLIED rev 2", ack is not None and ack.status == 0 and ack.rev == 2)
        host_view[r1] = L.record_merge_expected(host_view[r1], up_wallet)
        env.release(a)
        a2 = env.client("A2", r1)  # fresh client object: no last-synced state, its local GCI record is the OLD one
        a2.connect_and_ready(quiet=True)
        acks2 = [g.status for _c, g in a2.rec_acks]
        check("M second connect: NO MIGRATE_REQUEST (rev > 0), host record pushed (wallet 12345 / pocket 0x2005, not the GCI's)",
              L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST not in acks2 and a2.rec_pushes and a2.rec_pushes[-1]["data"] == host_view[r1]
              and L.record_get_u32(a2.rec_pushes[-1]["data"], L.REC_OFF_WALLET) == 12345
              and a2.rec_pushes[-1]["data"] != gci(r1) and a2.rec_pushes[-1]["rev"] == 2)
        a2.rec_resident_idx = r1
        # M: MIGRATE_UPLOAD when rev > 0 is refused STALE_BASE
        before = len(rec_lines(host))
        x = a2.upload_record(gci(r1), base=(epoch, 0), kind=L.PC_NETGAME_REC_KIND_MIGRATE_UPLOAD)
        ack = a2.wait_record_ack(xfer_id=x)
        check("M MIGRATE_UPLOAD with rev > 0 is refused STALE_BASE (reports the current epoch/rev 2), nothing imported",
              ack is not None and ack.status == L.PC_NETGAME_REC_ACK_STALE_BASE and ack.rev == 2 and ack.epoch == epoch)
        L.pump_sleep(2.3)  # > the 2 s pacing of stale-triggered pushes

        # ---------------- T4 STALE_BASE ----------------
        n0 = len(a2.rec_pushes)
        x = a2.upload_record(edit_record(host_view[r1], wallet=1), base=(epoch, 1))
        ack = a2.wait_record_ack(xfer_id=x)
        check("T4 base rev older than the host's -> STALE_BASE carrying the current (epoch, rev 2)",
              ack is not None and ack.status == L.PC_NETGAME_REC_ACK_STALE_BASE and ack.rev == 2 and ack.epoch == epoch)
        push = a2.wait_record_push(after=n0, timeout=4.0)
        check("T4 after STALE_BASE the host re-syncs the client with a PUSH_FULL of its record (digest ok)",
              push is not None and push["kind"] == 1 and push["digest_ok"] and push["data"] == host_view[r1])
        x = a2.upload_record(edit_record(host_view[r1], wallet=2), base=(epoch ^ 0x55, 2))
        ack = a2.wait_record_ack(xfer_id=x)
        check("T4 wrong epoch -> STALE_BASE", ack is not None and ack.status == L.PC_NETGAME_REC_ACK_STALE_BASE)
        L.pump_sleep(2.2)
        x1 = a2.upload_record(edit_record(host_view[r1], wallet=500), base=(epoch, 2))
        ack = a2.wait_record_ack(xfer_id=x1)
        check("T4 valid upload on the current base -> APPLIED rev 3", ack is not None and ack.status == 0 and ack.rev == 3)
        first_data = edit_record(host_view[r1], wallet=500)
        host_view[r1] = L.record_merge_expected(host_view[r1], first_data)
        L.pump_sleep(1.7)
        x2 = a2.upload_record(first_data, base=(epoch, 2))  # replay of the accepted upload: same bytes, OLD base, new xfer id
        ack = a2.wait_record_ack(xfer_id=x2)
        check("T4 replay of an old accepted upload (old base, new xfer id) -> STALE_BASE (rev now 3)",
              ack is not None and ack.status == L.PC_NETGAME_REC_ACK_STALE_BASE and ack.rev == 3)
        check("T4 stale/replayed uploads were not applied (host log has exactly the 2 APPLIED uploads of this resident so far)",
              len(re.findall(r"upload xfer \d+ APPLIED \(resident %d" % r1, host.log_text())) == 2)
        L.pump_sleep(2.2)

        # ---------------- T5 INVALID_FIELD matrix ----------------
        cur = a2.rec_local
        base = (epoch, 3)
        pid_flip = bytearray(cur)
        pid_flip[3] ^= 0x01
        exists0 = bytearray(cur)
        exists0[L.REC_OFF_EXISTS] = 0
        org_bad = list(cur[L.REC_OFF_ORG_TABLE:L.REC_OFF_ORG_TABLE + 8])
        org_bad[0] = org_bad[1]
        cases = [
            ("wallet 100000", edit_record(cur, wallet=100000), L.PC_NETGAME_REC_FIELD_WALLET, 0),
            ("bank 1000000000", edit_record(cur, bank=1000000000), L.PC_NETGAME_REC_FIELD_BANK, 0),
            ("loan 798001", edit_record(cur, loan=798001), L.PC_NETGAME_REC_FIELD_LOAN, 0),
            ("pocket 2 = 0xFFFF (reserved)", edit_record(cur, pocket=(2, 0xFFFF)), L.PC_NETGAME_REC_FIELD_POCKET, 2),
            ("pocket 4 = 0x4000 (warp type)", edit_record(cur, pocket=(4, 0x4000)), L.PC_NETGAME_REC_FIELD_POCKET, 4),
            ("pocket 7 = ITEM1 money idx 200 (outside the table)", edit_record(cur, pocket=(7, 0x21C8)), L.PC_NETGAME_REC_FIELD_POCKET, 7),
            ("pocket 14 = FTR1 beyond FTR1_END", edit_record(cur, pocket=(14, 0x3FFF)), L.PC_NETGAME_REC_FIELD_POCKET, 14),
            ("item conds bit 30", edit_record(cur, cond=0x40000000), L.PC_NETGAME_REC_FIELD_ITEM_COND, 0),
            ("item conds bit 31", edit_record(cur, cond=0x80000000), L.PC_NETGAME_REC_FIELD_ITEM_COND, 0),
            ("equipment 0xFFFF", edit_record(cur, equipment=0xFFFF), L.PC_NETGAME_REC_FIELD_EQUIPMENT, 0),
            ("my_org_no_table not a permutation", edit_record(cur, org=org_bad), L.PC_NETGAME_REC_FIELD_ORG_TABLE, 0),
            ("player_ID changed", bytes(pid_flip), L.PC_NETGAME_REC_FIELD_PLAYER_ID, 0),
            ("exists = 0", bytes(exists0), L.PC_NETGAME_REC_FIELD_EXISTS, 0),
            ("catalog order 2 item 0xFFFF", L.record_set_bytes(cur, L.REC_OFF_CATALOG_ITEM0 + 8, b"\xFF\xFF\x01\x00"),
             L.PC_NETGAME_REC_FIELD_CATALOG, 2),
            ("catalog order 0 item 0x21C8 (ITEM1 money idx outside the table)",
             L.record_set_bytes(cur, L.REC_OFF_CATALOG_ITEM0, b"\x21\xC8\x01\x00"), L.PC_NETGAME_REC_FIELD_CATALOG, 0),
            ("catalog order 4 legal item but shop_level 7", L.record_set_bytes(cur, L.REC_OFF_CATALOG_ITEM0 + 16, b"\x10\x04\x07\x00"),
             L.PC_NETGAME_REC_FIELD_CATALOG, 4),
            ("lotto month 13", L.record_set_bytes(cur, L.REC_OFF_LOTTO, b"\x0D\x00"), L.PC_NETGAME_REC_FIELD_LOTTO, 0),
            ("lotto 5 tickets with month 0", L.record_set_bytes(cur, L.REC_OFF_LOTTO, b"\x00\x05"), L.PC_NETGAME_REC_FIELD_LOTTO, 0),
        ]
        for name, data, field, slot in cases:
            x = a2.upload_record(data, base=base)
            ack = a2.wait_record_ack(xfer_id=x)
            check(f"T5 {name} -> INVALID_FIELD detail field {field} slot {slot} (rev unchanged {base[1]})",
                  ack is not None and ack.status == L.PC_NETGAME_REC_ACK_INVALID_FIELD
                  and (ack.detail & 0xFF) == field and (ack.detail >> 8) == slot and ack.rev == base[1])
        L.pump_sleep(0.3)
        # legal edge values + host-owned bytes changed in the same upload
        edge = edit_record(cur, wallet=99999, bank=999999999, loan=798000, pocket=(1, 0x2000), cond=0x3FFFFFFF)
        edge = L.record_set_u32(edge, L.REC_OFF_RESET_CODE, 0xDEADBEEF)
        edge = L.record_set_bytes(edge, L.REC_OFF_LOTTO, b"\x07\xFF")  # month 7, 255 tickets (aNSC_MAX_TICKETS)
        edge = L.record_set_bytes(edge, L.REC_OFF_CATALOG_ORDERS,
                                  b"\x10\x04\x03\x00" + b"\x20\x05\x01\x00" + b"\x30\x08\x02\x00" + b"\x00\x00\x00\x00" * 2)
        x = a2.upload_record(edge, base=base)
        ack = a2.wait_record_ack(xfer_id=x)
        check("T5 legal edge values (wallet 99999, bank 999999999, loan 798000, cond 0x3FFFFFFF, a NEW catalog order set, lotto month 7 / 255 tickets) + a changed reset_code -> APPLIED rev 4",
              ack is not None and ack.status == 0 and ack.rev == 4)
        expected = L.record_merge_expected(host_view[r1], edge)
        host_view[r1] = expected
        check("T5 (model) the expected host record keeps ONLY reset_code (lotto/catalog_orders are SHARED = merged)",
              expected[L.REC_OFF_RESET_CODE:L.REC_OFF_RESET_CODE + 4] == cur[L.REC_OFF_RESET_CODE:L.REC_OFF_RESET_CODE + 4]
              and expected[L.REC_OFF_CATALOG_ORDERS:L.REC_OFF_CATALOG_ORDERS + 20] == edge[L.REC_OFF_CATALOG_ORDERS:L.REC_OFF_CATALOG_ORDERS + 20]
              and expected[L.REC_OFF_LOTTO:L.REC_OFF_LOTTO + 2] == b"\x07\xFF")
        env.release(a2)
        a3 = env.ready("A3", r1)
        pushed = a3.rec_pushes[-1]["data"] if a3.rec_pushes else b""
        check("T5 next push == merge(host record, upload): client-owned bytes (wallet/bank/loan/pocket/cond) applied",
              pushed == expected and L.record_get_u32(pushed, L.REC_OFF_WALLET) == 99999
              and L.record_get_u32(pushed, L.REC_OFF_BANK) == 999999999 and L.record_get_u32(pushed, L.REC_OFF_LOAN) == 798000)
        check("T5 the host-owned reset_code was SILENTLY kept (push shows the old host value), player_ID/exists untouched",
              pushed[L.REC_OFF_RESET_CODE:L.REC_OFF_RESET_CODE + 4] == cur[L.REC_OFF_RESET_CODE:L.REC_OFF_RESET_CODE + 4]
              and pushed[L.REC_OFF_RESET_CODE:L.REC_OFF_RESET_CODE + 4] != b"\xDE\xAD\xBE\xEF"
              and pushed[:20] == cur[:20] and pushed[L.REC_OFF_EXISTS] == cur[L.REC_OFF_EXISTS])
        check("T5 the SHARED ranges were MERGED: the NEW catalog orders and the lotto tickets are on the host (a purchase is not lost)",
              pushed[L.REC_OFF_CATALOG_ORDERS:L.REC_OFF_CATALOG_ORDERS + 20] == edge[L.REC_OFF_CATALOG_ORDERS:L.REC_OFF_CATALOG_ORDERS + 20]
              and pushed[L.REC_OFF_LOTTO:L.REC_OFF_LOTTO + 2] == b"\x07\xFF")

        # ---------------- M: same-session continuation / different session / mid-upload disconnect ----------------
        a3.rec_resident_idx = r1
        last = a3.rec_last
        env.release(a3)
        a4 = env.client("A4", r1)
        a4.rec_last = last  # same host process, same (epoch, rev): continuation
        a4.connect_and_ready(quiet=True)
        st = [(g.status, g.xfer_id) for _c, g in a4.rec_acks]
        check("M same host session & (epoch, rev) -> ACK APPLIED (xfer 0), NO push", (0, 0) in st and not a4.rec_pushes
              and "continuation accepted" in host.log_text())
        # mid-upload disconnect: BEGIN + 5 chunks of a junk record, then leave
        junk = edit_record(a3.rec_local, wallet=99998, bank=77)
        L.pump_sleep(0.2)
        x = a4.next_record_xfer_id()
        a4.send_record_begin(L.PC_NETGAME_REC_KIND_UPLOAD, x, last[1], last[2], digest=L.fnv1a32(junk))
        for i in range(5):
            a4.send_record_chunk(i, x, junk[i * 1000:(i + 1) * 1000])
        L.pump_sleep(0.3)
        env.release(a4)
        a5 = env.client("A5", r1)
        a5.rec_last = (last[0], last[1], last[2] ^ 1)  # lineage mismatch -> host wins with a push
        a5.connect_and_ready(quiet=True)
        check("M mid-upload disconnect left the record unchanged (next push == host record), lineage mismatch -> PUSH_FULL",
              a5.rec_pushes and a5.rec_pushes[-1]["data"] == host_view[r1] and a5.rec_pushes[-1]["rev"] == 4)
        a5.rec_resident_idx = r1
        L.pump_sleep(1.7)
        good = edit_record(a5.rec_local, wallet=4242)
        x = a5.upload_record(good, base=(epoch, 4))
        ack = a5.wait_record_ack(xfer_id=x)
        check("M after the abandoned partial upload, a fresh connection's upload merges only its own bytes (APPLIED rev 5)",
              ack is not None and ack.status == 0 and ack.rev == 5)
        host_view[r1] = L.record_merge_expected(host_view[r1], good)

        # ---------------- T3 BAD_SHAPE / BAD_DIGEST ----------------
        off = len(host.log_text())
        L.pump_sleep(1.7)
        x = a5.upload_record(good + b"\x00", base=(epoch, 5))  # total_size 0x2441, 10 chunks
        ack = a5.wait_record_ack(xfer_id=x)
        check("T3 total_size != 0x2440 -> BAD_SHAPE (detail 1)", ack is not None and ack.status == L.PC_NETGAME_REC_ACK_BAD_SHAPE and ack.detail == 1)
        x = a5.upload_record(good[:9000], base=(epoch, 5))  # 9 chunks
        ack = a5.wait_record_ack(xfer_id=x)
        check("T3 chunk_count 9 (and total 9000) -> BAD_SHAPE", ack is not None and ack.status == L.PC_NETGAME_REC_ACK_BAD_SHAPE)
        L.pump_sleep(0.5)
        check("T3 the chunks of a BAD_SHAPE-refused transfer were absorbed: NO violation logged, peer alive",
              "violation" not in log_since(host, off) and a5.is_connected())
        off_rsv = len(host.log_text())
        x = a5.next_record_xfer_id()
        a5.send_record_begin(L.PC_NETGAME_REC_KIND_UPLOAD, x, epoch, 5, digest=L.fnv1a32(good), rsv=1)
        for i in range(10):
            a5.send_record_chunk(i, x, good[i * 1000:(i + 1) * 1000])
        ack = a5.wait_record_ack(xfer_id=x)
        check("T3 BEGIN.rsv != 0 (record_class seam) -> BAD_SHAPE detail 5, its chunks absorbed, NOT a violation, peer alive",
              ack is not None and ack.status == L.PC_NETGAME_REC_ACK_BAD_SHAPE and ack.detail == 5
              and (L.pump_sleep(0.5) or True) and "violation" not in log_since(host, off_rsv) and a5.is_connected())
        L.pump_sleep(1.0)
        x = a5.upload_record(good, base=(epoch, 5), digest=L.fnv1a32(good) ^ 1)
        ack = a5.wait_record_ack(xfer_id=x)
        check("T3 wrong digest -> BAD_DIGEST", ack is not None and ack.status == L.PC_NETGAME_REC_ACK_BAD_DIGEST)
        check("T3 nothing was applied (still rev 5)", len(re.findall(r"upload xfer \d+ APPLIED \(resident %d" % r1, host.log_text())) == 4)
        env.release(a5)

        # ---------------- T2 malformed chunk matrix ----------------
        def violation_client(label, slot):
            cl = env.ready(label, slot)
            cl.rec_resident_idx = slot
            return cl

        def good_begin(cl, xfer=None):
            x = cl.next_record_xfer_id() if xfer is None else xfer
            cl.send_record_begin(L.PC_NETGAME_REC_KIND_UPLOAD, x, cl.rec_last[1], cl.rec_last[2], digest=L.fnv1a32(host_view[r1]))
            return x

        def expect_violation(cl, off0, why, n):
            m = env.wait_log(r"peer \d+ violation %d/3: %s" % (n, re.escape(why)), off0, 3.0)
            check(f"T2 violation {n}/3 logged: {why}", m is not None)

        t2_sets = [
            [("CHUNK before BEGIN", lambda cl: cl.send_record_chunk(0, 77, host_view[r1][:1000])),
             ("CHUNK idx >= count", lambda cl: (good_begin(cl), cl.send_record_chunk(10, cl.rec_xfer_counter, b"x" * 1000, length=1000, offset=10000))),
             ("CHUNK offset != idx*1000", lambda cl: (good_begin(cl), cl.send_record_chunk(2, cl.rec_xfer_counter, b"x" * 1000, offset=2001)))],
            [("CHUNK len wrong", lambda cl: (good_begin(cl), cl.send_record_chunk(3, cl.rec_xfer_counter, b"x" * 1000, length=999))),
             ("CHUNK for a foreign xfer_id", lambda cl: (good_begin(cl), cl.send_record_chunk(0, cl.rec_xfer_counter + 50, b"x" * 1000))),
             ("duplicate CHUNK", lambda cl: (good_begin(cl), cl.send_record_chunk(1, cl.rec_xfer_counter, host_view[r1][1000:2000]),
                                              cl.send_record_chunk(1, cl.rec_xfer_counter, host_view[r1][1000:2000])))],
            [("CHUNK wrong size", lambda cl: cl.send_reliable(struct.pack("<BBHIHH", 49, 0, 1000, 5, 0, 0) + b"\x00" * 999)),
             ("BEGIN xfer_id not strictly increasing", lambda cl: (good_begin(cl, 9), good_begin(cl, 9))),
             ("BEGIN with a host-only or unknown kind", lambda cl: cl.send_record_begin(L.PC_NETGAME_REC_KIND_PUSH_FULL, 40, 1, 1))],
        ]
        slots_rot = [r1, r2, r3]
        for si, sset in enumerate(t2_sets):
            cl = violation_client(f"V{si}", slots_rot[si])
            off0 = len(host.log_text())
            applied_before = len(re.findall(r"APPLIED", host.log_text()))
            for n, (why, fn) in enumerate(sset, 1):
                fn(cl)
                expect_violation(cl, off0, why, n)
                if n < 3:
                    check(f"T2 peer still connected after violation {n}/3 ({why})", cl.is_connected() and not cl.inbox.peek_all(L.p_event("DISCONNECT")))
            check(f"T2 the third violation closes the peer (set {si})", wait_closed(cl, 3.0))
            check(f"T2 set {si}: no malformed transfer was applied", len(re.findall(r"APPLIED", host.log_text())) == applied_before)
            check(f"T2 set {si}: host log shows the close", re.search(r"peer \d+ closed after 3 record protocol violations", log_since(host, off0)) is not None)
            env.release(cl)

        # ---------------- T6 rate limit ----------------
        L.pump_sleep(0.5)
        cl = env.ready("R", r2)
        cl.rec_resident_idx = r2
        view = cl.rec_local
        epoch2 = cl.rec_last[1]
        rv = cl.rec_last[2]
        x = cl.upload_record(edit_record(view, wallet=10), base=(epoch2, rv))
        ack1 = cl.wait_record_ack(xfer_id=x)
        check("T6 first upload APPLIED", ack1 is not None and ack1.status == 0)
        x = cl.upload_record(edit_record(view, wallet=11), base=(epoch2, rv + 1))
        ack2 = cl.wait_record_ack(xfer_id=x)
        x = cl.upload_record(edit_record(view, wallet=12), base=(epoch2, rv + 1))
        ack3 = cl.wait_record_ack(xfer_id=x)
        check("T6 the 2nd and 3rd upload within 1 s are RATE_LIMITED", ack2 is not None and ack3 is not None
              and ack2.status == ack3.status == L.PC_NETGAME_REC_ACK_RATE_LIMITED)
        L.pump_sleep(1.7)
        x = cl.upload_record(edit_record(view, wallet=13), base=(epoch2, rv + 1))
        ack4 = cl.wait_record_ack(xfer_id=x)
        check("T6 after 1.5 s the next upload is APPLIED again (rate-limit run reset)", ack4 is not None and ack4.status == 0 and ack4.rev == rv + 2)
        statuses = []
        for i in range(5):
            x = cl.upload_record(edit_record(view, wallet=20 + i), base=(epoch2, rv + 2))
            ak = cl.wait_record_ack(xfer_id=x, timeout=2.0)
            statuses.append(None if ak is None else ak.status)
        check(f"T6 a flood of 5 uploads in a row is RATE_LIMITED each ({statuses})", all(s == L.PC_NETGAME_REC_ACK_RATE_LIMITED for s in statuses))
        check("T6 the 5th consecutive RATE_LIMITED closes the peer", wait_closed(cl, 3.0)
              and "closed: 5 consecutive RATE_LIMITED uploads" in host.log_text())
        env.release(cl)

        # ---------------- T7 HANDSHAKE / parked / duplicate HELLO / host-only ACK ----------------
        L.pump_sleep(0.5)
        hs = L.FakeClient("HS", ip, port, context_flags=None, wait_snapshot=False, record_hello=False)
        hs.connect(timeout=3.0)
        off = len(host.log_text())
        hs.send_record_hello()
        hs.upload_record(gci(r1), base=(1, 1))
        hs.send_record_ack(L.PC_NETGAME_REC_ACK_ADOPT_FAILED, 1)
        L.pump_sleep(1.5)
        check("T7 a HANDSHAKE peer (no IDENTITY) gets no reply to HELLO/BEGIN/CHUNK/ACK and the host logs nothing about it",
              not hs.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (47, 48, 49, 50))
              and not rec_lines(host, off) and hs.is_connected())
        hs.close()
        holder = env.ready("H", r3)
        park = L.FakeClient("PK", ip, port, player=L.resident_player(r3), context_flags=None, wait_snapshot=False, record_hello=False)
        park.town_claimed = L.resolve_host_town(ip, port)
        park.connect(timeout=3.0)
        park.send_identity(town=park.town_claimed, player=L.resident_player(r3))
        L.pump_sleep(0.5)
        off = len(host.log_text())
        hp = len(holder.rec_acks)
        park.send_record_hello()
        park.upload_record(gci(r3), base=(1, 1))
        L.pump_sleep(1.5)
        check("T7 a PARKED peer (claim on a live resident) gets no record reply and the live holder's record is untouched",
              not park.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (47, 48, 49, 50, L.PC_NETGAME_MSG_IDENTITY_ACK))
              and not rec_lines(host, off) and len(holder.rec_acks) == hp)
        park.close()
        # duplicate HELLO ignored; host-only status in an ACK is a violation
        off = len(host.log_text())
        n0 = len(holder.rec_acks)
        holder.send_record_hello()
        L.pump_sleep(0.8)
        check("T7 a duplicate HELLO on a connection is ignored (no ACK, no push, logged)",
              len(holder.rec_acks) == n0 and "duplicate/out-of-state HELLO ignored" in log_since(host, off))
        holder.send_record_ack(L.PC_NETGAME_REC_ACK_STALE_BASE, 1)
        check("T7 a client RECORD_ACK with a host-only status is a counted violation",
              env.wait_log(r"violation 1/3: client sent a host-only RECORD_ACK status", off, 3.0) is not None)
        env.release(holder)

        # ---------------- T11 upload timeout: late in-flight chunks are absorbed ----------------
        L.pump_sleep(0.5)
        to = env.ready("TO", r1)
        off = len(host.log_text())
        tgood = edit_record(to.rec_local, wallet=31337)
        x = to.next_record_xfer_id()
        to.send_record_begin(L.PC_NETGAME_REC_KIND_UPLOAD, x, to.rec_last[1], to.rec_last[2], digest=L.fnv1a32(tgood))
        for i in range(5):
            to.send_record_chunk(i, x, tgood[i * 1000:(i + 1) * 1000])
        m = env.wait_log(r"open upload xfer %d timed out -- discarded" % x, off, 8.0)
        check("T11 an incomplete upload is discarded by the host after ~5 s (log line)", m is not None)
        for i in range(5, 10):  # the late chunks of the timed-out transfer
            to.send_record_chunk(i, x, tgood[i * 1000:(i + 1) * 1000])
        L.pump_sleep(1.0)
        check("T11 late chunks of the timed-out transfer are ABSORBED: no violation logged, peer NOT closed",
              "violation" not in log_since(host, off) and to.is_connected() and not to.inbox.peek_all(L.p_event("DISCONNECT")))
        x2 = to.upload_record(tgood, base=(to.rec_last[1], to.rec_last[2]))
        ack = to.wait_record_ack(xfer_id=x2)
        check("T11 a fresh, complete upload on the same connection is still APPLIED afterwards", ack is not None and ack.status == 0)
        env.release(to)

        # ---------------- T8c v7 FakeClient ----------------
        v7 = L.FakeClient("V7", ip, port, player=L.resident_player(r2), context_flags=None, wait_snapshot=False)
        rej = None
        try:
            v7.connect_and_ready(protocol_version=7, quiet=True)
        except L.HandshakeRejected as e:
            rej = e.reject
        check("T8 a v7 FakeClient gets PROTOCOL_MISMATCH reporting v8",
              rej is not None and rej.reason == L.PC_NETGAME_REJECT_PROTOCOL_MISMATCH and rej.expected_protocol_version == 8)
        v7.close()

        # ---------------- T9 host's own resident ----------------
        text = host.log_text()
        rec_resident_refs = set(int(m) for m in re.findall(r"\[NET\]\[REC\][^\n]*resident (\d+)", text))
        check(f"T9 the host's own resident ({host_slot}) never appears in any record log line (residents touched: {sorted(rec_resident_refs)})",
              host_slot not in rec_resident_refs and rec_resident_refs <= {r1, r2, r3})
        all_pushes = [p for cl in env.clients for p in cl.rec_pushes]
        check("T9 no push ever carried the host resident's PersonalID", all(p["data"][:20] != gci(host_slot)[:20] for p in all_pushes) and len(all_pushes) >= 6)
        check("T9 the host process stayed alive through the whole run", host.alive())
    finally:
        for cl in env.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        host.stop()


def run_enforce(args, results, ip):
    port = args.port + 1
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check = lambda d, c: L.check(d, c, results)
    log_dir = os.path.dirname(os.path.abspath(__file__))
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", str(host_slot)],
                         log_path=os.path.join(log_dir, "d3_record_enforce_host.log")).start()
    clients = []
    try:
        if not host.wait_listening(60.0) or not host.boot_to_field(timeout=90.0, slot=host_slot):
            check("T8 enforcement host reached field-ready", False)
            return
        L.resolve_host_town(ip, port)
        nh = L.FakeClient("NOHELLO", ip, port, player=L.resident_player(residents[0]), record_hello=False)
        ok = good = None
        clients.append(nh)
        t_before = time.monotonic()
        nh.connect_and_ready(quiet=True)
        closed = wait_closed(nh, 12.0)
        d = nh.inbox.peek_all(L.p_event("DISCONNECT"))
        dt = (d[0].t - t_before) if d else -1.0
        check(f"T8 enforcement (always on): a READY peer that never sent HELLO is closed by the host ~5 s after READY (closed={closed}, "
              f"{dt:.1f}s after the connect started; READY precedes that by the handshake + snapshot time)",
              closed and 4.8 <= dt <= 12.0 and "sent no RECORD_HELLO within 5000 ms" in host.log_text())
        ok_c = L.FakeClient("HELLOOK", ip, port, player=L.resident_player(residents[1]))
        clients.append(ok_c)
        ok_c.connect_and_ready(quiet=True)
        L.pump_sleep(6.5)
        check("T8 enforcement (always on): a default FakeClient (HELLO flow) is unaffected and stays connected > 6 s",
              ok_c.is_connected() and ok_c.rec_synced and ok_c.rec_pushes and ok_c.rec_pushes[-1]["digest_ok"])
        # ---- D3 review fixes: HELLO reserved-zero rule and MIGRATE recovery after an epoch re-roll (FakeClient drives it) ----
        mg = L.FakeClient("MIG", ip, port, player=L.resident_player(residents[2]), record_hello=False, record_auto=False)
        clients.append(mg)
        mg.connect_and_ready(quiet=True)
        mg.rec_resident_idx = residents[2]
        mg.send_record_hello(flags=0x02)
        a1 = mg.wait_record_ack(timeout=3.0)
        mg.send_reliable(struct.pack(L.RECORD_HELLO_FMT, L.PC_NETGAME_MSG_RECORD_HELLO, 0, 1, L.PC_NETGAME_REC_SIZE, 0, 0, 0, 0))
        a2 = mg.wait_record_ack(timeout=3.0)
        mg.send_record_hello(record_size=L.PC_NETGAME_REC_SIZE + 1)
        a3 = mg.wait_record_ack(timeout=3.0)
        check("T10 HELLO with an undefined flag bit / non-zero _reserved0 -> BAD_SHAPE detail 6 (no violation); wrong record_size -> detail 4",
              a1 is not None and a2 is not None and a3 is not None and (a1.status, a1.detail) == (3, 6) and (a2.status, a2.detail) == (3, 6)
              and (a3.status, a3.detail) == (3, 4) and not re.search(r"violation \d/3", host.log_text()) and mg.is_connected())
        mg.send_record_hello()  # a clean HELLO is still accepted (the peer stayed AWAIT_HELLO)
        mr = mg.wait_record_ack(status=L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST, timeout=3.0)
        check("T10 a clean HELLO afterwards -> MIGRATE_REQUEST (rev 0)", mr is not None and mr.rev == 0)
        rec = mg.own_record()
        x = mg.upload_record(rec, base=(mr.epoch ^ 0x5A5A, 0), kind=L.PC_NETGAME_REC_KIND_MIGRATE_UPLOAD)
        st = mg.wait_record_ack(xfer_id=x, timeout=3.0)
        mr2 = mg.wait_record_ack(status=L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST, timeout=3.0)
        check("T10 MIGRATE_UPLOAD on a stale epoch (re-rolled by a save pause) -> STALE_BASE (current epoch, rev 0) THEN a repeated "
              "MIGRATE_REQUEST on the current epoch, nothing imported (NOT hook-tested for a real re-roll)",
              st is not None and st.status == L.PC_NETGAME_REC_ACK_STALE_BASE and st.rev == 0 and st.epoch == mr.epoch
              and mr2 is not None and mr2.epoch == mr.epoch and mr2.rev == 0 and "MIGRATE epoch stale" in host.log_text())
        x = mg.upload_record(rec, base=(mr2.epoch, 0), kind=L.PC_NETGAME_REC_KIND_MIGRATE_UPLOAD)
        ok1 = mg.wait_record_ack(xfer_id=x, timeout=3.0)
        check("T10 the re-issued MIGRATE upload on the current epoch is APPLIED once (rev 1) and followed by the push",
              ok1 is not None and ok1.status == 0 and ok1.rev == 1 and mg.wait_record_push(timeout=4.0) is not None)
        L.pump_sleep(1.7)
        x = mg.upload_record(rec, base=(mr2.epoch, 0), kind=L.PC_NETGAME_REC_KIND_MIGRATE_UPLOAD)
        again = mg.wait_record_ack(xfer_id=x, timeout=3.0)
        check("T10 migration is idempotent: a second MIGRATE_UPLOAD (rev now 1) is refused STALE_BASE",
              again is not None and again.status == L.PC_NETGAME_REC_ACK_STALE_BASE and again.rev == 1)
    finally:
        for cl in clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9700)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "d3_record_protocol_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        run_main(args, results, ip, snap_gci)
        restore()
        run_enforce(args, results, ip)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
