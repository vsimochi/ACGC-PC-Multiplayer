#!/usr/bin/env python3
"""test_d3_record_src.py - D3-0..D3-3 (host + client halves) SOURCE AUDIT of the host-mirrored resident record (protocol v8).

No game process. Reads pc_net_game.c / pc_net_game.h / pc_save_bswap.[ch] / include/m_private.h / net_spike_lib.py and checks:
  W   wire: ids 1..50 contiguous and unique, 47..50 = RECORD_*, exact struct sizes with _Static_assert + <= PC_NET_MAX_PAYLOAD,
      all record sends RELIABLE, protocol 8u, net_spike_lib specs agree, wire_baseline accepts only the documented v8 additions
  I   index discipline: the resident index of every record handler comes from pcnetgame_rec_gate() (bound_resident_idx only), the
      D3 block never assigns bound_*; the ONLY writer of Save_Get(private_data)[] in the block is pcnetgame_rec_merge_into_save
      (and it copies only CLIENT-owned ranges); the live record is never byte-swapped in place
  O   ownership table: C table == python RECORD_FIELD_RANGES, tiles 0..0x2440, immutable/host-owned offsets match m_private.h
  V   validation derived from the game's tables (no literal bounds), caps = mPr_WALLET_MAX / mPr_DEPOSIT_MAX / mPlayer_DEBT4
  S   state machine / gates: READY+bound dispatch gate, per-peer state in the memset struct, static fixed buffers (no malloc),
      HELLO enforcement ON (compile-time constant 1, no env knob), timings, no sidecar/file IO in the record path
  C   CLIENT half: fixed static buffers / no malloc, adoption precondition list, ONE memcpy into Now_Private (no mPr_ init
      helper, no RNG), host-owned ranges only for HOSTFIELDS (shared ownership table, not duplicated), upload cadence/pacing,
      request gate, quit flush bounded and save-free, test hooks default off
Tier: SOURCE AUDITED. Exit code 0 when all checks pass."""
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
    m = re.search(r"^(?:static )?[\w\s\*]+?\b%s\(.*?\)\s*\{\n" % re.escape(name), src, re.M)
    if not m:
        return ""
    i = m.end()
    j = src.index("\n}\n", i)
    return src[i:j]


def resolve_raw_text(c_raw):
    """Raw (comment-bearing) text of pcnetgame_rec_store_resolve through the end of the persistence glue."""
    return c_raw[c_raw.index("static void pcnetgame_rec_resolve_slot(int i) {"):c_raw.index("void pc_net_game_record_after_gci_save")]


