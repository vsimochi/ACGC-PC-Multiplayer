#!/usr/bin/env python3
"""test_ts_src.py - SOURCE AUDIT of Town Services milestone 1: the generic host -> client service mirror (TOWN_SVC_STATE, id 55) and the
host-authoritative MUSEUM_DONATE (TXN kind 8) / POLICE_CLAIM (kind 9) transactions, plus the Blathers / Booker dialogue seams.

Pure text checks over pc/src/pc_net_game.c (the TS HOST and TS CLIENT blocks, the X1b result handler, the dispatchers),
src/actor/npc/ac_npc_curator_move.c_inc, src/actor/npc/ac_npc_police2_move.c_inc, pc/src/pc_main.c, pc/include/pc_platform.h,
pc/include/pc_net_game.h and the Python library. No game process. Tier: SOURCE AUDITED.

  W  wire: ids 53/54 reserved + 55, the 352-byte message with exact _Static_asserts <= PC_NET_MAX_PAYLOAD, kinds 8/9, reasons 15..18,
     svc_seq16, python == C, wire_baseline green
  M  mirror: host table + digest poll + per-peer sent seq (no spam), client validation order, per-session seq memory, only mirrors written
     into Save_t, shop/reserved refused, a client-originated TOWN_SVC_STATE is dropped by the host
  H  host handler: gate order, the resident index only from the gate, ONE mutation site per service, vanilla functions only, no RNG
  C  client: no pocket write before APPLIED anywhere in the curator / police seams or the TS client block, the wait states, the hooks
  T  test hooks default off / loud / documented / role-gated
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


def strip_hook_bodies(text):
    """`text` without the bodies of the TEST-ONLY hook functions (the shop hook since milestone 2) (they read / write pockets by design)."""
    for name in ("pcnetgame_ts_test_log_state", "pcnetgame_run_ts_test_hook", "pcnetgame_run_shop_test_hook"):
        body = func_body(text, name)
        if body:
            text = text.replace(body, "")
    return text


def in_order(body, items):
    pos = [body.find(x) for x in items]
    return all(p >= 0 for p in pos) and pos == sorted(pos), [x for x, p in zip(items, pos) if p < 0]


def main():
    results = []
    check = lambda d, c: L.check(d, bool(c), results)
    c_raw = read("pc/src/pc_net_game.c")
    c = strip_comments(c_raw)
    h = read("pc/include/pc_net_game.h")
    main_c = read("pc/src/pc_main.c")
    plat_h = read("pc/include/pc_platform.h")
    cur = read("src/actor/npc/ac_npc_curator_move.c_inc")
    pol = read("src/actor/npc/ac_npc_police2_move.c_inc")
    hb, he = c_raw.index("===== TS HOST BEGIN"), c_raw.index("===== TS HOST END")
    cb, ce = c_raw.index("===== TS CLIENT BEGIN"), c_raw.index("===== TS CLIENT END")
    hblk_raw, cblk_raw = c_raw[hb:he], c_raw[cb:ce]
    hblk, cblk = strip_comments(hblk_raw), strip_comments(cblk_raw)
    ts_raw = func_body(hblk_raw, "pcnetgame_handle_host_ts_txn")
    ts = strip_comments(ts_raw)

    # ------------------------------------------------------------------ W: wire
    ids = dict(wire_baseline.c_message_ids(c_raw))
    check("W ids: 53 / 54 are ENUMERATED reserved ids (X2), 55 = TOWN_SVC_STATE, 56 = MAILBOX_LETTER (mail milestone 2), all ids contiguous 1..%d" % wire_baseline.EXPECTED_MAX_MSG_ID,
          ids.get("PC_NETGAME_MSG_TXN_RESERVED_53") == 53 and ids.get("PC_NETGAME_MSG_TXN_RESERVED_54") == 54 and ids.get("PC_NETGAME_MSG_TOWN_SVC_STATE") == 55
          and ids.get("PC_NETGAME_MSG_MAILBOX_LETTER") == 56 and ids.get("PC_NETGAME_MSG_IDENTITY_EXT") == 57 and ids.get("PC_NETGAME_MSG_IDENTITY_TOKEN") == 58
          and sorted(ids.values()) == list(range(1, wire_baseline.EXPECTED_MAX_MSG_ID + 1)) and wire_baseline.EXPECTED_MAX_MSG_ID == 61)
    check("W PCNetGameTownSvcStateMsg: u8 type, u8 service, u16 len, u32 seq, u32 digest, blob[PC_NETGAME_TS_BLOB_MAX]; sizeof == 352 (12 + 340), offsets 4 / 8 / 12, "
          "<= PC_NET_MAX_PAYLOAD, blobs fit (40, 63, the 320-byte shop)",
          "_Static_assert(sizeof(PCNetGameTownSvcStateMsg) == 352," in c_raw and "offsetof(PCNetGameTownSvcStateMsg, blob) == 12" in c_raw
          and "_Static_assert(sizeof(PCNetGameTownSvcStateMsg) <= PC_NET_MAX_PAYLOAD," in c_raw and "320u <= PC_NETGAME_TS_BLOB_MAX" in c_raw
          and "#define PC_NETGAME_TS_BLOB_MAX 340u" in c_raw and "PC_NETGAME_TS_POLICE_LEN == sizeof(PoliceBox_c)" in c_raw
          and "PC_NETGAME_TS_MUSEUM_LEN == sizeof(mMmd_info_c)" in c_raw)
    check("W kinds MUSEUM_DONATE = 8 / POLICE_CLAIM = 9, reasons ALREADY_DONATED 15 / NOT_AVAILABLE 16 / NOT_DONATABLE 17 / NO_DONOR_SLOT 18, services 1 / 2 / 3 (shop reserved)",
          all(re.search(r"#define %s\s+%du" % (n, v), c_raw) for n, v in (
              ("PC_NETGAME_TXN_KIND_MUSEUM_DONATE", 8), ("PC_NETGAME_TXN_KIND_POLICE_CLAIM", 9), ("PC_NETGAME_TXN_REASON_ALREADY_DONATED", 15),
              ("PC_NETGAME_TXN_REASON_NOT_AVAILABLE", 16), ("PC_NETGAME_TXN_REASON_NOT_DONATABLE", 17), ("PC_NETGAME_TXN_REASON_NO_DONOR_SLOT", 18),
              ("PC_NETGAME_TS_POLICE", 1), ("PC_NETGAME_TS_MUSEUM", 2), ("PC_NETGAME_TS_SHOP", 3))))
    check("W TXN_RESULT carries svc_seq16 (the former reserved u16); python formats / constants / reason names equal the C values",
          all([re.search(r"uint16_t post_pockets\[15\];\s*uint16_t svc_seq16;", strip_comments(c_raw)) is not None,
               L.TXN_RESULT_SPEC.tuple_cls._fields[-3] == "svc_seq16", L.TOWN_SVC_STATE_FMT == "<BBHII", L.TOWN_SVC_STATE_SPEC.size == 12,
               L.GAME_SPECS[55] is L.TOWN_SVC_STATE_SPEC, (L.PC_NETGAME_TXN_KIND_MUSEUM_DONATE, L.PC_NETGAME_TXN_KIND_POLICE_CLAIM) == (8, 9),
               [L.TXN_REASON_NAMES[i] for i in (15, 16, 17, 18)] == ["ALREADY_DONATED", "NOT_AVAILABLE", "NOT_DONATABLE", "NO_DONOR_SLOT"]]))
    wire_baseline.run(lambda desc, cond: check("W " + desc, cond), ROOT)

    # ------------------------------------------------------------------ M: mirror
    check("M the TS HOST block sits after the X3 host block and before the D3 client block; the TS CLIENT block after the X1b client block",
          c_raw.index("===== X3 HOST END") < hb < he < c_raw.index("===== D3 CLIENT BEGIN") and c_raw.index("===== X1b CLIENT END") < cb < ce)
    build = func_body(hblk_raw, "pcnetgame_ts_build")
    check("M host table: one slot per service (blob, digest, seq); police = 20 little-endian u16 of keep_items (40 B), museum = the 63 raw bytes of Save_Get(museum_display)",
          "static PCNetGameTsHost s_ts_host[PC_NETGAME_TS_NUM];" in hblk and "keep_items[i]" in build and "blob[2 * i] = (uint8_t)(v & 0xFFu);" in build
          and "blob[2 * i + 1] = (uint8_t)(v >> 8);" in build and "memcpy(blob, &Save_Get(museum_display), PC_NETGAME_TS_MUSEUM_LEN)" in build
          and "memcpy(blob, &Save_Get(shop), PC_NETGAME_TS_SHOP_LEN)" in build)  # milestone 2: service 3 = the 320 raw bytes of Shop_c
    refresh = func_body(hblk_raw, "pcnetgame_ts_refresh")
    check("M change detection is a DIGEST compare (FNV-1a32 of the blob): the seq is bumped only when len / digest differ, never otherwise (strictly increasing, never reset)",
          "dig = pcnetgame_fnv1a32(blob, len);" in refresh and "if (h->valid && h->len == len && h->digest == dig) {\n        return 0;" in refresh
          and "h->seq++;" in refresh and "h->seq = " not in refresh and "memset(s_ts_host" not in c and "s_ts_host[" in hblk)
    tick = func_body(hblk_raw, "pcnetgame_host_ts_tick")
    check("M the poll is bounded: a 500 ms digest cadence (PC_NETGAME_TS_CHECK_MS), the per-peer push runs only on a seq difference (ts_sent_seq), host role + world ready only",
          "#define PC_NETGAME_TS_CHECK_MS 500u" in hblk and "(uint32_t)(now - s_ts_next_check_ms) >= PC_NETGAME_TS_CHECK_MS" in tick
          and "s_role != PC_NETGAME_ROLE_HOST || !s_host_world_ready" in tick and "pcnetgame_host_ts_push_peer((PCNetPeerId)p)" in tick)
    push = func_body(hblk_raw, "pcnetgame_host_ts_push_peer")
    check("M push: READY peers only; sends only when ts_sent_seq != the table seq and records the seq only after the send was queued (a failed send is retried next poll)",
          "s_host_peer_link[peer] != PC_NETGAME_LINK_READY" in push and "st->ts_sent_seq[svc] != s_ts_host[svc].seq" in push
          and push.index("pcnetgame_ts_send_to_peer(peer, svc)") < push.index("st->ts_sent_seq[svc] = s_ts_host[svc].seq;")
          and "ts_sent_seq" in c_raw[c_raw.index("} PCNetGameHostPeerState;") - 900:c_raw.index("} PCNetGameHostPeerState;")])
    check("M a late join / reconnect needs no extra hook: reset_all_host_peer_state memsets the peer state (ts_sent_seq = 0), so the next poll pushes every service",
          "memset(&s_host_peer[peer], 0, sizeof(s_host_peer[peer]));" in func_body(c, "pcnetgame_reset_all_host_peer_state"))
    check("M host_ts_tick is called from the host section of pc_net_game_poll (after the record tick)",
          re.search(r"pcnetgame_host_record_tick\(\);[^\n]*\n\s*pcnetgame_host_ts_tick\(\);", c_raw) is not None)
    send = func_body(hblk_raw, "pcnetgame_ts_send_to_peer")
    check("M the message is RELIABLE and sent as offsetof(blob) + len bytes (variable); every pc_net_send of the TS host block is RELIABLE",
          "pc_net_send(peer, PC_NET_RELIABLE, &m, sz)" in send and "offsetof(PCNetGameTownSvcStateMsg, blob) + h->len" in send
          and all("PC_NET_RELIABLE" in s for s in re.findall(r"pc_net_send\(([^;]*)\)", hblk)))
    hd = func_body(c, "pcnetgame_handle_host_data")
    check("M the host DROPS a client-originated TOWN_SVC_STATE (never applied, never relayed): the host never overwrites its own copy from a peer",
          "data[0] == (uint8_t)PC_NETGAME_MSG_TOWN_SVC_STATE" in hd and "pcnetgame_handle_client_town_svc" not in hd and "pcnetgame_ts_client_apply" not in hd
          and "pcnetgame_handle_client_town_svc" in func_body(c, "pcnetgame_handle_client_data"))
    cl = func_body(cblk_raw, "pcnetgame_handle_client_town_svc")
    # batch A (A1): the HOST_CONFIG (service 4) session setting is applied BEFORE the save-ready stash by design (it is not a save region); the order below is that of the SAVE-REGION services
    cl_regions = cl.replace("pcnetgame_ts_client_apply(&m); /* a session setting, not a save region: no usable save needed */", "")
    ok, miss = in_order(cl_regions, ["s_client_link != PC_NETGAME_LINK_READY", "sizeof(m)", "svc != (int)PC_NETGAME_TS_POLICE && svc != (int)PC_NETGAME_TS_MUSEUM",
                             "m.len != expect", "pcnetgame_fnv1a32(m.blob, m.len) != m.digest", "m.seq <= s_ts_client_seq[svc]",
                             "pcnetgame_ts_valid_police_blob(m.blob)", "pcfa_save_ready()", "pcnetgame_ts_client_apply(&m)"])
    check("M the client validates in order: READY link, message size, service (3 SHOP and 4+ are ignored), exact len + size, digest, seq strictly above the last of this session "
          "(stale / duplicate ignored), content, usable local save, THEN applies (missing %s)" % miss, ok)
    check("M stale / duplicate seq is IGNORED (logged), the last accepted seq is kept per service and per SESSION (reset with the session)",
          "is stale / duplicate" in cl and "memset(s_ts_client_seq, 0, sizeof(s_ts_client_seq));" in func_body(c, "pcnetgame_reset_client_session_state")
          and "memset(s_ts_client_have, 0, sizeof(s_ts_client_have));" in func_body(c, "pcnetgame_reset_client_session_state"))
    check("M police content check: only 0 / ITEM1 / FTR items (what mPB_keep_item stores); museum content check: every donor nibble <= 5 (DELETED_PLAYER)",
          "ITEM_IS_ITEM1(v) || ITEM_IS_FTR(v)" in func_body(cblk_raw, "pcnetgame_ts_valid_police_blob") and "mMmd_DONATOR_DELETED_PLAYER" in func_body(cblk_raw, "pcnetgame_ts_valid_museum_blob"))
    ap = func_body(cblk_raw, "pcnetgame_ts_client_apply")
    check("M the client writes the blob into its LOCAL Save_Get(police_box) / Save_Get(museum_display) ONLY in pcnetgame_ts_client_apply; that function is called only by the mirror handler "
          "and the stash tick; no other 'keep_items[..] =' / museum_display write exists on the client side",
          "Save_Get(police_box).keep_items[i] = " in ap and "memcpy(&Save_Get(museum_display), m->blob, PC_NETGAME_TS_MUSEUM_LEN)" in ap
          and c.count("pcnetgame_ts_client_apply(") == 4  # definition + handler + stash tick + the batch A HOST_CONFIG early apply (a session setting, no save needed)
          and len(re.findall(r"keep_items\[[^\]]*\]\s*=[^=]", c)) == 2 and len(re.findall(r"&Save_Get\(museum_display\)", c)) == 2)  # host build() reads it; apply writes it
    check("M a mirror that arrives before the local save is usable is STASHED (one per service) and applied by the client tick; the interiors: the police draw table is refreshed through the "
          "existing mFI_SetFGUpData() when the client stands in SCENE_POLICE_BOX, the museum rooms reflect it at the next entry (documented)",
          "s_ts_client_stash[svc] = m;" in cl and "pcnetgame_ts_client_tick()" in c and "mFI_SetFGUpData();" in ap and "s_local_scene.scene_id == (uint8_t)SCENE_POLICE_BOX" in ap
          and "NEXT ENTRY" in cblk_raw)
    check("M the mirror state of a client never persists: no pc_save / GCI call anywhere in the TS blocks (clients never write a save; the host's normal save persists the service data)",
          not re.search(r"pc_save_|fopen|fwrite|pc_m_card", hblk + cblk))

    # ------------------------------------------------------------------ H: host handler
    check("H ONE host handler pcnetgame_handle_host_ts_txn(peer, msg); it never yields (no sleep / IO / network poll)",
          ts != "" and not re.search(r"\b(SDL_Delay|Sleep|fopen|fwrite|pc_net_poll|usleep)\b", ts))
    order = ["s_host_peer_link[peer] != PC_NETGAME_LINK_READY", "PC_TXN_FAULT_IGNORE_COMMIT", "pcnetgame_rec_gate(peer, 0, 0)", "PC_NETGAME_RECS_SYNCED", "s_host_world_ready",
             "shape_ok = in->_rsv0 == 0", "pcnetgame_txn_nonce_fenced(", "pcnetgame_txn_journal_find(", "R->max_seq = t->txn_seq;", "pcnetgame_rec_refresh_hostfields(idx, slot)",
             "t->base_epoch != slot->epoch", "pcnetgame_rec_validate_inventory(t->pre_pockets", "memcpy(post, t->pre_pockets", "pcnetgame_shop_pay(post",
             "pcnetgame_shop_sell_plan(t->pre_pockets", "mMmd_GetDisplayInfo(", "PC_TXN_FAULT_FAIL_WORLD",
             "pcnetgame_rec_validate_inventory(post, post_conds", "mSP_PlusSales(shop_price);", "mSP_ShopSaleReport(", "mMmd_RequestMuseumDisplay(", "mPB_copy_itemBuf(", "pcnetgame_rec_txn_write_inventory(idx, post", "slot->rev++;",
             "slot->dirty_unsaved = 1;", "R->last_pocket_rev = slot->rev;", "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED", "pcnetgame_ts_refresh(svc);",
             "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT", "pcnetgame_txn_send_applied(peer, idx, in, slot, (uint8_t)PC_NETGAME_TXN_REASON_NONE", "pcnetgame_host_ts_push_all();\n    /* 14.",
             "pcnetgame_rec_start_push(peer, idx, st->rec_push_kind)"]
    ok, miss = in_order(ts_raw, order)
    check("H the algorithm steps appear in order: READY gate, fault, binding gate, SYNCED / world ready, shape, journal (fence / replay / max_seq), base, pre-image, service validation, "
          "fail_world, post-image validated, the service commit, mirror write, rev / dirty / journal, refresh (the echoed seq), kill fault, RESULT, push to everyone, push restart (missing %s)" % miss, ok)
    check("H the resident index comes ONLY from the gate: one 'idx = pcnetgame_rec_gate(peer, 0, 0);', no other assignment, nothing read from the message",
          ts.count("idx = pcnetgame_rec_gate(peer, 0, 0);") == 1 and len(re.findall(r"\bidx\s*=[^=]", ts)) == 1 and not re.search(r"in->\w*(?:idx|resident)\w*", ts))
    check("H MUSEUM_DONATE: the donor is the BOUND resident: player_no is saved, set to idx, the VANILLA mMmd_RequestMuseumDisplay is the only commit, player_no is restored on the very next statement",
          re.search(r"u8 saved_player_no = Common_Get\(player_no\);\s*int ok;\s*Common_Get\(player_no\) = \(u8\)idx;[^\n]*\n\s*ok = mMmd_RequestMuseumDisplay\(\(mActor_name_t\)t->item\);\s*"
                    r"Common_Get\(player_no\) = saved_player_no;", ts) is not None and ts.count("mMmd_RequestMuseumDisplay(") == 1)
    check("H the vanilla predicate gates the donation: mMmd_GetDisplayInfo (CANNOT -> NOT_DONATABLE, ALREADY -> ALREADY_DONATED), the two paintings Blathers returns are refused, and a "
          "resident without a donor slot (idx outside PLAYER_NUM) is refused with NO_DONOR_SLOT",
          "info == mMmd_DISPLAY_CANNOT_DONATE" in ts and "info == mMmd_DISPLAY_ALREADY_DONATED" in ts and "FTR_SUM_ART02" in ts and "FTR_SUM_ART03" in ts
          and "idx < 0 || idx >= PLAYER_NUM" in ts and "PC_NETGAME_TXN_REASON_NO_DONOR_SLOT" in ts)
    check("H POLICE_CLAIM verifies (slot, EXPECTED item) against the host's keep_items, clears the slot and compacts with the vanilla mPB_copy_itemBuf; mismatch -> NOT_AVAILABLE",
          "(uint16_t)Save_Get(police_box).keep_items[pidx] != t->item" in ts and "PC_NETGAME_TXN_REASON_NOT_AVAILABLE" in ts
          and ts.index("Save_Get(police_box).keep_items[pidx] = (mActor_name_t)EMPTY_NO;") < ts.index("mPB_copy_itemBuf(Save_Get(police_box).keep_items);"))
    check("H the TS handler is the ONLY writer of museum_display / police_box on the host: mMmd_RequestMuseumDisplay( exists once in pc_net_game.c (this handler), "
          "mPB_keep_item( only in the host snowman path and the TEST-ONLY seed hook, mPB_copy_itemBuf( once",
          c.count("mMmd_RequestMuseumDisplay(") == 1 and c.count("mPB_copy_itemBuf(") == 1 and len(re.findall(r"\bmPB_keep_item\(\(mActor_name_t\)", c)) == 2
          and "mPB_keep_item(" in func_body(hblk_raw, "pcnetgame_ts_test_seed_police") and "g_pc_ts_test_seed_police" in func_body(hblk_raw, "pcnetgame_ts_test_seed_police"))
    check("H the host's OWN resident is never written: the post-image gate uses pcnetgame_rec_txn_idx_ok(idx) BEFORE the service commit (the host's own players use the vanilla local path)",
          "!pcnetgame_rec_txn_idx_ok(idx)" in ts and ts.index("!pcnetgame_rec_txn_idx_ok(idx)") < ts.index("mMmd_RequestMuseumDisplay("))
    check("H NO RNG and no mPr_ item writer on the mirror path: no RANDOM / rand / qrand / pcnetgame_rec_rand32 / mPr_Set* / mPr_Give* in the TS host block (the pocket is written raw "
          "by pcnetgame_rec_txn_write_inventory with mPr_SET_ITEM_COND values)",
          not re.search(r"\b(RANDOM|rand|qrand|pcnetgame_rec_rand32|fqrand|mPr_Set\w*|mPr_Give\w*|mPr_Clear\w*)\s*\(", hblk) and "pcnetgame_rec_txn_write_inventory(idx, post, post_conds, post_wallet)" in ts)
    mut_tokens = ("mMmd_RequestMuseumDisplay(", "keep_items[pidx] =", "pcnetgame_rec_txn_write_inventory(", "slot->rev++", "pcnetgame_txn_journal_add(R, in, hash, (uint8_t)PC_NETGAME_TXN_OUTCOME_APPLIED")
    rej_ok = []
    for m in re.finditer(r"pcnetgame_txn_reject\(", ts):
        nxt = ts.find("return", m.end())
        seg = ts[m.end():nxt] if nxt >= 0 else ts[m.end():]
        rej_ok.append(nxt >= 0 and not any(t in seg for t in mut_tokens))
    check("H every pcnetgame_txn_reject(..) in the handler reaches a 'return' before ANY mutation token (a REJECTED never falls through into a mutation) and there are >= 12 of them",
          rej_ok and all(rej_ok) and len(rej_ok) >= 12)
    check("H the shape check counts structural garbage as a violation (3 close the peer); semantic refusals never do",
          "pcnetgame_rec_violation(peer, \"malformed town-service TXN_COMMIT (BAD_SHAPE)\")" in ts and ts.count("pcnetgame_rec_violation(") == 1)
    check("H the dispatcher routes kinds 8 / 9 / 10 / 11 (museum, police, shop buy, shop sell) to this handler after the exact-size check; the X1 shape gate still refuses them (kind 1..3 only)",
          re.search(r"if \(tc\.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_MUSEUM_DONATE \|\| tc\.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_POLICE_CLAIM \|\|\s*tc\.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_SHOP_BUY \|\| tc\.kind == \(uint8_t\)PC_NETGAME_TXN_KIND_SHOP_SELL\) \{\s*pcnetgame_handle_host_ts_txn\(peer, &tc\);", hd) is not None
          and "shape_ok = in->kind >= (uint8_t)PC_NETGAME_INTERACT_KIND_PICKUP && in->kind <= (uint8_t)PC_NETGAME_INTERACT_KIND_BURY" in c)
    check("H the shared fault hooks reach the new kinds: ignore_commit, fail_world and kill_peer_after_commit have one site each in the TS handler (drop_result through the shared sender); "
          "the journal is the shared per-resident s_txn_res, never reset per peer",
          sorted(re.findall(r"pcnetgame_txn_fault_fire\((PC_TXN_FAULT_\w+)\)", ts)) == ["PC_TXN_FAULT_FAIL_WORLD", "PC_TXN_FAULT_IGNORE_COMMIT", "PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT"]
          and "s_txn_res[idx]" in ts and "memset(s_txn_res" not in hblk)
    check("H the RESULT echoes the service seq: s_txn_svc_echo is set right before pcnetgame_txn_send_applied (execution and replay) and cleared right after; the shared sender copies it into svc_seq16",
          ts.count("s_txn_svc_echo = (uint16_t)s_ts_host[svc].seq;") == 2 and ts.count("s_txn_svc_echo = 0;") == 2 and "out.svc_seq16 = s_txn_svc_echo;" in c)

    # ------------------------------------------------------------------ C: client
    ow = strip_comments(func_body(cur, "aCR_msg_win_open_wait"))
    check("C curator: NO pocket / museum write in the client donation branch (no mPr_Set*, no mMmd_Request*, no inventory access): the pocket is emptied only by the host post-image (APPLIED apply step)",
          ow != "" and not re.search(r"\b(mPr_Set\w*|mMmd_Request\w*|mMmd_Set\w*)\s*\(|inventory", ow))
    check("C curator: begin -> wait -> APPLIED proceeds / anything else row 2; PENDING returns without any state change; begin 'busy' (-1) returns too; the wait flag resets when a new offer menu opens",
          "pc_net_game_ts_begin_museum_donate(play->submenu.item_p->slot_no, (int)item)" in ow and "ts_begin < 0" in ow and "PC_NETGAME_TS_OP_PENDING" in ow
          and "ts_res != PC_NETGAME_TS_OP_APPLIED" in ow and "aCR_ts_waiting = FALSE;" in strip_comments(func_body(cur, "aCR_menu_close_wait_init")))
    pw = strip_comments(func_body(cur, "aCR_putaway_demo_end_wait_init"))
    check("C curator: the vanilla put-away commit + pocket clear stays unreachable for a client (a client's donation was committed by the host before the put-away rows)",
          "pc_net_game_role() != PC_NETGAME_ROLE_CLIENT" in pw and pw.index("pc_net_game_role() != PC_NETGAME_ROLE_CLIENT") < pw.index("mMmd_RequestMuseumDisplay(sm_item_p->item)") < pw.index("mPr_SetPossessionItem("))
    pa = strip_comments(func_body(pol, "aPOL2_check_answer"))
    client_branch = pa[pa.index("if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {"):pa.index("} else\n#endif") if "} else\n#endif" in pa else pa.index("} else")]
    check("C police: the client branch has NO pocket / lost-and-found write (no mPr_Set*, no keep_items assignment, no inventory access); only the host/vanilla path writes them, behind 'if (!claimed_remote)'",
          not re.search(r"\b(mPr_Set\w*)\s*\(|keep_items\[[^\]]*\]\s*=[^=]|inventory", client_branch) and pa.count("mPr_SetPossessionItem(") == 1 and "if (!claimed_remote)" in pa
          and pa.index("if (!claimed_remote)") < pa.index("mPr_SetPossessionItem("))
    check("C police: begin with (lost-and-found slot, EXPECTED item, free slot) -> wait while PENDING -> claimed_remote only on APPLIED; the expected item / index are remembered across frames "
          "(the mirror may compact keep_items before the answer is polled); the wait flag resets when a new claim starts",
          "pc_net_game_ts_begin_police_claim(free_slot, aPOL2_ts_idx, (int)aPOL2_ts_item)" in pa and "aPOL2_ts_item = *item_p;" not in pa and "PC_NETGAME_TS_OP_PENDING" in pa
          and pa.count("claimed_remote = TRUE;") == 1 and "aPOL2_ts_waiting = FALSE;" in strip_comments(func_body(pol, "aPOL2_message_ctrl")))
    mc = strip_comments(func_body(pol, "aPOL2_message_ctrl"))
    check("C police L1: the (index, item) the player POINTED AT is captured at the A-press (aPOL2_ts_idx / aPOL2_ts_item written there, read only by the claim begin) so a mirror update during the dialogue cannot change which item is claimed",
          "aPOL2_ts_idx = idx;" in mc and "aPOL2_ts_item = item;" in mc and mc.index("actor->item_idx = idx;") < mc.index("aPOL2_ts_idx = idx;")
          and "aPOL2_ts_idx" in pa and "actor->item_idx" not in pa[pa.index("pc_net_game_ts_begin_police_claim("):pa.index("pc_net_game_ts_begin_police_claim(") + 90])
    check("C police M1 (host): the host/solo path refuses (idx = -1) when the slot emptied under the dialogue (never 'here you go' for nothing); the guard is TARGET_PC, skipped for a remote claim, and sits between the vanilla slot query and the refusal",
          "if (!claimed_remote && *item_p == EMPTY_NO) {" in pa and pa.index("idx = mPlib_Get_space_putin_item();\n                }") < pa.index("if (!claimed_remote && *item_p == EMPTY_NO) {") < pa.index("if (idx == -1) {"))
    go = strip_comments(func_body(pol, "aPOL2_player_getout_check"))
    check("C police M2: leaving the police box compacts keep_items only when NOT a client (a client's copy is the host's mirror; compacting it diverged from the host indices)",
          "pc_net_game_role() != PC_NETGAME_ROLE_CLIENT" in go and go.index("pc_net_game_role() != PC_NETGAME_ROLE_CLIENT") < go.index("mPB_copy_itemBuf(Save_Get(police_box).keep_items);"))
    check("H M1: when the HOST player stands in SCENE_POLICE_BOX the claim handler writes EMPTY_NO into the slot but does NOT compact (vanilla compacts at the host's exit) and redraws via mFI_SetFGUpData(); otherwise it compacts at once",
          re.search(r"keep_items\[pidx\] = \(mActor_name_t\)EMPTY_NO;\s*if \(s_local_scene\.valid && s_local_scene\.scene_id == \(uint8_t\)SCENE_POLICE_BOX\) \{\s*mFI_SetFGUpData\(\);\s*\} else \{\s*mPB_copy_itemBuf\(", ts) is not None)
    ap2 = func_body(c, "pcnetgame_txn_apply_applied")
    res_h = func_body(c, "pcnetgame_handle_client_txn_result")
    check("C L2: apply_applied reports whether it applied (int): returns 0 on an owner change / an inconsistent post-image and 1 otherwise; the result handler tells the UI seam APPLIED only then (REJECTED otherwise), txn state is freed before either way",
          "static int pcnetgame_txn_apply_applied(" in c and ap2.count("return 0;") == 2 and ap2.rstrip().endswith("return 1;") and "const int applied_ok = pcnetgame_txn_apply_applied(&T, in);" in res_h
          and "pcnetgame_ts_op_resolve(T.kind, T.request_id, applied_ok);" in res_h and res_h.index("memset(&s_ctxn, 0, sizeof(s_ctxn));") < res_h.index("applied_ok"))
    check("C police: tickets / paper stacking is NOT used for a client (mPlib_Get_space_putin_item_forTICKET only in the vanilla host branch)",
          "mPlib_Get_space_putin_item_forTICKET" not in client_branch and "mPlib_Get_space_putin_item_forTICKET(item_p)" in pa)
    ts_cli = cblk
    check("C the TS client block (outside the TEST-ONLY hook) never touches the inventory: begin / poll / mirror apply contain no 'inventory' access",
          "inventory" not in strip_hook_bodies(cblk))
    ap_body = func_body(c, "pcnetgame_txn_apply_applied")
    check("C the pocket of a MUSEUM_DONATE / POLICE_CLAIM changes ONLY in pcnetgame_txn_apply_applied (host post-image, owner stamp checked): MUSEUM_DONATE behaves like a DROP (slot emptied), "
          "POLICE_CLAIM like a grant (the claimed item enters the slot; the collect bit is the vanilla side effect)",
          "const int is_claim = T->kind == (uint8_t)PC_NETGAME_TXN_KIND_POLICE_CLAIM;" in ap_body and "const int gain = is_pickup || is_grant || is_claim;" in ap_body
          and "pcnetgame_owner_stamp_matches(&T->owner)" in ap_body)
    ts_try = func_body(c, "pcnetgame_txn_try_send")
    check("C try_send: MUSEUM_DONATE needs the offered item still in its slot, POLICE_CLAIM a free slot (else the op is cancelled = REJECTED, nothing changed); the lost-and-found index travels as aux_cond; "
          "both go out as a TXN_COMMIT",
          "case PC_NETGAME_TXN_KIND_MUSEUM_DONATE:" in ts_try and "case PC_NETGAME_TXN_KIND_POLICE_CLAIM:" in ts_try and "aux_cond = T->ts_aux;" in ts_try
          and "T->kind == (uint8_t)PC_NETGAME_TXN_KIND_MUSEUM_DONATE || T->kind == (uint8_t)PC_NETGAME_TXN_KIND_POLICE_CLAIM" in ts_try)
    res_h = func_body(c, "pcnetgame_handle_client_txn_result")
    check("C the result handler resolves the UI operation on every matched outcome: REJECTED -> rejected, a non-echoing APPLIED -> rejected, APPLIED -> applied (or rejected when the apply step did not apply, L2) AFTER pcnetgame_txn_apply_applied",
          res_h.count("pcnetgame_ts_op_resolve(") == 3 and res_h.index("pcnetgame_txn_apply_applied(&T, in);") < res_h.index("pcnetgame_ts_op_resolve(T.kind, T.request_id, applied_ok);")
          and "pcnetgame_ts_op_resolve(s_ctxn.kind, s_ctxn.request_id, 0);" in func_body(c, "pcnetgame_txn_cancel_queued"))
    check("C the client UI seams: begin returns 1 / 0 / -1 (not a READY client -> 0), poll reports PENDING until resolved then APPLIED / REJECTED exactly once, a link loss reports REJECTED; "
          "the session reset clears the operation",
          "s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY" in func_body(cblk_raw, "pcnetgame_ts_begin") and "return -1;" in func_body(cblk_raw, "pcnetgame_ts_begin")
          and "return PC_NETGAME_TS_OP_PENDING;" in func_body(cblk_raw, "pc_net_game_ts_poll") and "memset(&s_ts_op, 0, sizeof(s_ts_op));" in func_body(c, "pcnetgame_reset_client_session_state")
          and all(x in h for x in ("int pc_net_game_ts_begin_museum_donate(int pocket_slot, int item);", "int pc_net_game_ts_begin_police_claim(int pocket_slot, int police_idx, int item);",
                                   "int pc_net_game_ts_poll(void);", "#define PC_NETGAME_TS_OP_PENDING  0")))
    check("C mail-based museum donations / gift letters stay refused for a client (Stage 0, unchanged: aPG_check_destination refuses MUSEUM mail with a gift)",
          "mMl_NAME_TYPE_MUSEUM" in func_body(read("src/actor/npc/ac_npc_post_girl.c_inc"), "aPG_check_destination") and "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT" in func_body(read("src/actor/npc/ac_npc_post_girl.c_inc"), "aPG_check_destination"))

    # ------------------------------------------------------------------ T: test hooks
    hook = func_body(cblk_raw, "pcnetgame_run_ts_test_hook")
    check("T the hooks are default OFF (globals 0 / NULL), documented in --help and pc_platform.h, loud ([NET][TS][TEST-ONLY]); the donate / claim hook is CLIENT-only and a complete no-op unless armed; "
          "the seed hook is HOST-only",
          "int g_pc_ts_test_donate = 0;" in main_c and "int g_pc_ts_test_claim = 0;" in main_c and "g_pc_ts_test_seed_police = NULL;" in main_c
          and all(x in main_c for x in ("--ts-test-seed-police=HEX", "--ts-test-donate ", "--ts-test-claim ")) and all(x in plat_h for x in ("g_pc_ts_test_seed_police", "g_pc_ts_test_donate", "g_pc_ts_test_claim"))
          and "(!g_pc_ts_test_donate && !g_pc_ts_test_claim) || s_stage >= 5 || s_role != PC_NETGAME_ROLE_CLIENT" in hook and "[NET][TS][TEST-ONLY]" in hook
          and "s_role != PC_NETGAME_ROLE_HOST" in func_body(hblk_raw, "pcnetgame_ts_test_seed_police") and c.count("pcnetgame_run_ts_test_hook();") == 1)
    check("T the donate hook's ONE local pocket write (a fish placed into a free slot, standing in for 'the player caught it') is inside the armed hook only, and it logs POCKET CHANGED BEFORE APPLIED "
          "if the pockets move while the transaction is unresolved",
          hook.count("Now_Private->inventory.pockets[s_slot] =") == 1 and "POCKET CHANGED BEFORE APPLIED" in hook)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
