#!/usr/bin/env python3
"""test_guest_protocol.py - GUESTS G1 (HOST half): extra players ("guests", a foreigner whose home town is NOT the host's town) with their
own host-side personal record, IDENTITY_EXT (57) / IDENTITY_TOKEN (58), record_class GUEST (RECORD_BEGIN.rsv = 1) over the D3 record transport,
a bounded guest table (8) with a trust-on-first-use token and save/mp/guests.dat.

PROTOCOL test against a REAL host process (`AnimalCrossing.exe --host --bootstrap-resident 0 --pickup-test-seed --bury-test-seed
--field-action-test-seed --ts-test-seed-police=...`, started/stopped by this script from the TEST COPY named by NET_SPIKE_GAME_BIN =
pc\\build64\\bin_fixture4; host = resident 0, clients = residents 1..3, at most 3 simultaneous clients, ports 11400+). `require_test_bin_dir()`
refuses the live dir; the fixture save dir is SNAPSHOTTED at start and RESTORED afterwards (guests.dat lives in save/mp, so it is removed
with the restore). The clients are scripted FakeClients (net_spike_lib: guest mode); the host's C code runs for real. The REAL game client as a
guest is NOT exercised here (G2). Persistence across a host restart is test_guest_persist.py; the file format / corruption handling is the native
unit test test_guest_storage.py; the source rules are test_guest_src.py.

Sections (one host process, in order; <= 3 live clients at any time):
  W    wire: spec sizes 42 / 20, builders
  F    FIRST CONTACT: token minted (IDENTITY_TOKEN right after IDENTITY_ACK, flag NEW, slot 0, 16 random non-zero bytes, never logged), record flow with
       record_class GUEST (MIGRATE_REQUEST -> MIGRATE_UPLOAD -> APPLIED rev 1 -> PUSH_FULL carrying rsv = 1), the pushed record == the merge of the
       upload into the host's blank guest record, guests.dat written immediately (independent Python parser: key, token, rev 0), nothing sidecar-like in
       card_a / card_b
  S    SECOND CONNECT with the token: same slot, no MIGRATE, push == the stored record, KNOWN flag, same token
  T    WRONG / MISSING token refused (the live guest unaffected), a record_class mismatch is refused BAD_SHAPE detail 5 without a violation
  D    two live sessions of one guest key: parked then refused after the park window; a SILENT old session is evicted and the newcomer admitted
  P    RESIDENT PRIORITY: a guest-flagged claim of a resident (free / connected / the host's own) is refused and never binds anything; the real resident
       then still connects; an unknown identity WITHOUT a valid EXT is refused exactly as before (NO_SAVE); malformed / late / flag-less EXT are ignored
       (so the claim is refused); home land == town land and IDENTITY / EXT mismatch are refused
  X    GUEST TRANSACTIONS against the guest record (slot space 4..11): pickup (pocket + money bag), drop, bury, DIG_BURIED grant, SHOP_BUY / SHOP_SELL,
       POLICE_CLAIM work; MUSEUM_DONATE / MAIL_SEND / MAIL_TAKE are refused NO_DONOR_SLOT (no rev change), no MAILBOX_LETTER is ever pushed to a guest;
       FRIENDSHIP_REQUEST / MAIL_REQUEST are keyed by the guest's home PersonalID (a forged mail sender is overwritten); a new session sees the final record
  N    TABLE FULL: 8 guests fit, the 9th is refused (nothing disposable), a known guest still reconnects; guests.dat holds exactly the 8 (unique tokens)
  M    (SECOND host process, pre-seeded guests.dat with an entry of ANOTHER town) M1 table keyed by (host town, key): the other town's entry is kept but
       inactive, the same key presenting that token is a first contact; M3 an UNCONFIRMED data-less entry is RE-MINTED for its key (a lost TOKEN must not
       lock the guest out), confirmed by a token-presenting reconnect, then a squatter / old token is refused; a squatter WITHOUT the token against a
       SILENT LIVE guest is refused with the session untouched and no eviction; per-address first-contact limit (3 per 60 s); table full: the OLDEST
       unconfirmed data-less idle entry is evicted, confirmed / data-bearing entries never

Tier: PROTOCOL TESTED (real host binary, scripted clients). Usage: python test_guest_protocol.py [--port 11400]
"""
import argparse
import os
import re
import shutil
import struct
import sys
import time
import zlib

import net_spike_lib as L
import test_shop_protocol as SH
import test_txn_protocol as TP
from test_txn_protocol import APPLIED, REJECTED, R, D_POCKET, first_free

HERE = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR_REL = "save"
GUEST_ENTRY = 4 + 20 + 16 + 16 + 16 + 0x2440   # v2: + age / town identity (pc_mp_guests.h)
GUEST_FILE = 32 + 8 * GUEST_ENTRY + 4
HOST_EXTRA = ["--pickup-test-seed", "--bury-test-seed", "--field-action-test-seed", "--ts-test-seed-police=0x2800,0x2801,0x2802"]
BURIED_TILE = (40, 104)   # --field-action-test-seed: a buried ITM_FOOD_APPLE
ITM_APPLE = 0x2800
FA_BURIED = L.FIELD_ACTION_KIND_DIG_BURIED
K_BURIED = L.PC_NETGAME_TXN_KIND_DIG_BURIED
SVC_SHOP, SVC_POLICE = L.PC_NETGAME_TS_SHOP, L.PC_NETGAME_TS_POLICE


def guests_path():
    return os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL, "mp", "guests.dat")


def parse_guests(path):
    """Independent parser of the guests.dat v2 layout (pc_mp_guests.h). Returns {'gen', 'e': [dict]}; raises AssertionError on any violation."""
    with open(path, "rb") as f:
        b = f.read()
    assert len(b) == GUEST_FILE, "size %d != %d" % (len(b), GUEST_FILE)
    assert b[:8] == b"ACMPGST\x00"
    ver, flags, nslots, esz, gen, rsv = struct.unpack("<6I", b[8:32])
    assert (ver, flags, nslots, esz, rsv) == (2, 0, 8, GUEST_ENTRY, 0), (ver, flags, nslots, esz, rsv)
    assert struct.unpack("<I", b[-4:])[0] == (zlib.crc32(b[:-4]) & 0xFFFFFFFF), "file crc32"
    ents = []
    for i in range(8):
        e = b[32 + i * GUEST_ENTRY:32 + (i + 1) * GUEST_ENTRY]
        d = {"present": e[0]}
        assert e[0] <= 1 and e[1] <= 1 and e[2] == 0 and e[3] == 0, "flag bytes"
        if e[0]:
            d["confirmed"] = e[1]
            d["pid"], d["token"] = bytes(e[4:24]), bytes(e[24:40])
            d["epoch"], d["rev"], d["age"], d["crc"] = struct.unpack("<4I", e[40:56])
            d["town_land_name"] = bytes(e[56:64])
            d["town_land_id"], tr, d["town_terrain_hash"] = struct.unpack("<HHI", e[64:72])
            d["record"] = bytes(e[72:])
            assert tr == 0 and d["crc"] == (zlib.crc32(d["record"]) & 0xFFFFFFFF), "record crc32 / town reserved"
            assert d["record"][:20] == d["pid"] and d["record"][0x1086] == 1, "record belongs to the key and exists"
        ents.append(d)
    return {"gen": gen, "e": ents}


