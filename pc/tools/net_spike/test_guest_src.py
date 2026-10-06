#!/usr/bin/env python3
"""test_guest_src.py - GUESTS G1 SOURCE AUDIT (no game process).

Reads pc_net_game.c / pc_mp_guests.[ch] / pc_m_card.c / net_spike_lib.py and checks the rules the guest design rests on:
  W   wire: ids 57 / 58, exact struct sizes (42 / 20) + offsets + <= 64 / PC_NET_MAX_PAYLOAD, the frozen 32-byte IDENTITY / IDENTITY_ACK, net_spike_lib agrees,
      wire_baseline (pinned structs + selftest mutations) green
  A   ONE accessor: pcnetgame_rec_priv_ptr() is the only place that turns a record slot into a Private_c*; every `Save_Get(private_data)[expr]` of
      pc_net_game.c sits in a PINNED set of functions (a new one fails this test and needs review), each of which is resident-only BY CONSTRUCTION
      (a guarded branch, a PLAYER_NUM loop, or an early guest refusal that precedes the first subscript); the transaction handlers (TXN_COMMIT / grants /
      town services) contain no private_data / homes subscript at all; guests can never index private_data[] / homes[] (no OOB)
  B   binding: bound_valid = 1 in one place; bound_class / bound_guest_slot assigned only in process_identity (+ the per-peer reset); the slot used by
      every record gate comes from the binding (pcnetgame_peer_rec_slot / the gate), never from a message; arrays are sized for the shared slot space
  C   classification: decided only by the host in process_identity; the resident classifier + own-resident function are byte-identical to HEAD; a guest
      is admitted only for an UNKNOWN identity with a valid EXT guest claim; a guest-flagged claim on a resident is refused; the key checks (home land,
      resident / house-owner collision, IDENTITY <-> EXT consistency); the token rules (required for a known key, plain memcmp, minted by the OS CSPRNG,
      never logged); the order mint -> durable write -> ACK -> TOKEN
  D   house services: museum donation / mail send / mail take refuse a guest slot (NO_DONOR_SLOT) before any index is derived; the shop is NOT refused
  P   persistence: guests.dat only through pc_mp_guests_load/save, never .gci / card_*, written on mint (durable), by the GCI-save hook and never while
      UNTRUSTED; pc_m_card.c is unchanged (no change to the GCI path)
  K   client: IDENTITY_EXT only for a foreigner (player_no >= mPr_FOREIGNER) and before IDENTITY; IDENTITY_TOKEN persists the token at first contact and
      REFUSES a different token; record_class GUEST in BEGIN.rsv; a resident client's wire is unchanged
Tier: SOURCE AUDITED. Exit code 0 when all checks pass."""
import os
import re
import subprocess
import sys

