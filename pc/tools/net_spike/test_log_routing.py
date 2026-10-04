#!/usr/bin/env python3
"""test_log_routing.py - pc_log OUTPUT ROUTING with real host launches (one small run per scenario; ports 12300+).

TIER: REAL game host processes (`--host PORT --bootstrap-resident 0`, no client) from the DISPOSABLE fixture dir pc\\build64\\bin_fixture4 (the
live / protected dirs are refused by net_spike_lib; the fixture save is auto-snapshotted and restored at exit). stdout+stderr are captured to a
file by Popen directly (net_spike_lib.HostProcess hard-codes --verbose, which is the very thing under test). One process at a time.

WHAT APPEARS (the precise contract this test pins):
  * no flag                 : NOTHING (stdout/stderr go to NUL exactly as before)
  * -debug                  : stdout kept + unbuffered; ONLY [GENERAL]-tagged new lines (the startup summary) plus every pre-existing UNCONDITIONAL
                              printf/OSReport line (e.g. [NET] hosting..., [NET][WORLD] host: world ready...). No [NETWORK]/[PLAYERS]/... new lines.
  * -debugnetwork           : same, with the [NETWORK] new lines instead (host listening, world ready, peer events); no [GENERAL]/[VILLAGERS]/[PLAYERS].
  * --debug=network,villagers: [NETWORK] and [VILLAGERS] new lines only ([PLAYERS] absent; scenario e is its positive control)
  * NOTE OSReport() lines (e.g. '[PC] --bootstrap-resident ...: resident bound') are gated on g_pc_verbose in pc_os.c, so only --verbose shows them
  * --verbose (legacy)      : unchanged: the legacy anchor lines, and NO new category-tagged line at all (LEGACY|GENERAL only gates old code)
  * -debugplayers -logtime -logfile F : stdout and F both carry the [PLAYERS] line with a [t+S.mmm fN] stamp; F carries no legacy printf line
  * -logfile F alone        : stdout stays quiet (redirected to NUL), F holds only the header line
  * env PC_LOG=villagers    : like -debugvillagers; env PC_NPC_TALKHOLD_DIAG=1 alone: output stays quiet (alias bit never un-redirects stdout)
Usage: python test_log_routing.py [--port 12300]"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402

CATS = "GENERAL|NETWORK|PLAYERS|VILLAGERS|BUILDINGS|ITEMS|WILDLIFE|SAVE|EVENTS|TRANSACTIONS|RECORDS|GUESTS|TOWNSVC|MAIL|SHOP|LEGACY"
TAG_RX = re.compile(r"^\[(%s)\] " % CATS, re.M)
WORLD_READY_RX = r"\[NET\]\[WORLD\] host: world ready \(land_id=0x"
BOUND_RX = r"\[PC\] --bootstrap-resident 0: resident bound, transitioning to town \(SCENE_FG\)"


class Run:
    def __init__(self, port, args, env=None, tag=""):
        L.require_launchable_bin_dir(L.GAME_BIN_DIR)
        self.exe = os.path.join(L.GAME_BIN_DIR, L.GAME_EXE_NAME)
        self.port = port
        self.log = os.path.join(tempfile.gettempdir(), "test_log_routing_%d_%s.out" % (port, tag))
        self.args = ["--host", str(port), "--bootstrap-resident", "0"] + list(args)
        e = dict(os.environ)
        for k in ("PC_LOG", "PC_PUPPET_DIAG", "PC_COLLIDE_DIAG", "PC_NPC_TALKHOLD_DIAG"):
            e.pop(k, None)
        if env:
            e.update(env)
        self.fp = open(self.log, "wb")
        self.proc = subprocess.Popen([self.exe] + self.args, cwd=L.GAME_BIN_DIR, stdout=self.fp, stderr=subprocess.STDOUT, env=e)

    def text(self):
        try:
            with open(self.log, "rb") as f:
                return f.read().decode("utf-8", "replace").replace("\r", "")
        except OSError:
            return ""

    def wait(self, rx, timeout):
        rxc = re.compile(rx)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if rxc.search(self.text()):
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.2)
        return False

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(8)
        self.fp.close()


def tags_of(text):
    return sorted(set(TAG_RX.findall(text)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12300)
    a = ap.parse_args()
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    tmp = tempfile.mkdtemp(prefix="pclogroute_")
    port = a.port

    # (a) no flag: quiet
    r = Run(port, [], tag="quiet")
    try:
        time.sleep(18)
        alive = r.alive()
        out = r.text()
    finally:
        r.stop()
    ck("a: plain --host --bootstrap-resident 0 booted and is still running after 18 s", alive)
    ck("a: no flag -> NOTHING on stdout/stderr (quiet exactly as before; %d bytes)" % len(out), out == "")
    port += 1

    # (b) -debugnetwork
    r = Run(port, ["-debugnetwork"], tag="net")
    try:
        ok = r.wait(r"\[NETWORK\] host world ready", 120)
        time.sleep(1.5)
        out = r.text()
    finally:
        r.stop()
    ck("b: -debugnetwork: the [NETWORK] 'host world ready' line appears", ok)
    ck("b: only the NETWORK category is tagged (tags seen: %s)" % tags_of(out), tags_of(out) == ["NETWORK"])
    ck("b: [NETWORK] host listening line present", re.search(r"^\[NETWORK\] host listening: UDP port %d protocol \d+" % (port), out, re.M) is not None)
    ck("b: unconditional legacy printf lines still appear (console is on): [NET] hosting + [NET][WORLD] world ready",
       ("[NET] hosting on UDP port %d (protocol" % port) in out and re.search(WORLD_READY_RX, out) is not None)
    ck("b: no [GENERAL] startup summary (GENERAL not selected) and no legacy-only (g_pc_verbose) speedhack-style line", "log mask=" not in out)
    port += 1

    # (c) -debug : GENERAL only
    r = Run(port, ["-debug"], tag="general")
    try:
        ok = r.wait(WORLD_READY_RX, 120)
        time.sleep(1.5)
        out = r.text()
    finally:
        r.stop()
    ck("c: -debug: host reached world ready", ok)
    ck("c: only GENERAL is tagged (tags seen: %s)" % tags_of(out), tags_of(out) == ["GENERAL"])
    ck("c: the GENERAL startup summary shows mask 0x00000001 and verbose=0", "[GENERAL] log mask=0x00000001 (verbose=0 console=1)" in out)
    ck("c: -debug does NOT enable the subsystems: no [NETWORK]/[VILLAGERS]/[PLAYERS] line although world ready ran (the same point that prints "
       "[NETWORK]/[VILLAGERS] in b/d); OSReport lines such as the bootstrap bind stay legacy-gated, so they are absent too",
       "[NETWORK]" not in out and "[VILLAGERS]" not in out and "[PLAYERS]" not in out and re.search(WORLD_READY_RX, out) is not None
       and re.search(BOUND_RX, out) is None)
    port += 1

    # (d) multi list
    r = Run(port, ["--debug=network,villagers"], tag="multi")
    try:
        ok = r.wait(r"\[VILLAGERS\] host villager roster at world ready", 120)
        r.wait(BOUND_RX, 5)
        time.sleep(1.5)
        out = r.text()
    finally:
        r.stop()
    ck("d: --debug=network,villagers: the [VILLAGERS] roster line appears", ok)
    ck("d: exactly NETWORK and VILLAGERS are tagged (tags seen: %s)" % tags_of(out), tags_of(out) == ["NETWORK", "VILLAGERS"])
    ck("d: [PLAYERS] is absent (positive control: -debugplayers prints it in scenario e) and the host did reach world ready",
       "[PLAYERS]" not in out and re.search(WORLD_READY_RX, out) is not None)
    ck("d: the roster line carries the villager count", re.search(r"^\[VILLAGERS\] host villager roster at world ready: now_npc_max=\d+", out, re.M) is not None)
    port += 1

    # (e) logtime + logfile with -debugplayers
    lf = os.path.join(tmp, "players.log")
    r = Run(port, ["-debugplayers", "-logtime", "-logfile", lf], tag="file")
    try:
        ok = r.wait(r"\[PLAYERS\] \[t\+", 120)
        r.wait(WORLD_READY_RX, 60)
        time.sleep(1.5)
        out = r.text()
    finally:
        r.stop()
    ftxt = open(lf, "rb").read().decode("utf-8", "replace").replace("\r", "") if os.path.exists(lf) else ""
    ck("e: the [PLAYERS] bootstrap line appears on stdout with a [t+S.mmm fN] stamp", ok and re.search(r"^\[PLAYERS\] \[t\+\d+\.\d{3} f\d+\] bootstrap resident 0 bound", out, re.M) is not None)
    ck("e: -logfile F was written: header + the same [PLAYERS] line (tags in file: %s)" % tags_of(ftxt),
       "[GENERAL] log file opened" in ftxt and re.search(r"^\[PLAYERS\] \[t\+\d+\.\d{3} f\d+\] bootstrap resident 0 bound", ftxt, re.M) is not None
       and set(tags_of(ftxt)) <= {"GENERAL", "PLAYERS"})
    ck("e: the file holds PC_LOG lines only (no legacy printf such as '[NET] hosting')", "[NET] hosting" not in ftxt and "[PC] --bootstrap" not in ftxt)
    ck("e: stdout carries the legacy lines too (console kept)", ("[NET] hosting on UDP port %d" % port) in out)
    port += 1

    # (e2) logfile alone
    lf2 = os.path.join(tmp, "only.log")
    r = Run(port, ["-logfile", lf2], tag="fileonly")
    try:
        time.sleep(18)
        alive = r.alive()
        out = r.text()
    finally:
        r.stop()
    f2 = open(lf2, "rb").read().decode("utf-8", "replace").replace("\r", "") if os.path.exists(lf2) else ""
    ck("e2: -logfile alone: host running and stdout stays quiet (%d bytes; redirect to NUL kept)" % len(out), alive and out == "")
    ck("e2: the file exists and holds only the header line", f2.splitlines() == [l for l in f2.splitlines() if l.startswith("[GENERAL] log file opened")] and "log file opened" in f2)
    port += 1

    # (f) --verbose legacy
    r = Run(port, ["--verbose"], tag="verbose")
    try:
        ok = r.wait(WORLD_READY_RX, 120)
        r.wait(BOUND_RX, 5)
        time.sleep(1.5)
        out = r.text()
    finally:
        r.stop()
    ck("f: --verbose: legacy anchor '[NET] hosting on UDP port' present", ("[NET] hosting on UDP port %d (protocol" % port) in out)
    ck("f: --verbose: '[NET][WORLD] host: world ready (land_id=0x' anchor present", ok)
    ck("f: --verbose: '[PC] --bootstrap-resident 0: resident bound, transitioning to town (SCENE_FG)' anchor present", re.search(BOUND_RX, out) is not None)
    ck("f: --verbose alone adds NO new category-tagged line (tags seen: %s; no 'log mask' summary)" % tags_of(out), tags_of(out) == [] and "log mask=" not in out)
    ck("f: --verbose output is non-empty and unbuffered-looking (>= 5 lines)", len(out.splitlines()) >= 5)
    verbose_text = out
    port += 1

    # (g) env PC_LOG
    r = Run(port, [], env={"PC_LOG": "villagers"}, tag="env")
    try:
        ok = r.wait(r"\[VILLAGERS\] host villager roster", 120)
        time.sleep(1.0)
        out = r.text()
    finally:
        r.stop()
    ck("g: env PC_LOG=villagers (no flag): the [VILLAGERS] line appears and nothing else is tagged (tags: %s)" % tags_of(out), ok and tags_of(out) == ["VILLAGERS"])
    port += 1

    # (h) legacy env alias alone stays quiet
    r = Run(port, [], env={"PC_NPC_TALKHOLD_DIAG": "1"}, tag="alias")
    try:
        time.sleep(18)
        alive = r.alive()
        out = r.text()
    finally:
        r.stop()
    ck("h: PC_NPC_TALKHOLD_DIAG=1 alone: running and still quiet (the alias bit never un-redirects stdout; %d bytes)" % len(out), alive and out == "")

    # sanity: nothing left behind
    left = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AnimalCrossing.exe"], capture_output=True).stdout.decode("utf-8", "replace")
    ck("no AnimalCrossing.exe process left running", "AnimalCrossing.exe" not in left)
    L.info("verbose run log kept at %s" % os.path.join(tempfile.gettempdir(), "test_log_routing_*_verbose.out"))
    try:
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        os.rmdir(tmp)
    except OSError:
        pass
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
