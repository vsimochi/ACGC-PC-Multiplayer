#!/usr/bin/env python3
"""test_dedicated_runtime.py - the DEDICATED SERVER mode (`--host --dedicated`), REAL HOST PROCESSES + FakeClients + the stdin console.

TIER: PROTOCOL / PROCESS TESTED (real `AnimalCrossing.exe --host <port> --dedicated` processes from the DISPOSABLE fixture pc\\build64\\bin_fixture4, four residents, ports
12400+; <= 2 FakeClients at a time; no keystrokes, no --bootstrap-resident). NO visual / gameplay verification. net_spike_lib's guard snapshots the fixture's whole save dir
and restores it at exit (and CloneSaveGuard here), so the fixture stays pristine (md5 170154aa44bcd63d7a001744a542cc0a).

RUNS (each is one real host process):
  A  (no --verbose, stdin pipe, own process group)  console always on without --verbose; startup lines; help / status / players / save / unknown command / case + extra args;
     `save` before the world is ready is REFUSED; a FakeClient joins resident 0 -> `players` lists it (RESIDENT idx 0, name, puppet, idle); `save` -> 'save: OK', the GCI mtime
     advances and the records sidecar hook ran; stdin EOF does NOT stop the server (a second FakeClient still connects afterwards); CTRL_BREAK_EVENT (Ctrl+C equivalent) ->
     graceful exit 0 with ONE final save.
  B  (--verbose, stdin pipe)  observer active (the same observer init), hidden window (EnumWindows: no visible top-level window of the pid), dummy audio log + the audio ring is
     drained (consumed samples grow), idle CPU + real frame rate over 10 s at the dedicated DEFAULT (60 Hz although the fixture's settings.ini says max_fps=240), then `stop` (sent twice): exit 0 within seconds, shutdown order, the
     shutdown save logged ONCE, the GCI valid with residents 0..3 BYTE-IDENTICAL.
  C  (no --verbose, explicit --framelimit 240, stdin pipe, own process group)  the user's limit wins over the dedicated 60 Hz default; idle CPU + real frame rate, `quit` alias -> exit 0.
  D  CONTROL: a plain `--host` (no --dedicated) shows a VISIBLE top-level window (proves the EnumWindows visibility check can detect one) and prints no [DEDICATED] line.
Soft assertions: idle CPU < 25 % of one core (the number is printed either way). Usage: python test_dedicated_runtime.py [--port 12400]
"""
import argparse
import ctypes
import os
import re
import signal
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402

PLAYERS_RX = re.compile(r'^  peer (\d+): link=(\w+) class=(RESIDENT idx|GUEST slot) (\d+) name="([^"]*)" puppet=(\w+) idle=(-?\d+)ms$', re.M)


def cpu_seconds(pid):
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    c, e, kt, ut = (ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong())
    k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut))
    k.CloseHandle(h)
    return (kt.value + ut.value) / 1e7


def cpu_percent(proc, seconds):
    t0, c0 = time.monotonic(), cpu_seconds(proc.pid)
    time.sleep(seconds)
    c1 = cpu_seconds(proc.pid)
    if c0 is None or c1 is None:
        return None
    return (c1 - c0) / (time.monotonic() - t0) * 100.0


def top_level_windows(pid):
    """[(hwnd, visible, class, title)] of the TOP-LEVEL windows owned by `pid`."""
    user32 = ctypes.windll.user32
    out = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            cls, title = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            user32.GetWindowTextW(hwnd, title, 256)
            out.append((hwnd, bool(user32.IsWindowVisible(hwnd)), cls.value, title.value))
        return True

    user32.EnumWindows(proto(cb), 0)
    return out


def ascii_name(raw):
    return "".join(chr(b) if 0x20 <= b <= 0x7E else "?" for b in raw).rstrip(" ")


class Rig:
    def __init__(self, results):
        self.results = results
        self.check = lambda d, c: L.check(d, bool(c), results)
        self.clients = []

    def join(self, port, label, slot):
        c = L.FakeClient(label, "127.0.0.1", port, player=L.resident_player(slot))
        c.rec_resident_idx = slot
        c.connect_and_ready(quiet=True)
        self.clients.append(c)
        return c

    def release(self, c):
        try:
            if c.state in (c.STATE_CONNECTED, c.STATE_PENDING):
                c.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
        if c in self.clients:
            self.clients.remove(c)
        L.pump_sleep(0.6)


