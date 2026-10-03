#!/usr/bin/env python3
"""test_shop_real_client.py - Town Services milestone 2 (NOOK'S SHOP), the REAL game client's half, ONE small smoke test: the shop mirror it receives and
applies, one real PURCHASE and one real SALE driven through the real client transaction code. REAL processes.

TIER: REAL HOST + REAL CLIENT game processes, hook-driven (no GUI automation, no manual play):
  host   = `AnimalCrossing.exe --host <port> --bootstrap-resident 0`
  client = `AnimalCrossing.exe --connect 127.0.0.1:<port> --bootstrap-resident <slot> --shop-test-buy | --shop-test-sell`
The client's mirror receive / validate / apply code, the UI-seam entry points the Nook dialogue calls (pc_net_game_shop_buy_stock_code, pc_net_game_ts_begin_shop_buy /
_sell, _poll), the TXN_COMMIT build / send, TXN_RESULT handling and the post-image apply step (pcnetgame_txn_apply_shop) and the host's handler + mirror push all run
for real. Assertions come from the processes' own log lines (order: shop mirror applied at join < request sent < TXN_RESULT < pocket + wallet write < the mirror
of the change) and a byte-exact check of the host record and the host's shop mirror through a fresh scripted FakeClient session after the client left.
The only synthetic inputs are TEST-ONLY, default-off hooks (they bypass the dialogue and the menu, never the request / TXN_RESULT chain): --shop-test-buy
raises the local wallet to 90000 (the hook's one local write, then a 5 s pause so the normal D3 upload carries it) and buys the cheapest buyable item of
the LOCAL mirror; --shop-test-sell places one fish into a free pocket slot (the one local write) and sells it.
NOT covered (no UI automation): the Nook dialogue state machines themselves (their wait / apply / refusal branches are SOURCE AUDITED in test_shop_src.py), the
visual refresh of the shop floor, the REJECTED branches with a real client (host side tested with scripted clients in test_shop_protocol.py), a second real client.

Run ONLY on the disposable pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN=<absolute path>, ports 11110+). The fixture save is snapshotted at start and restored
before every host launch and at the end. One real client at a time.
Usage: python test_shop_real_client.py [--port 11110] [--only r1,r2]
"""
import argparse
import os
import re
import shutil
import sys

import net_spike_lib as L
import test_txn_real_client as RC

CAPS = {0: 25000, 1: 90000, 2: 240000}


def count(rx, text):
    return len(re.findall(rx, text))


def final_state(text):
    m = re.search(r"--shop-test-(?:buy|sell): final item 0x([0-9A-F]{4}) pockets=([0-9A-F,]+) wallet=(\d+) sales_sum\(mirror\)=(\d+) stock\[(-?\d+)\]=0x([0-9A-F]{4})", text)
    if m is None:
        return None
    return dict(item=int(m.group(1), 16), pockets=tuple(int(x, 16) for x in m.group(2).strip(",").split(",")), wallet=int(m.group(3)), sales=int(m.group(4)),
                idx=int(m.group(5)), stock_now=int(m.group(6), 16))


