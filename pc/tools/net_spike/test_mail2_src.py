#!/usr/bin/env python3
"""test_mail2_src.py - SOURCE AUDIT of Mail milestone 2: the host-held house mailbox reaches its OWNING client (MAILBOX_LETTER, id 56), the client takes a
letter through the host (TXN_COMMIT kind 13 MAIL_TAKE), the mailbox UI opens for a client ONLY when it is host-fed, and the host generates villager replies
for every connected resident (M2r).

Pure text checks over pc/src/pc_net_game.c, src/actor/ac_mailbox_move.c_inc, src/game/m_tag_ovl.c, m_npc.c, pc/src/pc_main.c, pc/include/pc_platform.h /
pc_net_game.h and the python library. No game process. Tier: SOURCE AUDITED (the overlay / actor seams are not UI-tested; the host handler, the sender and
the client API are runtime-tested by test_mail2_protocol.py / test_mail2_real_client.py).

  W  wire: id 56 + the 316 B struct + kind 13 + reasons 26 / 27 equal in C and python; TXN_COMMIT / TXN_RESULT unchanged; wire_baseline green
  S  host sender: per-resident digest / seq table, owner-only delivery (the resident index comes from the binding, never from a message), bounded, a
     client-originated 56 is dropped
  H  host take handler: algorithm order, read-only checks before the single commit, the letter is read ONLY from the host's mailbox, the single writers
     of the mirror mail[] and of the host mailbox, faults
  C  client: MAILBOX_LETTER validation order, the shadow is written ONLY by the verified handler (+ the take apply / the revert), host-fed state and its
     reset, no mailbox-to-mail[] path before APPLIED, the one place a mailbox letter enters mail[] (the take apply), session reset
  O  UI seams: the mailbox actor / overlay open for a client ONLY when host-fed (precise gating), the transfer code goes through the host, the discard is
     refused, the M0 gates that must stay
  R  M2r: mNpc_RemailFor, the client no longer runs the load-time remail, the host pass for every connected resident (once per day), the known
     resident-dependent limitations pinned
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
    tag = read("src/game/m_tag_ovl.c")
    npc = read("src/game/m_npc.c")
    npc_h = read("include/m_npc.h")
    priv = read("src/game/m_private.c")
    mail_c = read("src/game/m_mail.c")
    mbx = read("src/actor/ac_mailbox_move.c_inc")
    evm = read("src/actor/ac_event_manager.c")
    m2hb, m2he = c_raw.index("===== MAIL2 HOST BEGIN"), c_raw.index("===== MAIL2 HOST END")
    m2h_raw = c_raw[m2hb:m2he]
    m2h = strip_comments(m2h_raw)
    m2cb, m2ce = c_raw.index("===== MAIL2 CLIENT BEGIN"), c_raw.index("===== MAIL2 CLIENT END")
    m2c_raw = c_raw[m2cb:m2ce]
    m2c = strip_comments(m2c_raw)
    hd_raw = func_body(m2h_raw, "pcnetgame_handle_host_mail_take_txn")
    hd = strip_comments(hd_raw)
    xcb, xce = c_raw.index("===== X1b CLIENT BEGIN"), c_raw.index("===== X1b CLIENT END")
    xc_raw = c_raw[xcb:xce]
    d3b, d3e = c_raw.index("===== D3 BEGIN"), c_raw.index("===== D3 END")
    d3_raw = c_raw[d3b:d3e]
    x1b, x1e = c_raw.index("===== X1 BEGIN"), c_raw.index("===== X1 END")

    # ------------------------------------------------------------------ W: wire
    ids = dict(wire_baseline.c_message_ids(c_raw))
    check("W id 56 = MAILBOX_LETTER is the highest message id (wire_baseline.EXPECTED_MAX_MSG_ID = %d); 55 = TOWN_SVC_STATE is unchanged; ids stay contiguous" % wire_baseline.EXPECTED_MAX_MSG_ID,
          ids.get("PC_NETGAME_MSG_MAILBOX_LETTER") == 56 and ids.get("PC_NETGAME_MSG_TOWN_SVC_STATE") == 55 and wire_baseline.EXPECTED_MAX_MSG_ID == 66
          and sorted(ids.values()) == list(range(1, 67)) and L.PC_NETGAME_MSG_MAILBOX_LETTER == 56)  # guests G1 appended 57 / 58 after it
    mb = c_raw[c_raw.index("typedef struct PCNetGameMailboxLetterMsg {"):c_raw.index("} PCNetGameMailboxLetterMsg;")]
    check("W PCNetGameMailboxLetterMsg: u8 type, u8 house, u8 mbox_idx, u8 flags, u32 seq, u32 digest, u16 used_count, u16 _rsv0, letter[298], u16 _rsv1; sizeof == 316, offsets 4 / 8 / 12 / 16 / 314, "
          "<= PC_NET_MAX_PAYLOAD, letter size == sizeof(Mail_c), slot count == HOME_MAILBOX_SIZE",
          " ".join(strip_comments(mb).split()) == ("typedef struct PCNetGameMailboxLetterMsg { uint8_t msg_type; uint8_t house; uint8_t mbox_idx; uint8_t flags; uint32_t seq; uint32_t digest; "
                                                   "uint16_t used_count; uint16_t _rsv0; uint8_t letter[PC_NETGAME_MAIL_WIRE_SIZE]; uint16_t _rsv1;")
          and "_Static_assert(sizeof(PCNetGameMailboxLetterMsg) == 316," in c_raw
          and "offsetof(PCNetGameMailboxLetterMsg, seq) == 4 && offsetof(PCNetGameMailboxLetterMsg, digest) == 8 &&" in c_raw and "offsetof(PCNetGameMailboxLetterMsg, letter) == 16" in c_raw
          and "offsetof(PCNetGameMailboxLetterMsg, _rsv1) == 314" in c_raw and "_Static_assert(sizeof(PCNetGameMailboxLetterMsg) <= PC_NET_MAX_PAYLOAD," in c_raw
          and "PC_NETGAME_MAIL_WIRE_SIZE == sizeof(Mail_c) && PC_NETGAME_MBOX_SLOTS == HOME_MAILBOX_SIZE" in c_raw)
    cm = dict(re.findall(r"#define (PC_NETGAME_(?:TXN_KIND_MAIL_TAKE|TXN_REASON_NO_SUCH_LETTER|TXN_REASON_MAIL_CHANGED|MBOX_SLOTS|MBOX_FLAG_EMPTY|MAIL_WIRE_SIZE))\s+(0x[0-9A-Fa-f]+|\d+)u", c_raw))
    check("W kind 13 MAIL_TAKE, reasons 26 NO_SUCH_LETTER / 27 MAIL_CHANGED, 10 slots, flag EMPTY 0x01, 298 B letter: C == net_spike_lib (MAILBOX_FULL 24 is reused for 'no free mail slot')",
          cm == {"PC_NETGAME_TXN_KIND_MAIL_TAKE": "13", "PC_NETGAME_TXN_REASON_NO_SUCH_LETTER": "26", "PC_NETGAME_TXN_REASON_MAIL_CHANGED": "27", "PC_NETGAME_MBOX_SLOTS": "10",
                 "PC_NETGAME_MBOX_FLAG_EMPTY": "0x01", "PC_NETGAME_MAIL_WIRE_SIZE": "298"}
          and L.PC_NETGAME_TXN_KIND_MAIL_TAKE == 13 and (L.PC_NETGAME_TXN_REASON_NO_SUCH_LETTER, L.PC_NETGAME_TXN_REASON_MAIL_CHANGED) == (26, 27)
          and L.TXN_REASON_NAMES[26] == "NO_SUCH_LETTER" and L.TXN_REASON_NAMES[27] == "MAIL_CHANGED" and (L.PC_NETGAME_MBOX_SLOTS, L.PC_NETGAME_MBOX_FLAG_EMPTY, L.PC_NETGAME_MAIL_WIRE_SIZE) == (10, 1, 298))
    check("W python spec: MAILBOX_LETTER_FMT '<BBBBIIHH298sH' is 316 B, registered in GAME_SPECS under 56; TXN_COMMIT (72 B) / TXN_RESULT (76 B) keep their exact size asserts (the take adds no field)",
          L.MAILBOX_LETTER_FMT == "<BBBBIIHH298sH" and L.MAILBOX_LETTER_SPEC.size == 316 and L.GAME_SPECS[56] is L.MAILBOX_LETTER_SPEC
          and "_Static_assert(sizeof(PCNetGameTxnCommitMsg) == 72," in c_raw and "_Static_assert(sizeof(PCNetGameTxnResultMsg) == 76," in c_raw)
    check("W the kind-13 tag convention of the doc comment: dest NONE, slot = MAILBOX slot, flags = destination mail slot, item = gift echo, aux_item = low 16 / aux_cond = bits 16..23 of the 24-bit hash",
          "tag.dest NONE, tag.slot = the MAILBOX slot (0..9)," in c_raw and "tag.flags = the destination mail[] slot" in c_raw and "tag.aux_item = the LOW 16 bits and tag.aux_cond = bits 16..23" in c_raw
          and "want24 = ((uint32_t)t->aux_cond << 16) | (uint32_t)t->aux_item;" in hd and "dst = (int)t->flags;" in hd)
    check("W the message doc says host -> the OWNING client ONLY, RELIABLE, never persisted", "host -> the OWNING READY client ONLY, RELIABLE," in c_raw and "NEVER persisted" in c_raw.replace("never persisted", "NEVER persisted"))
    wire_baseline.run(lambda desc, cond: check("W " + desc, cond), ROOT)

    # ------------------------------------------------------------------ S: host sender
    check("S the host table is per (resident, slot): PLAYER_NUM x 10 entries of {valid, empty, used, seq, digest, letter[298]}; seq is process-wide (never reset)",
          "static PCNetGameMboxHost s_mbox_host[PLAYER_NUM][PC_NETGAME_MBOX_SLOTS];" in c and "X->seq++;" in c
          and "s_mbox_host" not in func_body(c_raw, "pcnetgame_reset_all_host_peer_state") and "memset(s_mbox_host" not in c)
    rf = func_body(m2h_raw, "pcnetgame_mbox_refresh_resident")
    check("S the digest is the FNV-1a32 of the 298 canonical BE bytes (the same bytes as the wire letter); an EMPTY slot is the canonical cleared letter (so garbage in an unused slot never resends); seq bumps ONLY on a "
          "digest / empty change; the house is the one the resident's PersonalID owns (none -> no mailbox)",
          in_order(rf, ["pcnetgame_mbox_house_of(&Save_Get(private_data)[idx].player_ID)", "if (h < 0) {", "pcnetgame_mbox_empty_be()", "pcnetgame_mail_to_be(m, be);", "pcnetgame_fnv1a32(be, sizeof(be))",
                        "if (!X->valid || X->digest != dig || X->empty != (uint8_t)empty) {", "X->seq++;"])
          and "mMl_check_not_used_mail(m) == TRUE" in rf)
    tk = func_body(m2h_raw, "pcnetgame_host_mbox_tick")
    check("S the sender runs only for a READY + bound peer whose binding is still valid (resident record exists, PersonalID equal) and NOT the host's own resident, and only when that resident owns a house (a guest / extra "
          "player gets no mailbox); the resident index is st->bound_resident_idx (the binding), never a message field",
          in_order(tk, ["s_role != PC_NETGAME_ROLE_HOST || !s_host_world_ready", "s_host_peer_link[p] != PC_NETGAME_LINK_READY || !st->bound_valid", "idx = st->bound_resident_idx;",
                        "idx < 0 || idx >= PLAYER_NUM || (own >= 0 && idx == own)", "mPr_CheckCmpPersonalID(&st->bound_pid, &Save_Get(private_data)[idx].player_ID) != TRUE",
                        "pcnetgame_mbox_house_of(&st->bound_pid) < 0", "pcnetgame_mbox_refresh_resident(idx);", "pcnetgame_mbox_send((PCNetPeerId)p, idx, i)"])
          and "bound_resident_idx" in tk and "data[" not in tk)
    check("S bounded and paced: the digest poll is every PC_NETGAME_MBOX_CHECK_MS (500), at most PC_NETGAME_MBOX_PER_POLL (4) messages per peer per host poll, a failed send is retried next poll (sent seq not advanced), "
          "a (re)binding resets the per-peer sent seqs; the per-peer state is part of the memset-per-connection peer state",
          "#define PC_NETGAME_MBOX_CHECK_MS 500u" in c_raw and "#define PC_NETGAME_MBOX_PER_POLL 4" in c_raw and "sent < PC_NETGAME_MBOX_PER_POLL" in tk
          and in_order(tk, ["if (!pcnetgame_mbox_send(", "break;", "st->mbox_sent_seq[i] = X->seq;"]) and "st->mbox_res_plus1 != idx + 1" in tk and "memset(st->mbox_sent_seq, 0" in tk
          and "uint32_t             mbox_sent_seq[PC_NETGAME_MBOX_SLOTS];" in c_raw and "int                  mbox_res_plus1;" in c_raw)
    sd = func_body(m2h_raw, "pcnetgame_mbox_send")
    check("S the message is built ONLY from the host table (s_mbox_host) and the owner's house, RELIABLE, exactly sizeof(PCNetGameMailboxLetterMsg); the sender is the only place that writes msg_type MAILBOX_LETTER",
          "const PCNetGameMboxHost* X = &s_mbox_host[idx][i];" in sd and "pc_net_send(peer, PC_NET_RELIABLE, &m, (uint16_t)sizeof(m))" in sd and c.count("PC_NETGAME_MSG_MAILBOX_LETTER") == 4
          and "m.msg_type = (uint8_t)PC_NETGAME_MSG_MAILBOX_LETTER;" in sd and sd.count("pc_net_send(") == 1)
    hdl = func_body(c_raw, "pcnetgame_handle_host_data")
    check("S a client-originated MAILBOX_LETTER is dropped by the host (never applied, never relayed) before any handler",
          "data[0] == (uint8_t)PC_NETGAME_MSG_MAILBOX_LETTER" in hdl and "client-originated MAILBOX_LETTER" in hdl
          and hdl.index("PC_NETGAME_MSG_MAILBOX_LETTER") < hdl.index("pcnetgame_handle_host_ts_txn(peer, &tc)") and "pcnetgame_handle_client_mailbox_letter" not in hdl)
    check("S the host poll calls the sender and the M2r pass next to the town-service tick (host role only, after the world is ready)",
          "pcnetgame_host_mbox_tick();   /* mail milestone 2" in c_raw and "pcnetgame_host_remail_tick(); /* mail milestone 2" in c_raw
          and c_raw.index("pcnetgame_host_ts_tick();     /* town services") < c_raw.index("pcnetgame_host_mbox_tick();") < c_raw.index("pcnetgame_host_world_poll();"))

    # ------------------------------------------------------------------ H: take handler
    check("H the handler exists in the MAIL2 HOST block and the dispatcher routes exactly kind 13 to it (after the mail-send kind, before the X1 handler); one definition + one call",
          re.search(r"else if \(tc\.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_MAIL_TAKE\) \{\s*pcnetgame_handle_host_mail_take_txn\(peer, &tc\);", c_raw) is not None
          and c.count("pcnetgame_handle_host_mail_take_txn(") == 2)
    order = ["s_host_peer_link[peer] != PC_NETGAME_LINK_READY", "PC_TXN_FAULT_IGNORE_COMMIT", "pcnetgame_rec_gate(peer, 0, 0)", "st->rec_state != PC_NETGAME_RECS_SYNCED",
             "shape_ok = in->kind == (uint8_t)PC_NETGAME_TXN_KIND_MAIL_TAKE", "pcnetgame_txn_nonce_fenced(", "pcnetgame_txn_journal_find(", "R->max_seq = t->txn_seq;",
             "pcnetgame_rec_refresh_hostfields(idx, slot);", "t->base_epoch != slot->epoch", "pcnetgame_rec_validate_inventory(t->pre_pockets",
             "h = pcnetgame_mbox_house_of(&Save_Get(private_data)[idx].player_ID);", "L = &Save_Get(homes[h]).mailbox[t->slot];", "mMl_check_not_used_mail((Mail_c*)L) == TRUE",
             "PC_NETGAME_TXN_REASON_NO_SUCH_LETTER", "!= want24", "PC_NETGAME_TXN_REASON_MAIL_CHANGED", "L->present != (mActor_name_t)t->item", "mMl_FONT_NUM",
             "pcnetgame_is_pocket_legal_item(L->present)", "mMl_check_not_used_mail(&Save_Get(private_data)[idx].mail[dst]) != TRUE", "PC_NETGAME_TXN_REASON_MAILBOX_FULL",
             "PC_TXN_FAULT_FAIL_WORLD", "pcnetgame_rec_txn_idx_ok(idx)", "memcpy(&W, L, sizeof(W));", "pcnetgame_rec_txn_write_mail(idx, dst, &W);",
             "mMl_clear_mail(&Save_Get(homes[h]).mailbox[t->slot]);", "slot->rev++;", "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED",
             "pcnetgame_mbox_refresh_resident(idx);", "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT", "pcnetgame_txn_send_applied(peer, idx, in, slot, (uint8_t)PC_NETGAME_TXN_REASON_NONE", "if (st->rec_push_active) {"]
    check("H the algorithm order: READY gate -> ignore_commit -> binding gate -> SYNCED / world -> shape -> journal (fence, replay, conflict, max_seq) -> base -> pre-image validation -> the owner's house -> "
          "mailbox slot used (26) -> hash (27) -> gift echo -> font / recipient -> legal gift -> mirror mail[dst] free (24) -> fail_world -> writable -> COMMIT (copy, mirror write, mailbox slot cleared, rev++, journal, "
          "mailbox seq bump) -> kill_peer -> RESULT -> push restart", in_order(hd, order))
    pre_commit = hd[:hd.index("memcpy(&W, L, sizeof(W));")]
    check("H EVERYTHING before the commit is read-only: no mirror write, no mailbox write, no rev++, no journal APPLIED, no mMl_clear_mail / mMl_copy_mail, no assignment into Save_Get(...) / private_data / homes "
          "(the only state touched is the per-peer mbox_sent_seq re-push request of a REJECTED stale claim)",
          "pcnetgame_rec_txn_write_mail(" not in pre_commit and "slot->rev++" not in pre_commit and "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED" not in pre_commit
          and "mMl_clear_mail(" not in pre_commit and "mMl_copy_mail(" not in pre_commit and "memcpy(&Save_Get" not in pre_commit and "pcnetgame_rec_txn_write_inventory(" not in pre_commit
          and not re.search(r"(?m)^\s*\*?Save_Get\([^;]*?\)(?:\.\w+|\[[^\]]*\]|->\w+)*\s*=[^=]", pre_commit) and "pcnetgame_mbox_refresh_resident(" not in pre_commit)
    check("H the letter comes ONLY from the HOST's own mailbox (L = &Save_Get(homes[h]).mailbox[t->slot], h = the house of the BOUND resident's record); the message carries no letter bytes: the handler never copies "
          "from `in` / `t` into a Mail_c and only compares the 24-bit hash + gift echo; the destination is checked < 10 by the shape check",
          "memcpy(&W, in" not in hd and "memcpy(&W, t" not in hd and "memcpy(&W, L, sizeof(W));" in hd and not re.search(r"\bW\.\w+(?:\.\w+)*\s*=\s*(?:in|t)->", hd)
          and "t->flags < (uint8_t)mPr_INVENTORY_MAIL_COUNT" in hd and "t->slot < (uint8_t)PC_NETGAME_MBOX_SLOTS" in hd)
    check("H the take changes no pocket / condition / wallet (no inventory write; the post-image sent is the mirror's unchanged one) and delivers nothing to the post office",
          "pcnetgame_rec_txn_write_inventory(" not in hd and "inventory." not in hd.replace("t->pre_", "").replace("pcnetgame_rec_validate_inventory", "") and "mPO_" not in hd)
    check("H a guest / extra player (a bound resident with NO house on the host) is refused with NO_DONOR_SLOT before the mailbox is read (source audited: no guest exists today)",
          in_order(hd, ["if (h < 0) {", "PC_NETGAME_TXN_REASON_NO_DONOR_SLOT", "L = &Save_Get(homes[h]).mailbox[t->slot];"]))
    check("H a stale claim (empty slot / hash mismatch) asks for a re-push of THAT mailbox slot (its seq is bumped, same bytes) instead of a record push: the client's shadow is corrected by the digest sender",
          "if (stale) {" in hd and "s_mbox_host[idx][t->slot].seq++;" in hd and "mbox_sent_seq[t->slot] = 0" not in hd and hd.count("pcnetgame_rec_stale_push(") == 1)  # the one stale push is the BASE check, like every txn kind
    rej_ok = True
    for m in re.finditer(r"pcnetgame_txn_reject\(", hd):
        rest = hd[hd.index(";", m.end()) + 1:].lstrip()
        rej_ok = rej_ok and (rest.startswith("return;") or rest.startswith("if (stale) {") or rest.startswith("pcnetgame_rec_stale_push(") or rest.startswith("pcnetgame_rec_violation(")
                  or (rest.startswith("} else if (old->outcome == (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED) {") and re.search(r'"replay "\);\s*\}\s*return;', hd) is not None))
    check("H a REJECTED path never mutates: every pcnetgame_txn_reject is followed by return (or the paced stale push / the violation count / the re-push request and then return)",
          len(re.findall(r"pcnetgame_txn_reject\(", hd)) >= 10 and rej_ok)
    check("H faults: ignore_commit, fail_world and kill_peer_after_commit have exactly ONE site each in the take handler (drop_result is the shared RESULT sender)",
          sorted(re.findall(r"pcnetgame_txn_fault_fire\((PC_TXN_FAULT_\w+)\)", hd)) == ["PC_TXN_FAULT_FAIL_WORLD", "PC_TXN_FAULT_IGNORE_COMMIT", "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT"])
    wr = func_body(c_raw[x1b:x1e], "pcnetgame_rec_txn_write_mail")
    check("H the mirror mail[] has exactly three sanctioned writes outside the D3 merge, all through pcnetgame_rec_txn_write_mail (idx must pass pcnetgame_rec_txn_idx_ok = never the host's own resident, slot < 10): "
          "the kind-12 clear (NULL) and the kind-13 write (the host's letter, memcpy); no other statement assigns private_data[].mail[]",
          "pcnetgame_rec_txn_idx_ok(idx)" in wr and "mail_slot >= mPr_INVENTORY_MAIL_COUNT" in wr and "memcpy(&r->mail[mail_slot], letter, sizeof(Mail_c));" in wr
          and c.count("pcnetgame_rec_txn_write_mail(") == 3 and hd.count("pcnetgame_rec_txn_write_mail(idx, dst, &W)") == 1 and len(re.findall(r"(?:->|\.)mail\[[^\]]*\]\s*=[^=]", c)) == 0
          and "Save_Get(private_data)[idx].mail[dst]" in hd and not re.search(r"memcpy\(&Save_Get\(private_data\)", c))
    hb_writes = re.findall(r"mMl_clear_mail\(&Save_Get\(homes\[[^\]]*\]\)\.mailbox\[[^\]]*\]\)|memcpy\(&Save_Get\(homes\[[^\]]*\]\)\.mailbox\[[^\]]*\]|mMl_clear_mail\(m\);", c)
    check("H writes of the HOST's homes[].mailbox in pc_net_game.c are exactly: the take handler's clear (host), the TEST-ONLY seed hook (host-only hook; mMl_clear_mail(m) on a free slot) and the CLIENT shadow writes "
          "(apply, revert, take apply) -- 1 + 1 + 4 sites (apply, the refused-slot clear, revert, take apply); nothing else (the postman / generators stay vanilla code outside this file)",
          hd.count("mMl_clear_mail(&Save_Get(homes[h]).mailbox[t->slot]);") == 1 and len(re.findall(r"mMl_clear_mail\(m\);", c)) == 1
          and len(re.findall(r"memcpy\(&Save_Get\(homes\[h\]\)\.mailbox\[i\], ", c)) == 2 and len(re.findall(r"mMl_clear_mail\(&Save_Get\(homes\[h\]\)\.mailbox\[t->slot\]\);", c)) == 2
          and len(hb_writes) == 6)

    # ------------------------------------------------------------------ C: client
    chd = func_body(m2c_raw, "pcnetgame_handle_client_mailbox_letter")
    check("C the MAILBOX_LETTER handler validates in order: READY link, exact size, slot / flags / reserved bytes, the FNV digest of the 298 letter bytes, a seq strictly above the last of THIS session for that slot "
          "(and above a stashed one), then (local save usable, else a one-deep stash per slot) the apply",
          in_order(chd, ["s_client_link != PC_NETGAME_LINK_READY", "size != (uint16_t)sizeof(m)", "i >= (int)PC_NETGAME_MBOX_SLOTS", "pcnetgame_fnv1a32(m.letter, sizeof(m.letter)) != m.digest",
                         "m.seq == 0u", "!pcfa_save_ready() || Now_Private == NULL", "s_mbox_stash[i] = m;", "pcnetgame_mbox_client_apply(&m);"])
          and "m.seq <= s_mbox_seq[i]" in chd and "m.seq <= s_mbox_stash[i].seq" in chd)
    ap_raw = func_body(m2c_raw, "pcnetgame_mbox_client_apply")
    ap = strip_comments(ap_raw)
    check("C the apply validates the HOUSE (the host's homes[] index must be the house THIS player owns), the empty flag against the decoded letter, and for a used letter: font < mMl_FONT_NUM, recipient type PLAYER and "
          "recipient PersonalID == this player; only then it writes the shadow, the local mailbox slot, the seq, the slot mask and (all 10 received) the host-fed state",
          in_order(ap, ["(int)m->house != h", "pcnetgame_mail_from_be(m->letter, &L);", "empty != (mMl_check_not_used_mail(&L) == TRUE)", "(u32)L.content.font >= (u32)mMl_FONT_NUM",
                        "L.header.recipient.type != (u8)mMl_NAME_TYPE_PLAYER", "mPr_CheckCmpPersonalID(&L.header.recipient.personalID, &Now_Private->player_ID) != TRUE",
                        "memcpy(&s_mbox_shadow[i], &L, sizeof(Mail_c));", "memcpy(&Save_Get(homes[h]).mailbox[i], &L, sizeof(Mail_c));", "s_mbox_seq[i] = m->seq;", "s_mbox_mask |= ",
                        "s_mbox_host_fed = 1;"])
          and "(s_mbox_mask & PC_NETGAME_MBOX_ALL_MASK) == PC_NETGAME_MBOX_ALL_MASK" in ap and "#define PC_NETGAME_MBOX_ALL_MASK 0x03FFu" in c_raw)
    shadow_w = re.findall(r"memcpy\(&s_mbox_shadow\[|mMl_clear_mail\(&s_mbox_shadow\[|s_mbox_shadow\[[^\]]*\]\s*=[^=]", c)
    check("C the shadow (s_mbox_shadow) is written in exactly two places: the verified MAILBOX_LETTER apply (memcpy) and the take apply (clear of the slot it just moved out); the local homes[].mailbox is written by those "
          "plus the 500 ms revert from the shadow; nothing else on a client may change it",
          len(shadow_w) == 3 and ap.count("memcpy(&s_mbox_shadow[i], &L, sizeof(Mail_c));") == 1 and ap.count("mMl_clear_mail(&s_mbox_shadow[i]);") == 1
          and func_body(xc_raw, "pcnetgame_txn_apply_take").count("mMl_clear_mail(&s_mbox_shadow[t->slot]);") == 1)
    rv = func_body(m2c_raw, "pcnetgame_mbox_client_tick")
    check("C the revert: every 500 ms, READY client with a usable save, a slot whose local mailbox bytes differ from the shadow is restored FROM the shadow (this neutralises every vanilla / generator writer of the dead "
          "local mailbox); the stash of an early message is applied here",
          in_order(rv, ["s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY || !pcfa_save_ready()", "pcnetgame_mbox_client_apply(&s_mbox_stash[i]);", "< 500u",
                        "memcmp(&Save_Get(homes[h]).mailbox[i], &s_mbox_shadow[i], sizeof(Mail_c)) != 0", "memcpy(&Save_Get(homes[h]).mailbox[i], &s_mbox_shadow[i], sizeof(Mail_c));"])
          and "pcnetgame_mbox_client_tick(); /* mail milestone 2" in c_raw)
    us = func_body(m2c_raw, "pc_net_game_client_mailbox_usable")
    check("C pc_net_game_client_mailbox_usable() is 1 only for a READY CLIENT whose shadow is host-fed (all 10 slots this session) and who owns a house; every other role / state answers 0",
          "s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY && s_mbox_host_fed && pcnetgame_mbox_own_house() >= 0" in us and c.count("s_mbox_host_fed = 1;") == 1)
    rst = func_body(c_raw, "pcnetgame_reset_client_session_state")
    check("C the session reset closes the mailbox again and kills the take operation: slot mask, host-fed flag, seq memory, stash, take op, wait timer",
          all(x in rst for x in ("memset(&s_take_op, 0, sizeof(s_take_op));", "s_take_last_reason = 0;", "s_take_wait_ms = 0;", "memset(s_mbox_seq, 0, sizeof(s_mbox_seq));",
                                 "memset(s_mbox_stash_valid, 0, sizeof(s_mbox_stash_valid));", "s_mbox_mask = 0;", "s_mbox_host_fed = 0;")))
    check("C the client dispatcher routes id 56 to the handler (client side only, after the town-service message); the handler is called nowhere else",
          re.search(r"data\[0\] == \(uint8_t\)PC_NETGAME_MSG_MAILBOX_LETTER\) \{\s*pcnetgame_handle_client_mailbox_letter\(data, size\);", c_raw) is not None
          and c.count("pcnetgame_handle_client_mailbox_letter(") == 2)
    ts_ = func_body(xc_raw, "pcnetgame_txn_try_send")
    tcase = ts_[ts_.index("case PC_NETGAME_TXN_KIND_MAIL_TAKE: {"):ts_.index("case PC_NETGAME_TXN_KIND_DIG_BURIED:")]
    check("C try_send for kind 13: the shadow slot must still hold exactly the verified letter the operation began with and a local mail slot must be free (cancel, never wait); the destination travels in tag.flags, the gift "
          "echo from the stashed letter, the hash split into aux_cond / aux_item; the kind is a commit kind, a cancel resolves the operation as rejected",
          "pcnetgame_take_try_send_check(slot," in tcase and "pcnetgame_txn_cancel_queued(" in tcase and "flags = d;" in tcase and "item = (uint16_t)s_take_op.letter.present;" in tcase
          and "aux_cond = T->ts_aux;" in tcase and "aux_item = T->ts_aux_item;" in tcase and "T->kind == (uint8_t)PC_NETGAME_TXN_KIND_MAIL_TAKE;" in ts_
          and "s_ctxn.kind == (uint8_t)PC_NETGAME_TXN_KIND_MAIL_TAKE) {" in func_body(xc_raw, "pcnetgame_txn_cancel_queued") and "pcnetgame_take_op_resolve(request_id, applied);" in c)
    at = func_body(xc_raw, "pcnetgame_txn_apply_take")
    check("C APPLIED of kind 13 (pcnetgame_txn_apply_take): the letter written into mail[] is the COPY of the verified shadow letter taken at begin (hash re-checked against the tag; never the live shadow), into the "
          "preferred slot when still empty else the first free one; no slot -> return 0 WITHOUT advancing the D3 base (the next upload is STALE_BASE and the host push delivers the letter); pockets / wallet are never touched",
          in_order(at, ["!s_take_op.active", "(pcnetgame_mail_be_hash(&s_take_op.letter) & 0xFFFFFFu) != want24", "no matching verified letter -- nothing applied", "mMl_check_not_used_mail(&Now_Private->mail[t->flags]) == TRUE",
                        "if (dst < 0) {", "letter NOT placed", "memcpy(&Now_Private->mail[dst], &s_take_op.letter, sizeof(Mail_c));", "mMl_clear_mail(&s_mbox_shadow[t->slot]);",
                        "mMl_clear_mail(&Save_Get(homes[h]).mailbox[t->slot]);", "pcnetgame_crec_set_base("])
          and "inventory." not in at and "mPr_Set" not in at and "mPr_Give" not in at and at.count("pcnetgame_crec_set_base(") == 1
          and "(pcnetgame_mail_be_hash(&s_mbox_shadow[t->slot]) & 0xFFFFFFu) == want24" in at
          and "in->host_session == s_crec.base_session && in->epoch == s_crec.base_epoch && in->rev > s_crec.base_rev" in at)
    aa = func_body(xc_raw, "pcnetgame_txn_apply_applied")
    check("C apply_applied routes kind 13 to apply_take right after the kind-12 route (both after the owner stamp check, before the shop / pocket logic)",
          in_order(aa, ["pcnetgame_owner_stamp_matches(&T->owner)", "return pcnetgame_txn_apply_mail(T, in);", "if (T->kind == (uint8_t)PC_NETGAME_TXN_KIND_MAIL_TAKE) {", "return pcnetgame_txn_apply_take(T, in);",
                        "PC_NETGAME_TXN_KIND_SHOP_BUY"]))
    check("C the ONLY statement that puts a mailbox letter into Now_Private->mail[] is the take apply (one memcpy of the stashed letter); mMl_copy_mail(&Now_Private->mail is never used in pc_net_game.c; the M1 clear (send) is "
          "unchanged; the test hook writes nothing",
          c.count("memcpy(&Now_Private->mail[") == 1 and "memcpy(&Now_Private->mail[dst], &s_take_op.letter, sizeof(Mail_c));" in at and "mMl_copy_mail(&Now_Private->mail" not in c
          and len(re.findall(r"mMl_clear_mail\(&Now_Private->mail", c)) == 1)
    api = "".join(func_body(m2c_raw, n) for n in ("pc_net_game_mail_take_step", "pcnetgame_take_try_send_check", "pcnetgame_take_op_resolve", "pc_net_game_mail_take_last_reason"))
    check("C NO client mailbox-to-mail[] path before APPLIED: take_step / the try_send check / resolve contain no write of mail[], the shadow or the local mailbox (no memcpy / mMl_clear_mail / mMl_copy_mail into them); "
          "the step copies the shadow letter into the operation's PRIVATE stash (s_take_op.letter) only",
          "mMl_clear_mail(" not in api and "mMl_copy_mail(" not in api and not re.search(r"mail\[[^\]]*\]\s*=[^=]", api) and "memcpy(&Now_Private" not in api and "memcpy(&Save_Get" not in api
          and "memcpy(&s_take_op.letter, sh, sizeof(Mail_c));" in api and "homes" not in api and not re.search(r"->mailbox\[[^\]]*\]\s*=[^=]", api))
    st = func_body(m2c_raw, "pc_net_game_mail_take_step")
    check("C take_step: stateless for the overlay (an op for the same slot is polled, consumed exactly once; another slot's unresolved op = PENDING); begin needs a host-fed mailbox, a SYNCED record with no staged push / "
          "stopped uploads / other operation (bounded 5 s wait, then a local refusal), a used shadow slot with a legal letter, a free mail slot and an owner stamp; then it queues the kind-13 txn",
          in_order(st, ["s_take_op.active", "!pc_net_game_client_mailbox_usable()", "s_crec.state != PC_NETGAME_CRS_SYNCED || s_crec.up_blocked || s_crec.st_valid || pcnetgame_txn_begin_blocked()",
                        "PC_NETGAME_MAIL_CLEAN_TIMEOUT_MS", "mMl_chk_mail_free_space(Now_Private->mail, mPr_INVENTORY_MAIL_COUNT)", "pcnetgame_capture_owner_stamp(&stamp)",
                        "memcpy(&s_take_op.letter, sh, sizeof(Mail_c));", "s_ctxn.kind = (uint8_t)PC_NETGAME_TXN_KIND_MAIL_TAKE;", "s_ctxn.take_dst = (uint8_t)dst;", "pcnetgame_txn_try_send()"])
          and "memset(&s_take_op, 0, sizeof(s_take_op));" in st)
    lock = func_body(c_raw, "pc_net_game_client_pocket_locked")
    check("C while a take is unresolved the transaction is s_ctxn: pcnetgame_txn_busy() covers the pocket lock; pcnetgame_txn_begin_blocked() also counts an unconsumed take op; the take shares NO state with the M1 op",
          "pcnetgame_txn_busy()" in lock and "(s_take_op.active && !s_take_op.done)" in func_body(c_raw, "pcnetgame_txn_begin_blocked") and "s_mail_op" not in st)
    check("C the API is declared in pc_net_game.h (usable / take_step / take_last_reason) and the two reject codes equal the C reasons",
          all(x in h for x in ("int pc_net_game_client_mailbox_usable(void);", "int pc_net_game_mail_take_step(int mbox_idx, int* dst_out);", "int pc_net_game_mail_take_last_reason(void);",
                               "#define PC_NETGAME_MAIL_TAKE_REJECT_NO_SUCH_LETTER 26", "#define PC_NETGAME_MAIL_TAKE_REJECT_MAIL_CHANGED   27")))

    # ------------------------------------------------------------------ O: UI seams
    chk_mbx = mbx[mbx.index("static void aMBX_check_take_mail"):mbx.index("static void aMBX_check_flag")]
    gate_open = "&& (pc_net_game_role() != PC_NETGAME_ROLE_CLIENT || pc_net_game_client_mailbox_usable())"
    check("O1 the mailbox actor opens for a CLIENT only when host-fed: the open condition is exactly '" + gate_open + "' under #ifdef TARGET_PC, before the open request; the old unconditional M0 refusal is gone",
          "#ifdef TARGET_PC" in chk_mbx and chk_mbx.count(gate_open) == 1 and chk_mbx.index(gate_open) < chk_mbx.index("actor->req = aMBX_REQUEST_OPEN;") and "&& pc_net_game_role() != PC_NETGAME_ROLE_CLIENT\n" not in chk_mbx)
    flag = mbx[mbx.index("static void aMBX_check_flag"):mbx.index("static void aMBX_setup_flag_se_sub")]
    check("O1 the flag of a client's mailbox stays down until host-fed (mail_count = 0 for a CLIENT while usable() is 0, before it is used), then counts the shadow-fed local mailbox",
          "if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && !pc_net_game_client_mailbox_usable()) {\n        mail_count = 0;" in flag and flag.index("mail_count = 0;") < flag.index("if (mail_count != 0)"))
    ctm = func_body(tag, "mTG_check_trans_mail")
    ctk = func_body(tag, "mTG_check_trans_mail_mark")
    cmp_ = func_body(tag, "mTG_mailbox_change_mail_proc")
    check("O2 the overlay's transfer entry points (single letter / marked letters) and the 'take marked' proc carry the same precise gate: a client whose mailbox is NOT host-fed yields nothing / takes the vanilla refusal",
          gate_open in ctm and ctm.index(gate_open) < ctm.index("res = mTG_trans_mail(") and gate_open in ctk and ctk.index(gate_open) < ctk.index("res = mTG_trans_mail_mark(")
          and "if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && !pc_net_game_client_mailbox_usable()) {\n        idx = -1;" in cmp_)
    tm = func_body(tag, "mTG_trans_mail")
    tmm = func_body(tag, "mTG_trans_mail_mark")
    vanilla = "{\n                mMl_copy_mail(&Now_Private->mail[dst_idx], src);\n                mMl_clear_mail(src);\n            }"
    check("O3 a client's mailbox letter moves ONLY through the host: in both transfer functions the vanilla 'copy into mail[] + clear the mailbox slot' sits in the else branch of the CLIENT role test; the client branch calls "
          "mTG_client_mailbox_take (pending -> return TRUE with no state change; refused -> stop transferring) and then only animates",
          all(vanilla in f and f.index("if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {") < f.index("mTG_client_mailbox_take(src_idx, &dst_idx)") < f.index("} else\n#endif\n" + "            " + vanilla)
              for f in (tm, tmm))
          and "if (t == 0) {\n                    return TRUE; /* pending: nothing changes */" in tm and "if (t == 0) {\n                    return TRUE; /* pending: nothing changes */" in tmm
          and "mailbox_ovl->open_flag = FALSE;\n                    return FALSE;" in tm and "mailbox_ovl->mark_flag = 0;\n                    mailbox_ovl->mark_bitfield = 0;\n                    return TRUE;" in tmm
          and tag.count("mMl_copy_mail(&Now_Private->mail[dst_idx], src);") == 2)
    ctake = func_body(tag, "mTG_client_mailbox_take")
    check("O3 mTG_client_mailbox_take maps pc_net_game_mail_take_step: APPLIED -> 1, PENDING -> 0, anything else -> -1; it writes nothing itself",
          "switch (pc_net_game_mail_take_step(src_idx, dst_idx)) {" in ctake and "return 1;" in ctake and "return 0;" in ctake and "return -1;" in ctake and "mMl_" not in ctake)
    dm = func_body(tag, "mTG_dump_mail_mark_exe_proc")
    check("O4 throwing letters away from the mailbox stays REFUSED for a client (kind 14 is a later milestone): the marked-discard proc drops the mailbox marks for a client before they are used; the single-letter clear is skipped "
          "when the pointer is a mailbox slot (mTG_client_mail_is_mailbox); the multi-letter clear loop is skipped for a client",
          "if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {\n            mailbox_ovl->mark_bitfield = 0;" in dm and dm.index("mailbox_ovl->mark_bitfield = 0;") < dm.index("for (i = 0; i < HOME_MAILBOX_SIZE; i++) {")
          and "if (!mTG_client_mail_is_mailbox(mTG_get_mail_pointer(submenu, NULL)))" in tag and "if (pc_net_game_role() != PC_NETGAME_ROLE_CLIENT) /* mail milestone 2: refused for a client" in tag
          and "return pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && mail != NULL && Common_Get(now_home) != NULL &&\n           mail >= Common_Get(now_home)->mailbox && mail < Common_Get(now_home)->mailbox + HOME_MAILBOX_SIZE;" in func_body(tag, "mTG_client_mail_is_mailbox"))  # G5.0: + the NULL now_home guard (a guest has no home)
    check("O5 the M0 gates that must stay: the four villager generators that write straight into homes[].mailbox return before any write for a CLIENT, the mother's letter and the system letters are not generated for a client, "
          "the archive -> pocket exchange is refused for a client (test_mail_src.py audits each in detail)",
          "#define mNpc_CLIENT_SKIPS_MAILBOX_LETTER() (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT)" in npc and npc.count("if (mNpc_CLIENT_SKIPS_MAILBOX_LETTER()) {") == 4
          and "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT" in func_body(priv, "mPr_SendMotherMailPost") and "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT" in func_body(mail_c, "mMl_send_mail_box_com")
          and "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && cpmail_mark_cnt > 0" in func_body(tag, "mTG_cpmail_change_mail_proc"))

    # ------------------------------------------------------------------ R: M2r
    rf_ = func_body(npc, "mNpc_RemailFor")
    rm = func_body(npc, "mNpc_Remail")
    check("R mNpc_RemailFor(Private_c*) is the town-villager part of the vanilla mNpc_Remail for ONE record: it reads only priv->player_ID (no write into priv), writes host-owned state (the memories, the post office via "
          "mNpc_SendRemailPostOffice) and keeps the vanilla 'letter date != today' rule and the break on a full post office; declared in m_npc.h",
          "extern void mNpc_RemailFor(Private_c* priv);" in npc_h and "mPr_NullCheckPersonalID(&priv->player_ID) == FALSE" in rf_ and "mNpc_CheckLetterTime(&memory->letter.date, rtc_time) == TRUE" in rf_
          and "mNpc_SendRemailPostOffice(&priv->player_ID, &animal->id, NULL, memory->letter_info.cond," in rf_ and "memory->letter_info.send_reply = FALSE;" in rf_ and "priv->" not in rf_.replace("priv->player_ID", ""))
    check("R mNpc_Remail (the load-time call for the LOCAL player) is the vanilla sequence on host / solo (local-land check -> mNpc_RemailFor -> the foreign letter, unchanged) and returns at once for a network CLIENT "
          "(its replies are generated by the HOST; generating into its dead local post office would lose them and clear its remail state)",
          in_order(rm, ["pc_net_game_role() == PC_NETGAME_ROLE_CLIENT", "return;", "mLd_PlayerManKindCheck() == FALSE", "mNpc_RemailFor(priv);", "remail = &priv->remail;", "mNpc_ClearRemail(remail);"])
          and "#ifdef TARGET_PC" in rm)
    rt = func_body(m2h_raw, "pcnetgame_host_remail_tick")
    check("R the host pass: HOST role + world ready only, 1 Hz, for every READY bound peer whose record is SYNCED, not the host's own resident, with a valid binding; per resident ONCE per calendar day of the host clock "
          "(or when the test hook forces it); it calls mNpc_RemailFor on that resident's private_data record ONLY; the host's own resident keeps the vanilla load-time behaviour",
          in_order(rt, ["s_role != PC_NETGAME_ROLE_HOST || !s_host_world_ready", "now + 1000u", "Common_Get(time.rtc_time).year", "s_host_peer_link[p] != PC_NETGAME_LINK_READY || !st->bound_valid || st->rec_state != PC_NETGAME_RECS_SYNCED",
                        "(own >= 0 && idx == own)", "mPr_CheckCmpPersonalID(&st->bound_pid, &Save_Get(private_data)[idx].player_ID) != TRUE", "s_remail_day[idx] == today",
                        "mNpc_RemailFor(&Save_Get(private_data)[idx]);", "mPO_get_keep_mail_sum() < mPO_MAIL_STORAGE_SIZE", "s_remail_day[idx] = today;"])
          and c.count("mNpc_RemailFor(") == 1 and "s_remail_force" not in c)
    check("R the villager event / birthday / Xmas / Valentine letters already target the HOST homes[] house of EVERY resident (the vanilla generators take the resident's player_no / PersonalID), so M2 delivers them unchanged: "
          "the four generators (event present, birthday card, Xmas card, goodbye letter) index homes[mHS_get_arrange_idx(player_no)] and the event manager loops every private_data record",
          npc.count("home = Save_GetPointer(homes[mHS_get_arrange_idx(player_no)]);") == 4 and "mNpc_SendEventXmasCard(&priv[i].player_ID, i);" in evm and "mNpc_SendEventBirthdayCard2(&priv[i].player_ID, i);" in evm)
    check("R PIN (known limitation, flagged in the notes): the birthday-card eligibility test (mNpc_CheckFriendship) and mNpc_SendEventBirthdayCard2 still read / write the HOST's own player record (Common_Get(now_private)->"
          "birthday_present_npc / celebrated_birthday_year), not the resident's: a client resident's birthday cards depend on the host player's fields. A change here must be decided",
          "Common_Get(now_private)->birthday_present_npc != animal->id.npc_id" in npc and "Common_Get(now_private)->celebrated_birthday_year" in npc)
    check("R PIN (known limitation): mother mail, foreign-animal mail and the museum letters are still not generated on the host for a client resident (mPr_SendMailFromMother / mPr_SendForeingerAnimalMail use Common now_private)",
          "mPr_SendMailFromMother();" in read("src/game/m_start_data_init.c") and "mPr_SendForeingerAnimalMail(Common_Get(now_private));" in read("src/game/m_start_data_init.c"))

    # ------------------------------------------------------------------ T: hooks
    check("T the mail-2 test hooks are role-bound in pc_main.c like the milestone-1 ones: --mail-test-seed-mailbox / --mail-test-seed-reply refuse (exit 2) unless --host, --mail-test-take unless --connect; all default off, "
          "armed with a loud [TEST-ONLY] line, documented in --help and pc_platform.h",
          "int g_pc_mail_test_take = 0;" in main_c and "const char* g_pc_mail_test_seed_mailbox = NULL;" in main_c and "const char* g_pc_mail_test_seed_reply = NULL;" in main_c
          and 'strcmp(argv[i], "--mail-test-take") == 0' in main_c and 'strncmp(argv[i], "--mail-test-take=", 17) == 0' in main_c and 'strncmp(argv[i], "--mail-test-seed-mailbox=", 25) == 0' in main_c
          and 'strncmp(argv[i], "--mail-test-seed-reply=", 23) == 0' in main_c and main_c.count("[NET][MAIL][TEST-ONLY] --mail-test-") >= 4
          and "g_pc_mail_test_seed_mailbox != NULL || g_pc_mail_test_seed_reply != NULL) &&" in main_c and "(g_pc_mail_test_send != NULL || g_pc_mail_test_take != 0) && g_pc_net_role != 2" in main_c
          and "--mail-test-take[=N]" in main_c and "--mail-test-seed-mailbox=RES,COUNT,DELAY_MS[,GIFTHEX]" in main_c and "--mail-test-seed-reply=RES[,RES...]" in main_c
          and "extern int           g_pc_mail_test_take;" in plat_h and "extern const char*   g_pc_mail_test_seed_mailbox;" in plat_h and "extern const char*   g_pc_mail_test_seed_reply;" in plat_h)
    th = func_body(m2c_raw, "pcnetgame_run_mail_take_test_hook")
    check("T --mail-test-take: CLIENT-only, waits for the host-fed mailbox and a SYNCED record, calls the REAL pc_net_game_mail_take_step, writes NOTHING itself (no memcpy / mMl_ / mail[] assignment), loudly checks that "
          "no pocket / mail slot changed before APPLIED",
          "s_role != PC_NETGAME_ROLE_CLIENT" in th and "pc_net_game_client_mailbox_usable()" in th and "pc_net_game_mail_take_step(s_cur, &dst)" in th and "memcpy(" not in th and "mMl_clear_mail(" not in th
          and "mMl_copy_mail(" not in th and not re.search(r"mail\[[^\]]*\]\s*=[^=]", th) and "CHANGED BEFORE APPLIED" in th and th.count("[NET][MAIL][TEST-ONLY]") >= 8)
    sm = func_body(m2h_raw, "pcnetgame_mail_test_seed_mailbox")
    sr = func_body(m2h_raw, "pcnetgame_mail_test_seed_reply")
    check("T --mail-test-seed-mailbox / --mail-test-seed-reply: HOST-only (role re-checked), one-shot, loud, no getenv; the seed writes only the free slots of the named resident's house mailbox / one villager memory",
          "s_role != PC_NETGAME_ROLE_HOST" in sm and "s_role != PC_NETGAME_ROLE_HOST" in sr and "s_done = 1;" in sm and "s_done = 1;" in sr and "getenv" not in m2h and "[NET][MAIL][TEST-ONLY]" in sm and "[NET][MAIL][TEST-ONLY]" in sr
          and "mMl_check_not_used_mail(m) != TRUE" in sm and "s_remail_force" not in sm + sr)
    check("T the hooks are called once per poll from the one place the other test hooks are (a complete no-op unless armed)",
          "pcnetgame_mail_test_seed_mailbox(); /* mail milestone 2" in c_raw and "pcnetgame_mail_test_seed_reply();   /* mail milestone 2" in c_raw and "pcnetgame_run_mail_take_test_hook(); /* mail milestone 2" in c_raw)

    check("C review M1: take_step delivers a FINISHED op exactly once to whoever asks next, whatever slot it asks for (the single-letter overlay asks for the last used slot, which changes once the apply cleared the "
          "taken one); there is no slot comparison against the op and no 'abandoned' memset of a finished result",
          "s_take_op.mbox_idx ==" not in st and "abandoned overlay" not in st and in_order(st, ["s_take_op.active", "!s_take_op.done", "return PC_NETGAME_TS_OP_PENDING;", "dst_out = (int)s_take_op.dst;", "memset(&s_take_op, 0, sizeof(s_take_op));", "return r;"]))
    check("C review M2: a REFUSED MAILBOX_LETTER (house / empty flag / font / recipient) still counts as received: every refusal jumps to one label that clears the shadow + the local slot, records the seq and the slot "
          "mask and runs the host-fed check, so one bad slot can never keep the mailbox closed; the refusal logging is bounded",
          ap.count("goto refused;") == 3 and "\nrefused:" in ap_raw and in_order(ap_raw[ap_raw.index("\nrefused:"):], ["mMl_clear_mail(&s_mbox_shadow[i]);", "s_mbox_seq[i] = m->seq;", "s_mbox_mask |= (uint16_t)(1u << i);", "fed_check:", "s_mbox_host_fed = 1;"])
          and "s_refused_logs" in ap_raw and ap.count("return;") == 0)
    check("C review L: the host and the client keep DISTINCT 500 ms timers (s_mbox_next_check_ms host only, s_mbox_cl_next_check_ms client only)",
          c.count("static uint32_t          s_mbox_next_check_ms = 0;") == 1 and "s_mbox_next_check_ms" not in m2c and "s_mbox_cl_next_check_ms" in m2c and c.count("static uint32_t                  s_mbox_cl_next_check_ms;") == 1)

    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
