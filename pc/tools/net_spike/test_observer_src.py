#!/usr/bin/env python3
"""test_observer_src.py - SOURCE AUDIT of the opt-in hidden server observer (`--host-observer`). No game process.

Reads the sources (working tree) and `git show <BASELINE_REF|FEATURE_REF>:<path>` (read-only; "HEAD" below means the pre-observer parent BASELINE_REF) and checks the rules the design rests on:
  R   record + identity: the observer record is a static Private_c in pc_m_card.c OUTSIDE Save_t.private_data[]; it is only ever referenced inside the
      fenced /* OBSERVER-BEGIN */ ... /* OBSERVER-END */ blocks; bound with player_no == mPr_FOREIGNER (4) EXACTLY (no 5 / PLAYER_NUM+1 / TOTAL_PLAYER_NUM sentinel
      anywhere in the added code); bound BEFORE the init; the reserved PersonalID is checked against private_data[0..PLAYER_NUM) (even away residents) and
      homes[0..HOUSE_NUM) ownerIDs with a fixed candidate sequence, FAILS the startup (exit code 3) when no candidate is free, and is part of the guest-key conflict
  I   init: mSDI_StartDataInitObserver is TARGET_PC only, its calls are EXACTLY the player-neutral calls of mSDI_StartInitPak (read from the same file) minus
      mNpc_SetReturnAnimal / mNpc_SendRegisteredGoodbyMail / mEv_SetGateway, plus mEv_UnSetGateway first and mSDI_StartInitAfter(.., FALSE, ZELDA) last; no passport / Card B;
      the poll arms pc_save_ready ONLY after the init succeeded, never after a failure, goes to SCENE_FG at (1979, 760) with a plain fade (no RIDE_OFF_DEMO / train
      profile), forces borderless, sets a one-way latch under (play_main, SCENE_FG, no wipe, pcfa_scene_is_town())
  E   exclusion: every net guard of the trace-E list is present (roster, scene replay, scene announce, MOVE, appearance broadcast, PLAYER_ACTION, every
      pc_net_game_host_local_* function, player context 8, IDENTITY_ACK semantic + client skip, own-resident -1), the collision disarm reason, the player
      draw / collider / pad / talk-demo gates
  S   saves: the GCI writers, shutdown save (src/main.c), periodic save block are unchanged vs HEAD; pc_m_card.c WITHOUT its observer blocks is BYTE-IDENTICAL to HEAD;
      nothing inside an observer block serialises or copies the observer record into Save_t; every interactive / travel path that could resolve the observer as a
      resident is guarded (SaveHome, SaveStation_NextLand, toNextLand, InitGameStart, pre-write side effects, LoadPak merge)
  P   plain paths: every pre-existing function touched is HEAD plus ONLY observer lines (diff discipline: no HEAD code line removed or changed except re-indentation /
      an appended `&& !pc_host_observer_active()`), the mSDI / bootstrap / D3 / guest functions are byte-identical to HEAD, pc_main.c / game sources are pure additions
  W   wire: no struct layout change (wire_baseline green), protocol version unchanged, IDENTITY_ACK still 32 bytes, accepted keeps its nonzero == accepted reading
Tier: SOURCE AUDITED. Exit code 0 when all checks pass."""
import os
import re
import subprocess
import sys

import net_spike_lib as L
import wire_baseline

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
OBS_BLOCK = re.compile(r"/\* OBSERVER-BEGIN \*/.*?/\* OBSERVER-END \*/\n?", re.S)
# BASELINE_REF is the PARENT of the observer feature commit (FEATURE_REF = 1a0c8c8): every "vs HEAD" baseline of this audit is really "vs the code before the observer".
# (HEAD itself contains the observer since 1a0c8c8, so it can no longer serve as the baseline.)
# Read side per check:
#   * additive / "unchanged vs baseline" / diff-discipline checks read the FEATURE COMMIT's own blobs (`git show FEATURE_REF:path` vs `git show BASELINE_REF:path`), so
#     they are independent of any later work in the tree (e.g. Dedicated Phase A) and a violation introduced by the observer commit itself is still caught;
#   * checks that look for guards / markers / predicates read the CURRENT WORKING TREE (read()).
BASELINE_REF = "66bfa48"
FEATURE_REF = "1a0c8c8"
MARKERS = ("observer", "OBSERVER", "pc_host_observer", "HOST_NO_AVATAR", "host-observer")


