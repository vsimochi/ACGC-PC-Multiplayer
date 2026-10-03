#!/usr/bin/env python3
"""test_identity_validation_src.py - M9 identity Stage 1A SOURCE AUDIT (no game process, no network, read-only).

Behaviour-ORDERING / structure checks on pc/src/pc_net_game.c against the Stage 1A contract (protocol v7 unchanged):
  S1  protocol version is still 7u
  S2  the identity classification happens BEFORE any IDENTITY_ACK build / READY in pcnetgame_host_process_identity, and
      after the town checks (so the unchanged LAND_MISMATCH / NO_SAVE rejects keep their precedence)
  S3  the classifier is ONE isolated function returning the host-only enum {RESIDENT, UNKNOWN, AMBIGUOUS}; it never reads
      a player_no; the AMBIGUOUS branch (matches > 1) exists; process_identity calls it exactly once and refuses
      AMBIGUOUS / UNKNOWN / the host's own resident / an already-bound resident before READY
  S4  the per-peer binding is set only in process_identity (after READY) and cleared by the single memset-reset, which is
      called from connect, disconnect, drop and reject-and-close; no other memset of s_host_peer[] exists
  S5  the claimed PLAYER_CONTEXT player_no / ctx.player_no is used for nothing but log lines; destiny/money/goods/flags are
      clamped before they are stored
  S6  MAIL_REQUEST overwrites the sender PersonalID with the bound resident's before the villager lookup; FRIENDSHIP_REQUEST
      uses the bound PersonalID and no longer the ready_* claim cache
  S7  every host request handler is gated on LINK_READY
  S8  no wire layout/enum changed vs HEAD: every PCNetGame*/PCNet* wire struct, PCNetGameMsgType and
      PCNetGameRejectReason (values), the whole pc_net_game.h, and net_spike_lib's wire constants are identical to HEAD
  S9  no save-format / persistence file is modified in the working tree (read-only `git diff --name-only`)

Tier: SOURCE AUDITED only (not hook-driven). Usage: python test_identity_validation_src.py"""
import os
import re
import subprocess
import sys

import net_spike_lib as L

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SRC = os.path.join(REPO, "pc", "src", "pc_net_game.c")
HDR = os.path.join(REPO, "pc", "include", "pc_net_game.h")


def norm(b):
    return b.decode("utf-8", "replace").replace("\r\n", "\n")


def read(p):
    with open(p, "rb") as f:
        return norm(f.read())


def git(*args):
    return norm(subprocess.run(["git", "-C", REPO] + list(args), capture_output=True, check=True).stdout)


def func_body(text, name):
    """Text of the definition `[static] type name(...) { ... }` (brace matched; strings/comments skipped)."""
    m = None
    for cand in re.finditer(r"^(?:static )?[\w ]+?[\s\*]%s\(" % re.escape(name), text, re.M):
        j = text.find("{", cand.end())
        k = text.find(";", cand.end())
        if j != -1 and (k == -1 or j < k):
            m = cand
            break
    if not m:
        return ""
    j = text.find("{", m.end())
    depth = 0
    i = j
    in_str = False
    in_cmt = False
    while i < len(text):
        c = text[i]
        two = text[i:i + 2]
        if in_cmt:
            if two == "*/":
                in_cmt = False
                i += 1
        elif in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif two == "/*":
            in_cmt = True
            i += 1
        elif two == "//":
            i = text.find("\n", i)
            continue
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[j:i + 1]
        i += 1
    return ""


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"//[^\n]*", "", s)
    return re.sub(r"\s+", " ", s).strip()


def typedef_blocks(text, pattern):
    """{name: comment-stripped body} for `typedef struct|enum NAME { ... } NAME;` blocks whose NAME matches pattern."""
    out = {}
    for m in re.finditer(r"typedef (?:struct|enum) (\w+) \{(.*?)\n\} (\w+);", text, re.S):
        if re.match(pattern, m.group(3)):
            out[m.group(3)] = strip_comments(m.group(2))
    return out


