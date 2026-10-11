#!/usr/bin/env python3
r"""test_rover_free_str_src.py - Rover's train scene (second and later characters / multiplayer guest creation) showed "iiiiiiiiii" where Rover names ANOTHER traveller.

ROOT CAUSE (what this test pins down): Rover's lines 0x2AD9 ("... Ah yes! <STR_FREE0>, I believe.") and 0x2ADC ("<STR_FREE0> was the same way!") print message free string 0. ac_npc_guide2 fills it in
aNG2_set_talk_info_talk_start_wait() with aNG2_getP_other_pl_name(), which returns NULL when the local save has no OTHER resident (vanilla never reaches the scene that way; a multiplayer guest
or a first character does). mMsg_Set_free_str() ignores a NULL string, so free string 0 keeps stale buffer contents, which the message renders as the garbage name.
FIX: under TARGET_PC a NULL name is replaced by the plain stand-in "A traveler" (10 characters, length passed explicitly). A real other resident is still named exactly as before.

TIER: SOURCE checks + (when the game disc image is available in pc/build64/bin/rom) a decode of the REAL message data (message_data.bin from forest_2nd.arc, through the repo's tools/msg_tool.py)
proving which Rover messages use STR_FREE0. No game process, no gameplay: the displayed text in the real game is NOT verified here.
Usage: python test_rover_free_str_src.py"""
import glob
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
ISO = os.path.join(REPO, "pc", "build64", "bin", "rom", "Animal Crossing.iso")


