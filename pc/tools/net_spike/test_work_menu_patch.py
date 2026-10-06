#!/usr/bin/env python3
"""test_work_menu_patch.py - Nook Work Mode UX: the FIRST page of Nook's talk (vanilla message 0x1092, the bytes dumped from a real game with AC_TEST_DUMP_MSG) gets "I'd like to work" when the
character has NO active job and "Check my job" when it HAS one, decided from the authoritative job state at load time; offline / other messages are untouched; the PC message selftest passes.

TIER: HARNESS (the REAL pc_nook_house.c compiled with the build's flags and linked with stubs for pc_net_game_work_view / _offer_available; the transitions of the authoritative state
(start / complete / quit / reconnect) that drive the stub are covered by test_work_mode_protocol.py with a real host). Not a GUI run.
Usage: python test_work_menu_patch.py   (needs pc\build64 configured; uses its compiler + flags)
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

PC = os.path.abspath(os.path.join(HERE, "..", ".."))
BUILD = os.path.join(PC, "build64")


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    flags = open(os.path.join(BUILD, "CMakeFiles", "ac_pc.dir", "flags.make"), encoding="utf-8").read()
    defines = re.search(r"^C_DEFINES = (.*)$", flags, re.M).group(1).split()
    cc = re.search(r"# compile C with (\S+)", flags).group(1)
    rsp = "@" + os.path.join(BUILD, "CMakeFiles", "ac_pc.dir", "includes_C.rsp")
    out = os.path.join(os.environ.get("TEMP", "."), "work_menu_harness.exe")
    cmd = [cc] + defines + [rsp, "-std=gnu11", "-O1", "-fno-strict-aliasing", "-fwrapv", "-Dnullptr=NULL", "-I" + os.path.join(PC, "include"), os.path.join(HERE, "work_menu_harness.c"),
                            os.path.join(PC, "src", "pc_nook_house.c"), "-o", out]
    env = dict(os.environ, PATH=os.path.dirname(cc) + os.pathsep + os.environ.get("PATH", ""))
    r = subprocess.run(cmd, cwd=BUILD, capture_output=True, text=True, env=env)
    ck("the harness compiled (%s)" % (r.stderr.strip().splitlines()[-1] if r.returncode and r.stderr.strip() else "ok"), r.returncode == 0)
    if r.returncode != 0:
        print(r.stderr[-1500:])
        return L.summary_and_exit_code(results)
    p = subprocess.run([out], capture_output=True, text=True, env=env)
    txt = p.stdout
    print(txt)
    m = re.search(r"CONST SEL_WORK=(\d+) SEL_JOB=(\d+)", txt)
    sel_work, sel_job = int(m.group(1)), int(m.group(2))
    ck("the two labels are different generated choice strings", sel_work != sel_job)

    def row(name):
        m = re.search(r"^%s: (.*)$" % name, txt, re.M)
        return m.group(1) if m else ""

    ck("offline (no session): the vanilla page is untouched (4 choices, not patched)", "patched=0" in row("offline") and "code=0x18" in row("offline") and "first_page_work=0" in row("offline"))
    nj = row("nojob")
    ck("no active job: the first page has 5 choices and the 4th is 'I'd like to work' (before the vanilla last choice)", "patched=1" in nj and "code=0x79" in nj and ("ids=10,452,9,%d,11" % sel_work) in nj and "first_page_work=1" in nj)
    aj = row("activejob")
    ck("active job: the 4th choice is 'Check my job' instead", "patched=1" in aj and ("ids=10,452,9,%d,11" % sel_job) in aj and "first_page_work=1" in aj)
    ck("completed / quit (state NONE again): back to 'I'd like to work' -- the menu follows the authoritative state, there is no local working flag", ("ids=10,452,9,%d,11" % sel_work) in row("completed"))
    ck("job state not known yet: 'I'd like to work' (ENTER resumes an existing job)", ("ids=10,452,9,%d,11" % sel_work) in row("unknownyet"))
    ck("the text and the tail of the vanilla message are byte for byte unchanged after the patch (only the choice list grew)", "tail_ok=1" in nj and "tail_ok=1" in aj)
    ck("any other message id is never touched", "patched=0" in row("othermsg"))
    ck("the PC message selftest (every generated row builds, charset, endings; every job type x step) passes", "selftest=0" in txt)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
