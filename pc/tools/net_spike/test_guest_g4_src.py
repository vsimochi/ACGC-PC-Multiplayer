#!/usr/bin/env python3
"""test_guest_g4_src.py - GUESTS G4 (capacity: host-local max_guests cap, identity separation under concurrency) SOURCE AUDIT (no game process).

  S   settings: `max_guests` default 4, parsed 1..8 (an out-of-range value is ignored), written by the defaults text and the settings writer; `--max-guests N` (1..8, else exit 2) sets
      an override that wins over the file; both clipped to the guest table size
  P   cap placement: ONE call site of the admission gate, inside pcnetgame_host_process_identity's `if (is_guest) {` block, AFTER the duplicate-binding handling (a returning bound guest
      replaces its own old binding first) and BEFORE any token mint / re-mint / table create; the refusal is pcnetgame_host_refuse_identity(.., 0) = REJECT(SERVER_FULL) with a log line
      naming the cap; residents are never gated (none of the cap helpers is referenced by the resident branches, the resident classification, or any other admission code)
  R   transport reserve: PC_NET_MAX_PEERS is the hard limit; a new guest must leave a transport slot for every resident record that is not connected
  I   identity separation: puppets are keyed by TRANSPORT PEER id (never by player_no 4), the host binding derives the record slot (PLAYER_NUM + guest slot) from the binding only, per-peer
      state arrays are indexed by peer id; dedicated `status` shows the guest counts
  W   no wire change: message ids 1..58, wire_baseline green, protocol header / transport untouched, pc_net_game.c purely additive vs the G3 commit
  T   the new tests exist and carry the expected assertions
Tier: SOURCE AUDITED. Exit code 0 when all checks pass."""
import os
import re
import subprocess
import sys

import net_spike_lib as L
import test_guest_src as S
import wire_baseline

ROOT = S.ROOT
HERE = S.HERE
BASELINE = "891677b"   # G3 commit + docs (HEAD when G4 started)