def rd(*p):
    return open(os.path.join(REPO, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def real_messages():
    """decodes forest_2nd.arc message data from the disc image; None when it is not available"""
    if not os.path.isfile(ISO):
        return None
    try:
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import pyjkernel
        import msg_tool as M
    except Exception:  # noqa: BLE001
        return None
    f = open(ISO, "rb")
    f.seek(0x424)
    fst_off, fst_sz = struct.unpack(">II", f.read(8))
    f.seek(fst_off)
    d = f.read(fst_sz)
    n = struct.unpack(">I", d[8:12])[0]
    names = d[n * 12:]
    arc = None
    for i in range(1, n):
        if d[i * 12] == 0:
            no = struct.unpack(">I", b"\0" + d[i * 12 + 1:i * 12 + 4])[0]
            if names[no:names.index(b"\0", no)] == b"forest_2nd.arc":
                a, b = struct.unpack(">II", d[i * 12 + 4:i * 12 + 12])
                f.seek(a)
                arc = f.read(b)
    if arc is None:
        return None
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(prefix="rover_arc "), "forest_2nd.arc")
    open(tmp, "wb").write(arc)
    a = pyjkernel.from_archive_file(tmp, True)
    data = bytearray(a.get_file("bin2/data/message_data.bin").data)
    tab = a.get_file("bin2/data/message_data_table.bin").data

    def msg(i):
        s = 0 if i == 0 else struct.unpack(">I", tab[(i - 1) * 4:i * 4])[0]
        e = struct.unpack(">I", tab[i * 4:i * 4 + 4])[0]
        return M.decode_entry(data, s, e, i)
    return msg


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    mv = rd("src", "actor", "npc", "ac_npc_guide2_move.c_inc")
    ctrl = rd("src", "game", "m_msg_ctrl.c_inc")

    # ---- the cause, in source ----
    other = re.search(r"static u8\* aNG2_getP_other_pl_name\(void\) \{.*?\n\}\n", mv, re.S).group(0)
    ck("R1 aNG2_getP_other_pl_name() returns NULL when there is no other resident (the case that exposed the bug)", "return NULL;" in other and "No other players to reference" in other)
    setf = re.search(r"void mMsg_Set_free_str\(.*?\n\}\n", ctrl, re.S).group(0)
    ck("R2 mMsg_Set_free_str() does nothing for a NULL string (free string 0 keeps its old bytes)", "&& str != NULL" in setf and "free_str[i] = str[i]" in setf)
    start = re.search(r"static void aNG2_set_talk_info_talk_start_wait\(ACTOR\* actorx\) \{.*?\n\}\n", mv, re.S).group(0)
    ck("R3 the talk start sets FREE0 from that name and then the Rover message 0x2AD5", "mMsg_FREE_STR0" in start and "mDemo_Set_msg_num(0x2AD5)" in start)

    # ---- the fix ----
    fb = re.search(r"#ifdef TARGET_PC\n.*?if \(other_name == NULL\) \{\n\s*static u8 no_other_name\[\] = \{(.*?)\};.*?other_name = no_other_name;\n\s*other_len = \(int\)sizeof\(no_other_name\);.*?\n\s*\}\n#endif\n", start, re.S)
    ck("F1 under TARGET_PC a NULL name is replaced before mMsg_SET_FREE_STR", fb is not None and start.index("#endif") < start.index("mMsg_SET_FREE_STR"))
    if fb is not None:
        codes = [c.strip() for c in fb.group(1).split(",") if c.strip()]
        ascii_of = {"CHAR_SPACE": " ", **{"CHAR_%s" % c: c for c in "AtravelerTxy"}}
        text = "".join(ascii_of.get(c, "?") for c in codes)
        ck("F2 the stand-in spells exactly 'A traveler' (%d characters) using the game's own CHAR_ codes" % len(codes), text == "A traveler")
        ck("F3 it fits the free string buffer (mMsg_FREE_STRING_LEN 16) and its true length is passed (not the 8 of a player name)", len(codes) <= int(re.search(r"#define mMsg_FREE_STRING_LEN (\d+)", rd("include", "m_msg.h")).group(1))
           and "mMsg_SET_FREE_STR(mMsg_FREE_STR0, other_name, other_len);" in start and "int other_len = PLAYER_NAME_LEN;" in start)
    ck("F4 a real other resident is unchanged: name from aNG2_getP_other_pl_name(), length PLAYER_NAME_LEN", "u8* other_name = aNG2_getP_other_pl_name();" in start and "int other_len = PLAYER_NAME_LEN;" in start)
    nopc = re.sub(r"#ifdef TARGET_PC.*?#endif\n", "", start, flags=re.S)
    ck("F5 without TARGET_PC the original behaviour remains (the call sets FREE0 with PLAYER_NAME_LEN's value)", "mMsg_SET_FREE_STR(mMsg_FREE_STR0, other_name, other_len);" in nopc)
    ck("F6 the FOREIGNER (multiplayer guest) guard in aNG2_getP_other_pl_name is untouched", "player_no >= PLAYER_NUM" in other)
    ck("F7 FREE0 has no other writer in the Rover scene (this is the only place the name is set)", len(re.findall(r"mMsg_SET_FREE_STR\(mMsg_FREE_STR0", mv)) == 1)

    # ---- the real message data ----
    msg = real_messages()
    if msg is None:
        print("SKIP - real message-data checks: the disc image / repo tools are not available here")
    else:
        used = [hex(i) for i in range(0x2AC6, 0x2AE7) if "STR_FREE0" in msg(i)]
        ck("M1 in the REAL message data exactly 0x2AD9 and 0x2ADC (Rover scene range 0x2AC6-0x2AE6) use <STR_FREE0>: %s" % used, used == ["0x2ad9", "0x2adc"])
        ck("M2 0x2AD9 asks 'Who was that again?' and then prints FREE0; 0x2ADC says FREE0 'was the same way'", "Who was that again" in msg(0x2AD9) and "<<STR_FREE0>>" in msg(0x2AD9) and "was the" in msg(0x2ADC) and "same way" in msg(0x2ADC))
        ck("M3 no Rover-scene message uses the other free strings (FREE1..) that would need the same guard", not any(re.search(r"STR_FREE[1-9]", msg(i)) for i in range(0x2AC6, 0x2AE7)))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
