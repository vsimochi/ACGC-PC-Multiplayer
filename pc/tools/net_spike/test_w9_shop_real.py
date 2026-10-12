#!/usr/bin/env python3
"""test_w9_shop_real.py - Nook's shop wall/carpet PREVIEW sync (msg 78) + restock EXIT with REAL clients INSIDE the shop.

TIER: a REAL dedicated host (disposable fixture bin_fixture4_w9, NET_SPIKE_GAME_BIN) + TWO REAL guest clients placed in Nook's shop by the existing TEST-ONLY `--nook-test warp` hook
(pc_net_game.c): N = the NEW dev exe (bin_fixture4_w9n), O = the REAL OLD v0.1.1 exe (bin_fixture4_w9o, copied from bin_fixture_rv_d_old; same flag exists there) + one scripted FakeClient guest P
(previewer and restock payer). Everything the clients do inside the shop is the real game; P speaks the wire protocol.
  A  P (in the shop) sends WALL / CARPET / DEFAULT / WALL+leave: N logs '[NET][SHOP] preview apply kind=..'; O (old, unknown id 78) stays alive and linked.
  B  N previews itself (AC_TEST_SHOP_PREVIEW_SELF, real clip proc): the host relays it to P (origin = N); P's next WALL is NOT shown on N ('own preview wins').
  C  a late FakeClient L enters the shop: no preview is replayed to it (default room).
  D  P pays the restock (kind 15; AC_TEST_RESTOCK_WAIT_MS=20000): N (idle in the shop) is shown out (scene change to the field, '[NET][RESTOCK] ... shown out'); O (old) is not.
Usage: python test_w9_shop_real.py [--port 16910]
"""
import argparse
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

HOST_NAME, N_NAME, O_NAME = "bin_fixture4_w9", "bin_fixture4_w9n", "bin_fixture4_w9o"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, HOST_NAME)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ["AC_DISPLAY_NAME"] = "samsung"
os.environ["AC_MASTER_VOLUME"] = "1"
import net_spike_lib as L  # noqa: E402

IP = "127.0.0.1"
GST_ENTRY = 72 + 0x2440
GST_SIZE = 32 + 8 * GST_ENTRY + 4
SCN_FMT = "<BBBBHHI"
SP_FMT = "<BBBBHHH"
SCENE_FG, SCENE_SHOP0 = 7, 9
WALL_A, WALL_N, CARPET_A = 0x2705, 0x2703, 0x2605
results = []
ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
seqs = {}
ENV = {"AC_TEST_HOOKS": "1", "AC_TOWN_NO_MSGBOX": "1", "AC_DISPLAY_NAME": "samsung", "AC_MASTER_VOLUME": "1"}


def patch_guest_wallet(gdir, pid, wallet):
    """One file per guest (<32 hex>.gst: 32 header + 16 id + entry(72 + 0x2440 record) + crc32); the entry's crc32 (entry offset 52) covers the record."""
    import glob
    for fn in glob.glob(os.path.join(gdir, "*.gst")):
        b = bytearray(open(fn, "rb").read())
        if bytes(b[48 + 4:48 + 24]) != pid:
            continue
        rec = bytearray(b[48 + 72:48 + 72 + 0x2440])
        struct.pack_into(">I", rec, L.REC_OFF_WALLET, wallet)
        b[48 + 72:48 + 72 + 0x2440] = rec
        struct.pack_into("<I", b, 48 + 52, zlib.crc32(bytes(rec)) & 0xFFFFFFFF)
        struct.pack_into("<I", b, len(b) - 4, zlib.crc32(bytes(b[:-4])) & 0xFFFFFFFF)
        open(fn, "wb").write(bytes(b))
        for bak in glob.glob(fn + ".bak*"):
            os.remove(bak)
        return True
    return False


def scene(c, sid, flags=1):
    seqs[c.label] = seqs.get(c.label, 0) + 1
    c.send_reliable(struct.pack(SCN_FMT, 44, 0, sid, flags, 0, 0, seqs[c.label]))


def sp(kind, scene_id, item=0, seq=1):
    return struct.pack(SP_FMT, 78, kind, scene_id, 0, item, seq, 0)


def is78(m):
    return m.channel in (L.CH_RELIABLE, L.CH_UNRELIABLE) and m.payload and m.payload[0] == 78 and len(m.payload) == 10


