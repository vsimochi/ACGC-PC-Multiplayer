#!/usr/bin/env python3
"""test_dedicated_legacy_prompt.py - first launch of a dedicated server that finds a valid LEGACY town (save/card_a/...gci) ASKS on the console instead of adopting it silently.

REAL processes (`AnimalCrossing.exe --host P --dedicated`), console input through the process's stdin, on a DISPOSABLE copy of a fixture (never the live dir): the copy is made fresh for every case from
NET_SPIKE_GAME_BIN (a bin_fixture4* directory) with its servers/ tree removed.
  P1 legacy town + stdin "1": the prompt names the town, the legacy town is COPIED into servers/default/town/card_a (byte-identical), server.ini origin = legacy, the legacy save is byte-identical to before
  P2 legacy town + stdin "2" + a town name: NO copy of the legacy town, a NEW town is generated (server.ini origin = generated, a different GCI), the legacy save is byte-identical
  P3 an existing server-owned town (the directory left by P2): NO prompt, the server loads it
Usage: python test_dedicated_legacy_prompt.py [--port 11990]
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

GCI = "DobutsunomoriP_MURA.gci"


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Run:
    def __init__(self, d, port):
        self.out = []
        self.p = subprocess.Popen([os.path.join(d, "AnimalCrossing.exe"), "--host", str(port), "--dedicated"], cwd=d, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=self._rd, daemon=True).start()

    def _rd(self):
        for ln in iter(self.p.stdout.readline, b""):
            self.out.append(ln.decode("latin-1"))

    def text(self):
        return "".join(self.out)

    def send(self, s):
        self.p.stdin.write(s.encode())
        self.p.stdin.flush()

    def wait(self, rx, t):
        end = time.time() + t
        while time.time() < end:
            if re.search(rx, self.text()):
                return True
            time.sleep(0.2)
        return False

    def kill(self):
        try:
            self.p.kill()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5)


def fresh(src, dst):
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    shutil.rmtree(os.path.join(dst, "servers"), ignore_errors=True)


def ini(d):
    p = os.path.join(d, "servers", "default", "server.ini")
    return open(p, encoding="latin-1").read() if os.path.exists(p) else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11990)
    args = ap.parse_args()
    L.require_test_bin_dir()
    src = L.GAME_BIN_DIR
    if not os.path.basename(os.path.normpath(src)).startswith("bin_fixture4"):
        print("REFUSING: run on a bin_fixture4* copy", file=sys.stderr)
        return 2
    d = os.path.join(os.path.dirname(os.path.normpath(src)), "bin_fixture4_dlp")
    results = []
    ck = lambda t, c: L.check(t, bool(c), results)  # noqa: E731
    legacy = os.path.join(d, "save", "card_a", GCI)
    srv_gci = os.path.join(d, "servers", "default", "town", "card_a", GCI)

    fresh(src, d)
    h0 = sha(legacy)
    r = Run(d, args.port)
    try:
        ck("P1 the prompt appears, naming the detected town, with both options", r.wait(r"We noticed a save with the town named \"\w+\"\.\s+Would you like to use this town or generate a new one\?\s+\[1\] Use this town\s+\[2\] Generate new town", 60))
        ck("P1 nothing was adopted before the answer", not os.path.exists(srv_gci))
        r.send("1\n")
        ck("P1 the legacy town was adopted by copy after answering 1", r.wait(r"adopted the legacy town", 60) and os.path.exists(srv_gci))
        ck("P1 the server copy is byte-identical to the legacy save", os.path.exists(srv_gci) and sha(srv_gci) == h0)
        ck("P1 the legacy save is byte-identical to before", sha(legacy) == h0)
        r.wait(r"town identity recorded", 90)
        ck("P1 server.ini origin = legacy", "origin = legacy" in ini(d).replace("=", " = ").replace("  ", " ") or re.search(r"origin\s*=\s*legacy", ini(d)))
    finally:
        r.kill()

    fresh(src, d)
    h0 = sha(legacy)
    r = Run(d, args.port + 1)
    try:
        ck("P2 the prompt appears", r.wait(r"\[2\] Generate new town", 60))
        r.send("2\n")
        ck("P2 then the normal town-name prompt", r.wait(r"Enter town name \(1-8 characters\)", 60))
        r.send("Newtown\n")
        ck("P2 a new town is generated and recorded (origin = generated)", r.wait(r"town identity recorded", 120) and re.search(r"origin\s*=\s*generated", ini(d)))
        ck("P2 the legacy town was NOT copied / adopted", "adopted the legacy town" not in r.text() and os.path.exists(srv_gci) and sha(srv_gci) != h0)
        ck("P2 the legacy save is byte-identical to before", sha(legacy) == h0)
    finally:
        r.kill()

    # P3: the directory left by P2 now owns a server town: no prompt at all
    ck("P3 precondition: the server owns a town (server.ini [town] present)", re.search(r"origin\s*=\s*generated", ini(d)))
    h0 = sha(legacy)
    r = Run(d, args.port + 2)
    try:
        ck("P3 an existing server-owned town loads without any prompt", r.wait(r"Loading town", 60) and "We noticed a save" not in r.text() and "[1] Use this town" not in r.text())
        ck("P3 the legacy save is still byte-identical", sha(legacy) == h0)
    finally:
        r.kill()
    shutil.rmtree(d, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
