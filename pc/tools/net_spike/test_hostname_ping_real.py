#!/usr/bin/env python3
"""test_hostname_ping_real.py - release prep: hostname server addresses + the client ping counter.

TIER: REAL dedicated host + REAL client game processes on DISPOSABLE copies of pc\build64\bin_fixture4 (bin_fixture4_hping_rc = host, bin_fixture4_hping_rcc = client).
  A  servers.ini / --server-add / --servers: hostnames parse, persist UNCHANGED, list; malformed ones are refused (exit 2) and servers.ini is not touched
  B  a real client with an IPv4 literal (`--connect 127.0.0.1:P`) still reaches READY; show_ping off (default) prints no ping lines
  C  a real client with a HOSTNAME (`--connect localhost:P`, resolved by getaddrinfo) reaches READY; show_ping = 1 -> '[NET][PING] counter: first measurement N ms' with a plausible N
Usage: python test_hostname_ping_real.py [--port 11970]
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_hping_rc")
CLIENT_DIR = os.path.join(T.BUILD64, "bin_fixture4_hping_rcc")

if __name__ == "__main__":
    T.make_fixture("bin_fixture4_hping_rc")
    T.make_fixture("bin_fixture4_hping_rcc", empty_save=True)

import net_spike_lib as L  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
EXE = os.path.join(CLIENT_DIR, "AnimalCrossing.exe")
INI = "name = Pinger\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x1235\nland_id = 0x4321\n"
ENV = {"AC_TOWN_NO_MSGBOX": "1", "AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1"}


def sh(args, timeout=60):
    return subprocess.run([EXE] + args, cwd=CLIENT_DIR, capture_output=True, text=True, timeout=timeout)


def servers_text():
    p = os.path.join(CLIENT_DIR, "save", "mp", "servers.ini")
    return open(p, newline="").read() if os.path.isfile(p) else ""


def set_show_ping(v):
    p = os.path.join(CLIENT_DIR, "settings.ini")
    t = open(p, newline="").read()
    t = re.sub(r"(?m)^show_ping\s*=.*\r?\n?", "", t)
    if v:
        t = t.rstrip("\r\n") + "\nshow_ping = 1\n"
    open(p, "w", newline="").write(t)


def run_client(port, connect, uuid, results, ck, want_ping):
    client = L.ClientProcess(connect, extra_args=["--character", uuid, "--town-fetch"], log_path=T.log_path("hping_client_%s.log" % ("ping" if want_ping else "plain")),
                             bin_dir=CLIENT_DIR, env=ENV, label="hping").start()
    try:
        mo = client.wait_for_log(r"-> READY", 200.0)
        ck("%s: the client reached READY (%s)" % ("hostname" if not connect.startswith("127.") else "ipv4", connect), mo is not None)
        if mo is None:
            print("    client exit code: %s" % client.proc.poll())
            return None
        rtt = None
        if want_ping:
            m2 = client.wait_for_log(r"\[NET\]\[PING\] counter: first measurement (\d+) ms", 90.0)
            if m2 is not None:
                rtt = int(m2.group(1))
            time.sleep(1.0)
        else:
            time.sleep(8.0)
        return client.log_text(), rtt
    finally:
        client.stop()


def run(port, results):
    L.require_test_bin_dir()
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = None
    mp = os.path.join(CLIENT_DIR, "save", "mp")
    os.makedirs(mp, exist_ok=True)
    # ---------------- A: servers.ini / CLI
    r = sh(["--server-add", "tunnel", "example.gl.at.ply.gg:12345"])
    ck("A --server-add tunnel example.gl.at.ply.gg:12345 -> exit 0", r.returncode == 0)
    r = sh(["--server-add", "plain", "10.0.0.5:7777"])
    ck("A --server-add plain 10.0.0.5:7777 (IPv4 unchanged) -> exit 0", r.returncode == 0)
    r = sh(["--server-add", "dflt", "my-host.example.com"])
    ck("A a hostname without a port gets the default port 7777", r.returncode == 0 and "port=7777" not in "" and "my-host.example.com:7777" in r.stdout)
    t = servers_text()
    ck("A servers.ini persists the hostname unchanged", "address = example.gl.at.ply.gg" in t and "port = 12345" in t and "address = 10.0.0.5" in t and "address = my-host.example.com" in t)
    r = sh(["--servers"])
    ck("A --servers lists the hostname entry", "address=example.gl.at.ply.gg port=12345" in r.stdout and "address=10.0.0.5 port=7777" in r.stdout and r.returncode == 0)
    before = servers_text()
    bad = ["bad_host!:1", "-bad.example.com", "a..b.com", "host-.com", "1.2.3", "256.1.1.1", "1.2.3.4.5", "host:0", "host:99999", "host:", ":7777", "a" * 64, ("a" * 64) + ".com",
           "ex ample.com", "host:12ab"]
    refused = []
    for i, b in enumerate(bad):
        rr = sh(["--server-add", "bad%d" % i, b])
        if rr.returncode == 2:
            refused.append(b)
    ck("A all %d malformed addresses are refused with exit 2 (%d did)" % (len(bad), len(refused)), len(refused) == len(bad))
    ck("A servers.ini is untouched by the refusals", servers_text() == before)
    sh(["--server-delete", "tunnel"])
    sh(["--server-delete", "plain"])
    sh(["--server-delete", "dflt"])

    # ---------------- B / C: real connections
    try:
        with open(os.path.join(mp, "guest_pinger.ini"), "w", newline="") as f:
            f.write(INI)
        r = subprocess.run([EXE, "--character-import-profile", "pinger"], cwd=CLIENT_DIR, capture_output=True, text=True, timeout=60)
        m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
        ck("setup: the legacy profile was imported as a store character", r.returncode == 0 and m is not None)
        if m is None:
            return
        uuid = m.group(1)
        host = L.HostProcess(port=port, extra_args=["--dedicated", "--town-serve", "on"], log_path=T.log_path("hping_host.log"), bin_dir=HOST_DIR, stdin_pipe=True, verbose=False,
                             new_group=True).start()
        ok = host.wait_listening(60.0) and host.boot_to_dedicated(timeout=120.0)
        ck("host (dedicated, town-serve on) booted", ok)
        if not ok:
            return
        set_show_ping(False)
        out = run_client(port, "127.0.0.1:%d" % port, uuid, results, ck, False)
        if out is not None:
            ck("B show_ping off (default): no ping line in the client log", "[NET][PING]" not in out[0])
        set_show_ping(True)
        out = run_client(port, "localhost:%d" % port, uuid, results, ck, True)
        if out is not None:
            text, rtt = out
            ck("C the hostname connect resolved localhost (no connect failure line)", "client connect failed" not in text.lower())
            ck("C show_ping = 1: '[NET][PING] counter ON' and a first measurement", "[NET][PING] counter ON (client)" in text and rtt is not None)
            ck("C the measured RTT is plausible for loopback (0 < %s ms <= 250)" % rtt, rtt is not None and 0 < rtt <= 250)
    finally:
        if host is not None:
            host.send_line("stop")
            host.wait_exit(60.0)
            host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11970)
    args = ap.parse_args()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: disposable pc\build64\bin_fixture4_<name> copies only", file=sys.stderr)
        return 2
    results = []
    run(args.port, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
