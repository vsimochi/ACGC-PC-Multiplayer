#!/usr/bin/env python3
"""test_guest_lifecycle_real.py - guest -> resident lifecycle hardening, S1: a REAL dedicated host + REAL store-character clients, the Nook dialogue driven by synthetic key events.

TIER: real processes on DISPOSABLE copies of pc\\build64\\bin_fixture4: host bin_fixture4_lcr (resident slots 2 AND 3 / houses 2 AND 3 free), clients bin_fixture4_lcrc (character A) and
bin_fixture4_lcrd (character B), each with an EMPTY save dir. The live save dir, bin_talkfix*, bin_fixture4 and the other fixtures are never used. Display pinned with
AC_DISPLAY_NAME=samsung (exit code 4 = no unique match: the test stops), AC_MASTER_VOLUME=1 (1 %), AC_TEST_HOOKS=1. Screenshots capture ONLY the client rectangle of the game window
(game_input.capture_window_png) into %TEMP%\\acmp_town_logs\\lcr_*.png. Keys are SendInput scancodes, sent only while the game window is the foreground window; the dialogue is synchronised on
the log line `[CHOICE][TEST-ONLY] choice window is NORMAL` (printed by the game in test builds when a choice window opens).

PART A (character A, `--nook-test warp`): the first talk -> the intro offer -> No. -> membership.ini nook_intro = declined; the client quits; relaunch (`--nook-test wallet=20000,warp`): no
  re-prompt (the normal top menu), `Other things` has the extra `Buy a house.` row (screenshot), buy: pick a house, confirm, APPLIED -> the thanks row (the rejoin hold is released by the
  heartbeat: the in-process connect starts within 20 s of APPLIED, not ~40 s) -> in-process rejoin -> READY as the resident; token.dat holds guest + resident entries until the first KNOWN
  login, then only the resident; host: guests.dat has no entry of the guest, members.dat no handoff; the host is restarted -> the resident reconnects automatically and is KNOWN.
PART B (character B, `--house-buy-test auto` + env AC_PROMOTE_CLIENT_CRASH=1): the client DIES between token.dat and membership.ini (exit code 96) = killed after APPLIED and before the rejoin:
  token.dat holds both entries, membership.ini still says guest; relaunch (Play Online arguments) -> the guest token is still there -> RESIDENT_HANDOFF again -> role = resident -> READY ->
  KNOWN -> only the resident entry.
Usage: python test_guest_lifecycle_real.py [--port 11990]
"""
import argparse
import os
import re
import struct
import subprocess
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402
import make_four_resident_fixture as MF  # noqa: E402

HOST_NAME = "bin_fixture4_lcr"
CA_NAME = "bin_fixture4_lcrc"
CB_NAME = "bin_fixture4_lcrd"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, HOST_NAME)
CA_DIR = os.path.join(T.BUILD64, CA_NAME)
CB_DIR = os.path.join(T.BUILD64, CB_NAME)


def make_free_slots(dest, slots=(2, 3)):
    p = os.path.join(dest, MF.GCI_REL)
    g = bytearray(open(p, "rb").read())
    clear = b"\x20" * 16 + struct.pack(">HH", 0xFFFF, 0xFFFF)
    for slot in slots:
        po = MF.MAIN_OFF + MF.PRIV_OFF + slot * MF.PRIV_STRIDE
        g[po:po + MF.PRIV_STRIDE] = bytes(MF.PRIV_STRIDE)
        g[po:po + 20] = clear
        ho = MF.MAIN_OFF + MF.HOME_OFF + slot * MF.HOME_STRIDE
        g[ho:ho + 20] = clear
    m = bytearray(g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE])
    m[MF.CHK_OFF:MF.CHK_OFF + 2] = struct.pack(">H", MF.checksum(bytes(m)))
    g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_T_SIZE] = m
    g[MF.BACK_OFF:MF.BACK_OFF + MF.SAVE_SECTOR_SIZE] = g[MF.MAIN_OFF:MF.MAIN_OFF + MF.SAVE_SECTOR_SIZE]
    open(p, "wb").write(bytes(g))


