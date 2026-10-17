#!/usr/bin/env python3
"""test_chat_src.py - MULTIPLAYER TEXT CHAT (worker CH), source pins. No game process.

  W  wire: message ids 100 CHAT_SEND / 101 CHAT_MSG (enum + pc_chat.h constants), the exact struct bodies (68 / 80 bytes, _Static_asserts), the flags
  R  relay scope: the host relays only to READY peers whose announced scene id AND owner equal the speaker's, plus the speaker's own echo; the listen host presents
     only for its own line or a same-scene line; a SERVER announcement (`say`) goes to every READY peer
  H  host validation order: READY, the bound identity, the exact size, pc_chat_send_valid, the chat switch, the rate limiter, the mute list, sanitize -> relay;
     a client-originated CHAT_MSG is dropped; every relayed line is logged as "[CHAT] name: text"
  C  client: CHAT_MSG only while READY, validated first; CAPS sets the capability (per session, reset on link loss); CHAT_SEND only when available
  O  old peers: both dispatchers route 100 / 101 to the chat handlers only; nothing else (old peers ignore unknown ids: test_old_host_compat_src.py O1 / O8)
  S  safety: the chat code (pc_chat.c, pc_chat_ui.c, the CHAT block) writes no item / bell / world / save state and never interprets the text
  K  the chat key: keybindings.ini Chat (default T), free-play gate (no menu / message / demo / pause / wheel / text field / title / wipe), the open line owns the
     keyboard (pc_main.c before F4 / Esc), the local pad is zeroed in PADRead before the tool wheel, the opener character is swallowed, Enter / Esc drain
  D  drawing: graph.c calls pc_chat_draw after the PC menus; wall-clock timing; no heap allocation in the UI file; bubbles skipped over a submenu
  X  settings / env / console: [Network] chat = 1 default, AC_MP_CHAT=0, the console commands say / mute / unmute / mutes / chat and their help lines, mutes.ini
     next to ops.ini; the TEST-ONLY hook is behind pc_test_hook_getenv
Usage: python test_chat_src.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
results = []


def ck(d, c):
    results.append((d, bool(c)))
    print(("PASS - " if c else "FAIL - ") + d)


def rd(*p):
    return open(os.path.join(*p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def body(src, head):
    i = src.index(head)
    return src[i:src.index("\n}\n", i)]


def norm(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"\s+", " ", s).strip()


ng = rd(PC, "src", "pc_net_game.c")
ch = rd(PC, "include", "pc_chat.h")
cc = rd(PC, "src", "pc_chat.c")
ui = rd(PC, "src", "pc_chat_ui.c")
pm = rd(PC, "src", "pc_main.c")
pad = rd(PC, "src", "pc_pad.c")
gr = rd(ROOT, "src", "graph.c")
kb = rd(PC, "src", "pc_keybindings.c")
kbh = rd(PC, "include", "pc_keybindings.h")
st = rd(PC, "src", "pc_settings.c")
ded = rd(PC, "src", "pc_dedicated.c")
rp = rd(PC, "src", "pc_remote_player.c")
cm = rd(PC, "CMakeLists.txt")
blk = ng[ng.index("/* ===== CHAT BEGIN"):ng.index("/* ===== CHAT END ===== */")]

# ---- W
ck("W1 enum: PC_NETGAME_MSG_CHAT_SEND = 100, PC_NETGAME_MSG_CHAT_MSG = 101 (and no other id 100 / 101)",
   re.search(r"PC_NETGAME_MSG_CHAT_SEND\s*=\s*100,", ng) and re.search(r"PC_NETGAME_MSG_CHAT_MSG\s*=\s*101,", ng) and
   len(re.findall(r"PC_NETGAME_MSG_\w+\s*=\s*10[01],", ng)) == 2)
ck("W2 pc_chat.h ids and the cross _Static_assert in the CHAT block", "#define PC_CHAT_MSG_SEND 100" in ch and "#define PC_CHAT_MSG_MSG  101" in ch and
   "(int)PC_NETGAME_MSG_CHAT_SEND == PC_CHAT_MSG_SEND && (int)PC_NETGAME_MSG_CHAT_MSG == PC_CHAT_MSG_MSG" in blk)
sm = norm(ch[ch.index("typedef struct PCChatSendMsg {"):ch.index("} PCChatSendMsg;")])
mm = norm(ch[ch.index("typedef struct PCChatMsg {"):ch.index("} PCChatMsg;")])
ck("W3 PCChatSendMsg body pinned (u8 type, u8 len, u16 seq, u8 text[64]) = 68 bytes",
   sm == "typedef struct PCChatSendMsg { uint8_t msg_type; uint8_t len; uint16_t seq; uint8_t text[PC_CHAT_WIRE_TEXT];" and
   '_Static_assert(sizeof(PCChatSendMsg) == 68' in ch and "#define PC_CHAT_WIRE_TEXT 64" in ch)
ck("W4 PCChatMsg body pinned (type, origin, flags, len, u16 seq, scene_id, rsv, name[8], text[64]) = 80 bytes",
   mm == "typedef struct PCChatMsg { uint8_t msg_type; uint8_t origin; uint8_t flags; uint8_t len; uint16_t seq; uint8_t scene_id; uint8_t rsv; "
         "uint8_t name[PC_CHAT_NAME_LEN]; uint8_t text[PC_CHAT_WIRE_TEXT];" and '_Static_assert(sizeof(PCChatMsg) == 80' in ch)
ck("W5 flags SERVER 1 / CAPS 2 / ON 4 / NOTICE 8, max 60 codes, server origin 0xFF",
   all(x in ch for x in ("#define PC_CHAT_F_SERVER 0x01u", "#define PC_CHAT_F_CAPS   0x02u", "#define PC_CHAT_F_ON     0x04u", "#define PC_CHAT_F_NOTICE 0x08u",
                         "#define PC_CHAT_MAX_LEN   60", "#define PC_CHAT_ORIGIN_SERVER 0xFFu")))
ck("W6 both directions RELIABLE (every chat pc_net_send in the block)", blk.count("pc_net_send(") >= 5 and "PC_NET_UNRELIABLE" not in blk)

# ---- R
rl = body(ng, "static void pcnetgame_chat_host_relay(int origin, const uint8_t name[PC_CHAT_NAME_LEN], const uint8_t* text, int len, uint16_t seq) {")
ck("R1 relay loop: READY peers only; the speaker gets its echo; others only with the same scene id AND owner",
   "if (s_host_peer_link[i] != PC_NETGAME_LINK_READY) {" in rl and "if (i == origin) {" in rl and
   "sc.valid && sc.scene_id == ssc.scene_id && sc.owner == ssc.owner" in rl and "have &&" in rl)
ck("R2 the listen host presents its own line (self) or a same-scene line; never on a dedicated / observer host",
   "!g_pc_dedicated && !pc_host_observer_active()" in rl and "pc_chat_on_line(origin, 1, 0, name, text, len);" in rl and
   "s_local_scene.scene_id == ssc.scene_id && s_local_scene.owner == ssc.owner" in rl)
so = body(ng, "static int pcnetgame_chat_scene_of(int origin, PCNetPlayerScene* sc) {")
ck("R3 the speaker scene: the host's own player = s_local_scene (not for an observer / dedicated host), a peer = its announced scene",
   "s_local_scene" in so and "pc_host_observer_active() || g_pc_dedicated" in so and "pc_remote_player_get_scene((PCNetPlayerId)origin, sc) && sc->valid" in so)
dc = body(ng, "void pc_net_game_dedicated_chat(const char* cmd, const char* args, void (*out)(const char*)) {")
say = dc[dc.index('if (strcmp(cmd, "say") == 0) {'):dc.index('if (strcmp(cmd, "chat") == 0) {')]
ck("R4 `say`: flag SERVER, origin 0xFF, to EVERY READY peer (no scene filter), logged", "PC_CHAT_ORIGIN_SERVER, PC_CHAT_F_SERVER" in say and
   "if (s_host_peer_link[i] == PC_NETGAME_LINK_READY) {" in say and "scene" not in say.replace("scene_id", "") and "pcnetgame_chat_log_line(" in say)

# ---- H
hh = body(ng, "static void pcnetgame_handle_host_chat(PCNetPeerId peer, const uint8_t* data, uint16_t size) {")
order = ["s_host_peer_link[peer] != PC_NETGAME_LINK_READY", "data[0] != (uint8_t)PC_NETGAME_MSG_CHAT_SEND", "!s_host_peer[peer].bound_valid",
         "pc_chat_send_valid(&m)", "pcnetgame_chat_host_on()", "pc_chat_rate_take(&s_chat_peer[peer].rate", "pcnetgame_chat_peer_muted(peer)",
         "pc_chat_sanitize(m.text, m.len, clean)", "pcnetgame_chat_host_relay((int)peer, s_host_peer[peer].bound_pid.player_name, clean, n, m.seq);"]
pos = [hh.find(x) for x in order]
ck("H1 host order: READY, CHAT_MSG dropped, size + bound identity, shape, switch, rate, mute, sanitize, relay with the BOUND name (%s)" % pos,
   all(p >= 0 for p in pos) and pos == sorted(pos) and "size != sizeof(m)" in hh)
ck("H2 refusals answer the sender with a NOTICE (off / too fast / muted), at most one per 2 s", hh.count("pcnetgame_chat_notice(peer,") == 3 and
   "< 2000u" in body(ng, "static void pcnetgame_chat_notice(PCNetPeerId peer, const char* ascii) {"))
lg = body(ng, "static void pcnetgame_chat_log_line(const char* who, const uint8_t* text, int len, const char* detail) {")
ck("H3 every line is logged \"[CHAT] name: text\" (dedicated: pc_dedicated_say -> console + server.log)", 'pc_dedicated_say("[CHAT] %s: %s", who, u);' in lg and
   'printf("[CHAT] %s: %s\\n", who, u);' in lg and "pcnetgame_chat_log_line(nm, text, len, detail);" in rl)
ck("H4 the rate limiter: burst 3, 0.7 s (pc_chat.h); the listen host's own player has the same limit",
   "#define PC_CHAT_RATE_BURST    3" in ch and "#define PC_CHAT_RATE_REFILL_MS 700u" in ch and "pc_chat_rate_take(&s_chat_local_rate" in body(ng, "int pc_net_game_chat_send(const uint8_t* codes, int len) {"))

# ---- C
hc = body(ng, "static void pcnetgame_handle_client_chat(const uint8_t* data, uint16_t size) {")
ck("C1 client: READY + CLIENT only, exact size, pc_chat_msg_valid before anything, CAPS handled first, the text re-sanitized",
   "s_role != PC_NETGAME_ROLE_CLIENT || s_client_link != PC_NETGAME_LINK_READY" in hc and "size != sizeof(m)" in hc and
   hc.index("pc_chat_msg_valid(&m)") < hc.index("PC_CHAT_F_CAPS") < hc.index("pc_chat_sanitize(") < hc.index("pc_chat_on_line("))
ck("C2 self = the origin is this client's assigned wire id (its echo anchors on the local player)", "self = m.origin == (uint8_t)s_client_assigned_peer_id;" in hc)
ht = body(ng, "static void pcnetgame_chat_host_tick(void) {")
ck("C3 the host announces CAPS (+ON) to every READY peer once and again when the switch changes; a slot that leaves READY starts fresh",
   "!s_chat_peer[i].caps_sent || s_chat_peer[i].caps_on_sent != (uint8_t)on" in ht and "pcnetgame_chat_send_caps((PCNetPeerId)i, on);" in ht and
   "memset(&s_chat_peer[i], 0, sizeof(s_chat_peer[i]));" in ht and "pcnetgame_chat_host_tick();" in ng.replace(blk, ""))
ck("C4 the client tick runs the session reset (caps 0 + pc_chat_reset_session on link loss) and is called from pcnetgame_client_tick",
   "pcnetgame_chat_client_tick();" in body(ng, "static void pcnetgame_client_tick(void) {") and "pc_chat_reset_session();" in body(ng, "static void pcnetgame_chat_client_tick(void) {"))

# ---- O
hd = body(ng, "static void pcnetgame_handle_host_data(PCNetPeerId peer, const uint8_t* data, uint16_t size) {")
cd = body(ng, "static void pcnetgame_handle_client_data(const uint8_t* data, uint16_t size) {")
ck("O1 host dispatcher: 100 / 101 go to pcnetgame_handle_host_chat only; client dispatcher: 101 to pcnetgame_handle_client_chat only",
   "data[0] == (uint8_t)PC_NETGAME_MSG_CHAT_SEND || data[0] == (uint8_t)PC_NETGAME_MSG_CHAT_MSG) {\n        pcnetgame_handle_host_chat(peer, data, size);" in hd and
   "data[0] == (uint8_t)PC_NETGAME_MSG_CHAT_MSG) {\n        pcnetgame_handle_client_chat(data, size);" in cd and ng.count("pcnetgame_handle_host_chat(peer, data, size);") == 1 and
   ng.count("pcnetgame_handle_client_chat(data, size);") == 1)
ck("O2 both dispatchers still end in the silent 'unrecognized: ignore' (a NEWER peer's unknown id is ignored here too)",
   hd.rstrip().endswith("/* malformed / short / unrecognized: ignore rather than misinterpret */") and cd.rstrip().endswith("/* malformed/short/unrecognized: ignore */"))

# ---- S
forbid = ["Save_Set", "Common_Set(", "->inventory", "pockets", "money", "mFI_Set", "mPB_", "TXN", "txn", "Actor_info_make_actor", "Actor_delete",
          "pc_net_game_dedicated_give", "system(", "popen(", "pc_ded_execute", "mPr_SetPossessionItem", "->wallet", "field_update"]
bad = [(n, f) for n, src in (("pc_chat.c", cc), ("pc_chat_ui.c", ui), ("CHAT block", blk)) for f in forbid if f in src]
ck("S1 no item / bell / world / save writes and no command execution in the chat code (%s)" % bad, not bad)
ck("S2 the only game state the CHAT block reads for itself: the local player's name (Common_Get(now_private) read-only) and scenes",
   blk.count("Common_Get(") == 1 and "memcpy(name, pr->player_ID.player_name, PC_CHAT_NAME_LEN);" in blk)
ck("S3 text is carried as font codes, validated on both ends; code 0 / 0x7F / 0x80 never allowed",
   "s_ok[0] = 0;" in cc and "s_ok[0x7F] = 0;" in cc and "s_ok[0x80] = 0;" in cc and "pc_chat_send_valid" in hh and "pc_chat_msg_valid" in hc)

# ---- K
ck("K1 keybindings: Chat = T by default, ini key \"Chat\"", ".chat = SDL_SCANCODE_T," in kb and 'KB_ENTRY("Chat",         chat),' in kb and "PCInputCode chat;" in kbh)
he = body(ui, "int pc_chat_handle_event(const SDL_Event* e) {")
ck("K2 the key opens only: not repeated, no Ctrl/Alt/GUI, the bound scancode, a network role, pc_chat_can_open(); the capability check shows a notice",
   "!e->key.repeat" in he and "(int)e->key.keysym.scancode == g_pc_keybindings.chat" in he and "KMOD_CTRL | KMOD_ALT | KMOD_GUI" in he and
   "pc_net_game_role() == PC_NETGAME_ROLE_NONE || !pc_chat_can_open()" in he and "pc_chat_local_notice(why);" in he and
   he.index("pc_chat_can_open()") < he.index("pc_net_game_chat_available(&why)") < he.index("chat_open(k);"))
co = body(ui, "int pc_chat_can_open(void) {")
ck("K3 free play gate: play_main, not paused / wheel / observer / Play Online text / editor typing / title demo / wipe / submenu / demo / message window, a player",
   all(x in co for x in ("gamePT->exec != play_main", "g_pc_paused", "pc_tool_wheel_is_open()", "pc_host_observer_active()", "pc_play_online_menu_text_active()",
                         "g_pc_typing_mode && g_pc_editor_active", "mEv_CheckTitleDemo() != mEv_TITLEDEMO_NONE", "fb_wipe_mode != 0",
                         "submenu.process_status != mSM_PROCESS_WAIT", "submenu.menu_type != mSM_OVL_NONE", "mDemo_CheckDemo() != FALSE",
                         "!mMsg_Check_MainHide(mMsg_Get_base_window_p())", "GET_PLAYER_ACTOR_NOW() != NULL")))
ck("K4 the open line owns every key-down (Esc cancels, Enter sends, Backspace, Ctrl+V); the opener's own character is swallowed",
   "return 1; /* every key belongs to the line while it is open" in he and "SDLK_ESCAPE" in he and "SDLK_RETURN" in he and "SDLK_BACKSPACE" in he and
   "SDL_GetClipboardText" in he and "the opener's own character" in he)
kd = pm[pm.index("case SDL_KEYDOWN:"):pm.index("case SDL_MOUSEBUTTONDOWN:")]
ck("K5 pc_main.c: the chat hook runs after the Play Online text / key capture, before F4 / Esc / the pause menu; TEXTINPUT reaches it before the editor",
   kd.index("pc_play_online_menu_text_active()") < kd.index("pc_settings_menu_capture_active()") < kd.index("pc_chat_handle_event(&event)") < kd.index("SDLK_F4") <
   kd.index("SDLK_ESCAPE") and "if (pc_chat_handle_event(&event)) break;" in pm[pm.index("case SDL_TEXTINPUT:"):])
ck("K6 PADRead zeroes the local pad (buttons, sticks, triggers, the wheel opener) while pc_chat_pad_block(), BEFORE the tool wheel reads it",
   pad.index("if (pc_chat_pad_block()) {") < pad.index("pc_tool_wheel_input(wheel_kb") and "wheel_kb = wheel_pad = 0;" in pad and
   "stickX = stickY = cstickX = cstickY = 0;" in pad[pad.index("if (pc_chat_pad_block()) {"):pad.index("pc_tool_wheel_input(wheel_kb")])
pb = body(ui, "int pc_chat_pad_block(void) {")
ck("K7 after the line closes the pad stays neutral until Enter / Esc / the chat key are released (Enter is Start)", "s_drain" in pb and
   "SDL_SCANCODE_RETURN" in pb and "SDL_SCANCODE_ESCAPE" in pb and "s_drain = 1;" in body(ui, "static void chat_close(int sent) {"))
ck("K8 the client sends nothing before the host's CAPS ON: pc_net_game_chat_send requires pc_net_game_chat_available",
   "!pc_net_game_chat_available(NULL)" in body(ng, "int pc_net_game_chat_send(const uint8_t* codes, int len) {"))

# ---- D
ck("D1 graph.c draws the chat after the tool wheel (PC overlay layer)", gr.index("pc_tool_wheel_draw(game);") < gr.index("pc_chat_draw(game);") < gr.index("pc_net_notice_draw(game);"))
ck("D2 wall-clock timing (SDL_GetTicks) and no heap allocation in the UI", "SDL_GetTicks()" in ui and not re.search(r"\b(malloc|calloc|realloc)\s*\(", ui) and
   not re.search(r"\b(malloc|calloc|realloc)\s*\(", body(cc, "int pc_chat_store_add(PCChatStore* s, int speaker, int self, uint8_t flags,")))
db = body(ui, "static void chat_draw_bubbles(GAME* game, float vl, float vr, uint32_t now) {")
ck("D3 bubbles only over the live field (no submenu, not the title); the anchor is the local player or a LIVE puppet (pc_remote_player_get_draw_position)",
   "submenu.process_status != mSM_PROCESS_WAIT" in db and "mEv_CheckTitleDemo()" in db and "pc_remote_player_get_draw_position(" in ui and
   "int pc_remote_player_get_draw_position(PCNetPlayerId player_id, float* out_x, float* out_y, float* out_z) {" in rp and
   "pc_remote_player_actor_is_live(slot)" in body(rp, "int pc_remote_player_get_draw_position(PCNetPlayerId player_id, float* out_x, float* out_y, float* out_z) {"))
ck("D4 the bubble timing: 6 s + 0.1 s / char, max 12 s; log 10 s; 24 x 3 wrap; 2 bubbles per speaker; 6 log lines",
   "6000u + 100u *" in cc and "12000u" in cc and "#define PC_CHAT_LOG_SHOW_MS 10000u" in ch and "#define PC_CHAT_BUBBLE_COLS  24" in ch and
   "#define PC_CHAT_BUBBLE_LINES 3" in ch and "#define PC_CHAT_BUBBLES_PER  2" in ch and "#define PC_CHAT_LOG_SHOWN    6" in ch)

# ---- X
ck("X1 settings.ini [Network] chat = 1 by default (parsed 0/1, written back); env AC_MP_CHAT=0 kills it",
   ".chat = 1," in st and 'strcmp(key, "chat") == 0' in st and 'fprintf(f, "chat = %d\\n", g_pc_settings.chat);' in st and '"chat = 1\\n"' in st and
   'getenv("AC_MP_CHAT")' in body(ng, "int pc_net_game_chat_enabled_local(void) {"))
ck("X2 console: say / mute / unmute / mutes / chat dispatched to pc_net_game_dedicated_chat, with help lines",
   'strcmp(cmd, "say") == 0 || strcmp(cmd, "mute") == 0 || strcmp(cmd, "unmute") == 0 || strcmp(cmd, "mutes") == 0 || strcmp(cmd, "chat") == 0' in ded and
   "pc_net_game_dedicated_chat(cmd, args, pc_ded_out_line);" in ded and "say <text>" in ded and "mutes | mute <player> | unmute <player>" in ded and "chat [on|off]" in ded)
ck("X3 mutes.ini next to ops.ini (pcnetgame_ofurn_host_path), identity keys via pcnetgame_ops_resolve (ambiguous names refused)",
   'pcnetgame_ofurn_host_path(path, sizeof(path), "mutes.ini");' in blk and "pcnetgame_ops_resolve(a, c, 8);" in dc and "is ambiguous" in dc)
ck("X4 the TEST-ONLY send hook is behind pc_test_hook_getenv (PC_NET_TEST_HOOKS builds + AC_TEST_HOOKS=1) and uses the real submit path",
   'pc_test_hook_getenv("AC_TEST_CHAT_SEND")' in blk and "pc_chat_submit_utf8(e)" in blk and not re.search(r'(?<![_\w])getenv\("AC_TEST_CHAT_SEND"\)', blk))
ck("X5 CMake builds pc_chat.c and pc_chat_ui.c", "src/pc_chat.c" in cm and "src/pc_chat_ui.c" in cm)

print("-" * 60)
n = sum(1 for _d, ok in results if ok)
print("%d/%d checks passed" % (n, len(results)))
sys.exit(0 if n == len(results) else 1)