import net_spike_lib as L
import wire_baseline

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def read(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
        return f.read().decode("utf-8", "replace").replace("\r\n", "\n")


def mask(s):
    """Source with every comment replaced by spaces (offsets preserved)."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    s = re.sub(r"/\*.*?\*/", blank, s, flags=re.S)
    return re.sub(r"//[^\n]*", blank, s)


def functions(src):
    """[(start, end, name)] of every top-level function definition (line starting at column 0, ending at a '}' at column 0)."""
    out = []
    for m in re.finditer(r"^(?:static )?[A-Za-z_][\w \*]*?\b(\w+)\([^;{]*?\)\s*\{\n", src, re.M):
        if m.group(1) in ("if", "for", "while", "switch"):
            continue
        out.append((m.start(), src.index("\n}\n", m.end()), m.group(1)))
    return out


def body(src, funcs, name):
    for a, b, n in funcs:
        if n == name:
            return src[a:b]
    return ""


def calls(src, fn):
    """Full text of every call `fn(...)` (parentheses balanced, string literals skipped)."""
    out = []
    for m in re.finditer(r"\b%s\(" % re.escape(fn), src):
        i, depth, in_str = m.end(), 1, False
        while i < len(src) and depth:
            ch = src[i]
            if in_str:
                if ch == "\\":
                    i += 1
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        out.append(src[m.start():i])
    return out


def raw_main():
    return read("pc/src/pc_main.c")


def strip_observer(text):
    """pc_m_card.c with every `/* OBSERVER-BEGIN */ ... /* OBSERVER-END */` block (the --host-observer additions) removed: what remains must be the
    pre-observer file (the audits below compare the GCI writers against HEAD on THIS text, so the observer cannot hide a change in them)."""
    return re.sub(r"/\* OBSERVER-BEGIN \*/.*?/\* OBSERVER-END \*/\n?", "", text, flags=re.S)


def head_function(name, path="pc/src/pc_net_game.c", strip=False, ref="HEAD"):
    txt = subprocess.run(["git", "-C", ROOT, "show", ref + ":" + path], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace")
    txt = txt.replace("\r\n", "\n")
    if strip:
        txt = strip_observer(txt)  # HEAD already contains the committed --host-observer blocks: compare like with like (both sides without them)
    f = functions(txt)
    return body(txt, f, name)


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    raw = read("pc/src/pc_net_game.c")
    c = mask(raw)
    funcs = functions(c)
    fb = lambda n: body(c, funcs, n)
    hdr = read("pc/include/pc_net_game.h")
    gh = read("pc/include/pc_mp_guests.h")
    gc = read("pc/src/pc_mp_guests.c")

    # ------------------------------------------------------------------------------------------------ W
    ids = dict(wire_baseline.c_message_ids(raw))
    ck("W ids: 57 = IDENTITY_EXT, 58 = IDENTITY_TOKEN, all ids contiguous 1..%d" % wire_baseline.EXPECTED_MAX_MSG_ID,
       ids.get("PC_NETGAME_MSG_IDENTITY_EXT") == 57 and ids.get("PC_NETGAME_MSG_IDENTITY_TOKEN") == 58 and wire_baseline.EXPECTED_MAX_MSG_ID == 66
       and sorted(ids.values()) == list(range(1, 67)))
    ck("W IDENTITY_EXT: exact 42-byte size assert + offsets + <= 64 and <= PC_NET_MAX_PAYLOAD; IDENTITY_TOKEN 20 bytes",
       "_Static_assert(sizeof(PCNetGameIdentityExtMsg) == 42," in raw and "offsetof(PCNetGameIdentityExtMsg, token) == 25" in raw
       and "sizeof(PCNetGameIdentityExtMsg) <= 64 && sizeof(PCNetGameIdentityExtMsg) <= PC_NET_MAX_PAYLOAD" in raw
       and "_Static_assert(sizeof(PCNetGameIdentityTokenMsg) == 20," in raw and "sizeof(PCNetGameIdentityTokenMsg) <= PC_NET_MAX_PAYLOAD" in raw)
    ck("W the frozen IDENTITY / IDENTITY_ACK stay 32 bytes with protocol_version at offset 4 (guests added NO field to them)",
       "_Static_assert(sizeof(PCNetGameIdentityMsg) == 32," in raw and "_Static_assert(sizeof(PCNetGameIdentityAckMsg) == 32," in raw
       and "offsetof(PCNetGameIdentityMsg, protocol_version) == 4" in raw)
    ck("W net_spike_lib agrees: ids, flags, formats, spec sizes, the record classes",
       (L.PC_NETGAME_MSG_IDENTITY_EXT, L.PC_NETGAME_MSG_IDENTITY_TOKEN) == (57, 58) and L.IDENTITY_EXT_SPEC.size == 42 and L.IDENTITY_TOKEN_SPEC.size == 20
       and (L.PC_NETGAME_REC_CLASS_RESIDENT, L.PC_NETGAME_REC_CLASS_GUEST) == (0, 1) and L.PC_NETGAME_GUEST_MAX == 8
       and "#define PC_NETGAME_REC_CLASS_GUEST    1u" in raw and "#define PC_NETGAME_GUEST_MAX             8" in raw)
    ck("W the guest protocol is NOT a protocol bump (v8 unreleased, extended in place): version stays %d" % wire_baseline.EXPECTED_PROTOCOL_VERSION,
       wire_baseline.header_protocol_ok(hdr))
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    ck("W wire_baseline (pins the new structs EXACTLY, everything else identical to HEAD): all %d checks green" % len(wb), wb and all(x[1] for x in wb))

    # ------------------------------------------------------------------------------------------------ A
    ck("A pcnetgame_rec_priv_ptr is defined exactly once and is the only accessor of both record spaces: resident slot < PLAYER_NUM -> private_data, "
       "guest slot < PC_NETGAME_REC_SLOTS and the entry used -> s_guest_rec", len(re.findall(r"^static Private_c\* pcnetgame_rec_priv_ptr\(", c, re.M)) == 1
       and "slot >= 0 && slot < PLAYER_NUM" in fb("pcnetgame_rec_priv_ptr") and "slot >= PLAYER_NUM && slot < PC_NETGAME_REC_SLOTS && s_guest[slot - PLAYER_NUM].used" in fb("pcnetgame_rec_priv_ptr")
       and "&s_guest_rec[slot - PLAYER_NUM]" in fb("pcnetgame_rec_priv_ptr") and c.count("&s_guest_rec[") >= 4)
    ck("A s_guest_rec[] is addressed ONLY through the accessor, the guest table lifecycle functions (install / create / rollback) and the G6.2 operator tools (remove: "
       "pc_net_game_dedicated_guest_admin) and nothing else",
       sorted({n for a, b, n in funcs if "s_guest_rec[" in c[a:b]}) == sorted(["pcnetgame_rec_priv_ptr", "pcnetgame_guest_install", "pcnetgame_guest_create", "pcnetgame_guest_rollback_create",
                                                                              "pc_net_game_dedicated_guest_admin", "pcnetgame_promote_exec"]))  # guest-first purchase: the promote console command is a thin wrapper of pcnetgame_promote_exec (the former body)
    pinned = ["pc_net_game_dedicated_members", "pcnetgame_dedicated_give_resolve",  # the dedicated console tools (host admin items / members), reviewed
              # M-D / M-E: the admission cross-check (reads the host's own residents), the resident credential table (keyed by the resident PersonalID of private_data[idx]) and the
              # resident operator commands (residents / resident-reset / resident-arm); all host-side, none reachable with a guest slot
              "pc_net_game_dedicated_resident_admin", "pc_net_game_dedicated_resident_info", "pcnetgame_promote_exec", "pcnetgame_host_promotion_handoff",  # M-F: promote (the guest record -> a new resident) + the handoff check (resident PID lookup)
               "pcnetgame_dedicated_resident_resolve", "pcnetgame_host_admission_resolve_view", "pcnetgame_rec_has_promote_entry",  # M-H: the resolver input builder (reads the host residents); M-G: the promote-entry probe (guarded idx < PLAYER_NUM)
              "pcnetgame_resident_arm_active", "pcnetgame_resident_cred_find", "pcnetgame_resident_mint", "pcnetgame_resident_mint_rollback",
              "pcnetgame_guest_key_conflict", "pcnetgame_guest_name_conflict_resident", "pcnetgame_handle_host_mail_take_txn", "pcnetgame_handle_host_mail_txn", "pcnetgame_host_mbox_tick",
              "pcnetgame_host_process_identity", "pcnetgame_host_record_tick", "pcnetgame_host_remail_tick", "pcnetgame_host_revalidate_bound_peers",
              "pcnetgame_house_handle_begin", "pcnetgame_house_process_commit",  # furniture sync: guest slot -> NOT_OWNER before the first subscript (test_house_sync_src.py)
              "pcnetgame_mail_test_poke_museum", "pcnetgame_mail_test_seed_mailbox", "pcnetgame_mail_test_seed_reply", "pcnetgame_mbox_refresh_resident",
              "pcnetgame_mbox_send", "pcnetgame_members_prune_orphans", "pcnetgame_rec_gate", "pcnetgame_rec_priv_ptr", "pcnetgame_rec_resolve_slot", "pcnetgame_rec_slot", "pcnetgame_rec_store_build",
              # guest-first purchase (reviewed): the CLIENT-side local pre-check of the house purchase reads the LOCAL town copy with loop-bounded indices (i < PLAYER_NUM)
              "pc_net_game_house_purchase_precheck",
              # reviewed now (they were already at HEAD, unpinned): the personal-data (diary) sync code walks private_data with loop-bounded indices (slot < PLAYER_NUM; the client side
              # returns -1 for a guest claim / a non-resident before the loop)
              "pcnetgame_pdata_host_tick", "pcnetgame_pdc_own_slot"]
    users = sorted({n for a, b, n in funcs if re.search(r"Save_Get\(private_data\)\[", c[a:b])})
    pinned = sorted(pinned)
    ck("A the functions that subscript Save_Get(private_data)[...] are exactly the %d reviewed ones (a new one needs a guest-safety review): %s" % (len(pinned), users),
       users == pinned and not re.search(r"Save_Get\(private_data\)\[", re.sub(r"\n}\n", "\n}\n", "".join(c[b:a2] for (a, b, _n), (a2, _b2, _n2) in zip(funcs, funcs[1:])))))
    homes_users = sorted({n for a, b, n in funcs if re.search(r"Save_Get\(homes\[", c[a:b])})
    ck("A the functions that subscript Save_Get(homes[...]) are exactly the reviewed ones (house lookup by PersonalID, mail / mailbox paths behind the guest refusal, "
       "client-side shadows, TEST-ONLY hooks): %s" % homes_users,
       homes_users == sorted(["pcnetgame_promote_exec", "pc_net_game_house_purchase_precheck", "pcnetgame_guest_key_conflict", "pcnetgame_handle_host_mail_take_txn", "pcnetgame_handle_host_mail_txn", "pcnetgame_mail_test_force_delivery",
                              "pcnetgame_mail_test_seed_mailbox", "pcnetgame_mbox_client_apply", "pcnetgame_mbox_client_tick", "pcnetgame_mbox_house_of",
                              "pcnetgame_mbox_refresh_resident", "pcnetgame_run_mail_take_test_hook", "pcnetgame_run_mail_test_hook", "pcnetgame_txn_apply_take",
                              # furniture sync (house index from a bound PersonalID, guest refused first; client shadows; TEST-ONLY host edit): see test_house_sync_src.py
                              "pcnetgame_hcl_build_pair", "pcnetgame_hcl_local_cown", "pcnetgame_hcl_push_complete", "pcnetgame_hcl_write_save", "pcnetgame_house_host_refresh",
                              # furniture sync, reviewed (guest-safety review of da9074a): live apply / reconcile plan / room track (client shadows, range-checked house index),
                              # the client reconcile apply (own house only, no guest claim)
                              "pcnetgame_hcl_live_apply", "pcnetgame_hcl_reconcile_plan", "pcnetgame_hcl_room_track", "pcnetgame_house_client_reconcile_apply",
                              "pcnetgame_house_owned", "pcnetgame_house_process_commit", "pcnetgame_house_test_host_edit"]))
    # reviewed guards of the furniture-sync / promote code that subscripts homes[] / private_data[] (a guest can never reach them with an out-of-range index)
    hcl_own = fb("pcnetgame_hcl_own_house")
    prm_idx = fb("pcnetgame_promote_parse_index")
    mc_raw = mask(read("pc/src/pc_m_card.c"))
    mc_fn = functions(mc_raw)

    def other_c_callers(fn):
        out = []
        for d_ in ("pc/src", "pc/include", "pc/tools"):
            for dp, _dn, fns in os.walk(os.path.join(ROOT, d_.replace("/", os.sep))):
                for f_ in fns:
                    if f_.endswith((".c", ".cpp", ".cc")) and f_ != "pc_net_game.c":  # headers hold declarations only
                        t_ = mask(read(os.path.relpath(os.path.join(dp, f_), ROOT).replace(os.sep, "/")))
                        if re.search(r"\b%s\(" % fn, t_):
                            out.append(f_)
        return sorted(out)
    ck("A furniture sync / promote guards: hcl_own_house returns nothing for a guest (`Now_Private == NULL || s_client_guest_claim_sent`), live_apply range-checks the house index "
       "(`h < 0 || h >= PC_NETGAME_HOUSE_NUM`), promote_parse_index accepts only a single digit 0..3 (`sel[0] >= '0' && sel[0] <= '3'`), pc_mp_promote_create range-checks the house "
       "(`house < 0 || house >= PLAYER_NUM`), and the only callers of pc_net_game_dedicated_promote( / pc_net_game_dedicated_give( outside pc_net_game.c are in pc_dedicated.c",
       "Now_Private == NULL || s_client_guest_claim_sent" in hcl_own and "h < 0 || h >= PC_NETGAME_HOUSE_NUM" in fb("pcnetgame_hcl_live_apply")
       and "sel[0] >= '0' && sel[0] <= '3'" in prm_idx and prm_idx != ""
       and "house < 0 || house >= PLAYER_NUM" in body(mc_raw, mc_fn, "pc_mp_promote_create")
       and other_c_callers("pc_net_game_dedicated_promote") == ["pc_dedicated.c"] and other_c_callers("pc_net_game_dedicated_give") == ["pc_dedicated.c"])
    for name in ("pcnetgame_handle_host_txn_commit", "pcnetgame_x3_grant", "pcnetgame_handle_host_ts_txn", "pcnetgame_rec_process_upload", "pcnetgame_rec_handle_hello",
                 "pcnetgame_rec_start_push", "pcnetgame_txn_send_applied", "pcnetgame_rec_txn_write_inventory", "pcnetgame_rec_txn_write_mail", "pcnetgame_rec_txn_idx_ok",
                 "pcnetgame_rec_merge_into_save", "pcnetgame_rec_export_be", "pcnetgame_rec_refresh_hostfields"):
        b_ = fb(name)
        ck("A %s never subscripts private_data / homes (its record comes from the accessor with a slot from the binding gate)" % name,
           b_ != "" and "private_data" not in b_.replace("pcnetgame_rec_priv_ptr", "") and "homes[" not in b_)

    def before(fn, first, then):
        b_ = fb(fn)
        return first in b_ and then in b_ and b_.index(first) < b_.index(then)

    ck("A mail send: the guest slot (idx >= PLAYER_NUM) is refused BEFORE the first private_data / homes subscript",
       before("pcnetgame_handle_host_mail_txn", "if (idx >= PLAYER_NUM) {", "Save_Get(private_data)[idx]") and before("pcnetgame_handle_host_mail_txn", "if (idx >= PLAYER_NUM) {", "Save_Get(homes[")
       and "pcnetgame_txn_reject_guest_no_house(peer, idx, slot, in," in fb("pcnetgame_handle_host_mail_txn"))
    ck("A mail take: the guest slot is refused BEFORE the first private_data / homes subscript",
       before("pcnetgame_handle_host_mail_take_txn", "if (idx >= PLAYER_NUM) {", "Save_Get(private_data)[idx]") and before("pcnetgame_handle_host_mail_take_txn", "if (idx >= PLAYER_NUM) {", "Save_Get(homes[")
       and "pcnetgame_txn_reject_guest_no_house(peer, idx, slot, in," in fb("pcnetgame_handle_host_mail_take_txn"))
    ck("A the guest refusal helper answers with the EXISTING NO_DONOR_SLOT reason, not journaled (no new wire value), via pcnetgame_txn_reject",
       "PC_NETGAME_TXN_REASON_NO_DONOR_SLOT, 0, NULL, why" in fb("pcnetgame_txn_reject_guest_no_house"))
    ck("A mailbox helpers never index s_mbox_host / private_data with a guest slot: refresh_resident (range-checked ternary), send (guard first), tick (bound_resident_idx + PLAYER_NUM guard), remail (guard)",
       "(idx >= 0 && idx < PLAYER_NUM) ? pcnetgame_mbox_house_of(&Save_Get(private_data)[idx].player_ID) : -1" in fb("pcnetgame_mbox_refresh_resident")
       and before("pcnetgame_mbox_send", "idx < 0 || idx >= PLAYER_NUM", "s_mbox_host[idx][i]")
       and before("pcnetgame_host_mbox_tick", "idx < 0 || idx >= PLAYER_NUM", "Save_Get(private_data)[idx]") and "idx = st->bound_resident_idx;" in fb("pcnetgame_host_mbox_tick")
       and before("pcnetgame_host_remail_tick", "idx < 0 || idx >= PLAYER_NUM", "Save_Get(private_data)[idx]") and "idx = st->bound_resident_idx;" in fb("pcnetgame_host_remail_tick"))
    ck("A pcnetgame_rec_slot: the guest range returns BEFORE the first private_data subscript; the resident path keeps its range check",
       before("pcnetgame_rec_slot", "idx >= PLAYER_NUM && idx < PC_NETGAME_REC_SLOTS", "Save_Get(private_data)[idx]") and "idx < 0 || idx >= PLAYER_NUM" in fb("pcnetgame_rec_slot"))
    g = fb("pcnetgame_rec_gate")
    ck("A pcnetgame_rec_gate: the GUEST branch (bound_class GUEST -> PLAYER_NUM + guest slot, entry used, key equals the bind-time key) returns before the resident branch "
       "(idx = bound_resident_idx, own-resident test, bind-time PersonalID re-check) -- the resident gate text is unchanged",
       "st->bound_class == (uint8_t)PC_NETGAME_REC_CLASS_GUEST" in g and g.index("return idx;") < g.index("idx = st->bound_resident_idx;")
       and "memcmp(&s_guest[idx - PLAYER_NUM].key, &st->bound_pid, sizeof(st->bound_pid)) != 0" in g and "pcnetgame_host_own_resident_idx()" in g
       and "mPr_CheckCmpPersonalID(&st->bound_pid, &Save_Get(private_data)[idx].player_ID)" in g)
    ck("A every record / txn / grant / town-service handler gets its slot from pcnetgame_rec_gate() (never from a message): 6 gate calls outside the gate itself + the record handlers",
       len(re.findall(r"pcnetgame_rec_gate\(peer", c)) >= 10 and "pcnetgame_rec_gate(peer, 0, 0)" in fb("pcnetgame_handle_host_txn_commit")
       and "pcnetgame_rec_gate(peer, 0, 0)" in fb("pcnetgame_x3_grant") and "pcnetgame_rec_gate(peer, 0, 0)" in fb("pcnetgame_handle_host_ts_txn"))
    ck("A the museum donation refuses a guest slot (idx outside PLAYER_NUM -> NO_DONOR_SLOT) and Common player_no is only ever set to a resident slot",
       "if (idx < 0 || idx >= PLAYER_NUM) {" in fb("pcnetgame_handle_host_ts_txn") and "NO_DONOR_SLOT" in fb("pcnetgame_handle_host_ts_txn")
       and fb("pcnetgame_handle_host_ts_txn").index("idx >= PLAYER_NUM") < fb("pcnetgame_handle_host_ts_txn").index("Common_Get(player_no) = (u8)idx"))
    ts = fb("pcnetgame_handle_host_ts_txn")
    shop = ts[ts.index("if (is_shop) {"):ts.index("} else if (is_donate) {")]
    ck("D the SHOP is NOT refused for a guest (no PLAYER_NUM test in the shop branch): guests are full participants of buying / selling", "PLAYER_NUM" not in shop)
    ck("D guest slots in the txn index space: s_rec_slot / s_rec_backup / s_txn_res are sized PC_NETGAME_REC_SLOTS = PLAYER_NUM + PC_NETGAME_GUEST_MAX; the resident-only "
       "arrays (s_mbox_host, s_remail_day) stay PLAYER_NUM",
       "#define PC_NETGAME_REC_SLOTS (PLAYER_NUM + PC_NETGAME_GUEST_MAX)" in raw and "static PCNetGameRecSlot s_rec_slot[PC_NETGAME_REC_SLOTS];" in raw
       and "static uint8_t  s_rec_backup[PC_NETGAME_REC_SLOTS][PC_NETGAME_REC_SIZE];" in raw and "static PCNetGameTxnResident s_txn_res[PC_NETGAME_REC_SLOTS];" in raw
       and "s_mbox_host[PLAYER_NUM][PC_NETGAME_MBOX_SLOTS]" in raw and "s_remail_day[PLAYER_NUM]" in raw
       and "else if (idx < PC_NETGAME_REC_SLOTS) {" in fb("pcnetgame_txn_journal_clear"))

    # ------------------------------------------------------------------------------------------------ B
    ck("B bound_valid is assigned 1 in exactly one place (process_identity); bound_class / bound_guest_slot are assigned only there (+ the per-peer reset)",
       len(re.findall(r"bound_valid = 1", c)) == 1 and "bound_valid = 1" in fb("pcnetgame_host_process_identity")
       and sorted({n for a, b, n in funcs if re.search(r"bound_class = ", c[a:b])}) == ["pcnetgame_host_process_identity"]
       and sorted({n for a, b, n in funcs if re.search(r"bound_guest_slot = ", c[a:b])}) == ["pcnetgame_host_process_identity", "pcnetgame_reset_all_host_peer_state"]
       and "s_host_peer[peer].bound_guest_slot = -1;" in fb("pcnetgame_reset_all_host_peer_state"))
    slotfn = fb("pcnetgame_peer_rec_slot")
    ck("B pcnetgame_peer_rec_slot derives the slot ONLY from the binding (bound_valid, bound_class, bound_guest_slot < PC_NETGAME_GUEST_MAX / bound_resident_idx < PLAYER_NUM), never a message",
       "!st->bound_valid" in slotfn and "st->bound_guest_slot >= 0 && st->bound_guest_slot < PC_NETGAME_GUEST_MAX" in slotfn
       and "st->bound_resident_idx >= 0 && st->bound_resident_idx < PLAYER_NUM" in slotfn and "in->" not in slotfn)
    ck("B PLAYER_NUM + <guest slot> is only computed from the binding (peer_rec_slot, note_peer_gone)",
       sorted({n for a, b, n in funcs if re.search(r"PLAYER_NUM \+ st->bound_guest_slot", c[a:b])}) == ["pcnetgame_peer_rec_slot", "pcnetgame_rec_note_peer_gone"])
    ck("B the host's own resident is never a guest slot and a resident peer never matches a guest (bound_to_resident tests the class)",
       "bound_class == (uint8_t)PC_NETGAME_REC_CLASS_RESIDENT && s_host_peer[j].bound_resident_idx == idx" in fb("pcnetgame_host_peer_bound_to_resident")
       and "bound_class == (uint8_t)PC_NETGAME_REC_CLASS_GUEST && s_host_peer[j].bound_guest_slot == gslot" in fb("pcnetgame_host_peer_bound_to_guest"))
    ck("B friendship / MAIL_REQUEST use the bound key through pcnetgame_host_bound_personal_id (which works through the slot helper): a guest's key is its home PersonalID",
       "pcnetgame_peer_rec_slot(peer)" in fb("pcnetgame_host_bound_personal_id") and "mPr_CopyPersonalID(out, &s_host_peer[peer].bound_pid)" in fb("pcnetgame_host_bound_personal_id")
       and "pcnetgame_host_bound_personal_id(peer, &bound_pid)" in fb("pcnetgame_handle_host_mail_request") and "pcnetgame_host_bound_personal_id(peer, &pid)" in fb("pcnetgame_handle_host_friendship_request")
       and "ready_" not in fb("pcnetgame_handle_host_mail_request"))
    ck("B re-validation closes a guest whose entry vanished or whose key became a resident / house owner of the host save",
       "bound guest table entry is gone or changed" in fb("pcnetgame_host_revalidate_bound_peers") and "pcnetgame_guest_key_conflict(&st->bound_pid)" in fb("pcnetgame_host_revalidate_bound_peers"))

    # ------------------------------------------------------------------------------------------------ C
    ws = lambda t: re.sub(r"\s+", "", t)
    own_head = mask(head_function("pcnetgame_host_own_resident_idx"))
    # HEAD (>= the committed --host-observer feature) ALREADY contains the observer's early `return -1`: the worktree function must equal HEAD exactly
    own_expect = own_head if "if (pc_host_observer_active()) {" in own_head else own_head.replace("    int i;\n", "    int i;\n    if (pc_host_observer_active()) {\n        return -1;\n    }\n", 1)
    ck("C the RESIDENT classifier is byte-identical to HEAD and the own-resident function is HEAD plus EXACTLY the --host-observer early `return -1` "
       "(residents keep priority, the guest path changed neither; the observer plays no resident)",
       fb("pcnetgame_host_classify_identity") != "" and mask(head_function("pcnetgame_host_classify_identity")).strip() == fb("pcnetgame_host_classify_identity").strip()
       and "if (pc_host_observer_active()) {" in own_expect and ws(own_expect) == ws(fb("pcnetgame_host_own_resident_idx")))
    # M-D refactor: the admission DECISION (has_save / town, classify, guest / resident rules) moved from process_identity into pcnetgame_host_admission_decide(), which the
    # caller process_identity runs first; the concatenation keeps the same source order (decide, then the rest of process_identity), so the order checks keep their strictness.
    pi = fb("pcnetgame_host_admission_decide") + "\n" + fb("pcnetgame_host_process_identity")
    ck("C the class is decided in process_identity AFTER the unchanged NO_SAVE / LAND_MISMATCH checks and BEFORE the ACK: classify -> (UNKNOWN + EXT guest claim -> guest_check) / "
       "(RESIDENT + guest claim -> refuse) -> own-resident -> duplicate/park -> create -> reset -> ACK -> TOKEN -> READY",
       0 < pi.index("PC_NETGAME_REJECT_NO_SAVE") < pi.index("PC_NETGAME_REJECT_LAND_MISMATCH") < pi.index("pcnetgame_host_classify_identity(") < pi.index("pcnetgame_host_guest_check(")
       < pi.index("sent a GUEST claim but its IDENTITY matches resident") < pi.index("claimed resident is the host's own resident") < pi.index("pcnetgame_guest_create(")
       < pi.index("pcnetgame_reset_all_host_peer_state(peer);") < pi.index("PC_NETGAME_MSG_IDENTITY_ACK") < pi.index("PC_NETGAME_MSG_IDENTITY_TOKEN") < pi.index("PC_NETGAME_LINK_READY"))
    ck("C a guest is admitted ONLY for an UNKNOWN class with the EXT guest flag: the guest_check call sits in the `view.kind != PC_MP_ADMIT_RESIDENT` branch (M-H: the membership resolver decides, classify is the equivalence check); the resident branch refuses a guest claim",
       re.search(r"if \(view\.kind != PC_MP_ADMIT_RESIDENT\) \{.*?if \(!ext_guest\) \{[^}]*claimed identity matches no resident record of this town\", 1\);\s*return;\s*\}[^}]*pcnetgame_host_guest_check\(", pi, re.S)
       and re.search(r"resident_idx = view\.res_index;\s*if \(ext_guest\) \{[^}]*pcnetgame_host_admission_refuse_identity_text\(a, \"guest-flagged claim matches a resident of this town", pi, re.S))
    ext_readers = ["pcnetgame_handle_host_identity_ext", "pcnetgame_host_admission_decide", "pcnetgame_host_guest_check", "pcnetgame_host_process_identity",
                   "pcnetgame_host_promotion_handoff", "pcnetgame_host_resident_credential_check", "pcnetgame_townsrv_handle_req"]
    pr_ext = re.findall(r"ext->\w+", fb("pcnetgame_host_promotion_handoff"))
    cred = fb("pcnetgame_host_resident_credential_check")
    ck("C the EXT claim is only read by the EXT handler (cache), the admission decision, process_identity, guest_check (key / token), the promotion handoff (token only), the resident "
       "credential check (token only, after the `!ext_res` early exit) and the town-service request handler (no ext-> / .ext at all); the claimed player_no is still never used by ANY of the 7 readers",
       sorted({n for a, b, n in funcs if re.search(r"\bext_valid\b|\.ext\b|->ext\b|\bext->", c[a:b])}) == sorted(ext_readers)
       and not [n for n in ext_readers if "player_no" in fb(n)]
       and fb("pcnetgame_townsrv_handle_req") != "" and not re.search(r"\bext->|\.ext\b", fb("pcnetgame_townsrv_handle_req"))
       and pr_ext and set(pr_ext) <= {"ext->token_present", "ext->token"}
       and "if (!ext_res) {" in cred and "ext->token" in cred and cred.index("if (!ext_res) {") < cred.index("ext->token"))
    gcheck = fb("pcnetgame_host_guest_check")
    order = ["in->player_id != ext->home_player_id", "pcnetgame_guest_key_valid(key)", "ext->home_land_id == s_host_town.land_id", "pcnetgame_guest_key_conflict(key)",
             "s_guest_untrusted", "pcnetgame_guest_find(key)", "ext->token_present && memcmp(ext->token, s_guest[g].token, PC_NETGAME_GUEST_TOKEN_LEN) == 0",
             "pcnetgame_guest_entry_disposable(g)", "known guest key presented a WRONG token", "presented WITHOUT a token"]
    ck("C guest_check order: IDENTITY <-> EXT consistency, key validity, home land != town land, resident / house-owner collision, UNTRUSTED lock, (town, key) lookup, token compared "
       "(plain memcmp), only then the M3 re-mint exception for a disposable entry, else the WRONG / WITHOUT token refusal",
       all(o in gcheck for o in order) and [gcheck.index(o) for o in order] == sorted(gcheck.index(o) for o in order))
    kc = fb("pcnetgame_guest_key_conflict")
    ck("C a guest key may equal NO private_data PersonalID (even an away resident) and NO homes[].ownerID (byte-wise memcmp of the whole 20-byte PersonalID)",
       "memcmp(p, key, sizeof(*key)) == 0" in kc and kc.count("memcmp(p, key, sizeof(*key)) == 0") == 2 and "private_data" in kc and "ownerID" in kc and "i < PLAYER_NUM" in kc and "i < mHS_HOUSE_NUM" in kc)
    tf = fb("pcnetgame_guest_token_fresh")
    ck("C every token (first contact AND re-mint) comes from ONE helper: the OS CSPRNG (pc_mp_guests_random_bytes), a zero token is an RNG failure; create / re-mint call it BEFORE any state "
       "changes and make the entry DURABLE (guests.dat write) before returning",
       "pc_mp_guests_random_bytes(token_out, PC_NETGAME_GUEST_TOKEN_LEN)" in tf and "return !allz;" in tf and "return -2;" in fb("pcnetgame_guest_create")
       and fb("pcnetgame_guest_create").index("pcnetgame_guest_token_fresh(token_out)") < fb("pcnetgame_guest_create").index("pcnetgame_guest_store_write(\"new guest token minted\")")
       and fb("pcnetgame_guest_remint").index("pcnetgame_guest_token_fresh(token_out)") < fb("pcnetgame_guest_remint").index("pcnetgame_guest_store_write(\"unconfirmed guest re-minted\")")
       and not re.search(r"\brand\(|\bsrand\(|RANDOM\(|SDL_GetPerformanceCounter", fb("pcnetgame_guest_create") + fb("pcnetgame_guest_remint") + tf))
    guest_fns = ["pcnetgame_guest_install", "pcnetgame_guest_store_load", "pcnetgame_guest_store_build", "pcnetgame_guest_store_write", "pcnetgame_guest_create",
                 "pcnetgame_guest_rollback_create", "pcnetgame_host_guest_check", "pcnetgame_handle_host_identity_ext", "pcnetgame_host_process_identity",
                 "pcnetgame_guest_token_fresh", "pcnetgame_guest_remint", "pcnetgame_guest_remint_rollback", "pcnetgame_guest_confirm_on_record_step",
                 "pcnetgame_client_build_ext", "pcnetgame_handle_client_identity_token"]
    # the token bytes never reach printf: no printf argument names a token buffer (the strings only mention the WORD token)
    blk = "".join(fb(n) for n in guest_fns)
    printf_calls = calls(blk, "printf")
    ck("C the token never reaches a log line: none of %d printf calls in the guest code has a token buffer / byte as an argument" % len(printf_calls),
       printf_calls and not [p for p in printf_calls if re.search(r"(?:->|\.)token\b|\btoken_out\b|\bguest_token\b|s_guest\[[^\]]*\]\.token|\bm\.token\b", re.sub(r'"(?:[^"\\]|\\.)*"', '""', p))])
    ck("C the first-contact entry is rolled back (and the removal made durable) when the ACK / TOKEN could not be queued: the real guest never got the token",
       pi.count("pcnetgame_guest_rollback_create(guest_slot)") == 2 and "guest_new" in pi)
    ck("C the EXT handler caches only a well-formed claim before IDENTITY: wrong size, reserved / unknown flag bits, BOTH the GUEST and the RESIDENT flag, token bytes without token_present, a second claim and a late claim are ignored",
       all(x in fb("pcnetgame_handle_host_identity_ext") for x in ("s_host_peer_link[peer] != PC_NETGAME_LINK_HANDSHAKE || st->identity_pending", "st->ext_valid", "size != sizeof(m)",
                                                                  "(m.flags & ~(PC_NETGAME_IDEXT_FLAG_GUEST | PC_NETGAME_IDEXT_FLAG_RESIDENT)) != 0 || m._reserved0 != 0 || m._reserved1 != 0 || m.token_present > 1",
                                                                  "(PC_NETGAME_IDEXT_FLAG_GUEST | PC_NETGAME_IDEXT_FLAG_RESIDENT)) == (PC_NETGAME_IDEXT_FLAG_GUEST | PC_NETGAME_IDEXT_FLAG_RESIDENT)",
                                                                  "both the GUEST and the RESIDENT claim flags are set",
                                                                  "token bytes present without token_present")))
    ck("C a client may NOT originate IDENTITY_TOKEN: the host drops it before any handler", "data[0] == (uint8_t)PC_NETGAME_MSG_IDENTITY_TOKEN" in fb("pcnetgame_handle_host_data")
       and fb("pcnetgame_handle_host_data").index("PC_NETGAME_MSG_IDENTITY_TOKEN") < fb("pcnetgame_handle_host_data").index("pcnetgame_handle_host_record(peer"))
    ck("C the duplicate / stale-session rules of Stage 1B apply to a guest key (bound_to_guest -> park -> evict a silent one / refuse after 6000 ms)",
       "pcnetgame_host_peer_bound_to_guest(guest_slot, peer)" in pi and "pc_net_evict(" in pi and pi.count("pc_net_evict(") == 1 and "PC_NETGAME_DUP_PARK_MAX_MS" in pi)

    # ------------------------------------------------------------------------------------------------ P
    ck("P guests.dat is touched only through pc_mp_guests_load / pc_mp_guests_save (one call site each), no fopen / .gci / card_ path in pc_net_game.c's guest code",
       c.count("pc_mp_guests_load(") == 1 and c.count("pc_mp_guests_save(") == 1 and not re.search(r"\bfopen\b|card_a|card_b", blk) and ".gci" not in blk
       and '#define PC_MP_GUESTS_PATH         "save/mp/guests.dat"' in gh)
    ck("P guests.dat is written (1) when a token is minted / re-minted (durable, before the token is sent), (2) when an entry is rolled back, (3) when an entry is confirmed, (4) by the "
       "GCI-save hook, (5) by the G6.2 operator removal (pc_net_game_dedicated_guest_admin, after a backup) -- and never while UNTRUSTED",
       sorted({n for a, b, n in funcs if "pcnetgame_guest_store_write(" in c[a:b] and n != "pcnetgame_guest_store_write"})
       == sorted(["pcnetgame_guest_create", "pcnetgame_guest_rollback_create", "pcnetgame_guest_remint", "pcnetgame_guest_remint_rollback",
                  "pcnetgame_guest_confirm_on_record_step", "pc_net_game_record_after_gci_save", "pc_net_game_dedicated_guest_admin",
                  "pc_net_game_dedicated_give", "pcnetgame_promote_exec"]))
    give = fb("pc_net_game_dedicated_give")
    prom = fb("pcnetgame_promote_exec")  # the console command pc_net_game_dedicated_promote is a thin wrapper of it
    pord = ["s_guest_untrusted", "pc_mp_guests_backup_file(PC_MP_GUESTS_PATH", "pc_mp_promote_create("]
    pord2 = ["pcnetgame_members_commit(&nf", "pc_save_write_authoritative()", 'pcnetgame_guest_store_write("guest promoted to a resident")']
    ck("P the two further guests.dat writers are guarded: dedicated_give refuses a guest while UNTRUSTED (`if (is_guest && s_guest_untrusted) {`) BEFORE the inventory write and its failure path "
       "says the gift was rolled back; dedicated_promote refuses while UNTRUSTED, THEN backs guests.dat up, THEN creates the resident, and commits in the order members.dat -> authoritative "
       "save -> guest store write",
       "if (is_guest && s_guest_untrusted) {" in give and "pcnetgame_rec_txn_write_inventory(" in give and give.index("if (is_guest && s_guest_untrusted) {") < give.index("pcnetgame_rec_txn_write_inventory(")
       and "gift was rolled back" in raw
       and all(x in prom for x in pord + pord2) and [prom.index(x) for x in pord] == sorted(prom.index(x) for x in pord)
       and [prom.index(x) for x in pord2] == sorted(prom.index(x) for x in pord2))
    ck("P the store write refuses while UNTRUSTED (a write would lift the operator lock) and while nothing was loaded; the epoch alone never forces a rewrite; failures are logged, never fatal",
       "if (!s_guest_store_loaded || s_guest_untrusted) {" in fb("pcnetgame_guest_store_write") and "cmp.e[g].epoch = s_guest_file.e[g].epoch;" in fb("pcnetgame_guest_store_write")
       and "return 0;" in fb("pcnetgame_guest_store_write") and "exit(" not in fb("pcnetgame_guest_store_write") and "abort(" not in fb("pcnetgame_guest_store_write"))
    ck("P the hook writes the guest table FIRST (independent of the town / resident lineage) in the same GCI-save hook as the resident sidecar; the guest table is loaded with the host world",
       fb("pc_net_game_record_after_gci_save").index("pcnetgame_guest_store_write(\"after the GCI save\")") < fb("pc_net_game_record_after_gci_save").index("pcnetgame_rec_store_write(")
       and "pcnetgame_guest_store_load();" in fb("pcnetgame_rec_store_resolve").split("if (s_rec_resolved)")[0])
    ck("P an accepted guest change raises the same early-save request as a resident (note_peer_gone maps the guest slot) and the resident note_saved never clears a guest's dirty marker",
       "slot = st->bound_class == (uint8_t)PC_NETGAME_REC_CLASS_GUEST" in fb("pcnetgame_rec_note_peer_gone") and "for (i = 0; i < PLAYER_NUM; i++) {" in fb("pc_net_game_record_note_saved"))
    # PINNED reviewed baseline: da9074a (the HEAD the Opus guest-safety review audited). The writers, pc_card.c and pc_save_bswap.c must equal that commit EXACTLY.
    # Pre-session equivalence (the baseline vs 0c9bc72, the last commit before this work) was reviewed BY HAND: the writers are identical except pc_gci_path() / pc_gci_tmp_path()
    # and s_pc_town_gen++ in pc_save_write_gci.
    BASELINE = "a74740b"  # advanced from da9074a after review: M-G added ONLY an early return in pc_save_write_gci (skip saving when the loaded town is a SANITIZED client cache) = strictly fewer writes
    d = subprocess.run(["git", "-C", ROOT, "diff", "--stat", BASELINE, "--", "pc/src/pc_card.c"], capture_output=True, text=True).stdout.strip()
    d_bsw = subprocess.run(["git", "-C", ROOT, "diff", "--stat", BASELINE, "--", "pc/src/pc_save_bswap.c"], capture_output=True, text=True).stdout.strip()
    bsw_txt = read("pc/src/pc_save_bswap.c")
    d_bsw_ok = d_bsw == "" and bsw_txt.count("void pc_save_bswap_home(mHm_hs_c* home, pc_bswap_dir_t dir) {") == 1
    cur_mc = mask(strip_observer(read("pc/src/pc_m_card.c")))  # the --host-observer marker blocks removed (see strip_observer)
    cur_f = functions(cur_mc)
    writers = ("pc_save_write_gci_to", "pc_save_write_gci", "pc_save_rotate_backups", "pc_save_write_authoritative", "mCD_SaveHome_bg")
    same = {n: (mask(head_function(n, "pc/src/pc_m_card.c", True, BASELINE)).strip() == body(cur_mc, cur_f, n).strip() != "") for n in writers}
    ck("P the GCI writer functions (%s), the Card-B scan (pc_card.c) and the byte-swap code (pc_save_bswap.c, pc_save_bswap_home defined exactly once) are UNCHANGED vs the reviewed baseline %s: guests add no file to the vanilla save path" % (sorted(same), BASELINE),
       d == "" and d_bsw_ok and all(same.values()))
    ck("P pc_mp_guests.c is in the build (CMakeLists) and the path / format are documented", "pc_mp_guests.c" in read("pc/CMakeLists.txt") and "ACMPGST" in gh and "UNTRUSTED" in gh)

    # ------------------------------------------------------------------------------------------------ K
    tk = fb("pcnetgame_client_tick")
    ck("K the client sends IDENTITY_EXT only when it plays a foreigner (Common player_no >= mPr_FOREIGNER), once per connection, BEFORE the IDENTITY; a resident client takes no new branch",
       "(int)Common_Get(player_no) >= (int)mPr_FOREIGNER" in fb("pcnetgame_client_is_guest_player") and "pcnetgame_client_is_guest_player() && !s_client_guest_claim_sent" in tk
       and tk.index("pcnetgame_client_build_ext(") < tk.index("pcnetgame_build_identity_msg(&msg") and "s_client_guest_claim_sent = 1;" in tk
       and "s_client_guest_claim_sent = 0;" in fb("pcnetgame_reset_client_session_state"))
    ck("K the resident wire is unchanged: pcnetgame_build_identity_msg is byte-identical to HEAD",
       mask(head_function("pcnetgame_build_identity_msg")).strip() == fb("pcnetgame_build_identity_msg").strip())
    it = fb("pcnetgame_handle_client_identity_token")
    ck("K M2: at most ONE IDENTITY_TOKEN is accepted per connection (s_client_token_received, reset with the per-connection claim state), only after our own EXT; the shutdown on a "
       "DIFFERENT token happens ONLY inside the `token_present` branch (a token was PRESENTED); a token from a host this client holds NO token for is stored as a first contact",
       "if (s_client_token_received) {" in it and it.index("!s_client_guest_claim_sent") < it.index("if (s_client_token_received) {") < it.index("s_client_token_received = 1;")
       < it.index("if (s_client_ext_sent.token_present) {") < it.index("pcnetgame_client_refused(") < it.index("pc_mp_gtoken_put(&s_client_gtk, &e)")
       and it.count("pcnetgame_client_refused(") == 1 and fb("pcnetgame_reset_client_session_state").count("s_client_token_received = 0;") == 1
       and re.search(r"if \(s_client_ext_sent\.token_present\) \{\s*if \(memcmp\(m\.token, s_client_ext_sent\.token, PC_NETGAME_GUEST_TOKEN_LEN\) != 0\) \{[^}]*pcnetgame_client_refused\(\"guest token mismatch\"\);\s*return;\s*\}", it, re.S))
    ck("K IDENTITY_TOKEN is honoured only after a guest claim; a DIFFERENT token than the one presented REFUSES the host (shutdown, with reset instructions); first contact persists it "
       "(pc_mp_gtoken_put + save); a failed save is loud",
       "!s_client_guest_claim_sent" in it and "memcmp(m.token, s_client_ext_sent.token, PC_NETGAME_GUEST_TOKEN_LEN) != 0" in it and "pcnetgame_client_refused(" in it
       and "pc_mp_gtoken_put(&s_client_gtk, &e)" in it and "pc_mp_gtoken_save(pc_guest_token_path(), &s_client_gtk)" in it and "could NOT save the guest token" in it
       and "To start over delete" in it)
    ck("K the token file is looked up by (host TOWN identity, home PersonalID) BEFORE the ACK names the host; it is client metadata, never a GCI",
       "pc_mp_gtoken_find(&s_client_gtk, town->land_name, town->land_id, town->terrain_hash, home_be)" in fb("pcnetgame_client_build_ext_ex") and ".gci" not in fb("pcnetgame_client_build_ext_ex")
       and '#define PC_MP_GUEST_TOKEN_PATH    "save/mp/guest_token.dat"' in gh)
    ck("K record_class GUEST is stamped on the client's BEGIN only after a guest claim; the host stamps its pushes to a guest",
       "b.rsv = s_client_guest_claim_sent ? (uint8_t)PC_NETGAME_REC_CLASS_GUEST : (uint8_t)PC_NETGAME_REC_CLASS_RESIDENT;" in fb("pcnetgame_crec_pump_upload")
       and "b.rsv = st->bound_class == (uint8_t)PC_NETGAME_REC_CLASS_GUEST" in fb("pcnetgame_rec_pump_push"))
    # ------------------------------------------------------------------------------------------------ M1 / M3 / L3 / L4 (review follow-up)
    gdef = re.search(r"typedef struct PCNetGameGuest \{.*?\} PCNetGameGuest;", c, re.S)
    gdef = gdef.group(0) if gdef else ""
    ck("M1 the HOST table is keyed by (host town identity, guest key): every entry carries its town, pcnetgame_guest_find() matches the CURRENT town only (entries of other towns are kept but "
       "inactive), create stamps the town, guests.dat v2 stores it per entry",
       "PCNetGameTownIdentity town;" in gdef and "pcnetgame_town_equal(&s_guest[g].town, &s_host_town)" in fb("pcnetgame_guest_find")
       and "s_guest[g].town = s_host_town;" in fb("pcnetgame_guest_create") and "memcpy(e->town_land_name, s_guest[g].town.land_name, PC_NETGAME_LAND_LEN);" in fb("pcnetgame_guest_store_build")
       and "s_guest[g].town.land_id = fe->town_land_id;" in fb("pcnetgame_guest_install") and "#define PC_MP_GUEST_VERSION       2u" in gh
       and sorted({n for a, b, n in funcs if "pcnetgame_guest_find(" in c[a:b] and n != "pcnetgame_guest_find"}) == ["pcnetgame_host_guest_check"])
    ck("L4 the re-validation re-applies the town rule after a town change: the bound entry must still be THIS town's and its key's home land must not be this town's land",
       "!pcnetgame_town_equal(&s_guest[g].town, &s_host_town)" in fb("pcnetgame_host_revalidate_bound_peers") and "st->bound_pid.land_id == s_host_town.land_id" in fb("pcnetgame_host_revalidate_bound_peers")
       and "memcmp(st->bound_pid.land_name, s_host_town.land_name, PC_NETGAME_LAND_LEN) == 0" in fb("pcnetgame_host_revalidate_bound_peers"))
    dis = fb("pcnetgame_guest_entry_disposable")
    ck("M3 an entry is DISPOSABLE (re-mint / eviction) only if UNCONFIRMED, holds no accepted data (rev 0) and no peer is bound to it; confirmed entries are never touched",
       "!s_guest[g].confirmed" in dis and "s_rec_slot[PLAYER_NUM + g].rev == 0" in dis and "pcnetgame_host_peer_bound_to_guest(g, (PCNetPeerId)-1) < 0" in dis)
    cr = fb("pcnetgame_guest_create")
    ck("M3 a full table evicts ONLY a disposable entry, the OLDEST by age (nothing disposable -> -1 table full); the eviction is logged and undone if guests.dat cannot be written",
       "pcnetgame_guest_entry_disposable(g)" in cr and "(int32_t)(s_guest[g].age - s_guest[evict].age) < 0" in cr and "if (evict < 0) {" in cr and "return -1;" in cr
       and "evicting the OLDEST unconfirmed data-less idle entry" in cr and "s_guest[g] = old_g;" in cr)
    ck("M3 re-mint is reachable only from guest_check's disposable branch (mode 2) and only through the same rate limiter + durable write + delivery rollback as a first contact",
       "pcnetgame_guest_entry_disposable(g)" in fb("pcnetgame_host_guest_check") and "*out_mode = 2;" in fb("pcnetgame_host_guest_check")
       and sorted({n for a, b, n in funcs if "pcnetgame_guest_remint(" in c[a:b] and n != "pcnetgame_guest_remint"}) == ["pcnetgame_host_process_identity"]
       and pi.index("pcnetgame_guest_mint_allowed(") < pi.index("pcnetgame_guest_remint(") < pi.index("pcnetgame_guest_create(") and pi.count("pcnetgame_guest_remint_rollback(guest_slot, guest_old_token)") == 2)
    ck("M3 per-address first-contact limit: a token ISSUANCE (create or re-mint) is refused when the peer's IPv4 address (pc_net_peer_ip) already got 3 tokens within 60 s; bounded memory (ring of 16); "
       "a token-presenting known guest never goes through it",
       "if (guest_mode != 1) {" in pi and "pc_net_peer_ip(peer)" in pi and "#define PC_NETGAME_GUEST_MINT_MAX        3" in c and "#define PC_NETGAME_GUEST_MINT_WINDOW_MS  60000u" in c
       and "#define PC_NETGAME_GUEST_MINT_LOG        16" in c and "return n < PC_NETGAME_GUEST_MINT_MAX;" in fb("pcnetgame_guest_mint_allowed") and "exceeded the first-contact limit" in pi
       and "uint32_t pc_net_peer_ip(PCNetPeerId peer);" in read("pc/include/pc_net.h"))
    cf = fb("pcnetgame_guest_confirm_on_record_step")
    ck("M3 an entry is CONFIRMED only by the first authenticated record step (HELLO / BEGIN, after the record gate) of a connection that PRESENTED the matching token "
       "(guest_token_verified, assigned only at admission, mode 1)",
       "!st->guest_token_verified" in cf and "s_guest[g].confirmed = 1;" in cf and cf.count("s_guest[g].confirmed = 1;") == 1 and c.count("s_guest[g].confirmed = 1;") == 1
       and "s_host_peer[peer].guest_token_verified = (guest_mode == 1) ? 1u : 0u;" in pi and c.count("guest_token_verified =") == 1
       and fb("pcnetgame_rec_handle_hello").index("pcnetgame_rec_gate(") < fb("pcnetgame_rec_handle_hello").index("pcnetgame_guest_confirm_on_record_step(peer)")
       and fb("pcnetgame_rec_handle_begin").index("pcnetgame_rec_gate(") < fb("pcnetgame_rec_handle_begin").index("pcnetgame_guest_confirm_on_record_step(peer)")
       and sorted({n for a, b, n in funcs if "pcnetgame_guest_confirm_on_record_step(" in c[a:b] and n != "pcnetgame_guest_confirm_on_record_step"})
       == ["pcnetgame_rec_handle_begin", "pcnetgame_rec_handle_hello"])
    ck("M2/M3 the header documents the bearer-token limitation, the TOFU exposure and the v1 -> v2 (never released) change",
       all(x in gh for x in ("SECURITY MODEL / KNOWN LIMITATIONS", "BEARER token", "TRUST ON FIRST USE", "per-address limit", "never released")))
    # L3: hf_pending / PUSH_HOSTFIELDS is resident-only
    ck("L3 guest slots never get PUSH_HOSTFIELDS: the ONLY start_push(PUSH_HOSTFIELDS) sits in the host record tick behind a RESIDENT binding (bound_resident_idx >= 0 && < PLAYER_NUM) and "
       "hf_pending is only SET in refresh_hostfields (documented dormant for slots >= PLAYER_NUM)",
       len(re.findall(r"pcnetgame_rec_start_push\([^;]*PC_NETGAME_REC_KIND_PUSH_HOSTFIELDS", c)) == 1 and "st->bound_resident_idx >= 0 &&" in fb("pcnetgame_host_record_tick")
       and "st->bound_resident_idx < PLAYER_NUM && s_rec_slot[st->bound_resident_idx].hf_pending" in fb("pcnetgame_host_record_tick")
       and "L3 (guests): hf_pending is only ever CONSUMED for a RESIDENT binding" in raw and len(re.findall(r"->hf_pending = 1;|\bs->hf_pending = 1;", c)) == 1
       and "s->hf_pending = 1;" in fb("pcnetgame_rec_refresh_hostfields"))
    # M4: the fixture save guard
    lib = read("pc/tools/net_spike/net_spike_lib.py")
    ck("M4 net_spike_lib: CloneSaveGuard snapshots / restores the WHOLE save dir of every bin_fixture4* dir (incl. removal of save/mp) as well as the clone's gci; a launch from a fixture dir arms an "
       "automatic whole-save snapshot + at-exit restore (so a test without its own guard cannot contaminate the fixture)",
       "self.fixture = is_fixture_bin_dir(self.bin_dir)" in lib and "_fixture_restore(self.bin_dir, self.snap_dir)" in lib and "_auto_fixture_guard(bin_dir)" in lib
       and "atexit.register(" in lib and "shutil.rmtree(save, ignore_errors=True)" in lib and "def require_launchable_bin_dir" in lib)
    ck("M4 test_identity_validation.py runs under the shared guard; the duplicate per-test FixtureSaveGuard of test_client_villager_talk.py is gone (it uses L.CloneSaveGuard)",
       "with L.CloneSaveGuard():" in read("pc/tools/net_spike/test_identity_validation.py") and "class FixtureSaveGuard" not in read("pc/tools/net_spike/test_client_villager_talk.py")
       and "with L.CloneSaveGuard(args.bin_dir):" in read("pc/tools/net_spike/test_client_villager_talk.py"))
    # ------------------------------------------------------------------------------------------------ G2
    fa = mask(read("pc/src/pc_field_authority.c"))
    fa_fns = functions(fa)
    sr = body(fa, fa_fns, "pcfa_save_ready")
    ck("G2 pcfa_save_ready: a player_no >= mPr_FOREIGNER is 'not ready' (travelling: Save is another town) UNLESS this process is a network CLIENT (it never travels: the "
       "station save is refused for it) or a HOST whose --host-observer latch is set (pc_host_observer_ready(): the hidden observer never travels); a plain host / "
       "single-player foreigner stays not ready exactly as before; every other readiness condition is unchanged",
       re.search(r"if \(Common_Get\(player_no\) >= mPr_FOREIGNER\) \{\s*if \(pc_net_game_role\(\) == PC_NETGAME_ROLE_CLIENT\) \{\s*\} else if "
                 r"\(pc_net_game_role\(\) == PC_NETGAME_ROLE_HOST && pc_host_observer_ready\(\)\) \{\s*\} else \{\s*return 0;\s*\}\s*\}", sr, re.S)
       and all(x in sr for x in ("Common_Get(now_private) == NULL", "case SCENE_TITLE_DEMO:", "case SCENE_PLAYERSELECT_SAVE:", "mLd_CHECK_LAND_ID(Save_Get(land_info.id))")))
    mc = mask(read("pc/src/pc_m_card.c"))
    mfn = functions(mc)
    bg_poll = body(mc, mfn, "pc_bootstrap_guest_poll")
    bg_arr = body(mc, mfn, "pc_guest_arrive")  # G3: the arrival itself moved into the shared pc_guest_arrive (poll = one-shot / readiness waits + exit(2))
    bg_fin = body(mc, mfn, "pc_guest_creation_finish")
    bg_door = body(mc, mfn, "pc_guest_station_door")
    bg = bg_poll + "\n" + bg_arr + "\n" + bg_fin
    ck("G2 --bootstrap-guest (pc_bootstrap_guest_poll + G3 pc_guest_arrive): TEST-ONLY, one-shot, refuses any role but CLIENT, binds a foreigner exactly like the INCOMING_FOREIGNER arrival (now_private = the passport, "
       "player_no = mPr_FOREIGNER; G3.1: mSDI_StartDataInitGuest = the PAK init without the gateway), NEVER arms pc_save_ready, NEVER writes a save or touches private_data[] (it only READS a template), spawns at the station",
       bg_poll != "" and bg_arr != "" and "pc_net_game_role() != PC_NETGAME_ROLE_CLIENT" in bg_arr and "static int l_done = 0;" in bg_poll and "Common_Set(now_private, pass);" in bg_arr
       and "Common_Set(player_no, mPr_FOREIGNER);" in bg_arr and "mSDI_StartDataInitGuest(gamePT)" in bg_arr and "MODE_PAK" not in bg and "pc_save_ready" not in bg
       and "pc_save_write" not in bg and not re.search(r"Save_GetPointer\(private_data\[[^\]]*\]\)\s*->\s*\w+\s*=", bg) and "pc_guest_station_door(&door_data);" in bg_arr and "next_scene_id = SCENE_FG;" in bg_door
       and "exit_position.x = 1979;" in bg_door and "exit_position.z = 760;" in bg_door and "mAc_PROFILE_RIDE_OFF_DEMO" in bg_arr and bg_fin != "")
    mm = mask(read("pc/src/pc_main.c"))
    ck("G2 pc_main.c: --bootstrap-guest is parsed, documented in --help, and REFUSED (exit 2) without --connect or together with --bootstrap-resident; pc_vi.c polls it next to the resident poll",
       'strcmp(argv[i], "--bootstrap-guest") == 0' in mm and "const char* g_pc_bootstrap_guest = NULL;" in mm and "--bootstrap-guest NAME,LAND,PLAYER_ID,LAND_ID" in raw_main()
       and re.search(r"g_pc_bootstrap_guest != NULL && \(g_pc_net_role != 2 \|\| g_pc_bootstrap_resident >= 0\)", mm) and "pc_bootstrap_guest_poll();" in read("pc/src/pc_vi.c"))
    ck("G1.1 --bootstrap-guest is validated EARLY: pc_main.c calls pc_bootstrap_guest_validate(g_pc_bootstrap_guest) (exit 2 on failure) after the role check and before pc_platform_init(); "
       "the poll's failure paths all exit(2) with a stderr diagnostic (no silent `return` after l_done = 1)",
       mm.find("g_pc_bootstrap_guest != NULL && (g_pc_net_role != 2") < mm.find("pc_bootstrap_guest_validate(g_pc_bootstrap_guest)") < mm.find("    pc_platform_init();")
       and "return" not in bg_poll[bg_poll.index("l_done = 1;"):] and bg_poll[bg_poll.index("l_done = 1;"):].count("exit(2);") == 1 and "exit(" not in bg_arr
       and body(mc, mfn, "pc_bootstrap_guest_validate") != "")
    gci = gcheck.index("pcnetgame_guest_find(key)")
    ck("G1.1 guest_check: the KEY conflict + validity run for every mode before the lookup; the resident-name and reserved-name rules sit only AFTER the token decision (mode 1 is never refused for a name)",
       gcheck.index("pcnetgame_guest_key_conflict(key)") < gci and gcheck.index("if (tok_ok) {") > gci
       and gcheck.index("pcnetgame_guest_name_conflict_resident(key)") > gcheck.index("if (tok_ok) {") and gcheck.index("pc_mp_guests_name_reserved(key->player_name)") > gcheck.index("if (tok_ok) {")
       and "pcnetgame_guest_name_conflict_resident" not in fb("pcnetgame_host_revalidate_bound_peers") and "pcnetgame_guest_key_conflict(&st->bound_pid)" in fb("pcnetgame_host_revalidate_bound_peers"))
    ck("G2 the guest's save can never be written by this process: the client role gates every writer (pc_save_write_authoritative, save dialog, shutdown) and pc_save_ready stays 0 under the hook",
       "if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {" in mc and "pc_net_game_role() == PC_NETGAME_ROLE_CLIENT" in mask(read("pc/src/pc_m_card.c")))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
