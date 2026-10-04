#!/usr/bin/env python3
"""test_guest_g2_src.py - GUESTS G2 (empty-economy rule, guest profile, --guest flag) SOURCE AUDIT (no game process).

  A  G2.1 the fresh-character predicate: defined once in pc_mp_guests.c (pure: no game headers), called from exactly ONE place -- pcnetgame_rec_process_upload --
     under `kind == MIGRATE_UPLOAD && idx >= PLAYER_NUM` (a guest slot's first migrate), after the generic field validation and BEFORE the backup / merge / rev++; the
     refusal goes through the existing INVALID_FIELD ack (detail GUEST_NOT_FRESH = 13, reason in the high byte, no wire change, no protocol bump); every offset the
     predicate hard codes is static-asserted against offsetof(Private_c, ...) in pc_net_game.c; no other source file calls it (residents / later uploads / PUSH paths)
  B  G2.2 pc_guest_profile.c/.h: listed in CMake PC_SOURCES, libc + pc_mp_guests only (no game headers), uses the OS CSPRNG (pc_mp_guests_random_bytes) and the shared name
     rules, atomic tmp -> replace write, never writes on a parse error (the only fopen "wb" is the atomic writer, reached only from the missing-file branch)
  C  G2.3 --guest in pc_main.c: parsed, listed in --help, validated BEFORE the bootstrap-guest validation and before any init; CLIENT only (role 2), refuses --host /
     --dedicated / --host-observer / --bootstrap-resident / --bootstrap-guest with exit 2 + usage; loads / creates the profile, turns it into the --bootstrap-guest spec (the SAME
     arrival path, pc_bootstrap_guest_poll is not duplicated or edited), and exits 2 on a bad profile
  D  untouched: the wire (no protocol version bump, wire_baseline), the arrival code in pc_m_card.c, the title menu / actors (no G3 work); EOL of every edited file
Tier: SOURCE AUDITED. Exit code 0 when all checks pass."""
import os
import re
import sys

import net_spike_lib as L
import test_guest_src as S
import wire_baseline

ROOT = S.ROOT