def numstat(rel):
    out = subprocess.run(["git", "-C", ROOT, "diff", "--numstat", BASELINE, "--", rel], capture_output=True, check=True, timeout=60).stdout.decode().split()
    return (int(out[0]), int(out[1])) if out else (0, 0)


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    st_h = S.read("pc/include/pc_settings.h")
    st_c = S.read("pc/src/pc_settings.c")
    main_c = S.read("pc/src/pc_main.c")
    ng_raw = S.read("pc/src/pc_net_game.c")
    ng = S.mask(ng_raw)
    nf = S.functions(ng)
    nb = lambda n: S.body(ng, nf, n)

    # ------------------------------------------------------------------ S
    ck("S settings: PCSettings has max_guests, default 4 in the initializer, `g_pc_max_guests_override` default 0 (no override)",
       "int max_guests;" in st_h and ".max_guests = 4," in st_c and "int g_pc_max_guests_override = 0;" in st_c and "extern int g_pc_max_guests_override;" in st_h)
    ck("S settings: apply_setting parses max_guests with the range 1..8 and ignores everything else (the default stays)",
       'strcmp(key, "max_guests") == 0' in st_c and "if (val >= 1 && val <= 8) g_pc_settings.max_guests = val;" in st_c)
    ck("S settings: the defaults text and the settings writer both carry `max_guests` under [Network]", st_c.count('"max_guests = 4\\n"') == 1 and "[Network]" in st_c
       and 'fprintf(f, "max_guests = %d\\n", g_pc_settings.max_guests);' in st_c)
    flag = re.search(r'else if \(strcmp\(argv\[i\], "--max-guests"\) == 0\) \{(.*?)\n        \} else if', main_c, re.S)
    fb = flag.group(1) if flag else ""
    ck("S CLI: `--max-guests N` requires a value 1..8 (missing / 0 / 9 / non-numeric exits 2 with a REFUSED line) and sets g_pc_max_guests_override",
       flag is not None and "i + 1 >= argc || mg < 1 || mg > 8" in fb and "return 2;" in fb and "g_pc_max_guests_override = mg;" in fb and "--max-guests: REFUSED" in fb)
    ck("S CLI: pc_main.c changed ONLY by the 11 added --max-guests lines vs the G3 commit (nothing removed)", numstat("pc/src/pc_main.c") == (11, 0))
    ck("S pc_settings.c / .h changed additively only", numstat("pc/src/pc_settings.c")[1] == 1 and numstat("pc/include/pc_settings.h")[1] == 0)   # 1 deleted line = the old last string line of the defaults
    mg = nb("pcnetgame_host_max_guests")
    ck("S the effective cap: override wins over settings.ini, clamped to 1 .. PC_NETGAME_GUEST_MAX (the guest table size, 8)",
       "g_pc_max_guests_override > 0 ? g_pc_max_guests_override : g_pc_settings.max_guests" in mg and "n < 1" in mg and "PC_NETGAME_GUEST_MAX ? PC_NETGAME_GUEST_MAX : n" in mg)

    # ------------------------------------------------------------------ P
    pi = nb("pcnetgame_host_process_identity")
    gate = nb("pcnetgame_host_guest_cap_refusal")
    refs = [m.start() for m in re.finditer(r"pcnetgame_host_guest_cap_refusal\(", ng)]
    ck("P ONE call site of the admission gate (plus its definition), and it is inside pcnetgame_host_process_identity", len(refs) == 2 and pi != "" and "pcnetgame_host_guest_cap_refusal(peer," in pi
       and pi.count("pcnetgame_host_guest_cap_refusal(") == 1)
    i_dup = pi.index("if (other >= 0) {")
    m_isg = re.search(r"    if \(is_guest\) \{\s+char cap_why\[200\];", pi)
    i_isg = m_isg.start() if m_isg else -1
    i_cap = pi.index("pcnetgame_host_guest_cap_refusal(")
    i_mint = pi.index("pcnetgame_guest_mint_allowed(")
    i_rem = pi.index("pcnetgame_guest_remint(")
    i_cre = pi.index("pcnetgame_guest_create(")
    i_rej = pi.index("pcnetgame_host_refuse_identity(peer, cap_msg, 0);")
    ck("P ORDER inside process_identity: duplicate-binding handling (a bound returning guest replaces ITS OWN old binding) < the cap gate < token mint limiter < re-mint < create "
       "(nothing is minted / created before the cap is judged)", i_dup < i_isg < i_cap < i_mint < i_rem < i_cre)
    ck("P the refusal is REJECT(SERVER_FULL) through the existing refuse path (use_no_save 0) and returns before anything else: log text names the cap ('guest limit reached (%d of max_guests=%d ...)')",
       i_cap < i_rej < i_mint and "pcnetgame_host_refuse_identity(peer, cap_msg, 0);\n            return;" in pi
       and "guest limit reached (%d of max_guests=%d guests are connected)" in ng_raw)
    ck("P the gate sits in the guest-only block: the text between `if (is_guest) {` and the gate contains no resident path, and the resident classification / own-resident / duplicate code "
       "before it never mentions the cap helpers",
       all(x not in pi[:i_isg] for x in ("pcnetgame_host_max_guests", "pcnetgame_host_bound_guest_count", "pcnetgame_host_guest_cap_refusal", "pcnetgame_host_resident_peer_reserve", "max_guests")))
    users = {}
    for fn in ("pcnetgame_host_max_guests", "pcnetgame_host_bound_guest_count", "pcnetgame_host_guest_cap_refusal", "pcnetgame_host_resident_peer_reserve"):
        users[fn] = sorted({n for a, b, n in nf if re.search(r"\b%s\(" % fn, ng[a:b]) and n != fn})
    ck("P who calls what: the cap helpers are used ONLY by the gate, the dedicated counts accessor and each other (never by a resident / record / transport function): %s" % users,
       users["pcnetgame_host_guest_cap_refusal"] == ["pcnetgame_host_process_identity"]
       and users["pcnetgame_host_max_guests"] == ["pc_net_game_dedicated_guest_counts", "pcnetgame_host_guest_cap_refusal"]
       and users["pcnetgame_host_bound_guest_count"] == ["pc_net_game_dedicated_guest_counts", "pcnetgame_host_guest_cap_refusal"]
       and users["pcnetgame_host_resident_peer_reserve"] == ["pcnetgame_host_guest_cap_refusal"])
    ck("P the gate counts READY peers bound with class GUEST (never residents), excluding the asking peer: link READY, bound_valid, PC_NETGAME_REC_CLASS_GUEST",
       "s_host_peer_link[j] == PC_NETGAME_LINK_READY" in nb("pcnetgame_host_bound_guest_count") and "PC_NETGAME_REC_CLASS_GUEST" in nb("pcnetgame_host_bound_guest_count")
       and "j != (int)except_peer" in nb("pcnetgame_host_bound_guest_count"))
    ck("P the gate only reads: no write to any s_guest / s_host_peer / s_rec_slot state, no pc_net_* send", not re.search(r"s_guest\w*\[[^\]]*\]\s*(\.\w+)?\s*=[^=]|s_host_peer\[[^\]]*\]\.\w+\s*=[^=]|pc_net_send|pc_net_evict|pc_net_disconnect", gate))

    # ------------------------------------------------------------------ R
    rs = nb("pcnetgame_host_resident_peer_reserve")
    ck("R the reserve counts resident records that exist (not null PersonalID), are not the host's own resident and are not bound to a READY peer",
       "mPr_NullCheckPersonalID" in rs and "i != own" in rs and "pcnetgame_host_peer_bound_to_resident(i, (PCNetPeerId)-1) < 0" in rs and "pcnetgame_host_own_resident_idx()" in rs)
    ck("R the gate refuses a guest when occupied transport peers + reserve exceed PC_NET_MAX_PEERS (log: 'more are held for residents'), checked after the max_guests cap",
       "occupied + reserve > PC_NET_MAX_PEERS" in gate and "pc_net_peer_count()" in gate and "more are held for residents" in gate and gate.index("bound >= cap") < gate.index("occupied + reserve"))

    # ------------------------------------------------------------------ I
    rp = S.read("pc/src/pc_remote_player.c")
    ck("I puppets: slots are indexed by the transport peer id (PC_NET_MAX_PEERS + 1 incl. the host), pc_remote_player.c never mentions player_no", "#define PC_REMOTE_PLAYER_SLOT_COUNT (PC_NET_MAX_PEERS + 1)" in rp
       and "player_no" not in rp and "static PCRemotePlayerSlot s_slots[PC_REMOTE_PLAYER_SLOT_COUNT];" in rp)
    ck("I the host creates the puppet of a READY peer keyed by that peer: pc_remote_player_on_ready(peer, ...) in process_identity, the identity from the peer's OWN IDENTITY message",
       "pc_remote_player_on_ready(peer, &remote_identity);" in pi and "memcpy(remote_identity.player_name, in.player_name" in pi)
    pr = nb("pcnetgame_peer_rec_slot")
    ck("I the record slot of a peer is derived ONLY from its host-side binding: guest -> PLAYER_NUM + bound_guest_slot, resident -> bound_resident_idx; never from a message",
       "PLAYER_NUM + st->bound_guest_slot" in pr and "st->bound_resident_idx" in pr and "in->" not in pr and "msg" not in pr)
    ck("I per-peer state is indexed by the peer id (s_host_peer / s_host_peer_link / record rx-tx buffers have PC_NET_MAX_PEERS entries), never by a guest's shared player_no 4",
       "static PCNetGameHostPeerState s_host_peer[PC_NET_MAX_PEERS];" in ng_raw and "static PCNetGameLinkState s_host_peer_link[PC_NET_MAX_PEERS];" in ng_raw
       and "s_host_rec_rx[PC_NET_MAX_PEERS][PC_NETGAME_REC_SIZE]" in ng_raw)
    ck("I the guest-vs-guest admission rules stay: key conflict, wrong / missing token, duplicate live key parking and the same-town NAME rule among guests (pcnetgame_guest_name_conflict_guest)",
       "pcnetgame_guest_name_conflict_guest(key)" in nb("pcnetgame_host_guest_check") and "presented a WRONG token" in ng_raw.replace("known guest key presented a WRONG token", "presented a WRONG token")
       and "pcnetgame_host_peer_bound_to_guest(guest_slot, peer)" in pi)
    ded = S.read("pc/src/pc_dedicated.c")
    ck("I dedicated `status` prints `guests: bound=%d max_guests=%d` from pc_net_game_dedicated_guest_counts (HOST only)",
       'printf("  guests: bound=%d max_guests=%d\\n", gb, gc);' in ded and "int pc_net_game_dedicated_guest_counts(int* bound, int* cap) {" in ng_raw
       and "pc_net_game_dedicated_guest_counts" in S.read("pc/include/pc_dedicated.h"))

    # ------------------------------------------------------------------ W
    ck("W no wire change: message ids 1..58 unchanged, wire_baseline green, protocol header pc_net_game.h and the transport (pc_net.c / pc_net.h) untouched vs the G3 commit",
       sorted(dict(wire_baseline.c_message_ids(ng_raw)).values()) == list(range(1, 59)) and numstat("pc/include/pc_net_game.h") == (0, 0)
       and numstat("pc/src/pc_net.c") == (0, 0) and numstat("pc/include/pc_net.h") == (0, 0) and numstat("pc/src/pc_remote_player.c") == (0, 0))
    wb = []
    wire_baseline.run(lambda d, cond: wb.append((d, cond)), ROOT)
    ck("W wire_baseline: %d checks, all green" % len(wb), wb and all(c for _d, c in wb))
    ck("W pc_net_game.c is purely additive vs the G3 commit (0 lines removed)", numstat("pc/src/pc_net_game.c")[1] == 0 and numstat("pc/src/pc_net_game.c")[0] > 0)

    # ------------------------------------------------------------------ T
    tp = open(os.path.join(HERE, "test_guest_g4_protocol.py"), encoding="utf-8").read()
    tr = open(os.path.join(HERE, "test_guest_g4_real.py"), encoding="utf-8").read()
    ck("T test_guest_g4_protocol.py covers: 4 concurrent guests, the cap-4 refusal text, resident admitted at the cap, cross-token refusals, restart with --max-guests 8 / 2, settings.ini max_guests = 1, table full",
       all(x in tp for x in ("guest limit reached (4 of max_guests=4 guests are connected)", "WRONG token", "--max-guests", "max_guests = 1", "guest table is full", "more are held for residents",
                             "a RESIDENT is admitted while the guest cap is full")))
    ck("T test_guest_g4_real.py covers: two REAL guests + a REAL resident, own tokens, abrupt kill, same-record restart, host restart",
       all(x in tr for x in ("--guest", "--bootstrap-resident", "bin_fixture4_g4", "cA.stop()", "known guest: token verified", "guest_token.dat")))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
