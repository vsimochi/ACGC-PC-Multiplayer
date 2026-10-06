#!/usr/bin/env python3
"""test_resident_edge_real.py - resident credential EDGE cases with a REAL dedicated host and REAL client game processes (no FakeClient).

TIER: REAL host + REAL clients on DISPOSABLE copies of pc\\build64\\bin_fixture4: bin_fixture4_redge (host), bin_fixture4_redgec (STORE-character client, empty save dir),
bin_fixture4_redgel (LEGACY client: full town save, `--bootstrap-resident 1`, no character). Rig/helpers as test_resident_credentials_real.py. Host: `--dedicated --town-serve on
--resident-tokens tofu`, console through a stdin pipe. Files are only ever RENAMED aside / restored, never deleted.
  A  graceful host stop (console `stop`, exit code 0) -> restart (same port/save/members.dat): the store resident auto-reconnects ('identity re-established ... resident 1'),
     host logs KNOWN not MINTED, members.dat entry count unchanged, `residents` slot 1 once connected=yes, `players` lists exactly ONE bound RESIDENT idx 1 peer
  B  valid token.dat under a DIFFERENT (valid-format) town key dir only, the correct one renamed aside: REJECT 5 'presented NO token', no mint, members.dat byte-identical,
     host resident PIDs unchanged, other-key file / aside file untouched; restored token still authenticates (KNOWN)
  C  legacy `--connect --bootstrap-resident 1` (no character): RESIDENT claim, NEW mint after `resident-reset`, token in save/mp/resident_token.dat (PCMpGtk, keyed by the
     resident PID), legacy files kept; a SECOND run presents it (KNOWN, no new mint)
  D  hard kill + restart of the host with the legacy client connected: auto reconnect KNOWN, resident_token.dat byte-identical
  E  legacy resident_token.dat holding a token of ANOTHER town (valid format, different town key): REJECT 5 (NO token), members.dat / PIDs unchanged, real file untouched
Usage: python test_resident_edge_real.py [--port 11970]
"""
import argparse
import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_redge")
CLIENT_DIR = os.path.join(T.BUILD64, "bin_fixture4_redgec")
LEGACY_DIR = os.path.join(T.BUILD64, "bin_fixture4_redgel")
if __name__ == "__main__":
    T.make_fixture("bin_fixture4_redge")
    T.make_fixture("bin_fixture4_redgec", empty_save=True)
    T.make_fixture("bin_fixture4_redgel")

import net_spike_lib as L  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
HOST_GCI = os.path.join(HOST_DIR, L.SAVE_GCI_REL)  # the legacy FIXTURE file (seeds the dedicated server town on its first launch); runtime reads use L.host_gci(HOST_DIR)
EXE = os.path.join(CLIENT_DIR, "AnimalCrossing.exe")
MEMBERS = L.server_file("members.dat", HOST_DIR)  # the DEDICATED host's resident credentials: servers/default/residents/members.dat
LEGACY_TOK = os.path.join(LEGACY_DIR, "save", "mp", "resident_token.dat")
INI = "name = %s\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = %s\nland_id = 0x4321\n"
RES_PAT = r'slot 1: name="[^"]*" credential=(\w+) confirmed=(\S+) armed=(\w+) connected=(\w+)'
NO_TOKEN = "resident 1 has a credential and the claim presented NO token"


def pid_hex(slot):
    with open(L.host_gci(HOST_DIR), "rb") as f:
        f.seek(L._GCI_PRIVATE_BASE + slot * L._GCI_PRIVATE_STRIDE)
        return f.read(20).hex()


def all_pids():
    return [pid_hex(i) for i in range(4)]


def say(h, cmd, settle=1.5, timeout=25.0):
    off = len(h.log_text())
    assert h.send_line(cmd)
    end = time.monotonic() + timeout
    last, last_t = off, time.monotonic()
    while time.monotonic() < end:
        time.sleep(0.2)
        n = len(h.log_text())
        if n != last:
            last, last_t = n, time.monotonic()
        elif time.monotonic() - last_t >= settle and n > off:
            break
    return h.log_text()[off:].replace("\r\n", "\n")


