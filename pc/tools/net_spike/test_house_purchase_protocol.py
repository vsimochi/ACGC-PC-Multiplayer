#!/usr/bin/env python3
"""test_house_purchase_protocol.py - guest-first town, M1: the PAID HOUSE PURCHASE (TXN_COMMIT kind 14 HOUSE_PURCHASE), REAL DEDICATED HOSTS + scripted FakeClients + the stdin console.

TIER: scripted FakeClients against REAL `AnimalCrossing.exe --host <port> --dedicated` processes (stdin pipe) started from a DISPOSABLE fixture copy
pc\\build64\\bin_fixture4_hbuy (a fresh copy of the read-only bin_fixture4; NET_SPIKE_GAME_BIN points at it). The copy's town save is DERIVED here so that resident slot 3 / house 3 are FREE
(the same derivation as test_promotion_protocol.py). The live save dir, bin_talkfix*, bin_fixture4 and the other disposable copies are never used.

PHASES (real host processes on the SAME disposable save dir):
  P0  host (defaults): four guests join with FRESH records (BUYERA, BUYERB, POORGST, LATEGST), the host stops, their stored guests.dat records are PATCHED (wallet 25000 for A / B / D, 5000 for C)
  P1  host (`--resident-tokens tofu`): the guests reconnect (known tokens, the host pushes the stored record):
      P1 insufficient Bells (C, wallet 5000): TXN_RESULT REJECTED NO_FUNDS (19); guests.dat / members.dat / records.dat / the GCI file are byte-identical, the peer stays connected
      P7 (D, wallet 25000): a wrong price -> PRICE_MISMATCH (21), a stale base -> STALE_IMAGE (10), house 1 (owned) -> INVALID_HOUSE (29), house 7 (out of range) -> INVALID_HOUSE (29), a
         malformed message -> BAD_SHAPE (11); nothing changes
      P3 A and B send HOUSE_PURCHASE (auto) back to back for the LAST slot: exactly one APPLIED (post wallet = 25000 - 18400 = 6600), the other REJECTED NO_RESIDENCE (28) with its wallet intact
      P4 the winner receives TXN_RESULT -> RESIDENT_HANDOFF (66) -> REJECT 6 PROMOTED in that order; guests.dat has no entry of it, two members.dat entries (RESIDENT_TOKEN + PROMOTION_HANDOFF),
         records.dat slot 3 rev 1; the saved GCI: private_data[3] exists with the guest's name, loan 0, wallet 6600, arrangement 3 -> 3, house 3 owner = the new PID, residents 0..2 unchanged
      P2 the town is FULL: D's valid purchase -> NO_RESIDENCE, nothing changed; the console `promote LATEGST auto auto` still says the town is FULL (the console path runs the shared core)
      P5 the winner reconnects with its GUEST token: handoff (66 + REJECT 6) again; a RESIDENT-claim client with the new PID + the handed-over token is admitted (IDENTITY_TOKEN KNOWN)
      P6 that resident sends HOUSE_PURCHASE: REJECTED (a resident already owns a house)
Usage: python test_house_purchase_protocol.py [--port 12981]
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
import town_test_support as T  # noqa: E402
import make_four_resident_fixture as MF  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_hbuy")


def make_free_slot(dest, slot=3):
    """Derives the disposable town save: resident `slot` cleared (vanilla empty PersonalID, exists 0) and its house's owner cleared; checksum + backup copy recomputed like the game's writer."""
    p = os.path.join(dest, MF.GCI_REL)
    g = bytearray(open(p, "rb").read())
    main = bytes(g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE])
    assert MF.stored_checksum(main) == MF.checksum(main), "fixture checksum does not validate"
    clear = b"\x20" * 16 + struct.pack(">HH", 0xFFFF, 0xFFFF)
    po = MF.MAIN_OFF + MF.PRIV_OFF + slot * MF.PRIV_STRIDE
    g[po:po + MF.PRIV_STRIDE] = bytes(MF.PRIV_STRIDE)
    g[po:po + 20] = clear
    ho = MF.MAIN_OFF + MF.HOME_OFF + slot * MF.HOME_STRIDE
    g[ho:ho + 20] = clear
    m = bytearray(g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE])
    m[MF.CHK_OFF:MF.CHK_OFF + 2] = struct.pack(">H", MF.checksum(bytes(m)))
    g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE] = m
    g[MF.BACK_OFF:MF.BACK_OFF + MF.SAVE_SECTOR_SIZE] = g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_SECTOR_SIZE]
    open(p, "wb").write(bytes(g))


if __name__ == "__main__":
    T.make_fixture("bin_fixture4_hbuy")
    make_free_slot(os.path.join(T.BUILD64, "bin_fixture4_hbuy"))

import net_spike_lib as L  # noqa: E402

IP = "127.0.0.1"
PRICE = 18400
MBR_SIZE = 32 + 16 * 80 + 4
GST_ENTRY = 72 + 0x2440
GST_SIZE = 32 + 8 * GST_ENTRY + 4
REC_ENTRY = 4 + 20 + 16 + 0x2440
REC_SIZE = 32 + 4 * REC_ENTRY + 4
ARRANGEMENT_OFF = MF.MAIN_OFF + 0x2068A
MSG_TXN_RESULT = L.PC_NETGAME_MSG_TXN_RESULT


def mp_dir():
    return L.server_dir()  # a DEDICATED host's own tree (servers/default/), not save/mp


def mpath(n):
    return L.server_file(n)


def raw(path):
    with open(path, "rb") as f:
        return f.read()


def raw_or_none(path):
    return raw(path) if os.path.isfile(path) else None


def parse_members(path):
    b = raw(path)
    assert len(b) == MBR_SIZE and b[:8] == b"ACMPMBR\x00"
    assert struct.unpack_from("<I", b, len(b) - 4)[0] == zlib.crc32(b[:-4]) & 0xFFFFFFFF
    ents = []
    for i in range(16):
        o = 32 + i * 80
        if b[o] == 0:
            continue
        ents.append(dict(i=i, confirmed=b[o + 1], slot=b[o + 2], kind=b[o + 3], pid=b[o + 20:o + 40], token=b[o + 40:o + 56], aux=b[o + 60:o + 80]))
    return ents


def parse_guests(path):
    b = raw(path)
    assert len(b) == GST_SIZE and b[:8] == b"ACMPGST\x00"
    assert struct.unpack_from("<I", b, len(b) - 4)[0] == zlib.crc32(b[:-4]) & 0xFFFFFFFF
    out = []
    for i in range(8):
        e = b[32 + i * GST_ENTRY:32 + (i + 1) * GST_ENTRY]
        if e[0]:
            rec = e[72:72 + 0x2440]
            out.append(dict(slot=i, pid=bytes(e[4:24]), token=bytes(e[24:40]), rev=struct.unpack_from("<I", e, 44)[0],
                            wallet=struct.unpack_from(">I", rec, L.REC_OFF_WALLET)[0]))
    return out


def parse_records(path):
    b = raw(path)
    assert len(b) == REC_SIZE and b[:8] == b"ACMPREC\x00"
    assert struct.unpack_from("<I", b, len(b) - 4)[0] == zlib.crc32(b[:-4]) & 0xFFFFFFFF
    out = {}
    for i in range(4):
        o = 32 + i * REC_ENTRY
        if b[o]:
            out[i] = dict(pid=bytes(b[o + 4:o + 24]), epoch=struct.unpack_from("<I", b, o + 24)[0], rev=struct.unpack_from("<I", b, o + 28)[0])
    return out


def patch_guest_record(path, pid, patch):
    """Rewrites the stored record of the guests.dat entry with key `pid` through patch(bytearray) and recomputes the entry CRC32 + the file CRC32 (a test seed of a guest that played)."""
    b = bytearray(raw(path))
    for i in range(8):
        o = 32 + i * GST_ENTRY
        if b[o] and bytes(b[o + 4:o + 24]) == pid:
            rec = bytearray(b[o + 72:o + 72 + 0x2440])
            patch(rec)
            b[o + 72:o + 72 + 0x2440] = rec
            struct.pack_into("<I", b, o + 52, zlib.crc32(bytes(rec)) & 0xFFFFFFFF)
            struct.pack_into("<I", b, len(b) - 4, zlib.crc32(bytes(b[:-4])) & 0xFFFFFFFF)
            with open(path, "wb") as f:
                f.write(bytes(b))
            return True
    return False


def say(h, cmd, settle=1.2, timeout=8.0):
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
    return h.log_text()[off:]


class PClient(L.FakeClient):
    """FakeClient that records TXN_RESULT / RESIDENT_HANDOFF / REJECT in ARRIVAL order."""

    def __init__(self, *a, **kw):
        self.seen = []
        super().__init__(*a, **kw)

    def on_message(self, m):
        try:
            if m.channel == L.CH_RELIABLE and m.payload and m.payload[0] in (MSG_TXN_RESULT, L.PC_NETGAME_MSG_RESIDENT_HANDOFF, L.PC_NETGAME_MSG_REJECT):
                self.seen.append((m.payload[0], bytes(m.payload)))
        except Exception:  # noqa: BLE001
            pass
        return super().on_message(m)


class Rig:
    _n = [0]

    def __init__(self, results):
        self.results = results
        self.ck = lambda d, c: L.check(d, bool(c), results)
        self.clients = []
        self.port = None

    def start(self, tag, port, extra=()):
        h = L.HostProcess(port=port, extra_args=["--dedicated"] + list(extra), log_path=T.log_path("hbuy_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR,
                          stdin_pipe=True, verbose=False, new_group=True).start()
        orig = h.log_text
        h.log_text = lambda: orig().replace("\r\n", "\n")
        self.port = port
        ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
        self.ck("[%s] dedicated host booted (%s): observer + world + save ready" % (tag, " ".join(extra) or "defaults"), ok)
        if ok:
            L.resolve_host_town(IP, port)
        return h, ok

    def stop(self, h):
        h.send_line("stop")
        return h.wait_exit(60.0)

    def _bind(self):
        Rig._n[0] += 1
        return "127.0.%d.%d" % (60 + Rig._n[0] // 200, 2 + Rig._n[0] % 200)

    def guest(self, label, g, rec, token=None, record_wait=True):
        c = PClient(label, IP, self.port, guest=g, guest_token=token, guest_record_img=rec, bind_ip=self._bind(), record_wait=record_wait)
        self.clients.append(c)
        return c

    def join(self, label, g, rec, token=None, record_wait=True):
        c = self.guest(label, g, rec, token, record_wait)
        c.connect_and_ready(quiet=True)
        return c

    def attempt(self, label, g, rec, token=None, timeout=6.0):
        """-> (client, reject or None). On success the client is READY."""
        c = self.guest(label, g, rec, token, record_wait=False)
        try:
            c.connect_and_ready(timeout=timeout, quiet=True)
            L.pump_sleep(0.5)
            return c, None
        except L.HandshakeRejected as e:
            L.pump_sleep(0.3)
            return c, e.reject

    def resident(self, label, ident, slot, token):
        c = PClient(label, IP, self.port, player=ident, resident_claim=True, resident_token=token, bind_ip=self._bind(), record_hello=True, record_wait=False, wait_snapshot=False)
        c.rec_resident_idx = slot
        self.clients.append(c)
        c.connect_and_ready(timeout=8.0, quiet=True)
        L.pump_sleep(1.5)
        return c

    def release(self, c):
        if c is None:
            return
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


def gci_priv(gci, i):
    o = MF.MAIN_OFF + MF.PRIV_OFF + i * MF.PRIV_STRIDE
    return bytes(gci[o:o + MF.PRIV_STRIDE])


def gci_home(gci, i):
    o = MF.MAIN_OFF + MF.HOME_OFF + i * MF.HOME_STRIDE
    return bytes(gci[o:o + MF.HOME_STRIDE])


def snap():
    """Everything a refused purchase must leave byte-identical on disk."""
    return {"guests": raw_or_none(mpath("guests.dat")), "members": raw_or_none(mpath("members.dat")), "records": raw_or_none(mpath("records.dat")),
            "gci": T.md5_file(L.host_gci()), "baks": sorted(L.server_glob("*.bak-*"))}


def buy(c, price=PRICE, house=L.PC_NETGAME_HOUSE_AUTO, base=None, rid=1, **kw):
    """Sends ONE HOUSE_PURCHASE (kind 14) built from the client's local image. Returns the TxnSent."""
    return c.send_txn_commit(L.PC_NETGAME_TXN_KIND_HOUSE_PURCHASE, rid, L.PC_NETGAME_TXN_DEST_NONE, 0, 0, base=base, aux_cond=house, aux_item=price, **kw)


