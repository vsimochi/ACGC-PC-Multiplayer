#!/usr/bin/env python3
"""test_txn_src.py - SOURCE AUDIT of X1a (host half) + X1b (client half) of the transactional pickup/drop/bury commit (TXN_COMMIT 51 / TXN_RESULT 52).

Pure text checks over pc/src/pc_net_game.c (X1 WIRE block, the X1 BEGIN/END host block, the X1b CLIENT block, the extracted D3 helpers, the
confirm handler and the three client result handlers), src/game/m_player_lib.c, pc/src/pc_main.c, pc/include/pc_platform.h,
pc/include/pc_net_game.h and the Python library. No game process. Tier: SOURCE AUDITED.

  W  wire: ids 51/52 (53/54 reserved), 64/72/76 byte structs with exact _Static_asserts + <= PC_NET_MAX_PAYLOAD, every send RELIABLE,
     the version policy (v8 unreleased, no bump, STRICT equality intact), python formats/constants == the C values, wire_baseline green
  H  host: one handler call, the algorithm order, an explicit RESULT on every path after the READY gate, RESULT before the broadcast
  S  single sanctioned writers: merge_into_save + txn_write_inventory only; the index only from the gate; no mPr_Set*/RNG in the txn path
  J  journal: bounded ring, fence FIFO, per RESIDENT, never cleared per peer, cleared on town change / world reset / slot re-init
  L  legacy: CONFIRM(COMMIT) behind the named constant (1 since X1b = RETIRED; the old "processed as before" branch is dead code that only this
     audit covers), ABORT kept, a reservation is consumed once (commit_path), shared world helpers
  C  client (X1b): result handlers only VALIDATE and begin a txn (no pocket/wallet write, no CONFIRM(COMMIT)); the one pocket writer is
     pcnetgame_txn_apply_applied; s_bury_committed gone; nonce/seq never reset by a session reset; resend = identical stored bytes with no
     give-up; the COMMIT never goes out while an upload is queuing; locks (a)-(e); the client-only submenu lock; the ABORT(STALE) suppression;
     the host-side violation counting of malformed COMMITs; the test hook is default off / loud / documented
  F  fault hooks: default off, host-gated, loud, five modes, no environment override
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


def main():
    results = []
    check = lambda d, c: L.check(d, bool(c), results)
    c_raw = read("pc/src/pc_net_game.c")
    c = strip_comments(c_raw)
    h = read("pc/include/pc_net_game.h")
    main_c = read("pc/src/pc_main.c")
    plat_h = read("pc/include/pc_platform.h")
    b = c_raw.index("===== X1 BEGIN")
    e = c_raw.index("===== X1 END")
    blk_raw = c_raw[b:e]
    blk = strip_comments(blk_raw)
    d3b, d3e = c_raw.index("===== D3 BEGIN"), c_raw.index("===== D3 END")
    d3_raw = c_raw[d3b:d3e]
    hd_raw = func_body(blk_raw, "pcnetgame_handle_host_txn_commit")
    hd = strip_comments(hd_raw)

    # ------------------------------------------------------------------ W: wire
    ids = wire_baseline.c_message_ids(c_raw)
    d = dict(ids)
    check("W ids 51 = TXN_COMMIT and 52 = TXN_RESULT; 53 / 54 are ENUMERATED as the reserved X2 ids, 55 = TOWN_SVC_STATE (town services) and 56 = MAILBOX_LETTER (mail milestone 2); all ids contiguous 1..%d (wire_baseline.EXPECTED_MAX_MSG_ID)" % wire_baseline.EXPECTED_MAX_MSG_ID,
          d.get("PC_NETGAME_MSG_TXN_COMMIT") == 51 and d.get("PC_NETGAME_MSG_TXN_RESULT") == 52
          and d.get("PC_NETGAME_MSG_TXN_RESERVED_53") == 53 and d.get("PC_NETGAME_MSG_TXN_RESERVED_54") == 54 and d.get("PC_NETGAME_MSG_TOWN_SVC_STATE") == 55
          and d.get("PC_NETGAME_MSG_MAILBOX_LETTER") == 56
          and d.get("PC_NETGAME_MSG_IDENTITY_EXT") == 57 and d.get("PC_NETGAME_MSG_IDENTITY_TOKEN") == 58  # guests G1
          and sorted(v for _n, v in ids) == list(range(1, wire_baseline.EXPECTED_MAX_MSG_ID + 1)) == list(range(1, 59)))
    enum_body = c_raw[c_raw.index("typedef enum PCNetGameMsgType {"):c_raw.index("} PCNetGameMsgType;")]
    check("W the enum comment documents that 53 and 54 are reserved for X2 (TXN_QUERY / TXN_STATUS)", "53 and 54 are reserved for X2" in enum_body and "TXN_QUERY" in enum_body)
    for t, size in (("PCNetGameTxnTag", 64), ("PCNetGameTxnCommitMsg", 72), ("PCNetGameTxnResultMsg", 76)):
        check(f"W {t}: exact-size _Static_assert == {size}", f"_Static_assert(sizeof({t}) == {size}," in c_raw)
    for t in ("PCNetGameTxnCommitMsg", "PCNetGameTxnResultMsg"):
        check(f"W {t}: _Static_assert <= PC_NET_MAX_PAYLOAD", f"_Static_assert(sizeof({t}) <= PC_NET_MAX_PAYLOAD," in c_raw)
    check("W the field offsets of the tag / commit / result are statically asserted",
          "offsetof(PCNetGameTxnTag, pre_pockets) == 24" in c_raw and "offsetof(PCNetGameTxnCommitMsg, tag) == 8" in c_raw
          and "offsetof(PCNetGameTxnResultMsg, post_pockets) == 36" in c_raw and "offsetof(PCNetGameTxnResultMsg, post_wallet) == 72" in c_raw)
    check("W python specs: sizes 64 (tag) / 72 / 76, ids 51 / 52, formats as specified, both registered in GAME_SPECS",
          struct_size(L.TXN_TAG_FMT) == 64 and L.TXN_COMMIT_SPEC.size == 72 and L.TXN_RESULT_SPEC.size == 76
          and L.TXN_COMMIT_FMT == "<BBHIIIBBHBBHII15HHII" and L.TXN_RESULT_FMT == "<BBBBIIIIIIIBBH15HHII"
          and L.PC_NETGAME_MSG_TXN_COMMIT == 51 and L.PC_NETGAME_MSG_TXN_RESULT == 52 and L.GAME_SPECS[51] is L.TXN_COMMIT_SPEC and L.GAME_SPECS[52] is L.TXN_RESULT_SPEC)
    cm = dict(re.findall(r"#define (PC_NETGAME_TXN_\w+)\s+(0x[0-9A-Fa-f]+|\d+)u?\b", c_raw))
    pyc = {n: v for n, v in vars(L).items() if n.startswith("PC_NETGAME_TXN_") and isinstance(v, int)}
    mism = sorted(n for n, v in cm.items() if n in pyc and int(v, 0) != pyc[n])
    missing = sorted(n for n in cm if n not in pyc and n not in ("PC_NETGAME_TXN_RETIRE_LEGACY_COMMIT", "PC_NETGAME_TXN_RESEND_MS", "PC_NETGAME_TXN_LONG_LOG_MS", "PC_NETGAME_TXN_RESET_MS"))  # the last two are CLIENT-only timers (audited in section C)
    check(f"W every PC_NETGAME_TXN_* #define of the C source (reasons, dest, outcome, ring, fence) equals the net_spike_lib constant (mismatch {mism}, missing {missing}, {len(cm)} defines)",
          len(cm) >= 25 and not mism and not missing)
    names = {v: k.replace("PC_NETGAME_TXN_REASON_", "") for k, v in pyc.items() if k.startswith("PC_NETGAME_TXN_REASON_")}
    check("W TXN_REASON_NAMES (python) lists exactly the 28 C reasons (15 + the 4 town-services reasons + the 4 shop reasons + the 3 mail reasons + the 2 mail-take reasons), same names/values", {k: v for k, v in L.TXN_REASON_NAMES.items()} == names and len(names) == 28)
    sends = re.findall(r"pc_net_send\(([^()]*(?:\([^()]*\)[^()]*)*)\)", blk)
    check("W every pc_net_send in the X1 block is RELIABLE and there is exactly one (the RESULT sender)", len(sends) == 1 and all("PC_NET_RELIABLE" in s for s in sends))
    check("W the X1 block never sends a TXN_COMMIT (client -> host only) and the host dispatcher has no TXN_RESULT handling",
          "PC_NETGAME_MSG_TXN_COMMIT" not in blk and "PC_NETGAME_MSG_TXN_RESULT" in blk
          and "PC_NETGAME_MSG_TXN_RESULT" not in func_body(c_raw, "pcnetgame_handle_host_data"))
    disp = func_body(c_raw, "pcnetgame_handle_host_data")
    check("W dispatch: exact size check + id, aligned memcpy into a local, then the handler (and only there)",
          re.search(r"size == sizeof\(PCNetGameTxnCommitMsg\) && data\[0\] == \(uint8_t\)PC_NETGAME_MSG_TXN_COMMIT\) \{\s*PCNetGameTxnCommitMsg tc;\s*memcpy\(&tc, data, sizeof\(tc\)\);[^\n]*\n\s*"
                    r"if \(tc.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_MUSEUM_DONATE \|\| tc.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_POLICE_CLAIM \|\|\s*tc.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_SHOP_BUY \|\| tc.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_SHOP_SELL\) \{\s*pcnetgame_handle_host_ts_txn\(peer, &tc\);[^\n]*\n\s*\} else if \(tc.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_MAIL_SEND\) \{\s*pcnetgame_handle_host_mail_txn\(peer, &tc\);[^\n]*\n\s*\} else if \(tc.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_MAIL_TAKE\) \{\s*pcnetgame_handle_host_mail_take_txn\(peer, &tc\);[^\n]*\n\s*\} else \{\s*pcnetgame_handle_host_txn_commit\(peer, &tc\);",
                    disp) is not None and c.count("pcnetgame_handle_host_txn_commit(") == 2 and c.count("pcnetgame_handle_host_ts_txn(") == 2
          and c.count("pcnetgame_handle_host_mail_txn(") == 2 and c.count("pcnetgame_handle_host_mail_take_txn(") == 2)  # town services: kinds 8 / 9 / 10 / 11 route to their own one-phase handler; mail milestone 1: kind 12 to the mail handler; mail milestone 2: kind 13 to the take handler
    check("W version policy: v8 (header), NO bump (wire_baseline expects 8), the header documents the in-place extension, and the STRICT equality checks are intact",
          wire_baseline.header_protocol_ok(h) and wire_baseline.EXPECTED_PROTOCOL_VERSION == 8 and "VERSION POLICY (X1" in h and "EXTENDED IN PLACE" in h
          and "NO version" in h and "STRICT equality" in h and "if (version != PC_NETGAME_PROTOCOL_VERSION) {" in c
          and "in.protocol_version != PC_NETGAME_PROTOCOL_VERSION ||" in c)
    wire_baseline.run(lambda desc, cond: check("W " + desc, cond), ROOT)

    # ------------------------------------------------------------------ H: host handler
    check("H the X1 block sits AFTER the D3 host block and BEFORE the D3 client block (the D3 source audit keeps its meaning)",
          c_raw.index("===== D3 END") < b < e < c_raw.index("===== D3 CLIENT BEGIN"))
    check("H ONE handler, pcnetgame_handle_host_txn_commit(peer, msg), and it never yields: no sleep / wait / IO / loop over the network in the body",
          hd != "" and not re.search(r"\b(SDL_Delay|Sleep|fopen|fwrite|pc_net_poll|usleep)\b", hd))
    order = ["s_host_peer_link[peer] != PC_NETGAME_LINK_READY", "PC_TXN_FAULT_IGNORE_COMMIT", "pcnetgame_rec_gate(peer, 0, 0)",
             "PC_NETGAME_RECS_SYNCED", "s_host_world_ready", "shape_ok = in->kind", "pcnetgame_txn_nonce_fenced(", "pcnetgame_txn_journal_find(",
             "R->max_seq = t->txn_seq;", "rec = pcnetgame_host_interaction(peer, (int)in->kind);", "PC_NETGAME_PHASE_EXPIRED", "PC_TXN_FAULT_EXPIRE",
             "t->item != rec->item", "pcnetgame_rec_refresh_hostfields(idx, slot)", "t->base_epoch != slot->epoch",
             "pcnetgame_rec_validate_inventory(t->pre_pockets", "mPr_GetAmountForMoneyItem(", "pcnetgame_txn_world_check(", "PC_TXN_FAULT_FAIL_WORLD",
             "memcpy(post, t->pre_pockets", "pcnetgame_txn_world_write(", "pcnetgame_rec_txn_write_inventory(idx, post", "slot->rev++;",
             "slot->dirty_unsaved = 1;", "R->last_pocket_rev = slot->rev;", "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED",
             "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT", "pcnetgame_txn_send_applied(peer, idx, in, slot, (uint8_t)PC_NETGAME_TXN_REASON_NONE",
             "pcnetgame_txn_world_publish(peer, rec, (int)in->kind);\n    /* 17.", "pcnetgame_rec_start_push(peer, idx, st->rec_push_kind)"]
    pos = [hd_raw.find(x) for x in order]
    check("H the algorithm steps appear in the spec's order (gate, fault, binding, SYNCED, ready, shape, fence/replay, max_seq, reservation, expiry, echo, "
          "base, pre-image, precondition, world check, post-image, world write, mirror write, rev++/dirty/journal, RESULT, broadcast, push restart): missing %s" % [x for x, p in zip(order, pos) if p < 0],
          all(p >= 0 for p in pos) and pos == sorted(pos))
    check("H the resident index comes ONLY from the gate: one 'idx = pcnetgame_rec_gate(peer, 0, 0);' and no other assignment to idx",
          hd.count("idx = pcnetgame_rec_gate(peer, 0, 0);") == 1 and len(re.findall(r"\bidx\s*=[^=]", hd)) == 1 and not re.search(r"in->\w*(?:idx|resident)\w*", hd))
    check("H the base check requires epoch equality and R->last_pocket_rev <= base_rev <= slot->rev, answers STALE_IMAGE and triggers the paced stale push",
          "t->base_epoch != slot->epoch || t->base_rev < R->last_pocket_rev || t->base_rev > slot->rev" in hd and "PC_NETGAME_TXN_REASON_STALE_IMAGE" in hd
          and "pcnetgame_rec_stale_push(peer, idx, slot, now)" in hd)
    check("H the expired COMMIT is ANSWERED (L-2): an EXPIRED record or an aged PENDING one releases + REJECTED(EXPIRED), journaled",
          "rec->request_id == in->request_id && rec->phase == (uint8_t)PC_NETGAME_PHASE_EXPIRED" in hd and "PC_NETGAME_CONFIRM_TIMEOUT_MS" in hd
          and hd.count("PC_NETGAME_TXN_REASON_EXPIRED") >= 2)
    check("H the shape rules: kind 1..3, _rsv0/tag._rsv0/flags/aux_* zero, nonce and seq non-zero, pickup POCKET(<15)/WALLET(0xFF), drop/bury dest NONE and slot < 15",
          all(x in hd for x in ("in->kind >= (uint8_t)PC_NETGAME_INTERACT_KIND_PICKUP", "in->kind <= (uint8_t)PC_NETGAME_INTERACT_KIND_BURY", "in->_rsv0 == 0",
                                "t->_rsv0 == 0", "t->flags == 0", "t->aux_cond == 0", "t->aux_item == 0", "t->txn_nonce != 0", "t->txn_seq != 0",
                                "PC_NETGAME_TXN_DEST_POCKET && t->slot < (uint8_t)mPr_POCKETS_SLOT_COUNT", "PC_NETGAME_TXN_DEST_WALLET && t->slot == (uint8_t)PC_NETGAME_TXN_SLOT_WALLET",
                                "PC_NETGAME_TXN_DEST_NONE && t->slot < (uint8_t)mPr_POCKETS_SLOT_COUNT")))
    check("H the pre-image goes through the SHARED validator (extracted from the D3 upload path) and the post-image is validated again before any mutation",
          "pcnetgame_rec_validate_inventory(t->pre_pockets, t->pre_conds, t->pre_wallet)" in hd and "pcnetgame_rec_validate_inventory(post, post_conds, post_wallet)" in hd
          and hd.index("pcnetgame_rec_validate_inventory(post, post_conds, post_wallet)") < hd.index("pcnetgame_txn_world_write("))
    check("H the pickup WALLET precondition: amt > 0, pre_wallet <= mPr_WALLET_MAX and amt <= mPr_WALLET_MAX - pre_wallet",
          "amt == 0 || t->pre_wallet > (uint32_t)mPr_WALLET_MAX || amt > (uint32_t)mPr_WALLET_MAX - t->pre_wallet" in hd)
    check("H the post-image is the pre-image plus the delta (pickup POCKET: item + NORMAL cond; WALLET: wallet += amt; drop/bury: slot EMPTY_NO + NORMAL cond)",
          "post[t->slot] = t->item;" in hd and "post_wallet += amt;" in hd and "post[t->slot] = (uint16_t)EMPTY_NO;" in hd
          and hd.count("mPr_SET_ITEM_COND(post_conds, t->slot, mPr_ITEM_COND_NORMAL)") == 2)
    check("H the world step is the shared check / write / publish (no direct pcfa_ call in the handler); the bury reject echo follows the RESULT",
          not re.search(r"pcfa_\w+\(", hd) and hd.index("pcnetgame_host_send_bury_reject(") > hd.index("PC_NETGAME_TXN_REASON_WORLD_CHANGED, 1, rec, fail"))
    # explicit result on every return path
    lines = hd_raw.split("\n")
    rets = [i for i, ln in enumerate(lines) if re.match(r"\s*return;", ln)]
    ok_all = len(rets) >= 15 and "not READY: dropped silently" in lines[rets[0]] and "s_host_peer_link[peer] != PC_NETGAME_LINK_READY" in "\n".join(lines[max(0, rets[0] - 3):rets[0]])
    bad = []
    for i in rets[1:]:
        ln = lines[i]
        prev = "\n".join(lines[max(0, i - 6):i])
        if "RESULT sent" in ln:
            if not re.search(r"pcnetgame_txn_(?:reject|send_applied|send_result)\(", prev):
                bad.append((i, "RESULT annotation without a sender call"))
        elif "FAULT: no result by design" in ln:
            if "[TEST-ONLY]" not in prev:
                bad.append((i, "FAULT annotation without a TEST-ONLY fault branch"))
        else:
            bad.append((i, "unannotated return"))
    check(f"H EVERY return after the READY gate ({len(rets) - 1} of them) is preceded by a RESULT sender (pcnetgame_txn_reject / _send_applied / _send_result) "
          f"or is one of the two TEST-ONLY fault branches; the very first return is the READY gate (offenders: {bad})",
          not bad and ok_all)
    fault_returns = [lines[i] for i in rets if "FAULT: no result by design" in lines[i]]
    check("H exactly two fault branches skip the RESULT (ignore_commit, kill_peer_after_commit) and the fall-through end sends the APPLIED RESULT",
          len(fault_returns) == 2 and hd.rstrip().endswith("}") and hd.index("pcnetgame_txn_send_applied(peer, idx, in, slot, (uint8_t)PC_NETGAME_TXN_REASON_NONE") < hd.rindex("pcnetgame_rec_start_push("))
    rej_calls = [m.start() for m in re.finditer(r"pcnetgame_txn_reject\(", hd)]
    nxt_ok = all(re.match(r"[^;]*;\s*(?:/\*[^*]*\*/\s*)?(?:pcnetgame_rec_stale_push\([^;]*;\s*|if \(in->kind[^}]*\}\s*)?return;", hd[p:]) is not None or
                 re.search(r"return;", hd[p:p + 700]) is not None for p in rej_calls)
    check("H every pcnetgame_txn_reject(..) in the handler is followed by a return (a REJECTED never falls through into a mutation)", nxt_ok and len(rej_calls) >= 14)
    check("H the txn RESULT sender sets host_session from s_rec_host_session and echoes nonce/seq/request_id/kind; APPLIED carries the cdig of the mirror image",
          all(x in blk for x in ("out.host_session = s_rec_host_session;", "out.txn_nonce = in->tag.txn_nonce;", "out.txn_seq = in->tag.txn_seq;", "out.request_id = in->request_id;",
                                 "pcnetgame_crec_cown_digest(be)", "pcnetgame_rec_export_be(idx, be)")))
    check("H a push in flight is restarted at the new rev: 'if (st->rec_push_active) pcnetgame_rec_start_push(peer, idx, st->rec_push_kind)' is the last statement",
          hd.rstrip().endswith("if (st->rec_push_active) {\n        pcnetgame_rec_start_push(peer, idx, st->rec_push_kind);\n    }"))
    check("H the extractions exist and are shared: validate_inventory (D3 upload + txn), stale_push (D3 upload + txn), cown_digest (txn + D3 client)",
          c.count("pcnetgame_rec_validate_inventory(") >= 4 and c.count("pcnetgame_rec_stale_push(") == 8 and c.count("pcnetgame_crec_cown_digest(") >= 4  # X3: + the grant handler's STALE_IMAGE push; town services: + the TS handler's; mail milestone 1: + the mail handler's two (stale base, stale letter); mail milestone 2: + the take handler's one (stale base; a stale mailbox letter re-pushes the MAILBOX_LETTER instead)
          and "static uint32_t pcnetgame_crec_cown_digest" in blk and "static uint32_t pcnetgame_crec_cown_digest" not in c_raw[c_raw.index("===== D3 CLIENT BEGIN"):])

    # ------------------------------------------------------------------ S: single sanctioned writers
    wr = strip_comments(func_body(blk_raw, "pcnetgame_rec_txn_write_inventory"))
    assigns = sorted(set(re.findall(r"\br->([\w\.]+(?:\[\w+\])?)\s*=[^=]", wr)))
    check(f"S pcnetgame_rec_txn_write_inventory writes ONLY inventory.pockets[i], inventory.item_conditions and inventory.wallet (found {assigns}), "
          "after the own-resident / bound-index gate",
          assigns == sorted(["inventory.pockets[i]", "inventory.item_conditions", "inventory.wallet"]) and "pcnetgame_rec_txn_idx_ok(idx)" in wr and "memcpy" not in wr)
    idxok = strip_comments(func_body(blk_raw, "pcnetgame_rec_txn_idx_ok"))
    check("S the host's OWN resident (and a bad index / non-existing record) is refused by the writer's gate", "pcnetgame_host_own_resident_idx()" in idxok and "idx == own" in idxok
          and "exists == TRUE" in idxok and "idx >= 0 && idx < PLAYER_NUM" in idxok)
    wr_calls = len(re.findall(r"pcnetgame_rec_txn_write_inventory\(", c))
    check("S the writer has exactly THREE callers: the X1 commit handler (after the world write), since X3 the X3 grant core (after the world commit) and, since town services, the "
          "MUSEUM_DONATE / POLICE_CLAIM / SHOP_BUY / SHOP_SELL handler (after the service commit; it passes its computed post_wallet since the shop milestone); all pass the gate index",
          wr_calls == 4 and c.count("pcnetgame_rec_txn_write_inventory(idx, post, post_conds, post_wallet)") == 2
          and c.count("pcnetgame_rec_txn_write_inventory(idx, post, post_conds, t->pre_wallet)") == 1)
    direct = re.findall(r"Save_Get\(private_data\)\[[^\]]*\]\s*(?:\.\w+(?:\[[^\]]*\])?)+\s*=[^=]", blk)
    check("S the X1 block has no direct private_data assignment, memcpy/memset into it, or mPr_ writer (the txn path never calls mPr_SetPossessionItem / "
          "mPr_SetFreePossessionItem / mPr_GivePossessionBells / mPr_SetItemCollectBit)",
          not direct and not re.search(r"mem(?:cpy|set)\(\s*&?\s*\(?(?:\(uint8_t\*\))?\s*&?Save_Get\(private_data\)", blk)
          and not re.search(r"\bmPr_(?:Set\w*|Give\w*|Clear\w*)\s*\(", blk) and "mPr_SetPossessionItem" not in blk)
    mpr = sorted(set(re.findall(r"\b(mPr_\w+)\s*\(", blk)))
    check(f"S the only mPr_ function the X1 block calls is the read-only mPr_GetAmountForMoneyItem (+ the pure bit macro mPr_SET_ITEM_COND; found {mpr}); no RNG anywhere in it",
          mpr == ["mPr_GetAmountForMoneyItem", "mPr_SET_ITEM_COND"] and not re.search(r"\b(?:fqrand|RANDOM(?:_F)?|rand|pcnetgame_rec_rand32|SDL_GetPerformanceCounter)\s*\(", blk))
    wh = strip_comments(c_raw[c_raw.index("static const char* pcnetgame_txn_world_check"):c_raw.index("static void pcnetgame_txn_world_publish")])
    check("S the world helpers do not touch the resident record at all (no private_data / Now_Private / mPr_)",
          "private_data" not in wh and "Now_Private" not in wh and not re.search(r"\bmPr_\w+\(", wh))
    sb = strip_comments(func_body(c_raw, "pcnetgame_handle_host_confirm"))
    check("S the legacy confirm handler never writes the resident record and has no pcfa_set_tile / pcfa_set_deposit of its own (the shared world helpers do)",
          "private_data" not in sb and not re.search(r"pcfa_set_\w+\(", sb) and "pcnetgame_txn_world_write(" in sb)
    users = sorted(set(re.findall(r"(\w+)\s*\(", strip_comments(c_raw[c_raw.index("static int pcnetgame_txn_world_write"):c_raw.index("static void pcnetgame_txn_world_publish")]))))
    check("S pcfa_set_tile is called in exactly one place for these kinds: pcnetgame_txn_world_write", "pcfa_set_tile" in users and wh.count("pcfa_set_tile(") == 1)
    check("S the D3 block still has merge_into_save as its ONLY private_data writer (X1 added nothing to it) and documents the second writer",
          "pcnetgame_rec_txn_write_inventory()" in d3_raw[:d3_raw.index("D3-0 (b)")] and "outside this one" in d3_raw[:d3_raw.index("D3-0 (b)")])

    # ------------------------------------------------------------------ J: journal
    check("J the journal is a bounded ring per record SLOT (residents + guests, guests G1): PC_NETGAME_TXN_RING 16 entries, 4 fenced nonces, array [PC_NETGAME_REC_SLOTS] (PLAYER_NUM + PC_NETGAME_GUEST_MAX), eviction by head advance",
          "#define PC_NETGAME_TXN_RING       16" in c_raw and "#define PC_NETGAME_TXN_FENCED_NUM 4" in c_raw and "static PCNetGameTxnResident s_txn_res[PC_NETGAME_REC_SLOTS];" in c_raw
          and "#define PC_NETGAME_REC_SLOTS (PLAYER_NUM + PC_NETGAME_GUEST_MAX)" in c_raw
          and "PCNetGameTxnLog ring[PC_NETGAME_TXN_RING];" in blk and "R->head = (uint8_t)(((int)R->head + 1) % PC_NETGAME_TXN_RING);" in blk
          and "_Static_assert(sizeof(PCNetGameTxnLog) == 28" in blk and L.PC_NETGAME_TXN_RING == 16 and L.PC_NETGAME_TXN_FENCED_NUM == 4)
    check("J a new nonce fences the old one (FIFO memmove), wipes the ring and max_seq; replays compare the hash over the whole 72-byte message",
          "memmove(&R->fenced[0], &R->fenced[1]" in blk and "R->head = 0;" in blk and "R->n = 0;" in blk and "R->max_seq = 0;" in blk
          and "hash = pcnetgame_fnv1a32(in, sizeof(*in));" in hd)
    reset = strip_comments(func_body(c_raw, "pcnetgame_reset_all_host_peer_state"))
    check("J the journal is NOT cleared per peer: pcnetgame_reset_all_host_peer_state mentions neither s_txn_res nor the journal clear",
          "s_txn_res" not in reset and "pcnetgame_txn_journal_clear" not in reset)
    calls = {n: ("pcnetgame_txn_journal_clear(" in strip_comments(func_body(c_raw, n))) for n in ("pcnetgame_rec_on_town_changed", "pcnetgame_rec_on_world_reset", "pcnetgame_rec_slot")}
    gcalls = {n: ("pcnetgame_txn_journal_clear(PLAYER_NUM + g)" in strip_comments(func_body(c_raw, n))) for n in ("pcnetgame_guest_install", "pcnetgame_guest_create", "pcnetgame_guest_rollback_create")}
    check(f"J cleared by exactly the three spec'd events: town change, world reset, slot re-init ({calls}) + the three guest-table lifecycle events (guests G1: a guest entry is installed / created / rolled back: {gcalls}); no other caller",
          all(calls.values()) and all(gcalls.values()) and c.count("pcnetgame_txn_journal_clear(") == 8)  # declaration + definition + 3 calls + 3 guest-table calls
    check("J the journal is memory-only: nothing of it reaches the records file / GCI (no s_txn_res near pc_mp_records / store build)",
          "s_txn_res" not in strip_comments(func_body(c_raw, "pcnetgame_rec_store_build")) and "s_txn_res" not in strip_comments(func_body(c_raw, "pcnetgame_rec_store_write")))
    lib = open(os.path.join(HERE, "net_spike_lib.py"), encoding="utf-8").read()
    fcls = lib[lib.index("class FakeClient"):lib.index("def connect_ready_clients")]
    rcs = re.search(r"    def _reset_connection_state\(self\):.*?\n\n", fcls, re.S).group(0)
    check("J the FakeClient (test double of the real client's s_txn_nonce / s_txn_next_seq) keeps nonce + seq across reconnect; only new_process() re-rolls the nonce and restarts seq",
          "txn_nonce" not in rcs and "txn_seq" not in rcs and "def new_process(self" in fcls and fcls.count("self.txn_nonce = new_txn_nonce()") == 2)

    # ------------------------------------------------------------------ L: legacy path
    check("L the named switch PC_NETGAME_TXN_RETIRE_LEGACY_COMMIT is 1 (X1b flipped it: the real client sends TXN_COMMIT, CONFIRM(COMMIT) is retired)", re.search(r"^#define PC_NETGAME_TXN_RETIRE_LEGACY_COMMIT 1\s*$", c_raw, re.M) is not None)
    check("L the old 'legacy commit processed as before' branch (live only while the constant is 0) is still present BEHIND the retire return and writes through the shared world helpers: "
          "it can no longer run, so it is audited here instead of at runtime (test_txn_protocol MIX tests the retired behaviour)",
          "rec->commit_path = 1;" in sb and sb.index("if (PC_NETGAME_TXN_RETIRE_LEGACY_COMMIT)") < sb.index("rec->commit_path = 1;")
          and "pcnetgame_txn_world_write(rec, new_value, dep_on)" in sb and "private_data" not in sb)
    check("L CONFIRM(COMMIT) when the switch is 1: logged '... is retired in v8 -- use TXN_COMMIT', reservation released ABORTED, nothing mutated; ABORT is never retired",
          "if (PC_NETGAME_TXN_RETIRE_LEGACY_COMMIT) {" in sb and "CONFIRM(COMMIT) is retired in v8 -- use TXN_COMMIT" in c_raw
          and "PC_NETGAME_PHASE_ABORTED, \"legacy CONFIRM(COMMIT) is retired\"" in sb)
    check("L the retire branch comes after the ABORT branch and before the legacy world step", sb.index("PC_NETGAME_CONFIRM_ABORT") < sb.index("if (PC_NETGAME_TXN_RETIRE_LEGACY_COMMIT)") < sb.index("pcnetgame_txn_world_check("))
    check("L a reservation is consumed ONCE across both paths: the legacy commit sets commit_path = 1, the txn sets commit_path = 2 + phase DONE, and each path "
          "requires phase PENDING with the same request id (so neither can apply after the other); the stale-commit diagnostic names the winner",
          "rec->commit_path = 1;" in sb and "rec->commit_path = 2;" in hd and "rec->phase != (uint8_t)PC_NETGAME_PHASE_PENDING || rec->request_id != in->request_id" in hd
          and "rec->phase != (uint8_t)PC_NETGAME_PHASE_PENDING || rec->request_id != in->request_id" in sb and "already committed via" in sb
          and "request already committed (a reservation is consumed once)" in hd)
    check("L the reservation carries pocket_slot (0xFF for pickup; the request's slot for drop and bury) and the three request handlers set it",
          "rec->pocket_slot = 0xFFu;" in c and c.count("rec->pocket_slot = in->pocket_slot_idx;") == 2 and "uint8_t  pocket_slot;" in c_raw and "uint8_t  commit_path;" in c_raw)
    check("L the world write publishes through ONE path for both commit paths (FIELD_UPDATE + the pickup PLAYER_ACTION hint): pcnetgame_host_emit_player_action has the definition + 2 call sites",
          c.count("pcnetgame_host_emit_player_action(") == 3 and "pcnetgame_txn_world_publish(peer, rec, (int)in->kind)" in sb)
    check("L the legacy bury success BURY_RESULT(1) is sent ONLY by the legacy path (the txn handler never sends a BURY_RESULT success)",
          "pcnetgame_host_send_bury_result(" in sb and "pcnetgame_host_send_bury_result(" not in hd)

    # ------------------------------------------------------------------ C: client half (X1b)
    cb_i, ce_i = c_raw.index("===== X1b CLIENT BEGIN"), c_raw.index("===== X1b CLIENT END")
    cblk_raw = c_raw[cb_i:ce_i]
    cblk = strip_comments(cblk_raw)
    check("C the X1b CLIENT block sits right after the D3 CLIENT block (D3 audit unchanged) and before the host identity code",
          c_raw.index("===== D3 CLIENT END") < cb_i < ce_i < c_raw.index("static void pcnetgame_host_process_identity("))
    check("C s_bury_committed / PCNetGameBuryCommitted / PC_NETGAME_BURY_COMMITTED_KEEP are gone from the C source (the M9-D G2-3 restore mechanism is deleted)",
          "s_bury_committed" not in c and "PCNetGameBuryCommitted" not in c and "BURY_COMMITTED_KEEP" not in c)
    hp, hdr, hb = (func_body(c, n) for n in ("pcnetgame_handle_client_pickup_result", "pcnetgame_handle_client_drop_result",
                                                                  "pcnetgame_handle_client_bury_result"))
    check("C the three client result handlers contain NO pocket / wallet write: no mPr_Set* / mPr_Give*, no 'inventory.<field> =' and no CONFIRM(COMMIT) send",
          all(x != "" for x in (hp, hdr, hb))
          and not any(re.search(r"\bmPr_(?:Set|Give)\w*\s*\(", x) or re.search(r"inventory\.\w+(?:\[[^\]]*\])?\s*[-+|&]?=[^=]", x) or "PC_NETGAME_CONFIRM_COMMIT" in x
                      for x in (hp, hdr, hb)))
    check("C each result handler begins the transaction exactly once with its own kind and the validated pending owner stamp",
          hp.count("pcnetgame_txn_begin(") == 1 and hdr.count("pcnetgame_txn_begin(") == 1 and hb.count("pcnetgame_txn_begin(") == 1
          and "pcnetgame_txn_begin(kind, in->request_id, 0xFFu, (uint16_t)in->granted_item, NULL, &s_pickup_pending.owner)" in hp
          and "have_deferred ? &deferred : NULL" in hdr and "&s_drop_pending.owner" in hdr and "&s_bury_pending.owner" in hb)
    check("C every handler's 'accepted RESULT without a matching pending request: ABORT(STALE)' path is skipped while a txn for the SAME kind + request id is in flight "
          "(a late replay of the provisional accept must not release the host's reservation)",
          all(x.index("pcnetgame_txn_busy_with(kind, in->request_id)") < x.index("PC_NETGAME_CONFIRM_REASON_STALE") for x in (hp, hdr, hb)))
    check("C the bury handler's reject branch still reconciles the tile and has no restore (no mPr_*, the retained-claim branch is gone)",
          "pcnetgame_client_apply_tile(" in hb and "RESTORED" not in hb and "LOST" not in hb)
    check("C apply: the pocket / wallet writes of the X1b block are in pcnetgame_txn_apply_applied ONLY (try_send / begin / tick / the RESULT handler / the test hook / the lock predicate "
          "never write inventory.pockets / item_conditions / wallet)",
          len(re.findall(r"inventory\.pockets\[[^\]]*\]\s*=[^=]", cblk)) >= 1
          and all(re.search(r"inventory\.(?:pockets\[[^\]]*\]|item_conditions|wallet)\s*(?:\+)?=[^=]", func_body(c, n)) is None
                  for n in ("pcnetgame_txn_try_send", "pcnetgame_txn_begin", "pcnetgame_txn_tick", "pcnetgame_handle_client_txn_result", "pcnetgame_run_txn_test_hook",
                            "pc_net_game_client_pocket_locked"))
          and "inventory.pockets[i] = (mActor_name_t)in->post_pockets[i];" in cblk and "mPr_SetItemCollectBit((mActor_name_t)gitem)" in cblk)
    ap = func_body(c, "pcnetgame_txn_apply_applied")
    check("C apply order: owner stamp first (a changed player applies nothing); exact post-image only when the local inventory still equals the pre-image, else the delta, "
          "else (delta impossible) the host post-image; the exchange replacement only AFTER the pocket step; the base moves last, only forward and only on the same host session",
          ap.index("pcnetgame_owner_stamp_matches(&T->owner)") < ap.index("same = 0") < ap.index("how = \"host post-image\"") < ap.index("delta impossible")
          < ap.rindex("if (is_exch) {") < ap.rindex("pcnetgame_crec_set_base(")
          and "in->host_session == s_crec.base_session && in->epoch == s_crec.base_epoch && in->rev > s_crec.base_rev" in ap and "s_crec.next_check_ms = 0;" in ap
          # X3: the exchange replacement is part of the host post-image (the exact path) or a RAW write of the credit-verified tag value (the delta path);
          # mPr_SetPossessionItem (RNG for presents, writes Common_Get(now_private)) is gone from the apply step
          and "mPr_SetPossessionItem" not in ap and "np->inventory.pockets[s] = (mActor_name_t)t->aux_item;" in ap)
    rh = func_body(c, "pcnetgame_handle_client_txn_result")
    check("C the RESULT handler matches state SENT + nonce + seq + request id + kind (anything else is a logged late replay), FREES the state before applying on every matched "
          "outcome, and a REJECTED outcome writes nothing locally",
          "s_ctxn.state != PC_NETGAME_CTXN_SENT || in->txn_nonce != s_txn_nonce || in->txn_seq != s_ctxn.seq" in rh
          and "in->request_id != s_ctxn.request_id || in->kind != s_ctxn.kind" in rh
          and rh.index("memset(&s_ctxn, 0, sizeof(s_ctxn));") < rh.index("PC_NETGAME_TXN_OUTCOME_APPLIED") < rh.index("pcnetgame_txn_apply_applied(&T, in)")
          and "inventory" not in rh and "mPr_" not in rh)
    check("C the dispatcher routes exactly-sized TXN_RESULT messages to the handler (READY link only, aligned copy)",
          re.search(r"size == sizeof\(PCNetGameTxnResultMsg\) && data\[0\] == \(uint8_t\)PC_NETGAME_MSG_TXN_RESULT\) \{\s*PCNetGameTxnResultMsg tr;\s*if \(s_client_link != PC_NETGAME_LINK_READY\) return;\s*memcpy\(&tr, data, sizeof\(tr\)\);[^\n]*\n\s*pcnetgame_handle_client_txn_result\(&tr\);",
                    c_raw) is not None)
    reset = func_body(c, "pcnetgame_reset_client_session_state")
    shut = func_body(c, "pc_net_game_shutdown")
    check("C nonce / seq are PROCESS state: neither s_txn_nonce nor s_txn_next_seq is touched by the session reset or the shutdown (the session reset clears s_ctxn only); "
          "they are written only in pcnetgame_txn_alloc_seq",
          "memset(&s_ctxn, 0, sizeof(s_ctxn));" in reset and "s_txn_nonce" not in reset and "s_txn_next_seq" not in reset
          and "s_txn_nonce" not in shut and "s_txn_next_seq" not in shut
          and len(re.findall(r"s_txn_nonce\s*=[^=]", c)) == 2 and len(re.findall(r"s_txn_next_seq\s*=[^=]", c)) == 2
          and all(("s_txn_nonce =" not in func_body(c, n)) for n in ("pcnetgame_txn_try_send", "pcnetgame_txn_begin", "pcnetgame_txn_tick"))
          and "uint32_t           s_txn_next_seq = 1;" in c_raw)
    al = func_body(c, "pcnetgame_txn_alloc_seq")
    check("C seq: monotone (s_txn_next_seq++), a wrap re-rolls the nonce (different from the old one) and restarts at 1; the nonce is lazily rolled non-zero",
          "s_txn_nonce = pcnetgame_rec_rand32();" in al and "s_txn_next_seq == 0u" in al and "while (s_txn_nonce == old)" in al and "s_txn_next_seq = 1u;" in al
          and "return s_txn_next_seq++;" in al)
    ts = func_body(c, "pcnetgame_txn_try_send")
    tk = func_body(c, "pcnetgame_txn_tick")
    check("C the request is built ONCE from the pre-image of that moment and its bytes are STORED (T->wire / T->tag, X3: COMMIT or grant request) only after the send was queued; a failed send leaves it QUEUED with the same seq",
          "memcpy(T->wire, wire, wlen);" in ts and ts.index("pc_net_send(0, PC_NET_RELIABLE, wire, wlen)") < ts.index("memcpy(T->wire, wire, wlen);") < ts.index("T->state = PC_NETGAME_CTXN_SENT;")
          and "if (T->seq == 0u) {" in ts and ts.count("pc_net_send(") == 1)
    check("C the COMMIT never goes on the wire while an upload's chunks are still being queued, nor before the record is SYNCED, nor for another player; slot/room re-checked at build",
          "if (s_crec.up_active && s_crec.up_next <= PC_NETGAME_REC_CHUNK_COUNT) {" in ts and "s_crec.state != PC_NETGAME_CRS_SYNCED" in ts
          and ts.index("pcnetgame_owner_stamp_matches(&T->owner)") < ts.index("s_crec.state != PC_NETGAME_CRS_SYNCED") < ts.index("s_crec.up_active")
          < ts.index("pcnetgame_txn_pickup_route(") < ts.index("pc_net_send(")
          and "Now_Private->inventory.pockets[slot] != (mActor_name_t)T->item" in ts)
    check("C every TXN_COMMIT put on the wire comes from try_send (first send) or the tick's resend of the STORED s_ctxn.wire bytes: the message id appears once in the block (the builder), "
          "the tick sends the stored bytes and builds nothing",
          len(re.findall(r"PC_NETGAME_MSG_TXN_COMMIT", cblk)) == 1 and "pc_net_send(0, PC_NET_RELIABLE, s_ctxn.wire, s_ctxn.wire_len)" in tk and tk.count("pc_net_send(") == 1)
    check("C resend: every PC_NETGAME_TXN_RESEND_MS (2000 ms) the identical stored bytes, NO give-up while READY: the tick never frees/clears the state and never sends an ABORT; "
          "the only s_ctxn clears are the session reset, begin, cancel_queued (QUEUED only) and a matched result",
          "#define PC_NETGAME_TXN_RESEND_MS    2000u" in c_raw and "(uint32_t)(now - s_ctxn.sent_ms) >= PC_NETGAME_TXN_RESEND_MS" in tk
          and "memset(&s_ctxn" not in tk and "s_ctxn.state = PC_NETGAME_CTXN_FREE" not in tk and "CONFIRM" not in tk
          and "s_client_link != PC_NETGAME_LINK_READY" in tk and "if (pcnetgame_txn_tick()) { /* X1b" in c_raw
          and c.count("memset(&s_ctxn, 0, sizeof(s_ctxn))") == 8)  # X3: session reset, begin, begin_grant, cancel_queued, a matched result; town services: + ts_begin; mail milestone 1: + pcnetgame_mail_tick (begins the kind-12 txn); mail milestone 2: + pc_net_game_mail_take_step (begins the kind-13 txn)
    check("C a QUEUED txn (nothing ever on the wire) may be cancelled with an ABORT; that path is unreachable once SENT (cancel_queued is called only from try_send, which returns first unless QUEUED)",
          "if (T->state != PC_NETGAME_CTXN_QUEUED) {" in ts
          and all("pcnetgame_txn_cancel_queued(" not in func_body(c, n) for n in ("pcnetgame_txn_tick", "pcnetgame_handle_client_txn_result", "pcnetgame_txn_apply_applied")))
    check("C lock (a): request_pickup / request_drop / request_bury refuse while pcnetgame_txn_begin_blocked() (= txn busy OR a mail send in AWAIT_CLEAN) (pickup: return 1 = handled/nothing sent; drop and bury: return 0 + log), right after the D3 gate; "
          "exchange_request_drop inherits the refusal through request_drop",
          re.search(r'blocks\("PICKUP"\)\) \{\s*return 1;[^\n]*\n\s*\}\s*if \(pcnetgame_txn_begin_blocked\(\)\) \{\s*return 1;', c_raw) is not None
          and re.search(r'blocks\("DROP"\)\) \{\s*return 0;[^\n]*\n\s*\}\s*if \(pcnetgame_txn_begin_blocked\(\)\) \{[^}]*return 0;', c_raw) is not None
          and re.search(r'blocks\("BURY"\)\) \{\s*return 0;[^\n]*\n\s*\}\s*if \(pcnetgame_txn_begin_blocked\(\)\) \{[^}]*return 0;', c_raw) is not None
          and "pc_net_game_request_drop(slot, hand_item, ut_x, ut_z)" in func_body(c, "pc_net_game_exchange_request_drop"))
    pl = open(os.path.join(ROOT, "src", "game", "m_player_lib.c"), "rb").read().decode("utf-8", "replace").replace("\r\n", "\n")
    sm = pl[pl.index("extern int mPlib_able_submenu_type1(GAME* game) {"):]
    sm = sm[:sm.index("\n}\n")]
    lk = func_body(c, "pc_net_game_client_pocket_locked")
    check("C lock (b): mPlib_able_submenu_type1 ANDs in pc_net_game_client_pocket_locked() under #ifdef TARGET_PC ONLY, before any vanilla test; the predicate is CLIENT-only "
          "(returns 0 for any other role) and reads only the pending flags + txn busy; the include is TARGET_PC-guarded; no loop in the predicate",
          "#ifdef TARGET_PC\n    /* X1b lock (b)" in sm and "if (pc_net_game_client_pocket_locked()) {\n        return FALSE;\n    }\n#endif" in sm
          and sm.index("pc_net_game_client_pocket_locked()") < sm.index("check_request_main_priority_proc")
          and '#ifdef TARGET_PC\n#include "pc_bswap.h"\n#include "pc_net_game.h"' in pl
          and "s_role != PC_NETGAME_ROLE_CLIENT" in lk and "return 0;" in lk and "s_pickup_pending.valid || s_drop_pending.valid || s_bury_pending.valid || pcnetgame_txn_busy()" in lk
          and not re.search(r"\b(?:for|while)\b", lk) and "int pc_net_game_client_pocket_locked(void);" in h)
    check("C locks (c)/(d): D3 uploads are deferred and D3 adoption blocked while a txn is unresolved; the quit flush does not start an upload then",
          "pcnetgame_txn_busy()" in func_body(c, "pcnetgame_crec_upload_deferred") and "pcnetgame_txn_busy()" in func_body(c, "pcnetgame_crec_adopt_blocker")
          and "pcnetgame_txn_busy()" in func_body(c, "pc_net_game_client_record_quit_flush"))
    ta = func_body(c, "pcnetgame_crec_try_adopt")
    check("C lock (e): a staged push of the same session+epoch with rev < base_rev is applied HOST-OWNED ranges only and NEVER lowers the base (set_base is skipped; base_rev only moves via set_base)",
          "older_than_base = s_crec.state == PC_NETGAME_CRS_SYNCED && s_crec.st_session == s_crec.base_session &&" in ta
          and "s_crec.st_epoch == s_crec.base_epoch && s_crec.st_rev < s_crec.base_rev;" in ta and "|| older_than_base ||" in ta
          and re.search(r"if \(older_than_base\) \{[^}]*\} else \{\s*pcnetgame_crec_set_base\(", ta) is not None
          and len(re.findall(r"s_crec\.base_rev\s*=[^=]", c)) == 1)
    host_c = func_body(c, "pcnetgame_handle_host_data")
    check("C host: a structurally malformed COMMIT (BAD_SHAPE) and a wrong-size TXN_COMMIT count a violation toward the existing 3-violation close policy (after the RESULT is sent); semantic refusals never do",
          re.search(r'PC_NETGAME_TXN_REASON_BAD_SHAPE, 0, NULL, "malformed TXN_COMMIT"\);\s*[^\n]*\n\s*pcnetgame_rec_violation\(peer, "malformed TXN_COMMIT \(BAD_SHAPE\)"\);', c) is not None
          and "size != sizeof(PCNetGameTxnCommitMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_TXN_COMMIT" in host_c
          and 'pcnetgame_rec_violation(peer, "TXN_COMMIT wrong size")' in host_c and hd.count("pcnetgame_rec_violation(") == 1)
    hook = func_body(c, "pcnetgame_run_txn_test_hook")
    check("C the test hook --txn-test-pickup-drop: default OFF (int g_pc_txn_test_pickup_drop = 0), parsed only as an exact flag with a loud [TEST-ONLY] arm line, documented in --help and "
          "pc_platform.h, no environment override, role-gated to CLIENT in the C hook, every step logs [TEST-ONLY], and it only calls the real request functions",
          "int g_pc_txn_test_pickup_drop = 0;" in main_c and 'strcmp(argv[i], "--txn-test-pickup-drop") == 0' in main_c and "[NET][TXN][TEST-ONLY] --txn-test-pickup-drop armed" in main_c
          and "--txn-test-pickup-drop  Client-only TEST hook" in main_c and "extern int           g_pc_txn_test_pickup_drop;" in plat_h
          and "--txn-test-pickup-drop. CLIENT only" in plat_h and "getenv" not in cblk
          and "s_role != PC_NETGAME_ROLE_CLIENT" in hook and hook.count("[NET][TXN][TEST-ONLY]") >= 3
          and "pc_net_game_request_drop(found, (int)s_item, s_ux, s_uz)" in cblk and "pc_net_game_request_pickup(s_ux, s_uz, (int)s_item)" in cblk
          and c.count("pcnetgame_run_txn_test_hook();") == 1)
    check("C the FakeClient default commit path is 'txn' (like the real client); the legacy path is only an explicit opt-in",
          L.FakeClient.commit_mode == "txn")

    # ------------------------------------------------------------------ R: X1 review fixes (M1, M2, L1, L3, L5)
    demo = read("src/game/m_demo.c")
    wts = demo[demo.index("static int wait_talk_start() {"):]
    wts = wts[:wts.index("\n}\n")]
    check("R M1 the ONE talk-start function every conversation goes through (m_demo.c wait_talk_start: villagers, shops, museum, post office...) refuses to start "
          "while the CLIENT lock is set, under #ifdef TARGET_PC only, before any vanilla step, and only for a fresh talk (change_player), returning the existing "
          "'cannot talk now' answer FALSE (no stuck dialogue: the speaker keeps waiting)",
          "#ifdef TARGET_PC\n    /* X1 review M1" in wts and "if (demo->data.talk.change_player && pc_net_game_client_pocket_locked()) {\n        return FALSE;\n    }\n#endif" in wts
          and wts.index("pc_net_game_client_pocket_locked()") < wts.index("mPlib_request_main_talk_type1(")
          and '#ifdef TARGET_PC\n#include "pc_net_game.h"' in demo)
    check("R M1 the lock predicate it uses is the client-only one (single-player and host never locked)",
          "s_role != PC_NETGAME_ROLE_CLIENT" in func_body(c, "pc_net_game_client_pocket_locked"))
    ap2 = func_body(c, "pcnetgame_txn_apply_applied")
    imp = ap2[ap2.index("delta impossible"):]
    check("R M1(b) 'delta impossible' is NARROWED: it no longer copies the whole post-image (no loop over post_pockets, no wallet = post_wallet there); it touches only the "
          "transaction's slot (pickup) or caps the wallet (wallet pickup) and keeps the local slots/wallet otherwise, logging 'possible local loss' for a drop/bury whose item left",
          "np->inventory.wallet = in->post_wallet;" not in imp[:imp.index("if (is_exch) {")] and "possible local loss" in imp
          and "np->inventory.pockets[t->slot] = (mActor_name_t)in->post_pockets[t->slot];" in imp and "(u32)mPr_WALLET_MAX" in imp
          and "for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {\n                np->inventory.pockets[i] = (mActor_name_t)in->post_pockets[i];" not in imp[:imp.index("if (is_exch) {")])
    tk2 = func_body(c, "pcnetgame_txn_tick")
    check("R M2 a transaction unresolved for PC_NETGAME_TXN_RESET_MS (60 s) leaves the session through the normal exit (pc_net_game_shutdown), loudly "
          "'[NET][TXN] unresolved >60 s: reconnect required', without touching the inventory; the caller returns on it; QUEUED counts too (first_ms)",
          "#define PC_NETGAME_TXN_RESET_MS    60000u" in c_raw and "(uint32_t)(now - s_ctxn.first_ms) >= PC_NETGAME_TXN_RESET_MS" in tk2
          and "[NET][TXN] unresolved >%u s: reconnect required" in tk2 and "pc_net_game_shutdown();" in tk2 and "return 1;" in tk2
          and "inventory." not in tk2 and "if (pcnetgame_txn_tick()) {" in func_body(c, "pcnetgame_client_tick"))
    hc = func_body(c, "pcnetgame_handle_host_txn_commit")
    sy = hc[hc.index("st->rec_state != PC_NETGAME_RECS_SYNCED || !s_host_world_ready"):hc.index("shape_ok = in->kind")]
    check("R L1 NOT_SYNCED and BUSY refusals (before the journal step) RELEASE a still-PENDING reservation of this request (ABORTED) instead of leaving it for 20 s",
          "pcnetgame_host_interaction(peer, (int)in->kind)" in sy and sy.count("pcnetgame_txn_reject(") == 2 and "r0, \"resident record not SYNCED\"" in sy
          and "r0, \"host world not ready\"" in sy and "r0->phase == (uint8_t)PC_NETGAME_PHASE_PENDING && r0->request_id == in->request_id" in sy)
    rj = func_body(c, "pcnetgame_txn_reject")
    sr = func_body(c, "pcnetgame_txn_send_result")
    check("R L3 refused-COMMIT host logs are rate limited (first 400 per connection, then every 100th) through a per-peer counter; refusals never count as violations "
          "(pcnetgame_rec_violation appears only for BAD_SHAPE / wrong size)",
          "static int pcnetgame_txn_log_ok(PCNetPeerId peer)" in c and "c < 400u || (c % 100u) == 0u" in c and "uint32_t             txn_log_n;" in c
          and "pcnetgame_txn_log_ok(peer)" in rj and "pcnetgame_txn_log_ok(peer)" in sr and "rec_violation" not in rj and "rec_violation" not in sr
          and hc.count("pcnetgame_rec_violation(") == 1)
    check("R L5 commit_path is reset to 0 at all three reservation sites (pickup, drop, bury requests)",
          c.count("rec->commit_path = 0;") == 3)

    # ------------------------------------------------------------------ F: fault hooks
    check("F the globals default to OFF (mode 0, nth 1, count 1) in pc_main.c and are declared in pc_platform.h with the documented modes",
          "int g_pc_txn_fault_mode = 0;" in main_c and "int g_pc_txn_fault_nth = 1;" in main_c and "int g_pc_txn_fault_arg = 1;" in main_c
          and "extern int           g_pc_txn_fault_mode;" in plat_h and all(m in plat_h for m in ("ignore_commit", "fail_world", "expire", "drop_result", "kill_peer_after_commit"))
          and "HOST role ONLY" in plat_h and "OFF by default" in plat_h)
    check("F --txn-fault is host-gated twice: pc_main.c exits 2 unless the role is host (before stdout is redirected), and the C hook re-checks s_role == HOST",
          "if (g_pc_net_role != 1) {" in main_c and "--txn-fault is a HOST-only test hook" in main_c and "return 2;" in main_c
          and "s_role != PC_NETGAME_ROLE_HOST" in strip_comments(func_body(blk_raw, "pcnetgame_txn_fault_fire")))
    check("F the startup banner and every firing log loudly with [TEST-ONLY]: 'FAULT INJECTION ENABLED' / 'FAULT INJECTION FIRED'",
          "[NET][TXN][TEST-ONLY] FAULT INJECTION ENABLED" in main_c and "[NET][TXN][TEST-ONLY] FAULT INJECTION FIRED" in blk)
    check("F --help documents the flag", "--txn-fault=MODE[:N[:K]]" in main_c and "HOST-only TEST hook" in main_c)
    fires = re.findall(r"pcnetgame_txn_fault_fire\((PC_TXN_FAULT_\w+)\)", c)
    x3b, x3e = c_raw.index("===== X3 HOST BEGIN"), c_raw.index("===== X3 HOST END")
    x3blk = strip_comments(c_raw[x3b:x3e])
    check(f"F the five modes have exactly one site each in the X1 block ({sorted(fires)} overall) and, since X3, ignore_commit / fail_world / kill_peer_after_commit have ONE more "
          "site each in the X3 grant core (expire and drop_result stay X1-only: a grant has no reservation, and its RESULT goes through the one X1 sender); no environment variable can arm a fault",
          sorted(fires) == sorted(["PC_TXN_FAULT_IGNORE_COMMIT", "PC_TXN_FAULT_IGNORE_COMMIT", "PC_TXN_FAULT_IGNORE_COMMIT", "PC_TXN_FAULT_FAIL_WORLD", "PC_TXN_FAULT_FAIL_WORLD",
                                   "PC_TXN_FAULT_FAIL_WORLD", "PC_TXN_FAULT_EXPIRE", "PC_TXN_FAULT_DROP_RESULT", "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT",
                                   "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT", "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT",
                                   "PC_TXN_FAULT_IGNORE_COMMIT", "PC_TXN_FAULT_FAIL_WORLD", "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT",
                                   "PC_TXN_FAULT_IGNORE_COMMIT", "PC_TXN_FAULT_FAIL_WORLD", "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT"])  # town services: + one site each in the TS handler; mail milestone 1: + one each in the mail handler; mail milestone 2: + one each in the take handler
          and len(re.findall(r"pcnetgame_txn_fault_fire\(PC_TXN_FAULT_", blk)) == 5
          and sorted(re.findall(r"pcnetgame_txn_fault_fire\((PC_TXN_FAULT_\w+)\)", x3blk)) == ["PC_TXN_FAULT_FAIL_WORLD", "PC_TXN_FAULT_IGNORE_COMMIT", "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT"]
          and "getenv" not in blk and "getenv" not in x3blk and "g_pc_txn_fault" not in strip_comments(c_raw[:b]))
    check("F the crash modes are NOT implemented here (X4): no _exit / abort / TerminateProcess in the X1 block", not re.search(r"\b(?:_exit|exit|abort|TerminateProcess)\s*\(", blk))
    check("F pc_main.c parses exactly the five names (and nothing else arms a mode)", all(('"%s"' % m) in main_c for m in ("ignore_commit", "fail_world", "expire", "drop_result", "kill_peer_after_commit"))
          and main_c.count("g_pc_txn_fault_mode = ") == 2)

    return L.summary_and_exit_code(results)


def struct_size(fmt):
    import struct
    return struct.calcsize(fmt)


if __name__ == "__main__":
    sys.exit(main())