def members_entries():
    try:
        b = open(MEMBERS, "rb").read()
    except OSError:
        return []
    out = []
    for i in range(16):
        p = 32 + i * 80
        if b[p] == 1:
            out.append((b[p + 2], b[p + 1], b[p + 3], b[p + 40:p + 56]))
    return out


def gtk_entries(path):
    """[(host_land_name+id+hash bytes (p+4..p+20), home_pid, token)] of a PCMpGtk file."""
    b = open(path, "rb").read()
    out = []
    for i in range(4):
        p = 32 + i * 64
        if b[p] == 1:
            out.append((b[p + 4:p + 20], b[p + 28:p + 48], b[p + 48:p + 64]))
    return out


def gtk_craft_other_town(src, dst):
    """valid PCMpGtk copy of `src` whose entries belong to ANOTHER town (host terrain hash + land name bytes changed), CRC fixed."""
    b = bytearray(open(src, "rb").read())
    for i in range(4):
        p = 32 + i * 64
        if b[p] == 1:
            b[p + 16] ^= 0xA5   # terrain hash
            b[p + 4] ^= 0x01    # land name first byte
            b[p + 12] ^= 0x01   # land id
    b[-4:] = struct.pack("<I", zlib.crc32(bytes(b[:-4])) & 0xFFFFFFFF)
    open(dst, "wb").write(bytes(b))


def rd(p):
    return open(p, "rb").read()


def start_host(port, tag):
    for i in range(2):
        h = L.HostProcess(port=port, extra_args=["--dedicated", "--town-serve", "on", "--resident-tokens", "tofu"], log_path=T.log_path("redge_host_%s_%d.log" % (tag, i)),
                          bin_dir=HOST_DIR, stdin_pipe=True, verbose=False, new_group=True).start()
        if h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0):
            return h
        h.stop()
        time.sleep(2.0)
    return None


