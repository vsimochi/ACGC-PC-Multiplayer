#!/usr/bin/env python3
"""test_guest_letters_src.py - SOURCE AUDIT (+ a native unit test) of LETTERS TO GUESTS: the guest directory, player -> guest letters, villager event letters and the
museum completion letter for guests (docs/multiplayer-guest-roadmap.md "Letters to guests").

TIER: source audit + (--native) the pure date rule pc_guest_event_due.h compiled with gcc and run. No game process.
  I  the GUEST DIRECTORY: TOWN_SVC_STATE service 7 (324 B, array bound 8), built from the guest table with the deliverability rule, not built with the switch off; the
     client validates + keeps it (no save needed); HEAD's client ignores an unknown service (an older client is unaffected)
  J  the address book (one page: residents, guests, the museum last) and the post office girl (client: the directory is an address; the host's own player: local delivery)
  K  MAIL_SEND to a GUEST: the recipient rule, NO_SUCH_ADDRESS / self / MAILBOX_FULL / MAIL_LIMIT before any mutation, the guest sender's durable write before the journal with
     a full rollback, the resident sender's letter riding the next host save, the push
  L  villager EVENT letters: the vanilla senders unchanged, the window handed over BEFORE the cursor moves, Valentine after the vanilla pass, the departure copied before
     the villager is cleared, the guest builders reuse the vanilla builders, no Private_c / homes[] / post office touched
  M  the museum letter: the host-owned state_flags bits set only when the letter was taken, the host-field refresh + push, one durable write
  N  switches / wire: AC_MP_GUEST_MAIL=0 gates the directory, the recipient, the events, the address list; the test hook is TEST-ONLY; wire_baseline green
Usage: python test_guest_letters_src.py [--native]
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
import wire_baseline  # noqa: E402
from test_guest_mailbox_src import read, strip_comments, func_body, ROOT, PC, git_show_pre_guest_mailbox  # noqa: E402


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    c_raw = read("pc/src/pc_net_game.c")
    c = strip_comments(c_raw)
    head_c = git_show_pre_guest_mailbox("pc/src/pc_net_game.c")  # the last commit WITHOUT the guest mailbox / guest letters (HEAD itself now has them)
    npc = read("src/game/m_npc.c")
    head_npc = wire_baseline._git_show(ROOT, "src/game/m_npc.c").replace("\r\n", "\n")
    ev = read("src/actor/ac_event_manager.c")
    msm = read("src/game/m_museum.c")
    head_msm = wire_baseline._git_show(ROOT, "src/game/m_museum.c").replace("\r\n", "\n")
    adr = read("src/game/m_address_ovl.c")
    adrh = read("include/m_address_ovl.h")
    pg = read("src/actor/npc/ac_npc_post_girl.c_inc")
    gmh = read("pc/include/pc_guest_mail.h")
    due = read("pc/include/pc_guest_event_due.h")

    # ------------------------------------------------------------------ I
    ck("I service 7 GUEST_DIR (324 = 4 + 16 x 20), the array bound 8, the size asserted against the blob max and sizeof(PersonalID_c)",
       "#define PC_NETGAME_TS_GUESTDIR 7u" in c_raw and "#define PC_NETGAME_TS_NUM      9u" in c_raw  # worker B re-pin: service 8 MUSEUM_DONORS raised the bound to 9
       and "#define PC_NETGAME_GUESTDIR_MAX   16u" in c_raw
       and "_Static_assert(PC_NETGAME_TS_GUESTDIR_LEN == 324u && PC_NETGAME_TS_GUESTDIR_LEN <= PC_NETGAME_TS_BLOB_MAX && sizeof(PersonalID_c) == 20u," in c_raw
       and L.PC_NETGAME_TS_GUESTDIR == 7 and L.PC_NETGAME_TS_GUESTDIR_LEN == 324)
    b = func_body(c_raw, "pcnetgame_ts_build")
    ck("I the host builds service 7 only with the guest mail switch ON (else it does not exist: never valid, never pushed) and refreshes it with the others",
       "if (!pcnetgame_guest_mail_enabled()) {\n            return 0;" in b and "pcnetgame_ts_build_guestdir(blob);" in b
       and "(void)pcnetgame_ts_refresh((int)PC_NETGAME_TS_GUESTDIR);" in func_body(c_raw, "pcnetgame_ts_refresh_all"))
    gd = func_body(c_raw, "pcnetgame_ts_build_guestdir")
    tg = func_body(c_raw, "pcnetgame_guest_mail_target")
    ck("I the blob: zeroed, newest first (larger age), only DELIVERABLE guests (pcnetgame_guest_mail_target), at most 16, the raw key bytes",
       "memset(blob, 0, PC_NETGAME_TS_GUESTDIR_LEN);" in gd and "s_guest[g].age > s_guest[best].age" in gd and "pcnetgame_guest_mail_target(g)" in gd
       and "memcpy(blob + 4 + 20 * n, &s_guest[best].key, 20);" in gd and "blob[0] = (uint8_t)n;" in gd)
    ck("I deliverable = a used entry of the CURRENT town, confirmed OR holding accepted data (never disposable), the record's PersonalID = the key",
       "(s_guest[g].confirmed || s_rec_slot[PLAYER_NUM + g].rev > 0)" in tg and "pcnetgame_town_equal(&s_guest[g].town, &s_host_town)" in tg
       and "mPr_CheckCmpPersonalID(&pcnetgame_rec_priv_ptr(PLAYER_NUM + g)->player_ID, &s_guest[g].key) == TRUE" in tg and "s_guest_rec" not in tg
       and "!s_guest[g].confirmed && s_rec_slot[PLAYER_NUM + g].rev == 0" in func_body(c_raw, "pcnetgame_guest_entry_disposable"))
    hc = func_body(c_raw, "pcnetgame_handle_client_town_svc")
    vd = func_body(c_raw, "pcnetgame_ts_valid_guestdir_blob")
    ck("I the client accepts service 7 with the exact length, the digest, the seq rule and the validator (count <= 16, reserved zero, listed keys real, the rest zero); "
       "applied without a usable save (a session state)",
       "svc != (int)PC_NETGAME_TS_GUESTDIR" in hc and "(svc == (int)PC_NETGAME_TS_GUESTDIR) ? (uint16_t)PC_NETGAME_TS_GUESTDIR_LEN" in hc
       and "pcnetgame_ts_valid_guestdir_blob(m.blob)" in hc and hc.index("pcnetgame_ts_valid_guestdir_blob(m.blob)") < hc.index("memcpy(s_gdir_client_blob, m.blob")
       and hc.index("memcpy(s_gdir_client_blob, m.blob") < hc.index("if (!pcfa_save_ready())")
       and "blob[0] > PC_NETGAME_GUESTDIR_MAX || blob[1] != 0 || blob[2] != 0 || blob[3] != 0" in vd and "return 0; /* the unused entries are zero */" in vd)
    hh = func_body(head_c, "pcnetgame_handle_client_town_svc")
    ck("I an OLDER client (HEAD's handler) ignores service 7: it is not in its accepted list -> 'reserved / unknown -- ignored', nothing applied",
       "reserved / unknown -- ignored" in hh and "PC_NETGAME_TS_GUESTDIR" not in hh and "svc != (int)PC_NETGAME_TS_UPGRADE) {" in hh)
    dg = func_body(c_raw, "pcnetgame_guest_dir_get")
    ck("I the address view: host = its own table (switch on, world ready), client = the applied blob of THIS session (READY + have), solo = none; never the local player",
       "pcnetgame_ts_build_guestdir(blob);" in dg and "s_ts_client_have[PC_NETGAME_TS_GUESTDIR]" in dg and "s_client_link != PC_NETGAME_LINK_READY" in dg
       and "continue; /* not oneself */" in dg and "!pcnetgame_guest_mail_enabled()" in dg)

    # ------------------------------------------------------------------ J
    mk = func_body(adr, "mAD_make_player_address")
    ck("J the address book: under TARGET_PC the player page holds mAD_PAGE_MAX_ENTRIES names; guests come after the residents and before the museum, bounded by the room "
       "left on the single page (8 drawn rows; the museum takes two)",
       "Mail_nm_c player_mail_name[mAD_PAGE_MAX_ENTRIES];" in adrh and "Mail_nm_c player_mail_name[PLAYER_NUM + 1];" in adrh
       and "const int room = mAD_PAGE_MAX_ENTRIES - (int)*count_p - (mPr_CheckMuseumAddress(Now_Private) ? 2 : 0);" in mk
       and "pc_net_game_guest_address_list(name_p, room)" in mk and mk.index("pc_net_game_guest_address_list") < mk.index("mMsm_GetMuseumMailName(name_p);"))
    ck("J the post office girl (client): a guest of the directory is an address (the host re-checks); a museum letter with a gift is still refused (unchanged)",
       "return (mMl_hunt_for_send_address(mail) == -1 && !pc_net_game_guest_address_known(&mail->header.recipient.personalID)) ? 1 : 0;" in pg
       # worker B re-pin: a museum letter with a gift is now a HOST transaction (MUSEUM_MAIL kind 19): the museum is always an address on a client (0)
       and "if (mail->header.recipient.type == mMl_NAME_TYPE_MUSEUM && mail->present != EMPTY_NO && mail->present != RSV_NO) {\n            return 0;" in pg)
    ck("J the post office girl (the HOST's own player): a letter to a guest is checked (0 / 2 = mailbox full) and delivered locally instead of the post office",
       "const int g = pc_net_game_host_guest_letter_check(mail);" in pg and "if (!client_mail && !pc_net_game_host_guest_letter_post(&play->submenu.mail))" in pg)
    hp = func_body(c_raw, "pc_net_game_host_guest_letter_post")
    ck("J the host player's letter: sender forced to the host player, receive font, the real delivery, the push; host role only",
       "mPr_CopyPersonalID(&W.header.sender.personalID, &Common_Get(now_private)->player_ID);" in hp and "W.content.font = (u8)mMl_FONT_RECV;" in hp
       and "pcnetgame_guest_mbox_deliver(g, &W," in hp and "s_role != PC_NETGAME_ROLE_HOST" in func_body(c_raw, "pc_net_game_host_guest_letter_check"))

    # ------------------------------------------------------------------ K
    send = strip_comments(func_body(c_raw, "pcnetgame_handle_host_mail_txn"))
    rc = func_body(c_raw, "pcnetgame_guest_mail_recipient")
    ck("K the recipient rule: the switch, a PLAYER recipient, pcnetgame_guest_find (this town) + deliverable; anything else -1 (NO_SUCH_ADDRESS as before)",
       "!pcnetgame_guest_mail_enabled()" in rc and "pcnetgame_guest_find(&L->header.recipient.personalID)" in rc and "return pcnetgame_guest_mail_target(g) ? g : -1;" in rc)
    ck("K checks before any mutation: no house AND no guest -> NO_SUCH_ADDRESS; a guest to itself -> NO_SUCH_ADDRESS; no free slot -> MAILBOX_FULL; a guest sender's daily "
       "limit -> MAIL_LIMIT",
       "if (rh < 0) {\n                tg = pcnetgame_guest_mail_recipient(L);" in send and "if (rh < 0 && tg < 0) {" in send and "if (tg == gslot) {" in send
       and "} else if (tg_slot < 0) {" in send and send.index("if (tg == gslot) {") < send.index("memcpy(&s_guest_mbox[tg].box[tg_slot], &W, sizeof(W));"))
    send_raw = func_body(c_raw, "pcnetgame_handle_host_mail_txn")
    gstart = "    if (tg >= 0) {\n        /* guest mailbox: a letter TO A GUEST."
    gbr = strip_comments(send_raw[send_raw.index(gstart):send_raw.index("if (mPO_receipt_proc(&W, mPO_SENDTYPE_MAIL) != TRUE) {")])
    ck("K the commit: sender forced (before), the letter into the found slot, the sender's mirror slot cleared through the sanctioned writer, rev++; a GUEST sender writes the "
       "guest store BEFORE the journal and undoes both halves on failure (BUSY); the journal APPLIED, the count, the push; never the post office",
       "pcnetgame_rec_txn_write_mail(idx, (int)t->slot, NULL);" in gbr and 'if (gslot >= 0 && !pcnetgame_guest_store_write("guest letter to a guest")) {' in gbr
       and gbr.index('pcnetgame_guest_store_write("guest letter to a guest")') < gbr.index("PC_NETGAME_TXN_OUTCOME_APPLIED")
       and "mMl_clear_mail(&s_guest_mbox[tg].box[tg_slot]);" in gbr and "pcnetgame_rec_txn_write_mail(idx, (int)t->slot, &old_src);" in gbr and "slot->rev--;" in gbr
       and "PC_NETGAME_TXN_REASON_BUSY" in gbr and "pcnetgame_guest_mbox_refresh(tg);" in gbr and "mPO_receipt_proc" not in gbr
       and send_raw.index("mPr_CopyPersonalID(&W.header.sender.personalID, &st->bound_pid);") < send_raw.index(gstart))

    # ------------------------------------------------------------------ L
    same = ["mNpc_SendEventPresentMail", "mNpc_SendVtdayMail", "mNpc_SendBirthdayCard", "mNpc_SendEventBirthdayCard2", "mNpc_SendEventXmasCard", "mNpc_SendGoodbyAnimalMailOne",
            "mNpc_SendGoodbyAnimalMail", "mNpc_GetBirthdayCard", "mNpc_GetEventMail", "mNpc_SetGoodbyMailData", "mNpc_GetXmasCardData"]
    ck("L the vanilla resident senders / builders are byte-identical to HEAD (%d functions)" % len(same), all(func_body(npc, n) and func_body(npc, n) == func_body(head_npc, n) for n in same))
    ck("L the museum's resident senders are byte-identical to HEAD (mMsm_SendCompMail, mMsm_SendResultMail, mMsm_SendInformationMail, mMsm_SendMuseumMail)",
       all(func_body(msm, n) and func_body(msm, n) == func_body(head_msm, n) for n in ("mMsm_SendCompMail", "mMsm_SendResultMail", "mMsm_SendInformationMail", "mMsm_SendMuseumMail")))
    mec = func_body(ev, "mail_event_check")
    ck("L the window is handed over INSIDE the vanilla guard and BEFORE the cursor moves (last_date = date)",
       "pc_net_game_guest_event_window(last_time.raw, date.raw);" in mec and mec.index("pc_net_game_guest_event_window(") < mec.index("Save_Get(event_save_common).last_date = date.raw;")
       and mec.index("Save_Get(cheated_flag) == FALSE") < mec.index("pc_net_game_guest_event_window("))
    ck("L Valentine's Day: the guests' pass is flagged right after the vanilla mNpc_SendVtdayMail()",
       "mNpc_SendVtdayMail();\n#ifdef TARGET_PC\n            pc_net_game_guest_vtday_due();" in func_body(ev, "vt_wt_mail_check"))
    sg = func_body(npc, "mNpc_SetGoodbyAnimalMail")
    ck("L a departure: the villager's memories are copied (goodbye_note) inside mNpc_SetGoodbyAnimalMail, i.e. before every caller clears the Animal_c",
       "pc_net_game_guest_goodbye_note(anm_id, Save_Get(animals)[a].memories, ANIMAL_MEMORY_NUM);" in sg
       and all(npc.index("mNpc_SetGoodbyAnimalMail(&l_mnpc_goodby_mail", m.start()) < npc.index("mNpc_ClearAnimalInfo(", m.start()) for m in re.finditer(r"mNpc_SetGoodbyAnimalMail\(&l_mnpc_goodby_mail", npc)))
    gb = {n: strip_comments(func_body(npc, n)) for n in ("mNpc_GuestBirthdayCardsForSink", "mNpc_GuestXmasCardForSink", "mNpc_GuestVtdayMailForSink", "mNpc_GuestGoodbyMailForSink")}
    ck("L the guest builders reuse the vanilla builders and touch no house / post office / Private_c (only the sink receives the letter)",
       all(gb.values()) and "mNpc_GetBirthdayCard(mail, key, &animal->id);" in gb["mNpc_GuestBirthdayCardsForSink"] and "mNpc_GetXmasCardData(mail, key);" in gb["mNpc_GuestXmasCardForSink"]
       and "mNpc_GetEventMail(mail, &keys[sel_key], &animal->id, type, animal->id.looks);" in gb["mNpc_GuestVtdayMailForSink"]
       and "mNpc_SetGoodbyMailData(mail, key, anm_id)" in gb["mNpc_GuestGoodbyMailForSink"]
       and not any(w in body for body in gb.values() for w in ("homes[", "mPO_", "now_private", "private_data")))
    ck("L birthday rule = the vanilla friendship rule (the guest is the villager's HIGHEST-friendship memory); Valentine = the vanilla type rule among guest memories of the other sex",
       "best = mNpc_GetHighestFriendshipIdx(animal->memories, ANIMAL_MEMORY_NUM);" in gb["mNpc_GuestBirthdayCardsForSink"]
       and "mNpc_SelectBestFriend(&best_mem, mem, &best_friendship) == TRUE" in gb["mNpc_GuestVtdayMailForSink"] and "animal->memories[sel].friendship >= 80" in gb["mNpc_GuestVtdayMailForSink"])
    rw = func_body(c_raw, "pcnetgame_gev_run_window")
    ck("L the window pass uses the pure vanilla date rule (birthday from the guest's record, Christmas with its extra rule) and leaves the store to the next host save",
       "pc_guest_event_due(last_raw, now_raw, gp->birthday.month, gp->birthday.day, 0)" in rw and "pc_guest_event_due(last_raw, now_raw, 12u, 25u, 1)" in rw
       and "pcnetgame_gev_finish(&c, 0);" in rw and "check_past_day(min, now, max)" not in due and "last_raw < t && t <= now_raw" in due)
    newfns = ["pcnetgame_guest_mail_target", "pcnetgame_gev_targets", "pcnetgame_gev_run_window", "pcnetgame_gev_run_museum", "pcnetgame_gev_test_hooks", "pcnetgame_gev_sink",
              "pcnetgame_gev_run_goodbye", "pcnetgame_gev_run_vtday", "pcnetgame_host_guest_event_tick", "pc_net_game_host_guest_letter_post", "pcnetgame_ts_build_guestdir"]
    ck("L the new host functions read guest records only through THE accessor (pcnetgame_rec_priv_ptr), never s_guest_rec[] / private_data / homes[]",
       all(func_body(c_raw, n) for n in newfns) and not any(w in strip_comments(func_body(c_raw, n)) for n in newfns for w in ("s_guest_rec[", "private_data", "homes[")))
    ck("L the goodbye queue: bounded (8, the oldest dropped with a log), a delivered recipient is marked done, a full mailbox is retried every minute, residents skipped",
       "#define PC_NETGAME_GEV_BYE_Q 8" in c_raw and "s_gev_bye[q].done[m] = 1;" in func_body(c_raw, "pcnetgame_gev_run_goodbye")
       and "s_gev_retry_ms = now + 60000u;" in func_body(c_raw, "pcnetgame_host_guest_event_tick"))

    # ------------------------------------------------------------------ M
    gc = func_body(msm, "mMsm_GuestCompMailForSink")
    ck("M the museum letter: once per guest (RECEIVED clear), the vanilla letter (0x22F, FTR_NOG_MUSEUM, museum paper), the two bits set only after the sink took it",
       "(gpriv->state_flags & mPr_FLAG_MUSEUM_COMP_HANDBILL_RECEIVED) != 0" in gc and "FTR_START(FTR_NOG_MUSEUM), ITM_PAPER24, l_museum_name_str, 0x22F" in gc
       and gc.index("(*sink)(mail, ctx) != TRUE") < gc.index("gpriv->state_flags |= mPr_FLAG_MUSEUM_COMP_HANDBILL_SCHEDULED | mPr_FLAG_MUSEUM_COMP_HANDBILL_RECEIVED;"))
    rm = func_body(c_raw, "pcnetgame_gev_run_museum")
    ck("M the host then refreshes the record's host fields (rev++, dirty), pushes a bound client (rate limited) and writes the guest store at once (letter + bits, one file)",
       "pcnetgame_rec_refresh_hostfields(idx, rs);" in rm and "pcnetgame_rec_stale_push((PCNetPeerId)p, idx, rs, pcnetgame_now_ms());" in rm and "pcnetgame_gev_finish(&c, 1);" in rm
       and "PC_NETGAME_REC_HOSTBITS_STATE_FLAGS ((u32)(mPr_FLAG_MUSEUM_COMP_HANDBILL_SCHEDULED | mPr_FLAG_MUSEUM_COMP_HANDBILL_RECEIVED))" in c_raw)

    # ------------------------------------------------------------------ N
    et = func_body(c_raw, "pcnetgame_host_guest_event_tick")
    ck("N the switch gates the event tick, the recipient, the directory build and the host address view",
       "!pcnetgame_guest_mail_enabled()" in et and "!pcnetgame_guest_mail_enabled()" in rc and "!pcnetgame_guest_mail_enabled()" in b and "!pcnetgame_guest_mail_enabled()" in dg
       and "pcnetgame_host_guest_event_tick();  /* guest mailbox" in c_raw)
    th = func_body(c_raw, "pcnetgame_gev_test_hooks")
    ck("N the event test hook is TEST-ONLY (PC_NET_TEST_HOOKS + pc_test_hook_getenv), never getenv", "#ifdef PC_NET_TEST_HOOKS" in th
       and 'pc_test_hook_getenv("AC_TEST_GUEST_EVENT_MAIL")' in th and 'getenv("AC_TEST_GUEST_EVENT' not in c_raw.replace('pc_test_hook_getenv("AC_TEST_GUEST_EVENT', ""))
    ck("N the seams are declared in pc_guest_mail.h (not pc_net_game.h)", all(s in gmh for s in ("int pc_net_game_guest_address_list(Mail_nm_c* out, int max);",
       "void pc_net_game_guest_event_window(u32 last_raw, u32 now_raw);", "void pc_net_game_guest_goodbye_note(const void* anm_id, const void* memories, int n);")))
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    bad = [d for d, x in wb if not x]
    ck("N wire_baseline: %d checks, all green%s" % (len(wb), (" (FAILED: %s)" % bad) if bad else ""), wb and not bad)

    if "--native" in sys.argv:
        cc = os.environ.get("CC", "gcc")
        tmp = os.environ.get("TEMP", ".")
        src = os.path.join(tmp, "guest_event_due_test.c")
        exe = os.path.join(tmp, "guest_event_due_test.exe")
        cases = [  # (last y,m,d,h, now y,m,d,h, month, day, xmas, expected)
            ((26, 6, 14, 9), (26, 6, 15, 9), 6, 15, 0, 1),     # the day before -> the birthday
            ((26, 6, 15, 9), (26, 6, 15, 12), 6, 15, 0, 0),    # same day again: already passed (the cursor is the dedupe)
            ((26, 6, 10, 0), (26, 6, 20, 0), 6, 15, 0, 1),     # days skipped: caught up once
            ((26, 6, 16, 0), (26, 6, 20, 0), 6, 15, 0, 0),     # already behind the cursor
            ((26, 12, 30, 0), (27, 1, 2, 0), 12, 31, 0, 1),    # new-year wrap: last year's Dec 31
            ((26, 12, 30, 0), (27, 1, 2, 0), 1, 1, 0, 1),      # new-year wrap: this year's Jan 1
            ((26, 12, 24, 9), (26, 12, 25, 9), 12, 25, 1, 1),  # Christmas
            ((26, 12, 24, 9), (27, 1, 2, 9), 12, 25, 1, 0),    # Christmas card not across the new year (vanilla: same year)
            ((26, 6, 14, 9), (26, 6, 15, 9), 0, 15, 0, 0),     # no birthday set
            ((26, 6, 16, 9), (26, 6, 15, 9), 6, 15, 0, 0),     # cursor ahead of today (clock moved back)
            ((0, 0, 0, 0), (26, 6, 15, 9), 6, 15, 0, 0),       # cursor outside the vanilla guard (never set)
        ]
        with open(src, "w") as f:
            f.write('#include <stdio.h>\n#include "pc_guest_event_due.h"\n#define R(y,m,d,h) (((uint32_t)(y)<<24)|((uint32_t)(m)<<16)|((uint32_t)(d)<<8)|(uint32_t)(h))\n'
                    "int main(void){int f=0,p=0;\n")
            for (l, n, mo, dy, x, e) in cases:
                f.write("{int r=pc_guest_event_due(R(%d,%d,%d,%d),R(%d,%d,%d,%d),%du,%du,%d); if(r==%d)p++; else {f++; printf(\"FAIL %s -> %%d\\n\", r);}}\n"
                        % (l + n + (mo, dy, x, e, "%r %r %d/%d x%d" % (l, n, mo, dy, x))))
            f.write('printf("RESULT passed=%d failed=%d\\n",p,f);return f!=0;}\n')
        r = subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-O1", "-I", os.path.join(PC, "include"), src, "-o", exe], capture_output=True, text=True)
        ck("O the date rule compiled warning-free", r.returncode == 0 and r.stderr.strip() == "")
        if r.returncode == 0:
            p = subprocess.run([exe], capture_output=True, text=True, timeout=60)
            m = re.search(r"RESULT passed=(\d+) failed=(\d+)", p.stdout)
            print(p.stdout)
            ck("O the vanilla mail-event date rule (%d cases: the window, the dedupe, catch-up, the new-year wrap, the Christmas rule, unset / clock-back / unset cursor): %s"
               % (len(cases), m.group(0) if m else "no RESULT"), p.returncode == 0 and m is not None and m.group(2) == "0" and int(m.group(1)) == len(cases))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
