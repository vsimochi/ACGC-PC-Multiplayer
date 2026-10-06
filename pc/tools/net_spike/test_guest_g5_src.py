#!/usr/bin/env python3
"""test_guest_g5_src.py - GUESTS G5 (gameplay compatibility: player_no 4 safety + safe refusals) SOURCE AUDIT (no game process).

  A   G5.0 the three out-of-range accesses for the foreigner (player_no 4): the birthday read in update_schedule_today (m_event.c), the spirit-catch read in
      the pull-net include, the NULL now_home pointer maths in mTG_client_mail_is_mailbox (m_tag_ovl.c). Each guard exists, is TARGET_PC only and fires ONLY
      for the foreigner / a NULL home. Strongest pin: the working file with the pinned additions removed (m_tag_ovl.c: the pinned replacement undone) is
      BYTE-IDENTICAL to the baseline commit blob, i.e. every other byte of the vanilla function bodies (and files) is untouched.
  B   G5.1 refusals: a guest's MUSEUM_DONATE / MAIL_SEND are refused LOCALLY before any state is touched (nothing queued, nothing sent, pocket / letter
      untouched), only for a CLIENT guest; pc_net_game.c is purely additive (+15 lines) vs the baseline; resident paths are not touched (the guard tests the
      guest predicate only).
  C   facts the refusals / fixes rely on (read from the source, not changed): the shop catalog order is refused for EVERY client (and defended at order
      time), the mailbox is unusable for a guest (no house owned by its PersonalID), the host refuses kinds 8 / 12 / 13 for guest slots (NO_DONOR_SLOT,
      not journaled), no wire / protocol change.
  T   the new tests exist.
Tier: SOURCE AUDITED. Exit code 0 when all checks pass."""
import os
import re
import subprocess
import sys

import net_spike_lib as L
import test_guest_src as S
import wire_baseline

ROOT = S.ROOT
HERE = S.HERE
BASELINE = "e68c90c"   # G4 commit + docs (HEAD when G5 started)
G5_COMMIT = "8ad36ca"  # the G5 commit itself: the "diff vs baseline" pins below measure THE G5 CHANGE (baseline..G5 commit), so later milestones (G6) do not break them


