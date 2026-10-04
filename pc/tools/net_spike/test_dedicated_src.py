#!/usr/bin/env python3
"""test_dedicated_src.py - SOURCE AUDIT of the opt-in dedicated server mode (`--host --dedicated`). No game process.

Reads the working-tree sources and `git show BASELINE_REF:<path>` / `git diff BASELINE_REF FEATURE_REF` (read-only; "HEAD" below means that baseline, see BASELINE_REF). Checks:
  F   flag: g_pc_dedicated defaults to 0 and is assigned only by the `--dedicated` argument branch; the dedicated validation block refuses (exit 2) without
      --host / with --connect / --bootstrap-resident / --bootstrap-guest / the SAME ten host-self hooks the observer refuses (the two lists are compared),
      then sets g_pc_host_observer = 1; there is exactly ONE observer init (pc_host_observer_poll), nothing in pc_dedicated.c re-implements or calls it
  W   window / vsync: SDL_WINDOW_HIDDEN appears exactly once, inside an `if (g_pc_dedicated)` block; SDL_ShowWindow is never called anywhere in pc/src; the swap
      interval is `g_pc_dedicated ? 0 : g_pc_settings.vsync` (the only SDL_GL_SetSwapInterval call); the GL context creation is NOT skipped
  A   audio: pc_dedicated_pre_sdl_init() (SDL_setenv + OVERRIDE hint "dummy") runs BEFORE SDL_Init in pc_platform_init; pc_dedicated_post_sdl_init() right after it
      and fails (exit 1) unless the current driver is "dummy"; AIInit reports both outcomes and a failed open exits 1; AIInit / the producer thread are not removed
  R   render skip: ONLY the four seams are guarded (emu64_taskstart in graph_task_set00, pc_gx_begin_frame in JW_BeginFrame, pc_gx_draw_pending + the swap in
      VIWaitForRetrace); JW_EndFrame still calls VIWaitForRetrace and is byte-identical to HEAD, emu64_init/emu64_cleanup/JW_BeginFrame's FrameDrawing, the net poll,
      the save block and the pacing loop all stay un-guarded (pacing only changes the busy-wait threshold, dedicated only)
  C   console: the stdin reader thread body (pc_ded_reader_loop / pc_ded_enqueue / the thread entry) calls no game, save, network, SDL, logging or exit function;
      queue = 16 lines x 255 chars, drop+warn when full, truncation flagged; the thread is DETACHED and never joined/waited on; the poll runs from pc_vi.c right after
      pc_net_game_poll() under `if (g_pc_dedicated)`; the console `save` only sets a flag, the real write is in pc_vi.c's save block behind the role / world-ready /
      pcfa_save_ready gates and reports only after the call; `stop` only sets g_pc_running = 0; Ctrl+C handler bodies are byte-identical to HEAD
  X   shutdown: src/main.c still saves exactly once (one pc_save_write_authoritative call), after graph_proc, before pc_net_game_shutdown / pc_platform_shutdown /
      exit(0); the dedicated additions are notices only
  P   plain paths: against HEAD every touched file is additions only except the pinned guarded rewrites (the removed lines are exactly the pinned set, and each
      reappears guarded); every hunk carries a dedicated marker; flag default off; the python harness launch is unchanged with default arguments
Tier: SOURCE AUDITED. Exit code 0 when all checks pass.
"""
import os
import re
import subprocess
import sys

import net_spike_lib as L

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
MARKERS = ("dedicated", "DEDICATED")
# BASELINE_REF is the PARENT of the dedicated-mode feature commit (FEATURE_REF = 3afc165): every "vs HEAD" in this audit really means "vs the code before the
# dedicated mode". HEAD itself contains the feature since 3afc165 (and the working tree may carry later, unrelated work), so neither can be the baseline.
#   * head(rel) = the file as it was BEFORE the dedicated mode (BASELINE_REF blob); the "unchanged vs baseline" checks compare the working tree's function
#     bodies / files against it (current-behaviour checks stay on the working tree).
#   * hunks(rel) = `git diff -U0 BASELINE_REF FEATURE_REF` = the dedicated commit's OWN change (diff-discipline checks), independent of later work.
BASELINE_REF = "e310798"
FEATURE_REF = "3afc165"


