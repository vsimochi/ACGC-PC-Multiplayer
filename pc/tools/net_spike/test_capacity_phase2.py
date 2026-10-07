#!/usr/bin/env python3
"""test_capacity_phase2.py - player-capacity project, PHASE 2 (dynamic peer / transport capacity). Focused: the peer-table logic, the REAL transport over loopback, source audits.

TIER: native unit + native loopback (Linux, Winsock shim) + SOURCE AUDIT. No game process.
 1. peer_table_selftest.c   pure logic of pc/src/pc_peer_table.c: id space (host id 8 skipped, 0xFF never, max 254), free-slot choice / reuse / exhaustion for EVERY capacity 1..254,
                            the 256-bit peer set, runtime-sized per-peer tables (inline -> heap -> inline, zeroing, all-or-nothing under allocation failure). Also run under ASan/UBSan.
 2. peer_table_loopback_selftest.c  the REAL pc/src/pc_net.c (#included) driven over 127.0.0.1 by fake UDP clients: capacity 8 == the old table, 20 (ids 0..7, 9..20), 254 (the
                            maximum, event queue sized for it), refusal of a new address when full, id reuse, reliable / unreliable / broadcast / heartbeat above id 8, nonce restart,
                            client mode, bind failure, restart with another capacity. Needs POSIX sockets (tools/net_spike/winshim maps the few Winsock names): SKIPPED on Windows.
 3. source audits: PC_NET_MAX_PEERS is gone; every per-peer table is a registered PCNG_PEER_TABLE; the wire format (messages, transport constants) is unchanged; guests.dat /
                            guest admission untouched; settings / CLI for max_peers; Rover / radial menu / character files untouched.
NOT covered (needs the Windows / MSYS2 game build): the game layer running against a real host (pc_net_game.c is only syntax-checked), puppets, real clients.
Exit code 0 when all checks pass."""
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
CC = os.environ.get("CC", "gcc")
PARENT = "d703822"  # capacity phase 1
results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("PASS - " if ok else "FAIL - ") + name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def fd_limit():
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(max(soft, 4096), hard), hard))
    except Exception:
        pass


def build_run(scratch, name, srcs, extra=(), sanitize=False, timeout=240):
    exe = os.path.join(scratch, name + ("_asan" if sanitize else ""))
    cmd = [CC, "-std=gnu11", "-Wall", "-Wextra", "-O1"] + (["-fsanitize=address,undefined", "-fno-sanitize-recover=undefined"] if sanitize else []) + list(extra) + \
          ["-I", os.path.join(PC, "include"), os.path.join(HERE, name + ".c")] + [os.path.join(PC, "src", f) for f in srcs] + ["-o", exe]
    cp = subprocess.run(cmd, capture_output=True, text=True)
    tag = name + (" [ASan+UBSan]" if sanitize else "")
    check("compile %s (-Wall -Wextra, warning-free)" % tag, cp.returncode == 0 and cp.stderr.strip() == "")
    if cp.returncode != 0:
        print(cp.stderr)
        return None
    rp = subprocess.run([exe], capture_output=True, text=True, timeout=timeout, preexec_fn=fd_limit)
    m = re.search(r"RESULT passed=(\d+) failed=(\d+)", rp.stdout)
    for ln in rp.stdout.splitlines():
        if ln.startswith("FAIL: "):
            check("%s: %s" % (tag, ln[6:]), False)
    check("%s: all checks pass (exit 0, %s)" % (tag, m.group(0) if m else "no RESULT line; stderr: " + rp.stderr[-200:]), rp.returncode == 0 and m is not None and m.group(2) == "0")
    return rp.stdout


