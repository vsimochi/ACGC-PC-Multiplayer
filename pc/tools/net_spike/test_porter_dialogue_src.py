#!/usr/bin/env python3
r"""test_porter_dialogue_src.py - Porter (the station master monkey): the vanilla "going somewhere? -> Yes -> memory-card / passport trip back to another village" offer is replaced.

TIER: SOURCE + the native message selftest (pc_nook_house_selftest through test_work_menu_patch.py covers every generated row's bytes). No game process, no gameplay.
Checks: (S1) the generated row PC_NOOK_MSG_PORTER_STATION exists, is counted, ends with END, has no choice, uses only the font-safe charset and short lines; (S2) the station master's NORMAL
talk (aSTM_set_norm_talk_info) takes the new message for the two CHK_LEAVE rows (0x0943 resident / 0x095C guest) under TARGET_PC and ends the talk with END_WAIT (no CHK_LEAVE_TALK2 /
CARDPROC route can be entered from the PC build's talk); (S3) the vanilla (non-TARGET_PC) code is untouched; (S4) the other Porter rows (greeting 0x0831 / 0x07ED, forced welcome 0x07DD /
0x0966, the no-save 0x0945 case) are unchanged; (S5) the exact replacement text.
Usage: python test_porter_dialogue_src.py"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TEXT = [
    ["Welcome to the station!", "This station only serves", "this town, you see."],
    ["Visits with other players", "are arranged online, by", "connecting to a host."],
]


def rd(*p):
    return open(os.path.join(REPO, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    hdr = rd("pc", "include", "pc_nook_house.h")
    src = rd("pc", "src", "pc_nook_house.c")
    talk = rd("src", "actor", "npc", "ac_npc_station_master_talk.c_inc")
    stm = rd("src", "actor", "npc", "ac_npc_station_master.c")

    enum = re.search(r"enum \{\n    PC_NOOK_MSG_INTRO = 0.*?\n\};", hdr, re.S).group(0)
    names = re.findall(r"^\s*(PC_NOOK_MSG_[A-Z0-9_]+)", enum, re.M)
    count = int(re.search(r"#define PC_NOOK_MSG_COUNT (\d+)", hdr).group(1))
    ck("S1 PC_NOOK_MSG_PORTER_STATION is the last generated row and PC_NOOK_MSG_COUNT covers it (%d rows, count %d)" % (len(names), count), names[-1] == "PC_NOOK_MSG_PORTER_STATION" and count == len(names))
    body = re.search(r"case PC_NOOK_MSG_PORTER_STATION:\n(.*?)break;", src, re.S).group(1)
    ck("S1 the row ends with NK_END and has no choice / select code", "nk_code(&b, NK_END);" in body and "nk_choice" not in body and "NK_SEL" not in body)
    lits = re.findall(r'nk_text\(&b, "((?:[^"\\]|\\.)*)"\)', body)
    pages = [[]]
    text_all = []
    for chunk in re.split(r"nk_page\(&b\);", body):
        s = "".join(re.findall(r'nk_text\(&b, "((?:[^"\\]|\\.)*)"\)', chunk))
        lines = s.replace("\\n", "\n").split("\n")
        pages.append([ln for ln in lines if ln != ""])
    pages = pages[1:]
    text_all = [ln for pg in pages for ln in pg]
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 !',-.?:")
    ck("S1 only font-safe characters (letters, digits, space and ! ' , - . ? :): %s" % sorted(set("".join(text_all)) - allowed), set("".join(text_all)) <= allowed)
    ck("S1 every line is at most 28 characters (the longest existing generated line is 28) and a page has at most 3 lines (longest: %d)" % max(len(x) for x in text_all),
       all(len(x) <= 28 for x in text_all) and all(len(pg) <= 3 for pg in pages))
    ck("S5 the exact replacement text: %s" % pages, pages == TEXT)
    ck("S1 the text promises no friend list / travel feature (no 'friend', 'list', 'travel', 'ticket', 'invite')", not re.search(r"friend|list|travel|ticket|invite", " ".join(text_all), re.I))

    ck("S2 ac_npc_station_master.c includes pc_nook_house.h only under TARGET_PC", re.search(r"#ifdef TARGET_PC\n#include \"pc_nook_house.h\"[^\n]*\n#endif", stm) is not None)
    norm = re.search(r"static void aSTM_set_norm_talk_info\(.*?\n\}\n", talk, re.S).group(0)
    pc_branch = re.search(r"#ifdef TARGET_PC\n        \} else if \(data_p->talk_idx == aSTM_TALK_CHK_LEAVE_TALK\) \{(.*?)#endif", norm, re.S)
    ck("S2 the normal talk has a TARGET_PC branch for the CHK_LEAVE rows", pc_branch is not None)
    pb = pc_branch.group(1) if pc_branch else ""
    ck("S2 the branch sets the generated message and ends the talk with aSTM_TALK_END_WAIT", "pc_nook_msg_id(PC_NOOK_MSG_PORTER_STATION)" in pb and "aSTM_TALK_END_WAIT" in pb
       and "CHK_LEAVE_TALK2" not in pb and "CARDPROC" not in pb)
    rows = re.search(r"static aSTM_talk_data_c dt_tbl\[\] = \{(.*?)\};", norm, re.S).group(1)
    ck("S2 the two CHK_LEAVE rows (0x0943 residents, 0x095C guests) are still the rows the branch replaces", len(re.findall(r"aSTM_TALK_CHK_LEAVE_TALK, CAMERA2_PROCESS_TALK", rows)) == 2
       and "0x0943" in rows and "0x095C" in rows)
    nopc = re.sub(r"#ifdef TARGET_PC.*?#endif\n", "", norm, flags=re.S)
    ck("S3 without TARGET_PC the vanilla function is unchanged: the else branch still uses data_p->msg_no / change_talk_proc(data_p->talk_idx)",
       "mDemo_Set_msg_num(data_p->msg_no);" in nopc and "change_talk_proc(actorx, data_p->talk_idx)" in nopc and "PORTER" not in nopc)
    ck("S4 the other rows are untouched: 0x0831 / 0x07ED greetings, forced 0x07DD / 0x0966 / 0x0965, and the no-save 0x0945 branch",
       all(x in talk for x in ("{0x0831,", "{0x07ED,", "{0x07DD,", "{0x0966,", "{0x0965,", "mDemo_Set_msg_num(0x0945);")))
    ck("S4 the vanilla leave / card route functions still exist (the clip is untouched), they are just no longer reachable from the PC build's normal talk",
       "aSTM_chk_leave_talk2" in rd("src", "actor", "ac_station_clip.c_inc"))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
