#!/usr/bin/env python3
"""test_mail_src.py - SOURCE AUDIT of Mail milestone 1: M0 (the two client duplication holes D-1 / D-2), R (museum_record becomes HOST-owned) and M1
(a client's letter to a PLAYER through the host transaction TXN_COMMIT kind 12 MAIL_SEND).

Pure text checks over pc/src/pc_net_game.c, src/actor/ac_mailbox*.c*, src/actor/npc/ac_npc_post_girl.c_inc, src/game/m_tag_ovl.c, m_npc.c, m_private.c,
m_mail.c, pc/src/pc_main.c, pc/include/pc_platform.h / pc_net_game.h and the python library. No game process. Tier: SOURCE AUDITED (the dialogue seams
are not UI-tested; the host handler and the client API are runtime-tested by test_mail_protocol.py / test_mail_real_client.py).

  W  wire: kind 12 + reasons 23 / 24 / 25 + the upload-validator code 12 equal in C and python; NO new message id (max id unchanged), TXN_COMMIT /
     TXN_RESULT sizes unchanged; wire_baseline green
  R  museum_record: HOST range of the ownership table (offset 0x18, 0x4E B: measured, not the 0x17 of the m_private.h comment), static asserts, in the
     host-field digest, python mirror identical
  O  M0: a network CLIENT's mailbox never opens / never yields a letter (mailbox actor, both mailbox -> pocket transfers, the mark-and-take proc), the
     archive -> pocket exchange is refused, the load-time / day-change mailbox generators do not run for a client (client only: host / solo unchanged);
     the remaining direct mailbox writers are PINNED (a new one must be decided)
  H  host handler: reads the letter ONLY from its own mirror; the algorithm order; every check is read-only before the single commit; sender := bound
     identity, receive font; the one mail[] writer; faults; dispatcher
  C  client: AWAIT_CLEAN is not a transaction (never in txn_busy / upload_deferred, in pocket_locked), bounded, the clean predicate; the letter leaves the
     slot ONLY in pcnetgame_txn_apply_applied's mail path; no client write of mail[] / mailbox before APPLIED; seam (post girl + mTG_send_proc)
  T  test hooks: default off, loud, documented, role-gated, no environment override
"""
import os
import re
import sys

