#!/usr/bin/env python3
"""test_guest_g1_src.py - GUESTS G1 (fresh guest record) SOURCE AUDIT (no game process).

A new guest is its own independent character, never a copy of a resident. This audit pins the source rules that guarantee it:
  A  pc_bootstrap_guest_poll + (G3) the shared pc_guest_arrive it calls (pc_m_card.c): NO resident clone (no private_data[...] read, no mPr_CopyPrivateInfo / template), the fresh record comes from
     pc_guest_build_fresh_record, the NAME checks (valid game name, not a resident's name) run BEFORE anything is bound and refuse with exit code 2; the arrival
     binding / station spawn are as before; since G3 the init is mSDI_StartDataInitGuest (no gateway) instead of mSDI_StartDataInit(MODE_PAK); never mSDI NEW / NEW_PLAYER, no pc_save_ready, no homes[] / house calls
  B  pc_guest_build_fresh_record: mPr_ClearPrivateInfo THEN mPr_InitPrivateInfo, then the HOME PersonalID, exists, reset_code, gender / face / explicit starter
     shirt (mPlib_change_player_cloth_info_lv2, NOT the random mPr_SetNowPrivateCloth); DETERMINISTIC (no RANDOM / rand / fqrand / time in the guest creation
     code; the identity hash is FNV-1a32 over the canonical 20-byte PersonalID), writes only through `rec`
  C  pc_guest_resident_name_conflict: a BOUNDED loop over private_data[0..3] (PLAYER_NUM), read-only, not the OOB-prone aNG2_getP_other_pl_name
  D  the arguments: NAME,LAND,PLAYER_ID,LAND_ID[,GENDER[,FACE]] (optional trailing fields, GENDER 0|1, FACE 0..7 validated), --help text + comments additive
  E  HOST authority (pc_net_game.c pcnetgame_host_guest_check): valid name, resident-name refusal (all four slots, away residents too), other-guest name refusal
     for a NEW key only, all through the existing refuse path (SERVER_FULL, a clear log line, no wire change); the revalidation re-checks the resident name;
     the host's first-contact BLANK record is unchanged (zeros + player_ID + exists)
  F  the shared name rule pc_mp_guests_name_valid and the python mirror (net_spike_lib: derivation constants == the C ones)
  H  (Guests G1.1) invalid --bootstrap-guest arguments: the early validator (called from pc_main.c before any init), every failure path exits 2 with a stderr
     diagnostic (no silent return), the shared exact reserved-name helper, the host check order (mode 1 skips the resident-name / reserved rules, key conflict for all)
  G  untouched: no private_data / homes write by any guest-creation function; EOL of every edited file; wire_baseline green
Tier: SOURCE AUDITED. Exit code 0 when all checks pass."""
import os
import re
import sys

import net_spike_lib as L
import test_guest_src as S
import wire_baseline

ROOT = S.ROOT


def PC_RES_OK(gh_text):
    """The header literal is 'SERVER' + two spaces (8 bytes), equal to the observer's 6 letters + 2 padding spaces."""
    import re as _re
    m = _re.search(r'#define PC_MP_GUEST_RESERVED_NAME "([^"]*)"', gh_text)
    return m is not None and m.group(1) == "SERVER" + " " * 2 and len(m.group(1)) == 8


