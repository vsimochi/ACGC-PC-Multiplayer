#!/usr/bin/env python3
"""test_pdata_src.py - SOURCE AUDIT of PERSONAL DATA sync (diary only: kinds 3 PDATA_COMMIT / 4 PDATA_PUSH on the HOUSE_* ids 59..61). No game process.
Tier: SOURCE AUDITED (the HOST half is PROTOCOL TESTED by test_pdata_protocol.py; the CLIENT half -- adopt only with no menu open, commit after the diary menu closed -- has no
process test at all and is audited here only).

  W  wire: NO new message id and NO struct change (ids still contiguous 1..66, the three house structs keep their sizes), kinds / size / chunk count C == python, HOST_CONFIG byte 1
     bit 1 (build, validator, client apply), the diary slot layout, no typedef for the local state (wire_baseline audits typedef blocks)
  H  host: kinds routed BEFORE the house-sync gate, the slot ALWAYS from the authenticated binding (the page field is only compared), a guest refused before any index, the
     handler order, validation (0x7F / 0x80), the commit shares hup_* / the xfer counter and answers the other kind BUSY, only the bound resident's own slot is pushed
  C  client: own slot from the PersonalID, adopt / commit gates (no menu, no commit in flight, canon before commit), never persists, a client without the bit never sends
  D  defaults / options / settings / the vanilla editor cannot produce what the validator refuses / the diary overlay copies the whole block / hp_mail decision
"""
import re
import sys

import net_spike_lib as L
import wire_baseline
from test_ts_src import read, strip_comments, func_body, in_order