def main():
    results = []
    check = lambda d, c: L.check(d, c, results)
    c_raw = read("pc/src/pc_net_game.c")
    h = read("pc/include/pc_net_game.h")
    sw_c = read("pc/src/pc_save_bswap.c")
    sw_h = read("pc/include/pc_save_bswap.h")
    priv_h = read("include/m_private.h")
    b = c_raw.index("===== D3 BEGIN")
    e = c_raw.index("===== D3 END")
    blk_raw = c_raw[b:e]
    blk = strip_comments(blk_raw)
    c = strip_comments(c_raw)

    # ------------------------------------------------------------------ W: wire
    enum_body = c_raw[c_raw.index("typedef enum PCNetGameMsgType {"):c_raw.index("} PCNetGameMsgType;")]
    ids = [(m.group(1), int(m.group(2))) for m in re.finditer(r"^\s*(PC_NETGAME_MSG_\w+)\s*=\s*(\d+),", enum_body, re.M)]
    nums = [v for _n, v in ids]
    N = wire_baseline.EXPECTED_MAX_MSG_ID
    check("W message ids are 1..%d (wire_baseline.EXPECTED_MAX_MSG_ID), contiguous and unique (%d ids)" % (N, len(ids)),
          sorted(nums) == list(range(1, N + 1)) and len(set(nums)) == N and len(ids) == N)
    check("W RECORD_HELLO/BEGIN/CHUNK/ACK are exactly 47/48/49/50",
          dict((n, v) for n, v in ids if "RECORD" in n) == {"PC_NETGAME_MSG_RECORD_HELLO": 47, "PC_NETGAME_MSG_RECORD_BEGIN": 48,
                                                         "PC_NETGAME_MSG_RECORD_CHUNK": 49, "PC_NETGAME_MSG_RECORD_ACK": 50})
    for name, size in (("Hello", 24), ("Begin", 28), ("Chunk", 1012), ("Ack", 20)):
        t = "PCNetGameRecord%sMsg" % name
        check(f"W {t}: exact-size _Static_assert == {size} and <= PC_NET_MAX_PAYLOAD",
              f"_Static_assert(sizeof({t}) == {size}," in c_raw and f"_Static_assert(sizeof({t}) <= PC_NET_MAX_PAYLOAD," in c_raw)
    check("W record size/chunk constants and Private_c size are statically asserted",
          "#define PC_NETGAME_REC_SIZE         0x2440u" in c_raw and "_Static_assert(sizeof(Private_c) == PC_NETGAME_REC_SIZE" in c_raw
          and "== PC_NETGAME_REC_CHUNK_COUNT" in c_raw and "PC_NETGAME_REC_CHUNK_COUNT <= 16" in c_raw)
    check("W protocol is the expected v8 (header)", wire_baseline.header_protocol_ok(h) and wire_baseline.EXPECTED_PROTOCOL_VERSION == 8)
    check("W net_spike_lib agrees: PROTOCOL_VERSION == PC_NETGAME_PROTOCOL_VERSION == 8, spec sizes 24/28/1012/20, ids 47..50",
          L.PROTOCOL_VERSION == L.PC_NETGAME_PROTOCOL_VERSION == wire_baseline.EXPECTED_PROTOCOL_VERSION
          and (L.RECORD_HELLO_SPEC.size, L.RECORD_BEGIN_SPEC.size, L.RECORD_CHUNK_SPEC.size, L.RECORD_ACK_SPEC.size) == (24, 28, 1012, 20)
          and (L.PC_NETGAME_MSG_RECORD_HELLO, L.PC_NETGAME_MSG_RECORD_BEGIN, L.PC_NETGAME_MSG_RECORD_CHUNK, L.PC_NETGAME_MSG_RECORD_ACK) == (47, 48, 49, 50))
    sends = re.findall(r"pc_net_send\(([^()]*(?:\([^()]*\)[^()]*)*)\)", blk)
    check("W every pc_net_send in the D3 block is RELIABLE (%d sends)" % len(sends), len(sends) == 3 and all("PC_NET_RELIABLE" in s for s in sends))
    wire_baseline.run(lambda d, cond: check(d, cond), ROOT)
    check("W every enumerated ACK status / kind of the C header block matches net_spike_lib",
          all(re.search(r"#define PC_NETGAME_REC_%s\s+%du" % (n, getattr(L, "PC_NETGAME_REC_" + n)), c_raw)
              for n in ("KIND_PUSH_FULL", "KIND_PUSH_HOSTFIELDS", "KIND_UPLOAD", "KIND_MIGRATE_UPLOAD", "ACK_APPLIED", "ACK_STALE_BASE",
                        "ACK_BAD_DIGEST", "ACK_BAD_SHAPE", "ACK_INVALID_FIELD", "ACK_RATE_LIMITED", "ACK_NOT_BOUND", "ACK_BUSY",
                        "ACK_MIGRATE_REQUEST", "ACK_ADOPT_DEFERRED", "ACK_ADOPT_FAILED", "FIELD_PLAYER_ID", "FIELD_EXISTS", "FIELD_WALLET",
                        "FIELD_BANK", "FIELD_LOAN", "FIELD_POCKET", "FIELD_ITEM_COND", "FIELD_EQUIPMENT", "FIELD_ORG_TABLE")))

    # ------------------------------------------------------------------ I: index discipline
    funcs = re.findall(r"^static [\w\s\*]+?\b(pcnetgame_\w+)\(", blk, re.M)
    check("I D3 block defines the expected functions (%d)" % len(funcs), len(funcs) >= 25)
    check("I the D3 block never assigns bound_valid / bound_resident_idx / bound_pid",
          not re.search(r"bound_(?:valid|resident_idx|pid)\s*=[^=]", blk) and "mPr_CopyPersonalID(&st->bound_pid" not in blk)
    gate = func_body(blk_raw, "pcnetgame_rec_gate")
    check("I pcnetgame_rec_gate: READY check, bound_valid check, idx = st->bound_resident_idx, own-resident + bind-time PersonalID re-check",
          "s_host_peer_link[peer] != PC_NETGAME_LINK_READY" in gate and "!st->bound_valid" in gate
          and "idx = st->bound_resident_idx;" in gate and "pcnetgame_host_own_resident_idx()" in gate
          and "mPr_CheckCmpPersonalID(&st->bound_pid" in gate and "Save_Get(private_data)[idx].exists != TRUE" in gate)
    for fn in ("pcnetgame_rec_handle_hello", "pcnetgame_rec_handle_begin", "pcnetgame_rec_handle_chunk", "pcnetgame_rec_handle_ack",
               "pcnetgame_rec_process_upload"):
        body = func_body(blk_raw, fn)
        check(f"I {fn} obtains the resident ONLY through pcnetgame_rec_gate(peer, ..) and never indexes by a message field",
              "pcnetgame_rec_gate(peer" in body and not re.search(r"private_data\)\[\s*in->", body) and "in->resident" not in body)
    check("I no record handler reads a resident index from any message struct (no 'in->..idx'/'player_no' in the block)",
          not re.search(r"in->(?!chunk_idx)\w*(?:idx|resident|player_no)\w*", blk) and "in->chunk_idx" in blk)
    others = []
    for fn in funcs:
        if fn in ("pcnetgame_rec_merge_into_save",):
            continue
        body = func_body(blk_raw, fn)
        body = strip_comments(body)
        if re.search(r"Save_Get\(private_data\)\[[^\]]*\]\s*(?:\.\w+(?:\[[^\]]*\])?)+\s*=[^=]", body) \
                or re.search(r"mem(?:cpy|set)\(\s*&?\s*\(?\s*(?:\(uint8_t\*\))?\s*&?Save_Get\(private_data\)", body) \
                or re.search(r"mPr_\w+\(&Save_Get\(private_data\)", body) and "mPr_CopyPersonalID(&s->pid" not in body:
            others.append(fn)
    check("I no function of the D3 block other than pcnetgame_rec_merge_into_save writes Save_Get(private_data)[] (offenders: %s)" % others, not others)
    merge = strip_comments(func_body(blk_raw, "pcnetgame_rec_merge_into_save"))
    check("I the single merge helper copies ONLY ranges whose owner == CLIENT (one memcpy, owner test present)",
          merge.count("memcpy(") == 1 and "owner == PC_NETGAME_REC_OWN_CLIENT || s_rec_ranges[i].owner == PC_NETGAME_REC_OWN_SHARED" in merge and "(uint8_t*)&Save_Get(private_data)[idx]" in merge)
    check("I the merge is invoked exactly once, from the upload processor, after field validation and base checks",
          blk.count("pcnetgame_rec_merge_into_save(idx,") == 1 and
          func_body(blk_raw, "pcnetgame_rec_process_upload").index("pcnetgame_rec_validate_fields") <
          func_body(blk_raw, "pcnetgame_rec_process_upload").index("pcnetgame_rec_merge_into_save(idx,"))
    pu = func_body(blk_raw, "pcnetgame_rec_process_upload")
    order = [pu.index(x) for x in ("pcnetgame_rec_gate(", "pcnetgame_fnv1a32(rx", "PC_NETGAME_REC_MIN_GAP_MS", "up_base_epoch == slot->epoch",
                                   "pcnetgame_rec_validate_fields(", "pcnetgame_rec_merge_into_save(")]
    check("I validation order of design section 5: binding -> shape/digest -> rate -> base -> fields -> merge", order == sorted(order))
    # mail milestone 1: the per-letter hash helper (pcnetgame_mail_be_hash) converts a ZEROED static scratch Private_c holding ONE letter copy: a third, equally
    # harmless scratch conversion. The set of allowed targets stays closed: exactly these three scratch copies, never a live record.
    # mail milestone 2: the letter wire helpers pcnetgame_mail_to_be / pcnetgame_mail_from_be convert ZEROED static scratch Private_c copies holding ONE letter: two more equally
    # harmless scratch conversions. The set of allowed targets stays closed: exactly these five scratch copies, never a live record.
    check("I the live record is never converted in place: pc_save_bswap_private is only applied to the scratch copies (record scratch a / b + the mail-hash scratch + the two letter-wire scratches)",
          len(re.findall(r"pc_save_bswap_private\(", blk)) == 5 and "pc_save_bswap_private(&s_scratch, PC_BSWAP_TO_BE)" in blk and "pc_save_bswap_private(&s_scratch, PC_BSWAP_FROM_BE)" in blk
          and blk.count("static Private_c s_scratch;") == 2 and blk.count("memset(&s_scratch, 0, sizeof(s_scratch));") == 2
          and "pc_save_bswap_private(&s_rec_scratch_a, PC_BSWAP_TO_BE)" in blk
          and "pc_save_bswap_private(&s_rec_scratch_b, PC_BSWAP_FROM_BE)" in blk and "pc_save_bswap_private(&s_mail_scratch, PC_BSWAP_TO_BE)" in blk
          and "static Private_c s_mail_scratch;" in blk and "memset(&s_mail_scratch, 0, sizeof(s_mail_scratch));" in blk
          and "pc_save_bswap_private(&Save_Get" not in c)
    check("I the rx/tx images are static per-peer buffers; no malloc/calloc/realloc/free anywhere in the record path",
          "static uint8_t s_host_rec_rx[PC_NET_MAX_PEERS][PC_NETGAME_REC_SIZE];" in c_raw
          and "static uint8_t s_host_rec_tx[PC_NET_MAX_PEERS][PC_NETGAME_REC_SIZE];" in c_raw
          and not re.search(r"\b(?:malloc|calloc|realloc|free|alloca)\s*\(", blk))
    g0 = blk.index("static PCMpRecFile s_rec_file;")
    g1 = blk.index("static int pcnetgame_rec_send_ack(")
    blk_no_glue = blk[:g0] + blk[g1:]
    glue = blk[g0:g1]
    check("I no file IO / sidecar in the record protocol handlers (the D3-4 persistence glue is the only part that touches the store, and only through pc_mp_records_load/save)",
          not re.search(r"\b(?:fopen|fwrite|fread|remove|rename|records\.dat|\.gci)\b", blk_no_glue)
          and not re.search(r"\b(?:fopen|fwrite|fread|remove|rename|fclose|_commit|fsync)\b", glue) and "pc_mp_records_load(" in glue and "pc_mp_records_save(" in glue)
    check("I the host-field watcher only reads (digest), the slot lineage lives in static memory (PCNetGameRecSlot)",
          "static PCNetGameRecSlot s_rec_slot[PLAYER_NUM];" in c_raw and "pcnetgame_rec_hostfield_digest(const Private_c* r)" in blk)

    # ------------------------------------------------------------------ O: ownership table
    rows = [(int(a, 16), int(l, 16), o) for a, l, o in re.findall(r"\{\s*(0x[0-9A-Fa-f]+)u,\s*(0x[0-9A-Fa-f]+)u,\s*PC_NETGAME_REC_OWN_(\w+)\s*\}", blk)]
    own_name = {"IMMUTABLE": L.REC_OWN_IMMUTABLE, "HOST": L.REC_OWN_HOST, "CLIENT": L.REC_OWN_CLIENT, "SHARED": L.REC_OWN_SHARED}
    pyrows = [(off, ln, owner) for _n, off, ln, owner in L.RECORD_FIELD_RANGES]
    check("O C ownership table == python RECORD_FIELD_RANGES (%d ranges)" % len(rows),
          [(o, l, own_name[w]) for o, l, w in rows] == pyrows and len(rows) == 12)
    check("O ranges tile 0..0x2440 with no gap/overlap (checked on both tables)",
          rows[0][0] == 0 and all(rows[i][0] + rows[i][1] == rows[i + 1][0] for i in range(len(rows) - 1))
          and rows[-1][0] + rows[-1][1] == 0x2440)
    offs = {m.group(2): int(m.group(1), 16) for m in re.finditer(r"/\* 0x([0-9A-Fa-f]{4}) \*/\s+(?:[\w ]+?\s+\**)?(\w+)(?:\[[^\]]*\])?;", priv_h)}
    offs.update({m.group(2): int(m.group(1), 16) for m in re.finditer(r"/\* 0x([0-9A-Fa-f]{4}) \*/\s+(?:u8|u32|s8|mActor_name_t|PersonalID_c)\s+(\w+)", priv_h)})
    offs.update({m.group(2): int(m.group(1), 16) for m in re.finditer(r"/\* 0x([0-9A-Fa-f]{4}) \*/\s+mPr_catalog_order_c\s*\n?\s*(\w+)", priv_h)})
    check("O m_private.h offsets: player_ID 0x0, lotto 0x86/0x87, exists 0x1086, catalog_orders 0x10A8, reset_code 0x10F4",
          offs.get("player_ID") == 0 and offs.get("lotto_ticket_expiry_month") == 0x86 and offs.get("lotto_ticket_mail_storage") == 0x87
          and offs.get("exists") == 0x1086 and offs.get("catalog_orders") == 0x10A8 and offs.get("reset_code") == 0x10F4)
    owner_at = lambda off: next(o for a, l, o in rows if a <= off < a + l)
    check("O immutable = player_ID (+0x14) and exists; HOST-owned = museum_record (0x18, 0x4E B; mail milestone R) and reset_code (4) ONLY; SHARED (client-writable, host-consumed) = lotto x2 and catalog_orders (5*4); the rest CLIENT",
          owner_at(0) == "IMMUTABLE" and owner_at(0x13) == "IMMUTABLE" and owner_at(0x14) == "CLIENT" and owner_at(0x17) == "CLIENT"
          and owner_at(0x18) == "HOST" and owner_at(0x18 + 0x4D) == "HOST" and owner_at(0x66) == "CLIENT" and owner_at(0x67) == "CLIENT"
          and owner_at(0x86) == "SHARED" and owner_at(0x87) == "SHARED" and owner_at(0x88) == "CLIENT"
          and owner_at(0x1086) == "IMMUTABLE" and owner_at(0x1087) == "CLIENT"
          and owner_at(0x10A8) == "SHARED" and owner_at(0x10A8 + 19) == "SHARED" and owner_at(0x10BC) == "CLIENT"
          and owner_at(0x10F4) == "HOST" and owner_at(0x10F7) == "HOST" and owner_at(0x10F8) == "CLIENT"
          and sum(l for a, l, o in rows if o == "HOST") == 0x4E + 4 and sum(l for a, l, o in rows if o == "SHARED") == 2 + 20
          and sum(l for a, l, o in rows if o == "IMMUTABLE") == 0x14 + 1)
    check("O offsetof-style _Static_asserts pin every boundary against the real Private_c",
          all(x in c_raw for x in ("offsetof(Private_c, inventory.lotto_ticket_expiry_month) == 0x0086", "offsetof(Private_c, exists) == 0x1086",
                                   "offsetof(Private_c, catalog_orders) == 0x10A8", "offsetof(Private_c, reset_code) == 0x10F4",
                                   "sizeof(((Private_c*)0)->player_ID) == 0x14", "D3 ownership ranges must tile",
                                   "offsetof(Private_c, museum_record) == 0x0018", "sizeof(((Private_c*)0)->museum_record) == 0x004E",
                                   "offsetof(Private_c, inventory) == 0x0068")))
    check("O the museum_record range is the REAL struct member (measured offsetof 0x18 / sizeof 0x4E, NOT the 0x17 of m_private.h's comment: the struct is 2-byte aligned) and python agrees",
          L.REC_OFF_MUSEUM_RECORD == 0x18 and L.REC_MUSEUM_RECORD_SIZE == 0x4E
          and ("museum_record", 0x18, 0x4E, L.REC_OWN_HOST) in L.RECORD_FIELD_RANGES)
    check("O python table offsets of the named fields agree (pockets 0x68, wallet 0x8C, bank 0x122C, org table 0x2340)",
          L.REC_OFF_POCKETS == 0x68 and L.REC_OFF_WALLET == 0x8C and L.REC_OFF_BANK == 0x122C and L.REC_OFF_ORG_TABLE == 0x2340
          and re.search(r"/\* 0x0068 \*/ struct", priv_h) and re.search(r"/\* 0x122C \*/ u32 bank_account", priv_h)
          and re.search(r"/\* 0x2340 \*/ u8 my_org_no_table", priv_h) and re.search(r"/\* 0x008C \*/ u32 wallet", priv_h))

    # ------------------------------------------------------------------ V: validation derivation
    pl = func_body(blk_raw, "pcnetgame_is_pocket_legal_item")
    check("V pocket-legal predicate is derived from the game's tables (mNT_check_unknown, FTR0/FTR1 enum spans), no literal bounds",
          "mNT_check_unknown(item)" in pl and "FTR0_START" in pl and "FTR0_END" in pl and "FTR1_START" in pl and "FTR1_END" in pl
          and "NAME_TYPE_ITEM1" in pl and "EMPTY_NO" in pl and not re.search(r"0x[0-9A-Fa-f]{3,}", strip_comments(pl)))
    check("V caps come from the game constants (mPr_WALLET_MAX, mPr_DEPOSIT_MAX, loan = mPlayer_DEBT4 with a max assert)",
          "(u32)mPr_WALLET_MAX" in blk and "(u32)mPr_DEPOSIT_MAX" in blk and "((u32)mPlayer_DEBT4)" in blk and "mPlayer_DEBT4 >= mPlayer_DEBT3" in blk
          and "#define mPr_WALLET_MAX 99999" in priv_h and "#define mPr_DEPOSIT_MAX 999999999" in priv_h
          and "#define mPlayer_DEBT4 798000" in read("include/m_player.h"))
    vf = func_body(blk_raw, "pcnetgame_rec_validate_fields")
    vi = func_body(blk_raw, "pcnetgame_rec_validate_inventory")  # X1: the wallet/pocket/cond checks, shared with TXN_COMMIT's pre-image
    check("V field validation covers wallet, bank, loan, pockets, item cond bits 30-31, equipment, org-order permutation, immutables "
          "(wallet/pockets/conds through the shared pcnetgame_rec_validate_inventory, called from validate_fields)",
          all(x in vf + vi for x in ("inventory.wallet", "bank_account", "inventory.loan", "inventory.pockets", "0xC0000000u", "n->equipment",
                                     "my_org_no_table[i]", "be_client + 0x0000", "be_client[0x1086]"))
          and "pcnetgame_rec_validate_inventory(n->inventory.pockets, n->inventory.item_conditions, n->inventory.wallet)" in vf
          and "wallet > (u32)mPr_WALLET_MAX" in vi and "pockets[i]" in vi and "item_conditions & 0xC0000000u" in vi)

    # ------------------------------------------------------------------ S: state machine / gates
    disp = func_body(blk_raw, "pcnetgame_handle_host_record")
    check("S dispatcher: READY gate then bound gate (NOT_BOUND only for HELLO/BEGIN, only to READY peers)",
          "s_host_peer_link[peer] != PC_NETGAME_LINK_READY" in disp and "!s_host_peer[peer].bound_valid" in disp
          and disp.index("!= PC_NETGAME_LINK_READY") < disp.index("bound_valid") and "PC_NETGAME_REC_ACK_NOT_BOUND" in disp)
    check("S pc_net_game_poll host data path routes ids 47..50 to the record dispatcher, after the closing check",
          re.search(r"data\[0\] >= \(uint8_t\)PC_NETGAME_MSG_RECORD_HELLO && data\[0\] <= \(uint8_t\)PC_NETGAME_MSG_RECORD_ACK\) \{\s*pcnetgame_handle_host_record\(peer, data, size\)", c) is not None
          and c.index("s_host_peer[peer].closing") < c.index("pcnetgame_handle_host_record(peer, data, size)"))
    check("S per-peer record state lives inside PCNetGameHostPeerState (cleared by the single memset); the reset hook runs before it",
          all(x in c_raw[c_raw.index("typedef struct PCNetGameHostPeerState {"):c_raw.index("} PCNetGameHostPeerState;")]
              for x in ("rec_state;", "up_got_mask;", "up_xfer;", "rec_win_count;", "rec_deadline_ms;", "rec_violations;"))
          and c_raw.index("pcnetgame_rec_note_peer_gone(&s_host_peer[peer])") < c_raw.index("memset(&s_host_peer[peer], 0, sizeof(s_host_peer[peer]));"))
    enf = blk_raw[blk_raw.index("static int pcnetgame_rec_enforce_hello(void) {"):]
    enf = enf[:enf.index("\n}\n")]
    check("S HELLO enforcement is ON (named constant 1) and is a pure constant: no getenv / override can switch it off",
          "#define PC_NETGAME_REC_ENFORCE_HELLO 1" in c_raw and "PC_NETGAME_REC_ENFORCE_HELLO ? 1 : 0" in enf
          and "getenv" not in enf and 'PC_NETGAME_REC_ENFORCE_HELLO"' not in c_raw)
    check("S timings/limits: HELLO 5000, MIGRATE 10000, gap 1500, 30/min, 5 RATE_LIMITED, 3 violations, 5 s open-upload",
          all(x in c_raw for x in ("PC_NETGAME_REC_HELLO_TIMEOUT_MS   5000u", "PC_NETGAME_REC_MIGRATE_TIMEOUT_MS 10000u", "PC_NETGAME_REC_MIN_GAP_MS         1500u",
                                   "PC_NETGAME_REC_MAX_PER_MIN        30u", "PC_NETGAME_REC_RL_CLOSE_RUN       5u",
                                   "PC_NETGAME_REC_MAX_VIOLATIONS     3u", "PC_NETGAME_REC_UPLOAD_TIMEOUT_MS  5000u")))
    check("S chunk acceptance rules: xfer, idx<count, offset == idx*1000, exact len, got-mask bit, 3 violations close the peer",
          all(x in blk for x in ("in->xfer_id != st->up_xfer", "in->chunk_idx >= PC_NETGAME_REC_CHUNK_COUNT", "(uint32_t)in->offset != off",
                                 "(uint32_t)in->len != want", "st->up_got_mask & (uint16_t)(1u << in->chunk_idx)", "st->rec_violations >= PC_NETGAME_REC_MAX_VIOLATIONS")))
    check("S HELLO paths: rev==0 -> MIGRATE_REQUEST (AWAIT_MIGRATE), same session+(epoch,rev) -> APPLIED no push, else PUSH_FULL",
          all(x in blk for x in ("slot->rev == 0", "PC_NETGAME_REC_ACK_MIGRATE_REQUEST", "in->last_host_session == s_rec_host_session",
                                 "in->last_epoch == slot->epoch", "in->last_rev == slot->rev", "PC_NETGAME_REC_KIND_PUSH_FULL")))
    check("S MIGRATE only when rev==0 and AWAIT_MIGRATE with base (epoch, 0); UPLOAD only when SYNCED with base == current",
          "st->rec_state == PC_NETGAME_RECS_AWAIT_MIGRATE && slot->rev == 0 && st->up_base_rev == 0" in blk
          and "st->rec_state == PC_NETGAME_RECS_SYNCED && slot->rev > 0 && st->up_base_epoch == slot->epoch" in blk)
    check("S a MIGRATE import keeps a backup of the replaced host record in memory (s_rec_backup) before merging",
          "memcpy(s_rec_backup[idx], host_be" in blk and "slot->backup_valid = 1" in blk)
    check("S the host's own resident cannot be written: gate refuses own_idx, revalidation hook closes peers bound to it",
          "idx == own_idx" in gate and "bound resident became the host's own resident" in c_raw)
    check("S Q5 early-save flag exists (set on a dirty disconnect, consumer deferred): API declared in the header, no reader in the game yet",
          "int pc_net_game_record_early_save_requested(void);" in h and "void pc_net_game_record_note_saved(void);" in h
          and not re.search(r"pc_net_game_record_(?:early_save_requested|note_saved)\(", read("pc/src/pc_vi.c") + read("pc/src/pc_m_card.c")))
    check("S pc_save_bswap_private is public (header + definition) and wraps the static swap_Private",
          "void pc_save_bswap_private(Private_c* prv, pc_bswap_dir_t dir);" in sw_h
          and re.search(r"void pc_save_bswap_private\(Private_c\* prv, pc_bswap_dir_t dir\) \{\s*swap_Private\(prv, dir\);\s*\}", sw_c) is not None)
    check("S log lines use the [NET][REC] prefix", blk_raw.count("[NET][REC]") >= 20)
    cd = func_body(c_raw, "pcnetgame_handle_client_data")
    check("S client data path routes ids 47..50 to pcnetgame_handle_client_record only while the link is READY",
          re.search(r"data\[0\] >= \(uint8_t\)PC_NETGAME_MSG_RECORD_HELLO && data\[0\] <= \(uint8_t\)PC_NETGAME_MSG_RECORD_ACK\) \{\s*if \(s_client_link == PC_NETGAME_LINK_READY\) \{\s*pcnetgame_handle_client_record\(data, size\)", cd) is not None)
    check("S the FakeClient defaults answer the host (HELLO + migrate + push ACK) and keep enforcement-compat",
          L.FakeClient.record_hello and L.FakeClient.record_auto and L.FakeClient.record_wait)
    # ------------------------------------------------------------------ C: CLIENT half
    cb = c_raw.index("===== D3 CLIENT BEGIN")
    ce = c_raw.index("===== D3 CLIENT END")
    cli_raw = c_raw[cb:ce]
    cli = strip_comments(cli_raw)
    d3_all = blk + cli
    main_c = read("src/main.c")
    plat_h = read("pc/include/pc_platform.h")
    pc_main = read("pc/src/pc_main.c")
    check("C three fixed static 0x2440 buffers (rx, staged, tx), no malloc/calloc/realloc/free anywhere in the D3 blocks",
          all(("static uint8_t %s[PC_NETGAME_REC_SIZE];" % n) in cli for n in ("s_crec_rx", "s_crec_staged", "s_crec_tx"))
          and re.search(r"\b(?:malloc|calloc|realloc|free)\s*\(", d3_all) is None)
    check("C chunk acceptance identical to the host's: xfer, idx<count, offset==idx*1000, exact len, got-mask bit; a new BEGIN aborts an open one; strictly increasing xfer; digest checked",
          all(x in cli for x in ("in->xfer_id != s_crec.rx_xfer", "in->chunk_idx >= PC_NETGAME_REC_CHUNK_COUNT", "(uint32_t)in->offset != off",
                                 "(uint32_t)in->len != want", "s_crec.rx_got & (uint16_t)(1u << in->chunk_idx)", "in->xfer_id <= s_crec.rx_last_xfer",
                                 "s_crec.rx_open = 0;"))
          and "a new BEGIN aborts an open transfer" in cli_raw and "pcnetgame_fnv1a32(s_crec_rx, PC_NETGAME_REC_SIZE)" in cli and "s_crec.rx_digest" in cli)
    check("C only push kinds accepted from the host (client-only/unknown kind dropped); client sends BEGIN with host_session 0 and rsv 0",
          "in->kind != PC_NETGAME_REC_KIND_PUSH_FULL && in->kind != PC_NETGAME_REC_KIND_PUSH_HOSTFIELDS" in cli
          and "b.host_session = 0;" in cli and "b.rsv" not in cli)
    sends = re.findall(r"pc_net_send\(0,\s*(\w+)", cli)
    check("C every client record send is RELIABLE (%d sends)" % len(sends), len(sends) >= 4 and all(x == "PC_NET_RELIABLE" for x in sends))
    blocker = func_body(c_raw, "pcnetgame_crec_adopt_blocker")
    check("C adoption preconditions: save ready, latched save + Now_Private, owner stamp, PersonalID equal, no pending pickup/drop/bury/unresolved txn (X1b: replaces the deleted s_bury_committed)/exchange/catch/field-action, "
          "GAME_PLAY running, submenu fully idle (process_status/menu_type/mode/start_refuse_timer), no fade/wipe, real player actor, mPlib_able_submenu_type1",
          all(x in blocker for x in ("pcfa_save_ready()", "s_local_world_latched", "pcnetgame_owner_stamp_matches(&s_crec.owner)", "mPr_CheckCmpPersonalID(",
                                     "s_pickup_pending.valid", "s_drop_pending.valid", "s_bury_pending.valid", "pcnetgame_txn_busy()",
                                     "s_exchange_deferred.valid", "s_exchange_swap_slot >= 0", "s_catch_pending.valid", "s_field_action_queue_len > 0",
                                     "gamePT->exec != play_main", "submenu.process_status != mSM_PROCESS_WAIT", "submenu.menu_type != mSM_OVL_NONE",
                                     "submenu.mode != mSM_MODE_IDLE", "submenu.start_refuse_timer != 0", "fb_fade_type != FADE_TYPE_NONE",
                                     "fb_wipe_mode != WIPE_MODE_NONE", "pcnetgame_is_real_player_actor(pl)", "mPlib_able_submenu_type1((GAME*)play)")))
    ta = func_body(c_raw, "pcnetgame_crec_try_adopt")
    check("C a deferred adopt answers ADOPT_DEFERRED (1 s cadence), fails with ADOPT_FAILED after 10 s, and sends APPLIED after a successful adopt",
          "PC_NETGAME_REC_ACK_ADOPT_DEFERRED" in ta and "PC_NETGAME_REC_ACK_ADOPT_FAILED" in ta and "PC_NETGAME_REC_ACK_APPLIED" in ta
          and "#define PC_NETGAME_CREC_ADOPT_TIMEOUT_MS 10000u" in cli_raw and "#define PC_NETGAME_CREC_DEFER_ACK_MS     1000u" in cli_raw)
    ap_ = func_body(c_raw, "pcnetgame_crec_apply_staged")
    check("C apply: staged BE bytes converted FROM_BE on a COPY, host-owned-only for HOSTFIELDS, immutable ranges skipped, ONE memcpy into the live record",
          "pc_save_bswap_private(&s_crec_scratch, PC_BSWAP_FROM_BE)" in ap_ and "memcpy(&s_crec_scratch, s_crec_staged," in ap_
          and "r->owner == PC_NETGAME_REC_OWN_IMMUTABLE" in ap_ and "host_only && r->owner != PC_NETGAME_REC_OWN_HOST && r->owner != PC_NETGAME_REC_OWN_SHARED" in ap_
          and ap_.count("memcpy(np, &s_crec_merged, sizeof(Private_c))") == 1
          and len(re.findall(r"memcpy\(\s*(?:np|Now_Private)\b", cli)) == 1
          and "pc_save_bswap_private(Now_Private" not in cli and "pc_save_bswap_private(np" not in cli)
    check("C ownership table is shared with the host half (exactly one s_rec_ranges definition; the client code iterates it)",
          c_raw.count("static const PCNetGameRecRange s_rec_ranges[]") == 1 and "s_rec_ranges[i]" in ap_)
    check("C no mPr_ init helper, no RNG in the client record block (no mPr_Init*, mPr_SetNowPrivateCloth, RANDOM, fqrand, rand)",
          re.search(r"\b(mPr_Init\w*|mPr_SetNowPrivateCloth|RANDOM\w*|fqrand\w*|rand\s*\()", cli) is None)
    check("C cloth change refreshes the texture with the vanilla refresh (mPlib_change_player_cloth with cloth.idx), guarded by a loaded texture bank",
          "mPlib_change_player_cloth(gamePT, np->cloth.idx)" in ap_ and "mPlib_get_player_tex_p(gamePT) != NULL" in ap_)
    tick = func_body(c_raw, "pcnetgame_crec_tick")
    check("C upload cadence: 500 ms digest of the CLIENT-owned bytes, >= 2 s between starts, only in SYNCED, one transfer in flight, owner stamp, base = adopted (epoch, rev)",
          "#define PC_NETGAME_CREC_DIGEST_PERIOD_MS 500u" in cli_raw and "#define PC_NETGAME_CREC_UPLOAD_GAP_MS    2000u" in cli_raw
          and "s_crec.state != PC_NETGAME_CRS_SYNCED" in tick and "!s_crec.up_active && !s_crec.up_blocked" in tick
          and "pcnetgame_owner_stamp_matches(&s_crec.owner)" in tick and "s_crec.base_epoch, s_crec.base_rev" in tick
          and "PC_NETGAME_REC_KIND_UPLOAD" in tick and "pcnetgame_crec_cown_digest(" in cli)
    pu = func_body(c_raw, "pcnetgame_crec_pump_upload")
    check("C upload is paced under PC_NETGAME_SNAPSHOT_BACKLOG_LIMIT (never starves the snapshot pump), 4 messages per poll",
          "pc_net_reliable_backlog(0)" in pu and "PC_NETGAME_SNAPSHOT_BACKLOG_LIMIT" in pu and "PC_NETGAME_CREC_MSGS_PER_POLL" in pu
          and "#define PC_NETGAME_CREC_MSGS_PER_POLL    4" in cli_raw)
    ha = func_body(c_raw, "pcnetgame_crec_handle_ack")
    check("C ACK handling: APPLIED advances base/acked/last_*, STALE_BASE waits for the push then re-uploads, RATE_LIMITED/BUSY back off, BAD_*/INVALID_FIELD/NOT_BOUND stop uploads loudly, MIGRATE_REQUEST -> MIGRATE upload",
          all(x in ha for x in ("pcnetgame_crec_set_base(a->epoch, a->rev", "PC_NETGAME_REC_ACK_STALE_BASE", "PC_NETGAME_CREC_RETRY_STALE_MS",
                                "PC_NETGAME_REC_ACK_RATE_LIMITED", "PC_NETGAME_CREC_RETRY_RATE_MS", "PC_NETGAME_REC_ACK_BUSY", "PC_NETGAME_CREC_RETRY_BUSY_MS",
                                "PC_NETGAME_REC_ACK_BAD_DIGEST", "PC_NETGAME_REC_ACK_BAD_SHAPE", "PC_NETGAME_REC_ACK_INVALID_FIELD", "s_crec.up_blocked = 1",
                                "uploads STOPPED until the next host push", "PC_NETGAME_REC_ACK_MIGRATE_REQUEST", "s_crec.want_migrate = 1")))
    check("C the lineage moves only through pcnetgame_crec_set_base; a same-lineage FULL push after a STALE keeps local edits (host-owned ranges only)",
          cli.count("s_crec.base_epoch = ") == 1 and "s_crec.st_session == s_crec.base_session && s_crec.st_epoch == s_crec.base_epoch" in cli)
    reset_fn = strip_comments(func_body(c_raw, "pcnetgame_reset_client_session_state"))
    crec_reset = strip_comments(func_body(c_raw, "pcnetgame_crec_reset"))
    check("C link loss / reconnect clears the client record state (pcnetgame_crec_reset in the session reset) but NOT the carried last_* lineage",
          "pcnetgame_crec_reset();" in reset_fn and "s_crec_last" not in reset_fn and "s_crec_last" not in crec_reset
          and "memset(&s_crec, 0, sizeof(s_crec));" in crec_reset)
    check("C HELLO is sent after IDENTITY_ACK (on_ready hook in the ACK handler) with have_last only when the carried owner stamp matches",
          "pcnetgame_crec_on_ready();" in c_raw[c_raw.index("pc_remote_player_on_ready(PC_NETGAME_HOST_PLAYER_ID, &s_client_host_identity);"):][:400]
          and "memcmp(&s_crec_last.owner, &s_crec.owner" in func_body(c_raw, "pcnetgame_crec_send_hello"))
    gated = [("PICKUP", "pc_net_game_request_pickup"), ("DIG_BURIED", "pc_net_game_request_dig_buried"),
             ("DIG_HOLE(grant)", "pc_net_game_request_dig_hole_with_grant"), ("DIG_SHINE(grant)", "pc_net_game_request_dig_shine_with_grant"),
             ("DROP", "pc_net_game_request_drop"), ("BURY", "pc_net_game_request_bury"), ("CATCH", "pcnetgame_request_catch_common")]
    gate_fn = func_body(c_raw, "pcnetgame_client_record_gate_blocks")
    def fn_text(fn):  # also handles a signature that spans two lines
        i = c_raw.index("\n" + ("static " if fn == "pcnetgame_request_catch_common" else "int ") + ("int " if fn == "pcnetgame_request_catch_common" else "") + fn + "(")
        return c_raw[i:c_raw.index("\n}\n", i)]
    check("C request gate: pickup, dig_buried, dig_hole/shine with grant, drop (-> exchange), bury and catch refuse until the record is SYNCED (%d functions)" % len(gated),
          all('pcnetgame_client_record_gate_blocks("%s")' % w in fn_text(fn) for w, fn in gated)
          and "s_crec.state == PC_NETGAME_CRS_SYNCED" in gate_fn)
    qf = func_body(c_raw, "pc_net_game_client_record_quit_flush")
    qfs = strip_comments(qf)
    clause = main_c[main_c.index("if (pc_net_game_role_is_client()) {"):main_c.index("} else if (pcfa_save_ready()) {")]
    check("C quit flush: only in the CLIENT branch of the shutdown block, 1500 ms budget, no save written there",
          "pc_net_game_client_record_quit_flush(1500u);" in clause and "pc_save_write_authoritative" not in clause
          and "final shutdown save SKIPPED" in clause and "pc_save_write_authoritative()" in main_c[main_c.index("} else if (pcfa_save_ready()) {"):])
    check("C quit flush is bounded and light: deadline from max_ms, short SDL_Delay, pumps only the transport (no pc_net_game_poll, no world handlers), consumes only RECORD_ACK, gives up with a log",
          "deadline = t0 + max_ms" in qfs and "(int32_t)(now - deadline) >= 0" in qfs and "SDL_Delay(5)" in qfs and "pc_net_poll()" in qfs
          and "pc_net_game_poll(" not in qfs and "pcnetgame_handle_client_data" not in qfs and "PC_NETGAME_MSG_RECORD_ACK" in qfs
          and "quit flush gave up" in qf)
    check("C test hooks: --d3-test-wallet-add / -late are globals defaulting to 0, parsed in pc_main.c, documented in pc_platform.h, applied only in SYNCED (after the early return), loud [TEST-ONLY] log",
          "int g_pc_d3_test_wallet_add = 0;" in pc_main and "int g_pc_d3_test_wallet_add_late = 0;" in pc_main
          and "extern int           g_pc_d3_test_wallet_add;" in plat_h and "extern int           g_pc_d3_test_wallet_add_late;" in plat_h
          and tick.index("s_crec.state != PC_NETGAME_CRS_SYNCED") < tick.index("g_pc_d3_test_wallet_add != 0")
          and tick.index("s_crec.state != PC_NETGAME_CRS_SYNCED") < tick.index("g_pc_d3_test_wallet_add_late != 0")
          and cli_raw.count("[NET][REC][TEST-ONLY]") >= 2 and "TEST-ONLY" in plat_h)
    check("C the client sends HELLO from its first READY tick (hello_sent retried until queued), so the host's 5 s HELLO timeout is met",
          "if (!s_crec.hello_sent && " in tick and "pcnetgame_crec_send_hello();" in tick)
    check("C client log lines use the [NET][REC] prefix, loud ones are marked", cli_raw.count("[NET][REC]") >= 25 and "***" in cli_raw)

    # ------------------------------------------------------------------ R: D3 review fixes (host half)
    vf = func_body(blk_raw, "pcnetgame_rec_validate_fields")
    check("R SHARED fields are validated: catalog item pocket-legal-or-empty + shop_level < mSP_SHOP_TYPE_NUM, lotto month <= lbRTC_MONTHS_MAX, "
          "tickets != 0 needs month >= 1 (derived from m_post_office.c / m_shop.h / ac_npc_shop_common.c, no invented bounds)",
          "pcnetgame_is_pocket_legal_item(o->item)" in vf and "o->shop_level >= (u8)mSP_SHOP_TYPE_NUM" in vf and "PC_NETGAME_REC_FIELD_CATALOG" in vf
          and "lotto_ticket_expiry_month > (u8)lbRTC_MONTHS_MAX" in vf and "lotto_ticket_mail_storage != 0" in vf and "(u8)lbRTC_JANUARY" in vf
          and L.PC_NETGAME_REC_FIELD_CATALOG == 10 and L.PC_NETGAME_REC_FIELD_LOTTO == 11
          and "mPr_catalog_order_c" in priv_h and "mSP_SHOP_TYPE_NUM" in read("include/m_shop.h") and "lbRTC_MONTHS_MAX" in read("include/lb_rtc.h")
          and "#define aNSC_MAX_TICKETS 255" in read("include/ac_npc_shop_common.h"))
    hd = func_body(blk_raw, "pcnetgame_rec_hostfield_digest")
    check("R the host-consumption digest covers lotto (month + storage), catalog_orders, reset_code and (mail milestone R) museum_record (HOST + SHARED ranges)",
          all(x in hd for x in ("lotto_ticket_expiry_month", "lotto_ticket_mail_storage", "catalog_orders", "reset_code", "museum_record")))
    pu = func_body(blk_raw, "pcnetgame_rec_process_upload")
    check("R the upload path refreshes the host-consumption digest BEFORE the base check (stale-resurrection window closed), after the rate stage",
          "pcnetgame_rec_refresh_hostfields(idx, slot)" in pu
          and pu.index("PC_NETGAME_REC_MIN_GAP_MS") < pu.index("pcnetgame_rec_refresh_hostfields(idx, slot)") < pu.index("up_base_epoch == slot->epoch"))
    rf = func_body(blk_raw, "pcnetgame_rec_refresh_hostfields")
    check("R refresh: only for a synced slot (rev > 0), on a digest change rev++ + hf_pending + dirty; the merge recomputes the digest (an accepted upload never self-bumps)",
          "s->rev == 0" in rf and "s->rev++" in rf and "s->hf_pending = 1" in rf
          and "slot->hf_digest = pcnetgame_rec_hostfield_digest(&Save_Get(private_data)[idx]);" in pu
          and pu.index("pcnetgame_rec_merge_into_save(idx,") < pu.index("slot->hf_digest = pcnetgame_rec_hostfield_digest"))
    hh = func_body(blk_raw, "pcnetgame_rec_handle_hello")
    check("R HELLO: reserved-zero rule (_reserved0 / undefined flags -> BAD_SHAPE detail 6, no violation) and the digest refresh before the continuation decision",
          "in->_reserved0 != 0 || (in->flags & ~PC_NETGAME_REC_HELLO_FLAG_HAVE_LAST) != 0" in hh and "PC_NETGAME_REC_ACK_BAD_SHAPE, 6" in hh
          and hh.index("pcnetgame_rec_refresh_hostfields(idx, slot)") < hh.index("in->last_host_session == s_rec_host_session"))
    hb = func_body(blk_raw, "pcnetgame_rec_handle_begin")
    check("R BEGIN.rsv != 0 is refused: refuse_xfer + BAD_SHAPE detail 5, WITHOUT a violation (record_class seam enforced)",
          "if (in->rsv != 0) {" in hb and "PC_NETGAME_REC_ACK_BAD_SHAPE, 5" in hb
          and "pcnetgame_rec_violation(" not in hb[hb.index("if (in->rsv != 0) {"):hb.index("if (in->rsv != 0) {") + 400])
    tk = func_body(blk_raw, "pcnetgame_host_record_tick")
    check("R an upload timeout calls pcnetgame_rec_refuse_xfer(st, st->up_xfer) so late chunks are absorbed, not violations",
          "pcnetgame_rec_refuse_xfer(st, st->up_xfer)" in tk and tk.index("pcnetgame_rec_refuse_xfer(st, st->up_xfer)") < tk.index("st->up_open = 0;", tk.index("timed out")))
    wr = func_body(c_raw, "pcnetgame_reset_host_world_state")
    check("R a host world/session reset re-rolls the epochs AND host_session (pcnetgame_rec_on_world_reset)",
          "pcnetgame_rec_on_world_reset();" in wr and "s_rec_host_session = pcnetgame_rec_rand32();" in blk and "pcnetgame_rec_on_save_back();" in func_body(blk_raw, "pcnetgame_rec_on_world_reset"))
    check("R MIGRATE on a stale epoch while AWAIT_MIGRATE & rev 0: STALE_BASE then a repeated MIGRATE_REQUEST on the current epoch, deadline re-armed, nothing imported",
          "kind == PC_NETGAME_REC_KIND_MIGRATE_UPLOAD && st->rec_state == PC_NETGAME_RECS_AWAIT_MIGRATE && slot->rev == 0" in pu
          and "st->up_base_epoch != slot->epoch" in pu and "PC_NETGAME_REC_ACK_MIGRATE_REQUEST, 0, 0, slot->epoch, 0" in pu
          and "st->rec_deadline_ms = pcnetgame_now_ms() + PC_NETGAME_REC_MIGRATE_TIMEOUT_MS" in pu)
    disp = func_body(blk_raw, "pcnetgame_handle_host_record")
    check("R the CHUNK dispatch copies into an aligned local (no struct cast of the raw buffer)",
          "memcpy(&c, data, sizeof(c));" in disp and "(const PCNetGameRecordChunkMsg*)" not in disp)
    check("R unknown ACK status: comment, code and python agree (host counts any client status other than 0/9/10 as a violation)",
          "a HOST counts any client RECORD_ACK status other than 0/9/10 as a violation" in c_raw
          and "client sent a host-only RECORD_ACK status" in func_body(blk_raw, "pcnetgame_rec_handle_ack")
          and "a receiver ignores an unknown status" not in c_raw and "RECORD_ACK status other than 0/9/10" in read("pc/tools/net_spike/net_spike_lib.py"))
    check("R client: the dirty digest covers CLIENT + SHARED ranges, a HOSTFIELDS apply takes HOST + SHARED and re-baselines only a clean record",
          "== PC_NETGAME_REC_OWN_CLIENT || s_rec_ranges[i].owner == PC_NETGAME_REC_OWN_SHARED" in func_body(c_raw, "pcnetgame_crec_cown_digest")
          and "host_dirty_before" in func_body(c_raw, "pcnetgame_crec_try_adopt")
          and "!host_only || !host_dirty_before" in func_body(c_raw, "pcnetgame_crec_try_adopt"))
    check("R stale-resurrection by a consumed catalog order is covered by SOURCE AUDIT only (NOT hook-tested: no existing hook makes the host consume an order)", True)
    # ---------------- P: D3-4 persistence (sidecar save/mp/records.dat) ----------------
    store_c = strip_comments(read("pc/src/pc_mp_records.c"))
    store_h = read("pc/include/pc_mp_records.h")
    mcard = read("pc/src/pc_m_card.c")
    vi = read("pc/src/pc_vi.c")
    pcmain = read("pc/src/pc_main.c")
    cmake = read("pc/CMakeLists.txt")
    after = func_body(c_raw, "pc_net_game_record_after_gci_save")
    resolve_slot = func_body(c_raw, "pcnetgame_rec_resolve_slot")
    resolve = resolve_slot + func_body(c_raw, "pcnetgame_rec_store_resolve")
    wgci = func_body(mcard, "pc_save_write_gci")
    check("P path: PC_MP_RECORDS_PATH == save/mp/records.dat (sibling of card_a/card_b), no gci/GAF anywhere in the code or path",
          '#define PC_MP_RECORDS_PATH        "save/mp/records.dat"' in store_h and not re.search(r"gci|GAF", store_c, re.I)
          and not re.search(r"card_[ab]", store_c) and "save/mp" in store_h)
    check("P path: pc_card.c Card-B scan only looks inside its own card dir (get_card_dir) and the sidecar dir is not a card dir",
          'card_dir[2] = { "save/card_a", "save/card_b" }' in read("pc/src/pc_card.c") and "pc_card_scan_for_gci" in read("pc/src/pc_card.c")
          and "mp" not in re.findall(r'"save/(\w+)"', read("pc/src/pc_card.c")))
    check("P CMake: pc_mp_records.c is in PC_SOURCES (non-PC builds never see it)", "src/pc_mp_records.c" in cmake)
    check("P hook: pc_save_write_gci() calls pc_net_game_record_after_gci_save() only AFTER pc_save_write_gci_to() succeeded and the save was ready",
          "pc_save_write_gci_to(PC_GCI_PATH, PC_GCI_TMP_PATH)" in wgci and "if (ok && pc_save_ready)" in wgci
          and wgci.index("pc_save_write_gci_to(") < wgci.index("pc_net_game_record_after_gci_save(PC_GCI_PATH)"))
    check("P hook: the only caller of pc_net_game_record_after_gci_save is that Card-A hook (Card B writes go through pc_save_write_gci_to directly and never persist records)",
          len(re.findall(r"pc_net_game_record_after_gci_save\(", strip_comments(mcard))) == 1
          and not re.search(r"pc_net_game_record_after_gci_save", strip_comments(read("src/main.c")) + strip_comments(vi) + strip_comments(pcmain)))
    check("P hook: the authoritative save (periodic / early / shutdown) goes through pc_save_write_gci",
          "return pc_save_write_gci();" in func_body(mcard, "pc_save_write_authoritative"))
    store_write = func_body(c_raw, "pcnetgame_rec_store_write")
    store_build = func_body(c_raw, "pcnetgame_rec_store_build")
    check("P write-after-GCI: HOST only; persists only when the lineage was resolved for the town that is STILL loaded (resolved + town identity equal, NOT world-ready); busy-guarded; failure logged not fatal; unsaved markers cleared ONLY when the sidecar write succeeded / nothing to write",
          "pc_net_game_role() != PC_NETGAME_ROLE_HOST" in after and "!s_rec_resolved || !s_host_town_valid || !pcnetgame_town_equal(&cur, &s_host_town)" in after
          and "s_host_world_ready" not in after and "exit(" not in store_write + after and "abort(" not in store_write + after
          and re.search(r"if \(pcnetgame_rec_store_write\(\"after the GCI save\", gci_path\)\) \{\s*pc_net_game_record_note_saved\(\);", after) is not None
          and "records.dat write FAILED" in store_write)
    check("P write: coalesced (entries unchanged => no write); the GCI just written is flushed DURABLY (pc_mp_records_sync_file) BEFORE the sidecar save, and a failed flush SKIPS the sidecar write (rev>0 never reaches disk without a durable GCI)",
          "memcmp(nf.e, s_rec_file.e, sizeof(nf.e)) == 0" in store_write and "pc_mp_records_sync_file(gci_path) != 0" in store_write
          and store_write.index("pc_mp_records_sync_file(gci_path)") < store_write.index("pc_mp_records_save(")
          and re.search(r"pc_mp_records_sync_file\(gci_path\) != 0\) \{[^}]*return 0;", store_write, re.S) is not None
          and "pc_net_game_record_after_gci_save(PC_GCI_PATH)" in mcard)
    check("P rev>0 is persisted ONLY through pcnetgame_rec_store_write: pc_mp_records_save has exactly one call site in pc_net_game.c; its callers are the post-GCI-save hook and the immediate UNTRUSTED write",
          len(re.findall(r"pc_mp_records_save\(", c)) == 1 and "pc_mp_records_save(" in store_write
          and len(re.findall(r"pcnetgame_rec_store_write\(", c)) == 3)
    check("P entries carry PersonalID (BE image bytes), epoch, rev, digest_at_save (FNV of the BE image just saved) and the pre-migration backup",
          "memcpy(e->pid, be, PC_MP_REC_PID_SIZE);" in store_build and "e->digest_at_save = pcnetgame_fnv1a32(be, PC_NETGAME_REC_SIZE);" in store_build
          and "memcpy(e->backup, s_rec_backup[i], PC_NETGAME_REC_SIZE);" in store_build and "e->rev = s->rev;" in store_build and "e->epoch = s->epoch;" in store_build)
    check("P MEDIUM-1: a slot with nothing of its own (rev 0 and no backup, or PersonalID no longer equal) CARRIES the stored entry unchanged; UNTRUSTED synthetic slots never displace another identity's entry; another identity's backup is never adopted",
          "*e = *old;" in store_build and store_build.count("*e = *old;") == 2
          and "(s->rev > 0 || s->backup_valid) && mPr_CheckCmpPersonalID(&s->pid, &Save_Get(private_data)[i].player_ID) == TRUE" in store_build
          and "s->untrusted && old->present && memcmp(old->pid, be, PC_MP_REC_PID_SIZE) != 0" in store_build
          and "if (pid_match && e->backup_present)" in resolve and "keep_backup" not in func_body(c_raw, "pcnetgame_rec_slot"))
    check("P HIGH-1: UNTRUSTED is written to records.dat IMMEDIATELY after the slot loop (first pass only), with no GCI flush needed; load() stays UNTRUSTED while preserved *.corrupt-* files exist (storage unit tests T13)",
          re.search(r"s_rec_resolved = 1;\s*if \(first_untrusted_pass\) \{[^}]*pcnetgame_rec_store_write\(\"UNTRUSTED decision persisted immediately\", NULL\)", resolve, re.S) is not None
          and "count_corrupt_siblings(path" in store_c and "inf->mode = PC_MP_REC_LOAD_UNTRUSTED;" in store_c.split("count_corrupt_siblings(path")[1])
    check("P LOW-b: while the last sidecar write failed/was skipped (s_rec_store_last_failed) a FIRST migration (rev 0 HELLO) is refused with BUSY; cleared by the next successful write",
          "if (slot->rev == 0 && s_rec_store_last_failed) {" in func_body(blk_raw, "pcnetgame_rec_handle_hello")
          and "PC_NETGAME_REC_ACK_BUSY" in func_body(blk_raw, "pcnetgame_rec_handle_hello").split("s_rec_store_last_failed) {")[1][:900]
          and store_write.count("s_rec_store_last_failed = 1;") == 2 and "s_rec_store_last_failed = 0;" in store_write)
    check("P latch: the coalesce branch of pcnetgame_rec_store_write clears s_rec_store_last_failed (s_rec_file mirrors the last durable write: set only by a successful load or save) so a failed write cannot stick the first-migration BUSY refusal for the whole process",
          re.search(r"memcmp\(nf\.e, s_rec_file\.e, sizeof\(nf\.e\)\) == 0\) \{[^}]*s_rec_store_last_failed = 0;[^}]*return 1;", store_write, re.S) is not None
          and len(re.findall(r"s_rec_file = nf;", c)) == 1 and "s_rec_store_mode = pc_mp_records_load(PC_MP_RECORDS_PATH, &s_rec_file, &info);" in c)
    check("P re-init: a slot whose PersonalID changes (A -> B -> A in one process) is re-derived from the stored entry via pcnetgame_rec_resolve_slot (restored rev>0, no second MIGRATE), not freshly zeroed",
          "pcnetgame_rec_resolve_slot(idx);" in func_body(c_raw, "pcnetgame_rec_slot")
          and "for (i = 0; i < PLAYER_NUM; i++) {\n        pcnetgame_rec_resolve_slot(i);" in func_body(c_raw, "pcnetgame_rec_store_resolve"))
    check("P LOW-c: UNTRUSTED stickiness is documented in the code and the reset procedure is logged by load()",
          "STICKY: UNTRUSTED residents get rev 1 permanently" in c_raw and "remove records.dat, its .bak files AND the *.corrupt-* files." in c_raw
          and "To deliberately reset" in read("pc/src/pc_mp_records.c"))
    check("P load: lineage restored ONLY when the stored PersonalID equals the save's AND the digest equals the loaded record; digest mismatch -> epoch re-rolled, rev kept; PersonalID mismatch -> rev 0, backup kept",
          "memcmp(e->pid, be, PC_MP_REC_PID_SIZE) == 0" in resolve and "e->digest_at_save == pcnetgame_fnv1a32(be, PC_NETGAME_REC_SIZE)" in resolve
          and "s->epoch = e->epoch;" in resolve and "s->epoch = pcnetgame_rec_rand32();" in resolve and "s->rev = e->rev;" in resolve
          and "slot reassigned" in resolve_raw_text(c_raw) and "memcpy(s_rec_backup[i], e->backup, PC_NETGAME_REC_SIZE);" in resolve)
    check("P load: UNTRUSTED (all generations unreadable) -> every existing resident rev 1 + fresh epoch (no migration), loud log; a MISSING file leaves rev 0",
          "s_rec_untrusted = (s_rec_store_mode == PC_MP_REC_LOAD_UNTRUSTED);" in resolve and "if (s_rec_untrusted) {\n        s->rev = 1;" in resolve
          and "UNTRUSTED MODE" in c_raw and "DISABLED" in c_raw)
    check("P load timing: resolved when the host world first becomes ready (BEFORE s_host_world_ready = 1, i.e. before any client reaches READY) and lazily from pcnetgame_rec_slot",
          re.search(r"pcnetgame_rec_store_resolve\(\);[^\n]*\n\s*s_host_world_ready = 1;", c_raw) is not None
          and "pcnetgame_rec_store_resolve();" in func_body(c_raw, "pcnetgame_rec_slot") and "s_rec_resolved = 0;" in func_body(c_raw, "pcnetgame_rec_on_town_changed"))
    check("P load: the file is read once per process (s_rec_store_loaded) and host_session stays random per process (rolled by pcnetgame_rec_rand32, never persisted)",
          "if (!s_rec_store_loaded)" in resolve and "pcnetgame_rec_rand32()" in resolve and "host_session" not in store_c and "host_session" not in store_h.split("*/", 1)[1])
    check("P backup: a migrate import fills s_rec_backup/backup_valid (already in the record block) and a slot re-init drops it (a backup belongs to one PersonalID)",
          "slot->backup_valid = 1;" in c and "A backup belongs to ONE PersonalID" in c_raw and "if (pid_match && e->backup_present)" in resolve_slot)
    check("P storage: unreadable files are moved aside (never removed) in load and at save time; tmp-only removals; bak2 dropped only after the current file validated",
          "move_aside(gp, moved, sizeof(moved))" in store_c and "move_aside(path, moved, sizeof(moved))" in store_c
          and all(("tmp" in ln or "b2" in ln) for ln in store_c.splitlines() if re.search(r"\bremove\(", ln)))
    check("P storage: tmp write -> flush + commit (_commit/fsync) -> rotate -> atomic replace (MoveFileExA REPLACE_EXISTING / rename) -> restore .bak1 on failure",
          "commit_file(fp)" in store_c and "_commit(" in store_c and "fsync(" in store_c and "MOVEFILE_REPLACE_EXISTING" in store_c
          and "restored previous generation from .bak1" in store_c)
    check("P storage: the test-only fault hook is never referenced by game code",
          "pc_mp_records_set_fault" not in c_raw and "pc_mp_records_set_fault" not in mcard and "PCMpRecFault" not in c_raw)
    check("P early save (Q5): pc_vi.c consumes pc_net_game_record_early_save_due() (= early_save_requested() AND host world ready) under the same HOST + pcfa_save_ready gates, at most once per PC_EARLY_SAVE_MIN_GAP_MS (>= 5000), through pc_save_write_authoritative",
          "pc_net_game_record_early_save_due()" in vi and "#define PC_EARLY_SAVE_MIN_GAP_MS 5000u" in vi
          and re.search(r"pc_net_game_role\(\) == PC_NETGAME_ROLE_HOST && pcfa_save_ready\(\) && l_last_save_time != 0 &&\s*pc_net_game_record_early_save_due\(\)", vi) is not None
          and "save_ok = pc_save_write_authoritative();" in vi
          and "return s_host_world_ready && pc_net_game_record_early_save_requested();" in c_raw)
    check("P early save: the request is cleared by pc_net_game_record_note_saved() (called from the after-save hook), and set only for a bound peer whose slot has unsaved accepted uploads",
          "s_rec_early_save_request = 0;" in func_body(c_raw, "pc_net_game_record_note_saved")
          and "dirty_unsaved" in func_body(c_raw, "pcnetgame_rec_note_peer_gone") and "slot->dirty_unsaved = 1;" in blk)
    check("P threading: the Windows console-ctrl and signal handlers only clear g_pc_running (no record/save/file calls); all persistence runs on the frame/main-thread paths (pc_vi.c periodic+early, src/main.c shutdown)",
          re.search(r"pc_console_ctrl_handler\(DWORD ctrl_type\) \{[^}]*?g_pc_running = 0;[^}]*?\}", pcmain, re.S) is not None
          and "pc_save" not in func_body(pcmain, "pc_signal_handler") and "pc_net_game" not in func_body(pcmain, "pc_signal_handler")
          and "pc_save_write_authoritative()" in read("src/main.c"))
    check("P vanilla layout untouched: no GCI constant/offset/format line changed by this step (hook only wraps pc_save_write_gci)",
          "GCI_FILE_DATA_SIZE   mCD_LAND_SAVE_SIZE" in mcard and "static int pc_save_write_gci_to(const char* gci_path, const char* tmp_path) {" in mcard)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
