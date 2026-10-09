"""wplay_lib.py - helpers for the REAL-GAMEPLAY wildlife tests (test_wildlife_play_real.py): a disposable fixture whose residents carry a net and a rod, host + client processes started with the
TEST-ONLY autopilot (AC_TEST_AUTOPILOT=<command file>, see pc_net_game.c), and a tiny command / answer protocol over `ac_auto_<label>.txt` and the process logs."""
import os
import re
import struct
import time

import town_test_support as T
import make_four_resident_fixture as MF

NAME = "bin_fixture4_wplay"
BIN = os.path.join(T.BUILD64, NAME)
ITM_NET, ITM_ROD = 0x2200, 0x2203


def make_fixture():
    """fresh disposable copy with the freshly built exe; every resident gets a net (pocket 3) and a rod (pocket 4)"""
    for attempt in range(15):  # a process that was just stopped may still hold the exe for a moment
        try:
            T.make_fixture(NAME)
            break
        except PermissionError:
            time.sleep(1.0)
            if os.path.isdir(BIN) and not os.path.isfile(os.path.join(BIN, T.MARKER)):
                import shutil
                rom = os.path.join(BIN, "rom")
                if os.path.isdir(rom):
                    os.rmdir(rom)  # the junction only, never the ISO behind it
                shutil.rmtree(BIN, ignore_errors=True)  # a half-deleted copy of OUR OWN disposable fixture
    p = os.path.join(BIN, MF.GCI_REL)
    g = bytearray(open(p, "rb").read())
    for slot in range(4):
        base = MF.MAIN_OFF + MF.PRIV_OFF + slot * MF.PRIV_STRIDE
        pk = list(struct.unpack(">15H", g[base + 0x68:base + 0x68 + 30]))
        pk[3], pk[4] = ITM_NET, ITM_ROD
        g[base + 0x68:base + 0x68 + 30] = struct.pack(">15H", *pk)
    m = bytearray(g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE])
    m[MF.CHK_OFF:MF.CHK_OFF + 2] = struct.pack(">H", MF.checksum(bytes(m)))
    g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE] = m
    g[MF.BACK_OFF:MF.BACK_OFF + MF.SAVE_SECTOR_SIZE] = g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_SECTOR_SIZE]
    open(p, "wb").write(bytes(g))


class Player:
    """one game process (host or client) with an autopilot"""

    def __init__(self, L, label, proc):
        self.L, self.label, self.proc = L, label, proc
        self.file = "ac_auto_%s.txt" % label
        self.next_id = 1

    def log(self):
        return self.proc.log_text()

    def send(self, line):
        """write one autopilot command without waiting; returns its id (wait() collects the answer)"""
        cid = self.next_id
        self.next_id += 1
        with open(os.path.join(BIN, self.file), "w") as f:
            f.write("%d %s\n" % (cid, line))
        return cid

    def cmd(self, line, timeout=60.0):
        """send one autopilot command, wait for its [AUTO] answer; returns the answer line (or None)"""
        return self.wait(self.send(line), timeout)

    def wait(self, cid, timeout=60.0):
        m = self.proc.wait_for_log(r"\[AUTO\] (?:done %d \S+ (ok|fail|rejected|gone)[^\r\n]*|pos %d [^\r\n]*|tp %d [^\r\n]*|bugs %d\b[^\r\n]*|pres %d end|give %d [^\r\n]*|killfish %d [^\r\n]*|resetstamps %d done|pspawn %d [^\r\n]*|pdespawn %d [^\r\n]*|hspawn %d [^\r\n]*)" % (cid, cid, cid, cid, cid, cid, cid, cid, cid, cid, cid), timeout)
        return None if m is None else m.group(0)

    def pos(self):
        a = self.cmd("pos", 20.0)
        m = re.search(r"x=([-\d.]+) z=([-\d.]+) block=\((\d+),(\d+)\)", a or "")
        return (float(m.group(1)), float(m.group(2)), int(m.group(3)), int(m.group(4))) if m else None

    def walk(self, x, z, timeout=60.0, radius=20):
        a = self.cmd("walk %.1f %.1f %d" % (x, z, radius), timeout)
        return a is not None and " ok" in a, a

    def bugs(self):
        """[(entity, species, x, z)] bug entities with a LIVE local actor in this process"""
        a = self.cmd("bugs", 20.0) or ""
        return [(int(m.group(1)), int(m.group(2)), float(m.group(3)), float(m.group(4))) for m in re.finditer(r"(\d+):(\d+):(-?\d+):(-?\d+)", a.split(" ", 3)[-1] if a else "")]