def build_guests(entries, gen=1):
    """INDEPENDENT writer of the guests.dat v2 layout (a test seed). entries: up to 8 dicts (or None): pid, token, epoch, rev, age, confirmed,
    town_name (8 B), town_id, town_hash, record."""
    buf = bytearray(GUEST_FILE)
    buf[0:8] = b"ACMPGST\x00"
    struct.pack_into("<6I", buf, 8, 2, 0, 8, GUEST_ENTRY, gen, 0)
    for i, d in enumerate(entries):
        if not d:
            continue
        o = 32 + i * GUEST_ENTRY
        buf[o] = 1
        buf[o + 1] = 1 if d["confirmed"] else 0
        buf[o + 4:o + 24] = d["pid"]
        buf[o + 24:o + 40] = d["token"]
        struct.pack_into("<4I", buf, o + 40, d["epoch"], d["rev"], d["age"], zlib.crc32(d["record"]) & 0xFFFFFFFF)
        buf[o + 56:o + 64] = d["town_name"]
        struct.pack_into("<HHI", buf, o + 64, d["town_id"], 0, d["town_hash"])
        buf[o + 72:o + 72 + 0x2440] = d["record"]
    struct.pack_into("<I", buf, len(buf) - 4, zlib.crc32(bytes(buf[:-4])) & 0xFFFFFFFF)
    return bytes(buf)


class GRun(TP.Run):
    """TP.Run plus guest clients. Every guest client gets its OWN loopback source address (127.0.x.y) unless `bind_ip` is given: the host limits
    token issuances per ADDRESS (3 per 60 s), so a test that mints many guests from one address would trip the (correct) limit."""
    _ip_n = [0]

    def guest(self, label, g, token=None, rec=None, **kw):
        if "bind_ip" not in kw:
            GRun._ip_n[0] += 1
            kw["bind_ip"] = "127.0.%d.%d" % (20 + GRun._ip_n[0] // 200, 2 + GRun._ip_n[0] % 200)
        c = L.FakeClient(label, self.ip, self.port, guest=g, guest_token=token, guest_record_img=rec, **kw)
        self.clients.append(c)
        return c

    def attempt(self, c, timeout=5.0):
        """connect_and_ready; returns (ready client or None, REJECT fields or None)."""
        try:
            c.connect_and_ready(timeout=timeout, quiet=True)
            return c, None
        except L.HandshakeRejected as e:
            return None, e.reject
        finally:
            pass

    def manual(self, c, pre=(), post=(), ident=True, timeout=5.0):
        """Transport connect, optional raw messages BEFORE the IDENTITY, the IDENTITY, raw messages AFTER, then the handshake reply Msg (or None)."""
        c.town_claimed = c.claimed_town()
        c.connect(timeout=3.0)
        for raw in pre:
            c.send_reliable(raw)
        if ident:
            c.send_identity(town=c.town_claimed)
        for raw in post:
            c.send_reliable(raw)
        return c.wait_handshake_reply(timeout)


def mail_bytes(sender_player, land_name, land_id):
    """298-byte Mail_c: unresolvable recipient (nothing is delivered), the claimed sender PersonalID at +0x16 (name[8], land[8], player_id LE, land_id LE)."""
    buf = bytearray(298)
    buf[0x16:0x1E] = bytes(sender_player.player_name)[:8].ljust(8, b"\x00")
    buf[0x1E:0x26] = bytes(land_name)[:8].ljust(8, b"\x00")
    struct.pack_into("<HH", buf, 0x26, sender_player.player_id & 0xFFFF, land_id & 0xFFFF)
    return bytes(buf)


def gid(i):
    """The i-th extra test guest (home town HOMETWN, distinct ids)."""
    return L.guest_identity("GUEST%c" % (ord("A") + i), 0x4A01 + i, "HOMETWN", 0x5B01)


# ======================================================================================================================
# sections
# ======================================================================================================================
def s_wire(results):
    ck = lambda d, c: L.check(d, bool(c), results)
    g = gid(0)
    raw = L.build_identity_ext(g, b"\x11" * 16)
    f = L.IDENTITY_EXT_SPEC.decode(raw)
    ck("W IDENTITY_EXT is 42 bytes, id 57; decodes to the flag, the home PersonalID fields and the token", len(raw) == 42 and f.msg_type == 57 and f.flags == 1
       and bytes(f.home_player_name) == g.player_name and f.home_player_id == g.player_id and f.home_land_id == g.land_id and f.token_present == 1
       and bytes(f.token) == b"\x11" * 16 and f.reserved0 == 0 and f.reserved1 == 0)
    raw0 = L.build_identity_ext(g, None)
    ck("W without a token: token_present 0 and 16 zero bytes", L.IDENTITY_EXT_SPEC.decode(raw0).token_present == 0 and bytes(L.IDENTITY_EXT_SPEC.decode(raw0).token) == b"\x00" * 16)
    ck("W IDENTITY_TOKEN is 20 bytes, id 58", L.IDENTITY_TOKEN_SPEC.size == 20 and L.PC_NETGAME_MSG_IDENTITY_TOKEN == 58 and L.PC_NETGAME_MSG_IDENTITY_EXT == 57)
    ck("W the frozen IDENTITY / IDENTITY_ACK stay 32 bytes", L.IDENTITY_SPEC.size == 32 and L.IDENTITY_ACK_SPEC.size == 32)
    ck("W the guest pid helper is the BE PersonalID (the first 0x14 bytes of a record image)",
       L.guest_pid_be(g) == g.player_name + g.land_name + struct.pack(">HH", g.player_id, g.land_id) and len(L.guest_pid_be(g)) == 20)
    ck("W the guest record helper re-keys a legal record: PersonalID replaced, the rest identical", L.guest_record(g)[:20] == L.guest_pid_be(g) and len(L.guest_record(g)) == L.PC_NETGAME_REC_SIZE)


def s_first_contact(run):
    ck = run.check
    g = gid(0)
    a = run.guest("A", g)
    a.connect_and_ready(quiet=True)
    toks = [x for c, x in a.token_msgs if c == a.connect_count]
    tk = toks[0] if toks else None
    ck("F exactly one IDENTITY_TOKEN: flag NEW, guest_slot 0, table_size 8, 16 bytes not all zero",
       len(toks) == 1 and tk.flags == L.PC_NETGAME_IDTOKEN_FLAG_NEW and tk.guest_slot == 0 and tk.table_size == 8 and bytes(tk.token) != b"\x00" * 16)
    tok_idx = [m.index for m in a.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_IDENTITY_TOKEN, (L.CH_RELIABLE,)))]
    ck("F the IDENTITY_TOKEN is delivered right after the IDENTITY_ACK (reliable order) and before anything else the host sends",
       len(tok_idx) == 1 and tok_idx[0] > a.identity_ack_index and not [m for m in a.inbox.peek_all(None) if a.identity_ack_index < m.index < tok_idx[0]])
    ck("F the client double remembers the token (what the real client persists)", a.guest_token == bytes(tk.token))
    acks = [x for _c, x in a.rec_acks]
    ck("F record flow: MIGRATE_REQUEST (rev 0, xfer 0) then APPLIED for the MIGRATE upload (xfer 1, rev 1), same epoch",
       len(acks) >= 2 and acks[0].status == L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST and acks[0].xfer_id == 0 and acks[0].rev == 0
       and acks[1].status == L.PC_NETGAME_REC_ACK_APPLIED and acks[1].xfer_id == 1 and acks[1].rev == 1 and acks[1].epoch == acks[0].epoch)
    push = a.rec_pushes[0] if a.rec_pushes else None
    up = a.own_record()
    exp = L.record_merge_expected(L.guest_blank_record(g), up)
    ck("F the PUSH_FULL after the migration: kind 1, rev 1, digest ok, record_class GUEST (rsv 1)",
       push is not None and push["kind"] == L.PC_NETGAME_REC_KIND_PUSH_FULL and push["rev"] == 1 and push["digest_ok"] and push["rsv"] == L.PC_NETGAME_REC_CLASS_GUEST)
    ck("F the pushed record == the client-owned / shared ranges of the upload merged into the host's BLANK guest record (host-owned ranges stay zero, "
       "player_ID = the guest key, exists = 1)", push is not None and push["data"] == exp and push["data"][:20] == L.guest_pid_be(g))
    ck("F the pushed record is NOT any resident's: no resident PersonalID in it", push is not None and all(push["data"][:20] != L.record_from_gci(run.snap_gci, i)[:20] for i in range(4)))
    t = run.log()
    ck("F host log: bound to GUEST slot 0 (first contact: token minted), IDENTITY_EXT cached, guests.dat written when the token was minted",
       re.search(r"peer \d+ bound to GUEST slot 0 \(first contact: token minted;", t) and "IDENTITY_EXT cached (guest=1, token_present=0)" in t
       and "guests.dat written (new guest token minted" in t)
    ck("F the token never appears in the host log (it is a secret)", bytes(tk.token).hex() not in t.lower())
    ck("F guests.dat exists at save/mp/guests.dat (a sibling of card_a / card_b) right after the mint", os.path.isfile(guests_path()))
    try:
        pf = parse_guests(guests_path())
        e0 = pf["e"][0]
        ck("F guests.dat (parsed by an independent implementation): entry 0 = the guest key, the issued token, rev 0, the blank record; the other 7 absent",
           e0["present"] and e0["pid"] == L.guest_pid_be(g) and e0["token"] == bytes(tk.token) and e0["rev"] == 0 and e0["record"] == L.guest_blank_record(g)
           and all(not x["present"] for x in pf["e"][1:]))
    except AssertionError as exc:
        ck("F guests.dat parses (%s)" % exc, False)
    sd = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL)
    ck("F nothing guest-like in card_a / card_b, no .gci / GAF named file in save/mp",
       all("guest" not in n.lower() for d in ("card_a", "card_b") for n in os.listdir(os.path.join(sd, d)) if os.path.isdir(os.path.join(sd, d)))
       and all(not re.search(r"\.gci|^GAF", n, re.I) for n in os.listdir(os.path.join(sd, "mp"))))
    return a, exp, bytes(tk.token), push["session"], push["epoch"]


