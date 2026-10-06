#!/usr/bin/env python3
"""test_guest_lifecycle_protocol.py - guest -> resident LIFECYCLE HARDENING: S2 (migration, rollback, durability gate) + S3 (last-house race, crash recovery),
scripted FakeClients against REAL `AnimalCrossing.exe --host <port> --dedicated` processes (stdin pipe).

TIER: real dedicated hosts on DISPOSABLE copies pc\\build64\\bin_fixture4_lcyc (fresh copy of the read-only bin_fixture4, re-made between scenarios; NET_SPIKE_GAME_BIN points at it).
The town save is derived so that resident slot 3 / house 3 are FREE (like test_house_purchase_protocol.py). The live save dir, bin_talkfix*, bin_fixture4 and every other
fixture are never used. The hook flags (--promote-fault) need AC_TEST_HOOKS=1, which net_spike_lib's HostProcess puts in the child environment.

S2  (fixture 1) P0: the GCI is SEEDED for the guest key (a villager memory with send_reply + letter, contest_quest.player_id on a villager and on the islander, fishRecord[0].pid)
    plus control entries of ANOTHER guest; two guests join with fresh records, the host stops, their stored guests.dat records are PATCHED (wallet, mail[0] addressed to the guest,
    equipment = a net, a furniture bit, hint_count, destiny, maps, a delivery quest, a first-job errand (must NOT migrate) + a chain errand (must migrate), state_flags / reset_code
    (must NOT migrate)). (The first upload of a guest must be a FRESH character, so the content enters through the stored record, like test_house_purchase_protocol.py.)
    P1 `--promote-fault no_durable` (the town save cannot be durable): the purchase is REFUSED (REJECTED BUSY), guests.dat / members.dat / GCI byte-identical, the guest keeps its wallet.
    P2 `--promote-fault fail_save`: the authoritative save FAILS (one shot) -> REJECTED, GCI / guests.dat / members.dat byte-identical, and after a console `save` the GCI shows the
       memories / contest / fish still keyed to the GUEST (rolled back) and slot 3 free; the retry then APPLIES: the new resident's mail[0] is byte-equal with recipient = sender = the NEW
       PID, equipment / furniture bit / delivery quest / chain errand / hint_count / destiny / maps are carried, the first-job errand / state_flags / reset_code are not, memory +
       contest + islander contest + fish record are re-keyed to the new PID (host land for the villager memory), controls untouched.
S3  (fresh fixtures) a) two guests send kind 14 in the SAME tick for the LAST house (+ a byte-identical resend of the winner): exactly one APPLIED, the other NO_RESIDENCE, one resident, one charge.
    b) `--promote-fault crash_after_gci`: restart -> the GCI has the resident, guests.dat still held the guest, the handoff is valid: the guest claim gets 66 + REJECT 6, the load-time sweep
       removed the stale guest entry, the resident claim is KNOWN (handoff removed), and a REPLAYED old guest token is refused.
    c) `--promote-fault crash_after_members`: the handoff is INVALID (no resident in the GCI): the guest is admitted with its pre-debit wallet; the next promotion prunes the orphans.
Usage: python test_guest_lifecycle_protocol.py [--port 12991]
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

FIXTURE = "bin_fixture4_lcyc"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)


def make_free_slot(dest, slot=3):
    """Derives the disposable town save: resident `slot` cleared (vanilla empty PersonalID, exists 0) and its house's owner cleared; checksum + backup copy recomputed like the game's writer."""
    p = os.path.join(dest, MF.GCI_REL)
    g = bytearray(open(p, "rb").read())
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


def fresh_fixture():
    dest = T.make_fixture(FIXTURE)
    make_free_slot(dest)
    return dest


if __name__ == "__main__":
    fresh_fixture()

import net_spike_lib as L  # noqa: E402

IP = "127.0.0.1"
PRICE = 18400
MBR_SIZE = 32 + 16 * 80 + 4
GST_ENTRY = 72 + 0x2440
GST_SIZE = 32 + 8 * GST_ENTRY + 4
MSG_TXN_RESULT = L.PC_NETGAME_MSG_TXN_RESULT
ANIMALS_OFF = MF.MAIN_OFF + 0x17438
ANIMAL_STRIDE, MEM_OFF, MEM_STRIDE = 0x988, 0x10, 0x138
CONTEST_PID_OFF = 0x8A8 + 0x0E
ISLAND_ANIMAL_OFF = MF.MAIN_OFF + 0x22540 + 0xF00
FISH_OFF = MF.MAIN_OFF + 0x23E68
MELODY_OFF = MF.MAIN_OFF + 0x20F08
ARRANGEMENT_OFF = MF.MAIN_OFF + 0x2068A
SEED_TUNE = 0x1122334455667788
EQUIP_NET = 0x2200  # ITM_NET == ITM_TOOL_START


def mp_dir():
    return os.path.join(L.GAME_BIN_DIR, "save", "mp")


def mpath(n):
    return os.path.join(mp_dir(), n)


def gci_path():
    return os.path.join(L.GAME_BIN_DIR, MF.GCI_REL)


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
        ents.append(dict(i=i, confirmed=b[o + 1], slot=b[o + 2], kind=b[o + 3], pid=bytes(b[o + 20:o + 40]), token=bytes(b[o + 40:o + 56]), aux=bytes(b[o + 60:o + 80])))
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
            out.append(dict(slot=i, pid=bytes(e[4:24]), token=bytes(e[24:40]), rev=struct.unpack_from("<I", e, 44)[0], wallet=struct.unpack_from(">I", rec, L.REC_OFF_WALLET)[0]))
    return out


def patch_gci(fn):
    """Rewrites the main Save_t of the DISPOSABLE copy through fn(bytearray gci) and recomputes the checksum + backup copy like the game's writer."""
    p = gci_path()
    g = bytearray(raw(p))
    fn(g)
    m = bytearray(g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE])
    m[MF.CHK_OFF:MF.CHK_OFF + 2] = struct.pack(">H", MF.checksum(bytes(m)))
    g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE] = m
    g[MF.BACK_OFF:MF.BACK_OFF + MF.SAVE_SECTOR_SIZE] = g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_SECTOR_SIZE]
    open(p, "wb").write(bytes(g))