def raw_bytes(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
        return f.read()


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    cmake = S.read("pc/CMakeLists.txt")
    ng_raw = S.read("pc/src/pc_net_game.c")
    ng = S.mask(ng_raw)
    nf = S.functions(ng)
    gc_raw = S.read("pc/src/pc_mp_guests.c")
    gh_raw = S.read("pc/include/pc_mp_guests.h")
    pc_raw = S.read("pc/src/pc_guest_profile.c")
    pcm = S.mask(pc_raw)
    ph_raw = S.read("pc/include/pc_guest_profile.h")
    main_raw = S.read("pc/src/pc_main.c")
    mm = S.mask(main_raw)
    mf = S.functions(mm)

    # ---------------------------------------------------------------- A
    up = S.body(ng, nf, "pcnetgame_rec_process_upload")
    ck("A pcnetgame_rec_process_upload exists", up != "")
    all_src = {}
    src_dir = os.path.join(ROOT, "pc", "src")
    for fn in sorted(os.listdir(src_dir)):
        if fn.endswith((".c", ".cpp", ".h")):
            all_src[fn] = S.read("pc/src/" + fn)
    for fn in sorted(os.listdir(os.path.join(ROOT, "pc", "include"))):
        if fn.endswith(".h"):
            all_src["include/" + fn] = S.read("pc/include/" + fn)
    callers = {fn: len(re.findall(r"\bpc_mp_guest_record_fresh_check\(", S.mask(t))) for fn, t in all_src.items()}
    callers = {k: v for k, v in callers.items() if v}
    ck("A the predicate is DEFINED in pc_mp_guests.c, declared in pc_mp_guests.h, and called from exactly one place: pc_net_game.c (callers: %s)" % callers,
       callers == {"pc_mp_guests.c": 1, "pc_net_game.c": 1, "include/pc_mp_guests.h": 1}
       and "int pc_mp_guest_record_fresh_check(const uint8_t* r, size_t len, unsigned* detail) {" in gc_raw)
    ck("A that single call is inside pcnetgame_rec_process_upload (the upload handler of the D3 record protocol)", len(re.findall(r"\bpc_mp_guest_record_fresh_check\(", up)) == 1)
    m = re.search(r"if \(bad == 0 && kind == PC_NETGAME_REC_KIND_MIGRATE_UPLOAD && idx >= PLAYER_NUM\) \{(.*?)\n        \}\n        if \(bad == 0 && kind == PC_NETGAME_REC_KIND_MIGRATE_UPLOAD\) \{", up, re.S)
    ck("A it is guarded by `kind == MIGRATE_UPLOAD && idx >= PLAYER_NUM` (a guest slot's FIRST migrate only: never a resident, never a later UPLOAD), right after the generic "
       "pcnetgame_rec_validate_fields and before the replaced-record backup", m is not None and "pc_mp_guest_record_fresh_check(rx, PC_NETGAME_REC_SIZE, &fresh_off)" in m.group(1)
       and up.index("pcnetgame_rec_validate_fields(") < up.index("pc_mp_guest_record_fresh_check(") < up.index("memcpy(s_rec_backup[idx], host_be"))
    ck("A the verdict goes through the existing INVALID_FIELD refuse path: bad = GUEST_NOT_FRESH | reason << 8, then the SAME `if (bad != 0) { ... INVALID_FIELD ...; return; }` "
       "before pcnetgame_rec_merge_into_save / rev++ (nothing is merged or stored)",
       m is not None and "bad = (uint16_t)(PC_NETGAME_REC_FIELD_GUEST_NOT_FRESH | ((uint16_t)fresh_bad << 8));" in m.group(1)
       and up.index("pc_mp_guest_record_fresh_check(") < up.index("if (bad != 0) {") < up.index("PC_NETGAME_REC_ACK_INVALID_FIELD, bad,") < up.index("pcnetgame_rec_merge_into_save(idx, &s_rec_scratch_b);")
       < up.index("slot->rev++;"))
    ck("A the refusal log line says 'first MIGRATE REFUSED (not a fresh character)' with the reason text, the guest slot and the record offset, and that nothing was stored",
       "first MIGRATE REFUSED (not a fresh character): %s (record offset 0x%04X)" in up and "nothing stored, the guests.dat entry is unchanged" in up and "pc_mp_guest_fresh_reason_str(fresh_bad)" in up)
    ck("A the new INVALID_FIELD detail PC_NETGAME_REC_FIELD_GUEST_NOT_FRESH = 13 is a detail value only: no new message type, ack status or struct (the wire is unchanged)",
       "#define PC_NETGAME_REC_FIELD_GUEST_NOT_FRESH 13u" in ng_raw and ng_raw.count("PC_NETGAME_REC_FIELD_GUEST_NOT_FRESH") == 2
       and "#define PC_NETGAME_PROTOCOL_VERSION 8u" in S.read("pc/include/pc_net_game.h"))
    pred = S.body(S.mask(gc_raw), S.functions(S.mask(gc_raw)), "pc_mp_guest_record_fresh_check")
    ck("A the predicate is PURE: pc_mp_guests.c includes no game header (only pc_mp_guests.h / pc_mp_records.h + libc / OS) and the predicate reads only its buffer",
       re.findall(r'#include "([^"]+)"', gc_raw) == ["pc_mp_guests.h", "pc_mp_records.h"] and pred != "" and "Save_" not in pred and "Common_" not in pred and "printf" not in pred
       and "static" not in pred.split("{", 1)[0])
    for what, rx in (("pockets", r"0x68 \+ i \* 2"), ("item conditions", r"fr_be32\(r, 0x88\)"), ("wallet", r"fr_be32\(r, 0x8C\)"), ("bank", r"fr_be32\(r, 0x122C\)"),
                     ("loan == 100", r"fr_be32\(r, 0x90\) != \(uint32_t\)PC_MP_FRESH_LOAN"), ("lotto", r"r\[0x86\] != 0u \|\| r\[0x87\] != 0u"), ("equipment", r"fr_be16\(r, 0x4A4\)"),
                     ("letter gifts (present at +0x2C of each 0x12A letter)", r"0x4E0 \+ i \* 0x12A \+ 0x2C"), ("catalog orders", r"fr_all_zero\(r, 0x10A8, 0x14\)"),
                     ("catalog bit budget", r"bits > \(unsigned\)PC_MP_FRESH_CATALOG_MAX_BITS"), ("quests", r"0x94 \+ i \* 0x28"), ("state_flags == 1", r"fr_be32\(r, 0x2348\) != 1u")):
        ck("A the predicate checks %s" % what, re.search(rx, pred) is not None)
    ck("A pc_net_game.c static-asserts every offset the predicate hard codes (offsetof(Private_c, ...)): deliveries, errands, cloth, Mail_c.present, destiny, aircheck, maps, state_flags, ...",
       "Guests G2.1: fresh-guest predicate offsets" in ng_raw and all(x in ng_raw for x in ("offsetof(Mail_c, present) == 0x2C", "offsetof(Private_c, deliveries) == 0x0094", "offsetof(Private_c, state_flags) == 0x2348",
                                                                                       "offsetof(Private_c, maps) == 0x11DC", "offsetof(Private_c, ecard_letter_data) == 0x23E0")))
    ck("A the predicate is not wired into anything else: the PUSH / HELLO / resident paths and the 'guest first contact' blank-record creation do not mention it",
       "fresh_check" not in S.body(ng, nf, "pcnetgame_rec_handle_hello") and "fresh_check" not in S.body(ng, nf, "pcnetgame_rec_merge_into_save") and "fresh_check" not in S.body(ng, nf, "pcnetgame_rec_start_push"))

    # ---------------------------------------------------------------- B
    ck("B pc_guest_profile.c is listed in pc/CMakeLists.txt PC_SOURCES (next to pc_mp_guests.c)",
       re.search(r"set\(PC_SOURCES.*?\$\{CMAKE_CURRENT_SOURCE_DIR\}/src/pc_mp_guests\.c\s+\$\{CMAKE_CURRENT_SOURCE_DIR\}/src/pc_guest_profile\.c", cmake, re.S) is not None and cmake.count("pc_guest_profile.c") == 1)
    ck("B the module is pure C: it includes only pc_guest_profile.h / pc_mp_guests.h and libc / OS headers (no game / decomp header)",
       re.findall(r'#include "([^"]+)"', pc_raw) == ["pc_guest_profile.h", "pc_mp_guests.h"] and re.findall(r'#include "([^"]+)"', ph_raw) == [])
    ck("B ids come from the OS CSPRNG (pc_mp_guests_random_bytes) with no weak fallback (no rand / time / srand anywhere)", "pc_mp_guests_random_bytes(b, sizeof(b))" in pcm
       and not re.search(r"\b(rand|srand|time|clock)\s*\(", pcm))
    ck("B the name / reserved rules are the shared helpers pc_mp_guests_name_valid / pc_mp_guests_name_reserved", "pc_mp_guests_name_valid(nb)" in pcm and "pc_mp_guests_name_reserved(nb)" in pcm)
    ck("B the ONLY file write is the atomic writer (tmp -> flush + commit -> replace), reached only from the missing-file branch; a parse error returns before any write",
       len(re.findall(r'fopen\([^)]*"wb"\)', pcm)) == 1 and 'snprintf(tmp, sizeof(tmp), "%s.tmp", path);' in pcm and "rename_over(tmp, path)" in pcm and "_commit(_fileno(fp))" in pcm
       and pcm.count("write_atomic(") == 2)
    lc = S.body(pcm, S.functions(pcm), "pc_guest_profile_load_or_create")
    ck("B load_or_create: a write happens only when fopen fails with ENOENT; an existing file that cannot be parsed returns PC_GUEST_PROFILE_ERR with the file preserved (no write, no rename, no remove)",
       "errno != ENOENT" in lc and lc.count("write_atomic(") == 1 and lc.index("write_atomic(") < lc.index("pc_guest_profile_parse(") and "remove(" not in lc and "rename" not in lc
       and "the file was preserved unchanged" in lc)
    ck("B every key is required, unknown / duplicate keys are refused, ids are 1..0xFFFE, gender 0|1, face 0..7",
       all(x in pcm for x in ("unknown key", "the key appears more than once", "missing (every key is required", "must be 0 (male) or 1 (female)", "must be 0..7", "1..0xFFFE")))
    ck("B the header documents the immutability rule (name / home_town / player_id / land_id are the guest KEY: a change makes a NEW guest; gender / face seed a new guest only)",
       "IMMUTABLE in practice" in ph_raw and "makes a NEW guest" in ph_raw and "only the SEED of a brand-new guest" in ph_raw)

    # ---------------------------------------------------------------- C
    start = main_raw.index("if (g_pc_guest) {")
    blk = main_raw[start:main_raw.index("/* Guests G2: --bootstrap-guest is a CLIENT-only TEST hook", start)]
    ck("C --guest is parsed (`strcmp(argv[i], \"--guest\") == 0` -> g_pc_guest = 1) and --help documents it", 'else if (strcmp(argv[i], "--guest") == 0) {\n            g_pc_guest = 1;' in main_raw
       and '"  --guest             CLIENT-only (requires --connect' in main_raw)
    ck("C the guest block runs BEFORE the --bootstrap-guest role check / validation and before --dedicated / --host-observer handling (so nothing is initialised first)",
       main_raw.index("if (g_pc_guest) {") < main_raw.index("if (g_pc_bootstrap_guest != NULL && (g_pc_net_role != 2") < main_raw.index("pc_bootstrap_guest_validate(g_pc_bootstrap_guest)")
       < main_raw.index("[DEDICATED] REFUSED: --dedicated is a HOST-only option") < main_raw.index("[NET][OBSERVER] REFUSED: --host-observer is a HOST-only option"))
    for opt in ("--host", "--dedicated", "--host-observer", "--bootstrap-resident", "--bootstrap-guest"):
        ck("C --guest refuses %s (exit 2 + usage)" % opt, 'strcmp(argv[a], "%s") == 0' % opt in blk)
    ck("C the refusals print `--guest: REFUSED` + the usage line and `return 2`; CLIENT role (g_pc_net_role == 2, i.e. --connect) is required",
       blk.count("return 2;") == 3 and "g_pc_net_role != 2" in blk and "usage: AnimalCrossing --connect HOST[:PORT] --guest" in blk and blk.count("[PC] --guest: REFUSED") == 3)
    ck("C the profile is loaded / created with the module and a bad profile exits 2 with the module's diagnostic (bad key named): pc_guest_profile_load_or_create(PC_GUEST_PROFILE_PATH, ...)",
       "pc_guest_profile_load_or_create(PC_GUEST_PROFILE_PATH, &gp, gerr, sizeof(gerr))" in blk and "bad guest profile" in blk and "pc_guest_profile_spec(&gp, g_pc_guest_spec" in blk)
    ck("C it then drives the SAME arrival path: g_pc_bootstrap_guest = g_pc_guest_spec (the --bootstrap-guest spec); pc_bootstrap_guest_poll is neither duplicated nor edited by this change",
       "g_pc_bootstrap_guest = g_pc_guest_spec;" in blk and not re.search(r"pc_guest_profile", S.read("pc/src/pc_m_card.c")) and not re.search(r"pc_guest_profile|g_pc_guest\b", S.read("pc/src/pc_vi.c")))
    ck("C --bootstrap-guest stays the TEST hook (its own parse + early validation unchanged)",
       'strcmp(argv[i], "--bootstrap-guest") == 0 && i + 1 < argc' in main_raw and "pc_bootstrap_guest_validate(g_pc_bootstrap_guest)" in main_raw)

    # ---------------------------------------------------------------- D
    ck("D no protocol version bump (PC_NETGAME_PROTOCOL_VERSION is still the value the wire baseline pins) and the wire baseline is green", wire_baseline_ok())
    ck("D no G3 work: the title menu actor and the arrival code are not touched (no Join-as-Guest item, no pc_guest_profile reference in ac_animal_logo.c / pc_m_card.c)",
       "Join as Guest" not in S.read("src/actor/ac_animal_logo.c") and "pc_guest_profile" not in S.read("pc/src/pc_m_card.c"))
    eol_ok = True
    for rel, crlf in (("pc/src/pc_net_game.c", True), ("pc/src/pc_main.c", True), ("pc/CMakeLists.txt", None), ("pc/src/pc_mp_guests.c", False), ("pc/include/pc_mp_guests.h", False),
                      ("pc/src/pc_guest_profile.c", False), ("pc/include/pc_guest_profile.h", False), ("pc/tools/net_spike/guest_g2_selftest.c", False),
                      ("pc/tools/net_spike/test_guest_g2_unit.py", False), ("pc/tools/net_spike/test_guest_g2_protocol.py", False), ("pc/tools/net_spike/test_guest_g2_src.py", False)):
        b = raw_bytes(rel)
        n_crlf, n_lf = b.count(b"\r\n"), b.count(b"\n")
        good = True if crlf is None else ((n_crlf == n_lf) if crlf else (n_crlf == 0))
        if crlf is None:
            good = n_crlf in (0, n_lf)
        if not good:
            print("   EOL mismatch:", rel, n_crlf, n_lf)
        eol_ok = eol_ok and good
    ck("D every edited file keeps its EOL (CRLF worktree files fully CRLF, LF files with no CR)", eol_ok)
    return L.summary_and_exit_code(results)


def wire_baseline_ok():
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    return bool(wb) and all(c for _d, c in wb) and sorted(dict(wire_baseline.c_message_ids(S.read("pc/src/pc_net_game.c"))).values()) == list(range(1, 59))


if __name__ == "__main__":
    sys.exit(main())
