#!/usr/bin/env python3
"""test_version_src.py - the game VERSION (title screen corner, startup log, dedicated banner / status, Windows version resource): source audit.

ONE source: pc/include/pc_version.h (PC_VERSION_MAJOR / MINOR / PATCH / DEV, plain #defines). Everything else derives from it:
  pc/res/app.rc (FILEVERSION / PRODUCTVERSION = PC_VERSION_RC_NUMBERS, both version strings = PC_VERSION_NUMERIC_STRING "X.Y.Z.0"; no literal numbers left),
  the title screen corner text "v" PC_VERSION_STRING (pc_pause_menu.c pc_version_corner_draw, called ONLY from ac_animal_logo.c's title menu draw),
  the startup line "[GENERAL] Animal Crossing PC Multiplayer v<ver>" (pc_main.c) and the dedicated server's start line / `status` (pc_dedicated.c).
The release copy E:\\CodeStuff\\AnimalCrossingMultiplayer\\release_0.3.1 must read that single source (its rebuild_from_commit.sh), not app.rc literals.

TIER: SOURCE (no build, no game process; one tiny bash call runs the release script's own pc_version.h parser on the working tree header).
Usage: python test_version_src.py"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
REL = os.path.abspath(os.path.join(ROOT, "..", "release_0.3.1"))
BASH = r"C:\Program Files\Git\bin\bash.exe"

results = []


def ck(name, ok, detail=None):
    results.append(bool(ok))
    print(("PASS" if ok else "FAIL") + " - " + name + ("" if ok or detail is None else "  :: %s" % (detail,)))


def read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace").replace("\r\n", "\n")


def src(rel):
    return read(os.path.join(ROOT, rel.replace("/", os.sep)))


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def func_body(text, name):
    m = re.search(r"^(?:static )?[\w\s\*]+?\b%s\([^;{]*?\)\s*\{\n" % re.escape(name), text, re.M)
    if not m:
        return ""
    i = m.end()
    return text[i:text.index("\n}\n", i)]


def header_version(h):
    nums = {}
    for k in ("MAJOR", "MINOR", "PATCH", "DEV"):
        m = re.search(r"^#define PC_VERSION_%s +(\d+) *$" % k, h, re.M)
        nums[k] = int(m.group(1)) if m else None
    return nums


def main():
    h = src("pc/include/pc_version.h")
    v = header_version(h)
    ck("S1 pc_version.h defines PC_VERSION_MAJOR / MINOR / PATCH / DEV as plain integer #defines (readable by windres, gcc and the release script's sed) %s" % v,
       None not in v.values() and v["DEV"] in (0, 1))
    ck("S1b the numbers are the 0.3.1 development line: 0.3.1 with PC_VERSION_DEV 1 (the release commit flips DEV to 0)", (v["MAJOR"], v["MINOR"], v["PATCH"], v["DEV"]) == (0, 3, 1, 1), v)
    hc = strip_comments(h)
    ck("S1c the strings are built from the numbers with the two-level stringify (no literal version text in the header)",
       re.search(r"#define PC_VERSION_XSTR\(x\) PC_VERSION_STR_\(x\)", hc) and "#define PC_VERSION_STR_(x) #x" in hc
       and re.search(r"#define PC_VERSION_NUMERIC_STRING PC_VERSION_XSTR\(PC_VERSION_MAJOR\.PC_VERSION_MINOR\.PC_VERSION_PATCH\.0\)", hc)
       and re.search(r"#define PC_VERSION_BASE_STRING PC_VERSION_XSTR\(PC_VERSION_MAJOR\.PC_VERSION_MINOR\.PC_VERSION_PATCH\)", hc)
       and re.search(r"#define PC_VERSION_RC_NUMBERS PC_VERSION_MAJOR,PC_VERSION_MINOR,PC_VERSION_PATCH,0", hc)
       and re.search(r"#if PC_VERSION_DEV\n#define PC_VERSION_STRING PC_VERSION_BASE_STRING \"-dev\"\n#else\n#define PC_VERSION_STRING PC_VERSION_BASE_STRING\n#endif", hc)
       and not re.search(r"\"\d+\.\d+", hc))
    shown = "%d.%d.%d%s" % (v["MAJOR"], v["MINOR"], v["PATCH"], "-dev" if v["DEV"] else "")
    ck("S1d the displayed version format is X.Y.Z or X.Y.Z-dev (here %r)" % shown, re.fullmatch(r"\d+\.\d+\.\d+(-dev)?", shown))

    rc = src("pc/res/app.rc")
    ck("S2 pc/res/app.rc includes pc_version.h and takes FILEVERSION / PRODUCTVERSION / FileVersion / ProductVersion from it",
       '#include "../include/pc_version.h"' in rc and re.search(r"^FILEVERSION +PC_VERSION_RC_NUMBERS$", rc, re.M) and re.search(r"^PRODUCTVERSION +PC_VERSION_RC_NUMBERS$", rc, re.M)
       and re.search(r'VALUE "FileVersion", +PC_VERSION_NUMERIC_STRING$', rc, re.M) and re.search(r'VALUE "ProductVersion", +PC_VERSION_NUMERIC_STRING$', rc, re.M))
    ck("S2b no hand-written version number is left in app.rc (no \"X.Y.Z.W\" string, no a,b,c,d list)",
       not re.search(r'"\d+\.\d+\.\d+\.\d+"', rc) and not re.search(r"\b\d+, *\d+, *\d+, *\d+\b", rc))
    cm = src("pc/CMakeLists.txt")
    ck("S2c pc/CMakeLists.txt holds no second version (no project VERSION, no PC_VERSION variable / define; app.rc is still the RC_FILE)",
       not re.search(r"project\([^)]*VERSION", cm) and "PC_VERSION" not in cm and 'set(RC_FILE "${CMAKE_CURRENT_SOURCE_DIR}/res/app.rc")' in cm)

    # ---- the title screen draw
    pm = src("pc/src/pc_pause_menu.c")
    body = func_body(pm, "pc_version_corner_draw")
    ck("S3 pc_version_corner_draw (pc_pause_menu.c) draws \"v\" PC_VERSION_STRING with pc_text_draw: 4 dark outline copies + the translucent white text",
       body != "" and 'static const char text[] = "v" PC_VERSION_STRING;' in body
       and len(re.findall(r"pc_text_draw\(game, text, [^;]*0, 0, 0, 150, scale\);", body)) == 4 and "pc_text_draw(game, text, x, y, 255, 255, 255, 200, scale);" in body)
    ck("S3b it is a no-op for --dedicated (no window) and without a game / graph", "if (g_pc_dedicated || game == NULL || game->graph == NULL) return;" in body)
    ck("S3c it hugs the WINDOW's right edge: the visible font-space edge from the window aspect only (g_pc_widescreen_stretch is emu64's render-time state, never read "
       "here: the 1st real run read a stale 1 and drew at x 320), margins in window pixels",
       "right = 160.0f + 160.0f * win / gc;" in body and "g_pc_widescreen_stretch" not in strip_comments(body) and "if (win > gc + 0.01f) {" in body and "px = SCREEN_HEIGHT_F / (f32)g_pc_window_h;" in body
       and "x = right - w - 8.0f * px;" in body and "SCREEN_HEIGHT_F - (f32)mFont_TEX_CHAR_HEIGHT_F32 * scale - 6.0f * px" in body)
    ck("S3d over the Play Online / Options overlay it is skipped when the corner would lie inside 0..320 (their centered hint line at y 218)",
       "if (overlay_open && right - w - 8.0f * px < SCREEN_WIDTH_F) return;" in body)
    ck("S3e declared in pc_pause_menu.h; pc_pause_menu.c includes pc_version.h + pc_dedicated.h",
       "void pc_version_corner_draw(struct game_s* game, int overlay_open);" in src("pc/include/pc_pause_menu.h") and '#include "pc_version.h"' in pm and '#include "pc_dedicated.h"' in pm)

    # every call site in the code tree (src/ + pc/src, comments stripped): exactly one, in the title menu draw
    calls = []
    for base in ("src", os.path.join("pc", "src")):
        for dp, dn, fn in os.walk(os.path.join(ROOT, base)):
            if "d2utmpah5FWd" in dp:
                continue
            for n in fn:
                if n.endswith((".c", ".cpp", ".h")) and "d2utmpah5FWd" not in n:
                    t = strip_comments(read(os.path.join(dp, n)))
                    for m in re.finditer(r"\bpc_version_corner_draw\s*\(", t):
                        line = t[t.rfind("\n", 0, m.start()) + 1:t.find("\n", m.start())]
                        if not re.match(r"\s*void pc_version_corner_draw", line):
                            calls.append((os.path.relpath(os.path.join(dp, n), ROOT).replace("\\", "/"), line.strip()))
    ck("S4 pc_version_corner_draw is called exactly ONCE in the whole code tree, from src/actor/ac_animal_logo.c (no in-game / graph.c overlay call) %s" % calls,
       len(calls) == 1 and calls[0][0] == "src/actor/ac_animal_logo.c")
    al = src("src/actor/ac_animal_logo.c")
    menu = func_body(al, "aAL_pc_menu_draw")
    ck("S4b the call is the LAST statement of aAL_pc_menu_draw (on top of the main menu / Play Online / Options), overlay flag = Options open or Play Online active",
       menu.rstrip().endswith("pc_version_corner_draw(game, actor->pc_options_open || pc_play_online_menu_active());"))
    drw = func_body(al, "aAL_actor_draw")
    ck("S4c aAL_pc_menu_draw runs only in the title's menu branch (g_pc_title_main_menu_visible = 1, pad connected, GAME_START..OUT) inside mFont_SetMatrix / UnSetMatrix",
       len(re.findall(r"\baAL_pc_menu_draw\(", strip_comments(al))) == 2  # definition + the one call
       and re.search(r"g_pc_title_main_menu_visible = 1; \}\n\s*aAL_pc_menu_draw\(logo_actor, game\);", drw)
       and drw.find("mFont_SetMatrix(graph, mFont_MODE_FONT);") < drw.find("aAL_pc_menu_draw(logo_actor, game);") < drw.find("mFont_UnSetMatrix(graph, mFont_MODE_FONT);"))
    gr = strip_comments(src("src/graph.c"))
    ck("S4d graph.c's per-frame overlay chain does not draw the version (in-game screens never show it)", "pc_version" not in gr)

    # ---- log line / dedicated
    mc = src("pc/src/pc_main.c")
    ck("S5 pc_main.c logs the version once at startup in every role: PC_LOG GENERAL when enabled, else the same '[GENERAL] ...' stdout line, before the dedicated startup",
       '#include "pc_version.h"' in mc
       and re.search(r'if \(g_pc_log_mask & PCL_GENERAL\) \{\n\s*PC_LOG\(PCL_GENERAL, "Animal Crossing PC Multiplayer v%s\\n", PC_VERSION_STRING\);\n\s*\} else \{\n\s*printf\("\[GENERAL\] Animal Crossing PC Multiplayer v%s\\n", PC_VERSION_STRING\);\n\s*\}\n\s*if \(g_pc_dedicated\) \{\n\s*pc_dedicated_startup', mc))
    dc = src("pc/src/pc_dedicated.c")
    ck("S6 the dedicated start line ends with version=<ver> (the harness regex 'starting: role=host port=(\\d+)' still matches) and `status` prints 'version:' right after 'dedicated: yes'",
       '"dedicated server starting: role=host port=%u observer=on (--host-observer init reused) console=on version=%s", (unsigned)port, PC_VERSION_STRING);' in dc
       and 'pc_ded_printf("  dedicated: yes\\n");\n    pc_ded_printf("  version: %s\\n", PC_VERSION_STRING);' in dc and '#include "pc_version.h"' in dc)
    lit = []
    for n in ("pc/src/pc_main.c", "pc/src/pc_dedicated.c", "pc/src/pc_pause_menu.c", "src/actor/ac_animal_logo.c"):
        lit += [(n, m.group(0)) for m in re.finditer(r'"v?\d+\.\d+\.\d+(?!\.\d)[^"]*"', strip_comments(src(n)))]
    ck("S7 no hard-coded version string in the files that show it %s" % lit, not lit)

    # ---- UPDATING.txt (the other per-release edit)
    up = src("pc/updater/UPDATING.txt")
    m = re.search(r"/blob/v(\d+\.\d+\.\d+)/docs/updater\.md", up)
    ck("S8 UPDATING.txt links a released guide (dev line: the previous release; the release commit links v<this version>) %s" % (m and m.group(1)),
       m is not None and (v["DEV"] == 1 or m.group(1) == "%d.%d.%d" % (v["MAJOR"], v["MINOR"], v["PATCH"])))

    # ---- release_0.3.1
    rb_p = os.path.join(REL, "scripts", "rebuild_from_commit.sh")
    if not os.path.isfile(rb_p):
        ck("S9 release_0.3.1/scripts/rebuild_from_commit.sh exists", False, rb_p)
    else:
        rb = read(rb_p)
        ck("S9 release_0.3.1/scripts/rebuild_from_commit.sh: VER=0.3.1, R=release_0.3.1, reads pc/include/pc_version.h of the commit, refuses DEV != 0",
           "\nVER=0.3.1\n" in rb and "\nR=/e/CodeStuff/AnimalCrossingMultiplayer/release_0.3.1\n" in rb and 'show "$C:pc/include/pc_version.h"' in rb
           and '[ "$HV" = "$VER" ]' in rb and '[ "$HDEV" = "0" ]' in rb)
        ck("S9b it no longer greps app.rc for a literal version; it checks app.rc DERIVES from pc_version.h; UPDATING.txt must link v$VER",
           not re.search(r"app\.rc[^\n]*grep -q '\"0\.", rb) and '#include "../include/pc_version.h"' in rb and "PC_VERSION_RC_NUMBERS" in rb and '"/blob/v$VER/"' in rb)
        ck("S9c the built exe's version resource must be $VER.0 in ProductVersion, FileVersion and FILEVERSION",
           '[ "$V" = "$VER.0|$VER.0|$VER.0" ]' in rb and "VersionInfo" in rb)
        ck("S9d nothing in release_0.3.1's scripts still names 0.3.0 / release_0.3.0",
           all("0.3.0" not in read(os.path.join(REL, "scripts", n)) for n in os.listdir(os.path.join(REL, "scripts"))))
        mv = re.search(r"^vnum\(\) \{.*\}$", rb, re.M)
        if mv and os.path.isfile(BASH):
            hp = os.path.join(ROOT, "pc", "include", "pc_version.h").replace("\\", "/")
            script = 'VH=$(cat "%s")\n%s\necho "$(vnum PC_VERSION_MAJOR).$(vnum PC_VERSION_MINOR).$(vnum PC_VERSION_PATCH) $(vnum PC_VERSION_DEV)"\n' % (hp, mv.group(0))
            r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30)
            got = r.stdout.strip()
            ck("S9e the release script's own pc_version.h parser (its vnum line) reads the working header as '%s' (expected '0.3.1 1')" % got, got == "0.3.1 1", r.stderr)
        else:
            ck("S9e the release script's vnum parser line exists (and Git Bash to run it)", False, (bool(mv), BASH))
        pub = read(os.path.join(REL, "publish_release.py"))
        ck("S9f release_0.3.1/publish_release.py: default --version 0.3.1, title 'Animal Crossing Multiplayer Beta ' + version (= ...Beta 0.3.1), tag 'v' + version",
           'ap.add_argument("--version", default="0.3.1")' in pub and 'a.title = a.title or "Animal Crossing Multiplayer Beta " + a.version' in pub and 'tag = "v" + a.version' in pub
           and "0.3.0" not in pub)
        rd = read(os.path.join(REL, "README.txt"))
        ck("S9g release_0.3.1/README.txt documents the one-place bump (pc_version.h, DEV 0) + UPDATING.txt + release notes",
           "pc/include/pc_version.h" in rd and "PC_VERSION_DEV 0" in rd and "UPDATING.txt" in rd and "release-notes-0.3.1.md" in rd and "v0.3.1" in rd)
        ck("S9h release_0.3.1/launcher/ACLauncher.exe is byte-identical to release_0.3.0's",
           os.path.isfile(os.path.join(REL, "launcher", "ACLauncher.exe"))
           and open(os.path.join(REL, "launcher", "ACLauncher.exe"), "rb").read() == open(os.path.join(REL, "..", "release_0.3.0", "launcher", "ACLauncher.exe"), "rb").read())

    print("-" * 60)
    print("%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
