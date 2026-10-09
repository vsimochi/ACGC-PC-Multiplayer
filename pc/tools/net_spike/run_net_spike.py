#!/usr/bin/env python3
"""run_net_spike.py - Stage-0 networking foundation verifier.

Builds net_spike.exe (net_spike.c + pc/src/pc_net.c, linked against ws2_32) with a direct
gcc invocation -- NOT part of the normal ac_pc CMake build, matching the existing
pc/tools/lowaddr_spike/verify_spike.py convention of a small throwaway harness driven by a
Python script.

Then runs two scenarios as real, separate OS processes (one "host", one or two "client"s --
this is deliberately not faked within a single process, since pc_net.c is designed as one
role per process, exactly like two real game instances on a LAN):

  Scenario 1 (single client):
    1.  host starts                                   -> SPIKE HOST_STARTED
    2.  client connects                                -> pc_net_client_connect() succeeds
    3.  host detects the client                        -> SPIKE HOST_PEER_CONNECTED
    4.  client detects the host                        -> SPIKE CLIENT_CONNECTED
    5.  host sends a test packet                       -> SPIKE HOST_SEND
    6.  client receives it                             -> SPIKE CLIENT_RECV
    7.  client sends a test packet                     -> SPIKE CLIENT_SEND
    8.  host receives it                                -> SPIKE HOST_RECV
    9.  multiple packets exchanged                      -> more than one HOST_RECV/CLIENT_RECV
    10. disconnect is detected cleanly                  -> SPIKE HOST_PEER_DISCONNECTED
    11. non-blocking polling does not hang               -> each process exits within its
                                                             requested duration + a small margin

  Scenario 2 (two clients -> one host):
    host reaches peer_count() == 2, with two distinct PEER_CONNECTED events.

Usage:  python3 run_net_spike.py
Exit code 0 if every check passes, 1 otherwise.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PC_DIR = os.path.abspath(os.path.join(HERE, "..", ".."))
INCLUDE = os.path.join(PC_DIR, "include")
SRC = os.path.join(PC_DIR, "src")
EXE = os.path.join(HERE, "net_spike.exe")

GCC = os.environ.get("NET_SPIKE_GCC", "gcc")


def build():
    cmd = [
        GCC, "-std=gnu11", "-O2", "-Wall", "-Wextra",
        "-I", INCLUDE,
        os.path.join(HERE, "net_spike.c"),
        os.path.join(SRC, "pc_net.c"),
        "-o", EXE,
        "-lws2_32",
    ]
    print("BUILD:", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        raise SystemExit("build failed")
    if r.stdout.strip():
        print(r.stdout)
    if r.stderr.strip():
        print(r.stderr)
    print("BUILD ok ->", EXE)


def run_capture(args, duration_ms, slack_s=2.0):
    """Runs one net_spike.exe invocation, returns (stdout_text, elapsed_seconds)."""
    start = time.time()
    p = subprocess.run(
        [EXE] + args, capture_output=True, text=True,
        timeout=(duration_ms / 1000.0) + slack_s,
    )
    elapsed = time.time() - start
    return p.stdout, elapsed


def run_concurrent(host_args, host_ms, client_specs, slack_s=3.0):
    """client_specs: list of (args, duration_ms). Starts host + all clients concurrently,
    waits for all to finish, returns dict label -> stdout, plus per-process elapsed time."""
    procs = {}
    starts = {}

    starts["host"] = time.time()
    procs["host"] = subprocess.Popen([EXE] + host_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.15)  # let the host bind its socket before any client's first HELLO is sent

    for label, args, _ in client_specs:
        starts[label] = time.time()
        procs[label] = subprocess.Popen([EXE] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    outputs = {}
    elapsed = {}
    max_ms = host_ms
    for _, _, ms in client_specs:
        max_ms = max(max_ms, ms)

    for label, proc in procs.items():
        try:
            out, _ = proc.communicate(timeout=(max_ms / 1000.0) + slack_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            out = (out or "") + "\n[[run_net_spike: process hung and was killed]]\n"
        outputs[label] = out
        elapsed[label] = time.time() - starts[label]
    return outputs, elapsed


class Check:
    def __init__(self):
        self.results = []

    def ok(self, name, cond, detail=""):
        self.results.append((name, bool(cond), detail))
        print(("PASS" if cond else "FAIL") + " - " + name + (f" ({detail})" if detail else ""))

    def summary(self):
        failed = [n for n, ok, _ in self.results if not ok]
        print("-" * 60)
        print(f"{len(self.results) - len(failed)}/{len(self.results)} checks passed")
        if failed:
            print("FAILED:", ", ".join(failed))
        return len(failed) == 0


def main():
    build()
    chk = Check()

    # ---------------- Scenario 1: single host, single client ----------------
    host_ms = 2500
    client_ms = 1500  # client exits (and explicitly disconnects) well before the host does,
                       # so the host's own remaining runtime proves clean disconnect detection.
    port = 47321

    outputs, elapsed = run_concurrent(
        host_args=["host", str(port), str(host_ms)],
        host_ms=host_ms,
        client_specs=[("clientA", ["client", "127.0.0.1", str(port), str(client_ms), "A"], client_ms)],
    )
    host_out = outputs["host"]
    client_out = outputs["clientA"]

    print("\n--- host output ---")
    print(host_out)
    print("--- client output ---")
    print(client_out)

    chk.ok("1. host starts", "SPIKE HOST_STARTED" in host_out)
    chk.ok("2. client connects (process reported success)", "SPIKE CLIENT_STARTED" in client_out)
    chk.ok("3. host detects client", "SPIKE HOST_PEER_CONNECTED" in host_out)
    chk.ok("4. client detects host", "SPIKE CLIENT_CONNECTED" in client_out)
    chk.ok("5. host sends a test packet", "SPIKE HOST_SEND" in host_out)
    chk.ok("6. client receives it", "SPIKE CLIENT_RECV" in client_out)
    chk.ok("7. client sends a test packet", "SPIKE CLIENT_SEND" in client_out)
    chk.ok("8. host receives it", "SPIKE HOST_RECV" in host_out)

    host_recv_count = host_out.count("SPIKE HOST_RECV")
    client_recv_count = client_out.count("SPIKE CLIENT_RECV")
    chk.ok("9. multiple packets exchanged", host_recv_count > 1 and client_recv_count > 1,
           f"host_recv={host_recv_count} client_recv={client_recv_count}")

    chk.ok("10. disconnect detected cleanly", "SPIKE HOST_PEER_DISCONNECTED" in host_out)

    host_within_bounds = elapsed["host"] < (host_ms / 1000.0) + 2.0
    client_within_bounds = elapsed["clientA"] < (client_ms / 1000.0) + 2.0
    chk.ok("11. non-blocking polling does not hang",
           host_within_bounds and client_within_bounds,
           f"host={elapsed['host']:.2f}s client={elapsed['clientA']:.2f}s (budgets "
           f"{host_ms/1000.0+2.0:.2f}s / {client_ms/1000.0+2.0:.2f}s)")

    # ---------------- Scenario 2: two clients -> one host ----------------
    port2 = 47322
    host_ms2 = 2000
    client_ms2 = 1500
    outputs2, elapsed2 = run_concurrent(
        host_args=["host", str(port2), str(host_ms2)],
        host_ms=host_ms2,
        client_specs=[
            ("clientA2", ["client", "127.0.0.1", str(port2), str(client_ms2), "A2"], client_ms2),
            ("clientB2", ["client", "127.0.0.1", str(port2), str(client_ms2), "B2"], client_ms2),
        ],
    )
    host_out2 = outputs2["host"]
    print("\n--- two-client host output ---")
    print(host_out2)

    connected_peers = set()
    for line in host_out2.splitlines():
        if "SPIKE HOST_PEER_CONNECTED" in line:
            connected_peers.add(line.strip().split("peer=")[-1])
    chk.ok("12. two clients connecting to one host both detected",
           len(connected_peers) == 2, f"distinct peers seen: {connected_peers}")

    ok = chk.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
