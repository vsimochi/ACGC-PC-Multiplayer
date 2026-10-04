#!/usr/bin/env python3
"""test_log_src.py - pc_log source audit + a native unit run of pc_log.c (no game is started).

TIER: pure source/diff audit against git HEAD (read-only git calls) + ONE small native compile of pc/src/pc_log.c with MinGW gcc into a temp dir
(skipped with a PENDING-free INFO if gcc is missing).

Checks:
  M1  macro shape: PC_LOG / PC_LOG_RL test g_pc_log_mask BEFORE the call and __VA_ARGS__ appears only inside the guarded branch (no argument
      evaluation / formatting when the category is disabled); PC_LOG_RL keeps a per-call-site static counter inside the guarded branch
  C1  the category defines are single, distinct bits; exactly 15 named categories + LEGACY (bit 31); PCL_ALL_CATEGORIES == their OR (LEGACY
      excluded); the name table in pc_log.c has one entry per category with the matching bit
  P1  the "[CAT] " prefix is added only by pc_log_write(): no PC_LOG/PC_LOG_RL call site passes its own "[" prefix, and EVERY line the diff adds
      to an existing file is a PC_LOG line, its continuation, an #include "pc_log.h", or the small reviewed pc_main.c / pc_m_card.c hunks
  P2  no pre-existing line was removed from any touched file except the three reviewed ones (pc_main.c redirect condition, pc_m_card.c
      pc_save_write_authoritative signature) => every existing printf/OSReport string is byte-identical
  V1  g_pc_verbose semantics unchanged: its definition, the single `g_pc_verbose = 1;` assignment and the number of g_pc_verbose references in
      pc/src + src equal HEAD (+0); --verbose additionally ORs LEGACY|GENERAL; -debug sets GENERAL only (no LEGACY) in pc_log_cli_arg
  R1  the NUL redirect is skipped only when verbose / profile / an explicit debug request; the no-flag path is unchanged
  X1  critical prefixes / anchors the tests grep have the same occurrence counts as HEAD (pc/src + src)
  B1  pc/CMakeLists.txt lists pc_log.c (one additive line); pc_log.h/.c are new files with LF line endings; touched CRLF files stay all-CRLF
  N1  native unit run: disabled PC_LOG evaluates no argument and prints nothing; enabled prints once with the [CAT] prefix; PC_LOG_RL burst/every
      counts; -logtime stamp; the parser results (-debug = GENERAL, -debugall, --debug=a,b, errors, -debug-list); -logfile writes the file,
      refuses a save dir; PC_LOG env ORed in; legacy env aliases set PLAYERS / VILLAGERS bits without asking for console output
Usage: python test_log_src.py"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"


def rd(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
        return f.read().decode("utf-8", "replace")


def git(*args):
    return subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True).stdout.decode("utf-8", "replace")


def head(rel):
    return git("show", "HEAD:" + rel)


def count_wt(needle, _files=None):
    """Occurrences in the WORKTREE of the tracked files under pc/src and src (git grep, so new untracked files are not counted on either side)."""
    out = git("grep", "-F", "-o", "-h", "--", needle, "--", "pc/src", "src")
    return len([ln for ln in out.splitlines() if ln.strip()])


def count_head(needle):
    out = git("grep", "-F", "-o", "-h", "--", needle, "HEAD", "--", "pc/src", "src")
    return len([ln for ln in out.splitlines() if ln.strip()])


def diff_lines(rel):
    """(added, removed) lines (without the +/-) of the worktree vs HEAD diff of one file."""
    d = git("diff", "-U0", "--no-color", "HEAD", "--", rel).replace("\r", "")
    add, rem = [], []
    for ln in d.splitlines():
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if ln.startswith("+"):
            add.append(ln[1:])
        elif ln.startswith("-"):
            rem.append(ln[1:])
    return add, rem


C_HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "pc_log.h"
unsigned long pc_frame_counter = 77;
static int evals = 0;
static int side(void) { evals++; return 5; }
int main(int argc, char** argv) {
    int consumed = 0, r;
    const char* mode = argv[1];
    if (strcmp(mode, "disabled") == 0) {
        g_pc_log_mask = PCL_NET;
        PC_LOG(PCL_SAVE, "must not print %d\n", side());
        PC_LOG_RL(PCL_SAVE, 1, 1, "must not print %d\n", side());
        printf("evals=%d\n", evals);
    } else if (strcmp(mode, "enabled") == 0) {
        g_pc_log_mask = PCL_SAVE | PCL_VILLAGERS;
        PC_LOG(PCL_SAVE, "hello %d\n", side());
        PC_LOG(PCL_VILLAGERS, "no newline");
        PC_LOG(PCL_NET, "off %d\n", side());
        printf("evals=%d\n", evals);
    } else if (strcmp(mode, "rl") == 0) {
        int i, lines = 0;
        g_pc_log_mask = PCL_ITEMS;
        for (i = 0; i < 10; i++) { PC_LOG_RL(PCL_ITEMS, 3, 4, "rl %d\n", i); }
        (void)lines;
    } else if (strcmp(mode, "cli") == 0) {
        int want = 0, k;
        for (k = 2; k < argc; k++) {
            r = pc_log_cli_arg(argv[k], k + 1 < argc ? argv[k + 1] : NULL, &consumed);
            printf("arg=%s r=%d consumed=%d mask=0x%08X\n", argv[k], r, consumed, (unsigned)g_pc_log_mask);
            if (r == 2 || r == 3) break;
            k += consumed;
        }
        r = pc_log_finish_cli(&want);
        printf("finish=%d want_console=%d mask=0x%08X\n", r, want, (unsigned)g_pc_log_mask);
        if (r == 0 && g_pc_log_mask) { PC_LOG(PCL_GENERAL, "general line\n"); PC_LOG(PCL_PLAYERS, "players line\n"); }
    }
    return 0;
}
'''


def run_native(tmp, exe, args, env=None):
    e = dict(os.environ)
    e["PATH"] = os.path.dirname(GCC) + os.pathsep + e.get("PATH", "")
    for k in ("PC_LOG", "PC_PUPPET_DIAG", "PC_COLLIDE_DIAG", "PC_NPC_TALKHOLD_DIAG"):
        e.pop(k, None)
    if env:
        e.update(env)
    p = subprocess.run([exe] + args, capture_output=True, cwd=tmp, env=e, timeout=30)
    return p.returncode, p.stdout.decode("utf-8", "replace").replace("\r", ""), p.stderr.decode("utf-8", "replace").replace("\r", "")


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    hdr = rd("pc/include/pc_log.h")
    src = rd("pc/src/pc_log.c")

    # ---- M1 macro shape
    m = re.search(r"#define PC_LOG\(cat, \.\.\.\) \\\n(.*?)\n\n", hdr.replace("\r", ""), re.S)
    body = m.group(1) if m else ""
    ck("M1 PC_LOG defined as do { if (<mask check>) pc_log_write(..., __VA_ARGS__); } while (0)",
       re.fullmatch(r"\s*do \{ if \(PC_LOG_UNLIKELY\(g_pc_log_mask & \(uint32_t\)\(cat\)\)\) pc_log_write\(\(uint32_t\)\(cat\), __VA_ARGS__\); \} while \(0\)\s*", body) is not None)
    ck("M1 PC_LOG: __VA_ARGS__ appears exactly once, after the mask test", body.count("__VA_ARGS__") == 1 and body.index("g_pc_log_mask") < body.index("__VA_ARGS__"))
    m = re.search(r"#define PC_LOG_RL\(cat, burst, every, \.\.\.\) \\\n(.*?)\n\n", hdr.replace("\r", ""), re.S)
    rl = m.group(1) if m else ""
    ck("M1 PC_LOG_RL: mask test first, the static counter and every use of __VA_ARGS__ inside the guarded branch",
       rl.count("__VA_ARGS__") == 1 and rl.index("g_pc_log_mask") < rl.index("static uint32_t") < rl.index("__VA_ARGS__")
       and rl.index("g_pc_log_mask") < rl.index("pcl_n_++"))
    ck("M1 pc_log_write is declared printf-checked and the mask is a plain extern uint32_t", "extern uint32_t g_pc_log_mask;" in hdr and "format(printf, 2, 3)" in hdr)
    ck("M1 the header includes only <stdint.h> (light enough for decomp C files)", re.findall(r"#include <([^>]+)>", hdr) == ["stdint.h"] and '#include "' not in hdr)

    # ---- C1 categories
    defs = dict(re.findall(r"#define (PCL_[A-Z]+)\s+\(\(uint32_t\)1u << (\d+)\)", hdr))
    cats = ["GENERAL", "NET", "PLAYERS", "VILLAGERS", "BUILDINGS", "ITEMS", "WILDLIFE", "SAVE", "EVENTS", "TXN", "RECORDS", "GUESTS", "TOWNSVC", "MAIL", "SHOP"]
    ck("C1 15 named categories on bits 0..14 plus LEGACY on bit 31",
       [int(defs.get("PCL_" + c, -1)) for c in cats] == list(range(15)) and defs.get("PCL_LEGACY") == "31" and len(defs) == 16)
    mm = re.search(r"#define PCL_ALL_CATEGORIES \(\(uint32_t\)(0x[0-9A-Fa-f]+)u\)", hdr)
    ck("C1 PCL_ALL_CATEGORIES == OR of the 15 categories (LEGACY excluded)", mm is not None and int(mm.group(1), 16) == (1 << 15) - 1)
    table = re.findall(r'\{ (PCL_[A-Z]+),\s+"([a-z]+)"', src)
    ck("C1 pc_log.c name table: 15 entries, bit order == header order, canonical names",
       [t[0] for t in table] == ["PCL_" + c for c in cats]
       and [t[1] for t in table] == ["general", "network", "players", "villagers", "buildings", "items", "wildlife", "save", "events", "transactions",
                                      "records", "guests", "townsvc", "mail", "shop"])

    # ---- P1 / P2 diff audit
    touched = ["pc/src/pc_net_game.c", "pc/src/pc_m_card.c", "pc/src/pc_main.c", "pc/src/pc_wildlife_authority.c", "src/game/m_field_info.c",
               "pc/CMakeLists.txt"]
    allowed_removed = {"pc/src/pc_main.c": ["    if (!g_pc_verbose && !g_pc_profile_enabled) {"],
                       "pc/src/pc_m_card.c": []}
    for rel in touched:
        add, rem = diff_lines(rel)
        ck("P2 %s: removed lines == the reviewed set %s" % (rel, allowed_removed.get(rel, [])), rem == allowed_removed.get(rel, []))
    callsites = 0
    stray = []
    for rel in ("pc/src/pc_net_game.c", "pc/src/pc_m_card.c", "pc/src/pc_wildlife_authority.c", "src/game/m_field_info.c"):
        add, rem = diff_lines(rel)
        prev_open = False
        for ln in add:
            s = ln.strip()
            if "PC_LOG(" in s or "PC_LOG_RL(" in s:
                callsites += 1
                prev_open = not s.endswith(";")
                if re.search(r'PC_LOG(?:_RL)?\([^"]*"\[', s):
                    stray.append(("prefix in format", rel, s))
            elif prev_open:
                prev_open = not s.endswith(";")
            elif s in ('#include "pc_log.h"', "#ifdef TARGET_PC", "#endif") or s.startswith('#include "pc_log.h"'):
                pass
            elif rel == "pc/src/pc_m_card.c" and s in ("static int pc_save_write_authoritative_impl(void);", "", "int ok = pc_save_write_authoritative_impl();", "int pc_save_write_authoritative(void) {",
                                                       "return ok;", "}", "static int pc_save_write_authoritative_impl(void) {"):
                pass
            elif rel == "pc/src/pc_net_game.c" and s in ("if (is_shop) {", "}"):
                pass
            elif rel == "pc/src/pc_wildlife_authority.c" or True:
                stray.append(("unexpected added line", rel, s))
    ck("P1 every added line in the existing game files is a PC_LOG line / continuation / include / reviewed wrapper (stray: %s)" % stray[:3], not stray)
    ck("P1 no PC_LOG call site carries its own '[' prefix (pc_log_write adds [CAT] only to these new lines)", not [s for s in stray if s[0] == "prefix in format"])
    ck("P1 the sample sites stay modest and cover all 15 categories (%d PC_LOG call sites)" % callsites, 15 <= callsites <= 30)
    used = set()
    for rel in ("pc/src/pc_net_game.c", "pc/src/pc_m_card.c", "pc/src/pc_wildlife_authority.c", "src/game/m_field_info.c", "pc/src/pc_main.c"):
        used |= set(re.findall(r"PC_LOG(?:_RL)?\((PCL_[A-Z]+)", "\n".join(diff_lines(rel)[0])))
    ck("P1 every category has at least one call site (missing: %s)" % sorted(set("PCL_" + c for c in cats) - used), set("PCL_" + c for c in cats) <= used)
    ck("P1 pc_log_write adds the [CAT] prefix (upper-cased category name) itself", "toupper" in src and "buf[n++] = '['" in src)

    # ---- V1 verbose semantics
    files = None
    added_v = [ln.strip() for ln in diff_lines("pc/src/pc_main.c")[0] if "g_pc_verbose" in ln]
    all_added_v = [(rel, ln.strip()) for rel in ("pc/src/pc_net_game.c", "pc/src/pc_m_card.c", "pc/src/pc_wildlife_authority.c", "src/game/m_field_info.c", "pc/CMakeLists.txt")
                   for ln in diff_lines(rel)[0] if "g_pc_verbose" in ln]
    ck("V1 the only added g_pc_verbose references are pc_main.c's: the redirect condition (reviewed, R1), a comment and the explicit-debug summary argument; "
       "none in any other file (%d references at HEAD; worktree +%d = +3 added -1 removed condition)" % (count_head("g_pc_verbose"), count_wt("g_pc_verbose") - count_head("g_pc_verbose")),
       not all_added_v and len(added_v) == 3 and count_wt("g_pc_verbose") - count_head("g_pc_verbose") == 2
       and any(l.startswith("if (!g_pc_verbose && !g_pc_profile_enabled && !log_want_console)") for l in added_v)
       and any("g_pc_log_mask |= PCL_LEGACY" in l and "g_pc_verbose itself is unchanged" in l for l in added_v)
       and any(l.startswith("PC_LOG(PCL_GENERAL") and "g_pc_verbose, log_want_console" in l for l in added_v))
    mw = rd("pc/src/pc_main.c").replace("\r\n", "\n")
    mh = head("pc/src/pc_main.c").replace("\r\n", "\n")
    ck("V1 pc_main.c: one `g_pc_verbose = 1;` and the same definition as HEAD", mw.count("g_pc_verbose = 1;") == 1 == mh.count("g_pc_verbose = 1;")
       and "int           g_pc_verbose = 0;" in mw)
    ck("V1 --verbose / -v branch: g_pc_verbose = 1 then LEGACY|GENERAL ORed in",
       'strcmp(argv[i], "--verbose") == 0 || strcmp(argv[i], "-v") == 0) {\n            g_pc_verbose = 1;\n            g_pc_log_mask |= PCL_LEGACY | PCL_GENERAL;' in mw)
    ck("V1 PCL_LEGACY is ORed into the mask in pc_main.c exactly once (the --verbose branch)", mw.count("PCL_LEGACY") == 1)
    body = re.search(r"int pc_log_cli_arg\(.*?\n\}\n", src, re.S).group(0)
    dbg = re.search(r"if \(\*r == '\\0'\) \{[^}]*\}", body).group(0)
    ck("V1 pc_log_cli_arg: plain -debug sets only GENERAL (and asks for console)", "PCL_GENERAL" in dbg and "PCL_LEGACY" not in dbg and "PCL_ALL" not in dbg)
    ck("V1 pc_log_cli_arg never sets PCL_LEGACY (-debugall excludes it)", "PCL_LEGACY" not in body)

    # ---- R1 redirect
    ck("R1 redirect condition is `!g_pc_verbose && !g_pc_profile_enabled && !log_want_console` (no-flag path unchanged: still redirected)",
       "if (!g_pc_verbose && !g_pc_profile_enabled && !log_want_console) {\n#ifdef _WIN32\n        freopen(\"NUL\", \"w\", stdout);" in mw)
    ck("R1 log_want_console is set only by pc_log_finish_cli (flag or PC_LOG env), never by the legacy env aliases",
       "s_want_console = 1;" in src and src.index("pcl_env_set(\"PC_PUPPET_DIAG\")") > 0
       and "s_want_console" not in re.search(r'if \(pcl_env_set\("PC_PUPPET_DIAG"\).*?PCL_VILLAGERS;\n    \}', src, re.S).group(0))

    # ---- X1 prefixes
    prefixes = ["[NET][PUPPET]", "[NET]", "[NET][TXN]", "[NPC][TALKNET]", "[NET][REMOTE]", "[NET][MAIL]", "[NET][TS]", "[NET][REC]", "[NET][WORLD]",
                "[NET][SCENE]", "[NET][EVENT]", "host: world ready (%s", "--bootstrap-resident %d: resident bound, transitioning to town",
                "local save not loaded", "[SCENE_MODE]", "[TEST-ONLY]", " armed", "[PC] periodic save OK", "[PC] early save", "--pickup-test-seed: fixture item placed",
                "[NET][NPC]", "[NET][SHOP]", "[NET][GUEST]", "[NET][BURY]", "[NET][WILDLIFE]", "[PC] "]
    bad = []
    for p in prefixes:
        a, b = count_wt(p, files), count_head(p)
        if a != b:
            bad.append((p, a, b))
    ck("X1 %d critical prefixes / anchors: occurrence counts in pc/src + src equal HEAD (mismatches: %s)" % (len(prefixes), bad), not bad)

    # ---- B1 build / EOL
    cm = diff_lines("pc/CMakeLists.txt")
    ck("B1 CMake: exactly one added line, the pc_log.c source entry", cm[1] == [] and len(cm[0]) == 1 and "src/pc_log.c" in cm[0][0])
    ck("B1 pc_log.c listed once in PC_SOURCES", rd("pc/CMakeLists.txt").count("src/pc_log.c") == 1)
    for rel in ("pc/include/pc_log.h", "pc/src/pc_log.c"):
        raw = open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb").read()
        ck("B1 %s is a new file with LF line endings" % rel, b"\r" not in raw)
    eol = git("ls-files", "--eol", "pc/src/pc_main.c", "pc/src/pc_net_game.c", "pc/src/pc_m_card.c", "src/game/m_field_info.c", "pc/src/pc_wildlife_authority.c")
    for rel in ("pc/src/pc_main.c", "pc/src/pc_net_game.c", "pc/src/pc_m_card.c", "src/game/m_field_info.c"):
        raw = open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb").read()
        ck("B1 %s stays all-CRLF (matches git ls-files --eol w/crlf)" % rel, raw.count(b"\n") == raw.count(b"\r\n") and "w/crlf" in [l for l in eol.splitlines() if rel in l][0])
    raw = open(os.path.join(ROOT, "pc", "src", "pc_wildlife_authority.c"), "rb").read()
    ck("B1 pc_wildlife_authority.c stays all-LF", b"\r" not in raw)

    # ---- N1 native unit run
    if not os.path.isfile(GCC):
        L.info("N1 skipped: %s not found" % GCC)
        return L.summary_and_exit_code(results)
    tmp = tempfile.mkdtemp(prefix="pclogsrc_")
    try:
        cfile = os.path.join(tmp, "unit.c")
        exe = os.path.join(tmp, "unit.exe")
        with open(cfile, "w") as f:
            f.write(C_HARNESS)
        cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I", os.path.join(ROOT, "pc", "include"), cfile,
                             os.path.join(ROOT, "pc", "src", "pc_log.c"), "-o", exe], capture_output=True,
                            env=dict(os.environ, PATH=os.path.dirname(GCC) + os.pathsep + os.environ.get("PATH", "")))
        ck("N1 pc_log.c + a macro harness compile warning-free (-Wall -Wextra -Werror)%s" % ("" if cp.returncode == 0 else ": " + cp.stderr.decode()[:300]), cp.returncode == 0)
        if cp.returncode != 0:
            return L.summary_and_exit_code(results)
        rc, out, err = run_native(tmp, exe, ["disabled"])
        ck("N1 disabled category: no output and the arguments were NOT evaluated (evals=0)", rc == 0 and out == "evals=0\n")
        rc, out, err = run_native(tmp, exe, ["enabled"])
        ck("N1 enabled: '[SAVE] hello 5' once (argument evaluated once), '[VILLAGERS] no newline' newline-terminated, the NET line absent",
           out == "[SAVE] hello 5\n[VILLAGERS] no newline\nevals=1\n")
        rc, out, err = run_native(tmp, exe, ["rl"])
        ck("N1 PC_LOG_RL(burst 3, every 4) over 10 calls prints calls 0,1,2,4,8", out.split() == ["[ITEMS]", "rl", "0", "[ITEMS]", "rl", "1", "[ITEMS]", "rl", "2", "[ITEMS]", "rl", "4",
                                                                                     "[ITEMS]", "rl", "8"])
        rc, out, err = run_native(tmp, exe, ["cli", "-debug"])
        ck("N1 -debug: mask == GENERAL only (0x1), console requested, nothing else printed",
           "arg=-debug r=1 consumed=0 mask=0x00000001" in out and "finish=0 want_console=1 mask=0x00000001" in out and "[GENERAL] general line" in out and "players line" not in out)
        rc, out, err = run_native(tmp, exe, ["cli", "-debugall"])
        ck("N1 -debugall: mask == 0x7FFF (no LEGACY bit)", "finish=0 want_console=1 mask=0x00007FFF" in out)
        rc, out, err = run_native(tmp, exe, ["cli", "--debug=Network,villager", "-debugsave"])
        ck("N1 --debug=Network,villager + -debugsave: mask == NET|VILLAGERS|SAVE (0x8A)", "finish=0 want_console=1 mask=0x0000008A" in out)
        rc, out, err = run_native(tmp, exe, ["cli", "--debug=network,bogus"])
        ck("N1 unknown category in a list: result 2 and the message lists the valid categories", "r=2" in out and "bogus" in err and "valid categories: general network" in err)
        rc, out, err = run_native(tmp, exe, ["cli", "-debug-list"])
        ck("N1 -debug-list: result 3 and 15 category lines", "r=3" in out and len([l for l in out.splitlines() if l.startswith("  ") and l.split()[0] in
                                                                                       ("general", "network", "players", "villagers", "buildings", "items", "wildlife", "save", "events",
                                                                                        "transactions", "records", "guests", "townsvc", "mail", "shop")]) == 15)
        lf = os.path.join(tmp, "out.log")
        rc, out, err = run_native(tmp, exe, ["cli", "-debugplayers", "-logtime", "-logfile", lf])
        text = open(lf).read() if os.path.exists(lf) else ""
        ck("N1 -logfile: consumed the path argument, file has the header + GENERAL + PLAYERS lines, with the -logtime stamp",
           "arg=-logfile r=1 consumed=1" in out and "[GENERAL] log file opened" in text and re.search(r"\[PLAYERS\] \[t\+\d+\.\d{3} f77\] players line", text) is not None)
        lf2 = os.path.join(tmp, "only.log")
        rc, out, err = run_native(tmp, exe, ["cli", "-logfile", lf2])
        ck("N1 -logfile alone: console NOT requested (want_console=0), stdout carries no PC_LOG line, the file does",
           "want_console=0" in out and "[GENERAL] general line" not in out and "general line" in open(lf2).read())
        os.makedirs(os.path.join(tmp, "save"), exist_ok=True)
        rc, out, err = run_native(tmp, exe, ["cli", "-logfile", os.path.join(tmp, "save", "x.log")])
        ck("N1 -logfile in a save dir: finish returns nonzero, REFUSED, no file", "finish=1" in out and "REFUSED" in err and not os.path.exists(os.path.join(tmp, "save", "x.log")))
        rc, out, err = run_native(tmp, exe, ["cli", "-v"], env={"PC_LOG": "mail,shop"})
        ck("N1 env PC_LOG=mail,shop is ORed in (0x6000) and asks for console", "finish=0 want_console=1 mask=0x00006000" in out)
        rc, out, err = run_native(tmp, exe, ["cli", "-v"], env={"PC_LOG": "0x4"})
        ck("N1 env PC_LOG=0x4 (hex mask) == PLAYERS", "mask=0x00000004" in out)
        rc, out, err = run_native(tmp, exe, ["cli", "-v"], env={"PC_PUPPET_DIAG": "1", "PC_NPC_TALKHOLD_DIAG": "1"})
        ck("N1 PC_PUPPET_DIAG / PC_NPC_TALKHOLD_DIAG alias PLAYERS|VILLAGERS (0xC) WITHOUT asking for console (want_console=0)", "finish=0 want_console=0 mask=0x0000000C" in out)
        rc, out, err = run_native(tmp, exe, ["cli", "-v"], env={"PC_COLLIDE_DIAG": "1"})
        ck("N1 PC_COLLIDE_DIAG aliases PLAYERS (0x4), no console", "finish=0 want_console=0 mask=0x00000004" in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