def run(port, results):
    L.require_test_bin_dir()
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = client = None
    mp = os.path.join(CLIENT_DIR, "save", "mp")
    ARGS = ["--town-fetch", "--online-ui"]

    def new_client(uuid, tag):
        return L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--character", uuid] + ARGS, log_path=T.log_path("redge_client_%s.log" % tag), bin_dir=CLIENT_DIR,
                               label="redgec").start()

    def new_legacy(tag):
        return L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--bootstrap-resident", "1"], log_path=T.log_path("redge_legacy_%s.log" % tag), bin_dir=LEGACY_DIR,
                               label="redgel").start()

    def refused(c, host_, off, why):
        mo = c.wait_for_log(r"CANNOT JOIN", 170.0)
        time.sleep(1.0)
        ct, ht = c.log_text(), host_.log_text()[off:]
        c.stop()
        ck("%s: client logged the REJECT 5 refusal text" % why, mo is not None and "resident credential missing or wrong" in ct)
        ck("%s: never READY, never stored a token" % why, "-> READY" not in ct and "first claim: resident token" not in ct and "resident token verified" not in ct)
        ck("%s: host 'REFUSED before READY: ... %s'" % (why, NO_TOKEN), re.search(r"REFUSED before READY: .*" + re.escape(NO_TOKEN), ht) is not None)
        ck("%s: nothing minted" % why, "credential MINTED" not in ht)
        return ct, ht

    try:
        L.server_wipe_sidecars(HOST_DIR)
        os.makedirs(mp, exist_ok=True)
        with open(os.path.join(mp, "servers.ini"), "w", newline="") as f:
            f.write("[server]\nname = redgesrv\naddress = 127.0.0.1\nport = %d\n" % port)
        r = None
        with open(os.path.join(mp, "guest_roger.ini"), "w", newline="") as f:
            f.write(INI % ("Roger", "0x1234"))
        r = subprocess.run([EXE, "--character-import-profile", "roger"], cwd=CLIENT_DIR, capture_output=True, text=True, timeout=60)
        m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
        uuid = m.group(1) if m else None
        ck("setup: store character imported", uuid is not None)
        if uuid is None:
            return
        rpid = pid_hex(1)
        pids0 = all_pids()

        host = start_host(port, "a1")
        ck("host (dedicated, tofu) booted", host is not None)
        if host is None:
            return
        # fetch-only launch to learn the town key, then resident membership
        c = new_client(uuid, "prep")
        mo = c.wait_for_log(r"fetch: (?:installed|UP_TO_DATE) town ([0-9a-f_]{30})", 170.0)
        key = mo.group(1) if mo else None
        td = os.path.join(mp, "characters", uuid, "towns", key) if key else ""
        if key:
            os.makedirs(td, exist_ok=True)
        c.stop()
        ck("setup: town installed, town dir created", bool(key) and os.path.isdir(td))
        if not key:
            return
        with open(os.path.join(td, "membership.ini"), "w", newline="") as f:
            f.write("role = resident\ntown_pid = %s\nlast_server = redgesrv\n" % rpid)
        tpath = os.path.join(td, "token.dat")
        time.sleep(7.0)

        # ---------------- A: graceful stop -> restart -> reconnect
        client = new_client(uuid, "a")
        mo = client.wait_for_log(r"-> READY", 170.0)
        time.sleep(2.0)
        me = members_entries()
        ck("A first claim: READY, host MINTED once, one members.dat entry (resident 1, kind 1), token.dat stored",
           mo is not None and host.log_text().count("credential MINTED") == 1 and len(me) == 1 and me[0][0] == 1 and me[0][2] == 1 and os.path.isfile(tpath)
           and gtk_entries(tpath)[0][2] == me[0][3])
        tok = me[0][3]
        good_store = rd(tpath)
        coff = len(client.log_text())
        host.send_line("stop")
        code = host.wait_exit(60.0)
        ck("A graceful stop: the host exited with code 0 (got %r)" % (code,), code == 0)
        host.stop()
        ck("A members.dat survived the graceful stop: one entry, same token", len(members_entries()) == 1 and members_entries()[0][3] == tok)
        host = start_host(port, "a2")
        ck("A host restarted on the same port/save/members.dat", host is not None)
        if host is None:
            return
        mo = client.wait_for_log(r"reconnect successful .*identity re-established \(resident: resident 1", 170.0, since_offset=coff)
        time.sleep(2.0)
        ht = host.log_text()
        ck("A client auto-reconnected in the SAME process: 'identity re-established ... resident 1'", mo is not None and client.proc.poll() is None)
        ck("A host: 1 credential restored, KNOWN (verified), not MINTED/NEW", "1 resident credential(s) restored" in ht and "resident 1 credential verified (token sent: KNOWN)" in ht and "MINTED" not in ht)
        me = members_entries()
        ck("A members.dat entry count unchanged (1, no second mint), same token, confirmed", len(me) == 1 and me[0][3] == tok and me[0][1] == 1)
        ck("A client token.dat byte-identical", rd(tpath) == good_store)
        out = say(host, "residents")
        mo = re.search(RES_PAT, out)
        ck("A `residents`: slot 1 listed once, credential=yes confirmed=yes connected=yes", mo is not None and mo.groups() == ("yes", "yes", "no", "yes") and len(re.findall(r"slot 1:", out)) == 1)
        out = say(host, "players")
        ck("A `players`: exactly one peer, one bound RESIDENT idx 1 (no duplicate/stale binding)", "players: 1" in out and len(re.findall(r"class=RESIDENT idx 1", out)) == 1 and len(re.findall(r"peer \d+:", out)) == 1)
        ck("A host log: resident 1 bound exactly once since the restart", ht.count("bound to resident 1") == 1)
        client.stop()
        client = None
        time.sleep(9.0)

        # ---------------- B: token under the WRONG town key
        other = key[:-1] + ("0" if key[-1] != "0" else "1")
        odir = os.path.join(mp, "characters", uuid, "towns", other)
        os.makedirs(odir, exist_ok=True)
        shutil.copy2(tpath, os.path.join(odir, "token.dat"))
        aside = tpath + ".aside"
        os.replace(tpath, aside)
        mem_before, pids_before, other_bytes = rd(MEMBERS), all_pids(), rd(os.path.join(odir, "token.dat"))
        off = len(host.log_text())
        c = new_client(uuid, "b")
        ct, ht = refused(c, host, off, "B token under another town key")
        ck("B the client had NO credential for this town: 'token not held (first claim)'", "sent IDENTITY_EXT (token not held (first claim))" in ct)
        ck("B members.dat byte-identical, host resident PIDs unchanged (no second resident record)", rd(MEMBERS) == mem_before and all_pids() == pids_before == pids0 and len(members_entries()) == 1)
        ck("B correct-town token.dat was not created/overwritten; aside + other-key files untouched", not os.path.exists(tpath) and rd(aside) == good_store and rd(os.path.join(odir, "token.dat")) == other_bytes)
        os.replace(aside, tpath)
        ck("B correct credential restored byte-identical", rd(tpath) == good_store)
        time.sleep(7.0)
        client = new_client(uuid, "b2")
        mo = client.wait_for_log(r"-> READY", 170.0)
        time.sleep(2.0)
        ck("B after restoring: the same client is admitted KNOWN (credential intact, still no second mint)",
           mo is not None and "resident token verified by the host (resident 1)" in client.log_text() and "credential MINTED" not in host.log_text()[off:] and len(members_entries()) == 1)
        client.stop()
        client = None
        time.sleep(9.0)

        # ---------------- C: legacy --bootstrap-resident
        out = say(host, "resident-reset 1 confirm", timeout=40.0)
        ck("C setup: resident-reset 1 (disposable town), members empty", "credential RESET by the operator" in out and members_entries() == [])
        before = set(os.path.relpath(p, LEGACY_DIR) for p in glob.glob(os.path.join(LEGACY_DIR, "save", "**", "*"), recursive=True) if os.path.isfile(p))
        ck("C no resident_token.dat before the first legacy run", not os.path.exists(LEGACY_TOK))
        off = len(host.log_text())
        lc = new_legacy("c1")
        mo = lc.wait_for_log(r"-> READY", 170.0)
        time.sleep(2.0)
        ct, ht = lc.log_text(), host.log_text()[off:]
        me = members_entries()
        ck("C legacy client: RESIDENT claim without token, READY; host MINTED (NEW) once; one members entry", mo is not None and "playing a resident -- sent IDENTITY_EXT (token not held (first claim))" in ct
           and ht.count("credential MINTED") == 1 and len(me) == 1 and me[0][0] == 1)
        ck("C legacy client logged 'first claim: resident token ... saved to save/mp/resident_token.dat'", "first claim: resident token (resident 1) saved to save/mp/resident_token.dat" in ct)
        ents = gtk_entries(LEGACY_TOK) if os.path.isfile(LEGACY_TOK) else []
        ck("C resident_token.dat exists (PCMpGtk): ONE entry, token == host's, keyed by the resident PersonalID", len(ents) == 1 and me and ents[0][2] == me[0][3] and ents[0][1].hex() == rpid)
        after = set(os.path.relpath(p, LEGACY_DIR) for p in glob.glob(os.path.join(LEGACY_DIR, "save", "**", "*"), recursive=True) if os.path.isfile(p))
        ck("C no pre-existing legacy file was deleted", before <= after)
        ck("C no token text in logs", me and all(me[0][3].hex() not in open(p, errors="replace").read() for p in glob.glob(T.log_path("redge_*.log"))))
        legacy_bytes = rd(LEGACY_TOK)
        lc.stop()
        time.sleep(9.0)
        off = len(host.log_text())
        lc = new_legacy("c2")
        mo = lc.wait_for_log(r"-> READY", 170.0)
        time.sleep(2.0)
        ct, ht = lc.log_text(), host.log_text()[off:]
        ck("C second legacy run presents the token: 'token held', host KNOWN (verified), no new mint, client 'verified'",
           mo is not None and "sent IDENTITY_EXT (token held)" in ct and "resident 1 credential verified (token sent: KNOWN)" in ht and "MINTED" not in ht
           and "resident token verified by the host (resident 1)" in ct and len(members_entries()) == 1 and members_entries()[0][3] == me[0][3])
        ck("C resident_token.dat byte-identical after the second run", rd(LEGACY_TOK) == legacy_bytes)

        # ---------------- D: hard kill + restart with the legacy client connected
        client = lc
        coff = len(lc.log_text())
        host.proc.kill()
        host.proc.wait()
        host.stop()
        mo = lc.wait_for_log(r"\[NET\]\[RECONNECT\] client: host link lost", 60.0, since_offset=coff)
        ck("D legacy client noticed the dead host, process alive", mo is not None and lc.proc.poll() is None)
        host = start_host(port, "d")
        ck("D host restarted", host is not None)
        if host is None:
            return
        mo = lc.wait_for_log(r"reconnect successful .*identity re-established \(resident: resident 1", 170.0, since_offset=coff)
        time.sleep(2.0)
        ht = host.log_text()
        ck("D auto reconnect (same process): identity re-established resident 1; host KNOWN, not MINTED", mo is not None and "resident 1 credential verified (token sent: KNOWN)" in ht and "MINTED" not in ht)
        ck("D resident_token.dat byte-identical, members entry count 1, same token", rd(LEGACY_TOK) == legacy_bytes and len(members_entries()) == 1 and members_entries()[0][3] == me[0][3])
        out = say(host, "players")
        ck("D `players`: one bound RESIDENT idx 1", "players: 1" in out and len(re.findall(r"class=RESIDENT idx 1", out)) == 1)
        lc.stop()
        client = None
        time.sleep(9.0)

        # ---------------- E: token of ANOTHER town in resident_token.dat
        legacy_aside = LEGACY_TOK + ".aside"
        os.replace(LEGACY_TOK, legacy_aside)
        gtk_craft_other_town(legacy_aside, LEGACY_TOK)
        crafted = rd(LEGACY_TOK)
        ck("E crafted file is valid PCMpGtk with the SAME token but another town key", gtk_entries(LEGACY_TOK)[0][2] == me[0][3] and gtk_entries(LEGACY_TOK)[0][0] != gtk_entries(legacy_aside)[0][0])
        mem_before, pids_before = rd(MEMBERS), all_pids()
        off = len(host.log_text())
        lc = new_legacy("e")
        ct, ht = refused(lc, host, off, "E other-town token")
        ck("E the legacy client held no token for THIS town ('not held (first claim)')", "sent IDENTITY_EXT (token not held (first claim))" in ct)
        ck("E members.dat byte-identical, resident PIDs unchanged, still one credential", rd(MEMBERS) == mem_before and all_pids() == pids_before and len(members_entries()) == 1)
        ck("E crafted file untouched, real credential (aside) untouched", rd(LEGACY_TOK) == crafted and rd(legacy_aside) == legacy_bytes)
        os.replace(LEGACY_TOK, LEGACY_TOK + ".crafted")
        os.replace(legacy_aside, LEGACY_TOK)
        ck("E real credential file restored byte-identical", rd(LEGACY_TOK) == legacy_bytes)
    finally:
        if client is not None:
            client.stop()
        if host is not None:
            host.send_line("stop")
            if host.wait_exit(40.0) is None:
                host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11970)
    args = ap.parse_args()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: disposable pc\\build64\\bin_fixture4_<name> copies only", file=sys.stderr)
        return 2
    results = []
    run(args.port, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