def patch_guest_record(path, pid, patch):
    """Rewrites the stored record of the guests.dat entry with key `pid` through patch(bytearray) and recomputes the entry CRC32 + the file CRC32."""
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
        h = L.HostProcess(port=port, extra_args=["--dedicated"] + list(extra), log_path=T.log_path("lcyc_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR,
                          stdin_pipe=True, verbose=False, new_group=True).start()
        orig = h.log_text
        h.log_text = lambda: orig().replace("\r\n", "\n")
        self.port = port
        ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
        self.ck("[%s] dedicated host booted (%s)" % (tag, " ".join(extra) or "defaults"), ok)
        if ok:
            L.resolve_host_town(IP, port)
        return h, ok

    def stop(self, h):
        h.send_line("stop")
        return h.wait_exit(60.0)

    def _bind(self):
        Rig._n[0] += 1
        return "127.0.%d.%d" % (80 + Rig._n[0] // 200, 2 + Rig._n[0] % 200)

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
    """What a refused purchase must leave byte-identical (the promotion core backs the three files up BEFORE it changes anything, so the *.bak-* list is not part of it)."""
    return {"guests": raw_or_none(mpath("guests.dat")), "members": raw_or_none(mpath("members.dat")), "records": raw_or_none(mpath("records.dat")),
            "gci": T.md5_file(gci_path())}


def buy(c, price=PRICE, house=L.PC_NETGAME_HOUSE_AUTO, base=None, rid=1, **kw):
    return c.send_txn_commit(L.PC_NETGAME_TXN_KIND_HOUSE_PURCHASE, rid, L.PC_NETGAME_TXN_DEST_NONE, 0, 0, base=base, aux_cond=house, aux_item=price, **kw)


# ---------------------------------------------------------------------------------------------------------------- seeds
def mem_off(base, k):
    return base + MEM_OFF + k * MEM_STRIDE


def free_mem_slots(g, base):
    return [k for k in range(7) if bytes(g[mem_off(base, k) + 16:mem_off(base, k) + 20]) == bytes([255, 255, 255, 255])]


def _today_rtc():
    """lbRTC_time_c (BE): sec, min, hour, day, weekday (0 = Sunday), month, year u16 -- today's local date."""
    import datetime
    d = datetime.datetime.now()
    return bytes([0, 0, 12, d.day, (d.weekday() + 1) % 7, d.month]) + struct.pack(">H", d.year)


def seed_gci(pid_a, pid_b):
    """Seeds the GCI for guest A (and controls for guest B): a villager-0 memory with send_reply + a letter pattern, contest quests (villager 1 + the islander), fishRecord[0]."""
    out = {}

    def fn(g):
        b0 = ANIMALS_OFF
        f0 = free_mem_slots(g, b0)
        assert len(f0) >= 2, f0
        for name, k, pid, land, lid, tune in (("mem_a", f0[0], pid_a, b"OLDTOWN ", 0x1234, SEED_TUNE), ("mem_b", f0[1], pid_b, b"BTOWN   ", 0x4321, SEED_TUNE + 1)):
            o = mem_off(b0, k)
            g[o:o + 20] = pid
            g[o + 0x1C:o + 0x24] = land
            g[o + 0x24:o + 0x26] = struct.pack(">H", lid)
            g[o + 0x28:o + 0x30] = struct.pack(">Q", tune)
            g[o + 0x30] = 5               # friendship
            g[o + 0x31] = 0x25            # letter_info: exists + send_reply (+ a marker bit)
            g[o + 0x32:o + 0x32 + 16] = bytes(range(0xA0, 0xB0))  # a saved-letter pattern
            out[name] = (o, bytes(g[o:o + MEM_STRIDE]))
        for name, base, pid in (("con_a", ANIMALS_OFF + ANIMAL_STRIDE, pid_a), ("con_b", ANIMALS_OFF + 2 * ANIMAL_STRIDE, pid_b), ("con_isl", ISLAND_ANIMAL_OFF, pid_a)):
            o = base + CONTEST_PID_OFF
            g[o:o + 20] = pid
            out[name] = (o, bytes(g[o:o + 20]))
        for name, i, pid in (("fish_a", 0, pid_a), ("fish_b", 1, pid_b)):
            o = FISH_OFF + i * 0x20
            g[o:o + 20] = pid
            g[o + 20:o + 28] = _today_rtc()  # TODAY's date: the game's mFR_delete_npc_record drops fish records of non-residents that are not from today
            g[o + 28:o + 32] = struct.pack(">I", 2 + i)
            out[name] = (o, bytes(g[o:o + 0x20]))
    patch_gci(fn)
    return out


CTX_MAIL_BODY = b"LIFECYCLE-LETTER-0123456789ABCDEF"


def lifecycle_patch(rec, pid, wallet=25000):
    """The guest's stored record after it 'played': wallet, a letter addressed TO and FROM the guest key, a net in hand, a furniture bit, hint_count, destiny, a map, a delivery quest, a
    first-job errand (must NOT migrate), a chain errand (must migrate), state_flags / reset_code (must NOT migrate)."""
    struct.pack_into(">I", rec, L.REC_OFF_WALLET, wallet)
    struct.pack_into(">H", rec, L.REC_OFF_POCKETS, EQUIP_NET)
    rec[L.REC_OFF_MAIL:L.REC_OFF_MAIL + L.REC_MAIL_SIZE] = L.build_mail_be(pid, pid, present=0, body=CTX_MAIL_BODY)
    struct.pack_into(">H", rec, L.REC_OFF_EQUIPMENT, EQUIP_NET)
    struct.pack_into(">I", rec, 0x1108 + 4 * 30, 0x00000400)           # furniture_collected_bitfield[30] bit 10
    rec[0x1087] = 9                                                      # hint_count
    rec[0x10A2] = 3                                                      # destiny.type = BAD_LUCK
    rec[0x11DC:0x11DC + 8] = b"MAPTOWN "                                 # maps[0].land_name
    struct.pack_into(">H", rec, 0x11DC + 8, 0x7A7A)                      # maps[0].land_id
    q = bytearray(0x28)
    q[0] = 0x01                                                          # BE: quest_type 0 (delivery), quest_kind 1
    q[0x0C:0x28] = bytes(range(0x40, 0x40 + 0x1C))
    rec[0x94:0x94 + 0x28] = q
    e0 = bytearray(0x58)
    e0[0] = 0x43                                                         # type 1 (errand), kind 3 = FIRSTJOB_CHANGE_CLOTH
    e0[0x2A] = 0x02                                                      # errand_type FIRST_JOB
    rec[0x2EC:0x2EC + 0x58] = e0
    e1 = bytearray(0x58)
    e1[0] = 0x40                                                         # type 1 (errand), kind 0 = REQUEST
    e1[0x2A] = 0x01                                                      # errand_type CHAIN
    e1[0x0C:0x28] = bytes(range(0x80, 0x80 + 0x1C))
    rec[0x2EC + 0x58:0x2EC + 0xB0] = e1
    struct.pack_into(">I", rec, 0x2348, 0xFFFFFFFF)                      # state_flags (not carried)
    struct.pack_into(">I", rec, 0x10F4, 0xDEADBEEF)                      # reset_code (not carried)


def expected_mail(pid_old, npid):
    m = bytearray(L.build_mail_be(pid_old, pid_old, present=0, body=CTX_MAIL_BODY))
    m[0:20] = npid
    m[0x16:0x2A] = npid
    return bytes(m)


# ---------------------------------------------------------------------------------------------------------------- source audit
def source_audit(rig):
    ck = rig.ck
    rd = lambda *p: open(os.path.join(T.PC, *p), encoding="utf-8", errors="replace").read()  # noqa: E731
    ng, mc, mn = rd("src", "pc_net_game.c"), rd("src", "pc_m_card.c"), rd("src", "pc_main.c")
    mg = mc[mc.index("int pc_mp_promote_migrate("):mc.index("int pc_mp_promote_create(")]
    pc = mc[mc.index("int pc_mp_promote_create("):mc.index("void pc_mp_promote_rollback(void) {")]
    ck("S migrate: pc_pid_rekey copies the WHOLE PersonalID; migrate re-keys memories + contest quests (15 villagers + islander) + fish records, carries mail / equipment (pocket-legal) / bits (OR) / "
       "maps / hint_count / destiny / sunburn / remail / animal_memory / delivery + non-first-job errand quests; first-job kinds are skipped",
       "mPr_CopyPersonalID(p, npid);" in mc and "contest += pc_pid_rekey(&an->contest_quest.player_id, gkey, npid);" in mg and "fish += pc_pid_rekey(&Save_Get(fishRecord)[i].pid, gkey, npid);" in mg
       and "pc_net_game_pocket_legal_item(g->equipment)" in mg and "pc_promote_or_bits(priv->furniture_collected_bitfield" in mg and "mQst_ERRAND_TYPE_FIRST_JOB" in mg
       and "priv->saved_mail_header = g->saved_mail_header;" in mg and "state_flags" not in mg.replace("state_flags, museum_record", "").replace("NOT carried (documented): state_flags", ""))
    ck("S migrate is ATOMIC with the promotion: called inside pc_mp_promote_create AFTER pc_residence_assign (set_use) and covered by the rollback snapshot (priv / animals / island / FISH records)",
       "remapped = pc_mp_promote_migrate(gkey, g, priv);" in pc and pc.index("pc_residence_assign(slot, house)") < pc.index("pc_mp_promote_migrate(")
       and "memcpy(s_pc_promote_snap.fish, Save_Get(fishRecord)" in pc and "memcpy(Save_Get(fishRecord), s_pc_promote_snap.fish" in mc)
    pe = ng[ng.index("static int pcnetgame_promote_exec(const char* gsel, int gfixed"):ng.index("/* ===== M-F END ===== */")]
    ck("S durability gate (B3a): promote_exec refuses with BUSY BEFORE the dry-run / any change when pc_save_can_be_durable() is false; step 4 uses pc_save_write_authoritative_durable()",
       "if (!pc_save_can_be_durable())" in pe and pe.index("if (!pc_save_can_be_durable())") < pe.index("bak_g[0] = bak_m[0]") and "pc_save_write_authoritative_durable()" in pe and "pc_save_write_authoritative()" not in pe
       and "int pc_save_write_authoritative_durable(void) {" in mc and "int pc_save_can_be_durable(void) {" in mc)
    ck("S remail day reset (E): the new resident's s_remail_day is reset in promote_exec and restored on the rollback paths", "old_remail = s_remail_day[s];" in pe and pe.count("s_remail_day[s] = old_remail;") == 2)
    ck("S stale guest entry (B4b/4c): resident_confirm removes the guests.dat entry FIRST (keeps the handoff when that fails); a load-time sweep removes stale entries of promoted guests, never UNTRUSTED",
       "static int pcnetgame_guest_drop_promoted(" in ng and "static void pcnetgame_promoted_guest_sweep(void) {" in ng and "pcnetgame_promoted_guest_sweep(); /* lifecycle hardening (B4b/4c)" in ng
       and ng.index("pcnetgame_guest_drop_promoted(aux") < ng.index('"resident credential confirmed, promotion handoff removed"') and "s_guest_untrusted || !s_members_loaded || s_members_untrusted" in ng)
    hd = ng[ng.index("static void pcnetgame_handle_client_resident_handoff("):ng.index("/* G6.1: the REJECT reason")]
    ck("S client handoff (B8): token.dat is LOADED first (the guest entry is kept) and gains the resident entry BEFORE membership.ini is written; the guest entry is dropped only after the first KNOWN login",
       "(void)pc_mp_gtoken_load(tp, &f, &unreadable);" in hd and hd.index("pc_mp_gtoken_save(tp, &f)") < hd.index("pc_character_membership_write(") and "pcnetgame_client_drop_guest_entries();" in ng)
    ck("S purchase applied flag (B3b): the sticky flag is set BEFORE the post-image consistency check; a RESIDENT_HANDOFF for the claimed town also counts",
       ng.index("s_house_applied_sticky = 1; /* the HOST said APPLIED") < ng.index("carries an inconsistent post-image") and "return s_house_applied_sticky || s_client_handoff_seen;" in ng and "s_client_handoff_seen = 1;" in hd)
    ck("S allocator (D): one host pair + client twin; the new-code literals use PC_RESIDENCE_*; the 4-house assumption is a static assert",
       "int pc_residence_find_free(" in ng and "int pc_residence_assign(" in ng and "int pc_residence_local_free(" in ng and "t->aux_cond < (uint8_t)PC_RESIDENCE_HOUSES" in ng
       and "aux_cond <= 3u" not in ng and "house_or_auto > 3" not in ng and "PC_RESIDENCE_SLOTS == 4" in rd("include", "pc_residence.h") and "PC_RESIDENCE_HOUSES == 4" in rd("include", "pc_residence.h")
       and "aNSC_pc_hs_free[PC_RESIDENCE_HOUSES]" in open(os.path.join(T.PC, "..", "src", "actor", "npc", "ac_npc_shop_common.c"), encoding="utf-8", errors="replace").read())
    ck("S B12: the permutation check also requires the slot mHS_get_pl_no(house) maps to be this slot or free, and every live resident to own homes[arrange(r)]",
       "mHS_get_pl_no(house)" in ng and "does not own homes[" in ng and "pc_residence_check(slot, house, cerr, sizeof(cerr))" in pc)
    ck("S hooks: --promote-fault fail_save / crash_after_members / crash_after_gci (+ no_durable) are HOST-only, parsed inside PC_NET_TEST_HOOKS, loud, and need AC_TEST_HOOKS=1",
       "--promote-fault" in mn and "g_pc_promote_fault == 2" in pe and "g_pc_promote_fault == 3" in pe and "g_pc_promote_fault == 1" in mc and "[TEST-HOOK] REFUSED" in mn and "_exit(97)" in pe and "_exit(98)" in pe)


# ---------------------------------------------------------------------------------------------------------------- S2
def setup_s2(rig, ctx, port):
    """P0: seed the GCI, two guests (A = the migrant, D = the durability-gate guest) join with fresh records, the host stops, the stored records are patched."""
    ck = rig.ck
    ctx["gA"], ctx["gB"], ctx["gD"] = L.guest_identity("MIGRGST", 0x4C11, "HOMETWN", 0x5C21), L.guest_identity("CTRLGST", 0x4C12, "HOMETWN", 0x5C21), L.guest_identity("DURAGST", 0x4C13, "HOMETWN", 0x5C21)
    ctx["pidA"], ctx["pidB"] = L.guest_pid_be(ctx["gA"]), L.guest_pid_be(ctx["gB"])
    ctx["seeds"] = seed_gci(ctx["pidA"], ctx["pidB"])
    ck("P0 GCI seeded for guest A (memory + letter flags, contest quests on villager 1 and the islander, fishRecord[0]) and for the control guest B", len(ctx["seeds"]) == 7)
    h, ok = rig.start("s2p0", port - 1)
    if not ok:
        return False
    try:
        for key in ("gA", "gD"):
            c = rig.join(key, ctx[key], bytes(L.fresh_guest_record_for(ctx[key])))
            ctx["tok_" + key] = bytes(c.guest_token)
        L.pump_sleep(2.0)
        for c in list(rig.clients):
            rig.release(c)
        L.pump_sleep(7.0)
    finally:
        rig.closeall()
        ck("P0 graceful stop (exit 0)", rig.stop(h) == 0)
    ents = parse_guests(mpath("guests.dat"))
    ck("P0 two guests stored with a synced record (rev >= 1)", len(ents) == 2 and all(e["rev"] >= 1 for e in ents))
    ck("P0 A's stored record patched (mail[0] to/from A, a net, a furniture bit, quests, hints, ...)", patch_guest_record(mpath("guests.dat"), ctx["pidA"], lambda r: lifecycle_patch(r, ctx["pidA"])))
    ck("P0 D's stored record patched (wallet 25000)", patch_guest_record(mpath("guests.dat"), L.guest_pid_be(ctx["gD"]), lambda r: struct.pack_into(">I", r, L.REC_OFF_WALLET, 25000)))
    ctx["gci_before"] = raw(gci_path())
    return True


def phase_no_durable(rig, ctx, port):
    ck = rig.ck
    h, ok = rig.start("s2p1", port, ["--resident-tokens", "tofu", "--promote-fault", "no_durable"])
    if not ok:
        return
    try:
        d = rig.join("D", ctx["gD"], bytes(L.fresh_guest_record_for(ctx["gD"])), token=ctx["tok_gD"])
        L.pump_sleep(9.0)
        ck("P1 D holds the stored wallet 25000 (the host pushed the stored record)", L.record_inventory(d.rec_local)[2] == 25000)
        pre = snap()
        sent = buy(d, rid=1)
        r = d.wait_txn_result(sent.seq, 8.0)
        ck("P1 `no_durable`: the purchase is REFUSED: TXN_RESULT REJECTED BUSY (12), no post-image", r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED and r.reason == L.PC_NETGAME_TXN_REASON_BUSY)
        L.pump_sleep(2.0)
        t = h.log_text()
        ck("P1 ... guests.dat / members.dat / records.dat / the GCI file / backups byte-identical, the guest stays connected, nothing 'BOUGHT'", snap() == pre and d.is_connected() and "BOUGHT a house" not in t)
        ck("P1 ... the host says why: the town save cannot be made durable (nothing changed, the guest and its wallet are intact)", "the town save cannot be made durable" in t)
        ents = {e["pid"]: e for e in parse_guests(mpath("guests.dat"))}
        ck("P1 ... the guest entry and its wallet (25000) are intact in guests.dat", L.guest_pid_be(ctx["gD"]) in ents and ents[L.guest_pid_be(ctx["gD"])]["wallet"] == 25000)
    finally:
        rig.closeall()
        ck("P1 graceful stop (exit 0)", rig.stop(h) == 0)


def phase_fail_save(rig, ctx, port):
    ck = rig.ck
    h, ok = rig.start("s2p2", port, ["--resident-tokens", "tofu", "--promote-fault", "fail_save"])
    if not ok:
        return
    seeds, pidA, pidB = ctx["seeds"], ctx["pidA"], ctx["pidB"]
    try:
        a = rig.join("A", ctx["gA"], bytes(L.fresh_guest_record_for(ctx["gA"])), token=ctx["tok_gA"])
        L.pump_sleep(9.0)
        ck("P2 A holds the stored wallet 25000", L.record_inventory(a.rec_local)[2] == 25000)
        pre = snap()
        sent = buy(a, rid=1)
        r = a.wait_txn_result(sent.seq, 10.0)
        ck("P2 `fail_save`: REJECTED (the authoritative save failed -> the promotion rolled back), no post-image", r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED)
        L.pump_sleep(2.0)
        t = h.log_text()
        ck("P2 ... host log: the fault fired, 'the in-memory change was ROLLED BACK', the purchase REFUSED, wallet unchanged", "--promote-fault fail_save: the authoritative save FAILS" in t and "the in-memory change was ROLLED BACK" in t
           and "HOUSE_PURCHASE REFUSED" in t and "BOUGHT a house" not in t)
        ck("P2 ... guests.dat / members.dat / records.dat / the GCI file byte-identical, A still connected", snap() == pre and a.is_connected())
        say(h, "save", settle=3.0, timeout=12.0)
        L.pump_sleep(4.0)
        gci = raw(gci_path())
        o = seeds["mem_a"][0]
        ck("P2 after a console `save` the GCI shows the ROLLED-BACK state: slot 3 free, house 3 unowned, the arrangement unchanged", gci_priv(gci, 3)[0x1086] == 0 and gci_home(gci, 3)[0:20] == gci_home(ctx["gci_before"], 3)[0:20]
           and gci[ARRANGEMENT_OFF] == ctx["gci_before"][ARRANGEMENT_OFF])
        ck("P2 ... the memory and the contest quests (villager 1 + islander) are STILL keyed to the GUEST (restored by the rollback)", bytes(gci[o:o + 20]) == pidA and bytes(gci[seeds["con_a"][0]:seeds["con_a"][0] + 20]) == pidA
           and bytes(gci[seeds["con_isl"][0]:seeds["con_isl"][0] + 20]) == pidA)
        # retry: the one-shot fault is consumed -> APPLIED
        sent = buy(a, rid=2)
        r = a.wait_txn_result(sent.seq, 15.0)
        ck("P2 the retry APPLIES (post wallet 25000 - 18400 = 6600)", r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED and r.post_wallet == 25000 - PRICE)
        L.pump_sleep(2.0)
        ck("P2 host log: '[PC] migrate:' line with letters=1 equipment=1 contest_quests=2 fish_records=1 memories=1 quests=2", re.search(
            r"\[PC\] migrate: memories=1 contest_quests=2 fish_records=[01] letters=1 equipment=1 quests=2", h.log_text()) is not None)
        mem = parse_members(mpath("members.dat"))
        k1 = [e for e in mem if e["kind"] == 1]
        ctx["npid"] = bytes(k1[0]["pid"]) if len(k1) == 1 else None
        ck("P2 members.dat: one RESIDENT_TOKEN + one PROMOTION_HANDOFF (slot 3)", len(mem) == 2 and len(k1) == 1)
    finally:
        rig.closeall()
        ck("P2 graceful stop (exit 0)", rig.stop(h) == 0)


def s2_final(rig, ctx):
    ck = rig.ck
    npid, seeds, pidA, pidB = ctx.get("npid"), ctx["seeds"], ctx["pidA"], ctx["pidB"]
    if npid is None:
        ck("S2 final skipped: no purchase happened", False)
        return
    gci = raw(gci_path())
    orig = ctx["gci_before"]
    p3 = gci_priv(gci, 3)
    ck("S2 GCI: resident 3 = the buyer (new PID, exists, loan 0, wallet 6600)", p3[0:20] == npid and p3[0:8] == bytes(ctx["gA"].player_name) and p3[0x1086] == 1
       and struct.unpack(">I", p3[L.REC_OFF_LOAN:L.REC_OFF_LOAN + 4])[0] == 0 and struct.unpack(">I", p3[L.REC_OFF_WALLET:L.REC_OFF_WALLET + 4])[0] == 25000 - PRICE)
    mo = L.REC_OFF_MAIL
    ck("S2 GCI: mail[0] is byte-equal to the guest's letter with recipient AND sender re-keyed to the NEW PID; mail[1..9] unused", p3[mo:mo + L.REC_MAIL_SIZE] == expected_mail(pidA, npid)
       and all(p3[mo + i * L.REC_MAIL_SIZE + 0x2E] == L.MAIL_FONT_UNUSED for i in range(1, L.REC_MAIL_COUNT)))
    ck("S2 GCI: equipment = the net, the furniture bit (word 30 bit 10) is set (OR-ed over the intro bits), hint_count 9, destiny.type 3, maps[0] carried", struct.unpack(">H", p3[0x4A4:0x4A6])[0] == EQUIP_NET
       and struct.unpack(">I", p3[0x1108 + 120:0x1108 + 124])[0] & 0x400 and p3[0x1087] == 9 and p3[0x10A2] == 3 and p3[0x11DC:0x11DC + 8] == b"MAPTOWN " and struct.unpack(">H", p3[0x11E4:0x11E6])[0] == 0x7A7A
       and sum(bin(x).count("1") for x in struct.unpack(">43I", p3[0x1108:0x1108 + 172])) >= 2)
    q = bytearray(0x28)
    q[0] = 0x01
    q[0x0C:0x28] = bytes(range(0x40, 0x40 + 0x1C))
    ck("S2 GCI: the delivery quest is carried byte for byte (deliveries[0]); the FIRST-JOB errand (errands[0]) is NOT carried (cleared: type NONE), the chain errand (errands[1]) IS", p3[0x94:0x94 + 0x28] == bytes(q)
       and p3[0x2EC] != 0x43 and p3[0x2EC + 0x58] == 0x40 and p3[0x2EC + 0x58 + 0x0C:0x2EC + 0x58 + 0x28] == bytes(range(0x80, 0x80 + 0x1C)))
    ck("S2 GCI: state_flags and reset_code are NOT carried (the resident's own defaults)", p3[0x2348:0x234C] != b"\xff\xff\xff\xff" and p3[0x10F4:0x10F8] != bytes.fromhex("DEADBEEF"))
    melody = bytes(gci[MELODY_OFF:MELODY_OFF + 8])
    o, was = seeds["mem_a"]
    now = bytes(gci[o:o + MEM_STRIDE])
    ck("S2 GCI: the villager memory is re-keyed to the NEW PID (host land, town melody); last-talk, friendship, send_reply flag + the letter are byte-identical", now[0:20] == npid and now[0x1C:0x24] != b"OLDTOWN "
       and now[0x28:0x30] == melody and now[0x14:0x1C] == was[0x14:0x1C] and now[0x30:] == was[0x30:])
    ck("S2 GCI: contest_quest.player_id of villager 1 AND of the islander = the NEW PID; the control guest's contest quest (villager 2) is untouched", bytes(gci[seeds["con_a"][0]:seeds["con_a"][0] + 20]) == npid
       and bytes(gci[seeds["con_isl"][0]:seeds["con_isl"][0] + 20]) == npid and bytes(gci[seeds["con_b"][0]:seeds["con_b"][0] + 20]) == pidB)
    fa = bytes(gci[seeds["fish_a"][0]:seeds["fish_a"][0] + 20])
    ck("S2 GCI: fishRecord[0] is never left keyed to the GUEST: re-keyed to the NEW PID, or already purged by the game's own mFR_delete_npc_record (observed: the game purges the seed before the migration, fish_records=0)", fa in (npid, bytes(20)) or fa != pidA)
    o, was = seeds["mem_b"]
    ck("S2 GCI: the control guest's memory is byte-identical (not re-keyed)", bytes(gci[o:o + MEM_STRIDE]) == was)
    ck("S2 GCI: residents 0..2 are byte-identical to the fixture", all(gci_priv(gci, i) == gci_priv(orig, i) for i in range(3)))


def run_s2(rig, args):
    ctx = {}
    fresh_fixture()
    shutil.rmtree(mp_dir(), ignore_errors=True)
    if not setup_s2(rig, ctx, args.port):
        return
    phase_no_durable(rig, ctx, args.port + 1)
    phase_fail_save(rig, ctx, args.port + 2)
    s2_final(rig, ctx)


# ---------------------------------------------------------------------------------------------------------------- S3
def join_two(rig, tag, port, names, wallet=25000):
    """Fresh fixture + P0 for the given guest names; returns ({key: identity}, {key: token}). The stored records are patched to `wallet`."""
    fresh_fixture()
    shutil.rmtree(mp_dir(), ignore_errors=True)
    gs, toks = {}, {}
    h, ok = rig.start(tag + "p0", port)
    if not ok:
        return gs, toks
    try:
        for k, (n, pid) in enumerate(names):
            g = L.guest_identity(n, pid, "HOMETWN", 0x5D21)
            gs[n] = g
            c = rig.join(n, g, bytes(L.fresh_guest_record_for(g)))
            toks[n] = bytes(c.guest_token)
        L.pump_sleep(2.0)
        for c in list(rig.clients):
            rig.release(c)
        L.pump_sleep(7.0)
    finally:
        rig.closeall()
        rig.ck("[%s] P0 graceful stop (exit 0)" % tag, rig.stop(h) == 0)
    for n, g in gs.items():
        rig.ck("[%s] P0 %s's stored record patched (wallet %d)" % (tag, n, wallet), patch_guest_record(mpath("guests.dat"), L.guest_pid_be(g), lambda r: struct.pack_into(">I", r, L.REC_OFF_WALLET, wallet)))
    return gs, toks


def s3a_race(rig, port):
    ck = rig.ck
    gs, toks = join_two(rig, "s3a", port - 1, [("RACEGSTA", 0x4D11), ("RACEGSTB", 0x4D12)])
    if len(gs) != 2:
        return
    h, ok = rig.start("s3a", port, ["--resident-tokens", "tofu"])
    if not ok:
        return
    try:
        a = rig.join("A", gs["RACEGSTA"], bytes(L.fresh_guest_record_for(gs["RACEGSTA"])), token=toks["RACEGSTA"])
        b = rig.join("B", gs["RACEGSTB"], bytes(L.fresh_guest_record_for(gs["RACEGSTB"])), token=toks["RACEGSTB"])
        L.pump_sleep(9.0)
        ck("S3a both guests hold 25000 Bells on the host", L.record_inventory(a.rec_local)[2] == 25000 and L.record_inventory(b.rec_local)[2] == 25000)
        off = len(h.log_text())
        sa = buy(a, rid=1)
        a.resend_txn(sa)       # a byte-identical resend of A's request, in the SAME tick
        sb = buy(b, rid=1)
        ra, rb = a.wait_txn_result(sa.seq, 15.0), b.wait_txn_result(sb.seq, 15.0)
        res = {"A": ra, "B": rb}
        won = [k for k, r in res.items() if r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED]
        lost = [k for k, r in res.items() if r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_REJECTED]
        ck("S3a exactly ONE APPLIED and the other REJECTED NO_RESIDENCE (28), its wallet intact", len(won) == 1 and len(lost) == 1 and res[lost[0]].reason == L.PC_NETGAME_TXN_REASON_NO_RESIDENCE)
        L.pump_sleep(2.5)
        t = h.log_text()[off:]
        wc = {"A": a, "B": b}[won[0]] if won else None
        ck("S3a the winner got exactly ONE TXN_RESULT (the identical resend was DROPPED: its peer was already closed), one 'BOUGHT a house' line, one resident, one charge",
           wc is not None and len([1 for t_, _p in wc.seen if t_ == MSG_TXN_RESULT]) == 1 and t.count("BOUGHT a house") == 1
           and len([e for e in parse_members(mpath("members.dat")) if e["kind"] == 1]) == 1)
    finally:
        rig.closeall()
        ck("S3a graceful stop (exit 0)", rig.stop(h) == 0)


def s3b_crash_after_gci(rig, port):
    ck = rig.ck
    gs, toks = join_two(rig, "s3b", port - 2, [("CRASHGCI", 0x4E11)])
    if not gs:
        return
    g, tok = gs["CRASHGCI"], toks["CRASHGCI"]
    pid = L.guest_pid_be(g)
    rec = bytes(L.fresh_guest_record_for(g))
    h, ok = rig.start("s3b1", port - 1, ["--resident-tokens", "tofu", "--promote-fault", "crash_after_gci"])
    if not ok:
        return
    try:
        c = rig.join("G", g, rec, token=tok)
        L.pump_sleep(9.0)
        sent = buy(c, rid=1)
        code = h.wait_exit(40.0)
        ck("S3b `crash_after_gci`: the host process died right after the GCI save (exit code 98), no TXN_RESULT was sent", code == 98 and c.wait_txn_result(sent.seq, 1.0) is None)
    finally:
        rig.closeall()
        h.stop()
    mem = parse_members(mpath("members.dat")) if os.path.isfile(mpath("members.dat")) else []
    k1 = [e for e in mem if e["kind"] == 1]
    npid = bytes(k1[0]["pid"]) if len(k1) == 1 else None
    gci = raw(gci_path())
    ck("S3b after the crash: the GCI HOLDS the resident (slot 3 = the new PID), members.dat has the credential + the handoff, guests.dat STILL holds the guest (stale)",
       npid is not None and gci_priv(gci, 3)[0:20] == npid and len(mem) == 2 and pid in {e["pid"] for e in parse_guests(mpath("guests.dat"))})
    if npid is None:
        return
    rtok = bytes(k1[0]["token"])
    h, ok = rig.start("s3b2", port, ["--resident-tokens", "tofu"])
    if not ok:
        return
    try:
        t = h.log_text()
        ck("S3b restart: the load-time SWEEP removed the stale guest entry (guests.dat no longer holds the guest; backup made; the handoff + credential stay)",
           pid not in {e["pid"] for e in parse_guests(mpath("guests.dat"))} and "sweep: the guest was promoted" in t and len(parse_members(mpath("members.dat"))) == 2)
        w, rej = rig.attempt("G-claim", g, rec, token=tok)
        ck("S3b the guest claim with its GUEST token gets RESIDENT_HANDOFF (66) then REJECT 6 PROMOTED (the handoff is valid: the resident is in the GCI)", rej is not None and rej.reason == L.PC_NETGAME_REJECT_PROMOTED
           and [t_ for t_, _p in w.seen] == [L.PC_NETGAME_MSG_RESIDENT_HANDOFF, L.PC_NETGAME_MSG_REJECT])
        rig.release(w)
        ident = L.PlayerIdentity(npid[:8], struct.unpack(">H", npid[16:18])[0], 1)
        off = len(h.log_text())
        rc = rig.resident("RES3", ident, 3, rtok)
        t = h.log_text()[off:]
        last = rc.token_msgs[-1][1] if rc.token_msgs else None
        ck("S3b the resident claim with the handed-over token is KNOWN (IDENTITY_TOKEN KNOWN|RESIDENT)", rc.is_connected() and "bound to resident 3 (host-derived)" in t and last is not None
           and last.flags == (L.PC_NETGAME_IDTOKEN_FLAG_RESIDENT | L.PC_NETGAME_IDTOKEN_FLAG_KNOWN))
        L.pump_sleep(2.0)
        mem2 = parse_members(mpath("members.dat"))
        ck("S3b the handoff is gone after the first KNOWN login (members.dat: the credential only, confirmed); guests.dat has no entry of the guest", len(mem2) == 1 and mem2[0]["kind"] == 1 and mem2[0]["confirmed"] == 1
           and pid not in {e["pid"] for e in parse_guests(mpath("guests.dat"))})
        rig.release(rc)
        w2, rej2 = rig.attempt("G-replay", g, rec, token=tok)
        ck("S3b a REPLAYED old guest token is refused (REJECT, not admitted as a guest, not handed off again)", rej2 is not None and rej2.reason != L.PC_NETGAME_REJECT_PROMOTED and not w2.is_connected())
        rig.release(w2)
    finally:
        rig.closeall()
        ck("S3b graceful stop (exit 0)", rig.stop(h) == 0)


def s3c_crash_after_members(rig, port):
    ck = rig.ck
    gs, toks = join_two(rig, "s3c", port - 2, [("CRASHMBR", 0x4F11)])
    if not gs:
        return
    g, tok = gs["CRASHMBR"], toks["CRASHMBR"]
    pid = L.guest_pid_be(g)
    rec = bytes(L.fresh_guest_record_for(g))
    h, ok = rig.start("s3c1", port - 1, ["--resident-tokens", "tofu", "--promote-fault", "crash_after_members"])
    if not ok:
        return
    gci0 = T.md5_file(gci_path())
    try:
        c = rig.join("G", g, rec, token=tok)
        L.pump_sleep(9.0)
        sent = buy(c, rid=1)
        code = h.wait_exit(40.0)
        ck("S3c `crash_after_members`: the host died right after the members.dat write (exit code 97), no TXN_RESULT", code == 97 and c.wait_txn_result(sent.seq, 1.0) is None)
    finally:
        rig.closeall()
        h.stop()
    mem = parse_members(mpath("members.dat")) if os.path.isfile(mpath("members.dat")) else []
    gci = raw(gci_path())
    ents = {e["pid"]: e for e in parse_guests(mpath("guests.dat"))}
    ck("S3c after the crash: members.dat holds the credential + handoff (ORPHANS), the GCI has NO resident in slot 3 (the guest was not promoted), guests.dat still has the guest with its PRE-debit wallet 25000",
       len(mem) == 2 and gci_priv(gci, 3)[0x1086] == 0 and pid in ents and ents[pid]["wallet"] == 25000)
    old_tokens = {bytes(e["token"]) for e in mem}
    h, ok = rig.start("s3c2", port, ["--resident-tokens", "tofu"])
    if not ok:
        return
    try:
        off = len(h.log_text())
        c, rej = rig.attempt("G-back", g, rec, token=tok)
        t = h.log_text()[off:]
        L.pump_sleep(8.0)
        ck("S3c restart: the handoff is INVALID (resident not in the save): the guest is ADMITTED as a guest (no PROMOTED), with its pre-debit wallet 25000", rej is None and c.is_connected()
           and "handoff exists for this guest but is INVALID" in t and L.record_inventory(c.rec_local)[2] == 25000)
        off = len(h.log_text())
        sent = buy(c, rid=1)
        r = c.wait_txn_result(sent.seq, 15.0)
        ck("S3c the next purchase APPLIES", r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED and r.post_wallet == 25000 - PRICE)
        L.pump_sleep(2.0)
        t = h.log_text()[off:]
        mem2 = parse_members(mpath("members.dat"))
        ck("S3c the promotion PRUNED the orphans: log line + members.dat holds exactly the NEW credential + handoff (the crashed run's tokens are gone)",
           "orphaned promotion entr" in t and len(mem2) == 2 and not ({bytes(e["token"]) for e in mem2} & old_tokens - {tok}) and len([e for e in mem2 if e["kind"] == 1]) == 1)
    finally:
        rig.closeall()
        ck("S3c graceful stop (exit 0)", rig.stop(h) == 0)


def run(args, results):
    rig = Rig(results)
    source_audit(rig)
    for name, fn in (("S2", lambda: run_s2(rig, args)), ("S3a", lambda: s3a_race(rig, args.port + 10)), ("S3b", lambda: s3b_crash_after_gci(rig, args.port + 20)),
                     ("S3c", lambda: s3c_crash_after_members(rig, args.port + 30))):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            rig.ck("%s aborted by an exception: %s" % (name, e), False)
            rig.closeall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=type(1), default=12991)
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