def s_second(run, a, exp, tok, sess):
    ck = run.check
    g = gid(0)
    run.release(a)
    a2 = run.guest("A2", g, token=tok)
    a2.connect_and_ready(quiet=True)
    toks = [x for c, x in a2.token_msgs if c == a2.connect_count]
    acks = [x.status for _c, x in a2.rec_acks]
    push = a2.rec_pushes[-1] if a2.rec_pushes else None
    ck("S second connect with the token: IDENTITY_TOKEN flag KNOWN, slot 0, the SAME token", len(toks) == 1 and toks[0].flags == L.PC_NETGAME_IDTOKEN_FLAG_KNOWN
       and toks[0].guest_slot == 0 and bytes(toks[0].token) == tok)
    ck("S no MIGRATE_REQUEST (rev > 0); the host pushes the STORED record (rev 1, same host_session, class GUEST), byte for byte",
       L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST not in acks and push is not None and push["data"] == exp and push["rev"] == 1 and push["session"] == sess
       and push["rsv"] == L.PC_NETGAME_REC_CLASS_GUEST)
    ck("S host log: bound to GUEST slot 0 (known guest: token verified)", re.search(r"peer \d+ bound to GUEST slot 0 \(known guest: token verified;", run.log()) is not None)
    # an upload on the guest lineage works and the next session sees it
    up = L.record_set_u32(exp, L.REC_OFF_WALLET, 4242)
    up = L.record_set_u16(up, L.REC_OFF_POCKETS + 2 * 5, ITM_APPLE)
    L.pump_sleep(1.7)
    x = a2.upload_record(up, base=(push["epoch"], 1))
    ack = a2.wait_record_ack(xfer_id=x)
    ck("S an UPLOAD (class GUEST) on the guest lineage is APPLIED rev 2", ack is not None and ack.status == 0 and ack.rev == 2)
    exp2 = L.record_merge_expected(exp, up)
    return a2, exp2


