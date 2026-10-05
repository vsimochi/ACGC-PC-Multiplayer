#!/usr/bin/env python3
"""test_town_cache_unit.py - M-A (runtime Card-A directory) + M-B (town cache module) unit / audit / real-process test.

TIER: NATIVE UNIT + SOURCE AUDIT + two short REAL game process launches of a DISPOSABLE fixture copy.
  U   native unit (no game): town_cache_selftest.c + pc_town_cache.c compiled with the msys2 gcc -Wall -Wextra (must be warning-free) and run in a temp dir:
      accessor defaults / set-once, townkey formatting, paths (part is NEVER inside card_a), origin.ini round trip, find-by-server, CRC32, the in-order .part writer,
      the ATOMIC install (a failed / partial / corrupt download never replaces a valid cache; no .tmp / .part / .gci staged in card_a)
  S   source audit: no compile-time Card-A macro is left in pc_m_card.c, pc_card.c follows the runtime dir, the legacy migration is skipped with a town dir, the
      scan builds its names at runtime, town_serve defaults OFF, the new CLI flags are refused (exit 2) outside their role, the wire audit (wire_baseline) is green
  C   CLI refusals (the game exe returns exit 2 before initialising anything): --town-dir / --town-fetch without --connect, both together, --town-serve without --host
  A   M-A, real process: `--connect 127.0.0.1:1 --town-dir save/mp/towns/town_a` loads the save FROM THE TOWN DIR (log line), and a second launch with an EMPTY town dir
      does not pull the legacy save/DobutsunomoriP_MURA.gci into it; save/card_a and the legacy file are byte-identical (md5 over every file) before and after.
The fixture is a disposable copy (bin_fixture4_town, only the READ-ONLY bin_fixture4 is the source); the live save dir, bin_talkfix* and bin_fixture4 are never touched.
Usage: python test_town_cache_unit.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

PC = T.PC
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"
ROOT = os.path.abspath(os.path.join(PC, ".."))
results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


def read(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read().replace("\r\n", "\n")


def native_unit():
    scratch = os.path.join(tempfile.gettempdir(), "acmp_town_cache_unit")
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    exe = os.path.join(scratch, "town_cache_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    src = [os.path.join(HERE, "town_cache_selftest.c"), os.path.join(PC, "src", "pc_town_cache.c")]
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include")] + src + ["-o", exe], capture_output=True, text=True, env=env)
    check("U compile with msys2 gcc -Wall -Wextra succeeds", cp.returncode == 0)
    check("U compile is warning-free", cp.stderr.strip() == "")
    if cp.returncode != 0:
        print(cp.stderr)
        return
    rp = subprocess.run([exe, os.path.join(scratch, "data")], capture_output=True, text=True, env=env, timeout=120)
    out = rp.stdout
    passes = [ln[6:] for ln in out.splitlines() if ln.startswith("PASS: ")]
    fails = [ln[6:] for ln in out.splitlines() if ln.startswith("FAIL: ")]
    m = re.search(r"RESULT passed=(\d+) failed=(\d+)", out)
    for p in passes:
        results.append(("U " + p, True))
    for p in fails:
        check("U " + p, False)
    print("unit harness: %d passed, %d failed (exit %d)" % (len(passes), len(fails), rp.returncode))
    check("U harness exit code 0 and the RESULT line agrees with the PASS/FAIL lines", rp.returncode == 0 and m is not None and int(m.group(1)) == len(passes) and int(m.group(2)) == 0)


def source_audit():
    mc, cc, mn, cm, st_c, st_h = (read("pc/src/pc_m_card.c"), read("pc/src/pc_card.c"), read("pc/src/pc_main.c"), read("pc/CMakeLists.txt"),
                                  read("pc/src/pc_settings.c"), read("pc/include/pc_settings.h"))
    check("S pc_m_card.c: no PC_CARD_A_DIR / PC_GCI_PATH / PC_GCI_TMP_PATH macro (definition or use) is left; only the *_LEGACY ones remain",
          re.search(r"\bPC_CARD_A_DIR\b|\bPC_GCI_PATH\b|\bPC_GCI_TMP_PATH\b", mc) is None and "PC_GCI_PATH_LEGACY" in mc)
    check("S pc_m_card.c: the writer / loader / scan / reload / travel sites use pc_gci_path() / pc_gci_tmp_path() / pc_card_a_dir() (>= 20 uses)",
          len(re.findall(r"\bpc_gci_path\(\)|\bpc_gci_tmp_path\(\)|\bpc_card_a_dir\(\)", mc)) >= 20 and "pc_save_write_gci_to(pc_gci_path(), pc_gci_tmp_path())" in mc)
    check("S pc_m_card.c: the legacy migration is skipped while a town dir is active", "if (!pc_card_town_dir_active()) {" in mc and "pc_save_migrate_legacy();" in mc
          and mc.index("if (!pc_card_town_dir_active()) {", mc.index("int pc_save_check_and_load")) < mc.index("pc_save_migrate_legacy();", mc.index("int pc_save_check_and_load")))
    check("S pc_m_card.c: pc_ensure_save_dirs creates the town card dir (mkdir -p) instead of save/card_a when a town dir is set", "pc_town_mkdirs(pc_card_a_dir())" in mc)
    check("S pc_m_card.c: the GCI scan builds its names from pc_card_a_dir() at runtime", 'snprintf(gci_name_a, sizeof(gci_name_a), "%s/DobutsunomoriP_MURA.gci", pc_card_a_dir());' in mc)
    check("S pc_card.c: channel 0 follows pc_card_a_dir() (pc_card_scan_for_gci sees the town dir), card_b is unchanged",
          "return pc_card_a_dir();" in cc and "if (chan == 1) return card_dir[1];" in cc)
    check("S pc_m_card.c: pc_save_validate_gci_buffer/_file (exact size, GAF, town identity = the live FNV-1a hash) and pc_save_town_gen exist; town_gen bumps in pc_save_write_gci only after a real write",
          "int pc_save_validate_gci_buffer(" in mc and "int pc_save_validate_gci_file(" in mc and "s_pc_town_gen++;" in mc and mc.count("s_pc_town_gen++;") == 1
          and "len != (size_t)GCI_HEADER_SIZE + (size_t)GCI_FILE_DATA_SIZE" in mc)
    check("S CMake: pc_town_cache.c is part of PC_SOURCES", "src/pc_town_cache.c" in cm)
    check("S settings: town_serve defaults OFF (0), is parsed 0/1 only, is written by the defaults text and the writer; --town-serve override default -1",
          ".town_serve = 0," in st_c and "int g_pc_town_serve_override = -1;" in st_c and "if (val == 0 || val == 1) g_pc_settings.town_serve = val;" in st_c
          and '"town_serve = 0\\n"' in st_c and 'fprintf(f, "town_serve = %d\\n", g_pc_settings.town_serve);' in st_c and "int town_serve;" in st_h)
    check("S pc_main.c: --town-dir / --town-fetch are CLIENT-only (exit 2 otherwise), mutually exclusive; --town-serve is HOST-only and needs on|off",
          '"--town-dir"' in mn and '"--town-fetch"' in mn and '"--town-serve"' in mn and "g_pc_net_role != 2" in mn and "g_pc_net_role != 1" in mn
          and "cannot be combined with --town-fetch" in mn and "pc_card_set_town_dir(g_pc_town_dir)" in mn)
    check("S pc_main.c: --town-dir is NOT in the --help text (hidden); --town-fetch / --town-serve are documented there",
          '"  --town-dir' not in mn and '"  --town-fetch' in mn and '"  --town-serve' in mn)
    check("S pc_main.c: the legacy --connect start is unchanged without --town-fetch, and the prefetch runs after pc_platform_init and BEFORE pc_disc_init / boot_main",
          "g_pc_net_role == 2 && !g_pc_town_fetch" in mn and mn.index("pc_platform_init();\n#ifdef PC_LOW_ADDRESS_64") < mn.index("pc_net_game_town_prefetch(")
          < mn.index("pc_disc_init();\n    if (!pc_assets_init())") < mn.index("boot_main(argc"))
    check("S pc_main.c: a fetch with no usable town shows a message box (AC_TOWN_NO_MSGBOX test hook suppresses it) and never boots a wrong town (exit 3)",
          "SDL_ShowSimpleMessageBox(SDL_MESSAGEBOX_ERROR, \"Animal Crossing - town transfer\"" in mn and 'getenv("AC_TOWN_NO_MSGBOX")' in mn and "return 3;" in mn)
    import wire_baseline
    wb = []
    wire_baseline.run(lambda d, c: wb.append((d, c)), ROOT)
    for d, c in wb:
        if not c:
            check("S " + d, False)
    check("S wire_baseline: every wire audit is green with the 62..65 additions (%d checks)" % len(wb), bool(wb) and all(c for _d, c in wb))
    check("S wire_baseline.EXPECTED_MAX_MSG_ID is 66", wire_baseline.EXPECTED_MAX_MSG_ID == 66)


def run_exe(dst, args, timeout=30):
    return subprocess.run([os.path.join(dst, "AnimalCrossing.exe")] + args, cwd=dst, capture_output=True, text=True, timeout=timeout)


def cli_refusals(dst):
    r = run_exe(dst, ["--town-dir", "save/mp/towns/x"])
    check("C --town-dir without --connect: exit 2 + usage", r.returncode == 2 and "usage:" in r.stderr and "CLIENT-only" in r.stderr)
    r = run_exe(dst, ["--town-fetch"])
    check("C --town-fetch without --connect: exit 2", r.returncode == 2 and "CLIENT-only" in r.stderr)
    r = run_exe(dst, ["--connect", "127.0.0.1:1", "--town-dir", "x", "--town-fetch"])
    check("C --town-dir together with --town-fetch: exit 2", r.returncode == 2 and "cannot be combined" in r.stderr)
    r = run_exe(dst, ["--town-serve", "on"])
    check("C --town-serve without --host: exit 2", r.returncode == 2 and "HOST-only" in r.stderr)
    r = run_exe(dst, ["--host", "9", "--town-serve", "maybe"])
    check("C --town-serve with a bad value: exit 2", r.returncode == 2 and "on or off" in r.stderr)
    r = run_exe(dst, ["--host", "9", "--town-fetch"])
    check("C --town-fetch with --host: exit 2", r.returncode == 2)


def launch_until(dst, args, rx_ok, timeout, log_name):
    """Starts the game (verbose, log file), waits for rx_ok in the log (or exit / timeout), terminates it. Returns (log text, matched)."""
    log = T.log_path(log_name)
    with open(log, "wb") as fp:
        p = subprocess.Popen([os.path.join(dst, "AnimalCrossing.exe")] + args + ["--verbose"], cwd=dst, stdout=fp, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        matched = False
        try:
            while time.monotonic() < deadline and p.poll() is None:
                time.sleep(0.25)
                with open(log, "rb") as r:
                    if re.search(rx_ok, r.read().decode("utf-8", "replace")):
                        matched = True
                        break
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
    with open(log, "rb") as r:
        return r.read().decode("utf-8", "replace"), matched


def m_a_real(dst):
    save = os.path.join(dst, "save")
    card_a = os.path.join(save, "card_a")
    gci_name = "DobutsunomoriP_MURA.gci"
    src_gci = os.path.join(card_a, gci_name)
    legacy = os.path.join(save, gci_name)
    check("A fixture has the 4-resident GCI in save/card_a", os.path.isfile(src_gci))
    shutil.copy2(src_gci, legacy)  # the legacy flat-layout trap: pc_save_migrate_legacy would move it into a new dir that has no GCI
    town_a = os.path.join(save, "mp", "towns", "town_a", "card_a")
    town_b = os.path.join(save, "mp", "towns", "town_b")
    os.makedirs(town_a)
    shutil.copy2(src_gci, os.path.join(town_a, gci_name))
    card_before, legacy_before, town_before = T.tree_md5(card_a), T.md5_file(legacy), T.md5_file(os.path.join(town_a, gci_name))
    ls_save_before = sorted(os.listdir(save))

    text, ok = launch_until(dst, ["--connect", "127.0.0.1:1", "--town-dir", "save/mp/towns/town_a"], r"GCI save loaded successfully|GCI save load FAILED|No save file found", 120, "town_unit_a.log")
    check("A (town dir with a GCI) the save is loaded from the TOWN dir: 'Found GCI save: save/mp/towns/town_a/card_a/DobutsunomoriP_MURA.gci'",
          "Found GCI save: save/mp/towns/town_a/card_a/DobutsunomoriP_MURA.gci" in text)
    check("A the load succeeded (GCI save loaded successfully)", ok and "GCI save loaded successfully" in text)
    check("A no legacy migration ran ('Migrating save' absent) and nothing was read from save/card_a", "Migrating save" not in text and "Found GCI save: save/card_a" not in text and "GCI scan: found 'save/card_a" not in text)
    check("A save/card_a is byte-identical (md5 of every file, same file set) and the legacy file is byte-identical and still in place",
          T.tree_md5(card_a) == card_before and os.path.isfile(legacy) and T.md5_file(legacy) == legacy_before)
    check("A the town cache GCI was not written by the client (md5 unchanged)", T.md5_file(os.path.join(town_a, gci_name)) == town_before)

    text, ok = launch_until(dst, ["--connect", "127.0.0.1:1", "--town-dir", "save/mp/towns/town_b"], r"No save file found|GCI save loaded successfully", 120, "town_unit_b.log")
    check("A (EMPTY town dir) the process looked for the save in the TOWN dir and found none: 'No GCI save at save/mp/towns/town_b/card_a/DobutsunomoriP_MURA.gci'",
          "No GCI save at save/mp/towns/town_b/card_a/DobutsunomoriP_MURA.gci" in text and "No save file found" in text)
    check("A the legacy save/DobutsunomoriP_MURA.gci was NOT migrated into the empty town dir (still in place, md5 unchanged, town dir has no GCI)",
          "Migrating save" not in text and os.path.isfile(legacy) and T.md5_file(legacy) == legacy_before
          and not os.path.isfile(os.path.join(town_b, "card_a", gci_name)))
    check("A save/card_a still byte-identical after both launches and no new entry appeared in save/ besides mp (and the legacy file)",
          T.tree_md5(card_a) == card_before and set(os.listdir(save)) - set(ls_save_before) <= {"mp"})


def main():
    native_unit()
    source_audit()
    dst = T.make_fixture("bin_fixture4_town")
    try:
        cli_refusals(dst)
        m_a_real(dst)
    finally:
        pass
    bad = [d for d, c in results if not c]
    print("\n%d checks, %d failed" % (len(results), len(bad)))
    for d in bad:
        print("FAILED: " + d)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