def num(c, name):
    m = re.search(r"#define %s\s+\(?([0-9A-Fa-fx]+)u?\)?" % re.escape(name), c)
    return int(m.group(1), 0) if m else None


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    c = read("pc/src/pc_net_game.c")
    cs = strip_comments(c)
    h = read("pc/include/pc_net_game.h")
    card = read("pc/src/pc_m_card.c")
    main_c = read("pc/src/pc_main.c")
    stc = read("pc/src/pc_settings.c")
    sth = read("pc/include/pc_settings.h")
    road = read("docs/multiplayer-guest-roadmap.md")
    wb = read("pc/tools/net_spike/wire_baseline.py")

    # ------------------------------------------------------------------ W
    ids = dict(wire_baseline.c_message_ids(c))
    ck("W NO new message id: still contiguous 1..%d, 59 / 60 / 61 = HOUSE_BEGIN / CHUNK / ACK (wire_baseline.EXPECTED_MAX_MSG_ID unchanged)" % wire_baseline.EXPECTED_MAX_MSG_ID,
       wire_baseline.EXPECTED_MAX_MSG_ID >= 66 and sorted(ids.values()) == list(range(1, wire_baseline.EXPECTED_MAX_MSG_ID + 1)) and ids.get("PC_NETGAME_MSG_HOUSE_BEGIN") == 59 and ids.get("PC_NETGAME_MSG_HOUSE_ACK") == 61)
    ck("W NO struct change: the three house structs keep their sizes (36 / 1012 / 24) and the protocol version is still 8",
       all("_Static_assert(sizeof(%s) == %d," % (t, s) in c for t, s in (("PCNetGameHouseBeginMsg", 36), ("PCNetGameHouseChunkMsg", 1012), ("PCNetGameHouseAckMsg", 24)))
       and wire_baseline.header_protocol_ok(h) and L.PROTOCOL_VERSION == 8)
    ck("W kinds: 3 PDATA_COMMIT (C -> H), 4 PDATA_PUSH (H -> C) next to 1 / 2; block 0 = diary; one slot = 0x2E80 B = 12 chunks -- C == python",
       num(c, "PC_NETGAME_PDATA_KIND_COMMIT") == 3 == L.PDATA_KIND_COMMIT and num(c, "PC_NETGAME_PDATA_KIND_PUSH") == 4 == L.PDATA_KIND_PUSH
       and num(c, "PC_NETGAME_PDATA_BLOCK_DIARY") == 0 and num(c, "PC_NETGAME_PDATA_DIARY_SIZE") == 0x2E80 == L.PDATA_DIARY_SIZE
       and num(c, "PC_NETGAME_PDATA_CHUNKS") == 12 == L.PDATA_CHUNKS and num(c, "PC_NETGAME_HOUSE_KIND_OWNER_COMMIT") == 1 and num(c, "PC_NETGAME_HOUSE_KIND_CANON_PUSH") == 2)
    ck("W the diary slot layout is asserted in C: entries at +2, one slot = 12 x 992 = 0x2E80, 4 slots; PC_NETGAME_PDATA_DIARY_SIZE == PC_M_CARD_DIARY_SLOT_SIZE == 12 * mDI_ENTRY_SIZE",
       "offsetof(mCD_keep_diary_c, entries) == 2" in card and "PC_M_CARD_DIARY_SLOT_SIZE == 0x2E80" in card and "#define PC_M_CARD_DIARY_SLOT_SIZE 0x2E80" in h
       and "PC_NETGAME_PDATA_DIARY_SIZE == (uint32_t)PC_M_CARD_DIARY_SLOT_SIZE" in c and "(mCD_KEEP_DIARY_ENTRY_COUNT * mDI_ENTRY_SIZE)" in c and "PLAYER_NUM == 4" in c)
    bld = func_body(c, "pcnetgame_ts_build")
    val = func_body(c, "pcnetgame_ts_valid_hostcfg_blob")
    ap = func_body(c, "pcnetgame_ts_client_apply")
    ck("W HOST_CONFIG byte 1 bit 1 = personal sync: built from pcnetgame_pdata_host_enabled(), the validator accepts bits 0 and 1 only, the client reads it (python constant 0x02 == C)",
       num(c, "PC_NETGAME_HOSTCFG_FLAG_PERSONAL_SYNC") == 2 == L.HOSTCFG_FLAG_PERSONAL_SYNC
       and "if (pcnetgame_pdata_host_enabled()) {\n            blob[1] |= (uint8_t)PC_NETGAME_HOSTCFG_FLAG_PERSONAL_SYNC;" in bld
       and "(blob[1] & ~(uint8_t)(PC_NETGAME_HOSTCFG_FLAG_HOUSE_SYNC | PC_NETGAME_HOSTCFG_FLAG_PERSONAL_SYNC)) != 0u" in val
       and "pcnetgame_pdata_client_set_hostcfg((m->blob[1] & (uint8_t)PC_NETGAME_HOSTCFG_FLAG_PERSONAL_SYNC) != 0);" in ap)
    ck("W the local state is NOT a typedef (wire_baseline audits typedef blocks): no PCNetGamePdata* typedef, wire_baseline.py needs no change",
       "PCNetGamePdata" not in c and "PCNetGamePdata" not in wb)

    # ------------------------------------------------------------------ H
    hd = func_body(c, "pcnetgame_handle_host_house")
    ck("H kinds are routed by KIND in the host dispatcher BEFORE the house handlers (which start with the --house-sync gate): PDATA_COMMIT -> pcnetgame_pdata_handle_begin, a PDATA_PUSH is dropped, "
       "a PDATA chunk is consumed before pcnetgame_house_handle_chunk; the house handler's own gate is unchanged",
       in_order(hd, ["PC_NETGAME_PDATA_KIND_COMMIT", "pcnetgame_pdata_handle_begin(peer, &b);", "PC_NETGAME_PDATA_KIND_PUSH", "pcnetgame_house_handle_begin(peer, &b);"])[0]
       and in_order(hd, ["pcnetgame_pdata_handle_chunk(peer, &c)", "pcnetgame_house_handle_chunk(peer, &c);"])[0]
       and "if (!g_pc_house_sync) {" in func_body(c, "pcnetgame_house_handle_begin") and "g_pc_house_sync" not in func_body(c, "pcnetgame_pdata_handle_begin")
       and "g_pc_house_sync" not in func_body(c, "pcnetgame_pdata_handle_chunk") and "g_pc_house_sync" not in func_body(c, "pcnetgame_pdata_process_commit")
       and "s_host_peer_link[peer] != PC_NETGAME_LINK_READY || !s_host_peer[peer].bound_valid" in hd)
    pb = strip_comments(func_body(c, "pcnetgame_pdata_handle_begin"))
    ok, miss = in_order(pb, ["pcnetgame_rec_gate(peer, in->xfer_id, 0)", "pcnetgame_pdata_host_enabled()", "strictly increasing", "st->hup_last_xfer = in->xfer_id;", "BUSY",
                             "if (idx >= PLAYER_NUM) {", "PC_NETGAME_HOUSE_ACK_NOT_OWNER", "in->house != (uint8_t)PC_NETGAME_PDATA_BLOCK_DIARY", "in->rsv != (uint16_t)idx",
                             "in->rsv2 != 0", "PC_NETGAME_PDATA_CHUNKS", "st->hup_open = 1;"])
    ck("H PDATA_COMMIT BEGIN order: binding gate, feature gate, strictly increasing xfer (shared hup_last_xfer), BUSY for an open transfer, a GUEST refused (NOT_OWNER) BEFORE the page / block "
       "fields are looked at, block, page == the bound slot, reserved, shape, then open (%s)" % miss, ok)
    ck("H the slot is NEVER taken from a message: in->rsv is only compared (!=) with the binding's idx, the BEGIN's house / rsv / house_seq are never used as an index, the slot used by the "
       "commit and the push is the binding's: idx = pcnetgame_rec_gate() / st->bound_resident_idx",
       len(re.findall(r"in->rsv\b", pb)) == 1 and "in->rsv != (uint16_t)idx" in pb and not re.search(r"\[\s*in->(rsv|house)\s*\]", pb) and "hup_house" not in strip_comments(func_body(c, "pcnetgame_pdata_process_commit"))
       and "const int slot = st->bound_resident_idx;" in strip_comments(func_body(c, "pcnetgame_pdata_pump_push")))
    pc = strip_comments(func_body(c, "pcnetgame_pdata_process_commit"))
    ok, miss = in_order(pc, ["idx = pcnetgame_rec_gate(peer, xfer, 0);", "if (idx >= PLAYER_NUM) {", "PC_NETGAME_HOUSE_ACK_NOT_OWNER", "!s_host_world_ready || st->hpush_active",
                             "pcnetgame_fnv1a32(rx, PC_NETGAME_PDATA_DIARY_SIZE) != st->hup_digest", "pcnetgame_pdata_menu_busy()", "st->rec_rl_run++;", "pcnetgame_pdata_host_refresh(idx)",
                             "st->hup_session != s_rec_host_session || st->hup_seq != s_pd_page[idx].rev", "pcnetgame_pdata_validate(rx, &bad)", "pc_m_card_diary_slot_put(idx, rx)",
                             "rs->dirty_unsaved = 1;", "PC_NETGAME_HOUSE_ACK_APPLIED"])
    ck("H commit order: binding gate, guest -> NOT_OWNER before any slot, world / nothing in flight (BUSY), digest, BUSY while the host's own player has a menu (before the rate accounting, "
       "like houses), the shared rate limit, page refresh, base (host_session, page rev) else STALE, validation, ONE put into the live block with the BOUND slot, the save marked dirty, APPLIED (%s)" % miss, ok)
    ck("H the live block is patched in exactly ONE place on the host (pcnetgame_pdata_process_commit) and in one place on the client (apply_stash, own slot only)",
       cs.count("pc_m_card_diary_slot_put(") == 2 and "pc_m_card_diary_slot_put(" in strip_comments(func_body(c, "pcnetgame_pdc_apply_stash")))
    ck("H a rejection other than BUSY / RATE_LIMITED / NOT_OWNER changes nothing and owes the canonical slot back (pd_sent_rev = 0)",
       "status != (uint8_t)PC_NETGAME_HOUSE_ACK_BUSY && status != (uint8_t)PC_NETGAME_HOUSE_ACK_RATE_LIMITED && status != (uint8_t)PC_NETGAME_HOUSE_ACK_NOT_OWNER" in func_body(c, "pcnetgame_pdata_reject")
       and "st->pd_sent_rev = 0;" in func_body(c, "pcnetgame_pdata_reject") and "pc_m_card_diary_slot_put" not in func_body(c, "pcnetgame_pdata_reject"))
    va = func_body(c, "pcnetgame_pdata_validate")
    ck("H validation constants: CHAR_CONTROL_CODE 0x7F and CHAR_MESSAGE_TAG 0x80 refused anywhere in the 0x2E80 B (the checksum / landid are never in the payload and never written)",
       num(c, "PC_NETGAME_PDATA_BAD_BYTE_CONTROL") == 0x7F and num(c, "PC_NETGAME_PDATA_BAD_BYTE_TAG") == 0x80 and "i < PC_NETGAME_PDATA_DIARY_SIZE" in va
       and "PC_NETGAME_PDATA_BAD_BYTE_CONTROL" in va and "PC_NETGAME_PDATA_BAD_BYTE_TAG" in va and "checksum" not in strip_comments(c[c.index("PERSONAL DATA HOST BEGIN"):c.index("PERSONAL DATA HOST END")]))
    ck("H total_size / chunk_count must be exactly 0x2E80 / 12 (BAD_SHAPE 1 / 2) and the chunk handler checks idx < 12, offset == idx * 1000, len, duplicates, the 32-bit got mask",
       "in->chunk_count != (uint8_t)PC_NETGAME_PDATA_CHUNKS || in->total_size != (uint16_t)PC_NETGAME_PDATA_DIARY_SIZE" in pb
       and all(s in func_body(c, "pcnetgame_pdata_handle_chunk") for s in ("in->chunk_idx >= PC_NETGAME_PDATA_CHUNKS", "(uint32_t)in->offset != off", "(uint32_t)in->len != want", "hup_got_mask & (1u << in->chunk_idx)")))
    pt = strip_comments(func_body(c, "pcnetgame_pdata_host_tick"))
    pp = strip_comments(func_body(c, "pcnetgame_pdata_pump_push"))
    ck("H push: only RESIDENT bindings (a guest is skipped), only the bound resident's own slot, after READY + record SYNCED, never while a commit is open, one push at a time (a house CANON_PUSH in "
       "flight blocks it, a PDATA_PUSH in flight makes the house pump return), the BEGIN carries rsv = the slot and kind 4",
       "st->bound_class != (uint8_t)PC_NETGAME_REC_CLASS_RESIDENT" in pt and "st->hup_open || st->rec_state != PC_NETGAME_RECS_SYNCED" in pp
       and "st->hpush_kind != (uint8_t)PC_NETGAME_PDATA_KIND_PUSH" in pp and "b.kind = (uint8_t)PC_NETGAME_PDATA_KIND_PUSH;" in pp and "b.rsv = (uint16_t)st->pd_push_slot;" in pp
       and "st->hpush_kind == (uint8_t)PC_NETGAME_PDATA_KIND_PUSH" in strip_comments(func_body(c, "pcnetgame_house_pump_push")))
    ck("H one transfer at a time: a PDATA_COMMIT BEGIN while an other transfer is open -> BUSY, an OWNER_COMMIT BEGIN while a PDATA_COMMIT is open -> BUSY (dispatcher), both share hup_open / hup_last_xfer / "
       "the single reassembly buffer s_host_house_rx; the page revision poll (1 Hz) bumps rev on a host-side change and the tick pumps the pushes",
       "if (st->hup_open && (uint32_t)(now - st->hup_started_ms) < PC_NETGAME_HOUSE_UPLOAD_TIMEOUT_MS) {" in pb and "s_host_peer[peer].hup_open && s_host_peer[peer].hup_pd" in hd
       and "s_host_house_rx[peer]" in func_body(c, "pcnetgame_pdata_handle_chunk") and "s_pd_page[slot].rev++;" in func_body(c, "pcnetgame_pdata_host_refresh")
       and "pcnetgame_pdata_pump_push((PCNetPeerId)p);" in pt and "pcnetgame_pdata_host_tick();" in cs and "pcnetgame_pdata_host_reset();" in cs)
    ck("H BUSY while the host's own player has any menu open (the diary overlay copies the WHOLE block at open and writes it back whole): a process outside GAME_PLAY (a dedicated host) is never busy",
       "if (gamePT == NULL || gamePT->exec != play_main) {\n        return 0;" in func_body(c, "pcnetgame_pdata_menu_busy") and "menu_type != mSM_OVL_NONE" in func_body(c, "pcnetgame_pdata_menu_busy"))
    ck("H the per-peer state is reset with the peer (the new fields live in PCNetGameHostPeerState and are cleared by its memset); a PDATA kind from the wrong side is dropped on both ends",
       all(re.search(r"uint8_t\s+%s;" % f, c) for f in ("hup_pd", "hpush_kind", "pd_push_slot")) and "uint32_t             pd_sent_rev;" in c
       and "return; /* a PDATA_PUSH is host -> client only: misdirected, dropped */" in c and "if (in->kind != (uint8_t)PC_NETGAME_HOUSE_KIND_CANON_PUSH) {\n        return;" in func_body(c, "pcnetgame_hcl_handle_begin"))

    # ------------------------------------------------------------------ C
    ch = func_body(c, "pcnetgame_handle_client_house")
    ck("C the client dispatcher routes kind 4 / the open push's chunks / the commit's ACK to the personal handlers first (house handler unchanged: an old client drops kind 4 at "
       "'in->kind != CANON_PUSH'; its chunks only count a logged violation, nothing is written)",
       in_order(ch, ["PC_NETGAME_PDATA_KIND_PUSH", "pcnetgame_pdc_handle_begin(&b);", "pcnetgame_hcl_handle_begin(&b);"])[0] and in_order(ch, ["pcnetgame_pdc_handle_chunk(&c)", "pcnetgame_hcl_handle_chunk(&c);"])[0]
       and in_order(ch, ["pcnetgame_pdc_handle_ack(&a)", "pcnetgame_hcl_handle_ack(&a);"])[0]
       and "if (!s_hcl.on) {" not in func_body(c, "pcnetgame_pdc_handle_begin") and "if (in->kind != (uint8_t)PC_NETGAME_PDATA_KIND_PUSH || !s_pdc.on) {" in func_body(c, "pcnetgame_pdc_handle_begin"))
    os_ = strip_comments(func_body(c, "pcnetgame_pdc_own_slot"))
    ck("C own slot: -1 for a guest claim / a foreigner / no save; otherwise the private_data index whose PersonalID is the local player's",
       "s_client_guest_claim_sent || !pcnetgame_client_is_resident_player()" in os_ and "mPr_CheckCmpPersonalID(&Save_Get(private_data)[i].player_ID, &Now_Private->player_ID) == TRUE" in os_ and "return -1;" in os_)
    ap2 = strip_comments(func_body(c, "pcnetgame_pdc_apply_stash"))
    ck("C adopt gates: link active (bit seen), no commit in flight, local save usable, NO submenu open (pcnetgame_pdata_menu_busy), the stash slot must equal the own slot (else dropped), the "
       "local slot is read-modify-written with the stash digest-checked bytes, canon moves with it",
       "!pcnetgame_pdc_active() || s_pdc.c_active || !pcnetgame_pdc_save_usable() || pcnetgame_pdata_menu_busy()" in ap2 and "own != (int)s_pdp.stash_slot" in ap2
       and in_order(ap2, ["pc_m_card_diary_slot_get(own, s_pdc_cur)", "pc_m_card_diary_slot_put(own, s_pdc_stash)", "s_pdp.canon_valid = 1;"])[0])
    cp = strip_comments(func_body(c, "pcnetgame_pdc_push_complete"))
    ok, miss = in_order(cp, ["pcnetgame_fnv1a32(s_pdc_rx, PC_NETGAME_PDATA_DIARY_SIZE) != s_pdc.rx_digest", "own < 0 || own != slot", "stale / duplicate", "equals our base", "memcpy(s_pdc_stash, s_pdc_rx",
                             "pcnetgame_pdc_apply_stash()"])
    ck("C push receive: digest, the slot must be the local player's, stale / duplicate, equals-base keeps local edits, then stash + apply (%s)" % miss, ok)
    tk = strip_comments(func_body(c, "pcnetgame_pdata_client_tick"))
    ck("C commit trigger (the 'diary menu closed' hook: a 500 ms poll of the slot digest against canon): needs a canon (never uploads before the host's slot was received), the own slot == canon slot, "
       "no stash pending, no commit in flight, local save usable, NO submenu open, the retry / min-gap timers, digest != canon; the xfer id comes from the house counter (shared hup_last_xfer)",
       "own < 0 || s_pdc.c_active || s_pdp.stash_valid || !s_pdp.canon_valid || s_pdp.slot != (uint8_t)own || !pcnetgame_pdc_save_usable() || pcnetgame_pdata_menu_busy()" in tk
       and "pcnetgame_crec_time_ok(s_pdc.retry_not_before_ms, now)" in tk and "== s_pdp.digest" in tk and "s_pdc.c_xfer = ++s_hcl.c_xfer_counter;" in tk and "if (!pcnetgame_pdc_active()) {\n        return;" in tk)
    ck("C a client that never saw the bit sends nothing: every client entry starts with pcnetgame_pdc_active() / s_pdc.on; the HOST_CONFIG read sets / clears it; the answers: APPLIED moves canon, STALE and "
       "every refusal set force (HOST wins), NOT_OWNER disables the feature for the session",
       "return s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY && s_pdc.on && !s_pdc.disabled;" in func_body(c, "pcnetgame_pdc_active")
       and "s_pdc.on = on ? 1 : 0;" in func_body(c, "pcnetgame_pdata_client_set_hostcfg") and "s_pdc.disabled = 1;" in func_body(c, "pcnetgame_pdc_handle_ack")
       and func_body(c, "pcnetgame_pdc_handle_ack").count("s_pdp.force = 1;") == 2 and "s_pdp.canon_valid = 1;" in func_body(c, "pcnetgame_pdc_handle_ack"))
    ck("C the client never persists: the personal client block writes no file (no fopen / pc_card / save call) and the only local write is pc_m_card_diary_slot_put into the in-memory block",
       not re.search(r"fopen|pc_card_|pc_save_write|mCD_save|rec_store_write", strip_comments(c[c.index("PERSONAL DATA CLIENT BEGIN"):c.index("PERSONAL DATA CLIENT END")])))
    ck("C lifecycle: on_ready (owner stamp: canon belongs to ONE local player), reset_session (link loss), tick, hostcfg are wired at the house hooks",
       "pcnetgame_pdata_client_on_ready();" in func_body(c, "pcnetgame_crec_on_ready") and "pcnetgame_pdata_client_reset_session();" in cs and "pcnetgame_pdata_client_tick();" in cs)

    # ------------------------------------------------------------------ D
    en = func_body(c, "pcnetgame_pdata_host_enabled")
    ck("D defaults: HOST only; --personal-sync on|off > settings.ini personal_sync (auto / on / off) > AUTO = on only while town_serve != off; settings default -1 (auto); the CLI option is host-only (exit 2) "
       "and prints in --help",
       "s_role != PC_NETGAME_ROLE_HOST" in en and in_order(en, ["g_pc_personal_sync_override >= 0", "g_pc_settings.personal_sync >= 0", "g_pc_town_serve_override", "g_pc_settings.town_serve", "return m != 0;"])[0]
       and ".personal_sync = -1," in stc and "int g_pc_personal_sync_override = -1;" in stc and 'strcmp(key, "personal_sync") == 0' in stc and "int personal_sync;" in sth
       and "extern int g_pc_personal_sync_override;" in sth and '"--personal-sync"' in main_c and "g_pc_personal_sync_override >= 0 && g_pc_net_role != 1" in main_c and "--personal-sync on|off HOST-only" in main_c)
    ed = read("src/game/m_editor_ovl.c")
    tabs = re.findall(r"static u8 (\w+)\[\] = \{(.*?)\};", ed, re.S)
    kb = set()
    orn = None
    for name, body in tabs:
        vals = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", body)]
        if name == "mED_ornament_table":
            orn = vals
        elif name.startswith(("letter", "sign_", "mark_")):
            kb |= set(vals)
    ck("D the vanilla editor cannot produce 0x7F / 0x80: the keyboard tables (letterS / letterL / sign / mark) contain neither, and the ornament exchange table maps both to themselves (m_editor_ovl.c); "
       "CHAR_CONTROL_CODE 127 / CHAR_MESSAGE_TAG 128 are the control family of m_font.c",
       orn is not None and len(orn) == 256 and kb and 0x7F not in kb and 0x80 not in kb and orn[0x7F] == 0x7F and orn[0x80] == 0x80 and orn.count(0x7F) == 1 and orn.count(0x80) == 1
       and "#define CHAR_CONTROL_CODE 127" in read("include/m_font.h") and "#define CHAR_MESSAGE_TAG 128" in read("include/m_font.h")
       and "return c == CHAR_CONTROL_CODE || c == CHAR_MESSAGE_TAG;" in read("src/game/m_font.c"))
    dov = read("src/game/m_diary_ovl.c")
    ck("D the vanilla diary overlay copies the WHOLE block at open and writes the whole block back (why the host answers BUSY and the client adopts only with no menu open); the calendar overlay does not "
       "touch the diary block",
       "mCD_save_data_aram_to_main(diary_ovl->data, mCD_KEEP_DIARY_SIZE, mCD_ARAM_DATA_DIARY);" in dov and "mCD_save_data_main_to_aram(diary_ovl->data, mCD_KEEP_DIARY_SIZE, mCD_ARAM_DATA_DIARY);" in dov
       and "ARAM" not in read("src/game/m_calendar_ovl.c"))
    ck("D the accessor lives in pc_m_card.c over the SAME l_aram_block_p_table block the GCI writer serialises (the diary is the third block), copies one slot, never touches checksum / landid",
       "int pc_m_card_diary_slot_put(int slot, const void* in) {" in card and "l_aram_block_p_table[mCD_ARAM_DATA_DIARY]" in func_body(card, "pc_m_card_diary_slot_put")
       and "checksum" not in func_body(card, "pc_m_card_diary_slot_put") and "memcpy(blk, l_aram_block_p_table[mCD_ARAM_DATA_DIARY], sz);" in card)
    san = read("pc/src/pc_town_sanitize.c")
    npc = read("src/game/m_npc.c")
    ck("D hp_mail decision: a game-logic READER exists (mNpc_SendHPMail acts on a non-zero receive_time), so the sanitizer does NOT zero it (documented, not changed)",
       "extern void mNpc_SendHPMail()" in npc and "hp_mail->receive_time.year != 0" in npc and "hp_mail" not in san)
    ck("D the roadmap documents the feature (section 'Personal data sync'), the deferred letter / design storage, hp_mail and the limits",
       "Personal data sync" in road and "PDATA_COMMIT" in road and "PDATA_PUSH" in road and "hp_mail" in road and "letter storage" in road and "design storage" in road)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
