#!/usr/bin/env python3
"""extract_hni_win_v.py - TEST TOOL (read-only): extracts the gyroid window vertex table hni_win_v (0x800 bytes at offset 0x710540 of the Yaz0-decoded
foresta.rel, exactly what pc_assets.c loads at runtime: rom_src 0 = REL, SWAP_VTX) from the user's disc image into a SCRATCH file (native byte order,
16-byte Vtx: ob[3] s16, flag u16, tc[2] s16, cn[4] u8) for dresser_layout_selftest. The ROM data is never written into the repository.
Usage: python extract_hni_win_v.py <disc.iso> <out.bin>"""
import struct
import sys

OFF, SIZE = 0x710540, 0x800


def fst_find(f, name):
    f.seek(0x424)
    fst_off, fst_size = struct.unpack(">II", f.read(8))
    f.seek(fst_off)
    fst = f.read(fst_size)
    n = struct.unpack(">I", fst[8:12])[0]
    strtab = fst[n * 12:]
    for i in range(1, n):
        flags_name, a, b = struct.unpack(">III", fst[i * 12:i * 12 + 12])
        if flags_name >> 24:
            continue  # a directory
        no = flags_name & 0xFFFFFF
        nm = strtab[no:strtab.index(b"\0", no)].decode("ascii", "replace")
        if nm == name:
            return a, b
    return None


def yaz0(src, need):
    dst_size = struct.unpack(">I", src[4:8])[0]
    need = min(need, dst_size)
    dst = bytearray()
    sp = 16
    while len(dst) < need:
        code = src[sp]
        sp += 1
        for bit in range(8):
            if len(dst) >= need:
                break
            if code & (0x80 >> bit):
                dst.append(src[sp])
                sp += 1
            else:
                b1, b2 = src[sp], src[sp + 1]
                sp += 2
                dist = ((b1 & 0xF) << 8 | b2) + 1
                n = b1 >> 4
                if n == 0:
                    n = src[sp] + 0x12
                    sp += 1
                else:
                    n += 2
                start = len(dst) - dist
                for k in range(n):
                    dst.append(dst[start + k])
    return bytes(dst)


def main():
    iso, out = sys.argv[1], sys.argv[2]
    with open(iso, "rb") as f:
        hit = fst_find(f, "foresta.rel.szs")
        if hit is None:
            print("foresta.rel.szs not found")
            return 1
        f.seek(hit[0])
        raw = f.read(hit[1])
    rel = yaz0(raw, OFF + SIZE) if raw[:4] == b"Yaz0" else raw
    be = rel[OFF:OFF + SIZE]
    le = bytearray()
    for i in range(0, SIZE, 16):
        x, y, z, fl, s, t = struct.unpack(">hhhHhh", be[i:i + 12])
        le += struct.pack("<hhhHhh", x, y, z, fl, s, t) + be[i + 12:i + 16]
    with open(out, "wb") as g:
        g.write(le)
    v0 = struct.unpack("<hhh", le[0:6])
    print("wrote %s (%d vertices; v[0] = %s)" % (out, SIZE // 16, v0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
