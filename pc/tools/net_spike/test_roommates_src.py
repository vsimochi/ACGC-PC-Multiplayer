#!/usr/bin/env python3
"""test_roommates_src.py - ROOMMATES / MOVE-IN: source audit (docs/multiplayer-guest-roadmap.md "Roommates / move-in").

TIER: SOURCE AUDIT (+ the native selftest of pc_roommates.c). Usage: python test_roommates_src.py
  W  wire: id 104 = PC_ROOM_MSG_ID (static assert), 300 bytes, no new TXN kind / reason / town service, the capability is bit 1 of the 102 CAPS `total`,
     a client sends 104 only after it, old peers: the host drops a malformed 104 without a violation, the client ignores it unless READY
  H  host: requests validated (shape, READY + bound, rate 8 / 2 s, a live gyroid + the requester next to it), roommates.ini written BEFORE the answer and undone on a
     failed write, the guest-table housekeeping only while the guest store is trusted, no private_data[] / homes[] subscript by a guest index
  R  rights: the house push to a guest = its shared house only, the outdoor-furniture resident rights include a roommate, the house COMMIT gate is unchanged
  C  client: the door hooks (no knock for the shared house, a shut door for every other house of a guest) are TARGET_PC additions only; the show-out after a demotion
     waits for an idle player and uses the exit data of the door; the menu consumes the pad while open and is drawn after the emote wheel; the notice line
  S  switches + test hooks: settings.ini roommates / AC_MP_ROOMMATES=0, the TEST-ONLY hooks gated by PC_NET_TEST_HOOKS + AC_TEST_HOOKS
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
results = []


def rd(*p):
    with open(os.path.join(*p), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def ck(desc, cond):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def body(src, head):
    i = src.find(head)
    if i < 0:
        return ""
    j = src.find("\n}\n", i)
    return src[i:j + 3]


ng = rd(PC, "src", "pc_net_game.c")
ri = rd(PC, "src", "pc_net_game_roommates.c_inc")
rh = rd(PC, "include", "pc_roommates.h")
rc = rd(PC, "src", "pc_roommates.c")
uh = rd(PC, "include", "pc_room_ui.h")
uc = rd(PC, "src", "pc_room_ui.c")
od = rd(PC, "src", "pc_ofurn_draw.c")
ofh = rd(PC, "include", "pc_outdoor_furniture.h")
pad = rd(PC, "src", "pc_pad.c")
gr = rd(ROOT, "src", "graph.c")
pm = rd(PC, "src", "pc_pause_menu.c")
mh = rd(ROOT, "src", "actor", "ac_my_house_move.c_inc")
pcm = rd(ROOT, "src", "game", "m_player_common.c_inc")
st = rd(PC, "src", "pc_settings.c")
ded = rd(PC, "src", "pc_dedicated.c")
cm = rd(PC, "CMakeLists.txt")

# ---- W
ck("W1 PC_NETGAME_MSG_ROOMMATE = 104 in the enum, asserted equal to PC_ROOM_MSG_ID; PCRoomMsg is 300 bytes (static asserts in both files)",
   re.search(r"PC_NETGAME_MSG_ROOMMATE\s*=\s*104,", ng) and '_Static_assert(PC_ROOM_MSG_ID == (unsigned)PC_NETGAME_MSG_ROOMMATE, "ROOMMATE id");' in ri
   and "#define PC_ROOM_MSG_ID     104" in rh and "#define PC_ROOM_MSG_SIZE   300" in rh and "(sizeof(PCRoomMsg) == PC_ROOM_MSG_SIZE)" in rc
   and "sizeof(PCRoomMsg) == PC_ROOM_MSG_SIZE && sizeof(PCRoomMsg) <= PC_NET_MAX_PAYLOAD" in ri)
kinds = [int(v) for v in re.findall(r"#define PC_NETGAME_TXN_KIND_\w+\s+(\d+)u", ng)]
reasons = [int(v) for v in re.findall(r"#define PC_NETGAME_TXN_REASON_\w+\s+(\d+)u", ng)]
ck("W2 no new TXN kind (max %d = 22), reason (max %d = 62) or town service (TS_NUM 11)" % (max(kinds), max(reasons)),
   max(kinds) == 22 and max(reasons) == 62 and "#define PC_NETGAME_TS_NUM      11u" in ng and "ROOMMATE" not in " ".join(re.findall(r"#define (PC_NETGAME_TXN_\w+)", ng)))
ck("W3 the capability: PC_OFURN_C4_ROOMMATES = bit 1 of the 102 CAPS total (C3 storage keeps bit 0); the host ORs it in, the client reads it with its own mask",
   "#define PC_OFURN_C4_ROOMMATES 0x02u" in ofh and "#define PC_OFURN_C3_STORAGE 0x01u" in ofh
   and "m.total = pcnetgame_ofurn_c3_flags() | pcnetgame_room_caps_bit();" in ng and "pcnetgame_room_client_caps(m->total);" in ng
   and "const int on = (total & PC_OFURN_C4_ROOMMATES) != 0;" in ri and "s_ofcl.c3_flags = m->total & PC_OFURN_C3_STORAGE;" in ng)
on = body(ri, "int pc_net_game_room_on(void) {")
snd = body(ri, "uint32_t pc_net_game_room_send(int op, int ut_x, int ut_z, int item, const uint8_t* guest_key) {")
ck("W4 a client sends 104 only while READY and after the host's capability bit (an old host never sees 104); every message is validated before it is sent",
   "return s_role == PC_NETGAME_ROLE_CLIENT && s_client_link == PC_NETGAME_LINK_READY && s_rmc.caps;" in on and "if (!pc_net_game_room_on()" in snd
   and "if (!pc_room_msg_valid(&m, 1)) {" in snd and snd.index("pc_room_msg_valid(&m, 1)") < snd.index("pc_net_send(0, PC_NET_RELIABLE, &m"))
hd = body(ri, "static void pcnetgame_handle_host_roommate(PCNetPeerId peer, const uint8_t* data, uint16_t size) {")
ck("W5 host: a 104 needs a READY + bound peer and exactly 300 bytes, is validated (pc_room_msg_valid from_client) and rate limited (8 / 2 s); a bad one is dropped and "
   "logged, NEVER a record violation",
   "s_host_peer_link[peer] != PC_NETGAME_LINK_READY || !s_host_peer[peer].bound_valid" in hd and "if (size != sizeof(PCRoomMsg)) {" in hd
   and "if (!pc_room_msg_valid(&in, 1)) {" in hd and "PCNG_ROOM_RATE_N" in hd and "violation" not in ri)
ck("W6 dispatch: host by id (the handler checks the size), client by exact size + id; the client handler ignores anything while not READY",
   "if (data[0] == (uint8_t)PC_NETGAME_MSG_ROOMMATE) { /* ROOMMATES" in ng and "pcnetgame_handle_host_roommate(peer, data, size);" in ng
   and "if (size == sizeof(PCRoomMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_ROOMMATE) {" in ng
   and "s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY || size != sizeof(PCRoomMsg)" in body(ri, "static void pcnetgame_handle_client_roommate("))
ck("W7 the block is included once at the end of pc_net_game.c; the pure module + the UI are built (CMake)",
   ng.count('#include "pc_net_game_roommates.c_inc"') == 1 and ng.rstrip().endswith('#include "pc_net_game_roommates.c_inc" /* ROOMMATES / MOVE-IN (message 104): the host relations, the client seam, the console */')
   and "src/pc_roommates.c" in cm and "src/pc_room_ui.c" in cm)

# ---- H
h = body(ri, "static void pcnetgame_room_handle(int peer, const PCRoomMsg* in) {")
ck("H1 every request needs a live placed gyroid at that tile with that value (all cells match) and the requester next to it (the host's copy of its position, in town)",
   "(mi = pcnetgame_room_gyroid_at((int)in->ut_x, (int)in->ut_z, (int)in->item)) < 0 || !pcnetgame_room_actor_near(peer, (int)in->ut_x, (int)in->ut_z)" in h
   and "pcnetgame_ofurn_tiles_match(&s_ofh.m[mi], &bx, &bz, &bv)" in ri and "PC_NETGAME_CTX_FLAG_IN_TOWN" in body(ri, "static int pcnetgame_room_actor_near("))
ck("H2 roommates.ini is written BEFORE the RESULT goes out; a failed write restores the table (FAULT, nothing changed)",
   "if (r == PC_ROOM_R_OK && !pcnetgame_room_host_save()) {" in h and "memcpy(&s_rmh, &undo, sizeof(s_rmh)); /* nothing changed */" in h
   and h.index("pcnetgame_room_host_save()") < h.rindex("pcnetgame_room_reply_send(peer, &out);"))
ck("H3 only the holder decides (ACCEPT / DENY / REMOVE: the requester's key = the gyroid holder's), only a guest asks / withdraws / moves out",
   "if (memcmp(me, hkey, PC_ROOM_KEY_LEN) != 0) {\n                    r = PC_ROOM_R_NOT_OWNER;" in h and h.count("kind != PC_OPS_KIND_GUEST") >= 3)
gc = body(ri, "static void pcnetgame_room_host_gc(void) {")
ck("H4 housekeeping never prunes against an unknown guest table (UNTRUSTED / not loaded) and an unreadable roommates.ini refuses changes (BUSY) without rewriting it",
   "if (s_rmh_state != 1 || !s_guest_store_loaded || s_guest_untrusted) {" in gc and "s_rmh_state = rc == PC_ROOM_FILE_BAD ? -1 : 1;" in ri
   and "pcnetgame_room_host_ensure() < 0 && in->op != PC_ROOM_OP_QUERY" in h)
ck("H5 no guest index ever subscripts private_data[] / homes[] (the block indexes them only with resident loops 0..3 or a homes[] index from the save)",
   not re.search(r"private_data\)\[(?:idx|peer|g|slot)\]", ri) and not re.search(r"homes\[(?:idx|peer|g)\]", ri))
ck("H6 an unheld gyroid is claimed only by a RESIDENT with a house (the placer fields rewritten + the sidecar saved, undone on a failed save)",
   "if (kind == PC_OPS_KIND_RESIDENT && pcnetgame_room_house_of_key(me) >= 0 && pcnetgame_room_claim(mi, me," in ri
   and "if (!pcnetgame_ofurn_host_save()) {\n        *mk = before;" in body(ri, "static int pcnetgame_room_claim("))

# ---- R
ck("R1 the house push: a guest peer only while it is a ROOMMATE, and only its shared house; the commit gate (idx >= PLAYER_NUM -> NOT_OWNER) is untouched",
   "(st->bound_class == (uint8_t)PC_NETGAME_REC_CLASS_GUEST && !pcnetgame_room_peer_is_roommate((PCNetPeerId)p))" in ng
   and "(st->bound_class != (uint8_t)PC_NETGAME_REC_CLASS_GUEST || pcnetgame_room_peer_house(peer) == h)" in ng
   and ng.count("idx >= PLAYER_NUM") >= 2 and "NOT_OWNER" in body(ng, "static void pcnetgame_house_handle_begin("))
ck("R2 outdoor furniture: a roommate has a resident's rights (PERMS re-sent when they change); a new roommate gets its shared house pushed at once",
   "(s_host_peer[peer].bound_class == (uint8_t)PC_NETGAME_REC_CLASS_RESIDENT || pcnetgame_room_peer_is_roommate(peer))" in ng
   and "s_host_peer[p].hsent_seq[house] = 0;" in h)

# ---- C
ck("C1 the knock: a TARGET_PC block in the foreigner branch only; a roommate of that house walks in (no knock), every other case unchanged",
   "#ifdef TARGET_PC\n                    /* roommates (pc_room_ui.h): a guest walks into the house it shares like the holder does (no knock) */\n"
   "                    extern int pc_net_game_room_house_entry(int house);\n                    if (pc_net_game_room_house_entry(arrange_idx) > 0) {\n"
   "                        return FALSE;\n                    }\n#endif\n                    return TRUE;" in pcm)
ck("C2 the door: a TARGET_PC block before `res = TRUE`; a guest of a roommates host that does not live there gets a shut door + the notice",
   "if (pc_net_game_room_house_entry(my_house->action & 3) < 0) {\n                    pc_net_game_room_door_refused();\n                    return FALSE;\n                }\n#endif\n                res = TRUE;" in mh)
he = body(ri, "int pc_net_game_room_house_entry(int house) {")
ck("C3 the entry rule is a READY client of a roommates host, a guest player, house 0..3; a roommate only when the local house h is held by the STATUS holder",
   "!s_rmc.caps" in he and "!pcnetgame_client_is_guest_player()" in he and "return pcnetgame_room_client_lives_in(house) ? 1 : -1;" in he
   and "return mPr_CheckCmpPersonalID(&Save_Get(homes[h]).ownerID, &p) == TRUE;" in body(ri, "static int pcnetgame_room_client_lives_in(int h) {"))
ct = body(ri, "static void pcnetgame_room_client_tick(void) {")
ck("C4 the show-out after a demotion: no wipe / fade, no message / submenu (a demo holds it 10 s at most), a valid exit door (never scene 0 / the same scene), then the vanilla walk-out calls",
   "mPlib_able_submenu_type1((GAME*)play) == FALSE || mMsg_Check_MainHide(mMsg_Get_base_window_p()) == FALSE || play->submenu.process_status != mSM_PROCESS_WAIT ||" in ct
   and "(mDemo_CheckDemo() != FALSE && (uint32_t)(pcnetgame_now_ms() - s_rmc.eject_t0) < PCNG_ROOM_EJECT_DEMO_MS)" in ct
   and "Common_Get(structure_exit_door_data).next_scene_id == 0 || Common_Get(structure_exit_door_data).next_scene_id == play->scene_id" in ct
   and "goto_other_scene(play, Common_GetPointer(structure_exit_door_data), TRUE)" in ct and "aMR_SaveWaltzTempo2();" in ct and "mBGMPsComp_make_ps_wipe(0x195);" in ct)
ui_in = body(uc, "void pc_room_ui_input(")
ck("C5 the menu owns the pad while open (buttons / sticks zeroed), is called in PADRead after the emote wheel and drawn after it; the notice line at y 94",
   "*buttons = 0;\n    *stick_x = *stick_y = 0;\n    *cstick_x = *cstick_y = 0;" in ui_in and "pc_room_ui_input(&buttons, &stickX, &stickY, &cstickX, &cstickY);" in pad
   and pad.index("pc_emote_wheel_input(") < pad.index("pc_room_ui_input(") and "    pc_room_ui_draw(game);" in gr and gr.index("pc_emote_wheel_draw(game);") < gr.index("pc_room_ui_draw(game);")
   and "pc_menu_draw_centered(game, rm_line, 94.0f," in pm)
ck("C6 every decision asks Yes / No with No preselected; the gyroid menu is opened from the A seam (guest: the press, resident: the tap) only when the host offers it",
   "s_csel = 0; /* \"No\" first" in uc and "pc_net_game_room_is_gyroid((int)pc_ofurn_tile_value(&mk)) && pc_net_game_room_on()" in od
   and "pc_room_ui_open((int)tapped.ut_x, (int)tapped.ut_z, (int)pc_ofurn_tile_value(&tapped));" in od)
ck("C7 nothing in the UI / client seam writes game state (no pocket / wallet / house / save write)",
   not re.search(r"(inventory|pockets|wallet|homes\[[^\]]*\]\.[a-z_]+\s*=|Save_Set)", uc) and not re.search(r"\binventory\b|\bwallet\b", ri))

# ---- S
ck("S1 switches: settings.ini [Network] roommates (default 1, read + written), env AC_MP_ROOMMATES=0 (host: no bit + RESULT OFF; client: no menu)",
   ".roommates = 1," in st and 'strcmp(key, "roommates") == 0' in st and 'fprintf(f, "roommates = %d\\n", g_pc_settings.roommates);' in st
   and 'getenv("AC_MP_ROOMMATES")' in ri and "g_pc_settings.roommates != 0" in ri and "if (!pcnetgame_room_host_on()) {\n        r = PC_ROOM_R_OFF;" in h)
th = ri[ri.index("#ifdef PC_NET_TEST_HOOKS\n/* TEST-ONLY"):]
ck("S2 the TEST-ONLY hooks (AC_TEST_ROOM_GYROID / _ENTER / AC_TEST_ROOM_UI) live in PC_NET_TEST_HOOKS blocks and need AC_TEST_HOOKS=1",
   "if (!pc_test_hooks_enabled() ||" in th and "#ifdef PC_NET_TEST_HOOKS\n    pcnetgame_room_test_hooks();\n#endif" in ri
   and "#ifdef PC_NET_TEST_HOOKS\n#include \"pc_test_hooks.h\"" in uc and "if (!pc_test_hooks_enabled() || sc == NULL" in uc)
ck("S3 the console: `roommates` list / remove / clear (confirm word) / owner, listed in help",
   'strcmp(cmd, "roommates") == 0' in ded and "pc_net_game_dedicated_roommates(args, pc_ded_out_line);" in ded and "roommates [list] | remove <guest> confirm" in ded
   and 'if (strcmp(a2, "confirm") != 0) {' in ri)

# ---- native selftest
r = subprocess.run([sys.executable, os.path.join(HERE, "test_roommates_native.py")], capture_output=True, text=True)
ck("N the native selftest of pc_roommates.c passes (test_roommates_native.py)", r.returncode == 0 and "ALL PASS" in r.stdout)

print("-" * 60)
print("%d/%d checks passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