def read(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
        return f.read().decode("utf-8", "replace").replace("\r\n", "\n")


def head(rel):
    return subprocess.run(["git", "-C", ROOT, "show", BASELINE_REF + ":" + rel], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def hunks(rel):
    d = subprocess.run(["git", "-C", ROOT, "diff", "-U0", "--ignore-cr-at-eol", BASELINE_REF, FEATURE_REF, "--", rel], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace")
    out, cur = [], None
    for ln in d.split("\n"):
        if ln.startswith("@@"):
            cur = {"rem": [], "add": []}
            out.append(cur)
        elif cur is None or ln.startswith(("---", "+++")):
            continue
        elif ln.startswith("-"):
            cur["rem"].append(ln[1:].strip())
        elif ln.startswith("+"):
            cur["add"].append(ln[1:].strip())
    for h in out:
        h["rem"] = [x for x in h["rem"] if x]
        h["add"] = [x for x in h["add"] if x]
    return out


def func_body(src, name):
    """Text of the function `name` (from its definition line to the first line that is exactly '}'), '' if absent."""
    m = re.search(r"^[^\n;]*\b%s\([^;{]*\)\s*\{\n" % re.escape(name), src, re.M)
    if not m:
        return ""
    j = src.find("\n}\n", m.end())
    return src[m.start():j + 3] if j >= 0 else ""


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    return re.sub(r"//[^\n]*", " ", s)


def norm_tokens(t):
    """Identifiers / numbers of a code line, with a numeric 'u' suffix dropped (2000u == 2000)."""
    return {re.sub(r"^(\d+)u$", r"\1", x) for x in re.findall(r"\w+", t)}


def ws(t):
    return re.sub(r"\s+", "", t)


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)

    ded_c = read("pc/src/pc_dedicated.c")
    ded_h = read("pc/include/pc_dedicated.h")
    main_c = read("pc/src/pc_main.c")
    vi_c = read("pc/src/pc_vi.c")
    graph_c = read("src/graph.c")
    jsys = read("src/static/jsyswrap.cpp")
    mainmain = read("src/main.c")
    audio_c = read("pc/src/pc_audio.c")
    obs_h = read("pc/include/pc_host_observer.h")
    mcard = read("pc/src/pc_m_card.c")

    # ---------------------------------------------------------------- F: flag, validation, observer reuse
    ck("F1 g_pc_dedicated is defined once, default 0 (pc_dedicated.c); declared extern in pc_dedicated.h", len(re.findall(r"^int g_pc_dedicated = 0;$", ded_c, re.M)) == 1
       and "extern int g_pc_dedicated;" in ded_h)
    assigns = re.findall(r"g_pc_dedicated\s*=\s*[^=]", strip_comments(main_c))
    ck("F1 pc_main.c assigns g_pc_dedicated exactly once, in the `--dedicated` argument branch", len(assigns) == 1 and re.search(
        r'strcmp\(argv\[i\], "--dedicated"\) == 0\) \{\s*g_pc_dedicated = 1;', main_c) is not None)
    others = [p for p in ("pc/src/pc_vi.c", "pc/src/pc_net_game.c", "pc/src/pc_m_card.c", "src/graph.c", "src/main.c", "pc/src/pc_audio.c")
              if re.search(r"g_pc_dedicated\s*=[^=]", strip_comments(read(p)))]
    ck("F1 no other file assigns g_pc_dedicated (%s)" % others, not others)
    blk = main_c[main_c.index("    /* --dedicated: HOST-only;"):main_c.index("    /* --host-observer: HOST-only, exclusive")]
    ck("F2 the dedicated block refuses without --host (role != 1), with --bootstrap-resident, --bootstrap-guest and --connect, each `return 2`",
       all(x in blk for x in ("g_pc_net_role != 1", "g_pc_bootstrap_resident >= 0", "g_pc_bootstrap_guest != NULL", 'strcmp(argv[a], "--connect") == 0'))
       and blk.count("return 2;") == 5 and blk.count("usage: AnimalCrossing --host [port] --dedicated") == 5)
    ded_hooks = re.findall(r'"(--[a-z-]+)"', blk.split("k_dedicated_refused_hooks[] = {", 1)[1].split("};", 1)[0])
    obs_blk = main_c[main_c.index("    /* --host-observer: HOST-only, exclusive"):]
    obs_hooks = re.findall(r'"(--[a-z-]+)"', obs_blk.split("k_observer_refused_hooks[] = {", 1)[1].split("};", 1)[0])
    ck("F2 the dedicated hook refusal list is IDENTICAL to --host-observer's (%d hooks)" % len(obs_hooks), ded_hooks == obs_hooks and len(obs_hooks) == 10)
    ck("F3 the dedicated block sets the SAME observer flag (g_pc_host_observer = 1) after all refusals, once", blk.count("g_pc_host_observer = 1;") == 1
       and blk.index("g_pc_host_observer = 1;") > blk.rindex("return 2;"))
    ck("F3 the observer validation block that follows is unchanged (HEAD text contained verbatim)", ws(head("pc/src/pc_main.c").split("    /* --host-observer: HOST-only, exclusive", 1)[1]
                                                                                                    .split("    /* pc_log: env PC_LOG", 1)[0]) in ws(obs_blk))
    all_src = "".join(read(p) for p in ("pc/src/pc_m_card.c", "pc/src/pc_vi.c", "pc/src/pc_main.c", "pc/src/pc_net_game.c"))
    ck("F4 exactly ONE observer init: pc_host_observer_poll is defined once (pc_m_card.c) and called once (pc_vi.c); no second init function",
       len(re.findall(r"^void pc_host_observer_poll\(void\)", mcard, re.M)) == 1 and vi_c.count("pc_host_observer_poll();") == 1
       and not re.search(r"\bpc_host_observer_poll\b", strip_comments(ded_c) + strip_comments(ded_h)))
    code_c = strip_comments(ded_c)
    ck("F4 pc_dedicated.c never calls the observer init or any start-data-init / scene-change function (it only reads pc_host_observer_active/ready)",
       not re.search(r"pc_host_observer_poll|StartDataInit|mSDI_|goto_other_scene|mPr_|pc_bootstrap", code_c) and "pc_host_observer_active()" in code_c
       and "pc_host_observer_ready()" in code_c)
    ck("F5 pc_host_observer.h is unchanged vs HEAD", obs_h == head("pc/include/pc_host_observer.h"))

    # ---------------------------------------------------------------- W: window / vsync
    code_main = strip_comments(main_c)
    ck("W1 SDL_WINDOW_HIDDEN appears exactly once and inside an `if (g_pc_dedicated) {` block",
       code_main.count("SDL_WINDOW_HIDDEN") == 1 and re.search(r"if \(g_pc_dedicated\) \{\s*flags = SDL_WINDOW_OPENGL \| SDL_WINDOW_HIDDEN;", code_main) is not None)
    shows = [p for p in ("pc/src/" + n for n in os.listdir(os.path.join(ROOT, "pc", "src")) if n.endswith((".c", ".cpp"))) if "SDL_ShowWindow" in strip_comments(read(p))]
    ck("W1 SDL_ShowWindow is never called anywhere in pc/src (%s)" % shows, not shows)
    ck("W1 the default (non-dedicated) window flags line is untouched: SDL_WINDOW_OPENGL | SDL_WINDOW_SHOWN | SDL_WINDOW_RESIZABLE",
       "Uint32 flags = SDL_WINDOW_OPENGL | SDL_WINDOW_SHOWN | SDL_WINDOW_RESIZABLE;" in main_c)
    ck("W2 the GL context is still created on the hidden window (SDL_GL_CreateContext / gladLoadGL / pc_gx_init not guarded)",
       "g_pc_gl_context = SDL_GL_CreateContext(g_pc_window);" in main_c and re.search(r"if \(g_pc_dedicated\)[^\n]*\n[^\n]*SDL_GL_CreateContext", main_c) is None
       and "    pc_gx_init();\n" in main_c)
    swaps = re.findall(r"SDL_GL_SetSwapInterval\(([^;]*)\);", code_main)
    ck("W3 swap interval 0 under dedicated: the only SDL_GL_SetSwapInterval call is `g_pc_dedicated ? 0 : g_pc_settings.vsync`", swaps == ["g_pc_dedicated ? 0 : g_pc_settings.vsync"])
    ck("W4 texture preload skipped only under dedicated (`preload_textures && !g_pc_dedicated`)", "if (g_pc_settings.preload_textures && !g_pc_dedicated) {" in main_c)
    ck("W5 platform summary logged at the end of pc_platform_init (window hidden, vsync, audio driver, render skipped)",
       main_c.index("pc_dedicated_platform_summary();") > main_c.index("pc_texture_pack_init();") and "window=%s" in ded_c and "vsync=%s" in ded_c and "audio_driver=%s" in ded_c)

    # ---------------------------------------------------------------- A: audio
    init_body = func_body(main_c, "pc_platform_init")
    ip, ii, ipost = init_body.find("pc_dedicated_pre_sdl_init();"), init_body.find("SDL_Init(SDL_INIT_VIDEO"), init_body.find("pc_dedicated_post_sdl_init()")
    ck("A1 pc_platform_init: pre_sdl_init (dummy driver) runs BEFORE SDL_Init, post_sdl_init (verify) AFTER it; a failed verification exits 1", 0 < ip < ii < ipost
       and re.search(r"!pc_dedicated_post_sdl_init\(\)\) \{\s*SDL_Quit\(\);\s*exit\(1\);", init_body) is not None)
    pre = func_body(ded_c, "pc_dedicated_pre_sdl_init")
    ck("A2 the driver is selected by SDL_setenv(\"SDL_AUDIODRIVER\", \"dummy\", 1) AND an OVERRIDE-priority SDL_HINT_AUDIODRIVER hint", 'SDL_setenv("SDL_AUDIODRIVER", "dummy", 1);' in pre
       and 'SDL_SetHintWithPriority(SDL_HINT_AUDIODRIVER, "dummy", SDL_HINT_OVERRIDE);' in pre and "SDL_Init" not in strip_comments(pre))
    post = func_body(ded_c, "pc_dedicated_post_sdl_init")
    ck("A3 post_sdl_init returns 0 (startup refused) unless SDL_GetCurrentAudioDriver() is \"dummy\"", 'strcmp(drv, "dummy") != 0' in post and "return 0;" in post)
    opened = func_body(ded_c, "pc_dedicated_audio_opened")
    ai = func_body(audio_c, "AIInit")
    ck("A4 AIInit reports both outcomes to the dedicated module and a failed open exits 1 (no stalled producer)", "pc_dedicated_audio_opened(1," in ai and "pc_dedicated_audio_opened(0," in ai
       and "exit(1);" in opened and "if (!ok)" in opened)
    ck("A5 audio is not removed: AIInit still opens the SDL device, the producer thread / callback / ring are unchanged vs HEAD",
       "SDL_OpenAudioDevice" in ai and func_body(audio_c, "pc_audio_producer_func") == func_body(head("pc/src/pc_audio.c"), "pc_audio_producer_func")
       and func_body(audio_c, "pc_audio_callback") == func_body(head("pc/src/pc_audio.c"), "pc_audio_callback")
       and func_body(audio_c, "AIInitDMA") == func_body(head("pc/src/pc_audio.c"), "AIInitDMA"))

    # ---------------------------------------------------------------- R: render skip
    t00 = func_body(graph_c, "graph_task_set00")
    ck("R1 graph_task_set00: emu64_taskstart (TARGET_PC path) is guarded by `if (!g_pc_dedicated)`; emu64_init / emu64_cleanup / JW_BeginFrame / JW_EndFrame stay unguarded",
       re.search(r"if \(!g_pc_dedicated\) \{\s*emu64_taskstart\(this->Gfx_list05\);", t00) is not None and t00.count("emu64_taskstart(this->Gfx_list05)") == 2
       and re.search(r"^\s*emu64_init\(\);", t00, re.M) and re.search(r"^\s*emu64_cleanup\(\);", t00, re.M)
       and re.search(r"^\s*JW_BeginFrame\(\);", t00, re.M) and re.search(r"^\s*JW_EndFrame\(\);", t00, re.M))
    ck("R1 the non-TARGET_PC emu64_taskstart line is untouched", "#else\n            emu64_taskstart(this->Gfx_list05); /* work data */" in graph_c)
    jb = func_body(jsys, "JW_BeginFrame")
    ck("R2 JW_BeginFrame (PC path): pc_gx_begin_frame guarded by `if (!g_pc_dedicated)`, FrameDrawing = true still set", re.search(r"if \(!g_pc_dedicated\) \{\s*pc_gx_begin_frame\(\);\s*\}\s*FrameDrawing = true;", jb) is not None)
    ck("R2 JW_EndFrame is byte-identical to HEAD and still calls VIWaitForRetrace (net poll, saves, pacing)", func_body(jsys, "JW_EndFrame") == func_body(head("src/static/jsyswrap.cpp"), "JW_EndFrame")
       and "VIWaitForRetrace();" in func_body(jsys, "JW_EndFrame"))
    vw = func_body(vi_c, "VIWaitForRetrace")
    ck("R3 VIWaitForRetrace: pc_gx_draw_pending and pc_platform_swap_buffers are the only guarded render calls", "if (!g_pc_dedicated) pc_gx_draw_pending();" in vw
       and "if (!g_pc_dedicated) pc_platform_swap_buffers();" in vw and vw.count("g_pc_dedicated") == 5)
    unguarded = ["pc_platform_poll_events()", "pc_net_game_poll();", "pc_remote_player_poll();", "pc_bootstrap_resident_poll();", "pc_host_observer_poll();",
                 "pc_save_write_authoritative()", "SDL_Delay(1);", "frame_start_time = SDL_GetPerformanceCounter();", "pc_frame_counter++;"]
    ck("R3 poll_events / net poll / puppet poll / bootstrap + observer polls / saves / pacing / frame counter are NOT guarded: " + ", ".join(u.strip(";()") for u in unguarded[:4]) + ", ...",
       all(u in vw for u in unguarded) and not re.search(r"g_pc_dedicated[^\n]*\n[^\n]*(pc_net_game_poll\(\);|pc_remote_player_poll\(\);|pc_host_observer_poll\(\);)", vw))
    ck("R3 pacing: the sleep loop is unchanged except the busy-wait threshold (`remain_us > (g_pc_dedicated ? 1000u : 2000u)`); non-dedicated stays 2000 us",
       "if (remain_us > (g_pc_dedicated ? 1000u : 2000u)) {" in vw and "SDL_Delay(1);" in vw)
    ck("R4 pc_platform_swap_buffers is byte-identical to HEAD", func_body(main_c, "pc_platform_swap_buffers") == func_body(head("pc/src/pc_main.c"), "pc_platform_swap_buffers")
       and func_body(main_c, "pc_platform_swap_buffers") != "")
    ck("R5 the window title update is skipped only under dedicated", "if (!g_pc_dedicated) SDL_SetWindowTitle(g_pc_window, title);" in vw)

    # ---------------------------------------------------------------- C: console
    reader = func_body(ded_c, "pc_ded_reader_loop") or ded_c[ded_c.index("static void pc_ded_reader_loop("):ded_c.index("#ifdef _WIN32\nstatic HANDLE s_stdin_handle")]
    enqueue = func_body(ded_c, "pc_ded_enqueue")
    entry = ded_c[ded_c.index("static unsigned __stdcall pc_ded_reader_thread"):ded_c.index("#else\nstatic int s_stdin_fd")]
    thread_code = strip_comments(reader + enqueue + entry)
    forbidden = re.findall(r"\b(pc_net\w*|pcfa_\w*|pc_save\w*|Save_\w*|SDL_\w+|g_pc_running|printf|fprintf|puts|PC_LOG\w*|gamePT|pc_remote\w*|mFI_\w+|exit|malloc|free|pc_dedicated\w*)\b", thread_code)
    ck("C1 the reader thread (loop + enqueue + entry) calls no game / save / network / SDL / logging / exit / allocation function (found: %s)" % sorted(set(forbidden)), not forbidden)
    ck("C1 the thread does only raw reads + a locked enqueue: ReadFile/read, memcpy, Enter/LeaveCriticalSection (pthread mutex)",
       ("ReadFile(" in thread_code or "read(fd" in thread_code) and "memcpy(" in thread_code and "PC_DED_LOCK();" in thread_code and "PC_DED_UNLOCK();" in thread_code)
    ck("C2 queue: 16 lines x 255 chars (+NUL), drop+warn when full, truncated lines flagged and reported", "#define PC_DED_QUEUE_MAX 16" in ded_c and "#define PC_DED_LINE_MAX  255" in ded_c
       and "s_q_dropped++" in enqueue and "console input queue was full" in ded_c and "console line truncated" in ded_c and "truncated = 1;" in reader)
    ck("C3 EOF / read error: the thread just sets s_stdin_eof and exits; the MAIN thread reports it once and the server keeps running (no g_pc_running write on EOF)",
       "s_stdin_eof = 1;" in reader and "console input closed (EOF); the server keeps running" in ded_c and
       "g_pc_running" not in func_body(ded_c, "pc_dedicated_console_poll").split("s_stdin_eof && !s_stdin_eof_reported", 1)[1])
    ck("C4 the reader is DETACHED and never joined (CloseHandle on the thread handle / pthread_detach; no WaitForSingleObject / pthread_join / SDL_WaitThread in the module)",
       "CloseHandle((HANDLE)th); /* detach */" in ded_c and "pthread_detach(t);" in ded_c and not re.search(r"WaitForSingleObject|pthread_join|SDL_WaitThread|SDL_CreateThread", strip_comments(ded_c)))
    ck("C5 commands run only on the main thread: pc_dedicated_console_poll is called from pc_vi.c under `if (g_pc_dedicated)` right after pc_net_game_poll()",
       re.search(r"pc_net_game_poll\(\);\s*(?:/\*.*?\*/\s*)?if \(g_pc_dedicated\) \{\s*pc_dedicated_console_poll\(\);\s*\}", vw, re.S) is not None and vi_c.count("pc_dedicated_console_poll();") == 1)
    cmds = func_body(ded_c, "pc_ded_execute")
    ck("C6 command set: help, status, players, save, stop (+ quit / exit aliases), unknown -> 'unknown command: X (type help)'; case-insensitive (tolower)",
       all('strcmp(cmd, "%s") == 0' % c in cmds for c in ("help", "status", "players", "save", "stop", "quit", "exit")) and "unknown command: %s (type help)" in cmds and "tolower" in cmds)
    code_ded = strip_comments(ded_c)
    ck("C7 console `save` only sets a request flag: pc_dedicated.c never calls pc_save_write_authoritative / pcfa_save_ready's writer / any save function",
       "pc_save_write_authoritative" not in code_ded and "s_save_pending = 1;" in cmds)
    save_blk = vw[vw.index("if (pc_dedicated_save_request_pending())"):]
    save_blk = save_blk[:save_blk.index("        }\n    }\n") + 1]
    ck("C7 pc_vi.c performs it INSIDE the existing periodic-save block, behind role == HOST, host world ready and pcfa_save_ready(), via pc_save_write_authoritative(), "
       "and reports only after the call (or the refusal)", save_blk.index("PC_NETGAME_ROLE_HOST") < save_blk.index("pc_net_game_dedicated_world_ready()") < save_blk.index("pcfa_save_ready()")
       < save_blk.index("pc_save_write_authoritative()") < save_blk.index("pc_dedicated_save_report(console_save_ok, NULL)")
       and vw.index("l_last_save_time = 0;") < vw.index("if (pc_dedicated_save_request_pending())") < vw.index("pc_gx_draw_pending"))
    stop = func_body(ded_c, "pc_ded_cmd_stop")
    ck("C8 `stop` = print 'stopping...' + g_pc_running = 0 only (same flag as Ctrl+C; no save / net / SDL call)", "g_pc_running = 0;" in stop and "stopping..." in stop
       and not re.search(r"pc_save|pc_net|SDL_|exit\(", strip_comments(stop)))
    head_main = head("pc/src/pc_main.c")
    ck("C9 the Ctrl+C / console-ctrl / signal handlers are byte-identical to HEAD", func_body(main_c, "pc_console_ctrl_handler") == func_body(head_main, "pc_console_ctrl_handler") != ""
       and func_body(main_c, "pc_signal_handler") == func_body(head_main, "pc_signal_handler") != "")
    ck("C10 console routing: stdout is never redirected to NUL under dedicated (condition ends `&& !g_pc_dedicated`) and stdout stays unbuffered; the console attach keeps valid std handles",
       "if (!g_pc_verbose && !g_pc_profile_enabled && !log_want_console && !g_pc_dedicated) {" in main_c and "setvbuf(stdout, NULL, _IONBF, 0);" in main_c
       and "if (!in_ok) {" in ded_c and "if (!out_ok) {" in ded_c and "if (!err_ok) {" in ded_c and 'freopen("CONIN$", "r", stdin);' in ded_c and "AttachConsole(ATTACH_PARENT_PROCESS)" in ded_c)

    # ---------------------------------------------------------------- X: shutdown
    sd = func_body(mainmain, "mainproc")
    ck("X1 src/main.c shutdown: exactly ONE pc_save_write_authoritative call (the existing final save), after graph_proc and before pc_net_game_shutdown / pc_platform_shutdown / exit(0)",
       strip_comments(sd).count("pc_save_write_authoritative()") == 1 and sd.index("graph_proc(val);") < sd.index("if (!pc_save_write_authoritative())") < sd.index("pc_net_game_shutdown();")
       < sd.index("pc_platform_shutdown();") < sd.index("exit(0);"))
    ck("X2 the shutdown save is still gated by pcfa_save_ready() and refuses CLIENT (HEAD text); the dedicated additions are notices only (pc_dedicated_notify_shutdown)",
       "} else if (pcfa_save_ready()) {" in sd and "pc_net_game_role_is_client()" in sd and len(re.findall(r'pc_dedicated_notify_shutdown\("', sd)) == 4
       and "final save skipped (world/save not ready)" in sd)
    ck("X3 stop => one shutdown save: `stop` does not save; every save result is a wrapper notice (pc_save_write_authoritative -> pc_dedicated_notify_save_result), never a second save call",
       "pc_dedicated_notify_save_result(ok)" in func_body(mcard, "pc_save_write_authoritative"))

    # ---------------------------------------------------------------- P: plain paths
    pinned = {
        "pc/src/pc_main.c": ["SDL_GL_SetSwapInterval(g_pc_settings.vsync);", "if (g_pc_settings.preload_textures) {",
                             "if (!g_pc_verbose && !g_pc_profile_enabled && !log_want_console) {"],
        "pc/src/pc_vi.c": ["pc_gx_draw_pending();", "pc_platform_swap_buffers();", "if (remain_us > 2000) {", "SDL_SetWindowTitle(g_pc_window, title);"],
        "src/graph.c": ["emu64_taskstart(this->Gfx_list05); /* work data */"],
        "src/static/jsyswrap.cpp": ["pc_gx_begin_frame();"],
        "src/main.c": [], "pc/src/pc_audio.c": [], "pc/src/pc_m_card.c": [], "pc/src/pc_net_game.c": [], "pc/src/pc_remote_player.c": [],
        "pc/include/pc_remote_player.h": [], "pc/CMakeLists.txt": [],
    }
    for rel, expect in pinned.items():
        hs = hunks(rel)
        removed = [r for h in hs for r in h["rem"]]
        ck("P1 %s: the lines removed vs HEAD are exactly the pinned guarded rewrites %s" % (rel, expect), sorted(removed) == sorted(expect))
        ck("P1 %s: every hunk carries a dedicated marker (no unrelated change)" % rel, hs and all(any(m in a for a in h["add"] for m in MARKERS) for h in hs))
        # a removed line must come back in the same hunk (guarded / re-expressed), never vanish
        ck("P1 %s: every removed line reappears in the same hunk, guarded or re-expressed (all its identifiers are in one added line)" % rel,
           all(all(any(norm_tokens(r) <= norm_tokens(a) for a in h["add"]) for r in h["rem"]) for h in hs))
    ck("P1 pc/include/pc_net_game.h (the wire header) is byte-identical to HEAD: the dedicated accessor struct / prototypes live in pc_dedicated.h", read("pc/include/pc_net_game.h") == head("pc/include/pc_net_game.h")
       and "typedef struct PCNetGameDedicatedPeerInfo {" in ded_h and "int  pc_net_game_dedicated_peer_info(int slot, PCNetGameDedicatedPeerInfo* out);" in ded_h)
    ck("W6 frame pacing: dedicated defaults to the 60 Hz tick (g_pc_frame_limit_override = 60 only when no --framelimit / --no-framelimit was given), inside the dedicated block, after the observer flag",
       blk.index("if (g_pc_frame_limit_override < 0) {") > blk.index("g_pc_host_observer = 1;") and "g_pc_frame_limit_override = 60;" in blk and blk.count("g_pc_frame_limit_override") == 2)
    ck("W7 the always-on '[DEDICATED] observer active' notice sits at the observer's own one-way latch (same block as the '[NET][OBSERVER] host: observer active at acre' OSReport), guarded by g_pc_dedicated",
       re.search(r'observer active at acre \(%d,%d\)\\n", [^\n]*\n[^\n]*avatar main_index[^\n]*\n\s*if \(g_pc_dedicated\) pc_dedicated_say\("observer active at acre', mcard) is not None)
    ck("P2 CMake: exactly one added line, the pc_dedicated.c source entry", [a for h in hunks("pc/CMakeLists.txt") for a in h["add"]] == ["${CMAKE_CURRENT_SOURCE_DIR}/src/pc_dedicated.c"])
    def unguarded_sites():
        out = []
        for p in ("pc/src/pc_vi.c", "pc/src/pc_audio.c", "pc/src/pc_m_card.c", "pc/src/pc_net_game.c", "src/main.c"):
            lines = read(p).split("\n")
            tail_start = next((i for i, ln in enumerate(lines) if ln.startswith("int pc_net_game_dedicated_world_ready(void)")), 10 ** 9)
            for i, ln in enumerate(lines):
                if re.search(r"pc_dedicated_(?!h)\w+\(|pc_net_game_dedicated_announce\(", ln) and "extern" not in ln and not ln.lstrip().startswith(("/*", "*", "#", "void ", "int ")):
                    ctx = "\n".join(lines[max(0, i - 12):i + 1])
                    # guarded by `g_pc_dedicated` on the line / a few lines above, by the console-save request gate (itself `g_pc_dedicated && ...`), or inside the
                    # appended pc_net_game.c accessor block whose entry points check the flag
                    if "g_pc_dedicated" in ctx or "pc_dedicated_save_request_pending()" in ctx or i > tail_start:
                        continue
                    out.append((p, ln.strip()[:80]))
        return out
    ug = unguarded_sites()
    ck("P3 every pc_dedicated_* call site in pc_vi.c / pc_audio.c / pc_m_card.c / pc_net_game.c / src/main.c is under `if (g_pc_dedicated)` (or the inert-by-contract request gate); unguarded: %s" % ug,
       not ug and re.search(r"if \(g_pc_dedicated\) \{\s*pc_dedicated_startup\(", main_c) is not None)
    ck("P3 pc_dedicated_save_request_pending() is inert without the flag (`return g_pc_dedicated && s_save_pending;`)", "return g_pc_dedicated && s_save_pending;" in ded_c)
    lib = read("pc/tools/net_spike/net_spike_lib.py")
    ck("P4 HostProcess defaults reproduce the historical launch (verbose=True -> --verbose, no stdin pipe, no new process group)",
       "verbose=True, stdin_pipe=False, new_group=False" in lib and '"--host", str(self.port)] + (["--verbose"] if self.verbose else []) + self.extra_args' in lib)
    ck("P5 pc_dedicated.h documents the mode and pc_dedicated.c is inert without the flag (every public entry checks g_pc_dedicated first)",
       all(re.search(r"^(?:void|int) %s\([^)]*\) \{\n(?:    [A-Za-z_][^\n]*;\n)*    if \(!g_pc_dedicated\) \{" % fn, ded_c, re.M) for fn in (
           "pc_dedicated_console_poll", "pc_dedicated_startup", "pc_dedicated_platform_summary", "pc_dedicated_notify_save_result", "pc_dedicated_notify_shutdown",
           "pc_dedicated_pre_sdl_init", "pc_dedicated_post_sdl_init", "pc_dedicated_audio_opened")))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
