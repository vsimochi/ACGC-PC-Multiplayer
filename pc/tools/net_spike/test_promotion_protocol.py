#!/usr/bin/env python3
"""test_promotion_protocol.py - M-F (guest -> resident promotion, REDUCED scope: HOST side + the wire handoff), REAL DEDICATED HOSTS + scripted FakeClients + the stdin console.

TIER: scripted FakeClients against REAL `AnimalCrossing.exe --host <port> --dedicated` processes (stdin pipe) started from a DISPOSABLE fixture copy
pc\\build64\\bin_fixture4_promo (a fresh copy of the read-only bin_fixture4; NET_SPIKE_GAME_BIN points at it). The copy's town save is DERIVED here so that resident slot 3 is FREE
(private_data[3] cleared, house 3 ownerID cleared; the other three residents and houses are the fixture's). The live save dir, bin_talkfix* and bin_fixture4 are never used.
No real game client (the client half is source-audited only), no visual check.

PHASES (real host processes on the SAME disposable save dir):
  P0  host: two guests join with FRESH records (the host refuses a first MIGRATE that is not a fresh character: A = PROMOGST, female, a free face; B = OTHERGST); the host stops; A's stored
      guests.dat record is then PATCHED (pocket 0 = 0x2200, wallet 1234, bank 5678, entry + file CRCs recomputed): the state of a guest that played for a while
  P1  host (resident_tokens off, the default): A reconnects (known token), B is offline. Guard failures change NOTHING
      (guests.dat md5, no members.dat, records.dat md5, no backup file): an ONLINE guest, an unknown guest, no `confirm` (says what it would do), bad slot / house words, an occupied slot,
      an owned house, extra / unknown trailing words. Then `promote PROMOGST auto auto confirm` with A offline: result line (slot 3, house 3, never a token), guests.dat backup is
      byte-identical to the pre-image, A's guests.dat entry is GONE (B stays), members.dat (independent parser) = RESIDENT_TOKEN (unconfirmed, slot 3, the new PID) + PROMOTION_HANDOFF
      (aux = A's home PID, token = A's GUEST token, distinct from the resident token), records.dat slot 3 = rev 1 + the new PID, `residents` lists slot 3 with a credential. A reconnects
      with its guest token BEFORE anything else changed: RESIDENT_HANDOFF (66, 48 B: slot 3, the new PID, the resident token) THEN REJECT 6 PROMOTED (8 B); a wrong / missing token gets
      neither. A second promote (B) finds the town FULL and changes nothing. Graceful stop -> the saved GCI: slot 3 = the guest's name / gender / face / shirt, pockets / wallet / bank
      carried over, loan = the house price, a valid fresh record; house 3 ownerID = the new PID; residents 0..2 and houses 0..2 byte-identical; house arrangement unchanged.
  P2  host RESTART (resident_tokens tofu): the handoff persisted (66 then REJECT 6 again, same slot / PID / token); `residents` slot 3 credential=yes confirmed=no; a resident-claim client
      with the NEW PID + the handed-over token is admitted as resident 3 (IDENTITY_TOKEN KNOWN), the credential becomes CONFIRMED and the handoff entry is REMOVED; A with its guest token no longer
      gets a handoff (the name now belongs to a resident).
  P3  crash safety: a members.dat whose handoff names a resident that is NOT in the GCI (the state of a crash between the members.dat write and the town save) is IGNORED: guest B (token) is
      admitted as a guest as before (log line 'INVALID').
Usage: python test_promotion_protocol.py [--port 12961]
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

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_promo")


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
    T.make_fixture("bin_fixture4_promo")
    make_free_slot(os.path.join(T.BUILD64, "bin_fixture4_promo"))

import net_spike_lib as L  # noqa: E402

IP = "127.0.0.1"
ANIMALS_OFF = MF.MAIN_OFF + 0x17438          # Save_t.animals[15], stride 0x988 (memories[7] at +0x10, stride 0x138)
ANIMAL_STRIDE, MEM_OFF, MEM_STRIDE = 0x988, 0x10, 0x138
ISLAND_ANIMAL_OFF = MF.MAIN_OFF + 0x22540 + 0xF00  # Save_t.island.animal
MELODY_OFF = MF.MAIN_OFF + 0x20F08            # u64 BE, the town tune
REC_OFF_BIRTHDAY = 0x10A4
REC_OFF_FURN_BITS, REC_OFF_WALL_BITS, REC_OFF_CARPET_BITS = 0x1108, 0x11B4, 0x11C0
SEED_TUNE = 0x1122334455667788


def patch_gci(dest, fn):
    """Rewrites the main Save_t of the DISPOSABLE copy through fn(bytearray gci) and recomputes the checksum + backup copy like the game's writer."""
    p = os.path.join(dest, MF.GCI_REL)
    g = bytearray(open(p, "rb").read())
    fn(g)
    m = bytearray(g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE])
    m[MF.CHK_OFF:MF.CHK_OFF + 2] = struct.pack(">H", MF.checksum(bytes(m)))
    g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE] = m
    g[MF.BACK_OFF:MF.BACK_OFF + MF.SAVE_SECTOR_SIZE] = g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_SECTOR_SIZE]
    open(p, "wb").write(bytes(g))


def mem_off(animal_base, k):
    return animal_base + MEM_OFF + k * MEM_STRIDE


def free_mem_slots(g, animal_base):
    return [k for k in range(7) if bytes(g[mem_off(animal_base, k) + 16:mem_off(animal_base, k) + 20]) == bytes([255, 255, 255, 255])]


