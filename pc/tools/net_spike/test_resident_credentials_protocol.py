#!/usr/bin/env python3
"""test_resident_credentials_protocol.py - M-D (admission refactor) + M-E (resident credentials), REAL DEDICATED HOSTS + scripted FakeClients + the stdin console.

TIER: scripted FakeClients against REAL `AnimalCrossing.exe --host <port> --dedicated` processes (stdin pipe) started from a DISPOSABLE fixture copy
pc\\build64\\bin_fixture4_resid (a fresh copy of the read-only bin_fixture4; NET_SPIKE_GAME_BIN points at it; its save/mp is emptied first). The live save dir, bin_talkfix*
and bin_fixture4 itself are never used. No real game client (the real client half is covered by the source audit + the next manual / real run), no visual check.

PHASES (each one a real host process on the SAME disposable save/mp dir):
  P1  resident_tokens = off (the DEFAULT): a resident WITHOUT the claim and one WITH a RESIDENT IDENTITY_EXT are both admitted exactly as before: READY, NO IDENTITY_TOKEN, no
      [NET][RESIDENT] host line, members.dat never created; `residents` lists them with credential=no; a first guest is admitted with a NEW token (the guest path is unchanged)
  P2  tofu: first claim of resident 1 mints (IDENTITY_TOKEN flags RESIDENT|NEW = 0x05, slot 1, table_size 4), members.dat is written BEFORE it is sent (1316 B, the entry holds the
      token, an independent Python parser), reconnect with the token -> KNOWN (0x06) and the entry is CONFIRMED; a WRONG token / NO token with the claim / a legacy client (no EXT)
      -> REJECT reason 5 (8-byte form, protocol 8); an impostor with a wrong token cannot evict the LIVE peer; a mismatching EXT home PID is refused; a legacy client of a slot with NO
      credential (resident 2) is admitted without a mint (log line) and nothing is written; `residents` never prints a token; the operator tools: resident-arm refused (not required),
      resident-reset refused while bound / without `confirm` / unknown selector, with `confirm` it backs members.dat up byte-identically (members.dat.bak-<ts>) and deletes the
      credential; the old token is then refused ("a token for a resident this host has no credential for"), a client that dropped it mints a NEW (different) token
  P3  host RESTART (tofu): the credential persisted: the client's token -> KNOWN (a new token is NOT minted)
  P4  required (fresh members state): a resident claim without arm is REJECT 5; `resident-arm 1 confirm` arms (no write); the next claim mints ONE credential (arm consumed: `residents`
      says armed=no), a second claim without token and another resident are refused again; arming a resident that has a credential is refused; arming a legacy client's slot: the
      legacy client (no EXT) is refused (it cannot receive a token)
  P5  corrupt members.dat (garbage bytes) + tofu: the host does not crash, UNTRUSTED is logged, the bad file is PRESERVED (moved to .corrupt-<ts>, byte-identical), residents are
      admitted legacy-style (no token, nothing written, no new members.dat), `resident-reset` is refused (UNTRUSTED)
  P6  the same state + required: UNTRUSTED => EVERY resident is refused (reason 5); the preserved file is untouched
  P7  allow_new_guests = 0 (`--allow-new-guests 0`): a NEW guest key is refused SERVER_FULL (reason 2) with the log line and no guests.dat entry; the guest admitted in P1 (known key + its
      token) still returns KNOWN; residents are unaffected
Usage: python test_resident_credentials_protocol.py [--port 12900]
"""
import argparse
import glob
import hashlib
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

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_resid")
if __name__ == "__main__":
    T.make_fixture("bin_fixture4_resid")

import net_spike_lib as L  # noqa: E402

IP = "127.0.0.1"
REJ = L.PC_NETGAME_REJECT_RESIDENT_CREDENTIAL
FULL = L.PC_NETGAME_REJECT_SERVER_FULL
RES, NEW, KNOWN = L.PC_NETGAME_IDTOKEN_FLAG_RESIDENT, L.PC_NETGAME_IDTOKEN_FLAG_NEW, L.PC_NETGAME_IDTOKEN_FLAG_KNOWN
MBR_SIZE = 32 + 16 * 80 + 4


def mp_dir():
    return os.path.join(L.GAME_BIN_DIR, "save", "mp")


def members_path():
    return os.path.join(mp_dir(), "members.dat")