if __name__ == "__main__":
    make_free_slots(T.make_fixture(HOST_NAME))
    T.make_fixture(CA_NAME, empty_save=True)
    T.make_fixture(CB_NAME, empty_save=True)

import net_spike_lib as L  # noqa: E402
import game_input as G  # noqa: E402

ENV = {"AC_TEST_HOOKS": "1", "AC_TOWN_NO_MSGBOX": "1", "AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1"}
MARK = "[CHOICE][TEST-ONLY] choice window is NORMAL"
MBR_SIZE = 32 + 16 * 80 + 4
GST_ENTRY = 72 + 0x2440
GST_SIZE = 32 + 8 * GST_ENTRY + 4


def mpath(n):
    return os.path.join(L.GAME_BIN_DIR, "save", "mp", n)


def parse_members(path):
    b = open(path, "rb").read()
    assert len(b) == MBR_SIZE and b[:8] == b"ACMPMBR\x00"
    out = []
    for i in range(16):
        o = 32 + i * 80
        if b[o]:
            out.append(dict(slot=b[o + 2], kind=b[o + 3], pid=bytes(b[o + 20:o + 40]), confirmed=b[o + 1]))
    return out


def parse_guest_pids(path):
    if not os.path.isfile(path):
        return []
    b = open(path, "rb").read()
    return [bytes(b[32 + i * GST_ENTRY + 4:32 + i * GST_ENTRY + 24]) for i in range(8) if b[32 + i * GST_ENTRY]]


def gtk_entries(path):
    """[(home_pid, token)] of the PRESENT entries of a PCMpGtk token file."""
    if not os.path.isfile(path):
        return []
    b = open(path, "rb").read()
    return [(b[32 + i * 64 + 28:32 + i * 64 + 48], b[32 + i * 64 + 48:32 + i * 64 + 64]) for i in range(4) if b[32 + i * 64] == 1]


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


def wait_count(client, pattern, n, timeout):
    rx = re.compile(pattern)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if len(rx.findall(client.log_text())) >= n:
            return True
        if client.proc is not None and client.proc.poll() is not None:
            return len(rx.findall(client.log_text())) >= n
        time.sleep(0.4)
    return False


class Screen:
    """Key + screenshot driver of ONE client process (its game window only)."""

    def __init__(self, client, tag):
        self.client, self.tag, self.n = client, tag, 0
        self.gw = G.GameWindow(client.proc.pid)
        self.shots = []

    def fg(self):
        if not self.gw.ready():
            return False
        G.focus(self.gw.hwnd)
        return G.user32.GetForegroundWindow() == self.gw.hwnd

    def key(self, k, hold=0.12, after=0.3):
        if not self.fg():
            raise RuntimeError("the game window is not the foreground window: refusing to send keys")
        self.gw.press(k, hold=hold, after=after)

    def shot(self, name):
        if not self.fg():
            raise RuntimeError("the game window is not the foreground window: refusing to capture")
        self.n += 1
        path = T.log_path("lcr_%s_%02d_%s.png" % (self.tag, self.n, name))
        G.capture_window_png(self.gw.hwnd, path, scale=2)
        self.shots.append(path)
        L.info("screenshot: " + path)
        return path

    def markers(self):
        return self.client.log_text().count(MARK)

    def advance_until(self, n_markers, timeout, approach=False, period=1.5, shot_every=0):
        """Presses A (and, while approaching Nook, walks UP a little) until the log shows `n_markers` open choice windows. Never presses after the marker appeared."""
        end = time.monotonic() + timeout
        i = 0
        while time.monotonic() < end:
            if self.markers() >= n_markers:
                return True
            if self.client.proc.poll() is not None:
                return False
            if approach and i % 4 == 2:
                self.key("UP", hold=0.45, after=0.3)
            if self.markers() >= n_markers:
                return True
            self.key("A", after=0.2)
            time.sleep(period)
            i += 1
            if shot_every and i % shot_every == 0 and self.markers() < n_markers:
                self.shot("step")
        return self.markers() >= n_markers