def blob(rel):
    return subprocess.run(["git", "-C", ROOT, "show", BASELINE + ":" + rel], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def blob_at(rel, ref):
    return subprocess.run(["git", "-C", ROOT, "show", ref + ":" + rel], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def numstat_to_wt(rel, ref):
    """ref vs the WORKING TREE (--ignore-cr-at-eol): (added, removed)."""
    out = subprocess.run(["git", "-C", ROOT, "diff", "--numstat", "--ignore-cr-at-eol", ref, "--", rel], capture_output=True, check=True, timeout=60).stdout.decode().split()
    return (int(out[0]), int(out[1])) if out else (0, 0)


def numstat(rel):
    out = subprocess.run(["git", "-C", ROOT, "diff", "--numstat", BASELINE, G5_COMMIT, "--", rel], capture_output=True, check=True, timeout=60).stdout.decode().split()
    return (int(out[0]), int(out[1])) if out else (0, 0)


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)

    # ------------------------------------------------------------------ A
    ev_new, ev_old = S.read("src/game/m_event.c"), blob("src/game/m_event.c")
    ev_add = """#ifdef TARGET_PC
    /* G5.0: a guest is the foreigner (player_no 4): private_data[4] is out of range (it would read the field behind the array and could schedule a
     * bogus birthday); use the guest's own record (the bound passport) instead. */
    if (Common_Get(player_no) == mPr_FOREIGNER && Common_Get(now_private) != NULL) {
        priv = Common_Get(now_private);
    }
#endif

"""
    ck("A.a m_event.c: exactly the pinned 8-line guard is added (nothing removed); the file minus the guard is byte-identical to the baseline blob",
       numstat("src/game/m_event.c") == (8, 0) and ev_new.count(ev_add) == 1 and ev_new.replace(ev_add, "") == ev_old)
    fn = S.body(ev_new, S.functions(ev_new), "update_schedule_today")
    i_g = fn.index("#ifdef TARGET_PC")
    ck("A.a the guard sits inside update_schedule_today, AFTER the vanilla `priv = private_data[player_no]` init and BEFORE the first use of priv; it overrides priv only for player_no == mPr_FOREIGNER",
       fn.index("Private_c* priv = &Save_Get(private_data[Common_Get(player_no)]);") < i_g < fn.index("priv->birthday")
       and "Common_Get(player_no) == mPr_FOREIGNER" in fn[i_g:fn.index("#endif", i_g)])

    pn_new, pn_old = S.read("src/game/m_player_main_pull_net.c_inc"), blob("src/game/m_player_main_pull_net.c_inc")
    orig = """            int item_idx = mPr_GetPossessionItemIdxKindWithCond(Save_GetPointer(private_data[Common_Get(player_no)]),
                                                                ITM_SPIRIT0, ITM_SPIRIT4, FALSE);
"""
    ck("A.b pull-net include: the original 2-line vanilla read is kept verbatim in the #else branch; the file minus the TARGET_PC branch is byte-identical to the baseline blob (8 lines added, 0 removed)",
       numstat("src/game/m_player_main_pull_net.c_inc") == (8, 0) and pn_new.count(orig) == 1 and pn_old.count(orig) == 1
       and re.sub(r"#ifdef TARGET_PC\n.*?#else\n(.*?)#endif\n", r"\1", pn_new, count=1, flags=re.S) == pn_old)
    m = re.search(r"#ifdef TARGET_PC\n(.*?)#else\n", pn_new, re.S)
    ck("A.b the TARGET_PC branch reads Now_Private ONLY when player_no == mPr_FOREIGNER (and Now_Private is set), else the same private_data[player_no] pointer",
       m is not None and "Common_Get(player_no) == mPr_FOREIGNER && Common_Get(now_private) != NULL" in m.group(1) and "? Common_Get(now_private)" in m.group(1)
       and ": Save_GetPointer(private_data[Common_Get(player_no)])" in m.group(1))

    tg_new, tg_old = S.read("src/game/m_tag_ovl.c"), blob("src/game/m_tag_ovl.c")
    old_ret = """    return pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && mail != NULL && mail >= Common_Get(now_home)->mailbox &&
           mail < Common_Get(now_home)->mailbox + HOME_MAILBOX_SIZE;"""
    new_ret = """    /* G5.0: a guest (foreigner) has no home (now_home == NULL): its letters are never mailbox letters. */
    return pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && mail != NULL && Common_Get(now_home) != NULL &&
           mail >= Common_Get(now_home)->mailbox && mail < Common_Get(now_home)->mailbox + HOME_MAILBOX_SIZE;"""
    # G5 commit (8ad36ca, `git log -- src/game/m_tag_ovl.c`: the G5 change itself): THE G5 change is the one pinned return statement (+3 / -2)
    tg_g5 = blob_at("src/game/m_tag_ovl.c", G5_COMMIT)
    ck("A.c m_tag_ovl.c: the G5 change (baseline..%s) is ONLY the one pinned return statement (+3 / -2) gaining `Common_Get(now_home) != NULL` BEFORE the first dereference; undoing it in the G5 blob yields "
       "the baseline blob byte for byte; the worktree holds that guard exactly once" % G5_COMMIT,
       numstat("src/game/m_tag_ovl.c") == (3, 2) and tg_g5.count(new_ret) == 1 and tg_old.count(old_ret) == 1 and tg_g5.replace(new_ret, old_ret) == tg_old
       and new_ret.index("!= NULL") < new_ret.index("now_home)->mailbox") and tg_new.count(new_ret) == 1)
    # Later (8772e19, house-state-across-disconnect): 6 `#ifdef TARGET_PC` house-edit-lock refusals were ADDED to the pocket->room put procs (+48 / -0 vs the G5 blob). Pure additions only:
    # removing exactly those blocks from the working file must give the G5 blob byte for byte.
    lock_rx = re.compile(r"#ifdef TARGET_PC\n    /\*[^\n]*\*/\n    if \(pc_net_game_client_room_edit_locked\(\)\) \{\n(?:        [^\n]*\n)*?    \}\n#endif\n")
    lock_blocks = [mm.group(0) for mm in lock_rx.finditer(tg_new)]
    ck("A.c m_tag_ovl.c vs the G5 blob: ONLY pure additions of %d `#ifdef TARGET_PC` house edit-lock refusal blocks (each: pc_net_game_client_room_edit_locked() -> pc_net_game_room_edit_denied() -> "
       "warning window -> return), +48 / -0; the worktree minus those blocks IS the G5 blob" % 6,
       numstat_to_wt("src/game/m_tag_ovl.c", G5_COMMIT) == (48, 0) and len(lock_blocks) == 6
       and all("pc_net_game_room_edit_denied();" in b_ and "return;" in b_ for b_ in lock_blocks) and lock_rx.sub("", tg_new) == tg_g5)
    k = tg_new.index("static int mTG_client_mail_is_mailbox")
    ck("A.c the function is TARGET_PC-only (inside the existing #ifdef TARGET_PC block of the mail milestone 2 helpers)",
       tg_new.rfind("#ifdef TARGET_PC", 0, k) > tg_new.rfind("#endif", 0, k))

    # ------------------------------------------------------------------ B
    ng_new = S.read("pc/src/pc_net_game.c")
    ck("B pc_net_game.c is purely additive vs the baseline (+15 / -0)", numstat("pc/src/pc_net_game.c") == (15, 0))
    ng = S.mask(ng_new)
    nf = S.functions(ng)
    mus = S.body(ng, nf, "pc_net_game_ts_begin_museum_donate")
    ml = S.body(ng, nf, "pc_net_game_mail_begin_send")
    ck("B.1 MUSEUM_DONATE: a CLIENT guest is refused locally (return 0) BEFORE pcnetgame_ts_begin (no transaction queued, nothing sent); the vanilla call is the unchanged last statement",
       "s_role == PC_NETGAME_ROLE_CLIENT && pcnetgame_client_is_guest_player()" in mus and mus.index("return 0;") < mus.index("return pcnetgame_ts_begin(")
       and mus.index("pcnetgame_client_is_guest_player()") < mus.index("return pcnetgame_ts_begin(")
       and mus.rstrip().endswith("return pcnetgame_ts_begin((uint8_t)PC_NETGAME_TXN_KIND_MUSEUM_DONATE, pocket_slot, 0, item, 0);"))
    ck("B.2 MAIL_SEND: the guest refusal comes right after the role / READY / Now_Private test and BEFORE s_mail_op is touched (no state, letter stays); resident clients skip it",
       "pcnetgame_client_is_guest_player()" in ml
       and ml.index("s_role != PC_NETGAME_ROLE_CLIENT") < ml.index("pcnetgame_client_is_guest_player()") < ml.index("memset(&s_mail_op")
       and ml.index("pcnetgame_client_is_guest_player()") < ml.index("s_mail_op.active = 1"))
    pred = S.body(ng, nf, "pcnetgame_client_is_guest_player")
    ck("B the guest predicate is the existing one, unchanged (Now_Private set and player_no >= mPr_FOREIGNER): a resident (player_no 0..3) never matches",
       "(int)Common_Get(player_no) >= (int)mPr_FOREIGNER" in pred and pred == S.body(S.mask(blob("pc/src/pc_net_game.c")), S.functions(S.mask(blob("pc/src/pc_net_game.c"))),
                                                                                   "pcnetgame_client_is_guest_player"))
    ck("B the refusals log the reason (a console line); the in-game answer is the EXISTING give-back row (curator row 2) / 'no such address' row (post girl row 1)",
       "MUSEUM_DONATE refused locally: a guest cannot donate" in ng_new and "MAIL_SEND refused locally: a guest cannot send letters to players" in ng_new)
    cur = S.read("src/actor/npc/ac_npc_curator_move.c_inc")
    post = S.read("src/actor/npc/ac_npc_post_girl.c_inc")
    ck("B the callers map a 0 return onto the existing refusal paths: curator ts_begin == 0 -> act_idx = 2 (give back, item stays); post girl begin_send == 0 -> row 1 (letter handed back)",
       re.search(r"aCR_ts_waiting = \(ts_begin > 0\) \? TRUE : FALSE;\s+if \(!aCR_ts_waiting\) \{\s+act_idx = 2;", cur) is not None
       and re.search(r"r = pc_net_game_mail_begin_send\(item_p->slot_no\);\s+if \(r < 0\) \{\s+return -1;\s+\}\s+if \(r == 0\) \{\s+return 1;", post) is not None)

    # ------------------------------------------------------------------ C
    shop = S.read("src/actor/npc/ac_npc_shop_common.c")
    ck("C catalog ordering is refused for EVERY network client (guests included) with Nook's ORDER_UNAVAILABLE row before any bell is taken, and defended again at order time (ORDER_CANCEL, no order slot written)",
       re.search(r"if \(aNSC_PC_IS_CLIENT\(\)\) \{[^}]*action = 1;", shop, re.S) is not None
       and re.search(r"if \(aNSC_PC_IS_CLIENT\(\)\) \{[^}]*msg_no = aNSC_MSG_ORDER_CANCEL;\s+\} else if \(aNSC_money_check\(price\) == FALSE\)", shop, re.S) is not None
       and shop.index("aNSC_set_ftr_order(shop_common);", shop.index("static void aNSC_order_check")) > shop.index("msg_no = aNSC_MSG_ORDER_CANCEL;", shop.index("static void aNSC_order_check")))
    ck("C the mailbox is closed to a guest: usable() needs a house owned by the client's PersonalID, and pcnetgame_mbox_house_of() walks homes[] only (a guest owns none) -> -1",
       "pcnetgame_mbox_own_house() >= 0" in S.body(ng, nf, "pc_net_game_client_mailbox_usable") and "Save_Get(homes[i]).ownerID" in S.body(ng, nf, "pcnetgame_mbox_house_of"))
    ck("C the host refuses MUSEUM_DONATE / MAIL_SEND / MAIL_TAKE for guest slots with NO_DONOR_SLOT (existing, unchanged): the guest reasons are in the source",
       "has no museum donor slot" in ng_new and "a guest / extra player has no house: sending mail is refused" in ng_new
       and "has no house and no mailbox: taking mail is refused" in ng_new)
    ck("C no wire / protocol change by G5: message ids 1..66 (66 = M-F promotion handoff) (59..61 = furniture sync, 62..65 = town transfer), pc_net_game.h / pc_net.c / pc_net.h unchanged",
       sorted(dict(wire_baseline.c_message_ids(ng_new)).values()) == list(range(1, wire_baseline.EXPECTED_MAX_MSG_ID + 1)) and numstat("pc/include/pc_net_game.h") == (0, 0)
       and numstat("pc/src/pc_net.c") == (0, 0) and numstat("pc/include/pc_net.h") == (0, 0))
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    ck("C wire_baseline: %d checks, all green" % len(wb), wb and all(c for _d, c in wb))
    changed = sorted(subprocess.run(["git", "-C", ROOT, "diff", "--name-only", BASELINE, G5_COMMIT, "--", "src", "pc/src", "pc/include", "include"], capture_output=True, check=True,
                                    timeout=60).stdout.decode().split())
    ck("C resident / single-player byte identity: the only touched source files are the four above (%s)" % changed,
       changed == ["pc/src/pc_net_game.c", "src/game/m_event.c", "src/game/m_player_main_pull_net.c_inc", "src/game/m_tag_ovl.c"])

    # ------------------------------------------------------------------ T
    ck("T test_guest_g5_protocol.py exists", os.path.isfile(os.path.join(HERE, "test_guest_g5_protocol.py")))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