def source_audit(rig):
    ck = rig.ck
    ng = open(os.path.join(T.PC, "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read()
    mc = open(os.path.join(T.PC, "src", "pc_m_card.c"), encoding="utf-8", errors="replace").read()
    hh = ng[ng.index("static void pcnetgame_handle_host_house_purchase_txn("):ng.index("/* ===== HOUSE PURCHASE HOST END ===== */")]
    ck("S wire: kind 14 + reasons 28 / 29 / 30 + price 1000 + mPlayer_DEBT0 (= 18400) in C == net_spike_lib; the dispatch handles kind 14 BEFORE the X1 handler",
       "#define PC_NETGAME_TXN_KIND_HOUSE_PURCHASE 14u" in ng and "#define PC_NETGAME_TXN_REASON_NO_RESIDENCE    28u" in ng and "#define PC_NETGAME_TXN_REASON_INVALID_HOUSE   29u" in ng
       and "#define PC_NETGAME_TXN_REASON_NAME_TAKEN      30u" in ng and "(1000u + (uint32_t)mPlayer_DEBT0)" in ng and "#define mPlayer_DEBT0 17400" in open(os.path.join(T.PC, "..", "include", "m_player.h")).read()
       and (L.PC_NETGAME_TXN_KIND_HOUSE_PURCHASE, L.PC_NETGAME_TXN_REASON_NO_RESIDENCE, L.PC_NETGAME_TXN_REASON_INVALID_HOUSE, L.PC_NETGAME_TXN_REASON_NAME_TAKEN) == (14, 28, 29, 30)
       and ng.index("if (tc.kind == (uint8_t)PC_NETGAME_TXN_KIND_HOUSE_PURCHASE) {") < ng.index("pcnetgame_handle_host_txn_commit(peer, &tc);"))
    order = ["peer < 0 || peer >= pcnetgame_peer_span()", "pcnetgame_rec_gate(peer, 0, 0)", "if (idx < PLAYER_NUM) {", "s_guest_untrusted || s_members_untrusted", "shape_ok =", "pcnetgame_txn_nonce_fenced(",
             "pcnetgame_rec_refresh_hostfields(idx, slot)", "STALE_IMAGE", "pcnetgame_rec_validate_inventory(", "PRICE_MISMATCH", "INVALID_HOUSE", "NO_FUNDS",
             "mir->inventory.wallet = wallet_post;", "pcnetgame_promote_exec(NULL, g,", "mir->inventory.wallet = wallet_pre;", "pcnetgame_txn_send_result(peer, idx, in, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED",
             "PC_NETGAME_MSG_RESIDENT_HANDOFF", "PC_NETGAME_REJECT_PROMOTED"]
    idxs = [hh.find(x) for x in order]
    ck("S handler order: READY -> guest-only gate -> trusted files -> shape -> journal -> base -> inventory -> price -> house range -> funds -> debit -> promote core -> restore on failure -> RESULT -> 66 -> REJECT 6",
       all(i >= 0 for i in idxs) and idxs == sorted(idxs))
    ck("S the handler never touches private_data / homes directly (the mirror comes from the accessor, the core builds the resident)", "private_data" not in hh.replace("pcnetgame_rec_priv_ptr", "") and "homes[" not in hh)
    pe = ng[ng.index("static int pcnetgame_promote_exec(const char* gsel, int gfixed"):ng.index("/* ===== M-F END ===== */")]
    ck("S core: PROMOTE_ONLINE skips only the REQUESTING peer's binding, PROMOTE_PAID sets the paid creation (loan 0); the console wrapper runs flags 0 with its own confirm word",
       "pcnetgame_host_peer_bound_to_guest(g, online ? req_peer : (PCNetPeerId)-1)" in pe and "pc_mp_promote_set_paid((flags & PROMOTE_PAID) != 0)" in pe
       and "return pcnetgame_promote_exec(gsel, -1, ssel, hsel, confirm, 0u, (PCNetPeerId)-1, msg, cap, NULL);" in ng)
    ck("S pc_mp_promote_create: loan = mPlayer_DEBT0 unless the one-shot PAID flag (consumed at entry) says the guest paid the whole price (loan 0)",
       "priv->inventory.loan = mPlayer_DEBT0;" in mc and "if (paid) {\n        priv->inventory.loan = 0;" in mc and "s_pc_promote_paid = 0; /* consumed above into `paid` */" in mc)


def phase0(rig, args, ctx):
    ck = rig.ck
    h, ok = rig.start("p0", args.port - 1)
    if not ok:
        return
    try:
        for key in ("A", "B", "C", "D"):
            c = rig.join(key + "0", ctx["g"][key], ctx["rec"][key])
            ctx["tok"][key] = bytes(c.guest_token)
        L.pump_sleep(2.0)
        for c in list(rig.clients):
            rig.release(c)
        L.pump_sleep(7.0)  # the accepted uploads reach guests.dat with the 5 s early save (and with the final save at stop)
    finally:
        rig.closeall()
        code = rig.stop(h)
        ck("P0 graceful stop (exit 0)", code == 0)
    ents = parse_guests(mpath("guests.dat"))
    ck("P0 four guests stored with a synced record (rev >= 1)", len(ents) == 4 and all(e["rev"] >= 1 for e in ents))
    for key, wallet in (("A", 25000), ("B", 25000), ("C", 5000), ("D", 25000)):
        def goods(rec, wallet=wallet):
            struct.pack_into(">I", rec, L.REC_OFF_WALLET, wallet)
            struct.pack_into(">H", rec, L.REC_OFF_POCKETS, 0x2200)
        ck("P0 %s's stored record patched (wallet %d, pocket 0 = 0x2200)" % (key, wallet), patch_guest_record(mpath("guests.dat"), L.guest_pid_be(ctx["g"][key]), goods))


def phase1(rig, args, ctx):
    ck = rig.ck
    h, ok = rig.start("p1", args.port, ["--resident-tokens", "tofu"])
    if not ok:
        return h
    g, rec, tok = ctx["g"], ctx["rec"], ctx["tok"]
    try:
        c = rig.join("C", g["C"], rec["C"], token=tok["C"])
        d = rig.join("D", g["D"], rec["D"], token=tok["D"])
        L.pump_sleep(9.0)  # the token-presenting reconnects CONFIRM the entries (guests.dat writes): the snapshots below are taken AFTER them
        ck("P1 C holds the stored wallet 5000, D holds 25000 (the host pushed the stored record)", L.record_inventory(c.rec_local)[2] == 5000 and L.record_inventory(d.rec_local)[2] == 25000)
        pre = snap()
        # ---------------- P1 insufficient Bells
        sent = buy(c, rid=1)
        r = c.wait_txn_result(sent.seq, 5.0)
        ck("P1 insufficient Bells: REJECTED NO_FUNDS (19), kind echoed, no post-image", r is not None and r.kind == 14 and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and r.reason == L.PC_NETGAME_TXN_REASON_NO_FUNDS)
        L.pump_sleep(1.0)
        ck("P1 ... guests.dat / members.dat / records.dat / the GCI file / the backups are byte-identical, the peer stays connected, no 'BOUGHT' log line",
           snap() == pre and c.is_connected() and "BOUGHT a house" not in h.log_text())
        # ---------------- P7 refusals of a rich guest
        base = (d.rec_last[1], d.rec_last[2])
        cases = [
            ("a wrong price (18399)", dict(price=PRICE - 1), L.PC_NETGAME_TXN_REASON_PRICE_MISMATCH),
            ("a stale base (epoch + 1)", dict(base=(base[0] + 1, base[1])), L.PC_NETGAME_TXN_REASON_STALE_IMAGE),
            ("house 1 (owned by a resident)", dict(house=1), L.PC_NETGAME_TXN_REASON_INVALID_HOUSE),
            ("house 7 (out of range)", dict(house=7), L.PC_NETGAME_TXN_REASON_INVALID_HOUSE),
        ]
        for i, (label, kw, reason) in enumerate(cases):
            sent = buy(d, rid=10 + i, **kw)
            r = d.wait_txn_result(sent.seq, 5.0)
            ck("P7 %s -> REJECTED %s (%d)" % (label, L.TXN_REASON_NAMES.get(reason), reason), r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and r.reason == reason)
            L.pump_sleep(0.5)
        # a malformed message (flags != 0) -> BAD_SHAPE, nothing changes
        sent = buy(d, rid=20, flags=1)
        r = d.wait_txn_result(sent.seq, 5.0)
        ck("P7 a malformed message (flags 1) -> REJECTED BAD_SHAPE (11)", r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and r.reason == L.PC_NETGAME_TXN_REASON_BAD_SHAPE)
        L.pump_sleep(2.5)
        ck("P7 ... every refusal left the files byte-identical and D connected", snap() == pre and d.is_connected())
        # ---------------- P3 two guests, the LAST slot
        a = rig.join("A", g["A"], rec["A"], token=tok["A"])
        b = rig.join("B", g["B"], rec["B"], token=tok["B"])
        L.pump_sleep(6.0)
        ck("P3 A and B hold 25000 Bells on the host record", L.record_inventory(a.rec_local)[2] == 25000 and L.record_inventory(b.rec_local)[2] == 25000)
        off = len(h.log_text())
        sa = buy(a, rid=1)
        sb = buy(b, rid=1)  # back to back: the host runs them one after the other, synchronously
        ra, rb = a.wait_txn_result(sa.seq, 15.0), b.wait_txn_result(sb.seq, 15.0)
        res = {"A": ra, "B": rb}
        won = [k for k, r in res.items() if r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED]
        lost = [k for k, r in res.items() if r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED]
        ck("P3 exactly ONE APPLIED and the other REJECTED NO_RESIDENCE (28)", len(won) == 1 and len(lost) == 1 and res[lost[0]].reason == L.PC_NETGAME_TXN_REASON_NO_RESIDENCE)
        if len(won) != 1:
            return h
        w, l_ = won[0], lost[0]
        wc, lc = {"A": a, "B": b}[w], {"A": a, "B": b}[l_]
        ctx["winner"], ctx["loser"] = w, l_
        L.pump_sleep(2.0)
        types = [t for t, _p in wc.seen]
        rr = res[w]
        ck("P4 the winner got TXN_RESULT (APPLIED, kind 14, post wallet 25000 - 18400 = 6600, pockets untouched) -> RESIDENT_HANDOFF (66) -> REJECT 6 PROMOTED, in that order",
           types == [MSG_TXN_RESULT, L.PC_NETGAME_MSG_RESIDENT_HANDOFF, L.PC_NETGAME_MSG_REJECT] and rr.kind == 14 and rr.post_wallet == 25000 - PRICE and rr.post_pockets[0] == 0x2200
           and wc.seen[2][1][1] == L.PC_NETGAME_REJECT_PROMOTED)
        ck("P3 the loser stays connected with its wallet intact (record on the host: no change; its client image still 25000)", lc.is_connected() and L.record_inventory(lc.rec_local)[2] == 25000)
        t = h.log_text()[off:]
        ck("P4 host log: 'BOUGHT a house: resident slot 3, house 3, price 18400, wallet 25000 -> 6600, loan 0', then RESIDENT_HANDOFF sent", "BOUGHT a house: resident slot 3, house 3, price 18400, wallet 25000 -> 6600, loan 0" in t
           and "RESIDENT_HANDOFF sent" in t)
        # members.dat / guests.dat / records.dat right after the purchase
        ents = parse_guests(mpath("guests.dat"))
        pidw = L.guest_pid_be(g[w])
        ck("P4 guests.dat: the winner's entry is GONE (removed last), the loser, C and D remain", all(e["pid"] != pidw for e in ents) and len(ents) == 3)
        mem = parse_members(mpath("members.dat"))
        k1 = [e for e in mem if e["kind"] == 1]
        k2 = [e for e in mem if e["kind"] == 2]
        ck("P4 members.dat: exactly one RESIDENT_TOKEN + one PROMOTION_HANDOFF, both slot 3, unconfirmed; the handoff carries the winner's GUEST token and home PID",
           len(mem) == 2 and len(k1) == 1 and len(k2) == 1 and k1[0]["slot"] == 3 and k2[0]["slot"] == 3 and bytes(k2[0]["token"]) == tok[w] and bytes(k2[0]["aux"]) == pidw)
        if len(k1) == 1:
            ctx["npid"], ctx["rtok"] = bytes(k1[0]["pid"]), bytes(k1[0]["token"])
        # ---------------- P2 the town is FULL now
        pre2 = snap()
        sent = buy(d, rid=30)
        r = d.wait_txn_result(sent.seq, 5.0)
        ck("P2 the town is FULL: D's valid purchase -> REJECTED NO_RESIDENCE (28), wallet untouched", r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and r.reason == L.PC_NETGAME_TXN_REASON_NO_RESIDENCE)
        L.pump_sleep(2.0)
        s2 = snap()
        ck("P2 ... guests.dat (D's record), members.dat and the GCI file unchanged by the refusal", s2["members"] == pre2["members"] and s2["gci"] == pre2["gci"] and d.is_connected())
        for cl in (c, d, lc):
            rig.release(cl)
        L.pump_sleep(3.0)  # the host drops the released peers: the console refuses an ONLINE guest first
        o = say(h, "promote LATEGST auto auto")
        ck("P2 the console `promote` (same shared core, flags 0) still refuses: 'the town is FULL'", "the town is FULL" in o)
        # ---------------- P5 the winner comes back
        off = len(h.log_text())
        w2, rej = rig.attempt("W-back", g[w], rec[w], token=tok[w])
        ck("P5 the winner reconnecting with its GUEST token is handed off AGAIN: RESIDENT_HANDOFF (66) then REJECT 6", rej is not None and rej.reason == L.PC_NETGAME_REJECT_PROMOTED
           and [t_ for t_, _p in w2.seen] == [L.PC_NETGAME_MSG_RESIDENT_HANDOFF, L.PC_NETGAME_MSG_REJECT] and "the guest was PROMOTED to resident 3" in h.log_text()[off:])
        rig.release(w2)
        if "npid" in ctx:
            ident = L.PlayerIdentity(ctx["npid"][:8], struct.unpack(">H", ctx["npid"][16:18])[0], 1)
            off = len(h.log_text())
            rc = rig.resident("RES3", ident, 3, ctx["rtok"])
            t = h.log_text()[off:]
            last = rc.token_msgs[-1][1] if rc.token_msgs else None
            ck("P5 a RESIDENT-claim client with the new PID + the handed-over token is admitted as resident 3 (IDENTITY_TOKEN KNOWN|RESIDENT)", rc.is_connected() and "bound to resident 3 (host-derived)" in t
               and last is not None and last.flags == (L.PC_NETGAME_IDTOKEN_FLAG_RESIDENT | L.PC_NETGAME_IDTOKEN_FLAG_KNOWN) and bytes(last.token) == ctx["rtok"])
            # ---------------- P6 a resident sender
            pockets, conds, wallet = (tuple([0] * 15), 0, 0)
            sent = rc.send_txn_commit(L.PC_NETGAME_TXN_KIND_HOUSE_PURCHASE, 1, L.PC_NETGAME_TXN_DEST_NONE, 0, 0, pre=(tuple(0xFFFF for _ in range(15)), 0, 0), base=(0, 0),
                                      aux_cond=L.PC_NETGAME_HOUSE_AUTO, aux_item=PRICE)
            r = rc.wait_txn_result(sent.seq, 5.0)
            ck("P6 a RESIDENT sender -> REJECTED (PRECOND 9: a resident already owns a house), nothing created", r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and r.reason == L.PC_NETGAME_TXN_REASON_PRECOND)
            rig.release(rc)
    finally:
        rig.closeall()
        code = rig.stop(h)
        ck("P1 graceful stop (exit 0)", code == 0)
    return h


def final_checks(rig, ctx):
    ck = rig.ck
    if "winner" not in ctx or "npid" not in ctx:
        ck("F skipped: no purchase happened", False)
        return
    gci = raw(L.host_gci())
    orig = ctx["orig_gci"]
    npid, w, l_ = ctx["npid"], ctx["winner"], ctx["loser"]
    p3 = gci_priv(gci, 3)
    ck("F GCI: resident 3 = the buyer (name, host land, the new player id), exists, loan 0 (the price was paid in full), wallet 6600, pocket 0 = 0x2200",
       p3[0:20] == npid and p3[0:8] == bytes(ctx["g"][w].player_name) and p3[0x1086] == 1 and struct.unpack(">I", p3[L.REC_OFF_LOAN:L.REC_OFF_LOAN + 4])[0] == 0
       and struct.unpack(">I", p3[L.REC_OFF_WALLET:L.REC_OFF_WALLET + 4])[0] == 25000 - PRICE and struct.unpack(">H", p3[L.REC_OFF_POCKETS:L.REC_OFF_POCKETS + 2])[0] == 0x2200)
    h3 = gci_home(gci, 3)
    ck("F GCI: house 3 ownerID = the new resident, the rest of the house unchanged; the arrangement is unchanged (3 -> 3, 0x%02X)" % orig[ARRANGEMENT_OFF],
       h3[0:20] == npid and h3[20:] == gci_home(orig, 3)[20:] and gci[ARRANGEMENT_OFF] == orig[ARRANGEMENT_OFF])
    ck("F GCI: residents 0..2 and houses 0..2 (before the mailbox tail) are byte-identical to the fixture and the checksum validates",
       all(gci_priv(gci, i) == gci_priv(orig, i) for i in range(3)) and all(gci_home(gci, i)[:0x1A30] == gci_home(orig, i)[:0x1A30] for i in range(3))
       and MF.stored_checksum(gci[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE]) == MF.checksum(gci[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE]))
    ents = {bytes(e["pid"]): e for e in parse_guests(mpath("guests.dat"))}
    ck("F guests.dat after the run: the buyer is gone; the loser's stored wallet is still 25000 (a refused purchase never charges); POORGST 5000, LATEGST 25000",
       bytes(L.guest_pid_be(ctx["g"][w])) not in ents and ents[bytes(L.guest_pid_be(ctx["g"][l_]))]["wallet"] == 25000
       and ents[bytes(L.guest_pid_be(ctx["g"]["C"]))]["wallet"] == 5000 and ents[bytes(L.guest_pid_be(ctx["g"]["D"]))]["wallet"] == 25000)
    recs = parse_records(mpath("records.dat"))
    ck("F records.dat: slot 3 = the new PID at rev 1 (the host record is the truth)", 3 in recs and recs[3]["rev"] == 1 and recs[3]["pid"] == npid)


def run(args, results):
    rig = Rig(results)
    ctx = {"g": {}, "rec": {}, "tok": {}}
    src = os.path.join(L.GAME_BIN_DIR, MF.GCI_REL)
    ctx["orig_gci"] = raw(src)
    names = {"A": ("BUYERA", 0x4B11), "B": ("BUYERB", 0x4B12), "C": ("POORGST", 0x4B13), "D": ("LATEGST", 0x4B14)}
    for k, (n, pid) in names.items():
        ctx["g"][k] = L.guest_identity(n, pid, "HOMETWN", 0x5B21)
        ctx["rec"][k] = bytes(L.fresh_guest_record_for(ctx["g"][k]))
    L.server_wipe_sidecars()  # the disposable fixture only
    source_audit(rig)
    for name, fn in (("P0", phase0), ("P1", phase1)):
        try:
            fn(rig, args, ctx)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            rig.ck("%s aborted by an exception: %s" % (name, e), False)
            rig.closeall()
    final_checks(rig, ctx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12981)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: this test mutates the game save; run it on a disposable pc\\build64\\bin_fixture4_<name> copy only", file=sys.stderr)
        return 2
    results = []
    try:
        run(args, results)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("test aborted by an exception: %s" % e, False, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
