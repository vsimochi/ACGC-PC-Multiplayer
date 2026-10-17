"""test_emote_src.py - EMOTE WHEEL source audit + the native selftest (pc_emote.c).
  A  no new player state / wire message: mPlayer_INDEX_NUM unchanged (121), no new PC_NETGAME_MSG id, ACTION_EXT layout unchanged
  M  masking: MOVE action_state + the ACTION_EXT sampler go through pcnetgame_emote_wire_index, which returns the real index unless the
     overlay is active (pc_emote_wire_index: WAIT only, never in the station zone); move_state keeps the real class
  I  no inventory / equipment / world writes in the emote code (the overlay, the wheel, the pure module)
  K  keybindings: Emote (F) + Pad_Emote (D-pad Up) entries/defaults; the settings menu lists Tool Wheel / Chat / Emote Wheel rows; only the
     emote / D-Pad Up pad pair may share a button
  G  gating: every gate input is filled from its real source; the wheels and the chat exclude each other; the text-field / chat pad blocks
     cover the emote key; full-body emotes need empty hands; cooldown 1 s; caps
  H  hooks: WAIT main (Culc / face), Player_actor_change_main_index (end), Player_actor_move (request), graph.c draw, PADRead input
  C  cue parity: every sender cue (m_player_pc_emote.c_inc) has a receiver row (pc_remote_player.c, DEMO_WAIT + clip + variant)
  S  settings.ini [Gameplay] emotes (default 1) + AC_EMOTES=0; CMake lists the files; docs
  N  native selftest (emote_selftest.c, the REAL pc_emote.c)
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
results = []


def ck(desc, ok):
    results.append((desc, bool(ok)))
    print("%s %s" % ("PASS" if ok else "FAIL", desc))


def rd(*p):
    return open(os.path.join(*p), encoding="utf-8", errors="replace").read()


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


mph = rd(ROOT, "include", "m_player.h")
cinc = rd(ROOT, "src", "game", "m_player_pc_emote.c_inc")
wait = rd(ROOT, "src", "game", "m_player_main_wait.c_inc")
mpc = rd(ROOT, "src", "game", "m_player.c")
emo = rd(PC, "src", "pc_emote.c")
emoh = rd(PC, "include", "pc_emote.h")
wh = rd(PC, "src", "pc_emote_wheel.c")
ng = rd(PC, "src", "pc_net_game.c")
rp = rd(PC, "src", "pc_remote_player.c")
kb = rd(PC, "src", "pc_keybindings.c")
kbh = rd(PC, "include", "pc_keybindings.h")
sm = rd(PC, "src", "pc_settings_menu.c")
pad = rd(PC, "src", "pc_pad.c")
tw = rd(PC, "src", "pc_tool_wheel.c")
chat = rd(PC, "src", "pc_chat_ui.c")
graph = rd(ROOT, "src", "graph.c")
st = rd(PC, "src", "pc_settings.c")
sth = rd(PC, "include", "pc_settings.h")
cm = rd(PC, "CMakeLists.txt")
aext = rd(PC, "src", "pc_action_ext.c")
aexth = rd(PC, "include", "pc_action_ext.h")

# ---- A
enum = mph[mph.index("mPlayer_INDEX_DMA"):mph.index("mPlayer_INDEX_NUM")]
n_idx = len(re.findall(r"\bmPlayer_INDEX_\w+\s*,", enum)) + 1  # + DMA itself counted by the regex? (DMA is the first entry)
n_idx = len(re.findall(r"mPlayer_INDEX_\w+", enum))
ck("A mPlayer_INDEX_NUM is still 121 (%d entries before it)" % n_idx, n_idx == 121)
ck("A PC_AEXT_INDEX_NUM still 121 and the ACTION_EXT message is still 16 bytes", "#define PC_AEXT_INDEX_NUM 121" in aexth and 'sizeof(PCActionExtMsg) == 16' in aexth)
ck("A no wire message id in the emote code", not re.search(r"PC_NETGAME_MSG_\w+\s*=", strip_comments(emoh + emo + wh + cinc)))
ck("A the emote code sends nothing itself (no pc_net_send)", "pc_net_send" not in strip_comments(emo + wh + cinc))

# ---- M
ck("M MOVE action_state is built from pcnetgame_emote_wire_index", re.search(r"msg->action_state = pcnetgame_encode_action_state\(pcnetgame_emote_wire_index\(local, NULL, NULL\), s_local_main_entry_counter\)", ng))
ck("M the ACTION_EXT sampler uses it too and fills variant / face_set only for an emote",
   re.search(r"smp\.main_index = pcnetgame_emote_wire_index\(pl, &emote_id, &emote_face\);", ng) and
   re.search(r"if \(emote_id != 0\) \{\s*smp\.variant = emote_id;\s*smp\.face_set = 1;\s*smp\.face = emote_face;", ng))
fn = ng[ng.index("static int pcnetgame_emote_wire_index"):ng.index("/* ===== EMOTE WHEEL END ===== */")]
ck("M pcnetgame_emote_wire_index returns the real index when no overlay plays", re.search(r"if \(!active\) \{\s*return local->now_main_index;", fn))
ck("M ... and masks through pc_emote_wire_index with the station zone", "pc_emote_wire_index(local->now_main_index, active" in fn and "pc_emote_station_zone(" in fn)
wfn = emo[emo.index("int pc_emote_wire_index"):emo.index("int pc_emote_station_zone")]
ck("M pc_emote_wire_index: DEMO_WAIT only for WAIT + active + outside the station", "overlay_active && real_index == PC_EMOTE_IDX_WAIT && !in_station_zone" in wfn)
ck("M move_state still classifies the REAL index", "msg->move_state = (uint8_t)pcnetgame_classify_move_state(local->now_main_index);" in ng)
ck("M the station zone equals pc_remote_arrival_logic.h", all(x in rd(PC, "include", "pc_remote_arrival_logic.h") for x in ("PCARR_STATION_X 2200.0f", "PCARR_STATION_Z 820.0f", "PCARR_STATION_RADIUS 120.0f"))
   and "x - 2200.0f, dz = z - 820.0f" in emo and "-120.0f" in emo)
ck("M the counter is bumped at the start and at the restore", cinc.count("pc_net_game_note_player_main_entry(") == 2 and "pc_net_game_note_player_main_entry(mPlayer_INDEX_DEMO_WAIT)" in cinc)
ck("M the ACTION_EXT sender honours face_set", "m.face = smp->face_set ? smp->face : pc_aext_face_rule(smp->main_index, smp->anim0);" in aext)
ck("M msg_ok does not check variant (old peers accept a nonzero one)", "variant" not in aext[aext.index("int pc_aext_msg_ok"):aext.index("uint8_t pc_aext_face_rule")])
relay = ng[ng.index("static void pcnetgame_aext_host_relay"):ng.index("static void pcnetgame_handle_host_action_ext")]
ck("M the host relay copies the message (variant untouched)", "PCActionExtMsg m = *in;" in relay and "variant" not in relay)

# ---- I
code = strip_comments(cinc + emo + wh.split("#ifdef PC_NET_TEST_HOOKS\n/* =====")[0])
bad = [p for p in (r"->equipment\s*=[^=]", r"mPr_SetPossessionItem", r"->inventory\.pockets\[[^\]]*\]\s*=[^=]", r"Save_Set", r"mFI_SetFG", r"Common_Set\(", r"->cloth\s*=[^=]",
                    r"TryPutawayTool", r"TrySelectToolSlot", r"request_main_putin_item", r"request_main_putaway")
       if re.search(p, code)]
ck("I no inventory / equipment / world write in the emote code (%s)" % bad, not bad)
test_block = wh[wh.index("#ifdef PC_NET_TEST_HOOKS\n/* ====="):]
ck("I the TEST-ONLY script only reads the inventory (hash)", not re.search(r"priv->\w+(\.\w+)*\s*=[^=]", test_block))

# ---- K
ck("K header fields", "PCInputCode emote;" in kbh and "PCPadCode emote;" in kbh)
ck("K defaults: keyboard F, pad D-pad Up", ".emote = SDL_SCANCODE_F," in kb and re.search(r"\.emote\s*= SDL_CONTROLLER_BUTTON_DPAD_UP,", kb))
ck("K ini entries Emote / Pad_Emote", 'KB_ENTRY("Emote",        emote)' in kb and 'PAD_ENTRY("Pad_Emote",      emote)' in kb)
ck("K the ini still has Wheel / Chat / Pad_Wheel", 'KB_ENTRY("Wheel",        wheel)' in kb and 'KB_ENTRY("Chat",         chat)' in kb and 'PAD_ENTRY("Pad_Wheel",      wheel)' in kb)
rows = sm[sm.index("static const BindRow s_bind_rows[] = {"):sm.index("#define BIND_ROW_COUNT")]
ck("K settings menu rows: Tool Wheel (key + pad), Chat (key only), Emote Wheel (key + pad)",
   re.search(r'BROW\("Tool Wheel",\s*wheel,\s*wheel\)', rows) and re.search(r'BROW_K\("Chat",\s*chat,\s*"-"\)', rows) and re.search(r'BROW\("Emote Wheel",\s*emote,\s*emote\)', rows))
ck("K the rows come after the D-pad rows (scroll list order)", rows.index('"D-Pad Right"') < rows.index('"Tool Wheel"') < rows.index('"Chat"') < rows.index('"Emote Wheel"'))
ap = sm[sm.index("static int bind_pad_shared_ok"):sm.index("static void bind_enter_page")]
ck("K only the emote / D-Pad Up pad pair may share (every other conflict swaps)", "offsetof(PCPadBindings, emote)" in ap and "offsetof(PCPadBindings, dpad_up)" in ap and
   "if (bind_pad_shared_ok(row, i)) continue;" in ap and "if (*other == code) { *other = *dst; break; }" in sm)
ck("K keyboard conflicts still swap over every row (chat included)", re.search(r"static void bind_assign_kb\(int row, PCInputCode code\) \{[^}]*for \(int i = 0; i < BIND_ROW_COUNT; i\+\+\)", sm))
ck("K saved to keybindings.ini on change", "if (changed) pc_keybindings_save();" in sm)

# ---- G
gate = emo[emo.index("int pc_emote_gate("):emo.index("const char* pc_emote_reason_name")]
for fld in ("enabled", "paused", "title_demo", "transition", "menu_open", "observer", "chat_open", "tool_wheel", "msg_or_demo", "no_player", "main_index", "demo_mode",
            "on_ground", "in_water", "hands_empty", "since_last_ms"):
    ck("G the gate reads %s" % fld, "in->" + fld in gate)
ck("G WAIT..DASH only", "in->main_index < PC_EMOTE_IDX_WAIT || in->main_index > PC_EMOTE_IDX_DASH" in gate)
ck("G full-body needs empty hands; cooldown only for a start", "d->full_body && !in->hands_empty" in gate and "in->since_last_ms < PC_EMOTE_COOLDOWN_MS" in gate and "#define PC_EMOTE_COOLDOWN_MS 1000u" in emoh)
bg = cinc[cinc.index("static int Player_actor_pc_emote_build_gate"):cinc.index("int pc_emote_player_gate")]
for src in ("pc_emote_enabled()", "g_pc_paused", "pc_host_observer_active()", "pc_chat_typing()", "pc_tool_wheel_is_open()", "mEv_CheckTitleDemo()", "fb_wipe_mode",
            "submenu.start_refuse", "mDemo_CheckDemo()", "mMsg_Check_MainHide(", "Player_actor_Check_is_demo_mode(player->now_main_index)",
            "Player_actor_Check_is_demo_mode(player->requested_main_index)", "result.on_ground", "result.is_in_water", "priv->equipment == EMPTY_NO"):
    ck("G build_gate uses %s" % src, src in bg)
full = {m.group(1) for m in re.finditer(r"\[PC_EMOTE_(\w+)\] = \{ \"[^\"]+\", \d+, PC_EMOTE_KIND_\w+, 1,", emo)}
ck("G full-body emotes = Hooray, Twirl, Exercise (%s)" % sorted(full), full == {"HOORAY", "TWIRL", "EXERCISE"})
ck("G caps: 10 s per emote, 60 s for the Exercise loop", emo.count("10000u }") == 6 and "s_exercise, 60000u }" in emo)
ck("G the tool wheel refuses while the emote wheel is up", "g_pc_paused || pc_emote_wheel_is_open()" in tw)
ck("G the chat refuses while the emote wheel is up", "pc_tool_wheel_is_open() || pc_emote_wheel_is_open() || pc_host_observer_active()" in chat)
ck("G the emote wheel refuses while the tool wheel / chat is up (gate)", "PC_EMOTE_R_TOOL_WHEEL" in gate and "PC_EMOTE_R_CHAT" in gate)
ck("G PADRead: the text field blocks the emote key; the chat block zeroes both emote inputs",
   re.search(r"if \(pc_play_online_menu_text_blocking\(\)\) \{\s*wheel_kb = 0;\s*emote_kb = 0;", pad) and "emote_kb = emote_pad = 0;" in pad)
ck("G PADRead: buttons / C-stick zeroed while open and the triggers too", "*buttons = 0; /* no gameplay action while the wheel is up" in wh and
   re.search(r"if \(pc_emote_wheel_is_open\(\)\) \{\s*status\[0\]\.triggerLeft = 0;\s*status\[0\]\.triggerRight = 0;", pad))
ck("G the wheel re-checks the gate every poll while open", "if (s_open) {\n        const int r = pc_emote_player_gate(0);" in wh)
ck("G the request is re-gated at the start (pc_emote_player_gate(req))", "int r = pc_emote_player_gate(req);" in cinc)
ck("G the host observer's PADRead drop still comes after the emote input", pad.index("pc_emote_wheel_input(") < pad.index("if (pc_host_observer_active()) {"))

# ---- H
ck("H WAIT main: Culc + face hooks (TARGET_PC)", re.search(r"#ifdef TARGET_PC\s*if \(!Player_actor_pc_emote_Culc\(actor, game\)\)[^\n]*\s*#endif\s*Player_actor_CulcAnimation_Wait\(actor\);", wait) and
   re.search(r"#ifdef TARGET_PC\s*if \(!Player_actor_pc_emote_face\(actor\)\)[^\n]*\s*#endif\s*Player_actor_set_eye_pattern_normal\(actor\);", wait))
ck("H change_main_index: the overlay ends on every state entry", re.search(r"pc_net_game_note_player_main_entry\(player->now_main_index\);\s*Player_actor_pc_emote_on_main_entry\(actorx, game\);", mpc))
ck("H Player_actor_move consumes the request", "Player_actor_pc_emote_check(actorx, game);" in mpc)
ck("H the overlay is included after the WAIT state (static helpers reachable)", mpc.index('m_player_main_wait.c_inc"') < mpc.index('m_player_pc_emote.c_inc"'))
ck("H graph.c draws the wheel", "pc_emote_wheel_draw(game);" in graph)
ck("H PADRead feeds the wheel", "pc_emote_wheel_input(emote_kb, emote_pad, mouse_x, mouse_y, wheel_rx, wheel_ry" in pad)
ck("H the vanilla SHOCK request for Shocked", "Player_actor_request_main_shock_all(game, 0.0f, actorx->shape_info.rotation.y, FALSE, FALSE, mPlayer_REQUEST_PRIORITY_14)" in cinc)
ck("H the vanilla clip binding (SetupItem_Base1 + InitAnimation_Base2 STOP) and the WAIT1 restore",
   "Player_actor_InitAnimation_Base2(actorx, game, st->anim, anim1_idx, 1.0f, 1.0f, st->speed, -5.0f, cKF_FRAMECONTROL_STOP, part_table_idx);" in cinc and
   "Player_actor_InitAnimation_Base1(actorx, game, mPlayer_ANIM_WAIT1, anim1_idx, 1.0f, 1.0f, 0.5f, -5.0f, part_table_idx);" in cinc)
ck("H continuous effects are killed at the end (local)", "effect_kill_proc(l_pc_emote_cues[i].id, RSV_NO)" in cinc)

# ---- C
snd_cues = re.findall(r"\{ PC_EMOTE_(\w+), mPlayer_ANIM_(\w+), ([\d.]+)f, PCEM_CUE_(\w+), (\w+), \"(\w+)\" \}", cinc)
ids = {"HOORAY": 1, "SHOCKED": 2, "SURPRISED": 3, "PRAY": 4, "PHEW": 5, "BOO": 6, "TWIRL": 7, "EXERCISE": 8}
rcv = re.findall(r"\{ mPlayer_INDEX_DEMO_WAIT, mPlayer_ANIM_(\w+), ([\d.]+)f, PC_AEXT_CUE_(\w+), (\w+), \"(emote_\w+)\", (\d+) \}", rp)
ck("C sender cues found (%d) and receiver rows (%d)" % (len(snd_cues), len(rcv)), len(snd_cues) == 8 and len(rcv) == 8)
for em, anim, fr, kind, eid, name in snd_cues:
    want_kind = {"FX": "FX", "FXC": "FXC", "SND_ARAIIKI": "SND", "SND_WEAR": "SND"}[kind]
    want_id = {"SND_ARAIIKI": "PC_TID_ARAIIKI", "SND_WEAR": "NA_SE_WEAR"}.get(kind, eid)
    ok = any(a == anim and float(f) == float(fr) and k == want_kind and i == want_id and int(v) == ids[em] for a, f, k, i, _n, v in rcv)
    ck("C receiver row for %s (%s %s frame %s %s)" % (name, em, anim, fr, kind), ok)
ck("C receiver rows match the variant (emote id) and skip a still-alive continuous effect", "(c->variant != 0u && c->variant != pp->cur.variant)" in rp and "pp->fxc_live & bit" in rp)
ck("C receiver kills its continuous effects at the row end / a new row", 'pc_puppet_aext_fxc_kill(self, pp, "row_end")' in rp and 'pc_puppet_aext_fxc_kill(self, pp, "new_row")' in rp)
ck("C the old rows (variant 0 = any) keep firing for a real DEMO_WAIT (demo_shock)", '"demo_shock", 0 }' in rp)

# ---- S
ck("S settings: [Gameplay] emotes default 1, parsed, saved", ".emotes = 1," in st and 'strcmp(key, "emotes") == 0' in st and 'fprintf(f, "emotes = %d' in st and "int emotes;" in sth)
ck("S env AC_EMOTES=0 kill switch", 'getenv("AC_EMOTES")' in wh and "return s_env && g_pc_settings.emotes;" in wh)
ck("S CMake builds pc_emote.c + pc_emote_wheel.c, the wheel with the test hooks", "src/pc_emote.c" in cm and cm.count("src/pc_emote_wheel.c") == 2)
readme = rd(ROOT, "README.md")
ck("S README controls list the emote wheel", re.search(r"[Ee]mote", readme) is not None)

# ---- N
src = os.path.join(HERE, "emote_selftest.c")
ck("N native selftest present and includes the real module", os.path.isfile(src) and '#include "../../src/pc_emote.c"' in open(src).read())
gcc = None
for cand in ("gcc", r"C:\msys64\ucrt64\bin\gcc.exe"):
    try:
        subprocess.run([cand, "--version"], capture_output=True, check=True, timeout=30)
        gcc = cand
        break
    except (OSError, subprocess.SubprocessError):
        continue
if gcc is None:
    print("SKIPPED - gcc not found: native selftest not run")
else:
    out = os.path.join(os.environ.get("TEMP", HERE), "emote_selftest_src_audit.exe")
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + r"C:\msys64\ucrt64\bin"
    cp = subprocess.run([gcc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Iinclude", "tools/net_spike/emote_selftest.c", "-o", out, "-lm"], cwd=PC,
                        capture_output=True, timeout=120, env=env)
    ck("N selftest compiles warning-free (%s)" % cp.stderr.decode(errors="replace")[-300:], cp.returncode == 0)
    if cp.returncode == 0:
        r = subprocess.run([out], capture_output=True, timeout=60, env=env)
        tail = r.stdout.decode(errors="replace").strip().splitlines()[-1:] or [""]
        ck("N selftest passes (%s)" % tail[0], r.returncode == 0)

print("-" * 60)
n = sum(1 for _d, okv in results if okv)
print("%d/%d checks passed" % (n, len(results)))
sys.exit(0 if n == len(results) else 1)
