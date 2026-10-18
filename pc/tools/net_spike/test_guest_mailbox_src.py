#!/usr/bin/env python3
"""test_guest_mailbox_src.py - SOURCE AUDIT of the guest mailbox ("Guest mailbox via the post office", docs/multiplayer-guest-roadmap.md).

TIER: source audit (+ the native guest store selftest and the PC message selftest are run by their own harnesses: guest_store_selftest.c via test_capacity_phase3.py /
this file's --native switch, pc_nook_house_selftest via test_work_menu_patch.py). No game process.
  A  host code never indexes Save_t private_data[] / homes[] with a GUEST record index: the MAIL_SEND / MAIL_TAKE handlers read the bound record through
     pcnetgame_rec_priv_ptr only; every guest-mailbox host function is free of private_data / homes; s_guest_mbox is indexed by a range-checked guest slot
  B the per-guest store v4: version constants, the extension block written only for a non-empty mailbox, the digest folds the mailbox, the downgrade healing
  C MAIL_TAKE guest branch: the .gst file is written DURABLY before the journal APPLIED / the RESULT; a failed write undoes both halves
  D the client: MAILBOX_LETTER house 0xFF only for a guest (an older client refuses it: the house test), a separate copy (never homes[]); the overlays use ONE helper
  E the post office girl: armed rows, the choice mapping before the vanilla switch, the new actions sized by aPG_ACTION_NUM, disarm at the talk end
  F villager replies: mNpc_RemailFor byte-identical to HEAD, the sink variant, the guest pass with a growable per-slot day
  G promotion: the guest mailbox migrates inside pc_mp_promote_create (after the house assignment), the post office is in the rollback snapshot
  H the switches / limit and the wire: reason 38, no new message id, MAILBOX_LETTER layout unchanged
Usage: python test_guest_mailbox_src.py [--native]
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
import wire_baseline  # noqa: E402

PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))


# The guest mailbox is now COMMITTED, so wire_baseline._git_show (HEAD) already contains it. "What an OLDER client does" must be judged against the last commit WITHOUT it:
PRE_GUEST_MAILBOX_REF = "1adac30"  # "Guest museum donation, and Blathers dialogue race fix" (the commit before "Guest mailbox via the post office")


def git_show_pre_guest_mailbox(rel):
    return subprocess.run(["git", "-C", ROOT, "show", PRE_GUEST_MAILBOX_REF + ":" + rel], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def func_body(src, name):
    m = re.search(r"^[A-Za-z_][^\n;{}]*\b%s\([^;{]*\)\s*\{" % re.escape(name), src, re.M)
    if not m:
        return ""
    i = m.end()
    depth = 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[m.start():i]


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    c_raw = read("pc/src/pc_net_game.c")
    c = strip_comments(c_raw)
    gs = read("pc/src/pc_mp_guest_store.c")
    gsh = read("pc/include/pc_mp_guest_store.h")
    mc = read("pc/src/pc_m_card.c")
    npc = read("src/game/m_npc.c")
    tag = read("src/game/m_tag_ovl.c")
    mbx = read("src/game/m_mailbox_ovl.c")
    pg = read("src/actor/npc/ac_npc_post_girl.c_inc")
    pgh = read("include/ac_npc_post_girl.h")
    nk = read("pc/src/pc_nook_house.c")
    gmh = read("pc/include/pc_guest_mail.h")
    tm = read("src/game/m_trademark.c")

    # ------------------------------------------------------------------ A
    send = strip_comments(func_body(c_raw, "pcnetgame_handle_host_mail_txn"))
    take = strip_comments(func_body(c_raw, "pcnetgame_handle_host_mail_take_txn"))
    ck("A MAIL_SEND / MAIL_TAKE: no Save_Get(private_data)[...] subscript at all; the bound record comes from pcnetgame_rec_priv_ptr(idx) (resident or guest)",
       send and take and "private_data" not in send and "private_data" not in take and "mir = pcnetgame_rec_priv_ptr(idx);" in send and "mir = pcnetgame_rec_priv_ptr(idx);" in take)
    gsw = "if (idx >= PLAYER_NUM && (gslot < 0 || !pcnetgame_guest_mail_enabled())) {"
    ck("A the guest switch / entry check precedes the first homes[] subscript of both handlers; homes[] is read only on RESIDENT paths (gslot < 0 / the recipient house rh)",
       gsw in send and gsw in take and send.index(gsw) < send.index("Save_Get(homes[") and take.index(gsw) < take.index("Save_Get(homes[")
       and "if (gslot < 0) {" in send and send.index("if (gslot < 0) {") < send.index("Save_Get(homes[i]).ownerID")
       and "L = gslot >= 0 ? &s_guest_mbox[gslot].box[t->slot] : &Save_Get(homes[h]).mailbox[t->slot];" in take
       and "mMl_clear_mail(&Save_Get(homes[h]).mailbox[t->slot]);" in take[take.index("} else {", take.index("if (gslot >= 0) {\n")):])
    host_fns = ["pcnetgame_guest_mbox_reset", "pcnetgame_guest_mbox_from_record", "pcnetgame_guest_mbox_to_record", "pcnetgame_guest_mail_left_today", "pcnetgame_guest_mail_count_send",
                "pcnetgame_guest_mbox_deliver", "pcnetgame_guest_mbox_refresh", "pcnetgame_guest_mbox_send", "pcnetgame_host_guest_mbox_push", "pcnetgame_guest_remail_sink",
                "pcnetgame_host_guest_remail_tick", "pcnetgame_guest_mail_test_hooks", "pcnetgame_guest_of_idx"]
    bodies = {n: strip_comments(func_body(c_raw, n)) for n in host_fns}
    ck("A every guest-mailbox HOST function exists and never names private_data / homes[ (%d functions)" % len(host_fns),
       all(bodies[n] for n in host_fns) and not any("private_data" in b or "homes[" in b for b in bodies.values()))
    ck("A s_guest_mbox is indexed only by a range-checked guest slot (g < s_guest_cap && s_guest[g].used, the binding's bound_guest_slot or pcnetgame_guest_of_idx)",
       "g < 0 || g >= s_guest_cap || !s_guest[g].used" in bodies["pcnetgame_guest_mbox_refresh"] and "g < 0 || g >= s_guest_cap || !s_guest[g].used" in bodies["pcnetgame_host_guest_mbox_push"]
       and "g < 0 || g >= s_guest_cap || !s_guest[g].used" in bodies["pcnetgame_host_guest_remail_tick"] and "g < s_guest_cap && s_guest[g].used" in bodies["pcnetgame_guest_of_idx"]
       and "g < 0 || g >= s_guest_cap || !s_guest[g].used" in bodies["pcnetgame_guest_mbox_deliver"])
    ck("A the mailbox table GROWS with the guest store (PC_GROW_TABLE + s_guest_tables entry + the memory budget per slot), never PLAYER_NUM sized",
       "PC_GROW_TABLE(PCNetGameGuestMbox, s_guest_mbox, 0, PCNG_GUEST_INLINE)" in c and "PC_GROW_TABLE_ENTRY(s_guest_mbox, 0)" in c and "sizeof(PCNetGameGuestMbox)" in func_body(c_raw, "pcnetgame_guest_slot_bytes"))
    ck("A the guest record of the reply pass comes from THE accessor (pcnetgame_rec_priv_ptr(PLAYER_NUM + g)), never s_guest_rec[] directly",
       "mNpc_RemailForSink(pcnetgame_rec_priv_ptr(PLAYER_NUM + g), pcnetgame_guest_remail_sink, &c);" in bodies["pcnetgame_host_guest_remail_tick"] and "s_guest_rec" not in bodies["pcnetgame_host_guest_remail_tick"])
    ck("A a new occupant of a guest slot starts with an EMPTY mailbox (create resets it; the failed-create rollback restores the previous one); the load installs the file's",
       "pcnetgame_guest_mbox_reset(g);" in func_body(c_raw, "pcnetgame_guest_create") and "s_guest_mbox[g] = old_mbox;" in func_body(c_raw, "pcnetgame_guest_create")
       and "pcnetgame_guest_mbox_from_record(g, rec);" in func_body(c_raw, "pcnetgame_guest_load_visit"))

    # ------------------------------------------------------------------ B
    ck("B store: version 3 = no block, 4 = the block (ext_len 2988, ext_version 1, mask, 10 x 298), the v4 file is 12396 bytes, FILE_MAX sizes every buffer",
       "#define PC_MP_GS_VERSION_V3  3u" in gsh and "#define PC_MP_GS_VERSION     4u" in gsh and "#define PC_MP_GS_EXT_LEN     (4 + 4 + PC_MP_GS_MBOX_SLOTS * PC_MP_GS_MAIL_SIZE)" in gsh
       and "static uint8_t buf[PC_MP_GS_FILE_MAX + 1];" in func_body(gs, "read_and_parse") and "static uint8_t buf[PC_MP_GS_FILE_MAX];" in func_body(gs, "pc_mp_gs_save"))
    ser = func_body(gs, "pc_mp_gs_serialize")
    par = func_body(gs, "pc_mp_gs_parse")
    ck("B serialize writes v3 for an EMPTY mailbox (an older build still reads it) and v4 otherwise; it refuses what parse would refuse (mask bits, used / unused slot bytes)",
       "total == PC_MP_GS_FILE_SIZE ? PC_MP_GS_VERSION_V3 : PC_MP_GS_VERSION" in ser and "(r->mbox_mask & ~PC_MP_GS_MBOX_MASK) != 0" in ser and "used == all_zero(r->mbox[i], PC_MP_GS_MAIL_SIZE)" in ser)
    ck("B parse: older than 3 OLD, newer than 4 FUTURE, an unknown block version FUTURE, a wrong block length SHAPE, the CRC covers the block, mask / slot rules FIELD",
       "ver < PC_MP_GS_VERSION_V3" in par and "ver > PC_MP_GS_VERSION" in par and "!= PC_MP_GS_EXT_VERSION) {\n            return PC_MP_GST_ERR_VERSION_FUTURE;" in par
       and "!= PC_MP_GS_EXT_LEN) {\n            return PC_MP_GST_ERR_SHAPE;" in par and "pc_mp_records_crc32(buf, total - 4)" in par and "used == all_zero(L, PC_MP_GS_MAIL_SIZE)" in par)
    wr = func_body(c_raw, "pcnetgame_guest_store_write")
    ck("B the game writes the mailbox IN the guest's file and its digest decides the rewrite (pc_mp_gs_ext_digest over the entry digest); a v3 guest keeps its digest",
       "pcnetgame_guest_mbox_to_record(g, &rec);" in wr and "dg = pc_mp_gs_ext_digest(&rec, dg);" in wr and wr.index("dg = pc_mp_gs_entry_digest(&rec.e);") < wr.index("dg = pc_mp_gs_ext_digest(&rec, dg);")
       and "return entry_digest;" in func_body(gs, "pc_mp_gs_ext_digest"))
    ld = func_body(gs, "pc_mp_gs_load")
    ck("B downgrade healing: a preserved *.corrupt-* file of a guest with NO readable generation that parses cleanly now is COPIED back as the primary (kept, never deleted), counted, loaded",
       "copy_file_new(src, prim)" in ld and "inf->restored++;" in ld and "remove(" not in func_body(gs, "copy_file_new").replace("remove(tmp)", "") and "RESTORED guest file" in ld)

    # ------------------------------------------------------------------ C
    gtake = take[take.index("if (gslot >= 0) {\n"):]
    ck("C MAIL_TAKE guest commit: mail[dst] + the mailbox slot change in memory, the guest file is written DURABLY, THEN the journal APPLIED / the RESULT; a failed write puts both back "
       "(through the sanctioned writer) and answers REJECTED BUSY",
       'pcnetgame_guest_store_write("guest mailbox take")' in gtake and gtake.index('pcnetgame_guest_store_write("guest mailbox take")') < gtake.index("PC_NETGAME_TXN_OUTCOME_APPLIED")
       and "pcnetgame_rec_txn_write_mail(idx, dst, &old_dst);" in gtake and "memcpy(&s_guest_mbox[gslot].box[t->slot], &W, sizeof(W));" in gtake
       and "PC_NETGAME_TXN_REASON_BUSY, 1, NULL" in gtake and gtake.index("PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT") > gtake.index('pcnetgame_guest_store_write("guest mailbox take")'))

    # ------------------------------------------------------------------ D
    ap = func_body(c_raw, "pcnetgame_mbox_client_apply")
    ck("D the client takes house 0xFF ONLY as a guest (else refused) into its SEPARATE copy s_gmbox_view; a resident-house letter is refused for a guest; nothing writes homes[] for a guest",
       "guest_box = m->house == (uint8_t)PC_NETGAME_MBOX_HOUSE_GUEST" in ap and "a guest mailbox letter, but this player is not a guest" in ap and "tgt = s_gmbox_view;" in ap
       and "pcnetgame_client_is_guest_player()) {" in ap and "return h >= 0 ? Save_Get(homes[h]).mailbox : NULL;" in func_body(c_raw, "pcnetgame_mbox_local_target"))
    ck("D an OLDER client refuses a house-0xFF slot harmlessly (HEAD's apply: house must equal the owned house; a guest owns none -> refused, the local homes[] untouched because h < 0)",
       "if (h < 0 || (int)m->house != h) {" in git_show_pre_guest_mailbox("pc/src/pc_net_game.c") and "if (h >= 0) {\n        mMl_clear_mail(&Save_Get(homes[h]).mailbox[i]);" in git_show_pre_guest_mailbox("pc/src/pc_net_game.c"))
    uim = func_body(c_raw, "pc_ui_mailbox")
    ck("D THE helper pc_ui_mailbox(): the guest copy for a guest client, else Common_Get(now_home)->mailbox or NULL (now_home is never faked)",
       "return s_gmbox_view;" in uim and "Common_Get(now_home) != NULL ? Common_Get(now_home)->mailbox : NULL" in uim and "now_home) =" not in c and "Common_Set(now_home" not in c)
    ck("D every mailbox use of m_mailbox_ovl.c / m_tag_ovl.c goes through the one helper (no Common_Get(now_home)->mailbox outside the non-PC macro), with NULL guards",
       tag.count("Common_Get(now_home)->mailbox") == 1 and "#define mTG_MAILBOX() (Common_Get(now_home)->mailbox)" in tag and "#define mTG_MAILBOX() pc_ui_mailbox()" in tag
       and mbx.count("Common_Get(now_home)->mailbox") == 1 and "#define mMB_MAILBOX() (Common_Get(now_home)->mailbox)" in mbx and "#define mMB_MAILBOX() pc_ui_mailbox()" in mbx
       and "if (mMB_MAILBOX() == NULL) {" in mbx and "if (mail == NULL) {" in mbx and "if (mTG_MAILBOX() == NULL) {" in tag and tag.count("mTG_MAILBOX() != NULL") >= 4)
    ck("D the overlay opened at the post office requests NO mail-jump landing at its close (pc_ui_mailbox_is_guest); the house mailbox keeps the vanilla request",
       "if (!pc_ui_mailbox_is_guest())\n#endif\n    mPlib_request_main_mail_land_from_submenu();" in mbx)
    ck("D the take machinery serves the guest copy through the same transaction (own-mailbox predicate); the resident predicate is unchanged for residents",
       "!pcnetgame_client_own_mailbox_usable()" in func_body(c_raw, "pc_net_game_mail_take_step") and "pc_net_game_client_mailbox_usable() || pcnetgame_client_guest_mailbox_usable()" in c
       and "mMl_clear_mail(&tgt[t->slot]);" in func_body(c_raw, "pcnetgame_txn_apply_take"))
    ck("D the client MAIL_SEND guard for guests is gone (the host decides; an older host answers NO_DONOR_SLOT -> the post girl's row 1, the letter stays)",
       "pcnetgame_client_is_guest_player()" not in func_body(c_raw, "pc_net_game_mail_begin_send"))

    # ------------------------------------------------------------------ E
    ask = func_body(pg, "aPG_ask_for_business")
    ck("E the greeting row and the 'anything else' row are armed for a READY guest only (else disarmed: residents / host / solo untouched); disarmed at the talk end",
       "pc_post_mail_msg_arm(pc_net_game_guest_mail_offer_available() ? msg : -1);" in func_body(pg, "aPG_set_talk_info")
       and "pc_post_mail_msg_arm(pc_net_game_guest_mail_offer_available() ? msg_no[postgirl->status] + postgirl->npc_class.draw.draw_type : -1);" in func_body(pg, "aPG_repay_after_init")
       and "pc_post_mail_msg_arm(-1);" in func_body(pg, "aPG_talk_end_chk"))
    ck("E the choice mapping runs BEFORE the vanilla switch: 'Check my mail.' -> aPG_pc_check_mail, later choices shift down by one (the vanilla mapping sees its own indices)",
       "pc_post_mail_more_choice(mMsg_Get_msg_num(mMsg_Get_base_window_p()))" in ask and ask.index("aPG_pc_check_mail(postgirl, play);") < ask.index("switch (postgirl->status)")
       and "choice--;" in ask)
    ck("E the new actions: close the window -> open the VANILLA mailbox overlay (mSM_IV_OPEN_MAILBOX) -> the closing row; both tables sized by aPG_ACTION_NUM",
       "aPG_ACTION_PC_MAIL_WIN_CLOSE_WAIT," in pgh and "aPG_ACTION_NUM" in pgh and "static aPG_INIT_PROC init_proc[aPG_ACTION_NUM]" in pg and "static aPG_PROC process[aPG_ACTION_NUM]" in pg
       and "mSM_open_submenu(&play->submenu, mSM_OVL_INVENTORY, mSM_IV_OPEN_MAILBOX, 0x0);" in pg and "PC_NOOK_MSG_POST_DONE" in pg and "PC_NOOK_MSG_POST_NOMAIL" in pg)
    ck("E the offer needs a READY guest whose post office mailbox the host FED (an older host / the switch off never feeds it: no row)",
       "return pcnetgame_client_guest_mailbox_usable() && Now_Private != NULL;" in func_body(c_raw, "pc_net_game_guest_mail_offer_available"))
    ck("E the text: 'Check my mail.' (<= 16 characters), two closing rows ending with MSGEND, inside the PC message table (COUNT 41); the patch inserts before the cancel choice",
       'snprintf(tmp, sizeof(tmp), "Check my mail.");' in nk and "#define PC_NOOK_MSG_COUNT 55" in read("pc/include/pc_nook_house.h")  # worker C: + the 4 house-upgrade rows (41..44); worker E: + the 7 event NPC rows (45..51)  # + the Nook bulk sell basket quote row (52)
       and "PC_NOOK_SEL_CHECK_MAIL" in func_body(nk, "nk_post_mail_patch") and "s_pm_choice = k - 1;" in func_body(nk, "nk_post_mail_patch"))
    ck("E the daily limit maps onto the post girl's 'can't take more' row (3), never 'no such address'", "case PC_GUEST_MAIL_REJECT_LIMIT:" in pg and "#define PC_GUEST_MAIL_REJECT_LIMIT 38" in gmh)

    # ------------------------------------------------------------------ F
    head_npc = wire_baseline._git_show(ROOT, "src/game/m_npc.c")
    ck("F mNpc_RemailFor (the resident post office sink) is byte-identical to HEAD; mNpc_RemailForSink is the guest variant (send_reply cleared only when the sink took the letter, a refusal stops the pass)",
       func_body(npc, "mNpc_RemailFor") == func_body(head_npc, "mNpc_RemailFor") and "if ((*sink)(mail, ctx) != TRUE) {" in func_body(npc, "mNpc_RemailForSink")
       and func_body(npc, "mNpc_RemailForSink").index("(*sink)(mail, ctx)") < func_body(npc, "mNpc_RemailForSink").index("memory->letter_info.send_reply = FALSE;"))
    rt = bodies["pcnetgame_host_guest_remail_tick"]
    ck("F the guest pass: host + switch on, 1 Hz, READY bound SYNCED GUEST peers, once per host day per guest slot (reply_day in the growable table), the store written after a delivery",
       "!pcnetgame_guest_mail_enabled()" in rt and "st->bound_class != (uint8_t)PC_NETGAME_REC_CLASS_GUEST" in rt and "s_guest_mbox[g].reply_day = today;" in rt
       and 'pcnetgame_guest_store_write("villager reply to a guest")' in rt and "pcnetgame_host_guest_remail_tick(); /* guest mailbox" in c_raw)

    # ------------------------------------------------------------------ G
    pcr = func_body(mc, "pc_mp_promote_create")
    ck("G promotion: the guest mailbox (handed over one-shot) migrates INSIDE pc_mp_promote_create after the house assignment and the record migration; a letter that fits nowhere rolls "
       "the whole promotion back (never dropped)",
       "pc_mp_promote_migrate_mailbox(gkey, priv, home, s_pc_promote_gmbox, gmbox_n, &th, &tp, &tm);" in pcr and pcr.index("pc_residence_assign(slot, house)") < pcr.index("pc_mp_promote_migrate_mailbox(")
       and pcr.index("remapped = pc_mp_promote_migrate(gkey, g, priv);") < pcr.index("pc_mp_promote_migrate_mailbox(") and "s_pc_promote_gmbox_n = 0;" in pcr
       and "pc_mp_promote_rollback();\n            PC_PROMOTE_FAIL(" in pcr)
    mm = func_body(mc, "pc_mp_promote_migrate_mailbox")
    ck("G each letter: recipient / sender re-keyed (pc_pid_rekey), an illegal gift cleared, then the house mailbox, else the post office, else the carried letters, else -1",
       mm.count("pc_pid_rekey(") == 2 and "pc_net_game_pocket_legal_item(L.present)" in mm and mm.index("home->mailbox") < mm.index("mPO_receipt_proc(") < mm.index("priv->mail")
       and "return -1;" in mm)
    ck("G the rollback snapshot holds the post office (the house mailbox is part of `home`)", "PostOffice_c post_office;" in mc and "s_pc_promote_snap.post_office = Save_Get(post_office);" in mc
       and "Save_Get(post_office) = s_pc_promote_snap.post_office;" in func_body(mc, "pc_mp_promote_rollback"))
    pe = c_raw[c_raw.index("static int pcnetgame_promote_exec(const char* gsel, int gfixed, const char* ssel, const char* hsel, int confirm, unsigned flags, PCNetPeerId req_peer, char* msg, size_t cap,\n                                  struct pcng_promote_out* out) {"):]
    ck("G promote_exec hands the guest's mailbox over right before pc_mp_promote_create (console promote AND the paid purchase share it); the guest table keeps its copy until the entry is retired",
       pe.index("pc_mp_promote_set_guest_mailbox(s_guest_mbox[g].box, (int)PC_NETGAME_MBOX_SLOTS);") < pe.index("if (!pc_mp_promote_create(&s_guest_rec[g]"))
    ck("G pre-existing host bug fixed (found by the promotion test): trademark_cleanup no longer clears the house mailboxes of a LOADED PC save (the dedicated observer starts from it)",
       "if (!pc_save_loaded) {\n                mMl_clear_mail_box(home->mailbox, HOME_MAILBOX_SIZE);" in tm and "#else\n        mMl_clear_mail_box(home->mailbox, HOME_MAILBOX_SIZE);\n#endif" in tm)

    # ------------------------------------------------------------------ H
    ck("H switches: AC_MP_GUEST_MAIL=0 (read once, default ON) gates send / take / the push / the replies; AC_MP_GUEST_MAIL_PER_DAY (1..100, default 5)",
       'getenv("AC_MP_GUEST_MAIL")' in c_raw and 'getenv("AC_MP_GUEST_MAIL_PER_DAY")' in c_raw and "#define PC_NETGAME_GUEST_MAIL_PER_DAY  5" in c_raw
       and c.count("pcnetgame_guest_mail_enabled()") >= 4 and "PC_NETGAME_TXN_REASON_MAIL_LIMIT" in send)
    ck("H wire: reason 38 MAIL_LIMIT (C + python), the guest mailbox rides MAILBOX_LETTER (56) with house 0xFF (no new message id, the 316-byte layout unchanged)",
       "#define PC_NETGAME_TXN_REASON_MAIL_LIMIT 38u" in c_raw and L.PC_NETGAME_TXN_REASON_MAIL_LIMIT == 38 and L.TXN_REASON_NAMES.get(38) == "MAIL_LIMIT"
       and "#define PC_NETGAME_MBOX_HOUSE_GUEST    0xFFu" in c_raw and "_Static_assert(sizeof(PCNetGameMailboxLetterMsg) == 316," in c_raw and "PC_NETGAME_MSG_GUEST" not in c_raw)
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    ck("H wire_baseline: %d checks, all green" % len(wb), wb and all(x for _d, x in wb))
    ck("H the test hooks are TEST-ONLY: guarded by PC_NET_TEST_HOOKS and read through pc_test_hook_getenv (AC_TEST_HOOKS=1)",
       "#ifdef PC_NET_TEST_HOOKS" in bodies["pcnetgame_guest_mail_test_hooks"] or "#ifdef PC_NET_TEST_HOOKS" in func_body(c_raw, "pcnetgame_guest_mail_test_hooks")
       and 'pc_test_hook_getenv("AC_TEST_GUEST_MAIL_SEED")' in c_raw and 'getenv("AC_TEST_GUEST_MAIL' not in c_raw.replace('pc_test_hook_getenv("AC_TEST_GUEST_MAIL', ""))

    if "--native" in sys.argv:
        cc = os.environ.get("CC", "gcc")
        exe = os.path.join(os.environ.get("TEMP", "."), "gst_selftest_gm.exe")
        r = subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include"), os.path.join(HERE, "guest_store_selftest.c")]
                           + [os.path.join(PC, "src", f) for f in ("pc_mp_guest_store.c", "pc_mp_guests.c", "pc_mp_membership.c", "pc_mp_records.c")] + ["-o", exe], capture_output=True, text=True)
        ck("N guest_store_selftest compiled warning-free", r.returncode == 0 and r.stderr.strip() == "")
        if r.returncode == 0:
            d = os.path.join(os.environ.get("TEMP", "."), "gst_selftest_gm_run")
            import shutil
            shutil.rmtree(d, ignore_errors=True)  # the selftest expects a fresh directory (a previous run's stores make its migration group fail)
            p = subprocess.run([exe, d], capture_output=True, text=True, timeout=600)
            m = re.search(r"RESULT passed=(\d+) failed=(\d+)", p.stdout)
            ck("N guest_store_selftest (incl. the v4 group: round trip, v3 as empty, block rules, CRC, rotation, downgrade healing): %s" % (m.group(0) if m else "no RESULT"),
               p.returncode == 0 and m is not None and m.group(2) == "0" and "downgrade healing" in p.stdout)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
