#!/usr/bin/env python3
r"""test_board_init_src.py - bulletin board "iiiii" fix (SOURCE tier + a python model of the repair predicate; no game process).

Checks: (B1) a dedicated first-launch town runs mNtc_SetInitData(); (B2) the load path repairs zeroed posts after the byte swap; (B3) the repair predicate only matches an
all-zero 0xC0 body + all-zero time and restores the vanilla empty-slot form (CHAR_SPACE body, clear code), compacting survivors; (B4) vanilla SetInitData/ClearPage untouched;
(B5) python model of the predicate: zeroed -> repaired, legitimate / empty / partially-zero posts never touched.
Usage: python test_board_init_src.py"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BODY, TIME, CLEAR, SPACE, COUNT = 0xC0, 8, 0xFF, 0x20, 15


def rd(*p):
    return open(os.path.join(REPO, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def repair(board):
    """model of pc_board_repair_zeroed_posts: board = list of (body bytes, time bytes)"""
    n = 0
    out = []
    for b, t in board:
        if not any(b) and not any(t):
            out.append((bytes([SPACE]) * BODY, bytes([CLEAR]) * TIME))
            n += 1
        else:
            out.append((b, t))
    if n:
        keep = [p for p in out if p[1] != bytes([CLEAR]) * TIME]
        out = keep + [p for p in out if p[1] == bytes([CLEAR]) * TIME]
    return out, n


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    mc = rd("pc", "src", "pc_m_card.c")
    nt = rd("src", "game", "m_notice.c")

    gen = re.search(r"static int pc_server_generate_town\(GAME_PLAY\* play\) \{.*?\n\}\n", mc, re.S).group(0)
    ck("B1 pc_server_generate_town calls mNtc_SetInitData() after the NEW init and before the save write", "mNtc_SetInitData();" in gen and gen.find("mSDI_StartDataInit") < gen.find("mNtc_SetInitData();") < gen.find("pc_save_write_authoritative"))
    rd_fn = re.search(r"static int pc_save_read_gci\(const char\* path\) \{.*?\n\}\n", mc, re.S).group(0)
    ck("B2 pc_save_read_gci repairs the board right after the FROM_BE byte swap", re.search(r"pc_save_bswap\(&common_data\.save\.save, PC_BSWAP_FROM_BE\);\s*pc_board_repair_zeroed_posts\(common_data\.save\.save\.noticeboard, mNtc_BOARD_POST_COUNT\)", rd_fn))
    pred = re.search(r"static int pc_board_post_is_zeroed.*?\n\}\n", mc, re.S).group(0)
    ck("B3a predicate scans every byte of the whole post (body + time) for != 0", "sizeof(mNtc_board_post_c)" in pred and "!= 0" in pred and "return 1;" in pred)
    fix = re.search(r"int pc_board_repair_zeroed_posts.*?\n\}\n", mc, re.S).group(0)
    ck("B3b repair writes the vanilla empty-slot form (clear code + CHAR_SPACE body) only for zeroed posts", "pc_board_post_is_zeroed(&board[i])" in fix and "mTM_rtcTime_clear_code" in fix and "memset(board[i].message, CHAR_SPACE, MAIL_BODY_LEN)" in fix)
    ck("B3c logs one [BOARD] line when it repairs", "[BOARD] repaired" in fix)
    ck("B3d compaction only when something was repaired", re.search(r"if \(repaired > 0\) \{.*?compact|if \(repaired > 0\) \{", fix, re.S))
    ck("B4 vanilla ClearPage + SetInitData untouched", "page->post_time = mTM_rtcTime_clear_code;" in nt and "mem_clear(page->message, MAIL_BODY_LEN, CHAR_SPACE);" in nt and "mNtc_ClearPage(post, mNtc_BOARD_POST_COUNT - 4);" in nt)

    z = (bytes(BODY), bytes(TIME))
    legit = (b"Hello" + bytes([SPACE]) * (BODY - 5), bytes([0, 1, 2, 3, 4, 5, 6, 7]))
    empty = (bytes([SPACE]) * BODY, bytes([CLEAR]) * TIME)
    part_body = (bytes([0] * (BODY - 1)) + b"A", bytes(TIME))
    part_time = (bytes(BODY), bytes([0] * 7 + [1]))
    b, n = repair([z] * COUNT)
    ck("B5a an all-zero board becomes 15 empty slots", n == COUNT and all(p == empty for p in b))
    b, n = repair([legit, z, legit, empty])
    ck("B5b a zeroed post between legit ones is cleared, survivors compacted in order, nothing legit modified", n == 1 and b[0] == legit and b[1] == legit and b[2] == empty and b[3] == empty)
    b, n = repair([legit, empty, part_body, part_time])
    ck("B5c legit / empty / partially-zero posts are never touched (n == 0, identity)", n == 0 and b == [legit, empty, part_body, part_time])
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
