#!/usr/bin/env python3
"""test_admin_items_src.py - host admin item tools (`finditem` / `iteminfo` / `items` / `give`) SOURCE AUDIT (no game process).

Tier: SOURCE AUDITED. The behaviour itself is exercised by the REAL-PROCESS test test_admin_items_protocol.py.
  A  console: the four commands are dispatched by pc_ded_execute (main thread: only pc_dedicated_console_poll calls it, never the stdin reader thread) and listed in help.
  B  give (pc_net_game_dedicated_give): host + world-ready gate, own resident refused, rev==0 refused, untrusted guests refused, connected owner must be SYNCED / no open
     upload / in town; capacity check BEFORE any write (never overwrites); validation through pcnetgame_rec_validate_inventory + the sanctioned writer; lineage bump
     (rev++, dirty_unsaved, last_pocket_rev) and PUSH_FULL for a connected owner; guest store write with rollback; no direct house / mailbox edit, no RNG presents, no
     new message, wallet passed through unchanged.
  C  item restrictions come from pcnetgame_is_pocket_legal_item (+ money / tickets / my-design / rotation bits).
  D  charset: item names go through a 256-entry game-font -> ASCII table (not the identity 0x20-0x7E helper).
  E  no wire change (wire_baseline), pc_net_game.h untouched, no bell commands / teleport added, docs updated.
Exit code 0 when all checks pass."""
import re
import sys

import net_spike_lib as L
import test_guest_src as S
import wire_baseline