def start_client(port, cdir, args, tag, env_extra=None):
    env = dict(ENV)
    env.update(env_extra or {})
    return L.ClientProcess("127.0.0.1:%d" % port, extra_args=args, log_path=T.log_path("lcr_%s_client.log" % tag), bin_dir=cdir, env=env, label="lcr" + tag).start()


def import_character(cdir, port, ini_name, name, pid, land_id):
    mp = os.path.join(cdir, "save", "mp")
    os.makedirs(mp, exist_ok=True)
    with open(os.path.join(mp, "guest_%s.ini" % ini_name), "w", newline="") as f:
        f.write("name = %s\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x%04X\nland_id = 0x%04X\n" % (name, pid, land_id))
    with open(os.path.join(mp, "servers.ini"), "w", newline="") as f:
        f.write("[server]\nname = lcrsrv\naddress = 127.0.0.1\nport = %d\n" % port)
    r = subprocess.run([os.path.join(cdir, "AnimalCrossing.exe"), "--character-import-profile", ini_name], cwd=cdir, capture_output=True, text=True, timeout=60, env=dict(os.environ, **ENV))
    m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
    return m.group(1) if r.returncode == 0 and m else None


def town_dir(cdir, uuid):
    td = os.path.join(cdir, "save", "mp", "characters", uuid, "towns")
    ds = [os.path.join(td, d) for d in os.listdir(td)] if os.path.isdir(td) else []
    return ds[0] if len(ds) == 1 else None


def membership_text(cdir, uuid):
    td = town_dir(cdir, uuid)
    p = os.path.join(td, "membership.ini") if td else ""
    return open(p).read() if p and os.path.isfile(p) else ""


def start_host(port, tag):
    h = L.HostProcess(port=port, extra_args=["--dedicated", "--town-serve", "on", "--resident-tokens", "tofu"], log_path=T.log_path("lcr_%s_host.log" % tag), bin_dir=L.GAME_BIN_DIR,
                      stdin_pipe=True, verbose=False, new_group=True).start()
    ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
    return h, ok


