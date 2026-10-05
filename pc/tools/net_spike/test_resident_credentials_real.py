#!/usr/bin/env python3
"""test_resident_credentials_real.py - M-E resident credential round trip with a REAL dedicated host and REAL client processes (store characters, no FakeClient).

TIER: REAL host + REAL client game processes on DISPOSABLE copies of pc\\build64\\bin_fixture4 (bin_fixture4_rcred = host, bin_fixture4_rcredc = client with an EMPTY save dir).
Nothing else is touched (not the live save dir, bin_talkfix*, bin_fixture4). Client: `--connect 127.0.0.1:P --town-fetch --online-ui --character <uuid>`; the character's
membership.ini (role = resident, town_pid = the PersonalID of fixture resident 1) is written by the test after a fetch-only launch created the town dir (no guest claim is made).
Host: `--dedicated --town-serve on --resident-tokens tofu` (then `required` in H), console through a stdin pipe.
  A  first resident claim: the host mints a NEW credential (members.dat, one entry), the client reaches READY
  B  the client stored it: characters/<uuid>/towns/<key>/token.dat (PCMpGtk, ONE entry = the minted token, resident PID), exactly one town dir; no token text in any log
  C  abrupt host kill (TerminateProcess), the real client's auto reconnect (backoff 1,2,3,3 s) logs 'host link lost'
  D  the host restarts on the same port / save / members.dat: host logs KNOWN (not MINTED), the client logs 'identity re-established ... resident 1', still one entry
  E  `residents`: slot 1 credential=yes confirmed=yes connected=yes; no token text; a SECOND character (copy of the town + same resident PID, no token) is refused REJECT 5 while
     the real resident stays connected (no eviction)
  F  token.dat replaced by a different, valid PCMpGtk token: REJECT 5 (WRONG token), the client logs the refusal text, never READY, token.dat untouched
  G  token.dat missing: REJECT 5 (credential exists, NO token); `resident-reset 1 confirm`; the same client claims again and gets a NEW (different) token
  H  `--resident-tokens required` with a fresh members.dat: no credential -> REJECT 5 (not armed); `resident-arm 1 confirm`; exactly one mint; a second client process with no token is
     refused again (and again once armed was used up)
Usage: python test_resident_credentials_real.py [--port 11960]
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

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_rcred")
CLIENT_DIR = os.path.join(T.BUILD64, "bin_fixture4_rcredc")
if __name__ == "__main__":
    T.make_fixture("bin_fixture4_rcred")
    T.make_fixture("bin_fixture4_rcredc", empty_save=True)

import net_spike_lib as L  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
HOST_GCI = os.path.join(HOST_DIR, L.SAVE_GCI_REL)
EXE = os.path.join(CLIENT_DIR, "AnimalCrossing.exe")
MEMBERS = os.path.join(HOST_DIR, "save", "mp", "members.dat")
INI = "name = %s\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = %s\nland_id = 0x4321\n"
RES_PAT = r'slot 1: name="[^"]*" credential=(\w+) confirmed=(\S+) armed=(\w+) connected=(\w+)'


def resident_pid_hex(slot):
    with open(HOST_GCI, "rb") as f:
        f.seek(L._GCI_PRIVATE_BASE + slot * L._GCI_PRIVATE_STRIDE)
        return f.read(20).hex()


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
    """[(res_slot, confirmed, kind, token_bytes)] of the host members.dat (parsed straight from the bytes), [] when absent."""
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


def gtk_token(path):
    """(present entry count, token bytes of the first entry) of a PCMpGtk file."""
    b = open(path, "rb").read()
    n, tok = 0, None
    for i in range(4):
        p = 32 + i * 64
        if b[p] == 1:
            n += 1
            tok = tok or b[p + 48:p + 64]
    return n, tok


def gtk_set_token(path, new_tok):
    b = bytearray(open(path, "rb").read())
    for i in range(4):
        p = 32 + i * 64
        if b[p] == 1:
            b[p + 48:p + 64] = new_tok
    crc = zlib.crc32(bytes(b[:-4])) & 0xFFFFFFFF  # pc_mp_records_crc32 = standard CRC-32
    b[-4:] = struct.pack("<I", crc)
    open(path, "wb").write(bytes(b))


def start_host(port, policy, tag):
    for i in range(2):
        h = L.HostProcess(port=port, extra_args=["--dedicated", "--town-serve", "on", "--resident-tokens", policy], log_path=T.log_path("rcred_host_%s_%d.log" % (tag, i)),
                          bin_dir=HOST_DIR, stdin_pipe=True, verbose=False, new_group=True).start()
        if h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0):
            return h
        h.stop()
        time.sleep(2.0)
    return None


def stop_host(h):
    if h is None:
        return
    h.send_line("stop")
    if h.wait_exit(40.0) is None:
        h.stop()


def run(port, results):
    L.require_test_bin_dir()
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = client = client2 = None
    mp = os.path.join(CLIENT_DIR, "save", "mp")
    ARGS = ["--town-fetch", "--online-ui"]

    def new_client(uuid, tag):
        return L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--character", uuid] + ARGS, log_path=T.log_path("rcred_client_%s.log" % tag), bin_dir=CLIENT_DIR,
                               label="rcredc").start()

    def import_char(name, pid_hex):
        with open(os.path.join(mp, "guest_%s.ini" % name.lower()), "w", newline="") as f:
            f.write(INI % (name, pid_hex))
        r = subprocess.run([EXE, "--character-import-profile", name.lower()], cwd=CLIENT_DIR, capture_output=True, text=True, timeout=60)
        m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
        return m.group(1) if m else None

    def prepare_resident(uuid, tag, rpid):
        """fetch-only launch (killed right after the town is installed, before any claim) -> towns/<key>/membership.ini = resident."""
        c = new_client(uuid, "prep_" + tag)
        mo = c.wait_for_log(r"fetch: (?:installed|UP_TO_DATE) town ([0-9a-f_]{30})", 170.0)
        key = mo.group(1) if mo else None
        td = os.path.join(mp, "characters", uuid, "towns", key) if key else ""
        if key:
            os.makedirs(td, exist_ok=True)  # the town dir is normally created by the first claim; made here BEFORE any claim (the client is killed right after the fetch)
        c.stop()
        ok = bool(key) and os.path.isdir(td)
        if ok:
            with open(os.path.join(td, "membership.ini"), "w", newline="") as f:
                f.write("role = resident\ntown_pid = %s\nlast_server = rcredsrv\n" % rpid)
        return key, td, ok

    def run_refused(uuid, tag, host_, expect_host, why):
        """launch a client that must be refused; returns (client log text, host log slice)."""
        off = len(host_.log_text())
        c = new_client(uuid, tag)
        mo = c.wait_for_log(r"CANNOT JOIN", 170.0)
        time.sleep(1.0)
        ct, ht = c.log_text(), host_.log_text()[off:]
        c.stop()
        ck("%s: the client logged the REJECT 5 refusal text ('resident credential missing or wrong')" % why, mo is not None and "resident credential missing or wrong" in ct)
        ck("%s: it never reached READY and never bound / stored a token" % why, "-> READY" not in ct and "first claim: resident token" not in ct and "resident token verified" not in ct)
        ck("%s: host: REFUSED before READY: %s" % (why, expect_host), re.search(r"REFUSED before READY: .*" + re.escape(expect_host), ht) is not None)
        return ct, ht

    try:
        shutil.rmtree(os.path.join(HOST_DIR, "save", "mp"), ignore_errors=True)  # disposable host dir: no stale members.dat
        os.makedirs(mp, exist_ok=True)
        with open(os.path.join(mp, "servers.ini"), "w", newline="") as f:
            f.write("[server]\nname = rcredsrv\naddress = 127.0.0.1\nport = %d\n" % port)
        uuid = import_char("Roger", "0x1234")
        uuid2 = import_char("Mallory", "0x2345")
        ck("setup: two store characters imported", uuid is not None and uuid2 is not None)
        if uuid is None or uuid2 is None:
            return
        rpid = resident_pid_hex(1)

        host = start_host(port, "tofu", "tofu")
        ck("host (dedicated, town-serve on, resident-tokens tofu) booted", host is not None)
        if host is None:
            return
        ck("host: no members.dat before the first claim", members_entries() == [])

        key, td, ok = prepare_resident(uuid, "a", rpid)
        ck("setup: the fetch-only launch installed the town and the resident membership was written", ok)
        if not ok:
            return
        ck("setup: the fetch-only launch made no claim (no token.dat, nothing minted)", not os.path.exists(os.path.join(td, "token.dat")) and members_entries() == [])
        time.sleep(7.0)  # host drops the dead peer, if any

        # ---------------- A: first claim
        client = new_client(uuid, "main")
        mo = client.wait_for_log(r"-> READY", 170.0)
        time.sleep(2.0)
        ct, ht = client.log_text(), host.log_text()
        ck("A the client resolved the resident membership, bound slot 1 by PersonalID, sent the RESIDENT claim without a token and reached READY",
           mo is not None and "--resident-by-pid: resident PersonalID matches slot 1" in ct and "playing a resident -- sent IDENTITY_EXT (token not held (first claim))" in ct)
        ck("A the host MINTED a NEW credential for resident 1 (first claim, token sent: NEW)", "resident 1 credential MINTED (first claim, token sent: NEW)" in ht and ht.count("credential MINTED") == 1)
        me = members_entries()
        ck("A members.dat: exactly one entry, kind RESIDENT_TOKEN (1), resident slot 1", len(me) == 1 and me[0][0] == 1 and me[0][2] == 1)
        # ---------------- B: client stored it
        tdirs = glob.glob(os.path.join(mp, "characters", uuid, "towns", "*"))
        tpath = os.path.join(td, "token.dat")
        ck("B exactly one town dir under the character (no second town key dir)", len(tdirs) == 1 and os.path.basename(tdirs[0]) == key)
        ck("B client log: 'first claim: resident token ... saved' and token.dat exists", "first claim: resident token (resident 1) saved to" in ct and os.path.isfile(tpath))
        n, tok = gtk_token(tpath) if os.path.isfile(tpath) else (0, None)
        ck("B token.dat holds ONE PCMpGtk entry whose token equals the host's minted token", n == 1 and me and tok == me[0][3])
        tok_hex = tok.hex() if tok else "x"
        ck("B membership.ini: role = resident, town_pid = the resident PID", ("town_pid = " + rpid) in open(os.path.join(td, "membership.ini")).read())
        ck("B the host never printed the token; the client never logged it", tok_hex not in host.log_text() and tok_hex not in client.log_text())
        ck("B no other town dir for a different town key exists for the token (token is per town key)", len(glob.glob(os.path.join(mp, "characters", uuid, "towns", "*", "token.dat"))) == 1)

        # ---------------- E (part 1) + impostor while the real resident is connected
        out = say(host, "residents")
        mo = re.search(RES_PAT, out)
        ck("E `residents`: slot 1 credential=yes confirmed=no(not yet re-presented) connected=yes, no token text", mo is not None and mo.group(1) == "yes" and mo.group(4) == "yes" and tok_hex not in out)
        key2, td2, ok2 = prepare_resident(uuid2, "b", rpid)
        ck("E setup: second character (copy of the town, same resident PersonalID, NO token) prepared", ok2 and key2 == key)
        if ok2:
            run_refused(uuid2, "imp_tofu", host, "resident 1 has a credential and the claim presented NO token", "E-impostor(tofu, live resident connected)")
            ck("E-impostor: the real resident was NOT evicted (still connected) and nothing was minted", client.proc.poll() is None and "credential MINTED" not in host.log_text()[len(ht):] and len(members_entries()) == 1)
            ck("E-impostor: the second character has no token.dat", not os.path.exists(os.path.join(td2, "token.dat")))

        # ---------------- C: abrupt host kill
        coff = len(client.log_text())
        host.proc.kill()
        host.proc.wait()
        host.stop()
        mo = client.wait_for_log(r"\[NET\]\[RECONNECT\] client: host link lost", 60.0, since_offset=coff)
        ck("C the real client noticed the dead host and started its reconnect backoff", mo is not None and client.proc.poll() is None)
        ck("C members.dat survived the kill: still one entry, same token", len(members_entries()) == 1 and members_entries()[0][3] == tok)

        # ---------------- D: host restart on the same port/save/members.dat; auto reconnect
        host = start_host(port, "tofu", "tofu2")
        ck("D host restarted (same port, save, members.dat)", host is not None)
        if host is None:
            return
        mo = client.wait_for_log(r"reconnect successful .*identity re-established \(resident: resident 1", 170.0, since_offset=coff)
        time.sleep(2.0)
        ht2 = host.log_text()
        ck("D client logged 'identity re-established ... resident 1' (auto reconnect, same process)", mo is not None and client.proc.poll() is None)
        ck("D host restored exactly 1 credential and logged KNOWN (verified), not MINTED", "1 resident credential(s) restored" in ht2 and "resident 1 credential verified (token sent: KNOWN)" in ht2 and "MINTED" not in ht2)
        ck("D members.dat entry count unchanged (1), token unchanged, now confirmed", len(members_entries()) == 1 and members_entries()[0][3] == tok and members_entries()[0][1] == 1)
        ck("D client did not rewrite or lose token.dat (same token) and logged 'resident token verified'", gtk_token(tpath) == (1, tok) and "resident token verified by the host (resident 1)" in client.log_text()[coff:])
        # ---------------- E (part 2)
        out = say(host, "residents")
        mo = re.search(RES_PAT, out)
        ck("E `residents` after the reconnect: slot 1 credential=yes confirmed=yes armed=no connected=yes; no token text", mo is not None and mo.groups() == ("yes", "yes", "no", "yes") and tok_hex not in out)
        ck("E no token text anywhere in the host logs or the client log", all(tok_hex not in open(p, errors="replace").read() for p in glob.glob(T.log_path("rcred_*.log"))))

        client.stop()
        client = None
        time.sleep(9.0)  # host drops the dead peer

        # ---------------- F: wrong (different, valid-format) token
        good = open(tpath, "rb").read()
        bad_tok = bytes((b ^ 0x5A) for b in tok)
        gtk_set_token(tpath, bad_tok)
        mt = os.stat(tpath).st_mtime_ns
        wrong_bytes = open(tpath, "rb").read()
        ct, ht = run_refused(uuid, "wrong", host, "resident 1 has a credential and the claim presented a WRONG token", "F wrong token")
        ck("F the client presented a token ('token held'), no bind: token.dat untouched, membership unchanged, host entry unchanged",
           "sent IDENTITY_EXT (token held)" in ct and open(tpath, "rb").read() == wrong_bytes and members_entries()[0][3] == tok and "role = resident" in open(os.path.join(td, "membership.ini")).read())
        ck("F the refused client also logged the wrong-token hint ('presented a token ... delete the stored resident token')", "delete the stored resident token" in ct)
        ck("F no token text printed (host / client / residents)", tok_hex not in ht and bad_tok.hex() not in ht and tok_hex not in ct and bad_tok.hex() not in ct)
        time.sleep(7.0)
        out = say(host, "residents")
        mo = re.search(RES_PAT, out)
        ck("F `residents` after the refusal: credential=yes confirmed=yes connected=no", mo is not None and mo.group(1) == "yes" and mo.group(2) == "yes" and mo.group(4) == "no")

        # ---------------- G: missing token.dat
        os.replace(tpath, tpath + ".saved_good")  # (rename, not delete)
        ct, ht = run_refused(uuid, "notoken", host, "resident 1 has a credential and the claim presented NO token", "G missing token")
        ck("G the client sent the RESIDENT claim without a token ('not held (first claim)') and stored nothing", "sent IDENTITY_EXT (token not held (first claim))" in ct and not os.path.exists(tpath))
        time.sleep(7.0)
        out = say(host, "resident-reset 1 confirm", timeout=40.0)
        ck("G `resident-reset 1 confirm`: credential RESET (backup written)", "credential RESET by the operator" in out and len(members_entries()) == 0 and len(glob.glob(MEMBERS + ".bak-*")) >= 1)
        client = new_client(uuid, "reminted")
        mo = client.wait_for_log(r"-> READY", 170.0)
        time.sleep(2.0)
        ct, ht = client.log_text(), host.log_text()
        n, tok2 = gtk_token(tpath) if os.path.isfile(tpath) else (0, None)
        ck("G after resident-reset the same client claims again: READY, host MINTED (NEW), token.dat re-created with a DIFFERENT token",
           mo is not None and ht.count("credential MINTED") == 1 and n == 1 and tok2 is not None and tok2 != tok and len(members_entries()) == 1 and members_entries()[0][3] == tok2)
        ck("G no token text printed", tok2 is not None and tok2.hex() not in ht and tok2.hex() not in ct)
        client.stop()
        client = None
        time.sleep(9.0)

        # ---------------- H: required
        stop_host(host)
        host = None
        parked = os.path.join(HOST_DIR, "save", "mp", "parked_members")
        os.makedirs(parked, exist_ok=True)
        for f in glob.glob(os.path.join(HOST_DIR, "save", "mp", "members.dat*")):
            if os.path.isfile(f):
                os.replace(f, os.path.join(parked, os.path.basename(f)))  # move every generation aside (a missing members.dat with a .bak1 would be restored)
        os.replace(tpath, tpath + ".saved_minted2")
        host = start_host(port, "required", "req")
        ck("H host (resident-tokens required, fresh members.dat) booted", host is not None)
        if host is None:
            return
        ck("H fresh store: no credentials", members_entries() == [])
        out = say(host, "residents")
        mo = re.search(RES_PAT, out)
        ck("H `residents` under required: slot 1 credential=no armed=no", mo is not None and mo.group(1) == "no" and mo.group(3) == "no")
        run_refused(uuid, "req_unarmed", host, "resident_tokens=required and resident 1 has no credential and is not armed", "H unarmed")
        ck("H nothing minted while unarmed", members_entries() == [])
        time.sleep(7.0)
        out = say(host, "resident-arm 1 confirm", timeout=40.0)
        ck("H `resident-arm 1 confirm`: ARMED (one mint, memory only)", "ARMED for" in out)
        client = new_client(uuid, "req_armed")
        mo = client.wait_for_log(r"-> READY", 170.0)
        time.sleep(2.0)
        ct, ht = client.log_text(), host.log_text()
        n, tok3 = gtk_token(tpath) if os.path.isfile(tpath) else (0, None)
        ck("H after the arm exactly one mint succeeded: READY, token.dat stored, one members.dat entry", mo is not None and ht.count("credential MINTED") == 1 and n == 1 and len(members_entries()) == 1
           and members_entries()[0][3] == tok3)
        out = say(host, "residents")
        mo = re.search(RES_PAT, out)
        ck("H `residents`: credential=yes, armed=no (the arm was consumed), connected=yes", mo is not None and mo.group(1) == "yes" and mo.group(3) == "no" and mo.group(4) == "yes")
        # second client process presenting no token (the copied-town impostor), real resident still connected
        if ok2:
            run_refused(uuid2, "imp_req", host, "resident 1 has a credential and the claim presented NO token", "H second client without a token (required, resident connected)")
        ck("H the real resident stayed connected and no second mint happened", client.proc.poll() is None and host.log_text().count("credential MINTED") == 1 and len(members_entries()) == 1)
        client.stop()
        client = None
        time.sleep(9.0)
        # same resident, token missing again, arm used up: refused (credential exists)
        os.replace(tpath, tpath + ".saved_minted3")
        run_refused(uuid, "req_notoken", host, "resident 1 has a credential and the claim presented NO token", "H same character without a token after the arm was consumed")
        ck("H still exactly one credential", len(members_entries()) == 1 and host.log_text().count("credential MINTED") == 1)
    finally:
        for p in (client, client2):
            if p is not None:
                p.stop()
        if host is not None:
            stop_host(host)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11960)
    args = ap.parse_args()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: disposable pc\\build64\\bin_fixture4_<name> copies only", file=sys.stderr)
        return 2
    results = []
    run(args.port, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