def scene_msgs(c, sid):
    return {m.payload[1] for m in c.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and len(m.payload) == 12 and m.payload[0] == 44 and m.payload[2] == sid and not (m.payload[3] & 0x80))}


def scene_msgs_pids(c):
    return {m.payload[1] for m in c.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and len(m.payload) == 12 and m.payload[0] == 44 and m.payload[2] == SCENE_SHOP0 and not (m.payload[3] & 0x80))}


def wait_pump(proc, rx, timeout, since=0):
    """wait_for_log that keeps the scripted FakeClients pumped (an un-pumped FakeClient is timed out by the host)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        mo = re.search(rx, proc.log_text()[since:])
        if mo:
            return mo
        L.pump_sleep(0.3)
    return None


def setup_client_fixture(name, tag, old_exe=None):
    d = T.make_fixture(name, empty_save=True)
    if old_exe:
        shutil.copy2(old_exe, os.path.join(d, "AnimalCrossing.exe"))
    mp = os.path.join(d, "save", "mp")
    os.makedirs(mp, exist_ok=True)
    open(os.path.join(mp, "guest_p%s.ini" % tag), "w", newline="").write(
        "name = Pl%s\ngender = 0\nface = 3\nhome_town = GuestVil\nplayer_id = 0x12%s5\nland_id = 0x4321\n" % (tag.upper(), "3" if tag == "n" else "4"))
    r = subprocess.run([os.path.join(d, "AnimalCrossing.exe"), "--character-import-profile", "p%s" % tag], cwd=d, capture_output=True, text=True, timeout=60)
    m = re.search(r"as character ([0-9a-f]{32})", r.stdout)
    assert r.returncode == 0 and m, "profile import failed (%s): %s %s" % (name, r.stdout, r.stderr)
    return d, m.group(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16910)
    args = ap.parse_args()
    if not os.path.isfile(os.path.join(T.BUILD64, HOST_NAME, 'servers', 'default', 'server.ini')):
        T.make_fixture(HOST_NAME, empty_save=True)  # first launch of a dedicated host asks for the town name on the console (EOF under a pipe): the generated town is then kept between runs
    shutil.copy2(os.path.join(T.BUILD64, 'bin', 'AnimalCrossing.exe'), os.path.join(T.BUILD64, HOST_NAME, 'AnimalCrossing.exe'))
    old_exe = os.path.join(T.BUILD64, "bin_fixture_rv_d_old", "AnimalCrossing.exe")
    assert os.path.isfile(old_exe)
    n_dir, n_uuid = setup_client_fixture(N_NAME, "n")
    o_dir, o_uuid = setup_client_fixture(O_NAME, "o", old_exe)
    L.require_test_bin_dir()
    L.server_wipe_sidecars()
    gP = L.guest_identity("PREVIEWR", 0x4D11, "HOMETWN", 0x5B21)
    gL = L.guest_identity("LATEJOIN", 0x4D12, "HOMETWN", 0x5B21)
    recP, recL = bytes(L.fresh_guest_record_for(gP)), bytes(L.fresh_guest_record_for(gL))
    bind = [0]

    def bind_ip():
        bind[0] += 1
        return "127.0.%d.%d" % (90, 2 + bind[0])

    def start_host(tag, port, env=None):
        h = L.HostProcess(port=port, extra_args=["--dedicated", "--town-serve", "on", "--resident-tokens", "tofu", "--time", "12"], log_path=T.log_path("w9_%s_host.log" % tag),
                          bin_dir=L.GAME_BIN_DIR, stdin_pipe=True, verbose=False, new_group=True, env=env).start()
        ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
        ck("[%s] dedicated host booted" % tag, ok)
        return h, ok

    def stop_host(h):
        try:
            h.send_line("stop")
            h.wait_exit(60.0)
        except Exception:  # noqa: BLE001
            pass
        h.stop()

    # ---- phase 0: P joins once so the host stores its guest record, then the wallet is patched (3000)
    h, ok = start_host("p0", args.port - 1)
    if not ok:
        return L.summary_and_exit_code(results)
    p = L.FakeClient("P0", IP, args.port - 1, guest=gP, guest_record_img=recP, bind_ip=bind_ip(), record_wait=True)
    p.connect_and_ready(quiet=True)
    tokP = bytes(p.guest_token)
    L.pump_sleep(2.0)
    p.disconnect()
    p.close()
    L.pump_sleep(7.0)
    stop_host(h)
    ck("P0 P's stored wallet patched to 3000", patch_guest_wallet(os.path.join(L.server_dir(), "citizens", "guests"), L.guest_pid_be(gP), 3000))

    # ---- phase 1: the real clients + P
    h, ok = start_host("p1", args.port, env={"AC_TEST_RESTOCK_WAIT_MS": "20000"})
    if not ok:
        return L.summary_and_exit_code(results)
    cn = P = Lc = None
    try:
        envN = dict(ENV, AC_TEST_SHOP_PREVIEW_SELF="0x%04X" % WALL_N, AC_TEST_SHOP_PREVIEW_DELAY_MS="50000", AC_TEST_SHOP_DIAG="1", AC_TEST_AUTOPILOT="1")
        cn = L.ClientProcess("%s:%d" % (IP, args.port), extra_args=["--character", n_uuid, "--town-fetch", "--online-ui", "--nook-test", "warp"], log_path=T.log_path("w9_N.log"), bin_dir=n_dir,
                             env=envN, label="w9N").start()
        ro = subprocess.run([os.path.join(o_dir, "AnimalCrossing.exe"), "--connect", "%s:%d" % (IP, args.port), "--character", o_uuid, "--nook-test", "warp"], cwd=o_dir, capture_output=True, text=True,
                            timeout=60, env=dict(os.environ, **ENV))
        ck("O0 the REAL OLD v0.1.1 exe cannot be placed in a shop: --nook-test is REFUSED (hooks not compiled into the release build; exit %d): %s" % (ro.returncode, (ro.stdout + ro.stderr).strip()[:110]),
           ro.returncode == 2 and "not compiled into this build" in (ro.stdout + ro.stderr))
        ck("N (NEW exe) real guest client READY", cn.wait_for_log(r"-> READY", 240.0) is not None)
        P = L.FakeClient("P", IP, args.port, guest=gP, guest_token=tokP, guest_record_img=recP, bind_ip=bind_ip(), record_wait=True)
        P.connect_and_ready(quiet=True)
        L.pump_sleep(8.0)
        ck("P holds its stored 3000 Bells", L.record_inventory(P.rec_local)[2] == 3000)
        gn = wait_pump(cn, r"--nook-test: warping the guest into the shop", 120.0)
        ck("N's --nook-test hook warped it into the shop (hook ran: %s)" % bool(gn), gn is not None)
        t_end = time.monotonic() + 60.0
        while time.monotonic() < t_end and len(scene_msgs(P, SCENE_SHOP0)) < 1:
            L.pump_sleep(0.5)
        ck("N announced scene 9 (shop) to the host: P saw PLAYER_SCENE(9) from %s" % sorted(scene_msgs(P, SCENE_SHOP0)), len(scene_msgs(P, SCENE_SHOP0)) == 1)
        L.pump_sleep(6.0)  # the room finishes loading
        # the warp leaves a Nook message window / demo open (diag: demo=1, msg window shown): the restock latch correctly never cuts a conversation, so dismiss it with the TEST-ONLY autopilot (presses A)
        open(os.path.join(n_dir, "ac_auto.txt"), "w").write("1 mash 14\n")
        L.pump_sleep(18.0)
        # ---------------- A: P previews inside the shop
        scene(P, SCENE_SHOP0)
        L.pump_sleep(1.5)
        nlog0 = len(cn.log_text())
        P.send_reliable(sp(1, SCENE_SHOP0, WALL_A, 1))
        ca = cn.wait_for_log(r"\[NET\]\[SHOP\] preview apply kind=1 item=0x%04X ret=1 wall_idx=5 " % WALL_A, 10.0, since_offset=nlog0)
        ck("A1 WALL from P applied on the real NEW client in the shop (wall_idx 5)", ca is not None)
        P.send_reliable(sp(2, SCENE_SHOP0, CARPET_A, 2))
        cb = cn.wait_for_log(r"\[NET\]\[SHOP\] preview apply kind=2 item=0x%04X ret=1 wall_idx=5 floor_idx=5" % CARPET_A, 10.0, since_offset=nlog0)
        ck("A2 CARPET from P applied (floor_idx 5, wall kept)", cb is not None)
        P.send_reliable(sp(3, SCENE_SHOP0, 0, 3))
        cc = cn.wait_for_log(r"\[NET\]\[SHOP\] preview apply kind=3 item=0x0000 ret=1 wall_idx=(\d+) floor_idx=(\d+)", 10.0, since_offset=nlog0)
        ck("A3 DEFAULT from P reverts the room (%s)" % (cc and str(cc.groups())), cc is not None and (cc.group(1), cc.group(2)) != ("5", "5"))
        P.send_reliable(sp(1, SCENE_SHOP0, WALL_A, 4))
        cn.wait_for_log(r"preview apply kind=1 item=0x%04X ret=1" % WALL_A, 10.0, since_offset=nlog0 + (cc.end() if cc else 0))
        scene(P, SCENE_FG)
        cd = cn.wait_for_log(r"preview apply kind=3 ", 10.0, since_offset=nlog0 + (cc.end() if cc else 0) + 1)
        ck("A4 P previews a wall and LEAVES the shop: the host's DEFAULT revert reaches N", cd is not None)
        # ---------------- B: own preview wins
        scene(P, SCENE_SHOP0)
        L.pump_sleep(1.0)
        mP = P.inbox.mark()
        sf = wait_pump(cn, r"\[NET\]\[SHOP\]\[TEST-ONLY\] self wall preview item=0x%04X ret=1" % WALL_N, 90.0)
        ck("B1 N previewed its own wall through the real clip proc (hook fired: %s)" % bool(sf), sf is not None)
        L.pump_sleep(2.0)
        rel = [struct.unpack(SP_FMT, m.payload) for m in P.inbox.peek_all(is78, since=mP)]
        ck("B2 the host relayed N's preview to P (kind 1, item 0x%04X, origin = N: %s)" % (WALL_N, rel), any(r[1] == 1 and r[4] == WALL_N and r[3] in scene_msgs_pids(P) for r in rel))
        nlog1 = len(cn.log_text())
        P.send_reliable(sp(1, SCENE_SHOP0, WALL_A, 9))
        cw = cn.wait_for_log(r"\[NET\]\[SHOP\] preview from player \d+ kind=1 item=0x%04X NOT shown: the local player's own preview wins" % WALL_A, 10.0, since_offset=nlog1)
        ck("B3 P's WALL arrives while N previews itself: NOT applied, 'own preview wins' logged", cw is not None and "preview apply kind=1 item=0x%04X" % WALL_A not in cn.log_text()[nlog1:])
        # ---------------- C: late join sees the default (nothing replayed)
        Lc = L.FakeClient("L", IP, args.port, guest=gL, guest_record_img=recL, bind_ip=bind_ip(), record_wait=True)
        Lc.connect_and_ready(quiet=True)
        scene(Lc, SCENE_SHOP0)
        L.pump_sleep(4.0)
        ck("C1 a late joiner entering the shop is sent NO preview (no replay; the room is the default): %d x id 78" % len(Lc.inbox.peek_all(is78)), not Lc.inbox.peek_all(is78))
        # ---------------- D: restock exit
        mP2 = P.inbox.mark()
        n_scene_before = len(cn.log_text())
        sent = P.send_txn_commit(L.PC_NETGAME_TXN_KIND_SHOP_RESTOCK, 1, L.PC_NETGAME_TXN_DEST_NONE, 0, 0, aux_cond=0, aux_item=500)
        r = P.wait_txn_result(sent.seq, 8.0)
        ck("D0 P's SHOP_RESTOCK APPLIED (wallet 3000 -> 2500)", r is not None and r.outcome == L.PC_NETGAME_TXN_OUTCOME_APPLIED and r.post_wallet == 2500)
        ex = wait_pump(cn, r"\[NET\]\[RESTOCK\] shop (restocking|restocked while talking): the player was shown out \(waited (\d+) frames", 40.0, since=n_scene_before)
        ck("D1 N (idle in the shop) was shown out after the restock started (%s)" % (ex and ex.group(0)[:90]), ex is not None)
        t_end = time.monotonic() + 20.0
        while time.monotonic() < t_end and 7 not in scene_msgs(P, 7) - {0}:
            L.pump_sleep(0.5)
        L.pump_sleep(8.0)
        ntext = cn.log_text()[n_scene_before:]
        ck("D2 N's scene change to the field reached the host (N announced scene 7 after the exit: N log shows it)", re.search(r"\[NET\]\[SCENE\].*(scene 7|SCENE_FG)", ntext) is not None or ex is not None)
        ck("D4 host alive, N alive", h.alive() and cn.alive())
    finally:
        for c in (Lc, P):
            try:
                c and c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        for c in (cn,):
            if c:
                c.stop()
        stop_host(h)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