def part_a(port, results, st):
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = client = None
    try:
        uuid = import_character(CA_DIR, port, "buyer", "Buyer", 0x1234, 0x4321)
        ck("A setup: the legacy profile was imported as a store character", uuid is not None)
        if uuid is None:
            return
        host, ok = start_host(port, "a")
        ck("A host (dedicated, town-serve on, resident-tokens tofu; slots 2 + 3 free) booted", ok)
        if not ok:
            return
        base_args = ["--character", uuid, "--town-fetch", "--online-ui"]

        # ---------------- A1: first talk -> intro -> No
        client = start_client(port, CA_DIR, base_args + ["--nook-test", "warp"], "a1")
        mo = client.wait_for_log(r"-> READY", 220.0)
        if client.proc.poll() is not None:
            ck("A1 the client process is alive (exit code %s: 4 = AC_DISPLAY_NAME matched no unique display -> STOP)" % client.proc.returncode, False)
            return
        ck("A1 the client fetched the town and reached READY as a guest (wallet 0)", mo is not None and "arriving as a guest" in client.log_text())
        scr = Screen(client, "a1")
        ck("A1 the nook-test hook warped the guest into Nook's shop", client.wait_for_log(r"--nook-test: warping the guest into the shop", 90.0) is not None)
        time.sleep(8.0)
        scr.shot("shop_after_warp")
        got = scr.advance_until(1, 90.0, approach=True, shot_every=3)
        scr.shot("intro_choice")
        ck("A1 Nook's intro offer reached its Yes. / No. choice window", got)
        time.sleep(1.2)
        scr.key("Left Shift", after=0.5)  # B = the last choice = No.
        ck("A1 No.: log 'the guest declined the first offer'", client.wait_for_log(r"nook dialogue: the guest declined the first offer", 30.0) is not None)
        time.sleep(2.0)
        scr.shot("decline_row")
        m0 = scr.markers()
        for _ in range(4):  # read the closing pages; stop at once if a NEW choice window opens (a second conversation)
            if scr.markers() > m0:
                scr.key("Left Shift", after=0.5)
                break
            scr.key("A", after=0.2)
            time.sleep(1.8)
        scr.shot("after_decline")
        time.sleep(3.0)
        ms = membership_text(CA_DIR, uuid)
        ck("A1 membership.ini: role = guest and nook_intro = declined", "role = guest" in ms and "nook_intro = declined" in ms)
        ck("A1 no purchase happened (no HOUSE_PURCHASE in the client log, no resident created on the host)", "begin HOUSE_PURCHASE" not in client.log_text() and "BOUGHT" not in host.log_text())
        scr.gw.close()
        ck("A1 the client quit (WM_CLOSE)", client.wait_exit(40.0) is not None)
        client.stop()
        client = None
        time.sleep(9.0)

        # ---------------- A2: relaunch: no re-prompt, the extra row, buy
        client = start_client(port, CA_DIR, base_args + ["--nook-test", "wallet=20000,warp"], "a2")
        mo = client.wait_for_log(r"-> READY", 220.0)
        ck("A2 the relaunched client reached READY as a guest", mo is not None and client.proc.poll() is None)
        scr = Screen(client, "a2")
        ck("A2 the hook set the wallet (20000, uploaded) and warped into the shop", client.wait_for_log(r"--nook-test: setup done, wallet=20000", 90.0) is not None
           and client.wait_for_log(r"--nook-test: warping the guest into the shop", 90.0) is not None)
        time.sleep(8.0)
        got = scr.advance_until(1, 90.0, approach=True, shot_every=3)
        scr.shot("top_menu")
        ck("A2 the talk opens a choice window (the normal top menu: NO second intro offer)", got and "the guest declined the first offer" not in client.log_text())
        time.sleep(1.2)
        scr.key("S", after=0.4)
        scr.key("S", after=0.4)
        scr.key("A", after=0.5)  # Other things (third row)
        got = scr.advance_until(2, 45.0, period=1.5)
        time.sleep(1.5)
        scr.shot("other_things_menu")
        ck("A2 `Other things` opened its 5-row guest menu (extra row `Buy a house.`: see the screenshot)", got)
        for _ in range(3):
            scr.key("S", after=0.4)
        scr.key("A", after=0.5)
        ck("A2 'Buy a house.' chosen in the Other things menu (log)", client.wait_for_log(r"'Buy a house\.' chosen in the Other things menu", 30.0) is not None)
        got = scr.advance_until(3, 60.0, period=1.5)
        time.sleep(1.5)
        scr.shot("pick_house")
        ck("A2 the house choice window opened (price line first)", got)
        scr.key("A", after=0.5)  # the first listed house
        ck("A2 a house was chosen (log)", client.wait_for_log(r"nook dialogue: house (\d+) chosen", 30.0) is not None)
        got = scr.advance_until(4, 60.0, period=1.5)
        time.sleep(1.5)
        scr.shot("confirm")
        ck("A2 the confirmation window opened", got)
        scr.key("A", after=0.5)  # Yes.
        t_pay = time.monotonic()
        ck("A2 payment confirmed -> request sent (log)", client.wait_for_log(r"payment confirmed, requesting the purchase", 30.0) is not None)
        mo = client.wait_for_log(r"nook dialogue: purchase APPLIED -> congratulation row", 60.0)
        t_applied = time.monotonic()
        ck("A2 the purchase was APPLIED and the congratulation row was requested", mo is not None)
        if mo is not None:
            time.sleep(1.0)
            scr.shot("thanks_row")
            time.sleep(2.5)
            scr.key("A", after=0.3)
            scr.shot("thanks_row_2")
        mo = client.wait_for_log(r"play-online: in-process connect pid=(\d+)", 90.0)
        dt = time.monotonic() - t_applied
        ck("A2 heartbeat: the in-process rejoin started %.1f s after APPLIED (<= 20 s: released by the heartbeat / cap, not the old ~40 s hold)" % dt, mo is not None and dt <= 20.0)
        time.sleep(0.5)
        ctext = client.log_text()
        ck("A2 RESIDENT_HANDOFF honoured (token.dat + membership.ini written), same process rejoin", "token.dat + membership.ini written" in ctext and "re-joining IN-PROCESS as the resident" in ctext)
        td = town_dir(CA_DIR, uuid)
        ent = gtk_entries(os.path.join(td, "token.dat")) if td else []
        known_seen = "resident token verified by the host" in ctext
        ck("A2 token.dat holds the GUEST and the RESIDENT entry until the first KNOWN login (read %s the KNOWN line)" % ("AFTER" if known_seen else "before"), (len(ent) == 2 and not known_seen) or (known_seen and len(ent) in (1, 2)))
        ok2 = wait_count(client, r"-> READY", 2, 240.0)
        ck("A2 the SAME process reached READY a second time as the resident", ok2 and client.proc.poll() is None)
        time.sleep(3.0)
        scr.shot("resident_arrival")
        ctext = client.log_text()
        ck("A2 role = resident: bound by PersonalID, the RESIDENT claim went out", "is a RESIDENT of town" in ctext and "playing a resident -- sent IDENTITY_EXT" in ctext and "resident token verified by the host" in ctext)
        time.sleep(2.0)
        ent = gtk_entries(os.path.join(td, "token.dat")) if td else []
        ck("A2 after the first KNOWN login token.dat holds ONLY the resident entry", len(ent) == 1 and "obsolete guest token" in client.log_text())
        ms = membership_text(CA_DIR, uuid)
        ck("A2 membership.ini: role = resident, nook_intro = bought", "role = resident" in ms and "nook_intro = bought" in ms)
        htext = host.log_text()
        mem = parse_members(mpath("members.dat"))
        ck("A2 host: 'BOUGHT a house: ... loan 0', the handoff removed (members.dat: the credential only, confirmed), guests.dat has no entry", "BOUGHT a house" in htext and "promotion handoff removed" in htext
           and len(mem) == 1 and mem[0]["kind"] == 1 and mem[0]["confirmed"] == 1 and parse_guest_pids(mpath("guests.dat")) == [])
        out = say(host, "residents")
        ck("A2 `residents`: the new resident has a confirmed credential and is connected", re.search(r'name="Buyer\s*" credential=yes confirmed=yes armed=no connected=yes', out) is not None)

        # ---------------- A3: host restart -> automatic reconnect, KNOWN
        host.send_line("stop")
        ck("A3 host stopped (exit 0)", host.wait_exit(60.0) == 0)
        host.stop()
        off = len(client.log_text())
        host, ok = start_host(port, "a2")
        ck("A3 host restarted on the same port / save / members.dat", ok)
        mo = client.wait_for_log(r"reconnect successful \(attempt \d+, offline \d+ ms\) -- identity re-established \(resident: resident (\d)", 150.0, since_offset=off)
        time.sleep(3.0)
        ck("A3 the resident client reconnected by itself and was KNOWN", mo is not None and "credential verified (token sent: KNOWN)" in host.log_text())
        scr.shot("after_host_restart")
        st["a_ok"] = True
    finally:
        if client is not None:
            try:
                client.stop()
            except Exception:  # noqa: BLE001
                pass
        if host is not None:
            host.send_line("stop")
            host.wait_exit(60.0)
            host.stop()


