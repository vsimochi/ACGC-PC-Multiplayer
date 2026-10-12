#!/usr/bin/env python3
"""test_dresser_msg_patch.py - dresser storage: the empty / 1-item / 2-item OWNER dresser messages get "More storage" inserted before their last (cancel) choice,
ONE-SHOT and only when armed (ac_my_room arms it only when pc_net_game_dresser_available(); otherwise it disarms), plus the ac_my_room wiring (source).

TIER: HARNESS (the REAL pc_nook_house.c compiled with the build's flags, synthetic message bytes in the vanilla SETSELSTRk layout) + SOURCE. Not a GUI run.
Usage: python test_dresser_msg_patch.py   (needs pc\\build64 configured)"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
BUILD = os.path.join(PC, "build64")


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    flags = open(os.path.join(BUILD, "CMakeFiles", "ac_pc.dir", "flags.make"), encoding="utf-8").read()
    defines = re.search(r"^C_DEFINES = (.*)$", flags, re.M).group(1).split()
    cc = re.search(r"# compile C with (\S+)", flags).group(1)
    rsp = "@" + os.path.join(BUILD, "CMakeFiles", "ac_pc.dir", "includes_C.rsp")
    out = os.path.join(os.environ.get("TEMP", "."), "dresser_msg_harness.exe")
    cmd = [cc] + defines + [rsp, "-std=gnu11", "-O1", "-fno-strict-aliasing", "-fwrapv", "-Dnullptr=NULL", "-I" + os.path.join(PC, "include"), os.path.join(HERE, "dresser_msg_harness.c"),
                            os.path.join(PC, "src", "pc_nook_house.c"), "-o", out]
    env = dict(os.environ, PATH=os.path.dirname(cc) + os.pathsep + os.environ.get("PATH", ""))
    r = subprocess.run(cmd, cwd=BUILD, capture_output=True, text=True, env=env)
    ck("the harness compiled", r.returncode == 0)
    if r.returncode != 0:
        print(r.stderr[-1500:])
        return L.summary_and_exit_code(results)
    txt = subprocess.run([out], capture_output=True, text=True, env=env).stdout
    print(txt)
    more = int(re.search(r"CONST MORE=(\d+)", txt).group(1))
    mh = "%04X" % more

    def row(name):
        m = re.search(r"^%s: (.*)$" % name, txt, re.M)
        return m.group(1) if m else ""

    ck("the generated choice string reads 'More storage' (16 chars, space padded)", "label_ok=1 label=More storage    |" in txt)
    ck("not armed: the message is untouched", "bytes=41427F1600200021" in row("unarmed") and "more=-1" in row("unarmed"))
    ck("armed for another message: that message is untouched", "bytes=41427F170020002100227F5E5A7F01" in row("othermsg") and "more=-1" in row("othermsg"))
    ck("armed 2-choice message: 3 choices, 'More storage' at index 1 before the cancel id, the rest byte-identical",
       ("bytes=41427F170020%s00217F5E5A7F01" % mh) in row("sel2") and "more=1" in row("sel2"))
    ck("one-shot: the next load of the same message is untouched", "bytes=41427F16002000217F5E5A7F01" in row("again"))
    ck("armed 3-choice message: 4 choices, 'More storage' at index 2", ("bytes=41427F1800200021%s0022" % mh) in row("sel3") and "more=2" in row("sel3"))
    ck("disarmed (feature unavailable): no 'More storage' choice is reported (the vanilla mapping is used)", "more=-1" in row("disarmed"))
    ck("no room in the message buffer: untouched, not reported", "bytes=41427F17002000210022" in row("nocap") and "more=-1" in row("nocap"))
    ck("the PC message selftest still passes", "selftest=0" in txt)
    src = open(os.path.join(ROOT, "src", "actor", "ac_my_room_msg_ctrl.c_inc"), encoding="utf-8").read()
    mv = open(os.path.join(ROOT, "src", "actor", "ac_my_room_move.c_inc"), encoding="utf-8").read()
    ck("ac_my_room arms the patch ONLY when aMR_PcDresserOn() for the three owner dresser states, else disarms (-1)",
       "pc_dresser_msg_arm((aMR_PcDresserOn() && (t == aMR_MSG_STATE_OWNER_NO_ITEM || t == aMR_MSG_STATE_OWNER_EXIST_ITEM ||" in mv and ": -1);" in mv)
    ck("the three owner controls route the patched choice to the pool page before the vanilla mapping (TARGET_PC)",
       all("aMR_PcDresserMoreChoice(my_room, %s, %s)" % (m, v) in src for m, v in (("MSG_2570", "choice"), ("MSG_2571", "choice_no"), ("MSG_12058", "choice_no"))))
    nk = open(os.path.join(PC, "src", "pc_nook_house.c"), encoding="utf-8").read()
    ck("refusal line: a generated row PC_NOOK_MSG_DRESSER_REFUSED (built + charset-checked by the selftest above) shown ONLY for a non-APPLIED take-out",
       "case PC_NOOK_MSG_DRESSER_REFUSED:" in nk and
       "aMR_PcDresserClose(my_room, aMR_pc_txn_last == PC_NETGAME_TS_OP_APPLIED ? -1 : pc_nook_msg_id(PC_NOOK_MSG_DRESSER_REFUSED));" in src)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