def main():
    results = []
    check = lambda d, c: L.check(d, c, results)
    src = read(SRC)
    hdr = read(HDR)
    head_src = git("show", "HEAD:pc/src/pc_net_game.c")
    head_hdr = git("show", "HEAD:pc/include/pc_net_game.h")

    # S1
    check("S1 PC_NETGAME_PROTOCOL_VERSION is still 7u", re.search(r"#define PC_NETGAME_PROTOCOL_VERSION 7u\b", hdr) is not None)

    # S2
    pi = func_body(src, "pcnetgame_host_process_identity")
    check("S2 process_identity found", len(pi) > 500)
    i_town = pi.find("PC_NETGAME_REJECT_LAND_MISMATCH")
    i_nosave = pi.find("PC_NETGAME_REJECT_NO_SAVE")
    i_cls = pi.find("pcnetgame_host_classify_identity(")
    i_ack = pi.find("PC_NETGAME_MSG_IDENTITY_ACK")
    i_ready = pi.find("PC_NETGAME_LINK_READY")
    i_bind = pi.find("bound_valid = 1")
    i_snap = pi.find("pcnetgame_host_start_snapshot(")
    check("S2 classification runs after the unchanged NO_SAVE / LAND_MISMATCH checks",
          0 < i_nosave < i_town < i_cls)
    check("S2 classification runs BEFORE the IDENTITY_ACK is built/sent and before READY / roster / snapshot",
          0 < i_cls < i_ack < i_ready < i_snap)
    check("S2 the binding is set together with READY (after the ACK was queued), never earlier",
          i_ready < i_bind < i_snap and pi.count("bound_valid = 1") == 1)
    refuse_positions = [m.start() for m in re.finditer(r"pcnetgame_host_refuse_identity\(", pi)]
    check("S2 every refusal (ambiguous, unknown, host's own, already bound) is issued before the ACK",
          len(refuse_positions) == 4 and all(p < i_ack for p in refuse_positions))

    # S3
    cls = func_body(src, "pcnetgame_host_classify_identity")
    check("S3 classifier is a single isolated function returning PCNetGameIdentityClass",
          len(re.findall(r"^static PCNetGameIdentityClass pcnetgame_host_classify_identity\(", src, re.M)) == 1 and cls != "")
    check("S3 host-only enum has RESIDENT/UNKNOWN/AMBIGUOUS and is not part of any wire struct",
          all(k in src for k in ("PCNETGAME_IDCLASS_RESIDENT", "PCNETGAME_IDCLASS_UNKNOWN", "PCNETGAME_IDCLASS_AMBIGUOUS"))
          and "PCNETGAME_IDCLASS" not in hdr)
    check("S3 classifier compares against the host's OWN Save_Get(private_data) with the vanilla comparator "
          "(+ exists + valid land id)", all(k in cls for k in ("Save_Get(private_data)", "mPr_CheckCmpPersonalID",
                                                                "->exists == TRUE", "mPr_CheckPrivate(")))
    check("S3 classifier never reads a player_no", "player_no" not in cls)
    check("S3 classifier has the AMBIGUOUS branch (matches > 1) and the UNKNOWN branch (matches == 0)",
          "matches > 1" in cls and "return PCNETGAME_IDCLASS_AMBIGUOUS" in cls and "matches == 0" in cls)
    check("S3 process_identity calls the classifier exactly once",
          len(re.findall(r"pcnetgame_host_classify_identity\(", pi)) == 1)
    check("S3 the host's own resident and an already-bound live peer are refused (SERVER_FULL path), unknown -> NO_SAVE path",
          "pcnetgame_host_own_resident_idx()" in pi and "pcnetgame_host_peer_bound_to_resident(" in pi
          and re.search(r'no resident record of this town", 1\)', pi) is not None)
    check("S3 the duplicate scan excludes the peer itself (same-slot restart is never its own duplicate) and counts only "
          "READY+bound peers",
          re.search(r"j != \(int\)except_peer && s_host_peer_link\[j\] == PC_NETGAME_LINK_READY && s_host_peer\[j\]\.bound_valid",
                    src) is not None)
    check("S3 refusals reuse existing reject codes only (no new PC_NETGAME_REJECT_* enumerator)",
          set(re.findall(r"PC_NETGAME_REJECT_\w+", src)) <= {"PC_NETGAME_REJECT_PROTOCOL_MISMATCH",
                                                            "PC_NETGAME_REJECT_SERVER_FULL",
                                                            "PC_NETGAME_REJECT_LAND_MISMATCH", "PC_NETGAME_REJECT_NO_SAVE",
                                                            "PC_NETGAME_REJECT_LINGER_MS"})

    # S4
    reset = func_body(src, "pcnetgame_reset_all_host_peer_state")
    check("S4 the reset memsets s_host_peer[peer] and then restores bound_resident_idx = -1",
          re.search(r"memset\(&s_host_peer\[peer\], 0, sizeof\(s_host_peer\[peer\]\)\);\s*s_host_peer\[peer\]\."
                    r"bound_resident_idx = -1;", reset) is not None)
    check("S4 that is the ONLY memset of s_host_peer[] in the file",
          len(re.findall(r"memset\(&s_host_peer\[", src)) == 1)
    check("S4 bound_valid is assigned 1 in exactly one place (process_identity)",
          len(re.findall(r"bound_valid = 1", src)) == 1)
    for fn, why in (("pcnetgame_host_drop_peer", "host drop / timeout path"),
                    ("pcnetgame_host_reject_and_close", "reject path")):
        check(f"S4 {fn} ({why}) resets the per-peer state", "pcnetgame_reset_all_host_peer_state(peer)" in func_body(src, fn))
    ev = src
    check("S4 PEER_CONNECTED and PEER_DISCONNECTED both reset the host per-peer state",
          len(re.findall(r"pcnetgame_reset_all_host_peer_state\(", ev)) >= 6
          and re.search(r"PC_NET_EVENT_PEER_CONNECTED[\s\S]{0,900}?pcnetgame_reset_all_host_peer_state\(", ev) is not None
          and re.search(r"PC_NET_EVENT_PEER_DISCONNECTED[\s\S]{0,1200}?pcnetgame_reset_all_host_peer_state\(", ev) is not None)

    # S5
    ctxh = func_body(src, "pcnetgame_handle_host_player_context")
    nc = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    stmts = [m.group(0).strip() for m in re.finditer(r"[^;{}]*\b(?:ctx\.player_no|ctx->player_no|in->player_no)\b[^;]*;", nc)]
    allowed = ("printf(", "st->ctx.player_no = (uint8_t)(st->bound_valid ? st->bound_resident_idx : 0)",
               "(int)in->player_no != st->bound_resident_idx",
               # client-side capture / change detection / send of ITS OWN context (never the host handler)
               "s_client_last_ctx", "m.player_no = ctx.player_no", "out->player_no = (uint8_t)Common_Get(player_no)")
    bad = [s_[:90] for s_ in stmts if not any(a_ in s_ for a_ in allowed)]
    check(f"S5 the claimed/stored player_no is only logged, replaced by the host-derived index, or handled on the CLIENT "
          f"capture side ({len(stmts)} statements; other uses: {bad})", not bad and len(stmts) >= 5)
    check("S5 PLAYER_CONTEXT stores the host-derived bound idx, not in->player_no",
          "st->ctx.player_no = (uint8_t)(st->bound_valid ? st->bound_resident_idx : 0)" in ctxh
          and "st->ctx.player_no = in->player_no" not in ctxh)
    for k in ("mPr_DESTINY_NUM", "mPr_MONEY_POWER_MIN", "mPr_GOODS_POWER_MIN", "mPr_GOODS_POWER_MAX",
              "PC_NETGAME_CTX_FLAG_IN_TOWN"):
        check(f"S5 PLAYER_CONTEXT clamps against {k}", k in ctxh)
    check("S5 the stored fields are the clamped locals, never the raw message fields",
          "st->ctx.destiny_type = destiny;" in ctxh and "st->ctx.money_power = money;" in ctxh
          and "st->ctx.goods_power = goods;" in ctxh and "st->ctx.flags = flags;" in ctxh
          and "st->ctx.money_power = in->money_power" not in ctxh and "st->ctx.destiny_type = in->destiny_type" not in ctxh)
    # source ranges the clamps are traced to
    mp = read(os.path.join(REPO, "include", "m_private.h"))
    check("S5 traced ranges match the game headers: MONEY_POWER_MIN -80, GOODS_POWER -30..50, mPr_DESTINY_* enum ends at "
          "mPr_DESTINY_NUM", "#define mPr_MONEY_POWER_MIN -80" in mp and "#define mPr_GOODS_POWER_MIN -30" in mp
          and "#define mPr_GOODS_POWER_MAX 50" in mp and "mPr_DESTINY_NUM" in mp)

    # S6
    mail = func_body(src, "pcnetgame_handle_host_mail_request")
    check("S6 MAIL_REQUEST overwrites the sender with the bound resident's PersonalID BEFORE the villager lookup",
          0 < mail.find("mPr_CopyPersonalID(&mail.header.sender.personalID, &bound_pid)")
          < mail.find("mNpc_PcApplyMailToVillagerMemory("))
    check("S6 MAIL_REQUEST takes the bound PersonalID from the host's own save record (not the claim cache)",
          "pcnetgame_host_bound_personal_id(peer, &bound_pid)" in mail and "ready_" not in strip_comments(mail))
    bp = func_body(src, "pcnetgame_host_bound_personal_id")
    check("S6 the helper returns the CACHED bind-time bound_pid (no live Save_Get reread), keeps the bound_valid check",
          "mPr_CopyPersonalID(out, &s_host_peer[peer].bound_pid)" in bp and "Save_Get" not in bp
          and "!s_host_peer[peer].bound_valid" in bp)
    check("S6 bound_pid is set with bound_valid/bound_resident_idx in process_identity",
          0 < pi.find("bound_valid = 1") < pi.find("mPr_CopyPersonalID(&s_host_peer[peer].bound_pid") < pi.find("pcnetgame_host_send_full_roster"))
    fr = func_body(src, "pcnetgame_handle_host_friendship_request")
    check("S6 world-ready gate precedes the bound-PersonalID use / mutation in the friendship and mail handlers",
          0 < func_body(src, "pcnetgame_handle_host_friendship_request").find("!s_host_world_ready")
          < func_body(src, "pcnetgame_handle_host_friendship_request").find("pcnetgame_host_bound_personal_id(")
          and 0 < mail.find("!s_host_world_ready") < mail.find("pcnetgame_host_bound_personal_id(")
          < mail.find("mNpc_PcApplyMailToVillagerMemory("))
    check("S6 FRIENDSHIP_REQUEST uses the bound PersonalID, not the ready_* claim cache",
          "pcnetgame_host_bound_personal_id(peer, &pid)" in fr and "ready_" not in strip_comments(fr))

    # S7
    handlers = ["pcnetgame_handle_host_move", "pcnetgame_handle_host_appearance", "pcnetgame_handle_host_player_scene",
                "pcnetgame_handle_host_npc_talk", "pcnetgame_handle_host_pickup_request",
                "pcnetgame_handle_host_drop_request", "pcnetgame_handle_host_confirm",
                "pcnetgame_handle_host_field_action_request", "pcnetgame_handle_host_bury_request",
                "pcnetgame_handle_host_friendship_request", "pcnetgame_handle_host_mail_request",
                "pcnetgame_handle_host_snowman_build_request", "pcnetgame_handle_host_wildlife_spawn_trigger_request",
                "pcnetgame_handle_host_catch_request", "pcnetgame_handle_host_player_context"]
    for h in handlers:
        body = func_body(src, h)
        check(f"S7 {h} is gated on s_host_peer_link[peer] READY",
              body != "" and "s_host_peer_link[peer] != PC_NETGAME_LINK_READY" in body[:2600])
    dispatch = func_body(src, "pcnetgame_handle_host_data")
    check("S7 closing (rejected) peers are ignored before any dispatch",
          0 <= dispatch.find("s_host_peer[peer].closing") < dispatch.find("PC_NETGAME_MSG_IDENTITY"))

    # S8
    cur = typedef_blocks(src, r"PCNet\w*")
    old = typedef_blocks(head_src, r"PCNet\w*")
    diff = [k for k in set(cur) | set(old) if cur.get(k) != old.get(k)
            and k not in ("PCNetGameHostPeerState", "PCNetGameIdentityClass")]
    check(f"S8 every PCNet* typedef struct/enum in pc_net_game.c is unchanged vs HEAD except the host-only "
          f"PCNetGameHostPeerState / new PCNetGameIdentityClass (changed: {sorted(diff)}) [{len(cur)} blocks]", not diff and len(cur) > 50)
    check("S8 PCNetGameHostPeerState changed only by appended host-only fields (no wire struct)",
          "PCNetGameHostPeerState" in cur and cur["PCNetGameHostPeerState"].startswith(old["PCNetGameHostPeerState"][:200]))
    check("S8 PCNetGameRejectReason / PCNetGameMsgType values identical to HEAD",
          cur["PCNetGameRejectReason"] == old["PCNetGameRejectReason"] and cur["PCNetGameMsgType"] == old["PCNetGameMsgType"])
    check("S8 pc_net_game.h is byte-identical to HEAD (normalised EOL)", hdr == head_hdr)
    lib = read(os.path.join(HERE, "net_spike_lib.py"))
    head_lib = git("show", "HEAD:pc/tools/net_spike/net_spike_lib.py")
    wire_lines = lambda t: sorted(m.group(0) for m in re.finditer(r"^(?:(?:PC_NETGAME|PCNET)_\w+|\w+_FMT) = .*$", t, re.M))
    check("S8 net_spike_lib wire constants (PC_NETGAME_*, PCNET_*, *_FMT) identical to HEAD",
          wire_lines(lib) == wire_lines(head_lib) and len(wire_lines(lib)) > 50)
    check("S8 net_spike_lib IDENTITY_FMT/build layout unchanged: still 32 bytes",
          "assert IDENTITY_SPEC.size == 32" in lib)

    # S10 (review FIX 1): re-validation on the world-ready transition
    rv = func_body(src, "pcnetgame_host_revalidate_bound_peers")
    wt = func_body(src, "pcnetgame_host_world_tick")
    check("S10 world_tick calls the re-validation right after setting s_host_world_ready = 1",
          0 < wt.find("s_host_world_ready = 1;") < wt.find("pcnetgame_host_revalidate_bound_peers();"))
    check("S10 re-validation uses the host's own resident idx, the cached bind-time PersonalID and exists, and closes via "
          "the existing refuse path",
          all(k in rv for k in ("pcnetgame_host_own_resident_idx()", "mPr_CheckCmpPersonalID(&st->bound_pid",
                                ".exists != TRUE", "pcnetgame_host_refuse_identity(")))
    check("S10 bind time caches the PersonalID from the host save",
          "mPr_CopyPersonalID(&s_host_peer[peer].bound_pid" in pi)
    # NOT hook-tested: the host cannot be made to switch resident/save mid-run by the existing harness.

    # S9
    changed = [x for x in git("diff", "--name-only").split("\n") if x]
    forbidden = re.compile(r"(m_private\.|m_common_data\.|m_card\.|pc_save|pc_m_card\.|m_start_data_init|m_land\.|"
                           r"include/m_personal_id|mCD|\.gci$)")
    check(f"S9 no save-format / persistence file modified (working-tree changes: {changed})",
          not [c for c in changed if forbidden.search(c)])
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
