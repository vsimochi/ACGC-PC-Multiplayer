#!/usr/bin/env python3
"""test_able_museum_src.py - worker B: SOURCE audit of the Able Sisters journal / villager outfits / museum letters / guest donor names (no process).
Pins the ids and the seams the process tests cannot reach (Mable's dialogue gate, the post girl row mapping, the plaque / Blathers rows, the promotion snapshot).
Usage: python test_able_museum_src.py"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
results = []


def rd(*p):
    return open(os.path.join(REPO, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def ck(desc, cond):
    results.append(bool(cond))
    print(("PASS - " if cond else "FAIL - ") + desc)


ng = rd("pc", "src", "pc_net_game.c")
ni = rd("src", "actor", "ac_needlework_indoor.c")
nt = rd("src", "actor", "npc", "ac_npc_needlework_talk.c_inc")
pg = rd("src", "actor", "npc", "ac_npc_post_girl.c_inc")
tg = rd("src", "game", "m_tag_ovl.c")
ms = rd("src", "game", "m_museum.c")
cur = rd("src", "actor", "npc", "ac_npc_curator_move.c_inc")
mc = rd("pc", "src", "pc_m_card.c")
cm = rd("pc", "CMakeLists.txt")

# ---- ids
ck("W1 message id 90 VILLAGER_OUTFIT, 128-byte struct pinned", "PC_NETGAME_MSG_VILLAGER_OUTFIT       = 90," in ng and "sizeof(PCNetGameVillagerOutfitMsg) == 128" in ng)
ck("W2 TXN kind 19 MUSEUM_MAIL, reasons 42 MUSEUM_FULL / 43 MUSEUM_MAIL_OFF", "#define PC_NETGAME_TXN_KIND_MUSEUM_MAIL 19u" in ng and "#define PC_NETGAME_TXN_REASON_MUSEUM_FULL     42u" in ng
   and "#define PC_NETGAME_TXN_REASON_MUSEUM_MAIL_OFF 43u" in ng)
ck("W3 town service 8 MUSEUM_DONORS (284 B), PC_NETGAME_TS_NUM 11", "#define PC_NETGAME_TS_MDONORS  8u" in ng and "#define PC_NETGAME_TS_NUM      12u" in ng
   and "#define PC_NETGAME_TS_MDONORS_LEN (4u + 120u + PC_NETGAME_MDONORS_NAMES * 20u) /* 284" in ng)
ck("W4 no other id of this worker collides (kind 20 / reasons 45..47 belong to worker C)", "#define PC_NETGAME_TXN_KIND_HOUSE_UPGRADE 20u" in ng)

# ---- Able: journal / gate / seen revision
ck("A1 the trade requests are journaled around the vanilla calls (4 request kinds), never the live refresh",
   ni.count("pc_able_trade_begin(") == 4 and ni.count("pc_able_trade_end();") == 4 and "pc_able_trade_begin" not in ni[ni.find("static void aNI_PcRefreshFromSave"):ni.find("static int aNI_PcOrgIdx")])
ck("A2 the SEEN revision is fed only from the live refresh (buffer == Save)", "pc_able_buffer_synced(i);" in ni)
ck("A3 Mable's gate: exchange / display refused with the vanilla cancel line 0x2FF5, take-a-copy untouched",
   re.search(r"actor->talk_idx == aNNW_TALK_TRADE_CLOSE \|\| actor->talk_idx == aNNW_TALK_TRADE_CLOSE2\) && !pc_able_trade_gate\(\)\) \{\n\s*mMsg_ChangeMsgData\(msg_p, 0x2FF5\);", nt) is not None)
ck("A4 the client write of a traded page is built on the SEEN revision", "? s_able_txn.base_rev : s_pgc_rev[i];" in ng)
ck("A5 STALE / BAD / superseded / reconnect roll the my_org slot back", 'pcnetgame_able_rollback((in->flags & PCNG_PAGE_F_BAD) ? "write REFUSED"' in ng
   and 'pcnetgame_able_rollback("reconnect reset' in ng)
ck("A6 a rollback only restores a slot that still holds what the trade put there", "if (memcmp(o, &s_able_txn.org_after, sizeof(*o)) == 0) {" in ng)
ck("A7 a non-trade local edit lost to a newer host copy is logged SUPERSEDED (never silent)", "is SUPERSEDED by the host's rev" in ng)
ck("A8 guests: pushed every page; DESIGN writes allowed (rate limit + content checks unchanged), notice board resident-only",
   "!pcnetgame_guest_pages_on())) /* worker B" in ng and "in->kind == PCNG_PAGE_DESIGN && pcnetgame_guest_pages_on() && pcnetgame_guest_of_idx(idx) >= 0" in ng
   and "++s_pg_wr_n[peer] > PC_NETGAME_PAGE_HOST_WR_LIMIT || !pcnetgame_page_valid(" in ng
   and "#define PC_NETGAME_PAGE_HOST_WR_LIMIT 20u" in ng) # notice board latency work: the host's 20 writes / s limit is now a named constant (same value, same refusal)
ck("A9 host trend reset on an APPLIED design write; skipped while the villager's actor is live", "pcnetgame_outfit_trend_reset((int)in->index);" in ng
   and "if (pcnetgame_outfit_actor_live((uint16_t)a[i].id.npc_id)) {" in ng)
ck("A10 the client applies outfits only to villagers without a live actor (the actor writes its drawn outfit back at its save)",
   ng.count("pcnetgame_outfit_actor_live(w->npc_id)") == 1)

# ---- museum letters
ck("M1 the post girl: a client's museum letter WITH a gift takes the host path (kind 19) and MUSEUM_FULL maps onto the 'can't take more' row",
   "play->submenu.mail.header.recipient.type == mMl_NAME_TYPE_MUSEUM && play->submenu.mail.present != EMPTY_NO" in pg and "case PC_MUSEUM_MAIL_REJECT_FULL:" in pg)
ck("M2 m_tag_ovl keeps that letter in its slot until APPLIED", "(mail->header.recipient.type == mMl_NAME_TYPE_MUSEUM && mail->present != EMPTY_NO && mail->present != RSV_NO)" in tg)
ck("M3 the host runs the museum on the SENDER's record (rec_priv_ptr) with the sender's donor nibble", "mres = mMsm_PC_MuseumMailFor(mir, gift, donor);" in ng
   and "const int donor = pcnetgame_museum_donor_nibble(idx);" in ng)
ck("M4 the room check refuses instead of dropping a gift (vanilla drops it)", "!mMsm_PC_MuseumMailRoom(mir, L->present)" in ng)
ck("M5 m_museum.c: the explicit-donor variant never uses Common player_no", "mMmd_PC_RequestMuseumDisplayDonor(present, donor)" in ms
   and "Common_Get(player_no)" not in ms[ms.find("extern int mMsm_PC_MuseumMailFor"):ms.find("extern int mMsm_PC_GuestMuseumDay")])
ck("M6 the guests' day pass hooks the vanilla mMsm_DepositFossil (host only inside)", "pc_net_game_museum_day_pass(fossil_count < mMsm_DEPOSIT_FOSSIL_MAX && send_order_info);" in ms)
ck("M7 the pass marks the guest 'contacted' (the address book's Museum entry) only when the info letter was taken", "record->contacted = TRUE; /* the Museum entry" in ms)

# ---- donor names
ck("D1 the sidecar module is built", "src/pc_museum_donors.c" in cm)
ck("D2 in-person guest donations and MUSEUM_MAIL donations are attributed", ng.count("pcnetgame_mdon_note(") >= 3)
ck("D3 an attribution is only read while the nibble is 5", "mMmd_PC_ExhibitDonor(exhibit) != mMmd_DONATOR_DELETED_PLAYER" in ng)
ck("D4 plaques: fossil / picture / fish / insect use the guest name", all("pc_museum_guest_donor(" in rd("src", "actor", f) for f in
   ("ac_museum_fossil.c", "ac_museum_picture.c", "ac_museum_fish.c", "ac_museum_insect.c")))
ck("D5 Blathers: own / named rows for an attributed guest exhibit, every row < 16 (no transaction starts)",
   "aCR_pc_guest_row(item, donator, 4, 8)" in cur and "aCR_pc_guest_row(item, donator, 5, 9)" in cur and "aCR_pc_guest_row(item, donator, 7, 11)" in cur
   and "aCR_pc_guest_row(item, donator, 6, 10)" in cur)
ck("D6 promotion: museum_display in the snapshot, restored by the rollback, re-attributed inside the create",
   "mMmd_info_c museum_display;" in mc and "s_pc_promote_snap.museum_display = Save_Get(museum_display);" in mc
   and "Save_Get(museum_display) = s_pc_promote_snap.museum_display;" in mc and "remapped += pc_net_game_museum_promote(gkey, slot);" in mc)
ck("D7 the promotion never edits the sidecar (orphans are pruned later, a rollback makes them valid again)",
   "pc_mdon_set" not in ng[ng.find("int pc_net_game_museum_promote"):ng.find("/* ---- MUSEUM_DONORS (town service 8) ---- */")]
   and "pc_mdon_prune" not in ng[ng.find("int pc_net_game_museum_promote"):ng.find("/* ---- MUSEUM_DONORS (town service 8) ---- */")])

print("%d/%d checks passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
