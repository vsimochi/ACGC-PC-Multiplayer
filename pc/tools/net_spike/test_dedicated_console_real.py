#!/usr/bin/env python3
r"""test_dedicated_console_real.py - the dedicated server's INTERACTIVE console, REAL process, REAL Windows console (its own console window, as when the exe is double-clicked / started from a shell).

TIER: a real `AnimalCrossing.exe --host <port> --dedicated` started with no redirected std handles, so it opens its own console and takes it over (VT output, ReadConsoleInputW line editor, locked writer,
stdout pump with -debug). The test reads that console's SCREEN BUFFER (ReadConsoleOutputCharacterW, what the window displays) and types into its INPUT BUFFER (WriteConsoleInputW key records, the
records the console's own keyboard queue produces) through ded_con_helper.py. It does NOT use the OS keyboard, mouse or window focus, so it is not a human-in-front-of-the-window test: colours, fonts and
the window's look are not judged. Asynchronous output is real: a transport client connects / disconnects and the server prints its own [DEDICATED] notices while the command is half typed.
Run on the isolated hidden desktop (hidden_desktop_run.py) with AC_DISPLAY_NAME=samsung AC_MASTER_VOLUME=0. Usage: python test_dedicated_console_real.py [--port 12980]"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "0")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wplay_lib as W  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = W.BIN
W.make_fixture()
import net_spike_lib as L  # noqa: E402

HELPER = os.path.join(HERE, "ded_con_helper.py")
TMP = tempfile.mkdtemp(prefix="dedcon ")


def screen(pid):
    out = os.path.join(TMP, "screen.json")
    try:
        os.remove(out)
    except OSError:
        pass
    r = subprocess.run([sys.executable, HELPER, str(pid), "read", out], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not os.path.exists(out):
        return None
    return json.load(open(out))


def send(pid, spec):
    r = subprocess.run([sys.executable, HELPER, str(pid), "send", spec], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print("   helper send failed:", r.stderr.strip()[-200:])
    return r.returncode == 0


def wait_for(pid, pred, timeout=60.0, step=0.5):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = screen(pid)
        if last is not None and pred(last):
            return last
        time.sleep(step)
    return None


def start_server(port, extra=()):
    env = dict(os.environ, AC_TEST_HOOKS="1")
    # no std handle redirection at all: the exe is the GUI subsystem build and opens its OWN console (interactive mode)
    p = subprocess.Popen([os.path.join(W.BIN, L.GAME_EXE_NAME), "--host", str(port), "--dedicated"] + list(extra), cwd=W.BIN, env=env, creationflags=0x00000010)  # CREATE_NEW_CONSOLE
    return p


def answer_prompts(pid, deadline=120.0):
    """first start of a disposable fixture: the legacy-town question and the town name are asked on the console; answer like an operator would"""
    end = time.time() + deadline
    answered = set()
    while time.time() < end:
        s = screen(pid)
        if s is None:
            time.sleep(0.5)
            continue
        text = "\n".join(s["rows"])
        if "Generate new town" in text and "legacy" not in answered:
            send(pid, "2{ENTER}")
            answered.add("legacy")
        elif "Enter town name" in text and "name" not in answered:
            time.sleep(1.0)
            send(pid, "Testy{ENTER}")
            answered.add("name")
        elif "observer active" in text or "world ready" in text.lower() or "Server console ready" in text and "legacy" not in text and "Enter town name" not in text and s["rows"] and s["rows"][-1].startswith(">"):
            if "observer active" in text:
                return s
        time.sleep(0.7)
    return None


def W_name(slot):
    raw = L.resident_player(slot).player_name
    return "".join(chr(b) if 0x20 <= b <= 0x7E else "?" for b in raw).rstrip(" \x00")


def scene(scene_id, seq):
    import struct
    return struct.pack("<BBBBHHI", L.PC_NETGAME_MSG_PLAYER_SCENE, 0, scene_id, L.PC_NETGAME_SCENE_FLAG_IN_TOWN if scene_id == 7 else 0, 0, 0, seq)


def prompt_row(s):
    return s["rows"][s["cy"]] if s and s["cy"] < len(s["rows"]) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12980)
    args = ap.parse_args()
    results = []
    ck = lambda d, c, x="": L.check(d + ((" :: " + str(x)[:300]) if (x and not c) else ""), bool(c), results)  # noqa: E731

    p = start_server(args.port)
    pid = p.pid
    clients = []
    try:
        s = answer_prompts(pid)
        ck("R0 the server booted in its own console and printed its notices", s is not None, "no boot screen")
        if s is None:
            return L.summary_and_exit_code(results)
        text = "\n".join(s["rows"])
        ck("R1 the console shows the editor's prompt row ('>') after boot, cursor right after it", prompt_row(s) == ">" and s["cx"] == 2, (prompt_row(s), s["cx"]))
        ck("R1b server notices are formatted: 'HH:MM:SS INFO  DEDICATED ...' (time, level, subsystem)", re.search(r"^\d\d:\d\d:\d\d INFO  DEDICATED +\S", text, re.M) is not None, text[-600:])

        # ---- a half typed command, then asynchronous output from a real network event ----
        import struct
        L.resolve_host_town("127.0.0.1", args.port)  # the harness's own town probe (a throw-away session) happens before anything is typed
        time.sleep(1.0)
        name0, name1 = ("Gina", "Rolf")
        send(pid, "sta")
        time.sleep(0.5)
        before = screen(pid)
        c1 = L.FakeClient("c1", "127.0.0.1", args.port, guest=L.guest_identity("Gina", 0x4A01))  # the fresh generated town has no residents: joins as a guest
        clients.append(c1)
        c1.connect_and_ready(quiet=True)
        c1.send_reliable(scene(7, 1))
        s = wait_for(pid, lambda x: any("arrived into town" in r for r in x["rows"]), 30)
        ck("R2 a real join (connect, identity, town scene) printed server notices while 'sta' was typed", s is not None, None if s is None else s["rows"][-6:])
        if s is not None:
            ck("R2b the typed text is intact on the prompt row and the cursor is right after it", prompt_row(s) == "> sta" and s["cx"] == 5, (prompt_row(s), s["cx"]))
            new = [r for r in s["rows"][len(before["rows"]) - 1:-1] if r.strip()]
            ck("R2c the notice rows are whole, time-stamped lines above the prompt: %s" % new[:3],
               new and all(re.match(r"\d\d:\d\d:\d\d (INFO |WARN |ERROR) ", r) for r in new), new)
            ck("R2d the interactive console shows the player-facing lines, in order: 'A client is connecting to the server...' then '%s arrived into town'" % name0,
               [r.split("DEDICATED", 1)[-1].strip() for r in new if "DEDICATED" in r][:2] == ["A client is connecting to the server...", "%s arrived into town" % name0], new)
            allrows = chr(10).join(s["rows"])
            ck("R2e the low-level transport lines ('peer N connected (transport...', 'peer N READY: ...') are NOT repeated on the interactive console (they stay in servers/default/logs/server.log)",
               "transport; awaiting identity" not in allrows and "READY: RESIDENT" not in allrows and "was READY" not in allrows, allrows[-500:])

        # ---- editing in the middle with more output in between ----
        send(pid, "{LEFT}{LEFT}x")  # s x t a   (cursor after x)
        s = screen(pid)
        ck("R3 inserting in the middle: '> sxta', cursor after the x (col 4)", prompt_row(s) == "> sxta" and s["cx"] == 4, (prompt_row(s), s["cx"]))
        c2 = L.FakeClient("c2", "127.0.0.1", args.port, guest=L.guest_identity("Rolf", 0x4A02))
        clients.append(c2)
        c2.connect_and_ready(quiet=True)
        c2.send_reliable(scene(7, 1))
        time.sleep(1.5)
        send(pid, "{BS}")  # removes x
        s = screen(pid)
        ck("R3b backspace after a second burst of output: '> sta', cursor at col 3", prompt_row(s) == "> sta" and s["cx"] == 3, (prompt_row(s), s["cx"]))
        send(pid, "{RIGHT}{RIGHT}")
        send(pid, "tus")
        c1.disconnect()
        time.sleep(2.0)
        s = screen(pid)
        ck("R4 'status' typed in pieces around three bursts of output is intact: '> status', cursor at its end", prompt_row(s) == "> status" and s["cx"] == 8, (prompt_row(s), s["cx"]))
        ck("R4b the departure of the first player was printed as '%s left town'" % name0, any(r.endswith("%s left town" % name0) for r in s["rows"]), s["rows"][-8:])

        # ---- submit: executes exactly once ----
        n_before = sum(1 for r in s["rows"] if "dedicated: yes" in r)
        send(pid, "{ENTER}")
        s = wait_for(pid, lambda x: sum(1 for r in x["rows"] if "dedicated: yes" in r) > n_before, 20)
        time.sleep(1.0)
        s = screen(pid)
        ck("R5 `status` ran exactly once (one 'dedicated: yes' response)", s is not None and sum(1 for r in s["rows"] if "dedicated: yes" in r) == n_before + 1, None if s is None else [r for r in s["rows"] if "dedicated" in r])
        ck("R5b the submitted command stays in the scrollback as '> status' once, and a fresh empty prompt follows", s is not None and sum(1 for r in s["rows"] if r == "> status") == 1 and prompt_row(s) == ">", None if s is None else s["rows"][-12:])

        # ---- shutdown while the console waits for input ----
        send(pid, "sto")
        c2.disconnect()
        time.sleep(1.0)
        s = screen(pid)
        ck("R6 half-typed 'sto' survives the disconnect notices", prompt_row(s) == "> sto", (prompt_row(s), s["cx"]))
        send(pid, "p{ENTER}")
        try:
            rc = p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            rc = None
        ck("R7 `stop` made the server exit by itself (exit code 0) while its console was waiting for input", rc == 0, rc)
    finally:
        for c in clients:
            try:
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if p.poll() is None:
            # only the process this test started (verified by pid)
            subprocess.run(["taskkill", "/PID", str(p.pid), "/F"], capture_output=True)

    # ---- second run: -debug output (the game's own stdout) goes through the pump ----
    p = start_server(args.port + 1, ["-debugnetwork"])
    pid = p.pid
    c3 = None
    try:
        s = answer_prompts(pid)
        ck("D0 a -debugnetwork server booted in its own console", s is not None)
        if s is not None:
            send(pid, "abc")
            before = screen(pid)
            c3 = L.TransportClient("c3", "127.0.0.1", args.port + 1)
            c3.connect()
            time.sleep(2.0)
            s = screen(pid)
            ck("D1 debug lines from the game's stdout arrive formatted as NETWORK rows while 'abc' is typed", s is not None and len(s["rows"]) > len(before["rows"]) and any(re.match(r"\d\d:\d\d:\d\d \w+ +NETWORK", r) for r in s["rows"]), None if s is None else s["rows"][-8:])
            ck("D1b with -debugnetwork the low-level 'peer N connected (transport; awaiting identity)' detail IS shown (debug output keeps the troubleshooting lines)", any("transport; awaiting identity" in r for r in s["rows"]), s["rows"][-8:])
            ck("D2 the typed text is still intact on the prompt row", prompt_row(s) == "> abc" and s["cx"] == 5, (prompt_row(s), s["cx"]))
            send(pid, "{ESC}stop{ENTER}")
            try:
                rc = p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                rc = None
            ck("D3 Escape clears the line, `stop` exits the server (code 0)", rc == 0, rc)
    finally:
        if c3 is not None:
            try:
                c3.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if p.poll() is None:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/F"], capture_output=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