def s_tokens(run, a2, tok):
    ck = run.check
    g = gid(0)
    off = len(run.log())
    c1, rej1 = run.attempt(run.guest("B-notoken", g, token=None))
    ck("T the known guest key WITHOUT a token is REFUSED (a squatter cannot take over): REJECT reason SERVER_FULL", c1 is None and rej1 is not None
       and rej1.reason == L.PC_NETGAME_REJECT_SERVER_FULL and "known guest key presented WITHOUT a token" in run.log()[off:])
    c2, rej2 = run.attempt(run.guest("B-wrong", g, token=bytes(b ^ 0x5A for b in tok)))
    ck("T the known guest key with a WRONG token is REFUSED", c2 is None and rej2 is not None and "known guest key presented a WRONG token" in run.log()[off:])
    L.pump_sleep(0.5)
    ck("T the live guest session is unaffected by both attempts (still connected, no new binding, one 'bound to GUEST slot 0 (known' line since)",
       a2.is_connected() and len(re.findall(r"bound to GUEST slot", run.log()[off:])) == 0)
    # record_class mismatch: a guest connection sending the RESIDENT class (and vice versa) is refused detail 5, not a violation
    off = len(run.log())
    L.pump_sleep(1.7)
    x = a2.upload_record(a2.rec_local, base=(a2.rec_last[1], a2.rec_last[2]), rsv=L.PC_NETGAME_REC_CLASS_RESIDENT) if a2.rec_local else None
    ack = a2.wait_record_ack(xfer_id=x, timeout=3.0) if x is not None else None
    ck("T a BEGIN with record_class RESIDENT on a GUEST connection -> BAD_SHAPE detail 5, its chunks absorbed, NOT a violation, peer alive",
       ack is not None and ack.status == L.PC_NETGAME_REC_ACK_BAD_SHAPE and ack.detail == 5 and "violation" not in run.log()[off:] and a2.is_connected())


def s_dup(run, a2, tok, exp2):
    ck = run.check
    g = gid(0)
    off = len(run.log())
    t0 = time.monotonic()
    d, rej = run.attempt(run.guest("D", g, token=tok), timeout=10.0)
    dt = time.monotonic() - t0
    ck("D a SECOND live session of the same guest key (valid token) is PARKED, then REFUSED after the park window (>= 5 s, SERVER_FULL); the first stays alive",
       d is None and rej is not None and rej.reason == L.PC_NETGAME_REJECT_SERVER_FULL and 5.0 <= dt <= 9.5 and a2.is_connected()
       and re.search(r"parked \(guest slot 0 live on peer \d+, idle \d+ ms\)", run.log()[off:])
       and re.search(r"peer \d+ claims guest slot 0, still bound to live peer \d+ after 6000 ms", run.log()[off:])
       and "claimed guest is already connected on another peer" in run.log()[off:])
    # stale: the old session goes silent -> evicted after 2.5 s idle, the newcomer is admitted after the teardown
    off = len(run.log())
    a2.go_silent()
    e = run.guest("E", g, token=tok)
    t0 = time.monotonic()
    try:
        e.connect_and_ready(timeout=15.0, quiet=True)
        ok = True
    except Exception as exc:  # noqa: BLE001
        print("E handshake failed:", exc)
        ok = False
    dt = time.monotonic() - t0
    ck("D a SILENT old session is evicted (idle >= 2500 ms) and the newcomer with the token is admitted to the SAME slot (%.1f s)" % dt,
       ok and e.identity_ack is not None and re.search(r"evicted stale peer \d+ \(guest slot 0, idle \d+ ms\) for peer \d+", run.log()[off:])
       and re.search(r"bound to GUEST slot 0 \(known guest", run.log()[off:]) and e.rec_pushes and e.rec_pushes[-1]["rsv"] == 1)
    ck("D the admitted newcomer is pushed the SAME stored record (rev 2: the upload of the evicted session survived), no MIGRATE_REQUEST",
       ok and e.rec_pushes and e.rec_pushes[-1]["data"] == exp2 and e.rec_pushes[-1]["rev"] == 2
       and all(x.status != L.PC_NETGAME_REC_ACK_MIGRATE_REQUEST for _c, x in e.rec_acks))
    run.release(a2)
    return e