def say(host, cmd, wait_rx, timeout=8.0):
    """Sends a console line and waits for `wait_rx` to appear in the log AFTER the send. Returns the match or None."""
    off = len(host.log_text())
    if not host.send_line(cmd):
        return None
    return host.wait_for_log(wait_rx, timeout, since_offset=off)


def start(port, tag, log_dir, extra=(), **kw):
    h = L.HostProcess(port=port, extra_args=["--dedicated"] + list(extra), log_path=os.path.join(log_dir, "dedicated_%s.log" % tag), bin_dir=L.GAME_BIN_DIR,
                      stdin_pipe=True, **kw).start()
    orig = h.log_text
    h.log_text = lambda: orig().replace("\r\n", "\n")  # the game writes text-mode stdout: CRLF in the redirected log; normalise for the regexes
    return h


def gci_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def run(args, results):
    rig = Rig(results)
    check = rig.check
    save_dir = os.path.join(L.GAME_BIN_DIR, "save")
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    residents = {i: (ident, ex) for i, ident, ex in L.read_test_save_residents()}
    check("fixture has four existing residents 0..3 and no save/mp", sorted(residents) == [0, 1, 2, 3] and not os.path.exists(os.path.join(save_dir, "mp")))
    if sorted(residents) != [0, 1, 2, 3]:
        return
    pre = gci_bytes(gci_path)
    pre_recs = [L.record_from_gci(pre, i) for i in range(4)]

    # ===================================================================== RUN A
    port = args.port + 1
    h = start(port, "A", args.log_dir, verbose=False, new_group=True)
    try:
        check("A the host process started and is listening", h.wait_listening(60.0))
        t0 = h.wait_for_log(h.DEDICATED_START_RX, 20.0)
        check("A [DEDICATED] startup line (port, observer reuse, console on) appears WITHOUT --verbose (the console is always on)", t0 is not None
              and int(t0.group(1)) == port)
        # a `save` queued BEFORE the world is ready must be refused (gates apply), never run
        mo = say(h, "save", r"\[DEDICATED\] save: (?:refused: ([^\n]*)|OK|FAILED[^\n]*)", 30.0)
        refused_early = mo is not None and mo.group(1) is not None
        check("A `save` before the world is ready: 'save: refused: world not ready' (or the save-not-ready gate), not a claimed success", refused_early
              and ("world not ready" in mo.group(1) or "save not ready" in mo.group(1)))
        check("A the early refusal produced no 'save result' line (nothing was written)", "[DEDICATED] save result:" not in h.log_text())
        ready = h.boot_to_dedicated(timeout=120.0)
        check("A boot: observer active+ready, world ready, save ready (all via the server's own `status`)", ready)
        if not ready:
            return
        txt = h.log_text()
        check("A startup lines: platform summary says window=hidden, vsync=off, audio_driver=dummy and what rendering is skipped",
              re.search(h.DEDICATED_PLATFORM_RX, txt) is not None and re.search(h.DEDICATED_PLATFORM_RX, txt).groups() == ("hidden", "off", "dummy")
              and "render=skipped (emu64_taskstart, pc_gx_begin_frame, pc_gx_draw_pending, buffer swap; JW_EndFrame/VIWaitForRetrace kept)" in txt)
        check("A the dummy audio device opened ('[DEDICATED] audio device opened: driver=dummy ...')", re.search(r"\[DEDICATED\] audio device opened: driver=dummy freq=\d+ ch=2", txt) is not None)
        check("A world ready line present (no --verbose needed): '[NET][WORLD] host: world ready'", re.search(h.WORLD_READY_RX, txt) is not None)
        check("A no --verbose: the verbose-gated OSReport lines are absent (observer 'active at acre' is verbose-only), proving the console routing is independent of --verbose",
              "[NET][OBSERVER] host: observer active at acre" not in txt)
        check("A the always-on dedicated notice '[DEDICATED] observer active at acre (bx,bz)' IS present without --verbose (same latch as the observer's own line)",
              re.search(r"\[DEDICATED\] observer active at acre \(-?\d+,-?\d+\): the hidden server identity is parked in the town", txt) is not None)

        # --- help
        off = len(h.log_text())
        h.send_line("help")
        h.wait_for_log(r"  stop     graceful shutdown", 8.0, since_offset=off)
        ht = h.log_text()[off:]
        check("A `help` lists the 5 commands (help, status, players, save, stop) and the aliases", all(re.search(r"^  %s " % c, ht, re.M) for c in ("help", "status", "players", "save", "stop"))
              and "quit, exit" in ht)

        # --- status
        st = h.ask_status()
        check("A `status`: dedicated yes, observer active+ready, role host, listening_port, world ready yes, save ready yes, peers 0, audio dummy",
              st is not None and st.get("dedicated") == "yes" and st.get("observer") == "active=yes ready=yes" and st.get("network") == "role=host listening_port=%d" % port
              and st.get("world ready") == "yes" and st.get("save ready") == "yes" and st.get("peers") == "connected=0 ready=0" and st.get("audio", "").startswith("driver=dummy"))
        mo = re.match(r"(\d+)s \(\d+h \d\dm \d\ds\), frames=(\d+), avg_fps=([\d.]+)", (st or {}).get("uptime", ""))
        check("A `status` uptime / frames / avg_fps are real numbers (uptime > 0, frames > 0)", mo is not None and int(mo.group(1)) > 0 and int(mo.group(2)) > 0)
        cons1 = int(re.search(r"consumed_samples=(-?\d+)", st["audio"]).group(1))
        time.sleep(2.0)
        st2 = h.ask_status()
        cons2 = int(re.search(r"consumed_samples=(-?\d+)", st2["audio"]).group(1))
        check("A the dummy audio device keeps DRAINING the ring (consumed samples grew by %d in ~2 s; 32 kHz stereo = ~128000)" % (cons2 - cons1), cons2 - cons1 > 20000)
        check("A last save: periodic/earlier result is reported after a save happened (field present)", "last save" in st2)

        # --- players (empty), unknown command, case / args tolerance
        check("A `players` with nobody connected: 'no players connected'", say(h, "players", r"\[DEDICATED\] no players connected") is not None)
        check("A unknown command: 'unknown command: bogus (type help)'", say(h, "bogus", r"\[DEDICATED\] unknown command: bogus \(type help\)") is not None)
        check("A commands are case-insensitive and tolerate extra arguments / whitespace ('  STATUS  now please')",
              say(h, "  STATUS  now please", r"\[DEDICATED\] status\n  dedicated: yes") is not None)
        off = len(h.log_text())
        h.send_line("HeLp me")
        check("A 'HeLp me' still prints the help", h.wait_for_log(r"  stop     graceful shutdown", 8.0, since_offset=off) is not None)
        before_n = len(re.findall(r"\[DEDICATED\] unknown command", h.log_text()))
        h.send_line("")
        h.send_line("   ")
        time.sleep(1.0)
        check("A blank lines are ignored (no 'unknown command')", len(re.findall(r"\[DEDICATED\] unknown command", h.log_text())) == before_n)
        long_line = "status " + "x" * 600
        off = len(h.log_text())
        h.send_line(long_line)
        h.wait_for_log(r"\[DEDICATED\] status", 8.0, since_offset=off)
        check("A an over-long line (607 chars) is truncated to 255 with a warning and still executed (status answered)", "console line truncated to 255 characters" in h.log_text()[off:]
              and "[DEDICATED] status" in h.log_text()[off:])

        # --- queue overflow: a burst of 40 lines never blocks / crashes; lines beyond the 16-slot queue are dropped with a warning (the thread never waits)
        off = len(h.log_text())
        h.proc.stdin.write(("players\n" * 40).encode())
        h.proc.stdin.flush()
        time.sleep(2.0)
        seg = h.log_text()[off:]
        answered = len(re.findall(r"\[DEDICATED\] no players connected", seg))
        dropped = re.search(r"console input queue was full, (\d+) line\(s\) dropped", seg)
        check("A burst of 40 `players` lines in one write: the server stays alive; %d answered, %s dropped (answered + dropped == 40 or all 40 answered)"
              % (answered, dropped.group(1) if dropped else 0), h.alive() and (answered == 40 or (dropped is not None and answered + int(dropped.group(1)) == 40)))

        # --- a client joins resident 0
        L.resolve_host_town("127.0.0.1", port)
        c0 = rig.join(port, "A0", 0)
        L.pump_sleep(3.0)
        check("A FakeClient joined as resident 0 (READY, record synced)", c0.assigned_peer_id is not None and c0.rec_synced)
        txt = h.log_text()
        check("A always-on notices: '[DEDICATED] peer N connected' and 'peer N READY: RESIDENT idx 0 name=...' (no --verbose, no -debugnetwork)",
              re.search(r"\[DEDICATED\] peer %d connected \(transport; awaiting identity\)" % c0.assigned_peer_id, txt) is not None
              and re.search(r'\[DEDICATED\] peer %d READY: RESIDENT idx 0 name="[^"]+"' % c0.assigned_peer_id, txt) is not None)
        off = len(h.log_text())
        h.send_line("players")
        h.wait_for_log(r"  peer \d+: link=", 8.0, since_offset=off)
        seg = h.log_text()[off:]
        rows = PLAYERS_RX.findall(seg)
        want_name = ascii_name(residents[0][0].player_name)
        check("A `players` lists exactly the client: 1 row", "[DEDICATED] players: 1" in seg and len(rows) == 1)
        if rows:
            peer, link, cls, idx, name, puppet, idle = rows[0]
            check("A the row: peer slot %s, link READY, class RESIDENT idx 0, the resident's PersonalID name %r (not a token)" % (c0.assigned_peer_id, want_name),
                  int(peer) == c0.assigned_peer_id and link == "READY" and cls == "RESIDENT idx" and int(idx) == 0 and name == want_name)
            check("A the row reports puppet state ('%s': live/pending/none) and an idle time (%sms: a healthy peer is < 2000 ms)" % (puppet, idle),
                  puppet in ("live", "pending", "none") and 0 <= int(idle) < 2000)
            print("   INFO players row: puppet=%s idle=%sms" % (puppet, idle))
            check("A the puppet of the connected resident is LIVE in the (hidden, not drawn) world", puppet == "live")

        # --- console save: the same authoritative path; result only after it ran
        gci_m0 = os.path.getmtime(L.host_gci())  # a DEDICATED host saves into servers/default/town/card_a (the legacy fixture file only seeds it)
        rec_path = L.server_file("records.dat")
        rec_m0 = os.path.getmtime(rec_path) if os.path.isfile(rec_path) else None
        time.sleep(1.1)
        off = len(h.log_text())
        h.send_line("save")
        mo = h.wait_for_log(r"\[DEDICATED\] save: (OK|FAILED[^\n]*|refused: [^\n]*)", 30.0, since_offset=off)
        seg = h.log_text()[off:]
        check("A console `save`: 'save: OK' (after the write; the request line 'save: requested' came first)", mo is not None and mo.group(1) == "OK"
              and seg.index("save: requested") < seg.index("save result: OK") < seg.index("save: OK"))
        check("A the GCI mtime advanced (a real authoritative save ran)", os.path.getmtime(L.host_gci()) > gci_m0)
        rec_m1 = os.path.getmtime(rec_path) if os.path.isfile(rec_path) else None
        check("A the records sidecar hook ran with that save (servers/default/residents/records.dat exists%s)" % (" and its mtime advanced" if rec_m0 is not None else ""),
              rec_m1 is not None and (rec_m0 is None or rec_m1 > rec_m0))
        check("A the save-result notice states the result once per save ('[DEDICATED] save result: OK')", len(re.findall(r"\[DEDICATED\] save result: OK", seg)) == 1)

        # --- disconnect notice
        rig.release(c0)
        L.pump_sleep(1.0)
        check("A disconnect notice: '[DEDICATED] peer N disconnected (was READY): RESIDENT idx 0 ...'",
              re.search(r"\[DEDICATED\] peer \d+ disconnected \(was READY\): RESIDENT idx 0 name=", h.log_text()) is not None)
        off = len(h.log_text())
        h.send_line("players")
        h.wait_for_log(r"\[DEDICATED\] (no players connected|players: \d+)", 8.0, since_offset=off)
        check("A after the client left `players` says 'no players connected'", "[DEDICATED] no players connected" in h.log_text()[off:])

        # --- EOF on stdin must NOT stop the server
        h.close_stdin()
        eof = h.wait_for_log(r"\[DEDICATED\] console input closed \(EOF\); the server keeps running", 10.0)
        L.pump_sleep(2.0)
        check("A stdin EOF: the console reports it once and the server KEEPS RUNNING", eof is not None and h.alive()
              and len(re.findall(r"console input closed \(EOF\)", h.log_text())) == 1)
        c1 = rig.join(port, "A1", 1)
        L.pump_sleep(2.0)
        check("A after EOF a FakeClient still connects (resident 1 READY): the server is fully alive", c1.assigned_peer_id is not None and c1.rec_synced and h.alive())
        rig.release(c1)

        # --- Ctrl+Break (the Ctrl+C equivalent) -> graceful exit
        before = len(re.findall(r"final shutdown save OK", h.log_text()))
        sent = True
        try:
            os.kill(h.proc.pid, signal.CTRL_BREAK_EVENT)
        except OSError as e:
            sent = False
            print("   NOT VERIFIED: CTRL_BREAK_EVENT could not be sent (%s)" % e)
        code = h.wait_exit(30.0) if sent else None
        txt = h.log_text()
        if sent and code is None:
            print("   NOT VERIFIED / FAILED: the process did not exit after CTRL_BREAK_EVENT within 30 s (console not shared with the harness?)")
        check("A CTRL_BREAK_EVENT (own process group, console attached by the game): graceful exit code 0 within 30 s", sent and code == 0)
        check("A the Ctrl+Break shutdown ran the normal path ONCE: 'shutdown: final save OK' x1, 'shutdown: complete', 'shutting down networking'",
              len(re.findall(r"\[DEDICATED\] shutdown: final save OK", txt)) == 1 and "[DEDICATED] shutdown: complete" in txt and "[NET] shutting down networking (was host)" in txt)
        post = gci_bytes(L.host_gci())
        check("A the GCI is valid after the Ctrl+Break shutdown (size unchanged, residents 0..3 byte-identical: no-op client sessions)", len(post) == len(pre)
              and all(L.record_from_gci(post, i) == pre_recs[i] for i in range(4)))
        del before
    finally:
        h.stop()

    # ===================================================================== RUN B
    port = args.port + 2
    h = start(port, "B", args.log_dir, verbose=True)
    try:
        check("B host listening", h.wait_listening(60.0))
        check("B boot (status)", h.boot_to_dedicated(timeout=120.0))
        if not h.alive():
            return
        txt = h.log_text()
        check("B the observer init was REUSED: '[PC] --host-observer: observer bound ...' and '[NET][OBSERVER] host: observer active at acre' appear (verbose), no other init line",
              re.search(r"\[PC\] --host-observer: observer bound \(not a network participant: player_no=4, PersonalID 'SERVER  '", txt) is not None
              and re.search(h.OBSERVER_ACTIVE_RX, txt) is not None and "--bootstrap-resident" not in txt)
        check("B the audio driver is dummy in BOTH logs ('[AUDIO] Opened' + '[DEDICATED] audio device opened: driver=dummy')",
              "[AUDIO] Opened:" in txt and "[DEDICATED] audio device opened: driver=dummy" in txt and "[AUDIO] Producer thread started" in txt)
        wins = top_level_windows(h.proc.pid)
        print("   INFO top-level windows of the dedicated host: %s" % [(hex(w[0]), w[1], w[2]) for w in wins])
        check("B window hidden: the process owns %d top-level window(s), NONE visible (EnumWindows / IsWindowVisible)" % len(wins), len(wins) >= 1 and not any(w[1] for w in wins))
        check("B the default is the 60 Hz tick whatever settings.ini says (fixture max_fps=240): '[VI] frame limit=16666us (60 Hz)'", re.search(r"\[VI\] frame limit=1666\dus \(60 Hz\)", txt) is not None)
        s1 = h.ask_status()
        check("B `status` shows the frame limit '60 Hz'", s1 is not None and s1.get("frame limit") == "60 Hz")
        f1 = int(re.search(r"frames=(\d+)", s1["uptime"]).group(1))
        t1 = time.monotonic()
        pct = cpu_percent(h.proc, 10.0)
        s2 = h.ask_status()
        f2 = int(re.search(r"frames=(\d+)", s2["uptime"]).group(1))
        fps = (f2 - f1) / (time.monotonic() - t1)
        print("   MEASURED idle CPU (run B, default dedicated = 60 Hz, no clients): %.1f%% of one core over 10 s; real frame rate %.1f fps" % (pct if pct is not None else -1, fps))
        check("B idle CPU of an idle dedicated host is < 25%% of one core (soft threshold; measured %.1f%%)" % (pct if pct is not None else -1), pct is not None and pct < 25.0)
        check("B the loop is paced to the 60 Hz tick by the timer (measured %.1f fps, expected 55..62: no vsync, no spin)" % fps, 55.0 <= fps <= 62.0)
        # stop twice in a row: ONE shutdown
        off = len(h.log_text())
        t_stop = time.monotonic()
        h.send_line("stop")
        h.send_line("stop")
        code = h.wait_exit(30.0)
        took = time.monotonic() - t_stop
        txt = h.log_text()
        seg = txt[off:]
        check("B `stop`: exit code 0 within 30 s (took %.1f s)" % took, code == 0)
        check("B `stop` printed 'stopping...' once (the second stop did nothing: either 'already stopping' or the process was already gone)",
              seg.count("[DEDICATED] stopping...") == 1)
        order = ["[DEDICATED] stopping...", "[DEDICATED] save result: OK", "[DEDICATED] shutdown: final save OK", "[NET] shutting down networking (was host)",
                 "[DEDICATED] shutdown: complete (networking and platform closed)"]
        pos = [seg.find(x) for x in order]
        check("B shutdown order: stopping -> final save -> net shutdown -> platform closed -> complete (positions %s)" % pos, all(p >= 0 for p in pos) and pos == sorted(pos))
        check("B the shutdown save ran exactly ONCE (1 'final shutdown save OK' via OSReport, 1 'shutdown: final save OK', 1 'save result' after stop)",
              seg.count("final shutdown save OK") == 1 and seg.count("[DEDICATED] shutdown: final save OK") == 1 and seg.count("[DEDICATED] save result:") == 1
              and "final shutdown save FAILED" not in seg)
        post = gci_bytes(L.host_gci())
        check("B the GCI is valid and residents 0..3 are BYTE-IDENTICAL after the dedicated host's shutdown save", len(post) == len(pre)
              and all(L.record_from_gci(post, i) == pre_recs[i] for i in range(4)))
    finally:
        h.stop()

    # ===================================================================== RUN C
    port = args.port + 3
    h = start(port, "C", args.log_dir, extra=["--framelimit", "240"], verbose=False, new_group=True)
    try:
        check("C host listening", h.wait_listening(60.0))
        check("C boot (status) with an explicit --framelimit 240 (the user's value wins over the dedicated 60 Hz default)", h.boot_to_dedicated(timeout=120.0))
        if not h.alive():
            return
        check("C the explicit limit is active ('[VI] frame limit=4166us (240 Hz)')", re.search(r"\[VI\] frame limit=4166us \(240 Hz\)", h.log_text()) is not None)
        s1 = h.ask_status()
        check("C `status` shows the frame limit '240 Hz'", s1 is not None and s1.get("frame limit") == "240 Hz")
        f1 = int(re.search(r"frames=(\d+)", s1["uptime"]).group(1))
        t1 = time.monotonic()
        pct = cpu_percent(h.proc, 10.0)
        s2 = h.ask_status()
        f2 = int(re.search(r"frames=(\d+)", s2["uptime"]).group(1))
        fps = (f2 - f1) / (time.monotonic() - t1)
        print("   MEASURED idle CPU (run C, explicit --framelimit 240, no clients): %.1f%% of one core over 10 s; real frame rate %.1f fps" % (pct if pct is not None else -1, fps))
        check("C idle CPU < 25%% of one core even at 240 Hz (soft; measured %.1f%%)" % (pct if pct is not None else -1), pct is not None and pct < 25.0)
        check("C the 240 Hz tick is honoured (measured %.1f fps, expected 150..245)" % fps, 150.0 <= fps <= 245.0)
        h.send_line("quit")
        code = h.wait_exit(30.0)
        check("C `quit` (alias of stop): exit code 0, one final save", code == 0 and h.log_text().count("[DEDICATED] shutdown: final save OK") == 1)
    finally:
        h.stop()

    # ===================================================================== RUN D (control)
    port = args.port + 4
    h = L.HostProcess(port=port, extra_args=[], log_path=os.path.join(args.log_dir, "dedicated_D_plain_host.log"), bin_dir=L.GAME_BIN_DIR).start()
    try:
        check("D control: a plain `--host` is listening", h.wait_listening(60.0))
        time.sleep(6.0)
        wins = top_level_windows(h.proc.pid)
        print("   INFO top-level windows of the plain host: %s" % [(hex(w[0]), w[1], w[2], w[3]) for w in wins])
        check("D control: the plain host owns a VISIBLE top-level window (the EnumWindows check can see a shown window: the hidden result above is meaningful)",
              any(w[1] for w in wins))
        check("D control: a plain host prints no [DEDICATED] line and has no console thread", "[DEDICATED]" not in h.log_text())
    finally:
        h.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12400)
    ap.add_argument("--log-dir", default=HERE)
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    with L.CloneSaveGuard():
        try:
            run(args, results)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            L.check("test aborted by an exception: %s" % e, False, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
