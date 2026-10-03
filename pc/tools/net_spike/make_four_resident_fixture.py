#!/usr/bin/env python3
"""Build the disposable FOUR-RESIDENT multiplayer test fixture (M9 identity Stage 1A, test-only tooling).

Why: since Stage 1A the host admits a client only when its claimed PersonalID matches an existing resident of the HOST's
save (not the host's own resident, not already bound to another READY peer).  The pc/build64/bin_talkfix test save has
only two residents (slot 0 'Yubel' = host, slot 1 'Angelica'), so a second simultaneous client is refused (SERVER_FULL).
This tool derives pc/build64/bin_fixture4 from bin_talkfix with residents 2 and 3 populated.  No production code, flag or
identity rule is involved; the game loads the fixture like any other save.

Usage (from anywhere):
    python make_four_resident_fixture.py [--src <bin_talkfix>] [--dest <bin_fixture4>] [--ambiguous] [--force]
    python make_four_resident_fixture.py --clone-talkfix [--force]     -> pc/build64/bin_talkfix_clone (2-resident save, byte copy)
bin_talkfix is a PROTECTED artifact (the harness refuses to launch a game from it, net_spike_lib.require_launchable_bin_dir):
run what used to run on bin_talkfix on bin_talkfix_clone instead.  bin_talkfix is only ever READ by this tool.
Then run a multi-client test with   NET_SPIKE_GAME_BIN=<absolute path of bin_fixture4>   (host = resident 0).

Safety: READ-ONLY on the source save (only its .gci is read); writes ONLY inside --dest; refuses the live pc/build64/bin,
the source dir itself, and any dest outside pc/build64; refuses to replace an existing dest unless it carries this tool's
marker file (.four_resident_fixture) AND --force is given.  `rom` is a directory JUNCTION to the (read-only) ISO dir,
exactly like bin_talkfix (1.4 GB, not copied).

Layout facts (every one derived from repository source; nothing guessed):
  * GCI = CARDDir header (include/m_card.h; pc_m_card.c GCI_HEADER_SIZE = 0x40) + file data 0x72000
    (pc_m_card.c:63 GCI_FILE_DATA_SIZE = mCD_LAND_SAVE_SIZE) -> total 0x72040 = 467008 bytes.
  * Save_t main copy at file-data offset 0x26000 (pc_m_card.c:65 GCI_SAVE_MAIN_OFFSET), backup copy at 0x4C000
    (pc_m_card.c:66 = OTHERS_SIZE + sizeof(Save)); the writer makes backup = memcpy(main, sizeof(Save))
    (pc_m_card.c:390), i.e. also the bytes after sizeof(Save_t) up to the sector-aligned size.
  * Save_t: private_data[4] at +0x20 (include/m_common_data.h:90), homes[4] at +0x9CE8 (m_common_data.h:94), stride of
    Private_c 0x2440 (m_private.h:190-266), stride of mHm_hs_c 0x26B0 (m_home_h.h "sizeof(mHm_hs_c) == 0x26B0"),
    sizeof(Save_t) = 0x242A0 (m_common_data.h:173 last member offset).
  * PersonalID_c (m_personal_id.h:15-20): name[8], land_name[8], u16 player_id, u16 land_id (big-endian in the GCI,
    pc_save_bswap.c swaps it on load); Private_c.exists at +0x1086 (m_private.h:215); an empty slot has land_id 0xFFFF.
    mHm_hs_c.ownerID is a PersonalID_c at +0 (m_home_h.h).
  * Checksum (pc_m_card.c:380-390 writer; pc_save_bswap.c:1012 pc_checksum_be == mFRm_GetFlatCheckSum, m_flashrom.c:138):
    mFRm_chk_t.checksum is the BE u16 at Save_t+0x12 (m_flashrom.h:33-39); computed with that field zeroed over
    sizeof(Save_t) bytes as the two's complement of the sum of BE u16 words.  NOTE: pc_save_read_gci() does NOT verify
    this checksum (pc_m_card.c:610-640 only bswap-roundtrips), but it is recomputed here exactly as the writer does, and
    asserted against the source save first (so the algorithm is proven on a game-written file).
Residents 2/3 are byte copies of resident 1's Private_c and home (an initialised, valid record) with only the PersonalID
name / player_id changed (same town land name + land_id as the host's town); exists stays TRUE; house_arrangement is
left untouched (default identity mapping, so house index == player index, m_house.c:22 mHS_house_init DEFAULT_ARRANGEMENT).
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
LIVE_BIN = os.path.join(BUILD64, "bin")
DEF_SRC = os.path.join(BUILD64, "bin_talkfix")
DEF_DEST = os.path.join(BUILD64, "bin_fixture4")
DEF_DEST_AMBIG = os.path.join(BUILD64, "bin_fixture4_ambig")
DEF_DEST_CLONE = os.path.join(BUILD64, "bin_talkfix_clone")
CLONE_MARKER = ".talkfix_clone"
MARKER = ".four_resident_fixture"
GCI_REL = os.path.join("save", "card_a", "DobutsunomoriP_MURA.gci")

GCI_HEADER = 0x40
FILE_DATA = 0x72000
MAIN_OFF = GCI_HEADER + 0x26000
BACK_OFF = GCI_HEADER + 0x4C000
SAVE_T_SIZE = 0x242A0
SAVE_SECTOR_SIZE = BACK_OFF - MAIN_OFF  # sizeof(Save) = 0x26000
PRIV_OFF, PRIV_STRIDE, EXISTS_OFF = 0x20, 0x2440, 0x1086
HOME_OFF, HOME_STRIDE = 0x9CE8, 0x26B0
CHK_OFF = 0x12

NEW_RESIDENTS = {2: (b"Bella   ", 0xF1A2), 3: (b"Cleo    ", 0xF2B3)}
# --ambiguous variant: slot 2 carries EXACTLY resident 1's PersonalID (name, player_id, land) -- a plain second record with
# the same identity, i.e. what mPr_CheckCmpPersonalID cannot tell apart (the host classifier's AMBIGUOUS branch).
NEW_RESIDENTS_AMBIG = {2: (b"Angelica", 0xF081), 3: (b"Cleo    ", 0xF2B3)}


def norm(p):
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def checksum(save_t):
    """pc_checksum_be(save_t, size, 0) with the checksum field zeroed (what pc_save_write_gci_to stores)."""
    b = bytearray(save_t)
    b[CHK_OFF:CHK_OFF + 2] = b"\0\0"
    s = sum(struct.unpack(">%dH" % (len(b) // 2), bytes(b)))
    return (~(s & 0xFFFF) + 1) & 0xFFFF


def stored_checksum(save_t):
    return struct.unpack(">H", save_t[CHK_OFF:CHK_OFF + 2])[0]


def parse_residents(gci):
    """[(slot, name, player_id, land_name, land_id, exists)] for the 4 private_data records."""
    out = []
    for i in range(4):
        o = MAIN_OFF + PRIV_OFF + i * PRIV_STRIDE
        pid, lid = struct.unpack(">HH", gci[o + 16:o + 20])
        out.append((i, gci[o:o + 8], pid, gci[o + 8:o + 16], lid, gci[o + EXISTS_OFF]))
    return out


def derive_gci(src_gci, new_residents=None):
    new_residents = new_residents or NEW_RESIDENTS
    assert len(src_gci) == GCI_HEADER + FILE_DATA, "unexpected GCI size %d" % len(src_gci)
    main = src_gci[MAIN_OFF:MAIN_OFF + SAVE_T_SIZE]
    assert stored_checksum(main) == checksum(main), "source checksum does not validate: algorithm/layout mismatch, STOP"
    res = parse_residents(src_gci)
    assert res[0][5] == 1 and res[1][5] == 1 and res[2][4] == 0xFFFF and res[3][4] == 0xFFFF and res[2][5] == 0 and res[3][5] == 0, \
        "source save is not the expected 2-resident layout: %r" % (res,)
    land_name, land_id = res[0][3], res[0][4]
    out = bytearray(src_gci)
    for slot, (name, pid) in new_residents.items():
        p1 = MAIN_OFF + PRIV_OFF + 1 * PRIV_STRIDE
        pn = MAIN_OFF + PRIV_OFF + slot * PRIV_STRIDE
        out[pn:pn + PRIV_STRIDE] = out[p1:p1 + PRIV_STRIDE]               # resident 1's valid record
        out[pn:pn + 8] = name
        out[pn + 8:pn + 16] = land_name
        out[pn + 16:pn + 20] = struct.pack(">HH", pid, land_id)
        h1 = MAIN_OFF + HOME_OFF + 1 * HOME_STRIDE
        hn = MAIN_OFF + HOME_OFF + slot * HOME_STRIDE
        out[hn:hn + HOME_STRIDE] = out[h1:h1 + HOME_STRIDE]               # resident 1's valid house, new owner
        out[hn:hn + 8] = name
        out[hn + 8:hn + 16] = land_name
        out[hn + 16:hn + 20] = struct.pack(">HH", pid, land_id)
        assert out[pn + EXISTS_OFF] == 1
    main = bytearray(out[MAIN_OFF:MAIN_OFF + SAVE_T_SIZE])
    main[CHK_OFF:CHK_OFF + 2] = struct.pack(">H", checksum(bytes(main)))
    out[MAIN_OFF:MAIN_OFF + SAVE_T_SIZE] = main
    out[BACK_OFF:BACK_OFF + SAVE_SECTOR_SIZE] = out[MAIN_OFF:MAIN_OFF + SAVE_SECTOR_SIZE]  # writer: backup = copy of main
    return bytes(out)


def verify(dest, ambiguous=False):
    gci = open(os.path.join(dest, GCI_REL), "rb").read()
    main = gci[MAIN_OFF:MAIN_OFF + SAVE_T_SIZE]
    assert stored_checksum(main) == checksum(main), "fixture main checksum invalid"
    assert gci[BACK_OFF:BACK_OFF + SAVE_SECTOR_SIZE] == gci[MAIN_OFF:MAIN_OFF + SAVE_SECTOR_SIZE], "backup != main"
    res = parse_residents(gci)
    keys = set()
    for slot, name, pid, ln, lid, ex in res:
        assert ex == 1 and lid == res[0][4] and ln == res[0][3], "slot %d invalid" % slot
        keys.add((name, pid, ln, lid))
    assert len(keys) == (3 if ambiguous else 4), "unexpected number of distinct residents"
    # cross-check with the harness parser (independent code path in net_spike_lib)
    sys.path.insert(0, HERE)
    import net_spike_lib as L
    parsed = L.read_test_save_residents(dest)
    assert [(i, bytes(p.player_name), p.player_id, ex) for i, p, ex in parsed] == \
        [(s, n, p, True) for s, n, p, _l, _i, _e in res], "harness parser disagrees: %r" % (parsed,)
    return res


def clone_talkfix(src, dest):
    """Disposable full copy of bin_talkfix (exe, dll, ini, shaders, shader cache) with the save .gci copied BYTE-FOR-BYTE
    (only the main .gci; backups/other files are not copied); rom = junction to the live ISO dir. Read-only on src."""
    src_gci = os.path.join(src, GCI_REL)
    os.makedirs(os.path.join(dest, "save", "card_a"))
    os.makedirs(os.path.join(dest, "save", "card_b"))
    for f in ("AnimalCrossing.exe", "SDL2.dll", "keybindings.ini", "settings.ini", "shader_cache.bin"):
        if os.path.isfile(os.path.join(src, f)):
            shutil.copy2(os.path.join(src, f), os.path.join(dest, f))
    shutil.copytree(os.path.join(src, "shaders"), os.path.join(dest, "shaders"))
    shutil.copy2(src_gci, os.path.join(dest, GCI_REL))
    r = subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(dest, "rom"), os.path.join(LIVE_BIN, "rom")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("mklink /J failed: " + r.stdout + r.stderr)
    open(os.path.join(dest, CLONE_MARKER), "w").write("disposable clone of bin_talkfix; see make_four_resident_fixture.py\n")
    a, b = open(src_gci, "rb").read(), open(os.path.join(dest, GCI_REL), "rb").read()
    assert a == b, "clone save differs from source"
    res = parse_residents(b)
    assert res[0][5] == 1 and res[1][5] == 1 and res[2][5] == 0, "unexpected clone residents"
    print("clone:", dest, "(save byte-identical to bin_talkfix; residents %r %r)" % (res[0][1], res[1][1]))
    print("Run tests with NET_SPIKE_GAME_BIN=" + dest)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", default=DEF_SRC)
    ap.add_argument("--dest", default=None)
    ap.add_argument("--ambiguous", action="store_true",
                    help="variant with slot 2 == slot 1's PersonalID (default dest bin_fixture4_ambig)")
    ap.add_argument("--clone-talkfix", action="store_true",
                    help="build the disposable 2-resident clone pc/build64/bin_talkfix_clone (exe/dll/ini/shaders + the save "
                         "copied byte-for-byte; rom junction) instead of a 4-resident fixture")
    ap.add_argument("--force", action="store_true", help="rebuild an existing fixture dir (only if it has the marker file)")
    a = ap.parse_args()
    src = os.path.abspath(a.src)
    dest = os.path.abspath(a.dest or (DEF_DEST_CLONE if a.clone_talkfix else DEF_DEST_AMBIG if a.ambiguous else DEF_DEST))
    if norm(dest) == norm(DEF_SRC):
        sys.exit("REFUSING: dest may never be the protected bin_talkfix")
    if norm(dest) in (norm(LIVE_BIN), norm(src)) or os.path.dirname(norm(dest)) != norm(BUILD64):
        sys.exit("REFUSING: dest must be a NEW directory directly under pc/build64 (not the live bin, not the source)")
    if norm(src) == norm(LIVE_BIN):
        sys.exit("REFUSING: the live bin dir is never a source")
    if os.path.exists(dest):
        if not (a.force and os.path.isfile(os.path.join(dest, CLONE_MARKER if a.clone_talkfix else MARKER))):
            sys.exit("REFUSING: %s exists (pass --force only for a dir created by this tool)" % dest)
        os.rmdir(os.path.join(dest, "rom")) if os.path.isdir(os.path.join(dest, "rom")) else None  # junction only, never the ISO
        shutil.rmtree(dest)
    if a.clone_talkfix:
        return clone_talkfix(src, dest)
    new_gci = derive_gci(open(os.path.join(src, GCI_REL), "rb").read(),
                        NEW_RESIDENTS_AMBIG if a.ambiguous else NEW_RESIDENTS)      # read-only on the source save
    os.makedirs(os.path.join(dest, "save", "card_a"))
    os.makedirs(os.path.join(dest, "save", "card_b"))
    for f in ("AnimalCrossing.exe", "SDL2.dll", "keybindings.ini", "settings.ini"):
        shutil.copy2(os.path.join(src, f), os.path.join(dest, f))
    shutil.copytree(os.path.join(src, "shaders"), os.path.join(dest, "shaders"))
    with open(os.path.join(dest, GCI_REL), "wb") as f:
        f.write(new_gci)
    r = subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(dest, "rom"), os.path.join(LIVE_BIN, "rom")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("mklink /J failed: " + r.stdout + r.stderr)
    open(os.path.join(dest, MARKER), "w").write("disposable test fixture; see make_four_resident_fixture.py\n")
    res = verify(dest, a.ambiguous)
    print("fixture:", dest)
    for slot, name, pid, ln, lid, ex in res:
        print("  slot %d %r id=0x%04X land=%r land_id=0x%04X exists=%d" % (slot, name, pid, ln, lid, ex))
    print("OK (host = resident 0; clients = 1,2,3). Run tests with NET_SPIKE_GAME_BIN=" + dest)


if __name__ == "__main__":
    main()