def s_resident_priority(run, e, tok):
    ck = run.check
    town = L.resolve_host_town(run.ip, run.port)
    land_name, land_id = bytes(town.land_name), town.land_id

    def as_guest(slot, label):
        pl = L.resident_player(slot)
        return run.guest(label, L.GuestIdentity(bytes(pl.player_name), pl.player_id, land_name, land_id))

    # ---- a guest-flagged claim of a FREE resident ----
    off = len(run.log())
    c, rej = run.attempt(as_guest(run.r3, "P-free"))
    ck("P a guest-flagged claim whose IDENTITY matches a FREE resident is REFUSED (SERVER_FULL), logged, never bound as guest or resident",
       c is None and rej is not None and rej.reason == L.PC_NETGAME_REJECT_SERVER_FULL
       and re.search(r"sent a GUEST claim but its IDENTITY matches resident %d" % run.r3, run.log()[off:])
       and "guest-flagged claim matches a resident of this town" in run.log()[off:] and "bound to resident %d" % run.r3 not in run.log()[off:]
       and "bound to GUEST" not in run.log()[off:])
    # ---- a guest-flagged claim of a CONNECTED resident ----
    r1c = run.ready("R1", run.r1)
    off = len(run.log())
    t0 = time.monotonic()
    c, rej = run.attempt(as_guest(run.r1, "P-conn"))
    ck("P a guest-flagged claim of a CONNECTED resident is refused IMMEDIATELY (not parked: the guest path never contests a resident), the resident is unaffected",
       c is None and rej is not None and time.monotonic() - t0 < 4.0 and "parked" not in run.log()[off:] and r1c.is_connected())
    # ---- a guest-flagged claim of the HOST's own resident ----
    off = len(run.log())
    c, rej = run.attempt(as_guest(run.host_slot, "P-host"))
    ck("P a guest-flagged claim of the host's OWN resident is refused", c is None and rej is not None
       and re.search(r"sent a GUEST claim but its IDENTITY matches resident %d" % run.host_slot, run.log()[off:]))
    # ---- the real resident still connects afterwards (the failed guest attempts bound nothing) ----
    r3c = run.ready("R3", run.r3)
    ck("P the real resident (slot %d) connects normally afterwards: bound to resident %d, no guest involved" % (run.r3, run.r3),
       r3c.identity_ack is not None and "peer %d bound to resident %d" % (r3c.assigned_peer_id, run.r3) in run.log() and not r3c.token_msgs)
    # ---- L5: the class-mismatch "vice versa": a RESIDENT connection sending RECORD_BEGIN with record_class GUEST (rsv 1) ----
    off = len(run.log())
    L.pump_sleep(1.7)
    xv = r1c.upload_record(r1c.rec_local, base=(r1c.rec_last[1], r1c.rec_last[2]), rsv=L.PC_NETGAME_REC_CLASS_GUEST) if r1c.rec_local and r1c.rec_last else None
    ackv = r1c.wait_record_ack(xfer_id=xv, timeout=3.0) if xv is not None else None
    ck("P a BEGIN with record_class GUEST on a RESIDENT connection -> BAD_SHAPE detail 5, chunks absorbed, NOT a violation, the resident stays connected",
       ackv is not None and ackv.status == L.PC_NETGAME_REC_ACK_BAD_SHAPE and ackv.detail == 5 and "violation" not in run.log()[off:] and r1c.is_connected())
    run.release(r3c)
    run.release(r1c)
    # ---- unknown identity WITHOUT EXT: refused exactly as before (NO_SAVE) ----
    off = len(run.log())
    u, rej = run.attempt(L.FakeClient("U", run.ip, run.port, player=L.PlayerIdentity(b"NOBODY\x00\x00", 0x1234, 1)))
    ck("P an unknown identity without IDENTITY_EXT is refused as before: REJECT NO_SAVE, 'matches no resident record'", u is None and rej is not None
       and rej.reason == L.PC_NETGAME_REJECT_NO_SAVE and "claimed identity matches no resident record of this town" in run.log()[off:])
    # ---- EXT present but not a guest claim / malformed / late ----
    g = gid(3)
    cases = [("flags 0 (no guest bit), token present", [L.build_identity_ext(g, b"\x01" * 16, flags=0)], [], "ignored|cached"),
             ("reserved0 != 0", [L.build_identity_ext(g, None, rsv0=1)], [], "malformed IDENTITY_EXT ignored \\(reserved"),
             ("reserved1 != 0", [L.build_identity_ext(g, None, rsv1=1)], [], "malformed IDENTITY_EXT ignored \\(reserved"),
             ("undefined flag bit", [L.build_identity_ext(g, None, flags=0x03)], [], "malformed IDENTITY_EXT ignored \\(reserved"),
             ("token_present 2", [L.build_identity_ext(g, None, token_present=2)], [], "malformed IDENTITY_EXT ignored \\(reserved"),
             ("token bytes without token_present", [L.build_identity_ext(g, b"\x07" * 16, token_present=0)], [], "token bytes present without token_present"),
             ("wrong size", [L.build_identity_ext(g, None)[:-1]], [], "malformed IDENTITY_EXT ignored \\(wrong size"),
             ("AFTER the IDENTITY", [], [L.build_identity_ext(g, None)], "")]  # the host already refused + closed: the late EXT is dropped silently
    for name, pre, post, rx in cases:
        off = len(run.log())
        cl = run.guest("X-" + name[:6], g, guest_send_ext=False)
        m = run.manual(cl, pre=pre, post=post, timeout=4.0)
        cl.close()
        L.pump_sleep(0.3)
        ck("P EXT %s: the claim is not honoured -> the unknown identity is refused NO_SAVE and no guest slot is created (%s)" % (name, rx),
           m is not None and m.msg_type == L.PC_NETGAME_MSG_REJECT and m.game.reason == L.PC_NETGAME_REJECT_NO_SAVE and re.search(rx, run.log()[off:]) is not None
           and "bound to GUEST" not in run.log()[off:])
    # ---- consistency / home land checks ----
    off = len(run.log())
    bad_id = run.guest("X-ident", gid(4))
    bad_id.guest = gid(4)
    m = run.manual(bad_id, pre=[L.build_identity_ext(gid(5), None)], timeout=4.0)  # EXT says GUESTF, IDENTITY says GUESTE
    bad_id.close()
    ck("P IDENTITY name / id that differ from the EXT home PersonalID are refused", m is not None and m.msg_type == L.PC_NETGAME_MSG_REJECT
       and "differ from the guest's home PersonalID" in run.log()[off:])
    off = len(run.log())
    same_land = L.GuestIdentity(b"SAMELAND", 0x4A33, land_name, land_id)
    c, rej = run.attempt(run.guest("X-land", same_land))
    ck("P a guest whose HOME land equals this town's land is refused", c is None and rej is not None and "home land is this town's land" in run.log()[off:])
    # ---- the live guest E is still fine, no stray slot ----
    ck("P no extra guest slot was consumed by any refused claim (only slot 0 exists)", len(set(re.findall(r"bound to GUEST slot (\d+)", run.log()))) == 1 and e.is_connected())


