#!/usr/bin/env python3
"""test_capacity_phase4.py - player-capacity project, PHASE 4 (dynamic guest admission: max_guests 1..254, admission decoupled from transport / store / residents).

TIER: native tests (Linux gcc, Winsock shim, also ASan/UBSan) + SOURCE AUDITS. No game process.
 1. guest_admission_selftest.c  the REAL pc_net.c over loopback with fake clients + the REAL pure admission rules (pc_guest_admit.c), the REAL membership classifier and the REAL per-guest
                                store; the host around them is a MODEL (bound-guest table by wire id). Default 4 guests, 30 guests, all 254 wire ids, reuse after disconnect, persistent identity
                                after a "restart", guest-limit vs transport-full vs token-invalid vs untrusted vs allow_new, strict parsing of max_guests, 300 stored guests.
 2. guest_store_selftest + peer_table_selftest again (the pieces phase 4 builds on).
 3. source audits of pc_net_game.c / pc_main.c / pc_settings.c / pc_dedicated.c.
NOT covered (needs the Windows / MSYS2 game build): pc_net_game.c itself running against real clients (only syntax-checked), puppets, real guest records.
Exit code 0 when all checks pass."""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
CC = os.environ.get("CC", "gcc")
results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("PASS - " if ok else "FAIL - ") + name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def strip_comments(t):
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", t, flags=re.S))


def run_c(scratch, name, srcs, args, sanitize=False, shim_tu=False, timeout=900):
    tag = name + (" [ASan+UBSan]" if sanitize else "")
    san = ["-fsanitize=address,undefined", "-fno-sanitize-recover=undefined"] if sanitize else []
    base = [CC, "-std=gnu11", "-Wall", "-Wextra", "-O1"] + san + ["-I", os.path.join(PC, "include")]
    exe = os.path.join(scratch, name + ("_asan" if sanitize else ""))
    objs, ok = [], True
    units = [(os.path.join(HERE, name + ".c"), shim_tu)] + [(os.path.join(PC, "src", s), False) for s in srcs]
    for i, (src, shim) in enumerate(units):
        obj = os.path.join(scratch, "%s_%d%s.o" % (name, i, "_a" if sanitize else ""))
        cmd = base + (["-D_WIN32", "-I", os.path.join(HERE, "winshim")] if shim else []) + ["-c", src, "-o", obj]
        cp = subprocess.run(cmd, capture_output=True, text=True)
        if cp.returncode != 0 or cp.stderr.strip():
            print(cp.stderr[:1500])
            ok = False
        objs.append(obj)
    check("compile %s (-Wall -Wextra, warning-free)" % tag, ok)
    if not ok:
        return None
    lp = subprocess.run(base + objs + ["-o", exe], capture_output=True, text=True)
    if lp.returncode != 0:
        print(lp.stderr[:1500])
        check("link %s" % tag, False)
        return None
    os.makedirs(args[0], exist_ok=True)
    rp = subprocess.run([exe] + args, capture_output=True, text=True, timeout=timeout)
    m = re.search(r"RESULT passed=(\d+) failed=(\d+)", rp.stdout)
    for ln in rp.stdout.splitlines():
        if ln.startswith("FAIL: "):
            check("%s: %s" % (tag, ln[6:]), False)
    check("%s: all checks pass (exit 0, %s)" % (tag, m.group(0) if m else "no RESULT; stderr: " + rp.stderr[-300:]), rp.returncode == 0 and m is not None and m.group(2) == "0")
    return rp.stdout


