#!/usr/bin/env python3
"""test_guest_profiles_src.py - SOURCE AUDIT of the named guest profiles (--guest-profile NAME): several independent guests on ONE PC.

TIER: source audit (no game, no network). Pins what the behaviour tests cannot see:
  A  the default profile is unchanged: save/mp/guest.ini + save/mp/guest_token.dat (macros, path builder formats, the old load_or_create untouched and still the default creator)
  B  the client token file is profile aware: pc_net_game.c reads / writes it only through pc_guest_token_path() (no PC_MP_GUEST_TOKEN_PATH left in code)
  C  the title menu READS the selected profile and never creates it while drawing; only the join (title item / --guest) creates; ONE source of truth (the module's selection,
     set only by pc_main.c) feeds main, the menu and the token path
  D  flag handling: --guest-profile parsing / refusals, implies --guest, same exclusivity block, --help text
  E  the module: validation rules, read-only reader / sibling scan (no write, no remove, no rename), the only writer stays the atomic one, CSPRNG with the test seam NULL by
     default, uniqueness mechanism (sibling scan + bounded redraw on name AND full identity)
  F  no protocol / wire change
Usage: python test_guest_profiles_src.py
"""
import os
import re
import sys

import net_spike_lib as L
import test_guest_src as S
import wire_baseline


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    ph = S.read("pc/include/pc_guest_profile.h")
    pc_raw = S.read("pc/src/pc_guest_profile.c")
    pcm = S.mask(pc_raw)
    pf = S.functions(pcm)
    gh = S.read("pc/include/pc_mp_guests.h")
    ng_raw = S.read("pc/src/pc_net_game.c")
    ng = S.mask(ng_raw)
    main_raw = S.read("pc/src/pc_main.c")
    mm = S.mask(main_raw)
    card_raw = S.read("pc/src/pc_m_card.c")
    cm = S.mask(card_raw)
    cf = S.functions(cm)
    logo = S.read("src/actor/ac_animal_logo.c")

    # ---------------------------------------------------------------- A: default unchanged
    ck("A the default macros are unchanged: PC_GUEST_PROFILE_PATH 'save/mp/guest.ini', PC_MP_GUEST_TOKEN_PATH 'save/mp/guest_token.dat', PC_GUEST_TOKEN_DEFAULT_PATH the same string",
       '#define PC_GUEST_PROFILE_PATH "save/mp/guest.ini"' in ph and '#define PC_MP_GUEST_TOKEN_PATH    "save/mp/guest_token.dat"' in gh
       and '#define PC_GUEST_TOKEN_DEFAULT_PATH "save/mp/guest_token.dat"' in ph)
    fp = S.body(pcm, pf, "pc_guest_profile_file_path")
    ck("A the path builder: no profile -> '%s/guest.ini' / '%s/guest_token.dat' (the default names), a profile -> '%s/guest_%s.ini' / '%s/guest_token_%s.dat' with the FOLDED name",
       '"%s/guest_token.dat" : "%s/guest.ini"' in fp and '"%s/guest_token_%s.dat" : "%s/guest_%s.ini"' in fp and "pc_guest_profile_fold(profile, low)" in fp
       and "pc_guest_profile_name_check(profile, NULL, 0)" in fp)
    ck("A the legacy creator is untouched and remains the DEFAULT creator: pc_guest_profile_load_or_create(path) still exists and the named API calls it for a NULL / empty profile",
       "int pc_guest_profile_load_or_create(const char* path" in ph and "return pc_guest_profile_load_or_create(path, out, err, errcap);" in S.body(pcm, pf, "pc_guest_profile_load_or_create_in"))
    ck("A with NO selection the selected paths fall back to the default macros (PC_GUEST_PROFILE_PATH / PC_GUEST_TOKEN_DEFAULT_PATH)",
       'snprintf(buf, sizeof(buf), "%s", PC_GUEST_PROFILE_PATH);' in S.body(pcm, pf, "pc_guest_profile_selected_path")
       and 'snprintf(buf, sizeof(buf), "%s", PC_GUEST_TOKEN_DEFAULT_PATH);' in S.body(pcm, pf, "pc_guest_token_path") and "static char s_selected[PC_GUEST_PROFILE_ARG_MAX + 1];" in pcm)

    # ---------------------------------------------------------------- B: token file
    ck("B pc_net_game.c no longer uses PC_MP_GUEST_TOKEN_PATH in code (it uses pc_guest_token_path()); it includes pc_guest_profile.h",
       "PC_MP_GUEST_TOKEN_PATH" not in ng and ng_raw.count("pc_guest_token_path()") >= 7 and '#include "pc_guest_profile.h"' in ng_raw)
    ck("B the token file is LOADED (pc_mp_gtoken_load) and SAVED (pc_mp_gtoken_save) through the profile aware path, nowhere else: 1 load + 1 save in the client load/save helpers (pc_guest_token_path()) "
       "and, since M2, exactly ONE more save = the read-only legacy-token fallback copy-forward into the STORE character's per-town file (store_tp = pc_guest_token_path())",
       "pc_mp_gtoken_load(pcnetgame_client_token_path(), &s_client_gtk, &s_client_gtk_unreadable)" in ng and "pc_mp_gtoken_save(pc_guest_token_path(), &s_client_gtk)" in ng
       and "return pc_guest_token_path();" in ng  # M-E: pcnetgame_client_token_path() = pc_guest_token_path() for a guest (and a store character's resident); save/mp/resident_token.dat only for a legacy resident
       and len(re.findall(r"pc_mp_gtoken_load\(", ng)) == 1 and len(re.findall(r"pc_mp_gtoken_save\(", ng)) == 3  # guest token, legacy copy-forward, M-E resident token (tp = pcnetgame_client_token_path())
       and "const char* store_tp = pc_guest_token_path();" in ng and "pc_mp_gtoken_save(store_tp, &s_client_gtk)" in ng
       and ng.count("pc_session_legacy_token_lookup(") == 1)
    ck("B the per-town token override: pc_guest_token_path() itself is NOT redefined here; the ONLY place the active town is switched is pcnetgame_client_build_ext via pc_session_select_town (the token file is reloaded "
       "only when it returned true; a LEGACY character never changes), and the legacy token file is only READ (lookup) by the fallback",
       ng.count("pc_session_select_town(") == 1 and re.search(r"if \(pc_session_select_town\(town->land_name, town->land_id, town->terrain_hash\)\) \{\s*s_client_gtk_loaded = 0;", ng))
    ck("B no other source file references the token path macro for a client file open (pc_mp_guests.h keeps the macro for the default only)",
       not [f for f in os.listdir(os.path.join(S.ROOT, "pc", "src")) if f.endswith((".c", ".cpp")) and f != "pc_mp_guests.c" and "PC_MP_GUEST_TOKEN_PATH" in S.mask(S.read("pc/src/" + f))])

    # ---------------------------------------------------------------- C: title menu reads, join creates, one source of truth
    lab = S.body(cm, cf, "pc_guest_title_label")
    join = S.body(cm, cf, "pc_guest_title_join")
    ck("C the title-menu LABEL only READS the selected profile (pc_guest_profile_read_selected) and never creates: no load_or_create, no write in its body",
       "pc_guest_profile_read_selected(&gp, perr, sizeof(perr))" in lab and "load_or_create" not in lab and "fopen" not in lab and "write" not in lab)
    ck("C a missing profile shows a placeholder 'Join as Guest (new profile)', an existing one 'Join as Guest (%s)' with the name, a broken one logs and keeps 'Join as Guest'",
       "PC_GUEST_PROFILE_ABSENT" in lab and '"Join as Guest (new profile)"' in lab and '"Join as Guest (%s)"' in lab and 'static char s_pc_guest_title_label[48] = "Join as Guest";' in cm)
    ck("C only the JOIN creates: pc_guest_title_join uses pc_guest_profile_load_or_create_selected; it is the only load_or_create call in pc_m_card.c",
       "pc_guest_profile_load_or_create_selected(&gp, perr, sizeof(perr))" in join and len(re.findall(r"load_or_create", cm)) == 1)
    ck("C neither pc_m_card.c nor pc_main.c hard codes PC_GUEST_PROFILE_PATH any more (the SELECTED profile path is the one source)", "PC_GUEST_PROFILE_PATH" not in cm and "PC_GUEST_PROFILE_PATH" not in mm)
    ck("C --guest in pc_main.c loads / creates the SELECTED profile (pc_guest_profile_load_or_create_selected) and logs the selected path",
       "pc_guest_profile_load_or_create_selected(&gp, gerr, sizeof(gerr))" in mm and "pc_guest_profile_selected_path()" in mm)
    sel_calls = {}
    for fn in sorted(os.listdir(os.path.join(S.ROOT, "pc", "src"))):
        if fn.endswith((".c", ".cpp")) and fn != "pc_guest_profile.c":
            n = len(re.findall(r"\bpc_guest_profile_select\(", S.mask(S.read("pc/src/" + fn))))
            if n:
                sel_calls[fn] = n
    prep = S.body(mm, S.functions(mm), "pc_main_prepare_store_character")
    po_sel = S.body(mm, S.functions(mm), "pc_po_select_profile")
    ck("C ONE source of truth: the selection is set by exactly THREE call sites, all in pc_main.c: (1) the --guest-profile option parsing, (2) pc_main_prepare_store_character selecting the LEGACY profile that a "
       "--character names / that a store-less legacy character resolves to, (3) Play Online without relaunch: the one wrapper pc_po_select_profile (the in-process connect's select / clear / rollback "
       "restore; every other in-process use goes through it) (calls: %s)" % sel_calls,
       sel_calls == {"pc_main.c": 3} and len(re.findall(r"\bpc_guest_profile_select\(", mm)) == 3 and len(re.findall(r"\bpc_guest_profile_select\(", prep)) == 1
       and "return pc_guest_profile_select(name);" in po_sel and len(re.findall(r"\bpc_po_select_profile\(", mm)) == 5
       and "(void)pc_guest_profile_select(c.legacy_profile);" in prep and "c.storage == PC_CHARACTER_STORAGE_LEGACY" in prep and "(void)pc_guest_profile_select(argv[i + 1]);" in mm)
    ck("C the title actor still never touches the profile module (only pc_guest_title_* in pc_m_card.c)", "pc_guest_profile" not in logo and "pc_guest_title_label()" in logo)

    # ---------------------------------------------------------------- D: flags
    i_opt = mm.index('strcmp(argv[i], "--guest-profile") == 0')
    opt = mm[i_opt:mm.index('} else if (strcmp(argv[i], "--character") == 0)', i_opt)]
    ck("D --guest-profile is parsed next to --guest: it consumes the value, implies g_pc_guest = 1, validates with pc_guest_profile_name_check and selects with pc_guest_profile_select",
       "g_pc_guest = 1;" in opt and "i++;" in opt and "pc_guest_profile_name_check(argv[i + 1], perr, sizeof(perr))" in opt and "pc_guest_profile_select(argv[i + 1])" in opt)
    ck("D a missing / empty value, an invalid name and a repeated option each `return 2` with a `--guest-profile: REFUSED` diagnostic and a usage line",
       opt.count("return 2;") == 3 and opt.count("[PC] --guest-profile: REFUSED") == 3 and opt.count("usage: AnimalCrossing --connect HOST[:PORT] --guest-profile NAME") == 3
       and "needs a profile NAME" in opt and "given more than once" in opt)
    start = mm.index("if (g_pc_guest) {")
    blk = main_raw[start:main_raw.index("/* Guests G2: --bootstrap-guest is a CLIENT-only TEST hook", start)]
    ck("D --guest-profile shares the --guest exclusivity block unchanged: refuses --host / --dedicated / --host-observer / --bootstrap-resident / --bootstrap-guest and a missing --connect, "
       "exit 2 + the usage line (which now mentions [--guest-profile NAME]); since M2 the block also holds the 4th `return 2` of a refused store character (pc_main_prepare_store_character < 0, "
       "its message is printed inside pc_main_prepare_store_character, so 3 messages stay in the block), and the block is ALSO the one --character goes through (it sets g_pc_guest = 1)",
       all('strcmp(argv[a], "%s") == 0' % o in blk for o in ("--host", "--dedicated", "--host-observer", "--bootstrap-resident", "--bootstrap-guest")) and "g_pc_net_role != 2" in blk
       and "const int store_rc = pc_main_prepare_store_character();" in blk and "if (store_rc < 0) {" in blk and blk.index("g_pc_net_role != 2") < blk.index("pc_main_prepare_store_character()")
       and blk.count("return 2;") == 4 and blk.count("[PC] --guest: REFUSED") == 3 and "[PC] --guest: REFUSED: bad character" in mm and "usage: AnimalCrossing --connect HOST[:PORT] --guest [--guest-profile NAME]" in blk)
    ck("D the exclusivity block runs before the profile file is touched (the load is after the conflict / role refusals) and before --bootstrap-guest validation",
       blk.index("conflict != NULL") < blk.index("g_pc_net_role != 2") < blk.index("pc_guest_profile_load_or_create_selected") and mm.index("if (g_pc_guest) {") < mm.index("pc_bootstrap_guest_validate(g_pc_bootstrap_guest)"))
    ck("D --help documents --guest-profile (rule, files, default unchanged) and the header comment of the option exists",
       '"  --guest-profile NAME  CLIENT-only' in main_raw and "save/mp/guest_<name>.ini" in main_raw and "save/mp/guest_token_<name>.dat" in main_raw
       and "save/mp/guest.ini and save/mp/guest_token.dat" in main_raw and "ONE source of truth" in main_raw)

    # ---------------------------------------------------------------- E: the module
    chk = S.body(pcm, pf, "pc_guest_profile_name_check")
    ck("E the NAME rules: empty, > 16, charset [A-Za-z0-9-] only (no separators / dots / spaces), no leading '-', no Windows device names (CON PRN AUX NUL COM0..9 LPT0..9), each with its own message",
       all(x in chk for x in ("is empty", "is too long", "outside A-Z a-z 0-9 -", "must not start with '-'", "reserved Windows device name")) and "(c >= 'A' && c <= 'Z')" in chk
       and "'.'" not in chk and "'/'" not in chk and '"con", "prn", "aux", "nul"' in S.body(pcm, pf, "is_device_name") and "PC_GUEST_PROFILE_ARG_MAX" in ph)
    rd = S.body(pcm, pf, "pc_guest_profile_read")
    sib = S.body(pcm, pf, "scan_siblings") + S.body(pcm, pf, "sibling_take") + S.body(pcm, pf, "sibling_file_name")
    ck("E the reader and the sibling scan are READ ONLY: no write / create / remove / rename / mkdir in pc_guest_profile_read, scan_siblings, sibling_take",
       rd != "" and sib != "" and not re.search(r'"w|"a|"r\+|remove\(|rename|write_atomic|make_dir|_mkdir|MoveFile|DeleteFile|CreateFile', rd + sib))
    ck("E an unreadable sibling is skipped with a warning (fprintf stderr), never an error and never touched", "sibling profile skipped" in sib and "fprintf(stderr" in sib)
    ck("E the only file write is still the atomic writer: exactly one fopen(wb), write_atomic reached only from the default creator and the named creator, never from a reader",
       len(re.findall(r'fopen\([^)]*"wb"\)', pcm)) == 1 and pcm.count("write_atomic(") == 3 and "write_atomic(" not in rd + sib
       and "write_atomic(" in S.body(pcm, pf, "pc_guest_profile_load_or_create_in") and "write_atomic(" in S.body(pcm, pf, "pc_guest_profile_load_or_create"))
    cr = S.body(pcm, pf, "pc_guest_profile_load_or_create_in")
    ck("E the named creator never overwrites: it READS first and returns the loaded result / the error for anything but ABSENT BEFORE the sibling scan and the single write",
       cr.index("pc_guest_profile_read(path, &p, err, errcap)") < cr.index("if (r != PC_GUEST_PROFILE_ABSENT)") < cr.index("scan_siblings(") < cr.index("pc_guest_profile_make_unique(") < cr.index("write_atomic(")
       and "remove(" not in cr)
    mu = S.body(pcm, pf, "pc_guest_profile_make_unique")
    ck("E uniqueness: a bounded number of draws (32) over the CSPRNG ids; a draw is rejected when the display name (any case) OR the full identity equals a sibling's; a name collision first falls "
       "back to Guest + 2 hex digits of the id; the result must validate",
       "#define PROFILE_ID_DRAWS 32" in pcm and "attempt < PROFILE_ID_DRAWS" in mu and "random_id(&p.player_id) || !random_id(&p.land_id)" in mu and "sibling_conflict(&p, sib, nsib)" in mu
       and '"Guest%02X"' in mu and "pc_guest_profile_validate(&p, NULL, NULL)" in mu and "streq_ci(c->name, sib[k].name)" in S.body(pcm, pf, "sibling_conflict")
       and "c->player_id == sib[k].player_id && c->land_id == sib[k].land_id" in S.body(pcm, pf, "sibling_conflict"))
    dn = S.body(pcm, pf, "pc_guest_profile_default_name")
    ck("E default display name: the profile name cut to 8 characters; falls back to Guest + 2 hex digits when invalid, SERVER (any case) or 'Guest' (the default profile's own name)",
       "n > PC_GUEST_PROFILE_NAME_LEN" in dn and 'streq_ci(base, "SERVER")' in dn and "streq_ci(base, PC_GUEST_PROFILE_DEFAULT_NAME)" in dn and '"Guest%02X"' in dn
       and "pc_guest_profile_validate(&t, NULL, NULL)" in dn)
    ck("E the id source is the OS CSPRNG (pc_mp_guests_random_bytes) unless the TEST SEAM is set; the seam is NULL by default and set nowhere in production code",
       "static PCGuestProfileIdSource s_id_source = NULL;" in pcm and "pc_mp_guests_random_bytes(b, sizeof(b))" in pcm
       and not [f for f in os.listdir(os.path.join(S.ROOT, "pc", "src")) if f.endswith((".c", ".cpp")) and "pc_guest_profile_test_set_id_source" in S.mask(S.read("pc/src/" + f)) and f != "pc_guest_profile.c"]
       and not re.search(r"\b(rand|srand|time|clock)\s*\(", pcm))
    ck("E the module stays pure C: only pc_guest_profile.h / pc_mp_guests.h are included from the project, the header includes none",
       re.findall(r'#include "([^"]+)"', pc_raw) == ["pc_guest_profile.h", "pc_mp_guests.h"] and re.findall(r'#include "([^"]+)"', ph) == [])
    ck("E the header documents the profile feature: names / files / folding / uniqueness / unreadable siblings skipped",
       "named guest profiles (--guest-profile NAME)" in ph and "guest_token_<name>.dat" in ph and "folded to lower case" in ph and "re-drawn (bounded)" in ph and "unreadable ones are skipped" in ph)

    # ---------------------------------------------------------------- G: first-run guest creation (the Rover scene creates a NEW named profile)
    g2raw = S.read("src/actor/npc/ac_npc_guide2_move.c_inc")
    g2n = g2raw.replace(chr(13), "")
    fin = S.body(cm, cf, "pc_guest_creation_finish")
    arr = S.body(cm, cf, "pc_guest_arrive")
    join = S.body(cm, cf, "pc_guest_title_join")
    ck("G pc_main.c: a NAMED profile is READ first; only ABSENT draws an in-memory identity (prepare_new_selected) and arms the creation, nothing is written there",
       "pc_guest_profile_read_selected(&gp, gerr, sizeof(gerr))" in mm and "pc_guest_profile_prepare_new_selected(&gp, gerr, sizeof(gerr))" in mm and "pc_guest_creation_arm(&gp)" in mm
       and mm.index("pc_guest_profile_read_selected(&gp, gerr") < mm.index("pc_guest_profile_prepare_new_selected") < mm.index("pc_guest_profile_load_or_create_selected(&gp, gerr"))
    ck("G the title join does the same for a named profile (read, ABSENT -> prepare_new + arm, disarm on a failed arrival); the label still only reads",
       "pc_guest_profile_read_selected(&gp, perr, sizeof(perr))" in join and "pc_guest_profile_prepare_new_selected" in join and "pc_guest_creation_arm(&gp)" in join and "s_pc_guest_create_armed = 0" in join)
    ck("G pc_guest_arrive CREATE mode: the door is SCENE_START_DEMO2 north (120, 340) with NO RIDE_OFF_DEMO, the resident-name check is skipped; the station branch is unchanged",
       "create = s_pc_guest_create_armed" in arr and "SCENE_START_DEMO2" in arr and "exit_position.x = 120" in arr and "exit_position.z = 340" in arr
       and arr.count("mAc_PROFILE_RIDE_OFF_DEMO") == 1 and "pc_guest_station_door(&door_data)" in arr and "create ? -1 : pc_guest_resident_name_conflict" in arr)
    ck("G the finish validates, writes the profile CREATE-ONLY (pc_guest_profile_create_exclusive), applies the identity-hash shirt and goes to the station with RIDE_OFF_DEMO; "
       "it never runs the vanilla new-resident events, never rebuilds the record and exits 2 on failure",
       "pc_guest_profile_name_from_game(" in fin and "pc_guest_profile_create_exclusive(" in fin and "pc_guest_starter_shirt(" in fin and "mAc_PROFILE_RIDE_OFF_DEMO" in fin
       and "mEv_" not in fin and "pc_guest_build_fresh_record" not in fin and "mPr_SetNowPrivateCloth" not in fin and "pc_save_" not in fin and "pc_guest_creation_die" in fin
       and "exit(2)" in S.body(cm, cf, "pc_guest_creation_die"))
    ck("G the create-only write: MoveFileExA WITHOUT MOVEFILE_REPLACE_EXISTING (link() elsewhere), in its own helper beside the replacing one",
       "static int rename_exclusive(" in pcm and "MoveFileExA(src, dst, MOVEFILE_WRITE_THROUGH)" in pcm and "REPLACE_EXISTING" not in S.body(pcm, pf, "rename_exclusive"))
    ck("G the Rover actor (TARGET_PC): scene_change_wait_init calls the finish ONLY while a creation is active (face + BGM kept, mEv_ calls skipped); check_pname also rejects names the profile cannot store; "
       "getP_other_pl_name returns before its out-of-bounds second loop for player_no >= PLAYER_NUM",
       "if (pc_guest_creation_active()) {" in g2n and "pc_guest_creation_finish(play);" in g2n and "pc_guest_creation_name_ok(" in g2n
       and "if (player_no >= PLAYER_NUM) {" in g2n and "aNG2_set_pl_face_type(guide2);\n        pc_guest_creation_finish(play);" in g2n)
    ck("G the client's identity claim waits for the creation to finish (pc_net_game.c) and no wire / protocol constant changed",
       "pc_guest_creation_active()" in ng and "the identity claim waits for the Rover scene" in ng_raw)

    # ---------------------------------------------------------------- F: no wire change
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), S.ROOT)
    ck("F no protocol bump / no wire change: PC_NETGAME_PROTOCOL_VERSION 8u and the wire baseline (%d checks) is green" % len(wb),
       "#define PC_NETGAME_PROTOCOL_VERSION 8u" in S.read("pc/include/pc_net_game.h") and bool(wb) and all(c for _d, c in wb))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
