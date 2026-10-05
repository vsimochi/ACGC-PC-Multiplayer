#!/usr/bin/env python3
"""test_town_sanitize_unit.py - M-G sanitized town transfer image: NATIVE UNIT test of pc/src/pc_town_sanitize.c (town_sanitize_selftest.c) + a source audit.

  U  town_sanitize_selftest.c + pc_town_sanitize.c compiled with the msys2 gcc -Wall -Wextra (must be warning-free) and run: the exported table tiles the whole file (every
     byte classified), every private byte equals the template (except the documented keep ranges), keep bytes equal the input, the backup equals the main Save, the BE
     checksums equal an independent implementation, determinism, idempotence, size / GAF / land-id / template refusals leave the output untouched, marker present
  S  source audit: pc_m_card.c mirrors every offset of the table with _Static_asserts, builds the templates with mPr_ClearPrivateInfo + the ARAM initialisers, refuses a
     sanitized image outside a CLIENT and never writes it; the host serves through pc_town_sanitize and re-validates; the D3 guard (HELLO NO_MIGRATE + host wins) is in place
No game process is started and no save is touched.
Usage: python test_town_sanitize_unit.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"
results = []


def check(desc, cond):
    results.append((desc, bool(cond)))
    print(("PASS - " if cond else "FAIL - ") + desc)


def read(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read().replace("\r\n", "\n")


def native_unit():
    scratch = os.path.join(tempfile.gettempdir(), "acmp_town_sanitize_unit")
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    exe = os.path.join(scratch, "town_sanitize_selftest.exe")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(GCC) + os.pathsep + env.get("PATH", "")
    src = [os.path.join(HERE, "town_sanitize_selftest.c"), os.path.join(PC, "src", "pc_town_sanitize.c")]
    cp = subprocess.run([GCC, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include")] + src + ["-o", exe], capture_output=True, text=True, env=env)
    check("U compile with msys2 gcc -Wall -Wextra succeeds", cp.returncode == 0)
    check("U compile is warning-free", cp.stderr.strip() == "")
    if cp.returncode != 0:
        print(cp.stderr)
        return
    rp = subprocess.run([exe], capture_output=True, text=True, env=env, timeout=120, cwd=scratch)
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
    mc, ng, mn, st = read("pc/src/pc_m_card.c"), read("pc/src/pc_net_game.c"), read("pc/src/pc_main.c"), read("pc/src/pc_settings.c")
    check("S pc_m_card.c: _Static_asserts mirror the table (GCI / Others / ARAM / Private / homes / animals / post office offsets)",
          len(re.findall(r'"M-G: [^"]*drifted"', mc)) >= 8 and "offsetof(Anmmem_c, letter) == 0x32" in mc and "offsetof(mHm_hs_c, mailbox) == 0x1A30" in mc)
    check("S pc_m_card.c: templates = mPr_ClearPrivateInfo + my_org_no_table 0..7 + pc_save_bswap_private(TO_BE), ARAM = pc_init_mail_entries / pc_init_diary_entries (BE)",
          "mPr_ClearPrivateInfo(p);" in mc and "p->my_org_no_table[j] = (u8)j;" in mc and "pc_save_bswap_private(p, PC_BSWAP_TO_BE);" in mc
          and "pc_init_mail_entries(blk);" in mc and "pc_init_diary_entries(blk);" in mc)
    check("S pc_m_card.c: the loader refuses a sanitized image outside a network CLIENT (box + exit 3 through pc_main) and the writer never writes while it is set",
          "PC_TS_MARKER_TEXT, PC_TS_MARKER_LEN) == 0" in mc and "pc_net_game_role() != PC_NETGAME_ROLE_CLIENT" in mc and "pc_main_refuse_sanitized_town();" in mc
          and "if (g_pc_save_sanitized) {" in mc and "void pc_main_refuse_sanitized_town(void)" in mn and "exit(3);" in mn)
    check("S pc_net_game.c: the host serves pc_town_sanitize() output (new buffer), re-validates it as a GCI of the same town and caches per (gen, input crc)",
          "pc_town_sanitize(buf, got, out, tpl)" in ng and "pc_save_validate_gci_buffer(out, PC_NETGAME_TOWN_FILE_SIZE, &t2)" in ng and "s_townsrv_san_cache.in_crc == in_crc" in ng
          and "m.flags = flags;" in ng)
    check("S pc_net_game.c: the client rejects a stream whose TOWN_INFO flag disagrees with the marker",
          "pc_town_gci_file_is_sanitized(ipaths.part) != ((info.flags & PC_NETGAME_TOWN_INFO_FLAG_SANITIZED) != 0u)" in ng)
    check("S pc_net_game.c: D3 guard -- client HELLO NO_MIGRATE (resident + sanitized cache), no upload on MIGRATE_REQUEST, host 'HOST WINS' (flag / town_serve sanitized / promote entry)",
          "h.flags |= (uint8_t)PC_NETGAME_REC_HELLO_FLAG_NO_MIGRATE;" in ng and "NOT uploading" in ng and "HOST WINS (%s)" in ng and "pcnetgame_rec_has_promote_entry(idx)" in ng
          and "pcnetgame_town_serve_mode() == 1" in ng)
    check("S pc_net_game.c: the friendship snapshot sends the letter only to the player it is about", "pcnetgame_friendship_snapshot_redact(&fe, i);" in ng and "fe->has_letter = 0;" in ng)
    check("S pc_main.c / pc_settings.c: --town-serve off|on|full and settings.ini words / 0..2",
          '"full"' in mn and "g_pc_town_serve_override = strcmp(argv[i + 1], \"full\") == 0 ? 2" in mn and 'strcmp(value, "full") == 0) g_pc_settings.town_serve = 2' in st)


if __name__ == "__main__":
    native_unit()
    source_audit()
    bad = [d for d, ok in results if not ok]
    print("\n%d checks, %d failed" % (len(results), len(bad)))
    sys.exit(1 if bad else 0)
