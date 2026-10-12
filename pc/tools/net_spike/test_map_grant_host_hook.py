#!/usr/bin/env python3
"""test_map_grant_host_hook.py - RUNTIME test of the host's own first-purchase map grant (aNSC_pc_host_first_purchase_map_grant, the function aNSC_sell_item_init calls).
A REAL host process (AC_TEST_HOOKS=1 AC_TEST_HOST_MAP_GRANT=1) runs the shared function twice once the world is ready: the first call must add exactly one maps[] entry
for this land and set map_flag, the second must add nothing. The Nook dialogue is not driven (the dialogue part of aNSC_sell_item_init is not exercised).
Run from a bin_fixture4* dir: NET_SPIKE_GAME_BIN=<fixture> python test_map_grant_host_hook.py [port]"""
import os, re, shutil, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
os.environ["AC_TEST_HOST_MAP_GRANT"] = "1"
os.environ["AC_TEST_DUMP_MSG"] = "0x109D"  # the vanilla Nook purchase thanks row (aNSC_MSG_BUY_NORMAL = 0x107B + 34): the real loader dump shows the appended map page
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
import net_spike_lib as L  # noqa: E402
import test_txn_real_client as RC  # noqa: E402


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 14400
    results = []
    L.require_test_bin_dir()
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap = os.path.join(os.environ.get("TEMP", "."), "mgh_snapshot_%d" % os.getpid())
    shutil.rmtree(snap, ignore_errors=True)
    shutil.copytree(save_dir, snap)
    rig = RC.Rig(port, results, save_dir, snap)
    ck = rig.check
    try:
        host = rig.start_host("mgh", port, [])
        ck("host field-ready", host is not None)
        if host is not None:
            m1 = host.wait_for_log(r"host map-grant hook: before idx=(-?\d+) count=(\d+) map_flag=(\d+)", 60.0)
            m2 = host.wait_for_log(r"host map-grant hook: first=(\d) second=(\d) after idx=(-?\d+) count=(\d+) map_flag=(\d+)", 20.0)
            ck("hook ran (before + after lines)", m1 is not None and m2 is not None)
            host.wait_for_log(r"initial message 0x109D (loaded|could not be loaded)", 45.0)
            t = host.log_text()
            b = re.search(r"before idx=(-?\d+) count=(\d+) map_flag=(\d+)", t)
            a = re.search(r"first=(\d) second=(\d) after idx=(-?\d+) count=(\d+) map_flag=(\d+)", t)
            print("before:", b.groups() if b else None, " after:", a.groups() if a else None)
            if b and a:
                ck("fresh host save had no map for this land (idx -1)", b.group(1) == "-1")
                ck("first call granted (1), second was idempotent (0)", a.group(1) == "1" and a.group(2) == "0")
                ck("this land is now in maps[] and exactly one entry was added", a.group(3) != "-1" and int(a.group(4)) == int(b.group(2)) + 1)
                ck("map_flag is TRUE after the grant", a.group(5) == "1")
            o = re.search(r"host map-line hook: flag_after_first=(\d+) total_after_first=(\d+) flag_after_second=(\d+) total_after_second=(\d+)", t)
            c = re.search(r"host map-line hook: after consume flag=(\d+) total=(\d+) armed_msg=(-?\d+)", t)
            print("one-shot:", o.groups() if o else None, "consume:", c.groups() if c else None)
            ck("one-shot flag set by the first grant only (total 1 after both calls), consumed by the chain, armed for 0x109D",
               o is not None and c is not None and o.groups() == ("1", "1", "1", "1") and c.groups() == ("0", "1", "4253"))
            m = re.search(r"initial message 0x109D loaded, len (\d+), bytes:([0-9A-F ]+)", t)
            ck("real loader dump of the vanilla thanks row 0x109D appeared", m is not None)
            if m:
                bs = m.group(2).split()
                print("dump len", m.group(1), "tail:", " ".join(bs[-40:]))
                ck("the map page was appended (patch log) and the message still ends with MSGEND", "map line appended to message 0x109d" in t.lower() and bs[-2:] == ["7F", "00"])
                txt = bytes(int(x, 16) for x in bs)
                ck("the appended page text is in the loaded message", b"Here is a map of the town" in txt)
            host.stop()
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
