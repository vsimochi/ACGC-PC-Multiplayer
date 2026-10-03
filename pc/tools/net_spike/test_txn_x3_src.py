#!/usr/bin/env python3
"""test_txn_x3_src.py - SOURCE AUDIT of X3: the host-transactional GRANTS (DIG_BURIED / DIG_HOLE bonus / DIG_SHINE bell via FIELD_ACTION_REQUEST,
fish / bug CATCH via CATCH_REQUEST, and the exchange-menu DROP that folds the catch replacement into the transaction).

Pure text checks over pc/src/pc_net_game.c, pc/src/pc_main.c, pc/include/pc_platform.h, the vanilla sources the host tables duplicate
(src/actor/ac_uki_move.c_inc, src/actor/ac_ins_*.c, include/ac_insect_h.h), the Python library and the migrated tests. No game process.
Tier: SOURCE AUDITED.

  W  wire: FIELD_ACTION_REQUEST 12 -> 76 B and CATCH_REQUEST 20 -> 84 B with the trailing 64-byte tag (the tag defined BEFORE its first user), exact
     _Static_asserts + offsets, kinds 4..7, python formats == C, the exchange flag is no longer "MUST be 0", wire_baseline pins them
  H  host grant core (pcnetgame_x3_grant): the algorithm order, a result on every path, the item resolved ONCE (host-resolved / host-derived), the entity
     removal and the world commit only AFTER the post-image validated, raw mirror write, no mPr_* / RNG, the single catch entity removal, the exchange credit
  T  host tables: the duplicated fish table == aUKI_get_fish_type()'s fish_data[]; ITM_INSECT00 + species holds for EVERY case-form insect->item assignment
  C  client: no pocket / wallet / collection write before APPLIED in ANY result handler (field action, catch, pickup, drop, bury); the one writer is
     pcnetgame_txn_apply_applied; every remaining mPr_Set*Possession* site is classified (host-own test trigger / swap rollback / host-local bury)
  K  client plumbing: grants never use the field-action queue, one pocket transaction at a time, byte-identical stored resend, catch outcome + collection
     bits only on TXN_RESULT, the exchange replacement travels in the tag
  P  test hooks: --txn-test-dig-grant default off / loud / documented / client-gated; the migrated tests and helpers
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


def ok_order(body, *needles):
    pos = -1
    for n in needles:
        i = body.find(n, pos + 1)
        if i < 0:
            return False
        pos = i
    return True


def main():
    results = []
    check = lambda d, c: L.check(d, bool(c), results)
    c_raw = read("pc/src/pc_net_game.c")
    c = strip_comments(c_raw)
    main_c = read("pc/src/pc_main.c")
    plat_h = read("pc/include/pc_platform.h")
    x3b, x3e = c_raw.index("===== X3 HOST BEGIN"), c_raw.index("===== X3 HOST END")
    x3_raw = c_raw[x3b:x3e]
    x3 = strip_comments(x3_raw)
    gr = func_body(x3, "pcnetgame_x3_grant")

    # ------------------------------------------------------------------ W: wire
    check("W FIELD_ACTION_REQUEST is 76 bytes (12 + the 64-byte tag at offset 12) and CATCH_REQUEST is 84 bytes (20 + the tag at offset 20): exact _Static_asserts, <= PC_NET_MAX_PAYLOAD",
          "_Static_assert(sizeof(PCNetGameFieldActionRequestMsg) == 76," in c_raw and "_Static_assert(sizeof(PCNetGameCatchRequestMsg) == 84," in c_raw
          and "_Static_assert(offsetof(PCNetGameFieldActionRequestMsg, tag) == 12," in c_raw and "_Static_assert(offsetof(PCNetGameCatchRequestMsg, tag) == 20," in c_raw
          and "_Static_assert(sizeof(PCNetGameFieldActionRequestMsg) <= PC_NET_MAX_PAYLOAD," in c_raw and "_Static_assert(sizeof(PCNetGameCatchRequestMsg) <= PC_NET_MAX_PAYLOAD," in c_raw)
    check("W the 64-byte PCNetGameTxnTag is defined ONCE, BEFORE its first embedding user (FIELD_ACTION_REQUEST), and is reused unchanged by TXN_COMMIT / CATCH_REQUEST / FIELD_ACTION_REQUEST",
          c_raw.count("} PCNetGameTxnTag;") == 1 and c_raw.index("} PCNetGameTxnTag;") < c_raw.index("} PCNetGameFieldActionRequestMsg;") < c_raw.index("} PCNetGameCatchRequestMsg;")
          and "_Static_assert(sizeof(PCNetGameTxnTag) == 64," in c_raw
          and sorted(m.group(2) for m in re.finditer(r"typedef struct \w+ \{([^}]*?PCNetGameTxnTag tag;[^}]*?)\} (\w+);", c)) ==
          ["PCNetGameCatchRequestMsg", "PCNetGameClientTxn", "PCNetGameFieldActionRequestMsg", "PCNetGameTxnCommitMsg"])  # the three wire carriers + the client's stored copy
    check("W the transaction kinds 4..7 (DIG_BURIED / DIG_HOLE / DIG_SHINE / CATCH) are defined and the EXCHANGE flag is documented as an X3 DROP flag (no longer 'MUST be 0')",
          all(re.search(r"#define PC_NETGAME_TXN_KIND_%s\s+%du" % (n, v), c_raw) for n, v in (("DIG_BURIED", 4), ("DIG_HOLE", 5), ("DIG_SHINE", 6), ("CATCH", 7)))
          and "MUST be 0 in X1" not in c_raw and "TXN_COMMIT kind DROP only" in c_raw)
    check("W python: the X3 formats and constants equal the C layout (76 / 84 / 12 / 12 bytes, ids 29 / 30 / 41 / 42, kinds 4..7)",
          (L.FIELD_ACTION_REQUEST_FMT, L.CATCH_REQUEST_FMT) == ("<BBBBIBBHIIBBHBBHII15HHII", "<B3xIIIiIIBBHBBHII15HHII")
          and L.struct.calcsize(L.FIELD_ACTION_REQUEST_FMT) == 76 and L.struct.calcsize(L.CATCH_REQUEST_FMT) == 84
          and L.struct.calcsize(L.FIELD_ACTION_RESULT_FMT) == 12 and L.struct.calcsize(L.CATCH_RESULT_FMT) == 12
          and (L.PC_NETGAME_MSG_FIELD_ACTION_REQUEST, L.PC_NETGAME_MSG_FIELD_ACTION_RESULT, L.PC_NETGAME_MSG_CATCH_REQUEST, L.PC_NETGAME_MSG_CATCH_RESULT) == (29, 30, 41, 42)
          and (L.PC_NETGAME_TXN_KIND_DIG_BURIED, L.PC_NETGAME_TXN_KIND_DIG_HOLE, L.PC_NETGAME_TXN_KIND_DIG_SHINE, L.PC_NETGAME_TXN_KIND_CATCH) == (4, 5, 6, 7)
          and len(L.build_field_action_request(1, 2, 3, 4)) == 76 and len(L.build_catch_request(1, 2, 3, 4)) == 84)
    for name, ids in (("FIELD_ACTION", (29, 30)), ("CATCH", (41, 42))):
        enum = c_raw[c_raw.index("typedef enum PCNetGameMsgType {"):c_raw.index("} PCNetGameMsgType;")]
        check(f"W the message ids of {name} are unchanged ({ids}): no new message id was added by X3 (the max id 56 is the mail milestone 2, 55 the town-services milestone)",
              all(re.search(r"PC_NETGAME_MSG_%s_(?:REQUEST|RESULT)\s*=\s*%d," % (name, i), enum) for i in ids) and wire_baseline.EXPECTED_MAX_MSG_ID == 56)  # X3 added no id; 53 / 54 (reserved X2) + 55 (TOWN_SVC_STATE) came with town services, 56 (MAILBOX_LETTER) with mail milestone 2
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    check("W wire_baseline (pins the two grown structs EXACTLY, everything else identical to HEAD): all %d checks green" % len(wb), wb and all(x[1] for x in wb))

    # ------------------------------------------------------------------ H: host grant core
    check("H the X3 block sits right after the X1 block and the dispatch is unchanged (FIELD_ACTION_REQUEST / CATCH_REQUEST by exact sizeof, READY gate inside)",
          c_raw.index("===== X1 END") < x3b < x3e < c_raw.index("===== D3 CLIENT BEGIN") and gr != ""
          and "size == sizeof(PCNetGameFieldActionRequestMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_FIELD_ACTION_REQUEST" in c
          and "size == sizeof(PCNetGameCatchRequestMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_CATCH_REQUEST" in c)
    check("H grant core order: READY gate -> ignore_commit fault -> binding gate (idx only from pcnetgame_rec_gate) -> SYNCED / world ready -> shape -> journal (fence, replay, conflict) -> "
          "base -> pre-image validation -> slot-free precondition -> READ-ONLY validation (+ item resolution) -> fail_world -> post-image validated -> WORLD COMMIT -> mirror write -> "
          "rev++ / journal APPLIED -> kill fault -> despawn -> TXN_RESULT -> legacy RESULT",
          ok_order(gr, "s_host_peer_link[peer] != PC_NETGAME_LINK_READY", "PC_TXN_FAULT_IGNORE_COMMIT", "pcnetgame_rec_gate(peer, 0, 0)", "PC_NETGAME_RECS_SYNCED", "shape_ok =",
                   "pcnetgame_txn_nonce_fenced(", "pcnetgame_txn_journal_find(", "pcnetgame_rec_refresh_hostfields(", "PC_NETGAME_TXN_REASON_STALE_IMAGE",
                   "pcnetgame_rec_validate_inventory(t->pre_pockets", "PC_NETGAME_TXN_REASON_PRECOND, 1, \"pocket slot is not free\"", "h->validate(", "PC_TXN_FAULT_FAIL_WORLD",
                   "pcnetgame_rec_validate_inventory(post,", "h->commit(", "pcnetgame_rec_txn_write_inventory(idx, post, post_conds, t->pre_wallet)", "slot->rev++;",
                   "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED", "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT", "pcnetgame_commit_catch_despawn(cr->entity_id);",
                   "pcnetgame_txn_send_applied(", "pcnetgame_x3_legacy_result(peer, far, cr, 1, legacy_item)"))
    check("H the catch path order inside the core: pcnetgame_validate_catch (READ-ONLY) < claimed-item check < fail_world < post-image validation < pcwld_remove_by_id (the one mutation) < mirror write",
          ok_order(gr, "pcnetgame_validate_catch(0, peer", "pcnetgame_x3_catch_item(&crec", "PC_TXN_FAULT_FAIL_WORLD", "pcnetgame_rec_validate_inventory(post,", "pcwld_remove_by_id(cr->entity_id)",
                   "pcnetgame_rec_txn_write_inventory(") and gr.count("pcwld_remove_by_id(") == 1)
    check("H the host-local catch and the legacy wrapper keep validate-then-remove together (pcnetgame_validate_and_commit_catch = pcnetgame_validate_catch + pcwld_remove_by_id), and the peer "
          "CATCH handler is a thin forwarder to the transaction (no removal, no per-peer dedup answer of its own)",
          ok_order(func_body(c, "pcnetgame_validate_and_commit_catch"), "pcnetgame_validate_catch(", "pcwld_remove_by_id(entity_id)")
          and "pcwld_remove_by_id" not in func_body(c, "pcnetgame_handle_host_catch_request") and "pcnetgame_x3_catch_request(peer, in)" in func_body(c, "pcnetgame_handle_host_catch_request")
          and c.count("pcwld_remove_by_id(") == 2)
    check("H the FIELD_ACTION handler routes grant-carrying requests to the transaction BEFORE the per-peer dedup replay (a resend must reach the journal, not the dedup cache)",
          ok_order(func_body(c, "pcnetgame_handle_host_field_action_request"), "pcnetgame_x3_field_action_request(peer, in)", "s_host_field_action_dedup[peer]"))
    sh = gr[gr.index("shape_ok ="):gr.index("if (!shape_ok)")]
    check("H shape: kinds 4..7 only, flags / aux / _rsv0 all 0, nonce and seq non-zero; DIG_BURIED dest POCKET + item 0 (the host resolves it); DIG_HOLE only ITM_MONEY_100; DIG_SHINE only the three bell "
          "items; CATCH dest POCKET + a non-zero item OR dest NONE + slot 0xFF + item 0; a malformed grant counts a violation",
          all(s in sh for s in ("t->flags == 0", "t->aux_cond == 0", "t->aux_item == 0", "t->txn_nonce != 0", "t->txn_seq != 0", "t->_rsv0 == 0", "t->item == 0;",
                                "t->item == (uint16_t)ITM_MONEY_100", "ITM_MONEY_1000", "ITM_MONEY_10000", "ITM_MONEY_30000",
                                "t->dest == (uint8_t)PC_NETGAME_TXN_DEST_NONE && t->slot == (uint8_t)PC_NETGAME_TXN_SLOT_WALLET && t->item == 0"))
          and "pcnetgame_rec_violation(peer, \"malformed grant request (BAD_SHAPE)\")" in gr)
    returns = [m.start() for m in re.finditer(r"\breturn;", gr)]
    bad = []
    for i in returns:
        ctx = gr[max(0, i - 520):i]
        if not any(s in ctx for s in ("pcnetgame_x3_reject(", "pcnetgame_x3_legacy_result(", "pcnetgame_txn_send_result(", "TEST-ONLY", "pcnetgame_rec_start_push(")) \
                and not gr[:i].rstrip().endswith("}") and "s_host_peer_link[peer] != PC_NETGAME_LINK_READY" not in gr[max(0, i - 160):i]:
            bad.append(i)
    check("H every return after the READY gate answers (x3_reject / legacy result / TXN_RESULT) or is an injected-fault branch (%d returns, unexplained: %d)" % (len(returns), len(bad)), returns and not bad)
    check("H the item is resolved ONCE and never by an mPr_ helper or the RNG on the mirror: DIG_BURIED takes the host validator's resolved item (which already ran pcnetgame_resolve_pickup_item), "
          "DIG_HOLE / DIG_SHINE a bounded client roll, CATCH the host-derived item; the X3 block has no mPr_Set* / mPr_Give* / RANDOM / fqrand / rand call and only the pure condition macro",
          "grant_item = (uint16_t)fa_item;" in gr and not re.search(r"\bmPr_(?:Set\w*|Give\w*|Clear\w*|Dummy\w*|Get\w*)\s*\(", x3) and not re.search(r"\b(?:RANDOM\w*|fqrand\w*|rand|qrand)\s*\(", x3)
          and "mPr_SET_ITEM_COND" in x3 and "pcnetgame_resolve_pickup_item(" in func_body(c, "pcnetgame_validate_and_resolve_dig"))
    check("H the mirror is written ONLY through pcnetgame_rec_txn_write_inventory (raw values, after the world commit) and the X3 block has no direct private_data / Now_Private write",
          not re.search(r"Save_Get\(private_data\)\[[^\]]*\]\s*(?:\.\w+(?:\[[^\]]*\])?)+\s*=[^=]", x3) and "Now_Private" not in x3 and "private_data" not in x3.replace("pcnetgame_rec_txn_write_inventory", ""))
    check("H a grant request is answered by TXN_RESULT first (kinds 4..7 echo the resolved item), then the informational legacy RESULT; a catch's WILDLIFE_DESPAWN goes out before both; replays echo the "
          "JOURNALLED item (DIG_BURIED's request carries 0)",
          "rp.tag.item = old->item;" in gr and "pcnetgame_txn_send_applied(peer, idx, &rp, slot, (uint8_t)PC_NETGAME_TXN_REASON_REPLAYED" in gr
          and "in->tag.item = grant_item;" in gr and gr.index("pcnetgame_commit_catch_despawn(cr->entity_id);\n    }\n    pcnetgame_txn_send_applied(") > 0)
    check("H a catch with full pockets (dest NONE) is still accepted, the entity removed once, and the host-DERIVED item becomes the connection's one-shot exchange credit; any other catch clears it",
          "st->exch_credit_valid = (uint8_t)(t->dest == (uint8_t)PC_NETGAME_TXN_DEST_NONE);" in gr and "st->exch_credit_item = st->exch_credit_valid ? expect : 0;" in gr
          and "uint8_t              exch_credit_valid;" in c_raw and "uint16_t             exch_credit_item;" in c_raw)
    xh = func_body(c, "pcnetgame_handle_host_txn_commit")
    check("H the exchange-menu DROP (TXN_COMMIT kind DROP + flags EXCHANGE): shape needs kind DROP, aux_item != 0, aux_cond < 4; the replacement must equal the credit AND be NORMAL (PRECOND otherwise, the "
          "credit survives); the post-image puts aux_item into the dropped slot; the credit is consumed only by the APPLIED exchange; the journal hash covers the aux bytes (whole 72-byte message)",
          "t->flags == (uint8_t)PC_NETGAME_TXN_FLAG_EXCHANGE && in->kind == (uint8_t)PC_NETGAME_INTERACT_KIND_DROP" in xh and "t->aux_item != 0 && t->aux_cond < 4" in xh
          and "!st->exch_credit_valid || st->exch_credit_item != t->aux_item" in xh and "t->aux_cond != (uint8_t)mPr_ITEM_COND_NORMAL" in xh
          and "post[t->slot] = t->aux_item;" in xh and ok_order(xh, "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED", "st->exch_credit_valid = 0;")
          and "hash = pcnetgame_fnv1a32(in, sizeof(*in));" in xh)
    check("H the credit lives in the per-PEER state that pcnetgame_reset_all_host_peer_state memsets (a dead / reused connection never keeps a credit)",
          "memset(&s_host_peer[peer], 0, sizeof(s_host_peer[peer]));" in func_body(c, "pcnetgame_reset_all_host_peer_state") and "exch_credit" not in func_body(c, "pcnetgame_rec_on_town_changed"))
    pfn = func_body(c, "pcnetgame_x3_field_action_request")
    check("H FIELD_ACTION routing: an all-zero tag on every kind except DIG_BURIED is the legacy no-grant path (return 0, unchanged behaviour); DIG_BURIED without a tag, and a non-zero tag on any "
          "non-grant kind, fall into the grant core where the shape check rejects them (tkind 0)",
          "if (zero && far->kind != (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_BURIED) {\n        return 0;" in pfn and "uint8_t tkind = 0;" in pfn and "hash = pcnetgame_fnv1a32(far, sizeof(*far));" in pfn)
    fl = [m.group(1) for m in re.finditer(r"pcnetgame_txn_fault_fire\((PC_TXN_FAULT_\w+)\)", gr)]
    check("H fault hooks reused for the grants: ignore_commit / fail_world / kill_peer_after_commit (drop_result lives in the shared RESULT sender; expire needs a reservation, which a one-phase grant "
          "does not have -- the exchange DROP's expire path is the X1 handler's)", sorted(fl) == ["PC_TXN_FAULT_FAIL_WORLD", "PC_TXN_FAULT_IGNORE_COMMIT", "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT"]
          and "PC_TXN_FAULT_DROP_RESULT" in func_body(c, "pcnetgame_txn_send_result"))

    # ------------------------------------------------------------------ T: duplicated tables
    uki = read("src/actor/ac_uki_move.c_inc")
    uki_tbl = re.search(r"static mActor_name_t fish_data\[\] = \{(.*?)\};", uki, re.S).group(1)
    vanilla = re.findall(r"\bITM_\w+", uki_tbl)
    mine = re.findall(r"\bITM_\w+", re.search(r"static const mActor_name_t fish_data\[\] = \{(.*?)\};", func_body(x3, "pcnetgame_x3_fish_item"), re.S).group(1))
    check("T the host's duplicated fish table (%d entries) is IDENTICAL to aUKI_get_fish_type()'s fish_data[] in src/actor/ac_uki_move.c_inc, entry by entry" % len(mine),
          len(vanilla) == len(mine) == 45 and vanilla == mine)
    lib_tbl = [L.fish_item_for_species(i) for i in range(45)]
    check("T the python mirror L.fish_item_for_species == the table (ITM_FISH00 = 0x2300 + n; whale -> FISH39; 0x2500+14..16 trash; SALMON2 -> FISH22)",
          lib_tbl[0] == 0x2300 and lib_tbl[39] == 0x2300 + 39 and lib_tbl[40] == 0x2300 + 39 and lib_tbl[41:44] == [0x250E, 0x250F, 0x2510] and lib_tbl[44] == 0x2300 + 22
          and [i for i, n in enumerate(vanilla) if n == "ITM_FISH22"] == [22, 44])
    enum = read("include/ac_insect_h.h")
    types = re.findall(r"aINS_INSECT_TYPE_(\w+)", enum[:enum.index("aINS_INSECT_TYPE_NUM")])
    idx = {n: i for i, n in enumerate(types)}
    pairs, singles = [], []
    for fn in sorted(os.listdir(os.path.join(ROOT, "src", "actor"))):
        if fn.startswith("ac_ins_") and fn.endswith(".c"):
            t = strip_comments(read("src/actor/" + fn))
            paired = set()
            for m in re.finditer(r"case aINS_INSECT_TYPE_(\w+):\s*(?:insect|ins)->item = ITM_INSECT(\d+);", t):
                pairs.append((fn, m.group(1), int(m.group(2))))
                paired.add(int(m.group(2)))
            for m in re.finditer(r"(?:insect|ins)->item = ITM_INSECT(\d+);", t):
                if int(m.group(1)) not in paired:
                    singles.append((fn, int(m.group(1)), fn[len("ac_ins_"):-2].upper()))
    mism = [p_ for p_ in pairs if idx.get(p_[1]) != p_[2]]
    # a single-type actor file is bound to its species by the type -> program table of ac_insect_data.c_inc (index = species)
    ptab = re.findall(r"aINS_PROGRAM_(\w+),", read("src/actor/ac_insect_data.c_inc")[read("src/actor/ac_insect_data.c_inc").index("static int aINS_program_type[]"):])
    bad_single = [s_ for s_ in singles if [i for i, x in enumerate(ptab) if x == s_[2]] != [s_[1]]]
    covered = sorted(set(p_[2] for p_ in pairs) | set(s_[1] for s_ in singles))
    check("T bug item == ITM_INSECT00 + species for ALL 40 species: every case-form assignment (%d) matches the enum index of its case label (mismatches %s) and every single-type actor file "
          "(%d: %s) references exactly the one type whose enum index equals its item number (bad %s); ITM_INSECT00..39 are all covered (%d)"
          % (len(pairs), mism, len(singles), [s_[0] for s_ in singles], bad_single, len(covered)),
          not mism and not bad_single and covered == list(range(40)) and "ITM_SPIRIT0" in read("src/actor/ac_ins_hitodama.c")
          and "(mActor_name_t)(ITM_INSECT00 + rec->species)" in x3 and "(mActor_name_t)ITM_SPIRIT0" in x3)

    # ------------------------------------------------------------------ C: client write sites
    hf = func_body(c, "pcnetgame_handle_client_field_action_result")
    hc = func_body(c, "pcnetgame_handle_client_catch_result")
    hp, hdr, hb = (func_body(c, n) for n in ("pcnetgame_handle_client_pickup_result", "pcnetgame_handle_client_drop_result", "pcnetgame_handle_client_bury_result"))
    writes = lambda x: re.search(r"\bmPr_(?:Set|Give)\w*\s*\(", x) or re.search(r"inventory\.\w+(?:\[[^\]]*\])?\s*[-+|&]?=[^=]", x) or re.search(r"\bmSM_COLLECT\w*\s*\(", x) \
        or "Player_actor_putin_item" in x
    check("C NO result handler (FIELD_ACTION_RESULT, CATCH_RESULT, PICKUP / DROP / BURY RESULT) writes a pocket, the wallet, a collection bit or calls putin_item: every client grant happens in "
          "pcnetgame_txn_apply_applied / pcnetgame_handle_client_txn_result on TXN_RESULT(APPLIED)",
          all(x != "" for x in (hf, hc, hp, hdr, hb)) and not any(writes(x) for x in (hf, hc, hp, hdr, hb)))
    check("C the FIELD_ACTION result handler lost its three grants (DIG_BURIED, DIG_HOLE bonus, DIG_SHINE bell) and the field-action queue entry lost local_grant",
          "saved.local_grant" not in c and "local_grant" not in func_body(c, "pcnetgame_send_field_action_request_ex_grant").replace("uint16_t local_grant", "").replace("local_grant != 0", "").replace("local_grant, ut_x", "")
          and "typedef struct PCNetGameFieldActionPending" in c_raw and "uint16_t local_grant;" not in c_raw[c_raw.index("typedef struct PCNetGameFieldActionPending"):c_raw.index("} PCNetGameFieldActionPending;")])
    cap = func_body(c, "pcnetgame_txn_apply_applied")
    others = ("pcnetgame_txn_try_send", "pcnetgame_txn_begin", "pcnetgame_txn_begin_grant", "pcnetgame_txn_tick", "pcnetgame_handle_client_txn_result", "pcnetgame_txn_fill_tag",
              "pcnetgame_txn_cancel_queued", "pcnetgame_txn_catch_outcome", "pcnetgame_run_txn_test_hook", "pcnetgame_run_txn_dig_test_hook", "pc_net_game_client_pocket_locked",
              "pcnetgame_request_catch_common", "pcnetgame_send_field_action_request_ex_grant", "pc_net_game_request_dig_buried", "pc_net_game_request_dig_hole_with_grant",
              "pc_net_game_request_dig_shine_with_grant")
    check("C the pocket / wallet writer of the grant kinds is pcnetgame_txn_apply_applied ONLY: none of the request / begin / send / tick / result / hook functions assigns inventory.pockets / "
          "item_conditions / wallet",
          len(re.findall(r"inventory\.pockets\[[^\]]*\]\s*=[^=]", cap)) >= 3
          and all(re.search(r"inventory\.(?:pockets\[[^\]]*\]|item_conditions|wallet)\s*(?:\+)?=[^=]", func_body(c, n)) is None and func_body(c, n) != "" for n in others))
    gp = [m.start() for m in re.finditer(r"mPr_SetFreePossessionItem\(", c)]
    owners = []
    for p in gp:
        fnm = re.findall(r"^(?:static )?[\w ]+?[ \*]+(\w+)\([^{;]*\)\s*\{\n", c[:p], re.M)
        owners.append(fnm[-1] if fnm else "?")
    check("C mPr_SetFreePossessionItem survives ONLY in the two HOST-role test triggers that give the host its own caught item (%s); the three client grants are gone" % owners,
          sorted(owners) == ["pcnetgame_run_bug_catch_test_trigger_host", "pcnetgame_run_fish_catch_test_trigger_host"])
    sp = [m.start() for m in re.finditer(r"mPr_SetPossessionItem\(", c)]
    owners2 = []
    for p in sp:
        fnm = re.findall(r"^(?:static )?[\w ]+?[ \*]+(\w+)\([^{;]*\)\s*\{\n", c[:p], re.M)
        owners2.append(fnm[-1] if fnm else "?")
    check("C mPr_SetPossessionItem survives ONLY in pc_net_game_exchange_request_drop (it puts the HAND item back into its slot: the roll-back of the vanilla UI swap, net zero) and the HOST-local bury (the "
          "host's own pockets): %s" % sorted(owners2), sorted(owners2) == ["pc_net_game_exchange_request_drop", "pc_net_game_host_local_bury"])
    ex = func_body(c, "pc_net_game_exchange_request_drop")
    check("C the exchange replacement is no longer applied from a deferred client record: s_exchange_deferred is consumed by the drop result handler into the transaction (have_deferred ? &deferred), "
          "try_send puts it IN the tag (flags EXCHANGE, aux_item, aux_cond; only when the swap slot and owner stamp still match), and the apply step takes it from the host post-image",
          "have_deferred ? &deferred : NULL" in hdr and "s_exchange_deferred" not in cap and "s_exchange_deferred" not in func_body(c, "pcnetgame_txn_try_send")
          and "flags = (uint8_t)PC_NETGAME_TXN_FLAG_EXCHANGE;" in func_body(c, "pcnetgame_txn_try_send") and "s_exchange_deferred.valid = 1;" in ex)

    # ------------------------------------------------------------------ K: client plumbing
    ts = func_body(c, "pcnetgame_txn_try_send")
    tk = func_body(c, "pcnetgame_txn_tick")
    rq = func_body(c, "pcnetgame_send_field_action_request_ex_grant")
    check("K a grant never uses the field-action queue: ex_grant diverts DIG_BURIED / any bonus to pcnetgame_txn_begin_grant BEFORE touching s_field_action_queue, one pocket transaction at a time",
          ok_order(rq, "kind == (uint8_t)PC_NETGAME_FIELD_ACTION_KIND_DIG_BURIED", "pcnetgame_txn_begin_blocked()", "pcnetgame_txn_begin_grant(", "s_field_action_queue_len >= PC_NETGAME_FIELD_ACTION_QUEUE_DEPTH")
          and "pcnetgame_txn_begin_blocked()" in func_body(c, "pc_net_game_request_dig_buried") and "return pcnetgame_txn_begin_blocked();" in func_body(c, "pcnetgame_field_action_grant_already_pending"))  # mail milestone 1 review: the request-side begin check is pcnetgame_txn_begin_blocked() (s_ctxn busy OR a MAIL_SEND in AWAIT_CLEAN, a superset of pcnetgame_txn_busy())
    check("K the catch request: refused while a transaction is unresolved (return 0 = the seams' local denial), the old direct fire-and-forget pc_net_send is gone (the request is built in try_send only), "
          "the pending record keeps the outcome PENDING until the TXN_RESULT",
          "pcnetgame_txn_begin_blocked()" in func_body(c, "pcnetgame_request_catch_common") and "pc_net_send(" not in func_body(c, "pcnetgame_request_catch_common")
          and "pcnetgame_txn_begin_grant((uint8_t)PC_NETGAME_TXN_KIND_CATCH" in func_body(c, "pcnetgame_request_catch_common"))
    check("K try_send: the request is built ONCE with the pre-image of that moment (COMMIT / FIELD_ACTION_REQUEST / CATCH_REQUEST), the slot of a grant is the first FREE slot at send time (dest POCKET) or, "
          "for a catch with full pockets, dest NONE / slot 0xFF / item 0; its bytes are stored and the tick resends the IDENTICAL stored bytes; the tag is filled from Now_Private and the D3 base only",
          all(s in ts for s in ("PC_NETGAME_TXN_KIND_DIG_BURIED:", "PC_NETGAME_TXN_KIND_CATCH:", "pcnetgame_txn_fill_tag(&tag,", "memcpy(T->wire, wire, wlen);"))
          and "pc_net_send(0, PC_NET_RELIABLE, s_ctxn.wire, s_ctxn.wire_len)" in tk and ts.count("pc_net_send(") == 1 and tk.count("pc_net_send(") == 1
          and "tag->base_epoch = s_crec.base_epoch;" in func_body(c, "pcnetgame_txn_fill_tag") and "s_client_wildlife_known_generation" in ts)
    rh = func_body(c, "pcnetgame_handle_client_txn_result")
    check("K the catch outcome (exchange gate), the pending-record resolution and the collection bit (mSM_COLLECT_FISH_SET / _INSECT_SET) are produced ONLY by the TXN_RESULT handler, after the apply step "
          "and only for the same player; a REJECTED catch records REJECTED; the legacy CATCH_RESULT handler is informational",
          ok_order(rh, "pcnetgame_txn_apply_applied(&T, in)", "pcnetgame_txn_catch_outcome(&T, mine)", "mSM_COLLECT_FISH_SET(", "mSM_COLLECT_INSECT_SET(")
          and "pcnetgame_txn_catch_outcome(&T, 0)" in rh and "s_catch_last_outcome" not in hc and "s_catch_pending" not in hc.replace("(s_catch_pending.valid && s_catch_pending.request_id == in->request_id)", ""))
    check("K DIG_BURIED's echo is the host-resolved item (the request carries 0): the result handler accepts any non-empty item for kind 4 and requires an exact echo for every other kind; apply uses "
          "in->item as the granted item for kind 4 only",
          "(T.kind == (uint8_t)PC_NETGAME_TXN_KIND_DIG_BURIED ? in->item == (uint16_t)EMPTY_NO : in->item != T.tag.item)" in rh
          and "(T->kind == (uint8_t)PC_NETGAME_TXN_KIND_DIG_BURIED) ? in->item : t->item" in cap)
    check("K a full-pockets catch (dest NONE) changes no inventory on APPLIED (the host kept the pre-image and holds the credit) but still moves the D3 base and resolves the outcome ACCEPTED",
          "T->kind == (uint8_t)PC_NETGAME_TXN_KIND_CATCH && !to_pocket" in cap and "pcnetgame_crec_set_base(in->epoch, in->rev, in->host_session, in->cdig);" in cap)
    check("K the locks of X1b cover the grants without change: pcnetgame_txn_busy() (state != FREE) is what the inventory predicate, the upload deferral and the adopt blocker read, and every grant "
          "(QUEUED or SENT) holds s_ctxn", "return s_ctxn.state != PC_NETGAME_CTXN_FREE;" in func_body(c, "pcnetgame_txn_busy")
          and "pcnetgame_txn_busy()" in func_body(c, "pcnetgame_crec_upload_deferred") and "pcnetgame_txn_busy()" in func_body(c, "pcnetgame_crec_adopt_blocker"))

    # ------------------------------------------------------------------ P: hooks and migrated tests
    hook = func_body(c, "pcnetgame_run_txn_dig_test_hook")
    check("P --txn-test-dig-grant: default OFF (int g_pc_txn_test_dig_grant = 0), an exact flag with a loud [TEST-ONLY] arm line, documented in --help and pc_platform.h, no getenv, CLIENT-gated in the C "
          "hook, every step logs [TEST-ONLY], it only calls the real request functions, and it checks that no pocket changed before APPLIED",
          "int g_pc_txn_test_dig_grant = 0;" in main_c and 'strcmp(argv[i], "--txn-test-dig-grant") == 0' in main_c and "[NET][TXN][TEST-ONLY] --txn-test-dig-grant armed" in main_c
          and "--txn-test-dig-grant  Client-only TEST hook" in main_c and "extern int           g_pc_txn_test_dig_grant;" in plat_h and "--txn-test-dig-grant. CLIENT only" in plat_h
          and "getenv" not in hook and "s_role != PC_NETGAME_ROLE_CLIENT" in hook and hook.count("[NET][TXN][TEST-ONLY]") >= 3 and "[NET][TXN][TEST-ONLY]" in func_body(c, "pcnetgame_txn_test_dump_pockets")
          and all(f in hook for f in ("pc_net_game_request_dig_buried(", "pc_net_game_request_dig_hole_with_grant(", "pc_net_game_request_dig_shine_with_grant(", "POCKET CHANGED BEFORE APPLIED"))
          and c.count("pcnetgame_run_txn_dig_test_hook();") == 1)
    check("P the existing --force-fish-catch / --force-bug-catch CLIENT triggers now wait for the record to be SYNCED (a grant is refused before it; test-only fix, default-off hooks)",
          "pcnetgame_client_record_synced()" in func_body(c, "pcnetgame_run_fish_catch_test_trigger_client") and "pcnetgame_client_record_synced()" in func_body(c, "pcnetgame_run_bug_catch_test_trigger_client"))
    mig = ["test_bury_sync.py", "test_dig_family_sync.py", "test_field_action_sync.py", "test_field_action_wire.py", "test_identity_validation.py", "test_money_rock_late_join.py",
           "test_money_rock_review_gaps.py", "test_pitfall_consume_reject_reconcile.py", "test_snowman_sync.py", "test_tree_review_gaps.py"]
    txt = {f: open(os.path.join(HERE, f), "rb").read().decode("utf-8", "replace") for f in mig}
    check("P every test that builds a FIELD_ACTION_REQUEST by hand now sends the 76-byte layout (the 64-byte tag padded in its REQ_FMT: all zero = no grant) -- %d files" % len(mig),
          all("<BBBBIBBH64x" in t for t in txt.values()))
    check("P the DIG_BURIED users send a REAL grant tag through FakeClient.send_fa_grant (bury_sync's dig-up, dig_family B1, field_action D1/D2)",
          "send_fa_grant(KIND_DIG_BURIED" in txt["test_bury_sync.py"]
          and "send_fa_grant(kind, ut_x, ut_z, request_id, hole_variant=hole_variant)" in txt["test_dig_family_sync.py"] and "send_fa_grant(kind, ut_x, ut_z, request_id)" in txt["test_field_action_sync.py"])
    wl = {f: open(os.path.join(HERE, f), "rb").read().decode("utf-8", "replace") for f in ("test_wildlife_catch.py", "test_wildlife_bug_catch.py", "test_wildlife_catch_exchange_gate.py")}
    check("P the three wildlife catch tests send TAGGED catch requests (send_catch / catch_bytes via FakeClient.catch_request_bytes); no raw build_catch_request().send_reliable remains",
          all("catch_request_bytes(" in t and not re.search(r"send_reliable\(build_catch_request\(", t) for t in wl.values()))
    lib = open(os.path.join(HERE, "net_spike_lib.py"), encoding="utf-8").read()
    check("P FakeClient: send_fa_grant / send_catch_txn / catch_request_bytes / build_grant_tag exist; an APPLIED grant result patches the local image like the real client's apply step (the same generic _txn_on_result)",
          all(f in lib for f in ("def send_fa_grant(", "def send_catch_txn(", "def catch_request_bytes(", "def build_grant_tag(")) and "def _txn_on_result(self, m, g):" in lib)

    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