def run(args, results, ip, snap_gci, rig):
    check = rig.check
    host_slot = L.TEST_HOST_RESIDENT
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
    check("fixture has a non-host resident (slots %s)" % residents, len(residents) >= 1)
    if not residents:
        return
    r1 = residents[0]
    gci_inv = L.record_inventory(L.record_from_gci(snap_gci, r1))

    def peek_host_state(port, label):
        f = RC.fake_session(port, ip, r1, label)
        push = f.rec_pushes[-1]
        shop = f.wait_ts_state(L.PC_NETGAME_TS_SHOP, 4.0)
        out = (L.record_inventory(push["data"]), shop)
        RC.release(f)
        return out

    def _r1():
        port = args.port
        host = rig.start_host("shop_r1", port, [])
        check("R1 host reached genuine field-ready state", host is not None)
        if host is None:
            return
        L.resolve_host_town(ip, port)
        cl = rig.start_client("shop_r1", port, r1, ["--shop-test-buy"])
        check("R1 real client booted into the field and connected", cl is not None)
        if cl is None:
            host.stop()
            return
        m = cl.wait_for_log(r"--shop-test-buy: final item 0x([0-9A-F]{4}) ", 150.0)
        check("R1 the hook ran to its final report", m is not None)
        ct, ht = cl.log_text(), host.log_text()
        off = len(ht)
        cl.stop()
        rig.wait_peer_gone(off - 1)
        if m is None:
            host.stop()
            return
        fs = final_state(ct)
        item = int(m.group(1), 16)
        m_req = re.search(r"--shop-test-buy: requesting item 0x([0-9A-F]{4}) pocket slot (\d+) \(price/stock from the mirror\) wallet=(\d+) sales_sum\(mirror\)=(\d+)", ct)
        m_sent = re.search(r"TXN_COMMIT sent kind=SHOP_BUY request=(\d+) nonce=(\d+) seq=(\d+) dest=1 slot=(\d+) item=0x([0-9A-F]{4}) base=\(epoch (\d+), rev (\d+)\)", ct)
        m_res = re.search(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=SHOP_BUY request=(\d+) seq=(\d+) rev=(\d+)", ct)
        m_app = re.search(r"client: APPLIED request (\d+) kind=SHOP_BUY -- host post-image: wallet now (\d+)", ct)
        m_host = re.search(r"SHOP_BUY item=0x([0-9A-F]{4}) stock=0x([0-9A-F]{2}) pocket slot=(\d+) price=(\d+) wallet (\d+) -> (\d+) sales_sum now (\d+) committed", ht)
        check("R1 the client applied the SHOP mirror at join (len 320) before the hook did anything",
              count(r"\[NET\]\[TS\] client: applied service 3 \(SHOP\) seq \d+ digest 0x[0-9A-F]{8} len 320", ct) >= 1
              and ct.find("applied service 3 (SHOP)") < ct.find("--shop-test-buy: wallet raised"))
        check("R1 a SHOP_BUY (dest POCKET, the free slot, the cheapest item of the LOCAL mirror 0x%04X) was sent, answered by TXN_RESULT(APPLIED) with the same seq and applied by the "
              "host post-image" % item,
              all(x is not None for x in (m_req, m_sent, m_res, m_app, m_host)) and int(m_req.group(1), 16) == item == int(m_sent.group(5), 16)
              and int(m_req.group(2)) == int(m_sent.group(4)) and m_sent.group(3) == m_res.group(2) and int(m_res.group(3)) > int(m_sent.group(7)))
        price = int(m_host.group(4)) if m_host else -1
        check("R1 the wallet was debited by the HOST price (%d): 90000 -> %d on the client, the host line says the same" % (price, 90000 - price),
              m_app is not None and m_host is not None and int(m_app.group(2)) == 90000 - price == int(m_host.group(6)) and int(m_host.group(5)) == 90000
              and fs is not None and fs["wallet"] == 90000 - price)
        m_shop2 = None
        if m_res is not None:
            for mm in re.finditer(r"\[NET\]\[TS\] client: applied service 3 \(SHOP\) seq (\d+)", ct):
                if mm.start() > m_res.start():
                    m_shop2 = mm
                    break
        check("R1 order in the client log: shop mirror at join < wallet raised < request sent < TXN_RESULT APPLIED < pocket + wallet write < the shop mirror of the purchase",
              all(x is not None for x in (m_req, m_sent, m_res, m_app, m_shop2)) and ct.find("--shop-test-buy: wallet raised") < m_sent.start() < m_res.start() < m_app.start() < m_shop2.start())
        check("R1 the pockets and the wallet changed ONLY on APPLIED (the hook's invariant check never fired) and nothing was rejected / resent / inconsistent",
              "CHANGED BEFORE APPLIED" not in ct and "REJECTED(" not in ct and "resending the identical" not in ct and "delta merge" not in ct and "inconsistent" not in ct
              and "protocol violation" not in ct)
        check("R1 the client's LOCAL shop (written only by the mirror) shows the stock slot SOLD (the item left items[%s]) and holds the item in its pockets" % (fs and fs["idx"]),
              fs is not None and fs["stock_now"] != item and 0xFE0E <= fs["stock_now"] <= 0xFE18 and fs["pockets"].count(item) == 1)
        lvl = re.findall(r"\[NET\]\[TS\] host: shop level (\d+)", ht)
        s0 = int(m_req.group(4)) if m_req else 0
        check("R1 sales_sum (mirror) rose by the price through the vanilla mSP_PlusSales (%d -> %s, level %s)" % (s0, fs and fs["sales"], lvl[-1] if lvl else None),
              fs is not None and bool(lvl) and fs["sales"] == min(s0 + price, CAPS.get(int(lvl[-1]), 1 << 31)))
        check("R1 host: exactly one 'TXN APPLIED kind=SHOP_BUY' + one committed line, no CONFLICT / INTERNAL / REFUSED",
              count(r"TXN APPLIED kind=SHOP_BUY ", ht) == 1 and count(r"SHOP_BUY item=0x%04X .* committed" % item, ht) == 1 and "CONFLICT" not in ht and "INTERNAL" not in ht
              and "would be REFUSED" not in ht)
        check("R1 the client never refused the host's shop state (no 'refused:' line for service 3)", count(r"TOWN_SVC_STATE service 3 .*refused", ct) == 0)
        inv, shop = peek_host_state(port, "ShopR1rec")
        check("R1 host (new FakeClient session): the pushed record holds the item once and the debited wallet %d; the shop mirror shows the sold stock slot" % (90000 - price),
              fs is not None and shop is not None and tuple(inv[0]) == fs["pockets"] and inv[2] == 90000 - price and list(inv[0]).count(item) == 1 and list(gci_inv[0]).count(item) == 0
              and int.from_bytes(shop[1][0xCE + 2 * fs["idx"]:0xCE + 2 * fs["idx"] + 2], "little") == fs["stock_now"])
        host.stop()

    def _r2():
        port = args.port + 1
        host = rig.start_host("shop_r2", port, [])
        check("R2 host reached genuine field-ready state", host is not None)
        if host is None:
            return
        L.resolve_host_town(ip, port)
        cl = rig.start_client("shop_r2", port, r1, ["--shop-test-sell"])
        check("R2 real client booted into the field and connected", cl is not None)
        if cl is None:
            host.stop()
            return
        m = cl.wait_for_log(r"--shop-test-sell: final item 0x([0-9A-F]{4}) ", 150.0)
        check("R2 the hook ran to its final report", m is not None)
        ct, ht = cl.log_text(), host.log_text()
        off = len(ht)
        cl.stop()
        rig.wait_peer_gone(off - 1)
        if m is None:
            host.stop()
            return
        fs = final_state(ct)
        item = int(m.group(1), 16)
        m_place = re.search(r"--shop-test-sell: placed item 0x([0-9A-F]{4}) into pocket slot (\d+)", ct)
        m_req = re.search(r"--shop-test-sell: requesting item 0x([0-9A-F]{4}) pocket slot (\d+) wallet=(\d+) sales_sum\(mirror\)=(\d+)", ct)
        m_sent = re.search(r"TXN_COMMIT sent kind=SHOP_SELL request=(\d+) nonce=(\d+) seq=(\d+) dest=0 slot=(\d+) item=0x([0-9A-F]{4}) base=\(epoch (\d+), rev (\d+)\)", ct)
        m_res = re.search(r"TXN_RESULT APPLIED(?: \(replayed\))? kind=SHOP_SELL request=(\d+) seq=(\d+) rev=(\d+)", ct)
        m_app = re.search(r"client: APPLIED request (\d+) kind=SHOP_SELL -- host post-image: wallet now (\d+)", ct)
        m_host = re.search(r"SHOP_SELL mask=0x([0-9A-F]{4}) value=(\d+) wallet (\d+) -> (\d+) sales_sum now (\d+) committed", ht)
        check("R2 the client applied the SHOP mirror at join before the hook placed the fish",
              count(r"\[NET\]\[TS\] client: applied service 3 \(SHOP\) seq \d+ digest 0x[0-9A-F]{8} len 320", ct) >= 1 and ct.find("applied service 3 (SHOP)") < ct.find("--shop-test-sell: placed item"))
        check("R2 a SHOP_SELL (dest NONE, the slot the fish sat in, item 0x%04X) was sent, answered by TXN_RESULT(APPLIED) with the same seq and applied by the host post-image" % item,
              all(x is not None for x in (m_place, m_req, m_sent, m_res, m_app, m_host)) and int(m_place.group(1), 16) == item == int(m_sent.group(5), 16)
              and int(m_place.group(2)) == int(m_sent.group(4)) and int(m_host.group(1), 16) == 1 << int(m_place.group(2)) and m_sent.group(3) == m_res.group(2)
              and int(m_res.group(3)) > int(m_sent.group(7)))
        value = int(m_host.group(2)) if m_host else -1
        w0 = int(m_req.group(3)) if m_req else -1
        check("R2 the wallet was credited by the HOST value (%d > 0): %d -> %d on the client, the host line says the same" % (value, w0, w0 + value),
              m_app is not None and m_host is not None and value > 0 and int(m_host.group(3)) == w0 and int(m_host.group(4)) == w0 + value == int(m_app.group(2))
              and fs is not None and fs["wallet"] == w0 + value)
        m_shop2 = None
        if m_res is not None:
            for mm in re.finditer(r"\[NET\]\[TS\] client: applied service 3 \(SHOP\) seq (\d+)", ct):
                if mm.start() > m_res.start():
                    m_shop2 = mm
                    break
        lvl = re.findall(r"\[NET\]\[TS\] host: shop level (\d+)", ht)
        s0 = int(m_req.group(4)) if m_req else 0
        exp_sales = min(s0 + value // 2, CAPS.get(int(lvl[-1]), 1 << 31)) if lvl and value >= 0 else -1
        check("R2 sales_sum (mirror) rose by value / 2 = %d (%d -> %s) and the mirror arrived AFTER the TXN_RESULT" % (value // 2, s0, fs and fs["sales"]),
              fs is not None and fs["sales"] == exp_sales and (exp_sales == s0 or (m_shop2 is not None and m_res is not None and m_shop2.start() > m_res.start())))
        check("R2 the pockets and the wallet changed ONLY on APPLIED and nothing was rejected / resent / inconsistent",
              "CHANGED BEFORE APPLIED" not in ct and "REJECTED(" not in ct and "resending the identical" not in ct and "delta merge" not in ct and "inconsistent" not in ct
              and "protocol violation" not in ct)
        check("R2 the sold fish left the client's pockets (slot %s)" % (m_place and m_place.group(2)), fs is not None and item not in fs["pockets"])
        check("R2 host: exactly one 'TXN APPLIED kind=SHOP_SELL' + one committed line, no CONFLICT / INTERNAL",
              count(r"TXN APPLIED kind=SHOP_SELL ", ht) == 1 and count(r"SHOP_SELL mask=0x[0-9A-F]{4} value=\d+ ", ht) == 1 and "CONFLICT" not in ht and "INTERNAL" not in ht)
        inv, shop = peek_host_state(port, "ShopR2rec")
        check("R2 host (new FakeClient session): the pushed record no longer holds the fish and has the credited wallet %d; the shop stock is unchanged (sold items never enter the shop)" % (w0 + value),
              fs is not None and shop is not None and tuple(inv[0]) == fs["pockets"] and inv[2] == w0 + value and list(inv[0]).count(item) == list(gci_inv[0]).count(item))
        host.stop()

    for name, fn in (("r1", _r1), ("r2", _r2)):
        if name in args.only:
            fn()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11110)
    ap.add_argument("--only", default="r1,r2")
    args = ap.parse_args()
    args.only = set(x.strip() for x in args.only.split(",") if x.strip())
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "shop_real_client_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        run(args, results, ip, snap_gci, rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
