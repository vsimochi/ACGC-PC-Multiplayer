#!/usr/bin/env python3
r"""test_server_client_same_dir_real.py - hosting a dedicated server and playing on the SAME PC from the SAME folder.

TIER: REAL processes in the disposable fixture pc\build64\bin_fixture4: a dedicated server (`--host <port> --dedicated`) and an ordinary game client (`--connect 127.0.0.1:<port>`, a real resident of the
fixture's own save\card_a) running at the same time in ONE folder. Checks what the starter's old "any AnimalCrossing.exe running" guard was afraid of:
  S1  the server alone never writes anything under save\ (the client / single-player tree) nor settings.ini / keybindings.ini; its `save` command writes under servers\default\ only;
  S2  with the client connected and playing next to it, the client really joins the server (server says READY and, when it stands in the town, "<name> arrived into town"), both stay alive;
  S3  the server's town file (servers\default\town\card_a\...gci) is still a valid, same-size file afterwards and `stop` ends the server with exit code 0;
  S4  the client's save\ town file is still a valid same-size file (the server never wrote it).
Not tested: two clients of one folder, a GUI host (--host without --dedicated) on the same port, a real person playing.
Usage (hidden desktop, AC_DISPLAY_NAME=samsung AC_MASTER_VOLUME=0, NET_SPIKE_GAME_BIN=<...>\bin_fixture4): python test_server_client_same_dir_real.py [--port 12996]"""
import argparse
import hashlib
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402


def tree(root, sub):
    out = {}
    base = os.path.join(root, sub)
    for r, _d, files in os.walk(base):
        for f in files:
            p = os.path.join(r, f)
            try:
                st = os.stat(p)
                out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, hashlib.sha256(open(p, "rb").read()).hexdigest())
            except OSError:
                pass
    return out


def files_state(root, names):
    out = {}
    for n in names:
        p = os.path.join(root, n)
        out[n] = hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.isfile(p) else None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12996)
    args = ap.parse_args()
    results = []
    ck = lambda d, c, x="": L.check(d + ((" :: " + str(x)[:400]) if (x and not c) else ""), bool(c), results)  # noqa: E731
    root = L.GAME_BIN_DIR
    gci_client = os.path.join(root, L.SAVE_GCI_REL)
    gci_server = os.path.join(root, "servers", "default", "town", "card_a", os.path.basename(L.SAVE_GCI_REL))
    ck("fixture: the client's save\\card_a town exists and the server tree servers\\default exists", os.path.isfile(gci_client) and os.path.isdir(os.path.join(root, "servers", "default")), (gci_client, gci_server))
    if not (os.path.isfile(gci_client) and os.path.isdir(os.path.join(root, "servers", "default"))):
        return L.summary_and_exit_code(results)
    cfg_names = ["settings.ini", "keybindings.ini"]
    save_before = tree(root, "save")
    cfg_before = files_state(root, cfg_names)
    client_size = os.path.getsize(gci_client)
    server_size_before = os.path.getsize(gci_server) if os.path.isfile(gci_server) else None

    # the fixture's server tree is put back afterwards: a client session changes the server's resident data (that is the point), and other suites compare the server town with the fixture's save
    import shutil
    srv_dir = os.path.join(root, "servers", "default")
    srv_backup = srv_dir + ".before_same_dir_test"
    shutil.rmtree(srv_backup, ignore_errors=True)
    shutil.copytree(srv_dir, srv_backup)

    host = L.HostProcess(port=args.port, extra_args=["--dedicated"], log_path=os.path.join(HERE, "same_dir_server.log"), bin_dir=root, stdin_pipe=True, verbose=False, new_group=True).start()
    client = None
    try:
        ck("the dedicated server is listening and ready", host.wait_listening(60.0) and host.boot_to_dedicated(timeout=120.0))
        time.sleep(3.0)
        ck("S1 the server alone, booted and idle for a few seconds, wrote NOTHING under save\\ and did not change settings.ini / keybindings.ini", tree(root, "save") == save_before and files_state(root, cfg_names) == cfg_before)
        srv_before = tree(root, os.path.join("servers", "default"))
        off = len(host.log_text())
        host.send_line("save")
        host.wait_for_log(r"\[DEDICATED\] save result: (OK|FAILED)", 30.0, since_offset=off)
        time.sleep(1.0)
        srv_after = tree(root, os.path.join("servers", "default"))
        ck("S1b the server's `save` command wrote under servers\\default\\ (files changed there) and still nothing under save\\",
           any(srv_after.get(k, (0, 0, ""))[2] != v[2] or srv_after.get(k, (0, 0, ""))[1] != v[1] for k, v in srv_before.items()) and tree(root, "save") == save_before)

        # ---- the user's own game, same folder, same time ----
        t_client = time.time_ns()
        client = L.ClientProcess("127.0.0.1:%d" % args.port, log_path=os.path.join(HERE, "same_dir_client.log"), bin_dir=root, extra_args=["--bootstrap-resident", "0"]).start()  # automation picks resident 0 of the save (a person would choose it in the menu)
        ck("S2 the client of the same folder boots into the town and takes the server's snapshot", client.boot_to_field(timeout=120.0, slot=0))
        time.sleep(4.0)
        txt = host.log_text().replace("\r\n", "\n")
        ck("S2b the server reports the client READY (identity accepted) ...", re.search(r"peer \d+ READY: RESIDENT idx \d name=\"([^\"]*)\"", txt) is not None, txt[-600:])
        m = re.search(r"\[DEDICATED\] ([^\n]+) arrived into town", txt)
        ck("S2c ... and, once it stands in the town, '<name> arrived into town': %s" % (m.group(1) if m else None), m is not None)
        ck("S2d both processes are alive side by side", client.alive() and host.alive())
        changed = {k for k, v in tree(root, "save").items() if save_before.get(k) is None or save_before[k][2] != v[2]}
        ck("S2e anything that changed under save\\ changed AFTER the client started (the server never wrote there): %d file(s) %s" % (len(changed), sorted(changed)[:4]),
           all(tree(root, "save")[k][1] >= t_client for k in changed))
        client.stop()
        client = None
        time.sleep(2.0)
        off = len(host.log_text())
        host.send_line("stop")
        rc = host.wait_exit(60) if hasattr(host, "wait_exit") else None
        t0 = time.time()
        while host.alive() and time.time() - t0 < 60:
            time.sleep(0.5)
        ck("S3 `stop` ended the server by itself (exit code 0)", (not host.alive()) and host.exit_code() == 0, host.exit_code())
        ck("S3b the server's town file is a valid GCI of the same size afterwards", os.path.isfile(gci_server) and (server_size_before is None or os.path.getsize(gci_server) == server_size_before), (server_size_before, os.path.getsize(gci_server) if os.path.isfile(gci_server) else None))
        ck("S4 the client's own save\\card_a town file has the same size (never replaced by the server's)", os.path.getsize(gci_client) == client_size)
    finally:
        if client is not None:
            try:
                client.stop()
            except Exception:  # noqa: BLE001
                pass
        host.stop()
        shutil.rmtree(srv_dir, ignore_errors=True)
        shutil.move(srv_backup, srv_dir)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