def raw_bytes(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
        return f.read()


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    card = S.read("pc/src/pc_m_card.c")
    cm = S.mask(card)
    cf = S.functions(cm)
    cb = lambda n: S.body(cm, cf, n)
    ng_raw = S.read("pc/src/pc_net_game.c")
    ng = S.mask(ng_raw)
    nf = S.functions(ng)
    nb = lambda n: S.body(ng, nf, n)
    main_raw = S.read("pc/src/pc_main.c")
    gh = S.read("pc/include/pc_mp_guests.h")
    gc = S.read("pc/src/pc_mp_guests.c")
    lib = S.read("pc/tools/net_spike/net_spike_lib.py")

    # ---------------------------------------------------------------- A
    # G3: the arrival moved from pc_bootstrap_guest_poll into the shared pc_guest_arrive (the poll keeps the one-shot / readiness waits + exit(2)); `poll` below
    # means "the arrival code" = the poll AND the shared function, so every G1 rule is still pinned on the code that now carries it.
    poll_only = cb("pc_bootstrap_guest_poll")
    arr = cb("pc_guest_arrive")
    poll = poll_only + "\n" + arr
    ck("A pc_bootstrap_guest_poll and (G3) the shared pc_guest_arrive exist", poll_only != "" and arr != "")
    ck("A NO resident clone: the guest poll contains no private_data[...] access, no template pointer and no mPr_CopyPrivateInfo / mPr_CopyPrivate*",
       "private_data" not in poll and "tmpl" not in poll and "template" not in poll.lower() and "mPr_CopyPrivateInfo" not in poll and "bcopy" not in poll)
    ck("A the passport is built by pc_guest_build_fresh_record() into l_mcd_foreigner_file.file.priv (zeroed first), the optional GENDER / FACE arguments are passed through",
       "memset(&l_mcd_foreigner_file, 0, sizeof(l_mcd_foreigner_file));" in poll and "pass = &l_mcd_foreigner_file.file.priv;" in poll
       and "pc_guest_build_fresh_record(pass, &home, opt_gender, opt_face);" in poll
       and "pc_guest_spec_check(spec, &home, &opt_gender, &opt_face, &bad_why)" in poll)
    i_val, i_res, i_bld, i_bind = (arr.find(x) for x in ("pc_guest_spec_check(spec", "pc_guest_resident_name_conflict(&home)", "pc_guest_build_fresh_record(pass",
                                                         "Common_Set(now_private, pass);"))
    ck("A order (G1.1: the spec check = syntax + valid name + reserved name + valid identity lives in pc_guest_spec_check): spec check, resident-name check, THEN the fresh record, "
       "THEN the binding (nothing is bound when a name check fails)", 0 < i_val < i_res < i_bld < i_bind)
    sc = cb("pc_guest_spec_check")
    ck("A pc_guest_spec_check: parse, then the host's identity-validity tests, the SHARED valid-name rule and the SHARED reserved-name rule (in that order)",
       sc != "" and "pc_guest_parse_spec(spec, home, gender_out, face_out, why)" in sc and "pc_mp_guests_name_valid(home->player_name)" in sc and "pc_mp_guests_name_reserved(home->player_name)" in sc
       and sc.index("pc_guest_parse_spec(") < sc.index("home->player_name[0] == 0 || home->land_name[0] == 0 || home->player_id == 0xFFFFu || home->land_id == 0xFFFFu")
       < sc.index("pc_mp_guests_name_valid(") < sc.index("pc_mp_guests_name_reserved(") and "is not a valid game player name" in card and "is reserved for the server observer" in card)
    ck("A the resident-name refusal FAILS the arrival (G3: `return 0` with the reason in err; the CLI poll turns every failure into exit code 2 + stderr, the title menu into a message) before anything is bound",
       "equals the name of resident %d of this town" in card and arr.index("return 0;", i_res) < i_bld and "exit(" not in arr and poll_only.count("exit(2);") == 1
       and 'fprintf(stderr, "[PC] --bootstrap-guest: %s\\n", err);' in poll_only)
    ck("A the arrival binding is unchanged: Common_Set(player_no, mPr_FOREIGNER), station spawn (1979, 760) with RIDE_OFF_DEMO; (G3.1) the init is mSDI_StartDataInitGuest(gamePT) -- NOT "
       "mSDI_StartDataInit(.., MODE_PAK) any more (that one sets the gateway); a failed init restores the previous binding",
       "Common_Set(player_no, mPr_FOREIGNER);" in arr and "mSDI_StartDataInitGuest(gamePT)" in arr and "mSDI_StartDataInit(" not in arr and "MODE_PAK" not in arr and "1979" in arr and "760" in arr
       and "mAc_PROFILE_RIDE_OFF_DEMO" in arr and "Common_Set(time.rtc_enabled, TRUE);" in arr and "Common_Set(now_private, prev_private);" in arr)
    ck("A never the vanilla new-player / new-town init, never a save writer, no house / home call in the guest poll",
       not re.search(r"mSDI_INIT_MODE_NEW|NEW_PLAYER|mSDI_StartInitNew|mSDI_StartDataInitObserver", poll) and "pc_save_ready" not in poll and "pc_save_write" not in poll
       and not re.search(r"homes|mHS_|mHm_|mNW_InitOneMyOriginal|mPr_LoadPak", poll) and set(re.findall(r"\bmEv_\w+\(", poll)) <= {"mEv_CheckGateway("})  # G3: the one read-only gateway probe for the log line
    ck("A the guest log markers the real-client test greps are intact (additive: a FRESH-record line before the arrival line)",
       "[PC] %s: FRESH guest record (not a copy of any resident): gender=%d face=%d shirt=0x%04X (%s), empty pockets / wallet / letters" in card and 'pc_guest_arrive("--bootstrap-guest"' in poll_only
       and "bound as a foreigner, arriving at the station (SCENE_FG)" in card and "guest '%.8s' (home land id 0x%04X, player id 0x%04X)" in card)

    # ---------------------------------------------------------------- B
    bld = cb("pc_guest_build_fresh_record")
    ck("B pc_guest_build_fresh_record exists and calls mPr_ClearPrivateInfo(rec) THEN mPr_InitPrivateInfo(rec) THEN mPr_CopyPersonalID (the HOME PersonalID overwrites InitPrivateInfo's town land / random id)",
       bld != "" and 0 < bld.find("mPr_ClearPrivateInfo(rec);") < bld.find("mPr_InitPrivateInfo(rec);") < bld.find("mPr_CopyPersonalID(&rec->player_ID, (PersonalID_c*)home);"))
    ck("B ... then exists = TRUE, reset_code = 0, gender / face assigned, the starter shirt via the explicit setter mPlib_change_player_cloth_info_lv2 with a deterministic item, default designs",
       "rec->exists = TRUE;" in bld and "rec->reset_code = 0;" in bld and "rec->gender = (s8)gender;" in bld and "rec->face = (s8)face;" in bld
       and "mPlib_change_player_cloth_info_lv2(rec, (mActor_name_t)(ITM_CLOTH000 + (gender == mPr_SEX_FEMALE ? 8 : 0) + shirt_idx));" in bld and "pc_guest_init_designs(rec);" in bld
       and bld.find("mPr_CopyPersonalID") < bld.find("rec->exists = TRUE;") < bld.find("mPlib_change_player_cloth_info_lv2") < bld.find("pc_guest_init_designs(rec);"))
    ck("B gender / face / shirt come from the identity HASH when not given (bit 31 / bits 16..18 / bits 8..10), never from a random call",
       "gender = (int)((h >> 31) & 1u);" in bld and "face = (int)((h >> 16) & 7u);" in bld and "int shirt_idx = (int)((h >> 8) & 7u);" in bld
       and "const u32 h = pc_guest_identity_hash(home);" in bld)
    guest_fns = ["pc_guest_parse_spec", "pc_guest_identity_hash", "pc_guest_init_designs", "pc_guest_build_fresh_record", "pc_guest_resident_name_conflict", "pc_guest_arrive", "pc_bootstrap_guest_poll"]
    allg = "\n".join(cb(n) for n in guest_fns)
    ck("B DETERMINISTIC: no RANDOM / rand / fqrand / srand / time / mPr_SetNowPrivateCloth / mPr_GetRandom / RANDOM_F anywhere in the guest-creation code of pc_m_card.c "
       "(mPr_InitPrivateInfo itself draws the client RNG internally; every value it sets is overwritten, see the builder comment)",
       all(cb(n) != "" for n in guest_fns) and not re.search(r"\bRANDOM\w*\(|\brand\(|\bsrand\(|\bfqrand\(|\btime\(|\bclock\(|mPr_SetNowPrivateCloth|mPr_GetRandom|SDL_GetPerformance", allg))
    hh = cb("pc_guest_identity_hash")
    ck("B the identity hash is FNV-1a32 (offset 2166136261, prime 16777619) over name[8], land[8], player_id (high, low), land_id (high, low)",
       "u32 h = 2166136261u;" in hh and hh.count("* 16777619u;") == 6 and "id->player_name[k]" in hh and "id->land_name[k]" in hh and "(id->player_id >> 8) & 0xFFu" in hh
       and "(id->land_id >> 8) & 0xFFu" in hh and "(id->land_id & 0xFFu)" in hh)
    ck("B the fresh-record builder and the design init write ONLY through `rec` (no Save_Get / Save_Set / Common_Set / now_private / homes / private_data)",
       not re.search(r"Save_Get|Save_Set|Save_GetPointer|Common_Set|now_private|homes|private_data", bld + cb("pc_guest_init_designs")))
    di = cb("pc_guest_init_designs")
    ck("B default designs mirror mNW_InitOneMyOriginal into `rec`: palette table {0,8,7,7,0,0,0,0}, ROM names 0x6DF + i and ARAM textures for the first mNW_DEFAULT_ORIGINAL_TEX_NUM, 'blank' for the rest",
       "{ 0, 8, 7, 7, 0, 0, 0, 0 }" in di and "mString_Load_StringFromRom(d->name, mNW_ORIGINAL_DESIGN_NAME_LEN, 0x6DF + i);" in di and "JW_GetAramAddress(27) + i * mNW_DESIGN_TEX_SIZE" in di
       and "mNW_InitOriginalData(d);" in di and "i < mNW_DEFAULT_ORIGINAL_TEX_NUM" in di)
    ck("B compile-time guards: gender / face enum assumptions and the contiguous starter shirt tables",
       "mPr_SEX_MALE == 0 && mPr_SEX_FEMALE == 1 && mPr_FACE_TYPE_NUM == 8" in card and "ITM_CLOTH008 == ITM_CLOTH000 + 8 && ITM_CLOTH015 == ITM_CLOTH000 + 15" in card)
    vanilla = S.read("src/game/m_private.c")
    ck("B (vanilla facts the builder relies on, read from the decomp source) mPr_ClearPrivateInfo clears quests / mail / birthday / maps / museum and sets state_flags = 1; mPr_InitPrivateInfo sets exists / loan 100 / "
       "my_org_no_table and is NOT given the guest's identity; mPr_SetNowPrivateCloth is the RANDOM-based setter the guest code must not use; the explicit setter takes the item",
       all(x in vanilla for x in ("mQst_ClearDelivery(private_info->deliveries", "mMl_clear_mail_box(private_info->mail", "mPr_ClearPrivateBirthday(&private_info->birthday);",
                                  "mPr_ClearMapInfo(private_info->maps", "mMsm_ClearRecord(&private_info->museum_record);", "private_info->state_flags = 1;",
                                  "priv->exists = TRUE;", "priv->inventory.loan = 100;", "priv->my_org_no_table[i] = i;", "mPlib_change_player_cloth_info_lv2(priv, mPr_GetRandomCloth(priv->gender));"))
       and "mPlib_change_player_cloth_info_lv2(Private_c* priv, mActor_name_t item)" in S.read("src/game/m_player_lib.c"))

    # ---------------------------------------------------------------- C
    rc = cb("pc_guest_resident_name_conflict")
    ck("C the client's resident-name check is a BOUNDED, read-only loop over private_data[0..PLAYER_NUM) comparing the 8 name bytes of every PersonalID-holding slot (an away resident counts too)",
       rc != "" and "for (i = 0; i < PLAYER_NUM; i++)" in rc and "&Save_Get(private_data)[i].player_ID" in rc and "mPr_NullCheckPersonalID(p) == FALSE" in rc
       and "memcmp(p->player_name, home->player_name, PLAYER_NAME_LEN) == 0" in rc and not re.search(r"[^=!<>]=[^=]", rc.replace("int i;", "").replace("i = 0", "").replace("PersonalID_c* p =", "")))
    ck("C it does NOT use the vanilla aNG2_getP_other_pl_name (its second loop starts at i = -1 with no resident) nor mPr_CheckCmpPlayerName",
       "aNG2_getP_other_pl_name" not in cm and "mPr_CheckCmpPlayerName" not in rc)

    # ---------------------------------------------------------------- D
    ps = cb("pc_guest_parse_spec")
    ck("D the spec parser accepts 4..6 comma separated fields: NAME,LAND,PLAYER_ID,LAND_ID[,GENDER[,FACE]]; the first four are parsed exactly as before (space padded names, ids 1..0xFFFE)",
       "char* tok[6];" in ps and "if (n >= 6) {" in ps and "if (n < 4) {" in ps and "memset(out->player_name, ' ', PLAYER_NAME_LEN);" in ps and "memset(out->land_name, ' ', LAND_NAME_SIZE);" in ps
       and "pid == 0 || pid >= 0xFFFFul" in ps and "lid == 0 || lid >= 0xFFFFul" in ps and "len < 1" in ps and "len > PLAYER_NAME_LEN" in ps and "len > LAND_NAME_SIZE" in ps)
    ck("D GENDER is validated 0|1 (mPr_SEX_FEMALE max) and FACE 0..mPr_FACE_TYPE_NUM-1; both default to -1 = derive from the identity",
       "*gender_out = -1;" in ps and "*face_out = -1;" in ps and "gv > (unsigned long)mPr_SEX_FEMALE" in ps and "fv >= (unsigned long)mPr_FACE_TYPE_NUM" in ps
       and "*gender_out = (int)gv;" in ps and "*face_out = (int)fv;" in ps)
    ck("D the usage text (pc_main.c --help) documents the optional GENDER,FACE and the exit-code-2 name rule, and still starts with the old '--bootstrap-guest NAME,LAND,PLAYER_ID,LAND_ID' form (additive)",
       "--bootstrap-guest NAME,LAND,PLAYER_ID,LAND_ID[,GENDER[,FACE]]" in main_raw and "GENDER (0 male, 1 female) and FACE (0..7)" in main_raw
       and "must not equal a resident's name (otherwise exit code 2)" in main_raw and "FRESH" in main_raw and "template" not in main_raw.split("--bootstrap-guest NAME,LAND")[1][:900])
    ck("D the bad-spec message names the new form (G1.1: plus WHICH part is wrong)", "(expected NAME,LAND,PLAYER_ID,LAND_ID[,GENDER[,FACE]])" in card and "bad spec '%s': %s" in card
       and "GENDER (must be 0 = male or 1 = female, or empty)" in ps and "FACE (must be 0..7, or empty)" in ps)
    ck("D no new CLI flag: pc_main.c has exactly the one --bootstrap-guest value parse site (G1.1 adds a 'missing value' refusal branch) and the guest hook is still refused unless --connect",
       main_raw.count('strcmp(argv[i], "--bootstrap-guest") == 0 && i + 1 < argc') == 1 and main_raw.count('strcmp(argv[i], "--bootstrap-guest") == 0') == 2
       and re.search(r"g_pc_bootstrap_guest != NULL && \(g_pc_net_role != 2 \|\| g_pc_bootstrap_resident >= 0\)", S.mask(main_raw)))

    # ---------------------------------------------------------------- E
    gcheck = nb("pcnetgame_host_guest_check")
    order = ["pcnetgame_guest_key_valid(key)", "pc_mp_guests_name_valid(key->player_name)", "ext->home_land_id == s_host_town.land_id", "pcnetgame_guest_key_conflict(key)",
             "pcnetgame_guest_store_load();", "s_guest_untrusted", "pcnetgame_guest_find(key)", "if (tok_ok) {", "pcnetgame_guest_entry_disposable(g)",
             "pcnetgame_guest_name_conflict_guest(key)", "presented a guest token for a key this host has no entry for"]
    ck("E host guest_check order (G1.1): key valid -> NAME valid -> home land -> resident / house KEY collision (all modes) -> guests.dat -> lookup -> token decision (mode 1) -> "
       "re-mint (mode 2) -> new key: OTHER-GUEST-NAME -> first contact; the name rules are NOT before the token decision",
       all(o in gcheck for o in order) and [gcheck.index(o) for o in order] == sorted(gcheck.index(o) for o in order)
       and gcheck.index("pcnetgame_guest_name_conflict_resident(key)") > gcheck.index("if (tok_ok) {")
       and gcheck.index("pc_mp_guests_name_reserved(key->player_name)") > gcheck.index("if (tok_ok) {"))
    ck("E every name refusal goes through the existing refuse path (pcnetgame_host_refuse_identity -> SERVER_FULL, a log line, no new reject reason / message)",
       gcheck.count("pcnetgame_host_refuse_identity(peer, why, 0);") >= 3 and "guest claim: the guest name is not a valid game player name" in gcheck
       and gcheck.count("guest claim: the guest name is RESERVED for the server observer") == 2
       and "PC_NETGAME_REJECT" not in gcheck and "PC_NETGAME_MSG_" not in gcheck
       and "the guest name equals the name of a resident of this town" in ng_raw and "the guest name is already used by another guest of this town" in ng_raw)
    gi = gcheck.index("pcnetgame_guest_find(key)")
    ck("E the OTHER-GUEST name rule runs only on the new-key path (after the known-key branch returned), so a guest admitted before the rule is never locked out",
       gcheck.index("pcnetgame_guest_name_conflict_guest(key)") > gi and "return 0;\n    }\n    why = pcnetgame_guest_name_conflict_guest(key);" in gcheck)
    nr = nb("pcnetgame_guest_name_conflict_resident")
    ck("E host resident-name check: bounded loop over private_data[0..PLAYER_NUM) (every PersonalID-holding slot, away residents included), 8-byte memcmp of the names, read-only",
       "for (i = 0; i < PLAYER_NUM; i++)" in nr and "&Save_Get(private_data)[i].player_ID" in nr and "mPr_NullCheckPersonalID(p) == FALSE" in nr
       and "memcmp(p->player_name, key->player_name, PLAYER_NAME_LEN) == 0" in nr and "aNG2" not in nr and "mPr_CheckCmpPlayerName" not in nr)
    gg = nb("pcnetgame_guest_name_conflict_guest")
    ck("E host guest-name check: only entries of THIS town (inactive other-town entries ignored), only OTHER keys (a different full PersonalID), 8-byte name compare",
       "s_guest[g].used && pcnetgame_town_equal(&s_guest[g].town, &s_host_town)" in gg and "memcmp(&s_guest[g].key, key, sizeof(*key)) != 0" in gg
       and "memcmp(s_guest[g].key.player_name, key->player_name, PLAYER_NAME_LEN) == 0" in gg and "g < PC_NETGAME_GUEST_MAX" in gg)
    rv = nb("pcnetgame_host_revalidate_bound_peers")
    ck("E (G1.1) the world-ready revalidation still closes a bound guest whose KEY became a resident / house-owner identity, but NO LONGER one whose NAME a resident now carries "
       "(an authenticated returning guest is never closed for it)", "pcnetgame_guest_key_conflict(&st->bound_pid)" in rv and "pcnetgame_guest_name_conflict_resident" not in rv
       and "pc_mp_guests_name_reserved" not in rv)
    cr = nb("pcnetgame_guest_create")
    ck("E the host's first-contact BLANK record is unchanged: zeros, player_ID = key, exists = TRUE (the client's MIGRATE then merges the fresh record into it; the host-owned ranges "
       "museum_record / reset_code are zero in the fresh record too, so nothing is lost or invented)",
       "memset(&s_guest_rec[g], 0, sizeof(s_guest_rec[g]));" in cr and "mPr_CopyPersonalID(&s_guest_rec[g].player_ID, (PersonalID_c*)key);" in cr and "s_guest_rec[g].exists = TRUE;" in cr
       and "mMsm_ClearRecord(&private_info->museum_record);" in vanilla and "bzero(private_info, sizeof(Private_c));" in vanilla
       and "private_info->reset_code" not in vanilla.split("extern void mPr_ClearPrivateInfo")[1].split("static int mPr_GetRandomFace")[0])
    ck("E no wire change: the message id range is exactly 1..66 (66 = the M-F promotion handoff; 59..61 came with the furniture sync, 62..65 with the town transfer) and wire_baseline is green", sorted(dict(wire_baseline.c_message_ids(ng_raw)).values()) == list(range(1, 67)))
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    ck("E wire_baseline: %d checks, all green" % len(wb), wb and all(c for _d, c in wb))

    # ---------------------------------------------------------------- F
    nv = S.body(S.mask(gc), S.functions(S.mask(gc)), "pc_mp_guests_name_valid")
    ck("F the shared NAME rule lives in pc_mp_guests.[ch] (declared in the header, defined once): NUL / 127 / 128 / 205 / > 222 refused, blanks 32 / 210 / 211 do not count as content",
       "int  pc_mp_guests_name_valid(const uint8_t* name);" in gh and "#define PC_MP_GUEST_NAME_LEN 8" in gh and nv != ""
       and "ch == 0u || ch == 127u || ch == 128u || ch == 205u || ch > 222u" in nv and "ch != 32u && ch != 210u && ch != 211u" in nv and "k < PC_MP_GUEST_NAME_LEN" in nv)
    ck("F client and host call the SAME function (client: through pc_guest_spec_check, used by the early validator and by the poll)", "pc_mp_guests_name_valid(home->player_name)" in sc
       and "pc_mp_guests_name_valid(key->player_name)" in gcheck and "pc_guest_spec_check(spec, &home" in cb("pc_bootstrap_guest_validate"))
    fd = re.search(r"def guest_fresh_derive\(pid_be\):.*?return gender, face, item, item - ITM_CLOTH_START", lib, re.S)
    ck("F the python mirror derives with the SAME constants: FNV-1a32 of the 20 canonical bytes, gender = bit 31, face = bits 16..18, shirt = bits 8..10 in the gender's table",
       fd is not None and "h = fnv1a32(bytes(pid_be))" in fd.group(0) and "(h >> 31) & 1" in fd.group(0) and "(h >> 16) & 7" in fd.group(0) and "(h >> 8) & 7" in fd.group(0)
       and "8 if gender == SEX_FEMALE else 0" in fd.group(0) and "def fnv1a32(data):" in lib and "h = 2166136261" in lib and "16777619" in lib)
    ck("F the python test double's FakeClient guest uploads fresh_guest_record_for(): it never clones a resident (guest_record() only re-keys when `base` / `base_slot` is passed explicitly)",
       re.search(r"def guest_record\(g, base_slot=None, bin_dir=None, base=None\):.*?if base is None and base_slot is None:\s*return fresh_guest_record_for\(g\)", lib, re.S) is not None
       and "self.guest_record_img = guest_record(self.guest)" in lib)

    # ---------------------------------------------------------------- H  (Guests G1.1)
    val = cb("pc_bootstrap_guest_validate")
    ck("H pc_bootstrap_guest_validate exists in pc_m_card.c: runs the shared spec check, prints a stderr diagnostic naming WHICH part is wrong, returns 0 (the caller exits 2)",
       val != "" and "pc_guest_spec_check(spec, &home, &g, &f, &why)" in val and "fprintf(stderr," in val and "REFUSED: bad spec '%s': %s" in val and "return 0;" in val and "return 1;" in val)
    reasons = ["the spec is too long", "too many fields", "wrong field count", "NAME is empty", "NAME is too long", "LAND (the home town name) is empty", "LAND (the home town name) is too long",
               "bad PLAYER_ID", "bad LAND_ID", "bad GENDER", "bad FACE", "the identity is not a valid PersonalID", "NAME is not a valid game player name", "is reserved for the server observer"]
    ck("H every failure reason of the spec is a distinct diagnostic string (%d reasons)" % len(reasons), all(r in card for r in reasons))
    mm = S.mask(main_raw)
    i_opt = mm.find('g_pc_bootstrap_guest = argv[i + 1];')
    i_val = mm.find("pc_bootstrap_guest_validate(g_pc_bootstrap_guest)")
    i_role = mm.find("g_pc_bootstrap_guest != NULL && (g_pc_net_role != 2")
    first_init = mm.find("    pc_platform_init();")
    ck("H pc_main.c calls pc_bootstrap_guest_validate right after the option parsing / next to the role check, BEFORE pc_platform_init() (window / SDL / network / save), and exits 2 on failure",
       0 < i_opt < i_role < i_val < first_init and "return 2;" in mm[i_val:i_val + 160] and "extern int pc_bootstrap_guest_validate(const char* spec);" in mm)
    ck("H a --bootstrap-guest without a value (last argument) is refused with exit 2 instead of being silently ignored",
       re.search(r'strcmp\(argv\[i\], "--bootstrap-guest"\) == 0\) \{\s*/\*[^*]*\*/\s*fprintf\(stderr, "\[PC\] --bootstrap-guest: REFUSED: the option needs a spec argument[^;]*;\s*return 2;', main_raw))
    after = poll_only[poll_only.index("l_done = 1;"):]
    ck("H no failure path of the CLI arrival is a silent return (G3: the poll after `l_done = 1;` has no `return` at all and ONE exit(2) with the stderr diagnostic for every failure of pc_guest_arrive; "
       "pc_guest_arrive has exactly 10 failure paths (G6.3 added the home-land-equals-town refusal), each a `return 0` with a message: not a client, no GAME_PLAY, scene not ready, save reload, "
       "no valid save, bad spec, resident-name clash, home land == this town, mSDI_StartDataInitGuest, goto_other_scene)",
       "return" not in after and after.count("exit(2);") == 1 and after.count("fprintf(stderr, \"[PC] --bootstrap-guest: %s\\n\", err);") == 1 and after.count("fflush(stdout);") == 1
       and arr.count("return 0;") == 10 and arr.count("snprintf(err, errcap,") == 10 and arr.count("return 1;") == 1)
    pre = poll_only[:poll_only.index("l_done = 1;")]
    ck("H the only early `return`s of the poll are the one-shot / disabled guard and the two readiness waits (play_main, wipe), BEFORE l_done = 1 (the option-not-supplied path is untouched)",
       pre.count("return;") == 3 and "if (l_done || g_pc_bootstrap_guest == NULL) {" in pre and "gamePT->exec != play_main" in pre and "fb_wipe_mode != WIPE_MODE_NONE" in pre)
    ck("H the failing paths never leave anything bound: the two binding failures (StartDataInitGuest / goto_other_scene) RESTORE now_private / player_no / rtc_enabled before failing, the "
       "bad-spec / name failures are before the binding",
       re.search(r"mSDI_StartDataInitGuest\(gamePT\) != TRUE\) \{\s*Common_Set\(now_private, prev_private\);\s*Common_Set\(player_no, prev_player_no\);\s*Common_Set\(time\.rtc_enabled, prev_rtc\);", arr)
       and re.search(r"scene_res != TRUE\) \{\s*Common_Set\(demo_profiles\[0\], mAc_PROFILE_NUM\);\s*Common_Set\(now_private, prev_private\);", arr))
    # reserved name: one shared helper, exact, observer literal agrees
    rn = S.body(S.mask(gc), S.functions(S.mask(gc)), "pc_mp_guests_name_reserved")
    ck("H the RESERVED observer name is ONE shared exact helper (pc_mp_guests.[ch]): 8-byte memcmp against PC_MP_GUEST_RESERVED_NAME = \"SERVER  \" (NULL -> 0), not case folded",
       '#define PC_MP_GUEST_RESERVED_NAME "SERVER  "' in gh and "int  pc_mp_guests_name_reserved(const uint8_t* name);" in gh and rn != "" and "name == NULL" in rn
       and "memcmp(name, PC_MP_GUEST_RESERVED_NAME, PC_MP_GUEST_NAME_LEN) == 0" in rn and "tolower" not in rn and "toupper" not in rn and "strcasecmp" not in rn)
    obs_poll = cb("pc_host_observer_poll")
    ck("H the observer's literal is the same 6 letters space padded to 8: memset CHAR_SPACE x PLAYER_NAME_LEN then memcpy \"SERVER\", 6 (== PC_MP_GUEST_RESERVED_NAME)",
       "memset(base.player_name, CHAR_SPACE, PLAYER_NAME_LEN);" in obs_poll and 'memcpy(base.player_name, "SERVER", 6);' in obs_poll and PC_RES_OK(gh))
    ck("H client and host use the SAME reserved helper: the early spec check (validator + poll) and the host's new-key AND re-mint paths",
       "pc_mp_guests_name_reserved(home->player_name)" in sc and gcheck.count("pc_mp_guests_name_reserved(key->player_name)") == 3)
    # host order: new-key-only rules
    i_tok = gcheck.index("if (tok_ok) {")
    i_dis = gcheck.index("pcnetgame_guest_entry_disposable(g)")
    i_new = gcheck.rindex("if (pc_mp_guests_name_reserved(key->player_name)) {")
    ck("H host: mode 1 (known key + valid token) returns BEFORE any resident-name / reserved rule: that branch only LOGS when a resident / reserved name clash exists",
       "pcnetgame_guest_name_conflict_resident(key)" not in gcheck[:i_tok] and "pcnetgame_host_refuse_identity" not in gcheck[i_tok:i_dis].split("return 1;")[0]
       and "authenticated returning guest slot %d is admitted although its name now equals a resident's name or the reserved name" in gcheck and "*out_mode = 1;" in gcheck[i_tok:i_dis])
    ck("H host: mode 2 (unconfirmed data-less re-mint) applies the reserved + resident-name rules (a new identity in effect); a NEW key applies reserved, resident, other-guest",
       gcheck[i_dis:i_new].count("pc_mp_guests_name_reserved(key->player_name)") == 1 and gcheck[i_dis:i_new].count("pcnetgame_guest_name_conflict_resident(key)") == 1
       and gcheck[i_dis:i_new].index("pc_mp_guests_name_reserved") < gcheck[i_dis:i_new].index("*out_mode = 2;")
       and gcheck[i_new:].count("pc_mp_guests_name_reserved(key->player_name)") == 1 and gcheck[i_new:].count("pcnetgame_guest_name_conflict_resident(key)") == 1
       and gcheck[i_new:].count("pcnetgame_guest_name_conflict_guest(key)") == 1)
    ck("H host: the KEY conflict (resident PersonalID / house owner / observer id), the validity checks and the home-land check still run for ALL modes (before the lookup)",
       gcheck.index("pcnetgame_guest_key_conflict(key)") < gcheck.index("pcnetgame_guest_find(key)") and "pcnetgame_guest_key_valid(key)" in gcheck[:gcheck.index("pcnetgame_guest_find(key)")]
       and "pc_mp_guests_name_valid(key->player_name)" in gcheck[:gcheck.index("pcnetgame_guest_find(key)")] and "ext->home_land_id == s_host_town.land_id" in gcheck[:gcheck.index("pcnetgame_guest_find(key)")])
    ck("H the token decision is unchanged: plain memcmp of the stored token, a wrong / absent token for a known non-disposable key is refused",
       "memcmp(ext->token, s_guest[g].token, PC_NETGAME_GUEST_TOKEN_LEN) == 0" in gcheck and "known guest key presented a WRONG token" in gcheck and "presented WITHOUT a token" in gcheck)

    # ---------------------------------------------------------------- G
    ck("G the guest-creation code never writes Save_t private_data / homes: pc_m_card.c's guest functions contain no `Save_Set(private_data` / `Save_GetPointer(private_data` / homes / mHS_ / mHm_ calls",
       not re.search(r"Save_Set\(private_data|Save_GetPointer\(private_data|homes|mHS_|mHm_", allg.replace("PersonalID_c* p = &Save_Get(private_data)[i].player_ID;", "")))
    ck("G the observer / resident bootstrap and the GCI writers are not touched by this change: pc_save_bswap.c and the m_private.c decomp files have no diff vs HEAD "
       "(G3: m_start_data_init.c IS edited, additively -- pinned strictly in test_guest_g3_src.py)",
       all(not os.popen('git -C "%s" diff --name-only HEAD -- %s' % (ROOT, f)).read().strip() for f in ("src/game/m_private.c", "src/game/m_needlework.c", "include/m_private.h"))
       # furniture sync: pc_save_bswap.c may only GAIN the public pc_save_bswap_home() wrapper (no removed line, nothing else)
       and not [ln for ln in os.popen('git -C "%s" diff -U0 --ignore-cr-at-eol HEAD -- pc/src/pc_save_bswap.c' % ROOT).read().split("\n") if ln.startswith("-") and not ln.startswith("---")]
       and "void pc_save_bswap_home(mHm_hs_c* home, pc_bswap_dir_t dir) {" in os.popen('git -C "%s" diff -U0 --ignore-cr-at-eol HEAD -- pc/src/pc_save_bswap.c' % ROOT).read())
    eol_ok = True
    for rel, crlf in (("pc/src/pc_m_card.c", True), ("pc/src/pc_net_game.c", False), ("pc/src/pc_main.c", False), ("pc/src/pc_mp_guests.c", False), ("pc/include/pc_mp_guests.h", False),
                      ("pc/tools/net_spike/net_spike_lib.py", False), ("pc/tools/net_spike/test_guest_protocol.py", False), ("pc/tools/net_spike/test_guest_real_client.py", False), ("pc/tools/net_spike/test_guest_bootstrap_cli.py", False),
                      ("pc/tools/net_spike/test_guest_persist.py", False), ("pc/tools/net_spike/test_guest_src.py", False), ("pc/tools/net_spike/test_guest_g1_src.py", False),
                      ("pc/tools/net_spike/mp_guests_selftest.c", False)):
        b = raw_bytes(rel)
        n_crlf, n_lf = b.count(b"\r\n"), b.count(b"\n")
        good = (n_crlf == n_lf) if crlf else (n_crlf == 0)
        if not good:
            print("   EOL mismatch:", rel, n_crlf, n_lf)
        eol_ok = eol_ok and good
    ck("G every edited file keeps its EOL (CRLF worktree files fully CRLF, LF files with no CR)", eol_ok)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