def seed_memories(dest, pid_a, pid_b):
    """Seeds villager memories in the disposable GCI: the guest A's (town villagers 0 and 1: land OLDTOWN, id 0x1234, a marker tune), guest B's (villager 0, must stay untouched) and A's on the
    ISLANDER (the memuni union holds the furniture bitfield there: must stay untouched). Returns {name: (offset, 0x138 seeded bytes)}."""
    out = {}

    def fn(g):
        def put(name, base, k, pid, land, lid, tune, island=False):
            o = mem_off(base, k)
            g[o:o + 20] = pid
            if island:
                g[o + 0x1C:o + 0x20] = struct.pack(">I", 0xAABBCCDD)
                g[o + 0x20:o + 0x22] = struct.pack(">H", 0x0123)
            else:
                g[o + 0x1C:o + 0x24] = land
                g[o + 0x24:o + 0x26] = struct.pack(">H", lid)
            g[o + 0x28:o + 0x30] = struct.pack(">Q", tune)
            g[o + 0x30] = 5  # friendship
            out[name] = (o, bytes(g[o:o + MEM_STRIDE]))
        b0 = ANIMALS_OFF
        b1 = ANIMALS_OFF + ANIMAL_STRIDE
        f0, f1, fi = free_mem_slots(g, b0), free_mem_slots(g, b1), free_mem_slots(g, ISLAND_ANIMAL_OFF)
        assert len(f0) >= 2 and len(f1) >= 1 and len(fi) >= 1, (f0, f1, fi)
        put("a0", b0, f0[0], pid_a, b"OLDTOWN ", 0x1234, SEED_TUNE)
        put("b0", b0, f0[1], pid_b, b"BTOWN   ", 0x4321, SEED_TUNE + 1)
        put("a1", b1, f1[0], pid_a, b"OLDTOWN ", 0x1234, SEED_TUNE)
        put("isl", ISLAND_ANIMAL_OFF, fi[0], pid_a, None, 0, SEED_TUNE + 2, island=True)
    patch_gci(dest, fn)
    return out

MBR_SIZE = 32 + 16 * 80 + 4
GST_ENTRY = 72 + 0x2440
GST_SIZE = 32 + 8 * GST_ENTRY + 4
REC_ENTRY = 4 + 20 + 16 + 0x2440
REC_SIZE = 32 + 4 * REC_ENTRY + 4
ARRANGEMENT_OFF = MF.MAIN_OFF + 0x2068A


def mp_dir():
    return os.path.join(L.GAME_BIN_DIR, "save", "mp")


def mpath(n):
    return os.path.join(mp_dir(), n)


def raw(path):
    with open(path, "rb") as f:
        return f.read()


def md5_or_none(path):
    return T.md5_file(path) if os.path.isfile(path) else None


def parse_members(path):
    b = raw(path)
    assert len(b) == MBR_SIZE and b[:8] == b"ACMPMBR\x00"
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
    return ents


def build_members(entries, gen=1):
    buf = bytearray(MBR_SIZE)
    buf[0:8] = b"ACMPMBR\x00"
    struct.pack_into("<IIIIII", buf, 8, 1, 0, 16, 80, gen, 0)
    for n, e in enumerate(entries):
        o = 32 + n * 80
        struct.pack_into("<BBBB8sHHI20s16sI20s", buf, o, 1, e["confirmed"], e["slot"], e["kind"], e["land"], e["land_id"], 0, e["hash"], e["pid"], e["token"], e["age"], e["aux"])
    struct.pack_into("<I", buf, MBR_SIZE - 4, zlib.crc32(bytes(buf[:-4])) & 0xFFFFFFFF)
    return bytes(buf)