ROOT, HERE = S.ROOT, S.HERE


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    ng_raw = S.read("pc/src/pc_net_game.c")
    ng = S.mask(ng_raw)
    nf = S.functions(ng)
    dd_raw = S.read("pc/src/pc_dedicated.c")
    dd = S.mask(dd_raw)
    df = S.functions(dd)
    hdr = S.read("pc/include/pc_dedicated.h")
    give = S.body(ng, nf, "pc_net_game_dedicated_give")
    block = S.body(ng, nf, "pc_net_game_dedicated_item_giveblock")
    resolve = S.body(ng, nf, "pcnetgame_dedicated_give_resolve")

    # ---------------------------------------------------------------- A
    ex = S.body(dd, df, "pc_ded_execute")
    for cmd, fn in (("finditem", "pc_ded_cmd_finditem"), ("iteminfo", "pc_ded_cmd_iteminfo"), ("items", "pc_ded_cmd_items"), ("give", "pc_ded_cmd_give")):
        ck("A `%s` is dispatched to %s" % (cmd, fn), re.search(r'strcmp\(cmd, "%s"\) == 0\)\s*\{\s*%s\(args\);' % (cmd, fn), ex))
    helptxt = S.body(dd_raw, S.functions(dd_raw), "pc_ded_cmd_help")
    ck("A help lists finditem / iteminfo / items / give", all(x in helptxt for x in ("finditem <text> [page]", "iteminfo <id>", "items <furniture|tools|clothing|wallpaper|carpet|miscellaneous|all>", "give <player")))
    callers = [n for _a, _b, n in df if re.search(r"\bpc_ded_execute\(", S.body(dd, df, n)) and n != "pc_ded_execute"]
    ck("A pc_ded_execute is only called from pc_dedicated_console_poll (the main thread, once per frame)", callers == ["pc_dedicated_console_poll"])
    ck("A the stdin reader thread never executes commands", "pc_ded_execute" not in S.body(dd, df, "pc_ded_reader_loop") and "pc_net_game_dedicated_give" not in S.body(dd, df, "pc_ded_reader_loop"))
    ck("A pc_vi.c calls the console poll after pc_net_game_poll on the game thread", re.search(r"pc_net_game_poll\(\);[\s\S]{0,400}pc_dedicated_console_poll\(\);", S.read("pc/src/pc_vi.c")))
    ck("A the wrappers parse strictly: 0x/decimal numbers, qty 1..15, id 0..0xFFFF", "pc_ded_parse_num" in dd and "qv < 1 || qv > 15" in dd and "v > 0xFFFFul" in dd)
    ck("A pagination of 15 lines and a 'does not exist' answer for a page past the end", "PC_DED_ITEM_PAGE 15" in dd and "does not exist" in dd and "- no results" in dd)

    # ---------------------------------------------------------------- B
    ck("B give exists and is HOST-only: gated on s_role == HOST and s_host_world_ready", "s_role != PC_NETGAME_ROLE_HOST || !s_host_world_ready" in give)
    ck("B the host's own resident is refused", "pcnetgame_host_own_resident_idx()" in give)
    ck("B a record that never synced (rev 0) is refused (a MIGRATE would make the gift vanish)", re.search(r"slot == NULL \|\| slot->rev == 0u", give))
    ck("B untrusted guest storage refuses guest gives", "is_guest && s_guest_untrusted" in give)
    ck("B the target must pass pcnetgame_rec_txn_idx_ok", "pcnetgame_rec_txn_idx_ok(idx)" in give)
    ck("B a connected owner must be SYNCED, with no open upload / house upload, and outside in the town",
       all(x in give for x in ("PC_NETGAME_RECS_SYNCED", "st->up_open", "st->hup_open", "st->ctx_valid", "PC_NETGAME_CTX_FLAG_IN_TOWN")))
    i_cap = give.find("inventory is full")
    i_val = give.find("pcnetgame_rec_validate_inventory(")
    i_wr = give.find("pcnetgame_rec_txn_write_inventory(idx, post16")
    ck("B order: capacity check -> validation of the post image -> write (nothing is written before both)", 0 < i_cap < i_val < i_wr)
    ck("B only EMPTY_NO pockets are filled (never overwritten) and all-or-nothing (used < qty refuses before any write)", "post[i] == (mActor_name_t)EMPTY_NO" in give and "if (used < qty)" in give)
    ck("B item conditions of the filled slots are set to NORMAL", "mPr_SET_ITEM_COND(conds, i, mPr_ITEM_COND_NORMAL)" in give)
    ck("B the wallet is passed through unchanged (no bell command)", "pcnetgame_rec_txn_write_inventory(idx, post16, conds, rec->inventory.wallet)" in give and "wallet =" not in give)
    ck("B lineage bump: slot->rev++, dirty_unsaved = 1 and s_txn_res[idx].last_pocket_rev = slot->rev (stale TXNs become STALE_IMAGE)",
       all(x in give for x in ("slot->rev++", "slot->dirty_unsaved = 1", "s_txn_res[idx].last_pocket_rev = slot->rev")))
    ck("B a connected owner gets a PUSH_FULL (hf_pending cleared first)", re.search(r"slot->hf_pending = 0;[\s\S]{0,200}pcnetgame_rec_start_push\(\(PCNetPeerId\)peer, idx, \(uint8_t\)PC_NETGAME_REC_KIND_PUSH_FULL\)", give))
    ck("B an offline guest is persisted through pcnetgame_guest_store_write and rolled back when that fails", "pcnetgame_guest_store_write(" in give and "*slot = old_rs" in give and "s_txn_res[idx].last_pocket_rev = old_lpr" in give)
    ck("B residents request the early host save (the GCI + records.dat carry the new lineage)", "s_rec_early_save_request = 1" in give)
    ck("B no direct house / mailbox / present-RNG / transport edit in give",
       not re.search(r"s_hh\b|homes\[|mailbox|mPr_SetPossessionItem|mPr_DummyPresent|pc_net_send|Common_Get|Now_Private\b", give))
    ck("B names resolve over this town's residents and ACTIVE guests only (other-town entries skipped), ambiguity refused",
       "pcnetgame_town_equal(&s_guest[i].town, &s_host_town)" in resolve and "Ambiguous player" in resolve and "pc_net_game_dedicated_peer_info" in resolve)
    ck("B the failure texts exist: Unknown player / inventory is full / No items were given", all(x in (give + resolve) for x in ("Unknown player '%s'.", "inventory is full", "No items were given.")))
    ck("B the console wrapper prints 'GIVE: <name> received item 0x.... (Name)' and 'GIVE FAILED: ' lines", 'GIVE: %s received item 0x%04X (%s)%s.' in dd_raw and 'GIVE FAILED: %s' in dd_raw)

    # ---------------------------------------------------------------- C
    ck("C giveblock derives validity from pcnetgame_is_pocket_legal_item", "pcnetgame_is_pocket_legal_item(it)" in block)
    ck("C giveblock refuses money, tickets, my-design umbrellas / mannequins and rotated furniture",
       all(x in block for x in ("ITM_MONEY_START", "ITM_TICKET_START", "ITEM_IS_MYUMBRELLA_TOOL", "ITEM_IS_MYMANNIQUIN", "ITEM_IS_MYUMBRELLA", "(it & 3u) != 0u")))
    ck("C give consults giveblock and the console also refuses ids the game has no name for", "pc_net_game_dedicated_item_giveblock(id)" in give and "pc_ded_item_name(id, nm, sizeof(nm))" in S.body(dd, df, "pc_ded_cmd_give"))
    ck("C the wrapper masks rotation bits of furniture ids (reported), the net side refuses rotated ids", "v &= ~3ul" in dd and "(it & 3u) != 0u" in block)

    # ---------------------------------------------------------------- D
    ck("D a 256-entry game-font -> ASCII table exists and names are decoded through it", "static char s_ded_font_ascii[256];" in dd and "s_ded_font_ascii[raw[i]]" in S.body(dd, df, "pc_ded_item_name"))
    fi = S.body(dd, df, "pc_ded_font_init")
    ck("D the table folds the accented letters, maps hyphen/slash/plus/semicolon/hash/tilde and defaults to '?'",
       all(x in fi for x in ("s_ded_font_ascii[i] = '?'", "s_ded_font_ascii[35] = 'a'", "s_ded_font_ascii[144] = '-'", "s_ded_font_ascii[174] = '/'", "s_ded_font_ascii[180] = '+'",
                             "s_ded_font_ascii[208] = ';'", "s_ded_font_ascii[209] = '#'", "s_ded_font_ascii[42] = '~'")))
    ck("D item names never go through the identity 0x20-0x7E helper", "0x7E" not in dd and "pcnetgame_dedicated_ascii_name" not in S.body(ng, nf, "pc_net_game_dedicated_item_giveblock"))
    ck("D the 'unknown' / blank name entries are shown as '(unknown name)' and flagged not giveable", "(unknown name)" in dd and "[not giveable: no name]" in dd)

    # ---------------------------------------------------------------- E
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    for d, cond in wb:
        ck("E wire_baseline: " + d, cond)
    import subprocess
    diff_h = subprocess.run(["git", "-C", ROOT, "diff", "--name-only", "HEAD", "--", "pc/include/pc_net_game.h"], capture_output=True, timeout=60).stdout.decode().strip()
    ck("E pc_net_game.h is byte-identical to HEAD (the wire header is untouched)", diff_h == "")
    ck("E pc_dedicated.h only gained the host-only prototypes (no message / wire type)", "pc_net_game_dedicated_give(" in hdr and "pc_net_game_dedicated_item_giveblock(" in hdr)
    newcode = give + resolve + block
    ck("E no bell command and no teleport in give", "teleport" not in give.lower() and "bank" not in give.lower() and "loan" not in give.lower())
    ck("E no new TXN kind / message type was added by this feature", "PC_NETGAME_MSG_ADMIN" not in ng and "PC_NETGAME_TXN_KIND_ADMIN" not in ng)
    doc = S.read("docs/multiplayer-guest-roadmap.md")
    ck("E the docs describe the commands, formats, failure behaviour, pagination and the incomplete categories",
       all(x.lower() in doc.lower() for x in ("finditem", "iteminfo", "items <furniture", "give <", "GIVE FAILED", "Page ", "incomplete")))
    ck("E the new tests exist", all(__import__("os").path.isfile(__import__("os").path.join(HERE, f)) for f in ("test_admin_items_protocol.py", "test_admin_items_src.py")))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
