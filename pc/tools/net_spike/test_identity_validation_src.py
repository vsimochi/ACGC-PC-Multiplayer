#!/usr/bin/env python3
"""test_identity_validation_src.py - M9 identity Stage 1A (+1B) SOURCE AUDIT (no game process, no network, read-only).

Behaviour-ORDERING / structure checks on pc/src/pc_net_game.c against the Stage 1A contract (protocol v7 unchanged):
  S1  protocol version is the expected v8 (wire_baseline.EXPECTED_PROTOCOL_VERSION)
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
  S11 (Stage 1B) transport idle query + pc_net_evict (lose-style), constants 2500/6000, never-evict-live, park (no authority),
      evict only in the idle>=stale branch then admit only after the PEER_DISCONNECTED teardown, no wire/header change
  S9  save FORMAT untouched (content-based: layout/bswap sources identical to HEAD; pc_m_card.c diff touches no GCI layout line)

Tier: SOURCE AUDITED only (not hook-driven). Usage: python test_identity_validation_src.py"""
import os
import re
import subprocess
import sys

import net_spike_lib as L
import wire_baseline

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
    check("S1 PC_NETGAME_PROTOCOL_VERSION is v%d (wire_baseline.EXPECTED_PROTOCOL_VERSION)" % wire_baseline.EXPECTED_PROTOCOL_VERSION,
          wire_baseline.header_protocol_ok(hdr))

    # S2
    # M-D re-pin: the admission DECISION (has_save / town, classify, the guest and resident rules, the M-E credential check) is pcnetgame_host_admission_decide(); the refusal
    # application is pcnetgame_host_admission_apply_refusal(); process_identity keeps the duplicate park, cap, mint, ACK, token and the binding. The three bodies are checked
    # as ONE text in that order (decide, apply, process_identity): every ordering rule below is about that sequence.
    pi = (func_body(src, "pcnetgame_host_admission_decide") + "\n" + func_body(src, "pcnetgame_host_admission_apply_refusal") + "\n"
          + func_body(src, "pcnetgame_host_process_identity"))
    check("S2 process_identity found", len(pi) > 500 and "pcnetgame_host_admission_decide(peer, &in, &ext, ext_valid, &adm);" in func_body(src, "pcnetgame_host_process_identity"))
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
    check("S2 every refusal (ambiguous, unknown, guest-flagged claim on a resident, host's own, credential, already bound, guest cap / allow_new_guests / entry could not be created / re-minted, "
          "first-contact rate limit, resident mint) is issued before the ACK (M-D: 8 pcnetgame_host_refuse_identity sites = the apply helper's one + 7 in process_identity; the decision's "
          "refusals are DATA returned by pcnetgame_host_admission_decide and applied before any ACK; the guest-claim checks live in pcnetgame_host_guest_check)",
          len(refuse_positions) == 8 and all(p < i_ack for p in refuse_positions))

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
                                                            "PC_NETGAME_REJECT_RESIDENT_CREDENTIAL",  # M-E: the ONE deliberate new reason (8-byte form)
                                                            "PC_NETGAME_REJECT_PROMOTED",  # M-F: reason 6 (8-byte form), sent only to a promoted guest after its RESIDENT_HANDOFF
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
               # guests G1: a GUEST's host-derived player_no is the constant foreigner number, never a claim
               "st->ctx.player_no = (uint8_t)PLAYER_NUM", "(int)in->player_no != PLAYER_NUM",
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
    slot_fn = func_body(src, "pcnetgame_peer_rec_slot")
    check("S6 the helper returns the CACHED bind-time bound_pid (no live Save_Get reread) and keeps the bound_valid check (guests G1: through the one "
          "binding -> slot helper pcnetgame_peer_rec_slot, which holds the bound_valid check and never reads a message)",
          "mPr_CopyPersonalID(out, &s_host_peer[peer].bound_pid)" in bp and "Save_Get" not in bp
          and "pcnetgame_peer_rec_slot(peer)" in bp and "!st->bound_valid" in slot_fn and "Save_Get" not in slot_fn)
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
    # v8 (D3): the wire audit is delegated to wire_baseline, which accepts ONLY the documented v8 additions (ids 47-50, the four
    # pinned structs, the version constant, host-only members/structs) and fails on anything else (selftest-proven).
    wire_baseline.run(lambda d, c: check("S8 " + d, c), REPO)
    check("S8 PCNetGameHostPeerState changed only by appended host-only fields (no wire struct)",
          "PCNetGameHostPeerState" in cur and cur["PCNetGameHostPeerState"].startswith(old["PCNetGameHostPeerState"][:200]))
    hdr_removed = [l for l in git("diff", "-U0", "HEAD", "--", "pc/include/pc_net_game.h").split("\n")
                   if l.startswith("-") and not l.startswith("---")]
    check("S8 pc_net_game.h differs from HEAD only by ADDED lines plus the protocol-version define (removed: %s)" % hdr_removed,
          all(("PC_NETGAME_PROTOCOL_VERSION" in l or "unreleased v7, so no further bump" in l) for l in hdr_removed))
    lib = read(os.path.join(HERE, "net_spike_lib.py"))
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

    # S11 (Stage 1B): stale-session handling -- parking, never-evict-live, evict-then-admit ordering, no wire change
    net_c = read(os.path.join(REPO, "pc", "src", "pc_net.c"))
    net_h = read(os.path.join(REPO, "pc", "include", "pc_net.h"))
    head_net_c = git("show", "HEAD:pc/src/pc_net.c")
    win_c = net_c[net_c.index("#else /* _WIN32 */"):]  # skip the non-Windows stubs
    idle_fn = func_body(win_c, "pc_net_peer_idle_ms")
    evict_fn = func_body(win_c, "pc_net_evict")
    check("S11 transport has a read-only peer idle query built from last_recv_tick (CONNECTED host peers only, -1 else), "
          "declared in pc_net.h",
          "last_recv_tick" in idle_fn and "!= PCNET_PEER_CONNECTED" in idle_fn and "return -1" in idle_fn
          and "s_peers[peer]." not in re.sub(r"s_peers\[peer\]\.(state|last_recv_tick)", "", idle_fn)
          and "int pc_net_peer_idle_ms(PCNetPeerId peer);" in net_h)
    check("S11 pc_net_evict reuses the lose-style path (pcnet_lose_peer: delivers ACKed payloads, queues DISCONNECTED), "
          "not pc_net_disconnect/purge",
          "pcnet_lose_peer((int)peer)" in evict_fn and "pcnet_purge_peer_data_events" not in evict_fn
          and "pcnet_free_slot" not in evict_fn and "PCNET_WIRE_DISCONNECT" in evict_fn
          and "void pc_net_evict(PCNetPeerId peer);" in net_h)
    wire_c = lambda t: sorted(m.group(0) for m in re.finditer(r"^#define PCNET_(?:WIRE_|MAGIC|HEARTBEAT|TIMEOUT)\w*.*$", t, re.M))
    check("S11 transport wire constants/timeouts unchanged vs HEAD (PCNET_WIRE_*, MAGIC, HEARTBEAT 500, TIMEOUT 5000)",
          wire_c(net_c) == wire_c(head_net_c) and len(wire_c(net_c)) >= 4
          and "#define PCNET_HEARTBEAT_INTERVAL_MS 500u" in net_c and "#define PCNET_TIMEOUT_MS            5000u" in net_c)
    check("S11 the wire structs/enums of pc_net.c are unchanged vs HEAD",
          typedef_blocks(net_c, r".*") == typedef_blocks(head_net_c, r".*") and len(typedef_blocks(net_c, r".*")) >= 3)
    hd = git("diff", "-U0", "HEAD", "--", "pc/include/pc_net.h")
    check("S11 pc_net.h only gained declarations (no removed/changed line)",
          [l for l in hd.split("\n") if l.startswith("-") and not l.startswith("---")] == [])
    check("S11 pc_net_game.h: no declaration REMOVED vs HEAD (v8 only adds the early-save API + version text)",
          all(("PC_NETGAME_PROTOCOL_VERSION" in l or "unreleased v7, so no further bump" in l) for l in hdr_removed))
    check("S11 named constants: stale idle 2500u (5 missed heartbeats, < transport timeout 5000) and park window 6000u "
          "(> transport timeout)",
          re.search(r"#define PC_NETGAME_STALE_PEER_IDLE_MS 2500u\b", src) is not None
          and re.search(r"#define PC_NETGAME_DUP_PARK_MAX_MS\s+6000u\b", src) is not None)
    i_own = pi.find("claimed resident is the host's own resident")
    i_dup = pi.find("pcnetgame_host_peer_bound_to_resident(")
    i_idle = pi.find("pc_net_peer_idle_ms(")
    i_ev = pi.find("pc_net_evict(")
    i_cmp = pi.find(">= (int)PC_NETGAME_STALE_PEER_IDLE_MS")
    i_exp = pi.find("park_now - pst->dup_park_since_ms")
    i_dupref = pi.find("claimed resident is already connected on another peer")
    i_log_park = pi.find("parked (%s %d live")
    check("S11 the host's own-resident refusal stays immediate and precedes the duplicate/park logic",
          0 < i_own < i_dup < i_idle)
    check("S11 eviction happens ONLY inside the `idle >= PC_NETGAME_STALE_PEER_IDLE_MS` branch (one pc_net_evict call, "
          "never keyed on the claim alone) and before any ACK/READY",
          0 < i_idle < i_cmp < i_ev < i_ack and src.count("pc_net_evict(") == 1 and pi.count("pc_net_evict(") == 1
          and re.search(r"if \(idle_ms >= \(int\)PC_NETGAME_STALE_PEER_IDLE_MS\) \{[^}]*pc_net_evict\(", pi, re.S) is not None)
    check("S11 a live old peer is never dropped/disconnected/reset by process_identity (only pc_net_evict of a stale one)",
          "pc_net_disconnect(" not in pi and "pcnetgame_host_drop_peer(other" not in pi
          and "pcnetgame_reset_all_host_peer_state(other" not in pi and "reject_and_close(other" not in pi
          and "refuse_identity((PCNetPeerId)other" not in pi and "refuse_identity(other" not in pi)
    ev_branch = pi[i_cmp:i_exp]
    check("S11 after the eviction the newcomer stays parked (identity_pending = 1; return) -- admission only by the "
          "re-run through the duplicate check after the old peer's PEER_DISCONNECTED teardown",
          re.search(r"pc_net_evict\([^;]*;\s*pst->identity_pending = 1;\s*return;", ev_branch) is not None)
    check("S11 the refusal after the park window uses the unchanged SERVER_FULL path and only after PC_NETGAME_DUP_PARK_MAX_MS",
          i_cmp < i_exp < i_dupref < i_log_park and re.search(
              r"park_now - pst->dup_park_since_ms\) >= PC_NETGAME_DUP_PARK_MAX_MS\) \{[^}]*"
              r"pcnetgame_host_refuse_identity\(peer, is_guest \? \"claimed guest is already connected on another peer\"\s*:\s*"
              r"\"claimed resident is already connected on another peer\", 0\)",
              pi, re.S) is not None)
    check("S11 park: identity_pending stays 1 and the function returns (no ACK, no READY, no binding for the newcomer); "
          "the park window state is per peer and cleared by the single memset",
          re.search(r"pst->identity_pending = 1;[^\n]*\n\s*return;\s*\}\s*\n\s*(?:if \(is_guest\) \{[\s\S]*?\n    \}\s*\n\s*)?(?:if \(!is_guest && res_cred == 2\) \{[\s\S]*?\n    \}\s*\n\s*)?/\* Accept", pi) is not None
          and all(k in src for k in ("int                  dup_park_active;", "uint32_t             dup_park_since_ms;")))
    check("S11 park/evict log lines exist; no environment/flag bypass in process_identity",
          "host: peer %d parked (%s %d live on peer %d, idle %d ms)" in pi
          and "host: evicted stale peer %d (%s %d, idle %d ms) for peer %d" in pi
          and 'dup_label = is_guest ? "guest slot" : "resident";' in pi
          and "getenv" not in pi and "g_pc_" not in pi.replace("g_pc_dedicated", "") and "PC_ENHANCEMENTS" not in pi)
    pend = func_body(src, "pcnetgame_host_process_pending_identities")
    check("S11 parked peers are re-evaluated every host poll (process_pending, host poll after the event drain) without "
          "log spam", "pcnetgame_host_process_identity(" in pend and "dup_park_active" in pend)
    disc = src[src.find("case PC_NET_EVENT_PEER_DISCONNECTED:"):]
    disc = disc[:disc.find("case PC_NET_EVENT_DATA:")]
    check("S11 the (unchanged) host PEER_DISCONNECTED handler is the single teardown of an evicted peer: link DISCONNECTED, "
          "scene gone, reset_all_host_peer_state (reservations/dedup/binding/talk hold), remote actor removal",
          all(k in disc for k in ("PC_NETGAME_LINK_DISCONNECTED", "pcnetgame_host_peer_scene_gone(",
                                  "pcnetgame_reset_all_host_peer_state(", "pc_remote_player_on_disconnect(")))
    check("S11 Stage 1A refusal strings (ambiguous, unknown, host-own, duplicate) are all still present",
          all(k in pi for k in ("claimed identity matches more than one resident record (ambiguous)",
                                "claimed identity matches no resident record of this town",
                                "claimed resident is the host's own resident",
                                "claimed resident is already connected on another peer")))
    check("S11 protocol version is v%d" % wire_baseline.EXPECTED_PROTOCOL_VERSION, wire_baseline.header_protocol_ok(hdr))

    # S9 (content-based). The old check "no save/persistence FILE in `git diff --name-only`" is stale: pc_m_card.c is
    # intentionally modified by Batch G1 (client role skips arming the reset code / persisting). The intent is "the SAVE
    # FORMAT is untouched", so: (1) the layout/bswap sources are byte-identical to HEAD, and (2) the pc_m_card.c diff vs HEAD
    # touches no GCI layout / (de)serialisation line (constants, pc_save_write_gci_to, offsets, byte swaps, file I/O).
    layout_files = ["pc/src/pc_save_bswap.c", "include/m_private.h", "include/m_common_data.h", "include/m_home_h.h"]
    moved = [f for f in layout_files if git("diff", "--name-only", "HEAD", "--", f).strip()]
    # v8 (D3): pc_save_bswap.c may only GAIN the public wrapper pc_save_bswap_private (no removed line, no layout/swap change)
    bsw = git("diff", "-U0", "HEAD", "--", "pc/src/pc_save_bswap.c")
    bsw_add = [l[1:] for l in bsw.split("\n") if l.startswith("+") and not l.startswith("+++")]
    bsw_rm = [l for l in bsw.split("\n") if l.startswith("-") and not l.startswith("---")]
    if not bsw_rm and any(("void pc_save_bswap_private(Private_c* prv, pc_bswap_dir_t dir) {" in l) or ("void pc_save_bswap_home(mHm_hs_c* home, pc_bswap_dir_t dir) {" in l) for l in bsw_add) \
            and not any(re.match(r"\s*swap\d*\(", l) for l in bsw_add if "swap_Private(prv, dir);" not in l and "swap_mHm_hs(home, dir);" not in l):
        moved = [f for f in moved if f != "pc/src/pc_save_bswap.c"]
    check(f"S9 save-layout sources unchanged vs HEAD ({layout_files}; modified: {moved})",
          not moved and all(os.path.isfile(os.path.join(REPO, f)) for f in layout_files))
    # --host-observer: the additions of that opt-in feature are fenced by `/* OBSERVER-BEGIN */ ... /* OBSERVER-END */` markers (their own audit is
    # test_observer_src.py: no GCI / serialisation call inside them). The observer blocks are stripped from BOTH sides before diffing, so the observer
    # cannot hide a layout / serialisation change anywhere else in pc_m_card.c.
    # The baseline is 0c9bc72 (the last commit before the town-cache / transfer / promotion work): HEAD itself is no longer a usable baseline
    # (the working tree == HEAD, and stripping observer blocks on one side only made every observer line show up as a removal).
    import difflib
    strip_obs = lambda t: re.sub(r"/\* OBSERVER-BEGIN \*/.*?/\* OBSERVER-END \*/\n?", "", t, flags=re.S)
    # M-A runtime paths: the constants became runtime accessors (pc_town_cache.c); normalise the OLD spelling to the new one (whole identifiers only,
    # so PC_GCI_PATH_LEGACY is left alone).
    def nz_paths(t):
        t = re.sub(r"\bPC_GCI_TMP_PATH\b", "pc_gci_tmp_path()", t)
        t = re.sub(r"\bPC_GCI_PATH\b", "pc_gci_path()", t)
        return re.sub(r"\bPC_CARD_A_DIR\b", "pc_card_a_dir()", t)
    old_card_raw = git("show", "0c9bc72:pc/src/pc_m_card.c")
    base_card_raw = git("show", "a74740b:pc/src/pc_m_card.c")
    cur_card_raw = read(os.path.join(REPO, "pc/src/pc_m_card.c"))
    old_card = nz_paths(strip_obs(old_card_raw))
    cur_card = strip_obs(cur_card_raw)
    # (1) the three GCI (de)serialisers are byte-identical to the a74740b blob (observer blocks stripped on both sides)
    pinned = {}
    for fn in ("pc_save_write_gci_to", "pc_save_write_gci", "pc_save_read_gci"):
        cb, bb = func_body(cur_card, fn), func_body(strip_obs(base_card_raw), fn)
        pinned[fn] = bool(cb) and cb == bb
    check(f"S9 pc_save_write_gci_to / pc_save_write_gci / pc_save_read_gci bodies byte-identical to a74740b ({pinned})", all(pinned.values()))
    # (2) diff vs 0c9bc72: every changed layout / serialisation line must be inside a NEW function (not defined in 0c9bc72: pc_ts_*, pc_mp_promote_*,
    # the town-image reader / sanitize-template code, ...), a PC_TS_ / s_ts_ top-level template declaration, or in the explicit allow-list below.
    old_l, cur_l = old_card.split("\n"), cur_card.split("\n")
    def_rx = re.compile(r"^(?:static )?[\w \*]+?[\s\*](\w+)\(.*\)\s*\{\s*$")
    def fn_spans(lines):
        spans, i = [], 0
        while i < len(lines):
            m = def_rx.match(lines[i])
            if m and not lines[i].startswith(("if", "for", "while", "switch", "} ", " ")):
                j = i
                while j < len(lines) and lines[j] != "}":
                    j += 1
                spans.append((i, j, m.group(1)))
                i = j
            i += 1
        return spans
    old_spans, cur_spans = fn_spans(old_l), fn_spans(cur_l)
    # top-level `_Static_assert(...);` statements (any line range from `_Static_assert(` to the line ending in `);`): compile-time only, they can write nothing
    sa_lines, in_sa = set(), False
    for k_, l_ in enumerate(cur_l):
        if l_.startswith("_Static_assert("):
            in_sa = True
        if in_sa:
            sa_lines.add(k_)
            if l_.rstrip().endswith(");"):
                in_sa = False
    old_fns = {n for _, _, n in old_spans}
    def encl(spans, idx):
        for a_, b_, n in spans:
            if a_ <= idx <= b_:
                return n
        return None
    layout_rx = re.compile(r"GCI_|pc_save_write_gci|pc_save_load|OTHERS_SIZE|mCD_|put_be|get_be|bswap|sizeof\((?:Save|CARDDir|Private)|"
                           r"\b(?:fwrite|fread|fseek|calloc|malloc|memcpy|fopen|rename)\(|\.length|\.gci")
    # D3-4 hook / M-G refusal / M-A runtime-path spellings of existing lines (exact, stripped):
    allow = {
        "int ok = pc_save_write_gci_to(pc_gci_path(), pc_gci_tmp_path());",                 # D3-4 hook wrapper (the writer is pinned above)
        "pc_net_game_record_after_gci_save(pc_gci_path());",
        "if (memcmp(file_data + GCI_OTHERS_OFFSET + (PC_TS_MARKER_OFF - PC_TS_OTHERS_OFF), PC_TS_MARKER_TEXT, PC_TS_MARKER_LEN) == 0) {",  # M-G (inside the pinned reader)
        'pc_card_a_dir() "/DobutsunomoriP_MURA.gci",',                                         # M-A: pc_save_scan_gci_dir candidate paths (old / new spelling)
        'pc_card_a_dir() "/8P-GAFE-DobutsunomoriP_MURA.gci",',
        'snprintf(gci_name_a, sizeof(gci_name_a), "%s/DobutsunomoriP_MURA.gci", pc_card_a_dir());',
        'snprintf(gci_name_b, sizeof(gci_name_b), "%s/8P-GAFE-DobutsunomoriP_MURA.gci", pc_card_a_dir());',
        # M-A: the legacy-file migration is skipped inside a town dir (a pure guard around the existing pc_save_migrate_legacy() call)
        "if (!pc_card_town_dir_active()) { /* M-A: a town dir must never pull save/DobutsunomoriP_MURA.gci into itself (the legacy file is left alone) */",
        '#define pc_gci_path()       pc_card_a_dir() "/" PC_GCI_FILENAME',                    # normalisation artefact of the old PC_GCI_PATH define
        '#define pc_gci_tmp_path()   pc_card_a_dir() "/" PC_GCI_FILENAME ".tmp"',
    }
    sm = difflib.SequenceMatcher(None, old_l, cur_l, autojunk=False)
    offending, n_changed = [], 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        n_changed += (i2 - i1) + (j2 - j1)
        for i in range(i1, i2):                      # removed lines: only the allow-list may match the layout regex
            l = old_l[i]
            if layout_rx.search(l) and not re.match(r"\s*(/\*|\*|//)", l) and l.strip() not in allow:
                offending.append("-" + l.strip()[:70])
        for j in range(j1, j2):
            l = cur_l[j]
            if not layout_rx.search(l) or re.match(r"\s*(/\*|\*|//)", l) or l.strip() in allow:
                continue
            fn = encl(cur_spans, j)
            if fn is None:
                if "PC_TS_" in l or "s_ts_" in l or j in sa_lines:   # top-level sanitize-template constants / buffers / compile-time _Static_asserts
                    continue
            elif fn not in old_fns:
                continue
            offending.append("+" + (fn or "<top>") + ": " + l.strip()[:70])
    check(f"S9 pc_m_card.c diff vs 0c9bc72 (observers stripped both sides, paths normalised; {n_changed} changed lines) touches no GCI layout / serialisation line outside new functions + the allow-list (offending: {offending[:6]})",
          not offending)
    # (3) no new file-writing primitive outside the new functions: fopen(wb) / fwrite / rename counts per pre-existing function are unchanged
    wr_rx = re.compile(r'fopen\([^;]*"[wa]b?\+?"|\bfwrite\(|\brename\(')
    def wr_by_fn(lines, spans):
        out = {}
        for k, l in enumerate(lines):
            if wr_rx.search(l) and not re.match(r"\s*(/\*|\*|//)", l):
                n = encl(spans, k) or "<top>"
                out[n] = out.get(n, 0) + 1
        return out
    wo, wc = wr_by_fn(old_l, old_spans), wr_by_fn(cur_l, cur_spans)
    grown = {n: c for n, c in wc.items() if n in old_fns and c > wo.get(n, 0)}
    check(f"S9 no new fopen(wb)/fwrite/rename in any function that existed at 0c9bc72 (grown: {grown}; new-function writers: "
          f"{sorted(n for n in wc if n not in old_fns)})", not grown and "<top>" not in wc)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