def parse_members(path):
    """INDEPENDENT parser of members.dat v1 (header 32, 16 x 80, crc32 trailer). Returns {'gen', 'e': [present entries as dicts]}; asserts on any violation."""
    with open(path, "rb") as f:
        b = f.read()
    assert len(b) == MBR_SIZE, "size %d != %d" % (len(b), MBR_SIZE)
    assert b[:8] == b"ACMPMBR\x00"
    ver, flags, slots, esize, gen, rsv = struct.unpack_from("<IIIIII", b, 8)
    assert (ver, flags, slots, esize, rsv) == (1, 0, 16, 80, 0)
    assert struct.unpack_from("<I", b, len(b) - 4)[0] == zlib.crc32(b[:-4]) & 0xFFFFFFFF
    ents = []
    for i in range(16):
        o = 32 + i * 80
        if b[o] == 0:
            assert not any(b[o:o + 80])
            continue
        ents.append(dict(i=i, confirmed=b[o + 1], slot=b[o + 2], kind=b[o + 3], land=b[o + 4:o + 12], land_id=struct.unpack_from("<H", b, o + 12)[0],
                         hash=struct.unpack_from("<I", b, o + 16)[0], pid=b[o + 20:o + 40], token=b[o + 40:o + 56], age=struct.unpack_from("<I", b, o + 56)[0],
                         aux=b[o + 60:o + 80]))
    return dict(gen=gen, e=ents)


def raw(path):
    with open(path, "rb") as f:
        return f.read()


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