import net_spike_lib as L
import wire_baseline

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def read(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
        return f.read().decode("utf-8", "replace").replace("\r\n", "\n")


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def func_body(src, name):
    m = re.search(r"^(?:static )?[\w ]+?[ \*]+%s\([^{;]*\)\s*\{\n" % re.escape(name), src, re.M)
    if not m:
        return ""
    i = m.end()
    j = src.index("\n}\n", i)
    return src[i:j]


def in_order(body, needles):
    idx = []
    for n in needles:
        i = body.find(n)
        if i < 0:
            return False
        idx.append(i)
    return idx == sorted(idx) and len(set(idx)) == len(idx)


def main():
    results = []
    check = lambda d, c: L.check(d, bool(c), results)
    c_raw = read("pc/src/pc_net_game.c")
    c = strip_comments(c_raw)
    h = read("pc/include/pc_net_game.h")
    plat_h = read("pc/include/pc_platform.h")
    main_c = read("pc/src/pc_main.c")
    pg = read("src/actor/npc/ac_npc_post_girl.c_inc")
    pgc = strip_comments(pg)
    tag = read("src/game/m_tag_ovl.c")
    npc = read("src/game/m_npc.c")
    priv = read("src/game/m_private.c")
    mail_c = read("src/game/m_mail.c")
    mbx = read("src/actor/ac_mailbox_move.c_inc")
    mbx_c = read("src/actor/ac_mailbox.c")
    mhb, mhe = c_raw.index("===== MAIL HOST BEGIN"), c_raw.index("===== MAIL HOST END")
    mh_raw = c_raw[mhb:mhe]
    mh = strip_comments(mh_raw)
    mcb, mce = c_raw.index("===== MAIL CLIENT BEGIN"), c_raw.index("===== MAIL CLIENT END")
    mc_raw = c_raw[mcb:mce]
    mc = strip_comments(mc_raw)
    hd_raw = func_body(mh_raw, "pcnetgame_handle_host_mail_txn")
    hd = strip_comments(hd_raw)
    x1b, x1e = c_raw.index("===== X1 BEGIN"), c_raw.index("===== X1 END")
    x1 = strip_comments(c_raw[x1b:x1e])
    d3b, d3e = c_raw.index("===== D3 BEGIN"), c_raw.index("===== D3 END")
    d3 = strip_comments(c_raw[d3b:d3e])
    xcb, xce = c_raw.index("===== X1b CLIENT BEGIN"), c_raw.index("===== X1b CLIENT END")
    xc_raw = c_raw[xcb:xce]
    xc = strip_comments(xc_raw)

    # ------------------------------------------------------------------ W: wire
    cm = dict(re.findall(r"#define (PC_NETGAME_(?:TXN_KIND_MAIL_SEND|TXN_REASON_NO_SUCH_ADDRESS|TXN_REASON_MAILBOX_FULL|TXN_REASON_PO_FULL|REC_FIELD_MAIL_PRESENT))\s+(\d+)u", c_raw))
    check("W kind 12 MAIL_SEND, reasons 23 NO_SUCH_ADDRESS / 24 MAILBOX_FULL / 25 PO_FULL and the upload-validator code 12 in C == net_spike_lib",
          cm == {"PC_NETGAME_TXN_KIND_MAIL_SEND": "12", "PC_NETGAME_TXN_REASON_NO_SUCH_ADDRESS": "23", "PC_NETGAME_TXN_REASON_MAILBOX_FULL": "24",
                 "PC_NETGAME_TXN_REASON_PO_FULL": "25", "PC_NETGAME_REC_FIELD_MAIL_PRESENT": "12"}
          and L.PC_NETGAME_TXN_KIND_MAIL_SEND == 12 and (L.PC_NETGAME_TXN_REASON_NO_SUCH_ADDRESS, L.PC_NETGAME_TXN_REASON_MAILBOX_FULL, L.PC_NETGAME_TXN_REASON_PO_FULL) == (23, 24, 25)
          and L.PC_NETGAME_REC_FIELD_MAIL_PRESENT == 12 and L.TXN_REASON_NAMES[23] == "NO_SUCH_ADDRESS" and L.TXN_REASON_NAMES[24] == "MAILBOX_FULL" and L.TXN_REASON_NAMES[25] == "PO_FULL")
    check("W M1 added NO message id (55 was the highest at M1; the current highest, wire_baseline.EXPECTED_MAX_MSG_ID = %d, is MAILBOX_LETTER of mail milestone 2 -- audited by test_mail2_src.py) and TXN_COMMIT (72 B) / TXN_RESULT (76 B) keep their exact size asserts: the M1 letter is NEVER on the wire"
          % wire_baseline.EXPECTED_MAX_MSG_ID,
          wire_baseline.EXPECTED_MAX_MSG_ID >= 66 and max(v for _n, v in wire_baseline.c_message_ids(c_raw)) == 66 and dict(wire_baseline.c_message_ids(c_raw)).get("PC_NETGAME_MSG_TOWN_SVC_STATE") == 55
          and "_Static_assert(sizeof(PCNetGameTxnCommitMsg) == 72," in c_raw and "_Static_assert(sizeof(PCNetGameTxnResultMsg) == 76," in c_raw
          and not re.search(r"Mail_c\s+\w+;", c_raw[c_raw.index("typedef struct PCNetGameTxnCommitMsg"):c_raw.index("} PCNetGameTxnCommitMsg;")]))
    check("W the tag convention of the doc comment: dest NONE, slot = mail slot, item = gift echo, aux_item = low 16 / aux_cond = bits 16..23 of the 24-bit hash; python mail_hash24 is the same cut",
          "tag.aux_item = the LOW 16 bits and tag.aux_cond = bits 16..23" in c_raw and "want24 = ((uint32_t)t->aux_cond << 16) | (uint32_t)t->aux_item;" in hd
          and "L.mail_hash24" in "L.mail_hash24" and L.mail_hash24(bytes(L.REC_MAIL_SIZE)) == L.fnv1a32(bytes(L.REC_MAIL_SIZE)) & 0xFFFFFF)
    wire_baseline.run(lambda desc, cond: check("W " + desc, cond), ROOT)

    # ------------------------------------------------------------------ R: museum_record host-owned
    own_name = {"IMMUTABLE": L.REC_OWN_IMMUTABLE, "HOST": L.REC_OWN_HOST, "CLIENT": L.REC_OWN_CLIENT, "SHARED": L.REC_OWN_SHARED}
    rows = [(int(a, 16), int(l, 16), o) for a, l, o in re.findall(r"\{\s*(0x[0-9A-Fa-f]+)u,\s*(0x[0-9A-Fa-f]+)u,\s*PC_NETGAME_REC_OWN_(\w+)\s*\}", d3)]
    check("R the ownership table has the HOST range {0x0018, 0x004E} (museum_record) between two CLIENT ranges {0x0014, 0x0004} / {0x0066, 0x0020} and python RECORD_FIELD_RANGES is identical",
          (0x14, 4, "CLIENT") in rows and (0x18, 0x4E, "HOST") in rows and (0x66, 0x20, "CLIENT") in rows and (0x14, 0x72, "CLIENT") not in rows
          and [(o, l, own_name[w]) for o, l, w in rows] == [(o, l, w) for _n, o, l, w in L.RECORD_FIELD_RANGES] and len(rows) == 12)
    check("R static asserts pin it against the real struct: offsetof museum_record 0x0018, sizeof 0x004E, inventory 0x0068, and the ranges still tile 0..0x2440",
          "offsetof(Private_c, museum_record) == 0x0018" in c_raw and "sizeof(((Private_c*)0)->museum_record) == 0x004E" in c_raw
          and "offsetof(Private_c, inventory) == 0x0068" in c_raw and "0x0066 + 0x0020 == 0x0086" in c_raw and "D3 ownership ranges must tile" in c_raw)
    hdig = func_body(d3, "pcnetgame_rec_hostfield_digest")
    check("R the host-consumption digest covers museum_record (so a host consumption bumps the lineage rev and owes a PUSH_HOSTFIELDS); merge / client apply / digests are table driven (no hard-coded museum offsets)",
          "&r->museum_record, sizeof(r->museum_record)" in hdig and "0x0018" not in strip_comments(func_body(d3, "pcnetgame_rec_merge_into_save")))
    check("R the client's upload digest (cown) and the host merge copy only CLIENT + SHARED ranges: the HOST range is never taken from an upload nor part of a client's dirty check",
          "s_rec_ranges[i].owner == PC_NETGAME_REC_OWN_CLIENT || s_rec_ranges[i].owner == PC_NETGAME_REC_OWN_SHARED" in c)
    check("R the client's HOSTFIELDS apply overwrites HOST + SHARED ranges (museum_record included) and keeps local CLIENT edits",
          "host_only && r->owner != PC_NETGAME_REC_OWN_HOST && r->owner != PC_NETGAME_REC_OWN_SHARED" in c)
    writers = re.findall(r"museum_record", c)
    check("R nothing on a client writes museum_record: the only write in pc_net_game.c is the HOST-only TEST hook poke (stored_fossil_num) and the table / digest reads",
          len(re.findall(r"museum_record\.\w+\s*=[^=]", c)) == 1 and "p->museum_record.stored_fossil_num = (u8)((before + 1u) & 0x1Fu);" in c)

    # ------------------------------------------------------------------ O: M0
    chk_mbx = func_body(mbx, "aMBX_check_take_mail") or mbx[mbx.index("static void aMBX_check_take_mail"):mbx.index("static void aMBX_check_flag")]
    gate_open = "&& (pc_net_game_role() != PC_NETGAME_ROLE_CLIENT || pc_net_game_client_mailbox_usable())"
    check("O1 (M0, refined by mail milestone 2) the mailbox actor does not open for a CLIENT unless its mailbox is host-fed: the open condition carries '" + gate_open + "' under #ifdef TARGET_PC, before the open request",
          "#ifdef TARGET_PC" in chk_mbx and gate_open in chk_mbx and chk_mbx.index(gate_open) < chk_mbx.index("actor->req = aMBX_REQUEST_OPEN;") and "&& pc_net_game_role() != PC_NETGAME_ROLE_CLIENT\n" not in chk_mbx
          and "#ifdef TARGET_PC\n#include \"pc_net_game.h\"" in mbx_c)
    flag = mbx[mbx.index("static void aMBX_check_flag"):mbx.index("static void aMBX_setup_flag_se_sub")]
    check("O1 the mailbox flag of a client does not go up until the mailbox is host-fed (mail_count forced to 0 for the CLIENT role while pc_net_game_client_mailbox_usable() is 0, before it is used)",
          "if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && !pc_net_game_client_mailbox_usable()) {\n        mail_count = 0;" in flag and flag.index("mail_count = 0;") < flag.index("if (mail_count != 0)"))
    ctm = func_body(tag, "mTG_check_trans_mail")
    ctk = func_body(tag, "mTG_check_trans_mail_mark")
    cmp_ = func_body(tag, "mTG_mailbox_change_mail_proc")
    check("O2 defense in depth (M0, refined by mail milestone 2): mTG_check_trans_mail (single letter -> pocket) and _mark (marked letters -> pocket) take the 'empty mailbox' branch for a client unless its mailbox is host-fed",
          gate_open in ctm and ctm.index(gate_open) < ctm.index("res = mTG_trans_mail(") and gate_open in ctk and ctk.index(gate_open) < ctk.index("res = mTG_trans_mail_mark(")
          and "&& pc_net_game_role() != PC_NETGAME_ROLE_CLIENT /*" not in ctm + ctk)
    check("O2 the mailbox 'take marked letters' proc takes the vanilla 'pockets full' refusal branch for a client whose mailbox is NOT host-fed (idx = -1: marks cleared, nothing moves)",
          "if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && !pc_net_game_client_mailbox_usable()) {\n        idx = -1;" in cmp_ and cmp_.index("idx = -1;") < cmp_.index("if (idx != -1) {"))
    cpm = func_body(tag, "mTG_cpmail_change_mail_proc")
    check("O3 D-2: an archive -> pocket exchange (any archive letter marked) is refused for a client with the vanilla 'cannot exchange' branch; pocket -> archive only stays allowed (the gate needs cpmail_mark_cnt > 0)",
          "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && cpmail_mark_cnt > 0" in cpm and "inv_cnt = inv_mark_cnt;" in cpm and "cpmail_cnt = cpmail_mark_cnt;" in cpm
          and cpm.index("cpmail_mark_cnt > 0") < cpm.index("if (inv_cnt == inv_mark_cnt && cpmail_cnt == cpmail_mark_cnt) {"))
    gens = {"mNpc_SendEventPresentMail": npc, "mNpc_SendBirthdayCard": npc, "mNpc_SendEventXmasCard": npc, "mNpc_SendGoodbyAnimalMailOne": npc}
    ok = True
    for nm, src in gens.items():
        b = func_body(src, nm)
        gi = b.find("if (mNpc_CLIENT_SKIPS_MAILBOX_LETTER()) {\n        return FALSE;\n    }")
        wi = b.find("mMl_copy_mail(")
        ok = ok and 0 <= gi < wi and b.count("mNpc_CLIENT_SKIPS_MAILBOX_LETTER()") == 1
    check("O4 D-1: the four villager generators that write straight into homes[].mailbox (event present with RANDOM furniture, birthday card, Xmas card, goodbye letter) return before any mailbox write for a CLIENT", ok)
    check("O4 the generator gate is the plain CLIENT role test under TARGET_PC (0 otherwise): host / solo unchanged",
          "#define mNpc_CLIENT_SKIPS_MAILBOX_LETTER() (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT)" in npc and "#define mNpc_CLIENT_SKIPS_MAILBOX_LETTER() 0" in npc
          and npc.index("#ifdef TARGET_PC\n/* Mail milestone M0") < npc.index("#define mNpc_CLIENT_SKIPS_MAILBOX_LETTER() (pc"))
    mm = func_body(priv, "mPr_SendMotherMailPost")
    bc = func_body(mail_c, "mMl_send_mail_box_com")
    pc_ = func_body(mail_c, "mMl_send_mail_postoffice_com")
    check("O4 the mother's letter (a gift) and every system letter built by mMl_send_mail_* (museum info, post office gifts) are not generated for a client",
          mm.index("pc_net_game_role() == PC_NETGAME_ROLE_CLIENT") < mm.index("mMl_copy_mail(") and "return FALSE;" in mm[:mm.index("mMl_copy_mail(")]
          and bc.index("pc_net_game_role() == PC_NETGAME_ROLE_CLIENT") < bc.index("mMl_copy_mail(") and pc_.index("pc_net_game_role() == PC_NETGAME_ROLE_CLIENT") < pc_.index("mPO_receipt_proc(")
          and '#ifdef TARGET_PC\n#include "pc_net_game.h"' in priv and '#ifdef TARGET_PC\n#include "pc_net_game.h"' in mail_c)
    gate_blocks = []
    for name, src in (("m_npc.c", npc), ("m_private.c", priv), ("m_mail.c", mail_c), ("ac_mailbox_move.c_inc", mbx), ("m_tag_ovl.c", tag)):
        for m in re.finditer(r"pc_net_game_role\(\)\s*([!=]=)\s*PC_NETGAME_ROLE_(\w+)", src):
            gate_blocks.append((name, m.group(1), m.group(2)))
    mailish = [g for g in gate_blocks if g[0] in ("ac_mailbox_move.c_inc", "m_mail.c", "m_private.c")]
    check("O5 every M0 gate is a CLIENT-only test (== CLIENT, or != CLIENT on the 'open / take' condition); there is no HOST test and no pc_net_game_world_is_host_authoritative() in them",
          all(g[2] == "CLIENT" for g in mailish) and len(mailish) >= 5 and "world_is_host_authoritative" not in chk_mbx + flag + ctm + ctk + cmp_ + cpm + mm + bc + pc_)
    pinned = {"src/actor/ac_pterminal.c": 1, "src/actor/ac_shop_level.c": 1, "src/game/m_card.c": 1, "src/game/m_fishrecord.c": 1, "src/game/m_mark_room.c": 1,
              "src/game/m_mark_room_ovl.c": 1, "src/game/m_mail.c": 1, "src/game/m_npc.c": 4, "src/game/m_post_office.c": 1, "src/game/m_private.c": 1,
              "src/game/m_quest.c": 1, "src/game/m_shop.c": 2}
    found = {}
    for root_dir, _d, files in os.walk(os.path.join(ROOT, "src")):
        for fn in files:
            if fn.endswith((".c", ".c_inc")):
                rel = os.path.relpath(os.path.join(root_dir, fn), ROOT).replace(os.sep, "/")
                n = len(re.findall(r"mMl_copy_mail\((?:&)?[^;]*mailbox", read(rel)))
                if n:
                    found[rel] = n
    check("O6 PIN: the direct writers into homes[].mailbox are exactly this set (%d files): a client's local mailbox is a dead copy that cannot be opened or taken from (O1-O3), "
          "so the writers that are NOT gated (event-driven fishing / quest / shop / card letters, the host-gated post office delivery) are harmless; a NEW writer must be decided here" % len(pinned),
          found == pinned)

    # ------------------------------------------------------------------ H: host handler
    check("H the handler exists in its own MAIL HOST block and the dispatcher routes exactly kind 12 to it (after the town-service kinds, before the X1 handler)",
          "pcnetgame_handle_host_mail_txn(peer, &tc); /* mail milestone 1" in c_raw and c_raw.count("pcnetgame_handle_host_mail_txn(") == 2
          and re.search(r"else if \(tc\.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_MAIL_SEND\) \{\s*pcnetgame_handle_host_mail_txn\(peer, &tc\);", c_raw) is not None)
    order = ["s_host_peer_link[peer] != PC_NETGAME_LINK_READY", "PC_TXN_FAULT_IGNORE_COMMIT", "pcnetgame_rec_gate(peer, 0, 0)", "st->rec_state != PC_NETGAME_RECS_SYNCED",
             "shape_ok = in->kind == (uint8_t)PC_NETGAME_TXN_KIND_MAIL_SEND", "pcnetgame_txn_nonce_fenced(", "pcnetgame_txn_journal_find(", "R->max_seq = t->txn_seq;",
             "pcnetgame_rec_refresh_hostfields(idx, slot);", "t->base_epoch != slot->epoch", "pcnetgame_rec_validate_inventory(t->pre_pockets", "mPr_CheckCmpPersonalID(&Save_Get(homes[i]).ownerID",
             "pcnetgame_mail_be_hash(L) & 0xFFFFFFu", "mMl_check_send_mail((Mail_c*)L)", "pcnetgame_is_pocket_legal_item(L->present)", "L->header.recipient.type != (u8)mMl_NAME_TYPE_PLAYER",
             "mMl_hunt_for_send_address((Mail_c*)L)", "mMl_chk_mail_free_space(Save_Get(homes[rh]).mailbox", "mPO_count_mail(rh) >= HOME_MAILBOX_SIZE", "PC_TXN_FAULT_FAIL_WORLD",
             "pcnetgame_rec_txn_idx_ok(idx)", "memcpy(&W, L, sizeof(W));", "mPr_CopyPersonalID(&W.header.sender.personalID, &st->bound_pid);", "W.content.font = (u8)mMl_FONT_RECV;",
             "mPO_receipt_proc(&W, mPO_SENDTYPE_MAIL)", "pcnetgame_rec_txn_write_mail(idx, (int)t->slot, NULL)", "slot->rev++;", "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED",
             "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT", "pcnetgame_txn_send_applied(peer, idx, in, slot, (uint8_t)PC_NETGAME_TXN_REASON_NONE", "if (st->rec_push_active) {"]
    check("H the algorithm order: READY gate -> ignore_commit -> binding gate -> SYNCED / world -> shape -> journal (fence, replay, conflict, max_seq) -> base -> pre-image validation -> sender house -> "
          "letter hash -> font -> legal gift -> PLAYER recipient -> host address -> mailbox free -> PO quota -> fail_world -> writable -> COMMIT (copy, bound sender, receive font, "
          "mPO_receipt_proc, mirror slot cleared, rev++, journal) -> kill_peer -> RESULT -> push restart", in_order(hd, order))
    pre_commit = hd[:hd.index("memcpy(&W, L, sizeof(W));")]
    check("H EVERYTHING before the commit is read-only: no mPO_receipt_proc, no mirror write, no rev++, no journal APPLIED, no assignment into Save_Get(...) / private_data / post_office",
          "mPO_receipt_proc(" not in pre_commit and "pcnetgame_rec_txn_write_mail(" not in pre_commit and "slot->rev++" not in pre_commit and "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED" not in pre_commit
          and not re.search(r"(?m)^\s*\*?Save_Get\([^;]*?\)(?:\.\w+|\[[^\]]*\]|->\w+)*\s*=[^=]", pre_commit) and "mMl_copy_mail(" not in pre_commit and "mMl_clear_mail(" not in pre_commit
          and "memcpy(&Save_Get" not in pre_commit and "pcnetgame_rec_txn_write_inventory(" not in pre_commit)
    check("H the letter comes ONLY from the HOST's mirror (L = &Save_Get(private_data)[idx].mail[t->slot]); the message carries no letter bytes: the handler never copies from `in` / `t` into a Mail_c "
          "and only compares its 24-bit hash + gift echo",
          "L = &Save_Get(private_data)[idx].mail[t->slot];" in hd and "memcpy(&W, in" not in hd and "memcpy(&W, t" not in hd and "memcpy(&W, L, sizeof(W));" in hd
          and not re.search(r"\bW\.\w+(?:\.\w+)*\s*=\s*(?:in|t)->", hd))
    check("H the sender is forced to the BOUND identity (st->bound_pid cached at READY, never a message field) with type PLAYER, and the font is the RECEIVE font; the only other W write is none",
          "mPr_CopyPersonalID(&W.header.sender.personalID, &st->bound_pid);" in hd and "W.header.sender.type = (u8)mMl_NAME_TYPE_PLAYER;" in hd
          and len(re.findall(r"\bW\.[\w\.]+\s*=[^=]", hd)) == 2)
    check("H the vanilla predicates are the host's own: mMl_hunt_for_send_address over the HOST's homes, the mailbox free-slot test (24), mPO_count_mail >= HOME_MAILBOX_SIZE / mPO_get_keep_mail_sum() >= mPO_MAIL_STORAGE_SIZE / "
          "a free post_office.mail slot (25)",
          "mPO_get_keep_mail_sum() >= mPO_MAIL_STORAGE_SIZE" in hd and "mMl_chk_mail_free_space(Save_Get(post_office).mail, mPO_MAIL_STORAGE_SIZE) < 0" in hd
          and "PC_NETGAME_TXN_REASON_NO_SUCH_ADDRESS" in hd and "PC_NETGAME_TXN_REASON_MAILBOX_FULL" in hd and "PC_NETGAME_TXN_REASON_PO_FULL" in hd
          and "PC_NETGAME_TXN_REASON_NO_DONOR_SLOT" in hd and "PC_NETGAME_TXN_REASON_STALE_IMAGE" in hd and "PC_NETGAME_TXN_REASON_BAD_IMAGE" in hd)
    check("H a guest / extra player (a bound resident with NO house on the host) is refused with NO_DONOR_SLOT before anything else about the letter is read (source audited: no guest exists today)",
          in_order(hd, ["sh < 0", "PC_NETGAME_TXN_REASON_NO_DONOR_SLOT", "mMl_check_not_used_mail((Mail_c*)L) == TRUE"]))
    check("H a STALE_IMAGE (empty mirror slot / hash mismatch) triggers the paced stale push, like the base check: the client re-syncs from a full push",
          hd.count("pcnetgame_rec_stale_push(") == 2 and "if (stale) {" in hd)
    check("H the gift travels inside the letter only: the handler never writes pockets / conds / wallet (no inventory write; the post-image sent is the mirror's unchanged one)",
          "pcnetgame_rec_txn_write_inventory(" not in hd and "inventory." not in hd.replace("t->pre_", "").replace("pcnetgame_rec_validate_inventory", ""))
    rej_ok = True
    for m in re.finditer(r"pcnetgame_txn_reject\(", hd):
        rest = hd[hd.index(";", m.end()) + 1:].lstrip()
        rej_ok = rej_ok and (rest.startswith("return;") or rest.startswith("if (stale) {") or rest.startswith("pcnetgame_rec_stale_push(") or rest.startswith("pcnetgame_rec_violation(")
                  or (rest.startswith("} else if (old->outcome == (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED) {") and re.search(r'"replay "\);\s*\}\s*return;', hd) is not None))
    check("H a REJECTED path never mutates: every pcnetgame_txn_reject is followed by return (or the paced stale push / the violation count and then return), and mPO_receipt_proc() failing after the checks "
          "rejects PO_FULL with nothing mutated (mPO_receipt_check_mail mutates nothing for a PLAYER letter it refuses)",
          len(re.findall(r"pcnetgame_txn_reject\(", hd)) >= 10 and rej_ok)
    check("H faults: ignore_commit, fail_world and kill_peer_after_commit have exactly ONE site each in the mail handler (drop_result is the shared RESULT sender)",
          sorted(re.findall(r"pcnetgame_txn_fault_fire\((PC_TXN_FAULT_\w+)\)", hd)) == ["PC_TXN_FAULT_FAIL_WORLD", "PC_TXN_FAULT_IGNORE_COMMIT", "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT"])
    wr = func_body(c_raw[x1b:x1e], "pcnetgame_rec_txn_write_mail")
    check("H the mail[] writer pcnetgame_rec_txn_write_mail (X1 block) is the ONLY writer of a mirrored mail[] slot outside the D3 merge: idx must pass pcnetgame_rec_txn_idx_ok (never the host's own resident), "
          "slot < 10, raw mMl_clear_mail / memcpy, two callers (the kind-12 handler with NULL, the kind-13 take handler with the host's letter: audited in test_mail2_src.py)",
          "pcnetgame_rec_txn_idx_ok(idx)" in wr and "mail_slot >= mPr_INVENTORY_MAIL_COUNT" in wr and "mMl_clear_mail(&r->mail[mail_slot]);" in wr
          and c.count("pcnetgame_rec_txn_write_mail(") == 3 and hd.count("pcnetgame_rec_txn_write_mail(idx, (int)t->slot, NULL)") == 1
          and len(re.findall(r"(?:->|\.)mail\[[^\]]*\]\s*=[^=]", c)) == 0 and len(re.findall(r"mMl_clear_mail\(&r->mail", c)) == 1
          and "mMl_clear_mail(&Save_Get(private_data)" not in c and "Save_Get(private_data)[idx].mail" not in strip_comments(c_raw[d3b:d3e]).replace("&Save_Get(private_data)[idx].mail[t->slot]", ""))
    check("H the hash helper converts a ZEROED scratch Private_c holding one letter copy (no live record is touched); both sides use it",
          "static uint32_t pcnetgame_mail_be_hash(const Mail_c* m)" in c and "memset(&s_mail_scratch, 0, sizeof(s_mail_scratch));" in c
          and c.count("pcnetgame_mail_be_hash(") >= 8)
    vf = func_body(d3, "pcnetgame_rec_validate_fields")
    check("H the D3 upload validator checks mail[].present of every USED letter (font != 0xFF): EMPTY_NO / RSV_NO / pocket-legal, else INVALID_FIELD MAIL_PRESENT | slot << 8; python has the same code",
          "n->mail[i].content.font != 0xFF" in vf and "pr != (mActor_name_t)EMPTY_NO && pr != (mActor_name_t)RSV_NO && !pcnetgame_is_pocket_legal_item(pr)" in vf
          and "PC_NETGAME_REC_FIELD_MAIL_PRESENT | ((uint16_t)i << 8)" in vf and vf.index("PC_NETGAME_REC_FIELD_ORG_TABLE") < vf.index("PC_NETGAME_REC_FIELD_MAIL_PRESENT"))
    check("H the delivery is plain vanilla on the host: the handler calls mPO_receipt_proc once and nothing else delivers (the postman / mPO_business_proc stay the only delivery path)",
          hd.count("mPO_receipt_proc(") == 1 and "mPO_delivery" not in hd and "mHS_get_pl_no" not in hd)

    # ------------------------------------------------------------------ C: client
    check("C AWAIT_CLEAN is a state of its own (s_mail_op): it is NOT pcnetgame_txn_busy() and NOT in pcnetgame_crec_upload_deferred() (the upload flush must keep running), but it IS in pc_net_game_client_pocket_locked()",
          "return s_ctxn.state != PC_NETGAME_CTXN_FREE;" in func_body(c_raw, "pcnetgame_txn_busy") and "s_mail_op" not in func_body(c_raw, "pcnetgame_txn_busy")
          and "s_mail_op" not in func_body(c_raw, "pcnetgame_crec_upload_deferred") and "s_mail_op" not in func_body(c_raw, "pcnetgame_crec_adopt_blocker")
          and "(s_mail_op.active && !s_mail_op.done)" in func_body(c_raw, "pc_net_game_client_pocket_locked"))
    clean = func_body(mc_raw, "pcnetgame_mail_state_clean")
    check("C the clean predicate: SYNCED, a held acked digest, no upload in flight / staged push / stopped uploads, the owner stamp, and cdig of Now_Private == the acked cdig",
          all(x in clean for x in ("s_crec.state != PC_NETGAME_CRS_SYNCED", "!s_crec.acked_valid", "s_crec.up_active", "s_crec.up_blocked", "s_crec.st_valid", "pcnetgame_owner_stamp_matches(&s_crec.owner)",
                                   "pcnetgame_crec_now_cdig(&c)", "c == s_crec.acked_cdig")))
    tick = func_body(mc_raw, "pcnetgame_mail_tick")
    check("C the AWAIT_CLEAN poll is BOUNDED (PC_NETGAME_MAIL_CLEAN_TIMEOUT_MS = 5000) and only begins the kind-12 transaction when clean; a changed letter / owner / stopped uploads refuse locally; "
          "it runs once per client poll next to the transaction tick",
          "#define PC_NETGAME_MAIL_CLEAN_TIMEOUT_MS 5000u" in c_raw and "PC_NETGAME_MAIL_CLEAN_TIMEOUT_MS" in tick
          and in_order(tick, ["PC_NETGAME_MAIL_CLEAN_TIMEOUT_MS", "!pcfa_save_ready()", "pcnetgame_owner_stamp_matches(&s_mail_op.owner)", "(pcnetgame_mail_be_hash(ml) & 0xFFFFFFu) != s_mail_op.h24",
                              "s_crec.up_blocked", "!pcnetgame_mail_state_clean()", "s_ctxn.kind = (uint8_t)PC_NETGAME_TXN_KIND_MAIL_SEND;", "pcnetgame_txn_try_send()"])
          and "pcnetgame_mail_tick(); /* mail milestone 1" in c_raw and c_raw.index("pcnetgame_txn_tick()) { /* X1b") < c_raw.index("pcnetgame_mail_tick(); /* mail milestone 1"))
    ts = func_body(xc_raw, "pcnetgame_txn_try_send")
    mcase = ts[ts.index("case PC_NETGAME_TXN_KIND_MAIL_SEND: {"):ts.index("case PC_NETGAME_TXN_KIND_MAIL_TAKE: {")]
    check("C try_send (QUEUED -> SENT) for kind 12 re-checks that the slot still holds the same send-font letter (hash24) and that the record is STILL clean (cancel, never wait: a non-clean QUEUED txn "
          "would block the uploads forever), echoes the gift and fills aux_cond / aux_item from the stored hash",
          "pcnetgame_mail_be_hash(ml) & 0xFFFFFFu" in mcase and "pcnetgame_mail_state_clean()" in mcase and mcase.count("pcnetgame_txn_cancel_queued(") == 3
          and "item = (uint16_t)ml->present;" in mcase and "aux_cond = T->ts_aux;" in mcase and "aux_item = T->ts_aux_item;" in mcase
          and re.search(r"T->kind == \(uint8_t\)PC_NETGAME_TXN_KIND_MAIL_SEND \|\| T->kind == \(uint8_t\)PC_NETGAME_TXN_KIND_MAIL_TAKE;", ts) is not None)
    am = func_body(xc_raw, "pcnetgame_txn_apply_mail")
    check("C APPLIED of kind 12 (pcnetgame_txn_apply_mail): the sent letter leaves the slot ONLY here, and only if the slot still hashes to the sent letter (else kept + logged 'possible local divergence'); "
          "pockets / wallet are never touched; the D3 base moves forward only on the same session / epoch",
          "mMl_clear_mail(&Now_Private->mail[t->slot]);" in am and "== want24" in am and "possible local divergence" in am and "inventory." not in am and "mPr_Set" not in am and "mPr_Give" not in am
          and "in->host_session == s_crec.base_session && in->epoch == s_crec.base_epoch && in->rev > s_crec.base_rev" in am and am.count("pcnetgame_crec_set_base(") == 1
          and am.index("possible local divergence") < am.index("return 1;") < am.index("pcnetgame_crec_set_base("))
    aa = func_body(xc_raw, "pcnetgame_txn_apply_applied")
    check("C apply_applied routes kind 12 to apply_mail right after the owner stamp check, before the shop / pocket logic",
          in_order(aa, ["pcnetgame_owner_stamp_matches(&T->owner)", "if (T->kind == (uint8_t)PC_NETGAME_TXN_KIND_MAIL_SEND) {", "return pcnetgame_txn_apply_mail(T, in);", "PC_NETGAME_TXN_KIND_SHOP_BUY"]))
    all_mail_writes = re.findall(r"mMl_clear_mail\(&Now_Private->mail\[[^\]]*\]\)|mMl_copy_mail\(&Now_Private->mail|Now_Private->mail\[[^\]]*\]\s*=[^=]", c)
    check("C PC_NETGAME-side client writes of Now_Private->mail[]: exactly ONE clear (apply_mail, APPLIED only); the only other writes are the TEST-ONLY hook's letter (mMl_init_mail / present) and the D3 adopt memcpy",
          len(re.findall(r"mMl_clear_mail\(&Now_Private->mail", c)) == 1 and "mMl_copy_mail(&Now_Private->mail" not in c
          and len(re.findall(r"mMl_init_mail\(", c)) == 1 and len(re.findall(r"m->present = it;", c)) == 1)
    api = "".join(func_body(mc_raw, n) for n in ("pc_net_game_mail_begin_send", "pc_net_game_mail_poll", "pcnetgame_mail_tick", "pcnetgame_mail_op_resolve", "pcnetgame_mail_state_clean"))
    check("C no client write of mail[] or a mailbox before APPLIED: begin / poll / tick / resolve / the result handler contain no mMl_clear_mail / mMl_copy_mail / mail[] assignment / homes write",
          "mMl_clear_mail(" not in api and "mMl_copy_mail(" not in api and not re.search(r"mail\[[^\]]*\]\s*=[^=]", api) and "homes" not in api and "mailbox" not in api
          and "mMl_clear_mail(" not in func_body(xc_raw, "pcnetgame_handle_client_txn_result") and "mMl_copy_mail(" not in func_body(xc_raw, "pcnetgame_handle_client_txn_result"))
    bs = func_body(mc_raw, "pc_net_game_mail_begin_send")
    check("C begin_send refuses unless READY + SYNCED + uploads not stopped, the slot holds a used send-font PLAYER-addressed letter, and no other operation / transaction is unresolved (-1); it never changes the letter",
          in_order(bs, ["s_role != PC_NETGAME_ROLE_CLIENT", "s_mail_op.active || s_ts_op.active || pcnetgame_txn_busy()", "s_crec.state != PC_NETGAME_CRS_SYNCED || s_crec.up_blocked",
                        "!mMl_check_send_mail((Mail_c*)ml)", "pcnetgame_capture_owner_stamp(&stamp)", "s_crec.next_check_ms = 0;"])
          and "ml->header.recipient.type != (u8)mMl_NAME_TYPE_PLAYER" in bs and "mMl_clear_mail(" not in bs and "->mail[" not in strip_comments(bs).replace("Now_Private->mail[slot]", ""))
    check("C a REJECTED / refused result leaves the letter: resolve() only records done / outcome / reason; the result handler's REJECTED branch changes no inventory (kinds are shared with the shop / museum seams)",
          "s_mail_op.done = 1;" in func_body(mc_raw, "pcnetgame_mail_op_resolve") and "if (kind == (uint8_t)PC_NETGAME_TXN_KIND_MAIL_SEND) {\n        pcnetgame_mail_op_resolve(request_id, applied);" in c_raw)
    check("C session reset clears the operation (a link loss is a local refusal: the letter never left its slot) and pc_net_game_mail_poll() reports REJECTED then",
          "memset(&s_mail_op, 0, sizeof(s_mail_op));" in func_body(c_raw, "pcnetgame_reset_client_session_state") and "memset(&s_mail_op, 0, sizeof(s_mail_op));" in func_body(mc_raw, "pc_net_game_mail_poll"))
    # ---- seams
    cd = func_body(pg, "aPG_check_destination")
    step = func_body(pg, "aPG_client_mail_step")
    cw = func_body(pg, "aPG_receive_menu_close_wait")
    ini = func_body(pg, "aPG_receive_menu_close_wait_init")
    check("C seam aPG_check_destination: a client + PLAYER letter passes iff the LOCAL address lookup finds a house (no stale mailbox / quota checks); client + MUSEUM + gift is still refused; both under TARGET_PC",
          "return (mMl_hunt_for_send_address(mail) == -1) ? 1 : 0;" in cd and "mail->header.recipient.type == mMl_NAME_TYPE_MUSEUM && mail->present != EMPTY_NO && mail->present != RSV_NO" in cd
          and cd.index("#ifdef TARGET_PC") < cd.index("pc_net_game_role() == PC_NETGAME_ROLE_CLIENT") < cd.index("switch (mail->header.recipient.type)"))
    check("C seam aPG_client_mail_step: restores the letter (send font, byte-identical to the refusal copy-back) into its slot BEFORE begin_send, waits NEUTRALLY while PENDING (-1: no state change), "
          "APPLIED -> 0, MAILBOX_FULL -> 2, PO_FULL -> 3, every other reason / local refusal -> 1",
          in_order(step, ["aPG_check_destination(mail)", "cmp.content.font = mMl_FONT_SEND;", "memcmp(&cmp, &Now_Private->mail[item_p->slot_no], sizeof(Mail_c)) != 0", "return 1;", "pc_net_game_mail_begin_send(item_p->slot_no)",
                          "s_aPG_client_mail_active = 1;", "pc_net_game_mail_poll()", "return 0;", "PC_NETGAME_MAIL_REJECT_MAILBOX_FULL", "return 2;", "PC_NETGAME_MAIL_REJECT_PO_FULL", "return 3;", "default:\n        return 1;"])
          and step.count("return -1;") == 3 and "if (r == PC_NETGAME_TS_OP_PENDING) {\n        return -1;" in step and "if (r == 0) {\n            return 1;" in step
          and "mMl_copy_mail(&Now_Private->mail" not in step and "mPO_" not in step and "mMl_clear_mail(" not in step)  # review: the step COMPARES the slot with the letter, it never writes mail[]
    check("C seam close_wait: a client's PLAYER letter takes aPG_client_mail_step (-1 -> return, no message, no state change); the LOCAL mPO_receipt_proc() is skipped for it (the letter was removed by the host's APPLIED); "
          "every other recipient / host / solo keeps the vanilla calls; the refusal copy-back still restores the letter (idempotent); the in-flight static is reset in _init",
          "dest = client_mail ? aPG_client_mail_step(play) : aPG_check_destination(&play->submenu.mail);" in cw and "if (dest < 0) {\n                return;" in cw
          and "if (!client_mail)" in cw and cw.index("if (!client_mail)") < cw.index("mPO_receipt_proc(&play->submenu.mail, mPO_SENDTYPE_MAIL);")
          and "if (!client_mail) /* a client's player letter never left its slot" in cw and cw.index("never left its slot") < cw.index("mMl_copy_mail(&Now_Private->mail[submenu_item->slot_no], &play->submenu.mail);")
          and "dest = aPG_check_destination(&play->submenu.mail);" in cw
          and "s_aPG_client_mail_active = 0;" in ini and "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && play->submenu.mail.header.recipient.type == mMl_NAME_TYPE_PLAYER" in cw)
    sp = func_body(tag, "mTG_send_proc")
    check("C seam mTG_send_proc: a client's PLAYER letter is COPIED into submenu->mail (receive font there only) and stays in its pocket slot (no empty-slot upload); every other letter takes the unchanged vanilla "
          "font / copy / clear sequence",
          "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && mail->header.recipient.type == mMl_NAME_TYPE_PLAYER" in sp and "submenu->mail.content.font = mMl_FONT_RECV;" in sp
          and "mail->content.font = mMl_FONT_RECV;\n        mMl_copy_mail(&submenu->mail, mail);\n        mMl_clear_mail(mail);" in sp
          and sp.index("} else\n#endif") < sp.index("mMl_clear_mail(mail);"))
    check("C the API is declared in pc_net_game.h (begin / poll / last_reason, the three MAIL_REJECT codes equal the C reasons) and the post girl file includes it under TARGET_PC",
          all(x in h for x in ("int pc_net_game_mail_begin_send(int mail_slot);", "int pc_net_game_mail_poll(void);", "int pc_net_game_mail_last_reason(void);",
                               "#define PC_NETGAME_MAIL_REJECT_NO_SUCH_ADDRESS 23", "#define PC_NETGAME_MAIL_REJECT_MAILBOX_FULL    24", "#define PC_NETGAME_MAIL_REJECT_PO_FULL         25"))
          and '#ifdef TARGET_PC\n#include "pc_net_game.h"' in read("src/actor/npc/ac_npc_post_girl.c"))
    check("C the museum seam is untouched: client MUSEUM gift refusal kept, no mMsm_ call anywhere in the mail host / client blocks (museum letters wait for M3)",
          "mMsm_" not in mh and "mMsm_" not in mc and "mMsm_SendMuseumMail" not in c)

    blocked = func_body(c_raw, "pcnetgame_txn_begin_blocked")
    check("C one pocket-transaction token: every request-side begin check (pickup / drop / bury / grants / catch / field actions / town services) uses pcnetgame_txn_begin_blocked() = s_ctxn busy OR a MAIL_SEND in AWAIT_CLEAN; "
          "pcnetgame_txn_busy() itself (upload deferral, adopt blocker) is unchanged",
          "(s_mail_op.active && !s_mail_op.done)" in blocked and "(s_take_op.active && !s_take_op.done)" in blocked and "s_ctxn.state != PC_NETGAME_CTXN_FREE" in blocked and c.count("pcnetgame_txn_begin_blocked()") == 9
          and "pcnetgame_txn_begin_blocked()" in func_body(c_raw, "pcnetgame_ts_begin") and "s_mail_op" not in func_body(c_raw, "pcnetgame_txn_busy"))
    check("T the mail test hooks are role-bound in pc_main.c like --txn-fault: --mail-test-force-delivery / --mail-test-poke-museum refuse (exit 2) unless --host, --mail-test-send unless --connect",
          "g_pc_mail_test_force_delivery || g_pc_mail_test_poke_museum >= 0 || g_pc_mail_test_seed_mailbox != NULL || g_pc_mail_test_seed_reply != NULL) &&" in main_c.replace("\n        g_pc_net_role != 1", " g_pc_net_role != 1").replace("\n", " ")
          and "(g_pc_mail_test_send != NULL || g_pc_mail_test_take != 0) && g_pc_net_role != 2" in main_c
          and main_c.count("[NET][MAIL][TEST-ONLY] REFUSED:") == 2)  # mail milestone 2: + --mail-test-seed-mailbox / --mail-test-seed-reply (host) and --mail-test-take (client)

    # ------------------------------------------------------------------ T: hooks
    check("T --mail-test-send=<house>[,gift]: default NULL, exact-prefix arm with a loud [TEST-ONLY] line, documented in --help and pc_platform.h, CLIENT-gated, bypasses only the letter board "
          "(its one local write is the letter), calls the REAL begin_send / poll, loud step logs, checks nothing changed before APPLIED",
          "const char* g_pc_mail_test_send = NULL;" in main_c and 'strncmp(argv[i], "--mail-test-send=", 17) == 0' in main_c and "[NET][MAIL][TEST-ONLY] --mail-test-send=%s armed" in main_c
          and "--mail-test-send=HOUSE[,gift]" in main_c and "--mail-test-send=<house>[,gift]" in plat_h and "extern const char*   g_pc_mail_test_send;" in plat_h
          and "s_role != PC_NETGAME_ROLE_CLIENT" in func_body(mc_raw, "pcnetgame_run_mail_test_hook") and "pc_net_game_mail_begin_send(s_slot)" in mc and "pc_net_game_mail_poll()" in mc
          and "MAIL SLOT CHANGED BEFORE APPLIED" in mc and "POCKET CHANGED BEFORE APPLIED" in mc and mc.count("[NET][MAIL][TEST-ONLY]") >= 8)
    check("T --mail-test-force-delivery (HOST only) runs the REAL vanilla mPO_delivery_one_address() and logs the mailboxes; --mail-test-poke-museum=<resident> (HOST only) bumps stored_fossil_num once; "
          "both default off, loud, documented, role-gated, no getenv",
          "int g_pc_mail_test_force_delivery = 0;" in main_c and "int g_pc_mail_test_poke_museum = -1;" in main_c and 'strncmp(argv[i], "--mail-test-poke-museum=", 24) == 0' in main_c
          and "s_role != PC_NETGAME_ROLE_HOST" in func_body(mh_raw, "pcnetgame_mail_test_force_delivery") and "mPO_delivery_one_address(h)" in mh
          and "s_role != PC_NETGAME_ROLE_HOST" in func_body(mh_raw, "pcnetgame_mail_test_poke_museum") and "getenv" not in mh and "getenv" not in mc
          and "--mail-test-force-delivery" in plat_h and "--mail-test-poke-museum=<resident idx>" in plat_h and "--mail-test-force-delivery  --mail-test-poke-museum=N" in main_c)  # lifecycle hardening: the hook flags are listed on the `Test builds only` --help line
    check("T the hooks are called once per poll from the one place the other test hooks are (a complete no-op unless armed)",
          "pcnetgame_run_mail_test_hook(); /* mail milestone" in c_raw and "pcnetgame_mail_test_force_delivery(); /* mail milestone" in c_raw and "pcnetgame_mail_test_poke_museum(); /* mail milestone R" in c_raw)

    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