def s_transactions(run, a, tok):
    ck = run.check
    g = gid(0)
    b_g = gid(1)
    b = run.guest("B", b_g)
    b.connect_and_ready(quiet=True)
    ck("X a second guest gets slot 1 (first contact) while guest A is live", b.token_msgs and b.token_msgs[0][1].guest_slot == 1 and b.token_msgs[0][1].flags == 1)
    # ---- pickup (pocket + money bag), drop, bury on the guest record ----
    res = TP.t1_success_paths(run, a, b)
    ck("X pickup POCKET / drop / pickup WALLET (money bag) / bury ran against guest records (TP T1 matrix, guests as actor and observer)", res is not None)
    # ---- DIG_BURIED grant ----
    pre = a.txn_pre_image()
    slot = first_free(pre[0])
    base = a.rec_last
    a.claim_position(*L.tile_center(*BURIED_TILE))
    rid = run.fresh_rid()
    sent = a.send_fa_grant(FA_BURIED, BURIED_TILE[0], BURIED_TILE[1], rid)
    r = a.wait_txn_result(sent.seq, 3.0)
    run.tx_ok("X DIG_BURIED grant by a guest", r, APPLIED, R["NONE"])
    ck("X the dig grant went into the guest record: item in the chosen slot, rev + 1", r is not None and r.dest == D_POCKET and r.slot == slot and r.item == ITM_APPLE
       and r.rev == base[2] + 1 and r.post_pockets[slot] == ITM_APPLE)
    # ---- shop buy / sell ----
    sa = a.wait_ts_state(SVC_SHOP, 6.0, min_seq=1)
    ck("X the shop mirror reaches a guest (TOWN_SVC_STATE service 3)", sa is not None)
    if sa is not None:
        cand = SH.listed(run, sa[1])
        ck("X the stock offers a buyable item", len(cand) >= 1)
        if cand:
            i1, it1, p1 = cand[0]
            s1 = SH.free_slots(a, 1)[0]
            pre = SH.pre_free(a, s1, p1 + 777)
            base = a.rec_last
            rid, sent, r = SH.buy(run, a, s1, it1, i1, p1, p1 + 777, pre=pre)
            run.tx_ok("X SHOP_BUY by a guest at the host price", r, APPLIED, R["NONE"])
            ck("X the purchase wrote the GUEST record: item in the slot, wallet - host price, rev + 1", r is not None and r.post_pockets[s1] == it1
               and r.post_wallet == p1 + 777 - p1 and r.rev == base[2] + 1)
            if r is not None and r.outcome == APPLIED:
                pre = (tuple(r.post_pockets), r.post_conds, r.post_wallet)
                rid, sent, rs = SH.sell(run, a, 1 << s1, s1, it1, pre)
                run.tx_ok("X SHOP_SELL by a guest", rs, APPLIED, R["NONE"])
                ck("X the sale credited the guest wallet (price / 4) and emptied the slot", rs is not None and rs.post_pockets[s1] == 0
                   and rs.post_wallet == r.post_wallet + p1 // 4)
    # ---- police claim ----
    sp = a.wait_ts_state(SVC_POLICE, 6.0, min_seq=1)
    if sp is not None:
        items = struct.unpack("<20H", sp[1][:40])
        pidx = next((i for i, v in enumerate(items) if v != 0), None)
        ck("X the lost-and-found mirror holds the seeded items", pidx is not None)
        if pidx is not None:
            slot = first_free(a.txn_pre_image()[0])
            base = a.rec_last
            sent, r = a.txn_claim(slot, items[pidx], pidx, rid=run.fresh_rid())
            run.tx_ok("X POLICE_CLAIM by a guest", r, APPLIED, R["NONE"])
            ck("X the claimed item is in the guest record slot, rev + 1", r is not None and r.post_pockets[slot] == items[pidx] and r.rev == base[2] + 1)
    else:
        ck("X police mirror delivered", False)
    # ---- house services are refused with NO_DONOR_SLOT and change nothing ----
    L.pump_sleep(0.3)
    rev_before = a.rec_last[2]
    off = len(run.log())
    slot = first_free(a.txn_pre_image()[0])
    _s, r = a.txn_donate(slot, ITM_APPLE, rid=run.fresh_rid())
    run.tx_ok("X MUSEUM_DONATE by a guest is refused (no donor slot)", r, REJECTED, R["NO_DONOR_SLOT"])
    ck("X ... with the guest reason in the log, rev unchanged", "has no museum donor slot" in run.log()[off:] and r is not None and r.rev == rev_before)
    s_m = a.send_txn_commit(L.PC_NETGAME_TXN_KIND_MAIL_SEND, run.fresh_rid(), L.PC_NETGAME_TXN_DEST_NONE, 0, 0)
    rm = a.wait_txn_result(s_m.seq, 3.0)
    run.tx_ok("X MAIL_SEND by a guest is refused (no house)", rm, REJECTED, R["NO_DONOR_SLOT"])
    s_t = a.send_txn_commit(L.PC_NETGAME_TXN_KIND_MAIL_TAKE, run.fresh_rid(), L.PC_NETGAME_TXN_DEST_NONE, 0, 0)
    rt = a.wait_txn_result(s_t.seq, 3.0)
    run.tx_ok("X MAIL_TAKE by a guest is refused (no house, no mailbox)", rt, REJECTED, R["NO_DONOR_SLOT"])
    ck("X ... with the guest reasons in the log, rev unchanged by all three", "a guest / extra player has no house: sending mail is refused" in run.log()[off:]
       and "has no house and no mailbox: taking mail is refused" in run.log()[off:] and rm.rev == rev_before and rt.rev == rev_before)
    L.pump_sleep(1.6)
    ck("X no MAILBOX_LETTER is ever pushed to a guest", not a.mbox_msgs() and not b.mbox_msgs())
    # ---- friendship / mail request keyed by the guest's home PersonalID ----
    mark = a.inbox.mark()
    a.send_friendship_request(0, 1)
    L.pump_sleep(0.8)
    town = L.resolve_host_town(run.ip, run.port)
    fr = [m.game for m in a.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_FRIENDSHIP_UPDATE, (L.CH_RELIABLE,)), since=mark) if m.game is not None]
    ck("X a guest's FRIENDSHIP_REQUEST is keyed by its home PersonalID: the FRIENDSHIP_UPDATE carries the guest's name / land / ids",
       any(bytes(f.player_name) == g.player_name and bytes(f.land_name) == g.land_name and f.player_id == g.player_id and f.land_id == g.land_id for f in fr))
    off = len(run.log())
    pl = L.resident_player(run.r2)
    a.send_reliable(L.build_mail_request(mail_bytes(pl, town.land_name, town.land_id)))  # forged sender: a RESIDENT
    L.pump_sleep(0.7)
    ck("X a MAIL_REQUEST with a forged (resident) sender is overwritten with the guest's bound key (logged as a GUEST)",
       re.search(r"MAIL_REQUEST sender PersonalID differs from its bound GUEST key \(guest slot 0\) -- overwritten", run.log()[off:]) is not None)
    # ---- the record the host holds == the transaction mirror: a new session of A sees it ----
    final_local, final_last = a.rec_local, a.rec_last
    run.release(a)
    a3 = run.guest("A3", g, token=tok)
    a3.connect_and_ready(quiet=True)
    push = a3.rec_pushes[-1] if a3.rec_pushes else None
    ck("X a new session of the guest is pushed the mirror: rev == the last RESULT rev, the record == the patched local image byte for byte",
       push is not None and push["rev"] == final_last[2] and push["data"] == final_local and push["rsv"] == 1)
    ck("X the host never logged an INTERNAL error", "*** INTERNAL" not in run.log())
    run.release(b)
    return a3