class Rig:
    _n = [0]

    def __init__(self, results, log_dir):
        self.results, self.log_dir = results, log_dir
        self.ck = lambda d, c: L.check(d, bool(c), results)
        self.clients = []
        self.port = None

    def start(self, tag, port, extra=()):
        h = L.HostProcess(port=port, extra_args=["--dedicated"] + list(extra), log_path=os.path.join(self.log_dir, "resid_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR,
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
        return h.wait_exit(45.0)

    def _client(self, label, slot=None, guest=None, claim=False, token=None, record=None):
        Rig._n[0] += 1
        # a resident sends RECORD_HELLO (the host closes a peer that sends none within 5 s: a LIVE resident must stay connected while the console commands run); a guest does not need to
        if record is None:
            record = guest is None
        kw = dict(bind_ip="127.0.%d.%d" % (40 + Rig._n[0] // 200, 2 + Rig._n[0] % 200), record_hello=record, record_wait=False, wait_snapshot=False)
        if guest is not None:
            c = L.FakeClient(label, IP, self.port, guest=guest, guest_token=token, **kw)
        else:
            c = L.FakeClient(label, IP, self.port, player=L.resident_player(slot), resident_claim=claim, resident_token=token, **kw)
            c.rec_resident_idx = slot
        self.clients.append(c)
        return c

    def attempt(self, label, slot=None, guest=None, claim=False, token=None, timeout=6.0):
        """-> (client or None, reject or None). The client is READY (and has pumped 0.8 s so an IDENTITY_TOKEN is in) on success."""
        c = self._client(label, slot, guest, claim, token)
        try:
            c.connect_and_ready(timeout=timeout, quiet=True)
            L.pump_sleep(0.8)
            return c, None
        except L.HandshakeRejected as e:
            return None, e.reject

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


def tok_msgs(c):
    return [g for _conn, g in c.token_msgs]


def p1(rig, args):
    ck = rig.ck
    h, ok = rig.start("p1", args.port)
    if not ok:
        return h
    try:
        off = len(h.log_text())
        c1, rj = rig.attempt("p1-r1-legacy", slot=1)
        ck("P1 [off] resident 1 WITHOUT any claim is admitted (READY) exactly as before", c1 is not None and rj is None)
        ck("P1 [off] no IDENTITY_TOKEN is sent to a legacy resident", c1 is not None and tok_msgs(c1) == [])
        c2, rj = rig.attempt("p1-r2-claim", slot=2, claim=True)
        ck("P1 [off] resident 2 WITH a RESIDENT IDENTITY_EXT is admitted too (the flag is parsed and ignored)", c2 is not None and rj is None)
        ck("P1 [off] ... and gets NO IDENTITY_TOKEN, nothing is minted", c2 is not None and tok_msgs(c2) == [])
        t = h.log_text()[off:]
        ck("P1 [off] host log: bound to resident 1 / 2 (host-derived), the claim was cached and ignored", "bound to resident 1 (host-derived)" in t and "bound to resident 2 (host-derived)" in t
           and "IDENTITY_EXT cached (resident claim" in t)
        ck("P1 [off] host log: NO [NET][RESIDENT] host line, no members file load, no credential line", "[NET][RESIDENT]" not in t)
        ck("P1 [off] save/mp/members.dat does not exist (the file is never read or written under off)", not os.path.exists(members_path()))
        out = say(h, "residents")
        ck("P1 [off] `residents` lists slots 1 and 2 as connected, resident_tokens=off, credential=no", "resident_tokens=off" in out and re.search(r"slot 1: .* credential=no .* connected=yes", out) is not None
           and re.search(r"slot 2: .* credential=no .* connected=yes", out) is not None)
        rig.release(c1)
        rig.release(c2)
        # a first guest: the guest path is unchanged
        gA = L.guest_identity("GUESTA", 0x4A01)
        cg, rj = rig.attempt("p1-guestA", guest=gA)
        ck("P1 [off] a first guest is admitted with a NEW guest token (flags NEW only, no RESIDENT bit), slot 0, table 8",
           cg is not None and len(tok_msgs(cg)) == 1 and tok_msgs(cg)[0].flags == NEW and tok_msgs(cg)[0].guest_slot == 0 and tok_msgs(cg)[0].table_size == 8)
        if cg is not None:
            rig.guest_token = bytes(cg.guest_token) if cg.guest_token else bytes(tok_msgs(cg)[0].token)
        rig.release(cg)
        ck("P1 [off] guests.dat exists (the guest was minted), members.dat still does not", os.path.exists(os.path.join(mp_dir(), "guests.dat")) and not os.path.exists(members_path()))
        rc = rig.stop(h)
        ck("P1 `stop` exits 0", rc == 0)
    finally:
        rig.closeall()
    return h


def p2(rig, args):
    ck = rig.ck
    h, ok = rig.start("p2", args.port + 1, ["--resident-tokens", "tofu"])
    if not ok:
        return None
    tok1 = None
    try:
        t0 = len(h.log_text())
        ck("P2 [tofu] host log: the members file was loaded at world ready (MISSING, resident_tokens=tofu)", "members file 'save/mp/members.dat' load mode=MISSING" in h.log_text()
           and "resident_tokens=tofu" in h.log_text())
        # --- first claim: mint
        c1, rj = rig.attempt("p2-r1-first", slot=1, claim=True)
        ck("P2 [tofu] the FIRST claim of resident 1 is admitted", c1 is not None and rj is None)
        tm = tok_msgs(c1) if c1 else []
        ck("P2 [tofu] it receives exactly one IDENTITY_TOKEN: flags RESIDENT|NEW (0x05), resident index 1, table_size 4, a non-zero 16-byte token",
           len(tm) == 1 and tm[0].flags == (RES | NEW) and tm[0].guest_slot == 1 and tm[0].table_size == 4 and any(bytes(tm[0].token)))
        tok1 = bytes(tm[0].token) if tm else None
        ck("P2 [tofu] the FakeClient stored the token (the test double of save/mp/resident_token.dat)", c1 is not None and c1.resident_token == tok1)
        ck("P2 [tofu] members.dat exists, 1316 B, valid CRC (independent parser)", os.path.exists(members_path()) and len(raw(members_path())) == MBR_SIZE)
        mem = parse_members(members_path()) if os.path.exists(members_path()) else dict(e=[], gen=0)
        e = mem["e"][0] if mem["e"] else None
        r1 = L.resident_player(1)
        ck("P2 [tofu] exactly one entry: kind RESIDENT_TOKEN, res_slot 1, unconfirmed, the entry holds the SAME token that was sent (written before it was sent), pid = the resident's PersonalID",
           len(mem["e"]) == 1 and e["kind"] == 1 and e["slot"] == 1 and e["confirmed"] == 0 and e["token"] == tok1 and e["pid"][:8] == bytes(r1.player_name)[:8].ljust(8, b"\x00")
           and struct.unpack(">H", e["pid"][16:18])[0] == r1.player_id)
        gen_after_mint = mem["gen"]
        t = h.log_text()[t0:]
        ck("P2 [tofu] host log: members.dat written (resident credential minted) BEFORE the credential MINTED line; the token is never logged",
           "members.dat written (resident credential minted" in t and "credential MINTED" in t and t.index("members.dat written (resident credential minted") < t.index("credential MINTED")
           and tok1.hex() not in t.lower())
        rig.release(c1)
        # --- reconnect with the token: KNOWN + confirmed
        c1, rj = rig.attempt("p2-r1-known", slot=1, claim=True, token=tok1)
        tm = tok_msgs(c1) if c1 else []
        ck("P2 [tofu] reconnect with the token: admitted, IDENTITY_TOKEN flags RESIDENT|KNOWN (0x06), the SAME token echoed",
           c1 is not None and len(tm) == 1 and tm[0].flags == (RES | KNOWN) and bytes(tm[0].token) == tok1 and tm[0].guest_slot == 1)
        mem = parse_members(members_path())
        ck("P2 [tofu] the entry is now CONFIRMED (durable) and keeps the token; still one entry", len(mem["e"]) == 1 and mem["e"][0]["confirmed"] == 1 and mem["e"][0]["token"] == tok1)
        # --- an impostor cannot evict the LIVE peer
        t1 = len(h.log_text())
        imp, rj = rig.attempt("p2-impostor", slot=1, claim=True, token=bytes(16 * [0x77]))
        ck("P2 [tofu] an impostor with a WRONG token is REJECTED (reason 5, 8-byte form, protocol 8)", imp is None and rj is not None and rj.reason == REJ and rj.expected_protocol_version == 8)
        L.pump_sleep(1.5)
        tl = h.log_text()[t1:]
        ck("P2 [tofu] ... and it did NOT park / evict the live peer: no 'parked', no 'evicted stale peer'; the live resident 1 is still connected",
           "parked (resident" not in tl and "evicted stale peer" not in tl and c1 is not None and c1.is_connected())
        ck("P2 [tofu] host log: the refusal names the cause (WRONG token) and the token is never logged", "resident 1 has a credential and the claim presented a WRONG token" in tl
           and bytes(16 * [0x77]).hex() not in tl.lower())
        rig.release(c1)
        # --- refusals
        _c, rj = rig.attempt("p2-wrong", slot=1, claim=True, token=bytes(16 * [0x55]))
        ck("P2 [tofu] wrong token -> REJECT 5", rj is not None and rj.reason == REJ)
        _c, rj = rig.attempt("p2-notoken", slot=1, claim=True, token=None)
        ck("P2 [tofu] the claim WITHOUT a token -> REJECT 5", rj is not None and rj.reason == REJ)
        _c, rj = rig.attempt("p2-legacy", slot=1, claim=False)
        ck("P2 [tofu] a LEGACY client (no EXT) is refused once a credential exists -> REJECT 5", rj is not None and rj.reason == REJ)
        other = L.resident_player(3)
        c_bad = rig._client("p2-mismatch", slot=1, claim=True, token=tok1)
        c_bad.connect(timeout=3.0)
        c_bad.send_resident_ext(token=tok1, player=other, town=L.resolve_host_town(IP, rig.port))
        c_bad.send_identity(town=L.resolve_host_town(IP, rig.port))
        m = c_bad.wait_handshake_reply(5.0)
        ck("P2 [tofu] an EXT whose home PID differs from the IDENTITY is refused (REJECT 5): 'IDENTITY_EXT home PersonalID differs'", m is not None and m.msg_type == L.PC_NETGAME_MSG_REJECT
           and m.game.reason == REJ and "the IDENTITY_EXT home PersonalID differs from the IDENTITY" in h.log_text())
        rig.release(c_bad)
        # --- a slot without a credential, legacy client: admitted, nothing minted
        before = raw(members_path())
        t2 = len(h.log_text())
        c2, rj = rig.attempt("p2-r2-legacy", slot=2, claim=False)
        ck("P2 [tofu] a LEGACY client of resident 2 (NO credential yet) is admitted without a mint", c2 is not None and tok_msgs(c2) == [])
        ck("P2 [tofu] host log: 'resident 2 has no credential (legacy client)'; members.dat byte-identical (nothing written)",
           "resident 2 has no credential (legacy client)" in h.log_text()[t2:] and raw(members_path()) == before)
        rig.release(c2)
        # --- operator tools
        c1, rj = rig.attempt("p2-r1-bound", slot=1, claim=True, token=tok1)
        out = say(h, "residents")
        ck("P2 [tofu] `residents`: credential=yes confirmed=yes connected=yes for slot 1, credential=no for slot 2; no token anywhere in the output",
           re.search(r"slot 1: .* credential=yes confirmed=yes armed=no connected=yes", out) is not None and re.search(r"slot 2: .* credential=no", out) is not None
           and tok1.hex() not in out.lower() and "resident_tokens=tofu" in out)
        out = say(h, "resident-arm 2 confirm")
        ck("P2 [tofu] resident-arm is refused (only needed under required) and nothing changes", "only needed under resident_tokens=required" in out and raw(members_path()) == before)
        out = say(h, "resident-reset 1 confirm")
        ck("P2 [tofu] resident-reset is REFUSED while the resident is connected (no backup, members.dat byte-identical)", "is connected on peer" in out and raw(members_path()) == before
           and not glob.glob(members_path() + ".bak-*"))
        rig.release(c1)
        out = say(h, "resident-reset 1")
        ck("P2 [tofu] without `confirm` nothing changes (explains what would happen)", "re-run with `confirm`" in out and raw(members_path()) == before and not glob.glob(members_path() + ".bak-*"))
        out = say(h, "resident-reset 9 confirm")
        ck("P2 [tofu] an unknown selector is refused", "refused" in out and raw(members_path()) == before)
        out = say(h, "resident-reset 2 confirm")
        ck("P2 [tofu] resident-reset of a resident WITHOUT a credential is refused (nothing to reset)", "has no credential" in out and raw(members_path()) == before)
        out = say(h, "resident-reset 1 extra confirm")
        ck("P2 [tofu] extra arguments are refused", "expected exactly" in out and raw(members_path()) == before)
        out = say(h, "resident-reset 1 confirm")
        baks = glob.glob(members_path() + ".bak-*")
        ck("P2 [tofu] resident-reset 1 confirm: backs up members.dat FIRST (byte-identical members.dat.bak-<ts>), then deletes the credential",
           "credential DELETED" in out and len(baks) == 1 and raw(baks[0]) == before and parse_members(members_path())["e"] == [])
        _c, rj = rig.attempt("p2-oldtoken", slot=1, claim=True, token=tok1)
        ck("P2 [tofu] after the reset the OLD token is refused (a token for a resident the host has no credential for; nothing is minted over it)", rj is not None and rj.reason == REJ
           and "the client presents a token but this host has no credential" in h.log_text())
        c1, rj = rig.attempt("p2-r1-new", slot=1, claim=True, token=None)
        tm = tok_msgs(c1) if c1 else []
        tok1b = bytes(tm[0].token) if tm else None
        ck("P2 [tofu] a client that dropped its token mints a NEW credential (flags RESIDENT|NEW, a DIFFERENT token)", c1 is not None and len(tm) == 1 and tm[0].flags == (RES | NEW) and tok1b != tok1)
        ck("P2 [tofu] members.dat holds exactly the new token", [x["token"] for x in parse_members(members_path())["e"]] == [tok1b])
        rig.release(c1)
        rig.tok1 = tok1b
        rc = rig.stop(h)
        ck("P2 `stop` exits 0", rc == 0)
    finally:
        rig.closeall()
    return h


def p3(rig, args):
    ck = rig.ck
    h, ok = rig.start("p3", args.port + 2, ["--resident-tokens", "tofu"])
    if not ok:
        return None
    try:
        ck("P3 [tofu, restarted host] the credential was RESTORED from members.dat (load mode OK, 1 resident credential)", "load mode=OK" in h.log_text() and "1 resident credential(s) restored" in h.log_text())
        before = raw(members_path())
        c1, rj = rig.attempt("p3-r1", slot=1, claim=True, token=rig.tok1)
        tm = tok_msgs(c1) if c1 else []
        ck("P3 the client's token is KNOWN after the host restart (flags RESIDENT|KNOWN, the same token): nothing was minted", c1 is not None and len(tm) == 1 and tm[0].flags == (RES | KNOWN)
           and bytes(tm[0].token) == rig.tok1)
        mem = parse_members(members_path())
        ck("P3 still exactly one credential, now confirmed", len(mem["e"]) == 1 and mem["e"][0]["token"] == rig.tok1 and mem["e"][0]["confirmed"] == 1)
        rig.release(c1)
        _c, rj = rig.attempt("p3-wrong", slot=1, claim=True, token=bytes(16 * [0x11]))
        ck("P3 a wrong token is still refused after the restart (reason 5)", rj is not None and rj.reason == REJ)
        rc = rig.stop(h)
        ck("P3 `stop` exits 0", rc == 0)
    finally:
        rig.closeall()
    return h


def p4(rig, args):
    ck = rig.ck
    # fresh members state (the disposable fixture only): remove the file and its generations / backups
    for f in glob.glob(os.path.join(mp_dir(), "members.dat*")):
        os.remove(f)
    h, ok = rig.start("p4", args.port + 3, ["--resident-tokens", "required"])
    if not ok:
        return None
    try:
        ck("P4 [required] host log: resident_tokens=required, members file MISSING", "load mode=MISSING" in h.log_text() and "resident_tokens=required" in h.log_text())
        _c, rj = rig.attempt("p4-noarm", slot=1, claim=True)
        ck("P4 [required] a resident claim WITHOUT arm is refused (reason 5) and nothing is minted / written", rj is not None and rj.reason == REJ and not os.path.exists(members_path())
           and "has no credential and is not armed" in h.log_text())
        _c, rj = rig.attempt("p4-noarm-legacy", slot=2, claim=False)
        ck("P4 [required] a legacy client of a credential-less slot is refused too (reason 5)", rj is not None and rj.reason == REJ)
        out = say(h, "resident-arm 1")
        ck("P4 [required] resident-arm without `confirm` changes nothing", "re-run with `confirm`" in out and not os.path.exists(members_path()))
        out = say(h, "resident-arm 1 confirm")
        ck("P4 [required] resident-arm 1 confirm: ARMED (10 minutes, one use); NO file is written (memory only)", "ARMED for 10 minutes" in out and not os.path.exists(members_path()))
        out = say(h, "residents")
        ck("P4 [required] `residents` shows armed=yes for slot 1 only", re.search(r"slot 1: .* armed=yes", out) is not None and re.search(r"slot 2: .* armed=no", out) is not None
           and "resident_tokens=required" in out)
        c1, rj = rig.attempt("p4-mint", slot=1, claim=True)
        tm = tok_msgs(c1) if c1 else []
        tok = bytes(tm[0].token) if tm else None
        ck("P4 [required] the armed resident's claim mints ONE credential: IDENTITY_TOKEN RESIDENT|NEW, members.dat has exactly 1 entry", c1 is not None and len(tm) == 1 and tm[0].flags == (RES | NEW)
           and [x["token"] for x in parse_members(members_path())["e"]] == [tok])
        out = say(h, "residents")
        ck("P4 [required] the arm was CONSUMED (armed=no) by the one mint", re.search(r"slot 1: .* credential=yes .* armed=no", out) is not None)
        rig.release(c1)
        _c, rj = rig.attempt("p4-again", slot=1, claim=True, token=None)
        ck("P4 [required] a second claim of resident 1 without the token is refused (exactly one mint)", rj is not None and rj.reason == REJ and len(parse_members(members_path())["e"]) == 1)
        _c, rj = rig.attempt("p4-other", slot=2, claim=True)
        ck("P4 [required] another (un-armed) resident is refused", rj is not None and rj.reason == REJ and len(parse_members(members_path())["e"]) == 1)
        c1, rj = rig.attempt("p4-known", slot=1, claim=True, token=tok)
        tm = tok_msgs(c1) if c1 else []
        ck("P4 [required] resident 1 with its token is KNOWN (RESIDENT|KNOWN)", c1 is not None and len(tm) == 1 and tm[0].flags == (RES | KNOWN))
        rig.release(c1)
        out = say(h, "resident-arm 1 confirm")
        ck("P4 [required] arming a resident that already has a credential is refused", "already has a credential" in out)
        out = say(h, "resident-arm 2 confirm")
        ck("P4 [required] resident-arm 2 confirm armed", "ARMED" in out)
        _c, rj = rig.attempt("p4-armed-legacy", slot=2, claim=False)
        ck("P4 [required] an armed slot refuses a LEGACY client (no EXT: it cannot receive a token) and the arm stays", rj is not None and rj.reason == REJ
           and "it cannot receive a credential" in h.log_text() and "armed=yes" in say(h, "residents"))
        rc = rig.stop(h)
        ck("P4 `stop` exits 0", rc == 0)
    finally:
        rig.closeall()
    return h


def p56(rig, args):
    ck = rig.ck
    for f in glob.glob(os.path.join(mp_dir(), "members.dat*")):
        os.remove(f)
    garbage = b"THIS IS NOT A MEMBERS FILE" * 3
    with open(members_path(), "wb") as f:
        f.write(garbage)
    h, ok = rig.start("p5", args.port + 4, ["--resident-tokens", "tofu"])
    if ok:
        try:
            t = h.log_text()
            ck("P5 [tofu] host survived a corrupt members.dat: UNTRUSTED mode logged, the bad file preserved", "UNTRUSTED MODE" in t and "load mode=UNTRUSTED" in t)
            kept = glob.glob(members_path() + ".corrupt-*")
            ck("P5 [tofu] the corrupt file was MOVED to members.dat.corrupt-<ts> byte-identically; no members.dat is left", len(kept) == 1 and raw(kept[0]) == garbage and not os.path.exists(members_path()))
            c1, rj = rig.attempt("p5-r1", slot=1, claim=True)
            ck("P5 [tofu, UNTRUSTED] a resident is admitted legacy-style: READY, NO IDENTITY_TOKEN, nothing minted", c1 is not None and tok_msgs(c1) == [])
            ck("P5 [tofu, UNTRUSTED] host log: 'members.dat is UNTRUSTED: resident 1 is admitted legacy-style'; NO new members.dat was written",
               "members.dat is UNTRUSTED: resident 1 is admitted legacy-style" in h.log_text() and not os.path.exists(members_path()))
            rig.release(c1)
            out = say(h, "resident-reset 1 confirm")
            ck("P5 [UNTRUSTED] resident-reset is REFUSED (members.dat is UNTRUSTED) and nothing is written", "members.dat is UNTRUSTED" in out and not os.path.exists(members_path()))
            out = say(h, "residents")
            ck("P5 [UNTRUSTED] `residents` says UNTRUSTED", "members.dat is UNTRUSTED" in out and "unknown (UNTRUSTED)" in out)
            rc = rig.stop(h)
            ck("P5 `stop` exits 0 and still no members.dat", rc == 0 and not os.path.exists(members_path()))
        finally:
            rig.closeall()
    # P6: same state, required
    h, ok = rig.start("p6", args.port + 5, ["--resident-tokens", "required"])
    if ok:
        try:
            ck("P6 [required] only the preserved *.corrupt-* file remains: the load is UNTRUSTED (sticky), not MISSING", "load mode=UNTRUSTED" in h.log_text())
            _c, rj = rig.attempt("p6-r1", slot=1, claim=True)
            ck("P6 [required, UNTRUSTED] EVERY resident is refused (reason 5)", rj is not None and rj.reason == REJ and "members.dat is UNTRUSTED" in h.log_text())
            _c, rj = rig.attempt("p6-r2", slot=2, claim=False)
            ck("P6 [required, UNTRUSTED] a legacy resident is refused too", rj is not None and rj.reason == REJ)
            ck("P6 the preserved file is untouched and no members.dat appeared", len(glob.glob(members_path() + ".corrupt-*")) == 1 and not os.path.exists(members_path()))
            rc = rig.stop(h)
            ck("P6 `stop` exits 0", rc == 0)
        finally:
            rig.closeall()


def p7(rig, args):
    ck = rig.ck
    for f in glob.glob(os.path.join(mp_dir(), "members.dat*")):
        os.remove(f)
    h, ok = rig.start("p7", args.port + 6, ["--allow-new-guests", "0"])
    if not ok:
        return
    try:
        gB = L.guest_identity("GUESTB", 0x4A02)
        before = raw(os.path.join(mp_dir(), "guests.dat"))
        t0 = len(h.log_text())
        c, rj = rig.attempt("p7-newguest", guest=gB)
        ck("P7 [allow_new_guests=0] a NEW guest key is REFUSED with reason 2 SERVER_FULL (8-byte form)", c is None and rj is not None and rj.reason == FULL)
        tl = h.log_text()[t0:]
        ck("P7 host log: 'new guest key refused (allow_new_guests=0' and the REFUSED line", "new guest key refused (allow_new_guests=0" in tl and "new guests are not accepted on this host" in tl)
        ck("P7 guests.dat is byte-identical (nothing minted / created for the refused key)", raw(os.path.join(mp_dir(), "guests.dat")) == before)
        gA = L.guest_identity("GUESTA", 0x4A01)
        cg, rj = rig.attempt("p7-knownguest", guest=gA, token=rig.guest_token)
        tm = tok_msgs(cg) if cg else []
        ck("P7 the KNOWN guest A (key + its token) still returns: IDENTITY_TOKEN KNOWN, the same token", cg is not None and len(tm) == 1 and tm[0].flags == KNOWN and bytes(tm[0].token) == rig.guest_token)
        rig.release(cg)
        c1, rj = rig.attempt("p7-resident", slot=1, claim=False)
        ck("P7 residents are unaffected (a resident is admitted)", c1 is not None)
        rig.release(c1)
        rc = rig.stop(h)
        ck("P7 `stop` exits 0", rc == 0)
    finally:
        rig.closeall()


def source_audit(rig):
    ck = rig.ck
    ng = open(os.path.join(T.PC, "src", "pc_net_game.c"), encoding="utf-8", errors="replace", newline="").read().replace("\r\n", "\n")
    st = open(os.path.join(T.PC, "src", "pc_settings.c"), encoding="utf-8", errors="replace", newline="").read().replace("\r\n", "\n")
    mn = open(os.path.join(T.PC, "src", "pc_main.c"), encoding="utf-8", errors="replace", newline="").read().replace("\r\n", "\n")
    ck("S allow_new_guests = 1 and resident_tokens = off are the DEFAULTS; settings.ini [Network] parses allow_new_guests 0|1 and resident_tokens off|tofu|required (+ writes them)",
       ".allow_new_guests = 1," in st and ".resident_tokens = 0," in st and 'strcmp(key, "allow_new_guests") == 0' in st and 'strcmp(value, "tofu") == 0' in st
       and 'strcmp(value, "required") == 0' in st and "allow_new_guests = %d" in st and 'resident_tokens = %s' in st)
    ck("S the host CLI refuses a bad / missing value (exit 2) and both options outside --host", "--allow-new-guests: REFUSED: the option needs 0 or 1" in mn and "--resident-tokens: REFUSED: the option needs off, tofu or required" in mn
       and "HOST-only options" in mn)
    ck("S process_identity calls admission_decide, applies the refusal, runs the membership CROSS-CHECK (a log line only when it disagrees), and the decide function calls classify, guest_check and "
       "the credential check in that order (classify itself is NOT replaced)",
       "pcnetgame_host_admission_decide(peer, &in, &ext, ext_valid, &adm);" in ng and "pcnetgame_host_admission_crosscheck(peer, &in, &adm);" in ng
       and ng.index("pcnetgame_host_classify_identity(in, &resident_idx)") < ng.index("pcnetgame_host_guest_check(peer, in, ext, &a->guest_key") < ng.index("pcnetgame_host_resident_credential_check(peer, resident_idx")
       and "static PCNetGameIdentityClass pcnetgame_host_classify_identity(" in ng)
    pi = ng[ng.index("static void pcnetgame_host_process_identity(PCNetPeerId peer) {"):]
    ck("S the credential check (decide) happens BEFORE the duplicate-park step and the mint after it; the mint is before the ACK, the rollback covers a failed ACK / token send",
       ng.index("pcnetgame_host_resident_credential_check(peer, resident_idx") < ng.index("static void pcnetgame_host_process_identity(PCNetPeerId peer) {")
       and pi.index("if (other >= 0) {") < pi.index("const int mr = pcnetgame_resident_mint(resident_idx, res_token);") < pi.index("pc_net_send(peer, PC_NET_RELIABLE, &ack, (uint16_t)sizeof(ack))")
       and ng.count("pcnetgame_resident_mint_rollback(resident_idx)") >= 2)
    ck("S allow_new_guests only refuses guest_mode == 0 (a NEW key) inside the guest_mode != 1 block, before the rate limiter / create",
       "if (guest_mode == 0 && !pcnetgame_host_allow_new_guests()) {" in ng and ng.index("if (guest_mode == 0 && !pcnetgame_host_allow_new_guests()) {") < ng.index("pcnetgame_guest_mint_allowed(mint_ip, mint_now)")
       < ng.index("pcnetgame_guest_create(&guest_key, &guest_slot, guest_token)"))
    ck("S policy off: the credential check returns before ANY file access (members.dat is only loaded at world ready when resident_tokens != off)",
       "if (policy == PC_NETGAME_RESTOK_OFF) {\n        return 1;" in ng and "if (pcnetgame_resident_policy() != 0) {\n        pcnetgame_members_store_load();" in ng)
    ck("S client: the resident claim gate s_client_resident_claim_sent, the DIFFERENT-token rule for residents, REJECT 5 text, token files (store character: per-town token.dat + membership.ini "
       "role=resident; legacy: save/mp/resident_token.dat)",
       "s_client_resident_claim_sent = 1;" in ng and "resident token mismatch" in ng and "resident credential missing or wrong: ask the operator for resident-reset/arm" in ng
       and 'PC_MP_MEMBERS_RESIDENT_TOKEN_PATH' in ng and '"resident"' in ng and "pc_character_membership_write(" in ng)
    ck("S the host never logs a token: no printf in the RESIDENT CREDENTIALS blocks mentions a token member",
       not [ln for blk in re.findall(r"RESIDENT CREDENTIALS HOST BEGIN.*?RESIDENT CREDENTIALS HOST END", ng, re.S) + re.findall(r"RESIDENT CREDENTIALS ADMIN.*?RESIDENT CREDENTIALS ADMIN END", ng, re.S)
            for ln in blk.splitlines() if "printf(" in ln and re.search(r"\.token|->token|token\[", ln)])


def run(args, results):
    log_dir = os.path.join(T.LOG_DIR)
    os.makedirs(log_dir, exist_ok=True)
    rig = Rig(results, log_dir)
    rig.guest_token = None
    rig.tok1 = None
    shutil.rmtree(mp_dir(), ignore_errors=True)  # the disposable fixture only
    source_audit(rig)
    p1(rig, args)
    p2(rig, args)
    if rig.tok1 is not None:
        p3(rig, args)
    p4(rig, args)
    p56(rig, args)
    if rig.guest_token is not None:
        # restore the guest table of P1 (P4..P6 removed only members.dat*, guests.dat is still there)
        p7(rig, args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12900)
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