def main():
    scratch = os.path.join(tempfile.gettempdir(), "acmp_capacity2")
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)

    out = build_run(scratch, "peer_table_selftest", ["pc_peer_table.c"])
    if out is not None:
        need = ["every capacity 1..254: exactly `capacity` usable ids", "every capacity 1..254: `capacity` allocations succeed in increasing id order, skip the host id",
                "capacity 8 (the old table): ids are exactly 0..7", "set: every wire id 0..255 can be a member", "a failed allocation fails the whole resize",
                "...and changes NOTHING"]
        check("peer_table_selftest ran the id-space, allocation, set and all-or-nothing groups", all(("PASS: " + n) in out for n in need))
    build_run(scratch, "peer_table_selftest", ["pc_peer_table.c"], sanitize=True)

    if os.name == "nt":
        print("SKIP - loopback transport test needs POSIX sockets (tools/net_spike/winshim); run it on Linux/WSL")
    else:
        shim = ["-D_WIN32", "-I", os.path.join(HERE, "winshim")]
        out = build_run(scratch, "peer_table_loopback_selftest", ["pc_peer_table.c"], extra=shim)
        if out is not None:
            need = ["capacity 8: CONNECTED events carry ids 0..7 in order", "the 9th address is refused", "ids are 0..7 then 9..20: the host's id 8 is NEVER assigned",
                    "254 CONNECTED events: every id 0..254 except 8 exactly once", "not one CONNECTED event was lost with 254 undrained",
                    "reliable sends to ids 9 and 20 (above the old table)", "a refused address now gets the lowest freed id (0)", "...and leaves NO tables behind",
                    "the HELLO_ACK connects the client"]
            check("loopback test ran every scenario (8 / 20 / 254 / bind failure / restart / client)", all(any(ln.startswith("PASS: " + n) for ln in out.splitlines()) for n in need))
        build_run(scratch, "peer_table_loopback_selftest", ["pc_peer_table.c"], extra=shim, sanitize=True, timeout=400)

    # ---------------- source audits ----------------
    srcs = {}
    for dp, _, fs in os.walk(os.path.join(PC, "src")):
        for f in fs:
            if f.endswith((".c", ".h", ".cpp")):
                srcs[os.path.join(dp, f)] = open(os.path.join(dp, f), encoding="utf-8", errors="replace").read()
    for dp, _, fs in os.walk(os.path.join(PC, "include")):
        for f in fs:
            if f.endswith(".h"):
                srcs[os.path.join(dp, f)] = open(os.path.join(dp, f), encoding="utf-8", errors="replace").read()
    left = [os.path.relpath(p, ROOT) for p, t in srcs.items() if re.search(r"\bPC_NET_MAX_PEERS\b", t) and not p.endswith("pc_net.h")]
    check("PC_NET_MAX_PEERS is gone from every source / header (only the explanatory comment in pc_net.h names it)", not left and len(re.findall(r"\bPC_NET_MAX_PEERS\b", read("pc/include/pc_net.h"))) == 1)
    nh = read("pc/include/pc_net.h")
    check("pc_net.h: reserved id 8, runtime capacity API, no fixed table size", "#define PC_NET_RESERVED_PEER_ID 8" in nh and "int pc_net_set_peer_capacity(int capacity);" in nh and
          "int pc_net_peer_capacity(void);" in nh and "int pc_net_peer_span(void);" in nh)

    ng = read("pc/src/pc_net_game.c")
    decl = re.findall(r"^PCNG_PEER_TABLE2?\(\s*[\w ]+,\s*(\w+)", ng, re.M)
    reg = [r for r in re.findall(r"PCNG_REG\((\w+)\)", ng) if r != "NAME"]
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", ng, flags=re.S))  # the audit looks at code, not at comments that mention the names
    check("every per-peer host table is a PCNG_PEER_TABLE and is in the registry (%d declared, %d registered, same set)" % (len(decl), len(reg)), len(decl) >= 19 and sorted(decl) == sorted(reg))
    bare = [t for t in decl if re.search(r"sizeof\(\s*%s\s*\)" % re.escape(t), ng)]
    check("no sizeof() of a whole per-peer table (it is a pointer now; only element sizeof is allowed)", not bare)
    unindexed = []
    for t in decl:
        for m in re.finditer(r"\b%s\b(?!\[|_inl|_bind)" % re.escape(t), code):
            line = code[code.rfind("\n", 0, m.start()) + 1: code.find("\n", m.start())]
            if "PCNG_PEER_TABLE" not in line and "PCNG_REG" not in line:
                unindexed.append((t, line.strip()[:80]))
    check("every use of a per-peer table is indexed (NAME[...]), none passes / compares the pointer", not unindexed)
    check("tables resize all-or-nothing at host start, back to inline at shutdown, and a failed resize aborts the host start", "pcnetgame_peer_tables_resize(pc_net_peer_span())" in ng and
          "pcnetgame_peer_tables_release();" in ng and "could not allocate the per-peer host state" in ng)
    check("loops / bounds over peers use the game's own span: the transport's pc_net_peer_span() appears only where the tables are resized and in the start-up log",
          len(re.findall(r"pc_net_peer_span\(\)", code)) == 2 and "pcnetgame_peer_tables_resize(pc_net_peer_span())" in code)
    check("player-id keyed arrays span the whole wire id space (clients cannot know the host's capacity)", "s_action_seq_out[PC_NETGAME_WIRE_ID_SPACE]" in ng and "s_action_last_seq[PC_NETGAME_WIRE_ID_SPACE]" in ng)
    check("'is this origin a client peer?' no longer means origin < peer table size (the host id is a hole once there are more than 8 peers)", "if (origin < PC_NET_MAX_PEERS)" not in ng and ng.count("if (origin != (int)PC_NETGAME_HOST_PLAYER_ID)") == 2)
    check("talk-hold holders are a 256-bit PCPeerSet: no 16-bit shift of a peer id remains", "(uint16_t)(1u << peer)" not in ng and "(uint16_t)(1u << p)" not in ng and "uint16_t peer_mask" not in ng)
    check("admission semantics unchanged: max_guests default 4, clamp 1..8, cap check before the transport reserve", ".max_guests = 4" in read("pc/src/pc_settings.c") and "return n > PC_NETGAME_GUEST_MAX ? PC_NETGAME_GUEST_MAX : n;" in ng and
          ng.index("bound >= cap") < ng.index("occupied + reserve > pc_net_peer_capacity()"))
    check("the transport reserve and the capacity shown by the status line use the runtime capacity", "*peers_total = pc_net_peer_capacity();" in ng)

    rp, rph = read("pc/src/pc_remote_player.c"), read("pc/include/pc_remote_player.h")
    check("puppet slots: the fixed 9-slot table of phase 2 is a dynamic pool since capacity phase 6 (see test_capacity_phase567.py)", "PC_REMOTE_PLAYER_ID_LIMIT 255" in rph and "pc_puppet_pool_acquire" in rp)
    check("host wire id still 8 and equals the transport's reserved id (static assert)", "#define PC_NETGAME_HOST_WIRE_ID 8" in read("pc/include/pc_net_game.h") and "PC_NET_RESERVED_PEER_ID == PC_NETGAME_HOST_WIRE_ID" in read("pc/include/pc_net_game.h"))

    nc = read("pc/src/pc_net.c")
    check("transport: a full table refuses the new address with the EXISTING DISCONNECT notice (no new wire message) and counts it", "s_stats.peer_table_full_refused++;" in nc and
          "pcnet_send_ctrl(from, PCNET_WIRE_DISCONNECT, NULL, 0);" in nc)
    check("transport: reserved id never allocated (shared pc_peer_find_free with PC_NET_RESERVED_PEER_ID)", "pc_peer_find_free(s_peer_span, PC_NET_RESERVED_PEER_ID" in nc)

    # wire format untouched: no hunk of pc_net_game.c before the per-peer tables (the message / struct definitions live above line 2800), transport wire constants + typedef blocks identical
    d = subprocess.run(["git", "-C", ROOT, "diff", "-U0", PARENT, "--", "pc/src/pc_net_game.c"], capture_output=True, text=True)
    if d.returncode == 0:
        first = min(int(m.group(1)) for m in re.finditer(r"^@@ -(\d+)", d.stdout, re.M))
        check("wire: no change to pc_net_game.c above line 2800 (every message id / struct / reject reason is defined there); first hunk at line %d" % first, first >= 2800)
        sys.path.insert(0, HERE)
        import wire_baseline as W
        gp = lambda p, ref: subprocess.run(["git", "-C", ROOT, "show", ref + ":" + p], capture_output=True, check=True).stdout.decode("utf-8", "replace").replace("\r\n", "\n")
        a, b = W.typedef_blocks(read("pc/src/pc_net.c")), W.typedef_blocks(gp("pc/src/pc_net.c", PARENT))
        check("wire: every typedef block of pc_net.c (header, ack, slot, tx / rx entries) identical to the parent", a == b)
        check("wire: PCNET_WIRE_* / MAGIC / HEARTBEAT / TIMEOUT identical to the parent", W.wire_defines_net_c(read("pc/src/pc_net.c")) == W.wire_defines_net_c(gp("pc/src/pc_net.c", PARENT)))
        a, b = W.typedef_blocks(read("pc/include/pc_net.h")), W.typedef_blocks(gp("pc/include/pc_net.h", PARENT))
        check("wire: the only pc_net.h typedef that changed is the DIAGNOSTIC PCNetStats (+1 counter)", [k for k in set(a) | set(b) if a.get(k) != b.get(k)] == ["PCNetStats"])
        changed = subprocess.run(["git", "-C", ROOT, "diff", "--name-only", PARENT], capture_output=True, text=True).stdout.split()
        bad = [f for f in changed if re.search(r"pc_mp_guests|pc_mp_records|pc_mp_members|pc_character\.|pc_session\.|ac_npc_guide2|pc_m_card\.c|pc_pad\.c|pc_tool_wheel|pc_play_online_menu", f)]
        check("untouched: guests.dat / records / members, characters, Rover, radial menu, Play Online menu", not bad)
    else:
        print("SKIP - git history for %s not available: the wire-diff audits need it" % PARENT)

    st = read("pc/src/pc_settings.c")
    check("settings: max_peers default 8, range 1..254, written to / read from settings.ini; --max-peers is host-only", ".max_peers = 8," in st and "pc_peer_capacity_max(PC_NET_RESERVED_PEER_ID)" in st and
          'fprintf(f, "max_peers = %d\\n"' in st and "--max-peers: REFUSED: it is a HOST-only option" in read("pc/src/pc_main.c"))

    bad = [n for n, ok in results if not ok]
    print("test_capacity_phase2: %d checks, %d failed" % (len(results), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
