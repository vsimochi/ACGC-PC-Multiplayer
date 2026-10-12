#!/usr/bin/env python3
"""make_legacy_dresser_pool.py - MANUAL-TEST helper: write a legacy (v1, per-resident, unreleased) dresser pool file so the one-time v1 -> v2
migration can be watched by a human (see docs/dresser-storage-manual-checklist.md, check 4).

Writes <bin dir>\\save\\mp\\dresser_store.dat as a v1 file holding a pool of N items for one resident of the town in <bin dir>'s save (the resident's
identity key is read from the save, exactly like test_dresser_migration_protocol.py). Run it with the HOST STOPPED, on a DISPOSABLE copy of the game
folder only. Refuses to overwrite an existing dresser_store.dat / .v1.bak unless --force.

  python make_legacy_dresser_pool.py --bin-dir <disposable game folder> --slot 0 --count 60 [--first-item 0x2480]
"""
import argparse
import os
import struct
import sys

SAVE_GCI_REL = os.path.join("save", "card_a", "DobutsunomoriP_MURA.gci")
GCI_PRIVATE_BASE = 0x40 + 0x26000 + 0x20
GCI_PRIVATE_STRIDE = 0x2440
MAGIC = 0x52534344


def fnv(b, h=2166136261):
    for x in b:
        h = ((h ^ x) * 16777619) & 0xFFFFFFFF
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin-dir", required=True, help="a DISPOSABLE copy of the game folder (the one that contains save\\)")
    ap.add_argument("--slot", type=int, default=0, help="resident slot 0..3 whose identity owns the legacy pool")
    ap.add_argument("--count", type=int, default=60, help="items in the pool (1..60)")
    ap.add_argument("--first-item", type=lambda s: int(s, 0), default=0x2480, help="item id of pool slot 0 (the next ones are +1 each)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if not 1 <= a.count <= 60:
        print("--count must be 1..60", file=sys.stderr)
        return 2
    gci = os.path.join(a.bin_dir, SAVE_GCI_REL)
    if not os.path.isfile(gci):
        print("no save at %s" % gci, file=sys.stderr)
        return 2
    side = os.path.join(a.bin_dir, "save", "mp", "dresser_store.dat")
    for p in (side, side + ".v1.bak"):
        if os.path.exists(p) and not a.force:
            print("REFUSING: %s already exists (use --force only on a disposable copy)" % p, file=sys.stderr)
            return 2
    with open(gci, "rb") as f:
        f.seek(GCI_PRIVATE_BASE + a.slot * GCI_PRIVATE_STRIDE)
        raw = f.read(20)
    name, land = raw[0:8], raw[8:16]
    pid, land_id = struct.unpack(">HH", raw[16:20])
    key = name + struct.pack("<HHI", pid, land_id, fnv(land))
    rec = bytearray(680)
    rec[0:16] = key
    for i in range(a.count):
        struct.pack_into("<H", rec, 46 + 2 * i, (a.first_item + i) & 0xFFFF)
    hdr = struct.pack("<IIII", MAGIC, 1, 0, 1)
    crc = fnv(hdr)
    crc ^= (fnv(bytes(rec)) + 0 * 0x9E3779B1) & 0xFFFFFFFF
    os.makedirs(os.path.dirname(side), exist_ok=True)
    with open(side, "wb") as f:
        f.write(hdr + bytes(rec) + struct.pack("<I", crc))
    print("wrote a v1 pool of %d item(s) (0x%04X..) for resident slot %d to %s" % (a.count, a.first_item, a.slot, side))
    return 0


if __name__ == "__main__":
    sys.exit(main())