def s_table_full(run, a3, tok):
    ck = run.check
    run.release(a3)
    made = {}
    for i in range(2, 8):                      # guests C..H: slots 2..7 (A = 0, B = 1)
        c = run.guest("G%d" % i, gid(i))
        c.connect_and_ready(quiet=True)
        made[i] = (c.token_msgs[0][1].guest_slot, bytes(c.token_msgs[0][1].token)) if c.token_msgs else None
        run.release(c)
    ck("N guests C..H got the slots 2..7 in order, each with its own token", [made[i][0] if made[i] else None for i in range(2, 8)] == list(range(2, 8))
       and len({made[i][1] for i in range(2, 8)}) == 6)
    off = len(run.log())
    c9, rej = run.attempt(run.guest("G9", gid(8)))
    ck("N the 9th guest is REFUSED (nothing disposable: every entry is confirmed or holds accepted data): 'guest table is full', SERVER_FULL, nothing created", c9 is None and rej is not None and rej.reason == L.PC_NETGAME_REJECT_SERVER_FULL
       and "guest table is full" in run.log()[off:] and "bound to GUEST" not in run.log()[off:])
    a4 = run.guest("A4", gid(0), token=tok)
    a4.connect_and_ready(quiet=True)
    ck("N a KNOWN guest (slot 0) still reconnects with a full table", a4.identity_ack is not None and a4.token_msgs[0][1].guest_slot == 0
       and a4.token_msgs[0][1].flags == L.PC_NETGAME_IDTOKEN_FLAG_KNOWN)
    slots = [int(x) for x in re.findall(r"bound to GUEST slot (\d+)", run.log())]
    ck("N every bound guest slot is < 8 (the table size) over the whole run (%d bindings)" % len(slots), slots and max(slots) == 7 and all(0 <= s < 8 for s in slots))
    try:
        pf = parse_guests(guests_path())
        pres = [e for e in pf["e"] if e["present"]]
        ck("N guests.dat holds exactly the 8 guests with 8 UNIQUE tokens and the guests' keys", len(pres) == 8 and len({e["token"] for e in pres}) == 8
           and {e["pid"] for e in pres} == {L.guest_pid_be(gid(i)) for i in range(8)} and pf["gen"] >= 8)
    except AssertionError as exc:
        ck("N guests.dat parses (%s)" % exc, False)
    ck("N the host is alive and never logged an INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())
    run.release(a4)


def entries_of(pf, g):
    return [(i, e) for i, e in enumerate(pf["e"]) if e["present"] and e["pid"] == L.guest_pid_be(g)]


def s_m(run, seed_tok, seed_pid_guest):
    ck = run.check
    NEW, KNOWN = L.PC_NETGAME_IDTOKEN_FLAG_NEW, L.PC_NETGAME_IDTOKEN_FLAG_KNOWN
    tok = lambda c: c.token_msgs[-1][1]
    pf = parse_guests(guests_path())
    ck("M seed: guests.dat holds exactly the seeded entry of ANOTHER town (slot 0, confirmed, rev 3, town OTHERTWN) before the host's first write",
       pf["e"][0]["present"] and pf["e"][0]["confirmed"] == 1 and pf["e"][0]["rev"] == 3 and pf["e"][0]["town_land_name"] == b"OTHERTWN"
       and all(not x["present"] for x in pf["e"][1:]))
    # ---------------- M1: the table is keyed by (host town, key) ----------------
    k0 = seed_pid_guest
    off = len(run.log())
    c0 = run.guest("M-K0", k0, token=seed_tok)            # presents the token stored for the OTHER town
    c0.connect_and_ready(quiet=True)
    t0 = tok(c0)
    ck("M1 the same key presenting the token of ANOTHER town's entry is a FIRST CONTACT here: IDENTITY_TOKEN NEW in slot 1 with a different token "
       "(the other town's entry is inactive, never matched)",
       t0.flags == NEW and t0.guest_slot == 1 and bytes(t0.token) != seed_tok
       and "presented a guest token for a key this host has no entry for" in run.log()[off:] and "first contact: token minted" in run.log()[off:])
    pf = parse_guests(guests_path())
    pids0 = entries_of(pf, k0)
    ck("M1 guests.dat: BOTH entries of the key are kept: slot 0 byte-identical in token / town / rev (other town, token unchanged), slot 1 = this town with the new token",
       len(pids0) == 2 and pf["e"][0]["token"] == seed_tok and pf["e"][0]["town_land_name"] == b"OTHERTWN" and pf["e"][0]["rev"] == 3
       and pf["e"][1]["token"] == bytes(t0.token) and pf["e"][1]["town_land_name"] != b"OTHERTWN" and pf["e"][1]["confirmed"] == 0)
    run.release(c0)
    pf = parse_guests(guests_path())
    ck("M1 K0's first migration was accepted (rev 1) but the entry stays UNCONFIRMED (the client never showed the token again): data-bearing, never disposable",
       pf["e"][1]["rev"] == 1 and pf["e"][1]["confirmed"] == 0)
    # ---------------- M3: re-mint of an unconfirmed data-less entry, confirmation ----------------
    k1 = gid(21)
    c1 = run.guest("M-K1", k1, record_auto=False)
    c1.connect_and_ready(quiet=True)
    t1 = bytes(tok(c1).token)
    run.release(c1)
    pf = parse_guests(guests_path())
    e1 = entries_of(pf, k1)
    ck("M3 K1 first contact WITHOUT any record exchange: slot 2, unconfirmed, rev 0 (a data-less unconfirmed entry)", len(e1) == 1 and e1[0][0] == 2
       and e1[0][1]["confirmed"] == 0 and e1[0][1]["rev"] == 0 and e1[0][1]["token"] == t1)
    off = len(run.log())
    c1b = run.guest("M-K1b", k1, record_auto=False)       # the TOKEN message was lost / never stored: no token
    c1b.connect_and_ready(quiet=True)
    t1b = tok(c1b)
    ck("M3 K1 WITHOUT a token gets a RE-MINTED token (not a lock-out): NEW, SAME slot 2, a different token; logged",
       t1b.flags == NEW and t1b.guest_slot == 2 and bytes(t1b.token) != t1 and "unconfirmed entry: token re-minted" in run.log()[off:])
    run.release(c1b)
    c1c = run.guest("M-K1c", k1, token=bytes(t1b.token), record_auto=False)
    c1c.connect_and_ready(quiet=True)
    L.pump_sleep(0.5)
    ck("M3 K1 presenting the re-minted token: KNOWN, slot 2, the same token", tok(c1c).flags == KNOWN and tok(c1c).guest_slot == 2 and bytes(tok(c1c).token) == bytes(t1b.token))
    pf = parse_guests(guests_path())
    e1 = entries_of(pf, k1)
    ck("M3 ... and its first record step CONFIRMED the entry (guests.dat confirmed = 1, token = the re-minted one)", len(e1) == 1 and e1[0][1]["confirmed"] == 1
       and e1[0][1]["token"] == bytes(t1b.token) and "guest slot 2 CONFIRMED" in run.log())
    run.release(c1c)
    off = len(run.log())
    r_old, rej_old = run.attempt(run.guest("M-K1-old", k1, token=t1))
    r_none, rej_none = run.attempt(run.guest("M-K1-none", k1))
    ck("M3 a CONFIRMED entry is never re-minted: the OLD token and no token are both refused (WRONG token / WITHOUT a token)", r_old is None and r_none is None
       and rej_old is not None and rej_none is not None and "presented a WRONG token" in run.log()[off:] and "presented WITHOUT a token" in run.log()[off:]
       and "re-minted" not in run.log()[off:])
    # ---------------- L5: a squatter WITHOUT the token against a SILENT LIVE guest ----------------
    k2 = gid(22)
    c2 = run.guest("M-K2", k2, record_auto=False)
    c2.connect_and_ready(quiet=True)
    t2 = bytes(tok(c2).token)
    c2.go_silent()
    L.pump_sleep(3.2)                                      # idle > 2500 ms: a duplicate WITH the token would evict it
    off = len(run.log())
    sq, rej_sq = run.attempt(run.guest("M-squat", k2))
    sq2, rej_sq2 = run.attempt(run.guest("M-squat2", k2, token=bytes(b ^ 0x33 for b in t2)))
    lg = run.log()[off:]
    ck("L5 a squatter WITHOUT the token (and one with a WRONG token) targeting a SILENT LIVE (unconfirmed, data-less) guest is REFUSED (SERVER_FULL) -- no re-mint",
       sq is None and sq2 is None and rej_sq is not None and rej_sq.reason == L.PC_NETGAME_REJECT_SERVER_FULL and rej_sq2 is not None
       and "presented WITHOUT a token" in lg and "presented a WRONG token" in lg and "re-minted" not in lg)
    c2.resume()
    L.pump_sleep(1.0)
    pf = parse_guests(guests_path())
    e2 = entries_of(pf, k2)
    ck("L5 ... the live guest's session is UNTOUCHED: no eviction / teardown logged, no new binding, still connected, its entry unchanged (token, rev 0, unconfirmed)",
       "evicted stale peer" not in lg and "bound to GUEST" not in lg and "parked" not in lg and c2.is_connected()
       and len(e2) == 1 and e2[0][1]["token"] == t2 and e2[0][1]["rev"] == 0 and e2[0][1]["confirmed"] == 0)
    run.release(c2)
    # ---------------- M3: per-address first-contact rate limit ----------------
    RL = "127.0.9.9"
    made = []
    for i in range(3):
        c = run.guest("M-RL%d" % i, gid(23 + i), bind_ip=RL, record_auto=False)
        c.connect_and_ready(quiet=True)
        made.append(c.token_msgs[0][1].guest_slot)
        run.release(c)
    ck("M3 three new guests from ONE address are admitted (slots 4..6)", made == [4, 5, 6])
    off = len(run.log())
    c4, rej4 = run.attempt(run.guest("M-RL3", gid(26), bind_ip=RL, record_auto=False))
    pf = parse_guests(guests_path())
    ck("M3 the 4th NEW guest from the same address within 60 s is REFUSED (logged 'exceeded the first-contact limit'), nothing created",
       c4 is None and rej4 is not None and rej4.reason == L.PC_NETGAME_REJECT_SERVER_FULL and "exceeded the first-contact limit" in run.log()[off:]
       and not entries_of(pf, gid(26)) and sum(1 for x in pf["e"] if x["present"]) == 7)
    c5 = run.guest("M-RL-other", gid(26), record_auto=False)   # another address: admitted (slot 7), the table is now full
    c5.connect_and_ready(quiet=True)
    ck("M3 the same key from ANOTHER address is admitted (slot 7): the limit is per address", c5.token_msgs[0][1].guest_slot == 7)
    run.release(c5)
    # ---------------- M3: table full -> the OLDEST unconfirmed data-less idle entry is evicted ----------------
    pf = parse_guests(guests_path())
    ages = {i: pf["e"][i]["age"] for i in range(8)}
    ck("M3 the table is full (8 entries); K2 (slot 3) is the OLDEST unconfirmed data-less entry (age %s)" % ages,
       all(x["present"] for x in pf["e"]) and ages[3] < min(ages[4], ages[5], ages[6], ages[7]))
    off = len(run.log())
    c7 = run.guest("M-K7", gid(27), record_auto=False)
    c7.connect_and_ready(quiet=True)
    pf = parse_guests(guests_path())
    ck("M3 a NEW guest on a full table EVICTS the oldest disposable entry (K2, slot 3) and takes its slot; logged",
       c7.token_msgs[0][1].guest_slot == 3 and c7.token_msgs[0][1].flags == NEW and re.search(r"table full: evicting the OLDEST unconfirmed data-less idle entry \(slot 3,", run.log()[off:])
       and not entries_of(pf, k2) and len(entries_of(pf, gid(27))) == 1)
    ck("M3 the CONFIRMED entries (slot 0 other town, slot 2 K1) and the data-bearing unconfirmed K0 (slot 1) were NOT evicted",
       pf["e"][0]["present"] and pf["e"][0]["token"] == seed_tok and entries_of(pf, gid(21)) and pf["e"][2]["confirmed"] == 1 and entries_of(pf, k0) and len(entries_of(pf, k0)) == 2
       and sum(1 for x in pf["e"] if x["present"]) == 8)
    run.release(c7)
    off = len(run.log())
    c2b = run.guest("M-K2b", k2, token=t2, record_auto=False)    # the evicted guest returns with its old token: a first contact again (next-oldest evicted)
    c2b.connect_and_ready(quiet=True)
    ck("M3 the evicted guest returning with its old token is a first contact (NEW, a fresh token) -- the next-oldest disposable entry (slot 4) is evicted",
       c2b.token_msgs[0][1].flags == NEW and c2b.token_msgs[0][1].guest_slot == 4 and bytes(c2b.token_msgs[0][1].token) != t2
       and "table full: evicting the OLDEST" in run.log()[off:])
    run.release(c2b)
    ck("M the host is alive and never logged an INTERNAL error", run.host.alive() and "*** INTERNAL" not in run.log())


def phase_m(args, results, ip, snap_gci, residents, host_slot):
    """SECOND host process (fresh guests.dat seeded with an entry of ANOTHER town)."""
    import glob
    mp = os.path.join(L.GAME_BIN_DIR, SAVE_DIR_REL, "mp")
    os.makedirs(mp, exist_ok=True)
    for f in glob.glob(os.path.join(mp, "guests.dat*")):
        os.remove(f)
    kseed = gid(20)
    seed_tok = bytes((0xC0 + i * 3) & 0xFF for i in range(16))
    rec = L.guest_record(kseed)
    with open(guests_path(), "wb") as f:
        f.write(build_guests([{"pid": L.guest_pid_be(kseed), "token": seed_tok, "epoch": 0x99, "rev": 3, "age": 1, "confirmed": 1,
                               "town_name": b"OTHERTWN", "town_id": 0x7777, "town_hash": 0x12345678, "record": rec}], gen=5))
    host, ok = TP.start_host(ip, args.port + 1, "guestm", HOST_EXTRA, results)
    run = GRun(ip, args.port + 1, host, results, snap_gci, host_slot, residents[:3])
    try:
        if ok:
            s_m(run, seed_tok, kseed)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("guest protocol phase M raised %r" % exc, False, results)
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        L.check("[M] host did not crash (exit code before stop: %s)" % rc, rc is None, results)


def run_main(args, results, ip, snap_gci):
    port = args.port
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    L.check("fixture has >= 3 non-host residents (slots %s)" % residents, len(residents) >= 3, results)
    if len(residents) < 3:
        return
    s_wire(results)
    host, ok = TP.start_host(ip, port, "guest", HOST_EXTRA, results)
    run = GRun(ip, port, host, results, snap_gci, host_slot, residents[:3])
    try:
        if not ok:
            return
        a, exp, tok, sess, epoch = s_first_contact(run)
        a2, exp2 = s_second(run, a, exp, tok, sess)
        s_tokens(run, a2, tok)
        e = s_dup(run, a2, tok, exp2)
        s_resident_priority(run, e, tok)
        a3 = s_transactions(run, e, tok)
        s_table_full(run, a3, tok)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("guest protocol run raised %r" % exc, False, results)
    finally:
        for cl in run.clients:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
        rc = host.stop()
        L.check("host did not crash (exit code before stop: %s)" % rc, rc is None, results)
    phase_m(args, results, ip, snap_gci, residents, host_slot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11400)
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
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "guest_protocol_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        run_main(args, results, ip, snap_gci)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
