#!/usr/bin/env python3
"""test_display_refresh_src.py - live display refresh (SOURCE AUDIT + small behaviour model, no game process).
Police: host already redraws when the host player stands in the police box (mFI_SetFGUpData after a client claim); client redraws on the TS push.
Museum: paintings / plaques read Save every frame; fish + insect actors build newly donated exhibits in place from their own move
(Museum_*_PcRefreshDonations, multiplayer only); fossil skeletons (room FG furniture) still need re-entry.
Able Sisters: Needlework_Indoor re-copies a changed shared design (PAGE kind DESIGN) through the vanilla aNI_Copy*Data path."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NG = (ROOT / "pc/src/pc_net_game.c").read_text(encoding="utf-8", errors="replace")
FISH = (ROOT / "src/actor/ac_museum_fish.c").read_text(encoding="utf-8", errors="replace")
INS = (ROOT / "src/actor/ac_museum_insect.c").read_text(encoding="utf-8", errors="replace")
NI = (ROOT / "src/actor/ac_needlework_indoor.c").read_text(encoding="utf-8", errors="replace")
FISH_ALL = FISH + "".join(p.read_text(encoding="utf-8", errors="replace") for p in (ROOT / "src/actor").glob("ac_museum_fish_*.c_inc"))
INS_ALL = INS + "".join(p.read_text(encoding="utf-8", errors="replace") for p in (ROOT / "src/actor").glob("ac_museum_insect_*.c_inc"))
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def body(src, sig):
    i = src.find(sig)
    while i >= 0 and src.find(";", i) < src.find("{", i):  # skip prototypes
        i = src.find(sig, i + 1)
    if i < 0:
        return ""
    j = src.index("{", i)
    d = 0
    for k in range(j, len(src)):
        d += src[k] == "{"
        d -= src[k] == "}"
        if d == 0:
            return src[j:k + 1]
    return ""


# ---- police (unchanged, confirm no defect) ----
check("host police claim: redraw (no compaction) when the host is in the police box",
      re.search(r"keep_items\[pidx\] = \(mActor_name_t\)EMPTY_NO;\s*if \(s_local_scene\.valid && s_local_scene\.scene_id == \(uint8_t\)SCENE_POLICE_BOX\) \{[^}]*?mFI_SetFGUpData\(\);", NG, re.S) is not None)
check("client police TS apply: redraw when in the police box",
      re.search(r"svc == \(int\)PC_NETGAME_TS_POLICE && s_local_scene\.valid && s_local_scene\.scene_id == \(uint8_t\)SCENE_POLICE_BOX\) \{\s*mFI_SetFGUpData\(\);", NG) is not None)
check("client shop TS apply: floor display synced", "aSD_PC_SyncDisplayWithStock()" in NG)

# ---- museum fish ----
fr = body(FISH, "static void Museum_Fish_PcRefreshDonations(")
fmove = body(FISH, "void Museum_Fish_Actor_move(")
fct = body(FISH, "void Museum_Fish_Actor_ct(")
check("fish: refresh is called from the actor's own move (TARGET_PC)", re.search(r"#ifdef TARGET_PC\s*Museum_Fish_PcRefreshDonations\(actor, game\);\s*#endif", fmove) is not None)
check("fish: refresh is a no-op single-player", "pc_net_game_role() == PC_NETGAME_ROLE_NONE" in fr and fr.index("ROLE_NONE") < fr.index("for ("))
for stmt in ("kf._54C = mfish_model_tbl[i]", "kf._550[0] = mfish_anime_init_tbl[i]", "_62E_flags |= 1", "Museum_Fish_Prv_data_init(prv, game"):
    check("fish: refresh repeats the ct step '%s'" % stmt, stmt in fr and stmt.replace("game", "gamex") in fct)
check("fish: only builds not-yet-built donated fish", "(prv->_62E_flags & 1) == 0 && mMmd_FishInfo(i)" in fr)
check("fish: a late frog owns the lily pad like the ct", "hasu_p = &actor->_14788" in fr)
# bit 1 of _62E_flags is written only by the ct and the refresh (so it reliably records 'built')
plain = re.findall(r"_62E_flags\s*=[^=]", FISH_ALL)
bit1_clears = [m for m in re.findall(r"_62E_flags\s*&=\s*~\s*(0x[0-9A-Fa-f]+|\d+)", FISH_ALL) if int(m, 0) & 1]
check("fish: bit 1 of _62E_flags is never assigned / cleared outside the ct (so it records 'built')",
      not plain and len(bit1_clears) == 1 and "_62E_flags &= ~1" in fct)
check("fish: no allocation / actor spawn in the refresh", not re.search(r"malloc|Actor_info_make|Setupactor|_dt\(", fr))

# ---- museum insect ----
ir = body(INS, "static void Museum_Insect_PcRefreshDonations(")
imove = body(INS, "void Museum_Insect_Actor_move(")
ict = body(INS, "void Museum_Insect_Actor_ct(")
check("insect: refresh is called from the actor's own move (TARGET_PC)", re.search(r"#ifdef TARGET_PC\s*Museum_Insect_PcRefreshDonations\(actor, game\);", imove) is not None)
check("insect: refresh is a no-op single-player", "pc_net_game_role() == PC_NETGAME_ROLE_NONE" in ir)
for stmt in ("._8C |= 1", "._00 = i", "._14 = minsect_scale_tbl[i]", "set_relax_active_time(&actor->privInsects[i], game)", "minsect_ct[i](&actor->privInsects[i], game)"):
    check("insect: refresh repeats the ct step '%s'" % stmt, stmt in ir and stmt in ict)
check("insect: interpolation state re-seeded after an add", "Museum_Insect_CopyLogicState(actor)" in ir)
check("insect: stag-beetle partner gets the ct's partner bit", "p->_8C |= 0x10" in ir and "atans_table(d.y, -d.x)" in ir)
# bit 1 is never cleared on privInsects (the only '&= ~1' is ari_alone_dt on the ant sub-array)
clears = re.findall(r"(\w+)->_8C &= ~1;", INS_ALL)
check("insect: the only _8C bit-1 clear is ari_alone_dt (ant pool, not privInsects)", clears == ["actor"] and "void ari_alone_dt(" in INS_ALL
      and "privInsects" not in body(INS_ALL, "void ari_alone_dt("))


def _inside_test_hooks(src, marker):
    """True when `marker` sits inside an open `#ifdef PC_NET_TEST_HOOKS` (preprocessor depth counted from the nearest such #ifdef before it).
    The hook used to follow the #ifdef directly; since the museum-pockets test hook (worker B) shares the one block, it is no longer adjacent."""
    at = src.find(marker)
    start = src.rfind("#ifdef PC_NET_TEST_HOOKS", 0, at)
    if at < 0 or start < 0:
        return False
    depth = 0
    for line in src[start:at].splitlines():
        t = line.strip()
        if t.startswith("#if"):
            depth += 1
        elif t.startswith("#endif"):
            depth -= 1
            if depth == 0:
                return False
    return depth >= 1


# ---- Able Sisters ----
nr = body(NI, "static void aNI_PcRefreshFromSave(")
nmove = body(NI, "static void Needlework_Indoor_Actor_move(")
check("able: refresh called only while no menu is open", re.search(r"#ifdef TARGET_PC\s*if \(submenu->process_status == mSM_PROCESS_WAIT\) \{\s*(?:int [^;]*;\s*)?aNI_PcRefreshFromSave\(actor\);", nmove) is not None)
check("able: no-op single-player and while a local request is pending",
      "ROLE_NONE" in nr and all(k in nr for k in ("copy_cloth_data_requested", "exchange_cloth_data_requested", "copy_umb_data_requested", "exchange_umb_data_requested")))
check("able: uses the vanilla copy path without sound", "aNI_CopyClothData(i, save, FALSE)" in nr and "aNI_CopyUmbData(i, save, FALSE)" in nr)
check("able: compares the DISPLAYED buffer with the Save copy", "design_buf_idx[mNW_TYPE_MANEKIN][i] & 1" in nr and "design_buf_idx[mNW_TYPE_UMBRELLA][i] & 1" in nr)
for fn in ("static void aNI_CopyClothData(", "static void aNI_CopyUmbData("):
    b = body(NI, fn)
    check("able: %s writes the active buffer AND the Save together (local edits never look like a remote change)" % fn.split()[2],
          b.count("mNW_CopyOriginalTextureClass(") == 2 and "Save_Get(needlework)" in b)


# ---- test hook used by test_museum_refresh_real.py: test-hooks builds only, one-shot, goes through the game's own scene change ----
hk = body(NG, "static void pcnetgame_museum_warp_test_hook(")
check("hook: AC_TEST_MUSEUM_ROOM hook exists, inside PC_NET_TEST_HOOKS, env-gated (pc_test_hook_getenv)",
      hk != "" and 'pc_test_hook_getenv("AC_TEST_MUSEUM_ROOM")' in hk and _inside_test_hooks(NG, "static void pcnetgame_museum_warp_test_hook("))
check("hook: uses the game's own goto_other_scene and fires once", "goto_other_scene(play, &in, FALSE)" in hk and "s_state = 2" in hk)
check("hook: call site is compiled out without PC_NET_TEST_HOOKS", re.search(r"#ifdef PC_NET_TEST_HOOKS\s*pcnetgame_museum_warp_test_hook\(\);\s*#endif", NG) is not None)

# ---- fossil skeletons: the blocker is structural (documented here so a regression of the assumption is noticed) ----
MR = (ROOT / "src/actor/ac_my_room.c").read_text(encoding="utf-8", errors="replace")
MMD = (ROOT / "src/game/m_museum_display.c").read_text(encoding="utf-8", errors="replace")
check("fossil: every fossil slot always holds a furniture (dummy when undonated), so a donation REPLACES a live furniture actor, never fills an empty slot",
      "fossil_fg[fossil_ut] = fossil_data[fossil_idx].item + rotation;" in MMD and "FTR_DIN_TRIKERA_DUMMY" in MMD)
check("fossil: the furniture actors live in the my_room actor's static list sized from a per-scene table (fossil room = 25 = one per slot: no free actor slot)",
      "SCENE_MUSEUM_ROOM_FOSSIL" in MR and re.search(r"bank_count_table\[\] = \{[^}]*20, 25,", MR) is not None)
check("fossil: a furniture actor needs a model bank DMA'd into the room's fixed bank pool (aMR_GetFurnitureBank2), owned by ac_my_room.c (not part of this workstream)",
      "static int aMR_GetFurnitureBank2(" in MR)

# ---- behaviour model of the additive refresh (mirrors the C loops) ----
def model_refresh(built, donated, partners=None):
    added = []
    for i, d in enumerate(donated):
        if not built[i] and d:
            built[i] = True
            added.append(i)
    return added


built = [False] * 40
donated = [False] * 40
donated[3] = True
model_refresh(built, donated)          # ct state
donated[7] = True                      # remote donation arrives
check("model: exactly the newly donated exhibit is built", model_refresh(built, donated) == [7])
check("model: refresh is idempotent (nothing rebuilt next frame)", model_refresh(built, donated) == [])
check("model: already-built exhibits are never re-initialised", built[3] and model_refresh(built, donated) == [])

sys.exit(1 if fails else 0)