def main():
    scratch = os.path.join(tempfile.gettempdir(), "acmp_capacity4")
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    adm_srcs = ["pc_peer_table.c", "pc_guest_admit.c", "pc_mp_guest_store.c", "pc_mp_guests.c", "pc_mp_membership.c", "pc_mp_records.c"]
    if os.name == "nt":
        print("SKIP - guest_admission_selftest needs POSIX sockets (winshim); run it on Linux / WSL")
    else:
        out = run_c(scratch, "guest_admission_selftest", adm_srcs, [os.path.join(scratch, "adm")], shim_tu=True)
        if out is not None:
            need = ["default (max_guests 4): exactly 4 guests admitted", "max_guests 30: 30 guests admitted", "30 UNIQUE valid wire ids", "254 guests admitted, every wire id 0..254 except 8",
                    "10 new guests are admitted into the freed capacity", "known guests reconnect after the restart", "TOKEN_INVALID", "UNTRUSTED store refuses", "TRANSPORT_FULL",
                    "300 stored guests", "parse: 1..254 accepted"]
            check("admission test ran every scenario (default / 30 / 254 / reuse / restart / exhaustion / invalid config / store)", all(any(n in ln for ln in out.splitlines()) for n in need))
        run_c(scratch, "guest_admission_selftest", adm_srcs, [os.path.join(scratch, "adm_asan")], sanitize=True, shim_tu=True)
    gs = ["pc_mp_guest_store.c", "pc_mp_guests.c", "pc_mp_membership.c", "pc_mp_records.c"]
    run_c(scratch, "guest_store_selftest", gs, [os.path.join(scratch, "gs")])
    run_c(scratch, "peer_table_selftest", ["pc_peer_table.c"], [os.path.join(scratch, "pt")])

    ng, st, mn, dd = read("pc/src/pc_net_game.c"), read("pc/src/pc_settings.c"), read("pc/src/pc_main.c"), read("pc/src/pc_dedicated.c")
    code = strip_comments(ng)
    check("max_guests is no longer clamped to the old table size: pcnetgame_host_max_guests uses pc_guest_admit_clamp, PC_NETGAME_GUEST_MAX appears in no admission code",
          "pc_guest_admit_clamp(g_pc_max_guests_override" in code and not re.search(r"max_guests[^\n]*PC_NETGAME_GUEST_MAX|PC_NETGAME_GUEST_MAX[^\n]*max_guests", code) and
          len(re.findall(r"\bPC_NETGAME_GUEST_MAX\b", code)) == 3)  # define, static assert, inline size
    check("the admission gate is the pure pc_guest_admit_decide (guest limit vs transport full) and keeps the existing REJECT(SERVER_FULL) / log wording", "pc_guest_admit_decide(&in, buf, buf_size)" in code and
          "guest limit reached (%d of max_guests=%d guests are connected)" in read("pc/src/pc_guest_admit.c") and "pcnetgame_host_guest_cap_refusal(" in code and "guest table is full (no room for a new guest)" in code)
    check("settings.ini max_guests and --max-guests use the same strict parser and range (no atoi, no 1..8)", "pc_guest_admit_parse(value" in st and "val <= 8) g_pc_settings.max_guests" not in st and
          "pc_guest_admit_parse(argv[i + 1], &mg)" in mn and "mg > 8" not in mn)
    check("host start logs the configured / effective / transport numbers and warns when max_guests exceeds the transport capacity", "guests that can really be admitted" in ng and "is above the transport capacity" in ng)
    check("status prints configured / bound / available / stored guests, the transport numbers, and the reasons a guest is refused", all(s in dd for s in ("(configured), admissible now", "available %d", "guest store: %d guest(s) stored",
          "guest admission full", "transport full", "token invalid", "host untrusted")) and "pc_net_game_dedicated_capacity" in dd)
    check("`give peer N` accepts any peer id (not only one digit)", "pn = pn * 10 + (*d - '0')" in ng)
    check("residents stay 4 and are untouched: PC_NETGAME_RESIDENT_TABLE_SIZE 4, the guest gate only READS resident state (pcnetgame_host_resident_peer_reserve)", "#define PC_NETGAME_RESIDENT_TABLE_SIZE   4" in ng and
          "const int own = pcnetgame_host_own_resident_idx();" in code)
    diff = subprocess.run(["git", "-C", ROOT, "diff", "-U0", "HEAD", "--", "pc/src/pc_net_game.c"], capture_output=True, text=True).stdout
    hunks = [int(m.group(1)) for m in re.finditer(r"^@@ -(\d+)", diff, re.M)]
    check("wire unchanged: no hunk of pc_net_game.c inside the message / struct definitions except the guest-limit comment (lines %s)" % hunks[:3], all(h > 3000 or h in (186, 619, 620) for h in hunks))
    changed = subprocess.run(["git", "-C", ROOT, "diff", "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()
    check("Rover / radial menu / character files not touched (phases 5+ are covered by test_capacity_phase567.py)", not [f for f in changed if re.search(r"rover|radial|character", f, re.I)])
    bad = [n for n, ok in results if not ok]
    print("\nRESULT passed=%d failed=%d" % (len(results) - len(bad), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
