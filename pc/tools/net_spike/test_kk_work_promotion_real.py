#!/usr/bin/env python3
"""test_kk_work_promotion_real.py - guest -> resident PROMOTION keeps the character's Nook job and its K.K. claim, on REAL processes (a REAL dedicated host with a free resident slot +
REAL guest clients), disposable fixture pc\\build64\\bin_fixture4_kkpromo (recreated here; save snapshotted / restored).

  P1  guest ALPHA takes K.K.'s song (host-forced concert, TEST-ONLY AC_TEST_KK_CONCERT) and enters Work Mode; guest BETA enters Work Mode too (a bystander)
  P2  the host console promotes ALPHA to resident slot 3 (`promote Alpha auto auto confirm`)
  P3  work_jobs.dat: ALPHA's record is re-keyed to the NEW resident PersonalID (same job id, same state), the old guest key is gone, BETA's record is byte-identical, the count is unchanged
  P4  K.K.: the claim ALPHA had taken today moved to resident slot 3's vanilla bit (host log) - the new resident cannot take a second song at this concert
Usage: python test_kk_work_promotion_real.py [--port 12420]"""
import argparse
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

NAME = "bin_fixture4_kkpromo"
BIN = os.path.join(T.BUILD64, NAME)
if __name__ == "__main__":
    T.make_fixture(NAME)
os.environ["NET_SPIKE_GAME_BIN"] = BIN
import net_spike_lib as L  # noqa: E402
import test_kk_work_guests_work as W  # noqa: E402

PROFILES = {"alpha": ("Alpha", 0x4D31, 24130), "beta": ("Beta", 0x4D32, 24131)}


def free_slot3():
    import struct
    import make_four_resident_fixture as MF
    p = os.path.join(BIN, MF.GCI_REL)
    g = bytearray(open(p, "rb").read())
    clear = b"\x20" * 16 + struct.pack(">HH", 0xFFFF, 0xFFFF)
    slot = 3
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


def say(h, cmd, settle=2.5, timeout=40.0):
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


def guest(port, key, env, tag):
    return L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--guest", "--guest-profile", key, "-debug"], env=env, log_path=os.path.join(HERE, "kkpromo_%s_%s.log" % (key, tag)),
                           bin_dir=BIN, label="kkp" + key).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12420)
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    save_dir = os.path.join(BIN, "save")
    snap = os.path.join(os.environ.get("TEMP", "."), "kkpromo_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap)
    procs = []
    try:
        free_slot3()
        os.makedirs(os.path.join(save_dir, "mp"), exist_ok=True)
        for key, (nm, pid, lid) in PROFILES.items():
            open(os.path.join(save_dir, "mp", "guest_%s.ini" % key), "wb").write(
                ("# test profile\nname = %s\ngender = 1\nface = 5\nhome_town = Hometwn\nplayer_id = 0x%04X\nland_id = %d\n" % (nm, pid, lid)).encode("ascii"))
        wj = os.path.join(BIN, "servers", "default", "work_jobs.dat")  # a dedicated host keeps its files under servers/<id>
        if os.path.exists(wj):
            os.remove(wj)
        host = L.HostProcess(port=args.port, extra_args=["--dedicated", "--town-serve", "on", "--resident-tokens", "tofu", "--max-guests", "8", "-debug"],
                             env={"AC_TEST_KK_CONCERT": "1"}, log_path=os.path.join(HERE, "kkpromo_host.log"), bin_dir=BIN, stdin_pipe=True, verbose=False, new_group=True).start()
        procs.append(host)
        if host.wait_for_log(r"\[1\] Use this town", 60.0) is not None:  # first launch with a legacy town: answer the console question (use it)
            host.send_line("1")
        ok = host.wait_listening(60.0) and host.boot_to_dedicated(timeout=120.0)
        ck("dedicated host booted", ok)
        if not ok:
            return L.summary_and_exit_code(results)
        base = {"AC_TEST_DUMP_MSG": "0x1092", "AC_TEST_WORK_ENTER": "1", "AC_TEST_WORK_KEEP": "1"}
        a = guest(args.port, "alpha", dict(base, AC_TEST_EVNPC_CLAIM="2,5"), "p1")
        procs.append(a)
        res = a.wait_for_log(r"\[NET\]\[EVNPC\]\[TEST-ONLY\] client: claim op 2 (APPLIED|REJECTED) \(reason (\d+)", 170.0)
        ck("P1 guest ALPHA's K.K. claim is APPLIED", res is not None and res.group(1) == "APPLIED")
        ck("P1 ALPHA entered Work Mode (Nook offers 'Check my job')", W.first_page(a, "after-ENTER", 120.0) is True)
        b = guest(args.port, "beta", base, "p1")
        procs.append(b)
        ck("P1 BETA entered Work Mode too", W.first_page(b, "after-ENTER", 170.0) is True)
        f = W.parse(wj)
        before = W.by_key(f[2]) if f else {}
        ck("P1 two work records (one per guest)", len(before) == 2)
        time.sleep(3.0)
        a.stop()
        b.stop()
        time.sleep(10.0)  # the host drops the dead peers
        out = say(host, "guests")
        ck("P1 `guests` lists Alpha", "Alpha" in out)

        # ---------------- P2: promote
        out = say(host, "promote Alpha auto auto confirm")
        ck("P2 promote: 'PROMOTED to RESIDENT slot 3'", "PROMOTED to RESIDENT slot 3" in out)
        if "PROMOTED to RESIDENT" not in out:
            L.info("promote output: " + out[-800:])
            return L.summary_and_exit_code(results)
        ht = host.log_text()
        ck("P3 the host moved the work record to the promoted resident's PID (log)", "[NET][WORK] host: work record moved to the promoted resident's PID" in ht)
        f = W.parse(wj)
        after = W.by_key(f[2]) if f else {}
        gone = [k for k in before if k not in after]
        new = [k for k in after if k not in before]
        ck("P3 exactly ONE key changed: the old guest key is gone, a new (resident) key exists, the count is unchanged (%d -> %d)" % (len(before), len(after)),
           len(gone) == 1 and len(new) == 1 and len(after) == len(before))
        if gone and new:
            old_r, new_r = before[gone[0]], after[new[0]]
            ck("P3 the moved record kept its job id, state and every other byte (only the key differs)", old_r[20:] == new_r[20:] and new[0] != gone[0])
            other = [k for k in before if k != gone[0]][0]
            ck("P3 BETA's record is byte-identical", other in after and after[other] == before[other])
        ck("P4 the promoted guest's K.K. claim moved to the resident slot's vanilla bit (host log)",
           re.search(r"\[NET\]\[KK\] host: the promoted guest had already taken K\.K\.'s song today: the claim moved to resident slot 3's vanilla bit", ht) is not None)
        ck("host alive", host.alive())
    finally:
        for p in reversed(procs):
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap, save_dir)
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
