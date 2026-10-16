"""test_fishing_view_src.py - SOURCE AUDIT of worker P3 (fishing / bug-catching on puppets): BOBBER_VIEW (id 97) + PLAYER_ACTION kind 2 CATCH.

  W  wire: protocol version still 8; PC_NETGAME_MSG_BOBBER_VIEW = 97 (asserted equal to PC_FV_MSG_BOBBER_VIEW); PCBobberViewMsg pinned field by field with its
     24-byte _Static_assert; PLAYER_ACTION stays the frozen 10-byte struct (kind 2 = a new #define only); the client dispatch matches the exact size; the HOST
     has no handler for id 97 (a client-originated one falls into 'unrecognized: ignore')
  R  relay scope: the host notes BOBBER_STATE only AFTER the fish-AI proxy was fed (pcwld_host_set_bobber first); active views go only to READY peers whose
     announced scene is the angler's, never back to the angler, UNRELIABLE; the 'gone' is RELIABLE; the relay tick runs from the host poll; a disconnect drops
  C  CATCH: emitted after the commit (x3 grant: after pcnetgame_commit_caught_despawn; host-local: inside the accepted branch); the client routes kind 2 before
     the PICKUP check; an old receiver's PICKUP check (kind != PICKUP -> ignored) is unchanged
  S  safety: no wildlife / fish / catch / pocket / field / effect-pool write and no actor creation in the P3 blocks (pc_net_game.c, pc_remote_player.c) or in
     pc_fishing_view.c; the only pcwld calls are the read-only pcwld_client_collect_bobber / pcwld_find_by_id
  P  puppet: the NOTICE_NET row (GET_M1 + BODY_CONTINUE, net NET_GET_M), the dw call, the vanilla tol_uki models, the YAJIRUSHI drawn directly (no
     effect_make_proc / effect_kill_proc), rows 49-57 unchanged
  T  pc_fishing_view.c is in the game build; the native selftest includes the real module, builds and passes
Run: python test_fishing_view_src.py
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, HERE)
import wire_baseline  # noqa: E402

results = []


def check(desc, cond):
    results.append(bool(cond))
    print(("PASS - " if cond else "FAIL - ") + desc)


def rd(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def block(text, start, end):
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j]


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def main():
    gc = rd("pc/src/pc_net_game.c")
    gh = rd("pc/include/pc_net_game.h")
    fh = rd("pc/include/pc_fishing_view.h")
    fc = rd("pc/src/pc_fishing_view.c")
    rp = rd("pc/src/pc_remote_player.c")
    cm = rd("pc/CMakeLists.txt")

    # ---- W
    check("W protocol version is still 8", wire_baseline.header_protocol_ok(gh) and wire_baseline.EXPECTED_PROTOCOL_VERSION == 8)
    ids = dict((n, v) for n, v in wire_baseline.c_message_ids_raw(gc))
    vals = sorted(ids.values())
    check("W PC_NETGAME_MSG_BOBBER_VIEW = 97, ids unique", ids.get("PC_NETGAME_MSG_BOBBER_VIEW") == 97 and len(vals) == len(set(vals)))
    check("W the id is asserted equal to the header constant", "_Static_assert((int)PC_NETGAME_MSG_BOBBER_VIEW == (int)PC_FV_MSG_BOBBER_VIEW" in gc
          and "#define PC_FV_MSG_BOBBER_VIEW 97u" in fh)
    bv = re.search(r"typedef struct PCBobberViewMsg \{(.*?)\} PCBobberViewMsg;", fh, re.S)
    norm = " ".join(strip_comments(bv.group(1)).split()) if bv else None
    check("W PCBobberViewMsg pinned: %s" % norm,
          norm == "uint8_t msg_type; uint8_t origin; uint8_t active; uint8_t scene_id; uint8_t uki_status; int8_t gyo_status; uint8_t rod_type; uint8_t rsv; "
                  "float x, y, z; int16_t angle; uint16_t seq;")
    check("W size assert 24", '_Static_assert(sizeof(PCBobberViewMsg) == 24, "PCBobberViewMsg wire size drifted");' in fh)
    pa = re.search(r"typedef struct PCNetGamePlayerActionMsg \{(.*?)\} PCNetGamePlayerActionMsg;", gc, re.S)
    check("W PLAYER_ACTION struct unchanged (10 bytes, same members)",
          pa and " ".join(strip_comments(pa.group(1)).split()) == "uint8_t msg_type; uint8_t net_player_id; uint8_t kind; uint8_t flags; uint8_t ut_x; uint8_t ut_z; uint16_t item; uint16_t seq;"
          and '_Static_assert(sizeof(PCNetGamePlayerActionMsg) == 10' in gc)
    check("W kind 2 CATCH is a define next to kind 1 PICKUP", "#define PC_NETGAME_PLAYER_ACTION_KIND_PICKUP 1u" in gc and "#define PC_NETGAME_PLAYER_ACTION_KIND_CATCH 2u" in gc)
    check("W the client dispatch matches the exact size", "if (size == sizeof(PCBobberViewMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_BOBBER_VIEW) {" in gc)
    host_disp = block(gc, "static void pcnetgame_handle_host_data(", "/* Client side: the host's raw PC_NET_EVENT_DATA payload. */")
    check("W the HOST dispatch has no BOBBER_VIEW handler (client-originated id 97 is ignored)", "PC_NETGAME_MSG_BOBBER_VIEW" not in host_disp)

    # ---- R
    hbs = block(gc, "static void pcnetgame_handle_host_bobber_state(PCNetPeerId peer, const PCNetGameBobberStateMsg* in) {", "\n}\n")
    check("R BOBBER_STATE is noted for the view AFTER the fish-AI proxy was fed (unchanged order)",
          re.search(r"pcwld_host_set_bobber\(&b\);\s*pcnetgame_fv_host_note_state\(peer, in\);", hbs) is not None)
    bvb = block(gc, "/* ===== P3 FISHING VIEW BEGIN (worker P3): BOBBER_VIEW (id 97)", "/* ===== P3 FISHING VIEW END (BOBBER_VIEW) ===== */")
    tick = block(bvb, "static void pcnetgame_fv_host_tick(void) {", "\n}\n")
    check("R active views only to READY peers in the angler's scene, never back to the angler",
          "if (i == origin || s_host_peer_link[i] != PC_NETGAME_LINK_READY)" in tick
          and "if (m.active && !(pc_remote_player_get_scene((PCNetPlayerId)i, &sc) && sc.scene_id == m.scene_id))" in tick)
    check("R active = UNRELIABLE, gone = RELIABLE", "rel ? PC_NET_RELIABLE : PC_NET_UNRELIABLE" in tick and "*reliable = 1;" in fc and "*reliable = 0;" in fc)
    check("R the angler's scene must be the FIELD (client note) / the host's own (own bobber)",
          "sc.kind == (uint8_t)PC_NETSCENE_KIND_FIELD" in bvb and "s_local_scene.kind == (uint8_t)PC_NETSCENE_KIND_FIELD" in bvb)
    check("R plausibility vs. the angler's last MOVE position", "pc_remote_player_get_last_position((PCNetPlayerId)peer" in bvb and "PC_FV_MAX_REACH" in fc)
    check("R rate bound: one active view per angler per 100 ms", "#define PC_FV_SEND_INTERVAL_MS 100u" in fh and "PC_FV_SEND_INTERVAL_MS" in fc)
    check("R the relay tick runs from the host poll; a disconnect owes a 'gone'",
          "pcnetgame_fv_host_tick();   /* worker P3" in gc and "pcnetgame_fv_host_drop_peer((int)ev.peer);" in gc)
    cbv = block(bvb, "static void pcnetgame_handle_client_bobber_view(", "\n}\n")
    check("R the client never draws its own bobber from a view and filters by its own scene",
          "s_client_assigned_peer_id" in cbv and "s_local_scene.valid, s_local_scene.scene_id" in cbv)

    # ---- C
    x3 = block(gc, "    /* 13. a catch's WILDLIFE_DESPAWN broadcast goes out BEFORE the results", "pcnetgame_txn_send_applied(peer, idx, in, slot")
    check("C x3: kind 2 is emitted right after the committed despawn", re.search(r"pcnetgame_commit_caught_despawn\(cr->entity_id\);\s*pcnetgame_fv_host_emit_catch\(\(int\)peer, expect\);", x3) is not None)
    hl = block(gc, "int pc_net_game_host_local_wildlife_catch(uint32_t entity_id, int claimed_species) {", "\n}\n")
    check("C host-local: emitted only inside the accepted branch, the item read-only before the commit",
          re.search(r"if \(accepted\) \{.*?pcnetgame_commit_caught_despawn\(entity_id\);\s*if \(s_fv_local_catch_item != 0u\) \{\s*pcnetgame_fv_host_emit_catch", hl, re.S) is not None
          and "pcwld_find_by_id(entity_id, &fv_rec)" in hl)
    hcp = block(gc, "static void pcnetgame_handle_client_player_action(", "\n}\n")
    check("C the client routes kind 2 before the PICKUP checks (which are unchanged: an old receiver ignores kind 2)",
          hcp.index("PC_NETGAME_PLAYER_ACTION_KIND_CATCH") < hcp.index("if (in->kind != (uint8_t)PC_NETGAME_PLAYER_ACTION_KIND_PICKUP || in->flags != 0"))
    pab = block(gc, "/* ===== P3 FISHING VIEW BEGIN (worker P3): PLAYER_ACTION kind 2 CATCH", "/* ===== P3 FISHING VIEW END (PLAYER_ACTION kind 2) ===== */")
    check("C kind 2 shares the per-origin seq with PICKUP and goes to the origin's scene only",
          "s_action_seq_out[origin] = (uint16_t)(s_action_seq_out[origin] + 1u);" in pab and "sc.scene_id != osc.scene_id" in pab and "s_action_last_seq[origin] = in->seq;" in pab)

    # ---- S
    rpb = block(rp, "/* ===== P3 FISHING VIEW BEGIN (worker P3): bobber + caught creature on the puppets.", "/* ===== P3 FISHING VIEW END ===== */")
    forbidden = re.compile(r"pcwld_(?!client_collect_bobber|find_by_id)\w+\s*\(|Actor_info_make_actor|make_actor_proc|effect_make_proc|effect_kill_proc|Player_actor_putin_item|"
                           r"mSM_COLLECT|Common_Set\(|pcfa_set_tile|mFI_SetFG|mPr_Set|uki\w*->\w+\s*=[^=]|pc_net_game_request_catch|pcnetgame_validate_and_commit_catch|"
                           r"pcwld_remove_by_id|pockets\[")
    for name, txt in (("pc_fishing_view.c", fc), ("the pc_net_game.c BOBBER_VIEW block", bvb), ("the pc_net_game.c CATCH block", pab), ("the pc_remote_player.c P3 block", rpb)):
        bad = forbidden.findall(strip_comments(txt))
        check("S no wildlife / catch / pocket / field / effect-pool write or actor creation in %s (%s)" % (name, bad), not bad)
    check("S the puppet block draws only (display lists + the bg_item single draw), no sound / effect slot",
          "sAdo_" not in strip_comments(rpb) and "single_draw_proc" in rpb and "gSPDisplayList" in rpb)

    # ---- P
    check("P the NOTICE_NET row holds PULL_NET's clips (GET_M1, BODY_CONTINUE | ITEM_CARRY, NET_GET_M)",
          re.search(r'\[mPlayer_INDEX_NOTICE_NET\] = PCROW\("notice_net", mPlayer_ANIM_GET_M1, PC_ROW_A1_SAME, PC_NORMAL, PC_STOP, 0\.5f, 1\.0f, '
                    r'PC_ROWF_BODY_CONTINUE \| PC_ROWF_ITEM_CARRY, -1,\s*mPlayer_ITEM_DATA_NET_GET_M, PC_STOP', rp) is not None)
    check("P NOTICE_ROD row unchanged (GET_T2)", '[mPlayer_INDEX_NOTICE_ROD] = PCROW("notice_rod", mPlayer_ANIM_GET_T2' in rp)
    check("P RELEASE_CREATURE stays the WAIT1 idle fallback (vanilla's clip)", "[mPlayer_INDEX_RELEASE_CREATURE] = PCFB(" in rp)
    check("P the dw draws the P3 view after the carried item and the pickup item",
          re.search(r"pc_remote_player_draw_pickup_item\(self, game\);[^\n]*\n\s*pc_fv_pp_draw\(self, game\);", rp) is not None)
    check("P the bobber is the vanilla tol_uki1 / tol_uki2 display list", "s->rod_type ? tol_uki2_model : tol_uki1_model" in rpb)
    check("P the bobber is only drawn for a rod-carrying puppet", "self->visual.held_class == PC_HELD_ROD && pc_net_game_bobber_view_get(peer, &s, &since)" in rpb)
    check("P the YAJIRUSHI is the ef_yajirushi draw (ef_kore_modelT), for killifish / ant / mosquito",
          "ef_kore_modelT" in rpb and "aGYO_TYPE_KILLIFISH" in rpb and "aINS_INSECT_TYPE_ANT" in rpb and "aINS_INSECT_TYPE_MOSQUITO" in rpb)
    check("P every ground-item draw of a creature is gated to fish / junk (an insect CAGE item has no FIELD model: it crashed the observer)",
          strip_comments(rpb).count("single_draw_proc(") == 2 and "if (pc_fv_pp_item_drawable(p->shown_item)) {" in rpb
          and "scale > 0.0f && pc_fv_pp_item_drawable(p->rel_item)" in rpb and "== mNT_ITEM_TYPE_FISH" in rpb)
    check("P [NET][PUPPET][DIAG] lines for the bobber / catch / release",
          "bobber drawn" in rpb and "catch shown item=" in rpb and "release shown item=" in rpb)

    # ---- T
    check("T pc_fishing_view.c is in the game build", "src/pc_fishing_view.c" in cm)
    st = os.path.join(HERE, "fishing_view_selftest.c")
    check("T native selftest present and includes the real module", os.path.isfile(st) and '#include "../../src/pc_fishing_view.c"' in open(st).read())
    gcc = None
    for cand in ("gcc", r"C:\msys64\ucrt64\bin\gcc.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True, timeout=30)
            gcc = cand
            break
        except Exception:  # noqa: BLE001
            continue
    if gcc is not None:
        out = os.path.join(os.environ.get("TEMP", HERE), "fishing_view_selftest_src_audit.exe")
        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + os.pathsep + r"C:\msys64\ucrt64\bin"
        cp = subprocess.run([gcc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Iinclude", "tools/net_spike/fishing_view_selftest.c", "-lm", "-o", out],
                            cwd=os.path.join(ROOT, "pc"), capture_output=True, text=True, timeout=120, env=env)
        ok = cp.returncode == 0
        if not ok:
            print(cp.stderr[-2000:])
        if ok:
            rp2 = subprocess.run([out], capture_output=True, text=True, timeout=60, env=env)
            ok = rp2.returncode == 0
            print("INFO - " + rp2.stdout.strip().splitlines()[-1] if rp2.stdout.strip() else "")
        check("T the native selftest builds (-Werror) and passes", ok)
    else:
        print("INFO - gcc not found: native selftest not run here")
    print("\n%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