def read(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
        return f.read().decode("utf-8", "replace").replace("\r\n", "\n")


def head(rel):
    """The file as it was BEFORE the observer feature (BASELINE_REF)."""
    return blob(BASELINE_REF, rel)


def feat(rel):
    """The file as the observer feature commit left it (FEATURE_REF)."""
    return blob(FEATURE_REF, rel)


def blob(ref, rel):
    return subprocess.run(["git", "-C", ROOT, "show", ref + ":" + rel], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def mask(s):
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    s = re.sub(r"/\*.*?\*/", blank, s, flags=re.S)
    return re.sub(r"//[^\n]*", blank, s)


def functions(src):
    out = []
    for m in re.finditer(r"^(?:static )?[A-Za-z_][\w \*]*?\b(\w+)\([^;{]*?\)\s*\{\n", src, re.M):
        if m.group(1) in ("if", "for", "while", "switch"):
            continue
        j = src.find("\n}\n", m.end())
        if j >= 0:
            out.append((m.start(), j, m.group(1)))
    return out


def body(src, funcs, name):
    for a, b, n in funcs:
        if n == name:
            return src[a:b]
    return ""


def strip_obs(text):
    return OBS_BLOCK.sub("", text)


def ws(t):
    return re.sub(r"\s+", "", t)


def hunks(rel):
    """[{rem: [...], add: [...]}] of `git diff -U0 BASELINE_REF FEATURE_REF -- rel` = the observer commit's own change (stripped, non-blank lines)."""
    d = subprocess.run(["git", "-C", ROOT, "diff", "-U0", "--ignore-cr-at-eol", BASELINE_REF, FEATURE_REF, "--", rel], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace")
    out, cur = [], None
    for ln in d.split("\n"):
        if ln.startswith("@@"):
            cur = {"rem": [], "add": []}
            out.append(cur)
        elif cur is None:
            continue
        elif ln.startswith("-"):
            cur["rem"].append(ln[1:].strip())
        elif ln.startswith("+"):
            cur["add"].append(ln[1:].strip())
    for h in out:
        h["rem"] = [x for x in h["rem"] if x]
        h["add"] = [x for x in h["add"] if x]
    return out


def strip_comment(s):
    return re.sub(r"\s*/\*.*?\*/\s*$", "", s).strip()


def norm_added(line):
    """An added line with the observer fragments removed: what the HEAD line would have been (an appended `&& !pc_host_observer_active()` condition, a trailing comment)."""
    return strip_comment(line.replace(" && !pc_host_observer_active()", ""))


def diff_discipline(rel, allow_changed=()):
    """True iff EVERY hunk of the diff vs HEAD is an observer hunk (some added line carries an observer marker) AND no HEAD code line was removed or changed: each removed
    line must reappear in the SAME hunk (re-indentation, an appended `&& !pc_host_observer_active()`, a changed trailing comment) -- except the explicitly allowed
    rewrites. Returns (ok, detail)."""
    bad = []
    for h in hunks(rel):
        if h["add"] and not any(m in a for a in h["add"] for m in MARKERS) and not all(a.startswith(("/*", "*", "//")) for a in h["add"]):
            bad.append("non-observer hunk: %s" % h["add"][0][:60])
        pool = [ws(norm_added(a)) for a in h["add"]]
        for r in h["rem"]:
            if r in allow_changed:
                continue
            if ws(strip_comment(r)) in pool:
                pool.remove(ws(strip_comment(r)))
            else:
                bad.append("removed/changed: %s" % r[:60])
    return (not bad, "; ".join(bad[:3]) or "all hunks are observer hunks")


def diff_lines(rel):
    hs = hunks(rel)
    return [x for h in hs for x in h["rem"]], [x for h in hs for x in h["add"]]


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    card_raw = read("pc/src/pc_m_card.c")
    card_head = head("pc/src/pc_m_card.c")
    card = mask(card_raw)
    cf = functions(card)
    cb = lambda n: body(card, cf, n)
    net_raw = read("pc/src/pc_net_game.c")
    net = mask(net_raw)
    nf = functions(net)
    nb = lambda n: body(net, nf, n)
    sdi = mask(read("src/game/m_start_data_init.c"))
    sf = functions(sdi)
    sb = lambda n: body(sdi, sf, n)

    # ------------------------------------------------------------------------------------------------ R
    blocks = OBS_BLOCK.findall(card_raw)
    ck("R pc_m_card.c: 8 fenced observer blocks (includes, record + predicates, pick-id + poll, pre-write guard, InitGameStart / SaveHome / NextLand / toNextLand guards)",
       len(blocks) == 8 and card_raw.count("OBSERVER-BEGIN") == 8 and card_raw.count("OBSERVER-END") == 8)
    ck("R the record is a static Private_c of pc_m_card.c, not an element of Save_t.private_data[], not the passport, not g_foreigner_private (code, comments excluded)",
       "static Private_c s_pc_observer_private;" in card_raw and not re.search(r"s_pc_observer_private\s*=\s*&?\s*(Save|l_mcd|g_foreigner)", card_raw)
       and set(re.findall(r"Common_Set\(now_private,\s*([^)]*)\)", mask("".join(blocks)))) <= {"rec", "prev_private"}  # bound only to the static record (rec = &s_pc_observer_private) or restored
       and "Private_c* rec = &s_pc_observer_private;" in mask("".join(blocks))
       and "l_mcd_foreigner_file" not in mask("".join(blocks)) and "g_foreigner_private" not in mask("".join(blocks)))
    outside = strip_obs(card_raw)
    ck("R s_pc_observer_private is referenced ONLY inside the fenced observer blocks (no resident / travel / save code can reach it)", "s_pc_observer_private" not in outside)
    ck("R pc_m_card.c WITHOUT its observer blocks is BYTE-IDENTICAL to HEAD (no pre-existing line changed)  [feature commit vs its parent]", strip_obs(feat("pc/src/pc_m_card.c")) == card_head)
    obs_text = mask("".join(blocks))
    sets = re.findall(r"Common_Set\(player_no,\s*([^)]*)\)", obs_text) + re.findall(r"Common_Set\(player_no,\s*([^)]*)\)", sb("mSDI_StartDataInitObserver"))
    ck("R player_no is bound to mPr_FOREIGNER (4) EXACTLY (or the saved previous value on a failed init): %s" % sets,
       sets.count("mPr_FOREIGNER") >= 1 and set(sets) <= {"mPr_FOREIGNER", "prev_player_no"})
    all_added = "\n".join("\n".join(diff_lines(p)[1]) for p in ("pc/src/pc_m_card.c", "pc/src/pc_net_game.c", "pc/src/pc_main.c", "src/game/m_start_data_init.c"))
    ck("R no 5 / PLAYER_NUM + 1 / TOTAL_PLAYER_NUM / mPr_PLAYER_NUM player_no sentinel anywhere in the added code (traceD: >= 5 corrupts homes[] and freezes events)",
       not re.search(r"player_no[^;\n]{0,40}(?:,\s*5\b|PLAYER_NUM\s*\+\s*1|TOTAL_PLAYER_NUM|mPr_PLAYER_NUM)", all_added))
    poll = cb("pc_host_observer_poll")
    pick = cb("pc_host_observer_pick_id")
    i_bind = poll.find("Common_Set(now_private, rec);")
    i_pno = poll.find("Common_Set(player_no, mPr_FOREIGNER);")
    i_init = poll.find("mSDI_StartDataInitObserver(gamePT)")
    ck("R now_private + player_no are bound BEFORE the init (so its mEv_UnSetGateway() clears a stale visitor flag)", 0 < i_bind < i_pno < i_init)
    ck("R the record starts from the vanilla initialiser (valid default appearance, exists = TRUE) then gets the reserved identity: name 'SERVER', this town's land, a picked player_id",
       "mPr_InitPrivateInfo(rec);" in poll and 'memcpy(base.player_name, "SERVER", 6)' in poll and "pc_host_observer_pick_id(&base)" in poll and "rec->exists = TRUE;" in poll)
    ck("R the reserved id is checked against ALL four private_data PersonalIDs (no exists / null filter: an away resident counts) and ALL homes[] ownerIDs, on the full PersonalID and on the "
       "bare player_id, over a fixed deterministic candidate sequence of >= 8 ids",
       "i < PLAYER_NUM" in pick and "Save_Get(private_data)[i].player_ID" in pick and "i < mHS_HOUSE_NUM" in pick and "Save_Get(homes[i]).ownerID" in pick
       and "memcmp(p, &cand, sizeof(cand)) == 0 || p->player_id == cand.player_id" in pick and "exists" not in pick
       and "0xF0FEu - (u16)c" in pick and int(re.search(r"#define PC_OBSERVER_ID_CANDIDATES (\d+)", card).group(1)) >= 8)
    ck("R no free candidate FAILS the startup: loud message on stderr + log, exit code 3 (never 'continue with an ambiguous identity')",
       "if (pid == 0) {" in poll and "exit(3);" in poll and "refusing to host with an ambiguous identity" in poll and "[NET][OBSERVER] FATAL" in poll)
    gk = nb("pcnetgame_guest_key_conflict")
    ck("R the guest-key conflict check refuses the observer's PersonalID (no guest can claim it) before the resident / house-owner scans",
       "pc_host_observer_id_matches(key)" in gk and gk.index("pc_host_observer_id_matches(key)") < gk.index("private_data"))
    ck("R pc_host_observer_id_matches / active / ready are exported through the small header pc_host_observer.h; the predicate = HOST role && flag && Now_Private == the static record && player_no == mPr_FOREIGNER",
       all(x in read("pc/include/pc_host_observer.h") for x in ("int pc_host_observer_active(void);", "int pc_host_observer_ready(void);", "int pc_host_observer_id_matches(const void* personal_id);"))
       and ws("g_pc_host_observer != 0 && pc_net_game_role() == PC_NETGAME_ROLE_HOST && Common_Get(now_private) == &s_pc_observer_private && Common_Get(player_no) == mPr_FOREIGNER") in ws(cb("pc_host_observer_active"))
       and ws("s_pc_observer_latched != 0 && pc_host_observer_active()") in ws(cb("pc_host_observer_ready")))
    ck("R pcnetgame_host_own_resident_idx returns -1 FIRST when the observer is active (the host plays no resident), every resident stays claimable",
       ws("int i;if(pc_host_observer_active()){return-1;}if(Now_Private==NULL){return-1;}") in ws(nb("pcnetgame_host_own_resident_idx")))

    # ------------------------------------------------------------------------------------------------ I
    sdi_raw = read("src/game/m_start_data_init.c")
    obsfn = sb("mSDI_StartDataInitObserver")
    pak = sb("mSDI_StartInitPak")
    call_rx = r"\b(m[A-Z][A-Za-z0-9_]*)\("
    skip = ("mFRm_CheckSaveData", "mSDI_StartInitPak", "mSDI_StartDataInitObserver")  # the guard test + the function names themselves
    pak_calls = [c for c in re.findall(call_rx, pak) if c not in skip]
    obs_calls = [c for c in re.findall(call_rx, obsfn) if c not in skip]
    excluded = {"mNpc_SetReturnAnimal", "mNpc_GetInAnimalP", "mNpc_SendRegisteredGoodbyMail", "mEv_SetGateway"}
    expected = [c for c in pak_calls if c not in excluded]
    i_def = sdi_raw.index("extern int mSDI_StartDataInitObserver(GAME* game) {")
    ck("I mSDI_StartDataInitObserver is TARGET_PC only (its definition sits between #ifdef TARGET_PC and the closing #endif) and declared in m_start_data_init.h under TARGET_PC",
       sdi_raw.rfind("#ifdef TARGET_PC", 0, i_def) > sdi_raw.rfind("#endif", 0, i_def) and sdi_raw.index("#endif", i_def) > sdi_raw.index("\n}\n", i_def)
       and re.search(r"#ifdef TARGET_PC\n[^#]*extern int mSDI_StartDataInitObserver\(GAME\* game\);\n#endif", read("include/m_start_data_init.h")) is not None)
    ck("I its calls are EXACTLY mSDI_StartInitPak's player-neutral calls minus SetReturnAnimal / SendRegisteredGoodbyMail / SetGateway, with mEv_UnSetGateway first and mSDI_StartInitAfter last "
       "(Pak: %s | observer: %s)" % (pak_calls, obs_calls),
       obs_calls == ["mEv_UnSetGateway"] + expected + ["mSDI_StartInitAfter"] and expected and "mEv_SetGateway" not in obsfn.replace("mEv_UnSetGateway", ""))
    ck("I no passport / Card B / merge / travel call in the init or the poll (l_mcd_foreigner_file, mPr_LoadPak, pc_card_, mCD_Save*, mSDI_StartDataInit(.., MODE_PAK))",
       not re.search(r"l_mcd_foreigner_file|mPr_LoadPak|pc_card_|mCD_Save|mSDI_StartDataInit\(|MODE_PAK|g_foreigner_private", obsfn + poll + pick))
    ck("I mSDI_StartInitAfter(game, FALSE, mSDI_MALLOC_FLAG_ZELDA) with the vanilla malloc flag; the init refuses unless now_private != NULL and player_no == mPr_FOREIGNER",
       "mSDI_StartInitAfter(game, FALSE, mSDI_MALLOC_FLAG_ZELDA);" in obsfn and "Common_Get(player_no) != mPr_FOREIGNER" in obsfn and "return FALSE;" in obsfn)
    i_ok = poll.find("!= TRUE) {", i_init)
    i_ready = poll.find("pc_save_ready = 1;")
    ck("I pc_save_ready is armed ONLY after the init returned TRUE (and is disarmed again when the scene change fails)",
       0 < i_init < i_ok < i_ready and poll.count("pc_save_ready = 1;") == 1 and poll.count("pc_save_ready = 0;") == 1 and poll.index("pc_save_ready = 0;") > i_ready)
    ck("I the failure paths restore the previous binding (now_private / player_no) and the host stays unbound; the variants are logged ('observer init FAILED: ...')",
       poll.count("Common_Set(now_private, prev_private);") == 2 and poll.count("Common_Set(player_no, prev_player_no);") == 2 and poll.count("[NET][OBSERVER] host: observer init FAILED") >= 4)
    ck("I the observer enters SCENE_FG at the station point (1979, 760) with a plain fade, NO RIDE_OFF_DEMO / train demo profile (both demo_profiles cleared), borderless forced",
       "door_data.exit_position.x = 1979;" in poll and "door_data.exit_position.z = 760;" in poll and "WIPE_TYPE_FADE_BLACK" in poll and "mAc_PROFILE_RIDE_OFF_DEMO" not in poll
       and "Common_Set(demo_profiles[0], mAc_PROFILE_NUM);" in poll and "Common_Set(demo_profiles[1], mAc_PROFILE_NUM);" in poll and "g_mPlib_wade_disabled = TRUE;" in poll
       and "SCENE_FG" in poll and "goto_other_scene(play, &door_data, TRUE)" in poll)
    ck("I the poll is one-shot (l_started set before any outcome), waits for play_main + no wipe, HOST role + flag only; rtc_enabled is set like both bootstraps",
       "static int l_started = 0;" in poll and poll.index("l_started = 1;") < poll.index("mFRm_CheckSaveData()") and "gamePT->exec != play_main" in poll
       and "play->fb_wipe_mode != WIPE_MODE_NONE" in poll and "pc_net_game_role() != PC_NETGAME_ROLE_HOST" in poll and "Common_Set(time.rtc_enabled, TRUE);" in poll)
    ck("I the readiness latch is one-way and set in ONE place under play_main + SCENE_FG + no wipe + player actor + pcfa_scene_is_town(); logs the exact '[NET][OBSERVER] host: observer active at acre (%d,%d)'",
       card.count("s_pc_observer_latched = 1;") == 1 and "play->scene_id == SCENE_FG" in poll and "Save_Get(scene_no) == SCENE_FG" in poll and "pcfa_scene_is_town()" in poll
       and "s_pc_observer_latched = 0" not in card.replace("static int s_pc_observer_latched = 0;", "") and "[NET][OBSERVER] host: observer active at acre (%d,%d)\\n" in card_raw)
    pv = mask(read("pc/src/pc_vi.c"))
    ck("I pc_vi.c calls pc_host_observer_poll() exactly once, right after the resident / guest bootstrap polls and before the periodic save block",
       pv.count("pc_host_observer_poll();") == 1 and pv.index("pc_bootstrap_guest_poll();") < pv.index("pc_host_observer_poll();") < pv.index("pc_save_write_authoritative();"))
    ck("I pcfa_save_ready: the HOST foreigner passes ONLY via pc_host_observer_ready() (observer active AND latch); the CLIENT exemption is kept; pcfa_scene_is_town() is NOT inside pcfa_save_ready",
       re.search(r"if \(Common_Get\(player_no\) >= mPr_FOREIGNER\) \{\s*if \(pc_net_game_role\(\) == PC_NETGAME_ROLE_CLIENT\) \{\s*\} else if \(pc_net_game_role\(\) == PC_NETGAME_ROLE_HOST "
                 r"&& pc_host_observer_ready\(\)\) \{\s*\} else \{\s*return 0;\s*\}\s*\}", body(mask(read("pc/src/pc_field_authority.c")), functions(mask(read("pc/src/pc_field_authority.c"))), "pcfa_save_ready"), re.S)
       and "pcfa_scene_is_town" not in body(mask(read("pc/src/pc_field_authority.c")), functions(mask(read("pc/src/pc_field_authority.c"))), "pcfa_save_ready"))

    # ------------------------------------------------------------------------------------------------ E
    G = "pc_host_observer_active()"
    ck("E1 roster: the host's own APPEARANCE entry is skipped under the observer (capacity phase 5: the roster pump marks it delivered without sending; every peer entry is untouched)",
       re.search(r"if \(pc_host_observer_active\(\)\) \{[^}]*pc_roster_pend_delivered\(&s_roster_app, dest, subject\);[^}]*continue;[^}]*\}\s*pcnetgame_build_appearance_msg\(&amsg, \(uint8_t\)PC_NETGAME_HOST_PLAYER_ID\);",
                 nb("pcnetgame_roster_pump_dest"), re.S) is not None)
    ck("E2 local appearance-change broadcast: the HOST branch is gated (the cache still updates)",
       "} else if (s_role == PC_NETGAME_ROLE_HOST && !pc_host_observer_active()) {" in net_raw.replace("\r", "") and "s_last_local_appearance = current;" in net)
    ck("E3 MOVE send: the HOST branch is gated", "} else if (s_role == PC_NETGAME_ROLE_HOST && !pc_host_observer_active()) { /* --host-observer: no host MOVE stream */" in net_raw)
    ck("E4 PLAYER_SCENE: the join replay and the announce are both gated",
       "s_local_scene.valid && s_local_scene_sent && !pc_host_observer_active()" in nb("pcnetgame_roster_pump_dest") and
       "if (s_role == PC_NETGAME_ROLE_HOST && !pc_host_observer_active())" in nb("pcnetgame_scene_tick"))
    ck("E5 IDENTITY_ACK: under the observer accepted = 1 | HOST_NO_AVATAR, the identity is NOT captured (zero name / id), logged per peer; plain host: capture_local_identity as before",
       "ack.accepted = (uint8_t)(1u | PC_NETGAME_ACK_FLAG_HOST_NO_AVATAR);" in net_raw and "#define PC_NETGAME_ACK_FLAG_HOST_NO_AVATAR 0x02u" in net_raw
       and re.search(r"if \(pc_host_observer_active\(\)\) \{[^}]*ack\.accepted[^}]*\} else \{\s*pcnetgame_capture_local_identity\(ack\.player_name", nb("pcnetgame_host_process_identity"), re.S) is not None
       and "host has no avatar (roster / move / scene / appearance suppressed)" in net_raw)
    ck("E5 client: accepted is still read as 'any nonzero == accepted' (`!in.accepted` return unchanged); bit 0x02 skips ONLY pc_remote_player_on_ready(HOST), the rest of the handshake is unchanged",
       "if (!in.accepted) return;" in net_raw and re.search(r"if \(in\.accepted & PC_NETGAME_ACK_FLAG_HOST_NO_AVATAR\) \{[^}]*host has no avatar \(no host puppet\)[^}]*\} else \{\s*pc_remote_player_on_ready\(PC_NETGAME_HOST_PLAYER_ID, &s_client_host_identity\);\s*\}\s*pcnetgame_crec_on_ready\(\);", net_raw) is not None)
    ck("E6 PLAYER_ACTION for host pickups: pcnetgame_host_emit_player_action returns for origin == HOST id under the observer", "origin == (int)PC_NETGAME_HOST_PLAYER_ID && pc_host_observer_active()" in nb("pcnetgame_host_emit_player_action"))
    locals_ = sorted(set(re.findall(r"^(?:void|int) (pc_net_game_host_local_\w+)\(", net, re.M)) | {"pc_net_game_notify_local_field_pickup"})
    bad = [n for n in locals_ if G not in nb(n)]
    ck("E7 every pc_net_game_host_local_* action (%d: %s) + notify_local_field_pickup is a no-op under the observer" % (len(locals_), ", ".join(n.replace("pc_net_game_host_local_", "") for n in locals_)),
       len(locals_) >= 10 and not bad)
    ck("E8 pc_net_game_get_player_context(8) returns 0 under the observer", re.search(r"if \(player_id == PC_NETGAME_HOST_PLAYER_ID\) \{\s*if \(pc_host_observer_active\(\)\) \{\s*return 0;",
                                                                                      nb("pc_net_game_get_player_context")) is not None)
    rp = mask(read("pc/src/pc_remote_player.c"))
    rpf = functions(rp)
    ce = body(rp, rpf, "pc_remote_player_collide_eval")
    ck("E10 collide eval returns the reason 'observer' FIRST (before scene / snapshot / local checks); the disarm line 'DISARMED reason=observer' is logged once",
       ce.index('return "observer"') < ce.index('return "scene"') and "pc_host_observer_active()" in ce[:ce.index('return "observer"')][-80:] and
       "collider DISARMED reason=observer" in read("pc/src/pc_remote_player.c") and "s_observer_disarm_logged" in body(rp, rpf, "pc_remote_player_collide_update"))
    mp = read("src/game/m_player.c")
    mpc = mask(read("src/game/m_player_common.c_inc"))
    ck("E11 Player_actor_draw: the observer returns before any draw (TARGET_PC)", re.search(r"#ifdef TARGET_PC\s*if \(pc_host_observer_active\(\)\) \{\s*return;[^}]*\}\s*#endif\s*if \(mPlayer_MAIN_INDEX_VALID\(main_idx\) != FALSE\) \{",
                                                                                             mp.replace("\r", "")) is not None)
    cf2 = functions(mpc)
    ck("E11 colliders: forStand, forOutdoor and both item colliders (OCC) return before any CollisionCheck_set* call under the observer",
       all(G in body(mpc, cf2, n) and body(mpc, cf2, n).index(G) < body(mpc, cf2, n).index("CollisionCheck_set") for n in ("Player_actor_Excute_Corect_forStand", "Player_actor_Excute_Corect_forOutdoor"))
       and body(mpc, cf2, "Player_actor_SetPosition_OBJtoLine_forItem").index(G) < body(mpc, cf2, "Player_actor_SetPosition_OBJtoLine_forItem").index("CollisionCheck_setOCC"))
    pad = mask(read("pc/src/pc_pad.c"))
    ck("E11 pad input: PADRead (the single controller read) zeroes buttons, both sticks and both triggers under the observer, before the status is filled",
       re.search(r"if \(pc_host_observer_active\(\)\) \{\s*buttons = 0;\s*stickX = stickY = cstickX = cstickY = 0;\s*status\[0\]\.triggerLeft = 0;\s*status\[0\]\.triggerRight = 0;\s*\}\s*status\[0\]\.button = buttons;", pad) is not None
       and "SDL_GetKeyboardState" not in mask(read("src/game/m_player_lib.c")) and "SDL_GetKeyboardState" not in mask(read("src/game/m_player.c")))
    dm = mask(read("src/game/m_demo.c"))
    mr = body(dm, functions(dm), "mDemo_Request")
    ck("E9 talk demos: mDemo_Request refuses TALK / SPEAK / REPORT / SPEECH / EVENTMSG / EVENTMSG2 under the observer (the observer has no input to close a message window)",
       all(("type == mDemo_TYPE_%s" % t) in mr for t in ("TALK", "SPEAK", "REPORT", "SPEECH", "EVENTMSG", "EVENTMSG2")) and "pc_host_observer_active()" in mr
       and "if (request_num < mDemo_REQUEST_NUM) {" in mr and mr.index("pc_host_observer_active()") < mr.index("if (request_num < mDemo_REQUEST_NUM) {"))
    mraw = read("pc/src/pc_main.c")
    ck("E pc_main.c refusal sites: HOST-only, --bootstrap-resident, --bootstrap-guest and the hook list (10 names), all `return 2`, before the stdout redirect",
       all(x in mraw for x in ("--host-observer is a HOST-only option", "--host-observer cannot be combined with --bootstrap-resident", "--host-observer cannot be combined with --bootstrap-guest",
                               "--force-friendship-delta", "--force-mail-send", "--force-money-rock-hit", "--force-fish-catch", "--force-bug-catch", "--diag-bug-despawn-label-race",
                               "--scene-test-enter-shop", "--scene-test-leave-after", "--collide-test-overlap", "--collide-test-approach"))
       and mraw.index("if (g_pc_host_observer) {") < mraw.index("Redirect stdout/stderr to NUL unless verbose") and mraw.count("return 2;") >= 5)

    # ------------------------------------------------------------------------------------------------ S
    writers = ("pc_save_write_gci_to", "pc_save_write_gci", "pc_save_rotate_backups", "pc_save_write_authoritative", "pc_save_check_and_load")
    outside_feat = strip_obs(feat("pc/src/pc_m_card.c"))  # feature-commit side: independent of later (Phase A) edits to pc_m_card.c
    stripped = mask(outside_feat)
    sff = functions(stripped)
    hmask = mask(card_head)
    hf = functions(hmask)
    ck("S the GCI writers + the loader are unchanged vs HEAD (%s)" % ", ".join(writers),
       all(body(stripped, sff, n).strip() == body(hmask, hf, n).strip() != "" for n in writers))
    save_writers = ("pc_save_write_gci_to", "pc_save_write_gci", "pc_save_rotate_backups", "pc_save_write_authoritative")
    ck("S the writers never read the player binding (no Now_Private / now_private / player_no inside %s): Save_t is serialised verbatim, so the observer record can never reach a GCI"
       % ", ".join(save_writers), all(body(stripped, sff, n) != "" and not re.search(r"Now_Private|now_private|player_no", body(stripped, sff, n)) for n in save_writers))
    ck("S src/main.c (shutdown save) and pc_save_bswap.c are byte-identical to HEAD (the observer never needed a change: pcfa_save_ready() gates it)",
       feat("src/main.c") == head("src/main.c") and feat("pc/src/pc_save_bswap.c") == head("pc/src/pc_save_bswap.c"))
    rem, add = diff_lines("pc/src/pc_vi.c")
    ck("S pc_vi.c: the diff is ONLY the observer poll call block (no line removed; the periodic / early save logic is untouched)", not rem and all(("observer" in a or a in ("{", "}", "pc_host_observer_poll();")) or a.startswith("/*") for a in add))
    obs_all = "".join(blocks)
    ck("S nothing inside an observer block writes or serialises: no pc_save_write*, fwrite, private_data assignment, mPr_CopyPrivateInfo, mPr_LoadPak, bcopy into Save (the only pc_save_ready "
       "writes are the arming after a successful init and the disarm after a failed scene change)",
       not re.search(r"pc_save_write|fwrite|fopen|Save_Set\(|mPr_CopyPrivateInfo|mPr_LoadPak|bcopy|private_data\[[^\]]*\]\s*=|memcpy\(\s*&?Save", mask(obs_all)))
    ck("S the guards: pre-write side effects, InitGameStart, SaveHome, SaveStation_NextLand and toNextLand each start (before any write / travel statement) with the observer refusal",
       all(G in cb(n) for n in ("pc_save_pre_write_side_effects", "mCD_InitGameStart_bg", "mCD_SaveHome_bg", "mCD_SaveStation_NextLand_bg", "mCD_toNextLand"))
       and cb("mCD_SaveHome_bg").index(G) < cb("mCD_SaveHome_bg").index("pc_save_pre_write_side_effects(")
       and cb("mCD_SaveStation_NextLand_bg").index(G) < cb("mCD_SaveStation_NextLand_bg").index("if (is_foreigner) {")
       and cb("mCD_toNextLand").index(G) < cb("mCD_toNextLand").index("memset(&common_data")
       and cb("pc_save_pre_write_side_effects").index(G) < cb("pc_save_pre_write_side_effects").index("mCkRh_SavePlayTime")
       and cb("mCD_InitGameStart_bg").index(G) < cb("mCD_InitGameStart_bg").index("mSDI_StartDataInit("))
    mpri = mask(read("src/game/m_private.c"))
    lp = body(mpri, functions(mpri), "mPr_LoadPak_and_SetPrivateInfo2")
    ck("S mPr_LoadPak_and_SetPrivateInfo2 (the only routine that copies a Private_c INTO private_data[]) refuses under the observer before any merge", G in lp and lp.index(G) < lp.index("mPr_CopyPrivateInfo"))
    mh = read("src/game/m_house.c")
    ck("S mHS_get_arrange_idx logs (first 3) when called for the observer (player_no >= 4 aliases an in-range house); it cannot refuse, the observer stays outdoors and inert",
       "player_no >= mPr_FOREIGNER && pc_host_observer_active()" in mh and "WARNING: house arrangement looked up for the observer" in mh)

    # ------------------------------------------------------------------------------------------------ P
    for rel in ("pc/src/pc_net_game.c", "pc/src/pc_remote_player.c", "pc/src/pc_field_authority.c", "pc/src/pc_pad.c", "pc/src/pc_vi.c"):
        # the ONE deliberate rewrite of a HEAD line: pcfa_save_ready's client test became `== CLIENT { } else if (HOST && observer ready) { } else { return 0; }` (pinned exactly above)
        ok, detail = diff_discipline(rel, allow_changed=("if (pc_net_game_role() != PC_NETGAME_ROLE_CLIENT) {",) if rel.endswith("pc_field_authority.c") else ())
        ck("P %s: the diff vs HEAD removes / changes NO pre-existing code line (re-indentation and an appended `&& !pc_host_observer_active()` aside) and every added code line is an observer line (%s)" % (rel, detail), ok)
    for rel in ("pc/src/pc_main.c", "src/game/m_start_data_init.c", "include/m_start_data_init.h", "src/game/m_demo.c", "src/game/m_private.c", "src/game/m_house.c", "src/game/m_player.c",
                "src/game/m_player_common.c_inc"):
        rem, add = diff_lines(rel)
        ck("P %s: a PURE addition (no HEAD line removed or changed): %d added line(s)" % (rel, len(add)), not rem and add)
    ck("P the game sources' additions are all under #ifdef TARGET_PC (m_demo.c, m_private.c, m_house.c, m_player.c draw, m_player_common.c_inc, m_start_data_init.c/.h)",
       all(read(p).count("pc_host_observer_active") == 0 or re.findall(r"#ifdef TARGET_PC", read(p)) for p in ("src/game/m_demo.c", "src/game/m_private.c", "src/game/m_house.c", "src/game/m_player.c"))
       and read("src/game/m_player_common.c_inc").count("#ifdef TARGET_PC") >= 3)
    unchanged = [("src/game/m_start_data_init.c", n) for n in ("mSDI_StartInitNew", "mSDI_StartInitFrom", "mSDI_StartInitPak", "mSDI_StartInitAfter", "mSDI_StartInitBefore", "mSDI_StartDataInit", "mSDI_StartInitNewPlayer")]
    unchanged += [("pc/src/pc_m_card.c", n) for n in ("pc_bootstrap_resident_poll", "pc_bootstrap_guest_poll")]
    ok_same = True
    which = []
    for rel, n in unchanged:
        cur = mask(strip_obs(feat(rel)))
        hd = mask(head(rel))
        a, b = body(cur, functions(cur), n).strip(), body(hd, functions(hd), n).strip()
        if not (a and a == b):
            ok_same = False
            which.append(n)
    ck("P the plain --host / --bootstrap-resident / --bootstrap-guest / solo code is BYTE-IDENTICAL to HEAD: mSDI_StartInit{New,From,Pak,After,Before,NewPlayer}, mSDI_StartDataInit, "
       "pc_bootstrap_resident_poll, pc_bootstrap_guest_poll %s" % which, ok_same)
    gbody = body(mask(feat("pc/src/pc_net_game.c")), functions(mask(feat("pc/src/pc_net_game.c"))), "pcnetgame_host_classify_identity")
    ck("P the identity classifier is byte-identical to HEAD (only the own-resident function gained its early return; test_guest_src pins that exactly)",
       mask(head("pc/src/pc_net_game.c")).count("pcnetgame_host_classify_identity") >= 1 and
       body(mask(head("pc/src/pc_net_game.c")), functions(mask(head("pc/src/pc_net_game.c"))), "pcnetgame_host_classify_identity").strip() == gbody.strip() != "")

    # ------------------------------------------------------------------------------------------------ W
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    ck("W wire_baseline: no wire struct / enum / constant / protocol change vs HEAD (%d checks)" % len(wb), wb and all(c for _d, c in wb))
    ck("W IDENTITY_ACK is still exactly 32 bytes and the protocol version is unchanged (%d); the ACK change is a SEMANTIC of the existing `accepted` byte only (no bump)"
       % wire_baseline.EXPECTED_PROTOCOL_VERSION, "_Static_assert(sizeof(PCNetGameIdentityAckMsg) == 32" in net_raw and wire_baseline.EXPECTED_PROTOCOL_VERSION == 8
       and L.PROTOCOL_VERSION == 8 and "#define PC_NETGAME_PROTOCOL_VERSION 8u" in read("pc/include/pc_net_game.h"))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