def part_b(port, results):
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    host = client = None
    try:
        uuid = import_character(CB_DIR, port, "crasher", "Crasher", 0x2345, 0x4322)
        ck("B setup: the legacy profile was imported as a store character", uuid is not None)
        if uuid is None:
            return
        host, ok = start_host(port, "b")
        ck("B host booted (fresh town: slots 2 + 3 free again)", ok)
        if not ok:
            return
        base_args = ["--character", uuid, "--town-fetch", "--online-ui"]
        client = start_client(port, CB_DIR, base_args + ["--house-buy-test", "auto"], "b1", env_extra={"AC_PROMOTE_CLIENT_CRASH": "1"})
        mo = client.wait_for_log(r"-> READY", 220.0)
        ck("B1 the client reached READY as a guest", mo is not None and "arriving as a guest" in client.log_text())
        try:
            code = client.proc.wait(180.0)
        except subprocess.TimeoutExpired:
            code = None
        ctext = client.log_text()
        ck("B1 the client DIED between token.dat and membership.ini (exit code 96, 'client fault: exiting NOW')", code == 96 and "client fault: exiting NOW" in ctext)
        td = town_dir(CB_DIR, uuid)
        ent = gtk_entries(os.path.join(td, "token.dat")) if td else []
        ms = membership_text(CB_DIR, uuid)
        ck("B1 token.dat holds BOTH entries (guest kept + resident), membership.ini still says role = guest", len(ent) == 2 and "role = guest" in ms and "role = resident" not in ms)
        ck("B1 host: BOUGHT a house, the handoff was sent", "BOUGHT a house" in host.log_text() and "RESIDENT_HANDOFF sent" in host.log_text())
        client.stop()
        client = None
        time.sleep(9.0)
        # relaunch with the Play Online arguments (no hook): the guest token is still there
        client = start_client(port, CB_DIR, base_args, "b2")
        mo = client.wait_for_log(r"play-online: in-process connect pid=(\d+)", 240.0)
        ctext = client.log_text()
        ck("B2 relaunch: the claim with the kept GUEST token got the handoff again (the host log says so), the files were written, and the process rejoined IN-PROCESS",
           "the guest was PROMOTED to resident" in host.log_text() and "token.dat + membership.ini written" in ctext and mo is not None)
        ok2 = wait_count(client, r"-> READY", 1, 240.0)  # the relaunch never becomes a READY guest: the guest claim is answered by the handoff + REJECT 6
        time.sleep(4.0)
        ctext = client.log_text()
        ck("B2 READY as the resident, KNOWN (resident token verified), role = resident", ok2 and "resident token verified by the host" in ctext and "role = resident" in membership_text(CB_DIR, uuid))
        td = town_dir(CB_DIR, uuid)
        ent = gtk_entries(os.path.join(td, "token.dat")) if td else []
        ck("B2 after the first KNOWN login only the resident entry remains", len(ent) == 1)
        mem = parse_members(mpath("members.dat"))
        ck("B2 host: members.dat = the two residents' credentials (A's from part A is gone with the fresh fixture: one), no handoff left", all(e["kind"] == 1 for e in mem) and len(mem) == 1)
    finally:
        if client is not None:
            try:
                client.stop()
            except Exception:  # noqa: BLE001
                pass
        if host is not None:
            host.send_line("stop")
            host.wait_exit(60.0)
            host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11990)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4_"):
        print("REFUSING: disposable pc\\build64\\bin_fixture4_<name> copies only", file=sys.stderr)
        return 2
    results = []
    st = {}
    try:
        part_a(args.port, results, st)
        # PART B runs on a FRESH host town (the fixtures are re-made so that two houses are free again)
        make_free_slots(T.make_fixture(HOST_NAME))
        part_b(args.port + 1, results)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        L.check("test aborted by an exception: %s" % e, False, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
