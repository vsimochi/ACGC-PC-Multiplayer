#!/usr/bin/env python3
"""test_house_sync_src.py - SOURCE AUDIT of FURNITURE SYNC Stage 1 (host-authoritative player-house furniture, wire ids 59..61). No game process.
Tier: SOURCE AUDITED (the behaviour of the HOST half is PROTOCOL TESTED by test_house_sync_protocol.py; the CLIENT half -- dirty test, unsafe-scene gates, stash writer,
commit trigger, quit flush -- has no process test at all and is audited here only).

  W  wire: ids 59 / 60 / 61 + contiguity, 36 / 1012 / 24 byte structs with exact _Static_asserts, the image / commit sizes and chunk counts, the mHm_hs_c layout asserts,
     python constants == C, protocol version still 8, HOST_CONFIG byte 1 bit 0, pc_save_bswap_home public
  H  host: guest refused BEFORE any homes[] index, the ten handler steps in order, one commit site (merge + copy_owner after conservation), misdirected kinds dropped,
     guests never pushed, host-originated polling skips an unsafe house, per-peer / per-session reset
  G  client gates: plain upload / pocket transactions / FULL adoption / adoption watchdog, the commit trigger before the plain upload, the quit flush, the lifecycle
  S  every write of a received house into the in-memory save goes through ONE function, only after pcnetgame_local_house_unsafe(); the mailbox / haniwa are never written
  D  defaults: house sync OFF, test hooks off, documented
"""
import os
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
    main_c = read("pc/src/pc_main.c")
    plat = read("pc/include/pc_platform.h")
    bsw_c = read("pc/src/pc_save_bswap.c")
    bsw_h = read("pc/include/pc_save_bswap.h")
    names = read("include/m_name_table.h")
    road = read("docs/multiplayer-guest-roadmap.md")
    wb = read("pc/tools/net_spike/wire_baseline.py")

    # ------------------------------------------------------------------ W
    ids = dict(wire_baseline.c_message_ids(c))
    ck("W ids: 59 = HOUSE_BEGIN, 60 = HOUSE_CHUNK, 61 = HOUSE_ACK, all ids contiguous 1..%d (wire_baseline.EXPECTED_MAX_MSG_ID)" % wire_baseline.EXPECTED_MAX_MSG_ID,
       ids.get("PC_NETGAME_MSG_HOUSE_BEGIN") == 59 and ids.get("PC_NETGAME_MSG_HOUSE_CHUNK") == 60 and ids.get("PC_NETGAME_MSG_HOUSE_ACK") == 61
       and wire_baseline.EXPECTED_MAX_MSG_ID >= 66 and sorted(ids.values()) == list(range(1, wire_baseline.EXPECTED_MAX_MSG_ID + 1))
       and L.PC_NETGAME_MSG_HOUSE_BEGIN == 59 and L.PC_NETGAME_MSG_HOUSE_CHUNK == 60 and L.PC_NETGAME_MSG_HOUSE_ACK == 61)
    ck("W the protocol version is STILL 8 (extended in place, no bump): pc_net_game.h and net_spike_lib agree with wire_baseline",
       wire_baseline.header_protocol_ok(h) and L.PROTOCOL_VERSION == wire_baseline.EXPECTED_PROTOCOL_VERSION == 8)
    for t, size in (("PCNetGameHouseBeginMsg", 36), ("PCNetGameHouseChunkMsg", 1012), ("PCNetGameHouseAckMsg", 24)):
        ck("W %s: exact _Static_assert(sizeof == %d) and <= PC_NET_MAX_PAYLOAD" % (t, size),
           "_Static_assert(sizeof(%s) == %d," % (t, size) in c and "_Static_assert(sizeof(%s) <= PC_NET_MAX_PAYLOAD" % t in c)
    ck("W python specs: HOUSE_BEGIN 36 / HOUSE_CHUNK 1012 / HOUSE_ACK 24 bytes and registered in GAME_SPECS",
       L.HOUSE_BEGIN_SPEC.size == 36 and L.HOUSE_CHUNK_SPEC.size == 1012 and L.HOUSE_ACK_SPEC.size == 24
       and all(k in L.GAME_SPECS for k in (59, 60, 61)))
    ck("W the three wire structs are pinned by wire_baseline (V8_NEW_STRUCTS) and the two local structs are listed host-only",
       all(('"%s"' % t) in wb for t in ("PCNetGameHouseBeginMsg", "PCNetGameHouseChunkMsg", "PCNetGameHouseAckMsg", "PCNetGameHouseHost", "PCNetGameHouseClient")))
    ck("W constants: image 0x1A44, commit 16004 = image + 0x2440, chunk 1000, 17 / 7 chunks, kinds 1 / 2, ACK statuses 0..8 -- C == python",
       num(c, "PC_NETGAME_HOUSE_IMG_SIZE") == 0x1A44 == L.PC_NETGAME_HOUSE_IMG_SIZE and num(c, "PC_NETGAME_HOUSE_CHUNK_DATA") == 1000 == L.PC_NETGAME_HOUSE_CHUNK_DATA
       and "#define PC_NETGAME_HOUSE_COMMIT_SIZE     (PC_NETGAME_HOUSE_IMG_SIZE + PC_NETGAME_REC_SIZE)" in c and L.PC_NETGAME_HOUSE_COMMIT_SIZE == 0x1A44 + 0x2440 == 16004
       and num(c, "PC_NETGAME_HOUSE_COMMIT_CHUNKS") == 17 == L.PC_NETGAME_HOUSE_COMMIT_CHUNKS and num(c, "PC_NETGAME_HOUSE_PUSH_CHUNKS") == 7 == L.PC_NETGAME_HOUSE_PUSH_CHUNKS
       and num(c, "PC_NETGAME_HOUSE_KIND_OWNER_COMMIT") == 1 == L.PC_NETGAME_HOUSE_KIND_OWNER_COMMIT and num(c, "PC_NETGAME_HOUSE_KIND_CANON_PUSH") == 2 == L.PC_NETGAME_HOUSE_KIND_CANON_PUSH
       and all(num(c, "PC_NETGAME_HOUSE_ACK_" + n) == i == getattr(L, "PC_NETGAME_HOUSE_ACK_" + n)
               for i, n in enumerate(("APPLIED", "STALE", "BAD_DIGEST", "BAD_SHAPE", "INVALID_CELL", "CONSERVATION", "NOT_OWNER", "BUSY", "RATE_LIMITED"))))
    ck("W mHm_hs_c layout _Static_assert (0x26B0, floors 0x38, floor 0x8A8, mailbox 0x1A30, goki 0x2678, music_box 0x2684, layer 0x228) and the python offsets match the C defines",
       "sizeof(mHm_hs_c) == 0x26B0 && offsetof(mHm_hs_c, floors) == 0x38 && sizeof(mHm_flr_c) == 0x8A8 && offsetof(mHm_hs_c, mailbox) == 0x1A30" in c
       and "offsetof(mHm_hs_c, goki) == 0x2678 && offsetof(mHm_hs_c, music_box) == 0x2684" in c and "sizeof(mHm_lyr_c) == 0x228" in c
       and num(c, "PC_NETGAME_HOUSE_OFF_FLOORS") == 0x38 == L.HOUSE_OFF_FLOORS and num(c, "PC_NETGAME_HOUSE_FLOOR_STRIDE") == 0x8A8 == L.HOUSE_FLOOR_STRIDE
       and num(c, "PC_NETGAME_HOUSE_OFF_MUSIC") == 0x1A3C == L.HOUSE_OFF_MUSIC and num(c, "PC_NETGAME_HOUSE_FLOOR_OWNER_BYTES") == 0x8A5 == L.HOUSE_FLOOR_OWNER_BYTES
       and L.HOUSE_GCI_BASE == 0x40 + 0x26000 + 0x9CE8 and L.HOUSE_GCI_STRIDE == 0x26B0 and L.HOUSE_LAYER_STRIDE == 0x228)
    sv = read("include/m_common_data.h")
    ck("W the GCI offsets of homes[] (Save_t + 0x9CE8, stride 0x26B0) used by the python house_from_gci are the ones of include/m_common_data.h", "/* 0x009CE8 */ mHm_hs_c homes[PLAYER_NUM];" in sv)
    ck("W the item-class constants of the python double equal include/m_name_table.h (ITM_WALL_START 0x2700, ITM_CARPET_START 0x2600, ITM_MINIDISK_START 0x2A00, ITM_KABU_START 0x2F00, WALL_NUM / CARPET_NUM 67, MINIDISK_NUM 55)",
       "#define ITM_WALL_START 0x2700" in names and "#define ITM_CARPET_START 0x2600" in names and "#define ITM_MINIDISK_START 0x2A00" in names and "#define ITM_KABU_START 0x2F00" in names
       and "#define WALL_NUM 67" in names and "#define CARPET_NUM 67" in names and "#define MINIDISK_NUM 55" in names)
    ck("W pc_save_bswap_home is public (header + definition wrapping swap_mHm_hs)",
       "void pc_save_bswap_home(mHm_hs_c* home, pc_bswap_dir_t dir);" in bsw_h and re.search(r"void pc_save_bswap_home\(mHm_hs_c\* home, pc_bswap_dir_t dir\) \{\s*swap_mHm_hs\(home, dir\);", bsw_c))
    bld = func_body(c, "pcnetgame_ts_build")
    val = func_body(c, "pcnetgame_ts_valid_hostcfg_blob")
    ck("W HOST_CONFIG: byte 1 bit 0 = house sync built from g_pc_house_sync; the validator accepts only that bit and bit 1 (personal data sync, test_pdata_src.py) (other bits of byte 1 and bytes 2..7 must be zero)",
       "blob[1] = g_pc_house_sync ? (uint8_t)PC_NETGAME_HOSTCFG_FLAG_HOUSE_SYNC : 0u;" in bld and num(c, "PC_NETGAME_HOSTCFG_FLAG_HOUSE_SYNC") == 1 == L.PC_NETGAME_HOSTCFG_FLAG_HOUSE_SYNC
       and "blob[0] > 1u" in val and "(blob[1] & ~(uint8_t)(PC_NETGAME_HOSTCFG_FLAG_HOUSE_SYNC | PC_NETGAME_HOSTCFG_FLAG_PERSONAL_SYNC)) != 0u" in val and "for (i = 2; i < PC_NETGAME_TS_HOSTCFG_LEN; i++)" in val)
    ap = func_body(c, "pcnetgame_ts_client_apply")
    ck("W the client reads the bit in the HOST_CONFIG apply (no usable save needed) and a client without the bit never gates anything (every gate starts with pcnetgame_hcl_active() == on)",
       "pcnetgame_house_client_set_hostcfg((m->blob[1] & (uint8_t)PC_NETGAME_HOSTCFG_FLAG_HOUSE_SYNC) != 0);" in ap
       and "return s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY && s_hcl.on && !s_hcl.disabled;" in func_body(c, "pcnetgame_hcl_active")
       and all("if (!pcnetgame_hcl_active()) {" in func_body(c, n) for n in ("pcnetgame_house_client_txn_blocked", "pcnetgame_house_client_upload_deferred", "pcnetgame_house_client_adopt_blocker",
                                                                           "pcnetgame_house_client_adopt_wait", "pcnetgame_house_client_on_full_adopt", "pcnetgame_house_client_commit_tick",
                                                                           "pcnetgame_house_client_tick", "pcnetgame_house_client_quit_flush")))
    # ------------------------------------------------------------------ H
    hb = func_body(c, "pcnetgame_house_handle_begin")
    pc = func_body(c, "pcnetgame_house_process_commit")
    ok, miss = in_order(hb, ["pcnetgame_rec_gate(peer, in->xfer_id, 0)", "if (!g_pc_house_sync)", "PC_NETGAME_HOUSE_KIND_CANON_PUSH", "unknown kind", "strictly increasing",
                             "if (idx >= PLAYER_NUM) {", "pcnetgame_mbox_house_of(&Save_Get(private_data)[idx].player_ID)", "in->house != (uint8_t)h", "in->rsv != 0 || in->rsv2 != 0",
                             "PC_NETGAME_HOUSE_COMMIT_CHUNKS", "st->hup_open = 1;"])
    ck("H BEGIN handler order: gate, house sync on, misdirected CANON_PUSH dropped, unknown kind refused (+ violation), xfer increasing, GUEST -> NOT_OWNER BEFORE any homes[] / house index, house of the sender, claim checked, reserved, shape, open (missing: %s)" % miss, ok)
    ck("H the guest refusal uses the guest-slot test idx >= PLAYER_NUM (idx comes only from pcnetgame_rec_gate) and sends NOT_OWNER; the house index is derived from the BOUND record, never from the message",
       "PC_NETGAME_HOUSE_ACK_NOT_OWNER" in hb[hb.index("if (idx >= PLAYER_NUM) {"):hb.index("pcnetgame_mbox_house_of(")] and "in->house" not in hb[:hb.index("in->house != (uint8_t)h")])
    ok, miss = in_order(pc, ["pcnetgame_rec_gate(peer, xfer, 0)", "if (idx >= PLAYER_NUM) {", "pcnetgame_mbox_house_of(", "PC_NETGAME_HOUSE_ACK_BAD_SHAPE", "PC_NETGAME_HOUSE_ACK_BUSY",
                            "PC_NETGAME_HOUSE_ACK_BAD_DIGEST", "pcnetgame_local_house_unsafe(h)", "PC_NETGAME_HOUSE_ACK_RATE_LIMITED", "pcnetgame_rec_refresh_hostfields(idx, slot)",
                            "PC_NETGAME_HOUSE_ACK_STALE", "pcnetgame_rec_validate_fields(", "PC_NETGAME_HOUSE_ACK_INVALID_CELL", "pcnetgame_is_pocket_legal_item(nv)",
                            "pcnetgame_house_count_image(s_hs_cnt_old", "pcnetgame_house_conserved(", "PC_NETGAME_HOUSE_ACK_CONSERVATION", "pcnetgame_house_import_native(nimg",
                            "pcnetgame_rec_merge_into_save(idx, &s_rec_scratch_b)", "pcnetgame_house_copy_owner(&Save_Get(homes[h])", "pcnetgame_house_host_refresh(h, 1)",
                            "PC_NETGAME_HOUSE_ACK_APPLIED"])
    ck("H OWNER_COMMIT handler order: gate, guest NOT_OWNER, house, BAD_SHAPE, BUSY (world / record SYNCED / no push in flight), BAD_DIGEST, BUSY while the host's player is in the house (BEFORE the rate accounting), RATE_LIMITED, "
       "base STALE, record validated like an upload, cells, conservation, ONE commit (merge + owner-only copy), APPLIED (missing: %s)" % miss, ok)
    ck("H the commit is ONE call site: exactly one pcnetgame_rec_merge_into_save and one pcnetgame_house_copy_owner call in the commit handler, both after the CONSERVATION reject, and no early return between them and the ACK",
       pc.count("pcnetgame_rec_merge_into_save(") == 1 and pc.count("pcnetgame_house_copy_owner(") == 1 and pc.index("PC_NETGAME_HOUSE_ACK_CONSERVATION") < pc.index("pcnetgame_rec_merge_into_save(")
       and "return;" not in pc[pc.index("pcnetgame_house_import_native(nimg"):])
    blocks = strip_comments(c[c.index("/* ===== HOUSE COMMON + HOST BEGIN"):c.index("/* ===== HOUSE COMMON + HOST END")])         + strip_comments(c[c.index("/* ===== HOUSE CLIENT BEGIN"):c.index("/* ===== HOUSE CLIENT END")])
    assigns = [ln for ln in blocks.splitlines() if "Save_Get(homes[" in ln and "&Save_Get" not in ln and re.search(r"(?<![=!<>])=(?!=)", ln)]
    ck("H the house copy functions are the only writers of homes[] besides the TEST edit hook, and copy_owner / copy_canon never mention the mailbox or the haniwa (code, comments stripped)",
       all(k not in strip_comments(func_body(c, f)) for f in ("pcnetgame_house_copy_owner", "pcnetgame_house_copy_canon") for k in ("mailbox", "haniwa"))
       and len(assigns) == 1 and "layer_main.items[" in assigns[0])
    ck("H cell rule: a changed cell must be EMPTY / RSV_FE1F / pocket-legal, and a STRUCTURAL id (anything but EMPTY / RSV_FE1F / furniture / item1) may neither be written nor removed; floors the house lacks must be byte-identical; wall_floor in range",
       "ok = (nv == (mActor_name_t)RSV_FE1F || pcnetgame_is_pocket_legal_item(nv)) &&" in pc and "(ov == (mActor_name_t)EMPTY_NO || ov == (mActor_name_t)RSV_FE1F || ITEM_IS_FTR(ov) || ITEM_IS_ITEM1(ov));" in pc
       and "pcnetgame_house_floor_exists(&Save_Get(homes[h]), f)" in pc and "memcmp(nf, of, PC_NETGAME_HOUSE_FLOOR_OWNER_BYTES) != 0" in pc
       and "nf[0x8A0] >= (u8)FLOOR_ALL_NUM" in pc and "nf[0x8A1] >= (u8)WALL_ALL_NUM" in pc)
    cnt = func_body(c, "pcnetgame_house_count_id")
    ck("H conservation: normalisation N skips EMPTY / reserved / structural ids, maps furniture through mRmTp_FtrItemNo2Item1ItemNo(.., TRUE) to a rotation-free id, skips my-design mannequins / umbrellas, counts all four turnip ids as one class; "
       "wallpaper / carpet / disc classes may DECREASE, every other class must be exactly equal, the first 4 differing ids are logged",
       "mRmTp_FtrItemNo2Item1ItemNo(id, TRUE)" in cnt and "(mActor_name_t)(id & ~3u)" in cnt and "ITEM_IS_MYMANNIQUIN(id) || ITEM_IS_MYUMBRELLA(id)" in cnt
       and "ITM_KABU_START" in cnt and "default:\n            return;" in cnt and "ITEM_IS_MYUMBRELLA_TOOL(id)" in cnt
       and "if (nc[id] < oc[id] && pcnetgame_house_class_decreasable(id))" in func_body(c, "pcnetgame_house_conserved")
       and "first differing item ids (old -> new)" in pc and "diff[(*ndiff)++]" in func_body(c, "pcnetgame_house_conserved") and "*ndiff < 4" in func_body(c, "pcnetgame_house_conserved"))
    cr = func_body(c, "pcnetgame_house_count_record")
    ck("H conservation counts the 15 pockets AND the gift (present) of every USED letter (font != 0xFF) of the record mail[10]; wall / floor indexes map to wallpaper / carpet items except original designs; the music box bits to discs",
       "mPr_POCKETS_SLOT_COUNT" in cr and "mPr_INVENTORY_MAIL_COUNT" in cr and "r->mail[i].content.font != 0xFF" in cr and "r->mail[i].present" in cr
       and "ITM_CARPET_START + fl[0x8A0]" in func_body(c, "pcnetgame_house_count_image") and "ITM_WALL_START + fl[0x8A1]" in func_body(c, "pcnetgame_house_count_image")
       and "ITM_MINIDISK_START + n" in func_body(c, "pcnetgame_house_count_image") and "fl[0x8A0] < (u8)CARPET_NUM" in func_body(c, "pcnetgame_house_count_image")
       and "fl[0x8A1] < (u8)WALL_NUM" in func_body(c, "pcnetgame_house_count_image"))
    rj = func_body(c, "pcnetgame_house_reject")
    ck("H every rejection except BUSY / RATE_LIMITED / NOT_OWNER rolls the owner back: the house is owed again (hsent_seq cleared) and a FULL record push is queued; nothing was changed before",
       "status != (uint8_t)PC_NETGAME_HOUSE_ACK_BUSY && status != (uint8_t)PC_NETGAME_HOUSE_ACK_RATE_LIMITED && status != (uint8_t)PC_NETGAME_HOUSE_ACK_NOT_OWNER" in rj
       and "st->hsent_seq[h] = 0;" in rj and "pcnetgame_rec_stale_push(peer, idx, slot, pcnetgame_now_ms());" in rj)
    ht = func_body(c, "pcnetgame_house_host_tick")
    ck("H host tick: only with --house-sync + HOST role + world ready; guests are never pushed to; an open commit older than 5 s is discarded (nothing applied); the push pump is paced",
       "if (!g_pc_house_sync || s_role != PC_NETGAME_ROLE_HOST || !s_host_world_ready) {" in ht and "st->bound_class == (uint8_t)PC_NETGAME_REC_CLASS_GUEST" in ht
       and "PC_NETGAME_HOUSE_UPLOAD_TIMEOUT_MS" in ht and "discarded, nothing applied" in ht and "pcnetgame_house_pump_push((PCNetPeerId)p);" in ht
       and "backlog >= PC_NETGAME_SNAPSHOT_BACKLOG_LIMIT" in func_body(c, "pcnetgame_house_pump_push"))
    rf = func_body(c, "pcnetgame_house_host_refresh")
    ck("H host-originated changes: the canonical copy is re-read once per second, never while the host's own player is in that house (unsafe) except for the very first read and a commit's own write, and a changed digest bumps seq",
       num(c, "PC_NETGAME_HOUSE_PERIOD_MS") == 1000 and "if (s_hh[h].valid && !force && pcnetgame_local_house_unsafe(h)) {" in rf and "s_hh[h].seq++;" in rf
       and "pcnetgame_house_test_host_edit();" in ht and "pcnetgame_house_host_refresh(h, 0); /* host-originated changes" in ht)
    ck("H the host dispatchers: HOUSE_* ids go to pcnetgame_handle_host_house (READY + bound gate; a client's HOUSE_ACK and CANON_PUSH are dropped) and to pcnetgame_handle_client_house (READY only; an OWNER_COMMIT is dropped)",
       "data[0] >= (uint8_t)PC_NETGAME_MSG_HOUSE_BEGIN && data[0] <= (uint8_t)PC_NETGAME_MSG_HOUSE_ACK" in c and "pcnetgame_handle_host_house(peer, data, size);" in c
       and "pcnetgame_handle_client_house(data, size);" in c and "default:\n            return; /* HOUSE_ACK is host -> client only */" in func_body(c, "pcnetgame_handle_host_house")
       and "if (in->kind != (uint8_t)PC_NETGAME_HOUSE_KIND_CANON_PUSH) {\n        return;" in func_body(c, "pcnetgame_hcl_handle_begin")
       and "s_host_peer_link[peer] != PC_NETGAME_LINK_READY || !s_host_peer[peer].bound_valid" in func_body(c, "pcnetgame_handle_host_house"))
    ck("H per-peer state is cleared by the one per-peer memset (hup_* / hpush_* / hsent_seq are members of PCNetGameHostPeerState), the canonical copies by the host world reset, the host tick follows the record tick",
       all(re.search(m, c) for m in (r"uint8_t\s+hup_open;", r"uint8_t\s+hpush_active;", r"uint32_t\s+hsent_seq\[4\];")) and "pcnetgame_house_host_reset();   /* furniture sync" in c
       and c.index("pcnetgame_host_record_tick(); /* D3: HELLO/MIGRATE deadlines") < c.index("pcnetgame_house_host_tick();  /* furniture sync"))
    un = func_body(c, "pcnetgame_local_house_unsafe")
    ck("H pcnetgame_local_house_unsafe: unsafe without a running GAME_PLAY, during any fade / wipe, and in a player-house room of THAT house (field id index); forced by the TEST hook; reads no save",
       all(k in un for k in ("g_pc_house_test_host_in_house == h", "gamePT == NULL || gamePT->exec != play_main", "play->fb_fade_type != FADE_TYPE_NONE || play->fb_wipe_mode != WIPE_MODE_NONE",
                             "mSc_IS_SCENE_PLAYER_HOUSE_ROOM(sid)", "mFI_GetFieldId()", "mFI_GET_PLAYER_ROOM_NO(fid) == h", "return 1; /* a player room whose house cannot be identified"))
       and "Save_Get" not in un)
    # ------------------------------------------------------------------ G
    tb = func_body(c, "pcnetgame_txn_begin_blocked")
    ck("G pocket transactions: pcnetgame_txn_begin_blocked() includes the house gate (dirty own house / commit in flight / inside the house)", "pcnetgame_house_client_txn_blocked()" in tb)
    tg = func_body(c, "pcnetgame_house_client_txn_blocked")
    ck("G the gate body: commit in flight OR own house unsafe OR dirty, only for a resident with a house", "return s_hcl.c_active || pcnetgame_local_house_unsafe(h) || pcnetgame_hcl_dirty(h);" in tg and "if (h < 0) {\n        return 0;" in tg)
    ck("G plain record upload: pcnetgame_crec_upload_deferred() includes pcnetgame_house_client_upload_deferred() = !known || commit in flight || unsafe || dirty",
       "pcnetgame_house_client_upload_deferred()" in func_body(c, "pcnetgame_crec_upload_deferred")
       and "return !s_hcl.known || s_hcl.c_active || pcnetgame_local_house_unsafe(h) || pcnetgame_hcl_dirty(h);" in func_body(c, "pcnetgame_house_client_upload_deferred"))
    ab = func_body(c, "pcnetgame_crec_adopt_blocker")
    ck("G FULL adoption: pcnetgame_crec_adopt_blocker() consults the house blocker (commit in flight; a FULL push while the owner is inside its own house)",
       "pcnetgame_house_client_adopt_blocker(s_crec.st_kind == PC_NETGAME_REC_KIND_PUSH_FULL)" in ab and "a furniture commit is in flight" in func_body(c, "pcnetgame_house_client_adopt_blocker")
       and "staged_is_full && pcnetgame_local_house_unsafe(h)" in func_body(c, "pcnetgame_house_client_adopt_blocker"))
    ck("G the adoption watchdog treats 'owner inside its own house' as scene-lifecycle time (bounded by the cap), so a long visit never ends in ADOPT_FAILED",
       "pcnetgame_scene_is_pregame(sc) || pcnetgame_house_client_adopt_wait()" in func_body(c, "pcnetgame_crec_adopt_lifecycle_now"))
    ta = func_body(c, "pcnetgame_crec_try_adopt")
    ck("G a FULL adoption that replaces the client-owned ranges restores the own house in the same call (on_full_adopt after apply_staged, inside `if (!host_only)`), and after a refused commit the next FULL push replaces them even for the same lineage point (force_full_adopt)",
       "pcnetgame_crec_apply_staged(host_only, &cloth_refreshed, &equip_changed, &dirty_lost);\n    if (!host_only) {\n        if (rc_inside >= 0) {" in ta and "pcnetgame_house_client_on_full_adopt();" in ta
       and "pcnetgame_house_client_force_full_adopt()" in ta and ta.index("force_full_adopt") < ta.index("pcnetgame_crec_apply_staged("))
    ct = func_body(c, "pcnetgame_crec_tick")
    ck("G the commit is sent from pcnetgame_crec_tick BEFORE the plain upload trigger, its chunks are pumped there, and the plain upload only runs when the commit machinery does not own the tick",
       ct.index("pcnetgame_house_client_commit_tick(now)") < ct.index("upload trigger: digest the client-owned bytes") and "pcnetgame_house_client_pump();" in ct)
    cm = func_body(c, "pcnetgame_house_client_commit_tick")
    ok, miss = in_order(cm, ["s_hcl.c_active", "next_check_ms", "s_hcl.last_own_unsafe = pcnetgame_local_house_unsafe(h);", "!s_hcl.known || !s_hcp.canon_valid[h]",
                             "if (s_hcl.last_own_unsafe) {", "pcnetgame_hcl_inroom_ok(h)", "} else if (!pcnetgame_hcl_dirty(h)) {",
                             "s_crec.state != PC_NETGAME_CRS_SYNCED || s_crec.st_valid || s_crec.up_active", "pcnetgame_hcl_gates_pending()", "s_crec.base_session != s_hcp.canon_session[h]",
                             "pcnetgame_hcl_build_pair(h, in_room ? s_hcl_snap_img : NULL)", "pcnetgame_house_conserved(s_hcl_cnt_base, s_hcl_cnt_new", "pcnetgame_hcl_start_commit(h, now, in_room);"])
    ck("G commit trigger: house sync on, dirty (outside: the save; inside the room: ONLY a quiescent own room and the snapshot's digest), record SYNCED with no staged push / no record upload / no pickup-drop-bury-txn pending, both bases on the host's current session, rate gap, then the pair is built and sent (missing: %s)" % miss, ok)
    bp = func_body(c, "pcnetgame_hcl_build_pair")
    ck("G the pair is ONE payload: the local house image followed by the local record image", "pcnetgame_house_export_be(&Save_Get(homes[h]), s_hcl_tx);" in bp and "memcpy(s_hcl_tx, snap_img, PC_NETGAME_HOUSE_IMG_SIZE);" in bp
       and "memcpy(s_hcl_tx + PC_NETGAME_HOUSE_IMG_SIZE, s_crec_tx, PC_NETGAME_REC_SIZE);" in bp)
    pm = func_body(c, "pcnetgame_house_client_pump")
    ck("G the commit BEGIN carries the canon (host_session, seq) of the house and the CURRENT record base; digest = FNV of the whole 16004 byte payload", "b.host_session = s_hcp.canon_session[h];" in pm and "b.house_seq = s_hcp.canon_seq[h];" in pm
       and "b.rec_epoch = s_crec.base_epoch;" in pm and "b.rec_rev = s_crec.base_rev;" in pm and "pcnetgame_fnv1a32(s_hcl_tx, PC_NETGAME_HOUSE_COMMIT_SIZE)" in pm)
    ak = func_body(c, "pcnetgame_hcl_handle_ack")
    ck("G APPLIED moves the record base (pcnetgame_crec_set_base) and canon together; every refusal except BUSY / RATE_LIMITED / NOT_OWNER arms the rollback; NOT_OWNER disables the session; BUSY / RATE_LIMITED retry",
       "pcnetgame_crec_set_base(a->epoch, a->rev, a->host_session," in ak and "s_hcp.canon_seq[h] = a->house_seq;" in ak and ak.count("s_hcl.rollback = 1;") == 2 and "s_hcl.disabled = 1;" in ak
       and "PC_NETGAME_HCL_RETRY_BUSY_MS" in ak and "PC_NETGAME_HCL_RETRY_RATE_MS" in ak)
    qf = func_body(c, "pc_net_game_client_record_quit_flush")
    ck("G quit flush: the house flush runs first; when the player was inside its own house (or the house was never synced) the plain record flush is SKIPPED (it would upload the pockets of an unsent house edit alone)",
       qf.index("pcnetgame_house_client_quit_flush(max_ms)") < qf.index("t0 = pcnetgame_now_ms();")
       and "if (s_hcl.last_own_unsafe || !s_hcl.known || !s_hcp.canon_valid[h]) {" in func_body(c, "pcnetgame_house_client_quit_flush")
       and "return 0;" in func_body(c, "pcnetgame_house_client_quit_flush"))
    ctk = func_body(c, "pcnetgame_client_tick")
    ck("G client poll: pcnetgame_house_client_tick() right after pcnetgame_crec_tick(); the session part is cleared by pcnetgame_reset_client_session_state(), canon / stash survive for the same local player (owner stamp)",
       "pcnetgame_crec_tick(); /* D3: HELLO / push adoption / upload triggers */\n    pcnetgame_house_client_tick();" in ctk
       and "pcnetgame_house_client_reset_session();" in func_body(c, "pcnetgame_reset_client_session_state") and "memcmp(&cur, &s_hcp.owner, sizeof(cur)) != 0" in func_body(c, "pcnetgame_house_client_on_ready")
       and "pcnetgame_house_client_on_ready();" in func_body(c, "pcnetgame_crec_on_ready"))
    # ------------------------------------------------------------------ S
    ws = func_body(c, "pcnetgame_hcl_write_save")
    ap_ = func_body(c, "pcnetgame_hcl_apply_stash")
    fa = func_body(c, "pcnetgame_house_client_on_full_adopt")
    lw = func_body(c, "pcnetgame_hcl_live_apply")
    ck("S the ONLY client write of a received house into the save is pcnetgame_hcl_write_save() -> pcnetgame_house_copy_canon() (one call each); its callers are the stash writer, the FULL-adoption restore "
       "and the two live-apply paths of a VISITOR (no-rebuild path / the rebuild callback)",
       cs.count("pcnetgame_house_copy_canon(&Save_Get(homes[h]), &s_hs_native);") == 1 and cs.count("pcnetgame_hcl_write_save(") == 7 and "pcnetgame_hcl_write_save(h, s_hcp.stash_img[h])" in ap_
       and "pcnetgame_hcl_write_save(h, img)" in fa and "pcnetgame_house_copy_canon(" in ws and "pcnetgame_hcl_write_save(h, s_hcp.stash_img[h])" in lw
       and "pcnetgame_hcl_write_save(*(int*)ctx, s_hcp.stash_img[*(int*)ctx])" in func_body(c, "pcnetgame_hcl_live_write_cb"))
    ck("S both writers check pcnetgame_local_house_unsafe() BEFORE writing; the stash writer also refuses while the own house is dirty or its commit is in flight and while the local save is unusable",
       ap_.index("pcnetgame_local_house_unsafe(h)") < ap_.index("pcnetgame_hcl_write_save(") and fa.index("pcnetgame_local_house_unsafe(h)") < fa.index("pcnetgame_hcl_write_save(")
       and "if (h == own && (s_hcl.c_active || pcnetgame_hcl_dirty(h))) {" in ap_ and "!pcfa_save_ready() || !s_local_world_latched" in ap_)
    ck("S a push is validated before it is stashed: digest, strictly newer seq per (session, house), the owner of the pushed house equals the local owner; a push over a dirty own house is stashed, NEVER written over the edits",
       all(k in func_body(c, "pcnetgame_hcl_push_complete") for k in ("pcnetgame_fnv1a32(s_hcl_rx, PC_NETGAME_HOUSE_IMG_SIZE) != s_hcl.rx_digest", "s_hcl.rx_seq <= s_hcl.last_seq[h]",
                                                                     "mPr_CheckCmpPersonalID(&s_hs_native.ownerID, &Save_Get(homes[h]).ownerID) != TRUE", "s_hcp.stash_valid[h] = 1;")))
    ck("S the clients never persist a house: no GCI / save write function is called anywhere in the house blocks",
       not re.search(r"pc_save_write|mCD_|pcnetgame_rec_store_write|pc_save_gci", strip_comments(c[c.index("/* ===== HOUSE CLIENT BEGIN"):c.index("/* ===== HOUSE CLIENT END")])))
    # ------------------------------------------------------------------ L (live room, Stage 1b)
    mr = read("src/actor/ac_my_room.c")
    mrh = read("include/ac_my_room.h")
    mrs = strip_comments(mr)
    ex = strip_comments(func_body(mr, "aMR_pc_export_home"))
    rl = strip_comments(func_body(mr, "aMR_pc_live_reload"))
    qt = strip_comments(func_body(mr, "aMR_pc_room_quiet"))
    lrh = strip_comments(func_body(c, "pcnetgame_hcl_live_room_house"))
    ck("L ac_my_room.c: the three live-room entry points exist only under TARGET_PC (one guarded block before My_Room_Actor_dt) and are declared in ac_my_room.h under TARGET_PC",
       "#ifdef TARGET_PC\n/* ===== PC multiplayer furniture sync (Stage 1): live export" in mr and mr.index("int aMR_pc_live_reload(") < mr.index("static void My_Room_Actor_dt(ACTOR* actorx")
       and all(("extern int %s(" % n) in mrh[mrh.index("#ifdef TARGET_PC"):] for n in ("aMR_pc_room_quiet", "aMR_pc_export_home", "aMR_pc_live_reload")))
    ck("L the export works on a COPY and never modifies the live room: memcpy of the save house into `out`, actors only read through const pointers, no write to ftr_actor / used_list / the live layers (Save_Get is only the memcpy source)",
       "memcpy(out, &Save_Get(homes[idx]), sizeof(mHm_hs_c));" in ex and ex.count("Save_Get(") == 1 and "const FTR_ACTOR* ftr_actor" in ex and "FTR_ACTOR* ftr_actor" not in ex.replace("const FTR_ACTOR* ftr_actor", "")
       and "ftr_actor->items[item_idx] =" not in ex and "used_list[i] =" not in ex and "aMR_KeepItem2Fg" not in ex)
    ck("L the export reproduces the three teardown writes against the copy: ftr_switch (SaveSwitchData), haniwa_step (SaveHaniwaStepData), drawer / disc contents into the upper layers at the actor origin cell (KeepItem2Fg), and refuses when the field's layer pointers do not alias the save",
       "lyr[l].ftr_switch |=" in ex and "step_data[row] |=" in ex and "lyr[l].items[ut_z][ut_x] = ftr_actor->items[item_idx];" in ex and "aMR_Wpos2PlaceNumber(&ut_x, &ut_z, ftr_actor->position, ftr_actor->shape_type)" in ex
       and "aMR_GetLayerTopFg(l) != lyr[l].items[0]" in strip_comments(func_body(mr, "aMR_pc_live_house")))
    ck("L quiescence (room side): state == 0, no demo / message (current AND requested) / forced-open demo / emulator request / bgm reservation / throw lock / pickup / picking-up / leaf, no parent furniture, no reserved furniture, "
       "every used actor STOP with demo_status 0, wallpaper / carpet changers idle",
       all(k in qt for k in ("my_room->state != 0", "my_room->demo_flag != 0", "my_room->msg_type != aMR_MSG_STATE_NONE", "my_room->requested_msg_type != aMR_MSG_STATE_NONE", "my_room->force_open_demo_flag != 0",
                             "my_room->emu_info.request_flag != 0", "my_room->bgm_info.reserve_flag != 0", "my_room->throw_item_lock_flag != 0", "my_room->pickup_info.pickup_flag != 0",
                             "my_room->parent_ftr.ftrID != -1", "my_room->rsv_ftr[i].exist_flag != 0", "ftr_actor->state != aFTR_STATE_STOP || ftr_actor->demo_status != 0",
                             "my_indoor->wall_reserve.reserve_flag || my_indoor->floor_reserve.reserve_flag")))
    ck("L quiescence (room side): the vanilla picking_up_flag (never cleared after a pickup) is NOT tested, else the room stays 'not quiet' after the first furniture pickup of a visit",
       "picking_up_flag" not in qt and "my_room->pickup_info.pickup_flag != 0" in qt)
    ck("L quiescence (game side): GAME_PLAY running, no fade / wipe, a player-house room scene, submenu fully idle, no demo, message window hidden, player WAIT / WALK / RUN / DASH with no pending request, room quiet; >= 30 continuous GAME frames (game_frame based)",
       all(k in lrh for k in ("gamePT->exec != play_main", "play->fb_fade_type != FADE_TYPE_NONE || play->fb_wipe_mode != WIPE_MODE_NONE", "mSc_IS_SCENE_PLAYER_HOUSE_ROOM", "play->submenu.process_status != mSM_PROCESS_WAIT",
                             "play->submenu.menu_type != mSM_OVL_NONE", "play->submenu.mode != mSM_MODE_IDLE", "mDemo_CheckDemo() != FALSE", "!mMsg_Check_MainHide(mMsg_Get_base_window_p())",
                             "pl->requested_main_index_changed", "mPlayer_INDEX_WAIT", "mPlayer_INDEX_WALK", "mPlayer_INDEX_RUN", "mPlayer_INDEX_DASH", "aMR_pc_room_quiet()",
                             "need_own_room && Common_Get(field_type) != mFI_FIELDTYPE2_PLAYER_ROOM"))
       and num(c, "PC_NETGAME_HCL_QUIET_FRAMES") == 30 and "->game_frame" in func_body(c, "pcnetgame_hcl_quiet_frames") and "pcnetgame_hcl_quiet_update();\n    pcnetgame_hcl_room_track();" in func_body(c, "pcnetgame_house_client_tick"))
    ik = strip_comments(func_body(c, "pcnetgame_hcl_inroom_ok"))
    ck("L an in-room commit needs: a baseline captured at room entry, no refusal this visit, no rollback, no commit in flight, the OWN room quiescent NOW (live_room_house(1)) and for >= 30 frames; the dirty test is the snapshot digest vs canon",
       all(k in ik for k in ("!s_hcl.room_base_valid || s_hcl.room_stopped || s_hcl.rollback || s_hcl.c_active", "pcnetgame_hcl_live_room_house(1) != h", "pcnetgame_hcl_quiet_frames(h) >= PC_NETGAME_HCL_QUIET_FRAMES"))
       and "pcnetgame_house_room_digest(s_hcl_snap_img) == pcnetgame_house_room_digest(s_hcp.canon_img[h])" in strip_comments(cm))
    ck("L in-room commits only exist under house sync: the commit tick returns first when !pcnetgame_hcl_active(); aMR_pc_export_home is called from the client places only (snapshot, visitor live apply, the offline-taint reconcile plan / apply: 5 occurrences incl. the extern) and nowhere on the host",
       cm.lstrip().startswith("int h;") and "if (!pcnetgame_hcl_active()) {\n        return 0;" in cm and cs.count("aMR_pc_export_home(") == 5 and "aMR_pc_export_home(&s_hcl_snap, h, &fl)" in func_body(c, "pcnetgame_hcl_snapshot")
       and "aMR_pc_export_home(&s_hcl_cur, h, &fl)" in lw)
    ck("L client pre-check: before any in-room send the host's conservation rule is applied to (snapshot + pockets) vs the baseline pair captured at room entry / moved by each APPLIED; a mismatch stops in-room commits for the visit and sends nothing; the baseline moves only on APPLIED",
       cm.index("pcnetgame_house_conserved(s_hcl_cnt_base, s_hcl_cnt_new") < cm.index("pcnetgame_hcl_start_commit(h, now, in_room);") and "s_hcl.room_stopped = 1;" in cm
       and "if (s_hcl.c_inroom) {\n                memcpy(s_hcl_cnt_base, s_hcl_cnt_new" in ak and ak.count("s_hcl.room_stopped = 1;") == 2)
    ck("L after a STALE / refusal in-room commits stop (room_stopped) until the owner leaves; the FULL adoption / stash restore stay blocked inside the room (unchanged adopt_blocker / on_full_adopt unsafe gates)",
       "pcnetgame_local_house_unsafe(h)" in fa and "staged_is_full && pcnetgame_local_house_unsafe(h)" in func_body(c, "pcnetgame_house_client_adopt_blocker")
       and "|| s_hcl.c_active || pcnetgame_local_house_unsafe(h) || pcnetgame_hcl_dirty(h);" in tg + func_body(c, "pcnetgame_house_client_upload_deferred") and "s_hcl.room_base_valid = 0;" in func_body(c, "pcnetgame_hcl_room_track"))
    la = strip_comments(func_body(c, "pcnetgame_hcl_apply_stash"))
    ck("L a visitor's live apply never touches the OWN house: live_apply refuses h == own first, the stash writer only calls it as `h != own ? live_apply(h) : 0` when the house is unsafe; the ONLY own-house reload is the offline-taint reconcile inside try_adopt (pinned in section O)",
       "const int own = pcnetgame_hcl_own_house();" in lw[:120] and "h == own ||" in lw[:260] and "return h != own ? pcnetgame_hcl_live_apply(h) : 0;" in la and cs.count("aMR_pc_live_reload(") == 3
       and cs.count("pcnetgame_hcl_live_apply(") == 2)
    ck("L destroy-WITHOUT-write-back precedes the save write: the reload destroys every actor (dt_proc, CrossOffMoveBg, MinusWeight) and clears used_list before cb(ctx) writes the save, NEVER calls aMR_KeepItem2Fg / aMR_SaveSwitchData, and rebuilds the actors after the write",
       rl.index("profile->vtable->dt_proc(") < rl.index("(void)cb(ctx);") < rl.index("aMR_MakeFurnitureActor(actorx, play, mCoBG_LAYER0);") and rl.index("l_aMR_work.used_list[i] = FALSE;") < rl.index("(void)cb(ctx);")
       and "aMR_KeepItem2Fg" not in rl and "aMR_SaveSwitchData" not in rl and "mCoBG_CrossOffMoveBg(ftr_actor->move_bg_idx);" in rl and "aMR_MinusWeight(actorx, ftr_actor);" in rl
       and "aMR_pc_room_quiet()" in rl[:rl.index("(void)cb(ctx);")] and "aMR_ClearSwitchSaveData(my_room);" in rl[rl.index("(void)cb(ctx);"):])
    ck("L the rebuild is skipped (live floor left byte-identical) when the new layout equals the live one, is DEFERRED when the new layout puts furniture on a cell within one unit of the visitor, and only runs in a quiescent room for >= 30 frames",
       "pcnetgame_hcl_live_room_house(0) != h || pcnetgame_hcl_quiet_frames(h) < PC_NETGAME_HCL_QUIET_FRAMES" in lw and "memcpy(&Save_Get(homes[h]).floors[fl], &keep, sizeof(keep));" in lw
       and "nv != ov && (ITEM_IS_FTR(nv) || nv == (mActor_name_t)RSV_FE1F)" in lw and lw.index("deferred: the new layout puts furniture next to the visitor") < lw.index("aMR_pc_live_reload("))
    ck("L the host BUSY (host's player in / entering / leaving the house) is answered BEFORE the rate-limit window accounting and BEFORE rec_win_count++, so a retrying owner can never reach RATE_LIMITED / the peer drop because of BUSY",
       pc.index("pcnetgame_local_house_unsafe(h)") < pc.index("st->rec_win_start_ms") and pc.index("pcnetgame_local_house_unsafe(h)") < pc.index("st->rec_win_count++;") and pc.count("pcnetgame_local_house_unsafe(h)") == 1)
    ck("L fidelity hook: --house-test-fidelity (default off, logged as TEST-ONLY) makes My_Room_Actor_dt export before the teardown and log MATCH / MISMATCH after it; compiled out of non-PC builds",
       "int g_pc_house_test_fidelity = 0;" in main_c and 'strcmp(argv[i], "--house-test-fidelity") == 0' in main_c and "extern int           g_pc_house_test_fidelity;" in plat
       and re.search(r"aMR_pc_fidelity_before\(\);\s+aMR_SaveSwitchData\(my_room\);\s+aMR_AllFurnitureDestruct\(actorx, game\);\s+aMR_pc_fidelity_after\(\);", mr) is not None
       and "g_pc_house_test_fidelity &&" in mr and "#define aMR_pc_fidelity_before() ((void)0)" in mr)
    ck("L the roadmap documents the live room (live commit, live apply, quiescence, limitations, the unverified list) and no longer says the room works on a copy",
       "Stage 1b" in road and "quiescen" in road and "aMR_pc_export_home" in road and "unverified" in road[road.index("Stage 1b"):].lower()
       and "the room scene works on a COPY" not in road and "works on a COPY of the house floors" not in c)
    # ------------------------------------------------------------------ D
    ck("D house sync is OFF by default (g_pc_house_sync = 0, set only by --house-sync), the two TEST hooks default to off / NULL, all documented in --help and pc_platform.h",
       "int g_pc_house_sync = 0;" in main_c and "int g_pc_house_test_host_in_house = -1;" in main_c and "const char* g_pc_house_test_host_edit = NULL;" in main_c
       and 'strcmp(argv[i], "--house-sync") == 0' in main_c and "g_pc_house_sync = 1;" in main_c and "--house-sync        HOST opt-in" in main_c
       and "extern int           g_pc_house_sync;" in plat and "TEST-ONLY" in plat[plat.index("g_pc_house_sync") - 600:plat.index("g_pc_house_sync") + 400])
    ck("D the TEST hooks log loudly ([NET][HOUSE][TEST-ONLY]) and the host-edit hook only edits an owned house's canonical copy once a READY peer holds it",
       "[NET][HOUSE][TEST-ONLY] --house-test-host-in-house" in main_c and "[NET][HOUSE][TEST-ONLY] --house-test-host-edit" in main_c and "[NET][HOUSE][TEST-ONLY] --house-test-host-edit:" in c
       and "s_host_peer[pp].hsent_seq[v[0]] == s_hh[v[0]].seq" in func_body(c, "pcnetgame_house_test_host_edit"))
    ck("D the roadmap documents Stage 1 (Furniture synchronization: Stage 1, with the deviation notes, evidence tiers and the manual tests)",
       "### Furniture synchronization: Stage 1" in road and "NOT implemented" not in road[road.index("### Furniture synchronization: Stage 1"):road.index("### Furniture synchronization: Stage 1") + 200])
    # ------------------------------------------------------------------ R (own-room ground pickup)
    pk = read("src/game/m_player_main_pickup.c_inc")
    pks = strip_comments(pk)
    orl = strip_comments(func_body(c, "pc_net_game_client_own_room_local"))
    ck("R pc_net_game_client_own_room_local is declared in pc_net_game.h, defined once, and requires CLIENT + own house h >= 0 (guests excluded via pcnetgame_hcl_own_house) + player-house room scene + room no == h + PLAYER_ROOM field_type",
       "int pc_net_game_client_own_room_local(void);" in h and c.count("int pc_net_game_client_own_room_local(void) {") == 1
       and "s_role != PC_NETGAME_ROLE_CLIENT" in orl and "pcnetgame_hcl_own_house()" in orl and "h < 0" in orl and "mSc_IS_SCENE_PLAYER_HOUSE_ROOM" in orl
       and "mFI_GET_PLAYER_ROOM_NO(fid) != h" in orl and "Common_Get(field_type) == mFI_FIELDTYPE2_PLAYER_ROOM" in orl)
    ck("R the pickup seam only sends to the network when NOT own_room_local (predicate evaluated before pc_net_game_request_pickup), and the exchange_flag force-FALSE is skipped for it",
       "own_room_local = (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) && pc_net_game_client_own_room_local();" in pks
       and pks.index("pc_net_game_client_own_room_local()") < pks.index("pc_net_game_request_pickup(")
       and "PC_NETGAME_ROLE_CLIENT && !own_room_local) {\n        int ut_x, ut_z;" in pks and "PC_NETGAME_ROLE_CLIENT && !own_room_local) {\n        main_pickup_p->exchange_flag = FALSE;" in pks)
    ck("R full pockets (slot_idx < 0) in the own room block the pickup (no mutation, item stays on the floor); the money-bag-to-wallet shortcut is skipped so a bag goes to a pocket",
       "else if (own_room_local && (slot_idx < 0 || pc_net_game_client_room_edit_locked())) {" in pks and "host_pickup_blocked = TRUE;" in pks[pks.index("own_room_local && (slot_idx < 0"):pks.index("own_room_local && (slot_idx < 0") + 260]
       and "&& !own_room_local\n#endif\n            && mPr_GivePossessionBells(bell_amount)" in pks.replace(chr(13), ""))
    rp = func_body(c, "pc_net_game_request_pickup")
    ck("R the house-sync refusal in pc_net_game_request_pickup is logged (rate-limited), and visitors in another house stay blocked (the town-only rule is unchanged)",
       "[NET][PICKUP] refused:" in rp[:rp.index("ut_x < 0")] and "s_pickup_blk_log++ & 31u" in rp and "if (!pcfa_scene_is_town()) {" in rp)
    ck("R the roadmap documents the own-room pickup (cause, fix, safety, limitations)", "Own-room ground pickup" in road and "pc_net_game_client_own_room_local" in road)
    # ------------------------------------------------------------------ O (offline taint, edit lock, own-room reconcile)
    hello = strip_comments(func_body(c, "pcnetgame_crec_send_hello"))
    ck("O HELLO omits HAVE_LAST while the offline taint is set (the host then always answers with a FULL push: the same-host-process continuation hole is closed)",
       "&& !pcnetgame_house_client_offline_taint()) {\n        h.flags |=(uint8_t)PC_NETGAME_REC_HELLO_FLAG_HAVE_LAST;" in hello
       and "static int pcnetgame_house_client_offline_taint(void) {\n    return s_hcp.offline_taint;" in c)
    hs = strip_comments(c[c.index("continuation accepted") - 1500:c.index("continuation accepted") + 600])
    ck("O host: the no-push continuation needs HAVE_LAST + the same host session + epoch + rev, otherwise PUSH_FULL is started (so a HELLO without HAVE_LAST gets a FULL push in the same host session)",
       "(in->flags & PC_NETGAME_REC_HELLO_FLAG_HAVE_LAST) != 0 && in->last_host_session == s_rec_host_session" in hs and "pcnetgame_rec_start_push(peer, idx, (uint8_t)PC_NETGAME_REC_KIND_PUSH_FULL);" in hs
       and hs.index("continuation accepted") < hs.index("pcnetgame_rec_start_push(peer, idx, (uint8_t)PC_NETGAME_REC_KIND_PUSH_FULL);"))
    ll = strip_comments(func_body(c, "pcnetgame_client_on_link_lost"))
    ck("O taint is set in on_link_lost BEFORE the session reset (which zeroes s_hcl), only when house sync was on and the player owns a house; sync_last is set by set_hostcfg and survives the reset",
       "s_hcp.offline_taint = 1;" in ll and ll.index("s_hcp.offline_taint = 1;") < ll.index("pcnetgame_reset_client_session_state();") and "s_hcl.on && !s_hcl.disabled && pcnetgame_hcl_own_house() >= 0" in ll
       and "s_hcp.sync_last = s_hcl.on;" in strip_comments(func_body(c, "pcnetgame_house_client_set_hostcfg")) and "memset(&s_hcl, 0, sizeof(s_hcl));" in func_body(c, "pcnetgame_house_client_reset_session")
       and "s_hcp.sync_last = keep_sync_last;" in func_body(c, "pcnetgame_house_client_on_ready"))
    cmt = strip_comments(func_body(c, "pcnetgame_house_client_commit_tick"))
    qft = strip_comments(func_body(c, "pcnetgame_house_client_quit_flush"))
    ck("O commit_tick and the quit flush REFUSE while the taint is set (the offline pair can never be committed)",
       "if (s_hcp.offline_taint) {\n        return 1;" in cmt and cmt.index("s_hcp.offline_taint") < cmt.index("pcnetgame_hcl_build_pair") and "if (s_hcp.offline_taint) {" in qft
       and qft.index("s_hcp.offline_taint") < qft.index("pcnetgame_hcl_start_commit"))
    fa2 = strip_comments(fa)
    ck("O the taint is cleared ONLY by a FULL record adoption (on_full_adopt: not on a failed restore, not while inside) or by the own-room reconcile finish, by a new local player (owner change memset) or by a host without house sync",
       cs.count("s_hcp.offline_taint = 0;") == 6 and "if (pcnetgame_local_house_unsafe(h)) {\n        return;" in fa2 and fa2.index("pcnetgame_hcl_write_save(h, img)") < fa2.rindex("s_hcp.offline_taint = 0;")
       and "s_hcp.offline_taint = 0;" in strip_comments(func_body(c, "pcnetgame_house_client_reconcile_finish")) and "if (!s_hcl.on) {\n        s_hcp.offline_taint = 0;" in strip_comments(func_body(c, "pcnetgame_house_client_set_hostcfg")))
    lk = strip_comments(func_body(c, "pc_net_game_client_room_edit_locked"))
    ck("O the lock predicate: CLIENT + sync_last + own room local; locked when the link is not READY, the taint is set, or (not disabled) the house is unknown / the record not SYNCED; declared in pc_net_game.h with the denied / notice API",
       "s_role != PC_NETGAME_ROLE_CLIENT || !s_hcp.sync_last || !pc_net_game_client_own_room_local()" in lk and "s_client_link != PC_NETGAME_LINK_READY || s_hcp.offline_taint" in lk
       and "!s_hcl.known || s_crec.state != PC_NETGAME_CRS_SYNCED" in lk and "int pc_net_game_client_room_edit_locked(void);" in h and "void pc_net_game_room_edit_denied(void);" in h
       and "int pc_net_game_room_edit_notice(void);" in h)
    pmn = strip_comments(read("pc/src/pc_pause_menu.c"))
    ck("O the notice: one short rate-limited line drawn by pc_net_notice_draw ('Not connected - house changes are disabled' / 'Leave the house to resync')",
       "pc_net_game_room_edit_notice()" in pmn and "Not connected - house changes are disabled" in read("pc/src/pc_pause_menu.c") and "Leave the house to resync" in read("pc/src/pc_pause_menu.c")
       and "(uint32_t)(pcnetgame_now_ms() - s_edit_denied_ms) >= 3000u" in cs)
    pall = strip_comments(read("src/game/m_player_common.c_inc")).replace("\r", "")
    pa = pall[pall.index("static int Player_actor_CheckAndRequest_main_pickup_all(GAME* game) {"):]
    pa = pa[:pa.index("pc_net_game_client_room_edit_locked") + 400]
    tg = strip_comments(read("src/game/m_tag_ovl.c")).replace("\r", "")
    rp_ = tg[tg.index("static void mTG_room_put_proc(Submenu* submenu, mSM_MenuInfo_c* menu_info) {"):]
    mv = strip_comments(read("src/actor/ac_my_room_move.c_inc")).replace("\r", "")
    mc = strip_comments(read("src/actor/ac_my_room_msg_ctrl.c_inc")).replace("\r", "")
    ck("O lock gate: pickup_all returns FALSE first for field_type PLAYER_ROOM (no refuse demo), before any priority / request call",
       "field_type == mFI_FIELDTYPE2_PLAYER_ROOM && pc_net_game_client_room_edit_locked()" in pa and "return FALSE;" in pa[:pa.index("pc_net_game_client_room_edit_locked") + 200]
       and "Player_actor_Request_main_refuse_pickup_all" not in pa[:pa.index("pc_net_game_client_room_edit_locked") + 300])
    ck("O lock gate: mTG_room_put_proc opens the game's own refusal row (mWR_WARNING_PUT_ITEM) and returns BEFORE it reads the pocket / touches anything",
       rp_.index("pc_net_game_client_room_edit_locked()") < rp_.index("idx = mTG_get_table_idx(tag);") and "mTG_open_warning_window(submenu, menu_info, mWR_WARNING_PUT_ITEM);\n        return;" in rp_[:rp_.index("idx = mTG_get_table_idx(tag);")])
    ck("O lock gate: the push / pull / rotate grab returns FALSE before mPlib_request_main_hold_type1",
       mv.index("pc_net_game_client_room_edit_locked()") < mv.index("mPlib_request_main_hold_type1(game, contact_info0->ftrID") and mv.count("pc_net_game_client_room_edit_locked()") == 1)
    ck("O lock gate: wallpaper / carpet (4 procs) and putin are refused in the CALLERS in m_tag_ovl.c (ac_my_indoor's reserve procs return the old item, which would erase the pocket item), warning row + return before any change",
       tg.count("pc_net_game_client_room_edit_locked()") == 6)
    ck("O lock gate: the drawer / music-box choices (put in, take out, MD switch) are treated as cancel while locked (aMR_PcEditLocked in 6 handlers + its definition); the pickup seam has the belt-and-braces gate",
       mc.count("aMR_PcEditLocked()") == 7 and mc.count("pc_net_game_client_room_edit_locked()") == 1 and "(slot_idx < 0 || pc_net_game_client_room_edit_locked())" in pks)
    ra = strip_comments(func_body(c, "pcnetgame_house_client_reconcile_apply"))
    rr = strip_comments(func_body(c, "pcnetgame_hcl_reconcile_ready"))
    rpl = strip_comments(func_body(c, "pcnetgame_hcl_reconcile_plan"))
    ab2 = strip_comments(func_body(c, "pcnetgame_house_client_adopt_blocker"))
    tas = strip_comments(ta)
    ck("O reconcile: own-house reload ONLY inside try_adopt with !host_only, reload strictly BEFORE apply_staged, the FULL push stays staged (shared deferral) when the reload is not possible, finish AFTER apply_staged",
       cs.count("pcnetgame_house_client_reconcile_apply(") == 3 and tas.index("if (host_only) {") < tas.index("pcnetgame_house_client_reconcile_apply(rc_h)") < tas.index("pcnetgame_crec_apply_staged(")
       < tas.index("pcnetgame_house_client_reconcile_finish(rc_inside)") and "goto adopt_blocked_retry;" in tas and "aMR_pc_live_reload((GAME*)gamePT, pcnetgame_hcl_reconcile_write_cb, &h)" in ra
       and "if (!s_hcp.offline_taint || !pcnetgame_hcl_reconcile_ready(h)) {" in ra)
    ck("O reconcile terms: known, no commit in flight, live OWN room quiet >= 30 frames, a usable target (stash else canon), wall_floor equality with the live indoor actor, furniture-next-to-the-player deferral; the adoption blocker lets a FULL push through only then and only with the taint",
       "!s_hcl.known || s_hcl.c_active" in rr and "pcnetgame_hcl_live_room_house(1) != h || pcnetgame_hcl_quiet_frames(h) < PC_NETGAME_HCL_QUIET_FRAMES" in rr and "s_hcp.stash_valid[h]" in strip_comments(func_body(c, "pcnetgame_hcl_reconcile_target"))
       and "aMI_pc_wall_floor_matches(" in rpl and rpl.index("aMI_pc_wall_floor_matches(") < rpl.index("return 1;") and "nv != ov && (ITEM_IS_FTR(nv) || nv == (mActor_name_t)RSV_FE1F)" in rpl
       and "s_hcp.offline_taint && pcnetgame_hcl_reconcile_ready(h)" in ab2 and "return NULL;" in ab2[ab2.index("pcnetgame_hcl_reconcile_ready(h)"):ab2.index("pcnetgame_hcl_reconcile_ready(h)") + 200])
    ind = read("src/actor/ac_my_indoor.c").replace("\r", "")
    ck("O the indoor actor exports aMI_pc_wall_floor_matches (wall_num / floor_num equality, no pending reserve), TARGET_PC only",
       "int aMI_pc_wall_floor_matches(int wallpaper_idx, int flooring_idx) {" in ind and "my_indoor->wall_num == wallpaper_idx && my_indoor->floor_num == flooring_idx" in ind)
    ck("O the roadmap documents the disconnect / reconnect house state (taint, forced FULL push, edit lock, reconcile, limitations, manual tests)",
       "House state across disconnect/reconnect" in road and "offline taint" in road and "Leave the house to resync" in road and "aMI_pc_wall_floor_matches" in road)

    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
