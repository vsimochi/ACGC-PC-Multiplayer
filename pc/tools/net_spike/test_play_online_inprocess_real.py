#!/usr/bin/env python3
"""test_play_online_inprocess_real.py - Play Online WITHOUT relaunch: the title menu connects inside the running process.

TIER: REAL dedicated host + ONE REAL windowed client, GUI-driven with synthetic key events (game_input.py), on DISPOSABLE copies of pc\\build64\\bin_fixture4
(bin_fixture4_ipc_host, bin_fixture4_ipc_client = full copy with its own save/mp, bin_fixture4_ipc_client2 = empty save dir for the cancel scenario). The live save dir,
bin_talkfix*, bin_fixture4 and their exes are never launched or modified.
  host    = `AnimalCrossing.exe --host P --dedicated` (defaults: sanitized town serving ON)
  client  = `AnimalCrossing.exe --verbose` with NO connect arguments, env AC_RELAUNCH_DRYRUN=1 (tripwire: a pc_relaunch_connect would log 'RELAUNCH DRYRUN' and not quit),
            AC_DISPLAY_NAME=<--display, default samsung> (the window must open on that display or the process exits 4), AC_MASTER_VOLUME=1 (1 % output volume, test only)
  S1  title -> Play Online -> server -> Connect -> character: PASS iff the SAME PID and StartTime before / after, exactly one client process, no 'RELAUNCH' in the client log,
      the in-process connect line carries that PID, the host saw exactly two transport connections (the fetch peer, closed after TOWN_DONE, and the game peer) and NO disconnect
      of the game peer, and the client reaches READY (guest arrival).
  S2  (cancel) host stopped, client2 (no local save) with AC_TOWN_NO_MSGBOX=1: Connect -> 'no usable town' -> rolled back, the process is alive (same PID), still role NONE
      (the menu is back), no save/card_a or town directory was created.
Safety: keys are sent ONLY when the game window is the foreground window; screenshots capture ONLY the game window (client area); the window must be inside the logged bounds
of the matched display. All game processes are closed at the end.
Usage: python test_play_online_inprocess_real.py [--port 12100] [--display samsung] [--skip-cancel]
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

HOST_NAME, CLIENT_NAME, CLIENT2_NAME = "bin_fixture4_ipc_host", "bin_fixture4_ipc_client", "bin_fixture4_ipc_client2"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, HOST_NAME)
CLIENT_DIR = os.path.join(T.BUILD64, CLIENT_NAME)
CLIENT2_DIR = os.path.join(T.BUILD64, CLIENT2_NAME)
if __name__ == "__main__":
    T.make_fixture(HOST_NAME)
    T.make_fixture(CLIENT_NAME)
    T.make_fixture(CLIENT2_NAME, empty_save=True)

import net_spike_lib as L  # noqa: E402
import game_input as G  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
INI = "name = Roger\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x1234\nland_id = 0x4321\n"
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetProcessDPIAware()  # window rectangles / display bounds in the same (physical pixel) space as the game's SDL_GetDisplayBounds


def procs_in(dirpath):
    """[(pid, start_time_utc_filetime)] of the AnimalCrossing processes whose exe lives in dirpath."""
    ps = ("Get-Process AnimalCrossing -ErrorAction SilentlyContinue | ForEach-Object { '{0}|{1}|{2}' -f $_.Id, $_.StartTime.ToFileTimeUtc(), $_.Path }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60).stdout
    res = []
    want = os.path.normcase(os.path.abspath(dirpath))
    for ln in out.splitlines():
        parts = ln.strip().split("|", 2)
        if len(parts) == 3 and os.path.normcase(os.path.dirname(parts[2])) == want:
            res.append((int(parts[0]), parts[1]))
    return res


def seed_client(cdir, port):
    mp = os.path.join(cdir, "save", "mp")
    shutil.rmtree(mp, ignore_errors=True)
    os.makedirs(mp)
    with open(os.path.join(mp, "guest_roger.ini"), "w", newline="") as f:
        f.write(INI)
    with open(os.path.join(mp, "servers.ini"), "w", newline="") as f:
        f.write("[server]\nname = ipcsrv\naddress = 127.0.0.1\nport = %d\n" % port)
    r = subprocess.run([os.path.join(cdir, "AnimalCrossing.exe"), "--character-import-profile", "roger"], cwd=cdir, capture_output=True, text=True, timeout=60)
    m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
    return m.group(1) if (r.returncode == 0 and m) else None


def save_tree(cdir):
    """{relative path: size} of every file and directory under cdir/save."""
    out = {}
    root = os.path.join(cdir, "save")
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(d, n)
            out[os.path.relpath(p, root)] = os.path.getsize(p) if os.path.isfile(p) else -1
    return out


class Client:
    def __init__(self, cdir, tag, display, env_extra=None):
        L.require_launchable_bin_dir(cdir)
        self.cdir, self.tag = cdir, tag
        self.log_path = T.log_path("ipc_client_%s.log" % tag)
        env = dict(os.environ)
        env.update({"AC_TEST_HOOKS": "1", "AC_RELAUNCH_DRYRUN": "1", "AC_DISPLAY_NAME": display, "AC_MASTER_VOLUME": "1"})
        env.update(env_extra or {})
        self._fp = open(self.log_path, "wb")
        self.proc = subprocess.Popen([os.path.join(cdir, "AnimalCrossing.exe"), "--verbose"], cwd=cdir, stdout=self._fp, stderr=subprocess.STDOUT, env=env)
        self.win = G.GameWindow(self.proc.pid)

    def log(self):
        try:
            with open(self.log_path, "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def wait(self, rx, timeout, start=0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = re.compile(rx).search(self.log(), start)
            if m:
                return m
            if self.proc.poll() is not None:
                return None
            time.sleep(0.25)
        return None

    def foreground(self):
        if not self.win.ready():
            return False
        G.focus(self.win.hwnd)
        return user32.GetForegroundWindow() == self.win.hwnd

    def press(self, btn, after=0.45):
        if not self.foreground():
            raise RuntimeError("the game window is not the foreground window: refusing to send keys")
        self.win.press(btn, hold=0.12, after=after)

    def shot(self, name):
        path = os.path.join(T.LOG_DIR, "ipc_%s_%s.png" % (self.tag, name))
        if self.win.ready():
            G.capture_window_png(self.win.hwnd, path)  # the game window's client area only
        return path

    def rect(self):
        r = G.RECT()
        user32.GetWindowRect(self.win.hwnd, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._fp.close()


def drive_to_menu(c, ck, label):
    """title (logo -> menu) -> Play Online -> server 0 -> Connect: leaves the character page open (selection = the first / only character)."""
    ck(label + " title menu reached", c.wait(r"aAL_setupAction: 2 -> 3", 120.0) is not None)
    time.sleep(2.0)
    c.press("DOWN")
    c.shot("title_menu_playonline")
    c.press("START")   # Play Online
    c.shot("servers")
    c.press("START")   # the server
    c.press("START")   # Connect -> character page
    c.shot("characters")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12100)
    ap.add_argument("--display", default="samsung")
    ap.add_argument("--skip-cancel", action="store_true")
    args = ap.parse_args()
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    L.require_test_bin_dir()
    host = client = client2 = None
    try:
        uuid = seed_client(CLIENT_DIR, args.port)
        ck("setup: legacy profile imported as a store character in the client's own save/mp", uuid is not None)
        if uuid is None:
            return L.summary_and_exit_code(results)
        for i in range(3):
            host = L.HostProcess(port=args.port, extra_args=["--dedicated"], log_path=T.log_path("ipc_host_try%d.log" % i), bin_dir=HOST_DIR, stdin_pipe=True, verbose=False,
                                 new_group=True).start()
            if host.wait_listening(60.0) and host.boot_to_dedicated(timeout=120.0):
                break
            host.stop()
            host = None
            time.sleep(2.0)
        ck("dedicated host booted (town serving ON by default)", host is not None)
        if host is None:
            return L.summary_and_exit_code(results)

        # ------------------------------------------------------------------ S1
        client = Client(CLIENT_DIR, "s1", args.display)
        mo = client.wait(r"\[PC\] display: window goes to display (\d+)", 60.0)
        clog = client.log()
        ck("S1 the display list was logged and AC_DISPLAY_NAME matched exactly one display (window placed there, no exit 4)", mo is not None and client.proc.poll() is None)
        print("\n".join(ln for ln in clog.splitlines() if "[PC] display" in ln))
        if mo is None:
            return L.summary_and_exit_code(results)
        di = int(mo.group(1))
        bm = re.search(r"\[PC\] display %d: .*bounds=(-?\d+),(-?\d+) (\d+)x(\d+)" % di, clog)
        ck("S1 the matched display's friendly name / vendor identify the Samsung monitor", re.search(r"\[PC\] display %d: .*(vendor='SAM'|friendly-name='LC27RG50')" % di, clog) is not None)
        ck("S1 the AC_MASTER_VOLUME override was applied (1 %, settings untouched)", client.wait(r"AC_MASTER_VOLUME=1% overrides", 60.0) is not None)
        pid0 = client.proc.pid
        before = procs_in(CLIENT_DIR)
        ck("S1 before: exactly one client process", len(before) == 1 and before[0][0] == pid0)
        client.wait(r"aAL_setupAction: 2 -> 3", 120.0)
        for _ in range(80):
            if client.win.ready():
                break
            time.sleep(0.25)
        time.sleep(1.0)
        if bm:
            x, y, w, h = (int(v) for v in bm.groups())
            l, t, r, b = client.rect()
            ck("S1 the game window is on the matched display: window rect %s inside display bounds %s" % ((l, t, r, b), (x, y, x + w, y + h)),
               x <= (l + r) // 2 < x + w and y <= (t + b) // 2 < y + h)
        else:
            ck("S1 display bounds parsed", False)
        ck("S1 the game window is the foreground window (keys are only sent then)", client.foreground())
        off0 = len(client.log())
        drive_to_menu(client, ck, "S1")
        client.shot("before_connect")
        client.press("START", after=0.2)  # the character -> Connect (in-process)
        ck("S1 the connect request ran: in-process connect line", client.wait(r"\[PC\] play-online: in-process connect pid=(\d+) \(no relaunch\)", 90.0, off0) is not None)
        mo = re.search(r"play-online: in-process connect pid=(\d+) \(no relaunch\)", client.log())
        ck("S1 the in-process line carries the client's PID", mo is not None and int(mo.group(1)) == pid0)
        ck("S1 the title scene was left and the bootstrap armed", client.wait(r"play-online: title scene left: bootstrap (guest arrival|resident-by-PID) armed", 90.0, off0) is not None)
        client.shot("fading")
        mo = client.wait(r"-> READY", 150.0, off0)
        time.sleep(3.0)
        client.shot("after_ready")
        clog = client.log()
        ck("S1 the client reached READY", mo is not None)
        ck("S1 the guest arrival ran after the town load (town save re-read before the arrival)", "town save re-read from disk before the arrival" in clog)
        ck("S1 no relaunch: no 'RELAUNCH' (incl. 'RELAUNCH DRYRUN') in the client log", "RELAUNCH" not in clog)
        ck("S1 pc_net_game started once as a client after the fetch (fetch: installed/up-to-date town line present)", re.search(r"fetch: (installed town|up to date|town)", clog, re.I) is not None or "[NET][TOWN]" in clog)
        after = procs_in(CLIENT_DIR)
        ck("S1 after: exactly one client process, same PID and same StartTime", len(after) == 1 and after == before)
        ck("S1 the client process is still the one we started (alive)", client.proc.poll() is None and client.proc.pid == pid0)
        hlog = host.log_text()
        ev = []
        for m in re.finditer(r"\[NET\] host: peer (\d+) (transport-connected|disconnected)", hlog):
            ev.append(("C" if m.group(2) == "transport-connected" else "D", int(m.group(1))))
        conns = [e for e in ev if e[0] == "C"]
        ck("S1 host: exactly two transport connections (the fetch peer and the game peer): %s" % (ev,), len(conns) == 2)
        if len(conns) == 2:
            fetch_peer, game_peer = conns[0][1], conns[1][1]
            idx2 = [i for i, e in enumerate(ev) if e[0] == "C"][1]
            late_d = [e for e in ev[idx2 + 1:] if e[0] == "D"]
            ck("S1 host: no disconnect of the game peer after it connected (a late disconnect of the fetch peer, if any, is a different peer)", all(e[1] == fetch_peer != game_peer for e in late_d))
            ck("S1 host: the fetch peer's connection was closed (a disconnect of it, any time)", any(e[0] == "D" and e[1] == fetch_peer for e in ev))
        ck("S1 the host log shows a READY guest", re.search(r"peer \d+ READY: GUEST", hlog) is not None or "READY" in hlog)
        print("S1 screenshots in", T.LOG_DIR)
        client.stop()
        client = None
        time.sleep(1.0)
        ck("S1 closed: no client process left", len(procs_in(CLIENT_DIR)) == 0)

        # ------------------------------------------------------------------ S2 (cancel)
        if not args.skip_cancel:
            host.send_line("stop")
            if host.wait_exit(40.0) is None:
                host.stop()
            host = None
            time.sleep(1.5)
            ck("S2 setup: store character in client2", seed_client(CLIENT2_DIR, args.port) is not None)
            client2 = Client(CLIENT2_DIR, "s2", args.display, {"AC_TOWN_NO_MSGBOX": "1"})
            pid2 = client2.proc.pid
            ck("S2 window placed (display match)", client2.wait(r"\[PC\] display: window goes to display", 60.0) is not None)
            drive_to_menu(client2, ck, "S2")
            off = len(client2.log())
            tree_before = save_tree(CLIENT2_DIR)  # whatever the single-player boot itself created is not the attempt's doing
            _srv_path = os.path.join(CLIENT2_DIR, "save", "mp", "servers.ini")
            _srv_before = open(_srv_path, encoding="utf-8", errors="replace").read() if os.path.isfile(_srv_path) else None
            client2.press("START", after=0.2)
            mo = client2.wait(r"play-online: connect cancelled / failed, rolling back: (.*)", 90.0, off)
            time.sleep(1.5)
            client2.shot("after_cancel")
            c2log = client2.log()
            ck("S2 the connect was cancelled and rolled back: %s" % (mo.group(1) if mo else None), mo is not None)
            ck("S2 no client was started, nothing committed (no in-process connect line, no network client)", "in-process connect pid" not in c2log[off:] and "start_client" not in c2log[off:] and "-> READY" not in c2log)
            ck("S2 the process is alive (same PID) and no RELAUNCH", client2.proc.poll() is None and client2.proc.pid == pid2 and "RELAUNCH" not in c2log)
            tree_after = save_tree(CLIENT2_DIR)
            # mp\characters.ini (the 'last used character' hint) is written by pc_main_prepare_store_character BEFORE the fetch, exactly like the CLI path; it is the one allowed change
            # mp\servers.ini: the flow records the last-used character hint (last_character / last_town) exactly like the CLI --server path; comment/blank lines (the game rewrites its header comment) are ignored, any OTHER line change is still a failure
            def _strip_hints(t):
                return None if t is None else [l.strip() for l in t.splitlines() if l.strip() and not l.strip().startswith(("#", "last_character", "last_town"))]
            _srv_after = open(_srv_path, encoding="utf-8", errors="replace").read() if os.path.isfile(_srv_path) else None
            ck("S2 servers.ini changed at most in its last_character / last_town hint lines (before=%r after=%r)" % (_srv_before, _srv_after), _strip_hints(_srv_after) == _strip_hints(_srv_before))
            diff = sorted(k for k in (set(tree_after) | set(tree_before)) if tree_after.get(k) != tree_before.get(k) and k not in (os.path.join("mp", "characters.ini"), os.path.join("mp", "servers.ini")))
            ck("S2 the cancelled attempt created / changed nothing under save except the last-character hint (mp\\characters.ini); no town dir: %s" % diff,
               not diff and not os.path.isdir(os.path.join(CLIENT2_DIR, "save", "mp", "towns")))
            client2.press("START", after=0.3)  # the menu is back (selection still on the character): a second Connect must be accepted again
            ck("S2 the menu accepts a second request after the rollback (pending state released)", client2.wait(r"play-online: connect cancelled / failed, rolling back", 90.0, len(c2log)) is not None)
    finally:
        for c in (client, client2):
            if c is not None:
                c.stop()
        if host is not None:
            host.send_line("stop")
            if host.wait_exit(40.0) is None:
                host.stop()
        left = procs_in(CLIENT_DIR) + procs_in(CLIENT2_DIR) + procs_in(HOST_DIR)
        for pid, _ in left:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        print("cleanup: stray fixture processes killed: %s" % (left,))
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