def parse_guests(path):
    b = raw(path)
    assert len(b) == GST_SIZE and b[:8] == b"ACMPGST\x00"
    assert struct.unpack_from("<I", b, len(b) - 4)[0] == zlib.crc32(b[:-4]) & 0xFFFFFFFF
    out = []
    for i in range(8):
        e = b[32 + i * GST_ENTRY:32 + (i + 1) * GST_ENTRY]
        if e[0]:
            out.append(dict(slot=i, confirmed=e[1], pid=bytes(e[4:24]), token=bytes(e[24:40]), rev=struct.unpack_from("<I", e, 44)[0]))
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
    """FakeClient that records the RESIDENT_HANDOFF / REJECT messages in arrival order."""

    def __init__(self, *a, **kw):
        self.seen = []
        super().__init__(*a, **kw)

    def on_message(self, m):
        try:
            if m.channel == L.CH_RELIABLE and m.payload and m.payload[0] in (L.PC_NETGAME_MSG_RESIDENT_HANDOFF, L.PC_NETGAME_MSG_REJECT):
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
        h = L.HostProcess(port=port, extra_args=["--dedicated"] + list(extra), log_path=T.log_path("promo_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR,
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
        return "127.0.%d.%d" % (40 + Rig._n[0] // 200, 2 + Rig._n[0] % 200)

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


def parse_handoff(payload):
    t, flags, slot, rsv, pid, token, rsv2 = struct.unpack(L.RESIDENT_HANDOFF_FMT, payload)
    return dict(type=t, flags=flags, slot=slot, rsv=rsv, pid=pid, token=token, rsv2=rsv2)


def gci_priv(gci, i):
    o = MF.MAIN_OFF + MF.PRIV_OFF + i * MF.PRIV_STRIDE
    return bytes(gci[o:o + MF.PRIV_STRIDE])


def gci_home(gci, i):
    o = MF.MAIN_OFF + MF.HOME_OFF + i * MF.HOME_STRIDE
    return bytes(gci[o:o + MF.HOME_STRIDE])


def first_diff(a, b):
    return next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), None)


def source_audit(rig):
    ck = rig.ck
    ng = open(os.path.join(T.PC, "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read()
    mc = open(os.path.join(T.PC, "src", "pc_m_card.c"), encoding="utf-8", errors="replace").read()
    pf = ng[ng.index("int pc_net_game_dedicated_promote("):ng.index("/* ===== M-F END ===== */")]
    ck("S promote order: members.dat commit BEFORE the town save, the town save BEFORE the guests.dat entry removal (the entry goes LAST)",
       pf.index("pcnetgame_members_commit(&nf, \"guest promotion: resident credential + handoff\")") < pf.index("pc_save_write_authoritative()") < pf.index("memset(&s_guest[g], 0, sizeof(s_guest[g]));"))
    ck("S promote checks first: world ready, guests.dat / members.dat trusted, ONE guest of this town, offline, synced record, slot, house, unique name, members room, THEN `confirm`, THEN backups",
       [pf.index(x) for x in ("!s_host_world_ready", "s_guest_untrusted", "s_members_untrusted", "pcnetgame_dedicated_guest_resolve(", "pcnetgame_host_peer_bound_to_guest(g",
                              "has never synced", "the town is FULL", "already has an owner", "already has the name", "has no room for 2 entries", "if (!confirm) {",
                              "pc_mp_guests_backup_file(PC_MP_GUESTS_PATH")] == sorted(pf.index(x) for x in ("!s_host_world_ready", "s_guest_untrusted", "s_members_untrusted",
                              "pcnetgame_dedicated_guest_resolve(", "pcnetgame_host_peer_bound_to_guest(g", "has never synced", "the town is FULL", "already has an owner",
                              "already has the name", "has no room for 2 entries", "if (!confirm) {", "pc_mp_guests_backup_file(PC_MP_GUESTS_PATH")))
    ck("S promote never prints a token (no printf / snprintf line of the promote block or the handoff check mentions a token member)",
       not [ln for ln in pf.splitlines() if ("printf(" in ln) and re.search(r"->token|\.token|rtok|token\[", ln)])
    ho = ng[ng.index("static int pcnetgame_host_promotion_handoff(PCNetPeerId peer, const PersonalID_c* key, const PCNetGameIdentityExtMsg* ext) {"):]
    ho = ho[:ho.index("\n}\n")]
    ck("S handoff check: matches (town, aux_pid = the claimed guest key, the presented GUEST token), is VALID only when the resident PID is in private_data AND its credential entry exists, "
       "sends 66 then REJECT PROMOTED through the normal reject path",
       "memcmp(e->aux_pid, gpid" in ho and "memcmp(e->token, ext->token" in ho and "idx < 0 || ci < 0" in ho and ho.index("PC_NETGAME_MSG_RESIDENT_HANDOFF") < ho.index("PC_NETGAME_REJECT_PROMOTED")
       and "pcnetgame_host_reject_and_close(" in ho)
    ck("S the guest_check hook sits after the key validity / conflict checks and before the guest store lookup",
       ng.index("if (pcnetgame_host_promotion_handoff(peer, key, ext)) {") > ng.index("why = pcnetgame_guest_key_conflict(key);") and
       ng.index("if (pcnetgame_host_promotion_handoff(peer, key, ext)) {") < ng.index("g = pcnetgame_guest_find(key);"))
    ck("S pc_mp_promote_create (pc_m_card.c): vanilla Clear + Init, mHS_set_use, the veteran event state, a rollback snapshot; no ARAM / no catalog / no first-job",
       all(x in mc for x in ("mPr_ClearPrivateInfo(priv);\n    mPr_InitPrivateInfo(priv);", "mHS_set_use(slot, house)", "mEv_ClearPersonalEventFlag(slot);", "void pc_mp_promote_rollback(void) {",
                             "priv->inventory.loan = mPlayer_DEBT0;")) and "mEv_SetFirstJob" not in mc[mc.index("int pc_mp_promote_create("):mc.index("void pc_mp_promote_rollback(void) {")])
    ck("S client: RESIDENT_HANDOFF is honoured only after OUR guest claim and only when consistent; a store character gets token.dat + membership.ini role=resident, a legacy profile only a message; "
       "reason 6 text", "static void pcnetgame_handle_client_resident_handoff(" in ng and "!s_client_guest_claim_sent" in ng and "case PC_NETGAME_REJECT_PROMOTED:" in ng
       and "restart to join as a resident" in ng and "legacy guest profile: the token / membership files are NOT rewritten" in ng)


def source_audit_mi(rig):
    """M-I source audit: re-keyed memories + rollback of the animals, the orphan prune, the automatic relaunch wiring."""
    ck = rig.ck
    ng = open(os.path.join(T.PC, "src", "pc_net_game.c"), encoding="utf-8", errors="replace").read()
    mc = open(os.path.join(T.PC, "src", "pc_m_card.c"), encoding="utf-8", errors="replace").read()
    mm = open(os.path.join(T.PC, "src", "pc_main.c"), encoding="utf-8", errors="replace").read()
    rl = open(os.path.join(T.PC, "src", "pc_relaunch.c"), encoding="utf-8", errors="replace").read()
    vi = open(os.path.join(T.PC, "src", "pc_vi.c"), encoding="utf-8", errors="replace").read()
    cr = mc[mc.index("int pc_mp_promote_create("):mc.index("void pc_mp_promote_rollback(void) {")]
    rb = mc[mc.index("void pc_mp_promote_rollback(void) {"):mc.index("void pc_mp_promote_commit(void) {")]
    ck("S M-I: the snapshot (incl. animals[] and the island animal) is taken BEFORE anything is modified; the rollback restores both; the memories are re-keyed BEFORE the caller's save",
       cr.index("s_pc_promote_snap.animals") < cr.index("mPr_ClearPrivateInfo(priv);") and "Save_Get(animals), s_pc_promote_snap.animals" in rb and "Save_Get(island).animal = s_pc_promote_snap.island_animal" in rb
       and "mPr_CopyPersonalID(&mem->memory_player_id, &priv->player_ID)" in cr)
    ck("S M-I: the islander's memuni is NOT overwritten (the land / tune write is guarded by !is_island); mPr_SetItemCollectBit runs with now_private pointed at the new resident and restored",
       "if (!is_island) {" in cr and "Common_Set(now_private, priv);" in cr and "Common_Set(now_private, prev_now);" in cr and "mPr_SetItemCollectBit(FTR_START(FTR_SUM_CASSE01));" in cr)
    pf = ng[ng.index("int pc_net_game_dedicated_promote("):ng.index("/* ===== M-F END ===== */")]
    ck("S M-I: orphaned RESIDENT_TOKEN / PROMOTION_HANDOFF entries of this town are pruned (room check on a copy, persisted with the step-3 members.dat write)",
       "pcnetgame_members_prune_orphans(&nf)" in pf and pf.count("pcnetgame_members_prune_orphans(&nf)") == 2)
    hh = ng[ng.index("static void pcnetgame_handle_client_resident_handoff("):ng.index("static void pcnetgame_reject_text(")]
    ck("S M-I client: the relaunch flag is set only AFTER membership.ini was written (store character); REJECT 6 turns it into a pending request; take_relaunch is one-shot",
       hh.index("pc_character_membership_write(") < hh.index("s_client_promote_relaunch = 1;") and "in.reason == (uint8_t)PC_NETGAME_REJECT_PROMOTED && s_client_promote_relaunch" in ng
       and "s_client_relaunch_ready = 0;" in ng[ng.index("int pc_net_game_client_take_relaunch("):])
    po = mm[mm.index("void pc_main_relaunch_poll(void) {"):]
    po = po[:po.index("\n}\n")]
    ck("S M-I pc_main: only a --town-fetch process relaunches; g_pc_running = 0 ONLY after pc_relaunch_connect succeeded; failure shows a message; polled from pc_vi.c every frame",
       "if (!g_pc_town_fetch)" in po and po.index("pc_relaunch_connect(") < po.index("g_pc_running = 0;") and "pc_net_game_client_relaunch_failed(err)" in po
       and "pc_main_relaunch_poll();" in vi and "PC_RELAUNCH_CHARACTER" in po)
    ar = mc[mc.index("static int pc_guest_arrive("):]
    ar = ar[:ar.index("pc_guest_build_fresh_record(pass")]
    ck("S M-I pc_guest_arrive: a name clash with a resident is still REFUSED unless this character holds a guest token of the town (the promoted guest's re-fetched town); the guard sits before the refusal",
       "clash >= 0 && pc_net_game_client_holds_guest_token(&home)" in ar and ar.index("pc_net_game_client_holds_guest_token(&home)") < ar.index("REFUSED: the guest name")
       and "mPr_NullCheckPersonalID" not in ar[ar.index("pc_net_game_client_holds_guest_token"):ar.index("REFUSED: the guest name")])
    ck("S M-I pc_relaunch: AC_RELAUNCH_DRYRUN logs the command line and returns 0 (the caller does not quit), before any CreateProcess", 
       "AC_RELAUNCH_DRYRUN" in rl and rl.index("AC_RELAUNCH_DRYRUN") < rl.index("CreateProcessA"))


def phase0(rig, args, ctx):
    ck = rig.ck
    h, ok = rig.start("p0", args.port - 1)
    if not ok:
        return
    gA, gB, recA, recB = ctx["gA"], ctx["gB"], ctx["recA"], ctx["recB"]
    try:
        a = rig.join("A0", gA, recA)
        ctx["tokA"] = bytes(a.guest_token)
        b = rig.join("B0", gB, recB)
        ctx["tokB"] = bytes(b.guest_token)
        L.pump_sleep(2.0)
        rig.release(a)
        rig.release(b)
        L.pump_sleep(7.0)  # the accepted uploads reach guests.dat with the 5 s early save (and with the final save at stop)
    finally:
        rig.closeall()
        code = rig.stop(h)
        ck("P0 graceful stop (exit 0)", code == 0)
    ents = parse_guests(mpath("guests.dat"))
    ck("P0 two guests stored with a synced record (rev >= 1, tokens as handed out)", len(ents) == 2 and all(e["rev"] >= 1 for e in ents)
       and sorted(e["token"] for e in ents) == sorted([ctx["tokA"], ctx["tokB"]]))

    def goods(rec):
        struct.pack_into(">H", rec, L.REC_OFF_POCKETS, 0x2200)
        struct.pack_into(">I", rec, L.REC_OFF_WALLET, 1234)
        struct.pack_into(">I", rec, L.REC_OFF_BANK, 5678)
        struct.pack_into(">HBB", rec, REC_OFF_BIRTHDAY, 2000, 5, 17)
    ck("P0 A's stored record patched (pocket 0x2200, wallet 1234, bank 5678, birthday 5/17)", patch_guest_record(mpath("guests.dat"), L.guest_pid_be(gA), goods)
       and parse_guests(mpath("guests.dat")) is not None)
    ctx["seeds"] = seed_memories(L.GAME_BIN_DIR, L.guest_pid_be(gA), L.guest_pid_be(gB))
    ck("P0 villager memories seeded in the DISPOSABLE GCI (guest A on villagers 0 and 1 and on the islander, guest B on villager 0)", len(ctx["seeds"]) == 4)


def phase1(rig, args, ctx):
    ck = rig.ck
    h, ok = rig.start("p1", args.port)
    if not ok:
        return h
    gA, gB, recA, recB = ctx["gA"], ctx["gB"], ctx["recA"], ctx["recB"]
    tokA, tokB = ctx["tokA"], ctx["tokB"]
    try:
        out = say(h, "help")
        ck("P1 `help` lists promote", "  promote <guest> <slot|auto> <house|auto> confirm" in out)
        a = rig.join("A", gA, recA, token=tokA, record_wait=False)
        L.pump_sleep(9.0)  # the token-presenting reconnect CONFIRMS the entry (guests.dat write): the guard snapshots below are taken AFTER it
        ents = parse_guests(mpath("guests.dat"))
        ck("P1 two guests stored with a synced record (rev >= 1)", len(ents) == 2 and all(e["rev"] >= 1 for e in ents))
        ck("P1 before any promotion: no members.dat (policy off never creates it)", not os.path.exists(mpath("members.dat")))
        pre = {"g": raw(mpath("guests.dat")), "r": md5_or_none(mpath("records.dat"))}
        baks0 = sorted(glob.glob(mpath("*.bak-*")))
        # ---- guard failures: nothing changes
        o1 = say(h, "promote PROMOGST auto auto confirm")
        ck("P1 an ONLINE guest is refused ('is connected on peer')", "promote: refused: guest slot" in o1 and "is connected on peer" in o1)
        o2 = say(h, "promote NOBODY auto auto confirm")
        ck("P1 an unknown guest is refused ('no guest named')", "promote: refused:" in o2 and "no guest named" in o2)
        o3 = say(h, "promote OTHERGST auto auto")
        ck("P1 without `confirm` only says what it would do (RESIDENT slot 3, house 3, re-run with `confirm`)", "RESIDENT slot 3 with house 3" in o3 and "re-run with `confirm`" in o3)
        o4 = say(h, "promote OTHERGST 9 auto confirm")
        ck("P1 a bad slot word is refused", "must each be 0..3 or `auto`" in o4)
        o5 = say(h, "promote OTHERGST 1 auto confirm")
        ck("P1 an occupied resident slot is refused", "resident slot 1 is not free" in o5)
        o6 = say(h, "promote OTHERGST 3 1 confirm")
        ck("P1 an owned house is refused", "house 1 already has an owner" in o6)
        o7 = say(h, "promote OTHERGST auto auto confirm extra")
        o8 = say(h, "promote OTHERGST auto auto yes")
        ck("P1 extra / unknown trailing words are refused ('expected exactly')", "expected exactly" in o7 and "expected exactly" in o8)
        ck("P1 ... none of the guards changed anything: guests.dat byte-identical, no members.dat, records.dat unchanged, no backup file, no 'promoting' log line",
           raw(mpath("guests.dat")) == pre["g"] and not os.path.exists(mpath("members.dat")) and md5_or_none(mpath("records.dat")) == pre["r"]
           and sorted(glob.glob(mpath("*.bak-*"))) == baks0 and "ADMIN: promoting" not in h.log_text())
        rig.release(a)
        L.pump_sleep(2.5)
        # ---- the promotion
        pre_g = raw(mpath("guests.dat"))
        pre_r = md5_or_none(mpath("records.dat"))
        out = say(h, "promote PROMOGST auto auto confirm", timeout=25.0, settle=2.0)
        ck("P1 promote: 'PROMOTED to RESIDENT slot 3, house 3' and the [NET][PROMOTE] ADMIN line", "promote: guest slot" in out and 'PROMOTED to RESIDENT slot 3, house 3' in out and "[NET][PROMOTE] ADMIN: guest slot" in out)
        gbak = sorted(glob.glob(mpath("guests.dat.bak-*")))
        ck("P1 guests.dat was backed up first: exactly one guests.dat.bak-<ts>, BYTE-IDENTICAL to the pre-image", len(gbak) == 1 and raw(gbak[0]) == pre_g)
        if pre_r is not None:
            rbak = sorted(glob.glob(mpath("records.dat.bak-*")))
            ck("P1 records.dat was backed up first (it existed): identical to the pre-image", len(rbak) == 1 and T.md5_file(rbak[0]) == pre_r)
        ck("P1 members.dat did not exist: no backup of it", not glob.glob(mpath("members.dat.bak-*")))
        ents = parse_guests(mpath("guests.dat"))
        pidA = L.guest_pid_be(gA)
        ck("P1 A's guests.dat entry is REMOVED (last), B's entry is still there", len(ents) == 1 and ents[0]["pid"] == L.guest_pid_be(gB) and all(e["pid"] != pidA for e in ents))
        mem = parse_members(mpath("members.dat"))
        k1 = [e for e in mem if e["kind"] == 1]
        k2 = [e for e in mem if e["kind"] == 2]
        ck("P1 members.dat (independent parser, %d B): exactly one RESIDENT_TOKEN + one PROMOTION_HANDOFF, both slot 3, both unconfirmed" % MBR_SIZE,
           len(mem) == 2 and len(k1) == 1 and len(k2) == 1 and k1[0]["slot"] == 3 and k2[0]["slot"] == 3 and k1[0]["confirmed"] == 0 and k2[0]["confirmed"] == 0)
        if len(k1) == 1 and len(k2) == 1:
            npid, rtok = bytes(k1[0]["pid"]), bytes(k1[0]["token"])
            ctx["npid"], ctx["rtok"] = npid, rtok
            ck("P1 the new resident PID: A's name, the HOST's land / land id, a 0xF0xx player id; the handoff = (same PID, aux = A's home PID, token = A's GUEST token != the resident token)",
               npid[:8] == bytes(gA.player_name) and npid[8:16] == ctx["land"] and struct.unpack(">H", npid[18:20])[0] == ctx["land_id"] and (struct.unpack(">H", npid[16:18])[0] & 0xF000) == 0xF000
               and bytes(k2[0]["pid"]) == npid and bytes(k2[0]["aux"]) == pidA and bytes(k2[0]["token"]) == tokA and rtok != tokA and any(rtok))
            ctx["town"] = dict(land=bytes(k1[0]["land"]), land_id=k1[0]["land_id"], hash=k1[0]["hash"])
            lg = h.log_text()
            ck("P1 no token (resident / guest) appears anywhere in the host log", rtok.hex() not in lg.lower() and tokA.hex() not in lg.lower())
        recs = parse_records(mpath("records.dat")) if os.path.exists(mpath("records.dat")) else {}
        ck("P1 records.dat: slot 3 = the new PID at rev 1 (the host pushes it, no MIGRATE)", 3 in recs and recs[3]["rev"] == 1 and recs[3]["pid"] == ctx.get("npid"))
        o = say(h, "residents")
        ck("P1 `residents` lists slot 3 with the guest's name and a credential, never a token", re.search(r'slot 3: name="PROMOGST\s*" credential=yes confirmed=no', o) is not None
           and ctx.get("rtok", b"x").hex() not in o.lower())
        o = say(h, "guests")
        ck("P1 `guests` no longer lists PROMOGST", "PROMOGST" not in o and "OTHERGST" in o)
        # ---- the guest comes back: handoff, then REJECT 6
        off = len(h.log_text())
        a2, rej = rig.attempt("A-back", gA, recA, token=tokA)
        types = [t for t, _p in a2.seen]
        ck("P1 A (guest token, nothing else changed) is REJECTED with reason 6 PROMOTED and the host log line", rej is not None and rej.reason == L.PC_NETGAME_REJECT_PROMOTED
           and "the guest was PROMOTED to resident 3" in h.log_text()[off:])
        ck("P1 order on the wire: RESIDENT_HANDOFF (66) FIRST, then REJECT", types == [L.PC_NETGAME_MSG_RESIDENT_HANDOFF, L.PC_NETGAME_MSG_REJECT])
        if len(a2.seen) == 2:
            hm = a2.seen[0][1]
            ck("P1 RESIDENT_HANDOFF is 48 B: flags 0, slot 3, the new town PID, the resident token (== members.dat), reserved bytes zero", len(hm) == 48
               and parse_handoff(hm)["flags"] == 0 and parse_handoff(hm)["slot"] == 3 and parse_handoff(hm)["pid"] == ctx.get("npid") and parse_handoff(hm)["token"] == ctx.get("rtok")
               and parse_handoff(hm)["rsv"] == 0 and not any(parse_handoff(hm)["rsv2"]))
            ck("P1 REJECT 6 is the 8-byte form with protocol 8", len(a2.seen[1][1]) == 8 and a2.seen[1][1][1] == 6 and struct.unpack_from("<I", a2.seen[1][1], 4)[0] == 8)
        rig.release(a2)
        w, rej = rig.attempt("A-wrongtoken", gA, recA, token=bytes(range(1, 17)))
        ck("P1 a WRONG guest token gets NO handoff (and is not told PROMOTED)", w.seen is not None and L.PC_NETGAME_MSG_RESIDENT_HANDOFF not in [t for t, _p in w.seen]
           and (rej is None or rej.reason != L.PC_NETGAME_REJECT_PROMOTED))
        rig.release(w)
        n, rej = rig.attempt("A-notoken", gA, recA, token=None)
        ck("P1 NO token gets no handoff either", L.PC_NETGAME_MSG_RESIDENT_HANDOFF not in [t for t, _p in n.seen] and (rej is None or rej.reason != L.PC_NETGAME_REJECT_PROMOTED))
        rig.release(n)
        # ---- full town
        pre_g2, pre_m2 = raw(mpath("guests.dat")), raw(mpath("members.dat"))
        o = say(h, "promote OTHERGST auto auto confirm")
        ck("P1 a second promotion finds the town FULL and changes nothing", "the town is FULL" in o and raw(mpath("guests.dat")) == pre_g2 and raw(mpath("members.dat")) == pre_m2)
    finally:
        rig.closeall()
        code = rig.stop(h)
        ck("P1 graceful stop (exit 0)", code == 0)
    # ---- the saved GCI
    gci = raw(os.path.join(L.GAME_BIN_DIR, MF.GCI_REL))
    orig = ctx["orig_gci"]
    npid = ctx.get("npid")
    p3 = gci_priv(gci, 3)
    ck("P1 GCI: resident 3 = the guest (name, host land / land id, the new player id), exists, gender female, face = the guest's, the guest's shirt",
       npid is not None and p3[0:20] == npid and p3[0x1086] == 1 and p3[0x14] == 1 and p3[0x15] == ctx["face"] and p3[0x108A:0x108C] == ctx["recA"][0x108A:0x108C])
    ck("P1 GCI: pockets / wallet / bank carried over (0x2200 in pocket 0, the rest empty, wallet 1234, bank 5678), loan = the house price 17400, reset_code 0",
       struct.unpack(">H", p3[L.REC_OFF_POCKETS:L.REC_OFF_POCKETS + 2])[0] == 0x2200 and not any(p3[L.REC_OFF_POCKETS + 2:L.REC_OFF_POCKETS + 30])
       and struct.unpack(">I", p3[L.REC_OFF_WALLET:L.REC_OFF_WALLET + 4])[0] == 1234 and struct.unpack(">I", p3[L.REC_OFF_BANK:L.REC_OFF_BANK + 4])[0] == 5678
       and struct.unpack(">I", p3[L.REC_OFF_LOAN:L.REC_OFF_LOAN + 4])[0] == 17400 and struct.unpack(">I", p3[L.REC_OFF_RESET_CODE:L.REC_OFF_RESET_CODE + 4])[0] == 0)
    ck("P1 GCI: vanilla new-player defaults (design order table 0..7, no letters: every mail slot unused)", list(p3[L.REC_OFF_ORG_TABLE:L.REC_OFF_ORG_TABLE + 8]) == list(range(8))
       and all(p3[L.REC_OFF_MAIL + i * L.REC_MAIL_SIZE + 0x2E] == L.MAIL_FONT_UNUSED for i in range(L.REC_MAIL_COUNT)))
    ck("P1 GCI: the birthday was copied from the guest record (5/17), the intro's catalog bits are set (carpet 1, wallpaper 1, furniture >= 1: shirt / cassette)",
       p3[REC_OFF_BIRTHDAY + 2:REC_OFF_BIRTHDAY + 4] == bytes([5, 17]) and
       sum(bin(x).count("1") for x in struct.unpack(">3I", p3[REC_OFF_WALL_BITS:REC_OFF_WALL_BITS + 12])) == 1 and
       sum(bin(x).count("1") for x in struct.unpack(">3I", p3[REC_OFF_CARPET_BITS:REC_OFF_CARPET_BITS + 12])) == 1 and
       sum(bin(x).count("1") for x in struct.unpack(">43I", p3[REC_OFF_FURN_BITS:REC_OFF_FURN_BITS + 172])) >= 1)
    melody = bytes(gci[MELODY_OFF:MELODY_OFF + 8])
    seeds = ctx.get("seeds", {})
    ok_mem = len(seeds) == 4 and npid is not None
    for name in ("a0", "a1"):
        if name in seeds:
            o, was = seeds[name]
            now = bytes(gci[o:o + MEM_STRIDE])
            ok_mem = ok_mem and now[0:20] == npid and now[0x1C:0x24] == ctx["land"] and now[0x24:0x26] == struct.pack(">H", ctx["land_id"]) and now[0x28:0x30] == melody and now[0x14:0x1C] == was[0x14:0x1C] and now[0x30:] == was[0x30:]
    ck("P1 GCI: the guest's villager memories (villagers 0 and 1) now carry the NEW PID, the host land name / id and the town melody; last-talk time, friendship and letter are untouched", ok_mem)
    if "b0" in seeds:
        o, was = seeds["b0"]
        ck("P1 GCI: another guest's memory on villager 0 is byte-identical (not re-keyed)", bytes(gci[o:o + MEM_STRIDE]) == was)
    if "isl" in seeds:
        o, was = seeds["isl"]
        now = bytes(gci[o:o + MEM_STRIDE])
        ck("P1 GCI: the ISLANDER's memory is re-keyed to the new PID but its memuni (furniture bitfield) and tune are untouched", npid is not None and now[0:20] == npid and now[20:] == was[20:])
    h3 = gci_home(gci, 3)
    ck("P1 GCI: house 3 ownerID = the new resident, and nothing else of the house changed", npid is not None and h3[0:20] == npid and h3[20:] == gci_home(orig, 3)[20:])
    # the mailbox tail (offset >= 0x1A30) of a house is host RUNTIME state (the game delivers letters: observed on the host's own house 0 in the first run); everything before it is compared
    ck("P1 GCI: the OTHER residents 0..2 and houses 0..2 (everything before the mailbox, offset < 0x1A30) are byte-identical to the fixture", all(gci_priv(gci, i) == gci_priv(orig, i) for i in range(3))
       and all(gci_home(gci, i)[:0x1A30] == gci_home(orig, i)[:0x1A30] for i in range(3)))
    for i in range(3):
        if gci_priv(gci, i) != gci_priv(orig, i):
            L.info("resident %d differs at offset 0x%X" % (i, first_diff(gci_priv(gci, i), gci_priv(orig, i))))
        if gci_home(gci, i) != gci_home(orig, i):
            L.info("house %d differs at offset 0x%X" % (i, first_diff(gci_home(gci, i), gci_home(orig, i))))
    ck("P1 GCI: the house arrangement is unchanged (0x%02X) and the checksum validates" % orig[ARRANGEMENT_OFF], gci[ARRANGEMENT_OFF] == orig[ARRANGEMENT_OFF]
       and MF.stored_checksum(gci[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE]) == MF.checksum(gci[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE]))
    return h


def phase2(rig, args, ctx):
    ck = rig.ck
    if "npid" not in ctx:
        ck("P2 skipped: P1 produced no promotion", False)
        return
    gA, recA, tokA, npid, rtok = ctx["gA"], ctx["recA"], ctx["tokA"], ctx["npid"], ctx["rtok"]
    h, ok = rig.start("p2", args.port + 1, ["--resident-tokens", "tofu"])
    if not ok:
        return
    try:
        lg = h.log_text()
        ck("P2 after the RESTART members.dat is loaded (1 resident credential restored)", "1 resident credential(s) restored" in lg)
        a, rej = rig.attempt("A-restart", gA, recA, token=tokA)
        ck("P2 the handoff PERSISTED: 66 then REJECT 6 again, same slot / PID / token", rej is not None and rej.reason == 6 and [t for t, _p in a.seen] == [66, 3]
           and parse_handoff(a.seen[0][1])["slot"] == 3 and parse_handoff(a.seen[0][1])["pid"] == npid and parse_handoff(a.seen[0][1])["token"] == rtok)
        rig.release(a)
        o = say(h, "residents")
        ck("P2 `residents`: slot 3 credential=yes confirmed=no", re.search(r'slot 3: name="PROMOGST\s*" credential=yes confirmed=no', o) is not None)
        ident = L.PlayerIdentity(npid[:8], struct.unpack(">H", npid[16:18])[0], 1)
        off = len(h.log_text())
        c = rig.resident("RES3", ident, 3, rtok)
        t = h.log_text()[off:]
        ck("P2 a resident-claim client with the NEW PID + the handed-over token is admitted as RESIDENT 3 (bound, host-derived)", c.is_connected() and "bound to resident 3 (host-derived)" in t)
        last = c.token_msgs[-1][1] if c.token_msgs else None
        ck("P2 ... the host answers IDENTITY_TOKEN KNOWN|RESIDENT (0x06), slot 3 (no new token is minted)", last is not None and last.flags == (L.PC_NETGAME_IDTOKEN_FLAG_RESIDENT | L.PC_NETGAME_IDTOKEN_FLAG_KNOWN)
           and last.guest_slot == 3 and bytes(last.token) == rtok)
        ck("P2 ... host log: credential confirmed AND the promotion handoff removed", "promotion handoff removed" in t)
        mem = parse_members(mpath("members.dat"))
        ck("P2 members.dat: the RESIDENT_TOKEN is CONFIRMED with the SAME token, the PROMOTION_HANDOFF entry is gone", len(mem) == 1 and mem[0]["kind"] == 1 and mem[0]["confirmed"] == 1
           and bytes(mem[0]["token"]) == rtok and bytes(mem[0]["pid"]) == npid)
        o = say(h, "residents")
        ck("P2 `residents`: slot 3 confirmed=yes, connected=yes", re.search(r'slot 3: name="PROMOGST\s*" credential=yes confirmed=yes armed=no connected=yes', o) is not None)
        rig.release(c)
        a, rej = rig.attempt("A-after", gA, recA, token=tokA)
        ck("P2 after the confirmed login A gets no handoff any more (the name belongs to a resident): no 66, not REJECT 6", 66 not in [t for t, _p in a.seen] and (rej is None or rej.reason != 6))
        rig.release(a)
    finally:
        rig.closeall()
        code = rig.stop(h)
        ck("P2 graceful stop (exit 0)", code == 0)


def phase3(rig, args, ctx):
    ck = rig.ck
    if "town" not in ctx:
        ck("P3 skipped: no town key from P1", False)
        return
    gB, recB, tokB = ctx["gB"], ctx["recB"], ctx["tokB"]
    t = ctx["town"]
    ghost = b"GHOSTRES" + t["land"] + struct.pack(">HH", 0xF0AA, t["land_id"])
    ent = dict(confirmed=0, slot=2, kind=2, land=t["land"], land_id=t["land_id"], hash=t["hash"], pid=ghost, token=tokB, age=1, aux=L.guest_pid_be(gB))
    with open(mpath("members.dat"), "wb") as f:
        f.write(build_members([ent], gen=50))
    h, ok = rig.start("p3", args.port + 2)
    if not ok:
        return
    try:
        a, rej = rig.attempt("B-invalid-handoff", gB, recB, token=tokB)
        t_ = h.log_text()
        ck("P3 a handoff whose resident is NOT in the GCI is ignored (log 'INVALID ... not in the host save'): guest B is admitted as a guest as before",
           rej is None and a.is_connected() and 66 not in [x for x, _p in a.seen] and "is INVALID (the new resident is not in the host save)" in t_)
        ck("P3 ... and the guest entry is untouched (B still in guests.dat)", any(e["pid"] == L.guest_pid_be(gB) for e in parse_guests(mpath("guests.dat"))))
        rig.release(a)
    finally:
        rig.closeall()
        code = rig.stop(h)
        ck("P3 graceful stop (exit 0)", code == 0)


def run(args, results):
    rig = Rig(results)
    ctx = {}
    src = os.path.join(L.GAME_BIN_DIR, MF.GCI_REL)
    ctx["orig_gci"] = raw(src)
    land = ctx["orig_gci"][MF.MAIN_OFF + MF.PRIV_OFF + 8:MF.MAIN_OFF + MF.PRIV_OFF + 16]
    ctx["land"] = bytes(land)
    ctx["land_id"] = struct.unpack(">H", ctx["orig_gci"][MF.MAIN_OFF + MF.PRIV_OFF + 18:MF.MAIN_OFF + MF.PRIV_OFF + 20])[0]
    faces = {ctx["orig_gci"][MF.MAIN_OFF + MF.PRIV_OFF + i * MF.PRIV_STRIDE + 0x15] for i in range(3)}
    ctx["face"] = next(f for f in range(8) if f not in faces)
    gA = L.guest_identity("PROMOGST", 0x4A11, "HOMETWN", 0x5B11)
    gB = L.guest_identity("OTHERGST", 0x4A12, "HOMETWN", 0x5B11)
    recA = bytes(L.fresh_guest_record_for(gA, gender=1, face=ctx["face"]))
    ctx.update(gA=gA, gB=gB, recA=recA, recB=bytes(L.fresh_guest_record_for(gB)))
    shutil.rmtree(mp_dir(), ignore_errors=True)  # the disposable fixture only
    source_audit(rig)
    source_audit_mi(rig)
    for name, fn in (("P0", phase0), ("P1", phase1), ("P2", phase2), ("P3", phase3)):
        try:
            fn(rig, args, ctx)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            rig.ck("%s aborted by an exception: %s" % (name, e), False)
            rig.closeall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12961)
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
