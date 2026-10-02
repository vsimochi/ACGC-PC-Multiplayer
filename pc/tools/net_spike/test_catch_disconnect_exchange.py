#!/usr/bin/env python3
"""test_catch_disconnect_exchange.py - M9-D Group 1 source-audit checks (NO game process is launched).

Covers, at SOURCE level only (the real paths cannot be reached by the existing hooks):
  F1  mCD_SaveHome_bg (pc/src/pc_m_card.c) returns success WITHOUT writing / without the pre-write
      side effects when pc_net_game_role() == PC_NETGAME_ROLE_CLIENT, before any write call.
  F2  both disconnected-client catch-suppression branches (notice_net / notice_rod) record a denied
      outcome (pc_net_game_record_local_catch_denied) so the putaway exchange gate denies the exchange;
      the function records an accepted=0 outcome.
  F3  pcnetgame_reset_client_session_state clears s_catch_pending (converting a PENDING into REJECTED),
      and a new catch request clears a stale outcome.
  F4  the bug catch reach constant is 1000 units (acre diagonal + net reach) and still exists.

Runtime evidence for F1/F2 (vanilla save dialog / real net-swing + link drop + full-pockets exchange) is
NOT TESTED: no hook reaches those call sites.

Usage: python3 test_catch_disconnect_exchange.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from net_spike_lib import check, summary_and_exit_code  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read().replace("\r\n", "\n")


def func_body(src, header_re):
    m = re.search(header_re, src)
    if not m:
        return ""
    i = src.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    return ""


def main():
    results = []

    # ---- F1
    card = read("pc/src/pc_m_card.c")
    body = func_body(card, r"\nint mCD_SaveHome_bg\(int param_1, int\* chan\) \{")
    check("F1: mCD_SaveHome_bg body found", bool(body), results)
    g = body.find("pc_net_game_role() == PC_NETGAME_ROLE_CLIENT")
    w1 = body.find("pc_save_pre_write_side_effects(")
    w2 = body.find("pc_save_write_gci")
    check("F1: client role gate present", g >= 0, results)
    check("F1: gate precedes the pre-write side effects and every write call",
          g >= 0 and w1 > g and w2 > g, results)
    gate_block = body[g:w1] if g >= 0 and w1 > g else ""
    check("F1: gate returns mCD_TRANS_ERR_NONE (dialog completes) and logs [NET][SAVE]",
          "return mCD_TRANS_ERR_NONE;" in gate_block and "[NET][SAVE] client save skipped" in gate_block,
          results)
    check("F1: gate block does not write", "pc_save_write" not in gate_block, results)
    auth = func_body(card, r"\nint pc_save_write_authoritative\(void\) \{")
    check("F1: pc_save_write_authoritative untouched (still rejects CLIENT)",
          "PC_NETGAME_ROLE_CLIENT" in auth and "return FALSE;" in auth, results)

    # ---- F2
    net = read("pc/src/pc_net_game.c")
    rec = func_body(net, r"\nvoid pc_net_game_record_local_catch_denied\(uint32_t entity_id\) \{")
    check("F2: pc_net_game_record_local_catch_denied defined, records accepted=0",
          "s_catch_last_outcome.accepted = 0" in rec and "s_catch_last_outcome.valid = 1" in rec, results)
    hdr = read("pc/include/pc_net_game.h")
    check("F2: declared in pc_net_game.h",
          "void pc_net_game_record_local_catch_denied(uint32_t entity_id);" in hdr, results)
    for name in ("notice_net", "notice_rod"):
        s = read("src/game/m_player_main_%s.c_inc" % name)
        m = re.search(r"entity_id != 0 && pcwld_should_suppress_local_wildlife\(\)\) \{(.*?)\n\s*grant_handled = TRUE;",
                      s, re.S)
        check("F2: %s suppression branch records the denied outcome before grant_handled" % name,
              bool(m) and "pc_net_game_record_local_catch_denied(entity_id);" in m.group(1), results)
    # ---- L-3: READY client, seam ON, request could not be sent -> deny (no vanilla duplicate grant)
    for name, fn in (("notice_net", "pc_net_game_request_catch_bug"), ("notice_rod", "pc_net_game_request_catch_fish")):
        s = read("src/game/m_player_main_%s.c_inc" % name)
        m = re.search(r"if \(" + fn + r"\([^\n]*\)\) \{\s*grant_handled = TRUE;\s*\} else \{(.*?)\n\s*grant_handled = TRUE;\s*\}",
                      s, re.S)
        check("L-3: %s authoritative branch denies (records outcome, grant_handled) when the request is not sent" % name,
              bool(m) and "pc_net_game_record_local_catch_denied(entity_id);" in m.group(1), results)
        outer = s.find("pc_net_game_authoritative_wildlife_enabled()")
        elsebr = s.find("pc_net_game_record_local_catch_denied(entity_id);")
        check("L-3: %s else is inside the seam-enabled block (flag OFF keeps vanilla grant)" % name,
              0 <= outer < elsebr, results)
    gate = read("src/game/m_player_main_putaway_net.c_inc") + read("src/game/m_player_main_putaway_rod.c_inc")
    check("F2: putaway gates still deny on REJECTED",
          gate.count("PC_NETGAME_CATCH_STATUS_REJECTED") >= 2, results)

    # ---- F3
    rst = func_body(net, r"\nstatic void pcnetgame_reset_client_session_state\(void\) \{")
    check("F3: reset clears s_catch_pending", "memset(&s_catch_pending, 0, sizeof(s_catch_pending));" in rst,
          results)
    check("F3: a PENDING catch is converted to a REJECTED outcome (gate stays closed)",
          re.search(r"if \(s_catch_pending\.valid\) \{\s*s_catch_last_outcome\.valid = 1;\s*"
                    r"s_catch_last_outcome\.entity_id = s_catch_pending\.entity_id;\s*"
                    r"s_catch_last_outcome\.accepted = 0;", rst) is not None, results)
    check("F3: request id counter NOT reset", "s_next_catch_request_id = 1" not in rst, results)
    req = func_body(net, r"\nstatic int pcnetgame_request_catch_common\(")
    check("F3: new catch request clears a stale outcome before arming pending",
          req.find("s_catch_last_outcome.valid = 0;") != -1
          and req.find("s_catch_last_outcome.valid = 0;") < req.find("s_catch_pending.valid = 1;"), results)

    # ---- F4
    m = re.search(r"#define PC_NETGAME_BUG_CATCH_REACH_SQ \((\d+)\.0f \* (\d+)\.0f\)", net)
    check("F4: bug catch reach is 1000 units (square)", bool(m) and m.group(1) == "1000" and m.group(2) == "1000",
          results)
    check("F4: reach check still used (anti-teleport bound kept)",
          "pcnetgame_bug_catch_reach_check(" in net and net.count("pcnetgame_bug_catch_reach_check(") >= 2,
          results)
    check("F4: fish reach unchanged (800)", "#define PC_NETGAME_FISH_CATCH_REACH_SQ (800.0f * 800.0f)" in net,
          results)

    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
