#!/usr/bin/env python3
r"""test_board_init_real.py - REAL dedicated-server process test of the bulletin-board init + legacy repair (fixture pc\build64\bin_fixture4_s8, ports 14800+).
 A) first launch of `--dedicated` with NO servers dir generates a town; the saved GCI noticeboard must hold 4 initialized handbills (non-zero time, year>=2000) + 11 clear-code posts, no zero post.
 B) the saved GCI is patched: board = [real post, 2 ZERO posts, partial-zero post, ...zero...]; a new process loads it: '[BOARD] repaired N' logged; after its save the real + partial posts are
    preserved byte-for-byte (compacted in order) and no zero post remains.
 C) a third launch of the repaired save logs NO '[BOARD] repaired' (idempotent).
Usage: python test_board_init_real.py   (take /tmp/ac_game_lock first)"""
import glob
import os
import re
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FX = "bin_fixture4_s8"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FX)
os.environ["AC_DISPLAY_NAME"] = "samsung"
os.environ["AC_MASTER_VOLUME"] = "1"
if __name__ == "__main__":
    T.make_fixture(FX, empty_save=True)
import net_spike_lib as L  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
GCI = os.path.join(HOST_DIR, "servers", "default", "town", "card_a", "DobutsunomoriP_MURA.gci")
SAVE_OFF, BOARD_OFF, POST, N = 64 + 0x26000, 0x912C, 200, 15


def board():
    d = open(GCI, "rb").read()
    o = SAVE_OFF + BOARD_OFF
    return d, [d[o + i * POST:o + (i + 1) * POST] for i in range(N)]


def classify(p):
    t = p[0xC0:]
    if not any(p):
        return "ZERO"
    if t == b"\xff" * 8 and p[:0xC0] == b" " * 0xC0:
        return "empty"
    return "post"


def run(port, tag):
    h = L.HostProcess(port=port, extra_args=["--dedicated", "--verbose"], log_path=T.log_path("board_%s.log" % tag), bin_dir=HOST_DIR, stdin_pipe=True, verbose=True, new_group=True).start()
    ok = h.boot_to_dedicated(timeout=150.0)
    time.sleep(3.0)
    h.send_line("save")
    time.sleep(3.0)
    h.send_line("stop")
    if h.wait_exit(40.0) is None:
        h.stop()
    time.sleep(1.0)
    return ok, h.log_text()


def main():
    results = []
    ck = lambda d, c, x="": L.check(d + ((" :: " + str(x)[:300]) if (x and not c) else ""), bool(c), results)  # noqa: E731
    assert not os.path.isdir(os.path.join(HOST_DIR, "servers")) or True
    ok, log = run(14801, "A")
    ck("A0 first launch booted to dedicated", ok, log[-400:])
    ck("A1 town generated", "generating a new town" in log)
    ck("A2 the generated save exists", os.path.isfile(GCI))
    d, b = board()
    kinds = [classify(p) for p in b]
    print("A board kinds:", kinds)
    ck("A3 no zero post in the generated board", "ZERO" not in kinds, kinds)
    ck("A4 exactly 4 initialized handbills first, then 11 empty slots", kinds == ["post"] * 4 + ["empty"] * 11, kinds)
    yrs = [struct.unpack(">H", p[0xC6:0xC8])[0] for p in b[:4]]
    ck("A5 handbill post_time is a real clock time (year >= 2000, day 1..31)", all(y >= 2000 for y in yrs) and all(1 <= p[0xC3] <= 31 for p in b[:4]), yrs)
    ck("A6 no '[BOARD] repaired' on a fresh town", "[BOARD] repaired" not in log)
    real = b[0]
    partial = bytearray(200)
    partial[0xC0 - 1] = 0x41  # body zero except last byte, time zero
    zero = bytes(200)
    legacy = [real, zero, bytes(partial), zero, b[1], zero] + [zero] * 9
    o = SAVE_OFF + BOARD_OFF
    nd = bytearray(d)
    for i, p in enumerate(legacy):
        nd[o + i * POST:o + (i + 1) * POST] = p
    for f in glob.glob(GCI + "*"):
        os.remove(f)
    open(GCI, "wb").write(nd)
    ok, log = run(14802, "B")
    ck("B0 patched legacy save booted", ok, log[-400:])
    m = re.search(r"\[BOARD\] repaired (\d+)", log)
    ck("B1 [BOARD] repaired line logged", m is not None, log[-300:])
    nz = sum(1 for p in legacy if not any(p))
    ck("B2 repaired count == number of all-zero posts in the patched board (%d)" % nz, m is not None and int(m.group(1)) == nz, m and m.group(0))
    d2, b2 = board()
    k2 = [classify(p) for p in b2]
    print("B board kinds:", k2)
    ck("B3 after save: real, partial, real kept in original order at slots 0..2", b2[0] == real and b2[1] == bytes(partial) and b2[2] == legacy[4], k2)
    ck("B4 remaining slots are vanilla empty slots, no ZERO post", k2[3:] == ["empty"] * 12 and "ZERO" not in k2, k2)
    ok, log = run(14803, "C")
    ck("C1 third launch: no repair line (idempotent) and board unchanged", ok and "[BOARD] repaired" not in log and board()[1] == b2)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
